"""``frr`` — the routing, and nothing but the routing.

FRR is the only dialect here whose remit is *narrower* than an interface
configuration rather than wider. It configures a routing daemon: a VRF's static
routes, an OSPF area, a BGP autonomous system, and the handful of things a
router says about an interface it did not create. Everything else an inventory
holds — a bond, a VLAN, a radio, a PoE budget — is not something FRR declines to
express, it is something FRR was never asked about. That distinction is the
whole design of this module: out of remit is a manifest *skip* naming the
dialect that does have business with it, and the generated ``frr.conf`` is
complete for what FRR covers.

**Nothing is refused.** :func:`limits` returns an empty tuple, and that is a
statement rather than an omission. FRR's grammar is a superset of what an
inventory can state about routing: every static route, every VRF, every OSPF
interface and every BGP neighbour §16 can express has a spelling here. The two
cases that would have been refusals — a route or an interface naming a VRF the
device does not define, and an OSPF interface the device does not declare — are
refused before this module runs, by ``NG-F002``/``NG-F005`` in the model and by
``E034`` in the validator. Inventing a third refusal to have one would be
dishonest; when ``--force`` drives an inventory past ``E034``, the dropped name
is recorded in the manifest instead.

**No ``frr version``, no ``frr defaults``.** Both are the first lines of a real
``frr.conf`` and neither is written here. netgraph does not know which release
will read the file, and the defaults profile is not cosmetic: ``traditional``
and ``datacenter`` differ in timers and in what a session advertises, so picking
one would configure behaviour the inventory never stated. The file says so in a
comment where the two lines would have been, which is where the person adding
them will look.

**OSPF is enabled per interface, not by ``network`` statements.** ``spec.routing
.ospf.interfaces`` names interfaces, and ``ip ospf area`` in an interface block
is the spelling that means exactly that. A ``network A.B.C.D/M area X`` line
means something subtly different — it enables OSPF on whichever interfaces
happen to hold a matching address — so the two agree only until the address plan
changes, and the inventory named interfaces rather than prefixes.

**No ``no bgp ebgp-requires-policy``.** It is the line most generators emit, and
it changes what an eBGP session advertises. The inventory states neighbours, not
policy, so the file leaves the daemon's own default in place and says in a
comment that a session with no policy exchanges no prefixes on FRR 7.4 and
later. A generated file that quietly turns a safety default off is the kind of
thing nobody finds until the session is up.

**A route distinguisher has nowhere to go.** ``spec.vrfs[i].rd`` is written into
FRR only inside a BGP VPN address family, which needs an MPLS L3VPN design an
inventory does not describe (§16.1); the VRF is written as a plain routing
instance and the manifest says the RD was left out.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

from netgraph.export.config.header import config_header
from netgraph.export.config.model import ConfigFile, Unsupported
from netgraph.export.config.plan import DevicePlan, addresses_of
from netgraph.export.manifest import Reason, Recorder
from netgraph.models import (
    BgpConfig,
    Interface,
    InterfaceType,
    OspfConfig,
    StaticRoute,
    VrfDefinition,
)

__all__ = ["FILENAME", "declines", "files", "limits", "selects"]

#: Where FRR looks. One file rather than the per-daemon ``zebra.conf`` and
#: ``bgpd.conf`` split: integrated configuration is what current FRR ships with
#: (``frr.conf`` plus ``vtysh.conf``'s ``service integrated-vtysh-config``), and
#: it is the form ``vtysh -f`` reads back.
FILENAME = "etc/frr/frr.conf"

#: Interface types the host builds before FRR ever sees them. FRR configures an
#: interface; it creates none, so a bridge, a bond and a VLAN sub-interface are
#: recorded against the dialect that does build them. A ``tunnel`` is the same
#: case but gets its own record, because the tool that builds one is a different
#: tool again.
_STACKED: Final[frozenset[InterfaceType]] = frozenset(
    {InterfaceType.BRIDGE, InterfaceType.LAG, InterfaceType.VLAN}
)

#: The banner's own words. The second paragraph is the one worth the space: a
#: reader holding this file needs to know that the interfaces it names are not
#: created by it, or they will wonder why nothing came up.
_NOTES: Final[tuple[str, ...]] = (
    "Apply with 'vtysh -f <file>', or copy it into place and reload frr. It replaces",
    "the routing configuration of the daemon, and nothing here was read off the",
    "running system.",
    "",
    "Only routing is in this file. FRR creates no interfaces: a bridge, a bond, a",
    "VLAN sub-interface and a tunnel are built by whatever configures the host's",
    "netdevs, and zebra installs the addresses below onto interfaces that already",
    "exist. The export manifest names what was left out and which dialect writes it.",
)


def selects(plan: DevicePlan) -> bool:
    """Has this device anything for a routing daemon to do?

    Not "is it a router": a Linux server with one static route runs FRR in
    plenty of estates, and a box the inventory calls a router but gives no
    routing to has nothing to put in the file. The three fields FRR reads are
    the test.
    """
    spec = plan.device.spec
    return spec.routing is not None or bool(spec.routes) or bool(spec.vrfs)


def declines(plan: DevicePlan) -> str:
    """Why a device this dialect did not select got no file."""
    return (
        f"FRR is a routing daemon and this {plan.kind} declares no 'spec.routing', "
        f"'spec.routes' or 'spec.vrfs'; its frr.conf would be a hostname and nothing to "
        f"route. Its interfaces are 'netgraph export netplan' or 'netgraph export interfaces'"
    )


def limits(plan: DevicePlan) -> tuple[Unsupported, ...]:
    """Nothing: this dialect refuses no device.

    FRR's grammar is a superset of what an inventory can state about routing, so
    there is no field within FRR's remit that it would have to invent or drop.
    The two candidates are both refused earlier — a VRF reference that resolves
    to nothing by ``NG-F002``/``NG-F005`` when the document is parsed, and an
    OSPF interface the device has not got by ``E034`` — and everything else the
    inventory holds is out of FRR's remit rather than beyond its syntax, which
    makes it a manifest skip. See the module docstring.
    """
    return ()


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #


def files(plan: DevicePlan, recorder: Recorder) -> tuple[ConfigFile, ...]:
    """The one file vtysh reads for this device."""
    _record_out_of_remit(plan, recorder)

    lines = [
        *config_header("!", "frr", plan, notes=_NOTES),
        *_version_note(),
        # The inventory states the name, so this is transcription: FRR's
        # 'hostname' is what vtysh's prompt and its log lines carry.
        f"hostname {plan.name}",
        "!",
    ]
    for block in _blocks(plan):
        # '!' after every block, which is how FRR's own 'show running-config'
        # separates them and what makes a diff against it line up.
        lines.extend([*block, "!"])

    recorder.emitted += 1
    return (ConfigFile(path=FILENAME, content="".join(f"{line}\n" for line in lines)),)


def _version_note() -> Iterator[str]:
    """The comment standing where ``frr version``/``frr defaults`` would be."""
    yield "! No 'frr version' or 'frr defaults' line is written here: netgraph does not know"
    yield "! which release will read this file, and the defaults profile is not cosmetic --"
    yield "! 'traditional' and 'datacenter' differ in their timers and in what a session"
    yield "! advertises. Copy the two lines from the target's own frr.conf."
    yield "!"


def _blocks(plan: DevicePlan) -> Iterator[list[str]]:
    """Every stanza of the file, in the order FRR's own output has them."""
    spec = plan.device.spec
    for vrf in spec.vrfs:
        yield _vrf_block(plan, vrf)
    for interface in plan.interfaces:
        block = _interface_block(plan, interface)
        if block:
            yield block
    # A route with no 'vrf' is in the global instance, and FRR writes that one
    # at the top level rather than in any block.
    global_routes = [_route(route) for route in plan.routes if route.vrf is None]
    if global_routes:
        yield global_routes
    routing = spec.routing
    if routing is not None and routing.ospf is not None:
        yield _ospf_block(routing.ospf)
    if routing is not None and routing.bgp is not None:
        yield _bgp_block(routing.bgp)
    # Conventional, and load-bearing: without a 'line vty' node a reloaded
    # daemon has no terminal configuration to attach an access class to.
    yield ["line vty"]


