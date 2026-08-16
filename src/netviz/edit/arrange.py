"""Tidying a selection: align, distribute and snap to a grid.

The three gestures every diagram editor has and none of them means anything
about *one* shape. Aligning needs a set to agree on an edge, distributing needs
a set to share the space between its extremes, and snapping is the one that
works on a single element but is only ever asked for on a handful.

They are here rather than in the browser for the same reason every other
mutation is: an arrangement lives in ``kind: layout`` documents (§18), and
deciding which document holds which node — and writing back only the entries
that moved, keeping the comments and the spellings of the ones that did not —
is the mutation layer's job. What the page sends is *which elements* and *which
tidying*; what comes back is a diff.

The shape of the answer
-----------------------

One :class:`~netviz.edit.operations.SetGeometry` per layout document that
loses an entry, each carrying that document's whole ``nodes`` section for the
view. Whole, because ``SetGeometry`` replaces a section rather than merging into
it — and the replacement is itself a keyed merge, so an entry whose coordinates
did not change is left exactly as it was written, comment and all. Two hundred
nodes aligned in a tree with three layout documents is three operations and
three touched files, not two hundred.

Coordinates are netviz's throughout: points, ``y`` upwards, a position being
the **centre** of what it places. So "top" is the largest ``y``, which is the
one thing in this file worth reading twice.

A node whose entry stores no ``size`` is treated as a point. That is the honest
reading — the size is a consequence of the label and the arrangement did not
decide it — and it degrades exactly the way it should: aligning left a set of
unsized nodes aligns their centres, which is what a set of unsized nodes has.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from netviz.edit.errors import EditError
from netviz.edit.operations import Operation, SetGeometry
from netviz.layout.document import inline_entry
from netviz.layout.geometry import DEFAULT_GRID, Placement, round_coordinate
from netviz.layout.resolve import resolve_key
from netviz.loader.inventory import Inventory, namespace_of, short_name

__all__ = [
    "ALIGNMENTS",
    "ARRANGEMENTS",
    "DEFAULT_GRID",
    "DISTRIBUTIONS",
    "arrange_operations",
    "describe_arrangement",
]

#: How a selection may be aligned. The first three settle the ``x`` axis and the
#: last three the ``y`` axis, so "left" and "top" are not opposites: they are
#: the two axes' minima and maxima respectively.
ALIGNMENTS: Final[tuple[str, ...]] = ("left", "centre", "right", "top", "middle", "bottom")

#: How a selection may be spread out.
DISTRIBUTIONS: Final[tuple[str, ...]] = ("horizontal", "vertical")

#: Every command this module answers, as the page and the palette name them.
ARRANGEMENTS: Final[tuple[str, ...]] = (
    *(f"align.{name}" for name in ALIGNMENTS),
    *(f"distribute.{name}" for name in DISTRIBUTIONS),
    "snap",
)

#: How many nodes one command may move. A guard rather than a policy: the
#: operation carries every entry of the sections it rewrites, and a selection
#: larger than this is a request to re-run ``netviz layout``, not to nudge.
MAX_SELECTION: Final = 2000


def describe_arrangement(command: str, count: int) -> str:
    """One line naming what a tidying did, for the undo stack and the journal."""
    subject = f"{count} element{'' if count == 1 else 's'}"
    if command == "snap":
        return f"snap {subject} to the grid"
    kind, _, name = command.partition(".")
    if kind == "align":
        return f"align {subject} {name}"
    return f"distribute {subject} {name}ly"


@dataclass(frozen=True, slots=True)
class _Placed:
    """One node's stored position, and which document's key holds it."""

    #: Fully-qualified name of the layout document.
    layout: str
    #: The key exactly as that document writes it, which is what has to be
    #: written back: re-spelling ``sw-core`` as ``sites/hq/sw-core`` would be a
    #: change to every line of a file for a gesture that moved one node.
    key: str
    placement: Placement

    @property
    def left(self) -> float:
        return self.placement.x - (self.placement.width or 0.0) / 2

    @property
    def right(self) -> float:
        return self.placement.x + (self.placement.width or 0.0) / 2

    @property
    def bottom(self) -> float:
        return self.placement.y - (self.placement.height or 0.0) / 2

    @property
    def top(self) -> float:
        return self.placement.y + (self.placement.height or 0.0) / 2

    @property
    def width(self) -> float:
        return self.placement.width or 0.0

    @property
    def height(self) -> float:
        return self.placement.height or 0.0

    def moved(self, *, x: float | None = None, y: float | None = None) -> _Placed:
        placement = Placement(
            x=round_coordinate(self.placement.x if x is None else x),
            y=round_coordinate(self.placement.y if y is None else y),
            width=self.placement.width,
            height=self.placement.height,
        )
        return _Placed(layout=self.layout, key=self.key, placement=placement)


def arrange_operations(
    inventory: Inventory,
    *,
    command: str,
    view: str,
    addresses: Sequence[str],
    grid: float = DEFAULT_GRID,
) -> tuple[Operation, ...]:
    """The operations that tidy ``addresses`` in ``view``.

    Args:
        inventory: The loaded tree, whose layout documents hold the arrangement.
        command: One of :data:`ARRANGEMENTS`.
        view: The layer being arranged, as ``docs/schema.md`` §18 spells it.
        addresses: What the user selected. Anything the view does not place is
            reported rather than skipped, because a gesture that silently moved
            four of the six things you picked is worse than one that refused.
        grid: Pitch in points for ``snap``.

    Returns:
        One operation per layout document that loses an entry, in document
        order. Empty when nothing would move — aligning an already-aligned row
        writes no file, which is what keeps the gesture safe to repeat.

    Raises:
        EditError: The command is not one of :data:`ARRANGEMENTS`, the grid is
            not a usable pitch, or the selection cannot be arranged.
    """
    if command not in ARRANGEMENTS:
        raise EditError(
            f"unknown arrangement {command!r}; expected one of {', '.join(ARRANGEMENTS)}"
        )
    if command == "snap" and not grid > 0:
        raise EditError(f"the grid pitch must be a positive number of points, got {grid}")
    wanted = _unique(addresses)
    if not wanted:
        raise EditError("nothing is selected, so there is nothing to arrange")
    if len(wanted) > MAX_SELECTION:
        raise EditError(
            f"{len(wanted)} elements is more than one arrangement should move at once "
            f"(the limit is {MAX_SELECTION}); re-run 'netviz layout --write' instead"
        )

    placements = _placements(inventory, view)
    chosen = _chosen(wanted, placements, inventory=inventory)
    least = 1 if command == "snap" else 2
    if len(chosen) < least:
        raise EditError(
            f"{command} needs at least {least} placed element"
            f"{'' if least == 1 else 's'} in the {view} view; "
            f"{len(chosen)} of the {len(wanted)} selected {'is' if len(chosen) == 1 else 'are'} "
            f"placed"
        )

    moved = _moved(command, chosen, grid=grid)
    return _operations(inventory, view=view, moved=moved)


def _unique(addresses: Iterable[str]) -> tuple[str, ...]:
    """The selection, de-duplicated, in the order it was given."""
    seen: dict[str, None] = {}
    for address in addresses:
        text = str(address).strip()
        if text:
            seen.setdefault(text, None)
    return tuple(seen)


def _placements(inventory: Inventory, view: str) -> dict[str, _Placed]:
    """Every node ``view`` places, keyed by the node id the drawing gives it.

    First document wins, which is the rule
    :func:`~netviz.layout.resolve.resolve_geometry` applies when it merges the
    same documents into the arrangement the renderer draws. Two files placing
    one node is already a reported conflict; this must not disagree with the
    picture about which of them the reader is looking at.
    """
    found: dict[str, _Placed] = {}
    for fqn, layout in inventory.layouts.items():
        geometry = layout.view(view)
        if geometry is None:
            continue
        namespace = namespace_of(fqn)
        for key, node in geometry.nodes.items():
            resolved = resolve_key(key, inventory=inventory, namespace=namespace)
            found.setdefault(
                resolved, _Placed(layout=fqn, key=key, placement=Placement.from_model(node))
            )
    return found


def _chosen(
    wanted: Sequence[str], placements: Mapping[str, _Placed], *, inventory: Inventory
) -> tuple[_Placed, ...]:
    """The selected nodes that the view actually places, in selection order."""
    chosen: list[_Placed] = []
    for address in wanted:
        placed = placements.get(address)
        if placed is None:
            # The page sends the address the record carries; a caller typing a
            # short name has not resolved it, and ``_placements`` is keyed by
            # the resolved id.
            resolved = resolve_key(address, inventory=inventory, namespace="")
            placed = placements.get(resolved)
        if placed is not None:
            chosen.append(placed)
    return tuple(chosen)


def _moved(command: str, chosen: Sequence[_Placed], *, grid: float) -> tuple[_Placed, ...]:
    """Where each of them ends up."""
    kind, _, name = command.partition(".")
    if command == "snap":
        return tuple(
            placed.moved(x=_snapped(placed.placement.x, grid), y=_snapped(placed.placement.y, grid))
            for placed in chosen
        )
    if kind == "align":
        return _aligned(name, chosen)
    return _distributed(name, chosen)


def _snapped(value: float, grid: float) -> float:
    return round(value / grid) * grid


def _aligned(name: str, chosen: Sequence[_Placed]) -> tuple[_Placed, ...]:
    """Every node moved onto one edge, or onto the selection's own axis."""
    if name == "left":
        edge = min(placed.left for placed in chosen)
        return tuple(placed.moved(x=edge + placed.width / 2) for placed in chosen)
    if name == "right":
        edge = max(placed.right for placed in chosen)
        return tuple(placed.moved(x=edge - placed.width / 2) for placed in chosen)
    if name == "centre":
        axis = (min(placed.left for placed in chosen) + max(placed.right for placed in chosen)) / 2
        return tuple(placed.moved(x=axis) for placed in chosen)
    if name == "top":
        # ``y`` grows upwards, so the top of the selection is its maximum.
        edge = max(placed.top for placed in chosen)
        return tuple(placed.moved(y=edge - placed.height / 2) for placed in chosen)
    if name == "bottom":
        edge = min(placed.bottom for placed in chosen)
        return tuple(placed.moved(y=edge + placed.height / 2) for placed in chosen)
    axis = (min(placed.bottom for placed in chosen) + max(placed.top for placed in chosen)) / 2
    return tuple(placed.moved(y=axis) for placed in chosen)


