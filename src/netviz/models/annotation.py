"""The ``note``, ``area`` and ``legend`` document kinds: diagram annotations (§21).

A diagram is not only its devices. A callout saying *why* a link is orange, a
dashed box round the DMZ, a key explaining what the colours mean — every
diagramming tool has them, and until now netviz had no way to write one down.
Anything a user added to an exported drawing was lost the next time the picture
was rendered, which is a hole in the 1:1 contract between the visual and the
textual form.

These three kinds close it::

    apiVersion: netviz.dev/v1alpha1
    kind: note
    metadata:
      name: why-orange
    spec:
      text: |
        **Orange** links are fibre. The run to the annexe is 180 m,
        which is past what copper does.
      anchor:
        link: cables/cbl-annexe
      color: "#fef3c7"
    ---
    apiVersion: netviz.dev/v1alpha1
    kind: area
    metadata:
      name: dmz
    spec:
      label: DMZ
      members: [edge/fw-1, edge/srv-proxy]
      color: "#fee2e2"
    ---
    apiVersion: netviz.dev/v1alpha1
    kind: legend
    metadata:
      name: key
    spec:
      title: Key
      corner: bottom-right
      auto: layers

**None of the three is an element.** Like :mod:`~netviz.models.layout` they
are *sidecars*: they declare no network fact, own no interface, terminate no
cable, are never listed by ``netviz list`` and never appear in the graph. That
is not an implementation detail but the central promise of this section — an
annotation must never change what the tool *concludes*. Adding a note cannot
make ``netviz validate`` fail, cannot move a hop in ``netviz path``, cannot
appear in the configuration ``netviz export config`` generates, and cannot
show up as an infrastructure change in ``netviz plan``. ``tests/test_annotations.py``
asserts each of those separately, because a presentational layer that quietly
leaks into the analysis is worse than no presentational layer at all.

What they share
---------------

Every annotation carries two things in common, and they are the two questions a
renderer asks about one:

``views``
    Which drawings it appears in, named the way :data:`LAYOUT_VIEWS` names them.
    Empty — the default — means *every* view, which is what somebody writing a
    note about a site wants; ``views: [l3]`` is for a note that only makes sense
    once the picture is prefixes rather than cables.
``color``
    A hex colour, ``#rgb`` or ``#rrggbb``. Omitted takes the kind's default,
    which is the one thing the renderer is allowed to decide for itself.

Anchoring
---------

A note is placed in one of two ways, and exactly one of them:

* **To a point** — ``geometry: {x, y}``, in the same points-and-``y``-upwards
  system §18 uses, because a note dragged around the canvas is stored by the
  same machinery that stores a dragged switch.
* **To something** — ``anchor: {element: ...}`` or ``anchor: {link: ...}``,
  which is what survives the diagram being laid out again. A note anchored to a
  device follows it; a note pinned at ``x: 400`` does not.

An anchored note may *also* carry a point, and then the point wins for
placement and the anchor is what the leader line points at. That combination is
exactly what dragging an anchored note produces, so it has to be expressible.

Areas and members
-----------------

An area is a box drawn *behind* the nodes. It says what it contains in one of
three ways, and at least one is required:

``members``
    An explicit list of element references. The box is the hull of wherever
    those elements were drawn.
``selector``
    ``namespace`` and/or ``labels``, evaluated against the inventory. An area
    over a namespace is the *declarative* form of ``--collapse`` grouping: the
    same set of elements, boxed rather than folded.
``geometry``
    An explicit rectangle. A zone that is a region of the canvas rather than a
    set of devices — "everything below this line is on the UPS" — is spelled
    this way.

Legends
-------

A legend is a keyed list of swatches, positioned by corner rather than by
coordinate, because a key belongs at the edge of the paper and stays there
however the drawing is laid out. Its entries are either written out or, with
``auto: layers``, generated from what the current view *actually drew* — which
is the only form that cannot go stale.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator, Field, model_validator

from netviz.errors import echo_value
from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.element import AREA_KIND, LEGEND_KIND, NOTE_KIND
from netviz.models.layout import LAYOUT_VIEWS, Coordinate, Extent
from netviz.models.metadata import Metadata
from netviz.models.scalars import ApiVersion, ElementRef

__all__ = [
    "ANNOTATION_KINDS",
    "AREA_KIND",
    "BORDER_STYLES",
    "COHERENCE_RULE",
    "COLOUR_PATTERN",
    "CORNERS",
    "LEGEND_KIND",
    "MAX_LEGEND_ENTRIES",
    "MAX_MEMBERS",
    "MAX_TEXT_LENGTH",
    "NOTE_KIND",
    "SWATCH_SHAPES",
    "VALUE_RULE",
    "Annotation",
    "AnnotationGeometry",
    "Area",
    "AreaSelector",
    "AreaSpec",
    "Legend",
    "LegendEntry",
    "LegendSpec",
    "Note",
    "NoteAnchor",
    "NoteSpec",
]

#: The three annotation kinds, in the order ``docs/schema.md`` introduces them.
ANNOTATION_KINDS: Final[tuple[str, ...]] = (NOTE_KIND, AREA_KIND, LEGEND_KIND)

#: Longest note text, in characters. A note is a callout, not a document: past a
#: couple of thousand characters it is unreadable in a diagram and belongs in
#: ``metadata.description`` or a file of its own.
MAX_TEXT_LENGTH: Final = 4_000

#: Most elements one area may name explicitly. A larger zone is a ``selector``.
MAX_MEMBERS: Final = 1_000

#: Most swatches one legend may carry. A key nobody can read is not a key.
MAX_LEGEND_ENTRIES: Final = 64

#: How an area's outline is drawn. ``dashed`` is the default because a zone is a
#: convention rather than a cable, and a solid box reads as a real container.
BORDER_STYLES: Final[tuple[str, ...]] = ("solid", "dashed", "dotted", "none")

#: Where a legend sits. A corner rather than a coordinate: a key belongs at the
#: edge of the paper and should stay there when the drawing is laid out again.
CORNERS: Final[tuple[str, ...]] = ("top-left", "top-right", "bottom-left", "bottom-right")

#: The swatch a legend entry draws next to its label.
SWATCH_SHAPES: Final[tuple[str, ...]] = ("box", "line", "dashed", "dotted", "ellipse")

_COLOUR_RE: Final = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")

#: The colour syntax, for the JSON Schema and for the documentation.
COLOUR_PATTERN: Final = _COLOUR_RE.pattern

#: The rule every *value* problem is reported under: a colour that is not a
#: colour, a view no layer draws, a text that is empty.
VALUE_RULE: Final = "NG-G003"

#: The rule every *cross-field* problem is reported under: an ``x`` with no
#: ``y``, a note that is neither anchored nor placed, an area that encloses
#: nothing, a legend that is neither generated nor written out.
#:
#: Split from :data:`VALUE_RULE` because the two behave differently under a
#: field-at-a-time write. An editor dragging an unplaced note writes ``x`` and
#: then ``y``, and a plan applying the same change spells it the same way; the
#: document is briefly incoherent and finally correct. A *value* problem is never
#: like that — ``color: red`` is wrong when it is written and wrong afterwards.
#: So :mod:`netviz.edit.apply` refuses a write that trips :data:`VALUE_RULE`
#: immediately and lets one that trips this pass, leaving it to the commit gate,
#: which sees the finished batch. See ``docs/schema.md`` §21.4.
COHERENCE_RULE: Final = "NG-G005"


def _colour(value: Any) -> Any:
    """Refuse anything that is not a hex colour.

    Deliberately narrow. Graphviz, Mermaid, SVG and mxGraph all understand
    ``#rrggbb`` and disagree about everything else, and an annotation whose
    colour renders in one exporter and not another is worse than one with no
    colour at all.
    """
    if not isinstance(value, str) or not _COLOUR_RE.match(value):
        raise field_error(
            f"{echo_value(value)} is not a colour; write it as '#rgb' or '#rrggbb'",
            rule=VALUE_RULE,
        )
    return value.lower()


#: A hex colour. Lower-cased so two documents that mean the same colour compare
#: equal, which is what keeps ``netviz plan`` quiet about a case change.
Colour = Annotated[str, BeforeValidator(_colour)]


def _views(value: Any) -> Any:
    """Refuse a view no layer draws.

    The same closed set :mod:`~netviz.models.layout` scopes geometry by, for
    the same reason: an annotation scoped to ``l4`` would silently never render.
    """
    if value is None:
        return value
    if isinstance(value, str):
        raise field_error(
            f"'views' is a list of view names, not the single name {echo_value(value)}",
            rule=VALUE_RULE,
        )
    if not isinstance(value, (list, tuple)):
        raise field_error(
            f"'views' must be a list of view names, got {type(value).__name__}",
            rule=VALUE_RULE,
        )
    for name in value:
        if name not in LAYOUT_VIEWS:
            raise field_error(
                f"unknown view {echo_value(name)}; expected one of {', '.join(LAYOUT_VIEWS)}",
                rule=VALUE_RULE,
            )
    return value


#: The views one annotation appears in. Empty means every one of them.
Views = Annotated[tuple[str, ...], BeforeValidator(_views)]


class AnnotationGeometry(NetvizModel):
    """Where an annotation is drawn, and how big it is.

    Flat rather than the nested ``position``/``size`` of §18, because these four
    numbers are what a drag and a resize produce and what an editor writes back;
    a note is one box, not a node with an optional extent.

    Coordinates are the §18 system: points, ``y`` upwards, and ``x``/``y`` is
    the **centre** of the box. Both or neither: half a position places nothing.
    """

    x: Coordinate | None = None
    y: Coordinate | None = None
    width: Extent | None = None
    height: Extent | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> AnnotationGeometry:
        if (self.x is None) != (self.y is None):
            raise field_error(
                "a position needs both 'x' and 'y'; one on its own places nothing",
                rule=COHERENCE_RULE,
            )
        return self

    @property
    def placed(self) -> bool:
        """Does this geometry pin a point?"""
        return self.x is not None and self.y is not None

    @property
    def sized(self) -> bool:
        """Does this geometry give a box?"""
        return self.width is not None and self.height is not None


class AnnotationBase(NetvizModel):
    """The envelope every annotation document shares."""

    api_version: ApiVersion = Field(alias="apiVersion", serialization_alias="apiVersion")
    kind: str
    metadata: Metadata

    @property
    def name(self) -> str:
        """Shortcut for ``metadata.name``."""
        return self.metadata.name

    def __str__(self) -> str:
        return f"{self.kind}/{self.metadata.name}"


class AnnotationSpecBase(NetvizModel):
    """What every annotation says about *where* it applies and how it looks."""

    #: Which drawings it appears in. Empty means every one of them.
    views: Views = ()
    #: Fill colour, or ``None`` for the kind's default.
    color: Colour | None = None

    def draws_in(self, view: str) -> bool:
        """Does this annotation appear in the ``view`` drawing?"""
        return not self.views or view in self.views


# --------------------------------------------------------------------------- #
# note
# --------------------------------------------------------------------------- #


class NoteAnchor(NetvizModel):
    """What a note is about: one element, or one link. Never both."""

    #: A device, adapter, patch panel, PDU, user or group, by reference.
    element: ElementRef | None = None
    #: A cable or a tunnel, by reference.
    link: ElementRef | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> NoteAnchor:
        if (self.element is None) == (self.link is None):
            raise field_error(
                "an anchor names either an 'element' or a 'link', and exactly one of them",
                rule=COHERENCE_RULE,
            )
        return self

    @property
    def reference(self) -> str:
        """Whichever of the two was written."""
        if self.element is not None:
            return self.element
        assert self.link is not None  # guaranteed by _exactly_one
        return self.link


class NoteSpec(AnnotationSpecBase):
    """A free-text callout: what it says, what it is about, and where it sits."""

    #: The text, in the markdown subset §21.1 documents: paragraphs, ``**bold**``,
    #: ``*italic*``, ``` `code` ``` and ``- `` bullets. Anything else is drawn
    #: verbatim, which is the honest failure mode for a formatting tool that
    #: several very different renderers have to agree about.
    text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT_LENGTH)]
    #: What the note is about, if anything.
    anchor: NoteAnchor | None = None
    #: Where it is drawn, and how big.
    geometry: AnnotationGeometry | None = None
    #: Draw a line from the note to what it is anchored to. Inert without an
    #: anchor, and off by default when the note *is* the anchor's neighbour.
    leader: bool = True

    @model_validator(mode="after")
    def _placed_somehow(self) -> NoteSpec:
        placed = self.geometry is not None and self.geometry.placed
        if self.anchor is None and not placed:
            raise field_error(
                "a note needs an 'anchor' or a 'geometry' with x and y; "
                "otherwise nothing knows where to draw it",
                rule=COHERENCE_RULE,
            )
        return self


class Note(AnnotationBase):
    """A ``kind: note`` document: one callout on the diagram."""

    kind: Literal["note"] = "note"
    spec: NoteSpec


# --------------------------------------------------------------------------- #
# area
# --------------------------------------------------------------------------- #


class AreaSelector(NetvizModel):
    """Which elements an area contains, said as a query rather than a list.

    Every clause that is given must match, and a selector with no clause at all
    is refused: it would silently box the entire inventory.
    """

    #: A namespace prefix. ``sites/hq`` matches ``sites/hq`` and everything under
    #: it, which is what makes an area the declarative form of ``--collapse``.
    namespace: str | None = None
    #: Every one of these labels must be present with this value.
    labels: dict[str, str] = Field(default_factory=dict)
    #: Element kinds, if the area is about a class of thing.
    kinds: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _says_something(self) -> AreaSelector:
        if self.namespace is None and not self.labels and not self.kinds:
            raise field_error(
                "a selector must narrow something — 'namespace', 'labels' or 'kinds'; "
                "an empty one would box the whole inventory",
                rule=COHERENCE_RULE,
            )
        return self


class AreaSpec(AnnotationSpecBase):
    """A labelled region drawn behind the nodes."""

    #: The caption. Omitted draws an unlabelled box, which is legitimate for a
    #: purely visual grouping.
    label: str | None = None
    #: Elements named outright.
    members: Annotated[tuple[ElementRef, ...], Field(max_length=MAX_MEMBERS)] = ()
    #: Elements matched.
    selector: AreaSelector | None = None
    #: An explicit rectangle, for a zone that is a region of the canvas rather
    #: than a set of devices.
    geometry: AnnotationGeometry | None = None
    #: How the outline is drawn.
    border: Literal["solid", "dashed", "dotted", "none"] = "dashed"
    #: Extra space between the hull of the members and the box drawn round them,
    #: in points. Ignored when :attr:`geometry` gives the rectangle outright.
    padding: Annotated[float, Field(ge=0.0, le=400.0)] = 16.0

    @model_validator(mode="after")
    def _contains_something(self) -> AreaSpec:
        boxed = self.geometry is not None and self.geometry.placed and self.geometry.sized
        if not self.members and self.selector is None and not boxed:
            raise field_error(
                "an area needs 'members', a 'selector', or a 'geometry' with a position "
                "and a size; otherwise it encloses nothing",
                rule=COHERENCE_RULE,
            )
        return self


class Area(AnnotationBase):
    """A ``kind: area`` document: one zone drawn behind the diagram."""

    kind: Literal["area"] = "area"
    spec: AreaSpec


# --------------------------------------------------------------------------- #
# legend
# --------------------------------------------------------------------------- #


class LegendEntry(NetvizModel):
    """One row of a key: a swatch and what it means."""

    label: Annotated[str, Field(min_length=1, max_length=200)]
    color: Colour | None = None
    shape: Literal["box", "line", "dashed", "dotted", "ellipse"] = "box"
    #: A second line, for the row that needs one.
    description: str | None = None


class LegendSpec(AnnotationSpecBase):
    """A key: what the colours and the line styles in this drawing mean."""

    title: str | None = None
    #: Which corner of the drawing it sits in.
    corner: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "bottom-right"
    #: ``layers`` builds the entries from what the current view actually drew —
    #: the kinds of node present and the media of the links present — which is
    #: the only form of key that cannot go stale. ``None`` uses :attr:`entries`.
    auto: Literal["layers"] | None = None
    entries: Annotated[tuple[LegendEntry, ...], Field(max_length=MAX_LEGEND_ENTRIES)] = ()

    @model_validator(mode="after")
    def _says_something(self) -> LegendSpec:
        if self.auto is None and not self.entries:
            raise field_error(
                "a legend needs 'entries', or 'auto: layers' to derive them from the drawing",
                rule=COHERENCE_RULE,
            )
        if self.auto is not None and self.entries:
            raise field_error(
                "a legend is either generated ('auto') or written out ('entries'), not both",
                rule=COHERENCE_RULE,
            )
        return self


class Legend(AnnotationBase):
    """A ``kind: legend`` document: one key on the diagram."""

    kind: Literal["legend"] = "legend"
    spec: LegendSpec


#: Any annotation document. Not a discriminated union field anywhere — the three
#: are indexed apart, like layouts — but the alias is what every signature that
#: handles "an annotation" is spelled with.
Annotation = Note | Area | Legend
