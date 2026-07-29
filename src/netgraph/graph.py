"""The inventory as a :class:`networkx.MultiGraph`, for analysis rather than drawing.

:mod:`netgraph.render.graph` already resolves an inventory into a frozen
:class:`~netgraph.render.graph.Graph` — the thing the renderers consume. That
representation is deliberately immutable and renderer-shaped: ordered nodes,
ordered edges, no algorithms. This module is the other half: the same resolved
topology handed to :mod:`networkx`, so that connectivity questions ("what is
reachable from this switch in two hops?", "which broadcast domains exist?") are
answered by graph algorithms instead of by hand.

The two are not alternatives, they are one pipeline::

    resolved = build_graph(inventory)        # netgraph.render.graph
    g        = to_networkx(resolved)         # here
    g        = filter_graph(g, vlans=[10])
    print(stats(g))

Resolution happens exactly once, in :func:`~netgraph.render.graph.build_graph`,
so a networkx view and a rendered diagram can never disagree about what is
connected to what. Everything below consumes the output of that pass; nothing
here re-reads the YAML.

Why a multigraph
----------------

Two switches joined by two cables in a LAG are *two* edges, not one. A simple
graph would silently collapse them, losing a link the operator cares about most
— the redundant one. Every edge is therefore keyed by its stable identity
(:attr:`~netgraph.render.graph.Edge.id`: the cable's fully-qualified name, or
``<adapter>#upstream``), which also makes ``g[u][v]`` addressable by cable name.
Self-links (a cable with both ends on one device, §7.2) survive for the same
reason.

Node and edge attributes
------------------------

Nodes carry ``kind``, ``namespace``, ``name``, ``interfaces`` (the interface
*names*, in declaration order), ``ports`` (the full
:class:`~netgraph.render.graph.PortView` records), ``vlans`` and ``node_type``.
Edges carry ``source``/``target`` with their ``source_port``/``target_port``,
plus ``medium``, ``speed``, ``label``, ``length_m``, ``vlans`` and — on a
tunnel or an encapsulation edge — the resolved
:class:`~netgraph.render.graph.TunnelView`. ``source``
and ``target`` are repeated in the edge attributes on purpose: networkx yields
``(u, v)`` in adjacency order, which is not the order the cable declared its
ends in, so ``data["source_port"]`` would otherwise be ambiguous — see
:func:`ports_of`.

``node_type`` distinguishes a declared element from a node this layer derives:
an IP prefix at layer 3 (:data:`SUBNET_TYPE`), a tunnel drawn as a node
(:data:`TUNNEL_TYPE`) or a VLAN broadcast domain (:data:`DOMAIN_TYPE`, produced
by :func:`layers`). Filtering predicates apply to elements; derived nodes are
kept exactly as long as one selected element still belongs to them.
"""

from __future__ import annotations

import ipaddress
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import networkx as nx

from netgraph.loader.inventory import Inventory
from netgraph.render.graph import (
    SUBNET_ID_PREFIX,
    SUBNET_KIND,
    TUNNEL_ID_PREFIX,
    TUNNEL_KIND,
    Edge,
    EdgeKind,
    Layer,
    Node,
    NodeType,
    PortView,
    UnknownElementError,
    build_graph,
)
from netgraph.render.graph import (
    Graph as ResolvedGraph,
)

__all__ = [
    "DOMAIN_TYPE",
    "ELEMENT_TYPE",
    "LINK_EDGE_KINDS",
    "PHYSICAL_EDGE_KINDS",
    # Re-exported from the resolution layer: both belong to the vocabulary of
    # node ids here, so a caller that only imports this module still has them.
    "SUBNET_ID_PREFIX",
    "SUBNET_KIND",
    "SUBNET_TYPE",
    "TUNNEL_ID_PREFIX",
    "TUNNEL_KIND",
    "TUNNEL_TYPE",
    "VLAN_KIND",
    "VLAN_NODE_PREFIX",
    "BroadcastDomain",
    "GraphStats",
    "Layer",
    "Layers",
    "UnknownElementError",
    "broadcast_domains",
    "filter_graph",
    "layers",
    "ports_of",
    "resolve_node",
    "stats",
    "to_networkx",
]

