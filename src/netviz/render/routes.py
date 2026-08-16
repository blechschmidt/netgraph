"""The line every link in a drawing is drawn as, decided once.

:mod:`netviz.layout.routing` knows how to turn two anchors and a list of
bends into a line, and :mod:`netviz.layout.avoid` knows where the bends have
to go for that line to miss the boxes it is not attached to. This module is the
half of the job that needs a *graph*: which anchor belongs to which link, which
style each one asks for, which links are a bundle and so route as one, and how
far apart two cables between the same pair of devices have to be before a reader
can tell them apart.

It lives here rather than in the renderer because four things need the same
answer and must not each work it out:

* :mod:`netviz.render.dot` writes it into a Graphviz ``pos``;
* :mod:`netviz.render.jsonexport` publishes it for a client drawing the graph
  itself;
* :mod:`netviz.web.preview` hands it to the editor canvas, which puts a grab
  handle on every bend;
* ``netviz layout --route`` writes it into the inventory as waypoints, at
  which point it stops being computed and becomes a decision.

A route is only computed for a drawing whose every node is placed. Anywhere
else Graphviz is doing the layout, the node positions are not known until it
has, and a line drawn against half an arrangement would be a line through
nothing — see :func:`~netviz.render.dot.routing_advisories`, which is what
says so out loud.

Avoidance, and what is authoritative
------------------------------------

An orthogonal drawing routes **around** obstacles by default (``--no-avoid`` is
the escape hatch), which is what ``docs/follow-ups.md`` entry 19 asked for. Two
rules keep that from taking a decision away from the person who made it:

* a bend somebody dragged is never moved. The search fills the segments
  *between* pinned waypoints and leaves the waypoints themselves alone.
* a link whose existing line already keeps clear of every box it is not
  attached to is left exactly as it was, down to the last decimal place.
  Avoidance is not a re-draw; it is a repair, and a clean diagram renders
  byte-identically to the way it did before this existed. "Clear" is by the
  clearance rather than by the drawn border: a cable two points from a switch
  reads as a cable *on* the switch.

Computed waypoints are *not* written to the inventory by rendering. They are
recomputed every time, so a diagram cannot silently accumulate geometry nobody
chose; ``netviz layout --route`` is the command that makes one permanent, and
from then on it is an authored bend like any other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from netviz.layout.avoid import DEFAULT_BUDGET, Budget, Detour, Obstacle, Router
from netviz.layout.geometry import Geometry, LayoutMode, Routing
from netviz.layout.routing import FAN_GAP, Anchor, Route, fan_offsets
from netviz.layout.routing import route as route_link
from netviz.render.graph import Graph
from netviz.render.options import RenderOptions

__all__ = [
    "RouteCache",
    "RoutePlan",
    "anchors_of",
    "default_routing",
    "fans_of",
    "obstacles_of",
    "route_plan",
    "route_table",
]


def default_routing(graph: Graph, options: RenderOptions | None = None) -> Routing:
    """The style links in this drawing take when they do not say for themselves.

    The caller's override first — that is where ``--routing`` arrives — then the
    view's, then the inventory's, then the curve Graphviz has always drawn. A
    link with a style of its own is not consulted here; it beats all four, and
    :meth:`~netviz.layout.geometry.Geometry.routing_for` is where that is
    settled.
    """
    routing = options.routing if options is not None else None
    return graph.geometry.routing_for("", default=routing)


def anchors_of(graph: Graph) -> dict[str, Anchor]:
    """The box each placed node is left from.

    A node whose stored placement records no size gets the default box and is
    marked unmeasured. The route is still drawn — a clip a few points early is
    far better than a link that vanishes — and the renderer reports it, because
    ``netviz layout --write`` records the sizes and fixes it exactly.
    """
    return {fqn: Anchor.of(placement) for fqn, placement in graph.geometry.nodes.items()}


def obstacles_of(
    graph: Graph, *, clearance: float = DEFAULT_BUDGET.clearance, annotations: bool = True
) -> tuple[Obstacle, ...]:
    """Everything on the page a route has to keep out of.

    Every placed node, plus the annotations of §21 that pin a rectangle of their
    own — a free-standing ``kind: area`` and a placed ``kind: note``. An area
    that names *members* is deliberately **not** an obstacle: it is a zone drawn
    behind the devices it encloses, its rectangle is the hull of theirs, and
    treating it as solid would make every cable that terminates inside it
    unroutable. Its members are obstacles, which is the same thing said
    correctly.
    """
    boxes = [
        Obstacle.of(fqn, anchor, clearance) for fqn, anchor in sorted(anchors_of(graph).items())
    ]
    if annotations:
        boxes.extend(_annotation_obstacles(graph, clearance))
    return tuple(boxes)


def _annotation_obstacles(graph: Graph, clearance: float) -> Iterable[Obstacle]:
    """The §21 annotations that occupy a rectangle nothing else accounts for."""
    if not graph.annotations:
        return ()
    from netviz.render.annotations import annotation_views

    views = annotation_views(graph)
    found: list[Obstacle] = []
    for area in views.areas:
        if area.box is None or area.members:
            continue
        found.append(
            Obstacle.around(
                area.id, area.box.x, area.box.y, area.box.width, area.box.height, clearance
            )
        )
    for note in views.notes:
        if note.x is None or note.y is None or note.width is None or note.height is None:
            continue
        found.append(Obstacle.around(note.id, note.x, note.y, note.width, note.height, clearance))
    return found


def fans_of(graph: Graph) -> dict[int, float]:
    """How far each link is pushed off the direct line, by index into the graph.

    Two cables between one pair of devices land on exactly the same line once
    both ends are pinned: Graphviz's own nudging is part of *its* routing, and a
    fixed drawing does none of it. So parallel links are fanned here, each
    ending up with a line of its own to hover, to select and to drop a bend on.

    A bundle counts once rather than once per member (task 38): folding four
    cables into one trunk is done so the reader sees one line, and fanning the
    fold against itself would undo it. The bundle arrives here as a single edge,
    so this happens by construction rather than by a special case.

    A self-link is fanned too — the offset becomes how far its loop stands off
    the node — which is what separates the four VLANs terminating on one switch
    into four rings.
    """
    offsets: dict[int, float] = {}
    for pair, members in _parallel(graph).items():
        # A loop is fanned *outwards* rather than about a centre line. Centring
        # exists so that a lone link stays on the direct line between the two
        # devices, and a self-link has no such line to stay on — while the
        # offset decides how far the ring stands off the node, which is a
        # distance and cannot be signed. Centred offsets would put the first two
        # loops the same distance out in opposite directions, drawing one ring
        # on top of another.
        spread = (
            [index * FAN_GAP for index in range(len(members))]
            if len(set(pair)) == 1
            else list(fan_offsets(len(members)))
        )
        for member, offset in zip(members, spread, strict=True):
            offsets[member] = offset
    return offsets


def _parallel(graph: Graph) -> dict[tuple[str, ...], list[int]]:
    """Edge indices grouped by the unordered pair of nodes they join."""
    grouped: dict[tuple[str, ...], list[int]] = {}
    for index, edge in enumerate(graph.edges):
        grouped.setdefault(tuple(sorted((edge.source, edge.target))), []).append(index)
    return grouped


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """Every link's line, plus what deciding them cost and what was given up on."""

    #: One route per edge, in the graph's own order. ``None`` for a link this
    #: drawing cannot route.
    routes: tuple[Route | None, ...]
    #: The waypoints *routing* added for each edge, empty where it added none.
    #: Published to the editor beside the authored ones so the canvas draws the
    #: same line the renderer does without re-deriving it.
    computed: tuple[tuple[tuple[float, float], ...], ...]
    #: Links that fell back to the local rule, and why. Reported, never silent.
    detours: tuple[Detour, ...] = ()
    #: How many searches were run, how many links they moved, and how many
    #: states they popped between them — the numbers a bench prints.
    searched: int = 0
    avoided: int = 0
    expansions: int = 0
    #: Links whose route came straight out of a :class:`RouteCache`.
    reused: int = 0
    #: Links the local rule already drew clear of everything, so nothing was
    #: searched for them. Normally most of the drawing, and the reason routing
    #: costs a render so little.
    clear: int = 0

    @property
    def is_routed(self) -> bool:
        return any(route is not None for route in self.routes)


