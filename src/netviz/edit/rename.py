"""What else a rename has to carry, and how it should be spelled.

Renaming an element is never one document either. ``netviz edit rename A B``
has always rewritten every *reference* to ``A`` — a cable end, a tunnel's
``over``, an adapter's ``attached_to`` — but a reference is not the only place
a name is written down. Two more are, and both of them are keys rather than
values, which is why they were missed:

**Geometry** (§18) — a layout document places a node under a key that *is* the
element's address, so a rename that does not follow leaves a key naming nothing
(``W138``) and an arrangement that is silently lost: the renamed element is
drawn wherever the engine puts it, and ``netviz layout --prune`` then drops
the coordinates rather than moves them. The derived ids §18 also allows —
``hosts/adp-usb-eth#upstream``, ``tunnel:sites/hq/vx-100`` — depend on an
element the same way, so they move too.

**Annotations** (§21) — a note's anchor and an area's member list name elements,
and a stale one is ``W142``.

Both questions are already answered elsewhere and are answered here by calling
the same code: :func:`~netviz.edit.cascade.placed_element` says which element
a layout key depends on, and
:func:`~netviz.edit.cascade.annotation_references` walks a note's anchor and
an area's members. This module adds the third part, which a delete did not
need because a delete never has to write a name — the **spelling** rule.

A rename must not reformat the document it touches. ``sw-a`` written inside
``sites/hq/`` becomes ``sw-b``, not ``sites/hq/sw-b``, and a key that was
written fully qualified stays fully qualified. That is exactly the decision
:func:`~netviz.edit.references.reference_text` already makes for a reference —
keep the author's shape while it still resolves, and promote to the
fully-qualified name only when it stops. Renaming *across* namespaces is the
case where it stops: a short key that resolved outwards to one namespace may
resolve to nothing, or to something else, once the element has moved, and then
the short spelling is not a style choice but a bug.

What is deliberately not carried: an area's ``selector``. It names a *pattern*,
not an element, and a rename cannot tell whether the pattern was meant to match
the old name or merely happened to. Rewriting one would be guessing, and §21
already reports what a selector matches.

The invariant the tests hold this to, and it is the delete's invariant one
operation over: **a rename never introduces a finding**, and the renamed
element is drawn at exactly the coordinates it had before.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from netviz.edit.cascade import TUNNEL_ID_PREFIX, annotation_references, placed_element
from netviz.edit.paths import FieldPath
from netviz.edit.references import NameIndex, reference_text
from netviz.loader.inventory import Inventory, namespace_of

__all__ = [
    "RekeyedGeometry",
    "RenamePlan",
    "RepointedAnnotation",
    "plan_rename",
    "respelled_key",
]


@dataclass(frozen=True, slots=True)
class RekeyedGeometry:
    """One layout entry whose key has to be re-spelled, and how."""

    #: Fully-qualified name of the layout document holding it.
    layout: str
    view: str
    #: ``nodes`` or ``edges``. A group key is a namespace, which a rename
    #: does not touch: renaming an element never renames the folder it is in.
    section: str
    #: The key exactly as the document writes it today.
    key: str
    #: The key it becomes, in the spelling that document was already using.
    new_key: str


@dataclass(frozen=True, slots=True)
class RepointedAnnotation:
    """One note anchor or area member that has to be re-spelled, and how."""

    #: ``note`` or ``area``.
    kind: str
    #: Fully-qualified name of the annotation holding it.
    fqn: str
    #: Where in the raw document the name is written.
    path: FieldPath
    #: The reference exactly as the document writes it today.
    written: str
    #: What it becomes.
    new_written: str


@dataclass(frozen=True, slots=True)
class RenamePlan:
    """Everything one rename has to rewrite beyond the references to it."""

    #: The element's fully-qualified name before.
    old: str
    #: And after. A rename keeps the namespace; a move changes it.
    new: str
    #: Layout keys re-spelled, in document, view, section and key order.
    geometry: tuple[RekeyedGeometry, ...] = ()
    #: Note anchors and area members re-spelled.
    annotations: tuple[RepointedAnnotation, ...] = ()

    @property
    def empty(self) -> bool:
        """Is there nothing here? The ordinary case, and it must cost nothing.

        An inventory that declares no layout and no annotation — which is most
        of them — produces an empty plan, and an empty plan must leave every
        file untouched rather than write an empty block out.
        """
        return not (self.geometry or self.annotations)


def plan_rename(
    inventory: Inventory, *, old: str, new: str, index: NameIndex | None = None
) -> RenamePlan:
    """Every layout key and annotation reference ``old`` -> ``new`` has to rewrite.

    Args:
        inventory: The tree as it is now, before the rename.
        old: The element's fully-qualified name today.
        new: The fully-qualified name it is taking. For ``edit rename`` the
            namespace is the same; for ``edit move`` it is not, and that is the
            case where a short spelling may have to be promoted.
        index: The name table, if the caller already built one. It is the table
            of the tree *before* the rename; the table after is derived here,
            because deciding a spelling means asking both.

    Returns:
        A plan. Computing it changes nothing.
    """
    table = index if index is not None else NameIndex(inventory.elements)
    after = table.replaced(old, new)
    return RenamePlan(
        old=old,
        new=new,
        geometry=tuple(_rekeyed_geometry(inventory, old=old, new=new, after=after)),
        annotations=tuple(_repointed_annotations(inventory, old=old, new=new, after=after)),
    )


# --------------------------------------------------------------------------- #
# geometry (§18)
# --------------------------------------------------------------------------- #


def _rekeyed_geometry(
    inventory: Inventory, *, old: str, new: str, after: NameIndex
) -> Iterator[RekeyedGeometry]:
    """Every layout key that places ``old``, in every view of every document.

    Several layer-specific blocks is the ordinary case rather than the odd one:
    a device that has been arranged on the L1 diagram and again on the L3 one
    has two keys in two views of the same document, and both are its.
    """
    for fqn, layout in inventory.layouts.items():
        namespace = namespace_of(fqn)
        for view, geometry in layout.spec.views.items():
            for section in ("nodes", "edges"):
                for key in getattr(geometry, section):
                    if placed_element(key, inventory=inventory, namespace=namespace) != old:
                        continue
                    new_key = respelled_key(key, new=new, namespace=namespace, index=after)
                    if new_key == key:  # pragma: no cover - the names differ, so the keys do
                        continue
                    yield RekeyedGeometry(
                        layout=fqn, view=view, section=section, key=key, new_key=new_key
                    )


def respelled_key(key: str, *, new: str, namespace: str, index: NameIndex) -> str:
    """``key`` rewritten to name ``new``, keeping everything else about it.

    A layout key may decorate an address at both ends — ``tunnel:`` in front of
    a tunnel drawn as a node, ``#upstream`` behind the derived edge that hangs
    an adapter off its host — and neither decoration is part of the name. They
    are stripped, the address inside is re-spelled by the same rule a reference
    is, and they are put back.
    """
    element, separator, detail = key.partition("#")
    prefix = ""
    if element.startswith(TUNNEL_ID_PREFIX):
        prefix, element = TUNNEL_ID_PREFIX, element[len(TUNNEL_ID_PREFIX) :]
    text = reference_text(new, namespace=namespace, written=element, index=index)
    return f"{prefix}{text}{separator}{detail}"


# --------------------------------------------------------------------------- #
# annotations (§21)
# --------------------------------------------------------------------------- #


def _repointed_annotations(
    inventory: Inventory, *, old: str, new: str, after: NameIndex
) -> Iterator[RepointedAnnotation]:
    """Every note anchor and area member naming ``old``.

    One element can be named by both at once — a note pointing at the switch
    and the area drawn around the rack it is in — and each is its own document,
    so each is its own entry.
    """
    for kind, fqn, annotation in inventory.annotations:
        namespace = namespace_of(fqn)
        for written, path in annotation_references(annotation):
            if inventory.resolve_fqn(written, namespace=namespace) != old:
                continue
            text = reference_text(new, namespace=namespace, written=written, index=after)
            if text == written:  # pragma: no cover - the names differ, so the text does
                continue
            yield RepointedAnnotation(
                kind=kind, fqn=fqn, path=path, written=written, new_written=text
            )
