"""The pull list: one row per physical run, with both ends located.

This is the artefact somebody carries into a room. Nothing else the tool
produces answers the question an installer actually has — *which cable, from
which rack unit and port, to which rack unit and port, of what type, how long* —
and a diagram answers it badly, because a picture of a topology is not a
worklist.

One row per **cable document**, which is one run of cable an installer pulls.
That is why the graph is built at ``--layer physical``: every other layer
splices a run through a patch panel into the single logical link it is
electrically equivalent to (§15.2), which is right for a diagram and wrong here
— the two segments either side of a panel are two separate things to pull,
terminate and label.

The panel is not lost, though. The ``RUN`` column names the end-to-end link the
segment belongs to, taken from the spliced view, and ``SEGMENT`` says which leg
of it this is — so ``2 of 3`` reads as "the middle run, panel to panel", and the
three rows of one logical link sort together.

Locations come from ``metadata.location`` (§3.2): site, room, rack and the
lowest unit the element occupies. A ``patchpanel`` endpoint additionally fills
``*_PANEL_PORT``, so a row reads "from ``sw-01`` U12 port ``Gi1/0/7`` to
``pp-idf-a`` U40 panel port ``front/7``".

What it drops
-------------

Adapter attachments. A USB-to-Ethernet dongle's upstream is a physical
connection, but it is part of the adapter rather than a run somebody pulls, and
it has no medium, no length and no label to carry. Tunnels and subnet
memberships are not physical at all and never appear. Anything a filter removed
one end of is recorded in the manifest rather than emitted as a half-row.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from netgraph.export.context import ExportContext, location_of, record_dangling
from netgraph.export.header import html_header
from netgraph.export.manifest import Reason
from netgraph.export.names import csv_cell, markdown_cell
from netgraph.loader.inventory import namespace_of, short_name
from netgraph.models import Cable
from netgraph.render.graph import Edge, EdgeKind, Graph, Layer, Node

__all__ = ["COLUMNS", "End", "Row", "emit", "schedule"]

#: The columns, in the one order both output styles use. ``A`` and ``B`` are the
#: cable's two endpoints in the canonical order §7.1 stores them in, so a run
#: does not swap ends between two exports of the same inventory.
COLUMNS: Final[tuple[str, ...]] = (
    "RUN",
    "SEGMENT",
    "CABLE",
    "LABEL",
    "MEDIUM",
    "CATEGORY",
    "CONNECTOR",
    "SPEED",
    "LENGTH_M",
    "A_ELEMENT",
    "A_PORT",
    "A_PANEL_PORT",
    "A_SITE",
    "A_ROOM",
    "A_RACK",
    "A_UNIT",
    "B_ELEMENT",
    "B_PORT",
    "B_PANEL_PORT",
    "B_SITE",
    "B_ROOM",
    "B_RACK",
    "B_UNIT",
)

#: Kinds whose ports are panel positions rather than interfaces. Kept as a set
#: rather than a single string so a second passive kind added later fills the
#: ``*_PANEL_PORT`` column without a change here.
_PANEL_KINDS: Final = frozenset({"patchpanel"})


@dataclass(frozen=True, slots=True)
class End:
    """One end of a run, as far as the inventory places it."""

    element: str
    port: str
    kind: str
    site: str
    room: str
    rack: str
    unit: int | None

    @property
    def panel_port(self) -> str:
        """The port, when the element is a passive panel; blank otherwise.

        Redundant with :attr:`port` by construction, and worth the column: an
        installer scanning the sheet for "which panel position" should not have
        to know which of the two element names is the panel.
        """
        return self.port if self.kind in _PANEL_KINDS else ""

    @property
    def sort_key(self) -> tuple[int, str, str, str, int, str, str]:
        """Rack order, with everything unplaced after everything placed.

        The list is walked rack by rack, so a run between two located elements
        must sort by where it is. An element with no ``metadata.location`` has
        no place in that walk and would otherwise interleave arbitrarily, so it
        is pushed to the end by the leading flag.
        """
        placed = 0 if self.rack else 1
        return (placed, self.site, self.room, self.rack, self.unit or 0, self.element, self.port)


@dataclass(frozen=True, slots=True)
class Row:
    """One line of the pull list."""

    run: str
    segment: str
    cable: str
    label: str
    medium: str
    category: str
    connector: str
    speed: str
    length: str
    a: End
    b: End

    def cells(self) -> tuple[str, ...]:
        return (
            self.run,
            self.segment,
            self.cable,
            self.label,
            self.medium,
            self.category,
            self.connector,
            self.speed,
            self.length,
            self.a.element,
            self.a.port,
            self.a.panel_port,
            self.a.site,
            self.a.room,
            self.a.rack,
            _unit(self.a.unit),
            self.b.element,
            self.b.port,
            self.b.panel_port,
            self.b.site,
            self.b.room,
            self.b.rack,
            _unit(self.b.unit),
        )

    @property
    def sort_key(self) -> tuple[object, ...]:
        return (self.a.sort_key, self.b.sort_key, self.cable)


def schedule(context: ExportContext) -> tuple[Row, ...]:
    """The pull list as rows, in rack order, before any of it is formatted.

    Split out of :func:`emit` because the cable schedule of a site page in a
    ``netgraph report`` is this same list of runs in this same order — the whole
    point of the artefact is that an installer and a document agree about which
    cable goes where, and they can only agree if there is one derivation of it.
    What each front end differs in is the *serialisation*: CSV for a spreadsheet,
    a Markdown table for a ticket, a styled table for a report page.

    The recorder is filled in as a side effect, so a caller that wants to know
    what the list does not hold reads ``context.recorder`` afterwards.
    """
    physical = context.at(Layer.PHYSICAL)
    runs = _runs(context.at(Layer.L1))
    recorder = context.recorder
    record_dangling(physical, recorder)

    rows = sorted(
        (
            _row(edge, physical.nodes, runs)
            for edge in physical.edges
            if edge.kind is EdgeKind.CABLE
        ),
        key=lambda row: row.sort_key,
    )
    recorder.considered = len(context.inventory.cables)
    recorder.emitted = len(rows)
    _record_missing(context, physical, {row.cable for row in rows})
    return tuple(rows)


def emit(context: ExportContext) -> str:
    """Render the pull list as CSV or as a Markdown table."""
    rows = schedule(context)
    if context.options.table_format == "markdown":
        return _markdown(rows, context)
    return _csv(rows)


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


def _row(edge: Edge, nodes: Mapping[str, Node], runs: Mapping[str, tuple[str, str]]) -> Row:
    cable = edge.cable
    spec = cable.spec if cable is not None else None
    run, segment = runs.get(edge.id, ("", ""))
    return Row(
        run=run,
        segment=segment,
        cable=edge.id,
        label=(edge.label or ""),
        medium=edge.medium,
        category=getattr(spec, "category", None) or "",
        connector=getattr(spec, "connector", None) or "",
        speed=edge.speed_text or "",
        length=_length(edge.length_m),
        a=_end(nodes.get(edge.source), edge.source, edge.source_port),
        b=_end(nodes.get(edge.target), edge.target, edge.target_port),
    )


def _unit(position: int | None) -> str:
    """A rack unit, or the empty cell when the element is not placed."""
    return "" if position is None else str(position)


def _end(node: Node | None, fqn: str, port: str) -> End:
    if node is None:  # pragma: no cover - filter_graph keeps both ends or neither
        return End(element=fqn, port=port, kind="", site="", room="", rack="", unit=None)
    site, room, rack, position, _ = location_of(node)
    return End(
        element=fqn, port=port, kind=node.kind, site=site, room=room, rack=rack, unit=position
    )


def _length(metres: float | None) -> str:
    """The declared length, or blank. Never a placeholder.

    A pull list is read as a shopping list as often as a worklist, so an
    undeclared length has to be visibly absent rather than rendered as ``0`` or
    ``-``, either of which a spreadsheet would happily sum.
    """
    if metres is None:
        return ""
    return str(int(metres)) if float(metres).is_integer() else str(metres)


def _runs(spliced: Graph) -> Mapping[str, tuple[str, str]]:
    """``cable fqn -> (run label, "2 of 3")`` for every segment of a patched run.

    Read off the *spliced* graph, where a run through one or more panels is the
    single edge it is electrically equivalent to, and where
    :class:`~netgraph.render.graph.PatchView` remembers the segments it stands
    for and their order. A direct cable is not in the mapping at all: it is its
    own run and the columns stay blank rather than repeating the cable name.
    """
    mapping: dict[str, tuple[str, str]] = {}
    for edge in spliced.edges:
        patch = edge.patch
        if patch is None or len(patch.segments) < 2:
            continue
        label = (
            f"{short_name(edge.source)}:{edge.source_port} - "
            f"{short_name(edge.target)}:{edge.target_port}"
        )
        total = len(patch.segments)
        for index, segment in enumerate(patch.segments, start=1):
            mapping[segment] = (label, f"{index} of {total}")
    return mapping


def _record_missing(context: ExportContext, graph: Graph, emitted: Iterable[str]) -> None:
    """Say which declared cables produced no row, and why.

    A cable is absent for one of three reasons, and the manifest has to tell
    them apart because they call for different actions:

    * the graph could not resolve an endpoint — already recorded by
      :func:`~netgraph.export.context.record_dangling`, and a broken inventory;
    * a filter kept one end and not the other, so the row would name an element
      this list does not hold;
    * both ends survived but the *link* did not — a ``--vlan`` filter keeps a
      device that is in the VLAN and still drops a cable that carries none of
      it. Blaming that on a missing endpoint would send the reader looking for
      a device that is sitting right there in the table.

    Reporting any of them is what stops ``--namespace sites/north`` from looking
    like a complete cabling record for the site when the uplinks to the core are
    missing from it.
    """
    written = set(emitted)
    unresolved = {
        skip.subject for skip in context.recorder.skips if skip.reason is Reason.UNRESOLVED
    }
    for fqn, cable in sorted(context.inventory.cables.items()):
        if fqn in written or fqn in unresolved:
            continue
        if _both_ends_present(context, graph, cable, namespace=namespace_of(fqn)):
            context.recorder.skip(
                fqn,
                Reason.NOT_SELECTED,
                "both endpoints are in the selection but the link itself is not: it "
                "carries none of the VLANs asked for",
            )
            continue
        context.recorder.skip(
            fqn,
            Reason.HALF_SELECTED,
            "at least one endpoint is outside the selection, so the run would name "
            "an element this list does not hold",
        )


def _both_ends_present(
    context: ExportContext, graph: Graph, cable: Cable, *, namespace: str
) -> bool:
    """Did both of ``cable``'s endpoints survive the filter as nodes?

    Answered through the inventory's own reference resolution rather than by
    string comparison: an endpoint names an element the way the author wrote it
    — relative to the cable's namespace — and the graph is keyed by
    fully-qualified name.
    """
    return all(
        (resolved := context.inventory.resolve_fqn(endpoint.device, namespace=namespace))
        is not None
        and resolved in graph.nodes
        for endpoint in cable.spec.endpoints
    )


# --------------------------------------------------------------------------- #
# Output styles
# --------------------------------------------------------------------------- #


def _csv(rows: Sequence[Row]) -> str:
    """RFC 4180 CSV, with ``\\n`` line endings.

    Quoting is :mod:`csv`'s job — it is the one implementation worth trusting
    with a cable label that contains a comma, a quote or a newline — and Unix
    line endings match every other file this CLI writes.

    Every cell additionally goes through :func:`~netgraph.export.names.csv_cell`
    on the way out, which neutralises a field a spreadsheet would evaluate as a
    formula. This is the one artefact here that is *certain* to be opened in a
    spreadsheet, so a cable labelled ``=HYPERLINK(...)`` becoming a live link is
    a real outcome rather than a theoretical one.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(tuple(csv_cell(cell) for cell in row.cells()) for row in rows)
    return buffer.getvalue()


def _markdown(rows: Sequence[Row], context: ExportContext) -> str:
    """A GitHub-flavoured Markdown table, for a ticket or a printed sheet.

    Columns are padded to their widest cell so the source is readable before it
    is rendered, which is most of the point of putting it in a ticket.
    """
    table = [tuple(markdown_cell(cell) for cell in row.cells()) for row in rows]
    widths = [
        max(len(COLUMNS[index]), max((len(row[index]) for row in table), default=0))
        for index in range(len(COLUMNS))
    ]
    lines = list(
        html_header(
            "cable-list",
            (
                f"{len(rows)} run(s). One row per cable segment: a run through a patch panel "
                "is several.",
                "The manifest on stderr lists the cables this table does not hold and why.",
            ),
        )
    )
    lines.append(_markdown_row(COLUMNS, widths))
    lines.append("|" + "|".join("-" * (width + 2) for width in widths) + "|")
    lines.extend(_markdown_row(row, widths) for row in table)
    return "".join(f"{line}\n" for line in lines)


def _markdown_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    return (
        "| "
        + " | ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))
        + " |"
    )
