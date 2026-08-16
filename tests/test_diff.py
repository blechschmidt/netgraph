"""``netviz diff``: the changeset, drawn.

Three layers of assertion, in order of how much they pin down:

**The join.** :mod:`netviz.diff` translates a changeset keyed by *address*
into marks keyed by node and edge *id*. Most of what can go wrong here is a
mistranslation — a subnet membership marked amber because the host's model
string changed, a rename drawn as a deletion beside a creation — so those are
asserted one at a time.

**The drawing.** That the DOT backend paints the marks: green fill on a created
device, a dashed red outline on a removed one, a badge naming the fields that
moved. Asserted against attributes rather than against a whole file, so a
harmless change to an unrelated label does not fail here.

**The goldens.** Byte-for-byte snapshots in ``tests/fixtures/golden/diff/``,
regenerated with::

    pytest tests/test_diff.py --regen-golden

The two fixture trees they compare against are described in
``tests/fixtures/diff/README.md``. One of them keeps a stale ``layout.yaml`` on
purpose, because "a removed node keeps its persisted geometry" is a promise a
snapshot is the right way to hold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from netviz.cli import cli
from netviz.diff import Drawing, draw, renamed_addresses, updated_fields
from netviz.fsio import write_text
from netviz.loader import Inventory, load_tree
from netviz.plan import diff as diff_states
from netviz.plan.address import parse_address
from netviz.render import Layer, RenderOptions, build_graph, render_text, suffix_for
from netviz.render.diffview import DiffOverlay, Mark, diff_overlay, union_graph

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden" / "diff"

HOME_LAB = EXAMPLES / "home-lab"
PROPOSED = FIXTURES / "diff" / "home-lab-proposed"
ARRANGED = FIXTURES / "arranged"
ARRANGED_PROPOSED = FIXTURES / "diff" / "arranged-proposed"

#: The text formats a diff golden is kept for. Mermaid is deliberately absent:
#: it cannot express a mark, and :func:`netviz.render.supports_diff` says so.
FORMATS = ("dot", "json")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def trees() -> dict[str, Inventory]:
    """Every tree the comparisons need, loaded once."""
    loaded = {}
    for root in (HOME_LAB, PROPOSED, ARRANGED, ARRANGED_PROPOSED):
        inventory = load_tree(root)
        assert inventory.errors == [], f"{root} does not load cleanly: {inventory.errors}"
        loaded[root.name] = inventory
    return loaded


def drawing_of(
    trees: dict[str, Inventory], before: str, after: str, layer: Layer = Layer.L1
) -> Drawing:
    """The diff of two loaded trees at one layer, as the command builds it."""
    plan = diff_states(trees[before], trees[after])
    return draw(
        plan,
        build_graph(trees[before], layer=layer),
        build_graph(trees[after], layer=layer),
    )


@pytest.fixture(scope="module")
def home_lab(trees: dict[str, Inventory]) -> Drawing:
    return drawing_of(trees, "home-lab", "home-lab-proposed")


# --------------------------------------------------------------------------- #
# The join: a changeset keyed by address becomes marks keyed by drawn id
# --------------------------------------------------------------------------- #


def test_a_created_element_and_its_cable_are_both_added(home_lab: Drawing) -> None:
    assert home_lab.overlay.node("hosts/pc-new") is Mark.ADDED
    assert home_lab.overlay.edge("cables/cbl-sw-new") is Mark.ADDED


def test_a_deleted_element_is_still_drawn(home_lab: Drawing) -> None:
    """The point of the whole feature: a deletion has to be *on* the diagram."""
    assert "hosts/srv-nas" in home_lab.graph.nodes
    assert home_lab.overlay.node("hosts/srv-nas") is Mark.REMOVED
    assert home_lab.overlay.edge("cables/cbl-sw-nas") is Mark.REMOVED


def test_an_updated_element_names_the_fields_that_moved(home_lab: Drawing) -> None:
    assert home_lab.overlay.node("hosts/pc-desk") is Mark.CHANGED
    assert home_lab.overlay.fields["hosts/pc-desk"] == ("spec.model",)
    assert home_lab.overlay.badge("hosts/pc-desk") == "spec.model"


def test_a_rename_is_one_box_and_not_two(home_lab: Drawing) -> None:
    """Two boxes would say a device was swapped, which is not what happened."""
    assert "wireless/ap-home" not in home_lab.graph.nodes
    assert home_lab.overlay.node("wireless/ap-attic") is Mark.CHANGED
    assert home_lab.overlay.badge("wireless/ap-attic") == "was wireless/ap-home"


def test_an_untouched_element_carries_no_mark(home_lab: Drawing) -> None:
    assert home_lab.overlay.node("hosts/laptop") is Mark.UNCHANGED
    assert "hosts/laptop" not in home_lab.overlay.nodes


def test_a_derived_node_is_marked_by_presence(trees: dict[str, Inventory]) -> None:
    """Nothing declares ``subnet:…``, so only the two drawings can answer for it."""
    layer3 = drawing_of(trees, "home-lab", "home-lab-proposed", Layer.L3)
    assert layer3.overlay.edge("hosts/pc-new:eth0#192.168.10.0/24") is Mark.ADDED
    assert layer3.overlay.edge("hosts/srv-nas:eth0#192.168.10.0/24") is Mark.REMOVED


def test_a_subnet_membership_does_not_inherit_its_hosts_field_change(
    trees: dict[str, Inventory],
) -> None:
    """``spec.model`` moved on the device, not on what it is plugged into.

    A membership is keyed ``host:eth0#10.0.0.0/24``, which starts with the
    device's address; marking it amber for the device's change would claim the
    network moved when only a label did.
    """
    layer3 = drawing_of(trees, "home-lab", "home-lab-proposed", Layer.L3)
    assert layer3.overlay.edge("hosts/pc-desk:eno1#192.168.10.0/24") is Mark.UNCHANGED


def test_a_cable_whose_endpoint_was_renamed_is_changed_not_replaced(
    home_lab: Drawing,
) -> None:
    """Its own document moved — ``spec.endpoints`` now names the new host."""
    assert home_lab.overlay.edge("cables/cbl-sw-ap") is Mark.CHANGED
    assert home_lab.overlay.fields["cables/cbl-sw-ap"] == ("spec.endpoints",)


def test_a_layout_change_marks_nothing(trees: dict[str, Inventory]) -> None:
    """Dragging a node is not a change to the network it draws."""
    plan = diff_states(trees["arranged"], trees["arranged"])
    assert updated_fields(plan) == {}
    assert renamed_addresses(plan) == {}


def test_an_annotation_change_marks_nothing_even_under_a_device_name() -> None:
    """A note is presentational, and its name space is not the elements'.

    Every §21 kind has its own name space, so a note called ``core`` may sit
    beside a switch called ``core``. Keying the marks by fully-qualified name
    would let the note's edit paint the switch amber — claiming the network
    moved because somebody reworded a callout — which is exactly the leak an
    annotation is barred from causing. Built by hand rather than loaded: the
    point is what :mod:`netviz.diff` does with the address *type*.
    """
    from netviz.plan.model import Action, Change, Plan

    plan = Plan(
        changes=(
            Change(action=Action.UPDATE, address=parse_address("note.hosts/pc-desk"), kind="note"),
            Change(action=Action.UPDATE, address=parse_address("area.hosts/pc-desk"), kind="area"),
            Change(
                action=Action.UPDATE,
                address=parse_address("legend.hosts/pc-desk"),
                kind="legend",
            ),
            Change(
                action=Action.RENAME,
                address=parse_address("note.hosts/pc-desk"),
                new_address=parse_address("note.hosts/pc-new"),
                kind="note",
            ),
        )
    )

    assert updated_fields(plan) == {}
    assert renamed_addresses(plan) == {}


def test_an_element_change_beside_an_annotation_is_still_marked() -> None:
    """The skip is by address type, not a blanket silence on the name."""
    from netviz.plan.model import Action, Change, Plan

    plan = Plan(
        changes=(
            Change(action=Action.UPDATE, address=parse_address("note.hosts/pc-desk"), kind="note"),
            Change(
                action=Action.UPDATE, address=parse_address("device.hosts/pc-desk"), kind="computer"
            ),
        )
    )

    assert updated_fields(plan) == {"hosts/pc-desk": ()}


def test_an_unchanged_pair_produces_an_empty_overlay(trees: dict[str, Inventory]) -> None:
    drawing = drawing_of(trees, "home-lab", "home-lab")
    assert drawing.is_empty
    assert drawing.overlay.summary() == "no visible change"
    assert set(drawing.graph.nodes) == set(build_graph(trees["home-lab"]).nodes)


def test_the_union_keeps_the_after_state_first(trees: dict[str, Inventory]) -> None:
    """So that a diff of an unchanged tree draws like a plain render of it."""
    after = build_graph(trees["home-lab-proposed"])
    union = union_graph(build_graph(trees["home-lab"]), after)
    assert list(union.nodes)[: len(after.nodes)] == list(after.nodes)


def test_every_union_edge_has_both_its_endpoints(home_lab: Drawing) -> None:
    for edge in home_lab.graph.edges:
        assert edge.source in home_lab.graph.nodes
        assert edge.target in home_lab.graph.nodes


# --------------------------------------------------------------------------- #
# Geometry: a removal must not reshuffle the picture
# --------------------------------------------------------------------------- #


def test_a_removed_node_keeps_its_persisted_position(trees: dict[str, Inventory]) -> None:
    arranged = drawing_of(trees, "arranged", "arranged-proposed")
    assert arranged.overlay.node("hosts/srv-nas") is Mark.REMOVED
    placed = arranged.graph.geometry.nodes["hosts/srv-nas"]
    stored = build_graph(trees["arranged"]).geometry.nodes["hosts/srv-nas"]
    assert (placed.x, placed.y) == (stored.x, stored.y)


def test_every_node_of_an_arranged_diff_is_placed(trees: dict[str, Inventory]) -> None:
    """Otherwise the drawing falls back to a free layout and reshuffles wholesale."""
    arranged = drawing_of(trees, "arranged", "arranged-proposed")
    unplaced = set(arranged.graph.nodes) - set(arranged.graph.geometry.nodes)
    assert unplaced == set()


# --------------------------------------------------------------------------- #
# The drawing
# --------------------------------------------------------------------------- #


def dot_line(text: str, ident: str) -> str:
    """The one DOT statement declaring ``ident``."""
    matches = [line for line in text.splitlines() if line.strip().startswith(f'"{ident}" [')]
    assert len(matches) == 1, f"expected one statement for {ident}, found {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module")
def home_lab_dot(home_lab: Drawing) -> str:
    return render_text(home_lab.graph, "dot", RenderOptions(diff=home_lab.overlay))


def test_an_added_node_is_drawn_green(home_lab_dot: str) -> None:
    line = dot_line(home_lab_dot, "hosts/pc-new")
    assert 'color="#15803d"' in line
    assert 'fillcolor="#dcfce7"' in line
    assert "+ added" in home_lab_dot


def test_a_removed_node_is_drawn_red_and_dashed(home_lab_dot: str) -> None:
    """Dashed as well as red, so the picture survives a greyscale print."""
    line = dot_line(home_lab_dot, "hosts/srv-nas")
    assert 'color="#dc2626"' in line
    assert 'style="filled,dashed"' in line


def test_a_changed_node_is_amber_and_badged(home_lab_dot: str) -> None:
    assert 'color="#b45309"' in dot_line(home_lab_dot, "hosts/pc-desk")
    # The badge is a row of the node's HTML label, on a line of its own.
    assert "~ spec.model" in home_lab_dot


def test_an_untouched_node_is_faded(home_lab_dot: str) -> None:
    line = dot_line(home_lab_dot, "hosts/laptop")
    assert 'color="#d4d4d8"' in line
    assert 'fillcolor="#fafafa"' in line


def test_a_removed_link_is_dashed_whatever_it_is_made_of(home_lab_dot: str) -> None:
    removed = [line for line in home_lab_dot.splitlines() if "- removed" in line and "--" in line]
    assert removed and all("style=dashed" in line for line in removed)


def test_a_rendering_without_a_diff_is_untouched(trees: dict[str, Inventory]) -> None:
    """No overlay means byte-identical to what every release before this drew."""
    graph = build_graph(trees["home-lab"])
    assert render_text(graph, "dot", RenderOptions()) == render_text(graph, "dot", None)


# --------------------------------------------------------------------------- #
# The JSON form
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def home_lab_json(home_lab: Drawing) -> dict:
    return json.loads(render_text(home_lab.graph, "json", RenderOptions(diff=home_lab.overlay)))


def test_json_carries_the_changeset_beside_the_graph(home_lab_json: dict) -> None:
    """Two documents that could be paired wrongly is what this avoids."""
    assert home_lab_json["changeset"]["summary"] == {
        "create": 2,
        "update": 3,
        "delete": 2,
        "rename": 1,
        "total": 8,
    }
    assert home_lab_json["diff"]["counts"]["added"] == 2


def test_every_json_node_says_what_happened_to_it(home_lab_json: dict) -> None:
    """Untouched ones included: absence must not mean two different things."""
    marks = {node["id"]: node["diff"]["mark"] for node in home_lab_json["nodes"]}
    assert marks["hosts/pc-new"] == "added"
    assert marks["hosts/srv-nas"] == "removed"
    assert marks["hosts/laptop"] == "unchanged"


def test_json_names_the_fields_and_the_old_address(home_lab_json: dict) -> None:
    nodes = {node["id"]: node["diff"] for node in home_lab_json["nodes"]}
    assert nodes["hosts/pc-desk"]["fields"] == ["spec.model"]
    assert nodes["wireless/ap-attic"]["renamedFrom"] == "wireless/ap-home"


def test_json_without_a_diff_carries_no_diff_keys(trees: dict[str, Inventory]) -> None:
    document = json.loads(render_text(build_graph(trees["home-lab"]), "json", RenderOptions()))
    assert "diff" not in document
    assert "changeset" not in document
    assert all("diff" not in node for node in document["nodes"])


# --------------------------------------------------------------------------- #
# The overlay's own reporting
# --------------------------------------------------------------------------- #


def test_a_badge_counts_what_it_cannot_spell_out() -> None:
    overlay = DiffOverlay(
        nodes={"a": Mark.CHANGED},
        fields={"a": ("spec.one", "spec.two", "spec.three", "spec.four")},
    )
    assert overlay.badge("a") == "spec.one, spec.two, spec.three +1 more"


def test_an_empty_overlay_is_not_the_same_as_no_overlay() -> None:
    """It means "nothing moved", and a backend honouring it dims everything."""
    assert DiffOverlay().is_empty
    assert DiffOverlay().node("anything") is Mark.UNCHANGED


def test_the_overlay_serialises_to_plain_data(home_lab: Drawing) -> None:
    payload = home_lab.overlay.to_dict()
    assert payload["nodes"]["hosts/srv-nas"] == "removed"
    assert payload["fields"]["hosts/pc-desk"] == ["spec.model"]
    assert payload["renamedFrom"]["wireless/ap-attic"] == "wireless/ap-home"


def test_diff_overlay_needs_no_plan_at_all(trees: dict[str, Inventory]) -> None:
    """Presence alone is a usable overlay; the plan only adds the fine detail."""
    overlay = diff_overlay(build_graph(trees["home-lab"]), build_graph(trees["home-lab-proposed"]))
    assert overlay.node("hosts/pc-new") is Mark.ADDED
    assert overlay.fields == {}


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def run(*args: str) -> Result:
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def test_the_command_draws_two_folders() -> None:
    result = run("-i", str(PROPOSED), "diff", "--from", str(HOME_LAB), "-f", "dot")
    assert result.exit_code == 0
    assert "+ added" in result.stdout
    assert "2 added, 4 changed, 2 removed" in result.stderr


def test_the_command_refuses_a_format_that_cannot_say_what_changed() -> None:
    result = run("-i", str(PROPOSED), "diff", "--from", str(HOME_LAB), "-f", "mermaid")
    assert result.exit_code != 0
    assert "no way to say what changed" in result.stderr


def test_the_command_refuses_two_layers() -> None:
    result = run(
        "-i", str(PROPOSED), "diff", "--from", str(HOME_LAB), "--layer", "l1", "--layer", "l3"
    )
    assert result.exit_code != 0
    assert "a diff compares one view" in result.stderr


def test_the_command_needs_something_to_compare_against() -> None:
    result = run("-i", str(PROPOSED), "diff")
    assert result.exit_code != 0
    assert "nothing to compare against" in result.stderr


def test_against_and_from_are_the_same_side() -> None:
    result = run("-i", str(PROPOSED), "diff", "--against", "HEAD", "--from", str(HOME_LAB))
    assert result.exit_code != 0
    assert "same side of the diff" in result.stderr


def test_the_command_says_when_nothing_changed() -> None:
    result = run("-i", str(HOME_LAB), "diff", "--from", str(HOME_LAB))
    assert result.exit_code == 0
    assert "nothing changed" in result.stderr


def test_target_narrows_what_is_marked() -> None:
    result = run(
        "-i", str(PROPOSED), "diff", "--from", str(HOME_LAB), "--target", "device.hosts/pc-new"
    )
    assert result.exit_code == 0
    assert "diff at layer l1: 1 added" in result.stderr
    assert "removed" not in result.stderr


# --------------------------------------------------------------------------- #
# The goldens
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    """One comparison, rendered under one set of display options."""

    name: str
    before: Path
    after: Path
    layer: Layer
    options: RenderOptions = field(default_factory=RenderOptions)

    def golden(self, format: str) -> Path:
        return GOLDEN_DIR / f"{self.name}{suffix_for(format)}"


CASES = (
    Case(
        # The whole vocabulary in one picture: two creations, three updates, a
        # rename and two deletions, over the example the README already draws.
        name="home-lab-l1",
        before=HOME_LAB,
        after=PROPOSED,
        layer=Layer.L1,
        options=RenderOptions(title="Home lab, proposed"),
    ),
    Case(
        # The same change at layer 3, where the marks on the derived nodes come
        # from presence rather than from the changeset.
        name="home-lab-l3",
        before=HOME_LAB,
        after=PROPOSED,
        layer=Layer.L3,
        options=RenderOptions(title="Home lab, proposed, layer 3"),
    ),
    Case(
        # A stored arrangement with a deletion in it. This golden is the promise
        # that a removed node keeps its coordinates: ``hosts/srv-nas`` carries a
        # ``pos`` here, and the drawing is ``mode=fixed``.
        name="arranged-l1",
        before=ARRANGED,
        after=ARRANGED_PROPOSED,
        layer=Layer.L1,
    ),
)


def render_case(case: Case, format: str) -> str:
    plan = diff_states(load_tree(case.before), load_tree(case.after))
    drawing = draw(
        plan,
        build_graph(load_tree(case.before), layer=case.layer),
        build_graph(load_tree(case.after), layer=case.layer),
    )
    return render_text(drawing.graph, format, replace(case.options, diff=drawing.overlay))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("format", FORMATS)
def test_the_diff_matches_its_golden_file(case: Case, format: str, regen_golden: bool) -> None:
    actual = render_case(case, format)
    golden = case.golden(format)
    if regen_golden:
        golden.parent.mkdir(parents=True, exist_ok=True)
        write_text(golden, actual)
        pytest.skip(f"regenerated {golden.name}")
    assert golden.exists(), (
        f"missing golden {golden.relative_to(REPO_ROOT)}; "
        f"create it with 'pytest tests/test_diff.py --regen-golden'"
    )
    assert actual == golden.read_text(encoding="utf-8"), (
        f"the {format} diff of {case.name} drifted from its golden file. "
        f"If the change is intended, rerun with --regen-golden and review the diff."
    )


def test_no_stray_diff_goldens() -> None:
    """Every committed diff golden belongs to a case."""
    expected = {case.golden(format) for case in CASES for format in FORMATS}
    actual = {path for path in GOLDEN_DIR.iterdir() if path.is_file() and path.suffix != ".md"}
    assert actual == expected


def test_diff_goldens_are_free_of_machine_specific_paths() -> None:
    for case in CASES:
        for format in FORMATS:
            text = case.golden(format).read_text(encoding="utf-8")
            assert str(REPO_ROOT) not in text
            assert "/root/" not in text


def test_the_arranged_golden_places_the_removed_node() -> None:
    """The promise, held as a snapshot rather than only as an assertion."""
    document = json.loads((GOLDEN_DIR / "arranged-l1.json").read_text(encoding="utf-8"))
    assert document["layout"]["mode"] == "fixed"
    removed = next(node for node in document["nodes"] if node["id"] == "hosts/srv-nas")
    assert removed["diff"]["mark"] == "removed"
    assert "layout" in removed
