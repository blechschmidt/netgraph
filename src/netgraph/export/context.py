"""What every emitter is handed, and the two questions they all ask.

An emitter in this package is a pure function ``ExportContext -> str``. The
context carries the resolved inventory, the graphs it was asked for — already
narrowed by the same :class:`~netgraph.render.graph.FilterSpec` a render uses —
the format-specific options, and the :class:`~netgraph.export.manifest.Recorder`
it writes its omissions to.

Two questions recur across four of the five formats, and both are answered here
so the answers cannot drift:

**"Which addresses identify this element on the network?"**
    :func:`element_addresses`. Loopback and link-local are already excluded by
    :attr:`~netgraph.render.graph.PortView.routable_addresses`, which is the
    same exclusion the layer-3 diagram and ``netgraph ipam`` apply — one
    definition of "routable", three consumers.

**"Which single address would I reach it on?"**
    :func:`management_address`. An ``/etc/hosts`` fragment can list every
    address a device has; ``ansible_host`` and a scrape target are one field
    each, so something has to choose. The choice is spelled out in that
    function's docstring rather than left to configuration order, because a
    monitoring target that moves when an interface is added is worse than one
    that is wrong in a predictable way.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Final

from netgraph.export.manifest import Reason, Recorder
from netgraph.export.names import MAX_DNS_NAME, dns_name_parts
from netgraph.loader.inventory import Inventory
from netgraph.render.graph import Graph, Layer, Node, PortView
from netgraph.render.icons import IconTheme
from netgraph.subnets import IPNetwork

__all__ = [
    "Address",
    "ExportContext",
    "ExportOptions",
    "HostName",
    "NameRegistry",
    "element_addresses",
    "elements_of",
    "location_of",
    "management_address",
    "record_addressless",
    "record_dangling",
    "reverse_zone_of",
]

#: An interface that exists to manage the box rather than to carry its traffic.
#: Matched case-insensitively against the interface name *and* its description,
#: because vendors spell it in the name (``mgmt0``, ``Management1``) and
#: operators spell it in the description ("out-of-band management").
_MANAGEMENT_HINT: Final = re.compile(
    r"\b(?:mgmt|management|oob|idrac|ilo|bmc)\b|^mgmt|^management", re.IGNORECASE
)

#: Vendor-neutral spellings of a loopback interface. A loopback *interface*
#: often carries a perfectly routable address — a router ID — and is the right
#: thing to manage a router on when it has one, ranking just behind an explicit
#: management port.
_LOOPBACK_HINT: Final = re.compile(r"^(?:lo|loopback)\d*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Address:
    """One configured address, tied to the interface that holds it."""

    #: Fully-qualified name of the element.
    element: str
    #: Interface name within it.
    interface: str
    #: Position of the interface in ``spec.interfaces``, which is the tie-break
    #: every selection here falls back on: it is the order the author wrote.
    index: int
    #: The address without its prefix length, canonically formatted.
    ip: str
    #: The address as configured, ``10.0.0.1/24``.
    cidr: str
    #: The prefix the address sits in, for the reverse zone it belongs to.
    network: IPNetwork

    @property
    def version(self) -> int:
        """4 or 6."""
        return self.network.version

    @property
    def record_type(self) -> str:
        """``A`` or ``AAAA`` — the DNS type that holds this family."""
        return "A" if self.version == 4 else "AAAA"

    @property
    def packed(self) -> tuple[int, int]:
        """Sort key: family first, then numeric value.

        The integer form keeps the key comparable across families, which a mix
        of :class:`ipaddress.IPv4Address` and :class:`ipaddress.IPv6Address`
        would not be — the same reasoning as
        :attr:`netgraph.subnets.Subnet.sort_key`.
        """
        return (self.version, int(ipaddress.ip_address(self.ip)))

    @property
    def target(self) -> str:
        """The address as a network target, IPv6 bracketed.

        ``2001:db8::1`` is ambiguous the moment a port is appended to it, so
        the brackets of RFC 3986 §3.2.2 are applied here rather than in each
        caller that builds a ``host:port``.
        """
        return f"[{self.ip}]" if self.version == 6 else self.ip


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """Everything the command line settles that is not the filter.

    One flat record rather than one per format: the CLI parses a single option
    set, an emitter reads the fields it cares about, and a format that grows an
    option does not change any other signature.
    """

    # -- dns-zone --------------------------------------------------------
    #: Absolute zone origin, e.g. ``example.com.``. Required by ``dns-zone``.
    origin: str = ""
    ttl: int = 3600
    #: SOA ``MNAME`` — the primary nameserver. Defaults to ``ns.<origin>``.
    soa_mname: str = ""
    #: SOA ``RNAME`` — the responsible mailbox, in DNS form. Defaults to
    #: ``hostmaster.<origin>``.
    soa_rname: str = ""
    #: SOA serial. Deliberately *not* derived from the clock: an export must be
    #: byte-identical over an unchanged inventory, and a date-based serial would
    #: make every run a diff. Bump it from the pipeline that publishes the zone.
    soa_serial: int = 1
    soa_refresh: int = 86400
    soa_retry: int = 7200
    soa_expire: int = 3600000
    soa_minimum: int = 3600
    #: NS records to write at the apex. Empty means "only the SOA MNAME".
    nameservers: tuple[str, ...] = ()
    #: ``forward``, ``reverse`` or ``all``.
    zones: str = "all"

    # -- prometheus-sd ---------------------------------------------------
    #: Port appended to each target, or ``None`` for a bare address.
    port: int | None = None
    #: Static labels merged into every target group, from ``--label K=V``.
    labels: Mapping[str, str] = field(default_factory=dict)

    # -- cable-list ------------------------------------------------------
    #: ``csv`` or ``markdown``.
    table_format: str = "csv"

    # -- power -----------------------------------------------------------
    #: How the load schedule is laid out: ``csv`` for the sheet somebody signs,
    #: ``json`` for the same rows plus the per-PDU and per-PSE totals (§17.7).
    schedule_format: str = "csv"

    # -- drawio ----------------------------------------------------------
    #: Which view the diagram draws, as :class:`~netgraph.render.graph.Layer`
    #: spells it. Unlike every other format here, ``drawio`` draws *one* view
    #: and the reader chooses which — a cabling diagram and a routing diagram
    #: are different pictures, and a stakeholder is being asked about one.
    view: str = Layer.L1.value
    #: The icon theme inlined into the file, or ``None`` for coloured boxes.
    icons: IconTheme | None = None
    #: Write the deflate+base64 encoding draw.io's desktop app writes by
    #: default. Off by default here: a plain diagram is one that reviews.
    compress: bool = False
    #: Draw a container frame per namespace.
    frames: bool = True
    #: Does the exported diagram hold every element of the view? Set by the CLI
    #: from whether a filter was given, and stamped into the file: importing a
    #: filtered diagram must never read a missing cell as a deletion.
    complete: bool = True

    @property
    def wants_forward(self) -> bool:
        return self.zones in {"all", "forward"}

    @property
    def wants_reverse(self) -> bool:
        return self.zones in {"all", "reverse"}

    def mname(self) -> str:
        """The SOA MNAME, defaulted from the origin when none was given."""
        return self.soa_mname or f"ns.{self.origin}"

    def rname(self) -> str:
        """The SOA RNAME, defaulted from the origin when none was given."""
        return self.soa_rname or f"hostmaster.{self.origin}"

    def apex_nameservers(self) -> tuple[str, ...]:
        """The NS set of the zone: what was asked for, or the MNAME alone.

        A zone with no NS record at its apex is invalid (RFC 1035 §6.1), and a
        nameserver will refuse to load it — so the fallback is not a
        convenience, it is what keeps the default output usable.
        """
        return self.nameservers or (self.mname(),)


@dataclass(frozen=True, slots=True)
class ExportContext:
    """The resolved input of one export."""

    inventory: Inventory
    #: The graphs the emitter declared it needs, already filtered.
    graphs: Mapping[Layer, Graph]
    options: ExportOptions
    recorder: Recorder

    def at(self, layer: Layer) -> Graph:
        """The graph built at ``layer``.

        Raises:
            KeyError: The emitter asked for a layer it did not declare in its
                :attr:`~netgraph.export.Exporter.layers`. A programming error,
                not a user one.
        """
        return self.graphs[layer]


def elements_of(graph: Graph) -> tuple[Node, ...]:
    """Every element node of ``graph``, in canonical order.

    By fully-qualified name, never by load order: two runs over the same tree
    must produce the same bytes, and the loader's order depends on directory
    iteration.
    """
    return tuple(sorted(graph.element_nodes, key=lambda node: node.fqn))


def element_addresses(node: Node) -> tuple[Address, ...]:
    """Every routable address of ``node``, in canonical order.

    Ordered by family and then by numeric value, so an artefact lists IPv4
    before IPv6 and does not reshuffle when an interface is renamed. Loopback
    and link-local addresses never appear: they identify the element to itself,
    not to the network, and an ``/etc/hosts`` entry mapping a name to
    ``127.0.0.1`` on every machine is actively wrong.

    One address appears **once**, even when two interfaces of the element carry
    it — an anycast address on two VLAN interfaces, say, which the validator
    accepts because the two sit in different broadcast domains. A name-to-address
    mapping is a set: two identical A records are one RRSet with a duplicate in
    it (RFC 2181 §5), and two identical hosts lines are a resolver reading the
    first. The interface that declared it first wins, so the per-interface view
    an emitter wants is still reachable through :attr:`Node.ports`.
    """
    unique: dict[str, Address] = {}
    for address in sorted(
        _addresses(node), key=lambda entry: (entry.packed, entry.index, entry.interface)
    ):
        unique.setdefault(address.ip, address)
    return tuple(unique.values())


def _addresses(node: Node) -> Iterator[Address]:
    for index, port in enumerate(node.ports):
        for cidr in port.routable_addresses:
            interface = ipaddress.ip_interface(cidr)
            yield Address(
                element=node.fqn,
                interface=port.name,
                index=index,
                ip=str(interface.ip),
                cidr=cidr,
                network=interface.network,
            )


def management_address(node: Node) -> Address | None:
    """The one address to reach ``node`` on, or ``None`` when there is none.

    A single field — ``ansible_host``, a scrape target — cannot hold the four
    addresses a router has, so the choice is made by a fixed ranking rather
    than by whichever interface happens to be first:

    1. **An interface that says it is for management.** ``mgmt0``, ``Mgmt1``,
       ``idrac``, or any interface whose description mentions management or
       out-of-band. This is the address an operator means by "reach the box".
    2. **A loopback interface with a routable address** — a router ID. It is
       up whenever any path to the device is, which is exactly the property a
       management target wants.
    3. **Anything else**, in the order the interfaces were declared.

    Within each tier, IPv4 comes before IPv6: a dual-stacked estate still has
    tooling that only speaks v4, and picking the v6 address for a host that has
    both would break it silently. Ties below that are broken by interface order
    and then by the address itself, so the answer never depends on a dict.
    """
    candidates = element_addresses(node)
    if not candidates:
        return None
    ranks = {port.name: _management_rank(port) for port in node.ports}
    return min(
        candidates,
        key=lambda address: (
            ranks.get(address.interface, 2),
            address.version,
            address.index,
            address.packed,
        ),
    )


def _management_rank(port: PortView) -> int:
    """0 for a management port, 1 for a loopback, 2 for anything else."""
    haystack = f"{port.name} {port.description or ''}"
    if _MANAGEMENT_HINT.search(haystack):
        return 0
    if _LOOPBACK_HINT.match(port.name):
        return 1
    return 2


def record_addressless(node: Node, recorder: Recorder) -> None:
    """Record why ``node`` contributed no address, distinguishing the two cases.

    "This server has no address" and "this server has only a link-local
    address" look the same in the output and are entirely different problems,
    so they are two reasons rather than one. A port that is simply unnumbered
    is reported per interface: it is normal on a switch, and rolling it up per
    element would hide the one interface that was *meant* to be numbered.
    """
    configured = [port for port in node.ports if port.addresses]
    if not configured:
        recorder.skip(
            node.fqn,
            Reason.NO_ADDRESS,
            f"{node.kind} declares {len(node.ports)} interface(s), none with an address",
        )
        return
    recorder.skip(
        node.fqn,
        Reason.NOT_ROUTABLE,
        "every configured address is loopback or link-local, so none of them "
        "identifies the element on this network",
    )
    for port in sorted(configured, key=lambda entry: entry.name):
        recorder.skip(
            f"{node.fqn}:{port.name}",
            Reason.UNNUMBERED,
            f"{len(port.addresses)} address(es), all loopback or link-local",
        )


@dataclass(frozen=True, slots=True)
class HostName:
    """An element's name folded into the DNS grammar, both spellings."""

    #: Every label, innermost first: ``("sw-01", "access", "north", "sites")``.
    labels: tuple[str, ...]
    #: How many leading labels are the element's *own* name. Usually one, but
    #: ``metadata.name`` may itself hold dots: ``core.example.com`` is one name
    #: under the §4.1 grammar and three labels here.
    own: int = 1
    #: Is this element the one that gets to publish the short alias? False when
    #: an earlier element already claimed it, or when the format publishes no
    #: aliases at all; see :class:`NameRegistry`.
    owns_short: bool = True

    @property
    def fqdn(self) -> str:
        """``sw-01.access.north.sites`` — the canonical name."""
        return ".".join(self.labels)

    @property
    def short(self) -> str:
        """``sw-01`` — the element's own name, which may not be unique.

        *All* of its own labels, never just the first: the element called
        ``core.example.com`` is not called ``core``, and publishing ``core``
        for it would take the alias from the element that really does have that
        name and point ``ping core`` at the wrong machine.
        """
        return ".".join(self.labels[: self.own])

    @property
    def aliases(self) -> tuple[str, ...]:
        """The names to publish, canonical first.

        The qualified name leads because it is the one derived from a unique
        fully-qualified name; the short name follows as an alias, which is the
        ``/etc/hosts`` convention (canonical name first, aliases after) and the
        one that makes ``ping sw-01`` work in a flat inventory.

        The alias is dropped when it is not this element's to publish — either
        because it is the qualified name already (a root-level element), or
        because another element claimed it first. Two ``sw-01``s in two
        namespaces are entirely legal in an inventory and entirely ambiguous in
        a name service, and a resolver given both would silently answer with
        whichever it read first.
        """
        if not self.owns_short or self.short == self.fqdn:
            return (self.fqdn,)
        return (self.fqdn, self.short)


