"""Rendering a graded run: a terminal report, a JSON document, JUnit XML.

``text``
    A progress line per suite — ``✓ connectivity  6 passed`` — then, under the
    failures only, what went wrong: the message, the file and line the assertion
    is written on, and what the graph actually contained. A passing run is four
    lines whatever its size; a failing one is as long as it has to be.

``json``
    The whole run, verdict by verdict, for a script that wants to do something
    other than print it.

``junit``
    The XML GitHub, GitLab and Jenkins all render natively, one ``<testcase>``
    per assertion grouped by suite. This is the format that turns a red pipeline
    into a list of named, clickable failures rather than a wall of log.

All three are built from the same
:class:`~netgraph.testing.model.TestReport` and none of them decides anything:
the engine has already said what failed and why, so the three cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, Final

from netgraph.diagnostics import JUnitCase, as_junit, dump_json
from netgraph.loader.inventory import short_name
from netgraph.testing.model import FAILED, PASSED, SKIPPED, SuiteResult, TestReport, Verdict

__all__ = [
    "FORMATS",
    "REPORT_KIND",
    "SCHEMA_VERSION",
    "as_json",
    "as_junit_report",
    "render_test_report",
    "to_text",
]

#: Every value ``netgraph test --output-format`` accepts.
FORMATS: Final[tuple[str, ...]] = ("text", "json", "junit")

#: ``kind`` of the JSON document, so a consumer can tell it from every other
#: JSON netgraph writes without guessing from its shape.
REPORT_KIND: Final = "TestReport"

#: Bumped when the JSON shape changes incompatibly.
SCHEMA_VERSION: Final = 1

#: The mark printed in front of a suite and of a failing assertion. ASCII, so a
#: Windows console with a legacy code page prints a report rather than raising.
_MARKS: Final[dict[str, str]] = {PASSED: "ok  ", FAILED: "FAIL", SKIPPED: "skip"}


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


def to_text(report: TestReport, *, verbose: bool = False) -> str:
    """The run as a report for a terminal.

    Args:
        report: The graded run.
        verbose: Also print a line per passing assertion. Off by default: the
            interesting output of a test run is the part that failed.
    """
    return "\n".join(_text_lines(report, verbose=verbose))


def _text_lines(report: TestReport, *, verbose: bool) -> Iterator[str]:
    for pattern in report.unmatched:
        yield f"no test suite matches {pattern!r}"
    if not report.suites and not report.unmatched:
        yield (
            "no 'kind: testsuite' document in this inventory; see docs/commands/test.md "
            "for what one looks like"
        )
        return
    if not report.suites:
        return

    for suite in report.suites:
        yield _suite_line(suite)
        for verdict in suite.verdicts:
            if verdict.passed and not verbose:
                continue
            yield from _verdict_lines(verdict)
    yield ""
    yield _summary(report)


def _suite_line(suite: SuiteResult) -> str:
    """``ok   connectivity  6 passed`` — one line per suite, whatever its size."""
    counts = ", ".join(
        f"{suite.count(state)} {state}" for state in (PASSED, FAILED, SKIPPED) if suite.count(state)
    )
    described = f"  ({suite.description})" if suite.description else ""
    return f"{_MARKS[suite.state]}  {short_name(suite.name)}  {counts}{described}"


def _verdict_lines(verdict: Verdict) -> Iterator[str]:
    """One failing (or skipped, or verbosely passing) assertion, in full."""
    where = f"  {verdict.location}" if verdict.location is not None else ""
    yield f"  {_MARKS[verdict.state]}  {verdict.title}  [{verdict.type}]{where}"
    if verdict.message:
        yield f"        {verdict.message}"
    for line in verdict.detail:
        yield f"          {line}"
    if verdict.description and not verdict.passed:
        yield f"        why: {verdict.description}"


def _summary(report: TestReport) -> str:
    """The last line: what a reader looks at first when the run was long."""
    counts = [
        f"{report.count(state)} {state}"
        for state in (PASSED, FAILED, SKIPPED)
        if report.count(state)
    ]
    body = ", ".join(counts) if counts else "0 assertions"
    suites = f" in {len(report.suites)} suite{'' if len(report.suites) == 1 else 's'}"
    return f"{body}{suites}"


# --------------------------------------------------------------------------- #
# json
# --------------------------------------------------------------------------- #


def as_json(report: TestReport) -> dict[str, Any]:
    """The run as a JSON-serialisable document."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "tool": {"name": "netgraph", "version": report.version},
        "inventory": str(report.root),
        "summary": {
            "suites": len(report.suites),
            "assertions": report.total,
            "passed": report.count(PASSED),
            "failed": report.count(FAILED),
            "skipped": report.count(SKIPPED),
            "ok": report.ok,
        },
        "unmatched": list(report.unmatched),
        "suites": [_suite_json(suite) for suite in report.suites],
    }


