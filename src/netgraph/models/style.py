"""``spec.style`` — how one element is drawn (§22 of ``docs/schema.md``).

Appearance is inventory data. A colour somebody chose for the core switches is a
decision about the network's documentation, and it belongs in the documents
beside the cabling rather than in a flag somebody has to remember to pass. That
is what makes the visual editor's "select a shape, change how it looks" loop
expressible without breaking the single-source-of-truth rule: the editor writes
``spec.style.fill`` and the YAML is the record.

The vocabulary is closed, and deliberately so
---------------------------------------------

Every field here ends up inside a Graphviz attribute, an mxGraph style string or
an SVG attribute, and all three are text formats that netgraph *generates*. A
free-form pass-through would mean a value like ``red", shape="none`` reaching a
DOT file, or ``#fff;shape=image;image=data:...`` reaching a draw.io style — both
are injection, and both are exactly the kind of thing an inventory shared across
a team should not be able to do.

So: colours are a hex literal or one of :data:`NAMED_COLOURS`, and every other
field is a small enum or a bounded number. Nothing typed into a manifest reaches
an output format unvalidated, and a typo is answered with the nearest legal
spelling rather than with a broken diagram three steps later.

The nine fields
---------------

``fill``, ``stroke`` and ``fontColor`` are colours. ``strokeWidth`` and
``fontSize`` are bounded numbers. ``dash`` is the line pattern, spelled the way
Graphviz spells it because that is the vocabulary the built-in palette already
uses (``bold`` is a width there, not a pattern, and is kept for exactly that
reason). ``shape`` is a glyph from :data:`SHAPES`, which is the intersection of
what Graphviz draws and what draw.io can be told to draw, so a shape survives a
round-trip. ``icon`` overrides the ``--icons`` theme's pick for this one element
— a name resolved against the theme directory, which is why it is a bare name
and not a path. ``opacity`` fades one element without touching the rest.

Every one of them is optional, and an absent field means *inherit*: from the
theme, then from the icon set, then from the built-in palette. Unsetting a field
is how the editor's "reset to theme" works, and it is why nothing here has a
non-``None`` default — a default written into the document would pin the value
to the element and break the inheritance it is meant to fall back to.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from difflib import get_close_matches
from types import MappingProxyType
from typing import Annotated, Any, Final

from pydantic import BeforeValidator, Field, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error

__all__ = [
    "COLOUR_PATTERN",
    "DASHES",
    "ICON_NAME_PATTERN",
    "MAX_FONT_SIZE",
    "MAX_STROKE_WIDTH",
    "MIN_FONT_SIZE",
    "NAMED_COLOURS",
    "NO_ICON",
    "SHAPES",
    "STYLE_FIELDS",
    "STYLE_RULE",
    "STYLE_VALUE_RULE",
    "Dash",
    "IconName",
    "Shape",
    "Style",
    "StyleColour",
    "field_alias",
    "hex_colour",
]

#: The rule every *value* problem in a style is reported under: a colour that is
#: not a colour, a shape netgraph cannot draw, a font size nobody can read.
#: Mirrors :data:`netgraph.models.annotation.VALUE_RULE`, and behaves the same
#: way under a field-at-a-time write — a bad value is wrong when it is written
#: and wrong afterwards, so :mod:`netgraph.edit.apply` refuses it immediately.
STYLE_VALUE_RULE: Final = "NG-Z001"

#: The rule a style that is *self-defeating* is reported under: an element faded
#: to nothing, a label the same colour as the box behind it. Unlike
#: :data:`STYLE_VALUE_RULE` these are warnings from the semantic validator, not
#: schema errors — each value is legal on its own and only the combination is a
#: mistake, which an editor may pass through on its way somewhere else.
STYLE_RULE: Final = "NG-Z003"

#: The named colours a style may use, and the hex each one resolves to.
#:
#: A curated set rather than the CSS list. Every entry here has to read the same
#: in a Graphviz SVG, a draw.io canvas and a browser, and the CSS names include
#: several pairs no reader can tell apart (``gray``/``grey``/``darkgray``) plus a
#: long tail nobody reaches for. Twenty-four names cover a diagram's palette;
#: anything more particular is spelled as hex, which is always accepted.
#:
#: The hues are the ones the built-in palette already draws with, so a manifest
#: that says ``fill: green`` gets the same green a switch has by default rather
#: than a second, slightly different one.
NAMED_COLOURS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "none": "none",
        "transparent": "none",
        "white": "#ffffff",
        "black": "#111827",
        "grey": "#6b7280",
        "gray": "#6b7280",
        "silver": "#e2e8f0",
        "slate": "#475569",
        "red": "#dc2626",
        "maroon": "#991b1b",
        "orange": "#ea580c",
        "amber": "#b45309",
        "yellow": "#eab308",
        "olive": "#65a30d",
        "lime": "#84cc16",
        "green": "#16a34a",
        "teal": "#0f766e",
        "cyan": "#0891b2",
        "blue": "#2563eb",
        "navy": "#1e3a8a",
        "indigo": "#4338ca",
        "violet": "#7c3aed",
        "purple": "#9333ea",
        "magenta": "#c026d3",
        "pink": "#be185d",
        "brown": "#78350f",
    }
)

_HEX_RE: Final = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")

#: The colour syntax, for the JSON Schema and for the documentation: a hex
#: literal or one of :data:`NAMED_COLOURS`.
COLOUR_PATTERN: Final = (
    r"^(?:#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})|" + "|".join(sorted(NAMED_COLOURS)) + r")$"
)

#: How a line is drawn. Spelled as Graphviz spells it, because the built-in
#: palette is already written in this vocabulary (``fiber`` is ``bold``) and a
#: theme that wants to restate a default must be able to say it.
DASHES: Final[tuple[str, ...]] = ("solid", "dashed", "dotted", "bold")

#: The glyph a node is drawn as. The intersection of what Graphviz draws and
#: what draw.io can be told to draw, so a shape survives an export and a
#: re-import; anything Graphviz alone knows would be lost on the way back.
SHAPES: Final[tuple[str, ...]] = (
    "box",
    "rounded",
    "ellipse",
    "circle",
    "diamond",
    "hexagon",
    "triangle",
    "cylinder",
    "box3d",
    "folder",
    "note",
    "parallelogram",
    "trapezium",
    "plaintext",
)

#: What ``icon`` is set to in order to draw *this* element as a plain shape
#: while the rest of the diagram keeps the theme's pictures.
NO_ICON: Final = "none"

_ICON_RE: Final = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")

#: The icon-name grammar: a bare lower-case name, resolved against the icon
#: theme's directory exactly as an element kind is. No dots and no separators —
#: an icon is *chosen from the theme*, never read from a path an inventory
#: names, which is what keeps a shared manifest from reaching outside the theme
#: directory it was rendered with.
ICON_NAME_PATTERN: Final = _ICON_RE.pattern

#: Thinnest and thickest outline, in Graphviz points.
MAX_STROKE_WIDTH: Final = 20.0

#: Smallest and largest label, in points. Below six nobody reads it and above
#: ninety-six one node is the whole page.
MIN_FONT_SIZE: Final = 6
MAX_FONT_SIZE: Final = 96


def _suggest(value: str, candidates: Iterable[str]) -> str:
    """``"; did you mean 'dashed'?"``, or a listing of what is legal.

    A typo in a colour name is the single most likely mistake in this block, and
    a diagnostic that only says "not a colour" makes the user go and read the
    schema. One that names the nearest legal spelling usually ends the problem
    on the spot.
    """
    allowed = tuple(candidates)
    close = get_close_matches(value.lower(), allowed, n=1, cutoff=0.6)
    if close:
        return f"; did you mean {close[0]!r}?"
    return f"; expected one of {', '.join(allowed)}"


def _colour(value: Any) -> Any:
    """Refuse anything that is not a hex literal or a name from the vocabulary.

    The spelling is *kept*, not folded to hex: ``fill: navy`` is what the user
    wrote and what ``netgraph fmt`` should leave in the file. Resolution to
    ``#1e3a8a`` happens once, in :func:`hex_colour`, on the way to a renderer.
    """
    if not isinstance(value, str):
        raise field_error(
            f"{echo_value(value)} is not a colour; write a name or '#rrggbb'",
            rule=STYLE_VALUE_RULE,
        )
    text = value.strip()
    if _HEX_RE.match(text):
        return text.lower()
    folded = text.lower()
    if folded in NAMED_COLOURS:
        return folded
    raise field_error(
        f"{echo_value(value)} is not a colour; write '#rgb', '#rrggbb' or a "
        f"named colour{_suggest(folded, NAMED_COLOURS)}",
        rule=STYLE_VALUE_RULE,
    )


#: A colour: a hex literal, or one of :data:`NAMED_COLOURS`.
StyleColour = Annotated[str, BeforeValidator(_colour)]


def _enum(name: str, allowed: tuple[str, ...]) -> Callable[[Any], Any]:
    """A validator refusing anything outside ``allowed``, with a suggestion."""

    def check(value: Any) -> Any:
        if not isinstance(value, str):
            raise field_error(
                f"{echo_value(value)} is not a {name}; expected one of {', '.join(allowed)}",
                rule=STYLE_VALUE_RULE,
            )
        folded = value.strip().lower()
        if folded not in allowed:
            raise field_error(
                f"{echo_value(value)} is not a {name}{_suggest(folded, allowed)}",
                rule=STYLE_VALUE_RULE,
            )
        return folded

    return check


#: A line pattern from :data:`DASHES`.
Dash = Annotated[str, BeforeValidator(_enum("line style", DASHES))]

#: A glyph from :data:`SHAPES`.
Shape = Annotated[str, BeforeValidator(_enum("shape", SHAPES))]


def _icon_name(value: Any) -> Any:
    """Refuse an icon name that is a path, or that could become one."""
    if not isinstance(value, str):
        raise field_error(f"{echo_value(value)} is not an icon name", rule=STYLE_VALUE_RULE)
    text = value.strip().lower()
    if not _ICON_RE.match(text):
        raise field_error(
            f"{echo_value(value)} is not an icon name: write the bare name of a picture in "
            f"the icon theme, e.g. 'firewall', or {NO_ICON!r} to draw this element as a "
            f"plain shape. It must match {ICON_NAME_PATTERN}",
            rule=STYLE_VALUE_RULE,
        )
    return text


#: The name of a picture inside the icon theme, or :data:`NO_ICON`.
IconName = Annotated[str, BeforeValidator(_icon_name)]

#: Every field of the block, spelled the way a document spells it. The order is
#: the one the editor's inspector lists them in and the one ``netgraph fmt``
#: writes them in: what the shape *is*, then how it is outlined, then the label,
#: then the two overrides that replace the shape outright.
STYLE_FIELDS: Final[tuple[str, ...]] = (
    "fill",
    "stroke",
    "strokeWidth",
    "dash",
    "fontColor",
    "fontSize",
    "shape",
    "icon",
    "opacity",
)


class Style(NetgraphModel):
    """``spec.style`` — the appearance of one element (§22).

    Every field is optional and an absent one inherits; see the module
    docstring for the ladder and ``docs/styling.md`` for the worked examples.
    """

    #: Interior colour. ``none`` draws an unfilled shape.
    fill: StyleColour | None = None
    #: Outline colour, and — for a link — the colour of the line itself.
    stroke: StyleColour | None = None
    #: Outline width in points.
    stroke_width: Annotated[float, Field(gt=0, le=MAX_STROKE_WIDTH)] | None = Field(
        default=None, alias="strokeWidth", serialization_alias="strokeWidth"
    )
    #: Line pattern, one of :data:`DASHES`.
    dash: Dash | None = None
    #: Label colour.
    font_color: StyleColour | None = Field(
        default=None, alias="fontColor", serialization_alias="fontColor"
    )
    #: Label size in points.
    font_size: Annotated[int, Field(ge=MIN_FONT_SIZE, le=MAX_FONT_SIZE)] | None = Field(
        default=None, alias="fontSize", serialization_alias="fontSize"
    )
    #: Glyph, one of :data:`SHAPES`. Ignored on a link, which has no shape.
    shape: Shape | None = None
    #: Picture to draw this element as, overriding the ``--icons`` theme's pick
    #: for its kind. :data:`NO_ICON` draws the plain shape instead.
    icon: IconName | None = None
    #: How opaque the element is drawn, from ``0`` (invisible) to ``1``.
    opacity: Annotated[float, Field(ge=0.0, le=1.0)] | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Style:
        """``NG-Z002``: a ``style`` block that says nothing is a mistake.

        Almost always a half-finished edit or a key indented one level too far.
        An empty mapping would otherwise validate, render identically, and give
        the writer no signal at all that the thing they typed did nothing.
        """
        if not self.declared():
            raise field_error(
                "'style' is empty; give it at least one of "
                f"{', '.join(STYLE_FIELDS)}, or remove it",
                rule="NG-Z002",
            )
        return self

    def declared(self) -> Mapping[str, Any]:
        """The fields this block actually sets, keyed as a document spells them.

        The one place the alias mapping is applied, so a caller — the resolver,
        the editor's inspector, the JSON export — never has to know that
        ``strokeWidth`` is ``stroke_width`` in Python.
        """
        values = {
            "fill": self.fill,
            "stroke": self.stroke,
            "strokeWidth": self.stroke_width,
            "dash": self.dash,
            "fontColor": self.font_color,
            "fontSize": self.font_size,
            "shape": self.shape,
            "icon": self.icon,
            "opacity": self.opacity,
        }
        return MappingProxyType({key: value for key, value in values.items() if value is not None})


def field_alias(field: str) -> str:
    """``"strokeWidth"`` → ``"stroke_width"``: the Python name of a wire name."""
    return {"strokeWidth": "stroke_width", "fontColor": "font_color", "fontSize": "font_size"}.get(
        field, field
    )


def hex_colour(value: str | None) -> str | None:
    """Resolve a validated colour to what a renderer emits.

    A name becomes its hex; a hex literal is returned as it stands; ``none``
    stays ``none``, which is what Graphviz, mxGraph and SVG all spell "no
    colour". ``None`` in gives ``None`` out, meaning "inherit".
    """
    if value is None:
        return None
    return NAMED_COLOURS.get(value, value)
