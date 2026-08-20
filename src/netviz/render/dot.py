"""Graphviz DOT renderer, and the image formats Graphviz produces from it.

The document is laid out by a Jinja2 template
(``netviz/render/templates/graph.dot.j2``) rather than by string concatenation
here: the output is a documented artefact (``netviz render -f dot`` is meant
to be diffable and hand-editable), so its shape, attribute order and indentation
belong in one readable file. This module's job is to turn a
:class:`~netviz.render.graph.Graph` into the small view model that template
consumes, and to decide the visual encoding — shape and palette per element
kind, line style per medium, line width per link rate.

Escaping
--------

Two escaping contexts appear in the output and confusing them is the one way an
inventory name could inject DOT syntax, so the split is enforced by the
template environment rather than by discipline:

* **HTML-like label** (``label=<...>``) — the record table listing a node's
  name, kind and interfaces. The environment renders with ``autoescape=True``,
  so every bare interpolation is HTML-escaped, which is exactly what Graphviz's
  HTML-like label parser expects.
* **DOT quoted string** — node ids, element ids, tooltips, URLs, cluster labels
  and edge labels. These go through the ``dot_string`` filter
  (:func:`_dot_string`), which adds the quotes and the backslash escapes, drops
  what cannot be printed, and marks the result safe.

The template header spells out which to use where. Omitting the filter drops
the surrounding quotes and Graphviz rejects the file, so the failure mode is
loud rather than silent.

What a rendering carries besides the picture
--------------------------------------------

An SVG is the artefact that gets committed to a repository or dropped into a
wiki, so three attributes travel with it. All three are inert in a PNG or a PDF
— Graphviz simply has nowhere to put them — and none of them changes the
drawing:

* ``tooltip`` — the per-element detail of :mod:`netviz.render.details`, the
  same records ``netviz web`` shows in its info boxes, as plain text. Graphviz
  writes it as ``xlink:title`` on an anchor wrapping the shape, which not every
  browser pops up, so :func:`_promote_tooltips` moves it into the ``<title>``
  element of the shape's group on the way out. That is the one construct every
  browser has shown as a tooltip since SVG 1.1, with no JavaScript.
* ``URL`` — the link ``--link-template`` builds from the document, line and name
  each element came from (:mod:`netviz.render.links`), so a diagram in a wiki
  links back to the YAML behind every node.
* ``id`` — a stable identity derived from the fully-qualified name
  (:mod:`netviz.render.ids`), so a shape can be deep-linked and styled from
  outside. Graphviz copies it verbatim into the element it emits.

Icons
-----

With :attr:`RenderOptions.icons <netviz.render.options.RenderOptions.icons>`
set, a node is drawn as its kind's picture instead of a Graphviz shape: the
image becomes the first row of the same record table, and the node loses its
shape and fill so that nothing is drawn behind it. The file name goes into the
table as a bare ``<IMG SRC="router.svg">`` and the directory holding it into one
``imagepath`` graph attribute, which keeps the single machine-specific string in
a rendering on one line — and keeps everything interpolated into an HTML
attribute inside netviz's own alphabet, since a file name is
``<kind>.<extension>`` and a kind comes from a closed set.

Graphviz resolves that path while it lays the graph out, so an *image* rendering
would otherwise reference a file the reader may not have. SVG output therefore
gets its icons embedded as ``data:`` URIs on the way out
(:func:`_embed_icons`), which is what makes ``netviz render -f svg`` still
produce one self-contained file.

Annotations
-----------

The notes, areas and legends of §21 are drawn from
:func:`~netviz.render.annotations.annotation_views`, which resolves them once
for every backend. Two of the three are drawn differently depending on who is
laying the graph out, and that is forced rather than chosen — ``neato -n2``, the
engine that reproduces a stored arrangement, draws no clusters at all:

* an **area** is a ``subgraph cluster_area_N`` under an automatic layout and a
  rectangle in the graph's ``_background`` under a fixed one, exactly as a
  namespace frame is. A node is drawn inside at most one box, and an explicit
  area outranks both the layer's own clustering and ``--group-by-namespace``;
  see :func:`_area_groups` for the whole precedence rule.
* a **note** is an ordinary node with ``shape=note``, so it is laid out with the
  graph rather than floated over it, and its leader line is an edge with
  ``constraint=false`` — a callout must not be able to move what it comments on.
* a **legend** is a ``subgraph cluster_legend_N`` holding one ``plaintext``
  table of swatches. Its ``corner`` is honoured only in a fixed drawing;
  under an automatic layout Graphviz places it, because nothing in Graphviz
  pins a cluster to a corner and the tricks that come close distort the
  topology. :func:`_legend_views` says so at length.

Determinism is a hard requirement: the same inventory must produce
byte-identical DOT on every run, or golden-file tests and version control become
useless. Nothing here iterates a set without sorting it.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from collections.abc import Container, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Final

from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup

from netviz.errors import RenderError, clip_text, compact_ids, count_text
from netviz.layout.geometry import (
    COORDINATE_PLACES,
    Box,
    Geometry,
    LabelPlacement,
    LayoutMode,
    Routing,
)
from netviz.layout.graphviz import (
    POINTS_PER_INCH,
    DrawingError,
    parse_drawing,
    realign,
)
from netviz.layout.routing import Route, label_position
from netviz.loader.inventory import namespace_of, short_name
from netviz.models import LOCAL_ZONE, format_watts
from netviz.power import PowerNode
from netviz.render.aggregate import AggregateView
from netviz.render.annotations import (
    AnnotationViews,
    AreaView,
    LegendSwatch,
    LegendView,
    NoteView,
    Span,
    annotation_views,
    member_hull,
)
from netviz.render.details import (
    build_details,
    detail_text,
    namespace_text,
    plain_text,
    printable,
)
from netviz.render.diffview import Mark
from netviz.render.graph import (
    ANY_ZONE,
    Edge,
    EdgeKind,
    Graph,
    IpamView,
    Layer,
    Node,
    RackSlot,
    RackView,
    RoutingView,
    SecurityView,
    TunnelView,
)
from netviz.render.highlight import Highlight
from netviz.render.icons import IconTheme, suffix_order
from netviz.render.ids import ElementIds, element_ids
from netviz.render.links import Linker
from netviz.render.options import DEFAULT_RANKDIR, RenderOptions
from netviz.render.palette import (
    CLUSTER_FONT_SIZE,
    CLUSTER_LABEL_COLOUR,
    CLUSTER_NOUN,
    CLUSTER_PALETTE,
    CLUSTER_STROKE,
    DEFAULT_EDGE_PALETTE,
    DEFAULT_NODE_PALETTE,
    EDGE_PALETTE,
    NODE_PALETTE,
)
from netviz.render.routes import anchors_of, default_routing, route_plan, route_table
from netviz.render.styles import ResolvedStyle, StyleMap, dot_shape

__all__ = [
    "CLUSTER_PALETTE",
    "DEFAULT_EDGE_PALETTE",
    "DEFAULT_NODE_PALETTE",
    "DOT_ENV_VAR",
    "DOT_EXECUTABLE",
    "EDGE_PALETTE",
    "IMAGE_FORMATS",
    "NODE_PALETTE",
    "NOOP_ENGINE",
    "cluster_keys",
    "find_dot",
    "graphviz_install_hint",
    "measure_nodes",
    "missing_dot_message",
    "render_dot",
    "render_image",
    "routing_advisories",
    "run_graphviz",
    "to_dot",
    "to_image",
]

#: Formats :func:`to_image` can produce by running Graphviz.
IMAGE_FORMATS: Final[tuple[str, ...]] = ("svg", "png", "pdf")

#: The layout program shelled out to. Looked up on ``PATH``; never run through a
#: shell, and always with an argument list, so nothing in an inventory can reach
#: the command line.
#:
#: Spelled without ``.exe`` on purpose: :func:`shutil.which` consults
#: ``PATHEXT`` on Windows, so this one name finds ``dot.exe`` there and ``dot``
#: everywhere else. Appending the extension ourselves would *break* the lookup
#: on POSIX rather than help it on Windows.
DOT_EXECUTABLE: Final = "dot"

#: Names the ``dot`` binary outright, for the case ``PATH`` cannot be fixed.
#: The Windows Graphviz installer and the ``choco`` package do not always put
#: ``bin`` on ``PATH``, and a GUI-launched editor on macOS inherits a ``PATH``
#: without ``/opt/homebrew/bin`` — so an escape hatch that needs no shell
#: configuration is worth having on exactly the two platforms this repository
#: has least visibility into.
DOT_ENV_VAR: Final = "NETVIZ_DOT"

#: The Graphviz layout engine that reads a ``pos`` attribute. ``dot`` ignores
#: one outright, so an arrangement can only be reproduced through this one —
#: selected with ``-K`` on the same executable, so nothing extra has to be found
#: on ``PATH``.
NOOP_ENGINE: Final = "neato"


#: Prefix of the node id a frame's caption is emitted under. A colon cannot
#: appear in an element name, so it can never collide with one.
_FRAME_ID_PREFIX: Final = "cluster-label:"

#: Prefix of the DOT node id an annotation is emitted under (§21). A colon again:
#: a note is a node in the document, and it must not be possible for a device
#: called ``note-why-orange`` to collide with the note called ``why-orange``.
_ANNOTATION_ID_PREFIX: Final = "annotation:"

#: Where an anchored note that pins no position of its own is drawn in a fixed
#: arrangement, relative to the centre of what it is anchored to: up and to the
#: right, which is where a hand-drawn callout goes and which keeps it clear of a
#: label printed under the node.
_NOTE_OFFSET: Final[tuple[float, float]] = (140.0, 70.0)

#: The empty icon table :class:`_Look` defaults to. A shared constant behind a
#: ``default_factory``, because a ``mappingproxy`` written inline as a dataclass
#: default raises ``ValueError`` at import time on 3.11, where it is still
#: unhashable; see :data:`netviz.render.styles._NO_ORIGIN`.
_NO_ICONS: Final[Mapping[str, str]] = MappingProxyType({})

#: How far outside the drawing a legend is placed in a fixed arrangement. Enough
#: that a key never lands on a device; see :func:`_legend_pos`.
_LEGEND_MARGIN: Final = 90.0

#: Height of a legend swatch, in points: a block for a fill, a bar for a line
#: style. The width is the table's to decide, so a long label does not stretch
#: the swatch beside it.
_SWATCH_BOX: Final = 12
_SWATCH_RULE: Final = 4

#: Directories to look in when ``PATH`` does not have it, per :data:`os.name`.
#: Nothing here is a guess at a version number or a wildcard: each entry is the
#: default install location of a package manager or an installer, so a hit is a
#: real Graphviz and a miss costs one :meth:`~pathlib.Path.is_file` call.
_WELL_KNOWN_DIRS: Final[dict[str, tuple[str, ...]]] = {
    # The MSI/EXE installer and the ``choco`` package both land under Program
    # Files; the 32-bit path is there because the installer still offers it.
    "nt": (
        r"C:\Program Files\Graphviz\bin",
        r"C:\Program Files (x86)\Graphviz\bin",
        r"C:\Program Files\Graphviz\release\bin",
    ),
    # Homebrew on Apple Silicon, Homebrew on Intel, MacPorts, and the two
    # ordinary Unix prefixes -- which also covers a Linux desktop session that
    # was started with a minimal environment.
    "posix": (
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/local/bin",
        "/usr/bin",
    ),
}

#: The install command for the platform netviz is running on, first, followed
#: by the others as a note: a Windows user told to run ``apt install`` has been
#: given a worse error than no advice at all.
_INSTALL_HINTS: Final[dict[str, tuple[str, ...]]] = {
    "nt": ("winget install Graphviz.Graphviz", "choco install graphviz"),
    "posix": (
        "brew install graphviz (macOS)",
        "apt install graphviz (Debian/Ubuntu)",
        "dnf install graphviz (Fedora/RHEL)",
    ),
}


def graphviz_install_hint() -> str:
    """How to install Graphviz, phrased for the platform this is running on."""
    return ", or ".join(_INSTALL_HINTS.get(os.name, _INSTALL_HINTS["posix"]))


def find_dot() -> str | None:
    """The Graphviz ``dot`` executable, or ``None`` if it is not installed.

    Three places, most explicit first:

    1. :data:`DOT_ENV_VAR`, taken as given. A value that is not an executable
       file is *not* silently ignored — it is returned anyway, so that the
       subprocess failure names the path the user set rather than pretending
       they never set it.
    2. ``PATH``, via :func:`shutil.which`, which resolves ``dot.exe`` on Windows
       and ``dot`` elsewhere.
    3. :data:`_WELL_KNOWN_DIRS`, because "installed but not on ``PATH``" is the
       normal state of Graphviz on Windows and a common one on macOS. The result
       is an absolute path, so nothing about the caller's ``PATH`` matters
       afterwards.

    The answer is not cached: a user who installs Graphviz and re-runs
    ``netviz watch`` in the same session should get a diagram, not the
    conclusion the first render reached.
    """
    override = os.environ.get(DOT_ENV_VAR, "").strip()
    if override:
        return override

    found = shutil.which(DOT_EXECUTABLE)
    if found is not None:
        return found

    for directory in _WELL_KNOWN_DIRS.get(os.name, ()):
        # ``which`` with an explicit ``path`` still applies PATHEXT, so this
        # finds ``dot.exe`` under Program Files without naming the extension.
        candidate = shutil.which(DOT_EXECUTABLE, path=directory)
        if candidate is not None:
            return candidate
    return None


def missing_dot_message(*, subject: str) -> str:
    """What to tell a user who asked for ``subject`` without Graphviz installed.

    Kept next to :func:`find_dot` so the search and the explanation of what it
    searched cannot drift: the message names the environment variable *because*
    the lookup honours it, and says ``dot`` is looked for on ``PATH`` *because*
    that is the second place it looks.
    """
    return (
        f"cannot render {subject}: the Graphviz {DOT_EXECUTABLE!r} executable was not found "
        f"on PATH, nor in the usual install locations for this platform. "
        f"Install Graphviz ({graphviz_install_hint()}), set {DOT_ENV_VAR} to the full path of "
        f"the binary if it is installed somewhere unusual, or render with '--format dot' and "
        f"convert the file separately."
    )


#: How long a single layout may take before it is abandoned. Graphviz is
#: superlinear in the number of edges, so a large inventory rendered without a
#: filter can otherwise hang a terminal indefinitely.
_DOT_TIMEOUT_SECONDS: Final = 120

_TEMPLATE_NAME: Final = "graph.dot.j2"

#: Outline and text colour of something a :class:`~netviz.render.highlight.Highlight`
#: emphasises. Crimson is the one accent no element kind and no medium already
#: uses, so "on the traced path" cannot be misread as "fibre" or "cleartext
#: tunnel"; the kind's own fill is kept, so a highlighted switch is still
#: visibly a switch.
_HIGHLIGHT_STROKE: Final = "#b91c1c"
#: Pen width of a highlighted node outline or link. Wide enough to find on a
#: page of forty devices without swamping the ``penwidth`` steps that encode
#: link rate — a highlighted 1G link and a highlighted 100G link are both simply
#: "on the path".
_HIGHLIGHT_PENWIDTH: Final = "3.0"

#: Fill, outline and text of everything a highlight leaves out. Light enough to
#: read as background at a glance, dark enough that the labels are still legible
#: when a reader goes looking for where the path *could* have gone.
_DIM_FILL: Final = "#fafafa"
_DIM_STROKE: Final = "#d4d4d8"
_DIM_TEXT: Final = "#a1a1aa"
#: Pen width of a dimmed link — the thinnest step, below every rate threshold,
#: so the rate encoding never makes an off-path link louder than an on-path one.
_DIM_PENWIDTH: Final = "0.7"

#: How a :class:`~netviz.render.diffview.DiffOverlay` paints each mark:
#: outline, pen width, fill, text. Green for what arrives, red for what leaves,
#: amber for what moved — the three colours a reader of a code review already
#: knows — and the dimmed palette for everything untouched, shared with the
#: highlight above so a diff and a trace fade the background the same way.
#:
#: Hue is not the only carrier: a removed node and a removed link are also
#: **dashed** (see :func:`_diff_style`), so the picture survives a greyscale
#: print and a red-green reader.
_DIFF_ADDED_STROKE: Final = "#15803d"
_DIFF_ADDED_FILL: Final = "#dcfce7"
_DIFF_REMOVED_STROKE: Final = "#dc2626"
_DIFF_REMOVED_FILL: Final = "#fee2e2"
_DIFF_CHANGED_STROKE: Final = "#b45309"
_DIFF_CHANGED_FILL: Final = "#fef3c7"
#: Pen width of anything the diff marks. One step below the highlight's, because
#: a diff marks many things at once and a page of 3.0pt outlines is a page with
#: no emphasis in it.
_DIFF_PENWIDTH: Final = "2.5"
#: What a badge is prefixed with, per mark: the sigils ``netviz plan`` already
#: prints, so the diagram and the changeset read the same way.
_DIFF_SIGILS: Final[dict[str, str]] = {"added": "+", "removed": "-", "changed": "~"}

#: Pen width per link rate, widest threshold first. A reader should be able to
#: rank two links by rate without reading either label, which needs the steps to
#: be visible at a glance but bounded — a 100G link drawn to scale against a
#: 100M one would be a black bar.
_SPEED_PENWIDTH: Final[tuple[tuple[int, float], ...]] = (
    (100_000_000_000, 4.0),
    (25_000_000_000, 3.0),
    (10_000_000_000, 2.5),
    (2_500_000_000, 2.0),
    (1_000_000_000, 1.5),
)

#: Width of a link slower than every threshold above; also Graphviz's default,
#: but stated explicitly so a sub-gigabit link is visibly the thinnest step
#: rather than merely unannotated.
_MIN_PENWIDTH: Final = 1.0

#: Interfaces spelled out under a node before the rest are counted off. A
#: 48-port switch listing every port would push the topology off the page, and
#: the ports that carry a cable are labelled on the edges anyway.
_MAX_PORT_ROWS: Final = 8

#: An empty rack unit, drawn rather than left blank so the reader can count the
#: free space without measuring the gap between two boxes.
_EMPTY_UNIT: Final = "·"

#: Width and height, in points, of the cell an icon is drawn in. Every icon is
#: scaled into the same box and keeps its aspect ratio, so a diagram cannot end
#: up with a switch twice the size of the router next to it — and so the size an
#: icon is drawn at is netviz's decision rather than the theme author's.
_ICON_BOX: Final[tuple[int, int]] = (58, 44)

#: Media type per icon extension, for embedding one in SVG output.
_ICON_MEDIA_TYPES: Final[Mapping[str, str]] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}

#: How Graphviz reports that it could not read an icon. Both are warnings on a
#: zero exit status — it lays the graph out regardless, leaving a hole where the
#: picture should be — so they have to be looked for rather than waited for.
_IMAGE_TROUBLE: Final = re.compile(r"No loadimage plugin|No or improper image", re.IGNORECASE)

#: One element of an SVG rendering that carries a tooltip: the ``<title>`` the
#: group was given, and the anchor holding the tooltip Graphviz was asked for.
#: See :func:`_promote_tooltips` for why the two are swapped.
_TOOLTIP_ANCHOR: Final = re.compile(
    rb"<title>[^<]*</title>(\s*<g id=\"a_[^\"]*\"><a\b[^>]*\sxlink:title=\"([^\"]*)\")"
)


# --------------------------------------------------------------------------- #
# The view model the template consumes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Row:
    """One line of a node's record label.

    Either a three-column interface row — port, addresses, VLANs — or, when
    :attr:`spans` is set, a single note running the width of the table.
    """

    port: str
    addresses: str = ""
    vlans: str = ""
    spans: bool = False


@dataclass(frozen=True, slots=True)
class _NodeView:
    id: str
    title: str
    subtitle: str
    shape: str
    fill: str
    stroke: str
    style: str | None = None
    tooltip: str | None = None
    rows: tuple[_Row, ...] = ()
    #: Icon file name, resolved against the document's ``imagepath``. ``None``
    #: when no theme is in use or the theme has no picture for this kind.
    image: str | None = None
    #: ``id`` attribute to emit, or ``None`` to leave the node unidentified.
    #: See :mod:`netviz.render.ids`.
    element_id: str | None = None
    #: ``URL`` attribute to emit; see :mod:`netviz.render.links`.
    url: str | None = None
    #: Outline width, and the colour of every label the node's HTML table does
    #: not colour itself. ``None`` unless a highlight, a diff or a resolved
    #: style (§22) asks for one, so a rendering with none of the three is
    #: byte-identical to what it always was.
    penwidth: str | None = None
    fontcolor: str | None = None
    #: Label size in points; set only by a resolved style.
    fontsize: str | None = None
    #: ``pos`` attribute: the stored centre of the node, in points, with a
    #: trailing ``!`` when the node is pinned inside an otherwise free layout.
    #: ``None`` when the arrangement does not place this node.
    pos: str | None = None


@dataclass(frozen=True, slots=True)
class _EdgeView:
    source: str
    target: str
    color: str
    style: str
    tooltip: str | None = None
    penwidth: str | None = None
    label: str | None = None
    #: ``id`` attribute to emit; see :mod:`netviz.render.ids`.
    element_id: str | None = None
    #: ``URL`` attribute to emit; see :mod:`netviz.render.links`.
    url: str | None = None
    #: Label colour; set under a highlight or by a resolved style, for the
    #: same reason as :attr:`_NodeView.fontcolor`.
    fontcolor: str | None = None
    #: Label size in points; set only by a resolved style.
    fontsize: str | None = None
    #: Graphviz layout weight. Set only on a bundled edge, where it is the
    #: number of links folded in: four cables between two switches should pull
    #: them together four times as hard as one, which is what keeps a LAG short
    #: and straight instead of routed around the diagram.
    weight: str | None = None
    #: ``pos`` attribute: the route's Bézier control points. Only emitted for a
    #: fully-fixed drawing, where the no-op engine draws them as given; a
    #: partially-pinned one is re-routed around the nodes it just placed, and
    #: stale bends would cross them. Computed by :mod:`netviz.layout.routing`
    #: from the node positions, the stored bends and the link's routing style —
    #: which is what makes a *per-link* style expressible at all, Graphviz
    #: having a graph-wide ``splines`` and nothing per edge.
    pos: str | None = None
    #: ``lp`` attribute: where a nudged label is pinned. An output attribute
    #: everywhere except the no-op engine, which reads it back in.
    lp: str | None = None
    #: ``xlabel`` attribute, carrying the label when :attr:`label` cannot.
    #: Graphviz refuses to place a real edge label on an orthogonal route it
    #: laid out itself; a floating one it will place.
    xlabel: str | None = None
    #: ``arrowhead`` and ``constraint``. Set only on a note's leader line (§21),
    #: which is not a link: it must not draw a head, and it must not be allowed
    #: to pull the ranking about, or a callout would reshape the topology it is
    #: commenting on.
    arrowhead: str | None = None
    constraint: str | None = None


@dataclass(frozen=True, slots=True)
class _FrameView:
    """One namespace box or annotation area drawn from stored geometry.

    Two halves because Graphviz will draw only one of them for us: the rectangle
    goes into the graph's ``_background``, and the caption is a node — see
    :func:`_background` for why the obvious "put the text in the background too"
    is not available.
    """

    #: Node id of the caption. Colon-prefixed, so it cannot collide with an
    #: element's fully-qualified name.
    id: str
    label: str
    #: The rectangle, as one xdot polygon operation.
    outline: str
    #: ``pos`` of the caption node.
    caption: str
    #: Colour of the caption text.
    color: str = CLUSTER_LABEL_COLOUR
    #: The xdot operations that set the pen before :attr:`outline` is drawn: the
    #: line style, the pen colour and — for an area, which is a *filled* zone
    #: rather than a frame — the fill. Held as text rather than as colours
    #: because what may safely go into a ``_background`` is a very short list;
    #: see :func:`_background`.
    pen: str = ""


@dataclass(frozen=True, slots=True)
class _GroupView:
    """A ``cluster_*`` subgraph, or — with :attr:`id` unset — loose nodes."""

    nodes: tuple[_NodeView, ...] = field(default_factory=tuple)
    id: str | None = None
    label: str | None = None
    tooltip: str | None = None
    #: ``id`` attribute to emit on the cluster. Distinct from :attr:`id`, which
    #: is the subgraph's *name* in the DOT source and must start with
    #: ``cluster`` for Graphviz to box it at all.
    element_id: str | None = None


# --------------------------------------------------------------------------- #
# Stored geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LayoutPlan:
    """How a graph with stored geometry has to be laid out.

    ``dot`` ignores ``pos`` entirely, so an arrangement can only be honoured by
    an engine that reads it: ``neato``, either pinning part of the drawing or —
    with ``-n2`` — placing nothing at all and simply drawing what it was given.
    Which of the three applies is decided once, here, and both the DOT emission
    and the Graphviz invocation read the answer off it, so the document and the
    command line cannot disagree about what is being asked for.
    """

    mode: LayoutMode = LayoutMode.AUTO
    geometry: Geometry = field(default_factory=Geometry)
    #: The engine to run, overriding what :attr:`mode` implies. Only
    #: ``netviz layout --engine`` sets it: seeding an arrangement is the one
    #: time somebody chooses ``circo`` or ``fdp`` for a graph that has no
    #: geometry yet, and the choice is theirs rather than this module's.
    engine: str = ""

    @property
    def layout_engine(self) -> str:
        """The Graphviz layout engine, for ``-K``."""
        if self.engine:
            return self.engine
        return DOT_EXECUTABLE if self.mode is LayoutMode.AUTO else NOOP_ENGINE

    @property
    def noop(self) -> bool:
        """Is the engine to place nothing at all and draw what it is given?"""
        return self.mode is LayoutMode.FIXED and not self.engine

    def argv(self, executable: str, *, format: str) -> list[str]:
        """The Graphviz command line this plan means.

        ``-K`` is only added when a non-default engine is wanted, so an
        inventory with no arrangement produces exactly the command line it
        always did.
        """
        argv = [executable]
        if self.layout_engine != DOT_EXECUTABLE:
            argv.append(f"-K{self.layout_engine}")
        if self.noop:
            argv.append("-n2")
        argv.append(f"-T{format}")
        return argv


def layout_plan(graph: Graph) -> LayoutPlan:
    """What the arrangement stored for ``graph``, if any, asks the renderer to do."""
    geometry = graph.geometry
    mode = geometry.mode(graph.nodes)
    return LayoutPlan(mode=mode, geometry=geometry)


def cluster_keys(graph: Graph, options: RenderOptions) -> dict[str, str]:
    """Subgraph name to the thing it boxes, for the groups this render draws.

    The DOT subgraph names are positional (``cluster_0``) because a DOT id may
    hold neither the ``/`` of a namespace nor a ``-``; this is the one table
    that maps them back, and both the ``_background`` boxes and
    ``netviz layout --write`` read it, so a box cannot be stored under one key
    and drawn under another.
    """
    if graph.clusters:
        return {f"cluster_vrf_{index}": name for index, name in enumerate(graph.clusters)}
    if not options.group_by_namespace:
        return {}
    return {
        f"cluster_{index}": namespace
        for index, namespace in enumerate(graph.namespaces)
        if namespace
    }


def _coordinate(value: float) -> str:
    """One coordinate, as short as it can be written without losing a hundredth."""
    text = f"{value:.{COORDINATE_PLACES}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _pos(x: float, y: float, *, pin: bool = False) -> str:
    """A Graphviz ``pos`` value, optionally pinned."""
    return f"{_coordinate(x)},{_coordinate(y)}{'!' if pin else ''}"


def _spline(points: Sequence[tuple[float, float]]) -> str:
    """An edge ``pos``: the control points, in order."""
    return " ".join(_pos(x, y) for x, y in points)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

#: How each routing style is spelled as a Graphviz ``splines`` graph attribute.
#: Only consulted when the engine is doing the routing — a drawing whose nodes
#: are all placed carries an explicit ``pos`` per link instead, which is the only
#: way to express a *per-link* style, since Graphviz has no per-edge equivalent.
_SPLINES_ATTRIBUTE: Final[dict[Routing, str]] = {
    Routing.SPLINE: "true",
    Routing.ORTHOGONAL: "ortho",
    Routing.STRAIGHT: "line",
}


def routing_advisories(graph: Graph, options: RenderOptions | None = None) -> tuple[str, ...]:
    """What this rendering could not honour about the routes it was given.

    Graphviz will happily accept a document that asks for something it cannot
    do — an orthogonal layout with edge labels draws the labels somewhere else
    and says so on stderr; a pinned bend in a drawing it is still laying out is
    simply overwritten — so the ways a route can be quietly lost are enumerated
    here instead, in the vocabulary of the thing that fixes each one.

    Advisory, never fatal: a diagram that is nearly right is worth drawing, and
    a warning that stops a render is a warning nobody leaves turned on.
    """
    opts = options or RenderOptions()
    plan = layout_plan(graph)
    geometry = graph.geometry
    said: list[str] = []

    pinned = [edge for edge in graph.edges if geometry.link(edge.id).waypoints]
    if pinned and plan.mode is not LayoutMode.FIXED:
        loose = sorted(fqn for fqn in graph.nodes if fqn not in geometry.nodes)
        said.append(
            f"{count_text(len(pinned), 'link')} in this drawing "
            f"{'has' if len(pinned) == 1 else 'have'} bends pinned, but "
            f"{count_text(len(loose), 'node')} {'has' if len(loose) == 1 else 'have'} "
            f"no stored position ({_names(loose)}), so Graphviz has to route the whole "
            "diagram and the bends are lost. Run 'netviz layout --write' to place the rest"
        )

    if plan.mode is LayoutMode.FIXED:
        anchors = anchors_of(graph)
        unmeasured = sorted(
            {
                node
                for edge in graph.edges
                if not geometry.link(edge.id).is_empty
                for node in (edge.source, edge.target)
                if (anchor := anchors.get(node)) is not None and not anchor.measured
            }
        )
        if unmeasured:
            said.append(
                f"{count_text(len(unmeasured), 'node')} anchoring a routed link "
                f"{'records' if len(unmeasured) == 1 else 'record'} no size "
                f"({_names(unmeasured)}), so the route is clipped against a default box "
                "and may stop short of the shape. Re-run 'netviz layout --write' to "
                "record the sizes"
            )
        said.extend(_detour_advisories(graph, opts))
        return tuple(said)

    default = default_routing(graph, opts)
    per_link = sorted(
        {
            str(style)
            for edge in graph.edges
            if (style := geometry.link(edge.id).routing) is not None and style is not default
        }
    )
    if per_link:
        said.append(
            "this drawing is laid out by Graphviz, which has one routing style for the "
            f"whole graph, so the links asking for {', '.join(per_link)} are drawn "
            f"{default} like everything else. Run 'netviz layout --write' to pin the "
            "arrangement, after which every link is routed as it asks"
        )
    if default is Routing.ORTHOGONAL and _has_edge_labels(graph, opts):
        said.append(
            "Graphviz cannot place an edge label on an orthogonal route, so the labels are "
            "emitted as 'xlabel' and floated near their links instead. Pin the arrangement "
            "with 'netviz layout --write' to place them exactly"
        )
    return tuple(said)


def _detour_advisories(graph: Graph, options: RenderOptions) -> Iterator[str]:
    """Links obstacle avoidance gave up on, grouped by the reason it gave up.

    The cut-offs in :mod:`netviz.layout.avoid` exist so that a large, dense
    diagram does not spend a minute routing itself, and a cut-off that is not
    reported is indistinguishable from a router that does not work: the reader
    sees a line through a box either way. So each one comes out here, in the
    vocabulary of the knob that lifts it, and grouped — a drawing that hit the
    window bound will normally have hit it a hundred times, and a hundred lines
    saying the same thing is a wall nobody reads.
    """
    if not options.avoid:
        return
    plan = route_plan(graph, options)
    if not plan.detours:
        return
    grouped: dict[str, list[str]] = {}
    for detour in plan.detours:
        grouped.setdefault(detour.reason, []).append(detour.link)
    fixes = {
        "window": (
            "run across too crowded a part of the diagram to search; they are drawn "
            "without avoiding obstacles. Spread the devices out, draw less of the "
            "network with '--name' or '--namespace', or accept it with '--no-avoid'"
        ),
        "budget": (
            "took more steps to route than the search is allowed; they are drawn "
            "without avoiding obstacles. Usually a device boxed in by its neighbours: "
            "move one, or drop a bend of your own to say which way the cable goes"
        ),
        "unreachable": (
            "have no clear orthogonal route at all, so they are drawn straight through. "
            "Two shapes drawn on top of each other, or a corridor narrower than the "
            "clearance: re-run 'netviz layout --write' or move one of them"
        ),
    }
    for reason, links in grouped.items():
        yield (f"{count_text(len(links), 'link')} ({_names(sorted(set(links)))}) {fixes[reason]}")


def _has_edge_labels(graph: Graph, options: RenderOptions) -> bool:
    """Would this rendering annotate any link? Decided the way the render does."""
    return any(_edge_label(edge, graph.layer, options) for edge in graph.edges)


def _names(items: Sequence[str], *, most: int = 3) -> str:
    """A handful of addresses for a message, with the tail counted rather than listed."""
    if len(items) <= most:
        return ", ".join(items)
    return f"{', '.join(items[:most])} and {len(items) - most} more"


def _frames(graph: Graph, options: RenderOptions, plan: LayoutPlan) -> tuple[_FrameView, ...]:
    """The namespace boxes this drawing has stored geometry for.

    ``neato`` does not draw clusters — only ``dot`` and ``fdp`` do — so a fixed
    arrangement would silently lose every namespace frame. Since the arrangement
    already stores where each frame goes, drawing it ourselves is both possible
    and more faithful than letting an engine guess: the box is where the user
    put it rather than wherever the layout happened to land.

    Only the groups this render actually boxes are drawn, read off
    :func:`cluster_keys` rather than off the namespaces — a diagram rendered
    without ``--group-by-namespace`` has no frames, and stored boxes for frames
    nobody asked for must not appear.

    Empty when no group has stored geometry, which leaves the document
    byte-identical to one without the feature.
    """
    return tuple(
        _FrameView(
            id=f"{_FRAME_ID_PREFIX}{key}",
            label=key,
            outline=_dot_points(plan.geometry.groups[key]),
            caption=_caption_pos(plan.geometry.groups[key]),
            pen=f"c {_xdot_text(CLUSTER_STROKE)}",
        )
        for key in cluster_keys(graph, options).values()
        if key in plan.geometry.groups
    )


def _background(frames: Sequence[_FrameView]) -> str | None:
    """The frames as xdot draw operations for the ``_background`` attribute.

    Graphviz grows the canvas to fit a ``_background``, so a box drawn wider
    than its contents is not clipped.

    **Rectangles only, and no text.** A ``T`` operation in a ``_background``
    segfaults Graphviz 2.43 — the version Debian and Ubuntu ship — depending on
    whether anything else in the document has established a font, which makes it
    a landmine rather than a limitation. The caption is drawn as an ordinary
    node instead (:class:`_FrameView`), which every engine handles and which
    also puts the caption inside the drawing's bounding box for free. That is
    true of an annotation area's label as well as of a namespace's
    (``docs/follow-ups.md`` §17), and ``tests/test_render_annotations.py`` pins
    it down: the operations here are the polygon, the pen and the fill, and
    never text.

    Each frame carries its own pen operations, so a dashed area cannot leak its
    line style into the frame drawn after it.
    """
    if not frames:
        return None
    return " ".join(f"{frame.pen} {frame.outline}" for frame in frames)


def _dot_points(box: Box, *, operation: str = "p") -> str:
    """One rectangle as an xdot polygon operation.

    ``p`` is an outline and ``P`` a filled polygon — the two operations a
    ``_background`` is allowed to hold. Graphviz draws a filled one with the
    current pen colour as its edge, so an area needs no second outline pass.
    """
    left, bottom, right, top = box.bounds
    corners = " ".join(
        f"{_coordinate(x)} {_coordinate(y)}"
        for x, y in ((left, bottom), (left, top), (right, top), (right, bottom))
    )
    return f"{operation} 4 {corners}"


def _caption_pos(box: Box) -> str:
    """Where a frame's caption sits: centred, just *above* the top edge.

    Two decisions, both forced by what the caption is — an ordinary node.

    *Centred* rather than left-aligned the way ``labeljust=l`` puts a cluster's
    own label, because netviz does not measure text: an approximate left edge
    would look like a mistake, while a centred caption is exactly where it says
    it is.

    *Above* rather than inside, because a node placed inside the frame touches
    whatever the arrangement put near the top of it, and ``neato`` responds to
    two touching nodes by abandoning spline routing for the **whole graph**.
    A caption that cost every edge its curve would be a poor trade for eleven
    points of vertical space, and a title above a box reads as its title
    anyway. Graphviz grows the canvas to fit both the caption and the
    ``_background``, so nothing is clipped.
    """
    _, _, _, top = box.bounds
    return _pos(box.x, top + CLUSTER_FONT_SIZE)


def _xdot_text(value: str) -> str:
    """An xdot counted string: the byte length, a dash, then the bytes."""
    return f"{len(value.encode('utf-8'))} -{value}"


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def to_dot(
    graph: Graph,
    options: RenderOptions | None = None,
    *,
    target: str = "dot",
    route_edges: bool = True,
) -> str:
    """Render ``graph`` as Graphviz DOT source.

    The graph is undirected (a cable has no direction, §7.1), so the output is a
    ``graph``, not a ``digraph``, and edges use ``--``.

    A graph carrying a stored arrangement (§18) emits it: every placed node gets
    a ``pos``, every placed link its spline control points, and — when the whole
    drawing is placed — the namespace boxes are drawn as a ``_background``,
    because the no-op layout engine that reproduces the arrangement does not
    draw clusters. See :func:`layout_plan` for how the document is then run.

    The annotations of §21 are drawn too, unless
    :attr:`RenderOptions.annotations <netviz.render.options.RenderOptions.annotations>`
    is off; see the *Annotations* section of the module docstring for the
    vocabulary each of the three is drawn in and why it differs between an
    automatic and a fixed layout.

    Args:
        target: The output format this DOT is destined for. Only icon
            selection depends on it — see :func:`netviz.render.icons.suffix_order`
            — and the default suits DOT written out for someone else to lay out.
        route_edges: Whether the engine should be asked to *draw* the edges as
            well as place the nodes. Off for the two documents netviz lays out
            only to read coordinates back off — :func:`complete_layout` and
            :func:`measure_nodes`, which use the node positions and sizes and
            discard everything else.

            Not a cosmetic saving. ``neato``'s spline router, on nodes it did
            not choose the positions of, is superlinear: the probe run for a
            thousand-node graph with fifty nodes pinned took 52 seconds with
            routing on and half a second with it off, for the same positions and
            therefore the same final drawing. Nothing a reader sees depends on
            it, because the drawing that is *shown* is the second run, and that
            one routes everything.
    """
    opts = options or RenderOptions()
    look = _look(graph, opts, target=target)
    # The ids are computed whether or not they are *emitted*: they are how a
    # detail record is keyed, so the tooltips need them even when the document
    # itself stays anonymous. A rendering that wants neither pays for neither.
    identity = element_ids(graph) if opts.tooltips or opts.element_ids else ElementIds()
    details = build_details(graph, opts, ids=identity) if opts.tooltips else {}
    plan = layout_plan(graph)
    fixed = plan.mode is LayoutMode.FIXED
    annotations = annotation_views(graph, opts)
    # An area is a cluster when the engine is laying the graph out and a
    # background rectangle when it is not, so exactly one of the two lists is
    # ever non-empty and a node is never boxed twice.
    areas = () if fixed else _area_groups(graph, annotations, opts, look, identity, details, plan)
    frames = (*_frames(graph, opts, plan), *_area_frames(annotations, plan)) if fixed else ()
    template = _environment().get_template(_TEMPLATE_NAME)
    return template.render(
        title=opts.title,
        rankdir=opts.rankdir or DEFAULT_RANKDIR,
        # What the *engine* routes with. A fixed drawing carries an explicit
        # ``pos`` per link and this is inert; anywhere else it is the only place
        # a routing style can be expressed at all, so the inventory's default
        # goes here and a link asking for something different is reported by
        # :func:`routing_advisories` rather than silently ignored.
        #
        # Unless this document is only being laid out to read positions off, in
        # which case routing its edges is work whose answer is thrown away; see
        # ``route_edges``.
        splines=(_SPLINES_ATTRIBUTE[default_routing(graph, opts)] if route_edges else "false"),
        areas=areas,
        groups=_groups(graph, opts, look, identity, details, plan, skip=frozenset(_boxed(areas))),
        notes=_note_views(annotations, opts, plan),
        legends=_legend_views(graph, annotations, opts, plan),
        edges=(
            *_edge_views(graph, opts, look, identity, details, plan),
            *_leader_views(annotations),
        ),
        imagepath=(str(opts.icons.directory) if look.icons and opts.icons is not None else None),
        icon_width=_ICON_BOX[0],
        icon_height=_ICON_BOX[1],
        # ``inputscale`` tells neato that a pinned ``pos`` is in points rather
        # than inches. It is inert under ``-n2`` and inert under ``dot``, so it
        # is emitted whenever anything is placed: it costs nothing, and it makes
        # ``-f dot`` output that somebody lays out themselves honour the pins.
        inputscale=POINTS_PER_INCH if plan.mode is not LayoutMode.AUTO else None,
        frames=frames,
        background=_background(frames),
        frame_font_size=CLUSTER_FONT_SIZE,
    )


@dataclass(frozen=True, slots=True)
class _Look:
    """What a rendering draws with: the resolved styles and the pictures.

    One value threaded through the view builders instead of two, because the
    two are asked about together every time — a node's icon is a *field* of
    its resolved style (§22), so a builder holding one and not the other could
    not draw a node at all.
    """

    styles: StyleMap
    #: Icon *name* -> file name inside the theme directory. Keyed by name
    #: rather than by kind: ``spec.style.icon`` lets one element borrow
    #: another glyph, so the kind is only the default name.
    icons: Mapping[str, str] = field(default_factory=lambda: _NO_ICONS)

    def image(self, style: ResolvedStyle) -> str | None:
        """The file to draw this node as, or ``None`` for a plain shape."""
        return self.icons.get(style.icon) if style.icon else None


def _look(graph: Graph, options: RenderOptions, *, target: str) -> _Look:
    """Resolve every style in ``graph``, and find the pictures they ask for."""
    styles = StyleMap.build(
        graph,
        theme=options.theme,
        icons=options.icons,
        output=target,
        styling=options.styling,
    )
    return _Look(styles=styles, icons=_icon_files(styles, options.icons, target=target))


def _icon_files(styles: StyleMap, theme: IconTheme | None, *, target: str) -> Mapping[str, str]:
    """The icon file name behind each icon *name* this drawing asks for.

    Only the names actually wanted are resolved, so a theme is never asked for
    a picture the diagram has no use for, and a diagram of nothing but computers
    does not carry an ``imagepath`` for icons it never draws.
    """
    if theme is None:
        return {}
    return theme.files(styles.kinds_with_icons(), prefer=suffix_order(target))


def run_graphviz(
    source: str, *, format: str, plan: LayoutPlan | None = None, subject: str | None = None
) -> tuple[bytes, str]:
    """Run Graphviz over ``source`` and return ``(stdout, stderr)``.

    The single place a subprocess is started, so that every caller — the image
    renderers, and ``netviz layout`` reading coordinates back out — reports a
    missing binary, a timeout and a non-zero exit the same way.

    Args:
        source: The DOT document, handed over stdin.
        format: The ``-T`` value. Any Graphviz output format, not only the
            image ones: ``netviz layout`` asks for ``json``.
        plan: Which engine to run and whether to run it in no-op mode. ``None``
            means the plain hierarchical layout, as before.
        subject: What to call the output in a diagnostic; defaults to ``format``.

    Raises:
        RenderError: Graphviz is not installed, would not start, timed out,
            exited non-zero, or produced nothing.
    """
    what = subject or format
    executable = find_dot()
    if executable is None:
        raise RenderError(missing_dot_message(subject=what))
    argv = (plan or LayoutPlan()).argv(executable, format=format)
    try:
        # Fixed argv, no shell, and the DOT source goes over stdin: nothing from
        # an inventory ever becomes a command-line argument or a shell word.
        completed = subprocess.run(
            argv,
            input=source.encode("utf-8"),
            capture_output=True,
            timeout=_DOT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            f"Graphviz did not finish laying out the diagram within {_DOT_TIMEOUT_SECONDS}s. "
            "Narrow the graph with --namespace, --vlan or --neighbors-of, "
            "or render with '--format dot' and lay it out separately."
        ) from exc
    except OSError as exc:
        # Reachable in two ways worth telling apart. ``NETVIZ_DOT`` naming a
        # path that is not there is a typo the user can fix, and the message says
        # where to fix it. Anything else -- a binary that will not exec, a
        # permission problem, an exhausted process table -- is reported as
        # itself; guessing "install Graphviz" for those would send the reader
        # after the wrong thing.
        detail = f"could not run {executable!r}: {exc.strerror or exc}"
        if isinstance(exc, FileNotFoundError) and os.environ.get(DOT_ENV_VAR, "").strip():
            detail += f" ({DOT_ENV_VAR} names it; unset it to search PATH instead)"
        raise RenderError(f"cannot render {what}: {detail}") from exc

    stderr = _decode(completed.stderr)
    if completed.returncode != 0:
        detail = stderr or f"{DOT_EXECUTABLE} exited with {completed.returncode}"
        raise RenderError(f"Graphviz failed to render {what}: {detail}")
    if not completed.stdout:
        detail = stderr or "no diagnostic was reported"
        raise RenderError(f"Graphviz produced no {what} output: {detail}")
    return completed.stdout, stderr


def to_image(graph: Graph, options: RenderOptions | None = None, *, format: str) -> bytes:
    """Lay ``graph`` out by running Graphviz, and return the encoded image.

    Three shapes of run, decided by :func:`layout_plan` from the arrangement the
    inventory stores (§18):

    * nothing stored — the hierarchical layout, exactly as before;
    * everything stored — ``neato -n2``, which places nothing and draws what it
      is given, so the output reproduces the arrangement point for point;
    * some of it stored — two runs. The first pins what is placed and lets
      ``neato`` position the rest; its answer is brought back onto the stored
      coordinate system (:func:`~netviz.layout.graphviz.realign`) and the
      second run draws the completed arrangement in no-op mode. Two runs rather
      than one because a single pinned run returns the *whole* drawing scaled
      and translated onto Graphviz's canvas — which would move the nodes the
      user placed by hand, and moving those is the one thing an arrangement
      exists to prevent.

    Args:
        format: One of :data:`IMAGE_FORMATS`.

    Raises:
        RenderError: ``format`` is not an image format, the Graphviz ``dot``
            executable is not installed, or the layout failed or timed out.
    """
    if format not in IMAGE_FORMATS:
        supported = ", ".join(IMAGE_FORMATS)
        raise RenderError(f"{format!r} is not a Graphviz image format; expected one of {supported}")

    opts = options or RenderOptions()
    if layout_plan(graph).mode is LayoutMode.PARTIAL:
        graph = complete_layout(graph, opts, target=format)
    graph = measure_nodes(graph, opts, target=format)
    plan = layout_plan(graph)
    source = to_dot(graph, opts, target=format)
    theme = opts.icons
    icons = _look(graph, opts, target=format).icons
    payload, stderr = run_graphviz(source, format=format, plan=plan)
    if icons:
        # An unreadable icon is the one warning worth escalating: Graphviz
        # succeeds and simply leaves the picture out, so the user would get a
        # diagram of empty labels and no explanation.
        _check_icons_loaded(stderr, format=format)
        if format == "svg" and theme is not None:
            payload = _embed_icons(payload, theme, icons)
    if format == "svg" and opts.tooltips:
        payload = _promote_tooltips(payload)
    # dot reports non-fatal layout warnings on stderr with a zero exit status;
    # those are not this renderer's to escalate.
    return payload


def complete_layout(graph: Graph, options: RenderOptions, *, target: str = "svg") -> Graph:
    """A partially-arranged graph with every remaining node placed.

    Runs the pinned layout, reads the coordinates back and expresses them on the
    stored coordinate system, so the result is a graph whose arrangement is
    :attr:`~netviz.layout.geometry.LayoutMode.FIXED` and whose stored nodes
    are still exactly where they were stored. ``netviz layout --write`` and
    :func:`to_image` both go through it, which is what makes what is written the
    same as what is drawn.

    Raises:
        RenderError: Graphviz failed, or produced JSON that cannot be read.
    """
    plan = layout_plan(graph)
    if plan.mode is not LayoutMode.PARTIAL:
        return graph
    payload, _ = run_graphviz(
        # Positions only: the edges this run would draw are discarded, and
        # asking for them is most of what a partial layout used to cost.
        to_dot(graph, options, target=target, route_edges=False),
        format="json",
        plan=plan,
        subject="the diagram layout",
    )
    try:
        drawing = parse_drawing(payload)
    except DrawingError as exc:
        raise RenderError(f"cannot read the layout Graphviz computed: {exc}") from exc
    placed = realign(drawing, plan.geometry.nodes, plan.geometry.nodes)
    geometry = replace(
        plan.geometry,
        nodes={fqn: placed[fqn] for fqn in graph.nodes if fqn in placed},
    )
    return replace(graph, geometry=geometry)


def measure_nodes(graph: Graph, options: RenderOptions, *, target: str = "svg") -> Graph:
    """A fixed arrangement with the box of every routed link's anchors filled in.

    A route computed by netviz has to stop at the shape it runs into, and
    nothing in a stored arrangement says how big a shape is — Graphviz derives
    it from the label, which netviz cannot measure. ``netviz layout --write``
    records the sizes of the nodes a routed link leaves from for exactly this
    reason, so the common case costs nothing; this is the fallback for an
    arrangement written before a link was routed, or by hand.

    One extra Graphviz run, and only when a route would otherwise be clipped
    against a guess. Node positions are pinned and edge routing does not feed
    back into them, so what comes out is the same drawing measured.

    Raises:
        RenderError: Graphviz failed, or produced JSON that cannot be read.
    """
    plan = layout_plan(graph)
    geometry = plan.geometry
    wanted = {
        node
        for edge in graph.edges
        if not geometry.link(edge.id).is_empty
        for node in (edge.source, edge.target)
        if (placement := geometry.nodes.get(node)) is not None and placement.width is None
    }
    if plan.mode is not LayoutMode.FIXED or not wanted:
        return graph
    payload, _ = run_graphviz(
        to_dot(graph, options, target=target, route_edges=False),
        format="json",
        plan=plan,
        subject="the node sizes",
    )
    try:
        drawing = parse_drawing(payload)
    except DrawingError as exc:
        raise RenderError(f"cannot read the layout Graphviz computed: {exc}") from exc
    nodes = {
        fqn: (
            replace(placement, width=measured.width, height=measured.height)
            if fqn in wanted and (measured := drawing.nodes.get(fqn)) is not None
            else placement
        )
        for fqn, placement in geometry.nodes.items()
    }
    return replace(graph, geometry=replace(geometry, nodes=nodes))


def _check_icons_loaded(stderr: str, *, format: str) -> None:
    """Fail when Graphviz laid the graph out but could not read the icons.

    Raises:
        RenderError: An icon was not drawn.
    """
    if not _IMAGE_TROUBLE.search(stderr):
        return
    # Graphviz repeats its complaint once per node that could not be drawn, so
    # a 50-device diagram would otherwise report the same sentence 50 times.
    complaints = dict.fromkeys(line.strip() for line in stderr.splitlines() if line.strip())
    raise RenderError(
        f"Graphviz could not read the icons while rendering {format}: "
        f"{clip_text('; '.join(complaints))}. If the icons are SVG, this build of Graphviz "
        "has no SVG loader for that output (it needs librsvg): render '-f svg', which needs "
        "none, or point --icons at a directory of PNG icons, which every build can read."
    )


def _embed_icons(svg: bytes, theme: IconTheme, icons: Mapping[str, str]) -> bytes:
    """Inline the icon files an SVG rendering references, as ``data:`` URIs.

    Graphviz writes the ``SRC`` it was given straight into the output, so an SVG
    with icons would otherwise be a file that only draws correctly next to the
    theme directory — no use in a README, an email or the ``watch`` preview.
    Only the names netviz itself emitted are substituted; anything else in the
    document is left exactly as Graphviz wrote it.
    """
    sources = {name: theme.directory / name for name in set(icons.values())}
    encoded = {
        name.encode("utf-8"): uri
        for name, path in sources.items()
        if (uri := _data_uri(path)) is not None
    }
    if not encoded:  # pragma: no cover - the files were read moments ago by dot
        return svg

    def substitute(match: re.Match[bytes]) -> bytes:
        replacement = encoded.get(match.group(1))
        return match.group(0) if replacement is None else b'xlink:href="' + replacement + b'"'

    return re.sub(rb'xlink:href="([^"]*)"', substitute, svg)


def _promote_tooltips(svg: bytes) -> bytes:
    """Move each tooltip from ``xlink:title`` into the ``<title>`` it belongs to.

    Graphviz writes a ``tooltip`` attribute as ``xlink:title`` on an anchor
    wrapping the shape, and fills the group's ``<title>`` with the internal name
    it laid the graph out under. That is the wrong way round for a reader:
    ``<title>`` is the construct browsers have popped up since SVG 1.1, while
    ``xlink:title`` is deprecated and widely ignored — so a diagram opened in a
    browser would show ``routers/rtr-home`` and never the detail this renderer
    went to the trouble of computing.

    The substitution is textual because the alternative is a full XML round
    trip, which would cost the standalone document its declaration, its doctype
    and its comments. It matches Graphviz's own output shape only, and an
    unmatched document is returned unchanged, so a future Graphviz that emits
    something else degrades to the old behaviour rather than to a broken file.
    The replacement text is copied out of an attribute value Graphviz has
    already escaped, and every escape valid there is valid in element content.
    """
    return _TOOLTIP_ANCHOR.sub(
        lambda match: b"<title>" + match.group(2) + b"</title>" + match.group(1), svg
    )


def _data_uri(path: Path) -> bytes | None:
    """``data:<type>;base64,...`` for ``path``, or ``None`` if it cannot be read."""
    media_type = _ICON_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:  # pragma: no cover - the suffix decided the file
        return None
    try:
        payload = path.read_bytes()
    except OSError:  # pragma: no cover - the file was read moments ago by dot
        return None
    return f"data:{media_type};base64,".encode() + base64.b64encode(payload)


#: Kept so callers written against the original names keep working; ``to_dot``
#: and ``to_image`` are the canonical spellings.
render_dot = to_dot
render_image = to_image


def _decode(stream: bytes | None) -> str:
    return (stream or b"").decode("utf-8", "replace").strip()


# --------------------------------------------------------------------------- #
# Template environment
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """The Jinja2 environment, built once and reused.

    ``watch`` mode re-renders on every save, so compiling the template per call
    would be pure overhead. ``StrictUndefined`` turns a typo in the template
    into an exception instead of a silently missing attribute.
    """
    environment = Environment(
        loader=PackageLoader("netviz.render", "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    environment.filters["dot_string"] = _dot_string
    return environment


def _dot_string(value: object) -> Markup:
    """Quote ``value`` as a DOT string, turning real newlines into ``\\n``.

    Backslashes are escaped *before* the newline is converted, so the escape
    this function introduces is never escaped a second time. The result is
    returned as :class:`~markupsafe.Markup` because it is already complete —
    quotes included — and must not be HTML-escaped on its way into the document.
    That marking is why the filter belongs only in DOT-quoted positions; see the
    module docstring.

    Unprintable characters are dropped rather than escaped
    (:func:`~netviz.render.details.printable`): DOT has no escape for them,
    Graphviz would copy one straight into an SVG ``<text>``, and the result
    would be a document no XML parser accepts. Doing it here rather than in each
    producer means every quoted position — id, label, tooltip, URL — is covered
    by construction.
    """
    escaped = (
        printable(str(value))
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return Markup(f'"{escaped}"')


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


def _groups(
    graph: Graph,
    options: RenderOptions,
    look: _Look,
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    plan: LayoutPlan,
    *,
    skip: Container[str] = frozenset(),
) -> tuple[_GroupView, ...]:
    """The node groups to draw: one per namespace, or a single loose group.

    A layer-3 subnet node reports the root namespace, so it stays outside every
    cluster — a prefix spanning two sites belongs to neither of them. The root
    namespace is never boxed either: drawing a frame labelled ``/`` around half
    the diagram helps nobody.

    A layer that groups its own nodes wins over ``--group-by-namespace``: the
    routing view boxes each VRF (§16.8), and that is the grouping the reader asked
    for by choosing the layer.

    Args:
        skip: Nodes some other container has already claimed — the members of an
            annotation area (§21), which is drawn as a cluster of its own. A node
            belongs to at most one box in a DOT document, so the two groupings
            cannot both hold it; see :func:`_area_groups` for the precedence.
    """
    if graph.clusters:
        return _cluster_groups(graph, options, look, identity, details, plan, skip=skip)

    if not options.group_by_namespace:
        loose = [node for node in graph.nodes.values() if node.fqn not in skip]
        nodes = _node_views(graph, loose, options, look, identity, details, plan)
        return (_GroupView(nodes=tuple(nodes)),)

    groups: list[_GroupView] = []
    for index, namespace in enumerate(graph.namespaces):
        members = [node for node in graph.nodes_in(namespace) if node.fqn not in skip]
        views = tuple(_node_views(graph, members, options, look, identity, details, plan))
        if not namespace:
            groups.append(_GroupView(nodes=views))
            continue
        if not views:
            # Every node of this namespace is inside an area instead, and an
            # empty cluster is a labelled box round nothing.
            continue
        groups.append(
            _GroupView(
                nodes=views,
                # The subgraph's name stays positional: DOT boxes a subgraph only
                # when its name begins with ``cluster``, and an unquoted DOT id
                # may hold neither the ``/`` of a namespace nor a ``-``. The
                # stable identity goes in the ``id`` attribute instead.
                id=f"cluster_{index}",
                label=namespace,
                tooltip=_cluster_tooltip(namespace, members, options, identity, details),
                element_id=identity.cluster(namespace) if options.element_ids else None,
            )
        )
    return tuple(groups)


def _cluster_groups(
    graph: Graph,
    options: RenderOptions,
    look: _Look,
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    plan: LayoutPlan,
    *,
    skip: Container[str] = frozenset(),
) -> tuple[_GroupView, ...]:
    """One box per cluster the *layer* asked for, unboxed nodes first.

    Unboxed nodes lead so that a router straddling two instances is laid out
    between their boxes rather than after them, which is where it belongs — and
    so that a diagram with no VRF at all is identical to an ungrouped one.
    """
    loose = tuple(
        _node_views(
            graph,
            [node for node in graph.nodes.values() if not node.cluster and node.fqn not in skip],
            options,
            look,
            identity,
            details,
            plan,
        )
    )
    groups: list[_GroupView] = [_GroupView(nodes=loose)] if loose else []
    noun = CLUSTER_NOUN.get(graph.layer, "group")
    for index, cluster in enumerate(graph.clusters):
        members = [node for node in graph.nodes_in_cluster(cluster) if node.fqn not in skip]
        if not members:
            continue
        groups.append(
            _GroupView(
                nodes=tuple(_node_views(graph, members, options, look, identity, details, plan)),
                # Offset past the namespace clusters' numbering space so the two
                # groupings can never mint the same subgraph name. The ``vrf``
                # in it is historical and does the job of being unique, not of
                # naming what the box holds — the *label* does that.
                id=f"cluster_vrf_{index}",
                label=f"{noun} {cluster}",
                tooltip=_cluster_tooltip(cluster, members, options, identity, details, kind=noun),
                element_id=identity.cluster(cluster) if options.element_ids else None,
            )
        )
    return tuple(groups)


def _cluster_tooltip(
    namespace: str,
    members: Iterable[Node],
    options: RenderOptions,
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    *,
    kind: str = "namespace",
) -> str | None:
    """What the box around a namespace holds, or ``None`` with tooltips off."""
    if not options.tooltips:
        return None
    records = [
        record
        for node in members
        if (element := identity.node(node.fqn)) is not None
        and (record := details.get(element)) is not None
    ]
    return namespace_text(namespace, records, kind=kind)


def _node_views(
    graph: Graph,
    nodes: Iterable[Node],
    options: RenderOptions,
    look: _Look,
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    plan: LayoutPlan,
) -> Iterator[_NodeView]:
    for node in nodes:
        placement = plan.geometry.nodes.get(node.fqn)
        # The resolved style is the *baseline*: the element's own block, the
        # theme's, the icon set's and finally the palette's (§22). A highlight
        # and a diff are overlays and are applied over the top of it, because
        # a deleted device drawn in the colour somebody chose for it would
        # make the diff unreadable.
        style = look.styles.node(node.fqn)
        fill, stroke = style.faded_fill or "", style.faded_stroke or ""
        fontcolor, penwidth = style.faded_font_color, style.penwidth
        emphasis = _node_emphasis(node, options.highlight)
        if emphasis is not None:
            fill, stroke = emphasis.fill or fill, emphasis.stroke
        # A diff is applied *after* a highlight and overrides it: the two are
        # not normally combined, and when they are, "this is being deleted" is
        # the louder fact.
        mark = Mark.UNCHANGED if options.diff is None else options.diff.node(node.fqn)
        if options.diff is not None:
            emphasis = _diff_emphasis(mark)
            fill, stroke = emphasis.fill or fill, emphasis.stroke
        if emphasis is not None:
            fontcolor, penwidth = emphasis.fontcolor, emphasis.penwidth
        image = look.image(style)
        element = identity.node(node.fqn)
        record = details.get(element) if element is not None else None
        yield _NodeView(
            id=node.fqn,
            title=_inline(node.name),
            subtitle=_subtitle(node),
            # An icon *is* the glyph, so the shape it would sit inside is taken
            # away rather than drawn around it. The palette stays on the view
            # because the icon carries the same colours: a theme without a
            # picture for this kind falls back to the shape, in one diagram.
            shape="none" if image else dot_shape(style.shape),
            fill=fill,
            stroke=stroke,
            style="" if image else _diff_style(_node_style(node, style), mark),
            # A tooltip is a DOT string, not HTML, so its line breaks are
            # meaningful and are kept.
            tooltip=detail_text(record) if record is not None else None,
            rows=_with_badge(
                _node_rows(node, options, layer=graph.layer),
                mark,
                None if options.diff is None else options.diff.badge(node.fqn),
            ),
            image=image,
            element_id=element if options.element_ids else None,
            url=_node_url(graph, node, options.link_template),
            penwidth=penwidth,
            fontcolor=fontcolor,
            fontsize=None if style.font_size is None else str(style.font_size),
            # A stored *size* is deliberately not emitted. Graphviz derives the
            # same box from the same label, so pinning it would buy nothing and
            # a rounded value would clip a label by a hundredth of a point. The
            # size is published in the JSON export instead, where a client that
            # draws the graph itself has no label metrics to derive it from.
            pos=(
                None
                if placement is None
                else _pos(placement.x, placement.y, pin=plan.mode is LayoutMode.PARTIAL)
            ),
        )


@dataclass(frozen=True, slots=True)
class _Emphasis:
    """How loudly to draw one node or link under a highlight."""

    stroke: str
    penwidth: str
    #: Replacement fill for a dimmed node; ``None`` keeps the kind's own colour,
    #: which is what an emphasised node wants — the reader should still be able
    #: to tell a router from a switch on the traced path.
    fill: str | None = None
    fontcolor: str | None = None


#: The two ways a highlight can draw something. Shared by nodes and links so the
#: emphasised palette cannot drift between them.
_EMPHASISED: Final = _Emphasis(stroke=_HIGHLIGHT_STROKE, penwidth=_HIGHLIGHT_PENWIDTH)
_DIMMED: Final = _Emphasis(
    stroke=_DIM_STROKE, penwidth=_DIM_PENWIDTH, fill=_DIM_FILL, fontcolor=_DIM_TEXT
)


def _node_emphasis(node: Node, highlight: Highlight | None) -> _Emphasis | None:
    """How to draw ``node``, or ``None`` when no highlight is in force."""
    if highlight is None:
        return None
    return _EMPHASISED if highlight.has_node(node.fqn) else _DIMMED


def _edge_emphasis(edge: Edge, highlight: Highlight | None) -> _Emphasis | None:
    """How to draw ``edge``, or ``None`` when no highlight is in force."""
    if highlight is None:
        return None
    return _EMPHASISED if highlight.has_edge(edge.id) else _DIMMED


#: How each :class:`~netviz.render.diffview.Mark` is drawn. An untouched thing
#: reuses the highlight's dimmed palette rather than a second grey of its own.
_DIFF_EMPHASIS: Final[dict[Mark, _Emphasis]] = {
    Mark.ADDED: _Emphasis(
        stroke=_DIFF_ADDED_STROKE, penwidth=_DIFF_PENWIDTH, fill=_DIFF_ADDED_FILL
    ),
    Mark.REMOVED: _Emphasis(
        stroke=_DIFF_REMOVED_STROKE, penwidth=_DIFF_PENWIDTH, fill=_DIFF_REMOVED_FILL
    ),
    Mark.CHANGED: _Emphasis(
        stroke=_DIFF_CHANGED_STROKE, penwidth=_DIFF_PENWIDTH, fill=_DIFF_CHANGED_FILL
    ),
    Mark.UNCHANGED: _DIMMED,
}


def _diff_emphasis(mark: Mark) -> _Emphasis:
    """How loudly a diff draws something, given what the changeset says of it."""
    return _DIFF_EMPHASIS[mark]


def _diff_badge(mark: Mark, detail: str | None) -> _Row | None:
    """The caption a diff adds under a node, or ``None`` for an untouched one.

    Every marked node gets one, not only the amber ones: the sigil is what
    survives a greyscale print, and a reader who cannot tell the green from the
    amber can still read ``+`` and ``~``.
    """
    if mark is Mark.UNCHANGED:
        return None
    sigil = _DIFF_SIGILS.get(mark.value, "")
    text = f"{sigil} {mark.value}" if detail is None else f"{sigil} {detail}"
    return _Row(port=_inline(text), spans=True)


def _diff_label(label: str, mark: Mark, detail: str | None) -> str:
    """``label`` with the diff's sigil and detail added, on its own line.

    A link has no table to hang a badge row off, so the caption grows instead —
    which is also where the medium and the rate already are, so the two read
    together.
    """
    if mark is Mark.UNCHANGED:
        return label
    sigil = _DIFF_SIGILS.get(mark.value, "")
    caption = f"{sigil} {mark.value}" if detail is None else f"{sigil} {detail}"
    return f"{label}\n{caption}" if label else caption


def _with_badge(rows: tuple[_Row, ...], mark: Mark, detail: str | None) -> tuple[_Row, ...]:
    """``rows`` with the diff's caption appended, when there is one to append."""
    badge = _diff_badge(mark, detail)
    return rows if badge is None else (*rows, badge)


