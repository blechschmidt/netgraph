"""Running a parsed query against a resolved graph.

The evaluator is a fold over the tree in :mod:`netgraph.query.ast`, and it is
written *set-at-a-time* rather than node-at-a-time: every expression evaluates
to the set of nodes satisfying it, so a traversal — which is a question about
the whole graph and not about one node — is an operation on that set rather than
a special case threaded through a per-node predicate.

That choice is what makes the negation law hold. ``not X`` is the complement of
``X`` **within the universe the query is evaluated over**, which is every node
of the graph, so ``X`` and ``not X`` partition it exactly. There is no third
answer for a node that lacks the attribute: it simply is not in ``X``, and is
therefore in ``not X``. ``tests/test_query.py`` checks this against generated
inventories rather than trusting the paragraph.

Witnesses
---------

``interface[address in 10.20.0.0/16 and not has vrf]`` selects elements, but the
question was about interfaces. So a scope that is satisfied records *which*
sub-objects satisfied it, and :class:`QueryResult` carries those alongside the
nodes. Only at positive polarity: under an odd number of ``not`` s a scope's
being true is what makes the surrounding term false, and reporting the
interfaces that caused an element to be excluded as if they had been selected
would be a lie. ``netgraph query --interfaces`` prints exactly the positive
witnesses, which is why that flag answers the question the query was asked in.

Cost
----

One pass per term over the nodes, plus one breadth-first search per traversal,
bounded by the graph. Attribute reads go through :class:`~netgraph.query.facts.
Facts`, which indexes the edge list once. Nothing here is memoised across calls,
because the graph a query runs against changes on every edit — and nothing needs
to be: a thousand-device inventory answers a ten-term query in single-digit
milliseconds.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from netgraph.models import Device
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
    Traversal,
    TraversalKind,
)
from netgraph.query.attributes import Attribute, Domain, ValueType, lookup
from netgraph.query.errors import QueryError
from netgraph.query.facts import (
    Facts,
    LinkEnd,
    element_values,
    interface_values,
    link_values,
    netns_values,
    ports_of,
    zone_values,
)
from netgraph.query.parser import Query
from netgraph.render.graph import (
    Graph,
    NetnsView,
    Node,
    PortView,
    SecurityView,
    netns_views,
    security_views,
)

__all__ = ["QueryResult", "Witness", "evaluate", "matches"]

#: How many compiled regular expressions are kept. A query holds at most
#: :data:`~netgraph.query.parser.MAX_TERMS` of them and the editor re-evaluates
#: the same one on every keystroke, so a small cache removes the compile from
#: the inner loop without letting an adversarial workload grow it.
_PATTERN_CACHE: Final[dict[str, re.Pattern[str]]] = {}
_PATTERN_CACHE_LIMIT: Final = 512


@dataclass(frozen=True, slots=True)
class Witness:
    """One sub-object that satisfied a scope, and where it was.

    ``element`` is the fully-qualified name of the node it belongs to and
    ``name`` is what the sub-object is called within it — an interface name, a
    link id, a namespace, a zone.
    """

    domain: str
    element: str
    name: str

    def __str__(self) -> str:
        return f"{self.element}:{self.name}"


@dataclass(frozen=True, slots=True)
class QueryResult:
    """What a query selected, in graph order.

    Graph order and not sorted order: the graph is built in load order, which is
    the order every other netgraph listing uses, and a result that reordered
    itself would not line up with ``netgraph list`` beside it.
    """

    query: Query
    #: The fully-qualified names of the matching nodes.
    nodes: tuple[str, ...] = ()
    #: The sub-objects that satisfied a scope at positive polarity, deduplicated
    #: and in graph order. Empty when the query has no scope in it.
    witnesses: tuple[Witness, ...] = ()

    def __len__(self) -> int:
        return len(self.nodes)

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def __contains__(self, fqn: object) -> bool:
        return fqn in set(self.nodes)

    @property
    def selected(self) -> frozenset[str]:
        """The matches as a set, which is what a filter is layered with."""
        return frozenset(self.nodes)

    def interfaces(self) -> tuple[Witness, ...]:
        """The interface witnesses alone, for ``netgraph query --interfaces``."""
        return tuple(one for one in self.witnesses if one.domain == Domain.INTERFACE.value)


def evaluate(query: Query, graph: Graph) -> QueryResult:
    """Answer ``query`` against ``graph``.

    Args:
        query: A query already parsed and checked by
            :func:`~netgraph.query.parser.parse`.
        graph: The resolved graph to evaluate over. The universe is *every* node
            it holds, derived nodes included, so a query run on a layer-3 view
            can select a subnet and one run on a filtered graph sees only what
            survived the filter.

    Returns:
        The matching nodes in graph order, plus any scope witnesses.

    Raises:
        QueryError: Never for a syntactic reason — the parser has already
            settled those — but the signature is kept open because an attribute
            whose value cannot be compared the way the operator asks is a
            possibility the type system does not rule out.
    """
    run = _Run(query, graph)
    selected = run.solve(query.expr, universe=run.universe, polarity=True)
    order = {fqn: index for index, fqn in enumerate(graph.nodes)}
    nodes = tuple(sorted(selected, key=lambda fqn: order.get(fqn, len(order))))
    return QueryResult(query=query, nodes=nodes, witnesses=run.harvest(nodes))


def matches(query: Query, graph: Graph) -> frozenset[str]:
    """The matching fully-qualified names alone, for a caller that wants a set."""
    return evaluate(query, graph).selected


@dataclass
class _Run:
    """One evaluation. Holds the graph index and the witnesses collected so far."""

    query: Query
    graph: Graph
    facts: Facts = field(init=False)
    #: Every fqn, as the universe a complement is taken within.
    universe: frozenset[str] = field(init=False)
    #: element fqn -> the witnesses recorded for it, in the order recorded.
    found: dict[str, list[Witness]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.facts = Facts.of(self.graph)
        self.universe = frozenset(self.graph.nodes)

    def harvest(self, nodes: Sequence[str]) -> tuple[Witness, ...]:
        """The witnesses belonging to nodes that survived, deduplicated.

        A scope may be satisfied by an element the rest of the query then
        rejects — ``interface[address in 10.0.0.0/8] and kind = router`` records
        an interface on every switch it looked at — so the harvest is filtered
        by the final answer rather than reported as it was collected.
        """
        seen: set[Witness] = set()
        harvested: list[Witness] = []
        for fqn in nodes:
            for one in self.found.get(fqn, ()):
                if one not in seen:
                    seen.add(one)
                    harvested.append(one)
        return tuple(harvested)

    def record(self, witness: Witness) -> None:
        self.found.setdefault(witness.element, []).append(witness)

    # ----------------------------------------------------------- the fold

    def solve(self, expr: Expr, *, universe: frozenset[str], polarity: bool) -> frozenset[str]:
        """The nodes of ``universe`` satisfying ``expr``.

        ``polarity`` is False under an odd number of ``not`` s and controls only
        whether witnesses are recorded; the answer itself does not depend on it.
        """
        if isinstance(expr, All):
            return universe
        if isinstance(expr, Not):
            return universe - self.solve(expr.body, universe=universe, polarity=not polarity)
        if isinstance(expr, And):
            kept = universe
            for operand in expr.operands:
                kept = self.solve(operand, universe=kept, polarity=polarity)
                if not kept:
                    # Nothing left to narrow. Stopping here is not only cheaper:
                    # it also stops the remaining operands recording witnesses
                    # for a conjunction that has already failed.
                    return kept
            return kept
        if isinstance(expr, Or):
            found: frozenset[str] = frozenset()
            for operand in expr.operands:
                found |= self.solve(operand, universe=universe, polarity=polarity)
            return found
        if isinstance(expr, Traversal):
            return universe & self.traverse(expr, polarity=polarity)
        if isinstance(expr, Scope):
            return frozenset(
                fqn
                for fqn in universe
                if self.scope_holds(expr, self.graph.nodes[fqn], polarity=polarity)
            )
        if isinstance(expr, Exists):
            return frozenset(fqn for fqn in universe if self.exists(expr, self.graph.nodes[fqn]))
        return frozenset(fqn for fqn in universe if self.compare(expr, self.graph.nodes[fqn]))

    # ------------------------------------------------------- the traversal

    def traverse(self, expr: Traversal, *, polarity: bool) -> frozenset[str]:
        """The neighbourhood of whatever the traversal's body selects.

        The body is solved against the **whole** graph rather than against the
        universe it is being intersected into, for the same reason
        ``--neighbors-of`` traverses the whole graph: a switch two hops away is
        still two hops away when the other half of the query would have excluded
        the node in between.
        """
        seeds = self.solve(expr.body, universe=self.universe, polarity=polarity)
        if not seeds:
            return frozenset()
        reached = self.spread(seeds, hops=expr.hops)
        if expr.kind is TraversalKind.NEIGHBORS:
            return reached - seeds
        return reached

    def spread(self, seeds: Iterable[str], *, hops: int | None) -> frozenset[str]:
        """Breadth-first from every seed, at most ``hops`` deep; ``None`` is all.

        Terminates because each node is enqueued at most once and the graph is
        finite. That is the whole of the language's unboundedness budget.
        """
        seen = set(seeds)
        queue: deque[tuple[str, int]] = deque((fqn, 0) for fqn in seen)
        while queue:
            fqn, distance = queue.popleft()
            if hops is not None and distance >= hops:
                continue
            for near in self.facts.adjacent.get(fqn, ()):
                if near not in seen:
                    seen.add(near)
                    queue.append((near, distance + 1))
        return frozenset(seen)

    # ----------------------------------------------------------- the scopes

    def scope_holds(self, expr: Scope, node: Node, *, polarity: bool) -> bool:
        """Does some sub-object of ``node`` satisfy the scope's body?

        Every sub-object is tested even after one has matched, so that
        ``--interfaces`` reports all the addresses in a prefix rather than the
        first. The bodies are tiny and the collections short; the extra work is
        not measurable beside building the graph.
        """
        domain = Domain(expr.domain)
        held = False
        for name, subject in self.subjects(domain, node):
            if not self.holds(expr.body, domain, subject, node):
                continue
            held = True
            if polarity:
                self.record(Witness(domain.value, node.fqn, name))
        return held

    def subjects(self, domain: Domain, node: Node) -> tuple[tuple[str, object], ...]:
        """The sub-objects of ``node`` in ``domain``, each with its name."""
        if domain is Domain.INTERFACE:
            return tuple((port.name, port) for port in ports_of(node))
        if domain is Domain.LINK:
            return tuple((end.edge.id, end) for end in self.facts.links_of(node))
        if domain is Domain.NETNS:
            return tuple((view.name, view) for view in _namespaces(node))
        if domain is Domain.ZONE:
            return tuple((view.name, view) for view in _zones(node))
        return ()  # pragma: no cover - Domain.ELEMENT is refused by the parser

    def holds(self, expr: Expr, domain: Domain, subject: object, node: Node) -> bool:
        """Evaluate a scope body — a boolean expression over one sub-object."""
        if isinstance(expr, All):
            return True
        if isinstance(expr, Not):
            return not self.holds(expr.body, domain, subject, node)
        if isinstance(expr, And):
            return all(self.holds(one, domain, subject, node) for one in expr.operands)
        if isinstance(expr, Or):
            return any(self.holds(one, domain, subject, node) for one in expr.operands)
        if isinstance(expr, Exists):
            found = _resolved(domain, expr.attribute, expr)
            return bool(self.sub_values(domain, subject, node, found))
        if isinstance(expr, Comparison):
            found = _resolved(domain, expr.attribute, expr)
            values = self.sub_values(domain, subject, node, found)
            return _compare(values, expr, found[0])
        # Scope and Traversal cannot appear here: the parser refuses both inside
        # brackets, which is what keeps a scope body a predicate over one thing.
        raise QueryError(  # pragma: no cover - unreachable via parse()
            f"{type(expr).__name__.lower()} cannot be evaluated inside {domain}[…]",
            text=self.query.text,
            offset=expr.span.offset,
            length=expr.span.length,
            source=self.query.source,
        )

    def sub_values(
        self, domain: Domain, subject: object, node: Node, found: tuple[Attribute, str]
    ) -> tuple[str, ...]:
        attribute, _qualifier = found
        if domain is Domain.INTERFACE:
            assert isinstance(subject, PortView)
            return interface_values(subject, node, attribute.name)
        if domain is Domain.LINK:
            assert isinstance(subject, LinkEnd)
            return link_values(subject, attribute.name)
        if domain is Domain.NETNS:
            assert isinstance(subject, NetnsView)
            return netns_values(subject, attribute.name)
        assert isinstance(subject, SecurityView)
        return zone_values(subject, attribute.name)

    # ------------------------------------------------------- the leaf terms

    def exists(self, expr: Exists, node: Node) -> bool:
        attribute, qualifier = _resolved(Domain.ELEMENT, expr.attribute, expr)
        return bool(element_values(self.facts, node, attribute.name, qualifier))

    def compare(self, expr: Comparison, node: Node) -> bool:
        attribute, qualifier = _resolved(Domain.ELEMENT, expr.attribute, expr)
        values = element_values(self.facts, node, attribute.name, qualifier)
        return _compare(values, expr, attribute)


def _resolved(domain: Domain, name: str, expr: Expr) -> tuple[Attribute, str]:
    """The attribute a checked expression names. Present by construction."""
    found = lookup(domain, name)
    assert found is not None, f"{name!r} survived parsing but is not a {domain} attribute"
    del expr
    return found


def _namespaces(node: Node) -> tuple[NetnsView, ...]:
    """The namespaces ``netns[…]`` asks about on this node (§23.1).

    A node the netns layer minted *is* one stack and answers for itself. Every
    other node is an element, and the stacks it runs are read from its document
    — which is what keeps the scope an attribute of the machine rather than of
    the drawing. Without that, `netns[name = ov-app]` would silently match
    nothing at every layer but one, and an assertion written with it would pass
    by never having looked.
    """
    if node.netns is not None:
        return (node.netns,)
    device = node.element if isinstance(node.element, Device) else None
    return () if device is None else netns_views(node.fqn, device)


def _zones(node: Node) -> tuple[SecurityView, ...]:
    """The security zones ``zone[…]`` asks about on this node (§24.1).

    The same rule as :func:`_namespaces`, for the same reason: at the security
    layer a node is a zone, and everywhere else it is the device that declares
    them. ``local`` and ``any`` are included when the policy reaches for them,
    exactly as the layer draws them, so the two answers are one answer.
    """
    if node.security is not None:
        return (node.security,)
    device = node.element if isinstance(node.element, Device) else None
    return () if device is None else security_views(node.fqn, device)


# ------------------------------------------------------------- the operators


def _compare(values: Sequence[str], expr: Comparison, attribute: Attribute) -> bool:
    """Does ``values`` satisfy the comparison?

    Existential for the positive operators, universal for the negated ones, and
    false for both when there are no values at all. See the module docstring of
    :mod:`netgraph.query.attributes` — this function is that paragraph.
    """
    if not values:
        return False
    operator = expr.operator
    if operator in (Operator.NE, Operator.NOT_GLOB):
        positive = Operator.EQ if operator is Operator.NE else Operator.GLOB
        return not any(_one(value, positive, expr.values, attribute) for value in values)
    return any(_one(value, operator, expr.values, attribute) for value in values)


def _one(value: str, operator: Operator, wanted: Sequence[str], attribute: Attribute) -> bool:
    """One value against the operator's alternatives."""
    if operator is Operator.EQ:
        return any(_equal(value, one, attribute) for one in wanted)
    if operator is Operator.GLOB:
        return any(fnmatch.fnmatchcase(value, one) for one in wanted)
    if operator is Operator.REGEX:
        return any(_pattern(one).search(value) is not None for one in wanted)
    if operator is Operator.UNDER:
        return any(_under(value, one) for one in wanted)
    if operator is Operator.IN:
        if attribute.type is ValueType.ADDRESS:
            return any(_within(value, one) for one in wanted)
        return any(_equal(value, one, attribute) for one in wanted)
    return _ordered(value, operator, wanted)


