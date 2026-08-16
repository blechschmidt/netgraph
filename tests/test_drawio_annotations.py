"""Notes, areas and legends through the draw.io round trip (§21).

The three kinds of annotation are the first thing netviz exports that a
stakeholder is *meant* to edit rather than merely to read: a note is the reason
somebody was sent the diagram at all. So the properties asserted here are the
ones that decide whether that edit is worth anything, in descending order of how
much a failure would cost:

**The round trip is still a no-op.** An inventory with a note, an area and a
legend exports, parses and reconciles to an *empty* operation list. If opening
the file and saving it proposed changes, nobody could trust the changes it
proposed after a real edit — and the annotations are the part most likely to
break it, because a note's text passes through two conversions on the way out.

**Nothing is proposed for a legend, ever.** A key is generated from what the
drawing turned out to contain, so its cells exist to be recognised and ignored.
The failure mode of getting this wrong is a plan proposing to invent a document
per swatch, or — far worse — to delete the annotations of an inventory whose
diagram was exported before §21 existed.

**One edit means exactly one thing.** A dragged note is two geometry writes and
nothing else; a resized one adds two more; a retyped one is a text write; a
deleted one is a deletion.

**The file is a file.** Every cell is well-formed XML that a strict parser
accepts, carries the identity block the import reconciles by, and is drawn with
a shape draw.io actually knows.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from netviz.drawio import (
    ATTRIBUTES,
    BuildOptions,
    Cell,
    CellRole,
    ReconcileOptions,
    Scope,
    build_diagram,
    html_to_markup,
    markup_html,
    parse_mxfile,
    plain_text,
    reconcile,
    write_mxfile,
)
from netviz.drawio.markup import escape_html
from netviz.drawio.model import Diagram, absolute_geometry
from netviz.drawio.notes import Notes
from netviz.edit import EditSession
from netviz.edit.operations import CreateAnnotation, DeleteAnnotation, SetAnnotation
from netviz.loader import Inventory, load_tree
from netviz.render import FilterSpec, build_graph, filter_graph
from netviz.render.annotations import parse_markup
from netviz.render.graph import Layer

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
  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.2/24]}]
---
apiVersion: netviz.dev/v1alpha1
kind: cable
metadata: {name: cbl-fibre}
spec: {endpoints: [sw-core:eth0, srv-proxy:eth0], medium: fiber}
"""

#: One of each kind, and deliberately awkward: the note uses every piece of
#: markup the subset has, and the area is anchored to its members rather than to
#: a rectangle of its own, which is the case where the exported box is computed
#: and must therefore not come home as a change.
ANNOTATIONS = """\
apiVersion: netviz.dev/v1alpha1
kind: area
metadata: {name: dmz}
spec:
  label: DMZ & friends
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

#: The same tree with the note's document removed: what an inventory looks like
#: to a diagram exported before somebody else deleted the callout it holds.
AREA_ONLY = "\n".join(ANNOTATIONS.split("---")[:1])

#: The same three, with the note and the zone already pinned where somebody put
#: them. This is the state a *first* drag leaves behind, so it is the state a
#: *second* drag has to be tested against: an annotation that already has a
#: ``geometry`` is edited one field at a time, and one that has none cannot be,
#: because half a position is a document §21 refuses.
PLACED = ANNOTATIONS.replace(
    "  anchor: {element: edge/sw-core}",
    "  anchor: {element: edge/sw-core}\n  geometry: {x: 240, y: 420, width: 200, height: 96}",
).replace(
    '  color: "#fee2e2"',
    '  color: "#fee2e2"\n  geometry: {x: 190, y: 300, width: 300, height: 90}',
)

#: A stored arrangement, so the exported boxes are the ones §18 chose rather
#: than ones the grid invented. An area following its members is only a real
#: test when the members are somewhere in particular.
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
"""


def write_inventory(
    root: Path, *, annotations: str = ANNOTATIONS, layout: str = LAYOUT
) -> Inventory:
    (root / "edge").mkdir(parents=True, exist_ok=True)
    (root / "edge" / "net.yaml").write_text(TOPOLOGY, encoding="utf-8")
    if annotations:
        (root / "annotations.yaml").write_text(annotations, encoding="utf-8")
    if layout:
        (root / "layout.yaml").write_text(layout, encoding="utf-8")
    inventory = load_tree(root)
    assert inventory.errors == [], [str(error) for error in inventory.errors]
    return inventory


