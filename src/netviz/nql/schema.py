"""What a relational query may ask about, spelled once.

This table *is* the query language's coupling to ``docs/schema.md``. Every name
a query may write — a type to select from, a property to compare, a link to
navigate — is a row here, and nothing else is addressable. Two consequences,
both deliberate:

* A misspelling is a parse error with a suggestion, before an inventory is read.
  ``select interface { mak }`` never touches a file.
* Adding a fact to the language is two edits and no more: a row here, and the
  reader that fills it in :mod:`netviz.nql.world`. A test walks both and fails
  when only one of them was made.

The shape of the table is the shape of the document it describes. ``element`` is
the envelope every declared thing has (§3); ``device`` narrows it to the six
kinds that own interfaces and configuration (§6); ``interface``, ``address``,
``vlan`` and ``netns`` are the sub-objects a device holds; ``subnet``,
``broadcast_domain`` and ``link`` are derived — nobody declares them, and they
exist here for the same reason they exist in a diagram, which is that they are
the answer to the question an operator actually asks.

Naming
------

Types are singular and lower case (``interface``, not ``Interfaces``), because a
type names *one* of a thing even when a query returns many. Links are plural
when they are :attr:`~netviz.nql.types.Cardinality.MANY` (``interfaces``) and
singular when they are not (``parent``), so cardinality is legible before
``--describe`` is consulted.
"""

from __future__ import annotations

from typing import Final

from netviz.nql.types import Cardinality, Link, ObjectType, Property, ScalarKind, Schema

__all__ = ["SCHEMA"]

_ONE: Final = Cardinality.ONE
_OPT: Final = Cardinality.OPTIONAL
_MANY: Final = Cardinality.MANY

_STR: Final = ScalarKind.STR
_INT: Final = ScalarKind.INT
_FLOAT: Final = ScalarKind.FLOAT
_BOOL: Final = ScalarKind.BOOL
_IP: Final = ScalarKind.IP
_CIDR: Final = ScalarKind.CIDR


def _props(*properties: Property) -> dict[str, Property]:
    return {one.name: one for one in properties}


def _links(*links: Link) -> dict[str, Link]:
    return {one.name: one for one in links}


# --------------------------------------------------------------------------- #
# The envelope every declared element has (§3)
# --------------------------------------------------------------------------- #

_ELEMENT = ObjectType(
    name="element",
    summary="Anything a document declares: a device, a cable, a tunnel, an identity.",
    abstract=True,
    properties=_props(
        Property("name", _STR, _ONE, "Short name, unique within its namespace."),
        Property("fqn", _STR, _ONE, "Fully-qualified name, namespace included.", aliases=("id",)),
        Property("namespace", _STR, _ONE, "Folder path the document was found in.", ("ns",)),
        Property("kind", _STR, _ONE, "The document's `kind`: switch, cable, user, …"),
        Property("description", _STR, _OPT, "`metadata.description`.", aliases=("desc",)),
        Property("site", _STR, _OPT, "`metadata.location.site`."),
        Property("room", _STR, _OPT, "`metadata.location.room`."),
        Property("rack_name", _STR, _OPT, "`metadata.location.rack`."),
        Property("position", _INT, _OPT, "Lowest rack unit the element occupies."),
        Property("height", _INT, _OPT, "How many rack units it occupies."),
        Property(
            "labels",
            _STR,
            _MANY,
            "`metadata.labels` as `key=value`; read one with `lookup(.labels, 'role')`.",
        ),
        Property("annotations", _STR, _MANY, "`metadata.annotations` as `key=value`."),
        Property("file", _STR, _OPT, "Inventory-relative path of the declaring document."),
        Property("line", _INT, _OPT, "Line the document starts on."),
    ),
    links=_links(
        Link("interfaces", "interface", _MANY, "Ports this element owns; empty for a cable."),
        Link("addresses", "address", _MANY, "Every address on every one of its ports."),
        Link("subnets", "subnet", _MANY, "Prefixes it has an address in."),
        Link("broadcast_domains", "broadcast_domain", _MANY, "Domains it is a member of (§9.3)."),
        Link("links", "link", _MANY, "Cables, attachments and tunnels terminating here."),
        Link("neighbors", "element", _MANY, "Elements one link away.", ("neighbours",)),
        Link("rack", "rack", _OPT, "The rack the element is mounted in."),
    ),
)