def _equal(value: str, wanted: str, attribute: Attribute) -> bool:
    """Equality, at the attribute's type.

    A number compares numerically, so ``mtu = 09000`` is ``mtu = 9000``; an
    address compares as an address, so ``address = 10.1.0.1/30`` matches the
    port however the inventory spelled the prefix length. Everything else is
    exact text, case-sensitive, because a hostname is.
    """
    if attribute.type in (ValueType.NUMBER, ValueType.VLAN):
        try:
            return int(value) == int(wanted)
        except ValueError:
            return False
    if attribute.type is ValueType.ADDRESS:
        left, right = _network(value), _network(wanted)
        if left is not None and right is not None:
            return left == right
    return value == wanted


def _ordered(value: str, operator: Operator, wanted: Sequence[str]) -> bool:
    """``<``, ``<=``, ``>``, ``>=`` — numeric, the only types the parser allows."""
    try:
        left = int(value)
        rights = [int(one) for one in wanted]
    except ValueError:
        return False
    if operator is Operator.LT:
        return any(left < right for right in rights)
    if operator is Operator.LE:
        return any(left <= right for right in rights)
    if operator is Operator.GT:
        return any(left > right for right in rights)
    return any(left >= right for right in rights)


def _under(value: str, wanted: str) -> bool:
    """Namespace containment: the namespace itself, or anything below it.

    Segment-wise, so ``sites/north`` does not contain ``sites/northolt``. The
    empty prefix contains everything, which is what makes ``namespace under ""``
    the whole inventory rather than only its root.
    """
    if not wanted:
        return True
    return value == wanted or value.startswith(f"{wanted.rstrip('/')}/")


def _within(value: str, wanted: str) -> bool:
    """Is the address ``value`` inside the prefix ``wanted``?

    An address with a prefix length — which is how every address in an inventory
    is written — is tested by its *host* part, so ``10.1.0.1/30 in 10.1.0.0/16``
    holds. A mixed-family comparison is simply false rather than an error: an
    inventory holds both families and a query naming one of them means that one.
    """
    host = _address(value)
    network = _network(wanted)
    if host is None or network is None or host.version != network.version:
        return False
    return host in network


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_interface(text).ip
    except ValueError:
        return None


def _network(text: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None


def _pattern(source: str) -> re.Pattern[str]:
    """Compile ``source`` once. Checked by the parser, so this cannot raise."""
    found = _PATTERN_CACHE.get(source)
    if found is None:
        if len(_PATTERN_CACHE) >= _PATTERN_CACHE_LIMIT:
            _PATTERN_CACHE.clear()
        found = re.compile(source)
        _PATTERN_CACHE[source] = found
    return found
