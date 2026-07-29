"""Walking an inventory folder and turning it into an :class:`Inventory`.

Implements the discovery rules of ``docs/schema.md`` §2.1:

* ``NG-L001`` — only ``*.yaml`` / ``*.yml`` (case-insensitive) are loaded.
* ``NG-L002`` — path components starting with ``.`` or ``_`` are skipped, plus
  anything matched by a ``.netgraphignore`` (see :mod:`netgraph.loader.ignore`).
* ``NG-L003`` — symlinks are followed, but one that leaves the root or revisits
  a directory is an error.
* ``NG-L004`` — a file may hold several documents; empty ones are skipped.
* ``NG-L005`` — files are loaded in byte-wise order of their relative POSIX
  path, documents in file order, which makes every later stage deterministic.

The walk itself is iterative: an inventory nested a thousand directories deep
is unusual but should not hit the interpreter's recursion limit.

Building the elements is two-phase, and templates (§6.6) are why. A device may
name a template declared in a file that sorts after it, so a document carrying
``spec.from`` cannot be turned into an element as it is read. Deferring those
documents to the end of the walk would reorder the inventory, and load order is
what makes every rendering deterministic — so :class:`_Builder` keeps one *slot*
per document, in document order, and fills the deferred ones before anything is
indexed. An inventory written with templates therefore renders byte for byte
like the same inventory written out longhand.
"""

from __future__ import annotations

import gc
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from netgraph.errors import LoaderError, SchemaError, SchemaIssue
from netgraph.loader.documents import (
    RawDocument,
    YamlSyntaxError,
    parse_documents,
    read_documents,
)
from netgraph.loader.ignore import IGNORE_FILE_NAME, IgnoreStack, parse_ignore_file
from netgraph.loader.inventory import Inventory, LoadError, SourceLocation, qualify
from netgraph.loader.provenance import FieldPath, Provenance, Site
from netgraph.loader.templates import INHERIT_KEY, TemplateRegistry, resolved_spec
from netgraph.models import DEVICE_KINDS, TEMPLATE_KIND, Element, parse_document, parse_template

__all__ = [
    "STREAM_NAME",
    "YAML_SUFFIXES",
    "InventoryFile",
    "iter_inventory_files",
    "load_stream",
    "load_tree",
]

#: ``NG-L001`` — the suffixes a document may use, compared case-insensitively.
YAML_SUFFIXES: tuple[str, ...] = (".yaml", ".yml")

#: ``NG-L002`` — a path component starting with one of these is not descended into.
_SKIPPED_PREFIXES: tuple[str, ...] = (".", "_")


@dataclass(frozen=True, slots=True)
class InventoryFile:
    """A YAML file discovered in the tree."""

    #: Absolute path on disk.
    path: Path
    #: The same file relative to the inventory root, POSIX style.
    relative: PurePosixPath

    @property
    def namespace(self) -> str:
        """Implicit namespace: the directory holding the file (``""`` at the root)."""
        parent = self.relative.parent.as_posix()
        return "" if parent == "." else parent

    @property
    def sort_key(self) -> bytes:
        """``NG-L005`` — byte-wise order of the relative POSIX path."""
        return self.relative.as_posix().encode("utf-8", "surrogatepass")


@dataclass(frozen=True, slots=True)
class _Pending:
    """A directory queued for traversal."""

    path: Path
    relative: PurePosixPath
    ignores: IgnoreStack
    #: Real paths of the directories on the way here, for cycle detection.
    chain: frozenset[Path]


