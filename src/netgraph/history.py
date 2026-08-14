"""The inventory's history: the commits that changed it, and the diff each carries.

An inventory is a folder of YAML in a repository, so its *whole history* is
renderable. This module is the plumbing that makes that true — everything
between ``git`` and a :class:`~netgraph.plan.Plan`, and nothing above it.

What it answers
---------------

:func:`Timeline.commits`
    The commits that touched the inventory, newest first, as
    :class:`Commit` records carrying subject, author, date, parents and the
    hash of the inventory's *tree* at that revision.
:func:`Timeline.revision`
    The inventory as it stood at one revision, read out of the object database
    with ``git archive`` into a temporary directory. The working tree, the index
    and the checked-out branch are never touched, so scrubbing a timeline in one
    window cannot disturb an editor open in another.
:func:`Timeline.frame`
    One commit against its parent: the two inventories and the changeset
    between them, which is what ``netgraph diff`` draws and what
    ``netgraph log`` summarises.

Three edges it is honest about
------------------------------

**A revision whose inventory does not load** comes back as a
:class:`Revision` with :attr:`~Revision.error` set, and a :class:`Frame` that
says so. It is never silently skipped: a commit that broke the tree is
precisely the one a reader scrubbing the history is looking for.

**A revision with no inventory folder** is the same shape with a different
message, because a repository that grew its ``netgraph/`` directory at some
commit legitimately has nothing to draw before it.

**A range wider than :attr:`Timeline.max_revisions`** is refused by
:func:`Timeline.commits` before anything is loaded. Rendering is the expensive
part — two Graphviz runs per frame — and a scrubber over four hundred commits
that nobody asked for is a hung interface, not a feature.

Caching
-------

Two caches, both keyed by **tree hash** rather than by commit, so a revert, a
rebase and a cherry-pick all hit:

* :class:`Timeline` remembers the loaded :class:`~netgraph.loader.Inventory` for
  the last few trees it read. Stepping a frame forward reuses the previous
  frame's *after* state as the next frame's *before* state, so a linear walk
  loads one inventory per step rather than two.
* :class:`FrameCache` remembers whatever a caller derived from a *pair* of
  trees — the rendered SVG, in ``netgraph web``'s case — so scrubbing back over
  ground already covered is a dictionary lookup.

Both are bounded and thread-safe: a :class:`Timeline` is shared by every client
of one editing session.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Generic, TypeVar

from netgraph.config import DEFAULT_MAX_REVISIONS
from netgraph.errors import LoaderError, NetgraphError
from netgraph.loader import DocumentCache, Inventory, load_tree
from netgraph.plan import Plan
from netgraph.plan import diff as diff_states
from netgraph.plan.address import ADDRESS_TYPES, DEVICE_TYPE, LAYOUT_TYPE
from netgraph.plan.model import Action, Change, StateRef
from netgraph.plan.sources import (
    MissingInventory,
    PlanSourceError,
    check_revision,
    git,
    git_ref,
    inventory_prefix,
    repository_of,
)
from netgraph.plan.state import state_digest

__all__ = [
    "DEFAULT_MAX_REVISIONS",
    "Commit",
    "Frame",
    "FrameCache",
    "HistoryError",
    "Revision",
    "Timeline",
    "summarise",
]

#: How many loaded inventories one timeline keeps. A linear scrub needs two —
#: the frame's own and its parent's — and a few more make stepping backwards as
#: cheap as stepping forwards without holding a whole history in memory.
_STATE_CACHE: Final = 6

#: Field and record separators for ``git log --format``. Both are control
#: characters no commit subject, author name or ISO date can hold, which is what
#: makes the output parseable without quoting rules.
_FIELD: Final = "\x1f"
_RECORD: Final = "\x1e"

_LOG_FORMAT: Final = _FIELD.join(("%H", "%P", "%an", "%ae", "%aI", "%s")) + _RECORD

#: A commit hash, abbreviated for display. Long enough to stay unambiguous in a
#: repository with a decade of history, short enough to sit in a scrubber label.
_ABBREV: Final = 9

#: What the state before the first commit is called, where a revision would go.
_ROOT: Final = "(nothing)"


class HistoryError(NetgraphError):
    """The history cannot be read, or was asked for in a way that is refused."""


# --------------------------------------------------------------------------- #
# What a commit is
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit that touched the inventory."""

    #: Full hash. The one identity everything else is keyed by.
    hash: str
    #: Parents, in git's order. Empty for a root commit; more than one for a
    #: merge, whose frame is drawn against the first.
    parents: tuple[str, ...]
    #: ``metadata.name``-free facts for the label beside a scrubber.
    author: str
    email: str
    #: Author date, timezone-aware, as ``%aI`` gives it.
    when: datetime
    #: The commit subject: the first line of its message.
    subject: str
    #: Hash of the inventory *directory*'s tree object at this commit, or
    #: ``None`` when the directory does not exist there. This is the cache key
    #: for everything derived from the revision, because two commits that leave
    #: the inventory identical share it and a rebase preserves it.
    tree: str | None = None

    @property
    def abbrev(self) -> str:
        """The short hash a reader is shown."""
        return self.hash[:_ABBREV]

    @property
    def parent(self) -> str | None:
        """The revision this commit's frame is drawn against."""
        return self.parents[0] if self.parents else None

    @property
    def date(self) -> str:
        """``YYYY-MM-DD``, in the author's own timezone."""
        return self.when.strftime("%Y-%m-%d")

    def to_dict(self) -> dict[str, Any]:
        """The JSON form ``netgraph log`` prints and the editor fetches."""
        return {
            "hash": self.hash,
            "abbrev": self.abbrev,
            "parents": list(self.parents),
            "author": self.author,
            "email": self.email,
            "date": self.when.isoformat(),
            "subject": self.subject,
            "tree": self.tree,
        }


