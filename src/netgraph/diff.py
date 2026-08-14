"""``netgraph diff``: a changeset, drawn.

This module is deliberately thin, and deliberately the only place the two halves
meet. :mod:`netgraph.plan` decides *what changed* and :mod:`netgraph.render`
decides *what a network looks like*; neither imports the other, and neither
grows a second opinion about the other's job. What is left over is the
translation between them — a changeset is keyed by *address*, a drawing is keyed
by node and edge *id* — and that is what lives here.

The result of :func:`draw` is an ordinary :class:`~netgraph.render.graph.Graph`
and an ordinary :class:`~netgraph.render.diffview.DiffOverlay`, so every
existing backend draws it: ``dot`` and the Graphviz image formats paint it,
``html`` puts it behind the same info box, and ``json`` publishes the marks and
the changeset side by side. Nothing here renders anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from netgraph.plan.address import LAYOUT_TYPE
from netgraph.plan.model import Action, Plan
from netgraph.render.diffview import DiffOverlay, diff_overlay, union_graph
from netgraph.render.graph import Graph

__all__ = ["Drawing", "draw", "renamed_addresses", "updated_fields"]


@dataclass(frozen=True, slots=True)
class Drawing:
    """One diagram holding two states, and the marks that tell them apart."""

    #: The union of both states: everything the desired state draws, plus what
    #: it no longer has, still in place.
    graph: Graph
    #: What happened to each part of it.
    overlay: DiffOverlay

    @property
    def is_empty(self) -> bool:
        """Did the changeset leave no visible trace on this layer?

        True of a change that this drawing cannot show — a layout document moved
        a node at layer 1 and the diff was asked for at layer 3 — which is worth
        saying rather than presenting a wholly faded diagram as an answer.
        """
        return self.overlay.is_empty


def draw(plan: Plan, before: Graph, after: Graph) -> Drawing:
    """Join a changeset to the two drawings it sits between.

    Args:
        plan: The changeset, from :func:`netgraph.plan.diff`. It is the only
            authority on what *changed*; presence in the two graphs is the only
            authority on what was *added* or *removed*, because a derived node
            like a layer-3 prefix appears in no changeset.
        before: The state being changed from, rendered at some layer.
        after: The state being changed to, rendered at the same layer.

    Returns:
        The union graph and the overlay over it.
    """
    renames = renamed_addresses(plan)
    return Drawing(
        graph=union_graph(before, after, renames=renames),
        overlay=diff_overlay(
            before,
            after,
            updated=updated_fields(plan),
            renames=renames,
            changeset=plan.to_dict(),
        ),
    )


def updated_fields(plan: Plan) -> dict[str, tuple[str, ...]]:
    """Every element the plan *updates*, to the field paths that moved.

    Keyed by fully-qualified name rather than by :class:`address
    <netgraph.plan.address.Address>`, because that is what a drawing keys
    things by and an inventory cannot hold two elements under one name.
    ``layout`` documents are left out: a coordinate is not a fact about the
    network, and marking a node amber because it was dragged would say the
    device changed.
    """
    updated: dict[str, tuple[str, ...]] = {}
    for change in plan:
        if change.address.type == LAYOUT_TYPE:
            continue
        if change.action not in (Action.UPDATE, Action.RENAME):
            continue
        paths = tuple(field.text for field in change.fields)
        target = change.new_address.fqn if change.new_address is not None else change.address.fqn
        if paths or target not in updated:
            updated[target] = paths
    return updated


def renamed_addresses(plan: Plan) -> dict[str, str]:
    """Old fully-qualified name to new, for every rename in the plan.

    A rename is the one change that would otherwise be drawn as two: the same
    device, red under its old name and green under its new one. Handing the map
    to :func:`~netgraph.render.diffview.union_graph` collapses the pair into one
    amber box that says where it came from.
    """
    return {
        change.address.fqn: change.new_address.fqn
        for change in plan
        if change.action is Action.RENAME
        and change.new_address is not None
        and change.address.type != LAYOUT_TYPE
    }
