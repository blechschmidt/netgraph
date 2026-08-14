"""One folder, open for editing, shared by a browser and whoever else edits it.

``netgraph web`` used to hold a string. That is enough for a scratchpad and it
is not enough for an editor: a stream has no files, so it has no namespaces, no
place to write a new device, and nowhere for an undo to put anything back. This
module is the other thing — a *session* over a real inventory tree, which is
what a visual editor has to be built on.

What a session owns
-------------------

**The tree.** :meth:`EditingSession.tree` answers with every file the loader
would read, the documents in it, which element each one declares, and the line
it starts on. That is the mapping between the picture and the text: a node in
the diagram is an address, an address has a source location, and a source
location is a file and a line the editor can reveal.

**The write path.** Every change — a typed operation from the canvas, or a
whole file the user retyped — becomes a list of :mod:`netgraph.edit`
operations, applied through an :class:`~netgraph.edit.EditSession`. This module
never builds YAML, never writes a file, and never decides whether a change is
safe: it hands operations to the layer that does. A batch is applied and
committed as one, so a refusal leaves the disk untouched.

**The history.** Applying an operation yields its exact inverse, so undo is a
list. The list lives here rather than in the page, which is what makes it
survive a reload — and what makes an undo issued from one tab correct in the
other.

Two directions of reconciliation
--------------------------------

An editor that owns the file is easy and wrong: people edit inventories in
``$EDITOR``, ``git checkout`` moves them under you, and a tree that a second
tool cannot touch is not a source of truth. So:

* **Disk to browser.** :class:`TreeWatcher` runs the same ``watchfiles`` watch
  ``netgraph watch`` runs, over the same :class:`~netgraph.watch.loop.InventoryFilter`,
  and bumps :attr:`EditingSession.revision`. The page polls that number and
  refetches when it moves, so an edit made anywhere reaches the open browser.
* **Browser to disk.** Every write carries a precondition — a content hash for
  a whole-file write, the tree revision for a batch of operations. A write
  whose precondition has moved is refused as a :class:`Conflict` and the page
  is told what is there now, rather than the other edit being lost.

Writing is off unless asked for
-------------------------------

:attr:`EditingSession.writable` gates every mutating entry point, and the
command line only turns it on for an explicit flag *and* a loopback bind. A
session that is not writable answers the same reads and refuses every write
with :class:`ReadOnly`, so the preview use of the command survives intact and
"I only wanted to look at it" cannot become a write.
"""

from __future__ import annotations

import difflib
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final

from netgraph.config import Config, ValidationConfig, load_config
from netgraph.edit import (
    ConflictError,
    CreateElement,
    EditError,
    EditSession,
    Operation,
    ValidationRefused,
    WriteFile,
    command_list,
    operation_from_dict,
)
from netgraph.edit.tree import digest_of
from netgraph.errors import NetgraphError
from netgraph.loader import (
    YAML_SUFFIXES,
    DocumentCache,
    Inventory,
    LoadError,
    load_tree,
)
from netgraph.loader.tree import iter_inventory_files
from netgraph.plan import PlanSourceError
from netgraph.plan import diff as diff_states
from netgraph.plan.sources import git_ref
from netgraph.render import IconTheme
from netgraph.validate import validate
from netgraph.watch.pipeline import Problem, flatten_problems
from netgraph.web.preview import Preview, ViewOptions, render_diff, render_inventory

__all__ = [
    "BASELINES",
    "GIT_BASELINE",
    "MAX_FILE_BYTES",
    "SESSION_BASELINE",
    "Conflict",
    "EditingSession",
    "Gesture",
    "ReadOnly",
    "SessionError",
    "TreeWatcher",
    "relative_path",
]

#: The state the changes drawer compares against: the tree as this session first
#: saw it. The default, because "what have I done this afternoon" is the question
#: an editor is asked, and it is the one question neither git nor the undo stack
#: answers — git may be behind or ahead of where the editing started, and a stack
#: of steps is not a state.
SESSION_BASELINE: Final = "session"

#: The other one: ``HEAD``, as the inventory root looks in it. Offered only when
#: the root is in a repository, which is what :meth:`EditingSession.baselines`
#: is for.
GIT_BASELINE: Final = "git"

#: Both, in menu order.
BASELINES: Final[tuple[str, ...]] = (SESSION_BASELINE, GIT_BASELINE)

#: Largest file the editor will read into the page or accept back from it. A
#: document bigger than this is one to open in a real editor; the browser is not
#: going to make it pleasant and re-rendering it on every keystroke is not going
#: to be interactive.
MAX_FILE_BYTES: Final = 1_000_000

