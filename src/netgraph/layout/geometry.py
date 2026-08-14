"""The runtime form of a stored arrangement, and what a renderer asks of it.

:class:`~netgraph.models.layout.Layout` is what a *document* says.
:class:`Geometry` is what a *view* has: one flat table of coordinates, already
merged across every layout document in the tree and already resolved from the
addresses people write to the node ids the graph uses. Nothing here reads a
file, resolves a name or runs Graphviz — see :mod:`netgraph.layout.resolve` and
:mod:`netgraph.layout.seed` for those.

Coordinates are points, ``y`` upwards, a position being the centre of the thing
it places — Graphviz's system, unchanged, because the whole point of storing an
arrangement is to be able to hand it straight back.

The one decision this module makes is :meth:`Geometry.mode`, which is how a
renderer chooses between three quite different jobs:

``FIXED``
    Every node in the drawing has a stored position. The renderer emits them
    and runs Graphviz in no-op layout mode, so what comes out is exactly what
    was stored. This is the case a hand-arranged diagram must hit, every time.
``PARTIAL``
    Some do. The stored ones are pinned and the engine places the rest around
    them, which is what makes an arrangement usable while it is being built and
    what keeps a newly-added device from being invisible.
``AUTO``
    None do. Nothing changes: the graph is laid out from scratch, as it always
    was.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from netgraph.models.layout import (
    EdgeGeometry,
    GroupGeometry,
    LabelGeometry,
    NodeGeometry,
    Point,
)

__all__ = [
    "COORDINATE_PLACES",
    "DEFAULT_ROUTING",
    "Box",
    "Geometry",
    "LabelPlacement",
    "LayoutMode",
    "LinkGeometry",
    "Placement",
    "Routing",
    "round_coordinate",
]

#: Decimal places coordinates are stored and compared to. Graphviz reports
#: positions to about six significant figures; a hundredth of a point is a
#: three-thousandth of a millimetre on paper, so rounding here costs nothing
#: visible and buys a file that a person can read and a diff can show.
COORDINATE_PLACES: Final = 2


def round_coordinate(value: float) -> float:
    """A coordinate as it is stored: rounded, and never negative zero."""
    rounded = round(float(value), COORDINATE_PLACES)
    return 0.0 if rounded == 0.0 else rounded


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one node sits, and how big it is if the arrangement decided that."""

    x: float
    y: float
    #: In points. ``None`` means "as big as the label needs", which is what lets
    #: a device grow a port without invalidating its position.
    width: float | None = None
    height: float | None = None

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)

    def to_model(self) -> NodeGeometry:
        """The document form of this placement."""
        size = (
            None
            if self.width is None or self.height is None
            else {
                "width": round_coordinate(self.width),
                "height": round_coordinate(self.height),
            }
        )
        return NodeGeometry.model_validate(
            {
                "position": {"x": round_coordinate(self.x), "y": round_coordinate(self.y)},
                **({"size": size} if size is not None else {}),
            }
        )

    @classmethod
    def from_model(cls, geometry: NodeGeometry) -> Placement:
        size = geometry.size
        return cls(
            x=geometry.position.x,
            y=geometry.position.y,
            width=None if size is None else size.width,
            height=None if size is None else size.height,
        )

    def rounded(self) -> Placement:
        return Placement(
            x=round_coordinate(self.x),
            y=round_coordinate(self.y),
            width=None if self.width is None else round_coordinate(self.width),
            height=None if self.height is None else round_coordinate(self.height),
        )


