"""The push channel, presence, and what two clients do to each other.

``netgraph web DIR`` used to be a page polling an integer. This file covers what
replaced it and the properties that replacement must not cost:

* **The bus is honest about what it lost.** Events are numbered, replayed from a
  bounded ring for a client that says where it left off, and a resume point that
  has fallen out of the ring produces a *resync* rather than a plausible partial
  replay. A subscriber that falls behind is dropped, not buffered.
* **The stream is an optimisation.** Everything it carries is also answerable by
  a plain ``GET``, the polling path replays the same events out of the same
  ring, and no write is gated on having read one. A client that cannot hold a
  connection open is slower and not less correct.
* **Incremental is actually incremental.** ``/api/tree?path=`` answers for one
  file rather than the tree, and ``/api/graph?known=`` answers "nothing moved"
  without running Graphviz when the drawing would be identical.
* **Presence is advisory.** It lists who is connected and what they are in, it
  expires on its own, and it blocks nothing: the revision precondition and the
  content hash remain the only gates.
* **The conflict story holds under concurrency.** Two clients saving the same
  file, a client saving while the file moved on disk, and an undo issued in one
  tab landing in the other.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from netgraph.web.events import (
    EVENT_NAMES,
    QUEUE_CAPACITY,
    Event,
    EventBus,
    TooManyStreams,
)
from netgraph.web.presence import Presence
from netgraph.web.preview import ViewOptions, graph_digest
from netgraph.web.server import WebServer
from netgraph.web.session import EditingSession

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_LAB = REPO_ROOT / "examples" / "home-lab"

#: A file every fixture edits, and the one every conflict test races over.
TARGET = "hosts/srv-nas.yaml"

#: How long a test waits for something that crosses a thread. Generous: a
#: loopback round trip is microseconds and a busy CI machine is not.
TIMEOUT = 10.0


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "inventory"
    shutil.copytree(HOME_LAB, root)
    return root


@pytest.fixture
def session(tree: Path) -> EditingSession:
    return EditingSession(root=tree, writable=True)


@pytest.fixture
def served(session: EditingSession) -> Iterator[str]:
    with WebServer.create(session=session, host="127.0.0.1", port=0) as server:
        yield server.url.rstrip("/")


# --------------------------------------------------------------------------- #
# The bus
# --------------------------------------------------------------------------- #


def test_events_are_numbered_and_carry_their_revision() -> None:
    bus = EventBus()
    first = bus.publish("tree-changed", revision=2, files=["a.yaml"])
    second = bus.publish("history-changed", revision=2, undo=1, redo=0)
    assert (first.id, second.id) == (1, 2)
    assert first.to_dict() == {
        "id": 1,
        "event": "tree-changed",
        "revision": 2,
        "files": ["a.yaml"],
    }
    assert bus.last_id == 2


def test_a_frame_is_one_data_line_whatever_the_payload_holds() -> None:
    """The framing must not be breakable by a file name or an error message."""
    event = Event(id=7, name="file-changed", revision=3, data={"path": "a\nb\r\nc.yaml"})
    frame = event.frame().decode()
    assert frame.startswith("id: 7\nevent: file-changed\ndata: ")
    assert frame.endswith("\n\n")
    assert len([line for line in frame.splitlines() if line.startswith("data:")]) == 1
    assert json.loads(frame.splitlines()[2][len("data: ") :])["path"] == "a\nb\r\nc.yaml"


def test_a_resuming_subscriber_is_given_exactly_what_it_missed() -> None:
    bus = EventBus()
    for index in range(5):
        bus.publish("tree-changed", revision=index)
    with bus.subscribe(last_id=2) as subscription:
        assert not subscription.gap
        assert [event.id for event in subscription.wait(0)] == [3, 4, 5]


def test_a_resume_point_that_fell_out_of_the_ring_is_a_gap_not_a_guess() -> None:
    bus = EventBus(capacity=3)
    for index in range(6):
        bus.publish("tree-changed", revision=index)
    with bus.subscribe(last_id=1) as subscription:
        # Events 2 and 3 are gone. Replaying 4 onwards would be a patch applied
        # to a state the client cannot have.
        assert subscription.gap


def test_a_resume_point_ahead_of_the_bus_is_a_gap_too() -> None:
    """A client talking to a restarted server: its ids mean nothing here."""
    bus = EventBus()
    bus.publish("tree-changed", revision=1)
    with bus.subscribe(last_id=99) as subscription:
        assert subscription.gap


def test_a_fresh_subscriber_gets_no_backlog_and_no_gap() -> None:
    bus = EventBus()
    bus.publish("tree-changed", revision=1)
    with bus.subscribe() as subscription:
        assert not subscription.gap
        assert subscription.wait(0) == ()


def test_a_subscriber_that_falls_behind_is_dropped_rather_than_buffered() -> None:
    bus = EventBus()
    with bus.subscribe() as subscription:
        for index in range(QUEUE_CAPACITY + 5):
            bus.publish("tree-changed", revision=index)
        assert subscription.gap
        assert len(subscription.wait(0)) <= QUEUE_CAPACITY


def test_the_number_of_streams_is_bounded() -> None:
    bus = EventBus(max_streams=2)
    first, second = bus.subscribe(), bus.subscribe()
    with pytest.raises(TooManyStreams):
        bus.subscribe()
    first.close()
    assert bus.streams == 1
    bus.subscribe().close()
    second.close()


def test_closing_the_bus_wakes_and_closes_every_stream() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    woken = threading.Event()

    def wait() -> None:
        subscription.wait(TIMEOUT)
        woken.set()

    reader = threading.Thread(target=wait, daemon=True)
    reader.start()
    bus.close()
    assert woken.wait(TIMEOUT)
    assert subscription.closed


def test_publishing_reaches_a_waiting_subscriber() -> None:
    bus = EventBus()
    with bus.subscribe() as subscription:
        seen: list[Event] = []

        def wait() -> None:
            seen.extend(subscription.wait(TIMEOUT))

        reader = threading.Thread(target=wait, daemon=True)
        reader.start()
        time.sleep(0.05)
        bus.publish("presence", revision=1, clients=[])
        reader.join(TIMEOUT)
        assert [event.name for event in seen] == ["presence"]


# --------------------------------------------------------------------------- #
# What a session publishes
# --------------------------------------------------------------------------- #


def test_a_write_announces_the_file_the_tree_and_the_history(session: EditingSession) -> None:
    body = session.read_file(TARGET)
    session.write_file(TARGET, body["text"] + "\n# noted\n", base_hash=body["hash"])
    published = [(event.name, dict(event.data)) for event in session.events]
    names = [name for name, _ in published]
    assert names == ["file-changed", "tree-changed", "history-changed"]
    assert published[0][1]["path"] == TARGET
    assert published[0][1]["origin"] == "session"
    assert published[1][1]["files"] == [TARGET]
    assert published[2][1]["undo"] == 1


def test_a_write_that_changes_nothing_announces_nothing(session: EditingSession) -> None:
    body = session.read_file(TARGET)
    session.write_file(TARGET, body["text"], base_hash=body["hash"])
    assert list(session.events) == []


def test_a_change_on_disk_is_announced_as_one(session: EditingSession, tree: Path) -> None:
    """What :class:`TreeWatcher` does, without waiting on a filesystem event."""
    (tree / TARGET).write_text((tree / TARGET).read_text() + "\n# elsewhere\n", encoding="utf-8")
    session.invalidate([str(tree / TARGET)])
    names = [event.name for event in session.events]
    assert names == ["file-changed", "disk-changed", "tree-changed"]
    disk = next(event for event in session.events if event.name == "disk-changed")
    assert disk.data["files"] == [TARGET]
    assert disk.data["outside"] is False


def test_a_change_outside_the_inventory_names_no_file(session: EditingSession, tree: Path) -> None:
    """The config file, an editor's swap file: the revision moves, no row does."""
    session.invalidate([str(tree / "netgraph.toml"), "/somewhere/else.yaml"])
    tree_changed = next(event for event in session.events if event.name == "tree-changed")
    assert tree_changed.data["files"] == []
    assert tree_changed.data["outside"] is True