#: How many entries of history a session keeps. Each one holds the *text* of
#: every file its operation touched, so the stack is bounded by memory rather
#: than by taste; a hundred edits is far more than a session reaches for and
#: still a small number of documents.
MAX_HISTORY: Final = 100


class SessionError(NetgraphError):
    """A request the session cannot honour. Answered as ``400``."""

    exit_code = 2


class ReadOnly(SessionError):
    """A write reached a session that was not opened for writing. ``403``."""


class Conflict(SessionError):
    """A write whose precondition no longer holds. ``409``.

    Carries what is there now, so the page can show the difference instead of
    telling the user their work is gone.
    """

    def __init__(self, message: str, *, path: str | None = None, hash: str | None = None) -> None:
        super().__init__(message)
        self.path = path
        #: Content hash of what is on disk now; ``None`` when it is not there.
        self.hash = hash


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def relative_path(given: str) -> str:
    """Check a path from a request and return it as a relative POSIX path.

    The one place a request becomes a file name, so it is the one place that
    can go wrong. What is allowed is exactly what the loader would read: a
    relative path below the root, no component that the loader skips (``..``
    among them), and a YAML suffix. Everything else is refused by name rather
    than normalised into something plausible.

    Raises:
        SessionError: The path is absolute, escapes the root, names a hidden
            component, or is not a YAML document.
    """
    text = given.strip()
    if not text:
        raise SessionError("a file path is required")
    if "\\" in text or "\x00" in text:
        raise SessionError(f"{given!r} is not a path inside the inventory")
    pure = PurePosixPath(text)
    if pure.is_absolute():
        raise SessionError(f"{given!r} is absolute; paths are relative to the inventory root")
    parts = pure.parts
    if not parts:
        raise SessionError("a file path is required")
    for component in parts:
        if component in ("..", "."):
            raise SessionError(f"{given!r} leaves the inventory root")
        if component.startswith((".", "_")):
            raise SessionError(
                f"{given!r} names {component!r}, which the loader never reads (NG-L002)"
            )
    if pure.suffix.lower() not in YAML_SUFFIXES:
        expected = " or ".join(sorted(YAML_SUFFIXES))
        raise SessionError(f"{given!r} is not a YAML document; expected {expected}")
    return pure.as_posix()


