"""draw.io interchange: the mxGraph model, both ways.

netgraph's pitch is "draw.io for infrastructure, with the YAML as the source of
truth". This package is where it meets the actual tool: a diagram can be handed
to somebody who has never installed netgraph, edited in draw.io, and brought
back as a reviewable changeset against the inventory.

The pieces, in the order a diagram passes through them:

:mod:`~netgraph.drawio.identity`
    The ``netgraph:*`` attributes that say which inventory document a cell
    stands for. The whole round trip rests on these; the label deliberately
    carries nothing, so that editing it can *mean* something.
:mod:`~netgraph.drawio.model`
    The neutral form — cells, a diagram, and the map between netgraph's
    coordinate system and draw.io's.
:mod:`~netgraph.drawio.mxfile`
    The file itself: both the plain and the deflate+base64 encodings draw.io
    writes, read and written.
:mod:`~netgraph.drawio.styles`
    How a node, a link and a namespace look, including the shipped icons
    inlined as data URIs so an exported file needs nothing beside it.
:mod:`~netgraph.drawio.build`
    Graph in, mxGraph model out. Used by ``netgraph export drawio``.
:mod:`~netgraph.drawio.reconcile`
    Edited model in, :mod:`netgraph.edit` operations out. Used by ``netgraph
    import drawio``, which shows them as a :mod:`netgraph.plan` changeset before
    anything is written.

``docs/drawio.md`` is the prose version, including the one thing a reader most
needs: what a draw.io user may and may not safely change.
"""

from __future__ import annotations

from netgraph.drawio.build import BuildOptions, build_diagram, cell_id, element_of
from netgraph.drawio.identity import (
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
from netgraph.drawio.model import Cell, Diagram, Frame, absolute_geometry
from netgraph.drawio.mxfile import (
    DrawioFormatError,
    decode_diagram,
    encode_diagram,
    parse_mxfile,
    write_mxfile,
)
from netgraph.drawio.reconcile import (
    Level,
    Note,
    ReconcileOptions,
    Reconciliation,
    infer_kind,
    reconcile,
)
from netgraph.drawio.styles import data_uri, edge_style, group_style, node_style

__all__ = [
    "ATTRIBUTES",
    "MODEL_VERSION",
    "NAMESPACE_PREFIX",
    "NAMESPACE_URI",
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
    "build_diagram",
    "cell_id",
    "content_hash",
    "data_uri",
    "decode_diagram",
    "edge_style",
    "element_of",
    "encode_diagram",
    "group_style",
    "infer_kind",
    "node_style",
    "parse_mxfile",
    "qualified",
    "reconcile",
    "write_mxfile",
]
