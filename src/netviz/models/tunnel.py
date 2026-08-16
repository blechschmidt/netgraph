"""The ``tunnel`` element (§14 of ``docs/schema.md``).

A tunnel is to a logical topology what a cable is to a physical one: an
undirected link between interfaces that exists because two endpoints agreed to
encapsulate each other's traffic, not because anyone ran a wire. It is a
first-class element for the same reason a cable is — it carries its own
metadata, has its own identity, and is validated independently of the devices it
joins.

Two things make a tunnel more than a cable with a different colour:

*Type.* WireGuard, IPsec, OpenVPN, PPTP, L2TP, GRE, VXLAN and Geneve differ in
which layer they carry, what they run over, whether they encrypt, and how much
of the path MTU they consume. :class:`TunnelType` holds those facts once
(:data:`_PROFILES`), so defaults are materialised on load (§1) and the validator
can reason about them rather than about a free-text string.

*Nesting.* ``spec.over`` names the tunnel this one is encapsulated in, which is
how ``VXLAN over IPsec`` is written. The reference is resolved and the chain
walked by the graph layer, so the rendering knows the full stack
(``vxlan over ipsec``) and the validator can catch a loop (``NV-T005``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import Field, model_validator

from netviz.models.base import NetvizModel
from netviz.models.cable import InterfaceRef, sort_endpoints
from netviz.models.diagnostics import field_error
from netviz.models.element import ElementBase
from netviz.models.scalars import ElementRef, InterfaceMtu
from netviz.models.style import Style

__all__ = [
    "MAX_VNI",
    "Tunnel",
    "TunnelAuth",
    "TunnelMode",
    "TunnelSpec",
    "TunnelTransport",
    "TunnelType",
    "VirtualNetworkId",
]

#: Largest VXLAN/Geneve VNI: the identifier is a 24-bit field.
MAX_VNI: Final = 16_777_215

#: ``spec.vni`` — the 24-bit VXLAN/Geneve virtual network identifier.
VirtualNetworkId = Annotated[int, Field(strict=True, ge=0, le=MAX_VNI)]

#: ``spec.port`` — a UDP or TCP port number.
PortNumber = Annotated[int, Field(strict=True, ge=1, le=65535)]


class TunnelTransport(str, Enum):
    """What a tunnel's outer packets are, i.e. what a firewall has to pass."""

    UDP = "udp"
    TCP = "tcp"
    #: IP protocol 47.
    GRE = "gre"
    #: IP protocol 50 (ESP); IKE itself is UDP/500 and is not modelled here.
    ESP = "esp"

    @property
    def has_port(self) -> bool:
        """Does this transport carry a port number at all?"""
        return self in (TunnelTransport.UDP, TunnelTransport.TCP)

    def __str__(self) -> str:
        return self.value


class TunnelMode(str, Enum):
    """``spec.mode`` — IPsec's two encapsulation modes (RFC 4301 §3.2)."""

    #: The whole inner IP packet is encapsulated; the endpoints may be gateways.
    TUNNEL = "tunnel"
    #: Only the payload is protected; the endpoints are the communicating hosts.
    TRANSPORT = "transport"

    def __str__(self) -> str:
        return self.value


class TunnelAuth(str, Enum):
    """``spec.auth`` — how the two ends prove who they are.

    Deliberately an *enumeration of methods*, never a place to put material: a
    key, a password or a certificate in an inventory is a secret in version
    control, and netviz has no use for one.
    """

    #: A pre-shared secret (IPsec PSK, OpenVPN ``--secret``).
    PSK = "psk"
    #: X.509 certificates (IKEv2 RSA/ECDSA, OpenVPN TLS).
    CERTIFICATE = "certificate"
    #: Raw public keys, as WireGuard uses.
    PUBLIC_KEY = "public-key"
    #: A user name and password (PPTP/MS-CHAPv2, L2TP over PPP).
    PASSWORD = "password"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class _Profile:
    """The fixed facts about one tunnel technology."""

    #: 2 for a tunnel that carries frames, 3 for one that carries packets.
    layer: int
    transport: TunnelTransport
    #: Registered port, or ``None`` for a transport that has none.
    port: int | None
    #: Does the technology encrypt by itself?
    encrypted: bool
    #: Typical worst-case bytes the encapsulation takes off the path MTU, over
    #: IPv4. Used by ``W126`` to notice an overlay MTU the underlay cannot carry.
    overhead: int
    #: Does the type carry a VXLAN/Geneve-style virtual network identifier?
    has_vni: bool = False
    #: Is ``spec.mode`` (tunnel/transport) meaningful for this type?
    has_mode: bool = False


