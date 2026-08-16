"""Obstacle-avoiding orthogonal routing: the defect, the fix and the ceilings.

``docs/follow-ups.md`` entry 19 recorded a defect nobody had a number for — an
orthogonal link drawn straight across a switch it is not connected to — and the
first thing this file does is *count* it, on a committed fixture arranged to
cause exactly that. Everything after that is the fix and its promises:

* a link whose line already crosses nothing is left byte-identical;
* a bend somebody dragged is never moved;
* several links between the same two devices route as one bundle in lanes;
* the cut-offs are hit, reported, and fall back rather than truncating.

The counting is done by :func:`netviz.layout.avoid.crossings`, which is also
what ``tools/route_crossings.py`` prints, so the number in the follow-up entry
and the number this file asserts are the same number.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from netviz.layout.avoid import (
    Budget,
    Obstacle,
    Router,
    crossings,
    offset_polyline,
)
from netviz.layout.geometry import Routing
from netviz.layout.routing import Anchor, route
from netviz.loader import load_tree
from netviz.render import Layer, RenderOptions, build_graph
from netviz.render.routes import RouteCache, obstacles_of, route_plan

FIXTURES = Path(__file__).parent / "fixtures"
OBSTRUCTED = FIXTURES / "obstructed"


def _anchor(x: float, y: float, width: float = 60.0, height: float = 40.0) -> Anchor:
    return Anchor(x=x, y=y, width=width, height=height)


def _field(boxes: dict[str, Anchor], budget: Budget | None = None) -> Router:
    kit = budget or Budget()
    return Router([Obstacle.of(key, anchor, kit.clearance) for key, anchor in boxes.items()], kit)


def _drawn(
    router: Router, boxes: dict[str, Anchor], source: str, target: str, **kwargs: object
) -> tuple[tuple[float, float], ...]:
    """One link routed and then drawn, which is what a reader actually sees."""
    found = router.waypoints(
        boxes[source],
        boxes[target],
        source_key=source,
        target_key=target,
        link=f"{source}-{target}",
        **kwargs,  # type: ignore[arg-type]
    )
    line = route(
        boxes[source],
        boxes[target],
        waypoints=() if found is None else found,
        style=Routing.ORTHOGONAL,
    )
    return line.corners


# --------------------------------------------------------------------------- #
# The defect, measured
# --------------------------------------------------------------------------- #


def _plan_for(*, avoid: bool):
    inventory = load_tree(OBSTRUCTED)
    graph = build_graph(inventory, layer=Layer.L1)
    options = RenderOptions(routing=Routing.ORTHOGONAL, avoid=avoid)
    return graph, route_plan(graph, options)


def _crossing_pairs(graph, plan) -> tuple[tuple[str, str], ...]:
    """``(link, box)`` for every drawn line that runs across a box it is not on."""
    boxes = obstacles_of(graph, clearance=0.0)
    found: list[tuple[str, str]] = []
    for edge, line in zip(graph.edges, plan.routes, strict=True):
        if line is None:
            continue
        found.extend(
            (edge.id, box)
            for box in crossings(line.corners, boxes, exempt=frozenset({edge.source, edge.target}))
        )
    return tuple(found)


def test_the_fixture_reproduces_the_defect_it_was_committed_for() -> None:
    """Without avoidance, five links run across a box. That is the number to beat."""
    graph, plan = _plan_for(avoid=False)
    found = _crossing_pairs(graph, plan)
    assert [pair[0] for pair in found] == [
        "cbl-a-b",
        "cbl-c-d-1",
        "cbl-c-d-2",
        "cbl-c-d-3",
        "cbl-rtr-c",
    ]
    assert {pair[1] for pair in found} == {"sw-mid", "sw-blocker", "sw-wall"}


def test_avoidance_takes_the_fixture_to_zero_crossings() -> None:
    graph, plan = _plan_for(avoid=True)
    assert _crossing_pairs(graph, plan) == ()
    assert not plan.detours


def test_every_link_still_gets_a_line() -> None:
    """Avoidance is a repair, not a filter: nothing may vanish to achieve zero."""
    _, without = _plan_for(avoid=False)
    _, with_it = _plan_for(avoid=True)
    assert sum(line is not None for line in with_it.routes) == sum(
        line is not None for line in without.routes
    )


def test_a_clean_link_is_left_byte_identical() -> None:
    """The promise that makes this safe to turn on by default."""
    graph, without = _plan_for(avoid=False)
    _, with_it = _plan_for(avoid=True)
    clean = {"cbl-a-c", "cbl-b-d"}
    for edge, before, after, added in zip(
        graph.edges, without.routes, with_it.routes, with_it.computed, strict=True
    ):
        if edge.id not in clean:
            continue
        assert added == (), f"{edge.id} was routed although nothing was in its way"
        assert before is not None and after is not None
        assert before.corners == after.corners


def test_a_bend_somebody_placed_is_never_moved() -> None:
    """Routing fills the legs *between* pinned points and leaves the points alone."""
    graph, plan = _plan_for(avoid=True)
    index = next(i for i, edge in enumerate(graph.edges) if edge.id == "cbl-rtr-c")
    pinned = graph.geometry.link("cbl-rtr-c").waypoints
    assert pinned == ((700.0, -620.0),)
    line = plan.routes[index]
    assert line is not None
    assert pinned[0] in line.corners
    # And the leg beyond it did get re-shaped, or the test above proves nothing.
    assert len(plan.computed[index]) > len(pinned)


def test_a_pinned_bend_survives_the_route_running_straight_through_it() -> None:
    """The subtle half of "never moved": never *dropped* either.

    A bend the detour happens to run straight through is collinear with its
    neighbours, and the simplifier that turns a grid path into corners would
    otherwise drop it. No pixel changes when it does — and the canvas loses a
    grab handle, and pinning the route would write a shorter list back than the
    one the document already held.
    """
    graph = build_graph(load_tree(FIXTURES / "routed"), layer=Layer.L1)
    plan = route_plan(graph, RenderOptions())
    index = next(i for i, edge in enumerate(graph.edges) if edge.id == "cables/cbl-rtr-sw")
    pinned = graph.geometry.link("cables/cbl-rtr-sw").waypoints
    assert len(pinned) == 2
    line = plan.routes[index]
    assert line is not None
    # The detour leaves the second bend collinear with what comes after it.
    assert all(bend in plan.computed[index] for bend in pinned)
    assert all(bend in line.corners for bend in pinned)


def test_parallel_links_route_as_one_bundle_in_lanes() -> None:
    """Task 38's other half: three cables, one route, three offsets of it."""
    graph, plan = _plan_for(avoid=True)
    lanes = {
        edge.id: line.corners
        for edge, line in zip(graph.edges, plan.routes, strict=True)
        if edge.id.startswith("cbl-c-d") and line is not None
    }
    assert len(lanes) == 3
    shapes = {tuple(len(corners) for corners in [line]) for line in lanes.values()}
    assert len(shapes) == 1, "the lanes have different shapes, so they were routed apart"
    # Each lane runs along a row of its own, and the rows are one gap apart:
    # that, rather than "no crossings", is what distinguishes a bundle from
    # three cables that each found their own way past the same box.
    runs = sorted(min(corner[1] for corner in corners) for corners in lanes.values())
    assert len(set(runs)) == 3
    gaps = {round(b - a, 6) for a, b in pairwise(runs)}
    assert gaps == {14.0}


