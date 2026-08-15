"""The markdown subset (§21.1) as draw.io HTML, and back again.

A note's text is written in the markdown subset
:func:`~netgraph.render.annotations.parse_markup` defines. An mxGraph cell's
label is *HTML* — that is what ``html=1`` in a style means — so the exporter has
to render one into the other, and the importer has to read whatever draw.io's
rich-text editor left behind and turn it back into something a YAML document can
hold.

Two rules govern the pair, and they are not the same rule:

**Forwards is exact.** :func:`markup_html` is a total function of the parsed
blocks, so the importer can ask "was this note edited?" without a diff
algorithm: it renders the source stamped into the cell
(:data:`~netgraph.drawio.identity.ATTR_TEXT`) and compares the result with the
label byte for byte. That is what makes an untouched note produce *no*
operations on the way home, which is the guarantee the whole round trip rests
on.

**Backwards is best effort, and never lossy about words.**
:func:`html_to_markup` reads the five tags this module writes and the handful
draw.io's editor adds on its own, and anything else — a ``<font>``, a coloured
``<span>``, an underline — loses its *tag* and keeps its *text*. Dropping the
words would silently delete what somebody typed; keeping the tag would put HTML
into a YAML document that every other renderer would then print verbatim. Of the
three possible wrongs that is the least, and it is the one a reader can see and
fix.

The two are mutual inverses over the subset: ``html_to_markup(markup_html(
parse_markup(text)))`` is ``text`` again for anything §21.1 can express, which
``tests/test_drawio_annotations.py`` asserts over generated notes rather than
over examples.
"""

from __future__ import annotations

from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Final

from netgraph.render.annotations import Block, Span

__all__ = ["MAX_HTML_BYTES", "escape_html", "html_to_markup", "markup_html", "plain_text"]

#: Ceiling on one label read back from a diagram. A note is bounded at
#: :data:`~netgraph.models.annotation.MAX_TEXT_LENGTH` characters when netgraph
#: writes it; a label an editor rewrote can be larger, and the conversion is
#: linear, but a megabyte of markup in a cell is not a callout. Past the bound
#: the text is cut, and the model's own length rule reports it.
MAX_HTML_BYTES: Final = 64 * 1024

#: How each span style is spelled in an mxGraph label. ``<code>`` is not one of
#: draw.io's own toolbar buttons, but it is HTML and the browser that draws the
#: label renders it in a monospace face, which is the difference the author of a
#: note meant by the backticks.
_TAGS: Final[dict[str, str]] = {"bold": "b", "italic": "i", "code": "code"}

#: The inline markers, by the tag they arrive as. Both spellings of each: an
#: editor may write either, and ``<strong>`` means what ``<b>`` means.
_MARKERS: Final[dict[str, str]] = {
    "b": "**",
    "strong": "**",
    "i": "*",
    "em": "*",
    "code": "`",
}

#: Tags that end the line they are in. ``div`` and ``p`` are how draw.io's
#: editor separates paragraphs; ``br`` is what it writes for a plain newline.
_BREAKS: Final[frozenset[str]] = frozenset({"div", "p", "br", "ul", "ol", "li", "tr", "table"})

#: What HTML's five predefined entities are written as. Escaped here rather than
#: by the XML writer because the two escapings *compose*: this text becomes the
#: content of an HTML label, which then becomes the value of an XML attribute,
#: and an ampersand somebody typed has to survive both.
_ESCAPES: Final[tuple[tuple[str, str], ...]] = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
)


# --------------------------------------------------------------------------- #
# Forwards
# --------------------------------------------------------------------------- #


def markup_html(blocks: Sequence[Block]) -> str:
    """The parsed blocks of a note as the HTML an mxGraph label holds.

    Paragraphs become ``<div>``, which is what draw.io's own editor writes and
    therefore what it will still be writing after somebody has edited the note;
    a run of bullets becomes one ``<ul>``, because a list broken into one list
    per item draws with a gap between every line.

    Returns:
        The label, ready to be escaped into an XML attribute by
        :mod:`netgraph.drawio.mxfile`. Empty for a note with no blocks, which
        the model does not allow but a caller may still hand over.
    """
    parts: list[str] = []
    bullets: list[Block] = []

    def flush() -> None:
        if not bullets:
            return
        items = "".join(f"<li>{_spans_html(block.spans)}</li>" for block in bullets)
        parts.append(f"<ul>{items}</ul>")
        bullets.clear()

    for block in blocks:
        if block.kind == "bullet":
            bullets.append(block)
            continue
        flush()
        parts.append(f"<div>{_spans_html(block.spans)}</div>")
    flush()
    return "".join(parts)


