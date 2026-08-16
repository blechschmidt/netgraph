"""The neutral shape of an mxGraph model, and the two coordinate systems.

Everything between the XML and the inventory speaks this. :class:`Cell` is one
mxGraph cell reduced to the fields netviz writes or reads; :class:`Diagram` is
a whole ``<mxGraphModel>`` as an ordered list of them plus the metadata cell.
The XML is :mod:`netviz.drawio.mxfile`'s problem, and the inventory is
:mod:`netviz.drawio.build`'s and :mod:`netviz.drawio.reconcile`'s.

The coordinate systems
----------------------

They disagree in all three ways two 2-D systems can:

=================  ==========================  =========================
                   netviz (§18, Graphviz)    draw.io (mxGraph)
=================  ==========================  =========================
``y``              upwards                     downwards
A position is      the **centre** of the box    its **top-left** corner
The origin is      wherever the layout put it   the top-left of the page
=================  ==========================  =========================

:class:`Frame` is the whole of the translation, and it is stored *in the
exported file* (``netviz:originX``/``originY`` on the metadata cell) rather
than recomputed on import. Recomputing it would mean deriving the origin from
the very coordinates the draw.io user has just been editing — so moving one node
far enough to change the bounding box would silently shift every other node in
the inventory. Storing it makes the inverse exact.

A third disagreement is local rather than global: a cell inside a container is
positioned **relative to its parent** in mxGraph, which is what makes dragging a
namespace frame carry its contents. :func:`absolute_geometry` walks the parent
chain so that the rest of the code can think in page coordinates.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Final

from netviz.drawio.identity import (
    ATTR_ANNOTATED,
    ATTR_ORIGIN_X,
    ATTR_ORIGIN_Y,
    ATTR_ROLE,
    ATTR_SCOPE,
    ATTR_VERSION,
    ATTR_VIEW,
    CellRole,
    Scope,
)
from netviz.layout.geometry import round_coordinate

__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "MARGIN",
    "ROOT_ID",
    "Cell",
    "Diagram",
    "Frame",
    "absolute_geometry",
    "cell_id",
]

#: The id of the layer every top-level cell hangs off. mxGraph reserves ``0``
#: for the model root and ``1`` for its default layer; both are emitted verbatim
#: because every draw.io file has them and nothing else may take the ids.
ROOT_ID: Final = "1"

#: How far the drawing is inset from the page origin, in points. draw.io draws a
#: page border and a diagram flush against it reads as clipped.
MARGIN: Final = 40.0

#: The box a node gets when the arrangement does not say how big it is. A
#: :class:`~netviz.layout.geometry.Placement` carries a size only when one was
#: stored (§18), and "as big as the label needs" has no meaning in a file that
#: is going to be opened somewhere netviz is not.
DEFAULT_WIDTH: Final = 100.0
DEFAULT_HEIGHT: Final = 60.0


@dataclass(frozen=True, slots=True)
class Frame:
    """The map between netviz's coordinates and draw.io's.

    ``origin_x`` is the netviz ``x`` that lands on draw.io's ``x = 0``, and
    ``origin_y`` the netviz ``y`` that lands on draw.io's ``y = 0``. Both
    translations are exact inverses of each other, which
    ``tests/test_drawio.py`` asserts over generated coordinates rather than
    over a handful of examples.
    """

    origin_x: float = 0.0
    origin_y: float = 0.0

    def to_drawio(self, x: float, y: float) -> tuple[float, float]:
        """A netviz point as a draw.io page point."""
        return (round_coordinate(x - self.origin_x), round_coordinate(self.origin_y - y))

    def to_netviz(self, x: float, y: float) -> tuple[float, float]:
        """A draw.io page point back in netviz's system."""
        return (round_coordinate(x + self.origin_x), round_coordinate(self.origin_y - y))

    def box_to_drawio(self, x: float, y: float, width: float, height: float) -> tuple[float, float]:
        """The top-left corner, in draw.io, of a box centred on ``(x, y)``."""
        return self.to_drawio(x - width / 2, y + height / 2)

    def box_to_netviz(self, x: float, y: float, width: float, height: float) -> tuple[float, float]:
        """The centre, in netviz, of a draw.io box with that top-left corner."""
        return self.to_netviz(x + width / 2, y + height / 2)


