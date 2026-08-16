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
  and bumps :attr:`EditingSession.revision`. Every bump — from the watcher or
  from a write of this session's own — is announced on
  :attr:`EditingSession.events` (:mod:`netgraph.web.events`), naming the files
  that moved, so an open page refetches those and nothing else. The revision is
  still there to be polled, because a client that cannot hold a stream open must
  still be correct, only slower.
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

More than one client
--------------------

A session is shared: two tabs, or two people on the same machine. Each connected
client is issued an id (:mod:`netgraph.web.presence`), and the state payload
lists them with what each has selected and which files it has unsaved edits in.
The page draws remote selections faintly and badges a file somebody else is in.

All of that is *advisory*. Presence expires on a timer and blocks nothing; the
revision precondition and the content hash above remain the only gates on a
write, because they are the only two facts about the tree the server can check.
"""

from __future__ import annotations

import difflib
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final

from netgraph.config import DEFAULT_MAX_REVISIONS, Config, ValidationConfig, load_config
from netgraph.edit import (
    ARRANGEMENTS,
    DEFAULT_SUFFIX,
    Batch,
    ConflictError,
    CopyElement,
    CopyPlan,
    CreateElement,
    DeleteElement,
    EditableTree,
    EditError,
    EditSession,
    Operation,
    ValidationRefused,
    WriteFile,
    arrange_operations,
    clipboard_payload,
    command_list,
    copy_plan,
    describe_arrangement,
    move_plan,
    operation_from_dict,
    paste_plan,
)
from netgraph.edit.batch import describe as describe_batch
from netgraph.edit.cascade import describe as describe_cascade
from netgraph.edit.cascade import plan_cascade
from netgraph.edit.tree import digest_of
from netgraph.errors import NetgraphError
from netgraph.fixes import Fix, apply_fix, fixes_for, offers_for
from netgraph.history import FrameCache, HistoryError, Timeline
from netgraph.impact import LAYERS as IMPACT_LAYERS
from netgraph.impact import ImpactError, ImpactReport, simulate
from netgraph.layout.geometry import DEFAULT_GRID
from netgraph.loader import (
    YAML_SUFFIXES,
    DocumentCache,
    Inventory,
    LoadError,
    load_tree,
)
from netgraph.loader.inventory import short_name
from netgraph.loader.tree import InventoryFile, iter_inventory_files
from netgraph.plan import PlanSourceError
from netgraph.plan import diff as diff_states
from netgraph.plan.sources import git_ref
from netgraph.render import IconTheme
from netgraph.render.routes import RouteCache
from netgraph.validate import Finding, validate
from netgraph.watch.pipeline import Problem, Status, flatten_problems
from netgraph.web.events import EVENT_NAMES, EVENTS_PATH, HEARTBEAT_SECONDS, EventBus
from netgraph.web.presence import Client, Presence
from netgraph.web.preview import (
    Preview,
    ViewOptions,
    clip_problems,
    render_diff,
    render_inventory,
)

__all__ = [
    "BASELINES",
    "FRAME_CACHE_SIZE",
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

#: How many rendered timeline frames one session keeps. Each is an SVG of a
#: whole network, so the cap is on count rather than on bytes: a dozen is a
#: comfortable scrub back and forth over recent history, and a hundred would be
#: a session holding a repository's worth of pictures nobody is looking at.
FRAME_CACHE_SIZE: Final = 12

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
            **_diagnostics_payload(self.diagnostics),
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


def _file_entry(file: InventoryFile, documents: Mapping[str, Sequence[DocumentEntry]]) -> FileEntry:
    """One row of the file list: its bytes' hash, its size and its documents.

    A file that cannot be read is still a row. It is the one the user most needs
    to see, and leaving it out would make the list disagree with the folder.
    """
    relative = file.relative.as_posix()
    try:
        payload = file.path.read_bytes()
    except OSError as exc:  # pragma: no cover - vanished or unreadable
        return FileEntry(
            path=relative,
            namespace=file.namespace,
            hash="",
            size=0,
            error=exc.strerror or str(exc),
        )
    return FileEntry(
        path=relative,
        namespace=file.namespace,
        hash=digest_of(payload),
        size=len(payload),
        documents=tuple(documents.get(relative, ())),
    )


def _choose_fix(fixes: Sequence[Fix], *, rule: str, key: str | None) -> Fix:
    """The repair ``key`` names, or the only one there is.

    Raises:
        SessionError: There is no repair, or several and none was named, or the
            name is not one of them.
    """
    if not fixes:
        raise SessionError(f"{rule} has no mechanical fix here")
    if key is None:
        if len(fixes) > 1:
            offered = ", ".join(fix.key for fix in fixes)
            raise SessionError(f"{rule} offers more than one repair; pick one of {offered}")
        return fixes[0]
    chosen = next((fix for fix in fixes if fix.key == key), None)
    if chosen is None:
        offered = ", ".join(fix.key for fix in fixes)
        raise SessionError(f"{rule} offers {offered}, not {key!r}")
    return chosen


def _diagnostics_payload(problems: Sequence[Problem]) -> dict[str, Any]:
    """The ``diagnostics`` half of an answer: the rows to draw and what was left out.

    Every answer that carries diagnostics carries them the same way, capped the
    same way and counted the same way; see :data:`~netgraph.web.preview.MAX_PROBLEMS`.
    """
    shown, omitted = clip_problems(problems)
    return {
        "diagnostics": [_problem(problem) for problem in shown],
        "diagnosticsOmitted": omitted,
    }


def _problem(problem: Problem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "severity": str(problem.severity),
        "location": problem.location,
        "rule": problem.rule,
        "message": problem.message,
    }
    if problem.fixes:
        payload["fixes"] = [{"key": key, "title": title} for key, title in problem.fixes]
    return payload


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
    #: The push channel. Every revision bump, every committed gesture and every
    #: presence change is announced here; ``GET /api/events`` is a reader of it.
    #: A session with no reader publishes into a ring buffer and costs nothing.
    events: EventBus = field(default_factory=EventBus)
    #: Who else has this session open. Advisory throughout; see
    #: :mod:`netgraph.web.presence`.
    presence: Presence = field(default_factory=Presence)

    #: The server is threaded and a browser opens several connections, so every
    #: read of the tree and every write to it is serialised. Re-entrant because
    #: a write reloads the tree it just changed while still holding the lock.
    _lock: AbstractContextManager[bool] = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    _revision: int = field(default=1, init=False, repr=False)
    _inventory: Inventory | None = field(default=None, init=False, repr=False)
    #: The last answer :meth:`findings` gave, with the two objects it was an
    #: answer about. See that method for why identity is the whole key.
    _findings: tuple[Inventory, ValidationConfig, tuple[Finding, ...]] | None = field(
        default=None, init=False, repr=False
    )
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
    #: The history of the tree, opened on first use. ``None`` until somebody
    #: asks for it, because most sessions never open the timeline and opening it
    #: costs a git process.
    _timeline: Timeline | None = field(default=None, init=False, repr=False)
    #: Rendered frames, keyed by the pair of tree hashes they are between and
    #: the view they were drawn in. Bounded, because an SVG of a large network
    #: is megabytes and a scrubbed history is a lot of them. Each entry holds
    #: the changeset beside the picture, so a revisit recomputes neither.
    _frames: FrameCache[tuple[dict[str, Any], Preview]] = field(
        default_factory=lambda: FrameCache(FRAME_CACHE_SIZE), init=False, repr=False
    )
    #: Orthogonal routes kept between renders. Moving one device changes the
    #: obstacle set for the whole drawing, and re-searching every link because
    #: of it is what would put a full render inside a drag; the cache re-searches
    #: only the links whose line the move actually broke. Kept for the life of
    #: the session and never invalidated wholesale: an entry validates itself
    #: against the arrangement it is asked about, so a stale one cannot survive
    #: the thing that made it stale. See
    #: :class:`~netgraph.render.routes.RouteCache`.
    _routes: RouteCache = field(default_factory=RouteCache, init=False, repr=False)

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

    def grid(self) -> float:
        """The grid pitch ``snap to grid`` rounds to, in points."""
        self.inventory()  # ensures ``config`` matches the loaded revision
        return self.config.editor.grid if self.config is not None else DEFAULT_GRID

    def state(self, *, me: str | None = None) -> dict[str, Any]:
        """The small answer: has anything moved, may I write, and who else is here?

        Still polled by a client that could not open a stream, and still the
        first thing every client fetches — the stream tells you what *changed*,
        and this says what *is*. ``events`` names the push endpoint and the id of
        the last event published, so a client can subscribe from exactly the
        point this snapshot was taken and miss nothing in between.
        """
        with self._lock:
            state = {
                "mode": "session",
                "root": str(self.root),
                "revision": self._revision,
                "writable": self.writable,
                "undo": len(self._undo),
                "redo": len(self._redo),
                "undoLabel": self._undo[-1].label if self._undo else None,
                "redoLabel": self._redo[-1].label if self._redo else None,
                "maxFileBytes": MAX_FILE_BYTES,
                # The lattice ``snap to grid`` rounds to, so the page can say
                # what it is about to do rather than only doing it. A property
                # of the inventory (``[editor] grid``), not of the browser.
                "grid": self.grid(),
            }
        state["events"] = {
            "path": EVENTS_PATH,
            "lastEventId": self.events.last_id,
            "heartbeatMs": round(HEARTBEAT_SECONDS * 1000),
            "names": list(EVENT_NAMES),
            "streams": self.events.streams,
        }
        state["clients"] = self.presence.payload(me=me)
        state["editing"] = self.presence.editing()
        return state

    def tree(
        self, paths: Sequence[str] | None = None, *, diagnostics: bool = True
    ) -> dict[str, Any]:
        """Every file, its documents, and where each element was declared.

        This is the 1:1 mapping the project is built around, in one payload: a
        diagram node carries an address, an address appears here against a file
        and a line, and that is how "reveal the document that declares this" is
        answered without the page guessing at file names.

        Args:
            paths: Answer for these files only, rather than walking the tree.
                What an event-driven client asks for after a single-file save: a
                ``tree-changed`` event names the files that moved, and refetching
                a 1000-file listing to learn what one of them now hashes to is
                the cost this whole channel exists to remove. The answer carries
                ``partial: true`` and a ``missing`` list for anything named that
                is no longer there, so a client can patch its own list without
                guessing.
            diagnostics: Include the tree's findings. On by default, because a
                client that asked for the tree usually needs them; passed off by
                one that already has this revision's — an applied change comes
                back with them — since computing them means validating every
                element of the tree, partial fetch or not.

        Raises:
            SessionError: One of ``paths`` is not a path inside the inventory.
        """
        with self._lock:
            inventory = self.inventory()
            revision = self._revision
        documents = _documents_by_file(inventory)
        errors: list[LoadError] = []
        if paths is None:
            entries = [
                _file_entry(entry, documents)
                for entry in iter_inventory_files(self.root, errors=errors)
            ]
            missing: list[str] = []
        else:
            entries = []
            missing = []
            for relative in [relative_path(path) for path in paths]:
                pure = PurePosixPath(relative)
                target = self.root / pure
                if not target.is_file():
                    missing.append(relative)
                    continue
                # Through ``InventoryFile`` rather than by splitting the path
                # here, so a partial listing derives the namespace of a file by
                # exactly the rule the loader used to put it in one.
                entries.append(_file_entry(InventoryFile(path=target, relative=pure), documents))
        payload: dict[str, Any] = {
            "revision": revision,
            "root": str(self.root),
            "files": [entry.to_dict() for entry in entries],
            "discovery": [
                {"location": error.location, "message": error.message} for error in errors
            ],
        }
        if paths is not None:
            payload["partial"] = True
            payload["missing"] = missing
        if diagnostics:
            payload.update(_diagnostics_payload(self.diagnostics(inventory)))
        return payload

    def findings(
        self, inventory: Inventory | None = None, settings: ValidationConfig | None = None
    ) -> tuple[Finding, ...]:
        """What ``validate`` says about the tree, computed once per revision.

        One edit asks the validator the same question up to four times: the
        write path judges the tree it is about to write, the commit reports the
        diagnostics of the tree it wrote, the page fetches the file list with
        its diagnostics, and then fetches the diagram. On the benchmark tree
        that is four passes of a tenth of a second each, over objects that did
        not move between them.

        So the answer is remembered, and the memo is keyed by *identity* rather
        than by a revision number: :meth:`inventory` hands out a new object
        whenever the tree is reloaded and :meth:`settings` a new one whenever
        the config is, so an answer that is still valid is exactly an answer
        whose two inputs are still the same objects. Nothing has to remember to
        invalidate this, which is the point.
        """
        loaded = self.inventory() if inventory is None else inventory
        grading = self.settings() if settings is None else settings
        with self._lock:
            memo = self._findings
            if memo is not None and memo[0] is loaded and memo[1] is grading:
                return memo[2]
        answer = tuple(validate(loaded, grading))
        with self._lock:
            self._findings = (loaded, grading, answer)
        return answer

    def repairs(
        self, inventory: Inventory | None = None, settings: ValidationConfig | None = None
    ) -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
        """The mechanical repairs on offer, keyed as ``flatten_problems`` keys them.

        Every answer that carries problems carries these with them — the file
        list's diagnostics *and* the diagram's. The page draws one list from
        both, and a **Fix** button that came and went depending on which answer
        landed second would be a race the user could see.

        Computing them costs one pass over the findings and no file access: a
        producer is a pure function of the finding and the tree
        (:mod:`netgraph.fixes`).
        """
        loaded = self.inventory() if inventory is None else inventory
        return {
            (offer.finding.rule, offer.finding.message): tuple(
                (fix.key, fix.title) for fix in offer.fixes
            )
            for offer in offers_for(self.findings(loaded, settings), loaded)
        }

    def diagnostics(self, inventory: Inventory | None = None) -> tuple[Problem, ...]:
        """Every load error and finding of the tree, most severe group first."""
        loaded = self.inventory() if inventory is None else inventory
        return flatten_problems(loaded.errors, self.findings(loaded), fixes=self.repairs(loaded))

    def graph(
        self, view: ViewOptions | None = None, *, known: str | None = None
    ) -> tuple[Preview, int]:
        """Draw the tree, and say which revision was drawn.

        Args:
            view: Which graph to build and how to draw it.
            known: The :func:`~netgraph.web.preview.graph_digest` the caller
                already holds the drawing for. A tree can move — a description
                edited, a device added to a namespace this view filters out —
                without the picture moving at all, and on a large inventory the
                Graphviz run that would produce the identical SVG is the most
                expensive thing an edit triggers. When the fingerprints agree the
                answer says so and carries no picture.

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
        return (
            render_inventory(
                inventory,
                options,
                settings=settings,
                known=known,
                findings=self.findings(inventory, settings),
                fixes=self.repairs(inventory, settings),
                routes=self._routes,
            ),
            revision,
        )

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
        self,
        path: str,
        text: str,
        *,
        base_hash: str | None = None,
        force: bool = False,
        client: str | None = None,
    ) -> Change:
        """Replace one file wholesale, if it is still what the caller last read.

        The precondition is the point. Without it a browser tab left open over
        lunch would silently undo whatever was done in ``$EDITOR`` meanwhile;
        with it that write is refused and the page can show what is there now.
        ``base_hash`` of ``None`` means "I am creating this", and is refused if
        something is already there.

        ``client`` names who asked, and travels no further than the events this
        publishes: it lets a page recognise its own write and skip the reload the
        others do. It is not a permission and it is not checked — a request that
        claims somebody else's id can do nothing this one could not.

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
            return self._committed(
                self._commit(
                    [WriteFile(path=relative, text=text)],
                    label=f"edit {relative}",
                    force=force,
                    client=client,
                )
            )

    def apply(
        self,
        payload: Sequence[Mapping[str, Any]],
        *,
        revision: int | None = None,
        force: bool = False,
        client: str | None = None,
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
            return self._committed(
                self._commit(
                    operations, label=describe_batch(operations), force=force, client=client
                )
            )

    def arrange(
        self,
        command: str,
        *,
        view: str,
        addresses: Sequence[str],
        revision: int | None = None,
        client: str | None = None,
    ) -> Change:
        """Align, distribute or snap a selection, as one change.

        The tidying itself is :func:`~netgraph.edit.arrange.arrange_operations`,
        which reads the arrangement out of the tree's ``kind: layout`` documents
        and answers with one ``set-geometry`` per document that loses an entry.
        This adds what a session adds to any other write: the revision
        precondition, one entry in the undo stack, one validation, one save.

        A tidying that would move nothing writes nothing and does not move the
        revision — repeating an align is a no-op rather than a second identical
        step to undo.

        Args:
            command: One of :data:`~netgraph.edit.arrange.ARRANGEMENTS`.
            view: The layer being arranged.
            addresses: The selection, as element addresses.
            revision: The tree the page was looking at, as a precondition.
            client: Who asked, so their own tab is not told to reload.

        Raises:
            ReadOnly: This session does not write.
            SessionError: ``command`` is not one netgraph has.
            Conflict: ``revision`` is not the current one.
            EditError: The selection cannot be arranged; nothing is written.
        """
        self._require_writable()
        if command not in ARRANGEMENTS:
            raise SessionError(
                f"unknown arrangement {command!r}; expected one of {', '.join(ARRANGEMENTS)}"
            )
        with self._lock:
            if revision is not None and revision != self._revision:
                raise Conflict(
                    f"the inventory has changed since revision {revision} "
                    f"(it is now at {self._revision}); reload before arranging"
                )
            operations = arrange_operations(
                self.inventory(),
                command=command,
                view=view,
                addresses=list(addresses),
                grid=self.grid(),
            )
            if not operations:
                # Nothing moved. Answered rather than refused: aligning a row
                # that is already aligned succeeded, and a refusal would send
                # somebody looking for what they did wrong.
                return self._unchanged()
            return self._committed(
                self._commit(
                    operations,
                    label=describe_arrangement(command, len(addresses)),
                    force=False,
                    client=client,
                )
            )

    def reparent(
        self,
        addresses: Sequence[str],
        *,
        namespace: str,
        revision: int | None = None,
        force: bool = False,
        client: str | None = None,
    ) -> Change:
        """Re-home a selection into ``namespace``: the drop half of a drag (§2).

        What the canvas calls "dropping a switch into a rack" and what
        ``netgraph edit move`` calls a move are the same thing, and this is the
        one place that says so. :func:`~netgraph.edit.containers.move_plan`
        turns the drop into ``move`` operations and refuses an illegal one
        *before* any of them is applied, so the answer to a collision is the
        sentence naming both elements rather than a rolled-back batch and a
        validator's complaint.

        ``namespace`` is ``""`` for the root, which is what dropping onto empty
        canvas means. A drop that changes nothing — everything selected was
        already in that namespace — writes nothing and does not move the
        revision, for the reason :meth:`arrange` gives.

        Raises:
            ReadOnly: This session does not write.
            Conflict: ``revision`` is not the current one.
            EditError: The drop is not legal; nothing is written.
        """
        self._require_writable()
        with self._lock:
            self._check_revision(revision, "moving")
            inventory = self.inventory()
            plan = move_plan(
                inventory,
                list(addresses),
                namespace=namespace,
                files=EditableTree(root=self.root).facts(inventory),
            )
            if not plan.operations:
                return self._unchanged()
            return self._committed(
                self._commit(plan.operations, label=plan.describe(), force=force, client=client)
            )

    def _unchanged(self) -> Change:
        """The answer to a write that turned out to have nothing to write."""
        return Change(
            revision=self._revision,
            applied=(),
            inverse=(),
            files={},
            diagnostics=self.diagnostics(),
            undo_depth=len(self._undo),
            redo_depth=len(self._redo),
        )

    # -- the clipboard ---------------------------------------------------
    #
    # Four routes rather than one with a verb, because they are four different
    # kinds of thing: two of them write and two of them do not, and a caller has
    # to be able to tell which by looking at the URL. What they share is the
    # planning, and that is not here: it is
    # :mod:`netgraph.edit.clipboard`, so the browser, ``netgraph edit copy`` and
    # a script all get the same answer about what a copy is.

    def copy(self, addresses: Sequence[str], *, view: str | None = None) -> dict[str, Any]:
        """Serialise a selection for the system clipboard. Writes nothing.

        Deliberately *not* gated on ``--write``: reading a fragment out of a
        read-only session and pasting it into a writable one is a reasonable
        thing to do, and refusing it would be refusing a read.

        Raises:
            EditError: Nothing was named, or something named does not exist.
        """
        with self._lock:
            return clipboard_payload(self.inventory(), list(addresses), view=view)

    def cut(
        self,
        addresses: Sequence[str],
        *,
        view: str | None = None,
        revision: int | None = None,
        force: bool = False,
        client: str | None = None,
    ) -> tuple[dict[str, Any], Change]:
        """Copy a selection to the clipboard and delete it, as one change.

        One change rather than two, because a cut that half-failed — the payload
        made, the delete refused, or worse the other way round — is a state
        nobody asked for. The payload is built *first*, from the tree as it
        still is, and the delete only then goes through the ordinary batch.

        Returns:
            The clipboard payload, and what the delete did.

        Raises:
            ReadOnly: This session does not write.
            Conflict: ``revision`` is not the current one.
            EditError: The selection cannot be deleted; nothing is written.
        """
        self._require_writable()
        with self._lock:
            self._check_revision(revision, "cutting")
            payload = clipboard_payload(self.inventory(), list(addresses), view=view)
            wanted = [str(entry["address"]) for entry in payload["documents"]]
            operations: tuple[Operation, ...] = tuple(
                DeleteElement(address=address, cascade=True) for address in wanted
            )
            label = f"cut {len(wanted)} element{'' if len(wanted) == 1 else 's'}"
            return payload, self._committed(
                self._commit(operations, label=label, force=force, client=client)
            )

    def duplicate(
        self,
        addresses: Sequence[str],
        *,
        view: str | None = None,
        namespace: str | None = None,
        suffix: str = DEFAULT_SUFFIX,
        revision: int | None = None,
        force: bool = False,
        client: str | None = None,
    ) -> Change:
        """Copy a selection in place, links and geometry included.

        ``Ctrl-D``. The plan is
        :func:`~netgraph.edit.clipboard.copy_plan`'s, so what the browser writes
        and what ``netgraph edit duplicate`` writes are the same operations.

        Raises:
            ReadOnly: This session does not write.
            Conflict: ``revision`` is not the current one.
            EditError: The selection cannot be copied; nothing is written.
        """
        self._require_writable()
        with self._lock:
            self._check_revision(revision, "duplicating")
            plan = copy_plan(
                self.inventory(),
                list(addresses),
                namespace=namespace,
                suffix=suffix,
                view=view,
            )
            return self._applied_plan(plan, force=force, client=client)

    def paste(
        self,
        payload: Mapping[str, Any],
        *,
        namespace: str | None = None,
        view: str | None = None,
        at: tuple[float, float] | None = None,
        suffix: str = DEFAULT_SUFFIX,
        revision: int | None = None,
        force: bool = False,
        client: str | None = None,
    ) -> Change:
        """Write a clipboard fragment into this tree, as one change.

        The fragment may have come from this window, from another tab, or from
        another inventory entirely — nothing here reads a source element, so all
        three are the same code path.

        Raises:
            ReadOnly: This session does not write.
            Conflict: ``revision`` is not the current one.
            EditError: The payload is not a netgraph fragment, or cannot be
                written; nothing is written.
        """
        self._require_writable()
        with self._lock:
            self._check_revision(revision, "pasting")
            plan = paste_plan(
                self.inventory(),
                payload,
                namespace=namespace,
                suffix=suffix,
                view=view,
                at=at,
            )
            return self._applied_plan(plan, force=force, client=client)

    def _applied_plan(self, plan: CopyPlan, *, force: bool, client: str | None) -> Change:
        """Commit a copy plan, or answer that it had nothing to write."""
        if not plan.operations:
            # Every link in the fragment was dropped and there was nothing else:
            # answered rather than refused, for the reason ``arrange`` gives.
            return self._unchanged()
        return self._committed(
            self._commit(plan.operations, label=plan.describe(), force=force, client=client)
        )

    def _check_revision(self, revision: int | None, doing: str) -> None:
        """Refuse a write decided against a tree that has since moved on."""
        if revision is not None and revision != self._revision:
            raise Conflict(
                f"the inventory has changed since revision {revision} "
                f"(it is now at {self._revision}); reload before {doing}"
            )

    def fix(
        self,
        rule: str,
        message: str,
        *,
        key: str | None = None,
        revision: int | None = None,
        client: str | None = None,
    ) -> Change:
        """Apply the mechanical repair for one diagnostic, as one gesture.

        The finding is named by what identifies it everywhere else — its rule
        and its message — rather than by a position in a list, because the list
        the page is looking at may be several edits old and position 3 of it is
        not a thing that survives an edit. If nothing in the tree still reports
        that finding, the repair is refused instead of applied to whatever has
        taken its place.

        The repair goes through the same gate ``netgraph validate --fix`` uses:
        it is applied to a throwaway session and thrown away unless the finding
        is gone and no rule reports more than it did. Only then is it committed,
        as one labelled entry in the history and the journal — so the **Fix**
        button is undoable by ``Ctrl-Z`` and revertible from the changes drawer
        like anything else somebody did by hand.

        Args:
            rule: Canonical rule id of the finding, e.g. ``W138``.
            message: Its message, exactly as it was reported.
            key: Which repair to apply, for a rule that offers more than one.
                Required in that case; the single repair needs no name.
            revision: The tree the page was looking at, as a precondition.
            client: Who asked, so their own tab is not told to reload.

        Raises:
            ReadOnly: This session does not write.
            SessionError: The finding is not there, has no repair, or the repair
                would leave the tree worse than it found it.
            Conflict: ``revision`` is not the current one.
            EditError: The repair cannot be applied; nothing is written.
        """
        self._require_writable()
        with self._lock:
            if revision is not None and revision != self._revision:
                raise Conflict(
                    f"the inventory has changed since revision {revision} "
                    f"(it is now at {self._revision}); reload before fixing"
                )
            inventory = self.inventory()
            findings = validate(inventory, self.settings())
            finding = next(
                (entry for entry in findings if entry.rule == rule and entry.message == message),
                None,
            )
            if finding is None:
                raise SessionError(
                    f"this inventory no longer reports that {rule}; reload the problems list"
                )
            fix = _choose_fix(fixes_for(finding, inventory), rule=rule, key=key)
            probe = EditSession(root=self.root, config=self.config, cache=self.cache)
            outcome = apply_fix(probe, finding, fix, settings=self.settings(), before=findings)
            if not outcome.kept:
                raise SessionError(f"{rule} was not fixed: {outcome.reason}")
            return self._committed(
                self._commit(
                    fix.operations,
                    label=f"fix {rule}: {fix.title}",
                    force=False,
                    client=client,
                )
            )

    def undo(self, *, client: str | None = None) -> Change:
        """Put the last change back.

        The stack is the session's, not the page's, so an undo issued in one tab
        rewrites the files every other tab is showing. That is announced —
        ``file-changed`` for what moved, ``history-changed`` for the depths — so
        the other tab reloads rather than sitting on text that is nowhere on
        disk under a clean badge.

        Raises:
            ReadOnly: This session does not write.
            SessionError: There is nothing to undo.
        """
        return self._step(self._undo, self._redo, "undo", client=client)

    def redo(self, *, client: str | None = None) -> Change:
        """Apply the last undone change again."""
        return self._step(self._redo, self._undo, "redo", client=client)

    def _step(
        self,
        source: list[_History],
        target: list[_History],
        verb: str,
        *,
        client: str | None = None,
    ) -> Change:
        self._require_writable()
        with self._lock:
            if not source:
                raise SessionError(f"there is nothing to {verb}")
            entry = source[-1]
            # ``force``: this is a state the tree was already in, so refusing it
            # for the problems it has would leave no way back to it.
            change = self._commit(
                entry.backward, label=entry.label, force=True, history=False, client=client
            )
            source.pop()
            target.append(
                _History(label=entry.label, forward=entry.backward, backward=self._last_inverse)
            )
            restated = self._restated(change)
        # Outside the lock and unconditional: a step always moves the depths,
        # even when the batch it replayed wrote nothing.
        self.announce_history()
        return restated

    def _committed(self, change: Change) -> Change:
        """Announce the history depths after a write that moved them.

        A batch that wrote nothing moved nothing, and an event for it would wake
        every other tab to re-read two integers that did not change.
        """
        if change.files:
            self.announce_history()
        return change

    # -- the machinery ---------------------------------------------------

    def _commit(
        self,
        operations: Sequence[Operation],
        *,
        label: str,
        force: bool,
        history: bool = True,
        reverts: int | None = None,
        client: str | None = None,
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

        The batch is atomic in the strong sense: a bulk delete whose seventh
        element cannot go leaves the first six alone, because
        :class:`~netgraph.edit.Batch` puts the tree back before re-raising. That
        matters here and not on the command line, where a refusal ends the
        process anyway — this session is long-lived and goes on serving the tab
        that asked.
        """
        # Pin the session's starting state before the first write rather than
        # after it. Everything below eventually loads the tree, and a baseline
        # captured on the way *out* of the first commit would be the state after
        # that commit — so the drawer would never show the first thing you did.
        self.origin()
        session = EditSession(root=self.root, config=self.config, cache=self.cache)
        batch = Batch(session, label=label)
        result = batch.apply(operations)
        applied = result.applied
        changes = dict(result.changes)
        # Read before the write, which is the only moment the old text is still
        # on disk. The journal's hunk is the one thing here that cannot be
        # recomputed afterwards.
        previous = {path: self._text_on_disk(path) for path in changes}
        batch.commit(force=force)
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
            self.invalidate(changes, origin="session", client=client)
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

    def revert(
        self, entry_id: int, *, revision: int | None = None, client: str | None = None
    ) -> Change:
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
            return self._committed(
                self._commit(
                    entry.inverse,
                    label=f"revert {entry.label}",
                    force=True,
                    reverts=entry.id,
                    client=client,
                )
            )

    # -- the diff overlay ------------------------------------------------

    def diff(
        self,
        view: ViewOptions | None = None,
        *,
        against: str = SESSION_BASELINE,
        known: str | None = None,
    ) -> tuple[Preview, int]:
        """Draw the tree with what has changed since ``against`` painted on.

        Args:
            view: Which graph to build and how to draw it, as :meth:`graph`
                takes it.
            against: :data:`SESSION_BASELINE` — the tree as this session first
                saw it — or :data:`GIT_BASELINE`, which is ``HEAD`` as the
                inventory root looks in it.
            known: A fingerprint the caller already holds the overlay for, as
                :meth:`graph` takes it.

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
        preview = render_diff(before, inventory, plan, options, settings=settings, known=known)
        return preview, revision

    # -- the failure overlay ---------------------------------------------

    def impact(self, failed: Sequence[str], *, layer: str = "l1") -> dict[str, Any]:
        """What would stop being reachable if ``failed`` failed. Reads only.

        The editor's failure mode: click an element, and everything it would
        isolate greys out. Nothing about this touches a file, a revision or the
        undo stack — it is a question asked of the tree as it stands, and the
        answer is thrown away when the next element is clicked.

        Args:
            failed: Elements to remove, in the spellings
                :func:`~netgraph.impact.resolve_element` takes. Usually one: the
                shape the pointer was on.
            layer: Which view to measure in, one of
                :data:`~netgraph.impact.LAYERS`.

        Returns:
            The addresses that were removed, the addresses that lose their path
            to a gateway, and one line naming the count — everything the overlay
            needs and nothing it does not. The full analysis is ``netgraph
            impact``; a canvas has room for a number.

        Raises:
            SessionError: ``layer`` is not a layer, or nothing carries the name.
        """
        if layer not in IMPACT_LAYERS:
            raise SessionError(f"unknown layer {layer!r}; expected {', '.join(IMPACT_LAYERS)}")
        with self._lock:
            inventory = self.inventory()
            revision = self._revision
        try:
            report = simulate(inventory, fail=list(failed), wanted_layers=(layer,))
        except ImpactError as exc:
            raise SessionError(str(exc)) from exc
        result = report.layers[0] if report.layers else None
        isolated = list(result.isolated) if result is not None else ()
        removed = [failure.element for failure in report.failures]
        return {
            "revision": revision,
            "layer": layer,
            "failed": removed,
            "isolated": list(isolated),
            "anchors": list(report.anchors),
            "counts": {
                "failed": len(removed),
                "isolated": len(isolated),
                "served": result.served_before if result is not None else 0,
            },
            "message": _impact_summary(report, len(isolated), len(removed)),
        }

    def cascade(self, addresses: Sequence[str]) -> dict[str, Any]:
        """What deleting ``addresses`` would take with it. Reads only.

        The editor asks this *before* it deletes, so that the question it puts
        to the person pressing Delete is the truth rather than a guess made from
        the picture: the cables that die with a switch are visible on the canvas,
        but the tunnel three levels up that runs over one of them is not, and
        neither is the note anchored to it or the group that lists it.

        Nothing here changes a file, a revision or the undo stack. The plan is
        computed against the tree as it stands and thrown away; the delete that
        follows recomputes it, so two browsers deleting at once cannot act on
        each other's stale answer — the revision check is what catches that, and
        it is on the write, where it belongs.

        Args:
            addresses: What is to be deleted, in any spelling an address takes.
                A link may be named with the ``#`` suffix the canvas puts on
                one; it is dropped, because that suffix names a *drawn edge* and
                what is being deleted is the cable.

        Returns:
            :meth:`~netgraph.edit.cascade.CascadePlan.to_dict`, plus the
            ``revision`` it was computed at, the resolved ``asked`` addresses
            and a ``message`` naming the whole of it in one line. Addresses that
            resolve to nothing come back under ``unknown`` rather than raising:
            a stale selection is a normal thing for a canvas to hold, and the
            delete that follows will refuse them one at a time and say so.
        """
        with self._lock:
            inventory = self.inventory()
            revision = self._revision
        resolved: list[str] = []
        unknown: list[str] = []
        for address in addresses:
            fqn = inventory.lookup(address.split("#")[0]).fqn
            (resolved if fqn is not None else unknown).append(fqn or address)
        plan = plan_cascade(inventory, resolved)
        return {
            "revision": revision,
            "unknown": unknown,
            "takes_more": plan.takes_more,
            "message": describe_cascade(plan).removeprefix(", and "),
            **plan.to_dict(),
        }

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

    # -- the history timeline --------------------------------------------

    def timeline(self) -> Timeline:
        """The history of this inventory, opened once and kept.

        Kept rather than reopened because the caches hang off it: the loaded
        inventory of the frame you just looked at is the one the next frame
        needs, and the rendered frames are what make scrubbing back instant.
        The commit *list* is not cached — a session outlives commits, and a
        scrubber missing the commit somebody just made is the same lie the
        changes drawer refuses to tell.

        Raises:
            SessionError: The root is not in a git repository, or ``git``
                cannot be run at all.
        """
        self.inventory()  # ensures ``config`` matches the loaded revision
        with self._lock:
            if self._timeline is None:
                bound = (
                    self.config.history.max_revisions
                    if self.config is not None
                    else DEFAULT_MAX_REVISIONS
                )
                try:
                    self._timeline = Timeline.open(self.root, max_revisions=bound, cache=self.cache)
                except HistoryError as exc:
                    raise SessionError(str(exc)) from exc
            return self._timeline

    def history(self, *, limit: int | None = None) -> dict[str, Any]:
        """The commits a scrubber can step through, newest first.

        Without their changesets: listing thirty commits costs one ``git log``,
        and summarising them would cost sixty inventory loads for a panel that
        shows one at a time. :meth:`frame` computes the changeset of the commit
        actually selected.

        A repository with more history than the bound allows is **truncated to
        the newest** rather than refused — a scrubber that shows nothing because
        there is too much to show is not the honest answer here, and the cost
        the bound exists to prevent is per *frame*, which is drawn one at a
        time. What is refused is an explicit range: ``netgraph log --from`` and
        ``--to`` name what they want and are told when it is more than this
        will read. Either way the count is reported, so the page can say it is
        showing the newest hundred of three hundred rather than implying that
        is all there is.

        Raises:
            SessionError: There is no history to read at all.
        """
        timeline = self.timeline()
        bound = min(limit, timeline.max_revisions) if limit else timeline.max_revisions
        try:
            total = timeline.count()
            commits = timeline.commits(limit=bound)
        except HistoryError as exc:
            raise SessionError(str(exc)) from exc
        return {
            "commits": [commit.to_dict() for commit in commits],
            "bound": timeline.max_revisions,
            "total": total,
            "truncated": total > len(commits),
            "root": timeline.prefix or ".",
        }

    def frame(
        self,
        rev: str,
        view: ViewOptions | None = None,
        *,
        known: str | None = None,
    ) -> dict[str, Any]:
        """One commit of the history, drawn as the diff against its parent.

        The positions come from the layout document *as that revision had it*,
        because both sides of the diff are that revision's own tree — so a
        diagram that was arranged when the commit was made stays arranged when
        it is scrubbed back to.

        Args:
            rev: The commit to draw. A full hash from :meth:`history`; anything
                git resolves also works, which is what makes the route usable
                by hand.
            view: Which graph to build and how to draw it.
            known: A fingerprint the caller already holds this frame for, as
                :meth:`graph` takes it.

        Returns:
            The rendering, the commit it is of, and the one-line summary of
            what it did. A revision that cannot be read comes back as a failed
            rendering carrying the reason, never as an exception and never as a
            blank frame with no explanation.

        Raises:
            SessionError: There is no history, or no such revision.
        """
        timeline = self.timeline()
        try:
            commit = timeline.commit(rev)
        except HistoryError as exc:
            raise SessionError(str(exc)) from exc
        options = view or ViewOptions()
        if options.icons is None and self.icons is not None:
            options = replace(options, icons=self.icons)

        # Keyed before anything is read. The pair of tree hashes costs two
        # object lookups and decides the whole answer — the changeset as much as
        # the picture — so a frame scrubbed back to costs neither an export, nor
        # a load, nor a diff, nor a Graphviz run.
        key = (*timeline.trees(commit), options)
        held = self._frames.get(key)
        if held is None:
            frame = timeline.frame(commit)
            if not frame.ok:
                # Not an exception: a revision that does not load is a fact
                # about the history, and a scrubber must be able to stop on it
                # and read why rather than jumping over it or going blank. Not
                # cached either — the next attempt should try again, in case
                # what could not be read was the disk rather than the commit.
                return (
                    commit.to_dict()
                    | frame.derived()
                    | Preview(status=Status.FAILED, message=frame.summary).to_dict()
                )
            assert frame.plan is not None  # frame.ok said so
            preview = render_diff(
                frame.before.require(), frame.after.require(), frame.plan, options
            )
            if preview.status is Status.FAILED:
                return commit.to_dict() | frame.derived() | preview.to_dict()
            held = self._frames.put(key, (frame.derived(), preview))
        derived, preview = held
        if known is not None and known == preview.graph_hash:
            preview = replace(preview, svg=None, details={}, unchanged=True)
        return commit.to_dict() | derived | preview.to_dict()

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

    def invalidate(
        self, paths: Iterable[str] = (), *, origin: str = "disk", client: str | None = None
    ) -> int:
        """Note that the tree has changed, drop what was derived from it, and say so.

        Called by :class:`TreeWatcher` for a change made outside this session,
        and by the session itself after a write. The *authority* of the two is
        deliberately not distinguished — the revision moves either way, and
        pretending to know which of several writers is the real one is how an
        editor comes to show a tree that is not there — but their *provenance*
        is, because a client that made a change already knows what it did, and
        one that did not has to reload the file it has open.

        ``paths`` is what moved. A watcher hands over absolute paths and a commit
        hands over inventory-relative ones; both are normalised here, and
        anything outside the tree (``netgraph.toml``, a file the loader skips)
        still bumps the revision but names no file, which is the honest way to
        say "something changed and it was not one of your documents".
        """
        with self._lock:
            self._revision += 1
            self._inventory = None
            revision = self._revision
        moved, outside = self._relative(paths)
        for path in moved:
            self.events.publish(
                "file-changed",
                revision=revision,
                path=path,
                hash=self._digest_on_disk(path),
                origin=origin,
                client=client,
            )
        if origin == "disk":
            self.events.publish(
                "disk-changed", revision=revision, files=list(moved), outside=outside
            )
        self.events.publish(
            "tree-changed",
            revision=revision,
            files=list(moved),
            outside=outside,
            origin=origin,
            client=client,
        )
        return revision

    def _relative(self, paths: Iterable[str]) -> tuple[tuple[str, ...], bool]:
        """``paths`` as inventory-relative POSIX paths, and "was there anything else".

        Anything that is not a YAML document below the root — the config file,
        an editor's swap file that slipped past the filter — is reported only as
        the flag: naming it would invite a client to fetch it, and
        :func:`relative_path` would refuse to.
        """
        found: dict[str, None] = {}
        outside = False
        root = self.root.resolve()
        for given in paths:
            candidate = Path(given)
            if candidate.is_absolute():
                try:
                    candidate = candidate.resolve().relative_to(root)
                except (OSError, ValueError):
                    outside = True
                    continue
            try:
                found.setdefault(relative_path(candidate.as_posix()), None)
            except SessionError:
                outside = True
        return tuple(found), outside

    def announce_history(self) -> None:
        """Publish where the undo and redo stacks now stand.

        Its own event because the stacks move without the tree moving — a client
        that cannot undo any more needs its buttons greyed whether or not it made
        the change that emptied the stack. This is the mechanism behind "an undo
        issued from one tab lands in the other".
        """
        with self._lock:
            revision = self._revision
            undo, redo = len(self._undo), len(self._redo)
            undo_label = self._undo[-1].label if self._undo else None
            redo_label = self._redo[-1].label if self._redo else None
        self.events.publish(
            "history-changed",
            revision=revision,
            undo=undo,
            redo=redo,
            undoLabel=undo_label,
            redoLabel=redo_label,
        )

    # -- who else is here ------------------------------------------------

    def join(self, *, streaming: bool = False, client_id: str | None = None) -> Client:
        """Issue (or hand back) a client id, and tell everyone the list moved."""
        client = self.presence.join(streaming=streaming, client_id=client_id)
        self.announce_presence()
        return client

    def leave(self, client_id: str) -> None:
        """Drop a client, if it was there, and tell everyone."""
        if self.presence.leave(client_id):
            self.announce_presence()

    def report(
        self,
        client_id: str,
        *,
        selection: Sequence[str] | None = None,
        editing: Sequence[str] | None = None,
        view: str | None = None,
    ) -> Client:
        """Record what a client has selected and is editing, and tell the others.

        An unknown id — an expired entry, a restarted server — is given a new
        identity rather than refused: a page whose presence lapsed while its user
        was at lunch should rejoin, not lose the ability to say what it is doing.

        Only a change anybody else can see is announced. A keepalive that moved
        nothing wakes nobody, which is what lets this double as the polling
        client's heartbeat.
        """
        client, changed = self.presence.update(
            client_id, selection=selection, editing=editing, view=view
        )
        if client is None:
            fresh = self.presence.join(client_id=None)
            client, _ = self.presence.update(
                fresh.id, selection=selection, editing=editing, view=view
            )
            assert client is not None  # just created, under no lock in between
            self.announce_presence()
            return client
        if changed:
            self.announce_presence()
        return client

    def stream_ended(self, client_id: str) -> None:
        """Note that a client's event stream closed, without dropping the client.

        A browser reconnects a dropped stream within seconds and expects its
        identity back, so the entry stays and merely stops claiming to be
        streaming; :data:`~netgraph.web.presence.PRESENCE_TTL` removes it if the
        reconnect never comes.
        """
        _, changed = self.presence.update(client_id, streaming=False)
        if changed:
            self.announce_presence()

    def announce_presence(self) -> None:
        """Publish the client list. Cheap, and never carries anything from disk."""
        self.events.publish("presence", revision=self.revision, clients=self.presence.payload())

    def close(self) -> None:
        """Wake and drop every open stream, so the process can exit.

        A stream is a request thread parked in a wait. The socket server's
        shutdown does not touch it — the threads are daemons, so the process
        would still exit — but a test that stops a server and then asserts on
        what a reader saw needs the readers to have finished, and a user pressing
        Ctrl-C should not wait for a heartbeat to come round.
        """
        self.events.close()


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
        if isinstance(operation, (CreateElement, CopyElement)) and isinstance(name, str):
            namespace = operation.namespace or ""
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


def _impact_summary(report: ImpactReport, isolated: int, removed: int) -> str:
    """The one line the status bar has room for.

    Written to be read at a glance while the pointer is still over the shape:
    what would go, how much would go with it, and — when the answer is "nothing"
    — the reason, because "0 isolated" invites the reader to wonder whether the
    question was asked at all.
    """
    subject = short_name(report.failures[0].element) if report.failures else "nothing"
    if removed > 1:
        subject = f"{subject} and {removed - 1} more"
    if not report.anchors:
        return f"{subject}: no gateway is declared, so nothing can be measured"
    if not isolated:
        return f"{subject} isolates nothing: every other element keeps a path to a gateway"
    noun = "element" if isolated == 1 else "elements"
    return f"{subject} isolates {isolated} {noun} from the gateways"


#: Re-exported so a caller catching "the session refused" catches one name.
EDIT_ERRORS: Final = (SessionError, EditError, ConflictError, ValidationRefused)
