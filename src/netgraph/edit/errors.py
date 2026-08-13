"""What an edit refuses to do, and why.

Every failure mode of the write path has a type here rather than a string,
because every one of them is something a caller has to *handle* rather than
merely print. The web editor has to turn a conflict into "reload, your file
moved under you"; a scripted apply has to turn a cascade refusal into a prompt;
``netgraph edit`` has to map each one onto an exit status. A single
``EditError("...")`` would make all of that string matching.

The hierarchy is flat on purpose: one base class the CLI catches, and one
subclass per decision, each carrying the data the caller needs to explain
itself. Nothing here is raised for a problem *in* the inventory — a document
that does not parse or an element that does not validate is reported through
the loader's own :class:`~netgraph.loader.LoadError` and the validator's
:class:`~netgraph.validate.Finding`, which is what :class:`ValidationRefused`
carries.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "AddressError",
    "CascadeRequired",
    "ConflictError",
    "EditError",
    "OperationError",
    "PlacementError",
    "Problem",
    "RoundTripError",
    "ValidationRefused",
]


class EditError(Exception):
    """Base of every refusal the edit layer makes.

    Carries nothing but its message, so ``except EditError`` in a front end is
    enough to turn any of them into a diagnostic; the subclasses add the fields
    a front end that wants to do more than print needs.
    """


class OperationError(EditError):
    """The operation itself is malformed: an unknown kind, a bad field path.

    Raised while *building* an operation, before any file is looked at, so it
    never leaves a tree half-edited.
    """


class AddressError(EditError):
    """The address names no element, or names more than one.

    Attributes:
        address: The address as it was written.
        candidates: Every fully-qualified name it could have meant, when the
            problem is ambiguity rather than absence.
    """

    def __init__(self, message: str, *, address: str, candidates: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.address = address
        self.candidates = tuple(candidates)


class CascadeRequired(EditError):
    """The element is referred to by others, and ``cascade`` was not asked for.

    This is the refusal that keeps a delete from silently breaking a tree. The
    dependents are listed so the caller can print them, offer them, or re-run
    with ``--cascade``.

    Attributes:
        address: The element that was to be removed.
        dependents: Fully-qualified names of the elements that refer to it, in
            load order.
    """

    def __init__(self, message: str, *, address: str, dependents: Sequence[str]) -> None:
        super().__init__(message)
        self.address = address
        self.dependents = tuple(dependents)


class ConflictError(EditError):
    """A file changed on disk between being read and being written.

    The edit layer hashes every file it is going to touch when it reads it and
    checks the hash again immediately before the write. Somebody else's editor,
    a ``git checkout`` or a second netgraph process landing in between is
    therefore reported rather than overwritten.

    Attributes:
        path: The file, relative to the inventory root, POSIX style.
        expected: The digest the session read.
        actual: The digest the file has now, or ``None`` when it is gone.
    """

    def __init__(
        self, message: str, *, path: str, expected: str | None, actual: str | None
    ) -> None:
        super().__init__(message)
        self.path = path
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing that would be wrong with the tree after the edit."""

    #: Rule id (``E002``) for a semantic finding, ``load`` for a load error.
    rule: str
    #: Where it is, in ``sites/hq/sw.yaml#0:17`` notation, or ``-``.
    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.rule}: {self.message}"


class ValidationRefused(EditError):
    """The edit would introduce a problem the tree does not already have.

    Only *new* problems count. An inventory that already fails ``validate`` can
    still be edited — refusing otherwise would make the tool useless exactly
    when it is most needed — so the gate compares the count per rule before and
    after and objects only to an increase.

    Attributes:
        problems: The new problems, in the order the validator reported them.
    """

    def __init__(self, message: str, *, problems: Sequence[Problem] = ()) -> None:
        super().__init__(message)
        self.problems = tuple(problems)


class PlacementError(EditError):
    """A new element cannot be put anywhere sensible.

    Raised when an explicit ``file`` sits outside the inventory, names a
    directory that contradicts the namespace asked for, or is not a name the
    loader would read at all.
    """


class RoundTripError(EditError):
    """A file cannot be edited losslessly, so it is not edited at all.

    The one case that reaches this in practice is a document introduced by an
    inline ``--- key: value`` marker, which cannot be re-emitted without moving
    the first key onto its own line. ``netgraph fmt`` fixes it; until then the
    file is readable and renderable but not editable, which is the safe half of
    the trade.
    """
