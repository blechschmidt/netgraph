"""The built-in functions, each declared once for the checker and the executor.

A function is three things kept together on purpose: what it accepts, what it
returns, and what it does. Splitting them across modules is how a language ends
up accepting ``avg(.name)`` at parse time and failing at run time.

There are three shapes and no more.

**Elementwise** — ``lower``, ``contains``, ``starts_with``. Applied to the
cartesian product of its arguments, the way EdgeQL applies an operator to sets:
``starts_with(.interfaces.name, 'ge-')`` yields one boolean per interface, which
is what ``filter`` then reads existentially.

**Aggregate** — ``count``, ``sum``, ``min``, ``max``, ``avg``, ``any``, ``all``.
Collapses its argument's whole set to one value. This is the only way a query
turns many into one, and it is what makes ``{ ports := count(.interfaces) }`` a
number rather than a list.

**Graph** — ``neighbors``, ``reachable``. The two questions that are not
answerable by following named links a bounded number of times, and the only
place the language does an unbounded walk. Both are over *elements*: a query
that wants ports walks to them afterwards with ``.interfaces``.
"""

from __future__ import annotations

import fnmatch
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

from netviz.nql.types import Cardinality, ScalarKind, ValueType
from netviz.nql.world import Ref, Value, World

__all__ = ["FUNCTIONS", "Function", "Shape"]


class Shape(str, Enum):
    """How a function consumes its arguments."""

    ELEMENTWISE = "elementwise"
    AGGREGATE = "aggregate"
    GRAPH = "graph"

    def __str__(self) -> str:
        return self.value


class Accepts(str, Enum):
    """What one parameter will take. Checked at parse time against the schema."""

    ANY = "any"
    SCALAR = "scalar"
    NUMBER = "number"
    TEXT = "text"
    BOOL = "bool"
    OBJECT = "object"
    INT = "int"

    def __str__(self) -> str:
        return self.value

    def rejects(self, given: ValueType) -> str:
        """Why ``given`` is not acceptable here, or ``""`` when it is.

        An expression whose type could not be inferred is accepted by
        everything: one unknown should produce one diagnostic, not a cascade of
        them from every function it flows into.
        """
        if given.is_empty:
            return ""
        if self is Accepts.ANY:
            return ""
        if self is Accepts.OBJECT:
            return "" if given.is_object else "an object, not a scalar"
        if given.is_object:
            return f"a scalar, not {given.object_type} objects"
        scalar = given.scalar
        if self is Accepts.SCALAR:
            return ""
        if self is Accepts.NUMBER:
            return "" if scalar is not None and scalar.is_numeric else "a number"
        if self is Accepts.TEXT:
            return "" if scalar is ScalarKind.STR else "text"
        if self is Accepts.BOOL:
            return "" if scalar is ScalarKind.BOOL else "a boolean"
        return "" if scalar is ScalarKind.INT else "a whole number"


@dataclass(frozen=True, slots=True)
class Function:
    """One built-in: its signature, its result type and its implementation."""

    name: str
    shape: Shape
    #: One entry per parameter. Trailing ones may be omitted down to
    #: :attr:`required`.
    accepts: tuple[Accepts, ...]
    required: int
    summary: str
    #: What it returns, given what it was handed.
    infer: Callable[[Sequence[ValueType]], ValueType]
    apply: Callable[[World, Sequence[tuple[Value, ...]]], tuple[Value, ...]]

    def signature(self) -> str:
        """``count(any) -> one int`` — the line ``--explain`` prints."""
        params = [
            str(one) if index < self.required else f"[{one}]"
            for index, one in enumerate(self.accepts)
        ]
        return f"{self.name}({', '.join(params)})"


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


def _one(scalar: ScalarKind) -> Callable[[Sequence[ValueType]], ValueType]:
    return lambda _given: ValueType(scalar=scalar, card=Cardinality.ONE)


def _optional(scalar: ScalarKind) -> Callable[[Sequence[ValueType]], ValueType]:
    return lambda _given: ValueType(scalar=scalar, card=Cardinality.OPTIONAL)


def _same_scalar_optional(given: Sequence[ValueType]) -> ValueType:
    """``min``/``max`` return whatever they were given, or nothing."""
    scalar = given[0].scalar if given and given[0].scalar else ScalarKind.STR
    return ValueType(scalar=scalar, card=Cardinality.OPTIONAL)


