"""The stable address of an element: ``device.core/sw-1``, ``cable.dc/uplink``.

A fully-qualified name (``core/sw-1``) says *where* an element is and what it is
called. It does not say what sort of thing it is, and two different sorts of
thing may legitimately carry the same name — a layout called ``default`` next to
a switch called ``default`` is not a clash (§18). A changeset has to name both
without ambiguity and has to survive being written to a file and read back, so
it addresses elements by **type and qualified name**.

The type is a *category*, not the document's ``kind``. ``device.core/sw-1``
addresses the element regardless of whether it is declared as a ``switch`` or a
``router``, which is what makes ``kind: switch`` → ``kind: router`` an update of
one element rather than the destruction of one and the creation of another.
Every other category is its own kind already, so the distinction only shows up
on devices — which is exactly where it matters.

The sidecars are addressed the same way and sort after the elements: ``layout``
first, then the three annotation kinds of §21. A changeset therefore reads the
network before the picture of it, and — because a note has an address of its own
that no device can collide with — a note can never be reported as a change to
the thing it is about.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Final

from netgraph.loader.inventory import namespace_of, qualify, short_name
from netgraph.models import (
    ANNOTATION_DOCUMENT_KINDS,
    AREA_KIND,
    KINDS,
    LAYOUT_KIND,
    LEGEND_KIND,
    NOTE_KIND,
)

__all__ = [
    "ADDRESS_TYPES",
    "ANNOTATION_TYPES",
    "AREA_TYPE",
    "DEVICE_TYPE",
    "LAYOUT_TYPE",
    "LEGEND_TYPE",
    "NOTE_TYPE",
    "SIDECAR_TYPES",
    "TYPE_OF_KIND",
    "Address",
    "AddressSyntaxError",
    "address_of",
    "parse_address",
]

#: The five device kinds share one address type; see the module docstring.
DEVICE_TYPE: Final = "device"

#: Geometry documents are addressable, so an arrangement can be planned and
#: applied like anything else, but they live in their own name space.
LAYOUT_TYPE: Final = "layout"

#: The three annotation kinds (§21). Each is its own address type, spelled the
#: same as its ``kind``: an annotation declares no network fact, so there is no
#: category above it for two of them to share the way five device kinds do.
NOTE_TYPE: Final = NOTE_KIND
AREA_TYPE: Final = AREA_KIND
LEGEND_TYPE: Final = LEGEND_KIND

#: The annotation address types, for the callers that ask "is this presentation?".
ANNOTATION_TYPES: Final[tuple[str, ...]] = (NOTE_TYPE, AREA_TYPE, LEGEND_TYPE)

#: Every address type that is *not* an element: geometry and annotations. They
#: are addressable and plannable, but they are never paired up structurally and
#: nothing in the graph depends on one.
SIDECAR_TYPES: Final[frozenset[str]] = frozenset((LAYOUT_TYPE, *ANNOTATION_TYPES))

#: Every ``kind`` mapped to the address type that names it.
TYPE_OF_KIND: Final[dict[str, str]] = {
    "switch": DEVICE_TYPE,
    "router": DEVICE_TYPE,
    "hub": DEVICE_TYPE,
    "computer": DEVICE_TYPE,
    "server": DEVICE_TYPE,
    "cable": "cable",
    "adapter": "adapter",
    "tunnel": "tunnel",
    "patchpanel": "patchpanel",
    "pdu": "pdu",
    "user": "user",
    "group": "group",
    LAYOUT_KIND: LAYOUT_TYPE,
    NOTE_KIND: NOTE_TYPE,
    AREA_KIND: AREA_TYPE,
    LEGEND_KIND: LEGEND_TYPE,
}

#: Every address type, in the order a plan lists them: the elements first, then
#: the sidecars that only describe how they are drawn. A reader of a changeset
#: wants to know what happened to the network before what happened to the
#: picture of it. Sorted longest-first for parsing so no prefix can shadow
#: another; the tuple itself stays readable.
ADDRESS_TYPES: Final[tuple[str, ...]] = (
    DEVICE_TYPE,
    "cable",
    "adapter",
    "tunnel",
    "patchpanel",
    "pdu",
    "user",
    "group",
    LAYOUT_TYPE,
    *ANNOTATION_TYPES,
)

#: Ranking used to order a changeset within one action, so that the thing a
#: reader looks for first is printed first.
_TYPE_ORDER: Final[dict[str, int]] = {name: rank for rank, name in enumerate(ADDRESS_TYPES)}

_KNOWN_KINDS: Final[frozenset[str]] = frozenset((*KINDS, LAYOUT_KIND, *ANNOTATION_DOCUMENT_KINDS))


class AddressSyntaxError(ValueError):
    """An address could not be parsed."""


@dataclass(frozen=True, slots=True)
class Address:
    """``<type>.<namespace>/<name>`` — what a changeset entry is about."""

    #: One of :data:`ADDRESS_TYPES`.
    type: str
    #: Fully-qualified name: ``namespace/name``, or just ``name`` at the root.
    fqn: str

    def __post_init__(self) -> None:
        if self.type not in _TYPE_ORDER:
            raise AddressSyntaxError(
                f"unknown address type {self.type!r}; expected one of {', '.join(ADDRESS_TYPES)}"
            )
        if not self.fqn:
            raise AddressSyntaxError("an address needs a name")

    @property
    def namespace(self) -> str:
        """The directory part, ``""`` at the root of the tree."""
        return namespace_of(self.fqn)

    @property
    def name(self) -> str:
        """The ``metadata.name`` part."""
        return short_name(self.fqn)

    def renamed(self, name: str) -> Address:
        """The same address with a different ``metadata.name``."""
        return Address(type=self.type, fqn=qualify(self.namespace, name))

    @property
    def order(self) -> tuple[int, str]:
        """Sort key: type first, then name, so a plan reads by category."""
        return (_TYPE_ORDER[self.type], self.fqn)

    def matches(self, pattern: str) -> bool:
        """Does ``--target`` ``pattern`` select this address?

        Three spellings are accepted, because all three are things an operator
        reasonably types: the whole address (``device.core/sw-1``), the
        qualified name (``core/sw-1``) and the short name (``sw-1``). Each is a
        shell-style glob, so ``device.*`` and ``core/*`` both work.
        """
        return any(
            fnmatchcase(candidate, pattern) for candidate in (str(self), self.fqn, self.name)
        )

    def __str__(self) -> str:
        return f"{self.type}.{self.fqn}"


def address_of(kind: str, fqn: str) -> Address:
    """The address of an element of ``kind`` declared as ``fqn``.

    Raises:
        AddressSyntaxError: ``kind`` is not a kind this revision knows. The
            loader can never produce one, so this only fires for a hand-built
            document.
    """
    if kind not in _KNOWN_KINDS:
        raise AddressSyntaxError(f"unknown element kind {kind!r}")
    return Address(type=TYPE_OF_KIND[kind], fqn=fqn)


def parse_address(text: str) -> Address:
    """Read ``device.core/sw-1`` back into an :class:`Address`.

    A ``metadata.name`` may itself contain a dot (§4.1), so the type is matched
    against the closed set of known prefixes rather than split off at the first
    separator — ``device.sw.1`` is the device ``sw.1``, not a type called
    ``device.sw``.

    Raises:
        AddressSyntaxError: The text carries no known type prefix, or nothing
            after it.
    """
    for candidate in ADDRESS_TYPES:
        prefix = f"{candidate}."
        if text.startswith(prefix):
            return Address(type=candidate, fqn=text[len(prefix) :])
    raise AddressSyntaxError(
        f"{text!r} is not an address; expected '<type>.<name>' with type one of "
        f"{', '.join(ADDRESS_TYPES)}"
    )
