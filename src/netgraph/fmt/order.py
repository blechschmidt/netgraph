"""Canonical key order, derived from the models rather than written out again.

``docs/format.md`` says mapping keys are ordered "to match the field order in
``docs/schema.md``". That order is already encoded twice — once in the prose
table of ``docs/schema.md``, once in the field declaration order of the pydantic
models — and ``tests/test_docs.py`` keeps the two in step. Writing it out a
third time here would give the formatter its own copy to drift away from, so
this module *reads* it off the models instead: add a field to
:class:`~netgraph.models.interface.Interface` and the formatter places it
without being told.

The result is a :data:`Shape` tree — a mapping shape knows the order of its
keys and the shape of each value, a sequence shape knows the shape of its items,
and :data:`OPAQUE` marks a node whose keys are the user's own (``labels``,
``annotations``) and must therefore keep the order they were written in.

Two keys exist in YAML but not on any model, because the loader consumes them
before pydantic ever sees a document (``docs/schema.md`` §6.5, §6.6):

* ``spec.from`` — the template a device inherits from.
* ``spec.interfaces[].range`` — the range an interface entry expands into.

They are spliced in at the position ``docs/schema.md`` documents them at, and
:func:`check_order` asserts that every other key of every shape came from a
model, so a rename cannot leave a stale entry behind.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, TypeAlias

from netgraph.models.base import NetgraphModel
from netgraph.models.device import DeviceSpec
from netgraph.models.document import ELEMENT_MODELS
from netgraph.models.element import ElementBase
from netgraph.models.template import INHERIT_KEY, Template
from netgraph.models.testsuite import TestSuite

__all__ = [
    "ENVELOPE_ORDER",
    "LOADER_KEYS",
    "OPAQUE",
    "MappingShape",
    "SequenceShape",
    "Shape",
    "check_order",
    "document_shape",
    "order_keys",
]

#: ``docs/schema.md`` §3 — the four envelope keys, in the order they are
#: documented. Taken from :class:`ElementBase` plus ``spec``, which each
#: concrete element model adds for itself.
ENVELOPE_ORDER: Final[tuple[str, ...]] = ("apiVersion", "kind", "metadata", "spec")

#: The interface key the loader expands and removes (§6.5), and where it goes.
_RANGE_KEY: Final = "range"
_RANGE_AFTER: Final = "name"

#: Where ``spec.from`` (§6.6) sits in a device spec.
_INHERIT_AFTER: Final = "interfaces"

#: The two keys that are YAML but not model fields, spliced in by
#: :func:`_with_loader_keys`. :func:`check_order` allows exactly these.
LOADER_KEYS: Final[frozenset[str]] = frozenset({INHERIT_KEY, _RANGE_KEY})


@dataclass(frozen=True, slots=True)
class MappingShape:
    """A mapping whose keys have a documented order."""

    #: Every key the schema knows, in canonical order.
    order: tuple[str, ...]
    #: The shape of each known key's value. A key absent here is :data:`OPAQUE`.
    children: Mapping[str, Shape]

    def child(self, key: str) -> Shape:
        """The shape of ``key``'s value, or :data:`OPAQUE` for an unknown key."""
        return self.children.get(key, OPAQUE)


@dataclass(frozen=True, slots=True)
class SequenceShape:
    """A sequence whose items all share one shape."""

    item: Shape