@dataclass(slots=True)
class NameRegistry:
    """Hands out names, and refuses to hand out the same one twice.

    Folding is not injective: two elements whose qualified names differ only
    past the 63rd character of a label, or only in characters that fold to
    ``-``, come out with the same spelling. Every format here keys on that
    spelling — a hosts line, an A record, an Ansible host, a Prometheus
    ``instance`` — and every one of them would resolve the collision by
    silently keeping one of the two.

    So the registry keeps it. **Qualified names and short aliases live in one
    namespace**, because they end up in one namespace in the artefact: a
    root-level element called ``sw-01`` and the alias of ``sites/sw-01`` are
    the same string to a resolver, and issuing both would map one name to two
    machines. The first element in canonical order wins; a loser that cannot
    have its qualified name is skipped entirely, and a loser that can keeps it
    and loses only the alias.

    ``aliases`` says whether the format publishes short names at all. Only
    ``hosts`` does — a zone file and an Ansible inventory hold one name per
    element by construction — so the other three neither claim aliases nor
    report losing one, which would otherwise fill their manifests with skips
    describing something they never emit.

    ``origin`` is the suffix the names will be written under, when there is
    one. Checking the RFC 1035 length bound here rather than in the caller is
    what keeps a rejected element out of the manifest's ``rewritten`` list: a
    rename is only recorded once the name is certain to be published.
    """

    recorder: Recorder
    #: Zone origin the names hang under, for the total-length bound.
    origin: str = ""
    #: Does this format publish a short alias beside the qualified name?
    aliases: bool = False
    #: Every name issued so far -> the element that owns it.
    _issued: dict[str, str] = field(default_factory=dict)

    def register(self, node: Node) -> HostName | None:
        """``node``'s name, or ``None`` when it cannot be published.

        Returns:
            ``None`` when nothing survives the fold, when the name would exceed
            the RFC 1035 length bound under :attr:`origin`, or when another
            element already owns it. A skip has been recorded in every case;
            inventing a name would put a record in a zone file pointing at a
            machine nobody asked about.
        """
        own, outer = dns_name_parts(node.fqn)
        if not own:
            self.recorder.skip(
                node.fqn,
                Reason.NOT_REPRESENTABLE,
                "nothing survives folding the name into DNS labels; rename the element, "
                "or the directory that gives it its namespace",
            )
            return None

        name = HostName(labels=own + outer, own=len(own), owns_short=self.aliases)
        if not self._fits(name.fqdn):
            self.recorder.skip(
                node.fqn,
                Reason.NOT_REPRESENTABLE,
                f"'{self._under_origin(name.fqdn)}' exceeds the {MAX_DNS_NAME} octets "
                f"RFC 1035 §2.3.4 allows for a domain name",
            )
            return None

        owner = self._issued.get(name.fqdn)
        if owner is not None:
            self.recorder.skip(
                node.fqn,
                Reason.NAME_COLLISION,
                f"folds to '{name.fqdn}', the name already published for '{owner}'; "
                f"this format holds one element per name",
            )
            return None
        self._issued[name.fqdn] = node.fqn
        self.recorder.rewrite(
            node.fqn, field="hostname", original=_natural_name(node.fqn), rewritten=name.fqdn
        )

        if not self.aliases or name.short == name.fqdn:
            return name
        alias_owner = self._issued.setdefault(name.short, node.fqn)
        if alias_owner != node.fqn:
            self.recorder.skip(
                node.fqn,
                Reason.NAME_COLLISION,
                f"the short alias '{name.short}' is already published for '{alias_owner}', "
                f"so only the qualified name '{name.fqdn}' is written for this element",
            )
            return replace(name, owns_short=False)
        return name

    def _fits(self, fqdn: str) -> bool:
        return len(self._under_origin(fqdn)) <= MAX_DNS_NAME

    def _under_origin(self, fqdn: str) -> str:
        return f"{fqdn}.{self.origin}".rstrip(".") if self.origin else fqdn