def _diff_style(style: str | None, mark: Mark) -> str | None:
    """``style`` with the dashed outline a removed node is drawn with.

    A deletion has to be legible without colour — this is the picture someone
    prints and takes into a change meeting — so the one mark that means "this
    will not be here" carries a line style as well as a hue.
    """
    if mark is not Mark.REMOVED:
        return style
    parts = [part for part in (style or "filled").split(",") if part]
    if "dashed" not in parts:
        parts.append("dashed")
    return ",".join(parts)


def _node_url(graph: Graph, node: Node, template: Linker | None) -> str | None:
    """Where the document behind ``node`` lives, expanded through ``template``.

    A tunnel node stands for a ``tunnel`` document, so it links to that; a
    layer-3 prefix node stands for an inference from the addresses and links
    nowhere, because there is no file that says ``192.168.10.0/24``. A collapsed
    namespace stands for a directory rather than a document: there are many
    files behind it and no line to point at, so it links nowhere either.
    """
    if template is None or node.is_subnet or node.is_aggregate:
        return None
    fqn = node.tunnel.fqn if node.tunnel is not None else node.fqn
    return _expand(template, graph, fqn, kind=node.kind)


def _expand(template: Linker, graph: Graph, fqn: str, *, kind: str) -> str | None:
    """One element's link, or ``None`` when the graph cannot place it."""
    source = graph.source_of(fqn)
    return template.expand(
        file=source.relative if source is not None else None,
        line=source.line if source is not None else None,
        name=fqn,
        namespace=namespace_of(fqn),
        kind=kind,
    )


