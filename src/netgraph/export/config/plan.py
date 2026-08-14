"""One device, resolved into everything a dialect needs and nothing it invents.

Six dialects have to answer the same questions about a device before any of them
can write a line: which interfaces does it declare, which of them are stacked on
which, which tunnels have an end here and what is at the other end, and which
interface does a static route belong to. Answering them six times would be six
chances to answer them differently — and the difference would show up as a
netplan file and a systemd-networkd file that configure the same box two ways.

So they are answered once, here, and a :class:`DevicePlan` is what a dialect is
handed. The plan is a *view*, not a translation: every field on it is something
the inventory states, and the two places where netgraph reasons rather than reads
say so in their own docstrings — :attr:`TunnelPeer.endpoint`, which follows the
tunnel interface's declared underlay to an address that underlay declares, and
:func:`route_interface`, which finds the interface a next hop is on-link from.
Neither invents a value; both are derivations from stated facts, and both are
recorded in the export manifest as such.

The plan deliberately does *not* decide what a dialect can express. That is a
property of the dialect, not of the device, and putting it here would mean the
plan had to know about netplan's VRF tables and FRR's route distinguishers. Each
dialect declares its own limits (:mod:`netgraph.export.config.model`).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from netgraph.errors import compact_ids
from netgraph.export.context import ExportContext, elements_of
from netgraph.loader.inventory import Inventory, namespace_of, short_name
from netgraph.models import Device, Interface, InterfaceType, StaticRoute, Tunnel
from netgraph.models.interface import VlanMode
from netgraph.models.tunnel import TunnelType
from netgraph.render.graph import Layer
from netgraph.subnets import is_routable_address

__all__ = [
    "DevicePlan",
    "TunnelPeer",
    "TunnelPlan",
    "addresses_of",
    "device_plans",
    "is_stacked",
    "restricts_vlans",
    "route_interface",
    "unwritable_vlan",
]

#: How an address family is spelled in a field path, by IP version.
_FAMILY: Final[dict[int, str]] = {4: "ipv4", 6: "ipv6"}


# --------------------------------------------------------------------------- #
# Tunnels
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TunnelPeer:
    """The far end of a tunnel, as the near end would have to configure it."""

    #: Fully-qualified name of the peer device.
    element: str
    #: Its own name, for a comment that has no room for the namespace.
    name: str
    #: The peer's tunnel interface.
    interface: str
    #: Addresses the peer holds *inside* the tunnel, ``a/len``, in declaration
    #: order. This is what a WireGuard ``AllowedIPs`` is derived from, and it is
    #: exact: the peer says these are its overlay addresses, so routing them
    #: down the tunnel is what the inventory states rather than a guess at the
    #: networks behind it.
    overlay: tuple[str, ...] = ()
    #: ``host:port`` the outer packets go to, or empty when the inventory does
    #: not say.
    #:
    #: Derived, never invented: the peer's tunnel interface names its underlay
    #: in ``parent``, that underlay declares an address, and the tunnel declares
    #: a port. Break any link of that chain and there is no endpoint, which is
    #: the ordinary case for a peer behind NAT — WireGuard calls it a roaming
    #: peer and leaves ``Endpoint`` out, so leaving it out is also correct.
    endpoint: str = ""
    #: Why there is no :attr:`endpoint`, when there is none. Written into the
    #: generated file as a comment, because the operator reading it is the one
    #: who knows whether the peer roams or whether an address is missing.
    endpoint_note: str = ""


@dataclass(frozen=True, slots=True)
class TunnelPlan:
    """One tunnel with an end on this device, seen from this device."""

    #: Fully-qualified name of the tunnel document.
    fqn: str
    tunnel: Tunnel
    #: The local ``tunnel``-type interface the tunnel terminates on.
    interface: Interface
    peers: tuple[TunnelPeer, ...] = ()
    #: Where the tunnel document was declared, relative to the inventory root.
    source: str = ""

    @property
    def name(self) -> str:
        return self.tunnel.metadata.name

    @property
    def type(self) -> TunnelType:
        return self.tunnel.spec.type

    @property
    def port(self) -> int | None:
        """The outer port, materialised from the type's registered default."""
        return self.tunnel.spec.port

    @property
    def is_multipoint(self) -> bool:
        """More than two ends — a hub, or a broadcast overlay."""
        return len(self.tunnel.endpoints) > 2

    def field(self, *rest: str) -> str:
        """``spec.type`` of *this tunnel document*, for a refusal."""
        return ".".join(("spec", *rest))


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DevicePlan:
    """One device's networking, resolved once for every dialect to read."""

    #: Fully-qualified name.
    fqn: str
    #: The device's own name, which is what its files are named after.
    name: str
    #: ``switch``, ``router``, ``computer``, …
    kind: str
    device: Device
    #: Tunnels with an end on this device, in tunnel-name order.
    tunnels: tuple[TunnelPlan, ...] = ()
    #: Every document this configuration was generated from, relative to the
    #: inventory root and sorted. Written into the header of every file: months
    #: later the question is "which YAML do I edit to change this", and the
    #: generated file is the only thing in front of the person asking.
    sources: tuple[str, ...] = ()

    @property
    def interfaces(self) -> Sequence[Interface]:
        """``spec.interfaces``, in declaration order."""
        return self.device.spec.interfaces

    @property
    def routes(self) -> Sequence[StaticRoute]:
        return self.device.spec.routes

    def index_of(self, interface: Interface) -> int:
        """Position of ``interface`` in ``spec.interfaces``, for a field path."""
        for index, candidate in enumerate(self.interfaces):
            if candidate.name == interface.name:
                return index
        raise KeyError(interface.name)  # pragma: no cover - callers iterate the list

    def field(self, *rest: str | int) -> str:
        """``spec.vrfs[0].rd`` — a dotted path a reader can grep the YAML for."""
        text = "spec"
        for part in rest:
            text += f"[{part}]" if isinstance(part, int) else f".{part}"
        return text

    def interface_field(self, interface: Interface, *rest: str | int) -> str:
        """The same, rooted at one interface: ``spec.interfaces[2].vlan``."""
        return self.field("interfaces", self.index_of(interface), *rest)

    def interface(self, name: str) -> Interface | None:
        return self.device.interface(name)

    def tunnel_on(self, name: str) -> TunnelPlan | None:
        """The tunnel terminating on the interface called ``name``."""
        for plan in self.tunnels:
            if plan.interface.name == name:
                return plan
        return None

    def stacked_on(self, name: str) -> tuple[Interface, ...]:
        """Interfaces that name ``name`` as their ``parent``, in order."""
        return tuple(entry for entry in self.interfaces if entry.parent == name)

    def enslaved_by(self, name: str) -> Interface | None:
        """The bridge or LAG that lists ``name`` among its ``members``.

        An interface may be a member of at most one aggregate (``NG-I004``), so
        the first match is the only one; the loop is over the declaration order
        so that two aggregates the validator would have refused still give a
        deterministic answer under ``--force``.
        """
        for entry in self.interfaces:
            if entry.members and name in entry.members:
                return entry
        return None

    @property
    def forwards_ipv4(self) -> bool:
        """Is this device meant to forward IPv4?

        Materialised from the device kind when ``spec.forwarding`` is not
        stated, which is what makes a ``router`` forward and a ``computer`` not
        (``docs/schema.md`` §5.3). Emitting it is therefore transcription rather
        than invention — but the header of the file that carries it says where
        the value came from, because "my inventory never said that" is a fair
        thing to think on finding a sysctl in a generated tree.
        """
        forwarding = self.device.spec.forwarding
        return bool(forwarding and forwarding.ipv4)

    @property
    def forwards_ipv6(self) -> bool:
        forwarding = self.device.spec.forwarding
        return bool(forwarding and forwarding.ipv6)