# --------------------------------------------------------------------------- #
# What the browser is told
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DocumentEntry:
    """One document in a file, and the element it declares."""

    index: int
    #: 1-based line the document starts on, when the parser reported one.
    line: int | None
    kind: str
    name: str
    #: Fully-qualified address, the name every operation and every graph node
    #: uses. ``None`` for a document that declared nothing loadable.
    address: str | None
    namespace: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "line": self.line,
            "kind": self.kind,
            "name": self.name,
            "address": self.address,
            "namespace": self.namespace,
        }


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One file of the tree, as the file list shows it."""

    path: str
    namespace: str
    #: SHA-256 of the bytes on disk, which is the precondition a write carries.
    hash: str
    size: int
    documents: tuple[DocumentEntry, ...] = ()
    #: Set when the file could not be read or parsed; the page still lists it,
    #: because a file with a syntax error is exactly the one to open.
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "namespace": self.namespace,
            "hash": self.hash,
            "size": self.size,
            "documents": [document.to_dict() for document in self.documents],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class Change:
    """What one applied batch did, in the form the page and a log both take."""

    revision: int
    applied: tuple[dict[str, Any], ...]
    inverse: tuple[dict[str, Any], ...]
    files: Mapping[str, str | None]
    diagnostics: tuple[Problem, ...]
    undo_depth: int
    redo_depth: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "applied": list(self.applied),
            "inverse": list(self.inverse),
            "files": {
                path: (
                    {"state": "deleted"} if value is None else {"state": "written", "hash": value}
                )
                for path, value in sorted(self.files.items())
            },
            "diagnostics": [_problem(problem) for problem in self.diagnostics],
            "undo": self.undo_depth,
            "redo": self.redo_depth,
        }


@dataclass(frozen=True, slots=True)
class _History:
    """One undoable step: what was applied, and what puts it back."""

    label: str
    forward: tuple[Operation, ...]
    backward: tuple[Operation, ...]


@dataclass(frozen=True, slots=True)
class Gesture:
    """One thing the user did, and everything needed to review or undo it.

    The unit is the *gesture*, not the operation: deleting a switch is one entry
    even though it becomes a delete and four disconnects, because one entry is
    what the user did and four is an implementation detail of doing it. A
    gesture that wrote nothing is never recorded — saving an unedited file is
    not a change to review.
    """

    #: Monotonic within the session. What a revert names.
    id: int
    #: What the gesture was, in the mutation layer's own words.
    label: str
    #: Tree revision the gesture produced.
    revision: int
    #: The operations it became, forward.
    operations: tuple[Operation, ...]
    #: What puts it back. A revert applies exactly this.
    inverse: tuple[Operation, ...]
    #: Unified diff of every file it touched, one hunk set per file.
    hunk: str
    #: Files it wrote or removed, in path order.
    files: tuple[str, ...]
    #: Addresses the gesture names, for click-to-reveal. Derived from the
    #: operations rather than from the diff: an operation says which *element*
    #: it is about, and a diff only says which lines moved.
    addresses: tuple[str, ...]
    #: The ``netgraph edit …`` lines that would replay it.
    commands: tuple[str, ...]
    #: Has it since been put back — by this entry's revert, or by an undo?
    reverted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "revision": self.revision,
            "operations": [operation.to_dict() for operation in self.operations],
            "hunk": self.hunk,
            "files": list(self.files),
            "addresses": list(self.addresses),
            "commands": list(self.commands),
            "reverted": self.reverted,
            "revertible": bool(self.inverse) and not self.reverted,
        }


def _problem(problem: Problem) -> dict[str, Any]:
    return {
        "severity": str(problem.severity),
        "location": problem.location,
        "rule": problem.rule,
        "message": problem.message,
    }


# --------------------------------------------------------------------------- #
# The session
# --------------------------------------------------------------------------- #


@dataclass(eq=False)
class EditingSession:
    """An inventory folder open in the browser."""

    root: Path
    #: Whether any write endpoint is enabled at all. Off unless the command line
    #: was given the opt-in flag on a loopback bind.
    writable: bool = False
    #: Icon theme for the diagram, chosen on the command line because it names a
    #: directory on this machine and a request must not be able to.
    icons: IconTheme | None = None
    #: ``netgraph.toml``; read from the root when not supplied, and re-read
    #: whenever the tree is reloaded so editing it takes effect like any edit.
    config: Config | None = None
    #: The parse cache, shared with the rest of the run, which is what makes a
    #: reload incremental: only the files whose bytes changed are parsed again.
    #: ``None`` parses the whole tree on every revision.
    cache: DocumentCache | None = None

    #: The server is threaded and a browser opens several connections, so every
    #: read of the tree and every write to it is serialised. Re-entrant because
    #: a write reloads the tree it just changed while still holding the lock.
    _lock: AbstractContextManager[bool] = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    _revision: int = field(default=1, init=False, repr=False)
    _inventory: Inventory | None = field(default=None, init=False, repr=False)
    _undo: list[_History] = field(default_factory=list, init=False, repr=False)
    _redo: list[_History] = field(default_factory=list, init=False, repr=False)
    #: Every gesture this session made, oldest first. Unlike the undo stack this
    #: is never popped: a change that was undone is still something the session
    #: did, and a log that quietly forgets it cannot be reviewed.
    _journal: list[Gesture] = field(default_factory=list, init=False, repr=False)
    #: The tree as it was when the session opened, kept for the diff overlay.
    #: Captured on the first load rather than in ``__post_init__`` so that
    #: opening a session costs nothing until somebody looks at it.
    _origin: Inventory | None = field(default=None, init=False, repr=False)
    #: The inverse the last committed batch produced, which is what undo and
    #: redo push onto the other stack. Held on the session rather than returned
    #: because :meth:`_commit` answers with the page's payload, not with mine.
    _last_inverse: tuple[Operation, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    # -- reading ---------------------------------------------------------

    @property
    def revision(self) -> int:
        """Bumped whenever the tree changes, by this session or by anything else.

        The page polls it. Everything else it holds — the file list, the
        diagram, the text of the file it has open — is valid only for the
        revision it was fetched at.
        """
        with self._lock:
            return self._revision

    def inventory(self) -> Inventory:
        """The tree as it is now, loaded once per revision.

        The parse cache is kept across reloads, so a change to one file reparses
        one file. Everything after the parse — reference resolution, validation,
        the graph — is redone, which is what makes a rename in one document show
        up in the diagram of another.
        """
        with self._lock:
            if self._inventory is None:
                self.config = load_config(self.root)
                self._inventory = load_tree(self.root, cache=self.cache)
            if self._origin is None:
                self._origin = self._inventory
            return self._inventory

    def origin(self) -> Inventory:
        """The tree as it was when this session first looked at it.

        What the changes drawer draws against by default, and the only honest
        answer to "what have I done this afternoon" — the undo stack is a stack
        of steps, not a state, and git HEAD may be behind or ahead of where the
        editing started.
        """
        with self._lock:
            self.inventory()
            assert self._origin is not None  # set by the load above
            return self._origin

    def settings(self) -> ValidationConfig:
        """How this tree grades findings: its own ``netgraph.toml``."""
        self.inventory()  # ensures ``config`` matches the loaded revision
        return self.config.validation if self.config is not None else ValidationConfig()

    def state(self) -> dict[str, Any]:
        """The small answer the page polls: has anything moved, and may I write?"""
        with self._lock:
            return {
                "mode": "session",
                "root": str(self.root),
                "revision": self._revision,
                "writable": self.writable,
                "undo": len(self._undo),
                "redo": len(self._redo),
                "undoLabel": self._undo[-1].label if self._undo else None,
                "redoLabel": self._redo[-1].label if self._redo else None,
                "maxFileBytes": MAX_FILE_BYTES,
            }

    def tree(self) -> dict[str, Any]:
        """Every file, its documents, and where each element was declared.

        This is the 1:1 mapping the project is built around, in one payload: a
        diagram node carries an address, an address appears here against a file
        and a line, and that is how "reveal the document that declares this" is
        answered without the page guessing at file names.
        """
        with self._lock:
            inventory = self.inventory()
            revision = self._revision
        documents = _documents_by_file(inventory)
        files: list[FileEntry] = []
        errors: list[LoadError] = []
        for entry in iter_inventory_files(self.root, errors=errors):
            relative = entry.relative.as_posix()
            try:
                payload = entry.path.read_bytes()
            except OSError as exc:  # pragma: no cover - vanished or unreadable
                files.append(
                    FileEntry(
                        path=relative,
                        namespace=entry.namespace,
                        hash="",
                        size=0,
                        error=exc.strerror or str(exc),
                    )
                )
                continue
            files.append(
                FileEntry(
                    path=relative,
                    namespace=entry.namespace,
                    hash=digest_of(payload),
                    size=len(payload),
                    documents=tuple(documents.get(relative, ())),
                )
            )
        return {
            "revision": revision,
            "root": str(self.root),
            "files": [entry.to_dict() for entry in files],
            "diagnostics": [_problem(problem) for problem in self.diagnostics(inventory)],
            "discovery": [
                {"location": error.location, "message": error.message} for error in errors
            ],
        }

    def diagnostics(self, inventory: Inventory | None = None) -> tuple[Problem, ...]:
        """Every load error and finding of the tree, most severe group first."""
        loaded = self.inventory() if inventory is None else inventory
        return flatten_problems(loaded.errors, validate(loaded, self.settings()))

    def graph(self, view: ViewOptions | None = None) -> tuple[Preview, int]:
        """Draw the tree, and say which revision was drawn.

        Returns:
            The rendering and the revision it was made from, so a page that
            receives a stale answer — one made before a change it has already
            heard about — can ask again rather than show it.
        """
        with self._lock:
            inventory = self.inventory()
            revision = self._revision
        options = view or ViewOptions()
        if options.icons is None and self.icons is not None:
            options = replace(options, icons=self.icons)
        settings = self.settings().with_overrides(strict=True if options.strict else None)
        return render_inventory(inventory, options, settings=settings), revision

    def read_file(self, path: str) -> dict[str, Any]:
        """One file's text, with the hash a write of it has to quote back.

        Raises:
            SessionError: The path is not one the loader would read, the file is
                not there, or it is too big to edit in a browser.
        """
        relative = relative_path(path)
        target = self.root / PurePosixPath(relative)
        try:
            payload = target.read_bytes()
        except FileNotFoundError:
            raise SessionError(f"{relative} does not exist in {self.root}") from None
        except OSError as exc:
            raise SessionError(f"cannot read {relative}: {exc.strerror or exc}") from exc
        if len(payload) > MAX_FILE_BYTES:
            raise SessionError(
                f"{relative} is {len(payload)} bytes; this editor opens up to {MAX_FILE_BYTES}"
            )
        try:
            # ``utf-8-sig`` matches the loader and the mutation layer, which put
            # a byte-order mark back when they render.
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SessionError(f"{relative} is not UTF-8 text: {exc}") from exc
        return {
            "path": relative,
            "text": text,
            "hash": digest_of(payload),
            "revision": self.revision,
        }

    # -- writing ---------------------------------------------------------

    def write_file(
        self, path: str, text: str, *, base_hash: str | None = None, force: bool = False
    ) -> Change:
        """Replace one file wholesale, if it is still what the caller last read.

        The precondition is the point. Without it a browser tab left open over
        lunch would silently undo whatever was done in ``$EDITOR`` meanwhile;
        with it that write is refused and the page can show what is there now.
        ``base_hash`` of ``None`` means "I am creating this", and is refused if
        something is already there.

        Raises:
            ReadOnly: This session does not write.
            SessionError: The path or the text is unusable.
            Conflict: The file is not what ``base_hash`` says it was.
            ValidationRefused: The result would introduce a new error and
                ``force`` was not set.
        """
        self._require_writable()
        relative = relative_path(path)
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise SessionError(
                f"the document is larger than {MAX_FILE_BYTES} bytes; keep a file this size "
                f"out of the browser"
            )
        with self._lock:
            actual = self._digest_on_disk(relative)
            if base_hash is not None and actual != base_hash:
                raise Conflict(
                    f"{relative} changed on disk since it was opened"
                    + ("; it has been deleted" if actual is None else ""),
                    path=relative,
                    hash=actual,
                )
            if base_hash is None and actual is not None:
                raise Conflict(
                    f"{relative} already exists; open it before writing to it",
                    path=relative,
                    hash=actual,
                )
            return self._commit(
                [WriteFile(path=relative, text=text)],
                label=f"edit {relative}",
                force=force,
            )

    def apply(
        self,
        payload: Sequence[Mapping[str, Any]],
        *,
        revision: int | None = None,
        force: bool = False,
    ) -> Change:
        """Apply a batch of :mod:`netgraph.edit` operations, atomically.

        ``revision`` is the tree-level precondition: a page that decided to
        rename ``sw1`` was looking at a tree, and if that tree has moved the
        decision may no longer mean what it meant. Omitting it says the batch is
        safe against any tree, which is true of an undo and not much else.

        Raises:
            ReadOnly: This session does not write.
            SessionError: An operation is not one netgraph has.
            Conflict: ``revision`` is not the current one.
            EditError: An operation cannot be applied; nothing is written.
            ValidationRefused: The batch would introduce a new error.
        """
        self._require_writable()
        operations = _decode(payload)
        if not operations:
            raise SessionError("no operations were given")
        with self._lock:
            if revision is not None and revision != self._revision:
                raise Conflict(
                    f"the inventory has changed since revision {revision} "
                    f"(it is now at {self._revision}); reload before editing"
                )
            label = operations[0].describe()
            if len(operations) > 1:
                label += f" (+{len(operations) - 1} more)"
            return self._commit(operations, label=label, force=force)

    def undo(self) -> Change:
        """Put the last change back.

        Raises:
            ReadOnly: This session does not write.
            SessionError: There is nothing to undo.
        """
        return self._step(self._undo, self._redo, "undo")

    def redo(self) -> Change:
        """Apply the last undone change again."""
        return self._step(self._redo, self._undo, "redo")

    def _step(self, source: list[_History], target: list[_History], verb: str) -> Change:
        self._require_writable()
        with self._lock:
            if not source:
                raise SessionError(f"there is nothing to {verb}")
            entry = source[-1]
            # ``force``: this is a state the tree was already in, so refusing it
            # for the problems it has would leave no way back to it.
            change = self._commit(entry.backward, label=entry.label, force=True, history=False)
            source.pop()
            target.append(
                _History(label=entry.label, forward=entry.backward, backward=self._last_inverse)
            )
            return self._restated(change)

    # -- the machinery ---------------------------------------------------

    def _commit(
        self,
        operations: Sequence[Operation],
        *,
        label: str,
        force: bool,
        history: bool = True,
        reverts: int | None = None,
    ) -> Change:
        """Apply and write one batch through the mutation layer.

        Nothing here constructs YAML, decides placement or touches a file:
        :class:`~netgraph.edit.EditSession` does all of it, under the same
        validation gate and the same per-file conflict check the command line
        uses. What is added is the session's own bookkeeping — the history entry
        and the revision bump.

        A batch that changes nothing — saving a file that was not edited, or
        setting a field to what it already holds — writes nothing, records no
        history and does not move the revision. Anything else would make the
        page refetch the tree, and every other tab's undo stack grow, for an
        edit that did not happen.
        """
        # Pin the session's starting state before the first write rather than
        # after it. Everything below eventually loads the tree, and a baseline
        # captured on the way *out* of the first commit would be the state after
        # that commit — so the drawer would never show the first thing you did.
        self.origin()
        session = EditSession(root=self.root, config=self.config, cache=self.cache)
        applied = session.apply_all(operations)
        changes = dict(session.changes)
        # Read before the write, which is the only moment the old text is still
        # on disk. The journal's hunk is the one thing here that cannot be
        # recomputed afterwards.
        previous = {path: self._text_on_disk(path) for path in changes}
        session.commit(force=force)
        summary = session.summary(written=tuple(changes), changes=changes)
        # An empty inverse is the honest record of a batch that changed nothing:
        # it keeps a later redo from replaying whatever the *previous* batch
        # left behind, which is how an undo stack starts writing files nobody
        # asked it to.
        self._last_inverse = summary.inverse if changes else ()
        if changes:
            if history:
                self._undo.append(
                    _History(label=label, forward=tuple(operations), backward=summary.inverse)
                )
                del self._undo[:-MAX_HISTORY]
                self._redo.clear()
            self.invalidate()
            self._record(
                label=label,
                operations=tuple(operations),
                inverse=summary.inverse,
                changes=changes,
                previous=previous,
                reverts=reverts,
            )
        return Change(
            revision=self._revision,
            applied=tuple(
                {
                    "operation": item.operation.to_dict(),
                    "inverse": [operation.to_dict() for operation in item.inverse],
                    "summary": item.summary,
                    "files": list(item.files),
                }
                for item in applied
            ),
            inverse=tuple(operation.to_dict() for operation in summary.inverse),
            files={
                path: (None if text is None else digest_of(text.encode("utf-8")))
                for path, text in changes.items()
            },
            diagnostics=self.diagnostics(),
            undo_depth=len(self._undo),
            redo_depth=len(self._redo),
        )

    def _record(
        self,
        *,
        label: str,
        operations: tuple[Operation, ...],
        inverse: tuple[Operation, ...],
        changes: Mapping[str, str | None],
        previous: Mapping[str, str | None],
        reverts: int | None,
    ) -> None:
        """Add one gesture to the journal, and mark what it put back.

        Called only for a batch that actually wrote something, so the log holds
        gestures rather than attempts.
        """
        entry = Gesture(
            id=len(self._journal) + 1,
            label=label,
            revision=self._revision,
            operations=operations,
            inverse=inverse,
            hunk=_unified_diff(previous, changes),
            files=tuple(sorted(changes)),
            addresses=_addresses_of(operations),
            commands=command_list(operations, inventory=str(self.root)),
        )
        self._journal.append(entry)
        if reverts is not None:
            self._mark_reverted(reverts)

    def _mark_reverted(self, entry_id: int) -> None:
        for index, entry in enumerate(self._journal):
            if entry.id == entry_id:
                self._journal[index] = replace(entry, reverted=True)
                return

    def _text_on_disk(self, relative: str) -> str | None:
        """One file's text as it is now, or ``None`` when it is not there."""
        try:
            return (self.root / PurePosixPath(relative)).read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return None

    # -- the journal -----------------------------------------------------

    def journal(self) -> tuple[Gesture, ...]:
        """Every gesture this session made, oldest first."""
        with self._lock:
            return tuple(self._journal)

    def changes(self) -> dict[str, Any]:
        """The changes drawer's payload: the log, and the handover.

        ``commands`` is the whole session as a ``netgraph edit`` script, in the
        order it happened and with reverted gestures left in — a script that
        skipped them would not reproduce the tree, which is the one thing the
        handover has to do.
        """
        with self._lock:
            entries = tuple(self._journal)
            revision = self._revision
        return {
            "revision": revision,
            "root": str(self.root),
            "entries": [entry.to_dict() for entry in entries],
            "commands": [command for entry in entries for command in entry.commands],
        }

    def revert(self, entry_id: int, *, revision: int | None = None) -> Change:
        """Put one gesture back, whatever has happened since.

        A revert is an ordinary edit, not a rewind: it applies the gesture's own
        inverse as a new change, which is itself journalled and itself
        undoable. Reverting the third of ten gestures therefore leaves the other
        nine in place — and fails, loudly and without writing, when one of them
        depended on what the third one did.

        Raises:
            ReadOnly: This session does not write.
            SessionError: There is no such gesture, or it has been reverted.
            Conflict: ``revision`` is not the current one.
            EditError: The inverse cannot be applied; nothing is written.
        """
        self._require_writable()
        with self._lock:
            if revision is not None and revision != self._revision:
                raise Conflict(
                    f"the inventory has changed since revision {revision} "
                    f"(it is now at {self._revision}); reload before reverting"
                )
            entry = next((item for item in self._journal if item.id == entry_id), None)
            if entry is None:
                raise SessionError(f"this session made no change numbered {entry_id}")
            if entry.reverted:
                raise SessionError(f"change {entry_id} ({entry.label}) has already been put back")
            if not entry.inverse:
                raise SessionError(f"change {entry_id} ({entry.label}) records nothing to put back")
            # ``force``: this restores a state the tree was already in, so
            # refusing it for problems it already had would leave no way back.
            return self._commit(
                entry.inverse, label=f"revert {entry.label}", force=True, reverts=entry.id
            )

    # -- the diff overlay ------------------------------------------------

    def diff(
        self, view: ViewOptions | None = None, *, against: str = SESSION_BASELINE
    ) -> tuple[Preview, int]:
        """Draw the tree with what has changed since ``against`` painted on.

        Args:
            view: Which graph to build and how to draw it, as :meth:`graph`
                takes it.
            against: :data:`SESSION_BASELINE` — the tree as this session first
                saw it — or :data:`GIT_BASELINE`, which is ``HEAD`` as the
                inventory root looks in it.

        Returns:
            The rendering and the revision it was made from, like :meth:`graph`.

        Raises:
            SessionError: ``against`` is not one of the two, or a git baseline
                was asked for and cannot be read.
        """
        if against not in BASELINES:
            raise SessionError(f"unknown baseline {against!r}; expected {' or '.join(BASELINES)}")
        with self._lock:
            inventory = self.inventory()
            revision = self._revision
            origin = self._origin
        assert origin is not None  # ``inventory()`` above sets ``_origin``
        before = origin if against == SESSION_BASELINE else self._head()
        options = view or ViewOptions()
        if options.icons is None and self.icons is not None:
            options = replace(options, icons=self.icons)
        settings = self.settings().with_overrides(strict=True if options.strict else None)
        plan = diff_states(before, inventory)
        return render_diff(before, inventory, plan, options, settings=settings), revision

    def baselines(self) -> tuple[str, ...]:
        """Which baselines this session can draw against, in menu order.

        ``git`` is offered only when the root is in a repository with a
        readable ``HEAD``, because an option that always fails is not an option.
        """
        return BASELINES if self.in_repository() else (SESSION_BASELINE,)

    def in_repository(self) -> bool:
        """Is the inventory root inside a git repository with a commit in it?"""
        try:
            with git_ref(self.root, "HEAD"):
                return True
        except (PlanSourceError, OSError):
            return False

    def _head(self) -> Inventory:
        """The inventory as ``HEAD`` has it.

        Read afresh each time rather than cached: a session outlives commits,
        and a drawer that says "since HEAD" while comparing against a HEAD from
        two commits ago is worse than one that takes a moment.

        Raises:
            SessionError: There is no repository, no ``git``, or no ``HEAD``.
        """
        try:
            with git_ref(self.root, "HEAD") as exported:
                return load_tree(exported)
        except PlanSourceError as exc:
            raise SessionError(str(exc)) from exc

    def _restated(self, change: Change) -> Change:
        """``change`` with the stack depths as they are after the history moved."""
        return Change(
            revision=change.revision,
            applied=change.applied,
            inverse=change.inverse,
            files=change.files,
            diagnostics=change.diagnostics,
            undo_depth=len(self._undo),
            redo_depth=len(self._redo),
        )

    def _require_writable(self) -> None:
        if not self.writable:
            raise ReadOnly(
                "this session was opened read-only; restart with --write to edit the inventory"
            )

    def _digest_on_disk(self, relative: str) -> str | None:
        target = self.root / PurePosixPath(relative)
        try:
            return digest_of(target.read_bytes())
        except FileNotFoundError:
            return None
        except OSError as exc:  # pragma: no cover - unreadable but existing
            raise SessionError(f"cannot read {relative}: {exc.strerror or exc}") from exc

    def invalidate(self, paths: Iterable[str] = ()) -> int:
        """Note that the tree has changed and drop what was derived from it.

        Called by :class:`TreeWatcher` for a change made outside this session,
        and by the session itself after a write. The two are deliberately not
        distinguished: the page's answer to either is to refetch, and pretending
        to know which of several writers made a change is how an editor comes to
        show a tree that is not there.
        """
        del paths  # accepted so a watcher can pass its batch; only the fact counts
        with self._lock:
            self._revision += 1
            self._inventory = None
            return self._revision


