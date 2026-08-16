"""Routing configuration: VRFs, tables, policy and protocol adjacencies (§16).

Everything here hangs off a device's ``spec``, because routing is *state of a
box* rather than a thing between boxes: a route is written on one device, an
adjacency is configured on one device towards a neighbour it names by address.
That is the shape RFC 8349 (``ietf-routing``) gives it too — a control-plane
protocol and a routing table live inside a network instance, which is what a VRF
is (RFC 8529).

Five blocks, in the order a device declares them:

``spec.vrfs``
    The routing instances the device implements. Each has a name, a route
    distinguisher and an optional description. An interface binds itself to one
    with ``interfaces[].vrf``, and binding is what makes an address *private* to
    it: ``10.0.0.1/24`` in ``blue`` and ``10.0.0.1/24`` in the global instance
    are two different addresses, so they do not collide (``NG-A004``).
``spec.route_tables``
    The extra routing tables the device holds beyond the three every stack has.
    A table is a name and a number, and it exists to be *selected*: on its own
    it changes nothing, which is why it is declared next to the policy that
    reaches it.
``spec.routes``
    Static routes: a destination prefix, and how to reach it — a next hop
    (``via``), an egress interface (``dev``), or neither because the route
    discards (``blackhole``). Optionally in a VRF or a table, optionally with a
    metric.
``spec.routing_policy``
    The routing policy database: the ordered rules that decide *which table* a
    packet is routed by, rather than which route inside one wins. Policy-based
    routing is exactly this list — see below.
``spec.routing``
    The dynamic protocols and who they speak to: an OSPF area with the
    interfaces that run it, and a BGP autonomous system with its neighbours.

Policy-based routing
--------------------

Ordinary routing asks one question — "which route in *the* table matches this
destination?" Policy-based routing puts a question in front of it: "which table
should this packet be routed by at all?", answered from the packet's *source*,
its firewall mark, the interface it arrived on, or its DSCP. That is what
``spec.routing_policy`` models, and it is modelled the way every implementation
implements it (Linux's RPDB, RFC 1812 §5.2.4.3 "policy-based forwarding"): an
ordered list of rules, each with selectors and an action, walked from the lowest
``priority`` upwards until one matches.

Two consequences shape the model:

* **A rule names a table, not a route.** ``lookup`` is the ordinary action, and
  the table it names is either one of ``spec.route_tables``, one of
  ``spec.vrfs`` — a VRF *is* a table — or one of the three every stack is born
  with (``main``, ``local``, ``default``). Nothing else resolves (``NG-F019``).
* **Layer 4 is not a selector.** ``ip rule`` grew ``sport``/``dport`` late and
  no other implementation agrees on them; the portable way to route by port is
  to mark the packet in the firewall and match ``fwmark`` here. So the selectors
  stop at the network layer, and §16.9 says so rather than leaving the omission
  to be discovered.

**Peers are named by address, never by name.** A BGP neighbour is an address in
the real world, and it is an address in the inventory too; the graph layer
resolves it against every address the inventory configures
(:mod:`netgraph.render.graph`) and the validator reports the session it could
not place as a *warning*, because a perfectly correct eBGP session may point at
an upstream nobody declares here (``NG-F013``).

**No secrets, ever.** As with :mod:`netgraph.models.tunnel`, there is nowhere to
put a BGP password or an OSPF authentication key: a secret in an inventory is a
secret in version control, and netgraph has no use for one.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, Final

from pydantic import BeforeValidator, Field, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.scalars import Boolean, ElementName, IfName

__all__ = [
    "ASN_MAX",
    "DSCP_MAX",
    "FWMARK_PATTERN",
    "MAIN_TABLE",
    "PRIORITY_MAX",
    "RESERVED_TABLES",
    "ROUTE_DISTINGUISHER_PATTERN",
    "TABLE_ID_MAX",
    "AddressFamily",
    "Asn",
    "BgpConfig",
    "BgpNeighbor",
    "Dscp",
    "Fwmark",
    "IPPrefix",
    "OspfArea",
    "OspfConfig",
    "PolicyAction",
    "PolicyRule",
    "RouteDistinguisher",
    "RouteTable",
    "RouterId",
    "RoutingConfig",
    "RulePriority",
    "StaticRoute",
    "TableId",
    "VrfDefinition",
    "normalise_area",
    "normalise_fwmark",
    "normalise_rd",
]

#: Largest autonomous system number: ``inet-types:as-number`` is a ``uint32``
#: (RFC 6793 four-byte AS numbers).
ASN_MAX: Final = 4_294_967_295

#: Largest 16-bit field, the bound of a type-1/type-2 route distinguisher's
#: assigned number and of a two-byte AS number.
_UINT16_MAX: Final = 65_535

#: ``spec.routing.bgp.asn`` / ``neighbors[].remote_asn``. AS 0 is reserved
#: (RFC 7607) and is refused rather than stored as "unset".
Asn = Annotated[int, Field(strict=True, ge=1, le=ASN_MAX)]

#: ``spec.routes[].metric`` — the administrative distance or cost the device
#: attaches to the route. ``uint32``, as every implementation models it.
RouteMetric = Annotated[int, Field(strict=True, ge=0, le=ASN_MAX)]

#: An OSPF router identifier, and the same value where BGP calls it one: a
#: dotted quad even in an IPv6-only network (RFC 5340 §2.1, RFC 4271 §4.2).
RouterId = ipaddress.IPv4Address

#: A destination prefix of either family, in canonical CIDR form. Host bits are
#: refused rather than masked away: ``10.0.0.1/24`` as a route destination is
#: either a typo or a ``/32``, and guessing which would put a route in the
#: diagram that the device does not have.
IPPrefix = ipaddress.IPv4Network | ipaddress.IPv6Network


# --------------------------------------------------------------------------- #
# Route distinguishers
# --------------------------------------------------------------------------- #

#: The three RFC 4364 §4.2 encodings, as one ECMA-262 pattern: ``«number»:«n»``
#: for a two- or four-byte AS number, and ``«ipv4»:«n»`` for an address. Written
#: without named groups so that the JSON Schema can carry it verbatim, the same
#: reason :data:`~netgraph.models.scalars.BITRATE_PATTERN` is.
ROUTE_DISTINGUISHER_PATTERN: Final = r"^(?:\d{1,3}(?:\.\d{1,3}){3}|\d{1,10}):\d{1,10}$"

_RD_RE: Final[re.Pattern[str]] = re.compile(ROUTE_DISTINGUISHER_PATTERN)


def normalise_rd(value: Any) -> Any:
    """Check a route distinguisher and return it unchanged (``NG-F001``).

    There is nothing to normalise — an RD has exactly one spelling — but there
    is plenty to reject. RFC 4364 §4.2 gives three encodings, and each bounds
    its two halves differently:

    * **type 0**, ``«2-byte ASN»:«4-byte number»`` — ``65000:1``;
    * **type 1**, ``«IPv4 address»:«2-byte number»`` — ``192.0.2.1:1``;
    * **type 2**, ``«4-byte ASN»:«2-byte number»`` — ``4200000000:1``.

    A number past its half's bound is refused here rather than silently stored,
    because the RD is what makes two VRFs telling apart in a VPN and a value no
    router would accept is not one anybody should read off a diagram.

    A YAML scalar that is not a string is refused too: an unquoted ``65000:1``
    is a *number* to a YAML 1.1 loader (sexagesimal), and the digits cannot be
    recovered from it.
    """
    if isinstance(value, (bool, int, float)):
        raise ValueError(
            "route distinguisher was parsed as a number; quote it in the YAML "
            'document (for example "65000:1")'
        )
    if not isinstance(value, str):
        raise ValueError(f"expected a route distinguisher string, got {type(value).__name__}")

    text = value.strip()
    if _RD_RE.match(text) is None:
        raise ValueError(
            f"{echo_value(value)} is not a route distinguisher; expected "
            "'<asn>:<number>' or '<ipv4>:<number>' (RFC 4364 §4.2)"
        )
    administrator, _, assigned = text.rpartition(":")
    if "." in administrator:
        _check_rd_half(administrator, text, ipv4=True)
        _check_rd_number(assigned, text, limit=_UINT16_MAX, half="assigned number")
        return text
    number = int(administrator)
    if number > ASN_MAX:
        raise ValueError(
            f"{echo_value(value)}: {administrator} is not an AS number; the largest is {ASN_MAX}"
        )
    # Type 0 gives the assigned number four bytes, type 2 only two.
    limit = ASN_MAX if number <= _UINT16_MAX else _UINT16_MAX
    _check_rd_number(assigned, text, limit=limit, half="assigned number")
    return text


def _check_rd_half(administrator: str, text: str, *, ipv4: bool) -> None:
    """Check the administrator half of a type-1 route distinguisher."""
    if not ipv4:  # pragma: no cover - the caller decides, and only passes True
        return
    try:
        ipaddress.IPv4Address(administrator)
    except ValueError as exc:
        raise ValueError(
            f"{echo_value(text)}: {administrator} is not an IPv4 address: {exc}"
        ) from None


def _check_rd_number(assigned: str, text: str, *, limit: int, half: str) -> None:
    if int(assigned) > limit:
        raise ValueError(
            f"{echo_value(text)}: {assigned} is out of range for the {half} of this "
            f"route-distinguisher type; the largest is {limit}"
        )


#: ``spec.vrfs[].rd`` — an RFC 4364 §4.2 route distinguisher.
RouteDistinguisher = Annotated[str, BeforeValidator(normalise_rd), Field(pattern=_RD_RE.pattern)]


# --------------------------------------------------------------------------- #
# OSPF areas
# --------------------------------------------------------------------------- #

#: The dotted-quad form an area is stored in, as an ECMA-262 pattern.
AREA_PATTERN: Final = r"^\d{1,3}(?:\.\d{1,3}){3}$"


def normalise_area(value: Any) -> Any:
    """Normalise an OSPF area to dotted-quad form (``NG-F006``).

    An area identifier is a 32-bit number that RFC 2328 §C.2 writes as a dotted
    quad, and every implementation accepts both spellings: ``0`` and ``0.0.0.0``
    are the backbone. Both are accepted here and stored as the dotted quad, so
    two documents that spell the same area differently compare equal — which is
    what lets the validator tell one area from another at all.
    """
    if isinstance(value, bool):
        raise ValueError("expected an OSPF area, got a boolean")
    if isinstance(value, int):
        if not 0 <= value <= ASN_MAX:
            raise ValueError(
                f"OSPF area {value} is out of range; an area identifier is a 32-bit number"
            )
        return str(ipaddress.IPv4Address(value))
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit():
            return normalise_area(int(text))
        try:
            return str(ipaddress.IPv4Address(text))
        except ValueError:
            raise ValueError(
                f"{echo_value(value)} is not an OSPF area; expected a dotted quad such as "
                "'0.0.0.0' or the plain number 0"
            ) from None
    raise ValueError(f"expected an OSPF area, got {type(value).__name__}")


#: ``spec.routing.ospf.area`` — an RFC 2328 area identifier, stored dotted.
OspfArea = Annotated[str, BeforeValidator(normalise_area), Field(pattern=AREA_PATTERN)]


# --------------------------------------------------------------------------- #
# Routing tables and the policy database (§16.3, §16.6)
# --------------------------------------------------------------------------- #

#: The tables a stack has before anybody declares one, and the numbers they are
#: reserved at (Linux ``rt_tables``; ``main`` and ``local`` are RFC 1812 §5.2.4's
#: forwarding table split in two by the implementation). They are never declared
#: and always resolvable, which is why ``NG-F015`` refuses to let a declaration
#: shadow either half of an entry.
RESERVED_TABLES: Final[Mapping[str, int]] = MappingProxyType(
    {"local": 255, "main": 254, "default": 253}
)

#: The table a route with no ``table`` and no ``vrf`` is in, and the one the
#: last rule of a default policy database looks up.
MAIN_TABLE: Final = "main"

#: Largest routing table identifier. A ``u32``, of which 0 means "unspecified"
#: and is therefore not a table anything can be placed in.
TABLE_ID_MAX: Final = 4_294_967_295

#: Largest rule priority. Also a ``u32``; 0 is a real priority (Linux puts the
#: ``local`` lookup there), so unlike a table id it is allowed.
PRIORITY_MAX: Final = 4_294_967_295

#: Largest DSCP code point: six bits of the traffic class octet (RFC 2474 §3).
DSCP_MAX: Final = 63

#: ``spec.route_tables[].id`` — the number the table is known by.
TableId = Annotated[int, Field(strict=True, ge=1, le=TABLE_ID_MAX)]

#: ``spec.routing_policy[].priority`` and ``[].goto`` — where a rule sits in the
#: database. Lower is consulted first, which is the one thing every
#: implementation of policy routing agrees on.
RulePriority = Annotated[int, Field(strict=True, ge=0, le=PRIORITY_MAX)]

#: ``spec.routing_policy[].dscp`` — the six-bit code point, not the whole octet.
Dscp = Annotated[int, Field(strict=True, ge=0, le=DSCP_MAX)]

#: The canonical form of a firewall mark: lower-case hexadecimal, with an
#: optional mask. Written as one ECMA-262 pattern so the JSON Schema can carry
#: it verbatim, the same reason :data:`ROUTE_DISTINGUISHER_PATTERN` is.
FWMARK_PATTERN: Final = r"^0x[0-9a-f]{1,8}(?:/0x[0-9a-f]{1,8})?$"

_FWMARK_RE: Final[re.Pattern[str]] = re.compile(FWMARK_PATTERN)

#: What a *written* mark may look like before it is normalised: decimal or hex,
#: either half, in any case.
_FWMARK_INPUT_RE: Final[re.Pattern[str]] = re.compile(
    r"^(0[xX][0-9a-fA-F]{1,8}|\d{1,10})(?:/(0[xX][0-9a-fA-F]{1,8}|\d{1,10}))?$"
)


def normalise_fwmark(value: Any) -> Any:
    """Normalise a firewall mark to ``0x1`` or ``0x1/0xff`` (``NG-F017``).

    A mark is a 32-bit number that a firewall wrote on the packet and that a
    policy rule matches, optionally under a mask: ``0x1/0xff`` matches every
    packet whose low byte is 1, whatever the other three hold. Decimal and
    hexadecimal are both accepted because both are written in the wild, and both
    are stored as hexadecimal — a mark is a *bit field*, it is read as one, and
    two documents that spell one mark differently have to compare equal or
    nothing downstream can tell one rule from another.

    Two things are refused rather than stored:

    * a number past 32 bits, which no implementation would take;
    * a value with bits outside its own mask (``0x100/0xff``), which is a rule
      that can never match — the mask clears the very bits being compared.
    """
    if isinstance(value, bool):
        raise ValueError("expected a firewall mark, got a boolean")
    if isinstance(value, int):
        if not 0 <= value <= TABLE_ID_MAX:
            raise _fwmark_range(value)
        return f"0x{value:x}"
    if not isinstance(value, str):
        raise ValueError(f"expected a firewall mark, got {type(value).__name__}")

    text = value.strip()
    match = _FWMARK_INPUT_RE.match(text)
    if match is None:
        raise ValueError(
            f"{echo_value(value)} is not a firewall mark; expected a number or "
            "'<value>/<mask>', in decimal or hexadecimal (for example '0x1' or '0x1/0xff')"
        )
    mark = _fwmark_half(match.group(1))
    mask = _fwmark_half(match.group(2)) if match.group(2) is not None else None
    if mask is not None and mark & ~mask:
        raise ValueError(
            f"{echo_value(value)}: the value has bits the mask 0x{mask:x} clears, so the "
            f"rule can never match"
        )
    return f"0x{mark:x}" if mask is None else f"0x{mark:x}/0x{mask:x}"


def _fwmark_half(text: str) -> int:
    """One half of a mark, decimal or hexadecimal, checked against 32 bits."""
    number = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    if number > TABLE_ID_MAX:
        raise _fwmark_range(number)
    return number


def _fwmark_range(number: int) -> ValueError:
    """The out-of-range message, shared by the string and the integer path."""
    return ValueError(
        f"firewall mark {number} is out of range; a mark is a 32-bit number, so the "
        f"largest is {TABLE_ID_MAX}"
    )


#: ``spec.routing_policy[].fwmark`` — a mark, canonically hexadecimal.
Fwmark = Annotated[str, BeforeValidator(normalise_fwmark), Field(pattern=FWMARK_PATTERN)]


class AddressFamily(str, Enum):
    """Which of the two protocol families a policy rule is installed in.

    The policy database is per family — an IPv4 rule and an IPv6 rule with the
    same priority are two rules, in two lists, that never see each other's
    packets. Most rules say which family they are in by carrying a prefix; the
    field is for the ones that do not, and the one place it is *not* optional is
    a reader's understanding.
    """

    IPV4 = "ipv4"
    IPV6 = "ipv6"

    @classmethod
    def of(cls, version: int) -> AddressFamily:
        """The family of an :mod:`ipaddress` object's ``version``."""
        return cls.IPV4 if version == 4 else cls.IPV6


