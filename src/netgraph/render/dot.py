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
import re
import shutil
import subprocess
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final

from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup

from netgraph.errors import RenderError, clip_text, compact_ids, count_text
from netgraph.loader.inventory import namespace_of
from netgraph.render.aggregate import AGGREGATE_KIND, AggregateView
from netgraph.render.details import (
    build_details,
    detail_text,
    namespace_text,
    plain_text,
    printable,
)
from netgraph.render.graph import (
    PATCHPANEL_KIND,
    RACK_KIND,
    SUBNET_KIND,
    TUNNEL_KIND,
    Edge,
    EdgeKind,
    Graph,
    Layer,
    Node,
    RackView,
    TunnelView,
)
from netgraph.render.highlight import Highlight
from netgraph.render.icons import IconTheme, suffix_order
from netgraph.render.ids import ElementIds, element_ids
from netgraph.render.links import LinkTemplate
from netgraph.render.options import DEFAULT_RANKDIR, RenderOptions

__all__ = [
    "DOT_EXECUTABLE",
    "IMAGE_FORMATS",
    "render_dot",
    "render_image",
    "to_dot",
    "to_image",
]

#: Formats :func:`to_image` can produce by running Graphviz.
IMAGE_FORMATS: Final[tuple[str, ...]] = ("svg", "png", "pdf")

#: The layout program shelled out to. Looked up on ``PATH``; never run through a
#: shell, and always with an argument list, so nothing in an inventory can reach
#: the command line.
DOT_EXECUTABLE: Final = "dot"

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
# Entry points
# --------------------------------------------------------------------------- #


