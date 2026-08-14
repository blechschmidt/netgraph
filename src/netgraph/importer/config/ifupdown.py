"""``--from ifupdown``: reading back a Debian host's ``/etc/network/interfaces``.

The mirror of :mod:`netgraph.export.config.ifupdown`, and the reader most likely
to be handed something netgraph did not write: an operator's file holds hooks,
aliases, the distribution's ``lo`` stanza, ``source`` lines and options from half
a dozen packages that each grew their own ifupdown integration. None of that is
an error here. What is understood is read, what is not is noted, and nothing
raises — a drift check that refused to read a real file would be a drift check
nobody ran twice.

The stanza splitter is :func:`~netgraph.importer.config.common.stanzas`, which is
ifupdown's own shape: a keyword in column 0 opens a stanza and every indented
line below belongs to it. Keywords are matched case-insensitively and with ``_``
folded to ``-``, because ``bridge_ports`` (bridge-utils) and ``bond-slaves``
(ifenslave) disagree about the separator and ifupdown2 spells several of these
the other way round; the two spellings are one keyword and never two.

Six judgement calls decide what the draft ends up holding.

**The absence of ``auto`` is not an administrative down state.** ``auto eno1``
means "bring this up at boot", and ifupdown has no keyword for the opposite; an
interface with an ``iface`` stanza and no ``auto`` line is one an operator brings
up by hand, or one ``ifplugd`` brings up, or one whose ``auto`` line lives in a
sourced file. So a named interface is ``enabled: true`` and an unnamed one is
*not observed* — ``None``, never ``False``. The emitter writes ``enabled: false``
as an absence and says so in a comment; the reader cannot read that comment back
without treating every hand-written stanza as a disabled port, and a false
``enabled: false`` in a drift report is worse than a silence.

**``inet`` and ``inet6`` are one interface.** ifupdown carries one address per
stanza, so a dual-stack port is at least two of them. They merge into a single
:class:`~netgraph.importer.draft.DraftInterface`, and which address list an
address lands in is decided by parsing the address itself rather than by the
stanza's family keyword: the two disagree only in a file that is already wrong,
and the address is the more trustworthy of the two.

**An alias is a label, not an interface.** ``iface eno1:0`` configures a second
address *of* ``eno1`` under a 2.x-era label. Importing it as an interface would
put a port in the inventory that no cable can reach and that ``ip link`` does not
show, so the label is dropped and the address is folded into ``eno1`` — the same
statement the emitter makes when it refuses to generate aliases.

**A netmask is never invented.** ``address`` is read as CIDR or as a bare address
completed by ``netmask``, which ifupdown accepts as a dotted quad or as a prefix
length. An address with neither is reported and dropped: a prefix netgraph made
up would be compared against the inventory as though the host had stated it, and
would be reported as drift on the strength of netgraph's own guess.

**A VLAN id is read off the interface name.** ``vlan-raw-device`` names the
parent and nothing names the tag: ifupdown's ``vlan`` hook takes it from the
digits the name ends in, which is why the emitter refuses to write a
sub-interface whose name does not end in its VID. The reader applies the same
rule, and marks the resulting VLAN block with a comment saying where the id came
from. A name that carries no id leaves the port in the draft as a plain
interface, with the stacking reported instead of stated: netgraph's model
requires a ``vlan`` interface to name both a parent and its tag, and half of
that pair is a document that does not load.

**Loopbacks are not imported.** ``lo`` terminates no cable, appears in no
topology and carries only host-scope addresses; netgraph's ``loopback`` type is
for a router loopback somebody declared on purpose, which the distribution's
``iface lo inet loopback`` is not. This is :mod:`netgraph.importer.iproute`'s
rule, for its reason. A bridge or bond that the file declares with no member
port (``bridge_ports none``, or a stanza whose ports are configured elsewhere)
is left out for the same kind of reason: ``NG-I003`` requires an aggregate to
list at least one member, and an empty one would be a tree that does not load.

One thing the format simply does not state is an interface *type*: a port is
ethernet unless something in its stanza implies otherwise, which is what
``bridge_ports``, ``bond-slaves``, ``vlan-raw-device`` and ``wpa-ssid`` are read
for. A radio the file does not configure as a station is therefore imported as
an ethernet port — the file says nothing about the difference, and inventing one
from a name that looks like ``wlp1s0`` would be netgraph guessing at hardware
from a string.

Everything else ifupdown can hold is reported rather than dropped in silence.
``gateway`` is a default route and a draft models interfaces; ``up``, ``down``
and their pre/post variants are shell commands, which is also how the emitter
writes ``spec.routes``, so a generated file's routes come back as a note rather
than as routes; ``mapping`` picks a logical stanza by running a script netgraph
cannot evaluate; and ``source``/``source-directory`` cannot be followed at all,
because netgraph reads the one file it was given and not a filesystem — that
last one is named path by path, since it means the capture is incomplete and
every absence in it is unsafe to read as drift.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Final

from netgraph.importer.config.common import fold_into, read_int, stanzas
from netgraph.importer.draft import Draft, DraftDevice, DraftInterface, DraftVlan, comment_text
from netgraph.importer.names import interface_name
from netgraph.models.scalars import MAX_VLAN_ID, MIN_VLAN_ID

__all__ = ["read_ifupdown"]

#: Header keywords that bring an interface up without an operator asking.
#: ``allow-hotplug`` waits for the kernel to see the device rather than for the
#: boot to reach ``ifup -a``, which is a difference in *when*, not in whether the
#: interface is meant to be running.
_START_KEYWORDS: Final[frozenset[str]] = frozenset({"auto", "allow-auto", "allow-hotplug"})

#: Stanza headers that pull in configuration from elsewhere in the filesystem.
_INCLUDE_KEYWORDS: Final[frozenset[str]] = frozenset({"source", "source-directory"})

#: Option keywords whose value is a shell command. Recognised only so that the
#: run report can say they were seen: a draft has nowhere to put a command, and
#: guessing at what one does to the network would be worse than saying nothing.
_HOOK_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"up", "down", "pre-up", "post-up", "pre-down", "post-down"}
)

#: Option keywords that make an interface a wireless station. Two spellings for
#: two supplicants: ``wpa-ssid`` is wpa_supplicant's and ``wireless-essid`` is
#: the older ``wireless-tools`` one.
_WIRELESS_KEYWORDS: Final[frozenset[str]] = frozenset({"wpa-ssid", "wireless-essid"})

#: The digits an interface name ends in, which is where ifupdown's ``vlan`` hook
#: reads a sub-interface's 802.1Q VID from. The pattern
#: :mod:`netgraph.export.config.ifupdown` checks against, so the round trip of a
#: generated ``eno1.10`` is exact.
_TRAILING_DIGITS: Final[re.Pattern[str]] = re.compile(r"([0-9]+)$")

#: What ``bridge_ports`` and ``bond-slaves`` are given when the aggregate is
#: meant to start empty. It is a keyword, not a port, and an interface called
#: ``none`` would be invented out of nothing.
_NO_MEMBERS: Final = "none"

#: The families ``iface`` may declare that netgraph models. ifupdown also knows
#: ``ipx`` and ``can``, which carry no IP configuration and no topology.
_FAMILIES: Final[frozenset[str]] = frozenset({"inet", "inet6"})


@dataclass(slots=True)
class _Pass:
    """One file being read: where it came from, and what it has produced so far.

    The interfaces are held here rather than added to the device as they are
    parsed because admin state is not knowable until the file has been read to
    the end — an ``auto`` line may follow the stanza it names, and usually does
    not, but nothing in the format says it cannot.
    """

    source: str
    draft: Draft
    device: DraftDevice
    #: Interfaces by sanitised name, in the order the file first mentions them.
    interfaces: dict[str, DraftInterface] = field(default_factory=dict)
    #: Sanitised names an ``auto``/``allow-hotplug`` line brings up.
    started: set[str] = field(default_factory=set)
    #: Names whose stanza was read and deliberately left out, so that the report
    #: of what ``auto`` names without a stanza does not list the loopback this
    #: reader dropped itself.
    skipped: set[str] = field(default_factory=set)
    #: Option keywords the draft has no field for, named together at the end so
    #: that one file produces one line about them rather than one per port.
    unread: set[str] = field(default_factory=set)

    def note(self, message: str) -> None:
        """Record ``message`` against this input, once however often it is said."""
        self.draft.note(f"{self.source}: {message}")

    def interface(self, name: str) -> DraftInterface:
        """The interface called ``name``, created if this is its first stanza."""
        existing = self.interfaces.get(name)
        if existing is None:
            existing = self.interfaces[name] = DraftInterface(name=name)
        return existing


def read_ifupdown(text: str, *, source: str, host: str, draft: Draft) -> None:
    """Fold one ``/etc/network/interfaces`` into ``draft``.

    Args:
        text: The file, verbatim.
        source: Name of the input, for comments and the run report.
        host: Element name of the device the file came from. ifupdown never
            names the host it configures, so the caller supplies it — from the
            generated banner, or from ``--host``.
        draft: Accumulator, mutated in place.
    """
    fold_into(draft, host, source)
    state = _Pass(source=source, draft=draft, device=draft.device(host))
    for header, body in stanzas(text):
        _read_stanza(state, header, body)
    _finish(state)


def _read_stanza(state: _Pass, header: str, body: list[str]) -> None:
    """One stanza, dispatched on the keyword that opens it."""
    words = header.split()
    keyword = _keyword(words[0])
    if keyword in _START_KEYWORDS:
        state.started.update(
            name for word in words[1:] if (name := interface_name(_base(word))[0]) is not None
        )
    elif keyword == "iface":
        _read_iface(state, words, body)
    elif keyword in _INCLUDE_KEYWORDS:
        _note_include(state, keyword, words[1:])
    elif keyword == "mapping":
        state.note(
            "a 'mapping' stanza chooses which logical 'iface' a port uses by running a "
            "script at boot; netgraph cannot evaluate one, so it was not imported"
        )
    else:
        state.note(
            f"{keyword!r} opens a stanza netgraph does not read; only 'auto', "
            "'allow-hotplug', 'iface', 'mapping' and 'source' mean anything to this reader"
        )


def _read_iface(state: _Pass, words: list[str], body: list[str]) -> None:
    """An ``iface NAME FAMILY METHOD`` stanza and the options indented under it."""
    if len(words) < 2:
        state.note("an 'iface' stanza names no interface and was skipped")
        return
    raw, family, method = words[1], _word(words, 2), _word(words, 3)
    base = _base(raw)

    if base == "lo" or method == "loopback":
        state.skipped.add(interface_name(base)[0] or base)
        state.note(
            f"{raw!r} is the host loopback; it terminates no cable and holds only host-scope "
            "addresses, so it was not imported"
        )
        return
    if family is not None and family not in _FAMILIES:
        state.skipped.add(interface_name(base)[0] or base)
        state.note(
            f"{raw!r} is configured for the {family!r} address family, which netgraph does "
            "not model, so the stanza was not imported"
        )
        return

    name, original = interface_name(base)
    if name is None:
        state.note(f"the interface name {raw!r} holds no usable characters, so it was skipped")
        return
    if base != raw:
        state.note(
            f"{raw!r} is an ifupdown alias — a label on an address of {base!r} rather than a "
            "second interface — so the address was folded into that interface and the label "
            "was dropped"
        )

    interface = state.interface(name)
    if original is not None:
        _comment(
            interface,
            f"the interface is named {comment_text(original)!r} in the file; renamed here "
            "because a netgraph interface name may only hold letters, digits, '.', '/' and '-'",
        )
    if method == "dhcp":
        _comment(
            interface,
            "the stanza configures this interface by DHCP, so the file states no address for "
            "it; whatever it holds at runtime was not observed here",
        )
    _read_options(state, interface, body)


def _read_options(state: _Pass, interface: DraftInterface, body: list[str]) -> None:
    """The indented lines of one ``iface`` stanza."""
    addresses: list[str] = []
    netmask: str | None = None

    for line in body:
        keyword, value = _option(line)
        if keyword == "address":
            if value:
                addresses.append(value)
        elif keyword == "netmask":
            netmask = value or netmask
        elif keyword == "mtu":
            _read_mtu(state, interface, value)
        elif keyword == "hwaddress":
            _read_hwaddress(state, interface, value)
        elif keyword in ("bridge-ports", "bond-slaves"):
            _read_members(
                state, interface, value, kind="bridge" if keyword == "bridge-ports" else "lag"
            )
        elif keyword == "vlan-raw-device":
            _read_vlan(state, interface, value)
        elif keyword in _WIRELESS_KEYWORDS:
            interface.type = "wifi"
        elif keyword == "gateway":
            state.note(
                "'gateway' states a default route; 'netgraph import' writes interfaces and "
                "cables, and a device document's routing table is not part of the draft it "
                "builds, so the gateway was not imported"
            )
        elif keyword in _HOOK_KEYWORDS:
            state.note(
                "'up', 'down' and their pre/post variants run shell commands — which is also "
                "how a generated file carries 'spec.routes' — and a draft has nowhere to put "
                "a command, so the hooks were read and not imported"
            )
        elif keyword:
            state.unread.add(keyword)

    _read_addresses(state, interface, addresses, netmask)


def _read_addresses(
    state: _Pass, interface: DraftInterface, values: list[str], netmask: str | None
) -> None:
    """``address``/``netmask`` as ``a.b.c.d/len``, in the order the file states them."""
    for value in values:
        literal, _, stated = value.partition("/")
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            state.note(
                f"{interface.name}: {comment_text(value)!r} is not an IP address, so it was "
                "not imported"
            )
            continue
        prefix = _prefix_length(stated or netmask, version=address.version)
        if prefix is None:
            state.note(
                f"{interface.name}: the stanza states the address {address} with no usable "
                "netmask; netgraph does not invent a prefix length, because one it made up "
                "would be reported as drift against the inventory, so the address was dropped"
            )
            continue
        target = interface.ipv4 if address.version == 4 else interface.ipv6
        cidr = f"{address}/{prefix}"
        if cidr not in target:
            target.append(cidr)


def _prefix_length(mask: str | None, *, version: int) -> int | None:
    """``255.255.255.0`` or ``24`` as ``24``, or ``None`` when it is neither.

    ifupdown accepts both spellings for IPv4 and only the prefix length for
    IPv6, which is what :class:`ipaddress.IPv4Network` is doing here: it is the
    one place in the standard library that knows a dotted quad is a mask.
    """
    if not mask:
        return None
    width = 32 if version == 4 else 128
    length = read_int(mask, low=0, high=width)
    if length is not None:
        return length
    if mask.isdigit():
        # A prefix length out of range, rather than a dotted quad. Refusing it
        # here keeps the netmask branch below from reading '/64' as a mask.
        return None
    if version != 4:
        return None
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except ValueError:
        return None


def _read_mtu(state: _Pass, interface: DraftInterface, value: str) -> None:
    mtu = read_int(value, low=1)
    if mtu is not None:
        interface.mtu = mtu
        return
    state.note(f"{interface.name}: the MTU {comment_text(value)!r} is not a number")


def _read_hwaddress(state: _Pass, interface: DraftInterface, value: str) -> None:
    """``hwaddress ether aa:bb:cc:dd:ee:ff`` — the link-layer address to set.

    The class word is optional in the file and is not always ``ether``:
    ``hwaddress infiniband`` carries a 20-octet address that is not a MAC and
    that ``if:mac`` cannot hold, which is what the shape check below rejects.
    """
    candidate = value.split()[-1] if value.split() else ""
    parts = candidate.split(":")
    if len(parts) == 6 and all(len(part) == 2 for part in parts):
        interface.mac = candidate.lower()
        return
    state.note(
        f"{interface.name}: {comment_text(value)!r} is not a six-octet MAC address, so no "
        "'mac' was imported for this interface"
    )


def _read_members(state: _Pass, interface: DraftInterface, value: str, *, kind: str) -> None:
    """``bridge_ports`` / ``bond-slaves`` — the ports an aggregate is built from."""
    interface.type = kind
    for word in value.split():
        # ``bridge_ports none`` is bridge-utils' way of declaring an aggregate
        # that starts empty. It is a keyword rather than a port, and an
        # interface called 'none' would be one netgraph invented.
        if word == _NO_MEMBERS:
            continue
        member = interface_name(word)[0]
        if member is None:
            state.note(f"{interface.name}: the member name {word!r} holds no usable characters")
        elif member not in interface.members:
            interface.members.append(member)


def _read_vlan(state: _Pass, interface: DraftInterface, value: str) -> None:
    """``vlan-raw-device`` — the parent port, with the VID read off the name."""
    parent = interface_name(value)[0] if value else None
    if parent is None:
        state.note(
            f"{interface.name}: 'vlan-raw-device' names no usable parent interface, so no "
            "parent was imported"
        )
        return

    vid = _vid_of(interface.name)
    if vid is None:
        state.note(
            f"{interface.name} is a VLAN sub-interface of {parent}, but its name does not end "
            f"in a VLAN id between {MIN_VLAN_ID} and {MAX_VLAN_ID}; ifupdown's vlan hook reads "
            "the 802.1Q tag off the name and states it nowhere else, so the port was imported "
            "as a plain interface and the stacking was left out"
        )
        return

    interface.type = "vlan"
    interface.parent = interface.parent or parent
    if interface.vlan is None:
        interface.vlan = DraftVlan(
            mode="access",
            access_vlan=vid,
            comment=(
                f"the 802.1Q id is not stated in the file: ifupdown's vlan hook reads it off "
                f"the digits the interface name ends in, and netgraph read {vid} the same way"
            ),
        )
    state.device.vlans.add(vid)


def _vid_of(name: str) -> int | None:
    """The VLAN id ``name`` ends in, if it is one ifupdown could tag with."""
    match = _TRAILING_DIGITS.search(name)
    return None if match is None else read_int(match.group(1), low=MIN_VLAN_ID, high=MAX_VLAN_ID)


def _note_include(state: _Pass, keyword: str, arguments: list[str]) -> None:
    """A ``source`` line: the reason this capture is smaller than the host's file."""
    target = comment_text(" ".join(arguments)) if arguments else "an unnamed path"
    state.note(
        f"the file pulls in {target!r} with '{keyword}'; netgraph reads the file it was given "
        "and not the filesystem around it, so this capture is incomplete — anything configured "
        "there is missing here, and its absence is not drift"
    )


