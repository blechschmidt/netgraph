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

Enumeration order is help-text order: the two Graphviz text/image families
first, then the two formats that need no external tool.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Final

from netgraph.errors import RenderError
from netgraph.render.dot import to_dot, to_image
from netgraph.render.graph import Graph
from netgraph.render.jsonexport import to_json
from netgraph.render.mermaid import mermaid_advisories, to_mermaid
from netgraph.render.options import RenderOptions

__all__ = [
    "DEFAULT_MEDIA_TYPE",
    "RENDERERS",
    "Renderer",
    "media_type_for",
    "renderer_for",
    "supports_icons",
]

#: What an unregistered format is served as: a download, never something a
#: browser will try to interpret. Only reachable if a caller invents a format
#: name, since ``--format`` is checked against :data:`RENDERERS` first.
DEFAULT_MEDIA_TYPE: Final = "application/octet-stream"

#: Produces the rendering as text (``dot``, ``mermaid``, ``json``).
TextBackend = Callable[[Graph, RenderOptions | None], str]
#: Produces the rendering as bytes (the Graphviz image formats).
BinaryBackend = Callable[[Graph, RenderOptions | None], bytes]
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

    @property
    def is_text(self) -> bool:
        """Can this format be produced as a string?"""
        return self.to_text is not None

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
    )


def _image_renderer(name: str, description: str, media_type: str, *, binary: bool) -> Renderer:
    """A Graphviz image backend. ``to_image`` validates the format itself."""
    return Renderer(
        name=name,
        description=description,
        suffix=f".{name}",
        media_type=media_type,
        binary=binary,
        to_bytes=partial(to_image, format=name),
        supports_icons=True,
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
            ),
            _image_renderer("svg", "SVG image, via Graphviz", "image/svg+xml", binary=False),
            _image_renderer("png", "PNG image, via Graphviz", "image/png", binary=True),
            _image_renderer("pdf", "PDF document, via Graphviz", "application/pdf", binary=True),
            _text_renderer(
                "mermaid",
                "Mermaid flowchart, for embedding in Markdown",
                ".mmd",
                "text/plain; charset=utf-8",
                to_mermaid,
                mermaid_advisories,
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


def _names(*, text_only: bool = False) -> str:
    """Registered format names, for an error message."""
    return ", ".join(
        name for name, renderer in RENDERERS.items() if renderer.is_text or not text_only
    )