class TunnelType(str, Enum):
    """``spec.type`` — the encapsulation the tunnel uses (§14.1)."""

    WIREGUARD = "wireguard"
    IPSEC = "ipsec"
    OPENVPN = "openvpn"
    PPTP = "pptp"
    L2TP = "l2tp"
    GRE = "gre"
    VXLAN = "vxlan"
    GENEVE = "geneve"

    @property
    def profile(self) -> _Profile:
        """The fixed facts about this technology."""
        return _PROFILES[self]

    @property
    def layer(self) -> int:
        """2 when the tunnel carries ethernet frames, 3 when it carries packets."""
        return self.profile.layer

    @property
    def transport(self) -> TunnelTransport:
        """What the outer packets are."""
        return self.profile.transport

    @property
    def default_port(self) -> int | None:
        """The registered port, or ``None`` for GRE and ESP."""
        return self.profile.port

    @property
    def encrypts(self) -> bool:
        """Does the technology protect the payload on its own?

        ``False`` for GRE, VXLAN, Geneve, L2TP and PPTP: those need an
        encrypting underlay (``spec.over``) to be confidential, which is what
        ``W127`` is about. PPTP is listed as unencrypted deliberately — MPPE is
        broken, so a PPTP tunnel is a cleartext tunnel.
        """
        return self.profile.encrypted

    @property
    def overhead_bytes(self) -> int:
        """Typical bytes of encapsulation over IPv4; see :attr:`_Profile.overhead`."""
        return self.profile.overhead

    @property
    def iana_if_type(self) -> str:
        """The ``iana-if-type`` identity a tunnel interface maps to (§14.4)."""
        return "ianaift:tunnel"

    def __str__(self) -> str:
        return self.value


#: Per-type facts. The overheads are the widely published worst case over IPv4 —
#: the number an operator would set an overlay MTU from — not an exact packet
#: layout, which varies with cipher, IP version and NAT traversal.
_PROFILES: Final[dict[TunnelType, _Profile]] = {
    TunnelType.WIREGUARD: _Profile(
        layer=3, transport=TunnelTransport.UDP, port=51820, encrypted=True, overhead=80
    ),
    TunnelType.IPSEC: _Profile(
        layer=3,
        transport=TunnelTransport.ESP,
        port=None,
        encrypted=True,
        overhead=73,
        has_mode=True,
    ),
    TunnelType.OPENVPN: _Profile(
        layer=3, transport=TunnelTransport.UDP, port=1194, encrypted=True, overhead=69
    ),
    TunnelType.PPTP: _Profile(
        layer=3, transport=TunnelTransport.GRE, port=None, encrypted=False, overhead=40
    ),
    TunnelType.L2TP: _Profile(
        layer=2,
        transport=TunnelTransport.UDP,
        port=1701,
        encrypted=False,
        overhead=40,
        has_mode=True,
    ),
    TunnelType.GRE: _Profile(
        layer=3, transport=TunnelTransport.GRE, port=None, encrypted=False, overhead=24
    ),
    TunnelType.VXLAN: _Profile(
        layer=2,
        transport=TunnelTransport.UDP,
        port=4789,
        encrypted=False,
        overhead=50,
        has_vni=True,
    ),
    TunnelType.GENEVE: _Profile(
        layer=2,
        transport=TunnelTransport.UDP,
        port=6081,
        encrypted=False,
        overhead=50,
        has_vni=True,
    ),
}