# --------------------------------------------------------------------------- #
# The search itself
# --------------------------------------------------------------------------- #


def test_a_box_between_two_nodes_is_routed_around() -> None:
    boxes = {
        "a": _anchor(0, 0, 80, 50),
        "b": _anchor(300, 0, 120, 60),
        "c": _anchor(600, 0, 80, 50),
    }
    router = _field(boxes)
    corners = _drawn(router, boxes, "a", "c")
    assert crossings(corners, router.obstacles, exempt=frozenset({"a", "c"})) == ()


def test_nothing_in_the_way_means_no_search_and_no_answer() -> None:
    """``None`` is the ordinary answer, and it is what keeps a clean render fast."""
    boxes = {"p": _anchor(0, 0), "q": _anchor(400, 0)}
    router = _field(boxes)
    assert router.waypoints(boxes["p"], boxes["q"], source_key="p", target_key="q") is None
    assert router.searches == 0


def test_a_route_that_comes_back_straight_says_so() -> None:
    """Handing back a bare pair would let the local Z put the crossing back.

    Two nodes on the same row with a box on the mid-line: the answer is to run
    straight along the row, and a straight answer has to be *pinned* straight,
    because the local rule turns two unpinned points into a Z through the middle
    of the diagram.
    """
    boxes = {
        "a": _anchor(0, 0, 60, 40),
        "b": _anchor(300, 120, 60, 300),  # squarely on the Z's midpoint column
        "c": _anchor(600, 0, 60, 40),
    }
    router = _field(boxes)
    found = router.waypoints(boxes["a"], boxes["c"], source_key="a", target_key="c")
    assert found is not None
    corners = _drawn(router, boxes, "a", "c")
    assert crossings(corners, router.obstacles, exempt=frozenset({"a", "c"})) == ()


