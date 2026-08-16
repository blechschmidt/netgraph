"""The visual encoding: what colour and what shape each thing in a graph is drawn as.

One table per question, in one module, because four consumers ask the same
questions and a diagram whose key disagrees with the picture beside it is worse
than a picture with no key:

* :mod:`netgraph.render.dot` draws the graph, and picks a Graphviz shape, a fill
  and a line style from here;
* :mod:`netgraph.render.annotations` builds an ``auto: layers`` legend (§21)
  from exactly the same entries, so a swatch cannot claim a colour the drawing
  does not use;
* :mod:`netgraph.drawio.styles` exports the same encoding as mxGraph styles;
* :mod:`netgraph.render.mermaid` restates it as ``classDef`` rules, which is the
  one place a second copy survives — Mermaid's vocabulary is not a subset of
  Graphviz's, and the two tables are asserted equal in the test suite rather
  than shared.

Two principles decide every entry. **Shape carries the kind and colour repeats
it**, so a topology is readable before a single label is and stays readable in a
greyscale print. And **line style carries the medium and colour repeats it**, for
the same reason and for a reader who cannot distinguish two accent hues.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from netgraph.render.aggregate import AGGREGATE_KIND
from netgraph.render.graph import (
    GROUP_KIND,
    NETNS_KIND,
    PATCHPANEL_KIND,
    PDU_KIND,
    RACK_KIND,
    SUBNET_KIND,
    TUNNEL_KIND,
    USER_KIND,
    Edge,
    EdgeKind,
    Layer,
)

__all__ = [
    "CLUSTER_NOUN",
    "CLUSTER_PALETTE",
    "DEFAULT_EDGE_PALETTE",
    "DEFAULT_MEDIUM_STYLE",
    "DEFAULT_NODE_PALETTE",
    "DEFAULT_NODE_STYLE",
    "EDGE_PALETTE",
    "MEDIUM_STYLE",
    "NESTING_STYLE",
    "NODE_PALETTE",
    "NODE_STYLE",
    "VETH_STYLE",
    "edge_palette_key",
    "edge_style_for",
    "node_style_for",
]

#: The cluster frame netgraph draws itself when the layout engine will not.
#: The three colours match what ``graph.dot.j2`` sets on a ``subgraph cluster``,
#: because the two are alternative ways of drawing the same frame and a diagram
#: must not change appearance when it becomes fixed.
CLUSTER_STROKE: Final = "#9ca3af"
CLUSTER_LABEL_COLOUR: Final = "#4b5563"
CLUSTER_FONT: Final = "Helvetica,Arial,sans-serif"
CLUSTER_FONT_SIZE: Final = 11

#: Shape, fill and outline per element kind: the kind picks the glyph, so the
#: topology is readable before a single label is. The outline is a saturated
#: version of the fill, so kind stays legible in a greyscale print.
#:
#: ``hub`` and ``computer`` are both boxes (``rectangle`` is Graphviz's synonym
#: for ``box``); they are told apart by their palette, and a hub is rare enough
#: in a modern inventory that spending a third box shape on it would cost more
#: than it buys. :data:`~netgraph.render.graph.SUBNET_KIND` is not hardware — it
#: is a prefix the addresses imply — so it gets a palette entry of its own and
#: is drawn with rounded corners (see ``netgraph.render.dot._node_style``).
NODE_STYLE: Final[Mapping[str, tuple[str, str, str]]] = MappingProxyType(
    {
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
        # A network namespace is a *stack*, not a box on a shelf and not a folder
        # of documents. ``rounded`` is the shape every container diagram draws
        # one with, and it is the last one here no element kind had taken. Cyan
        # is the last accent free, and pairing it with the veth colour is what
        # makes the netns view legible at a glance: the boxes and the lines
        # between them are one vocabulary.
        NETNS_KIND: ("rounded", "#cffafe", "#0891b2"),
    }
)
DEFAULT_NODE_STYLE: Final[tuple[str, str, str]] = ("box", "#f5f5f5", "#6b7280")

#: Edge colour and line style per cable medium. Style carries the medium and
#: colour repeats it, so a link stays classifiable in a greyscale print and for
#: a reader who cannot distinguish the two accent colours.
MEDIUM_STYLE: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "copper": ("#4a4a4a", "solid"),
        "fiber": ("#d97706", "bold"),
        "wireless": ("#2563eb", "dashed"),
    }
)
DEFAULT_MEDIUM_STYLE: Final[tuple[str, str]] = ("#4a4a4a", "solid")

#: An adapter attachment is a bus, not a cable (§8.2). Drawing it like one would
#: claim a link that does not exist, so it gets a style no medium uses.
ATTACHMENT_STYLE: Final[tuple[str, str]] = ("#9ca3af", "dotted")

#: A subnet membership is not a cable either, so it borrows the subnet's colour.
SUBNET_EDGE_STYLE: Final[tuple[str, str]] = ("#0f766e", "solid")

#: A tunnel is drawn dashed, because it runs over a path the diagram already
#: shows rather than over one of its own. Colour carries confidentiality — the
#: one property of a tunnel a reader most needs at a glance — and the two are
#: far enough apart in lightness to survive a greyscale print: violet when the
#: payload is protected, crimson when it crosses the underlay in the clear.
TUNNEL_STYLE: Final[tuple[str, str]] = ("#6d28d9", "dashed")
CLEARTEXT_TUNNEL_STYLE: Final[tuple[str, str]] = ("#be123c", "dashed")

#: A tunnel's ``over`` is not a path at all; it says one link is carried by
#: another. Dotted and violet: the vocabulary of the tunnel it belongs to,
#: with a line weight that keeps it behind the tunnels themselves.
ENCAPSULATION_STYLE: Final[tuple[str, str]] = ("#8b5cf6", "dotted")

#: A membership (§19.3) is the only edge the identity view has, so it needs no
#: contrast with a sibling: solid, in the identity rose, and told apart from
#: everything else by being the only line on the page.
MEMBERSHIP_STYLE: Final[tuple[str, str]] = ("#be185d", "solid")

#: The two adjacencies of the routing view (§16.6). A BGP session is a
#: configured, point-to-point relationship, so it is drawn *solid*; an OSPF
#: adjacency is discovered and belongs to an area rather than to a pair, so it is
#: dotted. Both are blue-green — the colour of the routed layers here — and the
#: difference in line style survives a greyscale print, which the difference in
#: hue would not.
BGP_STYLE: Final[tuple[str, str]] = ("#0369a1", "solid")
OSPF_STYLE: Final[tuple[str, str]] = ("#0f766e", "dotted")

#: The two feeds of the power view (§17.5). An outlet feed is a cord somebody can
#: pull, so it is drawn *solid* in the PDU's amber; a PoE feed is power riding on
#: a data run that the diagram draws elsewhere, so it is dashed — the same
#: vocabulary a tunnel uses for "this runs over something else", in the power
#: palette rather than the tunnel one. The line style is what survives a
#: greyscale print, which is why the distinction is not carried by hue alone.
OUTLET_STYLE: Final[tuple[str, str]] = ("#b45309", "solid")
POE_STYLE: Final[tuple[str, str]] = ("#ca8a04", "dashed")

#: The two edges of the netns view (§23.3). A veth pair is a real link that
#: carries real frames, so it is drawn *solid*, in the cyan no other edge uses;
#: a nesting edge is not a path at all — it says one stack was created inside
#: another — so it borrows the encapsulation vocabulary of "this is carried by
#: that" and is dotted, in the slate the aggregate node already uses for "a
#: container of things". The line style is again what survives greyscale.
VETH_STYLE: Final[tuple[str, str]] = ("#0891b2", "solid")
NESTING_STYLE: Final[tuple[str, str]] = ("#475569", "dotted")

#: Fill and outline per element kind, without the Graphviz shape — for a
#: renderer that draws its own glyphs but must colour a node the way a diagram
#: does. Derived from :data:`NODE_STYLE` rather than restated, so the mxGraph
#: export (:mod:`netgraph.drawio.styles`) cannot drift away from the picture
#: netgraph itself draws.
NODE_PALETTE: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {kind: (fill, stroke) for kind, (_shape, fill, stroke) in NODE_STYLE.items()}
)

#: What a node of an unknown kind is coloured.
DEFAULT_NODE_PALETTE: Final[tuple[str, str]] = (DEFAULT_NODE_STYLE[1], DEFAULT_NODE_STYLE[2])

#: Colour and Graphviz line style per *reason a link is drawn*: the three
#: cable media first, then the edge kinds that are not cables at all. Same
#: reasoning as :data:`NODE_PALETTE` — one table, several renderers.
EDGE_PALETTE: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        **MEDIUM_STYLE,
        "attachment": ATTACHMENT_STYLE,
        "subnet": SUBNET_EDGE_STYLE,
        "tunnel": TUNNEL_STYLE,
        "cleartext-tunnel": CLEARTEXT_TUNNEL_STYLE,
        "encapsulation": ENCAPSULATION_STYLE,
        "membership": MEMBERSHIP_STYLE,
        "bgp": BGP_STYLE,
        "ospf": OSPF_STYLE,
        "outlet": OUTLET_STYLE,
        "poe": POE_STYLE,
        "veth": VETH_STYLE,
        "nesting": NESTING_STYLE,
    }
)

#: What a link netgraph has no palette entry for is drawn as.
DEFAULT_EDGE_PALETTE: Final[tuple[str, str]] = DEFAULT_MEDIUM_STYLE

#: The frame a namespace cluster is drawn with, so a group container in an
#: exported diagram is the same colour as the box a rendering draws.
CLUSTER_PALETTE: Final[tuple[str, str]] = (CLUSTER_STROKE, CLUSTER_LABEL_COLOUR)

#: Which :data:`EDGE_PALETTE` entry each non-cable edge kind takes. A cable takes
#: its medium instead, and a tunnel is decided by whether anything encrypts it,
#: so neither is in the table.
_EDGE_KIND_KEYS: Final[Mapping[EdgeKind, str]] = MappingProxyType(
    {
        EdgeKind.ATTACHMENT: "attachment",
        EdgeKind.SUBNET: "subnet",
        EdgeKind.ENCAPSULATION: "encapsulation",
        EdgeKind.MEMBERSHIP: "membership",
        EdgeKind.BGP: "bgp",
        EdgeKind.OSPF: "ospf",
        EdgeKind.OUTLET: "outlet",
        EdgeKind.POE: "poe",
        EdgeKind.VETH: "veth",
        EdgeKind.NESTING: "nesting",
    }
)


#: What the box a *layer* asks for is called, per layer. Only the two layers that
#: group their own nodes have an entry; every other layer clusters by namespace,
#: which is labelled with the namespace itself. Here rather than in either
#: renderer because a DOT diagram and a Mermaid one of the same graph must not
#: name the same box two things.
CLUSTER_NOUN: Final[Mapping[Layer, str]] = MappingProxyType(
    {
        Layer.ROUTING: "vrf",
        # The netns view boxes every stack of one machine together, so the box
        # *is* the machine — and saying so is what keeps it from being read as
        # one more namespace, which is exactly what the things inside it are.
        Layer.NETNS: "machine",
    }
)


def edge_palette_key(edge: Edge) -> str:
    """Which :data:`EDGE_PALETTE` entry ``edge`` is drawn with.

    The one function that decides it, because two things read the answer: the
    line the renderer draws, and the swatch an ``auto: layers`` legend puts
    beside it (§21). A key that was computed twice would eventually be computed
    two ways, and a legend that named a colour the diagram does not use is worse
    than no legend.

    A tunnel is the one kind whose entry depends on more than the kind: what a
    reader most needs to know about a tunnel is whether anything encrypts it, so
    a cleartext one is drawn in its own colour.
    """
    if edge.kind is EdgeKind.TUNNEL:
        return "tunnel" if edge.tunnel is None or edge.tunnel.protected else "cleartext-tunnel"
    if edge.kind is EdgeKind.CABLE:
        return edge.medium or "copper"
    return _EDGE_KIND_KEYS.get(edge.kind, edge.medium)


def edge_style_for(edge: Edge) -> tuple[str, str]:
    """``(colour, line style)`` for one link, defaulted for anything unlisted."""
    return EDGE_PALETTE.get(edge_palette_key(edge), DEFAULT_EDGE_PALETTE)


def node_style_for(kind: str) -> tuple[str, str, str]:
    """``(shape, fill, stroke)`` for an element kind, defaulted for an unknown one."""
    return NODE_STYLE.get(kind, DEFAULT_NODE_STYLE)