#: ``node_type`` of a declared device or adapter.
ELEMENT_TYPE: Final = str(NodeType.ELEMENT)
#: ``node_type`` of an IP prefix node (layer 3).
SUBNET_TYPE: Final = str(NodeType.SUBNET)
#: ``node_type`` of a tunnel node (§14).
TUNNEL_TYPE: Final = str(NodeType.TUNNEL)
#: ``node_type`` of a VLAN broadcast-domain node (:func:`layers`).
DOMAIN_TYPE: Final = "domain"

#: ``kind`` reported for a broadcast-domain node. The eight element kinds of §3
#: and :data:`~netgraph.render.graph.SUBNET_KIND` are all taken, so this cannot
#: collide with a declared kind.
VLAN_KIND: Final = "vlan"

#: Prefix of a broadcast-domain node's identity, e.g. ``vlan:10``. A colon
#: cannot occur in an element's fully-qualified name (§2 name grammar), so a
#: domain node can never shadow a device.
VLAN_NODE_PREFIX: Final = "vlan:"

#: Edge kinds that stand for something a technician could unplug. A layer-3
#: subnet-membership edge is not one of them, and neither is the VLAN
#: membership edge :func:`layers` derives.
PHYSICAL_EDGE_KINDS: Final[frozenset[str]] = frozenset(
    {str(EdgeKind.CABLE), str(EdgeKind.ATTACHMENT)}
)

#: Edge kinds that carry a frame from one element to another. A tunnel is not
#: something a technician can unplug, so it is not in
#: :data:`PHYSICAL_EDGE_KINDS` — but a layer-2 tunnel does extend a broadcast
#: domain across the underlay, which is the whole reason VXLAN exists, so
#: :func:`broadcast_domains` has to walk it.
LINK_EDGE_KINDS: Final[frozenset[str]] = PHYSICAL_EDGE_KINDS | {str(EdgeKind.TUNNEL)}

#: Link kinds that carry only the VLANs their two ends agree on. An adapter
#: attachment is not one — §8.2 requires that collapsing an adapter into its
#: host must not change connectivity, and a USB dongle does not prune VLANs.
_VLAN_PRUNING_KINDS: Final[frozenset[str]] = frozenset({str(EdgeKind.CABLE), str(EdgeKind.TUNNEL)})


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def to_networkx(source: Inventory | ResolvedGraph, *, layer: Layer | None = None) -> nx.MultiGraph:
    """Convert an inventory — or an already-resolved graph — into a multigraph.

    Args:
        source: A tree loaded by :func:`~netgraph.loader.load_tree`, or the
            :class:`~netgraph.render.graph.Graph` that
            :func:`~netgraph.render.graph.build_graph` produced from one.
            Passing the resolved graph avoids a second resolution pass when the
            caller already built one for a renderer.
        layer: Which view to build. Defaults to :attr:`Layer.L1`. Must not
            contradict ``source.layer`` when a resolved graph is passed.

    Returns:
        A :class:`networkx.MultiGraph` whose nodes follow inventory load order.
        Edges come back in networkx's node-major adjacency order rather than in
        cable load order — but that order is a function of the node order, so it
        too is the same on every run over the same tree, which is what a golden
        file or a ``git diff`` on generated output needs. ``g.graph`` carries
        ``root``, ``layer`` and ``dangling`` (the cables dropped for an
        unresolvable endpoint; see
        :attr:`~netgraph.render.graph.Graph.dangling`).

    Raises:
        ValueError: ``layer`` was given and disagrees with the resolved graph.
    """
    if isinstance(source, ResolvedGraph):
        if layer is not None and layer is not source.layer:
            raise ValueError(
                f"layer={layer} contradicts the resolved graph, which was built "
                f"for layer {source.layer}; rebuild it or drop the argument"
            )
        resolved = source
    else:
        resolved = build_graph(source, layer=Layer.L1 if layer is None else layer)

    graph = nx.MultiGraph()
    graph.graph.update(
        root=resolved.root,
        layer=str(resolved.layer),
        dangling=resolved.dangling,
    )
    for node in resolved.nodes.values():
        graph.add_node(node.fqn, **_node_attrs(node))
    for edge in resolved.edges:
        # ``key`` is the edge's own identity, so two cables between the same
        # pair of devices stay distinguishable and re-adding one is idempotent.
        graph.add_edge(edge.source, edge.target, key=edge.id, **_edge_attrs(edge))
    return graph