#: Both families, in the order everything here reports them.
_BOTH_FAMILIES: Final[tuple[AddressFamily, ...]] = (AddressFamily.IPV4, AddressFamily.IPV6)


class PolicyAction(str, Enum):
    """What happens to a packet a policy rule matches.

    ``lookup`` is the rule that does the routing; the other four are the ones
    that stop it, and they exist because a policy database without them can only
    ever *add* reachability. A rule that discards traffic from a guest prefix is
    one line here and a firewall in its absence.
    """

    #: Route the packet by the table this rule names, and stop walking.
    LOOKUP = "lookup"
    #: Discard it silently.
    BLACKHOLE = "blackhole"
    #: Discard it and answer "no route to host" (ICMP unreachable).
    UNREACHABLE = "unreachable"
    #: Discard it and answer "administratively prohibited".
    PROHIBIT = "prohibit"
    #: Jump to the rule at a given priority, skipping everything between.
    GOTO = "goto"

    @property
    def is_lookup(self) -> bool:
        return self is PolicyAction.LOOKUP

    @property
    def discards(self) -> bool:
        """Does the action drop the packet rather than route or redirect it?"""
        return self in (PolicyAction.BLACKHOLE, PolicyAction.UNREACHABLE, PolicyAction.PROHIBIT)


