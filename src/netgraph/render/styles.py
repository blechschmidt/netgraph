"""Resolving the style ladder, once, for every backend.

:mod:`netgraph.models.style` is what a document may say. :mod:`netgraph.render.theme`
is what a stylesheet may say. This module is where the two meet the built-in
palette and become a single answer per drawn thing — the answer DOT, draw.io and
the JSON export all read, so that a switch somebody painted navy is navy in all
three and is navy for the same reason.

One resolution, four consumers
------------------------------

Doing it here rather than in each backend is not tidiness. A style has an
inheritance ladder (element, theme, icon set, palette) and a *provenance*: the
editor's inspector has to say which rung each value came from for its "reset to
theme" action to be honest about what it will do. Three backends each
reimplementing that would drift on the first field anybody added, and the
drift would show up as a diagram that looks different depending on how it was
exported.

What the resolver does *not* do
-------------------------------

It does not decide emphasis. A ``--highlight`` and a ``netgraph diff`` overlay
still win over a resolved style, and each backend applies them on top: a removed
device drawn in the user's chosen navy instead of red would make the diff
unreadable, and the whole point of an overlay is that it is louder than the
drawing under it.

It also does not translate. :func:`dot_shape` and the mxGraph table in
:mod:`netgraph.drawio.styles` are where a vocabulary name becomes a backend's
spelling, because the two backends disagree and neither disagreement belongs in
the resolved value.

``--no-style``
--------------

The escape hatch renders with the ladder's *bottom two rungs only*: the icon set
and the built-in palette. Every declared style, element and theme alike, is
ignored, and the output is byte-identical to what the same inventory produced
before styling existed. That is what makes it useful — it is the answer to "is
this diagram odd because of the network or because of the stylesheet?".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from netgraph.models.style import NO_ICON, hex_colour
from netgraph.render.graph import Edge, EdgeKind, Graph, Node
from netgraph.render.icons import IconTheme, suffix_order
from netgraph.render.palette import edge_style_for, node_style_for
from netgraph.render.theme import StyleTarget, Theme

__all__ = [
    "DEFAULT_LAYER",
    "ELEMENT_LAYER",
    "ICONS_LAYER",
    "PLAIN",
    "THEME_LAYER",
    "ResolvedStyle",
    "StyleMap",
    "dot_shape",
    "edge_target",
    "fade",
    "node_target",
    "resolve_style",
]

#: The four rungs of the ladder, as the JSON export and the editor's inspector
#: name them. A theme rung additionally carries which rule won:
#: ``theme:blueprint#3``.
ELEMENT_LAYER: Final = "element"
THEME_LAYER: Final = "theme"
ICONS_LAYER: Final = "icons"
DEFAULT_LAYER: Final = "default"

#: Which edge kinds a theme selector calls what. A cable is ``cable`` and a
#: tunnel is ``tunnel`` whichever way the layer happens to draw it, because that
#: is the word the inventory uses; everything else is named by its edge kind, so
#: ``kind: subnet`` styles a membership line.
_EDGE_KINDS: Final[Mapping[EdgeKind, str]] = MappingProxyType(
    {
        EdgeKind.CABLE: "cable",
        EdgeKind.ATTACHMENT: "adapter",
        EdgeKind.TUNNEL: "tunnel",
        EdgeKind.ENCAPSULATION: "tunnel",
    }
)

#: How a vocabulary shape is spelled in DOT, plus the two Graphviz synonyms the
#: built-in palette already uses (``rectangle``, ``oval``) so a default resolves
#: to the exact bytes it always did. Anything not here falls back to ``box``:
#: the table is the whole of what may reach a DOT file, which is what keeps a
#: shape name from being a way to write an arbitrary attribute.
_DOT_SHAPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "box": "box",
        "rounded": "box",
        "ellipse": "ellipse",
        "circle": "circle",
        "diamond": "diamond",
        "hexagon": "hexagon",
        "triangle": "triangle",
        "cylinder": "cylinder",
        "box3d": "box3d",
        "folder": "folder",
        "note": "note",
        "parallelogram": "parallelogram",
        "trapezium": "trapezium",
        "plaintext": "plaintext",
        "rectangle": "rectangle",
        "oval": "oval",
    }
)

#: The shape a name netgraph does not know falls back to.
_FALLBACK_SHAPE: Final = "box"


def dot_shape(name: str | None) -> str:
    """The Graphviz spelling of a resolved shape, defaulted for an unknown one."""
    if name is None:
        return _FALLBACK_SHAPE
    return _DOT_SHAPES.get(name, _FALLBACK_SHAPE)


def fade(colour: str | None, opacity: float | None) -> str | None:
    """``("#1e3a8a", 0.5)`` → ``"#1e3a8a80"``.

    Graphviz, SVG and draw.io all read a fourth hex pair as alpha, which is the
    only way to express a per-element opacity that survives into a PNG. ``none``
    is already invisible and is left alone; so is a colour with no opacity asked
    of it, which is what keeps an unstyled rendering byte-identical.
    """
    # ``opacity: 1`` is not a no-op that somebody wrote by accident — it is how
    # an element opts *out* of an opacity a theme gave it — but the alpha pair
    # it would add says nothing, and a colour that renders identically should
    # be spelled identically.
    if colour is None or opacity is None or opacity >= 1 or colour == "none":
        return colour
    if not colour.startswith("#"):  # pragma: no cover - defensive; validated upstream
        return colour
    body = colour[1:]
    if len(body) == 3:
        body = "".join(char * 2 for char in body)
    alpha = max(0, min(255, round(opacity * 255)))
    return f"#{body[:6]}{alpha:02x}"


@dataclass(frozen=True, slots=True)
class ResolvedStyle:
    """The finished appearance of one node or one link.

    Colours are hex (or ``none``): a named colour is resolved on the way in, so
    no backend has to carry the name table. Everything else is the vocabulary
    spelling, and each backend translates.
    """

    fill: str | None = None
    stroke: str | None = None
    stroke_width: float | None = None
    dash: str | None = None
    font_color: str | None = None
    font_size: int | None = None
    shape: str | None = None
    #: The picture to draw instead of the shape: a *name*, not a file. The
    #: backend resolves it against the icon theme, which is the only thing that
    #: knows which extensions its renderer can read.
    icon: str | None = None
    opacity: float | None = None
    #: Which rung of the ladder supplied each field, keyed as a document spells
    #: the field. Only the fields that were set appear.
    origin: Mapping[str, str] = MappingProxyType({})

    @property
    def faded_fill(self) -> str | None:
        """:attr:`fill` with :attr:`opacity` folded into its alpha channel."""
        return fade(self.fill, self.opacity)

    @property
    def faded_stroke(self) -> str | None:
        return fade(self.stroke, self.opacity)

    @property
    def faded_font_color(self) -> str | None:
        return fade(self.font_color, self.opacity)

    @property
    def penwidth(self) -> str | None:
        """:attr:`stroke_width` as Graphviz spells it, or ``None`` to inherit."""
        if self.stroke_width is None:
            return None
        return f"{self.stroke_width:g}"

    def to_dict(self) -> dict[str, Any]:
        """The JSON form: the values, then where each came from.

        Exported under a node's or an edge's ``style`` key, so a consumer of the
        JSON draws what netgraph drew without reimplementing the ladder.
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
        payload: dict[str, Any] = {key: value for key, value in values.items() if value is not None}
        # In field order rather than in resolution order: which rung answered
        # first is an implementation detail, and a stable key order is what keeps
        # two exports of an unchanged inventory byte-identical.
        payload["from"] = {key: self.origin[key] for key in values if key in self.origin}
        return payload


