"""The renderer-facing view of an inventory: nodes, edges and VLAN membership.

Every renderer consumes a :class:`Graph` rather than an
:class:`~netviz.loader.inventory.Inventory`. The indirection buys three
things:

* **One resolution pass.** Cable endpoints are resolved to fully-qualified names
  exactly once, here, so DOT, Mermaid and JSON cannot disagree about what is
  connected to what.
* **Filtering is a graph operation.** ``--vlan``, ``--neighbors-of`` and friends
  narrow the graph (:func:`filter_graph`); the renderers stay unaware of them.
* **Renderers never see a broken reference.** A cable whose endpoint does not
  resolve is dropped and recorded in :attr:`Graph.dangling`. Normally the
  validator has already refused such an inventory (``E001``); the graph layer
  still has to cope because ``--force`` exists.

VLAN membership (§9.3)
----------------------

An untagged host port carries no ``vlan`` block, so asking "which elements are
in VLAN 10?" cannot be answered from the ports alone — the host would drop out
of its own broadcast domain. Membership is therefore computed in two passes:
each link derives the VLANs it carries from its two ends
(:func:`_link_vlans`), and each node then unions its own ports, its VLAN
database and the links it terminates. ``netviz render --vlan 10`` keeps the
host attached to an access port in VLAN 10, which is what a reader means.

The layers
----------

:class:`Layer` picks *which graph* is built. ``physical``, ``l1`` and ``l2``
share one topology; ``l3``, ``overlay`` and ``rack`` each build a different one:

``physical``
    Everything ``l1`` draws, plus the passive cross-connects: a patch panel is
    a node and the two cable segments either side of it are two edges. This is
    the cabling record — what a technician would find in the rack.
``l1`
    The physical topology: one node per device and adapter, one edge per cable
    and per adapter attachment, annotated with medium, rate and cable labels.
    Tunnels are drawn too, as logical edges over that topology — a tunnel is a
    declared element, and a diagram that silently left it out would be wrong by
    omission — but they are told apart by :class:`EdgeKind`, so a consumer
    asking "what could a technician unplug?" still gets the cables alone.
``l2``
    The same nodes and edges, annotated with VLAN membership instead. A
    layer-2 tunnel (VXLAN, Geneve, L2TP) extends a broadcast domain across the
    underlay, so it carries VLANs exactly as a trunk does; a layer-3 tunnel
    carries none.
``l3``
    A different graph. Nodes are the elements that hold a routable address
    *plus one node per IP prefix* (:mod:`netviz.subnets`); edges join an
    element to every prefix it has an address in. Cables do not appear — two
    devices are adjacent at layer 3 because they share a subnet, not because a
    cable happens to run between them. Elements with no routable address are
    left out rather than drawn floating, and loopback, link-local and
    unnumbered interfaces contribute nothing. A tunnel needs no special case
    here: the addresses on its ``tunnel`` interfaces put both ends in one
    prefix, which is exactly what the overlay *is* at layer 3.
``overlay``
    The tunnels themselves. Every tunnel becomes a node, joined to each element
    it terminates on and — this is why it is a node — to the tunnel it is
    encapsulated in. That edge is what makes ``VXLAN over IPsec`` drawable:
    nesting is a relation between two links, and a link cannot end on a link.
``routing``
    The control plane (§16.8). Nodes are the elements that take part in routing,
    labelled with the AS and router id their peers know them by; edges are the
    BGP sessions they declare and the OSPF adjacencies their addressing implies,
    and nodes are grouped into one cluster per VRF. Nothing physical appears: two
    routers are adjacent here because they exchange routes, which a cable neither
    guarantees nor is needed for.
``rack``
    Not a topology at all: one node per rack named by a ``metadata.location``,
    carrying the elevation of that rack — which unit each element occupies, and
    which units are empty. There are no edges, because a cable between two
    boxes says nothing about where the boxes are bolted.

Splicing a patch panel
----------------------

A panel is electrically transparent, so the same inventory has two honest
readings and :func:`build_graph` offers both. The *physical* one keeps the panel
and its segments; every other layer replaces a run
``switch → front/7 ⇄ rear/7 → server`` with the single edge ``switch → server``
it is indistinguishable from. The spliced edge remembers what it crossed in
:attr:`Edge.patch`, which is how ``netviz path`` can name the panel as a
pass-through without the panel being a hop.
"""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Container, Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeAlias

from netviz.annotations import AnnotationSet, annotations_for_view, area_members, note_anchor
from netviz.errors import count_text
from netviz.identity import identities, identity_plan
from netviz.layout.geometry import Geometry, Placement
from netviz.layout.resolve import resolve_geometry
from netviz.loader.inventory import Inventory, SourceLocation, namespace_of, short_name
from netviz.models import (
    GROUP_KIND,
    LOCAL_ZONE,
    PATCHPANEL_KIND,
    PDU_KIND,
    ROOT_NETNS,
    USER_KIND,
    Adapter,
    Cable,
    Device,
    FirewallRule,
    Group,
    Interface,
    Medium,
    NatRule,
    PatchPanel,
    Pdu,
    Style,
    Tunnel,
    TunnelType,
    User,
    format_bitrate,
    netns_path,
)
from netviz.models.metadata import Location
from netviz.power import Feed, PowerNode, PowerPlan, power_plan
from netviz.subnets import (
    SUBNET_ID_PREFIX,
    AddressPlacement,
    Subnet,
    is_routable_address,
    subnets_of,
)

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    # :mod:`netviz.render.aggregate` consumes this module, so the dependency
    # can only run one way at import time. The two annotations below are the
    # whole of what points back, and ``from __future__ import annotations``
    # keeps them strings.
    from netviz.render.aggregate import AggregateView, BundleView

__all__ = [
    "GROUP_KIND",
    "NETNS_ID_PREFIX",
    "NETNS_KIND",
    "NODE_KINDS",
    "PATCHPANEL_KIND",
    "PDU_KIND",
    "RACK_ID_PREFIX",
    "RACK_KIND",
    "SUBNET_ID_PREFIX",
    "SUBNET_KIND",
    "TUNNEL_ID_PREFIX",
    "TUNNEL_KIND",
    "USER_KIND",
    "ZONE_ID_PREFIX",
    "ZONE_KIND",
    "AdjacencyView",
    "Edge",
    "EdgeKind",
    "FilterSpec",
    "Graph",
    "Layer",
    "NetnsView",
    "Node",
    "NodeType",
    "PatchHop",
    "PatchView",
    "PolicyView",
    "PortView",
    "RackSlot",
    "RackView",
    "RoutingView",
    "SecurityView",
    "Subnet",
    "TunnelEnd",
    "TunnelView",
    "WirelessView",
    "build_graph",
    "filter_graph",
    "is_routable_address",
    "netns_node_id",
    "netns_views",
    "rack_elevations",
    "resolve_tunnels",
    "security_views",
    "splice_patch_panels",
    "zone_node_id",
]


class Layer(str, Enum):
    """Which view of the network a rendering shows."""

    #: Cabling: what is plugged into what, patch panels included.
    PHYSICAL = "physical"
    #: Physical: what is plugged into what, with medium, speed and cable labels.
    #: Passive cross-connects are spliced out; see the module docstring.
    L1 = "l1"
    #: Logical: the same topology annotated with VLAN membership and port mode.
    L2 = "l2"
    #: Routed: IP prefixes as nodes, joined to the elements addressed in them.
    L3 = "l3"
    #: Encapsulation: tunnels as nodes, joined to their endpoints and to the
    #: tunnels they run inside (§14).
    OVERLAY = "overlay"
    #: Control plane: the routers, joined by the BGP sessions and OSPF
    #: adjacencies they declare, clustered by VRF (§16.8).
    ROUTING = "routing"
    #: Placement: one node per rack, holding its front elevation (§3.2).
    RACK = "rack"
    #: Power: the PDUs and everything they feed, joined by outlet and PoE feeds
    #: (§17.5). A different graph again — a power path is not a data path.
    POWER = "power"
    #: Identity: the users and groups, joined by membership (§19.3). Not a
    #: network at all — it answers "who", which no other view can.
    IDENTITY = "identity"
    #: Namespaces: the network stacks *inside* each machine, nested as they are
    #: declared and joined by the veth pairs that cross between them (§23.3).
    #: The one view that looks below the device: everywhere else a host is one
    #: box, and a container host drawn as one box says nothing about the dozen
    #: stacks it is actually running.
    NETNS = "netns"
    #: Policy: the security zones every filtering device divides itself into,
    #: joined by the traffic its firewall permits between them (§24.5). The one
    #: view that draws a *decision* rather than a path — everywhere else an edge
    #: means "these two can reach each other", and here it means "and this is
    #: what is allowed to cross".
    SECURITY = "security"

    def __str__(self) -> str:
        return self.value

    @property
    def shows_panels(self) -> bool:
        """Does this layer draw a passive cross-connect as a node of its own?"""
        return self is Layer.PHYSICAL

    @property
    def builds_own_nodes(self) -> bool:
        """Does this layer replace the topology's node set with one of its own?

        ``rack``, ``power``, ``identity`` and ``security`` all do. The first two need the panels
        left in the map they are built from: a panel occupies rack units, and a
        run to a PoE device crosses one. Splicing panels out of a graph nobody
        draws would cost nothing but the walk that finds the switch at the far
        end. ``identity`` draws no hardware whatsoever.
        """
        return self in (Layer.RACK, Layer.POWER, Layer.IDENTITY, Layer.SECURITY)


class NodeType(str, Enum):
    """What a node stands for.

    Layer 1 and layer 2 hold elements and — where a tunnel joins more than two
    of them — tunnels; layer 3 and the overlay view mix element nodes with
    derived ones, so a consumer of a rendering needs to be able to tell them
    apart without pattern-matching on names.
    """

    #: A device or an adapter: hardware the inventory declares.
    ELEMENT = "element"
    #: An IP prefix, derived from the addresses of the elements (layer 3 only).
    SUBNET = "subnet"
    #: A ``tunnel`` document drawn as a node rather than as an edge, because it
    #: joins more than two endpoints or because something is nested inside it.
    TUNNEL = "tunnel"
    #: A rack named by a ``metadata.location``, holding its elevation. Derived:
    #: no document declares a rack, the elements in it do.
    RACK = "rack"
    #: A whole namespace, collapsed into one box by
    #: :func:`~netviz.render.aggregate.collapse_namespaces`. It stands for
    #: elements the diagram no longer draws, so a consumer must be able to tell
    #: it from the single device it otherwise looks like.
    AGGREGATE = "aggregate"
    #: One network namespace *inside* a device (§23). Derived: the device
    #: declares it, but the node standing for it is minted by the netns view,
    #: which is the only layer that draws below the machine.
    NETNS = "netns"
    #: One security zone of one device (§24.5). Derived in the same sense a
    #: netns node is: the device declares the zone, and the node standing for it
    #: is minted by the security view.
    ZONE = "zone"

    def __str__(self) -> str:
        return self.value


#: ``kind`` reported for a subnet node. The nine element kinds of §3 are all
#: taken, so this one cannot collide with a declared kind.
SUBNET_KIND: Final = "subnet"

#: ``kind`` reported for a rack node, and the prefix of its identity. The same
#: reasoning as :data:`SUBNET_KIND` and :data:`SUBNET_ID_PREFIX`: no element
#: kind is called ``rack``, and a colon keeps the id out of the name grammar.
RACK_KIND: Final = "rack"
RACK_ID_PREFIX: Final = "rack:"

#: Prefix of a subnet node's identity, e.g. ``subnet:10.0.0.0/24``. Defined by
#: :mod:`netviz.subnets`, beside the :attr:`Subnet.node_id` that mints one, and
#: re-exported here because every consumer of the graph reads it from this module.

#: ``kind`` reported for a tunnel node. It is also the declared ``kind`` of the
#: document it stands for, unlike :data:`SUBNET_KIND`: a tunnel node *is* the
#: element, drawn as a node instead of as an edge.
TUNNEL_KIND: Final = "tunnel"

#: Prefix of a tunnel node's identity, e.g. ``tunnel:sites/hq/vx-100``. The same
#: reasoning as :data:`SUBNET_ID_PREFIX`: a colon keeps it out of the namespace
#: of declared names, so the node and the element it stands for stay distinct
#: even in a graph that holds both.
TUNNEL_ID_PREFIX: Final = "tunnel:"

#: ``kind`` reported for a network-namespace node, and the prefix of its
#: identity: ``netns:hosts/srv-01:blue`` is the namespace ``blue`` on
#: ``hosts/srv-01``. Two colons rather than one because both halves need to be
#: recoverable and neither an element name nor a namespace name may contain one.
NETNS_KIND: Final = "netns"
NETNS_ID_PREFIX: Final = "netns:"


def netns_node_id(element: str, namespace: str) -> str:
    """Identity of the node standing for ``namespace`` on ``element`` (§23.3).

    The device's own fqn for its initial namespace: that node *is* the machine —
    it keeps the element, the icon, the source link and the geometry — and
    minting a second identity for it would mean a stored arrangement of the
    netns view could not be shared with any other.
    """
    return f"{NETNS_ID_PREFIX}{element}:{namespace}" if namespace else element


#: ``kind`` reported for a security-zone node, and the prefix of its identity.
#: The same shape as :data:`NETNS_KIND` and for the same reason: a zone belongs
#: to one device, so both halves have to be recoverable from the identity, and
#: ``zone:sites/hq/fw-edge:dmz`` is the zone ``dmz`` on ``sites/hq/fw-edge``.
ZONE_KIND: Final = "zone"
ZONE_ID_PREFIX: Final = "zone:"

#: The zone a rule is in when it names none. Not a value any document may write
#: -- ``src_zone`` is simply absent -- so it lives here rather than in the model:
#: it is a *drawing* of the absence, and the drawing needs somewhere to put the
#: line. Chosen so it cannot collide with a declared zone, which ``ElementName``
#: keeps to the same grammar ``local`` is in and ``NV-B001`` keeps off ``local``.
ANY_ZONE: Final = "any"


#: How far a derived stack node is placed from the machine it runs in, in
#: Graphviz points. Right by a little under a node's width and down by a little
#: over its height, so a host's containers form a readable column beside it
#: rather than landing on top of it or on each other. See :func:`_placed_stacks`.
STACK_OFFSET_X: Final = 90.0
STACK_OFFSET_Y: Final = -70.0


def zone_node_id(element: str, zone: str) -> str:
    """Identity of the node standing for ``zone`` on ``element`` (§24.5).

    Unlike :func:`netns_node_id` there is no case that answers the element's own
    fqn. A device is not one of its zones — ``local`` is the traffic that
    terminates on it, which is a fourth thing beside the zones and the box — so
    every zone node is minted, including the two nobody declares.
    """
    return f"{ZONE_ID_PREFIX}{element}:{zone}"


class EdgeKind(str, Enum):
    """Why two nodes are joined."""

    #: A ``cable`` document (§7).
    CABLE = "cable"
    #: An adapter's ``upstream.attached_to`` reference (§8.2).
    ATTACHMENT = "attachment"
    #: An element has an address in a subnet (layer 3 only).
    SUBNET = "subnet"
    #: A ``tunnel`` document (§14): either the whole point-to-point tunnel, or
    #: one endpoint's leg of a tunnel drawn as a node.
    TUNNEL = "tunnel"
    #: A tunnel's ``over``: this tunnel is carried inside that one (§14.3).
    ENCAPSULATION = "encapsulation"
    #: A BGP session one of the two ends declares (§16.6).
    BGP = "bgp"
    #: An OSPF adjacency: two routers in one area, on one link.
    OSPF = "ospf"
    #: One entry of a group's ``spec.members`` (§19.3): this group holds that
    #: identity. Directed in the only sense an undirected edge can be — source is
    #: the group, target the member — because "who is in what" is the question
    #: the view exists to answer.
    MEMBERSHIP = "membership"
    #: A cord from a PDU outlet to a power supply (§17.5).
    OUTLET = "outlet"
    #: Power over the uplink: a PSE port feeding a device that declares
    #: ``powered_by: poe``. Drawn distinctly from an outlet feed because it is a
    #: different kind of fact — nobody can unplug it without unplugging the data.
    POE = "poe"
    #: A veth pair: two ``ethernet`` interfaces of one machine naming each other
    #: with ``peer`` (§23.2). Drawn between the namespaces their ends are in,
    #: which is the whole point of one.
    VETH = "veth"
    #: A namespace's ``parent``: that stack was created from inside this one
    #: (§23.1). Directed in the only sense an undirected edge can be — source is
    #: the containing namespace — because nesting is what the edge says.
    NESTING = "nesting"
    #: What a device's firewall does to traffic from one zone to another (§24.5).
    #: Directed, and meant that way: policy is asymmetric — *lan to wan* is a
    #: different statement from *wan to lan*, and a firewall that treated them as
    #: one would not be a firewall.
    POLICY = "policy"

    def __str__(self) -> str:
        return self.value

    @property
    def is_power(self) -> bool:
        """Is this a power feed rather than a data path?"""
        return self in (EdgeKind.OUTLET, EdgeKind.POE)


