"""The ``patchpanel`` element (§15 of ``docs/schema.md``).

A patch panel is a *passive cross-connect*: numbered ports on the front,
numbered ports on the rear, and a fixed coupler joining each front port to
exactly one rear port. Nothing in it powers on, nothing in it makes a decision,
and a frame that enters one side leaves the other unchanged.

Modelling it matters because a real run almost never goes device to device. It
goes switch port → panel front → (structured cabling) → panel rear → server
port, and an inventory with no panel has to *lie* about that run by cabling the
two devices together directly — which loses the two things a patch record
exists for: which panel position the run occupies, and which rear port is still
free.

Why the ports are interfaces
----------------------------

A cable terminates on a panel port exactly as it terminates on a device port
(§7.1), so a panel port has to *be* an :class:`~netviz.models.interface.Interface`
rather than something a cable endpoint needs a second spelling for. The list is
derived from :attr:`PatchPanelSpec.ports` rather than written out: a 24-port
panel is 48 interfaces, all identical bar the number, which is precisely the
typing that made ``NG-R001`` ranges necessary in the first place.

The names are ``front/<n>`` and ``rear/<n>``. A panel port is therefore written
in a cable exactly like any other endpoint::

    endpoints:
      - sw-access-01:GigabitEthernet1/0/7
      - pp-idf-a:front/7

Front-to-rear mapping
---------------------

The default coupling is the identity — ``front/7`` is wired to ``rear/7``,
which is what the numbering on a real panel means. :attr:`PatchPanelSpec.couplers`
overrides it for a panel that is cross-wired, and is checked here so that a
mapping naming a port the panel does not have is refused at load rather than
producing a run that silently goes nowhere.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from functools import cached_property
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import BeforeValidator, Field, model_validator

from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.element import ElementBase
from netviz.models.interface import Interface, InterfaceType
from netviz.models.positions import (
    POSITION_RANGE_PATTERN,
    expand_positions,
    normalise_positions,
)
from netviz.models.style import Style

__all__ = [
    "FRONT",
    "MAX_PANEL_PORTS",
    "PATCHPANEL_KIND",
    "REAR",
    "PanelSide",
    "PatchPanel",
    "PatchPanelSpec",
    "PortRange",
    "panel_port",
    "parse_port_range",
    "split_panel_port",
]

#: ``kind`` of a patch panel. Named once so nothing downstream spells it out.
PATCHPANEL_KIND: Final = "patchpanel"

#: The two sides of a panel, and the prefix each one's port names carry.
FRONT: Final = "front"
REAR: Final = "rear"

#: Both sides, in the order a run crosses them.
PanelSide: Final[tuple[str, str]] = (FRONT, REAR)

#: ``NG-P006`` — ceiling on the positions one panel may declare. The largest
#: panel anyone ships is 96 positions in 4U; 1024 leaves room for a whole rack
#: modelled as one document and still bounds what a typo can ask for.
MAX_PANEL_PORTS: Final = 1024

#: What a normalised ``ports`` value looks like, for the JSON Schema.
PORT_RANGE_PATTERN: Final = POSITION_RANGE_PATTERN

#: What :func:`~netviz.models.positions.expand_positions` is told a panel's
#: numbered things are, so every ``NG-P006`` diagnostic reads the same way.
_PORT_RANGE: Final[dict[str, Any]] = {
    "field": "ports",
    "rule": "NG-P006",
    "limit": MAX_PANEL_PORTS,
    "noun": "patch panel",
    "unit": "position",
}


def parse_port_range(value: Any) -> tuple[str, ...]:
    """Expand a ``ports`` shorthand into the port numbers it names (``NG-P006``).

    A thin wrapper over :func:`~netviz.models.positions.expand_positions`; a
    PDU's ``outlets`` takes the same shorthand and shares the implementation.

    Returns:
        The port numbers as written, in ascending span order.
    """
    return expand_positions(value, **_PORT_RANGE)


def _normalise_ports(value: Any) -> Any:
    """Canonicalise ``ports`` to its string form, rejecting what cannot expand."""
    return normalise_positions(value, **_PORT_RANGE)


#: ``spec.ports`` — a count or a comma-separated list of spans, normalised to
#: the string form so that ``24`` and ``1-24`` are one value on the model.
PortRange = Annotated[str, BeforeValidator(_normalise_ports), Field(min_length=1)]


def _normalise_couplers(value: Any) -> Any:
    """Allow YAML's integer keys: ``{1: 24}`` and ``{'1': '24'}`` are one mapping."""
    if not isinstance(value, Mapping):
        return value
    return {
        (str(key) if isinstance(key, int) and not isinstance(key, bool) else key): (
            str(item) if isinstance(item, int) and not isinstance(item, bool) else item
        )
        for key, item in value.items()
    }


