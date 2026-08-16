"""``--from interfaces``: netviz's own configuration grammar, read back.

The mirror of :mod:`netviz.export.config.neutral`, and the only pairing here
where both halves are netviz's: the emitter's docstring *defines* the grammar
this parses, and the round trip through the two is meant to be exact for
everything a :class:`~netviz.importer.draft.Draft` can hold. Which is the
interesting part, because a draft holds rather less than a device document does,
and the difference is the whole subject of this module.

The grammar is nine stanza kinds — ``device``, ``vlan``, ``netns``, ``vrf``,
``route-table``, ``interface``, ``route``, ``policy`` and ``tunnel`` — opening in
column 0, with ``lower-case-with-hyphens`` attributes indented under them, a
value running to the end of the line, and a repeated attribute meaning a list
(``member``, ``ipv4-address``). A ``route`` is one line with no body. Splitting is
:func:`~netviz.importer.config.common.stanzas`, which also strips comments;
free text that held a ``#`` therefore loses its tail, which is exactly why the
emitter never writes an inline comment of its own.

What the draft has a field for is read into it: a device's kind, vendor, model,
serial and description; a VLAN database; and per interface its type,
description, admin state, MAC, MTU, parent, members, VLAN block and the two
address lists. Four judgement calls decide the rest.

**A stated kind is not an inferred one.** The file says ``kind switch`` because a
device document said so, so :meth:`~netviz.importer.draft.DraftDevice.refine_kind`
is given no comment: the ``inferred:`` note a fresh draft device carries is for a
kind netviz reasoned its way to, and this one was read. A kind outside
:data:`~netviz.models.DEVICE_KINDS` is reported and ignored rather than written
through, because the only thing worse than not knowing what a box is, is a
document that will not load for saying it.

**The VLAN database is the union of everything.** Every id in a ``vlan-access``,
``vlan-native`` or ``vlan-tagged`` line lands in ``spec.vlans`` as well as in the
port's own block, along with every ``vlan`` stanza. A port referencing a VLAN the
device does not define trips ``W113``, and an import that produced that warning
from a file netviz itself wrote would be reporting on its own output.

**A trunk's VLAN set is expanded here.** ``vlan-tagged 10,20,100-110`` is what
:func:`~netviz.errors.compact_ids` writes, and this reads it back: ids and
inclusive ``low-high`` ranges, separated by commas, bounded to
``1``-``4094``. :class:`~netviz.models.scalars.VlanSet` accepts the same
spelling and rather more besides, but it reports a bad token by *raising*, and a
reader that raised would abandon a whole capture over one hand-edited line.
``all`` and ``none`` are therefore not read: the emitter never writes them, and
guessing that a token it did not write means the whole range is a guess about
4094 VLANs.

**Everything with no home is named once.** A draft describes what a capture
observed, in the vocabulary
:mod:`netviz.importer.emit` can write; it has no field for a gateway, a
per-family MTU, a forwarding switch, a VRF, a radio, a PSE, a static route, a
routing table, a policy rule or a tunnel. Each of those is reported once per
attribute — not once per interface, because a 48-port switch would otherwise fill
the run report with one identical line per port — and the five whole-stanza kinds
(``route``, ``vrf``, ``route-table``, ``policy``, ``tunnel``) are reported with
their contents named. That last part matters: a
drift check that silently ignored a device's entire routing table would tell an
operator that nothing had changed, which is a different statement from "netviz
did not look".

Nothing here raises. An unknown stanza, an unknown attribute, a malformed number
and a VLAN id out of range are all notes, and the rest of the file is read.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Final

from netviz.importer.config.common import fold_into, read_int, read_vlan_id, stanzas
from netviz.importer.draft import Draft, DraftDevice, DraftInterface, DraftVlan, comment_text
from netviz.importer.names import interface_name
from netviz.models import DEVICE_KINDS, InterfaceType, VlanMode
from netviz.models.scalars import MAX_VLAN_ID, MIN_VLAN_ID

__all__ = ["read_interfaces"]

#: ``device`` attributes that are fields of :class:`DraftDevice` under the same
#: name. ``element`` is not among them: it carries the fully-qualified name,
#: which the caller has already taken from the banner, and a draft is keyed by
#: the bare device name with nowhere to put a namespace.
_DEVICE_FIELDS: Final[tuple[str, ...]] = ("vendor", "model", "serial", "description")

#: The interface types the grammar may state, taken from the model rather than
#: restated so that a type added there is read here without a second edit.
_INTERFACE_TYPES: Final[frozenset[str]] = frozenset(entry.value for entry in InterfaceType)

#: The VLAN modes, likewise.
_VLAN_MODES: Final[frozenset[str]] = frozenset(entry.value for entry in VlanMode)

#: Attributes the grammar writes that a draft has no field for, each with what
#: it states. The wildcard entries stand for a family of attributes
#: (``wireless-role``, ``wireless-bss``, …) and are reported as the family, so
#: that a radio produces one line in the run report rather than six.
_UNMODELLED: Final[dict[str, str]] = {
    "forwarding-ipv4": "whether the device forwards IPv4 between its interfaces",
    "forwarding-ipv6": "whether the device forwards IPv6 between its interfaces",
    "bridge-type": "the kind of bridge the device is",
    "bridge-name": "the name of the device's bridge instance",
    "bridge-address": "the bridge's own management address",
    "vrf": "the routing instance the interface belongs to",
    "vlan-ingress-filtering": "whether the port drops frames of VLANs it is not in",
    "vlan-acceptable-frames": "which of tagged and untagged frames the port admits",
    "ipv4-enabled": "that IPv4 is switched off on the interface",
    "ipv6-enabled": "that IPv6 is switched off on the interface",
    "ipv4-gateway": "the interface's IPv4 default gateway",
    "ipv6-gateway": "the interface's IPv6 default gateway",
    "ipv4-mtu": "a per-family IPv4 MTU",
    "ipv6-mtu": "a per-family IPv6 MTU",
    "ipv4-forwarding": "that the interface forwards IPv4",
    "ipv6-forwarding": "that the interface forwards IPv6",
    "wireless-*": "the radio's configuration, down to its SSIDs",
    "poe-*": "the port's Power over Ethernet configuration",
    "tunnel": "the tunnel this interface is the local end of",
}

#: Attribute prefixes that :data:`_UNMODELLED` holds a wildcard entry for.
_WILDCARD_PREFIXES: Final[tuple[str, ...]] = ("wireless-", "poe-")


@dataclass(slots=True)
class _Pass:
    """One file being read, and the stanzas of it that are reported at the end.

    The three lists are collected rather than reported as they are met so that a
    file with forty routes produces one line naming them and not forty saying
    the same thing.
    """

    source: str
    draft: Draft
    device: DraftDevice
    #: The name the caller keyed this capture by, which the ``device`` stanza is
    #: checked against.
    host: str
    routes: list[str] = field(default_factory=list)
    vrfs: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    policy: list[str] = field(default_factory=list)
    tunnels: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        """Record ``message`` against this input, once however often it is said."""
        self.draft.note(f"{self.source}: {message}")


@dataclass(slots=True)
class _VlanLines:
    """The ``vlan-*`` attributes of one interface stanza, before they are a block.

    Collected first and turned into a :class:`DraftVlan` afterwards because the
    mode decides what the other three mean, and nothing in the grammar says the
    mode comes first.
    """

    mode: str | None = None
    access: int | None = None
    native: int | None = None
    tagged: list[int] = field(default_factory=list)

    @property
    def stated(self) -> bool:
        return self.mode is not None or self.access is not None or bool(self.tagged)


def read_interfaces(text: str, *, source: str, host: str, draft: Draft) -> None:
    """Fold one ``interfaces.conf`` into ``draft``.

    Args:
        text: The file, verbatim.
        source: Name of the input, for comments and the run report.
        host: Element name of the device the file describes. A generated file
            names it twice — in the banner and in the ``device`` stanza — but
            the caller has already chosen, and a capture renamed on the command
            line is renamed on purpose.
        draft: Accumulator, mutated in place.
    """
    fold_into(draft, host, source)
    state = _Pass(source=source, draft=draft, device=draft.device(host), host=host)
    for header, body in stanzas(text):
        _read_stanza(state, header, body)
    _note_stated(state)


def _read_stanza(state: _Pass, header: str, body: list[str]) -> None:
    """One stanza, dispatched on the keyword that opens it."""
    words = header.split()
    kind = words[0].lower()
    if kind == "device":
        _read_device(state, words, body)
    elif kind == "vlan":
        _read_vlan_stanza(state, words, body)
    elif kind == "interface":
        _read_interface(state, words, body)
    elif kind == "route":
        state.routes.append(" ".join(words[1:]) or "an unnamed prefix")
    elif kind == "netns":
        _read_netns_stanza(state, words, body)
    elif kind == "vrf":
        state.vrfs.append(_vrf_text(words, body))
    elif kind == "route-table":
        state.tables.append(_table_text(words, body))
    elif kind == "policy":
        state.policy.append(_policy_text(words, body))
    elif kind == "tunnel":
        state.tunnels.append(_tunnel_text(words))
    else:
        state.note(
            f"{kind!r} is not one of the nine stanza kinds this grammar has ('device', "
            "'vlan', 'netns', 'vrf', 'route-table', 'interface', 'route', 'policy', "
            "'tunnel'), so the stanza was not imported"
        )


# --------------------------------------------------------------------------- #
# device and vlan
# --------------------------------------------------------------------------- #


def _read_device(state: _Pass, words: list[str], body: list[str]) -> None:
    """The ``device`` stanza: what the box is, and who made it."""
    name = words[1] if len(words) > 1 else None
    if name is not None and name != state.host:
        state.note(
            f"the 'device' stanza names {name!r} and this capture was read as {state.host!r}; "
            "the name the caller gave wins, because that is what the draft is keyed by"
        )
    for line in body:
        attribute, value = _attribute(line)
        if not value:
            continue
        if attribute == "kind":
            _read_kind(state, value)
        elif attribute in _DEVICE_FIELDS:
            # First observation wins, as everywhere else in the draft: a second
            # capture of one host fills gaps rather than overwriting.
            if getattr(state.device, attribute) is None:
                setattr(state.device, attribute, value)
        elif attribute != "element":
            _note_attribute(state, attribute)


def _read_kind(state: _Pass, value: str) -> None:
    """``kind switch`` — stated by the file, so recorded without an inference note."""
    if value not in DEVICE_KINDS:
        state.note(
            f"the 'device' stanza states kind {comment_text(value)!r}, which is not one of "
            f"{', '.join(DEVICE_KINDS)}; the draft kept {state.device.kind!r}"
        )
        return
    state.device.refine_kind(value, None)


def _read_netns_stanza(state: _Pass, words: list[str], body: list[str]) -> None:
    """A ``netns`` stanza: one network namespace of the machine (§23.1).

    The parent is kept because it is the only thing that makes the nesting a
    tree, and a draft that dropped it would flatten a two-level container host
    into one level without saying so.
    """
    name = words[1] if len(words) > 1 else ""
    if not name:
        state.note("a 'netns' stanza names no namespace and was skipped")
        return
    parent = ""
    for line in body:
        attribute, value = _attribute(line)
        if attribute == "parent" and value:
            parent = value
        elif attribute not in ("parent", "description"):
            _note_attribute(state, attribute)
    state.device.netns[name] = parent or state.device.netns.get(name, "")


def _read_vlan_stanza(state: _Pass, words: list[str], body: list[str]) -> None:
    """A ``vlan`` stanza: an entry of the device's VLAN database."""
    vid = _vlan_id(state, words[1] if len(words) > 1 else "")
    if vid is not None:
        state.device.vlans.add(vid)
    if body:
        state.note(
            "a VLAN's name and description have no home in the draft 'netviz import' "
            "builds, which writes a database of ids only, so they were not imported"
        )


