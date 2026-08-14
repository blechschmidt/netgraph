"""``netgraph layout``: seeding an arrangement, dropping it, and pruning it.

An arrangement has to come from somewhere. Nobody types four hundred
coordinates, and an editor that starts every diagram from an empty canvas is
useless. So the first arrangement is the *automatic* one: run the layout once,
read the coordinates back out of Graphviz and persist them. From that moment the
diagram is editable — the positions are in the tree, under review, in version
control, and dragging a switch changes a file.

Three operations, and one property that makes them worth having:

``--write``
    Lay the graph out with ``--engine`` and store the result. What is stored is
    what a subsequent render draws, exactly — see :func:`seed_geometry` for how
    that is guaranteed rather than hoped for.
``--clear``
    Drop the arrangement for a view. The diagram goes back to being laid out
    from scratch, which is sometimes the fastest way to fix one that has been
    dragged into a mess.
``--prune``
    Drop the geometry of things the inventory no longer has. Deleting a switch
    leaves its coordinates behind; they are harmless (a stored position for a
    node that is not drawn is not drawn either) but they are noise, and
    ``W138`` reports them until this clears them.

Every write goes through :mod:`netgraph.edit`, so the comments and the
formatting of a hand-edited layout file survive being re-seeded, and so an
editing session can batch a geometry change with the model change that
prompted it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from netgraph.edit.operations import Operation, SetGeometry
from netgraph.errors import RenderError
from netgraph.layout.document import geometry_sections, inline_entry
from netgraph.layout.geometry import (
    Box,
    Geometry,
    LayoutMode,
    LinkGeometry,
    Placement,
    round_coordinate,
)
from netgraph.layout.graphviz import Drawing, DrawingError, parse_drawing
from netgraph.layout.resolve import conflicts_in, resolve_key
from netgraph.loader.inventory import Inventory, namespace_of
from netgraph.models.layout import LAYOUT_VIEWS, ViewGeometry
from netgraph.render.dot import (
    LayoutPlan,
    cluster_keys,
    complete_layout,
    run_graphviz,
    to_dot,
)
from netgraph.render.graph import Graph
from netgraph.render.ids import element_ids
from netgraph.render.options import RenderOptions

__all__ = [
    "DEFAULT_LAYOUT_NAME",
    "LAYOUT_ENGINES",
    "SEED_PASSES",
    "LayoutReport",
    "LiveKeys",
    "ViewReport",
    "clear_operations",
    "inspect_layout",
    "live_keys",
    "prune_operations",
    "seed_geometry",
    "views_for",
    "write_operations",
]

#: The Graphviz engines an arrangement may be seeded with. ``dot`` is the
#: hierarchical layout netgraph renders with by default and the right answer for
#: most networks; the others are offered because "most" is not "all" — a ring is
#: a ``circo`` graph and a flat mesh is an ``fdp`` one.
LAYOUT_ENGINES: Final[tuple[str, ...]] = ("dot", "neato", "fdp", "sfdp", "circo", "twopi")

#: The name a layout document gets when the user does not choose one.
DEFAULT_LAYOUT_NAME: Final = "layout"

#: How many times a seed may be re-run to reach a fixed point. See
#: :func:`seed_geometry`; two is enough in practice and the third is only there
#: so that "did not converge" is a measured fact rather than an assumption.
SEED_PASSES: Final = 3


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


def seed_geometry(
    graph: Graph,
    options: RenderOptions,
    *,
    engine: str = "dot",
    replace_all: bool = False,
    with_waypoints: bool = False,
) -> Geometry:
    """Lay ``graph`` out and return the arrangement that reproduces the result.

    Two jobs, and which one is done depends on what is already stored:

    * nothing is, or ``replace_all`` was asked for — the whole view is laid out
      with ``engine``;
    * some of it is — the stored positions are kept and only the rest are
      placed, through the same pinned path a render takes
      (:func:`~netgraph.render.dot.complete_layout`). Adding a switch and
      re-seeding must not throw away an afternoon of arranging, so completing is
      the default and replacing is a flag.

    Either way the answer is a **fixed point**, not simply what the engine said.
    A no-op render translates the drawing so its bounding box starts at the
    origin, so coordinates taken straight from a ``dot`` run come back shifted
    the first time they are replayed — an arrangement that was stable from the
    *second* render onwards would be a poor thing to promise. So the candidate
    is re-run through the render path until it stops moving, which it does after
    one pass because the translation is idempotent, and what is stored satisfies
    ``render(seed(graph)) == seed(graph)``.

    Args:
        graph: The :class:`~netgraph.render.graph.Graph` to lay out, carrying
            whatever arrangement it already has.
        options: The :class:`~netgraph.render.options.RenderOptions` the diagram
            will be drawn with. They matter: labels decide node sizes, and node
            sizes decide the layout.
        engine: One of :data:`LAYOUT_ENGINES`.
        replace_all: Lay every node out afresh, discarding what is stored.
        with_waypoints: Is the caller going to *store* the routes this produces?
            It decides which node sizes are recorded: a route has to stop at the
            shape it runs into, and netgraph cannot measure a label, so the box
            of every node a stored route leaves from is recorded with it. Nodes
            no route touches are left sized by their labels, which is what keeps
            an arrangement valid when a device grows a port.

    Returns:
        The arrangement, rounded to the precision it is stored at: a position
        per node, a box per group, and the edge splines.

    Raises:
        RenderError: ``engine`` is unknown, Graphviz failed, or its output
            cannot be read.
    """
    if engine not in LAYOUT_ENGINES:
        supported = ", ".join(LAYOUT_ENGINES)
        raise RenderError(f"{engine!r} is not a layout engine; expected one of {supported}")

    view = graph.layer.value
    mode = graph.geometry.mode(graph.nodes)
    # A route somebody dragged into place is a decision, not a derived number,
    # so it is carried across every pass untouched rather than being replaced by
    # whatever the engine drew. ``--replace`` is the one thing that discards it,
    # which is what "lay every node out afresh" has to mean.
    routed = (
        {} if replace_all else {k: v for k, v in graph.geometry.edges.items() if not v.is_empty}
    )
    # Which nodes have to record how big they are: the ends of every link whose
    # route will be in the file when this is done.
    sized = _anchor_nodes(
        graph, {edge.id for edge in graph.edges} if with_waypoints else set(routed)
    )
    adopt = True
    if replace_all or mode is LayoutMode.AUTO:
        bare = replace(graph, geometry=Geometry(view=view))
        geometry = _geometry_of(
            _draw(bare, options, LayoutPlan(engine=engine)), bare, options, view=view, sized=sized
        )
        # The engine has just routed every link from scratch, which is as good
        # an answer as this will ever get: pin it now rather than re-reading the
        # settling pass's echo of it.
        routed, adopt = dict(geometry.edges), False
    elif mode is LayoutMode.PARTIAL:
        geometry = complete_layout(graph, options).geometry
    else:
        geometry = graph.geometry

    # Settle on the coordinates the *render* path produces, not the ones the
    # seeding engine produced, so that what is written is what will be drawn.
    #
    # A route is read back out of Graphviz **once**, on the first settling pass,
    # and is a pinned decision from then on. It has to be: the pass after that
    # one is drawing the route this one just produced, so re-reading it would be
    # reading our own output — and since a polyline of *n* bends goes to
    # Graphviz as 3n+1 control points and would come back as 3n-1 bends, the
    # answer would grow every time instead of settling.
    for _ in range(SEED_PASSES):
        candidate = replace(graph, geometry=geometry)
        plan = LayoutPlan(mode=LayoutMode.FIXED, geometry=geometry)
        drawn = _geometry_of(
            _draw(candidate, options, plan), candidate, options, view=view, sized=sized
        )
        shift = _translation(geometry.nodes, drawn.nodes)
        # The no-op engine draws no clusters, so a settling pass never reports a
        # group box; the boxes from the engine pass are carried across and moved
        # by exactly the translation the nodes moved by, which keeps a frame
        # around the nodes it was drawn around. A pinned route moves with them,
        # for the same reason: the bends are in the coordinate system the nodes
        # are in, and a pass that shifts one has to shift the other.
        edges = dict(drawn.edges) if adopt else {}
        edges.update({key: _shifted(link, shift) for key, link in routed.items()})
        settled = replace(replace_geometry_groups(drawn, geometry, shift), edges=edges)
        if settled.nodes == geometry.nodes:
            return settled
        routed, adopt = edges, False
        geometry = settled
    return geometry


def _shifted(link: LinkGeometry, shift: tuple[float, float]) -> LinkGeometry:
    """One link's geometry moved by ``shift``. A label position is normalised
    along the route and so moves with it for free."""
    dx, dy = shift
    if not dx and not dy:
        return link
    return replace(
        link,
        waypoints=tuple(
            (round_coordinate(x + dx), round_coordinate(y + dy)) for x, y in link.waypoints
        ),
    )


def replace_geometry_groups(
    drawn: Geometry, previous: Geometry, shift: tuple[float, float]
) -> Geometry:
    """``drawn`` carrying ``previous``'s group boxes, moved by ``shift``."""
    if not previous.groups:
        return drawn
    dx, dy = shift
    return replace(
        drawn,
        groups={
            key: Box(
                x=round_coordinate(box.x + dx),
                y=round_coordinate(box.y + dy),
                width=box.width,
                height=box.height,
            )
            for key, box in previous.groups.items()
        },
    )


