"""The pages of a report: what each one says, and in what order.

An overview, a page per site and a page per element. Every page is a list of
:class:`~netgraph.report.model.Section`, every section is fields, diagrams and
tables, and every table came from a derivation netgraph already had — see
:mod:`netgraph.report.collect`.

Two things run through all of it.

**Nothing is silently absent.** A section whose table is empty still appears, with
a sentence saying the inventory declares none of whatever it is about. "This
network has no tunnels" and "this report forgot about tunnels" look identical
otherwise, and only one of them is true.

**Every cross-reference is a real one.** A device named on a site page links to its
own page; a device page links back to its site, to the diagrams it appears in and
to the far end of every cable on it. The anchors those links point at are declared
here (:func:`~netgraph.report.layout.anchor`) and are part of what the tests
check: no page may reference an anchor no page offers.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Final

from netgraph import listing
from netgraph.diagnostics import Diagnostic
from netgraph.export import cables as cable_export
from netgraph.export.context import ExportContext, ExportOptions, location_of
from netgraph.export.manifest import Recorder
from netgraph.ipam import build_report as build_ipam_report
from netgraph.loader.inventory import Inventory, short_name, subset
from netgraph.models import Element, Pdu, format_watts
from netgraph.render.graph import Edge, EdgeKind, Graph, Layer, Node
from netgraph.report.collect import (
    DATA_LAYER,
    ROOT_SITE,
    Context,
    Scope,
    meta,
    paged_elements,
    table_from_listing,
    with_appearances,
)
from netgraph.report.diagrams import LAYER_TITLES
from netgraph.report.layout import anchor
from netgraph.report.model import BLANK, Cell, Column, Field, Page, Report, Section, Table
from netgraph.subnets import Subnet, subnets_of

__all__ = ["build"]

#: Column sets used more than once, so two pages cannot disagree about them.
_CABLE_COLUMNS: Final[tuple[Column, ...]] = (
    Column("RUN"),
    Column("SEGMENT"),
    Column("CABLE"),
    Column("LABEL"),
    Column("MEDIUM"),
    Column("CATEGORY"),
    Column("SPEED", "right"),
    Column("LENGTH", "right"),
    Column("A END"),
    Column("A PORT"),
    Column("A POSITION"),
    Column("B END"),
    Column("B PORT"),
    Column("B POSITION"),
)

#: The views the element tables are merged from. A report owes a page to every
#: declared element, and no single layer draws them all: the cabling view has the
#: patch panels, the power view has the PDUs.
_ELEMENT_LAYERS: Final = (DATA_LAYER, Layer.POWER)

_FINDING_COLUMNS: Final[tuple[Column, ...]] = (
    Column("SEVERITY"),
    Column("RULE"),
    Column("ELEMENT"),
    Column("LOCATION"),
    Column("MESSAGE"),
)


def build(context: Context) -> Report:
    """Collect every page, sites first so the device pages can reference them."""
    site_pages = [_site_page(context, group) for group in context.sites]
    context = with_appearances(context, _appearances(context, site_pages))
    device_pages = [_device_page(context, fqn) for fqn in sorted(paged_elements(context.inventory))]
    overview = _overview_page(context, site_pages)
    return Report(meta=meta(context), pages=(overview, *site_pages, *device_pages))


def _appearances(context: Context, pages: Sequence[Page]) -> dict[str, tuple[tuple[str, str], ...]]:
    """Element → the ``(page, layer)`` pairs whose drawing holds it."""
    found: dict[str, list[tuple[str, str]]] = {}
    for group, page in zip(context.sites, pages, strict=True):
        scope = context.sites[group]
        for section in page.sections:
            for diagram in section.diagrams:
                if not diagram.drawn:
                    continue
                for fqn in _drawn_elements(scope.nodes(Layer(diagram.layer))):
                    found.setdefault(fqn, []).append((page.path, diagram.layer))
    return {fqn: tuple(entries) for fqn, entries in found.items()}


def _drawn_elements(nodes: Mapping[str, Node]) -> Iterator[str]:
    """Which *elements* a set of drawn nodes shows.

    Usually the element nodes themselves. A rack elevation is the exception and
    the reason this is a function: its nodes are cabinets, and the elements are
    the slots inside them — which is exactly the drawing a reader looking for
    "where is this box" wants to be sent to.
    """
    for fqn, node in nodes.items():
        if node.is_element:
            yield fqn
        elif node.rack is not None:
            yield from (slot.element for slot in node.rack.slots)


# --------------------------------------------------------------------------- #
# The overview
# --------------------------------------------------------------------------- #


def _overview_page(context: Context, site_pages: Sequence[Page]) -> Page:
    """The index: what the inventory holds, and where to read about each part."""
    scope = context.report
    inventory = scope.inventory
    path = context.layout.index
    sections = [
        _contents_section(context, site_pages, path=path),
        _diagram_section(context, scope, path=path),
        _findings_section(context, context.diagnostics, note=_FINDINGS_NOTE),
        Section(
            key="devices",
            title="Every element",
            blurb="One row per element, linked to its page.",
            tables=(
                table_from_listing(
                    context,
                    listing.devices(inventory, layers=_ELEMENT_LAYERS),
                    key="devices",
                    title="Elements",
                    note=(
                        "PORTS counts declared interfaces; ADDRESS is the address that places "
                        "the element on the network, of however many it has."
                    ),
                    empty="This selection holds no element.",
                ),
            ),
        ),
        _addressing_section(context, scope),
        _vlan_section(context, scope),
    ]
    if inventory.tunnels:
        sections.append(
            Section(
                key="tunnels",
                title="Tunnels",
                blurb="Every overlay, with what it runs inside and what protects it.",
                tables=(
                    table_from_listing(
                        context,
                        listing.tunnels(inventory),
                        key="tunnels",
                        title="Tunnels",
                        note=(
                            "STACK reads outwards: 'vxlan over ipsec' is VXLAN carried inside "
                            "the IPsec tunnel."
                        ),
                    ),
                ),
            )
        )
    if inventory.pdus:
        sections.append(_power_section(context, scope))
    if inventory.users or inventory.groups:
        sections.append(_accounts_section(context, scope))
    return Page(
        path=path,
        kind="overview",
        title=context.options.title_for(inventory.root),
        summary=(
            f"{len(inventory.elements)} element(s) across "
            f"{len(context.sites)} site page(s), covering {context.options.scope}."
        ),
        sections=tuple(sections),
    )


#: Why a report shows its findings rather than presenting a clean face.
_FINDINGS_NOTE: Final = (
    "These are the findings 'netgraph validate' reports for this inventory. A report is "
    "only as authoritative as the inventory behind it, so they are documented here rather "
    "than left out."
)


def _contents_section(context: Context, site_pages: Sequence[Page], *, path: str) -> Section:
    """The site index: one row per site page, with what each holds."""
    rows: list[tuple[Cell, ...]] = []
    for group, page in zip(context.sites, site_pages, strict=True):
        scope = context.sites[group]
        inventory = scope.inventory
        rows.append(
            (
                Cell(text=group or ROOT_SITE, page=page.path),
                Cell(text=str(len(paged_elements(inventory)))),
                Cell(text=str(len(inventory.cables))),
                Cell(text=str(len(inventory.tunnels))),
                Cell(text=str(len(subnets_of(inventory)))),
            )
        )
    return Section(
        key="contents",
        title="Sites",
        blurb="One page per site. A site is a namespace; see --group-depth.",
        tables=(
            Table(
                key="sites",
                title="Site pages",
                columns=(
                    Column("SITE"),
                    Column("ELEMENTS", "right"),
                    Column("CABLES", "right"),
                    Column("TUNNELS", "right"),
                    Column("SUBNETS", "right"),
                ),
                rows=tuple(rows),
                empty="This selection holds no element.",
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# A site
# --------------------------------------------------------------------------- #


def _site_page(context: Context, group: str) -> Page:
    """One site: its diagrams, its address plan, its cabling, its findings."""
    scope = context.sites[group]
    inventory = scope.inventory
    path = context.layout.site(group)
    sections = [
        Section(
            key="summary",
            title="Summary",
            fields=(
                Field("Namespace", Cell(text=group or ROOT_SITE)),
                Field("Elements", Cell(text=str(len(paged_elements(inventory))))),
                Field("Cables", Cell(text=str(len(inventory.cables)))),
                Field("Tunnels", Cell(text=str(len(inventory.tunnels)))),
                Field("Subnets", Cell(text=str(len(subnets_of(inventory))))),
            ),
            links=(Cell(text="Report overview", page=context.layout.index),),
        ),
        _diagram_section(context, scope, path=path),
        Section(
            key="devices",
            title="Elements",
            blurb="Everything documented on this page, with a page each.",
            tables=(
                table_from_listing(
                    context,
                    listing.devices(inventory, layers=_ELEMENT_LAYERS),
                    key="devices",
                    title="Elements",
                    empty="This site holds no element.",
                ),
            ),
        ),
        _addressing_section(context, scope),
        _vlan_section(context, scope),
        _cabling_section(context, scope),
        _wireless_section(context, scope),
    ]
    if inventory.pdus:
        sections.append(_power_section(context, scope))
    if inventory.users or inventory.groups:
        sections.append(_accounts_section(context, scope))
    sections.append(_external_section(context, scope))
    sections.append(
        _findings_section(
            context,
            context.findings_for(inventory.elements),
            note="Findings anchored to an element of this site.",
        )
    )
    return Page(
        path=path,
        kind="site",
        title=group or ROOT_SITE,
        summary=f"As-built record of {group or 'the inventory root'}.",
        parent=context.layout.index,
        sections=tuple(sections),
    )


def _addressing_section(context: Context, scope: Scope) -> Section:
    """The address plan, with the utilisation ``netgraph ipam`` reports."""
    report = build_ipam_report(scope.inventory)
    return Section(
        key="addressing",
        title="Address plan",
        blurb="Every prefix an address sits in, and how full it is.",
        tables=(
            table_from_listing(
                context,
                listing.utilisation(report.rows),
                key="subnets",
                title="Subnets",
                note=(
                    "HOSTS is the usable capacity of the prefix, USED the distinct addresses "
                    "declared in it. Loopback and link-local prefixes are left out: they are "
                    "scoped to one host or one link and say nothing about the plan."
                ),
                empty="No address is declared here.",
                record_key="prefix",
            ),
        ),
        notes=(
            (
                f"{len(report.findings)} addressing conflict(s) in this scope; they are in the "
                "findings table, and 'netgraph ipam --conflicts' explains each one.",
            )
            if report.findings
            else ()
        ),
    )


def _vlan_section(context: Context, scope: Scope) -> Section:
    """VLANs, and the matrix that says which device is in which with what address."""
    inventory = scope.inventory
    prefixes = subnets_of(inventory)
    return Section(
        key="vlans",
        title="VLANs",
        blurb="Every VLAN, the prefixes carried in it and the elements that are in it.",
        tables=(
            table_from_listing(
                context,
                listing.vlans(inventory),
                key="vlan-summary",
                title="VLANs",
                note=(
                    "Membership is derived: a host on an untagged access port counts as a "
                    "member even though it declares no VLAN itself."
                ),
                empty="No VLAN is declared here.",
                record_key="",
            ),
            _vlan_matrix(context, scope, prefixes),
        ),
    )


def _vlan_matrix(context: Context, scope: Scope, prefixes: Sequence[Subnet]) -> Table:
    """One row per VLAN and element: the ports in it, and the prefixes on them."""
    # Layer 2 whether or not it is drawn: the matrix is about VLAN membership, and
    # ``--layer`` decides what is drawn rather than what is true.
    nodes = scope.nodes(Layer.L2)
    # First non-empty name wins, exactly as ``netgraph list vlans`` decides it:
    # two switches may define the same VLAN and only one of them may name it.
    names: dict[int, str] = {}
    for device in scope.inventory.devices.values():
        for definition in device.spec.vlans:
            if definition.name and definition.id not in names:
                names[definition.id] = definition.name
    rows: list[tuple[Cell, ...]] = []
    for vlan in sorted({vlan for node in nodes.values() for vlan in node.vlans}):
        for fqn, node in sorted(nodes.items()):
            if vlan not in node.vlans:
                continue
            ports = [port.name for port in node.ports if vlan in port.vlans]
            modes = sorted({port.vlan_mode or "-" for port in node.ports if vlan in port.vlans})
            carried = sorted(
                {
                    subnet.prefix
                    for subnet in prefixes
                    if vlan in subnet.vlans and fqn in subnet.elements
                }
            )
            rows.append(
                (
                    Cell(text=str(vlan)),
                    Cell(text=names.get(vlan) or BLANK),
                    context.cell_for(fqn),
                    Cell(text=", ".join(ports) or BLANK),
                    Cell(text=", ".join(modes) or BLANK),
                    Cell(text=", ".join(carried) or BLANK),
                )
            )
    return Table(
        key="vlan-matrix",
        title="VLAN, subnet and element matrix",
        columns=(
            Column("VLAN", "right"),
            Column("NAME"),
            Column("ELEMENT"),
            Column("PORTS"),
            Column("MODE"),
            Column("SUBNETS"),
        ),
        rows=tuple(rows),
        note=(
            "A row with no port is an element that reaches the VLAN over a link rather than "
            "by configuring it — a host behind an access port, or a hub."
        ),
        empty="No VLAN is declared here.",
    )


def _cabling_section(context: Context, scope: Scope) -> Section:
    """The cable schedule, and a port map per patch panel."""
    rows, recorder = _schedule(context, scope)
    tables = [
        Table(
            key="cable-schedule",
            title="Cable schedule",
            columns=_CABLE_COLUMNS,
            rows=tuple(_cable_row(context, row) for row in rows),
            note=(
                "One row per cable document, which is one run somebody pulls: a link through "
                "a patch panel is several. RUN names the end-to-end link and SEGMENT which "
                "leg of it this is. 'netgraph export cable-list' writes the same rows as CSV, "
                "with the full location of both ends."
            ),
            empty="No cable is declared here.",
        ),
        *_panel_maps(context, scope),
    ]
    notes: list[str] = []
    if recorder.skips:
        notes.append(
            f"{len(recorder.skips)} cable(s) are not in the schedule because one end is "
            "outside this page; they are listed under the links leaving this site."
        )
    return Section(
        key="cabling",
        title="Cabling",
        blurb="What an installer would carry into the room.",
        tables=tuple(tables),
        notes=tuple(notes),
    )


def _schedule(context: Context, scope: Scope) -> tuple[tuple[cable_export.Row, ...], Recorder]:
    """The cable schedule of one scope, from the exporter's own derivation."""
    graphs = {layer: scope.graph(layer) for layer in (Layer.PHYSICAL, Layer.L1)}
    recorder = Recorder()
    export_context = ExportContext(
        inventory=scope.inventory,
        graphs=graphs,
        options=ExportOptions(),
        recorder=recorder,
    )
    return cable_export.schedule(export_context), recorder