def device_plans(context: ExportContext) -> tuple[DevicePlan, ...]:
    """Every selected device, resolved, in canonical order.

    The selection is the graph the CLI already filtered, so ``--namespace`` and
    ``--kind`` narrow a configuration export exactly as they narrow a diagram.
    Non-device nodes — a patch panel, a PDU, an adapter — are dropped without a
    word: none of them runs a configuration, and saying so per panel would fill
    every manifest with the obvious.
    """
    inventory = context.inventory
    tunnels = _tunnels_by_device(inventory)
    plans: list[DevicePlan] = []
    for node in elements_of(context.at(Layer.L1)):
        device = node.element
        if not isinstance(device, Device):
            continue
        local = tunnels.get(node.fqn, ())
        plans.append(
            DevicePlan(
                fqn=node.fqn,
                name=node.name,
                kind=node.kind,
                device=device,
                tunnels=local,
                sources=_sources(inventory, node.fqn, local),
            )
        )
    return tuple(plans)


def _sources(inventory: Inventory, fqn: str, tunnels: Sequence[TunnelPlan]) -> tuple[str, ...]:
    """The documents one device's configuration was generated from."""
    own = inventory.source_of(fqn)
    paths = {own.relative for own in (own,) if own is not None}
    paths.update(plan.source for plan in tunnels if plan.source)
    return tuple(sorted(paths))


