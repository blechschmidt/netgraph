"""``interfaces`` — the dialect for a device netviz has no dialect for.

A campus inventory is mostly boxes nobody generates netplan for: a Catalyst, a
MikroTik, an access point, a hub. Six of the seven dialects here would say nothing
about any of them, and a configuration export that silently covers a third of the
estate is worse than one that covers none — the third that is missing is the
third somebody will assume is fine.

So this dialect covers *everything*, and it is the only one that can promise to:
it is not somebody else's grammar, it is netviz's own, and it is defined as
whatever holds one device's interface configuration completely. It therefore has
no :class:`~netviz.export.config.model.Unsupported` list at all, and that is a
statement about the format rather than an omission — a dialect that can refuse
nothing is a dialect that hides nothing.

What it is *not* is applicable. Nothing consumes this file; it is what a person
reads before typing into a vendor CLI, what a diff shows when a switch's
configuration is meant to change, and what ``netviz drift`` reads back as a
capture. That last one is why the grammar is strict rather than pretty.

The grammar
-----------

Stanzas begin in column 0 and their attributes are indented; a stanza ends at the
next unindented line. Attribute names are ``lower-case-with-hyphens``, values run
to the end of the line, and a repeated attribute is a list::

    interface eno1
        type ethernet
        description Uplink to the core switch
        mtu 1500
        vlan-mode trunk
        vlan-tagged 10,20,30
        ipv4-address 192.168.10.20/24
        ipv4-gateway 192.168.10.1

Nine stanza kinds — ``device``, ``vlan``, ``netns``, ``vrf``, ``route-table``,
``interface``, ``route``, ``policy`` and ``tunnel`` — in that order, which is the
order they depend on each other in: a route is placed in a table, and a policy
rule selects one. A value is present exactly when the inventory states it: there
is no default to read into an absent line, which is what makes the file a
faithful projection and what lets :mod:`netviz.importer.config.neutral` read it
back without inventing the difference between "off" and "not said".
"""

from __future__ import annotations

from collections.abc import Iterator

from netviz.errors import compact_ids
from netviz.export.config.header import config_header
from netviz.export.config.model import ConfigFile
from netviz.export.config.plan import DevicePlan, TunnelPlan
from netviz.export.manifest import Recorder
from netviz.models import (
    Interface,
    NetnsDefinition,
    PolicyRule,
    RouteTable,
    StaticRoute,
    VlanDefinition,
    VrfDefinition,
)
from netviz.models.interface import WirelessConfig
from netviz.models.power import PoeConfig

__all__ = ["FILENAME", "files", "selects"]

#: Where the file lands under the device's directory. Not a system path: no
#: system reads this, and pretending one does would invite somebody to copy it
#: into ``/etc``.
FILENAME = "interfaces.conf"


def selects(plan: DevicePlan) -> bool:
    """Every device. That is the entire point of this dialect."""
    return True


def files(plan: DevicePlan, recorder: Recorder) -> tuple[ConfigFile, ...]:
    """The one file this dialect writes for ``plan``."""
    lines = [
        *config_header(
            "#",
            "interfaces",
            plan,
            notes=(
                "A vendor-neutral projection of what the inventory declares. Nothing",
                "applies this file; it is what to type into a device's own CLI, what to",
                "diff when the configuration is meant to change, and what 'netviz drift'",
                "reads back. Absent lines are absent from the inventory, not defaults.",
            ),
        ),
        *_device(plan),
    ]
    for definition in plan.device.spec.vlans:
        lines.extend(["", *_vlan(definition)])
    for entry in plan.device.spec.netns:
        lines.extend(["", *_netns(entry)])
    for vrf in plan.device.spec.vrfs:
        lines.extend(["", *_vrf(vrf)])
    for table in plan.device.spec.route_tables:
        lines.extend(["", *_route_table(table)])
    for interface in plan.interfaces:
        lines.extend(["", *_interface(plan, interface)])
    for route in plan.routes:
        lines.extend(["", *_route(route)])
    for rule in plan.device.spec.routing_policy:
        lines.extend(["", *_policy(rule)])
    for tunnel in plan.tunnels:
        lines.extend(["", *_tunnel(tunnel)])

    recorder.emitted += 1
    return (ConfigFile(path=FILENAME, content="".join(f"{line}\n" for line in lines)),)


# --------------------------------------------------------------------------- #
# Stanzas
# --------------------------------------------------------------------------- #


def _device(plan: DevicePlan) -> Iterator[str]:
    spec = plan.device.spec
    yield f"device {plan.name}"
    yield f"    element {plan.fqn}"
    yield f"    kind {plan.kind}"
    for name, value in (
        ("vendor", spec.vendor),
        ("model", spec.model),
        ("serial", spec.serial),
        ("description", plan.device.metadata.description),
    ):
        if value:
            yield f"    {name} {_inline(value)}"
    # Written for both families even when false: forwarding is the one device-wide
    # switch whose *absence* from a file would be read as "off" by a reader who
    # knows the field exists, and the two are not the same statement.
    yield f"    forwarding-ipv4 {_boolean(plan.forwards_ipv4)}"
    yield f"    forwarding-ipv6 {_boolean(plan.forwards_ipv6)}"
    if spec.bridge is not None:
        yield f"    bridge-type {spec.bridge.type.value}"
        if spec.bridge.name:
            yield f"    bridge-name {spec.bridge.name}"
        if spec.bridge.address:
            yield f"    bridge-address {spec.bridge.address}"


