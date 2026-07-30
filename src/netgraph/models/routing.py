"""Routing configuration: VRFs, static routes and protocol adjacencies (§16).

Everything here hangs off a device's ``spec``, because routing is *state of a
box* rather than a thing between boxes: a route is written on one device, an
adjacency is configured on one device towards a neighbour it names by address.
That is the shape RFC 8349 (``ietf-routing``) gives it too — a control-plane
protocol and a routing table live inside a network instance, which is what a VRF
is (RFC 8529).

Three blocks, in the order a device declares them:

``spec.vrfs``
    The routing instances the device implements. Each has a name, a route
    distinguisher and an optional description. An interface binds itself to one
    with ``interfaces[].vrf``, and binding is what makes an address *private* to
    it: ``10.0.0.1/24`` in ``blue`` and ``10.0.0.1/24`` in the global instance
    are two different addresses, so they do not collide (``NG-A004``).
``spec.routes``
    Static routes: a destination prefix, and how to reach it — a next hop
    (``via``), an egress interface (``dev``), or neither because the route
    discards (``blackhole``). Optionally in a VRF, optionally with a metric.
``spec.routing``
    The dynamic protocols and who they speak to: an OSPF area with the
    interfaces that run it, and a BGP autonomous system with its neighbours.

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
from typing import Annotated, Any, Final

from pydantic import BeforeValidator, Field, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.scalars import Boolean, ElementName, IfName

__all__ = [
    "ASN_MAX",
    "ROUTE_DISTINGUISHER_PATTERN",
    "Asn",
    "BgpConfig",
    "BgpNeighbor",
    "IPPrefix",
    "OspfArea",
    "OspfConfig",
    "RouteDistinguisher",
    "RouterId",
    "RoutingConfig",
    "StaticRoute",
    "VrfDefinition",
    "normalise_area",
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
    metric: RouteMetric | None = None
    #: Discard matching packets instead of forwarding them.
    blackhole: Boolean = False

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
        if self.metric is not None:
            parts.append(f"metric {self.metric}")
        return " ".join(parts)


class OspfConfig(NetgraphModel):
    """``spec.routing.ospf`` — one OSPF area and the interfaces that run it.

    One area per device, deliberately: an area border router is a real thing,
    but modelling it needs per-interface areas, and a single area covers the
    inventories this revision is for. See §16.5.
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
    that could point somewhere else (§16.4).
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
