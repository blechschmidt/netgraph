"""draw.io interchange: the mxGraph model, both ways.

netviz's pitch is "draw.io for infrastructure, with the YAML as the source of
truth". This package is where it meets the actual tool: a diagram can be handed
to somebody who has never installed netviz, edited in draw.io, and brought
back as a reviewable changeset against the inventory.

The pieces, in the order a diagram passes through them:

:mod:`~netviz.drawio.identity`
    The ``netviz:*`` attributes that say which inventory document a cell
    stands for. The whole round trip rests on these; the label deliberately
    carries nothing, so that editing it can *mean* something.
:mod:`~netviz.drawio.model`
    The neutral form — cells, a diagram, and the map between netviz's
    coordinate system and draw.io's.
:mod:`~netviz.drawio.mxfile`
    The file itself: both the plain and the deflate+base64 encodings draw.io
    writes, read and written.
:mod:`~netviz.drawio.styles`
    How a node, a link and a namespace look, including the shipped icons
    inlined as data URIs so an exported file needs nothing beside it.
:mod:`~netviz.drawio.build`
    Graph in, mxGraph model out. Used by ``netviz export drawio``.
:mod:`~netviz.drawio.annotations`
    The notes, areas and legends of §21 as native mxGraph shapes — a sticky
    note, a container, a key — rather than as a picture of them.
:mod:`~netviz.drawio.markup`
    The markdown subset a note is written in, as the HTML an mxGraph label
    holds, and back again.
:mod:`~netviz.drawio.reconcile`
    Edited model in, :mod:`netviz.edit` operations out. Used by ``netviz
    import drawio``, which shows them as a :mod:`netviz.plan` changeset before
    anything is written.

``docs/drawio.md`` is the prose version, including the one thing a reader most
needs: what a draw.io user may and may not safely change.
"""

from __future__ import annotations

from netviz.drawio.annotations import AnnotationCells, annotation_cells, place_annotations
from netviz.drawio.build import BuildOptions, build_diagram, cell_id, element_of
from netviz.drawio.identity import (
    ANNOTATION_ROLES,
    ATTRIBUTES,
    MODEL_VERSION,
    NAMESPACE_PREFIX,
    NAMESPACE_URI,
    CellRole,
    Placedness,
    Scope,
    content_hash,
    qualified,
)
from netviz.drawio.markup import html_to_markup, markup_html, plain_text
from netviz.drawio.model import Cell, Diagram, Frame, absolute_geometry
from netviz.drawio.mxfile import (
    DrawioFormatError,
    decode_diagram,
    encode_diagram,
    parse_mxfile,
    write_mxfile,
)
from netviz.drawio.reconcile import (
    Level,
    Note,
    ReconcileOptions,
    Reconciliation,
    infer_kind,
    reconcile,
)
from netviz.drawio.styles import data_uri, edge_style, group_style, node_style

__all__ = [
    "ANNOTATION_ROLES",
    "ATTRIBUTES",
    "MODEL_VERSION",
    "NAMESPACE_PREFIX",
    "NAMESPACE_URI",
    "AnnotationCells",
    "BuildOptions",
    "Cell",
    "CellRole",
    "Diagram",
    "DrawioFormatError",
    "Frame",
    "Level",
    "Note",
    "Placedness",
    "ReconcileOptions",
    "Reconciliation",
    "Scope",
    "absolute_geometry",
    "annotation_cells",
    "build_diagram",
    "cell_id",
    "content_hash",
    "data_uri",
    "decode_diagram",
    "edge_style",
    "element_of",
    "encode_diagram",
    "group_style",
    "html_to_markup",
    "infer_kind",
    "markup_html",
    "node_style",
    "parse_mxfile",
    "place_annotations",
    "plain_text",
    "qualified",
    "reconcile",
    "write_mxfile",
]
