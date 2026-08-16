"""Reading the published JSON Schema the way a completion needs to read it.

``netviz schema`` already emits the whole contract — every key of every kind,
its type, its enum, and the prose from ``docs/schema.md`` as a ``description``.
Completion is then not a second description of the model but a *walk* over that
one, which is the only way the two cannot drift: a field added to a pydantic
model appears in the completion list on the next run, with its documentation,
without anyone editing this file.

The walk has to cope with the three shapes pydantic emits:

``$ref``
    Almost every non-scalar field. Resolved, with any sibling keys of the
    reference merged over the target — ``Interface`` is a ``$ref`` to
    ``PartialInterface`` plus its own ``required``.
``anyOf`` with a ``null`` branch
    Every optional field. The null branch carries no keys, so it is dropped and
    the remaining branches are merged.
``allOf`` with ``if``/``then``
    The conditional rules (a ``vlan`` interface requires ``parent``). The
    branches are read for *keys*, since a key that is only legal under a
    condition is still a key worth offering, but the conditions themselves are
    left to ``netviz validate``, which states them far better than a
    completion popup could.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from netviz.schema import build_schema

__all__ = ["Property", "SchemaIndex", "schema_index"]

#: How deep a ``$ref`` chain may go before we call it a cycle. The emitted
#: schema has no cycles; a future one that did must not hang an editor.
_MAX_DEPTH: Final = 32


@dataclass(frozen=True, slots=True)
class Property:
    """One key that may appear at a point in a document."""

    name: str
    schema: Mapping[str, Any]
    required: bool = False

    @property
    def description(self) -> str:
        return str(self.schema.get("description", ""))

    @property
    def title(self) -> str:
        return str(self.schema.get("title", self.name))

    @property
    def type_name(self) -> str:
        """A short type for the completion item's detail line."""
        return type_name(self.schema)

    @property
    def is_container(self) -> bool:
        """Does the value go on the next line rather than after the colon?"""
        return self.type_name in {"object", "array"} or bool(properties_of(self.schema))


class SchemaIndex:
    """The emitted schema, with the lookups a language server performs on it."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document
        self._defs: Mapping[str, Any] = document.get("$defs", {})
        mapping = document.get("discriminator", {}).get("mapping", {})
        self._kinds: dict[str, str] = {
            kind: str(pointer).rsplit("/", 1)[-1] for kind, pointer in mapping.items()
        }

    # -- the kinds -------------------------------------------------------

    @property
    def kinds(self) -> tuple[str, ...]:
        """Every ``kind`` a document may declare, in schema order."""
        return tuple(self._kinds)

    @property
    def api_version(self) -> str:
        """The one ``apiVersion`` this build understands."""
        root = self.root_for(next(iter(self._kinds), "switch"))
        version = properties_of(root).get("apiVersion", {}).get("const")
        return str(version) if version is not None else "netviz.dev/v1alpha1"

    def summary_of(self, kind: str) -> str:
        """The one-line description of ``kind``, for a completion item."""
        root = self.root_for(kind)
        return str(root.get("description", "")) if root else ""

    def root_for(self, kind: str) -> Mapping[str, Any]:
        """The document schema for ``kind``, or ``{}`` when there is no such kind."""
        name = self._kinds.get(kind)
        if name is None:
            return {}
        return self.resolve(self._defs.get(name, {}))

    # -- walking ---------------------------------------------------------

    def resolve(self, node: Mapping[str, Any], depth: int = 0) -> Mapping[str, Any]:
        """``node`` with its ``$ref`` followed and its optionality unwrapped."""
        if depth > _MAX_DEPTH:  # pragma: no cover - the emitted schema is acyclic
            return node
        pointer = node.get("$ref")
        if isinstance(pointer, str):
            target = self._defs.get(pointer.rsplit("/", 1)[-1], {})
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return self.resolve(merged, depth + 1)
        branches = _optional_branches(node)
        if branches is not None:
            merged = {k: v for k, v in node.items() if k not in {"anyOf", "oneOf"}}
            for branch in branches:
                merged = {**self.resolve(branch, depth + 1), **merged}
            return merged
        return node

    def schema_at(self, kind: str, path: Sequence[str | int]) -> Mapping[str, Any] | None:
        """The schema of the value at ``path`` in a ``kind`` document.

        ``("spec", "interfaces", 0, "mtu")`` walks properties by name and arrays
        by index. ``None`` when the path names nothing the schema allows, which
        is what tells a completion to offer nothing rather than to guess.
        """
        node: Mapping[str, Any] | None = self.root_for(kind)
        if not node:
            return None
        for part in path:
            if node is None:
                return None
            node = self.resolve(node)
            if isinstance(part, int):
                items = node.get("items")
                node = self.resolve(items) if isinstance(items, Mapping) else None
                continue
            found = properties_of(node).get(part)
            node = self.resolve(found) if found is not None else None
        return None if node is None else self.resolve(node)

    def properties_at(self, kind: str, path: Sequence[str | int]) -> tuple[Property, ...]:
        """Every key legal at ``path``, in schema order."""
        node = self.schema_at(kind, path)
        if node is None:
            return ()
        required = set(_required_of(node))
        return tuple(
            Property(name=name, schema=self.resolve(schema), required=name in required)
            for name, schema in properties_of(node).items()
        )

    def values_at(self, kind: str, path: Sequence[str | int]) -> tuple[tuple[str, str], ...]:
        """The literal values legal at ``path``, each with its documentation.

        Enums, ``const``s and booleans. Empty for a free-form scalar, which is
        the honest answer: offering a made-up example as a completion is worse
        than offering nothing.
        """
        node = self.schema_at(kind, path)
        return () if node is None else literal_values(node)


def properties_of(node: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Every declared property of ``node``, its conditional branches included."""
    found: dict[str, Mapping[str, Any]] = {}
    for name, schema in node.get("properties", {}).items():
        if isinstance(schema, Mapping):
            found[name] = schema
    for branch in _branches(node):
        for name, schema in branch.get("properties", {}).items():
            if isinstance(schema, Mapping):
                found.setdefault(name, schema)
    return found


