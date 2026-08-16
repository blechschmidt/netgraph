"""The join: one drift finding in, zero or more intents out.

This is the module the whole command exists for. ``netviz drift`` produces a
:class:`~netviz.drift.model.Change` per disagreement; this turns each one into
what would close it, in the vocabulary of :mod:`netviz.converge.intent`.

Three rules govern what is *not* produced, and they matter more than what is:

**Nothing is proposed that no capture contradicted.** Every intent carries the
:class:`~netviz.converge.model.Provenance` of the finding it came from. There
is no path through this module that invents work.

**A physical port is never removed.** A capture reporting an ``ethernet``
interface the inventory does not declare has found a port that was always there;
the inventory is the thing that is wrong, and the fix is a document, not a
command. Only interfaces netviz could have *created* -- VLAN sub-interfaces,
bridges, bonds, tunnels -- are ever deleted, which keeps the rule symmetric:
netviz removes what netviz makes.

**Cabling is never a command.** A cable in the wrong port is somebody walking to
a rack. It stays in the plan as a :attr:`~netviz.converge.intent.IntentKind.
MANUAL` intent so the plan is honest about the network not being converged, and
it carries no commands so nothing can pretend otherwise.

Prerequisites that the capture cannot see are filled in from the *inventory*:
if a port is being put into VLAN 20 and the declared device has 20 in its VLAN
database but the capture never saw it, the VLAN is created first. That is not an
invention -- it is the declared state, which is the thing being converged on.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Final

from netviz.converge.intent import Intent, IntentKind, article, order_intents
from netviz.converge.model import Provenance
from netviz.drift.model import Change, Direction, DriftReport
from netviz.importer.draft import Draft, DraftDevice, DraftInterface
from netviz.loader.inventory import Inventory, short_name
from netviz.models import Device
from netviz.models.interface import Interface, InterfaceType

__all__ = ["derive", "observed_devices"]

#: Interface types netviz creates, and may therefore remove. Everything else
#: is a hole in a chassis: the capture found it because it is physically there,
#: and the remedy for an undeclared one is a document, not a command.
#:
#: Spelled as :class:`~netviz.models.interface.InterfaceType` spells them --
#: an aggregate is ``lag``, not ``bond``, which is what ``ip`` calls it and what
#: :mod:`netviz.importer.iproute` translates it *from*. Derived from the enum
#: rather than written out, so a type added there cannot leave this set claiming
#: the old one.
_VIRTUAL_TYPES: Final[frozenset[str]] = frozenset(
    {
        InterfaceType.VLAN.value,
        InterfaceType.BRIDGE.value,
        InterfaceType.LAG.value,
        InterfaceType.TUNNEL.value,
    }
)

#: Interface scalars a plan can set. ``type`` is absent on purpose: an interface
#: whose type disagrees is not a field to correct but a thing that is not what
#: the inventory thought it was, and turning a bond into an ethernet port is not
#: an operation any dialect has.
_SETTABLE: Final[frozenset[str]] = frozenset({"mac", "mtu", "parent"})


def observed_devices(inventory: Inventory, draft: Draft) -> Mapping[str, DraftDevice]:
    """The capture's devices, keyed by the declared name they resolved to.

    Resolution is :meth:`~netviz.loader.inventory.Inventory.lookup`, exactly as
    :mod:`netviz.drift.compare` does it, so a device that drift reported under
    ``sites/north/sw-1`` is found here under the same name. A capture name that
    resolves to nothing is kept under itself, so an undeclared device still has
    somewhere to hang its findings.
    """
    resolved: dict[str, DraftDevice] = {}
    for name, device in draft.devices.items():
        resolution = inventory.lookup(name)
        resolved[resolution.fqn if resolution.fqn is not None else name] = device
    return resolved


def derive(inventory: Inventory, draft: Draft, report: DriftReport) -> tuple[Intent, ...]:
    """Every intent the findings in ``report`` ask for, in dependency order.

    Args:
        inventory: The declared tree the capture was compared against.
        draft: The capture, as the importer's readers built it.
        report: The comparison of the two.

    Returns:
        Intents sorted by :func:`~netviz.converge.intent.order_intents`.
        Prerequisites are *not* filled in here: they are per element and the
        builder groups by element, so
        :func:`~netviz.converge.intent.prerequisites_of` is applied there.
    """
    observed = observed_devices(inventory, draft)
    intents: list[Intent] = []
    for change in report.changes:
        intents.extend(_intents_for(inventory, observed, change))
    intents.extend(_vlan_prerequisites(inventory, observed, intents))
    return order_intents(_deduplicate(intents))


# --------------------------------------------------------------------------- #
# One finding at a time
# --------------------------------------------------------------------------- #


def _intents_for(
    inventory: Inventory,
    observed: Mapping[str, DraftDevice],
    change: Change,
) -> Iterator[Intent]:
    """Zero or more intents for one drift finding."""
    origin = _provenance(change)
    declared = _declared_device(inventory, change.element)

    if change.scope in ("link", "device"):
        yield _manual(change, origin)
        return
    if change.scope == "field" and change.field == "type":
        yield _manual(change, origin)
        return
    if declared is None:
        # A finding on something the inventory does not declare as a device --
        # an undeclared box's interface. Nothing to converge it on to.
        yield _manual(change, origin)
        return

    interface = _declared_interface(declared, change.path)
    handler = {
        "interface": _interface_intents,
        "field": _field_intents,
        "address": _address_intents,
        "member": _member_intents,
        "vlan": _vlan_intents,
    }.get(change.scope)
    if handler is None:  # pragma: no cover - every scope drift emits is handled
        yield _manual(change, origin)
        return
    seen = observed.get(change.element)
    yield from handler(change, origin, interface, seen)


def _interface_intents(
    change: Change,
    origin: Provenance,
    interface: Interface | None,
    seen: DraftDevice | None,
) -> Iterator[Intent]:
    """An interface the two sides disagree about the existence of."""
    if change.direction is Direction.MISSING and interface is not None:
        yield Intent(
            kind=IntentKind.INTERFACE_CREATE,
            element=change.element,
            interface=interface.name,
            interface_type=interface.type.value,
            parent=interface.parent or "",
            value=interface.type.value,
            provenance=(origin,),
        )
        yield from _configure_new(change.element, interface, origin)
        return

    if change.direction is not Direction.UNDECLARED:  # pragma: no cover - drift emits two
        return
    kind = (change.observed or "ethernet").strip()
    if kind not in _VIRTUAL_TYPES:
        yield _manual(
            change,
            origin,
            summary=f"by hand: {change.path} is a physical port the inventory does not declare",
            note=(
                f"the capture reports {article(kind)} {kind} interface {change.path!r} that the "
                "inventory does not declare. netviz will not shut or remove a physical port it "
                "knows nothing about; declare it, or take it out of service by hand"
            ),
        )
        return
    observed_interface = _observed_interface(seen, change.path)
    yield Intent(
        kind=IntentKind.INTERFACE_DELETE,
        element=change.element,
        interface=change.path,
        interface_type=kind,
        parent=(observed_interface.parent or "") if observed_interface is not None else "",
        previous=kind,
        provenance=(origin,),
    )


def _configure_new(element: str, interface: Interface, origin: Provenance) -> Iterator[Intent]:
    """Everything a freshly-created interface needs before it is of any use.

    A declared interface that the capture could not find has to be made *and*
    configured; drift reported its absence once, not once per field, so the
    fields come from the declaration. They carry the same provenance, because
    they are all one finding: this interface is not there.
    """
    if interface.mtu is not None:
        yield Intent(
            kind=IntentKind.INTERFACE_SET,
            element=element,
            interface=interface.name,
            target="mtu",
            value=str(interface.mtu),
            provenance=(origin,),
        )
    for member in interface.members or ():
        yield Intent(
            kind=IntentKind.MEMBER_ADD,
            element=element,
            interface=interface.name,
            target=member,
            value=member,
            provenance=(origin,),
        )
    yield from _vlan_of(element, interface, origin)
    for family in ("ipv4", "ipv6"):
        config = interface.ipv4 if family == "ipv4" else interface.ipv6
        for address in config.addresses if config is not None else ():
            yield Intent(
                kind=IntentKind.ADDRESS_ADD,
                element=element,
                interface=interface.name,
                target=f"{address.ip}/{address.prefix_length}",
                value=family,
                provenance=(origin,),
            )
    if interface.enabled:
        yield Intent(
            kind=IntentKind.INTERFACE_ENABLE,
            element=element,
            interface=interface.name,
            target="enabled",
            value="true",
            provenance=(origin,),
        )


def _vlan_of(element: str, interface: Interface, origin: Provenance) -> Iterator[Intent]:
    vlan = interface.vlan
    if vlan is None:
        return
    yield Intent(
        kind=IntentKind.VLAN_MODE,
        element=element,
        interface=interface.name,
        target="mode",
        value=vlan.mode.value,
        provenance=(origin,),
    )
    if vlan.access_vlan is not None:
        yield Intent(
            kind=IntentKind.VLAN_ACCESS,
            element=element,
            interface=interface.name,
            target=str(vlan.access_vlan),
            value=str(vlan.access_vlan),
            provenance=(origin,),
        )
    for tagged in vlan.trunk_vlans or ():
        yield Intent(
            kind=IntentKind.VLAN_TAG,
            element=element,
            interface=interface.name,
            target=str(tagged),
            value=str(tagged),
            provenance=(origin,),
        )


def _field_intents(
    change: Change,
    origin: Provenance,
    interface: Interface | None,
    seen: DraftDevice | None,
) -> Iterator[Intent]:
    """A scalar field of an interface that the two sides spell differently."""
    name = change.field or ""
    if name == "enabled":
        wanted = (change.declared or "").lower() == "true"
        yield Intent(
            kind=IntentKind.INTERFACE_ENABLE if wanted else IntentKind.INTERFACE_DISABLE,
            element=change.element,
            interface=change.path,
            target="enabled",
            value=change.declared,
            previous=change.observed,
            provenance=(origin,),
        )
        return
    if name not in _SETTABLE or change.declared is None:  # pragma: no cover - drift emits four
        yield _manual(change, origin)
        return
    yield Intent(
        kind=IntentKind.INTERFACE_SET,
        element=change.element,
        interface=change.path,
        target=name,
        value=change.declared,
        previous=change.observed,
        interface_type=interface.type.value if interface is not None else "",
        provenance=(origin,),
    )


def _address_intents(
    change: Change,
    origin: Provenance,
    interface: Interface | None,
    seen: DraftDevice | None,
) -> Iterator[Intent]:
    family = change.field or "ipv4"
    if change.direction is Direction.MISSING and change.declared:
        yield Intent(
            kind=IntentKind.ADDRESS_ADD,
            element=change.element,
            interface=change.path,
            target=change.declared,
            value=family,
            provenance=(origin,),
        )
        return
    if change.direction is Direction.UNDECLARED and change.observed:
        yield Intent(
            kind=IntentKind.ADDRESS_REMOVE,
            element=change.element,
            interface=change.path,
            target=change.observed,
            value=family,
            previous=change.observed,
            provenance=(origin,),
        )


def _member_intents(
    change: Change,
    origin: Provenance,
    interface: Interface | None,
    seen: DraftDevice | None,
) -> Iterator[Intent]:
    if change.direction is Direction.MISSING and change.declared:
        yield Intent(
            kind=IntentKind.MEMBER_ADD,
            element=change.element,
            interface=change.path,
            target=change.declared,
            value=change.declared,
            interface_type=interface.type.value if interface is not None else "",
            provenance=(origin,),
        )
        return
    if change.direction is Direction.UNDECLARED and change.observed:
        yield Intent(
            kind=IntentKind.MEMBER_REMOVE,
            element=change.element,
            interface=change.path,
            target=change.observed,
            previous=change.observed,
            interface_type=interface.type.value if interface is not None else "",
            provenance=(origin,),
        )


def _vlan_intents(
    change: Change,
    origin: Provenance,
    interface: Interface | None,
    seen: DraftDevice | None,
) -> Iterator[Intent]:
    field_name = change.field or "vlan"
    if field_name == "vlan.mode" and change.declared:
        yield Intent(
            kind=IntentKind.VLAN_MODE,
            element=change.element,
            interface=change.path,
            target="mode",
            value=change.declared,
            previous=change.observed,
            provenance=(origin,),
        )
        return
    if field_name == "vlan.access_vlan" and change.declared:
        yield Intent(
            kind=IntentKind.VLAN_ACCESS,
            element=change.element,
            interface=change.path,
            target=change.declared,
            value=change.declared,
            previous=change.observed,
            provenance=(origin,),
        )
        return
    if change.direction is Direction.UNDECLARED and change.observed:
        yield Intent(
            kind=IntentKind.VLAN_UNTAG,
            element=change.element,
            interface=change.path,
            target=change.observed,
            previous=change.observed,
            provenance=(origin,),
        )
        return
    if change.direction is Direction.MISSING and change.declared:  # pragma: no cover
        yield Intent(
            kind=IntentKind.VLAN_TAG,
            element=change.element,
            interface=change.path,
            target=change.declared,
            value=change.declared,
            provenance=(origin,),
        )


# --------------------------------------------------------------------------- #
# Prerequisites the capture could not see
# --------------------------------------------------------------------------- #


def _vlan_prerequisites(
    inventory: Inventory,
    observed: Mapping[str, DraftDevice],
    intents: list[Intent],
) -> Iterator[Intent]:
    """Create a VLAN before anything is put into it.

    A capture rarely reports a device's VLAN database -- ``ip`` has no concept of
    one and LLDP does not carry it -- so drift cannot report a missing VLAN
    definition, and a plan built from drift alone would put a port into a VLAN
    that does not exist. The declaration is consulted instead: if the inventory
    says the device has VLAN 20 and something in the plan puts a port in 20, the
    VLAN is created first.

    Only VLANs the *inventory* declares are created. A VLAN nothing declares is
    not netviz's to invent, and a plan that made one up would be configuring a
    network from a capture rather than from a document.
    """
    wanted: dict[tuple[str, str], list[Provenance]] = {}
    for intent in intents:
        if intent.kind not in (IntentKind.VLAN_ACCESS, IntentKind.VLAN_TAG):
            continue
        # The provenance is the *referring* intent's, not the element's first:
        # "create VLAN 20 because port9 is declared in it" is a sentence somebody
        # can check, and "because a member of bond0 disagrees" is not.
        for entry in intent.provenance:
            behind = wanted.setdefault((intent.element, intent.target), [])
            if entry not in behind:
                behind.append(entry)

    for (element, vlan_id), behind in wanted.items():
        declared = _declared_device(inventory, element)
        if declared is None:  # pragma: no cover - an intent always has a device
            continue
        definition = next(
            (entry for entry in declared.spec.vlans if str(entry.id) == vlan_id), None
        )
        seen = observed.get(element)
        already = {str(vlan) for vlan in seen.vlans} if seen is not None else set()
        if definition is None or vlan_id in already:
            continue
        yield Intent(
            kind=IntentKind.VLAN_CREATE,
            element=element,
            target=vlan_id,
            value=definition.name or "",
            provenance=tuple(behind),
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _deduplicate(intents: list[Intent]) -> list[Intent]:
    """One intent per (element, key), with every finding behind it kept.

    Two findings can ask for the same thing -- an interface declared missing and
    an address declared missing on it both want the address configured -- and
    applying it twice is at best noise in a script somebody has to review.
    """
    merged: dict[tuple[str, str], Intent] = {}
    for intent in intents:
        identity = (intent.element, intent.key)
        existing = merged.get(identity)
        if existing is None:
            merged[identity] = intent
            continue
        extra = tuple(entry for entry in intent.provenance if entry not in existing.provenance)
        if extra:
            merged[identity] = _with_provenance(existing, existing.provenance + extra)
    return list(merged.values())


def _with_provenance(intent: Intent, provenance: tuple[Provenance, ...]) -> Intent:
    return replace(intent, provenance=provenance)


def _manual(change: Change, origin: Provenance, *, note: str = "", summary: str = "") -> Intent:
    """A finding netviz will not write a command for."""
    return Intent(
        kind=IntentKind.MANUAL,
        element=change.element,
        interface=change.path,
        target=change.field or change.scope,
        provenance=(origin,),
        note=note or _manual_note(change),
        summary=summary or _manual_summary(change),
    )


def _manual_summary(change: Change) -> str:
    """The one line a table holds for a finding no command closes.

    Short, and about the *thing* rather than about netviz: an operator
    scanning a plan wants to see "move a cable" and "declare a device", not six
    variations on "netviz cannot do this".
    """
    where = f" on {change.path}" if change.path else ""
    if change.scope == "link" or change.kind == "cable":
        if change.direction is Direction.UNDECLARED:
            return f"by hand: an undeclared link is patched at {short_name(change.element)}"
        if change.direction is Direction.MISSING:
            return f"by hand: the cable {short_name(change.element)} is not where it is declared"
        return f"by hand: {change.field or 'a property'} of {short_name(change.element)} differs"
    if change.scope == "device":
        if change.direction is Direction.UNDECLARED:
            return f"by hand: {short_name(change.element)} is on the network and not in the tree"
        return f"by hand: {short_name(change.element)} is not the kind of device declared"
    if change.field == "type":
        return f"by hand: {change.path} is not the interface type declared"
    return f"by hand: {change.scope}{where} cannot be closed by a command"


def _manual_note(change: Change) -> str:
    """What a person has to do about a finding no command can close."""
    if change.scope == "link" or change.kind == "cable":
        if change.direction is Direction.UNDECLARED:
            return (
                "the capture found a link the inventory does not declare. Cabling is not "
                "configuration: declare the cable, or unplug it"
            )
        if change.direction is Direction.MISSING:
            return (
                "the inventory declares a link the capture contradicts. Move the cable back, "
                "or update the inventory to where it now is"
            )
        return (
            "a declared cable's physical properties disagree with the capture. No command "
            "changes a cable; correct the inventory, or replace the run"
        )
    if change.scope == "device":
        if change.direction is Direction.UNDECLARED:
            return (
                "the capture found a device the inventory does not declare, so there is no "
                "declared state to converge it on. Run 'netviz import' to adopt it, or take "
                "it off the network"
            )
        return (
            "the capture reports a different kind of device than the inventory declares. "
            "No command turns one box into another; one of the two is describing the wrong "
            "device"
        )
    if change.field == "type":
        return (
            "the interface is not the type the inventory declares. Changing an interface's "
            "type is a re-creation, not an edit, so netviz leaves it to a person who can "
            "see what else is on it"
        )
    return "no dialect netviz writes has a command for this difference"


def _provenance(change: Change) -> Provenance:
    return Provenance(
        element=change.element,
        kind=change.kind,
        path=change.path,
        field=change.field,
        direction=str(change.direction),
        declared=change.declared,
        observed=change.observed,
        message=change.message,
    )


def _declared_device(inventory: Inventory, fqn: str) -> Device | None:
    element = inventory.elements.get(fqn)
    return element if isinstance(element, Device) else None


def _declared_interface(device: Device, name: str) -> Interface | None:
    if not name:
        return None
    return next((entry for entry in device.spec.interfaces if entry.name == name), None)


def _observed_interface(device: DraftDevice | None, name: str) -> DraftInterface | None:
    if device is None:
        return None
    return device.interfaces.get(name)
