"""The HTTP face of ``netgraph web``: five routes, none of them a file name.

    GET  /            the page
    GET  /app.css     its style sheet
    GET  /app.js      its client
    GET  /api/source  the text the editor opens with
    POST /api/render  a document stream in, a diagram and its records out

The only route that accepts input is the last one, and what it accepts is a
JSON object holding the YAML the user typed plus the view options the page
offers. Nothing in that body ever becomes a path, a command-line argument or a
file: it is parsed in memory by :func:`~netgraph.web.preview.render_source` and
the answer is built from what it produced.

The rest of being a local server — loopback by default, the ``Host`` header
check that defeats DNS rebinding, the response headers — is
:mod:`netgraph.httpserve`'s, and is the same here as for ``netgraph watch
--serve``.

Two limits keep an interactive endpoint from being a way to hang the machine it
runs on: :data:`MAX_SOURCE_BYTES` bounds a request body, and Graphviz is given
the timeout it always has. Neither is a security boundary — the server answers
to this machine only — they are there so that pasting the wrong file into the
editor produces a refusal rather than a stall.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import cache
from http import HTTPStatus
from importlib import resources
from typing import Any, Final

from netgraph.httpserve import (
    DEFAULT_HOST,
    BackgroundServer,
    LocalHandler,
    bind,
    is_loopback,
)
from netgraph.render import IconTheme
from netgraph.web.preview import Preview, RequestError, ViewOptions, render_source

__all__ = [
    "ASSETS",
    "DEFAULT_PORT",
    "MAX_SOURCE_BYTES",
    "RENDER_PATH",
    "SOURCE_PATH",
    "WebServer",
    "asset",
]

#: Default port of ``netgraph web``. One above the preview's, so a watch run
#: and an editing session can be open at the same time.
DEFAULT_PORT: Final = 8081

SOURCE_PATH: Final = "/api/source"
RENDER_PATH: Final = "/api/render"

#: Largest document stream the editor may post, in bytes. An inventory this
#: size is one to keep in files and open with ``netgraph watch``; a browser
#: textarea is not the tool for it, and rendering it on every keystroke would
#: not be interactive anyway.
MAX_SOURCE_BYTES: Final = 1_000_000

#: The static files, by request path: ``(package resource, content type)``.
ASSETS: Final[dict[str, tuple[str, str]]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


@cache
def asset(name: str) -> bytes:
    """Read one of the files in ``netgraph/web/assets``.

    Cached, because the page is static: the process reads each file once and
    answers from memory afterwards. The name comes from :data:`ASSETS` and
    never from a request, so no path a client sends can reach this.
    """
    return (resources.files("netgraph.web") / "assets" / name).read_bytes()


class _Handler(LocalHandler):
    """The five routes. Everything they answer with is built in memory."""

    server_version = "netgraph-web"

    # Bound onto a subclass by :meth:`WebServer.create`.
    source: str
    icons: IconTheme | None
    on_render: Callable[[Preview], None]

    def handle_request(self, method: str, *, body: bool) -> None:
        path = self.path.split("?", 1)[0]
        if method in ("GET", "HEAD"):
            self._get(path, body=body)
        elif method == "POST":
            self._post(path)
        else:  # pragma: no cover - the base class routes nothing else here
            self.send_text(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "expected GET, HEAD or POST",
                allow="GET, HEAD, POST",
            )

    # -- GET -------------------------------------------------------------

    def _get(self, path: str, *, body: bool) -> None:
        if path in ASSETS:
            name, content_type = ASSETS[path]
            self.send_payload(HTTPStatus.OK, asset(name), content_type, body=body)
        elif path == SOURCE_PATH:
            payload = json.dumps({"source": self.source}).encode()
            self.send_payload(HTTPStatus.OK, payload, "application/json", body=body)
        elif path == RENDER_PATH:
            self.send_text(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "post a document stream to render it",
                body=body,
                allow="POST",
            )
        else:
            self.send_text(HTTPStatus.NOT_FOUND, "not found; the editor is at /", body=body)

    # -- POST ------------------------------------------------------------

    def _post(self, path: str) -> None:
        if path != RENDER_PATH:
            self.send_text(HTTPStatus.NOT_FOUND, f"nothing to post to at {path}")
            return
        try:
            payload = self._read_json()
            source = payload.get("source", "")
            if not isinstance(source, str):
                raise RequestError("'source' must be the YAML document stream, as a string")
            view = ViewOptions.from_request(payload, icons=self.icons)
        except RequestError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        preview = render_source(source, view)
        self.on_render(preview)
        self.send_payload(
            HTTPStatus.OK,
            json.dumps(preview.to_dict()).encode(),
            "application/json",
        )

    def _read_json(self) -> dict[str, Any]:
        """The request body as a JSON object.

        Raises:
            RequestError: The body is missing, too large, not JSON, or not an
                object. Every one of them is the client's mistake, and the
                message says which so that a hand-written request can be fixed.
        """
        header = self.headers.get("Content-Length")
        if header is None:
            raise RequestError("a Content-Length is required")
        try:
            length = int(header)
        except ValueError:
            raise RequestError(f"Content-Length {header!r} is not a number") from None
        if length < 0:
            raise RequestError("Content-Length must not be negative")
        if length > MAX_SOURCE_BYTES:
            raise RequestError(
                f"the document stream is {length} bytes; this editor renders up to "
                f"{MAX_SOURCE_BYTES}. Keep an inventory this size in files and open it "
                "with 'netgraph watch --serve'"
            )
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(f"the request body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RequestError("the request body must be a JSON object")
        return payload

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        """Refuse in the shape the page expects, so it can show the reason."""
        self.send_payload(
            status,
            json.dumps({"status": "failed", "message": message}).encode(),
            "application/json",
        )


class WebServer(BackgroundServer):
    """A running web interface, started and stopped explicitly.

    Use it as a context manager::

        with WebServer.create(source="") as web:
            print(web.url)
    """

    thread_name = "netgraph-web"

    @classmethod
    def create(
        cls,
        *,
        source: str = "",
        icons: IconTheme | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        log: Callable[[str], None] = lambda message: None,
        on_render: Callable[[Preview], None] = lambda preview: None,
    ) -> WebServer:
        """Bind the interface without answering requests yet.

        Args:
            source: What the editor opens with.
            icons: Icon theme for the diagram. Chosen here rather than by the
                browser, because it names a directory on this machine.
            host: Address to bind. Loopback unless the caller says otherwise.
            port: TCP port, or ``0`` to let the operating system choose.
            log: Receives access-log lines.
            on_render: Called with the outcome of every render, for a status
                line on the terminal that started the server.

        Raises:
            ServeError: The address cannot be bound.
        """
        handler = type(
            "_BoundHandler",
            (_Handler,),
            {
                "source": source,
                "icons": icons,
                "on_render": staticmethod(on_render),
                "loopback_only": is_loopback(host),
                "log": staticmethod(log),
            },
        )
        return cls(bind(handler, host=host, port=port, subject="the web interface"), host=host)