def test_the_state_says_where_to_subscribe_and_from_which_event(session: EditingSession) -> None:
    session.events.publish("presence", revision=1, clients=[])
    state = session.state()
    assert state["events"]["path"] == "/api/events"
    assert state["events"]["lastEventId"] == 1
    assert set(state["events"]["names"]) == set(EVENT_NAMES)


# --------------------------------------------------------------------------- #
# Incremental
# --------------------------------------------------------------------------- #


def test_the_tree_can_be_asked_about_one_file(session: EditingSession) -> None:
    whole = session.tree()
    partial = session.tree([TARGET])
    assert partial["partial"] is True
    assert [entry["path"] for entry in partial["files"]] == [TARGET]
    assert partial["missing"] == []
    one = next(entry for entry in whole["files"] if entry["path"] == TARGET)
    assert partial["files"][0] == one, "a partial row must equal the row a full fetch gives"


def test_a_partial_fetch_names_what_is_no_longer_there(session: EditingSession) -> None:
    partial = session.tree([TARGET, "hosts/gone.yaml"])
    assert partial["missing"] == ["hosts/gone.yaml"]
    assert [entry["path"] for entry in partial["files"]] == [TARGET]


def test_a_partial_fetch_still_checks_the_path(session: EditingSession) -> None:
    from netgraph.web.session import SessionError

    with pytest.raises(SessionError):
        session.tree(["../../etc/passwd"])


