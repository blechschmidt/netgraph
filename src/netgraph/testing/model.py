"""What ``netgraph test`` produces: one verdict per assertion, grouped by suite.

Three renderers consume these types — a terminal report, a JSON document and
JUnit XML — and none of them may compute anything the others do not see. So a
:class:`Verdict` carries everything a failure needs to be actionable, decided
once by the engine:

* **which assertion** — its title, its type, and its position in the suite;
* **which elements** — the fully-qualified names the verdict is about, so a
  failure over a selector says *which* switch, not "a switch";
* **what the graph actually contained** — the :attr:`Verdict.detail` lines,
  which are the whole difference between "not reachable" and a report somebody
  can act on;
* **where the assertion is written** — :class:`Location`, taken from the
  loader's provenance, in the ``file:line`` form every editor and every CI
  annotation turns into a link.

A verdict is in exactly one of three states. ``failed`` and ``passed`` are the
obvious two; ``skipped`` is for an assertion that could not be *asked* — a
single-point-of-failure claim about the power plan of an inventory that declares
no PDU. It is deliberately not a pass: a check that did not run is not a check
that succeeded, and JUnit already has the word for it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = [
    "FAILED",
    "PASSED",
    "SKIPPED",
    "STATES",
    "Location",
    "SuiteResult",
    "TestReport",
    "Verdict",
]

PASSED: Final = "passed"
FAILED: Final = "failed"
SKIPPED: Final = "skipped"

#: Every state a verdict can be in, worst first — the order a summary counts in.
STATES: Final[tuple[str, ...]] = (FAILED, SKIPPED, PASSED)


@dataclass(frozen=True, slots=True)
class Location:
    """Where something is written, in the form an editor turns into a link."""

    #: Path of the file relative to the inventory root, POSIX style.
    file: str
    #: 1-based line, or ``None`` when the loader could not narrow it further.
    line: int | None = None

    def __str__(self) -> str:
        return self.file if self.line is None else f"{self.file}:{self.line}"


@dataclass(frozen=True, slots=True)
class Verdict:
    """One assertion, graded."""

    #: Fully-qualified name of the suite the assertion belongs to.
    suite: str
    #: 0-based position within the suite, so two identically named assertions
    #: are still distinguishable in a report that sorts.
    index: int
    #: What the assertion claims: ``reachable``, ``unique``, ...
    type: str
    #: How the assertion is reported — its ``name``, or a rendering of its keys.
    title: str
    #: One of :data:`STATES`.
    state: str
    #: One line saying what happened. Empty on a pass.
    message: str = ""
    #: What the graph actually contained, one fact per line.
    detail: tuple[str, ...] = ()
    #: The elements the verdict is about, in graph order.
    elements: tuple[str, ...] = ()
    #: Where the assertion is written.
    location: Location | None = None
    #: The ``description`` the assertion carries, if any.
    description: str = ""

    @property
    def failed(self) -> bool:
        return self.state == FAILED

    @property
    def passed(self) -> bool:
        return self.state == PASSED

    @property
    def skipped(self) -> bool:
        return self.state == SKIPPED

    @property
    def classname(self) -> str:
        """How a JUnit reader groups this case: by suite, then by assertion type."""
        return f"netgraph.test.{self.suite}"


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """One ``kind: testsuite`` document, graded."""

    #: Fully-qualified name of the suite.
    name: str
    #: Its ``spec.description``, or the empty string.
    description: str = ""
    #: Where the document is.
    location: Location | None = None
    #: The verdicts, in the order the assertions are written.
    verdicts: tuple[Verdict, ...] = ()

    def count(self, state: str) -> int:
        """How many verdicts are in ``state``."""
        return sum(1 for verdict in self.verdicts if verdict.state == state)

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def failures(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.failed)

    @property
    def ok(self) -> bool:
        """Did every assertion in this suite that ran hold?

        A skip does not fail the run — that is what every test runner means by
        the word, and a claim about a power plan an inventory does not declare
        is genuinely not a claim this inventory got wrong. It is still counted
        and still printed, so a suite that skipped everything cannot pass for a
        suite that checked everything.
        """
        return not self.failures

    @property
    def state(self) -> str:
        """The suite's own state: the worst state any of its verdicts is in."""
        for state in STATES:
            if any(verdict.state == state for verdict in self.verdicts):
                return state
        return PASSED  # pragma: no cover - a suite always has an assertion


@dataclass(frozen=True, slots=True)
class TestReport:
    """Every suite that ran, and the inventory they ran against."""

    #: Root the inventory was loaded from.
    root: Path
    #: The suites, in load order.
    suites: tuple[SuiteResult, ...] = ()
    #: netgraph's version, quoted so a stored report says what produced it.
    version: str = ""
    #: Names given on the command line that matched no suite. Reported rather
    #: than ignored: ``netgraph test typo`` running zero assertions and exiting
    #: 0 is the exact false green this command exists to prevent.
    unmatched: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verdicts(self) -> Iterator[Verdict]:
        """Every verdict of every suite, in order."""
        for suite in self.suites:
            yield from suite.verdicts

    def count(self, state: str) -> int:
        """How many verdicts across every suite are in ``state``."""
        return sum(suite.count(state) for suite in self.suites)

    @property
    def total(self) -> int:
        return sum(suite.total for suite in self.suites)

    @property
    def ok(self) -> bool:
        """Did the whole run hold?

        False when any assertion failed, when a name given on the command line
        matched no suite, and when **nothing was graded at all**. A run that
        checked nothing is the false green this command exists to prevent, and
        it is what ``pytest`` does with an empty collection for the same reason.
        """
        if self.unmatched or not self.total:
            return False
        return all(suite.ok for suite in self.suites)
