#!/usr/bin/env python3
"""Count the links whose drawn line runs across a box it is not attached to.

The measurement behind ``docs/follow-ups.md`` entry 19. That entry recorded a
defect nobody had a number for — "orthogonal routes go through nodes" — and a
defect without a number is one that can only be declared fixed by looking at a
picture. This prints the number, for any inventory, with and without obstacle
avoidance, so the fix is measured rather than admired::

    tools/route_crossings.py tests/fixtures/obstructed
    tools/route_crossings.py examples/campus --layer l1 --repeat 20

What is counted
---------------

One *crossing* is one (link, box) pair: the drawn polyline of a link passes
through the rectangle of a node it is not an endpoint of. A link that runs
across three switches counts three times, because a diagram with three of those
is three times as wrong as one with a single crossing, and a boolean would hide
that.

The rectangle counted against is the node's own box, **not** the inflated one
the router keeps away from: a route that grazes the clearance ring is doing
exactly what it was asked to, and charging it for that would make the number
impossible to drive to zero.

The wall clock is the other half. Avoidance is only worth having if a render
still costs what a render cost, so the two columns are printed side by side and
``--repeat`` takes the median of several runs.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ is None and __name__ == "__main__":  # pragma: no cover - script entry
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netviz.layout.avoid import crossings
from netviz.layout.geometry import LayoutMode, Routing
from netviz.loader import load_tree
from netviz.render.graph import Layer, build_graph
from netviz.render.options import RenderOptions
from netviz.render.routes import obstacles_of, route_plan


@dataclass(frozen=True, slots=True)
class Tally:
    """What one pass over a drawing found."""

    #: ``(link, box)`` pairs, in the graph's own edge order.
    crossings: tuple[tuple[str, str], ...]
    #: Links that got a line at all.
    routed: int
    #: Links routing moved off the line the local rule would have drawn.
    avoided: int
    #: Links that fell back to the local rule, and why.
    detours: tuple[str, ...]
    #: How many A* searches ran, and how many states they popped between them.
    searched: int
    expansions: int
    #: Seconds one pass took.
    seconds: float

    @property
    def count(self) -> int:
        return len(self.crossings)


def tally(root: Path, *, layer: str, avoid: bool, clearance: float) -> Tally:
    """Route one drawing and count what the result runs across."""
    inventory = load_tree(root)
    graph = build_graph(inventory, layer=Layer(layer))
    options = RenderOptions(routing=Routing.ORTHOGONAL, avoid=avoid)
    started = time.perf_counter()
    plan = route_plan(graph, options)
    seconds = time.perf_counter() - started

    # Counted against the *drawn* boxes, so the number means what a reader sees.
    boxes = obstacles_of(graph, clearance=clearance, annotations=options.annotations)
    hits: list[tuple[str, str]] = []
    routed = 0
    for edge, line in zip(graph.edges, plan.routes, strict=True):
        if line is None:
            continue
        routed += 1
        exempt = frozenset({edge.source, edge.target})
        hits.extend((edge.id, box) for box in crossings(line.corners, boxes, exempt=exempt))
    return Tally(
        crossings=tuple(hits),
        routed=routed,
        avoided=plan.avoided,
        detours=tuple(detour.describe() for detour in plan.detours),
        searched=plan.searched,
        expansions=plan.expansions,
        seconds=seconds,
    )


def report(root: Path, *, layer: str, repeat: int, clearance: float, verbose: bool) -> int:
    inventory = load_tree(root)
    graph = build_graph(inventory, layer=Layer(layer))
    mode = graph.geometry.mode(graph.nodes)
    print(f"{root} — {layer}, {len(graph.nodes)} nodes, {len(graph.edges)} links, {mode} layout")
    if mode is not LayoutMode.FIXED:
        print()
        print(
            "  nothing to measure: this drawing is not fully arranged, so Graphviz routes it\n"
            "  and 'splines=ortho' already avoids nodes. Run 'netviz layout --write' first."
        )
        return 0

    rows = []
    for avoid in (False, True):
        runs = [tally(root, layer=layer, avoid=avoid, clearance=clearance) for _ in range(repeat)]
        head = runs[0]
        rows.append((avoid, head, statistics.median(run.seconds for run in runs)))

    print()
    print(f"{'':<12} {'crossings':>10} {'links cut':>10} {'re-routed':>10} {'median ms':>10}")
    print("-" * 56)
    for avoid, found, seconds in rows:
        cut = len({link for link, _ in found.crossings})
        print(
            f"{'--avoid' if avoid else '--no-avoid':<12} {found.count:>10} {cut:>10} "
            f"{found.avoided:>10} {seconds * 1000:>10.1f}"
        )

    before, after = rows[0][1], rows[1][1]
    print()
    print(
        f"searches: {after.searched}, states popped: {after.expansions}, "
        f"{after.routed} links routed"
    )
    if after.detours:
        print()
        print("gave up on:")
        for line in after.detours:
            print(f"  {line}")
    if verbose and after.crossings:
        print()
        print("still crossing:")
        for link, box in after.crossings:
            print(f"  {link} runs across {box}")
    if verbose and before.crossings:
        print()
        print("was crossing:")
        for link, box in before.crossings:
            print(f"  {link} runs across {box}")
    return 0 if not after.crossings else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="the inventory to route")
    parser.add_argument("--layer", default="l1", help="which view to draw")
    parser.add_argument("--repeat", type=int, default=5, help="rounds to take the median of")
    parser.add_argument(
        "--clearance",
        type=float,
        default=0.0,
        help="grow every box by this before counting a crossing (default: the drawn box)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="name every crossing")
    args = parser.parse_args(argv)
    return report(
        args.root,
        layer=args.layer,
        repeat=args.repeat,
        clearance=args.clearance,
        verbose=args.verbose,
    )


if __name__ == "__main__":  # pragma: no cover - script entry
    raise SystemExit(main())
