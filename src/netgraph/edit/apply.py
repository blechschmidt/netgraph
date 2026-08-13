"""Turning an operation into a change to the files, and into its inverse.

One function per operation, all of them with the same shape: work out what the
change means against the *loaded* inventory, make it against the *round-trip*
documents, and hand back what it did.

The two halves are deliberately different views of the same tree. Meaning comes
from the models — which element an address names, which interface a cable
terminates on, which references resolve to what — because that is where the
schema is already implemented and where a reference written as a short name is
already resolved the way the loader resolves it. Change is made to the YAML
nodes, because that is where the comments are. Nothing here guesses at meaning
from the YAML, and nothing infers layout from the models.

Inverses
--------

Every applier returns the operations that undo it. A **semantic** inverse is
returned only when it is exact *by construction* — when undoing it cannot
disturb a byte that the operation did not write:

======================== =====================================================
create                   delete: the document did not exist, so removing it
                         leaves the file as it was
connect                  disconnect, for the same reason
move within a namespace  move back to the index it came from, the document
                         being carried across verbatim
set of an absent field   unset: the key was not there, and had no comment
add-interface            remove-interface
======================== =====================================================

The last two carry a condition, ``_is_faithful``: they edit a document in place,
so they may only claim a semantic inverse when re-emitting that document
reproduces it exactly (see :mod:`netgraph.edit.roundtrip`). The first three do
not, because they add or move whole documents and never re-emit one.

Everything else is inverted with :class:`~netgraph.edit.operations.WriteFile`
carrying the text each touched file had before. That is not a shortcut around
the typed model, it is the only honest answer: undoing a rename means restoring
the *spelling* of eleven references, undoing an unset means restoring the
comment that sat above the key, and no semantic operation carries either.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import yaml as pyyaml

from netgraph.edit.errors import AddressError, CascadeRequired, EditError, OperationError
from netgraph.edit.operations import (
    AddInterface,
    Connect,
    CreateElement,
    DeleteElement,
    Disconnect,
    MoveElement,
    Operation,
    RemoveFile,
    RemoveInterface,
    RenameElement,
    SetField,
    UnsetField,
    WriteFile,
)
from netgraph.edit.paths import MISSING, get_field, parse_field_path, set_field, unset_field
from netgraph.edit.placement import check_file, choose_file, namespace_of_file
from netgraph.edit.references import (
    NameIndex,
    Reference,
    ReferenceRole,
    dependents_of,
    drop_reference,
    reference_text,
    references_of,
    rewrite_reference,
)
from netgraph.edit.tree import EditableTree
from netgraph.errors import SchemaError
from netgraph.fmt.canonical import format_stream
from netgraph.importer.names import element_name
from netgraph.loader.inventory import Inventory, namespace_of, qualify, short_name
from netgraph.models import API_VERSION, Cable, Element, parse_document

__all__ = ["AppliedOperation", "apply_operation"]


@dataclass(frozen=True, slots=True)
class AppliedOperation:
    """What one operation did, and how to undo it."""

    operation: Operation
    #: The operations that undo it, in the order they must be applied.
    inverse: tuple[Operation, ...]
    #: One line naming what happened, for a log or a terminal.
    summary: str
    #: Files the operation changed, in path order.
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Located:
    """An address, resolved to an element and to the document holding it."""

    fqn: str
    element: Element
    relative: str
    index: int

    @property
    def namespace(self) -> str:
        return namespace_of(self.fqn)


@dataclass(frozen=True, slots=True)
class _Context:
    """What every applier needs: the files, the meaning, and the name table."""

    tree: EditableTree
    inventory: Inventory

    @property
    def index(self) -> NameIndex:
        return NameIndex(self.inventory.elements)

    def locate(self, address: str) -> _Located:
        """Resolve an address to the element and the document it lives in.

        Raises:
            AddressError: Nothing carries that name, or several things do.
        """
        resolution = self.inventory.lookup(address)
        if resolution.ambiguous:
            raise AddressError(
                f"{address!r} is ambiguous; it could mean "
                f"{', '.join(resolution.ambiguous)}. Write the fully-qualified name.",
                address=address,
                candidates=resolution.ambiguous,
            )
        if resolution.fqn is None or resolution.element is None:
            raise AddressError(
                f"there is no element called {address!r} in this inventory",
                address=address,
            )
        source = self.inventory.sources.get(resolution.fqn)
        if source is None or source.relative is None:  # pragma: no cover - always indexed
            raise AddressError(
                f"{resolution.fqn} was not loaded from a file and cannot be edited",
                address=address,
            )
        return _Located(
            fqn=resolution.fqn,
            element=resolution.element,
            relative=source.relative,
            index=source.index,
        )

    def document(self, located: _Located) -> Any:
        """The round-trip tree of an element's document, marked as changing."""
        return self.tree.document(located.relative, located.index).touch()


