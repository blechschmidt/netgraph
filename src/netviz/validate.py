"""Semantic validation of a loaded inventory (``docs/schema.md`` §10).

The loader guarantees that every document *parses* and matches the schema of
its own ``kind``. This module answers the next question: do the documents agree
with **each other**? Cables must point at interfaces that exist, MAC and IP
addresses must be unique where uniqueness is physically required, and the two
ends of a link must be configured compatibly.

    findings = validate(inventory, config.validation)
    if any(finding.severity.is_fatal for finding in findings):
        ...

Design notes
------------

*Total, never raising.* :func:`validate` reports; it does not raise. A caller
decides what to do with the findings, exactly as with
:class:`~netviz.loader.inventory.LoadError`. The only exceptions that escape
come from a broken *configuration*, which is a tooling error rather than an
inventory error.

*One finding per problem.* A duplicate address shared by five interfaces is one
finding naming all five, not five findings. Group findings are anchored at the
**first** declaration in load order and name every participant in the message,
which keeps output stable across runs and lets a reader see the whole conflict
at once.

*Suppression is a filter, not a branch.* Rules never inspect the configuration;
the engine skips disabled rules and drops annotated findings after the fact, so
a check cannot accidentally behave differently when a rule is re-graded.

Suppressing a rule
------------------

Per inventory, in ``netviz.toml`` (see :mod:`netviz.config`)::

    [validate]
    ignore = ["W103"]

Per element, with an annotation on any element the finding names::

    metadata:
      name: spare-switch
      annotations:
        netviz/ignore: "W103, E004"     # or "*" for every rule

Because a finding carries every element it involves, annotating either end of a
cable is enough to silence a finding about that cable.
"""

from __future__ import annotations

import ipaddress
import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, TypeAlias

from netviz.annotations import select_area_members
from netviz.config import ValidationConfig
from netviz.connectivity import Graph as ConnectivityGraph
from netviz.connectivity import Separator, reachable, separators
from netviz.errors import count_text
from netviz.expectations import Declaration, Expectation, declarations, expectation_names
from netviz.identity import IdentityPlan, identity_plan
from netviz.loader.inventory import Inventory, SourceLocation, namespace_of
from netviz.loader.provenance import Site
from netviz.models import (
    AGGREGATE_TYPES,
    FRONT,
    GLOBAL_VRF,
    PATCHPANEL_KIND,
    Adapter,
    Annotation,
    Area,
    BgpConfig,
    BgpNeighbor,
    Bss,
    Cable,
    Computer,
    Device,
    Duplex,
    Element,
    FirewallConfig,
    FirewallHook,
    FirewallRule,
    Hub,
    Interface,
    InterfaceRef,
    InterfaceType,
    IPv4Address,
    IPv6Address,
    Medium,
    NatRule,
    Note,
    PanelSide,
    PatchPanel,
    PolicyAction,
    PolicyRule,
    RouteTable,
    RoutingConfig,
    Server,
    StaticRoute,
    Style,
    Switch,
    Tunnel,
    UserStatus,
    UserType,
    VlanConfig,
    VlanMode,
    VrfDefinition,
    WirelessConfig,
    Zone,
    split_panel_port,
)
from netviz.models.metadata import Location
from netviz.models.power import format_watts
from netviz.models.routing import AddressFamily
from netviz.models.scalars import MAX_VLAN_ID, MIN_VLAN_ID, format_bitrate
from netviz.models.style import hex_colour
from netviz.power import FeedKind, PowerPlan, UnresolvedReason, Uplink, power_plan
from netviz.rules import RULES, WILDCARD, Rule, Severity, resolve_rule_id
from netviz.subnets import (
    AddressPlacement,
    IPNetwork,
    Subnet,
    is_routable_address,
    subnets_of,
)

__all__ = [
    "IGNORE_ANNOTATIONS",
    "Finding",
    "Severity",
    "errors_only",
    "has_errors",
    "summarise",
    "validate",
]

#: Annotation keys whose value lists the rules to suppress on an element. The
#: ``netviz.dev/`` spelling matches the label prefix reserved in §3.1; the
#: short one is what people actually type.
IGNORE_ANNOTATIONS: Final[tuple[str, ...]] = ("netviz/ignore", "netviz.dev/ignore")

#: Separators accepted inside an ignore annotation: ``"E001, W102 W103"``.
_TOKEN_SEPARATORS: Final = ",;"

#: Longest list of names spelled out in a message before it is abbreviated.
_MAX_LISTED: Final = 8

#: *Active* elements that own interfaces: everything that can be configured
#: (§4.2). A patch panel owns ports too, but configures nothing, so every rule
#: about configuration reads :attr:`_Context.owners` and never sees one.
InterfaceOwner: TypeAlias = Device | Adapter

#: The same set as a tuple, for ``isinstance``.
_OWNER_TYPES: Final = (Device, Adapter)

#: Everything a *cable* may terminate on, panels included (§15.1).
CableTarget: TypeAlias = Device | Adapter | PatchPanel

#: The same set as a tuple, for ``isinstance``.
_CABLE_TARGET_TYPES: Final = (Device, Adapter, PatchPanel)

#: Elements a cable may reach that are end systems rather than network gear
#: (``W115``). An adapter is one: it is a port of the host it hangs off.
_HOST_TYPES: Final = (Computer, Server, Adapter)

#: Interface types a cable can terminate on (``NG-C009``), as an enum set.
_CABLEABLE_TYPES: Final[frozenset[InterfaceType]] = frozenset(
    itype for itype in InterfaceType if itype.is_cableable
)

#: Element kinds an adapter must not hang off (``NG-X007``). An adapter is a
#: port of the host it plugs into; network gear takes a cable.
_NOT_A_HOST_TYPES: Final = (Hub, Switch)

#: One port cabled into a hub's collision domain (``NG-H005``): the element that
#: owns it, the port as ``element:interface``, and the prefixes it is in.
_HubPeer: TypeAlias = tuple[str, str, frozenset["IPNetwork"]]

#: What ``E004`` groups an address by: the network stack it is in, the routing
#: instance, the address, its prefix and its broadcast domain — as the objects
#: the model layer already built rather than their text. The two partitions lead
#: because they are the coarsest: two addresses in different network namespaces
#: (§23.1) or different VRFs (§16.1) are never in conflict. The stack is ``""``
#: ``("", "")`` for a machine's initial namespace and ``(element, name)`` for a
#: declared one, because a namespace name means nothing outside the machine that
#: runs it while every machine's initial namespace is on the same wire.
_AddressKey: TypeAlias = tuple[
    tuple[str, str], str, "ipaddress.IPv4Address | ipaddress.IPv6Address", "IPNetwork", int | None
]

#: Either family's host address, as :mod:`ipaddress` models it. Spelled out here
#: rather than imported from :mod:`netviz.ipam`, which imports *this* module.
_IPAddress: TypeAlias = "ipaddress.IPv4Address | ipaddress.IPv6Address"


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Finding:
    """One semantic problem, tied to the rule that found it and the file it is in."""

    #: Canonical rule id, e.g. ``E002`` (see :mod:`netviz.rules`).
    rule: str
    #: Severity *after* configuration overrides and ``--strict`` are applied.
    severity: Severity
    #: One-line, human-readable description naming the elements involved.
    message: str
    #: Where the finding is anchored; ``None`` only for whole-inventory findings.
    source: SourceLocation | None = None
    #: Fully-qualified names of every element involved, anchor first. Any of
    #: them may suppress the finding through its ``netviz/ignore`` annotation.
    elements: tuple[str, ...] = ()
    #: Field path inside the anchor document, ``()`` for the document as a whole.
    field_path: tuple[str | int, ...] = ()

    @property
    def file(self) -> str | None:
        """The source file, relative to the inventory root."""
        return self.source.relative if self.source is not None else None

    @property
    def site(self) -> Site | None:
        """Where the offending field was written: file, line and column.

        Narrower than :attr:`source`, which only names the document. Following
        the loader's provenance also means a value a device inherited from a
        template resolves to the *template's* file, so an editor or a CI
        annotation sends the reader to the line they have to change.

        ``None`` when the element was built without a parsed document behind it
        — an inventory assembled in memory has no line to point at.
        """
        return self.source.locate(self.field_path) if self.source is not None else None

    @property
    def location(self) -> str:
        """Provenance in ``sites/hq/sw.yaml#0:17`` notation (``-`` when unknown)."""
        return str(self.source) if self.source is not None else "-"

    @property
    def element(self) -> str | None:
        """The element the finding is anchored to."""
        return self.elements[0] if self.elements else None

    @property
    def sort_key(self) -> tuple[str, int, int, int, str, str]:
        """Order findings by location, then severity, then rule id."""
        source = self.source
        return (
            source.relative if source is not None else "",
            source.index if source is not None else -1,
            source.line if source is not None and source.line is not None else -1,
            self.severity.rank,
            self.rule,
            self.message,
        )

    def __str__(self) -> str:
        return f"{self.location}: {self.severity}: {self.rule}: {self.message}"


def has_errors(findings: Iterable[Finding]) -> bool:
    """Does any finding fail the run?"""
    return any(finding.severity.is_fatal for finding in findings)


def errors_only(findings: Iterable[Finding]) -> list[Finding]:
    """The subset of ``findings`` that fails the run."""
    return [finding for finding in findings if finding.severity.is_fatal]


def summarise(findings: Iterable[Finding]) -> dict[Severity, int]:
    """Count findings per severity, in severity order."""
    counts = dict.fromkeys(Severity, 0)
    for finding in findings:
        counts[finding.severity] += 1
    return counts


@dataclass(frozen=True, slots=True)
class _Draft:
    """A finding before the engine attaches its rule id and severity.

    Checks describe *what* is wrong; the engine decides how loudly to say it.
    """

    message: str
    #: Anchor first; every entry can suppress the finding.
    elements: tuple[str, ...] = ()
    field_path: tuple[str | int, ...] = ()


#: A check reads the prepared context and yields one draft per problem.
Check = Callable[["_Context"], Iterator[_Draft]]


# --------------------------------------------------------------------------- #
# Prepared context
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """One end of a cable, resolved against the inventory."""

    cable_fqn: str
    cable: Cable
    ref: InterfaceRef
    #: Position within ``spec.endpoints`` (0 or 1), for the field path.
    index: int
    #: The element the device part names, when it resolves to something a cable
    #: may terminate on — a device, an adapter or a patch panel.
    owner_fqn: str | None = None
    owner: CableTarget | None = None
    #: The interface the reference names; ``None`` for an unknown interface and
    #: for an adapter upstream port, which carries no L2/L3 configuration (§8.1).
    interface: Interface | None = None
    #: The reference names the adapter's upstream port rather than an interface.
    is_upstream: bool = False
    #: Candidates when the device name stayed ambiguous (§2.2).
    ambiguous: tuple[str, ...] = ()
    #: Set when the name resolved to an element that owns no interfaces.
    wrong_kind: str | None = None

    @property
    def resolved(self) -> bool:
        """Does this endpoint name a real port on a real element?"""
        return self.owner_fqn is not None and (self.interface is not None or self.is_upstream)

    @property
    def panel(self) -> PatchPanel | None:
        """The patch panel this endpoint lands on, or ``None`` (§15.1).

        A panel port is an ordinary cable endpoint, so it goes through the same
        resolution as every other one; what it is *not* is a place where any
        configuration lives, which is why the rules that read a port's VLAN,
        MTU or addresses ask this first.
        """
        return self.owner if isinstance(self.owner, PatchPanel) else None

    @property
    def active_owner(self) -> InterfaceOwner | None:
        """The owner when it is an active element, never a patch panel."""
        return self.owner if isinstance(self.owner, _OWNER_TYPES) else None

    @property
    def port(self) -> str:
        """The endpoint as ``element:interface``, fully qualified where known."""
        return f"{self.owner_fqn or self.ref.device}:{self.ref.interface}"

    @property
    def field_path(self) -> tuple[str | int, ...]:
        # The model sorts ``spec.endpoints`` (§7.1), so ``index`` is a position
        # in the *canonical* order and the document may have written it
        # somewhere else. Reporting the canonical index would point a CI
        # annotation at the other end of the cable.
        written = self.ref.document_index
        return ("spec", "endpoints", self.index if written is None else written)


@dataclass(frozen=True, slots=True)
class _TunnelEnd:
    """One endpoint of a tunnel, resolved against the inventory (§14.3).

    The shape mirrors :class:`_Endpoint` deliberately: a tunnel endpoint is the
    same ``device:interface`` reference a cable uses, so the diagnostics a
    reader gets for a typo should be the same too.
    """

    tunnel_fqn: str
    tunnel: Tunnel
    ref: InterfaceRef
    #: Position within ``spec.endpoints``, for the field path.
    index: int
    #: The element the device part names, when it resolves to an interface owner.
    owner_fqn: str | None = None
    owner: InterfaceOwner | None = None
    #: The interface the reference names, when the element declares one.
    interface: Interface | None = None
    #: Candidates when the device name stayed ambiguous (§2.2).
    ambiguous: tuple[str, ...] = ()
    #: Set when the name resolved to an element that owns no interfaces.
    wrong_kind: str | None = None

    @property
    def resolved(self) -> bool:
        """Does this endpoint name a real interface on a real element?"""
        return self.owner_fqn is not None and self.interface is not None

    @property
    def port(self) -> str:
        """The endpoint as ``element:interface``, fully qualified where known."""
        return f"{self.owner_fqn or self.ref.device}:{self.ref.interface}"

    @property
    def field_path(self) -> tuple[str | int, ...]:
        # Sorted on load like a cable's (§14.3); see :attr:`_Endpoint.field_path`.
        written = self.ref.document_index
        return ("spec", "endpoints", self.index if written is None else written)


@dataclass(frozen=True, slots=True)
class _Encapsulation:
    """One tunnel's ``spec.over``, resolved (§14.3).

    Four rules read it — ``E018`` (does it name a tunnel at all), ``E019`` (do
    the references loop), ``W125`` (does the underlay actually reach both ends)
    and ``W127`` (does anything in the stack encrypt) — so it is resolved once
    and shared, which is also what keeps the validator and the renderer
    agreeing about what runs inside what.
    """

    tunnel_fqn: str
    tunnel: Tunnel
    #: The reference as written, e.g. ``ipsec-ab``.
    ref: str
    #: The tunnel the reference names, when it resolves to exactly one.
    over_fqn: str | None = None
    over: Tunnel | None = None
    #: Candidates when the name stayed ambiguous (§2.2).
    ambiguous: tuple[str, ...] = ()
    #: Set when the name resolved to an element that is not a tunnel.
    wrong_kind: str | None = None

    @property
    def field_path(self) -> tuple[str | int, ...]:
        return ("spec", "over")


@dataclass(frozen=True, slots=True)
class _Attachment:
    """One adapter's ``upstream.attached_to`` reference, resolved (§8.2).

    Resolved once here because four rules read it — ``E015`` (does it resolve at
    all), ``E013`` and ``W123`` (is the host attachment declared exactly once),
    ``E014`` (do the attachments loop) and ``W124`` (is the target a host) — and
    they must agree with the renderer about what the reference denotes.
    """

    adapter_fqn: str
    adapter: Adapter
    #: The reference as written, e.g. ``laptop`` or ``sites/hq/laptop``.
    ref: str
    #: The element the reference names, when it resolves to exactly one.
    host_fqn: str | None = None
    host: Element | None = None
    #: Candidates when the name stayed ambiguous (§2.2).
    ambiguous: tuple[str, ...] = ()

    @property
    def field_path(self) -> tuple[str | int, ...]:
        return ("spec", "upstream", "attached_to")


@dataclass(frozen=True, slots=True)
class _Placement:
    """One element's ``metadata.location``, resolved (§3.2).

    Three rules read it — ``E025`` (do two things overlap), ``E026`` (does
    anything stick out of the top) and ``E027`` (do two elements disagree about
    how tall the rack is) — so the elements are grouped by rack once, here.
    """

    fqn: str
    element: Element
    location: Location

    @property
    def rack(self) -> tuple[str, str, str] | None:
        return self.location.rack_key

    @property
    def field_path(self) -> tuple[str | int, ...]:
        return ("metadata", "location")

    def path(self, key: str) -> tuple[str | int, ...]:
        return ("metadata", "location", key)


@dataclass(frozen=True, slots=True)
class _RouteEntry:
    """One static route, tied to the device that configures it (§16.3)."""

    owner_fqn: str
    owner: Device
    #: Position in ``spec.routes``, for the field path of a finding.
    index: int
    route: StaticRoute

    @property
    def vrf(self) -> str:
        """The routing instance the route is in; global when it names none."""
        return self.route.vrf or GLOBAL_VRF

    @property
    def where(self) -> str:
        """``'rtr-a' route 0.0.0.0/0 via 203.0.113.1`` — the subject of a message."""
        return f"{_q(self.owner_fqn)} route {self.route.describe()}"

    def path(self, *suffix: str | int) -> tuple[str | int, ...]:
        return ("spec", "routes", self.index, *suffix)


@dataclass(frozen=True, slots=True)
class _Session:
    """One configured BGP session, with its far end resolved by address (§16.6).

    Resolution is by *address*, which is what the device is configured with. A
    session whose address matches nothing in the inventory is not an error — an
    eBGP peer may be a transit provider nobody declares here — so ``peer_fqn``
    is ``None`` rather than the check refusing to build.
    """

    owner_fqn: str
    owner: Device
    #: Position in ``spec.routing.bgp.neighbors``.
    index: int
    neighbor: BgpNeighbor
    #: The element holding the neighbour address, when one does.
    peer_fqn: str | None = None
    #: The interface of that element the address sits on.
    peer_interface: str | None = None

    @property
    def local_asn(self) -> int:
        """The AS the configuring device declares itself in."""
        bgp = self.owner.spec.routing.bgp if self.owner.spec.routing is not None else None
        # A neighbour can only be reached through a ``bgp`` block, so there is one.
        assert bgp is not None
        return bgp.asn

    @property
    def resolved(self) -> bool:
        return self.peer_fqn is not None

    @property
    def peer_port(self) -> str:
        """``rtr-b:lo0`` — where the neighbour address was found."""
        return f"{self.peer_fqn}:{self.peer_interface}"

    def path(self, *suffix: str | int) -> tuple[str | int, ...]:
        return ("spec", "routing", "bgp", "neighbors", self.index, *suffix)


@dataclass(frozen=True, slots=True)
class _Vrf:
    """One VRF a device declares, with the interfaces bound to it (§16.1)."""

    owner_fqn: str
    owner: Device
    index: int
    vrf: VrfDefinition
    #: Names of the interfaces that bind to it, in declaration order.
    interfaces: tuple[str, ...] = ()
    #: Positions in ``spec.routes`` of the routes placed in it.
    routes: tuple[int, ...] = ()

    def path(self, *suffix: str | int) -> tuple[str | int, ...]:
        return ("spec", "vrfs", self.index, *suffix)


@dataclass(frozen=True, slots=True)
class _PolicyDatabase:
    """One device's policy-based routing: its tables and the rules over them (§16.4).

    Held per device rather than flattened per rule, unlike :class:`_RouteEntry`,
    because every question worth asking about a policy database is about the
    database and not about one line of it: whether a table anything selects holds
    a route, whether a table any route is placed in is ever selected, and whether
    a rule is reachable at all. None of those can be answered from one rule.
    """

    owner_fqn: str
    owner: Device
    #: The declared tables, in declaration order. The reserved three are not
    #: here: nobody declared them, so no finding can be anchored at them.
    tables: tuple[RouteTable, ...]
    #: The policy database in declaration order — *not* priority order, since a
    #: finding's field path is a position in the document.
    rules: tuple[PolicyRule, ...]

    def table_path(self, index: int, *suffix: str | int) -> tuple[str | int, ...]:
        return ("spec", "route_tables", index, *suffix)

    def rule_path(self, rule: PolicyRule, *suffix: str | int) -> tuple[str | int, ...]:
        """The path of ``rule`` in the document it was written in.

        By identity rather than by a stored index, because the checks walk the
        database in *priority* order — which is the order the device walks it —
        and the document order is what a field path has to name.
        """
        return ("spec", "routing_policy", self.rules.index(rule), *suffix)


@dataclass(frozen=True, slots=True)
class _Firewall:
    """One device's filtering: its zones and the policy over them (§24).

    Held per device for the same reason :class:`_PolicyDatabase` is: every
    question worth asking here is about the whole of it. Whether a zone holds an
    interface, whether an interface is in a zone, whether a rule can be reached
    at all, and whether the marks the filter writes are the marks the routing
    policy reads — none of them can be answered from one rule.

    A device with ``spec.zones`` and no ``spec.firewall`` is here too, because
    ``W150`` and ``W151`` are about the zones alone: a partition nobody has
    written policy over yet is still a partition, and an empty one in it is
    still worth saying.
    """

    owner_fqn: str
    owner: Device
    #: The declared zones, in declaration order. :data:`LOCAL_ZONE` is not
    #: here: nobody declared it, so no finding can be anchored at it.
    zones: tuple[Zone, ...]
    #: The filter policy in declaration order — *not* priority order, since a
    #: finding's field path is a position in the document.
    rules: tuple[FirewallRule, ...]

    @property
    def policy(self) -> FirewallConfig | None:
        return self.owner.spec.firewall

    def zone_path(self, index: int, *suffix: str | int) -> tuple[str | int, ...]:
        return ("spec", "zones", index, *suffix)

    def rule_path(self, rule: FirewallRule, *suffix: str | int) -> tuple[str | int, ...]:
        """The path of ``rule`` in the document it was written in.

        By identity rather than by a stored index, because the checks walk the
        chain in *priority* order — which is the order the device walks it — and
        the document order is what a field path has to name.
        """
        return ("spec", "firewall", "rules", self.rules.index(rule), *suffix)


@dataclass(frozen=True, slots=True)
class _Context:
    """Everything the checks need, computed once.

    Building this up front keeps each rule a straight loop over prepared data
    instead of a nest of repeated lookups, and guarantees that all rules agree
    on how a reference resolves.
    """

    inventory: Inventory
    #: Devices and adapters in load order (cables own no interfaces).
    owners: Mapping[str, InterfaceOwner]
    endpoints: tuple[_Endpoint, ...]
    #: Each cable with its two endpoints, in load order. Ten rules read the
    #: endpoints two at a time; grouping them here rather than in a helper is
    #: what the rest of this class is for.
    endpoint_pairs: tuple[tuple[str, _Endpoint, _Endpoint], ...]
    #: Every adapter that declares an ``attached_to``, in load order.
    attachments: tuple[_Attachment, ...]
    #: Every tunnel endpoint, in load order (§14).
    tunnel_ends: tuple[_TunnelEnd, ...]
    #: Every tunnel that declares an ``over``, in load order.
    encapsulations: tuple[_Encapsulation, ...]
    #: ``(owner fqn, interface name)`` -> the tunnels terminating on it.
    tunnel_ports: Mapping[tuple[str, str], tuple[str, ...]]
    #: ``(owner fqn, interface name)`` -> the endpoints landing on it, in load
    #: order. Active elements only; a panel position lands in
    #: :attr:`panel_terminations` so ``E002`` and ``E022`` cannot both fire.
    terminations: Mapping[tuple[str, str], tuple[_Endpoint, ...]]
    #: The patch panels of the inventory, in load order (§15).
    panels: Mapping[str, PatchPanel]
    #: ``(panel fqn, position)`` -> the endpoints landing on it, in load order.
    panel_terminations: Mapping[tuple[str, str], tuple[_Endpoint, ...]]
    #: Every element that declares ``metadata.location``, in load order.
    placements: tuple[_Placement, ...]
    #: Element fqns reachable through a cable or an adapter attachment.
    connected: frozenset[str]
    #: Per element: interface name -> the LAG that aggregates it (§10.6).
    lag_masters: Mapping[str, Mapping[str, Interface]]
    #: Per element: interface name -> representative of its stacking group.
    stacking_groups: Mapping[str, Mapping[str, str]]
    #: Per element: interface name -> the interface, for stacking lookups.
    by_name: Mapping[str, Mapping[str, Interface]]
    #: Per element: interface name -> every ``lag``/``bridge`` aggregating it,
    #: in declaration order. ``lag_masters`` answers "which configuration
    #: governs this port"; this answers "how many claim it", which is what
    #: ``E008`` is about.
    aggregated_by: Mapping[str, Mapping[str, tuple[str, ...]]]
    #: Per element: the rule ids its annotations suppress.
    suppressions: Mapping[str, frozenset[str]]
    #: Every prefix an address sits in (:mod:`netviz.subnets`), in prefix
    #: order. This is the same grouping the layer-3 graph draws, so a finding
    #: about a subnet and the diagram of it can never disagree.
    subnets: tuple[Subnet, ...] = ()
    #: Every static route of every device, in load then declaration order (§16.3).
    routes: tuple[_RouteEntry, ...] = ()
    #: Every configured BGP session, with its far end resolved by address (§16.6).
    sessions: tuple[_Session, ...] = ()
    #: Every VRF any device declares, with what is bound and placed in it (§16.1).
    vrfs: tuple[_Vrf, ...] = ()
    #: Every device that declares a routing table or a policy rule (§16.4).
    policies: tuple[_PolicyDatabase, ...] = ()
    #: Every device that declares a security zone or a filter policy (§24).
    firewalls: tuple[_Firewall, ...] = ()
    #: Devices that declare ``spec.routing``, in load order.
    routing: Mapping[str, RoutingConfig] = field(default_factory=dict)
    #: Every address the inventory configures -> where it is, first declaration
    #: winning. This is what resolves a BGP neighbour: a peer is an address in
    #: the real world, so it is looked up as one here rather than by name. A
    #: duplicate address is ``E004``'s business, not this index's.
    address_owners: Mapping[_IPAddress, tuple[str, str]] = field(default_factory=dict)
    #: Every power fact of the inventory (:mod:`netviz.power`): the resolved
    #: feeds, the per-PDU load, the PoE budgets and the references that did not
    #: resolve. The same plan the ``power`` layer draws and ``list power``
    #: prints, so a finding and a diagram of it can never disagree.
    power: PowerPlan = field(default_factory=PowerPlan)
    #: Every group membership of the inventory (:mod:`netviz.identity`),
    #: resolved. The same plan the ``identity`` layer draws and ``list groups``
    #: prints, so a finding and a diagram of it can never disagree.
    identities: IdentityPlan = field(default_factory=IdentityPlan)

    def source_of(self, fqn: str | None) -> SourceLocation | None:
        if fqn is None:
            return None
        # A layout document (§18) and an annotation (§21) are not elements, so
        # neither is in ``sources``; a finding about one still has to point at
        # its file.
        if (source := self.inventory.source_of(fqn)) is not None:
            return source
        if (source := self.inventory.layout_sources.get(fqn)) is not None:
            return source
        for table in self.inventory.annotation_sources.values():
            if (source := table.get(fqn)) is not None:
                return source
        return None

    def effective(self, endpoint: _Endpoint) -> Interface | None:
        """The interface whose configuration governs a link end (§10.6).

        A cable that lands on a LAG member is governed by the aggregate: VLAN
        membership and MTU are properties of the bundle, not of one lane.
        """
        interface = endpoint.interface
        if interface is None or endpoint.owner_fqn is None:
            return interface
        masters = self.lag_masters.get(endpoint.owner_fqn, {})
        return masters.get(interface.name, interface)

    def bridges_frames(self, owner_fqn: str, interface: str) -> bool:
        """Is this port the end of a layer-2 tunnel?

        VXLAN, Geneve and L2TP carry ethernet frames, so their interfaces are
        switchports in everything but name: they neither hold an address nor
        need one, and ``W101`` would otherwise fire on every one of them.
        """
        return any(
            self.inventory.tunnels[fqn].spec.type.layer == 2
            for fqn in self.tunnel_ports.get((owner_fqn, interface), ())
            if fqn in self.inventory.tunnels
        )

    def tunnel_elements(self, tunnel_fqn: str) -> frozenset[str]:
        """The elements a tunnel terminates on, as far as its endpoints resolve."""
        return frozenset(
            end.owner_fqn
            for end in self.tunnel_ends
            if end.tunnel_fqn == tunnel_fqn and end.owner_fqn is not None
        )

    def on_link(
        self, owner_fqn: str, *, vrf: str, version: int, dev: str | None = None
    ) -> tuple[IPNetwork, ...]:
        """The prefixes ``owner_fqn`` can reach without routing (§16.3).

        A next hop is resolved by ARP or neighbour discovery, so it has to sit in
        a prefix the device holds *itself* — in the right family, and in the right
        routing instance, because a VRF is a routing table of its own and an
        address in another one is not reachable from this one at all.

        ``dev`` narrows it to one interface, which is what a route naming an
        egress means: the next hop is on *that* link or nowhere.
        """
        owner = self.owners.get(owner_fqn)
        if owner is None:  # pragma: no cover - callers iterate the owner map
            return ()
        prefixes: list[IPNetwork] = []
        for interface in owner.interfaces:
            if dev is not None and interface.name != dev:
                continue
            if (interface.vrf or GLOBAL_VRF) != vrf:
                continue
            prefixes.extend(
                address.network
                for address in interface.addresses()
                if address.network.version == version and is_routable_address(address)
            )
        return tuple(prefixes)

    def is_suppressed(self, rule_id: str, elements: Sequence[str]) -> bool:
        """Does any element involved carry an annotation silencing ``rule_id``?"""
        for fqn in elements:
            ignored = self.suppressions.get(fqn)
            if ignored and (WILDCARD in ignored or rule_id in ignored):
                return True
        return False


