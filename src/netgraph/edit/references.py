"""Which documents point at which elements, and how to rewrite one when it moves.

A rename is not a rename until every reference to the old name is a reference to
the new one, and a delete is not safe until the tree is asked what would be left
dangling. Both questions come down to the same table: the places one document
can name another.

There are six, and they are not guessable from the YAML — a string is a
reference because the *schema* says the field holds one, not because it has a
colon in it:

============================ ================================================
``spec.endpoints[]``         a cable or tunnel end, ``device:interface`` (§7, §14.3)
``spec.over``                the tunnel a tunnel runs inside (§14.4)
``spec.upstream.attached_to``the host an adapter is plugged into (§8.1)
``spec.power.inputs[]``      the outlet feeding a supply, ``pdu:outlet`` (§17)
``spec.members[]``           a user or nested group in a group (§19.2)
``spec.from``                the template a device inherits (§6.6)
============================ ================================================

So the references are read off the **models**, which is where the schema is
already encoded, and the raw YAML is only ever touched at the path a model
reference came from — and only after checking that the text there is still what
the model said it was. That check is what stops a value a *template* contributed
from being rewritten in the document that merely inherited it.

Choosing the replacement text is the other half. ``sw-home`` and
``switches/sw-home`` may name the same switch, and which one a document wrote is
a choice its author made: netgraph keeps it. A short name stays short if a short
name still resolves to the right element after the change, and a qualified name
stays qualified, in the same relative-or-absolute shape it had. Only when the
form the author chose would now resolve to something else — or to nothing — is
it escalated, and then to the fully-qualified name, which always resolves.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from netgraph.edit.errors import EditError
from netgraph.edit.paths import FieldPath, format_field_path, get_field
from netgraph.loader.inventory import namespace_of, qualify, short_name
from netgraph.models import Adapter, Cable, Element, Group, Tunnel

__all__ = [
    "NameIndex",
    "Reference",
    "ReferenceRole",
    "dependents_of",
    "locate_reference",
    "reference_text",
    "references_of",
    "rewrite_reference",
]


class ReferenceRole(str, Enum):
    """What a reference is *for*, which decides how a delete treats it."""

    #: One end of a cable or a tunnel. The link cannot survive its element.
    ENDPOINT = "endpoint"
    #: ``spec.over`` — the tunnel this tunnel is carried by.
    OVER = "over"
    #: ``spec.upstream.attached_to`` — the host an adapter hangs off. Optional,
    #: so it can be cleared instead of taking the adapter with it.
    ATTACHED_TO = "attached_to"
    #: One entry of ``spec.power.inputs``. A list entry, so it can be dropped.
    POWER_INPUT = "power_input"
    #: One entry of a group's ``spec.members`` (§19.2). A list entry, so a
    #: cascading delete drops the membership rather than the group: a group that
    #: loses a member is still a group, which is not true of a cable that loses
    #: an end.
    MEMBER = "member"
    #: ``spec.from`` — the template a device inherits. Not an element reference:
    #: templates are not elements, so nothing here ever deletes one. It is
    #: tracked because moving a document to another folder can change what a
    #: plain template name resolves to.
    TEMPLATE = "from"

    @property
    def is_structural(self) -> bool:
        """Does the referring element cease to make sense without its target?

        True for a link end: a cable with one end missing is not a cable. False
        for the optional scalars, which a cascading delete clears rather than
        following.
        """
        return self in (ReferenceRole.ENDPOINT, ReferenceRole.OVER)


@dataclass(frozen=True, slots=True)
class Reference:
    """One place a document names another element."""

    #: Fully-qualified name of the element holding the reference.
    source: str
    role: ReferenceRole
    #: Path to the value inside the *raw* document.
    path: FieldPath
    #: The element part, exactly as it was written.
    target: str
    #: The interface or outlet part, when the reference has one.
    detail: str | None = None

    @property
    def namespace(self) -> str:
        """The namespace the reference resolves from — the holder's own."""
        return namespace_of(self.source)

    def __str__(self) -> str:
        location = f"{self.source} {format_field_path(self.path)}"
        return (
            f"{location} -> {self.target}"
            if self.detail is None
            else (f"{location} -> {self.target}:{self.detail}")
        )


