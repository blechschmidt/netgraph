"""Batches, and the tidying gestures that need one.

Two things are asserted here and nowhere else.

**A batch is all-or-nothing.** ``netgraph.edit`` was already atomic per
operation — an applier that refuses puts back what it touched — and that is the
wrong grain for a selection. Deleting eleven switches when the seventh cannot go
must leave the other ten alone, and the assertion below is the strong form of
it: the tree's *bytes* are compared before and after the refusal, so a rollback
that restored the meaning but rewrote a comment would fail.

**A tidying is one reviewable diff.** Aligning a row of switches touches one
``kind: layout`` document per file that holds one of them, writes only the
entries that moved, and comes back as a single change to undo. The tests here
read the resulting YAML rather than the objects, because "the diff is
reviewable" is a claim about the file.
"""

from __future__ import annotations

import shutil
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from netgraph.config import parse_config
from netgraph.edit import (
    ARRANGEMENTS,
    Batch,
    CreateElement,
    DeleteElement,
    EditError,
    EditSession,
    SetField,
    UnsetField,
    arrange_operations,
    describe_arrangement,
)
from netgraph.edit.batch import describe
from netgraph.loader import load_tree

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
EXAMPLES: Final = REPO_ROOT / "examples"

#: Four switches in a row, deliberately ragged: no two share an x, no two share
#: a y, and the sizes differ so that "align left" is a claim about *edges* and
#: not about centres. Positions are points, ``y`` upwards, centre-anchored.
RAGGED: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: layout
metadata:
  name: default
spec:
  views:
    physical:
      nodes:
        rtr-home: {position: {x: 10, y: 300}, size: {width: 100, height: 40}}
        sw-home: {position: {x: 34, y: 200}, size: {width: 60, height: 40}}
        pc-desk: {position: {x: 61, y: 100}, size: {width: 40, height: 40}}
        srv-nas: {position: {x: 97, y: 20}, size: {width: 80, height: 40}}
"""


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A writable copy of ``examples/home-lab``."""
    root = tmp_path / "home-lab"
    shutil.copytree(EXAMPLES / "home-lab", root)
    return root


@pytest.fixture()
def arranged(home: Path) -> Path:
    """The same tree with a hand-written arrangement of four of its devices."""
    (home / "layout.yaml").write_text(RAGGED, encoding="utf-8", newline="\n")
    return home


