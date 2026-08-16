"""Reading an attribute off the resolved model.

One function per domain, each a pure ``(subject, attribute, qualifier) ->
values`` — a tuple of strings, empty when the subject has nothing to say. Text
is the common currency on purpose: a comparison decides for itself whether to
read ``"10.1.0.1/30"`` as a network or as a string, and a facts function that
returned five different Python types would push that decision here, where it
would have to be made without knowing the operator.

Nothing in this module reads a file, resolves a name or builds a graph. Every
answer comes out of the :class:`~netviz.render.graph.Graph` the caller already
has, which is what makes a query cheap enough to run on every keystroke in the
editor's search box.

The subject of the element domain is a :class:`~netviz.render.graph.Node`, so
a query sees exactly what the current *layer* drew. That is deliberate: at layer
3 a machine's containers are nodes of their own, and a query run against a layer
3 view should find them, because that is what the reader is looking at.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from netviz.render.graph import Edge, Graph, NetnsView, Node, PortView, SecurityView

__all__ = [
    "Facts",
    "LinkEnd",
    "element_values",
    "interface_values",
    "link_values",
    "netns_values",
    "ports_of",
    "zone_values",
]

#: What ``true`` and ``false`` read as. A BOOL attribute's value is normalised
#: to one of these by the parser, so a comparison is plain string equality.
_TRUE: Final = "true"
_FALSE: Final = "false"


def _text(value: object) -> tuple[str, ...]:
    """One value, or none when it is unset. The shape every reader returns."""
    if value is None or value == "":
        return ()
    return (str(value),)


def _flag(value: bool) -> tuple[str, ...]:
    return (_TRUE if value else _FALSE,)


@dataclass(frozen=True, slots=True)
class LinkEnd:
    """One link, seen from one of its ends.

    ``link[peer-kind = router]`` is a question about the *far* end, and which end
    is far depends on which element is being tested — so a link is paired with
    the element the query arrived from before any of its attributes are read.
    """

    edge: Edge
    #: Fully-qualified name of the element the query is being evaluated for.
    near: str
    #: The node at the other end, when the graph still holds it.
    peer: Node | None
    #: The far end's fully-qualified name, even when its node was filtered out.
    peer_fqn: str
    #: The interface the link attaches to on the near element, ``""`` if none.
    near_port: str
    peer_port: str

    @classmethod
    def of(cls, graph: Graph, edge: Edge, near: str) -> LinkEnd:
        far = edge.target if edge.source == near else edge.source
        near_port = edge.source_port if edge.source == near else edge.target_port
        far_port = edge.target_port if edge.source == near else edge.source_port
        return cls(
            edge=edge,
            near=near,
            peer=graph.nodes.get(far),
            peer_fqn=far,
            near_port=near_port or "",
            peer_port=far_port or "",
        )


@dataclass(frozen=True, slots=True)
class Facts:
    """Everything the evaluator needs to answer questions about one graph.

    Built once per evaluation and indexed by fully-qualified name, because a
    query with three terms in it would otherwise walk the edge list three times
    per node — which is the difference between a search box that keeps up with
    typing on a thousand-device inventory and one that does not.
    """

    graph: Graph
    #: fqn -> the links incident to it, in graph order.
    incident: Mapping[str, tuple[Edge, ...]]
    #: fqn -> the fully-qualified names one hop away, in graph order.
    adjacent: Mapping[str, tuple[str, ...]]
    #: fqn -> the inventory-relative path of the document declaring it.
    origin: Mapping[str, str]

    @classmethod
    def of(cls, graph: Graph) -> Facts:
        incident: dict[str, list[Edge]] = {fqn: [] for fqn in graph.nodes}
        adjacent: dict[str, list[str]] = {fqn: [] for fqn in graph.nodes}
        seen: dict[str, set[str]] = {fqn: set() for fqn in graph.nodes}
        for edge in graph.edges:
            for near, far in ((edge.source, edge.target), (edge.target, edge.source)):
                if near not in incident:
                    continue
                incident[near].append(edge)
                if far != near and far not in seen[near]:
                    seen[near].add(far)
                    adjacent[near].append(far)
        return cls(
            graph=graph,
            incident={fqn: tuple(edges) for fqn, edges in incident.items()},
            adjacent={fqn: tuple(names) for fqn, names in adjacent.items()},
            origin=_origins(graph),
        )

    def links_of(self, node: Node) -> tuple[LinkEnd, ...]:
        """Every link incident to ``node``, paired with the end it is seen from."""
        return tuple(
            LinkEnd.of(self.graph, edge, node.fqn) for edge in self.incident.get(node.fqn, ())
        )


def _origins(graph: Graph) -> dict[str, str]:
    """fqn -> the inventory-relative path of the document that declared it.

    ``Graph.sources`` already holds exactly this, keyed the same way; the copy
    exists so ``file`` is read through the one accessor every other attribute is
    and so a derived node, which nobody wrote, answers nothing rather than
    raising.
    """
    return {fqn: where.relative for fqn, where in graph.sources.items()}


# --------------------------------------------------------------- the element


def element_values(facts: Facts, node: Node, attribute: str, qualifier: str) -> tuple[str, ...]:
    """Every value ``attribute`` has on ``node``.

    ``attribute`` is the canonical row name from
    :mod:`netviz.query.attributes`, so aliases have already been resolved;
    ``qualifier`` is the part after the dot for a family such as ``label.role``.
    """
    if attribute == "name":
        return (node.name,)
    if attribute == "fqn":
        return (node.fqn,)
    if attribute == "namespace":
        return (node.namespace,) if node.namespace else ("",)
    if attribute == "kind":
        return (node.kind,)
    if attribute == "type":
        return (node.type.value,)
    if attribute == "description":
        return _text(_metadata(node, "description"))
    if attribute == "label":
        return _text(_labels(node).get(qualifier))
    if attribute in ("vendor", "model", "serial", "location"):
        return _text(getattr(_spec(node), attribute, None))
    if attribute == "vlan":
        return tuple(str(vlan) for vlan in sorted(node.vlans))
    if attribute == "address":
        return _unique(port.addresses for port in ports_of(node))
    if attribute == "routable-address":
        return _unique(port.routable_addresses for port in ports_of(node))
    if attribute == "prefix":
        return _text(node.subnet.prefix) if node.subnet is not None else ()
    if attribute == "interface":
        return tuple(port.name for port in ports_of(node))
    if attribute == "mac":
        return _unique([port.mac] for port in ports_of(node) if port.mac)
    if attribute == "mtu":
        return _unique([str(port.mtu)] for port in ports_of(node) if port.mtu is not None)
    if attribute == "vrf":
        return _vrfs(node)
    if attribute == "netns":
        return _netns_names(node)
    if attribute == "zone":
        return tuple(zone.name for zone in _zones(node))
    if attribute == "asn":
        return _text(node.routing.asn) if node.routing is not None else _text(_asn(node))
    if attribute == "router-id":
        return _text(node.routing.router_id) if node.routing is not None else ()
    if attribute == "area":
        return _text(node.routing.area) if node.routing is not None else ()
    if attribute == "degree":
        return (str(len(facts.incident.get(node.fqn, ()))),)
    if attribute == "ports":
        return (str(len(ports_of(node))),)
    if attribute == "file":
        return _text(facts.origin.get(node.fqn))
    return ()  # pragma: no cover - every row above is covered by the vocabulary test


def _spec(node: Node) -> object:
    return getattr(node.element, "spec", None)


def _metadata(node: Node, field: str) -> object:
    metadata = getattr(node.element, "metadata", None)
    return getattr(metadata, field, None)


def _labels(node: Node) -> Mapping[str, str]:
    labels = _metadata(node, "labels")
    return labels if isinstance(labels, Mapping) else {}


def ports_of(node: Node) -> tuple[PortView, ...]:
    """The interfaces a query sees on this node.

    A netns node stands for one stack of a machine, and its ports are that
    stack's — so ``interface[…]`` at the netns layer asks about the container
    rather than about the whole host, which is the only reading under which the
    node the reader clicked and the node the query matched are the same thing.

    Public because the evaluator's ``interface[…]`` scope has to iterate exactly
    what ``address`` and ``interface`` read: two accessors that disagreed would
    make ``has address`` and ``interface[has address]`` mean different things.
    """
    if node.netns is not None and not node.is_element:
        return node.netns.ports
    return node.ports


def _vrfs(node: Node) -> tuple[str, ...]:
    """Every VRF the element declares or binds an interface to, deduplicated."""
    found: dict[str, None] = {}
    for port in ports_of(node):
        if port.vrf:
            found[port.vrf] = None
    if node.routing is not None:
        for instance, _rd in node.routing.vrfs:
            found[instance] = None
    for declared in getattr(_spec(node), "vrfs", ()) or ():
        written = getattr(declared, "name", None)
        if written:
            found[str(written)] = None
    return tuple(found)


def _netns_names(node: Node) -> tuple[str, ...]:
    """Every network namespace the element runs, the initial one excluded.

    The initial namespace is spelled ``""`` everywhere in §23 and is not
    something anybody asks for by name, so ``has netns`` means "runs a container"
    rather than "is a machine".
    """
    found: dict[str, None] = {}
    if node.netns is not None and node.netns.name:
        found[node.netns.name] = None
    for declared in getattr(_spec(node), "netns", ()) or ():
        name = getattr(declared, "name", None)
        if name:
            found[str(name)] = None
    for port in ports_of(node):
        if port.netns:
            found[port.netns] = None
    return tuple(found)


def _zones(node: Node) -> tuple[SecurityView, ...]:
    if node.security is not None:
        return (node.security,)
    return ()


def _asn(node: Node) -> object:
    routing = getattr(_spec(node), "routing", None)
    bgp = getattr(routing, "bgp", None)
    return getattr(bgp, "asn", None)


def _unique(groups: Iterable[Sequence[str | None]]) -> tuple[str, ...]:
    """Flatten, drop the empties, keep the first occurrence of each."""
    found: dict[str, None] = {}
    for group in groups:
        for value in group:
            if value:
                found[str(value)] = None
    return tuple(found)


# ------------------------------------------------------------- the interface


def interface_values(port: PortView, element: Node, attribute: str) -> tuple[str, ...]:
    """Every value ``attribute`` has on one interface."""
    if attribute == "name":
        return (port.name,)
    if attribute == "type":
        return (port.type,)
    if attribute == "description":
        return _text(port.description)
    if attribute == "enabled":
        return _flag(port.enabled)
    if attribute == "address":
        return tuple(port.addresses)
    if attribute == "routable-address":
        return tuple(port.routable_addresses)
    if attribute == "mac":
        return _text(port.mac)
    if attribute == "mtu":
        return _text(port.mtu)
    if attribute == "vlan":
        return tuple(str(vlan) for vlan in sorted(port.vlans))
    if attribute == "vlan-mode":
        return _text(port.vlan_mode)
    if attribute == "vrf":
        return _text(port.vrf)
    if attribute == "netns":
        return _text(port.netns)
    if attribute == "peer":
        return _text(port.peer)
    if attribute == "element":
        return (element.fqn,)
    return ()  # pragma: no cover - covered by the vocabulary test


# ------------------------------------------------------------------ the link


def link_values(end: LinkEnd, attribute: str) -> tuple[str, ...]:
    """Every value ``attribute`` has on one incident link."""
    edge = end.edge
    if attribute == "id":
        return (edge.id,)
    if attribute == "kind":
        return (edge.kind.value,)
    if attribute == "medium":
        return _text(edge.medium)
    if attribute == "speed":
        return _text(edge.speed)
    if attribute == "length":
        return () if edge.length_m is None else (str(int(edge.length_m)),)
    if attribute == "label":
        return _text(edge.label)
    if attribute == "vlan":
        return tuple(str(vlan) for vlan in sorted(edge.vlans))
    if attribute == "port":
        return _text(end.near_port)
    if attribute == "peer":
        return (end.peer_fqn,)
    if attribute == "peer-name":
        return (end.peer.name,) if end.peer is not None else (end.peer_fqn.rsplit("/", 1)[-1],)
    if attribute == "peer-kind":
        return (end.peer.kind,) if end.peer is not None else ()
    if attribute == "peer-namespace":
        return (end.peer.namespace,) if end.peer is not None else ()
    if attribute == "peer-port":
        return _text(end.peer_port)
    return ()  # pragma: no cover - covered by the vocabulary test


# ----------------------------------------------------------------- the netns


def netns_values(view: NetnsView, attribute: str) -> tuple[str, ...]:
    """Every value ``attribute`` has on one network namespace."""
    if attribute == "name":
        return (view.name,)
    if attribute == "parent":
        return _text(view.parent)
    if attribute == "depth":
        return (str(len(view.path)),)
    if attribute == "description":
        return _text(view.description)
    if attribute == "interface":
        return tuple(port.name for port in view.ports)
    if attribute == "address":
        return _unique(port.addresses for port in view.ports)
    return ()  # pragma: no cover - covered by the vocabulary test


# ------------------------------------------------------------------ the zone


def zone_values(view: SecurityView, attribute: str) -> tuple[str, ...]:
    """Every value ``attribute`` has on one firewall zone."""
    if attribute == "name":
        return (view.name,)
    if attribute == "description":
        return _text(view.description)
    if attribute == "interface":
        return tuple(view.interfaces)
    if attribute == "rules":
        return (str(view.rules),)
    if attribute == "translations":
        return (str(view.translations),)
    if attribute == "declared":
        return _flag(view.is_declared)
    return ()  # pragma: no cover - covered by the vocabulary test