def references_of(fqn: str, element: Element) -> Iterator[Reference]:
    """Every reference ``element`` makes, with the raw path each was written at.

    A cable's endpoints are sorted by the model (§7.1) but a document's are not,
    so each endpoint's :attr:`~netgraph.models.InterfaceRef.document_index` is
    what the path uses — otherwise a rewrite of "the first endpoint" would land
    on the wrong line of the file.
    """
    if isinstance(element, (Cable, Tunnel)):
        for position, endpoint in enumerate(element.spec.endpoints):
            index = endpoint.document_index
            yield Reference(
                source=fqn,
                role=ReferenceRole.ENDPOINT,
                path=("spec", "endpoints", position if index is None else index),
                target=endpoint.device,
                detail=endpoint.interface,
            )
    if isinstance(element, Tunnel) and element.spec.over is not None:
        yield Reference(
            source=fqn,
            role=ReferenceRole.OVER,
            path=("spec", "over"),
            target=element.spec.over,
        )
    if isinstance(element, Adapter) and element.spec.upstream.attached_to is not None:
        yield Reference(
            source=fqn,
            role=ReferenceRole.ATTACHED_TO,
            path=("spec", "upstream", "attached_to"),
            target=element.spec.upstream.attached_to,
        )
    if isinstance(element, Group):
        for index, member in enumerate(element.spec.members):
            yield Reference(
                source=fqn,
                role=ReferenceRole.MEMBER,
                path=("spec", "members", index),
                target=member,
            )
    power = getattr(element.spec, "power", None)
    if power is not None:
        for index, entry in enumerate(power.inputs):
            yield Reference(
                source=fqn,
                role=ReferenceRole.POWER_INPUT,
                path=("spec", "power", "inputs", index),
                target=entry.pdu,
                detail=entry.outlet,
            )


def dependents_of(
    target: str,
    elements: Mapping[str, Element],
    index: NameIndex,
) -> list[Reference]:
    """Every reference in ``elements`` that resolves to ``target``, in load order.

    Resolution is the loader's (:meth:`~netgraph.loader.Inventory.lookup`): a
    reference written as a short name in a nested namespace is found, and one
    that happens to spell the same short name but resolves elsewhere is not.
    """
    found = []
    for fqn, element in elements.items():
        for reference in references_of(fqn, element):
            if reference.role is ReferenceRole.TEMPLATE:  # pragma: no cover - not from models
                continue
            if index.lookup(reference.target, reference.namespace) == target:
                found.append(reference)
    return found


# --------------------------------------------------------------------------- #
# Resolving names without an inventory
# --------------------------------------------------------------------------- #


class NameIndex:
    """Name resolution over a *set of names*, so a change can be resolved against.

    :class:`~netgraph.loader.Inventory` answers the same question, but only
    about the tree as it is. Choosing the text a rewritten reference should use
    means asking it about the tree as it *will be*, which is what this is for:
    build one from the current names, apply the same rename to it, and ask both.

    The algorithm is :meth:`~netgraph.loader.Inventory.lookup`'s, deliberately:
    a reference netgraph will resolve differently from the way this predicts is
    a bug, and the property tests compare the two.
    """

    def __init__(self, names: Iterable[str]) -> None:
        self._names = list(dict.fromkeys(names))
        self._by_namespace: dict[str, dict[str, str]] = {}
        self._by_short: dict[str, list[str]] = {}
        for fqn in self._names:
            self._by_namespace.setdefault(namespace_of(fqn), {})[short_name(fqn)] = fqn
            self._by_short.setdefault(short_name(fqn), []).append(fqn)

    def lookup(self, name: str, namespace: str = "") -> str | None:
        """The fully-qualified name ``name`` denotes in ``namespace``, or ``None``."""
        if "/" in name:
            for candidate in (qualify(namespace, name), name):
                if candidate in self._by_short.get(short_name(candidate), ()):
                    return candidate
            return None
        scope = namespace
        while True:
            found = self._by_namespace.get(scope, {}).get(name)
            if found is not None:
                return found
            if not scope:
                break
            scope = namespace_of(scope)
        matches = self._by_short.get(name, ())
        return matches[0] if len(matches) == 1 else None

    def replaced(self, old: str, new: str) -> NameIndex:
        """A copy with ``old`` renamed to ``new``, keeping the order of the rest."""
        return NameIndex(new if fqn == old else fqn for fqn in self._names)


def reference_text(new_fqn: str, *, namespace: str, written: str, index: NameIndex) -> str:
    """How a reference written as ``written`` should now spell ``new_fqn``.

    The candidates are tried in the order that keeps the author's choice: the
    shape they used first, then the shapes that are still correct, and the
    fully-qualified name last because it always resolves and is therefore the
    only safe fallback.
    """
    short = short_name(new_fqn)
    relative = _relative_to(new_fqn, namespace)
    candidates = [relative, new_fqn, short] if "/" in written else [short, relative, new_fqn]
    for candidate in candidates:
        if candidate is not None and index.lookup(candidate, namespace) == new_fqn:
            return candidate
    return new_fqn