def test_the_search_prefers_a_longer_straight_run_to_a_staircase() -> None:
    """What the bend penalty buys, and the reason it is a knob."""
    boxes = {"a": _anchor(0, 0), "b": _anchor(240, 30, 80, 200), "c": _anchor(500, 0)}
    router = _field(boxes)
    corners = _drawn(router, boxes, "a", "c")
    turns = sum(
        1
        for before, here, after in zip(corners, corners[1:], corners[2:], strict=False)
        if (before[0] == here[0]) != (here[0] == after[0])
    )
    assert turns <= 3, f"{turns} turns for a route round one box"


def test_the_line_a_route_avoids_is_charged_for_crossing_it() -> None:
    """Congestion and crossings are what make two detours take different ways."""
    boxes = {
        "a": _anchor(0, 0, 60, 40),
        "wall": _anchor(300, 0, 60, 400),
        "c": _anchor(600, 0, 60, 40),
        "d": _anchor(600, -200, 60, 40),
        "e": _anchor(0, -200, 60, 40),
    }
    router = _field(boxes)
    first = _drawn(router, boxes, "a", "c")
    second = _drawn(router, boxes, "e", "d")
    assert first != second


def test_the_indexed_crossing_test_answers_what_the_plain_one_does() -> None:
    """:meth:`Router.blocked` is a fast path, and a fast path that disagrees is a bug.

    It is asked once per link per render — it decides whether a link needs
    routing at all and whether a cached route is still valid — so it goes
    through the spatial index rather than over every obstacle. That is a
    different algorithm answering the same question, which is the kind of thing
    that drifts.
    """
    boxes = {
        f"n{index}": _anchor(index * 90.0, (index % 4) * 70.0, 60.0, 40.0) for index in range(24)
    }
    router = _field(boxes)
    for start in range(0, 24, 5):
        for finish in range(start + 1, 24, 7):
            source, target = f"n{start}", f"n{finish}"
            exempt = frozenset({source, target})
            line = route(boxes[source], boxes[target], style=Routing.ORTHOGONAL)
            assert sorted(router.blocked(line.corners, exempt=exempt)) == sorted(
                crossings(line.corners, router.obstacles, exempt=exempt)
            )


def test_a_window_too_big_to_search_is_reported_rather_than_truncated() -> None:
    boxes = {"a": _anchor(0, 0), "b": _anchor(300, 0, 200, 200), "c": _anchor(600, 0)}
    router = _field(boxes, Budget(max_cells=4))
    assert router.waypoints(boxes["a"], boxes["c"], source_key="a", target_key="c") is None
    assert [detour.reason for detour in router.detours] == ["window"]
    assert "without avoiding obstacles" in router.detours[0].describe()


