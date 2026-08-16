"""The ``layout`` document kind: hand-arranged diagram geometry (§18).

A ``kind: layout`` document says *where things are drawn*. It carries no
network facts at all — no interface, no address, no link — only coordinates::

    apiVersion: netviz.dev/v1alpha1
    kind: layout
    metadata:
      name: default
    spec:
      routing: orthogonal
      views:
        l1:
          nodes:
            core/sw-core:
              position: {x: 240, y: 396}
            core/rtr-edge:
              position: {x: 240, y: 540}
          edges:
            core/cbl-uplink:
              waypoints: [{x: 240, y: 470}]
              routing: straight
              label: {at: 0.35, offset: {x: 0, y: 12}}
          groups:
            core:
              position: {x: 240, y: 468}
              size: {width: 220, height: 260}

**A layout is not an element.** Like :mod:`~netviz.models.template` it is
never indexed among the elements, never drawn as a node, never listed by
``netviz list`` and never cabled. It is a *sidecar*, and the reasoning for
making it one rather than a ``spec.position`` on each device is recorded in
``docs/follow-ups.md`` entry 16; the short version is that a device's file
should stay a description of hardware, an arrangement should be droppable and
versionable on its own, and the same device sits in a different place in each
view — which a single field on the device cannot express.

Coordinates
-----------

Everything is in **points** (1/72 inch), in Graphviz's coordinate system:
``x`` grows to the right, ``y`` grows *upwards*, and a ``position`` is the
**centre** of the thing it places. That is deliberate rather than incidental —
it is the system the renderer feeds back to Graphviz verbatim (``neato -n2``),
so a stored arrangement reproduces exactly, and it is the system the JSON export
publishes so a browser can draw the same picture.

``size`` is optional on a node and required on a group. A node without one is
sized by its label, which is what makes an arrangement survive a device growing
an interface: the position is the decision, the size is a consequence.

Keys
----

A node key is the address of what it places, spelled the way references are
spelled everywhere else — a short name resolved against the layout document's
own namespace, or a fully-qualified one. Derived nodes that no document
declares are keyed by the id the graph gives them: ``subnet:10.0.0.0/24``,
``tunnel:site/wg0``, ``rack:hq/comms/r1``. An edge key is a cable's or tunnel's
address, or the synthetic id of a derived edge. A group key is a namespace.

A key naming something the inventory no longer has is a **warning**, not an
error (``NV-Y001``): deleting a switch must not break ``netviz validate``,
and ``netviz layout --prune`` is the one-line fix.

Links
-----

A link is geometry too, and an edge entry says three separable things about
one:

``waypoints``
    The bends the cable is dragged through, **interior points only**. The two
    ends are always the nodes themselves, so a route survives either of them
    being moved: drag the switch and the bends stay put, which is what makes a
    hand-routed trunk worth placing. An entry with no waypoints is a straight
    run between the two nodes.
``routing``
    ``spline`` (the curve Graphviz draws), ``orthogonal`` (right angles, the
    way a patch schedule is drawn) or ``straight`` (segment to segment). Set on
    the link it applies to; :attr:`ViewGeometry.routing` is the default for one
    view and :attr:`LayoutSpec.routing` the default for the whole inventory,
    and the most specific one wins.
``label``
    Where the link's annotation sits: ``at`` is how far along the route, from
    ``0`` at the source end to ``1`` at the target end, and ``offset`` nudges
    it off the line in points. This is what makes a dense VLAN diagram legible.

None of the three is required, but an entry must carry at least one of them:
an edge key with nothing under it says nothing, and the way to say nothing is
to leave the key out.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator, Field, model_validator

from netviz.errors import echo_value
from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.element import LAYOUT_KIND
from netviz.models.metadata import Metadata
from netviz.models.scalars import ApiVersion

__all__ = [
    "LAYOUT_KIND",
    "LAYOUT_VIEWS",
    "MAX_COORDINATE",
    "MAX_GEOMETRY_ENTRIES",
    "MAX_WAYPOINTS",
    "ROUTING_STYLES",
    "EdgeGeometry",
    "GroupGeometry",
    "LabelGeometry",
    "Layout",
    "LayoutSpec",
    "NodeGeometry",
    "Point",
    "Size",
    "ViewGeometry",
]

#: The views geometry may be scoped by, one per drawable layer. Written out
#: rather than derived from :class:`~netviz.render.graph.Layer` because the
#: models must not import the renderer; ``tests/test_layout.py`` asserts the two
#: agree exactly, so a new layer cannot be added without a view for it.
LAYOUT_VIEWS: Final[tuple[str, ...]] = (
    "physical",
    "l1",
    "l2",
    "l3",
    "overlay",
    "routing",
    "rack",
    "power",
    "identity",
    "netns",
    "security",
)

#: Largest coordinate magnitude, in points. A drawing 10 000 inches across is
#: not a diagram anybody will read, and the bound keeps a hand-typed exponent
#: out of the Graphviz command line.
MAX_COORDINATE: Final = 720_000.0

#: Most control points one edge may carry. Graphviz splines are cubic Béziers;
#: sixty-four bends is far past what a person places by hand.
MAX_WAYPOINTS: Final = 64

#: Most entries one section of one view may hold. An arrangement of a hundred
#: thousand nodes is a generated file, not a hand-arranged diagram.
MAX_GEOMETRY_ENTRIES: Final = 100_000

#: How a link is drawn between its bends. Spelled the way a person would say it
#: rather than the way Graphviz spells it (``true``/``ortho``/``line``), because
#: this is inventory somebody writes by hand; :mod:`netviz.layout.routing`
#: owns the translation.
ROUTING_STYLES: Final[tuple[str, ...]] = ("spline", "orthogonal", "straight")


def _coordinate(value: Any) -> Any:
    """Refuse a coordinate that is not a finite, sanely-bounded number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a number of points, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{echo_value(value)} is not a finite coordinate")
    if abs(number) > MAX_COORDINATE:
        raise ValueError(
            f"{number:g} is further than {MAX_COORDINATE:g} points from the origin; "
            "a diagram that large is a mistake"
        )
    return number