def to_dot(graph: Graph, options: RenderOptions | None = None, *, target: str = "dot") -> str:
    """Render ``graph`` as Graphviz DOT source.

    The graph is undirected (a cable has no direction, §7.1), so the output is a
    ``graph``, not a ``digraph``, and edges use ``--``.

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
    template = _environment().get_template(_TEMPLATE_NAME)
    return template.render(
        title=opts.title,
        rankdir=opts.rankdir or DEFAULT_RANKDIR,
        groups=_groups(graph, opts, icons, identity, details),
        edges=tuple(_edge_views(graph, opts, identity, details)),
        imagepath=str(opts.icons.directory) if icons and opts.icons is not None else None,
        icon_width=_ICON_BOX[0],
        icon_height=_ICON_BOX[1],
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


def to_image(graph: Graph, options: RenderOptions | None = None, *, format: str) -> bytes:
    """Lay ``graph`` out by running Graphviz, and return the encoded image.

    Args:
        format: One of :data:`IMAGE_FORMATS`.

    Raises:
        RenderError: ``format`` is not an image format, the Graphviz ``dot``
            executable is not installed, or the layout failed or timed out.
    """
    if format not in IMAGE_FORMATS:
        supported = ", ".join(IMAGE_FORMATS)
        raise RenderError(f"{format!r} is not a Graphviz image format; expected one of {supported}")

    executable = shutil.which(DOT_EXECUTABLE)
    if executable is None:
        raise RenderError(
            f"cannot render {format}: the Graphviz {DOT_EXECUTABLE!r} executable was not found "
            "on PATH. Install Graphviz (Debian/Ubuntu: 'apt install graphviz', "
            "macOS: 'brew install graphviz', Windows: 'winget install Graphviz.Graphviz'), "
            "or render with '--format dot' and convert the file separately."
        )

    opts = options or RenderOptions()
    source = to_dot(graph, opts, target=format)
    theme = opts.icons
    icons = _icon_files(graph, theme, target=format)
    try:
        # Fixed argv, no shell, and the DOT source goes over stdin: nothing from
        # an inventory ever becomes a command-line argument or a shell word.
        completed = subprocess.run(
            [executable, f"-T{format}"],
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
        raise RenderError(f"cannot render {format}: could not run {executable!r}: {exc}") from exc

    if completed.returncode != 0:
        detail = _decode(completed.stderr) or f"{DOT_EXECUTABLE} exited with {completed.returncode}"
        raise RenderError(f"Graphviz failed to render {format}: {detail}")
    if not completed.stdout:
        detail = _decode(completed.stderr) or "no diagnostic was reported"
        raise RenderError(f"Graphviz produced no {format} output: {detail}")
    payload = completed.stdout
    if icons:
        # An unreadable icon is the one warning worth escalating: Graphviz
        # succeeds and simply leaves the picture out, so the user would get a
        # diagram of empty labels and no explanation.
        _check_icons_loaded(_decode(completed.stderr), format=format)
        if format == "svg" and theme is not None:
            payload = _embed_icons(payload, theme, icons)
    if format == "svg" and opts.tooltips:
        payload = _promote_tooltips(payload)
    # dot reports non-fatal layout warnings on stderr with a zero exit status;
    # those are not this renderer's to escalate.
    return payload


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
) -> tuple[_GroupView, ...]:
    """The node groups to draw: one per namespace, or a single loose group.

    A layer-3 subnet node reports the root namespace, so it stays outside every
    cluster — a prefix spanning two sites belongs to neither of them. The root
    namespace is never boxed either: drawing a frame labelled ``/`` around half
    the diagram helps nobody.
    """
    if not options.group_by_namespace:
        nodes = _node_views(graph, graph.nodes.values(), options, icons, identity, details)
        return (_GroupView(nodes=tuple(nodes)),)

    groups: list[_GroupView] = []
    for index, namespace in enumerate(graph.namespaces):
        members = graph.nodes_in(namespace)
        views = tuple(_node_views(graph, members, options, icons, identity, details))
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


def _cluster_tooltip(
    namespace: str,
    members: Iterable[Node],
    options: RenderOptions,
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
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
    return namespace_text(namespace, records)


def _node_views(
    graph: Graph,
    nodes: Iterable[Node],
    options: RenderOptions,
    icons: Mapping[str, str],
    identity: ElementIds,
    details: Mapping[str, Mapping[str, object]],
) -> Iterator[_NodeView]:
    for node in nodes:
        shape, fill, stroke = _NODE_STYLE.get(node.kind, _DEFAULT_NODE_STYLE)
        emphasis = _node_emphasis(node, options.highlight)
        if emphasis is not None:
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
            style="" if image else _node_style(node),
            # A tooltip is a DOT string, not HTML, so its line breaks are
            # meaningful and are kept.
            tooltip=detail_text(record) if record is not None else None,
            rows=_node_rows(node, options, layer=graph.layer),
            image=image,
            element_id=element if options.element_ids else None,
            url=_node_url(graph, node, options.link_template),
            penwidth=emphasis.penwidth if emphasis is not None else None,
            fontcolor=emphasis.fontcolor if emphasis is not None else None,
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


def _node_url(graph: Graph, node: Node, template: LinkTemplate | None) -> str | None:
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


def _expand(template: LinkTemplate, graph: Graph, fqn: str, *, kind: str) -> str | None:
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
        return f"[{node.subnet.family} subnet]"
    if node.tunnel is not None:
        return f"[{node.tunnel.type} tunnel]"
    if node.aggregate is not None:
        # Not "[namespace]": the reader needs to know at a glance that the box
        # is a stand-in, and the number is the one fact a shape cannot carry.
        return f"[namespace, {count_text(node.aggregate.size, 'element')}]"
    if node.rack is not None:
        used = f"{node.rack.used_units}/{node.rack.height}U used"
        return f"[rack, {used}]" if not node.rack.inferred_height else f"[rack, {used}, inferred]"
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
) -> Iterator[_EdgeView]:
    for index, edge in enumerate(graph.edges):
        colour, style = _MEDIUM_STYLE.get(edge.medium, _DEFAULT_MEDIUM_STYLE)
        if edge.kind is EdgeKind.ATTACHMENT:
            colour, style = _ATTACHMENT_STYLE
        elif edge.kind is EdgeKind.SUBNET:
            colour, style = _SUBNET_EDGE_STYLE
        elif edge.kind is EdgeKind.ENCAPSULATION:
            colour, style = _ENCAPSULATION_STYLE
        elif edge.kind is EdgeKind.TUNNEL:
            colour, style = (
                _TUNNEL_STYLE
                if edge.tunnel is None or edge.tunnel.protected
                else _CLEARTEXT_TUNNEL_STYLE
            )
        emphasis = _edge_emphasis(edge, options.highlight)
        if emphasis is not None:
            colour = emphasis.stroke
        element = identity.edge(index)
        record = details.get(element) if element is not None else None
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
            label=_edge_label(edge, graph.layer, options) or None,
            tooltip=detail_text(record) if record is not None else None,
            element_id=element if options.element_ids else None,
            url=_edge_url(graph, edge, options.link_template),
            fontcolor=emphasis.fontcolor if emphasis is not None else None,
            weight=str(edge.bundle.size) if edge.bundle is not None else None,
        )


def _edge_url(graph: Graph, edge: Edge, template: LinkTemplate | None) -> str | None:
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

    if layer is Layer.L2:
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
                vlans=f"[{slot.kind}]"
                if not continuation
                else (f"{slot.height}U" if unit == slot.top else ""),
            )
        )
    return tuple(rows)


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
