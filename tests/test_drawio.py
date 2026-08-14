"""The draw.io round trip: export, edit, import, and the fidelity of the loop.

Four properties are asserted here, in this order of importance:

**The round trip is a no-op.** ``inventory → drawio → import → inventory``
produces an *empty plan*, for every published example and for every view. This
is the property the whole feature rests on: if re-importing an untouched diagram
proposed changes, nobody could ever trust the ones it proposed after a real
edit. It is checked by running the real pipeline — the same emitter the CLI
runs, the same reconciler, the same :func:`netgraph.plan.diff` — rather than by
comparing coordinates, because a no-op is a statement about the *plan*.

**An edit means exactly one thing.** ``tests/fixtures/drawio/arranged-edited.
drawio`` is the pristine export with the four gestures a draw.io user can
perform applied to it, and the plan it must produce is committed beside it.
``tools/gen_drawio_fixtures.py`` regenerates both, applying the edits as
reviewable text substitutions rather than committing an opaque blob.

**Both encodings read.** draw.io writes plain XML or deflate+base64, and a tool
that refuses one refuses most of the files it will be handed.

**A hostile file is refused rather than parsed.** These are documents a third
party sends. A document type declaration, an oversized file and a deflate bomb
each get a sentence, not a traceback and not an expanded entity.
"""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner
from hypothesis import given, settings
from hypothesis import strategies as st

from netgraph.cli import cli
from netgraph.drawio import (
    ATTRIBUTES,
    MODEL_VERSION,
    BuildOptions,
    Cell,
    CellRole,
    Diagram,
    DrawioFormatError,
    Frame,
    Level,
    Placedness,
    ReconcileOptions,
    Scope,
    build_diagram,
    cell_id,
    content_hash,
    decode_diagram,
    encode_diagram,
    infer_kind,
    parse_mxfile,
    qualified,
    reconcile,
    write_mxfile,
)
from netgraph.drawio.build import HALF_SELECTED, NOT_ARRANGED, NOT_REPRESENTABLE
from netgraph.drawio.identity import format_points, parse_points
from netgraph.drawio.mxfile import MAX_DOCUMENT_BYTES
from netgraph.drawio.styles import data_uri, icon_data_uri
from netgraph.edit import EditSession, RenameElement, SetGeometry
from netgraph.export import EXPORTERS, ExportContext, ExportOptions, export, layers_for
from netgraph.export.manifest import Reason
from netgraph.fsio import write_text
from netgraph.loader import Inventory, load_tree
from netgraph.plan import diff as diff_states
from netgraph.plan import render_plan
from netgraph.render import build_graph, filter_graph
from netgraph.render.graph import FilterSpec, Layer
from netgraph.render.icons import CISCO, icon_theme

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "drawio"
FIXTURE_INVENTORY = FIXTURES / "inventory"
ARRANGED = Path(__file__).resolve().parent / "fixtures" / "arranged"

#: Every published example, plus the two arranged fixtures. The arranged ones
#: matter most: an inventory with a *stored* arrangement is the case the export
#: exists to serve, and the case where an inversion error would show.
EXAMPLE_TREES = ("home-lab", "campus", "patch-room", "overlay", "quickstart")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def run_export(
    inventory: Inventory,
    *,
    view: str = "l1",
    spec: FilterSpec | None = None,
    **overrides: object,
) -> str:
    """Export ``inventory`` the way :func:`netgraph.cli.export_command` does.

    ``complete`` follows from ``spec``, exactly as the CLI derives it from what
    the reader typed: an export that was narrowed is a partial diagram, and
    that is what the file has to say about itself.
    """
    settings: dict[str, object] = {"view": view, "icons": CISCO, "complete": spec is None}
    settings.update(overrides)
    options = ExportOptions(**settings)  # type: ignore[arg-type]
    narrowing = spec or FilterSpec()
    graphs = {
        layer: filter_graph(build_graph(inventory, layer=layer), narrowing)
        for layer in layers_for("drawio", options)
    }
    return export(
        "drawio",
        lambda recorder: ExportContext(
            inventory=inventory, graphs=graphs, options=options, recorder=recorder
        ),
    ).payload


def round_trip(inventory: Inventory, *, view: str = "l1") -> tuple[object, ...]:
    """The operations a freshly exported, unedited diagram asks for."""
    diagram = parse_mxfile(run_export(inventory, view=view))
    graph = build_graph(inventory, layer=Layer(view))
    return reconcile(diagram, inventory, graph).operations