@pytest.fixture
def annotated(tmp_path: Path) -> Inventory:
    return write_inventory(tmp_path / "annotated")


@pytest.fixture
def placed(tmp_path: Path) -> Inventory:
    """The same inventory with the note and the zone pinned at a point of their own."""
    return write_inventory(tmp_path / "placed", annotations=PLACED)


def exported(inventory: Inventory, *, view: str = "l1", **overrides: object) -> str:
    graph = build_graph(inventory, layer=Layer(view))
    options = BuildOptions(view=view, **overrides)  # type: ignore[arg-type]
    return write_mxfile(build_diagram(graph, inventory, options))


def reconciled(
    inventory: Inventory, payload: str, *, view: str = "l1", **overrides: object
) -> object:
    diagram = parse_mxfile(payload)
    return reconcile(
        diagram,
        inventory,
        build_graph(inventory, layer=Layer(view)),
        ReconcileOptions(**overrides),  # type: ignore[arg-type]
    )


def cells_of(payload: str, role: CellRole) -> tuple[Cell, ...]:
    return parse_mxfile(payload).of_role(role)


def only(payload: str, role: CellRole) -> Cell:
    found = cells_of(payload, role)
    assert len(found) == 1, f"expected one {role} cell, got {len(found)}"
    return found[0]


def edited(payload: str, before: str, after: str) -> str:
    """``payload`` with one substitution, refusing to be a test of nothing."""
    assert before in payload, f"the fixture no longer holds {before!r}"
    return payload.replace(before, after, 1)


def dropped(payload: str, role: str) -> str:
    """``payload`` without the cells of one role — a stakeholder pressing delete."""
    pattern = re.compile(rf'\s*<object [^>]*netviz:role="{role}".*?</object>', re.DOTALL)
    edited_payload, count = pattern.subn("", payload)
    assert count, f"the fixture holds no {role} cell to delete"
    return edited_payload


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #


def test_the_export_is_well_formed_xml_a_strict_parser_accepts(annotated: Inventory) -> None:
    """The labels hold HTML, which is exactly where an escaping bug would land."""
    payload = exported(annotated)
    root = ElementTree.fromstring(payload)
    assert root.tag == "mxfile"
    # Not a rediscovery of the parser above: netviz's own reader is stricter,
    # and a document it refuses is one no round trip can begin.
    assert parse_mxfile(payload).is_netviz


def test_the_export_is_byte_stable(annotated: Inventory) -> None:
    """Two exports of one annotated inventory are one file, or it is not diffable."""
    assert exported(annotated) == exported(annotated)


def test_each_kind_becomes_the_native_shape_for_it(annotated: Inventory) -> None:
    """A note is a note, a zone is a container, a key is a key — not pictures of them."""
    payload = exported(annotated)
    assert "shape=note;" in only(payload, CellRole.NOTE).style
    area = only(payload, CellRole.AREA)
    assert "container=1;" in area.style and "collapsible=0;" in area.style
    assert "verticalAlign=top;" in area.style
    assert "container=1;" in cells_of(payload, CellRole.LEGEND)[0].style


def test_an_area_is_drawn_behind_the_nodes_it_encloses(annotated: Inventory) -> None:
    """mxGraph draws in document order, so 'behind' is a statement about the file."""
    cells = parse_mxfile(exported(annotated)).cells
    order = [cell.role for cell in cells]
    assert order.index(CellRole.AREA) < order.index(CellRole.NODE)
    assert order.index(CellRole.NODE) < order.index(CellRole.NOTE)


def test_an_area_encloses_the_boxes_of_its_members_and_not_only_their_centres(
    annotated: Inventory,
) -> None:
    payload = exported(annotated)
    diagram = parse_mxfile(payload)
    cells = diagram.by_id()
    area = only(payload, CellRole.AREA)
    left, top, width, height = absolute_geometry(area, cells)
    for node in cells_of(payload, CellRole.NODE):
        # Through :func:`absolute_geometry`, because a node hangs off its
        # namespace frame and its stored coordinates are that frame's, not the
        # page's — which is precisely the mistake this test would otherwise make
        # and the export could then get away with.
        node_left, node_top, node_width, node_height = absolute_geometry(node, cells)
        assert left <= node_left and node_left + node_width <= left + width
        assert top <= node_top and node_top + node_height <= top + height


