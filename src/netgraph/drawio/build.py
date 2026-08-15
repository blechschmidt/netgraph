"""Turning a resolved graph into an mxGraph model.

This is the export half of the round trip. It is a pure function of the graph,
the arrangement and the icon theme — nothing here reads a file except to inline
an icon, and nothing writes one.

Three decisions are worth reading before the code:

**Where the nodes go.** From the stored arrangement (§18) when there is one, so
the exported file opens *already arranged* rather than as a heap draw.io has to
lay out again. When there is none, a deterministic grid is invented — grouped by
namespace, in name order — and every invented position is marked
``netgraph:placed="auto"`` and recorded in the manifest. The mark matters: an
invented position that comes home unchanged must not be written into the tree as
though somebody had chosen it.

**What identifies a cell.** Not the label. See :mod:`netgraph.drawio.identity`.

**How a namespace becomes a frame.** As an mxGraph *container*, with the nodes
inside it as children and their coordinates relative to it, which is what makes
dragging a site in draw.io carry its switches. The frames nest the way the
namespaces do, and a frame's box comes from the arrangement when the arrangement
has one and from the bounding box of its members when it does not.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from netgraph.drawio.annotations import PlacedAnnotations, annotation_cells, place_annotations
from netgraph.drawio.identity import (
    ATTR_DOCUMENT,
    ATTR_HASH,
    ATTR_KIND,
    ATTR_LABEL,
    ATTR_LINK,
    ATTR_NAME,
    ATTR_NODE,
    ATTR_PLACED,
    ATTR_ROLE,
    ATTR_ROUTING,
    ATTR_SOURCE,
    ATTR_SOURCE_PORT,
    ATTR_TARGET,
    ATTR_TARGET_PORT,
    ATTR_WAYPOINTS,
    ATTR_X,
    ATTR_Y,
    MODEL_VERSION,
    CellRole,
    Placedness,
    Scope,
    content_hash,
    format_points,
)
from netgraph.drawio.model import MARGIN, ROOT_ID, Cell, Diagram, Frame, cell_id
from netgraph.drawio.notes import Level, Notes
from netgraph.drawio.styles import edge_style, group_style, icon_data_uri, node_style
from netgraph.layout.geometry import Box, Placement
from netgraph.loader.inventory import Inventory
from netgraph.plan.document import body_of
from netgraph.render.annotations import annotation_views
from netgraph.render.graph import Edge, Graph, Node
from netgraph.render.icons import IconTheme
from netgraph.render.options import RenderOptions
from netgraph.render.styles import StyleMap
from netgraph.render.theme import Theme

__all__ = [
    "AUTO_COLUMNS",
    "AUTO_COLUMN_WIDTH",
    "AUTO_ROW_HEIGHT",
    "BOX_HEIGHT",
    "BOX_WIDTH",
    "GROUP_PADDING",
    "ICON_SIZE",
    "BuildOptions",
    "build_diagram",
    "cell_id",
    "element_of",
]

#: The box a node without a stored size is drawn in, when it is drawn as a
#: coloured rectangle rather than as an icon. Wide enough for a qualified name
#: at draw.io's default font, which keeps the diagram legible unedited.
BOX_WIDTH: Final = 120.0
BOX_HEIGHT: Final = 60.0

#: The square an icon node occupies. Icons keep their aspect ratio inside it and
#: the label sits below, which is the convention every network diagram uses and
#: the one a draw.io user will not think to change.
ICON_SIZE: Final = 78.0

#: How far a namespace frame is inset from the nodes it holds, in points.
GROUP_PADDING: Final = 28.0

#: The grid an unarranged diagram is laid out on. Not a layout engine: a
#: readable default that is the same on every machine, so a diagram exported
#: from an inventory nobody has arranged is still a diagram and is still
#: byte-stable. ``netgraph layout --write`` is the way to get a real one.
AUTO_COLUMNS: Final = 5
AUTO_COLUMN_WIDTH: Final = 200.0
AUTO_ROW_HEIGHT: Final = 150.0

#: Extra vertical gap between two namespaces on the invented grid, so the
#: frames drawn round them do not touch.
_AUTO_NAMESPACE_GAP: Final = 80.0

#: The :class:`~netgraph.export.manifest.Reason` tokens this builder's notes
#: map onto. Spelled as literals rather than imported: the export package
#: imports *this* one, and a wire format has no business importing the artefact
#: registry that happens to use it. ``tests/test_drawio.py`` asserts each of
#: these is a real ``Reason``, so the two cannot drift apart in silence.
NOT_ARRANGED: Final = "not-arranged"
NOT_REPRESENTABLE: Final = "not-representable"
HALF_SELECTED: Final = "half-selected"


@dataclass(frozen=True, slots=True)
class BuildOptions:
    """What the caller settles about one export."""

    #: The view being drawn, as :class:`~netgraph.render.graph.Layer` spells it.
    view: str
    #: The icon theme to inline, or ``None`` to draw coloured boxes.
    icons: IconTheme | None = None
    #: Does the diagram hold every element of the view? A filtered export is
    #: :attr:`~netgraph.drawio.identity.Scope.PARTIAL`, and importing one never
    #: deletes anything: absence proves nothing about a diagram that was
    #: narrowed before it was drawn.
    scope: Scope = Scope.COMPLETE
    #: Draw a frame per namespace. Off gives a flat canvas, which is easier to
    #: rearrange wholesale and says less about where the documents live.
    groups: bool = True
    #: Draw the notes, areas and legends of §21. On by default, and off only for
    #: a caller that wants the topology alone — the annotations are part of the
    #: picture the inventory describes, not decoration added to it.
    annotations: bool = True
    #: The stylesheet in force (§22). mxGraph's style vocabulary lines up with
    #: netgraph's almost one for one, so a colour chosen in a manifest reaches
    #: draw.io as the same hex the SVG carries and survives the round-trip.
    theme: Theme | None = None
    #: Walk the style ladder at all; ``False`` is ``--no-style``.
    styling: bool = True


def build_diagram(
    graph: Graph,
    inventory: Inventory,
    options: BuildOptions,
    *,
    notes: Notes | None = None,
) -> Diagram:
    """The mxGraph model for one view of ``graph``.

    Args:
        graph: The filtered graph, carrying the arrangement for its layer.
        inventory: The tree it came from, for document paths and content hashes.
        options: The view, the icons and how much of the inventory this is.
        notes: Where omissions are recorded. Optional so the builder stays
            usable from a test that does not care about the manifest.

    Returns:
        A :class:`~netgraph.drawio.model.Diagram` whose cells are in a fixed
        order — metadata, areas, frames outermost first, nodes, links, then the
        notes and legends — so two exports of an unchanged inventory are
        byte-identical, and so the z-order draws each layer where it belongs.
    """
    record = notes if notes is not None else Notes()
    # One resolution for the whole export, exactly as the Graphviz backend
    # does it (§22), so a node cannot be one colour here and another there.
    # ``svg`` because a ``.drawio`` file is opened in a vector editor, which
    # is the case a raster icon loses.
    styles = StyleMap.build(
        graph,
        theme=options.theme,
        icons=options.icons,
        output="svg",
        styling=options.styling,
    )
    nodes = _ordered_nodes(graph)
    placements = _placements(graph, nodes, record)
    boxes = _group_boxes(graph, nodes, placements) if options.groups else {}
    placed = _annotations(graph, placements, boxes, options, record)
    frame = _frame(placements, boxes, placed)

    frames = _frame_cells(boxes, frame)
    groups = _reparented(frames)
    parents = {namespace: cell.id for namespace, cell in frames.items()}
    origins = {cell.id: (cell.x or 0.0, cell.y or 0.0) for cell in frames.values()}

    node_cells = tuple(
        _node_cell(
            node,
            placements[node.fqn],
            graph,
            inventory,
            options,
            frame,
            parents,
            origins,
            record,
            styles,
        )
        for node in nodes
    )
    ids = {node.fqn: cell.id for node, cell in zip(nodes, node_cells, strict=True)}
    link_cells = tuple(_link_cells(graph, inventory, frame, ids, record, styles))
    annotations = annotation_cells(placed, frame, inventory, ids)

    diagram = Diagram(
        view=options.view,
        name=options.view or "netgraph",
        frame=frame,
        scope=options.scope,
        version=MODEL_VERSION,
        annotated=options.annotations,
    )
    return diagram.with_cells(
        (
            diagram.metadata_cell(),
            *annotations.background,
            *groups,
            *node_cells,
            *link_cells,
            *annotations.foreground,
        )
    )


def _annotations(
    graph: Graph,
    placements: Mapping[str, _Placed],
    boxes: Mapping[str, Box],
    options: BuildOptions,
    record: Notes,
) -> PlacedAnnotations:
    """Every annotation of this view, boxed in netgraph coordinates (§21).

    Before the page frame, because the frame is computed from the answer: an
    area drawn round the outermost devices sticks out past them, and a legend
    sits outside the drawing entirely. Computing the frame first would put both
    off the page.
    """
    if not options.annotations:
        return PlacedAnnotations()
    positions = {
        fqn: Box(x=placed.x, y=placed.y, width=placed.width, height=placed.height)
        for fqn, placed in placements.items()
    }
    placed = place_annotations(
        annotation_views(graph, RenderOptions()),
        positions,
        drawing=_bounding_box([*(box.bounds for box in positions.values()), *_bounds(boxes)]),
    )
    _record_undrawn(graph, placed, record)
    return placed


def _record_undrawn(graph: Graph, placed: PlacedAnnotations, record: Notes) -> None:
    """Say so when an annotation this view declares is not in the file.

    An area whose members a filter removed, and a legend that turned out to have
    nothing to put in it, are both dropped rather than drawn as an empty box —
    which is right, and would be invisible. The manifest is where "it is
    declared and it is not in your diagram" gets said out loud.
    """
    drawn = placed.names()
    for fqn, annotation in graph.annotations:
        if fqn in drawn:
            continue
        record.add(
            Level.INFO,
            f"{annotation.kind}/{fqn}",
            "this drawing holds nothing for the annotation to be about, so it was left out; "
            "it is still declared, and a diagram of a view that draws its members will have it",
            reason=NOT_REPRESENTABLE,
        )


def _bounds(boxes: Mapping[str, Box]) -> Iterator[tuple[float, float, float, float]]:
    for box in boxes.values():
        yield box.bounds


def _bounding_box(bounds: Sequence[tuple[float, float, float, float]]) -> Box | None:
    if not bounds:
        return None
    return Box.from_bounds(
        min(entry[0] for entry in bounds),
        min(entry[1] for entry in bounds),
        max(entry[2] for entry in bounds),
        max(entry[3] for entry in bounds),
    )


# --------------------------------------------------------------------------- #
# Placing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Placed:
    """One node's box in netgraph coordinates, and where the box came from."""

    x: float
    y: float
    width: float
    height: float
    origin: Placedness

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(left, bottom, right, top)``."""
        return (
            self.x - self.width / 2,
            self.y - self.height / 2,
            self.x + self.width / 2,
            self.y + self.height / 2,
        )


def _placements(graph: Graph, nodes: Sequence[Node], record: Notes) -> Mapping[str, _Placed]:
    """Where every node of ``graph`` is drawn, stored or invented."""
    stored = graph.geometry.nodes
    invented = _grid([node for node in nodes if node.fqn not in stored])
    placed: dict[str, _Placed] = {}
    for node in nodes:
        width, height = _size_of(node)
        entry = stored.get(node.fqn)
        placed[node.fqn] = (
            _from_placement(entry, width, height)
            if entry is not None
            else _Placed(*invented[node.fqn], width=width, height=height, origin=Placedness.AUTO)
        )
    if invented:
        record.add(
            Level.INFO,
            f"view:{graph.layer}",
            f"{len(invented)} node(s) had no stored position and were laid out on a grid for "
            "the export; run 'netgraph layout --write' to make the arrangement part of the "
            "inventory, or arrange them in draw.io and import the result",
            reason=NOT_ARRANGED,
        )
    return placed


def _from_placement(entry: Placement, width: float, height: float) -> _Placed:
    return _Placed(
        x=entry.x,
        y=entry.y,
        width=entry.width if entry.width is not None else width,
        height=entry.height if entry.height is not None else height,
        origin=Placedness.STORED,
    )


def _size_of(node: Node) -> tuple[float, float]:
    """The box a node is drawn in when the arrangement does not say.

    A rack elevation and a collapsed namespace are *tables*, not devices: they
    hold text and are wider than they are tall, so they get the box rather than
    the icon square whether or not a theme has a picture for them.
    """
    if node.is_rack or node.is_aggregate:
        return (BOX_WIDTH, BOX_HEIGHT)
    return (ICON_SIZE, ICON_SIZE)


def _grid(pending: Sequence[Node]) -> Mapping[str, tuple[float, float]]:
    """A deterministic arrangement for the nodes nothing has placed.

    Namespaces stack downwards in name order and the nodes of each fill rows of
    :data:`AUTO_COLUMNS`. It is not a good layout and does not pretend to be
    one; it is a *reproducible* one, which is what an export needs.
    """
    if not pending:
        return {}
    grid: dict[str, tuple[float, float]] = {}
    top = 0.0
    for namespace in sorted({node.namespace for node in pending}):
        members = [node for node in pending if node.namespace == namespace]
        for index, node in enumerate(members):
            column, row = index % AUTO_COLUMNS, index // AUTO_COLUMNS
            grid[node.fqn] = (column * AUTO_COLUMN_WIDTH, top - row * AUTO_ROW_HEIGHT)
        rows = (len(members) + AUTO_COLUMNS - 1) // AUTO_COLUMNS
        top -= rows * AUTO_ROW_HEIGHT + _AUTO_NAMESPACE_GAP
    return grid


def _ordered_nodes(graph: Graph) -> tuple[Node, ...]:
    """Every node, in the one order the export ever uses.

    By namespace and then by fully-qualified name, never by graph order: the
    graph's order follows the loader's directory walk, and an export that
    changed when a directory was renamed would not be diffable.
    """
    return tuple(sorted(graph.nodes.values(), key=lambda node: (node.namespace, node.fqn)))


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #


def _group_boxes(
    graph: Graph, nodes: Sequence[Node], placements: Mapping[str, _Placed]
) -> Mapping[str, Box]:
    """The box of every namespace worth drawing a frame around.

    The root namespace gets none: a frame round the whole page says nothing.
    The stored box (§18) wins where there is one; otherwise the frame is the
    padded bounding box of everything inside it, computed innermost first so
    that a parent contains its children.
    """
    members = _members(nodes)
    if not members:
        return {}
    boxes: dict[str, Box] = {}
    for namespace in sorted(members, key=lambda name: (-name.count("/"), name)):
        stored = graph.geometry.groups.get(namespace)
        if stored is not None:
            boxes[namespace] = stored
            continue
        bounds = [placements[fqn].bounds for fqn in members[namespace]]
        bounds.extend(
            box.bounds for child, box in boxes.items() if child.startswith(f"{namespace}/")
        )
        boxes[namespace] = _padded(bounds)
    return boxes


def _members(nodes: Sequence[Node]) -> Mapping[str, tuple[str, ...]]:
    """Namespace to the fully-qualified names of the nodes directly inside it.

    A namespace that holds only sub-namespaces still gets an entry, with no
    direct members, so a frame is drawn round the site whose devices all live
    one folder further down.
    """
    found: dict[str, list[str]] = {}
    for node in nodes:
        if not node.namespace:
            continue
        found.setdefault(node.namespace, []).append(node.fqn)
        for ancestor in _ancestors(node.namespace):
            found.setdefault(ancestor, [])
    return {namespace: tuple(names) for namespace, names in sorted(found.items())}


def _ancestors(namespace: str) -> Iterator[str]:
    parts = namespace.split("/")
    for cut in range(1, len(parts)):
        yield "/".join(parts[:cut])


def _padded(bounds: Sequence[tuple[float, float, float, float]]) -> Box:
    left = min(entry[0] for entry in bounds) - GROUP_PADDING
    bottom = min(entry[1] for entry in bounds) - GROUP_PADDING
    right = max(entry[2] for entry in bounds) + GROUP_PADDING
    # Extra headroom at the top: a frame's label is drawn inside it.
    top = max(entry[3] for entry in bounds) + GROUP_PADDING * 1.5
    return Box.from_bounds(left, bottom, right, top)


def _frame(
    placements: Mapping[str, _Placed], boxes: Mapping[str, Box], placed: PlacedAnnotations
) -> Frame:
    """The coordinate map that puts the whole drawing on a positive page.

    The annotations count towards it. A note pinned above the topmost switch and
    a legend outside the bottom-right corner are both part of the picture, and a
    frame computed without them would put each of them off the page — where
    draw.io draws it perfectly well and nobody ever looks.
    """
    bounds = [entry.bounds for entry in placements.values()]
    bounds.extend(box.bounds for box in boxes.values())
    bounds.extend(placed.bounds())
    if not bounds:
        return Frame()
    return Frame(
        origin_x=min(entry[0] for entry in bounds) - MARGIN,
        origin_y=max(entry[3] for entry in bounds) + MARGIN,
    )


def _frame_cells(boxes: Mapping[str, Box], frame: Frame) -> Mapping[str, Cell]:
    """One container cell per namespace, in **page** coordinates, by namespace."""
    cells: dict[str, Cell] = {}
    for namespace in sorted(boxes):
        box = boxes[namespace]
        x, y = frame.box_to_drawio(box.x, box.y, box.width, box.height)
        cells[namespace] = Cell(
            id=cell_id("g", namespace),
            role=CellRole.GROUP,
            label=namespace.rpartition("/")[2],
            style=group_style(),
            vertex=True,
            x=x,
            y=y,
            width=box.width,
            height=box.height,
            attributes={
                ATTR_ROLE: CellRole.GROUP.value,
                ATTR_KIND: "namespace",
                ATTR_NAME: namespace,
                ATTR_NODE: namespace,
            },
        )
    return cells


def _reparented(frames: Mapping[str, Cell]) -> tuple[Cell, ...]:
    """The frames, outermost first, each nested inside the frame that holds it.

    Depth order so a parent is always defined before its children: mxGraph
    resolves ids lazily and would cope either way, but a file a person may read
    should not have to be read backwards. Turning the page coordinates into
    parent-relative ones is the last thing that happens to them.
    """
    ordered = sorted(frames, key=lambda name: (name.count("/"), name))
    cells: list[Cell] = []
    for namespace in ordered:
        cell = frames[namespace]
        parent = _enclosing(namespace, frames)
        if parent is None:
            cells.append(cell)
            continue
        outer = frames[parent]
        cells.append(
            Cell(
                id=cell.id,
                role=cell.role,
                label=cell.label,
                style=cell.style,
                parent=outer.id,
                vertex=True,
                x=(cell.x or 0.0) - (outer.x or 0.0),
                y=(cell.y or 0.0) - (outer.y or 0.0),
                width=cell.width,
                height=cell.height,
                attributes=cell.attributes,
            )
        )
    return tuple(cells)


def _enclosing(namespace: str, frames: Mapping[str, Cell]) -> str | None:
    """The nearest ancestor namespace that also has a frame."""
    parts = namespace.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = "/".join(parts[:cut])
        if candidate in frames:
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Cells
# --------------------------------------------------------------------------- #


def _node_cell(
    node: Node,
    placed: _Placed,
    graph: Graph,
    inventory: Inventory,
    options: BuildOptions,
    frame: Frame,
    parents: Mapping[str, str],
    origins: Mapping[str, tuple[float, float]],
    record: Notes,
    styles: StyleMap,
) -> Cell:
    """One vertex, positioned relative to the frame it sits in."""
    style = styles.node(node.fqn)
    icon = (
        icon_data_uri(options.icons, style.icon)
        if options.icons is not None and style.icon is not None
        else None
    )
    if options.icons is not None and icon is None:
        wanted = style.icon or node.kind
        record.add(
            Level.INFO,
            node.fqn,
            f"the icon theme has no picture called {wanted!r}, so the node is drawn as a "
            "coloured box; the diagram is complete, only plainer",
            reason=NOT_REPRESENTABLE,
        )
    x, y = frame.box_to_drawio(placed.x, placed.y, placed.width, placed.height)
    parent = parents.get(node.namespace, ROOT_ID)
    offset_x, offset_y = origins.get(parent, (0.0, 0.0))

    return Cell(
        id=cell_id("n", node.fqn),
        role=CellRole.NODE,
        label=node.name,
        style=node_style(style, icon=icon),
        parent=parent,
        vertex=True,
        x=x - offset_x,
        y=y - offset_y,
        width=placed.width,
        height=placed.height,
        attributes=_node_attributes(node, placed, graph, inventory),
    )


def _node_attributes(
    node: Node, placed: _Placed, graph: Graph, inventory: Inventory
) -> Mapping[str, str]:
    attributes: dict[str, str] = {
        ATTR_ROLE: CellRole.NODE.value,
        ATTR_KIND: node.kind,
        ATTR_NODE: node.fqn,
        ATTR_PLACED: placed.origin.value,
        ATTR_X: _plain(placed.x),
        ATTR_Y: _plain(placed.y),
    }
    element = inventory.get(node.fqn)
    if element is not None:
        attributes[ATTR_NAME] = node.fqn
        attributes[ATTR_HASH] = content_hash(body_of(element))
        source = graph.source_of(node.fqn)
        if source is not None:
            attributes[ATTR_DOCUMENT] = source.relative
    return attributes


def _link_cells(
    graph: Graph,
    inventory: Inventory,
    frame: Frame,
    ids: Mapping[str, str],
    record: Notes,
    styles: StyleMap,
) -> Iterator[Cell]:
    """One edge cell per link whose two ends both reached the diagram."""
    # The cells are emitted in id order so two exports agree byte for byte,
    # while a resolved style is keyed by the link's position in the graph.
    order = {edge.id: index for index, edge in enumerate(graph.edges)}
    for edge in sorted(graph.edges, key=lambda entry: (entry.id, entry.source, entry.target)):
        source, target = ids.get(edge.source), ids.get(edge.target)
        if source is None or target is None:
            record.add(
                Level.WARNING,
                edge.id,
                "one end of the link is not in this diagram, so the edge is left out rather "
                "than drawn to nothing",
                reason=HALF_SELECTED,
            )
            continue
        link = graph.geometry.link(edge.id)
        routing = graph.geometry.routing_for(edge.id).value
        yield Cell(
            id=cell_id("e", edge.id),
            role=CellRole.LINK,
            label=_link_label(edge, inventory),
            style=edge_style(styles.edge(order[edge.id]), routing=routing),
            parent=ROOT_ID,
            edge=True,
            source=source,
            target=target,
            points=tuple(frame.to_drawio(x, y) for x, y in link.waypoints),
            attributes=_link_attributes(edge, inventory, graph, routing),
        )


def _link_label(edge: Edge, inventory: Inventory) -> str:
    """What the edge says on the canvas.

    The element's own name for a declared link, so that editing it is a
    *rename* and means on an edge what it means on a node. A derived edge — an
    attachment, a subnet membership, an adjacency — has no name to edit, so it
    carries the physical label if it has one and nothing if it does not.
    """
    fqn = element_of(edge.id)
    if inventory.get(fqn) is not None:
        return fqn.rpartition("/")[2]
    return edge.label or ""


def element_of(edge_id: str) -> str:
    """The fully-qualified name of the document behind an edge id, if any.

    A cable's edge id *is* its name; a derived edge's carries a ``#`` and names
    the element it was derived from, or nothing at all. Splitting here rather
    than at each call site is what keeps the export and the import agreeing on
    which edges have a document behind them.
    """
    return edge_id.partition("#")[0]


def _link_attributes(
    edge: Edge, inventory: Inventory, graph: Graph, routing: str
) -> Mapping[str, str]:
    attributes: dict[str, str] = {
        ATTR_ROLE: CellRole.LINK.value,
        ATTR_KIND: edge.kind.value,
        ATTR_LINK: edge.id,
        ATTR_SOURCE: edge.source,
        ATTR_TARGET: edge.target,
        ATTR_ROUTING: routing,
    }
    if edge.source_port:
        attributes[ATTR_SOURCE_PORT] = edge.source_port
    if edge.target_port:
        attributes[ATTR_TARGET_PORT] = edge.target_port
    if edge.label:
        attributes[ATTR_LABEL] = edge.label

    waypoints = graph.geometry.link(edge.id).waypoints
    if waypoints:
        attributes[ATTR_WAYPOINTS] = format_points(waypoints)

    fqn = element_of(edge.id)
    element = inventory.get(fqn)
    if element is not None:
        attributes[ATTR_NAME] = fqn
        attributes[ATTR_HASH] = content_hash(body_of(element))
        source = graph.source_of(fqn)
        if source is not None:
            attributes[ATTR_DOCUMENT] = source.relative
    return attributes


def _plain(value: float) -> str:
    """A coordinate as an attribute value; twelve significant digits, not six."""
    return str(int(value)) if value == int(value) else f"{value:.12g}"