def _subtitle(node: Node) -> str:
    """The bracketed line under a node's name: what sort of thing it is."""
    if node.ipam is not None:
        # Checked before the subnet below, which every prefix of the plan also
        # is: at this layer what distinguishes one box from the next is whether
        # it is a block holding other blocks or a leaf holding hosts.
        return f"[{node.ipam.family} {node.ipam.noun}]"
    if node.subnet is not None:
        vrf = f", vrf {node.subnet.vrf}" if node.subnet.vrf else ""
        return f"[{node.subnet.family} subnet{vrf}]"
    if node.tunnel is not None:
        return f"[{node.tunnel.type} tunnel]"
    if node.aggregate is not None:
        # Not "[namespace]": the reader needs to know at a glance that the box
        # is a stand-in, and the number is the one fact a shape cannot carry.
        return f"[namespace, {count_text(node.aggregate.size, 'element')}]"
    if node.rack is not None:
        used = f"{node.rack.used_units}/{node.rack.height}U used"
        return f"[rack, {used}]" if not node.rack.inferred_height else f"[rack, {used}, inferred]"
    if node.routing is not None:
        # The identity its peers know it by, which is what every edge here is
        # labelled against; the kind is still readable from the shape.
        detail = ", ".join(node.routing.describe())
        return f"[{node.kind}, {detail}]" if detail else f"[{node.kind}]"
    if node.security is not None:
        # What the zone *is*, which for the two undeclared ones is the whole of
        # what there is to say: 'local' is the machine, 'any' is a rule that
        # named no zone. A declared zone says how much of the device it holds.
        return f"[zone, {_zone_detail(node.security)}]"
    return f"[{node.kind}]"


