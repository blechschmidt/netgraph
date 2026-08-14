"""``--from wireguard``: a wg-quick ``.conf``, or what ``wg showconf wg0`` prints.

Both are the same INI-ish grammar -- an ``[Interface]`` section and a ``[Peer]``
section per far end, ``Key = Value`` inside each -- and both describe *one
tunnel* rather than one host. That is the mirror image of
:mod:`netgraph.export.config.wireguard`, which writes one file per WireGuard
tunnel with an end on the device, and it is the fact every judgement call below
follows from.

**The interface's name is not in the file.** ``wg-quick up wg0`` reads
``/etc/wireguard/wg0.conf``; the name is the file's, and inside it there is
nothing that repeats it. Four candidates were considered and three rejected:

* The ``netgraph-element`` banner names the *device* this configuration belongs
  to, not the interface, so it cannot answer the question.
* The provenance comment the emitter writes above ``[Interface]`` names the
  *tunnel document* (``sites/hq/tun-hq-branch``), and the one above each
  ``[Peer]`` names the far end's element and interface. Neither is this end's
  interface name, and a comment is not a directive in any case -- an operator
  who copied the file elsewhere keeps the comment and changes the name.
* The file name, which is the one place wg-quick itself looks. This is what is
  used: the input's last path segment, its ``.conf`` suffix removed, and then
  the last dotted segment of what is left -- so ``wg0.conf`` and
  ``rtr-hq.wg0.conf`` both give ``wg0``, which is what a capture named after the
  host it came from should mean. The candidate is accepted only if it matches
  wg-quick's own test for a usable interface name (``[a-zA-Z0-9_=+.-]{1,15}``),
  which is what makes ``rtr-hq.wg0.conf`` safe to read and a hostname pasted
  into a filename mostly not.
* Nothing. An input that is not ``<name>.conf`` -- ``-`` for standard input, or
  a capture called ``rtr-hq-tunnels.txt`` -- yields no name, and then this
  reader adds *no interface* and says why. Naming it ``wg0`` because that is the
  usual name would invent a port, and an invented port is a cable end, a drift
  report and a document that nobody can trace back to a device.

The rule has one blind spot worth stating: ``wg0.100.conf`` gives ``100``, and if
the interface really is called ``wg0.100`` that is wrong. A file name with more
than one dotted segment therefore leaves a comment on the interface naming the
file it came from, so the mistake is visible in the generated document rather
than only here.

**The interface is a ``tunnel`` and nothing else is imported.** A WireGuard
netdev is exactly netgraph's ``tunnel`` type, so unlike
:mod:`netgraph.importer.config.frr` there is no fallback involved. What the file
holds beyond an address and an MTU has no home in a draft interface, and the
peers are the important case: netgraph models a tunnel as its own document naming
*both* ends (``docs/schema.md`` §14), and a wg-quick file names the far end only
by a public key -- which the inventory deliberately does not hold (§14.2), so
there is nothing to match it against. The peers are therefore counted and
reported, with their endpoints, and the tunnel document has to be written by
hand. Half a tunnel document, with one end guessed, would be worse than none.

**Key material is never read.** ``PrivateKey`` and ``PresharedKey`` are counted
and never quoted: their values do not reach a note, a comment, a description or a
document. This is stricter than it needs to be for the generated file, whose keys
are placeholders, and exactly as strict as it needs to be for a real one somebody
piped in.

**``AllowedIPs`` is not this interface's addresses.** It states which addresses
reach the peer, which is the far end's business; importing it here would put
another device's addresses on the local port.

**An address with no prefix length becomes a host route.** ``Address = 10.9.0.2``
is legal in a wg-quick file, and wg-quick hands it to ``ip address add``, which
makes it a ``/32`` (a ``/128`` for IPv6). Writing that is transcription of what
the tool does rather than a guess about intent, and the interface carries a
comment saying so.

**Silence means nothing.** This file describes one interface of a host and is
mute about every other one, about neighbours and about membership -- by design,
not by omission. So an interface in the inventory and not here is not drift, and
:mod:`netgraph.drift.coverage` grants this dialect no capability at all.

Nothing here raises: an unreadable line is counted and the next one is read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from netgraph.importer.config.common import fold_into, read_int
from netgraph.importer.draft import Draft, DraftInterface, comment_text
from netgraph.importer.names import interface_name

__all__ = ["read_wireguard"]

#: The suffix wg-quick requires, matched case-insensitively.
_SUFFIX: Final = ".conf"

#: wg-quick's own test for an interface name it will accept, from its
#: ``[[ $INTERFACE =~ ... ]]`` guard. Reused rather than re-invented: a name this
#: rejects is a name no wg-quick file was ever called, which is what makes the
#: file-name rule conservative instead of merely convenient.
_WG_NAME: Final = re.compile(r"^[A-Za-z0-9_=+.-]{1,15}$")

#: The shape an ``Address`` value has to have before it is written into a
#: document: digits, dots and colons, with an optional prefix length. Deliberately
#: not a parser -- the draft holds strings and the schema is what validates
#: them -- but enough to keep a word out of ``interfaces[].addresses``.
_ADDRESS: Final = re.compile(r"^[0-9A-Fa-f.:]+(?:/\d{1,3})?$")

#: The two section headers the format has, lowercased for comparison.
_INTERFACE_SECTION: Final = "interface"
_PEER_SECTION: Final = "peer"

#: Shared by the four wg-quick hooks, which differ only in when they run.
_HOOK_REASON: Final = (
    "a wg-quick hook is a shell command run around the interface; an inventory describes the "
    "network, not the commands that build it"
)

#: Every key this reader recognises and cannot keep, with the reason it cannot,
#: in the order the report states them. ``Endpoint`` is absent because it is
#: reported with its values; ``Address`` and ``MTU`` because they are imported.
#:
#: A key that is here is a key netgraph understands and has decided about, which
#: is a different statement from the one made about a key that is not.
_KEY_NOTES: Final[dict[str, str]] = {
    "PublicKey": (
        "wg-quick identifies a peer by its public key and netgraph identifies one by element "
        "and interface; nothing in this file connects the two, and the inventory holds no key "
        "to match against (docs/schema.md section 14.2)"
    ),
    "AllowedIPs": (
        "those addresses are the peer's, not this interface's, so importing them here would "
        "put the far end's addresses on the local port"
    ),
    "PersistentKeepalive": (
        "a keepalive changes what the tunnel does on the wire and no netgraph field states an "
        "interval"
    ),
    "ListenPort": (
        "the listen port belongs to the tunnel document ('spec.port'), which describes both "
        "ends; a draft interface has no port"
    ),
    "PrivateKey": (
        "netgraph stores no key material (docs/schema.md section 14.2); the value was not read "
        "and appears in no note, comment or document"
    ),
    "PresharedKey": (
        "the same -- a pre-shared symmetric key is key material, so its value was not read either"
    ),
    "FwMark": "the fwmark is host packet marking, which an inventory does not describe",
    "Table": (
        "'Table' selects which routing table wg-quick installs 'AllowedIPs' into; an import "
        "draft holds no route and no table"
    ),
    "DNS": (
        "the resolvers wg-quick sets while the tunnel is up are host configuration, not a "
        "property of the link"
    ),
    "SaveConfig": "'SaveConfig' is wg-quick's own behaviour and says nothing about the network",
    "PreUp": _HOOK_REASON,
    "PostUp": _HOOK_REASON,
    "PreDown": _HOOK_REASON,
    "PostDown": _HOOK_REASON,
}

#: Case-insensitive lookup into :data:`_KEY_NOTES`. Real files spell keys as
#: wg-quick's manual does, but ``wg setconf`` compares them case-insensitively
#: and so does this reader.
_KEYS_BY_LOWER: Final[dict[str, str]] = {key.lower(): key for key in _KEY_NOTES}


@dataclass(slots=True)
class _Seen:
    """What the file held that a draft interface has no field for, counted."""

    #: Recognised-but-unimportable key to how often it appeared. Values are
    #: never stored here -- only key names -- which is what keeps a private key
    #: out of the report by construction rather than by care.
    keys: dict[str, int] = field(default_factory=dict)
    peers: int = 0
    interfaces: int = 0
    endpoints: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    bad_address: list[str] = field(default_factory=list)
    bad_mtu: list[str] = field(default_factory=list)
    stray: int = 0


def read_wireguard(text: str, *, source: str, host: str, draft: Draft) -> None:
    """Fold one wg-quick file -- or one ``wg showconf`` -- into ``draft``.

    Args:
        text: The file, or the command's output.
        source: Name of the input. Unusually, this is read rather than only
            reported: it is the only place the interface's name can come from.
            See the module docstring.
        host: Element name of the device the tunnel terminates on. The file
            names neither end, so the caller supplies this one and the other is
            not knowable from here.
        draft: Accumulator, mutated in place.
    """
    fold_into(draft, host, source)

    interface = _interface_of(source, draft=draft)
    if interface is None:
        return

    seen = _Seen()
    section = ""
    for raw in text.splitlines():
        line = _uncommented(raw)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = _open_section(line, seen)
            continue
        key, separator, value = line.partition("=")
        if not separator:
            seen.stray += 1
            continue
        _read_key(key.strip(), value.strip(), section=section, interface=interface, seen=seen)

    draft.device(host).add_interface(interface)
    _report(seen, source=source, draft=draft)


def _uncommented(raw: str) -> str:
    """One line with its comment removed, or ``""`` when it is all comment.

    ``#`` at the start of a line is wg's comment marker and ``;`` is the INI
    convention, which operators use and wg-quick tolerates. An inline ``#`` after
    whitespace is stripped too, because that is what netgraph's own emitter
    writes -- ``PrivateKey = REPLACE-ME  # private key of rtr-hq`` -- and a
    reader that kept it would read back a value the emitter did not write.
    """
    line = raw.strip()
    if line.startswith(("#", ";")):
        return ""
    body, marker, _ = line.partition("#")
    return body.rstrip() if marker and body[-1:].isspace() else line


def _open_section(line: str, seen: _Seen) -> str:
    """Enter ``[Interface]`` or ``[Peer]``, counting what was entered."""
    name = line[1:-1].strip().lower()
    if name == _PEER_SECTION:
        seen.peers += 1
    elif name == _INTERFACE_SECTION:
        seen.interfaces += 1
    else:
        _append_unique(seen.sections, line)
    return name


def _read_key(
    key: str, value: str, *, section: str, interface: DraftInterface, seen: _Seen
) -> None:
    """One ``Key = Value`` line, in the section it appeared in."""
    canonical = _KEYS_BY_LOWER.get(key.lower())
    if canonical is not None:
        seen.keys[canonical] = seen.keys.get(canonical, 0) + 1
        return
    if key.lower() == "endpoint":
        # Reported with its value: a socket is not key material, and where the
        # far end answers is the one thing in a [Peer] section that helps
        # somebody write the tunnel document this file cannot become.
        _append_unique(seen.endpoints, value)
        return
    if section != _INTERFACE_SECTION:
        # ``Address`` and ``MTU`` mean the local interface; under a ``[Peer]``
        # they are not that interface's, and outside any section they belong to
        # nothing at all.
        _append_unique(seen.unknown, key)
        return
    if key.lower() == "address":
        _read_addresses(value, interface=interface, seen=seen)
    elif key.lower() == "mtu":
        _read_mtu(value, interface=interface, seen=seen)
    else:
        _append_unique(seen.unknown, key)


def _read_addresses(value: str, *, interface: DraftInterface, seen: _Seen) -> None:
    """``Address = 10.9.0.2/24, fd00::2/64`` -- IPv4 and IPv6 told apart by shape."""
    for item in value.split(","):
        entry = item.strip()
        if not entry:
            continue
        if not _ADDRESS.match(entry):
            # Not parsed into an address object here -- the draft holds strings
            # and the schema validates them -- but a value that cannot be one is
            # reported rather than written into a document that will not load.
            _append_unique(seen.bad_address, entry)
            continue
        if "/" not in entry:
            width = 128 if ":" in entry else 32
            entry = f"{entry}/{width}"
            _append_unique(
                interface.comments,
                f"an address in the configuration carries no prefix length; wg-quick passes it "
                f"to 'ip address add', which makes it a /{width}, so that is what is written "
                f"here",
            )
        target = interface.ipv6 if ":" in entry else interface.ipv4
        if entry not in target:
            target.append(entry)


def _read_mtu(value: str, *, interface: DraftInterface, seen: _Seen) -> None:
    mtu = read_int(value, low=1)
    if mtu is not None:
        interface.mtu = mtu
    else:
        _append_unique(seen.bad_mtu, value)


# --------------------------------------------------------------------------- #
# The interface's name
# --------------------------------------------------------------------------- #


def _interface_of(source: str, *, draft: Draft) -> DraftInterface | None:
    """The interface this file configures, named after the file that holds it.

    ``None`` when the input's name is not ``<interface>.conf``; the reason is on
    the draft's notes by then, and the caller adds nothing rather than inventing
    a port.
    """
    candidate, qualified = _name_from(source)
    if candidate is None:
        draft.note(
            f"{source}: a wg-quick file does not hold the name of the interface it configures "
            f"-- 'wg-quick up wg0' takes it from the file name -- and this input is not named "
            f"'<interface>.conf', so no interface could be named and none was imported; save "
            f"the capture as '<interface>.conf' and read it again"
        )
        return None

    name, original = interface_name(candidate)
    if name is None:  # pragma: no cover - wg-quick's own grammar excludes this
        draft.note(
            f"{source}: the interface name {candidate!r} taken from the file name holds no "
            f"usable characters, so no interface was imported"
        )
        return None

    interface = DraftInterface(name=name, type="tunnel")
    if original is not None:
        interface.comments.append(
            f"the interface is called {comment_text(original)!r} on the host; renamed here "
            "because a netgraph interface name may only hold letters, digits, '.', '/' and '-'"
        )
    if qualified:
        interface.comments.append(
            f"the name was taken from the file {comment_text(source)!r}: a wg-quick file does "
            f"not name its own interface, so the last dotted segment before '.conf' was used "
            f"-- check it if the file was renamed on its way here"
        )
    interface.comments.append(
        "this is one end of a WireGuard tunnel; netgraph models the tunnel as its own document "
        "naming both ends (docs/schema.md section 14), and this file identifies the far end "
        "only by a public key, so that document has to be written by hand"
    )
    return interface


def _name_from(source: str) -> tuple[str | None, bool]:
    """``("wg0", True)`` for ``rtr-hq.wg0.conf`` -- the name, and whether it was qualified.

    Both separators are handled because an input name reaches this reader as the
    caller typed it, and a Windows path typed at a Windows shell has ``\\``.
    """
    base = source.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base.lower().endswith(_SUFFIX):
        return (None, False)
    stem = base[: -len(_SUFFIX)]
    qualified = "." in stem
    candidate = stem.rpartition(".")[2] if qualified else stem
    if not _WG_NAME.match(candidate):
        return (None, False)
    return (candidate, qualified)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _report(seen: _Seen, *, source: str, draft: Draft) -> None:
    """One line per kind of thing the file held and the draft cannot.

    Fixed order, one note per kind however many times the kind occurred: a hub
    with forty peers should read as one sentence saying forty, not forty
    sentences.
    """
    if seen.peers:
        draft.note(
            f"{source}: the file names {seen.peers} WireGuard peer(s); netgraph models a "
            f"tunnel as its own document naming both ends (docs/schema.md section 14) and this "
            f"file identifies the far end only by a public key, which the inventory does not "
            f"hold, so no tunnel document was written -- write it by hand and it will match "
            f"the interface imported here"
        )
    if seen.endpoints:
        draft.note(
            f"{source}: the peer(s) are reached at {_listed(seen.endpoints)}; that is where a "
            f"tunnel document's far end lives, and it is recorded here because this reader "
            f"cannot write one"
        )
    for key, reason in _KEY_NOTES.items():
        count = seen.keys.get(key)
        if count:
            draft.note(f"{source}: {count} '{key}' line(s) were not imported: {reason}")
    if seen.interfaces > 1:
        draft.note(
            f"{source}: the file holds {seen.interfaces} '[Interface]' sections; wg-quick "
            f"configures one interface per file, so all of them were read as the same "
            f"interface -- check that this file is one configuration and not several"
        )
    elif not seen.interfaces:
        draft.note(
            f"{source}: the file holds no '[Interface]' section, so no address and no MTU were "
            f"read; the interface is still imported, because a wg-quick file named after it is "
            f"itself the statement that the host has it"
        )
    if seen.bad_address:
        draft.note(
            f"{source}: {len(seen.bad_address)} 'Address' value(s) ({_listed(seen.bad_address)}) "
            f"are not an address, so they were not imported"
        )
    if seen.bad_mtu:
        draft.note(
            f"{source}: 'MTU = {_listed(seen.bad_mtu)}' is not a number, so no MTU was imported"
        )
    if seen.sections:
        draft.note(
            f"{source}: {len(seen.sections)} section(s) netgraph does not read "
            f"({_listed(seen.sections)}) were left alone"
        )
    if seen.unknown:
        draft.note(
            f"{source}: {len(seen.unknown)} setting(s) netgraph does not read "
            f"({_listed(seen.unknown)}) were left alone; only the values a netgraph interface "
            f"can hold are imported"
        )
    if seen.stray:
        draft.note(
            f"{source}: {seen.stray} line(s) are neither a section header nor 'Key = Value' "
            f"and were skipped"
        )


def _listed(values: list[str], limit: int = 6) -> str:
    """``a, b, c and 4 more`` -- enough to recognise, short enough to read."""
    shown = ", ".join(comment_text(value) for value in values[:limit])
    remainder = len(values) - limit
    return f"{shown} and {remainder} more" if remainder > 0 else shown


def _append_unique(target: list[str], value: str) -> None:
    """Append ``value`` if it is new, keeping the order it was first seen in."""
    if value not in target:
        target.append(value)