def panel_port(side: str, number: str) -> str:
    """The interface name of one panel position, e.g. ``front/7``."""
    return f"{side}/{number}"


def split_panel_port(name: str) -> tuple[str, str] | None:
    """``front/7`` → ``("front", "7")``; ``None`` for anything else."""
    side, separator, number = name.partition("/")
    if not separator or side not in PanelSide or not number:
        return None
    return side, number


class PatchPanelSpec(NetvizModel):
    """``spec`` of a ``patchpanel`` document (§15.1)."""

    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    #: Descriptive: ``keystone``, ``fibre-lc``, ``coupler``.
    form_factor: str | None = None
    #: The positions the panel has, as a count (``24``) or spans (``1-12,17-24``).
    ports: PortRange
    #: Front position -> rear position, for a panel that is not wired straight
    #: through. Absent means the identity mapping, which is what the numbering
    #: printed on a real panel promises.
    couplers: Annotated[dict[str, str] | None, BeforeValidator(_normalise_couplers)] = None
    #: How this element is drawn (§22): a ``fill``, a ``stroke``, a ``shape``
    #: and six more, each optional and each inheriting from the theme, then the
    #: icon set, then the built-in palette when absent. See
    #: :mod:`netviz.models.style`.
    style: Style | None = None

    @cached_property
    def port_numbers(self) -> tuple[str, ...]:
        """Every position the panel has, in the order ``ports`` declares them."""
        return parse_port_range(self.ports)

    @cached_property
    def interfaces(self) -> list[Interface]:
        """The front ports then the rear ports, as ordinary interfaces (§15.1).

        Derived rather than declared: a panel port carries no address, no VLAN
        and no MAC — it is a hole with a number — so the only thing a document
        would add by writing them out is the chance to get one wrong.
        """
        return [
            Interface(
                name=panel_port(side, number),
                type=InterfaceType.ETHERNET,
                description=f"{side} position {number}",
            )
            for side in PanelSide
            for number in self.port_numbers
        ]

    @cached_property
    def _by_name(self) -> dict[str, Interface]:
        return {interface.name: interface for interface in self.interfaces}

    @cached_property
    def coupling(self) -> Mapping[str, str]:
        """Every port to the port it is coupled to, both directions (§15.2).

        Symmetric on purpose: a run may enter a panel from either side, and a
        walk that had to know which side it was on would get the answer wrong
        exactly once — at the panel where the operator patched the rear.
        """
        declared = self.couplers or {}
        mapping: dict[str, str] = {}
        for number in self.port_numbers:
            front = panel_port(FRONT, number)
            rear = panel_port(REAR, declared.get(number, number))
            mapping[front] = rear
            mapping[rear] = front
        return mapping

    def interface(self, name: str) -> Interface | None:
        """Look a panel port up by name."""
        return self._by_name.get(name)

    @model_validator(mode="after")
    def _check_couplers(self) -> PatchPanelSpec:
        if self.couplers is None:
            return self
        known = set(self.port_numbers)
        used: dict[str, str] = {}
        for front, rear in self.couplers.items():
            for side, number in ((FRONT, front), (REAR, rear)):
                if number not in known:
                    raise field_error(
                        f"'couplers' names {side} position {number!r}, which 'ports' does not "
                        f"declare",
                        rule="NG-P007",
                        path=("couplers", front),
                    )
            if rear in used:
                raise field_error(
                    f"'couplers' wires front {front!r} and front {used[rear]!r} to the same "
                    f"rear position {rear!r}; a coupler joins exactly two positions",
                    rule="NG-P007",
                    path=("couplers", front),
                )
            used[rear] = front
        return self


class PatchPanel(ElementBase):
    """A passive cross-connect: front positions coupled to rear positions."""

    kind: Literal["patchpanel"] = "patchpanel"
    spec: PatchPanelSpec

    default_glyph: ClassVar[str] = "patchpanel"

    @property
    def interfaces(self) -> list[Interface]:
        """The panel's ports, front then rear (§15.1)."""
        return self.spec.interfaces

    @property
    def port_numbers(self) -> tuple[str, ...]:
        """Every position the panel has."""
        return self.spec.port_numbers

    @property
    def coupling(self) -> Mapping[str, str]:
        """Port to coupled port, both directions."""
        return self.spec.coupling

    def interface(self, name: str) -> Interface | None:
        """Look a panel port up by name."""
        return self.spec.interface(name)

    def interface_names(self) -> Iterator[str]:
        """Every name a cable endpoint may refer to on this panel."""
        for interface in self.spec.interfaces:
            yield interface.name

    def opposite(self, port: str) -> str | None:
        """The port ``port`` is coupled to, or ``None`` if it has none."""
        return self.spec.coupling.get(port)