def _zone_detail(view: SecurityView) -> str:
    """``2 interfaces``, ``this machine``, ``every zone`` — the bracketed clause."""
    if view.name == LOCAL_ZONE:
        return "this machine"
    if view.name == ANY_ZONE:
        return "every zone"
    return count_text(len(view.interfaces), "interface")


def _node_style(node: Node, style: ResolvedStyle) -> str | None:
    """The ``style`` override for a node, or ``None`` to inherit ``filled``.

    Two things end up in this one Graphviz attribute: what the *node type* is
    drawn as — derived things rounded, non-physical ones dashed — and what a
    resolved style asks for. They are combined rather than one replacing the
    other, so painting a subnet node navy does not also square its corners.
    """
    return _combine_style(_kind_style(node), style)


def _combine_style(base: str | None, style: ResolvedStyle) -> str | None:
    """``base`` with the resolved shape and dash folded in.

    ``None`` in and nothing to add gives ``None`` out, which leaves the
    attribute off the node entirely and the graph-wide ``style=filled``
    inherited — the bytes an unstyled rendering has always produced.
    """
    extra = []
    if style.shape == "rounded":
        extra.append("rounded")
    if style.dash is not None and style.dash != "solid":
        extra.append(style.dash)
    if not extra:
        return base
    parts = [part for part in (base or "filled").split(",") if part]
    parts.extend(part for part in extra if part not in parts)
    return ",".join(parts)


