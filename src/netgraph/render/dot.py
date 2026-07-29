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
* **DOT quoted string** — node ids, tooltips, cluster labels and edge labels.
  These go through the ``dot_string`` filter (:func:`_dot_string`), which adds
  the quotes and the backslash escapes and marks the result safe.

The template header spells out which to use where. Omitting the filter drops
the surrounding quotes and Graphviz rejects the file, so the failure mode is
loud rather than silent.

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

from netgraph.errors import RenderError, clip_text
from netgraph.render.graph import SUBNET_KIND, Edge, EdgeKind, Graph, Layer, Node, Subnet
from netgraph.render.icons import IconTheme, suffix_order
from netgraph.render.options import RenderOptions

__all__ = [
    "DOT_EXECUTABLE",
    "IMAGE_FORMATS",
    "edge_element_id",
    "node_element_id",
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
    SUBNET_KIND: ("box", "#e0f2f1", "#0f766e"),
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


def node_element_id(index: int) -> str:
    """The ``id`` the ``index``-th node of a graph is drawn with.

    Graphviz copies a node's ``id`` attribute into the element it emits, so
    this is also the ``id`` on the ``<g class="node">`` of an SVG rendering —
    which is what lets a front end map a shape under the cursor back onto the
    node it stands for. Positions come from iterating
    :attr:`~netgraph.render.graph.Graph.nodes`, which is ordered, so the same
    graph always produces the same ids.

    The identity is deliberately *not* the fully-qualified name: that may hold
    characters an XML ``id`` may not, and a diagram published somewhere would
    then carry the inventory's names in a second, unescaped place.
    """
    return f"n{index}"


def edge_element_id(index: int) -> str:
    """The ``id`` the ``index``-th edge of a graph is drawn with.

    The counterpart of :func:`node_element_id`. Edges need one more than nodes
    do: two elements may be joined by several cables, and an SVG edge names
    only its endpoints, so without an id the parallel links are
    indistinguishable.
    """
    return f"e{index}"


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
    #: See :func:`node_element_id`.
    element_id: str | None = None


@dataclass(frozen=True, slots=True)
class _EdgeView:
    source: str
    target: str
    color: str
    style: str
    tooltip: str
    penwidth: str | None = None
    label: str | None = None
    #: ``id`` attribute to emit; see :func:`edge_element_id`.
    element_id: str | None = None


@dataclass(frozen=True, slots=True)
class _GroupView:
    """A ``cluster_*`` subgraph, or — with :attr:`id` unset — loose nodes."""

    nodes: tuple[_NodeView, ...] = field(default_factory=tuple)
    id: str | None = None
    label: str | None = None


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def to_dot(
    graph: Graph,
    options: RenderOptions | None = None,
    *,
    target: str = "dot",
    element_ids: bool = False,
) -> str:
    """Render ``graph`` as Graphviz DOT source.

    The graph is undirected (a cable has no direction, §7.1), so the output is a
    ``graph``, not a ``digraph``, and edges use ``--``.

    Args:
        target: The output format this DOT is destined for. Only icon
            selection depends on it — see :func:`netgraph.render.icons.suffix_order`
            — and the default suits DOT written out for someone else to lay out.
        element_ids: Give every node and edge an ``id`` attribute
            (:func:`node_element_id`, :func:`edge_element_id`). Off by default,
            because a hand-read DOT file is better without them; a front end
            that has to find the node under a mouse pointer turns them on.
    """
    opts = options or RenderOptions()
    icons = _icon_files(graph, opts.icons, target=target)
    template = _environment().get_template(_TEMPLATE_NAME)
    return template.render(
        title=opts.title,
        groups=_groups(graph, opts, icons, element_ids=element_ids),
        edges=tuple(_edge_views(graph, opts, element_ids=element_ids)),
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


def to_image(
    graph: Graph,
    options: RenderOptions | None = None,
    *,
    format: str,
    element_ids: bool = False,
) -> bytes:
    """Lay ``graph`` out by running Graphviz, and return the encoded image.

    Args:
        format: One of :data:`IMAGE_FORMATS`.
        element_ids: Passed to :func:`to_dot`. Graphviz copies the ids into the
            elements of an SVG rendering, so this is how a rendering becomes
            addressable from a browser.

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

    source = to_dot(graph, options, target=format, element_ids=element_ids)
    theme = (options or RenderOptions()).icons
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
    if icons:
        # An unreadable icon is the one warning worth escalating: Graphviz
        # succeeds and simply leaves the picture out, so the user would get a
        # diagram of empty labels and no explanation.
        _check_icons_loaded(_decode(completed.stderr), format=format)
        if format == "svg" and theme is not None:
            return _embed_icons(completed.stdout, theme, icons)
    # dot reports non-fatal layout warnings on stderr with a zero exit status;
    # those are not this renderer's to escalate.
    return completed.stdout


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
    """
    escaped = (
        str(value)
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
    *,
    element_ids: bool = False,
) -> tuple[_GroupView, ...]:
    """The node groups to draw: one per namespace, or a single loose group.

    A layer-3 subnet node reports the root namespace, so it stays outside every
    cluster — a prefix spanning two sites belongs to neither of them. The root
    namespace is never boxed either: drawing a frame labelled ``/`` around half
    the diagram helps nobody.
    """
    # Identity is assigned from the graph's own node order, not from the order
    # the nodes are drawn in: grouping by namespace reshuffles the second, and
    # a consumer of the ids resolves them against the graph, not the picture.
    ids = (
        {fqn: node_element_id(index) for index, fqn in enumerate(graph.nodes)}
        if element_ids
        else {}
    )

    if not options.group_by_namespace:
        nodes = _node_views(graph.nodes.values(), options, graph.layer, icons, ids)
        return (_GroupView(nodes=tuple(nodes)),)

    groups: list[_GroupView] = []
    for index, namespace in enumerate(graph.namespaces):
        members = tuple(_node_views(graph.nodes_in(namespace), options, graph.layer, icons, ids))
        if namespace:
            groups.append(_GroupView(nodes=members, id=f"cluster_{index}", label=namespace))
        else:
            groups.append(_GroupView(nodes=members))
    return tuple(groups)


def _node_views(
    nodes: Iterable[Node],
    options: RenderOptions,
    layer: Layer,
    icons: Mapping[str, str],
    #: ``fqn -> id``; empty when the rendering carries no ids.
    ids: Mapping[str, str],
) -> Iterator[_NodeView]:
    for node in nodes:
        shape, fill, stroke = _NODE_STYLE.get(node.kind, _DEFAULT_NODE_STYLE)
        subnet = node.subnet
        image = icons.get(node.kind)
        yield _NodeView(
            id=node.fqn,
            title=_inline(node.name),
            subtitle=f"[{subnet.family} subnet]" if subnet is not None else f"[{node.kind}]",
            # An icon *is* the glyph, so the shape it would sit inside is taken
            # away rather than drawn around it. The palette stays on the view
            # because the icon carries the same colours: a theme without a
            # picture for this kind falls back to the shape, in one diagram.
            shape="none" if image else shape,
            fill=fill,
            stroke=stroke,
            style="" if image else _node_style(node),
            # The description is a DOT string, not HTML, so its line breaks are
            # meaningful and are kept.
            tooltip=_subnet_tooltip(subnet) if subnet is not None else node.description,
            rows=_node_rows(node, options, layer),
            image=image,
            element_id=ids.get(node.fqn),
        )


def _node_style(node: Node) -> str | None:
    """The ``style`` override for a node, or ``None`` to inherit ``filled``."""
    if node.is_subnet:
        # Rounded, for something derived rather than declared: the reader should
        # not go looking for a device with this name.
        return "filled,rounded"
    if node.kind == "adapter":
        # §8.2: an adapter is hardware that may be collapsed into its host, so it
        # is drawn as a provisional part of the diagram.
        return "filled,dashed"
    return None


def _node_rows(node: Node, options: RenderOptions, layer: Layer) -> tuple[_Row, ...]:
    """One row per interface that has an address or a VLAN worth printing.

    An interface with neither is left out: on a 48-port switch those rows would
    be a column of port names burying the two ports that carry an address, and
    the ports a cable lands on are named on the edge already.
    """
    if node.subnet is not None:
        if options.show_vlans and node.vlans:
            return (_Row(port=f"vlan {_compact_ids(node.vlans)}", spans=True),)
        return ()

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
        vlans = f"vlan {_compact_ids(port.vlans)}" if options.show_vlans and port.vlans else ""
        if not addresses and not vlans:
            continue
        rows.append(_Row(port=_inline(port.name), addresses=addresses, vlans=vlans))

    if len(rows) > _MAX_PORT_ROWS:
        hidden = len(rows) - _MAX_PORT_ROWS
        rows = [
            *rows[:_MAX_PORT_ROWS],
            _Row(port=f"(+{_count(hidden, 'more interface')})", spans=True),
        ]
    return tuple(rows)


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #


def _edge_views(
    graph: Graph, options: RenderOptions, *, element_ids: bool = False
) -> Iterator[_EdgeView]:
    for index, edge in enumerate(graph.edges):
        colour, style = _MEDIUM_STYLE.get(edge.medium, _DEFAULT_MEDIUM_STYLE)
        if edge.kind is EdgeKind.ATTACHMENT:
            colour, style = _ATTACHMENT_STYLE
        elif edge.kind is EdgeKind.SUBNET:
            colour, style = _SUBNET_EDGE_STYLE
        yield _EdgeView(
            source=edge.source,
            target=edge.target,
            color=colour,
            style=style,
            penwidth=_penwidth(edge.speed),
            label=_edge_label(edge, graph.layer, options) or None,
            tooltip=_edge_tooltip(edge, options),
            element_id=edge_element_id(index) if element_ids else None,
        )


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
    parts: list[str] = []
    ports = _port_pair(edge)
    if ports:
        parts.append(ports)

    if edge.kind is EdgeKind.SUBNET:
        if options.show_ips and edge.addresses:
            parts.extend(_address_lines(edge.addresses, options.max_addresses))
        if options.show_vlans and edge.vlans:
            parts.append(f"vlan {_compact_ids(edge.vlans)}")
        return "\n".join(parts)

    if layer is Layer.L2:
        if options.show_vlans and edge.vlans:
            parts.append(f"vlan {_compact_ids(edge.vlans)}")
        elif edge.kind is EdgeKind.ATTACHMENT and edge.label:
            parts.append(edge.label)
        return "\n".join(parts)

    if edge.label:
        parts.append(edge.label)
    if edge.kind is EdgeKind.CABLE and edge.medium != "copper":
        parts.append(edge.medium)
    speed = edge.speed_text
    if speed:
        parts.append(speed)
    if options.show_vlans and edge.vlans and edge.kind is EdgeKind.CABLE:
        parts.append(f"vlan {_compact_ids(edge.vlans)}")
    return "\n".join(parts)


def _edge_tooltip(edge: Edge, options: RenderOptions) -> str:
    """The full physical record of a link, shown on hover.

    The tooltip is where the detail the label had no room for lives — but
    ``--no-show-vlans`` means "do not annotate this diagram with VLANs", not
    "hide them until the reader hovers", so the display flags apply here too.
    """
    if edge.kind is EdgeKind.SUBNET:
        # A membership has no physical record at all; what a reader wants on
        # hover is which port of which element the address sits on.
        parts = [f"{edge.source}:{edge.source_port}"]
        if edge.addresses:
            parts.append(", ".join(edge.addresses))
        if options.show_vlans and edge.vlans:
            parts.append(f"vlans: {_compact_ids(edge.vlans)}")
        return " — ".join(parts)

    parts = [f"{edge.kind}: {edge.name}", f"medium: {edge.medium}"]
    speed = edge.speed_text
    if speed:
        parts.append(f"speed: {speed}")
    if edge.length_m is not None:
        parts.append(f"length: {_number(edge.length_m)} m")
    if options.show_vlans and edge.vlans:
        parts.append(f"vlans: {_compact_ids(edge.vlans)}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Shared formatting
# --------------------------------------------------------------------------- #


def _subnet_tooltip(subnet: Subnet) -> str:
    """How populated the prefix is — the number a single-member subnet gives away."""
    return (
        f"{subnet.family} subnet {subnet.prefix}: "
        f"{_count(len(subnet.elements), 'element')}, "
        f"{_count(len(subnet.addresses), 'address', 'addresses')}"
    )


def _address_lines(addresses: Sequence[str], limit: int) -> list[str]:
    if len(addresses) <= limit:
        return list(addresses)
    remaining = len(addresses) - limit
    return [*addresses[:limit], f"(+{remaining} more)"]


def _compact_ids(ids: Iterable[int]) -> str:
    """Render VLAN ids as coalesced ranges: ``10,20,100-110``."""
    ordered = sorted(set(ids))
    if not ordered:
        return ""
    ranges: list[tuple[int, int]] = []
    for value in ordered:
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], value)
        else:
            ranges.append((value, value))
    return ",".join(str(low) if low == high else f"{low}-{high}" for low, high in ranges)


def _count(number: int, noun: str, plural: str | None = None) -> str:
    """``1 element`` / ``3 elements``, with an explicit plural where needed."""
    return f"{number} {noun}" if number == 1 else f"{number} {plural or noun + 's'}"


def _number(value: float) -> str:
    """Drop the trailing ``.0`` of a whole-number float."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _inline(text: str) -> str:
    """Collapse whitespace, so a value cannot break a table row across lines.

    Element and interface names cannot contain a newline (§2 name grammar), but
    the renderer must not depend on a validator that a later refactor could move
    or relax — the same reason node ids are quoted rather than trusted.
    """
    return " ".join(text.split())
