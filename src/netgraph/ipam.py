"""Address-space health: utilisation, free space and conflicts.

``netgraph list subnets`` answers "which prefixes exist?". This module answers
the three questions an operator asks *next*:

* **How full is it?** :func:`utilisation_of` sizes every derived prefix and
  counts what the inventory has put in it.
* **Where can I put the next device?** :func:`free_space` subtracts what is
  allocated from a prefix and hands back the holes as CIDR blocks;
  :func:`next_free` picks the first one big enough.
* **What is broken?** :func:`conflicts` reports the address-plan problems —
  and reports them by *calling the validator*, not by re-deriving them. Every
  check here is a rule of :mod:`netgraph.rules` with an ``NG-*`` alias, a
  write-up in ``docs/validation-rules.md`` and a fixture under
  ``tests/fixtures/invalid/``. There is exactly one implementation of "is this
  address plan sound", and ``netgraph validate`` and ``netgraph ipam`` are two
  views of it.

Everything here is a pure function of :func:`netgraph.subnets.subnets_of` and
an :class:`~netgraph.loader.inventory.Inventory`; nothing formats, so the CLI
is free to render a table, JSON or CSV from the same values.

Sizing
------

"Usable host addresses" is not ``num_addresses``:

* IPv4 spends the all-zeros and all-ones addresses of a prefix on the network
  and broadcast addresses — *except* on a ``/31``, where RFC 3021 gives both
  addresses to the two routers of a point-to-point link, and on a ``/32``,
  which is a host route.
* IPv6 has no broadcast address, but RFC 4291 §2.6.1 reserves the all-zeros
  interface identifier as the subnet-router anycast address. A ``/127``
  (RFC 6164) and a ``/128`` are sized like their IPv4 counterparts.

An IPv6 prefix is also far too large to print. Anything with 20 or more host
bits is rendered as a power of two by :func:`format_capacity`, so a ``/64``
occupies six columns instead of twenty.

Routing instances
-----------------

A VRF is a routing table of its own, so it is an address *space* of its own
(§16.1). Every function here therefore works per instance: a prefix in ``blue``
and the same prefix in the global table are two rows with two utilisations, and
:func:`aggregate` never merges across instances — two halves of a supernet in
different tables do not fill it, they are two plans that happen to be adjacent on
paper. :func:`free_space` and :func:`next_free` take a ``vrf`` argument for the
same reason: "what is free in 10.0.0.0/8" has a different answer per instance, and
the default — every instance at once — is the conservative one, because a block
free in one table but used in another is not a block anyone should hand out
without knowing which table they meant.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final, TypeAlias

from netgraph.config import ValidationConfig
from netgraph.loader.inventory import Inventory
from netgraph.models import GLOBAL_VRF
from netgraph.subnets import IPNetwork, Subnet, subnets_of
from netgraph.validate import Finding
from netgraph.validate import validate as run_validation

__all__ = [
    "DEFAULT_SIZE",
    "IPAM_RULES",
    "IPAddress",
    "Report",
    "Utilisation",
    "aggregate",
    "allocations_within",
    "build_report",
    "conflicts",
    "format_capacity",
    "format_utilisation",
    "free_space",
    "next_free",
    "parse_prefix",
    "parse_size",
    "usable_addresses",
    "utilisation_of",
]

#: Either family's host address, as :mod:`ipaddress` models it.
IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address

#: The rules that make up the conflict report, by canonical id. This is a
#: *filter*, not an order: the findings keep the order
#: :func:`netgraph.validate.validate` sorted them into, so ``netgraph ipam`` and
#: ``netgraph validate`` list the same problems in the same sequence.
#:
#: Four of the five checks the report makes are rules of their own; the fifth —
#: a duplicate host address inside a prefix — is *two* existing rules, because
#: the validator already distinguishes a clash within one broadcast domain
#: (``E004``, an error) from the same address claimed across two (``W106``, a
#: warning). Reproducing that distinction here would have meant a second
#: implementation that could disagree with the first.
IPAM_RULES: Final[tuple[str, ...]] = (
    "E004",  # duplicate host address within a prefix and VLAN
    "E020",  # a gateway that is not on-link
    "W106",  # the same address claimed twice in a prefix, across VLANs
    "W130",  # a prefix claimed by two broadcast domains
    "W131",  # a nested prefix used in a different broadcast domain
    "W132",  # an address outside every prefix on its own link
)

#: Host bits at or above which a capacity is printed as a power of two. 2^20 is
#: the largest count that still fits a terminal column comfortably, and it is
#: also the point past which the exact number stops being information: nobody
#: reads "18446744073709551615" as anything other than "a /64".
_POWER_OF_TWO_ABOVE: Final = 20

#: Default block size for :func:`next_free`, per family. A ``/24`` is the unit
#: an IPv4 plan is carved into, and RFC 4291 §2.5.4 makes the ``/64`` the unit
#: of an IPv6 one — SLAAC does not work in anything longer.
DEFAULT_SIZE: Final[dict[int, int]] = {4: 24, 6: 64}


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


def usable_addresses(network: IPNetwork) -> int:
    """How many hosts can be addressed in ``network``.

    See the module docstring for why this is not ``num_addresses``.
    """
    host_bits = network.max_prefixlen - network.prefixlen
    if host_bits == 0:  # a host route: the one address is the host
        return 1
    if host_bits == 1:  # RFC 3021 (v4) / RFC 6164 (v6): both ends are usable
        return 2
    if network.version == 4:
        return network.num_addresses - 2  # network and broadcast
    return network.num_addresses - 1  # subnet-router anycast (RFC 4291 §2.6.1)


def format_capacity(capacity: int, *, host_bits: int | None = None) -> str:
    """Render a host count so that a ``/64`` does not take twenty columns.

    Args:
        capacity: The count to render.
        host_bits: Host bits of the prefix it came from. Supplied by callers
            that have the prefix to hand, so the exponent is the prefix's own
            rather than one recovered from a number that is two short of it.
    """
    bits = host_bits if host_bits is not None else max(capacity, 1).bit_length()
    if bits < _POWER_OF_TWO_ABOVE:
        return str(capacity)
    return f"2^{bits}"


def format_utilisation(assigned: int, capacity: int) -> str:
    """Render ``assigned``/``capacity`` as a percentage.

    A prefix that is in use but rounds to zero is printed as ``<0.1%`` rather
    than ``0.0%``: "nothing is in here" and "two hosts are in a /64" are
    different facts, and only one of them means the prefix can be reclaimed.
    """
    if capacity <= 0:  # pragma: no cover - usable_addresses never returns 0
        return "-"
    percent = assigned * 100 / capacity
    if assigned and percent < 0.05:
        return "<0.1%"
    return f"{percent:.1f}%"


# --------------------------------------------------------------------------- #
# Utilisation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Utilisation:
    """How full one prefix is, and who is in it."""

    #: The prefix in ``10.0.0.0/24`` form.
    prefix: str
    network: IPNetwork
    #: Every VLAN an interface addressed here is a member of, ascending.
    vlans: tuple[int, ...] = ()
    #: The routing instance the prefix is in (§16.1); empty for the global one.
    #: With :attr:`prefix`, the identity of the row.
    vrf: str = GLOBAL_VRF
    #: Distinct addresses configured inside the prefix.
    assigned: int = 0
    #: Distinct elements holding one of them.
    devices: int = 0
    #: The prefixes this row stands for when :func:`aggregate` has collapsed
    #: siblings into it, in address order. Empty for a prefix that is its own row.
    members: tuple[str, ...] = ()
    #: Usable host addresses. Held rather than derived, because an aggregate
    #: row's capacity is the sum of its children's and not
    #: ``usable_addresses`` of the supernet — see :func:`aggregate`.
    capacity: int = 0

    @property
    def version(self) -> int:
        """4 or 6."""
        return self.network.version

    @property
    def family(self) -> str:
        """``ipv4`` or ``ipv6``, the schema's spelling."""
        return f"ipv{self.network.version}"

    @property
    def host_bits(self) -> int:
        return self.network.max_prefixlen - self.network.prefixlen

    @property
    def free(self) -> int:
        """Usable addresses nothing in the inventory has claimed."""
        return max(0, self.capacity - self.assigned)

    @property
    def is_aggregate(self) -> bool:
        return bool(self.members)

    @property
    def sort_key(self) -> tuple[str, int, int, int]:
        """Instance, family, network address, prefix length — as :mod:`netgraph.subnets`."""
        return (
            self.vrf,
            self.network.version,
            int(self.network.network_address),
            self.network.prefixlen,
        )

    def record(self) -> dict[str, object]:
        """The row as a mapping, for ``--format json`` and ``--format csv``."""
        return {
            "vrf": self.vrf,
            "prefix": self.prefix,
            "family": self.family,
            "vlans": list(self.vlans),
            "capacity": self.capacity,
            "assigned": self.assigned,
            "free": self.free,
            "utilisation": round(self.assigned / self.capacity, 6) if self.capacity else None,
            "devices": self.devices,
            "aggregated": list(self.members),
        }