def _unified_diff(before: Mapping[str, str | None], after: Mapping[str, str | None]) -> str:
    """The YAML hunk one gesture produced, over every file it touched.

    Unified diff because that is the form everyone already reads, and with
    ``a/`` and ``b/`` prefixes because that is the form ``git apply`` takes: a hunk
    copied out of the drawer should paste into a patch file without editing.
    A created file diffs against nothing and a deleted one against nothing, both
    of which unified diff already spells.
    """
    pieces: list[str] = []
    for path in sorted(after):
        old = (before.get(path) or "").splitlines(keepends=True)
        new = (after.get(path) or "").splitlines(keepends=True)
        pieces.extend(
            difflib.unified_diff(
                old, new, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="\n", n=3
            )
        )
        if pieces and not pieces[-1].endswith("\n"):
            pieces.append("\n")
    return "".join(pieces)


def _addresses_of(operations: Sequence[Operation]) -> tuple[str, ...]:
    """Every element address the operations name, in first-seen order.

    Read off the operations rather than out of the diff: an operation says which
    *element* it is about, which is what "reveal this in the file" needs, while
    a diff only says which lines moved.
    """
    found: dict[str, None] = {}
    for operation in operations:
        address = getattr(operation, "address", None)
        if isinstance(address, str) and address:
            found.setdefault(address, None)
        name = getattr(operation, "name", None)
        if isinstance(operation, CreateElement) and isinstance(name, str):
            namespace = operation.namespace
            found.setdefault(f"{namespace}/{name}" if namespace else name, None)
    return tuple(found)