def load_tree(root: Path) -> Inventory:
    """Load every YAML document below ``root`` into an :class:`Inventory`.

    Each document is validated against the schema of its ``kind`` and indexed
    under a fully-qualified name derived from its directory, so
    ``sites/berlin/rack1/sw1.yaml`` declaring ``name: sw1`` is registered as
    ``sites/berlin/rack1/sw1``. References resolve namespace-first and then
    globally; see :meth:`~netgraph.loader.inventory.Inventory.lookup`.

    Loading is *total*: an unreadable file, a YAML syntax error, a schema
    violation or a duplicate name is recorded in
    :attr:`~netgraph.loader.inventory.Inventory.errors` and the walk continues,
    so the user sees every problem in one run.

    Args:
        root: Directory to walk. A single YAML file is also accepted and loaded
            into the root namespace, which makes ``netgraph`` usable on one file.

    Returns:
        The populated inventory, possibly holding errors.

    Raises:
        LoaderError: ``root`` does not exist, or is neither a directory nor a
            YAML file. Everything a user can get wrong *inside* the tree is
            reported through :attr:`Inventory.errors` instead.
    """
    inventory = Inventory(root=root)
    builder = _Builder(inventory)
    with _deferred_gc():
        for entry in iter_inventory_files(root, errors=inventory.errors):
            _load_file(entry, builder)
        builder.finish()
    return inventory


#: The file name a stream with no file of its own is reported under. It appears
#: in every diagnostic the stream produces, so it is spelled like the path it
#: stands in for rather than like a placeholder.
STREAM_NAME: Final = "stream.yaml"


def load_stream(text: str, *, name: str = STREAM_NAME) -> Inventory:
    """Load a YAML document stream that never was a folder.

    One stream is one file's worth of documents, so every element lands in the
    root namespace and references resolve globally. Apart from that the rules
    are the ones :func:`load_tree` applies -- the same strict parser, the same
    schema validation, the same ``NG-N002`` duplicate check -- because the
    interactive front ends exist to tell a user what ``netgraph validate``
    would say about the very same text.

    Loading is total here too: a syntax error or a rejected document is
    recorded on :attr:`~netgraph.loader.inventory.Inventory.errors` and the
    remaining documents are still loaded, which is what keeps a diagram on
    screen while the document under the cursor is half typed.

    Args:
        text: The whole stream, ``---`` separators included.
        name: File name to report problems under. Nothing is opened.

    Returns:
        The populated inventory, possibly holding errors.
    """
    entry = InventoryFile(path=Path(name), relative=PurePosixPath(name))
    inventory = Inventory(root=Path(name))
    builder = _Builder(inventory)
    try:
        for document in parse_documents(text, path=entry.path, relative=entry.relative):
            builder.feed(document, entry=entry)
    except YamlSyntaxError as exc:
        inventory.record(
            LoadError(
                message=str(exc),
                path=entry.path,
                relative=name,
                line=exc.line,
                column=exc.column,
            )
        )
    builder.finish()
    return inventory


@contextmanager
def _deferred_gc() -> Iterator[None]:
    """Hold the cyclic collector off for the duration of one load.

    Loading is the worst possible shape for a generational collector: it
    allocates millions of short-lived objects — YAML node trees and the
    mappings they construct, thrown away one document at a time — while the set
    of *live* objects, the elements themselves, only grows. Every collection is
    therefore a full walk of an ever-larger graph that is almost entirely
    reachable, and there are hundreds of them. On the 1056-device benchmark
    tree that is 86 ms of a 483 ms load, about 18 %.

    Nothing here needs collecting: the garbage is node trees, dicts, lists and
    strings, none of which can form a reference cycle, so plain reference
    counting frees it the moment it goes out of scope — which is why peak RSS
    is unchanged (measured: 64 MB either way). The collector is only being
    asked not to keep looking for cycles that are not there.

    The state is captured and restored, so a caller that had already disabled
    the collector keeps it disabled, and an exception mid-walk does not leave
    it off.
    """
    enabled = gc.isenabled()
    if enabled:
        gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def iter_inventory_files(
    root: Path, *, errors: list[LoadError] | None = None
) -> list[InventoryFile]:
    """Discover every loadable YAML file below ``root``, in load order.

    Problems that affect a single directory — an unreadable folder, a symlink
    loop, a broken ``.netgraphignore`` — are appended to ``errors`` when a list
    is given and dropped otherwise, so discovery never aborts halfway. They are
    sorted by path first, which keeps the report identical from run to run even
    though the traversal order depends on the file system.

    Raises:
        LoaderError: ``root`` is missing or is not a directory or YAML file.
    """
    if not root.exists():
        raise LoaderError(f"inventory path does not exist: {root}")
    if root.is_file():
        if not _is_yaml_name(root.name):
            raise LoaderError(
                f"inventory path is not a directory or a YAML file: {root}",
            )
        return [InventoryFile(path=root, relative=PurePosixPath(root.name))]
    if not root.is_dir():
        raise LoaderError(f"inventory path is not a directory: {root}")

    real_root = root.resolve()
    found: list[InventoryFile] = []
    problems: list[LoadError] = []
    visited: set[Path] = {real_root}
    stack = [
        _Pending(
            path=root,
            relative=PurePosixPath(),
            ignores=IgnoreStack(),
            chain=frozenset({real_root}),
        )
    ]
    while stack:
        pending = stack.pop()
        ignores = _push_ignore_file(pending, problems)
        for entry in _scan(pending.path, problems):
            child = _classify(
                entry,
                pending=pending,
                ignores=ignores,
                real_root=real_root,
                visited=visited,
                problems=problems,
            )
            if isinstance(child, InventoryFile):
                found.append(child)
            elif child is not None:
                stack.append(child)

    found.sort(key=lambda item: item.sort_key)
    if errors is not None:
        errors.extend(sorted(problems, key=lambda problem: (problem.location, problem.message)))
    return found


