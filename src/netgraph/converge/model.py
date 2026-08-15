"""What a convergence plan *is*: the type a transport would one day execute.

``netgraph drift`` says how the live network differs from the inventory.
``netgraph export config`` says what a device would run if it agreed. This
module holds the join: the ordered, per-device, per-change record of how to get
from the first to the second.

The shape is deliberate, because it is a contract with something that does not
exist yet. netgraph opens no SSH session and never will from this command (see
``docs/commands/converge.md``); it writes a plan and a script. But a transport
*could* be built on top, and if it is, it needs more than a list of strings:

:class:`ConvergeChange`
    One thing to do, with its :class:`Provenance` — which inventory element and
    which drift finding asked for it — its :class:`Risk`, its
    :attr:`~ConvergeChange.prerequisites` (the ids of changes that must land
    first), the :class:`Command` list that performs it and the one that undoes
    it. A transport can therefore refuse a single change, retry it, or roll it
    back, without re-deriving anything.

:class:`DeviceConverge`
    Every change for one element, already ordered.

:class:`Batch`
    A set of elements that may be worked at the same time, with the blast
    radius that entails, from the :mod:`netgraph.impact` engine. A transport
    running a maintenance window walks batches, not devices.

:class:`ConvergePlan`
    All of it, plus what the run was: which capture, which dialect, whether
    disruptive changes were allowed.

Every collection is a tuple and every type is frozen, so a plan can be handed
around, cached, serialised and compared. Ordering is total and derived from the
data (never from a dict's insertion order), so two runs over an unchanged
inventory and an unchanged capture produce byte-identical output in every
format.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from netgraph import __version__
from netgraph.errors import NetgraphError
from netgraph.fsio import display_path

__all__ = [
    "Action",
    "Batch",
    "Command",
    "ConvergeChange",
    "ConvergeError",
    "ConvergePlan",
    "DeviceConverge",
    "DisruptiveChangeError",
    "Provenance",
    "Risk",
]


class Risk(str, Enum):
    """How much a change can cost if it is wrong.

    Two values, not five. An operator reviewing a script at two in the morning
    needs to know one thing — *can this lock me out or take something down* —
    and a scale with a middle would invite the answer "probably fine".
    """

    #: Nothing on the path the operator reaches the device by, and nothing that
    #: takes an interface down.
    SAFE = "safe"
    #: Touches the management path, or shuts an interface that carries traffic.
    #: Refused unless ``--allow-disruptive``.
    DISRUPTIVE = "disruptive"

    def __str__(self) -> str:
        return self.value


class Action(str, Enum):
    """What a change does to the device, in the coarsest terms that are useful."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    #: Nothing netgraph can write a command for: a cable that must be moved, a
    #: device that is not in the inventory at all. Carried in the plan rather
    #: than dropped, because "the plan is empty" and "the plan is empty and
    #: three cables are in the wrong ports" are different states of the world.
    MANUAL = "manual"

    def __str__(self) -> str:
        return self.value


#: The marker each action prints with, chosen to read like the drift report it
#: came from: ``+`` creates, ``-`` deletes, ``~`` changes, ``!`` needs hands.
ACTION_SYMBOLS: Final[dict[Action, str]] = {
    Action.CREATE: "+",
    Action.DELETE: "-",
    Action.UPDATE: "~",
    Action.MANUAL: "!",
}


