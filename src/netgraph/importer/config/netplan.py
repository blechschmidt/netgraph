"""``--from netplan``: the YAML an Ubuntu or Debian host renders its links from.

netplan is the one dialect this package reads that was already a declarative
document before netgraph wrote one, so reading it back is mostly structural:
``ethernets``/``wifis``/``bonds``/``bridges``/``vlans``/``tunnels`` are the six
sections :mod:`netgraph.export.config.netplan` writes the six interface types
into, and ``addresses``/``macaddress``/``mtu`` under an interface mean what they
say. What is interesting is everything else that mapping holds, because a
netplan file is a statement about *reachability* as much as about links and a
:class:`~netgraph.importer.draft.DraftInterface` has fields for only half of it.

**Link-local and loopback addresses are dropped.** ``fe80::/64`` is
autoconfigured on every link there is, and ``127.0.0.1/8`` belongs to the
loopback this reader does not import; keeping either would put an identical
address on every host in the tree. :mod:`netgraph.importer.iproute` drops the
same addresses by the scope the kernel reports, and a YAML file carries no
scope, so the test here is the prefix — which is what "link-local" and
"loopback" mean in the only two families netplan configures.

**A stated MAC address is kept, on a bridge and a bond too.** This is the one
place the netplan reader deliberately parts company with the ``iproute`` reader,
which drops the MAC of a bridge, a bond or a VLAN because the kernel *reports*
one it borrowed from underneath. ``macaddress:`` in a netplan file is not a
report: somebody wrote it, and an inventory records exactly that sort of
statement. Where the file states one address on a bridge and on its member, the
tree says so and ``E003`` reports it — which is the right outcome for a
configuration that really does say that.

**A VLAN sub-interface implies a trunk underneath it.** ``eno1.100`` receives
only what ``eno1`` tags with VID 100, so the parent must carry it; the parent's
``vlan`` block is written with a comment marking it as inference, exactly as
``iproute`` does and for the same reason — without it an ordinary host fails
``E009``. netplan has no syntax for a trunk at all, which is why the emitter
refuses to write one, so the inferred list is a floor and never a reading.

**What netplan says about reachability is noted, not imported.** ``routes:``,
the deprecated ``gateway4:``/``gateway6:``, ``nameservers:``, ``dhcp4:``/
``dhcp6:``, a radio's ``access-points:`` and a tunnel's ``peers:``/``key:`` have
no field in a draft interface — the importer writes no ``spec.routes``, no
resolver and no key material, and a tunnel is its own document naming both ends
(``docs/schema.md`` §14). Each is reported once per kind per source rather than
once per occurrence: the messages name the key and never the interface, and
:meth:`~netgraph.importer.draft.Draft.note` keeps only the first of two
identical lines, so a file with forty ``dhcp4:`` keys is one line in the run
report. A section netplan has and netgraph has no interface type for —
``vrfs``, ``modems``, ``nm-devices`` — is reported the same way.

**What netplan says about itself passes without a note.** ``version``,
``renderer``, ``optional``, ``wakeonlan``, ``dhcp-identifier`` and the rest
describe how netplan brings a link up. They state nothing about the network, so
reporting them would only make the honest reports harder to find.

**``set-name`` names the interface; the netplan key may not.** A stanza with a
``match:`` block is keyed by a label of the operator's choosing, and the link is
called whatever ``set-name`` says. Taking the key would put a name in the
inventory that no host answers to, so ``set-name`` wins and the key is recorded
in a comment. A ``match:`` with no ``set-name`` matches by property and could
match several links; the key is then all there is, and saying so is a note.

**Nothing here raises.** A capture is collected off a host by whatever the
operator had to hand, and a drift run over a hundred of them must not stop at
the one that came back truncated: a file that is not YAML, or has no
``network:``, is a note and a return. An interface the schema would reject —
a bond or bridge with no ``interfaces:`` (``NG-I003``), a ``vlans`` entry with
no ``id:`` or ``link:`` (``NG-I002``) — is dropped with its reason rather than
written into a tree that will not load.
"""

from __future__ import annotations

from typing import Any, Final

import yaml

from netgraph.importer.config.common import fold_into
from netgraph.importer.draft import Draft, DraftDevice, DraftInterface, DraftVlan, comment_text
from netgraph.importer.names import interface_name

__all__ = ["read_netplan"]

#: The netgraph interface type of each netplan section, in the order
#: :mod:`netgraph.export.config.netplan` writes them — members before the
#: aggregates built from them, so a reader of the run report sees the stack in
#: the order it is built.
_TYPE_BY_SECTION: Final[dict[str, str]] = {
    "ethernets": "ethernet",
    "wifis": "wifi",
    "bonds": "lag",
    "bridges": "bridge",
    "vlans": "vlan",
    "tunnels": "tunnel",
}