def _build_context(inventory: Inventory) -> _Context:
    owners: dict[str, InterfaceOwner] = {
        fqn: element
        for fqn, element in inventory.elements.items()
        if isinstance(element, _OWNER_TYPES)
    }

    # Built before anything resolves a reference, because that is what a
    # reference resolves *through*: ``Device.interface`` is a linear scan of
    # ``spec.interfaces``, which on a 48-port switch is 48 string comparisons
    # per cable end. ``NG-I001`` makes interface names unique within an element,
    # so the map and the scan cannot disagree.
    by_name: dict[str, dict[str, Interface]] = {}
    for fqn, owner in owners.items():
        names: dict[str, Interface] = {}
        for interface in owner.interfaces:
            names.setdefault(interface.name, interface)
        by_name[fqn] = names

    endpoints: list[_Endpoint] = []
    terminations: dict[tuple[str, str], list[_Endpoint]] = {}
    panel_terminations: dict[tuple[str, str], list[_Endpoint]] = {}
    connected: set[str] = set()

    for cable_fqn, cable in inventory.cables.items():
        namespace = namespace_of(cable_fqn)
        for index, ref in enumerate(cable.endpoints):
            endpoint = _resolve_endpoint(
                inventory, cable_fqn, cable, ref, index, namespace, by_name
            )
            endpoints.append(endpoint)
            owner_fqn = endpoint.owner_fqn
            if owner_fqn is None:
                continue
            # A device that exists but is missing the interface still counts as
            # cabled: reporting it as an orphan on top of E001 would be two
            # findings for one mistake.
            connected.add(owner_fqn)
            if not endpoint.resolved:
                continue
            landing = panel_terminations if endpoint.panel is not None else terminations
            landing.setdefault((owner_fqn, ref.interface), []).append(endpoint)

    attachments: list[_Attachment] = []
    for fqn, adapter in inventory.adapters.items():
        host = adapter.upstream.attached_to
        if host is None:
            continue
        resolution = inventory.lookup(host, namespace=namespace_of(fqn))
        attachments.append(
            _Attachment(
                adapter_fqn=fqn,
                adapter=adapter,
                ref=host,
                host_fqn=resolution.fqn,
                host=resolution.element,
                ambiguous=resolution.ambiguous,
            )
        )
        if resolution.fqn is not None:
            # §8.2: `attached_to` is itself a graph edge, so both ends are joined.
            connected.add(resolution.fqn)
            connected.add(fqn)

    tunnel_ends: list[_TunnelEnd] = []
    tunnel_ports: dict[tuple[str, str], list[str]] = {}
    encapsulations: list[_Encapsulation] = []
    for tunnel_fqn, tunnel in inventory.tunnels.items():
        namespace = namespace_of(tunnel_fqn)
        for index, ref in enumerate(tunnel.endpoints):
            end = _resolve_tunnel_end(inventory, tunnel_fqn, tunnel, ref, index, namespace, by_name)
            tunnel_ends.append(end)
            if end.resolved and end.owner_fqn is not None:
                tunnel_ports.setdefault((end.owner_fqn, ref.interface), []).append(tunnel_fqn)
        over = tunnel.spec.over
        if over is not None:
            encapsulations.append(
                _resolve_encapsulation(inventory, tunnel_fqn, tunnel, over, namespace)
            )

    routing = _collect_routing(owners)
    return _Context(
        inventory=inventory,
        owners=owners,
        endpoints=tuple(endpoints),
        endpoint_pairs=_pair_endpoints(endpoints),
        attachments=tuple(attachments),
        tunnel_ends=tuple(tunnel_ends),
        encapsulations=tuple(encapsulations),
        tunnel_ports={key: tuple(value) for key, value in tunnel_ports.items()},
        terminations={key: tuple(value) for key, value in terminations.items()},
        panels=inventory.patchpanels,
        panel_terminations={key: tuple(value) for key, value in panel_terminations.items()},
        placements=tuple(
            _Placement(fqn=fqn, element=element, location=element.metadata.location)
            for fqn, element in inventory.elements.items()
            if element.metadata.location is not None
        ),
        connected=frozenset(connected),
        lag_masters={fqn: _lag_masters(owner) for fqn, owner in owners.items()},
        stacking_groups={fqn: _stacking_groups(owner) for fqn, owner in owners.items()},
        by_name=by_name,
        aggregated_by={fqn: _aggregated_by(owner) for fqn, owner in owners.items()},
        suppressions=_collect_suppressions(inventory),
        subnets=_stable_subnets(subnets_of(inventory)),
        routes=routing.routes,
        sessions=routing.sessions,
        vrfs=routing.vrfs,
        policies=routing.policies,
        firewalls=routing.firewalls,
        routing=routing.routing,
        address_owners=routing.address_owners,
        power=power_plan(inventory),
        identities=identity_plan(inventory),
    )


@dataclass(frozen=True, slots=True)
class _Routing:
    """Everything §16 and §24 contribute to the context, collected in one pass.

    One walk rather than five, and no walk at all for an inventory that declares
    no routing and no filtering — which is every inventory written before §16 existed. The
    per-rule cost of the routing group is nil (``tools/profile_validate.py``);
    what would have shown up is four more traversals of every device in
    :func:`_build_context`, so there is one.
    """

    routes: tuple[_RouteEntry, ...] = ()
    sessions: tuple[_Session, ...] = ()
    vrfs: tuple[_Vrf, ...] = ()
    policies: tuple[_PolicyDatabase, ...] = ()
    firewalls: tuple[_Firewall, ...] = ()
    routing: Mapping[str, RoutingConfig] = field(default_factory=dict)
    address_owners: Mapping[_IPAddress, tuple[str, str]] = field(default_factory=dict)


def _collect_routing(owners: Mapping[str, InterfaceOwner]) -> _Routing:
    """Flatten the routing and filtering state of every device, or nothing at all.

    Two short-circuits, because most inventories say nothing about routing and
    ``_build_context`` is a third of ``validate``:

    * :func:`_routes_anything` decides in one comparison per device whether there
      is anything to flatten at all, so an inventory written before §16 existed
      pays one generator pass and nothing else;
    * the address index is built only when a session needs resolving, since it is
      a pass over every address in the inventory.
    """
    if not _routes_anything(owners):
        return _Routing()

    routes: list[_RouteEntry] = []
    vrfs: list[_Vrf] = []
    policies: list[_PolicyDatabase] = []
    firewalls: list[_Firewall] = []
    routing: dict[str, RoutingConfig] = {}
    peers: list[tuple[str, Device, BgpConfig]] = []

    for fqn, owner in owners.items():
        if not isinstance(owner, Device):
            continue
        spec = owner.spec
        for index, route in enumerate(spec.routes):
            routes.append(_RouteEntry(owner_fqn=fqn, owner=owner, index=index, route=route))
        for index, vrf in enumerate(spec.vrfs):
            vrfs.append(
                _Vrf(
                    owner_fqn=fqn,
                    owner=owner,
                    index=index,
                    vrf=vrf,
                    interfaces=tuple(
                        interface.name
                        for interface in owner.interfaces
                        if interface.vrf == vrf.name
                    ),
                    routes=tuple(
                        position
                        for position, route in enumerate(spec.routes)
                        if route.vrf == vrf.name
                    ),
                )
            )
        if spec.route_tables or spec.routing_policy:
            policies.append(
                _PolicyDatabase(
                    owner_fqn=fqn,
                    owner=owner,
                    tables=tuple(spec.route_tables),
                    rules=tuple(spec.routing_policy),
                )
            )
        if spec.zones or spec.firewall is not None:
            firewalls.append(
                _Firewall(
                    owner_fqn=fqn,
                    owner=owner,
                    zones=tuple(spec.zones),
                    rules=tuple(spec.firewall.rules) if spec.firewall else (),
                )
            )
        if spec.routing is not None:
            routing[fqn] = spec.routing
            if spec.routing.bgp is not None and spec.routing.bgp.neighbors:
                peers.append((fqn, owner, spec.routing.bgp))

    addresses = _index_addresses(owners) if peers else {}
    sessions = [
        _Session(
            owner_fqn=fqn,
            owner=owner,
            index=index,
            neighbor=neighbor,
            peer_fqn=peer[0] if (peer := addresses.get(neighbor.address)) else None,
            peer_interface=peer[1] if peer else None,
        )
        for fqn, owner, bgp in peers
        for index, neighbor in enumerate(bgp.neighbors)
    ]
    return _Routing(
        routes=tuple(routes),
        sessions=tuple(sessions),
        vrfs=tuple(vrfs),
        policies=tuple(policies),
        firewalls=tuple(firewalls),
        routing=routing,
        address_owners=addresses,
    )


def _routes_anything(owners: Mapping[str, InterfaceOwner]) -> bool:
    """Does any device declare routing or filtering state at all (§16, §24)?

    Both in one predicate because both are collected in one walk. A firewall is
    not routing, but the question the short-circuit is asking is "is there
    anything on a device's ``spec`` past its interfaces", and the answer has to
    cover everything that walk gathers or the walk would be skipped with work
    left in it.
    """
    return any(
        isinstance(owner, Device)
        and (
            owner.spec.routes
            or owner.spec.vrfs
            or owner.spec.route_tables
            or owner.spec.routing_policy
            or owner.spec.routing is not None
            or owner.spec.zones
            or owner.spec.firewall is not None
        )
        for owner in owners.values()
    )


def _index_addresses(owners: Mapping[str, InterfaceOwner]) -> dict[_IPAddress, tuple[str, str]]:
    """Every configured address -> ``(element, interface)``, first one winning.

    Loopback and link-local addresses are left out: ``127.0.0.1`` is on every
    machine and ``fe80::1`` on every link, so neither identifies an element — and
    a BGP session pointed at one would be pointed at the local box.
    """
    index: dict[_IPAddress, tuple[str, str]] = {}
    for fqn, owner in owners.items():
        for interface in owner.interfaces:
            for address in interface.addresses():
                if not is_routable_address(address):
                    continue
                index.setdefault(address.ip, (fqn, interface.name))
    return index


def _stable_subnets(subnets: Sequence[Subnet]) -> tuple[Subnet, ...]:
    """The same subnets with each member list in ``element:interface`` order.

    :func:`~netviz.subnets.subnets_of` keeps members in inventory load order
    on purpose, so that a subnet's member list and the nodes of a rendering
    agree about sequence. A *diagnostic* wants the opposite: load order is
    directory order, so "address 10.0.0.1 is claimed by 'a:eth0', 'b:eth0'"
    becomes "'b:eth0', 'a:eth0'" — and anchors itself to a different file — when
    two documents are merged into one or a file is renamed. Nothing about the
    network changed, but a SARIF baseline and a committed report both say
    something did.

    Every rule that reads ``ctx.subnets`` reports a *symmetric* fact — two
    claimants of one address, two broadcast domains in one prefix — where the
    members are interchangeable and the order is therefore free to be chosen for
    stability. Reordering the copy the validator sees keeps that away from
    everything else that derives a subnet.
    """
    return tuple(
        replace(
            subnet,
            members=tuple(
                sorted(subnet.members, key=lambda member: (member.element, member.interface))
            ),
        )
        for subnet in subnets
    )


def _resolve_endpoint(
    inventory: Inventory,
    cable_fqn: str,
    cable: Cable,
    ref: InterfaceRef,
    index: int,
    namespace: str,
    by_name: Mapping[str, Mapping[str, Interface]],
) -> _Endpoint:
    """Resolve one ``device:interface`` reference (§4.2)."""
    resolution = inventory.lookup(ref.device, namespace=namespace)
    element = resolution.element
    if element is None:
        return _Endpoint(
            cable_fqn=cable_fqn,
            cable=cable,
            ref=ref,
            index=index,
            ambiguous=resolution.ambiguous,
        )
    if not isinstance(element, _CABLE_TARGET_TYPES):
        return _Endpoint(
            cable_fqn=cable_fqn,
            cable=cable,
            ref=ref,
            index=index,
            wrong_kind=element.kind,
        )

    if isinstance(element, PatchPanel):
        # §15.1: a panel port is a cable endpoint like any other, but it is not
        # in ``by_name`` — that index is built for the *active* elements, whose
        # configuration the rules read.
        return _Endpoint(
            cable_fqn=cable_fqn,
            cable=cable,
            ref=ref,
            index=index,
            owner_fqn=resolution.fqn,
            owner=element,
            interface=element.interface(ref.interface),
        )

    is_upstream = isinstance(element, Adapter) and ref.interface == element.upstream.name
    names = by_name.get(resolution.fqn or "", {})
    return _Endpoint(
        cable_fqn=cable_fqn,
        cable=cable,
        ref=ref,
        index=index,
        owner_fqn=resolution.fqn,
        owner=element,
        interface=None if is_upstream else names.get(ref.interface),
        is_upstream=is_upstream,
    )


def _resolve_tunnel_end(
    inventory: Inventory,
    tunnel_fqn: str,
    tunnel: Tunnel,
    ref: InterfaceRef,
    index: int,
    namespace: str,
    by_name: Mapping[str, Mapping[str, Interface]],
) -> _TunnelEnd:
    """Resolve one endpoint of a tunnel (§4.2, §14.3)."""
    resolution = inventory.lookup(ref.device, namespace=namespace)
    element = resolution.element
    if element is None:
        return _TunnelEnd(
            tunnel_fqn=tunnel_fqn,
            tunnel=tunnel,
            ref=ref,
            index=index,
            ambiguous=resolution.ambiguous,
        )
    if not isinstance(element, _OWNER_TYPES):
        return _TunnelEnd(
            tunnel_fqn=tunnel_fqn,
            tunnel=tunnel,
            ref=ref,
            index=index,
            wrong_kind=element.kind,
        )
    return _TunnelEnd(
        tunnel_fqn=tunnel_fqn,
        tunnel=tunnel,
        ref=ref,
        index=index,
        owner_fqn=resolution.fqn,
        owner=element,
        interface=by_name.get(resolution.fqn or "", {}).get(ref.interface),
    )


def _resolve_encapsulation(
    inventory: Inventory, tunnel_fqn: str, tunnel: Tunnel, ref: str, namespace: str
) -> _Encapsulation:
    """Resolve one tunnel's ``spec.over`` (§14.3)."""
    resolution = inventory.lookup(ref, namespace=namespace)
    element = resolution.element
    if element is None:
        return _Encapsulation(
            tunnel_fqn=tunnel_fqn, tunnel=tunnel, ref=ref, ambiguous=resolution.ambiguous
        )
    if not isinstance(element, Tunnel):
        return _Encapsulation(
            tunnel_fqn=tunnel_fqn, tunnel=tunnel, ref=ref, wrong_kind=element.kind
        )
    return _Encapsulation(
        tunnel_fqn=tunnel_fqn,
        tunnel=tunnel,
        ref=ref,
        over_fqn=resolution.fqn,
        over=element,
    )


def _lag_masters(owner: InterfaceOwner) -> dict[str, Interface]:
    """Map each LAG member to its aggregate (§10.6)."""
    masters: dict[str, Interface] = {}
    for interface in owner.interfaces:
        if interface.type is InterfaceType.LAG:
            for member in interface.members or ():
                masters.setdefault(member, interface)
    return masters


def _aggregated_by(owner: InterfaceOwner) -> dict[str, tuple[str, ...]]:
    """Map each member to every ``lag``/``bridge`` that lists it (``NG-I005``)."""
    claims: dict[str, list[str]] = {}
    for interface in owner.interfaces:
        if interface.type in AGGREGATE_TYPES:
            for member in interface.members or ():
                claims.setdefault(member, []).append(interface.name)
    return {member: tuple(names) for member, names in claims.items()}


def _stacking_groups(owner: InterfaceOwner) -> dict[str, str]:
    """Map each interface to a representative of its stacking group.

    Interfaces joined by ``parent``/``members`` form one group: a VLAN
    sub-interface, its parent, a LAG and its members all legitimately share a
    hardware address, so a shared MAC within a group is not a duplicate.
    """
    parent: dict[str, str] = {interface.name: interface.name for interface in owner.interfaces}

    def find(name: str) -> str:
        root = name
        while parent[root] != root:
            root = parent[root]
        while parent[name] != root:  # path compression
            parent[name], name = root, parent[name]
        return root

    for interface in owner.interfaces:
        for lower in interface.lower_layer_if:
            if lower in parent:
                left, right = find(interface.name), find(lower)
                if left != right:
                    parent[left] = right

    return {name: find(name) for name in parent}


def _collect_suppressions(inventory: Inventory) -> dict[str, frozenset[str]]:
    """Read the ``netviz/ignore`` annotation of every element, layout and note.

    A layout document (§18) and a diagram annotation (§21) are not elements, but
    ``W138``, ``W142`` and ``W143`` are reported against them, and a finding
    nobody can annotate away is a finding people learn to ignore wholesale.
    """
    suppressions: dict[str, frozenset[str]] = {}
    documents = (
        *inventory.elements.items(),
        *inventory.layouts.items(),
        *((fqn, annotation) for _, fqn, annotation in inventory.annotations),
    )
    for fqn, element in documents:
        tokens: list[str] = []
        for key in IGNORE_ANNOTATIONS:
            raw = element.metadata.annotations.get(key)
            if raw:
                tokens.extend(_split_tokens(raw))
        if tokens:
            # Unknown ids are kept verbatim and simply match nothing: a typo in
            # a suppression must not hide the finding it was aimed at, and must
            # not abort a run over inventory data either.
            suppressions[fqn] = frozenset(resolve_rule_id(token, strict=False) for token in tokens)
    return suppressions


def _split_tokens(value: str) -> list[str]:
    """Split ``"E001, W102 W103"`` into its rule ids."""
    for separator in _TOKEN_SEPARATORS:
        value = value.replace(separator, " ")
    return value.split()


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def _check_endpoint_references(ctx: _Context) -> Iterator[_Draft]:
    """E001 — a cable endpoint references an unknown device or interface."""
    for endpoint in ctx.endpoints:
        elements = (endpoint.cable_fqn,)
        prefix = f"cable {_q(endpoint.cable_fqn)} endpoint {endpoint.ref}"
        owner, owner_fqn = endpoint.owner, endpoint.owner_fqn

        if endpoint.ambiguous:
            candidates = _join(sorted(endpoint.ambiguous))
            yield _Draft(
                f"{prefix}: {_q(endpoint.ref.device)} is ambiguous here; it matches "
                f"{candidates}. Move the cable next to the element it refers to, or "
                f"rename one of them.",
                elements,
                endpoint.field_path,
            )
        elif endpoint.wrong_kind is not None:
            yield _Draft(
                f"{prefix}: {_q(endpoint.ref.device)} is a {endpoint.wrong_kind}, which owns "
                f"no interfaces",
                elements,
                endpoint.field_path,
            )
        elif owner is None or owner_fqn is None:
            yield _Draft(
                f"{prefix}: no element named {_q(endpoint.ref.device)} is declared in this "
                f"inventory",
                elements,
                endpoint.field_path,
            )
        elif endpoint.panel is not None:
            # A missing panel position is ``E021``, which can say what the
            # positions of a 24-port panel are without listing 48 names.
            continue
        elif not endpoint.resolved:
            known = _join(sorted(owner.interface_names()))
            yield _Draft(
                f"{prefix}: {_q(owner_fqn)} has no interface {_q(endpoint.ref.interface)}; "
                f"it declares {known}",
                (*elements, owner_fqn),
                endpoint.field_path,
            )


def _check_double_termination(ctx: _Context) -> Iterator[_Draft]:
    """E002 — an interface is terminated by more than one cable."""
    for (owner_fqn, interface_name), endpoints in ctx.terminations.items():
        if len(endpoints) < 2:
            continue
        port = f"{owner_fqn}:{interface_name}"
        cables = list(dict.fromkeys(endpoint.cable_fqn for endpoint in endpoints))
        if len(cables) == 1:
            yield _Draft(
                f"both endpoints of cable {_q(cables[0])} terminate on {_q(port)}; "
                f"a cable joins two distinct interfaces",
                (cables[0], owner_fqn),
                ("spec", "endpoints"),
            )
        else:
            yield _Draft(
                f"interface {_q(port)} is terminated by {len(cables)} cables: {_join(cables)}. "
                f"A physical port takes one cable.",
                (*cables, owner_fqn),
                ("spec", "endpoints"),
            )


def _check_duplicate_mac(ctx: _Context) -> Iterator[_Draft]:
    """E003 — two interfaces in the inventory share a MAC address."""
    groups: dict[str, list[tuple[str, Interface]]] = {}
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            if interface.mac is not None:
                groups.setdefault(interface.mac, []).append((fqn, interface))

    for mac, entries in groups.items():
        # A stacking group (LAG and its members, a VLAN sub-interface and its
        # parent) shares one hardware address by design, so it counts once.
        seen: set[tuple[str, str]] = set()
        distinct: list[tuple[str, Interface]] = []
        for fqn, interface in entries:
            key = (fqn, ctx.stacking_groups[fqn].get(interface.name, interface.name))
            if key in seen:
                continue
            seen.add(key)
            distinct.append((fqn, interface))

        if len(distinct) < 2:
            continue
        ordered = _by_port(distinct)
        ports = [f"{fqn}:{interface.name}" for fqn, interface in ordered]
        yield _Draft(
            f"MAC address {mac} is used by {len(ports)} interfaces: {_join(ports)}",
            tuple(dict.fromkeys(fqn for fqn, _ in ordered)),
            _interface_path(ctx.owners[ordered[0][0]], ordered[0][1], "mac"),
        )


def _check_duplicate_ip(ctx: _Context) -> Iterator[_Draft]:
    """E004 — one IP address is assigned twice inside a subnet, VLAN and VRF.

    A **VRF** partitions the namespace (§16.1): a routing instance is a routing
    table of its own, so the same address in ``blue`` and in the global instance
    is the ordinary way to give two customers the same plan, and reporting it
    would report the feature. Two addresses only collide when they are in one
    instance, one prefix and one broadcast domain.

    A **network namespace** partitions it more completely still (§23.1), and
    differently: a VRF name is coordinated across an estate, so two devices
    naming ``blue`` are taken to mean one instance, while a namespace belongs to
    the machine that runs it and a ``blue`` on two hosts is two unrelated
    stacks. So the scope here is the *stack*: an address in a declared namespace
    is compared only against addresses in that namespace of that machine.

    The cost of that is a container bridged onto the LAN with an address another
    machine already holds, which this will not report. The alternative is a hard
    error on every host running two containers out of one image, which is the
    ordinary case — and a rule that fires on the ordinary case is a rule people
    turn off.
    """
    # Grouped on the :mod:`ipaddress` objects rather than on their text: they
    # compare and hash exactly as their spellings do, so the groups are the
    # same, and the only two that are ever rendered are the ones a finding
    # names. Nearly every address in a healthy inventory is alone in its group.
    groups: dict[_AddressKey, list[tuple[str, Interface]]] = {}
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            scope = interface.vlan.pvid if interface.vlan is not None else None
            vrf = interface.vrf or GLOBAL_VRF
            # The initial namespace of every machine keys as ``""`` so that two
            # ordinary hosts on one wire collide exactly as they always have; a
            # declared one is qualified by its machine, because that is the
            # whole extent of what the name means.
            stack = (fqn, interface.netns) if interface.netns else ("", "")
            for address in interface.addresses():
                # A loopback address is scoped to the host that holds it
                # (RFC 1122 §3.2.1.3, RFC 4291 §2.5.3) and never appears on a
                # link, so every machine declaring 127.0.0.1 is correct rather
                # than in conflict.
                if address.ip.is_loopback:
                    continue
                groups.setdefault((stack, vrf, address.ip, address.network, scope), []).append(
                    (fqn, interface)
                )

    for (stack, vrf, ip, network, scope), entries in groups.items():
        if len(entries) < 2:
            continue
        ordered = _by_port(entries)
        ports = [f"{fqn}:{interface.name}" for fqn, interface in ordered]
        domain = _describe_scope(scope)
        instance = "" if not vrf else f" of VRF {_q(vrf)}"
        if stack[1]:
            instance += f" in {_netns_text(stack[1])}"
        yield _Draft(
            f"IP address {ip} in {network}{instance} is assigned to {len(ports)} interfaces "
            f"in {domain}: {_join(ports)}",
            tuple(dict.fromkeys(fqn for fqn, _ in ordered)),
            _interface_path(ctx.owners[ordered[0][0]], ordered[0][1]),
        )


