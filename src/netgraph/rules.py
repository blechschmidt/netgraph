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

from dataclasses import dataclass
from enum import Enum
from typing import Final

__all__ = [
    "RULES",
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

    @property
    def names(self) -> tuple[str, ...]:
        """Every identifier this rule answers to, canonical id first."""
        return (self.id, *self.aliases)

    def __str__(self) -> str:
        return f"{self.id} ({self.severity}): {self.summary}"


#: Every rule, in report order. ``aliases`` ties each one to ``docs/schema.md``.
RULES: Final[tuple[Rule, ...]] = (
    Rule(
        "E001",
        Severity.ERROR,
        "A cable endpoint references an unknown device or interface.",
        ("NG-C002", "NG-C003"),
    ),
    Rule(
        "E002",
        Severity.ERROR,
        "An interface is terminated by more than one cable.",
        ("NG-C005",),
    ),
    Rule(
        "E003",
        Severity.ERROR,
        "The same MAC address is used by two interfaces in the inventory.",
        ("NG-I008",),
    ),
    Rule(
        "E004",
        Severity.ERROR,
        "The same IP address is assigned twice within one subnet and VLAN.",
        ("NG-A004",),
    ),
    Rule(
        "E005",
        Severity.ERROR,
        "The two ends of a link disagree about VLANs, so it carries less than it seems.",
        ("NG-C011",),
    ),
    Rule(
        "E006",
        Severity.ERROR,
        "An adapter declares more downstream interfaces than it has ports.",
        ("NG-X008",),
    ),
    Rule(
        "E007",
        Severity.ERROR,
        "Interface stacking through 'parent'/'members' contains a cycle.",
        ("NG-I004",),
    ),
    Rule(
        "E008",
        Severity.ERROR,
        "A lag/bridge member is itself aggregated or carries a sub-interface.",
        ("NG-I005",),
    ),
    Rule(
        "E009",
        Severity.ERROR,
        "A 'vlan' sub-interface's VID is not carried by its parent interface.",
        ("NG-V005",),
    ),
    Rule(
        "E010",
        Severity.ERROR,
        "A MAC address has the multicast bit set, so no interface can own it.",
        ("NG-I009",),
    ),
    Rule(
        "E011",
        Severity.ERROR,
        "A cable's medium disagrees with the radio/wired type of an endpoint.",
        ("NG-C006",),
    ),
    Rule(
        "E012",
        Severity.ERROR,
        "A cable endpoint is a loopback, vlan or bridge interface.",
        ("NG-C009",),
    ),
    Rule(
        "E013",
        Severity.ERROR,
        "A cable lands on an adapter's upstream port that 'attached_to' claims.",
        ("NG-X005",),
    ),
    Rule(
        "E014",
        Severity.ERROR,
        "Adapter 'attached_to' attachments form a cycle.",
        ("NG-X006",),
    ),
    Rule(
        "E015",
        Severity.ERROR,
        "An adapter's 'attached_to' names no element that could host it.",
        ("NG-X001",),
    ),
    Rule(
        "E016",
        Severity.ERROR,
        "A tunnel endpoint references an unknown element or interface.",
        ("NG-T002",),
    ),
    Rule(
        "E017",
        Severity.ERROR,
        "A tunnel endpoint is not an interface of type 'tunnel'.",
        ("NG-T003",),
    ),
    Rule(
        "E018",
        Severity.ERROR,
        "A tunnel's 'over' names no tunnel of this inventory.",
        ("NG-T004",),
    ),
    Rule(
        "E019",
        Severity.ERROR,
        "Tunnel 'over' references form a cycle, so nothing reaches the underlay.",
        ("NG-T005",),
    ),
    Rule(
        "W101",
        Severity.WARNING,
        "An interface has neither IPv4 nor IPv6 and is not a switchport.",
        ("NG-I013",),
    ),
    Rule(
        "W102",
        Severity.WARNING,
        "The two endpoints of a cable disagree about the MTU.",
        ("NG-C010",),
    ),
    Rule(
        "W103",
        Severity.WARNING,
        "A device terminates no cable and hosts no adapter: an orphan node.",
        ("NG-C016",),
    ),
    Rule(
        "W104",
        Severity.WARNING,
        "An access port of a layer-2-only switch carries an IP address.",
        ("NG-V009",),
    ),
    Rule(
        "W105",
        Severity.WARNING,
        "A subnet holds exactly one element, so its prefix length may be wrong.",
        ("NG-A008",),
    ),
    Rule(
        "W106",
        Severity.WARNING,
        "Two elements claim the same address in one subnet, in different VLANs.",
        ("NG-A009",),
    ),
    Rule(
        "W107",
        Severity.WARNING,
        "A lag/bridge member carries its own IPv4 or IPv6 addresses.",
        ("NG-I006",),
    ),
    Rule(
        "W108",
        Severity.WARNING,
        "A loopback interface declares a MAC address.",
        ("NG-I007",),
    ),
    Rule(
        "W109",
        Severity.WARNING,
        "A device declares no ethernet, wifi or lag interface, so it cannot be cabled.",
        ("NG-I012",),
    ),
    Rule(
        "W110",
        Severity.WARNING,
        "An address is the network or broadcast address of its own prefix.",
        ("NG-A005",),
    ),
    Rule(
        "W111",
        Severity.WARNING,
        "Two interfaces on one element hold overlapping prefixes.",
        ("NG-A006",),
    ),
    Rule(
        "W112",
        Severity.WARNING,
        "A loopback interface carries a prefix other than /32 or /128.",
        ("NG-A007",),
    ),
    Rule(
        "W113",
        Severity.WARNING,
        "A port references a VLAN the device's 'vlans' database does not declare.",
        ("NG-V004",),
    ),
    Rule(
        "W114",
        Severity.WARNING,
        "A trunk's 'native_vlan' is not listed in its 'trunk_vlans'.",
        ("NG-V006",),
    ),
    Rule(
        "W115",
        Severity.WARNING,
        "A port trunking every VLAN faces a host rather than another switch.",
        ("NG-V007",),
    ),
    Rule(
        "W116",
        Severity.WARNING,
        "A lag member declares a 'vlan' block that differs from the aggregate's.",
        ("NG-V008",),
    ),
    Rule(
        "W117",
        Severity.WARNING,
        "Both endpoints of one cable land on the same element.",
        ("NG-C004",),
    ),
    Rule(
        "W118",
        Severity.WARNING,
        "A cable's 'speed' disagrees with the speed an endpoint declares.",
        ("NG-C008",),
    ),
    Rule(
        "W119",
        Severity.WARNING,
        "A cable endpoint is a lag aggregate rather than one of its members.",
        ("NG-C012",),
    ),
    Rule(
        "W120",
        Severity.WARNING,
        "A cable is 'duplex: half' on a link that involves no hub.",
        ("NG-C013",),
    ),
    Rule(
        "W121",
        Severity.WARNING,
        "The topology graph is disconnected: it falls into separate islands.",
        ("NG-C014",),
    ),
    Rule(
        "W122",
        Severity.WARNING,
        "Two elements on one hub are addressed in different subnets.",
        ("NG-H005",),
    ),
    Rule(
        "W123",
        Severity.WARNING,
        "An adapter has cabled downstream ports but no 'attached_to' host.",
        ("NG-X002",),
    ),
    Rule(
        "W124",
        Severity.WARNING,
        "An adapter's 'attached_to' points at a hub or a switch, not a host.",
        ("NG-X007",),
    ),
    Rule(
        "W125",
        Severity.WARNING,
        "An overlay terminates where its underlay tunnel does not reach.",
        ("NG-T006",),
    ),
    Rule(
        "W126",
        Severity.WARNING,
        "A tunnel's MTU does not fit inside its underlay after encapsulation.",
        ("NG-T011",),
    ),
    Rule(
        "W127",
        Severity.WARNING,
        "A tunnel encrypts nothing and no tunnel it runs inside does either.",
        ("NG-T012",),
    ),
    Rule(
        "W128",
        Severity.WARNING,
        "A 'tunnel' interface is named by no tunnel document.",
        ("NG-T013",),
    ),
    Rule(
        "W129",
        Severity.WARNING,
        "Two tunnels terminating on one element use the same VNI.",
        ("NG-T014",),
    ),
    Rule(
        "I001",
        Severity.INFO,
        "A MAC address is locally administered rather than vendor-assigned.",
        ("NG-I010",),
    ),
    Rule(
        "I002",
        Severity.INFO,
        "An interface is enabled but terminates no cable.",
        ("NG-C015",),
    ),
    Rule(
        "I003",
        Severity.INFO,
        "A tunnel listens on a port other than the registered one for its type.",
        ("NG-T015",),
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
