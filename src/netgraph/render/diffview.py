"""A changeset, painted onto a diagram.

``netgraph plan`` already answers *what changed* — element by element, field by
field — and every renderer already answers *what the network looks like*. This
module is the join: one drawing that holds both states at once, with the
changeset deciding how loudly each part of it is drawn.

The rules are the ones a reader of a code review already knows:

======================  ==================================================
added                   green, drawn at full weight
removed                 red and dashed, but still **placed** — a deletion
                        that reshuffled the diagram would hide itself in the
                        churn it caused
changed                 amber, with a badge naming the fields that moved
untouched               faded, so the eye goes to the four boxes that moved
                        rather than to the four hundred that did not
======================  ==================================================

Where the marks come from
-------------------------

Two places, and neither of them is a new opinion about what changed:

* **Presence.** A node or a link drawn in the *after* graph and not in the
  *before* one was added; the other way round, removed. That is not a judgement,
  it is what the two renderings contain — which is also the only thing that can
  answer for a *derived* node, since no document declares ``subnet:10.0.0.0/24``
  and no changeset can therefore mention it.
* **The plan.** Everything finer — that an element was *updated* rather than
  merely still present, which of its fields moved, and that a box is the same
  device under a new name — comes from :class:`~netgraph.plan.model.Plan` and
  from nowhere else. :mod:`netgraph.plan` stays the single notion of a
  changeset; this module only decides what colour it is.

Renames
-------

A rename is one box, not two. The before-side node is dropped from the union
and the after-side one is marked *changed* with a badge naming where it came
from — because drawing ``sw-1`` in red beside ``sw-core`` in green says two
devices were swapped, which is exactly what did not happen.

Coordinates
-----------

The union takes the *after* arrangement and falls back to the *before* one for
anything the after state no longer places. That is what keeps a removed node
where it was: whether the layout document still names it (nothing to fall back
to) or was pruned along with the element (the before side still has it), the
node is drawn where the reader last saw it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Final

from netgraph.layout.geometry import Geometry
from netgraph.render.graph import Edge, Graph, Node

__all__ = [
    "BADGE_FIELDS",
    "DiffOverlay",
    "Mark",
    "diff_overlay",
    "union_graph",
]

#: How many changed field paths a badge spells out before it counts the rest.
#: Three fits on a node label; a device whose whole ``spec`` was rewritten
#: should send the reader to ``netgraph plan``, not grow a paragraph.
BADGE_FIELDS: Final = 3


class Mark(str, Enum):
    """What a changeset says about one drawn thing."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"

    def __str__(self) -> str:
        return self.value


#: The order marks are counted and reported in: what the reader looks for first.
MARK_ORDER: Final[tuple[Mark, ...]] = (Mark.ADDED, Mark.CHANGED, Mark.REMOVED, Mark.UNCHANGED)

