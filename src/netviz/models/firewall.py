"""Firewall configuration: zones, filter policy and NAT (§24).

Everything here hangs off a device's ``spec``, for the same reason
:mod:`netviz.models.routing` does: a firewall rule is *state of a box*. It is
written on one machine, it is walked by that machine, and no other machine can
see it. That is also why filtering is not a device kind here but a *function* of
one — a ``kind: firewall`` exists (§6) because operators buy boxes that do
nothing else, but a router with three rules on it filters just as truly, and a
schema that could only describe the box would be unable to describe the network.

Three blocks, in the order a device declares them:

``spec.zones``
    The security zones the device divides its interfaces into. A zone is a name
    and a set of interfaces, and it exists so that policy can be written about
    *where traffic comes from* rather than about which cable it arrived on —
    which is what makes a rule survive an interface being renamed, doubled or
    moved to a LAG.
``spec.firewall.rules``
    The filter policy: an ordered list walked from the lowest ``priority``
    upwards, first match deciding, exactly like the routing policy database of
    §16.4 and for the same reason — that is how every implementation implements
    it and any other reading would make the same document mean two things.
``spec.firewall.nat``
    Address translation: what is rewritten on the way out (``snat``,
    ``masquerade``) and on the way in (``dnat``, ``redirect``). Kept apart from
    the filter rules because it *is* apart: NAT happens in different hooks, at
    different times, and a packet is translated and filtered rather than
    translated or filtered.

Zones, and the one that is not declared
---------------------------------------

Every interface is in at most one zone (``NV-B003``). That is not a
simplification: it is the defining property of a zone in every zone-based
firewall there is, and it is what makes ``from lan to wan`` a statement about a
packet rather than a question. An interface in no zone is not an error — plenty
of interfaces carry no transit traffic — but on a device that declares zones at
all it is worth a second look, which is ``W151``.

:data:`LOCAL_ZONE` is the device itself: the traffic that terminates on the box
rather than passing through it. It is nameable without being declared and cannot
be declared (``NV-B001``), because the machine is not one of the things the
machine's interfaces are divided into. It is also what turns the two zone fields
into a hook: ``to: local`` is the input hook, ``from: local`` is output, and two
real zones are forward. So the schema never asks which chain a rule is in — the
zones already said.

The mark, and where it goes
---------------------------

``action: mark`` is the join between this section and §16.4. Policy-based
routing deliberately has no layer-4 selector; the portable way to route by port,
by user or by application is to mark the packet in the firewall and match
``fwmark`` in the routing policy database. This is the half that marks. The two
halves are checked against each other — a mark written that nothing reads is
``W152``, a mark read that nothing writes is ``W153`` — because each is silent
on its own and wrong only together.

**No secrets, ever.** As with :mod:`netviz.models.tunnel`, there is nowhere to
put a pre-shared key, a RADIUS secret or a certificate: a secret in an inventory
is a secret in version control.
"""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from typing import Annotated, Any, Final

from pydantic import BeforeValidator, Field, model_validator

from netviz.errors import echo_value
from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.routing import AddressFamily, Fwmark, IPPrefix, RulePriority
from netviz.models.scalars import Boolean, ElementName, IfName

__all__ = [
    "LOCAL_ZONE",
    "PORT_MAX",
    "PORT_RANGE_PATTERN",
    "ConnState",
    "FirewallAction",
    "FirewallConfig",
    "FirewallHook",
    "FirewallRule",
    "NatRule",
    "NatType",
    "Port",
    "PortRange",
    "Protocol",
    "Zone",
    "normalise_port_range",
]

#: The zone a packet is in when it is *for this machine* rather than passing
#: through it. Nameable in ``src_zone`` and ``dst_zone`` without being declared,
#: and refused as a declared zone name (``NV-B001``): the box is not one of the
#: parts the box's interfaces are divided into.
LOCAL_ZONE: Final = "local"

#: Largest transport port number: a ``u16``.
PORT_MAX: Final = 65535

#: ``spec.firewall.nat[].to_port`` and the halves of a port range.
Port = Annotated[int, Field(strict=True, ge=1, le=PORT_MAX)]

