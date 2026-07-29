"""What a trace is made of: endpoints, waypoints, links and paths.

The shapes here are the contract between the search
(:mod:`netgraph.trace.engine`) and everything that consumes one — the text
report, the JSON document, the highlighted rendering. They carry *resolved*
facts only: fully-qualified names, interface names that exist, the VLAN set that
survived every port on the route. Nothing here re-reads an inventory or decides
anything; the search has already done that.

Why a path is two sequences
---------------------------

A path is ``n`` waypoints and ``n - 1`` links, kept apart rather than
interleaved into one list of alternating things. Both consumers want it that
way: the text report prints an element block and then the link that leaves it,
and the JSON document is far easier to consume as two typed arrays than as one
array of a union. The invariant — ``len(links) == len(waypoints) - 1``, and
link *i* joins waypoint *i* to waypoint *i + 1* — is asserted by
:meth:`TracedPath.__post_init__` so a search that gets it wrong fails loudly
rather than producing a report that quietly skips a hop.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final

from netgraph.errors import NetgraphError
from netgraph.loader.inventory import short_name
from netgraph.render.graph import Layer, TunnelView
from netgraph.render.highlight import Highlight

__all__ = [
    "DEFAULT_MAX_HOPS",
    "MAX_PATHS",
    "Endpoint",
    "Frontier",
    "Link",
    "TraceError",
    "TraceResult",
    "TracedPath",
    "Waypoint",
]

#: How many links a path may cross before the search abandons it. A campus
#: spanning three sites is five hops end to end and the deepest realistic
#: enterprise path is well inside this; the guard exists so a mis-modelled
#: inventory full of parallel links cannot turn a question into a hang.
DEFAULT_MAX_HOPS: Final = 16

#: How many distinct paths one search collects before it stops looking. A
#: redundant *pair* is the case worth seeing, and a full mesh has combinatorially
#: many routes that say nothing new; hitting the cap is reported
#: (:attr:`TraceResult.truncated`) rather than passed off as the whole answer.
MAX_PATHS: Final = 64


class TraceError(NetgraphError):
    """A trace cannot be attempted: an endpoint names nothing, or too much.

    Distinct from *finding no path*, which is an answer rather than a failure
    and comes back as a :class:`TraceResult` with no paths in it.
    """

    #: The CLI turns this into a click usage error (exit 2) before it can reach
    #: the top level, because a bad ``SRC``/``DST`` is exactly that. The code is
    #: here for a library caller who lets it propagate; 6 is taken by
    #: :class:`~netgraph.httpserve.ServeError`.
    exit_code = 7

    def __init__(self, message: str, candidates: Iterable[str] = ()) -> None:
        #: Every element the ambiguous or unknown reference could have meant, so
        #: a front end can list them instead of only saying "no".
        self.candidates: tuple[str, ...] = tuple(candidates)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One end of a trace, resolved against the inventory.

    ``interface`` and ``address`` are set exactly when the reference pinned them
    down: ``sw1`` leaves both unset and lets the search leave by any port,
    ``sw1:port3`` fixes the interface, and ``10.0.0.7`` fixes both — which is
    what makes an address a usable way to ask the question at all.
    """

    #: What the user typed, kept for the report so the answer echoes the question.
    spec: str
    #: Fully-qualified name of the device or adapter.
    element: str
    kind: str
    interface: str | None = None
    #: The address in ``10.0.0.7/24`` form, when the reference was an address.
    address: str | None = None

    @property
    def name(self) -> str:
        """The element's short name."""
        return short_name(self.element)

    @property
    def port(self) -> str:
        """``element:interface``, or the element alone when no port was named."""
        return f"{self.element}:{self.interface}" if self.interface else self.element

    def __str__(self) -> str:
        return self.port


@dataclass(frozen=True, slots=True)
class Waypoint:
    """One element on a path, with the ports the traffic enters and leaves by.

    The first waypoint has no :attr:`ingress` and the last no :attr:`egress`:
    the traffic originates and terminates there rather than passing through.
    """

    #: Fully-qualified name.
    element: str
    kind: str
    ingress: str | None = None
    egress: str | None = None
    #: Routable addresses on the ingress port, in configuration order.
    ingress_addresses: tuple[str, ...] = ()
    egress_addresses: tuple[str, ...] = ()
    #: The VLANs still feasible when the traffic is inside this element. Empty
    #: on a routed path, and on a layer-2 path whose ports declare no membership
    #: at all — see :attr:`TracedPath.vlans`.
    vlans: frozenset[int] = frozenset()

    @property
    def name(self) -> str:
        return short_name(self.element)

    @property
    def is_origin(self) -> bool:
        return self.ingress is None

    @property
    def is_terminus(self) -> bool:
        return self.egress is None


