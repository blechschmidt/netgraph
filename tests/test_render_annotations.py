"""Drawing the notes, areas and legends of §21.

The promise these tests exist to hold is the one :mod:`netviz.models.annotation`
makes: an annotation changes the *picture* and nothing else. So the first
assertion here is a counting one — a graph with three annotation documents has
exactly the nodes and edges of the same graph without them — and everything
after it is about the two ways a renderer could break that promise quietly:
by drawing a box around elements a filter removed, or by drawing nothing at all
and saying so nowhere.

The rest is the vocabulary each backend degrades into, and one regression that
has already cost a segfault once: text in a Graphviz ``_background``
(``docs/follow-ups.md`` §17).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from netviz.loader import Inventory, load_tree
from netviz.render import FilterSpec, Layer, RenderOptions, build_graph, filter_graph
from netviz.render.annotations import (
    AREA_ID_PREFIX,
    LEGEND_ID_PREFIX,
    NOTE_ID_PREFIX,
    annotation_views,
    darken,
    parse_markup,
)
from netviz.render.dot import layout_plan, to_dot, to_image
from netviz.render.jsonexport import to_json
from netviz.render.mermaid import to_mermaid
from netviz.render.palette import NODE_PALETTE

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

TOPOLOGY = """\
apiVersion: netviz.dev/v1alpha1
kind: switch
metadata: {name: sw-core}
spec:
  interfaces:
    - {name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}
    - {name: eth1, type: ethernet}
---
apiVersion: netviz.dev/v1alpha1
kind: server
metadata: {name: srv-proxy}
spec:
  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.2/24], vlan: {mode: access, access_vlan: 20}}]
---
apiVersion: netviz.dev/v1alpha1
kind: cable
metadata: {name: cbl-fibre}
spec: {endpoints: [sw-core:eth0, srv-proxy:eth0], medium: fiber}
"""

OFFICE = """\
apiVersion: netviz.dev/v1alpha1
kind: computer
metadata: {name: pc}
spec:
  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.3/24], vlan: {mode: access, access_vlan: 10}}]
---
apiVersion: netviz.dev/v1alpha1
kind: cable
metadata: {name: cbl-desk}
spec: {endpoints: [pc:eth0, edge/sw-core:eth1], medium: copper}
"""

ANNOTATIONS = """\
apiVersion: netviz.dev/v1alpha1
kind: area
metadata: {name: dmz}
spec:
  label: DMZ
  members: [edge/sw-core, edge/srv-proxy]
  color: "#fee2e2"
---
apiVersion: netviz.dev/v1alpha1
kind: note
metadata: {name: why-orange}
spec:
  text: |
    **Orange** links are fibre. The run to the annexe is 180 m,
    which is past what copper does.

    - one *thing*
    - `code` two
  anchor: {element: edge/sw-core}
  color: "#fef3c7"
---
apiVersion: netviz.dev/v1alpha1
kind: legend
metadata: {name: key}
spec:
  title: Key
  corner: bottom-right
  auto: layers
"""

#: A stored arrangement for every node of the fixture, which is what makes
#: :attr:`~netviz.layout.geometry.LayoutMode.FIXED` — and therefore the
#: ``_background`` path — reachable.
LAYOUT = """\
apiVersion: netviz.dev/v1alpha1
kind: layout
metadata: {name: layout}
spec:
  views:
    l1:
      nodes:
        edge/sw-core:
          position: {x: 100, y: 300}
          size: {width: 80, height: 40}
        edge/srv-proxy:
          position: {x: 280, y: 300}
          size: {width: 80, height: 40}
        office/pc:
          position: {x: 190, y: 100}
          size: {width: 80, height: 40}