def test_a_note_leader_is_an_edge_with_no_arrowheads(annotated: Inventory) -> None:
    payload = exported(annotated)
    leader = only(payload, CellRole.LEADER)
    assert leader.edge
    assert leader.source == only(payload, CellRole.NOTE).id
    assert leader.target == next(
        cell.id
        for cell in cells_of(payload, CellRole.NODE)
        if cell.attribute("name").endswith("sw-core")
    )
    assert "endArrow=none;" in leader.style and "startArrow=none;" in leader.style
    assert "dashed=1;" in leader.style


def test_a_legend_is_a_frame_with_a_swatch_and_a_caption_per_row(annotated: Inventory) -> None:
    payload = exported(annotated)
    legend = cells_of(payload, CellRole.LEGEND)
    frame = legend[0]
    rows = [cell for cell in legend[1:] if cell.parent == frame.id]
    assert frame.label == "Key"
    # Two cells per entry — a swatch and its meaning — and every one of them a
    # child of the frame, so dragging the key moves the whole thing.
    assert len(rows) == len(legend) - 1 and len(rows) % 2 == 0
    assert {"switch", "server", "fiber"} <= {cell.label for cell in rows}


def test_the_markup_subset_becomes_html_a_browser_draws(annotated: Inventory) -> None:
    label = only(exported(annotated), CellRole.NOTE).label
    assert "<b>Orange</b>" in label
    assert "<ul><li>one <i>thing</i></li>" in label
    assert "<code>code</code>" in label
    assert "html=1" in only(exported(annotated), CellRole.NOTE).style


def test_the_html_is_escaped_into_the_attribute_rather_than_reaching_the_parser(
    annotated: Inventory,
) -> None:
    """The label is HTML inside an XML attribute: two escapings, composed."""
    payload = exported(annotated)
    assert "&lt;b&gt;Orange&lt;/b&gt;" in payload, "the tags belong to the label, not to the XML"
    # The area's caption holds an ampersand, which has to survive both layers.
    assert only(payload, CellRole.AREA).label == "DMZ &amp; friends"
    assert plain_text(only(payload, CellRole.AREA).label) == "DMZ & friends"


def test_every_annotation_cell_carries_the_identity_the_import_reconciles_by(
    annotated: Inventory,
) -> None:
    payload = exported(annotated)
    for role, kind, name in (
        (CellRole.NOTE, "note", "why-orange"),
        (CellRole.AREA, "area", "dmz"),
    ):
        cell = only(payload, role)
        assert cell.attribute("role") == kind
        assert cell.attribute("kind") == kind
        assert cell.attribute("name") == name
        assert cell.attribute("document") == "annotations.yaml"
        assert cell.attribute("hash").startswith("sha256:")
        assert cell.attribute("x") and cell.attribute("y")
        assert cell.attribute("width") and cell.attribute("height")
        assert set(cell.attributes) <= ATTRIBUTES, "an attribute nothing declares is unreadable"
    for cell in cells_of(payload, CellRole.LEGEND):
        assert cell.attribute("name") == "key"
        assert set(cell.attributes) <= ATTRIBUTES


def test_the_file_says_it_was_written_by_an_exporter_that_draws_annotations(
    annotated: Inventory,
) -> None:
    """Which is what makes a *missing* note readable as a deletion, and only then."""
    assert parse_mxfile(exported(annotated)).annotated
    assert not Diagram().annotated


def test_annotations_can_be_turned_off(annotated: Inventory) -> None:
    payload = exported(annotated, annotations=False)
    for role in (CellRole.NOTE, CellRole.AREA, CellRole.LEGEND, CellRole.LEADER):
        assert not cells_of(payload, role)
    assert not parse_mxfile(payload).annotated