def _check_gateway_on_link(ctx: _Context) -> Iterator[_Draft]:
    """E020 — an interface's ``gateway`` is not inside any prefix it holds.

    A first hop is reached by ARP or neighbour discovery, never by routing: if
    it is not on-link, the host has no way to send the very packet that would
    tell it how to reach the gateway. The usual cause is a prefix length that
    was shortened without the gateway being moved, or a gateway copied from a
    neighbouring subnet.

    An IPv6 link-local gateway is exempt. ``fe80::1`` is on-link by definition,
    and the interface's own link-local address is autoconfigured rather than
    written down, so there is no declared prefix for it to be inside of.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            for version, gateway in interface.gateways():
                if gateway.is_link_local:
                    continue
                prefixes = [
                    address.network
                    for address in interface.addresses()
                    if address.network.version == version
                ]
                if any(gateway in prefix for prefix in prefixes):
                    continue
                port = _q(f"{fqn}:{interface.name}")
                detail = (
                    f"its only IPv{version} prefixes are {_join_plain([str(p) for p in prefixes])}"
                    if prefixes
                    else f"the interface configures no IPv{version} address at all"
                )
                yield _Draft(
                    f"interface {port} has gateway {gateway}, which is not on-link: {detail}. "
                    f"A first hop is resolved by neighbour discovery, so an off-link one is "
                    f"never reachable.",
                    (fqn,),
                    _index_path(owner, interface.name, f"ipv{version}", "gateway"),
                )


# --------------------------------------------------------------------------- #
# Routing (§16)
# --------------------------------------------------------------------------- #


def _check_route_next_hop(ctx: _Context) -> Iterator[_Draft]:
    """E032 — a route's next hop is in no prefix the device holds (``NG-F008``).

    A next hop is reached by ARP or neighbour discovery, never by routing, so it
    has to be on-link: inside a prefix configured on an interface of this device,
    of the next hop's own family, in the route's own **routing instance**. A VRF
    is a routing table of its own (§16.1), so an address in another one is not
    merely a different subnet — it is unreachable from here by construction.

    Three exemptions, each for a next hop that is on-link *by definition* rather
    than by a prefix anybody wrote down:

    * an IPv6 link-local next hop (``fe80::1``), the normal way to write a
      next hop on an unnumbered link;
    * a route that names a ``dev`` the device does not have, which is ``E033``
      and would otherwise be reported twice;
    * a blackhole route, which has no next hop at all.
    """
    for entry in ctx.routes:
        via = entry.route.via
        if via is None or via.is_link_local:
            continue
        dev = entry.route.dev
        if dev is not None and dev not in ctx.by_name.get(entry.owner_fqn, {}):
            continue
        prefixes = ctx.on_link(entry.owner_fqn, vrf=entry.vrf, version=via.version, dev=dev)
        if any(via in prefix for prefix in prefixes):
            continue
        yield _Draft(
            f"{entry.where} has a next hop that is not on-link: {_describe_reach(prefixes, via, dev, entry.vrf)}. "
            f"A next hop is resolved by neighbour discovery, so an off-link one is never "
            f"reachable.",
            (entry.owner_fqn,),
            entry.path("via"),
        )


def _describe_reach(
    prefixes: Sequence[IPNetwork], via: _IPAddress, dev: str | None, vrf: str
) -> str:
    """Why ``via`` is not on-link: what the device does hold, and where."""
    scope = f"interface {_q(dev)}" if dev is not None else "the device"
    instance = "the global instance" if not vrf else f"VRF {_q(vrf)}"
    if not prefixes:
        return (
            f"{scope} configures no IPv{via.version} address in {instance} at all, so "
            f"{via} sits on no link of it"
        )
    listed = _join_plain([str(prefix) for prefix in prefixes])
    return f"{scope} is in {listed} in {instance}, and {via} is in none of them"


def _check_route_device(ctx: _Context) -> Iterator[_Draft]:
    """E033 — a route's ``dev`` names an interface the device has not got.

    ``dev`` is how a route is pointed at an egress rather than at an address: an
    unnumbered point-to-point link, or a route into a tunnel. A name that
    resolves to nothing is a route the device would refuse to install, which
    makes the destination unreachable while the inventory says it is served.
    """
    for entry in ctx.routes:
        dev = entry.route.dev
        if dev is None:
            continue
        known = ctx.by_name.get(entry.owner_fqn, {})
        if dev in known:
            continue
        yield _Draft(
            f"{entry.where} sends out of interface {_q(dev)}, which "
            f"{_q(entry.owner_fqn)} does not have; it has {_join(sorted(known))}",
            (entry.owner_fqn,),
            entry.path("dev"),
        )


def _check_ospf_interfaces(ctx: _Context) -> Iterator[_Draft]:
    """E034 — OSPF is enabled on an interface the device does not have (``NG-F010``).

    An area is only as big as the interfaces that run it, so a name that resolves
    to nothing is an adjacency that will never come up — and, in an inventory,
    a link that looks like it is in the IGP and is not.
    """
    for fqn, routing in ctx.routing.items():
        ospf = routing.ospf
        if ospf is None:
            continue
        known = ctx.by_name.get(fqn, {})
        missing = [name for name in ospf.interfaces if name not in known]
        if not missing:
            continue
        yield _Draft(
            f"element {_q(fqn)} runs OSPF area {ospf.area} on {_join(missing)}, which it "
            f"does not have; it has {_join(sorted(known))}",
            (fqn,),
            ("spec", "routing", "ospf", "interfaces"),
        )


def _check_bgp_asn(ctx: _Context) -> Iterator[_Draft]:
    """E035 — the two ends of a resolved session disagree about an AS (``NG-F011``).

    ``remote_asn`` is a claim about the peer, and the peer states its own
    ``asn``: when the two differ, the OPEN message carries an AS the far end does
    not recognise and the session never establishes. Reported per *claim*, from
    the document that makes it, because a typo on one side is one mistake and
    two sides typed differently are two.

    A peer that declares no ``routing.bgp`` at all is silent rather than
    reported: an inventory may model the box without modelling its control plane,
    and inventing a disagreement from an absent block would fire on every
    partially-described network.
    """
    for session in ctx.sessions:
        peer_fqn = session.peer_fqn
        if peer_fqn is None:
            continue
        peer = ctx.owners.get(peer_fqn)
        peer_routing = peer.spec.routing if isinstance(peer, Device) else None
        peer_bgp = peer_routing.bgp if peer_routing is not None else None
        if peer_bgp is None:
            continue
        claimed = session.neighbor.remote_asn
        if claimed != peer_bgp.asn:
            yield _Draft(
                f"element {_q(session.owner_fqn)} peers with {session.neighbor.address} "
                f"({_q(session.peer_port)}) as AS {claimed}, but {_q(peer_fqn)} declares "
                f"AS {peer_bgp.asn}; the session would never establish",
                (session.owner_fqn, peer_fqn),
                session.path("remote_asn"),
            )


def _check_router_ids(ctx: _Context) -> Iterator[_Draft]:
    """E036 — two elements claim the same router id (``NG-F012``).

    A router id names the router itself: OSPF drops adjacencies with a neighbour
    that claims the local id (RFC 2328 §10.5) and BGP refuses a session with a
    duplicate identifier (RFC 4271 §6.8). One device declaring the same value for
    OSPF and BGP is *one* identity, which is why the ids of a device are
    de-duplicated before they are counted.
    """
    claims: dict[ipaddress.IPv4Address, list[str]] = {}
    for fqn, routing in ctx.routing.items():
        for router_id in routing.router_ids:
            claims.setdefault(router_id, []).append(fqn)
    for router_id, holders in claims.items():
        if len(holders) < 2:
            continue
        ordered = sorted(holders)
        yield _Draft(
            f"router id {router_id} is claimed by {len(ordered)} elements: {_join(ordered)}. "
            f"A router id identifies the router, so a duplicate keeps every adjacency "
            f"between them down.",
            tuple(ordered),
            ("spec", "routing"),
        )


def _check_bgp_neighbour_resolves(ctx: _Context) -> Iterator[_Draft]:
    """W135 — a neighbour address is nowhere in the inventory (``NG-F013``).

    A warning, not an error, and deliberately so: an eBGP session towards a
    transit provider points at an address on *their* router, which is not an
    element of this inventory and never will be. What the warning is worth
    saying is that netviz cannot check the far end of this session, and that
    the routing view has nothing to draw the edge to.
    """
    for session in ctx.sessions:
        if session.resolved:
            continue
        described = f" ({session.neighbor.description})" if session.neighbor.description else ""
        yield _Draft(
            f"element {_q(session.owner_fqn)} peers with {session.neighbor.address} in "
            f"AS {session.neighbor.remote_asn}{described}, which no element of this "
            f"inventory is addressed at; the session is drawn to nothing and its far end "
            f"cannot be checked",
            (session.owner_fqn,),
            session.path("address"),
        )


def _check_empty_vrf(ctx: _Context) -> Iterator[_Draft]:
    """W136 — a VRF nothing is bound to (``NG-F014``).

    A routing instance is a table plus the interfaces that feed it. With no
    interface bound, it holds no address and no connected route, so every address
    and every static route placed in it is unreachable — and the partition it was
    declared to create does not exist: the addresses an operator meant to isolate
    are all still in the global instance.
    """
    for entry in ctx.vrfs:
        if entry.interfaces:
            continue
        placed = (
            f"; {count_text(len(entry.routes), 'route')} placed in it can never resolve a next hop"
            if entry.routes
            else ""
        )
        yield _Draft(
            f"element {_q(entry.owner_fqn)} declares VRF {_q(entry.vrf.name)} "
            f"(rd {entry.vrf.rd}), but no interface is bound to it, so it holds no "
            f"address{placed}",
            (entry.owner_fqn,),
            entry.path("name"),
        )


def _check_policy_empty_table(ctx: _Context) -> Iterator[_Draft]:
    """W147 — a policy rule selects a table nothing is in (``NG-F022``).

    Policy-based routing is two halves that have to meet: a rule that says
    *route this by table X*, and a route placed in X. With the second half
    missing, the rule matches, the lookup finds nothing, and the packet falls
    through to the next rule — so the traffic the operator diverted goes exactly
    where it would have gone anyway, silently.

    Only *declared* tables are checked. ``main`` holds every connected route the
    device has without anybody writing one down, and a VRF's table is fed by the
    interfaces bound to it, so neither is empty for being unmentioned; an empty
    VRF is ``W136``'s business instead.
    """
    for database in ctx.policies:
        for index, table in enumerate(database.tables):
            selecting = database.owner.spec.policy_for(table.name)
            if not selecting or database.owner.spec.routes_in(table.name):
                continue
            priorities = _join_plain([str(rule.priority) for rule in selecting])
            yield _Draft(
                f"element {_q(database.owner_fqn)} routes by {table.describe()} at "
                f"{'priorities' if len(selecting) > 1 else 'priority'} {priorities}, but no "
                f"route is placed in that table; the lookup finds nothing and the packet "
                f"falls through to the next rule",
                (database.owner_fqn,),
                database.table_path(index, "name"),
            )


def _check_unselected_route_table(ctx: _Context) -> Iterator[_Draft]:
    """W148 — a declared table no rule ever looks up (``NG-F023``).

    The other half of ``W147``, and the more common mistake: the routes are
    written, the table is declared, and the rule that would reach it was never
    added. A table nothing selects is consulted by nothing — it is not a
    fallback, it is dead weight — so any route in it is a statement about the
    device that the device does not act on.
    """
    for database in ctx.policies:
        for index, table in enumerate(database.tables):
            if database.owner.spec.policy_for(table.name):
                continue
            routes = database.owner.spec.routes_in(table.name)
            placed = (
                f"; {count_text(len(routes), 'route')} placed in it is never consulted"
                if routes
                else ""
            )
            yield _Draft(
                f"element {_q(database.owner_fqn)} declares {table.describe()}, but no rule "
                f"in 'spec.routing_policy' looks it up, so nothing is ever routed by it"
                f"{placed}",
                (database.owner_fqn,),
                database.table_path(index, "name"),
            )


def _check_shadowed_policy(ctx: _Context) -> Iterator[_Draft]:
    """W149 — a rule below one that matches everything (``NG-F024``).

    The policy database is walked from the lowest priority upwards and the first
    match decides, so a rule with no selector at all ends the walk: everything
    after it in the same family is unreachable, whatever it says. That is how a
    database is *meant* to be terminated — the last rule is normally
    ``lookup main`` — and it is also the most common way to break one, by
    numbering a new rule above the terminator instead of below it.

    Per family, because the two databases are separate lists: an IPv4 catch-all
    shadows nothing in IPv6. A ``goto`` catch-all is not a terminator either — it
    jumps forward, and what it jumps to is still reached.
    """
    for database in ctx.policies:
        for family in AddressFamily:
            walk = database.owner.spec.policy_in(family)
            blocker = next(
                (
                    rule
                    for rule in walk
                    if rule.is_catch_all and rule.action is not PolicyAction.GOTO
                ),
                None,
            )
            if blocker is None:
                continue
            shadowed = [rule for rule in walk if rule.priority > blocker.priority]
            if not shadowed:
                continue
            first = shadowed[0]
            others = (
                f", and {count_text(len(shadowed) - 1, 'rule')} after it"
                if len(shadowed) > 1
                else ""
            )
            yield _Draft(
                f"element {_q(database.owner_fqn)} has {family.value} policy rule "
                f"{_q(first.describe())} below {_q(blocker.describe())}, which matches every "
                f"packet; the database is walked in priority order and the first match "
                f"decides, so this rule{others} can never run",
                (database.owner_fqn,),
                database.rule_path(first, "priority"),
            )


def _check_empty_zone(ctx: _Context) -> Iterator[_Draft]:
    """W150 — a declared zone holds no interface (``NG-B010``).

    A zone is a partition of the device's interfaces, so one holding none is
    empty in the strongest sense: no packet can ever be in it, and every rule
    naming it — however carefully written — matches nothing. The finding counts
    those rules, because the number is the difference between a placeholder
    nobody has filled in yet and a policy that has quietly stopped working.
    """
    for firewall in ctx.firewalls:
        for index, zone in enumerate(firewall.zones):
            if zone.interfaces:
                continue
            policy = firewall.policy
            entries: tuple[FirewallRule | NatRule, ...] = (
                (*policy.rules, *policy.nat) if policy is not None else ()
            )
            naming = [rule for rule in entries if zone.name in rule.zones]
            written = (
                f"; {count_text(len(naming), 'rule')} naming it can never match" if naming else ""
            )
            yield _Draft(
                f"element {_q(firewall.owner_fqn)} declares security zone {_q(zone.name)}, "
                f"which holds no interface; a zone is a partition of the device's interfaces, "
                f"so nothing can ever be in this one{written}",
                (firewall.owner_fqn,),
                firewall.zone_path(index, "name"),
            )


def _check_unzoned_interface(ctx: _Context) -> Iterator[_Draft]:
    """W151 — an interface in no zone, on a device that has zones (``NG-B011``).

    Only on a device that declares zones at all: a machine with no zones has
    every interface outside one, which is the ordinary case and says nothing.
    Once a partition exists, though, an interface outside it is traffic the
    policy cannot name — a rule saying *from lan* does not reach it, and what it
    gets is whichever default applies, which is rarely what somebody dividing a
    device into zones had in mind.

    Reported once per device rather than once per interface: a 48-port switch
    with two zones would otherwise fill the report with one identical line per
    port, and the answer to all of them is the same one edit.
    """
    for firewall in ctx.firewalls:
        if not firewall.zones:
            continue
        outside = firewall.owner.spec.unzoned_interfaces()
        if not outside:
            continue
        names = _join_plain([interface.name for interface in outside])
        yield _Draft(
            f"element {_q(firewall.owner_fqn)} divides its interfaces into "
            f"{count_text(len(firewall.zones), 'zone')}, and {names} "
            f"{'is' if len(outside) == 1 else 'are'} in none of them; no rule naming a zone "
            f"can match traffic there, so it gets the chain default",
            (firewall.owner_fqn,),
            ("spec", "zones"),
        )


def _check_mark_nothing_reads(ctx: _Context) -> Iterator[_Draft]:
    """W152 — the firewall writes a mark the routing policy never matches (``NG-B012``).

    One half of §16.9. Policy-based routing has no layer-4 selector on purpose:
    the portable way to route by port is to mark the packet in the firewall and
    match ``fwmark`` in the policy database. A mark written by a rule nothing
    reads is that plan half-built — the marking works, every command applies, and
    the traffic goes out the default uplink anyway.

    A mark is *local to the machine*: it is metadata attached to a packet inside
    one kernel and it is gone the moment the packet leaves. So there is nowhere
    else the reader could be, and its absence is a statement about this device
    that can be made without looking at any other.
    """
    for firewall in ctx.firewalls:
        if firewall.policy is None:
            continue
        read = set(firewall.owner.spec.policy_marks())
        for rule in firewall.policy.rules_in():
            if rule.mark is None or rule.mark in read:
                continue
            where = (
                "the device declares no 'spec.routing_policy' at all"
                if not read
                else f"the policy database matches {_join_plain(sorted(read))}"
            )
            yield _Draft(
                f"element {_q(firewall.owner_fqn)} writes mark {rule.mark} in firewall rule "
                f"{_q(rule.describe())}, and no rule in 'spec.routing_policy' matches it: "
                f"{where}. A mark exists to be read, and it does not leave the machine",
                (firewall.owner_fqn,),
                firewall.rule_path(rule, "mark"),
            )


def _check_mark_nothing_writes(ctx: _Context) -> Iterator[_Draft]:
    """W153 — the routing policy matches a mark the firewall never writes (``NG-B013``).

    The other half of ``W152``, and the more common shape: the routing is done,
    the table is filled, and the rule that would mark the traffic is the line
    that never got typed. The rule matches nothing, so the packet falls through
    to the next one — the failure that looks like it works.

    Only on a device that declares ``spec.firewall``, because that is what makes
    the absence meaningful. A device whose filtering nobody has written down may
    well be marking; a device whose whole policy is in the inventory is not.
    """
    for firewall in ctx.firewalls:
        policy = firewall.policy
        if policy is None:
            continue
        written = set(policy.marks())
        for index, rule in enumerate(firewall.owner.spec.routing_policy):
            if rule.fwmark is None or rule.fwmark in written:
                continue
            where = (
                "its 'spec.firewall' writes no mark at all"
                if not written
                else f"its 'spec.firewall' writes {_join_plain(sorted(written))}"
            )
            yield _Draft(
                f"element {_q(firewall.owner_fqn)} routes by mark {rule.fwmark} in policy rule "
                f"{_q(rule.describe())}, and {where}; nothing puts that mark on a packet, so "
                f"the rule never matches and the traffic is routed by whatever comes after it",
                (firewall.owner_fqn,),
                ("spec", "routing_policy", index, "fwmark"),
            )


def _check_shadowed_firewall_rule(ctx: _Context) -> Iterator[_Draft]:
    """W154 — a filter rule below one that already decided its traffic (``NG-B014``).

    The chain is walked from the lowest priority upwards and the first *terminal*
    match decides, so a rule with no selector and a terminal action ends the walk
    for everything it covers. That is how a chain is meant to be closed — the
    last rule is often ``drop`` — and it is also the most common way to break
    one, by numbering a new rule above the closer instead of below it.

    **Covers**, not "matches everything". A rule with no selector is still about
    the zone pair it names, and ``lan -> wan accept`` says nothing about a packet
    from ``wan`` to ``dmz``. So an earlier rule shadows a later one only when its
    zone pair is at least as broad: each half is either unstated — which is every
    zone — or the same zone the later rule names. Getting this wrong in the
    other direction would be the worse failure: a finding telling an operator
    that a working rule is dead is a finding that gets the rule deleted.

    Per hook *and* per family, because those are separate chains: a closer in
    ``input`` shadows nothing in ``forward``, and an IPv4 one shadows nothing in
    IPv6. A ``mark`` or ``log`` rule shadows nothing either — it does something
    to the packet and the walk carries on, which is the whole reason those two
    actions exist.

    One finding per shadowed rule, not one per chain it is shadowed in: a rule
    below the closer in both families is one mistake, and saying so twice would
    double the report without doubling the work.
    """
    for firewall in ctx.firewalls:
        policy = firewall.policy
        if policy is None:
            continue
        reported: set[int] = set()
        for hook in FirewallHook:
            for family in AddressFamily:
                for shadowed, blocker in _shadowed_in(policy.rules_in(hook, family)):
                    if shadowed.priority in reported:
                        continue
                    reported.add(shadowed.priority)
                    yield _Draft(
                        f"element {_q(firewall.owner_fqn)} has {family.value} {hook.value} "
                        f"firewall rule {_q(shadowed.describe())} below "
                        f"{_q(blocker.describe())}, which matches every packet between "
                        f"those zones and decides it; the chain is walked in priority "
                        f"order, so this rule can never run",
                        (firewall.owner_fqn,),
                        firewall.rule_path(shadowed, "priority"),
                    )


def _shadowed_in(
    walk: Sequence[FirewallRule],
) -> Iterator[tuple[FirewallRule, FirewallRule]]:
    """``(unreachable rule, the rule that decided its traffic first)`` pairs.

    ``walk`` is one chain in priority order. A rule is unreachable when some
    earlier rule is terminal, carries no selector, and covers its zone pair —
    which is what :func:`_covers` decides. Only the *first* such blocker is
    reported per rule: naming the earliest one is naming the edit.
    """
    closers: list[FirewallRule] = []
    for rule in walk:
        blocker = next((entry for entry in closers if _covers(entry, rule)), None)
        if blocker is not None:
            yield rule, blocker
            continue
        if rule.is_catch_all and rule.action.is_terminal:
            closers.append(rule)


def _covers(blocker: FirewallRule, rule: FirewallRule) -> bool:
    """Does ``blocker``'s zone pair take in every packet ``rule`` is about?

    An unstated zone is every zone, so it covers whatever the later rule names;
    a stated one covers only itself. ``lan -> wan`` therefore closes the ``lan``
    to ``wan`` traffic and nothing else, while a rule naming neither zone closes
    the whole chain it is in.
    """
    return all(
        theirs is None or theirs == ours
        for theirs, ours in (
            (blocker.src_zone, rule.src_zone),
            (blocker.dst_zone, rule.dst_zone),
        )
    )


def _check_vlan_mismatch(ctx: _Context) -> Iterator[_Draft]:
    """E005 — the two ends of a link disagree about VLANs (``NG-C011``).

    Three shapes of disagreement, all of which leave a link that looks perfectly
    cabled while carrying nothing the operator meant it to: two access ports in
    different VLANs, an access port facing a trunk, and two trunks whose VLAN
    sets do not meet. A fourth — two trunks that both name a *native* VLAN and
    name different ones — is the same mistake for untagged frames only.

    A port with no ``vlan`` block at all is silent by design: an untagged host
    facing an access port is the normal pairing (§11.1), and the host correctly
    says nothing about VLANs. Both ends are resolved through the LAG master
    first (§10.6), because membership is a property of the bundle.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        left, right = ctx.effective(first), ctx.effective(second)
        if left is None or right is None:
            continue
        left_vlan, right_vlan = left.vlan, right.vlan
        if left_vlan is None or right_vlan is None:
            continue
        detail = _vlan_disagreement(
            _describe_port(first, left), left_vlan, _describe_port(second, right), right_vlan
        )
        if detail is None:
            continue
        yield _Draft(
            f"cable {_q(cable_fqn)} {detail}",
            _cable_elements(cable_fqn, first, second),
            ("spec", "endpoints"),
        )


def _vlan_disagreement(
    near: str, near_vlan: VlanConfig, far: str, far_vlan: VlanConfig
) -> str | None:
    """Describe how two linked ports disagree about VLANs, or ``None`` if they agree."""
    if near_vlan.mode is VlanMode.ACCESS and far_vlan.mode is VlanMode.ACCESS:
        if near_vlan.pvid == far_vlan.pvid:
            return None
        return (
            f"connects access port {near} in VLAN {near_vlan.pvid} to access port {far} in "
            f"VLAN {far_vlan.pvid}; the two ends would be in different broadcast domains"
        )

    if VlanMode.ACCESS in (near_vlan.mode, far_vlan.mode):
        if far_vlan.mode is VlanMode.ACCESS:  # normalise: the access end first
            near, near_vlan, far, far_vlan = far, far_vlan, near, near_vlan
        carried = _describe_carried(far_vlan)
        if near_vlan.pvid == far_vlan.pvid:
            return (
                f"connects access port {near} in VLAN {near_vlan.pvid} to trunk port {far} "
                f"carrying {carried}; only the trunk's native VLAN {far_vlan.pvid} crosses, and "
                f"every tagged VLAN it carries is dropped by the access port"
            )
        return (
            f"connects access port {near} in VLAN {near_vlan.pvid} to trunk port {far} carrying "
            f"{carried}, untagged in VLAN {far_vlan.pvid}; an access port drops every tagged "
            f"frame, so the two ends share no broadcast domain"
        )

    if near_vlan.vlan_ids().isdisjoint(far_vlan.vlan_ids()):
        return (
            f"connects trunk port {near} carrying {_describe_carried(near_vlan)} to trunk port "
            f"{far} carrying {_describe_carried(far_vlan)}; the two sets are disjoint, so the "
            f"link passes no VLAN at all"
        )
    if (
        near_vlan.native_vlan is not None
        and far_vlan.native_vlan is not None
        and near_vlan.native_vlan != far_vlan.native_vlan
    ):
        return (
            f"connects trunk port {near} with native VLAN {near_vlan.native_vlan} to trunk port "
            f"{far} with native VLAN {far_vlan.native_vlan}; untagged frames would cross from one "
            f"broadcast domain into the other"
        )
    return None


def _check_adapter_capacity(ctx: _Context) -> Iterator[_Draft]:
    """E006 — an adapter declares more downstream interfaces than it has ports."""
    for fqn, adapter in ctx.inventory.adapters.items():
        capacity = adapter.spec.ports
        if capacity is None or len(adapter.interfaces) <= capacity:
            continue
        yield _Draft(
            f"adapter {_q(fqn)} declares {count_text(len(adapter.interfaces), 'downstream interface')}"
            f" but has only {count_text(capacity, 'port')}",
            (fqn,),
            ("spec", "interfaces"),
        )


def _check_unaddressed_interface(ctx: _Context) -> Iterator[_Draft]:
    """W101 — an interface has no address, is not a switchport and bridges nothing."""
    for fqn, owner in ctx.owners.items():
        # §6.5: a hub is a layer-1 repeater; its ports cannot hold an address,
        # so the rule would fire on every one of them.
        if isinstance(owner, Hub):
            continue
        # Anything another interface is stacked on carries the lower layer, not
        # the addresses: LAG members and the parent of a VLAN sub-interface.
        substrate = {name for interface in owner.interfaces for name in interface.lower_layer_if}
        for index, interface in enumerate(owner.interfaces):
            if not interface.enabled or interface.name in substrate:
                continue
            if interface.has_ipv4_addresses or interface.has_ipv6_addresses:
                continue
            if interface.vlan is not None:
                continue
            # An 'ap' radio bridges its BSSs onto the air (§6.2.6). That is
            # switching, whether or not the SSIDs are mapped to a VLAN, so the
            # port needs no address to be doing something.
            if interface.wireless is not None and interface.wireless.role.is_ap:
                continue
            if ctx.bridges_frames(fqn, interface.name):
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} has no IPv4 or IPv6 address and "
                f"no 'vlan' block, so it neither routes nor switches",
                (fqn,),
                ("spec", "interfaces", index),
            )


def _check_mtu_mismatch(ctx: _Context) -> Iterator[_Draft]:
    """W102 — the two ends of a cable disagree about the MTU."""
    for cable_fqn, first, second in _linked_endpoints(ctx):
        left, right = ctx.effective(first), ctx.effective(second)
        if left is None or right is None or left.mtu is None or right.mtu is None:
            continue
        if left.mtu == right.mtu:
            continue
        yield _Draft(
            f"cable {_q(cable_fqn)} joins {_describe_port(first, left)} with MTU {left.mtu} to "
            f"{_describe_port(second, right)} with MTU {right.mtu}; the mismatch causes silent "
            f"path-MTU failures",
            _cable_elements(cable_fqn, first, second),
            ("spec", "endpoints"),
        )


def _check_orphan_device(ctx: _Context) -> Iterator[_Draft]:
    """W103 — a device terminates no cable and hosts no adapter."""
    for fqn in ctx.inventory.devices:
        if fqn in ctx.connected:
            continue
        yield _Draft(
            f"device {_q(fqn)} terminates no cable and hosts no adapter; it is drawn as an "
            f"isolated node",
            (fqn,),
        )


def _check_ip_on_access_port(ctx: _Context) -> Iterator[_Draft]:
    """W104 — an access port of a layer-2-only switch carries an IP address."""
    for fqn, device in ctx.inventory.devices.items():
        if not isinstance(device, Switch) or _is_layer3(device):
            continue
        for index, interface in enumerate(device.interfaces):
            # An SVI is exactly where a management address belongs, and it is
            # modelled as an access-mode block carrying the encapsulation VID.
            if interface.type is InterfaceType.VLAN or interface.vlan is None:
                continue
            if interface.vlan.mode is not VlanMode.ACCESS:
                continue
            if not (interface.has_ipv4_addresses or interface.has_ipv6_addresses):
                continue
            yield _Draft(
                f"access port {_q(f'{fqn}:{interface.name}')} of layer-2-only switch "
                f"{_q(fqn)} carries an IP address; put it on a 'vlan' (SVI) interface "
                f"instead",
                (fqn,),
                ("spec", "interfaces", index),
            )


def _check_lonely_subnet(ctx: _Context) -> Iterator[_Draft]:
    """W105 — a subnet holds exactly one element.

    This is the layer-3 view's own finding: the L1 and L2 pictures cannot show
    it, because a prefix is not a thing either of them draws.
    """
    for subnet in ctx.subnets:
        # A host route holds one address by definition, and the far end of a
        # point-to-point link is routinely outside the inventory — an ISP
        # hand-off is not a device anybody here declares.
        # Stacks, not elements (§23.1): a bridge in a host's initial namespace
        # and the two containers hanging off it are one element and three
        # parties, and the prefix they share is exactly not lonely.
        if subnet.is_point_to_point or len(subnet.stacks) != 1:
            continue
        first = subnet.members[0]
        ports = _join([member.port for member in subnet.members])
        yield _Draft(
            f"only one element is addressed in subnet {_q(subnet.prefix)}: {ports} "
            f"({_join_plain(subnet.addresses)}); nothing else in the inventory is addressed in "
            f"it, so either the prefix length is wrong or the neighbour is missing",
            (first.element,),
            ("spec", "interfaces", first.index),
        )


def _check_subnet_address_clash(ctx: _Context) -> Iterator[_Draft]:
    """W106 — two elements claim one address in a subnet, across VLAN boundaries.

    ``E004`` is the same clash seen from layer 2, and it deliberately scopes
    itself to one VLAN: re-using a prefix per broadcast domain is a normal
    design. Layer 3 has no VLAN column — a routing table does not — so the same
    address in one prefix is drawn as one subnet with two claimants whatever
    VLANs the two ports sit in, and that is worth saying once. When two of the
    holders *do* share a VLAN, ``E004`` reports it as an error and this rule
    stays quiet rather than doubling the diagnostic.
    """
    for subnet in ctx.subnets:
        for ip, holders in _group_by_ip(subnet).items():
            if len({holder.element for holder in holders}) < 2:
                continue
            if _shares_a_broadcast_domain(holders):
                continue
            first = holders[0]
            domains = _join_plain([_describe_scope(holder.scope) for holder in holders])
            yield _Draft(
                f"address {ip} in subnet {_q(subnet.prefix)} is claimed by "
                f"{count_text(len(holders), 'interface')} in different broadcast domains "
                f"({domains}): {_join([holder.port for holder in holders])}. The layer-3 view "
                f"draws one subnet, so not all of them can be reached at that address.",
                tuple(dict.fromkeys(holder.element for holder in holders)),
                ("spec", "interfaces", first.index),
            )


