"""Prose documentation for every schema field, keyed by ``(model, field)``.

Pydantic knows a field's type, whether it is required and what it defaults to.
It cannot know what the field *means*, nor which YANG node it maps to. Those two
things live here, in one table, because two generators need them:

* ``tools/gen_schema_reference.py`` turns them into the Description and YANG
  columns of ``docs/schema-reference.md``.
* :mod:`netgraph.schema` hangs them off the JSON Schema as ``description``, so
  an editor with the yaml-language-server shows them on hover.

Keeping one table means the reference and the schema cannot disagree, and
:func:`check_coverage` means neither can silently fall behind the models: a
field with no entry, or an entry naming a field that no longer exists, is a
hard error rather than a quietly incomplete document.

The text is Markdown. It is read by humans in a rendered table and in an editor
tooltip, so it may use backticks and must not use reStructuredText roles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from netgraph.models.adapter import AdapterSpec, UpstreamPort
from netgraph.models.base import NetgraphModel
from netgraph.models.cable import CableSpec, InterfaceRef
from netgraph.models.device import BridgeConfig, DeviceSpec, Forwarding, VlanDefinition
from netgraph.models.element import ElementBase
from netgraph.models.interface import (
    Interface,
    IPv4Address,
    IPv4Config,
    IPv6Address,
    IPv6Config,
    VlanConfig,
)
from netgraph.models.metadata import Metadata

__all__ = [
    "DOCUMENTED_MODELS",
    "FIELD_DOCS",
    "KIND_NOTES",
    "NONE",
    "Doc",
    "check_coverage",
]

#: Marker for "this field has no YANG counterpart".
NONE: Final = "—"


@dataclass(frozen=True)
class Doc:
    """The two things a pydantic model cannot tell us about a field."""

    #: One sentence, ending without a full stop unless it is more than one.
    description: str
    #: The YANG node this field maps to, or :data:`NONE`.
    yang: str = NONE


#: Every model whose fields are documented below, in reference order. The
#: element models themselves are not listed: they only add ``kind`` and ``spec``
#: to :class:`ElementBase`, and those two are described by :data:`KIND_NOTES`.
DOCUMENTED_MODELS: Final[tuple[type[NetgraphModel], ...]] = (
    ElementBase,
    Metadata,
    DeviceSpec,
    Forwarding,
    BridgeConfig,
    VlanDefinition,
    Interface,
    IPv4Config,
    IPv4Address,
    IPv6Config,
    IPv6Address,
    VlanConfig,
    CableSpec,
    InterfaceRef,
    AdapterSpec,
    UpstreamPort,
)

#: What distinguishes one ``kind`` from the next, one sentence each.
KIND_NOTES: Final[dict[str, str]] = {
    "switch": "VLAN-aware bridge. Layer-2 by default: `forwarding` is false/false.",
    "router": "Forwards by default: `forwarding` is true/true.",
    "hub": "Layer-1 repeater. Rejects `vlan`, `ipv4`, `ipv6`, `bridge`, `vlans` and "
    "`forwarding`; every interface must be `ethernet`.",
    "computer": "End host, drawn as a workstation.",
    "server": "End host, drawn as a rack-mount server. Structurally identical to `computer`.",
    "cable": "An undirected link between exactly two interfaces. Owns no interfaces.",
    "adapter": "Presents interfaces over a non-network host port.",
}

#: One entry per ``(model name, field name)``. Checked for exact coverage.
FIELD_DOCS: Final[dict[tuple[str, str], Doc]] = {
    # -- envelope ----------------------------------------------------------
    ("ElementBase", "api_version"): Doc(
        "Schema version of the document. Only `netgraph.dev/v1alpha1` is understood by this "
        "release; an unknown value is `NG-D002`."
    ),
    ("ElementBase", "kind"): Doc(
        "Which element this document declares. Selects the shape of `spec`, and is the "
        "discriminator of the model union."
    ),
    ("ElementBase", "metadata"): Doc("Identity, description, labels and annotations."),
    # -- metadata ----------------------------------------------------------
    ("Metadata", "name"): Doc(
        "Element name, unique within its namespace across all kinds (`NG-N002`). The namespace "
        "is the directory the document was found in."
    ),
    ("Metadata", "description"): Doc(
        "Free text, may be multi-line. Rendered as the node's tooltip in SVG output."
    ),
    ("Metadata", "labels"): Doc(
        "Selector-friendly key/value pairs. Keys follow the Kubernetes label grammar; the "
        "`netgraph.dev/` prefix is reserved for the tool."
    ),
    ("Metadata", "annotations"): Doc(
        "Per-element input to the tooling, not selectable. `netgraph/ignore` suppresses "
        "validation rules on this element."
    ),
    # -- device spec -------------------------------------------------------
    ("DeviceSpec", "vendor"): Doc("Hardware vendor, free text. Documentation only."),
    ("DeviceSpec", "model"): Doc("Hardware model designation, free text."),
    ("DeviceSpec", "serial"): Doc("Serial or asset number, free text."),
    ("DeviceSpec", "location"): Doc("Physical location, free text (site, room, rack unit)."),
    ("DeviceSpec", "interfaces"): Doc(
        "Every port and logical interface the device owns. At least one is required.",
        "/if:interfaces/if:interface",
    ),
    ("DeviceSpec", "bridge"): Doc(
        "The 802.1Q bridge component this device implements. Absent means the device is not a "
        "bridge.",
        "/dot1q:bridges/dot1q:bridge",
    ),
    ("DeviceSpec", "vlans"): Doc(
        "The device VLAN database: which VLANs exist on this device, and what they are called.",
        "…/dot1q:bridge-vlan/dot1q:vlan",
    ),
    ("DeviceSpec", "forwarding"): Doc(
        "Device-wide default for per-interface IP forwarding. Defaults to true/true on a "
        "`router` and false/false on every other kind; a `hub` must not declare it.",
        NONE,
    ),
    ("Forwarding", "ipv4"): Doc(
        "Default for `interfaces[].ipv4.forwarding` on this device.",
        "…/ip:ipv4/ip:forwarding (as the default)",
    ),
    ("Forwarding", "ipv6"): Doc(
        "Default for `interfaces[].ipv6.forwarding` on this device.",
        "…/ip:ipv6/ip:forwarding (as the default)",
    ),
    # -- bridge ------------------------------------------------------------
    ("BridgeConfig", "name"): Doc(
        "Name of the bridge component. Defaults to `metadata.name` once the document is loaded.",
        "/dot1q:bridges/dot1q:bridge/dot1q:name",
    ),
    ("BridgeConfig", "type"): Doc(
        "802.1Q bridge type, which decides the `dot1q:port-type` of every port on the device.",
        "/dot1q:bridges/dot1q:bridge/dot1q:bridge-type",
    ),
    ("BridgeConfig", "address"): Doc(
        "Bridge address (the bridge's own MAC), used as the spanning-tree bridge identifier.",
        "/dot1q:bridges/dot1q:bridge/dot1q:address",
    ),
    # -- VLAN database -----------------------------------------------------
    ("VlanDefinition", "id"): Doc(
        "VLAN identifier. Unique within the device (`NG-V001`).",
        "…/dot1q:bridge-vlan/dot1q:vlan/dot1q:vid",
    ),
    ("VlanDefinition", "name"): Doc(
        "Human name of the VLAN. 802.1Q caps it at 32 characters.",
        "…/dot1q:bridge-vlan/dot1q:vlan/dot1q:name",
    ),
    ("VlanDefinition", "description"): Doc("Free text. netgraph-only; 802.1Q has no such node."),
    # -- interface ---------------------------------------------------------
    ("Interface", "name"): Doc(
        "Interface name as the device itself spells it (`eth0`, `GigabitEthernet0/2`). Unique "
        "within the element (`NG-I001`), and the target of a cable endpoint.",
        "/if:interfaces/if:interface/if:name",
    ),
    ("Interface", "type"): Doc(
        "What kind of interface this is. Decides which other fields are allowed and whether a "
        "cable may terminate here (`NG-C009`).",
        "…/if:type",
    ),
    ("Interface", "description"): Doc(
        "Free text describing what the port is for.", "…/if:description"
    ),
    ("Interface", "enabled"): Doc(
        "Intended administrative state. A disabled interface is exempt from `W101`.",
        "…/if:enabled",
    ),
    ("Interface", "mac"): Doc(
        "Hardware address, EUI-48. Accepted in colon, dash or Cisco dotted form and normalised "
        "to lower-case colon form. `if:phys-address` is `config false` in RFC 8343, so an "
        "exporter must not write it to a live datastore.",
        "…/if:phys-address",
    ),
    ("Interface", "mtu"): Doc(
        "Layer-2 MTU in bytes. Propagated to `ipv4.mtu` and `ipv6.mtu` when those are not set. "
        "RFC 8343 has no layer-2 MTU node.",
        NONE,
    ),
    ("Interface", "ipv4"): Doc(
        "IPv4 configuration. Absent means the interface has no IPv4 stack.", "…/ip:ipv4"
    ),
    ("Interface", "ipv6"): Doc(
        "IPv6 configuration. Absent means the interface has no IPv6 stack.", "…/ip:ipv6"
    ),
    ("Interface", "vlan"): Doc(
        "802.1Q bridge-port configuration. Absent means the port is not VLAN-aware; a host port "
        "facing an access port normally omits it.",
        "…/dot1q:bridge-port",
    ),
    ("Interface", "parent"): Doc(
        "The interface this one is stacked on. Required for `type: vlan`, forbidden otherwise "
        "(`NG-I002`).",
        "…/if:lower-layer-if",
    ),
    ("Interface", "members"): Doc(
        "The interfaces aggregated by this one. Required for `type: lag` and `type: bridge`, "
        "forbidden otherwise (`NG-I003`).",
        "…/if:lower-layer-if",
    ),
    # -- address families --------------------------------------------------
    ("IPv4Config", "enabled"): Doc(
        "Whether the IPv4 stack is active on this interface.", "…/ip:ipv4/ip:enabled"
    ),
    ("IPv4Config", "forwarding"): Doc(
        "Whether the interface forwards IPv4. Left unset in the document, it inherits "
        "`spec.forwarding.ipv4`; RFC 8344's own default is false.",
        "…/ip:ipv4/ip:forwarding",
    ),
    ("IPv4Config", "mtu"): Doc(
        "IPv4 MTU. Defaults to `interfaces[].mtu` once the document is loaded.",
        "…/ip:ipv4/ip:mtu",
    ),
    ("IPv4Config", "addresses"): Doc(
        "The IPv4 addresses configured on the interface. `10.0.0.1/24` is shorthand for a full "
        "entry, and a bare list is shorthand for `{addresses: [...]}`.",
        "…/ip:ipv4/ip:address",
    ),
    ("IPv4Address", "ip"): Doc(
        "The address itself, without a prefix and without a zone index. RFC 8344's list key.",
        "…/ip:ipv4/ip:address/ip:ip",
    ),
    ("IPv4Address", "prefix_length"): Doc(
        "Prefix length. May be written as a dotted-quad `netmask` instead, which is normalised "
        "to a prefix length on load; a non-contiguous mask is rejected (`NG-A003`).",
        "…/ip:ipv4/ip:address/ip:prefix-length",
    ),
    ("IPv6Config", "enabled"): Doc(
        "Whether the IPv6 stack is active on this interface.", "…/ip:ipv6/ip:enabled"
    ),
    ("IPv6Config", "forwarding"): Doc(
        "Whether the interface forwards IPv6. Inherits `spec.forwarding.ipv6` when unset.",
        "…/ip:ipv6/ip:forwarding",
    ),
    ("IPv6Config", "mtu"): Doc(
        "IPv6 MTU. Defaults to `interfaces[].mtu`, but only when that is at least 1280.",
        "…/ip:ipv6/ip:mtu",
    ),
    ("IPv6Config", "addresses"): Doc(
        "The IPv6 addresses configured on the interface. Normalised to RFC 5952 lower-case "
        "compressed form.",
        "…/ip:ipv6/ip:address",
    ),
    ("IPv6Address", "ip"): Doc(
        "The address itself, zone-free. RFC 8344's list key.", "…/ip:ipv6/ip:address/ip:ip"
    ),
    ("IPv6Address", "prefix_length"): Doc(
        "Prefix length. Mandatory — RFC 8344 has no netmask case for IPv6.",
        "…/ip:ipv6/ip:address/ip:prefix-length",
    ),
    # -- VLAN port configuration -------------------------------------------
    ("VlanConfig", "mode"): Doc(
        "Access or trunk. 802.1Q has neither concept; netgraph expands the mode into a PVID, an "
        "acceptable-frame filter and VLAN membership (see docs/yang-mapping.md).",
        NONE,
    ),
    ("VlanConfig", "access_vlan"): Doc(
        "The VLAN an access port belongs to, and the encapsulation VID of a `type: vlan` "
        "sub-interface. Required in access mode — it defaults to 1 — and forbidden in trunk "
        "mode (`NG-V002`).",
        "…/dot1q:bridge-port/dot1q:pvid",
    ),
    ("VlanConfig", "trunk_vlans"): Doc(
        "The tagged VLAN set of a trunk port. Required in trunk mode, forbidden in access mode.",
        "…/dot1q:vlan/dot1q:egress-ports (tagged)",
    ),
    ("VlanConfig", "native_vlan"): Doc(
        "The untagged VLAN on a trunk. Trunk mode only (`NG-V003`); it is implicitly a member of "
        "the port's VLAN set.",
        "…/dot1q:bridge-port/dot1q:pvid",
    ),
    ("VlanConfig", "ingress_filtering"): Doc(
        "Drop frames tagged with a VLAN the port is not a member of.",
        "…/dot1q:bridge-port/dot1q:enable-ingress-filtering",
    ),
    ("VlanConfig", "acceptable_frames"): Doc(
        "Which frames the port admits. Derived from `mode` and `native_vlan` when not stated.",
        "…/dot1q:bridge-port/dot1q:acceptable-frame",
    ),
    # -- cable -------------------------------------------------------------
    ("CableSpec", "endpoints"): Doc(
        "Exactly two `device:interface` references (`NG-C001`). The link is undirected, so the "
        "pair is sorted on load and the order carries no meaning.",
        NONE,
    ),
    ("CableSpec", "medium"): Doc(
        "What the link physically is. `wireless` requires both endpoints to be `type: wifi` and "
        "forbids `length_m` and `category` (`NG-C006`, `NG-C007`).",
        NONE,
    ),
    ("CableSpec", "speed"): Doc(
        "Negotiated link rate. Written as bit/s or as `1Gbps`; stored in bit/s. Projected onto "
        "`if:speed` of both endpoints, which is `config false`.",
        "…/if:speed (on both endpoints)",
    ),
    ("CableSpec", "duplex"): Doc("Duplex of the link. `half` outside a hub link is `NG-C013`."),
    ("CableSpec", "length_m"): Doc("Physical length in metres. Documentation only."),
    ("CableSpec", "category"): Doc("Cable category, free text (`cat6`, `cat6a`, `om4`)."),
    ("CableSpec", "connector"): Doc("Connector type, free text (`rj45`, `lc`, `sc`)."),
    ("CableSpec", "label"): Doc(
        "The identifier printed on the cable or the patch panel. Drawn on the edge."
    ),
    ("InterfaceRef", "device"): Doc(
        "Name of the element the port belongs to. Resolved in the cable's own namespace first, "
        "then upwards (`NG-C002`)."
    ),
    ("InterfaceRef", "interface"): Doc(
        "Name of the interface on that element. An adapter's `upstream.name` counts (`NG-C003`)."
    ),
    # -- adapter -----------------------------------------------------------
    ("AdapterSpec", "vendor"): Doc("Hardware vendor, free text."),
    ("AdapterSpec", "model"): Doc("Hardware model designation, free text."),
    ("AdapterSpec", "serial"): Doc("Serial or asset number, free text."),
    ("AdapterSpec", "location"): Doc("Physical location, free text."),
    ("AdapterSpec", "form_factor"): Doc(
        "What sort of adapter this is: `usb-ethernet`, `dock`, `media-converter`, `sfp-module`. "
        "Descriptive only."
    ),
    ("AdapterSpec", "passthrough"): Doc(
        "May the renderer collapse the adapter into its host? True draws the host and the "
        "adapter as one node at layer 2; false keeps them separate."
    ),
    ("AdapterSpec", "ports"): Doc(
        "How many downstream ports the hardware physically has. Declaring it lets the validator "
        "catch an inventory that outgrew the device (`E006`)."
    ),
    ("AdapterSpec", "upstream"): Doc("The host-facing port: the bus the adapter plugs into."),
    ("AdapterSpec", "interfaces"): Doc(
        "The network ports the adapter presents downstream. Only `ethernet`, `wifi` and `lag` "
        "are allowed (`NG-X003`).",
        "/if:interfaces/if:interface",
    ),
    ("UpstreamPort", "name"): Doc(
        "Name of the host-side port. Shares the interface namespace of the adapter (`NG-X004`) "
        "and may be named by a cable endpoint.",
        "/if:interfaces/if:interface/if:name",
    ),
    ("UpstreamPort", "type"): Doc(
        "The host bus. `usb` and `usb-c` export as `ianaift:usb`; everything else as "
        "`ianaift:other`, because IANA registers no Thunderbolt or PCIe identity.",
        "…/if:type",
    ),
    ("UpstreamPort", "speed"): Doc(
        "Host-bus rate, e.g. `5Gbps` for USB 3.0. Written as bit/s or with a unit suffix.",
        "…/if:speed",
    ),
    ("UpstreamPort", "attached_to"): Doc(
        "The host the adapter is plugged into. A bare device name, never a `device:interface` "
        "reference (`NG-X001`). This is what joins the adapter to the graph when no cable does.",
        NONE,
    ),
}


def check_coverage() -> None:
    """Fail loudly when :data:`FIELD_DOCS` and the models disagree.

    Raises:
        RuntimeError: A field of a :data:`DOCUMENTED_MODELS` entry has no
            documentation, or the table names a field that no longer exists.
    """
    documented = set(FIELD_DOCS)
    actual = {(model.__name__, name) for model in DOCUMENTED_MODELS for name in model.model_fields}
    problems: list[str] = []
    missing = sorted(actual - documented)
    if missing:
        problems.append(
            "no FIELD_DOCS entry for: " + ", ".join(f"{model}.{field}" for model, field in missing)
        )
    extra = sorted(documented - actual)
    if extra:
        problems.append(
            "FIELD_DOCS names fields that no longer exist: "
            + ", ".join(f"{model}.{field}" for model, field in extra)
        )
    if problems:
        raise RuntimeError("netgraph.models.fielddocs: " + "; ".join(problems))
