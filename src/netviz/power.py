"""The power plan of an inventory: feeds, PDU loads and PoE budgets (§17).

Four consumers need the same answers and must not disagree about them:

* :mod:`netviz.validate` — is an outlet claimed twice, does a PDU's load fit
  inside its capacity, does a PoE-powered device have a PoE uplink;
* :mod:`netviz.render.graph` — the ``power`` layer, whose nodes are the PDUs
  and the powered elements and whose edges are the feeds;
* :mod:`netviz.cli` — ``netviz list power``, the per-PDU utilisation table;
* :mod:`netviz.export.power` — the load schedule.

So the resolution happens exactly once, here, the same way
:mod:`netviz.subnets` resolves addressing once for its three consumers. The
module is a pure function of an :class:`~netviz.loader.inventory.Inventory`:
nothing here reads a file, and nothing here reports a diagnostic — it produces
*facts*, including the fact that a reference did not resolve, and leaves the
grading of those facts to the validator.

Two ways to be fed
------------------

An **outlet feed** is declared from the load's side: a device's
``spec.power.inputs`` names ``pdu:outlet`` per power supply. Nothing on the PDU
mentions the device, which is the right direction — you read the label on the
cord, and a PDU with a list of its own downstream devices would be a second
place for the same fact to be wrong.

A **PoE feed** is not declared at all; it is *derived*. A device that says
``powered_by: poe`` takes its power over the run that carries its traffic, so the
feed is found by walking that run to the far end and looking for a ``poe`` block
on the port it lands on. The walk crosses patch panels — a run through a panel is
electrically one run, for power exactly as for frames (§15.2) — because a ceiling
access point patched through an IDF panel is the normal case, not the exotic one.

Sharing a load across feeds
---------------------------

A dual-corded server draws its load through both cords, so each of its two feeds
carries half of it. That is what :attr:`PduLoad.load_watts` sums, and it is the
figure a breaker is sized against in normal operation.

It is not the only figure worth having: when the other PDU fails, this one
carries the *whole* load of everything dual-corded to it.
:attr:`PduLoad.failover_watts` is that number, and both appear in the utilisation
table and the load schedule. Only the first one is graded (``NG-E012``): a pair of
PDUs each sized for half the rack is a design, not an error, and reporting it as
one would make the rule useless in the racks that have it right.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from netviz.loader.inventory import Inventory, namespace_of, short_name
from netviz.models import (
    Adapter,
    Device,
    PatchPanel,
    Pdu,
    PoeConfig,
    PowerConfig,
    PowerInput,
    format_watts,
)

__all__ = [
    "Feed",
    "FeedKind",
    "PduLoad",
    "PoeBudget",
    "PoePort",
    "PowerNode",
    "PowerPlan",
    "PowerRole",
    "UnresolvedInput",
    "Uplink",
    "format_utilisation_percent",
    "power_plan",
]

#: How many panels one run may cross before the walk gives up. A run crossing
#: more than this is a loop (``NG-P005``) or a plant nobody could trace; the
#: bound is what keeps a cross-wired pair of panels from hanging the resolver.
MAX_PATCH_HOPS: Final = 16


class FeedKind(str, Enum):
    """How power reaches an element."""

    #: A cord from a PDU outlet, declared by ``power.inputs``.
    OUTLET = "outlet"
    #: The uplink, derived from ``powered_by: poe`` and a PSE port at the far
    #: end of the run.
    POE = "poe"

    def __str__(self) -> str:
        return self.value


class PowerRole(str, Enum):
    """What an element is, power-wise. A switch is commonly two of these."""

    #: A ``pdu`` document: it distributes and consumes nothing itself.
    PDU = "pdu"
    #: It hands power out over its ports (``poe_budget_watts``, ``poe`` ports).
    PSE = "pse"
    #: It draws power (``draw_watts``, ``inputs``, ``powered_by``).
    LOAD = "load"

    def __str__(self) -> str:
        return self.value


class UnresolvedReason(str, Enum):
    """Why one ``power.inputs`` entry does not name a real outlet."""

    #: The reference resolves to no element at all.
    UNKNOWN_PDU = "unknown-pdu"
    #: It resolves to an element that is not a ``pdu``.
    NOT_A_PDU = "not-a-pdu"
    #: The PDU exists but has no outlet with that number.
    UNKNOWN_OUTLET = "unknown-outlet"
    #: More than one element carries the short name that was written.
    AMBIGUOUS_PDU = "ambiguous-pdu"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Feed:
    """One resolved power path into one element."""

    #: Fully-qualified name of the element being fed.
    element: str
    kind: FeedKind
    #: The PDU, for an outlet feed; the PSE element, for a PoE feed.
    source: str
    #: Outlet number on the PDU. Empty on a PoE feed.
    outlet: str = ""
    #: The PSE port on :attr:`source`. Empty on an outlet feed.
    port: str = ""
    #: The fed element's own interface the PoE arrives on. Empty on an outlet feed.
    peer_port: str = ""
    #: Panels the PoE run crosses, in the order it crosses them.
    through: tuple[str, ...] = ()
    #: The power-supply label on the load's side, e.g. ``psu1``.
    psu: str = ""
    #: Position in ``spec.power.inputs``, for the field path of a finding.
    index: int = 0
    #: What the source sets aside for this feed: the PSE-side class figure for
    #: PoE, and the load's share of its own draw for an outlet.
    reserved_watts: float = 0.0
    #: The whole typical draw of the fed element, however many feeds it has.
    element_watts: float = 0.0
    #: :attr:`~netviz.models.pdu.PduSpec.input_feed` of the PDU. Empty for a
    #: PoE feed and for a PDU that does not record one.
    input_feed: str = ""

    @property
    def id(self) -> str:
        """Stable identity, used as the edge id in the ``power`` layer."""
        if self.kind is FeedKind.POE:
            return f"{self.source}:{self.port}#poe:{self.element}"
        return f"{self.source}:{self.outlet}#power:{self.element}"

    @property
    def is_poe(self) -> bool:
        return self.kind is FeedKind.POE

    @property
    def source_label(self) -> str:
        """``pdu-r1-a:7`` / ``sw-access-01:Gi1/0/7`` — the socket, named."""
        return f"{self.source}:{self.outlet or self.port}"

    def describe(self) -> str:
        """One clause for an edge label."""
        parts = [f"outlet {self.outlet}" if self.outlet else self.port]
        if self.psu:
            parts.append(self.psu)
        if self.reserved_watts:
            parts.append(f"{format_watts(self.reserved_watts)} W")
        return ", ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class UnresolvedInput:
    """One ``power.inputs`` entry that names no outlet that exists (``NG-E011``)."""

    element: str
    index: int
    input: PowerInput
    reason: UnresolvedReason
    #: The PDU the reference did resolve to, when it resolved to something.
    pdu: str = ""
    #: Candidate fqns, when the short name was ambiguous.
    candidates: tuple[str, ...] = ()

    @property
    def field_path(self) -> tuple[str | int, ...]:
        return ("spec", "power", "inputs", self.index)


@dataclass(frozen=True, slots=True)
class Uplink:
    """One resolved far end of a run leaving a powered element.

    Recorded for every interface of every ``powered_by: poe`` element, whether or
    not the far end sources power, because "the uplink lands on a port with no
    PoE" is exactly what ``NG-E014`` has to say.
    """

    #: The element the run leaves.
    element: str
    #: Its own interface.
    interface: str
    #: The element at the far end.
    peer: str
    #: The port there.
    peer_port: str
    #: That port's PSE configuration, or ``None`` when it has none.
    poe: PoeConfig | None = None
    #: Panels crossed on the way, in order.
    through: tuple[str, ...] = ()

    @property
    def sources_power(self) -> bool:
        """Does the far end actually hand power down this run?"""
        return self.poe is not None and self.poe.enabled

    @property
    def deliverable_watts(self) -> float:
        """The most a device on this run may draw; zero when it sources none."""
        return self.poe.deliverable_watts if self.poe is not None and self.poe.enabled else 0.0

    def describe(self) -> str:
        """``sw-access-01:Gi1/0/7 (802.3at class 4, 30 W)``."""
        where = f"{self.peer}:{self.peer_port}"
        if self.poe is None:
            return f"{where} (no 'poe' block)"
        return f"{where} ({self.poe.describe()})"


@dataclass(frozen=True, slots=True)
class PoePort:
    """One PSE port of one element, and whether it holds budget (§17.3)."""

    element: str
    interface: str
    poe: PoeConfig
    #: The element this port feeds, when the walk found one.
    feeds: str = ""

    @property
    def counted(self) -> bool:
        """Does this port take budget out of the switch's pool?

        A ``poe`` block on a port with nothing on it is a *capability*, not an
        allocation: a 48-port PoE+ switch can source 30 W on every port and is
        sold with a 740 W supply, so counting all 48 would report every real
        switch as oversubscribed and the rule would be worthless. Two things do
        count — a port that feeds something, and a port whose ``budget_watts``
        was written down, which is the act of reserving it.
        """
        return self.poe.enabled and bool(self.feeds or self.poe.budget_watts is not None)

    @property
    def allocated_watts(self) -> float:
        return self.poe.allocation_watts if self.counted else 0.0


@dataclass(frozen=True, slots=True)
class PoeBudget:
    """One element's PoE pool and what is drawn from it (``pethMainPse``)."""

    element: str
    name: str
    #: ``power.poe_budget_watts``; ``None`` when it is not recorded, in which
    #: case ``NG-E013`` has nothing to compare against and says nothing.
    budget_watts: float | None
    ports: tuple[PoePort, ...] = ()

    @property
    def allocated_watts(self) -> float:
        return sum(port.allocated_watts for port in self.ports)

    @property
    def counted_ports(self) -> tuple[PoePort, ...]:
        return tuple(port for port in self.ports if port.counted)

    @property
    def free_watts(self) -> float | None:
        if self.budget_watts is None:
            return None
        return self.budget_watts - self.allocated_watts

    @property
    def is_oversubscribed(self) -> bool:
        return self.budget_watts is not None and self.allocated_watts > self.budget_watts

    @property
    def utilisation(self) -> float | None:
        """Fraction of the pool allocated, or ``None`` without a pool."""
        if not self.budget_watts:
            return None
        return self.allocated_watts / self.budget_watts


