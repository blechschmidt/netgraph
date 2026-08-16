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
copy                     delete, for the same reason: a copy is a whole new
                         document and never re-emits the one it came from
connect                  disconnect, for the same reason
move within a namespace  move back to the index it came from, the document
                         being carried across verbatim -- unless the move
                         emptied its source file; see :func:`_is_reproducible`
set of an absent field   unset: the key was not there, and had no comment
add-interface            remove-interface
======================== =====================================================

The last two carry a condition, ``_is_faithful``: they edit a document in place,
so they may only claim a semantic inverse when re-emitting that document
reproduces it exactly (see :mod:`netviz.edit.roundtrip`). The first three do
not, because they add or move whole documents and never re-emit one.

Everything else is inverted with :class:`~netviz.edit.operations.WriteFile`
carrying the text each touched file had before. That is not a shortcut around
the typed model, it is the only honest answer: undoing a rename means restoring
the *spelling* of eleven references, undoing an unset means restoring the
comment that sat above the key, and no semantic operation carries either.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import yaml as pyyaml

from netviz.edit.cascade import CascadePlan, ClearedReference, plan_cascade
from netviz.edit.clipboard import dedupe_name, strip_unique
from netviz.edit.errors import AddressError, CascadeRequired, EditError, OperationError
from netviz.edit.operations import (
    AddInterface,
    AppendItem,
    Connect,
    CopyElement,
    CreateAnnotation,
    CreateElement,
    DeleteAnnotation,
    DeleteElement,
    Disconnect,
    MoveElement,
    Operation,
    RemoveFile,
    RemoveInterface,
    RenameElement,
    SetAnnotation,
    SetField,
    SetGeometry,
    SetLinkGeometry,
    UnsetField,
    WriteFile,
)
from netviz.edit.paths import (
    MISSING,
    FieldPath,
    get_field,
    parse_field_path,
    set_field,
    unset_field,
)
from netviz.edit.placement import check_file, choose_file, namespace_of_file
from netviz.edit.references import (
    NameIndex,
    Reference,
    ReferenceRole,
    dependents_of,
    drop_reference,
    reference_text,
    references_of,
    rewrite_reference,
)
from netviz.edit.rename import RenamePlan, plan_rename
from netviz.edit.roundtrip import YamlDocument, YamlFile
from netviz.edit.tree import EditableTree
from netviz.errors import SchemaError
from netviz.fmt.canonical import format_stream
from netviz.importer.names import element_name
from netviz.layout.document import as_yaml, canonical_geometry
from netviz.loader.inventory import Inventory, namespace_of, qualify, short_name
from netviz.models import (
    API_VERSION,
    COHERENCE_RULE,
    LAYOUT_KIND,
    Cable,
    Element,
    parse_annotation,
    parse_document,
    parse_layout,
)

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
    written the way ``netviz fmt`` would write it — which means a tree that
    was canonical before the edit is still canonical after it.
    """
    dumped = pyyaml.safe_dump(
        dict(document), sort_keys=False, default_flow_style=False, allow_unicode=True, width=1 << 30
    )
    return format_stream(dumped)


def _copy(context: _Context, operation: CopyElement) -> tuple[Operation, ...]:
    """Write a second element built from an existing one.

    The copy starts as the source's *text*, re-parsed into a document of its own,
    so it arrives carrying the original's comments, its key order and its quoting
    — a copied switch reads like the switch it was copied from rather than like
    something a generator produced. Only three things are then changed on the way
    out: the name, the references
    :attr:`~netviz.edit.operations.CopyElement.rewrite` redirects, and the
    fields two elements cannot both have.

    Re-parsed rather than deep-copied, and that is not a stylistic choice:
    ``copy.deepcopy`` of a ``ruamel`` tree drops the comments attached to
    sequence *entries*, which in an inventory is most of them — the note above a
    port, the reason a VLAN exists. Parsing the text again is the only way to get
    a tree that is independent of the source and still says everything the source
    said.

    The inverse is a delete, exact by construction for the reason a create's is:
    the document did not exist, so removing it leaves the file as it was.
    """
    located = context.locate(operation.address)
    namespace = operation.namespace if operation.namespace is not None else located.namespace
    siblings = {
        short_name(fqn) for fqn in context.inventory.elements if namespace_of(fqn) == namespace
    }
    name = operation.name or dedupe_name(
        located.element.metadata.name, siblings, suffix=operation.suffix
    )
    fqn = qualify(namespace, name)
    if fqn in context.inventory.elements:
        raise EditError(f"{fqn} already exists; a name is unique within its namespace (NG-N002)")

    source = context.tree.document(located.relative, located.index)
    if source.inline:
        raise EditError(
            f"{located.fqn} is introduced by an inline '--- key: value' marker and cannot be "
            f"copied without moving its first key; run 'netviz fmt' on {located.relative} first"
        )
    made = YamlDocument(text=source.render())
    data = made.touch()
    metadata = data.get("metadata") if isinstance(data, MutableMapping) else None
    if not isinstance(metadata, MutableMapping):
        raise EditError(  # pragma: no cover - the loader read metadata.name from here
            f"{located.fqn}: its document has no metadata block to rename, so it cannot be copied"
        )
    metadata["name"] = name

    # Before the strip, so that a reference inside a field the strip removes is
    # never looked for at a path that has just gone.
    _rewire_copy(context, operation, located, data, namespace=namespace, new_fqn=fqn)
    if not operation.keep_unique:
        strip_unique(data)

    try:
        parse_document(_plain(data))
    except SchemaError as exc:
        problems = "; ".join(str(issue) for issue in exc.issues)
        raise EditError(f"the copy of {located.fqn} does not match the schema: {problems}") from exc
    relative = choose_file(
        kind=located.element.kind,
        namespace=namespace,
        name=name,
        files=context.tree.facts(context.inventory),
        requested=operation.file,
    )
    context.tree.insert_document(relative, -1, made.render())
    return (DeleteElement(address=fqn),)


def _rewire_copy(
    context: _Context,
    operation: CopyElement,
    located: _Located,
    data: Any,
    *,
    namespace: str,
    new_fqn: str,
) -> None:
    """Point the copy's references where the copy should point them.

    Two jobs at once, and they are the same arithmetic
    :func:`_requalify` does for a move:

    * **redirection.** A reference whose target is in ``rewrite`` names the
      *copy* of that target instead — which is what makes a cloned cable join
      the cloned switches.
    * **re-spelling.** A reference that is not redirected still has to keep
      meaning what it meant: a plain name resolves outwards from the folder its
      document sits in, so a copy that lands in another namespace can silently
      start naming something else.

    Raises:
        EditError: The element is a cable and nothing was redirected, which
            would put a second cable on an interface that already has one.
    """
    before = context.index
    after = NameIndex([*context.inventory.elements, new_fqn])
    redirected = 0
    for reference in references_of(located.fqn, located.element):
        target = before.lookup(reference.target, located.namespace)
        wanted = _redirected(operation, reference.target, target)
        if wanted is not None:
            redirected += 1
        elif target is None:  # already dangling; a copy is not the place to fix it
            continue
        else:
            wanted = target
        replacement = reference_text(
            wanted, namespace=namespace, written=reference.target, index=after
        )
        if replacement != reference.target:
            rewrite_reference(data, reference, replacement)
    if isinstance(located.element, Cable) and not redirected:
        raise EditError(
            f"copying {located.fqn} on its own would land a second cable on interfaces that "
            f"already have one (NG-C001); copy the elements it joins as well and the cable "
            f"comes with them, rewired to the copies"
        )


def _redirected(operation: CopyElement, written: str, resolved: str | None) -> str | None:
    """Which copy this reference should name instead, or ``None`` for none.

    The resolved name is tried first and is the normal answer. The *written*
    form is the fallback, and it is not a nicety: copying a set writes the
    copies one at a time, so by the time the cable in a set is copied the tree
    already holds ``clone/routers/rtr-home`` beside ``routers/rtr-home`` — and
    ``rtr-home`` no longer resolves to either of them. Resolution has become
    ambiguous *because of this very batch*, and refusing to redirect there would
    leave the cloned cable joining the originals.

    The written fallback is safe because ``rewrite`` is not a guess: it was
    computed over the whole selection against the tree as it was before any of
    it was copied. A written name is accepted only when exactly one entry claims
    it, so an ambiguity that was already in the document stays an ambiguity.
    """
    if resolved is not None and resolved in operation.rewrite:
        return operation.rewrite[resolved]
    matches = [
        new
        for old, new in operation.rewrite.items()
        if old == written or short_name(old) == written or old.endswith(f"/{written}")
    ]
    return matches[0] if len(matches) == 1 else None


def _delete(context: _Context, operation: DeleteElement) -> tuple[Operation, ...] | None:
    located = context.locate(operation.address)
    _remove_elements(context, [located], cascade=operation.cascade)
    return None


def _remove_elements(
    context: _Context, targets: Sequence[_Located], *, cascade: bool
) -> CascadePlan:
    """Delete these elements, and everything that cannot outlive them.

    The set is :func:`~netviz.edit.cascade.plan_cascade`'s, which is three
    layers deep: the elements whose *structure* depends on a target, the
    annotations §21 would refuse to load without it, and the §18 geometry that
    placed it. The first two are dependents and need ``cascade``; the third
    never does, because coordinates for something that is gone are not a
    dependency — they are litter, and leaving them behind is how deleting one
    switch used to hand back a tree with eight new ``W138`` warnings in it.

    Returns:
        The plan that was carried out, so a caller can say what it did.

    Raises:
        CascadeRequired: Something depends on a target and ``cascade`` is off.
    """
    plan = plan_cascade(
        context.inventory, (located.fqn for located in targets), index=context.index
    )
    if plan.takes_more and not cascade:
        listed = ", ".join(plan.dependents)
        raise CascadeRequired(
            f"{', '.join(plan.asked)} is referred to by {listed}; "
            f"delete those too with --cascade, or change them first",
            address=targets[0].fqn,
            dependents=plan.dependents,
        )

    # Order matters once and only once: every *edit* to a surviving document is
    # made before any document is *removed*, because removing one renumbers the
    # documents after it in its file and a path computed beforehand would then
    # land on the wrong one. So the edits go first, the removals are collected
    # rather than made as they are found, and the sweep takes the highest index
    # in each file first.
    doomed = list(_drop_geometry(context, plan))
    for cleared in plan.cleared:
        _clear_reference(context, cleared)
    doomed.extend(_doomed_documents(context, plan))

    for relative, index in sorted(set(doomed), key=lambda entry: (entry[0], -entry[1])):
        context.tree.remove_document(relative, index)
    return plan


def _doomed_documents(context: _Context, plan: CascadePlan) -> Iterator[tuple[str, int]]:
    """Where every document the plan removes lives, elements and annotations alike."""
    for fqn in plan.elements:
        located = context.locate(fqn)
        yield located.relative, located.index
    for doomed in plan.annotations:
        yield _locate_annotation(context, _annotation_address(doomed.kind, doomed.fqn))


def _annotation_address(kind: str, fqn: str) -> DeleteAnnotation:
    """An annotation's fully-qualified name, as the operation that names one."""
    return DeleteAnnotation(kind=kind, name=short_name(fqn), namespace=namespace_of(fqn))


