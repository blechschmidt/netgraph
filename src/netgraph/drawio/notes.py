"""What netgraph wants to say about a diagram, in either direction.

Both halves of the round trip have things to report that are neither an error
nor part of the artefact: a node the arrangement had no position for, a cell a
draw.io user added that netgraph could not place, an element that changed under
the diagram while it was out for review. One type carries all of them, so the
export's manifest and the import's report are the same shape and a reader who
has met one has met the other.

A note carries a machine-readable :attr:`Note.reason` beside the sentence. That
is what lets ``netgraph export drawio`` fold its notes into the ordinary export
manifest (:mod:`netgraph.export.manifest`) without this package having to import
it — which it must not, because the export package imports *this* one, and a
wire format has no business knowing about the artefact registry that uses it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ["Level", "Note", "Notes"]


class Level(str, Enum):
    """How much a note matters."""

    #: Something netgraph did, worth saying out loud.
    INFO = "info"
    #: Something in the diagram netgraph could not map, and did not guess at.
    WARNING = "warning"
    #: Something that stops the import.
    ERROR = "error"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Note:
    """One thing netgraph wants the operator to know about a diagram."""

    level: Level
    #: The cell, element or link it is about, in whatever spelling the reader
    #: will recognise: a cell id for something netgraph cannot otherwise name,
    #: and the empty string for something about the diagram as a whole.
    subject: str
    message: str
    #: The :class:`~netgraph.export.manifest.Reason` token this maps onto, for
    #: a note the export folds into its manifest. Empty for a note that is only
    #: ever printed.
    reason: str = ""

    def __str__(self) -> str:
        return f"{self.subject}: {self.message}" if self.subject else self.message

    def to_dict(self) -> dict[str, str]:
        record = {"level": self.level.value, "subject": self.subject, "message": self.message}
        if self.reason:
            record["reason"] = self.reason
        return record


@dataclass(slots=True)
class Notes:
    """An accumulator, so a builder can report without returning a pair."""

    entries: list[Note] = field(default_factory=list)

    def add(self, level: Level, subject: str, message: str, *, reason: str = "") -> None:
        self.entries.append(Note(level=level, subject=subject, message=message, reason=reason))

    def sealed(self) -> tuple[Note, ...]:
        return tuple(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> object:
        return iter(self.entries)