def _relative_to(fqn: str, namespace: str) -> str | None:
    """``fqn`` written relative to ``namespace``, or ``None`` if it is not below it."""
    if not namespace:
        return fqn
    prefix = f"{namespace}/"
    return fqn[len(prefix) :] if fqn.startswith(prefix) else None


# --------------------------------------------------------------------------- #
# Rewriting
# --------------------------------------------------------------------------- #

#: The two keys of each two-part reference form, in the mapping spelling.
#: A role that is absent from these is a plain scalar reference.
_ELEMENT_KEY: Final[dict[ReferenceRole, str]] = {
    ReferenceRole.ENDPOINT: "device",
    ReferenceRole.POWER_INPUT: "pdu",
}
_DETAIL_KEY: Final[dict[ReferenceRole, str]] = {
    ReferenceRole.ENDPOINT: "interface",
    ReferenceRole.POWER_INPUT: "outlet",
}


def rewrite_reference(document: Any, reference: Reference, replacement: str) -> bool:
    """Point ``reference`` at ``replacement`` in the raw ``document``.

    Both spellings of a two-part reference are handled: the compact
    ``sw1:eth0`` string and the ``{device: sw1, interface: eth0}`` mapping keep
    the shape they were written in.

    Returns:
        ``True`` when the document changed, ``False`` when the value there
        already reads as ``replacement``.

    Raises:
        EditError: The document holds no such reference — which means the value
            came from a template and has to be changed where it was written.
    """
    container, key = locate_reference(document, reference)
    value = container[key]
    if isinstance(value, str):
        written, separator, rest = value.partition(":")
        if _ELEMENT_KEY.get(reference.role) is None:
            written, separator, rest = value, "", ""
        if written == replacement:
            return False
        container[key] = f"{replacement}{separator}{rest}" if separator else replacement
        return True
    if value[_MAPPING_KEY(reference)] == replacement:
        return False
    value[_MAPPING_KEY(reference)] = replacement
    return True


def drop_reference(document: Any, reference: Reference) -> None:
    """Remove the reference itself: a list entry, or an optional scalar field.

    Used by a cascading delete for the references that are not structural — an
    adapter's ``attached_to``, a power input, a group membership — where the
    referring element survives without them.

    Raises:
        EditError: The document holds no such reference.
    """
    container, key = locate_reference(document, reference)
    if isinstance(container, list) and isinstance(key, int):
        container.pop(key)
        return
    del container[key]


def locate_reference(document: Any, reference: Reference) -> tuple[Any, str | int]:
    """The container and key the raw document holds this reference at.

    The path the model reported is tried first and *checked*: a document may
    write a cable's endpoints in either order, the model sorts them (§7.1), and
    the position an endpoint was written at is bookkeeping that not every way of
    loading an element carries. So when the path does not hold what the model
    said, the sibling entries are searched for the one that does — and only a
    unique match is accepted, because rewriting the wrong end of a cable is
    worse than refusing to rewrite either.

    Raises:
        EditError: Nothing at, or beside, that path reads as the reference. That
            is what a value contributed by a template looks like from here, and
            the message says so.
    """
    container = get_field(document, reference.path[:-1])
    key = reference.path[-1]
    if isinstance(container, (dict, list)):
        try:
            if _reads_as(container[key], reference):  # type: ignore[index]
                return container, key
        except (KeyError, IndexError, TypeError):
            pass
    if isinstance(container, list):
        matches = [
            position for position, entry in enumerate(container) if _reads_as(entry, reference)
        ]
        if len(matches) == 1:
            return container, matches[0]
    raise EditError(
        f"{reference.source}: {format_field_path(reference.path)} does not read as "
        f"{reference.target!r}; the value comes from a template and has to be changed in the "
        f"template that declares it"
    )


def _MAPPING_KEY(reference: Reference) -> str:
    """The mapping key holding the element part of a two-part reference."""
    return _ELEMENT_KEY[reference.role]


def _reads_as(value: Any, reference: Reference) -> bool:
    """Is ``value`` the reference the model reported, in either spelling?"""
    element_key = _ELEMENT_KEY.get(reference.role)
    if isinstance(value, str):
        if element_key is None:
            return value == reference.target
        written, separator, detail = value.partition(":")
        return bool(separator) and written == reference.target and detail == reference.detail
    if element_key is not None and isinstance(value, dict) and element_key in value:
        detail_key = _DETAIL_KEY[reference.role]
        return value[element_key] == reference.target and value.get(detail_key) == reference.detail
    return False