#: The canonical form of a port selector: one port, or a closed range. Written
#: as one ECMA-262 pattern so the JSON Schema carries it verbatim, the same
#: reason :data:`~netviz.models.routing.FWMARK_PATTERN` is.
PORT_RANGE_PATTERN: Final = r"^[1-9][0-9]{0,4}(?:-[1-9][0-9]{0,4})?$"

_PORT_RANGE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


def normalise_port_range(value: Any) -> Any:
    """Normalise a port selector to ``443`` or ``1000-2000`` (``NV-B005``).

    An integer and the string spelling it are the same selector and are stored
    the same way, because two documents that write one port differently have to
    compare equal or a diff between them reports a change nobody made.

    A range whose end is below its start is refused rather than reordered: the
    two readings of ``2000-1000`` — "somebody typed it backwards" and "somebody
    meant a different range" — are equally likely, and silently picking one
    would mean the document says something the author did not write.
    """
    if isinstance(value, bool):
        raise ValueError("expected a port or a port range, got a boolean")
    if isinstance(value, int):
        _check_port(value, echo=str(value))
        return str(value)
    if not isinstance(value, str):
        raise ValueError(f"expected a port or a port range, got {type(value).__name__}")

    match = _PORT_RANGE_RE.match(value)
    if match is None:
        raise ValueError(
            f"{echo_value(value)} is not a port or a port range; expected a number or "
            "'<low>-<high>' (for example '443' or '30000-32767')"
        )
    low = int(match.group(1))
    _check_port(low, echo=echo_value(value))
    if match.group(2) is None:
        return str(low)
    high = int(match.group(2))
    _check_port(high, echo=echo_value(value))
    if high < low:
        raise ValueError(
            f"{echo_value(value)} ends below where it starts; a port range is written "
            f"low first, so this is either {low}-{low} or {high}-{low}"
        )
    return f"{low}" if low == high else f"{low}-{high}"


def _check_port(number: int, *, echo: str) -> None:
    """Refuse a port outside 1-65535, naming the value the document wrote."""
    if not 1 <= number <= PORT_MAX:
        raise ValueError(
            f"{echo}: {number} is not a port; a port is a 16-bit number, so 1 to {PORT_MAX}"
        )


#: ``spec.firewall.rules[].src_ports`` and ``[].dst_ports`` — one port or a
#: closed range, canonically without whitespace.
PortRange = Annotated[str, BeforeValidator(normalise_port_range), Field(pattern=PORT_RANGE_PATTERN)]


class FirewallHook(str, Enum):
    """Where in the packet's journey through the machine a rule is consulted.

    Never written in a document: it is *derived* from the two zone fields, since
    naming both a hook and the zones would let one document say the packet both
    terminates here and passes through. See :attr:`FirewallRule.hooks`.
    """

    #: The packet is for this machine.
    INPUT = "input"
    #: The packet is passing through it.
    FORWARD = "forward"
    #: The packet was generated by it.
    OUTPUT = "output"


#: Every hook, in the order a packet could meet them.
_ALL_HOOKS: Final[tuple[FirewallHook, ...]] = (
    FirewallHook.INPUT,
    FirewallHook.FORWARD,
    FirewallHook.OUTPUT,
)

#: Both families, in the order everything here reports them.
_BOTH_FAMILIES: Final[tuple[AddressFamily, ...]] = (AddressFamily.IPV4, AddressFamily.IPV6)


class FirewallAction(str, Enum):
    """What happens to a packet a filter rule matches.

    Three of the five *decide* the packet's fate and end the walk; two of them
    do something to it and let the walk continue. That split is the whole reason
    the list is not simply "accept or drop": a rule that logs and a rule that
    marks are useful precisely because the packet carries on to the rule that
    decides, and a schema in which every action terminated could express neither.
    """

    #: Let the packet through. Terminal.
    ACCEPT = "accept"
    #: Discard it silently. Terminal.
    DROP = "drop"
    #: Discard it and say so — ICMP unreachable, or a TCP reset. Terminal.
    REJECT = "reject"
    #: Write ``mark`` on the packet and carry on walking. This is what §16.4's
    #: ``fwmark`` selector reads.
    MARK = "mark"
    #: Record it and carry on walking.
    LOG = "log"

    @property
    def is_terminal(self) -> bool:
        """Does the walk stop here, the packet's fate decided?"""
        return self in (FirewallAction.ACCEPT, FirewallAction.DROP, FirewallAction.REJECT)

    @property
    def permits(self) -> bool:
        """Does the action let the packet through?"""
        return self is FirewallAction.ACCEPT

    @property
    def denies(self) -> bool:
        """Does the action discard the packet?"""
        return self in (FirewallAction.DROP, FirewallAction.REJECT)