# --------------------------------------------------------------------------- #
# Devices (§6) and the other element kinds
# --------------------------------------------------------------------------- #

_DEVICE = ObjectType(
    name="device",
    summary="A machine with interfaces and configuration: the six kinds of §6.",
    bases=("element",),
    abstract=True,
    properties=_props(
        Property("vendor", _STR, _OPT, "`spec.vendor`."),
        Property("model", _STR, _OPT, "`spec.model`."),
        Property("serial", _STR, _OPT, "`spec.serial`."),
        Property("location", _STR, _OPT, "Free-text `spec.location`."),
        Property("asn", _INT, _OPT, "Local autonomous system number (§16.8)."),
        Property("router_id", _STR, _OPT, "BGP/OSPF router id."),
        Property("forwards", _BOOL, _OPT, "Does the device route between its interfaces?"),
    ),
    links=_links(
        Link("vlans", "vlan", _MANY, "The device's VLAN database, `spec.vlans` (§6.4)."),
        Link("netns", "netns", _MANY, "Network namespaces declared on it (§23.1)."),
        Link("zones", "zone", _MANY, "Security zones declared on it (§24.1)."),
        Link("routes", "route", _MANY, "Static routes, `spec.routes` (§16.5)."),
    ),
)

_DEVICE_KINDS: Final = {
    "switch": "A layer-2 forwarder.",
    "router": "A layer-3 forwarder.",
    "firewall": "A router that also filters (§24).",
    "hub": "A repeater: one collision domain, no forwarding table.",
    "computer": "An end host somebody uses.",
    "server": "An end host that serves.",
}

_ADAPTER = ObjectType(
    name="adapter",
    summary="Network ports presented over a non-network host port: a dongle, a dock, an SFP.",
    bases=("element",),
    properties=_props(
        Property("vendor", _STR, _OPT, "`spec.vendor`."),
        Property("model", _STR, _OPT, "`spec.model`."),
        Property("serial", _STR, _OPT, "`spec.serial`."),
        Property("form_factor", _STR, _OPT, "usb-ethernet, dock, media-converter, sfp-module."),
        Property("passthrough", _BOOL, _ONE, "May a renderer collapse it into its host (§8.2)?"),
        Property("upstream", _STR, _ONE, "Name of the host-facing port."),
        Property("upstream_type", _STR, _ONE, "usb, thunderbolt, pcie, sfp, internal."),
    ),
    links=_links(
        Link("attached_to", "element", _OPT, "The host it is plugged into.", ("host",)),
    ),
)

_PATCHPANEL = ObjectType(
    name="patchpanel",
    summary="A passive cross-connect: front ports coupled to rear ports (§15).",
    bases=("element",),
    aliases=("panel",),
    properties=_props(
        Property("vendor", _STR, _OPT, "`spec.vendor`."),
        Property("model", _STR, _OPT, "`spec.model`."),
        Property("serial", _STR, _OPT, "`spec.serial`."),
        Property("form_factor", _STR, _OPT, "keystone, fibre-lc, coupler."),
        Property("ports", _INT, _ONE, "How many front/rear port pairs it has."),
    ),
)

_PDU = ObjectType(
    name="pdu",
    summary="A power distribution unit and its outlets (§17).",
    bases=("element",),
    properties=_props(
        Property("vendor", _STR, _OPT, "`spec.vendor`."),
        Property("model", _STR, _OPT, "`spec.model`."),
        Property("serial", _STR, _OPT, "`spec.serial`."),
        Property("form_factor", _STR, _OPT, "rack-vertical, rack-horizontal, desktop."),
        Property("outlets", _INT, _ONE, "How many outlets it has."),
        Property("capacity_watts", _FLOAT, _OPT, "What the whole strip may deliver."),
        Property("input_feed", _STR, _OPT, "Which building feed it is on, for redundancy."),
    ),
)