# --------------------------------------------------------------------------- #
# The blocks themselves
# --------------------------------------------------------------------------- #


class VrfDefinition(NetgraphModel):
    """One entry of ``spec.vrfs`` — a routing instance (RFC 8529 §4).

    The name is how everything else refers to the instance: an interface binds
    to it, a route is placed in it, and the address-collision rules partition
    their namespace by it. Two devices that use the same name are taken to mean
    the same VRF, which is what an operator means by "the blue VRF" — the route
    distinguisher is recorded because MPLS needs it, not because netgraph
    identifies the instance by it.
    """

    name: ElementName
    #: ``NG-F001``: an RFC 4364 §4.2 route distinguisher.
    rd: RouteDistinguisher
    description: str | None = None


class RouteTable(NetgraphModel):
    """One entry of ``spec.route_tables`` — a routing table of its own (§16.3).

    A table is a name and a number and nothing else, because that is all a table
    is: a container routes are placed in. What makes it *do* anything is a policy
    rule that looks it up (§16.6) — a table nothing selects holds routes nothing
    consults, which is ``NG-F023``.

    The three tables every stack already has — ``main``, ``local`` and
    ``default`` — are never declared here and always nameable anyway; declaring
    either their name or their number is ``NG-F015``, because a second ``main``
    is not a second table, it is a document that has stopped describing the
    device.
    """

    name: ElementName
    #: ``NG-F015``: unique within the device, and not one of the reserved numbers.
    id: TableId
    description: str | None = None

    def describe(self) -> str:
        """``uplink-b (table 100)`` — one phrase, for a diagnostic or a label."""
        return f"{self.name} (table {self.id})"


