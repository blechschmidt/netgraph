"""What a name is, without leaving the file it is written in.

A cable document is four lines of references, and every question a reader has
about it is answered somewhere else: what is ``sw-home``, does it have a
``port3``, what is on that port already, which VLAN does it carry. Hovering is
the cheapest possible answer — the target is already resolved, because
:mod:`netgraph.lsp.index` resolved it to decide the span was a reference at all.

Two kinds of hover, and one fallback:

**A name.** An element, or one of its interfaces. Rendered as what the reader
came for: the kind, the fully-qualified name, the file it is declared in, and
for an interface its type, addresses, VLANs and what is cabled to it.

**A key.** The prose from ``docs/schema.md``, which the JSON Schema already
carries as a ``description``. Not as good as reading the specification, and very
much better than guessing what ``ingress_filtering`` does from its name.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from netgraph.loader.inventory import Inventory, namespace_of
from netgraph.lsp.context import CursorContext, Slot
from netgraph.lsp.index import Anchor, AnchorKind
from netgraph.lsp.schemaindex import SchemaIndex
from netgraph.models import Element

__all__ = ["element_markdown", "hover_markdown", "interface_markdown", "key_markdown"]


def hover_markdown(anchor: Anchor, inventory: Inventory) -> str:
    """The hover text for a name, ``""`` when there is nothing to say about it."""
    if anchor.kind is AnchorKind.INTERFACE_NAME and anchor.detail is not None:
        return interface_markdown(anchor.owner, anchor.detail, inventory)
    if anchor.target is None:
        return _unresolved(anchor)
    # The two halves of ``sw-home:port1`` answer two different questions, and
    # which one is under the caret is which one the reader is asking.
    if anchor.kind is AnchorKind.REFERENCE_DETAIL and anchor.detail is not None:
        return interface_markdown(anchor.target, anchor.detail, inventory)
    return element_markdown(anchor.target, inventory)


def _unresolved(anchor: Anchor) -> str:
    """A reference that names nothing. Say so rather than say nothing."""
    written = anchor.written or "this reference"
    detail = f":{anchor.detail}" if anchor.detail else ""
    return (
        f"`{written}{detail}` — **unresolved**\n\n"
        f"No element of this inventory answers to that name from "
        f"`{namespace_of(anchor.owner) or '/'}`."
    )


def element_markdown(fqn: str, inventory: Inventory) -> str:
    """An element: what it is, where it is written, and what it holds."""
    element = inventory.get(fqn)
    if element is None:
        return ""
    lines = [f"**{element.kind}** `{fqn}`"]
    description = element.metadata.description
    if description:
        lines.append("")
        lines.append(str(description))
    facts = list(_element_facts(element, fqn, inventory))
    source = inventory.source_of(fqn)
    if source is not None:
        facts.append(("declared in", f"`{source.relative}#{source.index}`"))
    if facts:
        lines.append("")
        lines.extend(f"- **{name}** — {value}" for name, value in facts)
    interfaces = _interfaces(element)
    if interfaces:
        lines.append("")
        shown = ", ".join(f"`{name}`" for name in interfaces[:12])
        more = "" if len(interfaces) <= 12 else f", … ({len(interfaces)} in total)"
        lines.append(f"**interfaces** {shown}{more}")
    return "\n".join(lines)


def interface_markdown(fqn: str, name: str, inventory: Inventory) -> str:
    """One interface of one element: type, addresses, VLANs, what lands on it."""
    element = inventory.get(fqn)
    if element is None:
        return ""
    interface = _interface(element, name)
    if interface is None:
        outlets = getattr(element.spec, "outlets", None)
        if outlets is not None:
            return f"**outlet** `{name}` of **{element.kind}** `{fqn}`\n\n- **outlets** `{outlets}`"
        return (
            f"`{name}` — **unknown interface**\n\n"
            f"**{element.kind}** `{fqn}` declares "
            f"{_listed(_interfaces(element)) or 'no interfaces'}."
        )
    lines = [f"**{_enum(interface.type)}** `{fqn}:{name}`"]
    description = getattr(interface, "description", None)
    if description:
        lines.append("")
        lines.append(str(description))
    facts = list(_interface_facts(interface))
    facts.extend(_link_facts(fqn, name, inventory))
    if facts:
        lines.append("")
        lines.extend(f"- **{key}** — {value}" for key, value in facts)
    return "\n".join(lines)


def key_markdown(context: CursorContext, schema: SchemaIndex) -> str:
    """The specification's own prose for the key the caret is on."""
    if context.kind is None or context.slot is Slot.NONE:
        return ""
    # ``path`` names the mapping the caret is in when the caret is on a key, so
    # the key on that line is appended: hovering ``interfaces:`` describes
    # ``interfaces`` rather than the ``spec`` it belongs to.
    path = context.path
    if context.slot is not Slot.VALUE and context.line_key is not None:
        path = (*path, context.line_key)
    node = schema.schema_at(context.kind, path)
    if node is None:
        return ""
    title = ".".join(str(part) for part in path) or context.kind
    description = str(node.get("description", ""))
    type_line = str(node.get("type", "")) or ""
    lines = [f"**`{title}`**" + (f" — *{type_line}*" if type_line else "")]
    if description:
        lines.append("")
        lines.append(description)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The facts