def _translation(
    before: Mapping[str, Placement], after: Mapping[str, Placement]
) -> tuple[float, float]:
    """How far the whole drawing moved between two no-op passes.

    A no-op render only ever *translates* — it normalises the bounding box to
    the origin and places nothing — so one node that appears in both answers
    settles it. Taking the first in sorted order rather than any node keeps two
    runs identical.
    """
    for key in sorted(before):
        if key in after:
            return (after[key].x - before[key].x, after[key].y - before[key].y)
    return (0.0, 0.0)


def _draw(graph: Graph, options: RenderOptions, plan: LayoutPlan) -> Drawing:
    """One Graphviz run at ``-Tjson``, parsed.

    Rendered with ``element_ids`` forced on, whatever the caller asked for: an
    ``id`` attribute is how an edge is identified in the answer, and it is inert
    as far as the layout is concerned — Graphviz places a graph by its labels and
    its topology, not by what its objects are called. So the coordinates this
    produces are the coordinates the caller's own render will produce.
    """
    payload, _ = run_graphviz(
        to_dot(graph, replace(options, element_ids=True), target="svg"),
        format="json",
        plan=plan,
        subject="the diagram layout",
    )
    try:
        return parse_drawing(payload)
    except DrawingError as exc:
        raise RenderError(f"cannot read the layout Graphviz computed: {exc}") from exc


