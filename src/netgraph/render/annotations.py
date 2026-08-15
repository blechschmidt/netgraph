"""Diagram annotations, resolved once into what a renderer actually draws (§21).

A ``note``, an ``area`` and a ``legend`` are *sidecars*: they declare no network
fact and may never change what the tool concludes
(:mod:`netgraph.models.annotation`). What they do change is the picture, and
five backends have to change it the same way — DOT, Mermaid, JSON, HTML and the
mxGraph export all have to agree about which elements a box encloses, what
colour it is and what a legend says. So the resolution happens here, once, and
each backend renders the answer in its own vocabulary.

What this module decides
------------------------

**Which members survive.** :func:`~netgraph.annotations.area_members` resolves an
area's ``members`` and ``selector`` against the *inventory*, which is the only
place that can — a reference may be relative and a selector is a query. The
graph carries that answer (:attr:`~netgraph.render.graph.Graph.annotation_targets`),
and this module narrows it to what the drawing actually holds. An element that a
filter removed, or that this layer never drew, is dropped **silently**: a
``--vlan 20`` diagram must not gain a dashed box round nothing, and an area left
with no members and no explicit rectangle is dropped entirely rather than drawn
as an empty frame. Likewise a note whose anchor is gone keeps its text and loses
its leader line, which is exactly what :func:`~netgraph.annotations.note_anchor`
promises by answering ``None`` for both "anchored to nothing" and "anchored to
something that is not there".

**What a generated legend says.** ``auto: layers`` builds its entries from what
*this* drawing contains — one swatch per node kind present, then one per reason a
link is drawn — with the colours read out of :mod:`netgraph.render.palette`,
which is the same table the renderer draws with. That is the whole point of the
generated form: a hand-written key goes stale the first time somebody adds a
fibre run, and one derived from the finished graph cannot.

**How the text is parsed.** :func:`parse_markup` implements the markdown subset
§21.1 documents — blank-line-separated paragraphs, ``- `` bullets, and inline
``**bold**``, ``*italic*`` and ``` `code` ``` — and nothing else. It is
deliberately tiny and **total**: it never raises, never backtracks and never
loops, because a note is drawn while a diagram is being rendered and a stale or
malformed one must degrade to literal text rather than to a traceback. Every
backend renders the same parse, so a note reads the same in an SVG, in a Mermaid
block and in the editor.

Colour
------

``spec.color`` is a fill, and every annotation needs an outline as well. Rather
than a second field nobody would set, the outline is derived from the fill by
:func:`darken`, so a custom colour brings its own matching stroke and the
defaults below are the only palette this module owns.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from netgraph.layout.geometry import Box
from netgraph.models.annotation import Area, AreaSpec, Legend, Note, NoteSpec
from netgraph.render.graph import Graph
from netgraph.render.ids import slug
from netgraph.render.options import RenderOptions
from netgraph.render.palette import (
    DEFAULT_EDGE_PALETTE,
    DEFAULT_NODE_PALETTE,
    EDGE_PALETTE,
    NODE_PALETTE,
    edge_palette_key,
)

__all__ = [
    "AREA_ID_PREFIX",
    "DEFAULT_AREA_FILL",
    "DEFAULT_LEGEND_FILL",
    "DEFAULT_NOTE_FILL",
    "LEGEND_ID_PREFIX",
    "NOTE_ID_PREFIX",
    "AnnotationViews",
    "AreaView",
    "Block",
    "LegendSwatch",
    "LegendView",
    "NoteView",
    "Span",
    "annotation_views",
    "darken",
    "member_hull",
    "parse_markup",
]

#: What each family of annotation ids is prefixed with. The same reasoning as
#: :mod:`netgraph.render.ids`: the prefix guarantees the id starts with a letter,
#: and keeps these out of the space of the node, edge and cluster ids — a
#: consumer holding one id should not have to know which sort of thing it names.
NOTE_ID_PREFIX: Final = "note-"
AREA_ID_PREFIX: Final = "area-"
LEGEND_ID_PREFIX: Final = "legend-"

#: What each kind is filled with when ``spec.color`` says nothing. Amber for a
#: note, because that is the colour of every sticky note anybody has ever put on
#: a drawing; a near-neutral slate for an area, because a zone sits *behind* the
#: diagram and must not compete with a node's own fill; white for a legend, which
#: is a piece of paper laid on the picture rather than part of it.
DEFAULT_NOTE_FILL: Final = "#fef3c7"
DEFAULT_AREA_FILL: Final = "#f1f5f9"
DEFAULT_LEGEND_FILL: Final = "#ffffff"

#: How much darker an outline is than the fill it is derived from. Enough that a
#: pale zone still has a visible edge, not so much that a saturated fill gets a
#: black border.
_STROKE_FACTOR: Final = 0.55

#: A colour any backend may interpolate. The model already refuses anything else
#: (``NG-G003``), but the renderers write colours into DOT attributes, HTML-like
#: label attributes, Mermaid ``classDef`` rules and mxGraph styles, and a value
#: that reaches four parsers should be re-checked at the boundary rather than
#: trusted because something upstream promised.
_COLOUR_RE: Final = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")

#: How ``auto: layers`` names a link whose palette key is not readable as it
#: stands. Everything else is named by its key — ``copper``, ``fiber``, ``bgp``.
_LINK_LABELS: Final[dict[str, str]] = {
    "cleartext-tunnel": "tunnel (cleartext)",
    "attachment": "adapter attachment",
    "subnet": "subnet membership",
    "membership": "group membership",
    "poe": "power over ethernet",
    "outlet": "outlet feed",
}

#: The swatch shape that stands for each Graphviz line style. A legend has no
#: room for a line weight, so ``bold`` — which is what fibre is drawn with —
#: reads as a plain line and is told apart by its colour, exactly as it is in the
#: drawing when printed small.
_SWATCH_SHAPES: Final[dict[str, str]] = {
    "solid": "line",
    "bold": "line",
    "dashed": "dashed",
    "dotted": "dotted",
}


# --------------------------------------------------------------------------- #
# The markdown subset
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Span:
    """A run of text in one style.

    ``style`` is ``""``, ``bold``, ``italic`` or ``code`` — the four §21.1
    allows. A backend that cannot express one of them prints the text, which is
    the honest degradation: the words are the content and the emphasis is not.
    """

    style: str
    text: str


@dataclass(frozen=True, slots=True)
class Block:
    """One paragraph or one bullet, as the spans it is made of.

    ``kind`` is ``paragraph`` or ``bullet``. Bullets are separate blocks rather
    than a nested list because the subset has no nesting: a renderer draws one
    row per block and needs to know only whether to put a mark in front of it.
    """

    kind: str
    spans: tuple[Span, ...] = ()

    @property
    def text(self) -> str:
        """The block with every marker removed — what a plain-text backend prints."""
        return "".join(span.text for span in self.spans)


#: Inline emphasis, longest marker first so ``**bold**`` is never read as two
#: italics. Each alternative requires at least one character between its
#: markers, so ``**`` and ```` `` ```` are literal text; and each is non-greedy
#: and cannot span a block, so the scan is linear in the length of the note and
#: has no way to backtrack. An unterminated marker matches nothing and is
#: therefore drawn as itself, which is what a reader who typed one asterisk
#: meant.
_INLINE: Final = re.compile(r"\*\*(?P<bold>[^*]+)\*\*|\*(?P<italic>[^*]+)\*|`(?P<code>[^`]+)`")

#: What starts a bullet. A tab counts as indentation, and ``-`` on its own is a
#: paragraph rather than an empty bullet.
_BULLET: Final = re.compile(r"^[ \t]*[-*][ \t]+(?P<text>.*)$")


def parse_markup(text: str) -> tuple[Block, ...]:
    """The §21.1 markdown subset of ``text``, as blocks of styled spans.

    Total by construction: every branch appends, nothing raises, and the one
    regular expression is linear. Anything the subset does not recognise —
    a heading, a table, a link, a stray asterisk — comes out as literal text,
    which is the only failure mode several very different renderers can agree
    about.

    Soft line breaks inside a paragraph are joined with a space, the way
    markdown joins them, so a note wrapped at 72 characters in YAML does not
    draw as a column of short lines.
    """
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(Block(kind="paragraph", spans=_spans(" ".join(paragraph))))
            paragraph.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        bullet = _BULLET.match(line)
        if bullet is not None:
            flush()
            blocks.append(Block(kind="bullet", spans=_spans(bullet.group("text").strip())))
            continue
        paragraph.append(stripped)
    flush()
    return tuple(blocks)


def _spans(text: str) -> tuple[Span, ...]:
    """One line of text split into its styled runs, in order."""
    spans: list[Span] = []
    cursor = 0
    for match in _INLINE.finditer(text):
        if match.start() > cursor:
            spans.append(Span(style="", text=text[cursor : match.start()]))
        style = match.lastgroup or ""
        spans.append(Span(style=style, text=match.group(style) if style else match.group(0)))
        cursor = match.end()
    if cursor < len(text):
        spans.append(Span(style="", text=text[cursor:]))
    return tuple(spans)


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #


def darken(colour: str, factor: float = _STROKE_FACTOR) -> str:
    """``colour`` scaled towards black, as ``#rrggbb``.

    How an outline is derived from a fill, so that an annotation in a colour
    somebody chose is drawn with an edge that matches it rather than with a
    fixed grey that fights it. Total: anything that is not a hex colour comes
    back unchanged, since the caller has nothing better to fall back to.
    """
    if _COLOUR_RE.match(colour) is None:
        return colour
    digits = colour[1:]
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    channels = (int(digits[index : index + 2], 16) for index in (0, 2, 4))
    return "#" + "".join(f"{max(0, min(255, int(value * factor))):02x}" for value in channels)


def _fill(colour: str | None, default: str) -> str:
    """The fill an annotation is drawn with: what it asked for, or its kind's."""
    if colour is None or _COLOUR_RE.match(colour) is None:
        return default
    return colour.lower()


