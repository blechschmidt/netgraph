"""Interfaces and their layer-2/layer-3 configuration (§6.2 of ``docs/schema.md``).

The shapes here mirror RFC 8343 (``ietf-interfaces``), RFC 8344 (``ietf-ip``)
and the IEEE 802.1Q bridge-port model; see §9 of the schema for the exact node
mapping.
"""

from __future__ import annotations

import ipaddress
from enum import Enum
from functools import cached_property
from typing import Annotated, Any, Final

from pydantic import Field, field_validator, model_validator

from netgraph.errors import clip_text, echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.scalars import (
    Boolean,
    IfName,
    InterfaceMtu,
    IPv4Mtu,
    IPv6Mtu,
    MacAddress,
    PrefixLengthV4,
    PrefixLengthV6,
    VlanId,
    VlanSet,
)

__all__ = [
    "AGGREGATE_TYPES",
    "CABLEABLE_TYPES",
    "AcceptableFrames",
    "IPv4Address",
    "IPv4Config",
    "IPv6Address",
    "IPv6Config",
    "Interface",
    "InterfaceList",
    "InterfaceType",
    "VlanConfig",
    "VlanMode",
]

#: Interface types that can terminate a cable (``NG-C009``).
CABLEABLE_TYPES: Final[frozenset[str]] = frozenset({"ethernet", "wifi", "lag"})
#: Minimum MTU for an interface that carries IPv6 addresses (``NG-I011``).
IPV6_MIN_MTU: Final = 1280


class InterfaceType(str, Enum):
    """§6.2.1 — the four core types plus the three extension types."""

    ETHERNET = "ethernet"
    WIFI = "wifi"
    LOOPBACK = "loopback"
    BRIDGE = "bridge"
    VLAN = "vlan"
    LAG = "lag"
    #: The local end of a ``tunnel`` document: ``wg0``, ``ipsec0``, ``vxlan100``
    #: (§14). Carries the *overlay* configuration — addresses inside the tunnel —
    #: while the tunnel document carries the encapsulation.
    TUNNEL = "tunnel"

    @property
    def iana_if_type(self) -> str:
        """The ``iana-if-type`` identity this type maps to (§6.2.1)."""
        return _IANA_IF_TYPE[self]

    @property
    def is_cableable(self) -> bool:
        """True for the physical (and aggregate) types a cable may terminate on."""
        return self.value in CABLEABLE_TYPES


_IANA_IF_TYPE: Final[dict[InterfaceType, str]] = {
    InterfaceType.ETHERNET: "ianaift:ethernetCsmacd",
    InterfaceType.WIFI: "ianaift:ieee80211",
    InterfaceType.LOOPBACK: "ianaift:softwareLoopback",
    InterfaceType.BRIDGE: "ianaift:bridge",
    InterfaceType.VLAN: "ianaift:l2vlan",
    InterfaceType.LAG: "ianaift:ieee8023adLag",
    InterfaceType.TUNNEL: "ianaift:tunnel",
}

#: Types whose ``parent`` names the interface they are stacked on. A ``vlan``
#: sub-interface must name one (``NG-I002``); a ``tunnel`` interface may name
#: the underlay port its outer packets leave by, which is optional because the
#: local end of a tunnel is often chosen by the routing table (§14.4).
_PARENT_TYPES: Final[frozenset[InterfaceType]] = frozenset(
    {InterfaceType.VLAN, InterfaceType.TUNNEL}
)

#: Types that require ``members`` (§6.2, ``NG-I003``).
AGGREGATE_TYPES: Final[frozenset[InterfaceType]] = frozenset(
    {InterfaceType.LAG, InterfaceType.BRIDGE}
)


class VlanMode(str, Enum):
    """§6.2.4 — operational port vocabulary, translated in §9.3."""

    ACCESS = "access"
    TRUNK = "trunk"


class AcceptableFrames(str, Enum):
    """``dot1q:acceptable-frame`` (§9.3)."""

    ALL = "admit-all-frames"
    TAGGED_ONLY = "admit-only-VLAN-tagged-frames"
    UNTAGGED_ONLY = "admit-only-untagged-and-priority-tagged"


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


