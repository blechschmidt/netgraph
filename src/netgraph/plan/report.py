"""Printing a plan: the terraform-shaped summary, and the field detail under it.

Two audiences, one document. The **summary line** is for the person who runs
``netgraph plan`` a dozen times a day and wants one number:

    Plan: + 3 to add, ~ 5 to change, - 1 to destroy.

The **per-element detail** is for the review that has to happen before
``netgraph apply``, and is written on the assumption that the reader knows the
inventory and not the diff — so it names the element, marks the action, and then
lists exactly the fields that move, old value to new:

    ~ device.core/sw-core-01
        spec.interfaces[name=Gi1/0/1].mtu: 1500 -> 9000
      + spec.interfaces[name=Gi1/0/9]: {name: Gi1/0/9, type: ethernet}

Values are rendered as compact YAML-ish flow, clipped, because a plan is read in
a terminal and a 4 KiB description pasted into it helps nobody. The unclipped
value is always in ``--json``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from netgraph.console import Console
from netgraph.diagnostics import dump_json
from netgraph.plan.model import ACTION_SIGILS, Action, Change, FieldChange, Plan
from netgraph.plan.paths import MISSING

__all__ = ["PLAN_FORMATS", "render_plan", "summary_line", "write_plan"]

#: What ``-F`` accepts.
PLAN_FORMATS: Final[tuple[str, ...]] = ("text", "json")

#: How much of one value a line quotes before it is clipped.
_MAX_VALUE: Final = 72

#: The phrase each action contributes to the summary line.
_PHRASES: Final[dict[Action, str]] = {
    Action.CREATE: "to add",
    Action.UPDATE: "to change",
    Action.DELETE: "to destroy",
    Action.RENAME: "to rename",
}


def summary_line(plan: Plan) -> str:
    """``+ 3 to add, ~ 5 to change, - 1 to destroy`` — or that there is nothing."""
    counts = plan.counts()
    parts = [
        f"{ACTION_SIGILS[action]} {counts[action]} {_PHRASES[action]}"
        for action in Action
        if counts[action]
    ]
    return ", ".join(parts) if parts else "no changes"


def write_plan(console: Console, plan: Plan, *, verbose: bool = False) -> None:
    """Print the whole plan: header, one block per change, summary last.

    Every line goes to the console's data stream. In text mode the report *is*
    the command's output, header and all, so none of it is commentary that
    ``--quiet`` should be able to drop; the structured formats move the one-line
    summary to stderr instead, and that is where the split belongs.
    """
    console.print(f"netgraph plan: {plan.source.description} → {plan.target.description}")
    console.print()
    if plan.empty:
        console.print("No changes. The two states describe the same network.")
        return
    for change in plan:
        for line in _block(console, change, verbose=verbose):
            console.print(line)
    console.print()
    console.print(f"Plan: {summary_line(plan)}.")


def render_plan(plan: Plan, output_format: str) -> str:
    """The plan as a machine-readable document.

    Raises:
        ValueError: ``output_format`` is not one of :data:`PLAN_FORMATS`.
    """
    if output_format != "json":
        raise ValueError(f"unknown plan format {output_format!r}")
    return dump_json(plan.to_dict())


def _block(console: Console, change: Change, *, verbose: bool) -> Iterator[str]:
    marker = console.style(f"{change.sigil} {change.headline}", fg=change.colour, bold=True)
    yield f"  {marker}  [{change.kind}]"
    if change.source is not None and verbose:
        yield f"      declared in {change.source}"
    if change.action is Action.UPDATE:
        yield from (f"      {line}" for line in _fields(change.fields))
    elif change.action in (Action.CREATE, Action.DELETE) and change.document is not None:
        yield from (f"      {line}" for line in _document(change.document, verbose=verbose))


def _fields(fields: Sequence[FieldChange]) -> Iterator[str]:
    for field in fields:
        if field.added:
            yield f"+ {field.text}: {_value(field.after)}"
        elif field.removed:
            yield f"- {field.text}: {_value(field.before)}"
        else:
            yield f"~ {field.text}: {_value(field.before)} -> {_value(field.after)}"


def _document(document: Mapping[str, Any], *, verbose: bool) -> Iterator[str]:
    """A created or destroyed element, one line per top-level key of its spec.

    The whole document is in the JSON; what a reviewer needs from the text form
    is enough to recognise the element — what it is, what it is called, what it
    is plugged into — not a re-print of the file.
    """
    metadata = document.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("description"):
        yield f"description: {_value(metadata['description'])}"
    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        return
    for key, value in spec.items():
        if not verbose and key == "interfaces" and isinstance(value, Sequence):
            names = [str(entry.get("name")) for entry in value if isinstance(entry, Mapping)]
            yield f"spec.interfaces: {_value(names)}"
            continue
        yield f"spec.{key}: {_value(value)}"


def _value(value: Any) -> str:
    """One value, in a compact flow form, clipped to something readable."""
    if value is MISSING:
        return "(absent)"
    text = _flow(value)
    if len(text) <= _MAX_VALUE:
        return text
    return text[: _MAX_VALUE - 1] + "…"


def _flow(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{key}: {_flow(item)}" for key, item in value.items()) + "}"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return "[" + ", ".join(_flow(item) for item in value) + "]"
    if isinstance(value, str):
        return " ".join(value.split()) or "''"
    return str(value)
