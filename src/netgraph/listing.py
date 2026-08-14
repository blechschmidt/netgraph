"""The tables behind ``netgraph list``, and behind every report that shows one.

Each function here answers one question about an inventory — what devices are in
it, what cables, what VLANs, what address space — and answers it in three shapes
at once: column headers with their alignment, rows of formatted cells, and the
same data as records for the machine-readable formats. One derivation, three
consumers: the terminal table, ``-F json``/``-F yaml``, and the Markdown and HTML
tables ``netgraph report`` writes. A column added here appears in all of them,
and none of them can drift from another.

The listings are deliberately *derived* rather than read straight off the
documents. Device rows, VLAN membership and subnets come from
:func:`~netgraph.render.graph.build_graph`, tunnel stacks from
:func:`~netgraph.render.graph.resolve_tunnels`, prefixes from
:func:`~netgraph.subnets.subnets_of` and power from
:func:`~netgraph.power.power_plan` — the same resolutions the diagrams draw. A
host on an untagged access port is a member of that VLAN in the table for the
same reason it is one in the picture.

Every function takes an :class:`~netgraph.loader.inventory.Inventory` and nothing
else, so a *scoped* table — one site of a campus — is the same function over a
narrowed inventory (:func:`~netgraph.loader.inventory.subset`) rather than a
second implementation with a filter in it.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from netgraph.console import Align
from netgraph.errors import compact_ids
from netgraph.identity import identity_plan
from netgraph.ipam import Utilisation, format_capacity, format_utilisation
from netgraph.loader.inventory import Inventory, short_name
from netgraph.models import GROUP_KIND, Adapter, Device, format_bitrate, format_watts
from netgraph.power import format_utilisation_percent, power_plan
from netgraph.render.graph import Layer, Node, build_graph, resolve_tunnels
from netgraph.subnets import subnets_of

__all__ = [
    "LISTINGS",
    "SUBJECTS",
    "Listing",
    "bss",
    "cables",
    "devices",
    "groups",
    "length_text",
    "power",
    "subnets",
    "tunnels",
    "users",
    "utilisation",
    "vlans",
]


@dataclass(frozen=True, slots=True)
class Listing:
    """One table, in the two shapes its consumers need.

    ``rows`` holds cells already formatted for a human — a bit rate as
    ``10Gbps``, an absent value as ``-`` — and ``records`` holds the same facts
    unformatted, with ``None`` where a value is missing, keyed the way the JSON
    and YAML output spells them. Neither is derived from the other: a table cell
    that had to be parsed back into a number would be a worse contract than two
    explicit shapes built side by side.
    """

    #: Column headings, in display order.
    headers: tuple[str, ...]
    #: One alignment per column, same order.
    aligns: tuple[Align, ...]
    #: One list of cells per row, same order as :attr:`headers`.
    rows: list[list[str]]
    #: The same rows as records, for ``-F json`` and for a report.
    records: list[dict[str, Any]]

    @property
    def is_empty(self) -> bool:
        """Did the inventory hold nothing this listing is about?"""
        return not self.rows


def length_text(metres: float | None) -> str:
    """A cable length as ``12m``, or ``-`` when none is declared."""
    if metres is None:
        return "-"
    return f"{int(metres)}m" if float(metres).is_integer() else f"{metres}m"


def devices(inventory: Inventory, *, layers: Sequence[Layer] = (Layer.L1,)) -> Listing:
    """Every element the given layers draw, one row each.

    ``layers`` exists because no single view of a network holds every kind of
    element: a patch panel is spliced out of everything above the cabling
    (§15.2) and a PDU only appears in the power view. ``netgraph list devices``
    asks for layer 1 — the topology a reader means by "the devices" — and
    ``netgraph report``, which owes a page to every declared element, asks for
    the cabling and the power view together. The graphs are merged in the order
    given and the first node for an element wins, so the single-layer answer is
    exactly what it always was.
    """
    nodes: dict[str, Node] = {}
    for layer in layers:
        for fqn, node in build_graph(inventory, layer=layer).nodes.items():
            nodes.setdefault(fqn, node)
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for node in nodes.values():
        # The table has room for one address, so it shows the one that says
        # where the element sits: every host also has 127.0.0.1 and ::1.
        addresses = node.routable_addresses
        rows.append(
            [
                node.fqn,
                node.kind,
                str(len(node.ports)),
                addresses[0] if addresses else "-",
                compact_ids(node.vlans) or "-",
            ]
        )
        records.append(
            {
                "name": node.fqn,
                "shortName": node.name,
                "kind": node.kind,
                "namespace": node.namespace,
                "interfaces": len(node.ports),
                "addresses": list(addresses),
                "vlans": sorted(node.vlans),
                "source": str(inventory.source_of(node.fqn) or ""),
            }
        )
    headers = ("NAME", "KIND", "PORTS", "ADDRESS", "VLANS")
    aligns: tuple[Align, ...] = ("left", "left", "right", "left", "left")
    return Listing(headers, aligns, rows, records)


def cables(inventory: Inventory) -> Listing:
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for fqn, cable in inventory.cables.items():
        left, right = cable.endpoints
        speed = format_bitrate(cable.spec.speed) if cable.spec.speed is not None else "-"
        rows.append(
            [
                fqn,
                cable.spec.medium.value,
                speed,
                str(left),
                str(right),
                length_text(cable.spec.length_m),
            ]
        )
        records.append(
            {
                "name": fqn,
                "medium": cable.spec.medium.value,
                "speed": cable.spec.speed,
                "duplex": cable.spec.duplex.value,
                "endpoints": [str(left), str(right)],
                "lengthM": cable.spec.length_m,
                "label": cable.spec.label,
                "source": str(inventory.source_of(fqn) or ""),
            }
        )
    headers = ("NAME", "MEDIUM", "SPEED", "A END", "B END", "LENGTH")
    aligns: tuple[Align, ...] = ("left", "left", "right", "left", "left", "right")
    return Listing(headers, aligns, rows, records)


def tunnels(inventory: Inventory) -> Listing:
    """Every tunnel, with its encapsulation stack and what protects it.

    The stack comes from :func:`~netgraph.render.graph.resolve_tunnels`, the
    same resolution ``render --layer overlay`` draws, so the listing and the
    diagram cannot disagree about what runs inside what. A tunnel whose
    endpoints do not resolve is still listed — the reader is most likely running
    this command *because* something is wrong — with its stack left at its own
    type.
    """
    views = {view.fqn: view for view in resolve_tunnels(inventory)[0]}
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for fqn, tunnel in inventory.tunnels.items():
        spec = tunnel.spec
        view = views.get(fqn)
        stack = view.stack_text if view is not None else spec.type.value
        protection = "yes" if tunnel.encrypts else ("underlay" if view and view.protected else "no")
        rows.append(
            [
                fqn,
                stack,
                str(spec.vni) if spec.vni is not None else "-",
                protection,
                str(len(spec.endpoints)),
                ", ".join(str(ref) for ref in spec.endpoints),
            ]
        )
        records.append(
            {
                "name": fqn,
                "type": spec.type.value,
                "stack": list(view.stack) if view is not None else [spec.type.value],
                "layer": spec.type.layer,
                "over": view.over if view is not None else spec.over,
                "vni": spec.vni,
                "encrypted": tunnel.encrypts,
                "protected": view.protected if view is not None else tunnel.encrypts,
                "transport": spec.type.transport.value,
                "port": spec.port,
                "mtu": spec.mtu,
                "endpoints": [str(ref) for ref in spec.endpoints],
                "source": str(inventory.source_of(fqn) or ""),
            }
        )
    headers = ("NAME", "STACK", "VNI", "ENCRYPTED", "ENDS", "ENDPOINTS")
    aligns: tuple[Align, ...] = ("left", "left", "right", "left", "right", "left")
    return Listing(headers, aligns, rows, records)


def bss(inventory: Inventory) -> Listing:
    """Every BSS the inventory declares: the wireless side of ``list vlans``.

    One row per SSID per radio, because that is the unit an operator works with
    — a dual-band access point serving three networks has six of them, and each
    has its own BSSID, its own VLAN and possibly its own security. Client radios
    are listed too, with their role, so that "who is on the guest network?" is a
    question the listing can answer.
    """
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    owners: Iterable[tuple[str, Device | Adapter]] = itertools.chain(
        inventory.devices.items(), inventory.adapters.items()
    )
    for fqn, owner in owners:
        for interface in owner.interfaces:
            wireless = interface.wireless
            if wireless is None:
                continue
            for entry in wireless.bss:
                rows.append(
                    [
                        entry.ssid + (" (hidden)" if entry.hidden else ""),
                        f"{fqn}:{interface.name}",
                        wireless.role.value,
                        wireless.channel_text or "-",
                        entry.bssid or "-",
                        str(entry.vlan) if entry.vlan is not None else "-",
                        entry.security.value if entry.security is not None else "-",
                    ]
                )
                records.append(
                    {
                        "ssid": entry.ssid,
                        "element": fqn,
                        "interface": interface.name,
                        "role": wireless.role.value,
                        "band": wireless.band.value if wireless.band is not None else None,
                        "channel": wireless.channel,
                        "widthMhz": wireless.width_mhz,
                        "txPowerDbm": wireless.tx_power_dbm,
                        "bssid": entry.bssid,
                        "vlan": entry.vlan,
                        "security": entry.security.value if entry.security is not None else None,
                        "hidden": entry.hidden,
                        "source": str(inventory.source_of(fqn) or ""),
                    }
                )
    headers = ("SSID", "RADIO", "ROLE", "CHANNEL", "BSSID", "VLAN", "SECURITY")
    aligns: tuple[Align, ...] = ("left", "left", "left", "left", "left", "right", "left")
    return Listing(headers, aligns, rows, records)


def vlans(inventory: Inventory) -> Listing:
    """Every VLAN, with the elements that participate in it.

    Membership comes from the graph, so a host on an untagged access port counts
    as a member of that VLAN even though it declares no ``vlan`` block itself.
    """
    graph = build_graph(inventory)
    names: dict[int, str] = {}
    for device in inventory.devices.values():
        for definition in device.spec.vlans:
            if definition.name and definition.id not in names:
                names[definition.id] = definition.name

    members: dict[int, list[str]] = {}
    ports: dict[int, int] = {}
    for node in graph.nodes.values():
        for vlan in node.vlans:
            members.setdefault(vlan, []).append(node.fqn)
        for port in node.ports:
            for vlan in port.vlans:
                ports[vlan] = ports.get(vlan, 0) + 1

    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for vlan in sorted(members):
        elements = members[vlan]
        rows.append([str(vlan), names.get(vlan, "-"), str(len(elements)), str(ports.get(vlan, 0))])
        records.append(
            {
                "id": vlan,
                "name": names.get(vlan),
                "elements": elements,
                "interfaces": ports.get(vlan, 0),
            }
        )
    headers = ("VLAN", "NAME", "ELEMENTS", "PORTS")
    aligns: tuple[Align, ...] = ("right", "left", "right", "right")
    return Listing(headers, aligns, rows, records)


def subnets(inventory: Inventory) -> Listing:
    """Every prefix an address sits in, with the elements holding one.

    The grouping is :func:`~netgraph.subnets.subnets_of`, the same one
    ``render --layer l3`` draws and the same one ``W105``/``W106`` are about, so
    the listing and the diagram cannot disagree. Loopback and link-local
    prefixes are left out there: they are scoped to a single host or a single
    link, so listing ``127.0.0.0/8`` once per machine would say nothing about
    the addressing plan this command exists to show.

    A ``VRF`` column appears only when something is in one (§16.1). Two routing
    instances may hold the same prefix, and without the column the two rows would
    be indistinguishable; adding it unconditionally would put an empty column in
    front of every inventory that has no VRF, which is nearly all of them.
    """
    prefixes = subnets_of(inventory)
    partitioned = any(subnet.vrf for subnet in prefixes)
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for subnet in prefixes:
        vlans = sorted(subnet.vlans)
        rows.append(
            [
                *([subnet.vrf or "-"] if partitioned else []),
                subnet.prefix,
                str(subnet.version),
                str(len(subnet.addresses)),
                str(len(subnet.elements)),
                compact_ids(vlans) or "-",
            ]
        )
        record: dict[str, Any] = {
            "subnet": subnet.prefix,
            "family": subnet.family,
            "addresses": list(subnet.addresses),
            "elements": list(subnet.elements),
            "vlans": vlans,
        }
        if subnet.vrf:
            record["vrf"] = subnet.vrf
        records.append(record)
    headers = (
        *(("VRF",) if partitioned else ()),
        "SUBNET",
        "IP",
        "ADDRESSES",
        "ELEMENTS",
        "VLANS",
    )
    aligns: tuple[Align, ...] = (
        *(("left",) if partitioned else ()),
        "left",
        "right",
        "right",
        "right",
        "left",
    )
    return Listing(headers, aligns, rows, records)


def power(inventory: Inventory) -> Listing:
    """One row per PDU: its outlets, its load and how full it is (§17.6).

    Shaped after the ``netgraph ipam`` utilisation table, and for the same
    reason: the question is capacity planning, so the columns are what is there,
    what is used, what is left, and the percentage that decides whether anybody
    has to act.

    Two load columns rather than one. ``LOAD`` is the normal-operation figure —
    each dual-corded device drawing half its watts through each cord — and is what
    ``E039`` grades. ``FAILOVER`` is what this unit carries when the other one in
    the pair dies, each load counted whole. A single-fed rack has the two the
    same; an A/B pair does not, and the gap between them *is* the redundancy plan.
    """
    plan = power_plan(inventory)
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for load in plan.pdus:
        rows.append(
            [
                short_name(load.pdu),
                load.input_feed or "-",
                str(load.outlet_count),
                str(load.used_outlets),
                str(load.free_outlets),
                format_watts(load.capacity_watts) if load.capacity_watts is not None else "-",
                format_watts(load.load_watts),
                format_watts(load.failover_watts),
                format_utilisation_percent(load.utilisation),
                str(len(load.elements)),
            ]
        )
        record: dict[str, Any] = {
            "pdu": load.pdu,
            "name": load.name,
            "inputFeed": load.input_feed,
            "outlets": load.outlet_count,
            "usedOutlets": load.used_outlets,
            "freeOutlets": load.free_outlets,
            "loadWatts": round(load.load_watts, 3),
            "failoverWatts": round(load.failover_watts, 3),
            "elements": list(load.elements),
        }
        if load.capacity_watts is not None:
            record["capacityWatts"] = load.capacity_watts
            record["freeWatts"] = round(load.capacity_watts - load.load_watts, 3)
        if load.utilisation is not None:
            record["utilisation"] = round(load.utilisation, 6)
        records.append(record)
    headers = (
        "PDU",
        "FEED",
        "OUTLETS",
        "USED",
        "FREE",
        "CAPACITY",
        "LOAD",
        "FAILOVER",
        "UTIL",
        "LOADS",
    )
    aligns: tuple[Align, ...] = (
        "left",
        "left",
        "right",
        "right",
        "right",
        "right",
        "right",
        "right",
        "right",
        "right",
    )
    return Listing(headers, aligns, rows, records)


def users(inventory: Inventory) -> Listing:
    """One row per identity: the account, and what it is a member of (§19.1).

    ``GROUPS`` is the reverse of what the documents hold — no ``user`` lists its
    groups (see :mod:`netgraph.models.identity`) — which is exactly why it is
    worth a column: it is the one fact about a person that cannot be read off
    their own file.
    """
    plan = identity_plan(inventory)
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for fqn, user in inventory.users.items():
        holders = plan.groups_of(fqn)
        rows.append(
            [
                short_name(fqn),
                user.login,
                user.spec.full_name or "-",
                user.spec.email or "-",
                str(user.spec.uid) if user.spec.uid is not None else "-",
                str(user.spec.type),
                str(user.spec.status),
                str(len(user.spec.ssh_keys)),
                ", ".join(short_name(group) for group in holders) or "-",
            ]
        )
        record: dict[str, Any] = {
            "user": fqn,
            "name": user.metadata.name,
            "login": user.login,
            "type": str(user.spec.type),
            "status": str(user.spec.status),
            "sshKeys": len(user.spec.ssh_keys),
            "groups": list(holders),
        }
        if user.spec.full_name:
            record["fullName"] = user.spec.full_name
        if user.spec.email:
            record["email"] = user.spec.email
        if user.spec.uid is not None:
            record["uid"] = user.spec.uid
        records.append(record)
    headers = ("USER", "LOGIN", "FULL NAME", "EMAIL", "UID", "TYPE", "STATUS", "KEYS", "GROUPS")
    aligns: tuple[Align, ...] = (
        "left",
        "left",
        "left",
        "left",
        "right",
        "left",
        "left",
        "right",
        "left",
    )
    return Listing(headers, aligns, rows, records)


def groups(inventory: Inventory) -> Listing:
    """One row per group: what it holds directly, and who it grants to (§19.2).

    Two member columns rather than one, for the same reason the power table has
    two load columns. ``MEMBERS`` is what the document says; ``PEOPLE`` is how
    many users the group actually reaches once the nesting has been walked, which
    is the number an access rule written against it grants to and the number no
    single document holds.
    """
    plan = identity_plan(inventory)
    rows: list[list[str]] = []
    records: list[dict[str, Any]] = []
    for fqn, group in inventory.groups.items():
        direct = plan.members_of(fqn)
        reached = plan.users_in(fqn)
        nested = tuple(member for member in direct if plan.kinds.get(member) == GROUP_KIND)
        rows.append(
            [
                short_name(fqn),
                str(group.gid) if group.gid is not None else "-",
                group.spec.email or "-",
                str(len(group.spec.members)),
                str(len(nested)),
                str(len(reached)),
                ", ".join(short_name(member) for member in direct) or "-",
            ]
        )
        record: dict[str, Any] = {
            "group": fqn,
            "name": group.metadata.name,
            "members": list(direct),
            "declaredMembers": list(group.members),
            "nestedGroups": list(nested),
            "users": list(reached),
        }
        if group.gid is not None:
            record["gid"] = group.gid
        if group.spec.email:
            record["email"] = group.spec.email
        records.append(record)
    headers = ("GROUP", "GID", "EMAIL", "MEMBERS", "NESTED", "PEOPLE", "HOLDS")
    aligns: tuple[Align, ...] = ("left", "right", "left", "right", "right", "right", "left")
    return Listing(headers, aligns, rows, records)


def utilisation(rows: Iterable[Utilisation], *, aggregated: bool = False) -> Listing:
    """The ``netgraph ipam`` utilisation table: how full each prefix is.

    Not in :data:`LISTINGS`, because it is not a subject of ``netgraph list``: it
    takes the rows :func:`netgraph.ipam.build_report` derived rather than an
    inventory, since the caller has usually asked for one address family or an
    aggregation first. It lives here because the *columns* are shared — the
    terminal table, ``ipam --format csv`` and the address plan on a report's site
    page ask the same eight questions about a prefix, and a report that answered
    them in different words would be a second definition of "full".

    The ``UTIL`` cell is plain text here. ``netgraph ipam`` colours it by band
    afterwards, which is a property of a terminal and not of the table.
    """
    materialised = list(rows)
    # A VRF column only when something is in one; see :func:`subnets`.
    partitioned = any(row.vrf for row in materialised)
    headers = ["PREFIX", "IP", "VLANS", "HOSTS", "USED", "FREE", "UTIL", "DEVICES"]
    aligns: list[Align] = ["left", "right", "left", "right", "right", "right", "right", "right"]
    if partitioned:
        headers.insert(0, "VRF")
        aligns.insert(0, "left")
    if aggregated:
        headers.append("PARTS")
        aligns.append("right")

    table: list[list[str]] = []
    for row in materialised:
        cells = [
            *([row.vrf or "-"] if partitioned else []),
            row.prefix,
            str(row.version),
            compact_ids(row.vlans) or "-",
            format_capacity(row.capacity, host_bits=row.host_bits),
            str(row.assigned),
            format_capacity(row.free, host_bits=row.host_bits),
            format_utilisation(row.assigned, row.capacity),
            str(row.devices),
        ]
        if aggregated:
            cells.append(str(len(row.members)) if row.is_aggregate else "-")
        table.append(cells)
    records = [dict(row.record()) for row in materialised]
    return Listing(tuple(headers), tuple(aligns), table, records)


LISTINGS: Final[Mapping[str, Callable[[Inventory], Listing]]] = {
    "devices": devices,
    "cables": cables,
    "tunnels": tunnels,
    "vlans": vlans,
    "bss": bss,
    "subnets": subnets,
    "power": power,
    "users": users,
    "groups": groups,
}


#: What ``netgraph list`` accepts as its subject, in the order ``--help`` shows
#: them. Derived from the registry so a listing added above becomes an argument
#: without a second list to update.
SUBJECTS: Final[tuple[str, ...]] = tuple(LISTINGS)
