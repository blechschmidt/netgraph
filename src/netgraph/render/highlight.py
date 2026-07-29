"""Emphasis for a rendering: the part of the graph a reader was asking about.

``netgraph path A B --highlight`` answers a question about *two* elements and
then wants the whole inventory drawn around the answer. That is a display
decision, not a topology one — every node and every link is still drawn, exactly
as :class:`~netgraph.render.options.RenderOptions` promises — so it belongs
here rather than in :class:`~netgraph.render.graph.FilterSpec`, which decides
what exists.

The distinction matters for the reader too. ``--neighbors-of`` removes the rest
of the network and leaves the impression that nothing else is connected;
highlighting keeps it on the page, dimmed, so a traced path is visibly *one
route through* a topology rather than the topology itself.

A :class:`Highlight` names nodes and edges by the identity the resolved graph
already gave them — :attr:`Node.fqn <netgraph.render.graph.Node.fqn>` and
:attr:`Edge.id <netgraph.render.graph.Edge.id>` — so a producer never has to
know how a backend spells them, and a backend never has to re-derive what was
traced.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["Highlight"]


@dataclass(frozen=True, slots=True)
class Highlight:
    """The nodes and links to emphasise; everything else is drawn dimmed.

    An *empty* highlight is not the same as no highlight at all: it means
    "nothing matched", and a backend honouring it dims the whole diagram. That
    is the honest rendering of a trace that found no path, so the caller decides
    whether to pass one — see :attr:`RenderOptions.highlight
    <netgraph.render.options.RenderOptions.highlight>`, where ``None`` is the
    unhighlighted default.
    """

    #: Fully-qualified names of the nodes to emphasise. A derived node counts:
    #: a layer-3 prefix is what an ``l3`` hop crosses, and a tunnel node is what
    #: a multipoint tunnel is drawn as.
    nodes: frozenset[str] = frozenset()
    #: :attr:`Edge.id <netgraph.render.graph.Edge.id>` of each link to emphasise.
    edges: frozenset[str] = frozenset()

    @classmethod
    def of(cls, nodes: Iterable[str] = (), edges: Iterable[str] = ()) -> Highlight:
        """Build one from any two iterables, without the caller freezing them."""
        return cls(nodes=frozenset(nodes), edges=frozenset(edges))

    @property
    def is_empty(self) -> bool:
        """Does this highlight emphasise nothing at all?"""
        return not (self.nodes or self.edges)

    def has_node(self, fqn: str) -> bool:
        return fqn in self.nodes

    def has_edge(self, edge_id: str) -> bool:
        return edge_id in self.edges

    def __or__(self, other: Highlight) -> Highlight:
        """The union of two highlights, so several paths can share a diagram."""
        return Highlight(nodes=self.nodes | other.nodes, edges=self.edges | other.edges)
