"""IP subnets derived from the addresses an inventory configures.

An inventory declares addresses on interfaces; nobody declares a *subnet*. Yet
three consumers need one and the same answer to "which prefixes exist, and who
sits in them?", and they must not disagree:

* ``netgraph list subnets`` prints it,
* the layer-3 graph (:mod:`netgraph.render.graph`) draws one node per prefix and
  joins every element to the prefixes it has an address in, and
* two validation rules are statements *about* that grouping — ``W105`` (a
  prefix with a single member) and ``W106`` (one address claimed twice inside
  one prefix).

Deriving it here, once, keeps a diagram, a listing and a finding from telling
three different stories about the same addressing plan.

Grouping is **by prefix and VRF**
---------------------------------

``10.0.0.1/24`` and ``10.0.0.2/24`` land in ``10.0.0.0/24`` whatever VLAN either
interface sits in. That is deliberate: a prefix is the unit an operator reasons
about when debugging reachability, and a routing table has no VLAN column. The
consequence — a prefix intentionally re-used in two broadcast domains appears
once — is what ``W106`` exists to point out.

A **VRF** is the one thing that does split it (§16.1). A routing instance is a
routing table of its own, so ``10.0.0.0/24`` in ``blue`` and ``10.0.0.0/24`` in
the global instance are two prefixes that share a spelling and nothing else:
they are two subnets here, they draw as two nodes, and an address in one is not
in conflict with the same address in the other. The instance an address is in
comes from ``interfaces[].vrf``; an interface that binds to none is in
:data:`~netgraph.models.GLOBAL_VRF`, which is what every address is in until
something says otherwise.

Loopback and link-local addresses are left out entirely (:func:`is_routable_address`):
they are scoped to one host or one link, so ``127.0.0.0/8`` is not a subnet of
this network and listing it once per machine would say nothing.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Container
from dataclasses import dataclass
from typing import Final, TypeAlias

from netgraph.loader.inventory import Inventory
from netgraph.models import GLOBAL_VRF, Adapter, Device, IPv4Address, IPv6Address

__all__ = [
    "GLOBAL_VRF",
    "SUBNET_ID_PREFIX",
    "AddressPlacement",
    "IPNetwork",
    "Subnet",
    "SubnetKey",
    "is_routable_address",
    "subnets_of",
]

#: Either family's prefix object, as :mod:`ipaddress` models it.
IPNetwork: TypeAlias = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Prefix of a subnet node's identity, e.g. ``subnet:10.0.0.0/24``. Lives here
#: rather than in the graph layer because :attr:`Subnet.node_id` is what mints
#: one: a colon cannot occur in an element's fully-qualified name (§4.1), so a
#: subnet node can never shadow a device.
SUBNET_ID_PREFIX: Final = "subnet:"

#: What makes two addresses members of one subnet: the routing instance and the
#: prefix. The VRF comes first because it is the coarser partition — a table,
#: then a prefix in it.
SubnetKey: TypeAlias = tuple[str, IPNetwork]

#: A prefix holding no more than this many addresses is a host route or a
#: point-to-point link: ``/30`` to ``/32``, and ``/126`` to ``/128``.
_POINT_TO_POINT_ADDRESSES = 4


def is_routable_address(address: IPv4Address | IPv6Address) -> bool:
    """Does this address say anything about where the element sits in the network?

    Loopback addresses are scoped to their own host (RFC 1122 §3.2.1.3, RFC 4291
    §2.5.3) and link-local addresses are scoped to one link, so neither
    identifies the element to a reader of a diagram, and neither places it in a
    subnet of *this* network.
    """
    return not (address.ip.is_loopback or address.ip.is_link_local)


@dataclass(frozen=True, slots=True)
class AddressPlacement:
    """One configured address, tied to the interface and element holding it."""

    #: Fully-qualified name of the device or adapter.
    element: str
    #: Interface name within that element.
    interface: str
    #: Position of the interface in ``spec.interfaces``, for a finding's field path.
    index: int
    #: The address in ``10.0.0.1/24`` form.
    address: str
    #: The address without its prefix length, which is what a clash is about.
    ip: str
    #: Every VLAN the interface is a member of; empty for a routed or untagged port.
    vlans: frozenset[int] = frozenset()
    #: ``dot1q:pvid`` of the interface, or ``None`` when it declares no ``vlan``
    #: block. This is the broadcast domain ``E004`` scopes a duplicate address to.
    scope: int | None = None
    #: The routing instance the interface is bound to (§16.1), or
    #: :data:`~netgraph.models.GLOBAL_VRF` for the global one.
    vrf: str = GLOBAL_VRF

    @property
    def port(self) -> str:
        """``element:interface``, the spelling every diagnostic uses."""
        return f"{self.element}:{self.interface}"

    @property
    def location(self) -> str:
        """``element:interface`` and the VRF, when it is not the global one."""
        return self.port if not self.vrf else f"{self.port} (vrf {self.vrf})"


@dataclass(frozen=True, slots=True)
class Subnet:
    """One IP prefix and every configured address inside it."""

    #: The prefix in ``10.0.0.0/24`` form. With :attr:`vrf`, the identity.
    prefix: str
    network: IPNetwork
    #: Placements in inventory load order, matching the order of the graph's nodes.
    members: tuple[AddressPlacement, ...] = ()
    #: The routing instance this prefix lives in (§16.1);
    #: :data:`~netgraph.models.GLOBAL_VRF` for the global one. Two subnets may
    #: share a :attr:`prefix` when they are in different instances.
    vrf: str = GLOBAL_VRF

    @property
    def key(self) -> SubnetKey:
        """What identifies this subnet: its instance and its prefix."""
        return (self.vrf, self.network)

    @property
    def node_id(self) -> str:
        """Identity of the graph node standing for this subnet.

        ``subnet:10.0.0.0/24`` in the global instance and
        ``subnet:blue/10.0.0.0/24`` in a VRF. The instance is left out of the
        global one so that every rendering of an inventory without a VRF in it —
        which is nearly all of them — keeps the identity it had before instances
        existed, and so that a golden file does not move under a feature nobody
        used. A VRF name cannot hold a ``/`` (``ElementName``), so the two forms
        cannot collide.
        """
        return (
            f"{SUBNET_ID_PREFIX}{self.prefix}"
            if not self.vrf
            else (f"{SUBNET_ID_PREFIX}{self.vrf}/{self.prefix}")
        )

    @property
    def label(self) -> str:
        """How the subnet is written for a reader: ``10.0.0.0/24 (vrf blue)``.

        The global instance is left unqualified. Nearly every inventory has only
        that one, and ``10.0.0.0/24 (vrf )`` would be noise on every node of
        every layer-3 diagram ever drawn.
        """
        return self.prefix if not self.vrf else f"{self.prefix} (vrf {self.vrf})"

    @property
    def version(self) -> int:
        """4 or 6."""
        return self.network.version

    @property
    def family(self) -> str:
        """``ipv4`` or ``ipv6``, the schema's spelling."""
        return f"ipv{self.network.version}"

    @property
    def elements(self) -> tuple[str, ...]:
        """The elements holding an address here, without repeats, in member order."""
        return tuple(dict.fromkeys(member.element for member in self.members))

    @property
    def addresses(self) -> tuple[str, ...]:
        """Every address in the prefix, in member order, repeats included."""
        return tuple(member.address for member in self.members)

    @property
    def vlans(self) -> frozenset[int]:
        """Every VLAN an interface in this prefix is a member of."""
        return (
            frozenset().union(*(member.vlans for member in self.members))
            if self.members
            else frozenset()
        )

    @property
    def is_point_to_point(self) -> bool:
        """Can the prefix hold at most two hosts?

        That is a ``/30`` to ``/32``, or a ``/126`` to ``/128``.

        Such a prefix is *expected* to look under-populated in an inventory: a
        host route holds one address by definition, and the far end of an ISP
        handoff is not a device anybody declares here. Rules about how populated
        a subnet is exempt them.
        """
        return self.network.num_addresses <= _POINT_TO_POINT_ADDRESSES

    @property
    def sort_key(self) -> tuple[str, int, int, int]:
        """Order by routing instance, then family, network address, prefix length.

        The instance leads so that the global table is listed whole before any
        VRF — ``GLOBAL_VRF`` is the empty string, which sorts before every
        declarable name — and an inventory with no VRF at all keeps exactly the
        order it had before instances existed.

        The integer form of the network address keeps the key comparable across
        families, which a mix of :class:`ipaddress.IPv4Address` and
        :class:`ipaddress.IPv6Address` objects would not be.
        """
        return (
            self.vrf,
            self.network.version,
            int(self.network.network_address),
            self.network.prefixlen,
        )

    def restricted_to(self, elements: Container[str]) -> Subnet:
        """The same prefix with only the members held by ``elements``.

        Filtering a graph removes elements; a subnet node that kept listing them
        would report members the reader cannot see.
        """
        return Subnet(
            prefix=self.prefix,
            network=self.network,
            members=tuple(member for member in self.members if member.element in elements),
            vrf=self.vrf,
        )