def _clear_reference(context: _Context, cleared: ClearedReference) -> None:
    """Drop one reference from a document that survives the delete.

    An element's reference is dropped through :mod:`~netviz.edit.references`,
    which checks that the raw document still reads as the model said before it
    touches it — the check that stops a value a *template* contributed from
    being edited in the document that merely inherited it. An annotation's is a
    plain path, because §21 has no templates and no inheritance.
    """
    if cleared.reference is not None:
        holder = context.locate(cleared.holder)
        drop_reference(context.document(holder), cleared.reference)
        _tidy_after_drop(context, holder, cleared.reference)
        return
    relative, index = _locate_annotation(context, _annotation_address(cleared.kind, cleared.holder))
    document = context.tree.document(relative, index).touch()
    container = get_field(document, cleared.path[:-1])
    key = cleared.path[-1]
    if isinstance(container, list) and isinstance(key, int):
        container.pop(key)
    elif isinstance(container, dict) and key in container:
        del container[key]
    _tidy_annotation(document, cleared.path)


def _tidy_annotation(document: Any, path: FieldPath) -> None:
    """Remove what a dropped annotation reference leaves behind that means nothing.

    An anchor with neither ``element`` nor ``link`` is refused by §21, and so is
    an empty ``members`` list on an area that has a selector or a rectangle. The
    key that held the reference is dropped when nothing is left in it.
    """
    parent = get_field(document, path[:-1])
    if isinstance(parent, (dict, list)) and not parent:
        unset_field(document, path[:-1])


