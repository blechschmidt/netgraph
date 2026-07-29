"""RFC 1035 zone files: the forward zone, and the reverse zones that match it.

Two halves of one answer. The forward zone maps every element to the addresses
it holds; the reverse zones map every one of those addresses back. They are
emitted together because the failure this export exists to prevent is exactly
the two disagreeing — a PTR left behind when a device was re-addressed in the
inventory, which no amount of care in a hand-maintained zone prevents.

Prefixes are **not** re-derived here. :func:`netgraph.subnets.subnets_of` is the
one implementation of "which prefixes exist, and who sits in them?", shared with
the layer-3 diagram, ``netgraph list subnets`` and ``netgraph ipam``; the reverse
zones are a regrouping of its output onto delegation boundaries
(:func:`~netgraph.export.context.reverse_zone_of`) and nothing more.

Layout
------

Every zone is written under an explicit ``$ORIGIN`` with relative owner names,
which is what a nameserver operator expects to read and what keeps the file
short. A run with ``--zones all`` concatenates the forward zone and each reverse
zone into one document separated by banners; that is a convenience for reading
and diffing, **not** something to hand to a nameserver, which loads one zone per
file. Use ``--zones forward`` and ``--zones reverse`` to split them, or cut on
the banners.

What it drops
-------------

Everything that is not a name-to-address mapping: VLANs, cabling, hardware,
interface detail. Only one name per element reaches the zone — the qualified
one — because a second A record under the short name would collide the moment
two namespaces hold the same device name, and a zone file cannot report a
collision, it can only serve one of them. Names are folded into DNS labels
(:mod:`netgraph.export.names`) and every fold is recorded in the manifest.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from netgraph.export.context import (
    Address,
    ExportContext,
    ExportOptions,
    HostName,
    NameRegistry,
    element_addresses,
    elements_of,
    record_addressless,
    reverse_zone_of,
)
from netgraph.export.header import comment_header
from netgraph.render.graph import Layer
from netgraph.subnets import Subnet, subnets_of

__all__ = ["emit"]

#: Column the class and type start in. Wide enough for a qualified name of a
#: few namespaces without pushing the type off the eye's path; a name past it
#: takes the next column instead — nothing is ever truncated.
_OWNER_COLUMN = 36


@dataclass(frozen=True, slots=True)
class _Host:
    """One element that reached the zone, with everything both halves need."""

    fqn: str
    name: HostName
    addresses: tuple[Address, ...]


@dataclass(frozen=True, slots=True)
class _Record:
    """One resource record, before it is laid out."""

    owner: str
    type: str
    data: str
    #: What the record is about, for the canonical sort. A forward record sorts
    #: by name; a PTR by the numeric value of the address, so a reverse zone
    #: reads in address order rather than in ``10, 100, 11`` order.
    sort_key: tuple[object, ...]


def emit(context: ExportContext) -> str:
    """Render the zone file(s), newline-terminated."""
    options = context.options
    hosts = _resolve(context)

    sections: list[list[str]] = []
    if options.wants_forward:
        sections.append(_forward_zone(options, hosts))
    if options.wants_reverse:
        sections.extend(_reverse_zones(context, hosts))

    lines = list(_document_header(options))
    for index, section in enumerate(sections):
        if index:
            lines.append("")
        lines.extend(section)
    return "".join(f"{line}\n" for line in lines)


def _resolve(context: ExportContext) -> Mapping[str, _Host]:
    """Every selected element that can appear in the zone, resolved once.

    Both halves need the same three facts — the folded name, the addresses, and
    whether the name still fits under the origin — and resolving them twice
    would report every rename twice in the manifest.
    """
    recorder = context.recorder
    origin = context.options.origin
    hosts: dict[str, _Host] = {}
    # The registry applies the RFC 1035 total-length bound under the origin, so
    # a name that cannot be written is rejected before it is reported as a
    # rename that never happened.
    registry = NameRegistry(recorder, origin=origin)

    nodes = elements_of(context.at(Layer.L1))
    recorder.considered = len(nodes)
    for node in nodes:
        addresses = element_addresses(node)
        if not addresses:
            record_addressless(node, recorder)
            continue
        name = registry.register(node)
        if name is None:
            continue
        recorder.emitted += 1
        hosts[node.fqn] = _Host(fqn=node.fqn, name=name, addresses=addresses)
    return hosts


def _document_header(options: ExportOptions) -> Iterator[str]:
    which = {
        "all": "the forward zone and every reverse zone it implies",
        "forward": "the forward zone only",
        "reverse": "the reverse zones only",
    }[options.zones]
    yield from comment_header(
        ";",
        "dns-zone",
        (
            f"Origin {options.origin}, TTL {options.ttl}. This document holds {which}.",
            "A nameserver loads one zone per file: split on the ';; zone' banners below,",
            "or re-run with '--zones forward' and '--zones reverse'.",
            "Only address records are emitted. VLANs, cabling and hardware detail have",
            "no representation here; the manifest on stderr lists what was left out.",
        ),
    )


# --------------------------------------------------------------------------- #
# Forward zone
# --------------------------------------------------------------------------- #


def _forward_zone(options: ExportOptions, hosts: Mapping[str, _Host]) -> list[str]:
    records = [
        _Record(
            owner=host.name.fqdn,
            type=address.record_type,
            data=address.ip,
            sort_key=(host.name.fqdn, *address.packed),
        )
        for host in hosts.values()
        for address in host.addresses
    ]
    return [
        ";; zone: forward",
        f"$ORIGIN {options.origin}",
        f"$TTL {options.ttl}",
        "",
        *_apex(options),
        "",
        *_layout(records),
    ]


def _apex(options: ExportOptions) -> Iterator[str]:
    """The SOA and NS records every zone must open with (RFC 1035 §6.1)."""
    indent = " " * (_OWNER_COLUMN - 1)
    yield f"@{indent}IN  SOA {options.mname()} {options.rname()} ("
    fields = (
        (options.soa_serial, "serial"),
        (options.soa_refresh, "refresh"),
        (options.soa_retry, "retry"),
        (options.soa_expire, "expire"),
        (options.soa_minimum, "minimum"),
    )
    width = max(len(str(value)) for value, _ in fields) + 2
    for index, (value, label) in enumerate(fields):
        token = f"{value} )" if index == len(fields) - 1 else str(value)
        yield f"{' ' * (_OWNER_COLUMN + 8)}{token.ljust(width)}  ; {label}"
    for nameserver in options.apex_nameservers():
        yield f"@{indent}IN  NS  {nameserver}"


# --------------------------------------------------------------------------- #
# Reverse zones
# --------------------------------------------------------------------------- #


def _reverse_zones(context: ExportContext, hosts: Mapping[str, _Host]) -> Iterator[list[str]]:
    """One section per delegation boundary, in address order."""
    options = context.options
    grouped: dict[str, list[Subnet]] = {}
    order: dict[str, tuple[int, int, int]] = {}

    for subnet in subnets_of(context.inventory):
        narrowed = subnet.restricted_to(hosts)
        if not narrowed.members:
            continue
        zone = reverse_zone_of(subnet.network)
        grouped.setdefault(zone, []).append(narrowed)
        order[zone] = min(order.get(zone, subnet.sort_key), subnet.sort_key)

    for zone in sorted(grouped, key=lambda name: order[name]):
        records = _pointers(grouped[zone], zone, hosts, options)
        if not records:
            continue
        yield [
            f";; zone: reverse {zone}",
            f"$ORIGIN {zone}",
            f"$TTL {options.ttl}",
            "",
            *_apex(options),
            "",
            *_layout(records),
        ]


def _pointers(
    subnets: Sequence[Subnet],
    zone: str,
    hosts: Mapping[str, _Host],
    options: ExportOptions,
) -> list[_Record]:
    """The PTR records of one reverse zone.

    An address held by two *elements* — ``E004``, which ``--force`` still
    exports — yields two PTRs rather than one arbitrary winner. That is legal
    (RFC 1034 §3.6.2 permits several) and it is the honest reading: the
    inventory really does claim both, and the operator seeing two PTRs will go
    and fix it.

    The same address on two *interfaces of one element* is a different matter
    and yields one PTR. ``subnets_of`` records a placement per interface, which
    is right for a utilisation figure and wrong for a name: an RRSet is a set
    (RFC 2181 §5), so the second copy is redundant at best and rejected by
    ``named-checkzone`` at worst. :func:`element_addresses` makes the same
    reduction for the forward half, and the two must agree.
    """
    records: list[_Record] = []
    seen: set[tuple[str, str]] = set()
    for subnet in subnets:
        for member in subnet.members:
            host = hosts.get(member.element)
            if host is None:  # pragma: no cover - restricted_to already removed these
                continue
            address = ipaddress.ip_address(member.ip)
            target = f"{host.name.fqdn}.{options.origin}"
            if (member.ip, target) in seen:
                continue
            seen.add((member.ip, target))
            records.append(
                _Record(
                    owner=_relative(address.reverse_pointer, zone),
                    type="PTR",
                    data=target,
                    sort_key=(int(address), target),
                )
            )
    return records


def _relative(pointer: str, zone: str) -> str:
    """``1.0.0.10.in-addr.arpa`` under ``0.0.10.in-addr.arpa.`` becomes ``1``.

    A pointer that does not sit under the zone is left absolute. That cannot
    happen for a zone derived from the address's own prefix, but it is the safe
    fallback: an absolute owner name is always correct, where a wrongly
    relativised one silently points somewhere else.
    """
    suffix = zone.rstrip(".")
    if pointer.endswith(f".{suffix}"):
        return pointer[: -len(suffix) - 1]
    return f"{pointer}."


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


def _layout(records: Iterable[_Record]) -> list[str]:
    """Records in canonical order, columns padded to the widest owner name."""
    ordered = sorted(records, key=lambda record: (record.sort_key, record.type, record.data))
    if not ordered:
        return []
    owners = max(_OWNER_COLUMN, max(len(record.owner) for record in ordered) + 2)
    types = max(len(record.type) for record in ordered)
    return [
        f"{record.owner.ljust(owners)}IN  {record.type.ljust(types)}  {record.data}"
        for record in ordered
    ]