def _geometry_of(
    drawing: Drawing,
    graph: Graph,
    options: RenderOptions,
    *,
    view: str,
    sized: Collection[str] = (),
) -> Geometry:
    """One Graphviz drawing as a stored arrangement, rounded."""
    stored = graph.geometry
    anchors = frozenset(sized)
    # Sizes are read from Graphviz and then dropped for every node that does not
    # anchor a hand-routed link. Graphviz derives the same box from the same
    # label on every run, so storing one would normally buy nothing and cost
    # correctness: a device that grows an interface grows its label, and a
    # stored size would then be a stale number.
    #
    # A node a *route* leaves from is the exception. The renderer has to know
    # where the border is to stop the line at it, and it has no label metrics to
    # work that out — so for those nodes the size is recorded, and a re-seed
    # refreshes it. See :mod:`netgraph.layout.routing`.
    nodes = {
        fqn: _placement(placed, sized=fqn in anchors)
        for fqn in graph.nodes
        if (placed := drawing.nodes.get(fqn)) is not None
    }
    names = cluster_keys(graph, options)
    groups = {
        names[subgraph]: _rounded_box(box)
        for subgraph, box in drawing.clusters.items()
        if subgraph in names
    }
    # Edges are matched by the ``id`` the document gave them, never by position:
    # Graphviz walks edges per node rather than in declaration order, so an
    # index-for-index pairing attaches a spline to the wrong link — which draws
    # a line across the diagram and parses perfectly.
    #
    # What a *hand-routed* link says is not read back here at all: the bends, the
    # style and the label position are decisions, and :func:`seed_geometry` puts
    # them back over the top of this so that re-seeding an arrangement cannot
    # quietly replace one with whatever the engine drew.
    identity = element_ids(graph)
    edges: dict[str, LinkGeometry] = {}
    for index, edge in enumerate(graph.edges):
        drawn = identity.edge(index)
        spline = drawing.edges.get(drawn) if drawn is not None else None
        if spline:
            edges[edge.id] = LinkGeometry(waypoints=_interior(spline))
    return Geometry(view=view, nodes=nodes, edges=edges, groups=groups, routing=stored.routing)


def _placement(placed: Placement, *, sized: bool) -> Placement:
    return Placement(
        x=round_coordinate(placed.x),
        y=round_coordinate(placed.y),
        width=round_coordinate(placed.width) if sized and placed.width is not None else None,
        height=round_coordinate(placed.height) if sized and placed.height is not None else None,
    )


def _anchor_nodes(graph: Graph, links: Collection[str]) -> frozenset[str]:
    """The nodes ``links`` leave from, whose size a stored route needs."""
    return frozenset(
        node for edge in graph.edges if edge.id in links for node in (edge.source, edge.target)
    )


