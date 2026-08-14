"""Which single failures split a graph, and how much each one cuts off.

The question ``netgraph impact`` exists to answer — *what breaks if this dies* —
is one question asked of many graphs: the physical topology, each broadcast
domain, the routed adjacency, the power plan. So the graph theory lives here,
once, over a representation deliberately smaller than
:class:`networkx.MultiGraph`: a tuple of node names, a tuple of edges, and a
frozen adjacency. Nothing in this module knows what a cable is.

What it computes
----------------

:func:`analyse` runs **one** depth-first search (Hopcroft-Tarjan) and comes back
with every articulation point, every bridge, and — for each of them — how many
*endpoints* stop being reachable from the anchors when it fails. The isolation
counts are the expensive-looking part and they are not: a naive implementation
removes each candidate and re-runs a traversal, which is O(V·(V+E)) and turns a
thousand-device tree into seconds of arithmetic per layer. Here they fall out of
the same DFS, from the subtree sizes and the number of anchors each subtree
contains, so the whole analysis is linear in the size of the graph.

The DFS is iterative. A rack-to-rack chain in a real inventory is nowhere near
CPython's recursion limit, but a thousand-device generated tree is, and a tool
that raises ``RecursionError`` on a large inventory fails exactly when the answer
matters most.

Three details that are easy to get wrong
----------------------------------------

**Parallel edges are not bridges.** Two cables in a LAG join the same pair of
switches; cutting one leaves the other. :class:`Graph` therefore keeps the
multiplicity of every adjacency and treats the second and later copies of the
parent edge as back edges, which is what stops either of them being reported.

**Not every node is an endpoint.** A layer-3 graph has nodes standing for IP
prefixes and a layer-2 graph has nodes standing for broadcast domains. Neither
is something a person loses service on, so :attr:`Graph.endpoints` decides what
the isolation counts count, and a node outside it contributes nothing to any
total.

**An anchor can be a single point of failure without being an articulation
point.** Remove the only gateway and everything downstream is cut off from the
network even though the graph never split. The counts are therefore computed for
*every* node from the same subtree arithmetic rather than only for the
articulation points, and a candidate is worth reporting when it isolates
something — which is the definition an operator would give.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "LINK",
    "NODE",
    "Analysis",
    "Cut",
    "Edge",
    "Graph",
    "Separator",
    "analyse",
    "components",
    "reachable",
    "separators",
]

#: :attr:`Cut.kind` of a node that fails — a device, an adapter, a PDU.
NODE: Final = "node"
#: :attr:`Cut.kind` of a link that fails — a cable, an attachment, a tunnel.
LINK: Final = "link"


@dataclass(frozen=True, slots=True)
class Edge:
    """One link, with the identity its inventory gave it."""

    #: Stable identity, unique in the graph: a cable's fully-qualified name.
    id: str
    source: str
    target: str

    @property
    def ends(self) -> tuple[str, str]:
        return (self.source, self.target)


@dataclass(frozen=True, slots=True)
class Graph:
    """A named, undirected multigraph, frozen and ready to be searched.

    Build one with :meth:`of`; the adjacency is derived there, so no caller can
    hand in an adjacency that disagrees with the edge list.
    """

    #: Node names in the caller's order. Every traversal here iterates this
    #: rather than a set, which is what makes the output reproducible.
    nodes: tuple[str, ...] = ()
    #: Links in the caller's order. A self-link is kept — it is a real cable —
    #: but joins nothing to nothing and is skipped by the searches.
    edges: tuple[Edge, ...] = ()
    #: The nodes an isolation count counts. Defaults to every node.
    endpoints: frozenset[str] = frozenset()
    #: ``node -> ((neighbour, (edge id, ...)), ...)``, neighbours in first-seen
    #: order so a depth-first search visits them the same way on every run.
    adjacency: Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]] = field(default_factory=dict)
    #: ``node -> position in`` :attr:`nodes`, for sorting anything back into
    #: graph order.
    position: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        nodes: Iterable[str],
        edges: Iterable[tuple[str, str, str]],
        *,
        endpoints: Iterable[str] | None = None,
    ) -> Graph:
        """Build a graph from node names and ``(id, source, target)`` triples.

        An edge naming a node that is not in ``nodes`` is dropped rather than
        implicitly declaring one: the caller decides what the graph contains,
        and a link to something outside it is a link to nothing.
        """
        ordered = tuple(dict.fromkeys(nodes))
        known = set(ordered)
        kept: list[Edge] = []
        neighbours: dict[str, dict[str, list[str]]] = {node: {} for node in ordered}
        for identity, source, target in edges:
            if source not in known or target not in known:
                continue
            kept.append(Edge(id=identity, source=source, target=target))
            if source == target:
                continue
            neighbours[source].setdefault(target, []).append(identity)
            neighbours[target].setdefault(source, []).append(identity)
        return cls(
            nodes=ordered,
            edges=tuple(kept),
            endpoints=frozenset(ordered if endpoints is None else (set(endpoints) & known)),
            adjacency={
                node: tuple((peer, tuple(ids)) for peer, ids in peers.items())
                for node, peers in neighbours.items()
            },
            position={node: index for index, node in enumerate(ordered)},
        )

    def weight(self, node: str) -> int:
        """1 when losing ``node`` costs somebody service, 0 when it does not."""
        return 1 if node in self.endpoints else 0

    def order(self, names: Iterable[str]) -> tuple[str, ...]:
        """``names`` sorted back into graph order, without repeats."""
        return tuple(sorted(dict.fromkeys(names), key=lambda name: self.position.get(name, -1)))

    def edge(self, identity: str) -> Edge | None:
        """The edge with this id, or ``None``."""
        return next((edge for edge in self.edges if edge.id == identity), None)

    @property
    def endpoint_count(self) -> int:
        return len(self.endpoints)


# --------------------------------------------------------------------------- #
# Traversal
# --------------------------------------------------------------------------- #


def reachable(
    graph: Graph,
    sources: Iterable[str],
    *,
    without_nodes: Iterable[str] = (),
    without_edges: Iterable[str] = (),
) -> frozenset[str]:
    """Every node reachable from ``sources``, with part of the graph removed.

    Args:
        graph: The graph to search.
        sources: Where the search starts. A source that is itself removed, or
            that names no node, contributes nothing.
        without_nodes: Nodes that have failed. Removing a node removes every
            link that terminates on it.
        without_edges: Edge ids that have failed.

    Returns:
        The reachable set, sources included. Removed nodes are never in it.
    """
    gone_nodes = set(without_nodes)
    gone_edges = set(without_edges)
    seen: set[str] = set()
    frontier: list[str] = []
    for source in sources:
        if source in graph.position and source not in gone_nodes and source not in seen:
            seen.add(source)
            frontier.append(source)
    while frontier:
        current = frontier.pop()
        for neighbour, identities in graph.adjacency.get(current, ()):
            if neighbour in seen or neighbour in gone_nodes:
                continue
            if gone_edges and all(identity in gone_edges for identity in identities):
                continue
            seen.add(neighbour)
            frontier.append(neighbour)
    return frozenset(seen)


def components(
    graph: Graph,
    *,
    without_nodes: Iterable[str] = (),
    without_edges: Iterable[str] = (),
) -> tuple[tuple[str, ...], ...]:
    """The connected components, each in graph order, ordered by first member."""
    gone_nodes = set(without_nodes)
    gone_edges = set(without_edges)
    seen: set[str] = set()
    found: list[tuple[str, ...]] = []
    for start in graph.nodes:
        if start in seen or start in gone_nodes:
            continue
        reached = reachable(graph, (start,), without_nodes=gone_nodes, without_edges=gone_edges)
        seen |= reached
        found.append(graph.order(reached))
    return tuple(found)


# --------------------------------------------------------------------------- #
# The depth-first forest
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Search:
    """One walk of the graph, and everything that falls out of it.

    A pure function of the graph — the anchors are not in here, because they are
    the caller's question rather than a property of the topology, and keeping
    them out is what lets one walk answer several.
    """

    #: Discovery time per node; a node absent from it is not in the graph.
    discovered: Mapping[str, int]
    #: DFS parent, ``None`` for the root of each tree.
    parent: Mapping[str, str | None]
    #: Nodes in discovery order.
    order: tuple[str, ...]
    #: Children ``c`` of ``v`` whose subtree falls off when ``v`` fails. For a
    #: root that is every child, which is why nothing here special-cases roots.
    detached: Mapping[str, tuple[str, ...]]
    #: Endpoints in each node's subtree, the node itself included.
    size: Mapping[str, int]
    #: *Nodes* in each node's subtree, endpoint or not. With the discovery time
    #: this makes "is x below v" an interval test rather than a walk.
    span: Mapping[str, int]
    #: Root of the DFS tree each node belongs to.
    root: Mapping[str, str]
    #: Endpoints per component, keyed by its root.
    component_size: Mapping[str, int]
    articulations: frozenset[str]
    bridges: frozenset[str]
    #: Edge id of the tree edge joining each node to its parent.
    tree_edge: Mapping[str, str]

    def below(self, ancestor: str, node: str) -> bool:
        """Is ``node`` in ``ancestor``'s subtree? ``ancestor`` itself counts."""
        start = self.discovered.get(ancestor)
        here = self.discovered.get(node)
        if start is None or here is None:
            return False
        return start <= here < start + self.span[ancestor]