def _check_prefix_domains(ctx: _Context) -> Iterator[_Draft]:
    """W130 — one prefix is claimed by two broadcast domains.

    A prefix is the address space of *one* segment. When two interfaces in
    different VLANs are addressed inside the same prefix, each host believes
    every address in it is reachable by ARP, and the half of them that sits in
    the other VLAN is not. Nothing routes between the two either, because a
    router will not forward between two interfaces it considers the same
    subnet. In IPAM terms this is the overlap that is *not* a nesting: neither
    claim contains the other, they simply collide.

    Only explicitly tagged interfaces count. A host on an access port declares
    no ``vlan`` block — its broadcast domain is a property of the switch it is
    cabled to, not of the document — so treating "untagged" as a domain of its
    own would fire on the ordinary pairing of a router sub-interface with the
    hosts it serves.

    When every domain holds *the same* addresses, nothing is said here: that is
    one address claimed twice, which ``W106`` and ``E004`` report more sharply
    and with the offending address named. The prefix is only reported as split
    once the two halves hold addresses of their own, which is when the split is
    a fact about the plan rather than about a single typo.
    """
    for subnet in ctx.subnets:
        tagged = [member for member in subnet.members if member.scope is not None]
        domains: dict[int, list[AddressPlacement]] = {}
        for member in tagged:
            domains.setdefault(member.scope, []).append(member)  # type: ignore[arg-type]
        # Two ports of one element in two VLANs of one prefix is a mistake too,
        # but it is ``W111``'s (overlapping prefixes on one element) and saying
        # it twice helps nobody.
        if len(domains) < 2 or len({member.element for member in tagged}) < 2:
            continue
        held = [frozenset(member.ip for member in members) for members in domains.values()]
        if all(addresses == held[0] for addresses in held):
            continue
        first = tagged[0]
        detail = _join_plain(
            [
                f"VLAN {vlan} ({_join([member.port for member in members])})"
                for vlan, members in sorted(domains.items())
            ]
        )
        yield _Draft(
            f"subnet {_q(subnet.prefix)} is claimed by "
            f"{count_text(len(domains), 'broadcast domain')}: {detail}. Neither half can "
            f"reach the other by ARP, and no router will forward between them.",
            tuple(dict.fromkeys(member.element for member in tagged)),
            ("spec", "interfaces", first.index),
        )


def _check_nested_prefix_domains(ctx: _Context) -> Iterator[_Draft]:
    """W131 — a nested prefix is used in a different broadcast domain than its parent.

    Nesting on its own is normal: ``10.0.0.0/16`` on a summarising router and
    ``10.0.5.0/24`` on the segment beneath it describe the same plan at two
    levels. It stops being normal when the two sit in different VLANs, because
    the wider prefix then tells its own segment that every address of the
    narrower one is on-link, which it is not. That is the mask-typo shape:
    ``/16`` typed where ``/24`` was meant.

    Compared pairwise over the derived prefixes, which are few — one per
    distinct prefix in the inventory — and only for prefixes that actually have
    tagged members on both sides, for the reason given on :func:`_check_prefix_domains`.
    """
    domains = [(subnet, _tagged_domains(subnet)) for subnet in ctx.subnets]
    for (outer, outer_vlans), (inner, inner_vlans) in itertools.combinations(domains, 2):
        # ``ctx.subnets`` is sorted by network address then prefix length, so
        # the wider prefix of an overlapping pair always comes first.
        if not (outer_vlans and inner_vlans and inner_vlans.isdisjoint(outer_vlans)):
            continue
        if inner.network.version != outer.network.version or not inner.network.subnet_of(
            outer.network  # type: ignore[arg-type]
        ):
            continue
        first = inner.members[0]
        yield _Draft(
            f"subnet {_q(inner.prefix)} sits inside {_q(outer.prefix)}, but the two are used "
            f"in different broadcast domains: {_q(inner.prefix)} in "
            f"{_vlan_list(inner_vlans)} and {_q(outer.prefix)} in {_vlan_list(outer_vlans)}. "
            f"Hosts in {_q(outer.prefix)} treat every address of {_q(inner.prefix)} as "
            f"on-link, so they will ARP for it instead of routing to it.",
            tuple(dict.fromkeys(member.element for member in (*inner.members, *outer.members))),
            ("spec", "interfaces", first.index),
        )


def _tagged_domains(subnet: Subnet) -> frozenset[int]:
    """The VLANs a prefix's *explicitly tagged* members sit in."""
    return frozenset(member.scope for member in subnet.members if member.scope is not None)


def _vlan_list(vlans: frozenset[int]) -> str:
    return _join_plain([f"VLAN {vlan}" for vlan in sorted(vlans)])


def _check_link_prefixes(ctx: _Context) -> Iterator[_Draft]:
    """W132 — two directly linked interfaces are addressed in prefixes that do not meet.

    A cable is one segment, so an address configured on it that lies outside
    every prefix the other end declares is outside every prefix *on its own
    link*: the two ends cannot exchange a single packet without a router, and
    there is no room for one between them. The usual cause is a host that kept
    the addressing of the desk it was moved from.

    Only families both ends configure are compared. A switchport carries no
    address at all and says nothing here, which is why this is quiet on the
    ordinary host-to-access-port link; and a dual-stack pair that agrees on
    IPv6 while disagreeing on IPv4 is still reported, because the IPv4 half is
    still broken. Both ends resolve through the LAG master first (§10.6).

    A cable landing on an interface with no socket — a loopback, a vlan
    sub-interface, a bridge — is skipped: ``E012`` already says the cable
    cannot exist, and there is no link for two prefixes to fail to meet on.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        left, right = ctx.effective(first), ctx.effective(second)
        if left is None or right is None or not (left.is_cableable and right.is_cableable):
            continue
        for version in (4, 6):
            near = _on_link_prefixes(left, version)
            far = _on_link_prefixes(right, version)
            if not near or not far:
                continue
            if any(one.overlaps(other) for one in near for other in far):
                continue
            yield _Draft(
                f"cable {_q(cable_fqn)} joins {_describe_port(first, left)} in "
                f"{_join_plain([str(prefix) for prefix in near])} to "
                f"{_describe_port(second, right)} in "
                f"{_join_plain([str(prefix) for prefix in far])}; the two ends share no IPv"
                f"{version} prefix, so neither address is inside any prefix on its own link.",
                _cable_elements(cable_fqn, first, second),
                ("spec", "endpoints"),
            )


def _on_link_prefixes(interface: Interface, version: int) -> tuple[IPNetwork, ...]:
    """The routable prefixes of one family an interface puts on its link.

    Loopback and link-local addresses are dropped: neither is reachable across
    a cable, so neither says anything about whether the two ends meet.
    """
    return tuple(
        dict.fromkeys(
            address.network
            for address in interface.addresses()
            if address.network.version == version and is_routable_address(address)
        )
    )


# --------------------------------------------------------------------------- #
# Interfaces (§10.2)
# --------------------------------------------------------------------------- #


def _check_stacking_cycle(ctx: _Context) -> Iterator[_Draft]:
    """E007 — ``parent``/``members`` stacking contains a cycle.

    The schema already rejects the one-step case: an interface cannot be its own
    ``parent`` (``NG-I002``) nor list itself as a member (``NG-I003``). A longer
    loop — ``bond0`` aggregating ``bond1`` aggregating ``bond0`` — passes every
    per-document check and is only visible once the whole element is in view.
    """
    for fqn, owner in ctx.owners.items():
        for cycle in _stacking_cycles(owner):
            ports = [f"{fqn}:{name}" for name in cycle]
            chain = " -> ".join(_q(name) for name in (*cycle, cycle[0]))
            yield _Draft(
                f"interface stacking on {_q(fqn)} is cyclic: {chain}. "
                f"{count_text(len(cycle), 'interface')} ({_join(ports)}) would each have to "
                f"sit on top of the next.",
                (fqn,),
                _index_path(owner, cycle[0]),
            )


def _stacking_cycles(owner: InterfaceOwner) -> list[list[str]]:
    """The cycles of the ``if:lower-layer-if`` graph, in load order.

    An iterative depth-first search rather than a recursive one: the recursion
    depth would be the length of the longest stack, which is inventory data.
    """
    lower: dict[str, tuple[str, ...]] = {
        interface.name: interface.lower_layer_if for interface in owner.interfaces
    }

    unvisited, active, done = 0, 1, 2
    state = dict.fromkeys(lower, unvisited)
    cycles: list[list[str]] = []

    for start in lower:
        if state[start] != unvisited:
            continue
        state[start] = active
        path = [start]
        stack = [(start, iter(lower[start]))]
        while stack:
            node, remaining = stack[-1]
            following = next(remaining, None)
            if following is None:
                stack.pop()
                state[node] = done
                path.pop()
            elif following not in state:
                continue  # NG-I002/NG-I003 already rejected the dangling reference
            elif state[following] is active:
                # A back edge closes exactly one cycle, and its own endpoints fix
                # that cycle's first and last interface, so no two back edges can
                # report the same loop. Only the loops reachable as back edges of
                # this spanning tree are found, which is one finding per element
                # rather than an enumeration of every cycle it contains.
                cycles.append(path[path.index(following) :])
            elif state[following] is unvisited:
                state[following] = active
                path.append(following)
                stack.append((following, iter(lower[following])))
    return cycles


def _check_member_is_aggregated(ctx: _Context) -> Iterator[_Draft]:
    """E008 — a ``lag``/``bridge`` member is not free to be aggregated.

    A physical port belongs to exactly one aggregate, and nothing may be stacked
    on it once it does: the aggregate owns the port's frames. Three shapes break
    that, and all three describe hardware that cannot be built.

    The one legitimate nesting is a ``lag`` inside a ``bridge`` — ``br0`` with
    ``members: [bond0, eth2]`` is how every Linux box bridges a bond — so it is
    the single exemption rather than a special case sprinkled through the check.
    """
    for fqn, owner in ctx.owners.items():
        claims = ctx.aggregated_by[fqn]
        by_name = ctx.by_name[fqn]
        parents = _sub_interface_parents(owner)
        for member, aggregates in claims.items():
            interface = by_name.get(member)
            if interface is None:
                continue  # NG-I003 already rejected the dangling member
            port = f"{fqn}:{member}"
            path = _index_path(owner, member)
            if len(aggregates) > 1:
                yield _Draft(
                    f"interface {_q(port)} is a member of {count_text(len(aggregates), 'aggregate')} "
                    f"at once: {_join([f'{fqn}:{name}' for name in aggregates])}. A port "
                    f"belongs to one aggregate.",
                    (fqn,),
                    path,
                )
            for aggregate in aggregates:
                if not _may_aggregate(by_name[aggregate], interface):
                    yield _Draft(
                        f"interface {_q(port)} is a {interface.type.value!r} aggregate but is "
                        f"listed as a member of {_q(f'{fqn}:{aggregate}')}, which is a "
                        f"{by_name[aggregate].type.value!r}; an aggregate cannot be enslaved "
                        f"to another one",
                        (fqn,),
                        path,
                    )
            for child in parents.get(member, ()):
                yield _Draft(
                    f"interface {_q(port)} is a member of {_join([f'{fqn}:{a}' for a in aggregates])}"
                    f" and is also the parent of sub-interface {_q(f'{fqn}:{child}')}; traffic "
                    f"for the sub-interface would never reach it",
                    (fqn,),
                    path,
                )


def _may_aggregate(aggregate: Interface, member: Interface) -> bool:
    """May ``member`` legitimately appear in ``aggregate``'s ``members``?

    Anything that is not itself an aggregate may. Of the aggregates, only a
    ``lag`` inside a ``bridge`` is real hardware.
    """
    if member.type not in AGGREGATE_TYPES:
        return True
    return aggregate.type is InterfaceType.BRIDGE and member.type is InterfaceType.LAG


def _sub_interface_parents(owner: InterfaceOwner) -> dict[str, tuple[str, ...]]:
    """Map each interface to the ``type: vlan`` sub-interfaces stacked on it."""
    children: dict[str, list[str]] = {}
    for interface in owner.interfaces:
        if interface.parent is not None:
            children.setdefault(interface.parent, []).append(interface.name)
    return {parent: tuple(names) for parent, names in children.items()}


def _check_addresses_on_member(ctx: _Context) -> Iterator[_Draft]:
    """W107 — a ``lag``/``bridge`` member carries its own addresses.

    The aggregate is the interface the network sees; an address on one lane is
    reachable only while that lane is up, which defeats the point of bonding.
    """
    for fqn, owner in ctx.owners.items():
        by_name = ctx.by_name[fqn]
        for member, aggregates in ctx.aggregated_by[fqn].items():
            interface = by_name.get(member)
            if interface is None:
                continue
            addresses = [str(address) for address in interface.addresses()]
            if not addresses:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{member}')} is a member of "
                f"{_join([f'{fqn}:{name}' for name in aggregates])} but carries addresses of "
                f"its own ({_join_plain(addresses)}); addresses belong on the aggregate",
                (fqn,),
                _index_path(owner, member),
            )


def _check_mac_on_loopback(ctx: _Context) -> Iterator[_Draft]:
    """W108 — a ``loopback`` interface declares a MAC address.

    A software loopback has no medium and therefore no hardware address. One
    written here is nearly always a block copied from a physical port, and it
    will collide with the port it came from under ``E003``.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            if interface.type is not InterfaceType.LOOPBACK or interface.mac is None:
                continue
            yield _Draft(
                f"loopback interface {_q(f'{fqn}:{interface.name}')} declares the MAC address "
                f"{interface.mac}; a software loopback has no hardware address",
                (fqn,),
                _index_path(owner, interface.name, "mac"),
            )


def _check_no_cableable_interface(ctx: _Context) -> Iterator[_Draft]:
    """W109 — a device declares no ``ethernet``, ``wifi`` or ``lag`` interface.

    Only those three can terminate a cable (``NG-C009``), so such a device can
    never appear on a link. Adapters are exempt: ``NG-X003`` already restricts
    them to exactly those types at schema time.
    """
    for fqn, device in ctx.inventory.devices.items():
        types = {interface.type for interface in device.interfaces}
        if types & _CABLEABLE_TYPES:
            continue
        declared = _join_plain(sorted({interface.type.value for interface in device.interfaces}))
        yield _Draft(
            f"device {_q(fqn)} declares no ethernet, wifi or lag interface (only {declared}), "
            f"so no cable can terminate on it",
            (fqn,),
            ("spec", "interfaces"),
        )


def _check_multicast_mac(ctx: _Context) -> Iterator[_Draft]:
    """E010 — a MAC address has the multicast bit set.

    Bit 0 of the first octet marks a group address (IEEE 802-2014 §8.2). A group
    address can be a frame's *destination* but never its source, so no interface
    can own one. Graded an error rather than §10.2's warning, on the precedent of
    ``E003``/``E004`` (§10.10): unlike a duplicate address, which VRRP makes
    legitimate, there is no configuration in which this is what was meant — it is
    a mistyped octet.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            if interface.mac is None or not _first_octet(interface.mac) & 0b1:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} declares the MAC address "
                f"{interface.mac}, whose first octet has the multicast bit set; a group "
                f"address is never a valid source address",
                (fqn,),
                _index_path(owner, interface.name, "mac"),
            )


def _check_local_mac(ctx: _Context) -> Iterator[_Draft]:
    """I001 — a MAC address is locally administered.

    Bit 1 of the first octet says the address was assigned by the operator
    rather than taken from the vendor's OUI (IEEE 802-2014 §8.2). That is
    perfectly legal and often deliberate — virtual machines, bonds, anonymised
    documentation — so it is information, not a complaint. It is worth printing
    because an address that no vendor issued cannot be looked up when tracing a
    port, and because a hand-written one is the kind that gets duplicated.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            octet = _first_octet(interface.mac) if interface.mac is not None else 0
            if not octet & 0b10:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} declares the locally administered "
                f"MAC address {interface.mac}; no vendor OUI identifies it",
                (fqn,),
                _index_path(owner, interface.name, "mac"),
            )


def _first_octet(mac: str) -> int:
    """The first octet of a normalised ``aa:bb:cc:dd:ee:ff`` address."""
    return int(mac[:2], 16)


# --------------------------------------------------------------------------- #
# Addresses (§10.3)
# --------------------------------------------------------------------------- #


def _check_reserved_address(ctx: _Context) -> Iterator[_Draft]:
    """W110 — an address is the network or broadcast address of its own prefix.

    Both are reserved: the all-zeros host part identifies the subnet (and is the
    subnet-router anycast address in IPv6, RFC 4291 §2.6.1), the all-ones one is
    the IPv4 directed broadcast. Neither can be assigned to an interface. A
    prefix with no host part to speak of — ``/31`` and ``/32``, ``/127`` and
    ``/128`` — is exempt, because RFC 3021 and RFC 6164 give both addresses of a
    point-to-point link to the two ends.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            for address in interface.addresses():
                reserved = _reserved_role(address)
                if reserved is None:
                    continue
                yield _Draft(
                    f"interface {_q(f'{fqn}:{interface.name}')} is configured with {address}, "
                    f"which is the {reserved} of {address.network}; it cannot be assigned to "
                    f"an interface",
                    (fqn,),
                    _index_path(owner, interface.name),
                )


def _reserved_role(address: IPv4Address | IPv6Address) -> str | None:
    """Name the reserved role ``address`` occupies in its own prefix, if any.

    Asked of the address's host bits rather than of an :mod:`ipaddress` network
    object, which is the same question with a very different cost. The three
    facts the network form needs — ``num_addresses``, ``network_address`` and
    ``broadcast_address`` — each build further address objects per *address*
    rather than per prefix, and this rule is the only one in the module that
    would ask them of a loopback address, so it was materialising 2000 prefixes
    nothing else in the run ever looks at (entry 7 of ``docs/follow-ups.md``).

    The arithmetic is the definition, not an approximation of it: a prefix holds
    at most two addresses exactly when it has at most one host bit, the network
    address is exactly the one whose host bits are all zero, and the IPv4
    directed broadcast is exactly the one whose host bits are all one.
    ``test_reserved_role_agrees_with_ipaddress`` pins that against the network
    form over every prefix length of both families.
    """
    width = 32 if isinstance(address, IPv4Address) else 128
    host_bits = width - address.prefix_length
    # /31 and /32, /127 and /128: RFC 3021 and RFC 6164 give both addresses of a
    # point-to-point link to its two ends, so neither is reserved.
    if host_bits <= 1:
        return None
    host_mask = (1 << host_bits) - 1
    host_part = int(address.ip) & host_mask
    if host_part == 0:
        return "network address" if width == 32 else "subnet-router anycast address"
    if width == 32 and host_part == host_mask:
        return "broadcast address"
    return None


def _check_overlapping_prefixes(ctx: _Context) -> Iterator[_Draft]:
    """W111 — two interfaces on one element sit in overlapping prefixes.

    Two ports in prefixes that contain one another leave the host with no single
    answer to "which interface do I send this out of"; two ports in the *same*
    prefix are the common spelling of it, and usually mean a prefix length was
    copied where a different subnet was meant. Addresses that are scoped rather
    than routed — loopback and link-local — are excluded, since ``fe80::/64`` on
    every port is how link-local works rather than a clash.

    Two addresses on **one** interface are exempt by §10.3's own wording: a
    secondary address inside the primary's prefix is an ordinary alias. So are
    two interfaces in **different VRFs** (§16.1): each instance has a routing
    table of its own, so there is no single egress to be ambiguous about — which
    is the entire reason to put two overlapping plans on one router.

    And so are two interfaces in different **network namespaces** (§23.1), for
    the same reason carried further: a namespace is a whole second stack, so the
    two ports are not two ways out of one host at all. Without this a container
    host would be reported once per container, which is the shape a `/30` on
    both ends of a veth pair inevitably has.
    """
    for fqn, owner in ctx.owners.items():
        # Grouped by routing instance rather than compared pairwise inside the
        # loop below: an inventory with no VRF in it has exactly one group, so
        # the quadratic part is the same walk it was before instances existed.
        by_instance: dict[tuple[str, str], list[tuple[str, IPNetwork]]] = {}
        for interface in owner.interfaces:
            for address in interface.addresses():
                if is_routable_address(address):
                    key = (interface.netns_name, interface.vrf or GLOBAL_VRF)
                    by_instance.setdefault(key, []).append((interface.name, address.network))

        for (netns, vrf), placements in by_instance.items():
            instance = "" if not vrf else f" of VRF {_q(vrf)}"
            instance += f" in namespace {_q(netns)}" if netns else ""
            for (left_port, left_net), (right_port, right_net) in _overlapping_pairs(placements):
                yield _Draft(
                    f"element {_q(fqn)} has overlapping prefixes on two interfaces{instance}: "
                    f"{_q(f'{fqn}:{left_port}')} is in {left_net} and "
                    f"{_q(f'{fqn}:{right_port}')} is in {right_net}; traffic for the overlap has "
                    f"no single egress",
                    (fqn,),
                    _index_path(owner, left_port),
                )


def _overlapping_pairs(
    placements: Sequence[tuple[str, IPNetwork]],
) -> Iterator[tuple[tuple[str, IPNetwork], tuple[str, IPNetwork]]]:
    """Each pair of placements on distinct interfaces whose prefixes overlap.

    A pair of *prefixes* is reported once per pair of interfaces, however many
    addresses each interface holds in them. The caller passes one routing
    instance at a time, so two interfaces in different instances are never a
    pair — each instance has a routing table, so there is no single egress to be
    ambiguous about.
    """
    seen: set[tuple[str, str, str, str]] = set()
    for index, (left_port, left_net) in enumerate(placements):
        for right_port, right_net in placements[index + 1 :]:
            if left_port == right_port or left_net.version != right_net.version:
                continue
            if not left_net.overlaps(right_net):
                continue
            key = (left_port, str(left_net), right_port, str(right_net))
            if key in seen:
                continue
            seen.add(key)
            yield (left_port, left_net), (right_port, right_net)


def _check_loopback_prefix(ctx: _Context) -> Iterator[_Draft]:
    """W112 — a ``loopback`` interface carries a prefix wider than a host route.

    A routed loopback is a single address the IGP advertises as a host route; a
    ``/24`` on one claims a whole subnet that exists nowhere on the wire, and
    every router that believes it black-holes the rest of that subnet. The
    host-scoped loopback prefixes are exempt — ``127.0.0.1/8`` is what every
    operating system actually configures, and RFC 1122 §3.2.1.3 reserves the
    whole of ``127.0.0.0/8`` for it — so the rule only speaks about the routed
    loopbacks it is aimed at.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            if interface.type is not InterfaceType.LOOPBACK:
                continue
            for address in interface.addresses():
                # The family is the model's own type; asking the prefix for its
                # version would build one, and for the host-scoped loopbacks this
                # rule then discards nothing else in the run needs (entry 7).
                host_length = 32 if isinstance(address, IPv4Address) else 128
                if address.prefix_length == host_length or address.ip.is_loopback:
                    continue
                yield _Draft(
                    f"loopback interface {_q(f'{fqn}:{interface.name}')} carries {address}; a "
                    f"routed loopback is a host route, so write it as "
                    f"{address.ip}/{host_length}",
                    (fqn,),
                    _index_path(owner, interface.name),
                )


# --------------------------------------------------------------------------- #
# VLANs (§10.4)
# --------------------------------------------------------------------------- #


def _check_undeclared_vlan(ctx: _Context) -> Iterator[_Draft]:
    """W113 — a port references a VLAN the device's ``vlans`` database omits.

    Declaring the database is optional (§6.4), so a device with no ``vlans`` at
    all is saying "not modelled here" rather than "none exist" and is skipped
    entirely. VLAN 1 is skipped too: 802.1Q gives every bridge a Default VLAN
    nobody configures, and the schema itself defaults ``access_vlan`` to it, so
    reporting it would fire on every port that simply left the field out.

    An SSID's ``vlan`` (§6.2.6) is a port membership like any other — it is the
    VLAN the radio bridges that BSS into — so it is checked against the same
    database and reported against the BSS that names it.
    """
    for fqn, device in ctx.inventory.devices.items():
        declared = {vlan.id for vlan in device.spec.vlans}
        if not declared:
            continue
        for interface in device.interfaces:
            vlan = interface.vlan
            if vlan is not None and not _trunks_every_vlan(vlan):
                missing = sorted(vlan.vlan_ids() - declared - {MIN_VLAN_ID})
                if missing:
                    yield _Draft(
                        f"port {_q(f'{fqn}:{interface.name}')} is a member of "
                        f"{'VLAN' if len(missing) == 1 else 'VLANs'} "
                        f"{_join_plain([str(vlan_id) for vlan_id in missing])}, which "
                        f"{_q(fqn)} does not declare in 'vlans'",
                        (fqn,),
                        _index_path(device, interface.name, "vlan"),
                    )
            if interface.wireless is None:
                continue
            for index, entry in enumerate(interface.wireless.bss):
                if entry.vlan is None or entry.vlan in declared or entry.vlan == MIN_VLAN_ID:
                    continue
                yield _Draft(
                    f"SSID {_q(entry.ssid)} on {_q(f'{fqn}:{interface.name}')} is bridged into "
                    f"VLAN {entry.vlan}, which {_q(fqn)} does not declare in 'vlans'",
                    (fqn,),
                    _index_path(device, interface.name, "wireless", "bss", index, "vlan"),
                )


def _check_sub_interface_vlan(ctx: _Context) -> Iterator[_Draft]:
    """E009 — a ``vlan`` sub-interface's VID is not carried by its parent.

    A sub-interface receives the frames its parent tags with that VID and
    nothing else. If the parent is not a trunk, or trunks a set the VID is not
    in, the sub-interface is configured for traffic that can never arrive.

    A ``bridge`` parent is resolved through its members, which is where an SVI
    normally hangs: ``Vlan99`` on ``br0`` is carried as long as *some* port of
    the bridge is in VLAN 99 (``docs/schema.md`` §11.1).
    """
    for fqn, owner in ctx.owners.items():
        by_name = ctx.by_name[fqn]
        for interface in owner.interfaces:
            if interface.type is not InterfaceType.VLAN or interface.vlan is None:
                continue
            parent = by_name.get(interface.parent or "")
            if parent is None:  # pragma: no cover - NG-I002 rejects a dangling parent
                continue
            vid = interface.vlan.pvid
            carried = _carried_vlans(parent, by_name)
            if carried is None or vid in carried:
                continue
            yield _Draft(
                f"sub-interface {_q(f'{fqn}:{interface.name}')} encapsulates VLAN {vid}, but its "
                f"parent {_q(f'{fqn}:{parent.name}')} carries {_describe_vlans(carried)}",
                (fqn,),
                _index_path(owner, interface.name, "vlan"),
            )


def _carried_vlans(
    interface: Interface, by_name: Mapping[str, Interface], _seen: frozenset[str] = frozenset()
) -> frozenset[int] | None:
    """Every VLAN whose frames reach ``interface``, or ``None`` when unbounded.

    ``None`` means "every VLAN": a port trunking ``all`` carries whatever is
    asked of it, so no sub-interface VID can be wrong on it.
    """
    vlan = interface.vlan
    if vlan is not None:
        return None if _trunks_every_vlan(vlan) else vlan.vlan_ids()
    if interface.type not in AGGREGATE_TYPES:
        return frozenset()
    # An aggregate with no `vlan` block of its own carries the union of what its
    # members carry; `_seen` keeps a cyclic stacking (E007) from looping here.
    carried: set[int] = set()
    for member in interface.members or ():
        lower = by_name.get(member)
        if lower is None or member in _seen:
            continue
        below = _carried_vlans(lower, by_name, _seen | {interface.name})
        if below is None:
            return None
        carried |= below
    return frozenset(carried)