@dataclass(frozen=True, slots=True)
class PduLoad:
    """One PDU, its outlets and everything drawn through it (§17.4)."""

    pdu: str
    name: str
    #: Every outlet the unit declares, in declaration order.
    outlets: tuple[str, ...]
    capacity_watts: float | None
    input_feed: str
    #: The feeds landing on it, in the order the loads declared them.
    feeds: tuple[Feed, ...] = ()

    @property
    def outlet_count(self) -> int:
        return len(self.outlets)

    @property
    def used_outlets(self) -> int:
        """Outlets with a cord in them. Counted distinctly: two claims on one
        outlet is ``NG-E010``, and counting it twice would hide the real number.
        """
        return len({feed.outlet for feed in self.feeds})

    @property
    def free_outlets(self) -> int:
        return max(self.outlet_count - self.used_outlets, 0)

    @property
    def load_watts(self) -> float:
        """Normal-operation load: each feed's share of its element's draw."""
        return sum(feed.reserved_watts for feed in self.feeds)

    @property
    def failover_watts(self) -> float:
        """What this unit carries if every other feed of every load fails.

        Each element counted once at its whole draw, however many cords it has.
        This is the figure that says whether an A/B pair really is redundant.
        """
        seen: dict[str, float] = {}
        for feed in self.feeds:
            seen[feed.element] = feed.element_watts
        return sum(seen.values())

    @property
    def elements(self) -> tuple[str, ...]:
        """Everything fed from this unit, first-seen order, no repeats."""
        return tuple(dict.fromkeys(feed.element for feed in self.feeds))

    @property
    def free_watts(self) -> float | None:
        if self.capacity_watts is None:
            return None
        return self.capacity_watts - self.load_watts

    @property
    def is_oversubscribed(self) -> bool:
        return self.capacity_watts is not None and self.load_watts > self.capacity_watts

    @property
    def utilisation(self) -> float | None:
        """Fraction of the capacity drawn, or ``None`` without a capacity."""
        if not self.capacity_watts:
            return None
        return self.load_watts / self.capacity_watts

    def outlet_map(self) -> Mapping[str, tuple[Feed, ...]]:
        """Outlet number -> the feeds claiming it, in declaration order."""
        claims: dict[str, list[Feed]] = {}
        for feed in self.feeds:
            claims.setdefault(feed.outlet, []).append(feed)
        return {number: tuple(entries) for number, entries in claims.items()}