@dataclass(frozen=True, slots=True)
class Provenance:
    """Why a change is in the plan: the element, and the drift finding.

    Every change has at least one. A change with no provenance would be
    netgraph's own opinion about a network, and this command does not have one:
    it only ever proposes what closes a difference somebody can go and read in
    ``netgraph drift`` output.
    """

    #: Fully-qualified name of the inventory element.
    element: str
    #: Its ``kind``, or ``None`` when the capture saw something undeclared.
    kind: str | None = None
    #: Where inside the element, as the inventory spells it: ``eno1``.
    path: str = ""
    #: The field that differed, when the finding was about one.
    field: str | None = None
    #: The drift :class:`~netgraph.drift.model.Direction` value.
    direction: str = ""
    #: The inventory's value, as the drift report rendered it.
    declared: str | None = None
    #: The capture's value, likewise.
    observed: str | None = None
    #: The drift finding's own sentence, verbatim.
    message: str = ""

    @property
    def location(self) -> str:
        """``hosts/pc-desk:eno1.mac`` — where the finding pointed."""
        tail = ".".join(part for part in (self.path, self.field) if part)
        return f"{self.element}:{tail}" if tail else self.element

    def as_record(self) -> dict[str, Any]:
        return {
            "element": self.element,
            "kind": self.kind,
            "path": self.path or None,
            "field": self.field,
            "direction": self.direction,
            "declared": self.declared,
            "observed": self.observed,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class Command:
    """One step of a script: a line to run, or a file to put in place.

    A declarative dialect (netplan, systemd-networkd, ifupdown, wg-quick) has no
    minimal *command* for "give this interface an MTU of 1500" — its minimal
    remediation is genuinely "make the file say this, then reload". So a command
    is one of two things rather than always a line, and which one it is is
    stated rather than left to be guessed from the text.
    """

    #: What the step does, as a shell line for ``exec`` and as a one-line
    #: summary for ``write``. Always safe to print on its own.
    text: str
    #: ``exec`` or ``write``.
    kind: str = "exec"
    #: Absolute path on the *device*, for ``write``.
    path: str | None = None
    #: The whole file, newline-terminated, for ``write``.
    content: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("exec", "write"):
            raise ValueError(f"not a command kind: {self.kind!r}")
        if (self.kind == "write") != (self.content is not None):
            raise ValueError("a 'write' command carries content and an 'exec' command does not")
        if (self.kind == "write") != (self.path is not None):
            raise ValueError("a 'write' command carries a path and an 'exec' command does not")

    def script_lines(self) -> Iterator[str]:
        """The step as the lines a ``.txt`` script holds.

        A write becomes a quoted here-document. The delimiter is quoted
        (``<<'NETGRAPH_EOF'``) so the shell performs no substitution on the
        body: a generated configuration may legitimately contain ``$``, and a
        WireGuard placeholder key certainly contains characters that are not a
        shell's business.
        """
        if self.kind == "exec":
            yield self.text
            return
        assert self.path is not None and self.content is not None
        parent = self.path.rsplit("/", 1)[0]
        if parent:
            yield f"install -d -m 0755 {parent}"
        yield f"cat > {self.path} <<'{_HEREDOC}'"
        yield from self.content.splitlines()
        yield _HEREDOC

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"kind": self.kind, "text": self.text}
        if self.kind == "write":
            record["path"] = self.path
            record["content"] = self.content
        return record


#: Here-document delimiter for a generated file. Long and namespaced because a
#: configuration file is arbitrary text and a short delimiter could occur in it.
_HEREDOC: Final = "NETGRAPH_EOF"