@dataclass(frozen=True, slots=True)
class _Cached:
    """One link's answer, and everything it depended on."""

    source: Anchor
    target: Anchor
    pinned: tuple[tuple[float, float], ...]
    style: Routing
    fan: float
    computed: tuple[tuple[float, float], ...]
    corners: tuple[tuple[float, float], ...]
    #: The whole answer, so a reuse hands back the object rather than rebuilding
    #: it. A :class:`~netviz.layout.routing.Route` is immutable, so sharing one
    #: between two renders is sharing a value.
    line: Route


@dataclass
class RouteCache:
    """Routes kept between renders, so a moved node re-routes only what it broke.

    The editor's problem, not the command line's. Dragging one device out of a
    thousand changes the obstacle set, and re-searching every link because of it
    would put a full render inside every drag — the interaction budget task 89
    established is what that would blow. So a link is re-searched only when one
    of the three things it actually depended on has changed:

    * either of its endpoints moved or was resized;
    * a bend was added, moved or removed on it;
    * the line it was drawn as now crosses a box it did not cross before —
      which is exactly the case "somebody dragged a switch onto my cable".

    A cache is a *rendering* optimisation and never a source of truth: every
    render from the command line starts cold, and an editor session that throws
    its cache away gets the same picture more slowly. What it cannot promise is
    that an incremental answer is identical to a cold one — the congestion term
    depends on what was routed before, and a reused route was computed against a
    slightly different drawing. It is the same *shape*; :meth:`invalidate` is
    the blunt instrument for the moment that is not good enough.
    """

    #: Keyed by ``(view, edge id)``: one session draws three layers, the same
    #: cable is an edge in each of them, and the arrangement it is placed by is
    #: a different one per view.
    entries: dict[tuple[str, str], _Cached] = field(default_factory=dict)
    #: Counted across the cache's life, for the bench and for nothing else.
    hits: int = 0
    misses: int = 0

    def invalidate(self) -> None:
        """Forget everything. The next render is a cold one."""
        self.entries.clear()

    def reusable(
        self,
        key: tuple[str, str],
        *,
        source: Anchor,
        target: Anchor,
        pinned: tuple[tuple[float, float], ...],
        style: Routing,
        fan: float,
        router: Router | None,
        exempt: frozenset[str],
    ) -> _Cached | None:
        found = self.entries.get(key)
        if found is None:
            return None
        if (
            found.source != source
            or found.target != target
            or found.pinned != pinned
            or found.style is not style
            or found.fan != fan
        ):
            return None
        if router is not None and router.blocked(found.corners, exempt=exempt):
            return None
        return found


