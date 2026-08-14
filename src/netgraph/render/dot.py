"""Graphviz DOT renderer, and the image formats Graphviz produces from it.

The document is laid out by a Jinja2 template
(``netgraph/render/templates/graph.dot.j2``) rather than by string concatenation
here: the output is a documented artefact (``netgraph render -f dot`` is meant
to be diffable and hand-editable), so its shape, attribute order and indentation
belong in one readable file. This module's job is to turn a
:class:`~netgraph.render.graph.Graph` into the small view model that template
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

* ``tooltip`` — the per-element detail of :mod:`netgraph.render.details`, the
  same records ``netgraph web`` shows in its info boxes, as plain text. Graphviz
  writes it as ``xlink:title`` on an anchor wrapping the shape, which not every
  browser pops up, so :func:`_promote_tooltips` moves it into the ``<title>``
  element of the shape's group on the way out. That is the one construct every
  browser has shown as a tooltip since SVG 1.1, with no JavaScript.
* ``URL`` — the link ``--link-template`` builds from the document, line and name
  each element came from (:mod:`netgraph.render.links`), so a diagram in a wiki
  links back to the YAML behind every node.
* ``id`` — a stable identity derived from the fully-qualified name
  (:mod:`netgraph.render.ids`), so a shape can be deep-linked and styled from
  outside. Graphviz copies it verbatim into the element it emits.

Icons
-----

With :attr:`RenderOptions.icons <netgraph.render.options.RenderOptions.icons>`
set, a node is drawn as its kind's picture instead of a Graphviz shape: the
image becomes the first row of the same record table, and the node loses its
shape and fill so that nothing is drawn behind it. The file name goes into the
table as a bare ``<IMG SRC="router.svg">`` and the directory holding it into one
``imagepath`` graph attribute, which keeps the single machine-specific string in
a rendering on one line — and keeps everything interpolated into an HTML
attribute inside netgraph's own alphabet, since a file name is
``<kind>.<extension>`` and a kind comes from a closed set.

Graphviz resolves that path while it lays the graph out, so an *image* rendering
would otherwise reference a file the reader may not have. SVG output therefore
gets its icons embedded as ``data:`` URIs on the way out
(:func:`_embed_icons`), which is what makes ``netgraph render -f svg`` still
produce one self-contained file.

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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Final

from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup

from netgraph.errors import RenderError, clip_text, compact_ids, count_text
from netgraph.layout.geometry import (
    COORDINATE_PLACES,
    Box,
    Geometry,
    LabelPlacement,
    LayoutMode,
    Routing,
)
from netgraph.layout.graphviz import (
    POINTS_PER_INCH,
    DrawingError,
    parse_drawing,
    realign,
)
from netgraph.layout.routing import Route, label_position
from netgraph.loader.inventory import namespace_of, short_name
from netgraph.models import format_watts
from netgraph.power import PowerNode
from netgraph.render.aggregate import AGGREGATE_KIND, AggregateView
from netgraph.render.details import (
    build_details,
    detail_text,
    namespace_text,
    plain_text,
    printable,
)
from netgraph.render.diffview import Mark
from netgraph.render.graph import (
    GROUP_KIND,
    PATCHPANEL_KIND,
    PDU_KIND,
    RACK_KIND,
    SUBNET_KIND,
    TUNNEL_KIND,
    USER_KIND,
    Edge,
    EdgeKind,
    Graph,
    Layer,
    Node,
    RackSlot,
    RackView,
    RoutingView,
    TunnelView,
)
from netgraph.render.highlight import Highlight
from netgraph.render.icons import IconTheme, suffix_order
from netgraph.render.ids import ElementIds, element_ids
from netgraph.render.links import Linker
from netgraph.render.options import DEFAULT_RANKDIR, RenderOptions
from netgraph.render.routes import anchors_of, default_routing, route_table

__all__ = [
    "DOT_ENV_VAR",
    "DOT_EXECUTABLE",
    "IMAGE_FORMATS",
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
DOT_ENV_VAR: Final = "NETGRAPH_DOT"

#: The Graphviz layout engine that reads a ``pos`` attribute. ``dot`` ignores
#: one outright, so an arrangement can only be reproduced through this one —
#: selected with ``-K`` on the same executable, so nothing extra has to be found
#: on ``PATH``.
NOOP_ENGINE: Final = "neato"

#: The cluster frame netgraph draws itself when the layout engine will not.
#: The three colours match what ``graph.dot.j2`` sets on a ``subgraph cluster``,
#: because the two are alternative ways of drawing the same frame and a diagram
#: must not change appearance when it becomes fixed.
_CLUSTER_STROKE: Final = "#9ca3af"
_CLUSTER_LABEL_COLOUR: Final = "#4b5563"
_CLUSTER_FONT: Final = "Helvetica,Arial,sans-serif"
_CLUSTER_FONT_SIZE: Final = 11
#: Prefix of the node id a frame's caption is emitted under. A colon cannot
#: appear in an element name, so it can never collide with one.
_FRAME_ID_PREFIX: Final = "cluster-label:"

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

#: The install command for the platform netgraph is running on, first, followed
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
    ``netgraph watch`` in the same session should get a diagram, not the
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

#: Shape, fill and outline per element kind: the kind picks the glyph, so the
#: topology is readable before a single label is. The outline is a saturated
#: version of the fill, so kind stays legible in a greyscale print.
#:
#: ``hub`` and ``computer`` are both boxes (``rectangle`` is Graphviz's synonym
#: for ``box``); they are told apart by their palette, and a hub is rare enough
#: in a modern inventory that spending a third box shape on it would cost more
#: than it buys. :data:`~netgraph.render.graph.SUBNET_KIND` is not hardware — it
#: is a prefix the addresses imply — so it gets a palette entry of its own and
#: is drawn with rounded corners (see :func:`_node_style`).
_NODE_STYLE: Final[Mapping[str, tuple[str, str, str]]] = {
    "router": ("diamond", "#dbe9f6", "#2563eb"),
    "switch": ("box3d", "#dcf0dc", "#16a34a"),
    "hub": ("box", "#f0e6d2", "#a16207"),
    "computer": ("rectangle", "#f5f5f5", "#6b7280"),
    "server": ("cylinder", "#eae2f5", "#7c3aed"),
    "adapter": ("ellipse", "#fdf0e3", "#ea580c"),
    # A patch panel is passive, so it gets the one shape that carries no
    # direction and no processing: a plain rectangle, in a neutral slate that
    # says "this is not a device" without borrowing another kind's colour.
    PATCHPANEL_KIND: ("box", "#eef2f7", "#64748b"),
    SUBNET_KIND: ("box", "#e0f2f1", "#0f766e"),
    # A tunnel is not hardware either, but unlike a subnet it *is* declared, so
    # it keeps a shape of its own rather than borrowing a box.
    TUNNEL_KIND: ("hexagon", "#ede9fe", "#6d28d9"),
    # A collapsed namespace is a *folder* of elements, and Graphviz's ``folder``
    # shape says exactly that — a reader who has ever seen a file manager knows
    # there is something inside it without being told. The slate palette is the
    # one no element kind uses, so a box that is not a device cannot be mistaken
    # for one at a glance.
    AGGREGATE_KIND: ("folder", "#e2e8f0", "#475569"),
    # A rack is a cabinet, not a thing on the network. ``box3d`` is already the
    # switch's, so the elevation gets a plain frame and earns its identity from
    # the table inside it.
    RACK_KIND: ("box", "#f8fafc", "#334155"),
    # A PDU is a strip, and ``box`` drawn tall is as close as Graphviz gets.
    # Amber is the colour every electrical drawing uses for a live conductor and
    # the one no element kind here had taken, which is what keeps a power node
    # from being read as part of the data path.
    PDU_KIND: ("box", "#fef3c7", "#b45309"),
    # An identity is a person, and Graphviz has no person. An ``oval`` is the
    # shape every organisation chart draws one with, and rose is the last accent
    # left — which matters more than the hue itself: the identity view must not
    # be mistakable for a fragment of the network views at a glance.
    USER_KIND: ("oval", "#fce7f3", "#be185d"),
    # A group is a *container* of those, so it borrows the folder shape the
    # collapsed namespace uses and the identity palette, saying both things at
    # once: something is inside it, and what is inside it is people.
    GROUP_KIND: ("folder", "#fbcfe8", "#9d174d"),
}
_DEFAULT_NODE_STYLE: Final[tuple[str, str, str]] = ("box", "#f5f5f5", "#6b7280")

#: Edge colour and line style per cable medium. Style carries the medium and
#: colour repeats it, so a link stays classifiable in a greyscale print and for
#: a reader who cannot distinguish the two accent colours.
_MEDIUM_STYLE: Final[Mapping[str, tuple[str, str]]] = {
    "copper": ("#4a4a4a", "solid"),
    "fiber": ("#d97706", "bold"),
    "wireless": ("#2563eb", "dashed"),
}
_DEFAULT_MEDIUM_STYLE: Final[tuple[str, str]] = ("#4a4a4a", "solid")

#: An adapter attachment is a bus, not a cable (§8.2). Drawing it like one would
#: claim a link that does not exist, so it gets a style no medium uses.
_ATTACHMENT_STYLE: Final[tuple[str, str]] = ("#9ca3af", "dotted")

#: A subnet membership is not a cable either, so it borrows the subnet's colour.
_SUBNET_EDGE_STYLE: Final[tuple[str, str]] = ("#0f766e", "solid")

#: A tunnel is drawn dashed, because it runs over a path the diagram already
#: shows rather than over one of its own. Colour carries confidentiality — the
#: one property of a tunnel a reader most needs at a glance — and the two are
#: far enough apart in lightness to survive a greyscale print: violet when the
#: payload is protected, crimson when it crosses the underlay in the clear.
_TUNNEL_STYLE: Final[tuple[str, str]] = ("#6d28d9", "dashed")
_CLEARTEXT_TUNNEL_STYLE: Final[tuple[str, str]] = ("#be123c", "dashed")

#: A tunnel's ``over`` is not a path at all; it says one link is carried by
#: another. Dotted and violet: the vocabulary of the tunnel it belongs to,
#: with a line weight that keeps it behind the tunnels themselves.
_ENCAPSULATION_STYLE: Final[tuple[str, str]] = ("#8b5cf6", "dotted")

#: A membership (§19.3) is the only edge the identity view has, so it needs no
#: contrast with a sibling: solid, in the identity rose, and told apart from
#: everything else by being the only line on the page.
_MEMBERSHIP_STYLE: Final[tuple[str, str]] = ("#be185d", "solid")

#: The two adjacencies of the routing view (§16.6). A BGP session is a
#: configured, point-to-point relationship, so it is drawn *solid*; an OSPF
#: adjacency is discovered and belongs to an area rather than to a pair, so it is
#: dotted. Both are blue-green — the colour of the routed layers here — and the
#: difference in line style survives a greyscale print, which the difference in
#: hue would not.
_BGP_STYLE: Final[tuple[str, str]] = ("#0369a1", "solid")
_OSPF_STYLE: Final[tuple[str, str]] = ("#0f766e", "dotted")

#: The two feeds of the power view (§17.5). An outlet feed is a cord somebody can
#: pull, so it is drawn *solid* in the PDU's amber; a PoE feed is power riding on
#: a data run that the diagram draws elsewhere, so it is dashed — the same
#: vocabulary a tunnel uses for "this runs over something else", in the power
#: palette rather than the tunnel one. The line style is what survives a
#: greyscale print, which is why the distinction is not carried by hue alone.
_OUTLET_STYLE: Final[tuple[str, str]] = ("#b45309", "solid")
_POE_STYLE: Final[tuple[str, str]] = ("#ca8a04", "dashed")

#: Outline and text colour of something a :class:`~netgraph.render.highlight.Highlight`
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

#: How a :class:`~netgraph.render.diffview.DiffOverlay` paints each mark:
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
#: What a badge is prefixed with, per mark: the sigils ``netgraph plan`` already
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
#: icon is drawn at is netgraph's decision rather than the theme author's.
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
    #: See :mod:`netgraph.render.ids`.
    element_id: str | None = None
    #: ``URL`` attribute to emit; see :mod:`netgraph.render.links`.
    url: str | None = None
    #: Outline width, and the colour of every label the node's HTML table does
    #: not colour itself. Both are ``None`` unless a highlight is in force, so a
    #: rendering without one is byte-identical to what it always was.
    penwidth: str | None = None
    fontcolor: str | None = None
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
    #: ``id`` attribute to emit; see :mod:`netgraph.render.ids`.
    element_id: str | None = None
    #: ``URL`` attribute to emit; see :mod:`netgraph.render.links`.
    url: str | None = None
    #: Label colour; set only under a highlight, for the same reason as
    #: :attr:`_NodeView.fontcolor`.
    fontcolor: str | None = None
    #: Graphviz layout weight. Set only on a bundled edge, where it is the
    #: number of links folded in: four cables between two switches should pull
    #: them together four times as hard as one, which is what keeps a LAG short
    #: and straight instead of routed around the diagram.
    weight: str | None = None
    #: ``pos`` attribute: the route's Bézier control points. Only emitted for a
    #: fully-fixed drawing, where the no-op engine draws them as given; a
    #: partially-pinned one is re-routed around the nodes it just placed, and
    #: stale bends would cross them. Computed by :mod:`netgraph.layout.routing`
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


@dataclass(frozen=True, slots=True)
class _FrameView:
    """One namespace box drawn from stored geometry, and its caption.

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
    #: ``netgraph layout --engine`` sets it: seeding an arrangement is the one
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
    ``netgraph layout --write`` read it, so a box cannot be stored under one key
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
            "diagram and the bends are lost. Run 'netgraph layout --write' to place the rest"
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
                "and may stop short of the shape. Re-run 'netgraph layout --write' to "
                "record the sizes"
            )
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
            f"{default} like everything else. Run 'netgraph layout --write' to pin the "
            "arrangement, after which every link is routed as it asks"
        )
    if default is Routing.ORTHOGONAL and _has_edge_labels(graph, opts):
        said.append(
            "Graphviz cannot place an edge label on an orthogonal route, so the labels are "
            "emitted as 'xlabel' and floated near their links instead. Pin the arrangement "
            "with 'netgraph layout --write' to place them exactly"
        )
    return tuple(said)


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
    also puts the caption inside the drawing's bounding box for free.
    """
    if not frames:
        return None
    return " ".join(
        op for frame in frames for op in (f"c {_xdot_text(_CLUSTER_STROKE)}", frame.outline)
    )


def _dot_points(box: Box) -> str:
    """One rectangle as an xdot unfilled-polygon operation."""
    left, bottom, right, top = box.bounds
    corners = " ".join(
        f"{_coordinate(x)} {_coordinate(y)}"
        for x, y in ((left, bottom), (left, top), (right, top), (right, bottom))
    )
    return f"p 4 {corners}"


def _caption_pos(box: Box) -> str:
    """Where a frame's caption sits: centred, just *above* the top edge.

    Two decisions, both forced by what the caption is — an ordinary node.

    *Centred* rather than left-aligned the way ``labeljust=l`` puts a cluster's
    own label, because netgraph does not measure text: an approximate left edge
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
    return _pos(box.x, top + _CLUSTER_FONT_SIZE)