@dataclass(frozen=True, slots=True)
class PowerNode:
    """Everything one element contributes to the power view.

    One flat record rather than three, because a switch is a PDU's load, a PSE
    and a box with a draw all at once, and a renderer asking "what does this node
    say about power" wants one answer.
    """

    element: str
    name: str
    kind: str
    roles: tuple[PowerRole, ...]
    #: Typical and worst-case draw; zero when none is declared.
    draw_watts: float = 0.0
    maximum_watts: float = 0.0
    #: PDU only.
    capacity_watts: float | None = None
    load_watts: float = 0.0
    failover_watts: float = 0.0
    outlets: int = 0
    used_outlets: int = 0
    input_feed: str = ""
    #: PSE only.
    poe_budget_watts: float | None = None
    poe_allocated_watts: float = 0.0
    #: Load only.
    inputs: int = 0
    redundant: bool = False
    powered_by_poe: bool = False

    @property
    def is_pdu(self) -> bool:
        return PowerRole.PDU in self.roles

    @property
    def is_pse(self) -> bool:
        return PowerRole.PSE in self.roles

    @property
    def is_load(self) -> bool:
        return PowerRole.LOAD in self.roles

    @property
    def utilisation(self) -> float | None:
        """A PDU's capacity utilisation, else a PSE's budget utilisation."""
        if self.is_pdu and self.capacity_watts:
            return self.load_watts / self.capacity_watts
        if self.poe_budget_watts:
            return self.poe_allocated_watts / self.poe_budget_watts
        return None

    def describe(self) -> tuple[str, ...]:
        """The lines a node label carries in the ``power`` layer."""
        lines: list[str] = []
        if self.is_pdu:
            lines.append(f"{self.used_outlets}/{self.outlets} outlets")
            if self.capacity_watts is not None:
                lines.append(
                    f"{format_watts(self.load_watts)}/{format_watts(self.capacity_watts)} W "
                    f"({format_utilisation_percent(self.utilisation)})"
                )
            elif self.load_watts:
                lines.append(f"{format_watts(self.load_watts)} W drawn")
            if self.input_feed:
                lines.append(f"feed {self.input_feed}")
            return tuple(lines)
        if self.draw_watts:
            text = f"{format_watts(self.draw_watts)} W"
            if self.maximum_watts and self.maximum_watts != self.draw_watts:
                text += f" (max {format_watts(self.maximum_watts)} W)"
            lines.append(text)
        if self.powered_by_poe:
            lines.append("powered over PoE")
        elif self.redundant:
            lines.append(f"redundant, {self.inputs} feeds")
        if self.poe_budget_watts is not None:
            lines.append(
                f"PoE {format_watts(self.poe_allocated_watts)}/"
                f"{format_watts(self.poe_budget_watts)} W"
            )
        elif self.poe_allocated_watts:
            lines.append(f"PoE {format_watts(self.poe_allocated_watts)} W out")
        return tuple(lines)

    def rack_note(self) -> str:
        """The short annotation the rack elevation carries (§17.5).

        A PDU shows how full it is; everything else shows what it draws. Both in
        one cell, because an elevation has one column to spare and the reader is
        asking one question of it: can this rack take another box.
        """
        if self.is_pdu:
            if self.capacity_watts:
                return f"{format_utilisation_percent(self.utilisation)} of {format_watts(self.capacity_watts)} W"
            return f"{format_watts(self.load_watts)} W" if self.load_watts else ""
        if self.draw_watts:
            return f"{format_watts(self.draw_watts)} W"
        return ""


