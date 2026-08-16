"""The wire: JSON-RPC 2.0 in ``Content-Length`` frames, over two byte streams.

This is the whole reason ``netviz lsp`` needs no new dependency. The Language
Server Protocol's transport is base protocol headers followed by a UTF-8 JSON
body, and reading it correctly is a hundred lines: read header lines until a
blank one, take ``Content-Length`` as a byte count, read exactly that many
bytes, parse.

Two rules are worth stating because getting either wrong produces a server that
works against one editor and hangs against the next:

* **The length is in bytes, not characters.** A single non-ASCII character in a
  device description makes the two differ, and a reader that counts characters
  then desynchronises for the rest of the session.
* **Writes are atomic and flushed.** A client is waiting on the response to a
  request before it sends the next one; a half-written frame is a deadlock, not
  a slow reply. The lock makes a background thread's notification safe to
  interleave with the main loop's responses.
"""

from __future__ import annotations

import json
import threading
from typing import IO, Any, Final

__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "REQUEST_CANCELLED",
    "REQUEST_FAILED",
    "SERVER_NOT_INITIALIZED",
    "Connection",
    "ProtocolError",
    "ResponseError",
]

#: JSON-RPC's own codes.
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
#: LSP's additions (§3.16 "Response Message").
SERVER_NOT_INITIALIZED: Final = -32002
REQUEST_FAILED: Final = -32803
REQUEST_CANCELLED: Final = -32800

#: Refuse a frame larger than this rather than allocate it. An inventory
#: document is kilobytes; 64 MiB is a corrupt stream or a hostile one.
MAX_CONTENT_LENGTH: Final = 64 * 1024 * 1024


class ProtocolError(Exception):
    """The stream is not a valid sequence of base-protocol frames."""


class ResponseError(Exception):
    """A request that cannot be answered, in the form the client expects."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error

    def __str__(self) -> str:
        return f"{self.message} ({self.code})"


class Connection:
    """One client, read from and written to as framed JSON."""

    def __init__(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = threading.Lock()

    # -- reading ---------------------------------------------------------

    def read(self) -> dict[str, Any] | None:
        """The next message, or ``None`` at end of stream.

        Raises:
            ProtocolError: The headers are malformed or the body is not a JSON
                object. Neither is recoverable — the stream position after a bad
                frame is unknown — so the caller's only move is to stop.
        """
        headers = self._read_headers()
        if headers is None:
            return None
        raw = headers.get("content-length")
        if raw is None:
            raise ProtocolError("frame has no Content-Length header")
        try:
            length = int(raw)
        except ValueError:
            raise ProtocolError(f"Content-Length is not a number: {raw!r}") from None
        if length < 0 or length > MAX_CONTENT_LENGTH:
            raise ProtocolError(f"Content-Length out of range: {length}")
        body = self._read_exactly(length)
        if body is None:
            raise ProtocolError(f"stream ended {length} bytes into a frame")
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"frame is not UTF-8 JSON: {exc}") from exc
        if not isinstance(message, dict):
            raise ProtocolError("frame is not a JSON object")
        return message

    def _read_headers(self) -> dict[str, str] | None:
        headers: dict[str, str] = {}
        while True:
            line = self._reader.readline()
            if not line:
                if headers:
                    raise ProtocolError("stream ended in the middle of a header block")
                return None
            if line in (b"\r\n", b"\n"):
                return headers
            try:
                name, colon, value = line.decode("ascii").partition(":")
            except UnicodeDecodeError:
                raise ProtocolError("header line is not ASCII") from None
            if not colon:
                raise ProtocolError(f"header line has no colon: {line!r}")
            headers[name.strip().lower()] = value.strip()

    def _read_exactly(self, length: int) -> bytes | None:
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self._reader.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # -- writing ---------------------------------------------------------

    def write(self, message: dict[str, Any]) -> None:
        """Frame and send one message, in full or not at all."""
        body = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        with self._write_lock:
            self._writer.write(frame)
            self._writer.flush()