# --------------------------------------------------------------------------- #
# interface
# --------------------------------------------------------------------------- #


def _read_interface(state: _Pass, words: list[str], body: list[str]) -> None:
    """One ``interface`` stanza, as one entry of ``spec.interfaces``."""
    if len(words) < 2:
        state.note("an 'interface' stanza names no interface and was skipped")
        return
    name, original = interface_name(words[1])
    if name is None:
        state.note(f"the interface name {words[1]!r} holds no usable characters, so it was skipped")
        return

    interface = DraftInterface(name=name)
    if original is not None:
        interface.comments.append(
            f"the interface is named {comment_text(original)!r} in the file; renamed here "
            "because a netviz interface name may only hold letters, digits, '.', '/' and '-'"
        )
    vlan = _VlanLines()
    for line in body:
        _read_interface_line(state, interface, vlan, line)
    interface.vlan = _vlan_block(state, vlan)
    state.device.add_interface(interface)


def _read_interface_line(
    state: _Pass, interface: DraftInterface, vlan: _VlanLines, line: str
) -> None:
    """One indented attribute of an ``interface`` stanza."""
    attribute, value = _attribute(line)
    if not value:
        return
    if attribute == "type":
        _read_type(state, interface, value)
    elif attribute == "description":
        interface.description = interface.description or value
    elif attribute == "enabled":
        interface.enabled = _boolean(state, interface.name, value)
    elif attribute == "mac":
        # Hex, and case-insensitive; the model's canonical form is lower case.
        interface.mac = value.lower()
    elif attribute == "mtu":
        interface.mtu = _number(state, interface.name, attribute, value)
    elif attribute == "parent":
        _read_parent(state, interface, value)
    elif attribute == "member":
        _read_member(state, interface, value)
    elif attribute == "netns":
        interface.netns = interface.netns or value
        state.device.netns.setdefault(value, "")
    elif attribute == "peer":
        interface.peer = interface.peer or interface_name(value)[0]
    elif attribute == "vlan-mode":
        _read_vlan_mode(state, vlan, value)
    elif attribute == "vlan-access":
        vlan.access = _vlan_id(state, value)
        _define(state, vlan.access)
    elif attribute == "vlan-native":
        vlan.native = _vlan_id(state, value)
        _define(state, vlan.native)
    elif attribute == "vlan-tagged":
        vlan.tagged = _vlan_ids(state, value)
        for vid in vlan.tagged:
            _define(state, vid)
    elif attribute in ("ipv4-address", "ipv6-address"):
        _read_address(state, interface, attribute, value)
    else:
        _note_attribute(state, attribute)


