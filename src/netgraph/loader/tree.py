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

Nothing above depends on where a document came from, which is what lets
:mod:`netgraph.loader.cache` short-circuit it: a file whose bytes have been seen
before is replayed as the slots it produced last time, in the same order, with
the same diagnostics, and the builder cannot tell the difference. Templates are
the exception and are never cached — see :meth:`_Builder.harvest`.
"""

from __future__ import annotations

import gc
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from netgraph.errors import LoaderError, SchemaError, SchemaIssue
from netgraph.loader.cache import CachedFile, CachedSlot, DocumentCache
from netgraph.loader.documents import (
    RawDocument,
    YamlSyntaxError,
    decode_text,
    parse_documents,
)
from netgraph.loader.ignore import IGNORE_FILE_NAME, IgnoreStack, parse_ignore_file
from netgraph.loader.inventory import Inventory, LoadError, SourceLocation, qualify
from netgraph.loader.provenance import FieldPath, Provenance, Site
from netgraph.loader.templates import INHERIT_KEY, TemplateRegistry, resolved_spec
from netgraph.models import (
    ANNOTATION_DOCUMENT_KINDS,
    DEVICE_KINDS,
    LAYOUT_KIND,
    TEMPLATE_KIND,
    TEST_SUITE_KIND,
    Element,
    parse_annotation,
    parse_document,
    parse_layout,
    parse_template,
    parse_test_suite,
)

__all__ = [
    "STREAM_NAME",
    "YAML_SUFFIXES",
    "InventoryFile",
    "Overlay",
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
class Overlay:
    """Files to read from memory instead of from the disk.

    :mod:`netgraph.edit` has to answer one question before it writes anything:
    *would* this change leave the tree loadable, and would it introduce an error
    the tree does not already have? The only honest way to answer it is to load
    the tree as it would be after the write — which must not mean writing it
    first. An overlay is that: the same walk, the same discovery rules, the same
    ordering, with a handful of paths answered from a string.

    ``files`` maps a POSIX path relative to the inventory root to the text that
    file should be taken to hold, or to ``None`` when it should be taken not to
    exist. A path the walk never reaches is *added* to it, in the position
    ``NG-L005`` puts it, so a document written into a new folder is loaded into
    the namespace that folder names.
    """

    files: Mapping[str, str | None]

    def applies_to(self, relative: str) -> bool:
        return relative in self.files

    def text_for(self, relative: str) -> str | None:
        return self.files.get(relative)

    def apply(self, entries: list[InventoryFile], root: Path) -> list[InventoryFile]:
        """``entries`` with the overlay's deletions removed and its files added."""
        kept = [
            entry for entry in entries if self.files.get(entry.relative.as_posix(), "") is not None
        ]
        known = {entry.relative.as_posix() for entry in kept}
        for relative, text in self.files.items():
            if text is None or relative in known:
                continue
            kept.append(
                InventoryFile(path=root / PurePosixPath(relative), relative=PurePosixPath(relative))
            )
        kept.sort(key=lambda entry: entry.sort_key)
        return kept


@dataclass(frozen=True, slots=True)
class _Pending:
    """A directory queued for traversal."""

    path: Path
    relative: PurePosixPath
    ignores: IgnoreStack
    #: Real paths of the directories on the way here, for cycle detection.
    chain: frozenset[Path]