def route_table(graph: Graph, options: RenderOptions | None = None) -> tuple[Route | None, ...]:
    """One route per edge of ``graph``, in the graph's own order.

    Indexed rather than keyed by edge id, because that is how every consumer
    already walks the edges and an index cannot collide.

    ``None`` for a link this drawing cannot route: because the arrangement does
    not place both of its ends, or because the drawing is not fully placed at
    all and Graphviz is doing the routing.
    """
    return route_plan(graph, options).routes


def route_plan(
    graph: Graph,
    options: RenderOptions | None = None,
    *,
    cache: RouteCache | None = None,
    budget: Budget = DEFAULT_BUDGET,
) -> RoutePlan:
    """Every link's line, decided together so that they can be decided apart.

    Together, because avoidance is global by nature: the obstacle grid is built
    once from every placed box and shared, and each route is charged for
    crossing the ones already drawn. Apart, because the answer per link is still
    a waypoint list, which is the same thing a person produces and the same
    thing the canvas can draw without knowing any of this.
    """
    geometry = graph.geometry
    empty = (None,) * len(graph.edges)
    if geometry.mode(graph.nodes) is not LayoutMode.FIXED:
        return RoutePlan(routes=empty, computed=((),) * len(graph.edges))
    anchors = anchors_of(graph)
    fans = fans_of(graph)
    default = default_routing(graph, options)
    router = _router(graph, options, default=default, budget=budget)
    return _plan(
        graph,
        anchors=anchors,
        geometry=geometry,
        fans=fans,
        default=default,
        router=router,
        cache=cache,
    )