def _vrf_block(plan: DevicePlan, vrf: VrfDefinition) -> list[str]:
    """One routing instance and the static routes placed in it."""
    lines = [f"vrf {vrf.name}"]
    if vrf.description:
        # FRR's vrf node has no 'description' command, so the operator's words
        # are kept as a comment rather than turned into a line vtysh rejects.
        lines.append(f" ! {_inline(vrf.description)}")
    lines.extend(f" {_route(route)}" for route in plan.routes if route.vrf == vrf.name)
    lines.append(" exit-vrf")
    return lines


def _interface_block(plan: DevicePlan, interface: Interface) -> list[str]:
    """What FRR has to say about one interface, or nothing at all.

    An interface FRR would only name is left out entirely: an empty block tells
    the reader an interface matters to the routing when it does not.
    """
    body: list[str] = []
    if interface.description:
        body.append(f" description {_inline(interface.description)}")
    for cidr in addresses_of(interface):
        body.append(f" {'ipv6' if ':' in cidr else 'ip'} address {cidr}")
    area = _ospf_area(plan, interface)
    if area is not None:
        body.append(f" ip ospf area {area}")
    if not body:
        return []
    # FRR spells the binding on the interface line itself, and an interface
    # named without it is a different interface in the global instance.
    head = f"interface {interface.name}"
    if interface.vrf:
        head += f" vrf {interface.vrf}"
    return [head, *body]


