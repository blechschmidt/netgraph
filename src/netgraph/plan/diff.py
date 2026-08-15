"""The diff engine: two loaded inventories in, one ordered changeset out.

:func:`diff` is a pure function. It reads no file, contacts no host, consults no
clock and mutates neither argument, which is what makes a plan reproducible and
what makes the whole of this package testable without a filesystem. Everything
that *is* impure — reading a git ref, running a capture, writing a plan file —
lives in :mod:`netgraph.plan.sources`, :mod:`netgraph.plan.live` and the two
commands.

The engine does four things, in this order:

1. Address every element on both sides (:mod:`netgraph.plan.address`).
2. Pair up the ones that are only on one side, structurally, so a rename is
   reported as a rename (:mod:`netgraph.plan.identity`).
3. Diff the bodies of everything that survived the pairing
   (:mod:`netgraph.plan.document`).
4. Put the result in dependency order (:mod:`netgraph.plan.order`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from netgraph.loader.inventory import Inventory
from netgraph.models import ElementBase
from netgraph.plan.address import (
    ANNOTATION_TYPES,
    LAYOUT_TYPE,
    SIDECAR_TYPES,
    Address,
    address_of,
)
from netgraph.plan.document import Diffable, body_of, diff_documents, document_of
from netgraph.plan.identity import detect_renames
from netgraph.plan.model import Action, Change, Plan, StateRef
from netgraph.plan.order import dependencies, order_changes

__all__ = ["diff", "elements_by_address"]

#: What either side of a diff holds: every addressable document of one state.
Documents = dict[Address, Diffable]


def diff(
    before: Inventory,
    after: Inventory,
    *,
    source: StateRef | None = None,
    target: StateRef | None = None,
    renames: bool = True,
) -> Plan:
    """The changeset that turns ``before`` into ``after``.

    Args:
        before: The state the changes will be applied to.
        after: The state they bring it to.
        source: How to describe ``before`` in the plan. Defaults to an anonymous
            reference, which is what a test wants and what a caller with nothing
            to say about provenance gets.
        target: How to describe ``after``.
        renames: Detect renames structurally. Turning it off makes every rename
            a delete plus a create, which is occasionally what an operator wants
            to see and never what they want to apply.

    Returns:
        An ordered :class:`~netgraph.plan.model.Plan`. Empty when the two states
        describe the same network, whatever the two trees look like on disk.
    """
    old = elements_by_address(before)
    new = elements_by_address(after)
    common = old.keys() & new.keys()
    deleted = {address: element for address, element in old.items() if address not in common}
    created = {address: element for address, element in new.items() if address not in common}

    pairs = (
        detect_renames(
            before,
            after,
            deleted=_elements_only(deleted),
            created=_elements_only(created),
        )
        if renames
        else {}
    )

    changes: list[Change] = []
    for address in sorted(common, key=lambda item: item.order):
        changes.extend(_updated(address, old[address], new[address], before))
    for old_address, new_address in pairs.items():
        changes.append(
            Change(
                action=Action.RENAME,
                address=old_address,
                kind=new[new_address].kind,
                new_address=new_address,
                source=_source_of(before, old_address),
            )
        )
        changes.extend(
            _updated(new_address, old[old_address], new[new_address], before, at=old_address)
        )
    for address, element in deleted.items():
        if address in pairs:
            continue
        changes.append(
            Change(
                action=Action.DELETE,
                address=address,
                kind=element.kind,
                document=document_of(element),
                source=_source_of(before, address),
            )
        )
    for address, element in created.items():
        if address in pairs.values():
            continue
        changes.append(
            Change(
                action=Action.CREATE,
                address=address,
                kind=element.kind,
                document=document_of(element),
            )
        )

    return Plan(
        changes=order_changes(changes, before=dependencies(before), after=dependencies(after)),
        source=source if source is not None else StateRef(kind="tree", description="source"),
        target=target if target is not None else StateRef(kind="tree", description="target"),
    )


def elements_by_address(inventory: Inventory) -> Documents:
    """Every addressable document of ``inventory``, keyed by address.

    Elements, layouts and annotations together: a diagram's geometry and the
    notes written on it are as much a part of the declared state as the devices
    they are about, and a plan that silently ignored them would let ``apply``
    leave a tree the plan said it had finished with.

    Each sidecar keeps its own name space. A note called ``dmz`` and an area
    called ``dmz`` and a device called ``dmz`` are three documents at three
    addresses, which is what stops any of them being mistaken for another.
    """
    documents: Documents = {
        address_of(element.kind, fqn): element for fqn, element in inventory.elements.items()
    }
    for fqn, layout in inventory.layouts.items():
        documents[Address(type=LAYOUT_TYPE, fqn=fqn)] = layout
    for kind, fqn, annotation in inventory.annotations:
        documents[address_of(kind, fqn)] = annotation
    return documents


def _updated(
    address: Address,
    old: Diffable,
    new: Diffable,
    before: Inventory,
    *,
    at: Address | None = None,
) -> Iterator[Change]:
    """The ``update`` entry for one element, when anything about it differs.

    ``at`` is where the element is *now*, when that is not where the update
    lands: the field changes of a renamed element are reported against the new
    address, but the file they were read from is the old one's.
    """
    fields = diff_documents(body_of(old), body_of(new))
    if not fields:
        return
    yield Change(
        action=Action.UPDATE,
        address=address,
        kind=new.kind,
        fields=fields,
        # Geometry is applied a whole view at a time, so a layout update has to
        # carry the view it wants rather than the deltas that got it there. Every
        # other kind is applied field by field and needs no such thing — an
        # annotation included: a note's colour is written at ``spec.color``, so
        # carrying the whole document would only cost the reader the one line
        # that says what actually changed.
        #
        # That holds even though §21 re-checks an annotation after every write,
        # which makes a half-written block a refusal. A block that is wholly new
        # is already *one* field change here — ``_walk`` only descends into a
        # mapping when both sides have one — so its change carries the block
        # entire; and where a plan spells the leaves separately anyway,
        # ``plan.execute`` grafts them back together against the tree it is
        # writing to. Neither needs the document the change came from.
        document=document_of(new) if address.type == LAYOUT_TYPE else None,
        source=_source_of(before, at if at is not None else address),
    )


def _elements_only(documents: Mapping[Address, Diffable]) -> dict[Address, ElementBase]:
    """Drop the sidecars: they carry nothing structural to be identified by.

    A layout is named by its author and describes elements by *their* names. Two
    of them with different names are two arrangements, and pairing them up on a
    guess would move somebody's saved diagram onto another view.

    An annotation is the same argument with more at stake. Its identity is the
    name somebody gave it and nothing else — two notes both anchored to the same
    switch are two things a person wrote, not one thing renamed — so a note that
    disappears and another that appears are a delete and a create, always.
    Guessing otherwise would silently move one author's words onto another
    element.
    """
    return {
        address: element
        for address, element in documents.items()
        if isinstance(element, ElementBase) and address.type not in SIDECAR_TYPES
    }


def _source_of(inventory: Inventory, address: Address) -> str | None:
    if address.type in ANNOTATION_TYPES:
        location = inventory.annotation_source(address.type, address.fqn)
    elif address.type == LAYOUT_TYPE:
        location = inventory.layout_sources.get(address.fqn)
    else:
        location = inventory.sources.get(address.fqn)
    return None if location is None else str(location)
