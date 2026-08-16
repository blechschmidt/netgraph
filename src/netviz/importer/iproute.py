"""``--from iproute``: ``ip -j link show`` and ``ip -j addr show`` for one host.

iproute2's JSON output is the richest of the three dialects and the only one
that describes a device's *configuration* rather than its neighbours. One
capture yields interface names, MAC addresses, MTUs, admin state, addresses, and
— through ``linkinfo`` — the three stacking constructs netviz models:

===================  ==========================  ===============================
``linkinfo``         netviz                    where the relationship comes from
===================  ==========================  ===============================
``info_kind: bridge``  ``type: bridge``          ``members``, from the ``master``
                                                 field of every enslaved link
``info_kind: bond``    ``type: lag``             the same
``info_kind: vlan``    ``type: vlan``            ``parent`` from ``link``, the VID
                                                 from ``info_data.id``
===================  ==========================  ===============================

``ip -j addr show`` is a strict superset of ``ip -j link show``, so one reader
serves both and passing both files for one host is not merely allowed but the
expected thing to do: they merge (see
:meth:`~netviz.importer.draft.DraftInterface.merge`) and the argument order
does not matter.

Four judgement calls are worth stating outright, because each one drops or adds
something and a reader of the generated tree deserves to know which:

**Loopback interfaces are not imported.** ``lo`` terminates no cable, appears in
no topology and carries only host-scope addresses. netviz's ``loopback`` type
exists for a router loopback somebody declares on purpose, which is not this.

**Link- and host-scope addresses are dropped.** ``fe80::/64`` and ``127.0.0.1/8``
are autoconfigured facts about a running kernel, not statements of intent, and
importing them would put an identical ``fe80::`` on every host in the tree.
Global-scope addresses are kept, with a comment when the kernel says they came
from DHCP.

**Derived MAC addresses are not written.** A bridge, a bond and a VLAN
sub-interface all *report* a MAC, and in every case it is borrowed from a member
or a parent. Writing it out would state as configuration something the kernel
chose, and would trip ``E003`` (the same MAC on two interfaces) on the most
ordinary Linux host there is.

**A VLAN sub-interface implies a trunk underneath it.** ``eno1.100`` can only
receive frames if ``eno1`` carries VLAN 100 tagged. ``ip`` never says so, but it
follows from what ``ip`` did say, so the parent gets
``vlan: {mode: trunk, trunk_vlans: [...]}`` with a comment marking it as
inference. Without it the tree would fail ``E009`` on a configuration that is
perfectly real.

Anything else stacked — WireGuard, VXLAN, GRE, VRF, tun/tap — is *reported and
skipped*. netviz models a tunnel as its own document naming both ends
(``docs/schema.md`` §14), and ``ip`` shows only the local end, so there is
nothing to write that would not be a guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from typing import Any, Final

from netviz.importer.draft import Draft, DraftDevice, DraftInterface, DraftVlan, comment_text
from netviz.importer.names import interface_name

__all__ = ["read_iproute"]

#: ``linkinfo.info_kind`` values that map onto a netviz interface type.
_TYPE_BY_INFO_KIND: Final[dict[str, str]] = {
    "bridge": "bridge",
    "bond": "lag",
    "team": "lag",
    "vlan": "vlan",
    # Virtual ethernet devices behave exactly like a NIC as far as this model is
    # concerned: they carry frames, hold addresses and can be bridged.
    "veth": "ethernet",
    "dummy": "ethernet",
    "macvlan": "ethernet",
    "macvtap": "ethernet",
    "ipvlan": "ethernet",
}

#: ``linkinfo.info_kind`` values that are tunnels. Recognised only so that the
#: run report can say *why* they were left out instead of silently dropping them.
_TUNNEL_INFO_KINDS: Final[frozenset[str]] = frozenset(
    {
        "wireguard",
        "vxlan",
        "gre",
        "gretap",
        "erspan",
        "ip6gre",
        "ip6gretap",
        "ipip",
        "sit",
        "ip6tnl",
        "geneve",
        "vti",
        "vti6",
        "xfrm",
        "tun",
    }
)

#: Interface types whose reported MAC belongs to something underneath them.
_DERIVED_MAC_TYPES: Final[frozenset[str]] = frozenset({"bridge", "lag", "vlan"})

#: Address scopes that describe a running kernel rather than a configuration.
_TRANSIENT_SCOPES: Final[frozenset[str]] = frozenset({"link", "host", "nowhere"})

#: A MAC of all zeroes identifies nothing; the kernel prints it for links that
#: have no hardware address at all.
_NULL_MAC: Final = "00:00:00:00:00:00"


def read_iproute(
    payload: Any,
    *,
    source: str,
    host: str,
    draft: Draft,
    exclude: Sequence[str] = (),
) -> None:
    """Fold one ``ip -j link show`` or ``ip -j addr show`` capture into ``draft``.

    Args:
        payload: The parsed JSON document — a list of link records.
        source: Name of the input, for comments and the run report.
        host: Element name of the device the capture was taken on. ``ip`` never
            names the host it ran on, so the caller supplies it.
        draft: Accumulator, mutated in place.
        exclude: ``fnmatch`` patterns; an interface whose name matches any of
            them is left out, which is how ``veth*`` and ``docker*`` are kept
            out of an inventory that is about physical topology.
    """
    records = [record for record in _records(payload) if isinstance(record, dict)]
    if not records:
        draft.note(f"{source}: no interface records in the capture")
        return

    device = draft.device(host)
    device.observed_in(source)
    members = _members_by_master(records)

    for record in records:
        _read_link(
            record, source=source, device=device, draft=draft, members=members, exclude=exclude
        )

    _infer_parent_trunks(device)


def _records(payload: Any) -> list[Any]:
    """The list of link records, however the caller wrapped it.

    ``ip -j`` prints a bare JSON array. A capture pasted into a wrapper object
    is common enough — an Ansible ``stdout`` field, a per-host collection
    script — that unwrapping one costs less than the support question does.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("addr_info", "links", "interfaces", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested
    return []


def _members_by_master(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``{"br0": ["eno1", "eno2"]}`` — the enslavement graph, read backwards.

    ``ip`` records enslavement on the *member* (``"master": "br0"``) and never
    on the aggregate, while netviz declares it on the aggregate
    (``members: [eno1, eno2]``). This is the whole of the translation.
    """
    members: dict[str, list[str]] = {}
    for record in records:
        master = record.get("master")
        name = record.get("ifname")
        if isinstance(master, str) and isinstance(name, str):
            members.setdefault(master, []).append(name)
    return {master: sorted(names) for master, names in members.items()}


def _read_link(
    record: dict[str, Any],
    *,
    source: str,
    device: DraftDevice,
    draft: Draft,
    members: dict[str, list[str]],
    exclude: Sequence[str],
) -> None:
    """One ``ip`` link record, as an interface of ``device`` — or as a note."""
    raw = record.get("ifname")
    if not isinstance(raw, str) or not raw:
        draft.note(f"{source}: a link record carries no 'ifname' and was skipped")
        return
    if any(fnmatchcase(raw, pattern) for pattern in exclude):
        return

    name, original = interface_name(raw)
    if name is None:  # pragma: no cover - the kernel does not produce such a name
        draft.note(f"{source}: interface name {raw!r} holds no usable characters and was skipped")
        return

    info = _info(record)
    kind = _text(info.get("info_kind"))
    interface_type = _interface_type(record, kind)
    if interface_type is None:
        _note_unimportable(record, raw, kind=kind, source=source, draft=draft)
        return

    interface = DraftInterface(name=name, type=interface_type)
    if original is not None:
        interface.comments.append(
            f"the interface is named {comment_text(original)!r} on the host; renamed here "
            "because a netviz interface name may only hold letters, digits, '.', '/' and '-'"
        )
    if kind is not None and interface_type == "ethernet" and kind not in ("veth", ""):
        interface.comments.append(
            f"inferred: 'ip' reports this as a {kind!r} link; netviz has no such type, "
            "so it is written as 'ethernet'"
        )

    _apply_state(interface, record)
    if not _apply_stacking(
        interface, record, info=info, members=members, source=source, draft=draft, device=device
    ):
        return
    _apply_addresses(interface, record, source=source, draft=draft)

    if interface.type in ("bridge", "lag") and not interface.members:
        draft.note(
            f"{source}: {raw!r} is a {kind} with no enslaved interface in this capture; "
            "netviz requires at least one member, so it was not imported"
        )
        device.note(
            f"{raw!r} was observed as a {kind} with no members and is therefore not listed "
            "below; capture the hosts that own its ports, or write it out by hand"
        )
        return

    device.add_interface(interface)


def _interface_type(record: dict[str, Any], kind: str | None) -> str | None:
    """The netviz ``type`` of a link record, or ``None`` when there is none."""
    if kind is not None:
        return _TYPE_BY_INFO_KIND.get(kind)
    link_type = _text(record.get("link_type"))
    return "ethernet" if link_type == "ether" else None


def _note_unimportable(
    record: dict[str, Any], raw: str, *, kind: str | None, source: str, draft: Draft
) -> None:
    """Say why a link was left out. Every skip is reported; none is silent."""
    link_type = _text(record.get("link_type"))
    if kind in _TUNNEL_INFO_KINDS:
        draft.note(
            f"{source}: {raw!r} is a {kind} tunnel; netviz models a tunnel as its own "
            "document naming both ends (docs/schema.md §14) and 'ip' shows only this end, "
            "so it was not imported"
        )
    elif link_type == "loopback":
        draft.note(
            f"{source}: {raw!r} is the kernel loopback; it terminates no cable and holds only "
            "host-scope addresses, so it was not imported"
        )
    else:
        draft.note(
            f"{source}: {raw!r} is a {kind or link_type or 'link of unknown type'} that maps "
            "onto no netviz interface type, so it was not imported"
        )


def _apply_state(interface: DraftInterface, record: dict[str, Any]) -> None:
    """Admin state, MAC and MTU."""
    flags = record.get("flags")
    if isinstance(flags, list):
        # The IFF_UP flag is the *administrative* state, which is what
        # ``if:enabled`` means; ``operstate`` is the carrier and is not modelled.
        interface.enabled = "UP" in flags

    address = _text(record.get("address"))
    if (
        address is not None
        and address != _NULL_MAC
        and interface.type not in _DERIVED_MAC_TYPES
        and _looks_like_mac(address)
    ):
        interface.mac = address.lower()

    mtu = record.get("mtu")
    if isinstance(mtu, int) and not isinstance(mtu, bool):
        interface.mtu = mtu


def _apply_stacking(
    interface: DraftInterface,
    record: dict[str, Any],
    *,
    info: dict[str, Any],
    members: dict[str, list[str]],
    source: str,
    draft: Draft,
    device: DraftDevice,
) -> bool:
    """``members`` for an aggregate, ``parent`` plus the VID for a sub-interface.

    Returns ``False`` when the record is a shape netviz cannot express, in
    which case the caller drops the interface; the reason is already on the
    draft's notes by then.
    """
    if interface.type in ("bridge", "lag"):
        enslaved = members.get(_text(record.get("ifname")) or "", ())
        interface.members = [
            cleaned for name in enslaved if (cleaned := interface_name(name)[0]) is not None
        ]
        return True
    if interface.type != "vlan":
        return True

    parent = _text(record.get("link"))
    data = _mapping(info.get("info_data"))
    vid = data.get("id")
    cleaned_parent = interface_name(parent)[0] if parent is not None else None
    if cleaned_parent is None or not isinstance(vid, int) or isinstance(vid, bool):
        draft.note(
            f"{source}: {record.get('ifname')!r} is a VLAN interface but the capture reports "
            "no parent link or no VLAN id, so it was not imported"
        )
        return False

    interface.parent = cleaned_parent
    interface.vlan = DraftVlan(mode="access", access_vlan=vid)
    device.vlans.add(vid)

    protocol = _text(data.get("protocol"))
    if protocol is not None and protocol not in ("802.1Q", "802.1q"):
        interface.comments.append(
            f"'ip' reports this sub-interface as {protocol}; netviz's VLAN model is "
            "802.1Q, so check that this is what you mean"
        )
    return True


def _apply_addresses(
    interface: DraftInterface, record: dict[str, Any], *, source: str, draft: Draft
) -> None:
    """The global-scope addresses of one link, IPv4 and IPv6 kept apart."""
    entries = record.get("addr_info")
    if not isinstance(entries, list):
        return
    dropped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scope = _text(entry.get("scope"))
        family = _text(entry.get("family"))
        local = _text(entry.get("local"))
        prefix = entry.get("prefixlen")
        if local is None or not isinstance(prefix, int) or isinstance(prefix, bool):
            continue
        if scope is not None and scope in _TRANSIENT_SCOPES:
            dropped += 1
            continue
        target = (
            interface.ipv4 if family == "inet" else interface.ipv6 if family == "inet6" else None
        )
        if target is None:
            continue
        value = f"{local}/{prefix}"
        if value not in target:
            target.append(value)
        if entry.get("dynamic"):
            interface.comments.append(
                f"{value} was assigned dynamically (DHCP or SLAAC) when the capture was "
                "taken; it is written here as configuration — confirm that it is fixed"
            )
    if dropped:
        draft.note(
            f"{source}: dropped {dropped} link- or host-scope address(es) from "
            f"{record.get('ifname')!r}; those describe a running kernel, not a configuration"
        )


def _infer_parent_trunks(device: DraftDevice) -> None:
    """Make every VLAN sub-interface's parent carry the VLAN it encapsulates.

    This is the one place the importer states something the tool did not print.
    It is not a guess: a sub-interface receives exactly the frames its parent
    tags with that VID, so a parent that did not carry it would make the
    sub-interface unreachable — and ``E009`` says so. The alternative is a tree
    that fails validation on an entirely ordinary host.
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
            "must carry them tagged; 'ip' does not report a port's VLAN set, so this list "
            "is the minimum — extend it with the VLANs the port really trunks"
        )
        if parent.vlan is None:
            parent.vlan = DraftVlan(mode="trunk", trunk_vlans=sorted(vids), comment=comment)
        elif parent.vlan.mode == "trunk":
            parent.vlan.trunk_vlans = sorted({*parent.vlan.trunk_vlans, *vids})
            parent.vlan.comment = parent.vlan.comment or comment
        device.vlans |= vids


def _ids(vids: set[int]) -> str:
    return ", ".join(str(vid) for vid in sorted(vids))


def _info(record: dict[str, Any]) -> dict[str, Any]:
    return _mapping(record.get("linkinfo"))


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _looks_like_mac(value: str) -> bool:
    """Is this the six-octet form? ``ip`` prints 20 octets for InfiniBand."""
    parts = value.split(":")
    return len(parts) == 6 and all(len(part) == 2 for part in parts)