def _drop_geometry(context: _Context, plan: CascadePlan) -> Iterator[tuple[str, int]]:
    """Take the deleted things out of every layout document that placed them (§18).

    The entries are removed from the raw document rather than rewritten through
    ``SetGeometry``, so the comments, the key order and the inline flow style of
    a hand-arranged file survive somebody deleting one switch out of forty. A
    section and a view that end up empty are dropped in turn — a view holding
    ``nodes: {}`` places nothing and says so at the top of the file.

    Yields:
        The layout documents left placing nothing at all, for the caller's own
        removal pass rather than removed here: a removal renumbers the documents
        after it in its file, so every removal a delete makes has to happen in
        one sorted sweep or two of them can land on the wrong document.
    """
    for fqn in sorted({entry.layout for entry in plan.geometry}):
        source = context.inventory.layout_sources.get(fqn)
        if source is None or source.relative is None:  # pragma: no cover - always indexed
            continue
        data = context.tree.document(source.relative, source.index).touch()
        spec = get_field(data, ("spec",))
        views = get_field(spec, ("views",)) if isinstance(spec, dict) else MISSING
        if not isinstance(views, dict):  # pragma: no cover - a layout always has views
            continue
        for entry in plan.geometry:
            if entry.layout != fqn:
                continue
            view = views.get(entry.view)
            section = view.get(entry.section) if isinstance(view, dict) else None
            if isinstance(section, dict):
                section.pop(entry.key, None)
        if _tidy_layout(data, views):
            yield source.relative, source.index


