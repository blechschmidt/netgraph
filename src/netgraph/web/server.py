"""The HTTP face of ``netgraph web``: a fixed set of routes over one session.

The command has two faces and this module serves both from one handler, because
they are the same page with a different thing behind it.

**The scratchpad** — ``netgraph web`` on a file, a pipe or nothing — holds a
document stream in the browser and renders what it is sent::

    GET  /api/source     the text the editor opens with
    POST /api/render     a document stream in, a diagram and its records out

**The editing session** — ``netgraph web DIR`` — holds a real inventory tree
(:mod:`netgraph.web.session`) and exposes it::

    GET  /api/state           revision, and what this session allows
    GET  /api/tree            files, documents, element addresses, source lines
    GET  /api/graph?view=l2   the resolved graph, its records and its geometry
    GET  /api/file/<path>     one file's text, with the hash a write must quote
    PUT  /api/file/<path>     that file back, refusing a stale one
    POST /api/ops             a batch of netgraph.edit operations, applied
    POST /api/undo, /api/redo the server-side history

Plus the page itself and its three assets, which are the same either way.

Three properties hold across all of it:

**No request ever becomes a path.** The static routes are a fixed table. The
file routes go through :func:`~netgraph.web.session.relative_path`, which
accepts a relative POSIX path below the root with a YAML suffix and no component
the loader would skip, and refuses everything else by name.

**Nothing is written unless the session says so.** Every mutating route asks the
session, which refuses unless it was opened writable — which the command line
only does for an explicit flag on a loopback bind. A read-only session answers
the same reads and 403s the rest.

**The write path is not here.** This module decodes a request and encodes an
answer; :mod:`netgraph.web.session` decides, and :mod:`netgraph.edit` writes. No
YAML is constructed anywhere in this package.

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
from urllib.parse import parse_qs, unquote, urlsplit

from netgraph.edit import ConflictError, EditError, ValidationRefused
from netgraph.httpserve import (
    DEFAULT_HOST,
    BackgroundServer,
    LocalHandler,
    bind,
    is_loopback,
)
from netgraph.render import IconTheme
from netgraph.web.preview import Preview, RequestError, ViewOptions, render_source
from netgraph.web.session import (
    SESSION_BASELINE,
    Conflict,
    EditingSession,
    ReadOnly,
    SessionError,
)

__all__ = [
    "ASSETS",
    "CHANGES_PATH",
    "DEFAULT_PORT",
    "DIFF_PATH",
    "FILE_PREFIX",
    "GRAPH_PATH",
    "MAX_SOURCE_BYTES",
    "OPS_PATH",
    "REDO_PATH",
    "RENDER_PATH",
    "REVERT_PATH",
    "SOURCE_PATH",
    "STATE_PATH",
    "TREE_PATH",
    "UNDO_PATH",
    "WebServer",
    "asset",
]

#: Default port of ``netgraph web``. One above the preview's, so a watch run
#: and an editing session can be open at the same time.
DEFAULT_PORT: Final = 8081

SOURCE_PATH: Final = "/api/source"
RENDER_PATH: Final = "/api/render"
STATE_PATH: Final = "/api/state"
TREE_PATH: Final = "/api/tree"
GRAPH_PATH: Final = "/api/graph"
OPS_PATH: Final = "/api/ops"
#: The changes drawer: the session's own log, and the handover command list.
CHANGES_PATH: Final = "/api/changes"
#: The same tree, drawn as a diff against a baseline (``?against=session|git``).
DIFF_PATH: Final = "/api/diff"
#: Put one logged gesture back.
REVERT_PATH: Final = "/api/revert"
UNDO_PATH: Final = "/api/undo"
REDO_PATH: Final = "/api/redo"
#: Everything after this is a path inside the inventory, and is checked as one.
FILE_PREFIX: Final = "/api/file/"

#: Largest body the editor may post or put, in bytes. An inventory this size is
#: one to keep in files and open with ``netgraph watch``; a browser textarea is
#: not the tool for it, and rendering it on every keystroke would not be
#: interactive anyway.
MAX_SOURCE_BYTES: Final = 1_000_000

#: The static files, by request path: ``(package resource, content type)``.
ASSETS: Final[dict[str, tuple[str, str]]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/detail.js": ("detail.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/session.js": ("session.js", "text/javascript; charset=utf-8"),
}

#: Where an asset is looked for, in order. ``detail.js`` — how one detail record
#: is drawn — lives with the renderer because the self-contained page
#: ``netgraph render -f html`` writes inlines the same file; serving it from
#: there is what keeps the preview and that page showing one thing rather than
#: two that drift.
_ASSET_ROOTS: Final[tuple[tuple[str, str], ...]] = (
    ("netgraph.web", "assets"),
    ("netgraph.render", "assets"),
)

#: How a refusal is reported. Every one of these is the *caller's* mistake or a
#: race with another editor, never a fault in the inventory — a broken document
#: is something to draw and list, not something to answer 500 with.
_STATUS: Final[tuple[tuple[type[Exception], HTTPStatus], ...]] = (
    (ReadOnly, HTTPStatus.FORBIDDEN),
    (Conflict, HTTPStatus.CONFLICT),
    (ConflictError, HTTPStatus.CONFLICT),
    (ValidationRefused, HTTPStatus.UNPROCESSABLE_ENTITY),
    (SessionError, HTTPStatus.BAD_REQUEST),
    (RequestError, HTTPStatus.BAD_REQUEST),
    (EditError, HTTPStatus.BAD_REQUEST),
)


@cache
def asset(name: str) -> bytes:
    """Read one of the page's static files.

    Cached, because the page is static: the process reads each file once and
    answers from memory afterwards. The name comes from :data:`ASSETS` and
    never from a request, so no path a client sends can reach this.
    """
    for package, directory in _ASSET_ROOTS:
        resource = resources.files(package) / directory / name
        if resource.is_file():
            return resource.read_bytes()
    raise FileNotFoundError(name)  # pragma: no cover - the name comes from ASSETS


class _Handler(LocalHandler):
    """The routes. Everything they answer with is built in memory."""

    server_version = "netgraph-web"

    # Bound onto a subclass by :meth:`WebServer.create`.
    source: str
    icons: IconTheme | None
    session: EditingSession | None
    on_render: Callable[[Preview], None]

    def handle_request(self, method: str, *, body: bool) -> None:
        path = urlsplit(self.path).path
        if method in ("GET", "HEAD"):
            self._get(path, body=body)
        elif method == "POST":
            self._post(path)
        elif method == "PUT":
            self._put(path)
        else:  # pragma: no cover - the base class routes nothing else here
            self.send_text(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "expected GET, HEAD, POST or PUT",
                allow="GET, HEAD, POST, PUT",
            )

    # -- GET -------------------------------------------------------------

    def _get(self, path: str, *, body: bool) -> None:
        if path in ASSETS:
            name, content_type = ASSETS[path]
            self.send_payload(HTTPStatus.OK, asset(name), content_type, body=body)
            return
        if path == STATE_PATH:
            self._json(HTTPStatus.OK, self._state(), body=body)
            return
        if self.session is None:
            self._get_stream(path, body=body)
            return
        try:
            self._get_session(self.session, path, body=body)
        except (SessionError, EditError) as exc:
            self._refuse(exc, body=body)

    def _get_stream(self, path: str, *, body: bool) -> None:
        if path == SOURCE_PATH:
            self._json(HTTPStatus.OK, {"source": self.source}, body=body)
        elif path == RENDER_PATH:
            self.send_text(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "post a document stream to render it",
                body=body,
                allow="POST",
            )
        else:
            self.send_text(HTTPStatus.NOT_FOUND, "not found; the editor is at /", body=body)

    def _get_session(self, session: EditingSession, path: str, *, body: bool) -> None:
        if path == TREE_PATH:
            self._json(HTTPStatus.OK, session.tree(), body=body)
        elif path == GRAPH_PATH:
            view = ViewOptions.from_query(parse_qs(urlsplit(self.path).query), icons=self.icons)
            preview, revision = session.graph(view)
            self.on_render(preview)
            self._json(HTTPStatus.OK, {"revision": revision} | preview.to_dict(), body=body)
        elif path == DIFF_PATH:
            query = parse_qs(urlsplit(self.path).query)
            view = ViewOptions.from_query(query, icons=self.icons)
            against = query.get("against", [SESSION_BASELINE])[-1]
            preview, revision = session.diff(view, against=against)
            self.on_render(preview)
            self._json(
                HTTPStatus.OK,
                {"revision": revision, "against": against} | preview.to_dict(),
                body=body,
            )
        elif path == CHANGES_PATH:
            self._json(
                HTTPStatus.OK,
                session.changes() | {"baselines": list(session.baselines())},
                body=body,
            )
        elif path.startswith(FILE_PREFIX):
            self._json(HTTPStatus.OK, session.read_file(_requested_path(path)), body=body)
        else:
            self.send_text(HTTPStatus.NOT_FOUND, "not found; the editor is at /", body=body)

    # -- POST ------------------------------------------------------------

    def _post(self, path: str) -> None:
        if self.session is None:
            self._post_stream(path)
        else:
            self._post_session(self.session, path)

    def _post_stream(self, path: str) -> None:
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
            self._refuse(exc)
            return
        preview = render_source(source, view)
        self.on_render(preview)
        self._json(HTTPStatus.OK, preview.to_dict())

    def _post_session(self, session: EditingSession, path: str) -> None:
        if path not in (OPS_PATH, UNDO_PATH, REDO_PATH, REVERT_PATH):
            self.send_text(HTTPStatus.NOT_FOUND, f"nothing to post to at {path}")
            return
        try:
            if path == UNDO_PATH:
                change = session.undo()
            elif path == REDO_PATH:
                change = session.redo()
            elif path == REVERT_PATH:
                payload = self._read_json()
                change = session.revert(_entry_id(payload), revision=_revision(payload))
            else:
                payload = self._read_json()
                change = session.apply(
                    payload.get("ops", []),
                    revision=_revision(payload),
                    force=bool(payload.get("force", False)),
                )
        except Exception as exc:  # narrowed by ``_refuse``, which re-raises the rest
            self._refuse(exc)
            return
        self._json(HTTPStatus.OK, change.to_dict())

    # -- PUT -------------------------------------------------------------

    def _put(self, path: str) -> None:
        session = self.session
        if session is None or not path.startswith(FILE_PREFIX):
            self.send_text(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "nothing here takes a PUT"
                if session is not None
                else "this is a document-stream scratchpad, not a tree; "
                "open a folder with 'netgraph web DIR --write' to edit files",
                allow="GET, HEAD, POST",
            )
            return
        try:
            payload = self._read_json()
            text = payload.get("text")
            if not isinstance(text, str):
                raise RequestError("'text' must be the file's new contents, as a string")
            base = payload.get("hash")
            if base is not None and not isinstance(base, str):
                raise RequestError("'hash' must be the content hash the file was read at")
            change = session.write_file(
                _requested_path(path),
                text,
                base_hash=base,
                force=bool(payload.get("force", False)),
            )
        except Exception as exc:
            self._refuse(exc)
            return
        self._json(HTTPStatus.OK, change.to_dict())

    # -- shared ----------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        if self.session is None:
            return {
                "mode": "stream",
                "revision": 0,
                "writable": False,
                "undo": 0,
                "redo": 0,
                "maxFileBytes": MAX_SOURCE_BYTES,
            }
        return self.session.state()

    def _json(self, status: HTTPStatus, payload: Any, *, body: bool = True) -> None:
        self.send_payload(status, json.dumps(payload).encode(), "application/json", body=body)

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
                f"the request body is {length} bytes; this editor accepts up to "
                f"{MAX_SOURCE_BYTES}. Keep a document this size out of the browser"
            )
        raw = self.rfile.read(length)
        # Whatever happens below, the body is off the connection; see
        # ``LocalHandler._discard_body`` for what the flag is for.
        self.body_consumed = True
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(f"the request body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RequestError("the request body must be a JSON object")
        return payload

    def _refuse(self, exc: BaseException, *, body: bool = True) -> None:
        """Answer in the shape the page expects, so it can show the reason.

        Only the refusals :data:`_STATUS` names are answered; anything else is a
        bug in netgraph and is re-raised, which the base server turns into a 500
        and a traceback in the log. Swallowing those would leave an editor
        quietly not saving.
        """
        for kind, status in _STATUS:
            if isinstance(exc, kind):
                self._json(status, _problem_body(exc, status), body=body)
                return
        raise exc


def _problem_body(exc: BaseException, status: HTTPStatus) -> dict[str, Any]:
    """The JSON a refusal carries: why, and whatever the page can act on."""
    payload: dict[str, Any] = {
        "status": "failed",
        "message": str(exc),
        "code": status.value,
    }
    if isinstance(exc, Conflict):
        payload["conflict"] = {"path": exc.path, "hash": exc.hash}
    elif isinstance(exc, ConflictError):
        payload["conflict"] = {"path": exc.path, "hash": exc.actual}
    elif isinstance(exc, ValidationRefused):
        payload["problems"] = [
            {"rule": problem.rule, "location": problem.location, "message": problem.message}
            for problem in exc.problems
        ]
    return payload


def _requested_path(path: str) -> str:
    """The inventory-relative path a ``/api/file/…`` route names.

    Percent-decoded here and checked in
    :func:`~netgraph.web.session.relative_path`, which is the only thing that
    ever turns one of these into a file name.
    """
    return unquote(path[len(FILE_PREFIX) :])


def _entry_id(payload: dict[str, Any]) -> int:
    """Which logged gesture a revert names.

    Raises:
        RequestError: The body names none, or names something that is not one.
    """
    value = payload.get("id")
    if not isinstance(value, int) or isinstance(value, bool):
        raise RequestError("'id' must be the number of the change to put back")
    return value


def _revision(payload: dict[str, Any]) -> int | None:
    """The tree revision a batch was decided against, if it named one."""
    value = payload.get("revision")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise RequestError("'revision' must be the tree revision this batch was built from")
    return value


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
        session: EditingSession | None = None,
        icons: IconTheme | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        log: Callable[[str], None] = lambda message: None,
        on_render: Callable[[Preview], None] = lambda preview: None,
    ) -> WebServer:
        """Bind the interface without answering requests yet.

        Args:
            source: What the scratchpad opens with. Ignored when ``session`` is
                given, because then the files are the source.
            session: The inventory this interface edits. ``None`` serves the
                document-stream scratchpad instead.
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
                "session": session,
                "icons": icons,
                "on_render": staticmethod(on_render),
                "loopback_only": is_loopback(host),
                "log": staticmethod(log),
            },
        )
        return cls(bind(handler, host=host, port=port, subject="the web interface"), host=host)
