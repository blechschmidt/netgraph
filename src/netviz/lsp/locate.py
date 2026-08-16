"""Turning a loader mark into a range an editor can underline.

The loader records where a value was written as a 1-based line and column: the
start of the scalar, the sequence or the mapping. An editor wants a *span*, and
a span it can highlight without swallowing the rest of the file — a
``spec.interfaces`` block forty lines long is a correct range for a diagnostic
about the list and a useless one to look at.

So a range here is always within one line, and it ends where the token does. The
token is found by re-reading the source text at the mark, which is the only way
to be right about a quoted scalar (``"eth 0"`` ends at the closing quote, not at
the space) and about a trailing comment (``mtu: 900  # too small`` ends at the
``900``). Where the mark points at a container rather than a scalar, the range
covers the first line of it, which is the line carrying the key that opened it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from netviz.loader.inventory import SourceLocation
from netviz.loader.provenance import Site
from netviz.lsp.text import Encoding, Position, Range, index_to_character

__all__ = ["Located", "locate_path", "range_at", "scalar_span", "value_at"]


@dataclass(frozen=True, slots=True)
class Located:
    """A span in one file of the inventory."""

    #: POSIX path relative to the inventory root.
    relative: str
    range: Range


def scalar_span(line: str, column: int) -> tuple[int, int]:
    """The ``(start, end)`` indices of the token starting at 0-based ``column``.

    ``column`` is clamped into the line, so a mark that points past the end of a
    line — which happens for a value that is only implied, such as a missing
    mandatory key — degrades to an empty range at the end rather than to a
    negative one.
    """
    start = max(0, min(column, len(line)))
    if start >= len(line):
        return start, len(line)
    quote = line[start]
    if quote in "\"'":
        index = start + 1
        while index < len(line):
            character = line[index]
            if character == "\\" and quote == '"':
                index += 2
                continue
            if character == quote:
                if quote == "'" and index + 1 < len(line) and line[index + 1] == "'":
                    index += 2
                    continue
                return start, index + 1
            index += 1
        return start, len(line)
    end = len(line)
    comment = line.find(" #", start)
    if comment != -1:
        end = comment
    return start, len(line[:end].rstrip())


def range_at(text: str, line: int, column: int, encoding: Encoding = Encoding.UTF16) -> Range:
    """The range of the token at 1-based ``line``/``column`` of ``text``."""
    lines = text.split("\n")
    number = max(0, min(line - 1, len(lines) - 1))
    source = lines[number].rstrip("\r")
    start, end = scalar_span(source, column - 1)
    return Range(
        Position(number, index_to_character(source, start, encoding)),
        Position(number, index_to_character(source, end, encoding)),
    )


def value_at(data: Any, path: Sequence[str | int]) -> Any:
    """The value at ``path`` in a parsed document, or ``None`` if it is not there."""
    current = data
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, Sequence) or isinstance(current, (str, bytes)):
                return None
            if not -len(current) <= part < len(current):
                return None
            current = current[part]
            continue
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def locate_path(source: SourceLocation, path: Sequence[str | int]) -> Site | None:
    """The site of ``path`` inside the document ``source`` names.

    ``None`` when the element was assembled in memory rather than parsed, which
    is the one case with no line to point at.
    """
    return source.locate(tuple(path))
