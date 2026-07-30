"""The load schedule: one row per power feed, with both ends located (§17.7).

This is the electrical counterpart of the pull list. An installer carries the
pull list; the person signing off a rack carries this — *which outlet, on which
strip, on which feed, powering which box in which rack unit, drawing how many
watts* — and neither a diagram nor a topology export answers it, because a power
path is not a data path.

One row per **feed**, not per device: a dual-corded server is two rows, which is
the whole point. A PoE-powered camera is a row too, and its ``FEED_KIND`` says
``poe`` so that a reader summing outlet loads does not double-count something
that occupies no outlet.

Two shapes, one set of rows
---------------------------

``--schedule-format csv`` is the sheet somebody prints and initials. ``json`` is
the same rows plus the per-PDU and per-PSE totals, which a capacity tool wants
and a spreadsheet computes for itself. Neither is the lossy one: the CSV holds
every fact about every feed, and the JSON adds only sums derived from them.

What it drops
-------------

Everything that is not power. Cables, addressing and VLANs have their own
exports; a load schedule that also carried them would be a second copy of the
inventory.

A feed whose ``pdu:outlet`` did not resolve is *not* a row — there is no outlet
to schedule — and is recorded in the manifest instead. So is a device that
declares a draw and no power path (``W137``), because a load nobody can find the
socket for is exactly what a schedule is supposed to surface. Only ``--force``
gets that far: the validator refuses both first.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from netgraph.export.context import ExportContext
from netgraph.export.manifest import Reason, Recorder
from netgraph.export.names import csv_cell
from netgraph.loader.inventory import short_name
from netgraph.models import format_watts
from netgraph.power import Feed, PduLoad, PoeBudget, PowerPlan, power_plan
from netgraph.render.graph import Graph, Layer, Node

__all__ = ["COLUMNS", "SCHEDULE_FORMATS", "emit"]

#: How the schedule is laid out. ``csv`` is the sheet; ``json`` adds the totals.
SCHEDULE_FORMATS: Final[tuple[str, ...]] = ("csv", "json")

#: The columns, in the one order the CSV uses. ``SOURCE`` and ``LOAD`` are the
#: two ends of the feed, always in that direction: power flows one way, unlike a
#: cable, so there is nothing to canonicalise.
COLUMNS: Final[tuple[str, ...]] = (
    "FEED_KIND",
    "SOURCE",
    "SOURCE_KIND",
    "OUTLET",
    "SOURCE_PORT",
    "INPUT_FEED",
    "SOURCE_SITE",
    "SOURCE_ROOM",
    "SOURCE_RACK",
    "SOURCE_UNIT",
    "LOAD",
    "LOAD_KIND",
    "PSU",
    "LOAD_PORT",
    "LOAD_SITE",
    "LOAD_ROOM",
    "LOAD_RACK",
    "LOAD_UNIT",
    "RESERVED_W",
    "ELEMENT_W",
    "REDUNDANT",
    "VIA",
)


@dataclass(frozen=True, slots=True)
class _Place:
    """Where one end of a feed is, as far as the inventory places it."""

    site: str = ""
    room: str = ""
    rack: str = ""
    unit: str = ""

    @property
    def sort_key(self) -> tuple[int, str, str, str, int]:
        """Rack order, with everything unplaced after everything placed."""
        return (
            0 if self.rack else 1,
            self.site,
            self.room,
            self.rack,
            -int(self.unit) if self.unit else 0,
        )


@dataclass(frozen=True, slots=True)
class _Row:
    """One feed, flattened to the sheet."""

    feed: Feed
    source_kind: str
    source_place: _Place
    load_kind: str
    load_place: _Place
    redundant: bool

    def cells(self) -> tuple[str, ...]:
        feed = self.feed
        return (
            str(feed.kind),
            feed.source,
            self.source_kind,
            feed.outlet,
            feed.port,
            feed.input_feed,
            self.source_place.site,
            self.source_place.room,
            self.source_place.rack,
            self.source_place.unit,
            feed.element,
            self.load_kind,
            feed.psu,
            feed.peer_port,
            self.load_place.site,
            self.load_place.room,
            self.load_place.rack,
            self.load_place.unit,
            format_watts(feed.reserved_watts) if feed.reserved_watts else "",
            format_watts(feed.element_watts) if feed.element_watts else "",
            "yes" if self.redundant else "",
            " ".join(short_name(panel) for panel in feed.through),
        )

    @property
    def sort_key(self) -> tuple[Any, ...]:
        """Down the racks, then by source, then by outlet.

        Outlets sort *numerically* where they are numbers, so ``10`` follows
        ``9`` rather than ``1`` — a sheet an electrician reads off a strip has to
        be in the order the strip is labelled.
        """
        outlet = self.feed.outlet
        return (
            self.source_place.sort_key,
            self.feed.source,
            0 if outlet.isdigit() else 1,
            int(outlet) if outlet.isdigit() else 0,
            outlet,
            self.feed.port,
            self.feed.element,
        )

    def record(self) -> dict[str, Any]:
        """The same row as JSON, omitting what the inventory did not say."""
        payload: dict[str, Any] = {
            "feedKind": str(self.feed.kind),
            "source": self.feed.source,
            "sourceKind": self.source_kind,
            "load": self.feed.element,
            "loadKind": self.load_kind,
        }
        if self.feed.outlet:
            payload["outlet"] = self.feed.outlet
        if self.feed.port:
            payload["sourcePort"] = self.feed.port
        if self.feed.input_feed:
            payload["inputFeed"] = self.feed.input_feed
        if self.feed.psu:
            payload["psu"] = self.feed.psu
        if self.feed.peer_port:
            payload["loadPort"] = self.feed.peer_port
        if self.feed.through:
            payload["via"] = list(self.feed.through)
        if self.feed.reserved_watts:
            payload["reservedWatts"] = round(self.feed.reserved_watts, 3)
        if self.feed.element_watts:
            payload["elementWatts"] = self.feed.element_watts
        if self.redundant:
            payload["redundant"] = True
        for prefix, place in (("source", self.source_place), ("load", self.load_place)):
            located = _place_record(place)
            if located:
                payload[f"{prefix}Location"] = located
        return payload


def _place_record(place: _Place) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in (("site", place.site), ("room", place.room), ("rack", place.rack)):
        if value:
            payload[key] = value
    if place.unit:
        payload["unit"] = int(place.unit)
    return payload


def emit(context: ExportContext) -> str:
    """Render the load schedule as CSV or as JSON."""
    graph = context.at(Layer.POWER)
    plan = power_plan(context.inventory)
    recorder = context.recorder
    recorder.considered = len(plan.feeds)

    places = {fqn: _place(node) for fqn, node in graph.nodes.items()}
    kinds = {fqn: node.kind for fqn, node in graph.nodes.items()}
    redundant = {
        fqn: node.power.redundant for fqn, node in graph.nodes.items() if node.power is not None
    }

    rows = sorted(
        (
            _Row(
                feed=feed,
                source_kind=kinds.get(feed.source, ""),
                source_place=places.get(feed.source, _Place()),
                load_kind=kinds.get(feed.element, ""),
                load_place=places.get(feed.element, _Place()),
                redundant=redundant.get(feed.element, False),
            )
            for feed in plan.feeds
            if feed.source in kinds and feed.element in kinds
        ),
        key=lambda row: row.sort_key,
    )
    recorder.emitted = len(rows)
    _record_gaps(plan, graph, {feed.id for feed in (row.feed for row in rows)}, recorder)

    if context.options.schedule_format == "json":
        return _json(rows, plan)
    return _csv(rows)


def _place(node: Node) -> _Place:
    """Where a node is, from ``metadata.location`` (§3.2)."""
    element = node.element
    location = element.metadata.location if element is not None else None
    if location is None:
        return _Place()
    return _Place(
        site=location.site or "",
        room=location.room or "",
        rack=location.rack or "",
        unit=str(location.position) if location.position is not None else "",
    )


def _record_gaps(
    plan: PowerPlan, graph: Graph, emitted: frozenset[str] | set[str], recorder: Recorder
) -> None:
    """Everything with a power fact that did not reach the sheet, and why.

    Three shapes, all of them a reader's question rather than a tool's excuse: a
    reference that names no outlet, a load with no socket at all, and a feed
    whose two ends are not both in the graph — which is what a filter does to
    half of a feed.
    """
    for entry in plan.unresolved:
        recorder.skip(
            entry.element,
            Reason.UNRESOLVED,
            f"input {entry.index + 1} names {entry.input}, which is {entry.reason}",
        )
    for feed in plan.feeds:
        if feed.id in emitted:
            continue
        recorder.skip(
            feed.element,
            Reason.HALF_SELECTED,
            f"the feed from {feed.source_label} has one end outside the selection",
        )
    for fqn, node in plan.nodes.items():
        if not node.is_load or not node.draw_watts or node.powered_by_poe:
            continue
        if any(feed.element == fqn for feed in plan.feeds):
            continue
        recorder.skip(
            fqn,
            Reason.NO_ADDRESS if fqn not in graph.nodes else Reason.NOT_REPRESENTABLE,
            f"draws {format_watts(node.draw_watts)} W but declares no power path (W137)",
        )


def _csv(rows: Sequence[_Row]) -> str:
    """RFC 4180 CSV, with ``\\n`` line endings.

    Every cell goes through :func:`~netgraph.export.names.csv_cell` on the way
    out, for the reason the pull list does it: this artefact is *certain* to be
    opened in a spreadsheet, and a PSU labelled ``=HYPERLINK(...)`` becoming a
    live link is a real outcome rather than a theoretical one.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(tuple(csv_cell(cell) for cell in row.cells()) for row in rows)
    return buffer.getvalue()


