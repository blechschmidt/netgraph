"""Shared pytest configuration.

The only project-wide switch is ``--regen-golden``, which rewrites the committed
renderer snapshots in ``tests/fixtures/golden/`` instead of asserting against
them. Regeneration is deliberately opt-in and never implicit: a snapshot that
silently rewrites itself when the renderer changes asserts nothing at all.
"""

from __future__ import annotations

import pytest


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
