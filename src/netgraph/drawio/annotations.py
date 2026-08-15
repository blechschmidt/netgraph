"""Notes, areas and legends as native mxGraph cells (§21).

The point of this module is that an annotation must arrive in draw.io as *what
it is*, not as a picture of it. A note is draw.io's own ``shape=note`` with an
HTML label, so a stakeholder retypes it in place; an area is a ``container=1``
rectangle behind the nodes, so dragging the DMZ carries the DMZ; a legend is a
frame holding a swatch and a caption per row, so a colour can be corrected with
a click. None of the three is an image, a group of paths or a text blob, and
that is the whole difference between a diagram somebody can edit and one they
can only look at.

Three decisions are worth reading before the code.

**Where each one goes.** An annotation is placed in netgraph's coordinates,
before the page frame exists, because the frame is computed *from* the result:
a note pinned above the drawing and a legend outside its corner both enlarge the
page, and a diagram that clips its own key at the margin is a bug a reader finds
by printing it. What decides the box:

* an **area** takes ``spec.geometry`` when it has one, and otherwise the hull of
  wherever its members were drawn, grown by ``spec.padding``
  (:func:`~netgraph.render.annotations.member_hull`) — so a zone follows the
  devices it names;
* a **note** takes ``spec.geometry`` when it has one, otherwise sits beside what
  it is anchored to, and otherwise stacks in a column beside the drawing, which
  is the only honest place for a callout whose anchor this view never drew;
* a **legend** sits just outside the corner it names, clear of everything else.

**Z-order is document order in mxGraph.** The areas come out of here as a
separate tuple from everything else, and :mod:`netgraph.drawio.build` writes
them before the frames and the nodes. A zone written after its members would
cover them, and a reader would report that netgraph exports blank boxes.

**Identity is the same identity.** Every cell carries the block
:mod:`netgraph.drawio.identity` defines, with ``kind`` holding the document kind
and ``name`` its fully-qualified name, so ``netgraph import drawio`` reconciles a
dragged note by the machinery that reconciles a dragged switch rather than by a
second mechanism that would drift from the first. The legend is the exception,
and deliberately: it is *generated* from what the drawing holds, so its cells
carry identity to be recognised and ignored, never to be written back.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from netgraph.drawio.identity import (
    ATTR_DOCUMENT,
    ATTR_HASH,
    ATTR_HEIGHT,
    ATTR_KIND,
    ATTR_LABEL,
    ATTR_NAME,
    ATTR_PLACED,
    ATTR_ROLE,
    ATTR_TEXT,
    ATTR_WIDTH,
    ATTR_X,
    ATTR_Y,
    CellRole,
    Placedness,
    content_hash,
)
from netgraph.drawio.markup import escape_html, markup_html
from netgraph.drawio.model import ROOT_ID, Cell, Frame
from netgraph.drawio.styles import (
    area_style,
    leader_style,
    legend_style,
    note_style,
    swatch_style,
    text_style,
)
from netgraph.layout.geometry import Box
from netgraph.loader.inventory import Inventory
from netgraph.models.annotation import AREA_KIND, LEGEND_KIND, NOTE_KIND
from netgraph.plan.document import body_of
from netgraph.render.annotations import (
    AnnotationViews,
    AreaView,
    LegendView,
    NoteView,
    member_hull,
)

__all__ = [
    "LEGEND_MARGIN",
    "NOTE_OFFSET",
    "NOTE_WIDTH",
    "AnnotationCells",
    "PlacedAnnotations",
    "annotation_cells",
    "cell_prefix",
    "place_annotations",
]

#: The box a note is drawn in when ``spec.geometry`` does not say. Wide enough
#: for a sentence at draw.io's default font without being wide enough to cover
#: the device it is about.
NOTE_WIDTH: Final = 200.0

#: Characters that fit on one line of that box, and the height of one line. Both
#: are estimates — nothing here can measure a font — and both err towards a note
#: that is slightly too tall, because text clipped by its own box is the failure
#: a reader cannot work around and whitespace is the one they never notice.
NOTE_CHARS: Final = 30
NOTE_LINE_HEIGHT: Final = 15.0

#: Space above and below the text inside a note, in points.
NOTE_PADDING: Final = 22.0

#: The shortest a note is drawn, whatever its text.
NOTE_MIN_HEIGHT: Final = 46.0

#: Where an unplaced note sits relative to what it is anchored to: up and to the
#: right, which is where a hand-drawn callout goes and which keeps it clear of
#: the label printed under a node. The same offset :mod:`netgraph.render.dot`
#: uses, so the two pictures agree.
NOTE_OFFSET: Final[tuple[float, float]] = (140.0, 70.0)

#: Extra height a labelled area gets above its members, in points: one line of
#: caption at the frame's font size, plus the space around it.
LABEL_HEADROOM: Final = 16.0

#: How far outside the drawing a legend, or a note with nowhere else to go, is
#: placed. Enough that a key never lands on a device.
LEGEND_MARGIN: Final = 60.0

#: One row of a key, and the block above it that the title occupies.
LEGEND_ROW_HEIGHT: Final = 20.0
LEGEND_TITLE_HEIGHT: Final = 22.0

#: Space inside a key's frame, the swatch column, and the gap after it.
LEGEND_PADDING: Final = 8.0
SWATCH_WIDTH: Final = 22.0
SWATCH_HEIGHT: Final = 12.0

#: How tall a swatch that stands for a *line* is drawn: a bar rather than a
#: block, which is what tells a line style from a fill at this size.
SWATCH_RULE_HEIGHT: Final = 4.0

#: What one character of a legend caption is worth, in points, and the bounds on
#: the width that estimate may produce.
LEGEND_CHAR_WIDTH: Final = 6.0
LEGEND_MIN_WIDTH: Final = 130.0
LEGEND_MAX_WIDTH: Final = 320.0

#: The id prefix each family of annotation cells takes. Distinct from the ``n``,
#: ``e`` and ``g`` of an element cell so that a raw XML diff says what it is
#: looking at, and so that no annotation can ever collide with an element.
_PREFIXES: Final[Mapping[str, str]] = {
    NOTE_KIND: "note",
    AREA_KIND: "area",
    LEGEND_KIND: "legend",
    "leader": "leader",
}


def cell_prefix(kind: str) -> str:
    """The cell-id prefix used for one annotation kind."""
    return _PREFIXES.get(kind, "annotation")


# --------------------------------------------------------------------------- #
# Placing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Boxed:
    """One annotation's rectangle in netgraph coordinates, and where it came from."""

    box: Box
    #: ``stored`` when the document gave the position, ``auto`` when this module
    #: invented one. An invented position that comes home unchanged must not be
    #: written into the tree, exactly as for a node.
    origin: Placedness = Placedness.AUTO


