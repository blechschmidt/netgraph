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

The three layers
----------------

:class:`Layer` picks *which graph* is built, and only layer 3 changes what
exists:

``l1``
    The physical topology: one node per device and adapter, one edge per cable
    and per adapter attachment, annotated with medium, rate and cable labels.
``l2``
    The same nodes and edges, annotated with VLAN membership instead.
``l3``
    A different graph. Nodes are the elements that hold a routable address
    *plus one node per IP prefix* (:mod:`netgraph.subnets`); edges join an
    element to every prefix it has an address in. Cables do not appear — two
    devices are adjacent at layer 3 because they share a subnet, not because a
    cable happens to run between them. Elements with no routable address are
    left out rather than drawn floating, and loopback, link-local and
    unnumbered interfaces contribute nothing.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
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
    format_bitrate,
)
from netgraph.subnets import AddressPlacement, Subnet, is_routable_address, subnets_of

__all__ = [
    "SUBNET_ID_PREFIX",
    "SUBNET_KIND",
    "Edge",
    "EdgeKind",
    "FilterSpec",
    "Graph",
    "Layer",
    "Node",
    "NodeType",
    "PortView",
    "Subnet",
    "build_graph",
    "filter_graph",
    "is_routable_address",
]


class Layer(str, Enum):
    """Which view of the network a rendering shows."""

    #: Physical: what is plugged into what, with medium, speed and cable labels.
    L1 = "l1"
    #: Logical: the same topology annotated with VLAN membership and port mode.
    L2 = "l2"
    #: Routed: IP prefixes as nodes, joined to the elements addressed in them.
    L3 = "l3"

    def __str__(self) -> str:
        return self.value


class NodeType(str, Enum):
    """What a node stands for.

    Layer 1 and layer 2 hold nothing but elements; layer 3 mixes the two, so a
    consumer of a rendering needs to be able to tell them apart without
    pattern-matching on names.
    """

    #: A device or an adapter: hardware the inventory declares.
    ELEMENT = "element"
    #: An IP prefix, derived from the addresses of the elements (layer 3 only).
    SUBNET = "subnet"

    def __str__(self) -> str:
        return self.value


#: ``kind`` reported for a subnet node. The seven element kinds of §3 are all
#: taken, so this one cannot collide with a declared kind.
SUBNET_KIND: Final = "subnet"

#: Prefix of a subnet node's identity, e.g. ``subnet:10.0.0.0/24``. A colon
#: cannot occur in an element's fully-qualified name (§2 name grammar), so a
#: subnet node can never shadow a device.
SUBNET_ID_PREFIX: Final = "subnet:"


class EdgeKind(str, Enum):
    """Why two nodes are joined."""

    #: A ``cable`` document (§7).
    CABLE = "cable"
    #: An adapter's ``upstream.attached_to`` reference (§8.2).
    ATTACHMENT = "attachment"
    #: An element has an address in a subnet (layer 3 only).
    SUBNET = "subnet"

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
    def labels(self) -> Mapping[str, str]:
        return self.element.metadata.labels if self.element is not None else {}

    @property
    def description(self) -> str | None:
        return self.element.metadata.description if self.element is not None else None

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

    @property
    def name(self) -> str:
        """The short name of the cable, or of the adapter for an attachment."""
        return short_name(self.id.partition("#")[0])

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
        return tuple(node for node in self.nodes.values() if not node.is_subnet)

    @property
    def subnet_nodes(self) -> tuple[Node, ...]:
        """The subnet nodes, in graph order; empty below layer 3."""
        return tuple(node for node in self.nodes.values() if node.is_subnet)

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
            elements without a routable address drop out. See the module
            docstring.

    Returns:
        A graph whose every edge references two nodes that exist. Cables with an
        unresolvable endpoint are reported in :attr:`Graph.dangling` instead of
        raising, because ``--force`` must still produce a picture. The layer-3
        graph keeps that list too: an unresolved cable costs a host the VLAN
        membership it would have inherited from the link.
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
    if layer is Layer.L3:
        nodes, edges = _routed_view(nodes, subnets_of(inventory))
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

    # Every predicate is about a declared element; subnets follow from whichever
    # elements survive (see FilterSpec).
    kept = {fqn for fqn, node in graph.nodes.items() if not node.is_subnet}
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

    subnets = _kept_subnets(graph, kept, reachable=reachable, seed=seed)
    nodes = {
        fqn: subnets.get(fqn, node)
        for fqn, node in graph.nodes.items()
        if fqn in kept or fqn in subnets
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


def _kept_subnets(
    graph: Graph, kept: Iterable[str], *, reachable: set[str] | None, seed: str | None
) -> dict[str, Node]:
    """The surviving subnet nodes, narrowed to the members that survived too.

    A prefix nobody selected still has is dropped: an empty subnet box would
    claim a broadcast domain the diagram no longer shows anything in. The one
    exception is a subnet named directly by ``--neighbors-of``, which is the
    element the reader asked about.
    """
    elements = set(kept)
    surviving: dict[str, Node] = {}
    for fqn, node in graph.nodes.items():
        if not node.is_subnet or node.subnet is None:
            continue
        if reachable is not None and fqn not in reachable:
            continue
        narrowed = node.subnet.restricted_to(elements)
        if not narrowed.members and fqn != seed:
            continue
        surviving[fqn] = replace(node, subnet=narrowed, vlans=narrowed.vlans)
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