@dataclass(frozen=True, slots=True)
class Revision:
    """The inventory as one revision has it — or why it has none."""

    #: What was asked for: a hash, ``HEAD``, a branch name.
    rev: str
    #: The inventory tree hash, or ``None`` when there is no inventory there.
    tree: str | None
    #: The loaded inventory. ``None`` exactly when :attr:`error` is set.
    inventory: Inventory | None = None
    #: Why it could not be read, in one line fit to show a user.
    error: str | None = None
    #: Is the reason simply that the inventory directory is not in this
    #: revision? Its own flag rather than a substring of :attr:`error`, because
    #: a *frame* treats "the folder did not exist yet" as an empty network and
    #: "the YAML does not parse" as a failure, and the two must not be told
    #: apart by reading English.
    missing: bool = False

    @property
    def ok(self) -> bool:
        return self.inventory is not None

    def require(self) -> Inventory:
        """The inventory, or the reason there is none, raised.

        Raises:
            HistoryError: :attr:`error` is set.
        """
        if self.inventory is None:
            raise HistoryError(self.error or f"the inventory at {self.rev} could not be read")
        return self.inventory


@dataclass(frozen=True, slots=True)
class Frame:
    """One commit against its parent: the pair of states, and what moved."""

    commit: Commit
    #: The parent's state. A root commit's parent is an empty inventory, so the
    #: first frame of a history reads as "everything was added" rather than as
    #: an error — which is what it is.
    before: Revision
    #: The commit's own state.
    after: Revision
    #: The changeset, or ``None`` when either side could not be read.
    plan: Plan | None = None
    #: Something true about this frame that its changeset cannot say: that one
    #: side of it predates the inventory, most often. Shown beside the summary
    #: rather than instead of it.
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.plan is not None

    @property
    def error(self) -> str | None:
        """Why this frame cannot be drawn, or ``None``."""
        if self.plan is not None:
            return None
        return self.after.error or self.before.error

    @property
    def summary(self) -> str:
        """One line: what this commit did to the network, or why nobody knows."""
        if self.plan is None:
            return self.error or "this revision cannot be read"
        summary = summarise(self.plan)
        return f"{summary} ({self.note})" if self.note else summary

    def derived(self) -> dict[str, Any]:
        """What this frame turned out to hold, without saying which commit it is.

        Separate from :meth:`to_dict` because it is a fact about the *pair of
        trees* rather than about the commit: two commits that leave the
        inventory in the same state carry the same changeset, which is what
        makes this the half worth caching by tree hash.
        """
        payload: dict[str, Any] = {
            "summary": self.summary,
            "error": self.error,
            "note": self.note,
        }
        if self.plan is not None:
            # ``changes`` rather than ``counts``: a rendering carries a
            # ``counts`` of its own — nodes and edges — and a frame is both at
            # once, so the two must not collide when they are merged.
            payload["changes"] = {
                action.value: count for action, count in self.plan.counts().items()
            }
        return payload

    def to_dict(self) -> dict[str, Any]:
        """The JSON form, for ``netgraph log -F json`` and the editor."""
        return self.commit.to_dict() | self.derived()