@dataclass(frozen=True, slots=True)
class PlacedAnnotations:
    """Every annotation of one drawing, boxed, before the page frame exists."""

    areas: tuple[tuple[AreaView, _Boxed], ...] = ()
    notes: tuple[tuple[NoteView, _Boxed], ...] = ()
    legends: tuple[tuple[LegendView, _Boxed], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.areas or self.notes or self.legends)

    @property
    def count(self) -> int:
        return len(self.areas) + len(self.notes) + len(self.legends)

    def names(self) -> frozenset[str]:
        """The fully-qualified name of every annotation that got a box."""
        return frozenset(
            (
                *(view.fqn for view, _boxed in self.areas),
                *(view.fqn for view, _boxed in self.notes),
                *(view.fqn for view, _boxed in self.legends),
            )
        )

    def bounds(self) -> tuple[tuple[float, float, float, float], ...]:
        """``(left, bottom, right, top)`` of every box, for the page frame."""
        return tuple(
            boxed.box.bounds
            for boxed in (
                *(entry for _view, entry in self.areas),
                *(entry for _view, entry in self.notes),
                *(entry for _view, entry in self.legends),
            )
        )


def place_annotations(
    views: AnnotationViews, positions: Mapping[str, Box], *, drawing: Box | None
) -> PlacedAnnotations:
    """Give every annotation of ``views`` a rectangle in netgraph coordinates.

    Args:
        views: The resolved annotations, from
            :func:`~netgraph.render.annotations.annotation_views`.
        positions: Where each node was drawn, by fully-qualified name. An area
            is the hull of these; a note anchored to one sits beside it.
        drawing: The box enclosing the nodes and the namespace frames, or
            ``None`` for an empty diagram. What a legend is positioned against.

    Returns:
        The annotations that could be given a box, in draw order. An area whose
        members were all filtered out is dropped upstream; one whose members are
        somehow unplaced is dropped here, for the same reason — a zone drawn at
        the origin is a zone drawn over whatever is at the origin.
    """
    areas = tuple(
        (area, boxed) for area in views.areas if (boxed := _area_box(area, positions)) is not None
    )
    notes = _note_boxes(views.notes, positions, drawing)
    content = _grown(drawing, [boxed.box for _view, boxed in (*areas, *notes)])
    legends = tuple(
        (legend, _Boxed(_legend_box(legend, content))) for legend in views.legends if legend.entries
    )
    return PlacedAnnotations(areas=areas, notes=notes, legends=legends)