def _interior(spline: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """A Graphviz spline as the bends it goes through.

    Graphviz reports a route that already starts and ends on the two shapes'
    borders; a stored one says only where it *bends*, because the ends are the
    nodes and have to follow them when they are dragged. So the two boundary
    points come off, and the renderer puts equivalents back — see
    :func:`netgraph.layout.routing.route`.
    """
    return tuple((round_coordinate(x), round_coordinate(y)) for x, y in spline[1:-1])


def _rounded_box(box: Box) -> Box:
    return Box(
        x=round_coordinate(box.x),
        y=round_coordinate(box.y),
        width=max(round_coordinate(box.width), 0.01),
        height=max(round_coordinate(box.height), 0.01),
    )


# --------------------------------------------------------------------------- #
# Turning an arrangement into edit operations
# --------------------------------------------------------------------------- #


def write_operations(
    geometry: Geometry,
    *,
    layout: str = DEFAULT_LAYOUT_NAME,
    namespace: str = "",
    file: str | None = None,
    with_waypoints: bool = False,
    routing: str | None = None,
) -> tuple[Operation, ...]:
    """The operations that persist ``geometry`` as one view of a layout document.

    Seeded edge waypoints are left out unless asked for. A seeded spline is a
    handful of control points per link of numbers nobody will ever read, and the
    render recomputes an identical one from the node positions; a *hand-placed*
    bend is a different thing, and that is what the flag is for. A link that
    pins a routing style or a label position is written either way, because
    neither is derivable and dropping one would undo a decision.

    An arrangement of nothing produces no operation. A view whose drawing is
    empty — ``--layer rack`` on an inventory that records no racks — has nothing
    to store, and writing ``rack: {}`` into a layout document would be a line
    that says so at the top of every file.

    Args:
        routing: The view's default routing style to write, or ``None`` to
            leave whatever the document says alone.
    """
    if geometry.is_empty and routing is None:
        return ()
    sections = geometry_sections(geometry, with_waypoints=with_waypoints)
    return (
        SetGeometry(
            view=geometry.view,
            nodes=sections.get("nodes", {}),
            edges=sections.get("edges", {}) if with_waypoints or "edges" in sections else None,
            groups=sections.get("groups", {}),
            routing=routing,
            layout=layout,
            namespace=namespace,
            file=file,
        ),
    )


def clear_operations(views: Iterable[str], *, inventory: Inventory) -> tuple[Operation, ...]:
    """The operations that drop every stored arrangement for ``views``.

    One per (document, view) actually holding geometry, so clearing a view no
    layout declares writes nothing at all rather than creating an empty document
    to delete it from.
    """
    wanted = tuple(views)
    return tuple(
        SetGeometry(
            view=view,
            nodes=None,
            edges=None,
            groups=None,
            layout=_short(fqn),
            namespace=namespace_of(fqn),
        )
        for fqn, document in inventory.layouts.items()
        for view in wanted
        if document.view(view) is not None
    )


def prune_operations(
    inventory: Inventory,
    *,
    live: Mapping[str, LiveKeys],
) -> tuple[Operation, ...]:
    """The operations that drop geometry naming things the inventory no longer has.

    Args:
        inventory: The loaded tree.
        live: Per view, the node, edge and group keys the drawing actually has.

    Returns:
        One operation per (document, view) that would lose an entry, carrying
        the entries that survive. A view that loses nothing produces nothing, so
        ``--prune`` on a clean tree writes no files.
    """
    operations: list[Operation] = []
    for fqn, document in inventory.layouts.items():
        namespace = namespace_of(fqn)
        for view, geometry in document.spec.views.items():
            keys = live.get(view)
            if keys is None:
                continue
            kept = _kept(geometry, keys, inventory=inventory, namespace=namespace)
            if kept is None:
                continue
            operations.append(
                SetGeometry(
                    view=view,
                    nodes=kept["nodes"],
                    edges=kept["edges"],
                    groups=kept["groups"],
                    layout=_short(fqn),
                    namespace=namespace,
                )
            )
    return tuple(operations)


@dataclass(frozen=True, slots=True)
class LiveKeys:
    """What one view's drawing actually contains, for pruning and reporting."""

    nodes: frozenset[str] = frozenset()
    edges: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()

    def holds(self, section: str, key: str) -> bool:
        return key in getattr(self, section)


def _kept(
    geometry: ViewGeometry, keys: LiveKeys, *, inventory: Inventory, namespace: str
) -> dict[str, Any] | None:
    """The entries of one view that survive a prune, or ``None`` if all do."""
    dropped = 0
    kept: dict[str, Any] = {}
    for section in ("nodes", "edges", "groups"):
        entries = getattr(geometry, section)
        surviving: dict[str, Any] = {}
        for key, value in entries.items():
            resolved = (
                _qualify(key, namespace)
                if section == "groups"
                else resolve_key(key, inventory=inventory, namespace=namespace)
            )
            if keys.holds(section, resolved):
                surviving[key] = inline_entry(value.model_dump(exclude_none=True))
            else:
                dropped += 1
        kept[section] = surviving
    return None if dropped == 0 else kept


def _qualify(key: str, namespace: str) -> str:
    if not namespace or key.startswith(f"{namespace}/") or key == namespace:
        return key
    return f"{namespace}/{key}"


def _short(fqn: str) -> str:
    return fqn.rpartition("/")[2]


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ViewReport:
    """What one view's arrangement covers, and what it has left behind."""

    view: str
    mode: LayoutMode
    #: Nodes the drawing has, and how many of them are placed.
    nodes: int = 0
    placed: int = 0
    edges: int = 0
    routed: int = 0
    groups: int = 0
    boxed: int = 0
    #: Stored keys naming nothing the drawing has, sorted.
    stale: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "view": self.view,
            "mode": str(self.mode),
            "nodes": {"total": self.nodes, "placed": self.placed},
            "edges": {"total": self.edges, "routed": self.routed},
            "groups": {"total": self.groups, "boxed": self.boxed},
            "stale": list(self.stale),
        }