_EMPTY_MARKS: Final[Mapping[str, Mark]] = MappingProxyType({})
_EMPTY_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({})
_EMPTY_NAMES: Final[Mapping[str, str]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class DiffOverlay:
    """Which parts of a drawing changed, and how.

    Keyed by the identity the resolved graph already gave things —
    :attr:`Node.fqn <netgraph.render.graph.Node.fqn>` and :attr:`Edge.id
    <netgraph.render.graph.Edge.id>` — for the same reason
    :class:`~netgraph.render.highlight.Highlight` is: a producer never has to
    know how a backend spells them, and a backend never has to re-derive what
    changed.

    Anything not named here is :attr:`Mark.UNCHANGED`, which a backend draws
    faded. An *empty* overlay is therefore not the same as no overlay at all: it
    is the honest rendering of "nothing changed", the whole diagram dimmed.
    """

    #: Node fully-qualified name to what happened to it.
    nodes: Mapping[str, Mark] = _EMPTY_MARKS
    #: :attr:`Edge.id <netgraph.render.graph.Edge.id>` to what happened to it.
    edges: Mapping[str, Mark] = _EMPTY_MARKS
    #: Node or edge id to the field paths that moved, as ``netgraph plan``
    #: spells them. Only ever set for :attr:`Mark.CHANGED`.
    fields: Mapping[str, tuple[str, ...]] = _EMPTY_FIELDS
    #: Node or edge id to the address it used to have. Only ever set for
    #: something the plan detected as a rename.
    renamed_from: Mapping[str, str] = _EMPTY_NAMES
    #: The changeset itself, as :meth:`Plan.to_dict <netgraph.plan.model.Plan>`
    #: renders it, for the JSON export to publish beside the graph. ``None``
    #: when the overlay was built without one. Held as plain data rather than as
    #: a :class:`~netgraph.plan.model.Plan` so that :mod:`netgraph.render` keeps
    #: no dependency on :mod:`netgraph.plan`.
    changeset: Mapping[str, Any] | None = field(default=None, repr=False)

    def node(self, fqn: str) -> Mark:
        """What happened to the node called ``fqn``."""
        return self.nodes.get(fqn, Mark.UNCHANGED)

    def edge(self, edge_id: str) -> Mark:
        """What happened to the link identified by ``edge_id``."""
        return self.edges.get(edge_id, Mark.UNCHANGED)

    def badge(self, ident: str) -> str | None:
        """The caption under a changed thing, or ``None`` when it needs none.

        A rename is named as one; a field change lists up to
        :data:`BADGE_FIELDS` paths and counts whatever is left, so a node label
        stays a label.
        """
        previous = self.renamed_from.get(ident)
        paths = self.fields.get(ident, ())
        parts: list[str] = []
        if previous is not None:
            parts.append(f"was {previous}")
        if paths:
            shown = ", ".join(paths[:BADGE_FIELDS])
            hidden = len(paths) - BADGE_FIELDS
            parts.append(f"{shown} +{hidden} more" if hidden > 0 else shown)
        return "; ".join(parts) or None

    @property
    def is_empty(self) -> bool:
        """Does this overlay say that nothing at all moved?"""
        return not (self.nodes or self.edges)

    def narrowed(self, addresses: Iterable[str]) -> DiffOverlay:
        """This overlay with only the marks belonging to ``addresses`` kept.

        What ``netgraph diff --target`` is: the reader asked about four devices
        and wants the other four hundred drawn *untouched* rather than removed
        from the page, which is the difference between narrowing a diff and
        filtering a graph.

        A derived id belongs to no document and is therefore dropped, which is
        the honest reading of "only these elements": nobody selected the subnet
        an unselected host happens to sit in.
        """
        keep = set(addresses)
        nodes = {key: value for key, value in self.nodes.items() if _node_owner(key, keep) in keep}
        edges = {key: value for key, value in self.edges.items() if _edge_owner(key, keep) in keep}
        marked = set(nodes) | set(edges)
        return replace(
            self,
            nodes=nodes,
            edges=edges,
            fields={key: value for key, value in self.fields.items() if key in marked},
            renamed_from={key: value for key, value in self.renamed_from.items() if key in marked},
        )

    def counts(self) -> dict[Mark, int]:
        """How many drawn things carry each mark, untouched ones excluded.

        Untouched things are excluded because they are not *in* the mapping:
        an overlay names what moved, and the rest is the default.
        """
        tally: dict[Mark, int] = {mark: 0 for mark in MARK_ORDER if mark is not Mark.UNCHANGED}
        for mark in (*self.nodes.values(), *self.edges.values()):
            if mark is not Mark.UNCHANGED:
                tally[mark] += 1
        return tally

    def summary(self) -> str:
        """One line for a status bar: ``3 added, 1 changed, 2 removed``."""
        tally = self.counts()
        parts = [f"{tally[mark]} {mark.value}" for mark in MARK_ORDER[:-1] if tally[mark]]
        return ", ".join(parts) or "no visible change"

    def to_dict(self) -> dict[str, Any]:
        """The overlay as plain data, for the JSON export and for the editor."""
        payload: dict[str, Any] = {
            "nodes": {key: value.value for key, value in sorted(self.nodes.items())},
            "edges": {key: value.value for key, value in sorted(self.edges.items())},
            "counts": {mark.value: count for mark, count in self.counts().items()},
        }
        if self.fields:
            payload["fields"] = {key: list(value) for key, value in sorted(self.fields.items())}
        if self.renamed_from:
            payload["renamedFrom"] = dict(sorted(self.renamed_from.items()))
        return payload


# --------------------------------------------------------------------------- #
# The union drawing
# --------------------------------------------------------------------------- #


def union_graph(before: Graph, after: Graph, *, renames: Mapping[str, str] = {}) -> Graph:
    """One graph holding both states: the after one, plus what it no longer has.

    The after state comes first and in its own order, so a diff of an inventory
    that changed nothing draws byte-identically to a plain render of it. What
    only the before state had is appended, keeping its own relative order, which
    is what makes a removal visible rather than merely absent.

    Args:
        before: The graph of the state being changed *from*.
        after: The graph of the state being changed *to*.
        renames: Old fully-qualified name to new, for elements the plan
            recognised as the same thing under another name. A before-side node
            or link whose identity translates into something the after side
            already draws is dropped rather than drawn twice.

    Returns:
        A graph at ``after``'s layer, rooted where ``after`` is rooted. Every
        edge still references two nodes the graph has.
    """
    nodes: dict[str, Node] = dict(after.nodes)
    for fqn, node in before.nodes.items():
        translated = _translate(fqn, renames)
        if translated in nodes:
            continue
        nodes[translated] = node if translated == fqn else replace(node, fqn=translated)

    seen = {edge.id for edge in after.edges}
    edges: list[Edge] = list(after.edges)
    for edge in before.edges:
        translated = _translate(edge.id, renames)
        if translated in seen:
            continue
        seen.add(translated)
        edges.append(
            replace(
                edge,
                id=translated,
                source=_translate(edge.source, renames),
                target=_translate(edge.target, renames),
            )
        )

    # An edge whose endpoint the union does not hold would be drawn against a
    # node Graphviz invents, so it is dropped with a reason — the same contract
    # ``build_graph`` gives a cable with an unresolvable end.
    kept: list[Edge] = []
    dangling: list[str] = [*after.dangling, *before.dangling]
    for edge in edges:
        if edge.source in nodes and edge.target in nodes:
            kept.append(edge)
        else:
            dangling.append(f"{edge.id}: an endpoint is drawn in neither state")

    return Graph(
        root=after.root,
        nodes=nodes,
        edges=tuple(kept),
        layer=after.layer,
        dangling=tuple(dict.fromkeys(dangling)),
        sources={**before.sources, **after.sources},
        geometry=_union_geometry(before.geometry, after.geometry, renames=renames),
    )


def _union_geometry(
    before: Geometry, after: Geometry, *, renames: Mapping[str, str] = {}
) -> Geometry:
    """The after arrangement, backfilled from the before one.

    After wins wherever both place something: it is the arrangement the tree
    will have once the change lands, and a diff must not draw a node somewhere
    the next plain render would not.
    """
    if before.is_empty:
        return after
    if after.is_empty and not renames:
        return before
    return Geometry(
        view=after.view or before.view,
        nodes={
            **{_translate(key, renames): value for key, value in before.nodes.items()},
            **dict(after.nodes),
        },
        edges={
            **{_translate(key, renames): value for key, value in before.edges.items()},
            **dict(after.edges),
        },
        groups={**dict(before.groups), **dict(after.groups)},
    )


def _translate(ident: str, renames: Mapping[str, str]) -> str:
    """``ident`` as the after state spells it, if a rename moved what it names.

    Every derived identity in the graph is either an address, an address with a
    ``#suffix``, or an address with a ``:`` qualifier — ``sw-1#upstream``,
    ``sw-1:eth0#10.0.0.0/24`` — so translating the address prefix translates
    every id that hangs off it, without this module knowing how any one of them
    is spelled.
    """
    if not renames:
        return ident
    direct = renames.get(ident)
    if direct is not None:
        return direct
    for old, new in renames.items():
        for separator in ("#", ":"):
            prefix = old + separator
            if ident.startswith(prefix):
                return new + separator + ident[len(prefix) :]
    return ident


# --------------------------------------------------------------------------- #
# The marks
# --------------------------------------------------------------------------- #


def diff_overlay(
    before: Graph,
    after: Graph,
    *,
    updated: Mapping[str, Sequence[str]] = {},
    renames: Mapping[str, str] = {},
    changeset: Mapping[str, Any] | None = None,
) -> DiffOverlay:
    """Mark every node and link of the union of ``before`` and ``after``.

    Args:
        before: The graph of the state being changed from.
        after: The graph of the state being changed to.
        updated: Address of each element the changeset *updates*, to the field
            paths that moved. An address marks every drawn thing that hangs off
            it — the node, and any link the element declares.
        renames: Old address to new, as :func:`union_graph` takes them.
        changeset: The plan as plain data, carried through to the JSON export.

    Returns:
        An overlay naming everything that moved. Nothing else is named: an
        untouched thing is the default, not an entry.
    """
    before_nodes = {_translate(fqn, renames) for fqn in before.nodes}
    before_edges = {_translate(edge.id, renames) for edge in before.edges}
    after_nodes = set(after.nodes)
    after_edges = {edge.id for edge in after.edges}

    union = union_graph(before, after, renames=renames)
    node_marks: dict[str, Mark] = {}
    edge_marks: dict[str, Mark] = {}
    fields: dict[str, tuple[str, ...]] = {}
    renamed_from: dict[str, str] = {}

    updated_ids = _touched(union, updated)
    renamed_ids = _touched(union, dict.fromkeys(renames.values(), ()))
    # A rename with no field change is still a change to the element, so both
    # sets decide the amber mark; only the badge distinguishes them.
    changed_ids = {**renamed_ids, **updated_ids}
    previous = {new: old for old, new in renames.items()}

    for fqn in union.nodes:
        mark = _mark(fqn, before_nodes, after_nodes, changed_ids)
        if mark is not Mark.UNCHANGED:
            node_marks[fqn] = mark
    for edge in union.edges:
        mark = _mark(edge.id, before_edges, after_edges, changed_ids)
        if mark is not Mark.UNCHANGED:
            edge_marks[edge.id] = mark

    marked = set(node_marks) | set(edge_marks)
    for ident, address in updated_ids.items():
        paths = tuple(updated.get(address, ()))
        if paths and ident in marked:
            fields[ident] = paths
    for ident, address in renamed_ids.items():
        old = previous.get(address)
        if old is not None and ident in marked:
            renamed_from[ident] = _translate_back(ident, address, old)

    return DiffOverlay(
        nodes=node_marks,
        edges=edge_marks,
        fields=fields,
        renamed_from=renamed_from,
        changeset=changeset,
    )


def _mark(
    ident: str, before: Iterable[str], after: Iterable[str], changed: Mapping[str, str]
) -> Mark:
    """Presence decides added and removed; the changeset decides the rest."""
    in_before, in_after = ident in before, ident in after
    if in_after and not in_before:
        return Mark.ADDED
    if in_before and not in_after:
        return Mark.REMOVED
    return Mark.CHANGED if ident in changed else Mark.UNCHANGED


def _touched(graph: Graph, addresses: Mapping[str, Any]) -> dict[str, str]:
    """Every drawn id that *is* one of ``addresses``, to the address itself.

    A device is a node, a cable is a link, and an adapter is both — §8.2 draws it
    as a box *and* as the attachment to its host, and both belong to the one
    document. What is deliberately excluded is everything a changed element
    merely *participates* in: a subnet membership is keyed
    ``host:eth0#10.0.0.0/24``, and marking it amber because the host's model
    string changed would claim the network moved when only a label did.
    """
    if not addresses:
        return {}
    found: dict[str, str] = {}
    for ident in graph.nodes:
        address = _node_owner(ident, addresses)
        if address is not None:
            found[ident] = address
    for edge in graph.edges:
        address = _edge_owner(edge.id, addresses)
        if address is not None:
            found[edge.id] = address
    return found


def _node_owner(ident: str, addresses: Mapping[str, Any]) -> str | None:
    """Which of ``addresses`` the node id ``ident`` draws, if any.

    A declared element is drawn under its own address; a tunnel and a rack are
    drawn under a qualified one (``tunnel:site/wg0``), and a subnet under an id
    no document could have declared, which is why it can never match.
    """
    if ident in addresses:
        return ident
    for prefix in ("tunnel:", "rack:"):
        if ident.startswith(prefix):
            rest = ident[len(prefix) :]
            return rest if rest in addresses else None
    return None


def _edge_owner(ident: str, addresses: Mapping[str, Any]) -> str | None:
    """Which of ``addresses`` the link id ``ident`` *is*, if any.

    A cable is drawn under its own address, and a link a document declares more
    than one of — an adapter's upstream attachment, a tunnel's two legs — under
    that address with a ``#`` qualifier. Nothing else is a document.
    """
    if ident in addresses:
        return ident
    head, sep, _ = ident.partition("#")
    return head if sep and head in addresses else None


def _translate_back(ident: str, address: str, old: str) -> str:
    """How ``ident`` was spelled before ``address`` was renamed from ``old``.

    A badge on a link should read ``was core/cbl-1``, not ``was core/cbl-1`` for
    the node and something unrecognisable for the attachment hanging off it.
    """
    if ident == address:
        return old
    if ident.startswith(address):
        return old + ident[len(address) :]
    return old