def _tidy_layout(data: Any, views: dict[str, Any]) -> bool:
    """Drop every level of a layout document the last entry left empty.

    Returns:
        ``True`` when the document now places nothing and should be removed.
    """
    for view in list(views):
        geometry = views[view]
        if not isinstance(geometry, dict):  # pragma: no cover - schema-checked on load
            continue
        for section in ("nodes", "edges", "groups"):
            if section in geometry and not geometry[section]:
                del geometry[section]
        if not set(geometry) - {"routing"}:
            del views[view]
    if views:
        return False
    spec = get_field(data, ("spec",))
    spec.pop("views", None)
    if not spec:
        data.pop("spec", None)
    return not _has_geometry(data)


def _tidy_after_drop(context: _Context, holder: _Located, reference: Reference) -> None:
    """Remove what a dropped reference leaves behind that is no longer true.

    ``spec.power.inputs`` is the only case, and it has two halves, both of them
    §17's own arithmetic rather than a policy invented here:

    * **No inputs left.** An empty list says nothing, so the key goes, and the
      ``power`` block goes with it if the list was all it said. A device that
      also declares ``powered_by`` or a draw keeps those: they are facts about
      the hardware, not about the PDU that was deleted.
    * **One input left, under ``redundant: true``.** That flag claims the device
      survives losing a feed, which needs two (``NG-E015``); with one it is a
      false statement about the network, and a delete that leaves one behind is
      a delete that silently made the inventory wrong. It goes with the feed it
      was about.
    """
    if reference.role is not ReferenceRole.POWER_INPUT:
        return
    document = context.tree.document(holder.relative, holder.index).touch()
    power = get_field(document, ("spec", "power"))
    inputs = get_field(document, ("spec", "power", "inputs"))
    if not isinstance(inputs, list) or not isinstance(power, dict):  # pragma: no cover - schema
        return
    if len(inputs) < 2 and power.get("redundant"):
        del power["redundant"]
    if not inputs:
        del power["inputs"]
        if not power:
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
    reproducible = _is_reproducible(context.tree.open(located.relative))
    text = context.tree.document(located.relative, located.index).render()
    context.tree.remove_document(located.relative, located.index)
    survived = context.tree.exists(located.relative)
    target = operation.index if operation.index is not None else -1
    position = context.tree.insert_document(relative, target, text)

    if namespace == located.namespace and (survived or reproducible):
        return (MoveElement(address=new_fqn, file=located.relative, index=located.index),)
    _repoint(context, old=located.fqn, new=new_fqn)
    _requalify(
        context, old=located.fqn, new=new_fqn, element=located.element, at=(relative, position)
    )
    return None


def _is_reproducible(file: YamlFile) -> bool:
    """Would re-creating this file from its documents alone put it back exactly?

    Only asked when a move empties its source file, which deletes it: the
    inverse then has to *make* the file again, and a file netviz makes is
    plain UTF-8 with ``\n`` line endings and nothing before its first document
    (:mod:`netviz.fsio`). A file that was any of those things differently —
    a CRLF checkout, a byte-order mark, a licence header above the first
    ``---`` — cannot be restored that way, so such a move is inverted with the
    primitive file restore instead, which carries the bytes.

    The common case is the cheap one: an LF file with no preamble round-trips,
    and keeps the semantic inverse an undo stack is nicer to read.
    """
    return file.newline == "\n" and not file.bom and not file.preamble


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
    """Rewrite everything that names ``old`` so that it names ``new``.

    Three places write a name down, and a rename that reaches only the first of
    them hands back a tree with new warnings in it:

    * a **reference** — a cable end, a tunnel's ``over``, an adapter's
      ``attached_to`` — rewritten through :mod:`~netviz.edit.references`;
    * a **layout key** (§18), which is an address used as a mapping key;
    * a **note anchor** or an **area member** (§21).

    The spelling each document used is kept wherever it still resolves, for all
    three; see :func:`~netviz.edit.references.reference_text`, which decides
    it, and :mod:`~netviz.edit.rename`, which applies that decision to the two
    that are not references.
    """
    index = context.index
    after = index.replaced(old, new)
    for reference in dependents_of(old, context.inventory.elements, index):
        if reference.source == old:  # pragma: no cover - nothing references itself
            continue
        holder = context.locate(reference.source)
        replacement = reference_text(
            new, namespace=reference.namespace, written=reference.target, index=after
        )
        rewrite_reference(context.document(holder), reference, replacement)
    _carry_arrangement(context, plan_rename(context.inventory, old=old, new=new, index=index))


