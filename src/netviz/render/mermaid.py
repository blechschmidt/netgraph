"""Mermaid ``flowchart`` exporter.

Mermaid is what makes a topology reviewable inside a pull request: GitHub,
GitLab and most documentation sites render a fenced ``mermaid`` block inline, so
a diagram travels with the YAML that produced it and needs no Graphviz on the
reader's machine.

Two constraints shape the output. Node identifiers must be alphanumeric, so the
fully-qualified name becomes a positional ``n0``, ``n1`` … id with the real name
in the label. And Mermaid has no escape syntax inside labels, only HTML entities
— :func:`_label` converts the handful of characters that would otherwise end the
label early.

A third constraint decides what this backend will *not* do. A Mermaid flowchart
node is a caption: it has no rows, no columns and no way to say "these units are
empty". A rack elevation is exactly those things, so ``--layer rack`` is
refused here with a :class:`~netviz.errors.RenderError` naming the formats
that can draw one, rather than emitted as a box that quietly leaves out the
free space — which is half of what an elevation is for.

Annotations degrade, and say so
-------------------------------

Mermaid has no vocabulary for most of §21, so the three kinds are drawn as
closely as a flowchart allows and every gap is stated in the output rather than
left for a reader to notice:

* an **area** with members becomes a ``subgraph``, which is Mermaid's only
  container. It gets the area's label, and loses its colour, its border style
  and its padding — a Mermaid subgraph has no style of its own. An area that is
  a *rectangle of canvas* rather than a set of elements has nothing to become,
  since Mermaid places nothing, and is dropped.
* a **note** becomes an ordinary node with a note-like ``classDef`` and, when it
  is anchored, a dotted link to what it is about. Mermaid has no note shape and
  no free-floating text, so it is a box among the boxes; the parsed emphasis of
  §21.1 is flattened to its text, because Mermaid label markup is a different
  language again.
* a **legend** is not expressible at all — there is no construct for a keyed
  table that is not part of the graph.

Everything dropped is named in a ``%%`` comment at the foot of the diagram. A
Mermaid comment is invisible in the rendered picture and plain in the source,
which is the right side of the trade: the reader of the *diagram* is not
distracted by a limitation they cannot act on, and the reader of the *document*
— who is usually the person wondering where their legend went — is told
directly.
"""

from __future__ import annotations

from collections.abc import Container, Iterable, Iterator, Mapping
from typing import Final

from netviz.errors import RenderError, count_text
from netviz.models import format_watts
from netviz.render.aggregate import AGGREGATE_KIND, AggregateView
from netviz.render.annotations import (
    DEFAULT_NOTE_FILL,
    AnnotationViews,
    annotation_views,
    darken,
)
from netviz.render.graph import (
    GROUP_KIND,
    PATCHPANEL_KIND,
    SUBNET_KIND,
    TUNNEL_KIND,
    USER_KIND,
    ZONE_KIND,
    Edge,
    EdgeKind,
    Graph,
    Layer,
    Node,
    TunnelView,
)
from netviz.render.options import DEFAULT_RANKDIR, RenderOptions
from netviz.render.palette import CLUSTER_NOUN

__all__ = ["MERMAID_MAX_EDGES", "mermaid_advisories", "render_mermaid", "to_mermaid"]

#: Edges Mermaid's renderer draws before it refuses the diagram outright.
#:
#: The ceiling belongs to the *renderer*, not to the syntax: the document that
#: trips it is well-formed, and ``maxEdges`` is a secure config that a diagram
#: is deliberately not allowed to raise for itself. GitHub, GitLab and
#: ``mmdc`` all render with the default, so nothing this module emits can make
#: a larger graph display. Exported so the CLI can warn on the way out — see
#: entry 4 of ``docs/follow-ups.md``.
MERMAID_MAX_EDGES: Final = 500

