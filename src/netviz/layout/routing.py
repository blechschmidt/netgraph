"""Turning a pinned link into the actual line that gets drawn.

A stored arrangement says where the *nodes* go (:mod:`netviz.layout.geometry`)
and, for a link somebody has routed by hand, which bends it goes through. It
does not say what the line between those points looks like — that is this
module, and it is deliberately the only place that decides, because three
different things draw the same cable and a reader must not be able to tell them
apart:

* the DOT renderer, which writes the route into a Graphviz ``pos`` attribute so
  ``netviz render`` reproduces it;
* the JSON export, which publishes it for anything drawing the graph itself;
* the editor canvas, which draws the route live under a dragging cursor and
  must land on the same pixels the render will.

The canvas mirrors :func:`route` in JavaScript (``web/assets/links.js``), and
``tests/test_browser.py`` runs the two against each other on a table of cases,
so the mirror cannot drift silently. Everything here is therefore written to be
mirrored: no floating-point luck, no library calls, fixed iteration counts.

The shape of an answer
----------------------

A :class:`Route` carries the same line twice, because its two consumers want
different forms of it:

``corners``
    The polyline the route follows — what an SVG ``path`` draws and what the
    handles in the editor sit on.
``controls``
    The same line as a sequence of cubic Bézier control points, ``3n + 1`` of
    them, which is what a Graphviz ``pos`` attribute is. A straight segment
    becomes a cubic whose control points are its own thirds, so a polyline and
    a curve go down one path and Graphviz draws both exactly.

Coordinates are points, ``y`` upwards, the same system as everything else in
:mod:`netviz.layout` — Graphviz's own, so a route can be handed straight
back to it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

from netviz.layout.geometry import (
    DEFAULT_ROUTING,
    LabelPlacement,
    Placement,
    Routing,
    round_coordinate,
)

__all__ = [
    "DEFAULT_NODE_SIZE",
    "FAN_GAP",
    "Anchor",
    "Route",
    "fan_offsets",
    "label_position",
    "route",
]

#: The box a node is assumed to occupy when the arrangement does not record its
#: size. Graphviz's own default node — 0.75 by 0.5 inches — because a guess that
#: is too *small* leaves a stub of line inside the shape, which reads as a
#: mistake, while one that is too large leaves a gap, which reads as a style.
DEFAULT_NODE_SIZE: Final[tuple[float, float]] = (54.0, 36.0)

#: How far a route stops short of the box it runs into, in points. Graphviz
#: draws an edge right up to the shape; a hand-routed one is clipped a hair
#: earlier so that a right-angled approach does not sit on the border line.
CLEARANCE: Final = 1.0

#: How far apart two parallel cables are fanned, in points. Wide enough that
#: each has a grab handle of its own at a normal zoom, narrow enough that four
#: of them still read as one trunk.
FAN_GAP: Final = 14.0

#: How far a self-link stands off its own node, in points.
LOOP_REACH: Final = 40.0

#: How many straight pieces one cubic is flattened into when a position along
#: the route is wanted. Twelve is well past the point where a label stops
#: visibly moving, and a fixed count is what lets the JavaScript mirror agree
#: to the last decimal place.
FLATTEN_STEPS: Final = 12

#: Distances below this are treated as zero: two points this close together are
#: the same point, and dividing by the gap between them is how a route becomes
#: ``nan``.
EPSILON: Final = 1e-9

#: How far outside a border a point may land and still count as on it. A
#: thousandth of a point is far below anything that can be drawn and far above
#: the error of dividing and multiplying a coordinate back; see :func:`_crossing`,
#: which is the one place it matters and the one place it is used.
TOUCH: Final = 1e-3


@dataclass(frozen=True, slots=True)
class Anchor:
    """One end of a route: where a node is, and how big a box to leave it from."""

    x: float
    y: float
    width: float = DEFAULT_NODE_SIZE[0]
    height: float = DEFAULT_NODE_SIZE[1]
    #: Was the size known, or is it :data:`DEFAULT_NODE_SIZE` standing in for
    #: one? A route drawn against a guessed box is still drawn — it is far
    #: better than none — but the renderer says so, because the fix is a
    #: one-line ``netviz layout --write``.
    measured: bool = True

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x, self.y)

    @classmethod
    def of(cls, placement: Placement) -> Anchor:
        """The anchor one stored placement describes."""
        if placement.width is None or placement.height is None:
            return cls(
                x=placement.x,
                y=placement.y,
                width=DEFAULT_NODE_SIZE[0],
                height=DEFAULT_NODE_SIZE[1],
                measured=False,
            )
        return cls(x=placement.x, y=placement.y, width=placement.width, height=placement.height)

    def contains(self, x: float, y: float) -> bool:
        """Is this point inside the box, clearance included?"""
        return (
            abs(x - self.x) <= self.width / 2 + CLEARANCE
            and abs(y - self.y) <= self.height / 2 + CLEARANCE
        )


@dataclass(frozen=True, slots=True)
class Route:
    """One link's line, in the two forms its consumers want."""

    #: The polyline the route follows, source end first.
    corners: tuple[tuple[float, float], ...]
    #: The same line as cubic Bézier control points: ``3n + 1`` of them.
    controls: tuple[tuple[float, float], ...]
    #: The style it was drawn in, so a caller can report what it did.
    routing: Routing = DEFAULT_ROUTING

    @property
    def is_empty(self) -> bool:
        return len(self.corners) < 2

    def rounded(self) -> Route:
        """This route at the precision coordinates are stored and compared at."""
        return Route(
            corners=tuple((round_coordinate(x), round_coordinate(y)) for x, y in self.corners),
            controls=tuple((round_coordinate(x), round_coordinate(y)) for x, y in self.controls),
            routing=self.routing,
        )

    def position(self, at: float) -> tuple[float, float]:
        """The point ``at`` of the way along the drawn line, by arc length.

        Along the *curve*, not along the control polygon: a label pinned half
        way down a spline has to sit on the spline, or dragging it in the
        editor and rendering it from the command line would disagree by however
        much the curve bulges.
        """
        return _walk(_flatten(self.controls), at)