# --------------------------------------------------------------------------- #
# Summarising a changeset in one line
# --------------------------------------------------------------------------- #

#: What each address type is called in a summary, singular and plural. A reader
#: of ``netgraph log`` wants "3 devices", not "3 device elements".
_NOUNS: Final[dict[str, tuple[str, str]]] = {
    DEVICE_TYPE: ("device", "devices"),
    "cable": ("link", "links"),
    "adapter": ("adapter", "adapters"),
    "tunnel": ("tunnel", "tunnels"),
    "patchpanel": ("patch panel", "patch panels"),
    "pdu": ("PDU", "PDUs"),
    "user": ("user", "users"),
    "group": ("group", "groups"),
    LAYOUT_TYPE: ("arrangement", "arrangements"),
}

#: What each action did, as a summary spells it.
_VERBS: Final[dict[Action, str]] = {
    Action.CREATE: "added",
    Action.DELETE: "removed",
    Action.RENAME: "renamed",
    Action.UPDATE: "changed",
}

#: An update whose every field path sits under one of these is summarised by the
#: *field* rather than by the element, because "2 addresses moved" is the
#: sentence a reader of a network's history is looking for and "2 devices
#: changed" is not.
_FIELD_NOUNS: Final[tuple[tuple[str, tuple[str, str], str], ...]] = (
    ("addresses", ("address", "addresses"), "moved"),
    ("interfaces", ("interface", "interfaces"), "changed"),
)

#: The order phrases come out in: element type first, then action, so a
#: summary reads devices before cables and additions before deletions.
_TYPE_ORDER: Final[dict[str, int]] = {name: rank for rank, name in enumerate(ADDRESS_TYPES)}
_ACTION_ORDER: Final[dict[Action, int]] = {action: rank for rank, action in enumerate(Action)}


def summarise(plan: Plan) -> str:
    """``3 devices added, 1 link removed, 2 addresses moved``.

    The one-line form of a changeset: what sort of thing moved, how many, and
    in which direction. Deliberately not :func:`~netgraph.plan.summary_line`,
    which counts *actions* — ``+ 3 to add`` is the right summary for a plan
    somebody is about to apply and the wrong one for a commit somebody is
    reading, where the question is what the network gained and lost.

    An empty plan says so: a commit can touch a YAML file — a comment, a
    reformat, a key reordered — without changing the network it describes, and
    a history that claimed otherwise would be lying about the diff it drew.
    """
    counts: dict[tuple[str, Action, str, str], int] = {}
    for change in plan:
        singular, plural, verb = _phrase_for(change)
        key = (change.address.type, change.action, f"{singular}\x00{plural}", verb)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "no change to the network"
    parts = []
    for (kind, action, nouns, verb), count in sorted(
        counts.items(), key=lambda item: (_TYPE_ORDER[item[0][0]], _ACTION_ORDER[item[0][1]])
    ):
        del kind, action
        singular, plural = nouns.split("\x00")
        parts.append(f"{count} {singular if count == 1 else plural} {verb}")
    return ", ".join(parts)