def _area_box(area: AreaView, positions: Mapping[str, Box]) -> _Boxed | None:
    """An area's rectangle: the one it pins, or the hull of what it encloses."""
    if area.box is not None:
        return _Boxed(area.box, Placedness.STORED)
    hull = member_hull(area, _corners(area.members, positions))
    if hull is None:
        return None
    if not area.label:
        return _Boxed(hull, Placedness.AUTO)
    # Room for the caption, which mxGraph draws *inside* the container at the
    # top: a hull grown by the padding alone would print the label of the zone
    # across the topmost device in it. The same allowance a namespace frame
    # makes, and for the same reason.
    left, bottom, right, top = hull.bounds
    return _Boxed(Box.from_bounds(left, bottom, right, top + LABEL_HEADROOM), Placedness.AUTO)


def _corners(members: Sequence[str], positions: Mapping[str, Box]) -> Iterator[tuple[float, float]]:
    """Both corners of every member's box, so the hull encloses the shapes.

    A hull over centres alone would cut through the nodes at the edge of the
    zone, which reads as a box drawn slightly wrong rather than as a box drawn
    round something.
    """
    for member in members:
        box = positions.get(member)
        if box is None:
            continue
        left, bottom, right, top = box.bounds
        yield (left, bottom)
        yield (right, top)


def _note_boxes(
    notes: Sequence[NoteView], positions: Mapping[str, Box], drawing: Box | None
) -> tuple[tuple[NoteView, _Boxed], ...]:
    """Every note's rectangle, in the order they were declared.

    A note that pins neither a position nor a drawn anchor still has to go
    somewhere: it stacks in a column to the left of the drawing, one below the
    next, which keeps it out of the picture and keeps two of them apart. Leaving
    it out instead would lose the text, and the text is the whole point of a
    note — :class:`~netgraph.render.annotations.NoteView` never drops one.
    """
    boxed: list[tuple[NoteView, _Boxed]] = []
    floating = 0
    for note in notes:
        width = note.width if note.width is not None else NOTE_WIDTH
        height = note.height if note.height is not None else _note_height(note, width)
        if note.x is not None and note.y is not None:
            boxed.append(
                (
                    note,
                    _Boxed(Box(x=note.x, y=note.y, width=width, height=height), Placedness.STORED),
                )
            )
            continue
        anchor = positions.get(note.anchor)
        if anchor is not None:
            centre = (anchor.x + NOTE_OFFSET[0], anchor.y + NOTE_OFFSET[1])
        else:
            centre = _floating(drawing, floating, width, height)
            floating += 1
        boxed.append((note, _Boxed(Box(x=centre[0], y=centre[1], width=width, height=height))))
    return tuple(boxed)