_CABLE = ObjectType(
    name="cable",
    summary="An undirected physical link between exactly two interfaces (§7).",
    bases=("element",),
    properties=_props(
        Property("medium", _STR, _ONE, "copper, fiber or wireless."),
        Property("speed", _INT, _OPT, "Negotiated link rate in bits per second."),
        Property("duplex", _STR, _ONE, "full or half."),
        Property("length_m", _FLOAT, _OPT, "Length of the run in metres."),
        Property("category", _STR, _OPT, "cat6a, om4, …"),
        Property("connector", _STR, _OPT, "rj45, lc, sc, …"),
        Property("label", _STR, _OPT, "What is written on the cable."),
    ),
    links=_links(
        Link("ends", "interface", _MANY, "The two interfaces it joins.", ("endpoints",)),
        Link("elements", "element", _MANY, "The two elements it joins."),
    ),
)

_TUNNEL = ObjectType(
    name="tunnel",
    summary="An overlay link between interfaces: WireGuard, IPsec, VXLAN, … (§14).",
    bases=("element",),
    properties=_props(
        Property("type", _STR, _ONE, "wireguard, ipsec, openvpn, gre, vxlan, geneve, …"),
        Property("layer", _INT, _ONE, "2 for a bridged overlay, 3 for a routed one."),
        Property("encrypted", _BOOL, _ONE, "Does the type or the configuration encrypt?"),
        Property("vni", _INT, _OPT, "VXLAN or Geneve virtual network identifier."),
        Property("mtu", _INT, _OPT, "Overlay MTU."),
        Property("port", _INT, _OPT, "Transport port."),
        Property("mode", _STR, _OPT, "IPsec tunnel or transport mode."),
        Property("multipoint", _BOOL, _ONE, "Does it join more than two ends?"),
        Property("label", _STR, _OPT, "Drawing label."),
    ),
    links=_links(
        Link("ends", "interface", _MANY, "The interfaces it terminates on.", ("endpoints",)),
        Link("elements", "element", _MANY, "The elements it joins."),
        Link("over", "tunnel", _OPT, "The tunnel this one is encapsulated in."),
    ),
)

_USER = ObjectType(
    name="user",
    summary="A person, a service account or a shared account (§19.1).",
    bases=("element",),
    properties=_props(
        Property("login", _STR, _ONE, "The account name."),
        Property("full_name", _STR, _OPT, "The person's name as they write it."),
        Property("email", _STR, _OPT, "Where mail reaches them."),
        Property("uid", _INT, _OPT, "POSIX user id."),
        Property("type", _STR, _ONE, "person, service or shared."),
        Property("status", _STR, _ONE, "active, disabled or …"),
        Property("keys", _INT, _ONE, "How many SSH public keys the account has."),
    ),
    links=_links(Link("groups", "group", _MANY, "Groups the account is a direct member of.")),
)

_GROUP = ObjectType(
    name="group",
    summary="A named set of users and groups (§19.2).",
    bases=("element",),
    properties=_props(
        Property("gid", _INT, _OPT, "POSIX group id."),
        Property("email", _STR, _OPT, "Where mail to the whole group goes."),
    ),
    links=_links(
        Link("members", "element", _MANY, "Direct members: users and nested groups."),
        Link("groups", "group", _MANY, "Groups this group is a direct member of."),
    ),
)

# --------------------------------------------------------------------------- #
# Sub-objects of a device
# --------------------------------------------------------------------------- #

