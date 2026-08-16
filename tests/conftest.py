"""Shared pytest configuration.

The parse cache is redirected to a temporary directory for the whole session
(:func:`isolate_the_parse_cache`), so a test run never reads or writes the
developer's real cache and a fresh checkout behaves like a machine that has run
netviz before. It is deliberately *not* switched off: the cache is on by
default in every command, and a suite that disabled it would leave the warm path
tested only by ``tests/test_cache.py`` while every golden, transcript and
end-to-end assertion kept exercising the cold one.

Two project-wide switches live here.

``--regen-golden`` rewrites the committed renderer snapshots in
``tests/fixtures/golden/`` instead of asserting against them. Regeneration is
deliberately opt-in and never implicit: a snapshot that silently rewrites itself
when the renderer changes asserts nothing at all.

:data:`PHASE_REPORTS` lets a fixture find out how the test it is tearing down
ended, which is what ``tests/test_browser.py`` needs to capture a screenshot and
the browser console log *only* for a failure. pytest does not otherwise tell a
fixture anything about the outcome, and a suite that saved an artefact for every
passing test would bury the one that matters.

``NETVIZ_HYPOTHESIS_PROFILE`` picks how hard the property tests in
``tests/test_properties.py`` and ``tests/test_fuzz_loader.py`` search. The
profiles are registered here rather than per module so that every property in
the suite runs under one budget and one seed; see :data:`PROFILES` for what each
one is for and ``docs/testing.md`` for how to run them.
"""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis.database import DirectoryBasedExampleDatabase

from netviz.loader import CACHE_DIR_ENV_VAR

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

_REQUESTED = os.environ.get("NETVIZ_HYPOTHESIS_PROFILE") or DEFAULT_PROFILE
if _REQUESTED not in PROFILES:
    # Hypothesis's own error for an unknown profile does not say what the known
    # ones are, and a typo in an environment variable that silently ran 25
    # examples where somebody asked for 1000 would be worse than either.
    raise RuntimeError(
        f"NETVIZ_HYPOTHESIS_PROFILE={_REQUESTED!r} is not a profile; "
        f"expected one of {', '.join(PROFILES)}"
    )
settings.load_profile(_REQUESTED)


#: Every phase's report, by ``"setup"``/``"call"``/``"teardown"``, stashed on the
#: item as soon as it exists. A fixture reads it during teardown to find out
#: whether the test it is finishing failed; see :func:`pytest_runtest_makereport`.
PHASE_REPORTS: Final[pytest.StashKey[dict[str, pytest.TestReport]]] = pytest.StashKey()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Record each phase's report on the item, for fixtures to consult.

    The standard recipe, and the only way a teardown can know what happened in
    the call phase. Nothing here changes the report; it is stashed and handed
    straight back.
    """
    report = yield
    item.stash.setdefault(PHASE_REPORTS, {})[report.when] = report
    return report


def failed(item: pytest.Item) -> bool:
    """Did any phase of ``item`` fail? Answerable from inside a fixture teardown."""
    return any(report.failed for report in item.stash.get(PHASE_REPORTS, {}).values())


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


#: Environment variables that decide, before any code in netviz runs, whether
#: its output is styled. ``NO_COLOR`` and ``FORCE_COLOR`` are read by
#: :meth:`netviz.console.Console._detect`; the rest are what the libraries
#: underneath consult for the same question.
_COLOUR_ENV_VARS: Final = ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE")


@pytest.fixture(scope="session", autouse=True)
def take_colour_out_of_the_environment() -> Iterator[None]:
    """Decide styling by the stream, never by the ambient environment.

    A dozen assertions in this suite are about *whether* netviz styles its
    output — that a redirect drops the escapes, that a table's header is plain
    when it is piped. Those are properties of the stream, and the environment can
    overrule the stream: ``FORCE_COLOR`` says "style anyway".

    CI sets exactly that, so every one of those assertions failed there and
    passed on the developer's machine — the worst shape a test failure comes in.
    The environment is therefore cleared for the whole session rather than
    per test, because several tests run netviz in a subprocess, which inherits
    it. A test that wants styling asks for it explicitly (``--color``, or a fake
    tty), which is the path a user takes too.
    """
    previous = {name: os.environ.pop(name, None) for name in _COLOUR_ENV_VARS}
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is not None:
                os.environ[name] = value


@pytest.fixture(scope="session", autouse=True)
def isolate_the_parse_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point :data:`~netviz.loader.CACHE_DIR_ENV_VAR` at a throwaway directory.

    Set in ``os.environ`` rather than through ``monkeypatch`` because several
    tests run the installed console script in a subprocess, and a cache is only
    isolated if the child process agrees where it is.

    One directory for the whole session, not one per test: entries are keyed by
    file contents, so a hit is by construction the same answer as a parse, and
    letting the suite hit means the assertions it already makes — goldens,
    documented transcripts, every CLI test — are made against the warm path too.
    """
    directory = tmp_path_factory.mktemp("netviz-cache")
    previous = os.environ.get(CACHE_DIR_ENV_VAR)
    os.environ[CACHE_DIR_ENV_VAR] = str(directory)
    try:
        yield directory
    finally:
        if previous is None:
            os.environ.pop(CACHE_DIR_ENV_VAR, None)
        else:  # pragma: no cover - only when a developer set it themselves
            os.environ[CACHE_DIR_ENV_VAR] = previous