def _plain_address(text: str, family: int) -> dict[str, Any] | None:
    """``10.0.0.1/24`` → ``{"ip": IPv4Address('10.0.0.1'), "prefix_length": 24}``.

    A hand-rolled fast path for the one spelling almost every address in a real
    inventory uses: a literal address of the expected family, a ``/``, and a
    decimal prefix length. It exists because the general path is expensive
    three times over — :func:`ipaddress.ip_interface` guesses the family by
    trying IPv4 and then IPv6, it builds a whole network object to recover a
    number the document already stated, and rendering the address back to a
    string only makes pydantic parse it a second time.

    ``None`` means "not this shape", and is not a verdict on the value: the
    caller falls back to :func:`ipaddress.ip_interface`, which owns every
    diagnostic this function must not invent. Anything the fast path *does*
    return is exactly what the general path would have returned, with the
    address left as the object ``ipaddress`` just built instead of its string
    form — pydantic validates the two to the same value.
    """
    address, separator, prefix = text.partition("/")
    # ``isascii`` matters: ``ipaddress`` rejects non-ASCII digits, ``isdigit``
    # alone accepts them, and ``int`` would then quietly convert one. The length
    # bound matters for the opposite reason: ``int`` *refuses* a literal of more
    # than 4300 digits, with a message about ``sys.set_int_max_str_digits`` that
    # would replace ``ipaddress``'s perfectly good one.
    if not separator or len(prefix) > 3 or not prefix.isascii() or not prefix.isdigit():
        return None
    length = int(prefix)
    if length > (32 if family == 4 else 128):
        return None
    try:
        ip = ipaddress.IPv4Address(address) if family == 4 else ipaddress.IPv6Address(address)
    except ValueError:
        return None
    return {"ip": ip, "prefix_length": length}


