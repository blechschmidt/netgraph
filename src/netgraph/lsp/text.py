"""Open buffers, and the arithmetic that turns an LSP position into an index.

A position in the protocol is a line and a *character*, and "character" means
whatever ``positionEncoding`` the client and the server agreed on in
``initialize``. The default is UTF-16 code units, which is Python's `str` index
only for text inside the basic multilingual plane — one emoji in a device
description and every column after it on that line is off by one. So the
conversion is explicit here, parameterised by the negotiated encoding, and every
feature converts through it rather than assuming.

The other job is applying ``textDocument/didChange``. Clients send incremental
ranges by default, and a server that mis-applies one diverges from the editor
silently: the diagnostics keep arriving, they just point at the wrong lines.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

__all__ = [
    "Encoding",
    "Position",
    "Range",
    "TextDocument",
    "full_range",
    "position_dict",
    "range_dict",
]


class Encoding(str, Enum):
    """How the client counts a column."""

    UTF8 = "utf-8"
    UTF16 = "utf-16"
    UTF32 = "utf-32"

    @classmethod
    def negotiate(cls, offered: Sequence[str] | None) -> Encoding:
        """The encoding to use given what the client says it supports.

        UTF-16 is the protocol's mandatory baseline, so it is the answer when
        the client offers nothing. UTF-32 is preferred where it is on offer:
        it is Python's own indexing, which means no conversion and no chance of
        one being wrong.
        """
        supported = set(offered or ())
        if cls.UTF32.value in supported:
            return cls.UTF32
        if cls.UTF8.value in supported:
            return cls.UTF8
        return cls.UTF16


@dataclass(frozen=True, slots=True, order=True)
class Position:
    """A zero-based line and character, as the protocol counts them."""

    line: int
    character: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Position:
        return cls(line=int(payload.get("line", 0)), character=int(payload.get("character", 0)))

    def to_dict(self) -> dict[str, int]:
        return {"line": self.line, "character": self.character}


@dataclass(frozen=True, slots=True, order=True)
class Range:
    """A half-open span between two positions."""

    start: Position
    end: Position

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Range:
        return cls(
            start=Position.from_dict(payload.get("start", {})),
            end=Position.from_dict(payload.get("end", {})),
        )

    @classmethod
    def at(cls, line: int, character: int, length: int = 0) -> Range:
        """A range starting at ``line``/``character`` and ``length`` long."""
        return cls(Position(line, character), Position(line, character + length))

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}

    def contains(self, position: Position) -> bool:
        """Is ``position`` inside, the end being exclusive unless empty?"""
        if position < self.start:
            return False
        return position <= self.end if self.start == self.end else position < self.end

    def touches(self, position: Position) -> bool:
        """Is ``position`` inside or at either edge?

        What a hover or a code action wants: a caret sitting immediately after
        the last character of a word is still on that word as far as a user is
        concerned.
        """
        return self.start <= position <= self.end

    def overlaps(self, other: Range) -> bool:
        """Do the two spans share any position, empty ones included?"""
        if self.start == self.end or other.start == other.end:
            return self.touches(other.start) or other.touches(self.start)
        return self.start < other.end and other.start < self.end


def position_dict(line: int, character: int) -> dict[str, int]:
    """The wire form of a position, for callers that never build a object."""
    return {"line": line, "character": character}


def range_dict(
    start_line: int, start_character: int, end_line: int, end_character: int
) -> dict[str, dict[str, int]]:
    """The wire form of a range."""
    return {
        "start": position_dict(start_line, start_character),
        "end": position_dict(end_line, end_character),
    }


@dataclass(slots=True)
class TextDocument:
    """One buffer the client has open, and what it currently holds."""

    uri: str
    text: str
    version: int = 0
    language_id: str = "yaml"
    _line_starts: list[int] | None = field(default=None, init=False, repr=False)

    # -- lines -----------------------------------------------------------

    @property
    def line_starts(self) -> list[int]:
        """Index of the first character of each line, computed once per edit."""
        if self._line_starts is None:
            starts = [0]
            for index, character in enumerate(self.text):
                if character == "\n":
                    starts.append(index + 1)
            self._line_starts = starts
        return self._line_starts

    @property
    def line_count(self) -> int:
        return len(self.line_starts)

    def line(self, number: int) -> str:
        """Line ``number`` (zero-based) without its terminator, ``""`` past the end."""
        starts = self.line_starts
        if not 0 <= number < len(starts):
            return ""
        start = starts[number]
        end = starts[number + 1] - 1 if number + 1 < len(starts) else len(self.text)
        return self.text[start:end].rstrip("\r")

    @property
    def lines(self) -> list[str]:
        return [self.line(number) for number in range(self.line_count)]

    # -- offsets ---------------------------------------------------------

    def offset_at(self, position: Position, encoding: Encoding = Encoding.UTF16) -> int:
        """The index into :attr:`text` of ``position``, clamped to the buffer."""
        starts = self.line_starts
        if position.line < 0:
            return 0
        if position.line >= len(starts):
            return len(self.text)
        start = starts[position.line]
        line = self.line(position.line)
        return start + character_to_index(line, position.character, encoding)

    def position_at(self, offset: int, encoding: Encoding = Encoding.UTF16) -> Position:
        """The position of the character at ``offset``."""
        offset = max(0, min(offset, len(self.text)))
        starts = self.line_starts
        number = bisect_right(starts, offset) - 1
        line = self.line(number)
        return Position(number, index_to_character(line, offset - starts[number], encoding))

    # -- editing ---------------------------------------------------------

    def replaced(self, text: str, version: int) -> TextDocument:
        """This document with entirely new contents."""
        return TextDocument(uri=self.uri, text=text, version=version, language_id=self.language_id)

    def apply(
        self,
        changes: Iterable[Mapping[str, Any]],
        version: int,
        encoding: Encoding = Encoding.UTF16,
    ) -> TextDocument:
        """This document with a ``didChange``'s content changes applied in order.

        A change without a ``range`` replaces the whole document, which is what
        a client using full synchronisation sends every time.
        """
        text = self.text
        document = self
        for change in changes:
            payload = change.get("range")
            replacement = str(change.get("text", ""))
            if payload is None:
                text = replacement
                document = TextDocument(uri=self.uri, text=text, language_id=self.language_id)
                continue
            span = Range.from_dict(payload)
            start = document.offset_at(span.start, encoding)
            end = document.offset_at(span.end, encoding)
            if end < start:
                start, end = end, start
            text = f"{text[:start]}{replacement}{text[end:]}"
            document = TextDocument(uri=self.uri, text=text, language_id=self.language_id)
        return TextDocument(uri=self.uri, text=text, version=version, language_id=self.language_id)

    # -- reading ---------------------------------------------------------

    def word_at(self, position: Position, encoding: Encoding = Encoding.UTF16) -> str:
        """The word the caret is in or immediately after, ``""`` when there is none.

        "Word" is netgraph's own vocabulary rather than the editor's: a name, a
        reference, a rule id, an interface, a prefix — so ``.``, ``-``, ``_``,
        ``/`` and ``:`` are all part of one.
        """
        line = self.line(position.line)
        index = character_to_index(line, position.character, encoding)
        start = index
        while start > 0 and _is_word(line[start - 1]):
            start -= 1
        end = index
        while end < len(line) and _is_word(line[end]):
            end += 1
        return line[start:end]


#: The characters a netgraph token is made of. See :meth:`TextDocument.word_at`.
_WORD: Final = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./:")


def _is_word(character: str) -> bool:
    return character in _WORD


def character_to_index(line: str, character: int, encoding: Encoding) -> int:
    """The Python index into ``line`` of protocol column ``character``.

    Out-of-range columns clamp to the end of the line, which the specification
    requires: a client is allowed to send a column past the end and expects it
    to mean "the end".
    """
    if character <= 0:
        return 0
    if encoding is Encoding.UTF32:
        return min(character, len(line))
    if encoding is Encoding.UTF8:
        counted = 0
        for index, symbol in enumerate(line):
            if counted >= character:
                return index
            counted += len(symbol.encode("utf-8"))
        return len(line)
    counted = 0
    for index, symbol in enumerate(line):
        if counted >= character:
            return index
        counted += 2 if ord(symbol) > 0xFFFF else 1
    return len(line)


def index_to_character(line: str, index: int, encoding: Encoding) -> int:
    """The protocol column of the Python index ``index`` into ``line``."""
    index = max(0, min(index, len(line)))
    if encoding is Encoding.UTF32:
        return index
    if encoding is Encoding.UTF8:
        return len(line[:index].encode("utf-8"))
    return sum(2 if ord(symbol) > 0xFFFF else 1 for symbol in line[:index])


def full_range(text: str, encoding: Encoding = Encoding.UTF16) -> Range:
    """The range covering the whole of ``text``.

    What a formatting edit and a whole-file workspace edit both replace.
    """
    lines = text.split("\n")
    last = len(lines) - 1
    return Range(
        Position(0, 0), Position(last, index_to_character(lines[last], len(lines[last]), encoding))
    )
