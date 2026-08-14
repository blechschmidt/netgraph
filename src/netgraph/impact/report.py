"""Rendering an impact analysis: for a person, and for a program.

Two shapes, one document behind both. ``text`` is what an operator reads at
three in the morning while deciding whether to send somebody to a data centre;
``json`` is what a script reads to decide whether the change goes ahead. They
are generated from the same :class:`~netgraph.impact.model.ImpactReport`, so
they cannot disagree, and both are byte-identical between two runs over an
unchanged tree.

There is deliberately no SARIF here. SARIF describes *findings in files*, and
"losing sw-core-01 strands 43 hosts" is not a finding in a file — it is a
property of a network that no line of YAML is guilty of. The one part of this
command that does have a file to point at is ``--redundancy``, whose
expectations are graded by ordinary validation rules, and those go out through
:mod:`netgraph.diagnostics` like every other finding, SARIF included.

House style: nothing is printed twice, a section with nothing in it is left out
entirely rather than printed empty, and every count says what it counted.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Final

from netgraph import __version__
from netgraph.diagnostics import dump_json
from netgraph.impact.model import (
    CAUSE_REQUESTED,
    STATUS_UNCHANGED,
    ImpactReport,
    LayerResult,
    PathResult,
    Spof,
)
from netgraph.loader.inventory import short_name

__all__ = ["REPORT_FORMATS", "render_impact", "to_json", "to_text"]

#: Every value ``impact --output-format`` accepts for the analysis itself.
#: ``sarif`` and ``github`` are accepted by the CLI for ``--redundancy`` alone,
#: where the output is a list of findings rather than an analysis.
REPORT_FORMATS: Final[tuple[str, ...]] = ("text", "json")

#: How many isolated elements a layer lists before it stops naming them. The
#: whole list is in the JSON; a text report that printed four hundred names
#: would bury the four counts somebody actually reads.
MAX_LISTED: Final = 12

#: What to call the anchors, given how they were chosen. A reader has to know
#: whether the answer is measured from what they asked for or from what the
#: inventory implied, and "1 anchor" says neither.
ANCHOR_NOUNS: Final[dict[str, str]] = {
    "given": "anchor",
    "gateways": "gateway",
    "routers": "router (no gateway is declared)",
}


def render_impact(report: ImpactReport, output_format: str) -> str:
    """Serialise ``report`` in one of :data:`REPORT_FORMATS`.

    Raises:
        ValueError: ``output_format`` is not one of them.
    """
    if output_format == "text":
        return to_text(report)
    if output_format == "json":
        return to_json(report)
    raise ValueError(f"not an impact report format: {output_format!r}")


def to_json(report: ImpactReport) -> str:
    """The whole analysis as one JSON document."""
    return dump_json(report.as_dict(version=__version__))


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


def to_text(report: ImpactReport) -> str:
    """The analysis as a person reads it."""
    return "\n".join(_lines(report)).rstrip("\n") + "\n"


def _lines(report: ImpactReport) -> Iterator[str]:
    yield from _failures(report)
    yield from _anchors(report)
    for layer in report.layers:
        yield from _layer(layer)
    yield from _paths(report.paths)
    yield from _spofs(report)
    yield from _expectations(report)
    yield from _summary(report)
    for note in report.notes:
        yield f"note: {note}"


def _failures(report: ImpactReport) -> Iterator[str]:
    if not report.failures:
        return
    requested = [failure for failure in report.failures if failure.cause == CAUSE_REQUESTED]
    collateral = [failure for failure in report.failures if failure.is_collateral]
    yield _count(len(requested), "element") + " removed:"
    for failure in requested:
        yield f"  {failure.describe()}"
    if collateral:
        yield ""
        yield f"{_count(len(collateral), 'element')} lost power as a consequence:"
        for failure in collateral:
            yield f"  {failure.describe()}"
    yield ""


def _anchors(report: ImpactReport) -> Iterator[str]:
    if not report.anchors:
        return
    named = ", ".join(short_name(anchor) for anchor in report.anchors[:MAX_LISTED])
    more = len(report.anchors) - MAX_LISTED
    if more > 0:
        named = f"{named} and {more} more"
    noun = ANCHOR_NOUNS.get(report.anchor_source, "anchor")
    yield f"reachable from {_count(len(report.anchors), noun)}: {named}"
    yield ""


def _layer(layer: LayerResult) -> Iterator[str]:
    headline = f"{layer.label}"
    if layer.anchors:
        headline += f": {layer.served_after} of {layer.served_before} elements still reachable"
    elif layer.note:
        headline += f": {layer.note}"
    yield headline
    if layer.isolated:
        yield f"  {_count(len(layer.isolated), 'element')} isolated:"
        yield from _names(layer.isolated)
    for split in layer.splits:
        yield f"  {split.label} is now in {_count(split.after, 'piece')}, was {split.before}:"
        for index, fragment in enumerate(split.fragments, start=1):
            names = ", ".join(short_name(member) for member in fragment[:MAX_LISTED])
            more = len(fragment) - MAX_LISTED
            yield f"    {index}. {names}{f' and {more} more' if more > 0 else ''}"
    if not layer.affected and layer.anchors:
        if layer.removed_nodes:
            yield (
                f"  nothing beyond the {_count(len(layer.removed_nodes), 'element')} removed "
                f"here became unreachable"
            )
        else:
            yield "  nothing became unreachable"
    if layer.stranded:
        yield (
            f"  ({_count(len(layer.stranded), 'element')} was already unreachable before "
            f"the failure)"
            if len(layer.stranded) == 1
            else f"  ({len(layer.stranded)} elements were already unreachable before the failure)"
        )
    yield ""


def _names(names: Sequence[str]) -> Iterator[str]:
    for name in names[:MAX_LISTED]:
        yield f"    {name}"
    if len(names) > MAX_LISTED:
        yield f"    … and {len(names) - MAX_LISTED} more"


def _paths(paths: Sequence[PathResult]) -> Iterator[str]:
    if not paths:
        return
    yield "paths:"
    for path in paths:
        detail = f"{path.before} before, {path.after} after"
        if path.note:
            detail = path.note
        yield f"  {path.status:<9} {path.source} → {path.destination}  ({detail})"
    yield ""


def _spofs(report: ImpactReport) -> Iterator[str]:
    if not report.spofs:
        if "spof" in report.modes:
            yield "no single point of failure isolates anything"
            yield ""
        return
    heading = f"{_count(report.spof_total, 'single point of failure')}"
    if report.truncated:
        heading += f", worst {len(report.spofs)} shown"
    yield f"{heading}:"
    yield f"  {'isolates':>8}  {'layer':<6} {'what'}"
    for spof in report.spofs:
        yield f"  {spof.isolated:>8}  {spof.layer:<6} {_describe(spof)}"
    yield ""


def _describe(spof: Spof) -> str:
    """``sw-core-01 (switch, articulation point)`` — the entry, in one clause."""
    parts = [part for part in (spof.element_kind, spof.reason) if part]
    return f"{spof.name} ({', '.join(parts)})" if parts else spof.name


def _expectations(report: ImpactReport) -> Iterator[str]:
    if "redundancy" not in report.modes:
        return
    if not report.findings:
        yield "every declared redundancy expectation is met"
        yield ""
        return
    yield f"{_count(len(report.findings), 'redundancy expectation')} not met:"
    for finding in report.findings:
        where = f" ({finding.file})" if finding.file else ""
        yield f"  {finding.rule} {finding.message}{where}"
    yield ""


def _summary(report: ImpactReport) -> Iterator[str]:
    if not report.failures:
        return
    parts = [f"{report.isolated_total} isolated at worst"]
    broken = len(report.broken_paths)
    if report.paths:
        parts.append(f"{broken} of {len(report.paths)} paths broken")
    unchanged = sum(1 for path in report.paths if path.status == STATUS_UNCHANGED)
    if report.paths and unchanged == len(report.paths):
        parts[-1] = "every checked path survives"
    yield "summary: " + ", ".join(parts)


def _count(number: int, noun: str) -> str:
    """``1 element`` / ``3 elements``, pluralised the way English mostly is."""
    if number == 1:
        return f"1 {noun}"
    plural = f"{noun}es" if noun.endswith(("s", "x", "ch")) else f"{noun}s"
    if " of " in noun:
        head, _, tail = noun.partition(" of ")
        plural = f"{head}s of {tail}"
    return f"{number} {plural}"


def _flatten(groups: Iterable[Sequence[str]]) -> tuple[str, ...]:
    """Every name in ``groups``, first-seen order, no repeats."""
    seen: dict[str, None] = {}
    for group in groups:
        for name in group:
            seen.setdefault(name, None)
    return tuple(seen)