def _distributed(name: str, chosen: Sequence[_Placed]) -> tuple[_Placed, ...]:
    """Equal gaps between the boxes, the two extremes left where they are.

    Gaps rather than centres, because a row of a 40-point node and a 200-point
    aggregate looks evenly spaced only when the *space between them* is even —
    which is what a reader means by "distribute". With no stored sizes every gap
    is between two points and the result is equally spaced centres, which is the
    same answer for the same reason.
    """
    horizontal = name == "horizontal"
    ordered = sorted(
        chosen, key=lambda placed: (placed.left if horizontal else placed.bottom, placed.key)
    )
    if len(ordered) < 3:
        # Two nodes are already distributed between themselves, and one is not a
        # distribution at all. Nothing to write.
        return ()
    extent = (
        (ordered[-1].right - ordered[0].left)
        if horizontal
        else (ordered[-1].top - ordered[0].bottom)
    )
    occupied = sum(placed.width if horizontal else placed.height for placed in ordered)
    gap = (extent - occupied) / (len(ordered) - 1)
    moved: list[_Placed] = []
    cursor = ordered[0].left if horizontal else ordered[0].bottom
    for placed in ordered:
        size = placed.width if horizontal else placed.height
        centre = cursor + size / 2
        moved.append(placed.moved(x=centre) if horizontal else placed.moved(y=centre))
        cursor += size + gap
    return tuple(moved)


def _operations(
    inventory: Inventory, *, view: str, moved: Sequence[_Placed]
) -> tuple[Operation, ...]:
    """One ``set-geometry`` per layout document that has something to say.

    A document whose nodes all landed where they already were is left out
    entirely, so a repeated align is a no-op rather than a second identical
    entry in the undo stack.
    """
    by_layout: dict[str, dict[str, _Placed]] = {}
    for placed in moved:
        by_layout.setdefault(placed.layout, {})[placed.key] = placed

    operations: list[Operation] = []
    for fqn, layout in inventory.layouts.items():
        changes = by_layout.get(fqn)
        geometry = layout.view(view)
        if not changes or geometry is None:
            continue
        nodes: dict[str, object] = {}
        touched = False
        for key, node in geometry.nodes.items():
            placement = Placement.from_model(node)
            replacement = changes.get(key)
            if replacement is not None and replacement.placement.position != placement.position:
                placement = replacement.placement
                touched = True
            nodes[key] = inline_entry(placement.to_model().model_dump(exclude_none=True))
        if not touched:
            continue
        operations.append(
            SetGeometry(
                view=view,
                nodes=nodes,
                layout=short_name(fqn),
                namespace=namespace_of(fqn),
            )
        )
    return tuple(operations)