class StaticRoute(NetgraphModel):
    """One entry of ``spec.routes`` — a configured route (RFC 8349 ``rt:route``).

    A route needs somewhere to send the packet, so at least one of ``via``,
    ``dev`` and ``blackhole`` is required (``NG-F004``); ``blackhole`` excludes
    the other two, because a route that discards has no egress.
    """

    prefix: IPPrefix
    #: Next-hop address. Must be of the same family as ``prefix`` (``NG-F003``)
    #: and reachable on a prefix the device configures (``NG-F008``).
    via: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    #: Egress interface, for a next hop on an unnumbered link or a route that
    #: sends to an interface rather than to an address (``NG-F009``).
    dev: IfName | None = None
    #: The routing instance the route belongs to; the global one when unset.
    vrf: ElementName | None = None
    #: The table the route is placed in, for policy-based routing (§16.6);
    #: ``main`` when unset. Names an entry of ``spec.route_tables``, or one of
    #: the reserved tables (``NG-F019``). A VRF is a table of its own, so naming
    #: both is a contradiction rather than a refinement (``NG-F018``).
    table: ElementName | None = None
    metric: RouteMetric | None = None
    #: Discard matching packets instead of forwarding them.
    blackhole: Boolean = False

    @model_validator(mode="after")
    def _check_table(self) -> StaticRoute:
        """``NG-F018``: ``vrf`` and ``table`` are two spellings of one choice."""
        if self.vrf is not None and self.table is not None:
            raise field_error(
                f"route {self.prefix} names both VRF {self.vrf!r} and table "
                f"{self.table!r}; a VRF is a routing table of its own, so a route is "
                f"placed in one or the other",
                rule="NG-F018",
                path=("table",),
            )
        return self

    @model_validator(mode="after")
    def _check_next_hop(self) -> StaticRoute:
        if self.blackhole:
            for key in ("via", "dev"):
                if getattr(self, key) is not None:
                    raise field_error(
                        f"a blackhole route discards packets and must not declare {key!r}",
                        rule="NG-F004",
                        path=(key,),
                    )
            return self
        if self.via is None and self.dev is None:
            raise field_error(
                "a route needs somewhere to send the packet: declare 'via', 'dev', "
                "or 'blackhole: true'",
                rule="NG-F004",
                path=("via",),
            )
        if self.via is not None and self.via.version != self.prefix.version:
            raise field_error(
                f"next hop {self.via} is IPv{self.via.version} but {self.prefix} is "
                f"IPv{self.prefix.version}; a next hop is resolved on the destination's "
                f"own address family",
                rule="NG-F003",
                path=("via",),
            )
        return self

    @property
    def family(self) -> str:
        """``ipv4`` or ``ipv6``, the schema's spelling."""
        return f"ipv{self.prefix.version}"

    @property
    def is_default(self) -> bool:
        """Is this the default route of its family?"""
        return self.prefix.prefixlen == 0

    def describe(self) -> str:
        """``0.0.0.0/0 via 203.0.113.1 dev wan0`` — one line, for a diagnostic."""
        parts = [str(self.prefix)]
        if self.blackhole:
            parts.append("blackhole")
        if self.via is not None:
            parts.append(f"via {self.via}")
        if self.dev is not None:
            parts.append(f"dev {self.dev}")
        if self.vrf is not None:
            parts.append(f"vrf {self.vrf}")
        if self.table is not None:
            parts.append(f"table {self.table}")
        if self.metric is not None:
            parts.append(f"metric {self.metric}")
        return " ".join(parts)

    @property
    def table_name(self) -> str:
        """The table this route lands in: its own, its VRF's, or ``main``.

        One property rather than three call sites deciding for themselves, because
        "which table is this route in" is asked by the validator, the routing view
        and the iproute2 emitter, and an answer that differed between them would
        put a route in one table on the diagram and another on the wire.
        """
        return self.table or self.vrf or MAIN_TABLE


