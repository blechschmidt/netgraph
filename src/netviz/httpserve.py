"""The plumbing the local HTTP front ends share.

Two commands serve something over HTTP — ``netviz watch --serve``, which
shows a diagram that re-renders itself, and ``netviz web``, which renders a
document stream the browser sends it. They are different applications, but the
promises they make about *being a local server* have to be identical, because a
user who has decided one of them is safe to run has decided it about both:

* **Loopback unless told otherwise.** An inventory describes internal topology
  — addresses, VLANs, what is plugged into what — so the default bind must not
  put it on the network, and ``--host`` is the explicit act of publishing it.
* **No path ever becomes a file name.** Each front end answers a fixed set of
  routes out of memory, so no request can reach a document the user did not
  offer it. One of those routes — the editing session's event stream — answers
  over time rather than at once (:meth:`LocalHandler.begin_stream`); it is the
  same promise, held open.
* **Nothing is written unless the command line asked for it.** ``PUT`` and the
  mutating ``POST`` routes exist in one front end only — the editing session of
  ``netviz web`` — and only when it was opened with the flag that enables
  them, on a loopback bind. Every other server here answers them with 405.
* **A ``Host`` header check on a loopback bind.** Without it any web page could
  point a name it controls at 127.0.0.1 and read the diagram — and with it the
  topology — out of the user's browser (DNS rebinding).
* **The same response headers**: a strict ``Content-Security-Policy``, no
  sniffing, no referrer, no caching, no framing.

What is *not* here is routing: which paths exist and what they answer with is
the application, and lives with it.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any, Final, TypeVar

from netviz.errors import NetvizError

__all__ = [
    "DEFAULT_CSP",
    "DEFAULT_HOST",
    "MAX_DISCARD",
    "BackgroundServer",
    "LocalHandler",
    "LocalServer",
    "ServeError",
    "address_family",
    "authority",
    "bind",
    "describe_exposure",
    "has_port",
    "is_loopback",
]

#: Loopback, always. Overriding this is an explicit decision the user makes.
DEFAULT_HOST: Final = "127.0.0.1"

#: Bind addresses that mean "every interface", and the address on this machine
#: that actually reaches them. Used only to print a URL a browser can open.
_WILDCARDS: Final[dict[str, str]] = {"0.0.0.0": "127.0.0.1", "::": "::1", "[::]": "::1"}

#: Most of a request body that :meth:`LocalHandler._discard_body` will read and
#: throw away to keep a connection usable. Anything larger is not worth the read;
#: the connection is closed instead, which is equally correct and cheaper.
MAX_DISCARD: Final = 4 * 1024 * 1024

#: What every response is allowed to load: whatever this server itself sent,
#: and nothing else. Both front ends inline their own diagram, so ``data:``
#: images are permitted — that is how an icon theme survives into an SVG — and
#: neither may be framed or navigate anywhere.
DEFAULT_CSP: Final = (
    "default-src 'none'; img-src 'self' data:; object-src 'self'; "
    "script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class ServeError(NetvizError):
    """Raised when a local server cannot be started."""

    exit_code = 6


def is_loopback(host: str) -> bool:
    """Does ``host`` name only this machine?

    Used to decide whether exposing a server needs a warning, so an
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


def has_port(header: str) -> bool:
    """Does a ``Host`` header carry a ``:port`` suffix?

    ``[::1]:8080`` has one and ``::1`` — which a client should have bracketed
    but may not have — does not.
    """
    if header.startswith("["):
        return header.rfind("]") < header.rfind(":")
    return header.count(":") == 1


def address_family(host: str) -> int:
    """AF_INET6 for a literal IPv6 address, AF_INET otherwise."""
    with suppress(ValueError):
        if ip_address(host.strip("[]")).version == 6:
            return socket.AF_INET6
    return socket.AF_INET


def authority(host: str, port: int) -> str:
    """``host:port``, bracketing a literal IPv6 address as a URL requires."""
    name = host.strip("[]")
    with suppress(ValueError):
        if ip_address(name).version == 6:
            return f"[{name}]:{port}"
    return f"{name}:{port}"


def describe_exposure(host: str, *, subject: str = "the preview") -> str | None:
    """A warning for a server that is reachable off this machine, if it is.

    Returns ``None`` for a loopback bind, so a caller can simply print whatever
    comes back.
    """
    if is_loopback(host):
        return None
    where = "every interface" if host in _WILDCARDS else host
    return (
        f"{subject} is bound to {where}: anyone who can reach this machine can read "
        f"the topology, addresses and VLANs of this inventory"
    )