def subnets_of(inventory: Inventory) -> tuple[Subnet, ...]:
    """Every prefix an address of ``inventory`` sits in.

    Ordered by routing instance, then address family, then network address, then
    prefix length, so two overlapping prefixes (``10.0.0.0/16`` and
    ``10.0.0.0/24``) keep a stable relative order. Within a subnet, members
    follow inventory load order — the same order the graph lists its nodes in, so
    a subnet's member list and the nodes of a rendering never disagree about
    sequence.

    Returns:
        One :class:`Subnet` per distinct ``(vrf, prefix)`` pair. Loopback and
        link-local addresses are excluded, so a prefix appears only if something
        is addressed in it.
    """
    # Keyed by the network *object* rather than by its text: equal prefixes
    # compare and hash equal, so the grouping is the same one string keys give,
    # but a prefix is rendered once instead of once per address in it. On a tree
    # with 2100 addresses in 90 prefixes that is 90 renderings, not 2100.
    members: dict[SubnetKey, list[AddressPlacement]] = {}

    for fqn, element in inventory.elements.items():
        # ``interface_owners`` would put every adapter after every device;
        # iterating the element map keeps load order instead.
        if not isinstance(element, (Device, Adapter)):
            continue
        for index, interface in enumerate(element.interfaces):
            vlan = interface.vlan
            vlans = vlan.vlan_ids() if vlan is not None else frozenset()
            scope = vlan.pvid if vlan is not None else None
            # An adapter has no VRF table (§16.1), so its addresses are always
            # in the global instance; ``Interface.vrf`` is ``None`` there.
            vrf = interface.vrf or GLOBAL_VRF
            for address in interface.addresses():
                if not is_routable_address(address):
                    continue
                # ``str(address)`` is ``f"{ip}/{prefix_length}"``, so rendering
                # the address a second time would render the same IPv6 literal
                # twice -- compression and all -- for one placement.
                ip_text = str(address.ip)
                members.setdefault((vrf, address.network), []).append(
                    AddressPlacement(
                        element=fqn,
                        interface=interface.name,
                        index=index,
                        address=f"{ip_text}/{address.prefix_length}",
                        ip=ip_text,
                        vlans=vlans,
                        scope=scope,
                        vrf=vrf,
                    )
                )

    subnets = [
        Subnet(prefix=str(network), network=network, members=tuple(placements), vrf=vrf)
        for (vrf, network), placements in members.items()
    ]
    subnets.sort(key=lambda subnet: subnet.sort_key)
    return tuple(subnets)