def _carry_arrangement(context: _Context, plan: RenamePlan) -> None:
    """Move the renamed element's geometry and annotations onto its new name.

    Nothing is written when the plan is empty, which is the ordinary case: an
    inventory with no layout document and no note must come out of a rename
    byte-identical apart from the name itself, and in particular must not gain
    an empty ``spec.views`` block.
    """
    if plan.empty:
        return
    for layout in dict.fromkeys(entry.layout for entry in plan.geometry):
        source = context.inventory.layout_sources.get(layout)
        if source is None or source.relative is None:  # pragma: no cover - always indexed
            continue
        data = context.tree.document(source.relative, source.index).touch()
        for entry in plan.geometry:
            if entry.layout != layout:
                continue
            section = get_field(data, ("spec", "views", entry.view, entry.section))
            if isinstance(section, MutableMapping) and entry.key in section:
                _rekey(section, entry.key, entry.new_key)
    for repointed in plan.annotations:
        relative, index = _locate_annotation(
            context, _annotation_address(repointed.kind, repointed.fqn)
        )
        document = context.tree.document(relative, index).touch()
        set_field(document, repointed.path, repointed.new_written)


def _rekey(section: MutableMapping[Any, Any], key: str, new_key: str) -> None:
    """Rename one mapping key in place, keeping its position and its comment.

    Position matters because a hand-arranged layout file is read by a person:
    the keys are usually in the order the diagram was built, and re-appending
    the one that was renamed would move it to the bottom and turn a one-line
    diff into a whole-block one. ``ruamel``'s ``insert`` is what puts it back
    where it was; the comment written beside the key is carried across by hand,
    because it is filed under the key's own text.
    """
    comments = getattr(section, "ca", None)
    # A key spelled the new way already — stale geometry left by an earlier
    # element of that name — loses to the entry that places the live one, and
    # goes first so that the position computed below is the one it will land at.
    if new_key in section:
        del section[new_key]
        if comments is not None:
            comments.items.pop(new_key, None)
    position = list(section).index(key)
    value = section.pop(key)
    comment = comments.items.pop(key, None) if comments is not None else None
    insert = getattr(section, "insert", None)
    if insert is not None:
        insert(position, new_key, value)
    else:  # pragma: no cover - every touched document is a round-trip tree
        section[new_key] = value
    if comment is not None and comments is not None:
        comments.items[new_key] = comment


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


def _append(context: _Context, operation: AppendItem) -> tuple[Operation, ...] | None:
    """Add one entry to a sequence, creating the sequence if it is not there.

    The inverse is a semantic one whenever the document round-trips: removing
    the entry that was just inserted restores the file, because nothing else in
    the sequence was rewritten -- and a sequence this operation *created* is
    undone by removing the key it created, not by leaving an empty list behind.
    """
    located = context.locate(operation.address)
    path = parse_field_path(operation.path)
    if path[:1] == ("kind",) or path[:2] == ("metadata", "name"):
        raise OperationError(f"{operation.path} is not a sequence; it is part of the envelope")
    document = context.document(located)
    existing = get_field(document, path)
    created = existing is MISSING
    if created:
        set_field(document, path, [])
        existing = get_field(document, path)
    if not isinstance(existing, list):
        raise OperationError(
            f"{operation.path}: there is no sequence to add to; it holds "
            f"{'nothing' if existing is None else type(existing).__name__}"
        )
    position = len(existing) if operation.index is None else operation.index
    if not 0 <= position <= len(existing):
        raise OperationError(
            f"{operation.path}: {operation.index} is not a position in a sequence of "
            f"{len(existing)}"
        )
    existing.insert(position, operation.value)
    if not _is_faithful(context, located):
        return None
    if created:
        return (UnsetField(address=located.fqn, path=operation.path),)
    return (UnsetField(address=located.fqn, path=f"{operation.path}[{position}]"),)


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
            f"add it first with 'netviz edit add-interface'"
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
    counter — the same ladder ``netviz import`` climbs, so a tree that mixes
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
    _remove_elements(context, [located], cascade=operation.cascade)
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


# --------------------------------------------------------------------------- #
# Diagram geometry
# --------------------------------------------------------------------------- #


def _set_geometry(context: _Context, operation: SetGeometry) -> tuple[Operation, ...] | None:
    """Write, merge or drop one view of one layout document (§18).

    Three cases, in the order they are reached:

    * the document exists — its round-trip tree is edited in place, so the
      comments and the quoting of a hand-arranged file survive being re-seeded;
    * it does not, and the operation writes something — it is created, placed
      the way any new document is;
    * it does not, and the operation clears — nothing happens, because there is
      nothing to clear.

    The inverse is always the primitive file restore. A semantic one would have
    to carry the geometry that was there before, which for a four-hundred-node
    arrangement is the whole file twice over — and the file restore is exactly
    that, once, with the comments included.
    """
    located = _find_layout(context, operation)
    if located is None:
        if operation.clears:
            return ()
        return _create_layout(context, operation)

    relative, index = located
    data = context.tree.document(relative, index).touch()
    spec = _mapping_at(data, "spec")
    views = _mapping_at(spec, "views")

    if operation.clears:
        views.pop(operation.view, None)
        _drop_if_empty(context, data, spec, views, relative=relative, index=index)
        return None

    view = _mapping_at(views, operation.view)
    for section in ("nodes", "edges", "groups"):
        wanted = getattr(operation, section)
        if wanted is None:
            continue
        _merge_section(view, section, wanted)
    if operation.routing is not None:
        # The empty string is "go back to the default", which is a different
        # request from "leave it alone" and has to be able to remove the key.
        if operation.routing:
            view["routing"] = operation.routing
        else:
            view.pop("routing", None)
    if not view:
        # Reachable from a prune whose view had nothing left in it: every key it
        # held named something the inventory no longer has.
        views.pop(operation.view, None)
    _drop_if_empty(context, data, spec, views, relative=relative, index=index)
    _check_layout(data, operation)
    return None