#: Node shape delimiters per element kind. Mermaid encodes shape in the
#: brackets around the label, so each entry is an (open, close) pair. A subnet
#: is a rounded rectangle: close to the switch box a reader already knows, but
#: unmistakably not one of the hardware shapes.
_NODE_SHAPE: Final[Mapping[str, tuple[str, str]]] = {
    "router": ("([", "])"),
    # ``{…}`` is Mermaid's rhombus: the decision shape, and a firewall is the
    # one box on the diagram whose whole job is deciding.
    "firewall": ("{", "}"),
    "switch": ("[", "]"),
    "hub": ("{{", "}}"),
    "computer": ("[/", "/]"),
    "server": ("[(", ")]"),
    "adapter": (">", "]"),
    # ``[/…\\]`` is taken by the aggregate, so a panel takes the one remaining
    # neutral frame: a plain box in a grey that no active kind uses.
    PATCHPANEL_KIND: ("[", "]"),
    SUBNET_KIND: ("(", ")"),
    # ``[[…]]`` is Mermaid's subroutine box: a thing the diagram delegates to,
    # which is what a tunnel is to the path underneath it.
    TUNNEL_KIND: ("[[", "]]"),
    # A trapezoid: the one shape Mermaid has left once the eight kinds above
    # have taken theirs, and its widening base reads as "several things under
    # one heading", which is what a collapsed namespace is.
    AGGREGATE_KIND: ("[/", "\\]"),
    # ``(( ))`` is Mermaid's circle: the organisation-chart shape, and the one
    # frame no network kind above claimed.
    USER_KIND: ("((", "))"),
    # ``[\…/]`` is the inverted trapezoid, the last frame Mermaid has left and
    # the mirror of the aggregate's: several things narrowing into one heading,
    # which is what a group is.
    GROUP_KIND: ("[\\", "/]"),
    # A zone is a region rather than a thing, so it takes the plainest frame
    # Mermaid has left: the square box every kind above declined.
    ZONE_KIND: ("[", "]"),
}
_DEFAULT_SHAPE: Final[tuple[str, str]] = ("[", "]")

#: Per-kind fill, applied through ``classDef`` so the styling stays readable.
_CLASS_STYLE: Final[Mapping[str, str]] = {
    "router": "fill:#dbe9f6,stroke:#2563eb,stroke-width:1px",
    "firewall": "fill:#fde8e8,stroke:#dc2626,stroke-width:1px",
    "switch": "fill:#dcf0dc,stroke:#16a34a,stroke-width:1px",
    "hub": "fill:#f0e6d2,stroke:#a16207,stroke-width:1px",
    "computer": "fill:#f5f5f5,stroke:#6b7280,stroke-width:1px",
    "server": "fill:#eae2f5,stroke:#7c3aed,stroke-width:1px",
    "adapter": "fill:#fdf0e3,stroke:#ea580c,stroke-width:1px,stroke-dasharray:4 3",
    PATCHPANEL_KIND: "fill:#eef2f7,stroke:#64748b,stroke-width:1px",
    SUBNET_KIND: "fill:#e0f2f1,stroke:#0f766e,stroke-width:1px",
    TUNNEL_KIND: "fill:#ede9fe,stroke:#6d28d9,stroke-width:1px,stroke-dasharray:4 3",
    AGGREGATE_KIND: "fill:#e2e8f0,stroke:#475569,stroke-width:2px",
    USER_KIND: "fill:#fce7f3,stroke:#be185d,stroke-width:1px",
    GROUP_KIND: "fill:#fbcfe8,stroke:#9d174d,stroke-width:2px",
    ZONE_KIND: "fill:#fef2f2,stroke:#dc2626,stroke-width:1px",
}

#: Link syntax per edge style: solid, thick (fibre) and dotted (attachment).
_SOLID: Final = ("---", "-- {label} ---")
_THICK: Final = ("===", "== {label} ===")
_DOTTED: Final = ("-.-", "-. {label} .-")

#: Edge kinds with no physical line to encode: they take the dotted style and
#: rely on their label. See :func:`_edges`. A BGP session is deliberately *not*
#: one of them — it is drawn solid, as it is in DOT, because the whole point of
#: the routing view is telling a configured session from a discovered adjacency.
#:
#: ``poe`` is dashed for the same reason a tunnel is: the power rides on a run the
#: diagram draws elsewhere. An ``outlet`` feed is a cord of its own and stays
#: solid. An ``allocation`` carries nothing at all — it says one block was
#: carved out of another — so it is dashed here as it is in DOT.
_LOGICAL_EDGE_KINDS: Final[frozenset[EdgeKind]] = frozenset(
    {
        EdgeKind.TUNNEL,
        EdgeKind.ENCAPSULATION,
        EdgeKind.OSPF,
        EdgeKind.POE,
        EdgeKind.ALLOCATION,
    }
)

_INDENT: Final = "    "

