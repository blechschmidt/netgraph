"""Orthogonal routing that goes *around* the boxes instead of through them.

``docs/follow-ups.md`` entry 19 recorded the defect this module closes: an
orthogonal link between two devices with a third sitting between them was drawn
straight across the third one's box, because
:func:`netgraph.layout.routing.route` decides one leg at a time and looks at
nothing else in the drawing. That locality is worth keeping — it is what lets
the editor canvas mirror the renderer exactly — so avoidance is a **separate
layer above it** rather than a change to it. This module works out *where the
bends go*; :func:`~netgraph.layout.routing.route` still draws the line through
them, in Python and in JavaScript alike, and both still agree to the last
decimal place.

The output is therefore a waypoint list — the same list
:class:`~netgraph.edit.operations.SetLinkGeometry` stores and the same list a
person produces by dragging a bend. That is deliberate: a computed route and a
hand-placed one are the same kind of thing, so a computed one can be handed to
the canvas, written into a layout document, or thrown away and recomputed,
without any consumer learning a second vocabulary.

How it works
------------

**The grid.** Every placed node is inflated by :attr:`Budget.clearance` into an
:class:`Obstacle`. The candidate coordinates are the *Hanan grid* of those
rectangles: for each obstacle its left edge, its right edge and its centre line,
on each axis. Corners are where a route may turn, edges are the corridors
between boxes, and centres are where a route leaves the node it starts at. For
``n`` obstacles that is at most ``3n + 2`` lines per axis, built **once per
render** and shared by every link — see :class:`Router`, which is the cache.

**The search.** An A\\* over ``(x index, y index, arrival axis)``. The arrival
axis is part of the state because a bend costs something: without it the search
would find the shortest *staircase* rather than the shortest *route*, and a
staircase is what a reader sees as a mistake. The heuristic is Manhattan
distance plus one bend when the two ends share neither row nor column, which is
admissible, so the first path popped is optimal for the cost function.

**The cost function.** Length in points, plus :attr:`Budget.bend` per turn, plus
:attr:`Budget.crossing` each time the line crosses one already routed, plus
:attr:`Budget.congestion` for running along a channel another route already
occupies. The last two are what turn a bundle of cables leaving one switch into
parallel lanes rather than one line drawn four times.

Complexity, and where it stops
------------------------------

Building the grid is ``O(n log n)`` in the number of placed nodes, once per
render. A single link's search is ``O(w log w)`` in the number of grid points
``w`` inside its window — the bounding box of its two ends, grown by a margin —
which for the ordinary case of two devices near each other is a handful of
points and immeasurably fast. It is *not* bounded by the size of the diagram,
which is the whole reason the window exists.

Three cut-offs, and every one of them is reported rather than silently applied
(:attr:`Router.detours`, surfaced by
:func:`netgraph.render.dot.routing_advisories`):

``Budget.max_cells``
    A window holding more grid points than this is not searched at all. It means
    the link runs across a dense part of a large diagram, where the answer would
    cost more than the ugliness it fixes.
``Budget.max_expansions``
    A search that has popped this many states is abandoned. The bound exists
    because a window can be admissible and still enclose a maze.
``no path``
    A link whose two ends are walled in — nodes drawn on top of each other, a
    clearance wider than the corridor between two racks — has no orthogonal
    route that avoids anything, and is drawn the way it was before.

In all three cases the link falls back to
:func:`netgraph.layout.routing.route`'s local Z or L: the diagram is no worse
than it was, and the reason is printed.

Coordinates are points, ``y`` upwards, the same system as the rest of
:mod:`netgraph.layout`.
"""

from __future__ import annotations

import heapq
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Final

from netgraph.layout.routing import Anchor

__all__ = [
    "Budget",
    "Detour",
    "Obstacle",
    "Router",
    "crossings",
    "offset_polyline",
]

#: Distances below this are treated as zero, and points this close together as
#: the same point. The same value :mod:`netgraph.layout.routing` uses, for the
#: same reason.
EPSILON: Final = 1e-9

#: How far a grid point must be inside a rectangle before it counts as inside
#: it. A route is *meant* to run along an obstacle's inflated border, and a
#: border coordinate that has been through a subtraction and an addition does
#: not always come back bit-identical; without the slack the corridor a route is
#: supposed to use reads as blocked.
INSIDE: Final = 1e-6

#: How wide the buckets of the segment index are, in points. Routed segments are
#: filed by the buckets they span so that counting the crossings of one move
#: touches a handful of them rather than every line already drawn. Roughly two
#: node widths: small enough that a bucket holds few segments, large enough that
#: a long corridor does not span hundreds.
BUCKET: Final = 128.0