def _search(graph: Graph) -> _Search:
    """One iterative Hopcroft-Tarjan pass; see the module docstring."""
    discovered: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    children: dict[str, list[str]] = {node: [] for node in graph.nodes}
    detached: dict[str, list[str]] = {node: [] for node in graph.nodes}
    tree_edge: dict[str, str] = {}
    tree_parallel: dict[str, bool] = {}
    root_of: dict[str, str] = {}
    articulations: set[str] = set()
    bridges: set[str] = set()
    order: list[str] = []
    clock = 0

    for start in graph.nodes:
        if start in discovered:
            continue
        discovered[start] = low[start] = clock
        clock += 1
        order.append(start)
        parent[start] = None
        root_of[start] = start
        # A frame is [node, parent, iterator]; a list, because the iterator is
        # advanced in place across many turns of the loop below.
        stack: list[list[Any]] = [[start, None, iter(graph.adjacency.get(start, ()))]]
        while stack:
            frame = stack[-1]
            node: str = frame[0]
            parent_node: str | None = frame[1]
            step = next(frame[2], None)
            if step is None:
                stack.pop()
                if not stack:
                    continue
                above: str = stack[-1][0]
                low[above] = min(low[above], low[node])
                if low[node] >= discovered[above]:
                    detached[above].append(node)
                    if stack[-1][1] is not None:
                        articulations.add(above)
                if low[node] > discovered[above] and not tree_parallel[node]:
                    bridges.add(tree_edge[node])
                continue

            neighbour, identities = step
            if neighbour == node:
                continue  # a self-link joins nothing to nothing
            if neighbour == parent_node:
                # The edge we came down. A second copy of it is a real cycle,
                # which is precisely why a LAG has two cables.
                if len(identities) > 1:
                    low[node] = min(low[node], discovered[neighbour])
                continue
            if neighbour not in discovered:
                discovered[neighbour] = low[neighbour] = clock
                clock += 1
                order.append(neighbour)
                parent[neighbour] = node
                root_of[neighbour] = start
                children[node].append(neighbour)
                tree_edge[neighbour] = identities[0]
                tree_parallel[neighbour] = len(identities) > 1
                stack.append([neighbour, node, iter(graph.adjacency.get(neighbour, ()))])
            elif discovered[neighbour] < discovered[node]:
                low[node] = min(low[node], discovered[neighbour])

        # The root splits the graph exactly when it has more than one child: a
        # second subtree could only ever have reached the first through it.
        if len(children[start]) > 1:
            articulations.add(start)

    size: dict[str, int] = {}
    span: dict[str, int] = {}
    for node in reversed(order):
        kids = children[node]
        size[node] = graph.weight(node) + sum(size[child] for child in kids)
        span[node] = 1 + sum(span[child] for child in kids)

    return _Search(
        discovered=discovered,
        parent=parent,
        order=tuple(order),
        detached={node: tuple(kids) for node, kids in detached.items()},
        size=size,
        span=span,
        root=root_of,
        component_size={root: size[root] for root, above in parent.items() if above is None},
        articulations=frozenset(articulations),
        bridges=frozenset(bridges),
        tree_edge=tree_edge,
    )


