"""The dialect-free vocabulary of a convergence, and the order it runs in.

A drift finding says *what disagrees*. An :class:`Intent` says *what to do about
it*, in terms no vendor owns: create this VLAN, put this address on that
interface, take that sub-interface away. The dialects in
:mod:`netgraph.converge.commands` turn an intent into lines; the ordering, the
risk classification and the prerequisites are all decided here, once, so they
cannot differ between one dialect and the next.

The order
---------

:data:`RANKS` is the whole ordering policy, and it is a flat table rather than a
graph search because the dependencies between *kinds* of change are fixed by how
networks work, not by the particular network:

* **Build before you use.** A VLAN exists before a port is put in it; an
  interface exists before it is enslaved, addressed or brought up.
* **Address before routing.** A next hop that is not on a configured subnet is
  rejected by every stack there is, so the address goes on first.
* **Undo in reverse.** Removals run after every addition and in the mirror
  order: an address comes off before the interface under it does, and a VLAN is
  deleted only once nothing is in it. Applied to a half-finished run, that
  leaves the device in a state that still forwards.
* **Down last, up early.** :data:`IntentKind.INTERFACE_ENABLE` sits before every
  removal and :data:`IntentKind.INTERFACE_DISABLE` after every addition, so a
  script that is interrupted has brought things up and not yet taken anything
  down.

Within a rank, order is by target name, which is stable and readable. Across
ranks, :func:`prerequisites_of` adds the *specific* edges the table cannot know:
this sub-interface needs that parent, this port needs that VLAN to exist.

Nothing here knows about the management path either; that is
:mod:`netgraph.converge.risk`. An intent is a statement of what would close a
difference, and whether it is safe to do is a separate question with a separate
answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from netgraph.converge.model import Action, Provenance

__all__ = [
    "RANKS",
    "Intent",
    "IntentKind",
    "article",
    "order_intents",
    "prerequisites_of",
]


class IntentKind(str, Enum):
    """Everything a convergence can propose.

    Bounded by what a capture can actually detect: ``netgraph drift`` compares
    the interface set, four interface scalars, the address list per family,
    bridge and bond membership, VLAN mode, access VLAN and carried VLANs, and
    the cabling. Nothing here proposes a change to something no input could have
    contradicted -- a plan that "fixed" an OSPF area nobody measured would be
    netgraph guessing with a root shell.
    """

    VLAN_CREATE = "vlan.create"
    INTERFACE_CREATE = "interface.create"
    INTERFACE_SET = "interface.set"
    MEMBER_ADD = "member.add"
    VLAN_MODE = "vlan.mode"
    VLAN_ACCESS = "vlan.access"
    VLAN_TAG = "vlan.tag"
    ADDRESS_ADD = "address.add"
    INTERFACE_ENABLE = "interface.enable"
    INTERFACE_DISABLE = "interface.disable"
    ADDRESS_REMOVE = "address.remove"
    VLAN_UNTAG = "vlan.untag"
    MEMBER_REMOVE = "member.remove"
    INTERFACE_DELETE = "interface.delete"
    VLAN_DELETE = "vlan.delete"
    #: Something a person has to do: move a cable, declare a device, re-terminate
    #: a fibre. Never carries commands.
    MANUAL = "manual"

    def __str__(self) -> str:
        return self.value


#: Position in the dependency order. Gaps of ten so a kind can be inserted
#: between two others without renumbering a plan somebody has already reviewed.
RANKS: Final[dict[IntentKind, int]] = {
    IntentKind.VLAN_CREATE: 10,
    IntentKind.INTERFACE_CREATE: 20,
    IntentKind.INTERFACE_SET: 30,
    IntentKind.MEMBER_ADD: 40,
    IntentKind.VLAN_MODE: 50,
    IntentKind.VLAN_ACCESS: 60,
    IntentKind.VLAN_TAG: 70,
    IntentKind.ADDRESS_ADD: 80,
    IntentKind.INTERFACE_ENABLE: 90,
    IntentKind.INTERFACE_DISABLE: 100,
    IntentKind.ADDRESS_REMOVE: 110,
    IntentKind.VLAN_UNTAG: 120,
    IntentKind.MEMBER_REMOVE: 130,
    IntentKind.INTERFACE_DELETE: 140,
    IntentKind.VLAN_DELETE: 150,
    IntentKind.MANUAL: 900,
}

#: Which :class:`~netgraph.converge.model.Action` each kind reports as. Only
#: used for display and for the JSON summary; the rank is what orders.
ACTIONS: Final[dict[IntentKind, Action]] = {
    IntentKind.VLAN_CREATE: Action.CREATE,
    IntentKind.INTERFACE_CREATE: Action.CREATE,
    IntentKind.INTERFACE_SET: Action.UPDATE,
    IntentKind.MEMBER_ADD: Action.CREATE,
    IntentKind.VLAN_MODE: Action.UPDATE,
    IntentKind.VLAN_ACCESS: Action.UPDATE,
    IntentKind.VLAN_TAG: Action.CREATE,
    IntentKind.ADDRESS_ADD: Action.CREATE,
    IntentKind.INTERFACE_ENABLE: Action.UPDATE,
    IntentKind.INTERFACE_DISABLE: Action.UPDATE,
    IntentKind.ADDRESS_REMOVE: Action.DELETE,
    IntentKind.VLAN_UNTAG: Action.DELETE,
    IntentKind.MEMBER_REMOVE: Action.DELETE,
    IntentKind.INTERFACE_DELETE: Action.DELETE,
    IntentKind.VLAN_DELETE: Action.DELETE,
    IntentKind.MANUAL: Action.MANUAL,
}

#: What each kind is a change *to*, for the plan's ``object`` column.
OBJECTS: Final[dict[IntentKind, str]] = {
    IntentKind.VLAN_CREATE: "vlan",
    IntentKind.VLAN_DELETE: "vlan",
    IntentKind.INTERFACE_CREATE: "interface",
    IntentKind.INTERFACE_DELETE: "interface",
    IntentKind.INTERFACE_SET: "field",
    IntentKind.INTERFACE_ENABLE: "field",
    IntentKind.INTERFACE_DISABLE: "field",
    IntentKind.MEMBER_ADD: "member",
    IntentKind.MEMBER_REMOVE: "member",
    IntentKind.VLAN_MODE: "vlan",
    IntentKind.VLAN_ACCESS: "vlan",
    IntentKind.VLAN_TAG: "vlan",
    IntentKind.VLAN_UNTAG: "vlan",
    IntentKind.ADDRESS_ADD: "address",
    IntentKind.ADDRESS_REMOVE: "address",
    IntentKind.MANUAL: "manual",
}

#: Kinds that take something down, or take something away from an interface that
#: is up. Read by :mod:`netgraph.converge.risk`; kept here so the two tables sit
#: next to the enum they classify.
REMOVES: Final[frozenset[IntentKind]] = frozenset(
    {
        IntentKind.INTERFACE_DISABLE,
        IntentKind.ADDRESS_REMOVE,
        IntentKind.VLAN_UNTAG,
        IntentKind.MEMBER_REMOVE,
        IntentKind.INTERFACE_DELETE,
        IntentKind.VLAN_DELETE,
    }
)


@dataclass(frozen=True, slots=True)
class Intent:
    """One dialect-free thing to do to one device."""

    kind: IntentKind
    #: Fully-qualified name of the element, or the capture's name for something
    #: the inventory does not declare.
    element: str
    #: The interface the change is on. Empty for a device-wide change (a VLAN
    #: database entry) and for a manual one.
    interface: str = ""
    #: What is being set, added or removed, in the device's own spelling: an
    #: address, a VLAN id, a member name, a field name.
    target: str = ""
    #: The value the inventory declares, when the change sets one.
    value: str | None = None
    #: The value the capture reported, so the inverse script can put it back.
    previous: str | None = None
    #: Interface ``type`` from the inventory, for a create; from the capture,
    #: for a delete. A dialect needs it to know whether it can make the thing.
    interface_type: str = ""
    #: Parent interface, for a stacked interface being created.
    parent: str = ""
    #: The drift findings behind this intent.
    provenance: tuple[Provenance, ...] = ()
    #: Free text for a manual intent: what a person has to go and do. Longer
    #: than :attr:`summary`, which is the one line a table holds.
    note: str = ""
    #: Overrides the sentence :func:`netgraph.converge.commands.describe` would
    #: produce. Only a manual intent sets it: what a person has to do cannot be
    #: derived from a verb and a noun the way a command can.
    summary: str = ""
    #: Extra prerequisites the caller knows about and the table cannot derive.
    requires: tuple[str, ...] = field(default=())

    @property
    def rank(self) -> int:
        return RANKS[self.kind]

    @property
    def action(self) -> Action:
        return ACTIONS[self.kind]

    @property
    def object(self) -> str:
        return OBJECTS[self.kind]

    @property
    def key(self) -> str:
        """Identity within one element: ``interface.eno1.mac``.

        Two intents with the same key are the same proposal, which is how a
        prerequisite refers to one without needing the whole object.
        """
        parts = [self.kind.value]
        if self.interface:
            parts.append(self.interface)
        if self.target:
            parts.append(self.target)
        return "/".join(parts)

    @property
    def id(self) -> str:
        """Identity within a plan: the element and the key."""
        return f"{self.element}#{self.key}"

    @property
    def order(self) -> tuple[int, str, str, str]:
        return (self.rank, self.object, self.interface, self.target)


def article(word: str) -> str:
    """``a`` or ``an``, by first letter.

    Interface types, VLAN modes and element kinds are all ASCII words from a
    closed set, so the naive rule is exactly right here and a linguistics library
    would be four dependencies to get ``an ethernet`` instead of ``a ethernet``.
    """
    return "an" if word[:1].lower() in "aeiou" else "a"


def order_intents(intents: Iterable[Intent]) -> tuple[Intent, ...]:
    """``intents`` in dependency order.

    A stable sort by :attr:`Intent.order` is enough, and it is enough for a
    reason worth stating: :data:`RANKS` already encodes every dependency between
    *kinds*, and the only dependencies left within a kind are between stacked
    interfaces -- a sub-interface and its parent, a bond and its members. Those
    are handled by name depth rather than by a topological sort, because a
    stacked interface in every dialect netgraph writes is named after the thing
    it sits on (``eno1.30``, ``br0``), and because a cycle in a device's
    interface stack is not a thing that can exist.
    """
    return tuple(
        sorted(
            intents,
            key=lambda intent: (intent.rank, intent.object, _depth(intent), intent.target),
        )
    )


def _depth(intent: Intent) -> tuple[int, str]:
    """Shallow interfaces before deep ones for a create; the reverse for a delete.

    ``eno1`` before ``eno1.30`` when both are being made, and ``eno1.30`` before
    ``eno1`` when both are going away. Depth is the number of dot-separated
    segments in the name, which is how every Linux dialect and every vendor CLI
    netgraph writes spells a sub-interface.
    """
    name = intent.interface or intent.target
    segments = name.count(".")
    if intent.kind in REMOVES:
        return (-segments, name)
    return (segments, name)


def prerequisites_of(intent: Intent, others: Sequence[Intent]) -> tuple[str, ...]:
    """The ids of intents in ``others`` that must land before ``intent``.

    Three specific edges the rank table cannot know, because they depend on what
    is in *this* plan:

    1. **A VLAN before a port that carries it.** Putting a port in VLAN 20 when
       the device's VLAN database has no 20 is rejected on every switch that has
       a VLAN database at all, and silently ignored on some that do not.
    2. **A parent before the interface stacked on it.** ``eno1.30`` cannot be
       created before ``eno1`` exists.
    3. **A member before the aggregate that enslaves it**, and the aggregate
       before the address that goes on it.

    Everything else is the rank order, which every consumer already respects.
    """
    by_key = {other.key: other for other in others if other is not intent}
    required: list[str] = []

    def need(key: str) -> None:
        other = by_key.get(key)
        if other is not None and other.rank <= intent.rank:
            required.append(other.id)

    if intent.kind in (IntentKind.VLAN_ACCESS, IntentKind.VLAN_TAG):
        need(f"{IntentKind.VLAN_CREATE.value}/{intent.target}")
    if intent.kind is IntentKind.VLAN_MODE:
        # Every VLAN this *interface* is being put into, not every VLAN in the
        # plan: making a port an access port depends on the one VLAN it lands in,
        # and requiring a VLAN some other port needs would serialise a plan for
        # no reason.
        for other in others:
            if (
                other.kind in (IntentKind.VLAN_ACCESS, IntentKind.VLAN_TAG)
                and other.interface == intent.interface
            ):
                need(f"{IntentKind.VLAN_CREATE.value}/{other.target}")
    if intent.parent:
        need(f"{IntentKind.INTERFACE_CREATE.value}/{intent.parent}")
    if intent.interface:
        need(f"{IntentKind.INTERFACE_CREATE.value}/{intent.interface}")
    if intent.kind is IntentKind.INTERFACE_DELETE:
        # Everything on the interface comes off before the interface does.
        required.extend(
            other.id
            for other in others
            if other is not intent
            and other.interface == intent.interface
            and other.kind in REMOVES
            and other.rank < intent.rank
        )
    required.extend(intent.requires)
    return tuple(sorted(set(required)))