def test_an_annotation_this_view_cannot_draw_is_reported_rather_than_dropped(
    tmp_path: Path,
) -> None:
    """Silently is how a renderer drops an empty zone; the manifest is where it is said."""
    inventory = write_inventory(
        tmp_path / "elsewhere",
        annotations=AREA_ONLY.replace(
            "  members: [edge/sw-core, edge/srv-proxy]", "  members: [edge/srv-proxy]"
        ),
    )
    graph = filter_graph(build_graph(inventory), FilterSpec(kinds=("switch",)))
    assert "edge/srv-proxy" not in graph.nodes, "the fixture must actually lose the member"
    record = Notes()
    build_diagram(graph, inventory, BuildOptions(view="l1"), notes=record)
    assert any(
        "left out" in note.message and note.subject == "area/dmz" for note in record.sealed()
    )


def test_an_inventory_with_no_annotations_exports_what_it_always_did(tmp_path: Path) -> None:
    """§21 is additive: a tree with no note in it produces the same topology cells."""
    inventory = write_inventory(tmp_path / "plain", annotations="")
    payload = exported(inventory)
    assert not cells_of(payload, CellRole.NOTE)
    assert len(cells_of(payload, CellRole.NODE)) == 2


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fixture", ["annotated", "placed"])
def test_a_full_round_trip_over_all_three_kinds_proposes_nothing(
    fixture: str, request: pytest.FixtureRequest
) -> None:
    """The property the whole feature rests on, for computed boxes and pinned ones."""
    inventory: Inventory = request.getfixturevalue(fixture)
    result = reconciled(inventory, exported(inventory))
    assert result.operations == (), [operation.describe() for operation in result.operations]
    assert result.summary() == "nothing to change"
    assert result.unmapped == 0, "an annotation cell netviz wrote is never 'unmapped'"


def test_the_operations_an_edited_diagram_asks_for_actually_apply(tmp_path: Path) -> None:
    """Reconciling is half the job; the other half is that the writes are legal.

    Every field written to an annotation is checked against §21 *as it lands*,
    so an operation list that is individually invalid at any point fails against
    a real tree while looking perfectly reasonable in a plan. This is that
    check, run the way the CLI runs it.
    """
    root = tmp_path / "tree"
    inventory = write_inventory(root)
    payload = edited(
        moved_note(exported(inventory)), "&lt;b&gt;Orange&lt;/b&gt;", "&lt;b&gt;Amber&lt;/b&gt;"
    )
    result = reconciled(inventory, payload)
    session = EditSession(root=root)
    session.apply_all(result.operations)
    session.commit()

    after = load_tree(root)
    assert after.errors == [], [str(error) for error in after.errors]
    note = after.annotations_of("note")["why-orange"]
    assert note.spec.geometry is not None and note.spec.geometry.placed
    assert note.spec.text.startswith("**Amber** links are fibre.")
    # And the diagram exported from the result is now a fixed point again.
    assert reconciled(after, exported(after)).operations == ()


@pytest.mark.parametrize("view", ["l1", "l2", "l3"])
def test_the_round_trip_is_a_no_op_in_every_view_that_draws_them(
    annotated: Inventory, view: str
) -> None:
    """An annotation with no ``views`` is drawn everywhere, so this must hold everywhere."""
    assert reconciled(annotated, exported(annotated, view=view), view=view).operations == ()


def test_a_compressed_round_trip_carries_the_annotations_too(annotated: Inventory) -> None:
    graph = build_graph(annotated, layer=Layer.L1)
    diagram = build_diagram(graph, annotated, BuildOptions(view="l1"))
    plain = parse_mxfile(write_mxfile(diagram))
    packed = parse_mxfile(write_mxfile(diagram, compress=True))
    assert packed.cells == plain.cells


def test_a_legend_never_proposes_anything_even_when_it_is_edited(annotated: Inventory) -> None:
    """Generated presentation. Recognised, and then ignored — including a retyped row."""
    payload = edited(exported(annotated), '<object label="switch"', '<object label="switches"')
    result = reconciled(annotated, payload)
    assert result.operations == ()
    assert result.unmapped == 0