#: Sections whose entries may state a lower link. ``parent`` is legal on a VLAN
#: and a tunnel and on nothing else (``NG-I002``), so ``link:`` read anywhere
#: else would be a field the models reject.
_PARENT_SECTIONS: Final[frozenset[str]] = frozenset({"vlans", "tunnels"})

#: Keys of ``network:`` that configure netplan rather than a link.
_RENDERER_KEYS: Final[frozenset[str]] = frozenset({"version", "renderer"})

#: Interface keys that carry a statement no draft interface has a field for, and
#: what to say about each. Keys that are one statement split over two lines of
#: YAML — ``dhcp4``/``dhcp6`` — share a message, so they share a note. The text
#: names the key and never the interface, which is what turns one occurrence per
#: port into one note per kind per source.
_DROPPED: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (
        ("routes",),
        "netplan 'routes:' say where traffic leaves by; 'netgraph import' writes interfaces "
        "and no 'spec.routes', so they were not imported",
    ),
    (
        ("gateway4", "gateway6"),
        "netplan 'gateway4:'/'gateway6:' are a default route in a spelling netplan itself "
        "deprecated; the importer writes no routes, so they were not imported",
    ),
    (
        ("nameservers",),
        "netplan 'nameservers:' configure host name resolution; netgraph describes links and "
        "the addresses on them, so they were not imported",
    ),
    (
        ("dhcp4", "dhcp6"),
        "netplan 'dhcp4:'/'dhcp6:' say an address is leased rather than what it is; only the "
        "addresses the file states are imported, so a link with nothing but DHCP has none here",
    ),
    (
        ("access-points",),
        "wifi 'access-points:' carry the SSIDs, key management and passphrase of a radio; that "
        "is 'interfaces[].wireless' in the schema (§10) and a draft interface has no field for "
        "it, so they were not imported",
    ),
)

#: The tunnel keys, split out because they appear only under ``tunnels:`` — where
#: ``id:`` is a VNI and not a VLAN — and because the two halves are dropped for
#: different reasons.
_DROPPED_TUNNEL: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (
        ("mode", "id", "local", "remote"),
        "a netplan tunnel's 'mode:', 'id:', 'local:' and 'remote:' describe an encapsulation; "
        "netgraph models a tunnel as its own document naming both ends (docs/schema.md §14), "
        "so they were not imported",
    ),
    (
        ("peers", "key"),
        "a netplan tunnel's 'peers:'/'key:' name the far ends and hold key material; netgraph "
        "models a tunnel as its own document naming both ends and stores no keys (§14.2), so "
        "they were not imported",
    ),
)


def read_netplan(text: str, *, source: str, host: str, draft: Draft) -> None:
    """Fold one netplan document into ``draft``.

    Args:
        text: The whole file, banner and all. A ``#`` banner is a YAML comment,
            so no stripping is needed before the parser sees it.
        source: Name of the input, for comments and the run report.
        host: Element name of the device the file was collected from. A netplan
            document names interfaces and never the host they are on, so the
            caller supplies it — from the banner of a generated file, or from
            ``--host``.
        draft: Accumulator, mutated in place.
    """
    fold_into(draft, host, source)
    network = _network(text, source=source, draft=draft)
    if network is None:
        return

    device = draft.device(host)
    for section in _TYPE_BY_SECTION:
        _read_section(
            network.get(section), section=section, source=source, device=device, draft=draft
        )
    _note_unread_sections(network, source=source, draft=draft)
    _infer_parent_trunks(device)


def _network(text: str, *, source: str, draft: Draft) -> dict[str, Any] | None:
    """The ``network:`` mapping, or ``None`` for a capture there is nothing in.

    ``yaml.safe_load`` rather than :mod:`netgraph.loader.documents`: that loader
    enforces the rules an *inventory* document is held to — no YAML 1.1 booleans,
    no repeated key, a depth ceiling — and raises an inventory diagnostic when
    one is broken. A file some other tool wrote is not held to them, and
    ``activation-mode: off`` is precisely the YAML 1.1 boolean it would refuse.

    ``ValueError`` is caught beside ``YAMLError`` because PyYAML resolves an
    integer scalar with ``int()``, which *raises* for a literal of more than 4300
    digits rather than returning one. That is a well-formed YAML document whose
    loading fails, and it would otherwise escape a drift run as a traceback about
    ``sys.set_int_max_str_digits`` — a reader here never raises on a capture.
    """
    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as exc:
        draft.note(
            f"{source}: the capture could not be read as YAML ({comment_text(str(exc))}), so "
            "nothing was read from it"
        )
        return None
    network = document.get("network") if isinstance(document, dict) else None
    if not isinstance(network, dict):
        draft.note(
            f"{source}: the capture holds no 'network:' mapping, so it is not a netplan "
            "document and nothing was read from it"
        )
        return None
    return network