def _natural_name(fqn: str) -> str:
    """The dotted spelling of ``fqn`` before any folding, for the manifest.

    ``sites/Building A/sw 1`` reads ``sw 1.Building A.sites`` here, so a reader
    comparing it with the folded form sees exactly which segments changed
    rather than having to reverse the namespace in their head.
    """
    segments = [segment for segment in fqn.split("/") if segment]
    if not segments:
        return fqn
    *namespace, name = segments
    return ".".join([name, *reversed(namespace)])


def record_dangling(graph: Graph, recorder: Recorder) -> None:
    """Carry the graph's own dropped links into the export manifest.

    :attr:`~netgraph.render.graph.Graph.dangling` already holds every cable and
    tunnel whose endpoint did not resolve, one message per problem, each opening
    with the element's fully-qualified name. Only ``--force`` gets this far —
    the validator refuses such an inventory first — but when it does, the
    artefact is missing links and the manifest is the only place that can say
    so.
    """
    for message in graph.dangling:
        subject, separator, detail = message.partition(": ")
        recorder.skip(
            subject if separator else message,
            Reason.UNRESOLVED,
            detail if separator else "",
        )


def location_of(node: Node) -> tuple[str, str, str, int | None, int]:
    """``(site, room, rack, position, height)`` of an element, blanks for unset.

    ``metadata.location`` is optional and every one of its fields is, so this
    flattens the four cases — no location, a location naming only a site, one
    naming a rack but no position, and a fully placed element — into one shape
    the emitters can format without four conditionals each.
    """
    element = node.element
    location = element.metadata.location if element is not None else None
    if location is None:
        return ("", "", "", None, 1)
    return (
        location.site or "",
        location.room or "",
        location.rack or "",
        location.position,
        location.height,
    )


