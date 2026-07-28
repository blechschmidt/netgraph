"""A local HTTP preview of the live render, with a page that reloads itself.

The server exists so that ``netgraph watch --serve`` can show a diagram that
updates as the inventory is edited. It is a *development* server and behaves
like one, with three deliberate restrictions:

* **It binds to 127.0.0.1 unless told otherwise.** An inventory describes
  internal network topology — addresses, VLANs, what is plugged into what — so
  the default must not put it on the network. ``--host`` is the explicit act of
  publishing it.
* **It serves memory, never the filesystem.** There are five fixed routes and
  no path is ever turned into a file name, so no request can reach a document
  the user did not ask to render.
* **It answers GET and HEAD only,** under a strict ``Content-Security-Policy``
  and with a ``Host`` header check that keeps a loopback-bound preview from
  being reached through a rebound DNS name.

The page polls :data:`STATUS_PATH` once a second and swaps the diagram in when
the revision changes. Polling rather than server-sent events keeps the client a
dozen lines of dependency-free JavaScript.
"""

from __future__ import annotations

import html
import json
import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any, Final
from urllib.parse import urlsplit

from netgraph.errors import NetgraphError
from netgraph.render import RENDERERS, media_type_for, suffix_for
from netgraph.watch.pipeline import LiveRender, Snapshot

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "STATUS_PATH",
    "PreviewServer",
    "ServeError",
    "describe_exposure",
    "is_loopback",
    "status_document",
]

#: Loopback, always. Overriding this is an explicit decision the user makes.
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8080

STATUS_PATH: Final = "/status.json"
_SCRIPT_PATH: Final = "/watch.js"
_STYLE_PATH: Final = "/watch.css"

#: How often the page asks whether the render has moved on, in milliseconds.
_POLL_INTERVAL_MS: Final = 1_000

#: Formats shown as text in a ``<pre>`` rather than as an image. Taken from the
#: renderer registry, so a text backend added later needs no change here.
_TEXTUAL: Final[frozenset[str]] = frozenset(
    name for name, renderer in RENDERERS.items() if renderer.is_text
)

#: Bind addresses that mean "every interface", and the address on this machine
#: that actually reaches them. Used only to print a URL a browser can open.
_WILDCARDS: Final[dict[str, str]] = {"0.0.0.0": "127.0.0.1", "::": "::1", "[::]": "::1"}


class ServeError(NetgraphError):
    """Raised when the preview server cannot be started."""

    exit_code = 6


def is_loopback(host: str) -> bool:
    """Does ``host`` name only this machine?

    Used to decide whether exposing the preview needs a warning, so an
    unresolvable or unrecognised name answers ``False``: the safe assumption
    about a name we cannot classify is that it is reachable from elsewhere.
    """
    name = host.strip().strip("[]")
    if name in ("localhost", ""):
        return True
    try:
        return ip_address(name).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Static assets
# --------------------------------------------------------------------------- #

_STYLE = """\
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: Canvas;
  color: CanvasText;
}
header {
  position: sticky;
  top: 0;
  display: flex;
  gap: .75rem;
  align-items: baseline;
  flex-wrap: wrap;
  padding: .5rem .75rem;
  border-bottom: 1px solid rgba(128, 128, 128, .35);
  background: Canvas;
}
header .state { font-weight: 700; text-transform: uppercase; letter-spacing: .05em; }
header.ok .state { color: #1a7f37; }
header.invalid .state, header.failed .state { color: #cf222e; }
header.pending .state { color: #9a6700; }
header .stamp { opacity: .6; }
header .stale { color: #9a6700; }
main { padding: .75rem; overflow: auto; }
main img, main object { max-width: 100%; }
main object { width: 100%; height: 80vh; }
pre { white-space: pre-wrap; word-break: break-word; margin: 0; }
#problems { padding: 0 .75rem .75rem; }
#problems:empty { display: none; }
#problems li { list-style: none; }
#problems ul { margin: 0; padding: 0; }
.empty { opacity: .6; padding: 2rem .75rem; }
"""