def _check_native_vlan_membership(ctx: _Context) -> Iterator[_Draft]:
    """W114 — a trunk's ``native_vlan`` is not listed in its ``trunk_vlans``.

    The native VLAN is the one the port sends and receives *untagged*, so it is
    a member of the port's VLAN set whether or not it appears in the list. The
    document then reads as carrying one VLAN fewer than the port does, which is
    exactly the sort of quiet disagreement between file and hardware this tool
    exists to surface. Writing it out changes nothing operationally.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            vlan = interface.vlan
            if vlan is None or vlan.native_vlan is None or vlan.trunk_vlans is None:
                continue
            if vlan.native_vlan in vlan.trunk_vlans:
                continue
            yield _Draft(
                f"trunk {_q(f'{fqn}:{interface.name}')} has native VLAN {vlan.native_vlan}, "
                f"which is not in its trunk_vlans ({vlan.trunk_vlans}); it is carried "
                f"untagged all the same, so list it",
                (fqn,),
                _index_path(owner, interface.name, "vlan", "native_vlan"),
            )


def _check_trunk_all_to_host(ctx: _Context) -> Iterator[_Draft]:
    """W115 — a port trunking every VLAN faces a host rather than a switch.

    ``trunk_vlans: all`` between switches is normal. Pointed at a host it hands
    the whole VLAN estate to a machine that needs one or two of them, which is
    both a broadcast load nobody planned for and the standard prerequisite for
    VLAN hopping.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        for near, far in ((first, second), (second, first)):
            interface = ctx.effective(near)
            if interface is None or interface.vlan is None:
                continue
            if not _trunks_every_vlan(interface.vlan):
                continue
            if not isinstance(far.owner, _HOST_TYPES):
                continue
            yield _Draft(
                f"port {_describe_port(near, interface)} trunks every VLAN and cable "
                f"{_q(cable_fqn)} takes it to {_q(far.port)}, which is a "
                f"{far.owner.kind} rather than a switch; trunk only the VLANs it needs",
                _cable_elements(cable_fqn, near, far),
                ("spec", "endpoints"),
            )


def _check_lag_member_vlan(ctx: _Context) -> Iterator[_Draft]:
    """W116 — a LAG member declares a ``vlan`` block that differs from the master's.

    §10.6 resolves VLAN and MTU checks on a member through its aggregate, so a
    member's own block is never what the link is checked against. When the two
    disagree, whichever one the reader believes is a coin toss — and the one the
    validator believes is the aggregate's.
    """
    for fqn, owner in ctx.owners.items():
        for member, master in ctx.lag_masters[fqn].items():
            interface = ctx.by_name[fqn].get(member)
            if interface is None or interface.vlan is None or interface.vlan == master.vlan:
                continue
            yield _Draft(
                f"LAG member {_q(f'{fqn}:{member}')} declares {_describe_vlan_block(interface)}, "
                f"but its aggregate {_q(f'{fqn}:{master.name}')} declares "
                f"{_describe_vlan_block(master)}; the aggregate's configuration is the one that "
                f"governs the link (§10.6)",
                (fqn,),
                _index_path(owner, member, "vlan"),
            )


def _trunks_every_vlan(vlan: VlanConfig) -> bool:
    """Is this ``trunk_vlans: all``, i.e. the whole 1-4094 range?"""
    trunk_vlans = vlan.trunk_vlans
    return trunk_vlans is not None and trunk_vlans.ranges == ((MIN_VLAN_ID, MAX_VLAN_ID),)


def _describe_vlans(vlans: frozenset[int]) -> str:
    """``VLANs 10, 20`` / ``no VLAN at all``."""
    if not vlans:
        return "no VLAN at all"
    ids = sorted(vlans)
    return f"{count_text(len(ids), 'VLAN')} ({_join_plain([str(vlan_id) for vlan_id in ids])})"


def _describe_carried(vlan: VlanConfig) -> str:
    """``VLANs 10,20-30`` — what a trunk carries, as one phrase.

    The canonical ``dot1qtypes:vid-range-type`` string rather than an
    enumeration, so a port trunking everything reads ``VLANs 1-4094`` instead of
    four thousand ids.
    """
    trunk_vlans = vlan.trunk_vlans
    if trunk_vlans is None:  # pragma: no cover - NG-V002 requires it in trunk mode
        return "no VLAN"
    return f"VLANs {trunk_vlans}"


def _describe_vlan_block(interface: Interface) -> str:
    """``access VLAN 10`` / ``trunk 10,20`` / ``no 'vlan' block``."""
    vlan = interface.vlan
    if vlan is None:
        return "no 'vlan' block"
    if vlan.mode is VlanMode.ACCESS:
        return f"access VLAN {vlan.access_vlan}"
    native = f" native {vlan.native_vlan}" if vlan.native_vlan is not None else ""
    return f"trunk {vlan.trunk_vlans}{native}"


def _group_by_ip(subnet: Subnet) -> dict[str, list[AddressPlacement]]:
    """The members of one prefix, grouped by the address they hold."""
    groups: dict[str, list[AddressPlacement]] = {}
    for member in subnet.members:
        groups.setdefault(member.ip, []).append(member)
    return groups


def _shares_a_broadcast_domain(holders: Sequence[AddressPlacement]) -> bool:
    """Do two *different* elements among ``holders`` sit in one VLAN scope?

    That is exactly ``E004``'s key — address, prefix and ``dot1q:pvid`` — so
    when it is true the clash is already reported, as an error.
    """
    seen: dict[int | None, str] = {}
    for holder in holders:
        other = seen.setdefault(holder.scope, holder.element)
        if other != holder.element:
            return True
    return False


def _describe_scope(scope: int | None) -> str:
    return f"VLAN {scope}" if scope is not None else "the untagged domain"


# --------------------------------------------------------------------------- #
# Cables (§10.5)
# --------------------------------------------------------------------------- #


def _check_self_link(ctx: _Context) -> Iterator[_Draft]:
    """W117 — both endpoints of one cable land on the same element (``NG-C004``).

    Legal — a loopback plug and an MLAG peer-link on one logical switch both
    look like this — but far more often it is a copy-pasted cable document whose
    second endpoint was never edited, which quietly leaves the real neighbour
    undrawn. ``E002`` already reports the degenerate case where both ends name
    the *same port*, so this rule stays quiet there rather than doubling it.
    """
    for cable_fqn, first, second in ctx.endpoint_pairs:
        if first.owner_fqn is None or first.owner_fqn != second.owner_fqn:
            continue
        if first.ref.interface == second.ref.interface:
            continue
        yield _Draft(
            f"both endpoints of cable {_q(cable_fqn)} land on {_q(first.owner_fqn)} "
            f"({_join([first.ref.interface, second.ref.interface])}); the cable joins the element "
            f"to itself and adds no path to the topology",
            _cable_elements(cable_fqn, first, second),
            ("spec", "endpoints"),
        )


def _check_wireless_medium(ctx: _Context) -> Iterator[_Draft]:
    """E011 — the cable's medium disagrees with an endpoint's type (``NG-C006``).

    ``medium: wireless`` models an *association* rather than a wire, so both
    ends must be radios; conversely a wire cannot be plugged into a radio. Each
    half is checked because each describes a different impossible link, and
    because a medium corrected on the cable but not on the port (or the reverse)
    is exactly how one of them arises.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        wireless = first.cable.spec.medium is Medium.WIRELESS
        radios = [endpoint for endpoint in (first, second) if _is_radio(endpoint)]
        if wireless and len(radios) < 2:
            wired = [endpoint for endpoint in (first, second) if not _is_radio(endpoint)]
            yield _Draft(
                f"cable {_q(cable_fqn)} is 'medium: wireless' but "
                f"{_join([endpoint.port for endpoint in wired])} "
                f"{'is' if len(wired) == 1 else 'are'} not 'type: wifi'; a wireless link is an "
                f"association between two radios",
                _cable_elements(cable_fqn, first, second),
                ("spec", "medium"),
            )
        elif not wireless and radios:
            medium = first.cable.spec.medium.value
            yield _Draft(
                f"cable {_q(cable_fqn)} is 'medium: {medium}' but "
                f"{_join([endpoint.port for endpoint in radios])} "
                f"{'is' if len(radios) == 1 else 'are'} 'type: wifi'; a radio terminates a "
                f"wireless association, not a {medium} run",
                _cable_elements(cable_fqn, first, second),
                ("spec", "medium"),
            )


def _is_radio(endpoint: _Endpoint) -> bool:
    """Is this endpoint a wifi port? An adapter's upstream bus port is not."""
    return endpoint.interface is not None and endpoint.interface.type is InterfaceType.WIFI


def _check_speed_mismatch(ctx: _Context) -> Iterator[_Draft]:
    """W118 — a cable's ``speed`` disagrees with an endpoint's own (``NG-C008``).

    §9.4 projects ``cable.speed`` onto ``if:speed`` at both ends, so the two
    cannot both be true. An interface has no ``speed`` of its own in this
    schema — the wire decides it — with one exception: an adapter's upstream
    port carries the *host bus* rate (§8.1), and a 1 Gbps dongle cabled as if it
    were a 10 Gbps link is the mismatch worth catching.
    """
    for endpoint in ctx.endpoints:
        owner = endpoint.owner
        if not endpoint.is_upstream or not isinstance(owner, Adapter):
            continue
        declared, cable_speed = owner.upstream.speed, endpoint.cable.spec.speed
        if declared is None or cable_speed is None or declared == cable_speed:
            continue
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} is {format_bitrate(cable_speed)} but its endpoint "
            f"{_q(endpoint.port)} declares {format_bitrate(declared)}; §9.4 projects the cable's "
            f"speed onto both ends, so the two cannot both hold",
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_uncableable_endpoint(ctx: _Context) -> Iterator[_Draft]:
    """E012 — an endpoint is a loopback, vlan or bridge interface (``NG-C009``).

    Those three are software constructs: a loopback has no medium, and an SVI or
    a bridge sits *above* the ports that do. A cable drawn to one describes a
    plug that has nowhere to go, and the port it was meant for is left looking
    free.
    """
    for endpoint in ctx.endpoints:
        interface = endpoint.interface
        if interface is None or interface.type.is_cableable:
            continue
        physical = _join(
            sorted(
                candidate.name
                for candidate in (endpoint.owner.interfaces if endpoint.owner else ())
                if candidate.type.is_cableable
            )
        )
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} terminates on {_q(endpoint.port)}, which is a "
            f"{interface.type.value!r} interface; only ethernet, wifi and lag interfaces can be "
            f"cabled"
            + (f" — {_q(endpoint.owner_fqn or '')} declares {physical}" if physical else ""),
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_aggregate_endpoint(ctx: _Context) -> Iterator[_Draft]:
    """W119 — an endpoint is a LAG aggregate rather than a member (``NG-C012``).

    A bundle is logical: the wires land on its members. Cabling the aggregate
    draws one link where the inventory means several, so the diagram understates
    both the port count and the redundancy the bundle exists to provide.
    """
    for endpoint in ctx.endpoints:
        interface = endpoint.interface
        if interface is None or interface.type is not InterfaceType.LAG:
            continue
        members = _join(list(interface.members or ()))
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} terminates on the lag aggregate "
            f"{_q(endpoint.port)}; a bundle is logical, so cable its members "
            f"({members}) instead",
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_half_duplex(ctx: _Context) -> Iterator[_Draft]:
    """W120 — ``duplex: half`` on a link that involves no hub (``NG-C013``).

    Half duplex means the two ends share the medium and must arbitrate for it,
    which is what a repeater's collision domain requires. Between two switched
    ports it is either a speed/duplex negotiation that failed — the classic
    cause of a link that passes pings and collapses under load — or a value
    copied from a document that described a hub.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        if first.cable.spec.duplex is not Duplex.HALF:
            continue
        if any(isinstance(endpoint.owner, Hub) for endpoint in (first, second)):
            continue
        yield _Draft(
            f"cable {_q(cable_fqn)} is 'duplex: half' but joins {_q(first.port)} to "
            f"{_q(second.port)}, neither of which is a hub; a shared collision domain needs a "
            f"repeater",
            _cable_elements(cable_fqn, first, second),
            ("spec", "duplex"),
        )


def _check_disconnected_topology(ctx: _Context) -> Iterator[_Draft]:
    """W121 — the topology falls into separate islands (``NG-C014``).

    Reported once for the whole inventory, naming each island's smallest member
    so a reader can find them on the diagram. Islands of **one** element are
    left to ``W103``, which says the same thing about a lone device in better
    words; this rule is about the case that looks fine locally — two halves of a
    network that are each internally cabled and never meet.
    """
    # Components over the *couplers* as well as the elements, then projected back
    # onto the active elements. A run through a patch panel joins its two ends
    # (§15.2), so a plant where every server link crosses one is a connected
    # topology; computing components over the active elements alone would call
    # each of those servers an island of its own.
    links = list(_topology_links(ctx))
    nodes = list(dict.fromkeys([*ctx.owners, *(node for pair in links for node in pair)]))
    islands = [
        active
        for island in _components(nodes, links)
        if len(active := [fqn for fqn in island if fqn in ctx.owners]) > 1
        # A lone element is W103's finding, not this one.
    ]
    if len(islands) < 2:
        return
    # Sorted by representative rather than left in the order the components were
    # discovered: that order is inventory load order, which is directory order,
    # so packing two documents into one file would reorder the islands in the
    # message and in ``elements`` without anything about the network changing.
    # Which island comes first says nothing anyway — they are, by definition,
    # not connected to each other.
    ordered = sorted((min(island), len(island)) for island in islands)
    representatives = [representative for representative, _ in ordered]
    described = ", ".join(
        f"{_q(representative)} ({count_text(size, 'element')})" for representative, size in ordered
    )
    yield _Draft(
        f"the topology is disconnected: {count_text(len(islands), 'island')} with no link between "
        f"them ({described}); either a cable or an 'attached_to' is missing, or these are "
        f"separate networks that belong in separate inventories",
        tuple(representatives),
    )


def _topology_links(ctx: _Context) -> Iterator[tuple[str, str]]:
    """Every edge the graph layer draws: cables (§7.1) and attachments (§8.2).

    A cable whose endpoint names a *missing interface* still joins its two
    elements, exactly as in :attr:`_Context.connected`: ``E001`` reports the bad
    reference, and treating the link as absent would split the topology over a
    typo.

    A panel end is named by its :func:`_coupler_node` rather than by the panel,
    so a run *through* the panel is one path and two runs patched through
    different positions of one panel are two.
    """
    for _, first, second in ctx.endpoint_pairs:
        left, right = _coupler_node(first), _coupler_node(second)
        if left is not None and right is not None:
            yield left, right
    for attachment in ctx.attachments:
        if attachment.host_fqn is not None:
            yield attachment.adapter_fqn, attachment.host_fqn


def _coupler_node(endpoint: _Endpoint) -> str | None:
    """Which node a cable end belongs to in the connectivity graph.

    An active element is itself. A panel position is its **coupler** — the pair
    of positions a plug on one side reaches from the other, named after the front
    one — because a panel is not one node: two runs patched through positions 7
    and 24 of one panel are two runs, and nothing joins them (§15.2). Naming the
    panel instead would silently merge every island that happens to cross it.

    A position the panel has not got is ``NG-P001`` and keeps the panel's own
    name: there is no coupler to belong to, and the link is left where the
    reference put it.
    """
    fqn = endpoint.owner_fqn
    if fqn is None or endpoint.panel is None:
        return fqn
    port = endpoint.port
    if split_panel_port(port) is None:
        return fqn
    front = port if port.startswith(f"{FRONT}/") else endpoint.panel.opposite(port)
    return f"{fqn}#{front or port}"


def _components(nodes: Iterable[str], links: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Group ``nodes`` into connected components, each member in load order.

    A union-find rather than a traversal: the edge list is the natural input
    here, and the components come out in the load order of their first member,
    which keeps the finding stable across runs.
    """
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # path compression
            parent[node], node = root, parent[node]
        return root

    for left, right in links:
        if left not in parent or right not in parent:
            continue
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    groups: dict[str, list[str]] = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    return list(groups.values())


def _check_uncabled_interface(ctx: _Context) -> Iterator[_Draft]:
    """I002 — an interface is ``enabled: true`` but terminates no cable (``NG-C015``).

    Information rather than a complaint: a spare port is a normal thing to own,
    and an uplink whose far end is outside the inventory (an ISP hand-off) is
    normal too. It is printed because the inverse reading is just as likely —
    the cable document was never written — and because a port list with the
    unused ports marked is what makes a patching decision possible.

    Only the types a cable *can* terminate on are considered (``NG-C009``), and
    lag aggregates are excluded: ``NG-C012`` says the wires land on the members,
    so an aggregate that terminates no cable is correct by construction. Saying
    ``enabled: false`` silences the finding and documents the port at the same
    time.
    """
    for fqn, owner in ctx.owners.items():
        for index, interface in enumerate(owner.interfaces):
            if not interface.enabled or not interface.type.is_cableable:
                continue
            if interface.type is InterfaceType.LAG:
                continue
            # A veth end is ``ethernet`` and can never be cabled (``E049``), so
            # the finding would be true of every one of them and actionable on
            # none: the "spare port" reading does not apply to an interface that
            # has no socket in the first place.
            if interface.is_veth:
                continue
            if (fqn, interface.name) in ctx.terminations:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} is enabled but terminates no cable; "
                f"mark it 'enabled: false' if the port is spare",
                (fqn,),
                ("spec", "interfaces", index),
            )


# --------------------------------------------------------------------------- #
# Hubs (§10.7)
# --------------------------------------------------------------------------- #


def _check_hub_subnets(ctx: _Context) -> Iterator[_Draft]:
    """W122 — elements on one hub are addressed in different subnets (``NG-H005``).

    A hub is a repeater: every port sees every frame, so everything plugged into
    one is in a single broadcast domain and belongs in a single prefix. Ports in
    prefixes that do not meet cannot talk to each other despite being wired
    together — the network looks built and is not.

    Hubs cabled to each other form one collision domain and are examined as a
    unit. The two address families are checked separately, since a v4-only host
    next to a v6-only host is a dual-stack rollout rather than a mistake.
    """
    for hubs, peers in _hub_domains(ctx):
        for version in (4, 6):
            addressed: list[tuple[str, str, frozenset[IPNetwork]]] = []
            for owner_fqn, port, prefixes in peers:
                family = frozenset(net for net in prefixes if net.version == version)
                if family:
                    addressed.append((owner_fqn, port, family))
            if len(addressed) < 2:
                continue
            if frozenset.intersection(*(prefixes for _, _, prefixes in addressed)):
                continue
            # By port name, not by the order the cables happened to load: every
            # port on a hub is equally a member of the one collision domain, so
            # nothing distinguishes a "first" one except which file it came from.
            addressed.sort(key=lambda entry: (entry[0], entry[1]))
            described = ", ".join(
                f"{_q(port)} in {_join_plain(sorted(str(net) for net in prefixes))}"
                for _, port, prefixes in addressed
            )
            yield _Draft(
                f"hub {_q(min(hubs))} joins {count_text(len(addressed), f'IPv{version} port')} "
                f"that share no prefix: {described}. A hub is one broadcast domain, so its "
                f"ports cannot reach each other from different subnets.",
                (*sorted(hubs), *dict.fromkeys(owner_fqn for owner_fqn, _, _ in addressed)),
                ("spec", "interfaces"),
            )


def _hub_domains(ctx: _Context) -> Iterator[tuple[tuple[str, ...], list[_HubPeer]]]:
    """Each collision domain: the hubs forming it, and the ports cabled into it.

    Hubs joined by a cable repeat each other's frames, so they are one domain
    and are grouped before their peers are collected. A port's addresses are
    read through the LAG master (§10.6) and only the routable ones count —
    ``fe80::/64`` on every interface is not a subnet anybody chose.
    """
    hub_fqns = [fqn for fqn, device in ctx.inventory.devices.items() if isinstance(device, Hub)]
    if not hub_fqns:
        return

    hub_set = set(hub_fqns)
    links: list[tuple[str, str]] = [
        (first.owner_fqn, second.owner_fqn)
        for _, first, second in ctx.endpoint_pairs
        if first.owner_fqn is not None
        and second.owner_fqn is not None
        and first.owner_fqn in hub_set
        and second.owner_fqn in hub_set
    ]
    for domain in _components(hub_fqns, links):
        members = set(domain)
        peers: list[_HubPeer] = []
        for _, first, second in _linked_endpoints(ctx):
            for near, far in ((first, second), (second, first)):
                if near.owner_fqn not in members or far.owner_fqn in members:
                    continue
                interface = ctx.effective(far)
                if interface is None or far.owner_fqn is None:
                    continue
                prefixes = frozenset(
                    address.network
                    for address in interface.addresses()
                    if is_routable_address(address)
                )
                if prefixes:
                    peers.append((far.owner_fqn, f"{far.owner_fqn}:{interface.name}", prefixes))
        yield tuple(domain), peers


# --------------------------------------------------------------------------- #
# Wireless (§10.14)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Radio:
    """One ``wifi`` interface that declares a ``wireless`` block (§6.2.6).

    Resolved into this shape because four rules read the same three things —
    which element the radio is on, which side of the association it is, and what
    it puts on the air — and because a ``wifi`` interface *without* a block has
    to stay invisible to all of them: an absent block is "not modelled", not
    "no SSIDs".
    """

    owner_fqn: str
    owner: InterfaceOwner
    interface: Interface
    wireless: WirelessConfig

    @property
    def port(self) -> str:
        """``element:interface``, the spelling every diagnostic uses."""
        return f"{self.owner_fqn}:{self.interface.name}"

    @property
    def is_ap(self) -> bool:
        return self.wireless.role.is_ap

    @property
    def domains(self) -> frozenset[int | None]:
        """The broadcast domains this radio bridges onto the air.

        ``None`` is the untagged domain, and is also what a radio with no BSS at
        all is taken to be on: an access point that names no SSID still bridges
        *something*, and treating it as tagged would make it share a domain with
        nobody.
        """
        if not self.wireless.bss:
            return frozenset({None})
        return frozenset(entry.vlan for entry in self.wireless.bss)

    def path(self, *suffix: str | int) -> tuple[str | int, ...]:
        return _interface_path(self.owner, self.interface, "wireless", *suffix)


def _radios(ctx: _Context) -> Iterator[_Radio]:
    """Every configured radio in the inventory, in load order."""
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            wireless = interface.wireless
            if wireless is not None:
                yield _Radio(fqn, owner, interface, wireless)


