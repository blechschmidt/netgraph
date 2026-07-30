"""Power: what a device draws, where it is fed from, and PoE (§17).

An as-built physical document has two halves. The first — what is bolted where
and what is patched into what — is :mod:`netgraph.models.patchpanel` and
``metadata.location``. This is the second: which outlet each power supply is
plugged into, how much the box draws, and which ports hand power *down* the
cable instead of taking it from an outlet.

It is worth modelling for the same reason cabling is: the mistakes are silent
and expensive. A rack fed from one PDU looks fine on a topology diagram and
fails as a unit. A PoE budget oversubscribed by two cameras works until the
third one is plugged in, and then browns out a switch. A device with no declared
power path is a device somebody will not find during a maintenance window.

Three blocks
------------

``spec.power``
    On a device: what it draws (:class:`PowerDraw`), which outlets feed its power
    supplies (:class:`PowerInput`), whether the feeds are meant to be
    independent (``redundant``), how much PoE the box can hand out
    (``poe_budget_watts``), and whether it takes its own power over its uplink
    (``powered_by: poe``).
``interfaces[].poe``
    On one port of a switch: that this port is a PSE, which standard it
    implements, and how much of the shared budget it reserves —
    :class:`PoeConfig`.
``spec.outlets`` / ``spec.capacity_watts``
    On a :mod:`~netgraph.models.pdu` document: the outlets that exist and how
    many watts may be drawn through them in total.

The vocabulary
--------------

The standards are unusually concrete here, so nothing below is invented:

* **PoE classes** come from IEEE 802.3-2022 clause 33/145. A class fixes the
  power the PSE must reserve *and* the power the PD may draw, and the two
  numbers differ by the cable loss the standard budgets for; both tables are
  here (:data:`POE_PSE_WATTS`, :data:`POE_PD_WATTS`) because a switch budget is
  computed from the first and a device's draw from the second.
* **The standards** name which classes exist: ``802.3af`` stops at class 3,
  ``802.3at`` adds class 4, ``802.3bt`` adds 5 to 8. A class outside its
  standard is ``NG-E004`` — a real configuration error, not a rounding one.
* **RFC 3621** (Power Ethernet MIB) is where the per-port and per-switch nodes
  come from: ``pethPsePortPowerClassifications`` is the class, ``pethMainPsePower``
  is the shared budget, ``pethPsePortAdminEnable`` is ``enabled``.
* **RFC 7460** (Power and Energy Monitoring MIB) is where a whole box's power
  numbers come from: ``eoPowerNameplate`` is a rated capacity, ``eoPower`` is what
  is actually drawn. See ``docs/yang-mapping.md``.

No operational state, as everywhere else: there is nowhere to put a *measured*
watt reading, because a number a file claims is stale before the file is saved.
``draw_watts`` is the nameplate figure a load schedule is built from.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Any, Final

from pydantic import BeforeValidator, Field, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.scalars import Boolean, ElementRef, Watts

__all__ = [
    "MAX_POWER_INPUTS",
    "POE_CLASS_MAX",
    "POE_PD_WATTS",
    "POE_PSE_WATTS",
    "OutletId",
    "PoeClass",
    "PoeConfig",
    "PoeStandard",
    "PowerConfig",
    "PowerDraw",
    "PowerInput",
    "PowerSource",
    "format_watts",
]

#: PSE-side power per IEEE 802.3 class: what a port must *reserve* from the
#: switch budget, cable loss included (802.3-2022 Table 145-1). Class 0 is
#: "unclassified" and reserves the class-3 figure, which is what every
#: implementation does with it.
POE_PSE_WATTS: Final[Mapping[int, float]] = {
    0: 15.4,
    1: 4.0,
    2: 7.0,
    3: 15.4,
    4: 30.0,
    5: 45.0,
    6: 60.0,
    7: 75.0,
    8: 90.0,
}

#: PD-side power per class: the most the *powered device* may draw, which is the
#: PSE figure less the loss the standard budgets for 100 m of cable. This is the
#: number to compare a camera's nameplate draw against.
POE_PD_WATTS: Final[Mapping[int, float]] = {
    0: 12.95,
    1: 3.84,
    2: 6.49,
    3: 12.95,
    4: 25.5,
    5: 40.0,
    6: 51.0,
    7: 62.0,
    8: 71.3,
}

#: Highest class each amendment defines. ``NG-E004`` refuses anything above it:
#: an ``802.3af`` port cannot deliver class 4, so declaring one is a mistake
#: about the hardware rather than a preference.
POE_CLASS_MAX: Final[Mapping[str, int]] = {"802.3af": 3, "802.3at": 4, "802.3bt": 8}

#: Ceiling on ``power.inputs``. Four-PSU chassis exist; forty do not, and the
#: bound keeps a copy-paste accident from becoming a load schedule nobody reads.
MAX_POWER_INPUTS: Final = 8


class PoeStandard(str, Enum):
    """Which IEEE 802.3 amendment a PSE port implements (§17.3)."""

    #: 802.3af-2003, "PoE": classes 0 to 3, up to 15.4 W at the port.
    AF = "802.3af"
    #: 802.3at-2009, "PoE+": adds class 4, up to 30 W at the port.
    AT = "802.3at"
    #: 802.3bt-2018, "PoE++": adds classes 5 to 8, up to 90 W at the port.
    BT = "802.3bt"

    @property
    def max_class(self) -> int:
        """The highest class this amendment defines."""
        return POE_CLASS_MAX[self.value]

    @property
    def max_watts(self) -> float:
        """PSE-side power of this amendment's highest class."""
        return POE_PSE_WATTS[self.max_class]

    def __str__(self) -> str:
        return self.value