def _spans_html(spans: Sequence[Span]) -> str:
    return "".join(_span_html(span) for span in spans)


def _span_html(span: Span) -> str:
    text = escape_html(span.text)
    tag = _TAGS.get(span.style)
    return f"<{tag}>{text}</{tag}>" if tag is not None else text


def escape_html(text: str) -> str:
    """One plain string as the content of an HTML label.

    For the captions that are *not* markdown — an area's label, a legend's
    title, a swatch's meaning. They are drawn in cells with ``html=1``, so a
    label holding a ``<`` would otherwise be read as the start of a tag and
    disappear.
    """
    for character, entity in _ESCAPES:
        text = text.replace(character, entity)
    return text


# --------------------------------------------------------------------------- #
# Backwards
# --------------------------------------------------------------------------- #


def html_to_markup(html: str) -> str:
    """An edited mxGraph label as the markdown subset, as far as it goes.

    Total: it never raises, whatever the editor left behind, because this runs
    on a file a third party handed back and refusing the whole import over one
    malformed span would lose every other edit in the diagram.

    Returns:
        The text for ``spec.text``. Structure the subset cannot hold is
        flattened rather than dropped: the words always survive.
    """
    return _read(html).result()


def plain_text(html: str) -> str:
    """An edited label as the one line of plain text a caption is.

    The inverse of :func:`escape_html` for anything it wrote, and a reasonable
    answer for anything it did not: an area whose caption somebody emboldened in
    draw.io comes back as the words, because ``spec.label`` is a string and a
    string is all it can hold.
    """
    return " ".join(text for _kind, text in _read(html, markers=False).lines())


def _read(html: str, *, markers: bool = True) -> _Reader:
    reader = _Reader(markers=markers)
    reader.feed(html[:MAX_HTML_BYTES])
    reader.close()
    return reader


class _Reader(HTMLParser):
    """One label, read into lines of markdown.

    ``convert_charrefs`` is left on, so ``&amp;`` reaches :meth:`handle_data` as
    an ampersand and the text that comes out is what the reader saw on the
    canvas rather than its encoding.
    """

    def __init__(self, *, markers: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self._markers = markers
        self._lines: list[tuple[str, str]] = []
        self._parts: list[str] = []
        self._open: list[str] = []
        self._bullet = False

    def _marker(self, tag: str) -> str | None:
        return _MARKERS.get(tag) if self._markers else None

    # -- structure --------------------------------------------------------- #

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        marker = self._marker(tag)
        if marker is not None:
            self._parts.append(marker)
            self._open.append(marker)
            return
        if tag in _BREAKS:
            self._flush()
            if tag == "li":
                self._bullet = True

    def handle_endtag(self, tag: str) -> None:
        marker = self._marker(tag)
        if marker is not None:
            # Close whatever is actually open rather than what the tag claims:
            # an editor that wrote ``<b><i>x</b></i>`` still meant both, and the
            # markers have to come off in the order they went on or the text
            # comes back with a stray asterisk in the middle of it.
            if marker in self._open:
                self._open.remove(marker)
            self._parts.append(marker)
            return
        if tag in _BREAKS:
            self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BREAKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    # -- assembly ---------------------------------------------------------- #

    def _flush(self) -> None:
        """End the line being built, if it has anything in it."""
        for marker in reversed(self._open):
            self._parts.append(marker)
        self._open.clear()
        text = " ".join("".join(self._parts).split())
        self._parts.clear()
        if text:
            self._lines.append(("bullet" if self._bullet else "paragraph", text))
        self._bullet = False

    def lines(self) -> tuple[tuple[str, str], ...]:
        """Every line read, as ``(kind, text)`` with ``kind`` the block kind."""
        self._flush()
        return tuple(self._lines)

    def result(self) -> str:
        """The lines, joined so that re-parsing them gives back these blocks.

        A blank line goes between two paragraphs, because
        :func:`~netgraph.render.annotations.parse_markup` joins consecutive
        lines into one; everywhere else a single newline is enough, since a
        bullet both ends the block before it and is ended by the line after it.
        """
        self._flush()
        out: list[str] = []
        for index, (kind, text) in enumerate(self._lines):
            if index:
                previous = self._lines[index - 1][0]
                out.append("\n\n" if previous == "paragraph" and kind == "paragraph" else "\n")
            out.append(f"- {text}" if kind == "bullet" else text)
        return "".join(out)