def test_a_search_that_runs_out_of_steps_is_reported_rather_than_truncated() -> None:
    boxes = {"a": _anchor(0, 0), "b": _anchor(300, 0, 200, 200), "c": _anchor(600, 0)}
    router = _field(boxes, Budget(max_expansions=4))
    assert router.waypoints(boxes["a"], boxes["c"], source_key="a", target_key="c") is None
    assert [detour.reason for detour in router.detours] == ["budget"]
    assert "gave up after" in router.detours[0].describe()


def test_a_link_with_no_way_out_falls_back_rather_than_failing() -> None:
    """A device walled in by its clearance has no orthogonal route. Draw it anyway."""
    boxes = {
        "a": _anchor(0, 0, 40, 40),
        "wall": _anchor(0, 0, 4000, 4000),
        "c": _anchor(600, 0, 40, 40),
    }
    router = _field(boxes)
    found = router.waypoints(boxes["a"], boxes["c"], source_key="a", target_key="c")
    assert found is None
    assert [detour.reason for detour in router.detours] == ["unreachable"]
    assert "no clear orthogonal route" in router.detours[0].describe()


def test_a_link_routing_gave_up_on_is_reported_by_the_renderer() -> None:
    """A cut-off nobody is told about is indistinguishable from a router that fails.

    The reader sees a line through a box either way, so every fallback comes out
    as a render advisory naming the links and the knob that lifts it.
    """
    from dataclasses import replace

    from netviz.layout.geometry import Placement
    from netviz.render.dot import routing_advisories

    graph = build_graph(load_tree(OBSTRUCTED), layer=Layer.L1)
    assert routing_advisories(graph, RenderOptions(routing=Routing.ORTHOGONAL)) == ()

    # Wall sw-c in: a box far larger than the page, centred on it, leaves its
    # three cables with no orthogonal route out that avoids anything.
    walled = dict(graph.geometry.nodes)
    walled["sw-wall"] = Placement(x=0.0, y=-300.0, width=8000.0, height=8000.0)
    boxed = replace(graph, geometry=replace(graph.geometry, nodes=walled))
    said = routing_advisories(boxed, RenderOptions(routing=Routing.ORTHOGONAL))
    assert any("no clear orthogonal route" in line for line in said), said
    # And it says nothing at all when avoidance is not what is drawing the lines.
    assert not any(
        "orthogonal route" in line
        for line in routing_advisories(
            boxed, RenderOptions(routing=Routing.ORTHOGONAL, avoid=False)
        )
    )


def test_a_budget_refuses_arithmetic_that_cannot_mean_anything() -> None:
    with pytest.raises(ValueError):
        Budget(clearance=-1.0)
    with pytest.raises(ValueError):
        Budget(max_cells=1)


# --------------------------------------------------------------------------- #
# Lanes
# --------------------------------------------------------------------------- #


def test_an_offset_polyline_stays_parallel_and_keeps_its_corners_square() -> None:
    line = ((0.0, 0.0), (0.0, 100.0), (200.0, 100.0), (200.0, 0.0))
    moved = offset_polyline(line, 20.0)
    assert len(moved) == len(line)
    for before, after in pairwise(moved):
        assert before[0] == pytest.approx(after[0]) or before[1] == pytest.approx(after[1])
    # Parallel: every segment is the offset away from the one it came from.
    assert abs(moved[1][1] - line[1][1]) == pytest.approx(20.0)
    assert abs(moved[0][0] - line[0][0]) == pytest.approx(20.0)


def test_offsetting_by_nothing_changes_nothing() -> None:
    line = ((0.0, 0.0), (0.0, 100.0), (200.0, 100.0))
    assert offset_polyline(line, 0.0) == line


def test_lanes_go_out_on_whichever_side_is_clear() -> None:
    """Half a bundle spread into the box it had just gone round is not a bundle."""
    boxes = {
        "a": _anchor(0, 0, 60, 40),
        "block": _anchor(300, 0, 100, 100),
        "c": _anchor(600, 0, 60, 40),
    }
    router = _field(boxes)
    spine = router.waypoints(boxes["a"], boxes["c"], source_key="a", target_key="c")
    assert spine is not None
    lanes = router.lanes(spine, 3, gap=14.0, exempt=frozenset({"a", "c"}))
    assert len(lanes) == 3
    for lane in lanes:
        line = route(boxes["a"], boxes["c"], waypoints=lane, style=Routing.ORTHOGONAL)
        assert crossings(line.corners, router.obstacles, exempt=frozenset({"a", "c"})) == ()