def _set_link_geometry(
    context: _Context, operation: SetLinkGeometry
) -> tuple[Operation, ...] | None:
    """Write, replace or drop the geometry of one link (§18).

    The same three cases as :func:`_set_geometry` and the same write path — the
    document's round-trip tree is edited in place, so a comment above the cable
    somebody routed last week survives this week's drag. What differs is the
    unit: one entry in the ``edges`` section rather than three whole sections,
    so two people dragging two different cables do not overwrite each other.

    Clearing the last entry tidies up after itself: the ``edges`` section goes,
    then the view if that was all it held, then the document, then the file.
    """
    located = _find_layout(context, operation)
    entry = operation.entry
    if located is None:
        if operation.clears:
            return ()
        return _create_link_layout(context, operation)

    relative, index = located
    data = context.tree.document(relative, index).touch()
    spec = _mapping_at(data, "spec")
    views = _mapping_at(spec, "views")
    view = _mapping_at(views, operation.view)
    edges = _mapping_at(view, "edges")
    if entry:
        current = edges.get(operation.link, MISSING)
        if current is not MISSING and _equivalent(current, entry):
            return None
        if isinstance(current, MutableMapping):
            _merge_entry(current, entry)
        else:
            edges[operation.link] = as_yaml(entry)
    else:
        edges.pop(operation.link, None)
    if not edges:
        view.pop("edges", None)
    if not view:
        views.pop(operation.view, None)
    _drop_if_empty(context, data, spec, views, relative=relative, index=index)
    _check_layout(data, operation)
    return None


def _create_link_layout(context: _Context, operation: SetLinkGeometry) -> tuple[Operation, ...]:
    """Write a new layout document holding just this one routed link."""
    document = {
        "apiVersion": API_VERSION,
        "kind": LAYOUT_KIND,
        "metadata": {"name": operation.layout},
        "spec": {"views": {operation.view: {"edges": {operation.link: operation.entry}}}},
    }
    _check_layout(document, operation)
    relative = choose_file(
        kind=LAYOUT_KIND,
        namespace=operation.namespace,
        name=operation.layout,
        files=context.tree.facts(context.inventory),
        requested=operation.file,
    )
    context.tree.insert_document(relative, -1, _emit(document))
    return (
        SetLinkGeometry(
            view=operation.view,
            link=operation.link,
            layout=operation.layout,
            namespace=operation.namespace,
        ),
    )


def _drop_if_empty(
    context: _Context, data: Any, spec: Any, views: Any, *, relative: str, index: int
) -> None:
    """Tidy a layout document that no longer places anything, and remove it.

    A document holding ``spec: {views: {}}`` says nothing at all, and a file
    holding only that is a file somebody has to work out is dead. The tree
    removes the file too when this was its last document.
    """
    if views:
        return
    spec.pop("views", None)
    if not spec:
        data.pop("spec", None)
    if not _has_geometry(data):
        context.tree.remove_document(relative, index)


def _find_layout(
    context: _Context, operation: SetGeometry | SetLinkGeometry
) -> tuple[str, int] | None:
    """Where the named layout document lives, or ``None`` if there is none."""
    source = context.inventory.layout_sources.get(operation.address)
    if source is None or source.relative is None:
        return None
    return source.relative, source.index


def _create_layout(context: _Context, operation: SetGeometry) -> tuple[Operation, ...]:
    """Write a new layout document holding just this view."""
    view: dict[str, Any] = {
        section: dict(value)
        for section in ("nodes", "edges", "groups")
        if (value := getattr(operation, section))
    }
    if operation.routing:
        view["routing"] = operation.routing
    document = {
        "apiVersion": API_VERSION,
        "kind": LAYOUT_KIND,
        "metadata": {"name": operation.layout},
        "spec": {"views": {operation.view: view}},
    }
    _check_layout(document, operation)
    relative = choose_file(
        kind=LAYOUT_KIND,
        namespace=operation.namespace,
        name=operation.layout,
        files=context.tree.facts(context.inventory),
        requested=operation.file,
    )
    context.tree.insert_document(relative, -1, _emit(document))
    return (
        SetGeometry(
            view=operation.view,
            layout=operation.layout,
            namespace=operation.namespace,
        ),
    )