@pytest.fixture(scope="module")
def inventories() -> dict[str, Inventory]:
    """Every tree the module reads, loaded once."""
    trees = {name: load_tree(EXAMPLES / name) for name in EXAMPLE_TREES}
    trees["arranged"] = load_tree(ARRANGED)
    trees["fixture"] = load_tree(FIXTURE_INVENTORY)
    return trees


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A writable copy of the fixture inventory, for tests that commit."""
    target = tmp_path / "inventory"
    for source in sorted(FIXTURE_INVENTORY.rglob("*")):
        if source.is_dir():
            continue
        destination = target / source.relative_to(FIXTURE_INVENTORY)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_text(destination, source.read_text(encoding="utf-8"))
    return target


def invoke(runner: CliRunner, *args: str) -> object:
    return runner.invoke(cli, list(args), catch_exceptions=False)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def test_the_format_is_registered_like_every_other() -> None:
    """``drawio`` goes through the same registry, so the CLI and docs pick it up."""
    exporter = EXPORTERS["drawio"]
    assert exporter.name == "drawio"
    assert exporter.suffix == ".drawio"
    assert exporter.lossy and exporter.description
    assert exporter.select is not None


@pytest.mark.parametrize("layer", list(Layer))
def test_every_view_is_exportable(layer: Layer) -> None:
    """``--view`` accepts each of the nine layers, and each builds its own graph."""
    assert layers_for("drawio", ExportOptions(view=layer.value)) == (layer,)


def test_the_declared_layers_are_the_default_when_no_options_are_given() -> None:
    """A caller with nothing to say still gets a usable answer."""
    assert layers_for("drawio") == (Layer.L1,)


@pytest.mark.parametrize("token", [NOT_ARRANGED, NOT_REPRESENTABLE, HALF_SELECTED])
def test_the_builders_reason_tokens_are_real_manifest_reasons(token: str) -> None:
    """The wire format spells them as literals; they must still be Reasons.

    :mod:`netgraph.drawio` must not import :mod:`netgraph.export` — the
    dependency runs the other way — so the tokens are restated there. This is
    what stops the two copies drifting apart in silence.
    """
    assert Reason(token).value == token


# --------------------------------------------------------------------------- #
# The file format
# --------------------------------------------------------------------------- #


def test_the_export_is_well_formed_xml_and_names_its_namespace(
    inventories: dict[str, Inventory],
) -> None:
    payload = run_export(inventories["arranged"])
    assert payload.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert f'xmlns:netgraph="{qualified("").rstrip(":")}"' not in payload  # sanity: not the prefix
    assert "xmlns:netgraph=" in payload
    assert payload.endswith("\n")
    parse_mxfile(payload)  # raises if it is not well-formed


def test_the_export_is_byte_stable(inventories: dict[str, Inventory]) -> None:
    """Two exports of one inventory are the same file, or it is not diffable."""
    assert run_export(inventories["campus"]) == run_export(inventories["campus"])


@pytest.mark.parametrize("compress", [False, True])
def test_both_encodings_carry_the_same_model(
    inventories: dict[str, Inventory], compress: bool
) -> None:
    """draw.io writes both; a reader that takes one takes most files, not all."""
    plain = parse_mxfile(run_export(inventories["home-lab"]))
    other = parse_mxfile(run_export(inventories["home-lab"], compress=compress))
    assert other.cells == plain.cells
    assert other.frame == plain.frame
    assert other.view == plain.view == "l1"


def test_the_compressed_encoding_is_the_one_drawio_writes() -> None:
    """URI-encode, raw-deflate, base64 — in that order, or draw.io opens nothing."""
    model = "<mxGraphModel><root><mxCell id='0'/></root></mxGraphModel>"
    payload = encode_diagram(model)
    assert decode_diagram(payload) == model
    # Raw deflate: no zlib header, which is what ``-15`` selects on both sides.
    with pytest.raises(zlib.error):
        zlib.decompress(__import__("base64").b64decode(payload))


def test_the_compressed_fixture_reads(inventories: dict[str, Inventory]) -> None:
    compressed = parse_mxfile((FIXTURES / "arranged-l1-compressed.drawio").read_text("utf-8"))
    plain = parse_mxfile((FIXTURES / "arranged-l1.drawio").read_text("utf-8"))
    assert compressed.cells == plain.cells


def test_the_committed_export_matches_what_the_emitter_produces_today(
    inventories: dict[str, Inventory], regen_golden: bool
) -> None:
    """The golden diagram, so a change to the emitter is reviewed as a diff."""
    golden = FIXTURES / "arranged-l1.drawio"
    actual = run_export(inventories["fixture"])
    if regen_golden:
        write_text(golden, actual)
        pytest.skip(f"regenerated {golden.name}; run tools/gen_drawio_fixtures.py for all three")
    assert actual == golden.read_text(encoding="utf-8"), (
        "the drawio export drifted from its golden file. If the change is intended, "
        "run 'python tools/gen_drawio_fixtures.py' and review the diff."
    )


def test_a_namespace_becomes_a_container_its_nodes_hang_off(
    inventories: dict[str, Inventory],
) -> None:
    """Which is what makes dragging a site in draw.io carry its devices."""
    diagram = parse_mxfile(run_export(inventories["fixture"]))
    groups = {cell.attribute("name"): cell for cell in diagram.of_role(CellRole.GROUP)}
    # ``cables/`` holds only cables, which are edges: a namespace with no node
    # in it gets no frame, because a frame round nothing says nothing.
    assert set(groups) == {"devices", "hosts"}
    for cell in diagram.of_role(CellRole.NODE):
        namespace = cell.attribute("node").rpartition("/")[0]
        assert cell.parent == groups[namespace].id
        assert "container=1" in groups[namespace].style


def test_frames_can_be_turned_off(inventories: dict[str, Inventory]) -> None:
    diagram = parse_mxfile(run_export(inventories["fixture"], frames=False))
    assert not diagram.of_role(CellRole.GROUP)
    assert all(cell.parent == "1" for cell in diagram.of_role(CellRole.NODE))


def test_every_node_carries_the_identity_the_import_reconciles_by(
    inventories: dict[str, Inventory],
) -> None:
    inventory = inventories["fixture"]
    diagram = parse_mxfile(run_export(inventory))
    nodes = diagram.of_role(CellRole.NODE)
    assert {cell.attribute("name") for cell in nodes} == set(inventory.elements) - {
        fqn for fqn, element in inventory.elements.items() if element.kind == "cable"
    }
    for cell in nodes:
        assert cell.attribute("kind")
        assert cell.attribute("document").endswith(".yaml")
        assert cell.attribute("hash").startswith("sha256:")
        assert cell.attribute("placed") == Placedness.STORED.value
        assert set(cell.attributes) <= ATTRIBUTES


def test_the_icons_are_inlined_so_the_file_needs_nothing_beside_it(
    inventories: dict[str, Inventory],
) -> None:
    """A stakeholder opens the file on a machine that has never seen netgraph."""
    diagram = parse_mxfile(run_export(inventories["fixture"]))
    styles = [cell.style for cell in diagram.of_role(CellRole.NODE)]
    assert styles and all("shape=image;" in style for style in styles)
    assert all("image=data:image/svg+xml," in style for style in styles)
    # No semicolon inside the value: draw.io splits a style on it, so the
    # ordinary ``;base64,`` spelling would truncate every icon.
    for style in styles:
        payload = style.partition("image=data:image/svg+xml,")[2].partition(";")[0]
        assert payload and "=" not in payload.rstrip("=")


def test_icons_can_be_turned_off(inventories: dict[str, Inventory]) -> None:
    diagram = parse_mxfile(run_export(inventories["fixture"], icons=None))
    for cell in diagram.of_role(CellRole.NODE):
        assert "shape=image" not in cell.style
        assert "fillColor=#" in cell.style


def test_a_theme_with_no_picture_for_a_kind_falls_back_and_says_so(tmp_path: Path) -> None:
    """A partial theme is legitimate; a diagram that fails over one is not."""
    theme_dir = tmp_path / "theme"
    theme_dir.mkdir()
    (theme_dir / "router.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    inventory = load_tree(FIXTURE_INVENTORY)
    options = ExportOptions(view="l1", icons=icon_theme(str(theme_dir)))
    result = export(
        "drawio",
        lambda recorder: ExportContext(
            inventory=inventory,
            graphs={Layer.L1: build_graph(inventory, layer=Layer.L1)},
            options=options,
            recorder=recorder,
        ),
    )
    reasons = {skip.reason for skip in result.manifest.skipped}
    assert Reason.NOT_REPRESENTABLE in reasons
    assert "image=data:image/png," in result.payload


def test_an_unarranged_inventory_still_exports_and_says_the_layout_is_netgraphs(
    inventories: dict[str, Inventory],
) -> None:
    inventory = inventories["home-lab"]
    result = export(
        "drawio",
        lambda recorder: ExportContext(
            inventory=inventory,
            graphs={Layer.L1: build_graph(inventory, layer=Layer.L1)},
            options=ExportOptions(view="l1"),
            recorder=recorder,
        ),
    )
    assert Reason.NOT_ARRANGED in {skip.reason for skip in result.manifest.skipped}
    diagram = parse_mxfile(result.payload)
    assert all(
        cell.attribute("placed") == Placedness.AUTO.value for cell in diagram.of_role(CellRole.NODE)
    )


def test_an_empty_inventory_produces_an_openable_diagram(tmp_path: Path) -> None:
    """Nothing to draw is a diagram with no cells, not a crash and not a blank file."""
    (tmp_path / "netgraph.toml").write_text("", encoding="utf-8")
    diagram = parse_mxfile(run_export(load_tree(tmp_path)))
    assert diagram.is_netgraph
    assert not diagram.of_role(CellRole.NODE)


def test_a_hostile_free_text_field_survives_the_xml(tmp_path: Path) -> None:
    """An inventory can hold every character XML has an opinion about."""
    hostile = "q\"uote <&> 'apos\ttab"
    write_text(
        tmp_path / "device.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n"
        f"  name: sw-1\n  description: {json.dumps(hostile)}\n"
        f"  labels:\n    role: {json.dumps(hostile)}\n"
        "spec:\n  interfaces:\n    - name: e0\n      type: ethernet\n",
    )
    inventory = load_tree(tmp_path)
    assert not inventory.errors
    payload = run_export(inventory)
    assert "<&>" not in payload  # every one of the five entities is escaped
    diagram = parse_mxfile(payload)
    assert [cell.label for cell in diagram.of_role(CellRole.NODE)] == ["sw-1"]


# --------------------------------------------------------------------------- #
# Coordinates
# --------------------------------------------------------------------------- #

COORDINATES = st.floats(min_value=-500_000, max_value=500_000, allow_nan=False)
EXTENTS = st.floats(min_value=0.01, max_value=10_000, allow_nan=False)


@given(origin_x=COORDINATES, origin_y=COORDINATES, x=COORDINATES, y=COORDINATES)
@settings(max_examples=200, deadline=None)
def test_the_two_coordinate_systems_are_exact_inverses(
    origin_x: float, origin_y: float, x: float, y: float
) -> None:
    """Every stored position passes through this twice; a drift here is a drift."""
    frame = Frame(origin_x=origin_x, origin_y=origin_y)
    back = frame.to_netgraph(*frame.to_drawio(x, y))
    assert back[0] == pytest.approx(x, abs=0.01)
    assert back[1] == pytest.approx(y, abs=0.01)


@given(x=COORDINATES, y=COORDINATES, width=EXTENTS, height=EXTENTS)
@settings(max_examples=200, deadline=None)
def test_a_box_survives_the_centre_to_corner_conversion(
    x: float, y: float, width: float, height: float
) -> None:
    """netgraph places a centre, draw.io places a corner; neither may drift."""
    frame = Frame(origin_x=-13.5, origin_y=907.25)
    corner = frame.box_to_drawio(x, y, width, height)
    centre = frame.box_to_netgraph(corner[0], corner[1], width, height)
    assert centre[0] == pytest.approx(x, abs=0.02)
    assert centre[1] == pytest.approx(y, abs=0.02)


@given(
    points=st.lists(st.tuples(COORDINATES, COORDINATES), max_size=12),
)
@settings(max_examples=100, deadline=None)
def test_waypoints_survive_being_written_into_an_attribute(
    points: list[tuple[float, float]],
) -> None:
    expected = tuple(points)
    produced = parse_points(format_points(expected))
    assert len(produced) == len(expected)
    for (want_x, want_y), (got_x, got_y) in zip(expected, produced, strict=True):
        assert got_x == pytest.approx(want_x)
        assert got_y == pytest.approx(want_y)


def test_a_malformed_waypoint_is_dropped_rather_than_raised_on() -> None:
    """This is read from a file a third-party editor rewrote."""
    assert parse_points("1,2 rubbish 3,4 5,") == ((1.0, 2.0), (3.0, 4.0))


# --------------------------------------------------------------------------- #
# The round trip is a no-op
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", [*EXAMPLE_TREES, "arranged", "fixture"])
def test_exporting_and_importing_an_untouched_diagram_changes_nothing(
    name: str, inventories: dict[str, Inventory]
) -> None:
    """The property the whole feature rests on.

    If a diagram that came home untouched proposed changes, nobody could trust
    the changes it proposed after a real edit. Asserted on the *operations*
    rather than on coordinates: a no-op is a statement about what would be
    written, and nothing would be.
    """
    assert round_trip(inventories[name]) == ()


@pytest.mark.parametrize(
    "view", ["physical", "l1", "l2", "l3", "overlay", "routing", "rack", "power", "identity"]
)
def test_the_round_trip_is_a_no_op_in_every_view(
    view: str, inventories: dict[str, Inventory]
) -> None:
    """Nine views, nine different node sets; the invariant is the same in each."""
    assert round_trip(inventories["campus"], view=view) == ()


@pytest.mark.parametrize("name", ["home-lab", "campus", "arranged"])
def test_the_round_trip_produces_an_empty_plan(
    name: str, inventories: dict[str, Inventory], tmp_path: Path
) -> None:
    """The end-to-end statement, through the real edit session and diff engine."""
    source = EXAMPLES / name if (EXAMPLES / name).is_dir() else ARRANGED
    root = tmp_path / name
    for path in sorted(source.rglob("*.yaml")):
        destination = root / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_text(destination, path.read_text(encoding="utf-8"))

    inventory = load_tree(root)
    diagram = parse_mxfile(run_export(inventory))
    result = reconcile(diagram, inventory, build_graph(inventory, layer=Layer.L1))
    session = EditSession(root=root)
    session.apply_all(result.operations)
    assert diff_states(session.baseline, session.inventory).empty
    assert not session.changes


# --------------------------------------------------------------------------- #
# The four gestures
# --------------------------------------------------------------------------- #


def edited() -> Diagram:
    return parse_mxfile((FIXTURES / "arranged-edited.drawio").read_text("utf-8"))


def test_the_hand_edited_fixture_produces_its_golden_plan(
    workspace: Path, regen_golden: bool
) -> None:
    """One fixture, four gestures, one committed plan.

    The fixture is the pristine export with a node moved, a label retyped, a
    node deleted and an edge drawn — the four things a draw.io user can do that
    netgraph acts on. Regenerate both with ``tools/gen_drawio_fixtures.py`` and
    this test with ``--regen-golden``.
    """
    inventory = load_tree(workspace)
    result = reconcile(edited(), inventory, build_graph(inventory, layer=Layer.L1))
    assert not result.failed
    assert (result.moved, result.renamed, result.deleted, result.connected) == (1, 1, 1, 1)

    session = EditSession(root=workspace)
    session.apply_all(result.operations)
    plan = diff_states(session.baseline, session.inventory)
    actual = _stable(render_plan(plan, "json"))

    golden = FIXTURES / "arranged-edited.plan.json"
    if regen_golden:
        write_text(golden, actual)
        pytest.skip(f"regenerated {golden.name}")
    assert actual == golden.read_text(encoding="utf-8"), (
        "the plan the edited diagram produces drifted from its golden. If the change is "
        "intended, rerun with --regen-golden and review the diff."
    )


#: The state digest names the temporary directory the workspace was copied to,
#: so it is elided from the golden. What it asserts — that a plan is only ever
#: applied to the tree it was made from — is ``netgraph apply``'s to test.
_DIGEST = re.compile(r'"hash": "[0-9a-f]+"')


def _stable(document: str) -> str:
    return _DIGEST.sub('"hash": "<digest>"', document)


def test_a_moved_cell_becomes_geometry_and_nothing_else(workspace: Path) -> None:
    inventory = load_tree(workspace)
    result = reconcile(
        edited(),
        inventory,
        build_graph(inventory, layer=Layer.L1),
        ReconcileOptions(renames=False, deletions=False, connections=False),
    )
    assert [type(operation) for operation in result.operations] == [SetGeometry]
    geometry = result.operations[0]
    assert isinstance(geometry, SetGeometry)
    assert geometry.nodes is not None
    # Every node keeps its entry: a set-geometry replaces the section, so a
    # partial one would drop the arrangement of everything that did not move.
    assert set(geometry.nodes) == set(inventory.layouts["layout"].spec.views["l1"].nodes)


def test_a_retyped_label_becomes_a_rename(workspace: Path) -> None:
    inventory = load_tree(workspace)
    result = reconcile(
        edited(),
        inventory,
        build_graph(inventory, layer=Layer.L1),
        ReconcileOptions(geometry=False, deletions=False, connections=False),
    )
    assert result.operations == (RenameElement(address="hosts/srv-app", new_name="srv-web"),)


def test_a_label_that_is_not_a_name_is_reported_rather_than_written(
    workspace: Path,
) -> None:
    """A stakeholder types a caption where a name was; netgraph does not oblige."""
    text = (FIXTURES / "arranged-edited.drawio").read_text("utf-8")
    text = text.replace('<object label="srv-web"', '<object label="the big one (rack 3)"')
    inventory = load_tree(workspace)
    result = reconcile(
        parse_mxfile(text),
        inventory,
        build_graph(inventory, layer=Layer.L1),
        ReconcileOptions(geometry=False, deletions=False, connections=False),
    )
    assert result.operations == ()
    assert any(
        "not a usable element name" in note.message for note in result.of_level(Level.WARNING)
    )


def test_a_label_that_collides_is_reported_rather_than_written(workspace: Path) -> None:
    text = (FIXTURES / "arranged-edited.drawio").read_text("utf-8")
    text = text.replace('<object label="srv-web"', '<object label="pc-1"')
    inventory = load_tree(workspace)
    result = reconcile(
        parse_mxfile(text),
        inventory,
        build_graph(inventory, layer=Layer.L1),
        ReconcileOptions(geometry=False, deletions=False, connections=False),
    )
    assert result.operations == ()
    assert any("already declared" in note.message for note in result.of_level(Level.WARNING))


def test_a_deleted_cell_becomes_a_cascading_delete(workspace: Path) -> None:
    inventory = load_tree(workspace)
    result = reconcile(
        edited(),
        inventory,
        build_graph(inventory, layer=Layer.L1),
        ReconcileOptions(geometry=False, renames=False, connections=False),
    )
    assert [operation.describe() for operation in result.operations] == [
        "delete hosts/pc-2 and everything that needs it"
    ]


def test_a_drawn_edge_becomes_a_cable_on_the_first_free_port(workspace: Path) -> None:
    inventory = load_tree(workspace)
    result = reconcile(
        edited(),
        inventory,
        build_graph(inventory, layer=Layer.L1),
        ReconcileOptions(geometry=False, renames=False, deletions=False),
    )
    assert [operation.describe() for operation in result.operations] == [
        "connect hosts/pc-1:eno1 to devices/sw-access:port3"
    ]


def test_an_edge_with_nowhere_to_land_is_reported_rather_than_forced(
    workspace: Path,
) -> None:
    """A cable on an occupied port is the mistake this whole tool exists to catch."""
    text = (FIXTURES / "arranged-l1.drawio").read_text("utf-8")
    ids = dict(re.findall(r'netgraph:name="([^"]+)"[^>]*id="(n-[^"]+)"', text))
    drawn = (
        f'<mxCell id="drawn-2" style="edgeStyle=none;" edge="1" parent="1" '
        f'source="{ids["devices/rtr-core"]}" target="{ids["devices/rtr-core"]}">'
        f'<mxGeometry relative="1" as="geometry" /></mxCell>'
    )
    inventory = load_tree(workspace)
    result = reconcile(
        parse_mxfile(text.replace("</root>", drawn + "</root>")),
        inventory,
        build_graph(inventory, layer=Layer.L1),
    )
    assert result.operations == ()
    assert any("both ends on one element" in note.message for note in result.notes)


def test_a_cell_netgraph_did_not_write_is_reported_and_left_alone(
    workspace: Path,
) -> None:
    """A legend, a note, an arrow: welcome on the canvas, absent from the tree."""
    text = (FIXTURES / "arranged-l1.drawio").read_text("utf-8")
    note = (
        '<mxCell id="note-1" value="please check this" style="text;html=1;" '
        'vertex="1" parent="1"><mxGeometry x="0" y="0" width="10" height="10" '
        'as="geometry" /></mxCell>'
    )
    inventory = load_tree(workspace)
    result = reconcile(
        parse_mxfile(text.replace("</root>", note + "</root>")),
        inventory,
        build_graph(inventory, layer=Layer.L1),
    )
    assert result.operations == ()
    assert result.unmapped == 1
    assert any("netgraph did not write" in note.message for note in result.notes)


# --------------------------------------------------------------------------- #
# Deletions, and the rule that keeps them safe
# --------------------------------------------------------------------------- #


def test_a_filtered_export_is_stamped_partial_and_deletes_nothing(
    inventories: dict[str, Inventory],
) -> None:
    """The failure mode of getting this wrong is deleting a site."""
    inventory = inventories["fixture"]
    payload = run_export(inventory, spec=FilterSpec(namespaces=("devices",)))
    diagram = parse_mxfile(payload)
    assert diagram.scope is Scope.PARTIAL

    result = reconcile(diagram, inventory, build_graph(inventory, layer=Layer.L1))
    assert result.deleted == 0
    assert result.operations == ()
    assert any("filtered view" in note.message for note in result.notes)


def test_an_unfiltered_export_is_stamped_complete(inventories: dict[str, Inventory]) -> None:
    assert parse_mxfile(run_export(inventories["fixture"])).scope is Scope.COMPLETE


def test_an_unreadable_scope_is_read_as_partial(inventories: dict[str, Inventory]) -> None:
    """Guessing the other way lets a corrupt attribute authorise a mass delete."""
    text = run_export(inventories["fixture"]).replace(
        'netgraph:scope="complete"', 'netgraph:scope="nonsense"'
    )
    assert parse_mxfile(text).scope is Scope.PARTIAL


def test_deletions_can_be_turned_off(workspace: Path) -> None:
    inventory = load_tree(workspace)
    result = reconcile(
        edited(),
        inventory,
        build_graph(inventory, layer=Layer.L1),
        ReconcileOptions(deletions=False),
    )
    assert result.deleted == 0


# --------------------------------------------------------------------------- #
# Staleness and versions
# --------------------------------------------------------------------------- #


def test_an_element_that_changed_under_the_diagram_is_reported_not_refused(
    workspace: Path,
) -> None:
    """Somebody else's commit landing must not lose the reviewer's work."""
    inventory = load_tree(workspace)
    diagram = parse_mxfile(run_export(inventory))
    write_text(
        workspace / "hosts" / "hosts.yaml",
        (workspace / "hosts" / "hosts.yaml")
        .read_text(encoding="utf-8")
        .replace("model: R350", "model: R450"),
    )
    changed = load_tree(workspace)
    result = reconcile(diagram, changed, build_graph(changed, layer=Layer.L1))
    assert not result.failed
    assert any("changed in the inventory" in note.message for note in result.of_level(Level.INFO))


def test_a_diagram_from_a_newer_netgraph_is_refused_with_a_sentence(
    inventories: dict[str, Inventory],
) -> None:
    inventory = inventories["fixture"]
    text = run_export(inventory).replace(
        f'netgraph:version="{MODEL_VERSION}"', 'netgraph:version="99"'
    )
    result = reconcile(parse_mxfile(text), inventory, build_graph(inventory, layer=Layer.L1))
    assert result.failed
    assert result.operations == ()


def test_a_cell_naming_an_element_that_is_gone_is_reported(workspace: Path) -> None:
    inventory = load_tree(workspace)
    diagram = parse_mxfile(run_export(inventory))
    write_text(
        workspace / "hosts" / "hosts.yaml",
        (workspace / "hosts" / "hosts.yaml").read_text(encoding="utf-8").split("---")[0],
    )
    shrunk = load_tree(workspace)
    result = reconcile(
        diagram,
        shrunk,
        build_graph(shrunk, layer=Layer.L1),
        ReconcileOptions(deletions=False),
    )
    assert any("not in the inventory any more" in note.message for note in result.notes)


# --------------------------------------------------------------------------- #
# A diagram netgraph did not write
# --------------------------------------------------------------------------- #

FOREIGN = """<?xml version="1.0" encoding="UTF-8"?>
<mxfile host="app.diagrams.net">
  <diagram id="hand-drawn" name="Page-1">
    <mxGraphModel dx="800" dy="600" grid="1">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="2" value="core-router" style="shape=mxgraph.cisco.routers.router;" vertex="1" parent="1">
          <mxGeometry x="40" y="40" width="60" height="60" as="geometry" />
        </mxCell>
        <mxCell id="3" value="access switch" style="rounded=0;whiteSpace=wrap;html=1;" vertex="1" parent="1">
          <mxGeometry x="200" y="40" width="120" height="60" as="geometry" />
        </mxCell>
        <mxCell id="4" value="" style="rounded=1;" vertex="1" parent="1">
          <mxGeometry x="380" y="40" width="120" height="60" as="geometry" />
        </mxCell>
        <mxCell id="5" style="edgeStyle=none;" edge="1" parent="1" source="2" target="3">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