@dataclass(frozen=True, slots=True)
class PortView:
    """One interface, flattened to what a renderer or exporter needs."""

    name: str
    type: str
    enabled: bool = True
    description: str | None = None
    mac: str | None = None
    mtu: int | None = None
    #: Addresses in ``10.0.0.1/24`` form, IPv4 first.
    addresses: tuple[str, ...] = ()
    #: The subset of :attr:`addresses` that identifies the element *on the
    #: network*: loopback and link-local addresses removed. Every host declares
    #: ``127.0.0.1`` and ``::1``, so printing them on a diagram would push the
    #: addresses a reader is actually looking for off the label.
    routable_addresses: tuple[str, ...] = ()
    #: ``access`` / ``trunk``, or ``None`` for a port with no ``vlan`` block.
    vlan_mode: str | None = None
    #: Every VLAN the port is a member of, native VLAN included (§9.3).
    vlans: frozenset[int] = frozenset()
    #: The network namespace the port is in, ``""`` for the machine's initial
    #: one (§23.1). Carried on every port, at every layer: which stack an
    #: address is in is a fact about the address, so a tooltip that named the
    #: address without it would be naming an address that may not be unique.
    netns: str = ""
    #: The other end of the veth pair this port is one end of, or ``None``
    #: (§23.2). An interface name on the *same* element, always.
    peer: str | None = None
    #: The VRF the port is bound to, or ``None`` for the global table (§16.2).
    #: Carried here rather than looked up out of ``spec.vrfs`` because the
    #: binding is a fact about the *port* — "is this address in a VRF" is a
    #: question asked of an interface, by the selector language and by a
    #: tooltip, and neither should have to re-read the device to answer it.
    vrf: str | None = None

    @classmethod
    def of(cls, interface: Interface) -> PortView:
        vlan = interface.vlan
        addresses = interface.addresses()
        return cls(
            name=interface.name,
            type=interface.type.value,
            enabled=interface.enabled,
            description=interface.description,
            mac=interface.mac,
            mtu=interface.mtu,
            addresses=tuple(str(address) for address in addresses),
            routable_addresses=tuple(
                str(address) for address in addresses if is_routable_address(address)
            ),
            vlan_mode=vlan.mode.value if vlan is not None else None,
            vlans=vlan.vlan_ids() if vlan is not None else frozenset(),
            netns=interface.netns_name,
            peer=interface.peer,
            vrf=interface.vrf,
        )

    @property
    def is_veth(self) -> bool:
        """Is this port one end of a veth pair (§23.2)?"""
        return self.peer is not None


@dataclass(frozen=True, slots=True)
class TunnelEnd:
    """One end of a tunnel, resolved against the inventory."""

    #: Fully-qualified name of the element the tunnel terminates on.
    element: str
    #: The ``tunnel`` interface on it, e.g. ``wg0``.
    interface: str

    @property
    def element_name(self) -> str:
        """The element's short name, for a label with no room for a namespace."""
        return short_name(self.element)

    def __str__(self) -> str:
        return f"{self.element}:{self.interface}"


@dataclass(frozen=True, slots=True)
class TunnelView:
    """One ``tunnel`` document, resolved and placed in its encapsulation stack.

    Resolution happens once, here, for the same reason cable endpoints do: the
    DOT, Mermaid and JSON renderings of one inventory must agree about what runs
    inside what.
    """

    #: Fully-qualified name of the ``tunnel`` document.
    fqn: str
    name: str
    namespace: str
    tunnel: Tunnel
    #: The endpoints that resolved, in the document's canonical (sorted) order.
    ends: tuple[TunnelEnd, ...] = ()
    #: Fully-qualified name of the tunnel this one runs inside, when ``over``
    #: resolved to one.
    over: str | None = None
    #: How many tunnels this one is nested inside; 0 runs on the underlay
    #: network itself.
    depth: int = 0
    #: The encapsulation stack, this tunnel's type first, then each underlay
    #: outwards: ``("vxlan", "ipsec")`` for VXLAN over IPsec.
    stack: tuple[str, ...] = ()
    #: The nearest underlay tunnel that encrypts, when this one does not. A
    #: VXLAN inside an IPsec tunnel is confidential even though VXLAN encrypts
    #: nothing, which is exactly why nesting is worth drawing.
    encrypted_by: str | None = None

    @property
    def id(self) -> str:
        """Identity of the node standing for this tunnel."""
        return f"{TUNNEL_ID_PREFIX}{self.fqn}"

    @property
    def type(self) -> str:
        """``wireguard``, ``ipsec``, ``vxlan`` … (§14.1)."""
        return self.tunnel.spec.type.value

    @property
    def layer(self) -> int:
        """2 when the tunnel carries frames, 3 when it carries packets."""
        return self.tunnel.spec.type.layer

    @property
    def encrypted(self) -> bool:
        """Is the payload protected by the tunnel itself?"""
        return self.tunnel.encrypts

    @property
    def protected(self) -> bool:
        """Is the payload protected by *something* — this tunnel or an underlay?"""
        return self.encrypted or self.encrypted_by is not None

    @property
    def vni(self) -> int | None:
        return self.tunnel.spec.vni

    @property
    def mtu(self) -> int | None:
        return self.tunnel.spec.mtu

    @property
    def label(self) -> str | None:
        return self.tunnel.spec.label

    @property
    def is_multipoint(self) -> bool:
        """Does the tunnel join more than two endpoints?"""
        return len(self.ends) > 2

    @property
    def elements(self) -> tuple[str, ...]:
        """The elements the tunnel terminates on, without repeats, in end order."""
        return tuple(dict.fromkeys(end.element for end in self.ends))

    @property
    def overhead_bytes(self) -> int:
        """Encapsulation cost of the whole stack, in bytes over IPv4.

        A VXLAN over IPsec costs both headers, and an overlay MTU has to fit
        inside what is left; ``W126`` is the rule that says so.
        """
        return sum(TunnelType(name).overhead_bytes for name in self.stack)

    @property
    def stack_text(self) -> str:
        """The stack as a reader says it out loud: ``vxlan over ipsec``."""
        return " over ".join(self.stack) if self.stack else self.type

    @property
    def summary(self) -> str:
        """One line naming the type, the VNI and what protects it."""
        parts = [self.stack_text]
        if self.vni is not None:
            parts.append(f"vni {self.vni}")
        if not self.encrypted:
            parts.append("encrypted underlay" if self.encrypted_by else "cleartext")
        return ", ".join(parts)

    def restricted_to(self, elements: Container[str]) -> TunnelView:
        """The same tunnel with only the ends held by ``elements``.

        Filtering a graph removes elements; a tunnel node that kept listing them
        would report endpoints the reader cannot see.
        """
        return replace(self, ends=tuple(end for end in self.ends if end.element in elements))


@dataclass(frozen=True, slots=True)
class NetnsView:
    """One network namespace of one machine, resolved (§23.3).

    Minted for every namespace the netns layer draws, the machine's initial one
    included — that one is carried on the *element* node rather than on a node of
    its own, so a reader looking at ``srv-01`` sees which of its interfaces never
    left the machine's own stack.

    Flattened here, once, for the reason :class:`RoutingView` is: four renderers
    draw this view and none of them may disagree about which stack an address is
    in.
    """

    #: Fully-qualified name of the machine the namespace is on.
    element: str
    #: ``spec.netns[].name``, or ``""`` for the machine's initial namespace.
    name: str = ""
    #: The chain from the initial namespace down to this one, outermost first;
    #: ``()`` for the initial namespace. ``("blue", "web")`` is ``web`` nested
    #: inside ``blue``, which is what makes nesting readable at a glance.
    path: tuple[str, ...] = ()
    #: The namespace this one was created inside, ``""`` for the initial one and
    #: for anything directly in it.
    parent: str = ""
    #: The interfaces in this namespace, in declaration order.
    ports: tuple[PortView, ...] = ()
    description: str | None = None
    #: The machine's own document. Carried because a stack node stands for
    #: something the machine declares and not for a document of its own, yet two
    #: consumers still have to ask the machine a question about it: ``--kind
    #: server`` has to select the containers of a server, and a routed trace has
    #: to know whether the machine forwards before it will pass *through* a
    #: stack (§6.1.1). Both would otherwise need the machine's node to be in the
    #: same graph, and at layer 3 it need not be — a bridging host with no
    #: address of its own is absent while its containers are drawn.
    device: Device | None = None

    @property
    def id(self) -> str:
        """Identity of the node standing for this namespace."""
        return netns_node_id(self.element, self.name)

    @property
    def owner_kind(self) -> str:
        """``kind`` of the machine running this stack, or ``""`` if unresolved.

        A stack has no kind of its own — it is drawn as a namespace, not as
        hardware — so this is what a ``--kind`` filter answers from.
        """
        return self.device.kind if self.device is not None else ""

    @property
    def is_root(self) -> bool:
        """Is this the machine's initial namespace rather than a declared one?"""
        return not self.name

    @property
    def depth(self) -> int:
        """How deeply the namespace is nested; ``0`` for the initial one."""
        return len(self.path)

    @property
    def label(self) -> str:
        """``blue/web`` — the namespace as a reader spells its containment."""
        return "/".join(self.path) if self.path else short_name(self.element)

    @property
    def qualified_label(self) -> str:
        """``srv-host-a/blue`` — the stack named from *outside* its machine.

        :attr:`label` names a stack from inside the box the netns view draws
        around each machine, where the machine's own name is on the box. Layer 3
        draws no such box — a prefix spans machines, so grouping its members by
        the hardware they run on would be a second grouping fighting the first —
        so a stack drawn there has to carry the machine's name itself, or two
        containers called ``blue`` on two hosts would be two nodes labelled the
        same.
        """
        return (
            short_name(self.element) if self.is_root else f"{short_name(self.element)}/{self.label}"
        )

    @property
    def addresses(self) -> tuple[str, ...]:
        """Every routable address configured in this stack, in interface order."""
        return tuple(address for port in self.ports for address in port.routable_addresses)

    @property
    def veth_ends(self) -> tuple[PortView, ...]:
        """The ports of this namespace that are one end of a veth pair."""
        return tuple(port for port in self.ports if port.is_veth)


@dataclass(frozen=True, slots=True)
class RoutingView:
    """What one element contributes to the routing layer (§16.8).

    Flattened out of ``spec.routing``, ``spec.vrfs``, ``spec.route_tables``,
    ``spec.routes`` and ``spec.routing_policy`` once, here, so that the DOT,
    Mermaid, JSON and HTML renderings of one inventory cannot disagree about
    which AS a router is in, which area a link is in, or which table a packet
    from a given prefix is routed by.
    """

    #: Fully-qualified name of the element.
    element: str
    asn: int | None = None
    #: The router id, as text; the OSPF one when the two differ, since that is
    #: the identity an adjacency is refused over (RFC 2328 §10.5).
    router_id: str | None = None
    #: The OSPF area the device runs, when it runs one.
    area: str | None = None
    #: The interfaces OSPF runs on, in declaration order.
    ospf_interfaces: tuple[str, ...] = ()
    #: ``(name, rd)`` per declared VRF, in declaration order.
    vrfs: tuple[tuple[str, str], ...] = ()
    #: The instances an interface is actually bound to, in interface order. A
    #: declared VRF nothing is bound to holds no address (``NV-F014``), so it is
    #: not an instance this router takes part in and cannot group it.
    bound_vrfs: tuple[str, ...] = ()
    #: Every static route the device holds, already rendered (``0.0.0.0/0 via
    #: 203.0.113.1 dev wan0``), in declaration order.
    routes: tuple[str, ...] = ()
    #: ``(name, id)`` per declared routing table, in declaration order (§16.3).
    tables: tuple[tuple[str, int], ...] = ()
    #: The policy database, already rendered (``100: from 10.20.0.0/16 lookup
    #: uplink-b``), **in priority order** — which is the order the device walks
    #: it, and therefore the only order in which the list means anything.
    policy: tuple[str, ...] = ()

    @property
    def speaks_bgp(self) -> bool:
        return self.asn is not None

    @property
    def speaks_ospf(self) -> bool:
        return self.area is not None

    @property
    def routes_by_policy(self) -> bool:
        """Does the element route by anything other than the destination (§16.4)?"""
        return bool(self.policy)

    @property
    def is_empty(self) -> bool:
        """Does the element take part in no routing at all?"""
        return not (
            self.speaks_bgp
            or self.speaks_ospf
            or self.routes
            or self.vrfs
            or self.tables
            or self.policy
        )

    @property
    def asn_text(self) -> str | None:
        """``AS 65001``, the way a diagram labels an autonomous system."""
        return None if self.asn is None else f"AS {self.asn}"

    def describe(self) -> tuple[str, ...]:
        """The lines a node label carries: what this router *is*, routing-wise."""
        lines = [text for text in (self.asn_text,) if text]
        if self.router_id is not None:
            lines.append(f"id {self.router_id}")
        if self.area is not None:
            lines.append(f"ospf area {self.area}")
        if self.policy:
            # Counted rather than listed: a policy database is as long as it
            # needs to be, and a node label is not. The rules themselves are on
            # the tooltip and in the JSON, which is where a reader who wants
            # them is looking.
            lines.append(count_text(len(self.policy), "policy rule"))
        return tuple(lines)


@dataclass(frozen=True, slots=True)
class SecurityView:
    """One security zone of one device, flattened for the renderers (§24.5).

    Built once, here, so that the DOT, Mermaid, JSON and HTML drawings of one
    inventory cannot disagree about which interfaces are in a zone or how many
    rules mention it.

    The two zones nobody declares get a view like any other. ``local`` is the
    machine itself — the traffic that terminates on it rather than crossing it —
    and ``any`` stands for a rule that left a zone unset, which means "wherever
    the packet came from". Both are minted only when the policy names them, so a
    device whose every rule states both zones draws neither.
    """

    #: The device this zone belongs to. A zone never spans two.
    element: str
    #: The zone's name, as a rule spells it.
    name: str
    #: The kind of that device — ``firewall``, ``router``, ``server``. Carried
    #: rather than looked up, because the device is not a node of this graph and
    #: ``--kind`` still has to be able to reach the zones on one.
    owner_kind: str = ""
    #: The interfaces in it, in declaration order. Empty for ``local``, for
    #: ``any``, and for a declared zone nothing has been put in yet (``W150``).
    interfaces: tuple[str, ...] = ()
    #: What the device declares about it, when it declares anything.
    description: str | None = None
    #: How many filter rules mention this zone, in either direction.
    rules: int = 0
    #: How many address translations do.
    translations: int = 0

    @property
    def id(self) -> str:
        return zone_node_id(self.element, self.name)

    @property
    def is_declared(self) -> bool:
        """Is this a zone of ``spec.zones``, rather than ``local`` or ``any``?"""
        return self.name not in (LOCAL_ZONE, ANY_ZONE)

    @property
    def is_empty(self) -> bool:
        """A declared zone with nothing in it — the shape ``W150`` reports."""
        return self.is_declared and not self.interfaces

    def describe(self) -> tuple[str, ...]:
        """The lines a node label carries under the zone's name."""
        if not self.is_declared:
            return ()
        if not self.interfaces:
            return ("no interface",)
        return (", ".join(self.interfaces),)


@dataclass(frozen=True, slots=True)
class PolicyView:
    """What one device's firewall does to traffic between two of its zones (§24.5).

    One per *zone pair* rather than one per rule, because that is the question
    the picture answers: an edge on this diagram is "traffic may cross from here
    to there, on these terms", and a diagram with one edge per rule would draw
    twelve lines between two boxes and answer nothing.

    :attr:`verdict` is the summary the edge is coloured by, and it is deliberately
    coarse. A pair whose rules all accept is open; one whose rules all deny is
    closed; one holding both is *conditional*, which is the interesting case and
    the one whose rules are worth reading in full.
    """

    element: str
    source: str
    target: str
    #: Every rule of the pair, already rendered, in priority order.
    rules: tuple[str, ...] = ()
    #: Every translation of the pair, already rendered, in declaration order.
    translations: tuple[str, ...] = ()
    #: ``open``, ``closed`` or ``conditional``.
    verdict: str = "conditional"

    @property
    def id(self) -> str:
        return f"{self.element}#policy:{self.source}>{self.target}"

    @property
    def permits(self) -> bool:
        return self.verdict != "closed"

    def label(self) -> tuple[str, ...]:
        """The edge label, one line per element: the rules, or a count of them.

        Three is the cut-off for the same reason
        :meth:`RoutingView.describe` counts rather than lists: an edge label is
        not a policy document, and the rules themselves are on the tooltip and in
        the JSON, which is where a reader who wants them is looking.

        Lines rather than one joined string, because each renderer joins them its
        own way — DOT after escaping each line, Mermaid onto the single line its
        link labels read well in — and a newline baked in here would be escaped
        into a literal ``\\n`` by the first of the two.
        """
        if len(self.rules) <= _POLICY_LABEL_RULES:
            return self.rules
        return (count_text(len(self.rules), "rule"),)


#: How many rules an edge of the security view spells out before it counts them.
_POLICY_LABEL_RULES: Final = 3


@dataclass(frozen=True, slots=True)
class AdjacencyView:
    """One protocol adjacency between two elements (§16.8).

    A BGP session and an OSPF adjacency are both "these two routers talk", but
    they are different facts about a network and are drawn differently, so the
    kind is carried rather than inferred from whichever field happens to be set.
    """

    #: ``bgp`` or ``ospf`` — :class:`EdgeKind` uses the same two spellings.
    protocol: str
    #: The element that declares the session, or the lower-sorting of the two
    #: ends of an OSPF adjacency.
    source: str
    target: str
    #: The address the session is configured towards; empty for OSPF, which
    #: discovers its neighbours rather than naming them.
    peer_address: str = ""
    #: The interface the peer address sits on, for a resolved BGP session; the
    #: interfaces the adjacency runs over, for OSPF.
    source_port: str = ""
    target_port: str = ""
    #: The two AS numbers, source first, when both ends declare one.
    asns: tuple[int, ...] = ()
    #: The OSPF area the adjacency is in.
    area: str | None = None
    description: str | None = None

    @property
    def is_internal(self) -> bool:
        """Is this session inside one AS — iBGP rather than eBGP?"""
        return len(self.asns) == 2 and self.asns[0] == self.asns[1]

    @property
    def label(self) -> str:
        """``65001 → 65002``, or ``area 0.0.0.0`` — what the edge is annotated with.

        The AS pair is what makes a BGP edge readable: whether a session is
        internal or external is the first thing anybody wants from a routing
        diagram, and two identical numbers say "iBGP" more directly than a legend
        can.
        """
        if self.protocol == "ospf":
            return f"area {self.area}" if self.area else "ospf"
        if len(self.asns) == 2:
            return (
                f"iBGP {self.asns[0]}" if self.is_internal else f"{self.asns[0]} → {self.asns[1]}"
            )
        return f"AS {self.asns[0]}" if self.asns else "bgp"

    @property
    def id(self) -> str:
        """Stable identity of the edge this adjacency becomes."""
        if self.protocol == "ospf":
            return f"{self.source}#ospf#{self.target}"
        return f"{self.source}#bgp#{self.peer_address}"


