"""The completion list: what the schema allows here, and what the tree contains.

Two sources, and the split between them is the whole design.

**The schema** answers "what may be written at this path" — the keys, their
enums, their documentation. It is generated from the models
(:mod:`netviz.schema`), so it is right by construction and stays right.

**The inventory** answers "what is there" — the element names a reference may
resolve to, and the interfaces those elements actually declare. A cable endpoint
whose completion list came from the schema alone would offer a regular
expression; what a user wants is ``sw-home:port3``, and specifically not
``sw-home:port9``, which is the mistake ``NG-C003`` exists to catch. Offering
only what exists is the same check, moved from after the save to before it.

Reference fields are not detected by looking for a colon. They are the six
:class:`~netviz.edit.references.ReferenceRole` paths, listed once in
:data:`REFERENCE_FIELDS` against the same section numbers the write path uses.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from netviz.loader.inventory import Inventory, namespace_of, short_name
from netviz.lsp.context import CursorContext, Slot
from netviz.lsp.schemaindex import Property, SchemaIndex
from netviz.models import Adapter, Cable, Element, Group, PatchPanel, Pdu, Tunnel

__all__ = ["REFERENCE_FIELDS", "CompletionItem", "completions"]

#: ``CompletionItemKind`` (§3.17.5). Only the four shapes this server emits.
KIND_PROPERTY: Final = 10
KIND_ENUM_MEMBER: Final = 20
KIND_VALUE: Final = 12
KIND_REFERENCE: Final = 18

#: Where a reference is written, and what it may name. The paths are the ones
#: :func:`~netviz.edit.references.references_of` reads, so a field that stops
#: being a reference stops being completed as one.
#:
#: ``element`` names the half that resolves to a document; ``detail`` the half
#: that names something inside it. ``kinds`` narrows the candidates to what the
#: field can legally point at, which is why ``spec.members`` offers people and
#: groups and nothing else.
REFERENCE_FIELDS: Final[Mapping[tuple[str, ...], tuple[str, str | None]]] = {
    ("spec", "endpoints"): ("element", "interface"),
    ("spec", "endpoints", "device"): ("element", None),
    ("spec", "endpoints", "interface"): ("interface", None),
    ("spec", "over"): ("tunnel", None),
    ("spec", "upstream", "attached_to"): ("element", None),
    ("spec", "members"): ("identity", None),
    ("spec", "power", "inputs", "pdu"): ("pdu", None),
    ("spec", "power", "inputs", "outlet"): ("outlet", None),
    ("spec", "from"): ("template", None),
}


@dataclass(frozen=True, slots=True)
class CompletionItem:
    """One entry of the list, before it is turned into wire JSON."""

    label: str
    kind: int
    #: Text actually inserted; the label when it is the same.
    insert: str | None = None
    detail: str = ""
    documentation: str = ""
    #: Lower sorts first. Used to put required keys above optional ones.
    sort: str = ""

    def to_dict(self, replace: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": self.label,
            "kind": self.kind,
            "textEdit": {"range": replace, "newText": self.insert or self.label},
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.documentation:
            payload["documentation"] = {"kind": "markdown", "value": self.documentation}
        payload["sortText"] = self.sort or self.label
        return payload


def completions(
    context: CursorContext, schema: SchemaIndex, inventory: Inventory, namespace: str
) -> list[CompletionItem]:
    """Everything that may be written where the caret is."""
    if not context.is_completable:
        return []
    if context.kind is None:
        return list(_bootstrap(context, schema))
    if context.slot is Slot.KEY:
        return list(_keys(context, schema))
    if context.slot is Slot.ITEM:
        return list(_item(context, schema, inventory, namespace))
    return list(_value(context, schema, inventory, namespace))


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def _bootstrap(context: CursorContext, schema: SchemaIndex) -> Iterator[CompletionItem]:
    """A document with no ``kind`` yet. Only the envelope can be offered."""
    if context.slot is Slot.VALUE and context.key == "kind":
        yield from _kind_values(schema)
        return
    if context.slot is Slot.VALUE and context.key == "apiVersion":
        yield CompletionItem(
            label=schema.api_version,
            kind=KIND_VALUE,
            insert=_value_text(context, schema.api_version),
        )
        return
    if context.slot is not Slot.KEY or context.path:
        return
    for name in ("apiVersion", "kind", "metadata", "spec"):
        if name in context.siblings:
            continue
        yield CompletionItem(
            label=name,
            kind=KIND_PROPERTY,
            insert=f"{name}: " if name in {"apiVersion", "kind"} else f"{name}:",
            detail="required",
            sort=f"0{name}",
        )


def _keys(context: CursorContext, schema: SchemaIndex) -> Iterator[CompletionItem]:
    assert context.kind is not None
    for entry in schema.properties_at(context.kind, context.path):
        if entry.name in context.siblings:
            continue
        yield CompletionItem(
            label=entry.name,
            kind=KIND_PROPERTY,
            insert=f"{entry.name}:" if entry.is_container else f"{entry.name}: ",
            detail=_detail(entry),
            documentation=entry.description,
            sort=f"{'0' if entry.required else '1'}{entry.name}",
        )


def _detail(entry: Property) -> str:
    parts = [entry.type_name] if entry.type_name else []
    if entry.required:
        parts.append("required")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def _value(
    context: CursorContext, schema: SchemaIndex, inventory: Inventory, namespace: str
) -> Iterator[CompletionItem]:
    assert context.kind is not None
    if context.key == "kind" and len(context.path) == 1:
        yield from _kind_values(schema)
        return
    reference = _reference_kind(context.path)
    if reference is not None:
        yield from _reference_values(context, reference, inventory, namespace)
        return
    for value, documentation in schema.values_at(context.kind, context.path):
        yield CompletionItem(
            label=value,
            kind=KIND_ENUM_MEMBER,
            insert=_value_text(context, value),
            documentation=documentation,
        )


def _item(
    context: CursorContext, schema: SchemaIndex, inventory: Inventory, namespace: str
) -> Iterator[CompletionItem]:
    """A sequence entry: the keys of an item mapping, or a scalar it may hold."""
    assert context.kind is not None
    reference = _reference_kind(context.path)
    if reference is not None:
        yield from _reference_values(context, reference, inventory, namespace)
        return
    node = schema.schema_at(context.kind, context.path)
    if node is not None and schema.properties_at(context.kind, context.path):
        yield from _keys(context, schema)
        return
    for value, documentation in schema.values_at(context.kind, context.path):
        yield CompletionItem(label=value, kind=KIND_ENUM_MEMBER, documentation=documentation)


def _kind_values(schema: SchemaIndex) -> Iterator[CompletionItem]:
    for kind in schema.kinds:
        yield CompletionItem(
            label=kind,
            kind=KIND_ENUM_MEMBER,
            documentation=schema.summary_of(kind),
        )


def _value_text(context: CursorContext, value: str) -> str:
    """``value``, with the space the caret is missing when it sits on the colon."""
    return f" {value}" if context.needs_space else value


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #


def _reference_kind(path: Sequence[str | int]) -> str | None:
    """What the value at ``path`` refers to, or ``None`` if it refers to nothing."""
    key = tuple(str(part) for part in path if not isinstance(part, int))
    entry = REFERENCE_FIELDS.get(key)
    return None if entry is None else entry[0]


def _reference_values(
    context: CursorContext, wanted: str, inventory: Inventory, namespace: str
) -> Iterator[CompletionItem]:
    """The names actually in the tree that this field may point at."""
    if wanted in {"interface", "outlet"} or ":" in context.prefix:
        yield from _detail_values(context, wanted, inventory, namespace)
        return
    for fqn, element in inventory.elements.items():
        if not _admits(wanted, element):
            continue
        label = _spelling(fqn, namespace, inventory)
        interfaces = _interface_names(element)
        insert = label
        if wanted == "element" and context.slot is Slot.ITEM and interfaces:
            # ``endpoints`` is written ``device:interface``; completing only the
            # device half would leave a value the schema rejects.
            insert = f"{label}:"
        yield CompletionItem(
            label=label,
            kind=KIND_REFERENCE,
            insert=_value_text(context, insert),
            detail=element.kind,
            documentation=_reference_doc(fqn, element, inventory),
            sort=f"{'0' if namespace_of(fqn) == namespace else '1'}{label}",
        )


def _detail_values(
    context: CursorContext, wanted: str, inventory: Inventory, namespace: str
) -> Iterator[CompletionItem]:
    """What is inside the element the other half of this reference names.

    The element half is either already typed on this line — ``sw-home:`` — or
    written beside it as a sibling key, which is the mapping spelling
    ``{device: sw-home, interface: port3}`` and the only spelling ``power.inputs``
    has.
    """
    device, _, partial = context.prefix.rpartition(":")
    prefix = f"{device}:" if device else ""
    if not device:
        device = context.written.get("device") or context.written.get("pdu") or ""
        partial = context.prefix
    if not device:
        return
    fqn = inventory.resolve_fqn(device.strip("'\""), namespace=namespace)
    element = inventory.get(fqn) if fqn is not None else None
    if element is None:
        return
    if wanted == "outlet" or isinstance(element, Pdu):
        yield from _outlet_values(context, element, prefix, partial)
        return
    for name in _interface_names(element):
        if partial and not name.startswith(partial):
            continue
        yield CompletionItem(
            label=f"{prefix}{name}",
            kind=KIND_REFERENCE,
            insert=_value_text(context, f"{prefix}{name}"),
            detail=_interface_detail(element, name),
        )


def _outlet_values(
    context: CursorContext, element: Any, prefix: str, partial: str
) -> Iterator[CompletionItem]:
    """A PDU's outlets, which are a numbering rather than a list of declarations."""
    for name in getattr(element, "outlet_numbers", ()):
        if partial and not name.startswith(partial):
            continue
        yield CompletionItem(
            label=f"{prefix}{name}",
            kind=KIND_REFERENCE,
            insert=_value_text(context, f"{prefix}{name}"),
            detail="outlet",
        )


