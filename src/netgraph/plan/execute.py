"""Turning a changeset into the edit operations that make it happen.

Every entry of a plan becomes one or more of the typed, comment-preserving
operations from :mod:`netgraph.edit`. Nothing here writes a file, parses YAML or
knows what a comment is: the edit layer already owns all of that, and going
through it is what makes ``netgraph apply`` preserve formatting for free and
refuse — through the same validation gate ``netgraph edit`` uses — to leave a
tree worse than it found it.

The translation, entry by entry:

============ ===================================================================
``create``   :class:`~netgraph.edit.operations.CreateElement` with the planned
             body. Placement is left to the edit layer, which puts the document
             where the tree's own conventions say it goes.
``delete``   :class:`~netgraph.edit.operations.DeleteElement`. Never cascading:
             a plan that deletes a device also deletes the cables on it, in that
             order, and a cascade would be the executor doing something the plan
             did not say.
``rename``   :class:`~netgraph.edit.operations.RenameElement`, which rewrites
             every reference to the old name as it goes.
``update``   One :class:`~netgraph.edit.operations.SetField` or
             :class:`~netgraph.edit.operations.UnsetField` per field, except for
             an interface appearing or disappearing whole — those are
             :class:`~netgraph.edit.operations.AddInterface` and
             :class:`~netgraph.edit.operations.RemoveInterface`, because a
             sequence entry cannot be written at a path that does not exist yet.
============ ===================================================================

The one subtlety is the field path. A plan stores
``spec.interfaces[name=eth0].mtu``; the edit layer wants
``spec.interfaces[2].mtu``. The index is resolved here, against the document as
it stands *at that moment* in the session — so a plan that adds two interfaces
and then sets a field on the first still lands on the right one.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from netgraph.edit import (
    AddInterface,
    CreateElement,
    DeleteElement,
    EditSession,
    Operation,
    RemoveInterface,
    RenameElement,
    SetField,
    SetGeometry,
    UnsetField,
    format_field_path,
)
from netgraph.errors import NetgraphError
from netgraph.plan.address import LAYOUT_TYPE, Address
from netgraph.plan.model import Action, Change, FieldChange, Plan
from netgraph.plan.paths import MISSING, Selector, Step, format_path, resolve

__all__ = ["PlanExecutionError", "operations_for", "translate"]

#: The path prefix that means "one entry of ``spec.interfaces``".
_INTERFACES: tuple[Step, ...] = ("spec", "interfaces")


class PlanExecutionError(NetgraphError):
    """A changeset entry cannot be turned into an operation against this tree."""


def translate(plan: Plan, session: EditSession) -> Iterator[tuple[Change, tuple[Operation, ...]]]:
    """Yield each change with the operations that make it, in plan order.

    The session is *advanced* as the generator runs: each entry's operations are
    applied before the next entry is translated, because resolving a field path
    needs the tree as the previous entries left it. A caller that only wants to
    look at the operations should hand in a session it is willing to throw away.

    Raises:
        PlanExecutionError: An entry cannot be expressed against this tree.
        EditError: An operation was refused. The session is left as it was when
            that operation was attempted.
    """
    for change in plan:
        operations = operations_for(change, session)
        session.apply_all(operations)
        yield change, operations


def operations_for(change: Change, session: EditSession) -> tuple[Operation, ...]:
    """The operations one changeset entry becomes, against the current session."""
    if change.address.type == LAYOUT_TYPE:
        return tuple(_geometry(change))
    if change.action is Action.CREATE:
        return (_create(change),)
    if change.action is Action.DELETE:
        return (DeleteElement(address=_addressed(change.address)),)
    if change.action is Action.RENAME:
        assert change.new_address is not None  # guaranteed by Change.from_dict
        return (
            RenameElement(address=_addressed(change.address), new_name=change.new_address.name),
        )
    return tuple(_updates(change, session))


def _create(change: Change) -> CreateElement:
    document = change.document
    if not isinstance(document, Mapping):
        raise PlanExecutionError(
            f"{change.address}: the plan has no document to create this element from"
        )
    metadata = document.get("metadata")
    return CreateElement(
        kind=change.kind,
        name=change.address.name,
        namespace=change.address.namespace,
        spec=document.get("spec") or {},
        metadata={
            key: value
            for key, value in (metadata or {}).items()
            if key != "name" and value not in ({}, [])
        },
    )


def _updates(change: Change, session: EditSession) -> Iterator[Operation]:
    address = _addressed(change.address)
    document = _document_of(change.address, session)
    for field in change.fields:
        if tuple(field.path) == ("kind",):
            # The diff is right to call this an update — all five device kinds
            # share one address — but the write path has no operation for it,
            # deliberately: a document's ``kind`` decides which model validates
            # it, so changing one is replacing the document rather than editing
            # a field. Saying so beats a bare "kind is not settable".
            raise PlanExecutionError(
                f"{change.address}: changing kind from {field.before!r} to {field.after!r} "
                f"replaces the document rather than editing it; 'netgraph edit delete' the "
                f"element and create it again, or apply the rest with --target"
            )
        interface = _interface_entry(field)
        if interface is not None:
            yield from _interface_operation(address, field, interface)
            continue
        path = _resolved(change.address, field, document)
        if field.after is MISSING:
            yield UnsetField(address=address, path=path)
        else:
            yield SetField(address=address, path=path, value=_plain(field.after))


def _interface_operation(
    address: str, field: FieldChange, selector: Selector
) -> Iterator[Operation]:
    """A whole interface appearing or disappearing, rather than a field of one."""
    if field.after is MISSING:
        yield RemoveInterface(address=address, name=selector.value)
        return
    entry = field.after
    if not isinstance(entry, Mapping):  # pragma: no cover - a keyed entry is a mapping
        raise PlanExecutionError(f"{address}: {field.text} is not an interface")
    yield AddInterface(address=address, interface=dict(entry))


def _interface_entry(field: FieldChange) -> Selector | None:
    """Is this change the whole of one ``spec.interfaces`` entry?"""
    if len(field.path) != len(_INTERFACES) + 1:
        return None
    if tuple(field.path[: len(_INTERFACES)]) != _INTERFACES:
        return None
    last = field.path[-1]
    if not isinstance(last, Selector) or last.key != "name":
        return None
    # Only when the entry appears or disappears whole; a change *inside* one is
    # an ordinary field write at a deeper path.
    return last if field.added or field.removed else None


def _resolved(address: Address, field: FieldChange, document: Any) -> str:
    """The plan's path, with every selector turned into the index it names."""
    try:
        # ``resolve`` leaves only keys and indices, which is exactly the edit
        # layer's grammar; the annotation is the wider plan one either way.
        resolved: Sequence[str | int] = [
            step for step in resolve(field.path, document) if not isinstance(step, Selector)
        ]
        return format_field_path(resolved)
    except Exception as error:
        raise PlanExecutionError(
            f"{address}: cannot locate {format_path(field.path)} in the document as it "
            f"stands ({error}); the tree has changed since the plan was made"
        ) from error


