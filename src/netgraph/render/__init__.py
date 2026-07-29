"""Rendering of a loaded inventory into graph output formats.

The pipeline has three stages, and each one is independently testable::

    graph = build_graph(inventory, layer=Layer.L1)   # resolve references
    graph = filter_graph(graph, FilterSpec(vlans={10}))
    payload = render(graph, "svg", RenderOptions(show_ips=False))

:func:`render` dispatches on the format name and always returns ``bytes``, so a
caller writing to a file or to stdout needs no per-format branching: text
formats are UTF-8 encoded, image formats come back as Graphviz produced them.
Use :func:`render_text` when the caller genuinely wants a string and has already
excluded the binary formats.

Every format is one entry in :data:`~netgraph.render.registry.RENDERERS`, and
the names, suffixes and format predicates here are derived from it rather than
listed a second time — see that module for how a backend is added.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from netgraph.errors import RenderError
from netgraph.render.aggregate import (
    AGGREGATE_ID_PREFIX,
    AGGREGATE_KIND,
    AggregateSpec,
    AggregateView,
    BundleMode,
    BundleView,
    aggregate_graph,
    bundle_links,
    collapse_namespaces,
    collapse_targets,
)
from netgraph.render.details import (
    DETAIL_OPTIONS,
    build_details,
    detail_text,
    namespace_text,
)
from netgraph.render.dot import IMAGE_FORMATS, render_dot, render_image, to_dot, to_image
from netgraph.render.graph import (
    SUBNET_ID_PREFIX,
    SUBNET_KIND,
    TUNNEL_ID_PREFIX,
    TUNNEL_KIND,
    Edge,
    EdgeKind,
    FilterSpec,
    Graph,
    Layer,
    Node,
    NodeType,
    PortView,
    Subnet,
    TunnelEnd,
    TunnelView,
    UnknownElementError,
    build_graph,
    filter_graph,
    is_routable_address,
    resolve_tunnels,
)
from netgraph.render.highlight import Highlight
from netgraph.render.html import PAGE_KIND, html_document, render_html, to_html
from netgraph.render.icons import (
    BUNDLED_THEMES,
    ICON_KINDS,
    IconTheme,
    icon_theme,
    theme_choices,
)
from netgraph.render.ids import ElementIds, element_ids
from netgraph.render.jsonexport import GRAPH_KIND, graph_to_dict, render_json, to_json
from netgraph.render.links import LINK_FIELDS, LinkTemplate
from netgraph.render.mermaid import (
    MERMAID_MAX_EDGES,
    mermaid_advisories,
    render_mermaid,
    to_mermaid,
)
from netgraph.render.options import RenderOptions
from netgraph.render.registry import (
    PAGE_CSP,
    RENDERERS,
    Renderer,
    content_security_policy_for,
    media_type_for,
    renderer_for,
    supports_highlight,
    supports_icons,
    supports_interaction,
    supports_layers,
)

__all__ = [
    "AGGREGATE_ID_PREFIX",
    "AGGREGATE_KIND",
    "BUNDLED_THEMES",
    "DETAIL_OPTIONS",
    "FORMATS",
    "GRAPH_KIND",
    "ICON_KINDS",
    "IMAGE_FORMATS",
    "LINK_FIELDS",
    "MERMAID_MAX_EDGES",
    "PAGE_CSP",
    "PAGE_KIND",
    "RENDERERS",
    "SUBNET_ID_PREFIX",
    "SUBNET_KIND",
    "TEXT_FORMATS",
    "TUNNEL_ID_PREFIX",
    "TUNNEL_KIND",
    "AggregateSpec",
    "AggregateView",
    "BundleMode",
    "BundleView",
    "Edge",
    "EdgeKind",
    "ElementIds",
    "FilterSpec",
    "Graph",
    "Highlight",
    "IconTheme",
    "Layer",
    "LinkTemplate",
    "Node",
    "NodeType",
    "PortView",
    "RenderOptions",
    "Renderer",
    "Subnet",
    "TunnelEnd",
    "TunnelView",
    "UnknownElementError",
    "advisories_for",
    "aggregate_graph",
    "build_details",
    "build_graph",
    "bundle_links",
    "collapse_namespaces",
    "collapse_targets",
    "content_security_policy_for",
    "detail_text",
    "element_ids",
    "filter_graph",
    "graph_to_dict",
    "html_document",
    "icon_theme",
    "is_binary_format",
    "is_routable_address",
    "media_type_for",
    "mermaid_advisories",
    "namespace_text",
    "render",
    "render_dot",
    "render_html",
    "render_image",
    "render_json",
    "render_layers",
    "render_mermaid",
    "render_text",
    "renderer_for",
    "resolve_tunnels",
    "suffix_for",
    "supports_highlight",
    "supports_icons",
    "supports_interaction",
    "supports_layers",
    "theme_choices",
    "to_dot",
    "to_html",
    "to_image",
    "to_json",
    "to_mermaid",
]

#: Every format ``netgraph render -f`` accepts, in help-text order.
FORMATS: Final[tuple[str, ...]] = tuple(RENDERERS)

#: Formats whose output is text, and so has a meaningful string form.
TEXT_FORMATS: Final[tuple[str, ...]] = tuple(
    name for name, renderer in RENDERERS.items() if renderer.is_text
)


def is_binary_format(format: str) -> bool:
    """Would ``format`` produce bytes that must not be printed to a terminal?

    SVG is text, but it is still an image format produced by Graphviz; it is
    safe on a terminal, so only the genuinely binary ones answer true. An
    unknown format answers ``False``: the caller that would print it has
    already been rejected by :func:`render`.
    """
    renderer = RENDERERS.get(format)
    return renderer is not None and renderer.binary


def suffix_for(format: str) -> str:
    """The conventional file extension for ``format``, e.g. ``.mmd``."""
    renderer = RENDERERS.get(format)
    return renderer.suffix if renderer is not None else f".{format}"


def advisories_for(format: str, *, nodes: int, edges: int) -> tuple[str, ...]:
    """What a user should know about rendering a graph this size as ``format``.

    Lets a front end warn about a backend's limits — Mermaid's edge ceiling
    today — without knowing which backend has any. An unknown format has
    nothing to say rather than raising: this is advisory, and the render call
    that follows is where a bad format is reported.
    """
    renderer = RENDERERS.get(format)
    return renderer.advise(nodes, edges) if renderer is not None else ()


def render_text(graph: Graph, format: str, options: RenderOptions | None = None) -> str:
    """Render ``graph`` in one of :data:`TEXT_FORMATS`.

    Raises:
        RenderError: ``format`` is unknown, or is not a text format.
    """
    return renderer_for(format).text(graph, options)


def render(graph: Graph, format: str, options: RenderOptions | None = None) -> bytes:
    """Render ``graph`` in any supported format, as bytes ready to be written.

    Raises:
        RenderError: ``format`` is unknown, or Graphviz is needed and missing.
    """
    return renderer_for(format).bytes(graph, options)


def render_layers(
    graphs: Sequence[Graph], format: str, options: RenderOptions | None = None
) -> bytes:
    """Render one output holding ``graphs``, one per layer.

    One graph is exactly :func:`render`, whatever the format; several need a
    format with somewhere to put them, which is what
    :func:`~netgraph.render.registry.supports_layers` answers and ``html`` is.

    Raises:
        RenderError: ``graphs`` is empty, ``format`` is unknown or holds one
            layer only, or Graphviz is needed and missing.
    """
    if not graphs:
        raise RenderError("nothing to render: no layer was selected")
    if len(graphs) == 1:
        return render(graphs[0], format, options)
    return renderer_for(format).document(graphs, options)