@dataclass(frozen=True, slots=True)
class PowerPlan:
    """Every power fact of one inventory, resolved once."""

    #: One per ``pdu`` document, in load order.
    pdus: tuple[PduLoad, ...] = ()
    #: One per element that sources PoE, in load order.
    pse: tuple[PoeBudget, ...] = ()
    #: Every resolved feed: outlet feeds in load order, then PoE feeds.
    feeds: tuple[Feed, ...] = ()
    #: Everything with something to say about power, keyed by fqn, load order.
    nodes: Mapping[str, PowerNode] = field(default_factory=dict)
    #: ``power.inputs`` entries that name no outlet that exists (``NG-E011``).
    unresolved: tuple[UnresolvedInput, ...] = ()
    #: Per ``powered_by: poe`` element: the far ends of its runs, in interface
    #: order. What ``NG-E014`` reads.
    uplinks: Mapping[str, tuple[Uplink, ...]] = field(default_factory=dict)

    def node(self, fqn: str) -> PowerNode | None:
        return self.nodes.get(fqn)

    def load_of(self, pdu: str) -> PduLoad | None:
        return next((load for load in self.pdus if load.pdu == pdu), None)

    def budget_of(self, element: str) -> PoeBudget | None:
        return next((budget for budget in self.pse if budget.element == element), None)

    def feeds_into(self, element: str) -> tuple[Feed, ...]:
        """Every feed that powers ``element``, in resolution order."""
        return tuple(feed for feed in self.feeds if feed.element == element)

    def outlet_claims(self) -> Mapping[tuple[str, str], tuple[Feed, ...]]:
        """``(pdu, outlet)`` -> the feeds claiming it. What ``NG-E010`` reads."""
        claims: dict[tuple[str, str], list[Feed]] = {}
        for feed in self.feeds:
            if feed.kind is FeedKind.OUTLET:
                claims.setdefault((feed.source, feed.outlet), []).append(feed)
        return {key: tuple(entries) for key, entries in claims.items()}

    @property
    def is_empty(self) -> bool:
        """Does the inventory say nothing about power at all?"""
        return not self.nodes

    @property
    def total_load_watts(self) -> float:
        return sum(load.load_watts for load in self.pdus)