def utilisation_of(subnets: Iterable[Subnet]) -> tuple[Utilisation, ...]:
    """Size every prefix and count what is in it.

    ``assigned`` counts *distinct addresses*, not placements: an address
    configured on two elements is one address that two elements are fighting
    over, and it consumes one slot in the prefix either way. ``W106`` and
    ``E004`` are what report the fight.
    """
    rows = [
        Utilisation(
            prefix=subnet.prefix,
            network=subnet.network,
            vlans=tuple(sorted(subnet.vlans)),
            vrf=subnet.vrf,
            assigned=len({member.ip for member in subnet.members}),
            devices=len(subnet.elements),
            capacity=usable_addresses(subnet.network),
        )
        for subnet in subnets
    ]
    rows.sort(key=lambda row: row.sort_key)
    return tuple(rows)


def aggregate(rows: Iterable[Utilisation]) -> tuple[Utilisation, ...]:
    """Collapse sibling prefixes that between them fill their supernet.

    Two prefixes are siblings when they are the two halves of one supernet.
    When *both* halves are declared, the supernet holds no address the plan has
    not already accounted for, so it can be printed as one row without the
    summary claiming space that is actually free. A supernet with only one half
    declared is left alone: collapsing it would hide a genuinely empty half.

    Applied repeatedly, so four adjacent ``/26``s become one ``/24`` rather
    than two ``/25``s.

    The aggregate's ``capacity`` is the **sum of its children's**, not
    ``usable_addresses`` of the supernet. Two ``/25``s really do have four
    unusable addresses between them; a ``/24`` drawn over them has two. The sum
    is what the plan can actually hold, so it is what is reported.
    """
    current = sorted(rows, key=lambda row: row.sort_key)
    while True:
        merged = _merge_siblings(current)
        if merged is None:
            return tuple(current)
        current = merged


