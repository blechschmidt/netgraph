"""Aggregation transforms: fewer, bigger boxes for an inventory too large to draw.

Every filter netgraph had before this module removes *detail by removing
elements*: ``--namespace``, ``--kind``, ``--name`` and ``--neighbors-of`` all
answer "show me less of the network". None of them answers "show me all of the
network, in less space", which is the question a reader of a 1000-device tree
actually has. Graphviz will lay such a tree out, but the picture is a hairball;
the information a reader wants — which sites exist, how they are joined, how big
each one is — is present and unreadable.

Two transforms answer it, and both run on the resolved
:class:`~netgraph.render.graph.Graph` *before* any renderer sees it, so DOT,
Mermaid, JSON and every image format derived from them agree about what was
aggregated::

    graph = build_graph(inventory, layer=Layer.L1)
    graph = filter_graph(graph, spec)                  # remove
    graph = aggregate_graph(graph, AggregateSpec(...))  # summarise
    payload = render(graph, "svg", options)

Namespace collapsing
--------------------

:func:`collapse_namespaces` replaces every node at or below a namespace with one
node standing for the whole of it, labelled with the namespace, how many
elements of each kind it holds, and the VLANs and prefixes those elements
participate in. A link that crossed the boundary is re-attached to the collapsed
node; a link wholly inside it is not drawn at all — it is recorded in
:attr:`AggregateView.internal_links`, which is what keeps the summary honest
about what it swallowed.

The collapsed node is a first-class node: it takes an ``id`` from
:mod:`netgraph.render.ids` and a tooltip from :mod:`netgraph.render.details`
exactly as a device does, and the JSON export marks it ``"type": "aggregate"``
with the list of elements it stands for — a consumer must never mistake one box
for one device.

Which namespaces to collapse can be named outright (``--collapse sites/north``)
or derived from a depth (``--collapse-depth 1``). Depth is counted from the
**shallowest namespace that actually branches**: a top-level directory every
element shares — ``sites/`` in the campus example, and in every tree
``netgraph init`` scaffolds — is not a level a reader distinguishes, because
nothing is outside it. Skipping it is what makes ``--collapse-depth 1`` mean
"one node per site" in the tree people actually have, rather than "one node".

Link bundling
-------------

:func:`bundle_links` draws one edge per pair of nodes instead of one per link.
Four cables in a LAG, or three cables and a tunnel, stack into a band of
parallel lines that says nothing a count would not say better; the bundle
carries the count in its label, the sum of the member rates, the union of their
VLANs, and the members themselves in :attr:`BundleView.edges` so the tooltip and
the JSON export can list what was folded together.

:class:`BundleMode` has three settings rather than two because "parallel links"
covers two different claims:

``LAG``
    The default. Bundle only links whose ports are members of a declared ``lag``
    interface. The inventory has *said* these are one logical link, so drawing
    them as four is drawing an implementation detail; nothing is being guessed.
    Two distinct port-channels between one pair of switches stay two edges,
    because the key includes the aggregate interface names.
``ALL``
    ``--bundle-links``. Bundle every set of parallel links, whatever the reason
    they are parallel. This is a judgement about legibility, so it is opt-in.
``NONE``
    ``--no-bundle-links``. Draw every link, which is what a cabling document
    wants.

Order matters: :func:`aggregate_graph` bundles, then collapses, then bundles
again. The first pass sees real elements, which is where LAG membership is
knowable; the second catches the links that only became parallel once two
namespaces became two nodes. Bundling is idempotent, so the second pass is a
no-op whenever the first one already did the work.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Final

from netgraph.errors import count_text
from netgraph.loader.inventory import namespace_of
from netgraph.models import InterfaceType, Pdu
from netgraph.render.graph import Edge, EdgeKind, Graph, Node, NodeType

__all__ = [
    "AGGREGATE_ID_PREFIX",
    "AGGREGATE_KIND",
    "AggregateSpec",
    "AggregateView",
    "BundleMode",
    "BundleView",
    "aggregate_graph",
    "bundle_links",
    "collapse_namespaces",
    "collapse_targets",
    "common_prefix",
]

#: ``kind`` reported for a collapsed-namespace node. The eight element kinds of
#: §3, :data:`~netgraph.render.graph.SUBNET_KIND` and
#: :data:`~netgraph.render.graph.TUNNEL_KIND` are all taken, so this cannot
#: collide with a declared kind.
AGGREGATE_KIND: Final = "namespace"

#: Prefix of a collapsed node's identity, e.g. ``ns:sites/north``. A colon
#: cannot occur in an element's fully-qualified name (§2 name grammar), so a
#: collapsed node can never shadow a device — the same guarantee
#: :data:`~netgraph.render.graph.SUBNET_ID_PREFIX` gives.
AGGREGATE_ID_PREFIX: Final = "ns:"

#: Suffix appended to the first member's id to name a bundled edge. It holds a
#: ``#``, which a cable's fully-qualified name cannot, so a bundle id is
#: distinguishable from a link id by inspection.
_BUNDLE_SUFFIX: Final = "#bundle"

#: Kinds listed by name in a collapsed node's label before the rest are counted
#: off. An inventory has eight kinds in total, so this only ever bites a
#: namespace holding almost all of them.
_MAX_KIND_ROWS: Final = 6


class BundleMode(str, Enum):
    """Which parallel links are drawn as one edge. See the module docstring."""

    #: Draw every link separately.
    NONE = "none"
    #: Bundle declared link aggregations only. The default.
    LAG = "lag"
    #: Bundle every set of parallel links.
    ALL = "all"

    def __str__(self) -> str:
        return self.value


# --------------------------------------------------------------------------- #
# What an aggregate carries
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AggregateView:
    """One collapsed namespace: what it stands for, and how big it is."""

    #: The namespace that was collapsed, e.g. ``sites/north``.
    namespace: str
    #: Fully-qualified names of the nodes folded in, in graph order. These are
    #: elements and — where one was declared inside the namespace — tunnels.
    elements: tuple[str, ...] = ()
    #: How many of each kind, ordered by kind name.
    by_kind: Mapping[str, int] = field(default_factory=dict)
    #: The namespaces folded in, in graph order; always includes
    #: :attr:`namespace` when something sat directly in it.
    namespaces: tuple[str, ...] = ()
    #: Every VLAN a member participates in.
    vlans: frozenset[int] = frozenset()
    #: Prefixes the members are addressed in, ordered by family, then network
    #: address, then prefix length — the order :mod:`netgraph.subnets` uses.
    subnets: tuple[str, ...] = ()
    #: Ids of the links that ran wholly inside the namespace and are therefore
    #: not drawn. Recorded rather than discarded: a summary that did not say how
    #: many links it swallowed would read as a site with no cabling.
    internal_links: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """Identity of the node standing for this namespace."""
        return f"{AGGREGATE_ID_PREFIX}{self.namespace}"

    @property
    def size(self) -> int:
        """How many elements the node stands for."""
        return len(self.elements)

    @property
    def kind_text(self) -> str:
        """``1 router, 2 switches, 9 computers``, bounded to what fits a label."""
        counted = [count_text(number, kind) for kind, number in self.by_kind.items()]
        if len(counted) <= _MAX_KIND_ROWS:
            return ", ".join(counted)
        hidden = len(counted) - _MAX_KIND_ROWS
        return ", ".join(counted[:_MAX_KIND_ROWS]) + f", (+{hidden} more kinds)"

    @property
    def summary(self) -> str:
        """One line: how many elements, of which kinds."""
        return count_text(self.size, "element") + (f": {self.kind_text}" if self.by_kind else "")


@dataclass(frozen=True, slots=True)
class BundleView:
    """The links one drawn edge stands for."""

    #: The member links, in graph order. Whole :class:`~netgraph.render.graph.Edge`
    #: records rather than ids, so a tooltip or an exporter describes a member
    #: with exactly the code that describes an unbundled link.
    edges: tuple[Edge, ...] = ()
    #: The aggregate interfaces this bundle *is*, source end first, when the
    #: inventory declared one. ``None`` for a bundle of merely parallel links.
    aggregate: tuple[str, str] | None = None

    @property
    def size(self) -> int:
        return len(self.edges)

    @property
    def links(self) -> tuple[str, ...]:
        """Ids of the member links, in graph order."""
        return tuple(edge.id for edge in self.edges)

    @property
    def is_aggregation(self) -> bool:
        """Did the inventory declare these links to be one logical link?"""
        return self.aggregate is not None

    @property
    def summary(self) -> str:
        """The one phrase a label has room for: ``lag, 4 members`` / ``4 links``."""
        if self.aggregate is not None:
            return f"lag, {count_text(self.size, 'member')}"
        return count_text(self.size, "link")


@dataclass(frozen=True, slots=True)
class AggregateSpec:
    """How much of the graph to summarise rather than draw.

    Unlike :class:`~netgraph.render.graph.FilterSpec`, none of this removes
    anything: every element is still represented, and an aggregate says exactly
    which ones it stands for.
    """

    #: Namespaces to collapse outright. A namespace nested inside another named
    #: one is redundant and is dropped, so the outermost request wins.
    collapse: tuple[str, ...] = ()
    #: Collapse every namespace at this depth. See the module docstring for what
    #: depth is counted from. ``None`` derives no namespaces.
    collapse_depth: int | None = None
    bundle: BundleMode = BundleMode.LAG

    @property
    def collapses(self) -> bool:
        """Was any collapsing asked for?"""
        return bool(self.collapse) or self.collapse_depth is not None

    @property
    def is_empty(self) -> bool:
        """Would this specification leave every graph untouched?

        The default — ``LAG`` bundling, no collapsing — is *not* empty: it
        changes any graph that declares an aggregate, which is the whole point
        of it being the default. Only ``--no-bundle-links`` with no ``--collapse``
        asks for nothing at all.
        """
        return not self.collapses and self.bundle is BundleMode.NONE

    def describe(self) -> str:
        """A one-line summary for diagnostics, e.g. ``collapse-depth=1, bundle=all``."""
        parts: list[str] = []
        if self.collapse:
            parts.append(f"collapse={','.join(self.collapse)}")
        if self.collapse_depth is not None:
            parts.append(f"collapse-depth={self.collapse_depth}")
        parts.append(f"bundle={self.bundle}")
        return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def aggregate_graph(graph: Graph, spec: AggregateSpec | None = None) -> Graph:
    """Apply both transforms to ``graph``, in the order the module docstring gives.

    Returns ``graph`` itself when nothing applies, so a pipeline that never asks
    for aggregation pays nothing and produces byte-identical output.
    """
    if spec is None:
        return graph
    result = bundle_links(graph, spec.bundle)
    targets = collapse_targets(result, spec)
    if targets:
        result = collapse_namespaces(result, targets)
        # Two namespaces that became two nodes may now be joined by links that
        # were not parallel before. The first pass could not see them.
        result = bundle_links(result, spec.bundle)
    return result


# --------------------------------------------------------------------------- #
# Namespace collapsing
# --------------------------------------------------------------------------- #


def collapse_targets(graph: Graph, spec: AggregateSpec) -> tuple[str, ...]:
    """The namespaces ``spec`` asks to collapse, in the order they will be drawn.

    Named namespaces come first, then the ones a depth derives, and a namespace
    nested inside another survivor is dropped: collapsing ``sites/north`` makes
    collapsing ``sites/north/access`` unobservable, so keeping both would only
    make the result depend on iteration order.
    """
    named = tuple(
        dict.fromkeys(candidate.strip("/") for candidate in spec.collapse if candidate.strip("/"))
    )
    derived = _depth_targets(graph, spec.collapse_depth)
    return _outermost(tuple(dict.fromkeys(named + derived)))


def _depth_targets(graph: Graph, depth: int | None) -> tuple[str, ...]:
    """Every namespace ``depth`` levels below the shallowest branching one."""
    if depth is None or depth < 1:
        return ()
    namespaces = [node.namespace for node in graph.nodes.values() if _has_namespace(node)]
    if not namespaces:
        return ()
    wanted = len(common_prefix(namespaces)) + depth
    targets = [
        "/".join(parts[:wanted])
        for namespace in namespaces
        if len(parts := namespace.split("/")) >= wanted
    ]
    return tuple(dict.fromkeys(targets))


def _has_namespace(node: Node) -> bool:
    """Does this node sit in a namespace a directory of the tree produced?

    A layer-3 prefix does not: it is derived from addresses that span whatever
    namespaces hold them, so it reports the root and belongs to no site.
    """
    return not node.is_subnet


def common_prefix(namespaces: Sequence[str]) -> tuple[str, ...]:
    """The namespace components every one of ``namespaces`` starts with.

    A namespace that *is* the whole prefix contributes no target of its own —
    there is nothing below it to summarise — so an element sitting directly in
    ``sites/`` stays a node of its own while the sites around it collapse.
    """
    split = [namespace.split("/") if namespace else [] for namespace in namespaces]
    shared: list[str] = []
    for index in range(min(len(parts) for parts in split)):
        component = split[0][index]
        if any(parts[index] != component for parts in split):
            break
        shared.append(component)
    return tuple(shared)


def _outermost(targets: Sequence[str]) -> tuple[str, ...]:
    """``targets`` with every namespace nested inside another one removed."""
    return tuple(
        candidate
        for candidate in targets
        if not any(other != candidate and _is_under(candidate, other) for other in targets)
    )


def _is_under(namespace: str, prefix: str) -> bool:
    """Is ``namespace`` the namespace ``prefix``, or a descendant of it?"""
    return namespace == prefix or namespace.startswith(f"{prefix}/")


def collapse_namespaces(graph: Graph, namespaces: Iterable[str]) -> Graph:
    """Replace everything at or below each of ``namespaces`` with one node.

    Args:
        graph: A resolved graph, filtered or not.
        namespaces: Namespace prefixes. Nesting is resolved by
            :func:`collapse_targets`; passing nested namespaces directly is not
            an error, the outermost simply wins.

    Returns:
        A new graph in which each collapsed namespace is a single node of type
        :attr:`~netgraph.render.graph.NodeType.AGGREGATE`. Links that crossed a
        boundary keep their identity and attach to the collapsed node; links
        wholly inside one are dropped from :attr:`~netgraph.render.graph.Graph.edges`
        and listed in the node's :attr:`AggregateView.internal_links`. ``graph``
        is returned unchanged when no node falls inside any of ``namespaces``.
    """
    targets = _outermost(tuple(dict.fromkeys(namespaces)))
    assignment = {
        fqn: target
        for fqn, node in graph.nodes.items()
        if _has_namespace(node)
        and (target := next((t for t in targets if _is_under(node.namespace, t)), None)) is not None
    }
    if not assignment:
        return graph

    members: dict[str, list[str]] = {}
    for fqn, target in assignment.items():
        members.setdefault(target, []).append(fqn)

    edges, internal = _collapse_edges(graph, assignment)
    aggregates = {
        target: _aggregate_node(graph, target, held, internal.get(target, ()))
        for target, held in members.items()
    }

    nodes: dict[str, Node] = {}
    for fqn, node in graph.nodes.items():
        target = assignment.get(fqn)
        if target is None:
            nodes[fqn] = node
            continue
        aggregate = aggregates[target]
        # The collapsed node takes the position of its first member, so node
        # order still follows the inventory rather than the namespace names.
        nodes.setdefault(aggregate.fqn, aggregate)

    return replace(graph, nodes=nodes, edges=edges)


def _collapse_edges(
    graph: Graph, assignment: Mapping[str, str]
) -> tuple[tuple[Edge, ...], Mapping[str, tuple[str, ...]]]:
    """Re-attach the crossing links; count the ones that vanished inside."""
    kept: list[Edge] = []
    internal: dict[str, list[str]] = {}
    for edge in graph.edges:
        source, target = assignment.get(edge.source), assignment.get(edge.target)
        if source is not None and source == target:
            internal.setdefault(source, []).append(edge.id)
            continue
        moved = replace(
            edge,
            source=f"{AGGREGATE_ID_PREFIX}{source}" if source is not None else edge.source,
            target=f"{AGGREGATE_ID_PREFIX}{target}" if target is not None else edge.target,
        )
        kept.append(edge if source is None and target is None else moved)
    return tuple(kept), {name: tuple(ids) for name, ids in internal.items()}


def _aggregate_node(
    graph: Graph, namespace: str, members: Sequence[str], internal: Sequence[str]
) -> Node:
    """The single node standing for one collapsed namespace."""
    held = [graph.nodes[fqn] for fqn in members]
    by_kind: dict[str, int] = {}
    vlans: set[int] = set()
    for node in held:
        by_kind[node.kind] = by_kind.get(node.kind, 0) + 1
        vlans |= node.vlans
    view = AggregateView(
        namespace=namespace,
        elements=tuple(members),
        by_kind=dict(sorted(by_kind.items())),
        namespaces=tuple(dict.fromkeys(node.namespace for node in held)),
        vlans=frozenset(vlans),
        subnets=_prefixes(held),
        internal_links=tuple(internal),
    )
    return Node(
        fqn=view.id,
        # The whole namespace, not its last component: a box labelled ``north``
        # inside a diagram that also holds ``north`` racks would be ambiguous,
        # and the cluster around it is not guaranteed to be drawn.
        name=namespace,
        kind=AGGREGATE_KIND,
        # The parent namespace, so ``--group-by-namespace`` still draws the
        # collapsed node inside whatever contains it.
        namespace=namespace_of(namespace),
        element=None,
        ports=(),
        vlans=frozenset(vlans),
        type=NodeType.AGGREGATE,
        aggregate=view,
    )


def _prefixes(nodes: Iterable[Node]) -> tuple[str, ...]:
    """The distinct prefixes the members are addressed in, in subnet order.

    Loopback and link-local addresses are already excluded from
    :attr:`~netgraph.render.graph.Node.routable_addresses`, so a namespace does
    not claim to participate in ``127.0.0.0/8``.
    """
    networks = {
        ipaddress.ip_interface(address).network
        for node in nodes
        for address in node.routable_addresses
    }
    ordered = sorted(
        networks,
        key=lambda network: (network.version, int(network.network_address), network.prefixlen),
    )
    return tuple(str(network) for network in ordered)


# --------------------------------------------------------------------------- #
# Link bundling
# --------------------------------------------------------------------------- #


def bundle_links(graph: Graph, mode: BundleMode = BundleMode.LAG) -> Graph:
    """Draw one edge per pair of nodes instead of one per parallel link.

    Args:
        graph: A resolved graph, collapsed or not.
        mode: Which links may be folded together; see :class:`BundleMode`.

    Returns:
        A new graph whose bundled edges carry a :class:`BundleView`. A bundle
        takes the position of its first member, keeps the union of the members'
        VLANs and the sum of their declared rates, and carries the members
        themselves so nothing about them is lost. ``graph`` is returned
        unchanged when no two links would fold together, which is what keeps a
        rendering of an inventory without aggregates byte-identical.
    """
    if mode is BundleMode.NONE or not graph.edges:
        return graph

    aggregates = _aggregate_ports(graph)
    groups: dict[tuple[object, ...], list[int]] = {}
    for index, edge in enumerate(graph.edges):
        key = _bundle_key(edge, mode, aggregates)
        if key is not None:
            groups.setdefault(key, []).append(index)

    folded = {
        index: members for members in groups.values() if len(members) > 1 for index in members
    }
    if not folded:
        return graph

    edges: list[Edge] = []
    for index, edge in enumerate(graph.edges):
        members = folded.get(index)
        if members is None:
            edges.append(edge)
        elif members[0] == index:
            edges.append(_bundle(graph, members, aggregates if mode is BundleMode.LAG else {}))
    return replace(graph, edges=tuple(edges))


def _aggregate_ports(graph: Graph) -> Mapping[str, Mapping[str, str]]:
    """Per element, which declared ``lag`` interface each member port belongs to.

    Read from the declared element rather than from
    :class:`~netgraph.render.graph.PortView`, because membership is a fact about
    the *aggregate* interface — ``members: [Gi1/0/1, Gi1/0/2]`` — and a port does
    not know it is in one.
    """
    mapping: dict[str, dict[str, str]] = {}
    for fqn, node in graph.nodes.items():
        # A PDU is an element with no interfaces at all (§17.1), so there is
        # nothing on it that could be aggregated.
        if node.element is None or isinstance(node.element, Pdu):
            continue
        ports = {
            member: interface.name
            for interface in node.element.interfaces
            if interface.type is InterfaceType.LAG
            for member in interface.members or ()
        }
        if ports:
            mapping[fqn] = ports
    return mapping


def _bundle_key(
    edge: Edge, mode: BundleMode, aggregates: Mapping[str, Mapping[str, str]]
) -> tuple[object, ...] | None:
    """What makes two links foldable, or ``None`` when this one is never folded."""
    pair = tuple(sorted((edge.source, edge.target)))
    if mode is BundleMode.ALL:
        return (pair,)
    left, right = _aggregate_of(edge, pair[0], aggregates), _aggregate_of(edge, pair[1], aggregates)
    # A one-sided aggregate is a misconfiguration rather than a reason to draw
    # four cables, so one declared end is enough to bundle on.
    return (pair, left, right) if left or right else None


def _aggregate_of(edge: Edge, fqn: str, aggregates: Mapping[str, Mapping[str, str]]) -> str:
    """The ``lag`` interface ``fqn`` terminates this link on, or ``""``."""
    ports = aggregates.get(fqn)
    if ports is None:
        return ""
    terminations = [
        port
        for endpoint, port in ((edge.source, edge.source_port), (edge.target, edge.target_port))
        if endpoint == fqn and port
    ]
    return next((ports[port] for port in terminations if port in ports), "")


def _bundle(
    graph: Graph, indexes: Sequence[int], aggregates: Mapping[str, Mapping[str, str]]
) -> Edge:
    """One edge standing for the links at ``indexes``.

    Args:
        aggregates: The declared ``lag`` memberships, or an empty mapping when
            the caller is folding merely-parallel links. ``--bundle-links``
            passes nothing here even for a group that happens to hold a LAG: a
            bundle of four aggregate members and two spare cross-links is six
            cables, not a six-member port-channel, and labelling it as one would
            be a claim about the configuration rather than about the drawing.
    """
    members = tuple(member for index in indexes for member in _members_of(graph.edges[index]))
    first = graph.edges[indexes[0]]
    kinds = {member.kind for member in members}
    media = {member.medium for member in members}
    speeds = [member.speed for member in members if member.speed is not None]
    source = _aggregate_of(first, first.source, aggregates)
    target = _aggregate_of(first, first.target, aggregates)
    return Edge(
        id=f"{members[0].id}{_BUNDLE_SUFFIX}",
        # A mixed bundle is drawn as the most physical thing in it: a reader
        # must not be told that three cables and a tunnel are a tunnel.
        kind=next(iter(kinds)) if len(kinds) == 1 else _dominant_kind(kinds),
        source=first.source,
        target=first.target,
        # A LAG bundle *is* the aggregate interface, so it names it. Anything
        # else joins ports that disagree, and the ports are in the tooltip.
        source_port=source,
        target_port=target,
        medium=next(iter(media)) if len(media) == 1 else "",
        # Four 10G cables carry 40G; that is what an aggregate is for. A member
        # that declares no rate contributes nothing rather than zeroing the sum.
        speed=sum(speeds) if speeds else None,
        label=None,
        # Members differ in length by construction; the tooltip lists each one.
        length_m=None,
        vlans=frozenset().union(*(member.vlans for member in members)),
        addresses=tuple(
            dict.fromkeys(address for member in members for address in member.addresses)
        ),
        bundle=BundleView(edges=members, aggregate=(source, target) if source or target else None),
    )


def _members_of(edge: Edge) -> tuple[Edge, ...]:
    """``edge``, or the links it already stands for.

    Bundling runs twice (see :func:`aggregate_graph`), so a second pass must
    flatten rather than nest: a bundle of bundles would report two members where
    a reader can count six lines.
    """
    return edge.bundle.edges if edge.bundle is not None else (edge,)


#: Which kind a mixed bundle reports, most physical first. A bundle holding a
#: cable is a cable that also carries something logical, never the other way
#: round.
_KIND_PRECEDENCE: Final[tuple[EdgeKind, ...]] = (
    EdgeKind.CABLE,
    EdgeKind.ATTACHMENT,
    EdgeKind.TUNNEL,
    EdgeKind.ENCAPSULATION,
    EdgeKind.SUBNET,
    EdgeKind.BGP,
    EdgeKind.OSPF,
)


def _dominant_kind(kinds: Iterable[EdgeKind]) -> EdgeKind:
    present = set(kinds)
    return next(kind for kind in _KIND_PRECEDENCE if kind in present)