def _read_section(
    entries: Any, *, section: str, source: str, device: DraftDevice, draft: Draft
) -> None:
    """Every interface of one netplan section, in the order the file lists them."""
    if entries is None:
        return
    if not isinstance(entries, dict):
        draft.note(
            f"{source}: the {section!r} section is not a mapping of interface names, so "
            "nothing was read from it"
        )
        return
    for key, body in entries.items():
        _read_interface(key, body, section=section, source=source, device=device, draft=draft)


def _read_interface(
    key: Any, body: Any, *, section: str, source: str, device: DraftDevice, draft: Draft
) -> None:
    """One entry of a netplan section, as an interface of ``device`` — or as a note."""
    # A key is a name, but YAML types it first: ``vlans: {10: {...}}`` arrives as
    # an int, and an interface called ``10`` is a legal one.
    label = key if isinstance(key, str) else str(key)
    mapping: dict[str, Any] = body if isinstance(body, dict) else {}
    if body is not None and not isinstance(body, dict):
        draft.note(
            f"{source}: the entry for {label!r} is not a mapping, so only the interface's "
            "name was read from it"
        )

    raw = _stated_name(mapping) or label
    name, original = interface_name(raw)
    if name is None:
        draft.note(
            f"{source}: the interface named {label!r} holds no usable characters, so it was "
            "not imported"
        )
        return

    interface = DraftInterface(name=name, type=_TYPE_BY_SECTION[section])
    _note_naming(interface, label, raw, original, mapping, source=source, draft=draft)
    _apply_link(interface, mapping)
    _apply_addresses(interface, mapping, source=source, draft=draft)
    _note_drops(mapping, section=section, source=source, draft=draft)
    if not _apply_stacking(
        interface, mapping, section=section, source=source, device=device, draft=draft
    ):
        return
    device.add_interface(interface)


def _stated_name(mapping: dict[str, Any]) -> str | None:
    """``set-name:`` — what the link is called once netplan has renamed it."""
    stated = mapping.get("set-name")
    return stated.strip() if isinstance(stated, str) and stated.strip() else None


def _note_naming(
    interface: DraftInterface,
    label: str,
    raw: str,
    original: str | None,
    mapping: dict[str, Any],
    *,
    source: str,
    draft: Draft,
) -> None:
    """Record every way the name written out differs from the netplan key."""
    if original is not None:
        interface.comments.append(
            f"the interface is named {comment_text(original)!r} on the host; renamed here "
            "because a netgraph interface name may only hold letters, digits, '.', '/' and '-'"
        )
    if raw != label:
        interface.comments.append(
            f"netplan keys this stanza {comment_text(label)!r} and renames the link with "
            "'set-name'; the name here is the one the host ends up using"
        )
    elif "match" in mapping:
        draft.note(
            f"{source}: {label!r} is matched by property ('match:') rather than by name and "
            "states no 'set-name'; the netplan key was taken as the interface name, which is "
            "right only if the link is also called that"
        )


def _apply_link(interface: DraftInterface, mapping: dict[str, Any]) -> None:
    """MAC, MTU and admin state — the properties of the link itself."""
    mac = mapping.get("macaddress")
    if isinstance(mac, str) and _looks_like_mac(mac):
        interface.mac = mac.strip().lower()
    elif isinstance(mac, str) and mac.strip():
        # netplan also accepts ``permanent``, ``random`` and ``stable``: a policy
        # for choosing an address rather than an address.
        interface.comments.append(
            f"netplan states 'macaddress: {comment_text(mac)}' here, which selects an address "
            "rather than being one; netgraph records only a fixed MAC"
        )

    mtu = mapping.get("mtu")
    if isinstance(mtu, int) and not isinstance(mtu, bool):
        interface.mtu = mtu

    mode = mapping.get("activation-mode")
    # YAML 1.1 resolves an unquoted ``off`` to the boolean false and the emitter
    # writes it unquoted, so both spellings reach here and mean the same thing.
    if mode is False or (isinstance(mode, str) and mode.strip().lower() == "off"):
        interface.enabled = False
    elif mode is not None:
        interface.comments.append(
            f"netplan states 'activation-mode: {comment_text(str(mode))}' here; netgraph "
            "models admin state as up or down, so this link was left as neither"
        )


