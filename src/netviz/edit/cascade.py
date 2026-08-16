"""What else a delete has to take, and what it must not.

Deleting an element is never one document. A cable with one end missing is not
a cable, a note anchored to a switch that is gone points at nothing, and the
coordinates that placed the switch are a line in a layout file naming something
the inventory no longer declares. Three different layers, one gesture — and the
person who pressed Delete meant all of it.

So the closure is taken over all three, and every entry in it is justified by a
rule that already exists somewhere else in netviz:

**Elements** — :mod:`~netviz.edit.references` says which references are
*structural* (a link end, a tunnel's carrier). A structural reference dies with
its target, transitively: a VXLAN over an IPsec tunnel over a cable goes when
the device at the end of the cable goes. Everything else is *optional* — an
adapter's ``attached_to``, a power input, a group membership — and is cleared
where it is written, because an adapter that loses its host is still an adapter.

**Annotations** (§21) — the same distinction, decided by §21's own coherence
rules rather than by a table here. A note anchored to a doomed element survives
if it carries a point of its own, because then dropping the anchor leaves a
document the schema accepts; a note that is *only* anchored cannot be drawn
without it and goes. An area drops the doomed members and survives if members,
a selector or a rectangle remain. In other words: the cascade removes an
annotation exactly when clearing the reference would leave one the loader would
refuse.

**Geometry** (§18) — a layout key naming a doomed element, and a group key
naming a namespace the delete empties. Never anything else: this is not
``netviz layout --prune``, which drops every key the current *drawing* lacks
and would therefore throw away the position of a device merely filtered out of
the view. A cascade removes the geometry of what it is itself removing, which is
a question about the inventory and not about any one picture.

The invariant the tests hold this to: **a cascading delete never introduces a
finding.** ``W138`` (stale geometry), ``W142`` (stale annotation) and the §21
coherence rules are exactly the diagnostics a half-done delete would produce,
and a tree that validated clean before one still does after.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from netviz.edit.paths import FieldPath
from netviz.edit.references import NameIndex, Reference, ReferenceRole, dependents_of
from netviz.layout.resolve import resolve_key
from netviz.loader.inventory import Inventory, namespace_of, qualify
from netviz.models.annotation import Annotation, Area, Note

#: Prefix of a tunnel node's id, from :mod:`netviz.render.graph`. Spelled out
#: rather than imported: the write path must not pull the renderer in, and this
#: is one four-character constant against a module graph twice the size.
TUNNEL_ID_PREFIX: Final = "tunnel:"

__all__ = [
    "CascadePlan",
    "Casualty",
    "ClearedReference",
    "DoomedAnnotation",
    "StaleGeometry",
    "placed_element",
    "plan_cascade",
]


@dataclass(frozen=True, slots=True)
class Casualty:
    """One thing a delete takes with it, said the way a person would say it."""

    #: What goes. An element's fully-qualified name, or an annotation's.
    address: str
    #: ``device``, ``cable``, ``tunnel``, ``note``, ``area`` — the document kind.
    kind: str
    #: Why it cannot stay, as a phrase that completes "it goes because …".
    reason: str


@dataclass(frozen=True, slots=True)
class ClearedReference:
    """A survivor losing one reference to something that is going."""

    #: The element or annotation holding the reference.
    holder: str
    kind: str
    #: Where in the raw document the reference is written.
    path: FieldPath
    #: What is being dropped, for a message: ``attached_to``, ``member``, ...
    what: str
    #: ``None`` for an annotation, whose references are not :class:`Reference`s.
    reference: Reference | None = None


@dataclass(frozen=True, slots=True)
class DoomedAnnotation:
    """An annotation the cascade removes whole, with where it lives."""

    kind: str
    fqn: str
    reason: str


@dataclass(frozen=True, slots=True)
class StaleGeometry:
    """One layout entry that would place something the delete removes."""

    #: Fully-qualified name of the layout document holding it.
    layout: str
    view: str
    #: ``nodes``, ``edges`` or ``groups``.
    section: str
    #: The key exactly as the document writes it.
    key: str


@dataclass(frozen=True, slots=True)
class CascadePlan:
    """Everything one delete does, computed before any of it is done."""

    #: What the caller named, fully qualified.
    asked: tuple[str, ...] = ()
    #: Every element removed, including :attr:`asked`, in name order.
    elements: tuple[str, ...] = ()
    #: The elements beyond :attr:`asked`, with why each one goes.
    collateral: tuple[Casualty, ...] = ()
    #: Annotations removed whole.
    annotations: tuple[DoomedAnnotation, ...] = ()
    #: References dropped from documents that survive.
    cleared: tuple[ClearedReference, ...] = ()
    #: Layout entries dropped.
    geometry: tuple[StaleGeometry, ...] = ()

    @property
    def takes_more(self) -> bool:
        """Does this delete remove or change anything beyond what was named?

        The question the editor asks before it decides whether to confirm: a
        delete that takes exactly what you pointed at needs no second thought,
        and one that takes six cables and a note does.
        """
        return bool(self.collateral or self.annotations or self.cleared)

    @property
    def dependents(self) -> tuple[str, ...]:
        """Every name that has to change for this delete, for a refusal message.

        Geometry is deliberately absent: coordinates are not a dependency, and
        "you cannot delete this switch, something draws it" is not a sentence
        anybody should have to read.
        """
        return tuple(
            sorted(
                {casualty.address for casualty in self.collateral}
                | {f"{doomed.kind} {doomed.fqn}" for doomed in self.annotations}
                | {reference.holder for reference in self.cleared}
            )
        )

    def to_dict(self) -> dict[str, object]:
        """The plan as JSON, which is how the browser is told what it is about to do."""
        return {
            "asked": list(self.asked),
            "elements": [
                {"address": casualty.address, "kind": casualty.kind, "reason": casualty.reason}
                for casualty in self.collateral
            ],
            "annotations": [
                {"address": doomed.fqn, "kind": doomed.kind, "reason": doomed.reason}
                for doomed in self.annotations
            ],
            "cleared": [
                {"address": reference.holder, "kind": reference.kind, "what": reference.what}
                for reference in self.cleared
            ],
            "geometry": [
                {"layout": entry.layout, "view": entry.view, "key": entry.key}
                for entry in self.geometry
            ],
        }


#: What a cleared reference is called in a message, per role. The words are the
#: field names, because that is what the person will see in the file afterwards.
_CLEARED_WHAT: Final[Mapping[ReferenceRole, str]] = {
    ReferenceRole.ATTACHED_TO: "attached_to",
    ReferenceRole.POWER_INPUT: "a power input",
    ReferenceRole.MEMBER: "a member",
}


def plan_cascade(
    inventory: Inventory, targets: Iterable[str], *, index: NameIndex | None = None
) -> CascadePlan:
    """Everything deleting ``targets`` removes, changes, or leaves behind.

    Args:
        inventory: The tree as it is now.
        targets: Fully-qualified names of the elements being deleted.
        index: The name table, if the caller already built one.

    Returns:
        A plan. Computing it changes nothing; :mod:`~netviz.edit.apply` is
        what carries it out, and the editor is what shows it to somebody first.
    """
    asked = tuple(sorted(dict.fromkeys(targets)))
    table = index if index is not None else NameIndex(inventory.elements)
    doomed, collateral, cleared = _element_closure(inventory, asked, table)
    annotations, annotation_refs = _annotation_closure(inventory, doomed)
    return CascadePlan(
        asked=asked,
        elements=tuple(sorted(doomed)),
        collateral=tuple(collateral),
        annotations=tuple(annotations),
        cleared=tuple(cleared) + tuple(annotation_refs),
        geometry=tuple(_stale_geometry(inventory, doomed)),
    )


# --------------------------------------------------------------------------- #
# elements
# --------------------------------------------------------------------------- #


def _element_closure(
    inventory: Inventory, asked: Sequence[str], index: NameIndex
) -> tuple[set[str], list[Casualty], list[ClearedReference]]:
    """The transitive set of elements that cannot survive ``asked``.

    A link dies with either of its ends and a tunnel with the tunnel it runs
    inside, so this is a closure rather than one pass: deleting the device at
    the end of the cable an IPsec tunnel runs over takes the VXLAN inside it
    too.
    """
    doomed = set(asked)
    pending = list(doomed)
    collateral: list[Casualty] = []
    cleared: list[ClearedReference] = []
    while pending:
        target = pending.pop()
        for reference in dependents_of(target, inventory.elements, index):
            if reference.source in doomed:
                continue
            source = inventory.elements[reference.source]
            if reference.role.is_structural:
                doomed.add(reference.source)
                pending.append(reference.source)
                collateral.append(
                    Casualty(
                        address=reference.source,
                        kind=source.kind,
                        reason=_structural_reason(reference, target),
                    )
                )
            else:
                cleared.append(
                    ClearedReference(
                        holder=reference.source,
                        kind=source.kind,
                        path=reference.path,
                        what=_CLEARED_WHAT.get(reference.role, reference.role.value),
                        reference=reference,
                    )
                )
    collateral.sort(key=lambda casualty: casualty.address)
    # A survivor may hold a reference to something that turned out to be doomed
    # itself, and then the whole document goes rather than one field of it.
    kept = [reference for reference in cleared if reference.holder not in doomed]
    kept.sort(key=lambda reference: (reference.holder, reference.path))
    return doomed, collateral, kept


def _structural_reason(reference: Reference, target: str) -> str:
    if reference.role is ReferenceRole.OVER:
        return f"it runs over {target}"
    end = f"{target}:{reference.detail}" if reference.detail else target
    return f"one end of it is {end}"


# --------------------------------------------------------------------------- #
# annotations (§21)
# --------------------------------------------------------------------------- #


def _annotation_closure(
    inventory: Inventory, doomed: set[str]
) -> tuple[list[DoomedAnnotation], list[ClearedReference]]:
    """Which annotations go, and which merely lose a name.

    The rule is §21's own: an annotation goes when clearing its doomed
    references would leave a document the loader refuses. Nothing here decides
    that independently — :meth:`_note_survives` and :meth:`_area_survives` are
    the model's validators read the other way round, and the tests check them
    against the models rather than against each other.
    """
    removed: list[DoomedAnnotation] = []
    cleared: list[ClearedReference] = []
    for kind, fqn, annotation in inventory.annotations:
        namespace = namespace_of(fqn)
        hit = [
            (written, path)
            for written, path in annotation_references(annotation)
            if inventory.resolve_fqn(written, namespace=namespace) in doomed
        ]
        if not hit:
            continue
        if _survives(annotation, dropping=len(hit)):
            cleared.extend(
                ClearedReference(
                    holder=fqn,
                    kind=kind,
                    path=path,
                    what="its anchor" if isinstance(annotation, Note) else "a member",
                )
                for _, path in hit
            )
            continue
        removed.append(
            DoomedAnnotation(kind=kind, fqn=fqn, reason=_annotation_reason(annotation, hit))
        )
    removed.sort(key=lambda doomed_annotation: (doomed_annotation.kind, doomed_annotation.fqn))
    cleared.sort(key=lambda reference: (reference.holder, reference.path))
    return removed, cleared


def annotation_references(annotation: Annotation) -> Iterator[tuple[str, FieldPath]]:
    """Every element an annotation names, with the raw path that named it.

    The same walk :func:`netviz.validate._annotation_references` makes — this
    is the write side of that read, and a legend still names nothing.
    """
    if isinstance(annotation, Note):
        if annotation.spec.anchor is not None:
            anchor = annotation.spec.anchor
            key = "element" if anchor.element is not None else "link"
            yield anchor.reference, ("spec", "anchor", key)
    elif isinstance(annotation, Area):
        for position, member in enumerate(annotation.spec.members):
            yield member, ("spec", "members", position)


def _survives(annotation: Annotation, *, dropping: int) -> bool:
    """Would the document still load with ``dropping`` of its references gone?"""
    if isinstance(annotation, Note):
        geometry = annotation.spec.geometry
        return geometry is not None and geometry.placed
    if isinstance(annotation, Area):
        spec = annotation.spec
        geometry = spec.geometry
        boxed = geometry is not None and geometry.placed and geometry.sized
        return bool(len(spec.members) - dropping) or spec.selector is not None or boxed
    return True  # pragma: no cover - a legend names nothing, so it is never reached


def _annotation_reason(annotation: Annotation, hit: Sequence[tuple[str, FieldPath]]) -> str:
    named = ", ".join(sorted({written for written, _ in hit}))
    if isinstance(annotation, Note):
        return f"it is anchored to {named} and is placed nowhere else"
    return f"it encloses {named} and would enclose nothing"


# --------------------------------------------------------------------------- #
# geometry (§18)
# --------------------------------------------------------------------------- #


def _stale_geometry(inventory: Inventory, doomed: set[str]) -> Iterator[StaleGeometry]:
    """Every layout entry that would place something this delete removes.

    Group keys are namespaces, and a namespace is emptied by a delete rather
    than deleted by one — so the surviving namespaces are computed and a key
    naming none of them goes. A namespace that still holds one device keeps its
    box, including the boxes of its parents.
    """
    surviving = _surviving_namespaces(inventory, doomed)
    for fqn, layout in inventory.layouts.items():
        namespace = namespace_of(fqn)
        for view, geometry in layout.spec.views.items():
            for section in ("nodes", "edges"):
                for key in getattr(geometry, section):
                    if placed_element(key, inventory=inventory, namespace=namespace) in doomed:
                        yield StaleGeometry(layout=fqn, view=view, section=section, key=key)
            for key in geometry.groups:
                if qualify(namespace, key) not in surviving:
                    yield StaleGeometry(layout=fqn, view=view, section="groups", key=key)


def placed_element(key: str, *, inventory: Inventory, namespace: str) -> str:
    """The element a layout key depends on, or the key itself if it is not one.

    Three spellings reach here, and two of them are §18's derived ids:

    ``switches/sw-core``
        A plain address, resolved the way every reference is.
    ``hosts/adp-usb-eth#upstream``
        A *derived edge*: no document declares it, but it exists because that
        adapter does, and it is drawn only for as long as the adapter is.
    ``tunnel:sites/hq/vx-100``
        A tunnel drawn as a node rather than as an edge. The node is derived,
        the element behind it is not.

    ``subnet:`` and ``rack:`` deliberately fall through as themselves. Those
    nodes exist because of what the *surviving* elements say — an address in a
    prefix, a location in a rack — and whether the last one has gone is a
    question for the layout engine, which is what ``netviz layout --prune``
    runs. Answering it by guessing here would throw away the position of a rack
    that still holds three devices.
    """
    element = key.partition("#")[0]
    if element.startswith(TUNNEL_ID_PREFIX):
        element = element[len(TUNNEL_ID_PREFIX) :]
    return resolve_key(element, inventory=inventory, namespace=namespace)


def _surviving_namespaces(inventory: Inventory, doomed: set[str]) -> frozenset[str]:
    """Every namespace still holding an element once ``doomed`` is gone, with parents."""
    names: set[str] = set()
    for fqn in inventory.elements:
        if fqn in doomed:
            continue
        scope = namespace_of(fqn)
        while scope and scope not in names:
            names.add(scope)
            scope = namespace_of(scope)
    return frozenset(names)


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def describe(plan: CascadePlan) -> str:
    """What the plan takes beyond what was asked for, as one clause.

    Empty when it takes nothing else, so a caller can write
    ``f"deleted {address}{describe(plan)}"`` and get the right sentence either
    way.
    """
    parts = [
        _plural(len(plan.collateral), "element"),
        _plural(len(plan.annotations), "annotation"),
        _plural(len(plan.cleared), "reference", verb="cleared"),
        _plural(len(plan.geometry), "geometry entry", plural="geometry entries"),
    ]
    said = [part for part in parts if part]
    if not said:
        return ""
    return ", and " + _joined(said)


def _plural(count: int, noun: str, *, plural: str | None = None, verb: str = "") -> str:
    if not count:
        return ""
    word = noun if count == 1 else (plural or f"{noun}s")
    return f"{count} {word}{' ' + verb if verb else ''}"


def _joined(parts: Sequence[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]