@dataclass(frozen=True, slots=True)
class PatchHop:
    """One panel a spliced run crosses, and the coupler it crosses it by."""

    #: Fully-qualified name of the patch panel.
    panel: str
    #: Port the run arrives on, e.g. ``front/7``.
    ingress: str
    #: The port that one is coupled to, e.g. ``rear/7``.
    egress: str

    @property
    def name(self) -> str:
        return short_name(self.panel)

    def __str__(self) -> str:
        return f"{self.panel}:{self.ingress} -> {self.egress}"


@dataclass(frozen=True, slots=True)
class PatchView:
    """What one edge stands for when a run crosses one or more patch panels.

    Set on the spliced edge only: the *physical* layer draws the segments
    themselves, so there is nothing to record there. It carries the segments in
    the order the run crosses them and the panels between them, which is what
    lets a trace name the pass-through and a JSON export reproduce the patch
    record.
    """

    #: Cable fully-qualified names, in the order the run crosses them.
    segments: tuple[str, ...] = ()
    #: The panels between them; always one fewer entry than :attr:`segments`.
    hops: tuple[PatchHop, ...] = ()

    @property
    def panels(self) -> tuple[str, ...]:
        """The panels crossed, in order, without repeats."""
        return tuple(dict.fromkeys(hop.panel for hop in self.hops))

    def describe(self) -> str:
        """``pp-idf-a front/7-rear/7`` — the pass-throughs, for a report."""
        return ", ".join(f"{hop.name} {hop.ingress}-{hop.egress}" for hop in self.hops)


@dataclass(frozen=True, slots=True)
class WirelessView:
    """What a ``medium: wireless`` edge is an association *to* (§6.2.6).

    A wireless link has a shape a cable does not: one end beacons and the other
    joins it, on a named network at a stated frequency. Resolving that once here
    is what lets every renderer label the link the same way — and what keeps a
    radio link tellable apart from a cable whose ``medium`` merely says
    ``wireless``, which is all the graph carried before.
    """

    #: The SSIDs the association is on: what the client joined when it says so,
    #: and otherwise everything the access point beacons.
    ssids: tuple[str, ...] = ()
    #: ``2.4GHz`` / ``5GHz`` / ``6GHz``, when the access point states one.
    band: str | None = None
    channel: int | None = None
    width_mhz: int | None = None
    #: ``element:interface`` of the ``ap``-role radio, when exactly one end is
    #: one. Empty for a link whose ends disagree about that — ``E028``.
    access_point: str = ""

    @property
    def channel_text(self) -> str | None:
        """``36/5GHz``, or just the band when no channel is stated."""
        if self.band is None:
            return None
        return self.band if self.channel is None else f"{self.channel}/{self.band}"

    def describe(self) -> str:
        """``home, guest @ 36/5GHz`` — the label an L2 diagram puts on the link."""
        network = ", ".join(self.ssids)
        tuning = self.channel_text
        if network and tuning:
            return f"{network} @ {tuning}"
        return network or tuning or ""


@dataclass(frozen=True, slots=True)
class RackSlot:
    """One element mounted in a rack, and the units it occupies."""

    element: str
    name: str
    kind: str
    #: Lowest unit occupied, counted from 1 at the bottom.
    position: int
    height: int = 1
    #: What this element says about power, when it says anything (§17.5). The
    #: elevation prints :meth:`~netviz.power.PowerNode.rack_note` from it: a
    #: draw for a load, a utilisation for a PDU. ``None`` for an element the
    #: inventory records no power for, which is what keeps an elevation of a
    #: pre-§17 inventory byte-identical to what it was.
    power: PowerNode | None = None

    @property
    def units(self) -> range:
        return range(self.position, self.position + self.height)

    @property
    def top(self) -> int:
        return self.position + self.height - 1


@dataclass(frozen=True, slots=True)
class RackView:
    """One rack and everything mounted in it, for the elevation (§3.2).

    :attr:`height` is what the elements declared through
    ``metadata.location.rack_height``; when none of them did, it is the top of
    the highest thing in the rack, so an elevation is still drawable for an
    inventory that records positions but never measured the cabinet.
    """

    #: ``(site, room, rack)`` — what makes two elements share a rack.
    key: tuple[str, str, str]
    #: How the rack is written out, e.g. ``hq / mdf / r1``.
    label: str
    height: int
    #: The elements in it, lowest position first.
    slots: tuple[RackSlot, ...] = ()
    #: True when :attr:`height` was inferred rather than declared.
    inferred_height: bool = False

    @property
    def id(self) -> str:
        """The identity of this rack's node: ``rack:hq/mdf/r1``."""
        return RACK_ID_PREFIX + "/".join(self.key)

    @property
    def site(self) -> str:
        return self.key[0]

    @property
    def room(self) -> str:
        return self.key[1]

    @property
    def name(self) -> str:
        return self.key[2]

    @property
    def used_units(self) -> int:
        return sum(slot.height for slot in self.slots)

    def occupant(self, unit: int) -> RackSlot | None:
        """What occupies ``unit``, or ``None`` when it is free."""
        return next((slot for slot in self.slots if unit in slot.units), None)

    def elevation(self) -> tuple[tuple[int, RackSlot | None], ...]:
        """Every unit from the top down, with what occupies it.

        Top down because that is how a person standing in front of a rack reads
        it, and because it puts U1 at the bottom of the drawing where it is.
        """
        return tuple((unit, self.occupant(unit)) for unit in range(self.height, 0, -1))

    def restricted_to(self, elements: Container[str]) -> RackView:
        """The same rack with only the slots ``elements`` still holds."""
        return replace(self, slots=tuple(slot for slot in self.slots if slot.element in elements))


@dataclass(frozen=True, slots=True)
class Node:
    """One drawable thing: an element, or — at layer 3 — an IP subnet.

    Cables are edges, not nodes. A subnet node carries no ``element`` and no
    ports; :attr:`subnet` holds what it is instead, and :attr:`type` says which
    of the two a node is.
    """

    #: Fully-qualified name; the identity used everywhere in the graph. A subnet
    #: node uses ``subnet:<prefix>`` (:data:`SUBNET_ID_PREFIX`).
    fqn: str
    #: ``metadata.name``, i.e. the fqn without its namespace; the prefix for a subnet.
    name: str
    #: ``switch``, ``router``, ``hub``, ``computer``, ``server``, ``adapter``
    #: or :data:`SUBNET_KIND`.
    kind: str
    namespace: str
    #: The declared element, or ``None`` for a derived node such as a subnet.
    element: Device | Adapter | PatchPanel | Pdu | User | Group | None = None
    ports: tuple[PortView, ...] = ()
    #: Every VLAN this element participates in, links included. See the module
    #: docstring. For a subnet: every VLAN an interface addressed in it is in.
    vlans: frozenset[int] = frozenset()
    type: NodeType = NodeType.ELEMENT
    #: The prefix this node stands for; set exactly for a subnet node.
    subnet: Subnet | None = None
    #: The tunnel this node stands for; set exactly for a tunnel node.
    tunnel: TunnelView | None = None
    #: The collapsed namespace this node stands for; set exactly for an
    #: aggregate node (:mod:`netviz.render.aggregate`).
    aggregate: AggregateView | None = None
    #: The rack this node stands for; set exactly for a rack node.
    rack: RackView | None = None
    #: What this element contributes to the control plane; set at
    #: :attr:`Layer.ROUTING` only, where it is the whole reason the node is drawn.
    routing: RoutingView | None = None
    #: What this element contributes to the power view; set at
    #: :attr:`Layer.POWER` only (§17.5).
    power: PowerNode | None = None
    #: The network namespace this node stands for; set at :attr:`Layer.NETNS`
    #: only (§23.3), on the derived nodes *and* on the element node, which
    #: stands for the machine's initial namespace.
    netns: NetnsView | None = None
    #: The security zone this node stands for; set at :attr:`Layer.SECURITY`
    #: only (§24.5), where it is the whole reason the node is drawn.
    security: SecurityView | None = None
    #: The box this node is drawn inside when the *layer* groups the nodes rather
    #: than the reader: the VRF at :attr:`Layer.ROUTING`. Distinct from
    #: :attr:`namespace`, which is where the document lives — a router holds any
    #: number of VRFs and lives in exactly one directory.
    cluster: str = ""

    @classmethod
    def for_tunnel(cls, view: TunnelView) -> Node:
        """The node standing for one tunnel.

        Unlike a subnet, a tunnel *is* a declared element, so it keeps its own
        namespace: ``--group-by-namespace`` draws it inside the site whose
        directory declared it, which is where the document lives.
        """
        return cls(
            fqn=view.id,
            name=view.name,
            kind=TUNNEL_KIND,
            namespace=view.namespace,
            element=None,
            vlans=frozenset(),
            type=NodeType.TUNNEL,
            tunnel=view,
        )

    @classmethod
    def for_pdu(cls, fqn: str, pdu: Pdu, power: PowerNode) -> Node:
        """The node standing for one power distribution unit (§17.5).

        A PDU is a declared element and keeps its own namespace, unlike a subnet
        or a rack: ``--group-by-namespace`` draws it inside the directory that
        declared it, which is where the document lives. It carries no ports —
        an outlet is not an interface (§17.1) — so the label is its power summary
        and nothing else.
        """
        return cls(
            fqn=fqn,
            name=pdu.metadata.name,
            kind=PDU_KIND,
            namespace=namespace_of(fqn),
            element=pdu,
            power=power,
        )

    @classmethod
    def for_identity(cls, fqn: str, element: User | Group) -> Node:
        """The node standing for one user or one group (§19.3).

        Like a PDU and unlike a subnet it is a declared element and keeps its own
        namespace, so ``--group-by-namespace`` draws it inside the directory that
        declared it. It carries no ports — an identity owns no interfaces
        (§19.1) — so the label is the account and nothing else.
        """
        return cls(
            fqn=fqn,
            name=element.metadata.name,
            kind=element.kind,
            namespace=namespace_of(fqn),
            element=element,
        )

    @classmethod
    def for_zone(cls, view: SecurityView, node: Node) -> Node:
        """The node standing for one security zone (§24.5).

        ``node`` is the device's own node, and the zone borrows two things from
        it. The namespace, so ``--group-by-namespace`` draws the zone inside the
        directory that declared the device — a zone has no document of its own,
        so the only honest answer is its device's. And the ports, narrowed to
        the interfaces actually in the zone, so ``--vlan`` still selects a
        broadcast domain here and a tooltip can say what is behind the box.
        """
        ports = tuple(port for port in node.ports if port.name in view.interfaces)
        return cls(
            fqn=view.id,
            name=view.name,
            kind=ZONE_KIND,
            namespace=node.namespace,
            ports=ports,
            vlans=frozenset().union(*(port.vlans for port in ports)) if ports else frozenset(),
            type=NodeType.ZONE,
            security=view,
            cluster=view.element,
        )

    @classmethod
    def for_stack(cls, view: NetnsView, node: Node) -> Node:
        """The node standing for one *non-initial* network stack of a machine.

        ``node`` is the machine's own node, and the stack borrows two things
        from it. The namespace, so ``--group-by-namespace`` draws the container
        inside the directory that declared its host — a namespace has no
        document of its own, so the only honest answer is its machine's. And,
        through :attr:`netns`, the machine itself, which is what
        :attr:`Node.address` answers and therefore what a click selects.

        Unlike :meth:`for_zone` the ports are not filtered out of ``node``: a
        stack already carries exactly the interfaces that are in it, so taking
        them from the view is both cheaper and the only way this node and the
        one :func:`_netns_view` mints cannot come to disagree.

        The two do differ in two respects, and both are decisions about the
        drawing rather than about the stack. The netns view boxes every stack of
        one machine together and labels the box with the machine, so its nodes
        say ``blue``; nothing boxes them here, so they say ``srv-01/blue``. And
        no :attr:`cluster` is set here: layer 3 groups by namespace or not at
        all, and a second grouping by machine would fight it.
        """
        return cls(
            fqn=view.id,
            name=view.qualified_label,
            kind=NETNS_KIND,
            namespace=node.namespace,
            ports=view.ports,
            vlans=frozenset().union(*(port.vlans for port in view.ports))
            if view.ports
            else frozenset(),
            type=NodeType.NETNS,
            netns=view,
        )

    @classmethod
    def for_rack(cls, view: RackView) -> Node:
        """The node standing for one rack and its elevation.

        A rack holds whatever the inventory bolted into it, which may come from
        several namespaces, so — like a subnet — it belongs to none of them and
        reports the root.
        """
        return cls(
            fqn=view.id,
            name=view.label or view.name,
            kind=RACK_KIND,
            namespace="",
            type=NodeType.RACK,
            rack=view,
        )

    @classmethod
    def for_subnet(cls, subnet: Subnet) -> Node:
        """The node standing for one IP prefix.

        A subnet spans whatever namespaces its members live in, so it belongs to
        none of them and reports the root namespace: ``--group-by-namespace``
        then draws it outside every group, which is where a cross-site prefix
        honestly belongs.
        """
        return cls(
            fqn=subnet.node_id,
            name=subnet.label,
            kind=SUBNET_KIND,
            namespace="",
            vlans=subnet.vlans,
            type=NodeType.SUBNET,
            subnet=subnet,
        )

    @property
    def is_subnet(self) -> bool:
        return self.type is NodeType.SUBNET

    @property
    def is_tunnel(self) -> bool:
        return self.type is NodeType.TUNNEL

    @property
    def is_rack(self) -> bool:
        """Does this node stand for a rack rather than for something in one?"""
        return self.type is NodeType.RACK

    @property
    def is_aggregate(self) -> bool:
        """Does this node stand for a whole namespace rather than one thing?"""
        return self.type is NodeType.AGGREGATE

    @property
    def is_netns(self) -> bool:
        """Does this node stand for a network namespace inside a machine (§23.3)?

        False for the element node of the netns view even though it carries a
        :attr:`netns`: that node is still the machine, and a renderer that drew
        it as a bare namespace box would lose the icon, the kind and the link to
        the document.
        """
        return self.type is NodeType.NETNS

    @property
    def identity(self) -> User | Group | None:
        """The user or group this node stands for, or ``None`` for everything else.

        The narrowing exists so a renderer can ask one question instead of
        pattern-matching on :attr:`kind`, which is what every other derived view
        of a node does (:attr:`power`, :attr:`routing`).
        """
        return self.element if isinstance(self.element, (User, Group)) else None

    @property
    def is_element(self) -> bool:
        """Does this node stand for a device or an adapter the reader can point at?"""
        return self.type is NodeType.ELEMENT

    @property
    def address(self) -> str:
        """The *element* a click on this node acts on (§18, §23.4).

        Since layer 3 draws one node per network stack, a machine running three
        containers is four nodes, and an editor that took the node id as the
        thing selected would offer to rename ``netns:hosts/srv-01:blue`` — an
        address no document has and ``netviz edit`` would refuse. A stack is
        not separately editable: it exists because ``spec.netns`` on the machine
        says so, so the machine is what a click selects, what a drag moves (all
        of its stacks together) and what a bulk edit is posted for.

        Everything else answers its own id, which is what it has always been.
        """
        return self.netns.element if self.netns is not None else self.fqn

    @property
    def _document(self) -> Device | Adapter | PatchPanel | Pdu | User | Group | Tunnel | None:
        """The declared document behind the node, tunnels included."""
        if self.element is not None:
            return self.element
        return self.tunnel.tunnel if self.tunnel is not None else None

    @property
    def labels(self) -> Mapping[str, str]:
        document = self._document
        return document.metadata.labels if document is not None else {}

    @property
    def style(self) -> Style | None:
        """What the declaring document says this node should look like (§22).

        ``None`` for a node nobody declared — a subnet, a rack, a collapsed
        namespace — which is not the same as "no style": those are drawn by the
        theme and the palette, and there is no document to hang a ``spec.style``
        on. See :mod:`netviz.render.styles` for the rest of the ladder.
        """
        document = self._document
        return getattr(document.spec, "style", None) if document is not None else None

    @property
    def description(self) -> str | None:
        document = self._document
        return document.metadata.description if document is not None else None

    @property
    def addresses(self) -> tuple[str, ...]:
        """Every address configured on the element, in interface order."""
        return tuple(address for port in self.ports for address in port.addresses)

    @property
    def routable_addresses(self) -> tuple[str, ...]:
        """The addresses worth printing on a diagram; see :attr:`PortView.routable_addresses`."""
        return tuple(address for port in self.ports for address in port.routable_addresses)

    def port(self, name: str) -> PortView | None:
        return next((port for port in self.ports if port.name == name), None)


