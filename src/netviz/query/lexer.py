"""Turning query text into tokens, each of which remembers where it came from.

The lexer is deliberately dull. There are five kinds of token — a word, a
number, a quoted string, a punctuation mark and end-of-input — and every one of
them carries its offset and its length, because the parser's whole job when
something is wrong is to underline the token it could not use
(:mod:`netviz.query.errors`).

Two decisions are worth naming.

**A word is generous.** ``sw-north-*``, ``sites/north``, ``10.20.0.0/16``,
``spec.interfaces``, ``label.role`` and ``GigabitEthernet1/0/1`` are all one
word. Network inventories are full of names with punctuation in them, and a
grammar that made the user quote every one of those would be a grammar nobody
writes queries in. The characters that *cannot* appear in a word are exactly the
ones the grammar needs: whitespace, parentheses, brackets, commas and the
comparison operators.

**A quoted string is an escape hatch, not a second syntax.** Single or double
quotes, backslash escapes for the quote and the backslash, no interpolation of
anything. It exists so that a value containing a space, a comma or a bracket can
still be written, and for nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from netviz.query.errors import MAX_QUERY_LENGTH, QueryError

__all__ = ["OPERATORS", "PUNCTUATION", "Token", "TokenKind", "tokenize"]


class TokenKind(str, Enum):
    """What a token is, coarsely. The parser refines it by looking at the text."""

    #: A bare run of word characters: a keyword, an attribute, or a value.
    WORD = "word"
    #: A quoted string. Distinguished from a word so that ``"and"`` is a value.
    STRING = "string"
    #: An unsigned integer. Quoted digits are a :attr:`STRING`, not a number.
    NUMBER = "number"
    #: One of :data:`PUNCTUATION` or :data:`OPERATORS`.
    SYMBOL = "symbol"
    #: The zero-width token at the end, so every error has something to point at.
    END = "end"


@dataclass(frozen=True, slots=True)
class Token:
    """One token, and the span of the query it occupies."""

    kind: TokenKind
    #: The token as the parser compares it: a word lower-cased for keywords is
    #: *not* done here — this is verbatim, because ``name = Sw-01`` is
    #: case-sensitive and ``AND`` is not.
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
        ``name = "and"`` mean the name *and* rather than a syntax error.
        """
        return self.text.lower() if self.kind is TokenKind.WORD else ""

    def is_symbol(self, *symbols: str) -> bool:
        """Is this one of the given punctuation marks?"""
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


#: Structural marks. Each is one character, so the scanner never has to back up.
PUNCTUATION: Final[frozenset[str]] = frozenset("()[],")

#: Comparison operators, longest first — the scanner tries them in this order,
#: so ``!=`` is never read as ``!`` followed by ``=``.
OPERATORS: Final[tuple[str, ...]] = ("<=", ">=", "!=", "==", "=~", "!~", "=", "~", "<", ">")

#: Characters that end a bare word. Everything else — letters, digits, ``-``,
#: ``_``, ``.``, ``/``, ``:``, ``*``, ``?``, ``@``, ``%`` — is part of one.
_BREAKS: Final[frozenset[str]] = PUNCTUATION | frozenset("<>=~!\"'") | frozenset(" \t\r\n\f\v")


def tokenize(text: str, *, source: str = "query") -> tuple[Token, ...]:
    """Scan ``text`` into tokens, ending with exactly one :attr:`TokenKind.END`.

    Args:
        text: The query as written.
        source: What a diagnostic calls the origin, e.g. ``query`` or a file.

    Returns:
        Every token in order, terminated by an END token at the end of input.

    Raises:
        QueryError: The text is longer than :data:`MAX_QUERY_LENGTH`, holds a
            character no token may contain, or ends inside a quoted string.
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
        if char in PUNCTUATION:
            tokens.append(Token(TokenKind.SYMBOL, char, at, 1))
            at += 1
            continue
        operator = _operator_at(text, at)
        if operator is not None:
            tokens.append(Token(TokenKind.SYMBOL, operator, at, len(operator)))
            at += len(operator)
            continue
        if char in "\"'":
            token, at = _string_at(text, at, source=source)
            tokens.append(token)
            continue
        start = at
        while at < size and text[at] not in _BREAKS:
            at += 1
        if at == start:
            # Only reachable for a character that is a break but not punctuation,
            # an operator or a quote -- i.e. '!' on its own.
            raise QueryError(
                f"{text[start]!r} is not part of any query operator",
                text=text,
                offset=start,
                help="write '!=' or '!~', or 'not' for negation",
                source=source,
            )
        word = text[start:at]
        kind = TokenKind.NUMBER if word.isdigit() else TokenKind.WORD
        tokens.append(Token(kind, word, start, at - start))

    tokens.append(Token(TokenKind.END, "", size, 0))
    return tuple(tokens)


def _operator_at(text: str, at: int) -> str | None:
    """The comparison operator starting at ``at``, longest match, or ``None``."""
    for operator in OPERATORS:
        if text.startswith(operator, at):
            return operator
    return None


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
            return (
                Token(TokenKind.STRING, "".join(parts), at, cursor + 1 - at),
                cursor + 1,
            )
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