def _cable_row(context: Context, row: cable_export.Row) -> tuple[Cell, ...]:
    """One schedule row, with both element names linked to their pages."""
    return (
        Cell(text=row.run or BLANK),
        Cell(text=row.segment or BLANK),
        Cell(text=short_name(row.cable)),
        Cell(text=row.label or BLANK),
        Cell(text=row.medium or BLANK),
        Cell(text=row.category or BLANK),
        Cell(text=row.speed or BLANK),
        Cell(text=f"{row.length}m" if row.length else BLANK),
        context.cell_for(row.a.element),
        Cell(text=row.a.port or BLANK),
        Cell(text=_position(row.a.site, row.a.room, row.a.rack, row.a.unit)),
        context.cell_for(row.b.element),
        Cell(text=row.b.port or BLANK),
        Cell(text=_position(row.b.site, row.b.room, row.b.rack, row.b.unit)),
    )


def _position(site: str, room: str, rack: str, unit: int | None) -> str:
    """``hq / mdf / r1 U12`` — as much of a location as the inventory declares."""
    parts = [part for part in (site, room, rack) if part]
    text = " / ".join(parts)
    if unit is not None:
        text = f"{text} U{unit}" if text else f"U{unit}"
    return text or BLANK


def _panel_maps(context: Context, scope: Scope) -> tuple[Table, ...]:
    """One port map per patch panel: what is coupled to what, and what is in it.

    Both sides of the panel, every position, patched or not — a free position is
    what a reader planning a move is looking for, and the *derived* ports
    (:attr:`~netgraph.models.patchpanel.PatchPanelSpec.interfaces`) are the only
    place they all exist: a panel document declares a count, not a list.
    """
    edges = scope.edges(DATA_LAYER)
    tables: list[Table] = []
    for fqn, panel in sorted(scope.inventory.patchpanels.items()):
        coupling = panel.coupling
        terminated: dict[str, tuple[str, str, str]] = {}
        for edge in edges:
            if edge.kind is not EdgeKind.CABLE:
                continue
            for near, near_port, far, far_port in (
                (edge.source, edge.source_port, edge.target, edge.target_port),
                (edge.target, edge.target_port, edge.source, edge.source_port),
            ):
                if near == fqn:
                    terminated[near_port] = (short_name(edge.id), far, far_port)
        rows: list[tuple[Cell, ...]] = []
        for port in panel.interface_names():
            record = terminated.get(port)
            rows.append(
                (
                    Cell(text=port),
                    Cell(text=coupling.get(port) or BLANK),
                    Cell(text=record[0] if record is not None else BLANK),
                    context.cell_for(record[1]) if record is not None else Cell(),
                    Cell(text=record[2] if record is not None else BLANK),
                )
            )
        tables.append(
            Table(
                key=anchor("panel", fqn),
                title=f"Patch panel {short_name(fqn)}",
                columns=(
                    Column("PORT"),
                    Column("COUPLED TO"),
                    Column("CABLE"),
                    Column("FAR END"),
                    Column("FAR PORT"),
                ),
                rows=tuple(rows),
                note=(
                    "Every position of the panel, front and rear, patched or not. COUPLED TO is "
                    "the position on the other side the run continues through."
                ),
                empty="This panel declares no port.",
            )
        )
    return tuple(tables)


