"""The resolved command line of ``netviz report``, in one object.

Separate from the collector so that a caller which is not the CLI — a test, or a
script generating the committed example under ``docs/`` — can state what it wants
without going through Click, and so the collector never has to ask "was that flag
given?".

Every field is already resolved: the timestamp has been parsed or pinned off, the
revision has been discovered or supplied, and the title has been decided. That is
what makes :func:`netviz.report.generate` a pure function of its arguments, and
therefore what makes two runs over one inventory produce identical bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from netviz import __version__
from netviz.render.graph import Layer
from netviz.render.options import RenderOptions

__all__ = ["Options"]


@dataclass(frozen=True, slots=True)
class Options:
    """Everything ``netviz report`` was asked for."""

    #: ``markdown``, ``html`` or ``json``.
    format: str = "markdown"
    #: Which layers to draw. Empty means "whatever this inventory has earned";
    #: see :func:`~netviz.report.collect.layers_for`.
    layers: tuple[Layer, ...] = ()
    #: Title for the overview. Empty derives one from the inventory directory.
    title: str = ""
    #: How many namespace levels below the shared prefix a site page covers.
    #: ``None`` lets :func:`~netviz.report.collect.site_groups` decide.
    group_depth: int | None = None
    #: ``--diagrams/--no-diagrams``.
    diagrams: bool = True
    #: The generated-at stamp, ISO-8601 UTC, or ``""`` for a report without one
    #: (:mod:`netviz.report.stamp`).
    generated_at: str = ""
    #: The inventory's git revision, or ``""`` when there is none.
    revision: str = ""
    #: ``clean`` or ``modified`` beside the revision.
    revision_state: str = ""
    #: What the report covers, in words: the filters, or "the whole inventory".
    scope: str = "the whole inventory"
    #: How much detail the drawings carry.
    render: RenderOptions = field(default_factory=RenderOptions)
    #: The version of netviz that produced the report. A field rather than a
    #: constant so a golden test can pin it and still exercise the real path.
    version: str = __version__

    def title_for(self, root: Path) -> str:
        """The report title: what was asked for, or the inventory's own name."""
        if self.title:
            return self.title
        name = root.name or str(root)
        return f"{name} — as-built network documentation"

    def inventory_name(self, root: Path) -> str:
        """How the inventory is named on the page.

        The directory's own name rather than its absolute path: the path is a
        property of the machine the report was generated on, and a document
        committed next to the inventory should not change because a colleague
        checked the repository out somewhere else.
        """
        return root.name or str(root)