def _link_radios(ctx: _Context) -> Iterator[tuple[str, tuple[_Radio, _Radio]]]:
    """Each wireless cable whose two ends both declare a ``wireless`` block.

    ``E011`` already reports a ``medium: wireless`` cable that lands on
    something which is not a radio at all, so a link is only interesting here
    once both ends are radios *and* both have said what they are.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        if first.cable.spec.medium is not Medium.WIRELESS:
            continue
        ends: list[_Radio] = []
        for end in (first, second):
            owner = ctx.owners.get(end.owner_fqn) if end.owner_fqn is not None else None
            interface = end.interface
            if owner is None or interface is None or interface.wireless is None:
                continue
            ends.append(_Radio(str(end.owner_fqn), owner, interface, interface.wireless))
        if len(ends) == 2:
            yield cable_fqn, (ends[0], ends[1])


def _check_wireless_association(ctx: _Context) -> Iterator[_Draft]:
    """E028 — a wireless link is not an AP-to-client association (``NG-W007``).

    An 802.11 link has a direction that a cable does not: one radio beacons and
    the other joins it. Two access points on one link is a document describing
    interference rather than a link, and two client radios is a link that no
    frame ever crosses — neither will beacon, so neither can be joined. A mesh
    node's backhaul is the second case's legitimate cousin and is written as
    ``role: mesh`` against the AP-role radio it associates to.
    """
    for cable_fqn, (first, second) in _link_radios(ctx):
        aps = [radio for radio in (first, second) if radio.is_ap]
        if len(aps) == 1:
            continue
        if len(aps) == 2:
            problem = (
                f"joins two 'ap' radios ({_join([first.port, second.port])}); one end has to "
                f"associate to the other, so it must be 'role: station' or 'role: mesh'"
            )
        else:
            roles = _join_plain(
                [
                    f"{_q(radio.port)} is {_q(radio.wireless.role.value)}"
                    for radio in (first, second)
                ]
            )
            problem = (
                f"joins no 'ap' radio ({roles}); neither end beacons, so there is no BSS for "
                f"the other to associate to"
            )
        yield _Draft(
            f"cable {_q(cable_fqn)} {problem}",
            tuple(dict.fromkeys([cable_fqn, first.owner_fqn, second.owner_fqn])),
            ("spec", "endpoints"),
        )


def _check_duplicate_bssid(ctx: _Context) -> Iterator[_Draft]:
    """E029 — two radios advertise the same BSSID (``NG-W008``).

    A BSSID identifies one basic service set to every client in earshot. Two of
    them answering to one address is the wireless equivalent of ``E003``: frames
    for one arrive at the other, and a client that roams between them never
    knows it moved. Repeats *within* one radio are ``NG-W005``, at schema time.

    Only ``ap`` radios are compared. A client's BSS entry records the BSSID it
    joined, so it is *supposed* to repeat the access point's — that is what
    makes it the same service set.
    """
    groups: dict[str, list[tuple[_Radio, int, Bss]]] = {}
    for radio in _radios(ctx):
        if not radio.is_ap:
            continue
        for index, entry in enumerate(radio.wireless.bss):
            if entry.bssid is not None:
                groups.setdefault(entry.bssid, []).append((radio, index, entry))

    for bssid, entries in groups.items():
        if len(entries) < 2:
            continue
        # Two SSIDs sharing an address are equally guilty; order by port so the
        # finding does not move when an unrelated file is renamed (see _by_port).
        ordered = sorted(entries, key=lambda entry: (entry[0].port, entry[2].ssid))
        described = [f"{_q(radio.port)} ({_q(entry.ssid)})" for radio, _, entry in ordered]
        yield _Draft(
            f"BSSID {bssid} is advertised by {count_text(len(ordered), 'BSS')}: "
            f"{_join_plain(described)}",
            tuple(dict.fromkeys(radio.owner_fqn for radio, _, _ in ordered)),
            ordered[0][0].path("bss", ordered[0][1], "bssid"),
        )


def _check_bss_vlan_carried(ctx: _Context) -> Iterator[_Draft]:
    """E030 — an SSID's VLAN is carried nowhere on the AP (``NG-W009``).

    An SSID that maps to a VLAN is a bridge between the air and that VLAN. If
    no interface of the access point is a member of it, the far side of that
    bridge is missing: clients associate, get an address from nowhere and reach
    nothing. ``W113`` is the neighbouring, weaker statement — the VLAN is not in
    the device's ``vlans`` database — and this one is about the ports.

    A port trunking ``all`` carries whatever is asked of it, so an access point
    with one of those cannot be wrong here.
    """
    for radio in _radios(ctx):
        if not radio.is_ap:
            continue
        carried: set[int] = set()
        unbounded = False
        for interface in radio.owner.interfaces:
            vlan = interface.vlan
            if vlan is None:
                continue
            if _trunks_every_vlan(vlan):
                unbounded = True
                break
            carried |= vlan.vlan_ids()
        if unbounded:
            continue
        for index, entry in enumerate(radio.wireless.bss):
            if entry.vlan is None or entry.vlan in carried:
                continue
            yield _Draft(
                f"SSID {_q(entry.ssid)} on {_q(radio.port)} is bridged into VLAN {entry.vlan}, "
                f"which {_q(radio.owner_fqn)} carries on no interface; the access point has no "
                f"path out of that VLAN",
                (radio.owner_fqn,),
                radio.path("bss", index, "vlan"),
            )


def _check_associated_ssid(ctx: _Context) -> Iterator[_Draft]:
    """E031 — a client joins an SSID its AP does not advertise (``NG-W010``).

    The association names a BSS, and the BSS is the access point's to define.
    An SSID that appears on the client and nowhere on the AP is either a typo or
    a record of the network as it used to be; either way the link drawn from it
    does not exist.

    An access point that lists no BSS at all is not modelling its SSIDs, so
    there is nothing to contradict and the rule stays quiet.
    """
    for _, (first, second) in _link_radios(ctx):
        aps = [radio for radio in (first, second) if radio.is_ap]
        if len(aps) != 1:
            continue  # E028 owns a link that is not one AP and one client
        ap = aps[0]
        client = second if ap is first else first
        advertised = ap.wireless.ssids
        if not advertised:
            continue
        for index, entry in enumerate(client.wireless.bss):
            if entry.ssid in advertised:
                continue
            yield _Draft(
                f"{_q(client.port)} is associated to SSID {_q(entry.ssid)}, which "
                f"{_q(ap.port)} does not advertise; it beacons "
                f"{_join(list(advertised))}",
                tuple(dict.fromkeys([client.owner_fqn, ap.owner_fqn])),
                client.path("bss", index, "ssid"),
            )


def _check_cochannel_aps(ctx: _Context) -> Iterator[_Draft]:
    """W134 — two APs in one broadcast domain overlap in frequency (``NG-W011``).

    Two access points bridging the same domain are meant to extend each other's
    coverage, which only works if they are on different frequencies: radios that
    overlap take turns instead of working in parallel, so the pair delivers
    roughly the throughput of one. It is a warning rather than an error because
    a deliberate same-channel deployment exists — a repeater has no choice but
    to sit on its parent's channel — and because the schema records no
    geometry, so netviz cannot know the two are far enough apart to be
    harmless.

    "One broadcast domain" is read as: the two elements are joined by the
    topology *and* the radios put a common VLAN on the air (an SSID with no
    ``vlan`` counts as the untagged domain). Both halves matter — VLAN 10 on two
    unconnected islands is two domains that share a number, exactly as in
    :func:`netviz.graph.broadcast_domains`.
    """
    radios = [radio for radio in _radios(ctx) if radio.is_ap and radio.wireless.band is not None]
    if len(radios) < 2:
        return

    islands = {
        fqn: index
        for index, component in enumerate(_components(ctx.owners, _topology_links(ctx)))
        for fqn in component
    }
    for left, right in itertools.combinations(radios, 2):
        if islands.get(left.owner_fqn, -1) != islands.get(right.owner_fqn, -2):
            continue
        if left.wireless.band is not right.wireless.band:
            continue
        shared = left.domains & right.domains
        if not shared:
            continue
        overlap = _overlapping_spans(left, right)
        if overlap is None:
            continue
        first, second = sorted((left, right), key=lambda radio: radio.port)
        yield _Draft(
            f"access points {_q(first.port)} ({_describe_radio(first)}) and {_q(second.port)} "
            f"({_describe_radio(second)}) both serve {_describe_domains(shared)} and their "
            f"channels overlap ({overlap}); co-channel radios take turns rather than adding up",
            tuple(dict.fromkeys([first.owner_fqn, second.owner_fqn])),
            first.path("channel"),
        )


def _overlapping_spans(left: _Radio, right: _Radio) -> str | None:
    """``2402-2422 MHz and 2412-2432 MHz`` when the two radios overlap.

    ``None`` means they do not, which includes either of them not stating a
    channel: a band alone says nothing about which 20 MHz slice is in use.
    """
    first, second = left.wireless.span_mhz(), right.wireless.span_mhz()
    if first is None or second is None:
        return None
    if first[0] >= second[1] or second[0] >= first[1]:
        return None
    return " and ".join(f"{low:g}-{high:g} MHz" for low, high in (first, second))


def _describe_radio(radio: _Radio) -> str:
    """``channel 6/2.4GHz, 40 MHz`` — how a radio is tuned."""
    parts = [f"channel {radio.wireless.channel_text}"]
    if radio.wireless.width_mhz is not None:
        parts.append(f"{radio.wireless.width_mhz} MHz")
    return ", ".join(parts)


def _describe_domains(domains: frozenset[int | None]) -> str:
    """``VLAN 10`` / ``the untagged domain``, for a set of them."""
    return _join_plain([_describe_scope(domain) for domain in sorted(domains, key=_domain_sort)])


def _domain_sort(domain: int | None) -> tuple[int, int]:
    """Order the untagged domain first, then VLANs by id."""
    return (1, domain) if domain is not None else (0, 0)


# --------------------------------------------------------------------------- #
# Adapters (§10.8)
# --------------------------------------------------------------------------- #


def _check_attachment_target(ctx: _Context) -> Iterator[_Draft]:
    """E015 — an ``attached_to`` names nothing that could host the adapter (``NG-X001``).

    Pass 2 checks the *grammar* of the reference — a bare element name, never a
    ``device:interface``. Whether it lands on anything is a question about the
    whole inventory and belongs here. The renderer drops the attachment edge
    when it does not, so without this rule a laptop would be drawn floating next
    to its own dongle with only a note on stderr to say why.
    """
    for attachment in ctx.attachments:
        prefix = f"adapter {_q(attachment.adapter_fqn)}: upstream.attached_to"
        elements = (attachment.adapter_fqn,)
        if attachment.ambiguous:
            yield _Draft(
                f"{prefix} {_q(attachment.ref)} is ambiguous here; it matches "
                f"{_join(sorted(attachment.ambiguous))}. Write it fully qualified, or move the "
                f"adapter next to the host it plugs into.",
                elements,
                attachment.field_path,
            )
        elif attachment.host is None:
            yield _Draft(
                f"{prefix} names no element declared in this inventory: {_q(attachment.ref)}. "
                f"The adapter is drawn detached from its host.",
                elements,
                attachment.field_path,
            )
        elif isinstance(attachment.host, PatchPanel):
            # ``E023`` reports every "a panel cannot do that" in one voice.
            continue
        elif not isinstance(attachment.host, _OWNER_TYPES):
            yield _Draft(
                f"{prefix} names {_q(attachment.ref)}, which is a {attachment.host.kind}; an "
                f"adapter plugs into a device or another adapter",
                (*elements, attachment.host_fqn or attachment.ref),
                attachment.field_path,
            )


def _check_attachment_and_cable(ctx: _Context) -> Iterator[_Draft]:
    """E013 — an adapter's upstream port is both attached and cabled (``NG-X005``).

    §8.2 declares the host attachment exactly once: ``attached_to`` *is* the
    edge, and no cable document is needed or permitted for it. Both spellings at
    once give the adapter two upstream links where the hardware has one plug,
    and leave a reader unable to tell which of the two is current.
    """
    attached = {
        attachment.adapter_fqn: attachment
        for attachment in ctx.attachments
        if attachment.host_fqn is not None
    }
    for endpoint in ctx.endpoints:
        attachment = attached.get(endpoint.owner_fqn or "")
        if not endpoint.is_upstream or attachment is None:
            continue
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} lands on the upstream port {_q(endpoint.port)} of "
            f"adapter {_q(attachment.adapter_fqn)}, which is already attached to "
            f"{_q(attachment.host_fqn or attachment.ref)}; the host attachment is declared once, "
            f"either as 'attached_to' or as a cable",
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_attachment_cycle(ctx: _Context) -> Iterator[_Draft]:
    """E014 — ``attached_to`` attachments form a cycle (``NG-X006``).

    A dock plugged into a dongle plugged back into the dock is not hardware
    anybody can build, and every consumer that walks the chain to find the host
    — the renderer's adapter collapsing, the VLAN propagation of §8.2 — would
    have to defend itself against it.
    """
    upstream = {
        attachment.adapter_fqn: attachment.host_fqn
        for attachment in ctx.attachments
        if attachment.host_fqn is not None
    }
    for cycle in _attachment_cycles(upstream):
        chain = " -> ".join(_q(fqn) for fqn in (*cycle, cycle[0]))
        yield _Draft(
            f"adapter attachment is cyclic: {chain}. "
            f"{count_text(len(cycle), 'adapter')} would each have to be plugged into the next.",
            tuple(cycle),
            ("spec", "upstream", "attached_to"),
        )


def _attachment_cycles(upstream: Mapping[str, str]) -> list[list[str]]:
    """The cycles of the ``adapter -> host`` graph, in load order.

    Every adapter has at most one host, so the graph is functional: following it
    from any node either runs out or closes exactly one loop. That makes a plain
    walk with a three-colour marking enough, and reports each cycle once.
    """
    unvisited, active, done = 0, 1, 2
    state: dict[str, int] = {}
    cycles: list[list[str]] = []

    for start in upstream:
        if state.get(start, unvisited) != unvisited:
            continue
        path: list[str] = []
        node: str | None = start
        while node is not None and state.get(node, unvisited) == unvisited:
            state[node] = active
            path.append(node)
            node = upstream.get(node)
        if node is not None and state.get(node) == active:
            cycles.append(path[path.index(node) :])
        for visited in path:
            state[visited] = done
    return cycles


def _check_unattached_adapter(ctx: _Context) -> Iterator[_Draft]:
    """W123 — an adapter is cabled downstream but has no host (``NG-X002``).

    §8.2 calls a free-standing adapter a spare in a drawer or a media converter
    in a run. Once something is patched into its downstream ports it is neither:
    the dongle is in use, and the host it is plugged into was left out. An
    adapter whose *upstream* port terminates a cable is exempt — that cable is
    the attachment, spelled the other legal way (see ``E013``).
    """
    attached = {attachment.adapter_fqn for attachment in ctx.attachments}
    for fqn, adapter in ctx.inventory.adapters.items():
        if fqn in attached or (fqn, adapter.upstream.name) in ctx.terminations:
            continue
        cabled = [
            interface.name
            for interface in adapter.interfaces
            if (fqn, interface.name) in ctx.terminations
        ]
        if not cabled:
            continue
        yield _Draft(
            f"adapter {_q(fqn)} has {count_text(len(cabled), 'cabled downstream port')} "
            f"({_join(cabled)}) but no 'upstream.attached_to'; nothing says which machine it is "
            f"plugged into",
            (fqn,),
            ("spec", "upstream"),
        )


def _check_attachment_is_a_host(ctx: _Context) -> Iterator[_Draft]:
    """W124 — ``attached_to`` points at a hub or a switch (``NG-X007``).

    An adapter is a port of the machine it plugs into, so its host is a computer,
    a server, a router — something with a bus. Network gear takes a cable. A
    media converter sitting between two switches is the case that tempts this
    spelling, and §8.2 gives it a better one: ``passthrough: false`` with a cable
    on each side, which draws the converter as the distinct node it is.
    """
    for attachment in ctx.attachments:
        host = attachment.host
        if not isinstance(host, _NOT_A_HOST_TYPES):
            continue
        yield _Draft(
            f"adapter {_q(attachment.adapter_fqn)} is attached to {_q(attachment.host_fqn or '')},"
            f" which is a {host.kind}; adapters plug into hosts. Model a converter between two "
            f"switches with 'passthrough: false' and a cable on each side.",
            (attachment.adapter_fqn, attachment.host_fqn or attachment.ref),
            attachment.field_path,
        )


# --------------------------------------------------------------------------- #
# Tunnels (§10.10)
# --------------------------------------------------------------------------- #


def _check_tunnel_endpoints(ctx: _Context) -> Iterator[_Draft]:
    """E016 — a tunnel endpoint references an unknown element or interface."""
    for end in ctx.tunnel_ends:
        elements = (end.tunnel_fqn,)
        prefix = f"tunnel {_q(end.tunnel_fqn)} endpoint {end.ref}"
        owner, owner_fqn = end.owner, end.owner_fqn

        if end.ambiguous:
            yield _Draft(
                f"{prefix}: {_q(end.ref.device)} is ambiguous here; it matches "
                f"{_join(sorted(end.ambiguous))}. Move the tunnel next to the element it "
                f"refers to, or rename one of them.",
                elements,
                end.field_path,
            )
        elif end.wrong_kind == PATCHPANEL_KIND:
            continue  # ``E023``: one voice for everything a panel cannot be.
        elif end.wrong_kind is not None:
            yield _Draft(
                f"{prefix}: {_q(end.ref.device)} is a {end.wrong_kind}, which owns no interfaces",
                elements,
                end.field_path,
            )
        elif owner is None or owner_fqn is None:
            yield _Draft(
                f"{prefix}: no element named {_q(end.ref.device)} is declared in this inventory",
                elements,
                end.field_path,
            )
        elif not end.resolved:
            yield _Draft(
                f"{prefix}: {_q(owner_fqn)} has no interface {_q(end.ref.interface)}; "
                f"it declares {_join(sorted(owner.interface_names()))}",
                (*elements, owner_fqn),
                end.field_path,
            )


def _check_tunnel_endpoint_type(ctx: _Context) -> Iterator[_Draft]:
    """E017 — a tunnel endpoint is not a ``tunnel`` interface (``NG-T003``).

    The endpoint of a tunnel is the *virtual* interface the operating system
    presents — ``wg0``, ``ipsec0``, ``vxlan100`` — not the physical port its
    outer packets happen to leave by. Landing a tunnel on ``eth0`` would draw an
    overlay on top of the very link that carries it, and would put the tunnel's
    inner addresses on the underlay interface.
    """
    for end in ctx.tunnel_ends:
        interface = end.interface
        if interface is None or end.owner_fqn is None:
            continue
        if interface.type is InterfaceType.TUNNEL:
            continue
        yield _Draft(
            f"tunnel {_q(end.tunnel_fqn)} terminates on {_q(end.port)}, which is of type "
            f"{_q(interface.type.value)}; a tunnel endpoint must be a 'tunnel' interface. "
            f"Declare one and point 'parent' at {_q(interface.name)} if the outer packets "
            f"leave by it.",
            (end.tunnel_fqn, end.owner_fqn),
            end.field_path,
        )


def _check_encapsulation_target(ctx: _Context) -> Iterator[_Draft]:
    """E018 — a tunnel's ``over`` names no tunnel (``NG-T004``)."""
    for step in ctx.encapsulations:
        if step.ambiguous:
            yield _Draft(
                f"tunnel {_q(step.tunnel_fqn)} runs over {_q(step.ref)}, which is ambiguous "
                f"here; it matches {_join(sorted(step.ambiguous))}",
                (step.tunnel_fqn,),
                step.field_path,
            )
        elif step.wrong_kind is not None:
            yield _Draft(
                f"tunnel {_q(step.tunnel_fqn)} runs over {_q(step.ref)}, which is a "
                f"{step.wrong_kind}; 'over' names the tunnel this one is encapsulated in. "
                f"A tunnel that runs directly over the physical topology omits it.",
                (step.tunnel_fqn,),
                step.field_path,
            )
        elif step.over_fqn is None:
            yield _Draft(
                f"tunnel {_q(step.tunnel_fqn)} runs over {_q(step.ref)}, which is not declared "
                f"in this inventory",
                (step.tunnel_fqn,),
                step.field_path,
            )


def _check_encapsulation_cycle(ctx: _Context) -> Iterator[_Draft]:
    """E019 — the ``over`` references loop (``NG-T005``).

    A tunnel carried by a tunnel carried by the first is not a deep stack, it is
    a definition with no bottom: nothing in it ever reaches a real packet.
    """
    over = {step.tunnel_fqn: step.over_fqn for step in ctx.encapsulations if step.over_fqn}
    for cycle in _attachment_cycles(over):
        anchor = cycle[0]
        yield _Draft(
            f"tunnel encapsulation loops: {' runs over '.join(_q(fqn) for fqn in cycle)} "
            f"runs over {_q(anchor)}. One of them has to reach the underlay network.",
            tuple(cycle),
            ("spec", "over"),
        )


def _check_underlay_reach(ctx: _Context) -> Iterator[_Draft]:
    """W125 — an overlay's endpoints are not all reachable through its underlay.

    ``vxlan over ipsec`` only works where the IPsec tunnel actually goes. When
    the outer tunnel does not terminate on every element the inner one does,
    the inner tunnel's outer packets have no protected path for at least one of
    its ends — the overlay is drawn joining two sites that cannot in fact reach
    each other that way.
    """
    for step in ctx.encapsulations:
        if step.over_fqn is None:
            continue
        inner = ctx.tunnel_elements(step.tunnel_fqn)
        outer = ctx.tunnel_elements(step.over_fqn)
        stranded = sorted(inner - outer)
        if not stranded or not outer:
            continue
        yield _Draft(
            f"tunnel {_q(step.tunnel_fqn)} runs over {_q(step.over_fqn)}, but "
            f"{_join(stranded)} {'is' if len(stranded) == 1 else 'are'} not an endpoint of "
            f"the underlay; the outer packets have no such path",
            (step.tunnel_fqn, step.over_fqn, *stranded),
            step.field_path,
        )


def _check_tunnel_mtu(ctx: _Context) -> Iterator[_Draft]:
    """W126 — an overlay MTU does not fit inside its underlay (``NG-T011``).

    Encapsulation is not free: every header in the stack comes off the payload
    the overlay can carry. An overlay MTU that ignores it produces packets the
    underlay has to fragment or drop, which is the classic "small transfers work,
    large ones hang" failure.
    """
    for step in ctx.encapsulations:
        outer, outer_fqn = step.over, step.over_fqn
        if outer is None or outer_fqn is None:
            continue
        spec = step.tunnel.spec
        if spec.mtu is None or outer.spec.mtu is None:
            continue
        # The underlay's own MTU already accounts for its own headers, so what
        # is left for this tunnel is that number minus this tunnel's overhead.
        budget = outer.spec.mtu - spec.type.overhead_bytes
        if spec.mtu <= budget:
            continue
        yield _Draft(
            f"tunnel {_q(step.tunnel_fqn)} has mtu {spec.mtu} but runs over {_q(outer_fqn)}, "
            f"whose mtu {outer.spec.mtu} leaves {budget} after {spec.type.overhead_bytes} bytes "
            f"of {spec.type} encapsulation; large packets will be fragmented or dropped",
            (step.tunnel_fqn, outer_fqn),
            ("spec", "mtu"),
        )


def _check_cleartext_tunnel(ctx: _Context) -> Iterator[_Draft]:
    """W127 — a tunnel carries traffic in the clear (``NG-T012``).

    GRE, VXLAN, Geneve, L2TP and PPTP encrypt nothing — PPTP's MPPE is broken,
    so it counts as cleartext however it is configured. That is perfectly
    correct inside a data centre and perfectly wrong across the internet, and a
    diagram is exactly where the difference should be visible. Nesting silences
    the finding: a VXLAN inside an IPsec tunnel is protected by the underlay,
    which is why ``over`` exists.
    """
    over = {step.tunnel_fqn: step.over_fqn for step in ctx.encapsulations if step.over_fqn}
    for fqn, tunnel in ctx.inventory.tunnels.items():
        if tunnel.encrypts:
            continue
        protector = _encrypting_underlay(fqn, over, ctx.inventory.tunnels)
        if protector is not None:
            continue
        yield _Draft(
            f"tunnel {_q(fqn)} is {tunnel.spec.type}, which encrypts nothing, and no tunnel "
            f"in its 'over' chain does either; everything it carries crosses the underlay in "
            f"the clear. Nest it inside an encrypting tunnel, or set 'encrypted: true' if the "
            f"deployment protects it some other way.",
            (fqn,),
            ("spec", "type"),
        )


def _encrypting_underlay(
    fqn: str, over: Mapping[str, str], tunnels: Mapping[str, Tunnel]
) -> str | None:
    """The nearest tunnel in ``fqn``'s ``over`` chain that encrypts, if any."""
    seen = {fqn}
    current = over.get(fqn)
    while current is not None and current not in seen:
        outer = tunnels.get(current)
        if outer is None:
            return None
        if outer.encrypts:
            return current
        seen.add(current)
        current = over.get(current)
    return None


def _check_unused_tunnel_interface(ctx: _Context) -> Iterator[_Draft]:
    """W128 — a ``tunnel`` interface is the endpoint of no tunnel (``NG-T013``).

    The counterpart of ``I002`` for the overlay: a ``tunnel`` interface with no
    ``tunnel`` document naming it describes one end of something the inventory
    never says the other end of, so it is drawn as a port that goes nowhere.
    """
    for fqn, owner in ctx.owners.items():
        for index, interface in enumerate(owner.interfaces):
            if interface.type is not InterfaceType.TUNNEL or not interface.enabled:
                continue
            if (fqn, interface.name) in ctx.tunnel_ports:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} is a tunnel interface but no "
                f"'tunnel' document names it, so the far end is unknown",
                (fqn,),
                ("spec", "interfaces", index),
            )


def _check_vni_clash(ctx: _Context) -> Iterator[_Draft]:
    """W129 — two tunnels on one element share a VNI (``NG-T014``).

    A VXLAN identifier names a virtual network *on a VTEP*. Two tunnels reusing
    one on the same element are either the same overlay written twice or two
    overlays that will bridge into each other.

    Both the groups and the names within a group are sorted before anything is
    reported. ``ctx.tunnel_ends`` is in discovery order, which is the order the
    files were walked in, so without the sort the *same* inventory split across
    directories differently produced the two tunnel names in the other order —
    and, where one pair of tunnels clashes on two elements at once, reported the
    other element. Neither is a different network.
    """
    by_key: dict[tuple[str, int], list[str]] = {}
    for end in ctx.tunnel_ends:
        vni = end.tunnel.spec.vni
        if vni is None or end.owner_fqn is None:
            continue
        holders = by_key.setdefault((end.owner_fqn, vni), [])
        if end.tunnel_fqn not in holders:
            holders.append(end.tunnel_fqn)

    reported: set[tuple[str, ...]] = set()
    for (owner_fqn, vni), unsorted_holders in sorted(by_key.items()):
        holders = sorted(unsorted_holders)
        if len(holders) < 2 or tuple(holders) in reported:
            continue
        reported.add(tuple(holders))
        yield _Draft(
            f"element {_q(owner_fqn)} terminates {len(holders)} tunnels that all use vni "
            f"{vni}: {_join(holders)}",
            (holders[0], *holders[1:], owner_fqn),
            ("spec", "vni"),
        )


def _check_nonstandard_port(ctx: _Context) -> Iterator[_Draft]:
    """I003 — a tunnel listens on a port other than the registered one.

    Information rather than a complaint: moving WireGuard off 51820 is a normal
    thing to do. It is printed because the port is the one fact a firewall rule
    needs and the one most likely to have been copied from another tunnel.
    """
    for fqn, tunnel in ctx.inventory.tunnels.items():
        default = tunnel.spec.type.default_port
        port = tunnel.spec.port
        if default is None or port is None or port == default:
            continue
        yield _Draft(
            f"tunnel {_q(fqn)} listens on {tunnel.spec.type.transport}/{port}; the registered "
            f"port for {tunnel.spec.type} is {default}",
            (fqn,),
            ("spec", "port"),
        )


# --------------------------------------------------------------------------- #
# Patch panels (§10.12)
# --------------------------------------------------------------------------- #


def _check_panel_position(ctx: _Context) -> Iterator[_Draft]:
    """E021 — a cable terminates on a position the panel does not have (``NG-P001``).

    A panel's positions come from ``spec.ports``, so ``front/25`` on a 24-port
    panel is not a typo the reader can see by looking at the panel document —
    it is a typo they can only see next to the range. Naming the range in the
    diagnostic is what makes the two comparable, and is why this is not the
    generic ``E001``: listing 48 interface names would bury the one fact that
    matters.
    """
    for endpoint in ctx.endpoints:
        panel = endpoint.panel
        if panel is None or endpoint.resolved:
            continue
        sides = " and ".join(f"{side}/<n>" for side in PanelSide)
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} terminates on {_q(endpoint.port)}, but patch "
            f"panel {_q(endpoint.owner_fqn or '')} has positions {panel.spec.ports} and its "
            f"ports are named {sides}",
            (endpoint.cable_fqn, endpoint.owner_fqn or endpoint.ref.device),
            endpoint.field_path,
        )


def _check_panel_double_termination(ctx: _Context) -> Iterator[_Draft]:
    """E022 — a panel position terminates more than one cable (``NG-P003``).

    A coupler is a hole with one plug in it on each side. Two cables in one
    position is a patch record that cannot be true, and it is worse than the
    device-port case (``E002``) because a panel is invisible below
    ``--layer physical``: the run would silently be spliced through whichever
    cable happened to be declared first.
    """
    for (panel_fqn, position), endpoints in ctx.panel_terminations.items():
        if len(endpoints) < 2:
            continue
        cables = list(dict.fromkeys(endpoint.cable_fqn for endpoint in endpoints))
        port = f"{panel_fqn}:{position}"
        if len(cables) == 1:
            yield _Draft(
                f"both endpoints of cable {_q(cables[0])} terminate on {_q(port)}; a cable "
                f"joins two distinct positions",
                (cables[0], panel_fqn),
                ("spec", "endpoints"),
            )
        else:
            yield _Draft(
                f"patch-panel position {_q(port)} is terminated by {len(cables)} cables: "
                f"{_join(cables)}. A coupler takes one plug per side.",
                (*cables, panel_fqn),
                ("spec", "endpoints"),
            )


def _check_panel_as_active_element(ctx: _Context) -> Iterator[_Draft]:
    """E023 — a panel is named where an active element is required (``NG-P004``).

    A panel has no bus to plug an adapter into and no operating system to
    terminate a tunnel on. Both spellings are the same mistake — reading the
    panel as the device on the other side of it — and both are worth a
    diagnostic that says so, because the alternative is a diagram in which a
    dongle hangs off a hole in a rack.
    """
    for attachment in ctx.attachments:
        if not isinstance(attachment.host, PatchPanel):
            continue
        yield _Draft(
            f"adapter {_q(attachment.adapter_fqn)}: upstream.attached_to names "
            f"{_q(attachment.ref)}, which is a patch panel; a panel is passive and has no host "
            f"bus. Cable the adapter to a panel position instead.",
            (attachment.adapter_fqn, attachment.host_fqn or attachment.ref),
            attachment.field_path,
        )

    for end in ctx.tunnel_ends:
        if end.wrong_kind != PATCHPANEL_KIND:
            continue
        yield _Draft(
            f"tunnel {_q(end.tunnel_fqn)} endpoint {end.ref}: {_q(end.ref.device)} is a patch "
            f"panel; a tunnel terminates on a 'tunnel' interface of an active element, and a "
            f"panel runs no software",
            (end.tunnel_fqn,),
            end.field_path,
        )


def _check_panel_loop(ctx: _Context) -> Iterator[_Draft]:
    """E024 — a run leaves a panel and is patched back into it (``NG-P005``).

    Follow a run through the couplers and it must reach an active port. A run
    that arrives back at a segment it has already crossed never will: it is a
    loop of copper between two holes, and at layer 2 it is a broadcast storm
    waiting for someone to plug the last cable in. The graph layer drops such a
    run rather than splicing it, so without this rule the only sign of it would
    be a link that quietly is not drawn.
    """
    reported: set[frozenset[str]] = set()
    for start in ctx.panel_terminations.values():
        for endpoint in start:
            loop = _patch_loop(endpoint, ctx)
            if loop is None or frozenset(loop) in reported:
                continue
            reported.add(frozenset(loop))
            yield _Draft(
                f"the patch run through {_join(loop)} comes back into a position it has "
                f"already crossed; a patch panel cannot be patched into itself",
                tuple(loop),
                ("spec", "endpoints"),
            )


def _patch_loop(start: _Endpoint, ctx: _Context) -> tuple[str, ...] | None:
    """Walk the run ``start`` belongs to; return its cables when it loops.

    ``None`` means the run terminates — on an active port, on an uncoupled
    position, or on a coupler nothing is patched into. Only the third of those
    is a problem, and it is ``W133``'s rather than this one's.
    """
    seen: list[str] = []
    current: _Endpoint | None = start
    while current is not None:
        if current.cable_fqn in seen:
            return tuple(seen[seen.index(current.cable_fqn) :])
        seen.append(current.cable_fqn)
        panel_fqn, panel = current.owner_fqn, current.panel
        if panel is None or panel_fqn is None:
            return None
        far = _far_end(current, ctx)
        if far is None or far.panel is None or far.owner_fqn is None:
            return None
        egress = far.panel.opposite(far.ref.interface)
        if egress is None:
            return None
        following = ctx.panel_terminations.get((far.owner_fqn, egress))
        current = following[0] if following else None
    return None


def _far_end(endpoint: _Endpoint, ctx: _Context) -> _Endpoint | None:
    """The other endpoint of ``endpoint``'s cable, when it resolved."""
    for candidate in ctx.endpoints:
        if candidate.cable_fqn == endpoint.cable_fqn and candidate is not endpoint:
            return candidate
    return None


def _check_dangling_patch(ctx: _Context) -> Iterator[_Draft]:
    """W133 — a cabled position is coupled to one nothing is patched into (``NG-P002``).

    Half a run. The cable exists, the coupler exists, and the far side of the
    panel is empty — so the port at the near end is *not* connected to anything,
    however much the inventory looks like it is. This is the single most common
    real patch-record error: the run was pulled, the front was patched, and the
    rear was left for later.

    A warning rather than an error, because "left for later" is also a
    legitimate state to record: the position is reserved, and the inventory is
    telling the truth about a job that is half done.
    """
    for (panel_fqn, position), endpoints in ctx.panel_terminations.items():
        panel = ctx.panels.get(panel_fqn)
        if panel is None:
            continue
        egress = panel.opposite(position)
        if egress is None or (panel_fqn, egress) in ctx.panel_terminations:
            continue
        cable = endpoints[0].cable_fqn
        yield _Draft(
            f"cable {_q(cable)} lands on {_q(f'{panel_fqn}:{position}')}, which is coupled to "
            f"{_q(egress)}; nothing is patched into that position, so the run stops inside the "
            f"panel and reaches nothing",
            (cable, panel_fqn),
            endpoints[0].field_path,
        )