def _numeric_total(given: Sequence[ValueType]) -> ValueType:
    """``sum`` of integers is an integer; of anything else, a real number."""
    scalar = ScalarKind.INT if given and given[0].scalar is ScalarKind.INT else ScalarKind.FLOAT
    return ValueType(scalar=scalar, card=Cardinality.ONE)


def _elementwise(scalar: ScalarKind) -> Callable[[Sequence[ValueType]], ValueType]:
    """A per-value function keeps the cardinality of its widest argument."""

    def infer(given: Sequence[ValueType]) -> ValueType:
        card = Cardinality.ONE
        for one in given:
            card = card.then(one.card)
        return ValueType(scalar=scalar, card=card)

    return infer


def _elements(_given: Sequence[ValueType]) -> ValueType:
    return ValueType(object_type="element", card=Cardinality.MANY)


# --------------------------------------------------------------------------- #
# Implementations
# --------------------------------------------------------------------------- #


def _cartesian(
    args: Sequence[tuple[Value, ...]], apply: Callable[[tuple[Value, ...]], Value | None]
) -> tuple[Value, ...]:
    """Apply ``apply`` to every combination of the arguments' values.

    Empty in, empty out: a function of nothing is nothing, which is what makes
    ``lower(.vendor)`` on a device with no vendor contribute no row rather than
    an empty string.
    """
    found: list[Value] = []
    for combination in itertools.product(*args):
        result = apply(combination)
        if result is not None:
            found.append(result)
    return tuple(found)


def _numbers(values: Sequence[Value]) -> list[float]:
    return [
        float(one) for one in values if isinstance(one, (int, float)) and not isinstance(one, bool)
    ]