def _kind_style(node: Node) -> str | None:
    """What the *node type* alone is drawn as, before any style is applied."""
    if node.is_subnet:
        # Rounded, for something derived rather than declared: the reader should
        # not go looking for a device with this name.
        return "filled,rounded"
    if node.is_tunnel:
        # Dashed, for something declared but not physical: there is a document
        # behind this box, but nothing anyone can put a hand on.
        return "filled,dashed"
    if node.kind == "adapter":
        # §8.2: an adapter is hardware that may be collapsed into its host, so it
        # is drawn as a provisional part of the diagram.
        return "filled,dashed"
    if node.is_rack:
        # A cabinet, drawn as one: square corners and a heavier frame than the
        # equipment inside it.
        return "filled"
    return None


def _node_rows(node: Node, options: RenderOptions, layer: Layer) -> tuple[_Row, ...]:
    """One row per interface that has an address or a VLAN worth printing.

    An interface with neither is left out: on a 48-port switch those rows would
    be a column of port names burying the two ports that carry an address, and
    the ports a cable lands on are named on the edge already.
    """
    if node.ipam is not None:
        return _ipam_rows(node.ipam, options)

    if node.subnet is not None:
        if options.show_vlans and node.vlans:
            return (_Row(port=f"vlan {compact_ids(node.vlans)}", spans=True),)
        return ()

    if node.tunnel is not None:
        return _tunnel_rows(node.tunnel)

    if node.aggregate is not None:
        return _aggregate_rows(node.aggregate, options)

    if node.rack is not None:
        return _rack_rows(node.rack)

    if node.routing is not None:
        return _routing_rows(node.routing)

    if node.security is not None:
        # Spanning rows: what is in a zone is a list of interface names, not the
        # three-column port table, because at this layer a port is not a place
        # anything is plugged into -- it is what puts traffic in this box.
        return tuple(_Row(port=_inline(line), spans=True) for line in node.security.describe())

    if node.power is not None:
        return _power_rows(node.power)

    if node.identity is not None:
        # Spanning rows, for the same reason the power view uses them: not one
        # of these facts is about a port, and an identity has no ports anyway.
        return tuple(_Row(port=_inline(line), spans=True) for line in node.identity.details())

    # At layer 3 each address is printed on the edge that puts the element in a
    # subnet, which also says which interface holds it; repeating the list under
    # the node would double the label to say less.
    with_addresses = options.show_ips and layer is not Layer.L3

    rows: list[_Row] = []
    for port in node.ports:
        addresses = (
            ", ".join(_address_lines(port.routable_addresses, options.max_addresses))
            if with_addresses
            else ""
        )
        vlans = f"vlan {compact_ids(port.vlans)}" if options.show_vlans and port.vlans else ""
        if not addresses and not vlans:
            continue
        rows.append(_Row(port=_inline(port.name), addresses=addresses, vlans=vlans))

    if len(rows) > _MAX_PORT_ROWS:
        hidden = len(rows) - _MAX_PORT_ROWS
        rows = [
            *rows[:_MAX_PORT_ROWS],
            _Row(port=f"(+{count_text(hidden, 'more interface')})", spans=True),
        ]
    return tuple(rows)


