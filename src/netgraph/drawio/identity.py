"""What makes a draw.io cell *this* element, and not one that looks like it.

A diagram exported for somebody else to edit comes back with the labels moved,
the boxes dragged and — if the exercise worked — a change somebody wanted. None
of that may be allowed to decide *which* inventory document a cell stands for.
A label is a display string: two switches may share one, a reader may retype
one, and draw.io itself will happily let a stakeholder call the core router
"the thing in the cupboard". So identity lives in custom attributes on the
cell, in a namespace of netgraph's own, and the label is left free to be edited
— which is the point, since a changed label is how a *rename* is expressed.

The attributes, all in the ``netgraph`` namespace:

``role``
    What sort of cell this is: ``node``, ``link``, ``group`` or ``metadata``.
    Read first, so nothing else has to be inferred from a style string.
``node`` / ``link``
    The **layout key**: the id :mod:`netgraph.render.graph` gives the node or
    edge, which is what a ``kind: layout`` document is keyed by (§18). Present
    on every netgraph cell, derived ones included.
``name``
    The fully-qualified name of the *element* the cell stands for, when it
    stands for one. Absent on a derived node — a subnet, a rack — because no
    document declares it, and absent on a foreign cell for the obvious reason.
``kind``
    ``switch``, ``cable``, ``tunnel``, ``namespace``… Enough to decide what a
    cell would have to become, without loading the inventory.
``document``
    Where the element is written, relative to the inventory root. Reported
    rather than acted on: it is what makes "which file do I edit?" answerable
    from the diagram alone.
``hash``
    A digest of the element's body (:func:`~netgraph.plan.document.body_of`) as
    it was when the file was exported. Compared on import so a diagram edited
    against a version of the inventory that has since moved on is *reported*
    rather than silently reconciled against something else.
``x`` / ``y`` / ``width`` / ``height`` / ``waypoints``
    Where the cell was when it left netgraph, in netgraph's own coordinates.
    This is what makes "was this dragged?" an exact question rather than a
    guess: a cell whose position still matches produces no geometry write at
    all, which is what lets a round trip through draw.io be a no-op. The two
    extents are written for an annotation, which is a box somebody resizes as
    readily as they move it, and not for a node, whose size is the icon's.
``text`` / ``label``
    What an annotation *said* when it left. A note's text is the markdown
    subset (§21.1) and its cell label is HTML, so "was this edited?" cannot be
    asked of the label alone; it is asked by rendering the stamped source again
    and comparing. Same idea, one step shorter, for an area's caption.

An annotation cell (§21) carries the same block, with ``kind`` holding the
document kind — ``note``, ``area``, ``legend`` — and ``name`` its fully-qualified
name. That is deliberate: an annotation is a document like any other, and the
import reconciles a dragged note by exactly the machinery that reconciles a
dragged switch.

Nothing here reads or writes XML; :mod:`netgraph.drawio.mxfile` does that, and
:mod:`netgraph.drawio.reconcile` is what acts on the answers.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Final

__all__ = [
    "ANNOTATION_ROLES",
    "ATTRIBUTES",
    "ATTR_ANNOTATED",
    "ATTR_DOCUMENT",
    "ATTR_GENERATOR",
    "ATTR_HASH",
    "ATTR_HEIGHT",
    "ATTR_KIND",
    "ATTR_LABEL",
    "ATTR_LINK",
    "ATTR_NAME",
    "ATTR_NODE",
    "ATTR_ORIGIN_X",
    "ATTR_ORIGIN_Y",
    "ATTR_PLACED",
    "ATTR_ROLE",
    "ATTR_ROUTING",
    "ATTR_SCOPE",
    "ATTR_SOURCE",
    "ATTR_SOURCE_PORT",
    "ATTR_TARGET",
    "ATTR_TARGET_PORT",
    "ATTR_TEXT",
    "ATTR_VERSION",
    "ATTR_VIEW",
    "ATTR_WAYPOINTS",
    "ATTR_WIDTH",
    "ATTR_X",
    "ATTR_Y",
    "HASH_DIGITS",
    "MODEL_VERSION",
    "NAMESPACE_PREFIX",
    "NAMESPACE_URI",
    "CellRole",
    "Placedness",
    "Scope",
    "content_hash",
    "format_points",
    "parse_points",
    "qualified",
]

#: The XML prefix the attributes are written under. Declared on ``<mxfile>``,
#: which keeps the document well-formed; :mod:`netgraph.drawio.mxfile` also
#: accepts a file whose declaration an editor dropped, because a diagram that
#: cannot be read back is worse than one that is slightly out of spec.
NAMESPACE_PREFIX: Final = "netgraph"

#: The namespace the prefix is bound to. A URL that is netgraph's, never
#: fetched: it is an identifier, in the XML tradition.
NAMESPACE_URI: Final = "https://netgraph.dev/schema/drawio/v1"

#: Bumped when the meaning of an attribute changes in a way an older importer
#: would get wrong. Read on import, so a file from the future is refused with a
#: sentence rather than reconciled into nonsense.
MODEL_VERSION: Final = "1"

ATTR_ROLE: Final = "role"
ATTR_KIND: Final = "kind"
ATTR_NAME: Final = "name"
ATTR_NODE: Final = "node"
ATTR_LINK: Final = "link"
ATTR_DOCUMENT: Final = "document"
ATTR_HASH: Final = "hash"
ATTR_VIEW: Final = "view"
ATTR_X: Final = "x"
ATTR_Y: Final = "y"
ATTR_WIDTH: Final = "width"
ATTR_HEIGHT: Final = "height"
ATTR_TEXT: Final = "text"
ATTR_PLACED: Final = "placed"
ATTR_WAYPOINTS: Final = "waypoints"
ATTR_ROUTING: Final = "routing"
ATTR_LABEL: Final = "label"
ATTR_SOURCE: Final = "source"
ATTR_TARGET: Final = "target"
ATTR_SOURCE_PORT: Final = "sourcePort"
ATTR_TARGET_PORT: Final = "targetPort"
ATTR_ORIGIN_X: Final = "originX"
ATTR_ORIGIN_Y: Final = "originY"
ATTR_VERSION: Final = "version"
ATTR_GENERATOR: Final = "generator"
ATTR_SCOPE: Final = "scope"

#: On the metadata cell: was this file written by an exporter that draws
#: annotations at all? Read on import, and the answer decides one thing only —
#: whether a *missing* annotation cell may be read as a deletion. A diagram
#: exported before §21 holds no note cells because none were ever written, and
#: reconciling that as "delete every note in the inventory" is the same class of
#: mistake as deleting an estate from a filtered export.
ATTR_ANNOTATED: Final = "annotated"

#: Every attribute netgraph writes, so a reader can tell one of its own from a
#: key some other tool put in the same namespace by accident.
ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        ATTR_ROLE,
        ATTR_KIND,
        ATTR_NAME,
        ATTR_NODE,
        ATTR_LINK,
        ATTR_DOCUMENT,
        ATTR_HASH,
        ATTR_VIEW,
        ATTR_X,
        ATTR_Y,
        ATTR_WIDTH,
        ATTR_HEIGHT,
        ATTR_TEXT,
        ATTR_LABEL,
        ATTR_PLACED,
        ATTR_WAYPOINTS,
        ATTR_ROUTING,
        ATTR_SOURCE,
        ATTR_TARGET,
        ATTR_SOURCE_PORT,
        ATTR_TARGET_PORT,
        ATTR_ORIGIN_X,
        ATTR_ORIGIN_Y,
        ATTR_VERSION,
        ATTR_GENERATOR,
        ATTR_SCOPE,
        ATTR_ANNOTATED,
    }
)


def qualified(attribute: str) -> str:
    """``kind`` → ``netgraph:kind``, the spelling that goes in the file."""
    return f"{NAMESPACE_PREFIX}:{attribute}"


class CellRole(str, Enum):
    """What a netgraph-authored cell is."""

    #: A vertex standing for something the diagram draws as a node: a declared
    #: element, or a derived one such as a subnet or a rack.
    NODE = "node"
    #: An edge: a cable, a tunnel, an attachment, an adjacency.
    LINK = "link"
    #: A namespace frame — a container the nodes of one folder sit inside.
    GROUP = "group"
    #: The invisible cell carrying what is true of the whole diagram: the view,
    #: the coordinate origin, the model version.
    METADATA = "metadata"
    #: A ``kind: note`` callout (§21). Its own role rather than a ``node`` with
    #: an odd kind, because the import must never read one as an element: a note
    #: that vanished is a deleted *note*, not a deleted switch.
    NOTE = "note"
    #: A ``kind: area`` zone, drawn behind the nodes it encloses.
    AREA = "area"
    #: Part of a ``kind: legend`` key — the frame, a swatch or a caption. All
    #: three share the role because all three are generated presentation and the
    #: import does exactly one thing with them: nothing.
    LEGEND = "legend"
    #: The dashed line from a note to what it is about. An edge in mxGraph and
    #: nothing at all in the inventory, so it is kept out of the ``link`` role
    #: that a cable takes.
    LEADER = "leader"

    def __str__(self) -> str:
        return self.value


#: The roles that stand for an annotation document, by the document kind. Every
#: other role in a file stands for an element or for the file itself.
ANNOTATION_ROLES: Final[dict[str, CellRole]] = {
    "note": CellRole.NOTE,
    "area": CellRole.AREA,
    "legend": CellRole.LEGEND,
}


class Placedness(str, Enum):
    """Where a cell's exported position came from."""

    #: From a ``kind: layout`` document: somebody arranged this.
    STORED = "stored"
    #: Invented by the export because nothing was stored. Reported in the
    #: manifest, and — crucially — *not* written back when it comes home
    #: unchanged, so exporting an unarranged inventory and importing it again
    #: does not silently commit an arrangement nobody chose.
    AUTO = "auto"

    def __str__(self) -> str:
        return self.value