def _count(_world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
    return (len(args[0]),)


def _sum(_world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
    total = float(sum(_numbers(args[0])))
    return (int(total) if total.is_integer() else total,)


def _avg(_world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
    numbers = _numbers(args[0])
    return (sum(numbers) / len(numbers),) if numbers else ()


def _extreme(
    pick: Callable[..., Value],
) -> Callable[[World, Sequence[tuple[Value, ...]]], tuple[Value, ...]]:
    def apply(_world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
        values = [one for one in args[0] if not isinstance(one, Ref)]
        if not values:
            return ()
        if all(isinstance(one, (int, float)) and not isinstance(one, bool) for one in values):
            return (pick(values),)
        return (pick(values, key=str),)

    return apply


def _any(_world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
    return (any(one is True for one in args[0]),)


def _all(_world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
    return (all(one is True for one in args[0]),)


def _text(value: Value, world: World) -> str:
    """One value as text, which is what every string function reads."""
    if isinstance(value, Ref):
        return world.display(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _string_function(
    apply: Callable[[str, str], Value],
) -> Callable[[World, Sequence[tuple[Value, ...]]], tuple[Value, ...]]:
    def run(world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
        return _cartesian(args, lambda pair: apply(_text(pair[0], world), _text(pair[1], world)))

    return run


def _unary_string(
    apply: Callable[[str], Value],
) -> Callable[[World, Sequence[tuple[Value, ...]]], tuple[Value, ...]]:
    def run(world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
        return _cartesian(args, lambda one: apply(_text(one[0], world)))

    return run


def _neighbors(world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
    hops = 1
    if len(args) > 1 and args[1]:
        first = args[1][0]
        hops = int(first) if isinstance(first, (int, float)) and not isinstance(first, bool) else 1
    seeds = [one for one in args[0] if isinstance(one, Ref)]
    return tuple(world.neighbors(seeds, hops))


def _reachable(world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
    return tuple(world.reachable([one for one in args[0] if isinstance(one, Ref)]))


def _label_of(key: str, values: Sequence[Value]) -> tuple[Value, ...]:
    """The values of the ``key=value`` entries whose key is ``key``."""
    found: list[Value] = []
    for one in values:
        name, separator, value = str(one).partition("=")
        if separator and name == key:
            found.append(value)
    return tuple(found)


def _lookup(world: World, args: Sequence[tuple[Value, ...]]) -> tuple[Value, ...]:
    keys = [_text(one, world) for one in args[1]]
    return tuple(value for key in keys for value in _label_of(key, args[0]))


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


def _function(
    name: str,
    shape: Shape,
    accepts: tuple[Accepts, ...],
    required: int,
    summary: str,
    infer: Callable[[Sequence[ValueType]], ValueType],
    apply: Callable[[World, Sequence[tuple[Value, ...]]], tuple[Value, ...]],
) -> Function:
    return Function(name, shape, accepts, required, summary, infer, apply)


FUNCTIONS: Final[Mapping[str, Function]] = {
    one.name: one
    for one in (
        _function(
            "count",
            Shape.AGGREGATE,
            (Accepts.ANY,),
            1,
            "How many values the set holds.",
            _one(ScalarKind.INT),
            _count,
        ),
        _function(
            "sum",
            Shape.AGGREGATE,
            (Accepts.NUMBER,),
            1,
            "The total. Zero for an empty set.",
            _numeric_total,
            _sum,
        ),
        _function(
            "min",
            Shape.AGGREGATE,
            (Accepts.SCALAR,),
            1,
            "The smallest value, or nothing when the set is empty.",
            _same_scalar_optional,
            _extreme(min),
        ),
        _function(
            "max",
            Shape.AGGREGATE,
            (Accepts.SCALAR,),
            1,
            "The largest value, or nothing when the set is empty.",
            _same_scalar_optional,
            _extreme(max),
        ),
        _function(
            "avg",
            Shape.AGGREGATE,
            (Accepts.NUMBER,),
            1,
            "The mean, or nothing when the set is empty.",
            _optional(ScalarKind.FLOAT),
            _avg,
        ),
        _function(
            "any",
            Shape.AGGREGATE,
            (Accepts.BOOL,),
            1,
            "Is at least one of them true?",
            _one(ScalarKind.BOOL),
            _any,
        ),
        _function(
            "all",
            Shape.AGGREGATE,
            (Accepts.BOOL,),
            1,
            "Are all of them true? True for an empty set.",
            _one(ScalarKind.BOOL),
            _all,
        ),
        _function(
            "len",
            Shape.ELEMENTWISE,
            (Accepts.TEXT,),
            1,
            "How many characters the text has.",
            _elementwise(ScalarKind.INT),
            _unary_string(len),
        ),
        _function(
            "lower",
            Shape.ELEMENTWISE,
            (Accepts.TEXT,),
            1,
            "The text in lower case.",
            _elementwise(ScalarKind.STR),
            _unary_string(str.lower),
        ),
        _function(
            "upper",
            Shape.ELEMENTWISE,
            (Accepts.TEXT,),
            1,
            "The text in upper case.",
            _elementwise(ScalarKind.STR),
            _unary_string(str.upper),
        ),
        _function(
            "text",
            Shape.ELEMENTWISE,
            (Accepts.ANY,),
            1,
            "Any value as text; an object becomes the name a table would print.",
            _elementwise(ScalarKind.STR),
            _unary_string(str),
        ),
        _function(
            "contains",
            Shape.ELEMENTWISE,
            (Accepts.ANY, Accepts.TEXT),
            2,
            "Does the first hold the second as a substring?",
            _elementwise(ScalarKind.BOOL),
            _string_function(lambda haystack, needle: needle in haystack),
        ),
        _function(
            "starts_with",
            Shape.ELEMENTWISE,
            (Accepts.ANY, Accepts.TEXT),
            2,
            "Does the first begin with the second?",
            _elementwise(ScalarKind.BOOL),
            _string_function(str.startswith),
        ),
        _function(
            "ends_with",
            Shape.ELEMENTWISE,
            (Accepts.ANY, Accepts.TEXT),
            2,
            "Does the first end with the second?",
            _elementwise(ScalarKind.BOOL),
            _string_function(str.endswith),
        ),
        _function(
            "matches",
            Shape.ELEMENTWISE,
            (Accepts.ANY, Accepts.TEXT),
            2,
            "Does the first match the glob the second spells?",
            _elementwise(ScalarKind.BOOL),
            _string_function(lambda subject, pattern: fnmatch.fnmatchcase(subject, pattern)),
        ),
        _function(
            "lookup",
            Shape.ELEMENTWISE,
            (Accepts.TEXT, Accepts.TEXT),
            2,
            "The value of one `key=value` entry of `.labels` or `.annotations`.",
            _elementwise(ScalarKind.STR),
            _lookup,
        ),
        _function(
            "neighbors",
            Shape.GRAPH,
            (Accepts.OBJECT, Accepts.INT),
            1,
            "Every element at most N links away, the seeds included. N defaults to 1.",
            _elements,
            _neighbors,
        ),
        _function(
            "reachable",
            Shape.GRAPH,
            (Accepts.OBJECT,),
            1,
            "The whole connected component of every seed.",
            _elements,
            _reachable,
        ),
    )
}