def test_diagnostics_can_be_left_out_of_a_partial_fetch(session: EditingSession) -> None:
    assert "diagnostics" not in session.tree([TARGET], diagnostics=False)
    assert "diagnostics" in session.tree([TARGET], diagnostics=True)


@requires_dot
def test_a_graph_whose_drawing_did_not_move_is_not_drawn_again(
    session: EditingSession, tree: Path
) -> None:
    """The point of the fingerprint: an edit that changes no drawn thing.

    A description is not on the diagram, so the DOT is byte-for-byte what it
    was — and the client already holds the picture, so there is nothing to send
    and nothing for Graphviz to do.
    """
    view = ViewOptions()
    first, _ = session.graph(view)
    assert first.graph_hash and first.svg and not first.unchanged

    body = session.read_file(TARGET)
    session.write_file(
        TARGET,
        body["text"].replace("description: Backup and media server", "description: Changed"),
        base_hash=body["hash"],
    )

    again, revision = session.graph(view, known=first.graph_hash)
    assert again.unchanged is True
    assert again.svg is None
    assert again.graph_hash == first.graph_hash
    assert revision == session.revision, "an unchanged drawing is still this revision's"


@requires_dot
def test_a_graph_that_did_move_is_drawn_and_fingerprinted_afresh(
    session: EditingSession,
) -> None:
    view = ViewOptions()
    first, _ = session.graph(view)
    body = session.read_file(TARGET)
    # An address is printed on the node, so this one really does redraw.
    session.write_file(
        TARGET,
        body["text"].replace("192.168.10.10/24", "192.168.10.11/24"),
        base_hash=body["hash"],
    )
    again, _ = session.graph(view, known=first.graph_hash)
    assert again.unchanged is False
    assert again.svg is not None
    assert again.graph_hash != first.graph_hash


@requires_dot
def test_a_fingerprint_distinguishes_the_views_it_has_to(session: EditingSession) -> None:
    """One hash per (graph, options): sending l1's for l2 must simply miss."""
    from netgraph.render import Layer

    digests = {
        layer: session.graph(ViewOptions(layer=layer))[0].graph_hash
        for layer in (Layer.L1, Layer.L2, Layer.L3)
    }
    assert len(set(digests.values())) == 3
    crossed, _ = session.graph(ViewOptions(layer=Layer.L2), known=digests[Layer.L1])
    assert crossed.unchanged is False


@requires_dot
def test_the_fingerprint_is_of_the_drawing_not_of_the_tree(session: EditingSession) -> None:
    """Same graph, same options, same hash — computed twice from scratch."""
    from netgraph.render import build_graph, filter_graph

    inventory = session.inventory()
    options = ViewOptions()
    graph = filter_graph(build_graph(inventory, layer=options.layer), options.filter_spec)
    assert graph_digest(graph, options.render_options) == session.graph(options)[0].graph_hash


# --------------------------------------------------------------------------- #
# Presence
# --------------------------------------------------------------------------- #


def test_a_client_is_issued_an_id_and_gets_it_back_on_reconnect() -> None:
    presence = Presence()
    first = presence.join(streaming=True)
    again = presence.join(streaming=True, client_id=first.id)
    assert again.id == first.id
    assert [client.id for client in presence.clients()] == [first.id]


def test_an_id_a_request_invents_is_not_honoured() -> None:
    presence = Presence()
    client = presence.join(client_id="somebody-elses-id")
    assert client.id != "somebody-elses-id"
    assert presence.update("somebody-elses-id", selection=["x"]) == (None, False)


