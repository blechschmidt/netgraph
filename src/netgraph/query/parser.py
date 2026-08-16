"""Recursive descent over :mod:`netgraph.query.lexer`'s tokens.

The grammar, in full — this is the normative statement of it, and
``docs/query.md`` is its prose::

    query      := or
    or         := and ( "or" and )*
    and        := unary ( "and" unary )*
    unary      := "not" unary | primary
    primary    := "(" query ")"
                | "neighbors" "of" unary
                | "within" NUMBER "hops" "of" unary
                | "reachable" "from" unary
                | DOMAIN "[" query "]"
                | "has" attribute
                | attribute operator value
                | attribute "in" "(" value ( "," value )* ")"
                | "*"
                | word                       -- sugar for: name ~ word

    operator   := "=" | "==" | "!=" | "~" | "!~" | "=~"
                | "<" | "<=" | ">" | ">=" | "in" | "under"

``and`` binds tighter than ``or``, ``not`` tighter than both, and a traversal
tighter still — so ``within 2 hops of fw-edge and kind = switch`` is *(the
neighbourhood) and (the switches)* rather than a neighbourhood of switches.
That is the reading every operator expects, and the one that makes the
parenthesised alternative worth writing when it is not what was meant.

Everything the parser refuses, it refuses with a span. There is no message here
that does not name the token it is about, because a message that says only
"syntax error" is a message that sends the reader back to the grammar to guess.

Bounds
------

Two, both of them there so that a generated or fuzzed query is refused rather
than survived: :data:`MAX_DEPTH` on how deep the tree may nest, checked as it is
built rather than after, and :data:`MAX_TERMS` on how many leaves it may have.
Neither is reachable by hand.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from dataclasses import dataclass
from typing import Final

from netgraph.errors import echo_value
from netgraph.models.scalars import MAX_VLAN_ID, MIN_VLAN_ID
from netgraph.query.ast import (
    All,
    And,
    Comparison,
    Exists,
    Expr,
    Not,
    Operator,
    Or,
    Scope,
    Span,
    Traversal,
    TraversalKind,
)
from netgraph.query.attributes import (
    Attribute,
    Domain,
    ValueType,
    attribute_names,
    lookup,
    suggestions,
)
from netgraph.query.errors import QueryError
from netgraph.query.lexer import Token, TokenKind, tokenize

__all__ = ["MAX_DEPTH", "MAX_HOPS", "MAX_TERMS", "Query", "parse"]

#: How deeply a query may nest. Every level is a parenthesis, a ``not``, a scope
#: or a traversal somebody wrote; thirty-two of them is far past readable and
#: comfortably inside Python's own recursion limit.
MAX_DEPTH: Final = 32

#: How many leaf terms one query may hold. A comparison, an existence test and
#: an ``*`` each count as one.
MAX_TERMS: Final = 256

#: The largest ``within N hops of``. Past this it is ``reachable from``, which
#: is the same answer computed once instead of N times.
MAX_HOPS: Final = 64

#: Longest regular expression ``=~`` accepts. A pattern is compiled and then run
#: once per node, so an enormous one is a denial of service dressed as a filter.
MAX_PATTERN: Final = 512

#: Keywords that may not be read as a bare-word name shorthand, because doing so
#: would silently turn a mistyped operator into a filter that matches nothing.
_RESERVED: Final[frozenset[str]] = frozenset(
    {"and", "or", "not", "of", "hops", "from", "has", "in", "under", "neighbors", "neighbours"}
)

#: Operators spelled as punctuation, and the enum member each denotes. ``==`` is
#: accepted for ``=`` because every programmer types it once.
_SYMBOLIC: Final[dict[str, Operator]] = {
    "=": Operator.EQ,
    "==": Operator.EQ,
    "!=": Operator.NE,
    "~": Operator.GLOB,
    "!~": Operator.NOT_GLOB,
    "=~": Operator.REGEX,
    "<": Operator.LT,
    "<=": Operator.LE,
    ">": Operator.GT,
    ">=": Operator.GE,
}

#: What ``true`` and ``false`` may be written as, for a BOOL attribute.
_BOOLEANS: Final[dict[str, bool]] = {
    "true": True,
    "yes": True,
    "1": True,
    "false": False,
    "no": False,
    "0": False,
}


@dataclass(frozen=True, slots=True)
class Query:
    """A parsed, checked query, and the text it was parsed from.

    The text is kept because every diagnostic raised later — an unknown element
    at the centre of a traversal, a graph that holds no such VLAN — underlines a
    span of it, and because ``netgraph query --json`` echoes the query it
    answered so a saved result says what produced it.
    """

    text: str
    expr: Expr
    #: What a diagnostic calls the origin: ``query``, ``--select`` or a document.
    source: str = "query"

    def __str__(self) -> str:
        return self.text


def parse(text: str, *, source: str = "query") -> Query:
    """Parse ``text`` into a checked :class:`Query`.

    Every name is resolved against :mod:`netgraph.query.attributes` and every
    value coerced to its attribute's type *here*, so a query that parses is a
    query that can be evaluated against any graph without raising: the only
    error evaluation can still produce is one about the inventory, such as a
    traversal centred on an element that does not exist.

    Args:
        text: The query as written.
        source: What a diagnostic names as the origin.

    Raises:
        QueryError: The text is not a query, names something no domain has, or
            carries a value the attribute cannot hold. The error underlines the
            span responsible.
    """
    parser = _Parser(text, source=source)
    expr = parser.parse()
    return Query(text=text, expr=expr, source=source)


class _Parser:
    """One parse. Holds the token stream, the cursor and the two budgets."""

    def __init__(self, text: str, *, source: str) -> None:
        self.text = text
        self.source = source
        self.tokens = tokenize(text, source=source)
        self.at = 0
        self.terms = 0

    # ------------------------------------------------------------ the stream

    @property
    def token(self) -> Token:
        return self.tokens[self.at]

    def take(self) -> Token:
        token = self.tokens[self.at]
        if token.kind is not TokenKind.END:
            self.at += 1
        return token

    def fail(
        self, message: str, token: Token | None = None, *, help: str | None = None
    ) -> QueryError:
        """A diagnostic underlining ``token``, defaulting to the current one."""
        where = token if token is not None else self.token
        return QueryError(
            message,
            text=self.text,
            offset=where.offset,
            length=max(1, where.length),
            help=help,
            source=self.source,
        )

    # ------------------------------------------------------------- the rules

    def parse(self) -> Expr:
        if self.token.kind is TokenKind.END:
            # Pointed at the start rather than at the END token: a query of
            # nothing but spaces has no offending character, and underlining the
            # position after the last space would ask the reader to look at
            # whitespace.
            raise QueryError(
                "the query is empty",
                text=self.text,
                offset=0,
                help="write '*' for everything, or a term such as 'kind = switch'",
                source=self.source,
            )
        expr = self.parse_or(depth=0)
        # Read into a local: `self.token` is a property over a cursor the parse
        # above has moved, and reusing the expression would let a reader (or a
        # type checker) carry the narrowing from the check at the top of this
        # method past the call that invalidated it.
        trailing = self.token
        if trailing.kind is not TokenKind.END:
            raise self.fail(
                f"{trailing.describe()} is not expected here",
                help="join two terms with 'and' or 'or'",
            )
        return expr

    def parse_or(self, *, depth: int) -> Expr:
        self.guard(depth)
        first = self.parse_and(depth=depth)
        if not self.token.is_word("or"):
            return first
        operands = [first]
        while self.token.is_word("or"):
            self.take()
            operands.append(self.parse_and(depth=depth))
        return Or(tuple(operands), _cover(operands))

    def parse_and(self, *, depth: int) -> Expr:
        first = self.parse_unary(depth=depth)
        if not self.token.is_word("and"):
            return first
        operands = [first]
        while self.token.is_word("and"):
            self.take()
            operands.append(self.parse_unary(depth=depth))
        return And(tuple(operands), _cover(operands))

    def parse_unary(self, *, depth: int) -> Expr:
        self.guard(depth)
        if self.token.is_word("not"):
            keyword = self.take()
            body = self.parse_unary(depth=depth + 1)
            return Not(body, Span.between(_span(keyword), _of(body)))
        return self.parse_primary(depth=depth)

    def parse_primary(self, *, depth: int) -> Expr:
        self.guard(depth)
        token = self.token
        if token.is_symbol("("):
            self.take()
            inner = self.parse_or(depth=depth + 1)
            close = self.token
            if not close.is_symbol(")"):
                raise self.fail(
                    f"expected ')' but found {close.describe()}",
                    help="every '(' needs a ')'",
                )
            self.take()
            return inner
        if token.kind is TokenKind.WORD:
            if token.is_word("neighbors", "neighbours"):
                return self.parse_neighbors(depth=depth)
            if token.is_word("within"):
                return self.parse_within(depth=depth)
            if token.is_word("reachable"):
                return self.parse_reachable(depth=depth)
            if token.is_word("has"):
                return self.parse_exists()
            if token.word in _RESERVED:
                raise self.fail(
                    f"{token.describe()} is a keyword, not a term",
                    help="quote it to use it as a name, or write a term such as 'kind = switch'",
                )
            if self.tokens[self.at + 1].is_symbol("["):
                return self.parse_scope(depth=depth)
        if token.is_symbol(")", "]", ","):
            raise self.fail(f"{token.describe()} has nothing to close")
        if token.kind is TokenKind.END:
            raise self.fail("the query ends after an operator", help="write a term after it")
        if token.kind is TokenKind.SYMBOL:
            # Everything a symbol could legally start has been tried above, so
            # what is left is punctuation where a term belongs. Reaching
            # parse_term with one would read it as a *name*, and `[` would then
            # be a perfectly good query for elements with a bracket in the name.
            raise self.fail(
                f"{token.describe()} is not the start of a term",
                help="write an attribute, a name, or '(' to group",
            )
        return self.parse_term()

    # -------------------------------------------------------- the traversals

    def parse_neighbors(self, *, depth: int) -> Expr:
        keyword = self.take()
        self.expect_word("of", after=keyword)
        body = self.parse_unary(depth=depth + 1)
        return Traversal(
            TraversalKind.NEIGHBORS, body, hops=1, span=Span.between(_span(keyword), _of(body))
        )

    def parse_within(self, *, depth: int) -> Expr:
        keyword = self.take()
        number = self.token
        if number.kind is not TokenKind.NUMBER:
            raise self.fail(
                f"expected a number of hops but found {number.describe()}",
                help="write 'within 2 hops of …'",
            )
        self.take()
        hops = int(number.text)
        if hops > MAX_HOPS:
            raise self.fail(
                f"{hops} hops is over the limit of {MAX_HOPS}",
                number,
                help="write 'reachable from …' for the whole connected component",
            )
        self.expect_word("hops", after=number, plural_of="hop")
        self.expect_word("of", after=keyword)
        body = self.parse_unary(depth=depth + 1)
        return Traversal(
            TraversalKind.WITHIN, body, hops=hops, span=Span.between(_span(keyword), _of(body))
        )

    def parse_reachable(self, *, depth: int) -> Expr:
        keyword = self.take()
        self.expect_word("from", after=keyword)
        body = self.parse_unary(depth=depth + 1)
        return Traversal(
            TraversalKind.REACHABLE, body, hops=None, span=Span.between(_span(keyword), _of(body))
        )

    def expect_word(self, word: str, *, after: Token, plural_of: str | None = None) -> Token:
        """Consume the keyword ``word``, or say what was written instead."""
        if self.token.is_word(word) or (plural_of and self.token.is_word(plural_of)):
            return self.take()
        raise self.fail(
            f"expected {word!r} after {after.describe()} but found {self.token.describe()}"
        )

    # ------------------------------------------------------------- the scope

    def parse_scope(self, *, depth: int) -> Expr:
        name = self.take()
        try:
            domain = Domain(name.word)
        except ValueError:
            raise self.fail(
                f"{name.describe()} is not something a query can look inside",
                name,
                help="the scopes are interface[…], link[…], netns[…] and zone[…]",
            ) from None
        if domain is Domain.ELEMENT:
            raise self.fail(
                "'element[…]' is the query itself",
                name,
                help="write the terms without the scope",
            )
        self.take()  # the '['
        if self.token.is_symbol("]"):
            raise self.fail(
                f"'{domain}[]' asks nothing",
                help=f"write 'has {domain}' for 'it has at least one'",
            )
        inner = _Domained(self, domain).parse_or(depth=depth + 1)
        close = self.token
        if not close.is_symbol("]"):
            raise self.fail(f"expected ']' but found {close.describe()}")
        self.take()
        return Scope(domain.value, inner, Span.between(_span(name), _span(close)), _span(name))

    # -------------------------------------------------------------- the terms

    @property
    def domain(self) -> Domain:
        """Which vocabulary a bare attribute is looked up in."""
        return Domain.ELEMENT

    def parse_exists(self) -> Expr:
        keyword = self.take()
        name = self.token
        if name.kind not in (TokenKind.WORD, TokenKind.STRING):
            raise self.fail(
                f"expected an attribute after 'has' but found {name.describe()}",
                help=f"for example 'has address' or 'has {self.domain}'",
            )
        self.take()
        self.resolve(name)
        self.spend(name)
        return Exists(name.text, Span.between(_span(keyword), _span(name)), _span(name))

    def parse_term(self) -> Expr:
        name = self.take()
        if name.kind is TokenKind.WORD and name.text == "*":
            self.spend(name)
            return All(_span(name))
        operator_token = self.token
        operator = self.operator_at(operator_token)
        if operator is None:
            # No operator follows, so this is the bare-word shorthand: a glob
            # over the name. It is what somebody types before reading any of
            # this, and it is what the editor's old substring search was.
            if name.kind is TokenKind.NUMBER:
                raise self.fail(
                    f"{name.describe()} on its own is not a term",
                    name,
                    help="write 'vlan = 10' or quote it to match a name",
                )
            self.spend(name)
            return Comparison(
                "name",
                Operator.GLOB,
                (_glob(name.text),),
                _span(name),
                _span(name),
                _span(name),
            )
        self.take()
        attribute, qualifier = self.resolve(name)
        if operator is Operator.IN and self.token.is_symbol("("):
            values, span = self.parse_alternatives(name, attribute.type)
        else:
            value_token = self.take()
            if value_token.kind is TokenKind.END:
                raise self.fail(
                    f"'{name.text} {operator_token.text}' has no value after it",
                    operator_token,
                    help="write what it should be compared with",
                )
            values = (self.coerce(name, attribute.type, operator, value_token),)
            span = _span(value_token)
        self.check_operator(name, attribute, operator, operator_token)
        self.spend(name)
        # The name is carried *as written*, qualifier and alias included, and
        # resolved again by the evaluator. Canonicalising here would make an
        # error message from evaluation name an attribute the reader never
        # typed, and it would put a second copy of the alias table in this file.
        del qualifier
        return Comparison(
            name.text,
            operator,
            values,
            Span.between(_span(name), span),
            _span(name),
            span,
        )

    def parse_alternatives(self, name: Token, kind: ValueType) -> tuple[tuple[str, ...], Span]:
        """``in (a, b, c)`` — one or more values, each coerced like a single one."""
        opened = self.take()
        values: list[str] = []
        while True:
            token = self.take()
            if token.kind is TokenKind.END or token.is_symbol("]"):
                raise self.fail("expected ')' to close the list of alternatives", opened)
            if token.is_symbol(")"):
                if values:
                    return tuple(values), Span.between(_span(opened), _span(token))
                raise self.fail(
                    f"'{name.text} in ()' asks for nothing", opened, help="list at least one value"
                )
            if token.is_symbol(","):
                raise self.fail("expected a value but found ','", token)
            values.append(self.coerce(name, kind, Operator.IN, token))
            if self.token.is_symbol(","):
                self.take()

    # ------------------------------------------------------ names and values

    def resolve(self, name: Token) -> tuple[Attribute, str]:
        """Look ``name`` up in this parser's domain, or say what is near it."""
        found = lookup(self.domain, name.text)
        if found is None:
            near = suggestions(self.domain, name.text)
            hint = (
                f"did you mean {', '.join(repr(one) for one in near)}?"
                if near
                else f"the {self.domain} attributes are "
                f"{', '.join(attribute_names(self.domain)[:8])} — see docs/query.md"
            )
            raise self.fail(
                f"{echo_value(name.text)} is not an attribute of {self.domain}",
                name,
                help=hint,
            )
        return found

    def check_operator(
        self, name: Token, attribute: Attribute, operator: Operator, token: Token
    ) -> None:
        """Refuse an operator the attribute's type has no meaning for."""
        ordering = (Operator.LT, Operator.LE, Operator.GT, Operator.GE)
        if operator in ordering and not attribute.orders:
            raise self.fail(
                f"{name.text!r} is {attribute.type} and does not order",
                token,
                help="'<' and '>' apply to a number or a VLAN id",
            )
        if operator is Operator.UNDER and attribute.type is not ValueType.PATH:
            raise self.fail(
                f"'under' applies to a path, and {name.text!r} is {attribute.type}",
                token,
                help="'~' matches a glob against any text",
            )
        if attribute.type is ValueType.BOOL and operator not in (Operator.EQ, Operator.NE):
            raise self.fail(
                f"{name.text!r} is true or false, so {operator.value!r} says nothing about it",
                token,
                help=f"write '{name.text} = true' or '{name.text} = false'",
            )

    def coerce(self, name: Token, kind: ValueType, operator: Operator, token: Token) -> str:
        """Check ``token`` against the attribute's type; return it as text.

        Values are carried through the tree as text and compared as text, with
        the *comparison* doing whatever the type needs — a VLAN as an int, an
        address as a network. Coercion here is therefore a check rather than a
        conversion, and it exists so that ``vlan = 5000`` is refused when the
        query is written rather than silently matching nothing forever after.
        """
        value = token.text
        if token.kind is TokenKind.END:
            raise self.fail(f"'{name.text}' has no value after it", help="write one")
        if kind is ValueType.VLAN and operator in (Operator.EQ, Operator.NE, Operator.IN):
            self.check_vlan(value, token)
        elif kind is ValueType.NUMBER and operator not in (
            Operator.GLOB,
            Operator.NOT_GLOB,
            Operator.REGEX,
        ):
            self.check_number(name, value, token)
        elif kind is ValueType.ADDRESS and operator is Operator.IN:
            self.check_network(value, token)
        elif kind is ValueType.BOOL:
            if value.lower() not in _BOOLEANS:
                raise self.fail(
                    f"{echo_value(value)} is not true or false",
                    token,
                    help="write 'true' or 'false'",
                )
            value = str(_BOOLEANS[value.lower()]).lower()
        if operator is Operator.REGEX:
            self.check_regex(value, token)
        if operator in (Operator.GLOB, Operator.NOT_GLOB):
            self.check_glob(value, token)
        return value

    def check_vlan(self, value: str, token: Token) -> None:
        if not value.isdigit():
            raise self.fail(
                f"{echo_value(value)} is not a VLAN id",
                token,
                help=f"a VLAN id is a number from {MIN_VLAN_ID} to {MAX_VLAN_ID}",
            )
        number = int(value)
        if not MIN_VLAN_ID <= number <= MAX_VLAN_ID:
            raise self.fail(
                f"VLAN {number} is outside {MIN_VLAN_ID}-{MAX_VLAN_ID}",
                token,
            )

    def check_number(self, name: Token, value: str, token: Token) -> None:
        try:
            int(value)
        except ValueError:
            raise self.fail(
                f"{echo_value(value)} is not a number, and {name.text!r} is one",
                token,
            ) from None

    def check_network(self, value: str, token: Token) -> None:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise self.fail(
                f"{echo_value(value)} is not an IP address or prefix",
                token,
                help="write something like '10.20.0.0/16' or '2001:db8::/32'",
            ) from None

    def check_regex(self, value: str, token: Token) -> None:
        if len(value) > MAX_PATTERN:
            raise self.fail(
                f"the pattern is {len(value)} characters long, over the {MAX_PATTERN} limit",
                token,
            )
        try:
            re.compile(value)
        except re.error as exc:
            raise self.fail(f"the pattern is not a regular expression: {exc}", token) from None

    def check_glob(self, value: str, token: Token) -> None:
        try:
            fnmatch.translate(value)
        except re.error as exc:  # pragma: no cover - fnmatch escapes everything
            raise self.fail(f"the glob cannot be compiled: {exc}", token) from None

    def operator_at(self, token: Token) -> Operator | None:
        """The operator ``token`` is, or ``None`` when it is not one."""
        if token.kind is TokenKind.SYMBOL:
            return _SYMBOLIC.get(token.text)
        if token.is_word("in"):
            return Operator.IN
        if token.is_word("under"):
            return Operator.UNDER
        return None

    # ------------------------------------------------------------- the budgets

    def guard(self, depth: int) -> None:
        if depth >= MAX_DEPTH:
            raise self.fail(
                f"the query nests more than {MAX_DEPTH} levels deep",
                help="a query this deep is generated; split it into several",
            )

    def spend(self, token: Token) -> None:
        self.terms += 1
        if self.terms > MAX_TERMS:
            raise self.fail(
                f"the query has more than {MAX_TERMS} terms",
                token,
                help="a query this long is generated; split it into several",
            )


