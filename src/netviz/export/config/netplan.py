"""``netplan`` — the renderer Ubuntu and Debian hosts read at boot.

netplan is the closest thing to netviz's own model that a Linux distribution
ships: a declarative YAML document describing intent, rendered by something else
into whatever the box actually runs. The mapping is therefore mostly structural —
``ethernets``/``wifis``/``bonds``/``bridges``/``vlans``/``tunnels``, one section
per interface type — and the interesting part is the four places where it is not.

**A VLAN sub-interface, not a VLAN-aware bridge.** netplan's ``vlans`` section
creates ``eno1.10``: an interface stacked on a parent, which is what a *host*
does with a VLAN. It has no syntax at all for the other thing an inventory calls
``vlan`` — the 802.1Q configuration of a bridge port, ``mode: trunk`` with a
tagged set. Writing the file without it would produce a host that accepts every
VLAN on a port the inventory says carries three, so a trunk or access port on a
device netplan was asked to write is a refusal
(:data:`~netviz.export.config.model.Unsupported`), not a silent omission.

**A VRF needs a table number netviz does not have.** netplan's ``vrfs`` section
requires ``table:``, a numeric routing table id. An inventory states a route
distinguisher (§16.1), which identifies the VRF in BGP and is *not* a table
number; deriving one from it would be inventing the single value that decides
which routes the VRF sees. Refused, with the field named.

**A gateway is a route.** ``gateway4``/``gateway6`` have been deprecated since
netplan 0.103 and are ignored by current releases, so ``ipv4.gateway`` becomes
``routes: [{to: default, via: …}]`` on the interface that declares it. That is
the same statement, in the spelling netplan still honours.

**Keys are placeholders.** A WireGuard private key and a wifi passphrase are the
two things netplan needs that an inventory deliberately does not hold (§14.2).
They are written as an obvious, un-runnable placeholder rather than left out:
omitting them gives a file netplan rejects with a message about the *schema*,
which sends the reader looking in the wrong place.

Everything netplan has no business with — PoE, a routing protocol, a rack
position — is recorded in the export manifest naming the dialect that does have
business with it, and is not a refusal: the netplan file is still correct for
what netplan covers.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Final

from netviz.errors import count_text
from netviz.export.config.header import config_header
from netviz.export.config.model import ConfigFile, Unsupported
from netviz.export.config.plan import (
    DevicePlan,
    TunnelPlan,
    addresses_of,
    netns_limits,
    restricts_vlans,
    route_interface,
    unwritable_vlan,
)
from netviz.export.manifest import Reason, Recorder
from netviz.models import Interface, InterfaceType, StaticRoute
from netviz.models.interface import RadioRole, Security
from netviz.models.tunnel import TunnelType

__all__ = ["FILENAME", "HOST_KINDS", "declines", "files", "limits", "selects"]

#: Where netplan looks. The numeric prefix is netplan's own convention for
#: ordering fragments, and 10 leaves room below for a distribution's own file
#: and above for a local override — a generated file should be neither the
#: first nor the last word.
FILENAME = "etc/netplan/10-netviz.yaml"

#: The kinds this dialect claims. A Linux host renders netplan; a Catalyst does
#: not, and generating a netplan file for one would be a category error rather
#: than a lossy export. A ``router`` is here because a Linux box doing the
#: routing is the ordinary home and edge case, and a ``firewall`` for the same
#: reason -- most of them are a Linux box too. Either that is not one is refused
#: the moment it declares something netplan cannot write; ``spec.firewall``
#: itself is not that, since netplan has no filter table and never claimed one
#: (``netviz export nftables`` writes it).
HOST_KINDS: Final[frozenset[str]] = frozenset({"computer", "server", "router", "firewall"})

#: netplan's ``tunnels[].mode`` for the tunnel types that are a netdev on Linux.
#: The four that are missing — IPsec, OpenVPN, PPTP and L2TP — are daemons, not
#: netdevs: strongSwan, openvpn and pppd own them, and netplan has no section
#: for any of the three.
_TUNNEL_MODES: Final[dict[TunnelType, str]] = {
    TunnelType.WIREGUARD: "wireguard",
    TunnelType.GRE: "gre",
    TunnelType.VXLAN: "vxlan",
}

#: netplan's ``auth.key-management`` for each security mode netviz records.
_KEY_MANAGEMENT: Final[dict[Security, str]] = {
    Security.OPEN: "none",
    Security.WPA2_PSK: "psk",
    Security.WPA2_EAP: "eap",
    Security.WPA3_PSK: "sae",
    Security.WPA3_EAP: "eap",
}

#: What netplan calls the section an interface of each type belongs in.
_SECTIONS: Final[dict[InterfaceType, str]] = {
    InterfaceType.ETHERNET: "ethernets",
    InterfaceType.WIFI: "wifis",
    InterfaceType.LAG: "bonds",
    InterfaceType.BRIDGE: "bridges",
    InterfaceType.VLAN: "vlans",
    InterfaceType.TUNNEL: "tunnels",
}

#: Written where netplan needs a secret. Deliberately not a valid key of any
#: length: a placeholder that parses is a placeholder that reaches production.
_PLACEHOLDER = "REPLACE-ME"


def selects(plan: DevicePlan) -> bool:
    """Is this a device that renders netplan?"""
    return plan.kind in HOST_KINDS


def declines(plan: DevicePlan) -> str:
    """Why a device this dialect did not select got no file."""
    return (
        f"netplan is rendered by a Linux host; a {plan.kind} is configured through its own "
        f"CLI, so 'netviz export interfaces' is what describes this device"
    )


def limits(plan: DevicePlan) -> tuple[Unsupported, ...]:
    """Everything netplan would have had to invent or drop for ``plan``."""
    return tuple(_limits(plan))


def _limits(plan: DevicePlan) -> Iterator[Unsupported]:
    yield from netns_limits(plan, "netplan")
    for index, vrf in enumerate(plan.device.spec.vrfs):
        yield Unsupported(
            element=plan.fqn,
            field=plan.field("vrfs", index),
            detail=(
                f"netplan's 'vrfs' section needs a numeric routing table for {vrf.name!r}; "
                f"the inventory states the route distinguisher {vrf.rd}, which identifies the "
                f"VRF in BGP and is not a table number. Deriving one would decide which "
                f"routes the VRF sees"
            ),
        )
    for interface in plan.interfaces:
        yield from _interface_limits(plan, interface)


def _interface_limits(plan: DevicePlan, interface: Interface) -> Iterator[Unsupported]:
    unwritable = unwritable_vlan(plan, interface)
    if unwritable:
        yield Unsupported(
            element=plan.fqn,
            field=plan.interface_field(interface, "vlan"),
            detail=(
                f"{unwritable}. netplan creates VLAN sub-interfaces but has no syntax for the "
                f"802.1Q configuration of a bridge port"
            ),
        )
    wireless = interface.wireless
    if wireless is None:
        return
    if wireless.role is RadioRole.MESH:
        yield Unsupported(
            element=plan.fqn,
            field=plan.interface_field(interface, "wireless", "role"),
            detail=(
                "netplan's access-point modes are infrastructure, ap and adhoc; it has no "
                "spelling for an 802.11s mesh, and 'adhoc' is a different protocol"
            ),
        )
    for position, bss in enumerate(wireless.bss):
        if bss.vlan is not None:
            yield Unsupported(
                element=plan.fqn,
                field=plan.interface_field(interface, "wireless", "bss", position, "vlan"),
                detail=(
                    f"SSID {bss.ssid!r} is bridged into VLAN {bss.vlan}; netplan configures a "
                    f"radio but cannot map one of its SSIDs into a VLAN, which hostapd's "
                    f"'vlan_file' does"
                ),
            )


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #


def files(plan: DevicePlan, recorder: Recorder) -> tuple[ConfigFile, ...]:
    """The one YAML document netplan reads for this device.

    Two passes over the interfaces, because the routes cannot be placed until it
    is known which interfaces reached the document: a route whose egress netplan
    left out has nowhere to hang, and a blackhole route has no egress at all.
    """
    bodies: list[tuple[Interface, list[str]]] = []
    for interface in plan.interfaces:
        if interface.type is InterfaceType.LOOPBACK:
            # ``lo`` is the kernel's, not netplan's: netplan has no section for a
            # loopback and rendering one would fight whatever created it.
            recorder.skip(
                f"{plan.fqn}:{interface.name}",
                Reason.NOT_REPRESENTABLE,
                "netplan has no section for a loopback interface; the kernel owns 'lo' and a "
                "declared router loopback belongs to the routing daemon",
            )
            continue
        body = _interface(plan, interface, recorder)
        if body is not None:
            bodies.append((interface, body))

    routes = _route_map(plan, recorder, [interface for interface, _ in bodies])
    _record_policy(plan, recorder)
    sections: dict[str, list[str]] = {name: [] for name in _SECTIONS.values()}
    for interface, body in bodies:
        block = [*body, *_render_routes(routes.get(interface.name, ()))]
        head = f"    {interface.name}:" if block else f"    {interface.name}: {{}}"
        sections[_SECTIONS[interface.type]].extend([head, *block])

    _record_out_of_remit(plan, recorder)

    lines = [
        *config_header(
            "#",
            "netplan",
            plan,
            notes=_notes(plan),
        ),
        "network:",
        "  version: 2",
    ]
    for name in _SECTIONS.values():
        if sections[name]:
            lines.extend([f"  {name}:", *sections[name]])

    recorder.emitted += 1
    return (ConfigFile(path=FILENAME, content="".join(f"{line}\n" for line in lines)),)


def _notes(plan: DevicePlan) -> tuple[str, ...]:
    notes = [
        "Apply with 'netplan try' first: this file replaces the interface configuration",
        "of the host, and nothing here was read off the running system.",
    ]
    if plan.forwards_ipv4 or plan.forwards_ipv6:
        notes.append("This device forwards; netplan has no switch for that, so the sysctl is not")
        notes.append("in this file. See 'netviz export interfaces' for the declared state.")
    return tuple(notes)


def _record_out_of_remit(plan: DevicePlan, recorder: Recorder) -> None:
    """Everything netplan is simply not the tool for, named with the tool that is."""
    if plan.device.spec.routing is not None:
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            "'spec.routing' is a routing protocol; netplan configures interfaces. Generate "
            "it with 'netviz export frr'",
        )
    for interface in plan.interfaces:
        if interface.poe is not None:
            recorder.skip(
                f"{plan.fqn}:{interface.name}",
                Reason.NOT_REPRESENTABLE,
                "'poe' is switch hardware configuration; netplan does not touch the PSE",
            )
        if restricts_vlans(interface) and interface.vlan is not None:
            # The refusal above already fired for every port whose VLAN block
            # changes behaviour; what is left is the ordinary access port, whose
            # untagged domain a plain interface carries with no line at all.
            recorder.skip(
                f"{plan.fqn}:{interface.name}",
                Reason.NOT_REPRESENTABLE,
                f"the port is untagged in VLAN {interface.vlan.pvid}; a plain interface "
                f"carries untagged frames, so netplan writes no line for it",
            )
        if interface.type is InterfaceType.TUNNEL and interface.vlan is not None:
            recorder.skip(
                f"{plan.fqn}:{interface.name}",
                Reason.NOT_REPRESENTABLE,
                f"the overlay carries VLAN {interface.vlan.pvid}; netplan builds the tunnel "
                f"netdev but has no field saying which VLAN rides inside it. A description "
                f"is lost, not a filter — see 'netviz export interfaces'",
            )


def _interface(plan: DevicePlan, interface: Interface, recorder: Recorder) -> list[str] | None:
    """The body of one interface's mapping, at six spaces of indent.

    Three answers, not two, because netplan distinguishes three cases and
    conflating any pair of them produces a file it refuses:

    ``None``
        This interface cannot go in the document at all. A tunnel netplan has no
        ``mode`` for, or a radio the inventory names no SSID on: netplan requires
        a key in both cases and netviz has nothing to put in it.
    ``[]``
        The interface exists and there is nothing to configure on it — a bare
        bond slave, a spare port. It is still declared, as ``eno2: {}``, because
        a bond naming a member netplan has never heard of is a file netplan
        rejects, and a port left out entirely reads as a deletion to the drift
        check.
    a non-empty list
        The ordinary case.
    """
    body: list[str] = []
    if interface.type is InterfaceType.TUNNEL:
        tunnel = plan.tunnel_on(interface.name)
        if tunnel is None or tunnel.type not in _TUNNEL_MODES:
            _record_unmapped_tunnel(plan, interface, tunnel, recorder)
            return None
        body.extend(_tunnel(plan, interface, tunnel, recorder))
    if interface.type is InterfaceType.WIFI:
        access_points = list(_wireless(plan, interface, recorder))
        if not access_points:
            return None
        body.extend(access_points)

    body.extend(_stacking(plan, interface))
    body.extend(_addresses(interface))
    body.extend(_link(plan, interface, recorder))
    return body


def _record_unmapped_tunnel(
    plan: DevicePlan, interface: Interface, tunnel: TunnelPlan | None, recorder: Recorder
) -> None:
    subject = f"{plan.fqn}:{interface.name}"
    if tunnel is None:
        recorder.skip(
            subject,
            Reason.UNRESOLVED,
            "a 'tunnel' interface that no tunnel document terminates on; netplan has "
            "nothing to build from it",
        )
        return
    recorder.skip(
        subject,
        Reason.NOT_REPRESENTABLE,
        f"{tunnel.type} is a daemon rather than a netdev, and netplan has no section for "
        f"one; strongSwan, openvpn and pppd own these",
    )


def _tunnel(
    plan: DevicePlan, interface: Interface, tunnel: TunnelPlan, recorder: Recorder
) -> Iterator[str]:
    yield f"      mode: {_TUNNEL_MODES[tunnel.type]}"
    if tunnel.port is not None:
        yield f"      port: {tunnel.port}"
    if tunnel.type is TunnelType.WIREGUARD:
        yield from _wireguard(plan, tunnel, recorder)
        return
    if tunnel.tunnel.spec.vni is not None:
        yield f"      id: {tunnel.tunnel.spec.vni}"
    yield from _tunnel_endpoints(plan, interface, tunnel, recorder)


def _tunnel_endpoints(
    plan: DevicePlan, interface: Interface, tunnel: TunnelPlan, recorder: Recorder
) -> Iterator[str]:
    """``local``/``remote`` for a point-to-point netdev tunnel.

    Both are underlay addresses, and both come from what the inventory states:
    ``local`` from the address of the underlay port this end names in ``parent``,
    ``remote`` from the far end's. Either being absent is reported rather than
    filled in — netplan will reject the file, which is the correct outcome for a
    tunnel whose endpoints nobody has written down.
    """
    local = _underlay_address(plan, interface)
    if local:
        yield f"      local: {local}"
    else:
        recorder.skip(
            f"{plan.fqn}:{interface.name}",
            Reason.NO_ADDRESS,
            "netplan needs 'local' for this tunnel mode; the interface names no underlay "
            "port ('parent'), or that port declares no routable address",
        )
    remotes = [peer for peer in tunnel.peers if peer.endpoint]
    if tunnel.is_multipoint:
        recorder.skip(
            f"{plan.fqn}:{interface.name}",
            Reason.NOT_REPRESENTABLE,
            f"{tunnel.name} has {len(tunnel.peers) + 1} ends; a netplan tunnel is "
            "point-to-point, so only a single 'remote' can be written",
        )
    if remotes:
        yield f"      remote: {remotes[0].endpoint}"
    else:
        recorder.skip(
            f"{plan.fqn}:{interface.name}",
            Reason.NO_ADDRESS,
            "netplan needs 'remote' for this tunnel mode; no peer of "
            f"{tunnel.name} declares an underlay address to reach it at",
        )


def _wireguard(plan: DevicePlan, tunnel: TunnelPlan, recorder: Recorder) -> Iterator[str]:
    # No ``mtu:`` here: :func:`_link` writes exactly one for the interface,
    # resolving the tunnel document's and the interface's own. Writing it in both
    # places gave a mapping with two ``mtu`` keys, which YAML 1.2 forbids, which
    # yamllint rejects, and which netplan resolved by silently taking the last.
    yield f'      key: "{_PLACEHOLDER}"  # private key of {plan.name}; netviz holds no keys'
    yield "      peers:"
    for peer in tunnel.peers:
        yield f"        # {peer.element}:{peer.interface}"
        yield f'        - keys: {{public: "{_PLACEHOLDER}"}}'
        if peer.endpoint:
            port = tunnel.port or 0
            yield f'          endpoint: "{_socket(peer.endpoint, port)}"'
        elif peer.endpoint_note:
            yield f"          # no endpoint: {peer.endpoint_note}"
        if peer.overlay:
            yield f"          allowed-ips: [{', '.join(_host_route(cidr) for cidr in peer.overlay)}]"
        else:
            recorder.skip(
                f"{plan.fqn}:{tunnel.interface.name}",
                Reason.NO_ADDRESS,
                f"peer {peer.element}:{peer.interface} declares no address inside the "
                f"tunnel, so there is nothing to put in 'allowed-ips'",
            )


def _stacking(plan: DevicePlan, interface: Interface) -> Iterator[str]:
    if interface.type is InterfaceType.VLAN and interface.vlan is not None:
        yield f"      id: {interface.vlan.pvid}"
        yield f"      link: {interface.parent}"
    if interface.members:
        yield f"      interfaces: [{', '.join(interface.members)}]"


def _addresses(interface: Interface) -> Iterator[str]:
    addresses = addresses_of(interface)
    if not addresses:
        return
    yield "      addresses:"
    for cidr in addresses:
        yield f"        - {cidr}"


def _link(plan: DevicePlan, interface: Interface, recorder: Recorder) -> Iterator[str]:
    if interface.mac:
        yield f'      macaddress: "{interface.mac}"'
    mtu = _mtu(plan, interface, recorder)
    if mtu is not None:
        yield f"      mtu: {mtu}"
    if not interface.enabled:
        # netplan 0.105's spelling for "define it, do not bring it up". The
        # inventory's ``enabled: false`` is exactly that statement.
        yield "      activation-mode: off"


def _mtu(plan: DevicePlan, interface: Interface, recorder: Recorder) -> int | None:
    """The one MTU of an interface, from the two places that may state it.

    A tunnel states its MTU on the tunnel document (``spec.mtu``, §14) and the
    interface may state one too. The interface wins where it does, because it is
    the more specific of the two — the same rule the ``networkd`` dialect
    applies, and the two must agree or a host configured from one file would run
    a different path MTU from the same host configured from the other.

    A disagreement is recorded rather than resolved in silence: the two numbers
    were both written down by somebody, and which was meant is not netviz's to
    decide.
    """
    tunnel = plan.tunnel_on(interface.name)
    declared = tunnel.tunnel.spec.mtu if tunnel is not None else None
    if interface.mtu is None:
        return declared
    if declared is not None and declared != interface.mtu:
        recorder.skip(
            f"{plan.fqn}:{interface.name}",
            Reason.NOT_REPRESENTABLE,
            f"the interface states mtu {interface.mtu} and {tunnel.name if tunnel else ''} "
            f"states {declared}; netplan holds one, and the interface's is the more specific",
        )
    return interface.mtu


def _record_policy(plan: DevicePlan, recorder: Recorder) -> None:
    """``spec.routing_policy``, which netplan hangs off an interface it is not about.

    netplan has ``routing-policy:``, and it takes ``from``/``to``/``table``/
    ``mark``/``priority`` — but it takes them *under one interface*, and a policy
    rule is a property of the whole stack. Writing the database under whichever
    interface happened to come first would be an arbitrary attribution in a file
    an operator edits by hand, and every rule with an ``iif``, an ``oif``, an
    inversion or an action other than ``lookup`` has no spelling there at all.
    So the database is named, once, with the emitter that writes all of it.
    """
    rules = plan.device.spec.routing_policy
    if not rules:
        return
    recorder.skip(
        plan.fqn,
        Reason.NOT_REPRESENTABLE,
        f"{plan.field('routing_policy')}: netplan's 'routing-policy:' sits under one "
        f"interface and a policy rule is a property of the whole stack, with no spelling "
        f"there for an 'iif', an 'oif', an inversion or a discarding action; the "
        + count_text(len(rules), "rule")
        + " of the policy database is written by 'netviz export routes' instead",
    )


def _route_map(
    plan: DevicePlan, recorder: Recorder, written: Sequence[Interface]
) -> dict[str, list[list[str]]]:
    """Which routes go under which interface, and what happens to the rest.

    netplan hangs every route off a device, and three kinds of route have no
    obvious one:

    * a **blackhole** route, which discards what it matches and by ``NV-F004``
      may name neither ``via`` nor ``dev``;
    * a route whose ``dev`` names an interface netplan left out — a loopback, a
      radio with no SSID;
    * a route whose next hop no declared prefix covers, which the validator
      already reports as ``E032``.

    The first is written under the first interface the document holds. netplan
    installs a blackhole route device-independently, so the file it sits in does
    not change what it does — an arbitrary choice, but not a guess. The other two
    are recorded as skipped: attaching a route to a port that cannot reach its
    next hop would produce a device that behaves differently from the file.

    Dropping any of them silently — which is what the first version of this did —
    is the one outcome ruled out. A route the inventory states and the artefact
    lacks, with a manifest that says nothing, is exactly the failure this package
    exists to prevent.
    """
    placed: dict[str, list[list[str]]] = {}
    names = {interface.name for interface in written}
    anchor = written[0].name if written else ""

    for interface in written:
        for family, config in (("ipv4", interface.ipv4), ("ipv6", interface.ipv6)):
            if config is not None and config.gateway is not None:
                placed.setdefault(interface.name, []).append(
                    [f"to: {'default' if family == 'ipv4' else '::/0'}", f"via: {config.gateway}"]
                )

    for index, route in enumerate(plan.routes):
        if route.table is not None and plan.device.spec.table_id(route.table) is None:
            recorder.skip(
                plan.fqn,
                Reason.NOT_REPRESENTABLE,
                f"{plan.field('routes', index, 'table')}: netplan's 'table:' is a number and "
                f"{route.table!r} is a VRF, whose table number a route distinguisher does not "
                f"state; the route to {route.prefix} is left out rather than written into "
                f"whichever table netplan would pick",
            )
            continue
        target = route_interface(plan, route)
        if target in names:
            placed.setdefault(target, []).append(_route(plan, route))
            continue
        if route.blackhole and anchor:
            placed.setdefault(anchor, []).append(_route(plan, route))
            continue
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            f"{plan.field('routes', index, 'prefix')}: netplan hangs a route off an interface, "
            + (
                f"and this device has none in the document to carry {route.prefix}"
                if route.blackhole
                else (
                    f"and the egress of the route to {route.prefix} is "
                    + (
                        f"{target!r}, which netplan left out"
                        if target is not None
                        else "not stated and no declared prefix covers its next hop"
                    )
                )
            ),
        )
    return placed


def _render_routes(entries: Sequence[Sequence[str]]) -> Iterator[str]:
    """One ``routes:`` block, or nothing when there are none."""
    if not entries:
        return
    yield "      routes:"
    for entry in entries:
        head, *rest = entry
        yield f"        - {head}"
        for line in rest:
            yield f"          {line}"


def _route(plan: DevicePlan, route: StaticRoute) -> list[str]:
    entry = [f"to: {route.prefix}"]
    if route.blackhole:
        entry.append("type: blackhole")
    elif route.via is not None:
        entry.append(f"via: {route.via}")
    if route.metric is not None:
        entry.append(f"metric: {route.metric}")
    # netplan's ``table:`` is a number, for the same reason its ``vrfs`` section
    # is: it renders to a netplan-managed table rather than to one named in
    # /etc/iproute2/rt_tables. A route whose table has no number never reaches
    # here -- :func:`_route_map` leaves it out and records why.
    if (number := plan.device.spec.table_id(route.table or "")) is not None:
        entry.append(f"table: {number}")
    return entry


def _wireless(plan: DevicePlan, interface: Interface, recorder: Recorder) -> Iterator[str]:
    """``access-points:`` — which SSID the radio associates to, or beacons.

    netplan refuses a ``wifis:`` entry with no access point at all ("No access
    points defined"), and refusing the whole file is how it refuses: the host
    then gets no configuration rather than a degraded one. A radio the inventory
    names no BSS for therefore cannot go in ``wifis:`` — there is nothing to put
    in the one key netplan requires, and netviz will not invent an SSID — so it
    is recorded as skipped and left out of the document.
    """
    wireless = interface.wireless
    if wireless is None or not wireless.bss:
        recorder.skip(
            f"{plan.fqn}:{interface.name}",
            Reason.NOT_REPRESENTABLE,
            "netplan needs at least one access point for a wifi device and refuses the whole "
            "file without one; the inventory names no SSID on this radio, and netviz will "
            "not invent one",
        )
        return
    yield "      access-points:"
    for bss in wireless.bss:
        yield f'        "{_escaped(bss.ssid)}":'
        yield f"          mode: {'ap' if wireless.role is RadioRole.AP else 'infrastructure'}"
        if wireless.band is not None:
            yield f'          band: "{wireless.band.value.replace("GHz", "")}GHz"'
        if wireless.channel is not None:
            yield f"          channel: {wireless.channel}"
        if bss.hidden:
            yield "          hidden: true"
        if bss.bssid:
            yield f'          bssid: "{bss.bssid}"'
        if bss.security is not None:
            yield "          auth:"
            yield f"            key-management: {_KEY_MANAGEMENT[bss.security]}"
            if bss.security is not Security.OPEN:
                yield f'            password: "{_PLACEHOLDER}"  # netviz holds no keys'


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


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

    ``allowed-ips`` is a routing decision, and the only one the inventory
    supports is "the peer itself". Widening it to the peer's whole prefix would
    route the near end's traffic for every address in that subnet down the
    tunnel, which is a policy nobody wrote down.
    """
    address, _, _ = cidr.partition("/")
    return f"{address}/{128 if ':' in address else 32}"


def _escaped(text: str) -> str:
    """``text`` as the inside of a double-quoted YAML scalar.

    Whitespace is *not* collapsed. An SSID is an opaque octet string (§4.1), so
    ``Guest  WiFi`` and ``Guest WiFi`` are two different networks and a radio
    configured with the second would never associate with the first. A
    double-quoted scalar can hold both, and the only characters that need doing
    anything about are the backslash, the quote, and a line break — which a
    quoted scalar could technically fold across lines, but which netplan's own
    reader has no use for.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")