@dataclass(frozen=True, slots=True)
class ConvergeChange:
    """One thing to do to one device, and everything needed to decide about it."""

    #: Stable within a plan, and stable between runs over unchanged inputs:
    #: ``hosts/pc-desk:eno1.mac/update``. A transport records it to say what it
    #: has already applied.
    id: str
    #: Fully-qualified name of the element the change is made on.
    element: str
    action: Action
    #: What is being changed: ``interface``, ``address``, ``vlan``, ``member``,
    #: ``field``, ``file``, ``link`` or ``device``.
    object: str
    #: Which one, in the device's own words: ``eno1``, ``eno1.ipv4``, ``20``.
    target: str
    #: One sentence, imperative, naming what will happen.
    summary: str
    #: The interface it is on, or ``""`` for a device-wide change and for a file.
    #: Carried explicitly because :attr:`id` is opaque: a target may be an
    #: address, and an address holds a ``/``.
    interface: str = ""
    #: The value the change sets, where it sets one -- the MTU, the VLAN mode,
    #: the address family. Carried on the change rather than only inside the
    #: command text so a consumer never has to parse a command back apart.
    value: str | None = None
    #: What the capture reported in its place, likewise. This is the value the
    #: rollback restores, and it is ``None`` when the capture reported nothing.
    previous: str | None = None
    #: Position in the dependency order. Lower runs first; see
    #: :mod:`netgraph.converge.intent`.
    rank: int = 0
    risk: Risk = Risk.SAFE
    #: Why the risk is what it is. Always set for a disruptive change, so a
    #: refusal can quote a reason rather than a category.
    risk_reason: str = ""
    #: The drift findings that asked for this change. At least one.
    provenance: tuple[Provenance, ...] = ()
    #: Ids of changes on the same device that have to land first.
    prerequisites: tuple[str, ...] = ()
    #: What to run, in order.
    commands: tuple[Command, ...] = ()
    #: What to run to put the device back the way the capture found it.
    rollback: tuple[Command, ...] = ()
    #: For a manual change: what a person has to go and do.
    note: str = ""

    @property
    def symbol(self) -> str:
        return ACTION_SYMBOLS[self.action]

    @property
    def actionable(self) -> bool:
        """Did netgraph produce commands for this?"""
        return self.action is not Action.MANUAL

    @property
    def order(self) -> tuple[int, str, str, str]:
        """Sort key: the dependency rank, then the target, then the id."""
        return (self.rank, self.object, self.target, self.id)

    def as_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "element": self.element,
            "action": str(self.action),
            "object": self.object,
            "interface": self.interface or None,
            "target": self.target,
            "summary": self.summary,
            "value": self.value,
            "previous": self.previous,
            "rank": self.rank,
            "risk": str(self.risk),
            "risk_reason": self.risk_reason or None,
            "note": self.note or None,
            "prerequisites": list(self.prerequisites),
            "provenance": [entry.as_record() for entry in self.provenance],
            "commands": [command.as_record() for command in self.commands],
            "rollback": [command.as_record() for command in self.rollback],
        }


@dataclass(frozen=True, slots=True)
class DeviceConverge:
    """Every change for one element, in the order they must be applied."""

    #: Fully-qualified name, or the capture's name for something undeclared.
    element: str
    kind: str | None = None
    changes: tuple[ConvergeChange, ...] = ()
    #: Index into :attr:`ConvergePlan.batches`, or ``None`` when the element is
    #: not a device the impact engine knows (a cable, an undeclared box).
    batch: int | None = None

    @property
    def risk(self) -> Risk:
        """The worst risk any change on this element carries."""
        return (
            Risk.DISRUPTIVE
            if any(change.risk is Risk.DISRUPTIVE for change in self.changes)
            else Risk.SAFE
        )

    @property
    def actionable(self) -> tuple[ConvergeChange, ...]:
        return tuple(change for change in self.changes if change.actionable)

    @property
    def manual(self) -> tuple[ConvergeChange, ...]:
        return tuple(change for change in self.changes if not change.actionable)

    @property
    def commands(self) -> tuple[Command, ...]:
        """Every command, in plan order, flattened."""
        return tuple(command for change in self.changes for command in change.commands)

    def as_record(self) -> dict[str, Any]:
        return {
            "element": self.element,
            "kind": self.kind,
            "risk": str(self.risk),
            "batch": self.batch,
            "changes": [change.as_record() for change in self.changes],
        }


@dataclass(frozen=True, slots=True)
class Batch:
    """Elements that may be worked in one maintenance window, and what that costs.

    Two devices share a batch when neither is in the other's blast radius and
    their blast radii do not overlap: taking both out at once is then no worse
    than taking either out on its own. Anything that would compound goes in a
    later batch, so walking the batches in order is a maintenance schedule.
    """

    index: int
    #: Fully-qualified names, sorted.
    elements: tuple[str, ...] = ()
    #: What loses reachability while this batch is being worked, from
    #: :func:`netgraph.impact.simulate`. Sorted, deduplicated.
    isolated: tuple[str, ...] = ()
    #: Namespaces the batch partitions, as ``sites/north: 1 -> 2``.
    splits: tuple[str, ...] = ()
    #: Why the batch is shaped this way, when it is not obvious.
    note: str = ""

    @property
    def disruptive(self) -> bool:
        return bool(self.isolated or self.splits)

    def as_record(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "elements": list(self.elements),
            "isolated": list(self.isolated),
            "splits": list(self.splits),
            "note": self.note or None,
        }