def _phrase_for(change: Change) -> tuple[str, str, str]:
    """``(singular, plural, verb)`` for one entry of a changeset."""
    singular, plural = _NOUNS.get(change.address.type, ("element", "elements"))
    verb = _VERBS[change.action]
    if change.action is not Action.UPDATE or not change.fields:
        return singular, plural, verb
    paths = [field.text for field in change.fields]
    for marker, (one, many), moved in _FIELD_NOUNS:
        if all(marker in path for path in paths):
            return one, many, moved
    return singular, plural, verb


# --------------------------------------------------------------------------- #
# A bounded, keyed cache
# --------------------------------------------------------------------------- #

_T = TypeVar("_T")


class FrameCache(Generic[_T]):
    """A small thread-safe LRU, keyed by whatever identifies a frame.

    Separate from :class:`Timeline` because what a frame *is* depends on who is
    asking: ``netgraph log`` wants the changeset, the editor wants a rendered
    SVG, and neither should have to hold the other's answer in memory. Both key
    by tree hash, which is why scrubbing back and forth costs nothing the second
    time.
    """

    __slots__ = ("_entries", "_lock", "hits", "misses", "size")

    def __init__(self, size: int = 16) -> None:
        self.size = max(1, size)
        self._entries: OrderedDict[Any, _T] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Any) -> _T | None:
        """The cached value, or ``None``. A hit is moved to the front."""
        with self._lock:
            if key not in self._entries:
                self.misses += 1
                return None
            self._entries.move_to_end(key, last=False)
            self.hits += 1
            return self._entries[key]

    def put(self, key: Any, value: _T) -> _T:
        """Remember ``value``, evicting the least recently used if need be."""
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key, last=False)
            while len(self._entries) > self.size:
                self._entries.popitem(last=True)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# --------------------------------------------------------------------------- #
# The timeline
# --------------------------------------------------------------------------- #