def route(
    source: Anchor,
    target: Anchor,
    *,
    waypoints: Sequence[tuple[float, float]] = (),
    style: Routing = DEFAULT_ROUTING,
    fan: float = 0.0,
) -> Route:
    """The line one link is drawn as.

    Args:
        source: Where the link starts, and the box it must not run into.
        target: Where it ends. The same anchor as ``source`` means a self-link,
            which is drawn as a loop standing off the node rather than as a
            line of zero length.
        waypoints: The bends it is pinned through, **interior points only**, in
            the graph's endpoint order.
        style: One of :class:`~netviz.layout.geometry.Routing`.
        fan: How far to displace the line sideways, in points, so that two
            cables between the same pair of devices can be told apart and
            grabbed separately. Ignored once a link has waypoints: those say
            where the cable goes, and nudging it off them would make dragging a
            bend not move the line to where it was dropped.

    Returns:
        The route. A link whose two ends are in the same place, with nothing
        pinned to pull it out of there, comes back empty — there is no line to
        draw and nothing sensible to draw it as.
    """
    pinned = tuple((float(x), float(y)) for x, y in waypoints)
    if _same_node(source, target):
        spine = _loop_spine(source, pinned, fan=fan)
    else:
        spine = (source.centre, *pinned, target.centre)
        if not pinned and fan:
            spine = _fanned(spine, fan)
    shaped = _shaped(spine, style)
    clipped = _clip(shaped, source, target, loop=_same_node(source, target))
    if len(clipped) < 2:
        return Route(corners=(), controls=(), routing=style)
    controls = _controls(clipped, style)
    return Route(corners=clipped, controls=controls, routing=style)


def label_position(line: Route, label: LabelPlacement | None) -> tuple[float, float] | None:
    """Where a link's annotation goes, or ``None`` when nothing pins it.

    Half way along with no offset is what a renderer does by itself, so it is
    reported as "nothing pinned" rather than as a coordinate: emitting the
    position Graphviz would have chosen anyway is a number in a file that can
    only ever go stale.
    """
    if label is None or line.is_empty:
        return None
    if label.at == 0.5 and not label.dx and not label.dy:
        return None
    x, y = line.position(label.at)
    return (x + label.dx, y + label.dy)


def fan_offsets(count: int, *, gap: float = FAN_GAP) -> tuple[float, ...]:
    """How far each of ``count`` parallel links is pushed off the centre line.

    Centred on zero, so a lone link is not moved at all and an odd-numbered
    bundle keeps one cable on the direct line between the two devices — which
    is the one a reader traces first.
    """
    if count <= 1:
        return (0.0,) * max(count, 0)
    middle = (count - 1) / 2
    return tuple((index - middle) * gap for index in range(count))


# --------------------------------------------------------------------------- #
# Building the spine
# --------------------------------------------------------------------------- #


def _same_node(source: Anchor, target: Anchor) -> bool:
    return abs(source.x - target.x) < EPSILON and abs(source.y - target.y) < EPSILON


def _loop_spine(
    node: Anchor, waypoints: Sequence[tuple[float, float]], *, fan: float
) -> tuple[tuple[float, float], ...]:
    """A self-link: out of the node, around, and back into it.

    Drawn *above* the node, and further above for each additional loop, so that
    four VLANs terminating on one switch are four rings rather than one thick
    one. A loop somebody has dragged bends through is drawn through them
    instead, which is the same promise every other link makes.
    """
    if waypoints:
        return (node.centre, *waypoints, node.centre)
    # Each further loop stands off further *and* reaches wider, so a stack of
    # them nests visibly. Height alone would draw one ring inside another with
    # the same two ends, which at a normal zoom is one thick line.
    reach = node.height / 2 + LOOP_REACH + abs(fan)
    spread = max(node.width / 4, FAN_GAP) + abs(fan) / 2
    return (
        node.centre,
        (node.x - spread, node.y + reach),
        (node.x + spread, node.y + reach),
        node.centre,
    )


