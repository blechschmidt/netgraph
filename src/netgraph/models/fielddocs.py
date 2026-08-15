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
from netgraph.models.annotation import (
    AnnotationGeometry,
    AreaSelector,
    AreaSpec,
    LegendEntry,
    LegendSpec,
    NoteAnchor,
    NoteSpec,
)
from netgraph.models.base import NetgraphModel
from netgraph.models.cable import CableSpec, InterfaceRef
from netgraph.models.device import BridgeConfig, DeviceSpec, Forwarding, VlanDefinition
from netgraph.models.element import ElementBase
from netgraph.models.identity import GroupSpec, UserSpec
from netgraph.models.interface import (
    Bss,
    Interface,
    IPv4Address,
    IPv4Config,
    IPv6Address,
    IPv6Config,
    VlanConfig,
    WirelessConfig,
)
from netgraph.models.layout import (
    EdgeGeometry,
    GroupGeometry,
    LabelGeometry,
    LayoutSpec,
    NodeGeometry,
    Point,
    Size,
    ViewGeometry,
)
from netgraph.models.metadata import Location, Metadata
from netgraph.models.patchpanel import PatchPanelSpec
from netgraph.models.pdu import PduSpec
from netgraph.models.power import PoeConfig, PowerConfig, PowerDraw, PowerInput
from netgraph.models.routing import (
    BgpConfig,
    BgpNeighbor,
    OspfConfig,
    RoutingConfig,
    StaticRoute,
    VrfDefinition,
)
from netgraph.models.testsuite import Assertion, TestSuiteSpec
from netgraph.models.tunnel import TunnelSpec

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
    Location,
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
    WirelessConfig,
    Bss,
    VrfDefinition,
    StaticRoute,
    RoutingConfig,
    OspfConfig,
    BgpConfig,
    BgpNeighbor,
    CableSpec,
    InterfaceRef,
    AdapterSpec,
    UpstreamPort,
    TunnelSpec,
    PatchPanelSpec,
    PowerConfig,
    PowerDraw,
    PowerInput,
    PoeConfig,
    PduSpec,
    UserSpec,
    GroupSpec,
    LayoutSpec,
    ViewGeometry,
    NodeGeometry,
    EdgeGeometry,
    LabelGeometry,
    GroupGeometry,
    Point,
    Size,
    TestSuiteSpec,
    Assertion,
    NoteSpec,
    NoteAnchor,
    AnnotationGeometry,
    AreaSpec,
    AreaSelector,
    LegendSpec,
    LegendEntry,
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
    "tunnel": "An undirected logical link between two or more `tunnel` interfaces. Owns no "
    "interfaces; `over` nests it inside another tunnel.",
    "patchpanel": "A passive cross-connect. Its `front/<n>` and `rear/<n>` ports are derived "
    "from `ports`, and a coupler joins each front port to one rear port; it is not a hop.",
    "pdu": "A power distribution unit. Its numbered outlets are derived from `outlets`; they "
    "are not interfaces, and a device names one in `power.inputs` rather than being cabled to "
    "it. Placed on a rack elevation like any other hardware.",
    "user": "One identity: a person, a service account or a shared login. Owns no interfaces "
    "and terminates no cable; it is drawn only in the `identity` view.",
    "group": "A named set of identities. `members` may name a `user` or another `group`, which "
    "is what makes a hierarchy expressible; the nesting must not loop (`NG-S012`).",
    "template": "A named partial device spec, merged into every device that names it in "
    "`spec.from`. Not an element: never drawn, never listed, never validated on its own.",
    "layout": "Diagram geometry for elements declared elsewhere, scoped by view. Not an "
    "element: it carries no network facts and is never drawn as a node. See `netgraph layout`.",
    "testsuite": "Named assertions about the network the other documents describe, graded by "
    "`netgraph test`. Not an element: it declares no device and is never drawn.",
    "note": "One free-text callout on the diagram, pinned to a point or anchored to an element "
    "or a link. Presentational: it declares no network fact and never changes what `validate`, "
    "`path`, `export` or `plan` conclude.",
    "area": "A labelled box drawn behind the nodes, enclosing the elements it names, matches or "
    "encircles. The declarative form of `--collapse` grouping: the same set of elements, boxed "
    "rather than folded.",
    "legend": "A key: what the colours and line styles of the drawing mean, placed by corner "
    "rather than by coordinate. `auto: layers` derives the entries from what the view drew.",
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
    ("Metadata", "location"): Doc(
        "Where the hardware physically is: site, room, rack and the rack units it occupies. "
        "Drives `--layer rack` and the placement rules `NG-U001` to `NG-U004`."
    ),
    ("Metadata", "labels"): Doc(
        "Selector-friendly key/value pairs. Keys follow the Kubernetes label grammar; the "
        "`netgraph.dev/` prefix is reserved for the tool."
    ),
    ("Metadata", "annotations"): Doc(
        "Per-element input to the tooling, not selectable. `netgraph/ignore` suppresses "
        "validation rules on this element."
    ),
    # -- location ----------------------------------------------------------
    ("Location", "site"): Doc("Site the element is installed at, free text."),
    ("Location", "room"): Doc("Room or floor within the site, free text."),
    ("Location", "rack"): Doc(
        "Rack identifier, unique within its room. Naming one is what puts the element on an "
        "elevation; site, room and rack together identify the rack (`NG-U001`)."
    ),
    ("Location", "position"): Doc(
        "Lowest rack unit the element occupies, counted from 1 at the bottom of the rack. "
        "Requires `rack` (`NG-U004`)."
    ),
    ("Location", "height"): Doc(
        "How many rack units the element occupies, upwards from `position`."
    ),
    ("Location", "rack_height"): Doc(
        "How tall the rack itself is. Any element in the rack may declare it; two that "
        "disagree are `NG-U003`, and nothing may extend past it (`NG-U002`)."
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
    ("DeviceSpec", "vrfs"): Doc(
        "The routing instances (VRFs) this device implements. An interface binds to one with "
        "`vrf`, and that binding is what partitions the address namespace.",
        "/ni:network-instances/ni:network-instance",
    ),
    ("DeviceSpec", "routes"): Doc(
        "Configured static routes, in the order the device holds them.",
        "…/rt:routing/rt:control-plane-protocols/rt:control-plane-protocol/rt:static-routes",
    ),
    ("DeviceSpec", "power"): Doc(
        "What the device draws, which PDU outlets feed it, and how much PoE it hands out "
        "(§17.2). Absent means the inventory records nothing about its power.",
        "/eo-mib:eoPowerTable/eoPowerEntry",
    ),
    ("DeviceSpec", "routing"): Doc(
        "The dynamic routing protocols the device takes part in: an OSPF area, a BGP autonomous "
        "system, or both.",
        "…/rt:routing/rt:control-plane-protocols/rt:control-plane-protocol",
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
    # -- routing (§16) -----------------------------------------------------
    ("VrfDefinition", "name"): Doc(
        "Name of the routing instance. Unique within the device (`NG-F001`), and what an "
        "interface's `vrf` and a route's `vrf` refer to. Two devices using one name mean one VRF.",
        "/ni:network-instances/ni:network-instance/ni:name",
    ),
    ("VrfDefinition", "rd"): Doc(
        "Route distinguisher, in one of the three RFC 4364 §4.2 encodings: `65000:1`, "
        "`192.0.2.1:1` or `4200000000:1`. Quote it — an unquoted `65000:1` is a number to YAML.",
        NONE,
    ),
    ("VrfDefinition", "description"): Doc(
        "Free text: what the instance is for.",
        "/ni:network-instances/ni:network-instance/ni:description",
    ),
    ("StaticRoute", "prefix"): Doc(
        "Destination prefix, either family, in canonical CIDR form. Host bits are rejected: a "
        "destination with them set is a typo or a host route, and netgraph will not guess which.",
        "…/rt:static-routes/v4ur:ipv4/v4ur:route/v4ur:destination-prefix",
    ),
    ("StaticRoute", "via"): Doc(
        "Next-hop address. Same family as `prefix` (`NG-F003`), and on a prefix the device "
        "configures (`NG-F008`).",
        "…/v4ur:route/v4ur:next-hop/v4ur:next-hop-address",
    ),
    ("StaticRoute", "dev"): Doc(
        "Egress interface, for an unnumbered next hop or a route pointed at an interface. Names "
        "an interface of this device (`NG-F009`).",
        "…/v4ur:route/v4ur:next-hop/v4ur:outgoing-interface",
    ),
    ("StaticRoute", "vrf"): Doc(
        "The routing instance holding the route. Names an entry of `spec.vrfs` (`NG-F005`); "
        "unset means the global instance.",
        "/ni:network-instances/ni:network-instance/ni:name",
    ),
    ("StaticRoute", "metric"): Doc(
        "Administrative distance or cost, as this device counts it. Documentation only: netgraph "
        "does not compute a best path.",
        NONE,
    ),
    ("StaticRoute", "blackhole"): Doc(
        "Discard matching packets. Excludes `via` and `dev` (`NG-F004`).",
        "…/v4ur:route/v4ur:next-hop/v4ur:special-next-hop",
    ),
    ("RoutingConfig", "ospf"): Doc(
        "The OSPF area this device runs, and on which interfaces.",
        "…/rt:control-plane-protocol[type='ospf']",
    ),
    ("RoutingConfig", "bgp"): Doc(
        "The BGP autonomous system this device is in, and its neighbours.",
        "…/rt:control-plane-protocol[type='bgp']",
    ),
    ("OspfConfig", "area"): Doc(
        "Area identifier, written as a dotted quad or as a plain number; `0` and `0.0.0.0` are "
        "the same backbone area and both normalise to `0.0.0.0`.",
        NONE,
    ),
    ("OspfConfig", "router_id"): Doc(
        "Router identifier — a dotted quad even in an IPv6-only network. Unique across the "
        "inventory (`NG-F012`).",
        NONE,
    ),
    ("OspfConfig", "interfaces"): Doc(
        "The interfaces OSPF runs on. Non-empty, free of duplicates (`NG-F006`), and each one an "
        "interface of this device (`NG-F010`).",
        "/if:interfaces/if:interface/if:name",
    ),
    ("BgpConfig", "asn"): Doc(
        "Local autonomous system number, 1 to 4294967295. AS 0 is reserved (RFC 7607).",
        NONE,
    ),
    ("BgpConfig", "router_id"): Doc(
        "BGP identifier — a dotted quad. Unique across the inventory (`NG-F012`); commonly the "
        "same value as the OSPF router id, which is one identity rather than a duplicate.",
        NONE,
    ),
    ("BgpConfig", "neighbors"): Doc(
        "The sessions this device configures. Peers are named by address, never by element name.",
        NONE,
    ),
    ("BgpNeighbor", "address"): Doc(
        "Peer address. Resolved against every address the inventory configures; a peer that "
        "resolves to nothing is `NG-F013`, a warning, because an eBGP peer may be external.",
        NONE,
    ),
    ("BgpNeighbor", "remote_asn"): Doc(
        "The AS the peer is in. Checked against the peer's own `asn` when the address resolves "
        "(`NG-F011`).",
        NONE,
    ),
    ("BgpNeighbor", "description"): Doc("Free text: what the session is for."),
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
    ("Interface", "vrf"): Doc(
        "The routing instance this interface is in. Names an entry of the device's `spec.vrfs` "
        "(`NG-F002`); unset means the global instance. An address only collides with another "
        "address in the same VRF.",
        "/ni:network-instances/ni:network-instance/ni:name",
    ),
    ("Interface", "poe"): Doc(
        "This port is power sourcing equipment: it hands power down the cable (§17.3). Only on "
        "a type a cable terminates on — `ethernet` or `lag` (`NG-E006`).",
        "/power-ethernet-mib:pethPsePortTable/pethPsePortEntry",
    ),
    ("Interface", "wireless"): Doc(
        "Radio configuration of a `type: wifi` interface: which side of the association it is, "
        "which frequency it uses and which BSSs it beacons or joins. Forbidden on every other "
        "type (`NG-W002`).",
        "…/dot11:wireless-interface",
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
    ("IPv4Config", "gateway"): Doc(
        "First hop for off-link IPv4 traffic, as a bare address without a prefix length. It "
        "must lie inside one of this interface's own prefixes (`NG-A013`).",
        "rt:routing/…/static-routes/v4ur:ipv4/v4ur:route/…/next-hop-address",
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
    ("IPv6Config", "gateway"): Doc(
        "First hop for off-link IPv6 traffic, as a bare address without a prefix length. It "
        "must lie inside one of this interface's own prefixes (`NG-A013`), unless it is "
        "link-local: `fe80::1` is on-link by definition and is exempt.",
        "rt:routing/…/static-routes/v6ur:ipv6/v6ur:route/…/next-hop-address",
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
    # -- wireless ----------------------------------------------------------
    ("WirelessConfig", "role"): Doc(
        "Which side of the association this radio is: `ap` beacons the SSIDs, `station` and "
        "`mesh` associate to one. A wireless link joins exactly one `ap` to one client "
        "(`NG-W007`).",
        "…/dot11:station-config/dot11:desired-bss-type",
    ),
    ("WirelessConfig", "band"): Doc(
        "The band the radio operates in: `2.4GHz`, `5GHz` or `6GHz`. Required alongside "
        "`channel` and `width_mhz`, because both mean different frequencies in different bands.",
        "…/dot11:phy/dot11:channel-starting-factor",
    ),
    ("WirelessConfig", "channel"): Doc(
        "The primary 20 MHz channel, as the band numbers it (`NG-W003`).",
        "…/dot11:phy/dot11:current-channel-number",
    ),
    ("WirelessConfig", "width_mhz"): Doc(
        "Total channel width in MHz. 40 is the most 2.4 GHz can bond and 320 is 6 GHz only "
        "(`NG-W004`).",
        "…/dot11:phy/dot11:current-channel-width",
    ),
    ("WirelessConfig", "tx_power_dbm"): Doc(
        "Radiated power in dBm. The MIB counts abstract power *levels* per PHY, so the unit is "
        "netgraph's own.",
        "…/dot11:phy/dot11:current-tx-power-level",
    ),
    ("WirelessConfig", "bss"): Doc(
        "The basic service sets this radio beacons (`ap`) or is associated to (`station`, "
        "`mesh`, at most one — `NG-W006`).",
        "…/dot11:bss",
    ),
    ("Bss", "ssid"): Doc(
        "The network name, 1 to 32 octets. Unique within one radio (`NG-W005`), and on a client "
        "radio it must be one the AP at the far end advertises (`NG-W010`).",
        "…/dot11:bss/dot11:ssid",
    ),
    ("Bss", "bssid"): Doc(
        "MAC address of this BSS — usually the radio's own for the first SSID and a derived one "
        "for each further SSID. Unique across the inventory (`NG-W008`).",
        "…/dot11:bss/dot11:bssid",
    ),
    ("Bss", "vlan"): Doc(
        "The VLAN this SSID's traffic is bridged into. Absent means the radio's untagged "
        "domain. Checked against the device's VLAN database (`W113`) and against the VLANs the "
        "access point actually carries (`NG-W009`).",
        NONE,
    ),
    ("Bss", "security"): Doc(
        "How the BSS authenticates: `open`, or WPA2/WPA3 with a passphrase (`-psk`) or an "
        "authentication server (`-eap`). Absent means nobody recorded it.",
        "…/dot11:bss/dot11:rsna-enabled",
    ),
    ("Bss", "hidden"): Doc(
        "The SSID is left out of the beacon. It is still on the air, and still a BSS this radio "
        "serves.",
        NONE,
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
    # -- tunnel ------------------------------------------------------------
    ("TunnelSpec", "type"): Doc(
        "The encapsulation: `wireguard`, `ipsec`, `openvpn`, `pptp`, `l2tp`, `gre`, `vxlan` or "
        "`geneve`. It decides the layer carried, the outer transport, the default port, whether "
        "the payload is encrypted and how much MTU the headers cost.",
        NONE,
    ),
    ("TunnelSpec", "endpoints"): Doc(
        "Two or more `device:interface` references, each naming an interface of `type: tunnel` "
        "(`NG-T001`, `NG-T003`). The link is undirected, so the list is sorted on load. Three or "
        "more endpoints make it multipoint, and it is then drawn as a node rather than a line.",
        NONE,
    ),
    ("TunnelSpec", "over"): Doc(
        "The tunnel this one is encapsulated in — `vxlan` over `ipsec` is written by naming the "
        "IPsec tunnel here (`NG-T004`). Absent means the tunnel runs directly over the physical "
        "topology. The chain must not loop (`NG-T005`).",
        NONE,
    ),
    ("TunnelSpec", "mode"): Doc(
        "IPsec's encapsulation mode, `tunnel` or `transport` (RFC 4301). Defaults to `tunnel`; "
        "every other type has only one mode and must not declare it (`NG-T008`).",
        NONE,
    ),
    ("TunnelSpec", "vni"): Doc(
        "The 24-bit VXLAN/Geneve virtual network identifier. Required for those two types and "
        "rejected for every other (`NG-T007`).",
        NONE,
    ),
    ("TunnelSpec", "port"): Doc(
        "Outer UDP/TCP port. Defaults to the registered port of the type (WireGuard 51820, "
        "OpenVPN 1194, L2TP 1701, VXLAN 4789, Geneve 6081) and is rejected for GRE and IPsec, "
        "which run directly over IP (`NG-T008`).",
        NONE,
    ),
    ("TunnelSpec", "mtu"): Doc(
        "MTU of the tunnel interface. Compared with what the underlay leaves after the "
        "encapsulation overhead of the whole stack (`NG-T011`).",
        NONE,
    ),
    ("TunnelSpec", "encrypted"): Doc(
        "Whether the payload is protected. Defaults to what the type does — true for WireGuard, "
        "IPsec and OpenVPN, false for GRE, VXLAN, Geneve, L2TP and PPTP, whose MPPE is broken. "
        "Set it to true to record that the deployment protects an otherwise cleartext type some "
        "other way.",
        NONE,
    ),
    ("TunnelSpec", "cipher"): Doc(
        "Negotiated cipher suite, free text (`chacha20-poly1305`, `aes-256-gcm`). Only on a "
        "tunnel that encrypts (`NG-T009`).",
        NONE,
    ),
    ("TunnelSpec", "auth"): Doc(
        "How the endpoints authenticate each other: `psk`, `certificate`, `public-key` or "
        "`password`. The *method*, never the material — netgraph stores no secrets (`NG-T010`).",
        NONE,
    ),
    ("TunnelSpec", "label"): Doc(
        "Free-text identifier printed on the edge, as a cable's `label` is."
    ),
    # -- patch panel -------------------------------------------------------
    ("PatchPanelSpec", "vendor"): Doc("Hardware vendor, free text. Documentation only."),
    ("PatchPanelSpec", "model"): Doc("Hardware model designation, free text."),
    ("PatchPanelSpec", "serial"): Doc("Serial or asset number, free text."),
    ("PatchPanelSpec", "form_factor"): Doc(
        "Descriptive: `keystone`, `fibre-lc`, `coupler`. Documentation only."
    ),
    ("PatchPanelSpec", "ports"): Doc(
        "The positions the panel has, as a count (`24`) or as spans (`1-12,17-24`). Each one "
        "becomes a `front/<n>` and a `rear/<n>` interface (`NG-P006`).",
        "/if:interfaces/if:interface",
    ),
    ("PatchPanelSpec", "couplers"): Doc(
        "Front position to rear position, for a panel that is not wired straight through. "
        "Absent means the identity mapping (`NG-P007`).",
        NONE,
    ),
    # -- power distribution unit -------------------------------------------
    ("PduSpec", "vendor"): Doc("Hardware vendor, free text. Documentation only."),
    ("PduSpec", "model"): Doc("Hardware model designation, free text."),
    ("PduSpec", "serial"): Doc("Serial or asset number, free text."),
    ("PduSpec", "form_factor"): Doc(
        "Descriptive: `vertical`, `horizontal`, `1U`, `0U`. Documentation only."
    ),
    ("PduSpec", "outlets"): Doc(
        "The outlets the unit has, as a count (`24`) or as spans (`1-12,17-24`). Referred to by "
        "number from a device's `power.inputs`; at most 512, no repeats (`NG-E001`).",
        "/entity-mib:entPhysicalTable/entPhysicalEntry",
    ),
    ("PduSpec", "capacity_watts"): Doc(
        "How many watts may be drawn through the unit in total. `NG-E012` sums the declared "
        "loads against it; absent means the rating is not recorded, and nothing is graded.",
        "/eo-mib:eoPowerTable/eoPowerEntry/eoPowerNameplate",
    ),
    ("PduSpec", "input_feed"): Doc(
        "Which supply feeds the unit — `A`, `B`, `ups-1`, `utility`. Free text, compared only "
        "for equality: two PDUs on one feed do not make a device redundant (`NG-E015`).",
        "/eo-ctx-mib:eoPowerRelationTable/eoPowerRelationEntry",
    ),
    # -- identity ----------------------------------------------------------
    ("UserSpec", "login"): Doc(
        "The account name, when it differs from `metadata.name`; absent means the two are the "
        "same. Estate-wide, so two users claiming one login is `NG-S013`.",
        "/ietf-system:system/authentication/user/name",
    ),
    ("UserSpec", "full_name"): Doc(
        "The person's name as they write it. Free text: a real name is not a grammar."
    ),
    ("UserSpec", "email"): Doc(
        "Where mail reaches them, `local@domain.tld`. Also what ties the identity to a "
        "directory without netgraph having to model the directory."
    ),
    ("UserSpec", "uid"): Doc(
        "POSIX user id, when the estate assigns one. 0 to 4294967294; two users claiming one "
        "is `NG-S013`.",
        "/ietf-system:system/authentication/user",
    ),
    ("UserSpec", "type"): Doc(
        "`person`, `service` or `shared`. Decides whether `NG-S015` and `NG-S016` have "
        "anything to say: only a person can depart, and only a person is expected in a group."
    ),
    ("UserSpec", "status"): Doc(
        "`active`, `suspended` or `departed`. A departed account is kept rather than deleted "
        "so the memberships still to be revoked stay visible (`NG-S015`)."
    ),
    ("UserSpec", "ssh_keys"): Doc(
        "Public keys the account authenticates with, `<algorithm> <base64> [comment]`. "
        "Normalised to single spaces; a private key is refused (`NG-S002`).",
        "/ietf-system:system/authentication/user/authorized-key",
    ),
    ("GroupSpec", "members"): Doc(
        "The users and nested groups in this group, as element references resolved outwards "
        "from the group's own namespace. Must resolve (`NG-S010`), must be an identity "
        "(`NG-S011`), and the nesting must not loop (`NG-S012`)."
    ),
    ("GroupSpec", "gid"): Doc(
        "POSIX group id, when the estate assigns one. 0 to 4294967294; two groups claiming one "
        "is `NG-S013`."
    ),
    ("GroupSpec", "email"): Doc(
        "Where mail to the whole group goes, when the group is also a distribution list."
    ),
    # -- power (device) ----------------------------------------------------
    ("PowerConfig", "draw_watts"): Doc(
        "What the device draws. A bare number is the typical draw; a mapping states `typical` "
        "and optionally `maximum`.",
        "/eo-mib:eoPowerTable/eoPowerEntry/eoPower",
    ),
    ("PowerConfig", "inputs"): Doc(
        "One entry per power supply, naming the PDU outlet feeding it. At most 8. Empty for a "
        "device fed over PoE, or one whose feed is not recorded yet (`NG-E016`).",
        "/eo-ctx-mib:eoPowerRelationTable/eoPowerRelationEntry",
    ),
    ("PowerConfig", "redundant"): Doc(
        "The feeds are meant to be independent: losing one must not lose the device. Needs at "
        "least two `inputs` (`NG-E002`) that land on different units and different feeds "
        "(`NG-E015`).",
        NONE,
    ),
    ("PowerConfig", "powered_by"): Doc(
        "Where the device's own power comes from: `outlet` (the default) or `poe`, meaning it "
        "takes power over its uplink and declares no `inputs` (`NG-E005`, `NG-E014`).",
        NONE,
    ),
    ("PowerConfig", "poe_budget_watts"): Doc(
        "The PoE power this device can hand out across every PSE port together. `NG-E013` "
        "checks the ports that hold budget fit inside it.",
        "/power-ethernet-mib:pethMainPseTable/pethMainPseEntry/pethMainPsePower",
    ),
    ("PowerDraw", "typical"): Doc(
        "Steady-state draw of the box as configured, in watts. This is what a load schedule sums.",
        "/eo-mib:eoPowerTable/eoPowerEntry/eoPower",
    ),
    ("PowerDraw", "maximum"): Doc(
        "Nameplate or PSU rating, in watts — what a breaker has to survive. Must not be below "
        "`typical` (`NG-E003`).",
        "/eo-mib:eoPowerTable/eoPowerEntry/eoPowerNameplate",
    ),
    ("PowerInput", "pdu"): Doc(
        "The PDU feeding this supply. An element reference, so it may be written fully "
        "qualified to pick one of several PDUs sharing a short name (`NG-E011`).",
        NONE,
    ),
    ("PowerInput", "outlet"): Doc(
        "The outlet on it, as the PDU numbers it. Must exist (`NG-E011`) and must not already "
        "feed something else (`NG-E010`). Writable as the shorthand `pdu-r1-a:7`.",
        NONE,
    ),
    ("PowerInput", "psu"): Doc(
        "Which supply on the device this feeds, e.g. `psu1`. Documentation only, and worth "
        "writing: it is what an operator reads off the back of a chassis."
    ),
    # -- power over ethernet -----------------------------------------------
    ("PoeConfig", "standard"): Doc(
        "Which IEEE 802.3 amendment the port implements: `802.3af` (classes 0-3), `802.3at` "
        "(adds 4) or `802.3bt` (adds 5-8).",
        "/power-ethernet-mib:pethPsePortTable/pethPsePortEntry/pethPsePortType",
    ),
    ("PoeConfig", "pse_class"): Doc(
        "The IEEE classification, 0 to 8, written `class` in YAML. Fixes the reservation; "
        "refused above the standard's own ceiling, and exclusive with `budget_watts` "
        "(`NG-E004`).",
        "…/pethPsePortEntry/pethPsePortPowerClassifications",
    ),
    ("PoeConfig", "budget_watts"): Doc(
        "An explicit reservation in watts instead of a class, for a vendor that lets an "
        "operator cap a port below what its class allows.",
        "…/pethPsePortEntry/pethPsePortPowerLimit",
    ),
    ("PoeConfig", "enabled"): Doc(
        "Is the port administratively allowed to source power? A disabled PSE port reserves "
        "nothing and powers nothing, which is what `NG-E014` reports it for.",
        "…/pethPsePortEntry/pethPsePortAdminEnable",
    ),
    # -- layout geometry (§18) --------------------------------------------
    ("LayoutSpec", "views"): Doc(
        "Geometry per view, keyed by layer name (`l1`, `l2`, `l3`, `routing`, ...). The same "
        "element sits differently in each, so each gets its own arrangement."
    ),
    ("ViewGeometry", "nodes"): Doc(
        "Where each node is drawn, keyed by its address. A derived node the inventory does not "
        "declare is keyed by its graph id, such as `subnet:10.0.0.0/24`."
    ),
    ("ViewGeometry", "edges"): Doc(
        "Bends each link is drawn through, keyed by the link's address."
    ),
    ("ViewGeometry", "groups"): Doc(
        "The box each namespace cluster is drawn as, keyed by namespace. Only drawn when the "
        "render groups by namespace."
    ),
    ("ViewGeometry", "routing"): Doc(
        "How links in this view are drawn when they do not say for themselves. Overrides "
        "`spec.routing`; a link's own `routing` overrides both."
    ),
    ("LayoutSpec", "routing"): Doc(
        "How links are drawn across the whole inventory unless a view or a link says "
        "otherwise: `spline` (the curve Graphviz draws), `orthogonal` (right angles) or "
        "`straight`."
    ),
    ("NodeGeometry", "position"): Doc("Centre of the node, in points."),
    ("NodeGeometry", "size"): Doc(
        "Box the node occupies, in points. Omitted means the label decides, which is what keeps "
        "an arrangement valid when a device grows a port."
    ),
    ("EdgeGeometry", "waypoints"): Doc(
        "The bends the link is drawn through, in points, ordered from its first endpoint to "
        "its second. Interior points only: the two ends are the nodes, so a route follows "
        "them when they are dragged."
    ),
    ("EdgeGeometry", "routing"): Doc(
        "How this link is drawn between its bends, overriding the view's and the inventory's "
        "default: `spline`, `orthogonal` or `straight`."
    ),
    ("EdgeGeometry", "label"): Doc(
        "Where the link's annotation sits. Omitted leaves it where the renderer puts it, "
        "which is half way along and on the line."
    ),
    ("LabelGeometry", "at"): Doc(
        "How far along the route the label sits, from `0` at the first endpoint to `1` at "
        "the second."
    ),
    ("LabelGeometry", "offset"): Doc(
        "How far off the line the label is nudged, in points. This is what makes a dense "
        "VLAN diagram legible."
    ),
    ("GroupGeometry", "position"): Doc("Centre of the cluster box, in points."),
    ("GroupGeometry", "size"): Doc("Extent of the cluster box, in points."),
    ("Point", "x"): Doc("Points from the left edge of the drawing, growing rightwards."),
    ("Point", "y"): Doc("Points from the bottom edge of the drawing, growing upwards."),
    ("Size", "width"): Doc("Width in points; strictly positive."),
    ("Size", "height"): Doc("Height in points; strictly positive."),
    # -- assertions (§20) --------------------------------------------------
    ("TestSuiteSpec", "description"): Doc(
        "What the suite is for, in one line. Printed as the suite's progress line."
    ),
    ("TestSuiteSpec", "assertions"): Doc(
        "The claims, graded in the order they are written. At least one: a suite that asserts "
        "nothing would report a green run having checked nothing."
    ),
    ("Assertion", "assert_"): Doc(
        "What is being claimed: `reachable`, `not-reachable`, `path-shorter-than`, `same-vlan`, "
        "`distinct-vlan`, `within-prefix`, `has-interface`, `port-count-at-least`, `unique`, "
        "`count` or `no-single-point-of-failure`. Every other key is read in its light."
    ),
    ("Assertion", "name"): Doc(
        "How the claim is reported — a sentence a reader who has never seen the inventory can "
        "act on. Defaults to a description built from the other keys."
    ),
    ("Assertion", "description"): Doc(
        "Why the claim is made. Printed under a failure, so it is where the ticket number or "
        "the standard that demands it belongs."
    ),
    ("Assertion", "from_"): Doc(
        "Where the trace starts: an element, `element:interface`, an IP address, or a selector "
        "matching several of them. The spellings `netgraph path` accepts."
    ),
    ("Assertion", "to"): Doc("Where the trace ends, in the same four spellings as `from`."),
    ("Assertion", "max_hops"): Doc(
        "Abandon a route that crosses more links than this. Defaults to the trace engine's own "
        "limit of 16."
    ),
    ("Assertion", "hops"): Doc(
        "`path-shorter-than`: the exclusive upper bound on the hop count of the shortest path."
    ),
    ("Assertion", "vlan"): Doc(
        "Restrict a trace to one VLAN, or pin which VLAN `same-vlan` means."
    ),
    ("Assertion", "layer"): Doc(
        "Which view the claim is about: `any`, `l2` or `l3` for a trace; `any`, `l1`, `l2`, "
        "`l3` or `power` for `no-single-point-of-failure`."
    ),
    ("Assertion", "select"): Doc(
        "Which elements the claim is about, in `netgraph render`'s filter vocabulary: "
        "`kind=switch, namespace=sites/north, name=sw-*`. A bare word is a name glob."
    ),
    ("Assertion", "prefix"): Doc(
        "`within-prefix`: the CIDR every routable address on a selected element must lie inside."
    ),
    ("Assertion", "interface"): Doc(
        "`has-interface`: the interface name every selected element must declare, or a glob "
        "matching it."
    ),
    ("Assertion", "ports"): Doc(
        "`port-count-at-least`: the inclusive lower bound on how many interfaces each selected "
        "element declares."
    ),
    ("Assertion", "field"): Doc(
        "`unique`: the field expression whose values must all differ, e.g. "
        "`spec.interfaces[name=mgmt0].ipv4[]`."
    ),
    ("Assertion", "equals"): Doc("`count`: how many elements the selector must match, exactly."),
    ("Assertion", "at_least"): Doc(
        "`count`: the inclusive lower bound on how many elements the selector matches."
    ),
    ("Assertion", "at_most"): Doc(
        "`count`: the inclusive upper bound on how many elements the selector matches."
    ),
    ("Assertion", "min_isolated"): Doc(
        "`no-single-point-of-failure`: ignore a candidate that isolates fewer endpoints than "
        "this. 1, the default, reports every one of them."
    ),
    # -- annotations (§21) -------------------------------------------------
    ("NoteSpec", "views"): Doc(
        "Which drawings the note appears in, by layer name. Empty means every one of them, "
        "which is what a remark about the site itself wants; `[l3]` is for a remark that only "
        "makes sense once the picture is prefixes rather than cables."
    ),
    ("NoteSpec", "color"): Doc(
        "Fill colour, `#rgb` or `#rrggbb`. Absent lets the renderer pick, which is the one "
        "presentational decision it is allowed to make for itself."
    ),
    ("NoteSpec", "text"): Doc(
        "What the note says, in the markdown subset of §21.1: paragraphs, `**bold**`, "
        "`*italic*`, `` `code` `` and `- ` bullets. Anything else is drawn verbatim, because "
        "several very different exporters have to agree about the result."
    ),
    ("NoteSpec", "anchor"): Doc(
        "What the note is about. An anchored note follows what it is anchored to when the "
        "diagram is laid out again; a note with only a position does not."
    ),
    ("NoteSpec", "geometry"): Doc(
        "Where the note is drawn, and how big. Required unless `anchor` says what to attach it "
        "to; given as well as an anchor, the point wins and the anchor is what the leader points "
        "at — which is what dragging an anchored note produces."
    ),
    ("NoteSpec", "leader"): Doc(
        "Draw a line from the note to what it is anchored to. Inert without an `anchor`."
    ),
    ("NoteAnchor", "element"): Doc(
        "The element the note is about, by reference. Exactly one of `element` and `link` is "
        "written."
    ),
    ("NoteAnchor", "link"): Doc("The cable or tunnel the note is about, by reference."),
    ("AnnotationGeometry", "x"): Doc(
        "Points from the left edge of the drawing to the **centre** of the annotation. Written "
        "with `y` or not at all: half a position places nothing."
    ),
    ("AnnotationGeometry", "y"): Doc(
        "Points from the bottom edge of the drawing, growing upwards — §18's system, so a "
        "dragged note is stored by the machinery that stores a dragged switch."
    ),
    ("AnnotationGeometry", "width"): Doc(
        "Width in points. Omitted lets the text decide, which is what keeps a note legible "
        "after it is edited."
    ),
    ("AnnotationGeometry", "height"): Doc("Height in points, omitted for the same reason."),
    ("AreaSpec", "views"): Doc(
        "Which drawings the area appears in, by layer name. Empty means every one of them."
    ),
    ("AreaSpec", "color"): Doc(
        "Fill colour, `#rgb` or `#rrggbb`. The box is drawn behind the nodes, so a pale one is "
        "the readable choice."
    ),
    ("AreaSpec", "label"): Doc(
        "The caption drawn on the box. Absent draws it unlabelled, which is legitimate for a "
        "purely visual grouping."
    ),
    ("AreaSpec", "members"): Doc(
        "The elements the zone encloses, named outright. The box is the hull of wherever they "
        "were drawn, so it follows them."
    ),
    ("AreaSpec", "selector"): Doc(
        "The elements the zone encloses, said as a query instead of a list — the form that does "
        "not go stale when the inventory grows."
    ),
    ("AreaSpec", "geometry"): Doc(
        "An explicit rectangle, for a zone that is a region of the canvas rather than a set of "
        'devices: "everything below this line is on the UPS". Needs a position and a size.'
    ),
    ("AreaSpec", "border"): Doc(
        "How the outline is drawn. `dashed` by default because a zone is a convention rather "
        "than a cable, and a solid box reads as a real container."
    ),
    ("AreaSpec", "padding"): Doc(
        "Space in points between the hull of the members and the box drawn round them. Ignored "
        "when `geometry` gives the rectangle outright."
    ),
    ("AreaSelector", "namespace"): Doc(
        "A namespace prefix: `sites/hq` matches `sites/hq` and everything under it. At least one "
        "clause is required — an empty selector would box the whole inventory."
    ),
    ("AreaSelector", "labels"): Doc(
        "Every one of these labels must be present with this value. Combined with the other "
        "clauses by and, never by or."
    ),
    ("AreaSelector", "kinds"): Doc(
        "Element kinds, for a zone that is about a class of thing rather than a place."
    ),
    ("LegendSpec", "views"): Doc(
        "Which drawings the key appears in, by layer name. Empty means every one of them."
    ),
    ("LegendSpec", "color"): Doc("Background of the key box, `#rgb` or `#rrggbb`."),
    ("LegendSpec", "title"): Doc("Heading of the key. Absent draws the swatches on their own."),
    ("LegendSpec", "corner"): Doc(
        "Which corner of the drawing the key sits in. A corner rather than a coordinate, so it "
        "stays at the edge of the paper when the diagram is laid out again."
    ),
    ("LegendSpec", "auto"): Doc(
        "`layers` builds the entries from what the view actually drew — the node kinds and link "
        "media present — which is the only form of key that cannot go stale. Exclusive with "
        "`entries`."
    ),
    ("LegendSpec", "entries"): Doc(
        "The rows, written out. Required unless `auto` derives them; a key nobody can read is "
        "not a key, so there is a ceiling on how many there may be."
    ),
    ("LegendEntry", "label"): Doc("What this swatch means, in the reader's words."),
    ("LegendEntry", "color"): Doc(
        "Colour of the swatch. Absent takes the renderer's colour for whatever the row is about."
    ),
    ("LegendEntry", "shape"): Doc(
        "What the swatch is drawn as. A line style says the row is about links; a box says it "
        "is about nodes."
    ),
    ("LegendEntry", "description"): Doc("A second line, for the row that needs one."),
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
