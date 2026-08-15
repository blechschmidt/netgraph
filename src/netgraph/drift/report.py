"""Rendering a :class:`~netgraph.drift.model.DriftReport` for a human or a machine.

Three formats, one for each place the answer is read:

``text``
    A diff, grouped by element. ``+`` is what the network has and the inventory
    does not, ``-`` what the inventory declares and the network lacks, ``~``
    what both have and spell differently. The blind spots follow in their own
    section, after a blank line and under their own heading, so that nobody
    reading quickly can mistake one for a difference.
``json``
    A stable envelope, documented in ``docs/commands/drift.md``: the run, the
    counts, one record per difference and one per blind spot.
``junit``
    One test case per element — not per difference — because that is the unit a
    reader of a CI test report navigates by, and because it makes the report a
    stable list whose rows go red and green rather than one that grows and
    shrinks. An element with nothing but blind spots is *skipped*, which is the
    word JUnit already has for "not run".

The document skeleton for the latter two lives in :mod:`netgraph.diagnostics`, so
``drift`` and ``validate`` cannot disagree about JSON indentation or XML
escaping.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

from netgraph.console import Console
from netgraph.diagnostics import JUnitCase, as_junit, dump_json
from netgraph.drift.model import (
    DIRECTION_SYMBOLS,
    Change,
    Direction,
    DriftReport,
    ElementDrift,
    Unobserved,
)
from netgraph.fsio import display_path

__all__ = ["FORMATS", "as_json", "as_junit_report", "render_drift", "write_text"]

#: Every value ``drift --output-format`` accepts, ``text`` first.
FORMATS: Final[tuple[str, ...]] = ("text", "json", "junit")

#: Version of the JSON envelope. Bumped only for a change a consumer could trip
#: over — a new optional key does not count, a renamed one does.
JSON_SCHEMA_VERSION: Final = 1

#: Colour per direction in the text report, matching the diff-like symbols.
_COLOURS: Final[dict[Direction, str]] = {
    Direction.UNDECLARED: "green",
    Direction.MISSING: "red",
    Direction.DISAGREES: "yellow",
}

#: How wide the location column is before the message is pushed onto its own
#: line. Wide enough for ``GigabitEthernet1/0/24.vlan.trunk_vlans`` and no wider.
_LOCATION_WIDTH: Final = 34


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


def write_text(console: Console, report: DriftReport) -> None:
    """Print ``report`` as a grouped diff, blind spots in their own section."""
    console.print(_header(report))
    console.print()

    groups = [group for group in report.elements if group.drifted]
    for group in groups:
        _write_group(console, group)
        console.print()

    if report.unobserved:
        console.print(console.bold(f"unobserved ({len(report.unobserved)})"))
        console.print(
            console.dim("  declared, but outside what these dialects see; never counted as drift")
        )
        for blind in report.unobserved:
            console.print(f"  {_blind_label(blind)}: {blind.reason}")
        console.print()

    console.print(_summary(report))


def _header(report: DriftReport) -> str:
    dialects = ", ".join(report.dialects) or "no dialect"
    inputs = _plural(len(report.inputs), "input")
    return f"drift of {_root_text(report.root)} against {inputs} ({dialects})"


def _root_text(root: Path) -> str:
    """The inventory root as the reader typed it, where that is possible.

    ``-i`` resolves to an absolute path, and an absolute path in the first line
    of a report is noise: it is the same every time and it is the one part of
    the output that differs between two machines looking at the same tree. The
    JSON envelope keeps the absolute form, because a consumer of that is not
    reading, it is resolving.

    :func:`~netgraph.fsio.display_path` is where that rule and the forward
    slashes it prints live; this is a name for what one means here.
    """
    return display_path(root)


def _write_group(console: Console, group: ElementDrift) -> None:
    kind = f" ({group.kind})" if group.kind else ""
    console.print(console.bold(f"{group.element}{kind}"))
    for change in group.changes:
        symbol = console.style(DIRECTION_SYMBOLS[change.direction], fg=_COLOURS[change.direction])
        console.print(f"  {symbol} {_where(change):<{_LOCATION_WIDTH}} {change.message}")


def _blind_label(blind: Unobserved) -> str:
    """``hosts/pc-desk:eno1.ipv4 192.168.10.20/24`` — where, and what exactly.

    The items are dropped when they only repeat the path, which is the case for
    a whole interface nobody could see: ``pc-desk:lo lo`` says nothing twice.
    """
    label = blind.location
    if blind.items and blind.items != (blind.path,):
        label += " " + ", ".join(blind.items)
    return label


def _where(change: Change) -> str:
    """The path inside the element, or the direction's own word for the element."""
    tail = ".".join(part for part in (change.path, change.field) if part)
    return tail or change.scope