def _router(
    graph: Graph, options: RenderOptions | None, *, default: Routing, budget: Budget
) -> Router | None:
    """The obstacle grid for this drawing, or ``None`` when nothing wants one.

    Built at most once per render — that is the whole reason it is here rather
    than inside the loop — and not at all unless something in the drawing is
    actually orthogonal, since the grid is the expensive half and a spline
    diagram has no use for it.
    """
    if options is not None and not options.avoid:
        return None
    wanted = default is Routing.ORTHOGONAL or any(
        graph.geometry.link(edge.id).routing is Routing.ORTHOGONAL for edge in graph.edges
    )
    if not wanted:
        return None
    return Router(
        obstacles_of(
            graph,
            clearance=budget.clearance,
            annotations=options is None or options.annotations,
        ),
        budget,
    )


def _plan(
    graph: Graph,
    *,
    anchors: Mapping[str, Anchor],
    geometry: Geometry,
    fans: Mapping[int, float],
    default: Routing,
    router: Router | None,
    cache: RouteCache | None,
) -> RoutePlan:
    routes: list[Route | None] = [None] * len(graph.edges)
    computed: list[tuple[tuple[float, float], ...]] = [()] * len(graph.edges)
    reused = 0
    groups = _parallel(graph)
    settled, kept = _settled(
        groups,
        graph,
        anchors=anchors,
        geometry=geometry,
        fans=fans,
        default=default,
        router=router,
        cache=cache,
    )
    reused += kept
    for index, drawn in settled.items():
        routes[index] = drawn
    for members in groups.values():
        if members[0] in settled:
            continue
        lanes = _lanes_for(
            members,
            graph,
            anchors=anchors,
            geometry=geometry,
            fans=fans,
            default=default,
            router=router,
            cache=cache,
        )
        for index in members:
            line, added, hit = _one(
                index,
                graph,
                anchors=anchors,
                geometry=geometry,
                fans=fans,
                default=default,
                router=router,
                cache=cache,
                lane=lanes.get(index),
            )
            routes[index] = line
            computed[index] = added
            reused += 1 if hit else 0
    return RoutePlan(
        routes=tuple(routes),
        computed=tuple(computed),
        detours=() if router is None else tuple(router.detours),
        searched=0 if router is None else router.searches,
        avoided=0 if router is None else router.avoided,
        expansions=0 if router is None else router.expansions,
        reused=reused,
        clear=len(settled),
    )


