"""What a query says when it cannot be read, and how it points at the problem.

A selector is typed once and read for years, so a rejection has to do more than
say *no*. Every error here carries the span it is about — an offset into the
query text and a length — and renders as the three lines a compiler renders::

    query:1:8: 'swtch' is not an element kind
      kind = swtch and vlan = 10
             ^^^^^
      help: expected one of adapter, computer, firewall, group, hub, …

The first line is the loader's own diagnostic shape (``<source>:<line>:<column>:
<message>``, :meth:`netgraph.loader.inventory.LoadError.location`), so a reader
who has seen one netgraph diagnostic recognises this one. The caret line under
it is what a query can have and a YAML document cannot: the whole subject fits
on one line, so the offending token can simply be underlined.

Everything is bounded. A query arrives from a shell, an HTTP request or a
document, so its length is not under our control; the echoed line is clipped to
:data:`MAX_QUERY_ECHO` characters around the span rather than reproduced whole,
and the underline is clipped with it. That is the same discipline
:func:`~netgraph.errors.echo_value` applies to a rejected scalar, for the same
reason: a diagnostic must not be a copy of its input.
"""

from __future__ import annotations

from typing import Final

from netgraph.errors import NetgraphError

__all__ = ["MAX_QUERY_ECHO", "MAX_QUERY_LENGTH", "QueryError"]

#: How much of the query line a diagnostic reproduces, centred on the span it
#: underlines. Wide enough for every query in the cookbook and narrow enough
#: that a pathological one does not become its own error message.
MAX_QUERY_ECHO: Final = 160

#: Longest query the parser accepts at all. A query is an expression a person
#: types, not a program: past this it is a generated string, and the honest
#: answer is to refuse it before allocating a token per character.
MAX_QUERY_LENGTH: Final = 4096

#: What replaces the elided part of an over-long echo, at either end.
_ELLIPSIS: Final = "…"


def _printable(text: str) -> str:
    """``text`` with every control character replaced by one space.

    Two reasons, and the second is the one that is not obvious. A tab would put
    the carets in the wrong column, since the block is aligned by counting
    characters and rendered by a terminal that counts stops. And Python treats
    ``\x1c`` to ``\x1e`` as *both* whitespace and line breaks, so a query holding
    one is scanned as a single line and then split into two by anything that
    calls :meth:`str.splitlines` on the diagnostic — which puts the caret line
    under a line that is not the one it marks.

    One space per character, never zero and never two, because the caret's
    position is an index into this string.
    """
    return "".join(
        " " if character < " " or character in _SEPARATORS else character for character in text
    )


#: The characters Python calls line breaks beyond ``\n`` and ``\r``. Each is
#: whitespace to the lexer and a line break to :meth:`str.splitlines`, which is
#: the disagreement :func:`_printable` exists to settle.
_SEPARATORS: Final = "\x1c\x1d\x1e\x85\u2028\u2029"


class QueryError(NetgraphError):
    """A query that cannot be parsed or cannot be evaluated.

    Usage rather than data: the exit code is 2, the one every other
    "you asked for something impossible" answer uses
    (:class:`~netgraph.errors.ConfigurationError`), because a bad query is a bad
    argument and not a bad inventory.
    """

    exit_code = 2

    def __init__(
        self,
        message: str,
        *,
        text: str = "",
        offset: int = 0,
        length: int = 1,
        help: str | None = None,
        source: str = "query",
    ) -> None:
        #: The problem, as one sentence and without the location.
        self.message = message
        #: The query as written.
        self.text = text
        #: Where the problem starts, as a 0-based offset into :attr:`text`.
        self.offset = max(0, min(offset, len(text)))
        #: How many characters it spans. At least 1, so there is always a caret.
        self.length = max(1, length)
        #: A second sentence suggesting what to write instead, or ``None``.
        self.help = help
        #: What the first line names as the origin of the text.
        self.source = source
        super().__init__(self.annotated())

    @property
    def column(self) -> int:
        """1-based column of the first offending character."""
        return self.offset + 1

    @property
    def line(self) -> int:
        """1-based line of the span. A query is one line unless it was written
        in a document, where a newline is legal whitespace."""
        return self.text.count("\n", 0, self.offset) + 1

    @property
    def location(self) -> str:
        """``query:1:8`` — the loader's location shape, for a one-line subject."""
        return f"{self.source}:{self.line}:{self.column}"

    def annotated(self) -> str:
        """The full diagnostic: location, message, the line, and the carets."""
        echo, caret = self._underline()
        block = f"{self.location}: {self.message}"
        if echo:
            block += f"\n  {echo}\n  {caret}"
        if self.help:
            block += f"\n  help: {self.help}"
        return block

    def _underline(self) -> tuple[str, str]:
        """The echoed line and the caret line under it, both clipped together.

        Clipping is done on the *line the span is in* and centred on the span,
        so underlining column 3000 of a generated query still shows the reader
        the tokens either side of the problem rather than the first 160
        characters of something else.

        The caret may land **one column past** the last character, and does for
        every "this ends too early" diagnostic: ``kind = switch and`` is wrong
        at the position after ``and``, and pointing at the ``d`` instead would
        underline the one token that is not the problem. That is what every
        compiler does, and it is the only case where the caret line is longer
        than the line above it.
        """
        if not self.text:
            return "", ""
        start = self.text.rfind("\n", 0, self.offset) + 1
        end = self.text.find("\n", self.offset)
        if end == -1:
            end = len(self.text)
        line = self.text[start:end]
        # The span, expressed within the line, and never past its end: an error
        # reported at end-of-input points one past the last character.
        span_at = self.offset - start
        span_len = max(1, min(self.length, max(1, len(line) - span_at)))

        left, right = 0, len(line)
        if len(line) > MAX_QUERY_ECHO:
            half = MAX_QUERY_ECHO // 2
            left = max(0, min(span_at - half, len(line) - MAX_QUERY_ECHO))
            right = left + MAX_QUERY_ECHO
        head = _ELLIPSIS if left > 0 else ""
        tail = _ELLIPSIS if right < len(line) else ""
        echo = f"{head}{line[left:right]}{tail}"

        echo = _printable(echo)
        pad = len(head) + max(0, span_at - left)
        carets = "^" * max(1, min(span_len, max(1, len(echo) - pad)))
        return echo, " " * pad + carets
