"""Reading coordinates back out of Graphviz, and putting an engine's answer on
the stored coordinate system.

Two pure jobs, deliberately kept away from anything that runs a subprocess so
that both are testable without Graphviz installed:

**Parsing.** Graphviz's ``-Tjson`` output is the one format that carries
everything an arrangement needs — node centres *and* their sizes, cluster
bounding boxes, and edge spline control points — in points, in the coordinate
system netgraph stores. ``-Tplain`` reports inches and no cluster boxes;
``-Tdot`` would mean writing a second DOT parser. So: JSON.

**Fitting.** When only some nodes are placed, they are pinned and the engine
places the rest around them — but ``neato`` returns the whole drawing scaled and
translated onto its own canvas, so the pinned nodes come back somewhere else.
The transform is a *similarity* with no rotation (Graphviz never rotates a
layout unless asked), which two pinned nodes determine exactly. Recovering it
and inverting it puts the newly-placed nodes back on the stored coordinate
system, where the pinned ones are still exactly where the user left them.

That is what makes a partial arrangement honest: a node the user dragged does
not move when a colleague adds a switch somewhere else in the tree.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from netgraph.layout.geometry import Box, Placement

__all__ = [
    "POINTS_PER_INCH",
    "SEPARATION_PADDING",
    "Drawing",
    "DrawingError",
    "Transform",
    "fit_transform",
    "parse_drawing",
    "realign",
    "separate",
]

#: Graphviz reports a node's ``width`` and ``height`` in inches and its ``pos``
#: in points, in the same document.
POINTS_PER_INCH: Final = 72.0


class DrawingError(ValueError):
    """Graphviz produced something this module cannot read."""


@dataclass(frozen=True, slots=True)
class Drawing:
    """What one Graphviz run placed where."""

    #: Node id to centre and size, in points.
    nodes: Mapping[str, Placement] = field(default_factory=dict)
    #: Spline control points per edge, keyed by the edge's ``id`` attribute.
    #: Keyed rather than positional because Graphviz does **not** emit edges in
    #: declaration order — it walks them per node — so matching by index pairs a
    #: spline with the wrong link, which draws a line across the diagram and
    #: still parses. An edge with no ``id`` is not reported at all.
    edges: Mapping[str, tuple[tuple[float, float], ...]] = field(default_factory=dict)
    #: Subgraph name (``cluster_0``) to its bounding box.
    clusters: Mapping[str, Box] = field(default_factory=dict)
    #: The drawing's own bounding box, when it reported one.
    bounds: Box | None = None


def parse_drawing(payload: bytes) -> Drawing:
    """Read a Graphviz ``-Tjson`` document into a :class:`Drawing`.

    Raises:
        DrawingError: The payload is not the JSON Graphviz emits.
    """
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrawingError(f"Graphviz did not produce readable JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DrawingError("Graphviz did not produce a JSON object")

    nodes: dict[str, Placement] = {}
    clusters: dict[str, Box] = {}
    for obj in document.get("objects") or ():
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        if not isinstance(name, str):
            continue
        position = _point(obj.get("pos"))
        if position is not None:
            nodes[name] = Placement(
                x=position[0],
                y=position[1],
                width=_inches(obj.get("width")),
                height=_inches(obj.get("height")),
            )
            continue
        box = _box(obj.get("bb"))
        if box is not None:
            clusters[name] = box

    edges: dict[str, tuple[tuple[float, float], ...]] = {}
    for edge in document.get("edges") or ():
        if not isinstance(edge, dict):
            continue
        identity = edge.get("id")
        spline = _spline(edge.get("pos"))
        if isinstance(identity, str) and spline:
            edges[identity] = spline
    return Drawing(nodes=nodes, edges=edges, clusters=clusters, bounds=_box(document.get("bb")))


def _point(value: Any) -> tuple[float, float] | None:
    """``"43,162"`` as a pair, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        return (float(parts[0]), float(parts[1]))
    except ValueError:
        return None


def _box(value: Any) -> Box | None:
    """``"l,b,r,t"`` as a box, or ``None``."""
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 4:
        return None
    try:
        left, bottom, right, top = (float(part) for part in parts)
    except ValueError:
        return None
    return Box.from_bounds(left, bottom, right, top)


def _spline(value: Any) -> tuple[tuple[float, float], ...]:
    """An edge's ``pos`` as control points, dropping the arrowhead markers.

    Graphviz prefixes an endpoint with ``s,`` or ``e,`` when the spline is
    clipped to an arrowhead. netgraph draws undirected links, so those normally
    do not appear; they are skipped rather than trusted, because a bare ``pos``
    with them in would otherwise be read as two extra bends.
    """
    if not isinstance(value, str):
        return ()
    points: list[tuple[float, float]] = []
    for token in value.split():
        parts = token.split(",")
        if len(parts) == 3 and parts[0] in ("s", "e"):
            continue
        point = _point(token)
        if point is not None:
            points.append(point)
    return tuple(points)


