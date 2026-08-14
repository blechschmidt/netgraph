"""One inventory, one sequence of edits, one decision about whether to write.

A session is what a caller actually holds. It owns the loaded inventory, the
file store, the undo stack, and the two gates that stand between an operation
and the disk:

**Validation.** Before anything is written the tree is loaded *as it would be*,
through :class:`~netgraph.loader.Overlay`, and validated. The comparison is
against the same tree loaded as it *is*, per rule and by count — so an inventory
that already has three ``W103`` warnings can still be edited, and one that would
gain a fourth cannot without ``--force``. Absolute cleanliness is not the bar,
because an inventory that fails ``validate`` is exactly when an editor is most
needed; not making it worse is.

**Conflict.** Every file the session read carries the SHA-256 of the bytes it
was read as. Immediately before the write each is checked again, and a file that
moved under the session is a :class:`~netgraph.edit.errors.ConflictError` rather
than a silent overwrite.

Between operations the inventory is reloaded from the overlay, so a batch that
creates a device and then sets a field on it works, and each operation resolves
names against the tree the previous one left behind. That is the expensive part
of a batch, and it is not optional: resolving a name against a stale index is
how an editor writes a reference to something that is no longer there.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from netgraph.config import Config, ValidationConfig
from netgraph.edit.apply import AppliedOperation, apply_operation
from netgraph.edit.errors import Problem, ValidationRefused
from netgraph.edit.operations import Operation
from netgraph.edit.tree import EditableTree
from netgraph.fmt.runner import diff_text
from netgraph.loader import DocumentCache, Inventory, Overlay, load_tree
from netgraph.validate import validate

__all__ = ["EditSession", "EditSummary"]


@dataclass(frozen=True, slots=True)
class EditSummary:
    """What a session did, in the form ``--json`` prints and a caller stores."""

    applied: tuple[AppliedOperation, ...]
    #: Files the session would write, or has written; ``None`` means removed.
    changes: Mapping[str, str | None]
    #: Files actually written. Empty for a dry run.
    written: tuple[str, ...] = ()

    @property
    def inverse(self) -> tuple[Operation, ...]:
        """The whole batch's undo, in the order it must be applied.

        Operations undo in reverse: the last change made is the first put back.
        """
        return tuple(
            operation for applied in reversed(self.applied) for operation in applied.inverse
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "applied": [
                {
                    "operation": applied.operation.to_dict(),
                    "inverse": [inverse.to_dict() for inverse in applied.inverse],
                    "summary": applied.summary,
                    "files": list(applied.files),
                }
                for applied in self.applied
            ],
            "inverse": [operation.to_dict() for operation in self.inverse],
            "files": {
                path: ("deleted" if text is None else "written")
                for path, text in sorted(self.changes.items())
            },
            "written": list(self.written),
        }


@dataclass(eq=False)
class EditSession:
    """An inventory open for editing."""

    root: Path
    #: ``netgraph.toml``'s ``[validation]``, so the gate grades findings the way
    #: ``netgraph validate`` would in this tree.
    config: Config | None = None
    #: The parse cache, shared with the rest of the run, and used on *every*
    #: load this session makes — including the overlaid ones.
    #:
    #: An overlaid file never consults it: :func:`~netgraph.loader.load_tree`
    #: takes the overlay branch before the cache branch, so a file whose bytes
    #: are in memory is parsed from memory. Every *other* file in the tree is
    #: exactly the bytes on disk, and re-parsing all of them to judge an edit to
    #: one of them is what made editing a thousand-device inventory cost a
    #: second per keystroke: a batch loads the tree three times (the baseline,
    #: the tree between operations, and the tree the gate judges), and on the
    #: benchmark tree that was 1.25 s of parsing per edit.
    cache: DocumentCache | None = None
    #: Files an editor is holding open with unsaved changes, keyed by POSIX path
    #: relative to :attr:`root`. They are the text the session reads, and the
    #: text every gate judges against, so an edit computed here is an edit
    #: against what the user is looking at rather than against a stale disk.
    buffers: Mapping[str, str] = field(default_factory=dict)

    _tree: EditableTree = field(init=False, repr=False)
    _inventory: Inventory | None = field(default=None, init=False, repr=False)
    _baseline: Inventory | None = field(default=None, init=False, repr=False)
    _applied: list[AppliedOperation] = field(default_factory=list, init=False, repr=False)
    _overlay: Overlay | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._tree = EditableTree(root=self.root)
        if not self.buffers:
            return
        for relative in sorted(self.buffers):
            self._tree.seed(relative, self.buffers[relative])
        # What the seeded files render as, which is what the tree will compare
        # its changes against -- not the raw buffer, which may differ from it by
        # a trailing newline the round-trip parser normalises.
        self._overlay = Overlay(
            files={relative: self._tree.text_of(relative) for relative in self.buffers}
        )

    # -- the tree --------------------------------------------------------

    @property
    def tree(self) -> EditableTree:
        return self._tree

    @property
    def baseline(self) -> Inventory:
        """The inventory as it is on disk, loaded once and kept for comparison."""
        if self._baseline is None:
            self._baseline = load_tree(self.root, cache=self.cache, overlay=self._overlay)
            self._inventory = self._baseline
        return self._baseline

    @property
    def inventory(self) -> Inventory:
        """The inventory as the operations applied so far have left it."""
        if self._inventory is None:
            self._inventory = (
                self.baseline
                if not self._tree.dirty
                else load_tree(self.root, cache=self.cache, overlay=self._tree.overlay())
            )
        return self._inventory

    # -- editing ---------------------------------------------------------

    def apply(self, operation: Operation) -> AppliedOperation:
        """Apply one operation to the in-memory tree.

        Nothing is written and nothing is validated here: a batch is judged as a
        batch, because an operation that is invalid on its own — a cable created
        before the device it lands on — is a normal thing for the next operation
        to fix.

        Raises:
            EditError: The operation cannot be applied. The tree is unchanged.
        """
        applied = apply_operation(operation, tree=self._tree, inventory=self.inventory)
        self._applied.append(applied)
        # The next operation must resolve names against what this one left.
        self._inventory = None
        return applied

    def apply_all(self, operations: Iterable[Operation]) -> tuple[AppliedOperation, ...]:
        """Apply operations in order, stopping at the first that refuses."""
        return tuple(self.apply(operation) for operation in operations)

    # -- reporting -------------------------------------------------------

    @property
    def applied(self) -> tuple[AppliedOperation, ...]:
        return tuple(self._applied)

    @property
    def changes(self) -> dict[str, str | None]:
        """Every file that would be written, in path order."""
        return self._tree.changes

    def diff(self) -> str:
        """A unified diff of everything the session would write.

        One hunk set per file, in path order, with git's ``a/``/``b/`` prefixes
        so the result is something ``git apply`` accepts.
        """
        return "".join(self._file_diffs())

    def _file_diffs(self) -> Iterator[str]:
        for relative, after in self.changes.items():
            before = self._tree.original_of(relative)
            yield diff_text(before or "", after or "", name=relative)

    def summary(
        self, *, written: Sequence[str] = (), changes: Mapping[str, str | None] | None = None
    ) -> EditSummary:
        """What the session did.

        ``changes`` is for reporting *after* a commit: writing makes the pending
        set empty, and a caller that has already written still has to be able to
        say what it wrote.
        """
        return EditSummary(
            applied=self.applied,
            changes=dict(self.changes if changes is None else changes),
            written=tuple(written),
        )

    # -- the gates -------------------------------------------------------

    def check(self) -> tuple[Problem, ...]:
        """Problems the pending changes would add to the tree.

        Returns:
            The new problems, in the order the loader and the validator report
            them. Empty when the edit makes nothing worse.
        """
        if not self._tree.dirty:
            return ()
        after = load_tree(self.root, cache=self.cache, overlay=self._tree.overlay())
        return _new_problems(self.baseline, after, config=self.config)

    def commit(self, *, force: bool = False) -> tuple[str, ...]:
        """Validate, check for conflicts, and write.

        Args:
            force: Write even if the edit introduces new problems. The conflict
                check is *not* skipped by this: overwriting somebody else's work
                is not a thing a user can mean.

        Returns:
            The files written or removed, in path order.

        Raises:
            ValidationRefused: The edit would introduce a new problem.
            ConflictError: A file changed on disk since it was read.
            EditError: A write failed.
        """
        if not force:
            problems = self.check()
            if problems:
                raise ValidationRefused(
                    f"the edit would introduce {len(problems)} new problem"
                    f"{'' if len(problems) == 1 else 's'}; nothing has been written "
                    f"(use --force to write it anyway)",
                    problems=problems,
                )
        written = self._tree.commit()
        # What was written is now what is on disk, so the next check compares
        # against it rather than against the tree this session opened.
        self._baseline = None
        self._inventory = None
        return written


def _new_problems(
    before: Inventory, after: Inventory, *, config: Config | None
) -> tuple[Problem, ...]:
    """What ``after`` is wrong about that ``before`` was not.

    Counted per rule rather than matched message by message, because a rename
    changes every message that names the element without changing what is wrong
    — and an editor that refused to rename anything in a tree with a pre-existing
    warning would be useless. An increase in the number of errors a rule reports
    is the signal; the messages are then listed so the user can see which ones.
    """
    settings = config.validation if config is not None else None
    old = _problems(before, settings)
    new = _problems(after, settings)
    old_counts = Counter(problem.rule for problem in old)
    new_counts = Counter(problem.rule for problem in new)
    worse = {rule for rule, count in new_counts.items() if count > old_counts[rule]}
    if not worse:
        return ()
    seen = {(problem.rule, problem.message) for problem in old}
    return tuple(
        problem
        for problem in new
        if problem.rule in worse and (problem.rule, problem.message) not in seen
    )


def _problems(inventory: Inventory, settings: ValidationConfig | None) -> tuple[Problem, ...]:
    """Every *error* an inventory has: load errors first, then fatal findings.

    Warnings are deliberately not counted. An edit that adds a warning is an
    edit somebody may well mean to make — a device with no description, a link
    with no label — and a write path that refused them would be one people
    routinely ``--force`` past, which is worse than one that does not ask.
    """
    problems = [
        Problem(rule=error.rule or "load", location=error.location, message=error.message)
        for error in inventory.errors
    ]
    problems.extend(
        Problem(
            rule=finding.rule,
            location="-" if finding.source is None else str(finding.source),
            message=finding.message,
        )
        for finding in validate(inventory, settings)
        if finding.severity.is_fatal
    )
    return tuple(problems)