class LocalHandler(BaseHTTPRequestHandler):
    """A request handler with the local-server promises already kept.

    A subclass implements :meth:`handle_request` and nothing else: the host
    check, the security headers and the access log are done here, so two front
    ends cannot end up with two different ideas of what a local server may do.
    """

    server_version = "netviz"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    #: Set by an application that has read the request body, so that
    #: :meth:`_discard_body` does not try to read it a second time. Reset before
    #: every request.
    body_consumed: bool = False

    # Bound onto a subclass when the server is created; see :func:`bind`.
    loopback_only: bool = True
    log: Callable[[str], None] = staticmethod(lambda message: None)
    content_security_policy: str = DEFAULT_CSP

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
        self._dispatch("GET", body=True)

    def do_HEAD(self) -> None:
        self._dispatch("HEAD", body=False)

    def do_POST(self) -> None:
        self._dispatch("POST", body=True)

    def do_PUT(self) -> None:
        # Only the editing session answers this, and only when it was opened for
        # writing; every other front end falls through to its own 405. The
        # method is dispatched here rather than in that one application so the
        # host check and the security headers apply to it too.
        self._dispatch("PUT", body=True)

    def _dispatch(self, method: str, *, body: bool) -> None:
        self.body_consumed = False
        try:
            if not self._host_is_allowed():
                self.send_payload(
                    HTTPStatus.MISDIRECTED_REQUEST,
                    b"this server is bound to loopback and only answers to localhost\n",
                    "text/plain; charset=utf-8",
                    body=body,
                )
                return
            self.handle_request(method, body=body)
        finally:
            self._discard_body()

    def _discard_body(self) -> None:
        """Read whatever of the request body the application did not.

        ``protocol_version`` is HTTP/1.1, so connections are kept alive — and on
        a kept-alive connection an unread body *is* the next request as far as
        the parser is concerned. Every refusal that answers without looking at
        the body is therefore a trap for the request after it: a 403 from a
        read-only session, a 404 for an unknown route, a 421 from the host check.
        The symptom is a nonsense ``501 Unsupported method`` on a later request,
        which reads like a bug in the client.

        Bounded rather than unbounded, and a body that says something the parser
        cannot use at all closes the connection instead of being guessed at.
        """
        if self.body_consumed:
            return
        if self.headers.get("Transfer-Encoding"):
            # Chunked, so the length is not in a header and the framing is not
            # worth re-implementing to throw the bytes away.
            self.close_connection = True
            return
        header = self.headers.get("Content-Length")
        if header is None:
            return
        try:
            length = int(header)
        except ValueError:
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > MAX_DISCARD:
            self.close_connection = True
            return
        with suppress(OSError):
            self.rfile.read(length)

    def handle_request(self, method: str, *, body: bool) -> None:
        """Answer one request. Implemented by the application."""
        raise NotImplementedError

    def _host_is_allowed(self) -> bool:
        """Reject a request that reached a loopback server under another name.

        A server the user chose to publish with ``--host`` makes no such
        promise and is not checked.
        """
        if not self.loopback_only:
            return True
        header = self.headers.get("Host", "")
        if not header:
            # HTTP/1.0 clients may omit it; they cannot be rebinding victims.
            return True
        hostname = header.rsplit(":", 1)[0] if has_port(header) else header
        return is_loopback(hostname)

    # -- responses -------------------------------------------------------

    def send_payload(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *,
        body: bool = True,
        headers: dict[str, str] | None = None,
        csp: str | None = None,
    ) -> None:
        """Send one complete response, with the fixed security headers.

        Args:
            csp: Content-Security-Policy for this response, replacing the
                server's. One response in one front end needs it — the
                self-contained page ``netviz watch -f html`` renders, which
                carries a policy of its own that the server's would contradict
                (:data:`~netviz.render.registry.PAGE_CSP`) — and it is a
                per-response decision rather than a per-server one because the
                page *around* it keeps the strict default.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp or self.content_security_policy)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

    def begin_stream(self, content_type: str, *, headers: dict[str, str] | None = None) -> None:
        """Open a response whose length is not known yet, and keep it open.

        One route needs this — the editing session's event stream — and it is
        here rather than there so that a long-lived response cannot end up with a
        different set of security headers from every other one.

        Two things differ from :meth:`send_payload`, and both follow from there
        being no ``Content-Length`` to send:

        * The connection is marked for closing. ``protocol_version`` is HTTP/1.1,
          so without a length the only framing left is end-of-connection —
          chunked encoding would work too and would buy nothing, since a client
          that hangs up mid-stream is the *normal* end of an event stream.
        * The request body, if any, is taken as read. A stream can outlive any
          notion of "the next request on this connection", so the base class must
          not try to drain one afterwards.

        The caller writes frames to ``self.wfile`` and flushes; a
        :class:`BrokenPipeError` or :class:`ConnectionResetError` from a write is
        the client having closed the tab, and is the loop's cue to stop.
        """
        self.close_connection = True
        self.body_consumed = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", self.content_security_policy)
        self.send_header("Connection", "close")
        # Nginx and friends buffer a proxied response by default, which turns an
        # event stream into a response that arrives all at once when it ends.
        # The header is theirs; a client behind a proxy that ignores it is why
        # the polling fallback exists.
        self.send_header("X-Accel-Buffering", "no")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def send_text(
        self, status: HTTPStatus, message: str, *, body: bool = True, allow: str | None = None
    ) -> None:
        """Send a plain-text status message, newline-terminated."""
        self.send_payload(
            status,
            f"{message}\n".encode(),
            "text/plain; charset=utf-8",
            body=body,
            headers={"Allow": allow} if allow else None,
        )


class LocalServer(ThreadingHTTPServer):
    """The socket side: threaded, and restartable in a loop.

    ``SO_REUSEADDR`` is the one socket option here that does not mean the same
    thing everywhere, and the difference is not cosmetic. On POSIX it means "do
    not refuse this bind merely because the port is in ``TIME_WAIT``", which is
    what lets ``netviz watch`` be stopped and restarted on a fixed port without
    a minute's wait. On Windows it means "bind this port even if another socket
    is *actively listening* on it": two servers then share the port and the
    kernel hands each incoming connection to whichever it likes, so a second
    ``netviz web --port 8080`` would appear to start and then answer half the
    requests with the other inventory.

    So the option is set only where it is safe, and Windows gets
    ``SO_EXCLUSIVEADDRUSE`` instead — which is the Windows way to say what a
    plain bind already says on POSIX: this port is mine, and a second bind must
    fail. That failure is what :func:`bind` turns into "cannot serve … address
    already in use", which is the message the user needs.
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        # Guarded with ``getattr`` rather than a platform check so that a type
        # checker running with ``--platform linux`` sees the same code the
        # Windows interpreter runs.
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:  # pragma: no cover - Windows only
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def bind(
    handler: type[LocalHandler], *, host: str, port: int, subject: str = "the preview"
) -> LocalServer:
    """Bind ``handler`` to ``host:port`` without answering requests yet.

    Raises:
        ServeError: The address cannot be bound.
    """
    server_class = type("_BoundServer", (LocalServer,), {"address_family": address_family(host)})
    try:
        server: LocalServer = server_class((host, port), handler)
    except OSError as exc:
        raise ServeError(
            f"cannot serve {subject} on {authority(host, port)}: {exc.strerror or exc}"
        ) from exc
    return server


#: A subclass of :class:`BackgroundServer`, so that ``create(...).start()`` on
#: one keeps its own type rather than widening to the base.
_Server = TypeVar("_Server", bound="BackgroundServer")


class BackgroundServer:
    """A bound server, started and stopped explicitly.

    Use it as a context manager::

        with BackgroundServer(bind(handler, host=host, port=port), host=host) as server:
            print(server.url)
    """

    #: Thread name, so a stack dump says which server is running.
    thread_name = "netviz-http"

    def __init__(self, server: LocalServer, *, host: str) -> None:
        self._server = server
        self._thread: threading.Thread | None = None
        self.host = host
        self.port: int = server.server_address[1]

    @property
    def url(self) -> str:
        """The address to open in a browser.

        A wildcard bind is reported as loopback: ``http://0.0.0.0:8080/`` is
        the address the socket listens on, not one a browser can connect to.
        """
        return f"http://{authority(_WILDCARDS.get(self.host, self.host), self.port)}/"

    def start(self: _Server) -> _Server:
        """Begin answering requests on a daemon thread."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name=self.thread_name,
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

    def __enter__(self: _Server) -> _Server:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