class PolicyRule(NetgraphModel):
    """One entry of ``spec.routing_policy`` — a rule of the policy database (§16.6).

    Read it as a sentence, which is how every implementation writes it: *at
    priority 100, a packet from 10.20.0.0/16 is routed by the table called
    uplink-b*. The selectors are the subject, the action is the verb, and the
    ``priority`` is where in the queue the sentence is read — lowest first, first
    match wins, and nothing after a matching ``lookup`` is consulted.

    The selectors are all layer 3 or below on purpose (see the module docstring):
    where the packet came from, where it is going, which interface it arrived on
    or would leave by, what the firewall marked it with, and what it asked for in
    its DSCP. A rule that names none of them matches everything, which is not a
    mistake — it is how the database is terminated.
    """

    #: ``NG-F020``: unique within the device, per family.
    priority: RulePriority
    #: Which family's database the rule is installed in. Derived from ``src`` and
    #: ``dst`` when they say so, and both families when nothing does.
    family: AddressFamily | None = None
    #: Source prefix — the selector policy-based routing exists for.
    src: IPPrefix | None = None
    #: Destination prefix. Narrower than a route for the same prefix would be,
    #: because it selects a *table* rather than a next hop.
    dst: IPPrefix | None = None
    #: The mark a firewall put on the packet, optionally masked (``0x1/0xff``).
    #: This is how a port, a user or an application reaches the policy database:
    #: something marks, and this matches (§16.9).
    fwmark: Fwmark | None = None
    #: The interface the packet arrived on (``NG-F021``).
    iif: IfName | None = None
    #: The interface the packet would leave by (``NG-F021``). Only meaningful for
    #: locally originated traffic, which is the point: it is how a socket bound
    #: to one uplink is routed by that uplink's table.
    oif: IfName | None = None
    #: The DSCP code point the packet carries (RFC 2474).
    dscp: Dscp | None = None
    #: Match everything the selectors do *not* — iproute2's ``not``. Meaningless
    #: without a selector to invert, which is ``NG-F017``.
    invert: Boolean = False
    action: PolicyAction = PolicyAction.LOOKUP
    #: The table to route by, required by ``lookup`` and refused by everything
    #: else (``NG-F016``). Names an entry of ``spec.route_tables``, a VRF, or a
    #: reserved table (``NG-F019``).
    table: ElementName | None = None
    #: The priority to jump to, required by ``goto`` and refused by everything
    #: else (``NG-F016``). Strictly greater than this rule's own, since the
    #: database is walked upwards and a backwards jump is a loop.
    goto: RulePriority | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _check_action(self) -> PolicyRule:
        """``NG-F016``: the action and the thing it acts on agree."""
        if self.action is PolicyAction.LOOKUP and self.table is None:
            raise field_error(
                "a 'lookup' rule needs the table to look up: name one in 'table', or "
                "choose an action that discards ('blackhole', 'unreachable', 'prohibit')",
                rule="NG-F016",
                path=("table",),
            )
        if self.action is not PolicyAction.LOOKUP and self.table is not None:
            raise field_error(
                f"a {self.action.value!r} rule does not route the packet, so it must not "
                f"name a table",
                rule="NG-F016",
                path=("table",),
            )
        if self.action is PolicyAction.GOTO and self.goto is None:
            raise field_error(
                "a 'goto' rule needs the priority it jumps to",
                rule="NG-F016",
                path=("goto",),
            )
        if self.action is not PolicyAction.GOTO and self.goto is not None:
            raise field_error(
                f"'goto' is the jump target of a 'goto' rule; this one is {self.action.value!r}",
                rule="NG-F016",
                path=("goto",),
            )
        if self.goto is not None and self.goto <= self.priority:
            raise field_error(
                f"this rule is at priority {self.priority} and jumps to {self.goto}; the "
                f"database is walked from the lowest priority upwards, so a jump that does "
                f"not go forwards is a loop",
                rule="NG-F016",
                path=("goto",),
            )
        return self

    @model_validator(mode="after")
    def _check_selectors(self) -> PolicyRule:
        """``NG-F017``: the selectors agree with each other and with the family."""
        if self.src is not None and self.dst is not None and self.src.version != self.dst.version:
            raise field_error(
                f"'src' is {self.src} and 'dst' is {self.dst}; one rule matches one "
                f"address family, so the two prefixes cannot be IPv{self.src.version} "
                f"and IPv{self.dst.version}",
                rule="NG-F017",
                path=("dst",),
            )
        if self.family is not None:
            for key in ("src", "dst"):
                prefix: IPPrefix | None = getattr(self, key)
                if prefix is not None and AddressFamily.of(prefix.version) is not self.family:
                    raise field_error(
                        f"the rule is declared {self.family.value} but {key!r} is "
                        f"{prefix}, which is IPv{prefix.version}",
                        rule="NG-F017",
                        path=(key,),
                    )
        if self.invert and not self.selectors:
            raise field_error(
                "'invert' matches everything the selectors do not, and this rule has no "
                "selector to invert, so it would match nothing at all",
                rule="NG-F017",
                path=("invert",),
            )
        return self

    @property
    def selectors(self) -> tuple[str, ...]:
        """The selectors, rendered, in the order :meth:`describe` reads them.

        Empty for a rule that matches every packet — which is what
        :attr:`is_catch_all` is, and what makes every rule after it in the same
        family unreachable (``W149``).
        """
        parts: list[str] = []
        if self.src is not None:
            parts.append(f"from {self.src}")
        if self.dst is not None:
            parts.append(f"to {self.dst}")
        if self.iif is not None:
            parts.append(f"iif {self.iif}")
        if self.oif is not None:
            parts.append(f"oif {self.oif}")
        if self.fwmark is not None:
            parts.append(f"fwmark {self.fwmark}")
        if self.dscp is not None:
            parts.append(f"dscp {self.dscp}")
        return tuple(parts)

    @property
    def is_catch_all(self) -> bool:
        """Does the rule match every packet of its family?"""
        return not self.selectors

    @property
    def families(self) -> tuple[AddressFamily, ...]:
        """The families this rule is installed in, IPv4 first.

        Stated when ``family`` is, derived from a prefix when one is given, and
        both when neither says: a rule selecting on nothing but a firewall mark
        is written once and installed twice, which is what an operator typing
        ``ip rule`` and ``ip -6 rule`` does by hand.
        """
        if self.family is not None:
            return (self.family,)
        for prefix in (self.src, self.dst):
            if prefix is not None:
                return (AddressFamily.of(prefix.version),)
        return _BOTH_FAMILIES

    @property
    def target(self) -> str:
        """The action, with whatever it acts on: ``lookup mgmt``, ``blackhole``."""
        if self.action is PolicyAction.LOOKUP:
            return f"lookup {self.table}"
        if self.action is PolicyAction.GOTO:
            return f"goto {self.goto}"
        return str(self.action.value)

    def describe(self) -> str:
        """``100: from 10.20.0.0/16 lookup uplink-b`` — one line, for a diagnostic."""
        selectors = list(self.selectors) or ["all"]
        if self.invert:
            selectors.insert(0, "not")
        return f"{self.priority}: {' '.join(selectors)} {self.target}"

    def matches_family(self, family: AddressFamily) -> bool:
        """Is this rule installed in ``family``'s database?"""
        return family in self.families