def _decode(payload: Sequence[Mapping[str, Any]]) -> tuple[Operation, ...]:
    """Turn the JSON a page posts into operations, refusing anything else.

    Raises:
        SessionError: The body is not a list of operation objects, or one of
            them is not an operation netgraph has.
    """
    if not isinstance(payload, list):
        raise SessionError("'ops' must be a list of operations")
    operations: list[Operation] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise SessionError(f"operation #{index} is not an object")
        try:
            operations.append(operation_from_dict(item))
        except EditError as exc:
            raise SessionError(f"operation #{index}: {exc}") from exc
    return tuple(operations)


def _documents_by_file(inventory: Inventory) -> dict[str, list[DocumentEntry]]:
    """Every declared document, grouped by the file it is in and in file order.

    Built from the loaded inventory rather than by re-reading: the addresses and
    the lines are exactly what loading already worked out, and a file list built
    from a second parse could disagree with the diagram.
    """
    found: dict[str, list[DocumentEntry]] = {}
    declared: list[tuple[Any, str, str, str]] = [
        (inventory.sources.get(fqn), element.kind, element.metadata.name, fqn)
        for fqn, element in inventory.elements.items()
    ]
    declared += [
        (inventory.layout_sources.get(fqn), layout.kind, layout.metadata.name, fqn)
        for fqn, layout in inventory.layouts.items()
    ]
    for source, kind, name, fqn in declared:
        if source is None or source.relative is None:  # pragma: no cover - always set
            continue
        namespace = fqn.rsplit("/", 1)[0] if "/" in fqn else ""
        found.setdefault(source.relative, []).append(
            DocumentEntry(
                index=source.index,
                line=source.line,
                kind=kind,
                name=name,
                address=fqn,
                namespace=namespace,
            )
        )
    for entries in found.values():
        entries.sort(key=lambda entry: entry.index)
    return found