#: The class every note is drawn with, and its style. Mermaid has no note shape,
#: so the pale fill and the dashed edge are the whole of what says "this is
#: commentary rather than a device" — which is why they are stated here even
#: though a plain box would parse just as well.
_NOTE_CLASS: Final = "netvizNote"
_NOTE_COLOURS: Final[tuple[str, str]] = (DEFAULT_NOTE_FILL, darken(DEFAULT_NOTE_FILL))
_NOTE_STYLE: Final = (
    f"fill:{_NOTE_COLOURS[0]},stroke:{_NOTE_COLOURS[1]},stroke-width:1px,stroke-dasharray:3 2"
)


def _refuse_rack(graph: Graph) -> None:
    """``--layer rack`` has no Mermaid form; say so, and say what does.

    Raises:
        RenderError: The graph was built for :attr:`Layer.RACK`.
    """
    if graph.layer is not Layer.RACK:
        return
    # Imported here: the registry imports this module to build itself, so
    # asking it a question at module scope would close the loop.
    from netviz.render.registry import rack_formats

    raise RenderError(
        "mermaid cannot draw a rack elevation: a flowchart node is a caption, with no rows "
        "to put the units in and no way to show an empty one. Render '--layer rack' as "
        + ", ".join(rack_formats())
        + ", or drop '--layer rack' to draw the topology instead"
    )


def to_mermaid(graph: Graph, options: RenderOptions | None = None) -> str:
    """Render ``graph`` as a Mermaid ``flowchart`` definition.

    The result is the bare definition, without the surrounding ```` ```mermaid ````
    fence, so it can be embedded in Markdown or fed to ``mmdc`` unchanged.

    Raises:
        RenderError: ``graph`` is a rack elevation. See the module docstring.
    """
    _refuse_rack(graph)
    opts = options or RenderOptions()
    ids = _identifiers(graph)
    views = annotation_views(graph, opts)
    # An area is a ``subgraph``, and so is a namespace, and a node may be in only
    # one of them: the explicit annotation wins. See
    # ``netviz.render.dot._area_groups`` for the whole precedence rule, which
    # every backend obeys.
    boxed = views.clustered

    lines: list[str] = []
    if opts.title:
        # A YAML front-matter block is Mermaid's own way of carrying a title.
        lines.extend(["---", f"title: {_front_matter(opts.title)}", "---"])
    direction = opts.rankdir or DEFAULT_RANKDIR
    lines.append(f"flowchart {direction}")

    lines.extend(_area_subgraphs(graph, views, ids, opts))
    if graph.clusters:
        # A layer that groups its own nodes wins over ``--group-by-namespace``;
        # see ``netviz.render.dot._groups``.
        lines.extend(_clustered_nodes(graph, ids, opts, skip=boxed))
    elif opts.group_by_namespace:
        lines.extend(_grouped_nodes(graph, ids, opts, skip=boxed))
    else:
        lines.extend(
            f"{_INDENT}{line}"
            for line in _nodes(
                [node for node in graph.nodes.values() if node.fqn not in boxed],
                ids,
                opts,
                graph.layer,
            )
        )

    lines.extend(f"{_INDENT}{line}" for line in _note_nodes(views))

    if graph.edges or views.notes:
        lines.append("")
        lines.extend(f"{_INDENT}{line}" for line in _edges(graph, ids, opts))
        lines.extend(f"{_INDENT}{line}" for line in _leaders(views, ids))

    lines.extend(_class_definitions(graph, ids))
    lines.extend(_note_styles(views))
    lines.extend(_degradations(views))
    return "\n".join(lines) + "\n"


def _identifiers(graph: Graph) -> Mapping[str, str]:
    """Positional, syntax-safe ids in graph order: ``n0``, ``n1``, …"""
    return {fqn: f"n{index}" for index, fqn in enumerate(graph.nodes)}