@dataclass(frozen=True, slots=True)
class ConvergePlan:
    """The whole answer: what to change, on what, in what order, at what cost."""

    #: Inventory root the declared side was loaded from.
    root: Path
    #: Capture inputs, in command-line order.
    inputs: tuple[str, ...] = ()
    #: Dialects the capture was read as, sorted.
    capture_dialects: tuple[str, ...] = ()
    #: The configuration dialect the commands are written in.
    dialect: str = "interfaces"
    #: One per element with something to do, sorted by name.
    devices: tuple[DeviceConverge, ...] = ()
    #: Maintenance batches, in the order they should be worked.
    batches: tuple[Batch, ...] = ()
    #: Was ``--allow-disruptive`` given?
    allow_disruptive: bool = False
    #: Anything the run wants the reader to know: a capture that saw nothing, a
    #: device the impact engine could not place.
    notes: tuple[str, ...] = ()
    version: str = __version__

    @property
    def changes(self) -> tuple[ConvergeChange, ...]:
        """Every change, device order then plan order."""
        return tuple(change for device in self.devices for change in device.changes)

    @property
    def converged(self) -> bool:
        """Is there nothing to do?

        True only when no element has a change of any kind — including the
        manual ones. A plan that is empty except for three cables in the wrong
        ports is not a converged network, and an exit code that said so would
        be the one thing this command must not get wrong.
        """
        return not self.changes

    @property
    def disruptive(self) -> tuple[ConvergeChange, ...]:
        """Every change that touches a management path or shuts an interface."""
        return tuple(change for change in self.changes if change.risk is Risk.DISRUPTIVE)

    @property
    def counts(self) -> dict[Action, int]:
        """How many changes of each action, every action present."""
        counts = dict.fromkeys(Action, 0)
        for change in self.changes:
            counts[change.action] += 1
        return counts

    def device(self, element: str) -> DeviceConverge | None:
        for entry in self.devices:
            if entry.element == element:
                return entry
        return None

    def as_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "root": display_path(self.root),
            "inputs": list(self.inputs),
            "captureDialects": list(self.capture_dialects),
            "dialect": self.dialect,
            "allowDisruptive": self.allow_disruptive,
            "converged": self.converged,
            "counts": {str(action): count for action, count in self.counts.items()},
            "devices": [device.as_record() for device in self.devices],
            "batches": [batch.as_record() for batch in self.batches],
            "notes": list(self.notes),
        }


class ConvergeError(NetgraphError):
    """The plan could not be built. Shares its status with a validation failure."""

    exit_code = 4


class DisruptiveChangeError(ConvergeError):
    """The plan would cut management reachability, and nobody said that was fine.

    Carries every disruptive change rather than the first: an operator deciding
    whether to pass ``--allow-disruptive`` is deciding about the whole set, and
    finding out about the second device only after re-running is exactly the
    failure mode :class:`~netgraph.export.config.model.UnsupportedConfigError`
    already avoids.
    """

    def __init__(self, changes: Sequence[ConvergeChange]) -> None:
        self.changes = tuple(sorted(changes, key=lambda change: (change.element, change.order)))
        super().__init__(self._message())

    def _message(self) -> str:
        elements = sorted({change.element for change in self.changes})
        head = (
            f"refusing to emit a plan: {len(self.changes)} change(s) on "
            f"{len(elements)} device(s) would touch the path the device is managed on. "
            "Nothing was written. Re-run with --allow-disruptive once you have a way back "
            "in -- console, out-of-band, or somebody standing next to the rack"
        )
        lines = [
            f"  {change.element}: {change.summary} -- {change.risk_reason}"
            for change in self.changes
        ]
        return "\n".join([head, *lines])