def _node_attrs(node: Node) -> dict[str, Any]:
    return {
        "name": node.name,
        "kind": node.kind,
        "namespace": node.namespace,
        "node_type": str(node.type),
        "interfaces": tuple(port.name for port in node.ports),
        "ports": node.ports,
        "vlans": node.vlans,
        "addresses": node.routable_addresses,
        "members": _members_of(node),
        "element": node.element,
        "subnet": node.subnet,
        "tunnel": node.tunnel,
        "node": node,
    }


def _members_of(node: Node) -> tuple[str, ...]:
    """The elements a derived node stands for; empty for a declared element."""
    if node.subnet is not None:
        return node.subnet.elements
    if node.tunnel is not None:
        return node.tunnel.elements
    return ()


def _edge_attrs(edge: Edge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "kind": str(edge.kind),
        "source": edge.source,
        "target": edge.target,
        "source_port": edge.source_port,
        "target_port": edge.target_port,
        "medium": edge.medium,
        "speed": edge.speed,
        "label": edge.label,
        "length_m": edge.length_m,
        "vlans": edge.vlans,
        "addresses": edge.addresses,
        "cable": edge.cable,
        "adapter": edge.adapter,
        "tunnel": edge.tunnel,
        "edge": edge,
    }


def ports_of(data: Mapping[str, Any], node: str) -> tuple[str, ...]:
    """The interface names ``node`` terminates this edge on.

    networkx yields an edge as ``(u, v, data)`` in adjacency order, which need
    not match the order the cable declared its endpoints in, so
    ``data["source_port"]`` cannot be assumed to belong to ``u``. This resolves
    it by name. A self-link returns both of its ends, the host end of an
    adapter attachment none (§8.1: it names no interface).
    """
    names = []
    if data.get("source") == node:
        names.append(str(data.get("source_port", "")))
    if data.get("target") == node:
        names.append(str(data.get("target_port", "")))
    return tuple(name for name in names if name)


def resolve_node(graph: nx.MultiGraph, name: str) -> str:
    """Resolve ``name`` to a node id, by fully-qualified name then by short name.

    Raises:
        UnknownElementError: no node matches, or the short name is ambiguous.
            :attr:`UnknownElementError.candidates` then holds the fully-qualified
            names that collided, so a caller can print them.
    """
    if graph.has_node(name):
        return str(name)
    matches = [fqn for fqn, data in graph.nodes(data=True) if data.get("name") == name]
    if len(matches) == 1:
        return str(matches[0])
    raise UnknownElementError(name, matches)


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