def _vlan(vlan: VlanDefinition) -> Iterator[str]:
    yield f"vlan {vlan.id}"
    if vlan.name:
        yield f"    name {_inline(vlan.name)}"
    if vlan.description:
        yield f"    description {_inline(vlan.description)}"


def _netns(entry: NetnsDefinition) -> Iterator[str]:
    """A ``netns`` stanza — one network namespace of the machine (§23.1).

    Before the interfaces, because they name it, and after the VLANs for the
    same reason the ``vrf`` stanzas are: this file is written in dependency
    order so that a reader typing it in from the top never refers forward.
    """
    yield f"netns {entry.name}"
    if entry.parent:
        yield f"    parent {entry.parent}"
    if entry.description:
        yield f"    description {_inline(entry.description)}"


def _vrf(vrf: VrfDefinition) -> Iterator[str]:
    yield f"vrf {vrf.name}"
    yield f"    rd {vrf.rd}"
    if vrf.description:
        yield f"    description {_inline(vrf.description)}"


def _interface(plan: DevicePlan, interface: Interface) -> Iterator[str]:
    yield f"interface {interface.name}"
    yield f"    type {interface.type.value}"
    if interface.description:
        yield f"    description {_inline(interface.description)}"
    if not interface.enabled:
        # Only the *administratively down* state is written. ``enabled`` defaults
        # to true, so a line for it would be netviz's default rather than the
        # document's statement -- and this file's contract is that every line is
        # the latter.
        yield "    enabled false"
    if interface.mac:
        yield f"    mac {interface.mac}"
    if interface.mtu is not None:
        yield f"    mtu {interface.mtu}"
    if interface.parent:
        yield f"    parent {interface.parent}"
    for member in interface.members or ():
        yield f"    member {member}"
    if interface.netns:
        yield f"    netns {interface.netns}"
    if interface.peer:
        yield f"    peer {interface.peer}"
    if interface.vrf:
        yield f"    vrf {interface.vrf}"
    yield from _vlan_block(interface)
    yield from _family(interface, "ipv4")
    yield from _family(interface, "ipv6")
    if interface.wireless is not None:
        yield from _wireless(interface.wireless)
    if interface.poe is not None:
        yield from _poe(interface.poe)
    tunnel = plan.tunnel_on(interface.name)
    if tunnel is not None:
        yield f"    tunnel {tunnel.name}"


def _vlan_block(interface: Interface) -> Iterator[str]:
    vlan = interface.vlan
    if vlan is None:
        return
    yield f"    vlan-mode {vlan.mode.value}"
    if vlan.access_vlan is not None:
        yield f"    vlan-access {vlan.access_vlan}"
    if vlan.native_vlan is not None:
        yield f"    vlan-native {vlan.native_vlan}"
    if vlan.trunk_vlans:
        yield f"    vlan-tagged {compact_ids(vlan.trunk_vlans)}"
    if not vlan.ingress_filtering:
        yield "    vlan-ingress-filtering false"
    if vlan.acceptable_frames is not None:
        yield f"    vlan-acceptable-frames {vlan.acceptable_frames.value}"


def _family(interface: Interface, family: str) -> Iterator[str]:
    config = interface.ipv4 if family == "ipv4" else interface.ipv6
    if config is None:
        return
    if not config.enabled:
        yield f"    {family}-enabled false"
    for address in config.addresses:
        yield f"    {family}-address {address.ip}/{address.prefix_length}"
    if config.gateway is not None:
        yield f"    {family}-gateway {config.gateway}"
    if config.mtu is not None:
        yield f"    {family}-mtu {config.mtu}"
    if config.forwarding:
        yield f"    {family}-forwarding true"


def _wireless(wireless: WirelessConfig) -> Iterator[str]:
    yield f"    wireless-role {wireless.role.value}"
    if wireless.band is not None:
        yield f"    wireless-band {wireless.band.value}"
    if wireless.channel is not None:
        yield f"    wireless-channel {wireless.channel}"
    if wireless.width_mhz is not None:
        yield f"    wireless-width {wireless.width_mhz}"
    if wireless.tx_power_dbm is not None:
        yield f"    wireless-tx-power {wireless.tx_power_dbm}"
    for bss in wireless.bss:
        # One line per BSS rather than a nested stanza. The SSID is the one
        # field here that may hold a space, so it is the one field that is
        # quoted -- and it has to be, because an SSID is an opaque octet string
        # (section 4.1) and 'Guest  WiFi' and 'Guest WiFi' are two networks.
        parts = [f"ssid={_quoted(bss.ssid)}"]
        if bss.bssid:
            parts.append(f"bssid={bss.bssid}")
        if bss.vlan is not None:
            parts.append(f"vlan={bss.vlan}")
        if bss.security is not None:
            parts.append(f"security={bss.security.value}")
        if bss.hidden:
            parts.append("hidden=true")
        yield f"    wireless-bss {' '.join(parts)}"


