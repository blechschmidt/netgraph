"""Turning relational query text into tokens that remember where they came from.

Same discipline as the selector's scanner (:mod:`netviz.query.lexer`) — every
token carries its offset and length so a diagnostic can underline it — and one
decision made the other way round, which is worth naming because it is the
visible difference between the two languages.

**A word here is narrow.** The selector lets ``sw-north-*`` and ``10.0.0.0/16``
be single bare words, because a selector is nearly all values. A relational
query is nearly all *names*: types, properties, links, functions, bindings. It
also has arithmetic, and ``-`` cannot be both the minus sign and a character in
``sw-core-01``. So a word is ``[A-Za-z_][A-Za-z0-9_]*`` and nothing else, and a
value with punctuation in it is quoted::

    select switch filter .name = 'sw-core-01'

That is what SQL asks for, what EdgeQL asks for, and the only rule under which
``.length_m - 2`` and ``sw-core-01`` can both be read.

Comments run from ``#`` to the end of the line, because a query long enough to
need ``order by`` is long enough to be kept in a file and explained.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from netviz.query.errors import MAX_QUERY_LENGTH, QueryError

__all__ = ["KEYWORDS", "OPERATORS", "PUNCTUATION", "Token", "TokenKind", "tokenize"]


class TokenKind(str, Enum):
    """What a token is, coarsely. The parser refines it by looking at the text."""

    #: An identifier or a keyword: ``[A-Za-z_][A-Za-z0-9_]*``.
    WORD = "word"
    #: A quoted string. Distinguished from a word so that ``'select'`` is a value.
    STRING = "string"
    #: An integer or a decimal. Quoted digits are a :attr:`STRING`.
    NUMBER = "number"
    #: One of :data:`PUNCTUATION` or :data:`OPERATORS`.
    SYMBOL = "symbol"
    #: The zero-width token at the end, so every error has something to point at.
    END = "end"


@dataclass(frozen=True, slots=True)
class Token:
    """One token, and the span of the query it occupies."""

    kind: TokenKind
    #: The token verbatim; a string's text is its *content*, quotes removed.
    text: str
    #: 0-based offset of the first character in the query text.
    offset: int
    #: How many characters of the query the token occupies, quotes included.
    length: int

    @property
    def end(self) -> int:
        """0-based offset one past the token."""
        return self.offset + self.length

    @property
    def word(self) -> str:
        """The token folded for keyword comparison: lower case, or ``""``.

        A quoted string never folds to a keyword, which is what makes
        ``.name = 'filter'`` mean the name *filter* rather than a syntax error.
        """
        return self.text.lower() if self.kind is TokenKind.WORD else ""

    def is_symbol(self, *symbols: str) -> bool:
        """Is this one of the given punctuation marks or operators?"""
        return self.kind is TokenKind.SYMBOL and self.text in symbols

    def is_word(self, *words: str) -> bool:
        """Is this one of the given keywords, spelled in any case?"""
        return self.word in words if self.kind is TokenKind.WORD else False

    def describe(self) -> str:
        """The token as an error message names it."""
        if self.kind is TokenKind.END:
            return "the end of the query"
        if self.kind is TokenKind.STRING:
            return f"the string {self.text!r}"
        return repr(self.text)


#: Structural marks. Each is one character, so the scanner never backs up.
PUNCTUATION: Final[frozenset[str]] = frozenset("()[]{},.")

#: Operators, longest first — the scanner tries them in this order, so ``:=`` is
#: never read as ``:`` followed by ``=`` and ``++`` never as two ``+``.
OPERATORS: Final[tuple[str, ...]] = (
    ":=",
    "<=",
    ">=",
    "!=",
    "==",
    "=~",
    "!~",
    "++",
    "=",
    "~",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "%",
    ":",
)

#: Words the grammar reserves. Used for diagnostics and for completion; the
#: parser matches them positionally rather than consulting this set, because
#: ``name`` is both a property and, in ``order by``, a perfectly ordinary word.
KEYWORDS: Final[tuple[str, ...]] = (
    "and",
    "asc",
    "by",
    "desc",
    "distinct",
    "exists",
    "false",
    "filter",
    "ilike",
    "in",
    "is",
    "like",
    "limit",
    "none",
    "not",
    "offset",
    "or",
    "order",
    "select",
    "then",
    "true",
    "under",
    "where",
    "with",
)

#: What may start an identifier, and what may continue one.
_WORD_START: Final = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_WORD_BODY: Final = _WORD_START | frozenset("0123456789")


def tokenize(text: str, *, source: str = "query") -> tuple[Token, ...]:
    """Scan ``text`` into tokens, ending with exactly one :attr:`TokenKind.END`.

    Args:
        text: The query as written.
        source: What a diagnostic calls the origin, e.g. ``query`` or a file.

    Returns:
        Every token in order, terminated by an END token at the end of input.

    Raises:
        QueryError: The text is longer than
            :data:`~netviz.query.errors.MAX_QUERY_LENGTH`, holds a character no
            token may contain, or ends inside a quoted string.
    """
    if len(text) > MAX_QUERY_LENGTH:
        raise QueryError(
            f"the query is {len(text)} characters long, over the {MAX_QUERY_LENGTH}-character "
            "limit",
            text=text[:MAX_QUERY_LENGTH],
            offset=MAX_QUERY_LENGTH - 1,
            help="a query is an expression somebody types; this one is generated",
            source=source,
        )

    tokens: list[Token] = []
    at = 0
    size = len(text)
    while at < size:
        char = text[at]
        if char.isspace():
            at += 1
            continue
        if char == "#":
            newline = text.find("\n", at)
            at = size if newline == -1 else newline
            continue
        if char in _WORD_START:
            start = at
            while at < size and text[at] in _WORD_BODY:
                at += 1
            tokens.append(Token(TokenKind.WORD, text[start:at], start, at - start))
            continue
        if char.isdigit():
            token, at = _number_at(text, at)
            tokens.append(token)
            continue
        if char in "\"'":
            token, at = _string_at(text, at, source=source)
            tokens.append(token)
            continue
        # A '.' followed by a digit is the start of a decimal written without a
        # leading zero; anywhere else it is the path operator.
        if char == "." and at + 1 < size and text[at + 1].isdigit():
            token, at = _number_at(text, at)
            tokens.append(token)
            continue
        operator = _operator_at(text, at)
        if operator is not None:
            tokens.append(Token(TokenKind.SYMBOL, operator, at, len(operator)))
            at += len(operator)
            continue
        if char in PUNCTUATION:
            tokens.append(Token(TokenKind.SYMBOL, char, at, 1))
            at += 1
            continue
        raise QueryError(
            f"{char!r} is not part of any operator or name",
            text=text,
            offset=at,
            help="names are letters, digits and '_'; write a value with punctuation in quotes",
            source=source,
        )

    tokens.append(Token(TokenKind.END, "", size, 0))
    return tuple(tokens)


def _operator_at(text: str, at: int) -> str | None:
    """The operator starting at ``at``, longest match, or ``None``."""
    for operator in OPERATORS:
        if text.startswith(operator, at):
            return operator
    return None


def _number_at(text: str, at: int) -> tuple[Token, int]:
    """Scan the number starting at ``at``; return it and the next offset.

    A ``.`` is consumed only when a digit follows it, so ``2.mtu`` is the number
    2 and the path step ``mtu`` rather than a malformed decimal, and ``2.5`` is
    one token.
    """
    start = at
    size = len(text)
    while at < size and text[at].isdigit():
        at += 1
    if at + 1 < size and text[at] == "." and text[at + 1].isdigit():
        at += 1
        while at < size and text[at].isdigit():
            at += 1
    return Token(TokenKind.NUMBER, text[start:at], start, at - start), at


def _string_at(text: str, at: int, *, source: str) -> tuple[Token, int]:
    """Scan the quoted string starting at ``at``; return it and the next offset."""
    quote = text[at]
    parts: list[str] = []
    cursor = at + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\" and cursor + 1 < len(text) and text[cursor + 1] in ("\\", quote):
            parts.append(text[cursor + 1])
            cursor += 2
            continue
        if char == quote:
            return Token(TokenKind.STRING, "".join(parts), at, cursor + 1 - at), cursor + 1
        parts.append(char)
        cursor += 1
    raise QueryError(
        f"this {quote} is never closed",
        text=text,
        offset=at,
        length=len(text) - at,
        help=f"add a closing {quote}",
        source=source,
    )