def filter_graph(
    graph: nx.MultiGraph,
    *,
    namespaces: Iterable[str] | None = None,
    vlans: Iterable[int] | None = None,
    kinds: Iterable[str] | None = None,
    name_regex: str | re.Pattern[str] | None = None,
    neighbors_of: str | None = None,
    depth: int = 1,
) -> nx.MultiGraph:
    """Narrow ``graph`` to the nodes the given predicates select.

    Values *within* one argument are alternatives (``kinds=["switch", "router"]``
    keeps both); different arguments are combined with AND, so
    ``namespaces=["sites/north"], kinds=["switch"]`` keeps the switches of that
    site only. An argument left at ``None`` selects everything.

    ``neighbors_of`` is applied **first** and traverses the *whole* graph, so a
    switch two hops away is still reachable through nodes the other predicates
    would have removed. The remaining predicates then apply to that
    neighbourhood.

    Args:
        graph: Any graph produced by this module.
        namespaces: Namespace prefixes; a node matches its own namespace and
            every descendant of it. ``""`` matches everything.
        vlans: Keep the elements that participate in one of these VLANs. A link
            carrying none of them is dropped even when both its ends survive: it
            is not part of the broadcast domain being drawn.
        kinds: Element kinds, e.g. ``switch``, ``router``, ``adapter``.
        name_regex: Unanchored regular expression, searched against the
            fully-qualified *and* the short name. A :class:`str` is compiled
            with :func:`re.compile`; an invalid pattern raises
            :exc:`re.error` unchanged.
        neighbors_of: Keep only the neighbourhood of this element. Accepts a
            fully-qualified or an unambiguous short name.
        depth: How many hops that neighbourhood extends. ``0`` (or less) keeps
            the named element alone. At layer 3 the traversal runs over subnet
            nodes, so depth 1 from a device reaches the prefixes it is addressed
            in and depth 2 the other devices in them.

    Returns:
        The induced subgraph, as an independent :class:`networkx.MultiGraph`
        with ``graph.graph`` copied over — never a view, so mutating the result
        cannot corrupt the input. Node and edge order is inherited from
        ``graph``, not from the (unordered) set of survivors, which keeps the
        result reproducible across runs.

    Raises:
        UnknownElementError: ``neighbors_of`` names no node, or an ambiguous one.
    """
    selected_namespaces = tuple(namespaces) if namespaces is not None else ()
    selected_vlans = frozenset(vlans) if vlans is not None else frozenset()
    selected_kinds = frozenset(kinds) if kinds is not None else frozenset()
    pattern = re.compile(name_regex) if isinstance(name_regex, str) else name_regex

    seed: str | None = None
    reachable: set[str] | None = None
    if neighbors_of is not None:
        seed = resolve_node(graph, neighbors_of)
        reachable = _neighbourhood(graph, seed, depth)

    kept: set[str] = set()
    derived: list[str] = []
    for fqn, data in graph.nodes(data=True):
        if reachable is not None and fqn not in reachable:
            continue
        if _node_type(data) != ELEMENT_TYPE:
            # Derived nodes have no namespace, kind or name of their own; they
            # follow from whichever elements survive, so they are decided below.
            derived.append(fqn)
            continue
        if selected_namespaces and not _in_namespaces(data["namespace"], selected_namespaces):
            continue
        if selected_kinds and data["kind"] not in selected_kinds:
            continue
        if pattern is not None and not _matches_name(data, fqn, pattern):
            continue
        if selected_vlans and not (data["vlans"] & selected_vlans):
            continue
        kept.add(fqn)

    narrowed: dict[str, dict[str, Any]] = {}
    for fqn in derived:
        attrs = _narrow_derived(graph.nodes[fqn], kept)
        if attrs is None and fqn != seed:
            # A prefix or a broadcast domain nobody selected still belongs to
            # would be an empty box claiming a domain the diagram no longer
            # shows anything in. The one exception is a derived node the caller
            # named directly.
            continue
        narrowed[fqn] = attrs if attrs is not None else dict(graph.nodes[fqn])

    result = nx.MultiGraph()
    result.graph.update(graph.graph)
    for fqn, data in graph.nodes(data=True):
        if fqn in kept:
            result.add_node(fqn, **data)
        elif fqn in narrowed:
            result.add_node(fqn, **narrowed[fqn])
    for source, target, key, data in graph.edges(keys=True, data=True):
        if not (result.has_node(source) and result.has_node(target)):
            continue
        edge_vlans = data.get("vlans", frozenset())
        if selected_vlans and edge_vlans and not (edge_vlans & selected_vlans):
            continue
        result.add_edge(source, target, key=key, **data)
    return result


def _node_type(data: Mapping[str, Any]) -> str:
    return str(data.get("node_type", ELEMENT_TYPE))


def _in_namespaces(namespace: str, selected: Sequence[str]) -> bool:
    for candidate in selected:
        prefix = candidate.strip("/")
        if not prefix or namespace == prefix or namespace.startswith(f"{prefix}/"):
            return True
    return False


def _matches_name(data: Mapping[str, Any], fqn: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(fqn) or pattern.search(str(data.get("name", ""))))


def _narrow_derived(data: Mapping[str, Any], kept: set[str]) -> dict[str, Any] | None:
    """Re-state a derived node in terms of the elements that survived.

    A subnet that kept listing removed members would report members the reader
    cannot see. Returns ``None`` when nothing it stands for is left.
    """
    attrs = dict(data)
    tunnel = attrs.get("tunnel")
    if tunnel is not None and _node_type(data) == TUNNEL_TYPE:
        restricted = tunnel.restricted_to(kept)
        if not restricted.ends:
            return None
        attrs["tunnel"] = restricted
        attrs["members"] = restricted.elements
        return attrs

    subnet = attrs.get("subnet")
    if subnet is not None:
        restricted = subnet.restricted_to(kept)
        if not restricted.members:
            return None
        attrs["subnet"] = restricted
        attrs["members"] = restricted.elements
        attrs["vlans"] = restricted.vlans
        return attrs

    members = tuple(member for member in attrs.get("members", ()) if member in kept)
    if not members:
        return None
    attrs["members"] = members
    return attrs


