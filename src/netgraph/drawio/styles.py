"""How a netgraph node, link and namespace look once they are mxGraph cells.

An mxGraph style is a flat ``key=value;key=value`` string, which constrains this
module more than it looks:

* **No semicolons and no equals signs inside a value.** draw.io splits the
  string on both, so anything embedded — most importantly an icon's data URI —
  has to survive that split. draw.io's own convention is
  ``data:image/png,<base64>``: the comma stands in for the ``;base64`` an
  ordinary data URI would carry, and draw.io puts it back when it draws the
  image. :func:`data_uri` writes that form, which is why an exported diagram
  opens with its icons in draw.io and *not* in a strict data-URI viewer.
* **Order is meaningful to nobody but a diff.** So it is fixed, and the same
  node exported twice produces the same bytes.

The colours are not this module's to choose: they come from
:mod:`netgraph.render.dot`, so a switch is the same green in a ``.drawio`` file
as it is in the SVG. What *is* chosen here is the translation of a Graphviz line
style — ``solid``, ``bold``, ``dashed``, ``dotted`` — into the two mxGraph knobs
that carry it, ``dashed``/``dashPattern`` and ``strokeWidth``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Final

from netgraph.render.dot import (
    CLUSTER_PALETTE,
    DEFAULT_EDGE_PALETTE,
    DEFAULT_NODE_PALETTE,
    EDGE_PALETTE,
    NODE_PALETTE,
)
from netgraph.render.graph import EdgeKind
from netgraph.render.icons import VECTOR_FIRST, IconTheme

__all__ = [
    "MAX_ICON_BYTES",
    "data_uri",
    "edge_style",
    "group_style",
    "icon_data_uri",
    "node_style",
    "palette_for",
    "style_of",
]

#: Ceiling on one icon inlined into a diagram. The shipped set is a few
#: kilobytes per kind; a user-supplied theme could hold a megabyte photograph
#: per device, and thirty of those is a file draw.io will not open. Past the
#: bound the node falls back to a coloured box and the manifest says so.
MAX_ICON_BYTES: Final = 512 * 1024

#: MIME type per icon extension. Only what :data:`~netgraph.render.icons.
#: ICON_SUFFIXES` allows, so an unexpected file cannot reach a style string.
_MEDIA_TYPES: Final[Mapping[str, str]] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}

#: Base style of every vertex. ``html=1`` is what makes draw.io render the
#: label as markup rather than as literal text, which is what every draw.io
#: cell carries; without it a label holding an ampersand shows the entity.
_VERTEX_BASE: Final = "rounded=0;whiteSpace=wrap;html=1;"

#: A node drawn as its kind's picture. The label goes *below* the image, which
#: is where every network diagram puts it, and the image is not stretched to the
#: cell — ``imageAspect=1`` keeps a wide icon wide.
_IMAGE_BASE: Final = (
    "shape=image;html=1;imageAspect=1;aspect=fixed;labelBackgroundColor=none;"
    "verticalLabelPosition=bottom;verticalAlign=top;"
)

#: A namespace frame. ``container=1`` is what makes the nodes inside it move
#: with it — the one behaviour that justifies exporting groups at all — and
#: ``collapsible=0`` keeps a stakeholder from folding a site away and wondering
#: where the switches went.
_GROUP_BASE: Final = (
    "rounded=0;html=1;whiteSpace=wrap;dashed=1;fillColor=none;container=1;"
    "collapsible=0;expand=0;recursiveResize=0;verticalAlign=top;align=left;"
    "spacingLeft=8;spacingTop=4;fontSize=11;"
)

#: Base style of every edge. Both arrowheads are off: a cable has no direction
#: (§7.1), and an arrow in a draw.io file is a claim about traffic flow that
#: netgraph is not making.
_EDGE_BASE: Final = "html=1;endArrow=none;startArrow=none;rounded=0;"

#: How a Graphviz line style is spelled in mxGraph. ``bold`` is a *width*
#: rather than a pattern, which is why the table carries both knobs.
_LINE_STYLES: Final[Mapping[str, str]] = {
    "solid": "dashed=0;strokeWidth=1;",
    "bold": "dashed=0;strokeWidth=3;",
    "dashed": "dashed=1;dashPattern=8 8;strokeWidth=1;",
    "dotted": "dashed=1;dashPattern=1 4;strokeWidth=1;",
}

#: How a link's routing style (§18) is drawn by draw.io. netgraph's three
#: spellings map onto mxGraph's edge styles, which are named differently and
#: mean the same three things.
_ROUTING_STYLES: Final[Mapping[str, str]] = {
    "spline": "edgeStyle=orthogonalEdgeStyle;curved=1;",
    "orthogonal": "edgeStyle=orthogonalEdgeStyle;curved=0;",
    "straight": "edgeStyle=none;curved=0;",
}

#: Which palette entry each non-cable edge kind takes. The cable kinds are
#: keyed by medium instead, which is why this maps only the rest.
_EDGE_KIND_PALETTE: Final[Mapping[EdgeKind, str]] = {
    EdgeKind.ATTACHMENT: "attachment",
    EdgeKind.SUBNET: "subnet",
    EdgeKind.TUNNEL: "tunnel",
    EdgeKind.ENCAPSULATION: "encapsulation",
    EdgeKind.MEMBERSHIP: "membership",
    EdgeKind.BGP: "bgp",
    EdgeKind.OSPF: "ospf",
    EdgeKind.OUTLET: "outlet",
    EdgeKind.POE: "poe",
}


def style_of(pairs: Iterable[tuple[str, str]]) -> str:
    """``[("fillColor", "#fff")]`` → ``"fillColor=#fff;"``.

    Every style string ends in a semicolon, which is draw.io's own convention
    and what makes concatenating two of them safe.
    """
    return "".join(f"{key}={value};" for key, value in pairs)


def palette_for(kind: str) -> tuple[str, str]:
    """``(fill, stroke)`` for an element kind, defaulted for an unknown one."""
    return NODE_PALETTE.get(kind, DEFAULT_NODE_PALETTE)


def node_style(kind: str, *, icon: str | None = None) -> str:
    """The style of one vertex.

    Args:
        kind: The element kind, which picks the colours.
        icon: A data URI to draw the node as, or ``None`` for a coloured box.
            An icon replaces the shape entirely — that is what ``shape=image``
            means — so the fill is dropped with it and the outline is kept as
            the label's colour.
    """
    fill, stroke = palette_for(kind)
    if icon is not None:
        return _IMAGE_BASE + style_of((("image", icon), ("fontColor", stroke)))
    return _VERTEX_BASE + style_of(
        (("fillColor", fill), ("strokeColor", stroke), ("fontColor", "#111827"))
    )


def edge_style(*, palette: str, routing: str = "spline") -> str:
    """The style of one edge.

    Args:
        palette: The :data:`~netgraph.render.dot.EDGE_PALETTE` key — a cable
            medium, or the name of an edge kind that is not a cable.
        routing: One of :data:`~netgraph.models.layout.ROUTING_STYLES`.
    """
    colour, line = EDGE_PALETTE.get(palette, DEFAULT_EDGE_PALETTE)
    return (
        _ROUTING_STYLES.get(routing, _ROUTING_STYLES["spline"])
        + _EDGE_BASE
        + _LINE_STYLES.get(line, _LINE_STYLES["solid"])
        + style_of((("strokeColor", colour), ("fontColor", colour), ("fontSize", "10")))
    )


def palette_key(kind: EdgeKind, medium: str) -> str:
    """Which palette entry a link takes: its medium, or its kind."""
    if kind is EdgeKind.CABLE:
        return medium or "copper"
    return _EDGE_KIND_PALETTE.get(kind, "copper")


def group_style() -> str:
    """The style of a namespace frame."""
    stroke, label = CLUSTER_PALETTE
    return _GROUP_BASE + style_of((("strokeColor", stroke), ("fontColor", label)))


def data_uri(media_type: str, payload: bytes) -> str:
    """An image as the data URI draw.io stores in a style.

    Note the comma where a conforming data URI has ``;base64,``: a semicolon
    inside a style value would end the declaration, so draw.io defines its own
    spelling and restores the standard one when it renders. Anything writing a
    style by hand has to do the same, which is why this is a function rather
    than an f-string at each call site.
    """
    return f"data:{media_type},{base64.b64encode(payload).decode('ascii')}"


def icon_data_uri(theme: IconTheme, kind: str) -> str | None:
    """The picture ``theme`` draws ``kind`` with, inlined, or ``None``.

    ``None`` when the theme has no icon for the kind, when the file has gone
    missing between the theme being resolved and here, or when it is past
    :data:`MAX_ICON_BYTES`. None of the three is an error — the caller falls
    back to a coloured box and records why — because a partial theme is a
    legitimate thing to have and an exported diagram must not fail over one.

    SVG is preferred over PNG: the exported file is going to be opened in a
    vector editor and printed at whatever size the reader chooses, which is
    exactly the case a raster icon loses.
    """
    name = theme.file_for(kind, prefer=VECTOR_FIRST)
    if name is None:
        return None
    return _inline(theme.directory / name)


@lru_cache(maxsize=64)
def _inline(path: Path) -> str | None:
    """One icon file as a data URI, read at most once per export run.

    Cached because an inventory draws the same six kinds a hundred times, and
    base64-encoding a file per node would be the most expensive thing in the
    export by a wide margin.
    """
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        return None
    try:
        if path.stat().st_size > MAX_ICON_BYTES:
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    return data_uri(media_type, payload)