# --------------------------------------------------------------------------- #
# The views
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AreaView:
    """One zone, ready to draw: what it encloses, and how big it is."""

    #: Stable slug, e.g. ``area-dmz``. Unique across every annotation of one
    #: drawing.
    id: str
    #: Fully-qualified name of the ``kind: area`` document.
    fqn: str
    #: The caption; ``""`` for a deliberately unlabelled box.
    label: str
    #: The elements it encloses that this drawing actually holds, in the order
    #: the area named them. See the module docstring on why the rest are dropped.
    members: tuple[str, ...]
    fill: str
    stroke: str
    #: ``solid``, ``dashed``, ``dotted`` or ``none``.
    border: str
    #: Space between the hull of the members and the box, in points. Ignored when
    #: :attr:`box` gives the rectangle outright.
    padding: float
    #: The explicit rectangle from ``spec.geometry``, or ``None`` to compute one
    #: from where the members were drawn.
    box: Box | None = None

    @property
    def is_placed(self) -> bool:
        """Does this area say where it goes, rather than following its members?"""
        return self.box is not None


@dataclass(frozen=True, slots=True)
class NoteView:
    """One callout, ready to draw: its text, what it points at, and where it sits."""

    id: str
    #: Fully-qualified name of the ``kind: note`` document.
    fqn: str
    #: The markdown-subset source, exactly as the document wrote it. Kept beside
    #: the parse so an exporter that round-trips a note (mxGraph, the editor)
    #: writes back what the author typed rather than a re-rendering of it.
    text: str
    #: The same text parsed once; see :func:`parse_markup`.
    lines: tuple[Block, ...]
    #: The node it is about, or ``""`` when it is unanchored or the anchor is not
    #: in this drawing.
    anchor: str
    #: Draw a line from the note to :attr:`anchor`. False without an anchor.
    leader: bool
    fill: str
    stroke: str
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None

    @property
    def is_placed(self) -> bool:
        """Does the note pin its own position?"""
        return self.x is not None and self.y is not None

    @property
    def plain(self) -> str:
        """The note as plain text, one line per block — for a backend with no markup."""
        return "\n".join(
            f"• {block.text}" if block.kind == "bullet" else block.text for block in self.lines
        )