#: The polling client. ``__POLL_MS__`` and ``__STATUS__`` are substituted below
#: rather than interpolated: JavaScript is mostly braces, and every format
#: syntax Python offers would need them escaped.
_SCRIPT_TEMPLATE = """\
(function () {
  "use strict";
  var poll = __POLL_MS__;
  var revision = -1;
  var payloadRevision = -1;
  var view = document.getElementById("view");
  var header = document.getElementById("status");
  var stamp = header.querySelector(".stamp");
  var state = header.querySelector(".state");
  var detail = header.querySelector(".detail");
  var stale = header.querySelector(".stale");
  var problems = document.getElementById("problems");

  function refreshPayload(rev) {
    var target = view.querySelector("[data-payload]");
    if (!target) { return; }
    var url = target.getAttribute("data-payload") + "?rev=" + rev;
    if (target.tagName === "PRE") {
      fetch(url, { cache: "no-store" })
        .then(function (r) { return r.text(); })
        .then(function (text) { target.textContent = text; })
        .catch(function () {});
    } else if (target.tagName === "OBJECT") {
      target.setAttribute("data", url);
    } else {
      target.setAttribute("src", url);
    }
  }

  function apply(status) {
    if (status.revision === revision) { return; }
    revision = status.revision;
    header.className = status.status;
    stamp.textContent = status.stamp;
    state.textContent = status.status;
    detail.textContent = status.message;
    stale.textContent = status.stale ? "(showing the last good render)" : "";
    problems.innerHTML = "";
    if (status.problems.length) {
      var list = document.createElement("ul");
      status.problems.forEach(function (line) {
        var item = document.createElement("li");
        item.textContent = line;
        list.appendChild(item);
      });
      problems.appendChild(list);
    }
    if (status.hasPayload && status.payloadRevision !== payloadRevision) {
      payloadRevision = status.payloadRevision;
      refreshPayload(payloadRevision);
    }
    document.title = status.status + " \\u2014 " + document.title.replace(/^\\S+ \\u2014 /, "");
  }

  function tick() {
    fetch("__STATUS__", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(apply)
      .catch(function () {})
      .then(function () { window.setTimeout(tick, poll); });
  }

  tick();
})();
"""

_SCRIPT: Final = _SCRIPT_TEMPLATE.replace("__POLL_MS__", str(_POLL_INTERVAL_MS)).replace(
    "__STATUS__", STATUS_PATH
)


