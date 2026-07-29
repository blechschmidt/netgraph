"""Stable, XML-safe identity for the nodes, edges and clusters of a drawing.

Graphviz copies an ``id`` attribute straight into the element it emits, so this
is what makes a rendering *addressable*: ``topology.svg#node-sites_hq_sw-core``
scrolls a browser to one switch, and ``#node-sites_hq_sw-core polygon {…}`` in a
stylesheet colours it, without a line of JavaScript and without anyone having to
count nodes.

That is only worth anything if the id is **stable**, so it is derived from the
fully-qualified name rather than from a position: adding a device to a file must
not renumber the bookmark someone put in a wiki. The derivation is a slug —
everything outside ``[A-Za-z0-9_.-]`` becomes an underscore — for two reasons:

* An XML ``id`` is an ``NCName``: it may not hold a ``/``, may not start with a
  digit, and (as an HTML fragment) is far easier to live with in ASCII. A
  fully-qualified name holds ``/`` by construction, and an inventory may hold
  anything at all.
* The ids of a published diagram are a second, unescaped copy of the inventory's
  names. Reducing them to a known alphabet means a name carrying markup, a
  right-to-left override or an astral-plane character cannot reach a consumer
  through this route at all.

Slugging is lossy, so two names can collide (``a/b`` and ``a_b``). Collisions
are broken by appending ``-2``, ``-3`` … in graph order, which is deterministic
for a given graph; a name long enough to be truncated keeps a digest of the
original instead, so truncation cannot silently merge two 200-character names.

One caveat about the output, which :func:`netgraph.render.dot.to_image`'s tests
pin down: Graphviz XML-escapes ``-`` as ``&#45;`` when it writes an ``id``
attribute out. That is *escaping*, not mangling — every XML parser, browser and
stylesheet sees ``node-sites_hq_sw-core`` — but a ``grep`` over the raw file
will not find it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import blake2s
from typing import Final

from netgraph.render.graph import Graph

__all__ = [
    "CLUSTER_ID_PREFIX",
    "EDGE_ID_PREFIX",
    "NODE_ID_PREFIX",
    "ElementIds",
    "element_ids",
    "slug",
]

#: What each family of ids is prefixed with. The prefix guarantees the id starts
#: with a letter — a fully-qualified name need not — and keeps netgraph's ids out
#: of the space of the ones Graphviz invents for itself (``node1``, ``edge1``,
#: ``clust1``), so the two can never collide in one document.
NODE_ID_PREFIX: Final = "node-"
EDGE_ID_PREFIX: Final = "edge-"
CLUSTER_ID_PREFIX: Final = "cluster-"

#: Everything an id may *not* hold, collapsed to a single underscore per run.
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.-]+")

#: Longest slug kept verbatim. Past this a name is truncated and a digest of the
#: whole of it appended, so the id stays bounded without two long names sharing
#: one. Names are short in practice; this is a guard, not a policy.
_MAX_SLUG: Final = 96
_DIGEST_BYTES: Final = 4


def slug(text: str) -> str:
    """``text`` reduced to the alphabet an XML ``id`` may hold.

    Deterministic and side-effect free: the same name always slugs to the same
    string, on every machine and every run, which is what lets a golden file
    pin the ids down.
    """
    cleaned = _UNSAFE.sub("_", text).strip("_")
    if not cleaned:
        # A name of nothing but unsafe characters still needs an id, and the
        # digest is the only thing left that distinguishes it from the next one.
        return blake2s(text.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()
    if len(cleaned) <= _MAX_SLUG:
        return cleaned
    digest = blake2s(text.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()
    return f"{cleaned[:_MAX_SLUG]}_{digest}"


@dataclass(frozen=True, slots=True)
class ElementIds:
    """The ``id`` every drawable part of one graph is emitted with.

    Built by :func:`element_ids` and passed around rather than recomputed, so
    the renderer and the detail records
    (:func:`~netgraph.render.details.build_details`) cannot disagree about which
    id belongs to which element — that agreement is the whole interface between
    a diagram and anything that inspects it.
    """

    #: Fully-qualified name to id, in graph order.
    nodes: Mapping[str, str] = field(default_factory=dict)
    #: One id per edge, indexed as :attr:`~netgraph.render.graph.Graph.edges` is.
    edges: tuple[str, ...] = ()
    #: Namespace to cluster id; the root namespace is never a cluster.
    clusters: Mapping[str, str] = field(default_factory=dict)

    def node(self, fqn: str) -> str | None:
        return self.nodes.get(fqn)

    def edge(self, index: int) -> str | None:
        return self.edges[index] if 0 <= index < len(self.edges) else None

    def cluster(self, namespace: str) -> str | None:
        return self.clusters.get(namespace)


def element_ids(graph: Graph) -> ElementIds:
    """Assign an id to every node, edge and namespace of ``graph``.

    Ids are unique across all three families — a consumer holding one id should
    not have to know which sort of thing it names to look it up — and are
    assigned in graph order, so the disambiguating suffix a collision earns
    depends only on the graph and not on the order a caller asked in.
    """
    taken: set[str] = set()
    nodes = {fqn: _unique(f"{NODE_ID_PREFIX}{slug(fqn)}", taken) for fqn in graph.nodes}
    edges = tuple(_unique(f"{EDGE_ID_PREFIX}{slug(edge.id)}", taken) for edge in graph.edges)
    clusters = {
        namespace: _unique(f"{CLUSTER_ID_PREFIX}{slug(namespace)}", taken)
        for namespace in graph.namespaces
        if namespace
    }
    return ElementIds(nodes=nodes, edges=edges, clusters=clusters)


def _unique(candidate: str, taken: set[str]) -> str:
    """``candidate``, suffixed until nothing else in the document holds it."""
    chosen = candidate
    counter = 2
    while chosen in taken:
        chosen = f"{candidate}-{counter}"
        counter += 1
    taken.add(chosen)
    return chosen