def test_a_legend_somebody_deleted_from_the_diagram_is_not_a_deletion(
    annotated: Inventory,
) -> None:
    """A key is redrawn from the graph on every export; removing it says nothing.

    A stakeholder who deletes the legend to make room on the page has said
    something about the *page*, and proposing to delete the document that
    generates it would be reading a layout decision as an inventory one.
    """
    result = reconciled(annotated, dropped(exported(annotated), "legend"))
    assert result.operations == ()


def test_a_diagram_from_before_annotations_existed_deletes_nothing(annotated: Inventory) -> None:
    """The one way a new importer could destroy an old inventory, closed by hand.

    Such a file holds no annotation cells at all — not because they were
    deleted, but because nothing ever wrote them — so the absence must not be
    read as a gesture.
    """
    payload = exported(annotated).replace(' netviz:annotated="1"', "")
    payload = dropped(dropped(dropped(payload, "note"), "area"), "leader")
    result = reconciled(annotated, payload)
    assert not any(isinstance(operation, DeleteAnnotation) for operation in result.operations)


def test_a_partial_export_never_deletes_an_annotation(annotated: Inventory) -> None:
    """Absence proves nothing about a diagram that was narrowed before it was drawn."""
    payload = dropped(exported(annotated, scope=Scope.PARTIAL), "note")
    result = reconciled(annotated, payload)
    assert not any(isinstance(operation, DeleteAnnotation) for operation in result.operations)


# --------------------------------------------------------------------------- #
# One edit means one thing
# --------------------------------------------------------------------------- #


def moved_note(payload: str, *, right: float = 40.0, down: float = 25.0) -> str:
    """``payload`` with the note cell dragged, and nothing else touched."""
    cell = only(payload, CellRole.NOTE)
    x, y, width, _height = cell.geometry
    return edited(
        payload,
        f'<mxGeometry x="{_number(x)}" y="{_number(y)}" width="{_number(width)}"',
        f'<mxGeometry x="{_number(x + right)}" y="{_number(y + down)}" width="{_number(width)}"',
    )


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.12g}"


def test_a_moved_note_is_exactly_two_geometry_writes(placed: Inventory) -> None:
    """And nothing else: not a text write, not a deletion, not a new cable."""
    result = reconciled(placed, moved_note(exported(placed)))
    assert [operation.path for operation in result.operations] == [
        "spec.geometry.x",
        "spec.geometry.y",
    ]
    assert all(isinstance(operation, SetAnnotation) for operation in result.operations)
    assert all(operation.kind == "note" for operation in result.operations)
    assert all(operation.name == "why-orange" for operation in result.operations)
    assert result.annotated == 2 and result.moved == 0


def test_the_first_drag_of_an_unplaced_note_writes_one_whole_geometry_block(
    annotated: Inventory,
) -> None:
    """Because half a position is a document §21 refuses, field by field.

    A note that follows its anchor has no ``geometry`` at all, and every write
    is checked against the schema the moment it lands — so ``spec.geometry.x``
    on its own would be rejected before ``spec.geometry.y`` could arrive. The
    block goes in as one write, which is what
    :class:`~netviz.edit.operations.SetAnnotation` says the first drag does.
    """
    (operation,) = reconciled(annotated, moved_note(exported(annotated))).operations
    assert isinstance(operation, SetAnnotation)
    assert operation.path == "spec.geometry"
    assert set(operation.value) == {"x", "y"}


def test_the_position_written_is_the_one_the_note_was_dragged_to(placed: Inventory) -> None:
    """draw.io's y runs downwards and its origin is the page's; netviz's do not."""
    payload = exported(placed)
    before = only(payload, CellRole.NOTE)
    operations = reconciled(placed, moved_note(payload, right=40.0, down=25.0)).operations
    written = {operation.path: operation.value for operation in operations}
    assert written["spec.geometry.x"] == float(before.attribute("x")) + 40.0
    assert written["spec.geometry.y"] == float(before.attribute("y")) - 25.0


def test_a_resized_note_writes_its_extent_as_well(placed: Inventory) -> None:
    payload = exported(placed)
    cell = only(payload, CellRole.NOTE)
    _x, _y, width, height = cell.geometry
    resized = edited(
        payload,
        f'width="{_number(width)}" height="{_number(height)}" as="geometry"',
        f'width="{_number(width + 60)}" height="{_number(height + 20)}" as="geometry"',
    )
    written = {
        operation.path: operation.value for operation in reconciled(placed, resized).operations
    }
    assert written["spec.geometry.width"] == width + 60
    assert written["spec.geometry.height"] == height + 20