@dataclass(frozen=True, slots=True)
class LegendSwatch:
    """One row of a key: a mark, and what it means."""

    label: str
    color: str
    #: One of :data:`~netgraph.models.annotation.SWATCH_SHAPES`.
    shape: str = "box"
    description: str = ""


@dataclass(frozen=True, slots=True)
class LegendView:
    """One key, ready to draw. Generated entries are already generated."""

    id: str
    #: Fully-qualified name of the ``kind: legend`` document.
    fqn: str
    title: str
    #: ``top-left``, ``top-right``, ``bottom-left`` or ``bottom-right``.
    corner: str
    entries: tuple[LegendSwatch, ...]
    fill: str
    stroke: str

    @property
    def at_top(self) -> bool:
        return self.corner.startswith("top")

    @property
    def at_left(self) -> bool:
        return self.corner.endswith("left")


@dataclass(frozen=True, slots=True)
class AnnotationViews:
    """Every annotation of one drawing, in the order a renderer draws them.

    Areas first — they go behind everything — then the notes, then the legends,
    which is the same order :class:`~netgraph.annotations.AnnotationSet` iterates
    in and the same order the layers of the picture stack in.
    """

    areas: tuple[AreaView, ...] = ()
    notes: tuple[NoteView, ...] = ()
    legends: tuple[LegendView, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.areas or self.notes or self.legends)

    @property
    def count(self) -> int:
        return len(self.areas) + len(self.notes) + len(self.legends)

    @property
    def clustered(self) -> frozenset[str]:
        """Every node an area encloses — the nodes a backend must not box twice."""
        return frozenset(member for area in self.areas for member in area.members)

    def area_of(self, fqn: str) -> AreaView | None:
        """The area a node is drawn inside, first-declared winning.

        A node belongs to at most one container in every backend netgraph has —
        a Graphviz cluster, a Mermaid subgraph, an mxGraph parent — so two
        overlapping areas have to be resolved, and the rule is the one that does
        not depend on the graph: whichever area was declared first.
        """
        return next((area for area in self.areas if fqn in area.members), None)


