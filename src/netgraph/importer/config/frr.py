"""``--from frr``: an ``frr.conf``, or what ``vtysh -c 'show running-config'`` prints.

FRR is the one dialect here whose remit is narrower than an interface
configuration rather than wider, and reading it back is where that hurts.
:mod:`netgraph.export.config.frr` writes routing — static routes, a VRF, an OSPF
area, a BGP autonomous system — and the handful of things a routing daemon says
about an interface somebody else built. A :class:`~netgraph.importer.draft.Draft`
holds none of that. It has devices, interfaces and cables; there is no field for
a route, none for a BGP neighbour and none for an OSPF area. So most of a real
``frr.conf`` is read, counted, and reported rather than imported, and that is the
shape of this module rather than a gap in it.

What does survive the trip is small: an interface block's name, its
``description``, its addresses and its admin state.

**Every imported interface is ``ethernet``, and that is a fallback.** FRR never
says what a link *is*. It creates no netdev — a bridge, a bond, a VLAN
sub-interface and a tunnel are all built by whatever configures the host, and
zebra installs addresses onto interfaces that already exist — so ``interface
bond0`` in an ``frr.conf`` reads exactly like ``interface eno1``. netgraph's
``type`` is required, so the neutral value is written and every such interface
carries a comment saying where it came from. An import that merges this capture
with an ``ip -j link show`` of the same host gets the real type from there:
:meth:`~netgraph.importer.draft.DraftInterface.merge` lets a specific type
displace the fallback, never the other way round.

**Silence about an interface means nothing at all.** FRR configures a subset of a
box's links, and an interface with nothing for the routing daemon to say about it
is not in the file — :func:`netgraph.export.config.frr._interface_block` omits an
empty block on purpose. An interface the inventory declares and this file does
not mention is therefore not drift; it is a question this dialect was never
asked. That is why :mod:`netgraph.drift.coverage` grants ``frr`` no capability:
not ``interfaces``, because the list is partial; not ``addresses``, because zebra
is one of several things that can put an address on a link; not ``links``,
because FRR reports no neighbours; not ``members``, because it builds nothing.

**The device kind is left alone.** A box running FRR looks like a router, and
saying so would be a guess: the emitter's own ``selects`` starts from the same
observation and rejects it, because a Linux server with one static route runs FRR
in plenty of estates. The draft's neutral ``computer`` stands until an input
observes something better.

**The hostname is read, never obeyed.** ``hostname`` in an ``frr.conf`` is what
vtysh's prompt and the daemon's log lines carry, and it is frequently not the
device's inventory name. When it differs from the ``host`` the caller named, the
difference is reported and the caller's choice is kept: retargeting the capture
silently would file one device's addresses under another's name.

**Loopbacks are not imported**, for the reason
:mod:`netgraph.importer.iproute` gives: ``lo`` terminates no cable and appears in
no topology. Only ``lo`` — netgraph's ``loopback`` type is for a router loopback
somebody declared on purpose, and ``lo0`` in an ``frr.conf`` is usually one of
those, so it is imported rather than dropped. It arrives with the fallback type
like every other interface here, which drift reads as "not seen" rather than as a
difference, because a capture that cannot list a device's interfaces cannot
contradict a declared type either.

Everything outside that — static routes, ``vrf`` blocks, ``router bgp``,
``router ospf``, ``ip ospf area``, ``line vty`` — is counted and reported once per
kind, with the count. Once per kind because a file with forty static routes would
otherwise bury its own report, and *with the count* because "the capture holds 12
static routes, none of which was imported" and "the capture holds no static
route" are different answers to the question an operator is actually asking.

Nothing here raises. A line this module does not recognise is tallied and the
next one is read: a running configuration holds route maps, prefix lists, BFD
sessions and vendor keywords, and a reader that stopped at the first of them
would be a reader nobody pointed at a real device twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from netgraph.importer.config.common import fold_into
from netgraph.importer.draft import Draft, DraftDevice, DraftInterface, comment_text
from netgraph.importer.names import interface_name

__all__ = ["read_frr"]

#: The kernel loopback, and only it. ``lo0`` and ``Loopback0`` are *declared*
#: router loopbacks -- an operator wrote them down, netgraph's ``loopback`` type
#: exists for exactly that, and dropping one would lose an interface the
#: inventory has -- so they are imported like any other interface, with the
#: fallback type and the comment that goes with it.
_LOOPBACK_NAMES: Final[frozenset[str]] = frozenset({"lo"})

#: Unindented keywords that carry nothing an inventory could hold and are not
#: worth a line in the report: the version and defaults profile the emitter
#: deliberately omits, the daemon's own logging and terminal settings, and the
#: markers ``show running-config`` ends its output with.
_BOILERPLATE: Final[frozenset[str]] = frozenset(
    {
        "agentx",
        "banner",
        "debug",
        "enable",
        "end",
        "exit",
        "frr",
        "log",
        "password",
        "service",
        "username",
    }
)

#: What ``ip route``/``ipv6 route`` is spelled as, at the top level and inside a
#: ``vrf`` block alike.
_ROUTE_KEYWORDS: Final[frozenset[str]] = frozenset({"ip", "ipv6"})

#: Words that are a namespace rather than a directive. ``ip``, ``ipv6`` and
#: ``no`` open half of FRR's grammar, so an unrecognised line starting with one
#: is reported with its second word as well: a report saying ``ip`` names
#: nothing, and ``ip forwarding`` names the line it came from.
_NAMESPACE_KEYWORDS: Final[frozenset[str]] = frozenset({"ip", "ipv6", "no"})


@dataclass(slots=True)
class _Seen:
    """What the file held that the draft has no field for, counted.

    Accumulated across the whole file and reported once at the end, so that a
    configuration with one static route and one with forty produce one line each
    -- and so that the line can state which of the two this was.
    """

    routes_v4: int = 0
    routes_v6: int = 0
    vrfs: list[str] = field(default_factory=list)
    bgp_asns: list[str] = field(default_factory=list)
    bgp_neighbors: list[str] = field(default_factory=list)
    ospf_instances: list[str] = field(default_factory=list)
    ospf_areas: list[str] = field(default_factory=list)
    ospf_interfaces: list[str] = field(default_factory=list)
    vty_lines: int = 0
    vrf_bindings: list[str] = field(default_factory=list)
    loopbacks: list[str] = field(default_factory=list)
    maskless: list[str] = field(default_factory=list)
    unrecognised: list[str] = field(default_factory=list)


def read_frr(text: str, *, source: str, host: str, draft: Draft) -> None:
    """Fold one ``frr.conf`` -- or one ``show running-config`` -- into ``draft``.

    Args:
        text: The file, or the command's output; the two have the same grammar,
            which is what ``service integrated-vtysh-config`` is for.
        source: Name of the input, for comments and the run report.
        host: Element name of the device the configuration belongs to. The
            file's own ``hostname`` is not used for this; see the module
            docstring.
        draft: Accumulator, mutated in place.
    """
    fold_into(draft, host, source)
    device = draft.device(host)
    seen = _Seen()

    head = ""
    interface: DraftInterface | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        # ``!`` at any indentation is a comment, and a lone ``!`` is how FRR
        # separates two blocks. Neither ends the block for this reader: the next
        # unindented keyword does, which is the rule vtysh itself parses by.
        if not stripped or stripped.startswith("!"):
            continue
        if line[:1].isspace():
            _read_body(stripped, head=head, interface=interface, seen=seen)
            continue
        head = stripped
        interface = _open_block(
            stripped, device=device, seen=seen, source=source, host=host, draft=draft
        )

    _report(seen, source=source, draft=draft)


# --------------------------------------------------------------------------- #
# The unindented keywords
# --------------------------------------------------------------------------- #


def _open_block(
    line: str, *, device: DraftDevice, seen: _Seen, source: str, host: str, draft: Draft
) -> DraftInterface | None:
    """Act on one unindented line, returning the interface its block configures.

    ``None`` for every other block, which is what stops the indented lines under
    a ``router bgp`` from being read as interface commands.
    """
    words = line.split()
    keyword = words[0]
    if keyword == "hostname":
        _read_hostname(words, source=source, host=host, draft=draft)
    elif keyword == "interface":
        return _open_interface(words, device=device, seen=seen, source=source, draft=draft)
    elif keyword == "vrf" and len(words) > 1:
        _append_unique(seen.vrfs, words[1])
    elif keyword == "router" and len(words) > 1:
        _open_router(words, seen)
    elif keyword == "line":
        seen.vty_lines += 1
    elif keyword in _ROUTE_KEYWORDS and words[1:2] == ["route"]:
        _count_route(keyword, seen)
    elif keyword not in _BOILERPLATE:
        _append_unique(seen.unrecognised, _directive(words))
    return None


def _read_hostname(words: list[str], *, source: str, host: str, draft: Draft) -> None:
    """Report a file that names a device other than the one it was read as.

    Not an error and not a retarget. ``hostname`` is the daemon's idea of what to
    print in a prompt; the caller's ``--host`` is a statement about which
    inventory element this configuration belongs to, and only one of the two is
    a claim about the tree being built.
    """
    if len(words) < 2 or words[1] == host:
        return
    draft.note(
        f"{source}: the file sets 'hostname {comment_text(words[1])}' but it was read as "
        f"{host!r}; netgraph kept the host you named -- re-run with --host "
        f"{comment_text(words[1])} if this configuration belongs to that device"
    )


def _open_interface(
    words: list[str], *, device: DraftDevice, seen: _Seen, source: str, draft: Draft
) -> DraftInterface | None:
    """Start an ``interface`` block, adding the interface to the device."""
    if len(words) < 2:
        _append_unique(seen.unrecognised, "interface")
        return None
    raw = words[1]
    if raw.lower() in _LOOPBACK_NAMES:
        _append_unique(seen.loopbacks, raw)
        return None
    name, original = interface_name(raw)
    if name is None:
        draft.note(f"{source}: interface name {raw!r} holds no usable characters and was skipped")
        return None

    interface = DraftInterface(name=name, type="ethernet")
    if original is not None:
        interface.comments.append(
            f"the interface is named {comment_text(original)!r} in the configuration; renamed "
            "here because a netgraph interface name may only hold letters, digits, '.', '/' "
            "and '-'"
        )
    interface.comments.append(
        "inferred: FRR configures interfaces but creates none, so this file never says what "
        "the link is; 'ethernet' is netgraph's neutral type -- correct it if this is a bridge, "
        "a bond, a VLAN sub-interface or a tunnel"
    )
    # ``interface eth0 vrf blue`` is a different interface from ``interface
    # eth0`` as far as FRR is concerned, and the draft has no field for the
    # binding, so the name is kept and the binding is reported.
    if len(words) > 3 and words[2] == "vrf":
        _append_unique(seen.vrf_bindings, f"{name} in {words[3]}")
    return device.add_interface(interface)


def _open_router(words: list[str], seen: _Seen) -> None:
    """Tally a ``router bgp`` or ``router ospf`` block by what it is."""
    protocol = words[1]
    if protocol == "bgp":
        _append_unique(seen.bgp_asns, words[2] if len(words) > 2 else "<no AS>")
    elif protocol.startswith("ospf"):
        _append_unique(seen.ospf_instances, protocol)
    else:
        _append_unique(seen.unrecognised, f"router {protocol}")


def _count_route(keyword: str, seen: _Seen) -> None:
    if keyword == "ipv6":
        seen.routes_v6 += 1
    else:
        seen.routes_v4 += 1


# --------------------------------------------------------------------------- #
# The indented lines
# --------------------------------------------------------------------------- #


def _read_body(line: str, *, head: str, interface: DraftInterface | None, seen: _Seen) -> None:
    """Act on one indented line, in the context of the block above it.

    A line belonging to a block that was already reported -- the body of a
    ``route-map``, a ``router ospf``'s router-id, an ``exit-vrf`` -- is passed
    over here rather than counted again: the report names the block, and naming
    each of its lines as well would say the same thing forty times.
    """
    words = line.split()
    keyword = words[0]
    opener = head.split()
    if opener[:1] == ["interface"]:
        if interface is not None:
            _read_interface_body(words, interface=interface, seen=seen)
        return
    if keyword in _ROUTE_KEYWORDS and words[1:2] == ["route"]:
        # A route inside a ``vrf`` block; FRR writes the global instance's
        # routes unindented and these indented, and both are routes.
        _count_route(keyword, seen)
        return
    if opener[:2] == ["router", "bgp"] and keyword == "neighbor" and words[2:3] == ["remote-as"]:
        # ``neighbor X remote-as Y`` is the line that declares a session;
        # ``neighbor X description``/``activate`` describe one already declared.
        _append_unique(seen.bgp_neighbors, words[1])
    if head.startswith("router ospf") and keyword in ("ospf", "router-id") and len(words) > 1:
        return


def _read_interface_body(words: list[str], *, interface: DraftInterface, seen: _Seen) -> None:
    """One line of an ``interface`` block: the four things the draft can hold."""
    negated = words[0] == "no"
    words = words[1:] if negated else words
    if not words:
        return
    keyword = words[0]

    if keyword == "shutdown":
        # ``shutdown`` is the administrative state, which is what ``enabled``
        # means; a running configuration usually shows only the ``no`` form.
        interface.enabled = negated
    elif keyword == "description" and len(words) > 1 and not negated:
        interface.description = " ".join(words[1:])
    elif keyword in ("ip", "ipv6") and words[1:2] == ["address"] and len(words) > 2 and not negated:
        _read_address(words[2], family=keyword, interface=interface, seen=seen)
    elif keyword == "ip" and words[1:3] == ["ospf", "area"] and len(words) > 3 and not negated:
        _append_unique(seen.ospf_areas, words[3])
        _append_unique(seen.ospf_interfaces, interface.name)
    else:
        _append_unique(seen.unrecognised, f"interface {_directive(words)}")


def _read_address(value: str, *, family: str, interface: DraftInterface, seen: _Seen) -> None:
    """One ``ip address``/``ipv6 address`` value, kept only in CIDR form.

    FRR's grammar takes a prefix length and zebra rejects an address without
    one, so a value missing it is a malformed line rather than a shorthand to
    expand. It is reported rather than repaired: guessing a mask would state a
    prefix nobody wrote, and the wrong mask is how a route ends up somewhere
    surprising.
    """
    if "/" not in value:
        _append_unique(seen.maskless, value)
        return
    target = interface.ipv6 if family == "ipv6" else interface.ipv4
    if value not in target:
        target.append(value)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _report(seen: _Seen, *, source: str, draft: Draft) -> None:
    """One line per kind of thing the file held and the draft cannot.

    Emitted in a fixed order rather than the order the file happened to use, so
    that two captures of the same device produce the same report.
    """
    routes = seen.routes_v4 + seen.routes_v6
    if routes:
        draft.note(
            f"{source}: the configuration holds {routes} static route(s) "
            f"({seen.routes_v4} IPv4, {seen.routes_v6} IPv6); an import draft describes "
            f"interfaces and links, so none of them was imported -- 'spec.routes' has to be "
            f"written by hand"
        )
    if seen.vrfs:
        draft.note(
            f"{source}: the configuration defines {len(seen.vrfs)} VRF(s) "
            f"({_listed(seen.vrfs)}); an import draft has no VRF, so they were not imported"
        )
    if seen.vrf_bindings:
        draft.note(
            f"{source}: {len(seen.vrf_bindings)} interface(s) are bound to a VRF "
            f"({_listed(seen.vrf_bindings)}); a draft interface has no VRF field, so the "
            f"interface was imported and the binding was not"
        )
    if seen.bgp_asns:
        draft.note(
            f"{source}: the configuration runs BGP (AS {_listed(seen.bgp_asns)}) with "
            f"{len(seen.bgp_neighbors)} neighbour(s); an import draft has no place for a "
            f"session, so none was imported"
        )
    if seen.ospf_instances:
        draft.note(
            f"{source}: the configuration runs OSPF ({_listed(seen.ospf_instances)}) on "
            f"{len(seen.ospf_interfaces)} interface(s)"
            + (f" in area(s) {_listed(seen.ospf_areas)}" if seen.ospf_areas else "")
            + "; an import draft has no place for it, so the interfaces were imported without "
            "their area"
        )
    if seen.vty_lines:
        draft.note(
            f"{source}: the configuration holds {seen.vty_lines} 'line vty' block(s); that is "
            f"the daemon's terminal configuration and describes no part of the network"
        )
    if seen.loopbacks:
        draft.note(
            f"{source}: {_listed(seen.loopbacks)} is the kernel loopback; it terminates no cable "
            f"and appears in no topology, so it was not imported"
        )
    if seen.maskless:
        draft.note(
            f"{source}: {len(seen.maskless)} address(es) ({_listed(seen.maskless)}) carry no "
            f"prefix length; netgraph writes addresses in CIDR form and will not guess a mask, "
            f"so they were not imported"
        )
    if seen.unrecognised:
        draft.note(
            f"{source}: {len(seen.unrecognised)} FRR directive(s) netgraph does not read "
            f"({_listed(seen.unrecognised)}) were left alone; a routing configuration says far "
            f"more than an inventory does"
        )


def _directive(words: list[str]) -> str:
    """How an unrecognised line is named in the report: one word, or two."""
    if words[0] in _NAMESPACE_KEYWORDS and len(words) > 1:
        return f"{words[0]} {words[1]}"
    return words[0]


def _listed(values: list[str], limit: int = 6) -> str:
    """``a, b, c and 4 more`` -- enough to recognise, short enough to read."""
    shown = ", ".join(comment_text(value) for value in values[:limit])
    remainder = len(values) - limit
    return f"{shown} and {remainder} more" if remainder > 0 else shown


def _append_unique(target: list[str], value: str) -> None:
    """Append ``value`` if it is new, keeping the order it was first seen in."""
    if value not in target:
        target.append(value)
