"""The interactive web interface: a YAML document stream in, a live diagram out.

``netgraph web`` opens a page with the stream in a text area on one side and the
rendering on the other. Editing re-renders; hovering a node or a link opens an
info box holding everything the diagram had no room for — every interface, its
addresses, its VLANs, and the links that terminate on it.

Four pieces, each usable on its own:

:mod:`~netgraph.web.preview`
    :func:`~netgraph.web.preview.render_source`, the ``parse → validate →
    render`` pass over a string. It never raises for anything the text can be
    wrong about, and it draws what resolved even when the stream was rejected —
    while it is being typed, most of it is.
:mod:`~netgraph.web.details`
    The info-box records, keyed by the id the drawn element carries. They are
    the JSON export, so what a hover says and what ``netgraph render -f json``
    prints cannot drift apart.
:mod:`~netgraph.web.svgdoc`
    Turning the Graphviz SVG into a fragment that can be embedded in a live
    page, with everything that could execute or navigate removed.
:mod:`~netgraph.web.server`
    :class:`~netgraph.web.server.WebServer`, the five routes over all of it,
    loopback-bound like every local server netgraph starts.

The split is what makes the interesting half testable without a browser:
:func:`~netgraph.web.preview.render_source` takes a string and returns the
diagram, the records and the problems, and no HTTP is involved.
"""

from __future__ import annotations

from netgraph.web.details import DETAIL_OPTIONS, build_details
from netgraph.web.preview import (
    MAX_VLAN,
    Preview,
    RequestError,
    ViewOptions,
    render_source,
)
from netgraph.web.server import (
    ASSETS,
    DEFAULT_PORT,
    MAX_SOURCE_BYTES,
    RENDER_PATH,
    SOURCE_PATH,
    WebServer,
    asset,
)
from netgraph.web.svgdoc import prepare

__all__ = [
    "ASSETS",
    "DEFAULT_PORT",
    "DETAIL_OPTIONS",
    "MAX_SOURCE_BYTES",
    "MAX_VLAN",
    "RENDER_PATH",
    "SOURCE_PATH",
    "Preview",
    "RequestError",
    "ViewOptions",
    "WebServer",
    "asset",
    "build_details",
    "prepare",
    "render_source",
]