@dataclass(frozen=True, slots=True)
class Budget:
    """What a route is allowed to cost, and what the search is allowed to spend.

    Every field is a knob rather than a constant because the right answer
    depends on the diagram: a rack elevation wants a large ``clearance`` and
    cheap bends, a wide-area map the opposite. The defaults are tuned on
    ``tests/fixtures/obstructed``, which is the fixture this module exists to
    draw correctly.
    """

    #: How far a route keeps away from a box it is not attached to, in points.
    #: Also how far apart two obstacles have to be before a route will thread
    #: between them at all.
    clearance: float = 10.0
    #: What one right-angled turn costs, in points of equivalent length. High
    #: enough that a route prefers a longer straight run to a staircase, low
    #: enough that it will still go round rather than through.
    bend: float = 60.0
    #: What crossing a line already routed costs. Higher than a bend: a reader
    #: untangles a corner far more easily than an intersection.
    crossing: float = 150.0
    #: What sharing a channel — a row or column another route already runs
    #: along — costs. Low: two cables in one corridor is normal and often right;
    #: this only breaks the tie towards spreading them out.
    congestion: float = 20.0
    #: The largest window, in grid points, that is searched at all.
    max_cells: int = 40_000
    #: The most states one search may pop before it is abandoned.
    max_expansions: int = 20_000
    #: How far past its two ends a link's window reaches, in points, before the
    #: proportional term below takes over. A route has to be allowed to leave
    #: the corridor between its endpoints to get round anything.
    margin: float = 160.0
    #: The window also grows with the length of the link, so that a long cable
    #: across a diagram may detour proportionally as far as a short one.
    margin_fraction: float = 0.35

    def __post_init__(self) -> None:
        if self.clearance < 0:
            raise ValueError("clearance cannot be negative")
        if self.max_cells < 4 or self.max_expansions < 4:
            raise ValueError("a search needs room for at least a few states")


#: What routing does when nothing says otherwise.
DEFAULT_BUDGET: Final = Budget()


@dataclass(frozen=True, slots=True)
class Detour:
    """One link routing gave up on, and why.

    Kept rather than logged in place because the renderer reports it: a diagram
    that quietly stopped avoiding obstacles halfway through is worse than one
    that never tried, and the number in ``detail`` is what says which knob to
    turn.
    """

    #: The edge id, as the graph spells it.
    link: str
    #: One of ``"window"``, ``"budget"`` or ``"unreachable"``.
    reason: str
    #: What was measured when it gave up: the window's size, or the number of
    #: states popped.
    detail: int

    def describe(self) -> str:
        if self.reason == "window":
            return (
                f"{self.link}: {self.detail} grid points around it, past the search window; "
                f"drawn without avoiding obstacles"
            )
        if self.reason == "budget":
            return (
                f"{self.link}: gave up after {self.detail} steps; drawn without avoiding obstacles"
            )
        return f"{self.link}: no clear orthogonal route exists; drawn without avoiding obstacles"


