"""Who is in what: group membership, resolved once for everything that reads it.

Four consumers ask the same question about a ``group``'s ``spec.members`` and
must not be able to answer it differently:

* the validator (``NV-S010``…``NV-S016``),
* the identity view of the graph (``--layer identity``),
* ``netviz list users`` and ``netviz list groups``,
* anything that later exports the membership to a directory.

A finding about a membership and a diagram of that membership disagreeing would
be the one bug this layer exists to make impossible, so resolution happens in
exactly one place — here — and everything else consumes :class:`IdentityPlan`.
That is the same arrangement :mod:`netviz.power` has for outlet feeds, for the
same reason.

Expansion
---------

Nesting is the point of groups, so the interesting question is not "who is
directly in this?" but "who is in this at all?". :meth:`IdentityPlan.expand`
answers it by walking the nesting, and does so **cycle-safely**: a loop is
``NV-S012``, an error, but a listing has to survive being run on an inventory
that has one. A group in a cycle expands to everything reachable from it and
stops, rather than not terminating.

Order is the order of discovery — the group's own members first, then what each
nested group contributes — because that is the order the documents are written
in and the one a reader can follow back to a file.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from netviz.loader.inventory import Inventory, namespace_of
from netviz.models import GROUP_KIND, USER_KIND, Group, User

__all__ = [
    "IDENTITY_KINDS",
    "IdentityPlan",
    "Membership",
    "identities",
    "identity_plan",
]

#: The two kinds an identity graph is made of (§19).
IDENTITY_KINDS: Final[frozenset[str]] = frozenset({USER_KIND, GROUP_KIND})


@dataclass(frozen=True, slots=True)
class Membership:
    """One entry of one group's ``spec.members``, resolved against the tree.

    Carries the position it was written at as well as what it resolved to,
    because both halves have consumers: a diagnostic and a reference rewrite need
    ``spec.members[2]`` to be findable in the file, and a diagram needs the fqn.
    """

    #: Fully-qualified name of the group holding the entry.
    group: str
    #: Position in ``spec.members``.
    index: int
    #: The reference exactly as written, e.g. ``alice`` or ``people/alice``.
    ref: str
    #: What it resolved to, or ``None`` when it resolved to nothing or to
    #: several things.
    member: str | None = None
    #: ``kind`` of the resolved element; ``""`` when nothing resolved.
    kind: str = ""
    #: Every candidate, when the name stayed ambiguous (§2.2).
    ambiguous: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.member is not None

    @property
    def is_identity(self) -> bool:
        """Did it resolve to something that may be a member at all?"""
        return self.kind in IDENTITY_KINDS

    @property
    def is_group(self) -> bool:
        """Did it resolve to a nested group?"""
        return self.kind == GROUP_KIND

    @property
    def field_path(self) -> tuple[str | int, ...]:
        return ("spec", "members", self.index)

    def __str__(self) -> str:
        return f"{self.group}[{self.index}] -> {self.ref}"


@dataclass(frozen=True, slots=True)
class IdentityPlan:
    """Every membership of an inventory, and the indexes derived from them."""

    #: Every entry of every group, in load then declaration order.
    memberships: tuple[Membership, ...] = ()
    #: Group fqn -> the identities directly in it, in declaration order. Only
    #: entries that resolved to a ``user`` or a ``group`` appear; the rest are
    #: ``NV-S010``'s and ``NV-S011``'s business, and a view that drew them would
    #: be drawing something that is not there.
    direct: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Identity fqn -> the groups that name it directly, in load order. The
    #: reverse index, which is what "which groups is this person in?" needs and
    #: what no document holds.
    holders: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Identity fqn -> its ``kind``, for every user and group in the tree. Kept
    #: so an expansion can be filtered without the inventory being passed around
    #: beside the plan.
    kinds: Mapping[str, str] = field(default_factory=dict)

    def members_of(self, group: str) -> tuple[str, ...]:
        """The identities directly in ``group``."""
        return self.direct.get(group, ())

    def groups_of(self, fqn: str) -> tuple[str, ...]:
        """The groups that directly name ``fqn``."""
        return self.holders.get(fqn, ())

    def expand(self, group: str) -> tuple[str, ...]:
        """Every identity in ``group``, nested groups walked through.

        The nested groups themselves are included, because "what is in this
        group" is a question about both — a reader auditing ``everyone`` needs to
        see that it holds ``engineering`` as well as who that brings.

        Cycle-safe: see the module docstring.
        """
        seen: set[str] = set()
        order: list[str] = []
        queue = [group]
        while queue:
            current = queue.pop(0)
            for member in self.direct.get(current, ()):
                if member in seen or member == group:
                    continue
                seen.add(member)
                order.append(member)
                queue.append(member)
        return tuple(order)

    def users_in(self, group: str) -> tuple[str, ...]:
        """Every ``user`` in ``group``, nested groups walked through.

        The headcount an access rule actually grants to, which is the number
        neither the group's own document nor any one of its members holds.
        """
        return tuple(fqn for fqn in self.expand(group) if self.kinds.get(fqn) == USER_KIND)

    def cycles(self) -> list[list[str]]:
        """The loops in the ``group -> nested group`` graph, in load order.

        A depth-first search with the usual three-colour marking, reporting one
        loop per tangle rather than the factorial number of rotations a naive
        enumeration would produce. Unlike an adapter's attachment a group has any
        number of parents *and* any number of children, so the functional walk
        that finds an attachment cycle does not apply here.
        """
        nesting = self._nesting
        unvisited, active, done = 0, 1, 2
        state: dict[str, int] = {}
        cycles: list[list[str]] = []
        seen: set[frozenset[str]] = set()

        def walk(node: str, path: list[str]) -> None:
            state[node] = active
            path.append(node)
            for nested in nesting.get(node, ()):
                colour = state.get(nested, unvisited)
                if colour == unvisited:
                    walk(nested, path)
                elif colour == active:
                    cycle = path[path.index(nested) :]
                    if frozenset(cycle) not in seen:
                        seen.add(frozenset(cycle))
                        cycles.append(list(cycle))
            path.pop()
            state[node] = done

        for start in nesting:
            if state.get(start, unvisited) == unvisited:
                walk(start, [])
        return cycles

    @property
    def _nesting(self) -> Mapping[str, Sequence[str]]:
        """Group fqn -> the groups directly inside it."""
        nesting: dict[str, list[str]] = {}
        for entry in self.memberships:
            if entry.is_group and entry.member is not None:
                nesting.setdefault(entry.group, []).append(entry.member)
        return nesting

    def __iter__(self) -> Iterator[Membership]:
        return iter(self.memberships)

    def __len__(self) -> int:
        return len(self.memberships)


def identity_plan(inventory: Inventory) -> IdentityPlan:
    """Resolve every group membership of ``inventory``.

    References resolve the way every other reference in the schema does
    (:meth:`~netviz.loader.Inventory.lookup`): outwards from the group's own
    namespace, then by short name across the tree when exactly one element
    carries it.

    Returns:
        An empty plan for a tree with no groups, without touching anything else.
    """
    if not inventory.groups:
        return IdentityPlan()

    memberships: list[Membership] = []
    direct: dict[str, list[str]] = {}
    holders: dict[str, list[str]] = {}

    for group_fqn, group in inventory.groups.items():
        namespace = namespace_of(group_fqn)
        for index, ref in enumerate(group.spec.members):
            resolution = inventory.lookup(ref, namespace=namespace)
            element = resolution.element
            entry = Membership(
                group=group_fqn,
                index=index,
                ref=ref,
                member=resolution.fqn,
                kind=element.kind if element is not None else "",
                ambiguous=resolution.ambiguous,
            )
            memberships.append(entry)
            if entry.is_identity and entry.member is not None:
                direct.setdefault(group_fqn, []).append(entry.member)
                holders.setdefault(entry.member, []).append(group_fqn)

    return IdentityPlan(
        memberships=tuple(memberships),
        direct={key: tuple(value) for key, value in direct.items()},
        holders={key: tuple(value) for key, value in holders.items()},
        kinds={fqn: element.kind for fqn, element in identities(inventory)},
    )


def identities(inventory: Inventory) -> Iterator[tuple[str, User | Group]]:
    """Every ``user`` and ``group`` of the inventory, in load order.

    Iterates the element map rather than chaining the two kind maps, so the order
    is the tree's rather than users-then-groups — which is what keeps a listing
    and a diagram of the same inventory in the same sequence.
    """
    for fqn, element in inventory.elements.items():
        if isinstance(element, (User, Group)):
            yield fqn, element
