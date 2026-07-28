"""``netgraph watch``: the cycle, the loop, and the preview server.

The properties asserted here are the ones the command promises and the ones a
user would only discover the hard way:

* **A failed cycle never destroys the good one.** The file on disk keeps its
  last valid contents and the preview keeps serving the last valid payload.
  This is the whole reason the command exists rather than a shell loop around
  ``netgraph render``.
* **The loop survives anything the inventory can do to it.** A syntax error, a
  vanished root, a name that no longer resolves: each is a status, never an
  exception that ends the watch.
* **The preview stays on this machine.** The default bind is loopback, the
  ``Host`` header is checked, and no request path is ever turned into a file
  name.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from netgraph.cli import cli
from netgraph.errors import RenderError
from netgraph.render import FORMATS, FilterSpec, Layer, RenderOptions, media_type_for, suffix_for
from netgraph.watch import (
    DEFAULT_HOST,
    CycleResult,
    InventoryFilter,
    LiveRender,
    PreviewServer,
    Problem,
    RenderRequest,
    ServeError,
    Status,
    describe_exposure,
    file_changes,
    is_loopback,
    run_cycle,
    run_watch,
    status_document,
    write_atomically,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_LAB = REPO_ROOT / "examples" / "home-lab"
INVALID = REPO_ROOT / "tests" / "fixtures" / "invalid"
BROKEN = INVALID / "e001-unknown-endpoint.yaml"
WARNING_ONLY = INVALID / "w103-orphan-device.yaml"


@pytest.fixture
def inventory(tmp_path: Path) -> Path:
    """A writable copy of the home-lab example, safe to break."""
    import shutil

    root = tmp_path / "inventory"
    shutil.copytree(HOME_LAB, root)
    return root


#: The wording PyYAML gives :data:`SYNTAX_BREAKAGE`. Deliberately one of the few
#: messages that is byte-identical on the libyaml and the pure-Python parser, so
#: the assertions below hold whichever base ``StrictSafeLoader`` was built over.
SYNTAX_PROBLEM = "found unexpected end of stream"

#: An unterminated quoted scalar: what a half-finished edit actually looks like.
SYNTAX_BREAKAGE = "\nnot: 'valid\n"


def break_inventory(root: Path) -> None:
    """Make one document unparseable, the way a half-finished edit does."""
    victim = next(root.rglob("*.yaml"))
    victim.write_text(victim.read_text() + SYNTAX_BREAKAGE, encoding="utf-8")


# --------------------------------------------------------------------------- #
# The cycle
# --------------------------------------------------------------------------- #


def test_a_clean_inventory_renders() -> None:
    result = run_cycle(RenderRequest(inventory=HOME_LAB, output_format="dot"))
    assert result.status is Status.OK
    assert result.payload is not None
    assert b"graph netgraph" in result.payload
    assert result.nodes == 6
    assert result.edges == 5
    assert result.problems == ()


def test_a_rejected_inventory_produces_no_payload() -> None:
    result = run_cycle(RenderRequest(inventory=BROKEN, output_format="dot"))
    assert result.status is Status.INVALID
    assert result.payload is None
    assert result.error_count == 1
    assert "E001" in str(result.problems[0])
    assert result.message == "1 error"


def test_force_renders_a_rejected_inventory_anyway() -> None:
    result = run_cycle(RenderRequest(inventory=BROKEN, output_format="dot", force=True))
    assert result.status is Status.OK
    assert result.payload is not None
    # The findings survive the override; --force silences the refusal, not the
    # reason for it.
    assert result.error_count == 1


def test_a_warning_alone_still_renders() -> None:
    result = run_cycle(RenderRequest(inventory=WARNING_ONLY, output_format="dot"))
    assert result.status is Status.OK
    assert result.warning_count == 1


def test_strict_turns_a_warning_into_a_rejection() -> None:
    result = run_cycle(RenderRequest(inventory=WARNING_ONLY, output_format="dot", strict=True))
    assert result.status is Status.INVALID
    assert result.payload is None


def test_a_syntax_error_is_reported_not_raised(inventory: Path) -> None:
    break_inventory(inventory)
    result = run_cycle(RenderRequest(inventory=inventory, output_format="dot"))
    assert result.status is Status.INVALID
    assert any(SYNTAX_PROBLEM in str(problem) for problem in result.problems)


def test_a_vanished_inventory_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    result = run_cycle(RenderRequest(inventory=tmp_path / "gone", output_format="dot"))
    assert result.status is Status.FAILED
    assert result.payload is None
    assert "gone" in result.message


def test_an_unresolvable_filter_is_a_failure(tmp_path: Path) -> None:
    # A --neighbors-of target can disappear mid-edit; that must not end the run.
    request = RenderRequest(
        inventory=HOME_LAB,
        output_format="dot",
        spec=FilterSpec(neighbors_of="no-such-device"),
    )
    result = run_cycle(request)
    assert result.status is Status.FAILED
    assert "no-such-device" in result.message


def test_the_configuration_file_is_re_read_every_cycle(inventory: Path) -> None:
    """Editing ``netgraph.toml`` must take effect like editing any document."""
    request = RenderRequest(inventory=inventory, output_format="dot", strict=True)
    assert run_cycle(request).status is Status.OK

    (inventory / "netgraph.toml").write_text(
        '[validate]\nseverity = { W103 = "error" }\n', encoding="utf-8"
    )
    (inventory / "orphan.yaml").write_text(
        WARNING_ONLY.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert run_cycle(request).status is Status.INVALID


def test_render_options_reach_the_renderer() -> None:
    plain = run_cycle(RenderRequest(inventory=HOME_LAB, output_format="dot"))
    titled = run_cycle(
        RenderRequest(
            inventory=HOME_LAB,
            output_format="dot",
            layer=Layer.L2,
            options=RenderOptions(title="a caption", show_ips=False),
        )
    )
    assert plain.payload is not None and titled.payload is not None
    assert b"a caption" in titled.payload
    assert titled.payload != plain.payload


# --------------------------------------------------------------------------- #
# The published state
# --------------------------------------------------------------------------- #


def _ok(payload: bytes = b"<svg/>") -> CycleResult:
    return CycleResult(status=Status.OK, payload=payload, message="1 node, 0 edges")


def _broken() -> CycleResult:
    return CycleResult(status=Status.INVALID, message="1 error")


def test_a_successful_cycle_advances_both_revisions() -> None:
    live = LiveRender("svg")
    snapshot = live.publish(_ok())
    assert (snapshot.revision, snapshot.payload_revision) == (1, 1)
    assert snapshot.payload == b"<svg/>"
    assert snapshot.stale is False


def test_a_failed_cycle_keeps_the_previous_render() -> None:
    live = LiveRender("svg")
    live.publish(_ok(b"first"))
    snapshot = live.publish(_broken())

    assert snapshot.status is Status.INVALID
    # The status moved on; the picture did not.
    assert snapshot.revision == 2
    assert snapshot.payload_revision == 1
    assert snapshot.payload == b"first"
    assert snapshot.stale is True


def test_a_failure_before_any_render_is_not_stale() -> None:
    live = LiveRender("svg")
    snapshot = live.publish(_broken())
    assert snapshot.payload is None
    assert snapshot.stale is False


def test_a_recovery_advances_the_payload_again() -> None:
    live = LiveRender("svg")
    live.publish(_ok(b"first"))
    live.publish(_broken())
    snapshot = live.publish(_ok(b"second"))

    assert (snapshot.revision, snapshot.payload_revision) == (3, 2)
    assert snapshot.payload == b"second"
    assert snapshot.stale is False


def test_the_status_document_is_json_serialisable() -> None:
    live = LiveRender("svg")
    live.publish(
        CycleResult(status=Status.INVALID, message="1 error", problems=_broken_problems()),
        stamp="12:00:00",
    )
    document = json.loads(json.dumps(status_document(live.snapshot())))
    assert document["status"] == "invalid"
    assert document["stamp"] == "12:00:00"
    assert document["hasPayload"] is False


def _broken_problems() -> tuple[Problem, ...]:
    return run_cycle(RenderRequest(inventory=BROKEN, output_format="dot")).problems


# --------------------------------------------------------------------------- #
# Atomic output
# --------------------------------------------------------------------------- #


def test_the_output_file_is_written_through_a_temporary(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "diagram.svg"
    write_atomically(target, b"payload")
    assert target.read_bytes() == b"payload"
    # No leftovers: a reader globbing the directory must see one file.
    assert [path.name for path in target.parent.iterdir()] == ["diagram.svg"]


def test_rewriting_leaves_no_intermediate_state(tmp_path: Path) -> None:
    target = tmp_path / "diagram.svg"
    write_atomically(target, b"first")
    write_atomically(target, b"second")
    assert target.read_bytes() == b"second"
    assert list(tmp_path.iterdir()) == [target]


def test_an_unwritable_destination_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(RenderError, match="cannot write"):
        write_atomically(blocker / "diagram.svg", b"payload")


# --------------------------------------------------------------------------- #
# What counts as a change
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relative",
    ["sw.yaml", "sites/hq/sw.yml", "sites/HQ/SW.YAML", "netgraph.toml", ".netgraphignore"],
)
def test_documents_and_configuration_trigger_a_render(tmp_path: Path, relative: str) -> None:
    assert InventoryFilter(root=tmp_path).accepts(tmp_path / relative)


@pytest.mark.parametrize(
    "relative",
    [
        "diagram.svg",
        "README.md",
        ".git/index",
        "__pycache__/x.yaml",
        "_drafts/sw.yaml",
        ".hidden/sw.yaml",
        "sw.yaml.swp",
    ],
)
def test_everything_else_is_ignored(tmp_path: Path, relative: str) -> None:
    assert not InventoryFilter(root=tmp_path).accepts(tmp_path / relative)


def test_a_directory_event_is_accepted(tmp_path: Path) -> None:
    # Renaming or deleting a directory may be the only event we get for the
    # documents inside it; a missed change is worse than a spare render.
    assert InventoryFilter(root=tmp_path).accepts(tmp_path / "sites" / "hq")


def test_the_render_target_never_triggers_itself(tmp_path: Path) -> None:
    output = tmp_path / "topology.yaml"
    watcher = InventoryFilter(root=tmp_path, ignore=[output])
    assert not watcher.accepts(output)
    assert watcher.accepts(tmp_path / "sw.yaml")


def test_a_single_file_inventory_ignores_its_neighbours(tmp_path: Path) -> None:
    only = tmp_path / "topology.yaml"
    watcher = InventoryFilter(root=tmp_path, only=[only])
    assert watcher.accepts(only)
    assert not watcher.accepts(tmp_path / "unrelated.yaml")
    # Configuration still applies to a single-file inventory.
    assert watcher.accepts(tmp_path / "netgraph.toml")


def test_the_filter_is_callable_as_watchfiles_expects(tmp_path: Path) -> None:
    from watchfiles import Change

    watcher = InventoryFilter(root=tmp_path)
    assert watcher(Change.modified, str(tmp_path / "sw.yaml"))
    assert not watcher(Change.deleted, str(tmp_path / "sw.png"))


def test_a_path_outside_the_root_is_judged_on_its_name(tmp_path: Path) -> None:
    # A symlinked subtree reports events under its real path, which need not
    # sit below the inventory root.
    watcher = InventoryFilter(root=tmp_path / "inventory")
    assert watcher.accepts(Path("/elsewhere/sw.yaml"))
    assert not watcher.accepts(Path("/elsewhere/diagram.svg"))


def test_a_real_edit_reaches_the_loop(inventory: Path) -> None:
    """The one test that goes through watchfiles rather than around it."""
    stop = threading.Event()
    changes = file_changes(
        [inventory],
        watch_filter=InventoryFilter(root=inventory),
        debounce_ms=50,
        stop=stop,
    )
    target = inventory / "added.yaml"

    def edit() -> None:
        # The watcher needs a moment to arm before an event can be seen.
        time.sleep(0.5)
        target.write_text(WARNING_ONLY.read_text(encoding="utf-8"), encoding="utf-8")

    writer = threading.Thread(target=edit)
    guard = threading.Timer(20, stop.set)  # Never hang the suite.
    writer.start()
    guard.start()
    try:
        batch = next(changes, None)
    finally:
        writer.join()
        guard.cancel()
        stop.set()
        changes.close()

    assert batch is not None, "the watcher did not report the edit"
    assert str(target) in batch


def test_the_watcher_stops_when_asked(inventory: Path) -> None:
    stop = threading.Event()
    changes = file_changes([inventory], watch_filter=InventoryFilter(root=inventory), stop=stop)
    stop.set()
    assert next(changes, None) is None


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def test_the_first_render_happens_before_any_change() -> None:
    seen: list[CycleResult] = []
    run_watch(
        RenderRequest(inventory=HOME_LAB, output_format="dot"),
        changes=[],
        on_result=seen.append,
    )
    assert [result.status for result in seen] == [Status.OK]


def test_every_batch_triggers_exactly_one_render(inventory: Path) -> None:
    seen: list[CycleResult] = []
    batches: list[list[str]] = []
    run_watch(
        RenderRequest(inventory=inventory, output_format="dot"),
        changes=[["a.yaml"], ["b.yaml", "c.yaml"]],
        on_result=seen.append,
        on_change=lambda batch: batches.append(list(batch)),
    )
    assert len(seen) == 3
    assert batches == [["a.yaml"], ["b.yaml", "c.yaml"]]


def test_a_broken_edit_leaves_the_last_good_file_in_place(inventory: Path, tmp_path: Path) -> None:
    output = tmp_path / "diagram.dot"
    statuses: list[Status] = []

    def break_it(_: object) -> None:
        break_inventory(inventory)

    live = run_watch(
        RenderRequest(inventory=inventory, output_format="dot"),
        # The first batch is emitted after the good render; breaking the tree
        # inside ``on_change`` makes the second cycle fail.
        changes=[["edit"]],
        output=output,
        on_result=lambda result: statuses.append(result.status),
        on_change=break_it,
    )

    assert statuses == [Status.OK, Status.INVALID]
    assert b"graph netgraph" in output.read_bytes()
    snapshot = live.snapshot()
    assert snapshot.stale is True
    assert snapshot.payload == output.read_bytes()


def test_a_write_failure_demotes_the_cycle(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    seen: list[CycleResult] = []

    live = run_watch(
        RenderRequest(inventory=HOME_LAB, output_format="dot"),
        changes=[],
        output=blocker / "diagram.dot",
        on_result=seen.append,
    )
    # Nothing landed anywhere, so nothing is published as visible either.
    assert seen[0].status is Status.FAILED
    assert live.snapshot().payload is None


def test_the_loop_survives_a_permanently_broken_inventory(inventory: Path) -> None:
    break_inventory(inventory)
    statuses: list[Status] = []
    run_watch(
        RenderRequest(inventory=inventory, output_format="dot"),
        changes=[["x"], ["y"]],
        on_result=lambda result: statuses.append(result.status),
    )
    assert statuses == [Status.INVALID, Status.INVALID, Status.INVALID]


# --------------------------------------------------------------------------- #
# The preview server
# --------------------------------------------------------------------------- #


@pytest.fixture
def preview() -> Iterator[tuple[PreviewServer, LiveRender]]:
    live = LiveRender("svg")
    server = PreviewServer.create(live, title="test", host="127.0.0.1", port=0)
    with server:
        yield server, live


def get(
    server: PreviewServer, path: str, *, host: str | None = None, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    """One request against the preview, returning status, headers and body."""
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        headers = {"Host": host} if host is not None else {}
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_the_page_is_served_at_the_root(
    preview: tuple[PreviewServer, LiveRender],
) -> None:
    server, _ = preview
    status, headers, body = get(server, "/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"/render.svg" in body
    assert b"/watch.js" in body


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/watch.css", "text/css; charset=utf-8"), ("/watch.js", "text/javascript; charset=utf-8")],
)
def test_the_page_assets_are_served(
    preview: tuple[PreviewServer, LiveRender], path: str, content_type: str
) -> None:
    server, _ = preview
    status, headers, body = get(server, path)
    assert status == 200
    assert headers["Content-Type"] == content_type
    assert body


def test_the_status_endpoint_tracks_the_live_state(
    preview: tuple[PreviewServer, LiveRender],
) -> None:
    server, live = preview
    _, _, body = get(server, "/status.json")
    assert json.loads(body)["status"] == "pending"

    live.publish(_ok(b"<svg/>"))
    _, headers, body = get(server, "/status.json")
    document = json.loads(body)
    assert headers["Content-Type"] == "application/json"
    assert (document["status"], document["revision"], document["hasPayload"]) == ("ok", 1, True)


def test_the_payload_is_unavailable_until_something_renders(
    preview: tuple[PreviewServer, LiveRender],
) -> None:
    server, live = preview
    status, _, _ = get(server, "/render.svg")
    assert status == 503

    live.publish(_ok(b"<svg>diagram</svg>"))
    status, headers, body = get(server, "/render.svg")
    assert status == 200
    assert headers["Content-Type"] == "image/svg+xml"
    assert body == b"<svg>diagram</svg>"


def test_a_query_string_does_not_change_the_route(
    preview: tuple[PreviewServer, LiveRender],
) -> None:
    # The page appends ?rev=N purely to defeat the browser cache.
    server, live = preview
    live.publish(_ok(b"<svg/>"))
    assert get(server, "/render.svg?rev=7")[0] == 200


def test_the_payload_route_follows_the_format() -> None:
    live = LiveRender("json")
    with PreviewServer.create(live, title="test", host="127.0.0.1", port=0) as server:
        live.publish(_ok(b"{}"))
        status, headers, _ = get(server, "/render.json")
        assert status == 200
        assert headers["Content-Type"] == "application/json"
        assert get(server, "/render.svg")[0] == 404
        assert b"<pre" in get(server, "/")[2]


@pytest.mark.parametrize("output_format", FORMATS)
def test_every_registered_format_can_be_previewed(output_format: str) -> None:
    """Route, content type and viewer all come from the renderer registry.

    A backend added later is previewable without a line changing in the server;
    the viewer is chosen from the media type, so text lands in a ``<pre>``, an
    image in an ``<img>`` and anything else in an ``<object>``.
    """
    live = LiveRender(output_format)
    with PreviewServer.create(live, title="test", host="127.0.0.1", port=0) as server:
        live.publish(_ok(b"payload"))
        status, headers, _ = get(server, f"/render{suffix_for(output_format)}")
        assert status == 200
        assert headers["Content-Type"] == media_type_for(output_format)

        page = get(server, "/")[2]
        expected = {
            "dot": b"<pre",
            "mermaid": b"<pre",
            "json": b"<pre",
            "svg": b"<img",
            "png": b"<img",
            "pdf": b"<object",
        }[output_format]
        assert expected in page


def test_no_other_path_is_served(preview: tuple[PreviewServer, LiveRender]) -> None:
    server, _ = preview
    for path in ("/etc/passwd", "/../../etc/passwd", "/index.html", "/render.svg.bak"):
        assert get(server, path)[0] == 404, path


def test_a_head_request_carries_the_headers_but_no_body(
    preview: tuple[PreviewServer, LiveRender],
) -> None:
    server, _ = preview
    status, headers, body = get(server, "/", method="HEAD")
    assert status == 200
    assert int(headers["Content-Length"]) > 0
    assert body == b""


def test_writing_is_not_offered(preview: tuple[PreviewServer, LiveRender]) -> None:
    server, _ = preview
    assert get(server, "/", method="POST")[0] == 501


def test_every_response_carries_the_hardening_headers(
    preview: tuple[PreviewServer, LiveRender],
) -> None:
    server, _ = preview
    _, headers, _ = get(server, "/")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store, max-age=0"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


@pytest.mark.parametrize("host", ["localhost:1", "127.0.0.1:1", "[::1]:1", "127.0.0.1"])
def test_a_loopback_host_header_is_accepted(
    preview: tuple[PreviewServer, LiveRender], host: str
) -> None:
    server, _ = preview
    assert get(server, "/", host=host)[0] == 200


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:8080", "10.0.0.1"])
def test_a_foreign_host_header_is_refused(
    preview: tuple[PreviewServer, LiveRender], host: str
) -> None:
    """A loopback preview must not be reachable through a rebound DNS name."""
    server, _ = preview
    assert get(server, "/", host=host)[0] == 421


def test_a_port_already_in_use_is_reported_clearly() -> None:
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        with pytest.raises(ServeError, match="cannot serve the preview"):
            PreviewServer.create(
                LiveRender("svg"), title="test", host="127.0.0.1", port=holder.getsockname()[1]
            )
    finally:
        holder.close()


def test_stopping_twice_is_harmless() -> None:
    server = PreviewServer.create(LiveRender("svg"), title="test", host="127.0.0.1", port=0)
    server.start()
    server.stop()
    server.stop()


def test_the_server_answers_from_another_thread(
    preview: tuple[PreviewServer, LiveRender],
) -> None:
    """The loop publishes while the server reads; both must be safe."""
    server, live = preview
    stop = threading.Event()

    def churn() -> None:
        for index in range(200):
            live.publish(_ok(f"<svg>{index}</svg>".encode()))
        stop.set()

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        while not stop.is_set():
            assert get(server, "/status.json")[0] == 200
    finally:
        worker.join()
    assert live.snapshot().revision == 200


# --------------------------------------------------------------------------- #
# Exposure
# --------------------------------------------------------------------------- #


def test_the_default_bind_is_loopback() -> None:
    # An inventory describes internal topology. If this ever changes, it must
    # be a decision somebody made on purpose.
    assert DEFAULT_HOST == "127.0.0.1"
    assert is_loopback(DEFAULT_HOST)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "[::1]", "localhost"])
def test_loopback_addresses_need_no_warning(host: str) -> None:
    assert is_loopback(host)
    assert describe_exposure(host) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.1.2.3", "netgraph.example.com"])
def test_a_reachable_bind_is_called_out(host: str) -> None:
    assert not is_loopback(host)
    warning = describe_exposure(host)
    assert warning is not None
    assert "topology" in warning


def test_a_wildcard_bind_advertises_an_openable_url() -> None:
    server = PreviewServer.create(LiveRender("svg"), title="test", host="0.0.0.0", port=0)
    try:
        # 0.0.0.0 is where it listens, not somewhere a browser can go.
        assert server.url.startswith("http://127.0.0.1:")
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str) -> Result:
    return runner.invoke(cli, list(args), catch_exceptions=False)


@pytest.mark.parametrize("option", ["--host", "--port"])
def test_serve_options_without_serve_are_a_usage_error(runner: CliRunner, option: str) -> None:
    value = "0.0.0.0" if option == "--host" else "9000"
    result = invoke(runner, "-i", str(HOME_LAB), "watch", option, value)
    assert result.exit_code == 2
    assert "add --serve" in result.output


def test_watch_renders_once_per_change_and_reports(
    runner: CliRunner, inventory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the command with a canned change stream instead of a filesystem."""
    output = tmp_path / "diagram.dot"
    monkeypatch.setattr("netgraph.cli.file_changes", lambda *a, **k: iter([["edited.yaml"]]))

    result = invoke(
        runner, "-i", str(inventory), "watch", "-f", "dot", "-o", str(output), "--title", "live"
    )

    assert result.exit_code == 0
    assert result.output.count("ok  ") == 2
    assert str(output) in result.output
    assert "watch stopped" in result.output
    assert b"live" in output.read_bytes()


