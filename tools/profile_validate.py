#!/usr/bin/env python3
"""Break the cost of ``netgraph.validate`` down by rule, not by function.

This is the harness behind the per-rule tables in ``docs/follow-ups.md``. It is
not part of the test suite -- like ``tools/bench_pipeline.py`` it takes seconds
and its numbers depend on the machine -- but it is committed so a later
measurement is comparable with an earlier one::

    python tools/profile_validate.py                    # generate, profile, report
    python tools/profile_validate.py --inventory DIR    # profile an existing tree
    python tools/profile_validate.py --top 15           # only the worst 15 rules

Why by rule and not by function: ``validate`` is a fixed list of checks over one
prepared context, so a function-level profile spreads a single rule's cost over
the helpers it shares with a dozen others (``_linked_endpoints``, ``_q``,
``_join``) and hides which *rule* is worth attacking. Each check here is timed
end to end, including the engine work its drafts cause -- the suppression test,
the ``Finding`` construction and the source lookup -- because that work only
exists because the rule yielded something.

``_build_context`` is timed separately and broken down into its own parts: it is
charged to no rule, and on a clean inventory it is usually the largest single
item.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - convenience for a checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from netgraph import validate as validate_module  # noqa: E402
from netgraph.config import ValidationConfig  # noqa: E402
from netgraph.loader import load_tree  # noqa: E402
from netgraph.loader.documents import HAVE_LIBYAML, StrictSafeLoader  # noqa: E402
from netgraph.loader.inventory import Inventory  # noqa: E402
from netgraph.subnets import subnets_of  # noqa: E402
from netgraph.validate import Finding, validate  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tools"))
from bench_pipeline import Shape, generate  # noqa: E402

T = TypeVar("T")


def best(call: Callable[[], T], *, repeat: int) -> tuple[T, float]:
    """Run ``call`` ``repeat`` times; return the last result and the *minimum* ms.

    The minimum rather than the median: a timing sample is bounded below by the
    real cost and unbounded above by whatever else the machine was doing.
    """
    samples: list[float] = []
    result: T = None  # type: ignore[assignment]
    for _ in range(repeat):
        start = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - start) * 1000)
    return result, min(samples)


def time_rules(inventory: Inventory, *, repeat: int) -> list[tuple[str, float, int]]:
    """Time every check in ``_CHECKS`` over one prepared context.

    Returns ``(rule id, milliseconds, findings)`` in report order. The context is
    built once and shared, exactly as ``validate`` shares it, so a rule is not
    charged for work the engine already did for every other rule.
    """
    settings = ValidationConfig()
    context = validate_module._build_context(inventory)
    rows: list[tuple[str, float, int]] = []

    for rule_id, check in validate_module._CHECKS:
        rule = validate_module._RULES_BY_ID[rule_id]
        severity = settings.severity_for(rule_id, rule.severity)

        def run(check: Callable[..., Iterator[object]] = check, rule_id: str = rule_id) -> int:
            found = 0
            for draft in check(context):  # type: ignore[arg-type]
                if context.is_suppressed(rule_id, draft.elements):  # type: ignore[attr-defined]
                    continue
                Finding(
                    rule=rule_id,
                    severity=severity,
                    message=draft.message,  # type: ignore[attr-defined]
                    source=context.source_of(
                        draft.elements[0] if draft.elements else None  # type: ignore[attr-defined]
                    ),
                    elements=draft.elements,  # type: ignore[attr-defined]
                    field_path=draft.field_path,  # type: ignore[attr-defined]
                )
                found += 1
            return found

        count, milliseconds = best(run, repeat=repeat)
        rows.append((rule_id, milliseconds, count))
    return rows


def time_context(inventory: Inventory, *, repeat: int) -> list[tuple[str, float]]:
    """Break ``_build_context`` into the parts a change could attack separately."""
    owners = {
        fqn: element
        for fqn, element in inventory.elements.items()
        if isinstance(element, validate_module._OWNER_TYPES)
    }

    def whole() -> object:
        return validate_module._build_context(inventory)

    def subnets() -> object:
        return subnets_of(inventory)

    def suppressions() -> object:
        return validate_module._collect_suppressions(inventory)

    def per_owner_maps() -> object:
        return (
            {fqn: validate_module._lag_masters(owner) for fqn, owner in owners.items()},
            {fqn: validate_module._stacking_groups(owner) for fqn, owner in owners.items()},
            {
                fqn: {interface.name: interface for interface in owner.interfaces}
                for fqn, owner in owners.items()
            },
            {fqn: validate_module._aggregated_by(owner) for fqn, owner in owners.items()},
        )

    def endpoints() -> object:
        from netgraph.loader.inventory import namespace_of

        out = []
        for cable_fqn, cable in inventory.cables.items():
            namespace = namespace_of(cable_fqn)
            for index, ref in enumerate(cable.endpoints):
                out.append(
                    validate_module._resolve_endpoint(
                        inventory, cable_fqn, cable, ref, index, namespace
                    )
                )
        return out

    parts = [
        ("_build_context (whole)", whole),
        ("  subnets_of", subnets),
        ("  endpoint resolution", endpoints),
        ("  per-owner maps", per_owner_maps),
        ("  suppressions", suppressions),
    ]
    return [(label, best(call, repeat=repeat)[1]) for label, call in parts]


def report(root: Path, *, repeat: int, top: int | None) -> None:
    print(f"inventory: {root}")
    print(f"           parser in use: {StrictSafeLoader.__name__} (libyaml={HAVE_LIBYAML})")

    inventory = load_tree(root)
    if inventory.errors:
        print(f"!! {len(inventory.errors)} load errors, first: {inventory.errors[0]}")
    print(f"           {len(inventory)} elements, {len(inventory.devices)} devices")

    findings, whole_ms = best(lambda: validate(inventory), repeat=repeat)
    print(f"\nvalidate (whole)              {whole_ms:8.1f} ms   {len(findings)} findings")

    print("\n-- context --------------------------------------------------")
    context_rows = time_context(inventory, repeat=repeat)
    for label, milliseconds in context_rows:
        print(f"{label:<30}{milliseconds:8.1f} ms")
    context_ms = context_rows[0][1]

    print("\n-- per rule -------------------------------------------------")
    rows = time_rules(inventory, repeat=repeat)
    ranked = sorted(rows, key=lambda row: -row[1])
    shown = ranked[:top] if top else ranked
    rules_ms = sum(row[1] for row in rows)
    for rule_id, milliseconds, count in shown:
        share = 100 * milliseconds / whole_ms if whole_ms else 0
        print(f"{rule_id:<8}{milliseconds:8.1f} ms  {share:5.1f}%  {count:5d} findings")
    if top and len(ranked) > top:
        rest = sum(row[1] for row in ranked[top:])
        print(f"{'(rest)':<8}{rest:8.1f} ms  {100 * rest / whole_ms:5.1f}%  "
              f"{len(ranked) - top} rules")

    print("\n-- totals ---------------------------------------------------")
    print(f"{'context':<30}{context_ms:8.1f} ms  {100 * context_ms / whole_ms:5.1f}%")
    print(f"{'rules':<30}{rules_ms:8.1f} ms  {100 * rules_ms / whole_ms:5.1f}%")
    accounted = context_ms + rules_ms
    print(f"{'engine + sort (residual)':<30}{whole_ms - accounted:8.1f} ms  "
          f"{100 * (whole_ms - accounted) / whole_ms:5.1f}%")
    print(f"{'median of samples':<30}{statistics.median([whole_ms]):8.1f} ms")


def _inventory_root(args: argparse.Namespace) -> Iterator[Path]:
    if args.inventory is not None:
        yield Path(args.inventory)
        return
    shape = Shape(sites=args.sites, racks_per_site=args.racks, hosts_per_rack=args.hosts)
    target = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="netgraph-vprof-"))
    if target.exists() and args.keep:
        shutil.rmtree(target)
    files, documents = generate(target, shape)
    print(f"generated {files} files / {documents} documents / {shape.devices} devices")
    try:
        yield target
    finally:
        if not args.keep:
            shutil.rmtree(target, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default = Shape()
    parser.add_argument("--sites", type=int, default=default.sites)
    parser.add_argument("--racks", type=int, default=default.racks_per_site)
    parser.add_argument("--hosts", type=int, default=default.hosts_per_rack)
    parser.add_argument("--repeat", type=int, default=5, help="samples per item (minimum wins)")
    parser.add_argument("--top", type=int, default=None, help="only the N most expensive rules")
    parser.add_argument("--keep", help="write the tree here and leave it behind")
    parser.add_argument("--inventory", help="profile an existing tree instead of generating one")
    args = parser.parse_args(argv)

    for root in _inventory_root(args):
        report(root, repeat=args.repeat, top=args.top)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
