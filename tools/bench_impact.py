#!/usr/bin/env python3
"""Time ``netviz impact`` on a thousand-device tree, stage by stage.

``bench_pipeline.py`` measures what it costs to *draw* an inventory. This
measures what it costs to *ask a question of* one, which is a different shape of
work and has a different thing to be afraid of. Drawing is dominated by parsing
and by Graphviz; failure analysis is dominated by whether the algorithms are
linear::

    python tools/bench_impact.py                    # generate, measure, report
    python tools/bench_impact.py --inventory ./net  # an existing tree
    python tools/bench_impact.py --sites 2          # something smaller
    python tools/bench_impact.py --json out.json    # for a guard to read

What is measured, and why each one is here:

**anchors** — deriving the designated gateways. One scan of every interface, so
it should be invisible; it is here because it is the one stage that grows with
the number of *addresses* rather than with the number of elements.

**views** — building the layer-1, layer-2 and layer-3 graphs. The floor under
everything else: it is a resolution pass plus a broadcast-domain walk, and
nothing this command does can be faster than it.

**--fail (one element)** — the whole simulation, including the second resolution
pass over the pruned inventory. This is the stage that pays for exactness (see
:mod:`netviz.impact.engine`), so the number worth watching is its ratio to
**views**: anything much above 2x means something is being rebuilt that should
have been carried over.

**--spof** — every articulation point and every bridge of all three layers, with
the isolation counts. On a generated tree this is the worst case there is: a tree
has no redundancy at all, so *every* internal node is an articulation point and
*every* cable is a bridge, and the answer runs to more than a thousand entries.
The naive implementation — remove each candidate, re-traverse — is O(V·(V+E))
and takes minutes here; :func:`netviz.connectivity.analyse` gets the same
answer out of one depth-first search, in single-digit milliseconds per layer.

**--redundancy** — the validation pass the expectations are graded by, with an
expectation annotated on every rack switch so the rules actually run.

Each is reported with the median of five runs. Run it after any change to
:mod:`netviz.connectivity` or :mod:`netviz.impact`; the numbers in
``docs/commands/impact.md`` came out of it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - convenience for a checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT / "tools") not in sys.path:  # pragma: no cover - importing the sibling harness
    sys.path.insert(0, str(REPO_ROOT / "tools"))

from bench_pipeline import Shape, generate, yaml_files  # noqa: E402

from netviz.connectivity import analyse  # noqa: E402
from netviz.impact import LAYERS, simulate, views  # noqa: E402
from netviz.impact.engine import anchors_for  # noqa: E402
from netviz.loader import load_tree  # noqa: E402
from netviz.validate import validate  # noqa: E402

T = TypeVar("T")

#: Rounds per measurement. The median of five: the first touch of any of these
#: paths warms a cache somewhere, and a mean would carry that warm-up forward.
SAMPLES: Final = 5

#: The annotation the ``--redundancy`` measurement puts on every rack switch, so
#: the rules being timed have something to grade. Timing them against an
#: inventory that declares nothing would time the early return.
EXPECTATION: Final = "  annotations:\n    netviz/redundancy: gateway\n"


def timed(label: str, call: Callable[[], T]) -> tuple[T, float]:
    """Run ``call`` :data:`SAMPLES` times; report the median, return the last result."""
    samples = []
    result: T
    for _ in range(SAMPLES):
        start = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - start) * 1000)
    median = statistics.median(samples)
    print(f"{label:<34} {median:8.1f} ms   (min {min(samples):.1f}, n={SAMPLES})")
    return result, median


def annotate(root: Path) -> int:
    """Put a ``gateway`` expectation on every rack switch. Returns how many."""
    count = 0
    for path in yaml_files(root):
        text = path.read_text(encoding="utf-8")
        if "kind: switch" not in text or "netviz/redundancy" in text:
            continue
        lines = text.splitlines(keepends=True)
        out: list[str] = []
        for line in lines:
            out.append(line)
            if line.startswith("  name: sw-"):
                out.append(EXPECTATION)
                count += 1
        path.write_text("".join(out), encoding="utf-8")
    return count


def report(root: Path, *, annotated: int) -> dict[str, Any]:
    files = yaml_files(root)
    size = sum(path.stat().st_size for path in files)
    print(f"inventory: {root}")
    print(f"           {len(files)} files, {size / 1_000_000:.1f} MB")

    inventory, load_ms = timed("load_tree", lambda: load_tree(root))
    if inventory.errors:
        print(f"!! {len(inventory.errors)} load errors, first: {inventory.errors[0]}")
    print(f"           {len(inventory)} elements, {len(inventory.devices)} devices")
    print(f"           {annotated} elements carry a redundancy expectation")
    print()

    anchors, anchor_ms = timed("derive anchors", lambda: anchors_for(inventory, ())[0])
    built, views_ms = timed("build views (l1, l2, l3)", lambda: views(inventory, LAYERS))
    for view in built:
        print(
            f"           {view.layer:<3} {len(view.graph.nodes):5d} nodes, "
            f"{len(view.graph.edges):5d} links, {view.graph.endpoint_count:5d} endpoints"
        )

    target = _busiest(inventory)
    print()
    failed, fail_ms = timed(
        f"--fail {target}", lambda: simulate(inventory, fail=[target], wanted_layers=LAYERS)
    )
    print(f"           {len(failed.layers[0].isolated)} isolated at layer 1")

    swept, spof_ms = timed(
        "--spof (l1, l2, l3 + power)",
        lambda: simulate(inventory, spof=True, wanted_layers=LAYERS, limit=25),
    )
    print(f"           {swept.spof_total} single points of failure, worst 25 materialised")

    _, analyse_ms = timed("  of which: analyse(l1) alone", lambda: analyse(built[0].graph, anchors))

    _, checks_ms = timed("--redundancy (validate)", lambda: validate(inventory))
    print()
    print(f"total for a --fail run: {load_ms + fail_ms:.0f} ms, of which {load_ms:.0f} ms is load")
    print(f"total for a --spof run: {load_ms + spof_ms:.0f} ms")

    return {
        "files": len(files),
        "bytes": size,
        "elements": len(inventory),
        "devices": len(inventory.devices),
        "annotated": annotated,
        "anchors": len(anchors),
        "spofTotal": swept.spof_total,
        "ms": {
            "load": round(load_ms, 1),
            "anchors": round(anchor_ms, 1),
            "views": round(views_ms, 1),
            "fail": round(fail_ms, 1),
            "spof": round(spof_ms, 1),
            "analyseL1": round(analyse_ms, 1),
            "validate": round(checks_ms, 1),
        },
    }


def _busiest(inventory: Any) -> str:
    """The element with the most cables on it — the failure worth measuring.

    Failing a leaf host measures nothing: it isolates itself and the traversal
    stops. The generated tree's core routers are what a maintenance window is
    actually about.
    """
    counts: dict[str, int] = {}
    for cable in inventory.cables.values():
        for endpoint in cable.endpoints:
            counts[endpoint.device] = counts.get(endpoint.device, 0) + 1
    return max(sorted(counts), key=lambda name: counts[name]) if counts else ""


@contextmanager
def _inventory_root(args: argparse.Namespace) -> Iterator[tuple[Path, int]]:
    if args.inventory is not None:
        root = Path(args.inventory)
        yield root, annotate(root) if args.annotate else 0
        return
    shape = Shape(
        sites=args.sites,
        racks_per_site=args.racks,
        hosts_per_rack=args.hosts,
    )
    with tempfile.TemporaryDirectory(prefix="netviz-bench-impact-") as directory:
        root = Path(directory)
        files, documents = generate(root, shape)
        print(f"generated {documents} documents in {files} files")
        yield root, annotate(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inventory", help="measure this tree instead of a generated one")
    # ``Shape`` uses slots, so its class attributes are descriptors rather than
    # the defaults; one instance is where the defaults actually live.
    shape = Shape()
    parser.add_argument("--sites", type=int, default=shape.sites)
    parser.add_argument("--racks", type=int, default=shape.racks_per_site)
    parser.add_argument("--hosts", type=int, default=shape.hosts_per_rack)
    parser.add_argument(
        "--annotate",
        action="store_true",
        help="with --inventory, write redundancy expectations into it first",
    )
    parser.add_argument("--json", help="also write the measurements here")
    args = parser.parse_args(argv)

    with _inventory_root(args) as (root, annotated):
        measurements = report(root, annotated=annotated)
    if args.json:
        Path(args.json).write_text(json.dumps(measurements, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - a harness, not a library
    raise SystemExit(main())
