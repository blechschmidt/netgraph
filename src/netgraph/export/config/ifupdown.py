"""``ifupdown`` — the ``/etc/network/interfaces`` a Debian host boots from.

ifupdown is the oldest of the Linux dialects here and the least declarative: the
file is not a description of a network, it is an ordered list of stanzas that
``ifup`` walks and a set of hooks it shells out to. Four of its habits decide the
shape of everything below.

**``auto`` is the admin state.** There is no keyword for "configured, but not
started". An interface comes up at boot because a line says ``auto eno1``, and
the only way to say the opposite is to not say that. So ``enabled: false``
renders as an *absence* — and an absence in a generated file reads as an
oversight, which is why it is written as an absence and a comment saying the
absence is the statement.

**One address per stanza.** ``iface`` carries a single ``address``, so a port
with four addresses is four stanzas. The older idiom — ``eno1:0``, ``eno1:1`` —
is a 2.x-era alias, which is a *label* on an address rather than a second
address, and generating labels would put names into ``ip addr`` output that the
inventory never mentions. Repeated stanzas want ifupdown 0.7 or later, which is
every Debian since wheezy. Only the first stanza of an interface carries the link
and stacking keywords: ``bridge_ports`` in a second one would build the bridge
twice.

**A route is a command, not syntax.** ifupdown knows ``gateway`` and nothing else
about routing, so ``spec.routes`` becomes ``up``/``down`` hooks running iproute2
— word for word the commands :mod:`netgraph.export.routes` writes, because two
generators describing one route differently is how a network and its
documentation part company. ``replace`` rather than ``add``, so a repeated
``ifup`` is idempotent; ``down``, which runs *before* the interface goes, so a
route cannot outlive the interface it points out of.

**A value runs to the end of the line.** Nothing here is annotated inline: a
``# netgraph holds no keys`` after a passphrase would become part of the
passphrase. What has to be said to the reader is said in a comment of its own, at
column 0, where ifupdown's parser expects one.

Three things are refused (:data:`~netgraph.export.config.model.Unsupported`)
rather than quietly dropped, because a file without them configures a different
network from the one declared: the 802.1Q configuration of a bridge port, for
which stock ifupdown has no syntax at all; a VRF, of which it has no notion, so
every address and route in one would land in the global table; and an ``ap``
radio, because ``wpa-ssid`` configures a *station* and pointing it at the SSID
the radio is meant to beacon would make it a client of its own network.

Everything ifupdown has no business with — a routing protocol, PoE, a tunnel
netdev, the distribution's own ``lo`` stanza — is recorded in the export manifest
naming the dialect that does have business with it, and is not a refusal: the
file is still correct for what ifupdown covers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Final

from netgraph.errors import count_text
from netgraph.export.config import netplan
from netgraph.export.config.header import config_header
from netgraph.export.config.model import ConfigFile, Unsupported
from netgraph.export.config.plan import (
    DevicePlan,
    addresses_of,
    netns_limits,
    restricts_vlans,
    route_interface,
    unwritable_vlan,
)
from netgraph.export.manifest import Reason, Recorder
from netgraph.models import Interface, InterfaceType, StaticRoute
from netgraph.models.interface import Security
from netgraph.models.tunnel import TunnelType

__all__ = ["FILENAME", "declines", "files", "limits", "selects"]

#: Where ifupdown looks, and the whole of what it looks at: unlike netplan there
#: is no fragment directory that merges, only a file that may ``source`` others.
#: So this is *the* file, and the banner says so.
FILENAME = "etc/network/interfaces"

#: How each aggregate names the ports it is built from. The two are spelled
#: differently — an underscore for the bridge, a hyphen for the bond — because
#: they come from two packages, ``bridge-utils`` and ``ifenslave``, that grew
#: their ifupdown hooks independently. Neither spelling is negotiable.
_MEMBER_KEYWORDS: Final[dict[InterfaceType, str]] = {
    InterfaceType.BRIDGE: "bridge_ports",
    InterfaceType.LAG: "bond-slaves",
}

#: The digits an interface name ends in, which is where ifupdown's ``vlan`` hook
#: reads a sub-interface's 802.1Q VID from. Anchored and ASCII-only: a Unicode
#: digit is not something ``vconfig`` would parse.
_TRAILING_DIGITS: Final[re.Pattern[str]] = re.compile(r"([0-9]+)$")

#: The security modes that authenticate with a passphrase, and so need a
#: ``wpa-psk``. ``open`` needs none, and an unstated ``security`` is *not* a
#: statement that the network is protected -- a ``wpa-psk`` on an open network
#: stops wpa_supplicant associating at all.
_PSK_SECURITY: Final[frozenset[Security]] = frozenset({Security.WPA2_PSK, Security.WPA3_PSK})

#: Written where a passphrase belongs. Deliberately not a valid PSK of any
#: length: a placeholder that works is a placeholder that reaches production.
_PLACEHOLDER = "REPLACE-ME"


def selects(plan: DevicePlan) -> bool:
    """Is this a device whose boot-time networking ifupdown owns?"""
    return plan.kind in netplan.HOST_KINDS


def declines(plan: DevicePlan) -> str:
    """Why a device this dialect did not select got no file."""
    return (
        f"/etc/network/interfaces is read by a Debian host; a {plan.kind} is configured "
        f"through its own CLI, so 'netgraph export interfaces' is what describes this device"
    )


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def limits(plan: DevicePlan) -> tuple[Unsupported, ...]:
    """Everything ifupdown would have had to invent or drop for ``plan``."""
    return tuple(_limits(plan))


def _limits(plan: DevicePlan) -> Iterator[Unsupported]:
    yield from netns_limits(plan, "ifupdown")
    for index, vrf in enumerate(plan.device.spec.vrfs):
        yield Unsupported(
            element=plan.fqn,
            field=plan.field("vrfs", index),
            detail=(
                f"ifupdown configures interfaces and has no notion of a routing instance, "
                f"so nothing it writes can be placed in {vrf.name!r}; every address and "
                f"route of that VRF would land in the global table, which is a different "
                f"network from the one the inventory describes"
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
                f"{unwritable}. Stock ifupdown has no 802.1Q bridge-port syntax at all "
                f"('bridge_vids' and 'bridge_pvid' are ifupdown2's, not ifupdown's)"
            ),
        )
    if interface.vrf is not None:
        yield Unsupported(
            element=plan.fqn,
            field=plan.interface_field(interface, "vrf"),
            detail=(
                f"{interface.name} is bound to VRF {interface.vrf!r}; ifupdown has no VRF "
                f"concept, so this interface's addresses and the routes out of it would "
                f"land in the global table rather than in that instance"
            ),
        )
    wireless = interface.wireless
    if wireless is not None and wireless.bss and wireless.role.is_ap:
        yield Unsupported(
            element=plan.fqn,
            field=plan.interface_field(interface, "wireless"),
            detail=(
                f"{interface.name} is an 'ap' radio beaconing {len(wireless.bss)} SSID(s); "
                f"ifupdown's 'wpa-ssid' and 'wireless-essid' configure a station through "
                f"wpa_supplicant, and an access point is hostapd's. Writing the station "
                f"form would make this radio a client of the network it is meant to serve"
            ),
        )


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #


def files(plan: DevicePlan, recorder: Recorder) -> tuple[ConfigFile, ...]:
    """The one ``/etc/network/interfaces`` this device boots from."""
    included = tuple(_included(plan, recorder))
    routes = _routes_by_interface(plan, {interface.name for interface in included}, recorder)
    lines = [*config_header("#", "ifupdown", plan, notes=_notes(plan))]
    for interface in included:
        body = _interface(plan, interface, routes.get(interface.name, ()), recorder)
        lines.extend(["", *body])

    _record_out_of_remit(plan, recorder)

    recorder.emitted += 1
    return (ConfigFile(path=FILENAME, content="".join(f"{line}\n" for line in lines)),)


def _notes(plan: DevicePlan) -> tuple[str, ...]:
    notes = [
        "This is the whole file: 'ifup -a' reads it in order, so it replaces",
        "/etc/network/interfaces rather than adding to it. The distribution's 'lo'",
        "stanza and any 'source' line are not reproduced here -- keep them.",
        "Nothing below was read off the running system; try 'ifup --no-act <iface>'.",
    ]
    if plan.forwards_ipv4 or plan.forwards_ipv6:
        notes.append("This device forwards; ifupdown has no switch for that, so the sysctl is")
        notes.append("not in this file. See 'netgraph export interfaces' for the declared state.")
    return tuple(notes)


def _included(plan: DevicePlan, recorder: Recorder) -> Iterator[Interface]:
    """The interfaces this file writes a stanza for, in declaration order."""
    for interface in plan.interfaces:
        if interface.type is InterfaceType.LOOPBACK:
            recorder.skip(
                f"{plan.fqn}:{interface.name}",
                Reason.NOT_REPRESENTABLE,
                "the 'lo' stanza of /etc/network/interfaces is the distribution's, not "
                "netgraph's; a second stanza for the loopback would fight the first, and a "
                "declared router loopback belongs to the routing daemon",
            )
            continue
        if interface.type is InterfaceType.TUNNEL:
            _record_tunnel(plan, interface, recorder)
            continue
        yield interface


def _record_tunnel(plan: DevicePlan, interface: Interface, recorder: Recorder) -> None:
    """A ``tunnel`` interface, named with the tool that does build one."""
    subject = f"{plan.fqn}:{interface.name}"
    tunnel = plan.tunnel_on(interface.name)
    if tunnel is not None and tunnel.type is TunnelType.WIREGUARD:
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            f"{tunnel.name} is a WireGuard tunnel and ifupdown has no syntax for one; "
            f"generate it with 'netgraph export wireguard'",
        )
        return
    recorder.skip(
        subject,
        Reason.NOT_REPRESENTABLE,
        "ifupdown has no tunnel syntax; the netdev is created by 'ip link add' or by the "
        "daemon that owns the encapsulation -- strongSwan, openvpn, pppd -- and this file "
        "configures neither",
    )


def _record_out_of_remit(plan: DevicePlan, recorder: Recorder) -> None:
    """Everything ifupdown is simply not the tool for, named with the tool that is."""
    if plan.device.spec.routing is not None:
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            "'spec.routing' is a routing protocol; ifupdown brings interfaces up. Generate "
            "it with 'netgraph export frr'",
        )
    if plan.device.spec.routing_policy:
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            f"{plan.field('routing_policy')}: a policy rule is a property of the whole stack "
            f"and ifupdown runs a command only from an interface coming up, so there is no "
            f"stanza these belong to; the "
            + count_text(len(plan.device.spec.routing_policy), "rule")
            + " of the policy database is written by 'netgraph export routes' instead",
        )
    for interface in plan.interfaces:
        if interface.poe is not None:
            recorder.skip(
                f"{plan.fqn}:{interface.name}",
                Reason.NOT_REPRESENTABLE,
                "'poe' is switch hardware configuration; ifupdown does not touch the PSE",
            )
        if restricts_vlans(interface) and interface.vlan is not None:
            # The refusal above already fired for every port whose VLAN block
            # changes behaviour; what is left is the ordinary access port, whose
            # untagged domain a plain interface carries with no line at all.
            recorder.skip(
                f"{plan.fqn}:{interface.name}",
                Reason.NOT_REPRESENTABLE,
                f"the port is untagged in VLAN {interface.vlan.pvid}; a plain interface "
                f"carries untagged frames, so ifupdown writes no line for it",
            )


def _routes_by_interface(
    plan: DevicePlan, written: set[str], recorder: Recorder
) -> dict[str, tuple[StaticRoute, ...]]:
    """Each static route filed under the interface whose stanza will carry it.

    A route this file cannot attach is recorded rather than hung off whichever
    interface came first: an ``up`` hook on the wrong port runs at the wrong
    moment, and a next hop that is not on-link there does not become reachable
    for having been written down.
    """
    attached: dict[str, list[StaticRoute]] = {}
    for route in plan.routes:
        name = route_interface(plan, route)
        if name is None or name not in written:
            recorder.skip(plan.fqn, Reason.UNRESOLVED, _unattached(route, name))
            continue
        attached.setdefault(name, []).append(route)
    return {name: tuple(routes) for name, routes in attached.items()}


def _unattached(route: StaticRoute, name: str | None) -> str:
    if route.blackhole and route.dev is None:
        # A discard route is deliberately attached to nothing, so this is not a
        # gap in the inventory: it is a route with no interface for ifupdown to
        # hook it to, and the script emitter is where it belongs.
        return (
            f"the blackhole route to {route.prefix} egresses nothing, and ifupdown runs a "
            f"command only from an interface coming up; 'netgraph export routes' writes it"
        )
    if name is None:
        return (
            f"the route to {route.prefix} names no 'dev' and no declared prefix covers its "
            f"next hop, so there is no interface to hang 'ip route replace' on"
        )
    return (
        f"the route to {route.prefix} egresses {name!r}, which this file writes no stanza "
        f"for, so there is no 'up' hook to run it from"
    )


def _interface(
    plan: DevicePlan,
    interface: Interface,
    routes: Sequence[StaticRoute],
    recorder: Recorder,
) -> Iterator[str]:
    """One interface: its admin state, then one stanza per address.

    An interface with no address still gets a stanza — ``inet manual`` — because
    a bridge member, a LAG slave and an unnumbered uplink all have to be brought
    up by something, and ``manual`` is ifupdown's spelling for "bring the link up
    and configure no address on it".
    """
    yield from _admin_state(interface)
    addresses = addresses_of(interface)
    families = tuple(_family(cidr) for cidr in addresses) or ("inet",)
    for position, family in enumerate(families):
        yield f"iface {interface.name} {family} {'static' if addresses else 'manual'}"
        body: list[str] = []
        if addresses:
            body.append(f"address {addresses[position]}")
            if families.index(family) == position:
                body.extend(_gateway(interface, family))
        if position == 0:
            body.extend(_link(interface))
            body.extend(_stacking(plan, interface, recorder))
            body.extend(_wireless(interface))
        body.extend(_route_commands(plan, routes, families, position))
        for line in body:
            yield f"    {line}"


def _admin_state(interface: Interface) -> Iterator[str]:
    """``auto``, or the comment that stands in for its absence."""
    if interface.enabled:
        yield f"auto {interface.name}"
        return
    yield f"# manual: {interface.name} is declared 'enabled: false'. ifupdown says that by"
    yield "# leaving 'auto' out, so the stanza below is defined but not started at boot."


def _family(cidr: str) -> str:
    """``inet`` or ``inet6`` — which ``iface`` family an address belongs to."""
    return "inet6" if ":" in cidr else "inet"


def _gateway(interface: Interface, family: str) -> Iterator[str]:
    """The default route, in the one spelling ifupdown has for a route."""
    config = interface.ipv4 if family == "inet" else interface.ipv6
    if config is not None and config.gateway is not None:
        yield f"gateway {config.gateway}"


def _link(interface: Interface) -> Iterator[str]:
    if interface.mtu is not None:
        yield f"mtu {interface.mtu}"
    if interface.mac:
        yield f"hwaddress ether {interface.mac}"


def _stacking(plan: DevicePlan, interface: Interface, recorder: Recorder) -> Iterator[str]:
    """What this interface is built from: members, or a VLAN parent."""
    keyword = _MEMBER_KEYWORDS.get(interface.type)
    if keyword is not None and interface.members:
        yield f"{keyword} {' '.join(interface.members)}"
    if interface.type is not InterfaceType.VLAN or interface.parent is None:
        return
    yield f"vlan-raw-device {interface.parent}"
    _record_untagged_name(plan, interface, recorder)


def _record_untagged_name(plan: DevicePlan, interface: Interface, recorder: Recorder) -> None:
    """The VID lives in the *name*, so a name that does not carry it loses it.

    ifupdown's ``vlan`` hook takes the 802.1Q tag from the interface name and has
    no keyword for it: ``eno1.10`` is VLAN 10 because of what it is called.
    ``vlan-raw-device`` alone therefore cannot carry an inventory that named the
    sub-interface something else, and the operator has to rename it — which is
    reported rather than refused, because everything else about the interface is
    written correctly and the fix is one line of YAML.
    """
    vlan = interface.vlan
    if vlan is None:  # pragma: no cover - the model requires one on a 'vlan' interface
        return
    match = _TRAILING_DIGITS.search(interface.name)
    if match is not None and int(match.group(1)) == vlan.pvid:
        return
    recorder.skip(
        f"{plan.fqn}:{interface.name}",
        Reason.NOT_REPRESENTABLE,
        f"ifupdown's vlan hook reads the 802.1Q VID off the interface name, and "
        f"{interface.name!r} does not end in {vlan.pvid}; name the sub-interface "
        f"'{interface.parent}.{vlan.pvid}' for the tag to survive",
    )


def _wireless(interface: Interface) -> Iterator[str]:
    """The station half of wpa_supplicant's ifupdown integration.

    Only a radio that associates reaches here: an ``ap`` radio is refused by
    :func:`limits`, so the file is never written for one.

    ``wpa-psk`` is written only for a BSS the inventory says uses a passphrase.
    An unstated ``security`` is not a statement that the network is protected,
    and a ``wpa-psk`` on an open network stops wpa_supplicant associating at all
    — so the earlier "assume protected" rule was inventing a fact and breaking
    the case it invented it for. The ``netplan`` dialect writes no ``auth:``
    block from the same silence, and the two must not contradict each other from
    one inventory.
    """
    wireless = interface.wireless
    if wireless is None or not wireless.bss or wireless.role.is_ap:
        return
    bss = wireless.bss[0]
    yield f"wpa-ssid {_inline(bss.ssid)}"
    if bss.security in _PSK_SECURITY:
        yield f"wpa-psk {_PLACEHOLDER}"


def _route_commands(
    plan: DevicePlan, routes: Sequence[StaticRoute], families: Sequence[str], position: int
) -> Iterator[str]:
    """The ``up``/``down`` hooks the stanza at ``position`` is responsible for.

    A route goes on the first stanza of its own family, so an IPv6 route is not
    run before the address it needs exists; a route whose family this interface
    configures no address for falls back to the first stanza, which is the only
    one there is to run it from.
    """
    for route in routes:
        family = "inet" if route.prefix.version == 4 else "inet6"
        home = families.index(family) if family in families else 0
        if home != position:
            continue
        version = "-4" if route.prefix.version == 4 else "-6"
        arguments = _route_arguments(plan, route)
        yield f"up ip {version} route replace {arguments}"
        yield f"down ip {version} route del {arguments}"


def _route_arguments(plan: DevicePlan, route: StaticRoute) -> str:
    """The words after ``ip -4 route replace``.

    The same words, in the same order, as
    :mod:`netgraph.export.routes`: one route described two ways by one program is
    how a device and its documentation start to disagree. Every word is an
    address, an integer or an ``IfName``, and §4.1's grammar for the last admits
    no shell metacharacter, so the line is inert in the shell ifupdown runs its
    hooks under. ``vrf`` is absent because a device declaring one never gets
    here — it is refused by :func:`limits`.
    """
    words: list[str] = []
    if route.blackhole:
        # A route *type*, and it precedes the prefix, unlike every other word.
        words.append("blackhole")
    words.append(str(route.prefix))
    if route.via is not None:
        words.extend(["via", str(route.via)])
    if route.dev is not None:
        words.extend(["dev", route.dev])
    if route.table is not None:
        # By number wherever the inventory knows one, exactly as
        # :mod:`netgraph.export.routes` writes it: a name resolves only through
        # /etc/iproute2/rt_tables, and an ifupdown hook is in no position to
        # have edited that file first.
        number = plan.device.spec.table_id(route.table)
        words.extend(["table", route.table if number is None else str(number)])
    if route.metric is not None:
        words.extend(["metric", str(route.metric)])
    return " ".join(words)


def _inline(text: str) -> str:
    """``text`` with its line breaks replaced by single spaces, and nothing else.

    An ifupdown value runs to the end of the line, so a newline in one would turn
    the rest of it into a keyword ifupdown does not know. That much has to go.

    What must *not* go is the rest of the whitespace. An SSID is an opaque octet
    string (§4.1): ``Guest  WiFi`` and ``Guest WiFi`` are two different networks,
    and a station configured with the second never associates with the first. So
    runs of spaces are left exactly as written, and only ``\r`` and ``\n``
    become one space each — which is still lossy, and is the reason a name
    holding one is worth noticing in the generated file.
    """
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