def _merge_siblings(rows: Sequence[Utilisation]) -> list[Utilisation] | None:
    """One pass of :func:`aggregate`, or ``None`` when nothing merged.

    Keyed on ``(vrf, supernet)``: two halves of one supernet in two routing
    instances are not siblings, however identical their prefixes look, so
    collapsing them would summarise a supernet neither instance holds.
    """
    by_supernet: dict[tuple[str, IPNetwork], list[Utilisation]] = {}
    for row in rows:
        if row.network.prefixlen == 0:  # nothing contains a default route
            continue
        by_supernet.setdefault((row.vrf, row.network.supernet()), []).append(row)

    pairs = {
        key: group
        for key, group in by_supernet.items()
        if len(group) == 2 and group[0].network != group[1].network
    }
    if not pairs:
        return None

    consumed = {(row.vrf, row.prefix) for group in pairs.values() for row in group}
    result = [row for row in rows if (row.vrf, row.prefix) not in consumed]
    result.extend(_combine(supernet, group) for (_, supernet), group in pairs.items())
    result.sort(key=lambda row: row.sort_key)
    return result


def _combine(supernet: IPNetwork, group: Sequence[Utilisation]) -> Utilisation:
    """One row standing for both halves of ``supernet``.

    ``group`` arrives in address order — it was filled by walking rows that were
    already sorted — so the members are concatenated rather than re-sorted. A
    string sort would put ``10.0.0.128/26`` before ``10.0.0.64/26``.
    """
    members: list[str] = []
    for row in group:
        members.extend(row.members or (row.prefix,))
    return Utilisation(
        prefix=str(supernet),
        network=supernet,
        vlans=tuple(sorted({vlan for row in group for vlan in row.vlans})),
        # Every member is in one instance by construction; see _merge_siblings.
        vrf=group[0].vrf,
        assigned=sum(row.assigned for row in group),
        # Elements are counted per row, so an element addressed in both halves
        # is counted twice. Summing is the honest option available here: the
        # placements themselves are not carried this far, and a max() would
        # under-report a genuinely wider aggregate.
        devices=sum(row.devices for row in group),
        members=tuple(members),
        capacity=sum(row.capacity for row in group),
    )