_INTERFACE = ObjectType(
    name="interface",
    summary="One entry of `spec.interfaces` — a port, a bridge, a sub-interface (§6.2).",
    aliases=("port",),
    properties=_props(
        Property("name", _STR, _ONE, "Interface name within its element."),
        Property("fqn", _STR, _ONE, "`element:name`, which is how a cable refers to it."),
        Property("type", _STR, _ONE, "ethernet, wifi, loopback, bridge, vlan, lag, tunnel."),
        Property("description", _STR, _OPT, "Free text.", aliases=("desc",)),
        Property("enabled", _BOOL, _ONE, "Intended admin state."),
        Property("mac", _STR, _OPT, "Hardware address, `if:phys-address`."),
        Property("mtu", _INT, _OPT, "Layer-2 MTU."),
        Property("vlan_mode", _STR, _OPT, "access or trunk, when the port is a bridge port."),
        Property("pvid", _INT, _OPT, "Untagged VLAN of the port."),
        Property("vrf", _STR, _OPT, "Routing instance the port is bound to (§16.1)."),
        Property("netns_name", _STR, _ONE, "Namespace the port is in; empty for the initial one."),
        Property("is_veth", _BOOL, _ONE, "Is this one end of a veth pair (§23.2)?"),
        Property("ssid", _STR, _MANY, "SSIDs a wifi interface serves or joins (§6.2.6)."),
        Property("band", _STR, _OPT, "Radio band of a wifi interface."),
        Property("channel", _INT, _OPT, "Radio channel of a wifi interface."),
        Property("radio_role", _STR, _OPT, "ap, station or mesh."),
        Property("poe", _BOOL, _ONE, "Is this port power sourcing equipment (§17.3)?"),
    ),
    links=_links(
        Link(
            "parent",
            "element",
            _ONE,
            "The element the interface is attached to: a device, an adapter or a panel.",
            aliases=("element", "owner"),
        ),
        Link("addresses", "address", _MANY, "Configured addresses.", aliases=("ips",)),
        Link("subnets", "subnet", _MANY, "Prefixes the port has an address in."),
        Link("vlans", "vlan", _MANY, "VLANs the port is a member of."),
        Link("broadcast_domains", "broadcast_domain", _MANY, "Domains the port is in (§9.3)."),
        Link("cable", "cable", _OPT, "The cable plugged into this port."),
        Link("tunnels", "tunnel", _MANY, "Tunnels terminating on this port."),
        Link("links", "link", _MANY, "Every graph link terminating here."),
        Link("peer", "interface", _OPT, "The far end of the cable, if there is one."),
        Link("veth_peer", "interface", _OPT, "The other end of the veth pair (§23.2)."),
        Link("base", "interface", _OPT, "`lower-layer-if`: the port a sub-interface sits on."),
        Link("members", "interface", _MANY, "Member ports of a bridge or a LAG."),
        Link("member_of", "interface", _MANY, "Bridges and LAGs this port is a member of."),
        Link("netns", "netns", _OPT, "The namespace the port is in, when it is not the initial."),
        Link("zone", "zone", _OPT, "The security zone the port is in (§24.1)."),
    ),
)

_ADDRESS = ObjectType(
    name="address",
    summary="One configured IP address, and where it is configured.",
    aliases=("ip",),
    properties=_props(
        Property("address", _STR, _ONE, "`10.0.0.1/24` — the address as configured."),
        Property("ip", _IP, _ONE, "`10.0.0.1` — the host part, without the prefix length."),
        Property("prefix_length", _INT, _ONE, "The `/24`."),
        Property("network", _CIDR, _ONE, "`10.0.0.0/24` — the prefix it sits in."),
        Property("family", _INT, _ONE, "4 or 6."),
        Property("vrf", _STR, _ONE, "Routing instance; empty for the global one."),
        Property("netns_name", _STR, _ONE, "Namespace; empty for the initial one."),
        Property("is_routable", _BOOL, _ONE, "False for loopback and link-local addresses."),
        Property("is_gateway", _BOOL, _ONE, "Is this address a declared default gateway?"),
    ),
    links=_links(
        Link("interface", "interface", _ONE, "The port it is configured on."),
        Link("element", "element", _ONE, "The element that port belongs to."),
        Link("subnet", "subnet", _OPT, "The derived prefix it is a member of."),
    ),
)

_VLAN = ObjectType(
    name="vlan",
    summary="One 802.1Q VLAN id, and everything that mentions it (§9).",
    properties=_props(
        Property("id", _INT, _ONE, "The VLAN id, 1 to 4094.", aliases=("vid",)),
        Property("name", _STR, _OPT, "Name from a device's VLAN database."),
        Property(
            "description", _STR, _OPT, "Description from a device's VLAN database.", ("desc",)
        ),
    ),
    links=_links(
        Link("interfaces", "interface", _MANY, "Ports that are members of it."),
        Link("elements", "element", _MANY, "Elements with a port in it."),
        Link("declared_on", "device", _MANY, "Devices whose VLAN database names it (§6.4)."),
        Link("links", "link", _MANY, "Cables and tunnels that carry it."),
        Link("broadcast_domains", "broadcast_domain", _MANY, "Its domains: one per component."),
        Link("subnets", "subnet", _MANY, "Prefixes addressed on it."),
    ),
)

