"""Interfaces and their layer-2/layer-3 configuration (§6.2 of ``docs/schema.md``).

The shapes here mirror RFC 8343 (``ietf-interfaces``), RFC 8344 (``ietf-ip``)
and the IEEE 802.1Q bridge-port model; see §9 of the schema for the exact node
mapping.
"""

from __future__ import annotations

import ipaddress
from enum import Enum
from functools import cached_property
from typing import Annotated, Any, Final, Literal

from pydantic import Field, field_validator, model_validator

from netviz.errors import clip_text, echo_value
from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.power import PoeConfig
from netviz.models.scalars import (
    Boolean,
    ElementName,
    IfName,
    InterfaceMtu,
    IPv4Mtu,
    IPv6Mtu,
    MacAddress,
    PrefixLengthV4,
    PrefixLengthV6,
    Ssid,
    TxPowerDbm,
    VlanId,
    VlanSet,
    WirelessChannel,
)

__all__ = [
    "AGGREGATE_TYPES",
    "CABLEABLE_TYPES",
    "CHANNEL_WIDTHS",
    "AcceptableFrames",
    "Band",
    "Bss",
    "IPv4Address",
    "IPv4Config",
    "IPv6Address",
    "IPv6Config",
    "Interface",
    "InterfaceList",
    "InterfaceType",
    "RadioRole",
    "Security",
    "VlanConfig",
    "VlanMode",
    "WirelessConfig",
]

#: Interface types that can terminate a cable (``NV-C009``).
CABLEABLE_TYPES: Final[frozenset[str]] = frozenset({"ethernet", "wifi", "lag"})
#: Minimum MTU for an interface that carries IPv6 addresses (``NV-I011``).
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
#: sub-interface must name one (``NV-I002``); a ``tunnel`` interface may name
#: the underlay port its outer packets leave by, which is optional because the
#: local end of a tunnel is often chosen by the routing table (§14.4).
_PARENT_TYPES: Final[frozenset[InterfaceType]] = frozenset(
    {InterfaceType.VLAN, InterfaceType.TUNNEL}
)

#: Types that require ``members`` (§6.2, ``NV-I003``).
AGGREGATE_TYPES: Final[frozenset[InterfaceType]] = frozenset(
    {InterfaceType.LAG, InterfaceType.BRIDGE}
)

#: Types that may carry a ``poe`` block (§17.3, ``NV-E006``). Power over
#: Ethernet travels over the twisted pairs of the run, so the port has to be one
#: a run lands on; ``lag`` is here because an aggregate of two PoE ports is how a
#: multi-gigabit access point is fed, and ``wifi`` is not because a radio is
#: precisely the port with no copper.
_POE_TYPES: Final[frozenset[InterfaceType]] = frozenset({InterfaceType.ETHERNET, InterfaceType.LAG})


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
            f"{echo_value(value)} carries a zone index; netviz uses zone-free addresses"
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
            f"{echo_value(value)} carries a zone index; netviz uses zone-free addresses"
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


class IPv4Address(NetvizModel):
    """One entry of ``interfaces[].ipv4.addresses`` (RFC 8344 ``ip:address``).

    The ``netmask`` form of the RFC 8344 ``subnet`` choice is accepted on input
    and normalised to ``prefix_length``; a non-contiguous mask is rejected
    (``NV-A003``).
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


class IPv6Address(NetvizModel):
    """One entry of ``interfaces[].ipv6.addresses``.

    RFC 8344 has no ``netmask`` case for IPv6, so ``prefix_length`` is
    mandatory (``NV-A001``). The address is normalised to the RFC 5952
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


class _AddressFamily(NetvizModel):
    """Shared fields of the ``ipv4``/``ipv6`` containers (RFC 8344).

    ``gateway`` is declared by each subclass rather than here, because its type
    is family-specific. It is the one field of these containers that RFC 8344
    does not define: a default route lives in ``ietf-routing``
    (``rt:routing/control-plane-protocols/static-routes/…/next-hop-address``),
    not in ``ietf-ip``. netviz keeps it on the interface anyway, because that
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
    """``NV-A002``: ``ip`` is the RFC 8344 list key, so it must be unique.

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