@dataclass(frozen=True, slots=True)
class LayoutReport:
    """What ``netgraph layout`` found, before it changed anything."""

    views: tuple[ViewReport, ...] = ()
    #: Layout documents in the tree, by fully-qualified name.
    documents: tuple[str, ...] = ()
    #: Geometry a second document declared for something already placed.
    conflicts: tuple[tuple[str, str, str, str], ...] = ()

    @property
    def stale(self) -> tuple[str, ...]:
        """Every stale key across every view, deduplicated and sorted."""
        return tuple(sorted({key for view in self.views for key in view.stale}))

    def to_dict(self) -> dict[str, object]:
        return {
            "documents": list(self.documents),
            "views": [view.to_dict() for view in self.views],
            "conflicts": [
                {"view": view, "section": section, "key": key, "layout": layout}
                for view, section, key, layout in self.conflicts
            ],
        }


def inspect_layout(
    inventory: Inventory, drawings: Sequence[tuple[str, LiveKeys, Geometry]]
) -> LayoutReport:
    """Summarise how much of each view is arranged.

    Args:
        inventory: The loaded tree, for the document list and the conflicts.
        drawings: Per view, the keys the drawing has and the geometry stored
            for it. Built by the caller because it is the caller that knows
            which filters and display options the diagram is drawn with, and an
            arrangement is only meaningful against a particular drawing.
    """
    views = tuple(
        ViewReport(
            view=view,
            mode=geometry.mode(keys.nodes),
            nodes=len(keys.nodes),
            placed=sum(1 for key in keys.nodes if key in geometry.nodes),
            edges=len(keys.edges),
            routed=sum(1 for key in keys.edges if key in geometry.edges),
            groups=len(keys.groups),
            boxed=sum(1 for key in keys.groups if key in geometry.groups),
            stale=_stale(keys, geometry),
        )
        for view, keys, geometry in drawings
    )
    return LayoutReport(
        views=views,
        documents=tuple(inventory.layouts),
        conflicts=tuple(conflicts_in(inventory, [view for view, _, _ in drawings])),
    )


def _stale(keys: LiveKeys, geometry: Geometry) -> tuple[str, ...]:
    """Stored keys the drawing has no node, edge or group for."""
    return tuple(
        sorted(
            {key for key in geometry.nodes if key not in keys.nodes}
            | {key for key in geometry.edges if key not in keys.edges}
            | {key for key in geometry.groups if key not in keys.groups}
        )
    )


def live_keys(graph: Graph, options: RenderOptions) -> LiveKeys:
    """What a drawing of ``graph`` contains, keyed the way geometry is."""
    return LiveKeys(
        nodes=frozenset(graph.nodes),
        edges=frozenset(edge.id for edge in graph.edges),
        groups=frozenset(cluster_keys(graph, options).values()),
    )


def views_for(names: Iterable[str]) -> tuple[str, ...]:
    """The requested views, in the canonical order, deduplicated."""
    wanted = set(names)
    return tuple(view for view in LAYOUT_VIEWS if view in wanted)