def _ospf_area(plan: DevicePlan, interface: Interface) -> str | None:
    """The area this interface runs, when ``spec.routing.ospf`` names it."""
    routing = plan.device.spec.routing
    ospf = routing.ospf if routing is not None else None
    if ospf is None or interface.name not in ospf.interfaces:
        return None
    return ospf.area


def _ospf_block(ospf: OspfConfig) -> list[str]:
    """The OSPF instance, which is a router-id and a comment about the area."""
    lines = [
        "! The area is enabled per interface, with 'ip ospf area' in the interface blocks",
        "! above, rather than with 'network' statements: a network statement enables OSPF",
        "! on whatever interface happens to hold a matching address, and the inventory",
        "! names interfaces. The two agree only until the address plan changes.",
        "!",
        "! This is OSPFv2. The inventory states one OSPF instance and no protocol version,",
        "! so no 'router ospf6' is inferred from the interfaces' IPv6 addresses.",
        "router ospf",
    ]
    if ospf.router_id is not None:
        lines.append(f" ospf router-id {ospf.router_id}")
    return lines


def _bgp_block(bgp: BgpConfig) -> list[str]:
    """The local AS, its neighbours, and one address family per family used."""
    lines = [
        "! 'no bgp ebgp-requires-policy' is deliberately absent: it changes what an eBGP",
        "! session advertises, and the inventory states neighbours rather than policy. On",
        "! FRR 7.4 and later a session with no inbound or outbound policy exchanges no",
        "! prefixes until one is configured.",
        f"router bgp {bgp.asn}",
    ]
    if bgp.router_id is not None:
        lines.append(f" bgp router-id {bgp.router_id}")
    for neighbor in bgp.neighbors:
        lines.append(f" neighbor {neighbor.address} remote-as {neighbor.remote_asn}")
        if neighbor.description:
            lines.append(
                f" neighbor {neighbor.address} description {_inline(neighbor.description)}"
            )
    for version, family in ((4, "ipv4"), (6, "ipv6")):
        # A session is carried over the family of the address it is configured
        # with, which is the only family the inventory says it exchanges.
        members = [entry for entry in bgp.neighbors if entry.version == version]
        if not members:
            continue
        lines.append(f" address-family {family} unicast")
        lines.extend(f"  neighbor {entry.address} activate" for entry in members)
        lines.append(" exit-address-family")
    return lines


def _route(route: StaticRoute) -> str:
    """One static route, in the order FRR's grammar takes the words.

    ``ip`` or ``ipv6`` comes from the destination's own family rather than from
    the next hop: the two are the same by ``NG-F003``, and the destination is
    the one a route always has.

    ``metric`` is not written; see the comment in the body for why substituting
    FRR's administrative distance for it would be the one thing this package
    promises not to do.
    """
    words = ["ipv6" if route.prefix.version == 6 else "ip", "route", str(route.prefix)]
    if route.blackhole:
        words.append("blackhole")
    if route.via is not None:
        words.append(str(route.via))
    if route.dev is not None:
        words.append(route.dev)
    # ``metric`` is deliberately absent. The number FRR takes in this position is
    # an *administrative distance* -- which protocol's answer wins for a prefix,
    # 1 to 255 -- and a metric ranks routes within one protocol over the whole
    # 32-bit range. Writing one where the other belongs would turn a metric of
    # 200 into a distance of 200 and a metric of 1000 into a syntax error, so it
    # is reported (:func:`_record_out_of_remit`) rather than substituted.
    return " ".join(words)


