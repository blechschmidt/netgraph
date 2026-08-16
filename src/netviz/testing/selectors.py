"""Turning an assertion's ``select:`` string into the filter the renderer uses.

``netviz render`` narrows a diagram with repeated flags — ``--kind switch
--namespace sites/north --name sw-*`` — which a YAML document has nowhere to
put. So an assertion writes the same thing as one scalar::

    select: kind=switch, namespace=sites/north, name=sw-*

and this module parses it into the very same
:class:`~netviz.render.graph.FilterSpec` the renderer filters with. That is
the whole design: an assertion and a diagram cannot disagree about what
``kind=switch`` selects, because there is one implementation of *selects*.

The grammar
-----------

Comma-separated terms. A term is ``key=value``, or a bare word which is short
for ``name=<word>`` — so ``select: sw-core`` and ``select: sw-*`` are the
spellings somebody reaches for first and both work. Keys are the long forms of
the flags: ``namespace``, ``vlan``, ``kind``, ``name``, ``neighbors-of`` and
``depth``. A key may be repeated, and repeats are *alternatives* exactly as a
repeated flag is; different keys are combined with AND.

Every rejection names what was written and what was expected, because a
selector is typed once and read for years.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from netviz.errors import echo_value
from netviz.models.scalars import MAX_VLAN_ID, MIN_VLAN_ID
from netviz.query import QueryError
from netviz.query.apply import narrow as narrow_graph
from netviz.render.graph import FilterSpec, Graph, UnknownElementError

__all__ = ["SELECTOR_KEYS", "SelectorError", "parse_selector", "query_spec", "select_nodes"]

#: Every key a selector may name, and the ``netviz render`` flag it is.
SELECTOR_KEYS: dict[str, str] = {
    "namespace": "--namespace",
    "vlan": "--vlan",
    "kind": "--kind",
    "name": "--name",
    "neighbors-of": "--neighbors-of",
    "depth": "--depth",
}

#: Spellings accepted for ``neighbors-of``. The hyphen matches the flag; the
#: underscore matches every other multi-word key in the schema, and a reader
#: guessing either is right.
_ALIASES: dict[str, str] = {"neighbors_of": "neighbors-of", "neighbours-of": "neighbors-of"}


class SelectorError(ValueError):
    """A ``select:`` string that cannot be read as a filter."""


def parse_selector(text: str) -> FilterSpec:
    """Parse one ``select:`` string.

    Args:
        text: The selector as written, e.g. ``kind=switch, name=sw-*``.

    Returns:
        The :class:`~netviz.render.graph.FilterSpec` it denotes.

    Raises:
        SelectorError: A term is empty, names an unknown key, or carries a value
            the key cannot hold.
    """
    namespaces: list[str] = []
    vlans: set[int] = set()
    kinds: list[str] = []
    names: list[str] = []
    neighbors_of: str | None = None
    depth = 1

    for key, value in _terms(text):
        if key == "namespace":
            namespaces.append(value)
        elif key == "kind":
            kinds.append(value)
        elif key == "name":
            names.append(value)
        elif key == "vlan":
            vlans.add(_vlan(value))
        elif key == "depth":
            depth = _depth(value)
        else:
            if neighbors_of is not None:
                raise SelectorError(
                    f"'neighbors-of' is given twice, as {echo_value(neighbors_of)} and "
                    f"{echo_value(value)}; a neighbourhood has one centre"
                )
            neighbors_of = value

    return FilterSpec(
        namespaces=tuple(namespaces),
        vlans=frozenset(vlans),
        kinds=tuple(kinds),
        names=tuple(names),
        neighbors_of=neighbors_of,
        depth=depth,
    )


def _terms(text: str) -> Iterator[tuple[str, str]]:
    """Every ``(key, value)`` in ``text``, with the bare-word shorthand expanded."""
    if not text.strip():
        raise SelectorError("the selector is empty; write 'kind=switch', 'name=sw-*' or a name")
    for raw in text.split(","):
        term = raw.strip()
        if not term:
            raise SelectorError(
                f"empty term in selector {echo_value(text)}; two commas in a row select nothing"
            )
        key, separator, value = term.partition("=")
        if not separator:
            # A bare word is a name glob: 'select: sw-core' is what somebody
            # writes before they have read anything about the grammar.
            yield "name", term
            continue
        key = _ALIASES.get(key.strip(), key.strip())
        value = value.strip()
        if key not in SELECTOR_KEYS:
            raise SelectorError(
                f"unknown selector key {echo_value(key)}; expected one of "
                f"{', '.join(SELECTOR_KEYS)}"
            )
        if not value:
            raise SelectorError(f"selector key {key!r} has no value; write '{key}=something'")
        yield key, value


def _vlan(value: str) -> int:
    """One VLAN id, or a message saying why it is not one."""
    try:
        vlan = int(value)
    except ValueError:
        raise SelectorError(
            f"'vlan={value}' is not a VLAN id; expected a number from {MIN_VLAN_ID} to "
            f"{MAX_VLAN_ID}"
        ) from None
    if not MIN_VLAN_ID <= vlan <= MAX_VLAN_ID:
        raise SelectorError(f"VLAN {vlan} is outside {MIN_VLAN_ID}-{MAX_VLAN_ID}")
    return vlan


def _depth(value: str) -> int:
    """The ``--depth`` of a neighbourhood."""
    try:
        depth = int(value)
    except ValueError:
        raise SelectorError(f"'depth={value}' is not a number of hops") from None
    if depth < 0:
        raise SelectorError(f"depth {depth} is negative; a neighbourhood cannot reach backwards")
    return depth


def query_spec(text: str | None, spec: FilterSpec | None = None) -> FilterSpec:
    """Fold an assertion's ``query:`` into the filter its ``select:`` produced.

    Both keys mean "which elements is this about", so both end up in the same
    :class:`~netviz.render.graph.FilterSpec` and are combined with AND — which
    is what makes ``select: kind=switch`` plus ``query: not has address`` read
    the way it looks. Given neither, the spec is returned unchanged.
    """
    base = spec if spec is not None else FilterSpec()
    return base if text is None else replace(base, select=text)


def select_nodes(graph: Graph, spec: FilterSpec) -> tuple[str, ...]:
    """The declared elements ``spec`` selects, in load order.

    Derived nodes — a layer-3 prefix, a tunnel, a rack — are excluded even when
    the filter keeps them: an assertion is made about things somebody declared,
    and "every switch has 24 ports" should not be graded against a subnet.

    Raises:
        SelectorError: ``neighbors-of`` names no node of the graph.
    """
    try:
        narrowed = narrow_graph(graph, spec, source="query")
    except QueryError as exc:
        # A malformed query is a broken *assertion*, not a broken network, and
        # the engine reports it the same way it reports a malformed selector —
        # so it is re-raised as the one error type the caller already catches,
        # carrying the caret block whole.
        raise SelectorError(str(exc)) from exc
    except UnknownElementError as exc:
        hint = f"; did you mean {', '.join(exc.candidates[:5])}?" if exc.candidates else ""
        raise SelectorError(
            f"'neighbors-of={exc.name}' names nothing in this inventory{hint}"
        ) from exc
    return tuple(node.fqn for node in narrowed.element_nodes)
