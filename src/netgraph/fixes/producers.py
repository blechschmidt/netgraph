"""The catalogue: which rules can be repaired mechanically, and how.

One entry per fixable rule, each holding a **pure** function from a finding and
the tree it was found in to the repairs on offer. Three properties are load
bearing, and every producer here is written to keep them:

*Pure.* A producer reads the inventory and returns operations. It opens no file,
resolves no path on disk and mutates nothing, so a caller may compute a repair to
show it, to count it, or to throw it away.

*Total.* A finding can outlive the tree that produced it — an editor holds one
while the file changes underneath — so a producer that cannot find what the
finding describes returns no fixes rather than raising. Every one of them
re-derives what it needs from the inventory and checks it still says what the
finding said.

*Honest about ambiguity.* Where a rule admits two plausible repairs, both are
offered and neither is picked. ``netgraph validate --fix`` applies a rule with
one repair and reports the rest, naming the choices; the editor puts a button
per choice on the diagnostic.

None of this makes a producer *safe*: the repair may still be a bad idea in a
particular tree. That is :mod:`netgraph.fixes.run`'s job — it applies a fix,
re-validates, and puts the tree back if a single new finding appeared.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from typing import Any, Final

from netgraph.edit.operations import (
    AddInterface,
    AppendItem,
    Disconnect,
    SetField,
    SetGeometry,
    UnsetField,
)
from netgraph.edit.paths import format_field_path
from netgraph.fixes.model import Choice, Fix, FixProducer, FixSpec
from netgraph.layout.document import inline_entry
from netgraph.loader.inventory import Inventory, namespace_of
from netgraph.models import (
    Adapter,
    Cable,
    Device,
    Group,
    Interface,
    InterfaceType,
    Medium,
    VlanSet,
)
from netgraph.validate import Finding

__all__ = [
    "FIXES",
    "fixable_rules",
    "fixes_for",
    "producer_for",
    "spec_for",
]

#: How alike a written interface name and a declared one have to be before the
#: difference is offered as a typo. High on purpose: ``eth0`` and ``eth1`` are
#: 0.75 alike and are two different ports, so anything that would confuse them
#: is not a fix, it is a coin toss.
_TYPO_CUTOFF: Final = 0.8

#: How much better than the runner-up the best match has to be. Without it, one
#: port of a switch whose names differ in a single digit would be picked over
#: forty-seven equally plausible ones.
_TYPO_MARGIN: Final = 0.05

#: The VLAN 802.1Q gives every bridge whether or not a database names it, which
#: is why ``W113`` does not report it and a repair must not declare it.
_DEFAULT_VLAN: Final = 1


# --------------------------------------------------------------------------- #
# Reading a finding back against the inventory
# --------------------------------------------------------------------------- #


def _element(inventory: Inventory, fqn: str | None, kind: type[Any] | tuple[type[Any], ...]) -> Any:
    """The element ``fqn`` names, when it is still there and still ``kind``."""
    if fqn is None:
        return None
    element = inventory.elements.get(fqn)
    return element if isinstance(element, kind) else None


def _interface_index(path: Sequence[str | int]) -> int | None:
    """The ``i`` of a ``spec.interfaces[i]…`` field path."""
    if len(path) < 3 or path[0] != "spec" or path[1] != "interfaces":
        return None
    return path[2] if isinstance(path[2], int) else None


def _interface_at(owner: Device | Adapter, path: Sequence[str | int]) -> Interface | None:
    """The interface a ``spec.interfaces[i]…`` path names, if it is still there."""
    index = _interface_index(path)
    if index is None or not 0 <= index < len(owner.interfaces):
        return None
    return owner.interfaces[index]


def _terminated(inventory: Inventory) -> set[tuple[str, str]]:
    """Every ``(element, interface)`` a cable already lands on.

    Resolved through the inventory rather than compared as text, because an
    endpoint may be written short in one document and fully qualified in
    another and still name the same port.
    """
    ports: set[tuple[str, str]] = set()
    for fqn, cable in inventory.cables.items():
        namespace = namespace_of(fqn)
        for ref in cable.endpoints:
            owner = inventory.resolve_fqn(ref.device, namespace=namespace)
            if owner is not None:
                ports.add((owner, ref.interface))
    return ports


def _mentions(data: Any, key: str, value: str) -> bool:
    """Does any ``key: value`` pair appear anywhere in ``data``?"""
    if isinstance(data, Mapping):
        return any(
            (name == key and item == value) or _mentions(item, key, value)
            for name, item in data.items()
        )
    if isinstance(data, (list, tuple)):
        return any(_mentions(item, key, value) for item in data)
    return False


# --------------------------------------------------------------------------- #
# E001 — a cable endpoint names an interface the element does not have
# --------------------------------------------------------------------------- #


def _fix_unknown_endpoint(finding: Finding, inventory: Inventory) -> Sequence[Fix]:
    """Repair a cable that lands on a port its element does not declare.

    Only the branch of ``E001`` that resolved the *element* and failed on the
    *interface* is fixable, and the finding says which one that is by naming the
    element as well as the cable. A reference to an element that does not exist
    at all is a different problem: nothing in the tree says whether the name is a
    typo or the device has yet to be written.

    Three repairs, because all three are things people mean. The first is only
    offered when exactly one declared port is close enough to the written name
    to be the typo it was, and that port takes no cable already.
    """
    if len(finding.elements) < 2:
        return ()
    cable_fqn, owner_fqn = finding.elements[0], finding.elements[1]
    cable = _element(inventory, cable_fqn, Cable)
    owner = _element(inventory, owner_fqn, (Device, Adapter))
    if cable is None or owner is None:
        return ()

    position = finding.field_path[-1] if finding.field_path else None
    ref = next(
        (
            candidate
            for index, candidate in enumerate(cable.endpoints)
            if (candidate.document_index if candidate.document_index is not None else index)
            == position
        ),
        None,
    )
    if ref is None or ref.interface in owner.interface_names():
        return ()

    fixes: list[Fix] = []
    taken = _terminated(inventory)
    free = [name for name in owner.interface_names() if (owner_fqn, name) not in taken]
    typo = _near_miss(ref.interface, free)
    if typo is not None:
        fixes.append(
            Fix(
                "retarget",
                f"point {cable_fqn!r} at {owner_fqn}:{typo} instead, which looks like "
                f"the port {ref.interface!r} was meant to be",
                (
                    SetField(
                        address=cable_fqn,
                        path=format_field_path(finding.field_path),
                        value=f"{ref.device}:{typo}",
                    ),
                ),
            )
        )
    fixes.append(
        Fix(
            "declare",
            f"declare interface {ref.interface!r} on {owner_fqn!r}",
            (
                AddInterface(
                    address=owner_fqn,
                    interface={"name": ref.interface, "type": _endpoint_type(cable).value},
                ),
            ),
        )
    )
    fixes.append(Fix("remove", f"remove cable {cable_fqn!r}", (Disconnect(address=cable_fqn),)))
    return tuple(fixes)


def _near_miss(written: str, candidates: Sequence[str]) -> str | None:
    """The one declared name ``written`` is a misspelling of, or ``None``.

    Two conditions, and both are needed. The name has to be *close* — a fix that
    guesses is worse than a diagnostic that does not — and it has to be closer
    than every other name by a clear margin: on a switch with 48 ports named
    alike, ``GigabitEthernet1/0/33`` resembles ``1/0/3`` and ``1/0/1`` almost
    equally, and choosing the first of those would be choosing a port at random.
    """
    scored = sorted(
        ((difflib.SequenceMatcher(a=written, b=name).ratio(), name) for name in candidates),
        reverse=True,
    )
    if not scored or scored[0][0] < _TYPO_CUTOFF:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < _TYPO_MARGIN:
        return None
    return scored[0][1]


def _endpoint_type(cable: Cable) -> InterfaceType:
    """What kind of port a cable of this medium terminates on (§7)."""
    if cable.spec.medium is Medium.WIRELESS:
        return InterfaceType.WIFI
    return InterfaceType.ETHERNET


# --------------------------------------------------------------------------- #
# W108 — a MAC address on a loopback
# --------------------------------------------------------------------------- #


def _fix_mac_on_loopback(finding: Finding, inventory: Inventory) -> Sequence[Fix]:
    """Drop the hardware address a software loopback cannot have.

    Unambiguous: the address is meaningless where it is written, and removing it
    is the whole repair. The alternative — making the interface a physical one —
    is not a repair of this document, it is a different network.
    """
    owner = _element(inventory, finding.element, (Device, Adapter))
    if owner is None:
        return ()
    interface = _interface_at(owner, finding.field_path)
    if interface is None or interface.type is not InterfaceType.LOOPBACK:
        return ()
    if interface.mac is None or finding.field_path[-1:] != ("mac",):
        return ()
    return (
        Fix(
            "drop",
            f"remove the MAC address from loopback {finding.element}:{interface.name}",
            (UnsetField(address=str(finding.element), path=format_field_path(finding.field_path)),),
        ),
    )


# --------------------------------------------------------------------------- #
# W113 — a port is in a VLAN the device's database does not declare
# --------------------------------------------------------------------------- #


def _fix_undeclared_vlan(finding: Finding, inventory: Inventory) -> Sequence[Fix]:
    """Add the missing entries to the device's VLAN database.

    The port is a member of the VLAN either way — that is what the finding says
    — so writing the database out changes nothing about the network and
    everything about whether the document can be read. The opposite repair,
    taking the VLAN off the port, changes what the port carries, so it is not
    offered: an editor that silently un-trunks a link is worse than a warning.
    """
    device = _element(inventory, finding.element, Device)
    if device is None:
        return ()
    declared = {vlan.id for vlan in device.spec.vlans}
    if not declared:
        return ()
    interface = _interface_at(device, finding.field_path)
    if interface is None:
        return ()

    missing = sorted(_referenced(interface, finding.field_path) - declared - {_DEFAULT_VLAN})
    if not missing:
        return ()
    listed = ", ".join(str(vlan_id) for vlan_id in missing)
    return (
        Fix(
            "declare",
            f"declare VLAN{'' if len(missing) == 1 else 's'} {listed} in the 'vlans' database "
            f"of {finding.element!r}",
            tuple(
                AppendItem(address=str(finding.element), path="spec.vlans", value={"id": vlan_id})
                for vlan_id in missing
            ),
        ),
    )


def _referenced(interface: Interface, path: Sequence[str | int]) -> frozenset[int]:
    """The VLAN ids the field ``path`` points at claims membership of.

    Two shapes reach here: the port's own ``vlan`` block, and the VLAN one of
    its BSSs is bridged into (§6.2.6). They are read separately because a
    finding names one of them, and repairing a radio's SSID by declaring the
    VLANs of the port it sits on would fix more than was reported.
    """
    tail = tuple(path[3:])
    if tail == ("vlan",) and interface.vlan is not None:
        return interface.vlan.vlan_ids()
    if (
        len(tail) == 4
        and tail[0] == "wireless"
        and tail[1] == "bss"
        and tail[3] == "vlan"
        and isinstance(tail[2], int)
        and interface.wireless is not None
        and 0 <= tail[2] < len(interface.wireless.bss)
    ):
        entry = interface.wireless.bss[tail[2]]
        return frozenset() if entry.vlan is None else frozenset({entry.vlan})
    return frozenset()


# --------------------------------------------------------------------------- #
# W114 — a trunk's native VLAN is not in its trunk_vlans
# --------------------------------------------------------------------------- #


def _fix_native_vlan(finding: Finding, inventory: Inventory) -> Sequence[Fix]:
    """Two readings of the same disagreement, and the document cannot say which.

    Either the list is short — the port does carry the VLAN untagged and the
    document forgot to say so — or the ``native_vlan`` is a leftover and the
    port was meant to tag everything. The first keeps what the hardware does and
    changes what the file says; the second is the reverse. Both are offered.
    """
    owner = _element(inventory, finding.element, (Device, Adapter))
    if owner is None:
        return ()
    interface = _interface_at(owner, finding.field_path)
    if interface is None or finding.field_path[-2:] != ("vlan", "native_vlan"):
        return ()
    vlan = interface.vlan
    if vlan is None or vlan.native_vlan is None or vlan.trunk_vlans is None:
        return ()
    if vlan.native_vlan in vlan.trunk_vlans:
        return ()

    port = f"{finding.element}:{interface.name}"
    base = format_field_path(finding.field_path[:-1])
    return (
        Fix(
            "list",
            f"list VLAN {vlan.native_vlan} in the trunk_vlans of {port}",
            (
                SetField(
                    address=str(finding.element),
                    path=f"{base}.trunk_vlans",
                    value=_with_vlan(vlan.trunk_vlans, vlan.native_vlan),
                ),
            ),
        ),
        Fix(
            "drop",
            f"remove the native_vlan of {port}, so it tags every VLAN it carries",
            (UnsetField(address=str(finding.element), path=format_field_path(finding.field_path)),),
        ),
    )


def _with_vlan(existing: VlanSet, vlan_id: int) -> str:
    """``existing`` plus ``vlan_id``, in the canonical range notation.

    Built from the ranges rather than from the ids so that ``all`` stays four
    characters instead of becoming a list of four thousand.
    """
    tokens = [str(low) if low == high else f"{low}-{high}" for low, high in existing.ranges]
    return str(VlanSet.model_validate([*tokens, str(vlan_id)]))


# --------------------------------------------------------------------------- #
# W136 — a VRF nothing is bound to
# --------------------------------------------------------------------------- #


def _fix_empty_vrf(finding: Finding, inventory: Inventory) -> Sequence[Fix]:
    """Remove a routing instance the document declares and never uses.

    Only when *nothing at all* names it: the finding fires on an unbound VRF
    even when static routes are placed in it, and removing the table those
    routes live in would turn a warning into an error. The remaining case is a
    declaration nobody reads, which is exactly what an unreferenced declaration
    is for removing.
    """
    device = _element(inventory, finding.element, Device)
    if device is None:
        return ()
    path = finding.field_path
    # The finding is anchored at the *name* of the entry -- 'spec.vrfs[0].name'
    # -- because that is the line a reader has to look at. The repair is about
    # the entry, so only the first three steps are its business.
    if len(path) < 3 or path[0] != "spec" or path[1] != "vrfs" or not isinstance(path[2], int):
        return ()
    index = path[2]
    if not 0 <= index < len(device.spec.vrfs):
        return ()
    vrf = device.spec.vrfs[index]
    if any(interface.vrf == vrf.name for interface in device.interfaces):
        return ()
    spec = device.spec.model_dump(exclude={"vrfs"}, exclude_none=True)
    if _mentions(spec, "vrf", vrf.name):
        return ()
    # A device left with 'vrfs: []' says the same thing as one with no key at
    # all, in a line somebody has to read and dismiss.
    target = "spec.vrfs" if len(device.spec.vrfs) == 1 else f"spec.vrfs[{index}]"
    return (
        Fix(
            "drop",
            f"remove the unused VRF {vrf.name!r} from {finding.element!r}",
            (UnsetField(address=str(finding.element), path=target),),
        ),
    )


# --------------------------------------------------------------------------- #
# W138 — geometry for something the inventory no longer declares
# --------------------------------------------------------------------------- #


def _fix_stale_geometry(finding: Finding, inventory: Inventory) -> Sequence[Fix]:
    """Drop one stale entry from one view of one layout document.

    What ``netgraph layout --prune`` does for a whole drawing, for the single
    key this finding names — and through the same operation, so the comments
    beside the entries that stay are kept and only the dead line goes.
    """
    fqn = finding.element
    path = finding.field_path
    if fqn is None or len(path) != 5 or path[0] != "spec" or path[1] != "views":
        return ()
    view, section, key = path[2], path[3], path[4]
    if not isinstance(view, str) or not isinstance(section, str) or not isinstance(key, str):
        return ()
    layout = inventory.layouts.get(fqn)
    if layout is None or section not in ("nodes", "edges", "groups"):
        return ()
    geometry = layout.spec.views.get(view)
    if geometry is None:
        return ()
    entries: Mapping[str, Any] = getattr(geometry, section)
    if key not in entries:
        return ()

    kept: dict[str, Any] = {
        name: inline_entry(value.model_dump(exclude_none=True))
        for name, value in entries.items()
        if name != key
    }
    sections: dict[str, Mapping[str, Any] | None] = {"nodes": None, "edges": None, "groups": None}
    sections[section] = kept
    return (
        Fix(
            "prune",
            f"drop {key!r} from the {view} view of layout {fqn!r}",
            (
                SetGeometry(
                    view=view,
                    nodes=sections["nodes"],
                    edges=sections["edges"],
                    groups=sections["groups"],
                    layout=fqn.rpartition("/")[2],
                    namespace=namespace_of(fqn),
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# W140 — a group still lists somebody who has left
# --------------------------------------------------------------------------- #


def _fix_departed_member(finding: Finding, inventory: Inventory) -> Sequence[Fix]:
    """Revoke the membership the departure was supposed to end.

    Not offered for the last member of a group: emptying it trades this warning
    for ``W139``, and "the access still exists" and "the group grants nothing"
    are different situations somebody should get to choose between.
    """
    group = _element(inventory, finding.element, Group)
    if group is None or len(finding.elements) < 2:
        return ()
    path = finding.field_path
    if len(path) != 3 or path[0] != "spec" or path[1] != "members" or not isinstance(path[2], int):
        return ()
    index = path[2]
    if not 0 <= index < len(group.spec.members) or len(group.spec.members) < 2:
        return ()
    member = inventory.users.get(finding.elements[1])
    if member is None or not member.has_departed:
        return ()
    return (
        Fix(
            "revoke",
            f"remove the departed {finding.elements[1]!r} from group {finding.element!r}",
            (UnsetField(address=str(finding.element), path=f"spec.members[{index}]"),),
        ),
    )


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


#: Every rule a finding can be repaired from, in rule-id order. A rule that is
#: absent is not fixable, and that is a statement about the *repair* rather than
#: about the rule: the tree does not say which of several networks the author
#: meant, so nothing here may pick one for them.
FIXES: Final[tuple[FixSpec, ...]] = (
    FixSpec(
        "E001",
        "Re-points the endpoint at the port it was misspelled from, declares the missing "
        "interface, or removes the cable",
        _fix_unknown_endpoint,
        (
            Choice("retarget", "writes the one declared port whose name is a near miss"),
            Choice("declare", "adds the interface the cable expects to the element"),
            Choice("remove", "deletes the cable"),
        ),
    ),
    FixSpec(
        "W108",
        "Removes the MAC address from the loopback",
        _fix_mac_on_loopback,
    ),
    FixSpec(
        "W113",
        "Adds the VLANs the port is a member of to the device's 'vlans' database",
        _fix_undeclared_vlan,
    ),
    FixSpec(
        "W114",
        "Lists the native VLAN in 'trunk_vlans', or removes the 'native_vlan'",
        _fix_native_vlan,
        (
            Choice("list", "adds the native VLAN to the trunk's VLAN set"),
            Choice("drop", "removes 'native_vlan', so the port tags everything it carries"),
        ),
    ),
    FixSpec(
        "W136",
        "Removes the VRF declaration, when nothing at all references it",
        _fix_empty_vrf,
    ),
    FixSpec(
        "W138",
        "Drops the stale entry from the layout document, as 'netgraph layout --prune' would",
        _fix_stale_geometry,
    ),
    FixSpec(
        "W140",
        "Removes the departed account from the group, unless it is the last member",
        _fix_departed_member,
    ),
)

_BY_RULE: Final[dict[str, FixSpec]] = {spec.rule: spec for spec in FIXES}


def spec_for(rule_id: str) -> FixSpec | None:
    """The fix entry for a canonical rule id, or ``None`` if there is none."""
    return _BY_RULE.get(rule_id)


def producer_for(rule_id: str) -> FixProducer | None:
    """The producer for a canonical rule id, or ``None`` if it is not fixable."""
    spec = _BY_RULE.get(rule_id)
    return spec.produce if spec is not None else None


def fixable_rules() -> tuple[str, ...]:
    """Every rule id that has a fix, in catalogue order."""
    return tuple(spec.rule for spec in FIXES)


def fixes_for(finding: Finding, inventory: Inventory) -> tuple[Fix, ...]:
    """Every repair on offer for one finding, in the order to show them.

    Empty when the rule has no fix, and also when it has one that does not apply
    to *this* finding — the producer re-derives what the finding described and
    finds it is no longer there.
    """
    produce = producer_for(finding.rule)
    if produce is None:
        return ()
    return tuple(produce(finding, inventory))
