"""Recognising that an element was *renamed* rather than destroyed and rebuilt.

A diff that keys on the name alone reports a rename as a delete plus a create.
That is not merely verbose: it is wrong about what will happen. Executed, it
would drop the device's document — taking its description, its comments and
every cable that terminates on it — and write a fresh one. The plan has to say
"this is the same thing, under a new name" and the executor has to make it so
with :class:`~netviz.edit.operations.RenameElement`, which rewrites every
reference to it in place.

So the match is made on **structural identity**: something about the element
that the network fixes and the operator does not choose. In descending order of
how much it is worth trusting:

1. An explicit ``netviz.dev/id`` annotation. If somebody has told us what the
   stable identity is, no inference can beat it.
2. A serial number. Vendors do not reissue them.
3. Hardware addresses. A device with the same set of MACs is the same device.
4. The link ends. A cable is *defined* by what it joins; a tunnel and an
   adapter nearly so.
5. The label on the cable. The sticker survives re-patching, which is the one
   change that moves a cable's ends without making it a different cable.
6. The rack position, then the port list. Weak, but two elements that occupy
   one rack unit or expose one set of ports are almost never different things.

Only the first three *veto*: see :data:`DECISIVE`.

A candidate is only accepted when a key matches **exactly one** unpaired element
on each side. Ambiguity is left as a delete and a create, which is the honest
answer: three unnamed patch panels in one rack cannot be told apart, and
guessing which became which would move cables onto the wrong panel.

Ordering matters too. A cable's ends are named by the devices they land on, so
devices are matched first and the cable's key is then computed against the names
the first pass settled on — otherwise renaming a switch would make every cable
on it look like a different cable.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final

from netviz.loader.inventory import Inventory, namespace_of
from netviz.models import Adapter, Cable, Device, ElementBase, PatchPanel, Pdu, Tunnel
from netviz.plan.address import DEVICE_TYPE, Address

__all__ = ["DECISIVE", "EVIDENCE", "STABLE_ID_ANNOTATION", "detect_renames", "fingerprints"]

#: The annotation an operator sets to pin an element's identity across renames.
STABLE_ID_ANNOTATION: Final = "netviz.dev/id"

#: Every kind of structural evidence, strongest first. A match is tried on each
#: in turn, and the first that names exactly one unpaired element on each side
#: wins.
EVIDENCE: Final[tuple[str, ...]] = (
    "id",
    "serial",
    "mac",
    "ends",
    "upstream",
    "label",
    "rack",
    "ports",
)

#: Evidence whose *disagreement* proves two elements are not the same thing, and
#: therefore vetoes a match some weaker evidence would have made.
#:
#: Only the immutable three qualify. A serial is burned into the hardware and a
#: MAC is assigned with it, so two elements that both state one and state
#: different ones are two boxes however alike their port lists are. Everything
#: else on the list is a property somebody can change without replacing
#: anything — which is exactly the case a rename has to survive: re-patching a
#: labelled cable changes its ends, and moving a switch changes its rack.
DECISIVE: Final[frozenset[str]] = frozenset({"id", "serial", "mac"})

#: Address types matched in the first pass. Everything in the second pass may
#: refer to one of them, so their names have to be settled first.
_ANCHORS: Final[frozenset[str]] = frozenset({DEVICE_TYPE, "patchpanel", "pdu", "user"})


def detect_renames(
    before: Inventory,
    after: Inventory,
    *,
    deleted: Mapping[Address, ElementBase],
    created: Mapping[Address, ElementBase],
) -> dict[Address, Address]:
    """Pair up unmatched elements, old address to new.

    Args:
        before: The source state, for resolving the old side's references.
        after: The target state, for resolving the new side's.
        deleted: Elements present only in ``before``, by address.
        created: Elements present only in ``after``, by address.

    Returns:
        Old address to new address, for every pair that could be identified. An
        element that is not in the result really was created or destroyed.
    """
    renames: dict[Address, Address] = {}
    remaining_old = dict(deleted)
    remaining_new = dict(created)
    for anchors in (True, False):
        old = {
            address: element
            for address, element in remaining_old.items()
            if (address.type in _ANCHORS) is anchors
        }
        new = {
            address: element
            for address, element in remaining_new.items()
            if (address.type in _ANCHORS) is anchors
        }
        if not old or not new:
            continue
        # The second pass resolves through the first pass's answers, so a cable
        # on a renamed switch still keys on the same pair of ends.
        aliases = {address.fqn: target.fqn for address, target in renames.items()}
        matched = _match(
            old, new, before=before, after=after, aliases=aliases if not anchors else {}
        )
        renames.update(matched)
        for address in matched:
            del remaining_old[address]
        for address in matched.values():
            del remaining_new[address]
    return renames


def _match(
    old: Mapping[Address, ElementBase],
    new: Mapping[Address, ElementBase],
    *,
    before: Inventory,
    after: Inventory,
    aliases: Mapping[str, str],
) -> dict[Address, Address]:
    """One pass: strongest evidence first, unique and uncontradicted matches only."""
    old_keys = {
        address: fingerprints(address, element, before, aliases=aliases)
        for address, element in old.items()
    }
    new_keys = {
        address: fingerprints(address, element, after, aliases={})
        for address, element in new.items()
    }

    renames: dict[Address, Address] = {}
    taken: set[Address] = set()
    for evidence in EVIDENCE:
        left = _by_key(old_keys, evidence, skip=set(renames))
        right = _by_key(new_keys, evidence, skip=taken)
        for key, candidates in left.items():
            others = right.get(key, ())
            if len(candidates) != 1 or len(others) != 1:
                continue
            source, target = candidates[0], others[0]
            if source.type != target.type or source == target:
                continue
            if _contradicted(old_keys[source], new_keys[target], evidence):
                continue
            renames[source] = target
            taken.add(target)
    return renames


def _by_key(
    keys: Mapping[Address, Mapping[str, tuple[Any, ...]]], evidence: str, *, skip: set[Address]
) -> dict[tuple[Any, ...], list[Address]]:
    grouped: dict[tuple[Any, ...], list[Address]] = {}
    for address, fingerprint in keys.items():
        if address in skip or evidence not in fingerprint:
            continue
        grouped.setdefault(fingerprint[evidence], []).append(address)
    return grouped


def _contradicted(
    source: Mapping[str, tuple[Any, ...]], target: Mapping[str, tuple[Any, ...]], evidence: str
) -> bool:
    """Does :data:`DECISIVE` evidence say these are different things?

    Two access points with the same four port names are plausibly one renamed
    access point — unless both state their MAC addresses and the two sets are
    disjoint, in which case they are plainly two boxes and the port names are a
    coincidence of the vendor's naming. Weak evidence may identify; it may not
    overrule what the hardware says.
    """
    for stronger in EVIDENCE:
        if stronger == evidence:
            return False
        if stronger not in DECISIVE:
            continue
        if stronger in source and stronger in target and source[stronger] != target[stronger]:
            return True
    return False  # pragma: no cover - the loop always reaches ``evidence``


def fingerprints(
    address: Address,
    element: ElementBase,
    inventory: Inventory,
    *,
    aliases: Mapping[str, str] = {},
) -> dict[str, tuple[Any, ...]]:
    """Every structural key for ``element``, keyed by the evidence it rests on.

    Args:
        address: Where the element lives, which fixes the namespace its
            references resolve in.
        element: The element itself.
        inventory: The state it belongs to, for resolving those references.
        aliases: Old fully-qualified name to new, applied to resolved reference
            targets so that a link keyed on an already-renamed device still
            matches. Empty on the first pass.

    Returns:
        Evidence name to key, in :data:`EVIDENCE` order — strongest first.
    """
    return dict(_keys(address, element, inventory, aliases))


def _keys(
    address: Address,
    element: ElementBase,
    inventory: Inventory,
    aliases: Mapping[str, str],
) -> Iterator[tuple[str, tuple[Any, ...]]]:
    stable = element.metadata.annotations.get(STABLE_ID_ANNOTATION)
    if stable:
        yield "id", (stable,)

    spec: Any = getattr(element, "spec", None)
    serial = getattr(spec, "serial", None)
    if serial:
        yield "serial", (getattr(spec, "vendor", None) or "", str(serial))

    if isinstance(element, Device):
        macs = tuple(
            sorted(
                interface.mac.lower() for interface in spec.interfaces if interface.mac is not None
            )
        )
        if macs:
            yield "mac", macs

    if isinstance(element, Cable | Tunnel):
        ends = _ends(element, address, inventory, aliases)
        if ends is not None:
            yield "ends", ends

    if isinstance(element, Adapter):
        upstream = _resolve(element.spec.upstream.attached_to, address, inventory, aliases)
        if upstream is not None:
            yield "upstream", (upstream, element.spec.upstream.name)

    label = getattr(spec, "label", None)
    if label:
        # The sticker on the wire. It survives re-patching, which is precisely
        # the change that moves a cable's ends and would otherwise make one
        # re-patched lead look like a destroyed cable and a new one.
        yield "label", (str(label),)

    if isinstance(element, Device | PatchPanel | Pdu):
        slot = _slot(element)
        if slot is not None:
            yield "rack", slot

    if isinstance(element, Device):
        ports = tuple(sorted(interface.name for interface in spec.interfaces))
        if len(ports) >= 2:
            yield "ports", (element.kind, *ports)


def _ends(
    element: Cable | Tunnel,
    address: Address,
    inventory: Inventory,
    aliases: Mapping[str, str],
) -> tuple[str, ...] | None:
    """``device:interface`` for both ends, resolved and sorted.

    ``None`` when either end does not resolve: an unresolvable reference is
    ``E001``'s business, and keying on the text of a broken one would match two
    cables that are equally broken in different places.
    """
    ends: list[str] = []
    for endpoint in element.spec.endpoints:
        target = _resolve(endpoint.device, address, inventory, aliases)
        if target is None:
            return None
        ends.append(f"{target}:{endpoint.interface}")
    return tuple(sorted(ends)) if len(ends) == 2 else None


def _resolve(
    reference: str | None,
    address: Address,
    inventory: Inventory,
    aliases: Mapping[str, str],
) -> str | None:
    if not reference:
        return None
    resolution = inventory.lookup(reference, namespace=namespace_of(address.fqn))
    if resolution.fqn is None:
        return None
    return aliases.get(resolution.fqn, resolution.fqn)


def _slot(element: ElementBase) -> tuple[str, ...] | None:
    """The rack unit an element occupies, when it has one.

    A rack and a position together are a physical slot, and two things cannot be
    in one. Either on its own is not: a whole room of servers shares a site.
    """
    location = element.metadata.location
    if location is None or location.rack is None or location.position is None:
        return None
    return (
        location.site or "",
        location.room or "",
        location.rack,
        str(location.position),
    )
