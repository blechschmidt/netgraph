"""Stored diagram geometry: the model, the merge, the renderers and the command.

The property that matters, and the one most of this file is about: **a stored
arrangement round-trips through render unchanged**. Seed a diagram, render it,
read the coordinates back out of Graphviz, and get the numbers that were stored —
not close to them, them. Everything else is in service of that: the model exists
to hold the numbers, the merge exists to find them, and the command exists to
produce them in the first place.

The tests that run Graphviz are marked ``requires_dot``; the rest do not need it,
including the arithmetic that makes the partial case exact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from netviz.cli import cli
from netviz.edit import EditSession, SetGeometry
from netviz.edit.errors import OperationError
from netviz.layout.document import canonical_geometry, geometry_sections, inline_entry
from netviz.layout.geometry import (
    Box,
    Geometry,
    LabelPlacement,
    LayoutMode,
    LinkGeometry,
    Placement,
    Routing,
    round_coordinate,
)
from netviz.layout.graphviz import (
    Drawing,
    Transform,
    fit_transform,
    parse_drawing,
    realign,
    separate,
)
from netviz.layout.resolve import conflicts_in, resolve_geometry, resolve_key
from netviz.layout.routing import FAN_GAP, Anchor, fan_offsets, label_position, route
from netviz.layout.seed import (
    LAYOUT_ENGINES,
    live_keys,
    seed_geometry,
    views_for,
    write_operations,
)
from netviz.loader import load_tree
from netviz.models import LAYOUT_VIEWS, parse_layout
from netviz.models.layout import MAX_COORDINATE
from netviz.render import Layer, RenderOptions, build_graph, filter_graph
from netviz.render.dot import complete_layout, layout_plan, run_graphviz, to_dot
from netviz.render.graph import FilterSpec
from netviz.render.jsonexport import to_json
from netviz.render.routes import fans_of

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


#: Two switches and the cable between them: the smallest inventory that draws
#: more than one node, so an arrangement of it has something to arrange.
PAIR = """\
apiVersion: netviz.dev/v1alpha1
kind: switch
metadata:
  name: sw-a
spec:
  interfaces:
    - name: port1
      type: ethernet
      mtu: 1500
      ipv4: [10.0.0.1/24]
---
apiVersion: netviz.dev/v1alpha1
kind: switch
metadata:
  name: sw-b
spec:
  interfaces:
    - name: port1
      type: ethernet
      mtu: 1500
      ipv4: [10.0.0.2/24]
---
apiVersion: netviz.dev/v1alpha1
kind: cable
metadata:
  name: cbl-a-b
spec:
  endpoints: [sw-a:port1, sw-b:port1]
  medium: copper