def _note_height(note: NoteView, width: float) -> float:
    """How tall a note has to be for its text, estimated from the character count.

    Nothing here can measure a font, so the estimate counts the lines each block
    wraps to at :data:`NOTE_CHARS` characters and rounds up. It is generous by
    design: a note with room to spare looks untidy, and one whose last bullet is
    cut off looks wrong.
    """
    per_line = max(8.0, NOTE_CHARS * width / NOTE_WIDTH)
    lines = sum(max(1, -(-len(block.text) // int(per_line))) for block in note.lines)
    return max(NOTE_MIN_HEIGHT, NOTE_PADDING + NOTE_LINE_HEIGHT * max(1, lines))


def _floating(drawing: Box | None, index: int, width: float, height: float) -> tuple[float, float]:
    """Where the ``index``-th note with nowhere to go sits: a column on the left."""
    if drawing is None:
        return (0.0, -index * (height + LEGEND_PADDING))
    left, _bottom, _right, top = drawing.bounds
    return (
        left - LEGEND_MARGIN - width / 2,
        top - height / 2 - index * (height + LEGEND_PADDING),
    )


def _legend_box(legend: LegendView, drawing: Box | None) -> Box:
    """A key's rectangle: just outside the corner it names.

    Outside rather than inside, because a key laid over an arranged drawing
    covers whatever the arrangement put in that corner, and netgraph has no way
    to know what that was. The corner is still honoured — a ``bottom-right``
    legend is at the bottom right of the page — which is what the reader asked
    for and what stays true when the drawing is laid out again.
    """
    width, height = _legend_size(legend)
    if drawing is None:
        return Box(x=0.0, y=0.0, width=width, height=height)
    left, bottom, right, top = drawing.bounds
    x = (
        (left - LEGEND_MARGIN - width / 2)
        if legend.at_left
        else (right + LEGEND_MARGIN + width / 2)
    )
    y = (top - height / 2) if legend.at_top else (bottom + height / 2)
    return Box(x=x, y=y, width=width, height=height)


def _legend_size(legend: LegendView) -> tuple[float, float]:
    """How big a key is: one row per swatch, and as wide as its longest caption."""
    captions = [legend.title, *(entry.label for entry in legend.entries)]
    longest = max((len(caption) for caption in captions), default=0)
    width = min(
        LEGEND_MAX_WIDTH,
        max(
            LEGEND_MIN_WIDTH,
            LEGEND_PADDING * 2 + SWATCH_WIDTH + LEGEND_PADDING + longest * LEGEND_CHAR_WIDTH,
        ),
    )
    height = (
        LEGEND_PADDING * 2
        + (LEGEND_TITLE_HEIGHT if legend.title else 0.0)
        + LEGEND_ROW_HEIGHT * len(legend.entries)
    )
    return (width, height)


def _grown(drawing: Box | None, boxes: Sequence[Box]) -> Box | None:
    """``drawing`` widened to hold ``boxes`` as well."""
    bounds = [box.bounds for box in boxes]
    if drawing is not None:
        bounds.append(drawing.bounds)
    if not bounds:
        return None
    return Box.from_bounds(
        min(entry[0] for entry in bounds),
        min(entry[1] for entry in bounds),
        max(entry[2] for entry in bounds),
        max(entry[3] for entry in bounds),
    )


# --------------------------------------------------------------------------- #
# Cells
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AnnotationCells:
    """The cells of one drawing's annotations, split by where they go in the file.

    Two tuples rather than one, because mxGraph draws in document order and the
    two halves belong on opposite sides of the nodes: an area is *behind* what it
    encloses and a note is *in front of* what it points at.
    """

    #: Written before the frames and the nodes: the areas.
    background: tuple[Cell, ...] = ()
    #: Written after the links: the notes, their leaders, and the legends.
    foreground: tuple[Cell, ...] = ()

    def __iter__(self) -> Iterator[Cell]:
        yield from self.background
        yield from self.foreground

    @property
    def count(self) -> int:
        return len(self.background) + len(self.foreground)


def annotation_cells(
    placed: PlacedAnnotations,
    frame: Frame,
    inventory: Inventory,
    node_ids: Mapping[str, str],
) -> AnnotationCells:
    """The mxGraph cells for every placed annotation.

    Args:
        placed: The boxed annotations, from :func:`place_annotations`.
        frame: The map from netgraph's coordinates to the page's.
        inventory: Where the documents live, for the ``document`` and ``hash``
            attributes that make a stale diagram detectable.
        node_ids: Cell id per node, for pointing a leader at its anchor.

    Returns:
        The cells, split into what goes behind the diagram and what goes in
        front of it.
    """
    background = tuple(_area_cell(area, boxed, frame, inventory) for area, boxed in placed.areas)
    notes = tuple(_note_cell(note, boxed, frame, inventory) for note, boxed in placed.notes)
    leaders = tuple(_leader_cells(placed, node_ids))
    legends = tuple(_legend_cells(placed, frame, inventory))
    return AnnotationCells(background=background, foreground=(*notes, *leaders, *legends))


def _area_cell(area: AreaView, boxed: _Boxed, frame: Frame, inventory: Inventory) -> Cell:
    box = boxed.box
    x, y = frame.box_to_drawio(box.x, box.y, box.width, box.height)
    return Cell(
        id=_cell_id(AREA_KIND, area.fqn),
        role=CellRole.AREA,
        label=escape_html(area.label),
        style=area_style(fill=area.fill, stroke=area.stroke, border=area.border),
        parent=ROOT_ID,
        vertex=True,
        x=x,
        y=y,
        width=box.width,
        height=box.height,
        attributes={
            **_identity(AREA_KIND, area.fqn, inventory),
            ATTR_LABEL: area.label,
            **_geometry_attributes(boxed),
        },
    )


def _note_cell(note: NoteView, boxed: _Boxed, frame: Frame, inventory: Inventory) -> Cell:
    box = boxed.box
    x, y = frame.box_to_drawio(box.x, box.y, box.width, box.height)
    return Cell(
        id=_cell_id(NOTE_KIND, note.fqn),
        role=CellRole.NOTE,
        label=markup_html(note.lines),
        style=note_style(fill=note.fill, stroke=note.stroke),
        parent=ROOT_ID,
        vertex=True,
        x=x,
        y=y,
        width=box.width,
        height=box.height,
        attributes={
            **_identity(NOTE_KIND, note.fqn, inventory),
            # The source, beside the rendering of it. This is what makes "was
            # the note edited?" an exact question: the importer renders this
            # again and compares, rather than trying to diff two HTML strings.
            ATTR_TEXT: note.text,
            **_geometry_attributes(boxed),
        },
    )


def _leader_cells(placed: PlacedAnnotations, node_ids: Mapping[str, str]) -> Iterator[Cell]:
    """One dashed line per note that points at something this drawing drew."""
    for note, _boxed in placed.notes:
        target = node_ids.get(note.anchor) if note.leader else None
        if target is None:
            continue
        yield Cell(
            id=_cell_id("leader", note.fqn),
            role=CellRole.LEADER,
            style=leader_style(stroke=note.stroke),
            parent=ROOT_ID,
            edge=True,
            source=_cell_id(NOTE_KIND, note.fqn),
            target=target,
            attributes={
                ATTR_ROLE: CellRole.LEADER.value,
                ATTR_KIND: NOTE_KIND,
                ATTR_NAME: note.fqn,
            },
        )


def _legend_cells(placed: PlacedAnnotations, frame: Frame, inventory: Inventory) -> Iterator[Cell]:
    """One frame per key, and two cells per row inside it.

    The rows are children of the frame, so their coordinates are relative to it
    and dragging the key in draw.io moves the whole thing — which is the only
    edit to a legend that does not need to mean anything, since the position of
    a generated key is not something the inventory records.
    """
    for legend, boxed in placed.legends:
        box = boxed.box
        x, y = frame.box_to_drawio(box.x, box.y, box.width, box.height)
        identity = _identity(LEGEND_KIND, legend.fqn, inventory)
        container = _cell_id(LEGEND_KIND, legend.fqn)
        yield Cell(
            id=container,
            role=CellRole.LEGEND,
            label=escape_html(legend.title),
            style=legend_style(fill=legend.fill, stroke=legend.stroke),
            parent=ROOT_ID,
            vertex=True,
            x=x,
            y=y,
            width=box.width,
            height=box.height,
            attributes=identity,
        )
        top = LEGEND_PADDING + (LEGEND_TITLE_HEIGHT if legend.title else 0.0)
        for index, entry in enumerate(legend.entries):
            row = top + index * LEGEND_ROW_HEIGHT
            rule = entry.shape != "box" and entry.shape != "ellipse"
            height = SWATCH_RULE_HEIGHT if rule else SWATCH_HEIGHT
            yield Cell(
                id=f"{container}-s{index}",
                role=CellRole.LEGEND,
                style=swatch_style(entry.shape, color=entry.color),
                parent=container,
                vertex=True,
                x=LEGEND_PADDING,
                y=row + (LEGEND_ROW_HEIGHT - height) / 2,
                width=SWATCH_WIDTH,
                height=height,
                attributes=identity,
            )
            yield Cell(
                id=f"{container}-t{index}",
                role=CellRole.LEGEND,
                label=escape_html(entry.label),
                style=text_style(),
                parent=container,
                vertex=True,
                x=LEGEND_PADDING * 2 + SWATCH_WIDTH,
                y=row,
                width=box.width - (LEGEND_PADDING * 3 + SWATCH_WIDTH),
                height=LEGEND_ROW_HEIGHT,
                attributes=identity,
            )


def _identity(kind: str, fqn: str, inventory: Inventory) -> dict[str, str]:
    """The attribute block every annotation cell carries.

    The same block an element cell carries, so the importer reconciles both by
    one rule. ``document`` and ``hash`` are what make a diagram edited against a
    version of the inventory that has since moved on *reported* rather than
    silently reconciled against something else.
    """
    attributes = {
        ATTR_ROLE: kind,
        ATTR_KIND: kind,
        ATTR_NAME: fqn,
    }
    document = inventory.annotations_of(kind).get(fqn)
    if document is not None:
        attributes[ATTR_HASH] = content_hash(body_of(document))
    source = inventory.annotation_source(kind, fqn)
    if source is not None:
        attributes[ATTR_DOCUMENT] = source.relative
    return attributes


def _geometry_attributes(boxed: _Boxed) -> dict[str, str]:
    """Where the cell was when it left, so a drag is an exact question."""
    box = boxed.box
    return {
        ATTR_PLACED: boxed.origin.value,
        ATTR_X: _plain(box.x),
        ATTR_Y: _plain(box.y),
        ATTR_WIDTH: _plain(box.width),
        ATTR_HEIGHT: _plain(box.height),
    }


def _cell_id(kind: str, fqn: str) -> str:
    # Imported here rather than at module scope: ``build`` imports this module
    # for the cells, and importing it back at module scope would be a cycle.
    from netgraph.drawio.build import cell_id

    return cell_id(cell_prefix(kind), fqn)


def _plain(value: float) -> str:
    """A coordinate as an attribute value; twelve significant digits, not six."""
    return str(int(value)) if value == int(value) else f"{value:.12g}"
