"""Reading a relational query, and checking it against the schema as it reads.

The parser is the type checker. Every name is resolved the moment it is read —
a type against :data:`~netviz.nql.schema.SCHEMA`, a member against the type it
was written on, a function against :data:`~netviz.nql.functions.FUNCTIONS` — so
a query that parses is a query that can run, and one that cannot is refused
before any file is opened:

    query:1:19: 'mak' is not a member of interface
      select interface { mak }
                         ^^^
      help: did you mean 'mac'?

Doing it in one pass rather than in a checker afterwards is what lets the
diagnostic name the *scope* as well as the member. ``.name`` means something
different inside ``interface { … }`` than it does inside ``filter``, and the
parser is the only place that knows which one it is currently in.

Grammar
-------

::

    query        := ("with" binding ("," binding)* )? expr
    binding      := NAME ":=" expr
    expr         := or
    or           := and ("or" and)*
    and          := unary ("and" unary)*
    unary        := ("not" | "exists" | "distinct") unary | comparison
    comparison   := additive (compare_op additive)?
                  | additive "is" "not"? TYPE
    compare_op   := "=" | "==" | "!=" | "<" | "<=" | ">" | ">="
                  | "in" | "not" "in" | "like" | "ilike" | "~" | "!~" | "=~" | "under"
    additive     := multiplicative (("+" | "-" | "++") multiplicative)*
    multiplicative := prefix (("*" | "/" | "%") prefix)*
    prefix       := "-" prefix | postfix
    postfix      := primary ("." NAME | "[" "is" TYPE "]")*
    primary      := NUMBER | STRING | "true" | "false" | "none"
                  | "(" expr ")"
                  | "{" expr ("," expr)* "}"          -- set
                  | "{" NAME ":=" expr ("," …)* "}"   -- free object
                  | "." NAME                          -- path from the current scope
                  | NAME "(" args ")"                 -- function call
                  | NAME                              -- type root, or a `with` binding
                  | select
    select       := "select" expr shape? clause*
    clause       := ("filter" | "where") expr
                  | "order" "by" order ((","|"then") order)*
                  | "limit" expr | "offset" expr
    order        := expr ("asc" | "desc")?
    shape        := "{" element ("," element)* "}"
    element      := "*" | NAME | NAME ":" shape clause* | NAME ":=" expr shape? clause*

Precedence runs loosest to tightest down that list, so ``a or b and c`` is
``a or (b and c)`` and ``.mtu + 8 > 1500`` compares the sum.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Final, NoReturn

from netviz.nql.ast import (
    Binary,
    BinaryOp,
    Call,
    Expr,
    FreeObject,
    IsType,
    Literal,
    OrderItem,
    Query,
    Root,
    Select,
    SetLiteral,
    Shape,
    ShapeElement,
    Span,
    Step,
    This,
    TypeFilter,
    Unary,
    UnaryOp,
    Var,
)
from netviz.nql.functions import FUNCTIONS
from netviz.nql.lexer import Token, TokenKind, tokenize
from netviz.nql.schema import SCHEMA
from netviz.nql.types import Cardinality, Link, ScalarKind, Schema, ValueType
from netviz.query.errors import QueryError

__all__ = ["MAX_DEPTH", "MAX_NODES", "is_relational", "parse"]

#: How deeply a query may nest. A hand-written one reaches four or five.
MAX_DEPTH: Final = 48

#: How many nodes a query may grow to. Past this it is generated, and the
#: honest answer is to refuse it rather than to spend the memory. Below
#: :data:`~netviz.query.errors.MAX_QUERY_LENGTH` divided by the shortest term
#: anybody can write, so the limit is one a query can actually reach.
MAX_NODES: Final = 1024

#: Longest regular expression ``=~`` accepts.
MAX_PATTERN: Final = 512

#: The words that make a query relational rather than a selector expression.
_OPENERS: Final = ("select", "with")

_BOOL: Final = ValueType(scalar=ScalarKind.BOOL, card=Cardinality.ONE)

#: Comparison operators written as symbols, and the node they build.
_SYMBOL_COMPARISONS: Final[dict[str, BinaryOp]] = {
    "=": BinaryOp.EQ,
    "==": BinaryOp.EQ,
    "!=": BinaryOp.NE,
    "<": BinaryOp.LT,
    "<=": BinaryOp.LE,
    ">": BinaryOp.GT,
    ">=": BinaryOp.GE,
    "~": BinaryOp.LIKE,
    "!~": BinaryOp.NOT_LIKE,
    "=~": BinaryOp.REGEX,
}

#: And the ones written as words.
_WORD_COMPARISONS: Final[dict[str, BinaryOp]] = {
    "in": BinaryOp.IN,
    "like": BinaryOp.LIKE,
    "ilike": BinaryOp.ILIKE,
    "under": BinaryOp.UNDER,
}

_ADDITIVE: Final[dict[str, BinaryOp]] = {
    "+": BinaryOp.ADD,
    "-": BinaryOp.SUB,
    "++": BinaryOp.CONCAT,
}

_MULTIPLICATIVE: Final[dict[str, BinaryOp]] = {
    "*": BinaryOp.MUL,
    "/": BinaryOp.DIV,
    "%": BinaryOp.MOD,
}


def is_relational(text: str) -> bool:
    """Does ``text`` open a relational query rather than a selector expression?

    One token of lookahead and nothing cleverer. ``select`` and ``with`` are the
    only two words a relational query may begin with, and neither is a legal
    start to a selector — a selector beginning with a bare word is a name
    search, and nobody searches for a device called ``select`` without quoting
    it, which is exactly what the quotes are for.
    """
    word = re.match(r"[A-Za-z_]+", text.lstrip())
    return word is not None and word.group(0).lower() in _OPENERS


def parse(text: str, *, source: str = "query", schema: Schema = SCHEMA) -> Query:
    """Read ``text`` into a checked query.

    Raises:
        QueryError: The text does not parse, names something the schema does not
            have, or asks for an operation the types do not support. The error
            underlines the span it is about.
    """
    return _Parser(text, source=source, schema=schema).run()


class _Parser:
    """Recursive descent, one token of lookahead, checking as it goes."""

    def __init__(self, text: str, *, source: str, schema: Schema) -> None:
        self.text = text
        self.source = source
        self.schema = schema
        self.tokens = tokenize(text, source=source)
        self.at = 0
        self.depth = 0
        self.nodes = 0
        #: Object types a leading ``.`` may navigate from, innermost last.
        self.scope: list[str] = []
        #: ``with`` bindings in scope, and what each evaluates to.
        self.bindings: dict[str, ValueType] = {}

    # ------------------------------------------------------------------ #
    # Token handling
    # ------------------------------------------------------------------ #

    @property
    def token(self) -> Token:
        return self.tokens[self.at]

    def peek(self, ahead: int) -> Token:
        """The token ``ahead`` positions on, or the END token past the tail."""
        return self.tokens[min(self.at + ahead, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.tokens[self.at]
        if token.kind is not TokenKind.END:
            self.at += 1
        return token

    def accept_symbol(self, *symbols: str) -> Token | None:
        return self.advance() if self.token.is_symbol(*symbols) else None

    def accept_word(self, *words: str) -> Token | None:
        return self.advance() if self.token.is_word(*words) else None

    def expect_symbol(self, symbol: str, *, why: str) -> Token:
        if not self.token.is_symbol(symbol):
            self.fail(f"expected {symbol!r} {why}, found {self.token.describe()}")
        return self.advance()

    def expect_name(self, *, what: str) -> Token:
        if self.token.kind is not TokenKind.WORD:
            self.fail(f"expected {what}, found {self.token.describe()}")
        return self.advance()

    def fail(
        self, message: str, *, token: Token | None = None, help: str | None = None
    ) -> NoReturn:
        at = self.token if token is None else token
        raise QueryError(
            message,
            text=self.text,
            offset=at.offset,
            length=max(1, at.length),
            help=help,
            source=self.source,
        )

    def fail_at(self, message: str, span: Span, *, help: str | None = None) -> NoReturn:
        raise QueryError(
            message,
            text=self.text,
            offset=span.offset,
            length=span.length,
            help=help,
            source=self.source,
        )

    def count(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            self.fail(f"the query has more than {MAX_NODES} terms")

    def deeper(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            self.fail(f"the query nests more than {MAX_DEPTH} deep")

    @staticmethod
    def span_of(token: Token) -> Span:
        return Span(token.offset, max(1, token.length))

    def span_to_here(self, start: Token) -> Span:
        end = self.tokens[max(0, self.at - 1)]
        return Span(start.offset, max(1, end.end - start.offset))

    # ------------------------------------------------------------------ #
    # Entry
    # ------------------------------------------------------------------ #

    def run(self) -> Query:
        bindings = self.parse_bindings()
        body = self.parse_statement()
        if self.token.kind is not TokenKind.END:
            self.fail(
                f"expected the end of the query, found {self.token.describe()}",
                help="a query is one expression; put a second one in its own command",
            )
        return Query(text=self.text, bindings=bindings, body=body, source=self.source)

    def parse_bindings(self) -> tuple[tuple[str, Expr], ...]:
        if self.accept_word("with") is None:
            return ()
        found: list[tuple[str, Expr]] = []
        while True:
            name = self.expect_name(what="a name for the binding")
            if name.text in self.bindings:
                self.fail(f"{name.text!r} is already bound", token=name)
            self.expect_symbol(":=", why="after the binding's name")
            value = self.parse_statement()
            self.bindings[name.text] = value.type
            found.append((name.text, value))
            if self.accept_symbol(",") is None:
                break
        return tuple(found)

    # ------------------------------------------------------------------ #
    # Expressions
    # ------------------------------------------------------------------ #

    def parse_statement(self) -> Expr:
        """An expression, with ``filter``/``order by``/``limit`` allowed after it.

        ``select`` is therefore optional wherever a whole set is expected — a
        parenthesised group, a function argument, a ``with`` binding, the query
        itself — so ``count(interface filter exists .addresses)`` reads the way
        it is meant rather than needing a nested ``select``. Inside a shape
        element the clauses belong to the *field*, which is why that one path
        calls :meth:`parse_expr` instead.
        """
        source = self.parse_expr()
        if not self.token.is_word("filter", "where", "order", "limit", "offset"):
            return source
        filters, order, limit, offset = self.parse_clauses(_path_subject(source).type)
        return self.select_over(source, None, filters, order, limit, offset, source.span)

    def parse_expr(self) -> Expr:
        self.deeper()
        try:
            return self.parse_or()
        finally:
            self.depth -= 1

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.token.is_word("or"):
            operator = self.advance()
            right = self.parse_and()
            left = self.logical(BinaryOp.OR, left, right, operator)
        return left

    def parse_and(self) -> Expr:
        left = self.parse_unary()
        while self.token.is_word("and"):
            operator = self.advance()
            right = self.parse_unary()
            left = self.logical(BinaryOp.AND, left, right, operator)
        return left

    def logical(self, op: BinaryOp, left: Expr, right: Expr, operator: Token) -> Expr:
        self.count()
        for side, name in ((left, "left"), (right, "right")):
            if side.type.is_object:
                self.fail_at(
                    f"the {name} side of {op} is {side.type.object_type} objects, not a condition",
                    side.span,
                    help="write 'exists' in front of it to ask whether there are any",
                )
        card = left.type.card.then(right.type.card)
        return Binary(
            op,
            left,
            right,
            ValueType(scalar=ScalarKind.BOOL, card=card),
            Span.between(left.span, right.span),
            self.span_of(operator),
        )

    def parse_unary(self) -> Expr:
        start = self.token
        if start.is_word("not"):
            self.advance()
            operand = self.parse_unary()
            self.count()
            if operand.type.is_object:
                self.fail_at(
                    f"'not' needs a condition, and this is {operand.type.object_type} objects",
                    operand.span,
                    help="write 'not exists' to ask whether there are none",
                )
            # Exactly one answer, whatever the operand's cardinality: see
            # :meth:`netviz.nql.execute._Executor.unary`.
            return Unary(UnaryOp.NOT, operand, _BOOL, self.span_to_here(start))
        if start.is_word("exists"):
            self.advance()
            operand = self.parse_unary()
            self.count()
            return Unary(UnaryOp.EXISTS, operand, _BOOL, self.span_to_here(start))
        if start.is_word("distinct"):
            self.advance()
            operand = self.parse_unary()
            self.count()
            return Unary(UnaryOp.DISTINCT, operand, operand.type, self.span_to_here(start))
        return self.parse_comparison()

    def parse_comparison(self) -> Expr:
        left = self.parse_additive()
        if self.token.is_word("is"):
            return self.parse_is(left)
        negated = False
        if self.token.is_word("not") and self.peek(1).is_word("in"):
            self.advance()
            negated = True
        operator = self.token
        op = (
            _SYMBOL_COMPARISONS.get(operator.text)
            if operator.kind is TokenKind.SYMBOL
            else _WORD_COMPARISONS.get(operator.word)
        )
        if op is None:
            if negated:
                self.fail("expected 'in' after 'not'")
            return left
        self.advance()
        right = self.parse_additive()
        self.count()
        self.check_comparison(op, left, right, operator)
        card = left.type.card.then(right.type.card)
        node: Expr = Binary(
            op,
            left,
            right,
            ValueType(scalar=ScalarKind.BOOL, card=card),
            Span.between(left.span, right.span),
            self.span_of(operator),
        )
        if negated:
            node = Unary(UnaryOp.NOT, node, node.type, node.span)
        return node

    def parse_is(self, left: Expr) -> Expr:
        self.advance()
        negated = self.accept_word("not") is not None
        name = self.expect_name(what="a type name after 'is'")
        target = self.resolve_type(name)
        if not left.type.is_object and not left.type.is_empty:
            self.fail_at(
                f"'is' asks what type an object is, and this is {left.type}",
                left.span,
            )
        self.count()
        return IsType(
            left,
            target,
            negated,
            ValueType(scalar=ScalarKind.BOOL, card=left.type.card),
            Span.between(left.span, self.span_of(name)),
        )

    def check_comparison(self, op: BinaryOp, left: Expr, right: Expr, operator: Token) -> None:
        """Refuse the comparisons whose types cannot mean anything."""
        if left.type.is_empty or right.type.is_empty:
            return
        if op in (BinaryOp.EQ, BinaryOp.NE):
            if left.type.is_object != right.type.is_object:
                self.fail(
                    f"cannot compare {left.type} with {right.type}",
                    token=operator,
                    help="compare a property of the object, such as '.name'",
                )
            return
        if op in (BinaryOp.LT, BinaryOp.LE, BinaryOp.GT, BinaryOp.GE):
            for side in (left, right):
                if side.type.is_object:
                    self.fail_at(f"{op} needs a scalar, and this is an object", side.span)
                # A written value is not checked: ``.ip > '10.0.0.1'`` is how an
                # address comparison is spelled, and the address on the right is
                # a string until something coerces it. What is checked is the
                # *other* side — the one read off the model, where ``.name < 3``
                # is a mistyped comparison rather than a question about order.
                if isinstance(side, Literal):
                    continue
                if side.type.scalar is not None and not side.type.scalar.orders:
                    self.fail_at(
                        f"{side.type.scalar} does not order, so {op} cannot be written on it",
                        side.span,
                        help="compare with '=' or '~', or order it with 'order by'",
                    )
            return
        if op is BinaryOp.REGEX:
            self.check_regex(right)
            return
        if op is BinaryOp.IN and right.type.is_object and not left.type.is_object:
            self.fail(
                f"cannot look for {left.type} among {right.type.object_type} objects",
                token=operator,
            )

    def check_regex(self, right: Expr) -> None:
        if not isinstance(right, Literal) or not isinstance(right.value, str):
            return
        if len(right.value) > MAX_PATTERN:
            self.fail_at(
                f"the pattern is {len(right.value)} characters, over the {MAX_PATTERN} limit",
                right.span,
            )
        try:
            re.compile(right.value)
        except re.error as error:
            self.fail_at(f"{right.value!r} is not a regular expression: {error}", right.span)

    def parse_additive(self) -> Expr:
        left = self.parse_multiplicative()
        while self.token.kind is TokenKind.SYMBOL and self.token.text in _ADDITIVE:
            operator = self.advance()
            right = self.parse_multiplicative()
            left = self.arithmetic(_ADDITIVE[operator.text], left, right, operator)
        return left

    def parse_multiplicative(self) -> Expr:
        left = self.parse_prefix()
        while self.token.kind is TokenKind.SYMBOL and self.token.text in _MULTIPLICATIVE:
            operator = self.advance()
            right = self.parse_prefix()
            left = self.arithmetic(_MULTIPLICATIVE[operator.text], left, right, operator)
        return left

    def arithmetic(self, op: BinaryOp, left: Expr, right: Expr, operator: Token) -> Expr:
        self.count()
        card = left.type.card.then(right.type.card)
        if op is BinaryOp.CONCAT:
            for side in (left, right):
                if side.type.is_object:
                    self.fail_at("'++' joins text, and this is an object", side.span)
            return Binary(
                op,
                left,
                right,
                ValueType(scalar=ScalarKind.STR, card=card),
                Span.between(left.span, right.span),
                self.span_of(operator),
            )
        for side in (left, right):
            if side.type.is_empty:
                continue
            if side.type.is_object or side.type.scalar is None or not side.type.scalar.is_numeric:
                self.fail_at(f"{op} needs numbers, and this is {side.type}", side.span)
        scalar = (
            ScalarKind.INT
            if op is not BinaryOp.DIV
            and left.type.scalar is ScalarKind.INT
            and right.type.scalar is ScalarKind.INT
            else ScalarKind.FLOAT
        )
        return Binary(
            op,
            left,
            right,
            ValueType(scalar=scalar, card=card),
            Span.between(left.span, right.span),
            self.span_of(operator),
        )

    def parse_prefix(self) -> Expr:
        start = self.token
        if start.is_symbol("-"):
            self.advance()
            operand = self.parse_prefix()
            self.count()
            if not operand.type.is_empty and (
                operand.type.is_object
                or operand.type.scalar is None
                or not operand.type.scalar.is_numeric
            ):
                self.fail_at(f"cannot negate {operand.type}", operand.span)
            return Unary(UnaryOp.NEG, operand, operand.type, self.span_to_here(start))
        return self.parse_postfix()

    def parse_postfix(self) -> Expr:
        node = self.parse_primary()
        while True:
            if self.token.is_symbol("."):
                self.advance()
                node = self.parse_step(node)
            elif self.token.is_symbol("["):
                node = self.parse_type_filter(node)
            else:
                return node

    def parse_step(self, source: Expr) -> Expr:
        name = self.expect_name(what="a property or link name after '.'")
        if not source.type.is_object:
            self.fail(
                f"{source.type} has no members, so '.{name.text}' cannot be read from it",
                token=name,
            )
        member = self.schema.member(source.type.object_type, name.text)
        if member is None:
            self.unknown_member(source.type.object_type, name)
        self.count()
        if isinstance(member, Link):
            value = ValueType(
                object_type=self.schema.canonical(member.target),
                card=source.type.card.then(member.card),
            )
        else:
            value = ValueType(scalar=member.type, card=source.type.card.then(member.card))
        return Step(
            source,
            member.name,
            isinstance(member, Link),
            value,
            Span.between(source.span, self.span_of(name)),
            self.span_of(name),
        )

    def unknown_member(self, type_name: str, name: Token) -> NoReturn:
        close = self.schema.suggest_member(type_name, name.text)
        self.fail(
            f"{name.text!r} is not a member of {type_name}",
            token=name,
            help=(
                f"did you mean {' or '.join(repr(one) for one in close)}?"
                if close
                else f"run 'netviz query --describe {type_name}' to see what it has"
            ),
        )

    def parse_type_filter(self, source: Expr) -> Expr:
        opening = self.expect_symbol("[", why="to narrow by type")
        if self.accept_word("is") is None:
            self.fail("expected 'is' after '[', as in '[is server]'")
        name = self.expect_name(what="a type name")
        target = self.resolve_type(name)
        self.expect_symbol("]", why="to close the type filter")
        if not source.type.is_object:
            self.fail_at(f"cannot narrow {source.type} by type", source.span)
        if not self.schema.is_subtype(
            target, source.type.object_type
        ) and not self.schema.is_subtype(source.type.object_type, target):
            self.fail(
                f"no {source.type.object_type} is ever a {target}",
                token=name,
                help=f"'{source.type.object_type}' and '{target}' are unrelated types",
            )
        self.count()
        card = Cardinality.MANY if source.type.card is Cardinality.MANY else Cardinality.OPTIONAL
        return TypeFilter(
            source,
            target,
            ValueType(object_type=target, card=card),
            Span.between(source.span, self.span_of(opening)),
        )

    def resolve_type(self, name: Token) -> str:
        found = self.schema.resolve(name.text)
        if found is None:
            close = self.schema.suggest_type(name.text)
            self.fail(
                f"{name.text!r} is not a type",
                token=name,
                help=(
                    f"did you mean {' or '.join(repr(one) for one in close)}?"
                    if close
                    else "run 'netviz query --describe' to see every type"
                ),
            )
        return found.name

    # ------------------------------------------------------------------ #
    # Primaries
    # ------------------------------------------------------------------ #

    def parse_primary(self) -> Expr:
        token = self.token
        self.count()
        if token.kind is TokenKind.NUMBER:
            self.advance()
            span = self.span_of(token)
            if "." in token.text:
                return Literal(
                    float(token.text),
                    ValueType(scalar=ScalarKind.FLOAT, card=Cardinality.ONE),
                    span,
                )
            return Literal(
                int(token.text), ValueType(scalar=ScalarKind.INT, card=Cardinality.ONE), span
            )
        if token.kind is TokenKind.STRING:
            self.advance()
            return Literal(
                token.text,
                ValueType(scalar=ScalarKind.STR, card=Cardinality.ONE),
                self.span_of(token),
            )
        if token.is_word("true", "false"):
            self.advance()
            return Literal(token.word == "true", _BOOL, self.span_of(token))
        if token.is_word("none"):
            self.advance()
            return Literal(None, ValueType(card=Cardinality.OPTIONAL), self.span_of(token))
        if token.is_word("select"):
            return self.parse_select()
        if token.is_symbol("("):
            self.advance()
            self.deeper()
            try:
                inner = self.parse_statement()
            finally:
                self.depth -= 1
            self.expect_symbol(")", why="to close the group")
            return inner
        if token.is_symbol("{"):
            return self.parse_braced()
        if token.is_symbol("."):
            return self.parse_this()
        if token.kind is TokenKind.WORD:
            return self.parse_name()
        self.fail(f"expected a value, a name or 'select', found {token.describe()}")

    def parse_this(self) -> Expr:
        """The implicit subject a leading ``.`` navigates from."""
        if not self.scope:
            self.fail(
                "a leading '.' reads a member of the object being filtered, and there is none here",
                help="name the type, as in 'select device.name'",
            )
        return This(
            ValueType(object_type=self.scope[-1], card=Cardinality.ONE),
            Span(self.token.offset, 1),
        )

    def parse_name(self) -> Expr:
        name = self.advance()
        if self.token.is_symbol("("):
            return self.parse_call(name)
        if name.text in self.bindings:
            return Var(name.text, self.bindings[name.text], self.span_of(name))
        found = self.schema.resolve(name.text)
        if found is None:
            close = [*self.schema.suggest_type(name.text), *sorted(self.bindings)[:2]]
            self.fail(
                f"{name.text!r} is not a type, a binding or a function",
                token=name,
                help=(
                    f"did you mean {' or '.join(repr(one) for one in close[:3])}?"
                    if close
                    else "a bare word names a type; write a value in quotes"
                ),
            )
        return Root(
            found.name,
            ValueType(object_type=found.name, card=Cardinality.MANY),
            self.span_of(name),
        )

    def parse_call(self, name: Token) -> Expr:
        function = FUNCTIONS.get(name.word)
        if function is None:
            close = [one for one in FUNCTIONS if one.startswith(name.word[:2])]
            self.fail(
                f"{name.text!r} is not a function",
                token=name,
                help=(
                    f"did you mean {close[0]!r}?"
                    if close
                    else f"the functions are {', '.join(FUNCTIONS)}"
                ),
            )
        self.expect_symbol("(", why="to open the argument list")
        args: list[Expr] = []
        if self.accept_symbol(")") is None:
            while True:
                args.append(self.parse_statement())
                if self.accept_symbol(",") is None:
                    break
            self.expect_symbol(")", why="to close the argument list")
        if not function.required <= len(args) <= len(function.accepts):
            wanted = (
                str(function.required)
                if function.required == len(function.accepts)
                else f"{function.required} to {len(function.accepts)}"
            )
            self.fail(
                f"{function.name} takes {wanted} arguments, and {len(args)} were given",
                token=name,
                help=function.signature(),
            )
        for index, argument in enumerate(args):
            complaint = function.accepts[index].rejects(argument.type)
            if complaint:
                self.fail_at(
                    f"argument {index + 1} of {function.name} must be {complaint}, "
                    f"and this is {argument.type}",
                    argument.span,
                    help=function.signature(),
                )
        return Call(
            function.name,
            tuple(args),
            function.infer([one.type for one in args]),
            self.span_to_here(name),
            self.span_of(name),
        )

    def parse_braced(self) -> Expr:
        """``{…}`` is a free object when its first entry is ``name := …``."""
        start = self.token
        if self.peek(1).kind is TokenKind.WORD and self.peek(2).is_symbol(":="):
            shape = self.parse_shape(subject="")
            return FreeObject(shape, ValueType(card=Cardinality.ONE), self.span_to_here(start))
        self.advance()
        items: list[Expr] = []
        if self.accept_symbol("}") is not None:
            return SetLiteral((), ValueType(card=Cardinality.MANY), self.span_to_here(start))
        while True:
            items.append(self.parse_expr())
            if self.accept_symbol(",") is None:
                break
        self.expect_symbol("}", why="to close the set")
        return SetLiteral(tuple(items), self.union_of(items), self.span_to_here(start))

    def union_of(self, items: list[Expr]) -> ValueType:
        """The type of ``{a, b}``: what both are, and always a set."""
        known = [one for one in items if not one.type.is_empty]
        if not known:
            return ValueType(card=Cardinality.MANY)
        first = known[0].type
        for other in known[1:]:
            if first.is_object != other.type.is_object:
                self.fail_at(
                    f"a set holds one kind of thing, and this mixes {first} with {other.type}",
                    other.span,
                )
            if first.is_object:
                shared = self.schema.common(first.object_type, other.type.object_type)
                if not shared:
                    self.fail_at(
                        f"{first.object_type} and {other.type.object_type} have no type in common",
                        other.span,
                    )
                first = ValueType(object_type=shared, card=Cardinality.MANY)
            elif first.scalar is not other.type.scalar:
                first = ValueType(scalar=ScalarKind.STR, card=Cardinality.MANY)
        return first.with_card(Cardinality.MANY)

    # ------------------------------------------------------------------ #
    # select
    # ------------------------------------------------------------------ #

    def parse_select(self) -> Expr:
        start = self.advance()
        self.deeper()
        try:
            source = self.parse_expr()
            shape = None
            if self.token.is_symbol("{") and source.type.is_object:
                shape = self.parse_shape(subject=source.type.object_type)
            filters, order, limit, offset = self.parse_clauses(_path_subject(source).type)
        finally:
            self.depth -= 1
        return self.select_over(
            source, shape, filters, order, limit, offset, self.span_to_here(start)
        )

    def select_over(
        self,
        source: Expr,
        shape: Shape | None,
        filters: tuple[Expr, ...],
        order: tuple[OrderItem, ...],
        limit: Expr | None,
        offset: Expr | None,
        span: Span,
    ) -> Expr:
        """Build the node, and put the clauses where the reader meant them.

        When the selected expression is a path ending in a scalar —
        ``interface.mac`` — the clauses belong to the *objects* the path came
        from, not to the strings it ends in: ``select interface.mac filter
        .parent is switch`` is "the MACs of the switch ports". So the ``select``
        is inserted at the path's subject and the remaining steps are re-applied
        on top, which is exactly what writing ``(interface filter …).mac`` says
        by hand.
        """
        if (
            isinstance(source, FreeObject)
            and shape is None
            and not (filters or order or limit or offset)
        ):
            # ``select { a := … }`` is the free object itself. Wrapping it would
            # make the query's shape the wrapper's, which is no shape at all.
            return source
        subject = _path_subject(source)
        if subject is not source:
            return _rebuild_path(
                source,
                subject,
                self.select_over(subject, None, filters, order, limit, offset, span),
            )
        card = source.type.card
        if filters or limit is not None or offset is not None:
            card = Cardinality.MANY if card is Cardinality.MANY else Cardinality.OPTIONAL
        return Select(
            source=source,
            shape=shape,
            filters=filters,
            order=order,
            limit=limit,
            offset=offset,
            type=source.type.with_card(card),
            span=span,
        )

    def parse_clauses(
        self, subject: ValueType
    ) -> tuple[tuple[Expr, ...], tuple[OrderItem, ...], Expr | None, Expr | None]:
        """``filter``, ``order by``, ``limit`` and ``offset``, in any order."""
        filters: list[Expr] = []
        order: list[OrderItem] = []
        limit: Expr | None = None
        offset: Expr | None = None
        while True:
            if self.token.is_word("filter", "where"):
                word = self.advance()
                condition = self.scoped(subject, self.parse_expr)
                self.check_condition(word.word, condition)
                filters.append(condition)
                continue
            if self.token.is_word("order"):
                if order:
                    self.fail("this query already has an 'order by'")
                self.advance()
                if self.accept_word("by") is None:
                    self.fail("expected 'by' after 'order'")
                order.extend(self.parse_order(subject))
                continue
            if self.token.is_word("limit", "offset"):
                word = self.advance()
                value = self.scoped(subject, self.parse_expr)
                self.check_count(word.word, value)
                if word.word == "limit":
                    if limit is not None:
                        self.fail("this query already has a 'limit'", token=word)
                    limit = value
                else:
                    if offset is not None:
                        self.fail("this query already has an 'offset'", token=word)
                    offset = value
                continue
            return tuple(filters), tuple(order), limit, offset

    def parse_order(self, subject: ValueType) -> list[OrderItem]:
        found: list[OrderItem] = []
        while True:
            key = self.scoped(subject, self.parse_expr)
            if key.type.is_object:
                self.fail_at(
                    "'order by' needs a scalar, and this is an object",
                    key.span,
                    help="order by one of its properties, such as '.name'",
                )
            descending = self.accept_word("desc") is not None
            if not descending:
                self.accept_word("asc")
            found.append(OrderItem(key, descending))
            if self.accept_word("then") is None and self.accept_symbol(",") is None:
                return found

    def check_condition(self, word: str, condition: Expr) -> None:
        """``filter`` takes a yes-or-no question, not a set of things."""
        if condition.type.is_empty:
            return
        if condition.type.is_object:
            self.fail_at(
                f"'{word}' needs a condition, and this is {condition.type.object_type} objects",
                condition.span,
                help="write 'exists' in front of it to keep the ones that have any",
            )
        if condition.type.scalar is not ScalarKind.BOOL:
            self.fail_at(
                f"'{word}' needs a condition, and this is {condition.type}",
                condition.span,
                help="compare it, as in \"= 'value'\"",
            )

    def check_count(self, word: str, value: Expr) -> None:
        if value.type.is_empty:
            return
        if value.type.is_object or value.type.scalar is not ScalarKind.INT:
            self.fail_at(f"'{word}' needs a whole number, and this is {value.type}", value.span)

    def scoped(self, subject: ValueType, parse: Callable[[], Expr]) -> Expr:
        """Parse with ``subject``'s object type as what a leading ``.`` reads."""
        pushed = subject.is_object
        if pushed:
            self.scope.append(subject.object_type)
        try:
            return parse()
        finally:
            if pushed:
                self.scope.pop()

    # ------------------------------------------------------------------ #
    # Shapes
    # ------------------------------------------------------------------ #

    def parse_shape(self, *, subject: str) -> Shape:
        start = self.expect_symbol("{", why="to open a shape")
        elements: list[ShapeElement] = []
        names: set[str] = set()
        if self.token.is_symbol("}"):
            self.fail("a shape must project at least one field")
        while True:
            for element in self.parse_shape_element(subject):
                if element.name in names:
                    self.fail_at(f"{element.name!r} is projected twice", element.span)
                names.add(element.name)
                elements.append(element)
            if self.accept_symbol(",") is None:
                break
            if self.token.is_symbol("}"):
                break
        self.expect_symbol("}", why="to close the shape")
        return Shape(tuple(elements), self.span_to_here(start))

    def parse_shape_element(self, subject: str) -> list[ShapeElement]:
        start = self.token
        if self.accept_symbol("*") is not None:
            if not subject:
                self.fail("'*' expands to a type's properties, and this object has no type")
            splat: list[ShapeElement] = []
            for one in self.schema.properties(subject):
                path = self.member_path(subject, one.name, start)
                splat.append(ShapeElement(one.name, path, path.type, span=self.span_of(start)))
            return splat
        name = self.expect_name(what="a field name")
        if self.accept_symbol(":=") is not None:
            value = self.scoped(
                ValueType(object_type=subject, card=Cardinality.ONE) if subject else ValueType(),
                self.parse_expr,
            )
            return [self.finish_element(name.text, value, start)]
        if self.token.is_symbol(":"):
            self.advance()
            path = self.member_path(subject, name.text, name)
            return [self.finish_element(name.text, path, start, expect_shape=True)]
        path = self.member_path(subject, name.text, name)
        return [ShapeElement(name.text, path, path.type, span=self.span_of(name))]

    def finish_element(
        self, name: str, value: Expr, start: Token, *, expect_shape: bool = False
    ) -> ShapeElement:
        """Attach a nested shape and any clauses to one projected field."""
        shape: Shape | None = None
        if self.token.is_symbol("{") and value.type.is_object:
            shape = self.parse_shape(subject=value.type.object_type)
        elif expect_shape:
            self.fail("expected '{' after ':', to say what the linked objects project to")
        filters, order, limit, offset = self.parse_clauses(value.type)
        if filters or order or limit is not None or offset is not None:
            card = value.type.card
            if filters or limit is not None or offset is not None:
                card = Cardinality.MANY if card is Cardinality.MANY else Cardinality.OPTIONAL
            value = Select(
                source=value,
                shape=None,
                filters=filters,
                order=order,
                limit=limit,
                offset=offset,
                type=value.type.with_card(card),
                span=value.span,
            )
        return ShapeElement(name, value, value.type, shape, self.span_to_here(start))

    def member_path(self, subject: str, member: str, at: Token) -> Step:
        """``.member`` read from the shape's subject."""
        if not subject:
            self.fail(
                f"{member!r} names a member, and a free object has no type to read one from",
                token=at,
                help=f"write '{member} := …' to compute the field",
            )
        found = self.schema.member(subject, member)
        if found is None:
            self.unknown_member(subject, at)
        self.count()
        base = This(ValueType(object_type=subject, card=Cardinality.ONE), self.span_of(at))
        if isinstance(found, Link):
            value = ValueType(object_type=self.schema.canonical(found.target), card=found.card)
        else:
            value = ValueType(scalar=found.type, card=found.card)
        return Step(
            base, found.name, isinstance(found, Link), value, self.span_of(at), self.span_of(at)
        )


def _path_subject(node: Expr) -> Expr:
    """The object set a scalar-ended path came from, or ``node`` itself.

    ``interface.mac`` yields ``interface``; ``device.interfaces.name`` yields
    ``device.interfaces``; ``device.interfaces`` yields itself, because it is
    already the objects being talked about. Anything that is not a path — a
    function call, a literal — yields itself, and a ``filter`` after one of
    those has no subject for a leading ``.`` to read.
    """
    current = node
    while not current.type.is_object and isinstance(current, (Step, TypeFilter)):
        current = current.source
    return current if current.type.is_object else node


def _rebuild_path(node: Expr, subject: Expr, replacement: Expr) -> Expr:
    """``node`` with ``subject`` swapped for ``replacement``, its steps kept.

    The step types are kept as they were: a filter can only shrink a set, so
    nothing downstream is told it will get more than it does.
    """
    if node is subject:
        return replacement
    if isinstance(node, (Step, TypeFilter)):
        return replace(node, source=_rebuild_path(node.source, subject, replacement))
    return node