# -- discovery ------------------------------------------------------------


def _scan(directory: Path, problems: list[LoadError]) -> list[os.DirEntry[str]]:
    """List ``directory``, recording (rather than raising) an OS error."""
    try:
        with os.scandir(directory) as entries:
            # Sorted so that the traversal, and therefore the report, does not
            # depend on the order the file system happens to hand entries back.
            return sorted(entries, key=lambda entry: entry.name)
    except OSError as exc:
        problems.append(
            LoadError(
                message=f"cannot read directory: {exc.strerror or exc}",
                path=directory,
            ),
        )
        return []


def _push_ignore_file(pending: _Pending, problems: list[LoadError]) -> IgnoreStack:
    """Add this directory's ``.netgraphignore`` to the stack, if it has one."""
    candidate = pending.path / IGNORE_FILE_NAME
    base = pending.relative.as_posix()
    base = "" if base == "." else base
    try:
        if not candidate.is_file():
            return pending.ignores
        return pending.ignores.push(parse_ignore_file(candidate, base=base))
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(
            LoadError(
                message=f"cannot read {IGNORE_FILE_NAME}: {exc}",
                path=candidate,
                relative=_relative_text(pending.relative / IGNORE_FILE_NAME),
            ),
        )
        return pending.ignores


def _classify(
    entry: os.DirEntry[str],
    *,
    pending: _Pending,
    ignores: IgnoreStack,
    real_root: Path,
    visited: set[Path],
    problems: list[LoadError],
) -> InventoryFile | _Pending | None:
    """Decide what a directory entry is: a file to load, a directory, or noise."""
    name = entry.name
    if name.startswith(_SKIPPED_PREFIXES):  # NG-L002
        return None

    relative = pending.relative / name
    path = Path(entry.path)
    try:
        is_dir = entry.is_dir(follow_symlinks=True)
        is_file = entry.is_file(follow_symlinks=True)
    except OSError as exc:  # pragma: no cover - broken symlink, races
        problems.append(
            LoadError(
                message=f"cannot stat entry: {exc.strerror or exc}",
                path=path,
                relative=_relative_text(relative),
            ),
        )
        return None

    if not is_dir and not is_file:
        return None
    if ignores.is_ignored(relative.as_posix(), is_dir=is_dir):
        return None
    if not is_dir:
        if not _is_yaml_name(name):  # NG-L001
            return None
        if entry.is_symlink() and not _within_root(path, real_root, relative, problems):
            return None
        return InventoryFile(path=path, relative=relative)

    real = _real_path(path)
    if real is None or not _within_root(path, real_root, relative, problems, real=real):
        return None
    if real in pending.chain:  # NG-L003
        problems.append(
            LoadError(
                message=f"symbolic link forms a cycle: {relative.as_posix()} -> {real}",
                path=path,
                relative=_relative_text(relative),
            ),
        )
        return None
    if real in visited:
        problems.append(
            LoadError(
                message=(
                    f"directory {relative.as_posix()} resolves to {real}, which is already "
                    "part of the inventory; loading it twice would duplicate every element"
                ),
                path=path,
                relative=_relative_text(relative),
            ),
        )
        return None

    visited.add(real)
    return _Pending(
        path=path,
        relative=relative,
        ignores=ignores,
        chain=pending.chain | {real},
    )


