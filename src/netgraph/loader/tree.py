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
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from netgraph.errors import LoaderError, SchemaError
from netgraph.loader.documents import RawDocument, YamlSyntaxError, read_documents
from netgraph.loader.ignore import IGNORE_FILE_NAME, IgnoreStack, parse_ignore_file
from netgraph.loader.inventory import Inventory, LoadError, SourceLocation, qualify
from netgraph.models import parse_document

__all__ = ["YAML_SUFFIXES", "InventoryFile", "iter_inventory_files", "load_tree"]

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
    for entry in iter_inventory_files(root, errors=inventory.errors):
        _load_file(entry, inventory)
    return inventory


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


def _load_file(entry: InventoryFile, inventory: Inventory) -> None:
    """Parse one file and add every document it holds to ``inventory``."""
    relative = entry.relative.as_posix()
    try:
        for document in read_documents(entry.path, relative=entry.relative):
            _add_document(document, entry=entry, inventory=inventory)
    except YamlSyntaxError as exc:
        inventory.record(
            LoadError(
                message=str(exc),
                path=entry.path,
                relative=relative,
                line=exc.line,
                column=exc.column,
            )
        )
    except OSError as exc:
        inventory.record(
            LoadError(
                message=f"cannot read file: {exc.strerror or exc}",
                path=entry.path,
                relative=relative,
            )
        )


def _add_document(document: RawDocument, *, entry: InventoryFile, inventory: Inventory) -> None:
    """Validate one document and index it, or record why that failed."""
    if document.data is None:  # NG-L004: an empty document is not an error.
        return

    relative = entry.relative.as_posix()
    try:
        element = parse_document(document.data, source=document.source)
    except SchemaError as exc:
        for error in _errors_from(exc, document=document, relative=relative):
            inventory.record(error)
        return

    source = SourceLocation(
        path=entry.path,
        relative=relative,
        index=document.index,
        line=document.line,
    )
    fqn = inventory.add(element, namespace=entry.namespace, source=source)
    if fqn is None:
        _record_duplicate(element.metadata.name, entry=entry, source=source, inventory=inventory)


def _errors_from(
    error: SchemaError, *, document: RawDocument, relative: str
) -> Iterator[LoadError]:
    """One :class:`LoadError` per schema issue, each located as precisely as possible."""
    if not error.issues:
        yield LoadError(
            message=str(error),
            path=document.path,
            relative=relative,
            line=document.line,
            index=document.index,
        )
        return
    for issue in error.issues:
        yield LoadError.from_issue(
            issue,
            path=document.path,
            relative=relative,
            index=document.index,
            line=document.line_for(issue.path),
        )


def _record_duplicate(
    name: str, *, entry: InventoryFile, source: SourceLocation, inventory: Inventory
) -> None:
    """``NG-N002`` — the name is taken; keep the first declaration and say where."""
    fqn = qualify(entry.namespace, name)
    first = inventory.source_of(fqn)
    where = f" (first declared at {first})" if first is not None else ""
    inventory.record(
        LoadError(
            message=f"duplicate element name {fqn!r}{where}; this document is ignored",
            path=source.path,
            relative=source.relative,
            line=source.line,
            index=source.index,
            field_path=("metadata", "name"),
            rule="NG-N002",
        )
    )


def _relative_text(relative: PurePosixPath) -> str:
    text = relative.as_posix()
    return "" if text == "." else text