def test_presence_expires_without_anybody_having_to_notice() -> None:
    now = [1000.0]
    presence = Presence(ttl=30.0, clock=lambda: now[0])
    client = presence.join()
    now[0] += 31.0
    assert presence.clients() == ()
    assert not presence.touch(client.id)


def test_a_keepalive_that_moved_nothing_is_not_announced() -> None:
    presence = Presence()
    client = presence.join()
    presence.update(client.id, selection=["switches/sw"], editing=[])
    _, changed = presence.update(client.id, selection=["switches/sw"], editing=[])
    assert changed is False


def test_what_a_client_claims_is_bounded() -> None:
    presence = Presence()
    client = presence.join()
    updated, _ = presence.update(client.id, selection=[f"a{index}" for index in range(500)])
    assert updated is not None
    assert len(updated.selection) == 64


def test_the_soft_lock_is_keyed_by_path() -> None:
    presence = Presence()
    one, two = presence.join(), presence.join()
    presence.update(one.id, editing=[TARGET])
    presence.update(two.id, editing=[TARGET, "routers/rtr-home.yaml"])
    assert presence.editing() == {
        TARGET: [one.id, two.id],
        "routers/rtr-home.yaml": [two.id],
    }


def test_a_session_lists_its_clients_and_their_soft_locks(session: EditingSession) -> None:
    client = session.join()
    session.report(client.id, selection=["hosts/srv-nas"], editing=[TARGET])
    state = session.state(me=client.id)
    assert [entry["id"] for entry in state["clients"]] == [client.id]
    assert state["clients"][0]["self"] is True
    assert state["clients"][0]["selection"] == ["hosts/srv-nas"]
    assert state["editing"] == {TARGET: [client.id]}


def test_a_soft_lock_blocks_nothing(session: EditingSession) -> None:
    """Advisory means advisory: the other client still writes the file."""
    holder = session.join()
    session.report(holder.id, editing=[TARGET])
    body = session.read_file(TARGET)
    other = session.join()
    change = session.write_file(
        TARGET, body["text"] + "\n# by the other one\n", base_hash=body["hash"], client=other.id
    )
    assert change.files[TARGET] is not None


