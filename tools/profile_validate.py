#!/usr/bin/env python3
"""Break the cost of ``netviz.validate`` down by rule, not by function.

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
exists because the rule yielded something. ``_build_context`` is charged to no
rule and reported separately.

**Every pass is cold.** The inventory is reloaded before each sample and the
rules are timed once each, in report order, exactly as ``validate`` runs them.
That matters since entry 7 of ``docs/follow-ups.md``: an address caches its
prefix on first use, so the *first* rule to look at an address pays for it and a
second ``validate`` over one inventory is not the run anybody's ``netviz
validate`` pays for. A warm profile would move that cost between rules and
flatter the total. The reported figure per item is the minimum over the samples.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - convenience for a checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from netviz import validate as validate_module  # noqa: E402
from netviz.config import ValidationConfig  # noqa: E402
from netviz.loader import load_tree  # noqa: E402
from netviz.loader.documents import HAVE_LIBYAML, StrictSafeLoader  # noqa: E402
from netviz.loader.inventory import Inventory  # noqa: E402
from netviz.validate import Finding, validate  # noqa: E402

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


def one_cold_pass(inventory: Inventory) -> tuple[float, list[tuple[str, float, int]]]:
    """One full ``validate`` over a freshly loaded inventory, timed per rule.

    Returns the cost of ``_build_context`` and, per rule in report order, its
    cost and the number of findings it produced.
    """
    settings = ValidationConfig()

    start = time.perf_counter()
    context = validate_module._build_context(inventory)
    context_ms = (time.perf_counter() - start) * 1000

    rows: list[tuple[str, float, int]] = []
    for rule_id, check in validate_module._CHECKS:
        rule = validate_module._RULES_BY_ID[rule_id]
        severity = settings.severity_for(rule_id, rule.severity)

        start = time.perf_counter()
        found = 0
        for draft in check(context):
            if context.is_suppressed(rule_id, draft.elements):
                continue
            Finding(
                rule=rule_id,
                severity=severity,
                message=draft.message,
                source=context.source_of(draft.elements[0] if draft.elements else None),
                elements=draft.elements,
                field_path=draft.field_path,
            )
            found += 1
        rows.append((rule_id, (time.perf_counter() - start) * 1000, found))

    return context_ms, rows


def cold_profile(root: Path, *, repeat: int) -> tuple[float, list[tuple[str, float, int]]]:
    """``repeat`` cold passes; the minimum of each item across them."""
    context_samples: list[float] = []
    rule_samples: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    order: list[str] = []

    for _ in range(repeat):
        context_ms, rows = one_cold_pass(load_tree(root))
        context_samples.append(context_ms)
        for rule_id, milliseconds, found in rows:
            if rule_id not in rule_samples:
                rule_samples[rule_id] = []
                order.append(rule_id)
            rule_samples[rule_id].append(milliseconds)
            counts[rule_id] = found

    return min(context_samples), [
        (rule_id, min(rule_samples[rule_id]), counts[rule_id]) for rule_id in order
    ]


def report(root: Path, *, repeat: int, top: int | None) -> None:
    print(f"inventory: {root}")
    print(f"           parser in use: {StrictSafeLoader.__name__} (libyaml={HAVE_LIBYAML})")

    inventory = load_tree(root)
    if inventory.errors:
        print(f"!! {len(inventory.errors)} load errors, first: {inventory.errors[0]}")
    print(f"           {len(inventory)} elements, {len(inventory.devices)} devices")

    def cold_validate() -> float:
        """One ``validate`` over a tree loaded outside the clock."""
        fresh = load_tree(root)
        start = time.perf_counter()
        validate(fresh)
        return (time.perf_counter() - start) * 1000

    # Cold, and reloaded per sample: see the module docstring.
    cold_ms = min(cold_validate() for _ in range(repeat))
    _, warm_ms = best(lambda: validate(inventory), repeat=repeat)
    print(f"\nvalidate (cold, per sample)   {cold_ms:8.1f} ms")
    print(f"validate (warm, same tree)    {warm_ms:8.1f} ms")

    context_ms, rows = cold_profile(root, repeat=repeat)
    ranked = sorted(rows, key=lambda row: -row[1])
    shown = ranked[:top] if top else ranked
    rules_ms = sum(row[1] for row in rows)

    print("\n-- per rule (cold) ------------------------------------------")
    print(f"{'_build_context':<8}{context_ms:8.1f} ms  {100 * context_ms / cold_ms:5.1f}%")
    for rule_id, milliseconds, count in shown:
        share = 100 * milliseconds / cold_ms if cold_ms else 0
        print(f"{rule_id:<8}{milliseconds:8.1f} ms  {share:5.1f}%  {count:5d} findings")
    if top and len(ranked) > top:
        rest = sum(row[1] for row in ranked[top:])
        print(
            f"{'(rest)':<8}{rest:8.1f} ms  {100 * rest / cold_ms:5.1f}%  {len(ranked) - top} rules"
        )

    print("\n-- totals ---------------------------------------------------")
    accounted = context_ms + rules_ms
    print(f"{'context':<30}{context_ms:8.1f} ms  {100 * context_ms / cold_ms:5.1f}%")
    print(f"{'rules':<30}{rules_ms:8.1f} ms  {100 * rules_ms / cold_ms:5.1f}%")
    # Each figure above is a minimum over independent samples, and minima do not
    # add up: the remainder is the engine and the final sort plus whatever the
    # per-item minima happened not to land in the same pass. It goes negative
    # once the per-item costs are small, which is a property of the statistic
    # rather than a measurement to read.
    print(
        f"{'engine, sort and slack':<30}{cold_ms - accounted:8.1f} ms  "
        f"{100 * (cold_ms - accounted) / cold_ms:5.1f}%"
    )


def _inventory_root(args: argparse.Namespace) -> Iterator[Path]:
    if args.inventory is not None:
        yield Path(args.inventory)
        return
    shape = Shape(sites=args.sites, racks_per_site=args.racks, hosts_per_rack=args.hosts)
    target = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="netviz-vprof-"))
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
    parser.add_argument("--repeat", type=int, default=5, help="cold passes (minimum wins)")
    parser.add_argument("--top", type=int, default=None, help="only the N most expensive rules")
    parser.add_argument("--keep", help="write the tree here and leave it behind")
    parser.add_argument("--inventory", help="profile an existing tree instead of generating one")
    args = parser.parse_args(argv)

    for root in _inventory_root(args):
        report(root, repeat=args.repeat, top=args.top)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
