"""What a report *is*, before anybody decides whether it is Markdown or HTML.

One collected :class:`Report` holds every page, and every page holds sections of
fields, diagrams and tables. Nothing here knows about Markdown syntax, HTML
escaping or file writing: the three writers under this package walk the same
object, which is what makes ``--format markdown``, ``--format html`` and
``--format json`` three renderings of one document rather than three documents.

Two decisions are worth spelling out.

**A cell is text plus an optional destination.** A table cannot hold pre-rendered
links: ``[sw-core](../devices/sw-core.md)`` is wrong on a page one directory
deeper, and ``<a href>`` is wrong in Markdown. So a :class:`Cell` carries the
bundle-relative page it points at and each writer turns that into whatever a link
is in its format, relative to the page it is writing
(:meth:`~netgraph.report.layout.Layout.link`).

**Sections are uniform.** Every page is a list of sections and every section has
the same shape, so the templates have one loop rather than one branch per kind of
content — which is what makes ``--template DIR`` a usable override: a reader who
wants their own layout replaces a template that does one thing, not a chain of
special cases.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from netgraph.console import Align

__all__ = [
    "FORMATS",
    "Cell",
    "Column",
    "Diagram",
    "Field",
    "Meta",
    "Page",
    "Report",
    "Section",
    "Table",
]

#: The three shapes a report can be written in, in ``--help`` order.
FORMATS: Final[tuple[str, ...]] = ("markdown", "html", "json")

#: Printed where a table has no value: an em dash reads as "nothing here" and,
#: unlike an empty cell, survives being pasted into a spreadsheet.
BLANK: Final = "—"


@dataclass(frozen=True, slots=True)
class Cell:
    """One value, and where the reader can go to learn more about it."""

    text: str = BLANK
    #: Bundle-relative path of the page this cell refers to, e.g.
    #: ``devices/sw-core.md``. Empty for a value that is only a value.
    page: str = ""
    #: In-page anchor within :attr:`page` (or within the *current* page, when
    #: :attr:`page` is empty).
    fragment: str = ""
    #: An absolute URL, for the rare cell that leaves the bundle — a rule's
    #: write-up in the online documentation. Never combined with :attr:`page`.
    url: str = ""

    @property
    def is_linked(self) -> bool:
        return bool(self.page or self.fragment or self.url)

    def to_json(self) -> Any:
        """Text alone for a plain cell; an object once there is a link to keep."""
        if not self.is_linked:
            return self.text
        record: dict[str, Any] = {"text": self.text}
        if self.page:
            record["page"] = self.page
        if self.fragment:
            record["anchor"] = self.fragment
        if self.url:
            record["url"] = self.url
        return record


@dataclass(frozen=True, slots=True)
class Column:
    """One column heading, with the alignment its values want."""

    header: str
    align: Align = "left"


@dataclass(frozen=True, slots=True)
class Table:
    """A titled table, with something to say even when it is empty."""

    #: Anchor for this table, unique within its page.
    key: str
    title: str
    columns: tuple[Column, ...] = ()
    rows: tuple[tuple[Cell, ...], ...] = ()
    #: How to read the table, or what it deliberately leaves out. One line.
    note: str = ""
    #: What to print instead of an empty table body. A report that silently
    #: omits a section reads as "there is nothing to say about this", which is
    #: a different claim from "this inventory declares none".
    empty: str = "Nothing declared."

    @property
    def is_empty(self) -> bool:
        return not self.rows

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "columns": [
                {"header": column.header, "align": column.align} for column in self.columns
            ],
            "rows": [[cell.to_json() for cell in row] for row in self.rows],
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Field:
    """One labelled fact, for the identity block at the top of a page."""

    label: str
    value: Cell = field(default_factory=Cell)

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value.to_json()}


@dataclass(frozen=True, slots=True)
class Diagram:
    """One layer, drawn — or a note saying why it is not."""

    layer: str
    title: str
    #: Bundle-relative path of the ``.svg`` file, for a format that references
    #: the drawing rather than embedding it.
    path: str = ""
    #: The drawing as an ``<svg>`` fragment, for a format that inlines it.
    #: Already sanitised by :mod:`netgraph.render.fragment`.
    markup: str = ""
    nodes: int = 0
    edges: int = 0
    #: Why there is no drawing: Graphviz is absent, the layer is empty, or
    #: drawings were switched off. Empty when there is one.
    note: str = ""

    @property
    def drawn(self) -> bool:
        return bool(self.path or self.markup)

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "title": self.title,
            "path": self.path,
            "nodes": self.nodes,
            "edges": self.edges,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Section:
    """One heading and everything under it."""

    #: Anchor, unique within the page. Every cross-reference in a report points
    #: at one of these, so they are part of the published contract.
    key: str
    title: str
    #: One line of prose under the heading, when the section needs framing.
    blurb: str = ""
    fields: tuple[Field, ...] = ()
    diagrams: tuple[Diagram, ...] = ()
    tables: tuple[Table, ...] = ()
    #: Cross-references, rendered as a list of links.
    links: tuple[Cell, ...] = ()
    #: Caveats, printed after the content.
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "blurb": self.blurb,
            "fields": [entry.to_json() for entry in self.fields],
            "diagrams": [diagram.to_json() for diagram in self.diagrams],
            "tables": [table.to_json() for table in self.tables],
            "links": [link.to_json() for link in self.links],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class Page:
    """One file of the bundle."""

    #: Bundle-relative path, extension included.
    path: str
    #: ``overview``, ``site`` or ``device``. Selects the template.
    kind: str
    title: str
    #: One line under the title saying what this page is about.
    summary: str = ""
    #: The page this one belongs under, for the breadcrumb and the "up" link.
    parent: str = ""
    sections: tuple[Section, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "parent": self.parent,
            "sections": [section.to_json() for section in self.sections],
        }


@dataclass(frozen=True, slots=True)
class Meta:
    """Where this report came from, so a printed copy stays traceable."""

    title: str
    #: The inventory root as the user named it.
    inventory: str
    #: The version of netgraph that produced the report.
    version: str
    #: ISO-8601 UTC, second precision. Empty when the stamp was pinned off,
    #: which is how a report becomes byte-identical between two runs.
    generated_at: str = ""
    #: Commit the inventory was at, when it is in a git work tree.
    revision: str = ""
    #: ``clean`` or ``modified`` beside :attr:`revision`; empty when unknown.
    revision_state: str = ""
    #: What the report covers: the filters, or "the whole inventory".
    scope: str = ""
    #: ``markdown``, ``html`` or ``json``.
    format: str = "markdown"
    #: Element counts by kind, plus the totals the overview prints.
    counts: Mapping[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "inventory": self.inventory,
            "netgraph": self.version,
            "generatedAt": self.generated_at or None,
            "revision": self.revision or None,
            "revisionState": self.revision_state or None,
            "scope": self.scope,
            "format": self.format,
            "counts": dict(self.counts),
        }


@dataclass(frozen=True, slots=True)
class Report:
    """A whole documentation bundle, collected and not yet written."""

    meta: Meta
    pages: tuple[Page, ...] = ()

    @property
    def overview(self) -> Page:
        """The index page. Every report has exactly one, and it is first."""
        return self.pages[0]

    def page_at(self, path: str) -> Page | None:
        """The page written to ``path``, or ``None``."""
        return next((page for page in self.pages if page.path == path), None)

    def of_kind(self, kind: str) -> tuple[Page, ...]:
        return tuple(page for page in self.pages if page.kind == kind)

    def to_json(self) -> dict[str, Any]:
        return {
            "meta": self.meta.to_json(),
            "pages": [page.to_json() for page in self.pages],
        }