# --------------------------------------------------------------------------- #
# Physical placement (§10.13)
# --------------------------------------------------------------------------- #


def _by_rack(ctx: _Context) -> Iterator[tuple[tuple[str, str, str], tuple[_Placement, ...]]]:
    """Every rack the inventory places something in, with its occupants."""
    racks: dict[tuple[str, str, str], list[_Placement]] = {}
    for placement in ctx.placements:
        key = placement.rack
        if key is not None:
            racks.setdefault(key, []).append(placement)
    for key, members in racks.items():
        yield key, tuple(members)


def _check_rack_overlap(ctx: _Context) -> Iterator[_Draft]:
    """E025 — two elements occupy the same unit of one rack (``NG-U001``).

    Two things cannot be bolted to the same four screw holes. In practice this
    catches the position that was copied from the row above and never changed,
    and the 2U server whose ``height`` was left at the default — both of which
    produce an elevation that looks plausible and is off by one for everything
    above it.
    """
    for key, members in _by_rack(ctx):
        # Ordered bottom of the rack upwards, and by name within a unit, rather
        # than in the order the documents loaded: a collision is symmetric —
        # neither occupant is the intruder — so reporting it in load order made
        # the wording, the ``elements`` list and the anchored file all move when
        # two documents were packed into one. Bottom-up is also how an elevation
        # is read.
        placed = sorted(
            (member for member in members if member.location.is_placed),
            key=lambda member: (member.location.position or 0, member.fqn),
        )
        for index, first in enumerate(placed):
            for second in placed[index + 1 :]:
                overlap = sorted(set(first.location.units) & set(second.location.units))
                if not overlap:
                    continue
                label = first.location.rack_label or "/".join(key)
                yield _Draft(
                    f"{_q(first.fqn)} and {_q(second.fqn)} both occupy "
                    f"{_units_text(overlap)} of rack {_q(label)}: "
                    f"{_span_text(first)} and {_span_text(second)}",
                    (first.fqn, second.fqn),
                    first.path("position"),
                )


def _check_rack_height(ctx: _Context) -> Iterator[_Draft]:
    """E026 — an element extends past the top of its rack (``NG-U002``).

    ``position`` is the *lowest* unit and ``height`` counts upwards, so a 4U
    panel at U40 of a 42U cabinet ends at U43 and does not fit. The arithmetic
    is exactly the part a person does wrong, which is the whole reason the
    block is structured rather than free text.
    """
    for key, members in _by_rack(ctx):
        declared = [
            member.location.rack_height
            for member in members
            if member.location.rack_height is not None
        ]
        if not declared:
            continue
        height = max(declared)
        for member in members:
            top = member.location.top
            if top is None or top <= height:
                continue
            label = member.location.rack_label or "/".join(key)
            yield _Draft(
                f"{_q(member.fqn)} is mounted at {_span_text(member)} of rack {_q(label)}, "
                f"which is {height}U tall; it would extend {top - height}U past the top",
                (member.fqn,),
                member.path("position"),
            )


def _check_rack_height_agreement(ctx: _Context) -> Iterator[_Draft]:
    """E027 — one rack is declared with two different heights (``NG-U003``).

    A rack has one height. Two elements that disagree about it mean either that
    one of them is in a different cabinet than its ``rack`` says, or that the
    number was guessed — and until that is settled, ``E026`` has no bound it
    can trust.
    """
    for key, members in _by_rack(ctx):
        declared: dict[int, list[str]] = {}
        for member in members:
            if member.location.rack_height is not None:
                declared.setdefault(member.location.rack_height, []).append(member.fqn)
        if len(declared) < 2:
            continue
        label = members[0].location.rack_label or "/".join(key)
        spelled = _join_plain(
            [f"{height}U by {_join(sorted(names))}" for height, names in sorted(declared.items())]
        )
        anchor = next(member for member in members if member.location.rack_height is not None)
        yield _Draft(
            f"rack {_q(label)} is declared {spelled}; a rack has one height",
            tuple(fqn for names in declared.values() for fqn in sorted(names)),
            anchor.path("rack_height"),
        )


def _units_text(units: Sequence[int]) -> str:
    """``U10`` or ``U10-U11``, for a span of rack units."""
    if len(units) == 1:
        return f"U{units[0]}"
    return f"U{units[0]}-U{units[-1]}"


def _span_text(placement: _Placement) -> str:
    """``U10-U11 (2U)`` — where one element sits and how much it takes."""
    location = placement.location
    units = list(location.units)
    return f"{_units_text(units)} ({location.height}U)" if units else "no position"


# --------------------------------------------------------------------------- #
# Power (§17)
def _check_stale_geometry(ctx: _Context) -> Iterator[_Draft]:
    """W138 — a layout document places something the inventory no longer has.

    Only *element addresses* are checked. A derived node — a layer-3 prefix, a
    tunnel drawn as a box, a rack elevation — has an id no document declares,
    and whether one still exists is a question about a particular drawing rather
    than about the inventory; ``netviz layout --prune`` builds the drawing and
    answers it, and removes more than this reports. A namespace is checked,
    because the inventory knows every namespace it has.

    A warning rather than an error, deliberately. Deleting a switch must not
    make ``netviz validate`` fail, and stale coordinates draw nothing: they
    place a node that is not in the diagram.
    """
    namespaces = set(ctx.inventory.namespaces)
    for fqn, layout in ctx.inventory.layouts.items():
        namespace = namespace_of(fqn)
        for view, geometry in sorted(layout.spec.views.items()):
            for section, keys in (
                ("nodes", geometry.nodes),
                ("edges", geometry.edges),
                ("groups", geometry.groups),
            ):
                for key in keys:
                    if _geometry_key_exists(
                        ctx, key, section=section, namespace=namespace, namespaces=namespaces
                    ):
                        continue
                    yield _Draft(
                        f"layout {_q(fqn)} places {_q(key)} in its {view} view, which this "
                        f"inventory does not declare; run 'netviz layout --prune' to drop it",
                        (fqn,),
                        ("spec", "views", view, section, key),
                    )


def _geometry_key_exists(
    ctx: _Context, key: str, *, section: str, namespace: str, namespaces: set[str]
) -> bool:
    """Does ``key`` still name something? ``True`` when it cannot be judged.

    A derived id — ``subnet:10.0.0.0/24`` for a prefix node, ``adp#upstream``
    for an adapter's attachment, ``sw:eth0#10.0.0.0/24`` for a membership — is a
    fact about a *drawing*, and only a drawing can say whether it still exists.
    Both punctuation marks are refused from an element name by ``NG-N001``, so
    finding either is proof the key is not one.
    """
    if section == "groups":
        return _qualified_namespace(key, namespace) in namespaces
    if ":" in key or "#" in key:
        return True
    return ctx.inventory.resolve_fqn(key, namespace=namespace) is not None


def _qualified_namespace(key: str, namespace: str) -> str:
    if not namespace or key.startswith(f"{namespace}/") or key == namespace:
        return key
    return f"{namespace}/{key}"


# --------------------------------------------------------------------------- #
# Diagram annotations (§21)
# --------------------------------------------------------------------------- #


def _check_stale_annotation(ctx: _Context) -> Iterator[_Draft]:
    """W142 — a note or an area is about something the inventory no longer has.

    A **warning**, and emphatically not an error. This is the one rule that has
    to be gentle: deleting a switch must not make ``netviz validate`` fail
    because somebody once wrote a note about it, and a note whose anchor is gone
    simply loses its leader line — it still says what it says. The whole point of
    §21 is that an annotation cannot change what the tool concludes, and a rule
    that could fail a build would be exactly that.
    """
    for kind, fqn, annotation in ctx.inventory.annotations:
        namespace = namespace_of(fqn)
        for reference, path in _annotation_references(annotation):
            if ctx.inventory.resolve_fqn(reference, namespace=namespace) is not None:
                continue
            yield _Draft(
                f"{kind} {_q(fqn)} is about {_q(reference)}, which this inventory does not "
                f"declare; {_ANNOTATION_CONSEQUENCE[kind]}",
                (fqn,),
                path,
            )


#: What actually happens to an annotation whose reference is gone, per kind. The
#: message says it, because the two outcomes differ and neither of them is "the
#: annotation disappears": a note keeps its text and loses only the line pointing
#: at nothing, and an area is still drawn round whichever members remain. Telling
#: somebody their note is gone when it is on the diagram in front of them is a
#: worse diagnostic than none.
_ANNOTATION_CONSEQUENCE: Final[dict[str, str]] = {
    "note": "the note is still drawn, without its leader line",
    "area": "it is left out of the box",
}


def _annotation_references(annotation: Annotation) -> Iterator[tuple[str, tuple[str | int, ...]]]:
    """Every element an annotation names, with the field path that named it.

    A legend names nothing — its entries are colours and words — so it yields
    nothing, which is why this is one function rather than three checks.
    """
    if isinstance(annotation, Note):
        if annotation.spec.anchor is not None:
            anchor = annotation.spec.anchor
            key = "element" if anchor.element is not None else "link"
            yield anchor.reference, ("spec", "anchor", key)
    elif isinstance(annotation, Area):
        for index, member in enumerate(annotation.spec.members):
            yield member, ("spec", "members", index)


def _check_empty_area(ctx: _Context) -> Iterator[_Draft]:
    """W143 — an area's selector matches nothing.

    Only a *selector* is checked, never an explicit member list: a stale member
    is ``W142``'s business and reported per name, which is more useful than
    "this box is empty". An area with an explicit ``geometry`` is never reported
    either — a rectangle drawn on the canvas encloses whatever happens to be
    inside it, and "nothing yet" is a legitimate state for one.
    """
    for fqn, area in ctx.inventory.areas.items():
        selector = area.spec.selector
        if selector is None:
            continue
        if select_area_members(ctx.inventory, selector, namespace=namespace_of(fqn)):
            continue
        yield _Draft(
            f"area {_q(fqn)} selects no element of this inventory; it will not be drawn",
            (fqn,),
            ("spec", "selector"),
        )


# --------------------------------------------------------------------------- #


def _styled(ctx: _Context) -> Iterator[tuple[str, Style]]:
    """Every element carrying a ``spec.style`` block (§22), with its fqn."""
    for fqn, element in ctx.inventory.elements.items():
        style = getattr(element.spec, "style", None)
        if style is not None:
            yield fqn, style


def _check_invisible_style(ctx: _Context) -> Iterator[_Draft]:
    """W144 — a style fades an element to nothing (``NG-Z003``).

    ``opacity: 0`` is legal on its own — it is the bottom of a legal range, and
    an editor dragging a slider passes through it — but an element drawn fully
    transparent is one a reader cannot see and cannot click, while every link to
    it is still drawn to the empty space where it was. That is a diagram that
    lies, and it is almost always a slider left at the wrong end rather than a
    decision. Hiding an element is what ``--kind``, ``--name`` and the other
    filters are for; they take it out of the topology as well as out of the
    picture.
    """
    for fqn, style in _styled(ctx):
        if style.opacity is not None and style.opacity <= 0:
            yield _Draft(
                f"{_q(fqn)} is styled with opacity 0, so it is drawn invisibly while its "
                f"links are still drawn to it. Use a filter to leave an element out.",
                (fqn,),
                ("spec", "style", "opacity"),
            )


def _check_unreadable_label(ctx: _Context) -> Iterator[_Draft]:
    """W145 — a label the same colour as the box behind it (``NG-Z005``).

    Reported only when *both* colours are written on the same element, so this
    never fires on an inherited value: a theme setting one and an element the
    other is a legitimate combination that the reader can see the result of, and
    a warning about a pair the element does not fully control would be a warning
    nobody can act on without editing somebody else's file.

    A transparent fill is exempt. ``fill: none`` means "whatever is behind
    this", and black text on it is the normal way to draw an unfilled shape.
    """
    for fqn, style in _styled(ctx):
        fill, font = style.fill, style.font_color
        if fill is None or font is None:
            continue
        if hex_colour(fill) != hex_colour(font) or hex_colour(fill) == "none":
            continue
        yield _Draft(
            f"{_q(fqn)} draws its label in {font!r} on a {fill!r} fill, so the label is "
            f"invisible. Give 'fontColor' a colour that contrasts with 'fill'.",
            (fqn,),
            ("spec", "style", "fontColor"),
        )


# --------------------------------------------------------------------------- #


def _check_outlet_claimed_twice(ctx: _Context) -> Iterator[_Draft]:
    """E037 — one PDU outlet feeds two power supplies (``NG-E010``).

    An outlet takes one plug. Two cords in one is a load schedule that cannot be
    true, and unlike the patch-panel version of the same mistake (``E022``) it is
    usually not a typo about the outlet: it is a *second* device someone added to
    a rack without a spare socket, which is exactly the case a schedule exists to
    prevent.

    Two inputs of the *same* device naming one outlet is caught earlier, by the
    model (``NG-E002``), so everything reported here involves two elements.
    """
    for (pdu, outlet), feeds in ctx.power.outlet_claims().items():
        elements = list(dict.fromkeys(feed.element for feed in feeds))
        if len(elements) < 2:
            continue
        anchor = feeds[0]
        yield _Draft(
            f"outlet {_q(f'{pdu}:{outlet}')} is claimed by {len(elements)} elements: "
            f"{_join(elements)}. An outlet takes one plug.",
            (*elements, pdu),
            ("spec", "power", "inputs", anchor.index),
        )


def _check_power_input_resolves(ctx: _Context) -> Iterator[_Draft]:
    """E038 — a power input names no outlet that exists (``NG-E011``).

    Four ways to get there, reported apart because the fix differs: the PDU is
    not in the inventory, the name is ambiguous, the name resolves to something
    that is not a PDU, or the PDU is right and the outlet number is not. The last
    one names the range the PDU declares, for the same reason ``E021`` does: the
    outlets come from a range rather than being written out, so ``25`` on a
    24-outlet strip is only visible next to that range.
    """
    for entry in ctx.power.unresolved:
        reference = _q(str(entry.input))
        if entry.reason is UnresolvedReason.UNKNOWN_PDU:
            message = (
                f"element {_q(entry.element)} is fed from {reference}, but no element named "
                f"{_q(entry.input.pdu)} exists"
            )
            elements: tuple[str, ...] = (entry.element,)
        elif entry.reason is UnresolvedReason.AMBIGUOUS_PDU:
            message = (
                f"element {_q(entry.element)} is fed from {reference}, but "
                f"{_q(entry.input.pdu)} is ambiguous: {_join(sorted(entry.candidates))}. "
                f"Write the reference fully qualified."
            )
            elements = (entry.element, *entry.candidates)
        elif entry.reason is UnresolvedReason.NOT_A_PDU:
            other = ctx.inventory.elements.get(entry.pdu)
            kind = other.kind if other is not None else "unknown"
            message = (
                f"element {_q(entry.element)} is fed from {reference}, but {_q(entry.pdu)} is a "
                f"{kind}, not a pdu; an outlet exists only on a 'pdu' document"
            )
            elements = (entry.element, entry.pdu)
        else:
            pdu = ctx.inventory.pdus.get(entry.pdu)
            declared = pdu.spec.outlets if pdu is not None else "none"
            message = (
                f"element {_q(entry.element)} is fed from {reference}, but pdu {_q(entry.pdu)} "
                f"has outlets {declared}"
            )
            elements = (entry.element, entry.pdu)
        yield _Draft(message, elements, entry.field_path)


def _check_pdu_capacity(ctx: _Context) -> Iterator[_Draft]:
    """E039 — the declared load on a PDU exceeds its capacity (``NG-E012``).

    Summed over the *normal-operation* share of each load: a dual-corded server
    draws half its watts through each cord, so a pair of PDUs each sized for half
    the rack is a correct design and is not reported. What is reported is a strip
    with more plugged into it than it is rated for, which no amount of failover
    planning makes acceptable.

    Silent when the PDU records no ``capacity_watts``: there is nothing to
    compare against, and inventing a rating for a strip nobody measured would
    turn a missing fact into a false error.
    """
    for load in ctx.power.pdus:
        if not load.is_oversubscribed or load.capacity_watts is None:
            continue
        over = load.load_watts - load.capacity_watts
        yield _Draft(
            f"pdu {_q(load.pdu)} carries {format_watts(load.load_watts)} W across "
            f"{count_text(len(load.elements), 'element')} but is rated for "
            f"{format_watts(load.capacity_watts)} W: {format_watts(over)} W over. "
            f"The loads are {_join(load.elements)}.",
            (load.pdu, *load.elements),
            ("spec", "capacity_watts"),
        )


def _check_poe_budget(ctx: _Context) -> Iterator[_Draft]:
    """E040 — the PoE allocated on a device's ports exceeds its budget (``NG-E013``).

    Only ports that hold budget are counted — one that feeds something, and one
    whose ``budget_watts`` was written down. A ``poe`` block on an empty port is a
    capability, and counting all forty-eight of them would report every real PoE
    switch as oversubscribed (see
    :attr:`~netviz.power.PoePort.counted`), which would make the rule useless
    exactly where it matters.
    """
    for budget in ctx.power.pse:
        if not budget.is_oversubscribed or budget.budget_watts is None:
            continue
        ports = budget.counted_ports
        spelled = _join_plain(
            [
                f"{port.interface} {format_watts(port.allocated_watts)} W"
                for port in sorted(ports, key=lambda port: port.interface)
            ]
        )
        over = budget.allocated_watts - budget.budget_watts
        yield _Draft(
            f"element {_q(budget.element)} allocates {format_watts(budget.allocated_watts)} W of "
            f"PoE across {count_text(len(ports), 'port')} but its budget is "
            f"{format_watts(budget.budget_watts)} W: {format_watts(over)} W over ({spelled})",
            (budget.element, *(port.feeds for port in ports if port.feeds)),
            ("spec", "power", "poe_budget_watts"),
        )


def _check_poe_uplink(ctx: _Context) -> Iterator[_Draft]:
    """E041 — a PoE-powered device's uplink offers no PoE, or too little (``NG-E014``).

    ``powered_by: poe`` says the device has no power cord, so the run carrying its
    traffic is its *only* power path. Three ways for that to be wrong, and all
    three are a device that will not come up:

    * no run leaves the device at all, or none arrives anywhere;
    * every run lands on a port with no ``poe`` block, or a disabled one;
    * a run does source power, but less than the device says it draws — a class-2
      port and a 20 W access point, which is the mistake this rule exists for.
    """
    for fqn, uplinks in ctx.power.uplinks.items():
        node = ctx.power.node(fqn)
        draw = node.draw_watts if node is not None else 0.0
        sourcing = [uplink for uplink in uplinks if uplink.sources_power]
        if not sourcing:
            yield _Draft(
                f"element {_q(fqn)} is powered over PoE but {_describe_uplinks(uplinks)}",
                (fqn, *(uplink.peer for uplink in uplinks)),
                ("spec", "power", "powered_by"),
            )
            continue
        best = max(sourcing, key=lambda uplink: uplink.deliverable_watts)
        if draw and draw > best.deliverable_watts:
            yield _Draft(
                f"element {_q(fqn)} draws {format_watts(draw)} W over PoE, but the best uplink "
                f"it has delivers {format_watts(best.deliverable_watts)} W: {best.describe()}",
                (fqn, best.peer),
                ("spec", "power", "draw_watts"),
            )


def _describe_uplinks(uplinks: Sequence[Uplink]) -> str:
    """The tail of ``E041`` when nothing on the far end sources power."""
    if not uplinks:
        return (
            "no cable leaves it, so there is no run to take power over; cable it to a PSE "
            "port, or drop 'powered_by: poe'"
        )
    disabled = [uplink for uplink in uplinks if uplink.poe is not None]
    if disabled:
        spelled = _join_plain([uplink.describe() for uplink in disabled])
        return f"every port it reaches has PoE turned off: {spelled}"
    spelled = _join_plain([uplink.describe() for uplink in uplinks])
    return f"nothing it is cabled to sources power: {spelled}"


def _check_power_redundancy(ctx: _Context) -> Iterator[_Draft]:
    """E042 — redundant power that is not redundant (``NG-E015``).

    ``redundant: true`` is a claim that losing one feed does not lose the device.
    Two cords into one PDU do not make that true — the strip, its breaker and its
    cord are the single point of failure — and neither do two PDUs fed from one
    supply, which is the subtler and more common version: the racking is right and
    the electrical plan is not.

    A PDU that records no ``input_feed`` is not evidence either way, so two
    different PDUs with no feed recorded are accepted. Silence is not a claim.
    """
    for fqn, element in ctx.inventory.devices.items():
        power = element.spec.power
        if power is None or not power.redundant:
            continue
        feeds = [feed for feed in ctx.power.feeds_into(fqn) if feed.kind is FeedKind.OUTLET]
        if len(feeds) < 2:
            # Fewer than two *resolved* feeds: the model already refused fewer
            # than two declared ones (``NG-E002``), so what is left is a feed
            # that did not resolve -- reported by ``E038``, and reporting it
            # twice would blame the redundancy claim for someone else's typo.
            continue
        sources = {feed.source for feed in feeds}
        if len(sources) == 1:
            pdu = next(iter(sources))
            yield _Draft(
                f"element {_q(fqn)} claims redundant power but all "
                f"{count_text(len(feeds), 'input')} come from pdu {_q(pdu)}: "
                f"{_join_plain([feed.source_label for feed in feeds])}. Losing that unit loses "
                f"the device.",
                (fqn, pdu),
                ("spec", "power", "redundant"),
            )
            continue
        supplies = {feed.input_feed for feed in feeds}
        if len(supplies) == 1 and (supply := next(iter(supplies))):
            yield _Draft(
                f"element {_q(fqn)} claims redundant power, but every pdu feeding it is on "
                f"input feed {_q(supply)}: {_join_plain([feed.source_label for feed in feeds])}. "
                f"Two units on one supply fail together.",
                (fqn, *sorted(sources)),
                ("spec", "power", "redundant"),
            )


def _check_missing_power_path(ctx: _Context) -> Iterator[_Draft]:
    """W137 — a device declares a draw but no power path (``NG-E016``).

    A warning rather than an error, deliberately. Recording draws before
    recording the outlets they are plugged into is the normal order in which an
    as-built document gets written, and refusing the half-finished state would
    make the model unusable exactly while it is being adopted. It is still worth
    saying: a load that appears on no PDU appears on no schedule either, so the
    rack looks emptier than it is.
    """
    for fqn, element in ctx.inventory.devices.items():
        power = element.spec.power
        if power is None or power.draw_watts is None or power.is_poe_powered:
            continue
        if power.inputs:
            # Declared but unresolved is ``E038``'s business; a device whose only
            # problem is a typo must not also be accused of having no feed.
            continue
        yield _Draft(
            f"element {_q(fqn)} declares a draw of {power.draw_watts.describe()} but no power "
            f"path: add 'power.inputs' naming the outlets it is plugged into, or "
            f"'powered_by: poe' if it takes power over its uplink",
            (fqn,),
            ("spec", "power", "draw_watts"),
        )


# --------------------------------------------------------------------------- #
# Declared survivability (:mod:`netviz.expectations`)
# --------------------------------------------------------------------------- #


def _check_gateway_redundancy(ctx: _Context) -> Iterator[_Draft]:
    """E047 — a declared ``gateway`` expectation the topology does not meet.

    ``netviz/redundancy: gateway`` is a promise that no *one* failure can cut
    this element off from its default gateway. That is exactly the statement
    "the two are two-connected", so the check is a search for the cut vertices
    and bridges between them (:func:`netviz.connectivity.separators`) rather
    than a simulation — an exact answer in one pass instead of an approximate
    one in a thousand.

    Three ways to fail it, reported apart because the fix differs: nothing to
    check (no gateway is declared, or it is configured on nothing), nothing to
    lose (there is no path even now), and the ordinary one — a path exists and
    one element or one cable carries all of it.

    Nothing here runs unless an element declares the expectation. The whole
    check is a graph build and one depth-first search, and an inventory that
    made no promise should not pay for the machinery that grades them.
    """
    wanted = [
        declaration
        for declaration in declarations(ctx.inventory)
        if declaration.wants(Expectation.GATEWAY) and declaration.element in ctx.owners
    ]
    if not wanted:
        return

    graph = _topology_graph(ctx)
    # ``ctx.address_owners`` is built only when something declares a BGP peer —
    # it exists for ``W135``, and building it for every inventory would be a
    # scan nothing else needed. Here it is needed, so it is built here.
    owners_by_address = ctx.address_owners or _index_addresses(ctx.owners)
    for declaration in wanted:
        fqn = declaration.element
        owner = ctx.owners[fqn]
        addresses = [address for _, address in _declared_gateways(owner)]
        if not addresses:
            yield _Draft(
                f"element {_q(fqn)} declares a 'gateway' redundancy expectation, but none of "
                f"its interfaces declares a 'gateway': there is nothing to stay connected to. "
                f"Add one, or drop the expectation.",
                (fqn,),
                declaration.field_path,
            )
            continue
        for address in addresses:
            if address.is_link_local:
                # ``fe80::1`` is on-link by definition and is almost never
                # written down as an address of the router that answers for it,
                # exactly as ``E020`` exempts it. Grading it would report every
                # correctly-configured IPv6 host.
                continue
            placement = owners_by_address.get(address)
            if placement is None:
                yield _Draft(
                    f"element {_q(fqn)} declares a 'gateway' redundancy expectation, but its "
                    f"gateway {address} is configured on nothing in this inventory, so no "
                    f"survivability claim about it can be checked",
                    (fqn,),
                    declaration.field_path,
                )
                continue
            gateway = placement[0]
            if gateway == fqn:
                continue  # it is its own first hop; there is no route to lose
            yield from _gateway_drafts(graph, declaration, fqn, gateway, address)


def _gateway_drafts(
    graph: ConnectivityGraph,
    declaration: Declaration,
    fqn: str,
    gateway: str,
    address: object,
) -> Iterator[_Draft]:
    """The E047 drafts for one ``(element, gateway)`` pair."""
    if gateway not in reachable(graph, (fqn,)):
        yield _Draft(
            f"element {_q(fqn)} declares a 'gateway' redundancy expectation but cannot reach "
            f"its gateway {address} on {_q(gateway)} at all: the topology joins them by no "
            f"path, redundant or otherwise",
            (fqn, gateway),
            declaration.field_path,
        )
        return
    found = separators(graph, fqn, gateway)
    if not found:
        return
    described = _join_plain([_separator_label(graph, separator) for separator in found])
    yield _Draft(
        f"element {_q(fqn)} declares a 'gateway' redundancy expectation, but "
        f"{count_text(len(found), 'single failure')} would cut it off from its gateway "
        f"{address} on {_q(gateway)}: {described}. Add a second path, or drop the "
        f"expectation.",
        (fqn, gateway, *(separator.id for separator in found if separator.is_node)),
        declaration.field_path,
    )


def _separator_label(graph: ConnectivityGraph, separator: Separator) -> str:
    """Name one separator the way a person would: the box, or the cable."""
    if separator.is_node:
        panel, _, position = separator.id.partition("#")
        return f"panel {panel} at {position}" if position else separator.id
    return separator.id


def _declared_gateways(owner: InterfaceOwner) -> Iterator[tuple[int, _IPAddress]]:
    """Every first hop the element configures, in interface then family order."""
    for interface in owner.interfaces:
        yield from interface.gateways()


def _topology_graph(ctx: _Context) -> ConnectivityGraph:
    """The physical plant as a searchable graph, panels spliced through.

    The same edges ``W121`` counts islands over, so the two rules cannot
    disagree about what is joined to what — including the detail that a run
    through a patch panel is one path and two runs through *different* positions
    of one panel are two (§15.2).
    """
    links = list(_topology_edges(ctx))
    nodes = list(
        dict.fromkeys([*ctx.owners, *(node for _, left, right in links for node in (left, right))])
    )
    return ConnectivityGraph.of(nodes, links, endpoints=ctx.owners)


