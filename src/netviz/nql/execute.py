"""Running a checked query against a built world.

Two passes, kept apart on purpose.

**Evaluation** turns an expression into a set of values — scalars, or references
to objects. It is where ``filter``, ``order by``, arithmetic and the functions
happen, and it never produces a dictionary.

**Projection** turns those values into the result: a scalar stays a scalar, and
an object becomes whatever its shape says it becomes, recursively. A shape is
therefore *output only*, exactly as it is in EdgeQL: ``(select device { name })``
used as a value is still a set of devices, and ``.vendor`` may be read from it.
Folding the two together would make a shape silently change what an expression
means, which is the bug that makes nested projections hard to reason about in
languages that do fold them.

Set semantics follow the same rule everywhere: an operator applies to the
cartesian product of its operands, and ``filter`` keeps an object when its
condition yields *at least one* true. So ``filter .addresses.ip = '10.0.0.1'``
means "has such an address", which is what an operator means by it. The
consequence to know is that ``!=`` is its mirror and not its negation — on a
multi-valued property it means "has a value that differs". ``not exists`` and
``all()`` are how the universal is said.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from netviz.nql.ast import (
    Binary,
    BinaryOp,
    Call,
    Expr,
    FreeObject,
    IsType,
    Literal,
    Param,
    Query,
    Root,
    Select,
    SetLiteral,
    Shape,
    Step,
    This,
    TypeFilter,
    Unary,
    UnaryOp,
    Var,
)
from netviz.nql.functions import FUNCTIONS
from netviz.nql.types import ValueType
from netviz.nql.world import Ref, Value, World
from netviz.query.errors import QueryError

__all__ = ["Column", "Result", "execute"]

#: How many compiled patterns to keep. A query has a handful; a session that
#: runs thousands of different ones is a generator, and re-compiling is the
#: right price for it.
_PATTERN_CACHE: Final[int] = 512

_COMPILED: dict[str, re.Pattern[str]] = {}


@dataclass(frozen=True, slots=True)
class Column:
    """One column of the result, for the renderers that need a table."""

    name: str
    #: Does this column hold a list?
    multi: bool = False
    #: Right-align numbers, left-align everything else.
    numeric: bool = False


@dataclass(frozen=True, slots=True)
class Result:
    """What a query produced, in the one shape every output format reads.

    :attr:`rows` is JSON-ready: scalars, ``None``, lists and dictionaries, and
    nothing else. Whether a field is a list is decided by the *schema*, through
    :attr:`~netviz.nql.ast.ShapeElement.type`, not by how many values came
    back — so a device with one interface still projects a one-element array and
    a script never has to sniff.
    """

    query: Query
    rows: tuple[Any, ...]
    type: ValueType
    columns: tuple[Column, ...] = ()

    def __len__(self) -> int:
        return len(self.rows)

    def __bool__(self) -> bool:
        return bool(self.rows)

    @property
    def is_tabular(self) -> bool:
        """Does every row have named fields?"""
        return bool(self.columns) and all(isinstance(row, dict) for row in self.rows)


def execute(query: Query, world: World, params: Mapping[str, Any] | None = None) -> Result:
    """Answer ``query`` against ``world``.

    Args:
        query: A query parsed against ``world``'s schema.
        params: The value of each ``$name`` the query uses, as
            :func:`~netviz.nql.binding.bind` returns them. The parser has
            already checked that every parameter the query names is here, so a
            missing one is a caller that parsed with one table and ran with
            another; it evaluates to the empty set rather than raising, which is
            what an absent value means everywhere else in the language.
    """
    return _Executor(world, params=dict(params or {})).run(query)


@dataclass
class _Executor:
    world: World
    bindings: dict[str, tuple[Value, ...]] = field(default_factory=dict)
    #: What each ``$name`` was bound to, by the caller rather than by the query.
    params: dict[str, tuple[Value, ...]] = field(default_factory=dict)
    #: The query as written, so a failure at run time underlines it the way a
    #: parse error does.
    text: str = ""

    # ------------------------------------------------------------------ #
    # Entry
    # ------------------------------------------------------------------ #

    def run(self, query: Query) -> Result:
        self.text = query.text
        for name, value in query.bindings:
            self.bindings[name] = self.evaluate(value, None)
        rows = tuple(self.project(query.body, None))
        return Result(query, rows, query.type, _columns(query))

    # ------------------------------------------------------------------ #
    # Projection
    # ------------------------------------------------------------------ #

    def project(self, node: Expr, scope: Ref | None) -> list[Any]:
        """The JSON values ``node`` contributes, with its shape applied."""
        if isinstance(node, FreeObject):
            return [self.object_of(node.shape, scope)]
        if isinstance(node, Select) and node.shape is None and isinstance(node.source, FreeObject):
            return self.project(node.source, scope)
        values = self.evaluate(node, scope)
        shape = node.shape if isinstance(node, Select) else None
        if shape is not None:
            return [self.object_of(shape, one) for one in values if isinstance(one, Ref)]
        return [self.plain(one) for one in values]

    def object_of(self, shape: Shape, scope: Ref | None) -> dict[str, Any]:
        """One object, projected through ``shape``."""
        found: dict[str, Any] = {}
        for element in shape.elements:
            if element.shape is not None:
                values = self.evaluate(element.expr, scope)
                items: list[Any] = [
                    self.object_of(element.shape, one) for one in values if isinstance(one, Ref)
                ]
            else:
                items = self.project(element.expr, scope)
            found[element.name] = items if element.is_multi else (items[0] if items else None)
        return found

    def plain(self, value: Value) -> Any:
        """One value as JSON: an object becomes the name a reader would recognise."""
        return self.world.display(value) if isinstance(value, Ref) else value

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def evaluate(self, node: Expr, scope: Ref | None) -> tuple[Value, ...]:
        if isinstance(node, Literal):
            return () if node.value is None else (node.value,)
        if isinstance(node, SetLiteral):
            return tuple(one for item in node.items for one in self.evaluate(item, scope))
        if isinstance(node, Root):
            return tuple(self.world.all(node.type_name))
        if isinstance(node, Var):
            return self.bindings.get(node.name, ())
        if isinstance(node, Param):
            return self.params.get(node.name, ())
        if isinstance(node, This):
            return (scope,) if scope is not None else ()
        if isinstance(node, Step):
            return self.step(node, scope)
        if isinstance(node, TypeFilter):
            return tuple(
                one
                for one in self.evaluate(node.source, scope)
                if isinstance(one, Ref)
                and self.world.schema.is_subtype(self.world.type_of(one), node.type_name)
            )
        if isinstance(node, IsType):
            return tuple(
                self.is_type(one, node.type_name) != node.negated
                for one in self.evaluate(node.operand, scope)
            )
        if isinstance(node, Call):
            args = [self.evaluate(one, scope) for one in node.args]
            return FUNCTIONS[node.name].apply(self.world, args)
        if isinstance(node, Unary):
            return self.unary(node, scope)
        if isinstance(node, Binary):
            return self.binary(node, scope)
        if isinstance(node, FreeObject):
            # Only meaningful as a projection; as a value it is one opaque thing,
            # and nothing in the grammar can look inside it.
            return ()
        return self.select(node, scope)

    def step(self, node: Step, scope: Ref | None) -> tuple[Value, ...]:
        """``source.member``, with object results deduplicated.

        Two interfaces of one device walked back with ``.parent`` are one
        device, not two — an object set is a set. Scalars keep their repeats,
        because ``count(.interfaces.mtu)`` counting distinct MTUs rather than
        interfaces would surprise everybody.
        """
        found: list[Value] = []
        seen: set[str] = set()
        for one in self.evaluate(node.source, scope):
            if not isinstance(one, Ref):
                continue
            for value in self.world.step(one, node.member, is_link=node.is_link):
                if isinstance(value, Ref):
                    if value.id in seen:
                        continue
                    seen.add(value.id)
                found.append(value)
        return tuple(found)

    def is_type(self, value: Value, type_name: str) -> bool:
        if not isinstance(value, Ref):
            return False
        return self.world.schema.is_subtype(self.world.type_of(value), type_name)

    def unary(self, node: Unary, scope: Ref | None) -> tuple[Value, ...]:
        if node.op is UnaryOp.EXISTS:
            return (bool(self.evaluate(node.operand, scope)),)
        values = self.evaluate(node.operand, scope)
        if node.op is UnaryOp.NOT:
            # One answer, not one per value. ``not (.addresses.ip = '10.0.0.1')``
            # means "has no such address"; the elementwise reading would mean
            # "has some other address", which is what ``!=`` already says and
            # would leave the universal impossible to write. It also makes
            # ``filter X`` and ``filter not X`` an exact partition of the set.
            return (not any(_truth(one) for one in values),)
        if node.op is UnaryOp.NEG:
            return tuple(
                -one
                for one in values
                if isinstance(one, (int, float)) and not isinstance(one, bool)
            )
        found: list[Value] = []
        seen: set[Any] = set()
        for one in values:
            key = one.id if isinstance(one, Ref) else (type(one).__name__, one)
            if key in seen:
                continue
            seen.add(key)
            found.append(one)
        return tuple(found)

    def binary(self, node: Binary, scope: Ref | None) -> tuple[Value, ...]:
        left = self.evaluate(node.left, scope)
        if node.op is BinaryOp.IN:
            right = self.evaluate(node.right, scope)
            return tuple(_member(one, right) for one in left)
        right = self.evaluate(node.right, scope)
        if node.op in (BinaryOp.AND, BinaryOp.OR):
            wants_both = node.op is BinaryOp.AND
            return tuple(
                (_truth(one) and _truth(other)) if wants_both else (_truth(one) or _truth(other))
                for one in left
                for other in right
            )
        if node.op.is_arithmetic or node.op is BinaryOp.CONCAT:
            return tuple(
                value
                for one in left
                for other in right
                if (value := self.arithmetic(node, one, other)) is not None
            )
        return tuple(self.compare(node, one, other) for one in left for other in right)

    def arithmetic(self, node: Binary, left: Value, right: Value) -> Value | None:
        if node.op is BinaryOp.CONCAT:
            return f"{self.plain(left)}{self.plain(right)}"
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return None
        if isinstance(left, bool) or isinstance(right, bool):
            return None
        if node.op is BinaryOp.ADD:
            return left + right
        if node.op is BinaryOp.SUB:
            return left - right
        if node.op is BinaryOp.MUL:
            return left * right
        if node.op in (BinaryOp.DIV, BinaryOp.MOD) and right == 0:
            raise QueryError(
                "cannot divide by zero",
                text=self.text,
                offset=node.op_span.offset,
                length=node.op_span.length,
                help="guard the divisor, as in 'filter .size > 0'",
            )
        return left / right if node.op is BinaryOp.DIV else left % right

    def compare(self, node: Binary, left: Value, right: Value) -> bool:
        op = node.op
        if op is BinaryOp.EQ:
            return _equal(left, right)
        if op is BinaryOp.NE:
            return not _equal(left, right)
        if op in (BinaryOp.LT, BinaryOp.LE, BinaryOp.GT, BinaryOp.GE):
            return _ordered(op, left, right)
        subject = self.plain(left)
        pattern = self.plain(right)
        text, needle = str(subject), str(pattern)
        if op is BinaryOp.LIKE:
            return fnmatch.fnmatchcase(text, needle)
        if op is BinaryOp.NOT_LIKE:
            return not fnmatch.fnmatchcase(text, needle)
        if op is BinaryOp.ILIKE:
            return fnmatch.fnmatchcase(text.lower(), needle.lower())
        if op is BinaryOp.REGEX:
            return _regex(needle).search(text) is not None
        # ``under``: a path is under itself, and under every ancestor of it. The
        # empty namespace is the inventory root, which everything is under.
        return not needle or text == needle or text.startswith(f"{needle}/")

    def select(self, node: Select, scope: Ref | None) -> tuple[Value, ...]:
        values = list(self.evaluate(node.source, scope))
        for condition in node.filters:
            values = [
                one
                for one in values
                if any(
                    _truth(answer)
                    for answer in self.evaluate(condition, one if isinstance(one, Ref) else scope)
                )
            ]
        if node.order:
            values = self.sorted(values, node, scope)
        start = _bound(self.evaluate(node.offset, scope)) if node.offset is not None else 0
        values = values[start:]
        if node.limit is not None:
            values = values[: _bound(self.evaluate(node.limit, scope))]
        return tuple(values)

    def sorted(self, values: list[Value], node: Select, scope: Ref | None) -> list[Value]:
        """Sort by each key in turn, least significant first, so ties break right."""
        for item in reversed(node.order):
            values.sort(
                key=lambda one: _sort_key(
                    self.evaluate(item.expr, one if isinstance(one, Ref) else scope)
                ),
                reverse=item.descending,
            )
        return values


# --------------------------------------------------------------------------- #
# Value semantics
# --------------------------------------------------------------------------- #


def _truth(value: Value) -> bool:
    """What counts as true. Only a boolean does; anything else is a type error
    the parser already refused, or an empty set that never reaches here."""
    return value is True


def _equal(left: Value, right: Value) -> bool:
    if isinstance(left, Ref) or isinstance(right, Ref):
        return isinstance(left, Ref) and isinstance(right, Ref) and left.id == right.id
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return str(left) == str(right)


def _ordered(op: BinaryOp, left: Value, right: Value) -> bool:
    pair = _comparable(left, right)
    if pair is None:
        return False
    low, high = pair
    if op is BinaryOp.LT:
        return bool(low < high)
    if op is BinaryOp.LE:
        return bool(low <= high)
    if op is BinaryOp.GT:
        return bool(low > high)
    return bool(low >= high)


def _comparable(left: Value, right: Value) -> tuple[Any, Any] | None:
    """The two values in a form that orders, or ``None`` when they do not.

    Numbers order as numbers, addresses as addresses, and anything else as
    text — which is why ``<`` is refused on text at parse time rather than
    silently doing a lexicographic comparison somebody did not ask for.
    """
    if isinstance(left, Ref) or isinstance(right, Ref):
        return None
    if (
        isinstance(left, (int, float))
        and isinstance(right, (int, float))
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    ):
        return float(left), float(right)
    low, high = _address(str(left)), _address(str(right))
    if low is not None and high is not None and low.version == high.version:
        return low, high
    return str(left), str(right)


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _network(text: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    if "/" not in text:
        return None
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None


def _member(value: Value, among: Sequence[Value]) -> bool:
    """``value in among``: containment for a prefix, membership for anything else.

    One rule decided per candidate rather than per operand, so
    ``.ip in {'10.0.0.0/8', '192.168.0.1'}`` asks the sensible question of each.
    """
    for candidate in among:
        if isinstance(candidate, Ref) or isinstance(value, Ref):
            if _equal(value, candidate):
                return True
            continue
        prefix = _network(str(candidate))
        if prefix is not None and _inside(value, prefix):
            return True
        if _equal(value, candidate):
            return True
    return False


def _inside(value: Value, prefix: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    text = str(value)
    held = _network(text)
    if held is not None:
        return held.version == prefix.version and held.subnet_of(prefix)  # type: ignore[arg-type]
    address = _address(text)
    return address is not None and address.version == prefix.version and address in prefix


def _regex(pattern: str) -> re.Pattern[str]:
    compiled = _COMPILED.get(pattern)
    if compiled is None:
        if len(_COMPILED) >= _PATTERN_CACHE:
            _COMPILED.clear()
        compiled = _COMPILED[pattern] = re.compile(pattern)
    return compiled


def _sort_key(values: tuple[Value, ...]) -> tuple[int, int, float, str]:
    """A total order over values of any type, empty last.

    Four parts, because a sort key must never raise: whether there is a value at
    all, whether it is a number, the number, and the text. Numbers therefore
    sort before text, and two numbers sort numerically rather than by their
    spelling — ``eth2`` before ``eth10`` is a different problem and not one a
    generic comparator can solve.
    """
    if not values:
        return (1, 0, 0.0, "")
    value = values[0]
    if isinstance(value, bool):
        return (0, 1, float(value), "")
    if isinstance(value, (int, float)):
        return (0, 1, float(value), "")
    if isinstance(value, Ref):
        return (0, 2, 0.0, value.local)
    address = _address(str(value))
    if address is not None:
        return (0, 1, float(int(address)), "")
    return (0, 2, 0.0, str(value))


def _bound(values: tuple[Value, ...]) -> int:
    """``limit``/``offset`` as a non-negative integer; absent means no bound."""
    if not values:
        return 0
    first = values[0]
    return max(0, int(first)) if isinstance(first, (int, float)) else 0


# --------------------------------------------------------------------------- #
# Columns
# --------------------------------------------------------------------------- #


def _columns(query: Query) -> tuple[Column, ...]:
    """The table columns a shaped query produces; empty for a scalar one."""
    shape = query.shape
    if shape is None:
        return ()
    return tuple(
        Column(
            element.name,
            multi=element.is_multi,
            numeric=element.type.scalar is not None and element.type.scalar.is_numeric,
        )
        for element in shape.elements
    )
