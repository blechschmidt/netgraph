"""The set of files an edit session may touch, and the state it keeps about them.

Three jobs, all of them about the gap between "read" and "written":

**Laziness.** A tree may hold a thousand documents and an edit touches one. A
file is read, split and round-trip parsed only when an operation names something
in it, so the cost of an edit is proportional to the edit rather than to the
inventory.

**Journalling.** Before any operation changes a file, the text that file *would*
have been written as is recorded. That record is what every inverse is built
from, and it is why undo is byte-exact rather than merely equivalent: the
inverse of "rename a device and rewrite the eleven references to it" is "put
these four files back", and no reasoning about quoting styles or reference
spellings is involved.

**Hashing.** Every file read gets the SHA-256 of its bytes taken at the moment
it was read, and every file written has it checked again immediately before the
write. A file somebody else changed in between is a
:class:`~netgraph.edit.errors.ConflictError` and not a lost edit — which is the
difference between a tool that can back a web editor and one that cannot.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from netgraph.edit.errors import ConflictError, EditError
from netgraph.edit.placement import FileFacts
from netgraph.edit.roundtrip import YamlDocument, YamlFile
from netgraph.fsio import write_text_atomically
from netgraph.loader.inventory import Inventory, SourceLocation
from netgraph.loader.tree import Overlay, iter_inventory_files
from netgraph.models import LAYOUT_KIND

__all__ = ["EditableTree", "digest_of"]


def digest_of(payload: bytes) -> str:
    """The content hash a conflict check compares, as lower-case hex."""
    return hashlib.sha256(payload).hexdigest()


@dataclass(eq=False)
class _Tracked:
    """One file the session has looked at."""

    file: YamlFile
    #: SHA-256 of the bytes on disk when it was read; ``None`` for a file the
    #: session is creating, which is expected *not* to exist at write time.
    digest: str | None
    #: The text as read, so an unchanged file can be recognised and skipped.
    original: str | None
    #: Set when the whole file is to be removed.
    removed: bool = False


@dataclass(eq=False)
class EditableTree:
    """Every file an edit may read or write, and what has happened to each."""

    root: Path
    _files: dict[str, _Tracked] = field(default_factory=dict, repr=False)
    #: Pre-images recorded for the operation currently being applied.
    _journal: dict[str, str | None] | None = field(default=None, repr=False)

    # -- reading ---------------------------------------------------------

    def path_of(self, relative: str) -> Path:
        return self.root / PurePosixPath(relative)

    def exists(self, relative: str) -> bool:
        """Is there a file at ``relative`` as far as this session is concerned?"""
        tracked = self._files.get(relative)
        if tracked is not None:
            return not tracked.removed
        return self.path_of(relative).is_file()

    def open(self, relative: str) -> YamlFile:
        """The file at ``relative``, read and split on first use.

        Raises:
            EditError: The file does not exist, or cannot be read or parsed.
        """
        tracked = self._files.get(relative)
        if tracked is not None:
            if tracked.removed:
                raise EditError(f"{relative} has been deleted by an earlier operation")
            return tracked.file
        path = self.path_of(relative)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise EditError(f"cannot read {relative}: {exc.strerror or exc}") from exc
        # ``utf-8-sig`` matches the loader, which tolerates a byte-order mark;
        # ``YamlFile`` puts the mark back when it renders.
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise EditError(f"cannot read {relative}: {exc}") from exc
        parsed = YamlFile.parse(text, relative=relative)
        self._files[relative] = _Tracked(
            file=parsed, digest=digest_of(payload), original=parsed.render()
        )
        return parsed

    def create(self, relative: str) -> YamlFile:
        """Start a new file at ``relative``.

        Raises:
            EditError: Something is already there.
        """
        if self.exists(relative):
            raise EditError(f"{relative} already exists")
        self.note(relative)
        tracked = self._files.get(relative)
        parsed = YamlFile.empty(relative)
        if tracked is not None:  # a file this session deleted, being made again
            tracked.file, tracked.removed = parsed, False
        else:
            self._files[relative] = _Tracked(file=parsed, digest=None, original=None)
        return parsed

    def open_or_create(self, relative: str) -> YamlFile:
        """The file at ``relative``, made if it is not there."""
        return self.open(relative) if self.exists(relative) else self.create(relative)

    # -- journalling -----------------------------------------------------

    def begin(self) -> None:
        """Start recording pre-images for one operation."""
        self._journal = {}

    def note(self, relative: str) -> None:
        """Record what ``relative`` holds right now, if it is not recorded yet."""
        if self._journal is None or relative in self._journal:
            return
        self._journal[relative] = self.text_of(relative)

    def end(self) -> dict[str, str | None]:
        """Finish the operation and hand back what it found before it ran.

        Only files it actually changed: an applier that reads a document and
        then refuses has still marked it, and a file whose text came out
        identical is not a change anybody should be shown.
        """
        recorded = self._journal or {}
        self._journal = None
        return {
            relative: before
            for relative, before in recorded.items()
            if before != self.text_of(relative)
        }

    def abort(self) -> None:
        """Undo everything the current operation did, then stop recording.

        An applier that refuses part-way through must leave nothing behind: the
        next operation in the batch, or the caller retrying with ``--cascade``,
        has to see the tree it would have seen. Every mutating entry point notes
        its file first, so the journal is a complete record of what to put back.
        """
        recorded = self._journal or {}
        self._journal = None
        for relative, before in recorded.items():
            tracked = self._files.get(relative)
            if tracked is None:  # pragma: no cover - noting implies tracking
                continue
            if before is None:
                if tracked.original is None:
                    del self._files[relative]
                else:  # pragma: no cover - deleted by an earlier operation
                    tracked.removed = True
                continue
            tracked.file = YamlFile.parse(before, relative=relative)
            tracked.removed = False

    def text_of(self, relative: str) -> str | None:
        """What ``relative`` would be written as now, or ``None`` if it is gone.

        A file the session has not opened is answered from the disk, which is
        the same answer: nothing has changed it.
        """
        tracked = self._files.get(relative)
        if tracked is not None:
            return None if tracked.removed else tracked.file.render()
        path = self.path_of(relative)
        if not path.is_file():
            return None
        try:
            return path.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - raced or binary
            raise EditError(f"cannot read {relative}: {exc}") from exc

    # -- writing ---------------------------------------------------------

    def document(self, relative: str, index: int) -> YamlDocument:
        """The document at ``index``, marked as about to change.

        Raises:
            EditError: There is no such document.
        """
        self.note(relative)
        parsed = self.open(relative)
        if not 0 <= index < len(parsed.documents):
            raise EditError(f"{relative} has no document #{index}")
        return parsed.documents[index]

    def insert_document(self, relative: str, index: int, text: str) -> int:
        """Put a document holding ``text`` into ``relative`` at ``index``.

        A negative ``index`` appends.

        Returns:
            The position it ended up at, which is what a caller that has to keep
            editing the document needs — the inventory does not know about it
            until the tree is loaded again.
        """
        self.note(relative)
        parsed = self.open_or_create(relative)
        position = len(parsed.documents) if index < 0 else min(index, len(parsed.documents))
        parsed.insert(position, YamlDocument(text=text))
        return position

    def remove_document(self, relative: str, index: int) -> str:
        """Take the document at ``index`` out, deleting the file if it was the last.

        Returns:
            The text the document held.

        Raises:
            EditError: There is no such document.
        """
        self.note(relative)
        parsed = self.open(relative)
        if not 0 <= index < len(parsed.documents):
            raise EditError(f"{relative} has no document #{index}")
        removed = parsed.remove(index)
        if parsed.is_empty:
            self.remove_file(relative)
        return removed.render()

    def write_file(self, relative: str, text: str) -> None:
        """Replace ``relative`` wholesale, re-splitting it.

        The primitive every inverse is built from. It goes through the same
        parse as any other read, so a restored file is a file the session can
        keep editing.
        """
        self.note(relative)
        parsed = YamlFile.parse(text, relative=relative)
        tracked = self._files.get(relative)
        if tracked is None:
            path = self.path_of(relative)
            digest = digest_of(path.read_bytes()) if path.is_file() else None
            original = self.text_of(relative)
            self._files[relative] = _Tracked(file=parsed, digest=digest, original=original)
            return
        tracked.file, tracked.removed = parsed, False

    def remove_file(self, relative: str) -> None:
        """Delete ``relative`` when the session commits."""
        self.note(relative)
        tracked = self._files.get(relative)
        if tracked is None:
            path = self.path_of(relative)
            if not path.is_file():
                raise EditError(f"{relative} does not exist")
            payload = path.read_bytes()
            self._files[relative] = _Tracked(
                file=YamlFile.empty(relative),
                digest=digest_of(payload),
                original=payload.decode("utf-8-sig"),
                removed=True,
            )
            return
        tracked.removed = True

    # -- the result ------------------------------------------------------

    @property
    def changes(self) -> dict[str, str | None]:
        """Every file whose contents differ from the disk, ``None`` when deleted.

        Ordered by path, so a diff, a JSON report and the write order all agree.
        """
        changed: dict[str, str | None] = {}
        for relative in sorted(self._files):
            tracked = self._files[relative]
            after = None if tracked.removed else tracked.file.render()
            if after == tracked.original:
                # Including the file this session created and then deleted
                # again, which is not a change to anything.
                continue
            changed[relative] = after
        return changed

    @property
    def dirty(self) -> bool:
        return bool(self.changes)

    def original_of(self, relative: str) -> str | None:
        """The file as it was read, or ``None`` if it was not there."""
        tracked = self._files.get(relative)
        return None if tracked is None else tracked.original

    def overlay(self) -> Overlay:
        """The pending changes, in the form :func:`~netgraph.loader.load_tree` takes."""
        return Overlay(files=dict(self.changes))

    def check_conflicts(self) -> None:
        """Compare every file to be written against the bytes it was read as.

        Raises:
            ConflictError: One of them changed on disk since it was read.
        """
        for relative in self.changes:
            tracked = self._files[relative]
            path = self.path_of(relative)
            try:
                actual = digest_of(path.read_bytes()) if path.is_file() else None
            except OSError as exc:  # pragma: no cover - unreadable but existing
                raise EditError(f"cannot read {relative}: {exc.strerror or exc}") from exc
            if actual == tracked.digest:
                continue
            if tracked.digest is None:
                raise ConflictError(
                    f"{relative} was created by something else while this edit was being "
                    f"prepared; the edit has not been written",
                    path=relative,
                    expected=None,
                    actual=actual,
                )
            raise ConflictError(
                f"{relative} changed on disk since it was read "
                f"({'it has been deleted' if actual is None else 'its contents differ'}); "
                f"the edit has not been written",
                path=relative,
                expected=tracked.digest,
                actual=actual,
            )

    def commit(self) -> tuple[str, ...]:
        """Write every change to disk, after checking for conflicts.

        Returns:
            The paths written or removed, in path order.

        Raises:
            ConflictError: A file changed since it was read; nothing is written.
            EditError: A write failed. Files written before it stay written —
                there is no cross-file transaction on a plain filesystem — and
                the caller is told which ones those were.
        """
        changes = self.changes
        self.check_conflicts()
        written: list[str] = []
        for relative, text in changes.items():
            path = self.path_of(relative)
            try:
                if text is None:
                    path.unlink()
                    _prune(path.parent, self.root)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomically(path, text)
            except OSError as exc:
                raise EditError(
                    f"cannot write {relative}: {exc.strerror or exc}"
                    + (f" (already written: {', '.join(written)})" if written else "")
                ) from exc
            written.append(relative)
            tracked = self._files[relative]
            tracked.original = text
            tracked.digest = None if text is None else digest_of(text.encode("utf-8"))
        return tuple(written)

    # -- facts for placement ---------------------------------------------

    def facts(self, inventory: Inventory) -> dict[str, FileFacts]:
        """What each file holds, for :func:`~netgraph.edit.placement.choose_file`.

        Built from the loaded inventory rather than by re-reading the tree: the
        kinds and names it needs are exactly what loading already worked out.
        Files that declare no element — a file of templates — are listed with no
        documents, so placement never invents a path that is already taken.

        Layout documents (§18) are listed alongside the elements even though
        they declare none, because placing a *second* one has to find the first:
        a tree whose geometry lives in ``layouts.yaml`` should keep it there.
        """
        found: dict[str, tuple[list[str], list[str]]] = {}
        for relative in _discovered(self.root):
            found.setdefault(relative, ([], []))
        declared: list[tuple[SourceLocation | None, str, str]] = [
            (inventory.sources.get(fqn), element.kind, element.metadata.name)
            for fqn, element in inventory.elements.items()
        ]
        declared += [
            (inventory.layout_sources.get(fqn), LAYOUT_KIND, layout.metadata.name)
            for fqn, layout in inventory.layouts.items()
        ]
        for source, kind, name in declared:
            if source is None or source.relative is None:  # pragma: no cover - always set
                continue
            kinds, names = found.setdefault(source.relative, ([], []))
            kinds.append(kind)
            names.append(name)
        for relative, tracked in self._files.items():
            if tracked.removed:
                found.pop(relative, None)
        return {
            relative: FileFacts(relative=relative, kinds=tuple(kinds), names=tuple(names))
            for relative, (kinds, names) in found.items()
        }


def _discovered(root: Path) -> Iterator[str]:
    """Every file the loader would read below ``root``, as POSIX paths."""
    for entry in iter_inventory_files(root, errors=[]):
        yield entry.relative.as_posix()


def _prune(directory: Path, root: Path) -> None:
    """Remove ``directory`` and its now-empty parents, stopping at ``root``.

    A namespace is a folder, so the last document leaving a folder takes the
    namespace with it. Leaving an empty directory behind would leave an empty
    namespace behind, which nothing can put anything in and every listing has to
    skip.
    """
    current = directory.resolve()
    stop = root.resolve()
    while current != stop and current.is_relative_to(stop):
        try:
            next(current.iterdir())
        except StopIteration:
            pass
        except OSError:  # pragma: no cover - vanished under us
            return
        else:
            return
        try:
            current.rmdir()
        except OSError:  # pragma: no cover - permissions, races
            return
        current = current.parent