def _merge_section(view: Any, section: str, wanted: Mapping[str, Any]) -> None:
    """Make ``view[section]`` say ``wanted``, keeping what it already said.

    A keyed merge rather than an assignment: an entry that is still wanted is
    updated where it sits, so the comment above it and the order it was written
    in are both kept, and only the entries that are gone are removed.
    """
    if not wanted:
        view.pop(section, None)
        return
    entries = _mapping_at(view, section)
    for key in [key for key in entries if key not in wanted]:
        del entries[key]
    for key, value in wanted.items():
        current = entries.get(key, MISSING)
        if current is not MISSING and _equivalent(current, value):
            continue
        if isinstance(current, MutableMapping):
            _merge_entry(current, value)
        else:
            entries[key] = as_yaml(value)


def _merge_entry(entry: MutableMapping[str, Any], value: Any) -> None:
    """One geometry entry, updated in place so its comments stay put."""
    if not isinstance(value, Mapping):  # pragma: no cover - callers pass mappings
        return
    for key in [key for key in entry if key not in value]:
        del entry[key]
    for key, item in value.items():
        if key in entry and _equivalent(entry[key], item):
            continue
        if key in entry and isinstance(entry[key], MutableMapping) and isinstance(item, Mapping):
            _merge_entry(entry[key], item)
        else:
            entry[key] = as_yaml(item)


def _equivalent(existing: Any, wanted: Any) -> bool:
    """Do these two say the same geometry, however each is spelled?

    What keeps a re-seed of an unchanged diagram from rewriting the file — and
    what keeps a hand-written ``position: [240, 396]`` from being expanded into
    a mapping the moment anything else in the view moves.
    """
    return bool(canonical_geometry(existing) == canonical_geometry(wanted))


def _mapping_at(data: Any, key: str) -> Any:
    """``data[key]``, made an empty mapping if it is absent or not one."""
    current = data.get(key)
    if not isinstance(current, MutableMapping):
        current = {}
        data[key] = current
    return current


def _has_geometry(data: Any) -> bool:
    """Does this layout document still place anything?"""
    spec = data.get("spec")
    views = spec.get("views") if isinstance(spec, Mapping) else None
    return bool(views)


def _check_layout(data: Any, operation: SetGeometry | SetLinkGeometry) -> None:
    """Refuse geometry that is not a layout document, naming what is wrong.

    Raises:
        EditError: The result does not match the layout schema.
    """
    try:
        parse_layout(_plain(data))
    except SchemaError as exc:
        problems = "; ".join(str(issue) for issue in exc.issues)
        raise EditError(
            f"the {operation.view} geometry of {operation.address} is not valid: {problems}"
        ) from exc


