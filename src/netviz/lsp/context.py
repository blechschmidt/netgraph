"""Where in the document the caret is, worked out from the text as it stands.

Completion happens on text that is not valid YAML yet — that is the whole point
of it — so the parser cannot answer "what path am I at?". Half a key, a colon
with nothing after it, a sequence dash on its own: all of them are what a user
has typed a moment before they want the list.

So this module reads the *layout* instead. Indentation and the sequence dash are
what YAML's block style encodes structure with, and both survive an incomplete
line. The scan builds the stack of open containers line by line, exactly as the
parser would, and stops at the caret:

.. code-block:: yaml

    kind: switch          # a key of the root mapping
    spec:                 # opens a mapping at the next indent
      interfaces:         # opens a sequence
        - name: eth0      # item 0, which opens a mapping of its own
          m|              # caret: a key of item 0 -> spec.interfaces[0]

Two deliberate refusals. **Flow style** (``[a, b]``, ``{k: v}``) is not
analysed: it is legal, ``netviz fmt`` writes it for short scalar sequences,
and reconstructing a path through it from half a line is guesswork. **A comment**
is not a place to complete. In both cases the answer is "no context", and every
feature above treats that as "offer nothing", which is the only safe default —
a wrong path produces a confidently wrong completion list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Final

from netviz.lsp.text import (
    Encoding,
    Position,
    Range,
    TextDocument,
    character_to_index,
    index_to_character,
)

__all__ = [
    "CursorContext",
    "Slot",
    "context_at",
    "document_bounds",
    "document_index_at",
    "kind_of",
]

#: ``key:`` at the start of a line's content, with whatever follows it. The
#: space after the colon is required by YAML and is what keeps ``sw-home:eth0``
#: — a cable endpoint — from reading as a key.
_KEY_RE: Final = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:(?P<rest>[ \t].*|)$")

#: A document separator; only ``---`` starts a new one.
_SEPARATOR_RE: Final = re.compile(r"^(?P<token>---|\.\.\.)([ \t].*)?$")

#: ``kind: switch`` at the top level of a document.
_KIND_RE: Final = re.compile(r"^kind:[ \t]*(?P<kind>[A-Za-z][A-Za-z0-9_-]*)[ \t]*(?:#.*)?$")

#: Flow style before the caret means the layout scan cannot be trusted.
_FLOW: Final = frozenset("[]{}")

#: The empty ``written`` map, shared. See :attr:`CursorContext.written`.
_EMPTY_WRITTEN: Final[Mapping[str, str]] = MappingProxyType({})


class Slot(str, Enum):
    """What the caret is positioned to type."""

    #: A key of the mapping named by :attr:`CursorContext.path`.
    KEY = "key"
    #: The value of :attr:`CursorContext.path`, after its ``key:``.
    VALUE = "value"
    #: A bare item of the sequence named by :attr:`CursorContext.path`.
    ITEM = "item"
    #: Not a place to complete: a comment, flow style, a separator line.
    NONE = "none"


@dataclass(frozen=True, slots=True)
class CursorContext:
    """The caret's position in the document's structure."""

    slot: Slot
    #: For :attr:`Slot.KEY`, the mapping the key would join. For the other two,
    #: the value being written. ``()`` is the document itself.
    path: tuple[str | int, ...] = ()
    #: ``kind`` of the document the caret is in, when it declares one.
    kind: str | None = None
    #: 0-based index of that document within the file.
    index: int = 0
    #: What has been typed of the token so far.
    prefix: str = ""
    #: The span a completion replaces.
    replace: Range = field(default_factory=lambda: Range(Position(0, 0), Position(0, 0)))
    #: Keys already written in the same mapping, each with its inline value
    #: (``""`` when it opened a block). Keeps a key from being offered twice,
    #: and lets the ``outlet:`` half of a reference be completed from the
    #: ``pdu:`` half written beside it.
    #:
    #: A factory for the same reason as
    #: :attr:`netviz.config.ValidationConfig.severity`: a ``mappingproxy`` is
    #: not accepted as a plain dataclass default before Python 3.12, and as a
    #: plain default this raised ``ValueError`` while *importing* the module on
    #: 3.10 and 3.11 — so every one of them failed at collection.
    written: Mapping[str, str] = field(default_factory=lambda: _EMPTY_WRITTEN)
    #: The key whose value is being written, for :attr:`Slot.VALUE`.
    key: str | None = None
    #: The key written on the caret's line, whole, wherever in it the caret is.
    #: Hovering the middle of ``interfaces:`` has to describe ``interfaces``,
    #: not the mapping it belongs to, and :attr:`prefix` stops at the caret.
    line_key: str | None = None
    #: The caret is hard against the ``:``, so an inserted value needs a space.
    needs_space: bool = False

    @property
    def siblings(self) -> frozenset[str]:
        """The keys already written in the same mapping."""
        return frozenset(self.written)

    @property
    def is_completable(self) -> bool:
        return self.slot is not Slot.NONE


@dataclass(slots=True)
class _Frame:
    """One open container in the block structure."""

    indent: int
    path: tuple[str | int, ...]
    #: ``"map"`` or ``"seq"``.
    shape: str
    #: Index the next item of a sequence takes.
    count: int = 0
    #: Keys already seen in a mapping, each with its inline value.
    keys: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Documents within a file
# --------------------------------------------------------------------------- #


def _segments(lines: Sequence[str]) -> list[tuple[int, int]]:
    """``(start, end)`` of each document in ``lines``, ``end`` exclusive.

    Empty documents are kept: they consume an index (``NG-L004``) and every
    diagnostic in the file is anchored by one.
    """
    breaks = [
        number
        for number, text in enumerate(lines)
        if (match := _SEPARATOR_RE.match(text)) is not None and match.group("token") == "---"
    ]
    bounds: list[tuple[int, int]] = []
    previous = 0
    for number in breaks:
        bounds.append((previous, number))
        previous = number + 1
    bounds.append((previous, len(lines)))
    if breaks and _is_blank(lines, *bounds[0]):
        # A file opening with ``---`` has no document before it.
        bounds = bounds[1:]
    return bounds or [(0, len(lines))]


def _is_blank(lines: Sequence[str], start: int, end: int) -> bool:
    return all(
        not lines[number].strip() or lines[number].lstrip().startswith("#")
        for number in range(start, min(end, len(lines)))
    )


def document_bounds(lines: Sequence[str], line: int) -> tuple[int, int, int]:
    """The document containing ``line``: its first line, its last, and its index.

    A separator line belongs to the document it opens.
    """
    bounds = _segments(lines)
    for index, (start, end) in enumerate(bounds):
        if start <= line < end or (index == len(bounds) - 1 and line >= end):
            return start, end, index
        if line == start - 1:  # the ``---`` that opens this one
            return start, end, index
    return bounds[0][0], bounds[0][1], 0  # pragma: no cover - unreachable


def document_index_at(lines: Sequence[str], line: int) -> int:
    """The 0-based index of the document ``line`` falls in."""
    return document_bounds(lines, line)[2]


def kind_of(lines: Sequence[str], start: int, end: int) -> str | None:
    """The ``kind`` the document between ``start`` and ``end`` declares."""
    for number in range(start, min(end, len(lines))):
        match = _KIND_RE.match(lines[number])
        if match is not None:
            return match.group("kind")
    return None


# --------------------------------------------------------------------------- #
# The caret
# --------------------------------------------------------------------------- #


def context_at(
    document: TextDocument, position: Position, encoding: Encoding = Encoding.UTF16
) -> CursorContext:
    """What the caret at ``position`` is positioned to type."""
    lines = document.lines
    if not 0 <= position.line < len(lines):
        return CursorContext(slot=Slot.NONE)
    start, end, index = document_bounds(lines, position.line)
    kind = kind_of(lines, start, end)
    blank = CursorContext(slot=Slot.NONE, kind=kind, index=index)
    raw = lines[position.line]
    column = character_to_index(raw, position.character, encoding)
    head = raw[:column]
    if "#" in head or _FLOW & set(head) or "\t" in head:
        return blank
    if _SEPARATOR_RE.match(raw) is not None:
        return blank

    stack, pending = _scan(lines, start, position.line)
    indent = len(head) - len(head.lstrip(" "))
    content = head[indent:]
    is_item = content.startswith("-")
    _settle(stack, pending, indent, is_item)
    _pop_to(stack, indent, is_item)
    if not stack:
        stack.append(_Frame(indent=indent, path=(), shape="seq" if is_item else "map"))
    frame = stack[-1]

    if is_item and frame.shape == "seq":
        after = content[1:]
        offset = indent + 1 + (len(after) - len(after.lstrip(" ")))
        return _classify(
            document=document,
            position=position,
            encoding=encoding,
            content=raw[offset:column],
            offset=offset,
            path=(*frame.path, frame.count),
            bare=Slot.ITEM,
            kind=kind,
            index=index,
            written={},
            line_key=_line_key(raw[offset:]),
        )
    return _classify(
        document=document,
        position=position,
        encoding=encoding,
        content=content,
        offset=indent,
        path=frame.path,
        bare=Slot.KEY,
        kind=kind,
        index=index,
        written=dict(frame.keys),
        line_key=_line_key(raw[indent:]),
    )


def _classify(
    *,
    document: TextDocument,
    position: Position,
    encoding: Encoding,
    content: str,
    offset: int,
    path: tuple[str | int, ...],
    bare: Slot,
    kind: str | None,
    index: int,
    written: Mapping[str, str],
    line_key: str | None,
) -> CursorContext:
    """The context for a caret ``content`` characters into a slot.

    ``bare`` is what the slot is when no ``key:`` has been typed into it: a key
    of the surrounding mapping, or an item of the surrounding sequence.

    One ambiguity is resolved against netviz rather than against YAML. Inside
    a sequence, ``- sw-home:`` with nothing after the colon is a mapping to a
    parser and a half-typed ``device:interface`` to everybody else — the
    sequences netviz's schema fills with mappings are written ``- name: eth0``
    with a value, and the ones written bare hold references. So a colon at the
    very end of a sequence item leaves the slot an item, and typing a space
    after it turns it into a key, which is exactly what typing that space means.
    """
    match = _KEY_RE.match(content)
    if match is not None and not (bare is Slot.ITEM and not match.group("rest")):
        key = match.group("key")
        rest = match.group("rest")
        value = rest.lstrip(" \t")
        return CursorContext(
            slot=Slot.VALUE,
            path=(*path, key),
            kind=kind,
            index=index,
            prefix=value,
            replace=_span(document, position, encoding, offset + len(content) - len(value)),
            key=key,
            line_key=key,
            written=MappingProxyType(dict(written)),
            needs_space=not rest,
        )
    return CursorContext(
        slot=bare,
        path=path,
        kind=kind,
        index=index,
        prefix=content,
        replace=_span(document, position, encoding, offset),
        written=MappingProxyType(dict(written)),
        line_key=line_key,
    )


def _line_key(content: str) -> str | None:
    """The key ``content`` opens with, ignoring where the caret happens to be."""
    match = _KEY_RE.match(content.rstrip())
    return None if match is None else match.group("key")


def _span(
    document: TextDocument, position: Position, encoding: Encoding, start_index: int
) -> Range:
    """The range from ``start_index`` on the caret's line to the caret."""
    line = document.line(position.line)
    return Range(Position(position.line, index_to_character(line, start_index, encoding)), position)