@dataclass(frozen=True, slots=True)
class Cell:
    """One mxGraph cell: a vertex, an edge, or the metadata carrier.

    ``x``/``y`` are draw.io coordinates **relative to** :attr:`parent`, exactly
    as the file stores them; :func:`absolute_geometry` is what turns them into
    page coordinates. ``attributes`` holds the ``netviz:*`` custom attributes
    with the prefix stripped, so a reader asks for ``kind`` rather than for
    ``netviz:kind``.
    """

    id: str
    #: ``None`` for a cell netviz did not write — every cell of a hand-made
    #: diagram, and anything a draw.io user added to one of netviz's.
    role: CellRole | None = None
    label: str = ""
    style: str = ""
    parent: str = ROOT_ID
    edge: bool = False
    vertex: bool = False
    #: Cell ids, for an edge. Empty when an end dangles, which draw.io allows
    #: and netviz cannot represent.
    source: str = ""
    target: str = ""
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    #: An edge's bends, in draw.io **page** coordinates. mxGraph stores edge
    #: points absolutely even when the edge hangs off a container, so unlike a
    #: vertex's ``x``/``y`` these need no parent walk.
    points: tuple[tuple[float, float], ...] = ()
    #: Is the cell drawn? The metadata cell is not.
    visible: bool = True
    attributes: Mapping[str, str] = field(default_factory=dict)

    def attribute(self, name: str, default: str = "") -> str:
        """One ``netviz:*`` attribute, or ``default`` when it is absent."""
        return self.attributes.get(name, default)

    @property
    def is_netviz(self) -> bool:
        """Did netviz write this cell?"""
        return self.role is not None

    @property
    def geometry(self) -> tuple[float, float, float, float]:
        """``(x, y, width, height)`` with the absent numbers defaulted."""
        return (
            self.x or 0.0,
            self.y or 0.0,
            self.width if self.width is not None else DEFAULT_WIDTH,
            self.height if self.height is not None else DEFAULT_HEIGHT,
        )


@dataclass(frozen=True, slots=True)
class Diagram:
    """A whole ``<mxGraphModel>``: its cells, and what is true of all of them."""

    #: Which netviz view this draws, as :class:`~netviz.render.graph.Layer`
    #: spells it. Empty for a diagram netviz did not write.
    view: str = ""
    #: The name draw.io shows on the tab.
    name: str = "netviz"
    cells: tuple[Cell, ...] = ()
    frame: Frame = Frame()
    scope: Scope = Scope.COMPLETE
    #: The model version the file was written at, for the compatibility gate.
    version: str = ""
    #: What wrote it, for the report. Never trusted.
    generator: str = ""
    #: Does the file come from an exporter that draws annotations (§21)? False
    #: for a diagram written before they existed — and the difference matters
    #: for exactly one decision, which
    #: :data:`~netviz.drawio.identity.ATTR_ANNOTATED` explains.
    annotated: bool = False

    def __iter__(self) -> Iterator[Cell]:
        return iter(self.cells)

    def by_id(self) -> Mapping[str, Cell]:
        """Every cell, keyed by id. Later duplicates lose, as mxGraph resolves."""
        found: dict[str, Cell] = {}
        for cell in self.cells:
            found.setdefault(cell.id, cell)
        return found

    def of_role(self, role: CellRole) -> tuple[Cell, ...]:
        """Every netviz cell of one role, in file order."""
        return tuple(cell for cell in self.cells if cell.role is role)

    @property
    def foreign(self) -> tuple[Cell, ...]:
        """Every cell netviz did not write, in file order."""
        return tuple(cell for cell in self.cells if not cell.is_netviz)

    @property
    def is_netviz(self) -> bool:
        """Was this file produced by ``netviz export drawio``?

        Answered by the metadata cell, not by the presence of the odd
        identity attribute: a diagram somebody assembled by copying cells out
        of one of netviz's has the attributes and none of the guarantees.
        """
        return bool(self.view) and any(cell.role is CellRole.METADATA for cell in self.cells)

    def metadata_cell(self) -> Cell:
        """The invisible cell carrying :attr:`frame`, :attr:`view` and the version."""
        annotated = {ATTR_ANNOTATED: "1"} if self.annotated else {}
        return Cell(
            id=METADATA_ID,
            role=CellRole.METADATA,
            vertex=True,
            visible=False,
            x=0.0,
            y=0.0,
            width=1.0,
            height=1.0,
            style=METADATA_STYLE,
            attributes={
                ATTR_ROLE: CellRole.METADATA.value,
                ATTR_VIEW: self.view,
                ATTR_VERSION: self.version,
                ATTR_SCOPE: self.scope.value,
                ATTR_ORIGIN_X: _plain(self.frame.origin_x),
                ATTR_ORIGIN_Y: _plain(self.frame.origin_y),
                **annotated,
            },
        )

    def with_cells(self, cells: tuple[Cell, ...]) -> Diagram:
        return replace(self, cells=cells)