_NETNS = ObjectType(
    name="netns",
    summary="A network namespace: a second, independent network stack in one machine (§23.1).",
    aliases=("namespace",),
    properties=_props(
        Property("name", _STR, _ONE, "Namespace name within its device."),
        Property("fqn", _STR, _ONE, "`device:namespace`."),
        Property("path", _STR, _ONE, "`blue/web` — the chain from the initial namespace."),
        Property("depth", _INT, _ONE, "How deeply it nests; 1 sits in the initial namespace."),
        Property("description", _STR, _OPT, "Free text.", aliases=("desc",)),
    ),
    links=_links(
        Link("device", "element", _ONE, "The machine the namespace runs in."),
        Link("parent", "netns", _OPT, "The namespace it was created from."),
        Link("children", "netns", _MANY, "Namespaces created from this one."),
        Link("interfaces", "interface", _MANY, "Ports moved into it."),
        Link("addresses", "address", _MANY, "Addresses configured inside it."),
    ),
)

_ZONE = ObjectType(
    name="zone",
    summary="A firewall security zone and the interfaces in it (§24.1).",
    properties=_props(
        Property("name", _STR, _ONE, "Zone name within its device."),
        Property("fqn", _STR, _ONE, "`device:zone`."),
        Property("description", _STR, _OPT, "Free text.", aliases=("desc",)),
        Property("rules", _INT, _ONE, "How many filter rules name this zone."),
    ),
    links=_links(
        Link("device", "element", _ONE, "The device the zone is declared on."),
        Link("interfaces", "interface", _MANY, "Ports in the zone."),
    ),
)

_ROUTE = ObjectType(
    name="route",
    summary="One static route from a device's `spec.routes` (§16.5).",
    properties=_props(
        Property("destination", _CIDR, _ONE, "The prefix being routed.", aliases=("prefix",)),
        Property("via", _IP, _OPT, "Next-hop address."),
        Property("interface_name", _STR, _OPT, "Egress interface, when the route names one."),
        Property("vrf", _STR, _ONE, "Routing instance; empty for the global one."),
        Property("table", _STR, _OPT, "Routing table, when the route is in a named one."),
        Property("metric", _INT, _OPT, "Administrative distance or metric."),
        Property("family", _INT, _ONE, "4 or 6."),
        Property("blackhole", _BOOL, _ONE, "Does the route discard rather than forward?"),
    ),
    links=_links(
        Link("device", "element", _ONE, "The device the route is configured on."),
        Link("interface", "interface", _OPT, "The egress interface, resolved."),
    ),
)

# --------------------------------------------------------------------------- #
# Derived: nobody declares these, and everybody asks about them
# --------------------------------------------------------------------------- #

_SUBNET = ObjectType(
    name="subnet",
    summary="An IP prefix derived from the addresses configured in it.",
    aliases=("prefix",),
    properties=_props(
        Property("prefix", _CIDR, _ONE, "`10.0.0.0/24`."),
        Property("fqn", _STR, _ONE, "Identity: the prefix, and the VRF when it is not global."),
        Property("vrf", _STR, _ONE, "Routing instance; empty for the global one."),
        Property("family", _INT, _ONE, "4 or 6."),
        Property("prefix_length", _INT, _ONE, "The `/24`."),
        Property("size", _INT, _ONE, "How many addresses the prefix holds."),
        Property("used", _INT, _ONE, "How many distinct addresses are configured in it."),
        Property("free", _INT, _ONE, "How many are not."),
        Property("utilisation", _FLOAT, _ONE, "used / size, between 0 and 1."),
        Property("is_point_to_point", _BOOL, _ONE, "Is it a /30 to /32, or a /126 to /128?"),
    ),
    links=_links(
        Link("addresses", "address", _MANY, "Every address configured in the prefix."),
        Link("interfaces", "interface", _MANY, "Every port with an address in it."),
        Link("elements", "element", _MANY, "Every element with an address in it."),
        Link("vlans", "vlan", _MANY, "VLANs the addressed ports are members of."),
    ),
)