def test_a_hand_drawn_diagram_is_read_and_reported_but_never_reconciled(
    inventories: dict[str, Inventory],
) -> None:
    """Inferring a device from a rectangle would put hardware nobody owns in the tree."""
    inventory = inventories["fixture"]
    diagram = parse_mxfile(FOREIGN)
    assert not diagram.is_netgraph

    result = reconcile(diagram, inventory, build_graph(inventory, layer=Layer.L1))
    assert result.operations == ()
    assert result.unmapped == 1  # the unlabelled rounded box, and only it
    messages = " ".join(note.message for note in result.notes)
    assert "not written by 'netgraph export drawio'" in messages
    assert "looks like a router" in messages
    assert "looks like a switch" in messages
    assert "docs/drawio.md" in messages


@pytest.mark.parametrize(
    ("style", "label", "expected"),
    [
        ("shape=mxgraph.cisco.routers.router;", "", "router"),
        ("shape=mxgraph.cisco.switches.workgroup_switch;", "", "switch"),
        ("rounded=0;", "file server", "server"),
        ("rounded=0;", "alice's laptop", "computer"),
        ("shape=mxgraph.rack.general.1u_rack_server;", "patch panel 1", "patchpanel"),
        ("rounded=0;", "a box", None),
        ("", "", None),
    ],
)
def test_a_kind_is_inferred_from_the_shape_and_the_label_or_not_at_all(
    style: str, label: str, expected: str | None
) -> None:
    assert infer_kind(Cell(id="x", style=style, label=label)) == expected