def _grouped_nodes(
    graph: Graph,
    ids: Mapping[str, str],
    options: RenderOptions,
    *,
    skip: Container[str] = frozenset(),
) -> Iterator[str]:
    for index, namespace in enumerate(graph.namespaces):
        members = [node for node in graph.nodes_in(namespace) if node.fqn not in skip]
        if not members:
            # Everything here is inside an annotation area instead, and an empty
            # subgraph is a labelled box round nothing.
            continue
        if not namespace:
            # Root-level elements, and every layer-3 subnet: a prefix spanning
            # two sites belongs inside neither site's box.
            yield from (f"{_INDENT}{line}" for line in _nodes(members, ids, options, graph.layer))
            continue
        yield f"{_INDENT}subgraph ns{index}[{_label(namespace)}]"
        yield f"{_INDENT * 2}direction {options.rankdir or DEFAULT_RANKDIR}"
        yield from (f"{_INDENT * 2}{line}" for line in _nodes(members, ids, options, graph.layer))
        yield f"{_INDENT}end"


def _clustered_nodes(
    graph: Graph,
    ids: Mapping[str, str],
    options: RenderOptions,
    *,
    skip: Container[str] = frozenset(),
) -> Iterator[str]:
    """One ``subgraph`` per box the layer asked for: a VRF (§16.8) or a machine (§23.3)."""
    loose = [node for node in graph.nodes.values() if not node.cluster and node.fqn not in skip]
    yield from (f"{_INDENT}{line}" for line in _nodes(loose, ids, options, graph.layer))
    noun = CLUSTER_NOUN.get(graph.layer, "group")
    for index, cluster in enumerate(graph.clusters):
        members = [node for node in graph.nodes_in_cluster(cluster) if node.fqn not in skip]
        if not members:
            continue
        yield f"{_INDENT}subgraph vrf{index}[{_label(f'{noun} {cluster}')}]"
        yield f"{_INDENT * 2}direction {options.rankdir or DEFAULT_RANKDIR}"
        yield from (f"{_INDENT * 2}{line}" for line in _nodes(members, ids, options, graph.layer))
        yield f"{_INDENT}end"


def _nodes(
    nodes: Iterable[Node], ids: Mapping[str, str], options: RenderOptions, layer: Layer
) -> Iterator[str]:
    for node in nodes:
        open_bracket, close_bracket = _NODE_SHAPE.get(node.kind, _DEFAULT_SHAPE)
        text = _label(_node_text(node, options, layer))
        yield f"{ids[node.fqn]}{open_bracket}{text}{close_bracket}"


def _edges(graph: Graph, ids: Mapping[str, str], options: RenderOptions) -> Iterator[str]:
    for edge in graph.edges:
        if edge.kind is EdgeKind.SUBNET:
            # A membership is a plain line: it is not a cable, so it has no
            # medium to encode in the line style.
            plain, labelled = _SOLID
        elif edge.kind in _LOGICAL_EDGE_KINDS:
            # Mermaid offers three line styles and the physical ones are spoken
            # for, so a tunnel borrows the dotted line and says what it is in
            # the label — which it would carry anyway.
            plain, labelled = _DOTTED
        elif edge.kind is EdgeKind.ATTACHMENT or edge.medium == "wireless":
            plain, labelled = _DOTTED
        elif edge.medium == "fiber":
            plain, labelled = _THICK
        else:
            plain, labelled = _SOLID

        text = _edge_text(edge, graph.layer, options)
        link = labelled.format(label=_label(text)) if text else plain
        yield f"{ids[edge.source]} {link} {ids[edge.target]}"


def _class_definitions(graph: Graph, ids: Mapping[str, str]) -> list[str]:
    """One ``classDef`` per kind present, then the membership assignments."""
    by_kind: dict[str, list[str]] = {}
    for fqn, node in graph.nodes.items():
        by_kind.setdefault(node.kind, []).append(ids[fqn])
    if not by_kind:
        return []

    lines = [""]
    for kind in sorted(by_kind):
        style = _CLASS_STYLE.get(kind)
        if style is not None:
            lines.append(f"{_INDENT}classDef {kind} {style}")
    for kind in sorted(by_kind):
        if kind in _CLASS_STYLE:
            lines.append(f"{_INDENT}class {','.join(by_kind[kind])} {kind}")
    return lines


# --------------------------------------------------------------------------- #
# Annotations (§21)
# --------------------------------------------------------------------------- #


def _note_id(index: int) -> str:
    """The flowchart id of the ``index``-th note.

    Not the annotation's own slug: a Mermaid identifier is alphanumeric, and the
    nodes of this document are already positional for the same reason
    (:func:`_identifiers`). ``note`` cannot collide with the ``n0`` a node gets,
    since no node id has a letter after its digits.
    """
    return f"note{index}"


