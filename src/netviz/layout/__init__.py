"""Diagram geometry: storing an arrangement and rendering from it.

A draw.io-like editor cannot lay the canvas out from scratch on every keystroke.
When somebody drags a switch to where it belongs, that is a decision about the
diagram, and it has to live in the same source of truth as the rest of the model
— which for netviz means a YAML document, loaded, validated and edited the
same way everything else is.

The pieces:

:mod:`netviz.models.layout`
    The ``kind: layout`` document. A sidecar, keyed by element address, scoped
    by view. ``docs/follow-ups.md`` entry 16 records why it is a sidecar rather
    than a ``spec.position`` on each device.
:mod:`netviz.layout.geometry`
    The runtime form: one flat table of coordinates per view, and the decision
    a renderer makes from it — reproduce the arrangement exactly, pin part of it
    and lay out the rest, or lay everything out as before.
:mod:`netviz.layout.resolve`
    Merging every layout document in a tree into one of those tables, resolving
    the addresses people write into the node ids the graph uses.
:mod:`netviz.layout.graphviz`
    Reading coordinates back out of Graphviz, and putting a partially-pinned
    engine run back on the stored coordinate system.
:mod:`netviz.layout.seed`
    ``netviz layout``: running the auto-layout once and persisting the result,
    dropping it again, and pruning what no longer exists. Imported on demand
    rather than re-exported here, because it reaches into the renderer and the
    renderer reaches into this package: the leaves stay importable from either
    direction, and only the command needs both.
"""

from __future__ import annotations

from netviz.layout.geometry import (
    COORDINATE_PLACES,
    Box,
    Geometry,
    LayoutMode,
    Placement,
    round_coordinate,
)
from netviz.layout.graphviz import Drawing, Transform, fit_transform, parse_drawing, realign
from netviz.layout.resolve import Conflict, conflicts_in, resolve_geometry, resolve_key

__all__ = [
    "COORDINATE_PLACES",
    "Box",
    "Conflict",
    "Drawing",
    "Geometry",
    "LayoutMode",
    "Placement",
    "Transform",
    "conflicts_in",
    "fit_transform",
    "parse_drawing",
    "realign",
    "resolve_geometry",
    "resolve_key",
    "round_coordinate",
]
