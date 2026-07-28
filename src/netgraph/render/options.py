"""Display options shared by every renderer.

These say *what to draw*, never *what exists*: which annotations appear on a
node, whether namespaces become visual groups. Which elements exist is decided
by :class:`~netgraph.render.graph.FilterSpec` before a renderer ever runs, so
turning a label off can never change the topology a reader sees.
"""

from __future__ import annotations

from dataclasses import dataclass

from netgraph.render.icons import IconTheme

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