def _real_path(path: Path) -> Path | None:
    """``Path.resolve`` that survives a dangling or looping symlink."""
    try:
        return path.resolve(strict=True)
    except OSError:  # pragma: no cover - dangling link, ELOOP
        return None


def _within_root(
    path: Path,
    real_root: Path,
    relative: PurePosixPath,
    problems: list[LoadError],
    *,
    real: Path | None = None,
) -> bool:
    """``NG-L003`` — refuse a link that points outside the inventory root."""
    resolved = real if real is not None else _real_path(path)
    if resolved is None:  # pragma: no cover - the link broke between stat and resolve
        return False
    if resolved.is_relative_to(real_root):
        return True
    problems.append(
        LoadError(
            message=(
                f"symbolic link {relative.as_posix()} escapes the inventory root "
                f"(points at {resolved})"
            ),
            path=path,
            relative=_relative_text(relative),
        ),
    )
    return False


def _is_yaml_name(name: str) -> bool:
    """``NG-L001`` — case-insensitive suffix test."""
    return name.lower().endswith(YAML_SUFFIXES)


# -- parsing --------------------------------------------------------------


def _load_file(entry: InventoryFile, builder: _Builder) -> None:
    """Parse one file and hand every document it holds to ``builder``."""
    relative = entry.relative.as_posix()
    try:
        for document in read_documents(entry.path, relative=entry.relative):
            builder.feed(document, entry=entry)
    except YamlSyntaxError as exc:
        builder.inventory.record(
            LoadError(
                message=str(exc),
                path=entry.path,
                relative=relative,
                line=exc.line,
                column=exc.column,
            )
        )
    except OSError as exc:
        builder.inventory.record(
            LoadError(
                message=f"cannot read file: {exc.strerror or exc}",
                path=entry.path,
                relative=relative,
            )
        )


# --------------------------------------------------------------------------- #
# Building elements out of documents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Ready:
    """A document that is already an element and only needs indexing."""

    element: Element
    source: SourceLocation
    namespace: str


@dataclass(frozen=True, slots=True)
class _Rejected:
    """A document that failed, with the diagnostics it produced."""

    errors: tuple[LoadError, ...]


@dataclass(frozen=True, slots=True)
class _Deferred:
    """A device that names a template and cannot be built until the walk ends."""

    document: RawDocument
    entry: InventoryFile
    reference: Any


_Slot = _Ready | _Rejected | _Deferred