def format_utilisation_percent(value: float | None) -> str:
    """``38.2%`` — a fraction as a percentage, or ``-`` for "not known".

    Deliberately the same shape as
    :func:`netviz.ipam.format_utilisation`, so the two utilisation tables read
    alike; it takes a fraction rather than a pair because a wattage is not a
    count of addresses and rounding it to integers first would lose the point.
    """
    if value is None:
        return "-"
    percent = value * 100
    if percent and percent < 0.05:
        return "<0.1%"
    return f"{percent:.1f}%"


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def power_plan(inventory: Inventory) -> PowerPlan:
    """Resolve every power fact of ``inventory`` (§17.4).

    Args:
        inventory: A tree loaded by :func:`~netviz.loader.load_tree`.

    Returns:
        The plan. A reference that does not resolve is *recorded*, in
        :attr:`PowerPlan.unresolved`, rather than raised or dropped: the
        validator grades it (``NG-E011``) and the renderer still has to draw the
        feeds that do resolve, because ``--force`` must produce a picture.
    """
    pdus = inventory.pdus
    outlet_feeds, unresolved = _outlet_feeds(inventory, pdus)
    uplinks = _poe_uplinks(inventory)
    poe_feeds = _poe_feeds(inventory, uplinks)
    feeds = (*outlet_feeds, *poe_feeds)

    loads = tuple(
        PduLoad(
            pdu=fqn,
            name=pdu.metadata.name,
            outlets=pdu.outlet_numbers,
            capacity_watts=pdu.capacity_watts,
            input_feed=pdu.input_feed,
            feeds=tuple(feed for feed in outlet_feeds if feed.source == fqn),
        )
        for fqn, pdu in pdus.items()
    )
    budgets = tuple(_poe_budgets(inventory, poe_feeds))
    nodes = _nodes(inventory, loads, budgets)
    return PowerPlan(
        pdus=loads,
        pse=budgets,
        feeds=feeds,
        nodes=nodes,
        unresolved=unresolved,
        uplinks=uplinks,
    )


