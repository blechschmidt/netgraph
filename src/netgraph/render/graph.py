"""The renderer-facing view of an inventory: nodes, edges and VLAN membership.

Every renderer consumes a :class:`Graph` rather than an
:class:`~netgraph.loader.inventory.Inventory`. The indirection buys three
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
database and the links it terminates. ``netgraph render --vlan 10`` keeps the
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
    *plus one node per IP prefix* (:mod:`netgraph.subnets`); edges join an
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
:attr:`Edge.patch`, which is how ``netgraph path`` can name the panel as a
pass-through without the panel being a hop.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Container, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Final

from netgraph.loader.inventory import Inventory, SourceLocation, namespace_of, short_name
from netgraph.models import (
    PATCHPANEL_KIND,
    Adapter,
    Cable,
    Device,
    Interface,
    PatchPanel,
    Tunnel,
    TunnelType,
    format_bitrate,
)
from netgraph.models.metadata import Location
from netgraph.subnets import AddressPlacement, Subnet, is_routable_address, subnets_of

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    # :mod:`netgraph.render.aggregate` consumes this module, so the dependency
    # can only run one way at import time. The two annotations below are the
    # whole of what points back, and ``from __future__ import annotations``
    # keeps them strings.
    from netgraph.render.aggregate import AggregateView, BundleView

__all__ = [
    "NODE_KINDS",
    "PATCHPANEL_KIND",
    "RACK_ID_PREFIX",
    "RACK_KIND",
    "SUBNET_ID_PREFIX",
    "SUBNET_KIND",
    "TUNNEL_ID_PREFIX",
    "TUNNEL_KIND",
    "Edge",
    "EdgeKind",
    "FilterSpec",
    "Graph",
    "Layer",
    "Node",
    "NodeType",
    "PatchHop",
    "PatchView",
    "PortView",
    "RackSlot",
    "RackView",
    "Subnet",
    "TunnelEnd",
    "TunnelView",
    "build_graph",
    "filter_graph",
    "is_routable_address",
    "rack_elevations",
    "resolve_tunnels",
    "splice_patch_panels",
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
    #: Placement: one node per rack, holding its front elevation (§3.2).
    RACK = "rack"

    def __str__(self) -> str:
        return self.value

    @property
    def shows_panels(self) -> bool:
        """Does this layer draw a passive cross-connect as a node of its own?"""
        return self is Layer.PHYSICAL


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
    #: :func:`~netgraph.render.aggregate.collapse_namespaces`. It stands for
    #: elements the diagram no longer draws, so a consumer must be able to tell
    #: it from the single device it otherwise looks like.
    AGGREGATE = "aggregate"

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

#: Prefix of a subnet node's identity, e.g. ``subnet:10.0.0.0/24``. A colon
#: cannot occur in an element's fully-qualified name (§2 name grammar), so a
#: subnet node can never shadow a device.
SUBNET_ID_PREFIX: Final = "subnet:"

#: ``kind`` reported for a tunnel node. It is also the declared ``kind`` of the
#: document it stands for, unlike :data:`SUBNET_KIND`: a tunnel node *is* the
#: element, drawn as a node instead of as an edge.
TUNNEL_KIND: Final = "tunnel"

#: Prefix of a tunnel node's identity, e.g. ``tunnel:sites/hq/vx-100``. The same
#: reasoning as :data:`SUBNET_ID_PREFIX`: a colon keeps it out of the namespace
#: of declared names, so the node and the element it stands for stay distinct
#: even in a graph that holds both.
TUNNEL_ID_PREFIX: Final = "tunnel:"


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

    def __str__(self) -> str:
        return self.value


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
        )


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
class RackSlot:
    """One element mounted in a rack, and the units it occupies."""

    element: str
    name: str
    kind: str
    #: Lowest unit occupied, counted from 1 at the bottom.
    position: int
    height: int = 1

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
    element: Device | Adapter | PatchPanel | None = None
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
    #: aggregate node (:mod:`netgraph.render.aggregate`).
    aggregate: AggregateView | None = None
    #: The rack this node stands for; set exactly for a rack node.
    rack: RackView | None = None

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
            fqn=f"{SUBNET_ID_PREFIX}{subnet.prefix}",
            name=subnet.prefix,
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
    def is_element(self) -> bool:
        """Does this node stand for a device or an adapter the reader can point at?"""
        return self.type is NodeType.ELEMENT

    @property
    def _document(self) -> Device | Adapter | PatchPanel | Tunnel | None:
        """The declared document behind the node, tunnels included."""
        if self.element is not None:
            return self.element
        return self.tunnel.tunnel if self.tunnel is not None else None

    @property
    def labels(self) -> Mapping[str, str]:
        document = self._document
        return document.metadata.labels if document is not None else {}

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
    #: into it (:mod:`netgraph.render.aggregate`). ``None`` on a link that is
    #: exactly itself.
    bundle: BundleView | None = None
    #: The patch panels this edge was spliced through, when it stands for a run
    #: of two or more cable segments (§15.2). ``None`` on a direct cable, which
    #: is what makes the two tellable apart in an export.
    patch: PatchView | None = None

    @property
    def name(self) -> str:
        """The short name of the cable, the adapter or the tunnel."""
        if self.tunnel is not None:
            return self.tunnel.name
        return short_name(self.id.partition("#")[0])

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
    #: ``--link-template`` expands (:mod:`netgraph.render.links`); it is
    #: deliberately not part of what a renderer *draws*.
    sources: Mapping[str, SourceLocation] = field(default_factory=dict)

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


def build_graph(inventory: Inventory, *, layer: Layer = Layer.L1) -> Graph:
    """Resolve an inventory into a renderable graph.

    Args:
        inventory: A tree loaded by :func:`~netgraph.loader.load_tree`.
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
    if panels and not layer.shows_panels:
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
    if not layer.shows_panels and layer is not Layer.RACK:
        # A panel that no cable reaches has no segments to splice, so removing
        # the nodes is a separate step from removing the edges.
        nodes = {fqn: node for fqn, node in nodes.items() if node.kind != PATCHPANEL_KIND}

    if layer is Layer.L3:
        nodes, edges = _routed_view(nodes, subnets_of(inventory))
    elif layer is Layer.OVERLAY:
        nodes, edges = _overlay_view(nodes, tunnels)
    elif layer is Layer.RACK:
        nodes, edges = _rack_view(inventory)
    return Graph(
        root=inventory.root,
        nodes=nodes,
        edges=edges,
        layer=layer,
        dangling=dangling,
        sources=dict(inventory.sources),
    )


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
        reports each of those as well (``NG-P001``…``NG-P005``); the graph layer
        drops the run because ``--force`` must still produce a picture.
    """
    touching = {edge.id for edge in edges if _panel_ends(edge, panels)}
    if not touching:
        return tuple(edges), ()

    dangling: list[str] = []
    # ``(panel, port)`` -> the one segment landing on it. A second cable on the
    # same port is ``NG-P003``; the first one declared wins so the walk stays
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
    reason — which is the same fact ``NG-P002`` or ``NG-P005`` reports about the
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


def rack_elevations(inventory: Inventory) -> tuple[RackView, ...]:
    """One :class:`RackView` per rack the inventory places anything in (§3.2).

    Racks come out in first-seen order and their slots bottom-up, so an
    elevation reads the way the inventory does and the way the cabinet does.
    An element whose ``location`` names a rack but no ``position`` is left out:
    it is in the room, and an elevation cannot say where.
    """
    placed: dict[tuple[str, str, str], list[RackSlot]] = {}
    heights: dict[tuple[str, str, str], int] = {}
    declared: dict[tuple[str, str, str], bool] = {}
    labels: dict[tuple[str, str, str], str] = {}

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
            # ``NG-U003`` refuses two elements that disagree; the tallest wins
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


def _routed_view(
    nodes: Mapping[str, Node], subnets: Sequence[Subnet]
) -> tuple[dict[str, Node], tuple[Edge, ...]]:
    """Turn the physical graph into the routed one.

    The physical edges are discarded: at layer 3 two elements are adjacent
    because they share a prefix, and a cable between them is neither necessary
    (a route may cross three switches) nor sufficient (a trunk carries VLANs the
    two ends do not both route). Elements keep the VLAN membership derived from
    the physical graph, so ``--vlan 10`` still selects the broadcast domain
    rather than only the ports that spell the VLAN out.

    Nodes come out as the addressed elements in inventory order, followed by the
    subnets in prefix order; edges follow the elements, then the interface order
    within each one, so the output reads the way the inventory does.
    """
    addressed = {element for subnet in subnets for element in subnet.elements}
    kept = {fqn: node for fqn, node in nodes.items() if fqn in addressed}

    memberships: dict[str, list[tuple[Subnet, AddressPlacement]]] = {}
    for subnet in subnets:
        for member in subnet.members:
            memberships.setdefault(member.element, []).append((subnet, member))

    edges: list[Edge] = []
    for fqn in kept:
        # One edge per (interface, prefix): a second address on the same
        # interface in the same prefix is another label, not another adjacency.
        grouped: dict[tuple[str, str], list[AddressPlacement]] = {}
        for subnet, member in sorted(
            memberships[fqn], key=lambda entry: (entry[1].index, entry[0].sort_key)
        ):
            grouped.setdefault((member.interface, subnet.prefix), []).append(member)
        for (interface, prefix), placements in grouped.items():
            edges.append(
                Edge(
                    id=f"{fqn}:{interface}#{prefix}",
                    kind=EdgeKind.SUBNET,
                    source=fqn,
                    target=f"{SUBNET_ID_PREFIX}{prefix}",
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
    panel is not one (``NG-P004``).
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
    "hub",
    "computer",
    "server",
    "adapter",
    "patchpanel",
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

    @property
    def is_empty(self) -> bool:
        """Would this filter keep the graph unchanged?"""
        return not (self.namespaces or self.vlans or self.kinds or self.names or self.neighbors_of)

    def describe(self) -> str:
        """A one-line summary for diagnostics, e.g. ``kind=switch, vlan=10``."""
        parts: list[str] = []
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
    the diagram no longer shows anything in. The one exception is a node named
    directly by ``--neighbors-of``, which is the thing the reader asked about.

    A rack is the one derived node whose members are *not* nodes of the graph:
    at :attr:`Layer.RACK` the elements are slots inside the cabinet rather than
    boxes beside it, so ``kept`` says nothing about them and the predicates are
    applied to the slots directly.
    """
    elements = set(kept)
    surviving: dict[str, Node] = {}
    for fqn, node in graph.nodes.items():
        if node.is_element:
            continue
        if reachable is not None and fqn not in reachable:
            continue
        if node.subnet is not None:
            narrowed = node.subnet.restricted_to(elements)
            if not narrowed.members and fqn != seed:
                continue
            surviving[fqn] = replace(node, subnet=narrowed, vlans=narrowed.vlans)
        elif node.tunnel is not None:
            restricted = node.tunnel.restricted_to(elements)
            if not restricted.ends and fqn != seed:
                continue
            surviving[fqn] = replace(node, tunnel=restricted)
        elif node.rack is not None:
            elevation = replace(
                node.rack,
                slots=tuple(slot for slot in node.rack.slots if _slot_selected(slot, spec)),
            )
            if not elevation.slots and fqn != seed:
                continue
            surviving[fqn] = replace(node, rack=elevation)
    return surviving


def _slot_selected(slot: RackSlot, spec: FilterSpec) -> bool:
    """Does ``spec`` select the element mounted in this slot?

    Namespace, kind and name are answerable from the slot itself. A VLAN filter
    is not — a slot records where a thing is bolted, not what it carries — and
    is therefore ignored here rather than silently emptying every cabinet.
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