# --------------------------------------------------------------------------- #
# The other direction: the disk
# --------------------------------------------------------------------------- #


class TreeWatcher:
    """A ``watchfiles`` watch over a session's root, on a background thread.

    The same watch ``netgraph watch`` runs, over the same filter, for the same
    reason: an edit made in ``$EDITOR``, by ``git checkout`` or by a second
    netgraph process is a change to the source of truth and the open browser has
    to hear about it. All it does is call
    :meth:`EditingSession.invalidate`; deciding what to refetch is the page's.

    Use it as a context manager::

        with TreeWatcher(session):
            ...
    """

    def __init__(
        self,
        session: EditingSession,
        *,
        debounce_ms: int | None = None,
        on_change: Callable[[Sequence[str]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.debounce_ms = debounce_ms
        self.on_change = on_change
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set when the watch could not be started at all, so a caller can say
        #: so once rather than leaving the user wondering why nothing updates.
        self.error: str | None = None

    def start(self) -> TreeWatcher:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="netgraph-web-watch", daemon=True
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> TreeWatcher:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _run(self) -> None:
        try:
            for batch in self._changes():
                self.session.invalidate(batch)
                if self.on_change is not None:
                    self.on_change(batch)
        except Exception as exc:  # pragma: no cover - a watcher must not kill the server
            # A watch that cannot start is a real loss — the browser stops
            # hearing about edits made elsewhere — but not a reason to take the
            # editor down with it, so it is reported and the server carries on.
            self.error = str(exc)
            if self.on_error is not None:
                self.on_error(self.error)

    def _changes(self) -> Iterator[tuple[str, ...]]:
        from netgraph.watch.loop import DEFAULT_DEBOUNCE_MS, InventoryFilter, file_changes

        yield from file_changes(
            [self.session.root],
            watch_filter=InventoryFilter(root=self.session.root),
            debounce_ms=self.debounce_ms or DEFAULT_DEBOUNCE_MS,
            stop=self._stop,
        )


#: Re-exported so a caller catching "the session refused" catches one name.
EDIT_ERRORS: Final = (SessionError, EditError, ConflictError, ValidationRefused)