def _neighbourhood(graph: nx.MultiGraph, seed: str, depth: int) -> set[str]:
    """Every node at most ``depth`` hops from ``seed``, self-links ignored."""
    if depth <= 0:
        return {seed}

    order = {node: index for index, node in enumerate(graph)}
    seen = {seed}
    frontier = deque([(seed, 0)])
    while frontier:
        current, distance = frontier.popleft()
        if distance >= depth:
            continue
        for neighbour in sorted(graph.adj[current], key=lambda node: order[node]):
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append((neighbour, distance + 1))
    return seen


# --------------------------------------------------------------------------- #
# Layers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BroadcastDomain:
    """One VLAN, and the elements that can reach each other inside it.

    A VLAN id is a *label on a link*, not a network: VLAN 10 configured on two
    switches that no VLAN-10-carrying path joins is two broadcast domains that
    happen to share a number. That is a topology bug worth seeing, so each
    connected component gets its own domain rather than being merged by id.
    """

    #: The VLAN id (§9).
    vlan: int
    #: 1-based ordinal among the domains carrying this VLAN, in member order.
    index: int
    #: Node identity in the layer-2 graph: ``vlan:10``, or ``vlan:10#2`` when
    #: the VLAN is partitioned.
    id: str
    #: Fully-qualified names of the members, in graph order.
    members: tuple[str, ...]
    #: Ids of the links inside the domain, in graph order; empty for a VLAN
    #: declared on one element and carried nowhere.
    links: tuple[str, ...]

    @property
    def name(self) -> str:
        """Short label, e.g. ``vlan10`` or ``vlan10#2``."""
        return self.id.replace(":", "", 1)

    @property
    def is_isolated(self) -> bool:
        """Is this a VLAN configured on a single element and carried nowhere?"""
        return len(self.members) == 1


@dataclass(frozen=True, slots=True)
class Layers:
    """The two views :func:`layers` splits a physical graph into."""

    #: Physical: elements, joined by every cable and adapter attachment.
    l1: nx.MultiGraph
    #: Logical: one node per broadcast domain, joined to each of its members.
    l2: nx.MultiGraph
    #: The domains behind the :attr:`l2` nodes, ordered by VLAN id.
    domains: tuple[BroadcastDomain, ...]


def layers(graph: nx.MultiGraph) -> Layers:
    """Split a graph into its layer-1 and layer-2 views.

    ``l1`` is the physical topology: the declared elements, joined by every
    cable and every adapter attachment. Any derived node (a layer-3 prefix) and
    any logical edge is dropped — nobody can unplug a subnet.

    ``l2`` is the broadcast-domain view. Nodes are the domains found by
    :func:`broadcast_domains` **plus** the elements that belong to at least one;
    edges join an element to each domain it is in, labelled with the interfaces
    through which it joins. A domain-only graph would have no edges at all, and
    a domain is only meaningful next to its members. Elements in no VLAN drop
    out rather than float, the same way an unaddressed element drops out of the
    layer-3 view.

    Passing a layer-3 graph is not an error but is rarely useful: it has no
    cables, so ``l1`` comes back with isolated nodes.
    """
    domains = broadcast_domains(graph)
    return Layers(l1=_physical_view(graph), l2=_domain_view(graph, domains), domains=domains)


def _physical_view(graph: nx.MultiGraph) -> nx.MultiGraph:
    result = nx.MultiGraph()
    result.graph.update(graph.graph)
    result.graph["layer"] = str(Layer.L1)
    for fqn, data in graph.nodes(data=True):
        if _node_type(data) == ELEMENT_TYPE:
            result.add_node(fqn, **data)
    for source, target, key, data in graph.edges(keys=True, data=True):
        if data.get("kind") not in PHYSICAL_EDGE_KINDS:
            continue
        if result.has_node(source) and result.has_node(target):
            result.add_edge(source, target, key=key, **data)
    return result