def _tunnels_by_device(inventory: Inventory) -> dict[str, tuple[TunnelPlan, ...]]:
    """Index every tunnel by the devices it terminates on.

    Built from the inventory rather than from the overlay graph on purpose. A
    tunnel is a property of the device that terminates it, and a filter that
    selected one end and not the other must still produce a complete
    configuration for the end it selected — the near end has to know the far
    end's public key whether or not the far end is being generated in the same
    run.
    """
    owners = inventory.interface_owners
    collected: dict[str, list[TunnelPlan]] = {}
    for fqn, tunnel in inventory.tunnels.items():
        namespace = namespace_of(fqn)
        ends: list[tuple[str, Interface]] = []
        for ref in tunnel.endpoints:
            owner_fqn = inventory.resolve_fqn(ref.device, namespace=namespace)
            owner = owners.get(owner_fqn) if owner_fqn is not None else None
            interface = owner.interface(ref.interface) if owner is not None else None
            if owner_fqn is None or interface is None:
                # The validator refuses this (E016); only --force reaches here,
                # and the graph has already recorded the dangling end.
                continue
            ends.append((owner_fqn, interface))

        source = inventory.source_of(fqn)
        for index, (owner_fqn, interface) in enumerate(ends):
            peers = tuple(
                _peer(inventory, other_fqn, other)
                for position, (other_fqn, other) in enumerate(ends)
                if position != index
            )
            collected.setdefault(owner_fqn, []).append(
                TunnelPlan(
                    fqn=fqn,
                    tunnel=tunnel,
                    interface=interface,
                    peers=peers,
                    source=source.relative if source is not None else "",
                )
            )
    return {
        owner: tuple(sorted(plans, key=lambda plan: (plan.interface.name, plan.fqn)))
        for owner, plans in collected.items()
    }


def _peer(inventory: Inventory, fqn: str, interface: Interface) -> TunnelPeer:
    """One far end, with its overlay addresses and its underlay endpoint."""
    endpoint, note = _underlay_endpoint(inventory, fqn, interface)
    return TunnelPeer(
        element=fqn,
        name=short_name(fqn),
        interface=interface.name,
        overlay=addresses_of(interface),
        endpoint=endpoint,
        endpoint_note=note,
    )


def _underlay_endpoint(inventory: Inventory, fqn: str, interface: Interface) -> tuple[str, str]:
    """``(address, why-not)`` for the outer destination of a peer's packets.

    The chain is entirely inside the inventory: the peer's tunnel interface says
    which port its outer packets leave by (``parent``, §14.4), that port declares
    an address, and that address is where the near end sends to. Each link that
    is missing produces a different note, because they are different things for
    an operator to do about it.
    """
    if interface.parent is None:
        return "", (
            f"{short_name(fqn)}:{interface.name} does not name an underlay port "
            f"('parent'), so the inventory does not say where to reach it"
        )
    owner = inventory.interface_owners.get(fqn)
    underlay = owner.interface(interface.parent) if owner is not None else None
    if underlay is None:
        return "", (f"the underlay port {interface.parent!r} of {short_name(fqn)} is not declared")
    routable = addresses_of(underlay, routable_only=True)
    if not routable:
        return "", (
            f"the underlay port {short_name(fqn)}:{underlay.name} declares no routable "
            f"address, so there is nothing to send the outer packets to"
        )
    return str(ipaddress.ip_interface(routable[0]).ip), ""


# --------------------------------------------------------------------------- #
# Shared derivations
# --------------------------------------------------------------------------- #


def addresses_of(interface: Interface, *, routable_only: bool = False) -> tuple[str, ...]:
    """``('10.0.0.1/24', '2001:db8::1/64')`` — every address, IPv4 first.

    Declaration order within a family, families in the order an operator reads
    them. ``routable_only`` applies the same exclusion the layer-3 diagram and
    ``netgraph ipam`` apply, which is what a tunnel endpoint wants: a link-local
    address is not somewhere to send an outer packet to.
    """
    return tuple(_addresses(interface, routable_only))


def _addresses(interface: Interface, routable_only: bool) -> Iterator[str]:
    for config in (interface.ipv4, interface.ipv6):
        if config is None:
            continue
        for address in config.addresses:
            if not routable_only or is_routable_address(address):
                yield f"{address.ip}/{address.prefix_length}"


def route_interface(plan: DevicePlan, route: StaticRoute) -> str | None:
    """Which interface a static route belongs to, or ``None``.

    netplan and systemd-networkd both hang a route off an interface; iproute2
    and FRR do not. So this exists for the two that need it, and it answers in
    the order the answer is *certain*:

    1. ``dev``, when the route states one. Nothing to derive.
    2. The interface whose declared prefix contains the next hop. A next hop has
       to be on-link for the route to work at all — the validator says so in
       ``E032`` — so the interface holding the covering prefix is not a guess,
       it is the only interface the route could use.

    ``None`` when the route states no ``dev`` and no ``via`` that any declared
    address covers. The dialect records it as skipped rather than attaching it
    to whichever interface came first, which would put a route on a port that
    cannot reach its next hop.
    """
    if route.dev is not None:
        return route.dev
    if route.via is None:
        return None
    for interface in plan.interfaces:
        for cidr in addresses_of(interface):
            network = ipaddress.ip_interface(cidr).network
            if network.version == route.via.version and route.via in network:
                return interface.name
    return None