@dataclass
class Timeline:
    """The history of one inventory directory, read out of one repository.

    Construct it with :meth:`open`, which is the call that fails when there is
    no repository — everything after that either answers or explains itself per
    revision.
    """

    #: The inventory root the history is of.
    root: Path
    #: The work tree it sits in.
    repository: Path
    #: Where the inventory sits inside the repository, ``""`` at its root.
    prefix: str
    #: Ceiling on how many revisions one range may hold.
    max_revisions: int = DEFAULT_MAX_REVISIONS
    #: The parse cache, shared with the rest of the run. Two revisions differ in
    #: a handful of files and agree on the other two thousand, and the cache is
    #: keyed by inventory-relative path and content — so a revision read after
    #: its neighbour parses only what the commit actually changed. ``None``
    #: parses every file of every revision.
    cache: DocumentCache | None = None
    #: Loaded inventories, keyed by tree hash.
    _states: FrameCache[Revision] = field(
        default_factory=lambda: FrameCache(_STATE_CACHE), repr=False
    )

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        max_revisions: int = DEFAULT_MAX_REVISIONS,
        cache: DocumentCache | None = None,
    ) -> Timeline:
        """The timeline of the inventory at ``root``.

        Raises:
            HistoryError: ``root`` is not in a git repository, or ``git`` cannot
                be run at all.
        """
        try:
            repository = repository_of(root)
            prefix = inventory_prefix(root, repository)
        except PlanSourceError as exc:
            raise HistoryError(str(exc)) from exc
        return cls(
            root=root,
            repository=repository,
            prefix=prefix,
            max_revisions=max(1, max_revisions),
            cache=cache,
        )

    # -- the commits ---------------------------------------------------------

    def count(self, *, since: str | None = None, until: str = "HEAD") -> int:
        """How many commits the range holds, without listing any of them.

        Asked first so a range too wide to draw is refused before anything is
        read, which is the difference between "that is more history than I will
        render" and a browser tab that stops answering.
        """
        output = self._git(
            ["rev-list", "--count", *self._range(since, until), *self._pathspec()],
            failure=self._range_failure(since, until),
        )
        return int(output.decode("utf-8", "replace").strip() or 0)

    def commits(
        self,
        *,
        since: str | None = None,
        until: str = "HEAD",
        limit: int | None = None,
    ) -> tuple[Commit, ...]:
        """The commits that changed the inventory, newest first.

        Args:
            since: Exclusive lower bound, as ``git log a..b`` means it: the
                revision itself is *not* listed, because it is the state the
                first listed commit is drawn against.
            until: Inclusive upper bound. ``HEAD`` by default.
            limit: Keep only this many, newest first. A limit is not a bound —
                it is applied by git and always honoured; the bound is about
                refusing a range nobody can look at.

        Raises:
            HistoryError: The range is wider than :attr:`max_revisions` and no
                ``limit`` narrows it, or git cannot read it.
        """
        if limit is None or limit > self.max_revisions:
            total = self.count(since=since, until=until)
            if total > self.max_revisions:
                raise HistoryError(
                    f"{_range_text(since, until)} holds {total} revisions of the inventory, more "
                    f"than the bound of {self.max_revisions}; narrow the range, pass a limit, or "
                    f"raise 'max-revisions' in the [history] table of netgraph.toml"
                )
        arguments = ["log", f"--format={_LOG_FORMAT}", "--no-color"]
        if limit is not None:
            arguments.append(f"--max-count={max(0, limit)}")
        arguments += [*self._range(since, until), *self._pathspec()]
        output = self._git(arguments, failure=self._range_failure(since, until))
        commits = tuple(_parse_log(output.decode("utf-8", "replace")))
        return self._with_trees(commits)

    def commit(self, rev: str) -> Commit:
        """The one commit ``rev`` names, whether or not it touched the inventory.

        No pathspec, unlike :meth:`commits`: a caller with a revision in hand is
        asking about *that* revision, and answering with the newest inventory
        change at or before it would silently draw a different frame from the
        one that was asked for.

        Raises:
            HistoryError: git does not resolve ``rev`` to a commit.
        """
        output = self._git(
            [
                "log",
                "--max-count=1",
                f"--format={_LOG_FORMAT}",
                "--no-color",
                self._revision_argument(rev),
                "--",
            ],
            failure=f"git cannot read the revision {rev!r} in this repository",
        )
        found = tuple(_parse_log(output.decode("utf-8", "replace")))
        if not found:  # pragma: no cover - a resolving rev always logs a commit
            raise HistoryError(f"{rev} names no commit in this repository")
        return self._with_trees(found)[0]

    def _with_trees(self, commits: Sequence[Commit]) -> tuple[Commit, ...]:
        """Fill in each commit's inventory tree hash, in one batch.

        ``git cat-file --batch-check`` takes the whole list on stdin and answers
        in order, so a hundred commits cost one process rather than a hundred.
        """
        if not commits:
            return ()
        specs = [self._tree_spec(commit.hash) for commit in commits]
        answers = self._batch_check(specs)
        return tuple(
            Commit(**{**_as_kwargs(commit), "tree": tree})
            for commit, tree in zip(commits, answers, strict=True)
        )

    def tree_of(self, rev: str) -> str | None:
        """The inventory tree hash at ``rev``, or ``None`` when it has none."""
        return self._batch_check([self._tree_spec(rev)])[0]

    def _batch_check(self, specs: Sequence[str]) -> list[str | None]:
        """Resolve each spec to an object hash, ``None`` when it resolves to none."""
        stdin = ("\n".join(specs) + "\n").encode("utf-8")
        output = self._git(
            ["cat-file", "--batch-check"],
            failure="git cannot resolve the inventory tree of a revision",
            stdin=stdin,
        )
        answers: list[str | None] = []
        for line in output.decode("utf-8", "replace").splitlines():
            parts = line.split()
            answers.append(parts[0] if len(parts) >= 3 and parts[1] == "tree" else None)
        # A malformed answer is a bug in this function's contract with git, not
        # a fact about the repository; pad rather than raise, so a timeline
        # still lists commits it cannot key a cache by.
        answers += [None] * (len(specs) - len(answers))
        return answers[: len(specs)]

    def _tree_spec(self, rev: str) -> str:
        checked = self._revision_argument(rev)
        return f"{checked}^{{tree}}" if not self.prefix else f"{checked}:{self.prefix}"

    # -- the states ----------------------------------------------------------

    def revision(self, rev: str, *, tree: str | None = None) -> Revision:
        """The inventory as ``rev`` has it, loaded once per distinct tree.

        Never raises for anything about the *revision*: a ref that does not
        resolve, a revision with no inventory folder and a revision whose YAML
        does not load all come back as a :class:`Revision` carrying the reason.
        The caller decides whether that is fatal — for ``diff --from`` it is,
        for one frame of a timeline it is not.
        """
        key = tree if tree is not None else self.tree_of(rev)
        if key is not None:
            cached = self._states.get(key)
            if cached is not None:
                return cached
        loaded = self._load(rev, key)
        return self._states.put(key, loaded) if key is not None else loaded

    def _load(self, rev: str, tree: str | None) -> Revision:
        if tree is None:
            where = f"the {self.prefix!r} directory" if self.prefix else "this repository"
            return Revision(
                rev=rev,
                tree=None,
                missing=True,
                error=f"{where} does not exist at {_short(rev)}; there is no inventory there",
            )
        try:
            with git_ref(self.root, rev) as exported:
                inventory = load_tree(exported, cache=self.cache)
        except MissingInventory as exc:  # pragma: no cover - the tree hash said otherwise
            return Revision(rev=rev, tree=tree, error=str(exc))
        except (PlanSourceError, LoaderError, OSError) as exc:
            return Revision(
                rev=rev, tree=tree, error=f"the inventory at {_short(rev)} cannot be read: {exc}"
            )
        if inventory.errors:
            first = inventory.errors[0]
            more = f" (and {len(inventory.errors) - 1} more)" if len(inventory.errors) > 1 else ""
            return Revision(
                rev=rev,
                tree=tree,
                error=f"the inventory at {_short(rev)} does not load: {first.message}{more}",
            )
        return Revision(rev=rev, tree=tree, inventory=inventory)

    def empty(self, rev: str | None = None) -> Revision:
        """A state with nothing in it: before a root commit, or before the folder.

        Not an error and not a missing revision — a repository's first commit
        genuinely added everything in it, and a frame that said "cannot read the
        parent" would hide the one commit a reader is most likely to want.
        """
        return Revision(rev=rev or _ROOT, tree=None, inventory=Inventory(root=self.root))

    # -- the frames ----------------------------------------------------------

    def trees(self, commit: Commit) -> tuple[str | None, str | None]:
        """``(before, after)`` inventory tree hashes for this commit's frame.

        The cheap half of :meth:`frame`: two object lookups and no load. It is
        what a cache of *drawn* frames is keyed by, so that a frame already
        drawn costs neither an export nor a changeset — and a revert, a
        cherry-pick and a rebase all hit, because none of them changes the pair
        of trees the diff is between.
        """
        parent = commit.parent
        return (self.tree_of(parent) if parent else None, commit.tree)

    def frame(self, commit: Commit, *, renames: bool = True) -> Frame:
        """``commit`` against its first parent, as a changeset.

        A merge is drawn against its first parent, which is what ``git log -p``
        shows and what "the state this landed on" means to a reader.

        A side with **no inventory directory** is read as an empty network
        rather than as a failure, and the frame says which side that was. The
        two are the same fact — nothing was described there — and refusing the
        commit that first added the folder would hide the one frame in which the
        whole network appears. A side whose YAML is *there and broken* is a
        failure, and stays one.
        """
        after = self.revision(commit.hash, tree=commit.tree)
        parent = commit.parent
        before = self.empty(parent) if parent is None else self.revision(parent)
        note = None
        if before.missing:
            before, note = self.empty(before.rev), "the inventory did not exist before this commit"
        if after.missing:
            after, note = self.empty(after.rev), "this commit removed the inventory"
        if not before.ok or not after.ok:
            return Frame(commit=commit, before=before, after=after)
        plan = diff_states(
            before.require(),
            after.require(),
            source=_ref(parent or _ROOT, before),
            target=_ref(commit.hash, after),
            renames=renames,
        )
        return Frame(commit=commit, before=before, after=after, plan=plan, note=note)

    def frames(self, commits: Sequence[Commit], *, renames: bool = True) -> Iterator[Frame]:
        """Every commit's frame, newest first.

        Walked in the order given so the inventory cache does its job: each
        commit's *parent* state is the next commit's *own* state, so a linear
        history costs one load per frame rather than two.
        """
        for commit in commits:
            yield self.frame(commit, renames=renames)

    # -- running git ---------------------------------------------------------

    def _git(self, arguments: list[str], *, failure: str, stdin: bytes | None = None) -> bytes:
        try:
            return git(arguments, cwd=self.repository, failure=failure, stdin=stdin)
        except PlanSourceError as exc:
            raise HistoryError(str(exc)) from exc

    def _revision_argument(self, rev: str) -> str:
        """Check that ``rev`` is a revision and not a git option in disguise.

        Every string that reaches a git argument list from outside goes through
        here. ``netgraph web`` takes revisions from a query string, and
        ``git log --output=<file>`` is a file this server would otherwise
        write on request; see :func:`~netgraph.plan.sources.check_revision`.
        """
        try:
            return check_revision(rev)
        except PlanSourceError as exc:
            raise HistoryError(str(exc)) from exc

    def _pathspec(self) -> list[str]:
        """``-- <prefix>``, which is what makes this the *inventory's* history."""
        return ["--", self.prefix] if self.prefix else []

    def _range(self, since: str | None, until: str) -> list[str]:
        checked = self._revision_argument(until)
        if since is None:
            return [checked]
        return [f"{self._revision_argument(since)}..{checked}"]

    def _range_failure(self, since: str | None, until: str) -> str:
        return (
            f"git cannot read {_range_text(since, until)}; check that the revisions exist "
            f"in this repository"
        )