def _area_subgraphs(
    graph: Graph, views: AnnotationViews, ids: Mapping[str, str], options: RenderOptions
) -> Iterator[str]:
    """Each area with members, as the one container Mermaid has.

    Colour, border style and padding are all lost — a Mermaid subgraph has no
    style — and the loss is reported by :func:`_degradations` rather than left
    to be discovered.
    """
    for index, area in enumerate(views.areas):
        members = [graph.nodes[member] for member in area.members if views.area_of(member) is area]
        if not members:
            continue
        label = area.label or area.fqn
        yield f"{_INDENT}subgraph area{index}[{_label(label)}]"
        yield f"{_INDENT * 2}direction {options.rankdir or DEFAULT_RANKDIR}"
        yield from (f"{_INDENT * 2}{line}" for line in _nodes(members, ids, options, graph.layer))
        yield f"{_INDENT}end"


def _note_nodes(views: AnnotationViews) -> Iterator[str]:
    """Each note as a plain box; see the module docstring on why a plain one."""
    for index, note in enumerate(views.notes):
        yield f"{_note_id(index)}[{_label(note.plain)}]"


def _leaders(views: AnnotationViews, ids: Mapping[str, str]) -> Iterator[str]:
    """A dotted, unlabelled link from each anchored note to what it is about."""
    for index, note in enumerate(views.notes):
        if note.leader and note.anchor in ids:
            yield f"{_note_id(index)} {_DOTTED[0]} {ids[note.anchor]}"


def _note_styles(views: AnnotationViews) -> list[str]:
    """The note class, and a ``style`` line for any note that is not its colour.

    One ``classDef`` for the common case keeps the document short; a note that
    chose its own colour gets a line of its own rather than a class nothing else
    would use.
    """
    if not views.notes:
        return []
    lines = [f"{_INDENT}classDef {_NOTE_CLASS} {_NOTE_STYLE}"]
    lines.append(
        f"{_INDENT}class {','.join(_note_id(i) for i in range(len(views.notes)))} {_NOTE_CLASS}"
    )
    for index, note in enumerate(views.notes):
        if (note.fill, note.stroke) != _NOTE_COLOURS:
            lines.append(
                f"{_INDENT}style {_note_id(index)} fill:{note.fill},stroke:{note.stroke},"
                f"stroke-width:1px,stroke-dasharray:3 2"
            )
    return lines


