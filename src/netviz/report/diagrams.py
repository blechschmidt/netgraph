"""The drawings a report holds, and what it says when it cannot draw one.

Nothing here lays a graph out: :func:`~netviz.render.dot.to_image` does, which
is the same call ``netviz render -f svg`` makes, so a diagram in a report is the
diagram the reader would get from the command line. This module decides three
things around it.

**Where the drawing goes.** A Markdown bundle references a ``.svg`` file, because
a Markdown document cannot hold an SVG element; an HTML bundle inlines the
drawing through :mod:`netviz.render.fragment`, because a page that fetches
nothing is a page that can be emailed. Both come from one Graphviz run.

**Where a shape links to.** In the HTML bundle each device in a diagram is an
anchor to its own page — that is most of what makes the bundle a *site* rather
than a folder of files. The URLs are page-relative and therefore depend on which
page embeds the drawing, so they are handed to the backend as a
:class:`~netviz.render.links.LinkMap` rather than derived from a template.

**What to say when there is no drawing.** Graphviz is optional in netviz, and
every other artefact works without it. A report is no different: a layer that
cannot be laid out is a section with a sentence in it saying why, not a failed
run and not a silently missing figure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Final

from markupsafe import Markup

from netviz.errors import RenderError
from netviz.render.dot import find_dot, graphviz_install_hint, to_image
from netviz.render.fragment import fragment
from netviz.render.graph import Graph, Layer
from netviz.render.links import LinkMap
from netviz.render.options import RenderOptions
from netviz.report.layout import Layout
from netviz.report.model import Diagram

__all__ = ["LAYER_TITLES", "Diagrams"]

#: What each layer is called in a report, and what the reader learns from it.
#: Longer than the switcher labels of ``render -f html``: a report is read by
#: somebody who did not choose the layer and needs to be told what they are
#: looking at.
LAYER_TITLES: Final[Mapping[Layer, str]] = {
    Layer.PHYSICAL: "Cabling, patch panels included",
    Layer.L1: "Physical topology",
    Layer.L2: "VLANs",
    Layer.L3: "IP subnets",
    Layer.IPAM: "Address plan: how full every prefix is",
    Layer.OVERLAY: "Tunnels",
    Layer.ROUTING: "Routing: BGP sessions and OSPF adjacencies",
    Layer.RACK: "Rack elevations",
    Layer.POWER: "Power: PDUs and feeds",
}

#: Said when ``--no-diagrams`` was given. Present rather than absent: a reader
#: comparing two reports must be able to tell "this network has no tunnels" from
#: "this report was generated without pictures".
_DISABLED: Final = "Diagrams were switched off for this report (--no-diagrams)."

#: Said instead for a format that has nowhere to put a drawing. A JSON document
#: records which layers a scope has and how big each one is; the picture is what
#: the Markdown and HTML bundles add.
_NOT_A_DRAWING_FORMAT: Final = (
    "This format records the layer rather than drawing it; render the report as "
    "markdown or html for the picture."
)


@dataclass(slots=True)
class Diagrams:
    """Draws the layers of a scope, collecting the files the bundle must hold.

    Mutable, and deliberately so: a Markdown bundle references drawings that have
    to be written *somewhere*, and threading a growing file map through every
    collector that might want a picture would put the plumbing in front of the
    content. :attr:`files` is the accumulator, and
    :func:`netviz.report.generate` merges it into the bundle at the end.
    """

    layout: Layout
    #: How much detail the drawings carry. ``element_ids`` is forced on for the
    #: HTML bundle, since an id is what a stylesheet and a deep link hold on to.
    options: RenderOptions = field(default_factory=RenderOptions)
    #: Fully-qualified element name → bundle-relative page about it.
    pages: Mapping[str, str] = field(default_factory=dict)
    #: ``--diagrams/--no-diagrams``.
    enabled: bool = True
    #: Bundle-relative path → bytes, for every drawing written as a file.
    files: dict[str, bytes] = field(default_factory=dict)
    #: Layers that could not be drawn at all, once each, for the caller to
    #: report on stderr rather than once per section.
    problems: list[str] = field(default_factory=list)

    @property
    def inline(self) -> bool:
        """Does this format embed the drawing instead of referencing a file?"""
        return self.layout.format == "html"

    @property
    def _absent(self) -> str:
        """Why this report holds no drawing: the format, or the flag."""
        return _NOT_A_DRAWING_FORMAT if self.layout.format == "json" else _DISABLED

    def draw(self, graph: Graph, *, scope: str, page: str) -> Diagram:
        """One layer of one scope, as a :class:`~netviz.report.model.Diagram`.

        Args:
            graph: The graph to draw, already built at the layer it is for.
            scope: What the drawing is of — a namespace, or ``""`` for the whole
                report. Part of the file name, so it must be stable.
            page: Bundle-relative path of the page that will hold the drawing.
                Decides what the links inside it are relative to.
        """
        title = LAYER_TITLES.get(graph.layer, graph.layer.value)
        empty = Diagram(
            layer=graph.layer.value,
            title=title,
            nodes=len(graph.nodes),
            edges=len(graph.edges),
        )
        if not self.enabled:
            return replace(empty, note=self._absent)
        if not graph.nodes:
            return replace(empty, note="Nothing in this report is drawn at this layer.")
        if find_dot() is None:
            return replace(
                empty,
                note=(
                    "Graphviz is not installed, so this layer could not be laid out. "
                    f"{graphviz_install_hint()}"
                ),
            )

        try:
            payload = to_image(graph, self._options(page), format="svg")
        except RenderError as exc:
            # A layout that failed is worth one line in the document and one on
            # stderr; it is not worth losing the tables to.
            self.problems.append(f"{graph.layer.value}: {exc}")
            return replace(empty, note=f"This layer could not be laid out: {exc}")

        if self.inline:
            # Markup, not str: this is the one value in a report that reaches a
            # page as markup rather than as text, and it has earned it — every
            # attribute of it has just been through the sanitiser in
            # netviz.render.fragment. Everything else the templates print is
            # inventory data and stays escaped.
            return replace(
                empty,
                markup=Markup(
                    fragment(
                        payload,
                        tooltips=self.options.tooltips,
                        local_links=bool(self.pages),
                        prefix=graph.layer.value,
                    )
                ),
            )
        path = self.layout.diagram(scope, graph.layer.value)
        self.files[path] = payload
        return replace(empty, path=path)

    def _options(self, page: str) -> RenderOptions:
        """The render options for a drawing embedded in ``page``.

        The link map is rebuilt per page rather than cached, because the URLs in
        it are relative to the page: the *same* drawing embedded one directory
        deeper links to the same device pages by a different path.
        """
        if not self.inline or not self.pages:
            return replace(self.options, link_template=None)
        urls = {
            fqn: self.layout.link(target, frm=page)
            for fqn, target in self.pages.items()
            if target and target != page
        }
        return replace(self.options, link_template=LinkMap(urls), element_ids=True)