_BROADCAST_DOMAIN = ObjectType(
    name="broadcast_domain",
    summary="One VLAN's worth of elements that can actually reach each other (§9.3).",
    aliases=("domain", "l2domain"),
    properties=_props(
        Property("id", _STR, _ONE, "`vlan:10`, or `vlan:10#2` when the VLAN is partitioned."),
        Property("name", _STR, _ONE, "`vlan10`, the short label a diagram uses."),
        Property("vlan_id", _INT, _ONE, "The VLAN id the domain carries."),
        Property("index", _INT, _ONE, "1-based ordinal among the domains carrying that id."),
        Property("size", _INT, _ONE, "How many elements are in it."),
        Property("is_isolated", _BOOL, _ONE, "Is the VLAN configured here and carried nowhere?"),
    ),
    links=_links(
        Link("vlan", "vlan", _ONE, "The VLAN this domain carries."),
        Link("members", "element", _MANY, "Elements in the domain.", aliases=("elements",)),
        Link("interfaces", "interface", _MANY, "Ports through which they join it."),
        Link("links", "link", _MANY, "Cables and tunnels carrying the VLAN inside it."),
    ),
)

_LINK = ObjectType(
    name="link",
    summary="One edge of the graph: a cable, an adapter attachment or a tunnel.",
    properties=_props(
        Property("id", _STR, _ONE, "Edge identity."),
        Property("kind", _STR, _ONE, "cable, attachment or tunnel."),
        Property("medium", _STR, _OPT, "copper, fiber or wireless, for a cable."),
        Property("speed", _INT, _OPT, "Link rate in bits per second."),
        Property("label", _STR, _OPT, "What is written on it."),
    ),
    links=_links(
        Link("elements", "element", _MANY, "The two elements it joins."),
        Link("interfaces", "interface", _MANY, "The two ports it terminates on."),
        Link("vlans", "vlan", _MANY, "VLANs the link carries."),
        Link("broadcast_domains", "broadcast_domain", _MANY, "Domains the link is inside."),
        Link(
            "element",
            "element",
            _OPT,
            "The document the link came from: a cable, a tunnel, or the adapter "
            "whose `upstream.attached_to` it is.",
        ),
    ),
)

_RACK = ObjectType(
    name="rack",
    summary="A cabinet: everything that named the same site, room and rack (§3.2).",
    properties=_props(
        Property("name", _STR, _ONE, "Rack identifier within its room."),
        Property("fqn", _STR, _ONE, "`site/room/rack`."),
        Property("site", _STR, _ONE, "Site the rack is in."),
        Property("room", _STR, _ONE, "Room the rack is in."),
        Property("height", _INT, _OPT, "How many units tall, when anything declared it."),
        Property("used", _INT, _ONE, "How many units are occupied."),
    ),
    links=_links(Link("elements", "element", _MANY, "What is mounted in it, bottom first.")),
)


def _device_kinds() -> tuple[ObjectType, ...]:
    """One concrete type per device kind, each inheriting the whole of ``device``.

    They carry no members of their own: a ``server`` differs from a ``switch``
    in what it is *for*, not in what a document may say about it. Declaring
    them anyway is what lets ``select server`` and ``.parent[is router]`` be
    written, which is the question an operator asks.
    """
    return tuple(
        ObjectType(name=kind, summary=summary, bases=("device",))
        for kind, summary in _DEVICE_KINDS.items()
    )


#: The one table. Order matters twice: a type must be declared after the types
#: it inherits from, and ``--describe`` with no argument lists them in this
#: order, which is the order ``docs/schema.md`` introduces them in.
SCHEMA: Final = Schema(
    (
        _ELEMENT,
        _DEVICE,
        *_device_kinds(),
        _ADAPTER,
        _PATCHPANEL,
        _PDU,
        _CABLE,
        _TUNNEL,
        _USER,
        _GROUP,
        _INTERFACE,
        _ADDRESS,
        _VLAN,
        _NETNS,
        _ZONE,
        _ROUTE,
        _SUBNET,
        _BROADCAST_DOMAIN,
        _LINK,
        _RACK,
    )
)