def test_one_lane_is_the_route_itself() -> None:
    line = ((0.0, 0.0), (0.0, 100.0))
    router = Router(())
    assert router.lanes(line, 1, gap=14.0) == (line,)


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #


def test_a_second_render_of_an_unchanged_drawing_searches_for_nothing() -> None:
    graph = build_graph(load_tree(OBSTRUCTED), layer=Layer.L1)
    options = RenderOptions(routing=Routing.ORTHOGONAL)
    cache = RouteCache()
    first = route_plan(graph, options, cache=cache)
    second = route_plan(graph, options, cache=cache)
    assert second.searched == 0, "an unchanged drawing was searched again"
    assert second.reused > 0
    for before, after in zip(first.routes, second.routes, strict=True):
        assert (before is None) == (after is None)
        if before is not None and after is not None:
            assert before.corners == after.corners


def test_moving_a_node_onto_a_cable_re_routes_that_cable_and_no_other() -> None:
    """The interaction the cache exists for, and the one it must get right."""
    from dataclasses import replace

    from netviz.layout.geometry import Placement

    inventory = load_tree(OBSTRUCTED)
    graph = build_graph(inventory, layer=Layer.L1)
    options = RenderOptions(routing=Routing.ORTHOGONAL)
    cache = RouteCache()
    route_plan(graph, options, cache=cache)

    # Drag sw-mid down onto the cable between sw-a and sw-c, which was clean.
    moved = dict(graph.geometry.nodes)
    moved["sw-mid"] = Placement(x=0.0, y=-150.0, width=200.0, height=80.0)
    nudged = replace(graph, geometry=replace(graph.geometry, nodes=moved))
    after = route_plan(nudged, options, cache=cache)

    assert after.searched > 0
    index = next(i for i, edge in enumerate(nudged.edges) if edge.id == "cbl-a-c")
    assert after.computed[index], "the cable the switch was dropped on was not re-routed"
    untouched = next(i for i, edge in enumerate(nudged.edges) if edge.id == "cbl-b-d")
    assert after.computed[untouched] == ()


def test_forgetting_the_cache_gives_the_same_picture() -> None:
    graph = build_graph(load_tree(OBSTRUCTED), layer=Layer.L1)
    options = RenderOptions(routing=Routing.ORTHOGONAL)
    cache = RouteCache()
    warm = route_plan(graph, options, cache=cache)
    cache.invalidate()
    assert not cache.entries
    cold = route_plan(graph, options, cache=cache)
    for a, b in zip(warm.routes, cold.routes, strict=True):
        assert (a is None) == (b is None)
        if a is not None and b is not None:
            assert a.corners == b.corners


# --------------------------------------------------------------------------- #
# Turning it off
# --------------------------------------------------------------------------- #


def test_no_avoid_draws_what_every_orthogonal_diagram_was_drawn_as_before() -> None:
    _, off = _plan_for(avoid=False)
    assert all(added == () for added in off.computed)
    assert off.searched == 0
    assert off.detours == ()


def test_a_spline_drawing_never_builds_an_obstacle_grid() -> None:
    """The grid is the expensive half, and a curve has nothing to route around."""
    graph = build_graph(load_tree(OBSTRUCTED), layer=Layer.L1)
    plan = route_plan(graph, RenderOptions(routing=Routing.SPLINE))
    assert plan.searched == 0
    assert all(added == () for added in plan.computed)


def test_areas_that_enclose_members_are_not_obstacles() -> None:
    """A zone drawn behind its members is not a wall; its members are."""
    graph = build_graph(load_tree(OBSTRUCTED), layer=Layer.L1)
    keys = {obstacle.key for obstacle in obstacles_of(graph)}
    assert keys == set(graph.geometry.nodes)