def _required_of(node: Mapping[str, Any]) -> frozenset[str]:
    """The keys ``node`` requires unconditionally."""
    required = node.get("required", ())
    return (
        frozenset(str(name) for name in required) if isinstance(required, Sequence) else frozenset()
    )


def _branches(node: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """The sub-schemas of ``node``'s combinators, one level down.

    ``if`` is skipped deliberately: its properties are the *condition*, not keys
    the object may carry, and a ``kind`` offered as a completion inside
    ``spec.interfaces[0]`` would be nonsense.
    """
    for keyword in ("allOf", "anyOf", "oneOf"):
        entries = node.get(keyword)
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            continue
        for entry in entries:
            if isinstance(entry, Mapping):
                yield entry
                for nested in ("then", "else"):
                    inner = entry.get(nested)
                    if isinstance(inner, Mapping):
                        yield inner


def _optional_branches(node: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """``node``'s union branches with the ``null`` one dropped, or ``None``.

    ``None`` means "not a union of that shape", which is different from an empty
    list: a union whose every branch was ``null`` carries no keys at all.
    """
    for keyword in ("anyOf", "oneOf"):
        entries = node.get(keyword)
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            continue
        kept = [
            entry for entry in entries if isinstance(entry, Mapping) and entry.get("type") != "null"
        ]
        if len(kept) != len(entries):
            return kept
    return None


def literal_values(node: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """``(value, documentation)`` for every literal ``node`` admits."""
    found: dict[str, str] = {}
    documentation = str(node.get("description", ""))
    _collect_literals(node, documentation, found)
    return tuple(found.items())


def _collect_literals(
    node: Mapping[str, Any], documentation: str, found: dict[str, str], depth: int = 0
) -> None:
    if depth > _MAX_DEPTH:  # pragma: no cover - bounded by the emitted schema
        return
    own = str(node.get("description", documentation))
    if "const" in node:
        found.setdefault(_literal_text(node["const"]), own)
    for value in node.get("enum", ()) or ():
        found.setdefault(_literal_text(value), own)
    if node.get("type") == "boolean":
        found.setdefault("true", own)
        found.setdefault("false", own)
    for branch in _branches(node):
        if "if" not in node or branch is not node.get("if"):
            _collect_literals(branch, own, found, depth + 1)


def _literal_text(value: Any) -> str:
    """A JSON literal as it is written in YAML."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def type_name(node: Mapping[str, Any]) -> str:
    """A short type for a completion detail, ``""`` when the schema says nothing."""
    declared = node.get("type")
    if isinstance(declared, str):
        return declared
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        names = [str(entry) for entry in declared if entry != "null"]
        if names:
            return " | ".join(names)
    if "enum" in node or "const" in node:
        return "string"
    if "properties" in node:
        return "object"
    for branch in _branches(node):
        found = type_name(branch)
        if found:
            return found
    return ""


@lru_cache(maxsize=1)
def schema_index() -> SchemaIndex:
    """The index over this build's schema, built once per process."""
    return SchemaIndex(build_schema())
