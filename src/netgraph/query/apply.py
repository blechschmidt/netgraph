"""Layering a query over the filter every command already has.

``--select`` is not a replacement for ``--kind`` and ``--namespace``; it is a
sixth field of the same :class:`~netgraph.render.graph.FilterSpec`, and this
module is the thin piece that fills it in. The order is fixed and matters:

1. build the graph for the requested layer;
2. answer the query against **that whole graph**, because a traversal is a
   question about the topology and a query narrowed first could not see past the
   nodes the other fields removed;
3. hand the resulting set of names to
   :func:`~netgraph.render.graph.filter_graph`, which does the graph surgery it
   has always done — the edges, the derived nodes, the geometry, the
   annotations — with the query's answer AND-ed in alongside the flags.

So ``--kind switch --select 'has vrf'`` keeps the switches that have one, the
same way ``--kind switch --vlan 10`` keeps the switches in that VLAN. Nothing in
:func:`filter_graph` had to learn what a query is, and nothing here had to learn
what an annotation target is.
"""

from __future__ import annotations

from dataclasses import replace

from netgraph.query.evaluate import evaluate
from netgraph.query.parser import Query, parse
from netgraph.render.graph import FilterSpec, Graph, filter_graph

__all__ = ["narrow", "resolve", "resolved_query"]


def resolved_query(spec: FilterSpec, *, source: str = "--select") -> Query | None:
    """Parse ``spec.select``, or ``None`` when no query was given.

    Separate from :func:`resolve` so a caller can reject a malformed query
    *before* loading an inventory: a typo in a selector should cost a usage
    error, not a full parse of a thousand documents followed by one.
    """
    return None if spec.select is None else parse(spec.select, source=source)


def resolve(spec: FilterSpec, graph: Graph, *, source: str = "--select") -> FilterSpec:
    """The same spec with :attr:`FilterSpec.selected` answered against ``graph``.

    Idempotent: a spec that already carries an answer is returned unchanged, so
    a pipeline that narrows several layers can answer once per layer without
    each stage having to know whether an earlier one did.
    """
    if spec.select is None or spec.selected is not None:
        return spec
    result = evaluate(parse(spec.select, source=source), graph)
    return replace(spec, selected=result.selected)


def narrow(graph: Graph, spec: FilterSpec, *, source: str = "--select") -> Graph:
    """Answer ``spec``'s query against ``graph``, then filter it.

    The drop-in for :func:`~netgraph.render.graph.filter_graph` at every call
    site that might see a ``--select``. A spec without one takes exactly the old
    path, allocation for allocation.

    Raises:
        QueryError: The query cannot be parsed.
        UnknownElementError: ``spec.neighbors_of`` names no node, exactly as
            :func:`filter_graph` raises it.
    """
    return filter_graph(graph, resolve(spec, graph, source=source))