def _read_type(state: _Pass, interface: DraftInterface, value: str) -> None:
    if value in _INTERFACE_TYPES:
        interface.type = value
        return
    state.note(
        f"{interface.name}: {comment_text(value)!r} is not a netviz interface type, so the "
        f"port was imported as {interface.type!r}"
    )


def _read_parent(state: _Pass, interface: DraftInterface, value: str) -> None:
    parent = interface_name(value)[0]
    if parent is None:
        state.note(f"{interface.name}: the parent name {value!r} holds no usable characters")
        return
    interface.parent = parent


def _read_address(state: _Pass, interface: DraftInterface, attribute: str, value: str) -> None:
    """``ipv4-address 192.168.10.20/24`` — one address, prefix length and all.

    The prefix is required rather than defaulted: ``ipaddress`` would read a
    bare address as a ``/32``, which is a statement about the subnet that the
    file did not make and that ``E010`` would then compare against every other
    address on the wire.
    """
    version = 4 if attribute.startswith("ipv4") else 6
    try:
        address = ipaddress.ip_interface(value) if "/" in value else None
    except ValueError:
        address = None
    if address is None or address.version != version:
        state.note(
            f"{interface.name}: {comment_text(value)!r} is not an IPv{version} address with a "
            "prefix length, so it was not imported"
        )
        return
    target = interface.ipv4 if version == 4 else interface.ipv6
    text = str(address)
    if text not in target:
        target.append(text)