class Protocol(str, Enum):
    """The IP protocol a rule selects on.

    A closed set rather than a number, because the eight here are the ones a
    policy is ever written about and a bare protocol number in a firewall rule is
    almost always a typo for one of them. ``icmp`` and ``icmpv6`` are separate
    protocols carried by separate families, which is a fact the schema can check
    (``NV-B005``) only because they are not one entry called "icmp".
    """

    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"
    ICMPV6 = "icmpv6"
    SCTP = "sctp"
    ESP = "esp"
    AH = "ah"
    GRE = "gre"

    @property
    def has_ports(self) -> bool:
        """Does this protocol have a port number to select on?"""
        return self in (Protocol.TCP, Protocol.UDP, Protocol.SCTP)

    @property
    def family(self) -> AddressFamily | None:
        """The family this protocol only exists in, if it is only in one."""
        if self is Protocol.ICMP:
            return AddressFamily.IPV4
        if self is Protocol.ICMPV6:
            return AddressFamily.IPV6
        return None


class ConnState(str, Enum):
    """A connection-tracking state (``ct state`` / ``conntrack --ctstate``).

    The reason a modern ruleset is short: one rule accepting ``established`` and
    ``related`` replaces the return path of every other rule in the file, and a
    document that could not say so would have to describe both directions of
    every flow.
    """

    NEW = "new"
    ESTABLISHED = "established"
    RELATED = "related"
    INVALID = "invalid"


class NatType(str, Enum):
    """Which address a NAT rule rewrites, and on which side of the machine."""

    #: Rewrite the source to a stated address, on the way out.
    SNAT = "snat"
    #: Rewrite the source to whatever the egress interface has, on the way out.
    #: The address is not stated because it is not known until the packet leaves.
    MASQUERADE = "masquerade"
    #: Rewrite the destination to a stated address, on the way in.
    DNAT = "dnat"
    #: Rewrite the destination to this machine, on the way in.
    REDIRECT = "redirect"

    @property
    def is_source(self) -> bool:
        """Does it rewrite the source address rather than the destination?"""
        return self in (NatType.SNAT, NatType.MASQUERADE)

    @property
    def needs_address(self) -> bool:
        """Does the rule have to state the address it translates to?"""
        return self in (NatType.SNAT, NatType.DNAT)


# --------------------------------------------------------------------------- #
# The blocks themselves
# --------------------------------------------------------------------------- #


class Zone(NetvizModel):
    """One entry of ``spec.zones`` — a security zone (§24.1).

    A name and the interfaces in it. The interfaces are what make the zone a
    fact about the device rather than a label: policy is written between zones,
    and which zone a packet is in is decided by the interface it crossed.

    An interface belongs to at most one zone (``NV-B003``), and a zone holding no
    interface at all is inert (``W150``) — nothing can be in it, so no rule
    naming it can ever match.
    """

    name: ElementName
    #: ``NV-B002``: each names an interface of this device. ``NV-B003``: no
    #: interface is in two zones.
    interfaces: list[IfName] = Field(default_factory=list)
    description: str | None = None

    def describe(self) -> str:
        """``dmz (eth2, eth3)`` — one phrase, for a diagnostic or a label."""
        if not self.interfaces:
            return f"{self.name} (no interface)"
        return f"{self.name} ({', '.join(self.interfaces)})"