@dataclass(frozen=True, slots=True)
class Box:
    """The rectangle a group is drawn as: centre, plus extent."""

    x: float
    y: float
    width: float
    height: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(left, bottom, right, top)``."""
        return (
            self.x - self.width / 2,
            self.y - self.height / 2,
            self.x + self.width / 2,
            self.y + self.height / 2,
        )

    @classmethod
    def from_bounds(cls, left: float, bottom: float, right: float, top: float) -> Box:
        return cls(
            x=(left + right) / 2,
            y=(bottom + top) / 2,
            width=max(right - left, 1e-6),
            height=max(top - bottom, 1e-6),
        )

    def to_model(self) -> GroupGeometry:
        return GroupGeometry.model_validate(
            {
                "position": {"x": round_coordinate(self.x), "y": round_coordinate(self.y)},
                "size": {
                    "width": max(round_coordinate(self.width), 0.01),
                    "height": max(round_coordinate(self.height), 0.01),
                },
            }
        )

    @classmethod
    def from_model(cls, geometry: GroupGeometry) -> Box:
        return cls(
            x=geometry.position.x,
            y=geometry.position.y,
            width=geometry.size.width,
            height=geometry.size.height,
        )


class Routing(str, Enum):
    """How a link is drawn between the points it is pinned through."""

    #: The curve Graphviz draws, and what every diagram looked like before this
    #: existed.
    SPLINE = "spline"
    #: Right angles: each leg runs horizontally then vertically, which is how a
    #: patch schedule and a rack diagram are drawn.
    ORTHOGONAL = "orthogonal"
    #: Straight from point to point, corners left sharp.
    STRAIGHT = "straight"

    def __str__(self) -> str:
        return self.value


#: What a link is drawn as when neither it, its view nor the inventory says.
DEFAULT_ROUTING: Final = Routing.SPLINE


@dataclass(frozen=True, slots=True)
class LabelPlacement:
    """Where a link's annotation sits, as a position *on the link*."""

    #: How far along the route, ``0`` at the source end and ``1`` at the target.
    at: float = 0.5
    #: How far off the line, in points.
    dx: float = 0.0
    dy: float = 0.0

    @property
    def offset(self) -> tuple[float, float]:
        return (self.dx, self.dy)

    def to_model(self) -> LabelGeometry:
        payload: dict[str, Any] = {"at": round_coordinate(self.at)}
        if self.dx or self.dy:
            payload["offset"] = {"x": round_coordinate(self.dx), "y": round_coordinate(self.dy)}
        return LabelGeometry.model_validate(payload)

    @classmethod
    def from_model(cls, geometry: LabelGeometry) -> LabelPlacement:
        offset = geometry.offset
        return cls(
            at=geometry.at,
            dx=0.0 if offset is None else offset.x,
            dy=0.0 if offset is None else offset.y,
        )

    def rounded(self) -> LabelPlacement:
        return LabelPlacement(
            at=round_coordinate(self.at),
            dx=round_coordinate(self.dx),
            dy=round_coordinate(self.dy),
        )


@dataclass(frozen=True, slots=True)
class LinkGeometry:
    """Everything one link's entry says: its bends, its style and its label.

    Every field is optional and the empty instance means "nothing is pinned",
    which is what :meth:`Geometry.link` hands back for a link nobody has
    touched — so a caller never has to test for ``None`` before asking what a
    link's routing is.
    """

    #: The **interior** points of the route, source end first. The two ends are
    #: the nodes themselves and are never stored: that is what lets a bend
    #: survive its endpoints being dragged.
    waypoints: tuple[tuple[float, float], ...] = ()
    #: The style this link is drawn in, or ``None`` to take the view's default.
    routing: Routing | None = None
    label: LabelPlacement | None = None

    @property
    def is_empty(self) -> bool:
        return not self.waypoints and self.routing is None and self.label is None

    def to_model(self) -> EdgeGeometry:
        payload: dict[str, Any] = {}
        if self.waypoints:
            payload["waypoints"] = [
                {"x": round_coordinate(x), "y": round_coordinate(y)} for x, y in self.waypoints
            ]
        if self.routing is not None:
            payload["routing"] = self.routing.value
        if self.label is not None:
            payload["label"] = self.label.to_model().model_dump(exclude_none=True)
        return EdgeGeometry.model_validate(payload)

    @classmethod
    def from_model(cls, geometry: EdgeGeometry) -> LinkGeometry:
        return cls(
            waypoints=tuple((point.x, point.y) for point in geometry.waypoints),
            routing=None if geometry.routing is None else Routing(geometry.routing),
            label=None if geometry.label is None else LabelPlacement.from_model(geometry.label),
        )

    def rounded(self) -> LinkGeometry:
        return LinkGeometry(
            waypoints=tuple((round_coordinate(x), round_coordinate(y)) for x, y in self.waypoints),
            routing=self.routing,
            label=None if self.label is None else self.label.rounded(),
        )


