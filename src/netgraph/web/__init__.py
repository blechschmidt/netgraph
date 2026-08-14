"""The interactive web interface: a YAML document stream in, a live diagram out.

``netgraph web`` opens a page with the stream in a text area on one side and the
rendering on the other. Editing re-renders; hovering a node or a link opens an
info box holding everything the diagram had no room for — every interface, its
addresses, its VLANs, and the links that terminate on it.

Six pieces, each usable on its own:

:mod:`~netgraph.web.preview`
    :func:`~netgraph.web.preview.render_source`, the ``parse → validate →
    render`` pass over a string. It never raises for anything the text can be
    wrong about, and it draws what resolved even when the stream was rejected —
    while it is being typed, most of it is.
:mod:`netgraph.render.details`
    The info-box records, keyed by the id the drawn element carries. They are
    the JSON export, so what a hover says and what ``netgraph render -f json``
    prints cannot drift apart — and they live in the renderer rather than here
    because ``netgraph render -f svg`` puts the same records in its tooltips.
:mod:`~netgraph.web.svgdoc`
    Turning the Graphviz SVG into a fragment that can be embedded in a live
    page, with everything that could execute or navigate removed.
:mod:`~netgraph.web.server`
    :class:`~netgraph.web.server.WebServer`, the routes over all of it,
    loopback-bound like every local server netgraph starts.
:mod:`~netgraph.web.events`
    :class:`~netgraph.web.events.EventBus`, the push channel an editing session
    announces itself on: numbered, revision-stamped, replayable from a bounded
    ring. It is an optimisation over the polled revision and never an authority,
    so a client that cannot hold a stream open loses latency and nothing else.
:mod:`~netgraph.web.presence`
    Who else has the same session open, what they have selected, and which files
    they have unsaved edits in. Advisory throughout — the revision and the
    content hash remain the only gates on a write.
:mod:`~netgraph.web.bindings`
    Every command the page has and the keys that reach it. Served at
    ``/api/bindings``, so the palette, the shortcut sheet and the ``keybindings``
    region of ``docs/commands/web.md`` are three views of one table.

The split is what makes the interesting half testable without a browser:
:func:`~netgraph.web.preview.render_source` takes a string and returns the
diagram, the records and the problems, and no HTTP is involved.
"""

from __future__ import annotations

from netgraph.render.details import DETAIL_OPTIONS, build_details
from netgraph.web.bindings import BINDINGS, SECTIONS, Binding
from netgraph.web.events import EVENT_NAMES, EVENTS_PATH, Event, EventBus
from netgraph.web.presence import Client, Presence
from netgraph.web.preview import (
    MAX_VLAN,
    Preview,
    RequestError,
    ViewOptions,
    graph_digest,
    render_source,
)
from netgraph.web.server import (
    ASSETS,
    BINDINGS_PATH,
    DEFAULT_PORT,
    MAX_SOURCE_BYTES,
    PRESENCE_PATH,
    RENDER_PATH,
    SOURCE_PATH,
    WebServer,
    asset,
)
from netgraph.web.svgdoc import prepare

__all__ = [
    "ASSETS",
    "BINDINGS",
    "BINDINGS_PATH",
    "DEFAULT_PORT",
    "DETAIL_OPTIONS",
    "EVENTS_PATH",
    "EVENT_NAMES",
    "MAX_SOURCE_BYTES",
    "MAX_VLAN",
    "PRESENCE_PATH",
    "RENDER_PATH",
    "SECTIONS",
    "SOURCE_PATH",
    "Binding",
    "Client",
    "Event",
    "EventBus",
    "Presence",
    "Preview",
    "RequestError",
    "ViewOptions",
    "WebServer",
    "asset",
    "build_details",
    "graph_digest",
    "prepare",
    "render_source",
]
