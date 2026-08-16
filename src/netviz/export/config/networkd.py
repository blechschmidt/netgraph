"""``networkd`` — the two files systemd needs per interface, and why they are two.

systemd-networkd splits a link into the thing that *exists* and the thing that is
*configured*: a ``.netdev`` creates a bridge, a bond, a VLAN or a tunnel, and a
``.network`` matched by name says what to do with a link once it is there. An
inventory writes one interface, so this dialect writes one file for a physical
port and two for anything stacked on one — and the split is not cosmetic. A key
in the wrong half applies at a different moment: ``MTUBytes=`` in a ``.netdev``
is what the device is created with, the same key in a ``.network`` is what it is
set to afterwards, and only the first survives the device being torn down and
rebuilt.

**The file names are the ordering.** networkd reads its directory in
lexicographic order and applies the first ``.network`` whose ``[Match]`` fits, so
a numeric prefix is load-bearing rather than decorative. Ports are written at
``10-`` and everything stacked on them at ``20-``. Nothing generated here matches
ambiguously — every ``[Match]`` names exactly one interface — but a directory
listing then reads in the order the stack is built, and a distribution's fragment
below and a local override above both still have a number left to take.

**It can say the thing netplan refuses.** A bridge port's 802.1Q configuration —
``mode: trunk`` with a tagged set — is the field that makes netplan give up on a
device, and networkd has ``[BridgeVLAN]`` for precisely it. A trunk or access port
is therefore written here rather than refused. The cost is that the section is
inert unless the bridge itself filters, which is why a bridge whose members
declare VLANs is given ``[Bridge] VLANFiltering=yes``: derived from those members'
own ``vlan`` blocks, and from nothing else.

**It cannot say the thing netplan can.** networkd does not configure a radio at
all — wpa_supplicant and iwd own association and key management, and this format
has no key that takes an SSID. That is a *skip* rather than a refusal, and the
distinction is the one this whole package turns on: a radio is outside networkd's
remit entirely, so its ``.network`` is still exactly right for the layer networkd
does own, and the manifest names ``netviz export netplan`` as the dialect that
writes the association. A refusal is for a field networkd *does* cover and cannot
spell, which is why only the VRF table is one.

**A VRF needs a table number.** ``Kind=vrf`` requires ``[VRF] Table=``, an
integer. The inventory states a route distinguisher (§16.1), which identifies the
VRF in BGP; turning one into the other would be netviz deciding which routes
the VRF sees. Refused, with the field named.

**A bond has no mode.** ``Kind=bond`` is written and ``[Bond] Mode=`` is not: the
inventory does not record whether a LAG is LACP or round robin, and the two fail
differently against a switch expecting the other. That is a manifest skip rather
than a refusal — the file is correct as far as it goes, and the mode is one line
for the operator to add against a switch they can see.

Two smaller things the format imposes. A comment needs a line of its own, because
systemd has no trailing ``#`` and would read one as part of the value — so the
note about a placeholder key sits above the key it describes. And forwarding is
spelled ``IPv4Forwarding=``/``IPv6Forwarding=``, which is systemd 256 and newer;
the single ``IPForward=`` key those replaced is named in the header of every file
that carries one.

Everything networkd has no business with — PoE, a routing protocol, a tunnel that
is really a daemon — is recorded in the export manifest naming the tool that does
have business with it, and is not a refusal: the generated files are still
correct for what networkd covers.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Final

from netviz.export.config.header import config_header
from netviz.export.config.model import ConfigFile, Unsupported
from netviz.export.config.netplan import HOST_KINDS
from netviz.export.config.plan import (
    DevicePlan,
    TunnelPlan,
    addresses_of,
    is_stacked,
    netns_limits,
    restricts_vlans,
    route_interface,
)
from netviz.export.manifest import Reason, Recorder
from netviz.models import Interface, InterfaceType, PolicyAction, PolicyRule, StaticRoute
from netviz.models.interface import VlanMode
from netviz.models.tunnel import TunnelType

__all__ = ["DIRECTORY", "declines", "files", "limits", "selects"]

#: Where networkd reads both halves of a link's configuration from. The
#: ``/run`` and ``/usr/lib`` copies of this directory exist too, and a generated
#: file belongs in neither: ``/etc`` is the one an operator owns.
DIRECTORY = "etc/systemd/network"

#: The ordering prefix of a port, and of anything stacked on one. See the module
#: docstring: networkd applies the first matching ``.network`` in filename order,
#: so these two numbers put a bridge member ahead of its bridge and leave room
#: for a distribution fragment below and a local override above.
_PORT_PREFIX = "10"
_STACKED_PREFIX = "20"

#: ``[NetDev] Kind=`` for the interface types the host builds itself. A tunnel is
#: absent because its kind comes from the tunnel document rather than from the
#: interface (:data:`_TUNNEL_KINDS`).
_NETDEV_KINDS: Final[dict[InterfaceType, str]] = {
    InterfaceType.BRIDGE: "bridge",
    InterfaceType.LAG: "bond",
    InterfaceType.VLAN: "vlan",
}

#: ``[NetDev] Kind=`` for the tunnel types the kernel has a netdev for. The four
#: that are missing — IPsec, OpenVPN, PPTP and L2TP — are daemons: strongSwan,
#: openvpn and pppd create and own those interfaces, and networkd has no kind
#: that would build one.
_TUNNEL_KINDS: Final[dict[TunnelType, str]] = {
    TunnelType.WIREGUARD: "wireguard",
    TunnelType.GRE: "gre",
    TunnelType.VXLAN: "vxlan",
    TunnelType.GENEVE: "geneve",
}

#: The section and identifier key of the two overlays that carry a VNI. They are
#: spelled differently for no reason other than history — VXLAN's identifier is
#: ``VNI`` and Geneve's is ``Id`` — and only ``[VXLAN]`` takes a ``Local=``.
_OVERLAY_SECTIONS: Final[dict[TunnelType, tuple[str, str]]] = {
    TunnelType.VXLAN: ("VXLAN", "VNI"),
    TunnelType.GENEVE: ("GENEVE", "Id"),
}

#: Written where networkd needs a secret. Deliberately not a valid key of any
#: length: a placeholder that parses is a placeholder that reaches production.
_PLACEHOLDER = "REPLACE-ME"


def selects(plan: DevicePlan) -> bool:
    """Is this a device that runs systemd-networkd?"""
    return plan.kind in HOST_KINDS


def declines(plan: DevicePlan) -> str:
    """Why a device this dialect did not select got no file."""
    return (
        f"systemd-networkd is a Linux host's network manager; a {plan.kind} is configured "
        f"through its own CLI, so 'netviz export interfaces' is what describes this device"
    )


def limits(plan: DevicePlan) -> tuple[Unsupported, ...]:
    """Everything networkd would have had to invent or drop for ``plan``."""
    return tuple(_limits(plan))


def _limits(plan: DevicePlan) -> Iterator[Unsupported]:
    yield from netns_limits(plan, "systemd-networkd")
    for index, vrf in enumerate(plan.device.spec.vrfs):
        yield Unsupported(
            element=plan.fqn,
            field=plan.field("vrfs", index),
            detail=(
                f"'[VRF] Table=' is an integer routing table, and networkd creates a VRF from "
                f"nothing else; the inventory states the route distinguisher {vrf.rd} for "
                f"{vrf.name!r}, which names the instance in BGP. Choosing a table number would "
                f"be choosing which routes the VRF carries"
            ),
        )


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #


def files(plan: DevicePlan, recorder: Recorder) -> tuple[ConfigFile, ...]:
    """The ``.netdev`` and ``.network`` files networkd reads for this device.

    In declaration order, and with each ``.netdev`` immediately before the
    ``.network`` that matches the device it creates: that is the order the pair
    has to be *read* in, and a generated tree is read far more often than it is
    applied.
    """
    written: list[ConfigFile] = []
    configured: set[str] = set()
    for interface in plan.interfaces:
        tunnel = plan.tunnel_on(interface.name)
        if not _is_writable(plan, interface, tunnel, recorder):
            continue
        stem = _file_stem(plan, interface, recorder)
        if is_stacked(interface):
            written.append(_netdev_file(plan, interface, tunnel, stem, recorder))
        written.append(_network_file(plan, interface, stem, unbound=not configured))
        configured.add(interface.name)

    _record_out_of_remit(plan, configured, recorder)
    if not written:
        return ()
    recorder.emitted += 1
    return tuple(written)


def _is_writable(
    plan: DevicePlan, interface: Interface, tunnel: TunnelPlan | None, recorder: Recorder
) -> bool:
    """Can networkd be given this interface at all?"""
    subject = f"{plan.fqn}:{interface.name}"
    if interface.type is InterfaceType.LOOPBACK:
        # ``lo`` is the kernel's. networkd ships its own ``99-default.link`` and
        # a loopback needs no ``.network``; a declared router loopback belongs to
        # the routing daemon, which creates it as a dummy device.
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            "the kernel owns 'lo' and networkd configures no loopback; a declared router "
            "loopback belongs to the routing daemon",
        )
        return False
    if interface.type is not InterfaceType.TUNNEL:
        return True
    if tunnel is None:
        recorder.skip(
            subject,
            Reason.UNRESOLVED,
            "a 'tunnel' interface that no tunnel document terminates on; there is no kind "
            "to build a netdev from",
        )
        return False
    if tunnel.type not in _TUNNEL_KINDS:
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            f"{tunnel.type} is a daemon rather than a netdev; strongSwan, openvpn and pppd "
            f"create these interfaces, and networkd has no kind that would",
        )
        return False
    return True


def _record_out_of_remit(plan: DevicePlan, configured: set[str], recorder: Recorder) -> None:
    """Everything networkd is simply not the tool for, named with the tool that is."""
    for index, rule in enumerate(plan.device.spec.routing_policy):
        if not rule.invert:
            continue
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            f"{plan.field('routing_policy', index, 'invert')}: policy rule {rule.priority} "
            f"matches everything its selectors do not, and '[RoutingPolicyRule]' has no key "
            f"that inverts a selector set; the rule is written without the inversion, so it "
            f"matches the opposite of what the inventory states. "
            f"'netviz export routes' writes it correctly, as 'ip rule add not ...'",
        )
    if plan.device.spec.routing is not None:
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            "'spec.routing' is a routing protocol; networkd configures interfaces and reads "
            "no adjacency. Generate it with 'netviz export frr'",
        )
    for interface in plan.interfaces:
        subject = f"{plan.fqn}:{interface.name}"
        if interface.poe is not None:
            recorder.skip(
                subject,
                Reason.NOT_REPRESENTABLE,
                "'poe' is switch hardware configuration; networkd does not touch the PSE",
            )
        if interface.type is InterfaceType.TUNNEL and interface.vlan is not None:
            recorder.skip(
                subject,
                Reason.NOT_REPRESENTABLE,
                f"the overlay carries VLAN {interface.vlan.pvid}; networkd builds the tunnel "
                f"netdev but has no field saying which VLAN rides inside it",
            )
        wireless = interface.wireless
        if wireless is not None:
            ssids = ", ".join(repr(bss.ssid) for bss in wireless.bss)
            recorder.skip(
                subject,
                Reason.NOT_REPRESENTABLE,
                f"'wireless' describes a {wireless.role.value} radio"
                + (f" carrying {ssids}" if ssids else "")
                + "; networkd configures the addresses on a radio and nothing about the "
                "association, which wpa_supplicant or iwd owns. 'netviz export netplan' "
                "writes the association",
            )
    for route in plan.routes:
        if route.blackhole:
            if not configured:
                recorder.skip(
                    plan.fqn,
                    Reason.UNRESOLVED,
                    f"route {route.prefix} discards what it matches, but this device got no "
                    f"'.network' file for the section to be stated in",
                )
            continue
        name = route_interface(plan, route)
        if name is None:
            recorder.skip(
                plan.fqn,
                Reason.UNRESOLVED,
                f"route {route.prefix} states no 'dev', and no declared prefix covers its "
                f"next hop; a networkd '[Route]' belongs to one interface, and guessing "
                f"which would put the route on a port that cannot reach it",
            )
        elif name not in configured:
            recorder.skip(
                plan.fqn,
                Reason.UNRESOLVED,
                f"route {route.prefix} leaves by {name!r}, which got no '.network' file, so "
                f"there is nothing for its '[Route]' section to attach to",
            )


# --------------------------------------------------------------------------- #
# The .network file
# --------------------------------------------------------------------------- #


def _network_file(
    plan: DevicePlan, interface: Interface, stem: str, *, unbound: bool
) -> ConfigFile:
    """One link's configuration: what to match, and what to do with it.

    Args:
        unbound: Does this file also carry the routes that belong to no
            interface? True for the first one written; see :func:`_routes`.
    """
    sections: list[Sequence[str]] = [
        ["[Match]", f"Name={interface.name}"],
        _link(interface),
        _network(plan, interface),
        _bridge_vlan(interface),
    ]
    sections.extend(_routes(plan, interface, unbound=unbound))
    if unbound:
        sections.extend(_policy_rules(plan))
    return ConfigFile(path=f"{DIRECTORY}/{stem}.network", content=_render(plan, sections))


def _link(interface: Interface) -> list[str]:
    """``[Link]`` — the properties of the device rather than of its addressing.

    The MTU and the MAC address are written here only for an interface networkd
    does not create. For one it does, both belong in the ``[NetDev]`` that
    creates it: they are then applied when the device appears rather than after,
    and there is one place to change them.
    """
    lines = ["[Link]"]
    if not is_stacked(interface):
        if interface.mtu is not None:
            lines.append(f"MTUBytes={interface.mtu}")
        if interface.mac:
            lines.append(f"MACAddress={interface.mac}")
    if not interface.enabled:
        # "Configure it, do not bring it up" — the inventory's ``enabled: false``
        # is exactly that statement, and networkd's is ActivationPolicy=down.
        lines.append("ActivationPolicy=down")
    return lines if len(lines) > 1 else []


def _network(plan: DevicePlan, interface: Interface) -> list[str]:
    """``[Network]`` — addresses, forwarding, and what this link is bound into."""
    lines = ["[Network]"]
    lines.extend(f"Address={cidr}" for cidr in addresses_of(interface))
    # The systemd 256 spelling. Before it, both families shared one 'IPForward='
    # key; the header of this file says so, because a host running 255 silently
    # ignores an unknown key rather than refusing the file.
    if plan.forwards_ipv4:
        lines.append("IPv4Forwarding=yes")
    if plan.forwards_ipv6:
        lines.append("IPv6Forwarding=yes")
    aggregate = plan.enslaved_by(interface.name)
    if aggregate is not None:
        # Enslavement is stated by the *member*, not by the aggregate: an
        # inventory writes ``members`` on the bridge, networkd writes 'Bridge='
        # on each port of it.
        key = "Bridge" if aggregate.type is InterfaceType.BRIDGE else "Bond"
        lines.append(f"{key}={aggregate.name}")
    for child in plan.stacked_on(interface.name):
        # 'VLAN=' here names a netdev stacked on this link, which is a different
        # statement from the VLAN *ids* of '[BridgeVLAN] VLAN='.
        if child.type is InterfaceType.VLAN:
            lines.append(f"VLAN={child.name}")
        elif _rides_on_underlay(plan, child):
            lines.append(f"Tunnel={child.name}")
    return lines if len(lines) > 1 else []


def _rides_on_underlay(plan: DevicePlan, interface: Interface) -> bool:
    """Must this tunnel netdev be claimed by the port its outer packets leave by?

    GRE, VXLAN and Geneve encapsulate on a link, and networkd attaches them only
    where a ``.network`` names them with ``Tunnel=``. WireGuard is a routed netdev
    with no lower link — it picks its source address from the routing table — so
    naming it there would bind it to something it does not sit on.
    """
    if interface.type is not InterfaceType.TUNNEL:
        return False
    tunnel = plan.tunnel_on(interface.name)
    if tunnel is None or tunnel.type not in _TUNNEL_KINDS:
        return False
    return tunnel.type is not TunnelType.WIREGUARD


def _bridge_vlan(interface: Interface) -> list[str]:
    """``[BridgeVLAN]`` — the 802.1Q configuration of a bridge port.

    One ``VLAN=`` line per id rather than a range: a range is legal syntax, but a
    port's membership is what an operator reads this file to check, and a list
    they can grep for a single number beats four characters saved.

    ``PVID=``/``EgressUntagged=`` are written only where the inventory states the
    untagged VLAN — an access port's, or a trunk's native VLAN. A trunk with no
    native VLAN has no untagged domain to declare, and VLAN 1 is netviz's
    fallback rather than the document's statement.

    Written only for a port whose ``vlan`` block is a filter
    (:func:`~netviz.export.config.plan.restricts_vlans`). The same block on a
    tunnel interface says which VLAN the overlay carries, which is not something
    the local bridge enforces, and a ``[BridgeVLAN]`` there would be a section
    about a bridge the interface is not a port of.
    """
    vlan = interface.vlan
    if vlan is None or not restricts_vlans(interface):
        return []
    lines = ["[BridgeVLAN]"]
    lines.extend(f"VLAN={vid}" for vid in sorted(vlan.vlan_ids()))
    if vlan.mode is VlanMode.ACCESS or vlan.native_vlan is not None:
        lines.append(f"PVID={vlan.pvid}")
        lines.append(f"EgressUntagged={vlan.pvid}")
    return lines


def _routes(plan: DevicePlan, interface: Interface, *, unbound: bool) -> Iterator[list[str]]:
    """One ``[Route]`` per gateway and per static route leaving this interface.

    A blackhole route is the one that belongs to no interface: it has no next hop
    and no egress by construction (``NG-F004``), and networkd installs it without
    a device. Every ``[Route]`` still has to be *stated* in some ``.network``, so
    it goes in the first one this device gets — an arbitrary choice, but not a
    guess, because the file it sits in does not change what the route does.
    """
    for config in (interface.ipv4, interface.ipv6):
        if config is not None and config.gateway is not None:
            # A '[Route]' with a gateway and no destination is the default route
            # of that gateway's family, which is what 'gateway' states.
            yield ["[Route]", f"Gateway={config.gateway}"]
    for route in plan.routes:
        if route.blackhole:
            if unbound:
                yield _route(plan, route)
        elif route_interface(plan, route) == interface.name:
            yield _route(plan, route)


def _route(plan: DevicePlan, route: StaticRoute) -> list[str]:
    lines = ["[Route]", f"Destination={route.prefix}"]
    if route.blackhole:
        lines.append("Type=blackhole")
    elif route.via is not None:
        lines.append(f"Gateway={route.via}")
    if route.metric is not None:
        lines.append(f"Metric={route.metric}")
    if route.table is not None:
        lines.append(f"Table={_table(plan, route.table)}")
    return lines


def _table(plan: DevicePlan, name: str) -> str:
    """How ``Table=`` names a routing table: its number, or failing that its name.

    ``Table=`` takes a name or a number, and networkd resolves a name against its
    own ``RouteTable=`` definitions rather than against
    ``/etc/iproute2/rt_tables`` — so the number is what both ends agree on, and
    the number is written wherever the inventory knows one.

    It does not know one for a VRF (§16.2), and the name is written then. That
    line is one networkd will refuse unless something defined the name, which is
    the point: a *loud* failure. Leaving the key out instead would install the
    route in ``main``, which is a different route from the one the inventory
    states and no message anywhere would say so. In practice neither happens —
    :func:`limits` refuses a device declaring a VRF at all — but the fallback
    has to be the safe one rather than rely on that staying true.
    """
    number = plan.device.spec.table_id(name)
    return name if number is None else str(number)


def _policy_rules(plan: DevicePlan) -> Iterator[list[str]]:
    """``[RoutingPolicyRule]`` per rule of the policy database (§16.4).

    The one section here that is not about the link the file matches: a policy
    rule is a property of the *stack*, and networkd installs one from whichever
    ``.network`` states it. So they go in the first file this device gets, for
    the same reason a blackhole route does — an arbitrary file, but not a guess,
    because which one it sits in does not change what it does.

    A rule installed in both families is written twice, once per ``Family=``,
    since the key takes one. ``ipv4-and-ipv6`` exists but only for a rule with no
    family-specific selector, and deciding which rules qualify is a judgement the
    two explicit sections do not need.
    """
    for rule in plan.device.spec.routing_policy:
        for family in rule.families:
            lines = ["[RoutingPolicyRule]", f"Priority={rule.priority}", f"Family={family.value}"]
            lines.extend(_rule_selectors(plan, rule))
            lines.extend(_rule_action(plan, rule))
            yield lines


def _rule_selectors(plan: DevicePlan, rule: PolicyRule) -> Iterator[str]:
    """Which packets the rule matches, in networkd's spelling of each selector.

    ``invert`` has no key: networkd inverts one selector at a time
    (``InvertRule=`` does not exist; ``IPProtocol=`` and friends have no negation)
    and the inventory inverts the whole set. It is recorded as a skip rather than
    dropped silently, in :func:`_record_out_of_remit`.
    """
    if rule.src is not None:
        yield f"From={rule.src}"
    if rule.dst is not None:
        yield f"To={rule.dst}"
    if rule.iif is not None:
        yield f"IncomingInterface={rule.iif}"
    if rule.oif is not None:
        yield f"OutgoingInterface={rule.oif}"
    if rule.fwmark is not None:
        yield f"FirewallMark={rule.fwmark}"
    if rule.dscp is not None:
        # ``TypeOfService=`` is the whole octet; a DSCP is its top six bits.
        yield f"TypeOfService={rule.dscp << 2}"


def _rule_action(plan: DevicePlan, rule: PolicyRule) -> Iterator[str]:
    """What the rule does: a table to route by, or the way it refuses to."""
    if rule.action is PolicyAction.LOOKUP:
        assert rule.table is not None  # NG-F016: a lookup always names one
        yield f"Table={_table(plan, rule.table)}"
        return
    if rule.action is PolicyAction.GOTO:
        yield f"GoTo={rule.goto}"
        return
    yield f"Type={rule.action.value}"


# --------------------------------------------------------------------------- #
# The .netdev file
# --------------------------------------------------------------------------- #


def _netdev_file(
    plan: DevicePlan,
    interface: Interface,
    tunnel: TunnelPlan | None,
    stem: str,
    recorder: Recorder,
) -> ConfigFile:
    """The device networkd has to create before the ``.network`` can match it."""
    sections: list[Sequence[str]] = [_netdev(interface, tunnel)]
    sections.extend(_netdev_detail(plan, interface, tunnel, recorder))
    return ConfigFile(path=f"{DIRECTORY}/{stem}.netdev", content=_render(plan, sections))


def _netdev(interface: Interface, tunnel: TunnelPlan | None) -> list[str]:
    kind = _TUNNEL_KINDS[tunnel.type] if tunnel is not None else _NETDEV_KINDS[interface.type]
    lines = ["[NetDev]", f"Name={interface.name}", f"Kind={kind}"]
    mtu = _mtu(interface, tunnel)
    if mtu is not None:
        lines.append(f"MTUBytes={mtu}")
    if interface.mac:
        lines.append(f"MACAddress={interface.mac}")
    return lines


def _mtu(interface: Interface, tunnel: TunnelPlan | None) -> int | None:
    """The interface's own MTU, or the tunnel document's where it has none.

    A tunnel's MTU is a property of the encapsulation and is written on the
    tunnel (§14); the interface may still override it, and does so where it is
    stated, because that is the more specific of the two.
    """
    if interface.mtu is not None:
        return interface.mtu
    return tunnel.tunnel.spec.mtu if tunnel is not None else None


def _netdev_detail(
    plan: DevicePlan, interface: Interface, tunnel: TunnelPlan | None, recorder: Recorder
) -> Iterator[list[str]]:
    """The kind-specific sections that follow ``[NetDev]``."""
    if tunnel is not None:
        yield from _tunnel_detail(plan, interface, tunnel, recorder)
    elif interface.type is InterfaceType.BRIDGE:
        yield from _bridge_detail(plan, interface)
    elif interface.type is InterfaceType.LAG:
        recorder.skip(
            f"{plan.fqn}:{interface.name}",
            Reason.NOT_REPRESENTABLE,
            "'[Bond] Mode=' has to be set by hand: the inventory does not record whether "
            "this LAG is LACP or a static mode, and the wrong one against a switch "
            "expecting the other is a link that comes up and drops frames",
        )
    elif interface.vlan is not None:
        yield ["[VLAN]", f"Id={interface.vlan.pvid}"]


def _bridge_detail(plan: DevicePlan, interface: Interface) -> Iterator[list[str]]:
    """``[Bridge] VLANFiltering=`` — derived from the ports, not from the bridge.

    A ``[BridgeVLAN]`` section is inert on a bridge that does not filter, so a
    bridge whose members declare a ``vlan`` block would otherwise get the VLAN
    configuration written and none of it applied. The members saying they are
    access or trunk ports *is* the statement that the bridge is VLAN-aware; there
    is nothing else it could mean.
    """
    for name in interface.members or ():
        member = plan.interface(name)
        if member is not None and member.vlan is not None:
            yield ["[Bridge]", "VLANFiltering=yes"]
            return


def _tunnel_detail(
    plan: DevicePlan, interface: Interface, tunnel: TunnelPlan, recorder: Recorder
) -> Iterator[list[str]]:
    if tunnel.type is TunnelType.WIREGUARD:
        yield _wireguard(plan, tunnel)
        yield from _wireguard_peers(plan, tunnel, recorder)
        return
    if tunnel.type is TunnelType.GRE:
        section = ["[Tunnel]"]
        section.extend(_endpoints(plan, interface, tunnel, recorder, local=True))
        yield section
        return
    yield _overlay(plan, interface, tunnel, recorder)


def _overlay(
    plan: DevicePlan, interface: Interface, tunnel: TunnelPlan, recorder: Recorder
) -> list[str]:
    """``[VXLAN]``/``[GENEVE]`` — an identified layer-2 overlay over UDP."""
    name, key = _OVERLAY_SECTIONS[tunnel.type]
    section = [f"[{name}]"]
    if tunnel.tunnel.spec.vni is not None:
        section.append(f"{key}={tunnel.tunnel.spec.vni}")
    local = tunnel.type is TunnelType.VXLAN
    section.extend(_endpoints(plan, interface, tunnel, recorder, local=local))
    if tunnel.port is not None:
        section.append(f"DestinationPort={tunnel.port}")
    return section


def _endpoints(
    plan: DevicePlan,
    interface: Interface,
    tunnel: TunnelPlan,
    recorder: Recorder,
    *,
    local: bool,
) -> Iterator[str]:
    """``Local=``/``Remote=`` for a point-to-point encapsulation.

    Both are underlay addresses and both come from what the inventory states:
    ``Local`` from the port this end names in ``parent``, ``Remote`` from the far
    end's. Either being absent is recorded rather than filled in — networkd
    refuses the netdev, which is the right outcome for a tunnel whose endpoints
    nobody has written down.
    """
    subject = f"{plan.fqn}:{interface.name}"
    if local:
        address = _underlay_address(plan, interface)
        if address:
            yield f"Local={address}"
        else:
            recorder.skip(
                subject,
                Reason.NO_ADDRESS,
                "no 'Local=' for this netdev: the interface names no underlay port "
                "('parent'), or that port declares no routable address",
            )
    if tunnel.is_multipoint:
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            f"{tunnel.name} has {len(tunnel.peers) + 1} ends; this netdev kind takes one "
            f"'Remote=', so the rest of the mesh is not in this file",
        )
    remotes = [peer for peer in tunnel.peers if peer.endpoint]
    if remotes:
        yield f"Remote={remotes[0].endpoint}"
    else:
        recorder.skip(
            subject,
            Reason.NO_ADDRESS,
            f"no 'Remote=' for this netdev: no peer of {tunnel.name} declares an underlay "
            f"address to reach it at",
        )


def _wireguard(plan: DevicePlan, tunnel: TunnelPlan) -> list[str]:
    section = ["[WireGuard]"]
    if tunnel.port is not None:
        section.append(f"ListenPort={tunnel.port}")
    # On its own line: systemd reads everything after '=' as the value, so a
    # trailing comment would become part of the key.
    section.append(f"# The private key of {plan.name}. netviz stores no key material.")
    section.append(f"PrivateKey={_PLACEHOLDER}")
    return section


def _wireguard_peers(
    plan: DevicePlan, tunnel: TunnelPlan, recorder: Recorder
) -> Iterator[list[str]]:
    for peer in tunnel.peers:
        section = [
            "[WireGuardPeer]",
            f"# {peer.element}:{peer.interface}",
            f"PublicKey={_PLACEHOLDER}",
        ]
        if peer.overlay:
            section.append(f"AllowedIPs={','.join(_host_route(cidr) for cidr in peer.overlay)}")
        else:
            recorder.skip(
                f"{plan.fqn}:{tunnel.interface.name}",
                Reason.NO_ADDRESS,
                f"peer {peer.element}:{peer.interface} declares no address inside the tunnel, "
                f"so there is nothing to put in 'AllowedIPs'",
            )
        if peer.endpoint:
            section.append(f"Endpoint={_socket(peer.endpoint, tunnel.port or 0)}")
        elif peer.endpoint_note:
            section.append(f"# no endpoint: {peer.endpoint_note}")
        yield section


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def _render(plan: DevicePlan, sections: Iterable[Sequence[str]]) -> str:
    """The banner and every non-empty section, one blank line apart."""
    chunks = ["\n".join(config_header("#", "networkd", plan, notes=_notes(plan)))]
    chunks.extend("\n".join(section) for section in sections if section)
    return "\n\n".join(chunks) + "\n"


def _notes(plan: DevicePlan) -> tuple[str, ...]:
    notes = [
        "Copy the whole directory, then 'networkctl reload': these files replace the",
        "configuration of every link they match, and nothing here was read off the",
        "running system.",
    ]
    if plan.forwards_ipv4 or plan.forwards_ipv6:
        notes.append("This device forwards. IPv4Forwarding=/IPv6Forwarding= need systemd 256 or")
        notes.append("newer; before that both families shared the single 'IPForward=' key.")
    return tuple(notes)


def _file_stem(plan: DevicePlan, interface: Interface, recorder: Recorder) -> str:
    """``10-eno1`` — the ordering prefix and the name the two files share.

    An interface name may hold a ``/`` — §4.1 allows it, and ``xe-0/0/0`` is one
    name rather than three — while a path segment may not: written literally, the
    file would land in a subdirectory networkd never reads. The slash is folded
    to a hyphen in the *file name* only. ``[Match] Name=`` keeps the inventory's
    spelling, because that is the string the kernel compares against, and the
    fold is recorded: a reader grepping a directory listing for the interface's
    own name would otherwise not find its file.
    """
    folded = interface.name.replace("/", "-")
    recorder.rewrite(
        f"{plan.fqn}:{interface.name}",
        field="filename",
        original=interface.name,
        rewritten=folded,
    )
    prefix = _STACKED_PREFIX if is_stacked(interface) else _PORT_PREFIX
    return f"{prefix}-{folded}"


def _underlay_address(plan: DevicePlan, interface: Interface) -> str:
    """The address of the port this tunnel's outer packets leave by."""
    if interface.parent is None:
        return ""
    underlay = plan.interface(interface.parent)
    if underlay is None:
        return ""
    routable = addresses_of(underlay, routable_only=True)
    return routable[0].partition("/")[0] if routable else ""


def _socket(address: str, port: int) -> str:
    """``host:port``, with an IPv6 literal bracketed (RFC 3986 §3.2.2)."""
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{port}"


def _host_route(cidr: str) -> str:
    """``10.9.0.1/24`` → ``10.9.0.1/32``.

    ``AllowedIPs`` is a routing decision, and the only one the inventory supports
    is "the peer itself". Widening it to the peer's whole prefix would send this
    end's traffic for every address in that subnet down the tunnel, which is a
    policy nobody wrote down.
    """
    address, _, _ = cidr.partition("/")
    return f"{address}/{128 if ':' in address else 32}"