class TunnelSpec(NetvizModel):
    """``spec`` of a ``tunnel`` document (§14)."""

    type: TunnelType
    #: Two or more ``device:interface`` references, each naming a ``tunnel``
    #: interface (``NV-T001``, ``NV-T003``); sorted for canonical output.
    endpoints: list[InterfaceRef]
    #: The tunnel this one runs inside, e.g. a VXLAN's IPsec underlay
    #: (``NV-T004``). Absent means the tunnel runs directly over the physical
    #: topology.
    over: ElementRef | None = None
    #: IPsec's encapsulation mode; materialised to ``tunnel`` on load.
    mode: TunnelMode | None = None
    #: The VXLAN/Geneve virtual network identifier. Required for those two types
    #: and rejected for every other (``NV-T007``).
    vni: VirtualNetworkId | None = None
    #: Outer port. Materialised from the registered default of the type on load;
    #: rejected for GRE and ESP, which carry no port (``NV-T008``).
    port: PortNumber | None = None
    #: MTU of the tunnel interface. ``W126`` compares it with what the underlay
    #: leaves after :attr:`TunnelType.overhead_bytes`.
    mtu: InterfaceMtu | None = None
    #: Whether the payload is protected. Materialised from the type on load; it
    #: may be set to ``true`` on an otherwise cleartext type to record that the
    #: deployment adds its own protection.
    encrypted: bool | None = None
    #: Negotiated cipher suite, free text (``chacha20-poly1305``, ``aes-256-gcm``).
    cipher: str | None = None
    #: How the endpoints authenticate each other. Never key material (§14.2).
    auth: TunnelAuth | None = None
    #: Free-text identifier printed on the edge, as a cable's ``label`` is.
    label: str | None = None
    #: How this element is drawn (§22): a ``fill``, a ``stroke``, a ``shape``
    #: and six more, each optional and each inheriting from the theme, then the
    #: icon set, then the built-in palette when absent. See
    #: :mod:`netviz.models.style`.
    style: Style | None = None

    @model_validator(mode="after")
    def _normalise(self) -> TunnelSpec:
        self._check_endpoints()
        self._check_type_specific()
        self._apply_defaults()
        return self

    def _check_endpoints(self) -> None:
        if len(self.endpoints) < 2:
            raise field_error(
                f"a tunnel joins at least two interfaces, got {len(self.endpoints)}",
                rule="NV-T001",
                path=("endpoints",),
            )
        seen: set[tuple[str, str]] = set()
        for index, ref in enumerate(self.endpoints):
            if ref.sort_key in seen:
                raise field_error(
                    f"endpoint {str(ref)!r} is listed twice",
                    rule="NV-T001",
                    path=("endpoints", index),
                )
            seen.add(ref.sort_key)
        # §14.3: like a cable, a tunnel is undirected, so the endpoint order
        # carries no meaning. Sorting makes the graph edge and the JSON export
        # canonical.
        sort_endpoints(self.endpoints)

    def _check_type_specific(self) -> None:
        profile = self.type.profile
        if profile.has_vni and self.vni is None:
            raise field_error(
                f"a {self.type} tunnel is identified by its 'vni'",
                rule="NV-T007",
                path=("vni",),
            )
        if not profile.has_vni and self.vni is not None:
            raise field_error(
                f"'vni' is a VXLAN/Geneve identifier and has no meaning for {self.type}",
                rule="NV-T007",
                path=("vni",),
            )
        if not profile.has_mode and self.mode is not None:
            raise field_error(
                f"'mode' distinguishes IPsec's tunnel and transport modes; {self.type} "
                f"has only one",
                rule="NV-T008",
                path=("mode",),
            )
        if not profile.transport.has_port and self.port is not None:
            raise field_error(
                f"{self.type} runs directly over IP as {profile.transport}, so it carries no port",
                rule="NV-T008",
                path=("port",),
            )
        if not self.encrypts and self.cipher is not None:
            raise field_error(
                f"'cipher' describes an encrypted tunnel; {self.type} encrypts nothing "
                f"unless 'encrypted: true' says the deployment adds it",
                rule="NV-T009",
                path=("cipher",),
            )
        if self.auth is not None and not (profile.encrypted or self.encrypted):
            raise field_error(
                f"{self.type} authenticates no peer, so 'auth' has nothing to describe",
                rule="NV-T009",
                path=("auth",),
            )

    def _apply_defaults(self) -> None:
        """§1 — the loader materialises defaults so the model is fully resolved."""
        profile = self.type.profile
        if self.encrypted is None:
            self.encrypted = profile.encrypted
        if self.mode is None and profile.has_mode:
            self.mode = TunnelMode.TUNNEL
        if self.port is None and profile.port is not None:
            self.port = profile.port

    @property
    def encrypts(self) -> bool:
        """Resolved confidentiality: the type's default, or what the document says."""
        return self.type.profile.encrypted if self.encrypted is None else self.encrypted


class Tunnel(ElementBase):
    """An undirected logical link between two or more ``tunnel`` interfaces."""

    kind: Literal["tunnel"] = "tunnel"
    spec: TunnelSpec

    has_interfaces: ClassVar[bool] = False
    default_glyph: ClassVar[str] = "tunnel"

    @model_validator(mode="before")
    @classmethod
    def _reject_secrets(cls, value: Any) -> Any:
        """Refuse the key material people reach for out of habit.

        ``extra="forbid"`` already rejects these, but the message it produces
        ("unknown key 'private_key'") reads as an oversight in the schema rather
        than as the deliberate refusal it is.
        """
        if not isinstance(value, dict):
            return value
        spec = value.get("spec")
        if not isinstance(spec, dict):
            return value
        for key in _SECRET_KEYS:
            if key in spec:
                raise field_error(
                    f"{key!r} is key material; netviz describes topology and never stores "
                    f"secrets. Use 'auth' to record the authentication method instead",
                    rule="NV-T010",
                    path=("spec", key),
                )
        return value

    @property
    def endpoints(self) -> list[InterfaceRef]:
        """Shortcut for ``spec.endpoints``."""
        return self.spec.endpoints

    @property
    def type(self) -> TunnelType:
        """Shortcut for ``spec.type``."""
        return self.spec.type

    @property
    def is_multipoint(self) -> bool:
        """Does the tunnel join more than two endpoints (a VXLAN mesh, a VPN hub)?"""
        return len(self.spec.endpoints) > 2

    @property
    def encrypts(self) -> bool:
        """Is the payload protected by the tunnel itself?"""
        return self.spec.encrypts

    def other_ends(self, ref: InterfaceRef) -> list[InterfaceRef]:
        """Every endpoint that is not ``ref``.

        Raises:
            KeyError: ``ref`` is not an endpoint of this tunnel.
        """
        rest = [endpoint for endpoint in self.spec.endpoints if endpoint != ref]
        if len(rest) == len(self.spec.endpoints):
            raise KeyError(f"{ref} is not an endpoint of tunnel {self.metadata.name!r}")
        return rest


#: Keys that would hold a secret. Refused with an explanation; see
#: :meth:`Tunnel._reject_secrets`.
_SECRET_KEYS: Final[tuple[str, ...]] = (
    "private_key",
    "preshared_key",
    "psk",
    "password",
    "secret",
    "passphrase",
)