@dataclass(frozen=True, slots=True)
class Link:
    """One link crossed between two waypoints.

    At layer 2 that is a cable, an adapter attachment or a tunnel — something
    with a medium and a rate. At layer 3 it is an IP prefix two elements are
    both addressed in, which is an adjacency rather than a wire; :attr:`kind` is
    what tells the two apart, and :attr:`subnet` is set exactly for the second.
    """

    #: Human-facing identity: the cable's fully-qualified name, the tunnel's, or
    #: the prefix. Not necessarily one of :attr:`graph_edges`.
    id: str
    #: ``cable``, ``attachment``, ``tunnel`` or ``subnet``
    #: (:class:`~netgraph.render.graph.EdgeKind`).
    kind: str
    source: str
    target: str
    source_port: str = ""
    target_port: str = ""
    #: ``copper``, ``fiber``, ``wireless``; ``""`` for anything not a cable.
    medium: str = ""
    speed: int | None = None
    #: The cable label / patch-panel identifier, or an attachment's bus type.
    label: str | None = None
    length_m: float | None = None
    #: VLANs the link carries, narrowed to what the trace found feasible.
    vlans: frozenset[int] = frozenset()
    #: The prefix crossed; set exactly on a ``subnet`` link.
    subnet: str | None = None
    #: The two addresses that put the endpoints in that prefix, source first.
    addresses: tuple[str, ...] = ()
    #: The tunnel this link is, or is realised by. A layer-2 hop over a VXLAN
    #: has one; so does a layer-3 hop whose two interfaces are the ends of one
    #: tunnel document, which is how an overlay shows up in a routed trace.
    tunnel: TunnelView | None = None
    #: :attr:`Edge.id <netgraph.render.graph.Edge.id>` of every edge of the
    #: rendered graph this link stands for, so ``--highlight`` can emphasise it
    #: without re-deriving the topology. A layer-3 hop is two of them (element
    #: to prefix, prefix to element) and a hop across a multipoint tunnel drawn
    #: as a node is two legs.
    graph_edges: tuple[str, ...] = ()
    #: Node ids the link passes *through* rather than ends on: the prefix node
    #: of a layer-3 hop, the tunnel node of a multipoint tunnel.
    graph_nodes: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """The short name of the cable, adapter or tunnel; the prefix as itself."""
        return self.id if self.kind == "subnet" else short_name(self.id.partition("#")[0])

    @property
    def is_cleartext_tunnel(self) -> bool:
        """Does this hop cross a tunnel nothing in its ``over`` chain encrypts?

        The same question ``W127`` asks of the inventory, asked of one route:
        a tunnel may be perfectly acceptable in a data centre and unacceptable
        on the path between these two elements, and only a trace can tell.
        """
        return self.tunnel is not None and not self.tunnel.protected