def _wireless_section(context: Context, scope: Scope) -> Section:
    """The BSS plan: one row per SSID per radio."""
    return Section(
        key="wireless",
        title="Wireless",
        blurb="Every BSS: which radio beacons which SSID, on what channel, into which VLAN.",
        tables=(
            table_from_listing(
                context,
                listing.bss(scope.inventory),
                key="bss",
                title="BSS and SSID plan",
                note="One row per SSID per radio: a dual-band AP serving three networks has six.",
                empty="No radio is declared here.",
                column=1,
                record_key="element",
            ),
        ),
    )


def _power_section(context: Context, scope: Scope) -> Section:
    """The PDU load schedule, from :func:`netgraph.power.power_plan`."""
    return Section(
        key="power",
        title="Power",
        blurb="Every PDU, what it feeds and how full it is.",
        tables=(
            table_from_listing(
                context,
                listing.power(scope.inventory),
                key="pdus",
                title="PDU load schedule",
                note=(
                    "LOAD is the normal-operation figure, FAILOVER what this unit carries when "
                    "its partner dies. The gap between them is the redundancy plan."
                ),
                empty="No PDU is declared here.",
                record_key="pdu",
            ),
        ),
    )


def _accounts_section(context: Context, scope: Scope) -> Section:
    """Who the network is for, from :func:`netgraph.identity.identity_plan` (§19).

    Two tables rather than one. They answer different questions — "what accounts
    exist" and "what does each grant" — and the second is the one that cannot be
    read off any single document: PEOPLE is the group's membership after the
    nesting has been walked.

    An identity gets no page of its own, unlike every piece of hardware. A page
    per device exists because a device has interfaces, addresses, a rack position
    and a power feed — a paragraph's worth of facts that will not fit in a row.
    An account is a row.
    """
    inventory = scope.inventory
    return Section(
        key="accounts",
        title="Identity",
        blurb="The accounts the network is for, and the groups that grant access.",
        tables=(
            table_from_listing(
                context,
                listing.users(inventory),
                key="users",
                title="Accounts",
                note=(
                    "GROUPS is the reverse of what the documents hold: no 'user' lists its "
                    "groups, so this is the one fact about a person their own file cannot say."
                ),
                empty="No account is declared here.",
                record_key="user",
            ),
            table_from_listing(
                context,
                listing.groups(inventory),
                key="groups",
                title="Groups",
                note=(
                    "HOLDS is what the document names; PEOPLE is how many accounts the group "
                    "reaches once nested groups have been walked."
                ),
                empty="No group is declared here.",
                record_key="group",
            ),
        ),
    )