# --------------------------------------------------------------------------- #
# Reading a hostile file
# --------------------------------------------------------------------------- #


def test_a_document_type_declaration_is_refused_rather_than_expanded() -> None:
    """The billion-laughs family. These are files a third party sends."""
    bomb = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>\n'
        "<mxfile><diagram>&lol2;</diagram></mxfile>\n"
    )
    with pytest.raises(DrawioFormatError, match="document type or entity declaration"):
        parse_mxfile(bomb)


def test_an_oversized_document_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(DrawioFormatError, match="ceiling"):
        parse_mxfile("<mxfile>" + " " * (MAX_DOCUMENT_BYTES + 1) + "</mxfile>")


def test_a_deflate_bomb_is_refused_rather_than_inflated() -> None:
    import base64

    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    payload = base64.b64encode(
        compressor.compress(b"A" * (200 * 1024 * 1024)) + compressor.flush()
    ).decode("ascii")
    with pytest.raises(DrawioFormatError, match="inflates past"):
        decode_diagram(payload)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("<mxfile></mxfile>", "holds no <diagram>"),
        ("<mxfile><diagram></diagram></mxfile>", "nothing in it"),
        ("<mxfile><diagram>not base64!!</diagram></mxfile>", "not base64"),
        ("<html><body/></html>", "not a draw.io file"),
        ("<mxfile><diagram", "not well-formed XML"),
    ],
)
def test_a_file_that_is_not_a_diagram_is_refused_with_a_sentence(text: str, message: str) -> None:
    with pytest.raises(DrawioFormatError, match=re.escape(message)):
        parse_mxfile(text)