@dataclass(frozen=True, slots=True)
class TracedPath:
    """One route from the source to the destination."""

    waypoints: tuple[Waypoint, ...]
    links: tuple[Link, ...] = ()
    layer: Layer = Layer.L2
    #: The VLANs the whole route is feasible in — the intersection of every port
    #: it crosses that declares membership. Empty when nothing on the route
    #: declared any, which is what a routed link between two routers looks like.
    vlans: frozenset[int] = frozenset()
    #: ``ipv4`` or ``ipv6`` for a routed path; ``None`` at layer 2, where an
    #: address family is not what decides reachability.
    family: str | None = None

    def __post_init__(self) -> None:
        if len(self.links) != max(len(self.waypoints) - 1, 0):
            raise ValueError(
                f"a path of {len(self.waypoints)} waypoint(s) must have "
                f"{max(len(self.waypoints) - 1, 0)} link(s), not {len(self.links)}"
            )

    @property
    def hops(self) -> int:
        """How many links the route crosses. Zero when both ends are one element."""
        return len(self.links)

    @property
    def elements(self) -> tuple[str, ...]:
        """The fully-qualified names on the route, in order."""
        return tuple(waypoint.element for waypoint in self.waypoints)

    @property
    def key(self) -> tuple[str, ...]:
        """Identity of the route, for de-duplication and for a stable order.

        Two cables in a LAG join the same pair of switches, so the *elements*
        do not identify a path — the links do. Both are included, because two
        routes may also share their links while differing in the ports.
        """
        return (*self.elements, *(link.id for link in self.links))

    @property
    def tunnels(self) -> tuple[TunnelView, ...]:
        """The distinct tunnels the route crosses, in the order it crosses them."""
        seen: dict[str, TunnelView] = {}
        for link in self.links:
            if link.tunnel is not None:
                seen.setdefault(link.tunnel.fqn, link.tunnel)
        return tuple(seen.values())

    @property
    def cleartext_tunnels(self) -> tuple[TunnelView, ...]:
        """The tunnels on the route that nothing in their ``over`` chain encrypts."""
        seen: dict[str, TunnelView] = {}
        for link in self.links:
            if link.is_cleartext_tunnel and link.tunnel is not None:
                seen.setdefault(link.tunnel.fqn, link.tunnel)
        return tuple(seen.values())

    def highlight(self) -> Highlight:
        """The nodes and links of this route, for ``--highlight``."""
        nodes = {waypoint.element for waypoint in self.waypoints}
        edges: set[str] = set()
        for link in self.links:
            nodes.update(link.graph_nodes)
            edges.update(link.graph_edges)
        return Highlight(nodes=frozenset(nodes), edges=frozenset(edges))

    def __iter__(self) -> Iterator[Waypoint]:
        return iter(self.waypoints)


@dataclass(frozen=True, slots=True)
class Frontier:
    """How far one layer's search got before it ran out of network.

    Kept per layer rather than merged, because the two answer different
    questions: the layer-2 frontier names the last device a *frame* could reach
    and therefore where a cabling or VLAN break is, while the layer-3 one names
    the last router a *packet* could reach. A trace that fails at both is most
    often fixed by looking at the first.
    """

    layer: Layer
    #: How many elements the search could reach at all, source included.
    reached: int = 0
    #: The element furthest from the source, and therefore the last place the
    #: traffic could still have got to. ``None`` only when the source itself is
    #: not in the graph.
    furthest: str | None = None
    #: How many links away that element is.
    depth: int = 0

    @property
    def is_isolated(self) -> bool:
        """Did the search get nowhere at all from the source?"""
        return self.reached <= 1


@dataclass(frozen=True, slots=True)
class TraceResult:
    """Everything one ``netgraph path`` run found, path or no path."""

    source: Endpoint
    destination: Endpoint
    #: Paths in the order the search ranked them: fewest hops first, then by the
    #: names on the route, so two runs over one inventory agree.
    paths: tuple[TracedPath, ...] = ()
    #: The layer the reported paths were found at, or ``None`` when none were.
    layer: Layer | None = None
    #: The VLAN ``--vlan`` forced, when one was given.
    forced_vlan: int | None = None
    max_hops: int = DEFAULT_MAX_HOPS
    #: True when the search stopped at :data:`MAX_PATHS`, so the caller can say
    #: that the list is a sample rather than the whole answer.
    truncated: bool = False
    #: How far each searched layer got, in the order the layers were tried.
    frontiers: tuple[Frontier, ...] = ()
    #: Anything the reader needs that is neither a path nor a failure: which
    #: layer was skipped and why, whether an endpoint is unaddressed.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        return bool(self.paths)

    @property
    def attempted(self) -> tuple[Layer, ...]:
        """The layers that were searched, in the order they were tried."""
        return tuple(frontier.layer for frontier in self.frontiers)

    @property
    def shortest(self) -> TracedPath | None:
        """The first path, which the search ordered to be a shortest one."""
        return self.paths[0] if self.paths else None

    def selected(self, *, all_paths: bool) -> tuple[TracedPath, ...]:
        """The paths to report: every one, or the shortest alone."""
        if all_paths or not self.paths:
            return self.paths
        return (self.paths[0],)

    def highlight(self, *, all_paths: bool) -> Highlight:
        """The union of the reported paths, for ``--highlight``."""
        result = Highlight()
        for path in self.selected(all_paths=all_paths):
            result = result | path.highlight()
        return result

    @property
    def cleartext_tunnels(self) -> tuple[TunnelView, ...]:
        """Every unprotected tunnel on any reported path, without repeats."""
        seen: dict[str, TunnelView] = {}
        for path in self.paths:
            for view in path.cleartext_tunnels:
                seen.setdefault(view.fqn, view)
        return tuple(seen.values())
