"""``--from networkd``: the ``.network`` and ``.netdev`` units of a systemd host.

What arrives here is not one file. systemd-networkd splits a link into the thing
that *exists* (a ``.netdev``) and the thing that is *configured* (a ``.network``
matched by name), and an operator collecting a host runs ``cat
/etc/systemd/network/*`` — so the capture is a stream of ``[Section]`` blocks
from however many files, with nothing but the sections themselves to say where
one unit ended. ``[NetDev]`` and ``[Match]`` are what say it: each begins a unit
and names the interface, and every section after one belongs to it until the
next. That rule is the whole of the file splitting, and it is exact for a
generated directory and for a hand-written one, because a unit that had neither
section would configure nothing.

**Three statements are read backwards, so stacking is resolved last.** networkd
states enslavement on the *member* (``[Network] Bridge=br0``) where netviz
states membership on the aggregate, exactly as ``ip`` does with ``master`` and
:func:`netviz.importer.iproute` inverts it. It also states a VLAN's and a
tunnel's lower link on the *lower link* (``[Network] VLAN=vlan10``) and never on
the netdev, which is the only place either can be read from. All three are
therefore collected across the whole stream and applied once it has been read,
not as each unit goes by: the ``.netdev`` that creates ``vlan10`` and the
``.network`` of ``eno1`` that claims it are different files, in either order.

**A unit the schema would reject is dropped, not written.** A bridge or bond no
unit enslaves anything into has no ``members`` (``NG-I003``); a ``vlan`` netdev
whose lower link is claimed by no ``.network``, or which states no ``[VLAN]
Id=``, has no ``parent`` or no VID (``NG-I002``). Both would produce a tree that
does not load, so both are dropped with the reason on the run report — the same
call :mod:`netviz.importer.iproute` makes for the same reason.

**``[BridgeVLAN]`` is a port's 802.1Q configuration, and almost fits.** One
``VLAN=`` and a ``PVID=`` naming it is an access port. Anything else is a trunk:
several ``VLAN=`` lines, a range, or a single VLAN with no PVID — a lone tagged
VLAN is still tagged. What does not fit is a trunk whose ``PVID=`` names an
untagged VLAN of its own, because the draft has no native-VLAN field; the id
goes into the tagged set, where it belongs anyway, and the fact that it was also
the untagged one is written as a comment above the block rather than lost.

**A VLAN sub-interface implies a trunk underneath it.** ``vlan10`` on ``eno1``
receives only what ``eno1`` tags with VID 10, so the parent must carry it. The
parent's ``vlan`` block is written with a comment marking the inference, as in
:mod:`netviz.importer.iproute`; where the parent already has a
``[BridgeVLAN]`` trunk the inferred ids are folded into it, since both are
statements about the same port.

**Link-local and loopback addresses are dropped**, by prefix — ``fe80::`` is
autoconfigured on every link there is and ``127.`` belongs to a loopback that is
not imported, so either would say the same thing about every host in the tree.
**A stated MAC address is kept**, on a bridge and a bond too: unlike the address
``ip`` reports for one, ``MACAddress=`` is something somebody wrote down, which
is what an inventory records.

**What has nowhere to go is noted once per kind per source.** ``[Route]``,
``[WireGuard]``, ``[WireGuardPeer]``, ``[Tunnel]``, ``[VXLAN]``, ``[GENEVE]``,
``[Bond]`` and the resolver and DHCP keys of ``[Network]`` state routing, key
material, an encapsulation or a bonding mode; a draft interface has a field for
none of them, and a tunnel is its own document naming both ends
(``docs/schema.md`` §14). The messages name the section or the key and never the
interface, and :meth:`~netviz.importer.draft.Draft.note` keeps only the first
of two identical lines, so forty units are one line in the run report. Sections
that configure how a link *comes up* — ``[DHCPv4]``, ``[IPv6AcceptRA]``,
``[Bridge]``, whose ``VLANFiltering=`` follows from the ports' own
``[BridgeVLAN]`` — state nothing about the network and pass without a note.

``Description=`` is read by neither half: it is systemd's own label for a unit
("Bridge br0"), the emitter writes none, and treating one as the inventory's
``description`` would make every hand-written host disagree with its document
over a string nobody meant as one.

**Nothing here raises.** A capture is collected by whatever the operator had to
hand, and a drift run over a hundred hosts must not stop at the one that came
back truncated. A stream with no unit in it, a ``[Match]`` that matches by
property or by glob rather than by one name, a ``Kind=`` netviz has no
interface type for: each is a note and a skip.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final

from netviz.importer.config.common import fold_into, read_int, read_vlan_id
from netviz.importer.draft import Draft, DraftDevice, DraftInterface, DraftVlan, comment_text
from netviz.importer.names import interface_name

__all__ = ["read_networkd"]

#: ``Key=Value``, as read off one line.
_Assignment = tuple[str, str]
#: A ``[Section]`` and its assignments, in the order the file states them.
_Section = tuple[str, list[_Assignment]]
#: One unit: the section that named an interface, and everything under it.
_Unit = list[_Section]

#: The sections that begin a unit. Both name an interface — ``[NetDev]`` the one
#: it creates, ``[Match]`` the one it configures — and nothing else in either
#: format does.
_UNIT_SECTIONS: Final[tuple[str, ...]] = ("NetDev", "Match")

#: The netviz interface type of each ``[NetDev] Kind=``. The four tunnel kinds
#: are the ones :mod:`netviz.export.config.networkd` writes; everything else a
#: kernel can create — ``veth``, ``dummy``, ``vrf``, ``tap`` — is reported and
#: skipped rather than forced into a type that would misdescribe it.
_TYPE_BY_KIND: Final[dict[str, str]] = {
    "bridge": "bridge",
    "bond": "lag",
    "vlan": "vlan",
    "wireguard": "tunnel",
    "gre": "tunnel",
    "vxlan": "tunnel",
    "geneve": "tunnel",
}

#: Types that may state a lower link (``NG-I002``). ``[Network] VLAN=`` and
#: ``Tunnel=`` name a netdev of one of these and of nothing else.
_PARENT_TYPES: Final[frozenset[str]] = frozenset({"vlan", "tunnel"})

#: ``[Network]`` keys that name the aggregate a link is enslaved into, and what
#: each of them says that aggregate is. The key is a statement about the *other*
#: end: ``Bond=bond0`` on a port says bond0 exists and is a bond.
_AGGREGATE_KEYS: Final[dict[str, str]] = {"Bridge": "bridge", "Bond": "lag"}

#: ``ActivationPolicy=`` values that mean "configured, not brought up". The rest
#: — ``up``, ``always-up``, ``manual``, ``bound`` — agree with the schema's
#: default of enabled, so reading them would state what is already assumed.
_DOWN_POLICIES: Final[frozenset[str]] = frozenset({"down", "always-down"})

#: Sections that carry a statement no draft interface has a field for, and what
#: to say about each. The text names the section and never the interface, which
#: is what turns one occurrence per unit into one note per kind per source.
_DROPPED_SECTIONS: Final[dict[str, str]] = {
    "Route": (
        "a networkd '[Route]' section says where traffic leaves by; 'netviz import' writes "
        "interfaces and no 'spec.routes', so none was imported"
    ),
    "WireGuard": (
        "a '[WireGuard]' section holds a listening port and a private key; netviz models a "
        "tunnel as its own document naming both ends and stores no key material "
        "(docs/schema.md §14.2), so it was not imported"
    ),
    "WireGuardPeer": (
        "a '[WireGuardPeer]' section names a far end by its public key; netviz names both "
        "ends of a tunnel by element and interface (docs/schema.md §14), which a key does not "
        "give, so it was not imported"
    ),
    "Tunnel": (
        "a '[Tunnel]' section describes an encapsulation and its endpoints; netviz models a "
        "tunnel as its own document naming both ends (docs/schema.md §14), so it was not "
        "imported"
    ),
    "VXLAN": (
        "a '[VXLAN]'/'[GENEVE]' section carries the VNI and the underlay of an overlay; that "
        "belongs to a tunnel document (docs/schema.md §14) rather than to an interface, so it "
        "was not imported"
    ),
    "GENEVE": (
        "a '[VXLAN]'/'[GENEVE]' section carries the VNI and the underlay of an overlay; that "
        "belongs to a tunnel document (docs/schema.md §14) rather than to an interface, so it "
        "was not imported"
    ),
    "Bond": (
        "a '[Bond]' section states the bonding mode and its timers; the inventory does not "
        "record whether a LAG is LACP or a static mode, so none of it was imported"
    ),
}

#: The same, for keys of a section this reader otherwise reads.
_DROPPED_KEYS: Final[tuple[tuple[str, tuple[str, ...], str], ...]] = (
    (
        "Network",
        ("DNS", "Domains", "NTP"),
        "networkd 'DNS='/'Domains='/'NTP=' configure host name resolution and time; netviz "
        "describes links and the addresses on them, so they were not imported",
    ),
    (
        "Network",
        ("DHCP", "DHCPServer"),
        "networkd 'DHCP='/'DHCPServer=' say an address is leased rather than what it is; only "
        "the addresses a unit states are imported, so a link with nothing but DHCP has none "
        "here",
    ),
)


def read_networkd(text: str, *, source: str, host: str, draft: Draft) -> None:
    """Fold one capture of a host's systemd-networkd units into ``draft``.

    Args:
        text: Every unit file of the host, concatenated. A ``#`` banner is a
            systemd comment, so nothing needs stripping before it is parsed.
        source: Name of the input, for comments and the run report.
        host: Element name of the device the units were collected from. A unit
            names interfaces and never the host they are on, so the caller
            supplies it — from the banner of a generated file, or from
            ``--host``.
        draft: Accumulator, mutated in place.
    """
    fold_into(draft, host, source)
    stream = _Stream(source=source, draft=draft)
    for unit in _units(_sections(text)):
        _read_unit(unit, stream)
    if not stream.interfaces:
        draft.note(
            f"{source}: no '[NetDev]' or '[Match]' section in the capture names an interface, "
            "so nothing was imported"
        )
        return

    device = draft.device(host)
    _apply_stacking(stream)
    _add_interfaces(stream, device)
    _infer_parent_trunks(device)


# --------------------------------------------------------------------------- #
# The stream
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Stream:
    """One capture as it is being read, before anything reaches the device.

    The interfaces are held here rather than added to the device as they are
    read because whether a unit can be written at all depends on units that may
    come after it: a bridge needs the members its ports declare, and a VLAN
    needs the parent that claims it. Building the whole stream first means the
    ones that cannot be written are dropped before they touch a draft another
    input has already contributed to.
    """

    source: str
    draft: Draft
    #: Interfaces this capture declares, keyed and ordered by name.
    interfaces: dict[str, DraftInterface] = field(default_factory=dict)
    #: ``{"br0": ["eno1", "eno2"]}`` — the enslavement graph, read backwards off
    #: the members that state ``Bridge=``/``Bond=``.
    members: dict[str, list[str]] = field(default_factory=dict)
    #: ``{"bond0": "lag"}`` — what the *key* that named an aggregate says it is.
    #: ``Bond=`` names a bond and ``Bridge=`` a bridge, so a member's own unit
    #: states the aggregate's type as well as its membership. Keeping it is what
    #: lets a capture handed in one unit file at a time still read a bond as a
    #: bond: the ``.netdev`` that would otherwise be the only witness may be a
    #: different input, read after this one.
    member_kind: dict[str, str] = field(default_factory=dict)
    #: ``{"vlan10": "eno1"}`` — the lower link of a netdev, read off the
    #: ``.network`` of that lower link, which is the only unit that states it.
    parents: dict[str, str] = field(default_factory=dict)

    def add(self, interface: DraftInterface) -> None:
        """Fold ``interface`` in, merging a ``.netdev`` with its ``.network``."""
        existing = self.interfaces.get(interface.name)
        if existing is None:
            self.interfaces[interface.name] = interface
        else:
            existing.merge(interface)

    def note(self, message: str) -> None:
        self.draft.note(f"{self.source}: {message}")


def _sections(text: str) -> Iterator[_Section]:
    """Every ``[Section]`` of the capture, with its assignments in file order.

    Repeats are kept on both counts — ``Address=`` may legitimately appear four
    times, and a stream holds a ``[Network]`` per unit — because collapsing
    either would lose exactly the statements this reader is here for.

    A comment is a ``#`` or a ``;`` at the start of a line and nothing else:
    systemd reads everything after the ``=`` as the value, so a ``#`` further
    along a line is part of an address, not a comment about one.
    """
    name = ""
    body: list[_Assignment] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            if name:
                yield name, body
            name, body = line[1:-1].strip(), []
            continue
        key, separator, value = line.partition("=")
        if separator and name:
            body.append((key.strip(), value.strip()))
    if name:
        yield name, body


def _units(sections: Iterable[_Section]) -> Iterator[_Unit]:
    """Split the stream into units at every ``[NetDev]`` and ``[Match]``."""
    unit: _Unit = []
    for section in sections:
        if section[0] in _UNIT_SECTIONS and unit:
            yield unit
            unit = []
        unit.append(section)
    if unit:
        yield unit


def _values(unit: _Unit, sections: tuple[str, ...], key: str) -> list[str]:
    """Every value ``key`` takes in ``sections``, in the order the unit states them."""
    return [
        value
        for name, body in unit
        for assigned, value in body
        if name in sections and assigned == key and value
    ]


def _setting(unit: _Unit, sections: tuple[str, ...], key: str) -> str | None:
    """The value of a key that is stated once, or the last of several.

    Last rather than first because that is systemd's own rule for a scalar key:
    a later assignment in the same unit overrides an earlier one.
    """
    values = _values(unit, sections, key)
    return values[-1] if values else None


# --------------------------------------------------------------------------- #
# One unit
# --------------------------------------------------------------------------- #


def _read_unit(unit: _Unit, stream: _Stream) -> None:
    """One unit, as an interface of the capture — or as a note."""
    raw = _unit_name(unit, stream)
    if raw is None:
        return
    name, original = interface_name(raw)
    if name is None:
        stream.note(f"the interface named {raw!r} holds no usable characters, so it was skipped")
        return

    interface_type = _unit_type(unit, raw, stream)
    if interface_type is None:
        return

    interface = DraftInterface(name=name, type=interface_type)
    if original is not None:
        interface.comments.append(
            f"the interface is named {comment_text(original)!r} on the host; renamed here "
            "because a netviz interface name may only hold letters, digits, '.', '/' and '-'"
        )
    _apply_link(interface, unit, stream)
    _apply_addresses(interface, unit, stream)
    _apply_vlan(interface, unit, stream)
    _collect_stacking(unit, name, stream)
    _note_drops(unit, stream)
    stream.add(interface)


def _unit_name(unit: _Unit, stream: _Stream) -> str | None:
    """The interface a unit is about, or ``None`` when it is about no single one.

    ``[NetDev] Name=`` is the device the unit creates. ``[Match] Name=`` is the
    device it configures, and unlike the first it is a *pattern*: a glob, or
    several names separated by spaces, matches links this capture cannot
    enumerate. Attributing such a unit to one interface would put another
    interface's MTU on it, so it is reported and skipped.
    """
    if any(name == "NetDev" for name, _ in unit):
        created = _setting(unit, ("NetDev",), "Name")
        if created is None:
            stream.note(
                "a '[NetDev]' section states no 'Name=', so the device it creates has no name "
                "to record and the unit was skipped"
            )
        return created
    if not any(name == "Match" for name, _ in unit):
        stream.note(
            "the capture opens with sections that belong to no '[NetDev]' or '[Match]', so "
            "there is no interface to attribute them to and they were skipped"
        )
        return None
    matched = _setting(unit, ("Match",), "Name")
    if matched is None:
        stream.note(
            "a '[Match]' section selects links by property rather than by 'Name=', so the "
            "unit could not be attributed to one interface and was skipped"
        )
        return None
    if len(matched.split()) > 1 or any(character in matched for character in "*?["):
        stream.note(
            f"'[Match] Name={comment_text(matched)}' is a pattern rather than one interface "
            "name; the unit configures links this capture does not list, so it was skipped"
        )
        return None
    return matched


def _unit_type(unit: _Unit, raw: str, stream: _Stream) -> str | None:
    """The netviz type of a unit, or ``None`` when netviz has none for it.

    A unit with no ``Kind=`` is a ``.network``: it configures a device something
    else created, which for a host that is not building it is a NIC. Where the
    ``.netdev`` is in the same capture the two merge, and the kind it stated
    wins over this fallback — see
    :meth:`~netviz.importer.draft.DraftInterface.merge`.
    """
    kind = _setting(unit, ("NetDev",), "Kind")
    if kind is None:
        return "ethernet"
    interface_type = _TYPE_BY_KIND.get(kind)
    if interface_type is None:
        stream.note(
            f"{raw!r} is a {comment_text(kind)!r} netdev, which maps onto no netviz "
            "interface type, so it was not imported"
        )
    return interface_type


def _apply_link(interface: DraftInterface, unit: _Unit, stream: _Stream) -> None:
    """MTU, MAC and admin state, from whichever half of the unit states them.

    ``[NetDev]`` and ``[Link]`` take the same two keys and mean subtly different
    things by them — what the device is created with, and what it is set to
    afterwards — but an inventory states one MTU and one address, so both are
    read into the one field.
    """
    stated = _setting(unit, ("Link", "NetDev"), "MTUBytes")
    if stated is not None:
        # systemd accepts a K/M/G suffix here; the emitter writes a plain byte
        # count and the schema holds one, so a suffixed value is reported rather
        # than guessed at.
        mtu = read_int(stated, low=1)
        if mtu is not None:
            interface.mtu = mtu
        else:
            stream.note(
                f"'MTUBytes={comment_text(stated)}' on {interface.name!r} is not a plain byte "
                "count; netviz records an integer MTU, so it was not imported"
            )

    mac = _setting(unit, ("Link", "NetDev"), "MACAddress")
    if mac is not None and _looks_like_mac(mac):
        interface.mac = mac.lower()
    elif mac is not None:
        stream.note(
            f"'MACAddress={comment_text(mac)}' on {interface.name!r} is not a six-octet "
            "address, so it was not imported"
        )

    policy = _setting(unit, ("Link",), "ActivationPolicy")
    if policy is not None and policy.lower() in _DOWN_POLICIES:
        interface.enabled = False


def _apply_addresses(interface: DraftInterface, unit: _Unit, stream: _Stream) -> None:
    """``Address=``, from ``[Network]`` and from the ``[Address]`` sections alike.

    The two spellings are one statement: ``[Address]`` exists so that an address
    can carry options, and the address itself is the same key with the same
    meaning.
    """
    dropped = False
    for value in _values(unit, ("Network", "Address"), "Address"):
        cidr = value.split()[0]
        if _is_transient(cidr):
            dropped = True
            continue
        # Nothing in the unit states a family, so the colon does: it is legal in
        # an IPv6 literal and in nothing else.
        target = interface.ipv6 if ":" in cidr else interface.ipv4
        if cidr not in target:
            target.append(cidr)
    if dropped:
        stream.note(
            "dropped link-local ('fe80::') and loopback ('127.') addresses; those are true of "
            "every host and say nothing about this one"
        )


def _is_transient(cidr: str) -> bool:
    """Is this an address every host has, rather than one this host was given?"""
    return cidr.lower().startswith("fe80:") or cidr.startswith("127.")


def _apply_vlan(interface: DraftInterface, unit: _Unit, stream: _Stream) -> None:
    """The VLAN a netdev encapsulates, or the ones a bridge port carries.

    A ``vlan`` netdev is read from ``[VLAN] Id=`` and nothing else: its type
    requires an access block carrying the encapsulation (``NG-I002``), so a
    ``[BridgeVLAN]`` in the same unit — legal, if that sub-interface is itself a
    port of a filtering bridge — cannot be what the block says.
    """
    if interface.type == "vlan":
        stated = _setting(unit, ("VLAN",), "Id")
        vid = read_vlan_id(stated) if stated is not None else None
        if vid is not None:
            interface.vlan = DraftVlan(mode="access", access_vlan=vid)
        return
    interface.vlan = _bridge_vlan(unit, stream)


def _bridge_vlan(unit: _Unit, stream: _Stream) -> DraftVlan | None:
    """``[BridgeVLAN]`` as an access port or a trunk."""
    tagged: set[int] = set()
    for value in _values(unit, ("BridgeVLAN",), "VLAN"):
        tagged |= _vlan_ids(value, stream)
    stated_pvid = _setting(unit, ("BridgeVLAN",), "PVID")
    pvid = read_vlan_id(stated_pvid) if stated_pvid is not None else None

    if pvid is not None and tagged <= {pvid}:
        return DraftVlan(mode="access", access_vlan=pvid)
    if not tagged:
        return None
    if pvid is None:
        return DraftVlan(mode="trunk", trunk_vlans=sorted(tagged))
    # The port carries several VLANs and hands one of them out untagged. The
    # tagged set is exact; the untagged one has no field in a draft, so it is
    # written where a person will see it instead of being dropped.
    return DraftVlan(
        mode="trunk",
        trunk_vlans=sorted(tagged | {pvid}),
        comment=(
            f"the capture states 'PVID={pvid}' on this port, so VLAN {pvid} is its untagged "
            "one; netviz's import writes the tagged set only, so state the native VLAN by "
            "hand if the port really has one"
        ),
    )


def _vlan_ids(value: str, stream: _Stream) -> set[int]:
    """One ``VLAN=`` value: a single id, or systemd's inclusive ``100-200`` range.

    Both ends are bounded to the 802.1Q range before the range is expanded. A
    single mistyped character — ``VLAN=1-4000000000`` — would otherwise ask for
    four billion integers and take the reader out with a ``MemoryError``, which
    is the one thing a reader is documented never to do.
    """
    first, separator, last = value.partition("-")
    if not separator:
        single = read_vlan_id(first)
        return {single} if single is not None else _unreadable(value, stream)
    low, high = read_vlan_id(first), read_vlan_id(last)
    if low is None or high is None or high < low:
        return _unreadable(value, stream)
    return set(range(low, high + 1))


def _unreadable(value: str, stream: _Stream) -> set[int]:
    stream.note(
        f"'[BridgeVLAN] VLAN={comment_text(value)}' is neither a VLAN id nor a range of them, "
        "so it was not imported"
    )
    return set()


def _collect_stacking(unit: _Unit, name: str, stream: _Stream) -> None:
    """Remember what this unit says about links other than itself.

    Three keys, all of which state a relationship from the wrong end for
    netviz: ``Bridge=``/``Bond=`` name the aggregate *this* link is a member
    of, and ``VLAN=``/``Tunnel=`` name a netdev stacked *on* this link. They are
    only applied once the whole stream has been read, because the unit they are
    about may not have been.
    """
    for key, kind in _AGGREGATE_KEYS.items():
        for value in _values(unit, ("Network",), key):
            aggregate = interface_name(value)[0]
            if aggregate is None:
                continue
            stream.member_kind[aggregate] = kind
            if name not in stream.members.setdefault(aggregate, []):
                stream.members[aggregate].append(name)
    for key in ("VLAN", "Tunnel"):
        for value in _values(unit, ("Network",), key):
            child = interface_name(value)[0]
            if child is not None:
                stream.parents[child] = name


def _note_drops(unit: _Unit, stream: _Stream) -> None:
    """Report what the unit states and a draft interface cannot hold."""
    for name, _ in unit:
        reason = _DROPPED_SECTIONS.get(name)
        if reason is not None:
            stream.note(reason)
    for section, keys, reason in _DROPPED_KEYS:
        if any(_values(unit, (section,), key) for key in keys):
            stream.note(reason)


# --------------------------------------------------------------------------- #
# The whole stream
# --------------------------------------------------------------------------- #


def _apply_stacking(stream: _Stream) -> None:
    """Turn what the units said about each other into ``parent`` and ``members``."""
    for child, lower in stream.parents.items():
        interface = stream.interfaces.get(child)
        if interface is None:
            stream.note(
                f"a unit stacks {child!r} on {lower!r}, but no '[NetDev]' in the capture "
                f"creates {child!r}, so the relationship was not imported"
            )
        elif interface.type in _PARENT_TYPES:
            interface.parent = lower
        else:
            stream.note(
                f"a unit stacks {child!r} on {lower!r}, but {child!r} is a "
                f"{interface.type} interface, which netviz does not stack on a parent, so "
                "the relationship was not imported"
            )
    for aggregate, members in stream.members.items():
        interface = stream.interfaces.get(aggregate)
        if interface is not None:
            interface.members = list(members)


def _add_interfaces(stream: _Stream, device: DraftDevice) -> None:
    """Add every interface the models would accept, and say why the rest went.

    The two checks are the two shapes networkd can state and netviz cannot
    write: an aggregate nothing is enslaved into, and a VLAN sub-interface with
    no parent or no VID. Writing either would produce a document that fails to
    load, which is a worse answer than a line in the run report.
    """
    for interface in stream.interfaces.values():
        if interface.type in ("bridge", "lag") and not interface.members:
            stream.note(
                f"{interface.name!r} is a {interface.type} that no unit in the capture "
                "enslaves an interface into; netviz requires at least one member "
                "(NG-I003), so it was not imported"
            )
            device.note(
                f"{interface.name!r} was observed as a {interface.type} with no member port "
                "and is therefore not listed below; add its ports by hand"
            )
            continue
        if interface.type == "vlan" and (interface.parent is None or interface.vlan is None):
            stream.note(
                f"{interface.name!r} is a VLAN netdev, but no '.network' in the capture claims "
                "it with 'VLAN=' or it states no '[VLAN] Id='; both are required of a VLAN "
                "sub-interface (NG-I002), so it was not imported"
            )
            continue
        added = device.add_interface(interface)
        if added.vlan is not None:
            # Every VLAN a port names goes into 'spec.vlans', or a port
            # referencing one trips ``W113`` for want of a database entry.
            device.vlans |= set(added.vlan.trunk_vlans)
            if added.vlan.access_vlan is not None:
                device.vlans.add(added.vlan.access_vlan)

    for aggregate, members in stream.members.items():
        if aggregate in stream.interfaces:
            continue
        kind = stream.member_kind.get(aggregate, "ethernet")
        existing = device.interfaces.get(aggregate)
        if existing is None:
            # ``Bond=bond0`` on a port states two things: that bond0 exists and
            # that it is a bond. Both are the *port's* statement, so neither
            # needs the ``.netdev`` to have been in this input — which matters,
            # because an operator may hand netviz one unit file per argument
            # rather than the concatenation the reader is written for, and the
            # ``.netdev`` is then a different capture read later. Without this
            # the aggregate arrives as a plain ethernet port and the comparison
            # reports its type as drift.
            device.add_interface(
                DraftInterface(
                    name=aggregate,
                    type=kind,
                    members=sorted(members),
                    comments=[
                        f"inferred: no '[NetDev]' in {stream.source} creates {aggregate!r}; "
                        f"its type and membership come from the ports that name it"
                    ],
                )
            )
            continue
        if existing.type == "ethernet" and kind != "ethernet":
            existing.type = kind
        existing.members.extend(name for name in members if name not in existing.members)


def _infer_parent_trunks(device: DraftDevice) -> None:
    """Make every VLAN sub-interface's parent carry the VLAN it encapsulates.

    The one place this reader states something no unit did. It is not a guess: a
    sub-interface receives exactly the frames its parent tags with that VID, so a
    parent that did not carry it would make the sub-interface unreachable — and
    ``E009`` says so. A parent that already declared a ``[BridgeVLAN]`` trunk
    keeps its own list with the inferred ids folded in, because both are
    statements about the same port and the union is the only one true of both.
    """
    wanted: dict[str, set[int]] = {}
    for interface in device.interfaces.values():
        if interface.type == "vlan" and interface.parent and interface.vlan is not None:
            vid = interface.vlan.access_vlan
            if vid is not None:
                wanted.setdefault(interface.parent, set()).add(vid)

    for parent_name, vids in wanted.items():
        parent = device.interfaces.get(parent_name)
        if parent is None:
            continue
        children = ", ".join(
            sorted(
                entry.name
                for entry in device.interfaces.values()
                if entry.type == "vlan" and entry.parent == parent_name
            )
        )
        comment = (
            f"inferred: {children} encapsulate(s) VLAN(s) {_ids(vids)} on this port, so it "
            "must carry them tagged; a '.network' states a port's VLAN set only where the "
            "bridge filters, so this list is the minimum — extend it with the VLANs the port "
            "really carries"
        )
        if parent.vlan is None:
            parent.vlan = DraftVlan(mode="trunk", trunk_vlans=sorted(vids), comment=comment)
        elif parent.vlan.mode == "trunk":
            parent.vlan.trunk_vlans = sorted({*parent.vlan.trunk_vlans, *vids})
            parent.vlan.comment = parent.vlan.comment or comment
        device.vlans |= vids


def _ids(vids: set[int]) -> str:
    return ", ".join(str(vid) for vid in sorted(vids))


def _looks_like_mac(value: str) -> bool:
    """Is this the six-octet form? ``MACAddress=`` also takes a 20-octet one."""
    parts = value.split(":")
    return len(parts) == 6 and all(len(part) == 2 for part in parts)