def apply_operation(
    operation: Operation, *, tree: EditableTree, inventory: Inventory
) -> AppliedOperation:
    """Apply one operation and return what it did and how to undo it.

    Raises:
        EditError: The operation cannot be applied. The tree is left as it was
            for everything raised before the first mutation, which is every
            check an applier makes; a failure part-way through is impossible
            because each applier finishes its mutations without further checks.
    """
    handler = _HANDLERS.get(type(operation))
    if handler is None:  # pragma: no cover - OPERATIONS and _HANDLERS are checked in tests
        raise OperationError(f"{type(operation).__name__} cannot be applied")
    context = _Context(tree=tree, inventory=inventory)
    tree.begin()
    try:
        inverse = handler(context, operation)
    except Exception:
        # A refusal must leave nothing behind -- not even a document marked as
        # about to change, which would be re-emitted and could move a line.
        tree.abort()
        raise
    journal = tree.end()
    return AppliedOperation(
        operation=operation,
        inverse=inverse if inverse is not None else _restore(journal),
        summary=operation.describe(),
        files=tuple(sorted(journal)),
    )


def _restore(journal: Mapping[str, str | None]) -> tuple[Operation, ...]:
    """The primitive inverse: put every file back the way it was."""
    return tuple(
        RemoveFile(path=path) if before is None else WriteFile(path=path, text=before)
        for path, before in sorted(journal.items())
    )


# --------------------------------------------------------------------------- #
# create / delete
# --------------------------------------------------------------------------- #


def _create(context: _Context, operation: CreateElement) -> tuple[Operation, ...]:
    fqn = qualify(operation.namespace, operation.name)
    if fqn in context.inventory.elements:
        raise EditError(f"{fqn} already exists; a name is unique within its namespace (NG-N002)")
    document = _element_document(
        kind=operation.kind,
        name=operation.name,
        metadata=operation.metadata,
        spec=operation.spec,
    )
    relative = choose_file(
        kind=operation.kind,
        namespace=operation.namespace,
        name=operation.name,
        files=context.tree.facts(context.inventory),
        requested=operation.file,
    )
    context.tree.insert_document(relative, -1, _emit(document))
    return (DeleteElement(address=fqn),)


def _element_document(
    *, kind: str, name: str, metadata: Mapping[str, Any], spec: Mapping[str, Any]
) -> dict[str, Any]:
    """The document a create writes, checked against the schema before it lands.

    Checking here rather than at the validation gate is not redundant: the gate
    reports what the *tree* would be wrong about, and a document that does not
    parse at all would come back as a load error naming a file the user never
    typed. A rejected ``spec`` should be reported as a rejected ``spec``.

    Raises:
        EditError: The document does not match the schema of its kind.
    """
    if "name" in metadata and metadata["name"] != name:
        raise OperationError(
            f"metadata.name is {metadata['name']!r} but the element was created as {name!r}"
        )
    document = {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": {"name": name, **{k: v for k, v in metadata.items() if k != "name"}},
        "spec": dict(spec),
    }
    try:
        parse_document(document)
    except SchemaError as exc:
        problems = "; ".join(str(issue) for issue in exc.issues)
        raise EditError(f"the new {kind} {name!r} does not match the schema: {problems}") from exc
    return document


def _emit(document: Mapping[str, Any]) -> str:
    """A freshly built document as canonical YAML.

    New text has no comments to preserve and no style to respect, so it is
    written the way ``netgraph fmt`` would write it — which means a tree that
    was canonical before the edit is still canonical after it.
    """
    dumped = pyyaml.safe_dump(
        dict(document), sort_keys=False, default_flow_style=False, allow_unicode=True, width=1 << 30
    )
    return format_stream(dumped)


def _delete(context: _Context, operation: DeleteElement) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    _remove_elements(context, [located], cascade=operation.cascade)
    return None