def load_tree(
    root: Path,
    *,
    keep_provenance: bool = False,
    cache: DocumentCache | None = None,
    overlay: Overlay | None = None,
) -> Inventory:
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
        keep_provenance: Record, on every element's
            :class:`~netgraph.loader.inventory.SourceLocation`, which file and
            field each of its values came from, so a *semantic* finding can be
            reported at the line and column that caused it rather than at the
            top of the document.

            Off by default because the redirect tables hold the YAML node trees
            alive for the lifetime of the inventory: measured on a 628-element
            tree, that is 18 MB retained instead of 5 MB. Only the machine-
            readable ``netgraph validate`` formats need it, and they ask.
        cache: A store of already-parsed files
            (:mod:`netgraph.loader.cache`), or ``None`` to parse everything.
            Every file is still read and hashed, so the tree on disk remains the
            only state that decides the result; what is skipped is turning bytes
            that have been seen before back into elements.

            Ignored when ``keep_provenance`` is set: provenance *is* the YAML
            node tree, and a cache that stored those would defeat the reason
            they are dropped.
        overlay: Files to read from memory rather than from disk
            (:class:`Overlay`), or ``None`` to load what is there. An overlaid
            file never consults the cache — its bytes are not the bytes on disk,
            so an entry keyed by them would be a lie about a file that exists.

    Returns:
        The populated inventory, possibly holding errors.

    Raises:
        LoaderError: ``root`` does not exist, or is neither a directory nor a
            YAML file. Everything a user can get wrong *inside* the tree is
            reported through :attr:`Inventory.errors` instead.
    """
    inventory = Inventory(root=root)
    builder = _Builder(inventory, keep_provenance=keep_provenance)
    store = None if keep_provenance else cache
    with _deferred_gc():
        entries = iter_inventory_files(root, errors=inventory.errors)
        if overlay is not None:
            entries = overlay.apply(entries, root)
        for entry in entries:
            _load_file(entry, builder, store, overlay)
        builder.finish()
    if store is not None:
        store.flush()
    return inventory


#: The file name a stream with no file of its own is reported under. It appears
#: in every diagnostic the stream produces, so it is spelled like the path it
#: stands in for rather than like a placeholder.
STREAM_NAME: Final = "stream.yaml"


def load_stream(text: str, *, name: str = STREAM_NAME, keep_provenance: bool = False) -> Inventory:
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
        keep_provenance: As :func:`load_tree`.

    Returns:
        The populated inventory, possibly holding errors.
    """
    entry = InventoryFile(path=Path(name), relative=PurePosixPath(name))
    inventory = Inventory(root=Path(name))
    builder = _Builder(inventory, keep_provenance=keep_provenance)
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


def _load_file(
    entry: InventoryFile,
    builder: _Builder,
    cache: DocumentCache | None = None,
    overlay: Overlay | None = None,
) -> None:
    """Turn one file into slots, from the cache when its bytes are known.

    The file is read either way — its bytes are the key — so a cache hit saves
    the parse and the model validation, not the ``read``. That is where the time
    is: on the benchmark tree the read is 2 ms of a 440 ms load.
    """
    relative = entry.relative.as_posix()
    if overlay is not None and overlay.applies_to(relative):
        text = overlay.text_for(relative)
        if text is not None:  # ``None`` is filtered out by ``Overlay.apply``.
            _parse_text(text, entry, builder)
        return
    try:
        content = entry.path.read_bytes()
    except OSError as exc:
        builder.inventory.record(
            LoadError(
                message=f"cannot read file: {exc.strerror or exc}",
                path=entry.path,
                relative=relative,
            )
        )
        return

    if cache is None:
        _parse_file(content, entry, builder)
        return

    key = cache.key_for(relative, content)
    cached = cache.get(key, path=entry.path, relative=relative)
    if cached is not None:
        builder.replay(cached, entry=entry)
        return

    mark = builder.mark()
    _parse_file(content, entry, builder)
    produced = builder.harvest(mark)
    if produced is None:
        cache.not_cacheable()
    else:
        cache.put(key, produced, path=entry.path)


def _parse_file(content: bytes, entry: InventoryFile, builder: _Builder) -> None:
    """Parse one file's bytes and hand every document it holds to ``builder``.

    Decoding happens inside the same guard as parsing, because "these bytes are
    not UTF-8" is reported the same way as "this text is not YAML": as a
    :class:`LoadError` against the file, never as an exception that would end
    the walk.
    """
    _feed(lambda: decode_text(content, entry.path), entry, builder)


def _parse_text(text: str, entry: InventoryFile, builder: _Builder) -> None:
    """Parse one file's decoded text and hand every document it holds to ``builder``."""
    _feed(lambda: text, entry, builder)