def test_a_namespace_declaration_an_editor_dropped_is_put_back(
    inventories: dict[str, Inventory],
) -> None:
    """An unbound prefix is a spelling problem, not a corrupted diagram."""
    text = run_export(inventories["fixture"])
    stripped = text.replace(f' xmlns:netgraph="{_namespace_uri(text)}"', "")
    assert "xmlns:netgraph" not in stripped
    assert parse_mxfile(stripped).cells == parse_mxfile(text).cells


def _namespace_uri(text: str) -> str:
    match = re.search(r'xmlns:netgraph="([^"]+)"', text)
    assert match is not None
    return match[1]


def test_a_bare_mxgraphmodel_is_read(inventories: dict[str, Inventory]) -> None:
    """draw.io writes one depending on how the file was saved."""
    text = run_export(inventories["fixture"])
    model = text[text.index("<mxGraphModel") : text.index("</mxGraphModel>") + 15]
    assert parse_mxfile(model).of_role(CellRole.NODE)


# --------------------------------------------------------------------------- #
# Small units
# --------------------------------------------------------------------------- #


def test_a_cell_id_is_derived_readable_and_collision_free() -> None:
    assert cell_id("n", "sites/hq") != cell_id("n", "sites-hq")
    assert cell_id("n", "sites/hq") == cell_id("n", "sites/hq")
    assert cell_id("n", "sites/hq").startswith("n-sites-hq-")
    assert cell_id("n", "").startswith("n-x-")


