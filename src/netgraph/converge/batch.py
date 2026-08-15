"""Grouping devices into maintenance windows, using the impact engine.

A plan that lists forty devices is a plan nobody can schedule. What an operator
actually needs to know is *which of these can I do at the same time, and what
goes dark while I do*. That is the question :mod:`netgraph.impact` already
answers for a hypothetical failure, and a device being reconfigured is a device
that may bounce -- so the two are the same question and get the same engine.

The rule for sharing a batch is one sentence: **two devices may be worked
together when neither is in the other's blast radius and their blast radii do
not overlap.** Taking both out at once is then no worse than taking either out
on its own, which is the only property that makes a window schedulable. Anything
that would compound -- an access switch and the core switch it hangs off, two
halves of a redundant pair -- falls into a later batch, so walking the batches
in order is a maintenance schedule that never doubles up an outage.

The packing is first-fit over devices in name order. First-fit is not optimal
and does not try to be: an optimal packing would depend on the whole set, so
adding one device could reshuffle every batch, and a schedule that changes
shape when the inventory grows by one is a schedule nobody trusts. First-fit in
a fixed order is stable -- adding a device can only ever append to a batch or
add one.

A device that has only safe changes still gets a batch, because "safe" means
netgraph could not see how it would cut reachability, not that a person should
run forty of them blind.
"""

from __future__ import annotations

from collections.abc import Sequence

from netgraph.converge.model import Batch
from netgraph.impact.engine import simulate
from netgraph.impact.model import ImpactError
from netgraph.loader.inventory import Inventory

__all__ = ["batches_for", "blast_radius"]

#: Layers a maintenance window is measured across. Power is left out: a device
#: being reconfigured is not a device losing its feed, and including it would
#: report a PDU's whole cabinet as collateral for an MTU change.
_LAYERS: tuple[str, ...] = ("l1", "l2", "l3")


def blast_radius(inventory: Inventory, element: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(isolated, splits)`` if ``element`` were out of service.

    Args:
        inventory: The declared tree.
        element: Fully-qualified name of a device.

    Returns:
        The elements that lose reachability, sorted and deduplicated across
        layers, and one line per namespace the failure partitions. Both empty
        when the element is not something the graphs hold -- a cable moved by
        hand, a device the inventory does not declare.
    """
    try:
        report = simulate(inventory, fail=[element], wanted_layers=_LAYERS)
    except ImpactError:
        # The element is not in the graphs: an undeclared device, or one that
        # was filtered out. Nothing measurable goes down, and guessing would be
        # worse than saying so.
        return ((), ())
    isolated: set[str] = set()
    splits: list[str] = []
    for layer in report.layers:
        isolated.update(layer.isolated)
        for split in layer.splits:
            line = f"{split.label}: {split.before} -> {split.after} pieces ({layer.layer})"
            if line not in splits:
                splits.append(line)
    isolated.discard(element)
    return (tuple(sorted(isolated)), tuple(splits))


def batches_for(inventory: Inventory, elements: Sequence[str]) -> tuple[Batch, ...]:
    """Pack ``elements`` into maintenance windows, worst-first within each.

    Args:
        inventory: The declared tree the blast radii are measured in.
        elements: Fully-qualified names, in any order; the packing sorts them.
    """
    radii = {element: blast_radius(inventory, element) for element in sorted(set(elements))}

    packed: list[list[str]] = []
    reach: list[set[str]] = []
    for element, (isolated, _splits) in sorted(radii.items()):
        footprint = {element, *isolated}
        for index, taken in enumerate(reach):
            if not (footprint & taken):
                packed[index].append(element)
                taken |= footprint
                break
        else:
            packed.append([element])
            reach.append(set(footprint))

    return tuple(
        Batch(
            index=index,
            elements=tuple(sorted(group)),
            isolated=tuple(sorted({name for member in group for name in radii[member][0]})),
            splits=tuple(dict.fromkeys(line for member in group for line in radii[member][1])),
            note=_note(index, group, radii),
        )
        for index, group in enumerate(packed)
    )


def _note(
    index: int,
    group: Sequence[str],
    radii: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
) -> str:
    """Why this batch is on its own, when that needs explaining.

    Only a *later* batch holding a single device is worth a sentence: it is
    there because its blast radius overlapped every batch before it, and an
    operator looking at a window with one device in it deserves to know that it
    is a consequence rather than an accident.
    """
    if index == 0 or len(group) > 1:
        return ""
    only = group[0]
    isolated, splits = radii[only]
    if not isolated and not splits:  # pragma: no cover - a clean device always fits batch 0
        return ""
    return (
        f"on its own: working {only} takes {len(isolated)} element(s) with it, which overlaps "
        "every batch before this one"
    )