def _xdot_text(value: str) -> str:
    """An xdot counted string: the byte length, a dash, then the bytes."""
    return f"{len(value.encode('utf-8'))} -{value}"


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def to_dot(graph: Graph, options: RenderOptions | None = None, *, target: str = "dot") -> str:
    """Render ``graph`` as Graphviz DOT source.

    The graph is undirected (a cable has no direction, §7.1), so the output is a
    ``graph``, not a ``digraph``, and edges use ``--``.

    A graph carrying a stored arrangement (§18) emits it: every placed node gets
    a ``pos``, every placed link its spline control points, and — when the whole
    drawing is placed — the namespace boxes are drawn as a ``_background``,
    because the no-op layout engine that reproduces the arrangement does not
    draw clusters. See :func:`layout_plan` for how the document is then run.

    Args:
        target: The output format this DOT is destined for. Only icon
            selection depends on it — see :func:`netgraph.render.icons.suffix_order`
            — and the default suits DOT written out for someone else to lay out.
    """
    opts = options or RenderOptions()
    icons = _icon_files(graph, opts.icons, target=target)
    # The ids are computed whether or not they are *emitted*: they are how a
    # detail record is keyed, so the tooltips need them even when the document
    # itself stays anonymous. A rendering that wants neither pays for neither.
    identity = element_ids(graph) if opts.tooltips or opts.element_ids else ElementIds()
    details = build_details(graph, opts, ids=identity) if opts.tooltips else {}
    plan = layout_plan(graph)
    frames = _frames(graph, opts, plan) if plan.mode is LayoutMode.FIXED else ()
    template = _environment().get_template(_TEMPLATE_NAME)
    return template.render(
        title=opts.title,
        rankdir=opts.rankdir or DEFAULT_RANKDIR,
        # What the *engine* routes with. A fixed drawing carries an explicit
        # ``pos`` per link and this is inert; anywhere else it is the only place
        # a routing style can be expressed at all, so the inventory's default
        # goes here and a link asking for something different is reported by
        # :func:`routing_advisories` rather than silently ignored.
        splines=_SPLINES_ATTRIBUTE[default_routing(graph, opts)],
        groups=_groups(graph, opts, icons, identity, details, plan),
        edges=tuple(_edge_views(graph, opts, identity, details, plan)),
        imagepath=str(opts.icons.directory) if icons and opts.icons is not None else None,
        icon_width=_ICON_BOX[0],
        icon_height=_ICON_BOX[1],
        # ``inputscale`` tells neato that a pinned ``pos`` is in points rather
        # than inches. It is inert under ``-n2`` and inert under ``dot``, so it
        # is emitted whenever anything is placed: it costs nothing, and it makes
        # ``-f dot`` output that somebody lays out themselves honour the pins.
        inputscale=POINTS_PER_INCH if plan.mode is not LayoutMode.AUTO else None,
        frames=frames,
        background=_background(frames),
        frame_color=_CLUSTER_LABEL_COLOUR,
        frame_font_size=_CLUSTER_FONT_SIZE,
    )