def _document_of(address: Address, session: EditSession) -> Any:
    """The element's document as plain data, for resolving selectors against.

    The *model* is used rather than the raw YAML, so a path lands the same way
    the diff computed it: through merged templates and expanded interface
    ranges. Where those make a path unwritable the edit layer refuses the
    operation, which is a better place to find out than a silent miss.
    """
    from netgraph.plan.document import document_of

    element = session.inventory.elements.get(address.fqn)
    if element is None:
        layout = session.inventory.layouts.get(address.fqn)
        if layout is None:
            raise PlanExecutionError(
                f"{address}: no such element in the tree; the plan was made against "
                f"a different state"
            )
        return document_of(layout)
    return document_of(element)


def _geometry(change: Change) -> Iterator[Operation]:
    """A layout document, one view at a time.

    Geometry is written by :class:`~netgraph.edit.operations.SetGeometry`, which
    takes a whole view rather than a field of one, so a layout entry does not go
    through the field-path machinery at all. Every section is passed explicitly,
    empty ones included: ``SetGeometry`` leaves a section alone only when it is
    ``None``, and a plan that says a view has no edges means it.
    """
    name, namespace = change.address.name, change.address.namespace
    views = _views(change.document if change.action is not Action.DELETE else None)
    touched = _touched_views(change)
    for view, geometry in views.items():
        if view not in touched:
            # Rewriting a view nobody moved would re-emit every coordinate in it
            # and put a hunk in the diff that the plan never claimed.
            continue
        yield SetGeometry(
            view=view,
            nodes=dict(geometry.get("nodes") or {}),
            edges=dict(geometry.get("edges") or {}),
            groups=dict(geometry.get("groups") or {}),
            layout=name,
            namespace=namespace,
        )
    for view in _dropped_views(change):
        if view not in views:
            yield SetGeometry(view=view, layout=name, namespace=namespace)


def _views(document: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    spec = (document or {}).get("spec") or {}
    views = spec.get("views") or {}
    return {
        str(view): geometry for view, geometry in views.items() if isinstance(geometry, Mapping)
    }


def _touched_views(change: Change) -> frozenset[str]:
    """Which views the change is actually about.

    A created layout is about all of them; an updated one is about whichever the
    field paths name, and the diff's paths start ``spec.views.<view>``.
    """
    if change.action is not Action.UPDATE:
        return frozenset(_views(change.document))
    return frozenset(
        str(field.path[2])
        for field in change.fields
        if len(field.path) >= 3 and tuple(field.path[:2]) == ("spec", "views")
    )


def _dropped_views(change: Change) -> tuple[str, ...]:
    """Views the change removes: every one of them on a delete, else the diff's."""
    if change.action is Action.DELETE:
        return tuple(_views(change.document))
    return tuple(
        str(field.path[2])
        for field in change.fields
        if field.removed and len(field.path) == 3 and tuple(field.path[:2]) == ("spec", "views")
    )


def _addressed(address: Address) -> str:
    """The edit layer addresses an element by its qualified name, not by type."""
    return address.fqn


def _plain(value: Any) -> Any:
    """A planned value as YAML-writable plain data."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_plain(item) for item in value]
    return value