"""


def layout_document(body: str, *, name: str = "layout") -> str:
    return (
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: layout\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  views:\n"
        f"{body}"
    )


@pytest.fixture
def pair(tmp_path: Path) -> Path:
    write(tmp_path, "net.yaml", PAIR)
    return tmp_path


def run(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["-i", str(root), *args], catch_exceptions=False)


def drawn_positions(graph: Any, options: RenderOptions | None = None) -> dict[str, tuple]:
    """Where Graphviz actually put each node when this graph was rendered."""
    opts = options or RenderOptions()
    completed = complete_layout(graph, opts)
    payload, _ = run_graphviz(
        to_dot(completed, opts, target="svg"), format="json", plan=layout_plan(completed)
    )
    drawing = parse_drawing(payload)
    return {name: (place.x, place.y) for name, place in drawing.nodes.items()}


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


def test_the_views_are_exactly_the_layers_that_are_drawn() -> None:
    """``LAYOUT_VIEWS`` is written out; this is what stops it drifting."""
    assert tuple(layer.value for layer in Layer) == LAYOUT_VIEWS


def test_a_point_may_be_written_as_a_pair_or_as_a_mapping() -> None:
    body = "    l1:\n      nodes:\n        sw-a: {position: [12, 34]}\n"
    layout = parse_layout_text(layout_document(body))
    assert layout.spec.views["l1"].nodes["sw-a"].position.as_tuple() == (12.0, 34.0)


def parse_layout_text(text: str) -> Any:
    import yaml

    return parse_layout(yaml.safe_load(text))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("    nope:\n      nodes: {}\n", "unknown view"),
        ("    l1:\n      nodes:\n        a: {position: [1, 2, 3]}\n", "exactly two numbers"),
        ("    l1:\n      nodes:\n        a: {position: {x: .nan, y: 0}}\n", "not a finite"),
        (
            f"    l1:\n      nodes:\n        a: {{position: [{MAX_COORDINATE * 2}, 0]}}\n",
            "further than",
        ),
        ("    l1:\n      nodes:\n        a: {position: [0, 0], size: [0, 4]}\n", "greater than 0"),
        ("    l1:\n      edges:\n        a: {}\n", "must say something about the link"),
        ("    l1:\n      edges:\n        a: {routing: diagonal}\n", "routing"),
        ("    l1:\n      edges:\n        a: {waypoints: [[0, 0]], label: {at: 2}}\n", "at"),
        ("    l1:\n      nodes: []\n", "must be a mapping"),
    ],
)
def test_a_malformed_arrangement_is_refused_with_a_reason(body: str, expected: str) -> None:
    with pytest.raises(Exception) as caught:
        parse_layout_text(layout_document(body))
    assert expected in str(caught.value)


def test_a_layout_is_not_an_element(pair: Path) -> None:
    """It is indexed apart, so it cannot collide with a device or be drawn."""
    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [0, 0]}\n"),
    )
    inventory = load_tree(pair)
    assert "layout" in inventory.layouts
    assert "layout" not in inventory.elements
    assert len(build_graph(inventory).nodes) == 2


def test_a_layout_may_share_a_name_with_a_device(pair: Path) -> None:
    """Separate name spaces: nothing ever resolves one where the other is meant."""
    write(pair, "arrange.yaml", layout_document("    l1: {}\n", name="sw-a"))
    inventory = load_tree(pair)
    assert inventory.errors == []
    assert "sw-a" in inventory.layouts
    assert "sw-a" in inventory.devices


def test_two_layouts_of_one_name_are_a_load_error(pair: Path) -> None:
    write(pair, "one.yaml", layout_document("    l1: {}\n"))
    write(pair, "two.yaml", layout_document("    l2: {}\n"))
    (error,) = load_tree(pair).errors
    assert error.rule == "NG-Y002"
    assert "duplicate layout name" in error.message


# --------------------------------------------------------------------------- #
# Resolution and merging
# --------------------------------------------------------------------------- #


def test_a_short_key_resolves_against_the_layouts_own_namespace(tmp_path: Path) -> None:
    write(tmp_path, "sites/hq/net.yaml", PAIR)
    write(
        tmp_path,
        "sites/hq/arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [7, 9]}\n"),
    )
    geometry = resolve_geometry(load_tree(tmp_path), "l1")
    assert geometry.nodes["sites/hq/sw-a"] == Placement(x=7.0, y=9.0)


def test_a_derived_node_id_is_taken_verbatim(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document("    l3:\n      nodes:\n        subnet:10.0.0.0/24: {position: [1, 2]}\n"),
    )
    geometry = resolve_geometry(load_tree(pair), "l3")
    assert geometry.nodes["subnet:10.0.0.0/24"] == Placement(x=1.0, y=2.0)


def test_a_group_key_is_qualified_by_the_documents_namespace(tmp_path: Path) -> None:
    write(tmp_path, "sites/hq/net.yaml", PAIR)
    write(
        tmp_path,
        "sites/hq/arrange.yaml",
        layout_document(
            "    l1:\n      groups:\n        access: {position: [1, 2], size: [3, 4]}\n"
        ),
    )
    geometry = resolve_geometry(load_tree(tmp_path), "l1")
    assert set(geometry.groups) == {"sites/hq/access"}


def test_the_first_document_to_place_a_node_wins_and_the_clash_is_reported(pair: Path) -> None:
    write(
        pair,
        "a-first.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [1, 1]}\n", name="one"),
    )
    write(
        pair,
        "b-second.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [9, 9]}\n", name="two"),
    )
    inventory = load_tree(pair)
    assert resolve_geometry(inventory, "l1").nodes["sw-a"] == Placement(x=1.0, y=1.0)
    (conflict,) = conflicts_in(inventory, ["l1"])
    assert (conflict.view, conflict.section, conflict.key, conflict.layout) == (
        "l1",
        "nodes",
        "sw-a",
        "one",
    )


def test_an_inventory_with_no_layout_pays_nothing(pair: Path) -> None:
    geometry = resolve_geometry(load_tree(pair), "l1")
    assert geometry.is_empty
    assert build_graph(load_tree(pair)).geometry.is_empty


def test_resolve_key_falls_back_to_the_key_itself() -> None:
    inventory = load_tree(EXAMPLES / "home-lab")
    assert resolve_key("nothing-here", inventory=inventory, namespace="") == "nothing-here"


# --------------------------------------------------------------------------- #
# Mode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("placed", "expected"),
    [((), LayoutMode.AUTO), (("a",), LayoutMode.PARTIAL), (("a", "b"), LayoutMode.FIXED)],
)
def test_the_mode_follows_how_much_is_placed(placed: tuple[str, ...], expected: LayoutMode) -> None:
    geometry = Geometry(nodes={key: Placement(0, 0) for key in placed})
    assert geometry.mode(("a", "b")) is expected


def test_an_empty_drawing_is_never_fixed() -> None:
    """Nothing to reproduce; sending it through the no-op engine buys nothing."""
    assert Geometry(nodes={"a": Placement(0, 0)}).mode(()) is LayoutMode.AUTO


def test_a_filtered_graph_is_judged_on_what_is_left(pair: Path) -> None:
    """Coordinates for nodes a filter removed must not make the rest 'partial'."""
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        sw-a: {position: [1, 1]}\n"
            "        sw-b: {position: [2, 2]}\n"
        ),
    )
    graph = filter_graph(build_graph(load_tree(pair)), FilterSpec(names=("sw-a",)))
    assert set(graph.geometry.nodes) == {"sw-a"}
    assert layout_plan(graph).mode is LayoutMode.FIXED


# --------------------------------------------------------------------------- #
# The DOT document
# --------------------------------------------------------------------------- #


def test_a_fixed_arrangement_emits_positions_and_asks_for_the_no_op_engine(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        sw-a: {position: [54, 18]}\n"
            "        sw-b: {position: [54, 126]}\n"
        ),
    )
    graph = build_graph(load_tree(pair))
    source = to_dot(graph)
    assert 'pos="54,18"' in source
    assert 'pos="54,126"' in source
    plan = layout_plan(graph)
    assert plan.argv("dot", format="svg") == ["dot", "-Kneato", "-n2", "-Tsvg"]


def test_a_partial_arrangement_pins_what_it_has(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [54, 18]}\n"),
    )
    graph = build_graph(load_tree(pair))
    source = to_dot(graph)
    assert 'pos="54,18!"' in source
    assert "inputscale=72" in source
    assert layout_plan(graph).argv("dot", format="svg") == ["dot", "-Kneato", "-Tsvg"]


def test_an_unarranged_graph_produces_exactly_the_document_it_always_did(pair: Path) -> None:
    source = to_dot(build_graph(load_tree(pair)))
    assert "pos=" not in source
    assert "inputscale" not in source
    assert "_background" not in source
    assert layout_plan(build_graph(load_tree(pair))).argv("dot", format="svg") == ["dot", "-Tsvg"]


def test_stored_group_boxes_are_drawn_as_a_background(tmp_path: Path) -> None:
    """``neato`` draws no clusters, so a fixed layout draws the frames itself."""
    write(tmp_path, "edge/net.yaml", PAIR)
    write(
        tmp_path,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        edge/sw-a: {position: [54, 18]}\n"
            "        edge/sw-b: {position: [54, 126]}\n"
            "      groups:\n"
            "        edge: {position: [54, 72], size: [120, 160]}\n"
        ),
    )
    graph = build_graph(load_tree(tmp_path))
    assert graph.geometry.groups == {"edge": Box(x=54.0, y=72.0, width=120.0, height=160.0)}
    source = to_dot(graph, RenderOptions(group_by_namespace=True))
    assert "_background=" in source
    assert "#9ca3af" in source, "the frame is drawn in the same grey a cluster is"
    assert '"cluster-label:edge"' in source, "and the caption is a node, not a draw operation"
    # Without the grouping there is no frame to draw, and no background either.
    assert "_background=" not in to_dot(graph)


def test_the_background_carries_no_text_operation(tmp_path: Path) -> None:
    """A ``T`` in a ``_background`` segfaults Graphviz 2.43 — see follow-up 17.

    Conditionally, on whether anything else in the document has established a
    font, which is what makes it worth a test rather than a comment: a diagram
    would render for months and then crash when a device was deleted.
    """
    write(tmp_path, "edge/net.yaml", PAIR)
    write(
        tmp_path,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        edge/sw-a: {position: [54, 18]}\n"
            "        edge/sw-b: {position: [54, 126]}\n"
            "      groups:\n"
            "        edge: {position: [54, 72], size: [120, 160]}\n"
        ),
    )
    source = to_dot(build_graph(load_tree(tmp_path)), RenderOptions(group_by_namespace=True))
    (background,) = re.findall(r'_background="([^"]*)"', source)
    assert " T " not in f" {background} ", background
    assert " F " not in f" {background} ", background


def test_edge_waypoints_become_a_route_through_them(pair: Path) -> None:
    """The bends are interior: the two ends of the route are the nodes.

    So the emitted ``pos`` passes through every waypoint and stops at each
    shape's border rather than starting at the first bend — which is what lets
    dragging a node carry its cables along instead of stranding them.
    """
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        sw-a: {position: [54, 18], size: [60, 20]}\n"
            "        sw-b: {position: [54, 126], size: [60, 20]}\n"
            "      edges:\n"
            "        cbl-a-b: {waypoints: [[90, 50], [90, 94]]}\n"
        ),
    )
    source = to_dot(build_graph(load_tree(pair)))
    (pos,) = re.findall(r'pos="([^"]+)"[^]]*label="port1', source)
    points = [tuple(float(part) for part in point.split(",")) for point in pos.split()]
    assert (90.0, 50.0) in points and (90.0, 94.0) in points
    # Clipped: the route leaves sw-a's box (18 ± 10) rather than its centre.
    assert points[0][1] >= 28.0 and points[-1][1] <= 116.0


def test_a_per_link_routing_style_beats_the_view_default(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      routing: orthogonal\n      nodes:\n"
            "        sw-a: {position: [54, 18], size: [60, 20]}\n"
            "        sw-b: {position: [200, 126], size: [60, 20]}\n"
            "      edges:\n"
            "        cbl-a-b: {routing: straight}\n"
        ),
    )
    geometry = build_graph(load_tree(pair)).geometry
    assert geometry.routing is Routing.ORTHOGONAL
    assert geometry.routing_for("cbl-a-b") is Routing.STRAIGHT
    assert geometry.routing_for("anything-else") is Routing.ORTHOGONAL


def test_a_nudged_label_is_pinned_with_lp(pair: Path) -> None:
    """``lp`` is an output attribute everywhere but the no-op engine, which is
    the one place a label position can be pinned at all."""
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        sw-a: {position: [54, 18], size: [60, 20]}\n"
            "        sw-b: {position: [54, 126], size: [60, 20]}\n"
            "      edges:\n"
            "        cbl-a-b: {label: {at: 0.25, offset: [30, 0]}}\n"
        ),
    )
    source = to_dot(build_graph(load_tree(pair)))
    (lp,) = re.findall(r'lp="([^"]+)"', source)
    x, y = (float(part) for part in lp.split(","))
    assert x == pytest.approx(84.0, abs=1.0)
    assert 40.0 < y < 70.0


# --------------------------------------------------------------------------- #
# The arithmetic that makes a partial arrangement exact
# --------------------------------------------------------------------------- #


def test_a_similarity_is_recovered_from_two_pinned_nodes() -> None:
    transform = Transform(scale=1.5, dx=10.0, dy=-4.0)
    pairs = [((0.0, 0.0), transform.apply(0.0, 0.0)), ((100.0, 40.0), transform.apply(100.0, 40.0))]
    fitted = fit_transform(pairs)
    assert fitted.scale == pytest.approx(1.5)
    assert fitted.invert(*transform.apply(7.0, 9.0)) == pytest.approx((7.0, 9.0))


def test_one_pinned_node_gives_a_translation_and_no_scale() -> None:
    """A single point fixes the origin and says nothing about scale."""
    fitted = fit_transform([((0.0, 0.0), (10.0, 20.0))])
    assert (fitted.scale, fitted.dx, fitted.dy) == (1.0, 10.0, 20.0)


def test_no_pinned_nodes_gives_the_identity() -> None:
    assert fit_transform([]).is_identity


def test_pinned_nodes_drawn_on_top_of_each_other_give_a_translation() -> None:
    fitted = fit_transform([((0.0, 0.0), (5.0, 5.0)), ((10.0, 0.0), (5.0, 5.0))])
    assert fitted.scale == 1.0


def test_realign_puts_a_pinned_node_back_exactly() -> None:
    drawing = Drawing(
        nodes={
            "pinned": Placement(x=200.0, y=200.0, width=40.0, height=20.0),
            "free": Placement(x=400.0, y=200.0, width=40.0, height=20.0),
        }
    )
    stored = {"pinned": Placement(x=10.0, y=10.0)}
    placed = realign(drawing, stored, stored)
    assert (placed["pinned"].x, placed["pinned"].y) == (10.0, 10.0)
    # The free node keeps the size Graphviz gave it: a label is as wide as it is,
    # whatever the engine did to the positions.
    assert placed["free"].width == 40.0


def test_separation_moves_the_free_node_and_leaves_the_pinned_one() -> None:
    nodes = {
        "fixed": Placement(x=0.0, y=0.0, width=100.0, height=40.0),
        "free": Placement(x=10.0, y=0.0, width=100.0, height=40.0),
    }
    moved = separate(nodes, fixed={"fixed"})
    assert moved["fixed"] == nodes["fixed"], "a hand-placed node is not shoved aside"
    apart = abs(moved["free"].x - moved["fixed"].x) >= 100.0 or (
        abs(moved["free"].y - moved["fixed"].y) >= 40.0
    )
    assert apart, moved


def test_separation_leaves_two_pinned_nodes_alone() -> None:
    """An arrangement is a decision; two hand-placed nodes are the user's business."""
    nodes = {
        "a": Placement(x=0.0, y=0.0, width=100.0, height=40.0),
        "b": Placement(x=1.0, y=0.0, width=100.0, height=40.0),
    }
    assert separate(nodes, fixed={"a", "b"}) == nodes