def _page(title: str, payload_path: str, output_format: str) -> str:
    """The wrapper page. Everything dynamic on it is filled in by the script.

    Which viewer suits a format is read off its registry entry rather than
    matched on its name: text goes in a ``<pre>``, an ``image/*`` media type in
    an ``<img>``, and anything else — a PDF today — in an ``<object>``, which
    is the element that defers to whatever plugin the browser has.
    """
    media_type = media_type_for(output_format)
    if output_format in _TEXTUAL:
        viewer = f'<pre data-payload="{html.escape(payload_path, quote=True)}"></pre>'
    elif not media_type.startswith("image/"):
        viewer = (
            f'<object data-payload="{html.escape(payload_path, quote=True)}" '
            f'type="{html.escape(media_type, quote=True)}"></object>'
        )
    else:
        viewer = (
            f'<img data-payload="{html.escape(payload_path, quote=True)}" alt="network diagram">'
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>netgraph — {html.escape(title)}</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="{_STYLE_PATH}">
</head>
<body>
<header id="status" class="pending">
  <span class="stamp"></span>
  <span class="state">pending</span>
  <span class="detail">waiting for the first render</span>
  <span class="stale"></span>
</header>
<main id="view">{viewer}</main>
<div id="problems"></div>
<script src="{_SCRIPT_PATH}"></script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    """Five fixed routes over the live render. No request touches a file."""

    server_version = "netgraph-watch"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Bound onto a subclass by :meth:`PreviewServer.create`.
    live: LiveRender
    page: bytes
    payload_path: str
    output_format: str
    loopback_only: bool
    log: Callable[[str], None]

    # -- plumbing --------------------------------------------------------

    def version_string(self) -> str:
        """Announce the server without the Python version underneath it."""
        return self.server_version

    def log_message(self, format: str, *args: Any) -> None:
        """Route the access log through the caller instead of stderr."""
        self.log(format % args)

    def log_error(self, format: str, *args: Any) -> None:
        # Errors here are ordinary client behaviour (a reload mid-response);
        # they are notes for a verbose run, not warnings for everybody.
        self.log_message(format, *args)

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:
        self._respond(body=True)

    def do_HEAD(self) -> None:
        self._respond(body=False)

    def _respond(self, *, body: bool) -> None:
        if not self._host_is_allowed():
            self._send(
                HTTPStatus.MISDIRECTED_REQUEST,
                b"this preview is bound to loopback and only answers to localhost\n",
                "text/plain; charset=utf-8",
                body=body,
            )
            return

        path = urlsplit(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, self.page, "text/html; charset=utf-8", body=body)
        elif path == _STYLE_PATH:
            self._send(HTTPStatus.OK, _STYLE.encode(), "text/css; charset=utf-8", body=body)
        elif path == _SCRIPT_PATH:
            self._send(HTTPStatus.OK, _SCRIPT.encode(), "text/javascript; charset=utf-8", body=body)
        elif path == STATUS_PATH:
            payload = json.dumps(status_document(self.live.snapshot())).encode()
            self._send(HTTPStatus.OK, payload, "application/json", body=body)
        elif path == self.payload_path:
            self._send_payload(body=body)
        else:
            self._send(
                HTTPStatus.NOT_FOUND,
                b"not found; the preview serves / only\n",
                "text/plain; charset=utf-8",
                body=body,
            )

    def _send_payload(self, *, body: bool) -> None:
        snapshot = self.live.snapshot()
        if snapshot.payload is None:
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                b"nothing has rendered yet\n",
                "text/plain; charset=utf-8",
                body=body,
            )
            return
        # An unregistered format is served as a download, so that a browser
        # cannot be talked into interpreting it as something else.
        self._send(HTTPStatus.OK, snapshot.payload, media_type_for(self.output_format), body=body)

    def _host_is_allowed(self) -> bool:
        """Reject a request that reached a loopback preview under another name.

        Without this, any web page could point a hostname it controls at
        127.0.0.1 and read the diagram — and with it the topology — out of the
        user's browser. A preview the user chose to publish with ``--host``
        makes no such promise and is not checked.
        """
        if not self.loopback_only:
            return True
        header = self.headers.get("Host", "")
        if not header:
            # HTTP/1.0 clients may omit it; they cannot be rebinding victims.
            return True
        hostname = header.rsplit(":", 1)[0] if _has_port(header) else header
        return is_loopback(hostname)

    def _send(self, status: HTTPStatus, payload: bytes, content_type: str, *, body: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Everything the page needs comes from this server; nothing else is
        # allowed to load, and no rendered document may frame or be framed.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self' data:; object-src 'self'; "
            "script-src 'self'; style-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if body:
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)


def _has_port(header: str) -> bool:
    """Does a ``Host`` header carry a ``:port`` suffix?

    ``[::1]:8080`` has one and ``::1`` — which a client should have bracketed
    but may not have — does not.
    """
    if header.startswith("["):
        return header.rfind("]") < header.rfind(":")
    return header.count(":") == 1


