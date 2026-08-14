"""The ``layout`` document kind: hand-arranged diagram geometry (§18).

A ``kind: layout`` document says *where things are drawn*. It carries no
network facts at all — no interface, no address, no link — only coordinates::

    apiVersion: netgraph.dev/v1alpha1
    kind: layout
    metadata:
      name: default
    spec:
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
          groups:
            core:
              position: {x: 240, y: 468}
              size: {width: 220, height: 260}

**A layout is not an element.** Like :mod:`~netgraph.models.template` it is
never indexed among the elements, never drawn as a node, never listed by
``netgraph list`` and never cabled. It is a *sidecar*, and the reasoning for
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
error (``NG-Y001``): deleting a switch must not break ``netgraph validate``,
and ``netgraph layout --prune`` is the one-line fix.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator, Field, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.element import LAYOUT_KIND
from netgraph.models.metadata import Metadata
from netgraph.models.scalars import ApiVersion

__all__ = [
    "LAYOUT_KIND",
    "LAYOUT_VIEWS",
    "MAX_COORDINATE",
    "MAX_GEOMETRY_ENTRIES",
    "MAX_WAYPOINTS",
    "EdgeGeometry",
    "GroupGeometry",
    "Layout",
    "LayoutSpec",
    "NodeGeometry",
    "Point",
    "Size",
    "ViewGeometry",
]

#: The views geometry may be scoped by, one per drawable layer. Written out
#: rather than derived from :class:`~netgraph.render.graph.Layer` because the
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
                rule="NG-Y003",
            )
        return dict(zip(keys, value, strict=True))
    return value


class Point(NetgraphModel):
    """A position in points, ``y`` upwards, origin at the bottom left."""

    x: Coordinate
    y: Coordinate

    @model_validator(mode="before")
    @classmethod
    def _accept_sequence(cls, value: Any) -> Any:
        return _pair(value, keys=("x", "y"), noun="point")

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


class Size(NetgraphModel):
    """An extent in points. Both dimensions are strictly positive."""

    width: Extent
    height: Extent

    @model_validator(mode="before")
    @classmethod
    def _accept_sequence(cls, value: Any) -> Any:
        return _pair(value, keys=("width", "height"), noun="size")


class NodeGeometry(NetgraphModel):
    """Where one node is drawn, and optionally how big it is."""

    #: The centre of the node.
    position: Point
    #: The box it occupies. Omitted means "whatever the label needs", which is
    #: what keeps an arrangement valid when a device grows a port.
    size: Size | None = None


class EdgeGeometry(NetgraphModel):
    """The bends one link is drawn through.

    The points become Graphviz spline control points, in the order written, from
    the ``source`` end to the ``target`` end of the edge as the graph orders
    them. An empty list is refused: an edge with no waypoints is an edge with no
    geometry, and the way to say that is to leave the entry out.
    """

    waypoints: Annotated[tuple[Point, ...], Field(min_length=1, max_length=MAX_WAYPOINTS)]


class GroupGeometry(NetgraphModel):
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
            rule="NG-Y003",
        )
    if len(value) > MAX_GEOMETRY_ENTRIES:
        raise field_error(
            f"{section!r} places {len(value)} things; at most {MAX_GEOMETRY_ENTRIES} "
            "may be arranged in one view",
            rule="NG-Y003",
        )
    for key in value:
        if not isinstance(key, str) or not key.strip():
            raise field_error(
                f"{section!r} is keyed by address; {echo_value(key)} is not one",
                rule="NG-Y003",
            )
    return value


class ViewGeometry(NetgraphModel):
    """Everything one layer's drawing places, keyed by address."""

    nodes: dict[str, NodeGeometry] = Field(default_factory=dict)
    edges: dict[str, EdgeGeometry] = Field(default_factory=dict)
    groups: dict[str, GroupGeometry] = Field(default_factory=dict)

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
        return not (self.nodes or self.edges or self.groups)


class LayoutSpec(NetgraphModel):
    """The arrangements one layout document carries, one per view."""

    views: dict[str, ViewGeometry] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _check_view_names(cls, value: Any) -> Any:
        """``NG-Y003`` — a view is one of the layers netgraph draws."""
        if not isinstance(value, dict):
            return value
        views = value.get("views")
        if views is None:
            return value
        if not isinstance(views, dict):
            raise field_error(
                f"'views' must be a mapping of view name to geometry, got {type(views).__name__}",
                rule="NG-Y003",
                path=("views",),
            )
        for name in views:
            if name not in LAYOUT_VIEWS:
                raise field_error(
                    f"unknown view {echo_value(name)}; expected one of {', '.join(LAYOUT_VIEWS)}",
                    rule="NG-Y003",
                    path=("views", name if isinstance(name, str) else str(name)),
                )
        return value


class Layout(NetgraphModel):
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
        return all(view.is_empty for view in self.spec.views.values())

    def __str__(self) -> str:
        return f"{LAYOUT_KIND}/{self.metadata.name}"