def _power_of(element: object) -> PowerConfig | None:
    """``spec.power`` of a device, or ``None`` for anything that has no spec."""
    if isinstance(element, Device):
        return element.spec.power
    return None


def _outlet_feeds(
    inventory: Inventory, pdus: Mapping[str, Pdu]
) -> tuple[tuple[Feed, ...], tuple[UnresolvedInput, ...]]:
    """Resolve every ``power.inputs`` entry against the PDUs (§17.2)."""
    feeds: list[Feed] = []
    unresolved: list[UnresolvedInput] = []
    for fqn, element in inventory.elements.items():
        power = _power_of(element)
        if power is None or not power.inputs:
            continue
        namespace = namespace_of(fqn)
        # The share each cord carries in normal operation; see the module
        # docstring. Computed once per element so two cords of one server always
        # add up to exactly its draw, whatever the arithmetic rounds to.
        share = power.typical_watts / len(power.inputs)
        for index, entry in enumerate(power.inputs):
            resolution = inventory.lookup(entry.pdu, namespace=namespace)
            target = resolution.fqn
            if target is None:
                unresolved.append(
                    UnresolvedInput(
                        element=fqn,
                        index=index,
                        input=entry,
                        reason=(
                            UnresolvedReason.AMBIGUOUS_PDU
                            if resolution.ambiguous
                            else UnresolvedReason.UNKNOWN_PDU
                        ),
                        candidates=tuple(resolution.ambiguous),
                    )
                )
                continue
            pdu = pdus.get(target)
            if pdu is None:
                unresolved.append(
                    UnresolvedInput(
                        element=fqn,
                        index=index,
                        input=entry,
                        reason=UnresolvedReason.NOT_A_PDU,
                        pdu=target,
                    )
                )
                continue
            if not pdu.has_outlet(entry.outlet):
                unresolved.append(
                    UnresolvedInput(
                        element=fqn,
                        index=index,
                        input=entry,
                        reason=UnresolvedReason.UNKNOWN_OUTLET,
                        pdu=target,
                    )
                )
                continue
            feeds.append(
                Feed(
                    element=fqn,
                    kind=FeedKind.OUTLET,
                    source=target,
                    outlet=entry.outlet,
                    psu=entry.psu or "",
                    index=index,
                    reserved_watts=share,
                    element_watts=power.typical_watts,
                    input_feed=pdu.input_feed,
                )
            )
    return tuple(feeds), tuple(unresolved)