class Scope(str, Enum):
    """How much of the inventory the exported diagram claims to hold."""

    #: Every element of the view. A node that is missing from such a file was
    #: deleted in draw.io, and reconciling it as a deletion is safe.
    COMPLETE = "complete"
    #: A filtered view. Absence proves nothing — the element may simply never
    #: have been drawn — so nothing is ever deleted on the strength of it.
    PARTIAL = "partial"

    def __str__(self) -> str:
        return self.value


#: Hex digits kept of the body digest. Sixty-four bits: this is a staleness
#: check on a file a human is carrying around, not a signature, and a full
#: SHA-256 in every cell would triple the size of the identity block.
HASH_DIGITS: Final = 16


def content_hash(body: Any) -> str:
    """A stable digest of one element's body, as ``sha256:<16 hex digits>``.

    The body is serialised as canonical JSON — sorted keys, no insignificant
    whitespace — so the digest depends on what the document *says* and not on
    how it was written. Two checkouts of one inventory therefore export the same
    hash, which is what makes the staleness check meaningful rather than noisy.
    """
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:HASH_DIGITS]}"


def format_points(points: tuple[tuple[float, float], ...]) -> str:
    """``((1, 2), (3, 4))`` → ``"1,2 3,4"``.

    One attribute rather than a nested element: the waypoints are read back as
    a unit and never edited by hand, and a flat string survives every editor
    that treats an unknown attribute as opaque.
    """
    return " ".join(f"{_number(x)},{_number(y)}" for x, y in points)


def parse_points(text: str) -> tuple[tuple[float, float], ...]:
    """The inverse of :func:`format_points`, ignoring anything malformed.

    A pair that does not parse is dropped rather than raised on: this is read
    from a file a third-party editor has rewritten, and losing one bend is a
    better outcome than refusing the diagram.
    """
    points: list[tuple[float, float]] = []
    for token in text.split():
        x, separator, y = token.partition(",")
        if not separator:
            continue
        try:
            points.append((float(x), float(y)))
        except ValueError:
            continue
    return tuple(points)


def _number(value: float) -> str:
    """A coordinate as short as it can be written without losing it.

    Twelve significant digits, not the six ``%g`` defaults to: a coordinate may
    legitimately be six figures before the point (``MAX_COORDINATE`` is 720 000)
    and two after it, and ``%g`` would round ``10000.25`` to ``10000.2``. A
    waypoint that does not survive being written is a link that reroutes itself
    on every round trip.
    """
    if value == int(value):
        return str(int(value))
    return f"{value:.12g}"
