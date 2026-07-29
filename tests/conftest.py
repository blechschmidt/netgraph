"""Shared pytest configuration.

Two project-wide switches live here.

``--regen-golden`` rewrites the committed renderer snapshots in
``tests/fixtures/golden/`` instead of asserting against them. Regeneration is
deliberately opt-in and never implicit: a snapshot that silently rewrites itself
when the renderer changes asserts nothing at all.

``NETGRAPH_HYPOTHESIS_PROFILE`` picks how hard the property tests in
``tests/test_properties.py`` and ``tests/test_fuzz_loader.py`` search. The
profiles are registered here rather than per module so that every property in
the suite runs under one budget and one seed; see :data:`PROFILES` for what each
one is for and ``docs/testing.md`` for how to run them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis.database import DirectoryBasedExampleDatabase

#: Where a failing example is recorded so the next run tries it first. Kept at
#: the repository root rather than in a temporary directory precisely so it can
#: be cached between CI runs: a property test that has already found a bug once
#: should never have to search for it a second time.
EXAMPLE_DATABASE: Final = Path(__file__).resolve().parent.parent / ".hypothesis" / "examples"

#: The example count each profile searches with. The names are the contract;
#: the numbers are a budget and may be tuned.
PROFILES: Final[dict[str, int]] = {
    # What a developer gets by default: enough to catch a regression in the
    # thing they just changed, fast enough to run on every save.
    "dev": 25,
    # What CI runs on every push. Fixed seed, capped count -- a property test
    # that flaked half the time would be worse than no property test at all.
    "ci": 50,
    # The nightly budget, and what to run locally when a property has just been
    # written and nobody knows yet whether it holds.
    "deep": 1000,
}

#: The profile used when nothing says otherwise.
DEFAULT_PROFILE: Final = "dev"

for _name, _examples in PROFILES.items():
    settings.register_profile(
        _name,
        max_examples=_examples,
        # Generating an inventory, writing it to disk and rendering it through
        # Graphviz is not a microsecond operation, and a deadline measured
        # against the first (JIT-cold, page-cache-cold) example produces flakes
        # that say nothing about the property.
        deadline=None,
        # The seed is *not* pinned with ``derandomize`` here, even though that
        # is the obvious way to spell it: Hypothesis refuses ``derandomize``
        # together with an explicit database, and the database is the half that
        # makes a failure cheap to re-run. Determinism comes from
        # ``--hypothesis-seed``, which pyproject.toml passes on every run and a
        # command line can still override; see ``docs/testing.md``.
        database=DirectoryBasedExampleDatabase(str(EXAMPLE_DATABASE)),
        # Every phase, including ``explain``: when one of these properties does
        # fail, the counterexample is an inventory, and an unshrunk one is a
        # wall of YAML rather than a bug report.
        phases=tuple(Phase),
        # These properties write to a temporary directory and shell out to
        # Graphviz, so "too slow" and "too much data" are expected in the tail
        # rather than symptoms of a broken strategy.
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
        print_blob=True,
    )

_REQUESTED = os.environ.get("NETGRAPH_HYPOTHESIS_PROFILE") or DEFAULT_PROFILE
if _REQUESTED not in PROFILES:
    # Hypothesis's own error for an unknown profile does not say what the known
    # ones are, and a typo in an environment variable that silently ran 25
    # examples where somebody asked for 1000 would be worse than either.
    raise RuntimeError(
        f"NETGRAPH_HYPOTHESIS_PROFILE={_REQUESTED!r} is not a profile; "
        f"expected one of {', '.join(PROFILES)}"
    )
settings.load_profile(_REQUESTED)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--regen-golden",
        action="store_true",
        default=False,
        help="Rewrite the renderer golden files in tests/fixtures/golden/ instead of "
        "comparing against them. Review the resulting diff before committing.",
    )


@pytest.fixture(scope="session")
def regen_golden(request: pytest.FixtureRequest) -> bool:
    """Was the suite invoked with ``--regen-golden``?"""
    return bool(request.config.getoption("--regen-golden"))