def _read_member(state: _Pass, interface: DraftInterface, value: str) -> None:
    member = interface_name(value)[0]
    if member is None:
        state.note(f"{interface.name}: the member name {value!r} holds no usable characters")
    elif member not in interface.members:
        interface.members.append(member)


def _read_vlan_mode(state: _Pass, vlan: _VlanLines, value: str) -> None:
    if value in _VLAN_MODES:
        vlan.mode = value
        return
    state.note(
        f"'vlan-mode {comment_text(value)}' is neither 'access' nor 'trunk'; the mode was read "
        "from the VLANs the port states instead"
    )


def _vlan_block(state: _Pass, vlan: _VlanLines) -> DraftVlan | None:
    """The ``vlan:`` block of one port, or ``None`` when the file stated none.

    A stanza that names VLANs without naming a mode gets the mode its VLANs
    imply — tagged ids are a trunk, a single untagged id is an access port —
    which is a reading of what is there rather than an addition to it.
    """
    if not vlan.stated:
        return None
    mode = vlan.mode or ("trunk" if vlan.tagged else "access")
    if vlan.native is not None:
        state.note(
            "'vlan-native' states the VLAN a trunk carries untagged; the draft's VLAN block "
            "holds a mode, an access VLAN and a tagged set, so the native VLAN reached the "
            "device's VLAN database and nothing else"
        )
    if mode == "trunk":
        return DraftVlan(mode="trunk", trunk_vlans=sorted(set(vlan.tagged)))
    return DraftVlan(mode="access", access_vlan=vlan.access)