# --------------------------------------------------------------------------- #


def _element_facts(element: Element, fqn: str, inventory: Inventory) -> Iterator[tuple[str, str]]:
    namespace = namespace_of(fqn)
    if namespace:
        yield "namespace", f"`{namespace}`"
    for field in ("vendor", "model", "serial", "location", "medium", "type"):
        value = getattr(element.spec, field, None)
        if value:
            yield field, f"`{_enum(value)}`"
    endpoints = getattr(element.spec, "endpoints", None)
    if endpoints:
        yield "endpoints", ", ".join(f"`{_endpoint(entry)}`" for entry in endpoints)
    labels = element.metadata.labels
    if labels:
        yield "labels", ", ".join(f"`{key}={value}`" for key, value in sorted(labels.items()))


def _interface_facts(interface: Any) -> Iterator[tuple[str, str]]:
    if getattr(interface, "enabled", True) is False:
        yield "state", "`disabled`"
    for field in ("mac", "mtu", "vrf", "parent"):
        value = getattr(interface, field, None)
        if value:
            yield field, f"`{value}`"
    members = getattr(interface, "members", None)
    if members:
        yield "members", _listed([str(entry) for entry in members])
    for family in ("ipv4", "ipv6"):
        config = getattr(interface, family, None)
        addresses = getattr(config, "addresses", ()) if config is not None else ()
        if addresses:
            yield family, _listed([_address(entry) for entry in addresses])
        gateway = getattr(config, "gateway", None) if config is not None else None
        if gateway:
            yield f"{family} gateway", f"`{gateway}`"
    vlan = getattr(interface, "vlan", None)
    if vlan is not None:
        yield "vlan", _vlan_text(vlan)
    wireless = getattr(interface, "wireless", None)
    if wireless is not None:
        role = _enum(getattr(wireless, "role", ""))
        yield "wireless", f"`{role}`" if role else "`configured`"


def _link_facts(fqn: str, name: str, inventory: Inventory) -> Iterator[tuple[str, str]]:
    """What terminates on this interface, which is the question a cable raises."""
    for cable_fqn, cable in inventory.cables.items():
        for endpoint in cable.spec.endpoints:
            resolved = inventory.resolve_fqn(endpoint.device, namespace=namespace_of(cable_fqn))
            if resolved != fqn or endpoint.interface != name:
                continue
            far = [entry for entry in cable.spec.endpoints if entry is not endpoint]
            other = f"`{_endpoint(far[0])}`" if far else "nothing"
            yield "cabled to", f"{other} by `{cable_fqn}`"


def _vlan_text(vlan: Any) -> str:
    mode = _enum(getattr(vlan, "mode", ""))
    access = getattr(vlan, "access_vlan", None)
    trunk = getattr(vlan, "trunk_vlans", None)
    native = getattr(vlan, "native_vlan", None)
    parts = [f"`{mode}`"] if mode else []
    if access is not None:
        parts.append(f"access `{access}`")
    if trunk:
        parts.append(f"trunk `{', '.join(str(entry) for entry in trunk)}`")
    if native is not None:
        parts.append(f"native `{native}`")
    return " ".join(parts) or "`configured`"


def _address(entry: Any) -> str:
    prefix = getattr(entry, "prefix_length", None)
    netmask = getattr(entry, "netmask", None)
    if prefix is not None:
        return f"{entry.ip}/{prefix}"
    return f"{entry.ip} {netmask}" if netmask else str(entry.ip)


def _endpoint(entry: Any) -> str:
    interface = getattr(entry, "interface", None)
    device = getattr(entry, "device", entry)
    return f"{device}:{interface}" if interface else str(device)


def _interfaces(element: Element) -> list[str]:
    interfaces = getattr(element.spec, "interfaces", None)
    if not isinstance(interfaces, Sequence):
        return []
    return [str(getattr(entry, "name", "")) for entry in interfaces]


def _interface(element: Element, name: str) -> Any:
    interfaces = getattr(element.spec, "interfaces", None)
    if not isinstance(interfaces, Sequence):
        return None
    for entry in interfaces:
        if getattr(entry, "name", None) == name:
            return entry
    return None


def _enum(value: Any) -> str:
    """An enum member as the YAML spells it."""
    return str(getattr(value, "value", value))


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)