class FirewallRule(NetvizModel):
    """One entry of ``spec.firewall.rules`` — a filter rule (§24.2).

    Read it as a sentence: *at priority 100, a TCP packet from the lan zone to
    this machine's port 22 is accepted*. The zones and the selectors are the
    subject, the action is the verb, and ``priority`` is where in the queue the
    sentence is read — lowest first, first terminal match wins.

    A rule that names no zone and no selector matches every packet, which is not
    a mistake: it is how a chain is terminated. It is also how a chain is broken,
    by numbering a rule above the terminator instead of below it, which is
    ``W154``.
    """

    #: ``NV-B008``: unique within the device, per family. The chain is walked
    #: from the lowest upwards, and a tie would leave the document unable to say
    #: which of two rules decides.
    priority: RulePriority
    #: Optional label, for the diagram and for a diagnostic to name the rule by.
    name: ElementName | None = None
    #: The zone the packet came from: a declared zone, or :data:`LOCAL_ZONE` for
    #: traffic this machine generated. Unset matches any zone (``NV-B004``).
    src_zone: ElementName | None = None
    #: The zone the packet is going to, on the same terms (``NV-B004``).
    dst_zone: ElementName | None = None
    #: Which family's chain the rule is in. Derived from ``src``, ``dst`` and
    #: ``protocol`` when they say so, and both families when nothing does.
    family: AddressFamily | None = None
    #: Source prefix.
    src: IPPrefix | None = None
    #: Destination prefix.
    dst: IPPrefix | None = None
    #: The IP protocol. Required by ``src_ports`` and ``dst_ports``, which no
    #: other protocol has (``NV-B005``).
    protocol: Protocol | None = None
    #: Source ports: single ports and closed ranges, matched as a set.
    src_ports: list[PortRange] = Field(default_factory=list)
    #: Destination ports, on the same terms. The usual selector, since it is the
    #: one that names the service.
    dst_ports: list[PortRange] = Field(default_factory=list)
    #: Connection-tracking states, matched as a set. Empty matches any state.
    ct_state: list[ConnState] = Field(default_factory=list)
    #: The ingress interface, when a zone is too coarse (``NV-B009``). Rare: the
    #: point of a zone is not needing this.
    iif: IfName | None = None
    #: The egress interface, on the same terms (``NV-B009``).
    oif: IfName | None = None
    #: Match everything the selectors do *not*. Meaningless without a selector
    #: to invert, which is ``NV-B005``.
    invert: Boolean = False
    #: Stated, never defaulted: a rule whose action nobody wrote down is a rule
    #: nobody finished writing, and guessing at it would be guessing about
    #: whether traffic flows.
    action: FirewallAction
    #: The mark to write, required by ``action: mark`` and refused by everything
    #: else (``NV-B005``). Read by ``spec.routing_policy[].fwmark`` (§16.4).
    mark: Fwmark | None = None
    #: The tag put in front of a logged packet, for ``action: log`` only.
    log_prefix: str | None = Field(default=None, max_length=64)
    description: str | None = None

    @model_validator(mode="after")
    def _check_action(self) -> FirewallRule:
        """``NV-B005``: the action and the thing it acts on agree."""
        if self.action is FirewallAction.MARK and self.mark is None:
            raise field_error(
                "a 'mark' rule needs the mark to write; name one in 'mark', or choose an "
                "action that decides the packet ('accept', 'drop', 'reject')",
                rule="NV-B005",
                path=("mark",),
            )
        if self.action is not FirewallAction.MARK and self.mark is not None:
            raise field_error(
                f"a {self.action.value!r} rule does not write a mark, so it must not name one",
                rule="NV-B005",
                path=("mark",),
            )
        if self.action is not FirewallAction.LOG and self.log_prefix is not None:
            raise field_error(
                f"'log_prefix' is the tag on a logged packet; this rule is "
                f"{self.action.value!r} and logs nothing",
                rule="NV-B005",
                path=("log_prefix",),
            )
        return self

    @model_validator(mode="after")
    def _check_selectors(self) -> FirewallRule:
        """``NV-B005``: the selectors agree with each other and with the family."""
        if self.src is not None and self.dst is not None and self.src.version != self.dst.version:
            raise field_error(
                f"'src' is {self.src} and 'dst' is {self.dst}; one rule matches one address "
                f"family, so the two prefixes cannot be IPv{self.src.version} and "
                f"IPv{self.dst.version}",
                rule="NV-B005",
                path=("dst",),
            )
        if self.family is not None:
            for key in ("src", "dst"):
                prefix: IPPrefix | None = getattr(self, key)
                if prefix is not None and AddressFamily.of(prefix.version) is not self.family:
                    raise field_error(
                        f"the rule is declared {self.family.value} but {key!r} is {prefix}, "
                        f"which is IPv{prefix.version}",
                        rule="NV-B005",
                        path=(key,),
                    )
        self._check_protocol()
        self._check_ports()
        if self.invert and not self.selectors:
            raise field_error(
                "'invert' matches everything the selectors do not, and this rule has no "
                "selector to invert, so it would match nothing at all",
                rule="NV-B005",
                path=("invert",),
            )
        if self.src_zone is not None and self.src_zone == self.dst_zone:
            raise field_error(
                f"the rule is from {self.src_zone!r} to {self.src_zone!r}; traffic that "
                f"stays inside one zone does not cross the firewall, so name two zones or "
                f"leave one unset",
                rule="NV-B004",
                path=("dst_zone",),
            )
        return self

    def _check_protocol(self) -> None:
        """``NV-B005``: a protocol that lives in one family, against the rule's."""
        if self.protocol is None:
            return
        only = self.protocol.family
        if only is None:
            return
        for key in ("family", "src", "dst"):
            value = getattr(self, key)
            if value is None:
                continue
            stated = value if key == "family" else AddressFamily.of(value.version)
            if stated is not only:
                raise field_error(
                    f"{self.protocol.value!r} is carried by {only.value} only, and the rule "
                    f"is {stated.value} by its {key!r}",
                    rule="NV-B005",
                    path=("protocol",),
                )

    def _check_ports(self) -> None:
        """``NV-B005``: ports belong to a protocol that has them, and repeat never."""
        for key in ("src_ports", "dst_ports"):
            ports: list[str] = getattr(self, key)
            if not ports:
                continue
            if self.protocol is None:
                raise field_error(
                    f"{key!r} selects on a port and the rule states no 'protocol'; a port "
                    f"number means nothing without one — name 'tcp', 'udp' or 'sctp'",
                    rule="NV-B005",
                    path=(key,),
                )
            if not self.protocol.has_ports:
                raise field_error(
                    f"{key!r} selects on a port and {self.protocol.value!r} has none; ports "
                    f"belong to 'tcp', 'udp' and 'sctp'",
                    rule="NV-B005",
                    path=(key,),
                )
            for index, port in enumerate(ports):
                if port in ports[:index]:
                    raise field_error(
                        f"port selector {port!r} is listed twice; the list is matched as a "
                        f"set, so the repeat adds nothing",
                        rule="NV-B005",
                        path=(key, index),
                    )
        for index, state in enumerate(self.ct_state):
            if state in self.ct_state[:index]:
                raise field_error(
                    f"connection state {state.value!r} is listed twice",
                    rule="NV-B005",
                    path=("ct_state", index),
                )

    @property
    def zones(self) -> tuple[str, ...]:
        """The zone names this rule refers to, in the order it states them."""
        return tuple(name for name in (self.src_zone, self.dst_zone) if name is not None)

    @property
    def hooks(self) -> tuple[FirewallHook, ...]:
        """Which hooks this rule is installed in, derived from its zones (§24.2).

        ``to: local`` is the input hook and ``from: local`` is output, because
        that is what those zones *mean*: a packet for this machine and a packet
        from it. Two real zones are forward. A rule that names one real zone and
        leaves the other unset could be either — it is a packet arriving from the
        lan, and whether it stops here is not something the rule says — so it is
        installed in both, exactly as an unstated family installs in both.
        """
        if self.dst_zone == LOCAL_ZONE:
            return (FirewallHook.INPUT,)
        if self.src_zone == LOCAL_ZONE:
            return (FirewallHook.OUTPUT,)
        if self.src_zone is not None and self.dst_zone is not None:
            return (FirewallHook.FORWARD,)
        if self.src_zone is not None:
            return (FirewallHook.FORWARD, FirewallHook.INPUT)
        if self.dst_zone is not None:
            return (FirewallHook.FORWARD, FirewallHook.OUTPUT)
        return _ALL_HOOKS

    @property
    def families(self) -> tuple[AddressFamily, ...]:
        """The families this rule is installed in, IPv4 first."""
        if self.family is not None:
            return (self.family,)
        if self.protocol is not None and (only := self.protocol.family) is not None:
            return (only,)
        for prefix in (self.src, self.dst):
            if prefix is not None:
                return (AddressFamily.of(prefix.version),)
        return _BOTH_FAMILIES

    @property
    def selectors(self) -> tuple[str, ...]:
        """The selectors, rendered, in the order :meth:`describe` reads them.

        The zones are not among them. They say *where* the rule is consulted
        rather than which packets it picks out of what arrives there, and a
        catch-all in one zone pair is still a catch-all — which is what
        :attr:`is_catch_all` and ``W154`` are about.
        """
        parts: list[str] = []
        if self.src is not None:
            parts.append(f"src {self.src}")
        if self.dst is not None:
            parts.append(f"dst {self.dst}")
        if self.iif is not None:
            parts.append(f"iif {self.iif}")
        if self.oif is not None:
            parts.append(f"oif {self.oif}")
        if self.protocol is not None:
            parts.append(self.protocol.value)
        if self.src_ports:
            parts.append(f"sport {','.join(self.src_ports)}")
        if self.dst_ports:
            parts.append(f"dport {','.join(self.dst_ports)}")
        if self.ct_state:
            parts.append(f"ct {','.join(state.value for state in self.ct_state)}")
        return tuple(parts)

    @property
    def is_catch_all(self) -> bool:
        """Does the rule match every packet reaching the hooks it is in?"""
        return not self.selectors and not self.invert

    @property
    def target(self) -> str:
        """The action, with whatever it acts on: ``mark 0x1``, ``accept``."""
        if self.action is FirewallAction.MARK:
            return f"mark {self.mark}"
        return str(self.action.value)

    @property
    def flow(self) -> str:
        """``lan -> wan`` — the zone pair, with ``any`` for whichever is unset."""
        return f"{self.src_zone or 'any'} -> {self.dst_zone or 'any'}"

    def describe(self) -> str:
        """``100: lan -> wan tcp dport 443 accept`` — one line, for a diagnostic."""
        selectors = list(self.selectors)
        if self.invert:
            selectors.insert(0, "not")
        return " ".join([f"{self.priority}:", self.flow, *selectors, self.target])

    def matches_family(self, family: AddressFamily) -> bool:
        """Is this rule installed in ``family``'s chain?"""
        return family in self.families

    def in_hook(self, hook: FirewallHook) -> bool:
        """Is this rule installed in ``hook``?"""
        return hook in self.hooks