def _ref(rev: str, revision: Revision) -> StateRef:
    """How a frame's plan describes one of its sides."""
    inventory = revision.inventory
    return StateRef(
        kind="git",
        description=_short(rev),
        digest=state_digest(inventory) if inventory is not None else "",
    )


def _as_kwargs(commit: Commit) -> dict[str, Any]:
    return {
        "hash": commit.hash,
        "parents": commit.parents,
        "author": commit.author,
        "email": commit.email,
        "when": commit.when,
        "subject": commit.subject,
    }


def _parse_log(output: str) -> Iterator[Commit]:
    for record in output.split(_RECORD):
        line = record.strip("\n")
        if not line:
            continue
        parts = line.split(_FIELD)
        if len(parts) != 6:  # pragma: no cover - the format string fixes the count
            continue
        sha, parents, author, email, when, subject = parts
        yield Commit(
            hash=sha,
            parents=tuple(parents.split()),
            author=author,
            email=email,
            when=_when(when),
            subject=subject,
        )


def _when(stamp: str) -> datetime:
    """One ``%aI`` field, whichever way this git spells a UTC offset.

    Most builds write ``+00:00`` and some write ``Z``; both are ISO 8601 and
    both mean the same instant. Before Python 3.11 ``fromisoformat`` accepts
    only the first, so on 3.10 a repository whose git wrote the second made
    every timeline command raise ``ValueError: Invalid isoformat string`` —
    which is a thing to normalise here rather than a thing to hope about.
    """
    return datetime.fromisoformat(f"{stamp[:-1]}+00:00" if stamp.endswith("Z") else stamp)


def _short(rev: str) -> str:
    """A revision as a reader should see it: abbreviated if it is a raw hash."""
    if len(rev) == 40 and all(character in "0123456789abcdef" for character in rev):
        return rev[:_ABBREV]
    return rev


def _range_text(since: str | None, until: str) -> str:
    return f"{_short(since)}..{_short(until)}" if since else _short(until)