class PowerSource(str, Enum):
    """Where a device's own power comes from (§17.2)."""

    #: A PDU outlet, or something the inventory does not model. The default:
    #: almost everything is plugged into a socket.
    OUTLET = "outlet"
    #: The uplink. A ceiling access point or a camera has no power cord, and its
    #: power path is the cable that carries its traffic — which is why
    #: ``NG-E014`` checks the far end of that cable offers PoE at all.
    POE = "poe"

    def __str__(self) -> str:
        return self.value


#: ``interfaces[].poe.class`` — an IEEE 802.3 classification, 0 to 8.
PoeClass = Annotated[int, Field(strict=True, ge=0, le=8)]

#: ``power.inputs[].outlet`` — an outlet as the PDU numbers it. Alphanumeric
#: rather than digits alone so a two-bank PDU labelled ``A1``…``B12`` can be
#: transcribed as it is printed; the outlet still has to exist (``NG-E011``).
OutletId = Annotated[str, Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9]+$")]


def _normalise_draw(value: Any) -> Any:
    """Accept ``draw_watts: 45`` as shorthand for ``{typical: 45}`` (§17.2).

    The typical figure is the one every load schedule is built from and the only
    one most nameplates state, so requiring a mapping to say it would be
    ceremony. A number becomes the typical draw; a mapping is taken as written.
    """
    if isinstance(value, bool):
        raise field_error("'draw_watts' is a number of watts, not a boolean", rule="NG-E003")
    if isinstance(value, (int, float)):
        return {"typical": value}
    return value


class PowerDraw(NetgraphModel):
    """``spec.power.draw_watts`` — the nameplate load of one device (§17.2).

    ``typical`` is what a load schedule sums: the steady-state draw of the box as
    configured. ``maximum`` is the nameplate or PSU rating, which is what a
    breaker has to survive; it is optional because most equipment lists only one
    figure, and it must not be below ``typical`` (``NG-E003``).

    Both map to RFC 7460: ``eoPower`` for the load and ``eoPowerNameplate`` for
    the rating.
    """

    typical: Watts
    maximum: Watts | None = None

    @model_validator(mode="after")
    def _check_order(self) -> PowerDraw:
        if self.maximum is not None and self.maximum < self.typical:
            raise field_error(
                f"'maximum' is {self.maximum} W but 'typical' is {self.typical} W; the maximum "
                f"draw cannot be below the typical one",
                rule="NG-E003",
                path=("maximum",),
            )
        return self

    @property
    def worst_case(self) -> float:
        """The figure a breaker has to survive: the maximum, or the typical."""
        return self.maximum if self.maximum is not None else self.typical

    def describe(self) -> str:
        """``120 W (max 250 W)`` — one clause, for a label or a diagnostic."""
        text = f"{format_watts(self.typical)} W"
        if self.maximum is not None:
            text += f" (max {format_watts(self.maximum)} W)"
        return text