def _external_section(context: Context, scope: Scope) -> Section:
    """The links that leave this page — the ones a scoped report would lose.

    Read off the **unfiltered** inventory, so a report of one site still names the
    uplinks to the rest of the network, and only for links between two declared
    elements: a tunnel drawn as a hub node joins its own legs, and reporting those
    as leaving the page would be a row about nothing.
    """
    inside = frozenset(scope.inventory.elements)
    outside = context.outside or context.report
    seen: set[tuple[str, str, str]] = set()
    rows: list[tuple[Cell, ...]] = []
    for layer in (DATA_LAYER, Layer.OVERLAY):
        graph = outside.graph(layer)
        for edge in graph.edges:
            local, remote, local_port, remote_port = _crossing(edge, inside)
            if local is None or remote is None:
                continue
            if not _is_element(graph, local) or not _is_element(graph, remote):
                continue
            # A cable is an edge of the cabling *and* of the overlay's underlay
            # view, so one run can be reached twice; the identity of a link is
            # its own id and its two ends.
            key = (edge.id, local, remote)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                (
                    Cell(text=edge.kind.value),
                    Cell(text=short_name(edge.id)),
                    context.cell_for(local),
                    Cell(text=local_port or BLANK),
                    context.cell_for(remote),
                    Cell(text=remote_port or BLANK),
                    context.site_cell(remote),
                )
            )
    return Section(
        key="external",
        title="Links leaving this page",
        blurb="Where this part of the network is joined to the rest of it.",
        tables=(
            Table(
                key="external-links",
                title="External links",
                columns=(
                    Column("KIND"),
                    Column("LINK"),
                    Column("LOCAL END"),
                    Column("LOCAL PORT"),
                    Column("FAR END"),
                    Column("FAR PORT"),
                    Column("DOCUMENTED ON"),
                ),
                rows=tuple(rows),
                note=(
                    "These links are deliberately absent from the diagrams and the cable "
                    "schedule above, which cover this page's elements only. A far end with no "
                    "page of its own is outside what this report documents at all."
                ),
                empty="Nothing on this page is joined to an element outside it.",
            ),
        ),
    )


