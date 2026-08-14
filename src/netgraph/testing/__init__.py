"""Executable assertions about the network: ``netgraph test``.

An inventory is a source of truth, and a source of truth nobody checks is a
document that quietly stops being true. A ``kind: testsuite`` document (§20 of
``docs/schema.md``) says what somebody is relying on; this package grades it::

    report = run_tests(inventory)
    print(render_test_report(report, "text"))

Four modules, each independently testable:

1. :mod:`~netgraph.testing.selectors` turns an assertion's ``select:`` string
   into the very :class:`~netgraph.render.graph.FilterSpec` ``netgraph render``
   filters with, so nobody has to learn a second query language;
2. :mod:`~netgraph.testing.fields` reads a value out of an element by path, for
   the ``unique`` assertion;
3. :mod:`~netgraph.testing.engine` grades every assertion against the graphs
   :func:`~netgraph.render.graph.build_graph` builds, the routes
   :mod:`netgraph.trace` finds and the cut analysis
   :mod:`netgraph.impact` runs;
4. :mod:`~netgraph.testing.report` renders the verdicts as text, as JSON or as
   the JUnit XML every CI system displays natively.

Nothing here re-reads the inventory or re-resolves a reference, so a failing
test and a rendered diagram cannot disagree about what is connected to what.
"""

from __future__ import annotations

from netgraph.testing.engine import MAX_PAIRS, MAX_REPORTED, run_tests, suite_location
from netgraph.testing.fields import FieldError, evaluate, render_value
from netgraph.testing.model import (
    FAILED,
    PASSED,
    SKIPPED,
    STATES,
    Location,
    SuiteResult,
    TestReport,
    Verdict,
)
from netgraph.testing.report import (
    FORMATS,
    REPORT_KIND,
    as_json,
    as_junit_report,
    render_test_report,
    to_text,
)
from netgraph.testing.selectors import (
    SELECTOR_KEYS,
    SelectorError,
    parse_selector,
    select_nodes,
)

__all__ = [
    "FAILED",
    "FORMATS",
    "MAX_PAIRS",
    "MAX_REPORTED",
    "PASSED",
    "REPORT_KIND",
    "SELECTOR_KEYS",
    "SKIPPED",
    "STATES",
    "FieldError",
    "Location",
    "SelectorError",
    "SuiteResult",
    "TestReport",
    "Verdict",
    "as_json",
    "as_junit_report",
    "evaluate",
    "parse_selector",
    "render_test_report",
    "render_value",
    "run_tests",
    "select_nodes",
    "suite_location",
    "to_text",
]
