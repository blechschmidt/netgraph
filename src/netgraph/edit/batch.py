"""N operations across N documents, applied as one change.

:class:`~netgraph.edit.session.EditSession` is already atomic *per operation*:
an applier that refuses leaves the tree exactly as it found it. That is the
wrong grain for an editor. Selecting eleven switches and pressing Delete is one
gesture, and if the seventh of them cannot go — something still refers to it, a
file moved on disk — the honest outcome is that none of them went. Six deleted
devices and an error message is a state nobody asked for and nobody can undo in
one step.

So a batch adds the missing grain, and it adds it in four places at once:

**One transaction.** The tree is snapshotted before the first operation and put
back if any of them refuses (:meth:`~netgraph.edit.tree.EditableTree.snapshot`).
The failure is re-raised naming the operation that caused it and its position,
because "operation 7 of 11 refused" is a different thing to be told than
"refused".

**One entry in the undo stack.** The inverse of the batch is the inverse of each
operation, in reverse order, which is what :attr:`BatchResult.inverse` is — and
what the web editor pushes as a single ``Ctrl-Z``.

**One conflict check, one save.** ``commit`` is the session's, so the whole
batch is validated once against the tree it would produce, hashed once against
the disk, and written once.

**One label.** :func:`describe` names the batch the way a log should: what the
first operation did, and how many more there were.

The unit above this one is a *gesture* — the web session's journal entry — and
the unit below is an operation. A batch is the middle: several typed operations
that mean one thing to the person who asked for them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from netgraph.edit.apply import AppliedOperation
from netgraph.edit.errors import EditError
from netgraph.edit.operations import Operation
from netgraph.edit.session import EditSession, EditSummary

__all__ = ["Batch", "BatchResult", "describe"]


def describe(operations: Sequence[Operation]) -> str:
    """One line naming a batch: the first operation, and how many followed.

    The first rather than a summary of all of them, because a batch is built by
    a gesture and the gesture's own subject is what the first operation names —
    "delete sites/hq/sw-a (+10 more)" is what somebody scanning an undo stack is
    looking for.
    """
    if not operations:
        return "no operations"
    label = operations[0].describe()
    if len(operations) > 1:
        label += f" (+{len(operations) - 1} more)"
    return label


@dataclass(frozen=True, slots=True)
class BatchResult:
    """What one batch did, and how to undo the whole of it."""

    #: The operations as they were asked for, in order.
    operations: tuple[Operation, ...]
    #: What each of them did, in the same order.
    applied: tuple[AppliedOperation, ...]
    #: One line naming the batch; see :func:`describe`.
    label: str
    #: Files the batch would write, or has written; ``None`` means removed.
    changes: Mapping[str, str | None]
    #: Files actually written. Empty until :meth:`Batch.commit` has run.
    written: tuple[str, ...] = ()

    @property
    def inverse(self) -> tuple[Operation, ...]:
        """The whole batch's undo, in the order it must be applied."""
        return tuple(
            operation for applied in reversed(self.applied) for operation in applied.inverse
        )

    @property
    def files(self) -> tuple[str, ...]:
        """Every file the batch touched, in path order."""
        return tuple(sorted(self.changes))

    def summary(self) -> EditSummary:
        """The same result in the shape ``--json`` prints."""
        return EditSummary(applied=self.applied, changes=dict(self.changes), written=self.written)


@dataclass(eq=False)
class Batch:
    """A set of operations that stand or fall together.

    Held rather than applied straight away so that a caller can build the list
    from a selection, add to it, and only then decide whether to write::

        batch = Batch(session, label="delete 11 elements")
        batch.add(DeleteElement(address=one, cascade=True) for one in doomed)
        result = batch.apply()          # all of them, or none of them
        batch.commit()                  # one validation, one hash check, one write
    """

    session: EditSession
    #: One line naming the batch. Derived from the operations when left empty.
    label: str = ""
    _operations: list[Operation] = field(default_factory=list, init=False, repr=False)
    _result: BatchResult | None = field(default=None, init=False, repr=False)

    # -- building --------------------------------------------------------

    def add(self, operations: Operation | Iterable[Operation]) -> Batch:
        """Queue one operation or several. Nothing is applied yet."""
        if isinstance(operations, Operation):
            self._operations.append(operations)
        else:
            self._operations.extend(operations)
        return self

    @property
    def operations(self) -> tuple[Operation, ...]:
        return tuple(self._operations)

    def __len__(self) -> int:
        return len(self._operations)

    def __bool__(self) -> bool:
        return bool(self._operations)

    # -- applying --------------------------------------------------------

    def apply(self, operations: Iterable[Operation] | None = None) -> BatchResult:
        """Apply every queued operation, or put the tree back as it was.

        Args:
            operations: Queued first, for the common case of building and
                applying in one call.

        Returns:
            What the batch did. Nothing is on disk yet; see :meth:`commit`.

        Raises:
            EditError: One operation refused. The session is exactly as it was
                before the batch started — no file changed, no operation
                recorded — and the message names which of them refused.
        """
        if operations is not None:
            self.add(operations)
        if not self._operations:
            raise EditError("a batch needs at least one operation")
        pending = tuple(self._operations)
        mark = self.session.begin()
        applied: list[AppliedOperation] = []
        for position, operation in enumerate(pending, start=1):
            try:
                applied.append(self.session.apply(operation))
            except EditError as exc:
                self.session.rollback(mark)
                if len(pending) == 1:
                    # Nothing to add: the caller asked for one thing and it was
                    # refused, and re-wrapping would only hide the exception's
                    # own type from a caller that knows what to do with it.
                    raise
                raise EditError(
                    f"operation {position} of {len(pending)} "
                    f"({operation.describe()}) was refused: {exc}. "
                    f"Nothing in this change was applied"
                ) from exc
        self._result = BatchResult(
            operations=pending,
            applied=tuple(applied),
            label=self.label or describe(pending),
            changes=dict(self.session.changes),
        )
        return self._result

    def commit(self, *, force: bool = False) -> BatchResult:
        """Validate the batch, check every file for conflicts, and write.

        One of each: the tree the batch would produce is loaded and graded
        once, every file it touches is hashed once, and the whole set is
        written together.

        Raises:
            EditError: The batch has not been applied.
            ValidationRefused: The batch would introduce a new problem.
            ConflictError: A file changed on disk since it was read.
        """
        if self._result is None:
            raise EditError("this batch has not been applied yet")
        written = self.session.commit(force=force)
        self._result = BatchResult(
            operations=self._result.operations,
            applied=self._result.applied,
            label=self._result.label,
            changes=self._result.changes,
            written=written,
        )
        return self._result

    @property
    def result(self) -> BatchResult | None:
        """What the batch did, once it has been applied."""
        return self._result