def family_of(interface: Interface) -> tuple[str, ...]:
    """Which address families an interface configures, ``('ipv4', 'ipv6')``."""
    return tuple(
        _FAMILY[version]
        for version, config in ((4, interface.ipv4), (6, interface.ipv6))
        if config is not None and config.addresses
    )


def restricts_vlans(interface: Interface) -> bool:
    """Does this interface's ``vlan`` block say what the *port* will accept?

    The block means two different things depending on what it is attached to,
    and three dialects here have to tell them apart.

    On a port a frame arrives at — an ethernet port, a radio, a LAG, a bridge —
    it is 802.1Q port configuration: ``mode: trunk`` with a tagged set is a
    filter on what the port admits.

    On a ``vlan`` sub-interface it is the encapsulation VID, which every dialect
    here can write. On a ``tunnel`` interface it says which VLAN the overlay
    carries — a fact about the far end, not a filter the local netdev applies —
    so a dialect that cannot write it loses a description rather than a
    restriction, and records a skip instead of refusing.
    """
    return interface.vlan is not None and interface.type in _VLAN_ENFORCING_TYPES


def unwritable_vlan(plan: DevicePlan, interface: Interface) -> str:
    """Why a dialect with no 802.1Q port syntax cannot write this port, or ``""``.

    :func:`restricts_vlans` says the block is a filter. This says whether
    *dropping* it would change what the device does, which is the question a
    refusal turns on, and the answer is not the same for the two port modes.

    A **trunk** cannot be dropped. The port admits three tagged VLANs and a file
    that does not say so gives a port admitting every VLAN there is; that is a
    different device, so a dialect with no syntax for it must refuse.

    An **access** port usually can. "VLAN 10, untagged" describes which broadcast
    domain the wire is in — a fact about the network, not a knob on the host —
    and a plain interface with no VLAN configuration at all carries it exactly.
    Refusing here would refuse most ordinary Linux hosts for stating something a
    netplan file has no need to repeat.

    The exception is an access port enslaved to a bridge that also carries a
    *different* VLAN. That bridge has to filter for either port to behave as
    declared, and a dialect that cannot say ``VLANFiltering=yes`` and which port
    is in which VLAN would bridge two broadcast domains into one. That is the
    worst outcome in this whole module, so it is named specifically rather than
    folded into the general access case.
    """
    vlan = interface.vlan
    if vlan is None or not restricts_vlans(interface):
        return ""
    if vlan.mode is VlanMode.TRUNK:
        carried = compact_ids(vlan.vlan_ids())
        return (
            f"{interface.name} is a trunk carrying VLAN {carried}; without the tagged set the "
            f"port would admit every VLAN there is"
        )
    bridge = plan.enslaved_by(interface.name)
    if bridge is None:
        return ""
    others = sorted(_sibling_vlans(plan, bridge, interface.name) - vlan.vlan_ids())
    if not others:
        return ""
    return (
        f"{interface.name} is in VLAN {vlan.pvid} and the bridge {bridge.name} it is a member "
        f"of also carries VLAN {compact_ids(others)}; without VLAN filtering on the bridge "
        f"those broadcast domains would become one"
    )


def _sibling_vlans(plan: DevicePlan, bridge: Interface, exclude: str) -> frozenset[int]:
    """Every VLAN the *other* members of ``bridge`` declare."""
    carried: set[int] = set()
    for name in bridge.members or ():
        member = plan.interface(name)
        if name == exclude or member is None or member.vlan is None:
            continue
        carried.update(member.vlan.vlan_ids())
    return frozenset(carried)


#: The types on which a ``vlan`` block is a filter rather than a description.
_VLAN_ENFORCING_TYPES: Final[frozenset[InterfaceType]] = frozenset(
    {
        InterfaceType.ETHERNET,
        InterfaceType.WIFI,
        InterfaceType.LAG,
        InterfaceType.BRIDGE,
    }
)


def is_stacked(interface: Interface) -> bool:
    """Is this interface built by the host rather than plugged into it?

    A bridge, a bond, a VLAN sub-interface and a tunnel are created by whatever
    reads the generated file; an ethernet port, a radio and a loopback are
    already there. Every dialect here splits its output on that line — netplan
    into ``ethernets`` versus ``bridges``, networkd into ``.network`` versus
    ``.netdev`` — so the question is asked once.
    """
    return interface.type in {
        InterfaceType.BRIDGE,
        InterfaceType.LAG,
        InterfaceType.VLAN,
        InterfaceType.TUNNEL,
    }