# --------------------------------------------------------------------------- #
# Annotations (§21)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _AreaGroupView:
    """One annotation area drawn as a Graphviz cluster.

    Only reachable under an automatic layout. A fixed one is drawn by
    ``neato -n2``, which draws no cluster at all, so there the same area becomes
    a :class:`_FrameView` instead.
    """

    #: The subgraph's *name* in the DOT source. Positional and ``cluster``-prefixed
    #: for the same two reasons a namespace group's is; see :func:`_groups`.
    id: str
    label: str
    nodes: tuple[_NodeView, ...]
    fill: str
    stroke: str
    #: The Graphviz ``style`` list, e.g. ``rounded,dashed``.
    style: str
    #: ``0`` for ``border: none``, which is how a cluster is drawn with a fill
    #: and no edge; ``None`` leaves Graphviz's default.
    penwidth: str | None = None
    #: ``id`` attribute to emit; see :mod:`netviz.render.annotations`.
    element_id: str | None = None


@dataclass(frozen=True, slots=True)
class _NoteLine:
    """One block of a note's label: whether it is a bullet, and its markup.

    :attr:`html` is already escaped and already marked safe — it is built here
    rather than in the template because the emphasis of §21.1 *is* markup
    (``<B>``, ``<I>``, ``<FONT>``) wrapped around text that must not be, and
    that is not a distinction a template's autoescaping can make. See
    :func:`_note_markup`.
    """

    bullet: bool
    html: Markup


@dataclass(frozen=True, slots=True)
class _NoteNodeView:
    """One note, drawn as an ordinary node with ``shape=note``."""

    id: str
    lines: tuple[_NoteLine, ...]
    fill: str
    stroke: str
    element_id: str | None = None
    #: ``pos``, for a note in a fixed arrangement; ``None`` under an automatic
    #: one, where Graphviz places it like any other node.
    pos: str | None = None


@dataclass(frozen=True, slots=True)
class _LegendRowView:
    """One swatch and its label."""

    color: str
    label: str
    description: str = ""
    #: Height of the swatch cell, in points. A line-shaped swatch is a bar
    #: rather than a block, which is what tells a line style from a fill.
    height: int = _SWATCH_BOX
    #: Set for a ``line``/``dashed``/``dotted`` swatch, which is drawn as a rule
    #: rather than as a filled box.
    rule: bool = False


@dataclass(frozen=True, slots=True)
class _LegendBlockView:
    """One legend, drawn as a cluster holding a single plaintext table."""

    #: The subgraph's name in the DOT source.
    id: str
    #: The DOT node id of the table inside it.
    node: str
    #: The caption, drawn as the table's first row rather than as the cluster's
    #: label: ``neato -n2`` draws no cluster, so a title on the box would
    #: disappear from exactly the drawings that place the key most carefully.
    title: str
    rows: tuple[_LegendRowView, ...]
    fill: str
    stroke: str
    element_id: str | None = None
    pos: str | None = None
    #: Width of the *table's* own border. ``0`` under an automatic layout, where
    #: the cluster draws the frame; ``1`` under a fixed one, where nothing else
    #: will — the same bargain :class:`_FrameView` makes for a namespace box.
    border: str = "0"


def _boxed(areas: Sequence[_AreaGroupView]) -> Iterator[str]:
    """Every node an area cluster has claimed, so nothing else boxes it too."""
    for area in areas:
        for node in area.nodes:
            yield node.id


def _area_groups(
    graph: Graph,
    views: AnnotationViews,
    options: RenderOptions,
    look: _Look,
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    plan: LayoutPlan,
) -> tuple[_AreaGroupView, ...]:
    """The areas of an automatically laid out drawing, as clusters.

    **Precedence.** A node is drawn inside at most one box, because that is all
    a DOT document can express, so three groupings have to be put in an order
    and this is it:

    1. an explicit ``kind: area`` that names the node — the most specific thing
       anybody wrote down about this diagram, and the only one of the three a
       person stated on purpose;
    2. among two areas that both name it, the one declared **first**. Declaration
       order is the only tie-break that does not depend on the graph, so the
       same inventory always draws the same picture;
    3. whatever is left: the layer's own clustering (the VRFs of §16.8) and then
       ``--group-by-namespace``, each of which loses the nodes an area took and
       is omitted entirely when it has none left.

    An area with an explicit ``geometry`` but no drawn members is not a cluster —
    there is nothing to put in it — and is skipped here; under a fixed layout the
    same area *is* drawn, as a rectangle, because there the coordinates mean
    something.
    """
    groups: list[_AreaGroupView] = []
    for index, area in enumerate(views.areas):
        members = [
            graph.nodes[member]
            for member in area.members
            # The first area to name a node keeps it; see the docstring.
            if views.area_of(member) is area
        ]
        if not members:
            continue
        groups.append(
            _AreaGroupView(
                id=f"cluster_area_{index}",
                label=area.label,
                nodes=tuple(_node_views(graph, members, options, look, identity, details, plan)),
                fill=area.fill,
                stroke=area.stroke,
                style=_area_style(area.border),
                penwidth="0" if area.border == "none" else None,
                element_id=area.id if options.element_ids else None,
            )
        )
    return tuple(groups)


def _area_style(border: str) -> str:
    """The Graphviz ``style`` an area's outline is drawn with.

    Always rounded, because a zone is a convention rather than a container, and
    a square box in a diagram of square boxes reads as another device. ``none``
    keeps the rounded fill and loses the edge through ``penwidth`` instead:
    Graphviz has no ``style`` value that means "filled but not outlined".
    """
    return "rounded,filled" if border in ("solid", "none") else f"rounded,filled,{border}"