# --------------------------------------------------------------------------- #
# Free space
# --------------------------------------------------------------------------- #


def allocations_within(
    prefix: IPNetwork, subnets: Iterable[Subnet], *, vrf: str | None = None
) -> tuple[IPNetwork, ...]:
    """What ``prefix`` has already been carved up into, in address order.

    A subnet nested inside ``prefix`` consumes the whole of itself: allocation
    happens a subnet at a time, and the seven free addresses left in a ``/29``
    holding one host are not space anyone will hand to another department.

    An address that falls inside ``prefix`` while its *own* prefix does not —
    a summary route configured as ``10.0.0.1/8`` inside a ``10.0.0.0/16``
    plan — cannot consume its prefix without consuming the whole plan, so it
    consumes a host route instead.

    Args:
        vrf: Count only what this routing instance has allocated — ``""`` for the
            global one. ``None``, the default, counts every instance, which is the
            conservative answer: a block free in one table and used in another is
            not free to hand out without saying which table.
    """
    ranges: list[tuple[int, int]] = []
    for subnet in subnets:
        if subnet.network.version != prefix.version:
            continue
        if vrf is not None and subnet.vrf != vrf:
            continue
        if _contains(prefix, subnet.network):
            ranges.append(
                (int(subnet.network.network_address), int(subnet.network.broadcast_address))
            )
            continue
        for member in subnet.members:
            address = int(ipaddress.ip_address(member.ip))
            if int(prefix.network_address) <= address <= int(prefix.broadcast_address):
                ranges.append((address, address))

    blocks: list[IPNetwork] = []
    for first, last in _merge_ranges(ranges):
        blocks.extend(_range_to_cidr(first, last, prefix.version))
    return tuple(blocks)


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> Iterator[tuple[int, int]]:
    """Sort inclusive integer ranges and fuse the ones that touch or overlap.

    Done on integers rather than through :func:`ipaddress.collapse_addresses`
    because a host route inside a wider allocation has to *disappear* into it,
    which range merging does naturally, and because the two families then need
    no separate code path.
    """
    ordered = sorted(ranges)
    if not ordered:
        return
    start, end = ordered[0]
    for first, last in ordered[1:]:
        if first > end + 1:
            yield start, end
            start, end = first, last
            continue
        end = max(end, last)
    yield start, end


def free_space(
    prefix: IPNetwork, subnets: Iterable[Subnet], *, vrf: str | None = None
) -> tuple[IPNetwork, ...]:
    """The unallocated ranges inside ``prefix``, as the fewest CIDR blocks.

    Ascending, and already collapsed: two adjacent free ``/25``s are reported
    as the ``/24`` they form, because that is the block an operator can
    actually hand out.

    Args:
        vrf: Which routing instance to answer for; see :func:`allocations_within`.
    """
    allocated = allocations_within(prefix, subnets, vrf=vrf)
    first = int(prefix.network_address)
    last = int(prefix.broadcast_address)
    version = prefix.version

    free: list[IPNetwork] = []
    cursor = first
    for block in allocated:
        start = int(block.network_address)
        if start > cursor:
            free.extend(_range_to_cidr(cursor, start - 1, version))
        cursor = max(cursor, int(block.broadcast_address) + 1)
    if cursor <= last:
        free.extend(_range_to_cidr(cursor, last, version))
    return tuple(free)


def next_free(
    prefix: IPNetwork, size: int, subnets: Iterable[Subnet], *, vrf: str | None = None
) -> IPNetwork | None:
    """The first free block of ``/size`` inside ``prefix``, or ``None``.

    ``free_space`` hands back CIDR blocks, so a free block can hold a ``/size``
    exactly when it is at least that wide — and when it is, its own network
    address is already aligned for one. Walking the free list in address order
    therefore finds the lowest such block without enumerating candidates, which
    matters when ``prefix`` is an IPv6 ``/32`` holding 2^32 possible ``/64``s.
    """
    if not 0 <= size <= prefix.max_prefixlen or size < prefix.prefixlen:
        return None
    for block in free_space(prefix, subnets, vrf=vrf):
        if block.prefixlen <= size:
            return ipaddress.ip_network((block.network_address, size))
    return None