def test_separation_is_deterministic() -> None:
    nodes = {f"n{index}": Placement(x=0.0, y=0.0, width=60.0, height=30.0) for index in range(6)}
    assert separate(nodes) == separate(dict(reversed(list(nodes.items()))))


# --------------------------------------------------------------------------- #
# Reading Graphviz back
# --------------------------------------------------------------------------- #


def test_a_drawing_is_read_out_of_the_json_graphviz_emits() -> None:
    payload = json.dumps(
        {
            "bb": "0,0,100,50",
            "objects": [
                {"name": "a", "pos": "10,20", "width": "0.5", "height": "0.25"},
                {"name": "cluster_0", "bb": "0,0,40,40"},
                {"name": "broken", "pos": "not,a,point"},
            ],
            "edges": [{"id": "edge:one", "pos": "e,1,2 3,4 5,6"}, {"pos": "7,8"}],
        }
    ).encode()
    drawing = parse_drawing(payload)
    assert drawing.nodes["a"] == Placement(x=10.0, y=20.0, width=36.0, height=18.0)
    assert drawing.clusters["cluster_0"].bounds == (0.0, 0.0, 40.0, 40.0)
    assert drawing.edges == {"edge:one": ((3.0, 4.0), (5.0, 6.0))}
    assert len(drawing.edges) == 1, "an edge with no id cannot be attributed to a link"
    assert "broken" not in drawing.nodes