class _Opaque:
    """A node the schema says nothing about. Its contents keep document order."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "OPAQUE"


#: Free-form data: ``metadata.labels``, ``metadata.annotations``, and anything
#: below a key no model declares. Never reordered.
OPAQUE: Final = _Opaque()

#: What :func:`document_shape` returns and :mod:`netgraph.fmt.canonical` walks.
Shape: TypeAlias = MappingShape | SequenceShape | _Opaque


def order_keys(present: typing.Iterable[str], shape: Shape) -> list[str]:
    """Order the keys actually present in a mapping.

    Keys the schema knows come first, in schema order; the rest follow in the
    order they were written. Putting an unknown key last rather than trying to
    keep it near its neighbours is what makes the result idempotent: a second
    pass over the output has to produce the same list, and "where it was" is not
    a property the output preserves.
    """
    keys = list(present)
    if not isinstance(shape, MappingShape):
        return keys
    known = set(shape.order)
    seen = set(keys)
    ordered = [key for key in shape.order if key in seen]
    ordered.extend(key for key in keys if key not in known)
    return ordered


def document_shape(kind: object) -> Shape:
    """The shape of a whole document declaring ``kind``.

    ``kind`` is whatever the document's ``kind`` key held, which for a malformed
    document is anything at all — an int, a list, or missing entirely. Anything
    that is not a kind netgraph knows yields the bare envelope shape, so a
    document nobody can classify still gets ``apiVersion``/``kind``/``metadata``/
    ``spec`` ordered and nothing below them touched.
    """
    if isinstance(kind, str):
        shape = _DOCUMENT_SHAPES.get(kind)
        if shape is not None:
            return shape
    return _ENVELOPE_SHAPE


def check_order() -> list[str]:
    """Report every key of every shape that no model declares.

    The shapes are read off the models, so the only way an unexplained key can
    appear is :func:`_with_loader_keys` splicing one in — which is deliberate
    for the two in :data:`LOADER_KEYS` and a bug for anything else. Run from
    ``tests/test_fmt.py``; a non-empty result is a failure.
    """
    declared: set[str] = set()
    for model in _reachable_models():
        declared.update(_field_names(model))
    problems: list[str] = []
    for kind, shape in sorted(_DOCUMENT_SHAPES.items()):
        for key in sorted(_walk_keys(shape)):
            if key not in declared and key not in LOADER_KEYS:
                problems.append(f"{kind}: {key!r} is ordered but no model declares it")
    return problems


def _reachable_models() -> list[type[NetgraphModel]]:
    """Every model a document can reach, for :func:`check_order`.

    Built by running the shape builder for its side effect on ``memo``, whose
    keys are exactly the models it recursed into.
    """
    roots: list[type[NetgraphModel]] = [ElementBase, Template, TestSuite, *ELEMENT_MODELS]
    memo: dict[Any, Shape] = {}
    for model in roots:
        _model_shape(model, memo)
    found = [key for key in memo if isinstance(key, type) and issubclass(key, NetgraphModel)]
    return [*roots, *found]


def _walk_keys(shape: Shape) -> set[str]:
    """Every key named anywhere below ``shape``."""
    keys: set[str] = set()
    stack: list[Shape] = [shape]
    seen: list[Shape] = []
    while stack:
        current = stack.pop()
        if any(current is other for other in seen):
            continue
        seen.append(current)
        if isinstance(current, MappingShape):
            keys.update(current.order)
            stack.extend(current.children.values())
        elif isinstance(current, SequenceShape):
            stack.append(current.item)
    return keys


# ---------------------------------------------------------------------------
# Building the shapes out of the models
# ---------------------------------------------------------------------------


def _field_names(model: type[NetgraphModel]) -> tuple[str, ...]:
    """The YAML keys of ``model``, in declaration order.

    Only ``apiVersion`` carries an alias (see ``netgraph.models.base``), but
    reading the alias rather than special-casing it means a second one would be
    picked up for free.
    """
    return tuple(field.alias or name for name, field in model.model_fields.items())


def _unwrap(annotation: Any) -> list[Any]:
    """Strip ``Annotated``/``Optional``/union wrappers down to concrete types."""
    origin = typing.get_origin(annotation)
    if origin is None:
        return [annotation]
    if origin in (types.UnionType, typing.Union):
        return [
            unwrapped
            for member in typing.get_args(annotation)
            if member is not type(None)
            for unwrapped in _unwrap(member)
        ]
    # ``Annotated[X, ...]`` — get_origin returns X's own origin for a subscripted
    # X, so the metadata check has to come from __metadata__ instead.
    metadata = getattr(annotation, "__metadata__", None)
    if metadata is not None:
        return _unwrap(typing.get_args(annotation)[0])
    return [annotation]


def _shape_for(annotation: Any, memo: dict[Any, Shape]) -> Shape:
    """The shape of a value annotated ``annotation``."""
    shapes = [_concrete_shape(member, memo) for member in _unwrap(annotation)]
    interesting = [shape for shape in shapes if shape is not OPAQUE]
    # A union of two shaped alternatives has no single key order, so it stays
    # opaque rather than picking one arbitrarily. No such union exists today.
    if len(interesting) != 1:
        return OPAQUE
    return interesting[0]


def _concrete_shape(annotation: Any, memo: dict[Any, Shape]) -> Shape:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (list, set, frozenset, tuple) and args:
        item = _shape_for(args[0], memo)
        return SequenceShape(item=item) if item is not OPAQUE else OPAQUE
    if origin is dict:
        # ``metadata.labels`` and friends: the keys are the user's, so there is
        # no order to impose and nothing below them to shape.
        return OPAQUE
    if isinstance(annotation, type) and issubclass(annotation, NetgraphModel):
        return _model_shape(annotation, memo)
    return OPAQUE


def _model_shape(model: type[NetgraphModel], memo: dict[Any, Shape]) -> Shape:
    cached = memo.get(model)
    if cached is not None:
        return cached
    # Seed the memo before recursing so a self-referential model terminates.
    children: dict[str, Shape] = {}
    shape = MappingShape(order=_field_names(model), children=children)
    memo[model] = shape
    for name, field in model.model_fields.items():
        children[field.alias or name] = _shape_for(field.annotation, memo)
    return shape


def _insert_after(order: tuple[str, ...], key: str, after: str) -> tuple[str, ...]:
    """``order`` with ``key`` placed directly after ``after``."""
    if key in order:  # pragma: no cover - the loader keys are not model fields
        return order
    if after not in order:  # pragma: no cover - guarded by check_order
        raise ValueError(f"cannot place {key!r}: no {after!r} in {order!r}")
    index = order.index(after) + 1
    return (*order[:index], key, *order[index:])


def _with_loader_keys(shape: Shape) -> Shape:
    """Splice ``spec.from`` and ``spec.interfaces[].range`` into a device spec.

    Both are real YAML the schema documents and the loader removes before
    pydantic sees the document, so neither is a model field — but a file that
    uses them still has to be formatted.
    """
    if not isinstance(shape, MappingShape):  # pragma: no cover - DeviceSpec is a mapping
        return shape
    children = dict(shape.children)
    interfaces = children.get("interfaces")
    if isinstance(interfaces, SequenceShape) and isinstance(interfaces.item, MappingShape):
        entry = interfaces.item
        children["interfaces"] = SequenceShape(
            item=MappingShape(
                order=_insert_after(entry.order, _RANGE_KEY, _RANGE_AFTER),
                children=entry.children,
            )
        )
    return MappingShape(
        order=_insert_after(shape.order, INHERIT_KEY, _INHERIT_AFTER),
        children=children,
    )


def _build() -> dict[str, Shape]:
    memo: dict[Any, Shape] = {}
    shapes: dict[str, Shape] = {}
    device_spec = _with_loader_keys(_model_shape(DeviceSpec, memo))
    for model in ELEMENT_MODELS:
        kind = model.model_fields["kind"].default
        shape = _model_shape(model, memo)
        assert isinstance(shape, MappingShape)  # every element is a mapping
        children = dict(shape.children)
        if model.model_fields["spec"].annotation is DeviceSpec:
            children["spec"] = device_spec
        shapes[kind] = MappingShape(order=shape.order, children=children)
    # A template's ``spec`` is ``dict[str, Any]`` on the model — the loader
    # merges it into a device spec later — so the shape has to be supplied here
    # rather than read off the annotation.
    template = _model_shape(Template, memo)
    assert isinstance(template, MappingShape)  # the envelope is a mapping
    shapes[Template.model_fields["kind"].default] = MappingShape(
        order=template.order,
        children={**template.children, "spec": device_spec},
    )
    # A test suite is not an element either, but unlike a layout its ``spec`` is
    # a modelled shape all the way down, so it orders exactly like one: 'assert'
    # first on every assertion, then what is being asserted about.
    suite = _model_shape(TestSuite, memo)
    assert isinstance(suite, MappingShape)  # the envelope is a mapping
    shapes[TestSuite.model_fields["kind"].default] = suite
    return shapes


def _envelope_shape() -> MappingShape:
    """The shape used when ``kind`` is missing or unrecognised."""
    memo: dict[Any, Shape] = {}
    base = _model_shape(ElementBase, memo)
    assert isinstance(base, MappingShape)  # the envelope is a mapping
    return MappingShape(order=ENVELOPE_ORDER, children=base.children)


_DOCUMENT_SHAPES: Final[dict[str, Shape]] = _build()
_ENVELOPE_SHAPE: Final[MappingShape] = _envelope_shape()