def _settled(
    groups: Mapping[tuple[str, ...], Sequence[int]],
    graph: Graph,
    *,
    anchors: Mapping[str, Anchor],
    geometry: Geometry,
    fans: Mapping[int, float],
    default: Routing,
    router: Router | None,
    cache: RouteCache | None = None,
) -> tuple[dict[int, Route], int]:
    """The links nothing is in the way of, decided and charged for *first*.

    Two passes rather than one, and the order is the point. A link whose
    existing line crosses nothing is never going to move — that is the promise
    the module docstring makes — so it is a fixed feature of the page, and a
    search that runs afterwards should be told about it. Do it the other way
    round and the first link to need a detour takes the shortest one available,
    which is regularly a corridor a straight, untouchable cable is already
    sitting in.

    A group of parallel links settles as a whole. If any member has to move, the
    bundle is routed together in the second pass, and recording half of it here
    would charge the bundle for crossing itself.

    A cache short-circuits the line itself, not merely the search. Deciding a
    clean link costs a :func:`~netviz.layout.routing.route` and a scan of the
    obstacle index, which is small per link and is paid for *every* link on
    every render — on a 200-link drawing it was the whole cost of routing, and
    the search it exists to avoid was a twentieth of it. So a link whose
    endpoints, bends, style and fan are all unchanged and whose line still
    crosses nothing is handed back verbatim.

    Returns:
        The settled routes by edge index, and how many of them came from the
        cache rather than being re-derived.
    """
    if router is None:
        return {}, 0
    settled: dict[int, Route] = {}
    kept = 0
    for members in groups.values():
        drawn: dict[int, Route] = {}
        from_cache = 0
        for index in members:
            edge = graph.edges[index]
            source, target = anchors.get(edge.source), anchors.get(edge.target)
            if source is None or target is None:
                break
            link = geometry.link(edge.id)
            style = link.routing or default
            fan = fans.get(index, 0.0)
            pinned = tuple(link.waypoints)
            exempt = frozenset({edge.source, edge.target})
            found = (
                None
                if cache is None
                else cache.reusable(
                    (geometry.view, edge.id),
                    source=source,
                    target=target,
                    pinned=pinned,
                    style=style,
                    fan=fan,
                    router=router,
                    exempt=exempt,
                )
            )
            if found is not None and not found.computed:
                drawn[index] = found.line
                from_cache += 1
                continue
            line = route_link(source, target, waypoints=pinned, style=style, fan=fan)
            if line.is_empty:
                break
            if style is Routing.ORTHOGONAL and router.blocked(line.corners, exempt=exempt):
                break
            if cache is not None and style is Routing.ORTHOGONAL:
                cache.entries[(geometry.view, edge.id)] = _Cached(
                    source=source,
                    target=target,
                    pinned=pinned,
                    style=style,
                    fan=fan,
                    computed=(),
                    corners=line.corners,
                    line=line,
                )
            drawn[index] = line
        else:
            for line in drawn.values():
                router.record(line.corners)
            settled.update(drawn)
            kept += from_cache
    return settled, kept


def _lanes_for(
    members: Sequence[int],
    graph: Graph,
    *,
    anchors: Mapping[str, Anchor],
    geometry: Geometry,
    fans: Mapping[int, float],
    default: Routing,
    router: Router | None,
    cache: RouteCache | None,
) -> dict[int, tuple[tuple[float, float], ...]]:
    """One route for a whole bundle, offset into a lane per member.

    Task 38 folds cables into a trunk when the reader asked for one; this is the
    case it did *not* fold — several distinct links between the same two devices,
    each of which the reader wants to be able to point at. Routed once and
    offset, rather than searched for N times: N independent searches through the
    same corridor produce N lines that fan out, take different ways round the
    same obstacle and re-converge, which is the opposite of what a bundle should
    look like.

    Only for the case avoidance is actually in: fewer than two members, no
    router, a self-link, an unplaced end or a link with authored bends all fall
    through to the ordinary per-link path, where the existing fan already does
    the right thing.
    """
    if router is None or len(members) < 2:
        return {}
    lead = graph.edges[members[0]]
    if lead.source == lead.target:
        return {}
    source, target = anchors.get(lead.source), anchors.get(lead.target)
    if source is None or target is None:
        return {}
    styles = {geometry.routing_for(graph.edges[index].id, default=default) for index in members}
    pinned = {geometry.link(graph.edges[index].id).waypoints for index in members}
    if styles != {Routing.ORTHOGONAL} or pinned != {()}:
        return {}
    exempt = frozenset({lead.source, lead.target})
    if cache is not None and _all_reusable(
        members,
        graph,
        cache=cache,
        source=source,
        target=target,
        fans=fans,
        router=router,
        exempt=exempt,
    ):
        # Every lane is still valid, so let the ordinary per-link cache path
        # hand each member back its own. A bundle is searched for as a whole or
        # not at all: re-deriving a lane while its neighbours came out of the
        # cache is how four parallel cables stop being parallel.
        return {}
    spine = router.waypoints(
        source, target, source_key=lead.source, target_key=lead.target, link=lead.id
    )
    if spine is None:
        return {}
    spread = router.lanes(spine, len(members), gap=FAN_GAP, exempt=exempt)
    return dict(zip(members, spread, strict=True))