def reverse_zone_of(network: IPNetwork) -> str:
    """The reverse zone that holds every address in ``network``.

    Delegation happens on octet boundaries for IPv4 (RFC 1035 §3.5) and on
    nibble boundaries for IPv6 (RFC 3596 §2.5), so a prefix that is not on one
    is covered by the *shorter* zone that contains it: a ``/22`` lives in the
    ``/16``'s zone, and a ``/30`` in the ``/24``'s. Rounding the other way
    would emit a zone name that covers only part of the prefix, and the records
    outside it would be unloadable.

    ``10.0.0.0/24`` gives ``0.0.10.in-addr.arpa.`` and ``2001:db8::/48`` gives
    ``0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa.``. A prefix shorter than one unit —
    ``0.0.0.0/0`` — gives the bare arpa zone, which is honest: nothing narrower
    contains it.

    The last unit is always left out of the zone name, even for a ``/32`` or a
    ``/128``. A host route is still an address that needs an owner name inside
    some zone, and a zone whose name *is* the address leaves nothing to write
    the PTR under — so a ``/32`` is served from the ``/24``'s zone, which is
    also where an operator would look for it.
    """
    if network.version == 4:
        units = str(network.network_address).split(".")[: min(network.prefixlen // 8, 3)]
        suffix = "in-addr.arpa."
    else:
        expanded = ipaddress.IPv6Address(network.network_address).exploded.replace(":", "")
        units = list(expanded[: min(network.prefixlen // 4, 31)])
        suffix = "ip6.arpa."
    return "".join(f"{unit}." for unit in reversed(units)) + suffix