def _remove_elements(
    context: _Context, targets: Sequence[_Located], *, cascade: bool
) -> tuple[str, ...]:
    """Delete these elements, and whatever cannot survive without them.

    Returns:
        Every fully-qualified name that was removed, in load order.

    Raises:
        CascadeRequired: Something refers to a target and ``cascade`` is off.
    """
    index = context.index
    doomed = {located.fqn for located in targets}
    holders: dict[str, _Located] = {located.fqn: located for located in targets}

    # A link dies with either of its ends, and a tunnel dies with the tunnel it
    # runs inside -- so the closure has to be taken rather than one pass made.
    pending = list(doomed)
    clearing: list[Reference] = []
    while pending:
        target = pending.pop()
        for reference in dependents_of(target, context.inventory.elements, index):
            if reference.source in doomed:
                continue
            if reference.role.is_structural:
                doomed.add(reference.source)
                holders[reference.source] = context.locate(reference.source)
                pending.append(reference.source)
            else:
                clearing.append(reference)

    asked_for = {located.fqn for located in targets}
    dependents = sorted({reference.source for reference in clearing} | (doomed - asked_for))
    if dependents and not cascade:
        listed = ", ".join(dependents)
        raise CascadeRequired(
            f"{', '.join(sorted(asked_for))} is referred to by {listed}; "
            f"delete those too with --cascade, or change them first",
            address=targets[0].fqn,
            dependents=dependents,
        )

    # Clear the optional references before the documents go, so that a document
    # holding both a reference to a doomed element and a doomed element of its
    # own is handled in one pass.
    for reference in clearing:
        if reference.source in doomed:
            continue
        holder = context.locate(reference.source)
        drop_reference(context.document(holder), reference)
        _tidy_after_drop(context, holder, reference)

    # Highest document index first, so removing one does not renumber the next.
    for fqn in sorted(doomed, key=lambda name: (holders[name].relative, -holders[name].index)):
        located = holders[fqn]
        context.tree.remove_document(located.relative, located.index)
    return tuple(sorted(doomed))


def _tidy_after_drop(context: _Context, holder: _Located, reference: Reference) -> None:
    """Remove what a dropped reference leaves behind that means nothing on its own.

    ``spec.power.inputs`` is the case: the schema refuses ``powered_by: outlet``
    with no inputs (§17), so dropping the last input has to drop the block that
    declared it rather than leave a document the loader rejects.
    """
    if reference.role is not ReferenceRole.POWER_INPUT:
        return
    document = context.tree.document(holder.relative, holder.index).touch()
    inputs = get_field(document, ("spec", "power", "inputs"))
    if isinstance(inputs, list) and not inputs:
        power = get_field(document, ("spec", "power"))
        del power["inputs"]
        if isinstance(power, dict) and not power:
            unset_field(document, ("spec", "power"))


# --------------------------------------------------------------------------- #
# rename / move
# --------------------------------------------------------------------------- #


def _rename(context: _Context, operation: RenameElement) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    if operation.new_name == located.element.metadata.name:
        raise EditError(f"{located.fqn} is already called {operation.new_name!r}")
    new_fqn = qualify(located.namespace, operation.new_name)
    if new_fqn in context.inventory.elements:
        raise EditError(
            f"{new_fqn} already exists; a name is unique within its namespace (NG-N002)"
        )
    document = context.document(located)
    metadata = get_field(document, ("metadata",))
    if not isinstance(metadata, dict) or metadata.get("name") != located.element.metadata.name:
        raise EditError(  # pragma: no cover - the loader read the name from here
            f"{located.fqn}: metadata.name is not written in this document"
        )
    metadata["name"] = operation.new_name
    _repoint(context, old=located.fqn, new=new_fqn)
    return None


def _move(context: _Context, operation: MoveElement) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    name = located.element.metadata.name
    relative = check_file(operation.file)
    namespace = namespace_of_file(relative)
    if relative == located.relative and operation.index is None:
        raise EditError(f"{located.fqn} is already in {relative}")
    new_fqn = qualify(namespace, name)
    if new_fqn != located.fqn and new_fqn in context.inventory.elements:
        raise EditError(
            f"{new_fqn} already exists; moving {located.fqn} there would collide with it"
        )

    # The document travels as text, so every comment, blank line and quoting
    # choice in it arrives unchanged -- a move is a move, not a reformat.
    text = context.tree.document(located.relative, located.index).render()
    context.tree.remove_document(located.relative, located.index)
    target = operation.index if operation.index is not None else -1
    position = context.tree.insert_document(relative, target, text)

    if namespace == located.namespace:
        return (MoveElement(address=new_fqn, file=located.relative, index=located.index),)
    _repoint(context, old=located.fqn, new=new_fqn)
    _requalify(
        context, old=located.fqn, new=new_fqn, element=located.element, at=(relative, position)
    )
    return None