def _scan(
    lines: Sequence[str], start: int, stop: int
) -> tuple[list[_Frame], tuple[tuple[str | int, ...], int] | None]:
    """The open containers after reading ``lines[start:stop]``.

    Returns the stack and, when the last structural line was a key with no
    inline value, the container that key is about to open — which cannot be
    classified until the next line says whether it is a sequence or a mapping.
    """
    stack: list[_Frame] = []
    pending: tuple[tuple[str | int, ...], int] | None = None
    for number in range(start, min(stop, len(lines))):
        raw = lines[number]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or _SEPARATOR_RE.match(raw) is not None:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        is_item = stripped == "-" or stripped.startswith("- ")
        _settle(stack, pending, indent, is_item)
        pending = None
        _pop_to(stack, indent, is_item)
        if not stack:
            stack.append(_Frame(indent=indent, path=(), shape="seq" if is_item else "map"))
        frame = stack[-1]
        content, offset = stripped, indent
        if is_item and frame.shape == "seq":
            item = (*frame.path, frame.count)
            frame.count += 1
            after = stripped[1:]
            offset = indent + 1 + (len(after) - len(after.lstrip(" ")))
            content = after.lstrip(" ")
            if not content:
                pending = (item, indent)
                continue
            stack.append(_Frame(indent=offset, path=item, shape="map"))
            frame = stack[-1]
        match = _KEY_RE.match(content)
        if match is None:
            continue
        key = match.group("key")
        frame.keys[key] = match.group("rest").strip()
        if not match.group("rest").strip():
            pending = ((*frame.path, key), offset)
    return stack, pending


def _settle(
    stack: list[_Frame],
    pending: tuple[tuple[str | int, ...], int] | None,
    indent: int,
    is_item: bool,
) -> None:
    """Turn a key that opened a block into the frame the next line proves it is.

    A sequence may sit at its key's own indent (``interfaces:`` then ``- name``
    in column 0), a mapping may not — which is exactly the test.
    """
    if pending is None:
        return
    path, key_indent = pending
    if indent > key_indent or (is_item and indent >= key_indent):
        stack.append(_Frame(indent=indent, path=path, shape="seq" if is_item else "map"))


def _pop_to(stack: list[_Frame], indent: int, is_item: bool) -> None:
    """Close every container the line at ``indent`` is outside of."""
    shape = "seq" if is_item else "map"
    while stack:
        top = stack[-1]
        if top.indent > indent or (top.indent == indent and top.shape != shape):
            stack.pop()
            continue
        break
