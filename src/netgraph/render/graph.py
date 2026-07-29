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

The four layers
---------------

:class:`Layer` picks *which graph* is built, and only layers 3 and ``overlay``
change what exists:

``l1``
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
"""

from __future__ import annotations

from collections import deque
from collections.abc import Container, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

from netgraph.loader.inventory import Inventory, namespace_of, short_name
from netgraph.models import (
    Adapter,
    Cable,
    Device,
    Interface,
    Tunnel,
    TunnelType,
    format_bitrate,
)
from netgraph.subnets import AddressPlacement, Subnet, is_routable_address, subnets_of

__all__ = [
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
    "PortView",
    "Subnet",
    "TunnelEnd",
    "TunnelView",
    "build_graph",
    "filter_graph",
    "is_routable_address",
    "resolve_tunnels",
]


class Layer(str, Enum):
    """Which view of the network a rendering shows."""

    #: Physical: what is plugged into what, with medium, speed and cable labels.
    L1 = "l1"
    #: Logical: the same topology annotated with VLAN membership and port mode.
    L2 = "l2"
    #: Routed: IP prefixes as nodes, joined to the elements addressed in them.
    L3 = "l3"
    #: Encapsulation: tunnels as nodes, joined to their endpoints and to the
    #: tunnels they run inside (§14).
    OVERLAY = "overlay"

    def __str__(self) -> str:
        return self.value


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

    def __str__(self) -> str:
        return self.value


#: ``kind`` reported for a subnet node. The eight element kinds of §3 are all
#: taken, so this one cannot collide with a declared kind.
SUBNET_KIND: Final = "subnet"

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
    element: Device | Adapter | None = None
    ports: tuple[PortView, ...] = ()
    #: Every VLAN this element participates in, links included. See the module
    #: docstring. For a subnet: every VLAN an interface addressed in it is in.
    vlans: frozenset[int] = frozenset()
    type: NodeType = NodeType.ELEMENT
    #: The prefix this node stands for; set exactly for a subnet node.
    subnet: Subnet | None = None
    #: The tunnel this node stands for; set exactly for a tunnel node.
    tunnel: TunnelView | None = None

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
    def is_element(self) -> bool:
        """Does this node stand for a device or an adapter the reader can point at?"""
        return self.type is NodeType.ELEMENT

    @property
    def _document(self) -> Device | Adapter | Tunnel | None:
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
    def tunnels(self) -> tuple[TunnelView, ...]:
        """Every tunnel the graph draws, as a node or as an edge, in graph order."""
        seen: dict[str, TunnelView] = {}
        for node in self.nodes.values():
            if node.tunnel is not None:
                seen.setdefault(node.tunnel.fqn, node.tunnel)
        for edge in self.edges:
            if edge.tunnel is not None:
                seen.setdefault(edge.tunnel.fqn, edge.tunnel)
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
        layer: ``l1`` and ``l2`` build the same physical graph and differ only in
            the annotations a renderer draws from it (§8.2). ``l3`` builds the
            routed graph instead: subnets become nodes, cables disappear, and
            elements without a routable address drop out. ``overlay`` builds the
            encapsulation graph: tunnels become nodes, joined to their endpoints
            and to the tunnels they run inside. See the module docstring.

    Returns:
        A graph whose every edge references two nodes that exist. Cables and
        tunnels with an unresolvable endpoint are reported in
        :attr:`Graph.dangling` instead of raising, because ``--force`` must still
        produce a picture. The layer-3 graph keeps that list too: an unresolved
        cable costs a host the VLAN membership it would have inherited from the
        link.
    """
    # Iterate the full element map so nodes keep inventory load order rather
    # than the devices-then-adapters order of ``interface_owners``.
    owners: dict[str, Device | Adapter] = {
        fqn: element
        for fqn, element in inventory.elements.items()
        if isinstance(element, (Device, Adapter))
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

    if layer is Layer.L3:
        nodes, edges = _routed_view(nodes, subnets_of(inventory))
    elif layer is Layer.OVERLAY:
        nodes, edges = _overlay_view(nodes, tunnels)
    return Graph(root=inventory.root, nodes=nodes, edges=edges, layer=layer, dangling=dangling)


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
    """Cables first (load order), then adapter attachments (§8.2)."""
    edges: list[Edge] = []
    dangling: list[str] = []
    owners = inventory.interface_owners

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
        if host_fqn is None or host_fqn not in owners:
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
    left_owner: Device | Adapter,
    left_port: str,
    right_owner: Device | Adapter,
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


def _port_vlans(owner: Device | Adapter, port: str) -> frozenset[int]:
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

    derived = _kept_derived(graph, kept, reachable=reachable, seed=seed)
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
    )


def _kept_derived(
    graph: Graph, kept: Iterable[str], *, reachable: set[str] | None, seed: str | None
) -> dict[str, Node]:
    """The surviving subnet and tunnel nodes, narrowed to the members left.

    A prefix nobody selected still has is dropped, and so is a tunnel with no
    endpoint left: an empty box would claim a broadcast domain — or an overlay —
    the diagram no longer shows anything in. The one exception is a node named
    directly by ``--neighbors-of``, which is the thing the reader asked about.
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
    return surviving


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