def _finish(state: _Pass) -> None:
    """Apply admin state, hand the interfaces over, and say what was left."""
    for name, interface in state.interfaces.items():
        if name in state.started:
            interface.enabled = True
        if interface.type in ("bridge", "lag") and not interface.members:
            state.note(
                f"{name} is declared as a {interface.type} with no member port; netgraph "
                "requires at least one (NG-I003), so it was not imported"
            )
            state.device.note(
                f"{name} was observed as a {interface.type} with no members and is therefore "
                "not listed below; add its ports, or remove it"
            )
            continue
        state.device.add_interface(interface)

    orphans = sorted(state.started - set(state.interfaces) - state.skipped)
    if orphans:
        state.note(
            f"'auto'/'allow-hotplug' names {comment_text(', '.join(orphans))}, which the file "
            "has no 'iface' stanza for; ifupdown takes those from a sourced file, so they were "
            "not imported"
        )
    if state.unread:
        state.note(
            f"these options have no field in a device document and were not imported: "
            f"{comment_text(', '.join(sorted(state.unread)))}"
        )


def _keyword(word: str) -> str:
    """A keyword in its one canonical spelling: lower case, hyphens throughout."""
    return word.lower().replace("_", "-")


def _option(line: str) -> tuple[str, str]:
    """``('bridge-ports', 'eno1 eno2')`` — one indented line, split at the keyword.

    Split on any run of whitespace rather than on a space: an operator's file is
    as likely to be indented and separated with tabs as with spaces.
    """
    parts = line.split(None, 1)
    if not parts:
        return ("", "")
    return (_keyword(parts[0]), parts[1].strip() if len(parts) > 1 else "")


def _word(words: list[str], index: int) -> str | None:
    """``words[index]`` folded to lower case, or ``None`` if the header is shorter."""
    return words[index].lower() if index < len(words) else None


def _base(name: str) -> str:
    """``eno1:0`` as ``eno1`` — an ifupdown alias labels an interface, it is not one."""
    return name.partition(":")[0]


def _comment(interface: DraftInterface, text: str) -> None:
    """Add ``text`` to an interface once, however many stanzas it is deduced from."""
    if text not in interface.comments:
        interface.comments.append(text)