# --------------------------------------------------------------------------- #
# What FRR is not the tool for
# --------------------------------------------------------------------------- #


def _record_out_of_remit(plan: DevicePlan, recorder: Recorder) -> None:
    """Everything the inventory holds that a routing daemon has no view of."""
    for index, vrf in enumerate(plan.device.spec.vrfs):
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            f"{plan.field('vrfs', index, 'rd')}: FRR writes a route distinguisher only inside "
            f"a BGP VPN address family, which needs an MPLS L3VPN design this inventory does "
            f"not describe; {vrf.name!r} is written as a plain routing instance",
        )
    for index, route in enumerate(plan.routes):
        if route.metric is None:
            continue
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            f"{plan.field('routes', index, 'metric')}: FRR's 'ip route' takes an "
            f"administrative distance in that position -- which protocol wins for a prefix, "
            f"1 to 255 -- and not a metric, which ranks routes within one protocol. The "
            f"route to {route.prefix} is written without its metric of {route.metric} rather "
            f"than with one number standing in for the other",
        )
    for interface in plan.interfaces:
        _record_interface(plan, interface, recorder)
    _record_unknown_ospf_interfaces(plan, recorder)


def _record_interface(plan: DevicePlan, interface: Interface, recorder: Recorder) -> None:
    subject = f"{plan.fqn}:{interface.name}"
    if interface.type in _STACKED:
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            f"FRR configures interfaces but builds none, and a {interface.type.value} is "
            f"built; create it with 'netgraph export netplan' or 'netgraph export networkd'",
        )
    elif interface.type is InterfaceType.TUNNEL:
        tunnel = plan.tunnel_on(interface.name)
        named = f"tunnel {tunnel.name!r}" if tunnel is not None else "a tunnel"
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            f"{named} terminates here and FRR creates no netdev; generate it with "
            f"'netgraph export wireguard' or 'netgraph export netplan'. FRR routes over the "
            f"interface once something else has made it",
        )
    elif interface.vlan is not None:
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            "'vlan' is 802.1Q port configuration; FRR has no switch port and no bridge, so "
            "neither the mode nor the tagged set is in this file",
        )
    if interface.wireless is not None:
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            "'wireless' configures a radio; hostapd and wpa_supplicant own that, and FRR "
            "sees only the interface it produces",
        )
    if interface.poe is not None:
        recorder.skip(
            subject,
            Reason.NOT_REPRESENTABLE,
            "'poe' is switch hardware configuration; FRR does not touch the PSE",
        )


def _record_unknown_ospf_interfaces(plan: DevicePlan, recorder: Recorder) -> None:
    """OSPF interfaces the device does not declare.

    ``E034`` refuses these, so a validated inventory never reaches this. Behind
    ``--force`` the name would otherwise vanish without a word: ``ip ospf area``
    lives in an interface block, and there is no block to put it in.
    """
    routing = plan.device.spec.routing
    ospf = routing.ospf if routing is not None else None
    if ospf is None:
        return
    declared = {interface.name for interface in plan.interfaces}
    for name in ospf.interfaces:
        if name in declared:
            continue
        recorder.skip(
            f"{plan.fqn}:{name}",
            Reason.UNRESOLVED,
            f"{plan.field('routing', 'ospf', 'interfaces')} names {name!r}, which the device "
            f"does not declare, so OSPF is not enabled on it. E034 refuses this; only "
            f"'--force' reaches here",
        )


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def _inline(text: str) -> str:
    """``text`` collapsed onto one line.

    A description runs to the end of the line in FRR's grammar, so a newline in
    one would turn the rest of it into a command vtysh either rejects or, worse,
    accepts as something else.
    """
    return " ".join(text.split())
