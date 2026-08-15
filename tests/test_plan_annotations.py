"""Annotations in a changeset: addressable, plannable, and never structural (§21).

A note, an area and a legend are sidecars. They are part of the declared state —
``netgraph apply`` has to be able to write one, and a plan that ignored them
would leave a tree it said it had finished with — and they are part of *no*
network fact. Both halves of that are tested here, but the second is the one
that matters: **adding, changing or removing an annotation must never show up as
a change to a device, a cable, a tunnel, an adapter, a patch panel, a PDU, a
user, a group or a layout.**

The strongest form of that claim is the one asserted below: an infrastructure
diff is byte-identical whether or not the two trees carry annotations at all.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from netgraph.edit import EditSession, Operation, SetField
from netgraph.edit.operations import CreateAnnotation, DeleteAnnotation, SetAnnotation
from netgraph.importer.draft import Draft
from netgraph.loader import load_tree
from netgraph.plan import (
    ADDRESS_TYPES,
    ANNOTATION_TYPES,
    LAYOUT_TYPE,
    MISSING,
    Action,
    Address,
    AddressSyntaxError,
    Change,
    Plan,
    address_of,
    adopt,
    diff,
    elements_by_address,
    operations_for,
    parse_address,
    summary_line,
    translate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

#: One of each kind, anchored to things ``examples/home-lab`` really has.
ANNOTATIONS = """\
apiVersion: netgraph.dev/v1alpha1
kind: note
metadata:
  name: why-orange
spec:
  text: The run to the shed is fibre; copper does not reach.
  anchor:
    element: switches/sw-home
  color: "#fef3c7"
---
apiVersion: netgraph.dev/v1alpha1
kind: area
metadata:
  name: hallway
spec:
  label: Hallway cabinet
  members:
    - switches/sw-home
    - routers/rtr-home
---
apiVersion: netgraph.dev/v1alpha1
kind: legend
metadata:
  name: key
spec:
  title: Key
  auto: layers
"""

#: Every address in a plan of ``ANNOTATIONS`` against a tree without them.
ADDED = {"note.why-orange", "area.hallway", "legend.key"}


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A writable copy of ``examples/home-lab``."""
    root = tmp_path / "home-lab"
    shutil.copytree(EXAMPLES / "home-lab", root)
    return root


@pytest.fixture()
def twin(tmp_path: Path) -> Path:
    """A second writable copy, so a plan has two trees to be between."""
    root = tmp_path / "twin"
    shutil.copytree(EXAMPLES / "home-lab", root)
    return root


def annotate(root: Path, text: str = ANNOTATIONS) -> Path:
    """Write the annotation documents into a tree, in a file of their own."""
    document = root / "annotations.yaml"
    document.write_text(text, encoding="utf-8")
    return document


def plan_of(before: Path, after: Path) -> Plan:
    return diff(load_tree(before), load_tree(after))


def addresses(plan: Plan) -> set[str]:
    return {str(change.address) for change in plan}


def entry(plan: Plan, address: str) -> Change:
    matches = [change for change in plan if str(change.address) == address]
    assert matches, f"{address} is not in the plan: {sorted(addresses(plan))}"
    return matches[0]


def infrastructure(plan: Plan) -> str:
    """The plan with every annotation entry dropped, as canonical JSON.

    What is left is the changeset an inventory without any annotations would
    have produced — so if annotations are as inert as §21 claims, this text does
    not depend on whether there were any.
    """
    return json.dumps(
        [change.to_dict() for change in plan if change.address.type not in ANNOTATION_TYPES],
        sort_keys=True,
        indent=2,
    )


def mutate(root: Path, *operations: Operation) -> None:
    session = EditSession(root=root)
    session.apply_all(operations)
    session.commit()


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


def test_each_annotation_kind_has_an_address_type() -> None:
    assert address_of("note", "why") == Address(type="note", fqn="why")
    assert address_of("area", "sites/dmz") == Address(type="area", fqn="sites/dmz")
    assert address_of("legend", "key") == Address(type="legend", fqn="key")


def test_an_annotation_address_survives_a_round_trip() -> None:
    for text in ("note.why-orange", "area.sites/dmz", "legend.key", "note.sw.1"):
        assert str(parse_address(text)) == text