def annotation_views(graph: Graph, options: RenderOptions | None = None) -> AnnotationViews:
    """Resolve ``graph``'s annotations into what a renderer draws.

    The single entry point every backend uses, so that a filtered diagram, a
    collapsed one and a diffed one all drop the same members and generate the
    same key.

    Returns:
        Empty when the graph carries no annotations, or when
        :attr:`RenderOptions.annotations <netgraph.render.options.RenderOptions.annotations>`
        is off — in which case a backend emits nothing at all, and its output is
        byte-identical to the same inventory with no annotations in it.
    """
    opts = options or RenderOptions()
    annotations = graph.annotations
    if not opts.annotations or not annotations:
        return AnnotationViews()

    taken: set[str] = set()
    areas = tuple(
        view
        for fqn, area in annotations.areas
        if (view := _area_view(graph, fqn, area, taken)) is not None
    )
    notes = tuple(_note_view(graph, fqn, note, taken) for fqn, note in annotations.notes)
    legends = tuple(_legend_view(graph, fqn, legend, taken) for fqn, legend in annotations.legends)
    return AnnotationViews(areas=areas, notes=notes, legends=legends)


def _area_view(graph: Graph, fqn: str, area: Area, taken: set[str]) -> AreaView | None:
    """One area, or ``None`` when this drawing gives it nothing to enclose."""
    spec: AreaSpec = area.spec
    members = _drawn(graph, graph.annotation_targets.get(fqn, ()))
    box = _box(spec)
    if not members and box is None:
        # Neither the elements it named nor a rectangle of its own: there is
        # nothing to draw a frame around, and an empty box on a filtered diagram
        # would claim a zone the reader can see is empty.
        return None
    fill = _fill(spec.color, DEFAULT_AREA_FILL)
    return AreaView(
        id=_id(AREA_ID_PREFIX, fqn, taken),
        fqn=fqn,
        label=spec.label or "",
        members=members,
        fill=fill,
        stroke=darken(fill),
        border=spec.border,
        padding=spec.padding,
        box=box,
    )


def _note_view(graph: Graph, fqn: str, note: Note, taken: set[str]) -> NoteView:
    """One note. Never dropped: the text is the point, and it survives its anchor."""
    spec: NoteSpec = note.spec
    targets = _drawn(graph, graph.annotation_targets.get(fqn, ()))
    anchor = targets[0] if targets else ""
    geometry = spec.geometry
    fill = _fill(spec.color, DEFAULT_NOTE_FILL)
    return NoteView(
        id=_id(NOTE_ID_PREFIX, fqn, taken),
        fqn=fqn,
        text=spec.text,
        lines=parse_markup(spec.text),
        anchor=anchor,
        # A leader with nothing at the far end is a line into empty space, so
        # the anchor decides this as much as the flag does.
        leader=bool(anchor) and spec.leader,
        fill=fill,
        stroke=darken(fill),
        x=geometry.x if geometry is not None else None,
        y=geometry.y if geometry is not None else None,
        width=geometry.width if geometry is not None else None,
        height=geometry.height if geometry is not None else None,
    )


