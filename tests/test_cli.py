"""Smoke tests for the packaging and CLI wiring."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import netgraph
from netgraph.cli import AppContext, cli, main
from netgraph.errors import LoaderError, NetgraphError
from netgraph.render import RENDERERS

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@contextmanager
def _temporary_command(name: str) -> Iterator[list[AppContext]]:
    """Register a throwaway subcommand that records the context it was given."""
    seen: list[AppContext] = []

    @cli.command(name)
    @click.pass_obj
    def _capture(obj: AppContext) -> None:
        seen.append(obj)

    try:
        yield seen
    finally:
        cli.commands.pop(name, None)


def test_package_exposes_version() -> None:
    assert netgraph.__version__
    assert netgraph.__version__ != "0.0.0.dev0"


def test_help_lists_global_options(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "--inventory" in result.output
    assert "--verbose" in result.output


def test_render_help_enumerates_every_registered_format(runner: CliRunner) -> None:
    """``-f`` is generated from the registry, descriptions included.

    A backend added to :data:`~netgraph.render.RENDERERS` documents itself in
    ``--help`` and becomes a valid choice without the CLI being edited.
    """
    result = runner.invoke(cli, ["render", "--help"])
    assert result.exit_code == 0
    # Click wraps the help text, so match on words rather than whole clauses.
    for name, renderer in RENDERERS.items():
        assert name in result.output
        assert renderer.description.split()[0] in result.output


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert netgraph.__version__ in result.output


def test_bare_invocation_prints_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_help_works_even_with_a_missing_inventory(runner: CliRunner, tmp_path: Path) -> None:
    # ``--help`` is eager and must short-circuit before option validation.
    result = runner.invoke(cli, ["--inventory", str(tmp_path / "absent"), "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_missing_inventory_is_a_usage_error(runner: CliRunner, tmp_path: Path) -> None:
    with _temporary_command("capture") as seen:
        result = runner.invoke(cli, ["--inventory", str(tmp_path / "absent"), "capture"])
    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert not seen


def test_inventory_is_resolved_onto_the_context(runner: CliRunner, tmp_path: Path) -> None:
    with _temporary_command("capture") as seen:
        result = runner.invoke(cli, ["--inventory", str(tmp_path), "-vv", "capture"])

    assert result.exit_code == 0, result.output
    assert seen[0].inventory == tmp_path.resolve()
    assert seen[0].verbosity == 2


def test_main_returns_zero_on_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "Usage:" in capsys.readouterr().out


def test_main_reports_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--no-such-option"]) == 2
    assert "no-such-option" in capsys.readouterr().err


def test_main_translates_netgraph_errors(capsys: pytest.CaptureFixture[str]) -> None:
    @cli.command("boom")
    def _boom() -> None:
        raise LoaderError("inventory is unreadable")

    try:
        assert main(["boom"]) == LoaderError.exit_code
    finally:
        cli.commands.pop("boom", None)

    assert "error: inventory is unreadable" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("quiet", "verbosity", "level", "expected"),
    [
        (False, 0, 1, ""),
        (False, 1, 1, "hello\n"),
        (False, 1, 2, ""),
        (False, 2, 2, "hello\n"),
        (True, 3, 1, ""),
    ],
)
def test_log_respects_quiet_and_verbosity(
    capsys: pytest.CaptureFixture[str],
    quiet: bool,
    verbosity: int,
    level: int,
    expected: str,
) -> None:
    AppContext(quiet=quiet, verbosity=verbosity).log("hello", level=level)
    captured = capsys.readouterr()
    assert captured.err == expected
    assert captured.out == ""  # diagnostics must never pollute stdout


def test_main_handles_abort(capsys: pytest.CaptureFixture[str]) -> None:
    @cli.command("halt")
    def _halt() -> None:
        raise click.exceptions.Abort

    try:
        assert main(["halt"]) == 130
    finally:
        cli.commands.pop("halt", None)

    assert "Aborted." in capsys.readouterr().err


def test_the_package_is_executable_with_python_dash_m() -> None:
    """``python -m netgraph`` is a documented entry point, so it is tested.

    This is the one path :class:`CliRunner` cannot reach: it exercises
    ``__main__.py``, the console-script wiring and the installed package
    together, in a real interpreter.
    """
    result = subprocess.run(
        [sys.executable, "-m", "netgraph", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert netgraph.__version__ in result.stdout


def test_python_dash_m_propagates_a_failing_exit_code() -> None:
    """``raise SystemExit(main())`` must forward the code, not swallow it."""
    result = subprocess.run(
        [sys.executable, "-m", "netgraph", "--no-such-option"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 2
    assert "no-such-option" in result.stderr


def test_error_hierarchy_is_rooted() -> None:
    for exc in (LoaderError,):
        assert issubclass(exc, NetgraphError)
    assert NetgraphError.exit_code != 0