def _summary(report: DriftReport) -> str:
    if not report.changes:
        return (
            f"no drift: {_plural(len(report.compared), 'element')} compared, "
            f"{len(report.unobserved)} unobserved"
        )
    counts = report.counts
    parts = ", ".join(
        f"{DIRECTION_SYMBOLS[direction]}{counts[direction]} {direction}"
        for direction in Direction
        if counts[direction]
    )
    return (
        f"{_plural(len(report.changes), 'difference')} across "
        f"{_plural(sum(1 for group in report.elements if group.drifted), 'element')} "
        f"({parts}); {len(report.unobserved)} unobserved"
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


# --------------------------------------------------------------------------- #
# json
# --------------------------------------------------------------------------- #


def as_json(report: DriftReport) -> dict[str, Any]:
    """The documented JSON envelope."""
    counts = report.counts
    return {
        "schemaVersion": JSON_SCHEMA_VERSION,
        "tool": {"name": "netgraph", "version": report.version},
        "inventory": {"root": str(report.root)},
        "capture": {
            "inputs": list(report.inputs),
            "dialects": list(report.dialects),
            "devices": list(report.observed),
        },
        "summary": {
            **{str(direction): counts[direction] for direction in Direction},
            "total": len(report.changes),
            "unobserved": len(report.unobserved),
            "compared": len(report.compared),
            "filtered": len(report.filtered),
        },
        "drifted": report.drifted,
        "compared": list(report.compared),
        "drift": [change.as_record() for change in report.changes],
        "unobserved": [blind.as_record() for blind in report.unobserved],
    }


# --------------------------------------------------------------------------- #
# junit
# --------------------------------------------------------------------------- #


def as_junit_report(report: DriftReport) -> str:
    """The report as a JUnit XML document with one case per element."""
    return as_junit(
        "netgraph drift",
        list(_cases(report)),
        properties={
            "inventory": str(report.root),
            "dialects": ",".join(report.dialects),
            "inputs": ",".join(report.inputs),
            "netgraph": report.version,
        },
        failure_type="drift",
    )


def _cases(report: DriftReport) -> Iterator[JUnitCase]:
    """One case per element, in element order rather than drift-first order.

    A test report is read as a list that should stay put between runs, so the
    ordering that puts the interesting rows on top — right for a terminal — is
    exactly wrong here. An element that was compared and agreed carries no
    change and no blind spot, so it is not in
    :attr:`~netgraph.drift.model.DriftReport.elements` at all; it still needs a
    green row, or a report of six devices would show only the broken ones and
    look like a report of one.
    """
    groups = {group.element: group for group in report.elements}
    passing = [element for element in report.compared if element not in groups]
    for name in sorted({*groups, *passing}):
        group = groups.get(name)
        if group is None:
            yield JUnitCase(classname="netgraph.drift.element", name=name)
            continue
        classname = f"netgraph.drift.{group.kind or 'element'}"
        if group.changes:
            detail = "\n".join(
                f"{DIRECTION_SYMBOLS[change.direction]} {change.location}: {change.message}"
                for change in group.changes
            )
            yield JUnitCase(
                classname=classname,
                name=name,
                failure=f"{_plural(len(group.changes), 'difference')} from the captured network",
                detail=detail,
            )
            continue
        yield JUnitCase(
            classname=classname,
            name=name,
            skipped="; ".join(sorted({blind.reason for blind in group.unobserved})),
        )


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def render_drift(report: DriftReport, output_format: str) -> str:
    """Serialise ``report`` in one of :data:`FORMATS` other than ``text``.

    Raises:
        ValueError: ``output_format`` is not a structured format.
    """
    if output_format == "json":
        return dump_json(as_json(report))
    if output_format == "junit":
        return as_junit_report(report)
    raise ValueError(f"not a structured drift format: {output_format!r}")
