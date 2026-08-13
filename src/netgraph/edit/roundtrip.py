"""One file, held as documents that can be edited without touching the others.

``netgraph fmt`` may rewrite a whole file, because rewriting the whole file *is*
what it was asked to do. An edit is the opposite: it changes one value in one
document, and every other byte of that file — the comment above the next key,
the blank line somebody put there to separate two racks, the single quotes
around a MAC address, the order the keys happen to be in — has to come out the
other side unchanged. A reviewer must be able to look at the diff and see the
edit, not the editor.

So a file is not held as one YAML stream here. It is held as:

* a **preamble**: everything before the first document, which is where a file's
  header comment lives and which ``ruamel`` drops on the floor when it dumps a
  stream;
* a list of **documents**, each keeping the exact source text it was read as,
  and each parsed round-trip only if something asks to *change* it.

Rendering concatenates them. A document nobody touched contributes its original
bytes verbatim, so the only text that can possibly move is text inside a
document an operation named. That is the whole trick, and it is what makes the
"only the intended hunk changes" assertion in the tests provable rather than
hopeful.

Inside a touched document the guarantee is weaker but still strong. ``ruamel``'s
round-trip parser keeps comments, blank lines, quoting style and key order, but
its emitter has to be told how the file indents its sequences before it can
reproduce them — so :func:`_style_for` *probes*: it dumps the untouched document
with each candidate style and keeps the first one that reproduces the source
byte for byte. A file written the way ``netgraph fmt`` writes it, and a file
written the other common way, both round-trip exactly.

Two further properties of the original file are recovered and restored, because
both are invisible in the parsed form and both would otherwise turn a one-line
edit into a whole-file diff: a UTF-8 byte-order mark, and CRLF line endings.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any, Final

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from netgraph.edit.errors import RoundTripError
from netgraph.fmt.canonical import INDENT, SEQUENCE_INDENT, SEQUENCE_OFFSET, format_stream

__all__ = [
    "STYLES",
    "IndentStyle",
    "YamlDocument",
    "YamlFile",
    "dump_document",
]

#: A byte-order mark, as it decodes. Written by some Windows editors, invisible
#: everywhere, and a whole-file diff if it is dropped.
_BOM: Final = "﻿"

#: ``---`` on a line of its own, which is how every document but the first is
#: introduced and how ``netgraph fmt`` writes them.
_MARKER_RE: Final = re.compile(r"^---([ \t]*\n|[ \t]*$)")


@dataclass(frozen=True, slots=True)
class IndentStyle:
    """How a file indents mappings and sequences, as ``ruamel`` spells it."""

    mapping: int = INDENT
    sequence: int = SEQUENCE_INDENT
    offset: int = SEQUENCE_OFFSET


#: The styles :func:`_style_for` tries, in order. The first is the canonical
#: form ``netgraph fmt`` writes; the second is what a sequence written flush
#: with its key looks like, which is the other style found in the wild. The
#: rest are rarer but cost one dump each to rule out.
STYLES: Final[tuple[IndentStyle, ...]] = (
    IndentStyle(2, 4, 2),
    IndentStyle(2, 2, 0),
    IndentStyle(2, 4, 0),
    IndentStyle(2, 6, 4),
    IndentStyle(4, 8, 4),
)


def _yaml(style: IndentStyle) -> YAML:
    """A round-trip ``YAML`` configured to reproduce ``style``.

    Built per call for the reason :func:`netgraph.fmt.canonical._yaml` gives:
    the object carries parser and emitter state, and sharing one across
    thousands of documents is how that state leaks from one into the next.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=style.mapping, sequence=style.sequence, offset=style.offset)
    # Never let the emitter wrap on its own: a line it folded would be a change
    # to a line nobody edited.
    yaml.width = 1 << 30
    yaml.explicit_start = False
    yaml.explicit_end = False
    yaml.default_flow_style = False
    return yaml


def dump_document(data: Any, style: IndentStyle) -> str:
    """Emit one document, without a ``---`` marker and without a preamble."""
    stream = io.StringIO()
    _yaml(style).dump(data, stream)
    return stream.getvalue()