def _degradations(views: AnnotationViews) -> list[str]:
    """What this diagram could not express, as Mermaid comments.

    Visible in the source and invisible in the picture, which is the right side
    of that trade: a reader of the drawing cannot act on the limitation, and a
    reader of the document usually *is* the person wondering where their legend
    went.
    """
    said: list[str] = []
    for area in views.areas:
        if not area.members:
            said.append(
                f"%% area '{area.fqn}' is not drawn: it is a rectangle of canvas rather than "
                f"a set of elements, and mermaid places nothing"
            )
    if any(area.members for area in views.areas):
        said.append(
            "%% areas are drawn as subgraphs: their colour, border style and padding are "
            "not expressible in mermaid"
        )
    for legend in views.legends:
        said.append(
            f"%% legend '{legend.fqn}' ({count_text(len(legend.entries), 'entry', 'entries')}) "
            f"is not "
            f"drawn: mermaid has no construct for a key that is not part of the graph"
        )
    return said


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def _node_text(node: Node, options: RenderOptions, layer: Layer) -> str:
    if node.ipam is not None:
        # Before the subnet below, which a prefix of the address plan also is:
        # here the box is not "who is in this subnet" but "how much of this block
        # is left", and the caption is the only place Mermaid can say so. The
        # bar the DOT record draws comes through as the text it already is.
        plan = [
            node.name,
            f"[{node.ipam.family} {node.ipam.noun}]",
            node.ipam.bar,
            *node.ipam.describe(),
        ]
        if options.show_vlans and node.vlans:
            plan.append(f"vlans: {_compact_ids(node.vlans)}")
        if node.ipam.free_blocks:
            plan.append(f"next: {node.ipam.free_blocks[0]}")
        return "\n".join(plan)

    subnet = node.subnet
    if subnet is not None:
        vrf = f", vrf {subnet.vrf}" if subnet.vrf else ""
        parts = [subnet.prefix, f"[{subnet.family} subnet{vrf}]"]
        if options.show_vlans and node.vlans:
            parts.append(f"vlans: {_compact_ids(node.vlans)}")
        return "\n".join(parts)

    if node.tunnel is not None:
        parts = [node.name, f"[{node.tunnel.type} tunnel]"]
        if node.tunnel.summary != node.tunnel.type:
            parts.append(node.tunnel.summary)
        if node.tunnel.mtu is not None:
            parts.append(f"mtu {node.tunnel.mtu}")
        return "\n".join(parts)

    if node.aggregate is not None:
        return "\n".join(_aggregate_text(node.aggregate, options))

    if node.routing is not None:
        # Who this router is to its peers, then what it carries: the same content
        # the DOT record holds, flattened into the lines a Mermaid node allows.
        routing = [node.name, f"[{node.kind}]", *node.routing.describe()]
        routing.extend(f"vrf {name} ({rd})" for name, rd in node.routing.vrfs)
        routing.extend(node.routing.routes)
        return "\n".join(routing)

    if node.security is not None:
        # The zone, and what is in it: the same content the DOT record holds.
        return "\n".join([node.name, "[zone]", *node.security.describe()])

    if node.power is not None:
        # What this box draws, distributes or hands out: the same content the DOT
        # record holds, and the whole reason the node is drawn at this layer.
        return "\n".join([node.name, f"[{node.kind}]", *node.power.describe()])

    if node.identity is not None:
        # Who this is, the same clauses the DOT record spells out (§19.3).
        return "\n".join([node.name, f"[{node.kind}]", *node.identity.details()])

    parts = [node.name, f"[{node.kind}]"]
    # At layer 3 the addresses live on the edges, where they say which interface
    # holds them; see the DOT renderer for the same reasoning.
    if options.show_ips and layer is not Layer.L3:
        addresses = node.routable_addresses
        if len(addresses) > options.max_addresses:
            shown = [
                *addresses[: options.max_addresses],
                f"(+{len(addresses) - options.max_addresses} more)",
            ]
        else:
            shown = list(addresses)
        parts.extend(shown)
    if options.show_vlans and node.vlans:
        parts.append(f"vlans: {_compact_ids(node.vlans)}")
    return "\n".join(parts)


def _edge_text(edge: Edge, layer: Layer, options: RenderOptions) -> str:
    """Ports plus the layer-appropriate annotation, kept to one line.

    Mermaid link labels do not wrap well, so unlike DOT this is a single line
    with a middle dot separator. A layer-3 membership carries the interface and
    the address it holds — there is no physical detail to choose between.
    """
    if edge.kind is EdgeKind.ENCAPSULATION:
        return f"{edge.label} over" if edge.label else "over"

    if edge.adjacency is not None:
        adjacency = [edge.adjacency.label]
        if edge.adjacency.description:
            adjacency.append(edge.adjacency.description)
        return " · ".join(adjacency)

    if edge.policy is not None:
        # The same content the DOT label carries, folded onto the one line
        # Mermaid link labels read well in; see :meth:`PolicyView.label`.
        return " · ".join(edge.policy.label())

    parts: list[str] = []
    ports = _port_text(edge)
    if ports:
        parts.append(ports)
    if edge.bundle is not None:
        parts.append(edge.bundle.summary)

    if edge.kind is EdgeKind.TUNNEL and edge.tunnel is not None:
        parts.extend(_tunnel_text(edge.tunnel))
        if layer is Layer.L2 and options.show_vlans and edge.vlans:
            parts.append(f"vlan {_compact_ids(edge.vlans)}")
        return " · ".join(parts)

    if edge.kind is EdgeKind.SUBNET:
        if options.show_ips and edge.addresses:
            parts.append(", ".join(edge.addresses))
        return " · ".join(parts)

    if edge.kind.is_power and edge.feed is not None:
        if edge.feed.reserved_watts:
            parts.append(f"{format_watts(edge.feed.reserved_watts)} W")
        return " · ".join(parts)

    if layer is Layer.L2:
        # See ``netviz.render.dot._edge_label``: the SSID and the channel are
        # the layer-2 facts about a radio link.
        if edge.wireless is not None and (association := edge.wireless.describe()):
            parts.append(association)
        if options.show_vlans and edge.vlans:
            parts.append(f"vlan {_compact_ids(edge.vlans)}")
    else:
        if edge.label:
            parts.append(edge.label)
        # A bundle whose members disagree about the medium reports none; an
        # empty part would show as a stray separator in the label.
        if edge.kind is EdgeKind.CABLE and edge.medium and edge.medium != "copper":
            parts.append(edge.medium)
        speed = edge.speed_text
        if speed:
            parts.append(speed)
    return " · ".join(parts)