def test_unreadable_graphviz_output_is_refused() -> None:
    with pytest.raises(ValueError, match="readable JSON"):
        parse_drawing(b"not json")


# --------------------------------------------------------------------------- #
# The document written
# --------------------------------------------------------------------------- #


def test_geometry_is_written_one_entry_per_line() -> None:
    sections = geometry_sections(
        Geometry(nodes={"b": Placement(2.0, 2.0), "a": Placement(1.0, 1.0)})
    )
    assert list(sections["nodes"]) == ["a", "b"], "sorted, so a re-seed is a stable diff"
    assert sections["nodes"]["a"] == {"position": {"x": 1, "y": 1}}


def test_a_whole_number_of_points_is_written_as_an_integer() -> None:
    entry = inline_entry({"position": {"x": 240.0, "y": 396.5}})
    assert entry == {"position": {"x": 240, "y": 396.5}}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"position": [1, 2]}, {"position": {"x": 1.0, "y": 2.0}}),
        ({"size": [3, 4]}, {"size": {"width": 3, "height": 4}}),
    ],
)
def test_two_spellings_of_one_position_compare_equal(first: Any, second: Any) -> None:
    assert canonical_geometry(first) == canonical_geometry(second)


def test_round_coordinate_never_produces_negative_zero() -> None:
    assert str(round_coordinate(-0.001)) == "0.0"


# --------------------------------------------------------------------------- #
# The edit operation
# --------------------------------------------------------------------------- #


def test_set_geometry_creates_a_layout_document(pair: Path) -> None:
    session = EditSession(root=pair)
    session.apply(
        SetGeometry(view="l1", nodes={"sw-a": inline_entry({"position": {"x": 1, "y": 2}})})
    )
    session.commit()
    inventory = load_tree(pair)
    assert inventory.layouts["layout"].spec.views["l1"].nodes["sw-a"].position.as_tuple() == (
        1.0,
        2.0,
    )
    assert (pair / "layout.yaml").is_file()


def test_set_geometry_keeps_the_comments_of_an_arranged_file(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        # the core switch belongs at the top\n"
            "        sw-a: {position: [1, 2]}\n"
            "        sw-b: {position: [3, 4]}\n"
        ),
    )
    session = EditSession(root=pair)
    session.apply(
        SetGeometry(
            view="l1",
            nodes={
                "sw-a": inline_entry({"position": {"x": 1, "y": 2}}),
                "sw-b": inline_entry({"position": {"x": 9, "y": 9}}),
            },
        )
    )
    session.commit()
    text = (pair / "arrange.yaml").read_text(encoding="utf-8")
    assert "# the core switch belongs at the top" in text
    assert "position: [1, 2]" in text, "an unmoved node keeps the spelling it was written in"
    assert "position: {x: 9, y: 9}" in text


def test_re_seeding_an_unchanged_arrangement_writes_nothing(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [1, 2]}\n"),
    )
    session = EditSession(root=pair)
    session.apply(
        SetGeometry(view="l1", nodes={"sw-a": inline_entry({"position": {"x": 1.0, "y": 2.0}})})
    )
    assert session.changes == {}


def test_set_geometry_removes_an_entry_that_is_no_longer_wanted(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        sw-a: {position: [1, 2]}\n"
            "        gone: {position: [3, 4]}\n"
        ),
    )
    session = EditSession(root=pair)
    session.apply(
        SetGeometry(view="l1", nodes={"sw-a": inline_entry({"position": {"x": 1, "y": 2}})})
    )
    session.commit()
    assert "gone" not in (pair / "arrange.yaml").read_text(encoding="utf-8")


def test_clearing_the_last_view_removes_the_document_and_its_file(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [1, 2]}\n"),
    )
    session = EditSession(root=pair)
    session.apply(SetGeometry(view="l1"))
    session.commit()
    assert not (pair / "arrange.yaml").exists()


def test_a_view_pruned_down_to_nothing_takes_its_document_with_it(pair: Path) -> None:
    """Every key was stale; what is left says nothing and should not be left behind."""
    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-gone: {position: [1, 2]}\n"),
    )
    session = EditSession(root=pair)
    session.apply(SetGeometry(view="l1", nodes={}, edges={}, groups={}))
    session.commit()
    assert not (pair / "arrange.yaml").exists()


