"""The shape of a parsed query.

Nine node types, all frozen, all carrying the span of query text they came from
so that an error raised while *evaluating* — an unknown element named as the
centre of a traversal, say — can underline the same way a parse error does.

The set is closed on purpose. There is no call node, no binding, no lambda and
no arithmetic: a query is a predicate over the resolved model plus bounded graph
traversal, and that is the whole of it. Everything the language can express
terminates, because the only recursion is over this finite tree and the only
loop is a breadth-first search bounded by the node count.

The tree also has no notion of *how* to evaluate anything. Attribute lookup
lives in :mod:`netgraph.query.facts`, the vocabulary in
:mod:`netgraph.query.attributes` and the walk in :mod:`netgraph.query.evaluate`;
this module is the grammar's data and nothing else, which is what lets the
parser be tested without a graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "All",
    "And",
    "Comparison",
    "Exists",
    "Expr",
    "Not",
    "Operator",
    "Or",
    "Scope",
    "Span",
    "Traversal",
    "TraversalKind",
    "walk",
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


class Operator(str, Enum):
    """How a comparison compares. The value is the operator as it is written."""

    #: Exact equality, after the value has been coerced to the attribute's type.
    EQ = "="
    #: Its negation. ``a != b`` is exactly ``not (a = b)``, including for
    #: multi-valued attributes: it is true when *no* value equals.
    NE = "!="
    #: Shell-style glob, as :mod:`fnmatch` matches, case-sensitively.
    GLOB = "~"
    #: Its negation.
    NOT_GLOB = "!~"
    #: Regular expression, unanchored, as :mod:`re` searches.
    REGEX = "=~"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    #: Membership: in a parenthesised list of alternatives, or — for an address
    #: attribute — inside a CIDR prefix.
    IN = "in"
    #: Namespace containment: ``namespace under sites`` keeps ``sites/north``.
    UNDER = "under"

    def __str__(self) -> str:
        return self.value


class TraversalKind(str, Enum):
    """Which walk of the graph a traversal term performs."""

    #: Every node adjacent to a match. Exactly ``within 1 hops of``, minus the
    #: matches themselves.
    NEIGHBORS = "neighbors"
    #: Every node at most N hops from a match, the matches included.
    WITHIN = "within"
    #: The whole connected component of every match, the matches included.
    REACHABLE = "reachable"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class All:
    """The predicate that holds of everything. What ``*`` parses to."""

    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Comparison:
    """``<attribute> <operator> <value>``.

    ``values`` holds one item for every operator but :attr:`Operator.IN`, where
    it holds the alternatives. Each value is text: coercion to an int, a VLAN id
    or an IP network happens against the attribute's declared type when the
    query is *bound* to a vocabulary, which is where a bad value gets a
    diagnostic naming both the value and the attribute that rejected it.
    """

    attribute: str
    operator: Operator
    values: tuple[str, ...]
    span: Span = field(default_factory=Span)
    #: Span of the attribute alone, for "unknown attribute" diagnostics.
    attribute_span: Span = field(default_factory=Span)
    #: Span of the value part alone, for "not a VLAN id" diagnostics.
    value_span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Exists:
    """``has <attribute>`` — the attribute is set, or holds at least one value."""

    attribute: str
    span: Span = field(default_factory=Span)
    attribute_span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Scope:
    """``interface[…]`` — true when some sub-object satisfies the inner query.

    Existential, always. ``interface[address in 10.0.0.0/8]`` selects an element
    with *an* interface addressed there; ``not interface[…]`` is how "no
    interface" is said, which is the only reading under which the two are
    negations of one another.
    """

    #: Which family of sub-object: ``interface``, ``link``, ``netns`` or ``zone``.
    domain: str
    body: Expr
    span: Span = field(default_factory=Span)
    domain_span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Traversal:
    """``neighbors of X`` / ``within N hops of X`` / ``reachable from X``."""

    kind: TraversalKind
    body: Expr
    #: Hop budget. 1 for ``neighbors``, the whole graph (``None``) for
    #: ``reachable``, and the written number for ``within N hops of``.
    hops: int | None = 1
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Not:
    """``not X``."""

    body: Expr
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class And:
    """``X and Y``, n-ary and flattened, so ``a and b and c`` is one node."""

    operands: tuple[Expr, ...]
    span: Span = field(default_factory=Span)


@dataclass(frozen=True, slots=True)
class Or:
    """``X or Y``, n-ary and flattened."""

    operands: tuple[Expr, ...]
    span: Span = field(default_factory=Span)


#: Every node type. Written as a union rather than a base class because the
#: evaluator dispatches on it exhaustively and mypy checks that it did.
Expr = All | Comparison | Exists | Scope | Traversal | Not | And | Or


def walk(expr: Expr) -> list[Expr]:
    """Every node of the tree, parents before children, in written order.

    For the checks that are about the query rather than about the network: does
    it mention an attribute this domain does not have, does it traverse inside a
    scope (which it may not), how deep does it nest.
    """
    found: list[Expr] = []
    pending: list[Expr] = [expr]
    while pending:
        node = pending.pop()
        found.append(node)
        if isinstance(node, (And, Or)):
            pending.extend(reversed(node.operands))
        elif isinstance(node, (Not, Traversal, Scope)):
            pending.append(node.body)
    return found