def _requalify(
    context: _Context, *, old: str, new: str, element: Element, at: tuple[str, int]
) -> None:
    """Keep the moved element's *own* references pointing where they pointed.

    A plain name resolves outwards from the folder its document sits in, so a
    document that moves to another folder can silently start naming a different
    element — or nothing at all. Every reference it makes is therefore resolved
    from where it *was* and re-spelled for where it *is*, and only the ones
    whose meaning would have changed are touched.
    """
    before, after = context.index, context.index.replaced(old, new)
    old_namespace, new_namespace = namespace_of(old), namespace_of(new)
    handle = context.tree.document(*at)
    for reference in references_of(old, element):
        target = before.lookup(reference.target, old_namespace)
        if target is None:  # already dangling; a move is not the place to fix it
            continue
        replacement = reference_text(
            new if target == old else target,
            namespace=new_namespace,
            written=reference.target,
            index=after,
        )
        if replacement != reference.target:
            rewrite_reference(handle.touch(), reference, replacement)


def _repoint(context: _Context, *, old: str, new: str) -> None:
    """Rewrite every reference to ``old`` so that it names ``new``.

    The spelling each document used is kept wherever it still resolves; see
    :func:`~netgraph.edit.references.reference_text`.
    """
    after = context.index.replaced(old, new)
    for reference in dependents_of(old, context.inventory.elements, context.index):
        if reference.source == old:  # pragma: no cover - nothing references itself
            continue
        holder = context.locate(reference.source)
        replacement = reference_text(
            new, namespace=reference.namespace, written=reference.target, index=after
        )
        rewrite_reference(context.document(holder), reference, replacement)


# --------------------------------------------------------------------------- #
# fields
# --------------------------------------------------------------------------- #


def _set(context: _Context, operation: SetField) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    path = parse_field_path(operation.path)
    if path[:1] == ("kind",) or path[:2] == ("metadata", "name"):
        raise OperationError(
            f"{operation.path} is not settable: use 'rename' to change a name and create a new "
            f"element to change a kind"
        )
    document = context.document(located)
    previous = get_field(document, path)
    set_field(document, path, operation.value)
    # Adding a key that was not there is undone by removing it, which cannot
    # disturb anything else -- but only if re-emitting the document reproduces
    # it, which is not true of every document (see ``_is_faithful``).
    if previous is MISSING and _is_faithful(context, located):
        return (UnsetField(address=located.fqn, path=operation.path),)
    return None


def _unset(context: _Context, operation: UnsetField) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    path = parse_field_path(operation.path)
    if len(path) == 1 and path[0] in _ENVELOPE:
        raise OperationError(f"{operation.path} is part of the document envelope and is required")
    unset_field(context.document(located), path)
    return None


#: The four keys every document has (§3). Removing one does not produce a
#: smaller document, it produces a document the loader rejects.
_ENVELOPE: Final = frozenset({"apiVersion", "kind", "metadata", "spec"})


def _is_faithful(context: _Context, located: _Located) -> bool:
    """Would re-emitting this document reproduce the source exactly?

    The condition under which an operation that *edits a document in place* may
    claim a semantic inverse. When it does not hold, applying the operation
    already rewrites lines nobody touched — a scalar the file escaped and the
    emitter does not, an indent style no candidate matched — and the opposite
    operation would rewrite them again rather than putting them back. The
    pre-images are the only thing that can.

    Found by ``test_a_sequence_of_operations_and_its_inverses_restore_the_tree``
    on a document holding an astral-plane character in a double-quoted scalar,
    which PyYAML escapes and ruamel does not.
    """
    return context.tree.document(located.relative, located.index).faithful