@dataclass(frozen=True, slots=True)
class Edge:
    """An undirected link between two nodes.

    ``source``/``target`` carry no direction: they are the endpoints in the
    canonical order the cable stored them in (§7.1). An attachment edge points
    from the host to the adapter purely so the two ends are distinguishable, and
    a subnet edge from the element to the prefix.
    """

    #: Stable identity: the cable's fqn, ``<adapter fqn>#upstream``, or
    #: ``<element fqn>:<interface>#<prefix>`` for a subnet membership.
    id: str
    kind: EdgeKind
    source: str
    target: str
    #: Interface name on the source side; ``""`` for the host end of an
    #: attachment, which has no interface to name (§8.1).
    source_port: str = ""
    target_port: str = ""
    #: ``copper``, ``fiber`` or ``wireless``; ``""`` for a logical edge such as
    #: a subnet membership, which runs over no medium at all.
    medium: str = "copper"
    #: Link rate in bit/s, when one is declared.
    speed: int | None = None
    #: The physical cable label / patch-panel identifier, when set.
    label: str | None = None
    length_m: float | None = None
    #: VLANs the link carries; see the module docstring.
    vlans: frozenset[int] = frozenset()
    #: Addresses that put the source element in the target subnet, in
    #: configuration order. Empty for every edge that is not a membership.
    addresses: tuple[str, ...] = ()
    #: The element the edge came from, for exporters that want the full record.
    cable: Cable | None = None
    adapter: Adapter | None = None
    #: The tunnel this edge is, or is a leg of; set on ``tunnel`` and
    #: ``encapsulation`` edges.
    tunnel: TunnelView | None = None
    #: The links this edge stands for, when several parallel ones were folded
    #: into it (:mod:`netviz.render.aggregate`). ``None`` on a link that is
    #: exactly itself.
    bundle: BundleView | None = None
    #: The patch panels this edge was spliced through, when it stands for a run
    #: of two or more cable segments (§15.2). ``None`` on a direct cable, which
    #: is what makes the two tellable apart in an export.
    patch: PatchView | None = None
    #: The association a ``medium: wireless`` link is, when at least one end
    #: declares a ``wireless`` block (§6.2.6). ``None`` on every wired link, and
    #: on a radio link whose ends model no radio detail.
    wireless: WirelessView | None = None
    #: The protocol adjacency this edge is; set on ``bgp`` and ``ospf`` edges,
    #: which exist only at :attr:`Layer.ROUTING` (§16.8).
    adjacency: AdjacencyView | None = None
    #: The power feed this edge is; set on ``outlet`` and ``poe`` edges, which
    #: exist only at :attr:`Layer.POWER` (§17.5).
    feed: Feed | None = None
    #: What the firewall does between two zones; set on ``policy`` edges, which
    #: exist only at :attr:`Layer.SECURITY` (§24.5).
    policy: PolicyView | None = None

    @property
    def name(self) -> str:
        """The short name of the cable, the adapter or the tunnel."""
        if self.tunnel is not None:
            return self.tunnel.name
        return short_name(self.id.partition("#")[0])

    @property
    def document(self) -> Cable | Tunnel | Adapter | None:
        """The declared element this edge came from, whichever kind it is.

        A cable first, then a tunnel, then the adapter an attachment stands for:
        the order they are drawn in, and the order in which at most one of them
        is ever set.
        """
        if self.cable is not None:
            return self.cable
        if self.tunnel is not None:
            return self.tunnel.tunnel
        return self.adapter

    @property
    def namespace(self) -> str:
        """The directory the declaring document was found in; ``""`` for none.

        A derived link — a subnet membership, a protocol adjacency, a power feed
        — has no document and therefore no namespace, which is the honest answer
        rather than borrowing one of its endpoints'.
        """
        if self.tunnel is not None:
            return self.tunnel.namespace
        document = self.document
        return namespace_of(self.id.partition("#")[0]) if document is not None else ""

    @property
    def labels(self) -> Mapping[str, str]:
        document = self.document
        return document.metadata.labels if document is not None else {}

    @property
    def style(self) -> Style | None:
        """What the declaring document says this link should look like (§22)."""
        document = self.document
        return getattr(document.spec, "style", None) if document is not None else None

    @property
    def is_logical(self) -> bool:
        """Is this something nobody can unplug — a subnet, a tunnel, a nesting?"""
        return self.kind is not EdgeKind.CABLE and self.kind is not EdgeKind.ATTACHMENT

    @property
    def is_self_link(self) -> bool:
        return self.source == self.target

    @property
    def is_patched(self) -> bool:
        """Does this edge run through at least one passive cross-connect?"""
        return self.patch is not None

    @property
    def speed_text(self) -> str | None:
        """The link rate in the largest exact unit, e.g. ``1Gbps``."""
        return format_bitrate(self.speed) if self.speed is not None else None

    def endpoints(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return ((self.source, self.source_port), (self.target, self.target_port))


@dataclass(frozen=True)
class Graph:
    """A filtered, resolved view of an inventory, ready to render."""

    #: The inventory root the graph was built from.
    root: Path
    #: Nodes in load order, keyed by fully-qualified name.
    nodes: Mapping[str, Node] = field(default_factory=dict)
    edges: tuple[Edge, ...] = ()
    layer: Layer = Layer.L1
    #: Cables dropped because an endpoint did not resolve, with the reason.
    dangling: tuple[str, ...] = ()
    #: Where each declared element was written, keyed by fully-qualified name —
    #: the whole inventory's, not only the part that survived a filter, because
    #: an edge names a cable that is no longer a node. Derived nodes (a layer-3
    #: prefix) have no entry: nobody wrote them. This is what
    #: ``--link-template`` expands (:mod:`netviz.render.links`); it is
    #: deliberately not part of what a renderer *draws*.
    sources: Mapping[str, SourceLocation] = field(default_factory=dict)
    #: The stored arrangement for this layer (§18), merged from every
    #: ``kind: layout`` document in the tree and narrowed to what survived the
    #: filter. Empty when nothing was arranged, which is the normal case: a
    #: renderer that finds it empty lays the graph out exactly as it always did.
    #:
    #: It lives on the graph rather than in ``RenderOptions`` because it is a
    #: fact about the inventory, not a preference about the drawing — which is
    #: what makes the SVG, HTML and JSON renderers agree on coordinates without
    #: any of them being told to.
    geometry: Geometry = field(default_factory=Geometry)
    #: The notes, areas and legends that belong in this view (§21), in the order
    #: they were declared. Presentational and *never* topology: nothing here is
    #: a node or an edge, no annotation contributes to :attr:`nodes` or
    #: :attr:`edges`, and a rendering made with annotations turned off is
    #: byte-identical to one of an inventory that declares none. They ride on the
    #: graph for the same reason :attr:`geometry` does — a renderer must not have
    #: to be told twice which drawing it is making — and are carried through
    #: every transform unchanged, because filtering a diagram narrows what an
    #: annotation *encloses* rather than which annotations exist.
    annotations: AnnotationSet = field(default_factory=AnnotationSet)
    #: What each annotation is *about*, keyed by the annotation's fully-qualified
    #: name: the elements an area encloses, or the one element a note is anchored
    #: to. Resolved by :mod:`netviz.annotations` against the inventory, which
    #: is the only place that can do it — a member may be written relatively and
    #: a selector is a query — and therefore resolved here, once, where the
    #: inventory is still in scope.
    #:
    #: These are inventory names, not necessarily drawn ones: a filter narrows
    #: the graph and not this map, and
    #: :func:`~netviz.render.annotations.annotation_views` intersects the two.
    #: That split is what lets an area survive a filter that removes half of it
    #: and disappear when it removes all of it.
    annotation_targets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def source_of(self, fqn: str) -> SourceLocation | None:
        """Where the element called ``fqn`` was declared, if it was declared."""
        return self.sources.get(fqn)

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, fqn: object) -> bool:
        return fqn in self.nodes

    def __iter__(self) -> Iterator[Node]:
        return iter(self.nodes.values())

    @property
    def namespaces(self) -> tuple[str, ...]:
        """Every namespace holding at least one node, in first-seen order."""
        return tuple(dict.fromkeys(node.namespace for node in self.nodes.values()))

    @property
    def element_nodes(self) -> tuple[Node, ...]:
        """The nodes standing for a declared device or adapter, in graph order."""
        return tuple(node for node in self.nodes.values() if node.is_element)

    @property
    def subnet_nodes(self) -> tuple[Node, ...]:
        """The subnet nodes, in graph order; empty below layer 3."""
        return tuple(node for node in self.nodes.values() if node.is_subnet)

    @property
    def tunnel_nodes(self) -> tuple[Node, ...]:
        """The tunnel nodes, in graph order.

        A point-to-point tunnel is an *edge* below the overlay layer, so this is
        empty unless the graph holds a multipoint tunnel or was built for
        :attr:`Layer.OVERLAY`.
        """
        return tuple(node for node in self.nodes.values() if node.is_tunnel)

    @property
    def rack_nodes(self) -> tuple[Node, ...]:
        """The rack nodes, in graph order; empty outside :attr:`Layer.RACK`."""
        return tuple(node for node in self.nodes.values() if node.is_rack)

    @property
    def aggregate_nodes(self) -> tuple[Node, ...]:
        """The collapsed-namespace nodes, in graph order; empty without ``--collapse``."""
        return tuple(node for node in self.nodes.values() if node.is_aggregate)

    @property
    def routing_nodes(self) -> tuple[Node, ...]:
        """The nodes carrying routing state; empty outside :attr:`Layer.ROUTING`."""
        return tuple(node for node in self.nodes.values() if node.routing is not None)

    @property
    def power_nodes(self) -> tuple[Node, ...]:
        """The nodes carrying power state; empty outside :attr:`Layer.POWER`."""
        return tuple(node for node in self.nodes.values() if node.power is not None)

    @property
    def clusters(self) -> tuple[str, ...]:
        """Every box the layer itself asks for, in first-seen order.

        Empty at every layer but :attr:`Layer.ROUTING`, which groups its nodes by
        VRF. A renderer that finds this non-empty groups by it *instead* of by
        namespace: the layer's own grouping is the one the reader asked for by
        choosing the layer, and two nested sets of boxes would say less than one.
        """
        return tuple(dict.fromkeys(node.cluster for node in self.nodes.values() if node.cluster))

    def nodes_in_cluster(self, cluster: str) -> tuple[Node, ...]:
        """The nodes drawn inside ``cluster``; ``""`` selects the unboxed ones."""
        return tuple(node for node in self.nodes.values() if node.cluster == cluster)

    @property
    def tunnels(self) -> tuple[TunnelView, ...]:
        """Every tunnel the graph draws, as a node or as an edge, in graph order.

        A tunnel folded into a bundle still counts: it is drawn, as one strand
        of an edge that says how many strands it has.
        """
        seen: dict[str, TunnelView] = {}
        for node in self.nodes.values():
            if node.tunnel is not None:
                seen.setdefault(node.tunnel.fqn, node.tunnel)
        for edge in self.edges:
            members = edge.bundle.edges if edge.bundle is not None else (edge,)
            for member in members:
                if member.tunnel is not None:
                    seen.setdefault(member.tunnel.fqn, member.tunnel)
        return tuple(seen.values())

    @property
    def vlans(self) -> frozenset[int]:
        """Every VLAN any node participates in."""
        return (
            frozenset().union(*(node.vlans for node in self.nodes.values()))
            if self.nodes
            else frozenset()
        )

    def nodes_in(self, namespace: str) -> tuple[Node, ...]:
        return tuple(node for node in self.nodes.values() if node.namespace == namespace)

    def adjacency(self) -> Mapping[str, frozenset[str]]:
        """Neighbours of every node, both directions, self-links excluded."""
        neighbours: dict[str, set[str]] = {fqn: set() for fqn in self.nodes}
        for edge in self.edges:
            if edge.source == edge.target:
                continue
            neighbours.setdefault(edge.source, set()).add(edge.target)
            neighbours.setdefault(edge.target, set()).add(edge.source)
        return {fqn: frozenset(values) for fqn, values in neighbours.items()}

    @property
    def is_empty(self) -> bool:
        return not self.nodes


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def _narrowed(geometry: Geometry, nodes: Mapping[str, Node], edges: Sequence[Edge]) -> Geometry:
    """The arrangement, restricted to what this drawing actually contains."""
    if geometry.is_empty:
        return geometry
    return geometry.narrowed(nodes, (edge.id for edge in edges))


def _placed_stacks(geometry: Geometry, nodes: Mapping[str, Node]) -> Geometry:
    """Give a split element's extra stacks a position derived from its own (§18).

    Stored geometry is keyed per element, because that is what somebody dragged
    and what a ``kind: layout`` document names. A layer that draws an element as
    *several* nodes therefore has coordinates for one of them and none for the
    rest, and a diagram that was fully arranged before its host declared a
    namespace would drop from :attr:`~netviz.layout.geometry.LayoutMode.FIXED`
    to ``PARTIAL`` — the whole drawing re-laid-out around one new box.

    The rule, so that nobody has to guess it:

    * the **initial** namespace *is* the element node and inherits its
      coordinates outright — it keeps the element's id, so there is nothing to
      derive;
    * every **further** stack is placed relative to it, offset down and to the
      right in declaration order, which puts the containers of one host in a
      readable column beside it and — being derived from a stored position —
      moves with the host when somebody drags it.

    A stack that has a stored position of its own keeps it: this fills gaps and
    never overrides. Nothing derived here is written to a document; ``netviz
    layout --write`` persists what the engine settled on, and ``--prune`` sees
    these ids in :func:`~netviz.layout.seed.live_keys` because they are nodes
    of the drawing, so a hand-placed ``netns:...`` entry is not stale.
    """
    if geometry.is_empty or not geometry.nodes:
        return geometry
    derived: dict[str, Placement] = {}
    ordinals: dict[str, int] = {}
    for fqn, node in nodes.items():
        view = node.netns
        if view is None or view.is_root or fqn in geometry.nodes:
            continue
        anchor = geometry.nodes.get(view.element)
        if anchor is None:
            continue
        step = ordinals[view.element] = ordinals.get(view.element, 0) + 1
        derived[fqn] = Placement(
            x=anchor.x + STACK_OFFSET_X,
            y=anchor.y + step * STACK_OFFSET_Y,
        )
    if not derived:
        return geometry
    return replace(geometry, nodes={**geometry.nodes, **derived})


def build_graph(inventory: Inventory, *, layer: Layer = Layer.L1) -> Graph:
    """Resolve an inventory into a renderable graph.

    Args:
        inventory: A tree loaded by :func:`~netviz.loader.load_tree`.
        layer: ``physical``, ``l1`` and ``l2`` build the same topology and differ
            in what is drawn from it (§8.2): ``physical`` keeps the patch panels
            and their segments, the other two splice each run into one edge, and
            ``l2`` annotates it with VLAN membership. ``l3`` builds the routed
            graph instead: subnets become nodes, cables disappear, and elements
            without a routable address drop out. ``overlay`` builds the
            encapsulation graph: tunnels become nodes, joined to their endpoints
            and to the tunnels they run inside. ``rack`` builds no topology at
            all — one node per rack, holding its elevation. See the module
            docstring.

    Returns:
        A graph whose every edge references two nodes that exist. Cables and
        tunnels with an unresolvable endpoint are reported in
        :attr:`Graph.dangling` instead of raising, because ``--force`` must still
        produce a picture. The layer-3 graph keeps that list too: an unresolved
        cable costs a host the VLAN membership it would have inherited from the
        link.
    """
    # Iterate the full element map so nodes keep inventory load order rather
    # than the devices-then-adapters order of ``cable_owners``.
    owners: dict[str, Device | Adapter | PatchPanel] = {
        fqn: element
        for fqn, element in inventory.elements.items()
        if isinstance(element, (Device, Adapter, PatchPanel))
    }
    ports = {
        fqn: tuple(PortView.of(interface) for interface in owner.interfaces)
        for fqn, owner in owners.items()
    }

    edges, dangling = _build_edges(inventory)
    tunnels, tunnel_dangling = resolve_tunnels(inventory)
    dangling += tunnel_dangling

    tunnel_nodes, tunnel_edges = _tunnel_topology(tunnels, ports)
    edges += tunnel_edges

    panels = inventory.patchpanels
    if panels and not layer.shows_panels and not layer.builds_own_nodes:
        # Splice *before* membership is computed, so a host behind a panel is
        # in the VLAN of the port at the far end of the run rather than in the
        # nothing a panel port declares. That is also what makes a spliced
        # graph identical to the directly-cabled one.
        edges, splice_dangling = splice_patch_panels(edges, panels, owners)
        dangling += splice_dangling

    node_vlans = _node_vlans(inventory, ports, edges)

    nodes = {
        fqn: Node(
            fqn=fqn,
            name=owner.metadata.name,
            kind=owner.kind,
            namespace=namespace_of(fqn),
            element=owner,
            ports=ports[fqn],
            vlans=node_vlans.get(fqn, frozenset()),
        )
        for fqn, owner in owners.items()
    }
    nodes.update(tunnel_nodes)
    if not layer.shows_panels and not layer.builds_own_nodes:
        # A panel that no cable reaches has no segments to splice, so removing
        # the nodes is a separate step from removing the edges.
        nodes = {fqn: node for fqn, node in nodes.items() if node.kind != PATCHPANEL_KIND}

    if layer is Layer.L3:
        nodes, edges = _routed_view(nodes, subnets_of(inventory))
    elif layer is Layer.OVERLAY:
        nodes, edges = _overlay_view(nodes, tunnels)
    elif layer is Layer.ROUTING:
        nodes, edges, routing_dangling = _routing_view(nodes, inventory, subnets_of(inventory))
        dangling += routing_dangling
    elif layer is Layer.RACK:
        nodes, edges = _rack_view(inventory)
    elif layer is Layer.POWER:
        nodes, edges = _power_view(inventory, nodes)
    elif layer is Layer.IDENTITY:
        nodes, edges = _identity_view(inventory)
    elif layer is Layer.NETNS:
        nodes, edges = _netns_view(nodes, edges)
    elif layer is Layer.SECURITY:
        nodes, edges = _security_view(nodes)
    geometry = resolve_geometry(inventory, layer.value)
    annotations = annotations_for_view(inventory, layer.value)
    return Graph(
        root=inventory.root,
        nodes=nodes,
        edges=edges,
        layer=layer,
        dangling=dangling,
        sources=dict(inventory.sources),
        geometry=_narrowed(_placed_stacks(geometry, nodes), nodes, edges),
        annotations=annotations,
        annotation_targets=_annotation_targets(inventory, annotations, nodes),
    )