class OspfConfig(NetgraphModel):
    """``spec.routing.ospf`` — one OSPF area and the interfaces that run it.

    One area per device, deliberately: an area border router is a real thing,
    but modelling it needs per-interface areas, and a single area covers the
    inventories this revision is for. See §16.7.
    """

    area: OspfArea = "0.0.0.0"
    router_id: RouterId | None = None
    #: The interfaces OSPF runs on. Each must be an interface of the device
    #: (``NG-F010``); the list is non-empty and free of duplicates (``NG-F006``).
    interfaces: list[IfName] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_interfaces(self) -> OspfConfig:
        seen: set[str] = set()
        for index, name in enumerate(self.interfaces):
            if name in seen:
                raise field_error(
                    f"interface {name!r} is listed twice",
                    rule="NG-F006",
                    path=("interfaces", index),
                )
            seen.add(name)
        return self


class BgpNeighbor(NetgraphModel):
    """One entry of ``spec.routing.bgp.neighbors`` — a configured session.

    The peer is an **address**, not a name. That is what the device is
    configured with, and it is what lets the graph layer find the peer among the
    addresses the inventory already declares without a second reference grammar
    that could point somewhere else (§16.6).
    """

    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    remote_asn: Asn
    description: str | None = None

    @property
    def version(self) -> int:
        """4 or 6 — the family the session is carried over."""
        return self.address.version


