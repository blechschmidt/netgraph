"""The push channel: what changed, said once, to everyone who is looking.

The editing session already knows the exact moment a file moves — its own writes
go through :meth:`~netviz.web.session.EditingSession._commit`, and everything
else arrives at :class:`~netviz.web.session.TreeWatcher` — but until this
module there was no way to *tell* the browser. The page polled an integer once a
second and, whenever it moved, refetched the whole tree and re-rendered the whole
diagram. That is a second of latency on every keystroke's worth of work, a full
Graphviz run for a change that may not touch the drawn layer at all, and, with
two tabs open, a race rather than a feature.

So: one bus per session, and one event per thing that happened.

    tree-changed      the tree moved to a new revision; carries the files
    file-changed      one file's bytes are different; carries its new hash
    history-changed   the undo/redo stack moved
    disk-changed      the change came from outside this session
    presence          who is connected, what they have selected, what is dirty
    hello             the first frame of a stream: your client id, the revision
    resync            "I lost your place": refetch everything

Four properties this has to have, and each one is a line of defence against the
failure mode of push channels — a client that quietly believes something untrue:

**Every event is numbered.** The ids are monotonic within a session and are sent
as SSE ``id:`` fields, so a browser that reconnects sends ``Last-Event-ID`` and
gets exactly what it missed out of a bounded ring buffer
(:data:`RING_CAPACITY`). A resume point that has already fallen out of the ring
is not guessed at: the stream opens with ``resync`` and the client refetches.

**Every event is revision-stamped.** A client that receives an event for a
revision it has already passed can drop it, which is what makes the replay above
idempotent.

**A slow consumer is dropped, not buffered.** Each subscription has a bounded
queue; a client that cannot keep up gets ``resync`` instead of an unbounded
backlog in the server, and a stalled tab cannot make the process grow.

**The channel is advisory.** Nothing about correctness depends on an event
arriving. The revision precondition in
:meth:`~netviz.web.session.EditingSession.apply` and the content hash in
:meth:`~netviz.web.session.EditingSession.write_file` remain the only gates on
a write, so a client on the polling path — a proxy that buffers, ``curl``, the
tests that use plain GETs — is exactly as safe as one on the stream, only
slower.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "EVENTS_PATH",
    "EVENT_NAMES",
    "HEARTBEAT_SECONDS",
    "MAX_STREAMS",
    "PRESENCE_PATH",
    "QUEUE_CAPACITY",
    "RING_CAPACITY",
    "Event",
    "EventBus",
    "Subscription",
    "TooManyStreams",
    "heartbeat_frame",
]

#: Where the stream is served. Named here rather than in the routing table so
#: that the state payload can point a client at it — "subscribe here, from this
#: event id" is part of what the state *is* — without the session importing the
#: server it is served by.
EVENTS_PATH: Final = "/api/events"

#: Where a client says what it is doing. The other half of the same contract:
#: a client on the polling fallback keeps its presence alive by posting here.
PRESENCE_PATH: Final = "/api/presence"

#: How many past events a session keeps for ``Last-Event-ID`` resume. A reconnect
#: happens within seconds, and a session that produced 256 events while one tab
#: was disconnected is one where refetching is cheaper than replaying anyway.
RING_CAPACITY: Final = 256

#: How many events one subscription may fall behind before it is resynchronised
#: instead. Smaller than the ring on purpose: a client this far behind is not
#: reading, and the honest answer to it is "start again", not a growing queue.
QUEUE_CAPACITY: Final = 64

#: How many event streams one session will hold open. A browser opens one per
#: tab; the limit is there so that a page in a reload loop cannot pin a thread
#: per attempt, since the stdlib server serves each connection on its own thread.
MAX_STREAMS: Final = 32

#: Seconds between heartbeat comments on an idle stream. Under the 60 seconds
#: most proxies and browsers give up after, and short enough that a client's
#: presence entry stays fresh without a separate keepalive request.
HEARTBEAT_SECONDS: Final = 15.0

#: Every event name this bus emits. Published as part of the state payload so a
#: client — or a reader of the API — can see the vocabulary without reading this
#: file, and so a test can assert the set has not silently grown.
EVENT_NAMES: Final[tuple[str, ...]] = (
    "hello",
    "tree-changed",
    "file-changed",
    "history-changed",
    "disk-changed",
    "presence",
    "resync",
)


class TooManyStreams(Exception):
    """Raised when :data:`MAX_STREAMS` streams are already open on a session."""


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, numbered and stamped with the revision it left.

    ``revision`` is the tree revision *after* the event, so a client can compare
    it with what it holds and skip anything it has already caught up with. Events
    that say nothing about the tree — ``presence`` above all — carry the revision
    current when they were published, which is the same comparison and always
    passes.
    """

    id: int
    name: str
    revision: int
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The JSON payload, which always carries its own id and revision.

        Duplicated with the SSE ``id:`` field on purpose: a client on the polling
        fallback receives these objects out of a plain JSON array, where there is
        no framing to carry them.
        """
        return {"id": self.id, "event": self.name, "revision": self.revision, **dict(self.data)}

    def frame(self) -> bytes:
        """This event as one ``text/event-stream`` frame.

        ``json.dumps`` never emits a newline inside a string, so the payload is
        always a single ``data:`` line and the framing cannot be broken by
        anything a file name or an error message contains.
        """
        payload = json.dumps(self.to_dict(), separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.name}\ndata: {payload}\n\n".encode()


def heartbeat_frame() -> bytes:
    """A comment frame: keeps the connection and the client's presence alive.

    A comment rather than an event so that it costs a reader nothing — no
    handler runs in the browser — while still being bytes on the wire, which is
    what a proxy's idle timeout and a half-open TCP connection both need.
    """
    return b": heartbeat\n\n"


class Subscription:
    """One open stream's view of the bus: a bounded queue and a way to wait.

    Never constructed directly; :meth:`EventBus.subscribe` makes one, and it must
    be closed — the bus holds a reference until it is, and a subscription that is
    never closed is a slot of :data:`MAX_STREAMS` that never comes back.
    """

    def __init__(self, bus: EventBus, backlog: tuple[Event, ...], *, gap: bool) -> None:
        self._bus = bus
        self._pending: deque[Event] = deque(backlog, maxlen=QUEUE_CAPACITY)
        self._condition = threading.Condition()
        self._closed = False
        #: The resume point had already fallen out of the ring, or this
        #: subscription later fell behind: the client has to refetch rather than
        #: apply what follows to a state it cannot know.
        self.gap = gap

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def offer(self, event: Event) -> None:
        """Queue one event, or mark a gap if this stream has fallen behind.

        Called by the bus, under its lock. Dropping the oldest event and
        remembering that we did is the whole of the back-pressure policy: a
        client that is not reading gets told to start again, and the server's
        memory does not grow to accommodate it.
        """
        with self._condition:
            if self._closed:
                return
            if len(self._pending) == QUEUE_CAPACITY:
                self.gap = True
                self._pending.clear()
            self._pending.append(event)
            self._condition.notify()

    def wait(self, timeout: float) -> tuple[Event, ...]:
        """Everything queued, waiting up to ``timeout`` seconds for the first.

        Returns an empty tuple when nothing arrived, which is the stream's cue to
        send a heartbeat, and also when the subscription has been closed — a
        caller loops on :attr:`closed`, so the two do not need distinguishing.
        """
        with self._condition:
            if not self._pending and not self._closed:
                self._condition.wait(timeout)
            drained = tuple(self._pending)
            self._pending.clear()
            return drained

    def close(self) -> None:
        """Release the slot. Safe to call twice, and from any thread."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()
        self._bus.release(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class EventBus:
    """Every change to one session, numbered, kept briefly, and fanned out.

    Thread-safe in both directions: the writer is whichever request thread
    committed, or the watcher thread, and the readers are the stream threads.
    """

    def __init__(self, *, capacity: int = RING_CAPACITY, max_streams: int = MAX_STREAMS) -> None:
        self._lock = threading.Lock()
        self._ring: deque[Event] = deque(maxlen=capacity)
        self._subscriptions: list[Subscription] = []
        self._next_id = 1
        self._max_streams = max_streams
        self._closed = False

    # -- publishing ------------------------------------------------------

    @property
    def last_id(self) -> int:
        """The id of the most recent event; ``0`` before anything was published."""
        with self._lock:
            return self._next_id - 1

    @property
    def streams(self) -> int:
        """How many streams are open right now."""
        with self._lock:
            return len(self._subscriptions)

    def publish(self, name: str, *, revision: int, **data: Any) -> Event:
        """Number one event, remember it, and hand it to every open stream.

        Returns the event, so a caller that wants to answer a request with what
        it just published — which is what a committed gesture does — does not
        have to reconstruct it.
        """
        with self._lock:
            event = Event(id=self._next_id, name=name, revision=revision, data=data)
            self._next_id += 1
            self._ring.append(event)
            subscriptions = tuple(self._subscriptions)
        for subscription in subscriptions:
            subscription.offer(event)
        return event

    # -- subscribing -----------------------------------------------------

    def subscribe(self, last_id: int | None = None) -> Subscription:
        """Open a stream, replaying whatever ``last_id`` missed.

        Args:
            last_id: The ``Last-Event-ID`` a reconnecting client sent, or
                ``None`` for a fresh stream. A fresh stream starts with no
                backlog and no gap: it is about to fetch the state anyway.

        Raises:
            TooManyStreams: :data:`MAX_STREAMS` are already open.
        """
        with self._lock:
            if len(self._subscriptions) >= self._max_streams:
                raise TooManyStreams(
                    f"this session already has {self._max_streams} event streams open; "
                    f"close a tab, or fall back to polling /api/state"
                )
            backlog, gap = self._replay(last_id)
            subscription = Subscription(self, backlog, gap=gap)
            self._subscriptions.append(subscription)
            return subscription

    def _replay(self, last_id: int | None) -> tuple[tuple[Event, ...], bool]:
        """What a resuming client missed, and whether anything was lost.

        Called under the lock. A resume point *ahead* of what this bus has
        published is a client talking to a restarted server: its ids mean
        nothing here, so it is a gap rather than an empty backlog.
        """
        if last_id is None:
            return (), False
        if last_id >= self._next_id - 1:
            return (), last_id > self._next_id - 1
        oldest = self._ring[0].id if self._ring else self._next_id
        return tuple(event for event in self._ring if event.id > last_id), last_id + 1 < oldest

    def release(self, subscription: Subscription) -> None:
        """Forget a closed subscription. Called by :meth:`Subscription.close`."""
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

    def history(self, after: int = 0) -> tuple[Event, ...]:
        """Every remembered event newer than ``after``.

        The polling fallback's other half: a client that cannot hold a stream
        open asks for this alongside the state, and gets the same events with the
        same ids — so the two paths run the same handlers and cannot drift into
        two ideas of what an event means.
        """
        with self._lock:
            return tuple(event for event in self._ring if event.id > after)

    def close(self) -> None:
        """Wake and close every stream. Called when the server is stopping."""
        with self._lock:
            self._closed = True
            subscriptions = tuple(self._subscriptions)
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription.close()

    def __iter__(self) -> Iterator[Event]:
        """The remembered events, oldest first. For tests and for debugging."""
        return iter(self.history())