def test_a_stream_that_ends_leaves_the_client_a_moment_to_come_back(
    session: EditingSession,
) -> None:
    client = session.join(streaming=True)
    session.stream_ended(client.id)
    listed = session.presence.clients()
    assert [entry.id for entry in listed] == [client.id]
    assert listed[0].streaming is False
    session.leave(client.id)
    assert session.presence.clients() == ()


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def call(base: str, path: str, method: str = "GET", body: Any = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class Stream:
    """One open ``/api/events`` connection, read on a thread.

    A real ``EventSource`` is a browser's; ``tests/test_browser.py`` exercises
    that. Here the point is the wire format and the server's behaviour, so this
    parses frames out of the socket and hands them over as dictionaries.
    """

    def __init__(self, base: str, *, client: str | None = None, last_event_id: int | None = None):
        query = f"?client={client}" if client else ""
        headers = {} if last_event_id is None else {"Last-Event-ID": str(last_event_id)}
        self._request = urllib.request.Request(base + "/api/events" + query, headers=headers)
        self.events: list[dict[str, Any]] = []
        self.comments = 0
        self.status = 0
        self._arrived = threading.Condition()
        self._stop = threading.Event()
        self._response: Any = None
        self._thread = threading.Thread(target=self._read, daemon=True)

    def __enter__(self) -> Stream:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _read(self) -> None:
        try:
            self._response = urllib.request.urlopen(self._request, timeout=TIMEOUT)
            self.status = self._response.status
            frame: dict[str, str] = {}
            for raw in self._response:
                if self._stop.is_set():
                    return
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith(":"):
                    self.comments += 1
                elif line.startswith(("id:", "event:", "data:")):
                    key, _, value = line.partition(":")
                    frame[key] = value.strip()
                elif not line and frame:
                    payload = json.loads(frame["data"])
                    payload["_id"] = int(frame["id"])
                    payload["_name"] = frame["event"]
                    with self._arrived:
                        self.events.append(payload)
                        self._arrived.notify_all()
                    frame = {}
        except Exception:  # pragma: no cover - the socket closing under us
            return

    def wait_for(self, name: str, *, after: int = 0) -> dict[str, Any]:
        """The next event called ``name`` at or after index ``after``."""
        deadline = time.monotonic() + TIMEOUT
        with self._arrived:
            while time.monotonic() < deadline:
                for event in self.events[after:]:
                    if event["_name"] == name:
                        return event
                self._arrived.wait(0.2)
        raise AssertionError(f"no {name!r} event arrived; saw {[e['_name'] for e in self.events]}")

    def close(self) -> None:
        self._stop.set()
        if self._response is not None:
            self._response.close()
        self._thread.join(timeout=TIMEOUT)


def test_the_stream_opens_with_a_hello_and_the_right_content_type(served: str) -> None:
    with Stream(served) as stream:
        hello = stream.wait_for("hello")
        assert stream.status == 200
        assert hello["client"] and hello["label"]
        assert hello["resync"] is False
        assert hello["heartbeatMs"] > 0


def test_a_write_reaches_an_open_stream(served: str) -> None:
    with Stream(served) as stream:
        stream.wait_for("hello")
        _, body = call(served, f"/api/file/{TARGET}")
        status, _ = call(
            served,
            f"/api/file/{TARGET}",
            "PUT",
            {"text": body["text"] + "\n# pushed\n", "hash": body["hash"]},
        )
        assert status == 200
        moved = stream.wait_for("file-changed")
        assert moved["path"] == TARGET
        assert moved["hash"] != body["hash"]
        assert stream.wait_for("tree-changed")["files"] == [TARGET]
        assert stream.wait_for("history-changed")["undo"] == 1


def test_a_stream_resumes_from_the_event_it_names(served: str, session: EditingSession) -> None:
    body = session.read_file(TARGET)
    session.write_file(TARGET, body["text"] + "\n# before\n", base_hash=body["hash"])
    published = session.events.last_id
    with Stream(served, last_event_id=1) as stream:
        assert stream.wait_for("hello")["resync"] is False
        # Events 2 and 3 were published before this stream existed, and arrive
        # on it anyway: that is what resuming means.
        replayed = stream.wait_for("tree-changed")
        assert replayed["id"] <= published
        assert stream.wait_for("history-changed")["undo"] == 1


def test_a_resume_point_that_is_gone_opens_with_resync(served: str) -> None:
    with Stream(served, last_event_id=10_000) as stream:
        assert stream.wait_for("hello")["resync"] is True


def test_a_head_of_the_stream_does_not_hold_a_thread(served: str) -> None:
    request = urllib.request.Request(served + "/api/events", method="HEAD")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
        assert response.headers.get("Content-Length") is None


def test_the_polling_path_replays_the_same_events(served: str) -> None:
    """The fallback is the same events with the same ids, not a lesser answer."""
    status, before = call(served, "/api/state?since=0")
    assert status == 200
    _, body = call(served, f"/api/file/{TARGET}")
    call(
        served,
        f"/api/file/{TARGET}",
        "PUT",
        {"text": body["text"] + "\n# polled\n", "hash": body["hash"]},
    )
    _, after = call(served, f"/api/state?since={before['events']['lastEventId']}")
    replay = after["events"]["replay"]
    assert [event["event"] for event in replay] == [
        "file-changed",
        "tree-changed",
        "history-changed",
    ]
    assert replay[0]["path"] == TARGET
    assert after["revision"] > before["revision"]


def test_a_plain_get_client_never_has_to_know_the_stream_exists(served: str) -> None:
    """The tests that predate this channel, and curl: still the whole API."""
    assert call(served, "/api/state")[0] == 200
    assert call(served, "/api/tree")[0] == 200
    assert call(served, f"/api/file/{TARGET}")[0] == 200


def test_the_tree_route_answers_for_named_files_only(served: str) -> None:
    status, partial = call(served, f"/api/tree?path={TARGET}&diagnostics=0")
    assert status == 200
    assert partial["partial"] is True
    assert [entry["path"] for entry in partial["files"]] == [TARGET]
    assert "diagnostics" not in partial


def test_the_tree_route_refuses_a_path_that_is_not_in_the_inventory(served: str) -> None:
    status, body = call(served, "/api/tree?path=../../etc/passwd")
    assert status == 400
    assert body["status"] == "failed"


@requires_dot
def test_the_graph_route_answers_unchanged_for_a_fingerprint_it_recognises(served: str) -> None:
    status, first = call(served, "/api/graph?view=l2")
    assert status == 200 and first["graphHash"]
    status, again = call(served, f"/api/graph?view=l2&known={first['graphHash']}")
    assert status == 200
    assert again["unchanged"] is True
    assert again["svg"] is None
    assert again["counts"] == first["counts"]


@requires_dot
def test_a_nonsense_fingerprint_costs_a_render_and_nothing_else(served: str) -> None:
    status, body = call(served, "/api/graph?view=l2&known=not-a-hash")
    assert status == 200
    assert body["unchanged"] is False
    assert body["svg"].startswith("<svg")


def test_presence_is_posted_and_listed(served: str) -> None:
    status, joined = call(served, "/api/presence", "POST", {})
    assert status == 200 and joined["client"]
    status, updated = call(
        served,
        "/api/presence",
        "POST",
        {"client": joined["client"], "selection": ["hosts/srv-nas"], "editing": [TARGET]},
    )
    assert status == 200
    assert updated["clients"][0]["selection"] == ["hosts/srv-nas"]
    _, state = call(served, f"/api/state?client={joined['client']}")
    assert state["editing"] == {TARGET: [joined["client"]]}


def test_a_leaving_client_is_dropped_at_once(served: str) -> None:
    _, joined = call(served, "/api/presence", "POST", {})
    status, left = call(
        served, "/api/presence", "POST", {"client": joined["client"], "leaving": True}
    )
    assert status == 200 and left["left"] is True
    assert call(served, "/api/state")[1]["clients"] == []


def test_presence_survives_a_read_only_session(tree: Path) -> None:
    """Two people can browse. Knowing where the other is looking still helps."""
    with WebServer.create(
        session=EditingSession(root=tree, writable=False), host="127.0.0.1", port=0
    ) as server:
        base = server.url.rstrip("/")
        status, joined = call(base, "/api/presence", "POST", {})
        assert status == 200
        assert call(base, "/api/presence", "POST", {"client": joined["client"]})[0] == 200
        assert call(base, "/api/undo", "POST")[0] == 403, "and still writes nothing"


def test_junk_in_a_presence_body_is_dropped_not_refused(served: str) -> None:
    _, joined = call(served, "/api/presence", "POST", {})
    status, body = call(
        served,
        "/api/presence",
        "POST",
        {"client": joined["client"], "selection": "not-a-list", "editing": [1, None, "a.yaml"]},
    )
    assert status == 200
    assert body["clients"][0]["editing"] == ["a.yaml"]


def test_the_scratchpad_has_no_stream_and_no_presence() -> None:
    with WebServer.create(source="", host="127.0.0.1", port=0) as server:
        base = server.url.rstrip("/")
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "/api/events", timeout=TIMEOUT)
        assert missing.value.code == 404
        request = urllib.request.Request(
            base + "/api/presence",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=TIMEOUT)
        assert refused.value.code == 404