def _annotation_targets(
    inventory: Inventory, annotations: AnnotationSet, nodes: Mapping[str, Node]
) -> dict[str, tuple[str, ...]]:
    """What every annotation of this view refers to, resolved once (§21).

    The renderers get a :class:`Graph`, not an :class:`Inventory`, and resolving
    an area's members needs the inventory: a member may be written relative to
    the namespace the area was declared in, and a selector is a query over
    labels, kinds and namespaces. Doing it here means the answer is computed
    exactly once per rendering and cannot differ from the one the validator
    reports as ``W142``/``W143`` — a warning that disagreed with the picture
    would be worse than no warning.

    An annotation whose references resolve to nothing keeps an empty entry
    rather than being dropped: whether that is fatal to the drawing is the
    renderer's decision (an area with no members is not drawn; a note with no
    anchor still says what it says), and this map does not make it.

    **A member that splits.** An area names *elements*, and at layer 3 an
    element may be drawn as several stacks (§23.1). An area that enclosed only
    the machine would draw its frame around the host and leave the containers
    running on it outside — a box labelled "tenant blue" with tenant blue's
    namespace outside it. So enclosing an element encloses every stack of it,
    appended after the element itself so that a *note*, which anchors to the
    first target, still points at the machine rather than at one of its
    containers.
    """
    targets: dict[str, tuple[str, ...]] = {}
    for fqn, area in annotations.areas:
        targets[fqn] = _with_stacks(area_members(inventory, fqn, area), nodes)
    for fqn, note in annotations.notes:
        anchor = note_anchor(inventory, fqn, note)
        targets[fqn] = () if anchor is None else _with_stacks((anchor,), nodes)
    return targets


def _with_stacks(elements: Sequence[str], nodes: Mapping[str, Node]) -> tuple[str, ...]:
    """``elements``, each followed by the stack nodes standing for the same machine."""
    stacks: dict[str, list[str]] = {}
    for fqn, node in nodes.items():
        view = node.netns
        if view is not None and not view.is_root:
            stacks.setdefault(view.element, []).append(fqn)
    if not stacks:
        return tuple(elements)
    return tuple(name for element in elements for name in (element, *stacks.get(element, ())))


# --------------------------------------------------------------------------- #
# Passive cross-connects
# --------------------------------------------------------------------------- #


def splice_patch_panels(
    edges: Sequence[Edge],
    panels: Mapping[str, PatchPanel],
    owners: Mapping[str, Device | Adapter | PatchPanel],
) -> tuple[tuple[Edge, ...], tuple[str, ...]]:
    """Replace every run through a patch panel with the single edge it is (§15.2).

    A panel is electrically transparent: a frame that enters ``front/7`` leaves
    ``rear/7`` unchanged, so at every layer but ``physical`` the run
    ``switch → front/7 ⇄ rear/7 → server`` *is* one link between the switch and
    the server. Walking it rather than deleting the panel is what keeps the
    result honest: the medium, the rate and the length of the run are properties
    of all of its segments, and only the walk knows which segments those are.

    The walk starts from a segment with at least one end on something active and
    follows couplers until it reaches another. Runs are discovered in edge
    order, so the direction of the resulting edge — and therefore the whole
    output — is deterministic.

    Args:
        edges: Every edge of the physical graph, panel segments included.
        panels: The patch panels, keyed by fully-qualified name.
        owners: Everything a cable may terminate on, for VLAN derivation.

    Returns:
        The spliced edges in the original order, and one message per run that
        does not arrive anywhere: a coupler with nothing patched on the far
        side, a panel port cabled twice, or a loop of panels. The validator
        reports each of those as well (``NV-P001``…``NV-P005``); the graph layer
        drops the run because ``--force`` must still produce a picture.
    """
    touching = {edge.id for edge in edges if _panel_ends(edge, panels)}
    if not touching:
        return tuple(edges), ()

    dangling: list[str] = []
    # ``(panel, port)`` -> the one segment landing on it. A second cable on the
    # same port is ``NV-P003``; the first one declared wins so the walk stays
    # deterministic, and the loser is dropped rather than silently followed —
    # splicing it too would reuse the segments beyond the panel and draw one
    # run twice.
    landings: dict[tuple[str, str], Edge] = {}
    contested: set[str] = set()
    for edge in edges:
        for panel, port in _panel_ends(edge, panels):
            existing = landings.setdefault((panel, port), edge)
            if existing is not edge:
                contested.add(edge.id)
                dangling.append(
                    f"{edge.id}: {panel}:{port} already terminates {existing.id}, so this "
                    f"segment is not spliced into a run"
                )

    spliced: list[Edge] = []
    consumed: set[str] = set()
    for edge in edges:
        if edge.id not in touching:
            spliced.append(edge)
            continue
        if edge.id in consumed or edge.id in contested:
            continue
        start = _active_end(edge, panels)
        if start is None:
            # Panel to panel: it is the middle of some run, or of none. Either
            # way it is not where a walk starts.
            continue
        run, problem = _walk_run(edge, start, panels, landings)
        consumed.update(segment.id for segment in run.segments)
        if problem is not None:
            dangling.append(problem)
            continue
        spliced.append(_splice(run, owners))

    for edge in edges:
        if edge.id in touching and edge.id not in consumed and edge.id not in contested:
            consumed.add(edge.id)
            dangling.append(
                f"{edge.id}: runs between patch panels only, so it reaches nothing that can "
                f"send or receive; it is drawn at layer 'physical' alone"
            )
    return tuple(spliced), tuple(dangling)


@dataclass(frozen=True, slots=True)
class _Run:
    """One walk from an active port through zero or more panels."""

    segments: tuple[Edge, ...]
    hops: tuple[PatchHop, ...] = ()
    #: ``(element, port)`` at either end, in the order the walk found them.
    source: tuple[str, str] = ("", "")
    target: tuple[str, str] = ("", "")


def _panel_ends(edge: Edge, panels: Mapping[str, PatchPanel]) -> tuple[tuple[str, str], ...]:
    """The ``(panel, port)`` ends of ``edge``; empty when it touches no panel."""
    if edge.kind is not EdgeKind.CABLE:
        return ()
    return tuple(
        (element, port)
        for element, port in ((edge.source, edge.source_port), (edge.target, edge.target_port))
        if element in panels
    )


def _active_end(edge: Edge, panels: Mapping[str, PatchPanel]) -> tuple[str, str] | None:
    """The end of ``edge`` that is not a panel, or ``None`` when both are."""
    for element, port in ((edge.source, edge.source_port), (edge.target, edge.target_port)):
        if element not in panels:
            return (element, port)
    return None


def _other_end(edge: Edge, element: str, port: str) -> tuple[str, str]:
    """The end of ``edge`` that is not ``(element, port)``."""
    if (edge.source, edge.source_port) == (element, port):
        return (edge.target, edge.target_port)
    return (edge.source, edge.source_port)


def _walk_run(
    first: Edge,
    start: tuple[str, str],
    panels: Mapping[str, PatchPanel],
    landings: Mapping[tuple[str, str], Edge],
) -> tuple[_Run, str | None]:
    """Follow ``first`` from ``start`` through the couplers it reaches.

    Returns the run and, when it does not arrive at a second active port, the
    reason — which is the same fact ``NV-P002`` or ``NV-P005`` reports about the
    inventory, said about this one run.
    """
    segments: list[Edge] = [first]
    hops: list[PatchHop] = []
    current, arrival = first, _other_end(first, *start)
    seen = {first.id}

    while arrival[0] in panels:
        panel_fqn, port = arrival
        egress = panels[panel_fqn].opposite(port)
        if egress is None:
            return _Run(tuple(segments), tuple(hops), start, arrival), (
                f"{current.id}: {panel_fqn}:{port} is coupled to nothing, so the run stops "
                f"inside the panel"
            )
        hops.append(PatchHop(panel=panel_fqn, ingress=port, egress=egress))
        following = landings.get((panel_fqn, egress))
        if following is None:
            return _Run(tuple(segments), tuple(hops), start, arrival), (
                f"{current.id}: nothing is patched into {panel_fqn}:{egress}, so the run "
                f"stops inside the panel"
            )
        if following.id in seen:  # pragma: no cover - a safety net, see below
            # Unreachable while every position terminates at most one cable:
            # the coupling is a bijection, so a walk over it cannot revisit a
            # segment. Kept because "cannot" here rests on a dedupe two dozen
            # lines above, and the cost of being wrong is a hang.
            return _Run(tuple(segments), tuple(hops), start, arrival), (
                f"{first.id}: the run loops back into {panel_fqn}:{egress}; a patch panel "
                f"cannot be patched into itself"
            )
        seen.add(following.id)
        segments.append(following)
        current, arrival = following, _other_end(following, panel_fqn, egress)

    return _Run(tuple(segments), tuple(hops), start, arrival), None


def _splice(run: _Run, owners: Mapping[str, Device | Adapter | PatchPanel]) -> Edge:
    """One edge standing for a whole run, with the attributes the run has.

    The rate is the slowest segment (a run is no faster than its worst leg), the
    length is the sum when every segment declares one, and the medium is what
    all of them agree on. The identity is the first segment's, which is the one
    a reader looking at the source end would name; :attr:`Edge.patch` carries
    the rest.
    """
    first = run.segments[0]
    if len(run.segments) == 1:
        return first

    source, source_port = run.source
    target, target_port = run.target
    speeds = [segment.speed for segment in run.segments if segment.speed is not None]
    lengths = [segment.length_m for segment in run.segments]
    media = {segment.medium for segment in run.segments}
    return Edge(
        id=first.id,
        kind=EdgeKind.CABLE,
        source=source,
        target=target,
        source_port=source_port,
        target_port=target_port,
        medium=media.pop() if len(media) == 1 else first.medium,
        speed=min(speeds) if speeds else None,
        label=next((segment.label for segment in run.segments if segment.label), None),
        length_m=sum(lengths) if all(value is not None for value in lengths) else None,  # type: ignore[arg-type]
        vlans=_link_vlans(owners[source], source_port, owners[target], target_port)
        if source in owners and target in owners
        else frozenset(),
        cable=first.cable,
        patch=PatchView(segments=tuple(segment.id for segment in run.segments), hops=run.hops),
    )


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #


def rack_elevations(
    inventory: Inventory, *, power: PowerPlan | None = None
) -> tuple[RackView, ...]:
    """One :class:`RackView` per rack the inventory places anything in (§3.2).

    Racks come out in first-seen order and their slots bottom-up, so an
    elevation reads the way the inventory does and the way the cabinet does.
    An element whose ``location`` names a rack but no ``position`` is left out:
    it is in the room, and an elevation cannot say where.

    Args:
        inventory: A tree loaded by :func:`~netviz.loader.load_tree`.
        power: The power plan to annotate the slots with (§17.5). Resolved from
            the inventory when not given; a caller that already has one passes it
            so the walk is not repeated.
    """
    placed: dict[tuple[str, str, str], list[RackSlot]] = {}
    heights: dict[tuple[str, str, str], int] = {}
    declared: dict[tuple[str, str, str], bool] = {}
    labels: dict[tuple[str, str, str], str] = {}
    # The elevation carries each occupant's draw and each PDU's utilisation
    # (§17.5), which is the one question an elevation cannot otherwise answer:
    # can this rack take another box. Resolved through the same plan the power
    # layer draws, so the two never disagree.
    plan = power if power is not None else power_plan(inventory)

    for fqn, element in inventory.elements.items():
        location: Location | None = element.metadata.location
        if location is None:
            continue
        key = location.rack_key
        if key is None:
            continue
        labels.setdefault(key, location.rack_label)
        slots = placed.setdefault(key, [])
        if location.rack_height is not None:
            # ``NV-U003`` refuses two elements that disagree; the tallest wins
            # here so a rendering under ``--force`` still holds everything.
            heights[key] = max(heights.get(key, 0), location.rack_height)
            declared[key] = True
        if location.position is None:
            continue
        slots.append(
            RackSlot(
                element=fqn,
                name=element.metadata.name,
                kind=element.kind,
                position=location.position,
                height=location.height,
                power=plan.node(fqn),
            )
        )

    views: list[RackView] = []
    for key, slots in placed.items():
        ordered = tuple(sorted(slots, key=lambda slot: (slot.position, slot.element)))
        tallest = max((slot.top for slot in ordered), default=1)
        views.append(
            RackView(
                key=key,
                label=labels[key],
                height=max(heights.get(key, 0), tallest, 1),
                slots=ordered,
                inferred_height=not declared.get(key, False),
            )
        )
    return tuple(views)


def _rack_view(inventory: Inventory) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """The placement graph: one node per rack, and no edges at all.

    There are no edges on purpose. A rack elevation answers "where is this
    bolted?", and a cable between two boxes is not an answer to that — drawing
    one would put a line between two elevations that says nothing about either.
    """
    return {view.id: Node.for_rack(view) for view in rack_elevations(inventory)}, ()


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #


def _power_view(
    inventory: Inventory, nodes: Mapping[str, Node]
) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """Turn the topology into the power distribution one (§17.5).

    Nodes are the PDUs and everything the inventory says draws or sources power;
    edges are the feeds. Everything else is discarded, because a cable is not a
    power path: two servers joined by a patch lead may be on opposite sides of the
    room electrically, and a PDU is joined to the boxes it feeds by cords no data
    diagram draws.

    A PDU is not in the topology at all — it owns no interfaces (§17.1) — so its
    node is built here rather than kept. Everything else is *kept*, so a switch in
    this view is the same node it is at layer 1: it carries its ports, its labels
    and its description, and only gains what it says about power.

    Two edge kinds, drawn distinctly and derived differently: an ``outlet`` feed
    is declared by the load, and a ``poe`` feed is derived by walking the load's
    uplink to the PSE port at the far end. Outlet feeds come first, in load order.
    """
    plan = power_plan(inventory)
    kept: dict[str, Node] = {}
    for fqn, summary in plan.nodes.items():
        existing = nodes.get(fqn)
        if existing is not None:
            kept[fqn] = replace(existing, power=summary)
            continue
        pdu = inventory.pdus.get(fqn)
        if pdu is not None:
            kept[fqn] = Node.for_pdu(fqn, pdu, summary)

    edges = tuple(
        Edge(
            id=feed.id,
            kind=EdgeKind.POE if feed.is_poe else EdgeKind.OUTLET,
            source=feed.source,
            target=feed.element,
            source_port=feed.outlet or feed.port,
            target_port=feed.peer_port or feed.psu,
            medium="",
            label=feed.psu or None,
            feed=feed,
        )
        for feed in plan.feeds
        if feed.source in kept and feed.element in kept
    )
    return kept, edges


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def _identity_view(inventory: Inventory) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """The membership graph: the identities, joined by what holds what (§19.3).

    None of the topology survives, and none of it should: a cable between two
    servers says nothing about who may log into either, and drawing both graphs
    at once would produce a picture in which neither is readable. This is the
    same decision the power view makes for the same reason.

    An edge runs from the group to the member, which is the direction the fact is
    written in and the direction a reader follows to answer "who is in this?". A
    member that does not resolve is simply not drawn: ``NV-S010`` is the place
    that says so, and ``--force`` has to keep producing a picture.
    """
    nodes = {fqn: Node.for_identity(fqn, element) for fqn, element in identities(inventory)}
    edges = tuple(
        Edge(
            id=f"{entry.group}#member{entry.index}",
            kind=EdgeKind.MEMBERSHIP,
            source=entry.group,
            target=entry.member,
            medium="",
        )
        for entry in identity_plan(inventory)
        if entry.is_identity and entry.member is not None
    )
    return nodes, edges


def _security_view(nodes: Mapping[str, Node]) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """The policy graph: the zones, joined by what may cross between them (§24.5).

    None of the topology survives, and none of it should. A cable between two
    hosts says nothing about whether the firewall between them lets anything
    through, and the whole point of a zone is that it is *not* an interface: the
    diagram a reader needs in order to argue about policy is one in which the
    boxes are the zones and the lines are the decisions.

    One node per zone, clustered by the device that declares it, so a reader
    sees which machine each partition belongs to. One edge per **zone pair the
    policy mentions**, directed from source to destination — because policy is
    asymmetric, and a picture that drew *lan to wan* and *wan to lan* as one line
    would have drawn the one thing a firewall exists to distinguish as the same
    thing.

    A device whose ``spec.firewall`` names a zone it does not declare cannot
    exist (``NV-B004``), so every edge here has both ends. What the view does
    mint is the two zones nobody declares: ``local`` for a rule about traffic
    terminating on the machine, and ``any`` for a rule that left a zone unset.
    Both appear only when the policy names them.

    A device with zones and no policy is drawn as its zones and no edges: it has
    a partition and nothing written over it yet, and that is worth seeing.
    """
    kept: dict[str, Node] = {}
    edges: list[Edge] = []
    for fqn, node in nodes.items():
        device = node.element if isinstance(node.element, Device) else None
        if device is None:
            continue
        views = security_views(fqn, device)
        if not views:
            continue
        for view in views:
            kept[view.id] = Node.for_zone(view, node)
        edges.extend(_policy_edge(policy) for policy in _policies_of(fqn, device))
    return kept, tuple(edges)


def security_views(fqn: str, device: Device) -> tuple[SecurityView, ...]:
    """Every zone of one device, declared ones first, or ``()`` for a device with none.

    Declared first and in declaration order, then ``local`` and ``any`` if the
    policy reaches for them, because that is the order a reader builds the
    picture in: here is how the machine is divided, and here are the two places
    traffic goes that are not one of the divisions.
    """
    spec = device.spec
    if not spec.zones and spec.firewall is None:
        return ()
    policy = spec.firewall
    rules = policy.rules if policy is not None else ()
    nat = policy.nat if policy is not None else ()

    def counted(name: str) -> tuple[int, int]:
        return (
            sum(1 for rule in rules if name in _zone_pair(rule)),
            sum(1 for entry in nat if name in _zone_pair(entry)),
        )

    views = [
        SecurityView(
            element=fqn,
            name=zone.name,
            owner_kind=device.kind,
            interfaces=tuple(zone.interfaces),
            description=zone.description,
            rules=counted(zone.name)[0],
            translations=counted(zone.name)[1],
        )
        for zone in spec.zones
    ]
    declared = {zone.name for zone in spec.zones}
    for name in (LOCAL_ZONE, ANY_ZONE):
        if name in declared:  # pragma: no cover - NV-B001 refuses both
            continue
        count, translations = counted(name)
        if count or translations:
            views.append(
                SecurityView(
                    element=fqn,
                    name=name,
                    owner_kind=device.kind,
                    rules=count,
                    translations=translations,
                )
            )
    return tuple(views)


