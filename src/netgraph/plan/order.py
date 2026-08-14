"""The order a changeset has to be executed in, and why it is that order.

Every reference in an inventory points *outwards* from the thing that cannot
exist without it: a cable names two devices, an adapter names the host it is
plugged into, a tunnel names the tunnel it runs over. That single direction
gives the whole ordering:

* **Deletions run first, dependents before dependees.** A cable is removed
  before the device it terminates on. Backwards, the delete of the device would
  be refused — :class:`~netgraph.edit.errors.CascadeRequired` — or, worse,
  forced through with ``--cascade`` and take the cable with it as a side effect
  the plan never mentioned.
* **Renames run next**, so that everything after them speaks the new names.
* **Creations run after, dependees before dependents.** The device exists before
  the cable that lands on it, so the cable's endpoints resolve when it is
  written and the validation gate has something to check them against.
* **Updates run last.** An update may point a field at something the plan has
  just created, and nothing else waits on an update.

Within a group the order is the dependency order where there is one, and the
address order where there is not, so two runs of the same diff produce the same
plan. A reference cycle — which the schema does not permit but a hand-written
tree can still contain — falls back to address order rather than looping.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from netgraph.edit.references import references_of
from netgraph.loader.inventory import Inventory, namespace_of
from netgraph.plan.address import Address, address_of
from netgraph.plan.model import Action, Change

__all__ = ["dependencies", "order_changes"]

#: The action groups, in the order they are executed.
_PHASES: tuple[Action, ...] = (Action.DELETE, Action.RENAME, Action.CREATE, Action.UPDATE)


def dependencies(inventory: Inventory) -> dict[Address, frozenset[Address]]:
    """What each element needs to already exist, by address.

    Built from the same reference table the edit layer uses to keep a rename
    consistent, so the plan and the executor cannot disagree about what depends
    on what.
    """
    graph: dict[Address, frozenset[Address]] = {}
    for fqn, element in inventory.elements.items():
        address = address_of(element.kind, fqn)
        namespace = namespace_of(fqn)
        needs: set[Address] = set()
        for reference in references_of(fqn, element):
            resolution = inventory.lookup(reference.target, namespace=namespace)
            if resolution.fqn is None or resolution.element is None:
                continue
            target = address_of(resolution.element.kind, resolution.fqn)
            if target != address:
                needs.add(target)
        graph[address] = frozenset(needs)
    return graph


def order_changes(
    changes: Iterable[Change],
    *,
    before: Mapping[Address, frozenset[Address]],
    after: Mapping[Address, frozenset[Address]],
) -> tuple[Change, ...]:
    """Sort a changeset into the order it must be executed in.

    Args:
        changes: The entries, in any order.
        before: Dependencies as the source state has them, used for deletions.
        after: Dependencies as the target state has them, used for creations.
    """
    entries = list(changes)
    ordered: list[Change] = []
    for action in _PHASES:
        group = [change for change in entries if change.action is action]
        if action is Action.DELETE:
            ordered.extend(_toposort(group, before, dependents_first=True))
        elif action is Action.CREATE:
            ordered.extend(_toposort(group, after, dependents_first=False))
        else:
            ordered.extend(sorted(group, key=lambda change: change.address.order))
    return tuple(ordered)


def _toposort(
    changes: Sequence[Change],
    graph: Mapping[Address, frozenset[Address]],
    *,
    dependents_first: bool,
) -> list[Change]:
    """Kahn's algorithm over the subgraph the group spans, ties by address.

    Only edges *within* the group matter: a cable created against a device that
    already exists constrains nothing. Anything left over when no node is ready
    is in a cycle, and is emitted in address order so the plan is still total.
    """
    by_address = {change.address: change for change in changes}
    inside = set(by_address)
    edges = {
        address: {target for target in graph.get(address, frozenset()) if target in inside}
        for address in inside
    }
    if dependents_first:
        # A depends on B, so A must go first: reverse every edge.
        reversed_edges: dict[Address, set[Address]] = {address: set() for address in inside}
        for address, targets in edges.items():
            for target in targets:
                reversed_edges[target].add(address)
        edges = reversed_edges

    ordered: list[Change] = []
    remaining = dict(edges)
    while remaining:
        ready = sorted(
            (address for address, needs in remaining.items() if not needs),
            key=lambda address: address.order,
        )
        if not ready:
            ready = [min(remaining, key=lambda address: address.order)]
        for address in ready:
            ordered.append(by_address[address])
            del remaining[address]
            for needs in remaining.values():
                needs.discard(address)
    return ordered
