"""The ``pdu`` element (§17.1 of ``docs/schema.md``).

A power distribution unit is the power half of what
:mod:`netviz.models.patchpanel` is for the data half: a strip of numbered
holes, bolted in a rack, that things plug into. Like a panel it is shaped by its
numbering rather than by its configuration — a 24-outlet vertical strip is
twenty-four identical facts — so ``outlets`` takes the same count-or-range
shorthand ``ports`` does, and for the same reason (§17.1).

Why it is an element and not a field
------------------------------------

Because two devices share one, and that sharing is the fact worth drawing. A
``power`` block on a server can say "PSU 1 is fed from outlet 7"; only a document
for the PDU itself can answer "what else is on that PDU, and is there capacity
left". Both questions are what a load schedule is, and the second is the one that
catches a rack fed from a single strip.

Why the outlets are not interfaces
----------------------------------

A patch-panel position *is* an interface, because a ``cable`` document terminates
on it. An outlet is not: a power cord is not a ``cable``, it carries no frames,
and giving a PDU forty-eight interfaces would put it in the layer-1 topology as a
node nothing is connected to. The reference goes the other way instead — a
device's ``power.inputs`` names ``pdu:outlet`` — which is also the direction the
fact is discovered in: you read the label on the cord, not on the strip.

Feeds
-----

:attr:`PduSpec.input_feed` is what makes A/B redundancy expressible. Two PDUs in
one rack are only independent if they are fed from different places, and the
inventory has no way to know whether they are unless somebody writes it down.
It is free text on purpose: what counts as "a different feed" is site knowledge
(two utility feeds, two UPS strings, a UPS and a generator), and netviz's job
is to notice that two feeds a device calls redundant carry the same name
(``NV-E015``), not to decide what the names mean.
"""

from __future__ import annotations

from functools import cached_property
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import BeforeValidator, Field

from netviz.models.base import NetvizModel
from netviz.models.element import ElementBase
from netviz.models.positions import (
    POSITION_RANGE_PATTERN,
    expand_positions,
    normalise_positions,
)
from netviz.models.power import format_watts
from netviz.models.scalars import Watts
from netviz.models.style import Style

__all__ = [
    "MAX_PDU_OUTLETS",
    "OUTLET_RANGE_PATTERN",
    "PDU_KIND",
    "OutletRange",
    "Pdu",
    "PduSpec",
    "parse_outlet_range",
]

#: ``kind`` of a power distribution unit. Named once so nothing downstream
#: spells it out.
PDU_KIND: Final = "pdu"

#: ``NV-E001`` — ceiling on the outlets one PDU may declare. The largest strip
#: anybody ships is 54 outlets; 512 leaves room for a whole modular chassis
#: modelled as one document and still bounds what a typo can ask for.
MAX_PDU_OUTLETS: Final = 512

#: What a normalised ``outlets`` value looks like, for the JSON Schema.
OUTLET_RANGE_PATTERN: Final = POSITION_RANGE_PATTERN

#: What :func:`~netviz.models.positions.expand_positions` is told a PDU's
#: numbered things are, so every ``NV-E001`` diagnostic reads the same way.
_OUTLET_RANGE: Final[dict[str, Any]] = {
    "field": "outlets",
    "rule": "NV-E001",
    "limit": MAX_PDU_OUTLETS,
    "noun": "power distribution unit",
    "unit": "outlet",
}


def parse_outlet_range(value: Any) -> tuple[str, ...]:
    """Expand an ``outlets`` shorthand into the outlet numbers it names.

    Accepts a count — ``24`` means outlets 1 to 24 — or comma-separated spans:
    ``1-24``, ``1-12,17-24``, ``7``. Leading zeros are preserved from the low
    bound of each span, so a strip labelled ``01``…``24`` can be transcribed as
    it is printed.

    Returns:
        The outlet numbers as written, in ascending span order.

    Raises:
        pydantic_core.PydanticCustomError: The value is not a count or a range,
            a span is malformed or inverted, a number repeats, or the total
            exceeds :data:`MAX_PDU_OUTLETS`. The error carries ``NV-E001``.
    """
    return expand_positions(value, **_OUTLET_RANGE)