def _is_element(graph: Graph, fqn: str) -> bool:
    """Is this node a declared element rather than a derived box?"""
    node = graph.nodes.get(fqn)
    return node is not None and node.is_element


def _crossing(edge: Edge, inside: frozenset[str]) -> tuple[str | None, str | None, str, str]:
    """``(local, remote, local port, remote port)`` for an edge with one end inside."""
    source_in = edge.source in inside
    target_in = edge.target in inside
    if source_in == target_in:
        return (None, None, "", "")
    if source_in:
        return (edge.source, edge.target, edge.source_port, edge.target_port)
    return (edge.target, edge.source, edge.target_port, edge.source_port)


def _findings_section(context: Context, diagnostics: Sequence[Diagnostic], *, note: str) -> Section:
    """Open validation findings, as a table nobody can miss."""
    rows = tuple(
        (
            Cell(text=str(diagnostic.severity)),
            Cell(text=diagnostic.rule, url=diagnostic.descriptor.help_uri or ""),
            context.cell_for(diagnostic.element) if diagnostic.element else Cell(),
            Cell(text=_location(diagnostic)),
            Cell(text=diagnostic.message),
        )
        for diagnostic in diagnostics
    )
    return Section(
        key="findings",
        title="Validation findings",
        tables=(
            Table(
                key="findings",
                title="Findings",
                columns=_FINDING_COLUMNS,
                rows=rows,
                note=note,
                empty="The validator reports nothing about this inventory.",
            ),
        ),
    )


def _location(diagnostic: Diagnostic) -> str:
    """``cables/links.yaml#0:17`` — where the finding is anchored."""
    if diagnostic.file is None:
        return BLANK
    text = diagnostic.file
    if diagnostic.index is not None:
        text += f"#{diagnostic.index}"
    if diagnostic.line is not None:
        text += f":{diagnostic.line}"
    return text


def _diagram_section(context: Context, scope: Scope, *, path: str) -> Section:
    """The layer diagrams of one scope, in the report's own layer order."""
    graphs = ((layer, scope.at(layer)) for layer in context.layers)
    drawings = [
        context.diagrams.draw(graph, scope=scope.name, page=path)
        for _layer, graph in graphs
        if graph is not None
    ]
    return Section(
        key="diagrams",
        title="Diagrams",
        blurb="One drawing per layer, from the same inventory as the tables below.",
        diagrams=tuple(drawings),
    )


# --------------------------------------------------------------------------- #
# A device
# --------------------------------------------------------------------------- #


def _device_page(context: Context, fqn: str) -> Page:
    """One element: what it is, where it is, what is on it and what it routes."""
    element = context.inventory.elements[fqn]
    node = context.report.node(fqn)
    sections = [
        _identity_section(context, fqn, element),
        _placement_section(context, fqn, element, node),
        _interface_section(context, fqn, element, node),
        _link_section(context, fqn),
        _routing_section(context, fqn),
        _appearance_section(context, fqn),
    ]
    return Page(
        path=context.layout.device(fqn),
        kind="device",
        title=short_name(fqn),
        summary=f"{element.kind} {fqn}",
        parent=context.layout.site(context.site_of.get(fqn, "")) or context.layout.index,
        sections=tuple(section for section in sections if section is not None),
    )