def _anchor_counts(search: _Search, anchors: set[str]) -> Mapping[str, int]:
    """Anchors in each node's subtree, accumulated bottom up.

    One pass over the discovery order in reverse: a node is always discovered
    before everything below it, so walking backwards visits every child before
    its parent without needing the child lists again.
    """
    counts: dict[str, int] = {node: (1 if node in anchors else 0) for node in search.order}
    for node in reversed(search.order):
        above = search.parent[node]
        if above is not None:
            counts[above] += counts[node]
    return counts


# --------------------------------------------------------------------------- #
# One failure at a time
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Cut:
    """One element whose loss would cut endpoints off from the anchors."""

    #: :data:`NODE` or :data:`LINK`.
    kind: str
    #: The node's name, or the link's edge id.
    id: str
    #: How many endpoints stop being reachable from the anchors. Never counts
    #: the failed node itself: it is gone, not stranded.
    isolated: int
    #: The two nodes a link joins; empty for a node cut.
    ends: tuple[str, ...] = ()
    #: Is this an articulation point? A node can isolate endpoints without being
    #: one — see the module docstring.
    articulation: bool = False
    #: Is this link a bridge? One link of a redundant pair is not.
    bridge: bool = False

    @property
    def is_node(self) -> bool:
        return self.kind == NODE

    @property
    def order(self) -> tuple[int, str, str]:
        """Sort key: worst first, then by kind and identity so ties are stable."""
        return (-self.isolated, self.kind, self.id)