def _link_map(inventory: Inventory) -> Mapping[tuple[str, str], tuple[tuple[str, str], ...]]:
    """``(element, port)`` -> the far ends cabled to it, in cable load order.

    A port terminating two cables is ``E002``; both are kept here so the walk
    stays a function of the inventory rather than of which cable loaded first.
    """
    owners = inventory.cable_owners
    links: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for fqn, cable in inventory.cables.items():
        namespace = namespace_of(fqn)
        resolved = [
            inventory.resolve_fqn(ref.device, namespace=namespace) for ref in cable.endpoints
        ]
        if any(target is None or target not in owners for target in resolved):
            continue
        left, right = resolved
        assert left is not None and right is not None  # narrowed by the guard above
        near, far = cable.endpoints[0].interface, cable.endpoints[1].interface
        links.setdefault((left, near), []).append((right, far))
        links.setdefault((right, far), []).append((left, near))
    return {key: tuple(value) for key, value in links.items()}


def _walk(
    start: tuple[str, str],
    links: Mapping[tuple[str, str], tuple[tuple[str, str], ...]],
    panels: Mapping[str, PatchPanel],
) -> tuple[tuple[str, str] | None, tuple[str, ...]]:
    """Follow a run from ``start`` to the first active element on it (§15.2).

    A patch panel is transparent to power exactly as it is to frames, so a run
    that enters ``front/7`` continues from ``rear/7``. Returns the far end and
    the panels crossed, or ``(None, crossed)`` when the run stops inside the
    plant — a coupler with nothing patched into it, or a loop.
    """
    crossed: list[str] = []
    here = start
    for _ in range(MAX_PATCH_HOPS):
        far = links.get(here)
        if not far:
            return None, tuple(crossed)
        element, port = far[0]
        panel = panels.get(element)
        if panel is None:
            return (element, port), tuple(crossed)
        crossed.append(element)
        opposite = panel.opposite(port)
        if opposite is None:
            return None, tuple(crossed)
        here = (element, opposite)
    return None, tuple(crossed)


def _poe_uplinks(inventory: Inventory) -> Mapping[str, tuple[Uplink, ...]]:
    """The far end of every run leaving a ``powered_by: poe`` element (§17.3).

    Only for elements that claim PoE power: walking every run of every device
    would cost the same as building the graph, and nothing else needs the answer.
    """
    wanted = {
        fqn: element
        for fqn, element in inventory.devices.items()
        if (power := element.spec.power) is not None and power.is_poe_powered
    }
    if not wanted:
        return {}
    links = _link_map(inventory)
    panels = inventory.patchpanels
    uplinks: dict[str, tuple[Uplink, ...]] = {}
    for fqn, element in wanted.items():
        found: list[Uplink] = []
        for interface in element.interfaces:
            far, crossed = _walk((fqn, interface.name), links, panels)
            if far is None:
                continue
            peer, peer_port = far
            peer_element = inventory.elements.get(peer)
            port = (
                peer_element.interface(peer_port)
                if isinstance(peer_element, (Device, Adapter))
                else None
            )
            found.append(
                Uplink(
                    element=fqn,
                    interface=interface.name,
                    peer=peer,
                    peer_port=peer_port,
                    poe=port.poe if port is not None else None,
                    through=crossed,
                )
            )
        uplinks[fqn] = tuple(found)
    return uplinks


def _poe_feeds(inventory: Inventory, uplinks: Mapping[str, tuple[Uplink, ...]]) -> tuple[Feed, ...]:
    """One feed per PoE-powered element, over the best uplink it has.

    "Best" is the one that can deliver most: a device with two runs to two
    switches is fed by whichever actually sources power, and by the more capable
    of the two when both do. An element whose runs source no power gets no feed
    at all, which is what ``NG-E014`` reports.
    """
    feeds: list[Feed] = []
    for fqn, candidates in uplinks.items():
        sourcing = [uplink for uplink in candidates if uplink.sources_power]
        if not sourcing:
            continue
        best = max(sourcing, key=lambda uplink: (uplink.deliverable_watts, uplink.interface))
        power = _power_of(inventory.elements.get(fqn))
        draw = power.typical_watts if power is not None else 0.0
        assert best.poe is not None  # ``sources_power`` narrowed it
        feeds.append(
            Feed(
                element=fqn,
                kind=FeedKind.POE,
                source=best.peer,
                port=best.peer_port,
                peer_port=best.interface,
                through=best.through,
                reserved_watts=best.poe.allocation_watts,
                element_watts=draw,
            )
        )
    return tuple(feeds)