#: What a node or a link looks like when nothing at all has been said about it.
#: Used as the ``--no-style`` floor for a kind the palette does not know.
PLAIN: Final = ResolvedStyle()


def node_target(node: Node) -> StyleTarget:
    """The facts a theme selector asks about one node."""
    return StyleTarget(
        kind=node.kind,
        name=node.name,
        namespace=node.namespace,
        labels=node.labels,
        style=node.style,
    )


def edge_target(edge: Edge) -> StyleTarget:
    """The facts a theme selector asks about one link."""
    return StyleTarget(
        kind=_EDGE_KINDS.get(edge.kind, edge.kind.value),
        name=edge.name,
        namespace=edge.namespace,
        labels=edge.labels,
        style=edge.style,
    )


def _node_defaults(node: Node) -> dict[str, Any]:
    """The built-in palette's answer for one node: shape, fill and outline."""
    shape, fill, stroke = node_style_for(node.kind)
    return {"shape": shape, "fill": fill, "stroke": stroke}


def _edge_defaults(edge: Edge) -> dict[str, Any]:
    """The built-in palette's answer for one link: colour and line pattern."""
    colour, dash = edge_style_for(edge)
    return {"stroke": colour, "dash": dash}


def _icon_default(target: StyleTarget, icons: IconTheme | None, *, output: str) -> str | None:
    """The picture the ``--icons`` theme has for this kind, if it has one."""
    if icons is None:
        return None
    return target.kind if icons.file_for(target.kind, prefer=suffix_order(output)) else None


