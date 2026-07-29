"""Display options shared by every renderer.

These say *what to draw*, never *what exists*: which annotations appear on a
node, whether namespaces become visual groups. Which elements exist is decided
by :class:`~netgraph.render.graph.FilterSpec` before a renderer ever runs, so
turning a label off can never change the topology a reader sees.
"""

from __future__ import annotations

from dataclasses import dataclass

from netgraph.render.icons import IconTheme
from netgraph.render.links import LinkTemplate

__all__ = ["RenderOptions"]


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
    #: Link each drawn element back to the document that declares it. Emitted as
    #: Graphviz ``URL`` attributes; SVG turns them into anchors, the raster
    #: formats drop them.
    link_template: LinkTemplate | None = None
    #: Give every node, edge and cluster a stable ``id`` derived from its
    #: fully-qualified name (:mod:`netgraph.render.ids`), so an SVG rendering can
    #: be deep-linked and styled from outside. Off by default: a hand-read DOT
    #: file is better without them.
    element_ids: bool = False