"""


def write_inventory(root: Path, *, annotations: str = "", layout: str = "") -> Inventory:
    """The two-namespace fixture, optionally annotated and optionally arranged."""
    (root / "edge").mkdir(parents=True, exist_ok=True)
    (root / "office").mkdir(parents=True, exist_ok=True)
    (root / "edge" / "net.yaml").write_text(TOPOLOGY, encoding="utf-8")
    (root / "office" / "net.yaml").write_text(OFFICE, encoding="utf-8")
    if annotations:
        (root / "annotations.yaml").write_text(annotations, encoding="utf-8")
    if layout:
        (root / "layout.yaml").write_text(layout, encoding="utf-8")
    inventory = load_tree(root)
    assert inventory.errors == [], [str(error) for error in inventory.errors]
    return inventory


@pytest.fixture
def annotated(tmp_path: Path) -> Inventory:
    return write_inventory(tmp_path / "annotated", annotations=ANNOTATIONS)


@pytest.fixture
def plain(tmp_path: Path) -> Inventory:
    return write_inventory(tmp_path / "plain")


# --------------------------------------------------------------------------- #
# An annotation is never topology
# --------------------------------------------------------------------------- #


def test_annotations_add_no_node_and_no_edge(annotated: Inventory, plain: Inventory) -> None:
    """The central promise of §21, asserted where a renderer could break it."""
    with_them = build_graph(annotated)
    without = build_graph(plain)
    assert len(with_them.nodes) == len(without.nodes)
    assert len(with_them.edges) == len(without.edges)
    assert list(with_them.nodes) == list(without.nodes)
    assert with_them.annotations.count == 3
    assert without.annotations.count == 0


def test_the_graph_carries_its_annotations_for_this_view(annotated: Inventory) -> None:
    graph = build_graph(annotated)
    assert [fqn for fqn, _ in graph.annotations.areas] == ["dmz"]
    assert [fqn for fqn, _ in graph.annotations.notes] == ["why-orange"]
    assert [fqn for fqn, _ in graph.annotations.legends] == ["key"]
    # Resolved once, where the inventory is still in scope.
    assert graph.annotation_targets["dmz"] == ("edge/sw-core", "edge/srv-proxy")
    assert graph.annotation_targets["why-orange"] == ("edge/sw-core",)


def test_an_annotation_scoped_to_another_view_does_not_appear(tmp_path: Path) -> None:
    inventory = write_inventory(
        tmp_path / "scoped",
        annotations=(
            "apiVersion: netviz.dev/v1alpha1\n"
            "kind: note\n"
            "metadata: {name: l3-only}\n"
            "spec:\n"
            "  text: only at layer 3\n"
            "  views: [l3]\n"
            "  anchor: {element: edge/sw-core}\n"
        ),
    )
    assert build_graph(inventory, layer=Layer.L1).annotations.count == 0
    assert build_graph(inventory, layer=Layer.L3).annotations.count == 1


def test_filtering_carries_the_annotations_and_narrows_what_they_enclose(
    annotated: Inventory,
) -> None:
    """A filter narrows what an area *encloses*, not which annotations exist."""
    graph = filter_graph(build_graph(annotated), FilterSpec(vlans=frozenset({10})))
    assert "edge/srv-proxy" not in graph.nodes, "the fixture must actually lose a member"
    assert graph.annotations.count == 3
    assert graph.annotation_targets["dmz"] == ("edge/sw-core", "edge/srv-proxy")

    views = annotation_views(graph)
    # …and the member that is no longer drawn is dropped, silently.
    assert views.areas[0].members == ("edge/sw-core",)


def test_an_area_with_nothing_left_to_enclose_is_dropped(annotated: Inventory) -> None:
    graph = filter_graph(build_graph(annotated), FilterSpec(namespaces=("office",)))
    assert graph.annotations.areas, "the area is still declared"
    assert annotation_views(graph).areas == (), "but there is nothing left to draw it round"


def test_a_note_keeps_its_text_and_loses_its_leader_when_the_anchor_goes(
    annotated: Inventory,
) -> None:
    graph = filter_graph(build_graph(annotated), FilterSpec(namespaces=("office",)))
    note = annotation_views(graph).notes[0]
    assert note.anchor == ""
    assert note.leader is False
    assert "Orange" in note.text


# --------------------------------------------------------------------------- #
# The markdown subset
# --------------------------------------------------------------------------- #


def test_the_parser_reads_paragraphs_bullets_and_inline_emphasis() -> None:
    blocks = parse_markup("**bold** and *italic*\nwrapped\n\n- a `code` item\n- another")
    assert [block.kind for block in blocks] == ["paragraph", "bullet", "bullet"]
    # A soft line break joins, the way markdown joins one.
    assert blocks[0].text == "bold and italic wrapped"
    assert [(span.style, span.text) for span in blocks[0].spans] == [
        ("bold", "bold"),
        ("", " and "),
        ("italic", "italic"),
        ("", " wrapped"),
    ]
    assert [span.style for span in blocks[1].spans] == ["", "code", ""]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "*",
        "**",
        "***",
        "`",
        "**unterminated",
        "a * b * c",
        "- ",
        "-",
        "\n\n\n",
        "*" * 500,
        "`" * 500,
        "**a**" * 200,
        "\t- tabbed\n\t\t- deeper",
        "‮right to left‬",
    ],
)
def test_the_parser_is_total(text: str) -> None:
    """It never raises and never loops: a stale note must not stop a render."""
    blocks = parse_markup(text)
    assert isinstance(blocks, tuple)
    # Whatever it decided, no character of the source was invented or lost from
    # inside a block: the spans of a block reconstruct its text.
    for block in blocks:
        assert block.text == "".join(span.text for span in block.spans)


@pytest.mark.parametrize(
    "text",
    ["a lone * star", "**unterminated", "an `unclosed code span", "10 * 4 = 40"],
)
def test_a_marker_with_no_partner_is_literal_text(text: str) -> None:
    """The honest failure mode for a formatting tool several renderers share."""
    (block,) = parse_markup(text)
    assert [(span.style, span.text) for span in block.spans] == [("", text)]


def test_every_renderer_sees_the_same_parse(annotated: Inventory) -> None:
    graph = build_graph(annotated)
    note = annotation_views(graph).notes[0]
    exported = json.loads(to_json(graph))["annotations"]["notes"][0]
    assert [block["kind"] for block in exported["blocks"]] == [block.kind for block in note.lines]
    assert exported["text"] == note.text


# --------------------------------------------------------------------------- #
# Identity and colour
# --------------------------------------------------------------------------- #


def test_ids_are_prefixed_stable_and_derived_from_the_name(annotated: Inventory) -> None:
    views = annotation_views(build_graph(annotated))
    assert views.areas[0].id == f"{AREA_ID_PREFIX}dmz"
    assert views.notes[0].id == f"{NOTE_ID_PREFIX}why-orange"
    assert views.legends[0].id == f"{LEGEND_ID_PREFIX}key"
    # Two runs over one inventory agree, which is what a bookmark depends on.
    again = annotation_views(build_graph(annotated))
    assert [area.id for area in again.areas] == [area.id for area in views.areas]


def test_a_stroke_is_derived_from_the_fill() -> None:
    assert darken("#ffffff") == "#8c8c8c"
    assert darken("#fff") == darken("#ffffff")
    assert darken("#000000") == "#000000"
    # Total: anything that is not a colour comes back as it went in.
    assert darken("chartreuse") == "chartreuse"


def test_an_annotation_without_a_colour_takes_its_kind_s_default(tmp_path: Path) -> None:
    inventory = write_inventory(
        tmp_path / "uncoloured",
        annotations=(
            "apiVersion: netviz.dev/v1alpha1\n"
            "kind: area\n"
            "metadata: {name: plain}\n"
            "spec: {members: [edge/sw-core]}\n"
        ),
    )
    area = annotation_views(build_graph(inventory)).areas[0]
    assert area.fill.startswith("#")
    assert area.stroke == darken(area.fill)
    assert area.label == ""


# --------------------------------------------------------------------------- #
# auto: layers
# --------------------------------------------------------------------------- #


def test_a_generated_legend_describes_what_the_drawing_actually_drew(
    annotated: Inventory,
) -> None:
    graph = build_graph(annotated)
    legend = annotation_views(graph).legends[0]
    labels = [entry.label for entry in legend.entries]
    assert labels == ["switch", "server", "computer", "fiber", "copper"]
    # The colours come from the table the renderer draws with, not from a copy.
    switch = next(entry for entry in legend.entries if entry.label == "switch")
    assert switch.color == NODE_PALETTE["switch"][0]
    assert switch.shape == "box"
    assert next(entry for entry in legend.entries if entry.label == "fiber").shape == "line"


def test_a_generated_legend_shrinks_with_the_diagram(annotated: Inventory) -> None:
    """The only kind of key that cannot go stale is one derived from the picture."""
    graph = filter_graph(build_graph(annotated), FilterSpec(vlans=frozenset({10})))
    labels = [entry.label for entry in annotation_views(graph).legends[0].entries]
    assert "server" not in labels
    assert "fiber" not in labels
    assert "computer" in labels


def test_a_written_out_legend_is_left_alone(tmp_path: Path) -> None:
    inventory = write_inventory(
        tmp_path / "written",
        annotations=(
            "apiVersion: netviz.dev/v1alpha1\n"
            "kind: legend\n"
            "metadata: {name: key}\n"
            "spec:\n"
            "  entries:\n"
            "    - {label: production, color: '#ff0000'}\n"
            "    - {label: lab, color: '#00ff00', shape: dashed, description: not monitored}\n"
        ),
    )
    entries = annotation_views(build_graph(inventory)).legends[0].entries
    assert [entry.label for entry in entries] == ["production", "lab"]
    assert entries[0].color == "#ff0000"
    assert entries[1].shape == "dashed"
    assert entries[1].description == "not monitored"


# --------------------------------------------------------------------------- #
# DOT
# --------------------------------------------------------------------------- #


def test_an_area_becomes_a_cluster_under_an_automatic_layout(annotated: Inventory) -> None:
    source = to_dot(build_graph(annotated))
    assert "subgraph cluster_area_0 {" in source
    assert 'label="DMZ";' in source
    assert 'fillcolor="#fee2e2";' in source
    assert "dashed" in source.split("subgraph cluster_area_0 {")[1].split("}")[0]


def test_an_area_outranks_group_by_namespace(annotated: Inventory) -> None:
    """A node is in one box, and the explicit annotation is the one that wins."""
    source = to_dot(build_graph(annotated), RenderOptions(group_by_namespace=True))
    area_block = source.split("subgraph cluster_area_0 {")[1].split("\n  }")[0]
    assert '"edge/sw-core"' in area_block
    assert '"edge/srv-proxy"' in area_block
    # The namespace that lost both its nodes is not drawn as an empty box, and
    # the one that kept its node still is.
    assert 'label="edge";' not in source
    assert 'label="office";' in source
    assert _declarations(source, "edge/sw-core") == 1, "a node is emitted exactly once"


def test_the_first_declared_area_wins_an_overlapping_node(tmp_path: Path) -> None:
    inventory = write_inventory(
        tmp_path / "overlapping",
        annotations=(
            "apiVersion: netviz.dev/v1alpha1\n"
            "kind: area\n"
            "metadata: {name: first}\n"
            "spec: {label: first, members: [edge/sw-core, edge/srv-proxy]}\n"
            "---\n"
            "apiVersion: netviz.dev/v1alpha1\n"
            "kind: area\n"
            "metadata: {name: second}\n"
            "spec: {label: second, members: [edge/sw-core, office/pc]}\n"
        ),
    )
    source = to_dot(build_graph(inventory))
    first = source.split("subgraph cluster_area_0 {")[1].split("\n  }")[0]
    second = source.split("subgraph cluster_area_1 {")[1].split("\n  }")[0]
    assert '"edge/sw-core"' in first
    assert '"edge/sw-core"' not in second
    assert '"office/pc"' in second
    assert _declarations(source, "edge/sw-core") == 1


def _declarations(source: str, fqn: str) -> int:
    """How many times ``fqn`` is *declared* as a node, ignoring the edges it ends."""
    return sum(1 for line in source.splitlines() if line.strip().startswith(f'"{fqn}" ['))


def test_a_note_is_a_node_and_its_leader_cannot_move_the_topology(
    annotated: Inventory,
) -> None:
    source = to_dot(build_graph(annotated))
    assert "shape=note" in source
    assert "<B>Orange</B> links are fibre." in source
    assert "<I>thing</I>" in source
    assert '<FONT FACE="monospace">code</FONT>' in source
    leader = next(line for line in source.splitlines() if "constraint=false" in line)
    assert "style=dotted" in leader
    assert "arrowhead=none" in leader
    assert '"edge/sw-core"' in leader


def test_a_legend_is_a_cluster_of_swatches(annotated: Inventory) -> None:
    source = to_dot(build_graph(annotated))
    assert "subgraph cluster_legend_0 {" in source
    # The title is a row of the table, not the cluster's label: see
    # ``netviz.render.dot._legend_views``.
    assert "<B>Key</B>" in source
    assert 'BGCOLOR="#dcf0dc"' in source, "the switch swatch is the switch's fill"
    assert ">switch</TD>" in source


def test_a_fixed_layout_draws_areas_as_rectangles_with_no_text_in_the_background(
    tmp_path: Path,
) -> None:
    """``docs/follow-ups.md`` §17: a ``T`` operation here segfaults Graphviz 2.43."""
    inventory = write_inventory(tmp_path / "fixed", annotations=ANNOTATIONS, layout=LAYOUT)
    graph = build_graph(inventory)
    assert str(layout_plan(graph).mode) == "fixed"
    source = to_dot(graph)
    background = source.split('_background="')[1].split('",')[0]
    assert " T " not in background, "text in a _background segfaults Graphviz 2.43"
    assert not background.startswith("T ")
    assert " P 4 " in background, "the area is a filled rectangle"
    # …and the caption is an ordinary node with a position, as a namespace
    # frame's is.
    assert '"cluster-label:area-dmz" [shape=plaintext' in source
    assert 'label="DMZ"' in source


def test_a_fixed_layout_places_the_notes_and_the_legend(tmp_path: Path) -> None:
    inventory = write_inventory(tmp_path / "placed", annotations=ANNOTATIONS, layout=LAYOUT)
    source = to_dot(build_graph(inventory))
    note = next(line for line in source.splitlines() if "shape=note" in line)
    assert "pos=" in note, "the no-op engine places nothing it is not given"
    legend = next(line for line in source.splitlines() if "annotation:legend-key" in line)
    assert "pos=" in legend


def test_an_area_with_no_geometry_follows_the_members_it_names(tmp_path: Path) -> None:
    inventory = write_inventory(tmp_path / "hull", annotations=ANNOTATIONS, layout=LAYOUT)
    background = to_dot(build_graph(inventory)).split('_background="')[1].split('",')[0]
    # ``P 4`` and then four ``x y`` pairs: the rectangle, and nothing else.
    corners = [float(token) for token in background.split(" P 4 ")[1].split()]
    assert len(corners) == 8
    # The two members sit at x=100 and x=280, are 80 wide, and the area pads by
    # 16 — so the box runs from 100-40-16 to 280+40+16.
    assert min(corners[::2]) == pytest.approx(44.0)
    assert max(corners[::2]) == pytest.approx(336.0)


# --------------------------------------------------------------------------- #
# Mermaid
# --------------------------------------------------------------------------- #


def test_mermaid_draws_an_area_as_a_subgraph_and_a_note_as_a_node(
    annotated: Inventory,
) -> None:
    source = to_mermaid(build_graph(annotated))
    assert 'subgraph area0["DMZ"]' in source
    assert "classDef netvizNote" in source
    assert "note0[" in source
    assert "note0 -.- n0" in source, "the leader points at the anchor"


def test_mermaid_says_what_it_could_not_draw(annotated: Inventory) -> None:
    """A degradation nobody is told about is indistinguishable from a bug."""
    source = to_mermaid(build_graph(annotated))
    assert "%% legend 'key' (5 entries) is not drawn" in source
    assert "%% areas are drawn as subgraphs" in source


def test_mermaid_drops_an_area_that_is_a_rectangle_rather_than_a_set(
    tmp_path: Path,
) -> None:
    inventory = write_inventory(
        tmp_path / "canvas",
        annotations=(
            "apiVersion: netviz.dev/v1alpha1\n"
            "kind: area\n"
            "metadata: {name: ups}\n"
            "spec:\n"
            "  label: on the UPS\n"
            "  geometry: {x: 0, y: 0, width: 400, height: 200}\n"
        ),
    )
    source = to_mermaid(build_graph(inventory))
    assert "subgraph area0" not in source
    assert "%% area 'ups' is not drawn" in source


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def test_the_json_export_publishes_the_whole_annotation(annotated: Inventory) -> None:
    document = json.loads(to_json(build_graph(annotated)))
    annotations = document["annotations"]
    area = annotations["areas"][0]
    assert area["id"] == "area-dmz"
    assert area["fqn"] == "dmz"
    assert area["members"] == ["edge/sw-core", "edge/srv-proxy"]
    assert area["color"] == "#fee2e2"
    assert area["border"] == "dashed"

    note = annotations["notes"][0]
    assert note["anchor"] == "edge/sw-core"
    assert note["leader"] is True
    assert note["blocks"][0]["spans"][0] == {"style": "bold", "text": "Orange"}

    legend = annotations["legends"][0]
    assert legend["corner"] == "bottom-right"
    assert legend["entries"][0]["label"] == "switch"

    # Still not topology.
    ids = {node["id"] for node in document["nodes"]}
    assert not ids & {area["id"], note["id"], legend["id"]}


def test_the_json_export_never_publishes_a_member_it_did_not_draw(
    annotated: Inventory,
) -> None:
    graph = filter_graph(build_graph(annotated), FilterSpec(vlans=frozenset({10})))
    document = json.loads(to_json(graph))
    drawn = {node["id"] for node in document["nodes"]}
    for area in document["annotations"]["areas"]:
        assert set(area["members"]) <= drawn


def test_the_annotations_key_is_absent_when_there_are_none(plain: Inventory) -> None:
    assert "annotations" not in json.loads(to_json(build_graph(plain)))


# --------------------------------------------------------------------------- #
# The toggle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("render", [to_dot, to_mermaid, to_json])
def test_annotations_off_is_byte_identical_to_an_inventory_with_none(
    annotated: Inventory, plain: Inventory, render: object
) -> None:
    """A display option, not a filter: turning it off leaves no trace at all."""
    call = render  # for the type checker; every renderer has the same signature
    assert callable(call)
    off = call(build_graph(annotated), RenderOptions(annotations=False))
    never = call(build_graph(plain), RenderOptions(annotations=False))
    assert off == never
    # …and turning it on does change something, or the test above proves nothing.
    assert call(build_graph(annotated), RenderOptions()) != off


def test_the_option_defaults_to_on() -> None:
    assert RenderOptions().annotations is True
    assert dataclasses.replace(RenderOptions(), annotations=False).annotations is False


# --------------------------------------------------------------------------- #
# Graphviz actually draws it
# --------------------------------------------------------------------------- #


@requires_dot
@pytest.mark.parametrize("arrangement", ["auto", "fixed"])
def test_graphviz_renders_an_annotated_diagram(tmp_path: Path, arrangement: str) -> None:
    """The end of the argument: a real ``dot`` lays this out without crashing."""
    inventory = write_inventory(
        tmp_path / arrangement,
        annotations=ANNOTATIONS,
        layout=LAYOUT if arrangement == "fixed" else "",
    )
    graph = build_graph(inventory)
    assert str(layout_plan(graph).mode) == arrangement
    payload = to_image(graph, RenderOptions(element_ids=True), format="svg")
    assert payload.startswith(b"<?xml")
    text = payload.decode("utf-8")
    assert "DMZ" in text
    assert "Orange" in text
    assert "Key" in text


@requires_dot
def test_graphviz_renders_a_labelled_area_in_a_background(tmp_path: Path) -> None:
    """The §17 regression, run rather than argued: a ``T`` here would segfault."""
    inventory = write_inventory(tmp_path / "seg", annotations=ANNOTATIONS, layout=LAYOUT)
    payload = to_image(build_graph(inventory), format="svg")
    assert b"<polygon" in payload
    assert b"DMZ" in payload