def _poe_budgets(inventory: Inventory, poe_feeds: Sequence[Feed]) -> Iterator[PoeBudget]:
    """One budget per element that has a PSE port or declares a pool (§17.3)."""
    fed: dict[tuple[str, str], str] = {(feed.source, feed.port): feed.element for feed in poe_feeds}
    for fqn, element in inventory.elements.items():
        power = _power_of(element)
        if not isinstance(element, Device):
            continue
        ports = tuple(
            PoePort(
                element=fqn,
                interface=interface.name,
                poe=interface.poe,
                feeds=fed.get((fqn, interface.name), ""),
            )
            for interface in element.interfaces
            if interface.poe is not None
        )
        budget = power.poe_budget_watts if power is not None else None
        if not ports and budget is None:
            continue
        yield PoeBudget(element=fqn, name=element.metadata.name, budget_watts=budget, ports=ports)


def _nodes(
    inventory: Inventory, loads: Sequence[PduLoad], budgets: Sequence[PoeBudget]
) -> Mapping[str, PowerNode]:
    """One :class:`PowerNode` per element with something to say about power."""
    by_pdu = {load.pdu: load for load in loads}
    by_pse = {budget.element: budget for budget in budgets}
    nodes: dict[str, PowerNode] = {}
    for fqn, element in inventory.elements.items():
        load = by_pdu.get(fqn)
        budget = by_pse.get(fqn)
        power = _power_of(element)
        says_something = power is not None and not power.is_empty
        if load is None and budget is None and not says_something:
            continue
        roles: list[PowerRole] = []
        if load is not None:
            roles.append(PowerRole.PDU)
        if budget is not None:
            roles.append(PowerRole.PSE)
        if says_something and power is not None and not _only_pse(power):
            roles.append(PowerRole.LOAD)
        nodes[fqn] = PowerNode(
            element=fqn,
            name=element.metadata.name,
            kind=element.kind,
            roles=tuple(roles),
            draw_watts=power.typical_watts if power is not None else 0.0,
            maximum_watts=power.worst_case_watts if power is not None else 0.0,
            capacity_watts=load.capacity_watts if load is not None else None,
            load_watts=load.load_watts if load is not None else 0.0,
            failover_watts=load.failover_watts if load is not None else 0.0,
            outlets=load.outlet_count if load is not None else 0,
            used_outlets=load.used_outlets if load is not None else 0,
            input_feed=load.input_feed if load is not None else "",
            poe_budget_watts=budget.budget_watts if budget is not None else None,
            poe_allocated_watts=budget.allocated_watts if budget is not None else 0.0,
            inputs=len(power.inputs) if power is not None else 0,
            redundant=power.redundant if power is not None else False,
            powered_by_poe=power.is_poe_powered if power is not None else False,
        )
    return nodes


def _only_pse(power: PowerConfig) -> bool:
    """Does the block say nothing but "this box hands power out"?

    A switch that declares a PoE pool and nothing else is not a load anybody can
    schedule — there is no draw and no cord — so it is drawn as a source alone.
    """
    return (
        power.poe_budget_watts is not None
        and power.draw_watts is None
        and not power.inputs
        and not power.is_poe_powered
    )


def describe_element(fqn: str) -> str:
    """The short name of an element, for a label with no room for a namespace."""
    return short_name(fqn)
