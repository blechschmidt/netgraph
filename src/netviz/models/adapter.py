"""The ``adapter`` element (§8 of ``docs/schema.md``).

An adapter presents one or more network interfaces over a non-network host
port: USB-to-Ethernet dongles, Thunderbolt docks, media converters.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from typing import ClassVar, Final, Literal

from pydantic import model_validator

from netviz.models.base import NetvizModel
from netviz.models.device import check_interface_set, resolve_address_family_defaults
from netviz.models.diagnostics import field_error
from netviz.models.element import ElementBase
from netviz.models.interface import Interface, InterfaceList, InterfaceType
from netviz.models.scalars import BitRate, Boolean, ElementRef, IfName, PortCount
from netviz.models.style import Style

__all__ = ["Adapter", "AdapterSpec", "UpstreamPort", "UpstreamType"]

#: ``NG-X003`` — the interface types an adapter may present downstream.
ADAPTER_INTERFACE_TYPES: Final[frozenset[InterfaceType]] = frozenset(
    {InterfaceType.ETHERNET, InterfaceType.WIFI, InterfaceType.LAG}
)


class UpstreamType(str, Enum):
    """``spec.upstream.type`` — the host-side bus (§8.1)."""

    USB = "usb"
    USB_C = "usb-c"
    THUNDERBOLT = "thunderbolt"
    PCIE = "pcie"
    M2 = "m2"
    SFP = "sfp"
    INTERNAL = "internal"

    @property
    def iana_if_type(self) -> str:
        """§8.1: the IANA registry has no Thunderbolt/PCIe identity."""
        if self in (UpstreamType.USB, UpstreamType.USB_C):
            return "ianaift:usb"
        return "ianaift:other"


class UpstreamPort(NetvizModel):
    """``spec.upstream`` — the host-facing port of an adapter (§8.1)."""

    #: Port name on the adapter side; referenceable as ``adapter:name``.
    name: IfName
    type: UpstreamType
    #: Host-bus rate, e.g. ``5Gbps`` for USB 3.0.
    speed: BitRate | None = None
    #: The host device the adapter is plugged into. A *device* reference, not an
    #: ``ifref`` (``NG-X001``); it may be written fully qualified (§2.2).
    attached_to: ElementRef | None = None


class AdapterSpec(NetvizModel):
    """``spec`` of an ``adapter`` document (§8)."""

    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    location: str | None = None
    #: Descriptive: ``usb-ethernet``, ``dock``, ``media-converter``, ``sfp-module``.
    form_factor: str | None = None
    #: Rendering hint: may the renderer collapse the adapter into its host (§8.2)?
    passthrough: Boolean = True
    #: Downstream network ports the hardware physically provides. Declaring it
    #: lets the validator catch an inventory that outgrew the device (``E006``).
    ports: PortCount | None = None
    upstream: UpstreamPort
    interfaces: InterfaceList
    #: How this element is drawn (§22): a ``fill``, a ``stroke``, a ``shape``
    #: and six more, each optional and each inheriting from the theme, then the
    #: icon set, then the built-in palette when absent. See
    #: :mod:`netviz.models.style`.
    style: Style | None = None

    @model_validator(mode="after")
    def _check_interfaces(self) -> AdapterSpec:
        # §23 is about a machine's network stacks, and an adapter is not a
        # machine: it has no ``spec.netns`` to name and nothing inside it to
        # create a veth pair between. Refused before ``check_interface_set`` so
        # the reader is told *that* rather than that the peer does not resolve.
        for index, interface in enumerate(self.interfaces):
            if interface.netns is not None:
                raise field_error(
                    f"{interface.name!r} is placed in a network namespace, but an adapter "
                    f"declares no namespace table; a namespace belongs to the host the "
                    f"adapter is attached to (schema §23.1)",
                    rule="NG-N022",
                    path=("interfaces", index, "netns"),
                )
            if interface.peer is not None:
                raise field_error(
                    f"{interface.name!r} declares a veth peer, but an adapter is hardware "
                    f"with no network stack of its own to join (schema §23.2)",
                    rule="NG-N023",
                    path=("interfaces", index, "peer"),
                )
        # NG-X004: the upstream port shares the interface namespace.
        check_interface_set(self.interfaces, reserved={self.upstream.name})
        for index, interface in enumerate(self.interfaces):
            if interface.type not in ADAPTER_INTERFACE_TYPES:
                permitted = ", ".join(sorted(t.value for t in ADAPTER_INTERFACE_TYPES))
                raise field_error(
                    f"{interface.name!r} is of type {interface.type.value!r}; an "
                    f"adapter only supports {permitted}",
                    rule="NG-X003",
                    path=("interfaces", index, "type"),
                )
            if interface.vrf is not None:
                # An adapter has no ``spec.vrfs`` to name (§16.1): the routing
                # instance belongs to the host the adapter hangs off, which is
                # where the binding has to be written to mean anything.
                raise field_error(
                    f"{interface.name!r} binds to a VRF, but an adapter declares no VRF "
                    f"table; bind the VRF on the host it is attached to",
                    rule="NG-F002",
                    path=("interfaces", index, "vrf"),
                )
        return self

    def interface(self, name: str) -> Interface | None:
        """Look a downstream interface up by name."""
        return next((itf for itf in self.interfaces if itf.name == name), None)


class Adapter(ElementBase):
    """A device presenting network interfaces over a non-network host port."""

    kind: Literal["adapter"] = "adapter"
    spec: AdapterSpec

    default_glyph: ClassVar[str] = "adapter"

    @model_validator(mode="after")
    def _apply_defaults(self) -> Adapter:
        # An adapter has no device-wide ``forwarding`` block, so the RFC 8344
        # default of ``false`` applies to every address family it declares.
        resolve_address_family_defaults(self.spec.interfaces, None)
        return self

    @property
    def interfaces(self) -> list[Interface]:
        """Shortcut for ``spec.interfaces`` (the *downstream* ports)."""
        return self.spec.interfaces

    @property
    def upstream(self) -> UpstreamPort:
        """Shortcut for ``spec.upstream``."""
        return self.spec.upstream

    def interface(self, name: str) -> Interface | None:
        """Look a downstream interface up by name."""
        return self.spec.interface(name)

    def interface_names(self) -> Iterator[str]:
        """Every name a cable endpoint may refer to, upstream port included."""
        yield self.spec.upstream.name
        for interface in self.spec.interfaces:
            yield interface.name