def _zone_pair(rule: FirewallRule | NatRule) -> tuple[str, str]:
    """``(source, destination)`` with :data:`ANY_ZONE` for whichever is unset."""
    return (rule.src_zone or ANY_ZONE, rule.dst_zone or ANY_ZONE)


def _policies_of(fqn: str, device: Device) -> tuple[PolicyView, ...]:
    """One view per zone pair the device's policy mentions, in walk order.

    Grouped by pair and ordered by the *lowest priority in each group*, which is
    the order the chain reaches the pairs in. Two pairs that never interact are
    in an arbitrary order either way; two that do are drawn in the order the
    device would consider them, and that is the order worth having.
    """
    policy = device.spec.firewall
    if policy is None:
        return ()
    grouped: dict[tuple[str, str], list[str]] = {}
    verdicts: dict[tuple[str, str], set[bool]] = {}
    for rule in policy.rules_in():
        pair = _zone_pair(rule)
        grouped.setdefault(pair, []).append(rule.describe())
        if rule.action.is_terminal:
            verdicts.setdefault(pair, set()).add(rule.action.permits)
    translations: dict[tuple[str, str], list[str]] = {}
    for entry in policy.nat:
        pair = _zone_pair(entry)
        grouped.setdefault(pair, [])
        translations.setdefault(pair, []).append(entry.describe())

    return tuple(
        PolicyView(
            element=fqn,
            source=pair[0],
            target=pair[1],
            rules=tuple(grouped[pair]),
            translations=tuple(translations.get(pair, ())),
            verdict=_verdict(verdicts.get(pair, set())),
        )
        for pair in grouped
    )


def _verdict(permits: AbstractSet[bool]) -> str:
    """``open``, ``closed`` or ``conditional``, from what a pair's rules decide.

    A pair with no terminal rule at all is *conditional* rather than open or
    closed: everything in it marks or logs and carries on, so what happens to
    the traffic is decided somewhere further down the chain and this edge is not
    the place that says. The chain default is on the device, not on the pair.
    """
    if permits == {True}:
        return "open"
    if permits == {False}:
        return "closed"
    return "conditional"


def _policy_edge(policy: PolicyView) -> Edge:
    """One drawn zone pair (§24.5)."""
    return Edge(
        id=policy.id,
        kind=EdgeKind.POLICY,
        source=zone_node_id(policy.element, policy.source),
        target=zone_node_id(policy.element, policy.target),
        medium="",
        policy=policy,
    )


def _routed_view(
    nodes: Mapping[str, Node], subnets: Sequence[Subnet]
) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """Turn the physical graph into the routed one: one node per network stack.

    The physical edges are discarded: at layer 3 two stacks are adjacent because
    they share a prefix, and a cable between them is neither necessary (a route
    may cross three switches) nor sufficient (a trunk carries VLANs the two ends
    do not both route). Elements keep the VLAN membership derived from the
    physical graph, so ``--vlan 10`` still selects the broadcast domain rather
    than only the ports that spell the VLAN out.

    One node per **stack**, not per element (§23.1)
    ----------------------------------------------

    A machine running containers is several routing tables, and drawing one node
    for it says the wrong thing twice over. It claims the container's address is
    on the host — so ``netviz list subnets`` and the diagram disagree with the
    machine — and it makes a route that has to pass *through* the host to leave
    the container a path of length zero, which is why ``netviz path`` could not
    trace out of one (follow-up 23). The split here is exactly the one
    :func:`_netns_view` performs at layer 1, so the two views name the same
    stacks with the same ids (:func:`netns_node_id`).

    A machine with **one** stack is left exactly as it was: its node id is its
    fully-qualified name, it keeps every port, and it carries no
    :attr:`Node.netns`. That is what makes this change invisible to an inventory
    that declares no ``spec.netns`` — the overwhelming majority — and it is
    checked by the golden fixtures rather than asserted here.

    An edge keeps being identified by ``<element>:<interface>#<prefix>`` rather
    than by the stack it now leaves from. Interface names are unique within a
    device (``NV-I001``), so that is still unambiguous, and it means a stored
    waypoint or a highlighted hop survives a machine growing its first namespace.

    Nodes come out as the addressed stacks in inventory order — each machine's
    initial namespace first, then its declared ones in declaration order —
    followed by the subnets in prefix order; edges follow the stacks, then the
    interface order within each one, so the output reads the way the inventory
    does.
    """
    stacks = _stack_views(nodes)
    memberships: dict[str, list[tuple[Subnet, AddressPlacement]]] = {}
    for subnet in subnets:
        for member in subnet.members:
            memberships.setdefault(netns_node_id(member.element, member.netns), []).append(
                (subnet, member)
            )

    kept: dict[str, Node] = {}
    for fqn, node in nodes.items():
        views = stacks.get(fqn)
        if views is None:
            if fqn in memberships:
                kept[fqn] = node
            continue
        for view in views:
            if view.id not in memberships:
                # A stack holding no routable address is not at layer 3 at all,
                # the same rule an unaddressed machine has always been under.
                continue
            kept[view.id] = (
                replace(node, ports=view.ports, netns=view)
                if view.is_root
                else Node.for_stack(view, node)
            )

    edges: list[Edge] = []
    for node_id in kept:
        # One edge per (interface, prefix): a second address on the same
        # interface in the same prefix is another label, not another adjacency.
        grouped: dict[tuple[str, str, str, str], list[AddressPlacement]] = {}
        for subnet, member in sorted(
            memberships[node_id], key=lambda entry: (entry[1].index, entry[0].sort_key)
        ):
            key = (member.element, member.interface, subnet.node_id, subnet.prefix)
            grouped.setdefault(key, []).append(member)
        for (element, interface, subnet_node, prefix), placements in grouped.items():
            edges.append(
                Edge(
                    id=f"{element}:{interface}#{prefix}",
                    kind=EdgeKind.SUBNET,
                    source=node_id,
                    target=subnet_node,
                    source_port=interface,
                    medium="",
                    addresses=tuple(placement.address for placement in placements),
                    vlans=frozenset().union(*(placement.vlans for placement in placements)),
                )
            )

    for subnet in subnets:
        node = Node.for_subnet(subnet)
        kept[node.fqn] = node
    return kept, tuple(edges)


def _stack_views(nodes: Mapping[str, Node]) -> dict[str, tuple[NetnsView, ...]]:
    """The stacks of every machine that runs more than one, by element (§23.1).

    Absent from the mapping means "one stack", which is what every device was
    before §23 and what every device of an inventory that declares no
    ``spec.netns`` still is. Keeping that case out of the mapping rather than
    representing it as a single-entry tuple is deliberate: it is the case a
    caller must leave untouched, and a shape that cannot be confused with the
    split one is the cheapest way of saying so.
    """
    views: dict[str, tuple[NetnsView, ...]] = {}
    for fqn, node in nodes.items():
        if not node.is_element or not isinstance(node.element, Device):
            continue
        stacks = netns_views(fqn, node.element)
        if len(stacks) > 1:
            views[fqn] = stacks
    return views


def _netns_view(
    nodes: Mapping[str, Node], edges: Sequence[Edge]
) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """Open every machine up and draw the stacks inside it (§23.3).

    Every other layer treats a device as one box, which is the right abstraction
    right up to the point where the box is a container host: a machine running
    twelve namespaces has twelve routing tables, and a diagram that draws one
    node has drawn one of them. This view draws them all.

    The element node stays, and stands for the machine's **initial** namespace —
    it keeps the kind, the icon, the document link and the arrangement, because
    it is still the machine. Each declared namespace becomes a node beside it,
    and every node of one machine is put in a cluster named after that machine,
    so the boxes a reader sees are "inside this host".

    Three kinds of edge, and each says something no other layer can:

    * a **veth pair** joins the two namespaces its ends are in — the crossing
      itself, which at layer 1 is invisible because both ends are inside one
      box;
    * a **nesting** edge runs from a namespace to the one created inside it,
      which is how arbitrarily deep nesting is drawn without nested boxes;
    * a **cable** is kept, re-pointed at the namespace holding the interface it
      lands on, which is what answers the question the view exists for: how does
      the stack in this container reach the wire?

    A machine that declares no namespace and no veth pair is drawn only when
    something it is cabled to *is* opened up. It has one stack, drawn as it is
    at every other layer, so it is here as context — the wire the container
    reaches the world by has to arrive somewhere — and not as a subject.
    Everything further away is dropped, for the reason the routing view drops
    the cabling: a view that answers one question should not also be a map.
    """
    kept: dict[str, Node] = {}
    where: dict[tuple[str, str], str] = {}  # (element, interface) -> node id
    opened: set[str] = set()
    veths: list[Edge] = []
    nesting: list[Edge] = []

    for fqn, node in nodes.items():
        device = node.element if isinstance(node.element, Device) else None
        views = netns_views(fqn, device) if device is not None else ()
        if device is None or not views:
            kept[fqn] = node
            for port in node.ports:
                where[(fqn, port.name)] = fqn
            continue

        cluster = fqn
        opened.add(fqn)
        for view in views:
            for port in view.ports:
                where[(fqn, port.name)] = view.id
            if view.is_root:
                kept[fqn] = replace(node, ports=view.ports, netns=view, cluster=cluster)
                continue
            kept[view.id] = Node(
                fqn=view.id,
                name=view.label,
                kind=NETNS_KIND,
                namespace=node.namespace,
                ports=view.ports,
                vlans=frozenset().union(*(port.vlans for port in view.ports))
                if view.ports
                else frozenset(),
                type=NodeType.NETNS,
                netns=view,
                cluster=cluster,
            )
            nesting.append(_nesting_edge(fqn, view))

        veths.extend(
            _veth_edge(fqn, first, second, where) for first, second in device.spec.veth_pairs()
        )

    if not opened:
        return {}, ()

    physical = tuple(
        replace(
            edge,
            source=where.get((edge.source, edge.source_port), edge.source),
            target=where.get((edge.target, edge.target_port), edge.target),
        )
        for edge in edges
        if edge.source in kept and edge.target in kept
    )
    # Exactly one hop of context. ``subject`` is every stack of every machine
    # this view opened — ``cluster`` is the machine, which makes the test one
    # lookup rather than a walk back through ``where`` — and ``context`` is
    # whatever is directly joined to one of them. Deliberately not a fixed point:
    # a second hop is the rest of the network, and this view is not a map of it.
    subject = {fqn for fqn, node in kept.items() if node.cluster in opened}
    context = {
        far
        for edge in physical
        for near, far in ((edge.source, edge.target), (edge.target, edge.source))
        if near in subject
    }
    drawn = subject | context
    kept = {fqn: node for fqn, node in kept.items() if fqn in drawn}
    surviving = tuple(edge for edge in physical if edge.source in kept and edge.target in kept)
    return kept, (*surviving, *veths, *nesting)


def netns_views(fqn: str, device: Device) -> tuple[NetnsView, ...]:
    """Every namespace of one machine, the initial one first.

    ``()`` when the machine has only one stack, which is what leaves it drawn as
    the single box every other layer draws it as. "Only one" means the document
    says nothing about namespaces *and* nothing about veth pairs: a machine may
    run a pair entirely inside its initial namespace (``I005``), and that is
    still a fact this view exists to show.
    """
    spec = device.spec
    if not spec.netns and not spec.veth_pairs():
        return ()
    parents = spec.netns_parents()
    described = {entry.name: entry.description for entry in spec.netns}
    return tuple(
        NetnsView(
            element=fqn,
            name=name,
            path=netns_path(name, parents),
            parent=parents.get(name, ROOT_NETNS),
            ports=tuple(PortView.of(interface) for interface in spec.interfaces_in_netns(name)),
            description=described.get(name),
            device=device,
        )
        for name in spec.netns_names()
    )


def _veth_edge(
    fqn: str, first: Interface, second: Interface, where: Mapping[tuple[str, str], str]
) -> Edge:
    """The edge one veth pair is: a link between the two stacks it joins (§23.2)."""
    return Edge(
        id=f"{fqn}:{first.name}#veth",
        kind=EdgeKind.VETH,
        source=where.get((fqn, first.name), fqn),
        target=where.get((fqn, second.name), fqn),
        source_port=first.name,
        target_port=second.name,
        # No ``label``: the two port names are drawn from ``source_port`` and
        # ``target_port`` already, and a label repeating them put the same pair
        # on the diagram twice.
        medium="",
    )


def _nesting_edge(fqn: str, view: NetnsView) -> Edge:
    """The edge one ``spec.netns[].parent`` is: that stack created this one."""
    return Edge(
        id=f"{view.id}#nesting",
        kind=EdgeKind.NESTING,
        source=netns_node_id(fqn, view.parent),
        target=view.id,
        medium="",
    )


def _routing_view(
    nodes: Mapping[str, Node], inventory: Inventory, subnets: Sequence[Subnet]
) -> tuple[dict[str, Node], tuple[Edge, ...], tuple[str, ...]]:
    """Turn the physical graph into the control-plane one (§16.8).

    Nodes are the elements that take part in routing at all — anything declaring
    ``routing``, ``routes`` or ``vrfs`` — labelled with the AS and router id that
    identify them to their peers. Everything physical is discarded: two routers
    are adjacent here because they exchange routes, and a cable between them is
    neither necessary (an eBGP session may cross a provider) nor sufficient (a
    trunk carries VLANs neither end routes).

    Two kinds of edge, resolved differently because the protocols work
    differently:

    * a **BGP session** is declared, by address, on one or both ends; it is drawn
      once however many times it is declared, and a session whose address matches
      nothing in the inventory is reported in :attr:`Graph.dangling` rather than
      drawn to a node that does not exist (``NV-F013``);
    * an **OSPF adjacency** is *discovered*, so it is derived the way the protocol
      derives it: two interfaces that run OSPF in the same area and are addressed
      in one subnet form one. Deriving it from the subnets rather than from the
      cables is what makes it right for two routers facing each other across a
      layer-2 switch, which no cable joins directly.

    **One node per element, not per stack.** Layer 3 splits a machine into its
    network stacks (§23.1, :func:`_routed_view`); this view deliberately does
    not, because the thing it draws is declared per *machine*: ``spec.routing``,
    ``spec.vrfs`` and ``spec.routes`` are one statement about one document, and
    §23 gives no way to say "OSPF runs in the ``blue`` namespace". Drawing four
    boxes for a container host and putting one AS number on all of them would
    claim four routers where the inventory declares one. The consequence is in
    :func:`_ospf_adjacencies`: two stacks of one machine that share a prefix are
    *not* an adjacency, because there is one process, and the ``left == right``
    test that says so is a test on the element on purpose.

    Nodes come out in inventory order, BGP edges before OSPF ones.
    """
    views = {
        fqn: view
        for fqn, node in nodes.items()
        if node.is_element
        and isinstance(node.element, Device)
        and not (view := _routing_of(fqn, node.element)).is_empty
    }
    kept = {
        fqn: replace(nodes[fqn], routing=view, cluster=_vrf_cluster(view))
        for fqn, view in views.items()
    }

    addresses = _address_index(inventory)
    sessions, dangling = _bgp_adjacencies(views, inventory, addresses)
    edges = [
        *(_adjacency_edge(view) for view in sessions),
        *(_adjacency_edge(view) for view in _ospf_adjacencies(views, subnets)),
    ]
    return kept, tuple(edges), dangling


def _routing_of(fqn: str, device: Device) -> RoutingView:
    """What one device contributes to the routing layer."""
    routing = device.spec.routing
    ospf = routing.ospf if routing is not None else None
    bgp = routing.bgp if routing is not None else None
    router_ids = routing.router_ids if routing is not None else ()
    return RoutingView(
        element=fqn,
        asn=bgp.asn if bgp is not None else None,
        router_id=str(router_ids[0]) if router_ids else None,
        area=ospf.area if ospf is not None else None,
        ospf_interfaces=tuple(ospf.interfaces) if ospf is not None else (),
        vrfs=tuple((vrf.name, vrf.rd) for vrf in device.spec.vrfs),
        tables=tuple((table.name, table.id) for table in device.spec.route_tables),
        bound_vrfs=tuple(
            dict.fromkeys(
                interface.vrf for interface in device.interfaces if interface.vrf is not None
            )
        ),
        routes=tuple(route.describe() for route in device.spec.routes),
        policy=_policy_of(device),
    )


def _policy_of(device: Device) -> tuple[str, ...]:
    """The policy database, rendered, in the order the device walks it (§16.4).

    Sorted by priority rather than kept in declaration order, because the
    diagram's job here is to answer "what happens to this packet", and that is
    the walk. A rule installed in both families appears once: it is one rule, and
    listing it twice would say the device has more policy than it has.
    """
    return tuple(
        rule.describe()
        for rule in sorted(device.spec.routing_policy, key=lambda rule: rule.priority)
    )


def _vrf_cluster(view: RoutingView) -> str:
    """The VRF box this router is drawn in, or ``""`` for none.

    A router with interfaces in exactly one instance is drawn inside it. A router
    that straddles several belongs to no box — the same choice a cross-site subnet
    gets at layer 3, and for the same reason: putting it in one of them would
    claim something the inventory does not say. Its instances are named on the
    label either way, so nothing is lost but the frame.
    """
    return view.bound_vrfs[0] if len(view.bound_vrfs) == 1 else ""


def _address_index(inventory: Inventory) -> dict[str, tuple[str, str]]:
    """Every configured address -> ``(element, interface)``, first one winning.

    Keyed by the *text* of the address rather than by an :mod:`ipaddress` object
    because that is what a peer is looked up by here and what an edge label
    carries; the text is already canonical, since the model normalises it.

    Loopback and link-local addresses are left out for the reason
    :func:`is_routable_address` gives: ``127.0.0.1`` is on every machine, so a
    session pointed at one resolves to whichever host was loaded first.
    """
    index: dict[str, tuple[str, str]] = {}
    for fqn, element in inventory.elements.items():
        if not isinstance(element, (Device, Adapter)):
            continue
        for interface in element.interfaces:
            for address in interface.addresses():
                if is_routable_address(address):
                    index.setdefault(str(address.ip), (fqn, interface.name))
    return index