class NatRule(NetvizModel):
    """One entry of ``spec.firewall.nat`` — an address translation (§24.4).

    Apart from the filter rules because it happens apart from them: a packet is
    translated *and* filtered, in different hooks, and a list that mixed the two
    would have to answer which happened first for every pair of entries in it.

    Order within the list is the order the translations are tried, first match
    winning, which is what every implementation does. There is no ``priority``:
    a NAT list is short enough that its own order is readable, and a number that
    only ever repeated the position would be one more thing to keep in step.
    """

    name: ElementName | None = None
    type: NatType
    #: The zone the packet came from (``NV-B004``). The usual selector for a
    #: source translation: *everything leaving towards the wan is masqueraded*.
    src_zone: ElementName | None = None
    #: The zone it is going to (``NV-B004``).
    dst_zone: ElementName | None = None
    family: AddressFamily | None = None
    src: IPPrefix | None = None
    dst: IPPrefix | None = None
    protocol: Protocol | None = None
    #: Destination ports, for a translation that picks one service out of an
    #: address (``NV-B006``). The published port, not the internal one.
    dst_ports: list[PortRange] = Field(default_factory=list)
    #: What the address becomes: required by ``snat`` and ``dnat``, refused by
    #: ``masquerade`` (whose address is the egress interface's, unknown here)
    #: and by ``redirect`` (whose address is this machine) — ``NV-B006``. An
    #: address rather than a prefix, because a translation rewrites to one.
    to_address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    #: What the port becomes. Optional everywhere but ``redirect``, which is a
    #: translation of the port and nothing else (``NV-B006``).
    to_port: Port | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _check_translation(self) -> NatRule:
        """``NV-B006``: the translation states what its type needs and no more."""
        if self.type.needs_address and self.to_address is None:
            raise field_error(
                f"a {self.type.value!r} rule rewrites the "
                f"{'source' if self.type.is_source else 'destination'} to a stated address; "
                f"name one in 'to_address'",
                rule="NV-B006",
                path=("to_address",),
            )
        if not self.type.needs_address and self.to_address is not None:
            reason = (
                "the egress interface's address, which is not known until the packet leaves"
                if self.type is NatType.MASQUERADE
                else "this machine, which is what 'redirect' means"
            )
            raise field_error(
                f"a {self.type.value!r} rule translates to {reason}, so it must not name "
                f"'to_address'",
                rule="NV-B006",
                path=("to_address",),
            )
        if self.type is NatType.REDIRECT and self.to_port is None:
            raise field_error(
                "a 'redirect' rule translates the port and nothing else; name one in 'to_port'",
                rule="NV-B006",
                path=("to_port",),
            )
        self._check_families()
        if self.dst_ports and (self.protocol is None or not self.protocol.has_ports):
            named = "no 'protocol'" if self.protocol is None else f"{self.protocol.value!r}"
            raise field_error(
                f"'dst_ports' selects on a port and the rule states {named}; ports belong "
                f"to 'tcp', 'udp' and 'sctp'",
                rule="NV-B006",
                path=("dst_ports",),
            )
        if self.to_port is not None and self.type.is_source and not self.dst_ports:
            raise field_error(
                f"a {self.type.value!r} rule with a 'to_port' rewrites the source port of "
                f"every packet it matches; that is a port range, not a port, and netviz "
                f"has no way to say which — leave 'to_port' unset",
                rule="NV-B006",
                path=("to_port",),
            )
        return self

    def _check_families(self) -> None:
        """``NV-B006``: every address the rule names is of one family."""
        stated: list[tuple[str, AddressFamily]] = []
        if self.family is not None:
            stated.append(("family", self.family))
        for key in ("src", "dst", "to_address"):
            value = getattr(self, key)
            if value is not None:
                stated.append((key, AddressFamily.of(value.version)))
        for key, family in stated[1:]:
            if family is not stated[0][1]:
                raise field_error(
                    f"{key!r} is {family.value} and {stated[0][0]!r} is {stated[0][1].value}; "
                    f"one translation rewrites one address family",
                    rule="NV-B006",
                    path=(key,),
                )

    @property
    def zones(self) -> tuple[str, ...]:
        """The zone names this rule refers to, in the order it states them."""
        return tuple(name for name in (self.src_zone, self.dst_zone) if name is not None)

    @property
    def families(self) -> tuple[AddressFamily, ...]:
        """The families this translation is installed in, IPv4 first."""
        if self.family is not None:
            return (self.family,)
        for value in (self.src, self.dst, self.to_address):
            if value is not None:
                return (AddressFamily.of(value.version),)
        return _BOTH_FAMILIES

    @property
    def target(self) -> str:
        """``to 10.0.0.5:8443``, ``to :8080``, or nothing for a masquerade."""
        address = "" if self.to_address is None else str(self.to_address)
        port = "" if self.to_port is None else f":{self.to_port}"
        return f"to {address}{port}" if address or port else ""

    def describe(self) -> str:
        """``dnat lan -> local tcp dport 443 to 10.0.0.5:8443`` — one line."""
        parts = [self.type.value, f"{self.src_zone or 'any'} -> {self.dst_zone or 'any'}"]
        if self.src is not None:
            parts.append(f"src {self.src}")
        if self.dst is not None:
            parts.append(f"dst {self.dst}")
        if self.protocol is not None:
            parts.append(self.protocol.value)
        if self.dst_ports:
            parts.append(f"dport {','.join(self.dst_ports)}")
        if target := self.target:
            parts.append(target)
        return " ".join(parts)


