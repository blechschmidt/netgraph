"""The layers a failure is measured against, as searchable graphs.

Everything in :mod:`netgraph.impact` reduces to the same shape: a
:class:`netgraph.connectivity.Graph` of one layer, plus enough of the resolved
topology to say what each node and edge *is* when the report names it. This
module is the only place that knows how to get from an
:class:`~netgraph.loader.inventory.Inventory` to that shape, so the failure
simulation, the single-point-of-failure sweep and the editor overlay cannot end
up analysing three subtly different graphs.

Nothing here re-reads the inventory or re-resolves a reference: every view is
built from :func:`~netgraph.render.graph.build_graph`, the same pass the
renderers and the trace engine consume, exactly as
:mod:`netgraph.graph` promises.

The four views
--------------

``l1``
    The physical plant: elements joined by cables and adapter attachments. A
    patch panel is not a node — a passive cross-connect takes no decision and
    :func:`~netgraph.render.graph.build_graph` already splices the run through
    it — so cutting one of *its* runs shows up as the cable it really is.
``l2``
    The broadcast domains, as the bipartite graph
    :func:`~netgraph.graph.layers` builds: each domain joined to each of its
    members. A VLAN that a failure splits in two comes back as two domains,
    because :func:`~netgraph.graph.broadcast_domains` partitions by what
    actually carries the VLAN rather than by the number on it.
``l3``
    The routed adjacency: elements joined through the IP prefixes they share.
``power``
    The feeds :mod:`netgraph.power` resolved: each PDU and each PSE joined to
    everything it powers.

Why layer 3 is gated on layer 1
--------------------------------

Two elements are adjacent at layer 3 when they hold addresses in one prefix.
That is the right definition for a *diagram* and the wrong one for a failure
simulation: cut the only cable between two routers and they still hold addresses
in the same prefix, so an ungated layer-3 view would report that nothing
happened. :func:`views` therefore splits each prefix node by the layer-1
component its members are in, exactly the way a VLAN is split into broadcast
domains — a prefix whose members can no longer reach each other is two prefixes
that happen to share a number, and saying so is the whole point of the command.

Layer 1 rather than layer 2 is the gate on purpose. An untagged network declares
no VLAN at all, so its elements belong to no broadcast domain, and gating on
domains would report every flat network as fully partitioned.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import networkx as nx

from netgraph.connectivity import Graph
from netgraph.graph import ELEMENT_TYPE, layers, to_networkx
from netgraph.loader.inventory import Inventory, namespace_of, short_name
from netgraph.power import FeedKind, PowerPlan, power_plan
from netgraph.render.graph import Layer, build_graph

__all__ = [
    "FAILABLE_LINKS",
    "LAYERS",
    "LAYER_TITLES",
    "POWER",
    "LayerView",
    "views",
]

#: Link kinds a technician could unplug, cut or tear down. A VLAN membership
#: edge and a subnet membership edge are relationships rather than things, so
#: neither is here — see :meth:`LayerView.is_failable`.
FAILABLE_LINKS: Final[frozenset[str]] = frozenset({"cable", "attachment", "tunnel"})

#: The layers ``--layer`` accepts, in report order.
LAYERS: Final[tuple[str, ...]] = ("l1", "l2", "l3")

#: The power plan, reported by ``--spof`` alongside the three network layers but
#: not something a ``--fail`` walks: a lost feed is a lost *element*, and the
#: cascade turns it into one before any layer is searched.
POWER: Final = "power"

#: What each view is, in the words the report prints next to the layer name.
LAYER_TITLES: Final[Mapping[str, str]] = {
    "l1": "physical",
    "l2": "broadcast domains",
    "l3": "routed adjacency",
    POWER: "power feeds",
}


@dataclass(frozen=True, slots=True)
class LayerView:
    """One layer, searchable, and able to name what it holds."""

    #: ``l1``, ``l2``, ``l3`` or :data:`POWER`.
    layer: str
    #: The searchable graph. Its endpoints are the declared elements.
    graph: Graph
    #: ``node -> kind``: the element's kind, ``vlan`` for a broadcast domain,
    #: ``subnet`` for a prefix, ``tunnel`` for a tunnel drawn as a node.
    kinds: Mapping[str, str] = field(default_factory=dict)
    #: ``node -> namespace``, for the elements only.
    namespaces: Mapping[str, str] = field(default_factory=dict)
    #: ``edge id -> what it is``: ``cable``, ``attachment``, ``tunnel``,
    #: ``vlan``, ``subnet`` or ``feed``.
    link_kinds: Mapping[str, str] = field(default_factory=dict)
    #: ``derived node -> the node of the underlying inventory graph``, for a
    #: prefix this view had to split. Empty when nothing was split.
    split_from: Mapping[str, str] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return LAYER_TITLES.get(self.layer, self.layer)

    @property
    def label(self) -> str:
        """``l1 (physical)`` — how the text report heads the section."""
        return f"{self.layer} ({self.title})"

    def describe(self, node: str) -> str:
        """A node as the report names it: its short name, or the derived id."""
        if node in self.namespaces:
            return short_name(node)
        return self.split_from.get(node, node)

    def elements(self) -> tuple[str, ...]:
        """The declared elements of this view, in graph order."""
        return tuple(node for node in self.graph.nodes if node in self.graph.endpoints)

    def is_failable(self, kind: str, identity: str) -> bool:
        """Is this node or link something a person could actually lose?

        A layer-2 graph is a bipartite drawing of broadcast domains and a
        layer-3 graph is one of IP prefixes. Both have nodes that are cut
        vertices of a real graph and are not *things*: nobody unplugs a VLAN,
        and reporting one as a single point of failure would put an item on a
        maintenance plan that no engineer could act on. The same goes for the
        membership edges that join an element to one.
        """
        if kind == "node":
            return identity in self.graph.endpoints
        return self.link_kinds.get(identity, "") in FAILABLE_LINKS


def views(
    inventory: Inventory,
    wanted: Sequence[str] = LAYERS,
    *,
    plan: PowerPlan | None = None,
) -> tuple[LayerView, ...]:
    """Build the requested views of ``inventory``, in :data:`LAYERS` order.

    Args:
        inventory: A tree loaded by :func:`~netgraph.loader.load_tree`.
        wanted: Which views to build. Unknown names are ignored rather than
            raising: the CLI has already constrained the choice, and a library
            caller asking for a layer that does not exist wants the ones that do.
        plan: A power plan already resolved for this inventory, when the caller
            has one. Only consulted for the :data:`POWER` view.

    Returns:
        One :class:`LayerView` per requested layer. Building ``l2`` costs
        nothing extra when ``l1`` was asked for: both come out of one physical
        resolution.
    """
    selected = [name for name in (*LAYERS, POWER) if name in set(wanted)]
    if not selected:
        return ()

    built: dict[str, LayerView] = {}
    physical: nx.MultiGraph | None = None
    if {"l1", "l2", "l3"} & set(selected):
        physical = to_networkx(build_graph(inventory, layer=Layer.L1))
    if physical is not None and {"l1", "l2"} & set(selected):
        split = layers(physical)
        if "l1" in selected:
            built["l1"] = _view("l1", split.l1)
        if "l2" in selected:
            built["l2"] = _view("l2", split.l2)
    if "l3" in selected:
        assert physical is not None  # selected implies the branch above ran
        built["l3"] = _routed_view(physical, to_networkx(build_graph(inventory, layer=Layer.L3)))
    if POWER in selected:
        built[POWER] = _power_view(inventory, plan if plan is not None else power_plan(inventory))
    return tuple(built[name] for name in selected if name in built)


# --------------------------------------------------------------------------- #
# The network layers
# --------------------------------------------------------------------------- #


def _view(layer: str, graph: nx.MultiGraph) -> LayerView:
    """A view straight out of a resolved layer, nothing rewritten."""
    nodes = tuple(graph.nodes)
    endpoints = tuple(node for node, data in graph.nodes(data=True) if _is_element(data))
    edges = tuple((str(key), source, target) for source, target, key in graph.edges(keys=True))
    return LayerView(
        layer=layer,
        graph=Graph.of(nodes, edges, endpoints=endpoints),
        kinds={node: str(data.get("kind", "")) for node, data in graph.nodes(data=True)},
        namespaces={node: namespace_of(node) for node in endpoints},
        link_kinds={
            str(key): str(data.get("kind", ""))
            for _, _, key, data in graph.edges(keys=True, data=True)
        },
    )


def _routed_view(physical: nx.MultiGraph, routed: nx.MultiGraph) -> LayerView:
    """The layer-3 view, with every prefix split by physical reachability.

    See the module docstring: an ungated prefix node would keep two routers
    adjacent after the only cable between them was cut.
    """
    component = _components_of(physical)
    nodes: list[str] = []
    edges: list[tuple[str, str, str]] = []
    endpoints: list[str] = []
    kinds: dict[str, str] = {}
    namespaces: dict[str, str] = {}
    link_kinds: dict[str, str] = {}
    split_from: dict[str, str] = {}

    for node, data in routed.nodes(data=True):
        if _is_element(data):
            nodes.append(node)
            endpoints.append(node)
            kinds[node] = str(data.get("kind", ""))
            namespaces[node] = namespace_of(node)

    for node, data in routed.nodes(data=True):
        if _is_element(data):
            continue
        # Group this prefix's members by the physical island they sit on. The
        # groups keep the order the members were discovered in, so the ``#2``
        # suffix means the same thing on every run over the same tree.
        groups: dict[Any, list[tuple[str, str]]] = {}
        for neighbour, key in _incident(routed, node):
            groups.setdefault(component.get(neighbour), []).append((neighbour, key))
        for index, members in enumerate(groups.values(), start=1):
            identity = node if len(groups) == 1 else f"{node}#{index}"
            nodes.append(identity)
            kinds[identity] = str(data.get("kind", ""))
            if identity != node:
                split_from[identity] = node
            for neighbour, key in members:
                link = key if identity == node else f"{key}#{index}"
                edges.append((link, identity, neighbour))
                link_kinds[link] = str(routed.edges[neighbour, node, key].get("kind", ""))

    for source, target, key, data in routed.edges(keys=True, data=True):
        if _is_element(routed.nodes[source]) and _is_element(routed.nodes[target]):
            edges.append((str(key), source, target))
            link_kinds[str(key)] = str(data.get("kind", ""))

    return LayerView(
        layer="l3",
        graph=Graph.of(nodes, edges, endpoints=endpoints),
        kinds=kinds,
        namespaces=namespaces,
        link_kinds=link_kinds,
        split_from=split_from,
    )


def _incident(graph: nx.MultiGraph, node: str) -> tuple[tuple[str, str], ...]:
    """``(neighbour, key)`` for every edge on ``node``, in adjacency order."""
    return tuple(
        (neighbour, str(key))
        for _, neighbour, key in graph.edges(node, keys=True)
        if neighbour != node
    )


def _components_of(graph: nx.MultiGraph) -> Mapping[str, int]:
    """``node -> component index``, indices assigned in node order."""
    seen: dict[str, int] = {}
    for node in graph:
        if node in seen:
            continue
        index = len(set(seen.values()))
        for member in nx.node_connected_component(graph, node):
            seen[member] = index
    return seen


def _is_element(data: Mapping[str, Any]) -> bool:
    return str(data.get("node_type", ELEMENT_TYPE)) == ELEMENT_TYPE


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #


def _power_view(inventory: Inventory, plan: PowerPlan) -> LayerView:
    """Sources joined to what they power, one edge per resolved feed."""
    nodes: list[str] = []
    endpoints: list[str] = []
    kinds: dict[str, str] = {}
    namespaces: dict[str, str] = {}
    for fqn in plan.nodes:
        element = inventory.elements.get(fqn)
        nodes.append(fqn)
        kinds[fqn] = element.kind if element is not None else ""
        namespaces[fqn] = namespace_of(fqn)
        node = plan.node(fqn)
        # A PDU is not something anybody loses service on; it is what the
        # service is lost *from*. Counting it would inflate every blast radius
        # by one and make two PDUs look worse than one.
        if node is not None and node.is_load:
            endpoints.append(fqn)
    edges = tuple((feed.id, feed.source, feed.element) for feed in plan.feeds)
    return LayerView(
        layer=POWER,
        graph=Graph.of(nodes, edges, endpoints=endpoints),
        kinds=kinds,
        namespaces=namespaces,
        link_kinds={feed.id: str(FeedKind(feed.kind)) for feed in plan.feeds},
    )


def feed_sources(plan: PowerPlan) -> Mapping[str, tuple[str, ...]]:
    """``element -> the distinct sources feeding it``, in resolution order.

    The shape both the cascade and the power single-point-of-failure sweep read:
    an element with exactly one source loses power when that source does,
    however many cords it declares into it.
    """
    sources: dict[str, list[str]] = {}
    for feed in plan.feeds:
        found = sources.setdefault(feed.element, [])
        if feed.source not in found:
            found.append(feed.source)
    return {element: tuple(found) for element, found in sources.items()}


def unpowered(plan: PowerPlan, failed: Iterable[str]) -> tuple[str, ...]:
    """Everything that loses power when ``failed`` does, transitively.

    A PDU feeds a switch; the switch sources PoE for the access points above the
    ceiling. Losing the PDU loses all of them, and a report that stopped at the
    switch would understate the blast radius by exactly the devices nobody can
    see. The walk repeats until nothing new goes dark, which terminates because
    each round removes at least one element from a finite set.
    """
    sources = feed_sources(plan)
    dark = set(failed)
    while True:
        added = {
            element
            for element, feeding in sources.items()
            if element not in dark and all(source in dark for source in feeding)
        }
        if not added:
            return tuple(sorted(dark - set(failed)))
        dark |= added
