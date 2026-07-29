"""The renderer registry — one entry per output format.

Every fact a caller needs about an output format lives on its
:class:`Renderer`: how to produce it, what to call the file, what content type
to serve it as, whether it is safe on a terminal, and whether there is anything
to warn about at a given graph size. :data:`RENDERERS` maps the format name to
that record, and it is the single place a new backend is declared.

The point is that no front end branches on a format name. Adding an ASCII-art
or GraphML backend means writing the module and adding one entry here; the
``-f`` choices, the file extension, the preview server's content type and the
size warnings all follow. Anything a front end has to know is either a field on
:class:`Renderer` or a bug in this design.

Enumeration order is help-text order: the Graphviz family first — the source,
then what it lays out, richest first — then the two formats that need no
external tool.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Final

from netgraph.errors import RenderError
from netgraph.render.dot import to_dot, to_image
from netgraph.render.graph import Graph
from netgraph.render.html import html_document, to_html
from netgraph.render.jsonexport import to_json
from netgraph.render.mermaid import mermaid_advisories, to_mermaid
from netgraph.render.options import RenderOptions

__all__ = [
    "DEFAULT_MEDIA_TYPE",
    "RENDERERS",
    "Renderer",
    "content_security_policy_for",
    "draws_racks",
    "media_type_for",
    "rack_formats",
    "renderer_for",
    "supports_highlight",
    "supports_icons",
    "supports_interaction",
    "supports_layers",
]

#: What an unregistered format is served as: a download, never something a
#: browser will try to interpret. Only reachable if a caller invents a format
#: name, since ``--format`` is checked against :data:`RENDERERS` first.
DEFAULT_MEDIA_TYPE: Final = "application/octet-stream"

#: What a self-contained page has to be served under, when a local server
#: serves one at all (``netgraph watch -f html --serve``).
#:
#: The page is *meant* to be opened from a file, where no server sends a policy
#: and the one in its own ``<meta>`` — a hash per inline block — is the whole
#: of it. Serving it under the default policy would apply a second, stricter
#: one on top: ``script-src 'self'`` refuses an inline block however it is
#: hashed, and ``frame-ancestors 'none'`` refuses the preview's own embedding
#: of it. This says "inline is allowed here", and the document's hashes then
#: decide *which* inline blocks, so the two together are no weaker than the
#: page on its own.
PAGE_CSP: Final = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self'"
)

#: Produces the rendering as text (``dot``, ``mermaid``, ``json``).
TextBackend = Callable[[Graph, RenderOptions | None], str]
#: Produces the rendering as bytes (the Graphviz image formats).
BinaryBackend = Callable[[Graph, RenderOptions | None], bytes]
#: Produces one output holding several layers at once (``html``).
DocumentBackend = Callable[[Sequence[Graph], RenderOptions | None], str]
#: Given ``(nodes, edges)``, anything a user should know before the output
#: reaches its consumer. Empty when there is nothing to say.
Advisor = Callable[[int, int], tuple[str, ...]]


def no_advisories(nodes: int, edges: int) -> tuple[str, ...]:
    """The default advisor: this backend has no size-dependent limits."""
    del nodes, edges
    return ()


@dataclass(frozen=True, slots=True)
class Renderer:
    """One output format, and everything a front end needs to know about it."""

    #: Format name, as passed to ``--format``. The key in :data:`RENDERERS`.
    name: str
    #: One clause for help text, e.g. ``"Graphviz DOT source"``.
    description: str
    #: Conventional file extension, leading dot included.
    suffix: str
    #: Content type for serving the rendering over HTTP.
    media_type: str
    #: Must not be written to a terminal. SVG is an image but is text on the
    #: wire, so this is narrower than "not a text format".
    binary: bool
    #: The backend, producing bytes ready to be written.
    to_bytes: BinaryBackend
    #: The backend as text, when the format has a meaningful string form.
    #: ``None`` for the image formats, whose bytes are Graphviz's own output.
    to_text: TextBackend | None = None
    #: Size-dependent warnings; see :func:`no_advisories`.
    advise: Advisor = no_advisories
    #: Can this backend draw an icon theme? Mermaid and JSON cannot, so a front
    #: end asks the registry rather than testing the format name itself.
    supports_icons: bool = False
    #: Does this backend carry tooltips, links and element ids into its output?
    #: True of ``dot``, which writes the attributes, and of ``svg``, which is
    #: the one laid-out format with somewhere to put them: a PNG and a PDF are
    #: pictures, and Mermaid and JSON have an interaction model of their own.
    interactive: bool = False
    #: Renders several layers into one output, or ``None`` when this format
    #: holds one view of the network and no more. Only ``html`` has a switcher
    #: to put them behind; a second layer in a DOT file or a PNG would be a
    #: second document glued to the first.
    to_document: DocumentBackend | None = None
    #: What this format's output must be served under when the default policy
    #: (:data:`~netgraph.httpserve.DEFAULT_CSP`) is too strict for it, or
    #: ``None`` to serve it under the default. A self-contained page carries its
    #: own hashed policy in a ``<meta>`` and is embedded by the watch preview,
    #: neither of which the default allows; every other format is inert data
    #: and needs nothing.
    csp: str | None = None
    #: Can this backend express a rack elevation
    #: (:attr:`~netgraph.render.graph.Layer.RACK`)? An elevation is a *grid* —
    #: one row per rack unit, empty units included — rather than a topology, so
    #: it needs a label vocabulary that can hold a table. Graphviz has one and
    #: JSON is structure all the way down; a Mermaid flowchart node is a caption
    #: with no rows, so it says so rather than emitting a box that silently
    #: leaves out the empty units.
    draws_racks: bool = True
    #: Can this backend draw part of the graph emphasised and the rest dimmed
    #: (:mod:`netgraph.render.highlight`)? Emphasis is *visual weight* — a bold
    #: outline, a dimmed fill — so it needs a backend that decides how things
    #: look. Mermaid and JSON have no such vocabulary, so ``netgraph path
    #: --highlight`` does not offer them rather than silently ignoring the flag.
    supports_highlight: bool = False

    @property
    def is_text(self) -> bool:
        """Can this format be produced as a string?"""
        return self.to_text is not None

    @property
    def holds_layers(self) -> bool:
        """Can one output of this format hold more than one layer?"""
        return self.to_document is not None

    def document(self, graphs: Sequence[Graph], options: RenderOptions | None = None) -> bytes:
        """Render ``graphs`` — one per layer — as a single output.

        Raises:
            RenderError: This format holds one layer only.
        """
        if self.to_document is None:
            raise RenderError(
                f"{self.name!r} output holds one layer; the formats that hold several are "
                f"{_names(layers_only=True)}"
            )
        return self.to_document(graphs, options).encode("utf-8")

    def text(self, graph: Graph, options: RenderOptions | None = None) -> str:
        """Render ``graph`` as text.

        Raises:
            RenderError: This format has no text form.
        """
        if self.to_text is None:
            raise RenderError(
                f"{self.name!r} is not a text format; expected one of {_names(text_only=True)}"
            )
        return self.to_text(graph, options)

    def bytes(self, graph: Graph, options: RenderOptions | None = None) -> bytes:
        """Render ``graph`` as bytes ready to be written to a file or stream."""
        return self.to_bytes(graph, options)


def _text_renderer(
    name: str,
    description: str,
    suffix: str,
    media_type: str,
    backend: TextBackend,
    advise: Advisor = no_advisories,
    *,
    supports_icons: bool = False,
    interactive: bool = False,
    supports_highlight: bool = False,
    to_document: DocumentBackend | None = None,
    csp: str | None = None,
    draws_racks: bool = True,
) -> Renderer:
    """A text backend, with its UTF-8 encoding wired up once."""

    def to_bytes(graph: Graph, options: RenderOptions | None = None) -> bytes:
        return backend(graph, options).encode("utf-8")

    return Renderer(
        name=name,
        description=description,
        suffix=suffix,
        media_type=media_type,
        binary=False,
        to_bytes=to_bytes,
        to_text=backend,
        advise=advise,
        supports_icons=supports_icons,
        interactive=interactive,
        supports_highlight=supports_highlight,
        to_document=to_document,
        csp=csp,
        draws_racks=draws_racks,
    )


def _image_renderer(
    name: str, description: str, media_type: str, *, binary: bool, interactive: bool = False
) -> Renderer:
    """A Graphviz image backend. ``to_image`` validates the format itself.

    Every one of them lays the graph out through the DOT backend, so all three
    inherit its icon and highlight support without being told.
    """
    return Renderer(
        name=name,
        description=description,
        suffix=f".{name}",
        media_type=media_type,
        binary=binary,
        to_bytes=partial(to_image, format=name),
        supports_icons=True,
        interactive=interactive,
        supports_highlight=True,
    )


#: Every format ``netgraph render -f`` accepts, keyed by name, in help order.
RENDERERS: Final[Mapping[str, Renderer]] = MappingProxyType(
    {
        renderer.name: renderer
        for renderer in (
            _text_renderer(
                "dot",
                "Graphviz DOT source",
                ".dot",
                "text/vnd.graphviz; charset=utf-8",
                to_dot,
                supports_icons=True,
                interactive=True,
                supports_highlight=True,
            ),
            _image_renderer(
                "svg",
                "SVG image, via Graphviz",
                "image/svg+xml",
                binary=False,
                interactive=True,
            ),
            _text_renderer(
                "html",
                "self-contained interactive page, via Graphviz",
                ".html",
                "text/html; charset=utf-8",
                to_html,
                supports_icons=True,
                interactive=True,
                supports_highlight=True,
                to_document=html_document,
                csp=PAGE_CSP,
            ),
            _image_renderer("png", "PNG image, via Graphviz", "image/png", binary=True),
            _image_renderer("pdf", "PDF document, via Graphviz", "application/pdf", binary=True),
            _text_renderer(
                "mermaid",
                "Mermaid flowchart, for embedding in Markdown",
                ".mmd",
                "text/plain; charset=utf-8",
                to_mermaid,
                mermaid_advisories,
                draws_racks=False,
            ),
            _text_renderer(
                "json",
                "node-link JSON, for downstream tooling",
                ".json",
                "application/json",
                to_json,
            ),
        )
    }
)


def supports_icons(format: str) -> bool:
    """Would ``format`` draw an icon theme if one were set?

    An unknown format answers ``False``; the render call that follows is where
    a bad format name is reported.
    """
    renderer = RENDERERS.get(format)
    return renderer is not None and renderer.supports_icons


def supports_highlight(format: str) -> bool:
    """Would ``format`` draw a :class:`~netgraph.render.highlight.Highlight`?

    An unknown format answers ``False``; the render call that follows is where
    a bad format name is reported.
    """
    renderer = RENDERERS.get(format)
    return renderer is not None and renderer.supports_highlight


def supports_layers(format: str) -> bool:
    """Can one output of ``format`` hold more than one layer?

    An unknown format answers ``False``, like its neighbours here: the render
    call that follows is where a bad format name is reported.
    """
    renderer = RENDERERS.get(format)
    return renderer is not None and renderer.holds_layers


def draws_racks(format: str) -> bool:
    """Can ``format`` express a rack elevation (``--layer rack``)?

    An unknown format answers ``False``, like its neighbours here: the render
    call that follows is where a bad format name is reported.
    """
    renderer = RENDERERS.get(format)
    return renderer is not None and renderer.draws_racks


def rack_formats() -> tuple[str, ...]:
    """The formats that can, in help-text order, for a diagnostic."""
    return tuple(name for name, renderer in RENDERERS.items() if renderer.draws_racks)


def content_security_policy_for(format: str) -> str | None:
    """The policy ``format``'s output must be served under, if it needs its own.

    ``None`` — every format but ``html`` — means the server's default is right.
    """
    renderer = RENDERERS.get(format)
    return renderer.csp if renderer is not None else None


def supports_interaction(format: str) -> bool:
    """Would ``format`` carry tooltips, links and element ids into its output?

    An unknown format answers ``False``, for the same reason
    :func:`supports_icons` does: the render call that follows is where a bad
    format name is reported.
    """
    renderer = RENDERERS.get(format)
    return renderer is not None and renderer.interactive


def renderer_for(format: str) -> Renderer:
    """The renderer registered for ``format``.

    Raises:
        RenderError: No such format.
    """
    try:
        return RENDERERS[format]
    except KeyError:
        raise RenderError(f"unknown output format {format!r}; expected one of {_names()}") from None


def media_type_for(format: str) -> str:
    """The content type ``format`` should be served as.

    Unlike :func:`renderer_for` this does not raise: a server that cannot name
    the format is exactly the case :data:`DEFAULT_MEDIA_TYPE` exists for.
    """
    renderer = RENDERERS.get(format)
    return renderer.media_type if renderer is not None else DEFAULT_MEDIA_TYPE


def _names(*, text_only: bool = False, layers_only: bool = False) -> str:
    """Registered format names, for an error message."""
    return ", ".join(
        name
        for name, renderer in RENDERERS.items()
        if (renderer.is_text or not text_only) and (renderer.holds_layers or not layers_only)
    )