def _admits(wanted: str, element: Element) -> bool:
    """May a field wanting ``wanted`` point at ``element``?"""
    if wanted == "tunnel":
        return isinstance(element, Tunnel)
    if wanted == "pdu":
        return isinstance(element, Pdu)
    if wanted == "identity":
        return str(element.kind) in {"user", "group"}
    if wanted == "template":
        return str(element.kind) == "template"
    # A cable endpoint, an adapter's host: anything that can own an interface.
    if isinstance(element, (Adapter, PatchPanel)):
        return True
    if isinstance(element, (Cable, Tunnel, Group, Pdu)):
        return False
    return bool(_interface_names(element))


def _spelling(fqn: str, namespace: str, inventory: Inventory) -> str:
    """How this element should be written from ``namespace``.

    The short name when it resolves to this element and nothing else, which is
    what a person would type; the fully-qualified name when it would not, which
    is what ``NG-N002`` makes necessary.
    """
    short = short_name(fqn)
    resolution = inventory.resolve_fqn(short, namespace=namespace)
    return short if resolution == fqn else fqn


def _reference_doc(fqn: str, element: Any, inventory: Inventory) -> str:
    lines = [f"**{element.kind}** `{fqn}`"]
    description = element.metadata.description
    if description:
        lines.append("")
        lines.append(str(description))
    interfaces = _interface_names(element)
    if interfaces:
        lines.append("")
        shown = ", ".join(f"`{name}`" for name in interfaces[:8])
        more = "" if len(interfaces) <= 8 else " …"
        lines.append(f"**interfaces** {shown}{more}")
    source = inventory.source_of(fqn)
    if source is not None:
        lines.append("")
        lines.append(f"`{source.relative}#{source.index}`")
    return "\n".join(lines)


def _interface_names(element: Any) -> list[str]:
    interfaces = getattr(element.spec, "interfaces", None)
    if not isinstance(interfaces, Sequence):
        return []
    return [str(getattr(entry, "name", "")) for entry in interfaces]


def _interface_detail(element: Any, name: str) -> str:
    interfaces = getattr(element.spec, "interfaces", None)
    for entry in interfaces if isinstance(interfaces, Sequence) else ():
        if getattr(entry, "name", None) == name:
            kind = getattr(entry, "type", None)
            return str(getattr(kind, "value", kind or ""))
    return ""