def test_the_stream_is_refused_from_another_host(served: str) -> None:
    """The loopback promise covers the new surface exactly as it covers the old."""
    request = urllib.request.Request(served + "/api/events", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=TIMEOUT)
    assert refused.value.code == 421


# --------------------------------------------------------------------------- #
# Two clients, one tree
# --------------------------------------------------------------------------- #


def test_two_clients_saving_the_same_file_and_the_second_is_refused(served: str) -> None:
    """Both read the same bytes; the second write states what is no longer true."""
    _, one = call(served, "/api/presence", "POST", {})
    _, two = call(served, "/api/presence", "POST", {})
    _, body = call(served, f"/api/file/{TARGET}")

    status, first = call(
        served,
        f"/api/file/{TARGET}",
        "PUT",
        {"text": body["text"] + "\n# from one\n", "hash": body["hash"], "client": one["client"]},
    )
    assert status == 200

    status, refusal = call(
        served,
        f"/api/file/{TARGET}",
        "PUT",
        {"text": body["text"] + "\n# from two\n", "hash": body["hash"], "client": two["client"]},
    )
    assert status == 409
    assert refusal["conflict"]["path"] == TARGET
    assert refusal["conflict"]["hash"] == first["files"][TARGET]["hash"]

    # Nothing of the first write was lost, and the second wrote nothing at all.
    _, now = call(served, f"/api/file/{TARGET}")
    assert now["text"].endswith("# from one\n")

    # Told what is really there, the second client can decide to overwrite.
    status, forced = call(
        served,
        f"/api/file/{TARGET}",
        "PUT",
        {"text": now["text"] + "# then two\n", "hash": now["hash"], "client": two["client"]},
    )
    assert status == 200 and forced["files"][TARGET]["state"] == "written"


