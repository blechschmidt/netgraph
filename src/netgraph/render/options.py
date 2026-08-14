"""Display options shared by every renderer.

These say *what to draw*, never *what exists*: which annotations appear on a
node, whether namespaces become visual groups. Which elements exist is decided
by :class:`~netgraph.render.graph.FilterSpec` before a renderer ever runs, so
turning a label off can never change the topology a reader sees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from netgraph.render.diffview import DiffOverlay
from netgraph.render.highlight import Highlight
from netgraph.render.icons import IconTheme
from netgraph.render.links import Linker

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
    #: Diagram caption; ``None`` leaves the rendering untitled.
    title: str | None = None
    #: Longest address list spelled out under a node before it is abbreviated.
    max_addresses: int = 4
    #: Layout direction, one of :data:`RANKDIRS`. ``None`` means
    #: :data:`DEFAULT_RANKDIR`, which is what the backends laid out as before
    #: this option existed. Honoured by the Graphviz backends and by Mermaid
    #: (which spells the same four tokens the same way); JSON carries no layout.
    rankdir: str | None = None
    #: Draw each node as its kind's icon instead of a plain Graphviz shape.
    #: ``None`` keeps the shapes. Honoured by the Graphviz backends; Mermaid and
    #: JSON have no picture to put an icon in, and ignore it.
    icons: IconTheme | None = None
    #: Carry the per-element detail into the drawing as hover text
    #: (:mod:`netgraph.render.details`). Emitted as Graphviz ``tooltip``
    #: attributes, which reach a reader in SVG and are dropped by every raster
    #: format. Turn it off for a diagram that must carry nothing the picture
    #: itself does not show.
    tooltips: bool = True
    #: Where each drawn element links to. ``--link-template`` builds a URL from
    #: the document that declares it; ``netgraph report`` hands over a
    #: :class:`~netgraph.render.links.LinkMap` of page URLs instead. Emitted as
    #: Graphviz ``URL`` attributes; SVG turns them into anchors, the raster
    #: formats drop them.
    link_template: Linker | None = None
    #: Give every node, edge and cluster a stable ``id`` derived from its
    #: fully-qualified name (:mod:`netgraph.render.ids`), so an SVG rendering can
    #: be deep-linked and styled from outside. Off by default: a hand-read DOT
    #: file is better without them.
    element_ids: bool = False
    #: Draw part of the graph emphasised and the rest dimmed
    #: (:mod:`netgraph.render.highlight`). ``None`` — the default — draws every
    #: node and link at full weight. This changes no topology: what is *drawn*
    #: is decided by :class:`~netgraph.render.graph.FilterSpec` before a
    #: renderer runs, and a highlight only decides how loudly. Honoured by the
    #: Graphviz backends; Mermaid and JSON have no visual weight to vary.
    highlight: Highlight | None = None
    #: Draw the graph as a *diff*: added things green, removed things red and
    #: dashed, changed things amber with a badge naming the fields that moved,
    #: and everything untouched faded
    #: (:mod:`netgraph.render.diffview`). ``None`` — the default — draws one
    #: state of the network, as every rendering did before ``netgraph diff``
    #: existed.
    #:
    #: Like a highlight this changes no topology. Unlike one it is normally
    #: paired with a graph that holds *both* states — see
    #: :func:`netgraph.render.diffview.union_graph` — because a removed device
    #: has to be somewhere to be drawn in red. Honoured by the Graphviz backends
    #: and by JSON, which publishes the marks and the changeset beside the
    #: graph; Mermaid has no vocabulary for either and ignores it.
    diff: DiffOverlay | None = None