def _area_frames(views: AnnotationViews, plan: LayoutPlan) -> tuple[_FrameView, ...]:
    """The areas of a *fixed* drawing, as background rectangles and captions.

    ``neato -n2`` draws no clusters, so an arranged diagram would otherwise lose
    every area exactly as it would lose every namespace frame — and the remedy is
    the same one, for the same reason: the rectangle goes into ``_background``
    and the caption is an ordinary ``plaintext`` node, because text in a
    ``_background`` segfaults the Graphviz that Debian and Ubuntu ship. See
    :func:`_background`.

    The rectangle is ``spec.geometry`` when the area gives one, and otherwise the
    box enclosing wherever the members were actually drawn, grown by the area's
    padding — so an area follows the devices it names when the arrangement moves
    them, which is the whole difference between naming members and pinning a box.
    """
    frames: list[_FrameView] = []
    for area in views.areas:
        box = area.box or member_hull(area, _member_corners(area, plan))
        if box is None:
            continue
        frames.append(
            _FrameView(
                id=f"{_FRAME_ID_PREFIX}{area.id}",
                label=area.label,
                # Filled, unlike a namespace frame: an area is a zone behind the
                # diagram rather than a line around part of it.
                outline=_dot_points(box, operation="P"),
                caption=_caption_pos(box),
                color=area.stroke,
                pen=_area_pen(area),
            )
        )
    return tuple(frames)


def _area_pen(area: AreaView) -> str:
    """The xdot operations setting the pen for one area's rectangle.

    Three operations, and every one of them is on the short list of what a
    ``_background`` may safely hold: a line style, a pen colour and a fill
    colour. Emitted per area rather than once, so a dashed zone cannot leak its
    line style into whatever is drawn after it.
    """
    style = "solid" if area.border == "none" else area.border
    stroke = area.fill if area.border == "none" else area.stroke
    return f"S {_xdot_text(style)} c {_xdot_text(stroke)} C {_xdot_text(area.fill)}"


def _member_corners(area: AreaView, plan: LayoutPlan) -> Iterator[tuple[float, float]]:
    """The corners of every member's box, or its centre when nothing measured it.

    A hull over centres alone would cut through the shapes at the edge of the
    zone; the sizes are recorded by ``netviz layout --write`` for exactly this
    sort of reason, and the centre is the honest fallback when they are not.
    """
    for member in area.members:
        placement = plan.geometry.nodes.get(member)
        if placement is None:
            continue
        if placement.width is None or placement.height is None:
            yield (placement.x, placement.y)
            continue
        half_width, half_height = placement.width / 2, placement.height / 2
        yield (placement.x - half_width, placement.y - half_height)
        yield (placement.x + half_width, placement.y + half_height)


def _note_views(
    views: AnnotationViews, options: RenderOptions, plan: LayoutPlan
) -> tuple[_NoteNodeView, ...]:
    """The notes, as nodes.

    A note is an ordinary node with ``shape=note`` — the shape Graphviz has for
    exactly this — so it is laid out with the graph instead of floating over it,
    and a reader can select it, link it and search it like anything else.

    Under a fixed arrangement it needs a ``pos`` like every other node. A note
    that pins its own position uses it; an anchored one that does not is placed
    beside whatever it is anchored to. A note that is neither — its anchor is on
    another layer, or a filter removed it — is the one case a fixed drawing
    leaves out, because the alternative is a ``pos`` of nothing, which the no-op
    engine reads as the origin and draws the callout on top of whatever the
    arrangement put in the corner.
    """
    placed = plan.mode is LayoutMode.FIXED
    views_and_positions = ((note, _note_pos(note, plan)) for note in views.notes)
    return tuple(
        _NoteNodeView(
            id=f"{_ANNOTATION_ID_PREFIX}{note.id}",
            lines=_note_lines(note),
            fill=note.fill,
            stroke=note.stroke,
            element_id=note.id if options.element_ids else None,
            pos=pos,
        )
        for note, pos in views_and_positions
        if pos is not None or not placed
    )


def _note_pos(note: NoteView, plan: LayoutPlan) -> str | None:
    """Where a note sits in a fixed drawing, or ``None`` to let Graphviz decide."""
    if plan.mode is not LayoutMode.FIXED:
        return None
    if note.x is not None and note.y is not None:
        return _pos(note.x, note.y)
    placement = plan.geometry.nodes.get(note.anchor)
    if placement is None:
        return None
    return _pos(placement.x + _NOTE_OFFSET[0], placement.y + _NOTE_OFFSET[1])


def _note_lines(note: NoteView) -> tuple[_NoteLine, ...]:
    """One row per block of the parsed note; an empty note keeps one blank row."""
    lines = tuple(
        _NoteLine(bullet=block.kind == "bullet", html=_note_markup(block.spans))
        for block in note.lines
    )
    return lines or (_NoteLine(bullet=False, html=Markup("")),)


def _note_markup(spans: Sequence[Span]) -> Markup:
    """One block's spans as Graphviz HTML-like markup.

    The text of each span is escaped and the tag around it is not, which is the
    one place in this renderer where those two have to be told apart inside a
    single string — so it is done here, with :class:`~markupsafe.Markup`, rather
    than in the template, where autoescaping can only make one decision for the
    whole value. The template's escaping contract is unchanged: what it receives
    is already exactly what belongs in an HTML-like label.

    Graphviz's HTML-like labels have ``<B>`` and ``<I>`` but no ``<CODE>``, so
    code spans are drawn in the monospace face, which is the difference a reader
    is looking for.
    """
    return Markup("").join(_span_markup(span) for span in spans)


def _span_markup(span: Span) -> Markup:
    # ``printable`` rather than ``_inline``: the whitespace *between* two spans
    # belongs to one of them, and collapsing it here would join a bold word to
    # the one after it. The parser has already flattened the line breaks, so
    # there is nothing left for the one-line rule to protect against.
    text = Markup.escape(printable(span.text))
    if span.style == "bold":
        return Markup("<B>{}</B>").format(text)
    if span.style == "italic":
        return Markup("<I>{}</I>").format(text)
    if span.style == "code":
        return Markup('<FONT FACE="monospace">{}</FONT>').format(text)
    return text


def _leader_views(views: AnnotationViews) -> tuple[_EdgeView, ...]:
    """One dotted line per note that points at something.

    ``constraint=false`` is the important attribute: a leader is not a link, and
    a note allowed to constrain the ranking would move the devices it is
    commenting on. Every other attribute says the same thing more quietly — no
    arrowhead, the note's own outline colour, and a dotted line no medium uses.

    Emitted after every real link, so a document read by a human keeps the
    topology in one block and the commentary after it.
    """
    return tuple(
        _EdgeView(
            source=f"{_ANNOTATION_ID_PREFIX}{note.id}",
            target=note.anchor,
            color=note.stroke,
            style="dotted",
            arrowhead="none",
            constraint="false",
        )
        for note in views.notes
        if note.leader
    )


def _legend_views(
    graph: Graph, views: AnnotationViews, options: RenderOptions, plan: LayoutPlan
) -> tuple[_LegendBlockView, ...]:
    """The legends, each a cluster holding one plaintext table.

    A cluster rather than a bare node so the key gets a frame without netviz
    measuring any text, and a ``plaintext`` node inside it because the swatches
    are table cells — a legend is a table, and Graphviz's HTML-like labels are
    the one place this renderer can draw one. The *title* goes inside the table
    rather than on the cluster, because a fixed drawing has no cluster to put it
    on and a key whose caption depended on the layout engine would be a key that
    vanished from the arranged diagram somebody had taken most care over.

    **Where it ends up.** Under a fixed arrangement the corner is honoured: the
    table is placed just outside the bounding box of everything else, on the side
    ``spec.corner`` names. Under an automatic layout **Graphviz decides**, and it
    will generally put a disconnected cluster beside the drawing rather than in
    the corner asked for — there is no Graphviz attribute that pins one, and the
    alternatives (a rank constraint, an invisible edge) distort the topology to
    move the key, which is a worse trade than a key in the wrong corner. Pin the
    arrangement with ``netviz layout --write`` to place it exactly.
    """
    bounds = _drawing_bounds(graph, plan) if plan.mode is LayoutMode.FIXED else None
    return tuple(
        _LegendBlockView(
            id=f"cluster_legend_{index}",
            node=f"{_ANNOTATION_ID_PREFIX}{legend.id}",
            title=legend.title,
            rows=tuple(_legend_row(entry) for entry in legend.entries),
            fill=legend.fill,
            stroke=legend.stroke,
            element_id=legend.id if options.element_ids else None,
            pos=None if bounds is None else _legend_pos(legend, bounds),
            border="1" if bounds is not None else "0",
        )
        for index, legend in enumerate(views.legends)
        if legend.entries
    )


def _legend_row(entry: LegendSwatch) -> _LegendRowView:
    rule = entry.shape in ("line", "dashed", "dotted")
    return _LegendRowView(
        color=entry.color,
        label=_inline(entry.label),
        description=_inline(entry.description),
        height=_SWATCH_RULE if rule else _SWATCH_BOX,
        rule=rule,
    )


def _drawing_bounds(graph: Graph, plan: LayoutPlan) -> Box | None:
    """The box enclosing everything placed, for putting a legend outside it."""
    corners: list[tuple[float, float]] = []
    for fqn in graph.nodes:
        placement = plan.geometry.nodes.get(fqn)
        if placement is None:
            continue
        width = (placement.width or 0.0) / 2
        height = (placement.height or 0.0) / 2
        corners.append((placement.x - width, placement.y - height))
        corners.append((placement.x + width, placement.y + height))
    if not corners:
        return None
    xs = [x for x, _ in corners]
    ys = [y for _, y in corners]
    return Box.from_bounds(min(xs), min(ys), max(xs), max(ys))


def _legend_pos(legend: LegendView, bounds: Box) -> str:
    """Where a legend's table sits in a fixed drawing: just outside a corner.

    Outside rather than inside, because netviz does not measure text and a key
    placed inside the bounding box by guesswork would sooner or later be drawn
    over a device. Graphviz grows the canvas to fit a node, so nothing is
    clipped.
    """
    left, bottom, right, top = bounds.bounds
    x = (left - _LEGEND_MARGIN) if legend.at_left else (right + _LEGEND_MARGIN)
    y = (top + _LEGEND_MARGIN) if legend.at_top else (bottom - _LEGEND_MARGIN)
    return _pos(x, y)


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #


def _edge_views(
    graph: Graph,
    options: RenderOptions,
    look: _Look,
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    plan: LayoutPlan,
) -> Iterator[_EdgeView]:
    routes = route_table(graph, options)
    default_style = default_routing(graph, options)
    # An orthogonal layout that Graphviz is doing itself cannot carry a real
    # edge label — it says so on stderr and draws it somewhere arbitrary — so
    # the label becomes an ``xlabel``, which Graphviz floats near the line
    # instead. Nothing is lost but the exact position, and the alternative is a
    # diagram whose annotations have silently moved.
    floating = plan.mode is not LayoutMode.FIXED and default_style is Routing.ORTHOGONAL
    for index, edge in enumerate(graph.edges):
        # Why the link is drawn decides how it looks, and the default comes
        # from the one table an ``auto: layers`` legend reads too (§21), so a
        # swatch can never name a colour this drawing does not use. A link
        # that says something about itself, or that a theme says something
        # about, has already had it folded in by the resolver (§22).
        resolved = look.styles.edge(index)
        colour = resolved.faded_stroke or ""
        style = resolved.dash or "solid"
        fontcolor = resolved.faded_font_color
        emphasis = _edge_emphasis(edge, options.highlight)
        if emphasis is not None:
            colour = emphasis.stroke
        mark = Mark.UNCHANGED if options.diff is None else options.diff.edge(edge.id)
        if options.diff is not None:
            emphasis = _diff_emphasis(mark)
            colour = emphasis.stroke
            # A removed link is dashed whatever it is made of: at this point the
            # question a reader has is not "copper or fibre" but "will it be
            # there". The medium is still on the label and in the tooltip.
            style = "dashed" if mark is Mark.REMOVED else style
        if emphasis is not None:
            fontcolor = emphasis.fontcolor
        element = identity.edge(index)
        record = details.get(element) if element is not None else None
        line = routes[index]
        label = (
            _diff_label(
                _edge_label(edge, graph.layer, options),
                mark,
                None if options.diff is None else options.diff.badge(edge.id),
            )
            or None
        )
        yield _EdgeView(
            source=edge.source,
            target=edge.target,
            color=colour,
            # The medium keeps its line style even when the link is dimmed: a
            # highlight says which links the answer runs over, not what they are.
            style=style,
            # Under a highlight the width says "on the path" instead of "this
            # fast", because a reader looking at a traced route is asking the
            # first question; the rate is still on the label and in the tooltip.
            penwidth=(
                emphasis.penwidth
                if emphasis is not None
                # A declared width is a decision about *this link* and wins
                # over the one the rate implies; with nothing declared the
                # rate still speaks, as it always has.
                else resolved.penwidth or _penwidth(edge.speed)
            ),
            label=None if floating else label,
            xlabel=label if floating else None,
            tooltip=detail_text(record) if record is not None else None,
            element_id=element if options.element_ids else None,
            url=_edge_url(graph, edge, options.link_template),
            fontcolor=fontcolor,
            fontsize=None if resolved.font_size is None else str(resolved.font_size),
            weight=str(edge.bundle.size) if edge.bundle is not None else None,
            pos=None if line is None else _spline(line.controls),
            lp=_label_pos(line, plan.geometry.link(edge.id).label) if label else None,
        )


def _label_pos(line: Route | None, label: LabelPlacement | None) -> str | None:
    """Where a link's annotation is pinned, as a Graphviz ``lp``.

    ``lp`` is normally an *output* attribute — where Graphviz decided to put the
    label — but the no-op engine honours it on input, which is the one place a
    label position can be pinned at all. So a nudged label reproduces exactly in
    a fixed drawing and is left to Graphviz everywhere else, which is the same
    bargain the bends make.
    """
    if line is None or label is None:
        return None
    placed = label_position(line, label)
    return None if placed is None else _pos(*placed)