def test_two_clients_racing_the_same_write_leave_one_winner(served: str) -> None:
    """The same race, run concurrently rather than in order.

    Both quote the same hash from real threads; exactly one may land, because
    the check and the write happen under the session's lock.
    """
    _, body = call(served, f"/api/file/{TARGET}")
    results: list[int] = []
    barrier = threading.Barrier(4)

    def save(marker: int) -> None:
        barrier.wait(timeout=TIMEOUT)
        status, _ = call(
            served,
            f"/api/file/{TARGET}",
            "PUT",
            {"text": body["text"] + f"\n# racer {marker}\n", "hash": body["hash"]},
        )
        results.append(status)

    threads = [threading.Thread(target=save, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=TIMEOUT)

    assert sorted(results) == [200, 409, 409, 409]


def test_a_client_saving_while_the_file_changed_on_disk_is_refused(
    served: str, session: EditingSession, tree: Path
) -> None:
    """``$EDITOR`` wins the race, and the browser is told rather than obeyed."""
    _, body = call(served, f"/api/file/{TARGET}")
    with Stream(served) as stream:
        stream.wait_for("hello")
        elsewhere = body["text"] + "\n# written in $EDITOR\n"
        (tree / TARGET).write_text(elsewhere, encoding="utf-8")
        session.invalidate([str(tree / TARGET)])

        moved = stream.wait_for("file-changed")
        assert moved["origin"] == "disk"
        assert moved["path"] == TARGET

        status, refusal = call(
            served,
            f"/api/file/{TARGET}",
            "PUT",
            {"text": body["text"] + "\n# from the browser\n", "hash": body["hash"]},
        )
        assert status == 409
        assert refusal["conflict"]["hash"] == moved["hash"]
        assert (tree / TARGET).read_text(encoding="utf-8") == elsewhere


def test_an_undo_in_one_tab_lands_in_the_other(served: str) -> None:
    """The stack is the server's, so the other tab has to hear about the step.

    Two streams stand in for two tabs. One applies a change and undoes it; the
    other is told which file moved, that the file is back to what it was, and
    that its own Undo button now has nothing behind it.
    """
    with Stream(served) as one, Stream(served) as two:
        one.wait_for("hello")
        two.wait_for("hello")
        _, original = call(served, f"/api/file/{TARGET}")

        _, applied = call(
            served,
            "/api/ops",
            "POST",
            {
                "revision": original["revision"],
                "ops": [
                    {
                        "op": "set",
                        "address": "hosts/srv-nas",
                        "path": "spec.model",
                        "value": "DS1522+",
                    }
                ],
            },
        )
        assert applied["undo"] == 1
        assert two.wait_for("history-changed")["undo"] == 1
        seen = len(two.events)

        status, undone = call(served, "/api/undo", "POST")
        assert status == 200 and undone["undo"] == 0 and undone["redo"] == 1

        # The other tab hears both halves: the file is different again, and the
        # history it was showing has moved under it.
        moved = two.wait_for("file-changed", after=seen)
        assert moved["path"] == TARGET
        assert moved["hash"] == original["hash"], "an undo restores the bytes"
        history = two.wait_for("history-changed", after=seen)
        assert (history["undo"], history["redo"]) == (0, 1)

        _, now = call(served, f"/api/file/{TARGET}")
        assert now["text"] == original["text"]


def test_an_undo_from_a_second_client_moves_the_first_clients_state(served: str) -> None:
    """The same property on the polling path: no stream anywhere in this test."""
    _, original = call(served, f"/api/file/{TARGET}")
    call(
        served,
        "/api/ops",
        "POST",
        {
            "revision": original["revision"],
            "ops": [
                {"op": "set", "address": "hosts/srv-nas", "path": "spec.model", "value": "DS1522+"}
            ],
        },
    )
    _, mid = call(served, "/api/state?since=0")
    assert mid["undo"] == 1

    call(served, "/api/undo", "POST")

    _, after = call(served, f"/api/state?since={mid['events']['lastEventId']}")
    assert after["undo"] == 0 and after["redo"] == 1
    replayed = [event["event"] for event in after["events"]["replay"]]
    assert "file-changed" in replayed and "history-changed" in replayed
    assert call(served, f"/api/file/{TARGET}")[1]["text"] == original["text"]