def _inches(value: Any) -> float | None:
    """A Graphviz size in inches, as points."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) * POINTS_PER_INCH
    if isinstance(value, str):
        try:
            return float(value) * POINTS_PER_INCH
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Putting an engine's answer back on the stored coordinate system
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Transform:
    """A uniform scale and a translation: ``out = scale * stored + (dx, dy)``."""

    scale: float = 1.0
    dx: float = 0.0
    dy: float = 0.0

    @property
    def is_identity(self) -> bool:
        return self.scale == 1.0 and self.dx == 0.0 and self.dy == 0.0

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (self.scale * x + self.dx, self.scale * y + self.dy)

    def invert(self, x: float, y: float) -> tuple[float, float]:
        """Where a drawn point sits on the stored coordinate system."""
        return ((x - self.dx) / self.scale, (y - self.dy) / self.scale)

    def scale_length(self, value: float) -> float:
        """A length drawn at ``value``, on the stored coordinate system."""
        return value / self.scale


#: Below this, two pinned nodes are on top of each other and the scale they
#: imply is noise rather than information.
_MIN_SPREAD: Final = 1e-6


def fit_transform(
    pairs: Sequence[tuple[tuple[float, float], tuple[float, float]]],
) -> Transform:
    """The similarity taking stored positions to where Graphviz drew them.

    Args:
        pairs: ``(stored, drawn)`` for every node whose position was pinned.

    Returns:
        The transform. With no pairs, the identity — there is nothing to align
        to, so the engine's own canvas is the answer. With one pair, or with
        several that Graphviz drew on top of each other, a pure translation:
        one point fixes the origin but says nothing about scale, and inventing
        one from a division by nearly zero would throw the drawing across the
        canvas.

    Least squares over a uniform scale, which for a similarity with no rotation
    has the closed form below: the scale is the ratio of the two point clouds'
    spread about their means, and the translation is what maps one mean onto the
    other. Two pinned nodes determine it exactly; more than two over-determine
    it and the fit averages out Graphviz's rounding.
    """
    if not pairs:
        return Transform()
    count = len(pairs)
    stored_x = sum(stored[0] for stored, _ in pairs) / count
    stored_y = sum(stored[1] for stored, _ in pairs) / count
    drawn_x = sum(drawn[0] for _, drawn in pairs) / count
    drawn_y = sum(drawn[1] for _, drawn in pairs) / count

    stored_spread = sum(
        (stored[0] - stored_x) ** 2 + (stored[1] - stored_y) ** 2 for stored, _ in pairs
    )
    drawn_spread = sum((drawn[0] - drawn_x) ** 2 + (drawn[1] - drawn_y) ** 2 for _, drawn in pairs)
    if stored_spread <= _MIN_SPREAD or drawn_spread <= _MIN_SPREAD:
        return Transform(scale=1.0, dx=drawn_x - stored_x, dy=drawn_y - stored_y)

    scale = math.sqrt(drawn_spread / stored_spread)
    return Transform(scale=scale, dx=drawn_x - scale * stored_x, dy=drawn_y - scale * stored_y)


def realign(
    drawing: Drawing, stored: Mapping[str, Placement], keys: Iterable[str]
) -> dict[str, Placement]:
    """Every node Graphviz drew, expressed on the stored coordinate system.

    ``keys`` names the nodes whose positions were pinned; they are what the
    transform is fitted from. A pinned node comes back at exactly its stored
    position rather than at the fit's prediction of it, because the arrangement
    is the truth and the fit is only how the rest of the drawing is brought to
    meet it.
    """
    pairs = [
        (stored[key].position, drawing.nodes[key].position)
        for key in keys
        if key in stored and key in drawing.nodes
    ]
    transform = fit_transform(pairs)
    placed: dict[str, Placement] = {}
    for name, placement in drawing.nodes.items():
        if name in stored:
            # The size still comes from the drawing: a stored placement carries
            # a position and usually no size, and the box is what separation
            # needs. Graphviz derives it from the label, so it is the same box
            # the next render will draw.
            placed[name] = Placement(
                x=stored[name].x,
                y=stored[name].y,
                width=stored[name].width or placement.width,
                height=stored[name].height or placement.height,
            )
            continue
        x, y = transform.invert(placement.x, placement.y)
        # The *size* is not transformed. Graphviz scales positions to pull nodes
        # apart but never the boxes themselves — a label is as wide as it is —
        # so a width divided by the fit would be a box that does not exist.
        placed[name] = Placement(x=x, y=y, width=placement.width, height=placement.height)
    return separate(placed, fixed=set(stored))


# --------------------------------------------------------------------------- #
# Separation
# --------------------------------------------------------------------------- #

#: Points left between two boxes that had to be pushed apart. Graphviz's own
#: ``nodesep`` default is 0.25in; this is a little under half of that, because
#: it is a repair rather than a layout parameter and the aim is "not touching",
#: not "evenly spaced".
SEPARATION_PADDING: Final = 8.0

#: How many times the separation pass may sweep every pair. Each sweep resolves
#: every overlap it sees, so it converges in a handful; the bound is there so a
#: pathological arrangement costs a bounded amount rather than hanging a render.
SEPARATION_PASSES: Final = 24


def separate(
    nodes: Mapping[str, Placement],
    *,
    fixed: Iterable[str] = (),
    padding: float = SEPARATION_PADDING,
) -> dict[str, Placement]:
    """Push overlapping nodes apart, moving only the ones that may move.

    Undoing Graphviz's scale is what makes this necessary. ``neato`` separates
    an overlapping layout by scaling the whole drawing up; putting the drawing
    back on the stored coordinate system undoes that scale — it has to, or the
    nodes somebody placed by hand would not be where they were placed — and the
    overlap the scale was removing comes back with it. Only the *positions* are
    scaled, though. A label is as wide as it is, so the boxes are known exactly,
    and separating them is a small, deterministic repair rather than a layout.

    ``fixed`` names the nodes that must not move: an arrangement is a decision,
    and a node placed by hand does not get shoved aside to make room for one the
    engine placed. Two fixed nodes that overlap each other are left overlapping,
    for the same reason.

    Returns:
        Every node, with the free ones moved as little as the overlaps allow.
    """
    pinned = set(fixed)
    moved = dict(nodes)
    order = sorted(moved)
    for _ in range(SEPARATION_PASSES):
        collisions = 0
        for index, first in enumerate(order):
            for second in order[index + 1 :]:
                if first in pinned and second in pinned:
                    continue
                shift = _overlap(moved[first], moved[second], padding)
                if shift is None:
                    continue
                collisions += 1
                _push(moved, first, second, shift, pinned)
        if not collisions:
            break
    return moved


def _overlap(a: Placement, b: Placement, padding: float) -> tuple[float, float] | None:
    """How far ``b`` must move off ``a``, or ``None`` if the boxes are clear.

    The answer is a shift along the axis of *least* penetration, which is what
    keeps the repair minimal: two nodes side by side are pushed sideways rather
    than one of them thrown over the other.
    """
    half_width = (_extent(a.width) + _extent(b.width)) / 2 + padding
    half_height = (_extent(a.height) + _extent(b.height)) / 2 + padding
    dx = b.x - a.x
    dy = b.y - a.y
    penetration_x = half_width - abs(dx)
    penetration_y = half_height - abs(dy)
    if penetration_x <= 0 or penetration_y <= 0:
        return None
    if penetration_x <= penetration_y:
        # A dead-centre collision has no direction to escape along; break the
        # tie the same way every time, or two runs would separate differently.
        return (penetration_x if dx >= 0 else -penetration_x, 0.0)
    return (0.0, penetration_y if dy >= 0 else -penetration_y)


def _push(
    nodes: dict[str, Placement],
    first: str,
    second: str,
    shift: tuple[float, float],
    pinned: set[str],
) -> None:
    """Apply a separation, split between the two unless one of them is fixed."""
    dx, dy = shift
    if first in pinned:
        nodes[second] = _moved(nodes[second], dx, dy)
    elif second in pinned:
        nodes[first] = _moved(nodes[first], -dx, -dy)
    else:
        nodes[first] = _moved(nodes[first], -dx / 2, -dy / 2)
        nodes[second] = _moved(nodes[second], dx / 2, dy / 2)


def _moved(placement: Placement, dx: float, dy: float) -> Placement:
    return Placement(
        x=placement.x + dx, y=placement.y + dy, width=placement.width, height=placement.height
    )


#: The box a node with no known size is separated as. Only reachable for a
#: drawing Graphviz reported without dimensions, which it does not do; the
#: value keeps the arithmetic total rather than describing anything real.
_DEFAULT_EXTENT: Final = 54.0


def _extent(value: float | None) -> float:
    return _DEFAULT_EXTENT if value is None else value
