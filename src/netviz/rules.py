"""The catalogue of semantic validation rules.

Every rule the validator can report is declared here exactly once, with a stable
identifier, a default severity and a one-line summary. Keeping the catalogue in
its own module lets the configuration layer resolve and check rule identifiers
without importing the validator itself.

Two identifier vocabularies exist and both are accepted everywhere a rule can be
named (``netviz.toml``, the ``netviz/ignore`` annotation, ``--disable``):

* The **short ids** ``E001``…``E050``, ``W101``…``W146`` and ``I001``…``I005``
  used by the validation engine and printed in diagnostics. The letter is the
  default severity — ``E`` error, ``W`` warning, ``I`` info — as first
  assigned; a rule keeps its id when an inventory re-grades it.
* The **schema ids** ``NV-*`` of ``docs/schema.md`` §10, kept as aliases so the
  published specification and the implementation never drift apart.

Identifiers are permanent. Once assigned, an id is never reused for a different
rule, so suppressions in a user's inventory keep meaning what they meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from netviz.fixes import FixProducer

__all__ = [
    "RULES",
    "RULES_DOC_URL",
    "RULE_IDS",
    "WILDCARD",
    "Rule",
    "Severity",
    "known_rule",
    "resolve_rule_id",
    "rule_for",
]

#: Token that selects every rule at once, e.g. ``netviz/ignore: "*"``.
WILDCARD: Final = "*"

#: Spellings of :data:`WILDCARD` accepted for readability.
_WILDCARD_TOKENS: Final[frozenset[str]] = frozenset({WILDCARD, "all", "any"})


class Severity(str, Enum):
    """How much a finding matters.

    ``error`` fails the run (exit code 4); ``warning`` and ``info`` are reported
    but do not stop rendering. ``--strict`` promotes warnings to errors.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Sort key, most severe first."""
        return _SEVERITY_RANK[self]

    @property
    def is_fatal(self) -> bool:
        """Does a finding of this severity fail the run?"""
        return self is Severity.ERROR

    def __str__(self) -> str:
        return self.value


_SEVERITY_RANK: Final[dict[Severity, int]] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}


@dataclass(frozen=True, slots=True)
class Rule:
    """One semantic check the validator can perform."""

    #: Short, permanent identifier, e.g. ``E002``.
    id: str
    #: Severity used unless the inventory configuration overrides it.
    severity: Severity
    #: One-line description, shown by ``netviz rules``.
    summary: str
    #: Equivalent identifiers from ``docs/schema.md`` §10.
    aliases: tuple[str, ...] = ()
    #: Heading of the rule's section in ``docs/validation-rules.md``, without
    #: the id, without Markdown. Short enough to be a SARIF ``name`` and a
    #: GitHub annotation ``title``; :attr:`anchor` derives the deep link from
    #: it. ``tests/test_docs.py`` fails if it stops matching the heading.
    title: str = ""
    #: Anchor to link to instead of the one :attr:`anchor` derives. Only for a
    #: pseudo-rule whose write-up is a whole section rather than a heading of
    #: its own — see :data:`netviz.diagnostics.LOAD_RULE`.
    section: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        """Every identifier this rule answers to, canonical id first."""
        return (self.id, *self.aliases)

    @property
    def alias(self) -> str | None:
        """The primary ``NV-*`` identifier, or ``None`` for a rule without one."""
        return self.aliases[0] if self.aliases else None

    @property
    def anchor(self) -> str:
        """The fragment of this rule's section in ``docs/validation-rules.md``.

        Derived rather than stored, using the same slug GitHub computes for the
        ``#### `E001` — unknown cable endpoint`` heading: lower-cased, with
        everything that is not a word character, a space or a hyphen dropped and
        spaces turned into hyphens. The em dash leaves the doubled hyphen in
        ``e001--unknown-cable-endpoint``.
        """
        if self.section:
            return self.section
        slug = _NON_SLUG.sub("", f"{self.id} — {self.title}".lower())
        return slug.replace(" ", "-")

    @property
    def help_uri(self) -> str:
        """Permanent link to the write-up of this rule."""
        return f"{RULES_DOC_URL}#{self.anchor}"

    @property
    def fix(self) -> FixProducer | None:
        """What repairs a finding of this rule, or ``None`` if nothing does.

        A pure function from a :class:`~netviz.validate.Finding` and the
        inventory it was found in to the repairs on offer; see
        :mod:`netviz.fixes`. ``None`` is the ordinary answer, and it means the
        repair is not mechanical — not that the rule is unimportant.

        Resolved through a deferred import rather than held as a field, because
        the catalogue must stay a *catalogue*: :mod:`netviz.config` reads it to
        check a rule id, and a producer reaches the whole write path and the
        model layer. Naming a rule therefore costs nothing until somebody asks
        what would repair it.
        """
        from netviz.fixes import producer_for

        return producer_for(self.id)

    @property
    def fixable(self) -> bool:
        """Can a finding of this rule be repaired mechanically?"""
        from netviz.fixes import spec_for

        return spec_for(self.id) is not None

    def __str__(self) -> str:
        return f"{self.id} ({self.severity}): {self.summary}"


#: Characters GitHub's heading slugs drop. Anything outside ``\w``, whitespace
#: and ``-`` disappears rather than being replaced, which is why an em dash
#: surrounded by spaces yields two hyphens and not three.
_NON_SLUG: Final[re.Pattern[str]] = re.compile(r"[^\w\s-]", re.UNICODE)

#: Where the rules are written up. Diagnostics in the machine-readable formats
#: link here, so it has to be a URL a stranger reading a CI annotation can open
#: — not a path relative to whatever directory the tool happened to run in.
RULES_DOC_URL: Final = "https://github.com/blechschmidt/netviz/blob/main/docs/validation-rules.md"


#: Every rule, in report order. ``aliases`` ties each one to ``docs/schema.md``.
RULES: Final[tuple[Rule, ...]] = (
    Rule(
        "E001",
        Severity.ERROR,
        "A cable endpoint references an unknown device or interface.",
        ("NV-C002", "NV-C003"),
        title="unknown cable endpoint",
    ),
    Rule(
        "E002",
        Severity.ERROR,
        "An interface is terminated by more than one cable.",
        ("NV-C005",),
        title="interface terminated by more than one cable",
    ),
    Rule(
        "E003",
        Severity.ERROR,
        "The same MAC address is used by two interfaces in the inventory.",
        ("NV-I008",),
        title="duplicate MAC address",
    ),
    Rule(
        "E004",
        Severity.ERROR,
        "The same IP address is assigned twice within one subnet and VLAN.",
        ("NV-A004",),
        title="duplicate IP address",
    ),
    Rule(
        "E005",
        Severity.ERROR,
        "The two ends of a link disagree about VLANs, so it carries less than it seems.",
        ("NV-C011",),
        title="VLAN mismatch across a link",
    ),
    Rule(
        "E006",
        Severity.ERROR,
        "An adapter declares more downstream interfaces than it has ports.",
        ("NV-X008",),
        title="adapter over capacity",
    ),
    Rule(
        "E007",
        Severity.ERROR,
        "Interface stacking through 'parent'/'members' contains a cycle.",
        ("NV-I004",),
        title="cyclic interface stacking",
    ),
    Rule(
        "E008",
        Severity.ERROR,
        "A lag/bridge member is itself aggregated or carries a sub-interface.",
        ("NV-I005",),
        title="a member is not free to be aggregated",
    ),
    Rule(
        "E009",
        Severity.ERROR,
        "A 'vlan' sub-interface's VID is not carried by its parent interface.",
        ("NV-V005",),
        title="sub-interface VLAN not carried by its parent",
    ),
    Rule(
        "E010",
        Severity.ERROR,
        "A MAC address has the multicast bit set, so no interface can own it.",
        ("NV-I009",),
        title="multicast MAC address",
    ),
    Rule(
        "E011",
        Severity.ERROR,
        "A cable's medium disagrees with the radio/wired type of an endpoint.",
        ("NV-C006",),
        title="medium disagrees with the endpoint type",
    ),
    Rule(
        "E012",
        Severity.ERROR,
        "A cable endpoint is a loopback, vlan or bridge interface.",
        ("NV-C009",),
        title="cable terminates on an interface with no socket",
    ),
    Rule(
        "E013",
        Severity.ERROR,
        "A cable lands on an adapter's upstream port that 'attached_to' claims.",
        ("NV-X005",),
        title="host attachment declared twice",
    ),
    Rule(
        "E014",
        Severity.ERROR,
        "Adapter 'attached_to' attachments form a cycle.",
        ("NV-X006",),
        title="cyclic adapter attachment",
    ),
    Rule(
        "E015",
        Severity.ERROR,
        "An adapter's 'attached_to' names no element that could host it.",
        ("NV-X001",),
        title="attached_to names nothing that could host the adapter",
    ),
    Rule(
        "E016",
        Severity.ERROR,
        "A tunnel endpoint references an unknown element or interface.",
        ("NV-T002",),
        title="unknown tunnel endpoint",
    ),
    Rule(
        "E017",
        Severity.ERROR,
        "A tunnel endpoint is not an interface of type 'tunnel'.",
        ("NV-T003",),
        title="tunnel endpoint is not a tunnel interface",
    ),
    Rule(
        "E018",
        Severity.ERROR,
        "A tunnel's 'over' names no tunnel of this inventory.",
        ("NV-T004",),
        title="over names no tunnel",
    ),
    Rule(
        "E019",
        Severity.ERROR,
        "Tunnel 'over' references form a cycle, so nothing reaches the underlay.",
        ("NV-T005",),
        title="cyclic tunnel encapsulation",
    ),
    Rule(
        "E020",
        Severity.ERROR,
        "An interface's 'gateway' lies outside every prefix configured on it.",
        ("NV-A013",),
        title="first hop is not on-link",
    ),
    Rule(
        "E021",
        Severity.ERROR,
        "A cable terminates on a position the patch panel does not have.",
        ("NV-P001",),
        title="cable on a position the patch panel does not have",
    ),
    Rule(
        "E022",
        Severity.ERROR,
        "A patch-panel position terminates more than one cable.",
        ("NV-P003",),
        title="patch-panel position terminated twice",
    ),
    Rule(
        "E023",
        Severity.ERROR,
        "A patch panel is named where an active element is required.",
        ("NV-P004",),
        title="patch panel where an active element is required",
    ),
    Rule(
        "E024",
        Severity.ERROR,
        "A patch run leaves a panel and is patched back into the same one.",
        ("NV-P005",),
        title="patch run loops back into its own panel",
    ),
    Rule(
        "E025",
        Severity.ERROR,
        "Two elements occupy the same unit of one rack.",
        ("NV-U001",),
        title="two elements occupy the same rack unit",
    ),
    Rule(
        "E026",
        Severity.ERROR,
        "An element extends past the top of the rack it is mounted in.",
        ("NV-U002",),
        title="element mounted above the top of its rack",
    ),
    Rule(
        "E027",
        Severity.ERROR,
        "One rack is declared with two different heights.",
        ("NV-U003",),
        title="rack declared with two heights",
    ),
    Rule(
        "E028",
        Severity.ERROR,
        "A wireless link does not join one 'ap' radio to a client radio.",
        ("NV-W007",),
        title="wireless link is not an association",
    ),
    Rule(
        "E029",
        Severity.ERROR,
        "The same BSSID is advertised by two radios in the inventory.",
        ("NV-W008",),
        title="duplicate BSSID",
    ),
    Rule(
        "E030",
        Severity.ERROR,
        "An SSID is mapped to a VLAN the access point carries nowhere.",
        ("NV-W009",),
        title="SSID VLAN is carried nowhere on the access point",
    ),
    Rule(
        "E031",
        Severity.ERROR,
        "A client radio is associated to an SSID its access point does not advertise.",
        ("NV-W010",),
        title="associated to an SSID the access point does not advertise",
    ),
    Rule(
        "E032",
        Severity.ERROR,
        "A route's next hop lies in no prefix the device configures in that VRF.",
        ("NV-F008",),
        title="next hop is not on-link",
    ),
    Rule(
        "E033",
        Severity.ERROR,
        "A route's 'dev' names an interface the device does not have.",
        ("NV-F009",),
        title="route sends out of an unknown interface",
    ),
    Rule(
        "E034",
        Severity.ERROR,
        "An OSPF interface is not in the device's interface list.",
        ("NV-F010",),
        title="OSPF runs on an interface the device does not have",
    ),
    Rule(
        "E035",
        Severity.ERROR,
        "The two ends of a resolved BGP session disagree about an AS number.",
        ("NV-F011",),
        title="BGP session disagrees about an AS number",
    ),
    Rule(
        "E036",
        Severity.ERROR,
        "Two elements claim the same router id.",
        ("NV-F012",),
        title="duplicate router id",
    ),
    Rule(
        "E037",
        Severity.ERROR,
        "One PDU outlet is claimed by two power supplies.",
        ("NV-E010",),
        title="PDU outlet claimed twice",
    ),
    Rule(
        "E038",
        Severity.ERROR,
        "A power input names an outlet that does not exist.",
        ("NV-E011",),
        title="power input names no outlet that exists",
    ),
    Rule(
        "E039",
        Severity.ERROR,
        "The declared load on a PDU exceeds its capacity.",
        ("NV-E012",),
        title="PDU load exceeds its capacity",
    ),
    Rule(
        "E040",
        Severity.ERROR,
        "The PoE allocated on a device's ports exceeds its budget.",
        ("NV-E013",),
        title="PoE allocation exceeds the budget",
    ),
    Rule(
        "E041",
        Severity.ERROR,
        "A PoE-powered device's uplink offers no PoE, or too little.",
        ("NV-E014",),
        title="PoE-powered device has no PoE uplink",
    ),
    Rule(
        "E042",
        Severity.ERROR,
        "A device claims redundant power but its feeds are not independent.",
        ("NV-E015",),
        title="redundant power that is not redundant",
    ),
    Rule(
        "E043",
        Severity.ERROR,
        "A group names a member the inventory does not declare.",
        ("NV-S010",),
        title="group member does not exist",
    ),
    Rule(
        "E044",
        Severity.ERROR,
        "A group names a member that is not a user or a group.",
        ("NV-S011",),
        title="group member is not an identity",
    ),
    Rule(
        "E045",
        Severity.ERROR,
        "Group membership forms a cycle.",
        ("NV-S012",),
        title="group membership cycle",
    ),
    Rule(
        "E046",
        Severity.ERROR,
        "Two identities claim the same login, uid or gid.",
        ("NV-S013",),
        title="duplicate account identifier",
    ),
    Rule(
        "E047",
        Severity.ERROR,
        "An element declares it must keep its gateway under any single failure, and does not.",
        (),
        title="declared gateway redundancy is not met",
    ),
    Rule(
        "E048",
        Severity.ERROR,
        "An element declares it must keep power under any single failure, and does not.",
        (),
        title="declared power redundancy is not met",
    ),
    Rule(
        "E049",
        Severity.ERROR,
        "A cable terminates on one end of a veth pair.",
        ("NV-N024",),
        title="cable on a virtual interface",
    ),
    Rule(
        "E050",
        Severity.ERROR,
        "A bridge or lag aggregates a member in another network namespace.",
        ("NV-N025",),
        title="aggregate spans network namespaces",
    ),
    Rule(
        "W101",
        Severity.WARNING,
        "An interface has neither IPv4 nor IPv6 and is not a switchport.",
        ("NV-I013",),
        title="interface neither routes nor switches",
    ),
    Rule(
        "W102",
        Severity.WARNING,
        "The two endpoints of a cable disagree about the MTU.",
        ("NV-C010",),
        title="MTU mismatch across a link",
    ),
    Rule(
        "W103",
        Severity.WARNING,
        "A device terminates no cable and hosts no adapter: an orphan node.",
        ("NV-C016",),
        title="orphan device",
    ),
    Rule(
        "W104",
        Severity.WARNING,
        "An access port of a layer-2-only switch carries an IP address.",
        ("NV-V009",),
        title="IP address on an access port",
    ),
    Rule(
        "W105",
        Severity.WARNING,
        "A subnet holds exactly one element, so its prefix length may be wrong.",
        ("NV-A008",),
        title="subnet with a single member",
    ),
    Rule(
        "W106",
        Severity.WARNING,
        "Two elements claim the same address in one subnet, in different VLANs.",
        ("NV-A009",),
        title="one address claimed twice in a subnet",
    ),
    Rule(
        "W107",
        Severity.WARNING,
        "A lag/bridge member carries its own IPv4 or IPv6 addresses.",
        ("NV-I006",),
        title="addresses on an aggregate member",
    ),
    Rule(
        "W108",
        Severity.WARNING,
        "A loopback interface declares a MAC address.",
        ("NV-I007",),
        title="MAC address on a loopback",
    ),
    Rule(
        "W109",
        Severity.WARNING,
        "A device declares no ethernet, wifi or lag interface, so it cannot be cabled.",
        ("NV-I012",),
        title="device that cannot be cabled",
    ),
    Rule(
        "W110",
        Severity.WARNING,
        "An address is the network or broadcast address of its own prefix.",
        ("NV-A005",),
        title="network or broadcast address assigned",
    ),
    Rule(
        "W111",
        Severity.WARNING,
        "Two interfaces on one element hold overlapping prefixes.",
        ("NV-A006",),
        title="overlapping prefixes on one element",
    ),
    Rule(
        "W112",
        Severity.WARNING,
        "A loopback interface carries a prefix other than /32 or /128.",
        ("NV-A007",),
        title="loopback with a non-host prefix",
    ),
    Rule(
        "W113",
        Severity.WARNING,
        "A port references a VLAN the device's 'vlans' database does not declare.",
        ("NV-V004",),
        title="undeclared VLAN referenced",
    ),
    Rule(
        "W114",
        Severity.WARNING,
        "A trunk's 'native_vlan' is not listed in its 'trunk_vlans'.",
        ("NV-V006",),
        title="native VLAN missing from trunk_vlans",
    ),
    Rule(
        "W115",
        Severity.WARNING,
        "A port trunking every VLAN faces a host rather than another switch.",
        ("NV-V007",),
        title="every VLAN trunked to a host",
    ),
    Rule(
        "W116",
        Severity.WARNING,
        "A lag member declares a 'vlan' block that differs from the aggregate's.",
        ("NV-V008",),
        title="LAG member contradicts its aggregate",
    ),
    Rule(
        "W117",
        Severity.WARNING,
        "Both endpoints of one cable land on the same element.",
        ("NV-C004",),
        title="both ends of a cable on one element",
    ),
    Rule(
        "W118",
        Severity.WARNING,
        "A cable's 'speed' disagrees with the speed an endpoint declares.",
        ("NV-C008",),
        title="cable and endpoint disagree about speed",
    ),
    Rule(
        "W119",
        Severity.WARNING,
        "A cable endpoint is a lag aggregate rather than one of its members.",
        ("NV-C012",),
        title="cable terminates on a LAG aggregate",
    ),
    Rule(
        "W120",
        Severity.WARNING,
        "A cable is 'duplex: half' on a link that involves no hub.",
        ("NV-C013",),
        title="half duplex without a hub",
    ),
    Rule(
        "W121",
        Severity.WARNING,
        "The topology graph is disconnected: it falls into separate islands.",
        ("NV-C014",),
        title="disconnected topology",
    ),
    Rule(
        "W122",
        Severity.WARNING,
        "Two elements on one hub are addressed in different subnets.",
        ("NV-H005",),
        title="one hub, two subnets",
    ),
    Rule(
        "W123",
        Severity.WARNING,
        "An adapter has cabled downstream ports but no 'attached_to' host.",
        ("NV-X002",),
        title="cabled adapter with no host",
    ),
    Rule(
        "W124",
        Severity.WARNING,
        "An adapter's 'attached_to' points at a hub or a switch, not a host.",
        ("NV-X007",),
        title="adapter attached to a hub or a switch",
    ),
    Rule(
        "W125",
        Severity.WARNING,
        "An overlay terminates where its underlay tunnel does not reach.",
        ("NV-T006",),
        title="overlay reaches past its underlay",
    ),
    Rule(
        "W126",
        Severity.WARNING,
        "A tunnel's MTU does not fit inside its underlay after encapsulation.",
        ("NV-T011",),
        title="tunnel MTU does not fit its underlay",
    ),
    Rule(
        "W127",
        Severity.WARNING,
        "A tunnel encrypts nothing and no tunnel it runs inside does either.",
        ("NV-T012",),
        title="tunnel carries traffic in the clear",
    ),
    Rule(
        "W128",
        Severity.WARNING,
        "A 'tunnel' interface is named by no tunnel document.",
        ("NV-T013",),
        title="tunnel interface named by no tunnel",
    ),
    Rule(
        "W129",
        Severity.WARNING,
        "Two tunnels terminating on one element use the same VNI.",
        ("NV-T014",),
        title="two tunnels share a VNI on one element",
    ),
    Rule(
        "W130",
        Severity.WARNING,
        "One prefix is claimed by two broadcast domains that cannot reach each other.",
        ("NV-A010",),
        title="prefix claimed by two broadcast domains",
    ),
    Rule(
        "W131",
        Severity.WARNING,
        "A prefix nested inside another is used in a different broadcast domain.",
        ("NV-A011",),
        title="nested prefix in a different broadcast domain",
    ),
    Rule(
        "W132",
        Severity.WARNING,
        "Two directly linked interfaces are addressed in prefixes that do not meet.",
        ("NV-A012",),
        title="address outside every prefix on its link",
    ),
    Rule(
        "W133",
        Severity.WARNING,
        "A cabled patch-panel position is coupled to one nothing is patched into.",
        ("NV-P002",),
        title="patch run stops inside the panel",
    ),
    Rule(
        "W134",
        Severity.WARNING,
        "Two access points in one broadcast domain share overlapping channels.",
        ("NV-W011",),
        title="access points on overlapping channels",
    ),
    Rule(
        "W135",
        Severity.WARNING,
        "A BGP neighbour address resolves to no element of the inventory.",
        ("NV-F013",),
        title="BGP neighbour is not in the inventory",
    ),
    Rule(
        "W136",
        Severity.WARNING,
        "A VRF is declared that no interface of the device is bound to.",
        ("NV-F014",),
        title="VRF with no interface bound to it",
    ),
    Rule(
        "W137",
        Severity.WARNING,
        "A device declares a power draw but no power path.",
        ("NV-E016",),
        title="declared draw with no power path",
    ),
    Rule(
        "W138",
        Severity.WARNING,
        "Diagram geometry names an element the inventory does not declare.",
        ("NV-Y001",),
        title="stale diagram geometry",
    ),
    Rule(
        "W139",
        Severity.WARNING,
        "A group has no members.",
        ("NV-S014",),
        title="group with no members",
    ),
    Rule(
        "W140",
        Severity.WARNING,
        "A group still lists a user who has departed.",
        ("NV-S015",),
        title="departed user still in a group",
    ),
    Rule(
        "W141",
        Severity.WARNING,
        "A redundancy expectation names something the tool does not understand.",
        (),
        title="unknown redundancy expectation",
    ),
    Rule(
        "W142",
        Severity.WARNING,
        "A diagram annotation names an element the inventory does not declare.",
        ("NV-G001",),
        title="annotation about something that is gone",
    ),
    Rule(
        "W143",
        Severity.WARNING,
        "An area's selector matches no element of the inventory.",
        ("NV-G004",),
        title="area that encloses nothing",
    ),
    Rule(
        "W144",
        Severity.WARNING,
        "A style fades an element to nothing, so it is drawn invisibly.",
        ("NV-Z003",),
        title="element styled invisible",
    ),
    Rule(
        "W145",
        Severity.WARNING,
        "A style draws an element's label in the colour of the box behind it.",
        ("NV-Z005",),
        title="unreadable label colour",
    ),
    Rule(
        "W146",
        Severity.WARNING,
        "A declared network namespace holds no interface.",
        ("NV-N026",),
        title="network namespace with no interface",
    ),
    Rule(
        "W147",
        Severity.WARNING,
        "A policy rule looks up a declared routing table that holds no route.",
        ("NV-F022",),
        title="policy rule looks up an empty table",
    ),
    Rule(
        "W148",
        Severity.WARNING,
        "A declared routing table that no policy rule ever looks up.",
        ("NV-F023",),
        title="routing table nothing selects",
    ),
    Rule(
        "W149",
        Severity.WARNING,
        "A policy rule is shadowed by an earlier rule that matches every packet.",
        ("NV-F024",),
        title="unreachable policy rule",
    ),
    Rule(
        "W150",
        Severity.WARNING,
        "A declared security zone holds no interface.",
        ("NV-B010",),
        title="security zone with no interface",
    ),
    Rule(
        "W151",
        Severity.WARNING,
        "An interface is in no zone, on a device that divides its interfaces into zones.",
        ("NV-B011",),
        title="interface in no zone",
    ),
    Rule(
        "W152",
        Severity.WARNING,
        "The firewall writes a mark no routing policy rule ever matches.",
        ("NV-B012",),
        title="firewall mark nothing reads",
    ),
    Rule(
        "W153",
        Severity.WARNING,
        "A routing policy rule matches a mark the device's firewall never writes.",
        ("NV-B013",),
        title="firewall mark nothing writes",
    ),
    Rule(
        "W154",
        Severity.WARNING,
        "A firewall rule is shadowed by an earlier rule that matches every packet.",
        ("NV-B014",),
        title="unreachable firewall rule",
    ),
    Rule(
        "I001",
        Severity.INFO,
        "A MAC address is locally administered rather than vendor-assigned.",
        ("NV-I010",),
        title="locally administered MAC address",
    ),
    Rule(
        "I002",
        Severity.INFO,
        "An interface is enabled but terminates no cable.",
        ("NV-C015",),
        title="enabled interface terminates no cable",
    ),
    Rule(
        "I003",
        Severity.INFO,
        "A tunnel listens on a port other than the registered one for its type.",
        ("NV-T015",),
        title="tunnel on a non-standard port",
    ),
    Rule(
        "I004",
        Severity.INFO,
        "A person's account is a member of no group.",
        ("NV-S016",),
        title="person in no group",
    ),
    Rule(
        "I005",
        Severity.INFO,
        "Both ends of a veth pair are in the same network namespace.",
        ("NV-N027",),
        title="veth pair crosses no boundary",
    ),
)

#: Canonical rule ids, in report order.
RULE_IDS: Final[tuple[str, ...]] = tuple(rule.id for rule in RULES)

#: Every accepted spelling (upper-cased) mapped to its rule.
_BY_NAME: Final[dict[str, Rule]] = {name.upper(): rule for rule in RULES for name in rule.names}


def rule_for(rule_id: str) -> Rule:
    """The rule ``rule_id`` names, accepting short ids and ``NV-*`` aliases.

    Raises:
        KeyError: No rule carries that identifier.
    """
    try:
        return _BY_NAME[rule_id.strip().upper()]
    except KeyError:
        raise KeyError(rule_id) from None


def known_rule(rule_id: str) -> bool:
    """Is ``rule_id`` a rule identifier this build knows about?"""
    return rule_id.strip().upper() in _BY_NAME


def resolve_rule_id(token: str, *, strict: bool = True) -> str:
    """Normalise ``token`` to a canonical rule id.

    The wildcard tokens (``*``, ``all``, ``any``) normalise to :data:`WILDCARD`.

    Args:
        token: A rule id, an ``NV-*`` alias, or a wildcard.
        strict: When true, an unknown identifier raises. When false, it is
            upper-cased and returned unchanged, so a typo in a *suppression*
            simply matches nothing instead of aborting the run.

    Raises:
        KeyError: ``strict`` is set and ``token`` names no known rule.
    """
    text = token.strip()
    if text.lower() in _WILDCARD_TOKENS:
        return WILDCARD
    rule = _BY_NAME.get(text.upper())
    if rule is not None:
        return rule.id
    if strict:
        raise KeyError(token)
    return text.upper()
