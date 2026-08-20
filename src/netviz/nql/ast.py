"""The shape of a parsed relational query.

Every node carries two things beyond its own content: the :class:`Span` of query
text it came from, so a failure while *executing* can underline the same way a
parse error does, and the :class:`~netviz.nql.types.ValueType` the parser
inferred for it.

Carrying the type on the tree rather than recomputing it is what makes the
result *structured*. A shape element over a
:attr:`~netviz.nql.types.Cardinality.MANY` link becomes a JSON array and one
over :attr:`~netviz.nql.types.Cardinality.ONE` becomes a scalar, and the
executor decides which by reading :attr:`ShapeElement.type` — not by looking at
how many values happened to come back. A device with one interface still
renders ``"interfaces": [ … ]``, which is the difference between a result a
script can rely on and one it has to sniff.

The tree is closed and finite. There is no user-defined function, no lambda and
no recursion: a query is a bounded walk of the schema, and the only unbounded
thing in the language — the connected-component search behind ``reachable`` —
is a library function whose termination is its own business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from netviz.nql.types import ValueType

__all__ = [
    "Binary",
    "BinaryOp",
    "Call",
    "Expr",
    "FreeObject",
    "IsType",
    "Literal",
    "OrderItem",
    "Query",
    "Root",
    "Select",
    "SetLiteral",
    "Shape",
    "ShapeElement",
    "Span",
    "Step",
    "This",
    "TypeFilter",
    "Unary",
    "UnaryOp",
    "Var",
]


@dataclass(frozen=True, slots=True)
class Span:
    """Where a node came from, as an offset and a length into the query text."""

    offset: int = 0
    length: int = 1

    @classmethod
    def between(cls, start: Span, end: Span) -> Span:
        """The span covering both, for a node built from two others."""
        left = min(start.offset, end.offset)
        right = max(start.offset + start.length, end.offset + end.length)
        return cls(left, max(1, right - left))


class BinaryOp(str, Enum):
    """How two expressions combine. The value is the operator as it is written."""

    OR = "or"
    AND = "and"
    EQ = "="
    NE = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    #: Membership in a set, or — when the right side is a prefix — containment.
    IN = "in"
    #: Shell-style glob, as :mod:`fnmatch` matches, case-sensitively.
    LIKE = "~"
    #: The same, case-insensitively.
    ILIKE = "ilike"
    #: Its negation.
    NOT_LIKE = "!~"
    #: Regular expression, unanchored, as :mod:`re` searches.
    REGEX = "=~"
    #: Namespace containment: ``.namespace under 'sites'`` keeps ``sites/north``.
    UNDER = "under"
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    MOD = "%"
    #: String concatenation, spelled as it is in EdgeQL so that ``+`` stays
    #: unambiguously arithmetic.
    CONCAT = "++"

    def __str__(self) -> str:
        return self.value

    @property
    def is_arithmetic(self) -> bool:
        """Does it compute a number? ``++`` is not one: it joins text."""
        return self in (BinaryOp.ADD, BinaryOp.SUB, BinaryOp.MUL, BinaryOp.DIV, BinaryOp.MOD)


class UnaryOp(str, Enum):
    """A prefix operator."""

    NOT = "not"
    NEG = "-"
    #: True when the operand's set is not empty. Always exactly one boolean.
    EXISTS = "exists"
    #: The operand's set with repeats removed, order preserved.
    DISTINCT = "distinct"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Literal:
    """A written scalar: a number, a quoted string, ``true``, ``false``, ``none``."""

    value: str | int | float | bool | None
    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class SetLiteral:
    """``{a, b, c}`` — the union of its items, in order, repeats kept."""

    items: tuple[Expr, ...]
    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Root:
    """A type name standing alone: every object of that type in the inventory."""

    #: The canonical type name, whatever spelling was written.
    type_name: str
    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Var:
    """A reference to a ``with`` binding."""

    name: str
    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class This:
    """The implicit scope a leading ``.`` navigates from.

    Inside ``filter``, a shape or an ``order by`` this is the object currently
    being considered; at the top level of a query there is none, and writing a
    leading ``.`` there is a parse error naming that fact.
    """

    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Step:
    """One navigation: ``source.member``, for a property or a link."""

    source: Expr
    #: The member as the schema spells it, not as it was written.
    member: str
    #: Whether the step lands on objects. Saves the executor a schema lookup.
    is_link: bool
    type: ValueType
    span: Span = field(default_factory=Span)
    member_span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class TypeFilter:
    """``source[is server]`` — the members of ``source`` that are that type."""

    source: Expr
    type_name: str
    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class IsType:
    """``x is server`` / ``x is not server`` — a boolean, not a narrowing."""

    operand: Expr
    type_name: str
    negated: bool
    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Call:
    """A built-in function applied to its arguments."""

    name: str
    args: tuple[Expr, ...]
    type: ValueType
    span: Span = field(default_factory=Span)
    name_span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Unary:
    op: UnaryOp
    operand: Expr
    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Binary:
    op: BinaryOp
    left: Expr
    right: Expr
    type: ValueType
    span: Span = field(default_factory=Span)
    op_span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class OrderItem:
    """One key of an ``order by`` clause."""

    expr: Expr
    descending: bool = False


@dataclass(frozen=True, slots=True)
class ShapeElement:
    """One field of a shape: a name, and the expression that fills it.

    ``foo`` is short for ``foo := .foo``; ``foo: {…}`` is short for
    ``foo := .foo {…}``. :attr:`type` is the expression's, and it is what decides
    whether the field renders as an array or as a single value.
    """

    name: str
    expr: Expr
    type: ValueType
    shape: Shape | None = None
    span: Span = field(default_factory=Span)

    @property
    def is_multi(self) -> bool:
        """Does this field render as a JSON array?"""
        return self.type.card.is_multi


@dataclass(frozen=True, slots=True)
class Shape:
    """``{ name, interfaces: { name } }`` — what an object projects to."""

    elements: tuple[ShapeElement, ...]
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class FreeObject:
    """``{ total := count(device), names := device.name }`` — one object, unlinked.

    Exactly one value comes back, whatever its fields hold. It is how a query
    returns a *report* rather than a list: a summary object with several
    independently-computed fields.
    """

    shape: Shape
    type: ValueType
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Select:
    """``select <source> <shape>? filter … order by … limit … offset …``.

    An expression, not a statement: it may be parenthesised and used anywhere a
    set is expected, which is what makes a correlated sub-query
    ``(select .labels filter …)`` a thing the grammar already had rather than a
    feature bolted on.
    """

    source: Expr
    shape: Shape | None = None
    #: Conjoined: two ``filter`` clauses are an ``and``, which is what a reader
    #: writing them on separate lines means.
    filters: tuple[Expr, ...] = ()
    order: tuple[OrderItem, ...] = ()
    limit: Expr | None = None
    offset: Expr | None = None
    type: ValueType = field(default_factory=ValueType)
    span: Span = field(default_factory=Span)


#: Every node type. A union rather than a base class because the executor
#: dispatches on it exhaustively and mypy checks that it did.
Expr = (
    Literal
    | SetLiteral
    | Root
    | Var
    | This
    | Step
    | TypeFilter
    | IsType
    | Call
    | Unary
    | Binary
    | FreeObject
    | Select
)


@dataclass(frozen=True, slots=True)
class Query:
    """A parsed query, the text it came from, and what it will produce."""

    text: str
    #: ``with`` bindings, in the order they were written; each may use the ones
    #: before it.
    bindings: tuple[tuple[str, Expr], ...]
    body: Expr
    source: str = "query"

    @property
    def type(self) -> ValueType:
        """What the query returns."""
        return self.body.type

    @property
    def shape(self) -> Shape | None:
        """The projection at the top of the query, when there is one."""
        if isinstance(self.body, Select):
            return self.body.shape
        if isinstance(self.body, FreeObject):
            return self.body.shape
        return None