def test_watch_reports_a_broken_inventory_without_stopping(
    runner: CliRunner, inventory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "diagram.dot"

    def changes(*_: object, **__: object) -> Iterator[list[str]]:
        break_inventory(inventory)
        yield ["edited.yaml"]

    monkeypatch.setattr("netgraph.cli.file_changes", changes)
    result = invoke(runner, "-i", str(inventory), "watch", "-f", "dot", "-o", str(output))

    assert result.exit_code == 0
    assert "invalid" in result.output
    assert "keeping the render from before" in result.output
    # The findings are reported, not just counted.
    assert SYNTAX_PROBLEM in result.output
    assert b"graph netgraph" in output.read_bytes()


def test_watch_notes_that_a_render_goes_nowhere(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netgraph.cli.file_changes", lambda *a, **k: iter([]))
    result = invoke(runner, "-i", str(HOME_LAB), "watch", "-f", "dot")
    assert result.exit_code == 0
    assert "checked and discarded" in result.output


def test_watch_serves_the_render_while_it_runs(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--serve answers requests during the run and gives the port back after."""
    started: list[PreviewServer] = []
    create = PreviewServer.create

    def capture(*args: Any, **kwargs: Any) -> PreviewServer:
        server = create(*args, **kwargs)
        started.append(server)
        return server

    bodies: list[bytes] = []

    def probe_while_watching(*_: object, **__: object) -> Iterator[list[str]]:
        # A generator, so the body runs when the loop first asks for a change:
        # by then the first render is published and the server is up.
        status, _headers, body = get(started[0], "/render.dot")
        assert status == 200
        bodies.append(body)
        yield from ()

    monkeypatch.setattr("netgraph.cli.PreviewServer.create", capture)
    monkeypatch.setattr("netgraph.cli.file_changes", probe_while_watching)
    result = invoke(runner, "-i", str(HOME_LAB), "watch", "-f", "dot", "--serve", "--port", "0")

    assert result.exit_code == 0
    assert "preview at http://127.0.0.1:" in result.output
    assert b"graph netgraph" in bodies[0]
    # The port is released when the command returns.
    assert not _is_listening(started[0].port)


def _is_listening(port: int) -> bool:
    probe = socket.socket()
    probe.settimeout(1)
    try:
        probe.connect(("127.0.0.1", port))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def test_a_quiet_watch_still_reports_problems(
    runner: CliRunner, inventory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--quiet drops the routine status lines, never the reason to look."""
    output = tmp_path / "diagram.dot"
    monkeypatch.setattr("netgraph.cli.file_changes", lambda *a, **k: iter([]))
    clean = invoke(runner, "-q", "-i", str(inventory), "watch", "-f", "dot", "-o", str(output))
    assert clean.output == ""

    break_inventory(inventory)
    broken = invoke(runner, "-q", "-i", str(inventory), "watch", "-f", "dot", "-o", str(output))
    assert "invalid" in broken.output


def test_ctrl_c_ends_the_watch_cleanly(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C is how this command is meant to end, so it is not a failure."""

    def interrupt(*_: object, **__: object) -> Iterator[list[str]]:
        yield ["edited.yaml"]
        raise KeyboardInterrupt

    monkeypatch.setattr("netgraph.cli.file_changes", interrupt)
    result = invoke(runner, "-i", str(HOME_LAB), "watch", "-f", "dot")
    assert result.exit_code == 0
    assert result.output.count("ok  ") == 2
    assert "watch stopped" in result.output
