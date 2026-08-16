"""Who else has this inventory open, and what they are in the middle of.

A second tab was a race before this module: two pages with their own idea of the
tree, each finding out about the other's writes a second late and only as "the
revision moved". The revision check in
:meth:`~netviz.web.session.EditingSession.write_file` meant nothing was *lost*
— but nothing was *shown* either, so the second person found out by being
refused.

Presence is the other half of that: the state payload lists who is connected,
what each of them has selected, and which files they have unsaved edits in. The
page draws remote selections faintly on the canvas and badges a file somebody
else is editing.

**It is advisory and it says so.** No entry here blocks a write. The revision and
the content hash remain the only gates, because they are the only two facts the
server can check; a soft lock is a courtesy between people who can see each
other's cursor, and a hard one built on a heartbeat would be a way to lock an
inventory by closing a laptop lid.

**A client is present only while it keeps saying so.** Entries expire
(:data:`PRESENCE_TTL`), refreshed by the heartbeat of an open event stream or by
a ``POST /api/presence`` from a client on the polling fallback. A crashed tab
therefore stops being present without anybody having to notice, which is the
behaviour a badge on a file needs: a stale lock is worse than no lock.

**Ids are issued here, never accepted from a request.** A client sends back the
id it was given and gets it again if it is still known — which is what makes a
stream reconnect keep its identity — and a new one otherwise. Nothing a request
says can name another client's entry.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

__all__ = ["MAX_CLIENTS", "MAX_TRACKED", "PRESENCE_TTL", "Client", "Presence"]

#: Seconds a client stays listed without saying anything. Twice the stream
#: heartbeat plus a margin, so a live tab is never briefly declared gone.
PRESENCE_TTL: Final = 45.0

#: How many clients one session will track. Far above what a loopback editor
#: sees, and there so that a page in a reload loop cannot grow the list without
#: bound; the oldest idle entry is evicted rather than the request refused.
MAX_CLIENTS: Final = 64

#: How many selected addresses and dirty paths one client may claim. A remote
#: selection is drawn, so this bounds the work the *other* pages do on its
#: behalf, and bounds the state payload with it.
MAX_TRACKED: Final = 64


@dataclass(frozen=True, slots=True)
class Client:
    """One connected page, as everybody else sees it."""

    id: str
    #: What to call it in the list. Chosen by the server from the id, so that a
    #: client cannot label itself as somebody else.
    label: str
    #: Monotonic clock readings; never sent as such, see :meth:`to_dict`.
    joined: float
    seen: float
    #: Element addresses this client has selected, in the order it named them.
    selection: tuple[str, ...] = ()
    #: Inventory-relative paths it has unsaved edits in. What a soft lock badge
    #: is drawn from.
    editing: tuple[str, ...] = ()
    #: Which layer it is looking at, for the list. Free text from the client, so
    #: the page renders it with ``textContent`` like everything else.
    view: str | None = None
    #: Is it on the event stream, or on the polling fallback? Shown, because
    #: "why is the other tab a second behind" has an answer.
    streaming: bool = False

    def to_dict(self, *, now: float, me: str | None = None) -> dict[str, Any]:
        """The JSON the page lists.

        Ages rather than timestamps: the clock here is monotonic and means
        nothing to a browser, and "idle for 3 s" is what the reader wants
        anyway.
        """
        return {
            "id": self.id,
            "label": self.label,
            "self": self.id == me,
            "ageMs": max(0, round((now - self.joined) * 1000)),
            "idleMs": max(0, round((now - self.seen) * 1000)),
            "selection": list(self.selection),
            "editing": list(self.editing),
            "view": self.view,
            "streaming": self.streaming,
        }


@dataclass(eq=False)
class Presence:
    """The connected clients of one session, with expiry.

    Every mutating method answers with whether anything the *other* clients can
    see actually changed, so the session can publish a ``presence`` event for a
    real change and stay quiet for a heartbeat that moved nothing.
    """

    ttl: float = PRESENCE_TTL
    #: Injectable so a test can expire an entry without sleeping.
    clock: Callable[[], float] = time.monotonic

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _clients: dict[str, Client] = field(default_factory=dict, init=False, repr=False)
    _counter: int = field(default=0, init=False, repr=False)

    def join(self, *, streaming: bool = False, client_id: str | None = None) -> Client:
        """Take a client's id back, or issue one.

        ``client_id`` is what a page sends on reconnect. It is honoured only if
        it names an entry that is still alive: an unknown id — a stale tab, a
        restarted server, or a request making one up — gets a fresh identity
        rather than the entry it asked for.
        """
        now = self.clock()
        with self._lock:
            self._expire(now)
            known = self._clients.get(client_id) if client_id else None
            if known is not None:
                updated = replace(known, seen=now, streaming=streaming or known.streaming)
                self._clients[known.id] = updated
                return updated
            self._counter += 1
            # The random half keeps one client from guessing another's id; the
            # counter half keeps the label stable and readable in a list.
            identity = f"{self._counter}-{secrets.token_hex(8)}"
            client = Client(
                id=identity,
                label=f"client {self._counter}",
                joined=now,
                seen=now,
                streaming=streaming,
            )
            self._clients[identity] = client
            self._evict()
            return client

    def touch(self, client_id: str) -> bool:
        """Note that a client is still there. ``False`` if it is not known."""
        now = self.clock()
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return False
            self._clients[client_id] = replace(client, seen=now)
            return True

    def update(
        self,
        client_id: str,
        *,
        selection: Sequence[str] | None = None,
        editing: Sequence[str] | None = None,
        view: str | None = None,
        streaming: bool | None = None,
    ) -> tuple[Client | None, bool]:
        """Record what a client is doing.

        Returns:
            The client as it now is — ``None`` when the id is unknown, which the
            caller answers by issuing a new one — and whether anything visible to
            the others changed. ``False`` for a keepalive that moved nothing,
            which is what keeps a heartbeat from waking every other page.
        """
        now = self.clock()
        with self._lock:
            self._expire(now)
            client = self._clients.get(client_id)
            if client is None:
                return None, False
            updated = replace(
                client,
                seen=now,
                selection=_bounded(selection) if selection is not None else client.selection,
                editing=_bounded(editing) if editing is not None else client.editing,
                view=view if view is not None else client.view,
                streaming=client.streaming if streaming is None else streaming,
            )
            self._clients[client_id] = updated
            changed = (
                updated.selection != client.selection
                or updated.editing != client.editing
                or updated.view != client.view
                or updated.streaming != client.streaming
            )
            return updated, changed

    def leave(self, client_id: str) -> bool:
        """Drop a client. ``True`` if it was there, so a caller can publish."""
        with self._lock:
            return self._clients.pop(client_id, None) is not None

    def clients(self) -> tuple[Client, ...]:
        """Everyone still present, oldest first, expired entries dropped."""
        now = self.clock()
        with self._lock:
            self._expire(now)
            return tuple(
                sorted(self._clients.values(), key=lambda client: (client.joined, client.id))
            )

    def payload(self, *, me: str | None = None) -> list[dict[str, Any]]:
        """The client list as the state payload and the ``presence`` event carry it."""
        now = self.clock()
        return [client.to_dict(now=now, me=me) for client in self.clients()]

    def editing(self) -> dict[str, list[str]]:
        """Which clients claim unsaved edits in which file, path in key.

        The soft lock, in the shape the file list draws it: a page looks up its
        own row's path and badges it when somebody who is not itself is in there.
        """
        found: dict[str, list[str]] = {}
        for client in self.clients():
            for path in client.editing:
                found.setdefault(path, []).append(client.id)
        return found

    # -- internals -------------------------------------------------------

    def _expire(self, now: float) -> None:
        """Drop everyone who has not been heard from within the TTL."""
        stale = [
            identity for identity, client in self._clients.items() if now - client.seen > self.ttl
        ]
        for identity in stale:
            del self._clients[identity]

    def _evict(self) -> None:
        """Keep the list bounded by dropping the longest-silent entry."""
        while len(self._clients) > MAX_CLIENTS:
            oldest = min(self._clients.values(), key=lambda client: client.seen)
            del self._clients[oldest.id]


def _bounded(values: Iterable[str]) -> tuple[str, ...]:
    """At most :data:`MAX_TRACKED` distinct non-empty strings, in first-seen order."""
    seen: dict[str, None] = {}
    for value in values:
        if isinstance(value, str) and value and value not in seen:
            seen[value] = None
        if len(seen) >= MAX_TRACKED:
            break
    return tuple(seen)