# --------------------------------------------------------------------------- #
# Stanzas with no home in a draft
# --------------------------------------------------------------------------- #


def _table_text(words: list[str], body: list[str]) -> str:
    """``uplink-b (table 100)`` — enough of a ``route-table`` stanza to name it by."""
    name = words[1] if len(words) > 1 else "an unnamed table"
    for line in body:
        attribute, value = _attribute(line)
        if attribute == "id" and value:
            return f"{name} (table {value})"
    return name


def _policy_text(words: list[str], body: list[str]) -> str:
    """``100 from 10.20.0.0/16 lookup uplink-b`` — one policy stanza, in one phrase.

    The selectors and the action are rebuilt from the body rather than summarised
    by priority alone: this text lands in a note saying the rules were *not*
    imported, and a note that named only the priorities would leave a reader with
    no way to tell which policy the capture had.
    """
    parts = [words[1] if len(words) > 1 else "an unnumbered rule"]
    action, target = "lookup", ""
    for line in body:
        attribute, value = _attribute(line)
        if attribute in ("from", "to", "iif", "oif", "fwmark", "dscp") and value:
            parts.append(f"{attribute} {value}")
        elif attribute == "action" and value:
            action = value
        elif attribute in ("table", "goto") and value:
            target = value
    return " ".join([*parts, action, target]).strip()


def _vrf_text(words: list[str], body: list[str]) -> str:
    """``blue (rd 65000:1)`` — enough of a VRF stanza to recognise it by."""
    name = words[1] if len(words) > 1 else "an unnamed VRF"
    for line in body:
        attribute, value = _attribute(line)
        if attribute == "rd" and value:
            return f"{name} (rd {value})"
    return name


def _tunnel_text(words: list[str]) -> str:
    """``wg0 (wireguard)`` — the name and encapsulation of a tunnel stanza."""
    name = words[1] if len(words) > 1 else "an unnamed tunnel"
    return f"{name} ({words[2]})" if len(words) > 2 else name