def _feed(source: Callable[[], str], entry: InventoryFile, builder: _Builder) -> None:
    """Decode, parse and feed one file, recording whatever went wrong."""
    try:
        for document in parse_documents(source(), path=entry.path, relative=entry.relative):
            builder.feed(document, entry=entry)
    except YamlSyntaxError as exc:
        builder.inventory.record(
            LoadError(
                message=str(exc),
                path=entry.path,
                relative=entry.relative.as_posix(),
                line=exc.line,
                column=exc.column,
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


@dataclass(frozen=True, slots=True)
class _Mark:
    """How far a builder had got before one file was fed to it.

    Everything a file contributes is appended, so a pair of marks delimits it
    exactly — which is what lets the cache be written by the builder rather than
    by a second parser that would have to agree with it.
    """

    slots: int
    errors: int
    templates: int
    layouts: int
    suites: int
    annotations: int


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
    profile of a template-free inventory exactly what it was. ``keep_provenance``
    is the one way to opt out of that, and it is off by default for exactly that
    reason -- see :func:`load_tree`.
    """

    inventory: Inventory
    templates: TemplateRegistry = field(default_factory=TemplateRegistry)
    #: Hand each element's redirect table to its :class:`SourceLocation`, so a
    #: diagnostic can later be narrowed from the document to the field.
    keep_provenance: bool = False
    _slots: list[_Slot] = field(default_factory=list)
    #: How many ``kind: template`` documents have been read. Only used to answer
    #: "did this file declare one?" in :meth:`harvest`.
    _templates_seen: int = 0
    #: The same count for ``kind: layout``; see :meth:`harvest`.
    _layouts_seen: int = 0
    #: The same count for ``kind: testsuite``; see :meth:`harvest`.
    _suites_seen: int = 0
    #: The same count for the three annotation kinds of §21; see :meth:`harvest`.
    _annotations_seen: int = 0

    # -- phase one: the walk ---------------------------------------------

    def feed(self, document: RawDocument, *, entry: InventoryFile) -> None:
        """Take one parsed document."""
        if document.data is None:  # NG-L004: an empty document is not an error.
            return
        kind = _kind_of(document.data)
        if kind == TEMPLATE_KIND:
            self._add_template(document, entry)
            return
        if kind == LAYOUT_KIND:
            self._add_layout(document, entry)
            return
        if kind == TEST_SUITE_KIND:
            self._add_test_suite(document, entry)
            return
        if kind in ANNOTATION_DOCUMENT_KINDS:
            self._add_annotation(document, entry)
            return

        reference = _inherit_reference(document.data)
        if reference is None:
            self._slots.append(self._build(document, entry))
        elif kind in DEVICE_KINDS:
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
        self._templates_seen += 1
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

    # -- the cache ---------------------------------------------------------

    def _add_layout(self, document: RawDocument, entry: InventoryFile) -> None:
        """Register a ``kind: layout`` document, or record why it is unusable.

        Indexed as it is read rather than deferred like an element, because a
        layout inherits nothing and refers to nothing that has to exist yet: a
        key naming an element declared in a file that sorts later is normal, and
        whether it names anything at all is the validator's question
        (``NG-Y001``), not the loader's.
        """
        self._layouts_seen += 1
        try:
            layout = parse_layout(document.data, source=document.source)
        except SchemaError as exc:
            for error in _schema_errors(exc, document):
                self.inventory.record(error)
            return
        source = SourceLocation(
            path=entry.path,
            relative=entry.relative.as_posix(),
            index=document.index,
            line=document.line,
        )
        if self.inventory.add_layout(layout, namespace=entry.namespace, source=source) is None:
            fqn = qualify(entry.namespace, layout.metadata.name)
            first = self.inventory.layout_sources.get(fqn)
            where = f" (first declared at {first})" if first is not None else ""
            self.inventory.record(
                LoadError(
                    message=f"duplicate layout name {fqn!r}{where}; this document is ignored",
                    path=source.path,
                    relative=source.relative,
                    line=source.line,
                    index=source.index,
                    field_path=("metadata", "name"),
                    rule="NG-Y002",
                )
            )

    def _add_test_suite(self, document: RawDocument, entry: InventoryFile) -> None:
        """Register a ``kind: testsuite`` document, or record why it is unusable.

        Indexed as it is read, like a layout: a suite refers to elements by name
        and whether those names resolve is ``netgraph test``'s question rather
        than the loader's. Its provenance is kept unconditionally — a failing
        assertion has to be able to name its own file and line so an editor can
        jump to it, there are never many suites, and their documents are small.
        """
        self._suites_seen += 1
        try:
            suite = parse_test_suite(document.data, source=document.source)
        except SchemaError as exc:
            for error in _schema_errors(exc, document):
                self.inventory.record(error)
            return
        source = SourceLocation(
            path=entry.path,
            relative=entry.relative.as_posix(),
            index=document.index,
            line=document.line,
            provenance=Provenance(base=document),
        )
        if self.inventory.add_test_suite(suite, namespace=entry.namespace, source=source) is None:
            fqn = qualify(entry.namespace, suite.metadata.name)
            first = self.inventory.test_suite_sources.get(fqn)
            where = f" (first declared at {first})" if first is not None else ""
            self.inventory.record(
                LoadError(
                    message=f"duplicate test suite name {fqn!r}{where}; this document is ignored",
                    path=source.path,
                    relative=source.relative,
                    line=source.line,
                    index=source.index,
                    field_path=("metadata", "name"),
                    rule="NG-K001",
                )
            )

    def _add_annotation(self, document: RawDocument, entry: InventoryFile) -> None:
        """Register a ``note``, ``area`` or ``legend`` document (§21).

        Indexed as it is read, like a layout and for the same reason: an
        annotation names elements that may be declared in a file sorting later,
        and whether it names anything at all is the validator's question
        (``NG-G001``) rather than the loader's.

        Its provenance carries the field-level redirect table unconditionally,
        like a test suite's: the editor writes annotations back field by field —
        a dragged note is ``spec.geometry.x`` — and an operation that cannot find
        the line it is changing cannot preserve the comment above it.
        """
        self._annotations_seen += 1
        try:
            annotation = parse_annotation(document.data, source=document.source)
        except SchemaError as exc:
            for error in _schema_errors(exc, document):
                self.inventory.record(error)
            return
        source = SourceLocation(
            path=entry.path,
            relative=entry.relative.as_posix(),
            index=document.index,
            line=document.line,
            provenance=Provenance(base=document),
        )
        if self.inventory.add_annotation(annotation, namespace=entry.namespace, source=source):
            return
        fqn = qualify(entry.namespace, annotation.metadata.name)
        first = self.inventory.annotation_source(annotation.kind, fqn)
        where = f" (first declared at {first})" if first is not None else ""
        self.inventory.record(
            LoadError(
                message=(
                    f"duplicate {annotation.kind} name {fqn!r}{where}; this document is ignored"
                ),
                path=source.path,
                relative=source.relative,
                line=source.line,
                index=source.index,
                field_path=("metadata", "name"),
                rule="NG-G002",
            )
        )

    def mark(self) -> _Mark:
        """Where the builder stands before a file is fed to it."""
        return _Mark(
            slots=len(self._slots),
            errors=len(self.inventory.errors),
            templates=self._templates_seen,
            layouts=self._layouts_seen,
            suites=self._suites_seen,
            annotations=self._annotations_seen,
        )

    def harvest(self, mark: _Mark) -> CachedFile | None:
        """What the file fed since ``mark`` produced, or ``None`` if uncacheable.

        Two shapes are refused, both for the same reason: their meaning is not a
        function of this file's bytes.

        * A file declaring a ``kind: template``. The template is *used* by
          documents in other files, so replaying this one from a cache would
          leave the registry empty and every device that inherits from it
          dangling.
        * A device carrying ``spec.from``. Its element is the merge of this
          file's document with a template that may be declared anywhere, so a
          key over this file alone cannot notice the template changing.
        * A file declaring a ``kind: layout``, a ``kind: testsuite`` or one of
          the three annotation kinds of §21. All are indexed apart from the
          elements and a replayed slot list would not carry any of them, so a
          cached file would silently lose the arrangement, the assertions, or the
          notes that it declares.

        Both stay on the slow path forever, which is the honest cost of a
        per-file cache. They are counted, so ``netgraph cache info`` can say how
        much of the tree is not being cached and why.
        """
        if (
            self._templates_seen != mark.templates
            or self._layouts_seen != mark.layouts
            or self._suites_seen != mark.suites
            or self._annotations_seen != mark.annotations
        ):
            return None
        slots: list[CachedSlot] = []
        for slot in self._slots[mark.slots :]:
            if isinstance(slot, _Deferred):
                return None
            if isinstance(slot, _Rejected):
                slots.append(CachedSlot(errors=slot.errors))
            else:
                slots.append(
                    CachedSlot(
                        element=slot.element,
                        index=slot.source.index,
                        line=slot.source.line,
                    )
                )
        return CachedFile(
            slots=tuple(slots),
            # Whole-file problems -- a syntax error, an undecodable byte -- are
            # recorded as they happen rather than held in a slot, so they are
            # taken from the inventory and replayed the same way.
            errors=tuple(self.inventory.errors[mark.errors :]),
        )

    def replay(self, cached: CachedFile, *, entry: InventoryFile) -> None:
        """Feed a file that was parsed on an earlier run.

        The slots are appended in file order and the file-level diagnostics are
        recorded immediately, which is exactly what :meth:`feed` would have done
        — so the inventory, and the order of every diagnostic in it, comes out
        the same as a cold load's.
        """
        for error in cached.errors:
            self.inventory.record(error)
        relative = entry.relative.as_posix()
        for slot in cached.slots:
            if slot.element is None:
                self._slots.append(_Rejected(errors=slot.errors))
                continue
            self._slots.append(
                _Ready(
                    element=slot.element,
                    source=SourceLocation(
                        path=entry.path,
                        relative=relative,
                        index=slot.index,
                        line=slot.line,
                    ),
                    namespace=entry.namespace,
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
                # Carried, on request, so that a *semantic* finding -- which
                # happens long after loading -- can still be narrowed from the
                # document to the field, and to the template file that supplied
                # it. The table holds the document's YAML node tree alive, which
                # is why the default is to let it go.
                provenance=provenance if self.keep_provenance else None,
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
        column=site.column,
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