def _icon_files(graph: Graph, theme: IconTheme | None, *, target: str) -> Mapping[str, str]:
    """The icon file name to draw each kind in ``graph`` with.

    Only the kinds actually present are resolved, so a theme is never asked for
    a picture the diagram has no use for, and a diagram of nothing but computers
    does not carry an ``imagepath`` for icons it never draws.
    """
    if theme is None:
        return {}
    kinds = sorted({node.kind for node in graph.nodes.values()})
    return theme.files(kinds, prefer=suffix_order(target))


def run_graphviz(
    source: str, *, format: str, plan: LayoutPlan | None = None, subject: str | None = None
) -> tuple[bytes, str]:
    """Run Graphviz over ``source`` and return ``(stdout, stderr)``.

    The single place a subprocess is started, so that every caller — the image
    renderers, and ``netgraph layout`` reading coordinates back out — reports a
    missing binary, a timeout and a non-zero exit the same way.

    Args:
        source: The DOT document, handed over stdin.
        format: The ``-T`` value. Any Graphviz output format, not only the
            image ones: ``netgraph layout`` asks for ``json``.
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
        # Reachable in two ways worth telling apart. ``NETGRAPH_DOT`` naming a
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
      coordinate system (:func:`~netgraph.layout.graphviz.realign`) and the
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
    icons = _icon_files(graph, theme, target=format)
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
    :attr:`~netgraph.layout.geometry.LayoutMode.FIXED` and whose stored nodes
    are still exactly where they were stored. ``netgraph layout --write`` and
    :func:`to_image` both go through it, which is what makes what is written the
    same as what is drawn.

    Raises:
        RenderError: Graphviz failed, or produced JSON that cannot be read.
    """
    plan = layout_plan(graph)
    if plan.mode is not LayoutMode.PARTIAL:
        return graph
    payload, _ = run_graphviz(
        to_dot(graph, options, target=target),
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

    A route computed by netgraph has to stop at the shape it runs into, and
    nothing in a stored arrangement says how big a shape is — Graphviz derives
    it from the label, which netgraph cannot measure. ``netgraph layout --write``
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
        to_dot(graph, options, target=target),
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
    Only the names netgraph itself emitted are substituted; anything else in the
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
        loader=PackageLoader("netgraph.render", "templates"),
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
    (:func:`~netgraph.render.details.printable`): DOT has no escape for them,
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
    icons: Mapping[str, str],
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    plan: LayoutPlan,
) -> tuple[_GroupView, ...]:
    """The node groups to draw: one per namespace, or a single loose group.

    A layer-3 subnet node reports the root namespace, so it stays outside every
    cluster — a prefix spanning two sites belongs to neither of them. The root
    namespace is never boxed either: drawing a frame labelled ``/`` around half
    the diagram helps nobody.

    A layer that groups its own nodes wins over ``--group-by-namespace``: the
    routing view boxes each VRF (§16.6), and that is the grouping the reader asked
    for by choosing the layer.
    """
    if graph.clusters:
        return _cluster_groups(graph, options, icons, identity, details, plan)

    if not options.group_by_namespace:
        nodes = _node_views(graph, graph.nodes.values(), options, icons, identity, details, plan)
        return (_GroupView(nodes=tuple(nodes)),)

    groups: list[_GroupView] = []
    for index, namespace in enumerate(graph.namespaces):
        members = graph.nodes_in(namespace)
        views = tuple(_node_views(graph, members, options, icons, identity, details, plan))
        if not namespace:
            groups.append(_GroupView(nodes=views))
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
    icons: Mapping[str, str],
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    plan: LayoutPlan,
) -> tuple[_GroupView, ...]:
    """One box per cluster the *layer* asked for, unboxed nodes first.

    Unboxed nodes lead so that a router straddling two instances is laid out
    between their boxes rather than after them, which is where it belongs — and
    so that a diagram with no VRF at all is identical to an ungrouped one.
    """
    loose = tuple(
        _node_views(
            graph,
            [node for node in graph.nodes.values() if not node.cluster],
            options,
            icons,
            identity,
            details,
            plan,
        )
    )
    groups: list[_GroupView] = [_GroupView(nodes=loose)] if loose else []
    for index, cluster in enumerate(graph.clusters):
        members = graph.nodes_in_cluster(cluster)
        groups.append(
            _GroupView(
                nodes=tuple(_node_views(graph, members, options, icons, identity, details, plan)),
                # Offset past the namespace clusters' numbering space so the two
                # groupings can never mint the same subgraph name.
                id=f"cluster_vrf_{index}",
                label=f"vrf {cluster}",
                tooltip=_cluster_tooltip(cluster, members, options, identity, details, kind="vrf"),
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
    icons: Mapping[str, str],
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
    plan: LayoutPlan,
) -> Iterator[_NodeView]:
    for node in nodes:
        placement = plan.geometry.nodes.get(node.fqn)
        shape, fill, stroke = _NODE_STYLE.get(node.kind, _DEFAULT_NODE_STYLE)
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
        image = icons.get(node.kind)
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
            shape="none" if image else shape,
            fill=fill,
            stroke=stroke,
            style="" if image else _diff_style(_node_style(node), mark),
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
            penwidth=emphasis.penwidth if emphasis is not None else None,
            fontcolor=emphasis.fontcolor if emphasis is not None else None,
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


#: How each :class:`~netgraph.render.diffview.Mark` is drawn. An untouched thing
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
    return f"[{node.kind}]"


def _node_style(node: Node) -> str | None:
    """The ``style`` override for a node, or ``None`` to inherit ``filled``."""
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
# Edges
# --------------------------------------------------------------------------- #


def _edge_views(
    graph: Graph,
    options: RenderOptions,
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
        colour, style = _MEDIUM_STYLE.get(edge.medium, _DEFAULT_MEDIUM_STYLE)
        if edge.kind is EdgeKind.ATTACHMENT:
            colour, style = _ATTACHMENT_STYLE
        elif edge.kind is EdgeKind.SUBNET:
            colour, style = _SUBNET_EDGE_STYLE
        elif edge.kind is EdgeKind.ENCAPSULATION:
            colour, style = _ENCAPSULATION_STYLE
        elif edge.kind is EdgeKind.BGP:
            colour, style = _BGP_STYLE
        elif edge.kind is EdgeKind.OSPF:
            colour, style = _OSPF_STYLE
        elif edge.kind is EdgeKind.OUTLET:
            colour, style = _OUTLET_STYLE
        elif edge.kind is EdgeKind.POE:
            colour, style = _POE_STYLE
        elif edge.kind is EdgeKind.MEMBERSHIP:
            colour, style = _MEMBERSHIP_STYLE
        elif edge.kind is EdgeKind.TUNNEL:
            colour, style = (
                _TUNNEL_STYLE
                if edge.tunnel is None or edge.tunnel.protected
                else _CLEARTEXT_TUNNEL_STYLE
            )
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
            penwidth=emphasis.penwidth if emphasis is not None else _penwidth(edge.speed),
            label=None if floating else label,
            xlabel=label if floating else None,
            tooltip=detail_text(record) if record is not None else None,
            element_id=element if options.element_ids else None,
            url=_edge_url(graph, edge, options.link_template),
            fontcolor=emphasis.fontcolor if emphasis is not None else None,
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