def test_clearing_one_of_two_views_keeps_the_other(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n        sw-a: {position: [1, 2]}\n"
            "    l2:\n      nodes:\n        sw-a: {position: [3, 4]}\n"
        ),
    )
    session = EditSession(root=pair)
    session.apply(SetGeometry(view="l1"))
    session.commit()
    views = load_tree(pair).layouts["layout"].spec.views
    assert set(views) == {"l2"}


def test_the_inverse_of_a_geometry_write_restores_the_file(pair: Path) -> None:
    original = layout_document("    l1:\n      nodes:\n        sw-a: {position: [1, 2]}\n")
    write(pair, "arrange.yaml", original)
    session = EditSession(root=pair)
    applied = session.apply(
        SetGeometry(view="l1", nodes={"sw-a": inline_entry({"position": {"x": 8, "y": 8}})})
    )
    session.commit()

    undo = EditSession(root=pair)
    undo.apply_all(applied.inverse)
    undo.commit()
    assert (pair / "arrange.yaml").read_text(encoding="utf-8") == original


def test_an_unknown_view_is_refused_before_anything_is_written() -> None:
    with pytest.raises(OperationError, match="unknown view"):
        SetGeometry(view="layer8")


def test_geometry_that_is_not_geometry_is_refused(pair: Path) -> None:
    session = EditSession(root=pair)
    with pytest.raises(Exception, match="is not valid"):
        session.apply(SetGeometry(view="l1", nodes={"sw-a": {"position": "over there"}}))
    assert session.changes == {}


def test_an_empty_view_produces_no_operation() -> None:
    """``--layer rack`` on an inventory with no racks must not write ``rack: {}``."""
    assert write_operations(Geometry(view="rack")) == ()


def test_write_operations_leave_waypoints_out_unless_asked() -> None:
    geometry = Geometry(
        view="l1",
        nodes={"a": Placement(0, 0)},
        edges={"c": LinkGeometry(waypoints=((1.0, 2.0),))},
    )
    (without,) = write_operations(geometry)
    (with_them,) = write_operations(geometry, with_waypoints=True)
    assert without.edges is None
    assert with_them.edges == {"c": {"waypoints": [{"x": 1, "y": 2}]}}


def test_a_routing_style_is_written_whether_or_not_waypoints_were_asked_for() -> None:
    """A style is a decision, not a derived number: dropping it would undo it."""
    geometry = Geometry(
        view="l1",
        nodes={"a": Placement(0, 0)},
        edges={"c": LinkGeometry(routing=Routing.ORTHOGONAL)},
    )
    (operation,) = write_operations(geometry)
    assert operation.edges == {"c": {"routing": "orthogonal"}}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_stale_geometry_is_a_warning_that_names_the_key(pair: Path) -> None:
    from netviz.validate import validate

    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-gone: {position: [1, 2]}\n"),
    )
    findings = [finding for finding in validate(load_tree(pair)) if finding.rule == "W138"]
    (finding,) = findings
    assert "sw-gone" in finding.message
    assert finding.field_path == ("spec", "views", "l1", "nodes", "sw-gone")


def test_a_derived_node_id_is_never_called_stale(pair: Path) -> None:
    """Only a drawing can judge one, and the validator does not build drawings."""
    from netviz.validate import validate

    write(
        pair,
        "arrange.yaml",
        layout_document("    l3:\n      nodes:\n        subnet:192.0.2.0/24: {position: [1, 2]}\n"),
    )
    assert [f for f in validate(load_tree(pair)) if f.rule == "W138"] == []


def test_a_synthetic_edge_id_is_never_called_stale(pair: Path) -> None:
    """An adapter attachment is a fact about a drawing; no element declares it."""
    from netviz.validate import validate

    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      edges:\n"
            "        adp-usb#upstream: {waypoints: [[1, 2]]}\n"
            "        sw-a:port1#10.0.0.0/24: {waypoints: [[3, 4]]}\n"
        ),
    )
    assert [f for f in validate(load_tree(pair)) if f.rule == "W138"] == []


def test_an_annotation_on_the_layout_suppresses_the_warning(pair: Path) -> None:
    from netviz.validate import validate

    write(
        pair,
        "arrange.yaml",
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: layout\n"
        "metadata:\n"
        "  name: layout\n"
        "  annotations:\n"
        '    netviz/ignore: "W138"\n'
        "spec:\n"
        "  views:\n"
        "    l1:\n      nodes:\n        sw-gone: {position: [1, 2]}\n",
    )
    assert [f for f in validate(load_tree(pair)) if f.rule == "W138"] == []


# --------------------------------------------------------------------------- #
# The JSON export
# --------------------------------------------------------------------------- #


def test_the_json_export_publishes_the_coordinates(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document(
            "    l1:\n      nodes:\n"
            "        sw-a: {position: [54, 18], size: [100, 40]}\n"
            "        sw-b: {position: [54, 126]}\n"
            "      edges:\n"
            "        cbl-a-b: {waypoints: [[54, 72]]}\n"
        ),
    )
    document = json.loads(to_json(build_graph(load_tree(pair))))
    assert document["layout"] == {"units": "points", "mode": "fixed", "routing": "spline"}
    placed = {node["id"]: node.get("layout") for node in document["nodes"]}
    assert placed["sw-a"] == {
        "position": {"x": 54.0, "y": 18.0},
        "size": {"width": 100.0, "height": 40.0},
    }
    assert placed["sw-b"] == {"position": {"x": 54.0, "y": 126.0}}
    (edge,) = document["edges"]
    assert edge["layout"]["waypoints"] == [{"x": 54.0, "y": 72.0}]
    # The route netviz draws is published beside what the inventory pinned, so
    # a client can reproduce the picture without reimplementing the routing.
    assert {"x": 54.0, "y": 72.0} in edge["layout"]["route"]
    assert edge["layout"]["drawnAs"] == "spline"
    assert len(edge["layout"]["controls"]) % 3 == 1


