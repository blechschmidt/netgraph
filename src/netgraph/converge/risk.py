"""Which changes can lock you out, and how netgraph decides.

The one failure mode that makes an automated remediation worse than no
remediation is the plan that works perfectly and then cannot be checked, because
the last command took away the path the operator was on. So every change is
classified before anything is written, and a plan holding a disruptive change is
refused unless the operator says, in as many words, that they have another way
in.

**The management path of a device** is defined here as three things, and the
first is not a guess:

1. **The interface netgraph would reach the box on.** That is
   :func:`netgraph.export.context.management_address`, the same ranking that
   picks ``ansible_host`` and a Prometheus scrape target -- an explicitly named
   management port first, then a loopback with a routable address, then the
   declared order. One definition, three consumers: if netgraph would monitor
   the device over ``mgmt0``, then ``mgmt0`` is what a converge script must not
   pull out from under itself.
2. **Everything that interface is built on.** Taking a member out of the bond
   that carries the management address, or deleting the parent of the management
   sub-interface, is the same mistake spelled differently.
3. **The VLAN it lives in**, when the management interface is a VLAN
   sub-interface or an access port: untagging that VLAN on the port is again the
   same mistake.

**"Shuts an interface carrying the operator's own session"** is the second half
of the definition, and it is deliberately broader than the first: bringing *any*
interface down, or deleting one, takes whatever is behind it away from whoever
is behind it. A device with no addresses at all -- an unmanaged switch in the
inventory -- has no management path netgraph can name, and there an interface
going down is still a thing somebody should have to opt into.

What is explicitly *not* disruptive: setting an MTU, correcting a MAC on an
interface that is not the management one, adding an address, creating a VLAN,
enabling an interface that was down. Those are the changes a converge run should
be able to make at three in the morning without an argument.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from netgraph.converge.intent import Intent, IntentKind
from netgraph.converge.model import Risk
from netgraph.export.context import management_address
from netgraph.models import Device
from netgraph.models.interface import Interface, VlanMode
from netgraph.render.graph import Node

__all__ = ["ManagementPath", "classify", "management_path"]

#: Intent kinds that take something away from an interface or take it down.
#: Anything on the management path that is one of these is disruptive; anything
#: on the management path that is *not* only changes a value, which the box
#: survives.
#:
#: :data:`IntentKind.MEMBER_ADD` is absent here and handled separately in
#: :func:`_against_management`, because membership is the one relation that names
#: two interfaces: enslaving the management port moves its address just as
#: finally as releasing it does, and which of the two is "taking away" depends on
#: which end you look from.
_TAKES_AWAY: Final[frozenset[IntentKind]] = frozenset(
    {
        IntentKind.INTERFACE_DISABLE,
        IntentKind.INTERFACE_DELETE,
        IntentKind.ADDRESS_REMOVE,
        IntentKind.MEMBER_REMOVE,
        IntentKind.VLAN_UNTAG,
        IntentKind.VLAN_DELETE,
        IntentKind.VLAN_MODE,
        IntentKind.VLAN_ACCESS,
    }
)

#: Interface fields whose change bounces the link. On the management path that
#: drops the session, which is the same outcome as shutting the interface and is
#: classified the same way.
#:
#: ``mtu`` is deliberately absent: every stack netgraph writes for changes it on
#: a live interface without taking the link down, and classifying it as
#: disruptive would make ``--allow-disruptive`` the flag every run needs, which
#: is the same as having no flag.
_BOUNCING_FIELDS: Final[frozenset[str]] = frozenset({"mac", "parent"})


@dataclass(frozen=True, slots=True)
class ManagementPath:
    """The interfaces and VLANs of one device that a session depends on."""

    element: str
    #: The single interface netgraph would reach the device on, or ``""`` when
    #: the device declares no address at all.
    interface: str = ""
    #: The address that choice was made on, for the diagnostic.
    address: str = ""
    #: :attr:`interface` and everything it is stacked on or aggregates.
    interfaces: frozenset[str] = frozenset()
    #: VLAN ids the management interface depends on, as strings.
    vlans: frozenset[str] = frozenset()

    def holds(self, interface: str) -> bool:
        return bool(interface) and interface in self.interfaces

    @property
    def known(self) -> bool:
        return bool(self.interface)


def management_path(node: Node, device: Device) -> ManagementPath:
    """Work out how ``device`` is reached, and what that depends on.

    Args:
        node: The device's node in the layer-1 graph, which is what
            :func:`~netgraph.export.context.management_address` reads.
        device: The declared document, for the interface stack.
    """
    chosen = management_address(node)
    if chosen is None:
        return ManagementPath(element=node.fqn)
    by_name = {interface.name: interface for interface in device.spec.interfaces}
    stack = _stack(chosen.interface, by_name)
    return ManagementPath(
        element=node.fqn,
        interface=chosen.interface,
        address=chosen.cidr,
        interfaces=frozenset(stack),
        vlans=frozenset(_vlans(stack, by_name)),
    )


def _stack(name: str, by_name: dict[str, Interface]) -> set[str]:
    """``name`` plus every interface underneath it.

    Walks two relationships to a fixed point: ``parent`` (a VLAN sub-interface
    on the port it tags) and ``members`` (a bridge or bond on the ports it
    aggregates). Bounded by the number of interfaces because an interface
    already in the set is never expanded twice, so a malformed inventory that
    declares a cycle cannot hang the command.
    """
    found = {name}
    pending = [name]
    while pending:
        interface = by_name.get(pending.pop())
        if interface is None:
            continue
        below = [interface.parent] if interface.parent else []
        below.extend(interface.members or ())
        for lower in below:
            if lower and lower not in found:
                found.add(lower)
                pending.append(lower)
    return found


def _vlans(stack: Iterable[str], by_name: dict[str, Interface]) -> set[str]:
    """VLAN ids the management stack rides in."""
    ids: set[str] = set()
    for name in stack:
        interface = by_name.get(name)
        if interface is None or interface.vlan is None:
            continue
        if interface.vlan.access_vlan is not None:
            ids.add(str(interface.vlan.access_vlan))
        if interface.vlan.mode is VlanMode.TRUNK:
            ids.update(str(vlan) for vlan in interface.vlan.trunk_vlans or ())
        if interface.vlan.native_vlan is not None:
            ids.add(str(interface.vlan.native_vlan))
    return ids


def classify(intent: Intent, path: ManagementPath | None) -> tuple[Risk, str]:
    """``(risk, reason)`` for one intent.

    ``path`` is ``None`` for an element the graph has no node for -- a cable, an
    undeclared device. Those carry no commands, so nothing can be disruptive.
    """
    if intent.kind is IntentKind.MANUAL:
        return (Risk.SAFE, "")
    if path is not None and path.known:
        verdict = _against_management(intent, path)
        if verdict is not None:
            return (Risk.DISRUPTIVE, verdict)
    if intent.kind is IntentKind.INTERFACE_DISABLE:
        return (
            Risk.DISRUPTIVE,
            f"shuts {intent.interface}; whatever is behind that port loses its path while "
            "the interface is down",
        )
    if intent.kind is IntentKind.INTERFACE_DELETE:
        return (
            Risk.DISRUPTIVE,
            f"removes the interface {intent.interface}; anything configured on it goes with it",
        )
    return (Risk.SAFE, "")


def _against_management(intent: Intent, path: ManagementPath) -> str | None:
    """The sentence explaining why this intent touches the management path."""
    where = f"{path.interface} carries the management address {path.address}"
    if intent.kind is IntentKind.VLAN_DELETE and intent.target in path.vlans:
        return f"deletes VLAN {intent.target}, which {where} depends on"

    # Membership names two interfaces, and the *member* is the one that moves:
    # 'ip link set mgmt0 master br0' takes the management address off mgmt0
    # whichever way round the intent reads. Both are checked, and enslaving is
    # as final as releasing -- the address stops answering either way.
    if intent.kind in (IntentKind.MEMBER_ADD, IntentKind.MEMBER_REMOVE) and path.holds(
        intent.target
    ):
        verb = "enslaves" if intent.kind is IntentKind.MEMBER_ADD else "releases"
        return (
            f"{verb} {intent.target} {'to' if verb == 'enslaves' else 'from'} "
            f"{intent.interface}, which moves the address off it; {where}"
        )

    # An address the capture found on the wrong interface is still *the*
    # management address, and taking it off is a lock-out wherever it is.
    if intent.kind is IntentKind.ADDRESS_REMOVE and intent.target == path.address:
        return (
            f"removes {intent.target} from {intent.interface}, and that is the address the "
            f"device is reached on"
        )

    if not path.holds(intent.interface):
        return None
    stacked = "" if intent.interface == path.interface else f" that {path.interface} is built on"
    if intent.kind in _TAKES_AWAY:
        if intent.kind is IntentKind.VLAN_UNTAG and intent.target not in path.vlans:
            return None
        return f"takes {intent.target or 'the interface'} off {intent.interface}{stacked}; {where}"
    if intent.kind is IntentKind.INTERFACE_SET and intent.target in _BOUNCING_FIELDS:
        return (
            f"changes {intent.target} on {intent.interface}{stacked}, which bounces the "
            f"interface; {where}"
        )
    return None