@dataclass(eq=False)
class _Builder:
    """Turns a stream of parsed documents into a populated :class:`Inventory`.

    Templates (§6.6) make loading two-phase: a device may name a template
    declared in a file that sorts after it, so a document carrying ``spec.from``
    cannot be built as it is read. Rather than defer such documents to the end —
    which would reorder the inventory, and with it every rendered diagram — the
    builder keeps one *slot* per document, in document order, and fills the
    deferred ones in :meth:`finish` before anything is indexed. The result is
    byte-identical to the same inventory written out longhand.

    Only the deferred documents keep their YAML node tree alive; everything else
    is validated during the walk and drops it, which is what keeps the memory
    profile of a template-free inventory exactly what it was.
    """

    inventory: Inventory
    templates: TemplateRegistry = field(default_factory=TemplateRegistry)
    _slots: list[_Slot] = field(default_factory=list)

    # -- phase one: the walk ---------------------------------------------

    def feed(self, document: RawDocument, *, entry: InventoryFile) -> None:
        """Take one parsed document."""
        if document.data is None:  # NG-L004: an empty document is not an error.
            return
        if _kind_of(document.data) == TEMPLATE_KIND:
            self._add_template(document, entry)
            return

        reference = _inherit_reference(document.data)
        if reference is None:
            self._slots.append(self._build(document, entry))
        elif _kind_of(document.data) in DEVICE_KINDS:
            self._slots.append(_Deferred(document=document, entry=entry, reference=reference))
        else:
            self._slots.append(
                _rejected(  # NG-M006
                    document,
                    path=("spec", INHERIT_KEY),
                    message=(
                        "'from' inherits a device template and is only supported by "
                        f"{', '.join(DEVICE_KINDS)}"
                    ),
                    rule="NG-M006",
                )
            )

    def _add_template(self, document: RawDocument, entry: InventoryFile) -> None:
        """Register a ``kind: template`` document, or record why it is unusable."""
        try:
            template = parse_template(document.data, source=document.source)
        except SchemaError as exc:
            for error in _schema_errors(exc, document):
                self.inventory.record(error)
            return
        if self.templates.add(template, document=document, namespace=entry.namespace) is None:
            fqn = qualify(entry.namespace, template.metadata.name)
            first = self.templates.source_of(fqn)
            where = f" (first declared at {Site(first, ())})" if first is not None else ""
            self.inventory.record(
                _error_at(
                    Site(document, ("metadata", "name")),
                    SchemaIssue(
                        path=("metadata", "name"),
                        message=(
                            f"duplicate template name {fqn!r}{where}; this document is ignored"
                        ),
                        rule="NG-M002",
                    ),
                )
            )

    # -- phase two: indexing ---------------------------------------------

    def finish(self) -> None:
        """Resolve the templates, build the deferred devices, index everything."""
        for document, issue in self.templates.resolve_all():
            self.inventory.record(_error_at(Site(document, issue.path), issue))

        for slot in self._slots:
            built = self._build_inherited(slot) if isinstance(slot, _Deferred) else slot
            if isinstance(built, _Rejected):
                for error in built.errors:
                    self.inventory.record(error)
                continue
            assert isinstance(built, _Ready)
            self._index(built)
        self._slots.clear()

    def _index(self, slot: _Ready) -> None:
        fqn = self.inventory.add(slot.element, namespace=slot.namespace, source=slot.source)
        if fqn is not None:
            return
        name = slot.element.metadata.name
        qualified = qualify(slot.namespace, name)
        first = self.inventory.source_of(qualified)
        where = f" (first declared at {first})" if first is not None else ""
        self.inventory.record(
            LoadError(
                message=f"duplicate element name {qualified!r}{where}; this document is ignored",
                path=slot.source.path,
                relative=slot.source.relative,
                line=slot.source.line,
                index=slot.source.index,
                field_path=("metadata", "name"),
                rule="NG-N002",
            )
        )

    # -- document -> element ---------------------------------------------

    def _build(self, document: RawDocument, entry: InventoryFile) -> _Slot:
        """Expand ranges and validate one document that inherits nothing."""
        data, provenance, issues = _expanded(document)
        if issues:
            return _Rejected(errors=tuple(_located(issues, provenance)))
        return self._validate(data, document=document, entry=entry, provenance=provenance)

    def _build_inherited(self, slot: _Deferred) -> _Slot:
        """Merge the named template underneath one device, then validate it."""
        document = slot.document
        data = document.data
        spec = data.get("spec") if isinstance(data, Mapping) else None
        assert isinstance(spec, Mapping)  # _inherit_reference found 'from' inside it

        merged, provenance, issues, template_fqn = self.templates.merge_into(
            spec,
            reference=slot.reference,
            namespace=slot.entry.namespace,
            provenance=Provenance(base=document),
        )
        if issues:
            return _Rejected(errors=tuple(_located(issues, provenance)))
        return self._validate(
            {**data, "spec": merged},
            document=document,
            entry=slot.entry,
            provenance=provenance,
            note=_inherited_note(data, template_fqn),
        )

    def _validate(
        self,
        data: Any,
        *,
        document: RawDocument,
        entry: InventoryFile,
        provenance: Provenance,
        note: str = "",
    ) -> _Slot:
        try:
            element = parse_document(data, source=document.source)
        except SchemaError as exc:
            if not exc.issues:
                return _Rejected(errors=(_whole_document_error(exc, document),))
            return _Rejected(errors=tuple(_located(exc.issues, provenance, note=note)))
        return _Ready(
            element=element,
            source=SourceLocation(
                path=entry.path,
                relative=entry.relative.as_posix(),
                index=document.index,
                line=document.line,
            ),
            namespace=entry.namespace,
        )