def _edge_url(graph: Graph, edge: Edge, template: Linker | None) -> str | None:
    """The document behind a link, expanded through ``template``.

    An adapter attachment is declared by the adapter (§8.2) and a tunnel leg by
    the tunnel, so both link to the one document that says the link exists. A
    layer-3 membership is declared by nobody — it follows from two addresses
    being in one prefix — so it links nowhere. Neither does a bundle: several
    documents declare it, and picking one of them would send the reader to an
    arbitrary member. The tooltip names them all.
    """
    if template is None or edge.kind is EdgeKind.SUBNET or edge.bundle is not None:
        return None
    fqn = edge.tunnel.fqn if edge.tunnel is not None else edge.id.partition("#")[0]
    return _expand(template, graph, fqn, kind=edge.kind.value)


def _penwidth(speed: int | None) -> str | None:
    """The line width standing for ``speed``, or ``None`` when none is declared.

    ``style=bold`` on a fibre link also implies a width; an explicit
    ``penwidth`` overrides it, so rate wins over medium wherever both apply and
    the medium stays readable from the colour.
    """
    if speed is None:
        return None
    for threshold, width in _SPEED_PENWIDTH:
        if speed >= threshold:
            return f"{width:.1f}"
    return f"{_MIN_PENWIDTH:.1f}"


def _port_pair(edge: Edge) -> str:
    """``lan0 -- port1``, or the one end that names an interface.

    The host end of an adapter attachment names no interface (§8.1), and a
    subnet membership has an element end only.
    """
    ends = [port for port in (edge.source_port, edge.target_port) if port]
    if len(ends) == 2:
        return f"{edge.source_port} -- {edge.target_port}"
    return ends[0] if ends else ""


def _edge_label(edge: Edge, layer: Layer, options: RenderOptions) -> str:
    """What the link is annotated with: the ports it joins, then layer detail.

    At layer 1 the interesting facts are physical — medium, rate, cable label.
    At layer 2 they are logical, so VLAN membership takes the label and the
    physical detail moves to the tooltip. At layer 3 the label is the address
    that puts the element in the subnet.
    """
    if edge.kind is EdgeKind.ENCAPSULATION:
        # The edge *is* the sentence "this runs inside that"; naming the inner
        # tunnel's type is what tells the reader which way round it goes.
        return f"{edge.label} over" if edge.label else "over"

    if edge.adjacency is not None:
        # The AS pair, or the area: which side of the routing plane the edge is
        # on is the whole content of a routing diagram, so it takes the label and
        # the ports go to the tooltip. A session's description follows it, because
        # "transit" beside ``65001 → 65002`` is what makes the picture readable.
        adjacency = [edge.adjacency.label]
        if edge.adjacency.description:
            adjacency.append(_inline(edge.adjacency.description))
        return "\n".join(adjacency)

    if edge.policy is not None:
        # The rules themselves, up to the handful that fit, because a policy
        # diagram in which the lines are unlabelled has drawn the topology of a
        # firewall and not its policy. Past that the label counts and the
        # tooltip lists; a chain is as long as it needs to be, a label is not.
        return "\n".join(_inline(line) for line in edge.policy.label())

    parts: list[str] = []
    ports = _port_pair(edge)
    if ports:
        parts.append(ports)
    if edge.bundle is not None:
        # How many lines this one line stands for, immediately under the ports,
        # because it is the fact that distinguishes a bundle from a link.
        parts.append(edge.bundle.summary)

    if edge.kind is EdgeKind.TUNNEL and edge.tunnel is not None:
        parts.extend(_tunnel_label(edge.tunnel))
        if layer is Layer.L2 and options.show_vlans and edge.vlans:
            parts.append(f"vlan {compact_ids(edge.vlans)}")
        return "\n".join(parts)

    if edge.kind is EdgeKind.SUBNET:
        if options.show_ips and edge.addresses:
            parts.extend(_address_lines(edge.addresses, options.max_addresses))
        if options.show_vlans and edge.vlans:
            parts.append(f"vlan {compact_ids(edge.vlans)}")
        return "\n".join(parts)

    if edge.kind.is_power and edge.feed is not None:
        # The ports already name the outlet and the PSU, so the label adds the
        # one number the reader is after: what this cord carries.
        watts = edge.feed.reserved_watts
        if watts:
            parts.append(f"{format_watts(watts)} W")
        if edge.feed.through:
            parts.append(f"via {_inline(short_name(edge.feed.through[0]))}")
        return "\n".join(parts)

    if layer is Layer.L2:
        # The association is layer-2 detail, not physical detail: which network
        # the link is on and on which frequency is exactly what distinguishes
        # two radio links that a dashed line draws identically (§6.2.6).
        if edge.wireless is not None:
            association = edge.wireless.describe()
            if association:
                parts.append(association)
        if options.show_vlans and edge.vlans:
            parts.append(f"vlan {compact_ids(edge.vlans)}")
        elif edge.kind is EdgeKind.ATTACHMENT and edge.label:
            parts.append(edge.label)
        return "\n".join(parts)

    if edge.label:
        parts.append(edge.label)
    # A bundle whose members disagree about the medium reports none, and an
    # empty line in a label is a blank row rather than nothing.
    if edge.kind is EdgeKind.CABLE and edge.medium and edge.medium != "copper":
        parts.append(edge.medium)
    speed = edge.speed_text
    if speed:
        parts.append(speed)
    if options.show_vlans and edge.vlans and edge.kind is EdgeKind.CABLE:
        parts.append(f"vlan {compact_ids(edge.vlans)}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Shared formatting
# --------------------------------------------------------------------------- #


def _tunnel_label(view: TunnelView) -> list[str]:
    """What a tunnel edge is annotated with, after the ports it joins.

    The encapsulation is always named — a dashed line says "tunnel" but not
    *which* tunnel, and the difference between WireGuard and cleartext GRE is
    the whole point. Nesting is spelled out on the same line (``vxlan over
    ipsec``) so a reader never has to open the overlay view to see it.
    """
    parts = [view.stack_text]
    if view.vni is not None:
        parts.append(f"vni {view.vni}")
    if view.label:
        parts.append(view.label)
    if not view.encrypted:
        # A tunnel a reader assumes is private but is not is the single most
        # expensive thing this diagram can get wrong, so it is on the label
        # rather than in the tooltip.
        parts.append("via encrypted underlay" if view.encrypted_by else "cleartext")
    return parts


def _power_rows(view: PowerNode) -> tuple[_Row, ...]:
    """What a node says about power (§17.5): the whole label, one clause a row.

    Spanning rows rather than the three-column port table, because none of these
    facts is about one port: a draw, a capacity and a PoE pool are properties of
    the box. The port that sources PoE is named on the *edge*, which is where the
    reader is looking when they ask which one it is.
    """
    return tuple(_Row(port=_inline(line), spans=True) for line in view.describe())


#: Free blocks named under a prefix before the rest are counted off. Three,
#: because the question they answer — "where does the next subnet go?" — is
#: answered by the widest one and its two alternatives; a ``/16`` with a stray
#: host in it has seventeen free blocks and sixteen of them are fragments.
_MAX_FREE_BLOCKS: Final = 3


def _ipam_rows(view: IpamView, options: RenderOptions) -> tuple[_Row, ...]:
    """The address plan's card for one prefix: how full, how much left, where.

    The bar spans the table and comes first: across a page of prefixes it is what
    a reader scans, and a column of percentages is not. Everything under it is
    the same fact in numbers, because a bar sixteen cells wide cannot tell 3 %
    from 5 % and an address plan is a document people do arithmetic with.

    The free *blocks* are named only for a prefix something is carved out of.
    For a leaf the count above them is the whole answer — the gaps between three
    hosts in a ``/24`` are not blocks anybody hands out — and naming them would
    turn every leaf into a wall of ``/31``s.
    """
    rows = [
        _Row(port=f"{view.bar}  {view.percent}", spans=True),
        _Row(port="used", addresses=f"{view.assigned} of {view.capacity_text}"),
    ]
    if view.free:
        rows.append(_Row(port="free", addresses=view.free_text))
    if view.devices:
        rows.append(_Row(port="in use by", addresses=count_text(view.devices, "element")))
    if options.show_vlans and view.utilisation.vlans:
        rows.append(_Row(port="vlan", addresses=compact_ids(view.utilisation.vlans)))
    for index, block in enumerate(view.free_blocks[:_MAX_FREE_BLOCKS]):
        rows.append(_Row(port="next" if index == 0 else "", addresses=_inline(block)))
    hidden = len(view.free_blocks) - _MAX_FREE_BLOCKS
    if hidden > 0:
        rows.append(_Row(port=f"(+{count_text(hidden, 'more free block')})", spans=True))
    return tuple(rows)


def _rack_rows(view: RackView) -> tuple[_Row, ...]:
    """The elevation: one row per rack unit, from the top of the cabinet down.

    Every unit gets a row, occupied or not, because the free space is half of
    what an elevation is for. A multi-unit element appears on each unit it
    fills — the row that starts it carries its name and kind, the ones above it
    carry a continuation mark — which is how a reader counts what is left
    without doing arithmetic. The rows are never truncated: an elevation with
    the middle of the rack elided would be worse than none.
    """
    rows: list[_Row] = []
    for unit, slot in view.elevation():
        if slot is None:
            rows.append(_Row(port=f"U{unit}", addresses=_EMPTY_UNIT, vlans=""))
            continue
        continuation = unit != slot.position
        rows.append(
            _Row(
                port=f"U{unit}",
                addresses=_inline(slot.name) if not continuation else "\u2502",
                vlans=_slot_note(slot) if not continuation else _continuation_note(slot, unit),
            )
        )
    return tuple(rows)


def _slot_note(slot: RackSlot) -> str:
    """``[server] 120 W`` — what occupies a unit, and what it costs (§17.5).

    The power note is appended to the kind rather than given a column of its own:
    an elevation is already three columns wide, and the reader asking "can this
    rack take another box" is asking one question of the two facts.
    """
    note = slot.power.rack_note() if slot.power is not None else ""
    return f"[{slot.kind}] {note}" if note else f"[{slot.kind}]"


def _continuation_note(slot: RackSlot, unit: int) -> str:
    """The height, printed on the topmost unit a multi-unit element fills."""
    return f"{slot.height}U" if unit == slot.top else ""


def _aggregate_rows(view: AggregateView, options: RenderOptions) -> tuple[_Row, ...]:
    """The record rows of a collapsed namespace: its census, then what it joins.

    The label answers the three questions the collapsed elements would have
    answered between them — how many of what, in which VLANs, in which prefixes
    — because a box that only said ``sites/north`` would summarise nothing. The
    element names themselves stay in the tooltip: there may be two hundred.
    """
    rows = [_Row(port=view.kind_text, spans=True)]
    if view.internal_links:
        rows.append(_Row(port=f"{count_text(len(view.internal_links), 'link')} inside", spans=True))
    if options.show_vlans and view.vlans:
        rows.append(_Row(port=f"vlan {compact_ids(view.vlans)}", spans=True))
    if options.show_ips and view.subnets:
        for line in _address_lines(view.subnets, options.max_addresses):
            rows.append(_Row(port=line, spans=True))
    return tuple(rows)


def _routing_rows(view: RoutingView) -> tuple[_Row, ...]:
    """The record rows of a router in the routing view: its VRFs, then its routes.

    The AS, the router id and the area are already in the subtitle — they say
    *who this router is*, which is what the node is for. What the rows add is what
    it carries: the instances it holds, and the routes it holds them in.
    """
    rows: list[_Row] = []
    for name, rd in view.vrfs[:_MAX_PORT_ROWS]:
        rows.append(_Row(port=f"vrf {_inline(name)}", addresses=_inline(rd)))
    hidden_vrfs = len(view.vrfs) - _MAX_PORT_ROWS
    if hidden_vrfs > 0:
        rows.append(_Row(port=f"(+{count_text(hidden_vrfs, 'more vrf')})", spans=True))
    for route in view.routes[:_MAX_PORT_ROWS]:
        rows.append(_Row(port=_inline(route), spans=True))
    hidden_routes = len(view.routes) - _MAX_PORT_ROWS
    if hidden_routes > 0:
        rows.append(_Row(port=f"(+{count_text(hidden_routes, 'more route')})", spans=True))
    return tuple(rows)


def _tunnel_rows(view: TunnelView) -> tuple[_Row, ...]:
    """The record rows of a tunnel drawn as a node: its stack, then its ends."""
    rows: list[_Row] = []
    # The subtitle already says ``[ipsec tunnel]``; the summary only earns a row
    # when it adds a VNI, an underlay or the fact that nothing is encrypted.
    if view.summary != view.type:
        rows.append(_Row(port=view.summary, spans=True))
    for end in view.ends[:_MAX_PORT_ROWS]:
        rows.append(_Row(port=_inline(end.element_name), addresses=_inline(end.interface)))
    hidden = len(view.ends) - _MAX_PORT_ROWS
    if hidden > 0:
        rows.append(_Row(port=f"(+{count_text(hidden, 'more endpoint')})", spans=True))
    if view.mtu is not None:
        rows.append(_Row(port=f"mtu {view.mtu}", spans=True))
    return tuple(rows)


def _address_lines(addresses: Sequence[str], limit: int) -> list[str]:
    if len(addresses) <= limit:
        return list(addresses)
    remaining = len(addresses) - limit
    return [*addresses[:limit], f"(+{remaining} more)"]


#: Whatever an inventory wrote, reduced to one line of printable text. A name
#: cannot contain a newline or a control character (§2 name grammar), but the
#: renderer must not depend on a validator that a later refactor could move or
#: relax — the same reason node ids are quoted rather than trusted. Graphviz
#: rejects a record label holding a control character outright, so this is the
#: difference between a diagram and an error message.
_inline = plain_text
