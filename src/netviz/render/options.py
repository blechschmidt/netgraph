"""Display options shared by every renderer.

These say *what to draw*, never *what exists*: which annotations appear on a
node, whether namespaces become visual groups. Which elements exist is decided
by :class:`~netviz.render.graph.FilterSpec` before a renderer ever runs, so
turning a label off can never change the topology a reader sees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from netviz.layout.geometry import Routing
from netviz.render.diffview import DiffOverlay
from netviz.render.highlight import Highlight
from netviz.render.icons import IconTheme
from netviz.render.links import Linker
from netviz.render.theme import Theme

__all__ = ["DEFAULT_RANKDIR", "RANKDIRS", "RenderOptions"]

#: Layout directions, spelled as Graphviz spells them. Mermaid happens to use
#: the same four tokens, so one value serves both backends and there is no
#: mapping table to keep in step.
RANKDIRS: Final[tuple[str, ...]] = ("TB", "LR", "BT", "RL")

#: What both backends lay out as when nothing asks otherwise. Top-to-bottom
#: suits the tree a network usually is: core at the top, access below it.
DEFAULT_RANKDIR: Final = "TB"


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """How much detail a rendering carries."""

    #: Print configured IP addresses under each node label.
    show_ips: bool = True
    #: Annotate ports and links with their VLAN membership.
    show_vlans: bool = True
    #: Draw each namespace as a visual group (a Graphviz cluster / Mermaid subgraph).
    group_by_namespace: bool = False
    #: Draw the notes, areas and legends the inventory declares for this view
    #: (§21). On by default: an annotation is something somebody wrote down
    #: *about this diagram*, so leaving it out has to be asked for. Off emits
    #: none of them, in every backend, and produces exactly the bytes the same
    #: inventory would produce with no annotation documents in it — which is what
    #: makes this a display option rather than a filter.
    annotations: bool = True
    #: Diagram caption; ``None`` leaves the rendering untitled.
    title: str | None = None
    #: Longest address list spelled out under a node before it is abbreviated.
    max_addresses: int = 4
    #: Layout direction, one of :data:`RANKDIRS`. ``None`` means
    #: :data:`DEFAULT_RANKDIR`, which is what the backends laid out as before
    #: this option existed. Honoured by the Graphviz backends and by Mermaid
    #: (which spells the same four tokens the same way); JSON carries no layout.
    rankdir: str | None = None
    #: How links are drawn when neither they nor the layout document say:
    #: ``spline``, ``orthogonal`` or ``straight``. ``None`` — the default —
    #: leaves the decision to the inventory (``spec.routing`` on a ``kind:
    #: layout`` document, or a view's own), and to Graphviz's curve when the
    #: inventory says nothing either. A link that pins its own style keeps it:
    #: this is a default, not an override, because a route somebody dragged
    #: into place is a decision about *that cable* and a command-line flag is
    #: not. Honoured by the Graphviz backends, by the JSON export and by the
    #: editor canvas; Mermaid has one edge shape and ignores it.
    routing: Routing | None = None
    #: Route orthogonal links *around* the boxes they are not attached to
    #: (:mod:`netviz.layout.avoid`) rather than straight across them.
    #: ``False`` is ``--no-avoid``: every link takes the local Z or L it took
    #: before obstacle avoidance existed, which is faster, entirely predictable,
    #: and occasionally what a reader of a deliberately schematic diagram wants.
    #: Only ever consulted for an *arranged* drawing whose links are orthogonal:
    #: a spline has nothing to route around, and an unarranged one is routed by
    #: Graphviz's own ``splines=ortho``, which already avoids nodes.
    avoid: bool = True
    #: Draw each node as its kind's icon instead of a plain Graphviz shape.
    #: ``None`` keeps the shapes. Honoured by the Graphviz backends; Mermaid and
    #: JSON have no picture to put an icon in, and ignore it.
    icons: IconTheme | None = None
    #: The stylesheet in force (§22): named selectors mapping onto style
    #: blocks, applied to whatever they match. ``None`` — the default — draws
    #: with the built-in palette and whatever the elements say about themselves.
    #: Honoured by the Graphviz backends, by the draw.io export and by JSON,
    #: which publishes the resolved style and its provenance beside each node;
    #: Mermaid restates the palette as ``classDef`` rules and has nowhere to put
    #: a per-element one.
    theme: Theme | None = None
    #: Walk the style ladder at all. ``False`` is ``--no-style``: every declared
    #: style, on an element and in a theme, is ignored, and the rendering is the
    #: plain one the built-in palette and the icon set produce. The escape hatch
    #: for reading a diagram whose stylesheet is in the way, and the reason a
    #: themed and an unthemed golden of one inventory are both worth keeping.
    styling: bool = True
    #: Carry the per-element detail into the drawing as hover text
    #: (:mod:`netviz.render.details`). Emitted as Graphviz ``tooltip``
    #: attributes, which reach a reader in SVG and are dropped by every raster
    #: format. Turn it off for a diagram that must carry nothing the picture
    #: itself does not show.
    tooltips: bool = True
    #: Where each drawn element links to. ``--link-template`` builds a URL from
    #: the document that declares it; ``netviz report`` hands over a
    #: :class:`~netviz.render.links.LinkMap` of page URLs instead. Emitted as
    #: Graphviz ``URL`` attributes; SVG turns them into anchors, the raster
    #: formats drop them.
    link_template: Linker | None = None
    #: Give every node, edge and cluster a stable ``id`` derived from its
    #: fully-qualified name (:mod:`netviz.render.ids`), so an SVG rendering can
    #: be deep-linked and styled from outside. Off by default: a hand-read DOT
    #: file is better without them.
    element_ids: bool = False
    #: Draw part of the graph emphasised and the rest dimmed
    #: (:mod:`netviz.render.highlight`). ``None`` — the default — draws every
    #: node and link at full weight. This changes no topology: what is *drawn*
    #: is decided by :class:`~netviz.render.graph.FilterSpec` before a
    #: renderer runs, and a highlight only decides how loudly. Honoured by the
    #: Graphviz backends; Mermaid and JSON have no visual weight to vary.
    highlight: Highlight | None = None
    #: Draw the graph as a *diff*: added things green, removed things red and
    #: dashed, changed things amber with a badge naming the fields that moved,
    #: and everything untouched faded
    #: (:mod:`netviz.render.diffview`). ``None`` — the default — draws one
    #: state of the network, as every rendering did before ``netviz diff``
    #: existed.
    #:
    #: Like a highlight this changes no topology. Unlike one it is normally
    #: paired with a graph that holds *both* states — see
    #: :func:`netviz.render.diffview.union_graph` — because a removed device
    #: has to be somewhere to be drawn in red. Honoured by the Graphviz backends
    #: and by JSON, which publishes the marks and the changeset beside the
    #: graph; Mermaid has no vocabulary for either and ignores it.
    diff: DiffOverlay | None = None