def _all_reusable(
    members: Sequence[int],
    graph: Graph,
    *,
    cache: RouteCache,
    source: Anchor,
    target: Anchor,
    fans: Mapping[int, float],
    router: Router,
    exempt: frozenset[str],
) -> bool:
    """Is every lane of this bundle still exactly where the last render put it?"""
    return all(
        cache.reusable(
            (graph.geometry.view, graph.edges[index].id),
            source=source,
            target=target,
            pinned=(),
            style=Routing.ORTHOGONAL,
            fan=fans.get(index, 0.0),
            router=router,
            exempt=exempt,
        )
        is not None
        for index in members
    )


def _one(
    index: int,
    graph: Graph,
    *,
    anchors: Mapping[str, Anchor],
    geometry: Geometry,
    fans: Mapping[int, float],
    default: Routing,
    router: Router | None,
    cache: RouteCache | None,
    lane: tuple[tuple[float, float], ...] | None,
) -> tuple[Route | None, tuple[tuple[float, float], ...], bool]:
    """One link: its route, the waypoints routing added, and whether it was cached."""
    edge = graph.edges[index]
    source = anchors.get(edge.source)
    target = anchors.get(edge.target)
    if source is None or target is None:
        return None, (), False
    link = geometry.link(edge.id)
    style = link.routing or default
    fan = fans.get(index, 0.0)
    pinned = tuple(link.waypoints)
    exempt = frozenset({edge.source, edge.target})

    added: tuple[tuple[float, float], ...] = () if lane is None else lane
    if lane is None and router is not None and style is Routing.ORTHOGONAL:
        found = (
            None
            if cache is None
            else cache.reusable(
                (geometry.view, edge.id),
                source=source,
                target=target,
                pinned=pinned,
                style=style,
                fan=fan,
                router=router,
                exempt=exempt,
            )
        )
        if found is not None:
            # The whole answer, not just the search: every input to
            # ``route_link`` is what it was, so re-running it would rebuild a
            # value that is already here and immutable.
            cache.hits += 1  # type: ignore[union-attr]  # found implies a cache
            router.record(found.corners)
            return found.line, found.computed, True
        search = router.waypoints(
            source,
            target,
            source_key=edge.source,
            target_key=edge.target,
            pinned=pinned,
            link=edge.id,
        )
        added = () if search is None else search
        if cache is not None:
            cache.misses += 1

    drawn = route_link(
        source,
        target,
        waypoints=added or pinned,
        style=style,
        fan=fan,
    )
    if drawn.is_empty:
        return None, (), False
    if router is not None:
        router.record(drawn.corners)
    if cache is not None and style is Routing.ORTHOGONAL:
        cache.entries[(geometry.view, edge.id)] = _Cached(
            source=source,
            target=target,
            pinned=pinned,
            style=style,
            fan=fan,
            computed=added,
            corners=drawn.corners,
            line=drawn,
        )
    return drawn, added, False