def test_an_unarranged_export_carries_no_layout_key(pair: Path) -> None:
    document = json.loads(to_json(build_graph(load_tree(pair))))
    assert "layout" not in document
    assert all("layout" not in node for node in document["nodes"])


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def _anchor(x: float, y: float, width: float = 60.0, height: float = 40.0) -> Anchor:
    return Anchor(x=x, y=y, width=width, height=height)


def _within(anchor: Anchor, point: tuple[float, float]) -> bool:
    """Is this point *strictly* inside the box, rather than on its border?

    A clipped route ends exactly on the border, which
    :meth:`~netviz.layout.routing.Anchor.contains` counts as inside — rightly,
    since that is the test for which points to drop. What a route must not do is
    go *through* the shape, which is this.
    """
    return (
        abs(point[0] - anchor.x) < anchor.width / 2 - 0.01
        and abs(point[1] - anchor.y) < anchor.height / 2 - 0.01
    )


def test_a_route_leaves_both_shapes_rather_than_crossing_them() -> None:
    """Edges are painted after nodes, so an unclipped route is plainly visible."""
    source, target = _anchor(0, 0), _anchor(0, 300)
    line = route(source, target)
    assert not any(_within(source, point) or _within(target, point) for point in line.corners)
    # And it starts *on* the border rather than somewhere out in the open. This
    # is the case the crossing test's slack exists for: leaving a box squarely
    # reaches the border exactly, and exactly is where floating point misses.
    assert line.corners[0][1] == pytest.approx(source.height / 2 + 1.0)


def test_an_orthogonal_route_between_two_nodes_turns_once_each_way() -> None:
    """A Z: half way along the dominant axis, across, and on."""
    line = route(_anchor(0, 0), _anchor(400, 200), style=Routing.ORTHOGONAL)
    for before, after in zip(line.corners, line.corners[1:], strict=False):
        assert before[0] == pytest.approx(after[0]) or before[1] == pytest.approx(after[1]), (
            "an orthogonal leg runs along one axis or the other, never both"
        )


def test_a_route_passes_through_every_bend_it_is_given() -> None:
    bends = ((120.0, 260.0), (300.0, 40.0))
    for style in Routing:
        line = route(_anchor(0, 0), _anchor(400, 300), waypoints=bends, style=style)
        for bend in bends:
            assert any(point == pytest.approx(bend, abs=0.01) for point in line.corners), (
                f"{style} dropped {bend}"
            )


def test_control_points_are_the_form_a_graphviz_pos_is() -> None:
    """``3n + 1``, or Graphviz draws something else entirely and never says so."""
    for style in Routing:
        for waypoints in ((), ((150.0, 150.0),), ((100.0, 40.0), (300.0, 260.0))):
            line = route(_anchor(0, 0), _anchor(400, 300), waypoints=waypoints, style=style)
            assert (len(line.controls) - 1) % 3 == 0 and len(line.controls) >= 4


def test_a_label_is_placed_along_the_route_it_is_pinned_to() -> None:
    line = route(_anchor(0, 0), _anchor(0, 400), style=Routing.STRAIGHT)
    assert label_position(line, LabelPlacement(at=0.5)) is None, "the default pins nothing"
    placed = label_position(line, LabelPlacement(at=0.25, dx=8.0, dy=0.0))
    assert placed is not None
    start, end = line.corners[0][1], line.corners[-1][1]
    assert placed[0] == pytest.approx(8.0)
    assert placed[1] == pytest.approx(start + (end - start) * 0.25, abs=0.5)


def test_parallel_links_are_fanned_apart_and_a_lone_one_is_not_moved() -> None:
    assert fan_offsets(1) == (0.0,)
    assert fan_offsets(2) == (-FAN_GAP / 2, FAN_GAP / 2)
    # An odd bundle keeps one cable on the direct line, which is the one a reader
    # traces first.
    assert 0.0 in fan_offsets(3)
    assert fan_offsets(4)[0] == -fan_offsets(4)[-1]


def test_a_fan_bows_a_link_off_the_line_without_changing_which_shapes_it_joins() -> None:
    source, target = _anchor(0, 0), _anchor(0, 400)
    bowed = route(source, target, fan=FAN_GAP)
    # The bow is the full gap at the middle and nothing at all at the ends,
    # which is what keeps every cable in a bundle the same length to the eye.
    assert max(abs(x) for x, _ in bowed.corners) == pytest.approx(FAN_GAP)
    # Both ends still stop at their own shape, aimed at the bow rather than
    # straight up: a fanned cable is a different line, not a shifted one.
    assert not any(_within(source, point) or _within(target, point) for point in bowed.corners)
    assert bowed.corners[0][0] != 0.0, "the line leaves the box aimed at the bow"


def test_a_fanned_link_with_bends_of_its_own_is_left_where_it_was_put() -> None:
    """A bend says where the cable goes; nudging it off would make a drag lie."""
    bends = ((90.0, 200.0),)
    assert (
        route(_anchor(0, 0), _anchor(0, 400), waypoints=bends).corners
        == route(_anchor(0, 0), _anchor(0, 400), waypoints=bends, fan=FAN_GAP).corners
    )


def test_a_self_link_is_a_ring_that_stands_further_off_for_each_fan() -> None:
    node = _anchor(0, 0)
    first = route(node, node)
    second = route(node, node, fan=FAN_GAP)
    assert first.corners[0] != first.corners[-1], "a loop leaves and returns at two points"
    # Further out *and* wider, so a stack of them nests visibly rather than
    # drawing one ring on top of another.
    assert max(y for _, y in second.corners) > max(y for _, y in first.corners)
    assert max(x for x, _ in second.corners) > max(x for x, _ in first.corners)


def test_self_links_on_one_node_are_spread_outwards_rather_than_centred(pair: Path) -> None:
    """Centred offsets put the first two loops the same distance out, both ways."""
    write(
        pair,
        "loops.yaml",
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: lp1}\n"
        "spec: {endpoints: [sw-a:port1, sw-a:port2], medium: copper}\n"
        "---\n"
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: lp2}\n"
        "spec: {endpoints: [sw-a:port3, sw-a:port4], medium: copper}\n",
    )
    graph = build_graph(load_tree(pair))
    loops = [index for index, edge in enumerate(graph.edges) if edge.source == edge.target]
    assert len(loops) == 2
    fans = fans_of(graph)
    assert sorted(abs(fans[index]) for index in loops) == [0.0, FAN_GAP]