@dataclass(frozen=True, slots=True)
class Analysis:
    """What one depth-first search found out about a graph."""

    #: Every node whose loss isolates at least one endpoint, worst first.
    nodes: tuple[Cut, ...] = ()
    #: Every link whose loss isolates at least one endpoint, worst first.
    links: tuple[Cut, ...] = ()
    #: Articulation points in graph order, whether or not they isolate anything.
    articulations: tuple[str, ...] = ()
    #: Bridges as edge ids, in edge order, likewise.
    bridges: tuple[str, ...] = ()
    #: Endpoints reachable from the anchors before anything fails.
    served: int = 0

    @property
    def cuts(self) -> tuple[Cut, ...]:
        """Nodes and links together, worst first, ties broken deterministically."""
        return tuple(sorted((*self.nodes, *self.links), key=lambda cut: cut.order))


def analyse(graph: Graph, anchors: Iterable[str] = ()) -> Analysis:
    """Find every single failure that would isolate an endpoint.

    Args:
        graph: The graph to analyse.
        anchors: The nodes that must stay reachable — the gateways, or whatever
            ``--from`` named. When none are given, there is nothing to be cut
            off *from*, so a failure is measured by what it strands outside the
            largest remaining fragment, which is the reading an operator gives a
            diagram with no gateway on it.

    Returns:
        The analysis. A cut that isolates nothing is left out of
        :attr:`Analysis.nodes` and :attr:`Analysis.links` — an articulation
        point with an anchor on either side splits the graph without costing
        anybody service — but still appears in :attr:`Analysis.articulations`
        and :attr:`Analysis.bridges`.
    """
    anchor_set = {node for node in anchors if node in graph.position}
    search = _search(graph)
    counts = _anchor_counts(search, anchor_set)
    anchored = bool(anchor_set)
    served = len(reachable(graph, anchor_set) & graph.endpoints) if anchored else 0

    node_cuts: list[Cut] = []
    for node in graph.nodes:
        parts = _node_parts(graph, search, counts, node, anchor_set)
        isolated = _isolated(parts, counts[search.root[node]] if anchored else None)
        if isolated:
            node_cuts.append(
                Cut(
                    kind=NODE,
                    id=node,
                    isolated=isolated,
                    articulation=node in search.articulations,
                )
            )

    link_cuts: list[Cut] = []
    for edge in graph.edges:
        ends = _link_parts(search, counts, edge)
        if ends is None:
            continue
        served_here = counts[search.root[edge.source]] if anchored else None
        isolated = _isolated(ends, served_here)
        if isolated:
            link_cuts.append(
                Cut(kind=LINK, id=edge.id, isolated=isolated, ends=edge.ends, bridge=True)
            )

    return Analysis(
        nodes=tuple(sorted(node_cuts, key=lambda cut: cut.order)),
        links=tuple(sorted(link_cuts, key=lambda cut: cut.order)),
        articulations=graph.order(search.articulations),
        bridges=tuple(edge.id for edge in graph.edges if edge.id in search.bridges),
        served=served,
    )


def _node_parts(
    graph: Graph,
    search: _Search,
    counts: Mapping[str, int],
    node: str,
    anchors: set[str],
) -> tuple[tuple[int, int], ...]:
    """The fragments left when ``node`` fails, as ``(endpoints, anchors)`` pairs."""
    root = search.root[node]
    total = search.component_size[root]
    total_anchors = counts[root]

    parts: list[tuple[int, int]] = []
    cut_size = 0
    cut_anchors = 0
    for child in search.detached[node]:
        parts.append((search.size[child], counts[child]))
        cut_size += search.size[child]
        cut_anchors += counts[child]
    rest_size = total - graph.weight(node) - cut_size
    rest_anchors = total_anchors - (1 if node in anchors else 0) - cut_anchors
    if rest_size > 0 or rest_anchors > 0:
        parts.append((rest_size, rest_anchors))
    return tuple(parts)