def resolve_style(
    target: StyleTarget,
    *,
    theme: Theme | None = None,
    icons: IconTheme | None = None,
    defaults: Mapping[str, Any] | None = None,
    output: str = "",
    styling: bool = True,
) -> ResolvedStyle:
    """Walk the ladder for one drawn thing and record where each value came from.

    Args:
        target: What is being drawn; see :class:`~netgraph.render.theme.StyleTarget`.
        theme: The stylesheet in force, or ``None``.
        icons: The ``--icons`` theme, which supplies ``icon`` and nothing else.
        defaults: The built-in palette's answer, the bottom rung.
        output: The output format, which decides whether a vector or a raster
            icon is preferred (see :func:`~netgraph.render.icons.suffix_order`).
        styling: ``False`` is ``--no-style``: skip the element and theme rungs
            entirely, leaving the icon set and the palette.
    """
    values: dict[str, Any] = {}
    origin: dict[str, str] = {}

    def take(source: Mapping[str, Any], layer: str) -> None:
        for name, value in source.items():
            if name in values or value is None:
                continue
            values[name] = value
            origin[name] = layer

    if styling:
        if target.style is not None:
            take(target.style.declared(), ELEMENT_LAYER)
        if theme is not None:
            for index, rule in theme.matching(target):
                take(rule.style.declared(), f"{THEME_LAYER}:{theme.name}#{index}")

    icon = _icon_default(target, icons, output=output)
    if icon is not None:
        take({"icon": icon}, ICONS_LAYER)
    take(dict(defaults or {}), DEFAULT_LAYER)

    # ``icon: none`` is a *decision* — draw this one as a plain shape — so it
    # survives resolution as a declared value and is dropped here, once, rather
    # than being special-cased in three backends.
    resolved_icon = values.get("icon")
    if resolved_icon == NO_ICON:
        resolved_icon = None

    return ResolvedStyle(
        fill=hex_colour(values.get("fill")),
        stroke=hex_colour(values.get("stroke")),
        stroke_width=values.get("strokeWidth"),
        dash=values.get("dash"),
        font_color=hex_colour(values.get("fontColor")),
        font_size=values.get("fontSize"),
        shape=values.get("shape"),
        icon=resolved_icon,
        opacity=values.get("opacity"),
        origin=MappingProxyType(dict(origin)),
    )


@dataclass(frozen=True)
class StyleMap:
    """Every resolved style of one rendering, keyed the way a backend asks.

    Nodes by fully-qualified name and links by position, which are the two
    identities the renderers already carry: a backend loops over
    ``graph.nodes`` and ``graph.edges`` and asks here, rather than threading a
    second parallel sequence through every function it has.
    """

    nodes: Mapping[str, ResolvedStyle] = MappingProxyType({})
    edges: tuple[ResolvedStyle, ...] = ()
    #: Was the ladder walked at all? ``False`` under ``--no-style``.
    enabled: bool = True
    #: The theme that was in force, for the provenance report and the editor.
    theme: Theme | None = None

    @classmethod
    def build(
        cls,
        graph: Graph,
        *,
        theme: Theme | None = None,
        icons: IconTheme | None = None,
        output: str = "",
        styling: bool = True,
    ) -> StyleMap:
        """Resolve every node and every link of ``graph``."""
        nodes = {
            fqn: resolve_style(
                node_target(node),
                theme=theme,
                icons=icons,
                defaults=_node_defaults(node),
                output=output,
                styling=styling,
            )
            for fqn, node in graph.nodes.items()
        }
        edges = tuple(
            resolve_style(
                edge_target(edge),
                theme=theme,
                # A link has no picture: an edge is a line, and the icon rung
                # is about what replaces a *shape*.
                icons=None,
                defaults=_edge_defaults(edge),
                output=output,
                styling=styling,
            )
            for edge in graph.edges
        )
        return cls(
            nodes=MappingProxyType(nodes),
            edges=edges,
            enabled=styling,
            theme=theme if styling else None,
        )

    def node(self, fqn: str) -> ResolvedStyle:
        return self.nodes.get(fqn, PLAIN)

    def edge(self, index: int) -> ResolvedStyle:
        return self.edges[index] if 0 <= index < len(self.edges) else PLAIN

    def kinds_with_icons(self) -> tuple[str, ...]:
        """Every icon name a node of this rendering asks for, sorted.

        The backends resolve icons *per name* rather than per kind now that an
        element may override one, so this is what replaces the old "which kinds
        are in the graph" question.
        """
        return tuple(sorted({style.icon for style in self.nodes.values() if style.icon}))