@dataclass(frozen=True, slots=True)
class Obstacle:
    """A rectangle a route must not cross, already grown by the clearance.

    ``key`` is what an endpoint is exempted by: a route has to be allowed inside
    the two boxes it joins, because that is where it starts and ends, and
    :func:`netgraph.layout.routing.route` clips those stubs off afterwards.
    """

    key: str
    left: float
    bottom: float
    right: float
    top: float

    @classmethod
    def of(cls, key: str, anchor: Anchor, clearance: float) -> Obstacle:
        return cls(
            key=key,
            left=anchor.x - anchor.width / 2 - clearance,
            bottom=anchor.y - anchor.height / 2 - clearance,
            right=anchor.x + anchor.width / 2 + clearance,
            top=anchor.y + anchor.height / 2 + clearance,
        )

    @classmethod
    def around(
        cls, key: str, x: float, y: float, width: float, height: float, clearance: float
    ) -> Obstacle:
        """An obstacle around a rectangle given as centre and extent."""
        return cls(
            key=key,
            left=x - width / 2 - clearance,
            bottom=y - height / 2 - clearance,
            right=x + width / 2 + clearance,
            top=y + height / 2 + clearance,
        )

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2, (self.bottom + self.top) / 2)

    def holds(self, x: float, y: float) -> bool:
        """Is this point strictly inside? A point *on* the border is not."""
        return (
            self.left + INSIDE < x < self.right - INSIDE
            and self.bottom + INSIDE < y < self.top - INSIDE
        )

    def cut_by(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        """Does the axis-aligned segment ``a`` to ``b`` pass through this rectangle?

        Only axis-aligned segments are asked about — every route this module
        produces or measures is orthogonal — which makes the test two interval
        overlaps rather than a clipping algorithm.
        """
        (x1, y1), (x2, y2) = a, b
        low_x, high_x = (x1, x2) if x1 <= x2 else (x2, x1)
        low_y, high_y = (y1, y2) if y1 <= y2 else (y2, y1)
        return (
            low_x < self.right - INSIDE
            and high_x > self.left + INSIDE
            and low_y < self.top - INSIDE
            and high_y > self.bottom + INSIDE
        )


# --------------------------------------------------------------------------- #
# Measuring the defect
# --------------------------------------------------------------------------- #


def crossings(
    corners: Sequence[tuple[float, float]],
    obstacles: Iterable[Obstacle],
    *,
    exempt: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Which obstacles a drawn line runs across, its own two ends excluded.

    The measurement the whole task is graded on, and it is deliberately a
    *count of names* rather than a boolean: "this diagram has four links through
    six boxes" is a number that can go to zero and be seen to have done so, and
    ``tools/route_crossings.py`` prints exactly that.
    """
    hit: list[str] = []
    for obstacle in obstacles:
        if obstacle.key in exempt:
            continue
        if any(obstacle.cut_by(a, b) for a, b in pairwise(corners)):
            hit.append(obstacle.key)
    return tuple(hit)


# --------------------------------------------------------------------------- #
# Parallel lanes
# --------------------------------------------------------------------------- #


def offset_polyline(
    corners: Sequence[tuple[float, float]], distance: float
) -> tuple[tuple[float, float], ...]:
    """An orthogonal polyline moved ``distance`` sideways, corners mitred.

    What makes a bundle of cables read as a bundle: the members share one route
    and are drawn as lanes beside it, rather than each being routed separately
    and fanning out and re-converging around whatever each one happened to find.

    Each segment is shifted along its own left normal and consecutive shifted
    lines are intersected, which for an axis-aligned polyline is exact: two
    perpendicular lines always meet, and the meeting point is one coordinate
    from each.
    """
    if distance == 0.0 or len(corners) < 2:
        return tuple(corners)
    shifted: list[tuple[tuple[float, float], tuple[float, float], bool]] = []
    for (x1, y1), (x2, y2) in pairwise(corners):
        dx, dy = x2 - x1, y2 - y1
        length = (dx * dx + dy * dy) ** 0.5
        if length < EPSILON:
            continue
        nx, ny = -dy / length * distance, dx / length * distance
        shifted.append(((x1 + nx, y1 + ny), (x2 + nx, y2 + ny), abs(dy) < EPSILON))
    if not shifted:
        return tuple(corners)
    moved: list[tuple[float, float]] = [shifted[0][0]]
    for (_, end, flat), (start, _, next_flat) in pairwise(shifted):
        moved.append(_meet(end, flat, start, next_flat))
    moved.append(shifted[-1][1])
    return _straighten(moved)


def _meet(
    end: tuple[float, float], flat: bool, start: tuple[float, float], next_flat: bool
) -> tuple[float, float]:
    """Where two shifted, perpendicular, axis-aligned runs meet.

    The corner takes its ``x`` from whichever of the two is *vertical* and its
    ``y`` from whichever is *horizontal*, which is why each run carries whether
    it was flat: after the shift the two are the same distance apart on both
    axes, so the point cannot be recovered from the coordinates alone. Two runs
    that are parallel — which :func:`_straighten` should have made impossible,
    but a degenerate route can still produce — never meet, and the midpoint
    keeps the line continuous.
    """
    if flat == next_flat:
        return ((end[0] + start[0]) / 2, (end[1] + start[1]) / 2)
    return (start[0], end[1]) if flat else (end[0], start[1])


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #


@dataclass
class _Occupancy:
    """What is already drawn, in the two forms the cost function asks about.

    ``rows`` and ``columns`` answer "is this channel busy?" in constant time.
    ``buckets`` answers "what does this move cross?" by filing every routed
    segment under the :data:`BUCKET`-wide cells it spans, so the question costs
    a few rectangle tests rather than a scan of the whole drawing.
    """

    #: Channel coordinate to the spans already occupied along it. A span rather
    #: than a count because a column is not busy everywhere just because
    #: something runs along part of it: charging a route for sharing a channel it
    #: never actually overlaps would push it away from the tidiest answer for no
    #: reason, and the case that matters — a cable leaving a switch upwards
    #: because another cable already leaves it downwards — needs the ends of the
    #: run to be compared, not its coordinate.
    rows: dict[float, list[tuple[float, float]]] = field(default_factory=dict)
    columns: dict[float, list[tuple[float, float]]] = field(default_factory=dict)
    buckets: dict[tuple[int, int], list[tuple[tuple[float, float], tuple[float, float]]]] = field(
        default_factory=dict
    )

    def add(self, corners: Sequence[tuple[float, float]]) -> None:
        """File one drawn line. Anything not axis-aligned is ignored.

        A diagonal reaches here when a leg was left for the local rule to shape
        — that rule turns it into an elbow the caller never sees — and both the
        channel counters and the crossing test are defined only for runs that
        lie along an axis. Charging a diagonal against a row would name a
        channel no line actually occupies.
        """
        for a, b in pairwise(corners):
            flat = abs(a[1] - b[1]) < EPSILON
            upright = abs(a[0] - b[0]) < EPSILON
            if flat and upright:
                continue
            if flat:
                self.rows.setdefault(_channel(a[1]), []).append(_span(a[0], b[0]))
            elif upright:
                self.columns.setdefault(_channel(a[0]), []).append(_span(a[1], b[1]))
            else:
                continue
            for cell in _cells(a, b):
                self.buckets.setdefault(cell, []).append((a, b))

    def busy(self, channel: float, span: tuple[float, float], *, flat: bool) -> int:
        """How many runs already occupy this stretch of this row or column."""
        found = (self.rows if flat else self.columns).get(_channel(channel))
        if not found:
            return 0
        low, high = span
        return sum(1 for start, end in found if start < high - INSIDE and end > low + INSIDE)

    def crossings(self, a: tuple[float, float], b: tuple[float, float]) -> int:
        """How many already-routed segments this move cuts across.

        Counted once per segment even when the two share several buckets, which
        is what the ``seen`` set is for: a long corridor is filed under every
        cell it spans and would otherwise be charged for each of them.
        """
        seen: set[int] = set()
        count = 0
        for cell in _cells(a, b):
            for other in self.buckets.get(cell, ()):
                if id(other) in seen:
                    continue
                seen.add(id(other))
                if _crosses(a, b, *other):
                    count += 1
        return count


def _channel(value: float) -> float:
    """A coordinate as a channel key: rounded, so two routes agree on one lane."""
    return round(value, 2)


def _span(a: float, b: float) -> tuple[float, float]:
    """Two coordinates as a low-to-high interval."""
    return (a, b) if a <= b else (b, a)


def _cells(a: tuple[float, float], b: tuple[float, float]) -> Iterator[tuple[int, int]]:
    """The bucket cells an axis-aligned segment spans."""
    low_x, high_x = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
    low_y, high_y = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
    for i in range(int(low_x // BUCKET), int(high_x // BUCKET) + 1):
        for j in range(int(low_y // BUCKET), int(high_y // BUCKET) + 1):
            yield (i, j)


def _crosses(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    """Do two axis-aligned segments meet at a point interior to both?

    Touching at an endpoint is not a crossing — two cables leaving the same
    switch share their first point and would otherwise be charged for it — and
    neither is running along each other, which is what the congestion term is
    for.
    """
    horizontal = abs(a[1] - b[1]) < EPSILON
    other_horizontal = abs(c[1] - d[1]) < EPSILON
    if horizontal == other_horizontal:
        return False
    if not horizontal:
        a, b, c, d = c, d, a, b
    y = a[1]
    x = c[0]
    low_x, high_x = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
    low_y, high_y = (c[1], d[1]) if c[1] <= d[1] else (d[1], c[1])
    return low_x + INSIDE < x < high_x - INSIDE and low_y + INSIDE < y < high_y - INSIDE


class Router:
    """The obstacle grid for one drawing, and the searches run over it.

    Built once per render and asked once per link, which is the whole point:
    the Hanan lines, the spatial index and the occupancy map are shared, so a
    thousand-device diagram pays for them once rather than a thousand times.

    A router is *stateful across calls* by design — :meth:`waypoints` records
    what it routed so the next link can be charged for crossing it — which
    makes the answer depend on the order links are asked about. The order is the
    graph's own, which is stable, so the same inventory routes the same way
    twice.
    """

    def __init__(self, obstacles: Iterable[Obstacle], budget: Budget = DEFAULT_BUDGET) -> None:
        self.budget = budget
        self.obstacles: tuple[Obstacle, ...] = tuple(obstacles)
        self.detours: list[Detour] = []
        self._by_key: Mapping[str, Obstacle] = {o.key: o for o in self.obstacles}
        self._xs, self._ys = _hanan(self.obstacles)
        self._index = _index(self.obstacles)
        self._drawn = _Occupancy()
        #: How many searches this router has run and how many states they
        #: popped, so a bench can report the cost of a render rather than
        #: guessing it.
        self.searches = 0
        self.expansions = 0
        self.avoided = 0

    # -- the public question ------------------------------------------------ #

    def waypoints(
        self,
        source: Anchor,
        target: Anchor,
        *,
        source_key: str,
        target_key: str,
        pinned: Sequence[tuple[float, float]] = (),
        link: str = "",
    ) -> tuple[tuple[float, float], ...] | None:
        """The bends one link needs to reach its far end without crossing anything.

        Args:
            source: The box the link leaves.
            target: The box it arrives at.
            source_key: The source node's id, so the route may start inside it.
            target_key: Likewise for the far end.
            pinned: The bends a **person** placed, which are authoritative: the
                search fills the segments *between* them and never moves one.
            link: The edge id, used only to name a detour.

        Returns:
            The interior waypoints, ready to hand to
            :func:`netgraph.layout.routing.route`, or ``None`` when the line the
            local rule already draws crosses nothing and must therefore be left
            exactly as it was. ``None`` is not a failure: it is the answer for
            most links in most diagrams, and it is what keeps a clean drawing
            byte-identical to the one this module was added to.
        """
        exempt = frozenset({source_key, target_key})
        legs: tuple[tuple[float, float], ...] = (
            source.centre,
            *((float(x), float(y)) for x, y in pinned),
            target.centre,
        )
        # With nothing pinned the local rule draws one Z across the whole link;
        # with bends pinned it draws an L per leg. Which one is being replaced
        # decides what "the line already crosses nothing" means, so the shape
        # has to travel with the leg.
        elbow = len(legs) > 2
        pieces: list[tuple[tuple[float, float], ...]] = []
        changed = False
        for start, end in pairwise(legs):
            found = self._leg(start, end, exempt=exempt, link=link, elbow=elbow)
            if found is None:
                pieces.append((start, end))
                continue
            changed = True
            pieces.append(found)
        if not changed:
            return None
        self.avoided += 1
        joined = _join(pieces, frozenset(legs[1:-1]))
        self._drawn.add(joined)
        # The two ends are handed back *with* the rest, even though they are the
        # node centres :func:`~netgraph.layout.routing.route` supplies anyway.
        # It costs a duplicate point that ``route`` drops, and it buys the one
        # thing a bundle needs: when this polyline is offset into a lane, the
        # lane's own ends move too, so four cables leave a switch at four points
        # rather than at one.
        if len(joined) == 2:
            # A route that came back straight still has to *say* it is straight.
            # The local rule turns two bare points into a Z, so handing back the
            # bare pair would put back the very crossing just routed around.
            (x1, y1), (x2, y2) = joined
            return (joined[0], ((x1 + x2) / 2, (y1 + y2) / 2), joined[1])
        return joined

    def lanes(
        self,
        polyline: Sequence[tuple[float, float]],
        count: int,
        *,
        gap: float,
        exempt: frozenset[str] = frozenset(),
    ) -> tuple[tuple[tuple[float, float], ...], ...]:
        """``count`` parallel lanes beside one routed line, on whichever side is clear.

        This is what makes a bundle of cables (task 38) read as a bundle: they
        share one route and are drawn beside it, rather than each being searched
        for separately and fanning out and re-converging around whatever each one
        happened to find.

        The lanes go out from the routed line in **one** direction rather than
        spreading either side of it, because the routed line is normally hugging
        an obstacle at exactly the clearance: half the bundle would be pushed
        into the box it had just gone round. Which direction that is, is decided
        by trying both and counting what each one runs into — the routed line
        knows which way it turned, but a polyline of several segments can have
        turned both ways, and counting is the answer that is right either way.
        """
        if count <= 1 or gap == 0.0:
            return (tuple(polyline),) * max(count, 0)
        best: tuple[tuple[tuple[float, float], ...], ...] = ()
        fewest = -1
        for sign in (1.0, -1.0):
            spread = tuple(offset_polyline(polyline, index * gap * sign) for index in range(count))
            hit = sum(len(crossings(lane, self.obstacles, exempt=exempt)) for lane in spread)
            if fewest < 0 or hit < fewest:
                best, fewest = spread, hit
            if not fewest:
                break
        return best

    def record(self, corners: Sequence[tuple[float, float]]) -> None:
        """Charge a line somebody else drew against the congestion map.

        A link that needed no detour is still a line on the page, and the next
        link that does need one should be told about it. The caller is the one
        that knows what was finally drawn — clipped, fanned and all — so it
        reports rather than this module assuming.
        """
        self._drawn.add(corners)

    def blocked(
        self, corners: Sequence[tuple[float, float]], *, exempt: frozenset[str] = frozenset()
    ) -> tuple[str, ...]:
        """Which boxes a drawn line runs across. See :func:`crossings`.

        Through the spatial index rather than over every obstacle, because this
        is asked once per link per render — it is how a link decides whether it
        needs routing at all, and how a cached route decides whether it is still
        valid. Scanning the whole drawing for each would make the cheap answer
        cost ``O(links * nodes)``, which on a few hundred of each is the entire
        cost of routing and is exactly the per-edge price the grid is cached to
        avoid.
        """
        hit: list[str] = []
        seen: set[str] = set()
        for a, b in pairwise(corners):
            for obstacle in self._near(a, b):
                if obstacle.key in exempt or obstacle.key in seen:
                    continue
                if obstacle.cut_by(a, b):
                    seen.add(obstacle.key)
                    hit.append(obstacle.key)
        return tuple(hit)

    # -- one leg ------------------------------------------------------------ #

    def _leg(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        exempt: frozenset[str],
        link: str,
        elbow: bool,
    ) -> tuple[tuple[float, float], ...] | None:
        """One segment of a route: from one fixed point to the next."""
        already = _local(start, end, elbow=elbow)
        if not already:
            return None
        if not self._cut(already, exempt):
            return None
        window = self._window(start, end)
        if window is None:
            self.detours.append(Detour(link=link, reason="window", detail=self._cells(start, end)))
            return None
        self.searches += 1
        return self._search(start, end, window=window, exempt=exempt, link=link)

    def _cut(self, corners: Sequence[tuple[float, float]], exempt: frozenset[str]) -> bool:
        """Does this polyline run across any box it is not attached to?"""
        for a, b in pairwise(corners):
            for obstacle in self._near(a, b):
                if obstacle.key not in exempt and obstacle.cut_by(a, b):
                    return True
        return False

    def _near(self, a: tuple[float, float], b: tuple[float, float]) -> Iterator[Obstacle]:
        seen: set[str] = set()
        for cell in _cells(a, b):
            for obstacle in self._index.get(cell, ()):
                if obstacle.key in seen:
                    continue
                seen.add(obstacle.key)
                yield obstacle

    def _holds(self, x: float, y: float, exempt: frozenset[str]) -> bool:
        """Is this grid point inside a box the route is not allowed in?"""
        cell = (int(x // BUCKET), int(y // BUCKET))
        for obstacle in self._index.get(cell, ()):
            if obstacle.key not in exempt and obstacle.holds(x, y):
                return True
        return False

    # -- the grid ----------------------------------------------------------- #

    def _lines(
        self, start: tuple[float, float], end: tuple[float, float], reach: float
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """The grid lines inside the window, with the two terminals spliced in.

        Sliced out of the cached arrays rather than rebuilt, which is what makes
        the grid a per-render cost instead of a per-link one. The terminals are
        added because a leg may start at a bend somebody dragged, which is on no
        obstacle's border and so on no Hanan line.
        """
        low_x, high_x = min(start[0], end[0]) - reach, max(start[0], end[0]) + reach
        low_y, high_y = min(start[1], end[1]) - reach, max(start[1], end[1]) + reach
        xs = _merge(
            self._xs[bisect_left(self._xs, low_x) : bisect_right(self._xs, high_x)],
            (start[0], end[0]),
        )
        ys = _merge(
            self._ys[bisect_left(self._ys, low_y) : bisect_right(self._ys, high_y)],
            (start[1], end[1]),
        )
        return xs, ys

    def _reach(self, start: tuple[float, float], end: tuple[float, float]) -> float:
        span = abs(end[0] - start[0]) + abs(end[1] - start[1])
        return self.budget.margin + self.budget.margin_fraction * span

    def _cells(self, start: tuple[float, float], end: tuple[float, float]) -> int:
        xs, ys = self._lines(start, end, self._reach(start, end))
        return len(xs) * len(ys)

    def _window(
        self, start: tuple[float, float], end: tuple[float, float]
    ) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
        xs, ys = self._lines(start, end, self._reach(start, end))
        if len(xs) * len(ys) > self.budget.max_cells:
            return None
        return xs, ys

    # -- the search --------------------------------------------------------- #

    def _search(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        window: tuple[tuple[float, ...], tuple[float, ...]],
        exempt: frozenset[str],
        link: str,
    ) -> tuple[tuple[float, float], ...] | None:
        """A\\* from ``start`` to ``end`` over the window's grid.

        A state is ``(x index, y index, arrival axis)`` with axis ``0``
        horizontal, ``1`` vertical and ``2`` "has not moved yet". Ties are
        broken on the state itself rather than on insertion order, so the answer
        does not depend on how Python happened to hash a tuple.
        """
        xs, ys = window
        sx, sy = _at(xs, start[0]), _at(ys, start[1])
        tx, ty = _at(xs, end[0]), _at(ys, end[1])
        if sx is None or sy is None or tx is None or ty is None:  # pragma: no cover - spliced in
            return None
        budget = self.budget
        goal = (tx, ty)
        blocked: dict[tuple[int, int], bool] = {}

        def free(i: int, j: int) -> bool:
            known = blocked.get((i, j))
            if known is None:
                known = not self._holds(xs[i], ys[j], exempt)
                blocked[(i, j)] = known
            return known

        def guess(i: int, j: int) -> float:
            dx, dy = abs(xs[i] - xs[tx]), abs(ys[j] - ys[ty])
            turn = budget.bend if dx > EPSILON and dy > EPSILON else 0.0
            return dx + dy + turn

        origin = (sx, sy, 2)
        best: dict[tuple[int, int, int], float] = {origin: 0.0}
        came: dict[tuple[int, int, int], tuple[int, int, int]] = {}
        queue: list[tuple[float, tuple[int, int, int]]] = [(guess(sx, sy), origin)]
        popped = 0
        while queue:
            estimate, state = heapq.heappop(queue)
            i, j, axis = state
            here = best.get(state)
            # A state reached again more cheaply is pushed again rather than
            # decreased in place, which ``heapq`` cannot do; the earlier, dearer
            # copy is still in the queue and is dropped here.
            if here is None or estimate - guess(i, j) > here + EPSILON:
                continue
            if (i, j) == goal:
                self.expansions += popped
                return _corners(xs, ys, _trace(came, state))
            popped += 1
            if popped > budget.max_expansions:
                self.expansions += popped
                self.detours.append(Detour(link=link, reason="budget", detail=popped))
                return None
            for step, moved in _steps(i, j, len(xs), len(ys)):
                ni, nj = moved
                if not free(ni, nj):
                    continue
                cost = self._step_cost(xs, ys, (i, j), (ni, nj), axis=axis, step=step)
                total = here + cost
                ahead = (ni, nj, step)
                if total + EPSILON < best.get(ahead, float("inf")):
                    best[ahead] = total
                    came[ahead] = state
                    heapq.heappush(queue, (total + guess(ni, nj), ahead))
        self.expansions += popped
        self.detours.append(Detour(link=link, reason="unreachable", detail=popped))
        return None

    def _step_cost(
        self,
        xs: Sequence[float],
        ys: Sequence[float],
        here: tuple[int, int],
        there: tuple[int, int],
        *,
        axis: int,
        step: int,
    ) -> float:
        """Length, plus a bend, plus what the move runs into."""
        a = (xs[here[0]], ys[here[1]])
        b = (xs[there[0]], ys[there[1]])
        budget = self.budget
        cost = abs(b[0] - a[0]) + abs(b[1] - a[1])
        if axis != 2 and axis != step:
            cost += budget.bend
        flat = step == 0
        busy = self._drawn.busy(
            a[1] if flat else a[0],
            _span(a[0], b[0]) if flat else _span(a[1], b[1]),
            flat=flat,
        )
        if busy:
            cost += budget.congestion * busy
        cut = self._drawn.crossings(a, b)
        if cut:
            cost += budget.crossing * cut
        return cost


# --------------------------------------------------------------------------- #
# Grid helpers
# --------------------------------------------------------------------------- #


def _hanan(obstacles: Sequence[Obstacle]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The candidate coordinates: every obstacle's two borders and its centre.

    The borders are the corridors — a route runs along them, at exactly the
    clearance — and the centre is where a route leaves the node it is attached
    to, which is the only point inside a box a route is ever at.
    """
    xs: set[float] = set()
    ys: set[float] = set()
    for obstacle in obstacles:
        cx, cy = obstacle.centre
        xs.update((obstacle.left, cx, obstacle.right))
        ys.update((obstacle.bottom, cy, obstacle.top))
    return tuple(sorted(xs)), tuple(sorted(ys))


def _index(obstacles: Sequence[Obstacle]) -> dict[tuple[int, int], list[Obstacle]]:
    """Obstacles filed under the :data:`BUCKET`-wide cells they cover."""
    buckets: dict[tuple[int, int], list[Obstacle]] = {}
    for obstacle in obstacles:
        for cell in _cells((obstacle.left, obstacle.bottom), (obstacle.right, obstacle.top)):
            buckets.setdefault(cell, []).append(obstacle)
    return buckets


def _merge(lines: Sequence[float], extra: Iterable[float]) -> tuple[float, ...]:
    """Sorted lines with a few more coordinates spliced in, duplicates dropped."""
    merged = sorted({*lines, *extra})
    kept: list[float] = []
    for value in merged:
        if kept and value - kept[-1] < INSIDE:
            continue
        kept.append(value)
    return tuple(kept)


def _at(lines: Sequence[float], value: float) -> int | None:
    """The index of ``value`` among ``lines``, which spliced it in."""
    index = bisect_left(lines, value - INSIDE)
    if index < len(lines) and abs(lines[index] - value) < INSIDE * 2:
        return index
    return None


def _steps(i: int, j: int, width: int, height: int) -> Iterator[tuple[int, tuple[int, int]]]:
    """The four moves out of one grid point, each with the axis it travels on."""
    if i > 0:
        yield 0, (i - 1, j)
    if i + 1 < width:
        yield 0, (i + 1, j)
    if j > 0:
        yield 1, (i, j - 1)
    if j + 1 < height:
        yield 1, (i, j + 1)


def _trace(
    came: Mapping[tuple[int, int, int], tuple[int, int, int]], state: tuple[int, int, int]
) -> list[tuple[int, int]]:
    path: list[tuple[int, int]] = []
    here: tuple[int, int, int] | None = state
    while here is not None:
        path.append((here[0], here[1]))
        here = came.get(here)
    path.reverse()
    return path


def _corners(
    xs: Sequence[float], ys: Sequence[float], path: Sequence[tuple[int, int]]
) -> tuple[tuple[float, float], ...]:
    """The grid path as coordinates, with every collinear point dropped."""
    points = [(xs[i], ys[j]) for i, j in path]
    return _straighten(points)


def _straighten(
    points: Sequence[tuple[float, float]], keep: frozenset[tuple[float, float]] = frozenset()
) -> tuple[tuple[float, float], ...]:
    """A polyline with its duplicate and collinear points dropped.

    ``keep`` is the set of points that survive being collinear. It holds the
    bends a *person* placed: dropping one changes no pixel — it lies on the line
    either way — but it changes what the answer *says*, and the answer is handed
    to a canvas that puts a grab handle on each bend and to an operation that can
    write them into a document. A bend that quietly stopped being mentioned the
    moment the route ran straight through it is a bend somebody would find
    missing later.
    """
    kept: list[tuple[float, float]] = []
    for point in points:
        if kept and abs(point[0] - kept[-1][0]) < EPSILON and abs(point[1] - kept[-1][1]) < EPSILON:
            continue
        kept.append(point)
    if len(kept) < 3:
        return tuple(kept)
    trimmed: list[tuple[float, float]] = [kept[0]]
    for before, here, after in zip(kept, kept[1:], kept[2:], strict=False):
        straight_x = abs(before[0] - here[0]) < EPSILON and abs(here[0] - after[0]) < EPSILON
        straight_y = abs(before[1] - here[1]) < EPSILON and abs(here[1] - after[1]) < EPSILON
        if here in keep or not (straight_x or straight_y):
            trimmed.append(here)
    trimmed.append(kept[-1])
    return tuple(trimmed)


def _join(
    pieces: Sequence[Sequence[tuple[float, float]]],
    keep: frozenset[tuple[float, float]] = frozenset(),
) -> tuple[tuple[float, float], ...]:
    """Consecutive legs concatenated, the shared point counted once."""
    joined: list[tuple[float, float]] = []
    for piece in pieces:
        for point in piece:
            if (
                joined
                and abs(point[0] - joined[-1][0]) < EPSILON
                and abs(point[1] - joined[-1][1]) < EPSILON
            ):
                continue
            joined.append(point)
    return _straighten(joined, keep)


def _local(
    start: tuple[float, float], end: tuple[float, float], *, elbow: bool
) -> tuple[tuple[float, float], ...]:
    """The line :func:`netgraph.layout.routing.route` already draws for one leg.

    Restated here rather than imported because what is wanted is the *shape* of
    one leg, and ``route`` shapes a whole link and clips it against two boxes.
    The two must agree — this is what decides whether a link is left exactly as
    it was — and ``tests/test_avoid.py`` runs them against each other on a table
    of cases so they cannot drift.
    """
    (x1, y1), (x2, y2) = start, end
    if abs(x2 - x1) < EPSILON and abs(y2 - y1) < EPSILON:
        return ()
    if elbow:
        corner = (x2, y1) if abs(x2 - x1) >= abs(y2 - y1) else (x1, y2)
        return _straighten(((x1, y1), corner, (x2, y2)))
    if abs(x2 - x1) >= abs(y2 - y1):
        middle = (x1 + x2) / 2
        return _straighten(((x1, y1), (middle, y1), (middle, y2), (x2, y2)))
    middle = (y1 + y2) / 2
    return _straighten(((x1, y1), (x1, middle), (x2, middle), (x2, y2)))