def _topology_edges(ctx: _Context) -> Iterator[tuple[str, str, str]]:
    """:func:`_topology_links`, with each link carrying the name of what it is.

    The pairs alone are enough to count islands, which is all ``W121`` needs. A
    finding that has to *name* the cable somebody would have to lay a second one
    beside needs the identity too, and the cable's fully-qualified name is the
    identity every other part of the tool uses for it.
    """
    for cable_fqn, first, second in ctx.endpoint_pairs:
        left, right = _coupler_node(first), _coupler_node(second)
        if left is not None and right is not None:
            yield f"cable {cable_fqn}", left, right
    for attachment in ctx.attachments:
        if attachment.host_fqn is not None:
            yield (
                f"attachment {attachment.adapter_fqn}",
                attachment.adapter_fqn,
                attachment.host_fqn,
            )


def _check_declared_power_redundancy(ctx: _Context) -> Iterator[_Draft]:
    """E048 — a declared ``power`` expectation the feeds do not meet.

    Distinct from ``E042``, which grades a device's own ``power.redundant``
    claim about its two cords. This grades the stronger statement an operator
    writes on the elements that matter: *nothing* whose failure takes the power
    away. That covers what ``E042`` cannot see — a device with one cord, a
    device fed over PoE by a switch that is itself on one PDU, two PDUs on one
    building supply — because it walks the whole feed chain rather than the
    inputs of one document.

    A device that ``E042`` already reports is left alone: two findings for one
    mistake teach people to read neither.
    """
    wanted = [
        declaration
        for declaration in declarations(ctx.inventory)
        if declaration.wants(Expectation.POWER) and declaration.element in ctx.inventory.elements
    ]
    if not wanted:
        return

    sources = _feed_sources(ctx.power)
    dark_after = {
        source: frozenset(_unpowered(ctx.power, {source})) | {source}
        for source in dict.fromkeys(feed.source for feed in ctx.power.feeds)
    }
    for declaration in wanted:
        fqn = declaration.element
        feeding = sources.get(fqn, ())
        if not feeding:
            yield _Draft(
                f"element {_q(fqn)} declares a 'power' redundancy expectation but has no "
                f"resolved power feed: add 'power.inputs' naming the outlets it is plugged "
                f"into, or 'powered_by: poe'",
                (fqn,),
                declaration.field_path,
            )
            continue
        if _reported_by_e042(ctx, fqn):
            continue
        singles = [source for source, dark in dark_after.items() if fqn in dark and source != fqn]
        if not singles:
            continue
        yield _Draft(
            f"element {_q(fqn)} declares a 'power' redundancy expectation, but losing any one "
            f"of {_join(sorted(singles))} switches it off; it is fed through "
            f"{_join(sorted(feeding))}. Add a feed from an independent source, or drop the "
            f"expectation.",
            (fqn, *sorted(singles)),
            declaration.field_path,
        )


def _reported_by_e042(ctx: _Context, fqn: str) -> bool:
    """Is this device already ``E042``'s finding — two cords into one PDU?"""
    element = ctx.inventory.devices.get(fqn)
    power = element.spec.power if element is not None else None
    if power is None or not power.redundant:
        return False
    feeds = [feed for feed in ctx.power.feeds_into(fqn) if feed.kind is FeedKind.OUTLET]
    return len(feeds) >= 2


def _feed_sources(plan: PowerPlan) -> dict[str, tuple[str, ...]]:
    """``element -> the distinct sources feeding it``, in resolution order."""
    sources: dict[str, list[str]] = {}
    for feed in plan.feeds:
        found = sources.setdefault(feed.element, [])
        if feed.source not in found:
            found.append(feed.source)
    return {element: tuple(found) for element, found in sources.items()}


def _unpowered(plan: PowerPlan, failed: set[str]) -> set[str]:
    """Everything that goes dark when ``failed`` does, transitively.

    The same walk :func:`netviz.impact.graphs.unpowered` makes, kept here as
    six lines over the plan rather than as an import: the validator must not
    depend on the impact engine, which depends on the validator.
    """
    sources = _feed_sources(plan)
    dark = set(failed)
    while True:
        added = {
            element
            for element, feeding in sources.items()
            if element not in dark and all(source in dark for source in feeding)
        }
        if not added:
            return dark - failed
        dark |= added


def _check_unknown_expectation(ctx: _Context) -> Iterator[_Draft]:
    """W141 — a redundancy expectation nothing understands, or one out of place.

    A warning rather than an error on purpose. An annotation is where a newer
    netviz will put things this build has never heard of, and refusing to load
    an inventory because of a word in a comment-shaped field would make the
    annotation useless for exactly the forward compatibility it exists for. It
    is still worth saying loudly: an expectation nothing grades is a promise
    nobody is keeping, and it reads in review as though somebody checked.
    """
    accepted = _join_plain(list(expectation_names()))
    for declaration in declarations(ctx.inventory):
        for token in declaration.unknown:
            yield _Draft(
                f"element {_q(declaration.element)} declares the redundancy expectation "
                f"{_q(token)}, which this build does not understand; it grades {accepted}",
                (declaration.element,),
                declaration.field_path,
            )
        if not declaration.expectations:
            continue
        element = ctx.inventory.elements.get(declaration.element)
        if element is not None and declaration.element not in ctx.owners:
            yield _Draft(
                f"element {_q(declaration.element)} is a {element.kind}, which owns no "
                f"interfaces and takes no power: a redundancy expectation on it grades "
                f"nothing. Put it on the device the expectation is about.",
                (declaration.element,),
                declaration.field_path,
            )


# --------------------------------------------------------------------------- #
# Identity (§19)
# --------------------------------------------------------------------------- #


def _check_member_resolves(ctx: _Context) -> Iterator[_Draft]:
    """E043 — a group names a member that does not exist (``NG-S010``).

    The same two failures every reference in this schema can have, reported
    apart because the fix differs: nothing of that name, or several things of
    that name and no way to tell which was meant. An access rule written against
    a group is only as true as the group's membership, so a name that resolves to
    nothing is not a cosmetic problem — it is a person the list silently does not
    include.
    """
    for entry in ctx.identities:
        if entry.resolved:
            continue
        if entry.ambiguous:
            yield _Draft(
                f"group {_q(entry.group)} names member {_q(entry.ref)}, which is "
                f"ambiguous: {_join(sorted(entry.ambiguous))}. Write the reference fully "
                f"qualified.",
                (entry.group, *entry.ambiguous),
                entry.field_path,
            )
        else:
            yield _Draft(
                f"group {_q(entry.group)} names member {_q(entry.ref)}, but no element "
                f"of that name exists",
                (entry.group,),
                entry.field_path,
            )


def _check_member_is_identity(ctx: _Context) -> Iterator[_Draft]:
    """E044 — a group member is not a user or a group (``NG-S011``).

    A group holds identities. A switch is not one, and a membership naming one is
    almost always a name collision rather than a statement about the switch —
    which is exactly why it is worth saying out loud instead of resolving to the
    device and drawing an edge nobody meant.
    """
    for entry in ctx.identities:
        if not entry.resolved or entry.is_identity:
            continue
        # The member resolved, so both are real elements worth naming.
        assert entry.member is not None
        yield _Draft(
            f"group {_q(entry.group)} names member {_q(entry.ref)}, but {_q(entry.member)} "
            f"is a {entry.kind}; a group holds 'user' and 'group' elements",
            (entry.group, entry.member),
            entry.field_path,
        )


def _check_membership_cycle(ctx: _Context) -> Iterator[_Draft]:
    """E045 — group membership forms a cycle (``NG-S012``).

    Nesting is the point of groups, so the loop it makes possible has to be
    refused: ``everyone`` inside ``engineering`` inside ``everyone`` has no
    membership list at all, because expanding it never terminates. A group naming
    *itself* is refused earlier, by the model (``NG-S003``), which can see it
    without an inventory and can therefore point at the line.
    """
    for cycle in ctx.identities.cycles():
        chain = " -> ".join(_q(fqn) for fqn in (*cycle, cycle[0]))
        yield _Draft(
            f"group membership is cyclic: {chain}. "
            f"{count_text(len(cycle), 'group')} would each have to contain the next, so "
            f"none of them has a membership that can be listed.",
            tuple(cycle),
            ("spec", "members"),
        )


def _check_account_identifiers(ctx: _Context) -> Iterator[_Draft]:
    """E046 — two identities claim one login, uid or gid (``NG-S013``).

    All three are *the* key of an account in the system that consumes them: two
    users with one login are one account with two owners, and two POSIX ids that
    collide make a file owned by whichever document happened to be applied last.
    Reported together because they are one mistake with three spellings, and
    anchored at the first claimant so the finding lands where the id was assigned.

    Namespaces do not make a difference here, deliberately. Two switches called
    ``sw-1`` in two sites are two switches; two accounts called ``alice`` in two
    directories are the same person's login twice, and the whole reason to record
    a login rather than rely on ``metadata.name`` is that it is estate-wide.
    """
    logins: dict[str, list[str]] = {}
    uids: dict[int, list[str]] = {}
    for fqn, user in ctx.inventory.users.items():
        logins.setdefault(user.login, []).append(fqn)
        if user.spec.uid is not None:
            uids.setdefault(user.spec.uid, []).append(fqn)

    gids: dict[int, list[str]] = {}
    for fqn, group in ctx.inventory.groups.items():
        if group.gid is not None:
            gids.setdefault(group.gid, []).append(fqn)

    for login, holders in logins.items():
        if len(holders) > 1:
            yield _Draft(
                f"login {_q(login)} is claimed by {count_text(len(holders), 'user')}: "
                f"{_join(holders)}. One login is one account.",
                tuple(holders),
                ("spec", "login"),
            )
    for uid, holders in uids.items():
        if len(holders) > 1:
            yield _Draft(
                f"uid {uid} is claimed by {count_text(len(holders), 'user')}: {_join(holders)}. "
                f"Two users sharing a uid are one user to the filesystem.",
                tuple(holders),
                ("spec", "uid"),
            )
    for gid, holders in gids.items():
        if len(holders) > 1:
            yield _Draft(
                f"gid {gid} is claimed by {count_text(len(holders), 'group')}: {_join(holders)}. "
                f"Two groups sharing a gid are one group to the filesystem.",
                tuple(holders),
                ("spec", "gid"),
            )


def _check_empty_group(ctx: _Context) -> Iterator[_Draft]:
    """W139 — a group has no members (``NG-S014``).

    A warning, not an error: a group created before the people who will be in it
    is a normal intermediate state, and so is one that has been emptied on purpose
    and kept so its name stays reserved. It is still worth saying, because an
    access rule written against an empty group grants nothing and *looks* like it
    grants something — which is the failure that gets noticed on the day somebody
    needed the access.
    """
    for fqn, group in ctx.inventory.groups.items():
        if group.is_empty:
            yield _Draft(
                f"group {_q(fqn)} has no members: anything granted to it is granted to nobody",
                (fqn,),
                ("spec", "members"),
            )


def _check_departed_member(ctx: _Context) -> Iterator[_Draft]:
    """W140 — a group still lists a user who has left (``NG-S015``).

    This is the rule the ``status`` field exists for. Deleting the ``user``
    document would make the person disappear from the inventory *and* from every
    group naming them, which is exactly the wrong outcome: the memberships are
    what somebody has to go and revoke, and they cannot revoke what the inventory
    no longer records. Marking the account ``departed`` keeps the list of things
    to undo visible until they have been undone.

    Silent for a ``shared`` or ``service`` account, which has no person to depart.
    """
    for entry in ctx.identities:
        member = ctx.inventory.users.get(entry.member or "")
        if member is None or not member.has_departed or not member.is_person:
            continue
        assert entry.member is not None
        yield _Draft(
            f"group {_q(entry.group)} still lists {_q(entry.member)}, whose account is "
            f"'departed': the membership is access that has not been revoked",
            (entry.group, entry.member),
            entry.field_path,
        )


def _check_ungrouped_user(ctx: _Context) -> Iterator[_Draft]:
    """I004 — a person's account is a member of no group (``NG-S016``).

    Info, because it is a fact rather than a fault: plenty of estates grant a
    person access directly and never put them in anything. It is reported at all
    because the opposite reading — "this person is in no group, so they have no
    access" — is the one an auditor wants confirmed, and because an account that
    was meant to be in a group and is not looks exactly like this.

    Only ``person`` accounts (§19.1). A service account outside every group is the
    normal shape of a service account, and saying so on each one would drown the
    people this rule is about.
    """
    for fqn, user in ctx.inventory.users.items():
        if user.spec.type is not UserType.PERSON or ctx.identities.groups_of(fqn):
            continue
        if user.spec.status is not UserStatus.ACTIVE:
            # A suspended or departed account belonging to nothing is the state
            # ``W140`` asks for, not a gap.
            continue
        yield _Draft(
            f"user {_q(fqn)} is a member of no group",
            (fqn,),
            ("metadata", "name"),
        )


# --------------------------------------------------------------------------- #
# Network namespaces and veth pairs (§23)
# --------------------------------------------------------------------------- #


def _check_cable_on_veth(ctx: _Context) -> Iterator[_Draft]:
    """E049 — a cable terminates on one end of a veth pair (``NG-N024``).

    A veth end is ``ethernet`` by type, so ``E012`` waves it through: the type
    check cannot tell it apart from the port on the back of the machine, and
    that is deliberate — everything a bridge or a VLAN sub-interface can be
    stacked on a physical port, it can be stacked on a veth end. What a veth end
    does not have is a socket. Its far side is already claimed, by the peer it
    names, and a cable drawn to it describes a plug with nowhere to go while the
    physical port somebody meant is left looking free.
    """
    for endpoint in ctx.endpoints:
        interface = endpoint.interface
        if interface is None or not interface.is_veth:
            continue
        physical = _join(
            sorted(
                candidate.name
                for candidate in (endpoint.owner.interfaces if endpoint.owner else ())
                if candidate.type.is_cableable and not candidate.is_veth
            )
        )
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} terminates on {_q(endpoint.port)}, which is one "
            f"end of the veth pair {_q(interface.name)}/{_q(interface.peer or '')}; a veth end "
            f"has no socket, its far side is the peer it names"
            + (f" — {_q(endpoint.owner_fqn or '')} declares {physical}" if physical else ""),
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_aggregate_spans_netns(ctx: _Context) -> Iterator[_Draft]:
    """E050 — a bridge or lag aggregates a member in another namespace (``NG-N025``).

    A bridge forwards frames between its ports and a bond schedules them across
    its slaves; both are one datapath, and a datapath belongs to exactly one
    network stack. Moving a port into a namespace is precisely the operation
    that takes it *out* of that stack, so the kernel removes it from the
    aggregate as it goes — the two lines cannot both be true afterwards.

    This is the one place §23 constrains §6.2's stacking, and only there: a
    ``vlan`` sub-interface is *not* checked, because moving one into another
    namespace is a supported thing to do and the sub-interface keeps receiving
    the frames its parent tags.
    """
    for fqn, owner in ctx.owners.items():
        by_name = ctx.by_name[fqn]
        for interface in owner.interfaces:
            if interface.type not in AGGREGATE_TYPES:
                continue
            for name in interface.members or ():
                member = by_name.get(name)
                if member is None or member.netns_name == interface.netns_name:
                    continue
                yield _Draft(
                    f"{_q(f'{fqn}:{interface.name}')} is a {interface.type.value} in "
                    f"{_netns_text(interface.netns_name)} and aggregates "
                    f"{_q(name)}, which is in {_netns_text(member.netns_name)}; one datapath "
                    f"belongs to one network stack",
                    (fqn,),
                    _index_path(owner, name, "netns"),
                )


def _check_empty_netns(ctx: _Context) -> Iterator[_Draft]:
    """W146 — a declared namespace holds no interface (``NG-N026``).

    The same shape as ``W136`` and for the same reason: a namespace is a stack,
    and a stack with no interface in it has no address, no route and no way in
    or out. It is not an error — ``ip netns add`` makes exactly that, and a
    document may be describing a sandbox before anything is moved into it — but
    it is far more often a namespace whose interfaces were renamed out from
    under it, and the isolation somebody declared does not exist.
    """
    for fqn, owner in ctx.owners.items():
        if not isinstance(owner, Device):
            continue
        occupied = {interface.netns_name for interface in owner.spec.interfaces}
        for index, entry in enumerate(owner.spec.netns):
            if entry.name in occupied:
                continue
            # A namespace that holds only *other* namespaces is still empty —
            # nothing in it has an address — but saying what it does hold is the
            # difference between "delete this" and "the interfaces went into the
            # one inside it".
            inner = sorted(e.name for e in owner.spec.netns if e.parent == entry.name)
            nested = (
                f"; it holds {count_text(len(inner), 'namespace')} ({_join(inner)}) "
                f"and nothing else"
                if inner
                else ""
            )
            yield _Draft(
                f"element {_q(fqn)} declares network namespace {_q(entry.name)}, but no "
                f"interface is in it, so it has no address and no way in or out{nested}",
                (fqn,),
                ("spec", "netns", index, "name"),
            )


def _check_veth_inside_one_netns(ctx: _Context) -> Iterator[_Draft]:
    """I005 — both ends of a veth pair are in one namespace (``NG-N027``).

    Legal, and occasionally meant: a veth pair is the standard way to join two
    bridges inside one stack. Reported anyway, as information, because the far
    more common reading is that ``netns`` was written on one end and forgotten
    on the other — and that mistake produces a document that validates, draws a
    link, and describes a namespace nothing reaches.
    """
    for fqn, owner in ctx.owners.items():
        if not isinstance(owner, Device):
            continue
        for first, second in owner.spec.veth_pairs():
            if first.netns_name != second.netns_name:
                continue
            yield _Draft(
                f"veth pair {_q(f'{fqn}:{first.name}')}/{_q(second.name)} has both ends in "
                f"{_netns_text(first.netns_name)}, so it crosses no namespace boundary",
                (fqn,),
                _index_path(owner, first.name, "peer"),
            )


def _netns_text(namespace: str) -> str:
    """``the initial namespace`` / ``namespace 'blue'`` — the phrase §23 findings use."""
    return f"namespace {_q(namespace)}" if namespace else "the initial namespace"


#: Every check, paired with the rule it reports, in report order.
_CHECKS: Final[tuple[tuple[str, Check], ...]] = (
    ("E001", _check_endpoint_references),
    ("E002", _check_double_termination),
    ("E003", _check_duplicate_mac),
    ("E004", _check_duplicate_ip),
    ("E005", _check_vlan_mismatch),
    ("E006", _check_adapter_capacity),
    ("E007", _check_stacking_cycle),
    ("E008", _check_member_is_aggregated),
    ("E009", _check_sub_interface_vlan),
    ("E010", _check_multicast_mac),
    ("E011", _check_wireless_medium),
    ("E012", _check_uncableable_endpoint),
    ("E013", _check_attachment_and_cable),
    ("E014", _check_attachment_cycle),
    ("E015", _check_attachment_target),
    ("E016", _check_tunnel_endpoints),
    ("E017", _check_tunnel_endpoint_type),
    ("E018", _check_encapsulation_target),
    ("E019", _check_encapsulation_cycle),
    ("E020", _check_gateway_on_link),
    ("E021", _check_panel_position),
    ("E022", _check_panel_double_termination),
    ("E023", _check_panel_as_active_element),
    ("E024", _check_panel_loop),
    ("E025", _check_rack_overlap),
    ("E026", _check_rack_height),
    ("E027", _check_rack_height_agreement),
    ("E028", _check_wireless_association),
    ("E029", _check_duplicate_bssid),
    ("E030", _check_bss_vlan_carried),
    ("E031", _check_associated_ssid),
    ("E032", _check_route_next_hop),
    ("E033", _check_route_device),
    ("E034", _check_ospf_interfaces),
    ("E035", _check_bgp_asn),
    ("E036", _check_router_ids),
    ("E037", _check_outlet_claimed_twice),
    ("E038", _check_power_input_resolves),
    ("E039", _check_pdu_capacity),
    ("E040", _check_poe_budget),
    ("E041", _check_poe_uplink),
    ("E042", _check_power_redundancy),
    ("E043", _check_member_resolves),
    ("E044", _check_member_is_identity),
    ("E045", _check_membership_cycle),
    ("E046", _check_account_identifiers),
    ("E047", _check_gateway_redundancy),
    ("E048", _check_declared_power_redundancy),
    ("E049", _check_cable_on_veth),
    ("E050", _check_aggregate_spans_netns),
    ("W101", _check_unaddressed_interface),
    ("W102", _check_mtu_mismatch),
    ("W103", _check_orphan_device),
    ("W104", _check_ip_on_access_port),
    ("W105", _check_lonely_subnet),
    ("W106", _check_subnet_address_clash),
    ("W107", _check_addresses_on_member),
    ("W108", _check_mac_on_loopback),
    ("W109", _check_no_cableable_interface),
    ("W110", _check_reserved_address),
    ("W111", _check_overlapping_prefixes),
    ("W112", _check_loopback_prefix),
    ("W113", _check_undeclared_vlan),
    ("W114", _check_native_vlan_membership),
    ("W115", _check_trunk_all_to_host),
    ("W116", _check_lag_member_vlan),
    ("W117", _check_self_link),
    ("W118", _check_speed_mismatch),
    ("W119", _check_aggregate_endpoint),
    ("W120", _check_half_duplex),
    ("W121", _check_disconnected_topology),
    ("W122", _check_hub_subnets),
    ("W123", _check_unattached_adapter),
    ("W124", _check_attachment_is_a_host),
    ("W125", _check_underlay_reach),
    ("W126", _check_tunnel_mtu),
    ("W127", _check_cleartext_tunnel),
    ("W128", _check_unused_tunnel_interface),
    ("W129", _check_vni_clash),
    ("W130", _check_prefix_domains),
    ("W131", _check_nested_prefix_domains),
    ("W132", _check_link_prefixes),
    ("W133", _check_dangling_patch),
    ("W134", _check_cochannel_aps),
    ("W135", _check_bgp_neighbour_resolves),
    ("W136", _check_empty_vrf),
    ("W137", _check_missing_power_path),
    ("W138", _check_stale_geometry),
    ("W139", _check_empty_group),
    ("W140", _check_departed_member),
    ("W141", _check_unknown_expectation),
    ("W142", _check_stale_annotation),
    ("W143", _check_empty_area),
    ("W144", _check_invisible_style),
    ("W145", _check_unreadable_label),
    ("W146", _check_empty_netns),
    ("W147", _check_policy_empty_table),
    ("W148", _check_unselected_route_table),
    ("W149", _check_shadowed_policy),
    ("W150", _check_empty_zone),
    ("W151", _check_unzoned_interface),
    ("W152", _check_mark_nothing_reads),
    ("W153", _check_mark_nothing_writes),
    ("W154", _check_shadowed_firewall_rule),
    ("I001", _check_local_mac),
    ("I002", _check_uncabled_interface),
    ("I003", _check_nonstandard_port),
    ("I004", _check_ungrouped_user),
    ("I005", _check_veth_inside_one_netns),
)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def validate(inventory: Inventory, config: ValidationConfig | None = None) -> list[Finding]:
    """Check an inventory for semantic problems.

    Args:
        inventory: A tree loaded by :func:`~netviz.loader.load_tree`.
        config: Suppressions and severity overrides, normally
            ``load_config(root).validation``. Defaults apply when omitted.

    Returns:
        Every finding that survives suppression, ordered by source file, then
        by position in the file, then by severity and rule id. The order is
        stable across runs, which keeps golden-file tests meaningful.
    """
    settings = config if config is not None else ValidationConfig()
    context = _build_context(inventory)

    findings: list[Finding] = []
    for rule_id, check in _CHECKS:
        if settings.is_disabled(rule_id):
            continue
        rule = _RULES_BY_ID[rule_id]
        severity = settings.severity_for(rule_id, rule.severity)
        for draft in check(context):
            if context.is_suppressed(rule_id, draft.elements):
                continue
            findings.append(
                Finding(
                    rule=rule_id,
                    severity=severity,
                    message=draft.message,
                    source=context.source_of(draft.elements[0] if draft.elements else None),
                    elements=draft.elements,
                    field_path=draft.field_path,
                )
            )

    findings.sort(key=lambda finding: finding.sort_key)
    return findings


_RULES_BY_ID: Final[Mapping[str, Rule]] = {rule.id: rule for rule in RULES}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _pair_endpoints(
    endpoints: Sequence[_Endpoint],
) -> tuple[tuple[str, _Endpoint, _Endpoint], ...]:
    """Group resolved endpoints by the cable they belong to, in load order.

    ``NG-C001`` guarantees the pair at schema time; the guard is here so a
    document that somehow escaped it cannot make a rule raise.
    """
    by_cable: dict[str, list[_Endpoint]] = {}
    for endpoint in endpoints:
        by_cable.setdefault(endpoint.cable_fqn, []).append(endpoint)
    return tuple(
        (cable_fqn, pair[0], pair[1])
        for cable_fqn, pair in by_cable.items()
        if len(pair) == 2  # NG-C001 guarantees it; a stray document must not raise
    )


def _linked_endpoints(ctx: _Context) -> Iterator[tuple[str, _Endpoint, _Endpoint]]:
    """Yield each cable whose two endpoints both resolve, with those endpoints."""
    for cable_fqn, first, second in ctx.endpoint_pairs:
        if first.resolved and second.resolved:
            yield cable_fqn, first, second


def _cable_elements(cable_fqn: str, *endpoints: _Endpoint) -> tuple[str, ...]:
    """The cable plus the elements it joins, without repeats."""
    owners = (endpoint.owner_fqn for endpoint in endpoints)
    return tuple(dict.fromkeys([cable_fqn, *(fqn for fqn in owners if fqn is not None)]))


def _describe_port(endpoint: _Endpoint, effective: Interface) -> str:
    """Quoted ``element:interface``, naming the LAG the check resolved through."""
    text = _q(endpoint.port)
    if endpoint.interface is not None and effective.name != endpoint.interface.name:
        return f"{text} (aggregated by {_q(effective.name)})"
    return text


def _by_port(entries: Sequence[tuple[str, Interface]]) -> list[tuple[str, Interface]]:
    """Order the members of a *symmetric* clash by name rather than by load order.

    Two interfaces claiming one address are equally guilty: neither is "the
    original" and neither is "the duplicate". Reported in the order the
    inventory happened to load, the finding's wording, its ``elements`` list and
    the file it is anchored to would all move when an unrelated file is renamed
    or two documents are merged into one — because load order is directory
    order. That churn lands in a SARIF baseline and in ``git diff`` on a
    committed report, saying a network changed when only a filename did.

    Sorting by ``element:interface`` makes all three a function of what the
    inventory *says* and of nothing else. Only use it where the members really
    are interchangeable; a rule with a genuine "first declaration wins" ordering
    (``NG-N002``) must keep it.
    """
    return sorted(entries, key=lambda entry: (entry[0], entry[1].name))


def _interface_path(
    owner: InterfaceOwner, interface: Interface, *suffix: str | int
) -> tuple[str | int, ...]:
    """Field path of an interface inside its element document."""
    return _index_path(owner, interface.name, *suffix)


def _index_path(owner: InterfaceOwner, name: str, *suffix: str | int) -> tuple[str | int, ...]:
    """Field path of the interface called ``name`` inside its element document."""
    for index, candidate in enumerate(owner.interfaces):
        if candidate.name == name:
            return ("spec", "interfaces", index, *suffix)
    return ("spec", "interfaces")  # pragma: no cover - the interface always belongs


def _is_layer3(device: Device) -> bool:
    """Does the device forward IP, i.e. is it more than a layer-2 bridge?"""
    forwarding = device.spec.forwarding
    return bool(forwarding and (forwarding.ipv4 or forwarding.ipv6))


def _q(value: str) -> str:
    return f"'{value}'"


def _join(names: Sequence[str], limit: int = _MAX_LISTED) -> str:
    """Render a list of names, abbreviating anything unreasonably long."""
    if len(names) <= limit:
        return ", ".join(_q(name) for name in names)
    shown = ", ".join(_q(name) for name in names[:limit])
    return f"{shown} and {len(names) - limit} more"


def _join_plain(values: Sequence[str], limit: int = _MAX_LISTED) -> str:
    """Render a list of values that are not names — addresses, VLAN descriptions.

    Repeats are collapsed: naming the same broadcast domain twice reads as a
    second one.
    """
    unique = list(dict.fromkeys(values))
    if len(unique) <= limit:
        return ", ".join(unique)
    return f"{', '.join(unique[:limit])} and {len(unique) - limit} more"
