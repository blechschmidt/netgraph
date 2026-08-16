"""Executable assertions about the network: ``netviz test``.

An inventory is a source of truth, and a source of truth nobody checks is a
document that quietly stops being true. A ``kind: testsuite`` document (§20 of
``docs/schema.md``) says what somebody is relying on; this package grades it::

    report = run_tests(inventory)
    print(render_test_report(report, "text"))

Four modules, each independently testable:

1. :mod:`~netviz.testing.selectors` turns an assertion's ``select:`` string
   into the very :class:`~netviz.render.graph.FilterSpec` ``netviz render``
   filters with, so nobody has to learn a second query language;
2. :mod:`~netviz.testing.fields` reads a value out of an element by path, for
   the ``unique`` assertion;
3. :mod:`~netviz.testing.engine` grades every assertion against the graphs
   :func:`~netviz.render.graph.build_graph` builds, the routes
   :mod:`netviz.trace` finds and the cut analysis
   :mod:`netviz.impact` runs;
4. :mod:`~netviz.testing.report` renders the verdicts as text, as JSON or as
   the JUnit XML every CI system displays natively.

Nothing here re-reads the inventory or re-resolves a reference, so a failing
test and a rendered diagram cannot disagree about what is connected to what.
"""

from __future__ import annotations

from netviz.testing.engine import MAX_PAIRS, MAX_REPORTED, run_tests, suite_location
from netviz.testing.fields import FieldError, evaluate, render_value
from netviz.testing.model import (
    FAILED,
    PASSED,
    SKIPPED,
    STATES,
    Location,
    SuiteResult,
    TestReport,
    Verdict,
)
from netviz.testing.report import (
    FORMATS,
    REPORT_KIND,
    as_json,
    as_junit_report,
    render_test_report,
    to_text,
)
from netviz.testing.selectors import (
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