# --------------------------------------------------------------------------- #
# Round-tripping through Graphviz
# --------------------------------------------------------------------------- #


@requires_dot
@pytest.mark.parametrize("example", ["home-lab", "campus"])
def test_a_stored_arrangement_round_trips_through_render_unchanged(
    tmp_path: Path, example: str
) -> None:
    """The property the whole feature rests on.

    Seed the arrangement, render it, and read back where Graphviz actually put
    each node: the numbers must be the ones that were stored. Not approximately —
    a diagram that drifts a point per render is a diagram nobody can arrange.
    """
    root = tmp_path / example
    copy_tree(EXAMPLES / example, root)
    result = run(root, "layout", "--write")
    assert result.exit_code == 0, result.output

    graph = build_graph(load_tree(root))
    assert layout_plan(graph).mode is LayoutMode.FIXED
    drawn = drawn_positions(graph)
    for fqn, stored in graph.geometry.nodes.items():
        assert drawn[fqn] == pytest.approx((stored.x, stored.y), abs=0.005), fqn


@requires_dot
def test_seeding_twice_over_writes_nothing_the_second_time(tmp_path: Path) -> None:
    """A fixed point, not merely a stable-looking one."""
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write").exit_code == 0
    before = (root / "layout.yaml").read_text(encoding="utf-8")
    assert run(root, "layout", "--write").exit_code == 0
    assert (root / "layout.yaml").read_text(encoding="utf-8") == before


@requires_dot
def test_a_partially_positioned_graph_lays_out_without_overlap(tmp_path: Path) -> None:
    """Placing the rest around what is pinned must not stack anything.

    Undoing the engine's scale is what makes this a real risk: ``neato``
    separates by scaling the whole drawing up, and putting it back on the stored
    coordinate system undoes that.
    """
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write").exit_code == 0

    # Drop half the positions, keeping the file a valid arrangement of the rest.
    inventory = load_tree(root)
    kept = dict(sorted(build_graph(inventory).geometry.nodes.items())[:4])
    body = "".join(
        f"        {fqn}: {{position: [{place.x}, {place.y}]}}\n" for fqn, place in kept.items()
    )
    (root / "layout.yaml").write_text(
        layout_document(f"    l1:\n      nodes:\n{body}"), encoding="utf-8"
    )

    graph = build_graph(load_tree(root))
    assert layout_plan(graph).mode is LayoutMode.PARTIAL
    completed = complete_layout(graph, RenderOptions())

    # Every pinned node is exactly where it was pinned ...
    for fqn, stored in graph.geometry.nodes.items():
        placed = completed.geometry.nodes[fqn]
        assert (placed.x, placed.y) == (stored.x, stored.y), fqn
    # ... every node is placed ...
    assert set(completed.geometry.nodes) == set(graph.nodes)
    # ... and nothing sits on top of anything else in the drawing that results.
    assert_no_overlap(completed)


@requires_dot
def test_seeded_waypoints_belong_to_the_links_they_were_read_from(tmp_path: Path) -> None:
    """Edges are matched to the drawing by position; this is what checks it.

    A seeded spline must start and end near the nodes its link joins. Matched to
    the wrong edge it would still be a valid spline — and would draw a line
    across the diagram — so "it parses" is not the assertion worth making.
    """
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write", "--waypoints").exit_code == 0

    graph = build_graph(load_tree(root))
    placed = graph.geometry.nodes
    assert graph.geometry.edges, "the seed stored the routes"
    for edge in graph.edges:
        link = graph.geometry.link(edge.id)
        if not link.waypoints:
            continue
        ends = (placed[edge.source], placed[edge.target])
        for point, end in zip((link.waypoints[0], link.waypoints[-1]), ends, strict=True):
            assert abs(point[0] - end.x) < 300 and abs(point[1] - end.y) < 300, edge.id
        # And the size of every node a stored route leaves from is recorded with
        # it, because the route has to stop at the shape and netviz cannot
        # measure a label.
        assert all(end.width is not None for end in ends), edge.id


@requires_dot
def test_the_svg_and_the_json_agree_on_where_everything_is(tmp_path: Path) -> None:
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write").exit_code == 0

    graph = build_graph(load_tree(root))
    exported = {
        node["id"]: (node["layout"]["position"]["x"], node["layout"]["position"]["y"])
        for node in json.loads(to_json(graph))["nodes"]
    }
    drawn = drawn_positions(graph)
    for fqn, position in exported.items():
        assert drawn[fqn] == pytest.approx(position, abs=0.005), fqn


@requires_dot
def test_group_boxes_survive_a_fixed_layout(tmp_path: Path) -> None:
    """``neato`` draws no clusters, so the frames have to come from the store."""
    root = tmp_path / "campus"
    copy_tree(EXAMPLES / "campus", root)
    assert run(root, "layout", "--write", "--group-by-namespace").exit_code == 0
    graph = build_graph(load_tree(root))
    assert graph.geometry.groups, "the seed stored the cluster boxes"
    source = to_dot(graph, RenderOptions(group_by_namespace=True))
    assert "_background=" in source
    payload, _ = run_graphviz(source, format="svg", plan=layout_plan(graph))
    assert b"#9ca3af" in payload, "the frames are drawn"


@requires_dot
def test_an_arrangement_is_seeded_with_the_engine_that_was_asked_for(tmp_path: Path) -> None:
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write", "--engine", "circo").exit_code == 0
    circo = build_graph(load_tree(root)).geometry.nodes

    assert run(root, "layout", "--clear").exit_code == 0
    assert run(root, "layout", "--write", "--engine", "dot").exit_code == 0
    assert build_graph(load_tree(root)).geometry.nodes != circo


@requires_dot
def test_an_unknown_engine_is_refused() -> None:
    from netviz.errors import RenderError

    graph = build_graph(load_tree(EXAMPLES / "home-lab"))
    with pytest.raises(RenderError, match="is not a layout engine"):
        seed_geometry(graph, RenderOptions(), engine="ouija")