def snapshot(root: Path) -> dict[str, bytes]:
    """Every YAML file below ``root``, as bytes, keyed by relative path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.yaml"))
    }


def positions(root: Path, view: str = "physical") -> dict[str, tuple[float, float]]:
    """What the layout document on disk says, read back as plain YAML."""
    document = yaml.safe_load((root / "layout.yaml").read_text(encoding="utf-8"))
    found: dict[str, tuple[float, float]] = {}
    for key, entry in document["spec"]["views"][view]["nodes"].items():
        point = entry["position"]
        pair = point if isinstance(point, list) else (point["x"], point["y"])
        found[key] = (float(pair[0]), float(pair[1]))
    return found


def tidy(root: Path, command: str, addresses: list[str], **kwargs: Any) -> tuple[Any, ...]:
    """Run one arrangement against the tree on disk and commit what it produces."""
    session = EditSession(root=root)
    operations = arrange_operations(
        session.inventory, command=command, view="physical", addresses=addresses, **kwargs
    )
    if not operations:
        return ()
    batch = Batch(session)
    batch.apply(operations)
    batch.commit()
    return operations


# --------------------------------------------------------------------------- #
# A batch is all-or-nothing
# --------------------------------------------------------------------------- #


def test_a_batch_applies_every_operation_as_one_change(home: Path) -> None:
    session = EditSession(root=home)
    batch = Batch(session, label="two fields")
    result = batch.apply(
        [
            SetField(address="pc-desk", path="spec.model", value="NUC"),
            SetField(address="sw-home", path="spec.model", value="GS308"),
        ]
    )

    assert result.label == "two fields"
    assert len(result.applied) == 2
    # Two documents, so two files, and one change carrying both.
    assert result.files == ("hosts/pc-desk.yaml", "switches/sw-home.yaml")
    written = batch.commit().written
    assert written == result.files
    assert "NUC" in (home / "hosts" / "pc-desk.yaml").read_text(encoding="utf-8")
    assert "GS308" in (home / "switches" / "sw-home.yaml").read_text(encoding="utf-8")


def test_a_refused_operation_puts_back_every_byte_the_earlier_ones_wrote(home: Path) -> None:
    """The whole point. Two good operations, then one that cannot be applied."""
    before = snapshot(home)
    session = EditSession(root=home)
    batch = Batch(session)

    with pytest.raises(EditError) as raised:
        batch.apply(
            [
                SetField(address="pc-desk", path="spec.model", value="NUC"),
                SetField(address="sw-home", path="spec.model", value="GS308"),
                SetField(address="pc-ghost", path="spec.model", value="nothing"),
            ]
        )

    # Which one refused, said in the message: "operation 3 of 3" is a different
    # thing to be told from "there is no element called 'pc-ghost'".
    assert "operation 3 of 3" in str(raised.value)
    assert "Nothing in this change was applied" in str(raised.value)
    assert session.changes == {}, "the tree still holds the first two operations"
    assert not session.applied, "and still counts them as applied"
    session.commit()
    assert snapshot(home) == before


def test_a_rolled_back_batch_leaves_the_session_usable(home: Path) -> None:
    """A refusal is not a poisoned session: the next batch must work."""
    session = EditSession(root=home)
    with pytest.raises(EditError):
        Batch(session).apply(
            [
                SetField(address="pc-desk", path="spec.model", value="NUC"),
                DeleteElement(address="sw-home"),  # cables terminate on it
            ]
        )

    result = Batch(session).apply([SetField(address="pc-desk", path="spec.model", value="NUC")])
    assert result.files == ("hosts/pc-desk.yaml",)
    assert len(session.applied) == 1


def test_a_rollback_restores_a_file_the_batch_created(home: Path) -> None:
    """The file a create wrote must not survive the batch that wrote it."""
    before = snapshot(home)
    session = EditSession(root=home)
    with pytest.raises(EditError):
        Batch(session).apply(
            [
                CreateElement(kind="switch", name="sw-new"),
                CreateElement(kind="switch", name="sw-home"),  # the name is taken
            ]
        )
    assert session.changes == {}
    session.commit()
    assert snapshot(home) == before


def test_a_rollback_restores_a_file_the_batch_deleted(home: Path) -> None:
    before = snapshot(home)
    session = EditSession(root=home)
    with pytest.raises(EditError):
        Batch(session).apply(
            [
                DeleteElement(address="phone", cascade=True),
                DeleteElement(address="pc-ghost"),
            ]
        )
    assert session.changes == {}
    session.commit()
    assert snapshot(home) == before


def test_one_refused_operation_keeps_its_own_exception(home: Path) -> None:
    """A batch of one is that operation, so its refusal must not be re-wrapped.

    ``CascadeRequired`` carries the dependents the caller has to be shown; a
    generic ``EditError`` around it would throw them away for no gain.
    """
    from netgraph.edit import CascadeRequired

    session = EditSession(root=home)
    with pytest.raises(CascadeRequired) as raised:
        Batch(session).apply([DeleteElement(address="sw-home")])
    assert raised.value.dependents


def test_the_inverse_of_a_batch_undoes_the_whole_of_it(home: Path) -> None:
    before = snapshot(home)
    session = EditSession(root=home)
    batch = Batch(session)
    result = batch.apply(
        [
            SetField(address="pc-desk", path="spec.model", value="NUC"),
            SetField(address="sw-home", path="spec.model", value="GS308"),
        ]
    )
    batch.commit()
    assert snapshot(home) != before

    undo = EditSession(root=home)
    Batch(undo).apply(result.inverse).operations  # noqa: B018 - applied for its effect
    undo.commit()
    assert snapshot(home) == before


def test_an_empty_batch_is_refused(home: Path) -> None:
    with pytest.raises(EditError, match="at least one operation"):
        Batch(EditSession(root=home)).apply([])


def test_a_batch_cannot_be_committed_before_it_is_applied(home: Path) -> None:
    with pytest.raises(EditError, match="not been applied"):
        Batch(EditSession(root=home)).commit()


def test_a_batch_can_be_built_up_before_it_is_applied(home: Path) -> None:
    batch = Batch(EditSession(root=home))
    assert not batch
    batch.add(SetField(address="pc-desk", path="spec.model", value="NUC"))
    batch.add([UnsetField(address="sw-home", path="metadata.description")])
    assert len(batch) == 2
    assert batch.result is None
    assert len(batch.apply().applied) == 2
    assert batch.result is not None


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, 'set pc-desk spec.model = "NUC"'),
        (3, 'set pc-desk spec.model = "NUC" (+2 more)'),
    ],
)
def test_a_batch_is_named_after_its_first_operation(count: int, expected: str) -> None:
    one = SetField(address="pc-desk", path="spec.model", value="NUC")
    assert describe([one] * count) == expected


def test_a_batch_of_nothing_is_named_as_such() -> None:
    assert describe([]) == "no operations"


# --------------------------------------------------------------------------- #
# Align, distribute, snap
# --------------------------------------------------------------------------- #


def test_aligning_left_moves_every_left_edge_onto_the_leftmost(arranged: Path) -> None:
    tidy(arranged, "align.left", ["rtr-home", "sw-home", "pc-desk"])
    placed = positions(arranged)
    # rtr-home is 100 wide centred on 10, so its left edge is at -40. The others
    # move so that *their* left edges land there, which is a claim about edges.
    assert placed["rtr-home"] == (10.0, 300.0)
    assert placed["sw-home"] == (-10.0, 200.0)
    assert placed["pc-desk"] == (-20.0, 100.0)
    assert placed["srv-nas"] == (97.0, 20.0), "an element nobody selected does not move"


def test_aligning_right_moves_every_right_edge_onto_the_rightmost(arranged: Path) -> None:
    tidy(arranged, "align.right", ["rtr-home", "sw-home", "srv-nas"])
    placed = positions(arranged)
    # srv-nas is 80 wide centred on 97, so the right edge is 137.
    assert placed["srv-nas"] == (97.0, 20.0)
    assert placed["rtr-home"] == (87.0, 300.0)
    assert placed["sw-home"] == (107.0, 200.0)


def test_aligning_centres_uses_the_selections_own_axis(arranged: Path) -> None:
    tidy(arranged, "align.centre", ["rtr-home", "srv-nas"])
    placed = positions(arranged)
    # Left edge -40, right edge 137: the axis is half way between them.
    assert placed["rtr-home"][0] == pytest.approx(48.5)
    assert placed["srv-nas"][0] == pytest.approx(48.5)


def test_aligning_top_is_the_largest_y_because_y_grows_upwards(arranged: Path) -> None:
    tidy(arranged, "align.top", ["sw-home", "pc-desk", "srv-nas"])
    placed = positions(arranged)
    assert {name: y for name, (_, y) in placed.items() if name != "rtr-home"} == {
        "sw-home": 200.0,
        "pc-desk": 200.0,
        "srv-nas": 200.0,
    }


def test_aligning_bottom_is_the_smallest_y(arranged: Path) -> None:
    tidy(arranged, "align.bottom", ["sw-home", "pc-desk", "srv-nas"])
    placed = positions(arranged)
    assert [placed[name][1] for name in ("sw-home", "pc-desk", "srv-nas")] == [20.0, 20.0, 20.0]


def test_aligning_middles_uses_the_horizontal_axis(arranged: Path) -> None:
    tidy(arranged, "align.middle", ["sw-home", "srv-nas"])
    placed = positions(arranged)
    assert placed["sw-home"][1] == pytest.approx(110.0)
    assert placed["srv-nas"][1] == pytest.approx(110.0)


def test_distributing_equalises_the_gaps_and_leaves_the_extremes(arranged: Path) -> None:
    tidy(arranged, "distribute.vertical", ["rtr-home", "sw-home", "pc-desk", "srv-nas"])
    placed = positions(arranged)
    # Every node is 40 tall, so equal gaps mean equally spaced centres — and the
    # bottom of the lowest and the top of the highest are where they were.
    ys = sorted(y for _, y in placed.values())
    assert ys[0] == 20.0
    assert ys[-1] == 300.0
    gaps = [b - a for a, b in pairwise(ys)]
    # Within the hundredth of a point coordinates are stored to; see
    # netgraph.layout.geometry.round_coordinate.
    assert max(gaps) - min(gaps) <= 0.02, gaps


def test_distributing_two_elements_moves_nothing(arranged: Path) -> None:
    before = snapshot(arranged)
    assert tidy(arranged, "distribute.horizontal", ["rtr-home", "sw-home"]) == ()
    assert snapshot(arranged) == before


def test_snapping_rounds_to_the_configured_pitch(arranged: Path) -> None:
    tidy(arranged, "snap", ["rtr-home", "sw-home", "pc-desk", "srv-nas"], grid=25)
    placed = positions(arranged)
    assert placed == {
        "rtr-home": (0.0, 300.0),
        "sw-home": (25.0, 200.0),
        "pc-desk": (50.0, 100.0),
        "srv-nas": (100.0, 25.0),
    }


def test_snapping_takes_its_pitch_from_netgraph_toml(arranged: Path) -> None:
    (arranged / "netgraph.toml").write_text("[editor]\ngrid = 50\n", encoding="utf-8", newline="\n")
    config = parse_config({"editor": {"grid": 50}})
    session = EditSession(root=arranged, config=config)
    operations = arrange_operations(
        session.inventory,
        command="snap",
        view="physical",
        addresses=["sw-home"],
        grid=config.editor.grid,
    )
    batch = Batch(session)
    batch.apply(operations)
    batch.commit()
    assert positions(arranged)["sw-home"] == (50.0, 200.0)


def test_an_arrangement_writes_only_the_entries_that_moved(arranged: Path) -> None:
    """The reviewable-diff claim, asserted on the text of the file."""
    before = (arranged / "layout.yaml").read_text(encoding="utf-8").splitlines()
    tidy(arranged, "align.centre", ["sw-home", "pc-desk"])
    after = (arranged / "layout.yaml").read_text(encoding="utf-8").splitlines()
    changed = [line for line in before if line not in after]
    assert len(changed) == 2, changed
    assert all("sw-home" in line or "pc-desk" in line for line in changed)
    assert len(after) == len(before), "no line was added or removed"


def test_an_arrangement_that_changes_nothing_produces_no_operation(arranged: Path) -> None:
    tidy(arranged, "align.left", ["sw-home", "pc-desk"])
    before = snapshot(arranged)
    assert tidy(arranged, "align.left", ["sw-home", "pc-desk"]) == ()
    assert snapshot(arranged) == before, "aligning twice is not two changes"


def test_an_arrangement_spans_the_layout_documents_it_has_to(tmp_path: Path) -> None:
    """Two files placing two nodes each: one operation per file, one change."""
    root = tmp_path / "split"
    shutil.copytree(EXAMPLES / "home-lab", root)
    (root / "north.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: layout\nmetadata:\n  name: north\n"
        "spec:\n  views:\n    physical:\n      nodes:\n"
        "        rtr-home: {position: {x: 10, y: 300}}\n"
        "        sw-home: {position: {x: 40, y: 200}}\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "south.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: layout\nmetadata:\n  name: south\n"
        "spec:\n  views:\n    physical:\n      nodes:\n"
        "        pc-desk: {position: {x: 70, y: 100}}\n"
        "        srv-nas: {position: {x: 90, y: 20}}\n",
        encoding="utf-8",
        newline="\n",
    )
    session = EditSession(root=root)
    operations = arrange_operations(
        session.inventory,
        command="align.left",
        view="physical",
        addresses=["rtr-home", "sw-home", "pc-desk", "srv-nas"],
    )
    assert len(operations) == 2, "one per layout document"
    batch = Batch(session)
    result = batch.apply(operations)
    assert result.files == ("north.yaml", "south.yaml")
    batch.commit()
    for name in ("north", "south"):
        document = yaml.safe_load((root / f"{name}.yaml").read_text(encoding="utf-8"))
        for entry in document["spec"]["views"]["physical"]["nodes"].values():
            assert entry["position"]["x"] == 10.0


def test_a_layout_key_written_short_keeps_its_spelling(tmp_path: Path) -> None:
    """A namespaced tree whose layout names its nodes the way its folder does."""
    root = tmp_path / "campus"
    shutil.copytree(EXAMPLES / "campus", root)
    inventory = load_tree(root)
    namespaced = sorted(fqn for fqn in inventory.elements if "/" in fqn)[:2]
    folder = namespaced[0].rsplit("/", 1)[0]
    (root / folder / "layout.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: layout\nmetadata:\n  name: here\n"
        "spec:\n  views:\n    physical:\n      nodes:\n"
        + "".join(
            f"        {fqn.rsplit('/', 1)[1]}: {{position: {{x: {10 + index * 30}, y: 0}}}}\n"
            for index, fqn in enumerate(namespaced)
        ),
        encoding="utf-8",
        newline="\n",
    )
    session = EditSession(root=root)
    operations = arrange_operations(
        session.inventory, command="align.left", view="physical", addresses=namespaced
    )
    batch = Batch(session)
    batch.apply(operations)
    batch.commit()
    text = (root / folder / "layout.yaml").read_text(encoding="utf-8")
    for fqn in namespaced:
        assert f"        {fqn.rsplit('/', 1)[1]}:" in text, "the short key was re-spelled"


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_an_unknown_arrangement_is_refused(arranged: Path) -> None:
    with pytest.raises(EditError, match="unknown arrangement"):
        arrange_operations(
            load_tree(arranged), command="align.diagonal", view="physical", addresses=["sw-home"]
        )


def test_an_empty_selection_is_refused(arranged: Path) -> None:
    with pytest.raises(EditError, match="nothing is selected"):
        arrange_operations(load_tree(arranged), command="align.left", view="physical", addresses=[])


def test_aligning_one_element_is_refused(arranged: Path) -> None:
    with pytest.raises(EditError, match="at least 2 placed elements"):
        arrange_operations(
            load_tree(arranged), command="align.left", view="physical", addresses=["sw-home"]
        )


def test_arranging_a_view_with_no_stored_geometry_is_refused(home: Path) -> None:
    with pytest.raises(EditError, match="0 of the 2 selected"):
        arrange_operations(
            load_tree(home),
            command="align.left",
            view="physical",
            addresses=["sw-home", "pc-desk"],
        )


def test_a_grid_of_zero_is_refused(arranged: Path) -> None:
    with pytest.raises(EditError, match="positive number of points"):
        arrange_operations(
            load_tree(arranged),
            command="snap",
            view="physical",
            addresses=["sw-home"],
            grid=0,
        )


def test_a_selection_larger_than_the_cap_is_refused(arranged: Path) -> None:
    with pytest.raises(EditError, match="more than one arrangement should move"):
        arrange_operations(
            load_tree(arranged),
            command="snap",
            view="physical",
            addresses=[f"device-{index}" for index in range(2001)],
        )


def test_a_selection_of_duplicates_is_one_element(arranged: Path) -> None:
    with pytest.raises(EditError, match="at least 2 placed elements"):
        arrange_operations(
            load_tree(arranged),
            command="align.left",
            view="physical",
            addresses=["sw-home", "sw-home", " "],
        )


@pytest.mark.parametrize("command", ARRANGEMENTS)
def test_every_arrangement_has_a_line_naming_it(command: str) -> None:
    said = describe_arrangement(command, 4)
    assert said.startswith(("align ", "distribute ", "snap "))
    assert "4 elements" in said
    assert "4 element" in describe_arrangement(command, 4)


def test_one_element_is_named_in_the_singular() -> None:
    assert describe_arrangement("snap", 1) == "snap 1 element to the grid"


# --------------------------------------------------------------------------- #
# The grid, in netgraph.toml
# --------------------------------------------------------------------------- #


def test_the_grid_defaults_to_twenty_points() -> None:
    from netgraph.layout.geometry import DEFAULT_GRID

    assert parse_config({}).editor.grid == DEFAULT_GRID


@pytest.mark.parametrize("value", [8, 12.5])
def test_the_grid_is_read_from_the_editor_table(value: float) -> None:
    assert parse_config({"editor": {"grid": value}}).editor.grid == float(value)


@pytest.mark.parametrize(
    ("table", "complaint"),
    [
        ({"grid": 0}, "must be greater than 0"),
        ({"grid": -4}, "must be greater than 0"),
        ({"grid": "wide"}, "must be a number of points"),
        ({"grid": True}, "must be a number of points"),
        ({"pitch": 20}, "unknown key"),
    ],
)
def test_a_bad_editor_table_says_which_key_and_why(table: dict[str, Any], complaint: str) -> None:
    from netgraph.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match=complaint):
        parse_config({"editor": table})


def test_the_editor_table_must_be_a_table() -> None:
    from netgraph.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="must be a table"):
        parse_config({"editor": 20})