def test_annotations_are_addressed_after_the_infrastructure() -> None:
    """A plan reads the network first and the picture of it second."""
    ranks = {name: rank for rank, name in enumerate(ADDRESS_TYPES)}
    assert ranks[LAYOUT_TYPE] > ranks["device"]
    assert min(ranks[name] for name in ANNOTATION_TYPES) > ranks[LAYOUT_TYPE]
    assert [ranks[name] for name in ANNOTATION_TYPES] == sorted(
        ranks[name] for name in ANNOTATION_TYPES
    )


def test_an_unknown_kind_is_still_refused() -> None:
    with pytest.raises(AddressSyntaxError):
        address_of("annotation", "why")


def test_one_name_may_be_a_device_an_area_and_a_note(tmp_path: Path) -> None:
    """Each kind keeps its own name space, so a shared name is three documents."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "tree.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n  name: dmz\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: note\n"
        "metadata:\n  name: dmz\n"
        "spec:\n  text: careful\n  anchor:\n    element: dmz\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: area\n"
        "metadata:\n  name: dmz\n"
        "spec:\n  members: [dmz]\n",
        encoding="utf-8",
    )
    inventory = load_tree(root)
    assert not inventory.errors, inventory.errors
    documents = elements_by_address(inventory)
    assert {str(address) for address in documents} == {"device.dmz", "note.dmz", "area.dmz"}


# --------------------------------------------------------------------------- #
# The invariant
# --------------------------------------------------------------------------- #


def test_adding_annotations_changes_nothing_else(home: Path, twin: Path) -> None:
    annotate(twin)
    plan = plan_of(home, twin)
    assert addresses(plan) == ADDED
    assert all(change.action is Action.CREATE for change in plan)


def test_removing_annotations_changes_nothing_else(home: Path, twin: Path) -> None:
    annotate(home)
    plan = plan_of(home, twin)
    assert addresses(plan) == ADDED
    assert all(change.action is Action.DELETE for change in plan)


def test_the_summary_counts_them(home: Path, twin: Path) -> None:
    annotate(twin)
    plan = plan_of(home, twin)
    assert plan.counts() == {
        Action.CREATE: 3,
        Action.UPDATE: 0,
        Action.DELETE: 0,
        Action.RENAME: 0,
    }
    assert plan.to_dict()["summary"] == {
        "create": 3,
        "update": 0,
        "delete": 0,
        "rename": 0,
        "total": 3,
    }
    assert not plan.empty
    assert summary_line(plan) == "+ 3 to add"


def test_an_infrastructure_diff_is_identical_with_and_without_annotations(
    home: Path, twin: Path
) -> None:
    """The claim in its strongest form: same bytes, annotations or not.

    Not "the same set of addresses" — the same *text*, field changes, sources
    and order included. Anything an annotation leaked into the infrastructure
    half of a plan would show up here as a diff of the diff.
    """
    mutate(twin, SetField(address="sw-home", path="spec.model", value="TL-SG108PE"))
    bare = infrastructure(plan_of(home, twin))

    annotate(home)
    annotate(twin, ANNOTATIONS.replace("Hallway cabinet", "Hall cupboard"))
    annotated = plan_of(home, twin)

    assert infrastructure(annotated) == bare
    assert "area.hallway" in addresses(annotated)


def test_an_annotation_is_never_paired_with_an_element(home: Path, twin: Path) -> None:
    """Rename detection may not reach an annotation, in either direction.

    An annotation is named by its author and identified by nothing else. Pairing
    two on a guess would move one person's words onto another element, so a note
    that goes and a note that arrives are a delete and a create — always.
    """
    annotate(home)
    annotate(twin, ANNOTATIONS.replace("name: why-orange", "name: why-fibre"))
    plan = plan_of(home, twin)
    assert [change.action for change in plan] == [Action.DELETE, Action.CREATE]
    assert addresses(plan) == {"note.why-orange", "note.why-fibre"}


def test_an_annotation_never_borrows_an_elements_name(home: Path, twin: Path) -> None:
    """A note called after a device is not evidence about that device."""
    annotate(twin, ANNOTATIONS.replace("name: why-orange", "name: sw-home"))
    plan = plan_of(home, twin)
    assert "note.sw-home" in addresses(plan)
    assert not [change for change in plan if change.address.type == "device"]


def test_a_capture_never_proposes_dropping_an_annotation(home: Path) -> None:
    """No capture has a word for a note, so none is evidence about one."""
    annotate(home)
    inventory = load_tree(home)
    adoption = adopt(inventory, Draft())
    assert not adoption.rejected, adoption.rejected
    assert set(adoption.inventory.notes) == set(inventory.notes)
    assert set(adoption.inventory.areas) == set(inventory.areas)
    assert set(adoption.inventory.legends) == set(inventory.legends)
    assert diff(inventory, adoption.inventory).empty


# --------------------------------------------------------------------------- #
# What an annotation change says
# --------------------------------------------------------------------------- #


def test_an_update_is_reported_field_by_field(home: Path, twin: Path) -> None:
    """Unlike a layout, an annotation carries deltas rather than the document.

    Geometry is applied a whole view at a time and so has to be planned that
    way. A note's text is an ordinary field at an ordinary path, and a reviewer
    should be shown the sentence that changed rather than the document it is in.
    """
    annotate(home)
    annotate(twin, ANNOTATIONS.replace("copper does not reach", "copper will not reach"))
    change = entry(plan_of(home, twin), "note.why-orange")
    assert change.action is Action.UPDATE
    assert change.document is None
    assert [field.text for field in change.fields] == ["spec.text"]
    assert change.fields[0].after == "The run to the shed is fibre; copper will not reach."


def test_a_removed_field_is_reported_as_removed(home: Path, twin: Path) -> None:
    annotate(home)
    annotate(twin, ANNOTATIONS.replace('  color: "#fef3c7"\n', ""))
    change = entry(plan_of(home, twin), "note.why-orange")
    assert [field.text for field in change.fields] == ["spec.color"]
    assert change.fields[0].after is MISSING


def test_a_created_annotation_carries_its_document(home: Path, twin: Path) -> None:
    annotate(twin)
    change = entry(plan_of(home, twin), "legend.key")
    assert change.kind == "legend"
    assert change.document is not None
    assert change.document["spec"] == {"title": "Key", "corner": "bottom-right", "auto": "layers"}


def test_a_change_names_the_file_it_came_from(home: Path, twin: Path) -> None:
    """Provenance resolves through the annotation tables, not the element ones."""
    annotate(home)
    annotate(twin, ANNOTATIONS.replace("Hallway cabinet", "Hall cupboard"))
    change = entry(plan_of(home, twin), "area.hallway")
    assert change.source is not None
    assert change.source.startswith("annotations.yaml")


# --------------------------------------------------------------------------- #
# Applying one
# --------------------------------------------------------------------------- #


def named(operation: Any) -> tuple[str, str, str]:
    """How an annotation operation names its document."""
    return (operation.kind, operation.name, operation.namespace)


def test_creating_an_annotation_becomes_one_operation(home: Path, twin: Path) -> None:
    annotate(twin)
    change = entry(plan_of(home, twin), "note.why-orange")
    (operation,) = operations_for(change, EditSession(root=home))
    assert isinstance(operation, CreateAnnotation)
    assert named(operation) == ("note", "why-orange", "")
    assert operation.spec["text"].startswith("The run to the shed")


def test_deleting_an_annotation_becomes_one_operation(home: Path, twin: Path) -> None:
    annotate(home)
    change = entry(plan_of(home, twin), "area.hallway")
    (operation,) = operations_for(change, EditSession(root=home))
    assert isinstance(operation, DeleteAnnotation)
    assert named(operation) == ("area", "hallway", "")


def test_updating_an_annotation_becomes_one_write_per_field(home: Path, twin: Path) -> None:
    annotate(home)
    annotate(twin, ANNOTATIONS.replace("Hallway cabinet", "Hall cupboard"))
    change = entry(plan_of(home, twin), "area.hallway")
    (operation,) = operations_for(change, EditSession(root=home))
    assert isinstance(operation, SetAnnotation)
    assert named(operation) == ("area", "hallway", "")
    assert (operation.path, operation.value) == ("spec.label", "Hall cupboard")


def test_renaming_an_annotation_writes_its_name(home: Path) -> None:
    """Nothing refers to an annotation, so a rename is a field write and no more."""
    annotate(home)
    change = Change(
        action=Action.RENAME,
        address=parse_address("note.why-orange"),
        kind="note",
        new_address=parse_address("note.why-fibre"),
    )
    (operation,) = operations_for(change, EditSession(root=home))
    assert isinstance(operation, SetAnnotation)
    assert (operation.path, operation.value) == ("metadata.name", "why-fibre")


# --------------------------------------------------------------------------- #
# A block that is not there yet
# --------------------------------------------------------------------------- #

#: ``why-orange``, dragged: a note that had only an anchor now has a position.
PLACED = ANNOTATIONS.replace(
    '  color: "#fef3c7"\n',
    '  color: "#fef3c7"\n  geometry:\n    x: 120\n    y: -40\n',
)


def apply_plan(plan: Plan, root: Path) -> tuple[Operation, ...]:
    """Run a plan against a real tree and write it, as ``netgraph apply`` does."""
    session = EditSession(root=root)
    applied = tuple(
        operation for _, operations in translate(plan, session) for operation in operations
    )
    session.commit()
    return applied


def test_a_new_block_is_one_write_rather_than_a_leaf_at_a_time(home: Path, twin: Path) -> None:
    """``spec.geometry`` arrives whole, because half of one is not a §21 note.

    The diff already reports a wholly-new block as one field change, and the
    executor has to keep it that way: ``spec.geometry.x`` on its own is a
    position with no ``y``, and the write path re-checks the document after
    every field.
    """
    annotate(home)
    annotate(twin, PLACED)
    change = entry(plan_of(home, twin), "note.why-orange")
    assert [field.text for field in change.fields] == ["spec.geometry"]
    (operation,) = operations_for(change, EditSession(root=home))
    assert isinstance(operation, SetAnnotation)
    assert (operation.path, operation.value) == ("spec.geometry", {"x": 120.0, "y": -40.0})


def test_placing_an_unplaced_note_applies_to_a_real_tree(home: Path, twin: Path) -> None:
    """The end-to-end claim: the plan applies, and the tree says what it planned."""
    annotate(home)
    annotate(twin, PLACED)
    plan = plan_of(home, twin)
    apply_plan(plan, home)

    inventory = load_tree(home)
    assert not inventory.errors, inventory.errors
    geometry = inventory.notes["why-orange"].spec.geometry
    assert geometry is not None
    assert (geometry.x, geometry.y) == (120.0, -40.0)
    assert diff(inventory, load_tree(twin)).empty


def test_leaves_of_an_absent_block_are_grafted_back_together(home: Path) -> None:
    """A plan that spells the leaves separately still applies.

    A plan file is written now and applied later, and nothing stops one naming
    ``spec.geometry.x`` and ``spec.geometry.y`` as two changes — a hand-written
    one, or one from a netgraph whose diff split them. Applied in order they
    would be refused halfway, so the executor grafts them onto the one write of
    the block that the tree can actually take.
    """
    annotate(home)
    change = Change.from_dict(
        {
            "action": "update",
            "address": "note.why-orange",
            "kind": "note",
            "fields": [
                {"path": "spec.geometry.x", "after": 120},
                {"path": "spec.geometry.y", "after": -40},
            ],
        }
    )
    (operation,) = operations_for(change, EditSession(root=home))
    assert isinstance(operation, SetAnnotation)
    assert (operation.path, operation.value) == ("spec.geometry", {"x": 120, "y": -40})

    apply_plan(Plan(changes=(change,)), home)
    geometry = load_tree(home).notes["why-orange"].spec.geometry
    assert geometry is not None
    assert (geometry.x, geometry.y) == (120.0, -40.0)


def test_a_block_that_is_already_there_is_still_written_field_by_field(
    home: Path, twin: Path
) -> None:
    """Only an *absent* block is grouped; a placed note is dragged one field at a time.

    Which is what keeps a write as specific as the field it changes, and keeps
    the comments beside the fields it does not.
    """
    annotate(home, PLACED)
    annotate(twin, PLACED.replace("x: 120", "x: 200").replace("y: -40", "y: -50"))
    change = entry(plan_of(home, twin), "note.why-orange")
    operations = operations_for(change, EditSession(root=home))
    assert [(operation.path, operation.value) for operation in operations] == [  # type: ignore[attr-defined]
        ("spec.geometry.x", 200.0),
        ("spec.geometry.y", -50.0),
    ]