def _rejected(document: RawDocument, *, path: FieldPath, message: str, rule: str) -> _Rejected:
    """A slot holding one diagnostic located in the document as it was written."""
    return _Rejected(
        errors=(
            _error_at(Site(document, path), SchemaIssue(path=path, message=message, rule=rule)),
        )
    )


def _kind_of(data: Any) -> Any:
    return data.get("kind") if isinstance(data, Mapping) else None


def _inherit_reference(data: Any) -> Any:
    """``spec.from``, or ``None`` when the document does not inherit."""
    if not isinstance(data, Mapping):
        return None
    spec = data.get("spec")
    if not isinstance(spec, Mapping):
        return None
    return spec.get(INHERIT_KEY)


def _inherited_note(data: Any, template_fqn: str | None) -> str:
    """The clause appended to a diagnostic that lands in a template's file."""
    if template_fqn is None:  # pragma: no cover - a merge that succeeded has one
        return ""
    metadata = data.get("metadata") if isinstance(data, Mapping) else None
    name = metadata.get("name") if isinstance(metadata, Mapping) else None
    who = f"{name!r}" if isinstance(name, str) else "a device"
    return f" (inherited by {who} through 'spec.from: {template_fqn}')"


def _expanded(document: RawDocument) -> tuple[Any, Provenance, list[SchemaIssue]]:
    """Expand the interface ranges of one document, if it declares any."""
    provenance = Provenance(base=document)
    data = document.data
    spec = data.get("spec") if isinstance(data, Mapping) else None
    if not isinstance(spec, Mapping):
        return data, provenance, []
    body, provenance, issues = resolved_spec(spec, provenance=provenance)
    if issues:
        return data, provenance, issues
    if body is spec:
        return data, provenance, []
    return {**data, "spec": body}, provenance, []


def _located(
    issues: Iterator[SchemaIssue] | tuple[SchemaIssue, ...] | list[SchemaIssue],
    provenance: Provenance,
    *,
    note: str = "",
) -> Iterator[LoadError]:
    """One :class:`LoadError` per issue, pointed at the file that wrote the field."""
    for issue in issues:
        site = provenance.locate(issue.path)
        inherited = site.document is not provenance.base
        yield _error_at(site, issue, note=note if inherited else "")


def _error_at(site: Site, issue: SchemaIssue, *, note: str = "") -> LoadError:
    """A :class:`LoadError` for one issue at one resolved location."""
    return LoadError(
        message=f"{issue.message}{note}",
        path=site.file,
        relative=site.relative,
        line=site.line,
        index=site.index,
        field_path=site.path,
        rule=issue.rule,
    )


def _schema_errors(error: SchemaError, document: RawDocument) -> Iterator[LoadError]:
    """Locate the issues of a :class:`SchemaError` within one unrewritten document."""
    if not error.issues:
        yield _whole_document_error(error, document)
        return
    yield from _located(error.issues, Provenance(base=document))


def _whole_document_error(error: SchemaError, document: RawDocument) -> LoadError:
    return LoadError(
        message=str(error),
        path=document.path,
        relative=document.relative.as_posix(),
        line=document.line,
        index=document.index,
    )


def _relative_text(relative: PurePosixPath) -> str:
    text = relative.as_posix()
    return "" if text == "." else text