def test_a_content_hash_depends_on_what_a_document_says_not_how_it_is_written() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})
    assert content_hash({}).startswith("sha256:")


def test_a_data_uri_uses_the_comma_form_drawio_stores() -> None:
    """A semicolon inside a style value would end the declaration."""
    assert data_uri("image/png", b"\x00") == "data:image/png,AA=="


def test_an_icon_past_the_size_bound_is_not_inlined(tmp_path: Path) -> None:
    theme_dir = tmp_path / "theme"
    theme_dir.mkdir()
    (theme_dir / "router.png").write_bytes(b"\x00" * (600 * 1024))
    theme = icon_theme(str(theme_dir))
    assert theme is not None
    assert icon_data_uri(theme, "router") is None


def test_the_metadata_cell_is_invisible_locked_and_singular(
    inventories: dict[str, Inventory],
) -> None:
    """A user who deletes it loses the round trip, so it takes effort to."""
    diagram = parse_mxfile(run_export(inventories["fixture"]))
    metadata = diagram.of_role(CellRole.METADATA)
    assert len(metadata) == 1
    assert not metadata[0].visible
    assert "locked=1" in metadata[0].style
    assert "deletable=0" in metadata[0].style


def test_a_diagram_with_no_metadata_cell_is_not_one_of_netgraphs() -> None:
    assert not Diagram(cells=(Cell(id="a", role=CellRole.NODE),)).is_netgraph