def _normalise_outlets(value: Any) -> Any:
    """Canonicalise ``outlets`` to its string form, rejecting what cannot expand."""
    return normalise_positions(value, **_OUTLET_RANGE)


#: ``spec.outlets`` — a count or comma-separated spans, normalised to the string
#: form so that ``24`` and ``1-24`` are one value on the model.
OutletRange = Annotated[str, BeforeValidator(_normalise_outlets), Field(min_length=1)]


class PduSpec(NetvizModel):
    """``spec`` of a ``pdu`` document (§17.1)."""

    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    #: Descriptive: ``vertical``, ``horizontal``, ``1U``, ``0U``.
    form_factor: str | None = None
    #: The outlets the unit has, as a count (``24``) or spans (``1-12,17-24``).
    outlets: OutletRange
    #: How many watts may be drawn through the unit in total
    #: (``eoPowerNameplate``). ``NV-E012`` sums the declared draws against it;
    #: absent means "not recorded", and the sum is then reported without a
    #: verdict rather than guessed at.
    capacity_watts: Watts | None = None
    #: Which supply feeds the unit — ``A``, ``B``, ``ups-1``, ``utility``. Free
    #: text; ``NV-E015`` compares two of them for equality and nothing more.
    input_feed: str | None = Field(default=None, max_length=64)
    #: How this element is drawn (§22): a ``fill``, a ``stroke``, a ``shape``
    #: and six more, each optional and each inheriting from the theme, then the
    #: icon set, then the built-in palette when absent. See
    #: :mod:`netviz.models.style`.
    style: Style | None = None

    @cached_property
    def outlet_numbers(self) -> tuple[str, ...]:
        """Every outlet the unit has, in the order ``outlets`` declares them."""
        return parse_outlet_range(self.outlets)

    @cached_property
    def _outlet_set(self) -> frozenset[str]:
        return frozenset(self.outlet_numbers)

    def has_outlet(self, number: str) -> bool:
        """Does the unit have an outlet numbered ``number``?

        Compared as written rather than numerically: a strip printed ``01``…``24``
        and one printed ``1``…``24`` are different labels, and quietly accepting
        ``7`` for ``07`` would put a cord in a hole nobody can find.
        """
        return number in self._outlet_set

    @property
    def outlet_count(self) -> int:
        return len(self.outlet_numbers)

    def describe(self) -> str:
        """``24 outlets, 3680 W, feed A`` — one line for a label."""
        parts = [f"{self.outlet_count} outlet{'' if self.outlet_count == 1 else 's'}"]
        if self.capacity_watts is not None:
            parts.append(f"{format_watts(self.capacity_watts)} W")
        if self.input_feed:
            parts.append(f"feed {self.input_feed}")
        return ", ".join(parts)


class Pdu(ElementBase):
    """A power distribution unit: numbered outlets, and what may be drawn."""

    kind: Literal["pdu"] = "pdu"
    spec: PduSpec

    default_glyph: ClassVar[str] = "pdu"
    #: A PDU owns no interfaces: an outlet is not a port and a cord is not a
    #: cable (see the module docstring). This is what keeps it out of the
    #: layer-1 topology and out of every cable-endpoint lookup.
    has_interfaces: ClassVar[bool] = False

    @property
    def outlet_numbers(self) -> tuple[str, ...]:
        """Every outlet the unit has."""
        return self.spec.outlet_numbers

    @property
    def capacity_watts(self) -> float | None:
        """The unit's rated capacity, or ``None`` when it is not recorded."""
        return self.spec.capacity_watts

    @property
    def input_feed(self) -> str:
        """The supply feeding the unit; ``""`` when it is not recorded."""
        return self.spec.input_feed or ""

    def has_outlet(self, number: str) -> bool:
        """Does the unit have an outlet numbered ``number``?"""
        return self.spec.has_outlet(number)