class _Domained(_Parser):
    """The same parser, resolving bare attributes in a scope's domain.

    A subclass rather than a parameter because the domain is fixed for the whole
    of a scope's body and every rule below :meth:`parse_term` has to see it; the
    token stream, the cursor and the budgets are *shared* with the outer parser
    by construction, so a scope's terms count against the same limits and the
    outer parser resumes exactly where this one stopped.
    """

    def __init__(self, outer: _Parser, domain: Domain) -> None:
        self.text = outer.text
        self.source = outer.source
        self.tokens = outer.tokens
        self._outer = outer
        self._domain = domain

    @property
    def domain(self) -> Domain:
        return self._domain

    # The cursor and the term count live on the outermost parser, so that
    # `self.at` means the same thing on both sides of a scope's brackets.
    @property
    def at(self) -> int:
        return self._outer.at

    @at.setter
    def at(self, value: int) -> None:
        self._outer.at = value

    @property
    def terms(self) -> int:
        return self._outer.terms

    @terms.setter
    def terms(self, value: int) -> None:
        self._outer.terms = value

    def parse_scope(self, *, depth: int) -> Expr:
        raise self.fail(
            "a scope cannot be written inside another scope",
            help="an interface has no interfaces; write the terms side by side",
        )

    def parse_neighbors(self, *, depth: int) -> Expr:
        raise self._no_traversal()

    def parse_within(self, *, depth: int) -> Expr:
        raise self._no_traversal()

    def parse_reachable(self, *, depth: int) -> Expr:
        raise self._no_traversal()

    def _no_traversal(self) -> QueryError:
        return self.fail(
            f"a traversal cannot be written inside {self.domain}[…]",
            help="the graph is walked between elements; move the traversal outside the brackets",
        )


def _span(token: Token) -> Span:
    return Span(token.offset, max(1, token.length))


def _of(expr: Expr) -> Span:
    return expr.span


def _cover(operands: list[Expr]) -> Span:
    return Span.between(operands[0].span, operands[-1].span)


def _glob(text: str) -> str:
    """The bare-word shorthand's glob: a word with no wildcard means "contains".

    ``sw-core`` finds ``sw-core-01``, because that is what somebody typing a name
    into a search box means, and ``sw-*`` is left exactly as written, because
    somebody who typed a wildcard means the wildcard.
    """
    return text if any(char in text for char in "*?[") else f"*{text}*"