def _plain(data: Any) -> Any:
    """A round-trip tree as plain Python, for the schema check.

    ``ruamel`` subclasses of ``dict`` and ``list`` validate fine, but its
    scalars do not always: a folded string or a quoted integer is its own type,
    and pydantic is right to refuse what it cannot recognise. Rebuilding the
    branch as builtins is cheap — a layout document is coordinates — and it
    means the check tests the *document*, not the parser that read it.
    """
    if isinstance(data, Mapping):
        return {str(key): _plain(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_plain(item) for item in data]
    if isinstance(data, bool):
        return bool(data)
    if isinstance(data, int):
        return int(data)
    if isinstance(data, float):
        return float(data)
    if isinstance(data, str):
        return str(data)
    return data


# --------------------------------------------------------------------------- #
# Annotations (§21)
# --------------------------------------------------------------------------- #


def _locate_annotation(
    context: _Context, operation: DeleteAnnotation | SetAnnotation
) -> tuple[str, int]:
    """Where the named annotation's document lives, as ``(file, index)``.

    Raises:
        AddressError: No annotation of that kind carries that name. Spelled as
            an address error rather than an edit error because that is what it
            is — and because the editor turns one into "nothing here" rather
            than "the write failed".
    """
    source = context.inventory.annotation_source(operation.kind, operation.address)
    if source is not None and source.relative is not None:
        return source.relative, source.index
    # The inventory may not have it *because of an earlier operation in this
    # batch*: one gesture is several writes, and an annotation is briefly
    # incoherent between two of them (see
    # :data:`~netviz.models.annotation.COHERENCE_RULE`), which drops it from
    # the reloaded index. The files still hold it, and the batch is atomic, so
    # finding it there is the honest answer rather than a workaround.
    found = context.tree.find_document(
        kind=operation.kind, name=short_name(operation.address), namespace=operation.namespace
    )
    if found is None:
        raise AddressError(
            f"there is no {operation.kind} called {operation.address!r} in this inventory",
            address=operation.address,
        )
    return found


def _annotation_document(
    *, kind: str, name: str, metadata: Mapping[str, Any], spec: Mapping[str, Any]
) -> dict[str, Any]:
    """The document a create writes, checked against §21 before it lands.

    Same reasoning as :func:`_element_document`: a rejected ``spec`` should come
    back as a rejected ``spec``, naming the field, rather than as a load error
    against a file the user never typed.
    """
    if "name" in metadata and metadata["name"] != name:
        raise OperationError(
            f"metadata.name is {metadata['name']!r} but the {kind} was created as {name!r}"
        )
    document = {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": {"name": name, **{k: v for k, v in metadata.items() if k != "name"}},
        "spec": dict(spec),
    }
    try:
        parse_annotation(document)
    except SchemaError as exc:
        problems = "; ".join(str(issue) for issue in exc.issues)
        raise EditError(f"the new {kind} {name!r} does not match the schema: {problems}") from exc
    return document


def _create_annotation(context: _Context, operation: CreateAnnotation) -> tuple[Operation, ...]:
    if operation.address in context.inventory.annotations_of(operation.kind):
        raise EditError(
            f"a {operation.kind} called {operation.address} already exists; "
            "a name is unique within its namespace and its kind (NG-G002)"
        )
    document = _annotation_document(
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
    return (
        DeleteAnnotation(kind=operation.kind, name=operation.name, namespace=operation.namespace),
    )


def _delete_annotation(
    context: _Context, operation: DeleteAnnotation
) -> tuple[Operation, ...] | None:
    relative, index = _locate_annotation(context, operation)
    context.tree.remove_document(relative, index)
    return None


def _set_annotation(context: _Context, operation: SetAnnotation) -> tuple[Operation, ...] | None:
    """Write one field of an annotation, or take it out.

    ``metadata.name`` *is* settable here, unlike on an element, and that is the
    whole rename story for §21: nothing in an inventory refers to an annotation,
    so there are no references to rewrite and no reason for a second operation.
    ``kind`` is not settable — a note is not an area with a different word on it.
    """
    relative, index = _locate_annotation(context, operation)
    path = operation.parsed_path
    if path[:1] == ("kind",):
        raise OperationError("kind is not settable: create a new annotation of the kind you want")
    if operation.unset and len(path) == 1 and path[0] in _ENVELOPE:
        raise OperationError(f"{operation.path} is part of the document envelope and is required")
    document = context.tree.document(relative, index).touch()
    previous = get_field(document, path)
    if operation.unset:
        unset_field(document, path)
    else:
        set_field(document, path, operation.value)
    _check_annotation(document, operation)
    if (
        previous is MISSING
        and not operation.unset
        and context.tree.document(relative, index).faithful
    ):
        return (
            SetAnnotation(
                kind=operation.kind,
                name=operation.name,
                namespace=operation.namespace,
                path=operation.path,
                unset=True,
            ),
        )
    return None


def _check_annotation(data: Any, operation: SetAnnotation) -> None:
    """Refuse a write that puts a *value* outside §21, naming what is wrong.

    Checked after every field write rather than only on create, because a
    ``color`` that is not a colour should come back naming the field somebody
    typed rather than as a load error against a file they never opened.

    **Coherence is deliberately not checked here** (:data:`COHERENCE_RULE`). One
    gesture is several writes: dragging a note that has never been placed writes
    ``spec.geometry.x`` and then ``spec.geometry.y``, and between the two the
    document says something §21 forbids — an ``x`` with no ``y``. Refusing there
    would make the second half of every drag unreachable. A value problem is
    never like that: ``color: red`` is wrong when it is written and wrong when
    the batch ends. So the coherence rules are left to the commit gate, which
    reloads the finished tree and reports them against the document; the batch
    is atomic, so nothing incoherent is ever left on disk either way.

    Raises:
        EditError: A value in the result does not match the annotation schema.
    """
    try:
        parse_annotation(_plain(data))
    except SchemaError as exc:
        issues = [issue for issue in exc.issues if issue.rule != COHERENCE_RULE]
        if not issues:
            return
        problems = "; ".join(str(issue) for issue in issues)
        raise EditError(
            f"{operation.path} would leave {operation.kind} {operation.address} invalid: {problems}"
        ) from exc


_Handler = Callable[[_Context, Any], "tuple[Operation, ...] | None"]


#: One applier per operation. A handler returns the semantic inverse when it has
#: one that is exact, and ``None`` to have the pre-images used instead.
_HANDLERS: Final[dict[type[Operation], _Handler]] = {
    CreateElement: _create,
    CopyElement: _copy,
    DeleteElement: _delete,
    RenameElement: _rename,
    MoveElement: _move,
    SetField: _set,
    UnsetField: _unset,
    AppendItem: _append,
    AddInterface: _add_interface,
    RemoveInterface: _remove_interface,
    Connect: _connect,
    Disconnect: _disconnect,
    SetGeometry: _set_geometry,
    SetLinkGeometry: _set_link_geometry,
    CreateAnnotation: _create_annotation,
    DeleteAnnotation: _delete_annotation,
    SetAnnotation: _set_annotation,
    WriteFile: _write_file,
    RemoveFile: _remove_file,
}
