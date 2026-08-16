"""Rendering a convergence plan: for a person, for a program, for a pull request.

Three shapes, one document behind all of them, so they cannot disagree and all
three are byte-identical between two runs over unchanged inputs.

``text``
    What an operator reads before deciding. Grouped by device, ordered by the
    dependency rank, every command shown, every disruptive change flagged in the
    margin, the maintenance batches at the end.
``json``
    What a script reads, and what a transport would consume: the whole
    :class:`~netviz.converge.model.ConvergePlan`, provenance and prerequisites
    included, nothing summarised away.
``markdown``
    What goes in a change ticket or a pull-request comment. The same content as
    ``text`` with tables where a table reads better than a list, because the
    audience is the person approving the window rather than the one running it.

House style, shared with the rest of netviz: nothing is printed twice, a
section with nothing in it is left out rather than printed empty, and every
count says what it counted.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Final

from netviz.converge.model import (
    Action,
    Batch,
    ConvergeChange,
    ConvergePlan,
    DeviceConverge,
    Risk,
)
from netviz.diagnostics import dump_json
from netviz.fsio import display_path
from netviz.loader.inventory import short_name

__all__ = ["REPORT_FORMATS", "render_converge", "to_json", "to_markdown", "to_text"]

#: Every value ``--format`` accepts.
REPORT_FORMATS: Final[tuple[str, ...]] = ("text", "json", "markdown")

#: How many provenance lines one change prints before it stops naming them. The
#: whole list is in the JSON; a declarative dialect's single file can close a
#: dozen findings and printing all of them would bury the command.
MAX_PROVENANCE: Final = 3


def render_converge(plan: ConvergePlan, output_format: str) -> str:
    """Serialise ``plan`` in one of :data:`REPORT_FORMATS`.

    Raises:
        ValueError: ``output_format`` is not one of them.
    """
    if output_format == "text":
        return to_text(plan)
    if output_format == "json":
        return to_json(plan)
    if output_format == "markdown":
        return to_markdown(plan)
    raise ValueError(f"not a converge report format: {output_format!r}")


def to_json(plan: ConvergePlan) -> str:
    """The whole plan as one JSON document."""
    return dump_json(plan.as_record())


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


def to_text(plan: ConvergePlan) -> str:
    return "\n".join(_text_lines(plan)) + "\n"


def _text_lines(plan: ConvergePlan) -> Iterator[str]:
    yield _headline(plan)
    yield ""
    if plan.converged:
        yield "Nothing to do: the network already matches the inventory."
        return

    for device in plan.devices:
        yield from _device_text(plan, device)
    if plan.batches:
        yield "maintenance batches"
        for batch in plan.batches:
            yield from _batch_text(batch)
        yield ""
    for note in plan.notes:
        yield f"note: {note}"
    if plan.notes:
        yield ""
    yield _summary(plan)


def _headline(plan: ConvergePlan) -> str:
    inputs = f"{len(plan.inputs)} input(s)" if plan.inputs else "no input"
    dialects = f" ({', '.join(plan.capture_dialects)})" if plan.capture_dialects else ""
    return (
        f"converge plan for {display_path(plan.root)} from {inputs}{dialects}, "
        f"written as {plan.dialect} commands"
    )


def _device_text(plan: ConvergePlan, device: DeviceConverge) -> Iterator[str]:
    kind = f" ({device.kind})" if device.kind else ""
    batch = f" [batch {device.batch}]" if device.batch is not None else ""
    yield f"{device.element}{kind}{batch}"
    for change in device.changes:
        yield from _change_text(change)
    yield ""


def _change_text(change: ConvergeChange) -> Iterator[str]:
    flag = "!! " if change.risk is Risk.DISRUPTIVE else "   "
    yield f"  {change.symbol} {flag}{change.summary}"
    for entry in change.provenance[:MAX_PROVENANCE]:
        yield f"        from {entry.location}: {entry.message}"
    if len(change.provenance) > MAX_PROVENANCE:
        yield f"        ... and {len(change.provenance) - MAX_PROVENANCE} more finding(s)"
    if change.prerequisites:
        # The key, not the short name: a prerequisite id is ``element#kind/target``
        # and its last segment alone is a bare ``20``, which reads as nothing.
        after = ", ".join(entry.partition("#")[2] or entry for entry in change.prerequisites)
        yield f"        after {after}"
    if change.risk is Risk.DISRUPTIVE and change.risk_reason:
        yield f"        disruptive: {change.risk_reason}"
    if change.note and change.note != change.summary:
        yield f"        note: {change.note}"
    for command in change.commands:
        if command.kind == "write":
            # Not '$ ...': this step is a file, and prefixing it with a prompt
            # would read as a command a reader could paste. The bytes are in the
            # script and in the JSON; what belongs here is the size, which is
            # what somebody scanning a plan wants to know about a file.
            lines = (command.content or "").count("\n")
            yield f"        > {command.path} ({lines} line(s))"
            continue
        yield f"        $ {command.text}"


def _batch_text(batch: Batch) -> Iterator[str]:
    yield f"  batch {batch.index}: {', '.join(short_name(name) for name in batch.elements)}"
    if batch.isolated:
        listed = ", ".join(short_name(name) for name in batch.isolated[:MAX_LISTED])
        more = "" if len(batch.isolated) <= MAX_LISTED else f", +{len(batch.isolated) - MAX_LISTED}"
        yield f"      isolates {len(batch.isolated)} element(s): {listed}{more}"
    for split in batch.splits:
        yield f"      splits {split}"
    if batch.note:
        yield f"      {batch.note}"
    if not batch.disruptive:
        yield "      nothing loses reachability while this batch is worked"


#: How many isolated elements a batch names before it stops. The whole list is
#: in the JSON.
MAX_LISTED: Final = 8


def _summary(plan: ConvergePlan) -> str:
    counts = plan.counts
    parts = [
        f"{counts[Action.CREATE]} create",
        f"{counts[Action.UPDATE]} update",
        f"{counts[Action.DELETE]} delete",
    ]
    tail = ""
    if counts[Action.MANUAL]:
        tail = f", {counts[Action.MANUAL]} needing hands"
    disruptive = len(plan.disruptive)
    risk = f", {disruptive} disruptive" if disruptive else ""
    return (
        f"{len(plan.changes)} change(s) across {len(plan.devices)} element(s) "
        f"({', '.join(parts)}{tail}){risk}; "
        f"{len(plan.batches)} maintenance batch(es)"
    )


# --------------------------------------------------------------------------- #
# markdown
# --------------------------------------------------------------------------- #


def to_markdown(plan: ConvergePlan) -> str:
    return "\n".join(_markdown_lines(plan)) + "\n"


def _markdown_lines(plan: ConvergePlan) -> Iterator[str]:
    yield "# Convergence plan"
    yield ""
    yield f"* **Inventory**: `{display_path(plan.root)}`"
    yield f"* **Capture**: {_inputs_phrase(plan)}"
    yield f"* **Dialect**: `{plan.dialect}`"
    yield f"* **Disruptive changes allowed**: {'yes' if plan.allow_disruptive else 'no'}"
    yield ""
    if plan.converged:
        yield "Nothing to do: the network already matches the inventory."
        return
    yield _summary(plan)
    yield ""

    for device in plan.devices:
        yield from _device_markdown(device)
    if plan.batches:
        yield "## Maintenance batches"
        yield ""
        yield "| Batch | Elements | Isolates | Splits |"
        yield "|---|---|---|---|"
        for batch in plan.batches:
            yield (
                f"| {batch.index} "
                f"| {_cell(short_name(name) for name in batch.elements)} "
                f"| {_cell(short_name(name) for name in batch.isolated) or '—'} "
                f"| {_cell(batch.splits) or '—'} |"
            )
        yield ""
    if plan.notes:
        yield "## Notes"
        yield ""
        for note in plan.notes:
            yield f"* {note}"
        yield ""


def _device_markdown(device: DeviceConverge) -> Iterator[str]:
    kind = f" ({device.kind})" if device.kind else ""
    batch = f" — batch {device.batch}" if device.batch is not None else ""
    yield f"## `{device.element}`{kind}{batch}"
    yield ""
    yield "| # | Change | Risk | From |"
    yield "|---|---|---|---|"
    for index, change in enumerate(device.changes, start=1):
        risk = "**disruptive**" if change.risk is Risk.DISRUPTIVE else "safe"
        origin = _cell(entry.location for entry in change.provenance[:MAX_PROVENANCE])
        yield f"| {index} | {change.symbol} {_escape(change.summary)} | {risk} | {origin or '—'} |"
    yield ""
    commands = [command for change in device.changes for command in change.commands]
    if commands:
        yield "```sh"
        for command in commands:
            yield from command.script_lines()
        yield "```"
        yield ""
    manual = device.manual
    if manual:
        yield "Needs hands:"
        yield ""
        for change in manual:
            yield f"* {_escape(change.note or change.summary)}"
        yield ""


def _inputs_phrase(plan: ConvergePlan) -> str:
    if not plan.inputs:
        return "no input"
    dialects = f" read as {', '.join(plan.capture_dialects)}" if plan.capture_dialects else ""
    return f"{', '.join(f'`{name}`' for name in plan.inputs)}{dialects}"


def _cell(values: Iterable[str]) -> str:
    """A table cell from an iterable, escaped, or ``''`` when there is nothing."""
    return "<br>".join(_escape(value) for value in values)


def _escape(text: str) -> str:
    """``text`` with the two characters that would break a table cell removed.

    A pipe ends a cell and a newline ends a row; a device description or a drift
    message may legitimately hold either, and a table that silently gained a
    column would be worse than one with an escaped pipe in it.
    """
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