@dataclass(eq=False)
class YamlDocument:
    """One document of a file, as source text and — on demand — as a tree.

    ``text`` is the source exactly as it was read, with the ``---`` line that
    introduced it stripped off (:attr:`marker` remembers there was one). It is
    what :meth:`YamlFile.render` writes back for a document nothing changed, and
    it is what makes an untouched document impossible to reformat by accident.
    """

    #: Source text of the document, without its ``---`` marker.
    text: str
    #: Was this document introduced by a ``---`` line?
    marker: bool = False
    #: ``--- key: value`` — a marker with content on the same line. Such a
    #: document is rendered verbatim and refuses to be edited, because
    #: re-emitting it would move the first key onto its own line.
    inline: bool = False
    _data: Any = field(default=None, repr=False)
    _parsed: bool = field(default=False, repr=False)
    _style: IndentStyle | None = field(default=None, repr=False)
    #: Did some candidate style reproduce the source byte for byte? When it did
    #: not, re-emitting this document will move lines nobody edited — which is
    #: also why an operation on it cannot claim a semantic inverse.
    _faithful: bool = field(default=False, repr=False)
    #: Was the source already in canonical form? If it was, a mutated document
    #: is put back through the formatter so the file stays canonical; if it was
    #: not, it is emitted as it is, because reformatting it would bury the edit.
    _canonical: bool = field(default=False, repr=False)
    #: Set once something has asked for the tree *and* means to change it.
    dirty: bool = False

    @property
    def data(self) -> Any:
        """The document as a ``ruamel`` tree, parsed and style-probed on demand.

        Raises:
            RoundTripError: The document cannot be round-tripped (see
                :attr:`inline`), or is not well-formed YAML.
        """
        if not self._parsed:
            self._parse()
        return self._data

    def touch(self) -> Any:
        """Mark the document changed and return its tree.

        Raises:
            RoundTripError: As :attr:`data`.
        """
        data = self.data
        self.dirty = True
        return data

    @property
    def faithful(self) -> bool:
        """Would re-emitting this document reproduce its source exactly?"""
        if not self._parsed:
            self._parse()
        return self._faithful

    def _parse(self) -> None:
        if self.inline:
            raise RoundTripError(
                "this document is introduced by an inline '--- key: value' marker, which "
                "cannot be edited without moving its first key; run 'netgraph fmt' on the "
                "file first"
            )
        self._style, self._data, self._faithful = _style_for(self.text)
        self._parsed = True
        self._canonical = _is_canonical(self.text)

    def render(self) -> str:
        """The document's text: verbatim when untouched, re-emitted when not."""
        if not self.dirty:
            return self.text
        style = self._style or STYLES[0]
        emitted = dump_document(self._data, style)
        if not self._canonical:
            return emitted
        # The file was canonical before the edit, so it should be canonical
        # after it: a key added at the end of a mapping belongs wherever the
        # schema order puts it, and a value's quoting follows the same rule it
        # would follow anywhere else. Formatting a document that was *not*
        # canonical is the case this deliberately does not do -- it would bury
        # the edit under a reformat nobody asked for.
        try:
            return format_stream(emitted)
        except Exception:  # pragma: no cover - format_stream re-reads its own output
            return emitted


def _is_canonical(text: str) -> bool:
    """Is ``text`` already what ``netgraph fmt`` would write?"""
    try:
        return format_stream(text) == text
    except Exception:  # pragma: no cover - the document parsed, so this cannot fail
        return False


def _style_for(text: str) -> tuple[IndentStyle, Any, bool]:
    """The indent style that reproduces ``text``, the tree it parsed to, and whether it does.

    Falls back to the canonical style when no candidate reproduces the source —
    which happens for a document whose layout no fixed indent describes, a flow
    mapping spread over several lines say. The edit still goes through; the
    diff is then wider than one hunk, which is the honest outcome and is why
    ``fmt`` exists.

    Raises:
        RoundTripError: The round-trip parser cannot read the document. Not the
            same thing as "the document is invalid": ``ruamel`` resolves scalars
            by YAML 1.1 rules and then converts them, and the two do not quite
            agree — an interface named ``-._`` matches its float pattern and
            reaches ``float("-.")``. netgraph's own loader reads that scalar as
            the string it plainly is, so this arrives on a document ``validate``
            accepts, and it has to be a refusal rather than a traceback. The
            same case is handled the same way in
            :func:`netgraph.fmt.canonical.format_stream`.
    """
    first: Any = None
    for index, style in enumerate(STYLES):
        try:
            data = _yaml(style).load(text)
        except YAMLError as exc:
            raise RoundTripError(f"cannot parse document: {exc}") from exc
        except (ArithmeticError, LookupError, TypeError, ValueError) as exc:
            # By category rather than by type: what these have in common is that
            # they are what a *parser* fails with, and which one arrives depends
            # on the scalar pattern the document tripped.
            raise RoundTripError(
                f"the round-trip parser could not read this document "
                f"({type(exc).__name__}: {exc}). It may hold a scalar that parser "
                f"resolves differently from the loader; quoting the value makes "
                f"both agree, and 'netgraph fmt' reports the same thing."
            ) from exc
        if index == 0:
            first = data
        if dump_document(data, style) == text:
            return style, data, True
    return STYLES[0], first, False


