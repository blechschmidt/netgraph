"""The catalogue of semantic validation rules.

Every rule the validator can report is declared here exactly once, with a stable
identifier, a default severity and a one-line summary. Keeping the catalogue in
its own module lets the configuration layer resolve and check rule identifiers
without importing the validator itself.

Two identifier vocabularies exist and both are accepted everywhere a rule can be
named (``netgraph.toml``, the ``netgraph/ignore`` annotation, ``--disable``):

* The **short ids** ``E001``…``E019``, ``W101``…``W129`` and ``I001``…``I003``
  used by the validation engine and printed in diagnostics. The letter is the
  default severity — ``E`` error, ``W`` warning, ``I`` info — as first
  assigned; a rule keeps its id when an inventory re-grades it.
* The **schema ids** ``NG-*`` of ``docs/schema.md`` §10, kept as aliases so the
  published specification and the implementation never drift apart.

Identifiers are permanent. Once assigned, an id is never reused for a different
rule, so suppressions in a user's inventory keep meaning what they meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

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

#: Token that selects every rule at once, e.g. ``netgraph/ignore: "*"``.
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
    #: One-line description, shown by ``netgraph rules``.
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
    #: its own — see :data:`netgraph.report.LOAD_RULE`.
    section: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        """Every identifier this rule answers to, canonical id first."""
        return (self.id, *self.aliases)

    @property
    def alias(self) -> str | None:
        """The primary ``NG-*`` identifier, or ``None`` for a rule without one."""
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

    def __str__(self) -> str:
        return f"{self.id} ({self.severity}): {self.summary}"


#: Characters GitHub's heading slugs drop. Anything outside ``\w``, whitespace
#: and ``-`` disappears rather than being replaced, which is why an em dash
#: surrounded by spaces yields two hyphens and not three.
_NON_SLUG: Final[re.Pattern[str]] = re.compile(r"[^\w\s-]", re.UNICODE)

#: Where the rules are written up. Diagnostics in the machine-readable formats
#: link here, so it has to be a URL a stranger reading a CI annotation can open
#: — not a path relative to whatever directory the tool happened to run in.
RULES_DOC_URL: Final = "https://github.com/blechschmidt/netgraph/blob/main/docs/validation-rules.md"


#: Every rule, in report order. ``aliases`` ties each one to ``docs/schema.md``.
RULES: Final[tuple[Rule, ...]] = (
    Rule(
        "E001",
        Severity.ERROR,
        "A cable endpoint references an unknown device or interface.",
        ("NG-C002", "NG-C003"),
        title="unknown cable endpoint",
    ),
    Rule(
        "E002",
        Severity.ERROR,
        "An interface is terminated by more than one cable.",
        ("NG-C005",),
        title="interface terminated by more than one cable",
    ),
    Rule(
        "E003",
        Severity.ERROR,
        "The same MAC address is used by two interfaces in the inventory.",
        ("NG-I008",),
        title="duplicate MAC address",
    ),
    Rule(
        "E004",
        Severity.ERROR,
        "The same IP address is assigned twice within one subnet and VLAN.",
        ("NG-A004",),
        title="duplicate IP address",
    ),
    Rule(
        "E005",
        Severity.ERROR,
        "The two ends of a link disagree about VLANs, so it carries less than it seems.",
        ("NG-C011",),
        title="VLAN mismatch across a link",
    ),
    Rule(
        "E006",
        Severity.ERROR,
        "An adapter declares more downstream interfaces than it has ports.",
        ("NG-X008",),
        title="adapter over capacity",
    ),
    Rule(
        "E007",
        Severity.ERROR,
        "Interface stacking through 'parent'/'members' contains a cycle.",
        ("NG-I004",),
        title="cyclic interface stacking",
    ),
    Rule(
        "E008",
        Severity.ERROR,
        "A lag/bridge member is itself aggregated or carries a sub-interface.",
        ("NG-I005",),
        title="a member is not free to be aggregated",
    ),
    Rule(
        "E009",
        Severity.ERROR,
        "A 'vlan' sub-interface's VID is not carried by its parent interface.",
        ("NG-V005",),
        title="sub-interface VLAN not carried by its parent",
    ),
    Rule(
        "E010",
        Severity.ERROR,
        "A MAC address has the multicast bit set, so no interface can own it.",
        ("NG-I009",),
        title="multicast MAC address",
    ),
    Rule(
        "E011",
        Severity.ERROR,
        "A cable's medium disagrees with the radio/wired type of an endpoint.",
        ("NG-C006",),
        title="medium disagrees with the endpoint type",
    ),
    Rule(
        "E012",
        Severity.ERROR,
        "A cable endpoint is a loopback, vlan or bridge interface.",
        ("NG-C009",),
        title="cable terminates on an interface with no socket",
    ),
    Rule(
        "E013",
        Severity.ERROR,
        "A cable lands on an adapter's upstream port that 'attached_to' claims.",
        ("NG-X005",),
        title="host attachment declared twice",
    ),
    Rule(
        "E014",
        Severity.ERROR,
        "Adapter 'attached_to' attachments form a cycle.",
        ("NG-X006",),
        title="cyclic adapter attachment",
    ),
    Rule(
        "E015",
        Severity.ERROR,
        "An adapter's 'attached_to' names no element that could host it.",
        ("NG-X001",),
        title="attached_to names nothing that could host the adapter",
    ),
    Rule(
        "E016",
        Severity.ERROR,
        "A tunnel endpoint references an unknown element or interface.",
        ("NG-T002",),
        title="unknown tunnel endpoint",
    ),
    Rule(
        "E017",
        Severity.ERROR,
        "A tunnel endpoint is not an interface of type 'tunnel'.",
        ("NG-T003",),
        title="tunnel endpoint is not a tunnel interface",
    ),
    Rule(
        "E018",
        Severity.ERROR,
        "A tunnel's 'over' names no tunnel of this inventory.",
        ("NG-T004",),
        title="over names no tunnel",
    ),
    Rule(
        "E019",
        Severity.ERROR,
        "Tunnel 'over' references form a cycle, so nothing reaches the underlay.",
        ("NG-T005",),
        title="cyclic tunnel encapsulation",
    ),
    Rule(
        "E020",
        Severity.ERROR,
        "An interface's 'gateway' lies outside every prefix configured on it.",
        ("NG-A013",),
        title="first hop is not on-link",
    ),
    Rule(
        "E021",
        Severity.ERROR,
        "A cable terminates on a position the patch panel does not have.",
        ("NG-P001",),
        title="cable on a position the patch panel does not have",
    ),
    Rule(
        "E022",
        Severity.ERROR,
        "A patch-panel position terminates more than one cable.",
        ("NG-P003",),
        title="patch-panel position terminated twice",
    ),
    Rule(
        "E023",
        Severity.ERROR,
        "A patch panel is named where an active element is required.",
        ("NG-P004",),
        title="patch panel where an active element is required",
    ),
    Rule(
        "E024",
        Severity.ERROR,
        "A patch run leaves a panel and is patched back into the same one.",
        ("NG-P005",),
        title="patch run loops back into its own panel",
    ),
    Rule(
        "E025",
        Severity.ERROR,
        "Two elements occupy the same unit of one rack.",
        ("NG-U001",),
        title="two elements occupy the same rack unit",
    ),
    Rule(
        "E026",
        Severity.ERROR,
        "An element extends past the top of the rack it is mounted in.",
        ("NG-U002",),
        title="element mounted above the top of its rack",
    ),
    Rule(
        "E027",
        Severity.ERROR,
        "One rack is declared with two different heights.",
        ("NG-U003",),
        title="rack declared with two heights",
    ),
    Rule(
        "E028",
        Severity.ERROR,
        "A wireless link does not join one 'ap' radio to a client radio.",
        ("NG-W007",),
        title="wireless link is not an association",
    ),
    Rule(
        "E029",
        Severity.ERROR,
        "The same BSSID is advertised by two radios in the inventory.",
        ("NG-W008",),
        title="duplicate BSSID",
    ),
    Rule(
        "E030",
        Severity.ERROR,
        "An SSID is mapped to a VLAN the access point carries nowhere.",
        ("NG-W009",),
        title="SSID VLAN is carried nowhere on the access point",
    ),
    Rule(
        "E031",
        Severity.ERROR,
        "A client radio is associated to an SSID its access point does not advertise.",
        ("NG-W010",),
        title="associated to an SSID the access point does not advertise",
    ),
    Rule(
        "W101",
        Severity.WARNING,
        "An interface has neither IPv4 nor IPv6 and is not a switchport.",
        ("NG-I013",),
        title="interface neither routes nor switches",
    ),
    Rule(
        "W102",
        Severity.WARNING,
        "The two endpoints of a cable disagree about the MTU.",
        ("NG-C010",),
        title="MTU mismatch across a link",
    ),
    Rule(
        "W103",
        Severity.WARNING,
        "A device terminates no cable and hosts no adapter: an orphan node.",
        ("NG-C016",),
        title="orphan device",
    ),
    Rule(
        "W104",
        Severity.WARNING,
        "An access port of a layer-2-only switch carries an IP address.",
        ("NG-V009",),
        title="IP address on an access port",
    ),
    Rule(
        "W105",
        Severity.WARNING,
        "A subnet holds exactly one element, so its prefix length may be wrong.",
        ("NG-A008",),
        title="subnet with a single member",
    ),
    Rule(
        "W106",
        Severity.WARNING,
        "Two elements claim the same address in one subnet, in different VLANs.",
        ("NG-A009",),
        title="one address claimed twice in a subnet",
    ),
    Rule(
        "W107",
        Severity.WARNING,
        "A lag/bridge member carries its own IPv4 or IPv6 addresses.",
        ("NG-I006",),
        title="addresses on an aggregate member",
    ),
    Rule(
        "W108",
        Severity.WARNING,
        "A loopback interface declares a MAC address.",
        ("NG-I007",),
        title="MAC address on a loopback",
    ),
    Rule(
        "W109",
        Severity.WARNING,
        "A device declares no ethernet, wifi or lag interface, so it cannot be cabled.",
        ("NG-I012",),
        title="device that cannot be cabled",
    ),
    Rule(
        "W110",
        Severity.WARNING,
        "An address is the network or broadcast address of its own prefix.",
        ("NG-A005",),
        title="network or broadcast address assigned",
    ),
    Rule(
        "W111",
        Severity.WARNING,
        "Two interfaces on one element hold overlapping prefixes.",
        ("NG-A006",),
        title="overlapping prefixes on one element",
    ),
    Rule(
        "W112",
        Severity.WARNING,
        "A loopback interface carries a prefix other than /32 or /128.",
        ("NG-A007",),
        title="loopback with a non-host prefix",
    ),
    Rule(
        "W113",
        Severity.WARNING,
        "A port references a VLAN the device's 'vlans' database does not declare.",
        ("NG-V004",),
        title="undeclared VLAN referenced",
    ),
    Rule(
        "W114",
        Severity.WARNING,
        "A trunk's 'native_vlan' is not listed in its 'trunk_vlans'.",
        ("NG-V006",),
        title="native VLAN missing from trunk_vlans",
    ),
    Rule(
        "W115",
        Severity.WARNING,
        "A port trunking every VLAN faces a host rather than another switch.",
        ("NG-V007",),
        title="every VLAN trunked to a host",
    ),
    Rule(
        "W116",
        Severity.WARNING,
        "A lag member declares a 'vlan' block that differs from the aggregate's.",
        ("NG-V008",),
        title="LAG member contradicts its aggregate",
    ),
    Rule(
        "W117",
        Severity.WARNING,
        "Both endpoints of one cable land on the same element.",
        ("NG-C004",),
        title="both ends of a cable on one element",
    ),
    Rule(
        "W118",
        Severity.WARNING,
        "A cable's 'speed' disagrees with the speed an endpoint declares.",
        ("NG-C008",),
        title="cable and endpoint disagree about speed",
    ),
    Rule(
        "W119",
        Severity.WARNING,
        "A cable endpoint is a lag aggregate rather than one of its members.",
        ("NG-C012",),
        title="cable terminates on a LAG aggregate",
    ),
    Rule(
        "W120",
        Severity.WARNING,
        "A cable is 'duplex: half' on a link that involves no hub.",
        ("NG-C013",),
        title="half duplex without a hub",
    ),
    Rule(
        "W121",
        Severity.WARNING,
        "The topology graph is disconnected: it falls into separate islands.",
        ("NG-C014",),
        title="disconnected topology",
    ),
    Rule(
        "W122",
        Severity.WARNING,
        "Two elements on one hub are addressed in different subnets.",
        ("NG-H005",),
        title="one hub, two subnets",
    ),
    Rule(
        "W123",
        Severity.WARNING,
        "An adapter has cabled downstream ports but no 'attached_to' host.",
        ("NG-X002",),
        title="cabled adapter with no host",
    ),
    Rule(
        "W124",
        Severity.WARNING,
        "An adapter's 'attached_to' points at a hub or a switch, not a host.",
        ("NG-X007",),
        title="adapter attached to a hub or a switch",
    ),
    Rule(
        "W125",
        Severity.WARNING,
        "An overlay terminates where its underlay tunnel does not reach.",
        ("NG-T006",),
        title="overlay reaches past its underlay",
    ),
    Rule(
        "W126",
        Severity.WARNING,
        "A tunnel's MTU does not fit inside its underlay after encapsulation.",
        ("NG-T011",),
        title="tunnel MTU does not fit its underlay",
    ),
    Rule(
        "W127",
        Severity.WARNING,
        "A tunnel encrypts nothing and no tunnel it runs inside does either.",
        ("NG-T012",),
        title="tunnel carries traffic in the clear",
    ),
    Rule(
        "W128",
        Severity.WARNING,
        "A 'tunnel' interface is named by no tunnel document.",
        ("NG-T013",),
        title="tunnel interface named by no tunnel",
    ),
    Rule(
        "W129",
        Severity.WARNING,
        "Two tunnels terminating on one element use the same VNI.",
        ("NG-T014",),
        title="two tunnels share a VNI on one element",
    ),
    Rule(
        "W130",
        Severity.WARNING,
        "One prefix is claimed by two broadcast domains that cannot reach each other.",
        ("NG-A010",),
        title="prefix claimed by two broadcast domains",
    ),
    Rule(
        "W131",
        Severity.WARNING,
        "A prefix nested inside another is used in a different broadcast domain.",
        ("NG-A011",),
        title="nested prefix in a different broadcast domain",
    ),
    Rule(
        "W132",
        Severity.WARNING,
        "Two directly linked interfaces are addressed in prefixes that do not meet.",
        ("NG-A012",),
        title="address outside every prefix on its link",
    ),
    Rule(
        "W133",
        Severity.WARNING,
        "A cabled patch-panel position is coupled to one nothing is patched into.",
        ("NG-P002",),
        title="patch run stops inside the panel",
    ),
    Rule(
        "W134",
        Severity.WARNING,
        "Two access points in one broadcast domain share overlapping channels.",
        ("NG-W011",),
        title="access points on overlapping channels",
    ),
    Rule(
        "I001",
        Severity.INFO,
        "A MAC address is locally administered rather than vendor-assigned.",
        ("NG-I010",),
        title="locally administered MAC address",
    ),
    Rule(
        "I002",
        Severity.INFO,
        "An interface is enabled but terminates no cable.",
        ("NG-C015",),
        title="enabled interface terminates no cable",
    ),
    Rule(
        "I003",
        Severity.INFO,
        "A tunnel listens on a port other than the registered one for its type.",
        ("NG-T015",),
        title="tunnel on a non-standard port",
    ),
)

#: Canonical rule ids, in report order.
RULE_IDS: Final[tuple[str, ...]] = tuple(rule.id for rule in RULES)

#: Every accepted spelling (upper-cased) mapped to its rule.
_BY_NAME: Final[dict[str, Rule]] = {name.upper(): rule for rule in RULES for name in rule.names}


def rule_for(rule_id: str) -> Rule:
    """The rule ``rule_id`` names, accepting short ids and ``NG-*`` aliases.

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
        token: A rule id, an ``NG-*`` alias, or a wildcard.
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