def _contains(outer: IPNetwork, inner: IPNetwork) -> bool:
    """Is ``inner`` inside ``outer``? Same-version networks only."""
    return int(inner.network_address) >= int(outer.network_address) and int(
        inner.broadcast_address
    ) <= int(outer.broadcast_address)


def _range_to_cidr(first: int, last: int, version: int) -> Iterator[IPNetwork]:
    """The CIDR blocks exactly covering the inclusive address range.

    The two families are spelled out rather than parameterised because
    :func:`ipaddress.summarize_address_range` is only defined for a *pair* of
    addresses of one family, and expressing that through a variable factory
    loses exactly the guarantee that makes the call safe.
    """
    if version == 4:
        yield from ipaddress.summarize_address_range(
            ipaddress.IPv4Address(first), ipaddress.IPv4Address(last)
        )
        return
    yield from ipaddress.summarize_address_range(
        ipaddress.IPv6Address(first), ipaddress.IPv6Address(last)
    )


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #


def conflicts(inventory: Inventory, config: ValidationConfig | None = None) -> tuple[Finding, ...]:
    """The address-plan findings of :func:`netgraph.validate.validate`.

    Filtered rather than recomputed, so a suppression in ``netgraph.toml`` or a
    ``netgraph/ignore`` annotation silences a finding here exactly as it does
    in ``netgraph validate``, and a rule re-graded to ``info`` is reported at
    the severity the inventory chose.
    """
    wanted = frozenset(IPAM_RULES)
    return tuple(finding for finding in run_validation(inventory, config) if finding.rule in wanted)


# --------------------------------------------------------------------------- #
# The whole report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Report:
    """Everything ``netgraph ipam`` prints, before it is rendered."""

    rows: tuple[Utilisation, ...] = ()
    findings: tuple[Finding, ...] = ()
    #: True when :func:`aggregate` was applied to :attr:`rows`.
    aggregated: bool = False
    subnets: tuple[Subnet, ...] = field(default=(), repr=False)

    @property
    def assigned(self) -> int:
        return sum(row.assigned for row in self.rows)

    def of_family(self, version: int) -> tuple[Utilisation, ...]:
        return tuple(row for row in self.rows if row.version == version)


def build_report(
    inventory: Inventory,
    config: ValidationConfig | None = None,
    *,
    aggregated: bool = False,
) -> Report:
    """Assemble the utilisation table and the conflict list for ``inventory``."""
    subnets = subnets_of(inventory)
    rows = utilisation_of(subnets)
    return Report(
        rows=aggregate(rows) if aggregated else rows,
        findings=conflicts(inventory, config),
        aggregated=aggregated,
        subnets=subnets,
    )


# --------------------------------------------------------------------------- #
# Parsing the command line's prefixes
# --------------------------------------------------------------------------- #


def parse_prefix(text: str) -> IPNetwork:
    """Read a ``--free``/``--next-free`` argument as a prefix.

    Host bits are allowed and dropped — ``--free 10.0.0.1/24`` means the
    ``10.0.0.0/24`` the operator was looking at — because the address is what
    an operator has on the clipboard.

    Raises:
        ValueError: The text is not a prefix, with the reason.
    """
    candidate = text.strip()
    if not candidate:
        raise ValueError("expected a prefix such as 10.0.0.0/16")
    try:
        return ipaddress.ip_network(candidate, strict=False)
    except ValueError as exc:
        raise ValueError(f"{candidate!r} is not an IP prefix: {exc}") from None


def parse_size(text: str, version: int) -> int:
    """Read a ``--size`` argument as a prefix length.

    ``24`` and ``/24`` are both accepted; the leading slash is how the length
    is written everywhere else, so rejecting it would be pedantry.

    Raises:
        ValueError: The text is not a prefix length for ``version``.
    """
    limit = 32 if version == 4 else 128
    candidate = text.strip().lstrip("/")
    if not candidate.isascii() or not candidate.isdigit():
        raise ValueError(f"{text.strip()!r} is not a prefix length; write it as /24 or 24")
    size = int(candidate)
    if size > limit:
        raise ValueError(f"/{size} is not an IPv{version} prefix length; the longest is /{limit}")
    return size