class PowerInput(NetgraphModel):
    """One entry of ``spec.power.inputs`` — one PSU and the outlet feeding it.

    Accepts the compact string form ``pdu-r1-a:7`` and the equivalent mapping
    form, the same grammar a cable endpoint uses (§4.2), because it is the same
    kind of fact: a named thing on a named element. ``psu`` labels the supply on
    the *device* side, which is what an operator reads off the back of a chassis
    and what makes a diagnostic about "the second input" name something real.
    """

    #: The PDU. An :data:`~netgraph.models.scalars.ElementRef`, so it may be
    #: written fully qualified to pick one of several PDUs sharing a short name.
    pdu: ElementRef
    #: The outlet on it, as the PDU numbers it. Must exist (``NG-E011``) and
    #: must not already feed something else (``NG-E010``).
    outlet: OutletId
    #: The power supply this feeds, e.g. ``psu1``. Documentation only.
    psu: str | None = Field(default=None, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        if isinstance(value, PowerInput):
            return value
        if isinstance(value, str):
            pdu, separator, outlet = value.partition(":")
            if not separator:
                raise field_error(
                    f"{echo_value(value)} is not an outlet reference; expected 'pdu:outlet'",
                    rule="NG-E002",
                )
            if ":" in outlet:
                raise field_error(
                    f"{echo_value(value)} contains more than one ':'; an outlet is named by "
                    f"one identifier",
                    rule="NG-E002",
                )
            return {"pdu": pdu, "outlet": outlet}
        return value

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.pdu, self.outlet)

    def __str__(self) -> str:
        return f"{self.pdu}:{self.outlet}"


class PoeConfig(NetgraphModel):
    """``interfaces[].poe`` — this port is a PSE (§17.3).

    Declaring the block is what makes the port a power sourcing equipment port;
    ``enabled: false`` records a port that *could* source power and is
    administratively told not to, which is ``pethPsePortAdminEnable`` and is the
    difference between "no PoE here" and "PoE turned off here".

    How much the port reserves is said in one of two ways, and never both
    (``NG-E004``):

    * ``class`` — an IEEE classification. The reservation is the PSE-side figure
      for that class (:data:`POE_PSE_WATTS`), which is what the switch actually
      takes out of its budget.
    * ``budget_watts`` — an explicit reservation, for a vendor that lets an
      operator cap a port below its class.

    With neither, the port reserves its standard's maximum: that is what a switch
    with no per-port configuration does, and assuming less would make an
    oversubscribed budget look fine.
    """

    #: Which amendment the port implements (``pethPsePortType``).
    standard: PoeStandard
    #: The IEEE classification, 0 to 8. Refused above the standard's own
    #: ceiling (``NG-E004``). Written ``class`` in YAML.
    pse_class: PoeClass | None = Field(default=None, alias="class")
    #: An explicit reservation in watts, instead of a class.
    budget_watts: Watts | None = None
    #: ``pethPsePortAdminEnable``. A disabled PSE port reserves nothing and
    #: powers nothing, which is what ``NG-E014`` reports it for.
    enabled: Boolean = True

    @model_validator(mode="after")
    def _check_allocation(self) -> PoeConfig:
        if self.pse_class is not None and self.budget_watts is not None:
            raise field_error(
                "declare either 'class' or 'budget_watts', not both: a class already fixes "
                "the reservation, and two answers cannot both be the budget",
                rule="NG-E004",
                path=("budget_watts",),
            )
        if self.pse_class is not None and self.pse_class > self.standard.max_class:
            raise field_error(
                f"class {self.pse_class} is not defined by {self.standard}, which stops at "
                f"class {self.standard.max_class} ({format_watts(self.standard.max_watts)} W); "
                f"declare 'standard: 802.3bt' if the hardware really delivers it",
                rule="NG-E004",
                path=("class",),
            )
        return self

    @property
    def allocation_watts(self) -> float:
        """What this port takes out of the switch's budget.

        Zero when the port is administratively disabled: a switch does not
        reserve power for a PSE that is told not to source any.
        """
        if not self.enabled:
            return 0.0
        if self.budget_watts is not None:
            return self.budget_watts
        if self.pse_class is not None:
            return POE_PSE_WATTS[self.pse_class]
        return self.standard.max_watts

    @property
    def deliverable_watts(self) -> float:
        """The most a device on this port may draw, PD-side.

        The PD figure, not the PSE one: the difference is the cable loss the
        standard budgets for, and a camera drawing the PSE figure would be
        outside the standard. ``NG-E014`` compares a declared draw against this.
        """
        if self.budget_watts is not None:
            return self.budget_watts
        if self.pse_class is not None:
            return POE_PD_WATTS[self.pse_class]
        return POE_PD_WATTS[self.standard.max_class]

    def describe(self) -> str:
        """``802.3at class 4, 30.0 W`` — one clause for a label."""
        parts = [str(self.standard)]
        if self.pse_class is not None:
            parts.append(f"class {self.pse_class}")
        text = " ".join(parts)
        if not self.enabled:
            return f"{text}, disabled"
        return f"{text}, {format_watts(self.allocation_watts)} W"


