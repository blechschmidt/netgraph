"""The line every link in a drawing is drawn as, decided once.

:mod:`netgraph.layout.routing` knows how to turn two anchors and a list of
bends into a line. This module is the half of that job that needs a *graph*:
which anchor belongs to which link, which style each one asks for, and how far
apart two cables between the same pair of devices have to be fanned before a
reader can tell them apart.

It lives here rather than in the renderer because three things need the same
answer and must not each work it out:

* :mod:`netgraph.render.dot` writes it into a Graphviz ``pos``;
* :mod:`netgraph.render.jsonexport` publishes it for a client drawing the graph
  itself;
* :mod:`netgraph.web.preview` hands it to the editor canvas, which puts a grab
  handle on every bend.

A route is only computed for a drawing whose every node is placed. Anywhere
else Graphviz is doing the layout, the node positions are not known until it
has, and a line drawn against half an arrangement would be a line through
nothing — see :func:`~netgraph.render.dot.routing_advisories`, which is what
says so out loud.
"""

from __future__ import annotations

from collections.abc import Mapping

from netgraph.layout.geometry import Geometry, LayoutMode, Routing
from netgraph.layout.routing import FAN_GAP, Anchor, Route, fan_offsets
from netgraph.layout.routing import route as route_link
from netgraph.render.graph import Graph
from netgraph.render.options import RenderOptions

__all__ = ["anchors_of", "default_routing", "fans_of", "route_table"]


def default_routing(graph: Graph, options: RenderOptions | None = None) -> Routing:
    """The style links in this drawing take when they do not say for themselves.

    The caller's override first — that is where ``--routing`` arrives — then the
    view's, then the inventory's, then the curve Graphviz has always drawn. A
    link with a style of its own is not consulted here; it beats all four, and
    :meth:`~netgraph.layout.geometry.Geometry.routing_for` is where that is
    settled.
    """
    routing = options.routing if options is not None else None
    return graph.geometry.routing_for("", default=routing)


def anchors_of(graph: Graph) -> dict[str, Anchor]:
    """The box each placed node is left from.

    A node whose stored placement records no size gets the default box and is
    marked unmeasured. The route is still drawn — a clip a few points early is
    far better than a link that vanishes — and the renderer reports it, because
    ``netgraph layout --write`` records the sizes and fixes it exactly.
    """
    return {fqn: Anchor.of(placement) for fqn, placement in graph.geometry.nodes.items()}


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
    grouped: dict[tuple[str, ...], list[int]] = {}
    for index, edge in enumerate(graph.edges):
        grouped.setdefault(tuple(sorted((edge.source, edge.target))), []).append(index)
    offsets: dict[int, float] = {}
    for pair, members in grouped.items():
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


def route_table(graph: Graph, options: RenderOptions | None = None) -> tuple[Route | None, ...]:
    """One route per edge of ``graph``, in the graph's own order.

    Indexed rather than keyed by edge id, because that is how every consumer
    already walks the edges and an index cannot collide.

    ``None`` for a link this drawing cannot route: because the arrangement does
    not place both of its ends, or because the drawing is not fully placed at
    all and Graphviz is doing the routing.
    """
    geometry = graph.geometry
    if geometry.mode(graph.nodes) is not LayoutMode.FIXED:
        return (None,) * len(graph.edges)
    anchors = anchors_of(graph)
    fans = fans_of(graph)
    default = default_routing(graph, options)
    return tuple(
        _route(edge_index, graph, anchors=anchors, geometry=geometry, fans=fans, default=default)
        for edge_index in range(len(graph.edges))
    )


def _route(
    index: int,
    graph: Graph,
    *,
    anchors: Mapping[str, Anchor],
    geometry: Geometry,
    fans: Mapping[int, float],
    default: Routing,
) -> Route | None:
    edge = graph.edges[index]
    source = anchors.get(edge.source)
    target = anchors.get(edge.target)
    if source is None or target is None:
        return None
    link = geometry.link(edge.id)
    drawn = route_link(
        source,
        target,
        waypoints=link.waypoints,
        style=link.routing or default,
        fan=fans.get(index, 0.0),
    )
    return None if drawn.is_empty else drawn