def _poe(poe: PoeConfig) -> Iterator[str]:
    yield f"    poe-standard {poe.standard.value}"
    yield f"    poe-enabled {_boolean(poe.enabled)}"
    if poe.pse_class is not None:
        yield f"    poe-class {poe.pse_class}"
    if poe.budget_watts is not None:
        yield f"    poe-budget-watts {poe.budget_watts}"


def _route_table(table: RouteTable) -> Iterator[str]:
    """A ``route-table`` stanza — one routing table of the device (§16.3).

    Before the routes, because they are placed in it, and before the policy,
    because that selects it: the same dependency order every other stanza kind
    here is written in.
    """
    yield f"route-table {table.name}"
    yield f"    id {table.id}"
    if table.description:
        yield f"    description {_inline(table.description)}"


def _route(route: StaticRoute) -> Iterator[str]:
    words = [f"route {route.prefix}"]
    if route.blackhole:
        words.append("blackhole")
    if route.via is not None:
        words.extend(["via", str(route.via)])
    if route.dev is not None:
        words.extend(["dev", route.dev])
    if route.vrf is not None:
        words.extend(["vrf", route.vrf])
    if route.table is not None:
        words.extend(["table", route.table])
    if route.metric is not None:
        words.extend(["metric", str(route.metric)])
    yield " ".join(words)


def _policy(rule: PolicyRule) -> Iterator[str]:
    """A ``policy`` stanza — one rule of the policy database (§16.4).

    Opened by the priority, because that is the rule's identity: it is where the
    rule sits in the walk, and two rules of one family cannot share it
    (``NG-F020``). ``family`` is written only when the document states it, since
    a rule that states none is installed in both — and reading that back as
    ``ipv4`` would silently halve it.
    """
    yield f"policy {rule.priority}"
    if rule.family is not None:
        yield f"    family {rule.family.value}"
    for name, value in (
        ("from", rule.src),
        ("to", rule.dst),
        ("iif", rule.iif),
        ("oif", rule.oif),
        ("fwmark", rule.fwmark),
        ("dscp", rule.dscp),
    ):
        if value is not None:
            yield f"    {name} {value}"
    if rule.invert:
        yield "    invert true"
    yield f"    action {rule.action.value}"
    if rule.table is not None:
        yield f"    table {rule.table}"
    if rule.goto is not None:
        yield f"    goto {rule.goto}"
    if rule.description:
        yield f"    description {_inline(rule.description)}"


def _tunnel(plan: TunnelPlan) -> Iterator[str]:
    spec = plan.tunnel.spec
    yield f"tunnel {plan.name} {spec.type.value}"
    yield f"    element {plan.fqn}"
    yield f"    interface {plan.interface.name}"
    if spec.port is not None:
        yield f"    port {spec.port}"
    if spec.vni is not None:
        yield f"    vni {spec.vni}"
    if spec.mode is not None:
        yield f"    mode {spec.mode.value}"
    if spec.mtu is not None:
        yield f"    mtu {spec.mtu}"
    if spec.encrypted is not None:
        yield f"    encrypted {_boolean(spec.encrypted)}"
    if spec.cipher:
        yield f"    cipher {_inline(spec.cipher)}"
    if spec.auth is not None:
        yield f"    auth {spec.auth.value}"
    if spec.over is not None:
        yield f"    over {spec.over}"
    for peer in plan.peers:
        words = [f"    peer {peer.name}:{peer.interface}"]
        if peer.endpoint:
            words.append(f"endpoint={peer.endpoint}")
        if peer.overlay:
            words.append(f"overlay={','.join(peer.overlay)}")
        yield " ".join(words)
        if peer.endpoint_note:
            yield f"    # {peer.endpoint_note}"


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def _boolean(value: bool) -> str:
    return "true" if value else "false"


def _inline(text: str) -> str:
    """``text`` with its line breaks replaced by single spaces, and nothing else.

    A description and a vendor string are free text and may hold a newline; a
    value here runs to the end of the line, so a newline in one would turn the
    rest of the value into a stanza. That much has to go, and losing it is
    visible in the output, which is the right failure: the alternative is a file
    that parses back into something else.

    Runs of spaces are left exactly as written. Collapsing them would be lossy
    for no reason in a description, and *wrong* in the one field where it
    matters — see :func:`_quoted`.
    """
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _quoted(text: str) -> str:
    """``text`` as a double-quoted token, for a value inside a key=value list.

    Only the SSID needs this, and it needs it for a reason worth stating: an
    SSID is an opaque octet string (§4.1), so ``Guest  WiFi`` and ``Guest WiFi``
    are two different networks and a station configured with the second never
    associates with the first. Written unquoted into a space-separated list, the
    two would be one token and the difference would be gone.
    """
    escaped = _inline(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