def assert_no_overlap(graph: Any) -> None:
    """No two nodes of the drawn diagram share any area."""
    payload, _ = run_graphviz(
        to_dot(graph, RenderOptions(), target="svg"), format="json", plan=layout_plan(graph)
    )
    boxes = parse_drawing(payload).nodes
    names = sorted(boxes)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            a, b = boxes[first], boxes[second]
            apart = (
                abs(a.x - b.x) >= (a.width + b.width) / 2
                or abs(a.y - b.y) >= (a.height + b.height) / 2
            )
            assert apart, f"{first} overlaps {second}"


def copy_tree(source: Path, target: Path) -> None:
    import shutil

    shutil.copytree(source, target)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_report_says_nothing_is_arranged(pair: Path) -> None:
    result = run(pair, "layout")
    assert result.exit_code == 0
    assert "no layout document" in result.output
    assert "auto" in result.output


def test_the_report_counts_what_is_placed(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [1, 2]}\n"),
    )
    result = run(pair, "layout")
    assert "partial" in result.output
    assert "1/2" in result.output


def test_the_report_is_machine_readable(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-a: {position: [1, 2]}\n"),
    )
    document = json.loads(run(pair, "layout", "--json").output)
    (view,) = document["views"]
    assert view["view"] == "l1"
    assert view["mode"] == "partial"
    assert view["nodes"] == {"total": 2, "placed": 1}


def test_the_report_warns_about_stale_entries(pair: Path) -> None:
    write(
        pair,
        "arrange.yaml",
        layout_document("    l1:\n      nodes:\n        sw-gone: {position: [1, 2]}\n"),
    )
    assert "stale" in run(pair, "layout").output


@requires_dot
def test_prune_drops_what_the_diagram_has_not_got(tmp_path: Path) -> None:
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write").exit_code == 0
    text = (root / "layout.yaml").read_text(encoding="utf-8")
    (root / "layout.yaml").write_text(
        text + "        ghosts/nobody: {position: [1, 2]}\n", encoding="utf-8"
    )

    assert run(root, "layout", "--prune").exit_code == 0
    assert "ghosts/nobody" not in (root / "layout.yaml").read_text(encoding="utf-8")
    assert build_graph(load_tree(root)).geometry.nodes, "the live entries are untouched"


@requires_dot
def test_prune_on_a_clean_arrangement_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write").exit_code == 0
    before = (root / "layout.yaml").read_text(encoding="utf-8")
    result = run(root, "layout", "--prune")
    assert "nothing to change" in result.output
    assert (root / "layout.yaml").read_text(encoding="utf-8") == before


def test_clear_on_an_unarranged_inventory_writes_nothing(pair: Path) -> None:
    assert "nothing to change" in run(pair, "layout", "--clear").output


def test_write_and_clear_together_is_a_usage_error(pair: Path) -> None:
    result = run(pair, "layout", "--write", "--clear")
    assert result.exit_code == 2
    assert "opposite things" in result.output


def test_replace_without_write_is_a_usage_error(pair: Path) -> None:
    result = run(pair, "layout", "--replace")
    assert result.exit_code == 2
    assert "does nothing on its own" in result.output


def test_clear_and_prune_together_is_a_usage_error(pair: Path) -> None:
    assert run(pair, "layout", "--clear", "--prune").exit_code == 2


def test_an_inventory_that_does_not_load_is_refused(tmp_path: Path) -> None:
    write(tmp_path, "broken.yaml", "apiVersion: netviz.dev/v1alpha1\nkind: nonsense\n")
    result = run(tmp_path, "layout")
    assert result.exit_code == 1
    assert "refusing to arrange" in result.output


@requires_dot
def test_a_dry_run_writes_nothing_and_prints_the_diff(pair: Path) -> None:
    result = run(pair, "layout", "--write", "--dry-run")
    assert result.exit_code == 0
    assert "+kind: layout" in result.output
    assert not (pair / "layout.yaml").exists()


@requires_dot
def test_writing_again_completes_rather_than_replaces(tmp_path: Path) -> None:
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write").exit_code == 0
    arranged = dict(build_graph(load_tree(root)).geometry.nodes)

    # Take one node's entry away; re-seeding must place it and move nothing.
    victim = sorted(arranged)[0]
    lines = (root / "layout.yaml").read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.strip().startswith(victim))
    (root / "layout.yaml").write_text("".join(lines[:start] + lines[start + 2 :]), encoding="utf-8")

    assert run(root, "layout", "--write").exit_code == 0
    after = build_graph(load_tree(root)).geometry.nodes
    assert set(after) == set(arranged)
    for fqn, place in arranged.items():
        if fqn != victim:
            assert after[fqn] == place, fqn


@requires_dot
def test_replace_lays_everything_out_afresh(tmp_path: Path) -> None:
    root = tmp_path / "home-lab"
    copy_tree(EXAMPLES / "home-lab", root)
    assert run(root, "layout", "--write", "--engine", "circo").exit_code == 0
    circo = dict(build_graph(load_tree(root)).geometry.nodes)
    assert run(root, "layout", "--write", "--replace", "--engine", "dot").exit_code == 0
    assert build_graph(load_tree(root)).geometry.nodes != circo


@requires_dot
def test_the_layout_document_can_be_named_and_placed(pair: Path) -> None:
    result = run(pair, "layout", "--write", "--name", "wall-chart", "--file", "wall.yaml")
    assert result.exit_code == 0, result.output
    assert "wall-chart" in load_tree(pair).layouts
    assert (pair / "wall.yaml").is_file()


def test_views_for_returns_the_canonical_order() -> None:
    assert views_for(["l2", "l1", "l1"]) == ("l1", "l2")


def test_live_keys_reports_what_a_drawing_contains(pair: Path) -> None:
    graph = build_graph(load_tree(pair))
    keys = live_keys(graph, RenderOptions())
    assert keys.nodes == {"sw-a", "sw-b"}
    assert keys.edges == {"cbl-a-b"}


def test_every_engine_offered_is_one_graphviz_has() -> None:
    assert LAYOUT_ENGINES[0] == "dot", "the default is the layout netviz draws with"
    assert set(LAYOUT_ENGINES) <= {"dot", "neato", "fdp", "sfdp", "circo", "twopi", "patchwork"}