class VlanConfig(NetvizModel):
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
                    rule="NV-V002",
                    path=("trunk_vlans",),
                )
            if self.native_vlan is not None:
                raise field_error(
                    "'native_vlan' is only allowed in trunk mode",
                    rule="NV-V003",
                    path=("native_vlan",),
                )
            if self.access_vlan is None:
                self.access_vlan = 1
        else:
            if self.access_vlan is not None:
                raise field_error(
                    "'access_vlan' is not allowed in trunk mode",
                    rule="NV-V002",
                    path=("access_vlan",),
                )
            if self.trunk_vlans is None:
                raise field_error(
                    "'trunk_vlans' is required in trunk mode",
                    rule="NV-V002",
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
# Wireless configuration (§6.2.6)
# --------------------------------------------------------------------------- #


class RadioRole(str, Enum):
    """§6.2.6 — which side of an association a radio is on.

    ``ap`` beacons, so it is the end that owns the SSIDs; ``station`` and
    ``mesh`` associate to one. A mesh node's backhaul radio is a station of the
    node above it, spelled separately because it is a relay rather than a client
    and a diagram should not draw it as a laptop.
    """

    AP = "ap"
    STATION = "station"
    MESH = "mesh"

    @property
    def is_ap(self) -> bool:
        return self is RadioRole.AP

    @property
    def is_client(self) -> bool:
        """Does this role associate *to* an AP rather than beacon for one?"""
        return self is not RadioRole.AP


class Band(str, Enum):
    """§6.2.6 — the three bands 802.11 operates in.

    The band is not derivable from the channel number: channels 1 to 13 exist in
    both the 2.4 GHz and the 6 GHz plan and mean different frequencies, so a
    document that states a channel has to state the band with it (``NV-W003``).
    """

    B2_4 = "2.4GHz"
    B5 = "5GHz"
    B6 = "6GHz"

    @property
    def channels(self) -> frozenset[int]:
        """Every channel number this band numbers (``NV-W003``)."""
        return _BAND_CHANNELS[self]

    @property
    def widths(self) -> frozenset[int]:
        """The channel widths this band supports (``NV-W004``)."""
        return _BAND_WIDTHS[self]

    def centre_mhz(self, channel: int) -> int:
        """Centre frequency of the 20 MHz channel ``channel``, in MHz.

        Raises:
            KeyError: ``channel`` is not a channel of this band.
        """
        if channel not in self.channels:
            raise KeyError(channel)
        base, spacing = _BAND_PLAN[self]
        return _CHANNEL_14_MHZ if self is Band.B2_4 and channel == 14 else base + spacing * channel


#: The channel widths the schema accepts at all, largest last (§6.2.6).
CHANNEL_WIDTHS: Final[tuple[int, ...]] = (20, 40, 80, 160, 320)

#: ``base + spacing * channel`` gives a channel's centre frequency in MHz. The
#: 2.4 GHz plan is anchored so that channel 1 is 2412 MHz; channel 14 is the one
#: exception and is spelled out in :data:`_CHANNEL_14_MHZ`.
_BAND_PLAN: Final[dict[Band, tuple[int, int]]] = {
    Band.B2_4: (2407, 5),
    Band.B5: (5000, 5),
    Band.B6: (5950, 5),
}

#: Channel 14 (Japan, 802.11b only) sits 12 MHz above channel 13 rather than 5.
_CHANNEL_14_MHZ: Final = 2484

_BAND_CHANNELS: Final[dict[Band, frozenset[int]]] = {
    # 1 to 14; 14 is 802.11b-only and legal in Japan alone, but it is a channel a
    # device can be set to, and refusing to *record* it would be wrong.
    Band.B2_4: frozenset(range(1, 15)),
    # UNII-1 through UNII-4, as numbered by 802.11: 32 to 68 and 96 in the low
    # bands, 100 to 144 in UNII-2 extended, 149 to 177 in UNII-3 and UNII-4.
    Band.B5: frozenset({32, 68, 96})
    | frozenset(range(36, 65, 4))
    | frozenset(range(100, 145, 4))
    | frozenset(range(149, 178, 4)),
    # UNII-5 through UNII-8: the 20 MHz channels are numbered 1, 5, 9 … 233.
    Band.B6: frozenset(range(1, 234, 4)),
}

_BAND_WIDTHS: Final[dict[Band, frozenset[int]]] = {
    # 802.11n at 2.4 GHz can bond two channels and no more; there is not enough
    # spectrum in the band for 80 MHz to exist.
    Band.B2_4: frozenset({20, 40}),
    # 320 MHz is an 802.11be feature of the 6 GHz band only.
    Band.B5: frozenset({20, 40, 80, 160}),
    Band.B6: frozenset(CHANNEL_WIDTHS),
}


class Security(str, Enum):
    """§6.2.6 — how a BSS authenticates and encrypts.

    ``open`` covers both a genuinely open network and OWE: netviz records
    whether a passphrase or an authentication server is involved, which is what
    a reader of a diagram needs, and not the cipher suite negotiated on top.
    """

    OPEN = "open"
    WPA2_PSK = "wpa2-psk"
    WPA2_EAP = "wpa2-eap"
    WPA3_PSK = "wpa3-psk"
    WPA3_EAP = "wpa3-eap"

    @property
    def is_encrypted(self) -> bool:
        """Does traffic in this BSS get link-layer encryption?"""
        return self is not Security.OPEN


class Bss(NetvizModel):
    """One entry of ``interfaces[].wireless.bss`` — a basic service set.

    On an ``ap`` radio each entry is an SSID the radio beacons, with the VLAN
    its traffic is bridged into. On a ``station`` or ``mesh`` radio the single
    entry names the BSS the radio is associated to, which is what ``NV-W010``
    checks against the AP at the other end of the link.
    """

    ssid: Ssid
    #: ``dot11:bssid`` — the MAC address of the BSS. Usually the radio's own
    #: address for the first SSID and a derived one for each further SSID, which
    #: is why it is written per BSS rather than inherited from ``mac``.
    bssid: MacAddress | None = None
    #: The VLAN this SSID's traffic is bridged into; ``None`` means the radio's
    #: untagged domain. Checked against the device's VLAN database (``W113``)
    #: and against what the AP actually carries (``NV-W009``).
    vlan: VlanId | None = None
    security: Security | None = None
    #: A hidden SSID is absent from the beacon, not absent from the air.
    hidden: Boolean = False


class WirelessConfig(NetvizModel):
    """``interfaces[].wireless`` — the radio configuration of a ``wifi`` port.

    Only a ``wifi`` interface may carry it (``NV-W002``): the block is what
    turns an otherwise shapeless ``medium: wireless`` link into an association
    with a direction, a frequency and a name.
    """

    role: RadioRole
    band: Band | None = None
    #: The primary 20 MHz channel. Requires ``band``, and must be one the band
    #: numbers (``NV-W003``).
    channel: WirelessChannel | None = None
    #: Total channel width in MHz — 20, 40, 80, 160 or 320. Requires ``band``,
    #: which bounds it (``NV-W004``).
    width_mhz: Literal[20, 40, 80, 160, 320] | None = None
    tx_power_dbm: TxPowerDbm | None = None
    bss: list[Bss] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_channel_plan(self) -> WirelessConfig:
        """``NV-W003``/``NV-W004``: the frequency settings agree with the band."""
        if self.band is None:
            for name in ("channel", "width_mhz"):
                if getattr(self, name) is not None:
                    raise field_error(
                        f"'{name}' requires 'band'; channel numbers and widths mean "
                        "different frequencies in different bands",
                        rule="NV-W003",
                        path=(name,),
                    )
            return self

        if self.channel is not None and self.channel not in self.band.channels:
            raise field_error(
                f"channel {self.channel} is not a channel of the {self.band.value} band; "
                f"{self.band.value} numbers {_channel_summary(self.band)}",
                rule="NV-W003",
                path=("channel",),
            )
        if self.width_mhz is not None and self.width_mhz not in self.band.widths:
            allowed = ", ".join(str(width) for width in sorted(self.band.widths))
            raise field_error(
                f"width_mhz {self.width_mhz} is not available in the {self.band.value} band; "
                f"it supports {allowed} MHz",
                rule="NV-W004",
                path=("width_mhz",),
            )
        return self

    @model_validator(mode="after")
    def _check_bss(self) -> WirelessConfig:
        """``NV-W005``/``NV-W006``: the BSS list fits the role it is written on."""
        if self.role.is_client and len(self.bss) > 1:
            raise field_error(
                f"a {self.role.value!r} radio associates to one BSS at a time, "
                f"but {len(self.bss)} are listed",
                rule="NV-W006",
                path=("bss",),
            )

        ssids: set[str] = set()
        bssids: set[str] = set()
        for index, entry in enumerate(self.bss):
            if entry.ssid in ssids:
                raise field_error(
                    f"SSID {entry.ssid!r} is declared twice on this radio",
                    rule="NV-W005",
                    path=("bss", index, "ssid"),
                )
            ssids.add(entry.ssid)
            if entry.bssid is not None:
                if entry.bssid in bssids:
                    raise field_error(
                        f"BSSID {entry.bssid} is declared twice on this radio",
                        rule="NV-W005",
                        path=("bss", index, "bssid"),
                    )
                bssids.add(entry.bssid)
        return self

    @property
    def ssids(self) -> tuple[str, ...]:
        """Every SSID this radio beacons or is associated to, in order."""
        return tuple(entry.ssid for entry in self.bss)

    @property
    def channel_text(self) -> str | None:
        """``36/5GHz``, the way a diagram labels a link; ``None`` if unstated."""
        if self.band is None:
            return None
        if self.channel is None:
            return self.band.value
        return f"{self.channel}/{self.band.value}"

    def span_mhz(self) -> tuple[float, float] | None:
        """The frequency range the radio occupies, as ``(low, high)`` in MHz.

        Centred on the primary channel, because that is all the schema records:
        a bonded channel's real centre depends on which secondary channels the
        radio picked, which no document states. The approximation is what
        ``W134`` compares, and it can only make the overlap test *more* willing
        to warn — never less.
        """
        if self.band is None or self.channel is None:
            return None
        centre = self.band.centre_mhz(self.channel)
        half = (self.width_mhz or 20) / 2
        return centre - half, centre + half


def _channel_summary(band: Band) -> str:
    """``1-14`` / ``32-177``, for the diagnostic of an out-of-plan channel."""
    channels = sorted(band.channels)
    return f"{channels[0]}-{channels[-1]}"


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #


class Interface(NetvizModel):
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
    #: The routing instance this interface belongs to (§16.1). Names an entry of
    #: the device's ``spec.vrfs`` (``NV-F002``); unset means the global instance,
    #: and *that* is what partitions the address namespace: an address is only
    #: in conflict with another address in the same VRF.
    vrf: ElementName | None = None
    #: Radio configuration; ``wifi`` interfaces only (§6.2.6).
    wireless: WirelessConfig | None = None
    #: Power sourcing equipment configuration; a port that hands power down the
    #: cable (§17.3). Only on a type that terminates one (``NV-E006``).
    poe: PoeConfig | None = None
    #: ``if:lower-layer-if`` of a ``vlan`` sub-interface.
    parent: IfName | None = None
    #: ``if:lower-layer-if`` of a ``lag`` or ``bridge`` interface.
    members: list[IfName] | None = None
    #: The network namespace this interface lives in (§23). Names an entry of
    #: the device's ``spec.netns`` (``NV-N022``); unset means the device's
    #: initial namespace. This is the *whole* stack the interface is in, not
    #: just its routing table — see :attr:`vrf`, which partitions the second.
    netns: ElementName | None = None
    #: The other end of the veth pair this interface is one end of (§23.2).
    #: Names another ``type: ethernet`` interface of the same element, which
    #: must name this one back (``NV-N023``). Unset means an ordinary port.
    peer: IfName | None = None

    @model_validator(mode="after")
    def _check_veth(self) -> Interface:
        """``NV-N023``: only an ethernet interface is one end of a veth pair.

        A veth end is ``ianaift:ethernetCsmacd`` and nothing else — that is the
        point of §23.2 — so ``peer`` on a ``loopback``, a ``bridge``, a ``vlan``
        sub-interface or a ``tunnel`` is not a veth pair described unusually, it
        is a kind of link that does not exist. ``wifi`` and ``lag`` are refused
        for the same reason even though a cable may terminate on them: a radio
        has no far end inside the machine, and an aggregate of veths is
        expressed by aggregating the two ends, not by pairing the bond.

        Symmetry, and that the peer exists at all, need the rest of the element
        in view and are checked by :func:`~netviz.models.device.check_interface_set`.
        """
        if self.peer is None:
            return self
        if self.type is not InterfaceType.ETHERNET:
            raise field_error(
                f"'peer' makes this interface one end of a veth pair, which is a pair of "
                f"'ethernet' interfaces (schema §23.2), not {self.type.value!r}",
                rule="NV-N023",
                path=("peer",),
            )
        if self.peer == self.name:
            raise field_error(
                "'peer' must not be the interface itself: a veth pair has two ends",
                rule="NV-N023",
                path=("peer",),
            )
        return self

    @model_validator(mode="after")
    def _check_stacking(self) -> Interface:
        if self.type is InterfaceType.VLAN:
            if self.parent is None:
                raise field_error(
                    "'parent' is required for type 'vlan'", rule="NV-I002", path=("parent",)
                )
            if self.parent == self.name:
                raise field_error(
                    "'parent' must not be the interface itself",
                    rule="NV-I002",
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
                    rule="NV-I002",
                    path=("parent",),
                )
            if self.parent == self.name:
                raise field_error(
                    "'parent' must not be the interface itself",
                    rule="NV-I002",
                    path=("parent",),
                )

        if self.type in AGGREGATE_TYPES:
            if self.members is None:
                raise field_error(
                    f"'members' is required for type {self.type.value!r}",
                    rule="NV-I003",
                    path=("members",),
                )
            if not self.members:
                raise field_error("'members' is empty", rule="NV-I003", path=("members",))
            duplicates = _duplicates(self.members)
            if duplicates:
                raise field_error(
                    f"duplicate members: {', '.join(duplicates)}",
                    rule="NV-I003",
                    path=("members",),
                )
            if self.name in self.members:
                raise field_error(
                    f"{self.name!r} lists itself as a member",
                    rule="NV-I003",
                    path=("members",),
                )
        elif self.members is not None:
            raise field_error(
                f"'members' is only allowed for types 'lag' and 'bridge', not {self.type.value!r}",
                rule="NV-I003",
                path=("members",),
            )

        return self

    @model_validator(mode="after")
    def _check_wireless(self) -> Interface:
        """``NV-W002``: only a radio has a radio configuration."""
        if self.wireless is not None and self.type is not InterfaceType.WIFI:
            raise field_error(
                f"'wireless' is only allowed for type 'wifi', not {self.type.value!r}",
                rule="NV-W002",
                path=("wireless",),
            )
        return self

    @model_validator(mode="after")
    def _check_poe(self) -> Interface:
        """``NV-E006``: only a port a cable lands on can hand power down it.

        PoE travels over the twisted pairs of an ethernet run, so a ``loopback``,
        a ``vlan`` sub-interface, a ``bridge`` or a ``tunnel`` cannot source it —
        there is no copper. ``wifi`` is refused for the same reason and one
        further one: a radio is exactly the thing that has no cable.
        """
        if self.poe is not None and self.type not in _POE_TYPES:
            permitted = ", ".join(sorted(itype.value for itype in _POE_TYPES))
            raise field_error(
                f"'poe' is only allowed on a port a cable terminates on ({permitted}), "
                f"not on type {self.type.value!r}",
                rule="NV-E006",
                path=("poe",),
            )
        return self

    @model_validator(mode="after")
    def _check_mtu(self) -> Interface:
        """``NV-I011``: an interface with IPv6 addresses needs at least 1280 bytes."""
        if self.mtu is not None and self.mtu < IPV6_MIN_MTU and self.has_ipv6_addresses:
            raise field_error(
                f"mtu {self.mtu} is below the IPv6 minimum of {IPV6_MIN_MTU} but the "
                "interface carries IPv6 addresses",
                rule="NV-I011",
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
        """``NV-C009``: only physical and aggregate interfaces can be cabled."""
        return self.type.is_cableable

    @property
    def is_veth(self) -> bool:
        """Is this one end of a veth pair (§23.2)?

        Asked wherever a rule is about a *socket* rather than about a port: a
        veth end is cableable by type and uncabled by nature, so ``I002`` and
        ``NV-N024`` both have to be able to tell the two apart.
        """
        return self.peer is not None

    @property
    def netns_name(self) -> str:
        """The network namespace this interface is in, ``""`` for the initial one.

        The normalised form of :attr:`netns`: every consumer wants a string it
        can group by, and ``None`` and ``""`` meaning the same namespace would
        make two of them.

        Deliberately not called ``namespace``. That word already means the
        *folder* namespace of §2.2 everywhere else in this codebase — it is what
        :attr:`~netviz.render.graph.Node.namespace` holds — and two unrelated
        things under one name on neighbouring objects is how a bug gets written.
        """
        return self.netns or ""

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
