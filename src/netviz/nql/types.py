"""The meta-model a relational query is checked against.

An NQL query is not checked against the *data*; it is checked against a
description of the data, and this module is the shape of that description. Four
things and no more:

* a :class:`ScalarKind` — the six kinds of leaf value a network fact can be,
* a :class:`Cardinality` — how many of something a step yields,
* a :class:`Property` and a :class:`Link` — a named step to a scalar, or to
  another object,
* an :class:`ObjectType`, which is a named bag of those two, plus the types it
  inherits from.

:mod:`netviz.nql.schema` fills the table in; :mod:`netviz.nql.parser` reads it
to reject ``.vlna`` before any inventory is loaded; :mod:`netviz.nql.world`
reads it to know what it is obliged to build. Keeping the three apart is what
makes "the query language is tightly coupled to the schema" a checkable claim
rather than a slogan: the coupling is one table, and a test can walk it.

Inheritance is single-rooted and shallow on purpose — ``server`` is a
``device`` is an ``element`` — because that is what ``docs/schema.md`` already
says. A query written against ``device`` therefore reads every kind of device,
and ``.parent[is server]`` narrows a polymorphic link back down without the
user having to know which concrete kinds exist.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Cardinality",
    "Link",
    "Member",
    "ObjectType",
    "Property",
    "ScalarKind",
    "Schema",
    "ValueType",
    "normalise_name",
]


def normalise_name(name: str) -> str:
    """The spelling a type or member is looked up under.

    Case is folded and ``-`` reads as ``_``, so ``broadcast-domain``,
    ``broadcast_domain`` and ``BroadcastDomain`` are one name. A schema written
    in YAML uses hyphens, Python uses underscores and a reader coming from
    EdgeQL or Cypher reaches for CamelCase; refusing two of the three would be
    pedantry about a spelling that carries no information.
    """
    return name.replace("-", "_").lower()


class ScalarKind(str, Enum):
    """What a leaf value is, which decides which operators apply to it."""

    #: Text. Compared with ``=``, globbed with ``~``, matched with ``=~``.
    STR = "str"
    #: A whole number: a VLAN id, an MTU, a rack unit, a port count.
    INT = "int"
    #: A real number: watts, metres, a utilisation ratio.
    FLOAT = "float"
    BOOL = "bool"
    #: A host address without its prefix length. ``in`` tests containment in a
    #: :attr:`CIDR` rather than membership of a set.
    IP = "ip"
    #: A network prefix, ``10.0.0.0/24``.
    CIDR = "cidr"

    def __str__(self) -> str:
        return self.value

    @property
    def orders(self) -> bool:
        """May ``<``, ``<=``, ``>`` and ``>=`` be written against this kind?

        Numbers and addresses order; text does not. Sorting text is a fine
        thing to ask for in ``order by``, which is why that clause does not go
        through here — but ``name < "sw"`` is nearly always a mistyped
        comparison rather than a lexicographic question, and saying so at parse
        time costs nothing.
        """
        return self in (ScalarKind.INT, ScalarKind.FLOAT, ScalarKind.IP)

    @property
    def is_numeric(self) -> bool:
        return self in (ScalarKind.INT, ScalarKind.FLOAT)


class Cardinality(str, Enum):
    """How many values a step yields, and therefore how it is rendered.

    This is the whole reason NQL can return *structured* results without being
    told to: a shape element over a :attr:`MANY` link becomes a JSON array, one
    over :attr:`ONE` or :attr:`OPTIONAL` becomes a scalar (``null`` when
    absent). Nobody writes ``array_agg``.
    """

    #: Exactly one. A device always has a name.
    ONE = "one"
    #: Zero or one. A device may have no vendor.
    OPTIONAL = "optional"
    #: Any number, including none.
    MANY = "many"

    def __str__(self) -> str:
        return self.value

    @property
    def is_multi(self) -> bool:
        """Does this render as an array?"""
        return self is Cardinality.MANY

    def then(self, other: Cardinality) -> Cardinality:
        """Cardinality of ``a.b`` given the cardinality of each step.

        One-to-one composes to one; anything reachable through a set is a set;
        an optional step anywhere makes the whole path optional.
        """
        if self is Cardinality.MANY or other is Cardinality.MANY:
            return Cardinality.MANY
        if self is Cardinality.OPTIONAL or other is Cardinality.OPTIONAL:
            return Cardinality.OPTIONAL
        return Cardinality.ONE


@dataclass(frozen=True, slots=True)
class Property:
    """A named step from an object to a scalar."""

    name: str
    type: ScalarKind
    card: Cardinality = Cardinality.OPTIONAL
    summary: str = ""
    #: Other spellings of this same step, e.g. ``ip`` for ``address``.
    aliases: tuple[str, ...] = ()

    @property
    def is_link(self) -> bool:
        return False

    def describe(self) -> str:
        """``vendor: optional str`` — the one line ``--describe`` prints."""
        spelling = "|".join((self.name, *self.aliases))
        return f"{spelling}: {self.card} {self.type}"


@dataclass(frozen=True, slots=True)
class Link:
    """A named step from an object to other objects.

    ``target`` names an :class:`ObjectType`, which may be abstract: an
    interface's ``parent`` is an ``element``, because a port belongs to a
    device, an adapter or a patch panel and a query should not have to care
    which until it asks.
    """

    name: str
    target: str
    card: Cardinality = Cardinality.MANY
    summary: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def is_link(self) -> bool:
        return True

    def describe(self) -> str:
        spelling = "|".join((self.name, *self.aliases))
        return f"{spelling}: {self.card} {self.target}"


#: Either kind of step. The parser dispatches on :attr:`Property.is_link`.
Member = Property | Link


@dataclass(frozen=True, slots=True)
class ValueType:
    """What an expression evaluates to, as far as the parser can tell.

    Either an object set (``object_type`` names the type) or a scalar set
    (``scalar`` names the kind), never both. :attr:`EMPTY` is neither: it is
    what ``{}`` and a failed inference produce, and it is compatible with
    everything so that one unknown does not cascade into a page of errors.
    """

    object_type: str = ""
    scalar: ScalarKind | None = None
    card: Cardinality = Cardinality.MANY

    @property
    def is_object(self) -> bool:
        return bool(self.object_type)

    @property
    def is_empty(self) -> bool:
        """Neither an object nor a scalar: nothing is known about it."""
        return not self.object_type and self.scalar is None

    def with_card(self, card: Cardinality) -> ValueType:
        return ValueType(object_type=self.object_type, scalar=self.scalar, card=card)

    def __str__(self) -> str:
        what = self.object_type or (str(self.scalar) if self.scalar else "empty")
        return f"{self.card} {what}"


@dataclass(frozen=True, slots=True)
class ObjectType:
    """One queryable kind of thing, and the steps out of it.

    ``bases`` names the types this one inherits every property and link from.
    Every type declared here may stand alone as the source of a ``select``.
    """

    name: str
    summary: str
    bases: tuple[str, ...] = ()
    abstract: bool = False
    properties: Mapping[str, Property] = field(default_factory=dict)
    links: Mapping[str, Link] = field(default_factory=dict)
    #: Other spellings that resolve to this type, e.g. ``ip`` for ``address``.
    aliases: tuple[str, ...] = ()


class Schema:
    """The type table, with inheritance resolved once at construction.

    Every lookup a parser makes is a dict hit: the members of a type are
    flattened into it when the schema is built, so ``server.name`` does not walk
    to ``element`` at parse time and a hot query does not pay for the hierarchy.
    """

    def __init__(self, types: Sequence[ObjectType]) -> None:
        self._types: dict[str, ObjectType] = {}
        self._aliases: dict[str, str] = {}
        self._members: dict[str, dict[str, Member]] = {}
        #: Nearest base first, so :meth:`common` finds the *nearest* shared type.
        self._ancestors: dict[str, tuple[str, ...]] = {}
        self._descendants: dict[str, tuple[str, ...]] = {}
        for one in types:
            self._add(one)
        self._flatten()

    def _add(self, one: ObjectType) -> None:
        key = normalise_name(one.name)
        if key in self._types:
            raise ValueError(f"duplicate object type {one.name!r}")
        for base in one.bases:
            if normalise_name(base) not in self._types:
                raise ValueError(f"{one.name!r} inherits from {base!r}, which is not declared yet")
        self._types[key] = one
        self._aliases[key] = key
        for alias in one.aliases:
            self._aliases[normalise_name(alias)] = key

    def _flatten(self) -> None:
        for key, one in self._types.items():
            ancestors: list[str] = []
            pending = [normalise_name(base) for base in one.bases]
            while pending:
                current = pending.pop(0)
                if current in ancestors:
                    continue
                ancestors.append(current)
                pending.extend(normalise_name(base) for base in self._types[current].bases)
            self._ancestors[key] = tuple(ancestors)
            # Inherited members first and in base order, so ``--describe`` reads
            # from the general to the particular, the way the schema document
            # introduces them.
            members: dict[str, Member] = {}
            for base in reversed(ancestors):
                members.update(self._own_members(self._types[base]))
            members.update(self._own_members(one))
            self._members[key] = members
        for key in self._types:
            self._descendants[key] = tuple(
                name
                for name, one in self._types.items()
                if not one.abstract and (name == key or key in self._ancestors[name])
            )

    @staticmethod
    def _own_members(one: ObjectType) -> dict[str, Member]:
        """This type's own steps, keyed by every spelling that reaches them.

        An alias is a second key onto the *same* object, not a copy, so
        :meth:`properties` can drop the duplicates by identity and ``*`` expands
        to one column per step rather than one per spelling.
        """
        members: dict[str, Member] = {}
        declared: list[Member] = [*one.properties.values(), *one.links.values()]
        for member in declared:
            members[normalise_name(member.name)] = member
            for alias in member.aliases:
                members[normalise_name(alias)] = member
        return members

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #

    def resolve(self, name: str) -> ObjectType | None:
        """The type ``name`` spells, following aliases, or ``None``."""
        key = self._aliases.get(normalise_name(name))
        return self._types[key] if key is not None else None

    def canonical(self, name: str) -> str:
        """``name`` as the schema spells it, or as written when unknown."""
        one = self.resolve(name)
        return one.name if one is not None else name

    def member(self, type_name: str, member: str) -> Member | None:
        """The property or link ``member`` names on ``type_name``."""
        one = self.resolve(type_name)
        if one is None:
            return None
        return self._members[normalise_name(one.name)].get(normalise_name(member))

    def members(self, type_name: str) -> Mapping[str, Member]:
        """Every member of ``type_name``, inherited ones first."""
        one = self.resolve(type_name)
        return {} if one is None else self._members[normalise_name(one.name)]

    def properties(self, type_name: str) -> tuple[Property, ...]:
        """Every scalar member once, in declaration order. What ``*`` expands to."""
        return tuple(
            one
            for one in dict.fromkeys(self.members(type_name).values())
            if isinstance(one, Property)
        )

    def links(self, type_name: str) -> tuple[Link, ...]:
        """Every link member once, in declaration order."""
        return tuple(
            one for one in dict.fromkeys(self.members(type_name).values()) if isinstance(one, Link)
        )

    def is_subtype(self, name: str, of: str) -> bool:
        """Is every ``name`` also an ``of``? True when they are the same type."""
        lower, upper = self.resolve(name), self.resolve(of)
        if lower is None or upper is None:
            return False
        key, target = normalise_name(lower.name), normalise_name(upper.name)
        return key == target or target in self._ancestors[key]

    def concrete(self, name: str) -> tuple[str, ...]:
        """The concrete types a value of ``name`` may actually be."""
        one = self.resolve(name)
        return () if one is None else self._descendants[normalise_name(one.name)]

    def common(self, left: str, right: str) -> str:
        """The nearest type both are: for the two branches of a union.

        Falls back to ``element`` and then to the empty string rather than
        raising, because a union of a scalar and an object is a different error
        and the caller reports that one.
        """
        if self.is_subtype(left, right):
            return self.canonical(right)
        if self.is_subtype(right, left):
            return self.canonical(left)
        for candidate in self._ancestors.get(normalise_name(left), ()):
            if self.is_subtype(right, candidate):
                return self.canonical(candidate)
        return ""

    def __iter__(self) -> Iterator[ObjectType]:
        return iter(self._types.values())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.resolve(name) is not None

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def suggest_type(self, name: str) -> tuple[str, ...]:
        """Type names close to ``name``, for a "did you mean" line."""
        return tuple(
            difflib.get_close_matches(normalise_name(name), sorted(self._aliases), n=3, cutoff=0.6)
        )

    def suggest_member(self, type_name: str, member: str) -> tuple[str, ...]:
        """Members of ``type_name`` close to ``member``."""
        names = sorted(self.members(type_name))
        return tuple(difflib.get_close_matches(normalise_name(member), names, n=3, cutoff=0.6))
