"""Translating between the ``file:`` URIs a client speaks and the paths we walk.

The Language Server Protocol identifies every document by URI, and netviz
identifies every document by a path relative to the inventory root. The two have
to line up exactly or a diagnostic is published against a document the editor
does not believe it has open, so the conversion lives in one place and is
tested against the shapes clients actually send: percent-encoded spaces, a
Windows drive letter behind a leading slash, and the empty authority
``file:///`` that every client writes and no client parses back.
"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, unquote, urlsplit

__all__ = ["is_file_uri", "path_to_uri", "relative_to", "uri_to_path"]

#: ``/c:/Users/...`` — the shape a Windows path takes inside a ``file:`` URI.
_DRIVE_RE = re.compile(r"^/([A-Za-z]):(/|$)")

#: Characters that stay literal in a path segment. The set RFC 8089 allows, less
#: the ones VS Code and Neovim both leave encoded, so a URI we emit compares
#: equal to the one the client sent for the same file.
_SAFE = "/:@"


def is_file_uri(uri: str) -> bool:
    """Does ``uri`` name something on this machine's filesystem?"""
    return urlsplit(uri).scheme == "file"


def uri_to_path(uri: str) -> Path | None:
    """The path ``uri`` names, or ``None`` when it does not name a local file.

    A client may open an untitled buffer (``untitled:Untitled-1``) or a document
    inside an archive; neither has a path, and neither is an inventory file.
    """
    parts = urlsplit(uri)
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    if parts.netloc and parts.netloc.lower() != "localhost":
        # A UNC share: file://server/share/x -> \\server\share\x.
        return Path(PureWindowsPath(f"//{parts.netloc}{path}"))
    drive = _DRIVE_RE.match(path)
    if drive is not None:
        return Path(PureWindowsPath(path[1:]))
    return Path(path)


def path_to_uri(path: Path) -> str:
    """``path`` as a ``file:`` URI, absolute and percent-encoded."""
    text = PureWindowsPath(path).as_posix() if "\\" in str(path) else path.as_posix()
    if _DRIVE_RE.match(f"/{text}") is not None:
        text = f"/{text}"
    if not text.startswith("/"):
        text = f"/{text}"
    return f"file://{quote(text, safe=_SAFE)}"


def relative_to(path: Path, root: Path) -> str | None:
    """``path`` as a POSIX path below ``root``, or ``None`` if it is not below it.

    Both sides are resolved first: an editor routinely hands out a path through
    a symlinked home directory, and the inventory root came from the command
    line. Comparing them unresolved is how a file ends up opened twice under two
    names, with two sets of diagnostics.
    """
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:  # pragma: no cover - resolution needs no filesystem in 3.10+
        resolved, base = path, root
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return None
