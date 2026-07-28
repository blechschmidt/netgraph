"""The ``cable`` element (§7 of ``docs/schema.md``).

A cable is an undirected physical link between exactly two interfaces. It is a
first-class element so that it can carry its own metadata and be validated
independently of the devices it joins.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Literal

from pydantic import model_serializer, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.element import ElementBase
from netgraph.models.scalars import BitRate, ElementRef, IfName, LengthMetres

__all__ = ["Cable", "CableSpec", "Duplex", "InterfaceRef", "Medium"]


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

    @model_serializer
    def _serialise(self) -> str:
        return str(self)

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

    @model_validator(mode="after")
    def _normalise(self) -> CableSpec:
        if len(self.endpoints) != 2:
            raise field_error(
                f"a cable joins exactly two interfaces, got {len(self.endpoints)}",
                rule="NG-C001",
                path=("endpoints",),
            )
        # §7.1: the link is undirected, so the endpoint order carries no
        # meaning. Sorting makes the graph edge and the JSON export canonical.
        self.endpoints.sort(key=lambda ref: ref.sort_key)

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
