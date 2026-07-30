"""What a comparison produces: differences, blind spots, and the report holding both.

Three types, and the discipline is that nothing crosses between them.

:class:`Change`
    A *difference*: something both sides could have agreed about and did not.
    Every change is one of three :class:`Direction` values — the network has it
    and the inventory does not, the inventory declares it and the network lacks
    it, or both have it and the values differ.

:class:`Unobserved`
    A *blind spot*: something the inventory declares that the capture is
    constitutionally unable to confirm or deny. It carries the reason, because
    "unobserved" without one is indistinguishable from a bug.

:class:`DriftReport`
    Both lists, plus what the run was: which inputs, which dialects, which
    elements were compared. :attr:`DriftReport.drifted` is true when there is at
    least one :class:`Change`; blind spots never make it true, which is the
    guarantee the whole command rests on.

Ordering is total and deterministic (:attr:`Change.order`), so two runs over an
unchanged inventory and an unchanged capture produce byte-identical output in
every format and a report can be committed and diffed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Final

from netgraph import __version__

__all__ = [
    "DIRECTION_SYMBOLS",
    "Change",
    "Direction",
    "DriftReport",
    "ElementDrift",
    "Unobserved",
]


class Direction(str, Enum):
    """Which side of the comparison has the thing the other does not."""

    #: The network has it; the inventory does not declare it.
    UNDECLARED = "undeclared"
    #: The inventory declares it; the network does not have it.
    MISSING = "missing"
    #: Both have it, and they disagree.
    DISAGREES = "disagrees"

    def __str__(self) -> str:
        return self.value


#: The marker each direction is printed with. Chosen to read like a diff, which
#: is what the report is: ``+`` for what the capture added, ``-`` for what it
#: lacks, ``~`` for what it changed.
DIRECTION_SYMBOLS: Final[dict[Direction, str]] = {
    Direction.UNDECLARED: "+",
    Direction.MISSING: "-",
    Direction.DISAGREES: "~",
}

#: Sort weight per direction, so one element's differences read
#: added-removed-changed rather than in discovery order.
_DIRECTION_ORDER: Final[dict[Direction, int]] = {
    Direction.UNDECLARED: 0,
    Direction.MISSING: 1,
    Direction.DISAGREES: 2,
}


@dataclass(frozen=True, slots=True)
class Change:
    """One difference between the declared inventory and the captured network."""

    direction: Direction
    #: What kind of thing differs: ``device``, ``interface``, ``address``,
    #: ``vlan``, ``member``, ``link`` or ``field``.
    scope: str
    #: Fully-qualified name of the declared element, or the observed name when
    #: the network has an element the inventory does not.
    element: str
    #: ``kind`` of the element — ``switch``, ``cable``, … — or ``None`` when the
    #: element is undeclared and nothing says what it is.
    kind: str | None = None
    #: Where inside the element, as the inventory spells it: ``port5.vlan``,
    #: ``eno1.ipv4``. Empty for the element itself.
    path: str = ""
    #: The field that differs, when the scope is a field of something.
    field: str | None = None
    #: The inventory's value, rendered for display.
    declared: str | None = None
    #: The capture's value, rendered for display.
    observed: str | None = None
    #: One sentence naming what differs and how it was seen.
    message: str = ""

    @property
    def location(self) -> str:
        """``sw-home:port5.vlan.trunk_vlans`` — the element and the path in it."""
        tail = ".".join(part for part in (self.path, self.field) if part)
        return f"{self.element}:{tail}" if tail else self.element

    @property
    def order(self) -> tuple[str, str, str, int, str]:
        """Sort key: element, then path, then field, then direction."""
        return (
            self.element,
            self.path,
            self.field or "",
            _DIRECTION_ORDER[self.direction],
            self.message,
        )

    def as_record(self) -> dict[str, Any]:
        """This change as one entry of the JSON ``drift`` array."""
        return {
            "direction": str(self.direction),
            "scope": self.scope,
            "element": self.element,
            "kind": self.kind,
            "path": self.path or None,
            "field": self.field,
            "declared": self.declared,
            "observed": self.observed,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class Unobserved:
    """Something declared that no input could confirm or deny.

    Never a difference, never counted as drift, always carrying the reason it
    could not be checked — so a report that is mostly blind spots reads as
    "capture more" rather than as "your inventory is wrong".
    """

    #: Fully-qualified name of the declared element.
    element: str
    kind: str | None = None
    #: ``device``, ``interface``, ``field``, ``address``, ``vlan`` or ``link``.
    scope: str = "field"
    #: Where inside the element; empty for the element itself.
    path: str = ""
    #: The names the reason applies to — field names, interface names, VLAN ids.
    items: tuple[str, ...] = ()
    #: Why no input could see them.
    reason: str = ""

    @property
    def location(self) -> str:
        return f"{self.element}:{self.path}" if self.path else self.element

    @property
    def order(self) -> tuple[str, str, str, str]:
        return (self.element, self.path, self.scope, self.reason)

    def as_record(self) -> dict[str, Any]:
        return {
            "element": self.element,
            "kind": self.kind,
            "scope": self.scope,
            "path": self.path or None,
            "items": list(self.items),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ElementDrift:
    """Everything the comparison said about one element.

    The report is built as two flat lists because that is what sorts and
    serialises cleanly; a reader wants it grouped, and so does a JUnit test
    case, so the grouping is derived once here rather than in each renderer.
    """

    element: str
    kind: str | None
    changes: tuple[Change, ...] = ()
    unobserved: tuple[Unobserved, ...] = ()

    @property
    def drifted(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True, slots=True)
class DriftReport:
    """The outcome of comparing one inventory with one capture."""

    #: Inventory root the declared side was loaded from.
    root: Path
    #: Input names, in command-line order.
    inputs: tuple[str, ...] = ()
    #: Dialects the inputs were read as, sorted and deduplicated.
    dialects: tuple[str, ...] = ()
    #: Sorted by :attr:`Change.order`.
    changes: tuple[Change, ...] = ()
    #: Sorted by :attr:`Unobserved.order`.
    unobserved: tuple[Unobserved, ...] = ()
    #: Fully-qualified names of the elements that were compared on both sides.
    compared: tuple[str, ...] = ()
    #: Observed device names, in the order the capture yielded them.
    observed: tuple[str, ...] = ()
    #: Elements the ``--only``/``--exclude`` filters left out.
    filtered: tuple[str, ...] = field(default=())
    version: str = __version__

    @property
    def drifted(self) -> bool:
        """Does the network disagree with the inventory?

        Blind spots are excluded on purpose: an unobserved field is not a
        disagreement, and a command that failed a build over one would be a
        command nobody could run against a partial capture.
        """
        return bool(self.changes)

    @property
    def counts(self) -> dict[Direction, int]:
        """How many changes of each direction, every direction present."""
        counts = dict.fromkeys(Direction, 0)
        for change in self.changes:
            counts[change.direction] += 1
        return counts

    @property
    def elements(self) -> tuple[ElementDrift, ...]:
        """Changes and blind spots grouped by element, in element order.

        Elements that drifted come first — they are what a reader opened the
        report for — and each group is otherwise sorted by name.
        """
        kinds: dict[str, str | None] = {}
        changes: dict[str, list[Change]] = {}
        unobserved: dict[str, list[Unobserved]] = {}
        for change in self.changes:
            kinds.setdefault(change.element, change.kind)
            changes.setdefault(change.element, []).append(change)
        for blind in self.unobserved:
            kinds.setdefault(blind.element, blind.kind)
            unobserved.setdefault(blind.element, []).append(blind)
        groups = [
            ElementDrift(
                element=name,
                kind=kinds[name],
                changes=tuple(changes.get(name, ())),
                unobserved=tuple(unobserved.get(name, ())),
            )
            for name in sorted(kinds)
        ]
        return tuple(sorted(groups, key=lambda group: (not group.drifted, group.element)))
