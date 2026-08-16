"""What a changeset *is*: four actions, an address each, and field-level detail.

The types here are the whole contract between the diff engine, the two commands
and the file a plan is stored in. They hold no behaviour beyond serialisation on
purpose — :mod:`netviz.plan.diff` decides what a change is, :mod:`netviz.plan.order`
decides what order they go in, :mod:`netviz.plan.execute` decides how to make
them, and none of those three needs to be re-read to understand the others.

Four actions, and why there are exactly four:

``create``
    An element in the target state and in no other. Carries the whole document
    body, because there is no before to diff against.
``update``
    An element on both sides whose fields differ. Carries one
    :class:`FieldChange` per field, never a whole document: "the MTU went from
    1500 to 9000" is a reviewable statement and "here are two 40-line YAML
    documents, spot the difference" is not.
``delete``
    An element in the source state and in no other. Carries its body too, so
    that a plan read back a week later still says what is about to be lost.
``rename``
    One element, two names. Detected structurally (:mod:`netviz.plan.identity`)
    rather than reported as a delete plus a create, which is the difference
    between a plan a reviewer can read and one they cannot. A rename carries no
    fields: when the element also changed, the changeset holds a separate
    ``update`` at the new address, so the two decisions stay separable.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from netviz import __version__
from netviz.plan.address import Address, parse_address
from netviz.plan.paths import MISSING, Step, format_path, parse_path

__all__ = [
    "ACTION_SIGILS",
    "PLAN_SCHEMA_VERSION",
    "Action",
    "Change",
    "FieldChange",
    "Plan",
    "PlanFormatError",
    "StateRef",
    "plan_from_dict",
]

#: Bumped when the on-disk plan grows a field an older netviz would misread.
PLAN_SCHEMA_VERSION: Final = 1


class PlanFormatError(ValueError):
    """A stored plan is not one this revision can read."""


class Action(str, Enum):
    """What a changeset entry does to one element."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RENAME = "rename"

    def __str__(self) -> str:
        return self.value


#: The one-character marker each action prints with, as ``terraform plan`` does.
ACTION_SIGILS: Final[dict[Action, str]] = {
    Action.CREATE: "+",
    Action.UPDATE: "~",
    Action.DELETE: "-",
    Action.RENAME: "→",
}