#: Id of the metadata cell. Fixed rather than minted, so a second export
#: overwrites the first one's rather than accumulating them.
METADATA_ID: Final = "netviz-metadata"

#: How the metadata cell is drawn, which is: not at all. ``locked=1`` keeps it
#: out of a rubber-band selection, so a draw.io user cannot delete the diagram's
#: identity by dragging a box round the canvas.
METADATA_STYLE: Final = (
    "shape=rectangle;html=1;fillColor=none;strokeColor=none;opacity=0;"
    "movable=0;resizable=0;deletable=0;editable=0;locked=1;connectable=0;"
)


#: Longest slug kept in a cell id before the digest. Enough to recognise the
#: element in a raw XML diff, short enough that the ids stay one line each.
_SLUG_LIMIT: Final = 48

#: Anything that is not safe and readable inside an XML id.
_UNSAFE_IN_ID: Final = re.compile(r"[^A-Za-z0-9]+")


def cell_id(prefix: str, key: str) -> str:
    """A stable, readable, unique cell id for ``key``.

    Readable so an XML diff can be reviewed, and hashed so that two keys which
    fold to the same slug — ``sites/hq`` and ``sites-hq`` — still get two ids.
    Derived rather than random, because two exports of one inventory must
    produce the same file.

    Lives here rather than beside the builder that mints most of the ids,
    because :mod:`netviz.drawio.annotations` mints the rest and both sides of
    that pair cannot import each other.
    """
    slug = _UNSAFE_IN_ID.sub("-", key).strip("-")[:_SLUG_LIMIT] or "x"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{slug}-{digest}"


def absolute_geometry(cell: Cell, cells: Mapping[str, Cell]) -> tuple[float, float, float, float]:
    """``(x, y, width, height)`` of ``cell`` in page coordinates.

    mxGraph positions a cell relative to its parent when the parent is a
    container, so the offsets of every ancestor up to the default layer have to
    be added on. The walk is bounded: a file that claims a cycle of parents —
    which mxGraph itself would not survive — stops rather than spinning.
    """
    x, y, width, height = cell.geometry
    seen = {cell.id}
    parent = cells.get(cell.parent)
    while parent is not None and parent.id not in seen and parent.id != ROOT_ID:
        seen.add(parent.id)
        x += parent.x or 0.0
        y += parent.y or 0.0
        parent = cells.get(parent.parent)
    return (x, y, width, height)


def _plain(value: float) -> str:
    """A float as short as it can be written without losing it, for an attribute.

    Twelve significant digits; see :func:`netviz.drawio.identity._number` for
    why the six ``%g`` defaults to are not enough.
    """
    rounded = round_coordinate(value)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.12g}"