def _identity_section(context: Context, fqn: str, element: Element) -> Section:
    """Identity and metadata: the fields that say which box this is."""
    metadata = element.metadata
    source = context.inventory.source_of(fqn)
    spec = element.spec
    fields = [
        Field("Name", Cell(text=metadata.name)),
        Field("Qualified name", Cell(text=fqn)),
        Field("Kind", Cell(text=element.kind)),
        Field("Site", context.site_cell(fqn)),
        Field("Description", Cell(text=metadata.description or BLANK)),
        Field("Vendor", Cell(text=getattr(spec, "vendor", None) or BLANK)),
        Field("Model", Cell(text=getattr(spec, "model", None) or BLANK)),
        Field("Serial", Cell(text=getattr(spec, "serial", None) or BLANK)),
        Field("Declared in", Cell(text=str(source) if source is not None else BLANK)),
    ]
    tables = [
        _pairs_table(
            key="labels",
            title="Labels",
            mapping=metadata.labels,
            empty="This element carries no label.",
        ),
        _pairs_table(
            key="annotations",
            title="Annotations",
            mapping=metadata.annotations,
            empty="This element carries no annotation.",
        ),
    ]
    return Section(
        key="identity",
        title="Identity",
        fields=tuple(fields),
        tables=tuple(tables),
    )


def _pairs_table(*, key: str, title: str, mapping: Mapping[str, str], empty: str) -> Table:
    """A key/value mapping as a two-column table, sorted."""
    return Table(
        key=key,
        title=title,
        columns=(Column("KEY"), Column("VALUE")),
        rows=tuple(
            (Cell(text=name), Cell(text=value or BLANK)) for name, value in sorted(mapping.items())
        ),
        empty=empty,
    )


def _placement_section(context: Context, fqn: str, element: Element, node: Node | None) -> Section:
    """Where the element is bolted, and what it draws there."""
    site, room, rack, position, height = (
        location_of(node) if node is not None else ("", "", "", None, 1)
    )
    fields = [
        Field("Site", Cell(text=site or BLANK)),
        Field("Room", Cell(text=room or BLANK)),
        Field("Rack", Cell(text=rack or BLANK)),
        Field("Position", Cell(text=f"U{position}" if position is not None else BLANK)),
        Field("Height", Cell(text=f"{height}U")),
    ]
    power = context.power.nodes.get(fqn)
    if power is not None:
        fields.extend(
            (
                Field("Power draw", Cell(text=_watts(power.draw_watts))),
                Field("Power maximum", Cell(text=_watts(power.maximum_watts))),
                Field(
                    "Power capacity",
                    Cell(
                        text=_watts(power.capacity_watts)
                        if power.capacity_watts is not None
                        else BLANK
                    ),
                ),
            )
        )
    feeds = tuple(feed for feed in context.power.feeds if feed.element == fqn)
    return Section(
        key="placement",
        title="Placement and power",
        fields=tuple(fields),
        tables=(
            Table(
                key="feeds",
                title="Power feeds",
                columns=(
                    Column("KIND"),
                    Column("SOURCE"),
                    Column("OUTLET"),
                    Column("PORT"),
                    Column("RESERVED", "right"),
                ),
                rows=tuple(
                    (
                        Cell(text=str(feed.kind)),
                        context.cell_for(feed.source),
                        Cell(text=feed.outlet or BLANK),
                        Cell(text=feed.port or feed.peer_port or BLANK),
                        Cell(text=_watts(feed.reserved_watts)),
                    )
                    for feed in feeds
                ),
                note="A dual-corded element has one row per cord.",
                empty="No power feed is declared for this element.",
            ),
        ),
    )


def _watts(value: float) -> str:
    return f"{format_watts(value)} W" if value else BLANK


def _interface_section(context: Context, fqn: str, element: Element, node: Node | None) -> Section:
    """Interfaces with their addresses, VLANs, MTU, LAG membership and radios."""
    interfaces = getattr(element, "interfaces", [])
    ports = {port.name: port for port in (node.ports if node is not None else ())}
    member_of: dict[str, list[str]] = {}
    for interface in interfaces:
        for member in interface.members or ():
            member_of.setdefault(member, []).append(interface.name)

    rows: list[tuple[Cell, ...]] = []
    for interface in interfaces:
        port = ports.get(interface.name)
        addresses = port.addresses if port is not None else ()
        rows.append(
            (
                Cell(text=interface.name),
                Cell(text=interface.type.value),
                Cell(text="yes" if interface.enabled else "no"),
                Cell(text=str(interface.mac) if interface.mac else BLANK),
                Cell(text=str(interface.mtu) if interface.mtu is not None else BLANK),
                Cell(text=", ".join(addresses) if addresses else BLANK),
                Cell(text=_vlan_text(interface)),
                Cell(text=interface.vrf or BLANK),
                Cell(text=_aggregation_text(interface, member_of.get(interface.name, ()))),
                Cell(text=interface.description or BLANK),
            )
        )

    tables = [
        Table(
            key="interfaces",
            title="Interfaces",
            columns=(
                Column("NAME"),
                Column("TYPE"),
                Column("UP"),
                Column("MAC"),
                Column("MTU", "right"),
                Column("ADDRESSES"),
                Column("VLANS"),
                Column("VRF"),
                Column("AGGREGATION"),
                Column("DESCRIPTION"),
            ),
            rows=tuple(rows),
            note=(
                "Addresses are as configured, prefix length included. VLANS reads "
                "'mode: ids', with the native VLAN named where a trunk has one."
            ),
            empty="This element declares no interface.",
        )
    ]
    if isinstance(element, Pdu):
        tables.append(
            Table(
                key="outlets",
                title="Outlets",
                columns=(Column("OUTLET"), Column("FEEDS")),
                rows=tuple(
                    (
                        Cell(text=outlet),
                        Cell(text=", ".join(_fed_by(context, fqn, outlet)) or BLANK),
                    )
                    for outlet in element.outlet_numbers
                ),
                empty="This PDU declares no outlet.",
            )
        )
    radios = listing.bss(_only(context.inventory, fqn))
    if not radios.is_empty:
        tables.append(
            table_from_listing(
                context,
                radios,
                key="radios",
                title="Radios and BSSs",
                note="The wireless detail of this element, one row per SSID per radio.",
                column=1,
                record_key="element",
            )
        )
    return Section(key="interfaces", title="Interfaces", tables=tuple(tables))


