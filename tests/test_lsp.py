"""``netgraph lsp``, driven over the wire rather than through any one editor.

Every test here talks JSON-RPC to a real :class:`~netgraph.lsp.LanguageServer`
over real pipes: ``Content-Length`` frames in, framed responses and
notifications out. That is deliberate. A language server is defined by what it
puts on the socket, and a test that called the handlers directly would pass
while the framing, the initialisation gate and the notification ordering were
all wrong — which is exactly the class of bug that makes a server work in one
editor and hang in the next.

The inventory under test is a copy of ``examples/home-lab``, so the fixtures are
documents that already have to stay valid for the rest of the suite.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import pytest

from netgraph.lsp import Connection, LanguageServer
from netgraph.lsp.context import Slot, context_at, document_bounds
from netgraph.lsp.jsonrpc import (
    METHOD_NOT_FOUND,
    SERVER_NOT_INITIALIZED,
    ProtocolError,
)
from netgraph.lsp.locate import scalar_span
from netgraph.lsp.text import Encoding, Position, Range, TextDocument, full_range
from netgraph.lsp.uri import path_to_uri, uri_to_path
from netgraph.lsp.workspace import LONE_FILE_RULES

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
HOME_LAB: Final = REPO_ROOT / "examples" / "home-lab"

#: Long enough that a slow machine does not fail the suite, short enough that a
#: server that has genuinely stopped answering does not hang it.
TIMEOUT: Final = 20.0


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


class Driver:
    """A minimal LSP client: frames out, frames in, on real pipes."""

    def __init__(self, root: Path | None, *, watch: bool = False) -> None:
        to_server_read, to_server_write = os.pipe()
        to_client_read, to_client_write = os.pipe()
        self._server_connection = Connection(
            os.fdopen(to_server_read, "rb"), os.fdopen(to_client_write, "wb")
        )
        self._client_connection = Connection(
            os.fdopen(to_client_read, "rb"), os.fdopen(to_server_write, "wb")
        )
        self.server = LanguageServer(connection=self._server_connection, root=root, watch=watch)
        self.exit_code: int | None = None
        self._next_id = 0
        self._responses: dict[int, Mapping[str, Any]] = {}
        self._notifications: list[Mapping[str, Any]] = []
        self._lock = threading.Condition()
        self._arrivals: queue.Queue[None] = queue.Queue()
        self._serve = threading.Thread(target=self._run, daemon=True)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._serve.start()
        self._reader.start()

    # -- plumbing --------------------------------------------------------

    def _run(self) -> None:
        self.exit_code = self.server.serve()

    def _read(self) -> None:
        while True:
            try:
                message = self._client_connection.read()
            except (ProtocolError, OSError, ValueError):  # pragma: no cover - shutdown race
                return
            if message is None:
                return
            with self._lock:
                identifier = message.get("id")
                if identifier is not None and "method" not in message:
                    self._responses[int(identifier)] = message
                else:
                    self._notifications.append(message)
                self._lock.notify_all()

    # -- sending ---------------------------------------------------------

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._client_connection.write(
            {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
        )

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """Send a request and wait for its response message."""
        self._next_id += 1
        identifier = self._next_id
        self._client_connection.write(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": method,
                "params": dict(params or {}),
            }
        )
        with self._lock:
            if not self._lock.wait_for(lambda: identifier in self._responses, timeout=TIMEOUT):
                raise AssertionError(f"{method} was never answered")
            return self._responses.pop(identifier)

    def result(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """The ``result`` of a request, asserting that it did not error."""
        message = self.request(method, params)
        assert "error" not in message, message["error"]
        return message.get("result")

    def error(self, method: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """The ``error`` of a request, asserting that it did not succeed."""
        message = self.request(method, params)
        assert "error" in message, message
        error = message["error"]
        assert isinstance(error, Mapping)
        return error

    # -- receiving -------------------------------------------------------

    def wait_for_diagnostics(
        self,
        uri: str,
        until: Callable[[list[dict[str, Any]]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """The most recent ``publishDiagnostics`` for ``uri``, waiting for one.

        ``until`` waits for a *particular* publication rather than the next one,
        which is what a test that expects an edit to change the answer needs:
        the server may legitimately publish twice, and asserting on the first
        would be a race.
        """
        accept = until if until is not None else (lambda _: True)

        def arrived() -> bool:
            return any(
                message.get("method") == "textDocument/publishDiagnostics"
                and (message.get("params") or {}).get("uri") == uri
                and accept(list((message.get("params") or {}).get("diagnostics", [])))
                for message in self._notifications
            )

        with self._lock:
            if not self._lock.wait_for(arrived, timeout=TIMEOUT):
                published = sorted(
                    (message.get("params") or {}).get("uri", "")
                    for message in self._notifications
                    if message.get("method") == "textDocument/publishDiagnostics"
                )
                raise AssertionError(f"no diagnostics for {uri}; published: {published}")
            for message in reversed(self._notifications):
                params = message.get("params") or {}
                if (
                    message.get("method") == "textDocument/publishDiagnostics"
                    and params.get("uri") == uri
                    and accept(list(params.get("diagnostics", [])))
                ):
                    return list(params.get("diagnostics", []))
        raise AssertionError("unreachable")  # pragma: no cover

    def forget_diagnostics(self) -> None:
        """Drop what has been published so the next wait sees only new news."""
        with self._lock:
            self._notifications = [
                message
                for message in self._notifications
                if message.get("method") != "textDocument/publishDiagnostics"
            ]

    def logs(self) -> list[str]:
        with self._lock:
            return [
                str((message.get("params") or {}).get("message", ""))
                for message in self._notifications
                if message.get("method") == "window/logMessage"
            ]

    # -- lifecycle -------------------------------------------------------

    def initialize(self, root: Path | None, **capabilities: Any) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "processId": None,
            "capabilities": _CAPABILITIES | capabilities,
            "clientInfo": {"name": "tests"},
        }
        if root is not None:
            params["rootUri"] = path_to_uri(root)
            params["workspaceFolders"] = [{"uri": path_to_uri(root), "name": root.name}]
        result = self.result("initialize", params)
        assert isinstance(result, Mapping)
        self.notify("initialized", {})
        return result

    def open(self, path: Path, text: str | None = None, version: int = 1) -> str:
        uri = path_to_uri(path)
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "yaml",
                    "version": version,
                    "text": path.read_text(encoding="utf-8") if text is None else text,
                }
            },
        )
        return uri

    def change(self, uri: str, changes: Sequence[Mapping[str, Any]], version: int) -> None:
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": list(changes),
            },
        )

    def close(self) -> int:
        self.result("shutdown")
        self.notify("exit")
        self._serve.join(TIMEOUT)
        assert not self._serve.is_alive(), "the server did not exit"
        assert self.exit_code is not None
        return self.exit_code


#: What a modern client advertises, which is what the server is tuned for.
_CAPABILITIES: Final[dict[str, Any]] = {
    "general": {"positionEncodings": ["utf-16"]},
    "workspace": {"workspaceEdit": {"documentChanges": True}},
    "textDocument": {
        "codeAction": {
            "dataSupport": True,
            "resolveSupport": {"properties": ["edit"]},
        }
    },
}


@pytest.fixture
def inventory(tmp_path: Path) -> Path:
    """A writable copy of ``examples/home-lab``."""
    root = tmp_path / "home-lab"
    shutil.copytree(HOME_LAB, root)
    return root


@pytest.fixture
def driver(inventory: Path) -> Iterator[Driver]:
    """A client with the folder open and the two files under test loaded."""
    client = Driver(inventory)
    client.initialize(inventory)
    yield client
    if client.exit_code is None:
        with contextlib.suppress(AssertionError):  # the test already failed
            client.close()


def _name_of(uri: str) -> str:
    """The file name a URI points at, asserting that it points at a file."""
    path = uri_to_path(uri)
    assert path is not None, uri
    return path.name


def _line_of(path: Path, needle: str) -> int:
    """The 0-based line of the first line holding ``needle``."""
    for number, text in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if needle in text:
            return number
    raise AssertionError(f"{path.name} has no line holding {needle!r}")


def _column_of(path: Path, needle: str, inside: str) -> int:
    line = _line_of(path, needle)
    text = path.read_text(encoding="utf-8").splitlines()[line]
    return text.index(inside)


# --------------------------------------------------------------------------- #
# The wire
# --------------------------------------------------------------------------- #


class TestFraming:
    """:mod:`netgraph.lsp.jsonrpc` — the base protocol, on its own."""

    @staticmethod
    def _connection(payload: bytes) -> tuple[Connection, Any]:
        import io

        out = io.BytesIO()
        return Connection(io.BytesIO(payload), out), out

    def test_a_frame_round_trips(self) -> None:
        connection, out = self._connection(b"")
        connection.write({"jsonrpc": "2.0", "method": "ping", "params": {}})
        assert out.getvalue().startswith(b"Content-Length: ")
        header, _, body = out.getvalue().partition(b"\r\n\r\n")
        assert int(header.split(b": ")[1]) == len(body)

    def test_the_length_counts_bytes_not_characters(self) -> None:
        """One non-ASCII character and the two differ; the reader must agree."""
        connection, out = self._connection(b"")
        connection.write({"jsonrpc": "2.0", "method": "note", "params": {"text": "café"}})
        frame = out.getvalue()
        back, _ = self._connection(frame)
        message = back.read()
        assert message is not None
        assert message["params"]["text"] == "café"

    def test_extra_headers_are_tolerated(self) -> None:
        body = b'{"jsonrpc":"2.0","method":"ping"}'
        frame = (
            b"Content-Length: %d\r\n"
            b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n%s"
        ) % (len(body), body)
        connection, _ = self._connection(frame)
        message = connection.read()
        assert message is not None and message["method"] == "ping"

    def test_end_of_stream_is_not_an_error(self) -> None:
        connection, _ = self._connection(b"")
        assert connection.read() is None

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"Content-Length: nope\r\n\r\n{}", id="not-a-number"),
            pytest.param(b"Content-Type: text/plain\r\n\r\n{}", id="no-length"),
            pytest.param(b"nonsense\r\n\r\n{}", id="no-colon"),
            pytest.param(b"Content-Length: 9\r\n\r\n{", id="truncated-body"),
            pytest.param(b"Content-Length: 3\r\n\r\nnot", id="not-json"),
            pytest.param(b"Content-Length: 2\r\n\r\n[]", id="not-an-object"),
            pytest.param(b"Content-Length: 1", id="truncated-headers"),
        ],
    )
    def test_a_malformed_frame_is_refused(self, payload: bytes) -> None:
        connection, _ = self._connection(payload)
        with pytest.raises(ProtocolError):
            connection.read()


class TestUris:
    """A URI and a path have to agree, or diagnostics land on nothing."""

    def test_a_path_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "sites" / "sw 01.yaml"
        uri = path_to_uri(path)
        assert uri.startswith("file:///")
        assert "%20" in uri
        assert uri_to_path(uri) == path

    def test_a_non_file_uri_has_no_path(self) -> None:
        assert uri_to_path("untitled:Untitled-1") is None

    def test_a_windows_uri_keeps_its_drive(self) -> None:
        assert str(uri_to_path("file:///c:/inv/sw.yaml")).endswith("sw.yaml")


class TestPositions:
    """Column arithmetic, which is UTF-16 unless the client says otherwise."""

    def test_utf16_columns_count_surrogate_pairs(self) -> None:
        document = TextDocument(uri="file:///x", text="a\U0001f600b\n")
        # The emoji is two UTF-16 code units, so 'b' is at column 3.
        assert document.offset_at(Position(0, 3), Encoding.UTF16) == 2
        assert document.position_at(2, Encoding.UTF16) == Position(0, 3)

    def test_utf32_columns_are_python_indices(self) -> None:
        document = TextDocument(uri="file:///x", text="a\U0001f600b\n")
        assert document.offset_at(Position(0, 2), Encoding.UTF32) == 2

    def test_a_column_past_the_end_clamps(self) -> None:
        document = TextDocument(uri="file:///x", text="ab\n")
        assert document.offset_at(Position(0, 99)) == 2

    def test_the_negotiation_prefers_no_conversion(self) -> None:
        assert Encoding.negotiate(["utf-16", "utf-32"]) is Encoding.UTF32
        assert Encoding.negotiate(None) is Encoding.UTF16
        assert Encoding.negotiate(["utf-8"]) is Encoding.UTF8

    def test_an_incremental_change_is_applied_where_it_says(self) -> None:
        document = TextDocument(uri="file:///x", text="one\ntwo\nthree\n")
        changed = document.apply([{"range": Range.at(1, 0, 3).to_dict(), "text": "TWO"}], version=2)
        assert changed.text == "one\nTWO\nthree\n"
        assert changed.version == 2

    def test_a_change_without_a_range_replaces_everything(self) -> None:
        document = TextDocument(uri="file:///x", text="one\n")
        assert document.apply([{"text": "two\n"}], version=2).text == "two\n"

    def test_the_full_range_covers_the_buffer(self) -> None:
        assert full_range("a\nbb\n") == Range(Position(0, 0), Position(2, 0))


class TestScalarSpans:
    """Where a value ends, which is what a squiggle stops at."""

    @pytest.mark.parametrize(
        ("line", "column", "expected"),
        [
            ("  mtu: 900", 7, "900"),
            ("  mtu: 900  # too small", 7, "900"),
            ("  name: 'eth 0'", 8, "'eth 0'"),
            ('  name: "a b"', 8, '"a b"'),
            ("    - sw-home:port1", 6, "sw-home:port1"),
            ("  name: it''s", 8, "it''s"),
        ],
    )
    def test_a_token_ends_where_it_ends(self, line: str, column: int, expected: str) -> None:
        start, end = scalar_span(line, column)
        assert line[start:end] == expected

    def test_a_mark_past_the_end_is_empty(self) -> None:
        assert scalar_span("abc", 99) == (3, 3)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


class TestLifecycle:
    def test_initialize_announces_what_the_server_can_do(self, inventory: Path) -> None:
        client = Driver(inventory)
        result = client.initialize(inventory)
        capabilities = result["capabilities"]
        assert capabilities["positionEncoding"] == "utf-16"
        assert capabilities["hoverProvider"] is True
        assert capabilities["definitionProvider"] is True
        assert capabilities["referencesProvider"] is True
        assert capabilities["documentFormattingProvider"] is True
        assert capabilities["renameProvider"]["prepareProvider"] is True
        assert "quickfix" in capabilities["codeActionProvider"]["codeActionKinds"]
        assert capabilities["textDocumentSync"]["change"] == 2
        assert result["serverInfo"]["name"] == "netgraph"
        assert client.close() == 0

    def test_utf32_is_used_when_the_client_offers_it(self, inventory: Path) -> None:
        client = Driver(inventory)
        result = client.initialize(inventory, general={"positionEncodings": ["utf-32", "utf-16"]})
        assert result["capabilities"]["positionEncoding"] == "utf-32"
        client.close()

    def test_nothing_is_answered_before_initialize(self, inventory: Path) -> None:
        client = Driver(inventory)
        error = client.error("textDocument/hover", {})
        assert error["code"] == SERVER_NOT_INITIALIZED
        client.notify("exit")

    def test_an_unknown_method_is_refused_rather_than_ignored(self, driver: Driver) -> None:
        error = driver.error("textDocument/inlayHint", {})
        assert error["code"] == METHOD_NOT_FOUND

    def test_exit_without_shutdown_is_a_failure(self, inventory: Path) -> None:
        client = Driver(inventory)
        client.initialize(inventory)
        client.notify("exit")
        client._serve.join(TIMEOUT)
        assert client.exit_code == 1


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


class TestDiagnostics:
    def test_a_clean_file_publishes_nothing(self, driver: Driver, inventory: Path) -> None:
        uri = driver.open(inventory / "switches" / "sw-home.yaml")
        assert driver.wait_for_diagnostics(uri) == []

    def test_an_unknown_endpoint_is_reported_where_it_is_written(
        self, driver: Driver, inventory: Path
    ) -> None:
        links = inventory / "cables" / "links.yaml"
        text = links.read_text(encoding="utf-8").replace("sw-home:port1", "sw-home:port9", 1)
        uri = driver.open(links, text)
        diagnostics = driver.wait_for_diagnostics(uri)
        [problem] = [entry for entry in diagnostics if entry["severity"] == 1]
        assert problem["code"] == "NG-C002"
        assert problem["source"] == "netgraph"
        assert "port9" in problem["message"]
        assert problem["codeDescription"]["href"].endswith(
            "docs/validation-rules.md#e001--unknown-cable-endpoint"
        )
        assert problem["data"]["rule"] == "E001"
        line = problem["range"]["start"]["line"]
        assert text.splitlines()[line].strip() == "- sw-home:port9"
        start = problem["range"]["start"]["character"]
        end = problem["range"]["end"]["character"]
        assert text.splitlines()[line][start:end] == "sw-home:port9"

    def test_a_syntax_error_is_reported_with_a_range(self, driver: Driver, inventory: Path) -> None:
        path = inventory / "switches" / "sw-home.yaml"
        uri = driver.open(path, "kind: switch\n  bad: indent\n")
        diagnostics = driver.wait_for_diagnostics(uri)
        assert diagnostics, "a file that does not parse must still be reported"
        assert all(entry["severity"] == 1 for entry in diagnostics)

    def test_an_edit_updates_and_then_clears_the_diagnostics(
        self, driver: Driver, inventory: Path
    ) -> None:
        links = inventory / "cables" / "links.yaml"
        original = links.read_text(encoding="utf-8")
        uri = driver.open(links, original)
        assert driver.wait_for_diagnostics(uri) == []

        line = _line_of(links, "- sw-home:port1")
        column = _column_of(links, "- sw-home:port1", "port1")
        driver.forget_diagnostics()
        driver.change(
            uri,
            [{"range": Range.at(line, column, len("port1")).to_dict(), "text": "port9"}],
            version=2,
        )
        broken = driver.wait_for_diagnostics(uri)
        assert [entry["code"] for entry in broken if entry["severity"] == 1] == ["NG-C002"]

        driver.forget_diagnostics()
        driver.change(
            uri,
            [{"range": Range.at(line, column, len("port9")).to_dict(), "text": "port1"}],
            version=3,
        )
        assert driver.wait_for_diagnostics(uri) == []

    def test_a_diagnostic_in_another_file_reaches_that_file(
        self, driver: Driver, inventory: Path
    ) -> None:
        """The tree is one unit: editing a switch breaks the cable that lands on it."""
        switch = inventory / "switches" / "sw-home.yaml"
        links = inventory / "cables" / "links.yaml"
        driver.open(links)
        uri = driver.open(switch, switch.read_text(encoding="utf-8").replace("port1", "portX", 1))
        assert driver.wait_for_diagnostics(uri) is not None
        cable_diagnostics = driver.wait_for_diagnostics(path_to_uri(links))
        assert any(entry["code"] == "NG-C002" for entry in cable_diagnostics)

    def test_a_change_on_disk_refreshes_without_the_client(self, inventory: Path) -> None:
        """The whole point of watching: an edit made by another tool still lands."""
        client = Driver(inventory, watch=True)
        client.initialize(inventory)
        links = inventory / "cables" / "links.yaml"
        uri = client.open(links)
        assert client.wait_for_diagnostics(uri) == []
        client.forget_diagnostics()
        # Close the buffer so the server reads the file rather than the buffer.
        client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        links.write_text(
            links.read_text(encoding="utf-8").replace("sw-home:port1", "sw-home:port9", 1),
            encoding="utf-8",
        )
        client.server._queue.put_nowait(_changed_event(links))
        diagnostics = client.wait_for_diagnostics(uri, _has_code("NG-C002"))
        assert any(entry["code"] == "NG-C002" for entry in diagnostics)
        client.close()

    def test_watched_file_notifications_from_the_client_also_refresh(
        self, driver: Driver, inventory: Path
    ) -> None:
        links = inventory / "cables" / "links.yaml"
        uri = driver.open(links)
        assert driver.wait_for_diagnostics(uri) == []
        driver.forget_diagnostics()
        driver.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        links.write_text(
            links.read_text(encoding="utf-8").replace("sw-home:port1", "sw-home:port9", 1),
            encoding="utf-8",
        )
        driver.notify(
            "workspace/didChangeWatchedFiles",
            {"changes": [{"uri": uri, "type": 2}]},
        )
        assert driver.wait_for_diagnostics(uri, _has_code("NG-C002"))


def _has_code(code: str) -> Callable[[list[dict[str, Any]]], bool]:
    """Waits for a publication that reports ``code``."""
    return lambda diagnostics: any(entry.get("code") == code for entry in diagnostics)


def _changed_event(path: Path) -> Any:
    from netgraph.lsp.server import _Event

    return _Event("changed", (str(path),))


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #


class TestCompletion:
    @staticmethod
    def _complete(driver: Driver, uri: str, line: int, character: int) -> list[dict[str, Any]]:
        result = driver.result(
            "textDocument/completion",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
        )
        assert isinstance(result, Mapping)
        return list(result["items"])

    def test_the_keys_of_an_interface_come_from_the_schema(
        self, driver: Driver, inventory: Path
    ) -> None:
        path = inventory / "switches" / "new.yaml"
        text = (
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: switch\n"
            "metadata:\n"
            "  name: sw-new\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: eth0\n"
            "      \n"
        )
        uri = driver.open(path, text)
        items = self._complete(driver, uri, 7, 6)
        labels = {item["label"] for item in items}
        assert {"type", "mtu", "ipv4", "vlan"} <= labels
        assert "name" not in labels, "a key already written is not offered again"
        mtu = next(item for item in items if item["label"] == "mtu")
        assert mtu["detail"].startswith("integer")
        assert "MTU" in mtu["documentation"]["value"]
        assert mtu["textEdit"]["newText"] == "mtu: "

    def test_an_enum_value_is_offered_with_its_prose(self, driver: Driver, inventory: Path) -> None:
        path = inventory / "switches" / "new.yaml"
        text = (
            "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nspec:\n  interfaces:\n    - type: \n"
        )
        uri = driver.open(path, text)
        items = self._complete(driver, uri, 4, 12)
        labels = [item["label"] for item in items]
        assert labels == ["ethernet", "wifi", "loopback", "bridge", "vlan", "lag", "tunnel"]

    def test_the_kinds_are_offered_before_the_document_has_one(
        self, driver: Driver, inventory: Path
    ) -> None:
        uri = driver.open(inventory / "switches" / "sw-home.yaml", "kind: ")
        labels = {item["label"] for item in self._complete(driver, uri, 0, 6)}
        assert {"switch", "router", "cable", "tunnel", "pdu"} <= labels

    def test_a_cable_endpoint_offers_the_elements_that_exist(
        self, driver: Driver, inventory: Path
    ) -> None:
        # A new document, not a replacement for ``links.yaml``: opening a
        # buffer over an existing file takes that file's elements out of the
        # tree, which is right and would make this test about nothing.
        path = inventory / "cables" / "new.yaml"
        text = (
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: cable\n"
            "metadata:\n"
            "  name: cbl-new\n"
            "spec:\n"
            "  endpoints:\n"
            "    - \n"
        )
        uri = driver.open(path, text)
        items = self._complete(driver, uri, 6, 6)
        labels = {item["label"] for item in items}
        assert {"sw-home", "rtr-home", "pc-desk"} <= labels
        assert "cbl-rtr-sw" not in labels, "a cable cannot terminate on a cable"
        switch = next(item for item in items if item["label"] == "sw-home")
        assert switch["detail"] == "switch"
        assert switch["textEdit"]["newText"] == "sw-home:"
        assert "port1" in switch["documentation"]["value"]

    def test_the_interface_half_offers_only_the_ports_that_exist(
        self, driver: Driver, inventory: Path
    ) -> None:
        path = inventory / "cables" / "new.yaml"
        text = (
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: cable\n"
            "metadata:\n"
            "  name: cbl-new\n"
            "spec:\n"
            "  endpoints:\n"
            "    - sw-home:\n"
        )
        uri = driver.open(path, text)
        labels = {item["label"] for item in self._complete(driver, uri, 6, 14)}
        assert labels == {
            "sw-home:br0",
            "sw-home:Vlan10",
            "sw-home:port1",
            "sw-home:port2",
            "sw-home:port3",
            "sw-home:port4",
            "sw-home:port5",
        }

    def test_a_group_member_offers_people_and_groups_only(
        self, driver: Driver, inventory: Path
    ) -> None:
        path = inventory / "people" / "new.yaml"
        text = (
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: group\n"
            "metadata:\n"
            "  name: g\n"
            "spec:\n"
            "  members:\n"
            "    - \n"
        )
        uri = driver.open(path, text)
        labels = {item["label"] for item in self._complete(driver, uri, 6, 6)}
        assert {"ana", "kit", "admins"} <= labels
        assert "sw-home" not in labels

    def test_nothing_is_offered_inside_a_comment(self, driver: Driver, inventory: Path) -> None:
        uri = driver.open(inventory / "switches" / "sw-home.yaml", "kind: switch  # a s\n")
        assert self._complete(driver, uri, 0, 19) == []


# --------------------------------------------------------------------------- #
# Hover, definition, references
# --------------------------------------------------------------------------- #


class TestHover:
    def test_a_cable_endpoint_hovers_as_the_port_it_lands_on(
        self, driver: Driver, inventory: Path
    ) -> None:
        links = inventory / "cables" / "links.yaml"
        uri = driver.open(links)
        line = _line_of(links, "- rtr-home:lan0")
        column = _column_of(links, "- rtr-home:lan0", "lan0")
        result = driver.result(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        assert isinstance(result, Mapping)
        value = result["contents"]["value"]
        assert "routers/rtr-home:lan0" in value
        assert "**ipv4**" in value, "the addresses on the port are the point"
        assert "cabled to" in value, "so is what is already plugged into it"

    def test_the_device_half_hovers_as_the_device(self, driver: Driver, inventory: Path) -> None:
        links = inventory / "cables" / "links.yaml"
        uri = driver.open(links)
        line = _line_of(links, "- rtr-home:lan0")
        column = _column_of(links, "- rtr-home:lan0", "rtr-home")
        result = driver.result(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        assert isinstance(result, Mapping)
        assert "lan0" in result["contents"]["value"]
        assert "routers/rtr-home.yaml" in result["contents"]["value"]

    def test_an_unresolved_reference_says_so(self, driver: Driver, inventory: Path) -> None:
        links = inventory / "cables" / "links.yaml"
        text = links.read_text(encoding="utf-8").replace("rtr-home:lan0", "rtr-gone:lan0", 1)
        uri = driver.open(links, text)
        line = _line_of(links, "- rtr-home:lan0")
        result = driver.result(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": 8}},
        )
        assert isinstance(result, Mapping)
        assert "unresolved" in result["contents"]["value"]

    def test_a_key_hovers_as_the_specification_describes_it(
        self, driver: Driver, inventory: Path
    ) -> None:
        switch = inventory / "switches" / "sw-home.yaml"
        uri = driver.open(switch)
        line = _line_of(switch, "  interfaces:")
        result = driver.result(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": 4}},
        )
        assert isinstance(result, Mapping)
        assert "interface" in result["contents"]["value"].lower()

    def test_hovering_nothing_answers_nothing(self, driver: Driver, inventory: Path) -> None:
        uri = driver.open(inventory / "switches" / "sw-home.yaml")
        result = driver.result(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 0}},
        )
        assert result in (None, {}) or "contents" in result


class TestNavigation:
    def test_definition_jumps_from_a_cable_to_the_switch_it_lands_on(
        self, driver: Driver, inventory: Path
    ) -> None:
        links = inventory / "cables" / "links.yaml"
        switch = inventory / "switches" / "sw-home.yaml"
        uri = driver.open(links)
        line = _line_of(links, "- sw-home:port1")
        column = _column_of(links, "- sw-home:port1", "sw-home")
        result = driver.result(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        assert isinstance(result, list) and len(result) == 1
        assert uri_to_path(result[0]["uri"]) == switch
        target = switch.read_text(encoding="utf-8").splitlines()[
            result[0]["range"]["start"]["line"]
        ]
        assert "sw-home" in target

    def test_definition_from_the_interface_half_lands_on_the_interface(
        self, driver: Driver, inventory: Path
    ) -> None:
        links = inventory / "cables" / "links.yaml"
        switch = inventory / "switches" / "sw-home.yaml"
        uri = driver.open(links)
        line = _line_of(links, "- sw-home:port1")
        column = _column_of(links, "- sw-home:port1", "port1")
        result = driver.result(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        assert isinstance(result, list) and result
        target = switch.read_text(encoding="utf-8").splitlines()[
            result[0]["range"]["start"]["line"]
        ]
        assert "port1" in target

    def test_references_finds_every_cable_that_lands_on_a_switch(
        self, driver: Driver, inventory: Path
    ) -> None:
        switch = inventory / "switches" / "sw-home.yaml"
        links = inventory / "cables" / "links.yaml"
        uri = driver.open(switch)
        line = _line_of(switch, "  name: sw-home")
        column = _column_of(switch, "  name: sw-home", "sw-home")
        result = driver.result(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": column},
                "context": {"includeDeclaration": True},
            },
        )
        assert isinstance(result, list)
        assert uri_to_path(result[0]["uri"]) == switch, "the declaration comes first"
        in_links = [entry for entry in result if uri_to_path(entry["uri"]) == links]
        assert len(in_links) == 5, "five cables terminate on sw-home"

    def test_document_symbols_name_the_elements_a_file_declares(
        self, driver: Driver, inventory: Path
    ) -> None:
        uri = driver.open(inventory / "cables" / "links.yaml")
        result = driver.result("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        assert isinstance(result, list)
        assert {entry["name"] for entry in result} == {
            "cbl-rtr-sw",
            "cbl-sw-desk",
            "cbl-sw-nas",
            "cbl-sw-dongle",
            "cbl-sw-ap",
            "wl-ap-phone",
        }
        assert {entry["detail"] for entry in result} == {"cable"}


# --------------------------------------------------------------------------- #
# Rename
# --------------------------------------------------------------------------- #


class TestRename:
    def test_prepare_offers_the_name_under_the_caret(self, driver: Driver, inventory: Path) -> None:
        links = inventory / "cables" / "links.yaml"
        uri = driver.open(links)
        line = _line_of(links, "- sw-home:port1")
        column = _column_of(links, "- sw-home:port1", "sw-home")
        result = driver.result(
            "textDocument/prepareRename",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        assert isinstance(result, Mapping)
        assert result["placeholder"] == "sw-home"
        assert result["range"]["start"]["character"] == column

    def test_an_interface_name_is_not_offered_for_rename(
        self, driver: Driver, inventory: Path
    ) -> None:
        links = inventory / "cables" / "links.yaml"
        uri = driver.open(links)
        line = _line_of(links, "- sw-home:port1")
        column = _column_of(links, "- sw-home:port1", "port1")
        result = driver.result(
            "textDocument/prepareRename",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        assert result is None

    def test_rename_rewrites_every_reference_in_every_file(
        self, driver: Driver, inventory: Path
    ) -> None:
        links = inventory / "cables" / "links.yaml"
        switch = inventory / "switches" / "sw-home.yaml"
        uri = driver.open(links)
        line = _line_of(links, "- sw-home:port1")
        column = _column_of(links, "- sw-home:port1", "sw-home")
        result = driver.result(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": column},
                "newName": "sw-core",
            },
        )
        assert isinstance(result, Mapping)
        changes = {
            uri_to_path(entry["textDocument"]["uri"]): entry["edits"][0]["newText"]
            for entry in result["documentChanges"]
            if "textDocument" in entry
        }
        assert set(changes) == {links, switch}
        assert "sw-core:port1" in changes[links]
        assert "sw-home" not in changes[links]
        assert "name: sw-core" in changes[switch]
        # The whole reason the write path is used rather than a search: comments
        # and everything around the changed token survive.
        before = switch.read_text(encoding="utf-8")
        assert changes[switch].count("#") == before.count("#")

    def test_renaming_from_the_declaration_works_too(self, driver: Driver, inventory: Path) -> None:
        switch = inventory / "switches" / "sw-home.yaml"
        uri = driver.open(switch)
        line = _line_of(switch, "  name: sw-home")
        column = _column_of(switch, "  name: sw-home", "sw-home")
        result = driver.result(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": column},
                "newName": "sw-core",
            },
        )
        assert isinstance(result, Mapping)
        assert len(result["documentChanges"]) == 2

    def test_a_rename_sees_the_buffer_rather_than_the_disk(
        self, driver: Driver, inventory: Path
    ) -> None:
        """An unsaved edit is what the user is looking at, so it is what is renamed."""
        links = inventory / "cables" / "links.yaml"
        switch = inventory / "switches" / "sw-home.yaml"
        text = links.read_text(encoding="utf-8").replace("- sw-home:port5", "- sw-home:port4", 1)
        uri = driver.open(links, text)
        line = _line_of(switch, "  name: sw-home")
        switch_uri = driver.open(switch)
        result = driver.result(
            "textDocument/rename",
            {
                "textDocument": {"uri": switch_uri},
                "position": {
                    "line": line,
                    "character": _column_of(switch, "  name: sw-home", "sw-home"),
                },
                "newName": "sw-core",
            },
        )
        assert isinstance(result, Mapping)
        rewritten = next(
            entry["edits"][0]["newText"]
            for entry in result["documentChanges"]
            if "textDocument" in entry and uri_to_path(entry["textDocument"]["uri"]) == links
        )
        assert rewritten.count("- sw-core:port4") == 2, "the unsaved edit is in the result"
        assert uri  # the buffer was the one that was rewritten

    def test_an_empty_new_name_is_refused(self, driver: Driver, inventory: Path) -> None:
        switch = inventory / "switches" / "sw-home.yaml"
        uri = driver.open(switch)
        line = _line_of(switch, "  name: sw-home")
        error = driver.error(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": 8},
                "newName": "  ",
            },
        )
        assert "empty" in error["message"]

    def test_renaming_something_that_is_not_a_name_is_refused(
        self, driver: Driver, inventory: Path
    ) -> None:
        switch = inventory / "switches" / "sw-home.yaml"
        uri = driver.open(switch)
        error = driver.error(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 0, "character": 0},
                "newName": "x",
            },
        )
        assert "only element names" in error["message"]


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


class TestFormatting:
    def test_formatting_returns_what_fmt_would_write(self, driver: Driver, inventory: Path) -> None:
        from netgraph.fmt import format_source

        path = inventory / "switches" / "sw-home.yaml"
        text = "kind:    switch\nmetadata:\n    name:   sw-home\n"
        uri = driver.open(path, text)
        result = driver.result("textDocument/formatting", {"textDocument": {"uri": uri}})
        assert isinstance(result, list) and len(result) == 1
        assert result[0]["newText"] == format_source(text, name="sw-home.yaml")
        assert result[0]["range"] == full_range(text).to_dict()

    def test_a_canonical_file_needs_no_edit(self, driver: Driver, inventory: Path) -> None:
        uri = driver.open(inventory / "switches" / "sw-home.yaml")
        assert driver.result("textDocument/formatting", {"textDocument": {"uri": uri}}) == []

    def test_a_file_that_does_not_parse_is_refused_rather_than_mangled(
        self, driver: Driver, inventory: Path
    ) -> None:
        uri = driver.open(inventory / "switches" / "sw-home.yaml", "kind: switch\n  bad:\n")
        error = driver.error("textDocument/formatting", {"textDocument": {"uri": uri}})
        assert "cannot be formatted" in error["message"]


# --------------------------------------------------------------------------- #
# Code actions
# --------------------------------------------------------------------------- #


class TestCodeActions:
    @staticmethod
    def _fixable(inventory: Path) -> tuple[Path, str, str]:
        """A document with a finding :mod:`netgraph.fixes` can repair, and how.

        ``W108`` — a loopback declaring a MAC address — because it has exactly
        one repair, so the action is unambiguous and the assertion is about the
        plumbing rather than about which of three fixes was chosen. The
        modification is derived from the example, so a change to the example
        fails this test rather than silently skipping it.
        """
        path = inventory / "hosts" / "pc-desk.yaml"
        text = path.read_text(encoding="utf-8")
        marker = "    - name: lo\n      type: loopback\n"
        assert marker in text, "the fixture no longer declares a loopback"
        broken = text.replace(marker, f"{marker}      mac: '02:00:00:00:00:01'\n", 1)
        return path, text, broken

    def test_a_quick_fix_is_offered_and_resolves_to_an_edit(
        self, driver: Driver, inventory: Path
    ) -> None:
        path, _, broken = self._fixable(inventory)
        uri = driver.open(path, broken)
        diagnostics = driver.wait_for_diagnostics(uri)
        assert diagnostics, "the fixture no longer produces a finding"
        target = diagnostics[0]
        actions = driver.result(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": uri},
                "range": target["range"],
                "context": {"diagnostics": [target]},
            },
        )
        assert isinstance(actions, list) and actions
        quick = [entry for entry in actions if entry["kind"] == "quickfix"]
        assert quick, [entry["title"] for entry in actions]
        assert quick[0]["title"].startswith("netgraph: ")
        assert "edit" not in quick[0], "the edit is computed on resolve"
        resolved = driver.result("codeAction/resolve", quick[0])
        assert isinstance(resolved, Mapping)
        assert "edit" in resolved, "the resolve must attach what it would change"
        [change] = [
            entry for entry in resolved["edit"]["documentChanges"] if "textDocument" in entry
        ]
        assert uri_to_path(change["textDocument"]["uri"]) == path

    def test_the_fix_everything_action_is_offered(self, driver: Driver, inventory: Path) -> None:
        path, _, broken = self._fixable(inventory)
        uri = driver.open(path, broken)
        driver.wait_for_diagnostics(uri)
        actions = driver.result(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": uri},
                "range": Range.at(0, 0).to_dict(),
                "context": {"diagnostics": [], "only": ["source.fixAll"]},
            },
        )
        assert isinstance(actions, list)
        assert [entry["kind"] for entry in actions] == ["source.fixAll.netgraph"]
        resolved = driver.result("codeAction/resolve", actions[0])
        assert isinstance(resolved, Mapping)
        assert "edit" in resolved

    def test_a_clean_file_offers_no_quick_fix(self, driver: Driver, inventory: Path) -> None:
        uri = driver.open(inventory / "switches" / "sw-home.yaml")
        driver.wait_for_diagnostics(uri)
        actions = driver.result(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": uri},
                "range": Range.at(0, 0).to_dict(),
                "context": {"diagnostics": []},
            },
        )
        assert actions == []

    def test_a_client_without_resolve_gets_the_edit_up_front(self, inventory: Path) -> None:
        client = Driver(inventory)
        client.initialize(inventory, textDocument={})
        path, _, broken = TestCodeActions._fixable(inventory)
        uri = client.open(path, broken)
        diagnostics = client.wait_for_diagnostics(uri)
        actions = client.result(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": uri},
                "range": diagnostics[0]["range"],
                "context": {"diagnostics": diagnostics[:1]},
            },
        )
        assert isinstance(actions, list) and actions
        assert any("edit" in entry for entry in actions)
        client.close()


# --------------------------------------------------------------------------- #
# The lone file
# --------------------------------------------------------------------------- #


class TestLoneFile:
    def test_a_file_opened_without_a_folder_is_still_checked(self, tmp_path: Path) -> None:
        path = tmp_path / "switch.yaml"
        path.write_text(
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: switch\n"
            "metadata:\n"
            "  name: sw\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: eth0\n"
            "      type: ethernet\n"
            "      mtu: 12\n",
            encoding="utf-8",
        )
        client = Driver(None)
        client.initialize(None)
        uri = client.open(path)
        diagnostics = client.wait_for_diagnostics(uri)
        assert any(
            entry["data"]["rule"] == "E101" or "mtu" in entry["message"].lower()
            for entry in diagnostics
        ), diagnostics
        client.close()

    def test_the_rules_that_need_a_tree_are_held_back(self, tmp_path: Path) -> None:
        path = tmp_path / "cable.yaml"
        path.write_text(
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: cable\n"
            "metadata:\n"
            "  name: cbl\n"
            "spec:\n"
            "  endpoints:\n"
            "    - sw-a:eth0\n"
            "    - sw-b:eth0\n"
            "  medium: copper\n",
            encoding="utf-8",
        )
        client = Driver(None)
        client.initialize(None)
        uri = client.open(path)
        diagnostics = client.wait_for_diagnostics(uri)
        assert not [entry for entry in diagnostics if entry["data"]["rule"] in LONE_FILE_RULES], (
            diagnostics
        )
        client.close()

    def test_renaming_needs_a_folder_and_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "switch.yaml"
        path.write_text(
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: switch\n"
            "metadata:\n"
            "  name: sw\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: eth0\n"
            "      type: ethernet\n",
            encoding="utf-8",
        )
        client = Driver(None)
        client.initialize(None)
        uri = client.open(path)
        client.wait_for_diagnostics(uri)
        error = client.error(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 3, "character": 8},
                "newName": "sw2",
            },
        )
        assert "open folder" in error["message"]
        client.close()


# --------------------------------------------------------------------------- #
# The caret, on its own
# --------------------------------------------------------------------------- #


class TestCursorContext:
    """:mod:`netgraph.lsp.context` — the layout scan, without a server."""

    @staticmethod
    def _at(text: str, line: int, character: int) -> Any:
        return context_at(TextDocument(uri="file:///x.yaml", text=text), Position(line, character))

    def test_a_key_inside_a_sequence_item(self) -> None:
        context = self._at("spec:\n  interfaces:\n    - name: eth0\n      m\n", 3, 7)
        assert context.slot is Slot.KEY
        assert context.path == ("spec", "interfaces", 0)
        assert context.prefix == "m"
        assert context.siblings == {"name"}

    def test_a_sequence_at_its_key_own_indent(self) -> None:
        context = self._at("spec:\n  interfaces:\n  - name: eth0\n    t\n", 3, 5)
        assert context.path == ("spec", "interfaces", 0)

    def test_the_index_counts_the_items_above(self) -> None:
        context = self._at("spec:\n  vlans:\n    - id: 10\n    - id: 20\n    - \n", 4, 6)
        assert context.path == ("spec", "vlans", 2)

    def test_a_value_after_a_bare_colon_needs_a_space(self) -> None:
        context = self._at("spec:\n  vendor:\n", 1, 10)
        assert context.slot is Slot.VALUE
        assert context.path == ("spec", "vendor")
        assert context.needs_space is True

    def test_flow_style_is_left_alone(self) -> None:
        context = self._at("spec:\n  trunk_vlans: [10, 2\n", 1, 22)
        assert context.slot is Slot.NONE

    def test_a_comment_is_left_alone(self) -> None:
        assert self._at("kind: switch  # x\n", 0, 17).slot is Slot.NONE

    def test_the_document_index_follows_the_separators(self) -> None:
        lines = ["---", "kind: switch", "---", "kind: cable", "spec:"]
        assert document_bounds(lines, 1) == (1, 2, 0)
        assert document_bounds(lines, 4)[2] == 1

    def test_a_file_without_separators_is_one_document(self) -> None:
        assert document_bounds(["kind: switch"], 0) == (0, 1, 0)

    def test_the_kind_is_read_from_the_document_the_caret_is_in(self) -> None:
        text = "kind: cable\nspec:\n---\nkind: switch\nspec:\n  v\n"
        assert self._at(text, 5, 3).kind == "switch"


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


class TestCommand:
    def test_the_command_serves_a_session_over_its_own_stdio(
        self, inventory: Path, tmp_path: Path
    ) -> None:
        """End to end through ``netgraph lsp``, as an editor would spawn it."""
        import subprocess
        import sys

        log = tmp_path / "lsp.log"
        payload = _frames(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "processId": None,
                        "rootUri": path_to_uri(inventory),
                        "capabilities": _CAPABILITIES,
                    },
                },
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}},
                {"jsonrpc": "2.0", "method": "exit", "params": {}},
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-m", "netgraph", "lsp", "--no-watch", "--log", str(log)],
            input=payload,
            capture_output=True,
            timeout=60,
            check=False,
            cwd=REPO_ROOT,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
        assert b"Content-Length: " in completed.stdout
        assert b'"serverInfo"' in completed.stdout
        assert log.exists()

    def test_it_exits_cleanly_when_the_client_leaves_the_pipe_open(self, inventory: Path) -> None:
        """A client may send ``exit`` and keep stdin open. Several do.

        The thread framing the client's stream is then still inside a blocking
        read when the interpreter finalises, which CPython answers by aborting
        the process — an editor reports that as a crash directly after a clean
        shutdown. Writing the frames one at a time and never closing the pipe is
        the only way to reproduce it.
        """
        import subprocess
        import sys

        proc = subprocess.Popen(
            [sys.executable, "-m", "netgraph", "lsp", "--no-watch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(
                _frames(
                    [
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "processId": None,
                                "rootUri": path_to_uri(inventory),
                                "capabilities": _CAPABILITIES,
                            },
                        },
                        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                        {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}},
                        {"jsonrpc": "2.0", "method": "exit", "params": {}},
                    ]
                )
            )
            proc.stdin.flush()
            # Deliberately no close() and no communicate(): the pipe stays open.
            assert proc.wait(timeout=60) == 0
            assert b"_enter_buffered_busy" not in proc.stderr.read()
        finally:
            if proc.poll() is None:  # pragma: no cover - only on a failure
                proc.kill()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def test_a_broken_stream_ends_the_session_without_a_traceback(self, inventory: Path) -> None:
        import subprocess
        import sys

        completed = subprocess.run(
            [sys.executable, "-m", "netgraph", "lsp", "--no-watch"],
            input=b"Content-Length: banana\r\n\r\n{}",
            capture_output=True,
            timeout=60,
            check=False,
            cwd=REPO_ROOT,
        )
        assert completed.returncode == 1
        assert b"Traceback" not in completed.stderr


def _frames(messages: Sequence[Mapping[str, Any]]) -> bytes:
    out = bytearray()
    for message in messages:
        body = json.dumps(message).encode("utf-8")
        out += b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
    return bytes(out)


# --------------------------------------------------------------------------- #
# The mapping spelling, and the rest of the reference roles
# --------------------------------------------------------------------------- #


@pytest.fixture
def patch_room(tmp_path: Path) -> Path:
    """A copy of ``examples/patch-room``: racks, panels, PDUs and power inputs."""
    root = tmp_path / "patch-room"
    shutil.copytree(REPO_ROOT / "examples" / "patch-room", root)
    return root


class TestPowerAndPanels:
    """``power.inputs`` is written as a mapping, which is a second code path."""

    def test_a_power_input_resolves_to_its_pdu(self, patch_room: Path) -> None:
        client = Driver(patch_room)
        client.initialize(patch_room)
        path = patch_room / "network" / "sw-core-01.yaml"
        uri = client.open(path)
        line = _line_of(path, "      - pdu: pdu-r1-a")
        column = _column_of(path, "      - pdu: pdu-r1-a", "pdu-r1-a")
        hover = client.result(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        assert isinstance(hover, Mapping)
        assert "pdu" in hover["contents"]["value"]
        assert "pdu-r1-a" in hover["contents"]["value"]

        definition = client.result(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        assert isinstance(definition, list) and definition
        assert _name_of(definition[0]["uri"]) == "pdus.yaml"
        client.close()

    def test_an_outlet_completes_from_the_pdu_written_beside_it(self, patch_room: Path) -> None:
        """The two halves are two keys, so the ``pdu:`` line is what says which."""
        client = Driver(patch_room)
        client.initialize(patch_room)
        path = patch_room / "network" / "new.yaml"
        text = (
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: server\n"
            "metadata:\n"
            "  name: srv-new\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: eth0\n"
            "      type: ethernet\n"
            "  power:\n"
            "    inputs:\n"
            "      - pdu: pdu-r1-a\n"
            "        outlet: \n"
        )
        uri = client.open(path, text)
        result = client.result(
            "textDocument/completion",
            {"textDocument": {"uri": uri}, "position": {"line": 11, "character": 16}},
        )
        assert isinstance(result, Mapping)
        labels = [item["label"] for item in result["items"]]
        assert labels[:3] == ["1", "2", "3"], labels[:5]
        assert all(item["detail"] == "outlet" for item in result["items"])
        client.close()

    def test_the_pdu_half_offers_only_pdus(self, patch_room: Path) -> None:
        client = Driver(patch_room)
        client.initialize(patch_room)
        path = patch_room / "network" / "new.yaml"
        text = (
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: server\n"
            "metadata:\n"
            "  name: srv-new\n"
            "spec:\n"
            "  power:\n"
            "    inputs:\n"
            "      - pdu: \n"
        )
        uri = client.open(path, text)
        result = client.result(
            "textDocument/completion",
            {"textDocument": {"uri": uri}, "position": {"line": 7, "character": 13}},
        )
        assert isinstance(result, Mapping)
        assert {item["label"] for item in result["items"]} == {
            "pdu-r1-a",
            "pdu-r1-b",
            "pdu-r2-a",
            "pdu-r2-b",
        }
        client.close()

    def test_references_to_a_pdu_find_every_supply_fed_from_it(self, patch_room: Path) -> None:
        client = Driver(patch_room)
        client.initialize(patch_room)
        path = patch_room / "power" / "pdus.yaml"
        uri = client.open(path)
        line = _line_of(path, "  name: pdu-r1-a")
        result = client.result(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": 9},
                "context": {"includeDeclaration": False},
            },
        )
        assert isinstance(result, list)
        assert sorted(_name_of(entry["uri"]) for entry in result) == [
            "rtr-edge-01.yaml",
            "sw-core-01.yaml",
        ]
        client.close()


class TestWorkspaceEdits:
    """The two shapes of ``WorkspaceEdit``, and what each can express."""

    @staticmethod
    def _builder(before: Mapping[str, str], **kwargs: Any) -> Any:
        from netgraph.lsp.edits import WorkspaceEditBuilder

        return WorkspaceEditBuilder(
            uri_of=lambda relative: f"file:///inv/{relative}",
            before_of=lambda relative: before.get(relative),
            version_of=lambda relative: 7 if relative in before else None,
            **kwargs,
        )

    def test_document_changes_carry_versions_and_operations(self) -> None:
        builder = self._builder({"a.yaml": "old\n"})
        edit = builder.build({"a.yaml": "new\n", "b.yaml": "made\n", "c.yaml": None})
        kinds = [entry.get("kind", "edit") for entry in edit["documentChanges"]]
        assert kinds == ["edit", "create", "edit", "delete"]
        change = edit["documentChanges"][0]
        assert change["textDocument"] == {"uri": "file:///inv/a.yaml", "version": 7}
        assert change["edits"][0]["newText"] == "new\n"
        assert change["edits"][0]["range"] == full_range("old\n").to_dict()

    def test_a_client_without_document_changes_gets_the_flat_map(self) -> None:
        builder = self._builder({"a.yaml": "old\n"}, document_changes=False)
        edit = builder.build({"a.yaml": "new\n", "gone.yaml": None})
        assert set(edit) == {"changes"}
        # A flat map has no way to say "delete"; saying nothing is the honest
        # answer, and the alternative — an empty file — would still be loaded.
        assert set(edit["changes"]) == {"file:///inv/a.yaml"}

    def test_a_rename_reaches_a_client_without_document_changes(self, inventory: Path) -> None:
        client = Driver(inventory)
        client.initialize(inventory, workspace={})
        switch = inventory / "switches" / "sw-home.yaml"
        uri = client.open(switch)
        result = client.result(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {
                    "line": _line_of(switch, "  name: sw-home"),
                    "character": _column_of(switch, "  name: sw-home", "sw-home"),
                },
                "newName": "sw-core",
            },
        )
        assert isinstance(result, Mapping)
        assert set(result) == {"changes"}
        assert len(result["changes"]) == 2
        client.close()


class TestTheWatcher:
    """The thread that reads the folder, and what happens when it cannot."""

    def test_a_watcher_that_cannot_start_is_reported_once(self, tmp_path: Path) -> None:
        from netgraph.lsp.watcher import FolderWatcher

        problems: list[str] = []
        watcher = FolderWatcher(
            tmp_path / "does-not-exist",
            on_change=lambda _: None,
            on_error=problems.append,
            debounce_ms=10,
        )
        watcher.start()
        watcher.join(TIMEOUT)
        assert problems and "file watching is off" in problems[0]

    def test_stopping_a_watcher_that_never_started_is_harmless(self, tmp_path: Path) -> None:
        from netgraph.lsp.watcher import FolderWatcher

        watcher = FolderWatcher(tmp_path, on_change=lambda _: None)
        watcher.stop()
        watcher.join(1.0)