def _link_parts(
    search: _Search, counts: Mapping[str, int], edge: Edge
) -> tuple[tuple[int, int], ...] | None:
    """The two fragments left when ``edge`` fails, or ``None`` if it is no bridge."""
    if edge.id not in search.bridges:
        return None
    lower = edge.target if search.parent.get(edge.target) == edge.source else edge.source
    root = search.root[lower]
    total = search.component_size[root]
    return (
        (search.size[lower], counts[lower]),
        (total - search.size[lower], counts[root] - counts[lower]),
    )


def _isolated(parts: Sequence[tuple[int, int]], component_anchors: int | None) -> int:
    """How many endpoints lose service when a component falls into ``parts``.

    Args:
        parts: The fragments left behind, as ``(endpoints, anchors)`` pairs.
        component_anchors: How many anchors the component held *before* the
            failure, the failed element included. ``None`` when the caller
            designated no anchors at all.

    Three cases, and the third is the one worth stating. A component that never
    held an anchor was never being served, so a failure inside it isolates
    nobody — reporting its whole size would drown the real answer in the parts
    of the inventory that are deliberately off-network. A component that still
    holds one loses the fragments that do not. And a component whose only anchor
    *was* the failed element loses everything: the gateway is a single point of
    failure for its whole domain, which is exactly the finding an operator
    wants to see rather than the zero an "is it an articulation point" test
    would have returned.
    """
    if not parts:
        return 0
    if component_anchors is None:
        if len(parts) == 1:
            return 0
        return sum(size for size, _ in parts) - max(size for size, _ in parts)
    if component_anchors == 0:
        return 0
    if any(count for _, count in parts):
        return sum(size for size, count in parts if count == 0)
    return sum(size for size, _ in parts)


# --------------------------------------------------------------------------- #
# What separates two named nodes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Separator:
    """One failure that would put two named nodes into different fragments."""

    kind: str
    #: The node's name, or the link's edge id.
    id: str
    ends: tuple[str, ...] = ()

    @property
    def is_node(self) -> bool:
        return self.kind == NODE


def separators(graph: Graph, source: str, target: str) -> tuple[Separator, ...]:
    """Every single failure that would disconnect ``source`` from ``target``.

    Exact, and linear: a cut vertex ``v`` separates the two exactly when one of
    them is in a subtree that falls off ``v`` and the other is not, and a bridge
    separates them exactly when one of them is below it. Both are interval tests
    on the depth-first numbering (:meth:`_Search.below`), so nothing here removes
    an element and re-traverses.

    An empty tuple means the two are two-connected — there are two independent
    routes and no one failure takes both — which is what a ``gateway``
    expectation asks for. It is *also* what comes back when the two are already
    disconnected, so a caller must check reachability first.

    Args:
        graph: The graph to search.
        source: One end, by node name.
        target: The other.

    Returns:
        The separators, nodes before links, each in graph order. ``source`` and
        ``target`` are never among them: losing an end is not a way of
        separating it from anything.
    """
    if source not in graph.position or target not in graph.position or source == target:
        return ()
    search = _search(graph)
    if search.root[source] != search.root[target]:
        return ()

    nodes: list[Separator] = []
    for node in graph.nodes:
        if node in (source, target) or node not in search.articulations:
            continue
        if _split_by_node(search, node, source, target):
            nodes.append(Separator(kind=NODE, id=node))
    links: list[Separator] = []
    for edge in graph.edges:
        if edge.id not in search.bridges:
            continue
        lower = edge.target if search.parent.get(edge.target) == edge.source else edge.source
        if search.below(lower, source) != search.below(lower, target):
            links.append(Separator(kind=LINK, id=edge.id, ends=edge.ends))
    return (*nodes, *links)


def _split_by_node(search: _Search, node: str, source: str, target: str) -> bool:
    """Do ``source`` and ``target`` end up in different fragments without ``node``?"""
    return _fragment(search, node, source) != _fragment(search, node, target)


def _fragment(search: _Search, node: str, other: str) -> str:
    """Which fragment ``other`` lands in when ``node`` is removed.

    Named by the detached child whose subtree holds it, or ``""`` for the rest
    of the component — everything still hanging off ``node``'s own ancestors.
    """
    for child in search.detached[node]:
        if search.below(child, other):
            return child
    return ""