def _split_address_shorthand(value: Any, family: int) -> Any:
    """Expand the ``10.10.10.1/24`` shorthand into ``{ip, prefix_length}``."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if "%" in text:
        raise ValueError(
            f"{echo_value(value)} carries a zone index; netgraph uses zone-free addresses"
        )
    if "/" not in text:
        raise ValueError(
            f"{echo_value(value)} is missing a prefix length; write it as "
            f"{'10.0.0.1/24' if family == 4 else '2001:db8::1/64'} or as a mapping"
        )
    plain = _plain_address(text, family)
    if plain is not None:
        return plain
    try:
        interface = ipaddress.ip_interface(text)
    except ValueError as exc:
        raise ValueError(
            f"{echo_value(value)} is not a valid IPv{family} address: {clip_text(str(exc))}"
        ) from exc
    if interface.version != family:
        raise ValueError(
            f"{echo_value(value)} is an IPv{interface.version} address, expected IPv{family}"
        )
    return {"ip": str(interface.ip), "prefix_length": interface.network.prefixlen}


def _reject_zone(value: Any) -> Any:
    if isinstance(value, str) and "%" in value:
        raise ValueError(
            f"{echo_value(value)} carries a zone index; netgraph uses zone-free addresses"
        )
    return value


def _plain_gateway(value: Any, family: int) -> Any:
    """Normalise a ``gateway`` entry: a bare address, never a prefix.

    A first hop is one host, so ``10.0.0.254/24`` is a category error rather
    than a spelling of ``10.0.0.254``. Saying so here beats pydantic's generic
    "value is not a valid IPv4 address", because the mistake — pasting the
    address *with* its mask out of an ``ip addr`` listing — has an obvious fix.
    """
    if not isinstance(value, str):
        return _reject_zone(value)
    text = _reject_zone(value.strip())
    if "/" in text:
        raise ValueError(
            f"{echo_value(value)} carries a prefix length; a 'gateway' is a single "
            f"first-hop address, written as {text.partition('/')[0]}"
        )
    try:
        gateway = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ValueError(
            f"{echo_value(value)} is not a valid IPv{family} address: {clip_text(str(exc))}"
        ) from exc
    if gateway.version != family:
        raise ValueError(
            f"{echo_value(value)} is an IPv{gateway.version} address, expected IPv{family}"
        )
    return gateway


class IPv4Address(NetgraphModel):
    """One entry of ``interfaces[].ipv4.addresses`` (RFC 8344 ``ip:address``).

    The ``netmask`` form of the RFC 8344 ``subnet`` choice is accepted on input
    and normalised to ``prefix_length``; a non-contiguous mask is rejected
    (``NG-A003``).
    """

    ip: ipaddress.IPv4Address
    prefix_length: PrefixLengthV4

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, value: Any) -> Any:
        value = _split_address_shorthand(value, 4)
        if not isinstance(value, dict):
            return value

        data = dict(value)
        _reject_zone(data.get("ip"))
        netmask = data.pop("netmask", None)
        has_prefix = data.get("prefix_length") is not None

        if netmask is None:
            if not has_prefix:
                raise ValueError("exactly one of 'prefix_length' or 'netmask' is required")
            return data
        if has_prefix:
            raise ValueError("'prefix_length' and 'netmask' are mutually exclusive")

        data["prefix_length"] = _prefix_length_from_netmask(netmask)
        return data

    @property
    def interface(self) -> ipaddress.IPv4Interface:
        """The address as an :mod:`ipaddress` interface object."""
        return ipaddress.IPv4Interface((self.ip, self.prefix_length))

    @cached_property
    def network(self) -> ipaddress.IPv4Network:
        """The prefix this address sits in.

        Cached, and built without the intermediate
        :class:`ipaddress.IPv4Interface` — which constructs exactly this network
        internally and then throws the rest away. Five validation rules and the
        layer-3 graph each ask an address for its prefix, so an uncached
        property re-derived it once per consumer; see entry 7 of
        ``docs/follow-ups.md``. The value is a pure function of two fields that
        never change after validation, so caching it cannot alter a result.

        The integer form of :attr:`ip` is handed over rather than the object:
        :mod:`ipaddress` re-parses an address object it is given back out of
        ``str(address)``, and the integer is the state that object already
        holds.
        """
        return ipaddress.IPv4Network((int(self.ip), self.prefix_length), strict=False)

    @property
    def netmask(self) -> ipaddress.IPv4Address:
        """The dotted-quad form of :attr:`prefix_length`."""
        return self.network.netmask

    def __str__(self) -> str:
        return f"{self.ip}/{self.prefix_length}"


def _prefix_length_from_netmask(netmask: Any) -> int:
    if not isinstance(netmask, str):
        raise ValueError(f"'netmask' must be a dotted quad, got {type(netmask).__name__}")
    try:
        network = ipaddress.IPv4Network(f"0.0.0.0/{netmask}")
    except ValueError as exc:
        # ipaddress rejects non-contiguous masks with this exact wording.
        raise ValueError(
            f"{echo_value(netmask)} is not a contiguous IPv4 netmask: {clip_text(str(exc))}"
        ) from exc
    return network.prefixlen


class IPv6Address(NetgraphModel):
    """One entry of ``interfaces[].ipv6.addresses``.

    RFC 8344 has no ``netmask`` case for IPv6, so ``prefix_length`` is
    mandatory (``NG-A001``). The address is normalised to the RFC 5952
    lower-case compressed form.
    """

    ip: ipaddress.IPv6Address
    prefix_length: PrefixLengthV6

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, value: Any) -> Any:
        value = _split_address_shorthand(value, 6)
        if not isinstance(value, dict):
            return value
        data = dict(value)
        _reject_zone(data.get("ip"))
        if "netmask" in data:
            raise ValueError("'netmask' is IPv4 only; IPv6 addresses use 'prefix_length'")
        if data.get("prefix_length") is None:
            raise ValueError("'prefix_length' is required for IPv6 addresses")
        return data

    @property
    def interface(self) -> ipaddress.IPv6Interface:
        """The address as an :mod:`ipaddress` interface object."""
        return ipaddress.IPv6Interface((self.ip, self.prefix_length))

    @cached_property
    def network(self) -> ipaddress.IPv6Network:
        """The prefix this address sits in.

        Cached and built directly, for the reason given on
        :attr:`IPv4Address.network`. IPv6 gains most: the round trip through
        ``str`` that the address-object form pays is a full RFC 5952
        compression and re-parse.
        """
        return ipaddress.IPv6Network((int(self.ip), self.prefix_length), strict=False)

    def __str__(self) -> str:
        return f"{self.ip}/{self.prefix_length}"


def _expand_family_shorthand(value: Any, family: int) -> Any:
    """Expand the ``ipv4: [10.0.0.1/24]`` shorthand (§6.2.3)."""
    if isinstance(value, (list, tuple)):
        return {"addresses": list(value)}
    if isinstance(value, str):
        raise ValueError(
            f"expected a mapping or a list of addresses; write "
            f"'ipv{family}: [{value}]' or 'ipv{family}: {{addresses: [{value}]}}'"
        )
    return value


class _AddressFamily(NetgraphModel):
    """Shared fields of the ``ipv4``/``ipv6`` containers (RFC 8344).

    ``gateway`` is declared by each subclass rather than here, because its type
    is family-specific. It is the one field of these containers that RFC 8344
    does not define: a default route lives in ``ietf-routing``
    (``rt:routing/control-plane-protocols/static-routes/…/next-hop-address``),
    not in ``ietf-ip``. netgraph keeps it on the interface anyway, because that
    is where an operator writes it and where the only check worth making —
    "is the first hop on-link?" (``E020``) — can be made.
    """

    #: ``ip:ipv4/enabled`` / ``ip:ipv6/enabled``.
    enabled: Boolean = True
    #: ``ip:*/forwarding``. ``None`` until the device default is applied.
    forwarding: Boolean | None = None

    @property
    def is_forwarding(self) -> bool:
        """Resolved forwarding state (RFC 8344 defaults to ``false``)."""
        return bool(self.forwarding)


class IPv4Config(_AddressFamily):
    """``interfaces[].ipv4`` — the RFC 8344 ``ip:ipv4`` container."""

    #: ``ip:ipv4/mtu``; defaults to the interface MTU.
    mtu: IPv4Mtu | None = None
    addresses: list[IPv4Address] = Field(default_factory=list)
    #: First hop for traffic this interface cannot deliver on-link — the
    #: next-hop of the default route. Not part of RFC 8344; see
    #: :class:`_AddressFamily`.
    gateway: ipaddress.IPv4Address | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, value: Any) -> Any:
        return _expand_family_shorthand(value, 4)

    @field_validator("gateway", mode="before")
    @classmethod
    def _normalise_gateway(cls, value: Any) -> Any:
        return _plain_gateway(value, 4)

    @field_validator("addresses")
    @classmethod
    def _unique_addresses(cls, addresses: list[IPv4Address]) -> list[IPv4Address]:
        _check_unique_addresses(addresses)
        return addresses


class IPv6Config(_AddressFamily):
    """``interfaces[].ipv6`` — the RFC 8344 ``ip:ipv6`` container."""

    #: ``ip:ipv6/mtu``; defaults to the interface MTU when it is at least 1280.
    mtu: IPv6Mtu | None = None
    addresses: list[IPv6Address] = Field(default_factory=list)
    #: First hop for off-link IPv6 traffic. See :class:`_AddressFamily`.
    gateway: ipaddress.IPv6Address | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, value: Any) -> Any:
        return _expand_family_shorthand(value, 6)

    @field_validator("gateway", mode="before")
    @classmethod
    def _normalise_gateway(cls, value: Any) -> Any:
        return _plain_gateway(value, 6)

    @field_validator("addresses")
    @classmethod
    def _unique_addresses(cls, addresses: list[IPv6Address]) -> list[IPv6Address]:
        _check_unique_addresses(addresses)
        return addresses


def _check_unique_addresses(addresses: list[Any]) -> None:
    """``NG-A002``: ``ip`` is the RFC 8344 list key, so it must be unique.

    Compared as :mod:`ipaddress` objects rather than as strings: within one
    family that is the same equivalence — both are normalised by then — and it
    avoids rendering every address of every interface back to text purely to
    hash it.
    """
    seen: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for entry in addresses:
        key = entry.ip
        if key in seen:
            raise ValueError(f"duplicate address {key}")
        seen.add(key)


# --------------------------------------------------------------------------- #
# VLAN configuration
# --------------------------------------------------------------------------- #


class VlanConfig(NetgraphModel):
    """``interfaces[].vlan`` — the 802.1Q bridge-port configuration (§6.2.4)."""

    mode: VlanMode
    #: ``dot1q:pvid`` in access mode; the encapsulation VID of a ``vlan`` interface.
    access_vlan: VlanId | None = None
    #: The tagged VLAN set of a trunk port.
    trunk_vlans: VlanSet | None = None
    #: Untagged VLAN on a trunk; becomes ``dot1q:pvid``.
    native_vlan: VlanId | None = None
    ingress_filtering: Boolean = True
    #: ``dot1q:acceptable-frame``; derived per §9.3 when not stated.
    acceptable_frames: AcceptableFrames | None = None

    @model_validator(mode="after")
    def _check_mode_consistency(self) -> VlanConfig:
        if self.mode is VlanMode.ACCESS:
            if self.trunk_vlans is not None:
                raise field_error(
                    "'trunk_vlans' is not allowed in access mode",
                    rule="NG-V002",
                    path=("trunk_vlans",),
                )
            if self.native_vlan is not None:
                raise field_error(
                    "'native_vlan' is only allowed in trunk mode",
                    rule="NG-V003",
                    path=("native_vlan",),
                )
            if self.access_vlan is None:
                self.access_vlan = 1
        else:
            if self.access_vlan is not None:
                raise field_error(
                    "'access_vlan' is not allowed in trunk mode",
                    rule="NG-V002",
                    path=("access_vlan",),
                )
            if self.trunk_vlans is None:
                raise field_error(
                    "'trunk_vlans' is required in trunk mode",
                    rule="NG-V002",
                    path=("trunk_vlans",),
                )

        if self.acceptable_frames is None:
            self.acceptable_frames = self._derive_acceptable_frames()
        return self

    def _derive_acceptable_frames(self) -> AcceptableFrames:
        """§9.3 derivation table."""
        if self.mode is VlanMode.ACCESS:
            return AcceptableFrames.UNTAGGED_ONLY
        if self.native_vlan is not None:
            return AcceptableFrames.ALL
        return AcceptableFrames.TAGGED_ONLY

    @property
    def pvid(self) -> int:
        """``dot1q:pvid`` (§9.3): the access VLAN, the native VLAN, or 1."""
        if self.mode is VlanMode.ACCESS:
            return self.access_vlan if self.access_vlan is not None else 1
        return self.native_vlan if self.native_vlan is not None else 1

    def vlan_ids(self) -> frozenset[int]:
        """Every VLAN the port is a member of, native VLAN included (§9.3)."""
        if self.mode is VlanMode.ACCESS:
            return frozenset({self.pvid})
        members = set(self.trunk_vlans) if self.trunk_vlans is not None else set()
        if self.native_vlan is not None:
            members.add(self.native_vlan)
        return frozenset(members)


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #


class Interface(NetgraphModel):
    """One entry of ``spec.interfaces`` (RFC 8343 ``if:interface``)."""

    name: IfName
    type: InterfaceType
    description: str | None = None
    #: Intended admin state → ``if:enabled``.
    enabled: Boolean = True
    #: ``if:phys-address``; stored as intended state (§9.1).
    mac: MacAddress | None = None
    #: Layer-2 MTU, propagated to both address families (§6.2.2).
    mtu: InterfaceMtu | None = None
    ipv4: IPv4Config | None = None
    ipv6: IPv6Config | None = None
    vlan: VlanConfig | None = None
    #: ``if:lower-layer-if`` of a ``vlan`` sub-interface.
    parent: IfName | None = None
    #: ``if:lower-layer-if`` of a ``lag`` or ``bridge`` interface.
    members: list[IfName] | None = None

    @model_validator(mode="after")
    def _check_stacking(self) -> Interface:
        if self.type is InterfaceType.VLAN:
            if self.parent is None:
                raise field_error(
                    "'parent' is required for type 'vlan'", rule="NG-I002", path=("parent",)
                )
            if self.parent == self.name:
                raise field_error(
                    "'parent' must not be the interface itself",
                    rule="NG-I002",
                    path=("parent",),
                )
            if self.vlan is None or self.vlan.mode is not VlanMode.ACCESS:
                raise field_error(
                    "type 'vlan' requires a 'vlan' block in access mode carrying the "
                    "encapsulation VID (schema §6.2.1)",
                    path=("vlan",),
                )
        elif self.parent is not None:
            if self.type not in _PARENT_TYPES:
                raise field_error(
                    f"'parent' is only allowed for types 'vlan' and 'tunnel', "
                    f"not {self.type.value!r}",
                    rule="NG-I002",
                    path=("parent",),
                )
            if self.parent == self.name:
                raise field_error(
                    "'parent' must not be the interface itself",
                    rule="NG-I002",
                    path=("parent",),
                )

        if self.type in AGGREGATE_TYPES:
            if self.members is None:
                raise field_error(
                    f"'members' is required for type {self.type.value!r}",
                    rule="NG-I003",
                    path=("members",),
                )
            if not self.members:
                raise field_error("'members' is empty", rule="NG-I003", path=("members",))
            duplicates = _duplicates(self.members)
            if duplicates:
                raise field_error(
                    f"duplicate members: {', '.join(duplicates)}",
                    rule="NG-I003",
                    path=("members",),
                )
            if self.name in self.members:
                raise field_error(
                    f"{self.name!r} lists itself as a member",
                    rule="NG-I003",
                    path=("members",),
                )
        elif self.members is not None:
            raise field_error(
                f"'members' is only allowed for types 'lag' and 'bridge', not {self.type.value!r}",
                rule="NG-I003",
                path=("members",),
            )

        return self

    @model_validator(mode="after")
    def _check_mtu(self) -> Interface:
        """``NG-I011``: an interface with IPv6 addresses needs at least 1280 bytes."""
        if self.mtu is not None and self.mtu < IPV6_MIN_MTU and self.has_ipv6_addresses:
            raise field_error(
                f"mtu {self.mtu} is below the IPv6 minimum of {IPV6_MIN_MTU} but the "
                "interface carries IPv6 addresses",
                rule="NG-I011",
                path=("mtu",),
            )
        return self

    @property
    def has_ipv4_addresses(self) -> bool:
        return bool(self.ipv4 and self.ipv4.addresses)

    @property
    def has_ipv6_addresses(self) -> bool:
        return bool(self.ipv6 and self.ipv6.addresses)

    @property
    def is_cableable(self) -> bool:
        """``NG-C009``: only physical and aggregate interfaces can be cabled."""
        return self.type.is_cableable

    @property
    def lower_layer_if(self) -> tuple[str, ...]:
        """``if:lower-layer-if`` (§9.1): the parent, or the members."""
        if self.parent is not None:
            return (self.parent,)
        return tuple(self.members or ())

    def addresses(self) -> tuple[IPv4Address | IPv6Address, ...]:
        """Every configured address, IPv4 first."""
        v4: tuple[IPv4Address | IPv6Address, ...] = tuple(self.ipv4.addresses) if self.ipv4 else ()
        v6: tuple[IPv4Address | IPv6Address, ...] = tuple(self.ipv6.addresses) if self.ipv6 else ()
        return v4 + v6

    def gateways(self) -> tuple[tuple[int, ipaddress.IPv4Address | ipaddress.IPv6Address], ...]:
        """Each configured first hop as ``(family, address)``, IPv4 first.

        Callers that care about the gateway invariably care about it per family
        — a first hop has to be on-link in a prefix of its *own* family — so the
        version is handed over with it rather than re-derived.
        """
        gateways: list[tuple[int, ipaddress.IPv4Address | ipaddress.IPv6Address]] = []
        if self.ipv4 is not None and self.ipv4.gateway is not None:
            gateways.append((4, self.ipv4.gateway))
        if self.ipv6 is not None and self.ipv6.gateway is not None:
            gateways.append((6, self.ipv6.gateway))
        return tuple(gateways)


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicated: list[str] = []
    for value in values:
        if value in seen and value not in duplicated:
            duplicated.append(value)
        seen.add(value)
    return duplicated


#: Interfaces indexed by name, used by the device and adapter specs.
InterfaceList = Annotated[list[Interface], Field(min_length=1)]