def _note_stated(state: _Pass) -> None:
    """Say what the file held that the draft cannot, naming it."""
    if state.routes:
        state.note(
            f"the file states {len(state.routes)} static route(s) "
            f"[{comment_text('; '.join(state.routes))}]; 'netviz import' builds devices, "
            "interfaces and cables, and has no routing table to put them in, so they were not "
            "imported — a drift check cannot compare this device's routes"
        )
    if state.vrfs:
        state.note(
            f"the file states {len(state.vrfs)} routing instance(s) "
            f"[{comment_text('; '.join(state.vrfs))}]; a draft has no VRF, so they were not "
            "imported and the interfaces bound to them read as though they were global"
        )
    if state.tables:
        state.note(
            f"the file states {len(state.tables)} routing table(s) "
            f"[{comment_text('; '.join(state.tables))}]; a draft has no routing table, so "
            "they were not imported"
        )
    if state.policy:
        state.note(
            f"the file states {len(state.policy)} policy rule(s) "
            f"[{comment_text('; '.join(state.policy))}]; a draft has no policy database, so "
            "they were not imported — a drift check cannot compare which table this device "
            "routes a packet by"
        )
    if state.tunnels:
        state.note(
            f"the file states {len(state.tunnels)} tunnel(s) "
            f"[{comment_text('; '.join(state.tunnels))}]; netviz models a tunnel as its own "
            "document naming both ends (docs/schema.md §14) and this file describes only this "
            "end, so they were not imported"
        )


def _note_attribute(state: _Pass, attribute: str) -> None:
    """Report an attribute with no field in the draft, once per attribute."""
    family = next(
        (f"{prefix}*" for prefix in _WILDCARD_PREFIXES if attribute.startswith(prefix)), attribute
    )
    stated = _UNMODELLED.get(family)
    if stated is None:
        state.note(
            f"{attribute!r} is not an attribute of the 'interfaces' grammar netviz writes, "
            "so it was not imported"
        )
        return
    state.note(
        f"{family!r} states {stated}; the draft 'netviz import' builds has no field for it, "
        "so it was not imported and a drift check cannot compare it"
    )


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def _attribute(line: str) -> tuple[str, str]:
    """``('vlan-tagged', '10,20')`` — one indented line, split at the attribute.

    Split on any run of whitespace rather than on a space, so that a file
    indented with tabs reads the same as the one the emitter writes.
    """
    parts = line.split(None, 1)
    if not parts:
        return ("", "")
    return (parts[0].lower(), parts[1].strip() if len(parts) > 1 else "")


def _boolean(state: _Pass, subject: str, value: str) -> bool | None:
    """``true``/``false``, or ``None`` — which is "not observed", not "false"."""
    if value in ("true", "false"):
        return value == "true"
    state.note(f"{subject}: {comment_text(value)!r} is not 'true' or 'false', so it was ignored")
    return None


def _number(state: _Pass, subject: str, attribute: str, value: str) -> int | None:
    number = read_int(value, low=0)
    if number is not None:
        return number
    state.note(f"{subject}: {attribute} {comment_text(value)!r} is not a number, so it was ignored")
    return None


def _vlan_id(state: _Pass, value: str) -> int | None:
    """One VLAN id, bounded to the range 802.1Q has."""
    vid = read_vlan_id(value)
    if vid is not None:
        return vid
    state.note(
        f"{comment_text(value)!r} is not a VLAN id between {MIN_VLAN_ID} and {MAX_VLAN_ID}, so "
        "it was not imported"
    )
    return None


def _vlan_ids(state: _Pass, value: str) -> list[int]:
    """``10,20,100-110`` as ``[10, 20, 100, ..., 110]``.

    The inverse of :func:`~netviz.errors.compact_ids`, which is what wrote the
    line: comma-separated ids and inclusive ``low-high`` ranges, and nothing
    else. Both ends of a range are bounded to ``1``-``4094`` before it is
    expanded, so a mistyped range costs a note rather than four billion ids.
    """
    found: set[int] = set()
    for token in value.split(","):
        text = token.strip()
        if not text:
            continue
        low, separator, high = text.partition("-")
        if not separator:
            vid = _vlan_id(state, text)
            if vid is not None:
                found.add(vid)
            continue
        first, last = _vlan_id(state, low.strip()), _vlan_id(state, high.strip())
        if first is None or last is None:
            continue
        if first > last:
            state.note(f"the VLAN range {text!r} is inverted, so it was not imported")
            continue
        found.update(range(first, last + 1))
    return sorted(found)


def _define(state: _Pass, vid: int | None) -> None:
    """Put ``vid`` in the device's VLAN database, where ``W113`` looks for it."""
    if vid is not None:
        state.device.vlans.add(vid)