#: The colour each action is printed in.
ACTION_COLOURS: Final[dict[Action, str]] = {
    Action.CREATE: "green",
    Action.UPDATE: "yellow",
    Action.DELETE: "red",
    Action.RENAME: "cyan",
}


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field of one element, before and after.

    ``before`` and ``after`` are :data:`~netviz.plan.paths.MISSING` when the
    field is absent on that side. Absent is not ``None``: a document that says
    ``mtu: null`` and one that says nothing about the MTU differ, and only the
    second is restored by removing the key.
    """

    #: The field, in the selector grammar of :mod:`netviz.plan.paths`.
    path: tuple[Step, ...]
    before: Any = MISSING
    after: Any = MISSING

    @property
    def text(self) -> str:
        """The path as it is printed and stored."""
        return format_path(self.path)

    @property
    def added(self) -> bool:
        return self.before is MISSING and self.after is not MISSING

    @property
    def removed(self) -> bool:
        return self.after is MISSING and self.before is not MISSING

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"path": self.text}
        if self.before is not MISSING:
            record["before"] = self.before
        if self.after is not MISSING:
            record["after"] = self.after
        return record

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FieldChange:
        try:
            path = parse_path(str(payload["path"]))
        except KeyError:
            raise PlanFormatError("a field change needs a 'path'") from None
        return cls(
            path=path,
            before=payload.get("before", MISSING),
            after=payload.get("after", MISSING),
        )


@dataclass(frozen=True, slots=True)
class Change:
    """One entry of a changeset: what happens to one element."""

    action: Action
    address: Address
    #: The element's ``kind``. For an update this is the kind *after* the change.
    kind: str
    #: Field-level detail. Empty for every action but ``update``.
    fields: tuple[FieldChange, ...] = ()
    #: Where a rename lands. ``None`` for every other action.
    new_address: Address | None = None
    #: The whole document body, for ``create`` and ``delete``.
    document: Mapping[str, Any] | None = None
    #: ``file#index`` the element is declared at, when the source state had it.
    source: str | None = None

    @property
    def sigil(self) -> str:
        return ACTION_SIGILS[self.action]

    @property
    def colour(self) -> str:
        return ACTION_COLOURS[self.action]

    @property
    def headline(self) -> str:
        """The line that names the change, without its field detail."""
        if self.action is Action.RENAME:
            return f"{self.address} → {self.new_address}"
        return str(self.address)

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "action": self.action.value,
            "address": str(self.address),
            "kind": self.kind,
        }
        if self.new_address is not None:
            record["newAddress"] = str(self.new_address)
        if self.fields:
            record["fields"] = [change.to_dict() for change in self.fields]
        if self.document is not None:
            record["document"] = dict(self.document)
        if self.source is not None:
            record["source"] = self.source
        return record

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Change:
        try:
            action = Action(str(payload["action"]))
            address = parse_address(str(payload["address"]))
            kind = str(payload["kind"])
        except (KeyError, ValueError) as error:
            raise PlanFormatError(f"unreadable change entry: {error}") from error
        new_address = payload.get("newAddress")
        return cls(
            action=action,
            address=address,
            kind=kind,
            fields=tuple(FieldChange.from_dict(entry) for entry in payload.get("fields", ())),
            new_address=None if new_address is None else parse_address(str(new_address)),
            document=payload.get("document"),
            source=payload.get("source"),
        )


@dataclass(frozen=True, slots=True)
class StateRef:
    """Where one side of a plan came from, and what it hashed to."""

    #: ``tree``, ``git``, ``folder`` or ``live``.
    kind: str
    #: One line naming it, for the plan header and the terminal.
    description: str
    #: Content hash of the state (:mod:`netviz.plan.state`), when it has one.
    #: A live capture does not: there is nothing to re-read it from.
    digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"kind": self.kind, "description": self.description}
        if self.digest is not None:
            record["hash"] = self.digest
        return record

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StateRef:
        try:
            return cls(
                kind=str(payload["kind"]),
                description=str(payload["description"]),
                digest=None if payload.get("hash") is None else str(payload["hash"]),
            )
        except KeyError as error:
            raise PlanFormatError(f"unreadable state reference: missing {error}") from error


@dataclass(frozen=True, slots=True)
class Plan:
    """An ordered changeset between two inventory states."""

    changes: tuple[Change, ...] = ()
    #: The state the changes are applied *to*.
    source: StateRef = field(default_factory=lambda: StateRef(kind="tree", description="-"))
    #: The state they bring it *to*.
    target: StateRef = field(default_factory=lambda: StateRef(kind="tree", description="-"))
    version: str = __version__

    def __iter__(self) -> Iterator[Change]:
        return iter(self.changes)

    def __len__(self) -> int:
        return len(self.changes)

    @property
    def empty(self) -> bool:
        """Is there nothing to do? This is what CI gates on."""
        return not self.changes

    def counts(self) -> dict[Action, int]:
        """How many entries of each action, every action present."""
        counts = dict.fromkeys(Action, 0)
        for change in self.changes:
            counts[change.action] += 1
        return counts

    def select(self, patterns: Sequence[str]) -> Plan:
        """The subset of the plan ``--target`` selects, in the same order.

        A rename is matched at either end: an operator who targets the new name
        has plainly asked for the rename that produces it.
        """
        if not patterns:
            return self
        return Plan(
            changes=tuple(change for change in self.changes if _selected(change, patterns)),
            source=self.source,
            target=self.target,
            version=self.version,
        )

    def to_dict(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "schemaVersion": PLAN_SCHEMA_VERSION,
            "tool": {"name": "netviz", "version": self.version},
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "summary": {
                **{action.value: counts[action] for action in Action},
                "total": len(self.changes),
            },
            "empty": self.empty,
            "changes": [change.to_dict() for change in self.changes],
        }


def _selected(change: Change, patterns: Sequence[str]) -> bool:
    addresses = (
        [change.address] if change.new_address is None else [change.address, change.new_address]
    )
    return any(address.matches(pattern) for address in addresses for pattern in patterns)


def plan_from_dict(payload: Any) -> Plan:
    """Read a stored plan back.

    Raises:
        PlanFormatError: The document is not a plan, or is a newer revision of
            one. Refusing an unknown ``schemaVersion`` matters more here than it
            does for a report: this document is about to be *executed*.
    """
    if not isinstance(payload, Mapping):
        raise PlanFormatError("a plan file holds a JSON object")
    version = payload.get("schemaVersion")
    if version != PLAN_SCHEMA_VERSION:
        raise PlanFormatError(
            f"plan schema version {version!r} cannot be applied by netviz {__version__}, "
            f"which writes and reads version {PLAN_SCHEMA_VERSION}"
        )
    changes = payload.get("changes", ())
    if not isinstance(changes, Sequence) or isinstance(changes, str):
        raise PlanFormatError("'changes' holds a list of change entries")
    tool = payload.get("tool")
    return Plan(
        changes=tuple(Change.from_dict(entry) for entry in changes),
        source=StateRef.from_dict(payload.get("source", {})),
        target=StateRef.from_dict(payload.get("target", {})),
        version=str(tool.get("version", __version__)) if isinstance(tool, Mapping) else __version__,
    )