class PowerConfig(NetgraphModel):
    """``spec.power`` — a device's power, both directions (§17.2).

    "Both directions" is the whole shape of it. A switch *takes* power through
    ``inputs`` and *gives* it through ``poe_budget_watts`` and the ``poe`` blocks
    on its ports; an access point takes power over its uplink and gives none. One
    block covers all three because they are one question — where does the power
    in this box come from and go to — and splitting it would put two halves of
    one answer in two places.
    """

    #: What the device draws. A bare number is the typical draw.
    draw_watts: Annotated[PowerDraw | None, BeforeValidator(_normalise_draw)] = None
    #: One entry per power supply, naming the outlet feeding it. Empty for a
    #: device fed over PoE, or for one whose feed is simply not recorded yet —
    #: which is ``NG-E016``, a warning, so a partial inventory stays usable.
    inputs: list[PowerInput] = Field(default_factory=list, max_length=MAX_POWER_INPUTS)
    #: The feeds are meant to be independent: losing one must not lose the
    #: device. Requires at least two inputs (``NG-E002``), and ``NG-E015``
    #: checks they land somewhere that makes the claim true.
    redundant: Boolean = False
    #: Where the device's own power comes from. ``poe`` says the uplink, and
    #: excludes ``inputs`` (``NG-E005``).
    powered_by: PowerSource = PowerSource.OUTLET
    #: The PoE power this device can hand out across every PSE port together
    #: (``pethMainPsePower``). ``NG-E013`` checks the ports fit inside it.
    poe_budget_watts: Watts | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> PowerConfig:
        seen: dict[tuple[str, str], int] = {}
        for index, entry in enumerate(self.inputs):
            first = seen.setdefault(entry.sort_key, index)
            if first != index:
                raise field_error(
                    f"input {index + 1} names outlet {entry}, which input {first + 1} already "
                    f"names; one outlet feeds one supply",
                    rule="NG-E002",
                    path=("inputs", index),
                )

        if self.powered_by is PowerSource.POE and self.inputs:
            raise field_error(
                "'powered_by: poe' means the device takes its power over its uplink, so it "
                "has no outlet inputs; drop 'inputs', or drop 'powered_by'",
                rule="NG-E005",
                path=("inputs",),
            )

        if self.redundant and len(self.inputs) < 2:
            raise field_error(
                f"'redundant: true' claims the device survives losing a feed, which needs at "
                f"least two inputs; {_input_count(len(self.inputs))}",
                rule="NG-E002",
                path=("redundant",),
            )
        return self

    @property
    def is_empty(self) -> bool:
        """Does the block say nothing at all about power?"""
        return (
            self.draw_watts is None
            and not self.inputs
            and self.poe_budget_watts is None
            and self.powered_by is PowerSource.OUTLET
        )

    @property
    def typical_watts(self) -> float:
        """The declared typical draw, or zero when none is declared."""
        return self.draw_watts.typical if self.draw_watts is not None else 0.0

    @property
    def worst_case_watts(self) -> float:
        """The declared worst-case draw, or zero when none is declared."""
        return self.draw_watts.worst_case if self.draw_watts is not None else 0.0

    @property
    def is_poe_powered(self) -> bool:
        return self.powered_by is PowerSource.POE


def _input_count(count: int) -> str:
    """``it declares one`` / ``it declares none`` — the tail of NG-E002."""
    return "it declares none" if count == 0 else "it declares one"


def format_watts(value: float) -> str:
    """Render a wattage the way a nameplate does: no trailing ``.0``.

    ``15.4`` keeps its decimal because the PoE tables are written that way;
    ``120`` loses one because no nameplate carries it. Shared by every label,
    table and export so one figure has one spelling everywhere.
    """
    rounded = round(value, 1)
    return f"{rounded:.0f}" if rounded == int(rounded) else f"{rounded:.1f}"