def _fanned(spine: Sequence[tuple[float, float]], offset: float) -> tuple[tuple[float, float], ...]:
    """A two-point spine bowed sideways, so parallel links can be told apart."""
    (x1, y1), (x2, y2) = spine[0], spine[-1]
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < EPSILON:
        return tuple(spine)
    # The normal, not the tangent: the bow is *across* the run, which is what
    # keeps every cable in a bundle the same length to the eye.
    nx, ny = -dy / length, dx / length
    middle = ((x1 + x2) / 2 + nx * offset, (y1 + y2) / 2 + ny * offset)
    return ((x1, y1), middle, (x2, y2))


# --------------------------------------------------------------------------- #
# Applying the style
# --------------------------------------------------------------------------- #


def _shaped(
    spine: Sequence[tuple[float, float]], style: Routing
) -> tuple[tuple[float, float], ...]:
    """The polyline ``style`` makes of the points the route has to pass through."""
    points = _dedupe(spine)
    if len(points) < 2 or style is not Routing.ORTHOGONAL:
        return points
    return _dedupe(_orthogonal(points))


def _orthogonal(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """The same route with every leg broken into horizontal and vertical runs.

    Two rules, and which applies depends on whether anybody has said where the
    cable goes:

    * **nothing pinned** — a Z. The line runs half way along its dominant axis,
      crosses, and finishes: the shape a patch schedule is drawn in, and the
      one that stays symmetrical when either end moves.
    * **bends pinned** — an L per leg, turning along the leg's own dominant
      axis first. Locally decided, so dragging one bend cannot re-shape the
      leg on the far side of the route.
    """
    if len(points) == 2:
        (x1, y1), (x2, y2) = points
        if abs(x2 - x1) >= abs(y2 - y1):
            middle = (x1 + x2) / 2
            return [(x1, y1), (middle, y1), (middle, y2), (x2, y2)]
        middle = (y1 + y2) / 2
        return [(x1, y1), (x1, middle), (x2, middle), (x2, y2)]

    elbowed: list[tuple[float, float]] = [points[0]]
    for start, end in pairwise(points):
        (x1, y1), (x2, y2) = start, end
        elbow = (x2, y1) if abs(x2 - x1) >= abs(y2 - y1) else (x1, y2)
        elbowed.append(elbow)
        elbowed.append(end)
    return elbowed


def _dedupe(points: Iterable[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Consecutive duplicates dropped. A zero-length leg is not a leg."""
    kept: list[tuple[float, float]] = []
    for point in points:
        if kept and abs(point[0] - kept[-1][0]) < EPSILON and abs(point[1] - kept[-1][1]) < EPSILON:
            continue
        kept.append(point)
    return tuple(kept)


# --------------------------------------------------------------------------- #
# Leaving the boxes
# --------------------------------------------------------------------------- #


def _clip(
    points: Sequence[tuple[float, float]], source: Anchor, target: Anchor, *, loop: bool
) -> tuple[tuple[float, float], ...]:
    """The route with the parts inside either endpoint's box taken off.

    Graphviz stops an edge at the shape it runs into; a route we compute
    ourselves starts at the node's *centre*, and the piece between the centre
    and the border would be drawn straight across the label — edges are painted
    after nodes, so it would be plainly visible rather than hidden underneath.

    Two nodes drawn on top of each other have no border to stop at. That is a
    diagram somebody has to fix, not a rendering to refuse, so the route is
    handed back whole and the line runs through both shapes.
    """
    forward = _clip_end(points, source)
    backward = _clip_end(tuple(reversed(forward)), target)
    clipped = tuple(reversed(backward))
    if len(clipped) < 2 and loop:
        # A loop clipped away entirely means the reach is inside the box, which
        # only a node the size of a page can manage. Keep the ring.
        return tuple(points)
    return clipped


def _clip_end(
    points: Sequence[tuple[float, float]], anchor: Anchor
) -> tuple[tuple[float, float], ...]:
    """``points`` with its leading run inside ``anchor``'s box replaced by the crossing."""
    index = 0
    while index < len(points) and anchor.contains(*points[index]):
        index += 1
    if index >= len(points):
        return tuple(points)
    if index == 0:
        return tuple(points)
    crossing = _crossing(points[index - 1], points[index], anchor)
    return (crossing, *points[index:])


def _crossing(
    inside: tuple[float, float],
    outside: tuple[float, float],
    anchor: Anchor,
) -> tuple[float, float]:
    """Where the segment from inside the box to outside it crosses the border.

    A slab test: the crossing is at the smallest parameter at which one axis
    reaches its half-extent *while the other is still within the box*. The
    second half is what rules out the two intersections with the extensions of
    the sides, which a box has four of and a rectangle only two.

    The "still within" test carries a hair of slack, and it is load-bearing
    rather than defensive: a route that leaves a box squarely reaches the border
    exactly, and ``0 + 300 * (21 / 300)`` is ``21.000000000000004``. Without the
    slack the crossing is rejected, the segment is left unclipped, and the line
    is drawn straight across the label — which is precisely the case an
    axis-aligned diagram is made of.
    """
    x1, y1 = inside
    x2, y2 = outside
    dx, dy = x2 - x1, y2 - y1
    half_w = anchor.width / 2 + CLEARANCE
    half_h = anchor.height / 2 + CLEARANCE
    best = 1.0
    axes = (
        (dx, x1, half_w, anchor.x, dy, y1, half_h, anchor.y),
        (dy, y1, half_h, anchor.y, dx, x1, half_w, anchor.x),
    )
    for delta, start, half, centre, other_delta, other_start, other_half, other_centre in axes:
        if abs(delta) < EPSILON:
            continue
        for edge in (centre - half, centre + half):
            t = (edge - start) / delta
            if not (0.0 <= t <= best):
                continue
            across = other_start + other_delta * t
            if abs(across - other_centre) <= other_half + TOUCH:
                best = t
    return (x1 + dx * best, y1 + dy * best)


# --------------------------------------------------------------------------- #
# Becoming a curve
# --------------------------------------------------------------------------- #


def _controls(
    corners: Sequence[tuple[float, float]], style: Routing
) -> tuple[tuple[float, float], ...]:
    """The polyline as cubic Bézier control points, ``3n + 1`` of them."""
    if style is Routing.SPLINE and len(corners) > 2:
        return _smooth(corners)
    return _segments(corners)


def _segments(corners: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Each leg as a cubic whose handles are its own thirds: a straight line."""
    controls: list[tuple[float, float]] = [corners[0]]
    for (x1, y1), (x2, y2) in pairwise(corners):
        controls.append((x1 + (x2 - x1) / 3, y1 + (y2 - y1) / 3))
        controls.append((x1 + 2 * (x2 - x1) / 3, y1 + 2 * (y2 - y1) / 3))
        controls.append((x2, y2))
    return tuple(controls)


def _smooth(corners: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """A Catmull-Rom curve through every corner, as cubic Béziers.

    Which is what makes ``spline`` mean something once a link has bends: the
    line passes *through* each one — a bend the user dropped is where the cable
    goes, not a hint about where it might like to go — and arrives at each with
    the direction its neighbours imply, so a hand-routed trunk curves the way
    Graphviz's own splines do.
    """
    points = list(corners)
    # Reflected end points give the first and last legs a neighbour to take
    # their tangent from, so the curve leaves the node the way the run does.
    first = (2 * points[0][0] - points[1][0], 2 * points[0][1] - points[1][1])
    last = (2 * points[-1][0] - points[-2][0], 2 * points[-1][1] - points[-2][1])
    padded = [first, *points, last]
    controls: list[tuple[float, float]] = [points[0]]
    for index in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[index - 1], padded[index], padded[index + 1], padded[index + 2]
        controls.append((p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6))
        controls.append((p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6))
        controls.append(p2)
    return tuple(controls)


# --------------------------------------------------------------------------- #
# Walking the curve
# --------------------------------------------------------------------------- #


def _flatten(controls: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """The curve as a polyline of :data:`FLATTEN_STEPS` pieces per cubic."""
    if len(controls) < 4:
        return tuple(controls)
    points: list[tuple[float, float]] = [controls[0]]
    for start in range(0, len(controls) - 3, 3):
        p0, p1, p2, p3 = controls[start : start + 4]
        for step in range(1, FLATTEN_STEPS + 1):
            points.append(_bezier(p0, p1, p2, p3, step / FLATTEN_STEPS))
    return tuple(points)


def _bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    )


def _walk(points: Sequence[tuple[float, float]], at: float) -> tuple[float, float]:
    """The point ``at`` of the way along a polyline, measured by length."""
    if not points:
        return (0.0, 0.0)
    if len(points) == 1:
        return points[0]
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in pairwise(points)]
    total = sum(lengths)
    if total < EPSILON:
        return points[0]
    wanted = max(0.0, min(1.0, at)) * total
    travelled = 0.0
    for (a, b), length in zip(pairwise(points), lengths, strict=True):
        if travelled + length >= wanted:
            t = 0.0 if length < EPSILON else (wanted - travelled) / length
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        travelled += length
    return points[-1]