def _aggregate_text(view: AggregateView, options: RenderOptions) -> list[str]:
    """A collapsed namespace, as the same census the DOT renderer draws.

    The element names are left out here and in DOT alike: the box may stand for
    two hundred of them, and Mermaid has no tooltip to move them to. The JSON
    export is where a reader who needs the list goes.
    """
    parts = [view.namespace, "[namespace]", view.summary]
    if view.internal_links:
        parts.append(f"{len(view.internal_links)} links inside")
    if options.show_vlans and view.vlans:
        parts.append(f"vlans: {_compact_ids(view.vlans)}")
    if options.show_ips and view.subnets:
        shown = view.subnets[: options.max_addresses]
        hidden = len(view.subnets) - len(shown)
        parts.extend(shown)
        if hidden:
            parts.append(f"(+{hidden} more)")
    return parts


def _tunnel_text(view: TunnelView) -> list[str]:
    """The encapsulation, the VNI and — loudest — whether it is in the clear."""
    parts = [view.stack_text]
    if view.vni is not None:
        parts.append(f"vni {view.vni}")
    if view.label:
        parts.append(view.label)
    if not view.encrypted:
        parts.append("via encrypted underlay" if view.encrypted_by else "cleartext")
    return parts


def _port_text(edge: Edge) -> str:
    if edge.source_port and edge.target_port:
        return f"{edge.source_port} ↔ {edge.target_port}"
    return edge.source_port or edge.target_port


def _compact_ids(ids: Iterable[int]) -> str:
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


def _label(text: str) -> str:
    """Quote a Mermaid label, replacing what the parser would choke on.

    Mermaid offers HTML entities rather than backslash escapes, and ``<br/>``
    rather than a newline.
    """
    escaped = (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )
    return f'"{escaped}"'


def _front_matter(title: str) -> str:
    """A title safe for the YAML front-matter block: single line, quoted.

    Unlike a flowchart label, the front matter is read by a YAML parser, so a
    backslash is an escape character here and has to be doubled — ``C:\\Users``
    would otherwise be read as the invalid escape ``\\U``, and a title ending in
    a backslash would escape its own closing quote and swallow the document.
    Backslashes go first, so the one this function adds before a quote is not
    itself doubled afterwards.
    """
    collapsed = " ".join(title.split())
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------- #
# Advisories
# --------------------------------------------------------------------------- #


def mermaid_advisories(nodes: int, edges: int) -> tuple[str, ...]:
    """Warnings about a Mermaid diagram of this size, for the caller to print.

    The backend owns this, not the CLI: the ceiling is a property of Mermaid,
    so a front end should be able to ask "is there anything to say about a
    rendering this big?" without knowing which format it asked for. See
    :data:`RENDERERS <netviz.render.RENDERERS>`.

    The output is correct; the *consumer* refuses it. Mermaid's renderer stops
    at :data:`MERMAID_MAX_EDGES` edges and ``maxEdges`` is a secure config, so a
    diagram cannot raise its own ceiling and there is nothing netviz can emit
    that fixes this. GitHub, GitLab and ``mmdc`` all render with the default, so
    the only remedies are a smaller graph or a format without the limit — which
    is what the message says, in the CLI's own vocabulary, because a warning
    that cannot be acted on is noise.

    Args:
        nodes: Node count of the rendered graph; unused today, part of the
            signature every backend's advisor shares.
        edges: Edge count of the rendered graph.
    """
    del nodes
    if edges <= MERMAID_MAX_EDGES:
        return ()
    return (
        f"this diagram has {edges} edges, over Mermaid's limit of {MERMAID_MAX_EDGES}: "
        "GitHub, GitLab and mermaid-cli will refuse to draw it, and the limit cannot be "
        "raised from inside the document. Cut the graph down with --namespace, --kind or "
        "--neighbors-of, or use '-f dot' or '-f svg', which have no such ceiling",
    )


#: Kept so callers written against the original name keep working; ``to_mermaid``
#: is the canonical spelling, matching ``to_dot`` and ``to_json``.
render_mermaid = to_mermaid