def _legend_view(graph: Graph, fqn: str, legend: Legend, taken: set[str]) -> LegendView:
    """One key, with ``auto: layers`` already turned into swatches."""
    spec = legend.spec
    entries = (
        _generated_entries(graph)
        if spec.auto == "layers"
        else tuple(
            LegendSwatch(
                label=entry.label,
                color=_fill(entry.color, DEFAULT_LEGEND_FILL),
                shape=entry.shape,
                description=entry.description or "",
            )
            for entry in spec.entries
        )
    )
    fill = _fill(spec.color, DEFAULT_LEGEND_FILL)
    return LegendView(
        id=_id(LEGEND_ID_PREFIX, fqn, taken),
        fqn=fqn,
        title=spec.title or "",
        corner=spec.corner,
        entries=entries,
        fill=fill,
        stroke=darken(fill),
    )


def _generated_entries(graph: Graph) -> tuple[LegendSwatch, ...]:
    """The key ``auto: layers`` builds: what this drawing actually drew.

    Node kinds first, in the order the graph holds them, then the reasons a link
    is drawn, in the order the edges hold them. Both are read off the finished
    graph rather than off the inventory, so a key on a ``--vlan 10`` diagram
    describes the ten devices left rather than the four hundred that were not
    drawn — which is the only form of legend that cannot go stale.
    """
    return (
        *(_node_swatch(kind) for kind in _distinct(node.kind for node in graph.nodes.values())),
        *(_link_swatch(key) for key in _distinct(edge_palette_key(edge) for edge in graph.edges)),
    )


def _node_swatch(kind: str) -> LegendSwatch:
    fill, _stroke = NODE_PALETTE.get(kind, DEFAULT_NODE_PALETTE)
    return LegendSwatch(label=kind, color=fill, shape="box")


def _link_swatch(key: str) -> LegendSwatch:
    colour, line = EDGE_PALETTE.get(key, DEFAULT_EDGE_PALETTE)
    return LegendSwatch(
        label=_LINK_LABELS.get(key, key), color=colour, shape=_SWATCH_SHAPES.get(line, "line")
    )


def _distinct(values: Iterable[str]) -> tuple[str, ...]:
    """The distinct values, in first-seen order — never a set iteration.

    Determinism is a hard requirement of every renderer here: the same inventory
    must produce byte-identical output on every run.
    """
    return tuple(dict.fromkeys(value for value in values if value))


def _drawn(graph: Graph, targets: Sequence[str]) -> tuple[str, ...]:
    """``targets``, narrowed to the ones this drawing holds, in the given order."""
    return tuple(dict.fromkeys(target for target in targets if target in graph.nodes))


def _box(spec: AreaSpec) -> Box | None:
    """The explicit rectangle an area was given, or ``None`` to derive one."""
    geometry = spec.geometry
    if geometry is None or not geometry.placed or not geometry.sized:
        return None
    assert geometry.x is not None and geometry.y is not None  # narrowed by ``placed``
    assert geometry.width is not None and geometry.height is not None  # by ``sized``
    return Box(x=geometry.x, y=geometry.y, width=geometry.width, height=geometry.height)


def _id(prefix: str, fqn: str, taken: set[str]) -> str:
    """A stable, collision-free id for one annotation.

    Slugged by :func:`~netgraph.render.ids.slug`, so the alphabet and the
    truncation rule are the ones every other id in a rendering obeys, and
    disambiguated in declaration order so the suffix a collision earns depends
    only on the inventory.
    """
    candidate = f"{prefix}{slug(fqn)}"
    chosen = candidate
    counter = 2
    while chosen in taken:
        chosen = f"{candidate}-{counter}"
        counter += 1
    taken.add(chosen)
    return chosen


# --------------------------------------------------------------------------- #
# Placement helpers
# --------------------------------------------------------------------------- #


def member_hull(area: AreaView, positions: Iterable[tuple[float, float]]) -> Box | None:
    """The rectangle enclosing ``positions``, grown by the area's padding.

    Used by a backend drawing an area into a *fixed* arrangement, where the
    engine places nothing and netgraph has to compute the frame itself. ``None``
    when nothing was placed, which leaves the area undrawn rather than drawn at
    the origin.
    """
    points = list(positions)
    if not points:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    pad = area.padding
    return Box.from_bounds(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
