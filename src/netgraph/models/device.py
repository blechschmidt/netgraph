"""Device elements: ``switch``, ``router``, ``hub``, ``computer`` and ``server``.

The five kinds share one ``spec`` shape (§6.1 of ``docs/schema.md``); they
differ in which fields are permitted (§6.5) and in how the renderer draws them.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import Enum
from typing import ClassVar, Final, Literal

from pydantic import Field, model_validator

from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.element import ElementBase
from netgraph.models.interface import Interface, InterfaceList, InterfaceType
from netgraph.models.routing import RoutingConfig, StaticRoute, VrfDefinition
from netgraph.models.scalars import Boolean, ElementName, MacAddress, VlanId

__all__ = [
    "DEVICE_KINDS",
    "GLOBAL_VRF",
    "BridgeConfig",
    "BridgeType",
    "Computer",
    "Device",
    "DeviceSpec",
    "Forwarding",
    "Hub",
    "Router",
    "Server",
    "Switch",
    "VlanDefinition",
]

#: The name of the instance an interface that binds to no VRF is in. The empty
#: string rather than ``"default"`` or ``None``: it is not a name anybody may
#: declare (``ElementName`` requires at least one character), so it cannot be
#: shadowed by a VRF called ``global``, and it sorts before every real name — so
#: the global instance leads every listing that groups by VRF.
GLOBAL_VRF: Final = ""


class BridgeType(str, Enum):
    """``dot1q:bridge/bridge-type`` identities (§6.3)."""

    CUSTOMER_VLAN = "customer-vlan-bridge"
    PROVIDER = "provider-bridge"
    PROVIDER_EDGE = "provider-edge-bridge"
    TWO_PORT_MAC_RELAY = "two-port-mac-relay-bridge"
    MAC = "mac-bridge"

    @property
    def port_type(self) -> str:
        """``dot1q:port-type`` implied by this bridge type (§9.3)."""
        return "dot1q:d-bridge-port" if self is BridgeType.MAC else "dot1q:c-vlan-bridge-port"


class Forwarding(NetgraphModel):
    """``spec.forwarding`` — the device-wide default for ``ip:*/forwarding``."""

    ipv4: Boolean
    ipv6: Boolean


class BridgeConfig(NetgraphModel):
    """``spec.bridge`` — the 802.1Q bridge component (§6.3)."""

    #: ``dot1q:bridge/name``; defaults to ``metadata.name``.
    name: ElementName | None = None
    type: BridgeType = BridgeType.CUSTOMER_VLAN
    #: ``dot1q:bridge/address``.
    address: MacAddress | None = None


class VlanDefinition(NetgraphModel):
    """One entry of ``spec.vlans`` — the device VLAN database (§6.4)."""

    id: VlanId
    #: ``dot1q:vlan/name`` (``dot1qtypes:name-type``).
    name: str | None = Field(default=None, max_length=32)
    description: str | None = None


class DeviceSpec(NetgraphModel):
    """``spec`` of every device kind (§6.1)."""

    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    location: str | None = None
    interfaces: InterfaceList
    bridge: BridgeConfig | None = None
    vlans: list[VlanDefinition] = Field(default_factory=list)
    #: ``None`` until the per-kind default of §6.1.1 is applied by the element.
    forwarding: Forwarding | None = None
    #: The routing instances this device implements (§16.1).
    vrfs: list[VrfDefinition] = Field(default_factory=list)
    #: Configured static routes (§16.2).
    routes: list[StaticRoute] = Field(default_factory=list)
    #: The dynamic routing protocols the device takes part in (§16.3).
    routing: RoutingConfig | None = None

    @model_validator(mode="after")
    def _check_interfaces(self) -> DeviceSpec:
        check_interface_set(self.interfaces)
        return self

    @model_validator(mode="after")
    def _check_vrf_table(self) -> DeviceSpec:
        """``NG-F001``/``NG-F002``/``NG-F005``: the VRF table and its references.

        A VRF is referred to by name from two places in the same ``spec`` — an
        interface binds to one, a route is placed in one — so the reference is
        resolved here, where both are in view. Everything that reaches *outside*
        the document (an interface a route sends out of, an OSPF interface, a BGP
        peer) is the validator's business instead.
        """
        declared: set[str] = set()
        for index, vrf in enumerate(self.vrfs):
            if vrf.name in declared:
                raise field_error(
                    f"VRF {vrf.name!r} is declared twice",
                    rule="NG-F001",
                    path=("vrfs", index, "name"),
                )
            declared.add(vrf.name)

        for index, interface in enumerate(self.interfaces):
            if interface.vrf is not None and interface.vrf not in declared:
                raise field_error(
                    f"{interface.name!r} binds to VRF {interface.vrf!r}, which "
                    f"{_vrf_table(declared)}",
                    rule="NG-F002",
                    path=("interfaces", index, "vrf"),
                )

        for index, route in enumerate(self.routes):
            if route.vrf is not None and route.vrf not in declared:
                raise field_error(
                    f"route {route.prefix} is placed in VRF {route.vrf!r}, which "
                    f"{_vrf_table(declared)}",
                    rule="NG-F005",
                    path=("routes", index, "vrf"),
                )
        return self

    @model_validator(mode="after")
    def _check_vlan_database(self) -> DeviceSpec:
        """``NG-V001``: ``vlans[].id`` is unique within a device."""
        seen: set[int] = set()
        for index, vlan in enumerate(self.vlans):
            if vlan.id in seen:
                raise field_error(
                    f"VLAN {vlan.id} is declared twice",
                    rule="NG-V001",
                    path=("vlans", index, "id"),
                )
            seen.add(vlan.id)
        return self

    def interface(self, name: str) -> Interface | None:
        """Look an interface up by name."""
        return next((itf for itf in self.interfaces if itf.name == name), None)

    def vlan(self, vlan_id: int) -> VlanDefinition | None:
        """Look a VLAN up in the device VLAN database."""
        return next((vlan for vlan in self.vlans if vlan.id == vlan_id), None)

    def vrf(self, name: str) -> VrfDefinition | None:
        """Look a routing instance up by name (§16.1)."""
        return next((vrf for vrf in self.vrfs if vrf.name == name), None)

    def interfaces_in(self, vrf: str) -> tuple[Interface, ...]:
        """Every interface bound to ``vrf``, in declaration order.

        :data:`GLOBAL_VRF` selects the interfaces that bind to no VRF at all,
        which is the instance every address is in until something says otherwise.
        """
        wanted = vrf or None
        return tuple(interface for interface in self.interfaces if interface.vrf == wanted)


def _vrf_table(declared: Iterable[str]) -> str:
    """``is not declared in 'spec.vrfs' (which holds …)`` — the tail of NG-F002."""
    names = sorted(declared)
    if not names:
        return "is not declared: the device declares no 'spec.vrfs' at all"
    return f"is not declared in 'spec.vrfs'; it holds {', '.join(repr(name) for name in names)}"


def check_interface_set(interfaces: Iterable[Interface], *, reserved: Iterable[str] = ()) -> None:
    """Check name uniqueness and stacking references within one element.

    ``NG-I001``: interface names are unique within their device. ``NG-I002`` /
    ``NG-I003``: ``parent`` and ``members`` resolve to interfaces on the same
    device. ``reserved`` holds extra names that are taken but are not entries of
    the list itself (the adapter upstream port, ``NG-X004``).
    """
    reserved_names = set(reserved)
    names: set[str] = set()
    entries = list(interfaces)
    for index, interface in enumerate(entries):
        if interface.name in reserved_names:
            raise field_error(
                f"interface name {interface.name!r} collides with the upstream port",
                rule="NG-X004",
                path=("interfaces", index, "name"),
            )
        if interface.name in names:
            raise field_error(
                f"interface name {interface.name!r} is declared twice",
                rule="NG-I001",
                path=("interfaces", index, "name"),
            )
        names.add(interface.name)

    known = names | reserved_names
    for index, interface in enumerate(entries):
        for referenced in interface.lower_layer_if:
            if referenced not in known:
                key = "parent" if interface.parent is not None else "members"
                raise field_error(
                    f"{interface.name!r} references unknown interface {referenced!r}",
                    rule="NG-I002" if key == "parent" else "NG-I003",
                    path=("interfaces", index, key),
                )


class Device(ElementBase):
    """Base class of the five device kinds.

    Subclasses only differ in class-level policy: the ``spec.forwarding``
    default (§6.1.1), whether layer-2/layer-3 configuration is permitted at all
    (§6.5) and the glyph the renderer picks by default.
    """

    spec: DeviceSpec

    #: §6.1.1 — routers forward by default, everything else does not.
    forwarding_default: ClassVar[bool] = False
    #: §6.5 — a hub is a layer-1 repeater and rejects VLAN configuration.
    vlan_aware: ClassVar[bool] = True
    #: §6.5 — a hub has no IP stack.
    layer3_aware: ClassVar[bool] = True
    #: §6.5 — the renderer's default node glyph.
    default_glyph: ClassVar[str] = "device"
    #: Interface types this kind accepts; ``None`` means "every type".
    allowed_interface_types: ClassVar[frozenset[InterfaceType] | None] = None

    @model_validator(mode="after")
    def _apply_kind_policy(self) -> Device:
        self._check_kind_constraints()
        self._apply_defaults()
        return self

    def _check_kind_constraints(self) -> None:
        """§6.5 / ``NG-H001`` to ``NG-H004``."""
        if not self.vlan_aware:
            for key in ("bridge", "vlans"):
                if getattr(self.spec, key):
                    raise field_error(
                        f"a {self.kind} is a layer-1 repeater and has no {key!r}",
                        rule="NG-H003",
                        path=("spec", key),
                    )
            for index, interface in enumerate(self.spec.interfaces):
                if interface.vlan is not None:
                    raise field_error(
                        f"a {self.kind} interface must not declare 'vlan'",
                        rule="NG-H001",
                        path=("spec", "interfaces", index, "vlan"),
                    )

        if not self.layer3_aware:
            for key in ("forwarding", "vrfs", "routes", "routing"):
                if getattr(self.spec, key):
                    raise field_error(
                        f"a {self.kind} has no IP stack and must not declare {key!r}",
                        rule="NG-H003",
                        path=("spec", key),
                    )
            for index, interface in enumerate(self.spec.interfaces):
                for family in ("ipv4", "ipv6"):
                    if getattr(interface, family) is not None:
                        raise field_error(
                            f"a {self.kind} interface must not declare {family!r}",
                            rule="NG-H002",
                            path=("spec", "interfaces", index, family),
                        )
                if interface.vrf is not None:
                    raise field_error(
                        f"a {self.kind} interface has no IP stack, so it is in no VRF",
                        rule="NG-H002",
                        path=("spec", "interfaces", index, "vrf"),
                    )

        allowed = self.allowed_interface_types
        if allowed is not None:
            for index, interface in enumerate(self.spec.interfaces):
                if interface.type not in allowed:
                    permitted = ", ".join(sorted(itype.value for itype in allowed))
                    raise field_error(
                        f"{interface.name!r} is of type {interface.type.value!r}; "
                        f"a {self.kind} only supports {permitted}",
                        rule="NG-H004",
                        path=("spec", "interfaces", index, "type"),
                    )

    def _apply_defaults(self) -> None:
        """§1 — the loader materialises defaults so the model is fully resolved."""
        if self.layer3_aware and self.spec.forwarding is None:
            self.spec.forwarding = Forwarding(
                ipv4=self.forwarding_default, ipv6=self.forwarding_default
            )
        if self.spec.bridge is not None and self.spec.bridge.name is None:
            self.spec.bridge.name = self.metadata.name
        resolve_address_family_defaults(self.spec.interfaces, self.spec.forwarding)

    @property
    def interfaces(self) -> list[Interface]:
        """Shortcut for ``spec.interfaces``."""
        return self.spec.interfaces

    def interface(self, name: str) -> Interface | None:
        """Look an interface up by name."""
        return self.spec.interface(name)

    def interface_names(self) -> Iterator[str]:
        """Every name a cable endpoint may refer to on this element."""
        for interface in self.spec.interfaces:
            yield interface.name


def resolve_address_family_defaults(
    interfaces: Iterable[Interface], forwarding: Forwarding | None
) -> None:
    """Fill in the ``ipv4``/``ipv6`` defaults inherited from the element (§6.2.3).

    ``forwarding`` supplies the device-wide default for ``ip:*/forwarding``; the
    interface MTU supplies the default for ``ip:*/mtu``. A layer-2 MTU below the
    IPv6 minimum is not propagated to IPv6 (§9.2).
    """
    for interface in interfaces:
        if interface.ipv4 is not None:
            if interface.ipv4.forwarding is None:
                interface.ipv4.forwarding = forwarding.ipv4 if forwarding else False
            if interface.ipv4.mtu is None and interface.mtu is not None:
                interface.ipv4.mtu = interface.mtu
        if interface.ipv6 is not None:
            if interface.ipv6.forwarding is None:
                interface.ipv6.forwarding = forwarding.ipv6 if forwarding else False
            if interface.ipv6.mtu is None and interface.mtu is not None and interface.mtu >= 1280:
                interface.ipv6.mtu = interface.mtu


class Switch(Device):
    """A VLAN-aware layer-2 bridge."""

    kind: Literal["switch"] = "switch"
    default_glyph: ClassVar[str] = "switch"


class Router(Device):
    """A layer-3 forwarder. Forwards by default (§6.1.1)."""

    kind: Literal["router"] = "router"
    forwarding_default: ClassVar[bool] = True
    default_glyph: ClassVar[str] = "router"


class Hub(Device):
    """A layer-1 repeater: no MAC table, no VLANs, no IP stack (§6.5)."""

    kind: Literal["hub"] = "hub"
    vlan_aware: ClassVar[bool] = False
    layer3_aware: ClassVar[bool] = False
    default_glyph: ClassVar[str] = "hub"
    allowed_interface_types: ClassVar[frozenset[InterfaceType] | None] = frozenset(
        {InterfaceType.ETHERNET}
    )


class Computer(Device):
    """An end host, drawn as a workstation."""

    kind: Literal["computer"] = "computer"
    default_glyph: ClassVar[str] = "workstation"


class Server(Device):
    """An end host, drawn as a rack-mount server. Structurally a computer."""

    kind: Literal["server"] = "server"
    default_glyph: ClassVar[str] = "server"


#: The kinds whose ``spec`` is a :class:`DeviceSpec`, and therefore the kinds a
#: ``template`` can be merged into (§6.6). In the order §3 lists them.
DEVICE_KINDS: tuple[str, ...] = ("switch", "router", "hub", "computer", "server")