def _bgp_adjacencies(
    views: Mapping[str, RoutingView],
    inventory: Inventory,
    addresses: Mapping[str, tuple[str, str]],
) -> tuple[tuple[AdjacencyView, ...], tuple[str, ...]]:
    """One :class:`AdjacencyView` per BGP session, and one message per lost peer.

    A session both ends declare is one session, so it is drawn once: the pair is
    keyed on the two elements and the address family, which keeps a v4 and a v6
    session between the same routers as the two sessions they are. The first
    declaration in inventory order wins, so the direction of the edge — and the
    order of its AS pair — is deterministic.
    """
    seen: dict[tuple[str, str, int], AdjacencyView] = {}
    dangling: list[str] = []
    for fqn, view in views.items():
        device = inventory.devices.get(fqn)
        routing = device.spec.routing if device is not None else None
        bgp = routing.bgp if routing is not None else None
        if bgp is None:
            continue
        for neighbor in bgp.neighbors:
            peer = addresses.get(str(neighbor.address))
            if peer is None:
                dangling.append(
                    f"{fqn}: BGP neighbour {neighbor.address} (AS {neighbor.remote_asn}) is not "
                    f"an address of this inventory, so the session is drawn to nothing"
                )
                continue
            peer_fqn, peer_port = peer
            if peer_fqn == fqn:
                # A session pointed at the router's own address: nothing to draw
                # a line between. Reported by the validator as an off-link peer
                # or simply as a mistake; the picture just leaves it out.
                continue
            first, second = sorted((fqn, peer_fqn))
            key = (first, second, neighbor.version)
            if key in seen:
                continue
            peer_view = views.get(peer_fqn)
            seen[key] = AdjacencyView(
                protocol=EdgeKind.BGP.value,
                source=fqn,
                target=peer_fqn,
                peer_address=str(neighbor.address),
                target_port=peer_port,
                asns=(
                    (view.asn, peer_view.asn)
                    if view.asn is not None and peer_view is not None and peer_view.asn is not None
                    else ((view.asn,) if view.asn is not None else ())
                ),
                description=neighbor.description,
            )
    return tuple(seen.values()), tuple(dangling)


def _ospf_adjacencies(
    views: Mapping[str, RoutingView], subnets: Sequence[Subnet]
) -> tuple[AdjacencyView, ...]:
    """Every OSPF adjacency the addressing implies, in subnet then element order.

    Two routers are adjacent when they run OSPF in the same area on interfaces
    addressed in one subnet. Both halves matter: a shared subnet without the
    interface being in the process is not an adjacency, and two interfaces in one
    process in different areas do not form one either.

    One edge per pair of routers per area, however many subnets and families they
    share — a dual-stacked link is one adjacency drawn once, not two.
    """
    adjacencies: dict[tuple[str, str, str], AdjacencyView] = {}
    for subnet in subnets:
        speakers = [
            (member.element, member.interface, view)
            for member in subnet.members
            if (view := views.get(member.element)) is not None
            and member.interface in view.ospf_interfaces
        ]
        for (left, left_port, left_view), (right, right_port, _) in itertools.combinations(
            speakers, 2
        ):
            area = left_view.area
            # ``left == right`` is a test on the *element*, not on the stack it
            # is addressed in: a machine's two namespaces sharing a prefix are
            # one OSPF process talking to itself, not an adjacency. See
            # :func:`_routing_view` for why this view is not split by stack.
            if left == right or area is None or area != views[right].area:
                continue
            source, source_port, target, target_port = (
                (left, left_port, right, right_port)
                if left <= right
                else (right, right_port, left, left_port)
            )
            adjacencies.setdefault(
                (source, target, area),
                AdjacencyView(
                    protocol=EdgeKind.OSPF.value,
                    source=source,
                    target=target,
                    source_port=source_port,
                    target_port=target_port,
                    area=area,
                ),
            )
    return tuple(adjacencies.values())


def _adjacency_edge(view: AdjacencyView) -> Edge:
    """The edge one adjacency is drawn as.

    ``medium`` is empty for the reason a tunnel's is: a routing session runs over
    whatever the rest of the diagram provides, and claiming a wire would claim one
    that is not this edge's.
    """
    return Edge(
        id=view.id,
        kind=EdgeKind.BGP if view.protocol == EdgeKind.BGP.value else EdgeKind.OSPF,
        source=view.source,
        target=view.target,
        source_port=view.source_port,
        target_port=view.target_port,
        medium="",
        label=view.label,
        adjacency=view,
    )


def _overlay_view(
    nodes: Mapping[str, Node], tunnels: Sequence[TunnelView]
) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """Turn the physical graph into the encapsulation one (§14.5).

    Every tunnel becomes a node, because nesting is a relation between two
    *links* and a link cannot end on a link: ``over`` is drawable only once the
    inner tunnel has a node to start from. The elements are kept as the ground
    the stack stands on — a tunnel with nothing at either end says nothing —
    and everything physical is discarded: at this layer two elements are
    adjacent because they agreed to encapsulate, not because a cable runs
    between them.

    Nodes come out as the terminating elements in inventory order, followed by
    the tunnels in inventory order; edges follow the tunnels, each one's
    endpoints first and its underlay last.
    """
    members = {element for view in tunnels for element in view.elements}
    kept = {fqn: node for fqn, node in nodes.items() if node.is_element and fqn in members}
    for view in tunnels:
        kept[view.id] = Node.for_tunnel(view)

    edges: list[Edge] = []
    for view in tunnels:
        edges.extend(_endpoint_edges(view))
        if view.over is not None:
            edges.append(
                Edge(
                    id=f"{view.fqn}#over",
                    kind=EdgeKind.ENCAPSULATION,
                    source=view.id,
                    target=f"{TUNNEL_ID_PREFIX}{view.over}",
                    medium="",
                    label=view.type,
                    tunnel=view,
                )
            )
    return kept, tuple(edges)


def _tunnel_topology(
    tunnels: Sequence[TunnelView], ports: Mapping[str, tuple[PortView, ...]]
) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """The nodes and edges tunnels contribute to the layer-1/layer-2 graph.

    A point-to-point tunnel is an edge: it joins two elements, exactly as a
    cable does, and giving it a node would put a box in the middle of every VPN
    on the diagram. A tunnel with three or more endpoints has no such shape, so
    it becomes a node with one leg per endpoint — the same choice a subnet gets
    at layer 3, for the same reason.
    """
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    for view in tunnels:
        if view.is_multipoint:
            nodes[view.id] = Node.for_tunnel(view)
            edges.extend(_endpoint_edges(view, ports))
            continue
        first, second = view.ends
        edges.append(
            Edge(
                id=view.fqn,
                kind=EdgeKind.TUNNEL,
                source=first.element,
                target=second.element,
                source_port=first.interface,
                target_port=second.interface,
                # A tunnel runs over whatever the underlay provides; claiming a
                # medium would claim a wire that is not this element's.
                medium="",
                label=view.label,
                vlans=_tunnel_vlans(view, ports),
                tunnel=view,
            )
        )
    return nodes, tuple(edges)


def _endpoint_edges(
    view: TunnelView, ports: Mapping[str, tuple[PortView, ...]] | None = None
) -> Iterator[Edge]:
    """One edge per endpoint of a tunnel drawn as a node."""
    vlans = _tunnel_vlans(view, ports) if ports is not None else frozenset()
    for end in view.ends:
        yield Edge(
            id=f"{view.fqn}#{end}",
            kind=EdgeKind.TUNNEL,
            source=end.element,
            target=view.id,
            source_port=end.interface,
            medium="",
            vlans=vlans,
            tunnel=view,
        )


def _tunnel_vlans(
    view: TunnelView, ports: Mapping[str, tuple[PortView, ...]] | None
) -> frozenset[int]:
    """The VLANs a tunnel carries between its ends.

    A **layer-2** tunnel — VXLAN, Geneve, L2TP — extends a broadcast domain
    across the underlay; that is the entire reason it exists, so it carries the
    VLANs its endpoints are configured for exactly as a trunk does. A
    **layer-3** tunnel carries packets, so it carries no VLAN at all, however
    the ports at either end happen to be configured.
    """
    if ports is None or view.layer != 2:
        return frozenset()
    carried: set[int] = set()
    for end in view.ends:
        for port in ports.get(end.element, ()):
            if port.name == end.interface:
                carried |= port.vlans
    return frozenset(carried)


def resolve_tunnels(inventory: Inventory) -> tuple[tuple[TunnelView, ...], tuple[str, ...]]:
    """Resolve every ``tunnel`` document against ``inventory`` (§14.3).

    Endpoints are resolved to fully-qualified names and ``over`` to the tunnel
    it names, then the encapsulation chain of each tunnel is walked outwards to
    give it a depth, a stack (``("vxlan", "ipsec")``) and the nearest underlay
    that encrypts.

    Returns:
        The resolved tunnels in inventory load order, and one message per
        problem — an endpoint that names nothing, an ``over`` that names no
        tunnel, a chain that loops. Problems are *reported*, never raised: the
        validator refuses such an inventory (``E016``…``E019``), and the graph
        layer still has to cope because ``--force`` exists.
    """
    owners = inventory.interface_owners
    dangling: list[str] = []
    partial: dict[str, TunnelView] = {}

    for fqn, tunnel in inventory.tunnels.items():
        namespace = namespace_of(fqn)
        ends: list[TunnelEnd] = []
        missing: list[str] = []
        for ref in tunnel.endpoints:
            owner_fqn = inventory.resolve_fqn(ref.device, namespace=namespace)
            owner = owners.get(owner_fqn) if owner_fqn is not None else None
            if owner_fqn is None or owner is None or owner.interface(ref.interface) is None:
                missing.append(str(ref))
                continue
            ends.append(TunnelEnd(element=owner_fqn, interface=ref.interface))
        if missing:
            dangling.append(f"{fqn}: unresolved endpoint(s): {', '.join(missing)}")
        if len(ends) < 2:
            dangling.append(f"{fqn}: fewer than two endpoints resolve, so it is not drawn")
            continue

        over: str | None = None
        declared = tunnel.spec.over
        if declared is not None:
            target = inventory.resolve_fqn(declared, namespace=namespace)
            if target is None or target not in inventory.tunnels:
                dangling.append(f"{fqn}: 'over' names no known tunnel: {declared!r}")
            else:
                over = target

        partial[fqn] = TunnelView(
            fqn=fqn,
            name=tunnel.metadata.name,
            namespace=namespace,
            tunnel=tunnel,
            ends=tuple(ends),
            over=over,
        )

    # An underlay that was itself dropped cannot be drawn under anything.
    for fqn, view in partial.items():
        if view.over is not None and view.over not in partial:
            dangling.append(f"{fqn}: 'over' names {view.over!r}, which is not drawn")
            partial[fqn] = replace(view, over=None)

    reported: set[frozenset[str]] = set()
    views: list[TunnelView] = []
    for fqn in partial:
        chain, cycle = _encapsulation_chain(fqn, partial)
        # Every tunnel in a loop walks the same loop, rotated to start at
        # itself, so the members are what identifies it — not the order. The
        # first one in inventory order reports it, which keeps the message
        # deterministic without repeating it once per member.
        if cycle is not None and frozenset(cycle) not in reported:
            reported.add(frozenset(cycle))
            dangling.append(
                "tunnel encapsulation loops: " + " over ".join(cycle) + " over " + cycle[0]
            )
        outer = chain[1:]
        views.append(
            replace(
                partial[fqn],
                depth=len(outer),
                stack=tuple(partial[step].type for step in chain),
                encrypted_by=next(
                    (step for step in outer if partial[step].encrypted),
                    None,
                ),
            )
        )
    return tuple(views), tuple(dangling)


def _encapsulation_chain(
    fqn: str, tunnels: Mapping[str, TunnelView]
) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
    """``fqn`` and every tunnel it runs inside, innermost first.

    Returns the chain and, when the ``over`` references loop, the members of
    that loop in the order they were walked — so the caller can report the cycle
    once rather than once per tunnel in it.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = fqn
    while current is not None and current in tunnels:
        if current in seen:
            start = chain.index(current)
            return tuple(chain), tuple(chain[start:])
        seen.add(current)
        chain.append(current)
        current = tunnels[current].over
    return tuple(chain), None


def _build_edges(inventory: Inventory) -> tuple[tuple[Edge, ...], tuple[str, ...]]:
    """Cables first (load order), then adapter attachments (§8.2).

    Cable endpoints resolve against :attr:`Inventory.cable_owners` rather than
    :attr:`Inventory.interface_owners`, because a patch panel port terminates a
    cable exactly as a device port does (§15.1). Adapter attachments still
    resolve against the active elements: an adapter hangs off a host, and a
    panel is not one (``NV-P004``).
    """
    edges: list[Edge] = []
    dangling: list[str] = []
    owners = inventory.cable_owners

    for fqn, cable in inventory.cables.items():
        namespace = namespace_of(fqn)
        resolved = [
            inventory.resolve_fqn(ref.device, namespace=namespace) for ref in cable.endpoints
        ]
        left, right = resolved
        if left is None or right is None or left not in owners or right not in owners:
            missing = ", ".join(
                str(ref)
                for ref, target in zip(cable.endpoints, resolved, strict=True)
                if target is None or target not in owners
            )
            dangling.append(
                f"{fqn}: unresolved endpoint(s): {missing or 'not a cableable element'}"
            )
            continue
        spec = cable.spec
        edges.append(
            Edge(
                id=fqn,
                kind=EdgeKind.CABLE,
                source=left,
                target=right,
                source_port=cable.endpoints[0].interface,
                target_port=cable.endpoints[1].interface,
                medium=spec.medium.value,
                speed=spec.speed,
                label=spec.label,
                length_m=spec.length_m,
                vlans=_link_vlans(
                    owners[left],
                    cable.endpoints[0].interface,
                    owners[right],
                    cable.endpoints[1].interface,
                ),
                cable=cable,
                wireless=_link_wireless(
                    (left, owners[left], cable.endpoints[0].interface),
                    (right, owners[right], cable.endpoints[1].interface),
                )
                if spec.medium is Medium.WIRELESS
                else None,
            )
        )

    for fqn, adapter in inventory.adapters.items():
        host = adapter.upstream.attached_to
        if host is None:
            continue
        host_fqn = inventory.resolve_fqn(host, namespace=namespace_of(fqn))
        if host_fqn is None or host_fqn not in owners or host_fqn in inventory.patchpanels:
            dangling.append(f"{fqn}: upstream.attached_to names no known element: {host!r}")
            continue
        edges.append(
            Edge(
                id=f"{fqn}#upstream",
                kind=EdgeKind.ATTACHMENT,
                source=host_fqn,
                target=fqn,
                source_port="",
                target_port=adapter.upstream.name,
                # §8.2: the attachment is a copper edge carrying the bus rate.
                medium="copper",
                speed=adapter.upstream.speed,
                label=adapter.upstream.type.value,
                vlans=frozenset().union(
                    *(
                        interface.vlan.vlan_ids()
                        for interface in adapter.interfaces
                        if interface.vlan is not None
                    )
                )
                if any(interface.vlan is not None for interface in adapter.interfaces)
                else frozenset(),
                adapter=adapter,
            )
        )

    return tuple(edges), tuple(dangling)


def _link_vlans(
    left_owner: Device | Adapter | PatchPanel,
    left_port: str,
    right_owner: Device | Adapter | PatchPanel,
    right_port: str,
) -> frozenset[int]:
    """The VLANs a cable carries, from the configuration of its two ends.

    Two ports that both declare VLAN membership carry the intersection: a trunk
    pruned to 10,20 facing a trunk carrying 20,30 passes only VLAN 20. When one
    end is an untagged host port it declares nothing, and the link carries
    whatever the configured end says — which is what puts the host into the
    access VLAN it actually sits in.

    An empty intersection means the two ends disagree (``E005``); the union is
    used instead so the link is still drawn with the VLANs involved rather than
    silently losing its annotation.
    """
    left = _port_vlans(left_owner, left_port)
    right = _port_vlans(right_owner, right_port)
    if not left:
        return right
    if not right:
        return left
    return left & right or left | right


#: One end of a link, as :func:`_link_wireless` needs it.
_LinkEnd: TypeAlias = "tuple[str, Device | Adapter | PatchPanel, str]"


def _link_wireless(left: _LinkEnd, right: _LinkEnd) -> WirelessView | None:
    """What the association on a ``medium: wireless`` cable is (§6.2.6).

    The two ends do not carry the same information and are not interchangeable:
    the access point owns the frequency and the list of SSIDs it beacons, while
    the client owns the one fact the AP cannot state — *which* of those SSIDs
    this particular link is on. The view takes each from whichever end knows it,
    and falls back to the other end when only one is modelled at all.

    ``None`` when neither end declares a ``wireless`` block: the link is then no
    more than ``medium: wireless``, and inventing an empty annotation for it
    would put a stray ``@`` on the diagram.
    """
    radios = [
        (f"{fqn}:{port}", interface.wireless)
        for fqn, owner, port in (left, right)
        if (interface := owner.interface(port)) is not None and interface.wireless is not None
    ]
    if not radios:
        return None

    access_points = [(port, radio) for port, radio in radios if radio.role.is_ap]
    clients = [(port, radio) for port, radio in radios if not radio.role.is_ap]
    # The frequency is the access point's; with no AP end — which is ``E028`` —
    # any radio that states one is better than dropping the annotation.
    tuned = next(
        (radio for _, radio in [*access_points, *radios] if radio.band is not None),
        None,
    )
    joined = next((radio.ssids for _, radio in clients if radio.ssids), ())
    beaconed = next((radio.ssids for _, radio in access_points if radio.ssids), ())
    return WirelessView(
        ssids=joined or beaconed,
        band=tuned.band.value if tuned is not None and tuned.band is not None else None,
        channel=tuned.channel if tuned is not None else None,
        width_mhz=tuned.width_mhz if tuned is not None else None,
        access_point=access_points[0][0] if len(access_points) == 1 else "",
    )


def _port_vlans(owner: Device | Adapter | PatchPanel, port: str) -> frozenset[int]:
    interface = owner.interface(port)
    if interface is None or interface.vlan is None:
        return frozenset()
    return interface.vlan.vlan_ids()


def _node_vlans(
    inventory: Inventory,
    ports: Mapping[str, tuple[PortView, ...]],
    edges: Sequence[Edge],
) -> dict[str, frozenset[int]]:
    """Union each element's own ports, its VLAN database and its links."""
    membership: dict[str, set[int]] = {fqn: set() for fqn in ports}

    for fqn, views in ports.items():
        for view in views:
            membership[fqn] |= view.vlans

    for fqn, device in inventory.devices.items():
        # §6.4: a VLAN in the device database exists on that device even when no
        # port references it yet.
        membership[fqn] |= {vlan.id for vlan in device.spec.vlans}

    for edge in edges:
        for endpoint in (edge.source, edge.target):
            if endpoint in membership:
                membership[endpoint] |= edge.vlans

    _propagate_through_adapters(membership, edges)
    return {fqn: frozenset(vlans) for fqn, vlans in membership.items()}


