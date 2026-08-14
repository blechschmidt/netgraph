"""The HTTP face of ``netgraph web``: a fixed set of routes over one session.

The command has two faces and this module serves both from one handler, because
they are the same page with a different thing behind it.

**The scratchpad** — ``netgraph web`` on a file, a pipe or nothing — holds a
document stream in the browser and renders what it is sent::

    GET  /api/source     the text the editor opens with
    POST /api/render     a document stream in, a diagram and its records out

**The editing session** — ``netgraph web DIR`` — holds a real inventory tree
(:mod:`netgraph.web.session`) and exposes it::

    GET  /api/state           revision, what this session allows, who else is here
    GET  /api/tree            files, documents, element addresses, source lines
    GET  /api/graph?view=l2   the resolved graph, its records and its geometry
    GET  /api/file/<path>     one file's text, with the hash a write must quote
    PUT  /api/file/<path>     that file back, refusing a stale one
    POST /api/ops             a batch of netgraph.edit operations, applied
    POST /api/undo, /api/redo the server-side history
    GET  /api/events          server-sent events: what changed, as it changes
    POST /api/presence        what this client has selected and is editing

Plus the page itself and its three assets, which are the same either way.

**The stream is an optimisation, not a channel of authority.** ``/api/events``
says what moved so that a client can refetch one file instead of a tree and skip
a Graphviz run for a picture that did not change. Every fact it carries is also
answerable by a plain ``GET``: ``/api/state`` still holds the revision, and
``?since=`` on it replays the same events out of the same ring buffer for a
client that cannot hold a connection open — a buffering proxy, ``curl``, a test.
Nothing is writable through the stream, and no write is gated on having read it.

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
from collections.abc import Callable, Mapping, Sequence
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
from netgraph.web.bindings import payload as bindings_payload
from netgraph.web.events import (
    EVENTS_PATH,
    HEARTBEAT_SECONDS,
    PRESENCE_PATH,
    Event,
    Subscription,
    TooManyStreams,
    heartbeat_frame,
)
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
    "BINDINGS_PATH",
    "CHANGES_PATH",
    "DEFAULT_PORT",
    "DIFF_PATH",
    "EVENTS_PATH",
    "FILE_PREFIX",
    "FIX_PATH",
    "FRAME_PATH",
    "GRAPH_PATH",
    "HISTORY_PATH",
    "IMPACT_PATH",
    "MAX_SOURCE_BYTES",
    "OPS_PATH",
    "PRESENCE_PATH",
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
#: The keyboard bindings and the command list, from :mod:`netgraph.web.bindings`.
#: Answered in both faces: the scratchpad has fewer commands available, not fewer
#: commands, and the palette says which are out of reach and why.
BINDINGS_PATH: Final = "/api/bindings"
TREE_PATH: Final = "/api/tree"
GRAPH_PATH: Final = "/api/graph"
OPS_PATH: Final = "/api/ops"
#: The changes drawer: the session's own log, and the handover command list.
CHANGES_PATH: Final = "/api/changes"
#: The same tree, drawn as a diff against a baseline (``?against=session|git``).
DIFF_PATH: Final = "/api/diff"
#: What would stop being reachable if the named elements failed
#: (``?fail=<address>&layer=l1``). Read-only: the failure overlay changes no
#: file, no revision and no undo stack.
IMPACT_PATH: Final = "/api/impact"
#: The commits that changed this inventory, for the timeline scrubber.
HISTORY_PATH: Final = "/api/history"
#: One of them, drawn as the diff against its parent (``?rev=<commit>``).
FRAME_PATH: Final = "/api/frame"
#: Put one logged gesture back.
REVERT_PATH: Final = "/api/revert"
#: Apply the mechanical repair for one diagnostic (:mod:`netgraph.fixes`).
FIX_PATH: Final = "/api/fix"
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
    "/keys.js": ("keys.js", "text/javascript; charset=utf-8"),
    "/a11y.js": ("a11y.js", "text/javascript; charset=utf-8"),
    "/cull.js": ("cull.js", "text/javascript; charset=utf-8"),
    "/links.js": ("links.js", "text/javascript; charset=utf-8"),
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
        if path == BINDINGS_PATH:
            self._json(HTTPStatus.OK, bindings_payload(), body=body)
            return
        if self.session is None:
            self._get_stream(path, body=body)
            return
        try:
            self._get_session(self.session, path, body=body)
        except (SessionError, EditError) as exc:
            self._refuse(exc, body=body)

    @property
    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query)

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
        query = self._query
        if path == TREE_PATH:
            # ``?path=`` names the files to answer for, so a client that was told
            # exactly what moved does not pay for a walk of the whole tree.
            wanted = _paths(query)
            self._json(
                HTTPStatus.OK,
                session.tree(wanted, diagnostics=_flag(query, "diagnostics", default=True)),
                body=body,
            )
        elif path == EVENTS_PATH:
            self._events(session, body=body)
        elif path == GRAPH_PATH:
            view = ViewOptions.from_query(query, icons=self.icons)
            preview, revision = session.graph(view, known=_known(query))
            self.on_render(preview)
            self._json(HTTPStatus.OK, {"revision": revision} | preview.to_dict(), body=body)
        elif path == DIFF_PATH:
            view = ViewOptions.from_query(query, icons=self.icons)
            against = query.get("against", [SESSION_BASELINE])[-1]
            preview, revision = session.diff(view, against=against, known=_known(query))
            self.on_render(preview)
            self._json(
                HTTPStatus.OK,
                {"revision": revision, "against": against} | preview.to_dict(),
                body=body,
            )
        elif path == IMPACT_PATH:
            self._json(
                HTTPStatus.OK,
                session.impact(
                    query.get("fail", []),
                    layer=query.get("layer", ["l1"])[-1],
                ),
                body=body,
            )
        elif path == HISTORY_PATH:
            self._json(
                HTTPStatus.OK,
                session.history(limit=_integer(query, "limit")),
                body=body,
            )
        elif path == FRAME_PATH:
            view = ViewOptions.from_query(query, icons=self.icons)
            rev = query.get("rev", [""])[-1]
            if not rev:
                raise SessionError("a frame is of one revision; give '?rev=<commit>'")
            self._json(HTTPStatus.OK, session.frame(rev, view, known=_known(query)), body=body)
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

    # -- the event stream ------------------------------------------------

    def _events(self, session: EditingSession, *, body: bool) -> None:
        """Hold one ``text/event-stream`` open until the client goes away.

        The whole of the push channel's HTTP side. What it must get right:

        * **Resume, or say it cannot.** ``Last-Event-ID`` — the header a browser
          resends by itself, or ``?lastEventId=`` for a client written by hand —
          is replayed out of the ring buffer. A resume point that has fallen out
          of it opens with ``resync`` instead of a plausible-looking partial
          replay, because a client that applies a patch to a state it does not
          have is worse off than one that refetches.
        * **Say hello first.** The opening frame carries the client id and the
          revision, so a page knows who it is and what it is looking at before
          any incremental event arrives.
        * **Beat.** An idle stream writes a comment every
          :data:`~netgraph.web.events.HEARTBEAT_SECONDS`, which keeps a proxy
          from timing the connection out, keeps this client's presence entry
          alive, and — because a write to a dead socket raises — is how the
          server notices a tab that was closed without a FIN.
        * **Leave nothing behind.** The subscription and the presence entry are
          released in ``finally``: this thread is the only owner of both.

        A ``HEAD`` is answered with the headers and no stream. There is nothing
        to say in one, and holding a connection open for a client that has told
        us it will read no body is a thread spent on nothing.
        """
        try:
            subscription = session.events.subscribe(self._last_event_id())
        except TooManyStreams as exc:
            self.send_text(HTTPStatus.SERVICE_UNAVAILABLE, str(exc), body=body)
            return
        # Read before anyone joins: this is the id the client resumes from, and
        # an event published while the stream is being set up must land *after*
        # it rather than be skipped by it.
        resume_from = session.events.last_id
        self.begin_stream("text/event-stream; charset=utf-8")
        if not body:  # a HEAD: the headers are the whole answer
            subscription.close()
            return
        client = session.join(streaming=True, client_id=_client_id(self._query))
        try:
            self._write_frame(
                Event(
                    id=resume_from,
                    name="hello",
                    revision=session.revision,
                    data={
                        "client": client.id,
                        "label": client.label,
                        # The resume point had fallen out of the ring: this
                        # stream cannot be a continuation, so say so in the first
                        # frame rather than let a patch land on a state that is
                        # not there.
                        "resync": subscription.gap,
                        "heartbeatMs": round(HEARTBEAT_SECONDS * 1000),
                        "clients": session.presence.payload(me=client.id),
                    },
                ).frame()
            )
            self._pump(session, subscription, client.id)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The tab was closed, the machine slept, the socket died. Every one
            # of them is the ordinary end of a stream, not a fault to log.
            pass
        finally:
            subscription.close()
            # Not a *leave*: a browser reconnects a dropped stream within
            # seconds and should get its identity back, so the entry is marked
            # as no longer streaming and left to expire. A tab that is really
            # closing says so with a POST to /api/presence.
            session.stream_ended(client.id)

    def _pump(self, session: EditingSession, subscription: Subscription, client_id: str) -> None:
        """Write events as they arrive, and a heartbeat when they do not."""
        while True:
            # ``wait`` returns nothing both when the interval elapsed and when
            # the subscription was closed under us — by the client hanging up or
            # by the server stopping — so the flag is what ends the loop.
            events = subscription.wait(HEARTBEAT_SECONDS)
            if subscription.closed:
                return
            if not events:
                self._write_frame(heartbeat_frame())
                # The beat is also this client's keepalive: a stream that is
                # open is a client that is present, and no separate request
                # should be needed to say so.
                session.presence.touch(client_id)
                continue
            for event in events:
                self._write_frame(event.frame())
            if subscription.gap:
                # This stream fell behind and lost events. Anything sent now
                # would be applied to a state the client cannot have, so it is
                # told to start again and the connection is ended; its reconnect
                # opens with a fresh subscription.
                self._write_frame(
                    Event(
                        id=events[-1].id,
                        name="resync",
                        revision=session.revision,
                        data={"reason": "this stream fell behind"},
                    ).frame()
                )
                return

    def _write_frame(self, frame: bytes) -> None:
        self.wfile.write(frame)
        self.wfile.flush()

    def _last_event_id(self) -> int | None:
        """Where a reconnecting client left off, or ``None`` for a fresh stream.

        A value that is not a number is treated as no value at all: the client is
        then sent the state it would have fetched anyway, which is right, rather
        than a 400 that leaves a page with no channel over a malformed header.
        """
        raw = self.headers.get("Last-Event-ID") or (self._query.get("lastEventId") or [""])[-1]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

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
        if path == PRESENCE_PATH:
            self._presence(session)
            return
        if path not in (OPS_PATH, UNDO_PATH, REDO_PATH, REVERT_PATH, FIX_PATH):
            self.send_text(HTTPStatus.NOT_FOUND, f"nothing to post to at {path}")
            return
        try:
            if path == UNDO_PATH:
                change = session.undo(client=_client_id(self._query))
            elif path == REDO_PATH:
                change = session.redo(client=_client_id(self._query))
            elif path == FIX_PATH:
                payload = self._read_json()
                change = session.fix(
                    _required(payload, "rule"),
                    _required(payload, "message"),
                    key=_optional(payload, "fix"),
                    revision=_revision(payload),
                    client=_client(payload),
                )
            elif path == REVERT_PATH:
                payload = self._read_json()
                change = session.revert(
                    _entry_id(payload),
                    revision=_revision(payload),
                    client=_client(payload),
                )
            else:
                payload = self._read_json()
                change = session.apply(
                    payload.get("ops", []),
                    revision=_revision(payload),
                    force=bool(payload.get("force", False)),
                    client=_client(payload),
                )
        except Exception as exc:  # narrowed by ``_refuse``, which re-raises the rest
            self._refuse(exc)
            return
        self._json(HTTPStatus.OK, change.to_dict())

    def _presence(self, session: EditingSession) -> None:
        """Say who you are and what you are doing; hear who else is here.

        Three jobs in one route, because they are one round trip for a client on
        the polling fallback: it is the keepalive that stops the entry expiring,
        the way a selection and a set of unsaved files are published, and the way
        the list comes back.

        It writes nothing to disk and is therefore *not* gated on ``--write``: a
        read-only session is still a session two people can have open, and
        knowing where the other one is looking is exactly as useful there. What
        gates it is the same thing that gates every other route here — the
        loopback bind and the ``Host`` check in :class:`~netgraph.httpserve.LocalHandler`.
        """
        try:
            payload = self._read_json()
        except RequestError as exc:
            self._refuse(exc)
            return
        identity = _client(payload)
        if payload.get("leaving"):
            # The tab is closing and said so, which is the difference between a
            # list that is right now and one that is right in 45 seconds.
            if identity is not None:
                session.leave(identity)
            self._json(HTTPStatus.OK, {"clients": session.presence.payload(), "left": True})
            return
        client = (
            session.join(client_id=None)
            if identity is None
            else session.report(
                identity,
                selection=_strings(payload.get("selection")),
                editing=_strings(payload.get("editing")),
                view=_text(payload.get("view")),
            )
        )
        self._json(
            HTTPStatus.OK,
            {
                "client": client.id,
                "label": client.label,
                "clients": session.presence.payload(me=client.id),
                "revision": session.revision,
                "lastEventId": session.events.last_id,
            },
        )

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
                client=_client(payload),
            )
        except Exception as exc:
            self._refuse(exc)
            return
        self._json(HTTPStatus.OK, change.to_dict())

    # -- shared ----------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        """What the page fetches at boot, and what a polling client re-fetches.

        ``?client=`` keeps that client's presence alive and marks it in the list,
        so a page that could not open a stream stays visible to the others by
        doing the thing it was doing anyway. ``?since=`` replays the events after
        an id out of the same ring buffer the stream reads, so a polling client
        gets the same incremental instructions — refetch *these* files, this
        picture did not move — rather than only "the revision is different".
        """
        if self.session is None:
            return {
                "mode": "stream",
                "revision": 0,
                "writable": False,
                "undo": 0,
                "redo": 0,
                "maxFileBytes": MAX_SOURCE_BYTES,
            }
        query = self._query
        identity = _client_id(query)
        if identity is not None:
            self.session.presence.touch(identity)
        state = self.session.state(me=identity)
        since = _integer(query, "since")
        if since is not None:
            state["events"] = dict(state["events"]) | {
                "since": since,
                "replay": [event.to_dict() for event in self.session.events.history(since)],
            }
        return state

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


def _paths(query: Mapping[str, Sequence[str]]) -> list[str] | None:
    """Which files ``/api/tree`` was asked about, or ``None`` for all of them.

    ``?path=a.yaml&path=b.yaml`` and ``?path=a.yaml,b.yaml`` both work: the first
    is what a program builds, the second is what a person types. Each one is
    checked by :func:`~netgraph.web.session.relative_path` before it becomes a
    file name, exactly like the ``/api/file/`` routes — this is a second door
    into the same room and it gets the same lock.
    """
    given = [item for value in query.get("path", ()) for item in value.split(",") if item]
    return given or None


def _known(query: Mapping[str, Sequence[str]]) -> str | None:
    """The graph fingerprint the caller says it already holds a drawing for.

    Only ever compared for equality with one this server computed, so a value
    that is nonsense costs a full render and nothing else.
    """
    values = query.get("known") or ()
    return values[-1] or None if values else None


def _flag(query: Mapping[str, Sequence[str]], name: str, *, default: bool) -> bool:
    """A ``0``/``1`` query flag, defaulting when it is absent or unreadable."""
    values = query.get(name) or ()
    if not values:
        return default
    return values[-1] not in ("0", "false", "no", "")


def _integer(query: Mapping[str, Sequence[str]], name: str) -> int | None:
    """A numeric query parameter, or ``None`` when absent or not a number."""
    values = query.get(name) or ()
    if not values:
        return None
    try:
        return int(values[-1])
    except ValueError:
        return None


def _client_id(query: Mapping[str, Sequence[str]]) -> str | None:
    """The client id a request carries in its query string.

    Never a permission: it names an entry in the presence list, which decides
    nothing about what a request may do. See
    :meth:`~netgraph.web.session.EditingSession.write_file`.
    """
    values = query.get("client") or ()
    return values[-1] or None if values else None


def _client(payload: Mapping[str, Any]) -> str | None:
    """The same, out of a JSON body."""
    value = payload.get("client")
    return value if isinstance(value, str) and value else None


def _strings(value: Any) -> list[str] | None:
    """A list of strings from a request body, or ``None`` when it named none.

    Anything that is not a list of strings is *dropped* rather than refused: this
    is presence, it decides nothing, and a page whose selection cannot be
    reported is better off than one whose save is rejected for it.
    """
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str) and item]


def _text(value: Any) -> str | None:
    """One short string from a request body, bounded so a list stays a list."""
    return value[:64] if isinstance(value, str) and value else None


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


def _required(payload: dict[str, Any], key: str) -> str:
    """A required string field of a request body.

    Raises:
        RequestError: It is missing, empty or not a string.
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RequestError(f"{key!r} must be a non-empty string")
    return value


def _optional(payload: dict[str, Any], key: str) -> str | None:
    """The same, for a field a request may leave out.

    Raises:
        RequestError: It is present and is not a string.
    """
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RequestError(f"{key!r} must be a non-empty string when it is given")
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

    #: The session this interface serves, or ``None`` for the scratchpad. Held so
    #: that stopping the server can also close the event streams it is holding
    #: open — each one is a request thread parked in a wait, and shutting the
    #: socket does not wake it.
    session: EditingSession | None = None

    def stop(self) -> None:
        """Stop answering, close every open event stream, and release the port."""
        if self.session is not None:
            self.session.close()
        super().stop()

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
        web = cls(bind(handler, host=host, port=port, subject="the web interface"), host=host)
        web.session = session
        return web