def test_a_dragged_zone_takes_its_extent_with_it(annotated: Inventory) -> None:
    """Or it would snap back: a zone with a position and no size follows its members."""
    payload = exported(annotated)
    cell = only(payload, CellRole.AREA)
    x, y, width, _height = cell.geometry
    dragged = edited(
        payload,
        f'<mxGeometry x="{_number(x)}" y="{_number(y)}" width="{_number(width)}"',
        f'<mxGeometry x="{_number(x + 30)}" y="{_number(y)}" width="{_number(width)}"',
    )
    (operation,) = reconciled(annotated, dragged).operations
    assert operation.path == "spec.geometry"
    assert set(operation.value) == {"x", "y", "width", "height"}
    assert operation.value["width"] == width


def test_a_retyped_note_comes_back_as_the_markdown_subset(annotated: Inventory) -> None:
    payload = edited(exported(annotated), "&lt;b&gt;Orange&lt;/b&gt;", "&lt;b&gt;Amber&lt;/b&gt;")
    (operation,) = reconciled(annotated, payload).operations
    assert isinstance(operation, SetAnnotation)
    assert operation.path == "spec.text"
    assert operation.value.startswith("**Amber** links are fibre.")
    assert "- one *thing*" in operation.value
    assert "`code` two" in operation.value


def test_markup_draw_io_cannot_express_keeps_its_words(annotated: Inventory) -> None:
    """A tag netviz did not write loses the tag and never the text."""
    payload = edited(
        exported(annotated),
        "&lt;b&gt;Orange&lt;/b&gt;",
        "&lt;font color=&quot;red&quot;&gt;Orange&lt;/font&gt;",
    )
    (operation,) = reconciled(annotated, payload).operations
    assert operation.value.startswith("Orange links are fibre.")


def test_an_emptied_note_is_reported_rather_than_written(annotated: Inventory) -> None:
    """A note with no text is not a note, and ``spec.text`` refuses to be empty."""
    payload = only(exported(annotated), CellRole.NOTE)
    blanked = edited(exported(annotated), escape_xml(payload.label), "")
    result = reconciled(annotated, blanked)
    assert result.operations == ()
    assert any("delete the cell" in note.message for note in result.notes)


def escape_xml(value: str) -> str:
    """The label as it appears *in the file*, for a substitution to find it."""
    for character, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;")):
        value = value.replace(character, entity)
    return value


def test_a_retyped_area_caption_is_one_label_write(annotated: Inventory) -> None:
    payload = edited(
        exported(annotated), '<object label="DMZ &amp;amp; friends"', '<object label="Perimeter"'
    )
    (operation,) = reconciled(annotated, payload).operations
    assert isinstance(operation, SetAnnotation)
    assert (operation.kind, operation.name, operation.path) == ("area", "dmz", "spec.label")
    assert operation.value == "Perimeter"


def test_a_deleted_note_is_a_deletion_of_the_note_and_of_nothing_else(
    annotated: Inventory,
) -> None:
    payload = dropped(dropped(exported(annotated), "note"), "leader")
    (operation,) = reconciled(annotated, payload).operations
    assert isinstance(operation, DeleteAnnotation)
    assert (operation.kind, operation.name) == ("note", "why-orange")


def test_a_sticky_note_somebody_added_becomes_a_note_document(annotated: Inventory) -> None:
    """The one vertex a draw.io user can add that netviz is willing to write."""
    payload = edited(
        exported(annotated),
        "        </root>",
        '          <mxCell id="drawn-1" value="check the &lt;b&gt;uplink&lt;/b&gt;" '
        'style="shape=note;whiteSpace=wrap;html=1;" vertex="1" parent="1">\n'
        '            <mxGeometry x="500" y="500" width="160" height="80" as="geometry" />\n'
        "          </mxCell>\n"
        "        </root>",
    )
    (operation,) = reconciled(annotated, payload).operations
    assert isinstance(operation, CreateAnnotation)
    assert operation.kind == "note"
    assert operation.name == "check-the-uplink"
    assert operation.spec["text"] == "check the **uplink**"
    assert operation.spec["geometry"]["width"] == 160.0


