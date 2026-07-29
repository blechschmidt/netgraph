"""A local HTTP preview of the live render, with a page that reloads itself.

The server exists so that ``netgraph watch --serve`` can show a diagram that
updates as the inventory is edited. It is a *development* server and behaves
like one, with three deliberate restrictions:

* **It binds to 127.0.0.1 unless told otherwise.**
* **It serves memory, never the filesystem.** There are five fixed routes and
  no path is ever turned into a file name, so no request can reach a document
  the user did not ask to render.
* **It answers GET and HEAD only,** under a strict ``Content-Security-Policy``
  and with a ``Host`` header check that keeps a loopback-bound preview from
  being reached through a rebound DNS name.

The first and third are promises :mod:`netgraph.httpserve` makes on behalf of
every local server netgraph starts; what is here is the routing on top of them.

The page polls :data:`STATUS_PATH` once a second and swaps the diagram in when
the revision changes. Polling rather than server-sent events keeps the client a
dozen lines of dependency-free JavaScript.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, Final
from urllib.parse import urlsplit

from netgraph.httpserve import (
    DEFAULT_HOST,
    BackgroundServer,
    LocalHandler,
    ServeError,
    bind,
    describe_exposure,
    is_loopback,
)
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

#: Default port of ``netgraph watch --serve``. The bind address comes from
#: :data:`netgraph.httpserve.DEFAULT_HOST`, which is loopback.
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


class _Handler(LocalHandler):
    """Five fixed routes over the live render. No request touches a file."""

    server_version = "netgraph-watch"

    # Bound onto a subclass by :meth:`PreviewServer.create`.
    live: LiveRender
    page: bytes
    payload_path: str
    output_format: str

    def handle_request(self, method: str, *, body: bool) -> None:
        if method not in ("GET", "HEAD"):
            self.send_text(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "the preview is read-only; it answers GET and HEAD",
                body=body,
                allow="GET, HEAD",
            )
            return

        path = urlsplit(self.path).path
        if path == "/":
            self.send_payload(HTTPStatus.OK, self.page, "text/html; charset=utf-8", body=body)
        elif path == _STYLE_PATH:
            self.send_payload(HTTPStatus.OK, _STYLE.encode(), "text/css; charset=utf-8", body=body)
        elif path == _SCRIPT_PATH:
            self.send_payload(
                HTTPStatus.OK, _SCRIPT.encode(), "text/javascript; charset=utf-8", body=body
            )
        elif path == STATUS_PATH:
            payload = json.dumps(status_document(self.live.snapshot())).encode()
            self.send_payload(HTTPStatus.OK, payload, "application/json", body=body)
        elif path == self.payload_path:
            self._send_render(body=body)
        else:
            self.send_text(HTTPStatus.NOT_FOUND, "not found; the preview serves / only", body=body)

    def _send_render(self, *, body: bool) -> None:
        snapshot = self.live.snapshot()
        if snapshot.payload is None:
            self.send_text(HTTPStatus.SERVICE_UNAVAILABLE, "nothing has rendered yet", body=body)
            return
        # An unregistered format is served as a download, so that a browser
        # cannot be talked into interpreting it as something else.
        self.send_payload(
            HTTPStatus.OK, snapshot.payload, media_type_for(self.output_format), body=body
        )


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


class PreviewServer(BackgroundServer):
    """A running preview, started and stopped explicitly.

    Use it as a context manager::

        with PreviewServer.create(live, title="inventory") as preview:
            print(preview.url)
    """

    thread_name = "netgraph-preview"

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
        return cls(bind(handler, host=host, port=port), host=host)