def _domain_view(graph: nx.MultiGraph, domains: Sequence[BroadcastDomain]) -> nx.MultiGraph:
    result = nx.MultiGraph()
    result.graph.update(graph.graph)
    result.graph["layer"] = str(Layer.L2)

    members = {member for domain in domains for member in domain.members}
    for fqn, data in graph.nodes(data=True):
        if fqn in members:
            result.add_node(fqn, **data)

    for domain in domains:
        result.add_node(
            domain.id,
            name=domain.name,
            kind=VLAN_KIND,
            # A VLAN spans whatever namespaces its members live in, so it
            # belongs to none of them and reports the root namespace.
            namespace="",
            node_type=DOMAIN_TYPE,
            interfaces=(),
            ports=(),
            vlans=frozenset({domain.vlan}),
            addresses=(),
            members=domain.members,
            vlan=domain.vlan,
            domain=domain,
            element=None,
            subnet=None,
            tunnel=None,
        )
        for member in domain.members:
            ports = tuple(
                port.name
                for port in graph.nodes[member].get("ports", ())
                if domain.vlan in port.vlans
            )
            result.add_edge(
                member,
                domain.id,
                key=f"{member}#{domain.id}",
                id=f"{member}#{domain.id}",
                kind=VLAN_KIND,
                source=member,
                target=domain.id,
                # An element can reach a VLAN through several ports; the first
                # names the edge, ``interfaces`` keeps all of them.
                source_port=ports[0] if ports else "",
                target_port="",
                interfaces=ports,
                medium="",
                speed=None,
                label=None,
                length_m=None,
                vlans=frozenset({domain.vlan}),
                addresses=(),
                cable=None,
                adapter=None,
                tunnel=None,
            )
    return result


def broadcast_domains(graph: nx.MultiGraph) -> tuple[BroadcastDomain, ...]:
    """Every VLAN broadcast domain in ``graph``, ordered by VLAN id.

    An element is a *candidate* member of VLAN 10 when its ``vlans`` says so —
    which covers a port that declares the VLAN, a device that only lists it in
    its VLAN database (§6.4), and a host attached to an access port in it (see
    the :mod:`netgraph.render.graph` module docstring). Candidates are then
    partitioned by walking the links that actually carry the VLAN:

    * a **cable** carries VLAN 10 when its ``vlans`` contains 10, i.e. when both
      ends agree on it (:func:`~netgraph.render.graph._link_vlans`);
    * a **layer-2 tunnel** carries it on the same terms: VXLAN, Geneve and L2TP
      extend a broadcast domain across the underlay, so two sites bridged by one
      are a single domain rather than two that share a number;
    * an **adapter attachment** always carries it, for candidates on both ends.
      §8.2 requires that collapsing an adapter into its host must not change
      connectivity, and a USB dongle does not prune VLANs.

    Each connected component of that walk is one domain. A VLAN whose members
    are not joined by any such link therefore yields several domains — see
    :class:`BroadcastDomain`.
    """
    order = {node: index for index, node in enumerate(graph)}
    membership = {
        fqn: frozenset(data.get("vlans", frozenset()))
        for fqn, data in graph.nodes(data=True)
        if _node_type(data) == ELEMENT_TYPE
    }
    every_vlan = sorted({vlan for vlans in membership.values() for vlan in vlans})

    domains: list[BroadcastDomain] = []
    for vlan in every_vlan:
        candidates = [fqn for fqn in graph if vlan in membership.get(fqn, frozenset())]
        adjacency, links = _vlan_links(graph, vlan, set(candidates))
        components = _components(candidates, adjacency, order)
        for index, component in enumerate(components, start=1):
            inside = set(component)
            identity = f"{VLAN_NODE_PREFIX}{vlan}"
            if len(components) > 1:
                identity = f"{identity}#{index}"
            domains.append(
                BroadcastDomain(
                    vlan=vlan,
                    index=index,
                    id=identity,
                    members=component,
                    links=tuple(
                        key
                        for source, target, key in links
                        if source in inside and target in inside
                    ),
                )
            )
    return tuple(domains)


def _vlan_links(
    graph: nx.MultiGraph, vlan: int, candidates: set[str]
) -> tuple[dict[str, set[str]], tuple[tuple[str, str, str], ...]]:
    """Adjacency and edge ids of the links carrying ``vlan`` between candidates.

    Layer-2 tunnels count: a VXLAN joining two sites in VLAN 10 makes them one
    broadcast domain, which is the whole reason the overlay was built. A
    layer-3 tunnel carries no VLAN at all (see
    :func:`~netgraph.render.graph._tunnel_vlans`), so it prunes itself out here.
    """
    adjacency: dict[str, set[str]] = {fqn: set() for fqn in candidates}
    links: list[tuple[str, str, str]] = []
    for source, target, key, data in graph.edges(keys=True, data=True):
        kind = data.get("kind")
        if kind not in LINK_EDGE_KINDS:
            continue
        if source not in candidates or target not in candidates:
            continue
        if kind in _VLAN_PRUNING_KINDS and vlan not in data.get("vlans", frozenset()):
            continue
        adjacency[source].add(target)
        adjacency[target].add(source)
        links.append((source, target, str(key)))
    return adjacency, tuple(links)