def test_the_builder_is_usable_without_a_notes_accumulator(
    inventories: dict[str, Inventory],
) -> None:
    inventory = inventories["fixture"]
    diagram = build_diagram(
        build_graph(inventory, layer=Layer.L1), inventory, BuildOptions(view="l1")
    )
    assert write_mxfile(diagram).startswith("<?xml")


# --------------------------------------------------------------------------- #
# The commands
# --------------------------------------------------------------------------- #


def test_the_export_command_writes_a_diagram_and_a_manifest(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "out.drawio"
    result = invoke(
        runner,
        "-i",
        str(FIXTURE_INVENTORY),
        "export",
        "drawio",
        "--view",
        "l1",
        "-o",
        str(target),
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert parse_mxfile(target.read_text(encoding="utf-8")).view == "l1"


def test_a_drawio_only_flag_is_refused_on_another_format() -> None:
    """A flag that is quietly dropped is worse than one that errors."""
    runner = CliRunner()
    result = invoke(runner, "-i", str(FIXTURE_INVENTORY), "export", "hosts", "--view", "l2")
    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert "--view applies to 'drawio'" in result.output  # type: ignore[attr-defined]


def test_the_import_command_shows_a_changeset_and_writes_nothing_on_a_dry_run(
    workspace: Path,
) -> None:
    runner = CliRunner()
    before = (workspace / "layout.yaml").read_text(encoding="utf-8")
    result = invoke(
        runner,
        "-i",
        str(workspace),
        "import",
        "drawio",
        str(FIXTURES / "arranged-edited.drawio"),
        "--dry-run",
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "1 moved, 1 renamed, 1 deleted, 1 newly connected" in result.output  # type: ignore[attr-defined]
    assert (workspace / "layout.yaml").read_text(encoding="utf-8") == before


def test_the_import_command_applies_what_was_confirmed(workspace: Path) -> None:
    runner = CliRunner()
    result = invoke(
        runner,
        "-i",
        str(workspace),
        "import",
        "drawio",
        str(FIXTURES / "arranged-edited.drawio"),
        "--auto-approve",
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    after = load_tree(workspace)
    assert "hosts/srv-web" in after
    assert "hosts/pc-2" not in after
    assert after.layouts["layout"].spec.views["l1"].nodes["hosts/pc-1"].position.x == 697.0


def test_importing_an_untouched_export_through_the_command_changes_nothing(
    workspace: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    diagram = tmp_path / "pristine.drawio"
    invoke(runner, "-i", str(workspace), "export", "drawio", "-o", str(diagram))
    result = invoke(runner, "-i", str(workspace), "import", "drawio", str(diagram))
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "nothing to change" in result.output  # type: ignore[attr-defined]


def test_the_legacy_import_signature_still_works(tmp_path: Path) -> None:
    """``netgraph import caps/*.json`` is in everybody's shell history."""
    runner = CliRunner()
    capture = tmp_path / "sw-1.csv"
    capture.write_text("sw-1,port1,pc-1,eno1\n", encoding="utf-8")
    result = invoke(runner, "import", "--dry-run", "-o", str(tmp_path / "out"), str(capture))
    assert "kind: cable" in result.output  # type: ignore[attr-defined]


def test_a_view_that_contradicts_the_file_is_a_usage_error(tmp_path: Path) -> None:
    runner = CliRunner()
    result = invoke(
        runner,
        "-i",
        str(FIXTURE_INVENTORY),
        "import",
        "drawio",
        str(FIXTURES / "arranged-l1.drawio"),
        "--view",
        "l3",
    )
    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert "cannot be imported as the l3 one" in result.output  # type: ignore[attr-defined]


def test_a_file_that_is_not_a_diagram_exits_three(tmp_path: Path) -> None:
    runner = CliRunner()
    rubbish = tmp_path / "not.drawio"
    rubbish.write_text("hello", encoding="utf-8")
    result = invoke(runner, "-i", str(FIXTURE_INVENTORY), "import", "drawio", str(rubbish))
    assert result.exit_code == 3  # type: ignore[attr-defined]


def test_the_import_reports_as_json_when_asked(workspace: Path) -> None:
    runner = CliRunner()
    result = invoke(
        runner,
        "-i",
        str(workspace),
        "import",
        "drawio",
        str(FIXTURES / "arranged-edited.drawio"),
        "--dry-run",
        "--json",
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    documents = list(_json_documents(result.output))  # type: ignore[attr-defined]
    assert documents and isinstance(documents[0], list)
    assert all("level" in entry for entry in documents[0])


def _json_documents(output: str) -> Iterator[object]:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(output):
        while index < len(output) and output[index] not in "[{":
            index += 1
        if index >= len(output):
            return
        value, index = decoder.raw_decode(output, index)
        yield value