def test_an_ordinary_rectangle_somebody_added_is_still_left_alone(annotated: Inventory) -> None:
    """netviz does not invent documents from shapes. The note is the exception."""
    payload = edited(
        exported(annotated),
        "        </root>",
        '          <mxCell id="drawn-2" value="something" style="rounded=0;whiteSpace=wrap;" '
        'vertex="1" parent="1">\n'
        '            <mxGeometry x="500" y="500" width="160" height="80" as="geometry" />\n'
        "          </mxCell>\n"
        "        </root>",
    )
    result = reconciled(annotated, payload)
    assert result.operations == ()
    assert result.unmapped == 1


def test_the_annotation_switch_turns_the_whole_thing_off(annotated: Inventory) -> None:
    """Off must still not report netviz's own cells as things it cannot place."""
    payload = moved_note(exported(annotated))
    result = reconciled(annotated, payload, annotations=False)
    assert result.operations == ()
    assert result.unmapped == 0


def test_an_annotation_the_inventory_has_lost_is_reported_not_written(
    annotated: Inventory, tmp_path: Path
) -> None:
    payload = exported(annotated)
    without = write_inventory(tmp_path / "without", annotations=AREA_ONLY)
    result = reconciled(without, payload)
    assert not any(isinstance(operation, SetAnnotation) for operation in result.operations)
    assert any("not in the inventory any more" in note.message for note in result.notes)


# --------------------------------------------------------------------------- #
# The markdown subset, both ways
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "html"),
    [
        ("plain", "<div>plain</div>"),
        ("**bold**", "<div><b>bold</b></div>"),
        ("*it*", "<div><i>it</i></div>"),
        ("`code`", "<div><code>code</code></div>"),
        ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
        ("a & b", "<div>a &amp; b</div>"),
        ("<script>", "<div>&lt;script&gt;</div>"),
    ],
)
def test_the_subset_renders_as_the_html_draw_io_draws(text: str, html: str) -> None:
    assert markup_html(parse_markup(text)) == html


@given(
    st.lists(
        st.sampled_from(
            [
                "plain words",
                "**bold** and more",
                "an *emphasis* here",
                "a `code` span",
                "- a bullet",
                "- another bullet",
                'punctuation & <angles> "quoted"',
            ]
        ),
        min_size=1,
        max_size=6,
    )
)
@settings(max_examples=200, deadline=None)
def test_html_and_markup_are_inverses_over_the_subset(lines: list[str]) -> None:
    """The property the import's 'was this edited?' question depends on.

    Rendering a note and reading it back has to reach a fixed point in one step,
    or an untouched diagram would come home proposing to rewrite every note in
    it — which is the no-op guarantee, stated as a property rather than as an
    example.
    """
    text = "\n\n".join(lines)
    html = markup_html(parse_markup(text))
    assert markup_html(parse_markup(html_to_markup(html))) == html


@given(st.text(max_size=200))
@settings(max_examples=200, deadline=None)
def test_reading_a_label_back_never_raises(label: str) -> None:
    """It runs on a file a third party edited; a traceback there loses every edit."""
    assert isinstance(html_to_markup(label), str)
    assert isinstance(plain_text(label), str)


def test_a_caption_survives_being_escaped_and_read_back() -> None:
    for caption in ("DMZ", "a & b", "<b>", 'q"q', "über"):
        assert plain_text(escape_html(caption)) == caption


def _annotation_cells(payload: str) -> Iterator[Cell]:
    for role in (CellRole.NOTE, CellRole.AREA, CellRole.LEGEND, CellRole.LEADER):
        yield from cells_of(payload, role)


def test_no_annotation_cell_collides_with_an_element_cell(annotated: Inventory) -> None:
    """Two cells with one id is a diagram draw.io resolves by dropping one."""
    payload = exported(annotated)
    identifiers = [cell.id for cell in parse_mxfile(payload).cells]
    assert len(identifiers) == len(set(identifiers))
    assert all(
        cell.id.split("-")[0] in {"note", "area", "legend", "leader"}
        for cell in _annotation_cells(payload)
    )