def _only(inventory: Inventory, fqn: str) -> Inventory:
    """The inventory narrowed to one element, for a per-element listing."""
    return subset(inventory, {fqn})


def _fed_by(context: Context, pdu: str, outlet: str) -> tuple[str, ...]:
    """The elements plugged into one outlet, as short names."""
    return tuple(
        short_name(feed.element)
        for feed in context.power.feeds
        if feed.source == pdu and feed.outlet == outlet
    )


def _vlan_text(interface: object) -> str:
    """``trunk 10,20 (native 1)`` — an interface's VLAN configuration in one cell."""
    vlan = getattr(interface, "vlan", None)
    if vlan is None:
        return BLANK
    if vlan.mode.value == "access":
        return f"access {vlan.access_vlan}" if vlan.access_vlan is not None else "access"
    ids = ",".join(str(entry) for entry in sorted(vlan.trunk_vlans or ()))
    text = f"trunk {ids}" if ids else "trunk"
    if vlan.native_vlan is not None:
        text += f" (native {vlan.native_vlan})"
    return text


def _aggregation_text(interface: object, member_of: Sequence[str]) -> str:
    """What this interface is part of, or made of: a LAG, a bridge, a parent."""
    parts: list[str] = []
    members = getattr(interface, "members", None)
    if members:
        parts.append(f"members: {', '.join(members)}")
    parent = getattr(interface, "parent", None)
    if parent:
        parts.append(f"under {parent}")
    if member_of:
        parts.append(f"member of {', '.join(sorted(member_of))}")
    return "; ".join(parts) or BLANK


def _link_section(context: Context, fqn: str) -> Section:
    """Every cable and every tunnel that terminates on this element."""
    runs = _runs_through(context)
    cables: list[tuple[Cell, ...]] = []
    for edge in context.report.edges(DATA_LAYER):
        if edge.kind not in (EdgeKind.CABLE, EdgeKind.ATTACHMENT):
            continue
        near, far, near_port, far_port = _ends_of(edge, fqn)
        if near is None or far is None:
            continue
        cables.append(
            (
                Cell(text=near_port or BLANK),
                Cell(text=short_name(edge.id)),
                Cell(text=edge.label or BLANK),
                Cell(text=edge.medium or BLANK),
                Cell(text=edge.speed_text or BLANK),
                Cell(text=f"{edge.length_m:g}m" if edge.length_m is not None else BLANK),
                context.cell_for(far),
                Cell(text=far_port or BLANK),
                Cell(text=runs.get(edge.id, BLANK)),
            )
        )

    tunnels: list[tuple[Cell, ...]] = []
    for edge in context.report.edges(Layer.OVERLAY):
        view = edge.tunnel
        if view is None:
            continue
        ends = {end.element: end.interface for end in view.ends}
        if fqn not in ends:
            continue
        others = tuple(sorted(element for element in ends if element != fqn))
        tunnels.append(
            (
                Cell(text=short_name(view.fqn)),
                Cell(text=view.stack_text),
                Cell(text=ends[fqn] or BLANK),
                Cell(text=", ".join(short_name(other) for other in others) or BLANK),
                Cell(
                    text="yes" if view.tunnel.encrypts else ("underlay" if view.protected else "no")
                ),
            )
        )

    return Section(
        key="links",
        title="Links",
        blurb="What is plugged into this element, and what runs over it.",
        tables=(
            Table(
                key="cables",
                title="Cables",
                columns=(
                    Column("PORT"),
                    Column("CABLE"),
                    Column("LABEL"),
                    Column("MEDIUM"),
                    Column("SPEED", "right"),
                    Column("LENGTH", "right"),
                    Column("FAR END"),
                    Column("FAR PORT"),
                    Column("VIA"),
                ),
                rows=tuple(cables),
                note=(
                    "One row per cable that physically terminates here. VIA names the panels "
                    "the run this cable is a segment of crosses, and the two ends of that run; "
                    "the site page's cable schedule has every segment of it. An attachment row "
                    "is an adapter's upstream rather than a cable somebody pulled."
                ),
                empty="Nothing is cabled to this element.",
            ),
            Table(
                key="tunnels",
                title="Tunnels",
                columns=(
                    Column("TUNNEL"),
                    Column("STACK"),
                    Column("INTERFACE"),
                    Column("OTHER ENDS"),
                    Column("ENCRYPTED"),
                ),
                rows=tuple(dict.fromkeys(tunnels)),
                empty="No tunnel terminates on this element.",
            ),
        ),
    )


