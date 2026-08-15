"""Resolving what a diagram annotation is *about* (§21).

Three questions, and every consumer asks at least one of them: which elements
does this area enclose, what does this note point at, and which annotations
belong in the drawing being made. They are answered here rather than in the
renderer because the validator asks them too — ``W142`` and ``W143`` are exactly
"the answer is nothing" — and a warning that disagreed with the picture would be
worse than no warning.

Nothing in this module reads or writes the graph. An annotation is
presentational: it may be *placed* by what the graph drew, and it may never
change it. See :mod:`netgraph.models.annotation` for why that is the central
promise of §21 rather than an implementation detail.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from netgraph.loader.inventory import Inventory, namespace_of
from netgraph.models import Annotation, Area, AreaSelector, Legend, Note

__all__ = [
    "AnnotationSet",
    "annotations_for_view",
    "area_members",
    "note_anchor",
    "select_area_members",
]


def select_area_members(
    inventory: Inventory, selector: AreaSelector, *, namespace: str = ""
) -> tuple[str, ...]:
    """Every element ``selector`` matches, as fully-qualified names, in load order.

    The clauses are conjunctive: an element must satisfy every one that is
    given. ``namespace`` is where the area was declared, and a relative
    ``selector.namespace`` is resolved against it, the way every other reference
    in the schema is — so an area declared in ``sites/hq`` that selects
    ``access`` means ``sites/hq/access``.

    A namespace clause matches the namespace *and everything under it*, which is
    what makes an area over a namespace the declarative form of ``--collapse``:
    the same set of elements, boxed rather than folded.
    """
    wanted = _selected_namespace(selector.namespace, namespace)
    matched: list[str] = []
    for fqn, element in inventory.elements.items():
        if wanted is not None and not _within(namespace_of(fqn), wanted):
            continue
        if selector.kinds and element.kind not in selector.kinds:
            continue
        labels = element.metadata.labels
        if any(labels.get(key) != value for key, value in selector.labels.items()):
            continue
        matched.append(fqn)
    return tuple(matched)


def _selected_namespace(clause: str | None, namespace: str) -> str | None:
    """The namespace a selector's clause means, resolved against its own.

    ``""`` is a legitimate clause — it means the root, and therefore everything —
    so the absent case is ``None`` rather than a falsy string.
    """
    if clause is None:
        return None
    clause = clause.strip("/")
    if not clause or not namespace:
        return clause
    return (
        clause
        if clause.startswith(f"{namespace}/") or clause == namespace
        else f"{namespace}/{clause}"
    )


def _within(namespace: str, wanted: str) -> bool:
    """Is ``namespace`` ``wanted`` or below it? The root contains everything."""
    if not wanted:
        return True
    return namespace == wanted or namespace.startswith(f"{wanted}/")


def area_members(inventory: Inventory, fqn: str, area: Area) -> tuple[str, ...]:
    """Every element the area encloses: the explicit list, then the selector's.

    Deduplicated, explicit names first and in the order they were written, then
    whatever the selector adds in load order. That ordering is not cosmetic: the
    hull is computed over this sequence and a diff of a rendered drawing should
    not shuffle when a device is added somewhere unrelated.

    A member that names nothing is silently dropped here and reported by the
    validator as ``W142``. Resolution failure is not this function's to report:
    it is called once per render and once per validation, and a renderer that
    raised would make a stale note fatal.
    """
    namespace = namespace_of(fqn)
    resolved: list[str] = []
    seen: set[str] = set()
    for member in area.spec.members:
        target = inventory.resolve_fqn(member, namespace=namespace)
        if target is not None and target not in seen:
            seen.add(target)
            resolved.append(target)
    if area.spec.selector is not None:
        for target in select_area_members(inventory, area.spec.selector, namespace=namespace):
            if target not in seen:
                seen.add(target)
                resolved.append(target)
    return tuple(resolved)


def note_anchor(inventory: Inventory, fqn: str, note: Note) -> str | None:
    """The fully-qualified name the note is anchored to, or ``None``.

    ``None`` covers both "it is anchored to nothing" and "it is anchored to
    something that is gone" — which is deliberate, because a renderer treats the
    two identically: draw the note where its geometry says, with no leader.
    """
    anchor = note.spec.anchor
    if anchor is None:
        return None
    return inventory.resolve_fqn(anchor.reference, namespace=namespace_of(fqn))


@dataclass(frozen=True, slots=True)
class AnnotationSet:
    """The annotations of one view, in the order they were declared.

    Kept as three tuples rather than one heterogeneous list because every
    consumer wants exactly one of them at a time: a renderer draws the areas
    first (they go behind everything), then the notes, then the legends.
    """

    notes: tuple[tuple[str, Note], ...] = ()
    areas: tuple[tuple[str, Area], ...] = ()
    legends: tuple[tuple[str, Legend], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.notes or self.areas or self.legends)

    def __iter__(self) -> Iterator[tuple[str, Annotation]]:
        """Every annotation, areas first: the order a renderer draws them in."""
        yield from self.areas
        yield from self.notes
        yield from self.legends

    @property
    def count(self) -> int:
        return len(self.notes) + len(self.areas) + len(self.legends)


def annotations_for_view(inventory: Inventory, view: str) -> AnnotationSet:
    """The annotations that belong in the ``view`` drawing.

    ``view`` is a layer name — the same closed set §18 scopes geometry by. An
    annotation with no ``spec.views`` appears in every drawing, which is what
    somebody writing a note about a site wants; one that lists views appears
    only in those.
    """
    return AnnotationSet(
        notes=_scoped(inventory.notes.items(), view),
        areas=_scoped(inventory.areas.items(), view),
        legends=_scoped(inventory.legends.items(), view),
    )


_T = TypeVar("_T", Note, Area, Legend)


def _scoped(items: Iterable[tuple[str, _T]], view: str) -> tuple[tuple[str, _T], ...]:
    return tuple((fqn, item) for fqn, item in items if item.spec.draws_in(view))