class FirewallConfig(NetvizModel):
    """``spec.firewall`` — what the device does to the packets it sees (§24.2).

    The three defaults are what happens to a packet no rule decided, one per
    hook, and they are stated rather than left implicit because they are the most
    consequential three words in any ruleset. They default to *deny inbound,
    deny transit, permit outbound*: the shape every firewall guide has recommended
    for thirty years, and the one whose failure mode is a service that does not
    work rather than a network that is open.
    """

    #: What a packet *for this machine* gets when no rule decides.
    default_input: FirewallAction = FirewallAction.DROP
    #: What a packet *through* it gets when no rule decides.
    default_forward: FirewallAction = FirewallAction.DROP
    #: What a packet *from* it gets when no rule decides. Permitted, because a
    #: machine that cannot answer a DNS query cannot be administered either.
    default_output: FirewallAction = FirewallAction.ACCEPT
    #: The filter policy, in declaration order. What the device walks is
    #: :meth:`rules_in`, which is this in priority order.
    rules: list[FirewallRule] = Field(default_factory=list)
    #: The address translations, in the order they are tried.
    nat: list[NatRule] = Field(default_factory=list)
    description: str | None = None

    @model_validator(mode="after")
    def _check_defaults(self) -> FirewallConfig:
        """``NV-B007``: a default decides the packet, so it is one of the three."""
        for key in ("default_input", "default_forward", "default_output"):
            action: FirewallAction = getattr(self, key)
            if not action.is_terminal:
                raise field_error(
                    f"{key!r} is {action.value!r}, which does not decide the packet — the "
                    f"walk would carry on past the end of the chain, and there is nothing "
                    f"there; a default is 'accept', 'drop' or 'reject'",
                    rule="NV-B007",
                    path=(key,),
                )
        return self

    def default_for(self, hook: FirewallHook) -> FirewallAction:
        """What a packet no rule decided gets in ``hook``."""
        if hook is FirewallHook.INPUT:
            return self.default_input
        if hook is FirewallHook.OUTPUT:
            return self.default_output
        return self.default_forward

    def rules_in(
        self, hook: FirewallHook | None = None, family: AddressFamily | None = None
    ) -> tuple[FirewallRule, ...]:
        """The chain, in the order it is walked: by priority, lowest first.

        Sorted rather than kept in declaration order, because that *is* the
        chain: two rules in the document are a set, and the order they are
        consulted in is the number on them. Ties cannot happen within one family
        (``NV-B008``), so the sort is total and the result is what the device
        would do.
        """
        selected = [
            rule
            for rule in self.rules
            if (hook is None or rule.in_hook(hook))
            and (family is None or rule.matches_family(family))
        ]
        return tuple(sorted(selected, key=lambda rule: rule.priority))

    def marks(self) -> tuple[str, ...]:
        """Every mark the policy writes, once each, in priority order (§16.4)."""
        written = [rule.mark for rule in self.rules_in() if rule.mark is not None]
        return tuple(dict.fromkeys(written))

    def zones_named(self) -> tuple[str, ...]:
        """Every zone name the policy refers to, once each, in the order stated."""
        names: list[str] = []
        entries: tuple[FirewallRule | NatRule, ...] = (*self.rules, *self.nat)
        for rule in entries:
            names.extend(rule.zones)
        return tuple(dict.fromkeys(names))

    @property
    def is_default_deny(self) -> bool:
        """Does a packet nothing decided get dropped in both transit hooks?"""
        return self.default_input.denies and self.default_forward.denies

    def describe(self) -> str:
        """``12 rules, 2 translations, default drop/drop/accept`` — one phrase."""
        counts = f"{len(self.rules)} rule(s), {len(self.nat)} translation(s)"
        defaults = "/".join(
            action.value
            for action in (self.default_input, self.default_forward, self.default_output)
        )
        return f"{counts}, default {defaults}"