def _ends_of(edge: Edge, fqn: str) -> tuple[str | None, str | None, str, str]:
    """``(near, far, near port, far port)`` when ``edge`` touches ``fqn``."""
    if edge.source == fqn:
        return (edge.source, edge.target, edge.source_port, edge.target_port)
    if edge.target == fqn:
        return (edge.target, edge.source, edge.target_port, edge.source_port)
    return (None, None, "", "")


def _runs_through(context: Context) -> Mapping[str, str]:
    """Cable → the run it is a segment of, panel by panel.

    The panels are only visible from the *spliced* view (:attr:`Layer.L1`), where
    a link through a cross-connect is the single edge it is electrically
    equivalent to and remembers the segments it stands for. A device page lists
    the cables that physically terminate on it — which is what somebody standing
    at the device sees — so the run is the one thing it has to look up somewhere
    else. Without this the column could never fill: at the cabling layer, no edge
    crosses a panel, because the panels are nodes there.
    """
    runs: dict[str, str] = {}
    for edge in context.report.graph(Layer.L1).edges:
        patch = edge.patch
        if patch is None or not patch.hops:
            continue
        hops = ", ".join(
            f"{short_name(hop.panel)} {hop.ingress}→{hop.egress}" for hop in patch.hops
        )
        ends = f"{short_name(edge.source)}:{edge.source_port} - {short_name(edge.target)}:{edge.target_port}"
        for segment in patch.segments:
            runs[segment] = f"{hops} (run {ends})"
    return runs


def _routing_section(context: Context, fqn: str) -> Section | None:
    """VRFs, static routes and protocol adjacencies, when the element has any."""
    graph = context.report.graph(Layer.ROUTING)
    node = graph.nodes.get(fqn)
    view = node.routing if node is not None else None
    if view is None:
        return None

    adjacencies = tuple(
        (
            Cell(text=edge.adjacency.protocol),
            context.cell_for(other),
            Cell(text=edge.adjacency.peer_address or BLANK),
            Cell(text=port or BLANK),
            Cell(text=edge.adjacency.area or BLANK),
            Cell(
                text=", ".join(str(asn) for asn in edge.adjacency.asns) or BLANK,
            ),
        )
        for edge in graph.edges
        if edge.adjacency is not None
        for other, port in ((_peer_of(edge, fqn), _peer_port_of(edge, fqn)),)
        if other is not None
    )
    return Section(
        key="routing",
        title="Routing",
        blurb="The control plane this element takes part in.",
        fields=(
            Field("AS number", Cell(text=str(view.asn) if view.asn is not None else BLANK)),
            Field("Router id", Cell(text=view.router_id or BLANK)),
            Field("OSPF area", Cell(text=view.area or BLANK)),
            Field(
                "OSPF interfaces",
                Cell(text=", ".join(view.ospf_interfaces) or BLANK),
            ),
        ),
        tables=(
            Table(
                key="vrfs",
                title="VRFs",
                columns=(Column("NAME"), Column("RD"), Column("BOUND")),
                rows=tuple(
                    (
                        Cell(text=name),
                        Cell(text=rd or BLANK),
                        Cell(text="yes" if name in view.bound_vrfs else "no"),
                    )
                    for name, rd in view.vrfs
                ),
                note="An unbound VRF holds no address: it is declared but nothing is in it.",
                empty="This element declares no VRF.",
            ),
            Table(
                key="routes",
                title="Static routes",
                columns=(Column("ROUTE"),),
                rows=tuple((Cell(text=route),) for route in view.routes),
                empty="This element declares no static route.",
            ),
            Table(
                key="adjacencies",
                title="Adjacencies",
                columns=(
                    Column("PROTOCOL"),
                    Column("PEER"),
                    Column("PEER ADDRESS"),
                    Column("LOCAL PORT"),
                    Column("AREA"),
                    Column("AS NUMBERS"),
                ),
                rows=adjacencies,
                note="A BGP session names its peer address; an OSPF adjacency discovers it.",
                empty="This element has no adjacency.",
            ),
        ),
    )


def _peer_of(edge: Edge, fqn: str) -> str | None:
    if edge.source == fqn:
        return edge.target
    if edge.target == fqn:
        return edge.source
    return None


def _peer_port_of(edge: Edge, fqn: str) -> str:
    return edge.source_port if edge.source == fqn else edge.target_port


def _appearance_section(context: Context, fqn: str) -> Section:
    """Which diagrams this element appears in, as links to them."""
    links = tuple(
        Cell(
            text=f"{LAYER_TITLES.get(Layer(layer), layer)} ({layer})",
            page=page,
            fragment="diagrams",
        )
        for page, layer in context.appearances.get(fqn, ())
    )
    return Section(
        key="diagrams",
        title="Diagrams",
        blurb="The drawings this element is in.",
        links=links,
        notes=()
        if links
        else ("This element is not drawn in any of the diagrams this report holds.",),
    )