def _suite_json(suite: SuiteResult) -> dict[str, Any]:
    body: dict[str, Any] = {"name": suite.name, "state": suite.state}
    if suite.description:
        body["description"] = suite.description
    if suite.location is not None:
        body["location"] = _location_json(suite.location.file, suite.location.line)
    body["assertions"] = [_verdict_json(verdict) for verdict in suite.verdicts]
    return body


def _verdict_json(verdict: Verdict) -> dict[str, Any]:
    body: dict[str, Any] = {
        "index": verdict.index,
        "assert": verdict.type,
        "name": verdict.title,
        "state": verdict.state,
    }
    if verdict.message:
        body["message"] = verdict.message
    if verdict.detail:
        body["detail"] = list(verdict.detail)
    if verdict.elements:
        body["elements"] = list(verdict.elements)
    if verdict.description:
        body["description"] = verdict.description
    if verdict.location is not None:
        body["location"] = _location_json(verdict.location.file, verdict.location.line)
    return body


def _location_json(file: str, line: int | None) -> dict[str, Any]:
    return {"file": file} if line is None else {"file": file, "line": line}


# --------------------------------------------------------------------------- #
# junit
# --------------------------------------------------------------------------- #


def as_junit_report(report: TestReport) -> str:
    """The run as a JUnit XML document with one case per assertion.

    Every case carries the file and line of the assertion, as attributes and
    again in the body of ``<failure>``: GitLab reads the attributes, and every
    other reader shows the body, so the failure names its own source wherever
    it is displayed.
    """
    return as_junit(
        "netgraph test",
        list(_cases(report)),
        properties={"inventory": str(report.root), "netgraph": report.version},
        failure_type="assertion",
    )


def _cases(report: TestReport) -> Iterator[JUnitCase]:
    for suite in report.suites:
        for verdict in suite.verdicts:
            location = verdict.location
            yield JUnitCase(
                classname=verdict.classname,
                name=_case_name(suite, verdict),
                failure=verdict.message or None if verdict.failed else None,
                detail=_case_detail(verdict),
                skipped=verdict.message or "not run" if verdict.skipped else None,
                type=verdict.type,
                file=None if location is None else location.file,
                line=None if location is None else location.line,
            )


def _case_name(suite: SuiteResult, verdict: Verdict) -> str:
    """A name unique within the suite, so a reader does not merge two rows.

    Two assertions may legitimately carry the same ``name`` — the same claim
    made about two sites — so the index goes in front. It is also the position a
    reader counts to when they open the file.
    """
    duplicated = sum(1 for other in suite.verdicts if other.title == verdict.title) > 1
    return f"[{verdict.index}] {verdict.title}" if duplicated else verdict.title


def _case_detail(verdict: Verdict) -> str:
    """The body of ``<failure>``: where, why, and what was actually there."""
    lines: list[str] = []
    if verdict.location is not None:
        lines.append(f"at {verdict.location}")
    if verdict.description:
        lines.append(f"why: {verdict.description}")
    lines.extend(verdict.detail)
    if verdict.elements:
        lines.append("elements: " + ", ".join(_capped(verdict.elements)))
    return "\n".join(lines)


def _capped(names: Sequence[str], limit: int = 20) -> list[str]:
    if len(names) <= limit:
        return list(names)
    return [*names[:limit], f"... and {len(names) - limit} more"]


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def render_test_report(report: TestReport, output_format: str, *, verbose: bool = False) -> str:
    """Serialise ``report`` in one of :data:`FORMATS`.

    Raises:
        ValueError: ``output_format`` is not one of them.
    """
    if output_format == "text":
        return to_text(report, verbose=verbose)
    if output_format == "json":
        return dump_json(as_json(report))
    if output_format == "junit":
        return as_junit_report(report)
    raise ValueError(f"not a test report format: {output_format!r}")
