"""Typed representations of network elements and their configuration.

The models mirror ``docs/schema.md`` one-to-one:

* :class:`Metadata` and :class:`ElementBase` -- the document envelope (§3).
* :class:`Interface` and friends -- RFC 8343 / RFC 8344 / 802.1Q data (§6.2).
* :class:`Device` and its five kinds, :class:`Cable`, :class:`Adapter` (§6-§8)
  and :class:`Tunnel`, the logical counterpart of a cable (§14).
* :data:`Element` -- the discriminated union on ``kind``.

:func:`parse_document` is the entry point the loader uses; it raises
:class:`~netgraph.errors.SchemaError` with the field path of every offending
value.

Models are *normalising*: MAC addresses, IP addresses, VLAN sets and bit rates
are stored in canonical form and defaults are materialised on load, so a
document and its parsed form describe exactly the same network (§1).
"""

from __future__ import annotations

from netgraph.errors import SchemaError, SchemaIssue
from netgraph.models.adapter import (
    ADAPTER_INTERFACE_TYPES,
    Adapter,
    AdapterSpec,
    UpstreamPort,
    UpstreamType,
)
from netgraph.models.base import NetgraphModel
from netgraph.models.cable import Cable, CableSpec, Duplex, InterfaceRef, Medium
from netgraph.models.device import (
    DEVICE_KINDS,
    BridgeConfig,
    BridgeType,
    Computer,
    Device,
    DeviceSpec,
    Forwarding,
    Hub,
    Router,
    Server,
    Switch,
    VlanDefinition,
)
from netgraph.models.document import (
    ELEMENT_MODELS,
    Element,
    element_model_for,
    parse_document,
    parse_template,
)
from netgraph.models.element import DOCUMENT_KINDS, KINDS, TEMPLATE_KIND, ElementBase
from netgraph.models.interface import (
    AGGREGATE_TYPES,
    CABLEABLE_TYPES,
    AcceptableFrames,
    Interface,
    InterfaceType,
    IPv4Address,
    IPv4Config,
    IPv6Address,
    IPv6Config,
    VlanConfig,
    VlanMode,
)
from netgraph.models.metadata import RESERVED_LABEL_PREFIX, Location, Metadata
from netgraph.models.patchpanel import (
    FRONT,
    MAX_PANEL_PORTS,
    PATCHPANEL_KIND,
    REAR,
    PanelSide,
    PatchPanel,
    PatchPanelSpec,
    panel_port,
    parse_port_range,
    split_panel_port,
)
from netgraph.models.scalars import (
    API_VERSION,
    VlanSet,
    format_bitrate,
    normalise_mac,
    parse_bitrate,
)
from netgraph.models.template import INHERIT_KEY, TEMPLATE_SPEC_KEYS, Template
from netgraph.models.tunnel import (
    MAX_VNI,
    Tunnel,
    TunnelAuth,
    TunnelMode,
    TunnelSpec,
    TunnelTransport,
    TunnelType,
)

__all__ = [
    "ADAPTER_INTERFACE_TYPES",
    "AGGREGATE_TYPES",
    "API_VERSION",
    "CABLEABLE_TYPES",
    "DEVICE_KINDS",
    "DOCUMENT_KINDS",
    "ELEMENT_MODELS",
    "FRONT",
    "INHERIT_KEY",
    "KINDS",
    "MAX_PANEL_PORTS",
    "MAX_VNI",
    "PATCHPANEL_KIND",
    "REAR",
    "RESERVED_LABEL_PREFIX",
    "TEMPLATE_KIND",
    "TEMPLATE_SPEC_KEYS",
    "AcceptableFrames",
    "Adapter",
    "AdapterSpec",
    "BridgeConfig",
    "BridgeType",
    "Cable",
    "CableSpec",
    "Computer",
    "Device",
    "DeviceSpec",
    "Duplex",
    "Element",
    "ElementBase",
    "Forwarding",
    "Hub",
    "IPv4Address",
    "IPv4Config",
    "IPv6Address",
    "IPv6Config",
    "Interface",
    "InterfaceRef",
    "InterfaceType",
    "Location",
    "Medium",
    "Metadata",
    "NetgraphModel",
    "PanelSide",
    "PatchPanel",
    "PatchPanelSpec",
    "Router",
    "SchemaError",
    "SchemaIssue",
    "Server",
    "Switch",
    "Template",
    "Tunnel",
    "TunnelAuth",
    "TunnelMode",
    "TunnelSpec",
    "TunnelTransport",
    "TunnelType",
    "UpstreamPort",
    "UpstreamType",
    "VlanConfig",
    "VlanDefinition",
    "VlanMode",
    "VlanSet",
    "element_model_for",
    "format_bitrate",
    "normalise_mac",
    "panel_port",
    "parse_bitrate",
    "parse_document",
    "parse_port_range",
    "parse_template",
    "split_panel_port",
]