class BgpConfig(NetgraphModel):
    """``spec.routing.bgp`` — the local AS, its router id and its neighbours."""

    asn: Asn
    router_id: RouterId | None = None
    neighbors: list[BgpNeighbor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_neighbors(self) -> BgpConfig:
        """``NG-F007``: one session per neighbour address."""
        seen: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for index, neighbor in enumerate(self.neighbors):
            if neighbor.address in seen:
                raise field_error(
                    f"neighbour {neighbor.address} is declared twice",
                    rule="NG-F007",
                    path=("neighbors", index, "address"),
                )
            seen.add(neighbor.address)
        return self

    def neighbor(
        self, address: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> BgpNeighbor | None:
        """The session configured towards ``address``, if there is one."""
        return next((entry for entry in self.neighbors if entry.address == address), None)


class RoutingConfig(NetgraphModel):
    """``spec.routing`` — the dynamic protocols a device takes part in.

    Both blocks are optional and neither implies the other: a device may run
    OSPF alone, BGP alone, both, or — with the block absent entirely — neither.
    """

    ospf: OspfConfig | None = None
    bgp: BgpConfig | None = None

    @property
    def router_ids(self) -> tuple[ipaddress.IPv4Address, ...]:
        """Every router id declared here, without repeats, OSPF first.

        One device commonly gives OSPF and BGP the same identifier — usually a
        loopback address — and that is one identity, not a duplicate, which is
        why the two are de-duplicated before anything compares them across
        devices (``NG-F012``).
        """
        ids = [
            block.router_id
            for block in (self.ospf, self.bgp)
            if block is not None and block.router_id is not None
        ]
        return tuple(dict.fromkeys(ids))

    @property
    def is_empty(self) -> bool:
        """Does the block declare no protocol at all?"""
        return self.ospf is None and self.bgp is None
