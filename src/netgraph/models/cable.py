"""The ``cable`` element (§7 of ``docs/schema.md``).

A cable is an undirected physical link between exactly two interfaces. It is a
first-class element so that it can carry its own metadata and be validated
independently of the devices it joins.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Literal

from pydantic import PrivateAttr, model_serializer, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.element import ElementBase
from netgraph.models.scalars import BitRate, ElementRef, IfName, LengthMetres
from netgraph.models.style import Style

__all__ = ["Cable", "CableSpec", "Duplex", "InterfaceRef", "Medium", "sort_endpoints"]


class Medium(str, Enum):
    """``spec.medium`` (§7)."""

    COPPER = "copper"
    FIBER = "fiber"
    WIRELESS = "wireless"


class Duplex(str, Enum):
    """``spec.duplex`` (§7)."""

    FULL = "full"
    HALF = "half"


class InterfaceRef(NetgraphModel):
    """A ``device:interface`` reference (§4.2).

    Accepts the compact string form and the equivalent mapping form; both
    normalise to the same value and serialise back to ``device:interface``.

    The device part is an :data:`~netgraph.models.scalars.ElementRef`, not a
    declared name: it may be written fully qualified
    (``sites/berlin/rack1/sw1``) to pick one of several elements sharing a
    short name (§2.2).
    """

    device: ElementRef
    interface: IfName

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        if isinstance(value, InterfaceRef):
            return value
        if isinstance(value, str):
            device, separator, interface = value.partition(":")
            if not separator:
                raise ValueError(
                    f"{echo_value(value)} is not an interface reference; expected 'device:interface'"
                )
            if ":" in interface:
                raise ValueError(
                    f"{echo_value(value)} contains more than one ':'; interface names must not "
                    "contain a colon"
                )
            return {"device": device, "interface": interface}
        return value

    #: Position this reference held in ``spec.endpoints`` as it was *written*,
    #: before :func:`sort_endpoints` moved it. ``None`` for a reference that
    #: never went through an endpoint list.
    _document_index: int | None = PrivateAttr(default=None)

    @model_serializer
    def _serialise(self) -> str:
        return str(self)

    @property
    def document_index(self) -> int | None:
        """Where this endpoint sits in the document, or ``None`` if unknown.

        Kept on the reference rather than as a permutation on the spec because
        :meth:`__eq__` here compares :attr:`sort_key` and nothing else, so the
        bookkeeping stays invisible to equality — two cables written with their
        endpoints in opposite orders must still compare equal (§7.1).
        """
        return self._document_index

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.device, self.interface)

    def __str__(self) -> str:
        return f"{self.device}:{self.interface}"

    def __hash__(self) -> int:
        return hash(self.sort_key)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, InterfaceRef):
            return self.sort_key == other.sort_key
        if isinstance(other, str):
            return str(self) == other
        return NotImplemented


def sort_endpoints(endpoints: list[InterfaceRef]) -> None:
    """Put an endpoint list in canonical order, remembering where each entry was.

    Cables (§7.1) and tunnels (§14.3) are both undirected, so both sort
    ``spec.endpoints`` to make the graph edge and the JSON export canonical.
    Sorting moves entries away from the position they occupy in the document,
    and a diagnostic naming ``spec.endpoints[1]`` would then point at the
    *other* end of the link — in the machine-readable report formats, at the
    wrong line of the file. Each reference therefore keeps the index it was
    written at, in :attr:`InterfaceRef.document_index`.
    """
    for index, ref in enumerate(endpoints):
        ref._document_index = index
    endpoints.sort(key=lambda ref: ref.sort_key)


class CableSpec(NetgraphModel):
    """``spec`` of a ``cable`` document (§7)."""

    #: Exactly two entries (``NG-C001``); sorted for canonical output.
    endpoints: list[InterfaceRef]
    medium: Medium
    #: Negotiated link rate, projected onto ``if:speed`` of both endpoints (§9.4).
    speed: BitRate | None = None
    duplex: Duplex = Duplex.FULL
    length_m: LengthMetres | None = None
    category: str | None = None
    connector: str | None = None
    #: Physical cable-label / patch-panel identifier printed on the edge.
    label: str | None = None
    #: How this element is drawn (§22): a ``fill``, a ``stroke``, a ``shape``
    #: and six more, each optional and each inheriting from the theme, then the
    #: icon set, then the built-in palette when absent. See
    #: :mod:`netgraph.models.style`.
    style: Style | None = None

    @model_validator(mode="after")
    def _normalise(self) -> CableSpec:
        if len(self.endpoints) != 2:
            raise field_error(
                f"a cable joins exactly two interfaces, got {len(self.endpoints)}",
                rule="NG-C001",
                path=("endpoints",),
            )
        sort_endpoints(self.endpoints)

        if self.medium is Medium.WIRELESS:
            for key in ("length_m", "category"):
                if getattr(self, key) is not None:
                    raise field_error(
                        f"{key!r} is not allowed on a wireless link",
                        rule="NG-C007",
                        path=(key,),
                    )
        return self


class Cable(ElementBase):
    """An undirected physical link between exactly two interfaces."""

    kind: Literal["cable"] = "cable"
    spec: CableSpec

    has_interfaces: ClassVar[bool] = False

    @property
    def endpoints(self) -> list[InterfaceRef]:
        """Shortcut for ``spec.endpoints``."""
        return self.spec.endpoints

    @property
    def is_self_link(self) -> bool:
        """``NG-C004``: both endpoints sit on the same element."""
        return self.spec.endpoints[0].device == self.spec.endpoints[1].device

    def other_end(self, ref: InterfaceRef) -> InterfaceRef:
        """The endpoint that is not ``ref``."""
        first, second = self.spec.endpoints
        if ref == first:
            return second
        if ref == second:
            return first
        raise KeyError(f"{ref} is not an endpoint of cable {self.metadata.name!r}")