#: One coordinate, in points. Stored as a ``float`` whatever the document wrote,
#: so ``240`` and ``240.0`` are one value and two documents that mean the same
#: arrangement compare equal.
Coordinate = Annotated[float, BeforeValidator(_coordinate)]

#: A positive extent, in points.
Extent = Annotated[float, BeforeValidator(_coordinate), Field(gt=0.0)]


def _pair(value: Any, *, keys: tuple[str, str], noun: str) -> Any:
    """Accept ``[a, b]`` as shorthand for ``{keys[0]: a, keys[1]: b}``.

    A two-element sequence is how anybody writing an arrangement by hand wants
    to spell a point, and how a diff of one reads. The mapping form stays
    canonical because it is the one that says which number is which.
    """
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise field_error(
                f"a {noun} written as a sequence must have exactly two numbers "
                f"({keys[0]} then {keys[1]}), got {len(value)}",
                rule="NV-Y003",
            )
        return dict(zip(keys, value, strict=True))
    return value


class Point(NetvizModel):
    """A position in points, ``y`` upwards, origin at the bottom left."""

    x: Coordinate
    y: Coordinate

    @model_validator(mode="before")
    @classmethod
    def _accept_sequence(cls, value: Any) -> Any:
        return _pair(value, keys=("x", "y"), noun="point")

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


class Size(NetvizModel):
    """An extent in points. Both dimensions are strictly positive."""

    width: Extent
    height: Extent

    @model_validator(mode="before")
    @classmethod
    def _accept_sequence(cls, value: Any) -> Any:
        return _pair(value, keys=("width", "height"), noun="size")


class NodeGeometry(NetvizModel):
    """Where one node is drawn, and optionally how big it is."""

    #: The centre of the node.
    position: Point
    #: The box it occupies. Omitted means "whatever the label needs", which is
    #: what keeps an arrangement valid when a device grows a port.
    size: Size | None = None


class LabelGeometry(NetvizModel):
    """Where a link's annotation sits, relative to the link.

    Stored *on the link* rather than as a coordinate, so that nudging a VLAN
    label clear of a crossing cable survives both endpoints being dragged
    somewhere else. :attr:`at` slides it along the route and :attr:`offset`
    lifts it off, in points, in the same ``y``-upwards system as everything
    else here.
    """

    #: How far along the route, from ``0`` at the source end to ``1`` at the
    #: target end. Half way is where a renderer puts a label left alone.
    at: Annotated[float, BeforeValidator(_coordinate), Field(ge=0.0, le=1.0)] = 0.5
    #: How far off the line, in points. Omitted means "on it".
    offset: Point | None = None