def _json(rows: Sequence[_Row], plan: PowerPlan) -> str:
    """The same rows, plus the totals a capacity tool would otherwise re-derive."""
    payload = {
        "feeds": [row.record() for row in rows],
        "pdus": [_pdu_record(load) for load in plan.pdus],
        "pse": [_pse_record(budget) for budget in plan.pse],
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _pdu_record(load: PduLoad) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "pdu": load.pdu,
        "name": load.name,
        "outlets": load.outlet_count,
        "usedOutlets": load.used_outlets,
        "freeOutlets": load.free_outlets,
        "loadWatts": round(load.load_watts, 3),
        "failoverWatts": round(load.failover_watts, 3),
        "loads": list(load.elements),
    }
    if load.input_feed:
        payload["inputFeed"] = load.input_feed
    if load.capacity_watts is not None:
        payload["capacityWatts"] = load.capacity_watts
        payload["freeWatts"] = round(load.capacity_watts - load.load_watts, 3)
    if load.utilisation is not None:
        payload["utilisation"] = round(load.utilisation, 6)
    return payload


def _pse_record(budget: PoeBudget) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "element": budget.element,
        "name": budget.name,
        "allocatedWatts": round(budget.allocated_watts, 3),
        "ports": [
            {
                "interface": port.interface,
                "standard": str(port.poe.standard),
                "allocatedWatts": round(port.allocated_watts, 3),
                "counted": port.counted,
                **({"class": port.poe.pse_class} if port.poe.pse_class is not None else {}),
                **({"feeds": port.feeds} if port.feeds else {}),
            }
            for port in budget.ports
        ],
    }
    if budget.budget_watts is not None:
        payload["budgetWatts"] = budget.budget_watts
        payload["freeWatts"] = round(budget.budget_watts - budget.allocated_watts, 3)
    if budget.utilisation is not None:
        payload["utilisation"] = round(budget.utilisation, 6)
    return payload
