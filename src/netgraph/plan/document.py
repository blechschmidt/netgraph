"""The normalised form an element is diffed in, and the diff itself.

Two questions, answered in one place because the answer to the second depends
entirely on the answer to the first.

**What is compared.** Not the file. A plan is a diff of *meaning*, so an element
is compared as the model the loader built for it, dumped back to plain data:
templates already merged, interface ranges already expanded, defaults already
filled in, every scalar already in its canonical form. Two trees that spell the
same network differently produce an empty plan, which is what makes ``netgraph
plan --from HEAD`` usable on a tree somebody has just run ``netgraph fmt`` over.

**How it is compared.** Recursively, mapping by key, with one refinement that
does all the work: a list of mappings that carry a common unique identifier is
matched *by that identifier* rather than by position. Adding an interface to the
front of ``spec.interfaces`` is then one added entry, not one added entry and
eleven renumbered ones. The identifier also becomes the path a change is
reported at — ``spec.interfaces[name=eth0].mtu`` — which is stable enough to be
stored in a plan file and resolved against the tree later.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from netgraph.models import AnnotationBase, ElementBase, Layout
from netgraph.plan.model import FieldChange
from netgraph.plan.paths import MISSING, Selector, Step

__all__ = ["KEYED_LISTS", "Diffable", "body_of", "diff_documents", "document_of"]

#: Everything a plan can be about: the elements, and the sidecars that describe
#: how they are drawn. All three are ordinary documents with an envelope and a
#: ``spec``, so one normalisation and one diff serve all of them.
Diffable = ElementBase | Layout | AnnotationBase

#: Lists whose entries are matched by an identifier of their own rather than by
#: position, and the field that identifies one.
#:
#: The list is short, and short on purpose. Matching by key buys a readable diff
#: — one added interface instead of one addition and eleven renumberings — but a
#: change reported *at* an entry has to be executable at that entry, and the
#: write path can only do that where it has an operation for it:
#: ``AddInterface`` and ``RemoveInterface`` (§6.2). Everywhere else a list is
#: compared whole and written whole, which is always expressible and is what a
#: reader of ``spec.endpoints`` or an address list wants to see anyway.
KEYED_LISTS: Final[dict[tuple[str, ...], str]] = {("spec", "interfaces"): "name"}

#: Envelope keys that identify the *document* rather than describe the element.
#: They are compared through the address, never as fields: ``metadata.name`` is
#: the subject of a rename, not a field of it.
_IDENTITY_PATHS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {("metadata", "name"), ("apiVersion",)}
)


def document_of(element: Diffable) -> dict[str, Any]:
    """The element as plain data, in the shape its YAML document has.

    Empty mappings and lists are dropped. Pydantic materialises every optional
    container — ``labels``, ``annotations`` — whether the author wrote one or
    not, and carrying them would make every ``create`` entry in a plan print
    four lines of nothing.
    """
    dumped = element.model_dump(mode="json", by_alias=True, exclude_none=True)
    pruned = _prune(dumped)
    return pruned if isinstance(pruned, dict) else {}


def body_of(element: Diffable) -> dict[str, Any]:
    """:func:`document_of` without the keys that make it *this* document.

    What is left is everything a diff may speak about. ``kind`` stays: a switch
    becoming a router is a change to the element, not a different element.
    """
    document = document_of(element)
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        document["metadata"] = {key: value for key, value in metadata.items() if key != "name"}
        if not document["metadata"]:
            del document["metadata"]
    document.pop("apiVersion", None)
    return document


def _prune(value: Any) -> Any:
    """Drop empty containers, recursively."""
    if isinstance(value, Mapping):
        out = {key: _prune(item) for key, item in value.items()}
        return {key: item for key, item in out.items() if not _is_empty(item)}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_prune(item) for item in value]
    return value


def _is_empty(value: Any) -> bool:
    return isinstance(value, dict | list) and not value


def diff_documents(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[FieldChange, ...]:
    """Every field that differs between two element bodies, in document order."""
    return tuple(_walk(before, after, ()))


def _walk(before: Any, after: Any, steps: tuple[Step, ...]) -> Iterator[FieldChange]:
    if steps in _IDENTITY_PATHS:
        return
    if before is not MISSING and after is not MISSING and before == after:
        return
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        yield from _walk_mapping(before, after, steps)
        return
    if _is_list(before) and _is_list(after):
        identifier = _identifier(before, after, steps)
        if identifier is not None:
            yield from _walk_keyed(before, after, steps, identifier)
            return
    yield FieldChange(path=steps, before=before, after=after)


def _walk_mapping(
    before: Mapping[str, Any], after: Mapping[str, Any], steps: tuple[Step, ...]
) -> Iterator[FieldChange]:
    """Keys of ``after`` in its own order, then whatever only ``before`` had.

    The order matters for readability: a reader of a plan follows the document
    they are moving *towards*, and removals collect at the end where they are
    obvious rather than scattered through it.
    """
    for key in after:
        yield from _walk(before.get(key, MISSING), after[key], (*steps, key))
    for key in before:
        if key not in after:
            yield from _walk(before[key], MISSING, (*steps, key))


def _walk_keyed(
    before: Sequence[Any], after: Sequence[Any], steps: tuple[Step, ...], identifier: str
) -> Iterator[FieldChange]:
    old = {_key_of(entry, identifier): entry for entry in before}
    new = {_key_of(entry, identifier): entry for entry in after}
    for key, entry in new.items():
        selector: Step = Selector(key=identifier, value=key)
        yield from _walk(old.get(key, MISSING), entry, (*steps, selector))
    for key, entry in old.items():
        if key not in new:
            yield FieldChange(path=(*steps, Selector(key=identifier, value=key)), before=entry)


def _is_list(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _identifier(before: Sequence[Any], after: Sequence[Any], steps: tuple[Step, ...]) -> str | None:
    """The field that names an entry of both lists, or ``None``.

    The list has to be one of :data:`KEYED_LISTS`, and the key has to be present
    on every entry of both sides and unique within each. Anything less and
    matching by it would silently drop or merge an entry, which is worse than
    falling back to comparing the lists whole.
    """
    candidate = KEYED_LISTS.get(_shape(steps))
    if candidate is None:
        return None
    entries = [*before, *after]
    if not entries or not all(isinstance(entry, Mapping) for entry in entries):
        return None
    if not all(candidate in entry for entry in entries):
        return None
    if not all(_is_unique(side, candidate) for side in (before, after)):
        return None
    return candidate


def _shape(steps: tuple[Step, ...]) -> tuple[str, ...]:
    """The path with its selectors and indices dropped, for the lookup above."""
    return tuple(step for step in steps if isinstance(step, str))


def _is_unique(entries: Sequence[Any], identifier: str) -> bool:
    keys = [_key_of(entry, identifier) for entry in entries]
    return len(set(keys)) == len(keys)


def _key_of(entry: Any, identifier: str) -> str:
    value = entry[identifier]
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