class EdgeGeometry(NetvizModel):
    """How one link is drawn: its bends, its routing style and its label.

    The waypoints are the **interior** points of the route, in the order
    written, from the ``source`` end to the ``target`` end of the edge as the
    graph orders them; the two ends are the nodes, which is why moving a node
    moves the route without invalidating the bends. An entry carrying none of
    the three fields is refused (``NV-Y003``): it says nothing, and the way to
    say nothing is to leave the key out.
    """

    waypoints: Annotated[tuple[Point, ...], Field(max_length=MAX_WAYPOINTS)] = ()
    #: One of :data:`ROUTING_STYLES`, or ``None`` to take the view's default.
    routing: Literal["spline", "orthogonal", "straight"] | None = None
    label: LabelGeometry | None = None

    @model_validator(mode="after")
    def _says_something(self) -> EdgeGeometry:
        if not self.waypoints and self.routing is None and self.label is None:
            raise field_error(
                "an edge entry must say something about the link — waypoints, "
                "routing or label; drop the key to say nothing",
                rule="NV-Y003",
            )
        return self


class GroupGeometry(NetvizModel):
    """The box one namespace cluster is drawn as.

    Unlike a node, a group carries a required :attr:`size`: nothing else decides
    how big it is, because a cluster is drawn around whatever the arrangement
    put inside it rather than around whatever Graphviz packed into it.
    """

    #: The centre of the box.
    position: Point
    size: Size


def _entries(value: Any, *, section: str) -> Any:
    """Reject a section that is not a mapping, or that is absurdly large."""
    if value is None:
        return value
    if not isinstance(value, dict):
        raise field_error(
            f"{section!r} must be a mapping of address to geometry, got {type(value).__name__}",
            rule="NV-Y003",
        )
    if len(value) > MAX_GEOMETRY_ENTRIES:
        raise field_error(
            f"{section!r} places {len(value)} things; at most {MAX_GEOMETRY_ENTRIES} "
            "may be arranged in one view",
            rule="NV-Y003",
        )
    for key in value:
        if not isinstance(key, str) or not key.strip():
            raise field_error(
                f"{section!r} is keyed by address; {echo_value(key)} is not one",
                rule="NV-Y003",
            )
    return value


class ViewGeometry(NetvizModel):
    """Everything one layer's drawing places, keyed by address."""

    nodes: dict[str, NodeGeometry] = Field(default_factory=dict)
    edges: dict[str, EdgeGeometry] = Field(default_factory=dict)
    groups: dict[str, GroupGeometry] = Field(default_factory=dict)
    #: How links in this view are drawn when they do not say for themselves.
    #: ``None`` takes :attr:`LayoutSpec.routing`.
    routing: Literal["spline", "orthogonal", "straight"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_sections(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for section in ("nodes", "edges", "groups"):
                if section in value:
                    _entries(value[section], section=section)
        return value

    @property
    def is_empty(self) -> bool:
        """Does this view place nothing at all?"""
        return not (self.nodes or self.edges or self.groups or self.routing)


class LayoutSpec(NetvizModel):
    """The arrangements one layout document carries, one per view."""

    views: dict[str, ViewGeometry] = Field(default_factory=dict)
    #: The inventory-wide default routing style, overridden per view and per
    #: link. ``None`` means ``spline`` — the curve Graphviz has always drawn.
    routing: Literal["spline", "orthogonal", "straight"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_view_names(cls, value: Any) -> Any:
        """``NV-Y003`` — a view is one of the layers netviz draws."""
        if not isinstance(value, dict):
            return value
        views = value.get("views")
        if views is None:
            return value
        if not isinstance(views, dict):
            raise field_error(
                f"'views' must be a mapping of view name to geometry, got {type(views).__name__}",
                rule="NV-Y003",
                path=("views",),
            )
        for name in views:
            if name not in LAYOUT_VIEWS:
                raise field_error(
                    f"unknown view {echo_value(name)}; expected one of {', '.join(LAYOUT_VIEWS)}",
                    rule="NV-Y003",
                    path=("views", name if isinstance(name, str) else str(name)),
                )
        return value


class Layout(NetvizModel):
    """A ``kind: layout`` document: geometry for one or more views."""

    api_version: ApiVersion = Field(alias="apiVersion", serialization_alias="apiVersion")
    kind: Literal["layout"] = "layout"
    metadata: Metadata
    spec: LayoutSpec = Field(default_factory=LayoutSpec)

    @property
    def name(self) -> str:
        """Shortcut for ``metadata.name``."""
        return self.metadata.name

    def view(self, name: str) -> ViewGeometry | None:
        """The geometry this document holds for ``name``, if any."""
        return self.spec.views.get(name)

    @property
    def is_empty(self) -> bool:
        """Does this document place nothing at all?"""
        if self.spec.routing is not None:
            return False
        return all(view.is_empty for view in self.spec.views.values())

    def __str__(self) -> str:
        return f"{LAYOUT_KIND}/{self.metadata.name}"