def _add_interface(context: _Context, operation: AddInterface) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    name = operation.interface.get("name")
    if not isinstance(name, str) or not name:
        raise OperationError("an interface needs a 'name'")
    if not located.element.has_interfaces:
        raise EditError(f"a {located.element.kind} has no interfaces to add to")
    document = context.document(located)
    interfaces = get_field(document, ("spec", "interfaces"))
    if interfaces is MISSING:  # pragma: no cover - every kind with ports requires it
        set_field(document, ("spec", "interfaces"), [])
        interfaces = get_field(document, ("spec", "interfaces"))
    if not isinstance(interfaces, list):  # pragma: no cover - the schema says it is one
        raise EditError(f"{located.fqn}: spec.interfaces is not a sequence")
    if any(isinstance(entry, dict) and entry.get("name") == name for entry in interfaces):
        raise EditError(f"{located.fqn} already has an interface called {name!r} (NG-I001)")
    position = (
        len(interfaces)
        if operation.index is None
        else max(0, min(operation.index, len(interfaces)))
    )
    interfaces.insert(position, dict(operation.interface))
    if not _is_faithful(context, located):
        return None
    return (RemoveInterface(address=located.fqn, name=name),)


def _remove_interface(
    context: _Context, operation: RemoveInterface
) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    # The handle rather than the index: removing the cables below may take
    # documents out of this very file and renumber everything after them.
    handle = context.tree.document(located.relative, located.index)
    document = handle.touch()
    interfaces = get_field(document, ("spec", "interfaces"))
    if _interface_position(interfaces, operation.name) is None:
        raise EditError(
            f"{located.fqn} declares no interface written as {operation.name!r} in its own "
            f"document; it may come from a 'range' or from a template, which is where it has "
            f"to be removed"
        )

    # A sub-interface cannot outlive the port it hangs off (§6.2.4), so removing
    # a port removes the VLANs stacked on it, transitively.
    doomed = _interface_closure(interfaces, operation.name)
    links = sorted(
        {
            reference.source
            for reference in dependents_of(located.fqn, context.inventory.elements, context.index)
            if reference.role is ReferenceRole.ENDPOINT and reference.detail in doomed
        }
    )
    blockers = sorted(links) + sorted(doomed - {operation.name})
    if blockers and not operation.cascade:
        raise CascadeRequired(
            f"{located.fqn}:{operation.name} is used by {', '.join(blockers)}; "
            f"remove those too with --cascade, or change them first",
            address=located.fqn,
            dependents=blockers,
        )
    if links:
        _remove_elements(context, [context.locate(fqn) for fqn in links], cascade=True)

    interfaces = get_field(handle.data, ("spec", "interfaces"))
    for name in doomed:
        # A bridge or LAG that listed the port has to stop listing it, or the
        # document it is in no longer loads at all (``NG-I003``).
        for entry in interfaces:
            members = entry.get("members") if isinstance(entry, dict) else None
            if isinstance(members, list) and name in members:
                members.remove(name)
        position = _interface_position(interfaces, name)
        if position is not None:  # pragma: no branch - every doomed name was found above
            interfaces.pop(position)
    return None


def _interface_closure(interfaces: Any, name: str) -> set[str]:
    """``name`` and every interface that hangs off it through ``parent``."""
    doomed = {name}
    pending = [name]
    while pending:
        parent = pending.pop()
        for entry in interfaces if isinstance(interfaces, list) else ():
            if not isinstance(entry, dict) or entry.get("parent") != parent:
                continue
            child = entry.get("name")
            if isinstance(child, str) and child not in doomed:
                doomed.add(child)
                pending.append(child)
    return doomed


def _interface_position(interfaces: Any, name: str) -> int | None:
    if not isinstance(interfaces, list):
        return None
    for position, entry in enumerate(interfaces):
        if isinstance(entry, dict) and entry.get("name") == name:
            return position
    return None


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def _connect(context: _Context, operation: Connect) -> tuple[Operation, ...]:
    ends = [_endpoint(context, operation.a), _endpoint(context, operation.b)]
    namespace = (
        operation.namespace
        if operation.namespace is not None
        else _common_namespace([end.located.namespace for end in ends])
    )
    name = operation.name or _cable_name(context, ends)
    fqn = qualify(namespace, name)
    if fqn in context.inventory.elements:
        raise EditError(f"{fqn} already exists; give the cable another name")

    index = context.index
    spec: dict[str, Any] = {
        "endpoints": [
            f"{reference_text(end.located.fqn, namespace=namespace, written=end.written, index=index)}"
            f":{end.interface}"
            for end in ends
        ],
        "medium": "copper",
    }
    spec.update({key: value for key, value in operation.spec.items() if key != "endpoints"})
    document = _element_document(kind="cable", name=name, metadata={}, spec=spec)
    relative = choose_file(
        kind="cable",
        namespace=namespace,
        name=name,
        files=context.tree.facts(context.inventory),
        requested=operation.file,
    )
    context.tree.insert_document(relative, -1, _emit(document))
    return (Disconnect(address=fqn),)


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """One end of a ``connect``, resolved."""

    located: _Located
    #: The element part exactly as the caller wrote it.
    written: str
    interface: str


