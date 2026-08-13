"""The two statements about editing that are universally quantified.

``tests/test_edit.py`` pins what each operation does on inventories somebody
wrote. Two of the promises in ``docs/editing.md`` are not about those
inventories, though — they are about *every* inventory and *every* sequence of
operations, and an example test can only ever say they held for the cases
somebody thought of:

**Undo is byte-exact.** A random sequence of operations, followed by its
inverses applied in reverse, restores the tree byte for byte — every comment,
every blank line, every quoting choice, every file, and the absence of the files
that were deleted along the way. That is the property the whole design of
:mod:`netgraph.edit` exists to deliver, and it is the one that would rot first.

**The graph is a function of the end state, not of the route to it.** An
inventory edited into a shape and an inventory *written* in that shape are the
same network. If they were not, an editor would be a way of producing trees that
mean something different from the trees people write by hand, which is exactly
the failure a 1:1 mapping between the picture and the text has to rule out.

A third, quieter property is asserted alongside: reading a file and writing it
straight back is the identity, for every file of every generated inventory in
every layout. Everything above rests on it.

Reading a failure
-----------------

The counterexample is an :class:`~strategies.InventoryPlan` plus a list of
operations. ``print(plan.per_document())`` gives the YAML verbatim and the
operations print as the command lines that produce them, so a failure is
reproducible under the ordinary CLI. See ``docs/testing.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from netgraph.edit import (
    AddInterface,
    CreateElement,
    DeleteElement,
    EditError,
    EditSession,
    MoveElement,
    Operation,
    RenameElement,
    SetField,
    UnsetField,
    YamlFile,
)
from netgraph.loader import load_tree, namespace_of
from netgraph.render import Layer, RenderOptions, build_graph
from netgraph.render.jsonexport import graph_to_dict

import strategies as ng  # isort: skip -- tests/ is on sys.path, not a package


#: Operations that are always applicable to *some* element of any inventory, and
#: that between them exercise every path an inverse can take: a semantic one
#: (create, move, set-of-an-absent-field, add-interface) and a primitive one
#: (rename, delete, unset, set-of-a-present-field).
_MAX_OPERATIONS: Final = 4


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file below ``root``, as bytes. Absence is part of the state."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def operations_for(root: Path, choices: Sequence[int]) -> list[Operation]:
    """A sequence of operations aimed at whatever this inventory happens to hold.

    Driven by a list of integers rather than by a strategy over element names,
    because the names only exist once the plan has been drawn — so the strategy
    supplies indices and this picks what they land on. An index that lands on
    nothing simply produces no operation, which keeps generation total.
    """
    inventory = load_tree(root)
    names = sorted(inventory.elements)
    devices = sorted(inventory.devices)
    if not names:
        return []

    operations: list[Operation] = []
    for serial, choice in enumerate(choices):
        target = names[choice % len(names)]
        kind = choice % 6
        if kind == 0 and devices:
            # ``spec.vendor`` rather than something every kind happens to take:
            # a generated operation has to be *schema-valid*, or the property
            # would be about the gate rather than about the inverse.
            operations.append(
                SetField(
                    address=devices[choice % len(devices)],
                    path="spec.vendor",
                    value=f"V{serial}",
                )
            )
        elif kind == 1:
            operations.append(
                SetField(address=target, path="metadata.description", value=f"d{serial}")
            )
        elif kind == 2:
            operations.append(RenameElement(address=target, new_name=f"renamed-{serial}"))
        elif kind == 3:
            operations.append(
                CreateElement(
                    kind="server",
                    name=f"made-{serial}",
                    namespace=namespace_of(target),
                    spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
                )
            )
        elif kind == 4 and devices:
            device = devices[choice % len(devices)]
            operations.append(
                AddInterface(
                    address=device,
                    interface={"name": f"added{serial}", "type": "ethernet"},
                )
            )
        elif kind == 5:
            operations.append(
                MoveElement(
                    address=target,
                    file=f"{namespace_of(target)}/moved-{serial}.yaml".lstrip("/"),
                )
            )
    return operations


def apply_some(session: EditSession, operations: Sequence[Operation]) -> list[Operation]:
    """Apply what applies, and collect the inverses in the order they must run.

    A generated operation may be refused for a perfectly good reason — a name
    that is already taken, a cascade that was not asked for — and a refusal is
    itself part of the contract: it must leave nothing behind. So refusals are
    skipped rather than assumed away, and the property still holds over whatever
    did apply.
    """
    inverses: list[Operation] = []
    for operation in operations:
        try:
            applied = session.apply(operation)
        except EditError:
            continue
        inverses[:0] = applied.inverse
    return inverses


# --------------------------------------------------------------------------- #
# Round-tripping
# --------------------------------------------------------------------------- #


@given(plan=ng.inventory_plans(), layout=st.sampled_from(["per-document", "per-namespace"]))
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_reading_a_file_and_writing_it_back_is_the_identity(
    plan: ng.InventoryPlan, layout: str
) -> None:
    """Everything else rests on this: an untouched file is not rewritten."""
    for relative, text in plan.layouts()[layout].items():
        parsed = YamlFile.parse(text, relative=relative)
        assert parsed.render() == text


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #


@given(
    plan=ng.inventory_plans(),
    choices=st.lists(st.integers(min_value=0, max_value=97), max_size=_MAX_OPERATIONS),
)
@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
def test_a_sequence_of_operations_and_its_inverses_restore_the_tree(
    tmp_path_factory: pytest.TempPathFactory,
    plan: ng.InventoryPlan,
    choices: list[int],
) -> None:
    root = plan.write(tmp_path_factory.mktemp("edit"))
    assume(not load_tree(root).errors)
    before = snapshot(root)

    session = EditSession(root=root)
    inverses = apply_some(session, operations_for(root, choices))
    assume(session.changes)
    session.commit(force=True)
    assert snapshot(root) != before, "the operations must have done something to undo"

    undo = EditSession(root=root)
    undo.apply_all(inverses)
    undo.commit(force=True)
    assert snapshot(root) == before


@given(
    plan=ng.inventory_plans(),
    choices=st.lists(st.integers(min_value=0, max_value=97), min_size=1, max_size=2),
)
@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
def test_undoing_one_operation_at_a_time_restores_the_tree(
    tmp_path_factory: pytest.TempPathFactory,
    plan: ng.InventoryPlan,
    choices: list[int],
) -> None:
    """The per-operation form: each inverse restores exactly its own operation.

    Stronger than the batch property and cheaper to read a failure from — it
    names the one operation whose inverse is wrong instead of a sequence.
    """
    root = plan.write(tmp_path_factory.mktemp("edit"))
    assume(not load_tree(root).errors)
    for operation in operations_for(root, choices):
        before = snapshot(root)
        session = EditSession(root=root)
        try:
            applied = session.apply(operation)
        except EditError:
            assert snapshot(root) == before
            continue
        if not session.changes:
            continue
        session.commit(force=True)
        undo = EditSession(root=root)
        undo.apply_all(applied.inverse)
        undo.commit(force=True)
        assert snapshot(root) == before, f"{operation.describe()} does not undo exactly"


# --------------------------------------------------------------------------- #
# The end state is the end state
# --------------------------------------------------------------------------- #


def graph_of(root: Path) -> dict[str, Any]:
    """The layer-1 graph of an inventory, as data two trees can be compared by.

    Nodes and edges are sorted by id. Their *order* in the document is load
    order, which is the byte order of the file names (``NG-L005``) — a fact
    about the layout, and the one thing two trees in the same end state are
    allowed to disagree about.
    """
    inventory = load_tree(root)
    document = graph_to_dict(build_graph(inventory, layer=Layer.L1), RenderOptions())
    for key in ("nodes", "edges"):
        document[key] = sorted(document[key], key=lambda entry: entry["id"])
    return document


def rewritten(root: Path, destination: Path) -> Path:
    """The same inventory, written from scratch: one plain document per file.

    "From scratch" has to mean *not* a copy of the files that are there, or the
    property would compare an inventory with itself. So every element is emitted
    from its model, in the namespace it belongs to, into a file whose name says
    nothing — which is exactly what a person retyping the inventory from the
    picture would produce, and nothing like what the edit produced.
    """
    inventory = load_tree(root)
    for index, (fqn, element) in enumerate(inventory.elements.items()):
        namespace = namespace_of(fqn)
        folder = destination / namespace if namespace else destination
        folder.mkdir(parents=True, exist_ok=True)
        # Numbered, not named: a ``metadata.name`` may be 253 characters, which
        # is a legal name and an illegal file name on every platform.
        (folder / f"element{index}.yaml").write_text(
            ng.dump_documents([element.model_dump(mode="json", by_alias=True)]),
            encoding="utf-8",
        )
    return destination


@given(
    plan=ng.inventory_plans(),
    choices=st.lists(st.integers(min_value=0, max_value=97), max_size=_MAX_OPERATIONS),
)
@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
def test_the_edited_tree_and_a_tree_written_in_that_shape_are_one_network(
    tmp_path_factory: pytest.TempPathFactory,
    plan: ng.InventoryPlan,
    choices: list[int],
) -> None:
    base = tmp_path_factory.mktemp("edit")
    root = plan.write(base / "tree")
    assume(not load_tree(root).errors)

    session = EditSession(root=root)
    apply_some(session, operations_for(root, choices))
    assume(session.changes)
    session.commit(force=True)
    assume(not load_tree(root).errors)

    fresh = rewritten(root, base / "fresh")
    assume(not load_tree(fresh).errors)
    assert graph_of(fresh) == graph_of(root)


# --------------------------------------------------------------------------- #
# Regressions
# --------------------------------------------------------------------------- #


def test_a_cable_whose_endpoints_are_written_out_of_canonical_order(tmp_path: Path) -> None:
    """The model sorts endpoints; the document did not have to.

    Found by the property above, through the parse cache: a cached element
    reports every endpoint's ``document_index`` as its *canonical* position, so
    a rename that trusted the index rewrote the wrong end of the cable — which
    the round-trip check then reported as "the value comes from a template".
    ``locate_reference`` now treats the index as a hint. See entry 15 of
    ``docs/follow-ups.md``.
    """
    (tmp_path / "tree.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: zeta\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n---\n"
        "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: alpha\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n---\n"
        "apiVersion: netgraph.dev/v1alpha1\nkind: cable\nmetadata:\n  name: link\n"
        # ``zeta`` sorts after ``alpha``, so the model reorders these two.
        "spec:\n  endpoints:\n    - zeta:eth0\n    - alpha:eth0\n  medium: copper\n",
        encoding="utf-8",
    )
    session = EditSession(root=tmp_path)
    session.apply(RenameElement(address="zeta", new_name="omega"))
    session.commit()
    text = (tmp_path / "tree.yaml").read_text(encoding="utf-8")
    assert "    - omega:eth0\n    - alpha:eth0\n" in text
    assert not load_tree(tmp_path).errors


def test_removing_a_port_takes_the_bridge_membership_with_it(tmp_path: Path) -> None:
    """Found by the property above: a dangling ``members`` entry (``NG-I003``).

    Removing a port that a bridge lists left the bridge naming an interface that
    no longer existed, which does not merely warn — the document stops loading,
    and every cable to the device then dangles.
    """
    (tmp_path / "sw.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n  name: sw\n"
        "spec:\n  interfaces:\n    - name: br0\n      type: bridge\n"
        "      members: [eth0, eth1]\n"
        "    - name: eth0\n      type: ethernet\n"
        "    - name: eth1\n      type: ethernet\n",
        encoding="utf-8",
    )
    from netgraph.edit import RemoveInterface

    session = EditSession(root=tmp_path)
    session.apply(RemoveInterface(address="sw", name="eth1"))
    assert session.check() == ()
    session.commit()
    assert "eth1" not in (tmp_path / "sw.yaml").read_text(encoding="utf-8")


def test_a_document_that_cannot_be_re_emitted_exactly_still_undoes_exactly(
    tmp_path: Path,
) -> None:
    """Found by the sequence property: a scalar PyYAML escapes and ruamel does not.

    ``"\U0001f4a1"`` is written escaped by the emitter that produced the file and
    literally by the one that edits it, so re-emitting the document changes a
    line the edit never touched. Setting a field that was absent would normally
    be undone by unsetting it — but here that would leave the rewritten scalar
    behind, so the inverse has to be the pre-image instead.
    """
    text = (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: hub\n"
        "metadata:\n"
        "  name: h\n"
        "spec:\n"
        "  interfaces:\n"
        "  - name: eth0\n"
        "    type: ethernet\n"
        '  location: "\\U0001F4A1"\n'
    )
    (tmp_path / "h.yaml").write_text(text, encoding="utf-8")
    before = snapshot(tmp_path)

    session = EditSession(root=tmp_path)
    applied = session.apply(SetField(address="h", path="spec.vendor", value="V"))
    session.commit()
    assert [operation.op for operation in applied.inverse] == ["write-file"]

    undo = EditSession(root=tmp_path)
    undo.apply_all(applied.inverse)
    undo.commit()
    assert snapshot(tmp_path) == before


def test_deleting_the_last_document_of_a_file_deletes_the_file(tmp_path: Path) -> None:
    """Absence is part of the tree state, so it is part of what undo restores."""
    (tmp_path / "one.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: h\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n",
        encoding="utf-8",
    )
    before = snapshot(tmp_path)
    session = EditSession(root=tmp_path)
    applied = session.apply(DeleteElement(address="h"))
    session.commit(force=True)
    assert not (tmp_path / "one.yaml").exists()

    undo = EditSession(root=tmp_path)
    undo.apply_all(applied.inverse)
    undo.commit(force=True)
    assert snapshot(tmp_path) == before


def test_unsetting_a_commented_field_and_undoing_restores_the_comment(tmp_path: Path) -> None:
    """The reason an ``unset`` is inverted by a ``write-file`` and not by a ``set``."""
    text = (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: hub\n"
        "metadata:\n"
        "  name: h\n"
        "spec:\n"
        "  serial: ABC123  # the label on the front of the box\n"
        "  interfaces:\n"
        "    - name: eth0\n"
        "      type: ethernet\n"
    )
    (tmp_path / "h.yaml").write_text(text, encoding="utf-8")
    session = EditSession(root=tmp_path)
    applied = session.apply(UnsetField(address="h", path="spec.serial"))
    session.commit()
    assert "the label on the front" not in (tmp_path / "h.yaml").read_text(encoding="utf-8")

    undo = EditSession(root=tmp_path)
    undo.apply_all(applied.inverse)
    undo.commit()
    assert (tmp_path / "h.yaml").read_text(encoding="utf-8") == text