def _components(
    candidates: Sequence[str], adjacency: Mapping[str, set[str]], order: Mapping[str, int]
) -> tuple[tuple[str, ...], ...]:
    """Connected components of ``candidates``, in graph order inside and out."""
    seen: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in candidates:
        if start in seen:
            continue
        seen.add(start)
        component = [start]
        frontier = deque([start])
        while frontier:
            current = frontier.popleft()
            for neighbour in sorted(adjacency[current], key=lambda node: order[node]):
                if neighbour not in seen:
                    seen.add(neighbour)
                    component.append(neighbour)
                    frontier.append(neighbour)
        components.append(tuple(sorted(component, key=lambda node: order[node])))
    return tuple(components)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GraphStats:
    """How big a graph is, and how it decomposes."""

    #: Every node, derived ones included.
    nodes: int
    #: Every edge. Parallel cables count once each; a self-link counts once.
    edges: int
    #: Declared devices and adapters, i.e. nodes of type :data:`ELEMENT_TYPE`.
    elements: int
    #: Distinct VLAN ids anything in the graph participates in.
    vlans: int
    #: Tunnels the graph draws, as nodes or as edges (§14).
    tunnels: int
    #: Distinct IP prefixes the visible elements are addressed in.
    subnets: int
    #: Distinct namespaces holding at least one element; the root counts as one.
    namespaces: int
    #: Connected components, isolated nodes included.
    components: int
    #: Element count per kind, ordered by kind name.
    by_kind: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable form, for ``--format json`` and for tests."""
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "elements": self.elements,
            "vlans": self.vlans,
            "tunnels": self.tunnels,
            "subnets": self.subnets,
            "namespaces": self.namespaces,
            "components": self.components,
            "by_kind": dict(self.by_kind),
        }


def stats(graph: nx.MultiGraph) -> GraphStats:
    """Summarise ``graph``: node, edge, VLAN and subnet counts.

    Subnets are counted from the addresses still configured on the *visible*
    elements, not from the subnet nodes a layer-3 graph happens to hold, so the
    number means the same thing at every layer and after any filter. Loopback
    and link-local addresses are excluded, exactly as in
    :func:`~netgraph.subnets.subnets_of` — ``127.0.0.1`` is not a subnet of this
    network.
    """
    vlans: set[int] = set()
    prefixes: set[str] = set()
    namespaces: set[str] = set()
    tunnels: set[str] = set()
    by_kind: dict[str, int] = {}
    elements = 0

    for _, data in graph.nodes(data=True):
        vlans |= set(data.get("vlans", frozenset()))
        tunnel = data.get("tunnel")
        if tunnel is not None:
            tunnels.add(tunnel.fqn)
        if _node_type(data) != ELEMENT_TYPE:
            continue
        elements += 1
        namespaces.add(str(data.get("namespace", "")))
        kind = str(data.get("kind", ""))
        by_kind[kind] = by_kind.get(kind, 0) + 1
        prefixes |= _prefixes(data.get("ports", ()))

    for _, _, data in graph.edges(data=True):
        vlans |= set(data.get("vlans", frozenset()))
        tunnel = data.get("tunnel")
        if tunnel is not None:
            tunnels.add(tunnel.fqn)

    return GraphStats(
        nodes=graph.number_of_nodes(),
        edges=graph.number_of_edges(),
        elements=elements,
        vlans=len(vlans),
        tunnels=len(tunnels),
        subnets=len(prefixes),
        namespaces=len(namespaces),
        components=nx.number_connected_components(graph),
        by_kind=dict(sorted(by_kind.items())),
    )


def _prefixes(ports: Iterable[PortView]) -> set[str]:
    """The distinct prefixes the routable addresses of ``ports`` sit in."""
    return {
        str(ipaddress.ip_interface(address).network)
        for port in ports
        for address in port.routable_addresses
    }