def status_document(snapshot: Snapshot) -> dict[str, Any]:
    """The JSON the page polls. Kept flat and small; it is fetched every second."""
    return {
        "revision": snapshot.revision,
        "payloadRevision": snapshot.payload_revision,
        "status": str(snapshot.status),
        "message": snapshot.message,
        "stamp": snapshot.stamp,
        "problems": list(snapshot.problems),
        "format": snapshot.output_format,
        "hasPayload": snapshot.payload is not None,
        "stale": snapshot.stale,
    }


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # A preview restarted in a loop must not trip over its own TIME_WAIT sockets.
    allow_reuse_address = True


class PreviewServer:
    """A running preview, started and stopped explicitly.

    Use it as a context manager::

        with PreviewServer.create(live, title="inventory") as preview:
            print(preview.url)
    """

    def __init__(self, server: _Server, *, host: str) -> None:
        self._server = server
        self._thread: threading.Thread | None = None
        self.host = host
        self.port: int = server.server_address[1]

    @classmethod
    def create(
        cls,
        live: LiveRender,
        *,
        title: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        log: Callable[[str], None] = lambda message: None,
    ) -> PreviewServer:
        """Bind a preview server without starting to answer requests yet.

        Args:
            live: State every response is derived from.
            title: Shown in the page title; the inventory path, normally.
            host: Address to bind. Loopback unless the caller says otherwise.
            port: TCP port, or ``0`` to let the operating system choose.
            log: Receives access-log lines.

        Raises:
            ServeError: The address cannot be bound.
        """
        output_format = live.snapshot().output_format
        payload_path = f"/render{suffix_for(output_format)}"

        # ``BaseHTTPRequestHandler`` is instantiated per request with a fixed
        # signature, so its configuration has to live on the class. Building a
        # subclass per server — rather than mutating ``_Handler`` — keeps two
        # concurrent previews independent.
        handler = type(
            "_BoundHandler",
            (_Handler,),
            {
                "live": live,
                "page": _page(title, payload_path, output_format).encode(),
                "payload_path": payload_path,
                "output_format": output_format,
                "loopback_only": is_loopback(host),
                "log": staticmethod(log),
            },
        )
        server_class = type("_BoundServer", (_Server,), {"address_family": _address_family(host)})
        try:
            server = server_class((host, port), handler)
        except OSError as exc:
            raise ServeError(
                f"cannot serve the preview on {_authority(host, port)}: {exc.strerror or exc}"
            ) from exc
        return cls(server, host=host)

    # -- lifecycle -------------------------------------------------------

    @property
    def url(self) -> str:
        """The address to open in a browser.

        A wildcard bind is reported as loopback: ``http://0.0.0.0:8080/`` is
        the address the socket listens on, not one a browser can connect to.
        """
        return f"http://{_authority(_WILDCARDS.get(self.host, self.host), self.port)}/"

    def start(self) -> PreviewServer:
        """Begin answering requests on a daemon thread."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="netgraph-preview",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        """Stop answering and release the port. Safe to call twice."""
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> PreviewServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def _address_family(host: str) -> int:
    """AF_INET6 for a literal IPv6 address, AF_INET otherwise."""
    with suppress(ValueError):
        if ip_address(host.strip("[]")).version == 6:
            return socket.AF_INET6
    return socket.AF_INET


def _authority(host: str, port: int) -> str:
    """``host:port``, bracketing a literal IPv6 address as a URL requires."""
    name = host.strip("[]")
    with suppress(ValueError):
        if ip_address(name).version == 6:
            return f"[{name}]:{port}"
    return f"{name}:{port}"


def describe_exposure(host: str) -> str | None:
    """A warning for a preview that is reachable off this machine, if it is.

    Returns ``None`` for a loopback bind, so a caller can simply print whatever
    comes back.
    """
    if is_loopback(host):
        return None
    where = "every interface" if host in _WILDCARDS else host
    return (
        f"the preview is bound to {where}: anyone who can reach this machine can read "
        f"the topology, addresses and VLANs of this inventory"
    )