class LayoutMode(str, Enum):
    """How much of a drawing the stored arrangement decides."""

    #: Nothing is stored; lay the graph out from scratch.
    AUTO = "auto"
    #: Some nodes are placed; pin those and place the rest around them.
    PARTIAL = "partial"
    #: Every node is placed; reproduce the arrangement exactly.
    FIXED = "fixed"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Geometry:
    """One view's arrangement, merged and resolved, ready to render."""

    #: The view it belongs to, as :class:`~netgraph.render.graph.Layer` spells it.
    view: str = ""
    #: Node id to placement.
    nodes: Mapping[str, Placement] = field(default_factory=dict)
    #: Edge id to what its entry pins, in the graph's endpoint order.
    edges: Mapping[str, LinkGeometry] = field(default_factory=dict)
    #: Namespace to cluster box.
    groups: Mapping[str, Box] = field(default_factory=dict)
    #: The routing style links in this view take when they do not say for
    #: themselves — the view's own, or failing that the inventory's.
    routing: Routing | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.nodes or self.edges or self.groups or self.routing)

    def link(self, edge_id: str) -> LinkGeometry:
        """What is pinned for one link, which may be nothing at all."""
        return self.edges.get(edge_id, _NOTHING_PINNED)

    def routing_for(self, edge_id: str, *, default: Routing | None = None) -> Routing:
        """The style one link is drawn in, most specific answer first.

        The link's own entry beats everything, because it is a decision
        somebody made about *that cable*; then the caller's override (which is
        where ``--routing`` arrives), then the view's, then the inventory's.
        """
        pinned = self.link(edge_id).routing
        if pinned is not None:
            return pinned
        return default or self.routing or DEFAULT_ROUTING

    def mode(self, node_ids: Iterable[str]) -> LayoutMode:
        """How much of a drawing over ``node_ids`` this arrangement decides.

        An empty drawing is :attr:`LayoutMode.AUTO`: there is nothing to
        reproduce, and claiming a fixed layout for it would send an empty graph
        through the no-op engine for no reason.
        """
        ids = tuple(node_ids)
        if not ids or not self.nodes:
            return LayoutMode.AUTO
        placed = sum(1 for node_id in ids if node_id in self.nodes)
        if placed == 0:
            return LayoutMode.AUTO
        return LayoutMode.FIXED if placed == len(ids) else LayoutMode.PARTIAL

    def narrowed(self, node_ids: Iterable[str], edge_ids: Iterable[str]) -> Geometry:
        """This arrangement with everything the drawing does not contain dropped.

        A filtered graph must not be laid out against coordinates for nodes it
        no longer has: those nodes would still count towards
        :meth:`mode` and a drawing of three devices would be judged partially
        placed because the other ninety-seven are not there.
        """
        keep_nodes = set(node_ids)
        keep_edges = set(edge_ids)
        return Geometry(
            view=self.view,
            nodes={k: v for k, v in self.nodes.items() if k in keep_nodes},
            edges={k: v for k, v in self.edges.items() if k in keep_edges},
            groups=dict(self.groups),
            routing=self.routing,
        )


#: What :meth:`Geometry.link` answers for a link nothing is pinned for. One
#: shared instance because it is immutable and asked for constantly.
_NOTHING_PINNED: Final = LinkGeometry()


def points_of(geometry: EdgeGeometry) -> tuple[tuple[float, float], ...]:
    """The waypoints of an edge as plain pairs."""
    return tuple(_pair(point) for point in geometry.waypoints)


def _pair(point: Point) -> tuple[float, float]:
    return (point.x, point.y)
