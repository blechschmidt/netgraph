"""The search: how A reaches B, and what the traffic crosses on the way.

Two searches live here, over the two graphs
:func:`~netgraph.render.graph.build_graph` already builds. Neither re-reads the
inventory and neither re-resolves a reference — a traced path and a rendered
diagram are two views of one resolution pass, which is what makes
``--highlight`` honest.

Layer 2 — the physical walk
---------------------------

Over the layer-1 graph: cables, adapter attachments and layer-2 tunnels. What
makes it a *layer-2* walk rather than a connectivity walk is that an element
only relays a frame when its kind says it does, and only within a VLAN:

* a **hub** is a repeater (§6.5) and relays everything;
* an **adapter** is transparent — §8.2 requires that collapsing it into its
  host must not change connectivity, so it must not change reachability either;
* a **switch** relays between two ports, subject to VLAN membership;
* a **router**, **computer** or **server** is where a frame stops. Traffic
  arriving at one of them has arrived; it does not pass through. Getting past a
  router is what layer 3 is for.

VLAN membership is carried as a *feasible set* rather than a single id, narrowed
at every port that declares one. An untagged host port declares nothing and
narrows nothing, which is what keeps a workstation inside the access VLAN its
switch put it in; an access port in VLAN 20 on a route already committed to
VLAN 10 empties the set, and that branch is not a path. The set that survives to
the end is what the trace *assumed*, and is reported as such — usually one
VLAN, and legitimately several when the whole route is trunked.

Layer 3 — the routed walk
-------------------------

Over the layer-3 graph, where an edge means "this element has an address in
this prefix". Two elements are one hop apart when they share a prefix, and an
element in the middle of a route only relays when ``spec.forwarding`` says it
does — which is true of a router by default (§6.1.1) and of a layer-3 switch
that declares it, and false of a workstation with two NICs. The whole route
stays in one address family: a packet does not change family at a hop.

An overlay needs no special case at either layer. A VXLAN carries its VLANs, so
the layer-2 walk crosses it exactly as it crosses a trunk; a WireGuard tunnel's
two ends are addressed in one prefix, so the layer-3 walk crosses it exactly as
it crosses a link — and :func:`_tunnel_index` then labels that hop with the
tunnel document behind it, which is how ``vxlan over ipsec`` reaches the report.

Enumeration
-----------

Both searches enumerate *simple* paths — no element twice — depth-first in graph
order, so a loop terminates and the results are the same on every run. Parallel
links are kept apart by edge identity, so the two cables of a LAG are two paths
rather than one: a redundant pair is the case a reader is most often asking
about, and collapsing it would hide the answer.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from netgraph.loader.inventory import Inventory
from netgraph.models import Device
from netgraph.render.graph import (
    TUNNEL_ID_PREFIX,
    Edge,
    EdgeKind,
    Graph,
    Layer,
    Node,
    TunnelView,
    build_graph,
    netns_node_id,
    resolve_tunnels,
)
from netgraph.trace.endpoints import resolve_endpoint
from netgraph.trace.model import (
    DEFAULT_MAX_HOPS,
    MAX_PATHS,
    Endpoint,
    Frontier,
    Link,
    TracedPath,
    TraceError,
    TraceResult,
    Waypoint,
)

__all__ = ["trace"]

#: Element kinds that relay a frame between two of their own ports. A router,
#: a computer and a server are absent on purpose: at layer 2 they are where
#: traffic stops.
_L2_RELAYS: Final[frozenset[str]] = frozenset({"switch", "hub", "adapter"})

#: Kinds that relay without looking at VLAN tags at all. A hub is a layer-1
#: repeater (§6.5) and an adapter is a bus that §8.2 requires be transparent, so
#: neither may prune a VLAN the way a switch port does.
_VLAN_BLIND_RELAYS: Final[frozenset[str]] = frozenset({"hub", "adapter"})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def trace(
    inventory: Inventory,
    source: str,
    destination: str,
    *,
    vlan: int | None = None,
    max_hops: int = DEFAULT_MAX_HOPS,
    limit: int = MAX_PATHS,
) -> TraceResult:
    """Find every distinct route from ``source`` to ``destination``.

    Args:
        inventory: A tree loaded by :func:`~netgraph.loader.load_tree`.
        source: ``SRC`` as typed — an element, ``element:interface`` or an IP
            address (:func:`~netgraph.trace.endpoints.resolve_endpoint`).
        destination: ``DST``, in the same three spellings.
        vlan: Force the layer-2 walk into this VLAN, and skip layer 3 entirely.
            A VLAN is a layer-2 fact, so asking for one is asking a layer-2
            question; answering it with a routed path would answer a different
            one.
        max_hops: How many links a route may cross before it is abandoned.
        limit: How many routes to collect before giving up on finding more.

    Returns:
        A :class:`~netgraph.trace.model.TraceResult`. Finding **no** path is a
        result, not an error: it carries the layers that were searched and how
        far each got, so the break can be located.

    Raises:
        TraceError: An endpoint names nothing, names more than one thing, or
            the two endpoints name addresses in different families.
    """
    src = resolve_endpoint(inventory, source, role="source")
    dst = resolve_endpoint(inventory, destination, role="destination")

    physical = build_graph(inventory, layer=Layer.L1)
    notes: list[str] = []

    if src.element == dst.element and src.netns == dst.netns:
        return _same_element(physical, src, dst, vlan=vlan, max_hops=max_hops)

    if src.element == dst.element:
        # One machine, two of its network stacks (§23.1). Not "the traffic never
        # leaves it": it leaves the namespace, and whether it comes back into
        # the other one is a routing question — which is why the layer-2 walk is
        # skipped rather than asked. That walk is over cables, and the veth pair
        # joining two stacks of one machine is inside the box every cable ends
        # on, so it could only ever answer "no".
        notes.append(
            f"both ends are on {src.element} but in different network namespaces "
            f"({_netns_text(src)} and {_netns_text(dst)}), so the trace looked for a routed "
            f"path between the two stacks"
        )
        return _routed_only(inventory, src, dst, max_hops=max_hops, limit=limit, notes=notes)

    l2 = _trace_l2(physical, src, dst, vlan=vlan, max_hops=max_hops, limit=limit)
    if l2.paths or vlan is not None:
        if vlan is not None and not l2.paths:
            notes.append(
                f"the search was restricted to VLAN {vlan}, so no routed path was looked for; "
                f"drop '--vlan' to let the trace cross a router"
            )
        return _result(src, dst, l2, vlan=vlan, max_hops=max_hops, notes=notes)

    notes.append(
        "no layer-2 path: the two elements are in no common broadcast domain, so the trace "
        "looked for a routed one"
    )
    return _routed_only(
        inventory, src, dst, max_hops=max_hops, limit=limit, notes=notes, before=(l2,)
    )


def _netns_text(endpoint: Endpoint) -> str:
    """How a namespace is named in a note: its name, or "the initial namespace"."""
    return f"'{endpoint.netns}'" if endpoint.netns else "the initial namespace"


def _routed_only(
    inventory: Inventory,
    source: Endpoint,
    destination: Endpoint,
    *,
    max_hops: int,
    limit: int,
    notes: list[str],
    before: Sequence[_Search] = (),
) -> TraceResult:
    """Build the layer-3 graph, search it, and fold the answer in with ``before``.

    Reached from two places — after a layer-2 walk found nothing, and directly
    when both ends are stacks of one machine — so that "which family, and why
    not" is decided once and worded once.
    """
    routed = build_graph(inventory, layer=Layer.L3)
    family, reason = _common_family(routed, source, destination)
    if family is None:
        # Every ``None`` carries its reason: "there is no routed path" is only
        # useful next to why there cannot be one.
        notes.append(reason)
        return _result(source, destination, *before, vlan=None, max_hops=max_hops, notes=notes)

    tunnels = _tunnel_index(resolve_tunnels(inventory)[0])
    l3 = _trace_l3(
        routed, source, destination, family=family, tunnels=tunnels, max_hops=max_hops, limit=limit
    )
    return _result(source, destination, *before, l3, vlan=None, max_hops=max_hops, notes=notes)


@dataclass(frozen=True, slots=True)
class _Search:
    """What one layer's search found, path or no path."""

    layer: Layer
    paths: tuple[TracedPath, ...] = ()
    truncated: bool = False
    frontier: Frontier | None = None


