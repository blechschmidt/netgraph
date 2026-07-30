"""``netgraph report``: the as-built document an engineer is asked to hand over.

Everything else this tool produces answers a question. A report answers the
*deliverable*: a per-site, per-device record with the diagram, the address plan,
the VLAN and trunk matrix, the cable schedule, the rack positions, the wireless
plan and the open validation findings — all of it consistent with all the rest,
because every page came from one inventory and one set of derivations.

The shape of a bundle
---------------------

An overview, a page per site and a page per element::

    <out>/
      README.md              index.html            report.json
      sites/<site>.md        sites/<site>.html
      devices/<name>.md      devices/<name>.html
      diagrams/<scope>-<layer>.svg

``--format markdown`` is the default: it is what a reader commits next to the
inventory and diffs. ``--format html`` writes the same document as a
self-contained site — one style sheet inlined per page, no fetches, and every
device in every diagram is a link to that device's page. ``--format json`` writes
the whole document as one file for downstream tooling; nothing is lost but the
prose formatting.

Where the content comes from
----------------------------

Nothing is derived twice. The tables are :mod:`netgraph.listing` (what
``netgraph list`` prints), the address plan is :mod:`netgraph.ipam`, the cable
schedule is :func:`netgraph.export.cables.schedule` (what
``netgraph export cable-list`` writes), the power schedule is
:mod:`netgraph.power`, the findings are :mod:`netgraph.validate` through
:mod:`netgraph.diagnostics`, and the diagrams are the graphs
:mod:`netgraph.render` draws. A per-site view is those same functions over a
narrowed inventory (:func:`netgraph.loader.inventory.subset`), which is what makes
a site page and the overview provably agree.

Determinism
-----------

The same inventory produces the same bytes, so a report can be committed and
reviewed as a diff. Page names are slugs of fully-qualified names with collisions
resolved in sorted order, tables are sorted, JSON is dumped in a fixed order, and
files are written with ``\\n`` endings on every platform. The one thing that
varies is the generated-at stamp, which is why it can be pinned — see
:mod:`netgraph.report.stamp`.

Traceability
------------

Every page carries the netgraph version, the generated-at stamp and the
inventory's git revision when it has one, because six months later those are the
only questions anybody has about a printed report.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from netgraph.diagnostics import Diagnostic
from netgraph.loader.inventory import Inventory
from netgraph.report.bundle import Bundle
from netgraph.report.collect import (
    Context,
    Scope,
    layers_for,
    paged_elements,
    prepare,
    site_groups,
)
from netgraph.report.diagrams import LAYER_TITLES, Diagrams
from netgraph.report.layout import DEVICES_DIR, DIAGRAMS_DIR, SITES_DIR, Layout, anchor, page_slug
from netgraph.report.model import FORMATS, Cell, Column, Diagram, Meta, Page, Report, Section, Table
from netgraph.report.options import Options
from netgraph.report.pages import build
from netgraph.report.stamp import (
    EPOCH_ENV_VAR,
    NO_TIMESTAMP,
    Revision,
    git_revision,
    resolve_timestamp,
)
from netgraph.report.write import JSON_FILE, render_pages, stylesheet

__all__ = [
    "DEVICES_DIR",
    "DIAGRAMS_DIR",
    "EPOCH_ENV_VAR",
    "FORMATS",
    "JSON_FILE",
    "LAYER_TITLES",
    "NO_TIMESTAMP",
    "SITES_DIR",
    "Bundle",
    "Cell",
    "Column",
    "Context",
    "Diagram",
    "Diagrams",
    "Layout",
    "Meta",
    "Options",
    "Page",
    "Report",
    "Revision",
    "Scope",
    "Section",
    "Table",
    "anchor",
    "build",
    "collect",
    "generate",
    "git_revision",
    "layers_for",
    "page_slug",
    "paged_elements",
    "render_pages",
    "resolve_timestamp",
    "site_groups",
    "stylesheet",
]


def collect(
    inventory: Inventory,
    *,
    options: Options | None = None,
    diagnostics: Iterable[Diagnostic] = (),
    full: Inventory | None = None,
) -> tuple[Report, Layout, Diagrams]:
    """Collect the document without writing anything.

    Returns the report, the layout its cross-references were built against and the
    diagram helper holding the drawings, which is what
    :func:`generate` then renders. Split out so a test — or a caller doing
    something else with the document — can inspect the model without going
    anywhere near a template or a file.
    """
    resolved = options or Options()
    layout = Layout.build(
        resolved.format,
        devices=paged_elements(inventory),
        sites=site_groups(inventory, depth=resolved.group_depth),
    )
    diagrams = Diagrams(
        layout=layout,
        options=resolved.render,
        # Every element with a page is a link target; which of them a given
        # drawing actually links to is whatever it draws.
        pages=layout.devices,
        # A JSON document references no drawing, so laying one out would be
        # several seconds of Graphviz nobody reads.
        enabled=resolved.diagrams and resolved.format != "json",
    )
    context = prepare(
        inventory,
        options=resolved,
        layout=layout,
        diagrams=diagrams,
        diagnostics=diagnostics,
        full=full,
    )
    return build(context), layout, diagrams


def generate(
    inventory: Inventory,
    *,
    options: Options | None = None,
    diagnostics: Iterable[Diagnostic] = (),
    full: Inventory | None = None,
    templates: Path | None = None,
) -> tuple[Bundle, Diagrams]:
    """Build the whole bundle: every page, plus the drawings they reference.

    Args:
        inventory: The selection to document, already filtered.
        options: The resolved command line; defaults to a Markdown report of
            everything.
        diagnostics: Open validation findings, so the report can carry them.
        full: The inventory before filtering, so a scoped report can still say
            which links leave the scope.
        templates: ``--template DIR``, whose templates win over the bundled ones.

    Returns:
        The bundle, and the diagram helper — whose ``problems`` list is what the
        CLI reports on stderr when a layer could not be laid out.

    Raises:
        NetgraphError: A template is missing or failed to render.
    """
    report, layout, diagrams = collect(
        inventory, options=options, diagnostics=diagnostics, full=full
    )
    pages = render_pages(report, layout=layout, templates=templates)
    return Bundle(files=pages).merged(diagrams.files), diagrams