def _endpoint(context: _Context, text: str) -> _Endpoint:
    """Parse and resolve ``device:interface``.

    Raises:
        OperationError: The text is not an interface reference.
        AddressError: The device part names nothing, or several things.
        EditError: The element has no such interface.
    """
    device, separator, interface = text.partition(":")
    if not separator or not interface or ":" in interface:
        raise OperationError(f"{text!r} is not an endpoint; expected 'device:interface'")
    located = context.locate(device)
    # ``has_interfaces`` is the class-level answer to "can this be cabled at
    # all" (§7.1); everything for which it is true has ``interface``.
    owner = located.element if located.element.has_interfaces else None
    if owner is None or owner.interface(interface) is None:  # type: ignore[union-attr]
        raise EditError(
            f"{located.fqn} has no interface called {interface!r} (NG-C002); "
            f"add it first with 'netgraph edit add-interface'"
        )
    return _Endpoint(located=located, written=device, interface=interface)


def _common_namespace(namespaces: Sequence[str]) -> str:
    """The deepest namespace that contains all of ``namespaces``."""
    parts = [namespace.split("/") if namespace else [] for namespace in namespaces]
    shared: list[str] = []
    for step in zip(*parts, strict=False):
        if len(set(step)) != 1:
            break
        shared.append(step[0])
    return "/".join(shared)


def _cable_name(context: _Context, ends: Sequence[_Endpoint]) -> str:
    """A name for a cable nobody named: the importer's, and unique.

    ``cbl-a-b`` while that is free, then the long form naming both ports, then a
    counter — the same ladder ``netgraph import`` climbs, so a tree that mixes
    imported and hand-drawn cables reads as one tree.
    """
    a, b = (short_name(end.located.fqn) for end in ends)
    ports = [end.interface for end in ends]
    candidates = [
        element_name(f"cbl-{a}-{b}")[0] or "cbl",
        element_name(f"cbl-{a}-{ports[0]}-{b}-{ports[1]}")[0] or "cbl",
    ]
    taken = {short_name(fqn) for fqn in context.inventory.elements}
    for candidate in candidates:
        if candidate not in taken:
            return candidate
    for serial in range(2, 1000):  # pragma: no branch - a thousand parallel links is enough
        candidate = f"{candidates[-1]}-{serial}"
        if candidate not in taken:
            return candidate
    raise EditError("cannot derive a free cable name; give one with --name")  # pragma: no cover


def _disconnect(context: _Context, operation: Disconnect) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    if not isinstance(located.element, Cable):
        raise EditError(
            f"{located.fqn} is a {located.element.kind}, not a cable; use 'delete' for it"
        )
    _remove_elements(context, [located], cascade=False)
    return None


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #


def _write_file(context: _Context, operation: WriteFile) -> tuple[Operation, ...] | None:
    context.tree.write_file(check_file(operation.path), operation.text)
    return None


def _remove_file(context: _Context, operation: RemoveFile) -> tuple[Operation, ...] | None:
    context.tree.remove_file(check_file(operation.path))
    return None


_Handler = Callable[[_Context, Any], "tuple[Operation, ...] | None"]


#: One applier per operation. A handler returns the semantic inverse when it has
#: one that is exact, and ``None`` to have the pre-images used instead.
_HANDLERS: Final[dict[type[Operation], _Handler]] = {
    CreateElement: _create,
    DeleteElement: _delete,
    RenameElement: _rename,
    MoveElement: _move,
    SetField: _set,
    UnsetField: _unset,
    AddInterface: _add_interface,
    RemoveInterface: _remove_interface,
    Connect: _connect,
    Disconnect: _disconnect,
    WriteFile: _write_file,
    RemoveFile: _remove_file,
}