def _result(
    source: Endpoint,
    destination: Endpoint,
    *searches: _Search,
    vlan: int | None,
    max_hops: int,
    notes: Sequence[str] = (),
) -> TraceResult:
    """Fold the searches that ran into one answer, the first hit winning."""
    winner = next((search for search in searches if search.paths), None)
    return TraceResult(
        source=source,
        destination=destination,
        paths=winner.paths if winner is not None else (),
        layer=winner.layer if winner is not None else None,
        forced_vlan=vlan,
        max_hops=max_hops,
        truncated=winner.truncated if winner is not None else any(s.truncated for s in searches),
        frontiers=tuple(search.frontier for search in searches if search.frontier is not None),
        notes=tuple(notes),
    )


def _same_element(
    graph: Graph, source: Endpoint, destination: Endpoint, *, vlan: int | None, max_hops: int
) -> TraceResult:
    """SRC and DST are one element — a question about its own switching.

    ``netgraph path sw1:port1 sw1:port3`` is a real question with a real answer:
    a switch relays between those ports only if they share a VLAN. The route is
    zero hops long, because no link is crossed.
    """
    node = graph.nodes.get(source.element)
    egress, ingress = source.interface, destination.interface
    start = frozenset({vlan}) if vlan is not None else frozenset()
    feasible: frozenset[int] | None = start
    notes = [f"{source.element} is both ends of this trace: the traffic never leaves it"]

    if node is not None and egress and ingress and egress != ingress:
        narrowed = _narrow(start, _port_vlans(node, egress))
        feasible = _narrow(narrowed, _port_vlans(node, ingress)) if narrowed is not None else None
        if feasible is None:
            notes.append(
                f"ports {egress} and {ingress} share no VLAN, so {source.element} does not "
                f"relay between them"
            )
        elif node.kind not in _L2_RELAYS:
            notes.append(
                f"a {node.kind} does not relay between two of its own ports, so nothing "
                f"crosses from {egress} to {ingress} inside it"
            )
            feasible = None

    vlans = feasible if feasible is not None else frozenset()
    waypoint = Waypoint(
        element=source.element,
        kind=source.kind,
        ingress=ingress,
        egress=egress,
        ingress_addresses=_port_addresses(node, ingress),
        egress_addresses=_port_addresses(node, egress),
        vlans=vlans,
    )
    paths = (
        (TracedPath(waypoints=(waypoint,), layer=Layer.L2, vlans=vlans),)
        if feasible is not None
        else ()
    )
    return TraceResult(
        source=source,
        destination=destination,
        paths=paths,
        layer=Layer.L2 if paths else None,
        forced_vlan=vlan,
        max_hops=max_hops,
        frontiers=(Frontier(layer=Layer.L2, reached=1, furthest=source.element, depth=0),),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------- #
# Layer 2
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Hop:
    """One link a layer-2 walk can cross, seen from one of its ends."""

    #: Identity of the link, for the report and for de-duplication.
    id: str
    kind: str
    #: The element this hop is seen from, and the one at the far end. Both are
    #: kept so a reported link runs in the direction the traffic does; a cable
    #: stores its ends in the order §7.1 canonicalised them, which need not be
    #: the order they are crossed in.
    near: str
    other: str
    own_port: str
    other_port: str
    edge: Edge
    graph_edges: tuple[str, ...]
    graph_nodes: tuple[str, ...] = ()


def _l2_adjacency(graph: Graph) -> Mapping[str, tuple[_Hop, ...]]:
    """Every link each element can relay onto, in graph order.

    A point-to-point link becomes one hop per end. A multipoint tunnel is drawn
    as a *node* with one leg per endpoint (see
    :func:`~netgraph.render.graph._tunnel_topology`), which is right for a
    picture and wrong for a walk — a frame does not stop at the tunnel — so its
    legs are recombined here into one hop per pair of endpoints, carrying both
    legs so ``--highlight`` still emphasises the whole thing.
    """
    hops: dict[str, list[_Hop]] = {fqn: [] for fqn in graph.nodes}
    legs: dict[str, list[Edge]] = {}

    for edge in graph.edges:
        if edge.is_self_link:
            # A cable with both ends on one device changes nothing about who can
            # reach whom, and a simple path could not use it twice anyway.
            continue
        if edge.kind is EdgeKind.TUNNEL:
            if edge.target.startswith(TUNNEL_ID_PREFIX):
                legs.setdefault(edge.target, []).append(edge)
                continue
            if edge.tunnel is None or edge.tunnel.layer != 2:
                # A layer-3 tunnel carries packets, not frames: it extends no
                # broadcast domain, so a layer-2 walk cannot cross it.
                continue
        elif (  # pragma: no cover - layer 1 holds no subnet or encapsulation edge
            edge.kind is not EdgeKind.CABLE and edge.kind is not EdgeKind.ATTACHMENT
        ):
            continue
        _add_pair(hops, edge)

    for node_id, members in legs.items():
        view = graph.nodes[node_id].tunnel
        if view is None or view.layer != 2:  # pragma: no cover - a node always carries its view
            continue
        for index, first in enumerate(members):
            for second in members[index + 1 :]:
                if first.source == second.source:
                    continue
                _add_pair(
                    hops,
                    first,
                    other=second.source,
                    other_port=second.source_port,
                    graph_edges=(first.id, second.id),
                    graph_nodes=(node_id,),
                    identity=view.fqn,
                )
    return {fqn: tuple(entries) for fqn, entries in hops.items()}


def _add_pair(
    hops: dict[str, list[_Hop]],
    edge: Edge,
    *,
    other: str | None = None,
    other_port: str | None = None,
    graph_edges: tuple[str, ...] = (),
    graph_nodes: tuple[str, ...] = (),
    identity: str | None = None,
) -> None:
    """Record one link as a hop from each of its two ends."""
    left, left_port = edge.source, edge.source_port
    right = other if other is not None else edge.target
    right_port = other_port if other_port is not None else edge.target_port
    edges = graph_edges or (edge.id,)
    name = identity if identity is not None else edge.id
    for near, near_port, far, far_port in (
        (left, left_port, right, right_port),
        (right, right_port, left, left_port),
    ):
        if near not in hops:  # pragma: no cover - every edge end is a node
            continue
        hops[near].append(
            _Hop(
                id=name,
                kind=str(edge.kind),
                near=near,
                other=far,
                own_port=near_port,
                other_port=far_port,
                edge=edge,
                graph_edges=edges,
                graph_nodes=graph_nodes,
            )
        )


def _trace_l2(
    graph: Graph,
    source: Endpoint,
    destination: Endpoint,
    *,
    vlan: int | None,
    max_hops: int,
    limit: int,
) -> _Search:
    """Walk the physical topology from ``source``, honouring VLAN membership."""
    if (  # pragma: no cover - every device and adapter becomes a layer-1 node
        source.element not in graph.nodes or destination.element not in graph.nodes
    ):
        # Defensive: the endpoints were resolved against the inventory, and the
        # layer-1 graph holds a node for every element that owns interfaces. A
        # future layer that drops one must not make this a crash.
        return _Search(layer=Layer.L2)

    adjacency = _l2_adjacency(graph)
    found: list[TracedPath] = []
    depths: dict[str, int] = {source.element: 0}
    truncated = False

    def walk(
        current: str,
        ingress: str,
        feasible: frozenset[int],
        route: list[tuple[str, str, str]],
        links: list[Link],
        visited: set[str],
    ) -> None:
        nonlocal truncated
        if current == destination.element:
            if destination.interface is None or destination.interface == ingress:
                found.append(_build_path(graph, [*route, (current, ingress, "")], links, feasible))
            return
        if len(links) >= max_hops or len(found) >= limit:
            truncated = truncated or len(found) >= limit
            return

        node = graph.nodes[current]
        origin = not links
        for hop in adjacency[current]:
            if hop.other in visited:
                continue
            if origin:
                if source.interface is not None and hop.own_port != source.interface:
                    continue
            elif not _relays(node, ingress, hop.own_port):
                continue

            narrowed = _narrow(feasible, _port_vlans(node, hop.own_port))
            if narrowed is None:
                continue
            narrowed = _narrow(narrowed, _port_vlans(graph.nodes.get(hop.other), hop.other_port))
            if narrowed is None:
                continue

            depth = len(links) + 1
            if hop.other not in depths or depth < depths[hop.other]:
                depths[hop.other] = depth
            route.append((current, ingress, hop.own_port))
            links.append(_l2_link(hop, narrowed))
            visited.add(hop.other)
            walk(hop.other, hop.other_port, narrowed, route, links, visited)
            visited.discard(hop.other)
            links.pop()
            route.pop()

    start = frozenset({vlan}) if vlan is not None else frozenset()
    walk(source.element, "", start, [], [], {source.element})
    return _Search(
        layer=Layer.L2,
        paths=_ranked(found),
        truncated=truncated,
        frontier=_frontier(Layer.L2, depths),
    )


def _relays(node: Node, ingress: str, egress: str) -> bool:
    """Does ``node`` carry a frame from ``ingress`` out of ``egress``?

    VLAN membership is applied separately, by narrowing the feasible set; this
    is the kind-level question alone. Ports must differ: a switch does not send
    a frame back out of the port it arrived on.
    """
    if node.kind not in _L2_RELAYS:
        return False
    return ingress != egress or not ingress


def _narrow(feasible: frozenset[int], declared: frozenset[int]) -> frozenset[int] | None:
    """Intersect the feasible VLANs with what a port declares.

    An **empty** ``feasible`` means "nothing has constrained this route yet",
    not "no VLAN works": an untagged host port declares no membership and must
    not be read as excluding every VLAN. ``None`` is the answer that *does* mean
    the route is dead — the port is in VLANs the route cannot be in.
    """
    if not declared:
        return feasible
    if not feasible:
        return declared
    return (feasible & declared) or None


def _port_vlans(node: Node | None, name: str) -> frozenset[int]:
    """VLAN membership of one port, or nothing when it declares none."""
    if node is None or not name:
        return frozenset()
    if node.kind in _VLAN_BLIND_RELAYS:
        # §6.5 and §8.2: neither a repeater nor a bus prunes a VLAN, so reading
        # membership off one of their ports would invent a constraint.
        return frozenset()
    port = node.port(name)
    return port.vlans if port is not None else frozenset()


def _port_addresses(node: Node | None, name: str | None) -> tuple[str, ...]:
    """The routable addresses on one port, for the report."""
    if node is None or not name:
        return ()
    port = node.port(name)
    return port.routable_addresses if port is not None else ()


def _l2_link(hop: _Hop, vlans: frozenset[int]) -> Link:
    """One crossed link, in the direction the traffic crosses it."""
    edge = hop.edge
    return Link(
        id=hop.id,
        kind=hop.kind,
        source=hop.near,
        target=hop.other,
        source_port=hop.own_port,
        target_port=hop.other_port,
        medium=edge.medium,
        speed=edge.speed,
        label=edge.label,
        length_m=edge.length_m,
        vlans=vlans,
        tunnel=edge.tunnel,
        patch=edge.patch,
        graph_edges=hop.graph_edges,
        graph_nodes=hop.graph_nodes,
    )


def _build_path(
    graph: Graph,
    route: Sequence[tuple[str, str, str]],
    links: Sequence[Link],
    feasible: frozenset[int],
) -> TracedPath:
    """Turn a walked route into a :class:`TracedPath`."""
    waypoints = tuple(
        Waypoint(
            element=element,
            kind=graph.nodes[element].kind,
            ingress=ingress or None,
            egress=egress or None,
            ingress_addresses=_port_addresses(graph.nodes.get(element), ingress),
            egress_addresses=_port_addresses(graph.nodes.get(element), egress),
            vlans=feasible,
        )
        for element, ingress, egress in route
    )
    return TracedPath(waypoints=waypoints, links=tuple(links), layer=Layer.L2, vlans=feasible)


# --------------------------------------------------------------------------- #
# Layer 3
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _End:
    """One network stack's presence in a prefix.

    ``node`` is what the search walks — a stack of a machine (§23.1) — and
    ``element`` is the machine it is inside. The two differ exactly on a
    container host, and keeping both is what lets a hop be *found* between two
    stacks and *reported* as the machine plus the namespace.
    """

    node: str
    element: str
    netns: str
    interface: str
    addresses: tuple[str, ...]
    edge_id: str


@dataclass(frozen=True, slots=True)
class _Prefix:
    """One IP prefix and everything addressed in it."""

    prefix: str
    family: str
    node_id: str
    ends: tuple[_End, ...]


def _prefixes(graph: Graph) -> tuple[_Prefix, ...]:
    """The prefixes of a layer-3 graph, with their members, in graph order."""
    members: dict[str, list[_End]] = {}
    for edge in graph.edges:
        if edge.kind is not EdgeKind.SUBNET:  # pragma: no cover - l3 holds only these
            continue
        node = graph.nodes.get(edge.source)
        view = node.netns if node is not None else None
        members.setdefault(edge.target, []).append(
            _End(
                node=edge.source,
                element=view.element if view is not None else edge.source,
                netns=view.name if view is not None else "",
                interface=edge.source_port,
                addresses=edge.addresses,
                edge_id=edge.id,
            )
        )
    result: list[_Prefix] = []
    for node_id, ends in members.items():
        subnet = graph.nodes[node_id].subnet
        if subnet is None:  # pragma: no cover - a subnet node always carries one
            continue
        result.append(
            _Prefix(prefix=subnet.prefix, family=subnet.family, node_id=node_id, ends=tuple(ends))
        )
    return tuple(result)


def _stacks_of(graph: Graph, endpoint: Endpoint) -> tuple[str, ...]:
    """The layer-3 nodes an endpoint may start from or arrive at (§23.1).

    An element is one node at layer 3 until it runs containers, at which point
    it is several and the argument has to say — or be taken to mean any of them:

    * ``10.30.0.11`` and ``srv-01:veth-blue`` pin the interface, and an
      interface is in exactly one stack, so exactly one node answers;
    * ``srv-01`` pins nothing below the machine, so every stack of it does. That
      is the reading that keeps ``netgraph path srv-01 sw-core`` working on a
      host whose only addresses are inside its containers, and it is the same
      reading ``sw1`` already had at layer 2 ("by any of its ports").

    Ordered as the graph is, so the initial namespace is tried first and a
    shortest path from the machine is found before one from a container.
    """
    if endpoint.netns is not None:
        pinned = netns_node_id(endpoint.element, endpoint.netns)
        return (pinned,) if pinned in graph.nodes else ()
    return tuple(
        fqn
        for fqn, node in graph.nodes.items()
        if fqn == endpoint.element
        or (node.netns is not None and node.netns.element == endpoint.element)
    )


def _tunnel_index(views: Iterable[TunnelView]) -> Mapping[tuple[str, str], tuple[TunnelView, ...]]:
    """Which tunnels terminate on each ``(element, interface)``.

    This is what lets a routed hop say ``wireguard`` instead of only naming the
    prefix: a tunnel's two ends are addressed in one subnet, so the layer-3
    walk crosses it without knowing it is one.
    """
    index: dict[tuple[str, str], list[TunnelView]] = {}
    for view in views:
        for end in view.ends:
            index.setdefault((end.element, end.interface), []).append(view)
    return {key: tuple(entries) for key, entries in index.items()}


def _common_family(graph: Graph, source: Endpoint, destination: Endpoint) -> tuple[str | None, str]:
    """The address family to route in, and — when there is none — why not.

    The second element of the pair is empty exactly when the first is not.

    An address given as an argument decides it outright — that is the question
    the operator asked. Otherwise IPv4 wins where both ends have one, because
    that is what an unqualified "can A reach B?" still means on most networks,
    and the choice is reported either way.

    Raises:
        TraceError: The two arguments named addresses in different families.
    """
    wanted = {
        role: _family_of(endpoint.address)
        for role, endpoint in (("source", source), ("destination", destination))
        if endpoint.address is not None
    }
    families = set(wanted.values())
    if len(families) > 1:
        raise TraceError(
            f"{source.spec!r} is {wanted['source']} and {destination.spec!r} is "
            f"{wanted['destination']}; a packet does not change address family at a hop, so "
            f"there is no path to trace between them"
        )

    available = {
        role: _families_of(graph, _stacks_of(graph, endpoint))
        for role, endpoint in (("source", source), ("destination", destination))
    }
    for role, endpoint in (("source", source), ("destination", destination)):
        if not available[role]:
            return None, (
                f"{endpoint.element} carries no routable address, so it does not appear at "
                f"layer 3 and no routed path to it exists"
            )

    if families:
        family = families.pop()
        for role, endpoint in (("source", source), ("destination", destination)):
            if family not in available[role]:
                return None, (
                    f"{endpoint.element} has no {family} address, so no {family} path reaches it"
                )
        return family, ""

    shared = [
        family
        for family in ("ipv4", "ipv6")
        if family in available["source"] & available["destination"]
    ]
    if not shared:
        return None, (
            f"{source.element} and {destination.element} share no address family — one is "
            f"{'/'.join(sorted(available['source']))} only and the other "
            f"{'/'.join(sorted(available['destination']))} only"
        )
    return shared[0], ""


def _family_of(address: str) -> str:
    return f"ipv{ipaddress.ip_interface(address).version}"


def _families_of(graph: Graph, stacks: Sequence[str]) -> frozenset[str]:
    """The address families these stacks of one element hold an address in.

    The union over the stacks, because the question this answers is whether a
    routed path to the *argument* can exist, and an argument that named the
    machine may be satisfied by any stack in it.
    """
    return frozenset(
        _family_of(address)
        for fqn in stacks
        if (node := graph.nodes.get(fqn)) is not None
        for address in node.routable_addresses
    )


def _trace_l3(
    graph: Graph,
    source: Endpoint,
    destination: Endpoint,
    *,
    family: str,
    tunnels: Mapping[tuple[str, str], tuple[TunnelView, ...]],
    max_hops: int,
    limit: int,
) -> _Search:
    """Walk prefix by prefix, through the network stacks that forward.

    The unit of the walk is a **stack**, not an element (§23.1). A container and
    the host that runs it are two nodes, two routing tables and two hops apart,
    so a packet that leaves a container by its host's bridge crosses two
    prefixes and is a two-hop path — where drawing one node per machine made it
    a path of length zero, which the search then rejected (follow-up 23). On an
    inventory that declares no ``spec.netns`` every machine is exactly one stack
    and this is the walk it has always been.

    ``visited`` therefore holds stacks: passing *through* a machine by way of a
    second stack of it is legitimate and common, and forbidding it — which is
    what an element-keyed set would do — is precisely the bug.
    """
    starts = _stacks_of(graph, source)
    finishes = frozenset(_stacks_of(graph, destination))
    if not starts or not finishes:  # pragma: no cover - _common_family rejects these first
        # An element with no routable address is absent from the layer-3 graph,
        # and ``_common_family`` has already turned that into a note. Defensive.
        return _Search(layer=Layer.L3)

    adjacency: dict[str, list[tuple[_Prefix, _End, _End]]] = {fqn: [] for fqn in graph.nodes}
    for prefix in _prefixes(graph):
        if prefix.family != family:
            continue
        for near in prefix.ends:
            for far in prefix.ends:
                if near.node == far.node:
                    continue
                adjacency.setdefault(near.node, []).append((prefix, near, far))

    found: list[TracedPath] = []
    depths: dict[str, int] = dict.fromkeys(starts, 0)
    truncated = False

    def walk(
        current: str,
        ingress: _End | None,
        route: list[tuple[str, _End | None, _End | None]],
        links: list[Link],
        visited: set[str],
        crossed: set[str],
    ) -> None:
        nonlocal truncated
        if current in finishes and ingress is not None:
            # ``ingress`` is set from the second waypoint on, and the first is
            # the source -- which is a different stack, because one stack at both
            # ends is answered by ``_same_element``.
            if _matches(destination, ingress):
                found.append(
                    _build_l3_path(graph, [*route, (current, ingress, None)], links, family)
                )
            return
        if len(links) >= max_hops or len(found) >= limit:
            truncated = truncated or len(found) >= limit
            return
        if links and not _forwards(graph.nodes[current], family):
            # A stack that does not forward is a destination, not a via.
            return

        origin = not links
        for prefix, near, far in adjacency[current]:
            if far.node in visited or prefix.prefix in crossed:
                continue
            if origin and not _matches(source, near):
                continue

            depth = len(links) + 1
            if far.node not in depths or depth < depths[far.node]:
                depths[far.node] = depth
            route.append((current, ingress, near))
            links.append(_l3_link(prefix, near, far, tunnels))
            visited.add(far.node)
            crossed.add(prefix.prefix)
            walk(far.node, far, route, links, visited, crossed)
            crossed.discard(prefix.prefix)
            visited.discard(far.node)
            links.pop()
            route.pop()

    for start in starts:
        # Every stack the source argument may have meant. A path found from the
        # machine's own namespace and one found from a container inside it are
        # different routes, and ``_ranked`` puts the shorter first.
        walk(start, None, [], [], {start}, set())
    return _Search(
        layer=Layer.L3,
        paths=_ranked(found),
        truncated=truncated,
        frontier=_frontier(Layer.L3, depths),
    )


def _matches(endpoint: Endpoint, end: _End) -> bool:
    """Does the prefix membership ``end`` satisfy what the argument pinned down?"""
    if endpoint.interface is not None and endpoint.interface != end.interface:
        return False
    return endpoint.address is None or endpoint.address in end.addresses


def _forwards(node: Node, family: str) -> bool:
    """Does ``node`` route packets of ``family`` between its interfaces?

    ``spec.forwarding`` is the inventory's own answer (§6.1.1): a router says
    yes by default, a layer-3 switch says so explicitly, and a workstation with
    two NICs says no — which is exactly right, because a host does not route
    between them unless someone configured it to.

    For a **stack** node the answer is the machine's, because that is the only
    answer the inventory has: ``spec.forwarding`` is one statement per document
    and §23 gives no way to say "and not in the ``blue`` namespace". So a
    container of a forwarding host forwards, which is what makes a route out of
    a nested namespace traceable, and none of the stacks of a host that does not
    forward do. A per-namespace override, if it is ever wanted, is a change to
    the model that this reads without further work.
    """
    element = node.element
    if element is None and node.netns is not None:
        element = node.netns.device
    if not isinstance(element, Device) or element.spec.forwarding is None:
        return False
    forwarding = element.spec.forwarding
    return bool(forwarding.ipv4 if family == "ipv4" else forwarding.ipv6)


def _l3_link(
    prefix: _Prefix,
    near: _End,
    far: _End,
    tunnels: Mapping[tuple[str, str], tuple[TunnelView, ...]],
) -> Link:
    """One prefix crossing, with the tunnel behind it when there is one.

    ``source`` and ``target`` are the *nodes* the hop runs between, which at
    layer 3 are stacks and everywhere else are elements — the JSON report has
    always called them ``node`` — so a hop from a container to its host names
    both ends distinguishably instead of naming one machine twice.
    """
    return Link(
        id=prefix.prefix,
        kind=str(EdgeKind.SUBNET),
        source=near.node,
        target=far.node,
        source_port=near.interface,
        target_port=far.interface,
        subnet=prefix.prefix,
        addresses=(*near.addresses[:1], *far.addresses[:1]),
        tunnel=_shared_tunnel(tunnels, near, far),
        graph_edges=(near.edge_id, far.edge_id),
        graph_nodes=(prefix.node_id,),
    )


def _shared_tunnel(
    tunnels: Mapping[tuple[str, str], tuple[TunnelView, ...]], near: _End, far: _End
) -> TunnelView | None:
    """The tunnel both ends of a routed hop terminate on, if there is one."""
    here = tunnels.get((near.element, near.interface), ())
    if not here:
        return None
    there = {view.fqn for view in tunnels.get((far.element, far.interface), ())}
    return next((view for view in here if view.fqn in there), None)


def _l3_waypoint(node: Node, ingress: _End | None, egress: _End | None) -> Waypoint:
    """One stop on a routed path, named as the machine plus its namespace.

    A waypoint reports the **element**, not the node id, even where the node is
    a stack: ``hosts/srv-01`` is the thing a reader can open, rename and point
    at, and ``netns:hosts/srv-01:blue`` is an identity the graph mints. Which
    stack of it the traffic was in is a fact of its own and is carried as one
    (:attr:`~netgraph.trace.model.Waypoint.netns`); ``kind`` likewise stays the
    machine's, so a routed report reads ``server`` and not ``netns``.
    """
    view = node.netns
    return Waypoint(
        element=view.element if view is not None else node.fqn,
        kind=view.owner_kind or node.kind if view is not None else node.kind,
        netns=view.name if view is not None else "",
        ingress=ingress.interface if ingress is not None else None,
        egress=egress.interface if egress is not None else None,
        ingress_addresses=ingress.addresses if ingress is not None else (),
        egress_addresses=egress.addresses if egress is not None else (),
    )


def _build_l3_path(
    graph: Graph,
    route: Sequence[tuple[str, _End | None, _End | None]],
    links: Sequence[Link],
    family: str,
) -> TracedPath:
    waypoints = tuple(
        _l3_waypoint(graph.nodes[node], ingress, egress) for node, ingress, egress in route
    )
    return TracedPath(waypoints=waypoints, links=tuple(links), layer=Layer.L3, family=family)


# --------------------------------------------------------------------------- #
# Shared
# --------------------------------------------------------------------------- #


def _ranked(paths: Iterable[TracedPath]) -> tuple[TracedPath, ...]:
    """Shortest first, then by the names on the route; duplicates removed.

    The tie-break is the route itself rather than discovery order, so ``--all``
    lists a redundant pair the same way on every run and in a readable order —
    which is what makes a golden file of a trace worth committing.
    """
    unique: dict[tuple[str, ...], TracedPath] = {}
    for path in paths:
        unique.setdefault(path.key, path)
    return tuple(sorted(unique.values(), key=lambda path: (path.hops, path.key)))


def _frontier(layer: Layer, depths: Mapping[str, int]) -> Frontier:
    """How far the search got, so a failure can name where the break is.

    The furthest element is the one at the greatest depth — the last place the
    traffic could still have reached. Ties go to whichever was reached first,
    which follows graph order and is therefore stable.
    """
    furthest, depth = None, -1
    for element, reached_at in depths.items():
        if reached_at > depth:
            furthest, depth = element, reached_at
    return Frontier(layer=layer, reached=len(depths), furthest=furthest, depth=max(depth, 0))