def _propagate_through_adapters(membership: dict[str, set[int]], edges: Sequence[Edge]) -> None:
    """Share VLAN membership across every ``attached_to`` edge.

    A laptop whose only port is a USB dongle declares no VLAN of its own, yet it
    sits in whatever VLAN the dongle's downstream cable lands in — §8.2 is
    explicit that collapsing the adapter must not change connectivity, so the
    host and its adapter have to answer ``--vlan 10`` the same way. Chains
    (dongle in a dock) are handled by repeating until nothing changes; the
    number of passes is bounded by the length of the longest chain.
    """
    attachments = [edge for edge in edges if edge.kind is EdgeKind.ATTACHMENT]
    for _ in range(len(attachments)):
        changed = False
        for edge in attachments:
            if edge.source not in membership or edge.target not in membership:
                continue
            shared = membership[edge.source] | membership[edge.target]
            for endpoint in (edge.source, edge.target):
                if membership[endpoint] != shared:
                    membership[endpoint] = set(shared)
                    changed = True
        if not changed:
            return


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


#: The element kinds that become graph *nodes*, and therefore the kinds a filter
#: may select. A cable is an edge, and a tunnel is one too below the ``overlay``
#: layer — where it does become a node it is derived from the elements it joins,
#: exactly as a subnet is, so it is kept whenever one of them survives rather
#: than selected in its own right.
#:
#: Lives here rather than in the CLI because :class:`FilterSpec` is what
#: consumes it: ``--kind``, ``[render] kind`` and the completion of both read
#: this one tuple.
NODE_KINDS: Final[tuple[str, ...]] = (
    "switch",
    "router",
    "firewall",
    "hub",
    "computer",
    "server",
    "adapter",
    "patchpanel",
    "pdu",
    USER_KIND,
    GROUP_KIND,
)


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """Which part of the graph to draw.

    Values *within* one field are alternatives (``--kind switch --kind router``
    keeps both); different fields are combined with AND, so
    ``--namespace sites/north --kind switch`` keeps the switches of that site
    only. An unset field selects everything.

    Every field selects *elements*. A layer-3 subnet node is derived rather than
    declared — it has no namespace, no kind and no name of its own — so it is
    kept exactly as long as one selected element still has an address in it, and
    it then lists only those members. ``--kind router`` at layer 3 therefore
    draws the routers and the prefixes they route, not a set of empty boxes.
    """

    #: Namespace prefixes; a node matches its own namespace and any descendant.
    namespaces: tuple[str, ...] = ()
    vlans: frozenset[int] = frozenset()
    kinds: tuple[str, ...] = ()
    #: Shell-style globs matched against the fully-qualified *and* the short name.
    names: tuple[str, ...] = ()
    #: Keep only the neighbourhood of this element, at most :attr:`depth` hops away.
    neighbors_of: str | None = None
    depth: int = 1
    #: The elements a ``--select`` query matched, or ``None`` when none was
    #: given. **Already answered**: the query is parsed and evaluated by
    #: :mod:`netviz.query` against the unfiltered graph, and what arrives here
    #: is the set of fully-qualified names it selected.
    #:
    #: The layering is deliberate and is what keeps one implementation of
    #: *selects*. A query can traverse and can ask about interfaces, so it must
    #: see the whole graph; the fields above cannot express it, so they are not
    #: asked to. Both then narrow the same graph in one place, AND-ed together
    #: like every other pair of fields — ``--kind switch --select 'has vrf'``
    #: keeps the switches that have one.
    selected: frozenset[str] | None = None
    #: The query as written, for a diagnostic and for ``describe``.
    select: str | None = None

    @property
    def is_empty(self) -> bool:
        """Would this filter keep the graph unchanged?

        A spec carrying an *unanswered* query is not empty, even though there is
        nothing yet to narrow with: a caller that skipped the filter on the
        strength of this would silently render the whole inventory for a query
        that selects three devices.
        """
        return not (
            self.namespaces
            or self.vlans
            or self.kinds
            or self.names
            or self.neighbors_of
            or self.select is not None
            or self.selected is not None
        )

    def describe(self) -> str:
        """A one-line summary for diagnostics, e.g. ``kind=switch, vlan=10``."""
        parts: list[str] = []
        if self.select is not None:
            parts.append(f"select={self.select}")
        if self.namespaces:
            parts.append(f"namespace={','.join(self.namespaces)}")
        if self.vlans:
            parts.append(f"vlan={','.join(str(vlan) for vlan in sorted(self.vlans))}")
        if self.kinds:
            parts.append(f"kind={','.join(self.kinds)}")
        if self.names:
            parts.append(f"name={','.join(self.names)}")
        if self.neighbors_of:
            parts.append(f"neighbors-of={self.neighbors_of} depth={self.depth}")
        return ", ".join(parts) if parts else "none"


class UnknownElementError(LookupError):
    """``--neighbors-of`` named an element the graph does not hold."""

    def __init__(self, name: str, candidates: Sequence[str] = ()) -> None:
        self.name = name
        self.candidates = tuple(candidates)
        super().__init__(name)


def filter_graph(graph: Graph, spec: FilterSpec) -> Graph:
    """Narrow ``graph`` to the nodes ``spec`` selects, keeping the edges between them.

    ``--neighbors-of`` is applied first and traverses the *whole* graph, so a
    switch two hops away is still reachable through nodes the other filters
    would have removed. The remaining predicates then apply to that
    neighbourhood. At layer 3 the traversal runs over subnet nodes, so depth 1
    from a device reaches the prefixes it is addressed in and depth 2 the other
    devices in them.

    Raises:
        UnknownElementError: ``spec.neighbors_of`` names no node.
    """
    if spec.select is not None and spec.selected is None:
        # The query has not been answered, and this function cannot answer it:
        # doing so needs the vocabulary, the parser and the traversal, none of
        # which the renderer knows about. :func:`netviz.query.apply.narrow` is
        # the entry point that answers it and then calls this; a spec that got
        # here unanswered came from a caller that bypassed it, and rendering the
        # whole inventory instead would be a silently wrong picture.
        raise ValueError(
            f"the --select query {spec.select!r} has not been answered; "
            "call netviz.query.apply.narrow() rather than filter_graph()"
        )
    if spec.is_empty:
        return graph

    seed: str | None = None
    reachable: set[str] | None = None
    if spec.neighbors_of is not None:
        seed = _resolve_node(graph, spec.neighbors_of)
        reachable = _neighbourhood(graph, seed, spec.depth)

    # Every predicate is about a device or an adapter; subnet and tunnel nodes
    # follow from whichever of those survive (see FilterSpec).
    kept = {fqn for fqn, node in graph.nodes.items() if node.is_element}
    if reachable is not None:
        kept &= reachable
    if spec.selected is not None:
        # A query may legitimately select a derived node — a layer-3 prefix, a
        # tunnel — and those are decided below, by _kept_derived, exactly as
        # they are for every other field. Here only the elements are narrowed.
        kept &= spec.selected
    if spec.namespaces:
        kept &= {fqn for fqn in kept if _in_namespaces(graph.nodes[fqn].namespace, spec.namespaces)}
    if spec.kinds:
        kept &= {fqn for fqn in kept if graph.nodes[fqn].kind in spec.kinds}
    if spec.names:
        kept &= {fqn for fqn in kept if _matches_name(graph.nodes[fqn], spec.names)}
    if spec.vlans:
        kept &= {fqn for fqn in kept if graph.nodes[fqn].vlans & spec.vlans}

    derived = _kept_derived(graph, kept, spec=spec, reachable=reachable, seed=seed)
    nodes = {
        fqn: derived.get(fqn, node)
        for fqn, node in graph.nodes.items()
        if fqn in kept or fqn in derived
    }
    edges = tuple(
        edge
        for edge in graph.edges
        if edge.source in nodes
        and edge.target in nodes
        # With a VLAN filter, a link that carries none of the requested VLANs is
        # not part of that broadcast domain even when both its ends are.
        and (not spec.vlans or not edge.vlans or edge.vlans & spec.vlans)
    )
    return Graph(
        root=graph.root,
        nodes=nodes,
        edges=edges,
        layer=graph.layer,
        dangling=graph.dangling,
        sources=graph.sources,
        geometry=_narrowed(graph.geometry, nodes, edges),
        # Carried whole. A filter narrows what an annotation *encloses* — which
        # is decided against the surviving nodes when the drawing is made — and
        # not which annotations the view has; an area that loses every member is
        # dropped by the renderer, not here.
        annotations=graph.annotations,
        annotation_targets=graph.annotation_targets,
    )


def _kept_derived(
    graph: Graph,
    kept: Iterable[str],
    *,
    spec: FilterSpec,
    reachable: set[str] | None,
    seed: str | None,
) -> dict[str, Node]:
    """The surviving subnet, tunnel and rack nodes, narrowed to the members left.

    A prefix nobody selected still has is dropped, and so is a tunnel with no
    endpoint left: an empty box would claim a broadcast domain — or an overlay —
    the diagram no longer shows anything in. There are two exceptions, and both
    are "the reader asked for this node itself": the node named directly by
    ``--neighbors-of``, and a node a ``--select`` query matched. The second is
    what makes ``--select 'prefix in 10.1.0.0/16'`` at layer 3 draw those
    prefixes rather than nothing — a query can name a derived node, which none
    of the flags can, so it must be able to keep one.

    A rack is the one derived node whose members are *not* nodes of the graph:
    at :attr:`Layer.RACK` the elements are slots inside the cabinet rather than
    boxes beside it, so ``kept`` says nothing about them and the predicates are
    applied to the slots directly.
    """
    elements = set(kept)
    chosen = spec.selected if spec.selected is not None else frozenset()
    surviving: dict[str, Node] = {}
    for fqn, node in graph.nodes.items():
        if node.is_element:
            continue
        if reachable is not None and fqn not in reachable:
            continue
        pinned = fqn == seed or fqn in chosen
        if node.subnet is not None:
            narrowed = node.subnet.restricted_to(elements)
            if not narrowed.members and not pinned:
                continue
            surviving[fqn] = replace(node, subnet=narrowed, vlans=narrowed.vlans)
        elif node.tunnel is not None:
            restricted = node.tunnel.restricted_to(elements)
            if not restricted.ends and not pinned:
                continue
            surviving[fqn] = replace(node, tunnel=restricted)
        elif node.rack is not None:
            if spec.selected is not None and fqn not in chosen:
                # A query is answered over the *nodes* of the graph, and at this
                # layer the cabinet is the node while its contents are slots. So
                # a query selects cabinets, and the flags — which reach the
                # slots through _slot_selected — still select what is in them.
                continue
            elevation = replace(
                node.rack,
                slots=tuple(slot for slot in node.rack.slots if _slot_selected(slot, spec)),
            )
            if not elevation.slots and not pinned:
                continue
            surviving[fqn] = replace(node, rack=elevation)
        elif node.netns is not None:
            # A namespace has exactly one member: the machine that runs it. At
            # the netns layer that machine is a node of this graph (its initial
            # namespace) and at layer 3 it need not be — a bridging host with no
            # address of its own is not at layer 3 while its containers are — so
            # the predicates are applied to the machine directly, exactly as a
            # zone applies them to its device. ``--name srv-01`` therefore
            # selects the host *and everything inside it*, which is what
            # somebody who asked for a container host by name meant on the
            # layers that draw its containers.
            if not _stack_selected(node, spec) and not pinned:
                continue
            surviving[fqn] = node
        elif node.security is not None:
            # A zone's one member is its device, and unlike a namespace's the
            # device is *not* a node of this graph — nothing at
            # :attr:`Layer.SECURITY` stands for the box. So the predicates are
            # applied to it directly, exactly as a rack applies them to its
            # slots, and ``--name fw-edge`` selects that firewall's zones.
            if not _zone_selected(node, spec) and not pinned:
                continue
            surviving[fqn] = node
    return surviving


def _owner_selected(spec: FilterSpec, fqn: str, owner: str) -> bool:
    """Did a ``--select`` query keep this derived node, or the element behind it?

    Either answer is a yes, and both are needed. A query evaluates over the
    nodes of the graph, so at :attr:`Layer.NETNS` it can match a *container* by
    name and only that container should survive; but it can equally match the
    machine — ``kind = server`` never matches a stack node, whose kind is not a
    server's — and selecting a machine has to select the stacks inside it, for
    the same reason ``--name srv-01`` does.

    True when no query was given, so every other caller is unaffected.
    """
    return spec.selected is None or fqn in spec.selected or owner in spec.selected


def _zone_selected(node: Node, spec: FilterSpec) -> bool:
    """Does ``spec`` select the device whose zone this node is?

    Namespace, kind and name are answered from the device the zone belongs to,
    since selecting "the firewall" has to select its zones. A VLAN filter is the
    one that is answered from the node: a zone carries the ports in it, so the
    VLANs it is in are a fact about the zone rather than about the box.
    """
    view = node.security
    if view is None:  # pragma: no cover - the caller checked
        return False
    if not _owner_selected(spec, node.fqn, view.element):
        return False
    if spec.namespaces and not _in_namespaces(namespace_of(view.element), spec.namespaces):
        return False
    if spec.kinds and view.owner_kind not in spec.kinds:
        return False
    if spec.vlans and not (node.vlans & spec.vlans):
        return False
    return not spec.names or any(
        fnmatchcase(view.element, pattern) or fnmatchcase(short_name(view.element), pattern)
        for pattern in spec.names
    )


def _stack_selected(node: Node, spec: FilterSpec) -> bool:
    """Does ``spec`` select the machine whose network stack this node is (§23.1)?

    Three decisions, each of which had to be made once the graph could hold a
    stack node:

    * **Namespace and kind come from the machine.** A network namespace is not
      an inventory namespace — one is a routing table inside a host, the other
      is the directory the host's document lives in — so ``--namespace`` cannot
      name a stack and does not try to. Selecting a directory selects the
      machines in it and therefore every stack of them.
    * **A name may select one stack.** ``--name srv-01`` selects the machine, so
      it selects all of its stacks; ``--name 'srv-01/blue'`` selects that one
      container, because the stack's qualified label is how the diagram spells
      it and a filter should be able to name what the reader can see.
    * **VLANs come from the node.** A stack carries exactly the ports that are
      in it, so which broadcast domains it is in is a fact about the stack and
      not about the machine — the same split :func:`_zone_selected` makes.
    """
    view = node.netns
    if view is None:  # pragma: no cover - the caller checked
        return False
    if not _owner_selected(spec, node.fqn, view.element):
        return False
    if spec.namespaces and not _in_namespaces(namespace_of(view.element), spec.namespaces):
        return False
    if spec.kinds and view.owner_kind not in spec.kinds:
        return False
    if spec.vlans and not (node.vlans & spec.vlans):
        return False
    return not spec.names or any(
        fnmatchcase(view.element, pattern)
        or fnmatchcase(short_name(view.element), pattern)
        or fnmatchcase(view.qualified_label, pattern)
        for pattern in spec.names
    )


def _slot_selected(slot: RackSlot, spec: FilterSpec) -> bool:
    """Does ``spec`` select the element mounted in this slot?

    Namespace, kind and name are answerable from the slot itself. A VLAN filter
    is not — a slot records where a thing is bolted, not what it carries — and
    is therefore ignored here rather than silently emptying every cabinet. Nor
    is a ``--select`` query: it is answered over the nodes of the graph, and at
    this layer the node is the cabinet (see :func:`_kept_derived`).
    """
    if spec.namespaces and not _in_namespaces(namespace_of(slot.element), spec.namespaces):
        return False
    if spec.kinds and slot.kind not in spec.kinds:
        return False
    return not spec.names or any(
        fnmatchcase(slot.element, pattern) or fnmatchcase(slot.name, pattern)
        for pattern in spec.names
    )


def _neighbourhood(graph: Graph, seed: str, depth: int) -> set[str]:
    """Every node at most ``depth`` hops from ``seed``."""
    if depth <= 0:
        return {seed}

    adjacency = graph.adjacency()
    seen = {seed}
    frontier = deque([(seed, 0)])
    while frontier:
        current, distance = frontier.popleft()
        if distance >= depth:
            continue
        for neighbour in sorted(adjacency.get(current, frozenset())):
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append((neighbour, distance + 1))
    return seen


def _resolve_node(graph: Graph, name: str) -> str:
    """Resolve a node by fully-qualified name, then by unique short name."""
    if name in graph.nodes:
        return name
    matches = [fqn for fqn, node in graph.nodes.items() if node.name == name]
    if len(matches) == 1:
        return matches[0]
    raise UnknownElementError(name, matches)


def _in_namespaces(namespace: str, selected: Iterable[str]) -> bool:
    for candidate in selected:
        prefix = candidate.strip("/")
        if not prefix or namespace == prefix or namespace.startswith(f"{prefix}/"):
            return True
    return False


def _matches_name(node: Node, patterns: Iterable[str]) -> bool:
    return any(
        fnmatchcase(node.fqn, pattern) or fnmatchcase(node.name, pattern) for pattern in patterns
    )