@dataclass(eq=False)
class YamlFile:
    """A YAML file, split into the parts an edit has to keep apart."""

    #: Path relative to the inventory root, POSIX style.
    relative: str
    #: Everything before the first document: a header comment, blank lines, or
    #: nothing at all. ``ruamel`` cannot round-trip it, so it is never parsed.
    preamble: str = ""
    documents: list[YamlDocument] = field(default_factory=list)
    #: ``\n`` or ``\r\n``, whichever the file used.
    newline: str = "\n"
    #: Did the file start with a byte-order mark?
    bom: bool = False
    #: True for a file this session invents, which has no bytes on disk yet.
    created: bool = False

    @classmethod
    def parse(cls, text: str, *, relative: str) -> YamlFile:
        """Split ``text`` into a preamble and its documents.

        The split points come from the YAML parser rather than from a search
        for ``---``, so a ``---`` inside a block scalar is text and not a
        document boundary.

        Raises:
            RoundTripError: ``text`` is not well-formed YAML.
        """
        bom = text.startswith(_BOM)
        if bom:
            text = text[len(_BOM) :]
        newline = "\r\n" if "\r\n" in text else "\n"
        body = text.replace("\r\n", "\n")

        try:
            starts = [
                event.start_mark.index
                for event in pyyaml.parse(body, Loader=pyyaml.SafeLoader)
                if isinstance(event, pyyaml.DocumentStartEvent)
            ]
        except pyyaml.YAMLError as exc:
            raise RoundTripError(f"cannot parse {relative}: {exc}") from exc

        if not starts:
            # Comments and blank lines only: nothing to edit, everything to keep.
            return cls(relative=relative, preamble=body, newline=newline, bom=bom)

        bounds = [*starts, len(body)]
        documents = []
        for index in range(len(starts)):
            documents.append(_document(body[bounds[index] : bounds[index + 1]]))
        return cls(
            relative=relative,
            preamble=body[: starts[0]],
            documents=documents,
            newline=newline,
            bom=bom,
        )

    @classmethod
    def empty(cls, relative: str) -> YamlFile:
        """A file this session is creating, with nothing in it yet."""
        return cls(relative=relative, created=True)

    def render(self) -> str:
        """The file as it should be written: preamble, documents, separators.

        The first document keeps whatever marker it was written with; every
        later one gets the ``---`` it needs to be a document at all. So deleting
        the first document of a file promotes the second without leaving a stray
        separator behind, and appending one adds exactly the separator and the
        document.
        """
        parts = [self.preamble]
        for index, document in enumerate(self.documents):
            marker = document.marker if index == 0 else True
            if marker and not document.inline:
                parts.append("---\n")
            parts.append(document.render())
        text = "".join(parts)
        if self.newline != "\n":
            text = text.replace("\n", self.newline)
        return _BOM + text if self.bom else text

    @property
    def is_empty(self) -> bool:
        """Nothing left worth keeping — the file should go.

        A file whose last document was deleted still has its preamble, and a
        header comment is not a reason to keep an empty file around: the thing
        it was a header *for* is gone.
        """
        return not self.documents

    def insert(self, index: int, document: YamlDocument) -> None:
        """Put ``document`` at ``index``, adjusting the ``---`` markers.

        A document that lands anywhere but first needs a marker; a document that
        lands first inherits the marker style of the one it displaced, so a file
        that opened with an explicit ``---`` still does.
        """
        if index == 0 and self.documents:
            document.marker = self.documents[0].marker
        elif index > 0:
            document.marker = True
        self.documents.insert(index, document)

    def remove(self, index: int) -> YamlDocument:
        """Take the document at ``index`` out, adjusting the ``---`` markers."""
        removed = self.documents.pop(index)
        if index == 0 and self.documents:
            self.documents[0].marker = removed.marker
        return removed


def _document(segment: str) -> YamlDocument:
    """One split-out segment, as a document plus what introduced it."""
    match = _MARKER_RE.match(segment)
    if match is not None:
        return YamlDocument(text=segment[match.end() :], marker=True)
    if segment.startswith("---"):
        return YamlDocument(text=segment, marker=False, inline=True)
    return YamlDocument(text=segment, marker=False)