def _apply_addresses(
    interface: DraftInterface, mapping: dict[str, Any], *, source: str, draft: Draft
) -> None:
    """``addresses:``, IPv4 and IPv6 kept apart by the only thing that tells them apart."""
    entries = mapping.get("addresses")
    if not isinstance(entries, list):
        return
    dropped = False
    for entry in entries:
        cidr = _address(entry)
        if cidr is None:
            continue
        if _is_transient(cidr):
            dropped = True
            continue
        # A netplan address is a CIDR string and nothing states its family, so
        # the colon does: it is legal in an IPv6 literal and in nothing else.
        target = interface.ipv6 if ":" in cidr else interface.ipv4
        if cidr not in target:
            target.append(cidr)
    if dropped:
        draft.note(
            f"{source}: dropped link-local ('fe80::') and loopback ('127.') addresses; those "
            "are true of every host and say nothing about this one"
        )


def _address(entry: Any) -> str | None:
    """One entry of ``addresses:``, in either spelling netplan accepts."""
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        # netplan 0.104's option form: ``- 10.0.0.1/24: {lifetime: 0}``. The key
        # is the address; the options are about how long the kernel keeps it.
        for key in entry:
            if isinstance(key, str) and key.strip():
                return key.strip()
    return None


def _is_transient(cidr: str) -> bool:
    """Is this an address every host has, rather than one this host was given?"""
    return cidr.lower().startswith("fe80:") or cidr.startswith("127.")


def _apply_stacking(
    interface: DraftInterface,
    mapping: dict[str, Any],
    *,
    section: str,
    source: str,
    device: DraftDevice,
    draft: Draft,
) -> bool:
    """``members`` for an aggregate, ``parent`` and the VID for a sub-interface.

    Returns ``False`` when the entry is a shape the models reject, in which case
    the caller drops the interface; the reason is already on the notes by then.
    """
    if section in _PARENT_SECTIONS:
        link = mapping.get("link")
        interface.parent = interface_name(link)[0] if isinstance(link, str) else None
    if section in ("bonds", "bridges"):
        interface.members = _members(mapping.get("interfaces"))
        if not interface.members:
            draft.note(
                f"{source}: {interface.name!r} is a {interface.type} whose 'interfaces:' list "
                "is empty or absent; netgraph requires at least one member (NG-I003), so it "
                "was not imported"
            )
            device.note(
                f"{interface.name!r} was declared in netplan as a {interface.type} with no "
                "member port and is therefore not listed below; add its ports by hand"
            )
            return False
    if section != "vlans":
        return True

    vid = mapping.get("id")
    if interface.parent is None or not isinstance(vid, int) or isinstance(vid, bool):
        draft.note(
            f"{source}: {interface.name!r} is in the 'vlans:' section but states no usable "
            "'link:' or no 'id:'; both are required of a VLAN sub-interface (NG-I002), so it "
            "was not imported"
        )
        return False
    interface.vlan = DraftVlan(mode="access", access_vlan=vid)
    device.vlans.add(vid)
    return True


def _members(stated: Any) -> list[str]:
    """``interfaces: [eno1, eno2]`` in the file's own order, without repeats.

    Not sorted: the list is one an operator wrote, and the order of a bond's
    ports is the order they will be read back in.
    """
    names: list[str] = []
    for entry in stated if isinstance(stated, list) else ():
        if not isinstance(entry, str):
            continue
        cleaned = interface_name(entry)[0]
        if cleaned is not None and cleaned not in names:
            names.append(cleaned)
    return names


def _note_drops(mapping: dict[str, Any], *, section: str, source: str, draft: Draft) -> None:
    """Report what the file states and a draft interface cannot hold."""
    entries = (*_DROPPED, *_DROPPED_TUNNEL) if section == "tunnels" else _DROPPED
    for keys, reason in entries:
        if any(key in mapping for key in keys):
            draft.note(f"{source}: {reason}")


def _note_unread_sections(network: dict[str, Any], *, source: str, draft: Draft) -> None:
    """Report every ``network:`` key that is neither a section nor about netplan."""
    for section in network:
        if not isinstance(section, str) or section in _TYPE_BY_SECTION:
            continue
        if section in _RENDERER_KEYS:
            continue
        draft.note(
            f"{source}: the netplan section {section!r} maps onto no netgraph interface type, "
            "so nothing in it was imported"
        )


def _infer_parent_trunks(device: DraftDevice) -> None:
    """Make every VLAN sub-interface's parent carry the VLAN it encapsulates.

    The one place this reader states something the file did not. It is not a
    guess: a sub-interface receives exactly the frames its parent tags with that
    VID, so a parent that did not carry it would make the sub-interface
    unreachable — and ``E009`` says so. netplan cannot describe a trunk in the
    first place, which is why the comment says the list is a floor.
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
            "must carry them tagged; netplan has no syntax for a trunk, so this list is the "
            "minimum — extend it with the VLANs the port really carries"
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
    """Is this the six-octet form netgraph records, rather than a netplan policy?"""
    parts = value.strip().split(":")
    return len(parts) == 6 and all(len(part) == 2 for part in parts)
