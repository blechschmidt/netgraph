"""The CI integrations this repository ships to other repositories.

``.pre-commit-hooks.yaml`` and ``.github/actions/netgraph-validate/action.yml``
are consumed by *other people's* repositories, at a tag, through machinery this
test suite never runs. Nothing else in the project would notice an option
renamed out from under them, a hook whose ``entry`` no longer resolves, or a
documented input the action does not declare — so the promises those files make
are asserted against the CLI here, where a break is cheap.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.report import FORMATS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_FILE = REPO_ROOT / ".pre-commit-hooks.yaml"
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "netgraph-validate"
ACTION_FILE = ACTION_DIR / "action.yml"
ACTION_README = ACTION_DIR / "README.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_DOC = REPO_ROOT / "docs" / "ci.md"


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def hooks() -> list[dict[str, Any]]:
    parsed = load_yaml(HOOKS_FILE)
    assert isinstance(parsed, list)
    return parsed


@pytest.fixture(scope="module")
def action() -> dict[str, Any]:
    parsed = load_yaml(ACTION_FILE)
    assert isinstance(parsed, dict)
    return parsed


# --------------------------------------------------------------------------- #
# pre-commit
# --------------------------------------------------------------------------- #


def test_the_hook_file_declares_netgraph_validate(hooks: list[dict[str, Any]]) -> None:
    ids = [hook["id"] for hook in hooks]
    assert ids == ["netgraph-validate"], "the published hook ids are an API; adding one is fine"

    (hook,) = hooks
    assert hook["entry"] == "netgraph validate"
    assert hook["language"] == "python", "pre-commit builds the venv from pyproject.toml"
    assert hook["pass_filenames"] is False, "the tree is validated as a whole"
    assert hook["require_serial"] is True
    assert hook["name"] and hook["description"]


def test_the_hook_only_runs_when_yaml_changed(hooks: list[dict[str, Any]]) -> None:
    pattern = re.compile(hooks[0]["files"])
    assert pattern.search("inventory/devices/sw.yaml")
    assert pattern.search("sw.yml")
    assert not pattern.search("README.md")


def test_the_hook_entry_is_a_command_the_cli_actually_has() -> None:
    """``entry: netgraph validate`` has to keep resolving, options and all."""
    result = CliRunner().invoke(cli, ["validate", "--help"])
    assert result.exit_code == 0
    for option in ("--strict", "--disable", "--output-format"):
        assert option in result.output


def test_the_local_config_does_not_shadow_the_published_hooks() -> None:
    """``.pre-commit-config.yaml`` is what netgraph runs on itself; a different file."""
    config = load_yaml(REPO_ROOT / ".pre-commit-config.yaml")
    assert "repos" in config
    assert not isinstance(config, list), "the two files must not be confused for one another"


# --------------------------------------------------------------------------- #
# The composite action
# --------------------------------------------------------------------------- #


#: What ``docs/ci.md`` and the action README promise. Removing one is a breaking
#: change for every workflow that sets it.
EXPECTED_INPUTS = {
    "inventory",
    "strict",
    "disable",
    "output-format",
    "output-file",
    "fail-on-error",
    "install",
    "version",
}
EXPECTED_OUTPUTS = {"exit-code", "failed", "report"}


def test_the_action_is_a_composite_action(action: dict[str, Any]) -> None:
    assert action["runs"]["using"] == "composite"
    assert action["name"] and action["description"]
    for step in action["runs"]["steps"]:
        assert step["shell"] == "bash" or "uses" in step


def test_the_action_declares_the_documented_inputs_and_outputs(action: dict[str, Any]) -> None:
    assert set(action["inputs"]) == EXPECTED_INPUTS
    assert set(action["outputs"]) == EXPECTED_OUTPUTS
    for name, spec in action["inputs"].items():
        assert spec["description"].strip(), f"input {name} is undocumented"
        assert "default" in spec, f"input {name} has no default, so it is effectively required"


def test_the_action_defaults_agree_with_the_cli(action: dict[str, Any]) -> None:
    inputs = action["inputs"]
    assert inputs["output-format"]["default"] in FORMATS
    assert inputs["inventory"]["default"] == "."
    # Booleans are strings in the Actions expression language; anything else
    # would silently compare unequal to 'true' in the step's shell.
    for name in ("strict", "fail-on-error", "install"):
        assert inputs[name]["default"] in {"true", "false"}, name


def test_the_action_validates_its_own_output_format(action: dict[str, Any]) -> None:
    """A typo in ``output-format`` must fail loudly, not fall through to a default."""
    script = "\n".join(step.get("run", "") for step in action["runs"]["steps"] if "run" in step)
    assert "text|json|sarif|github" in script
    for name in FORMATS:
        assert name in script


def test_the_action_readme_documents_every_input_and_output(action: dict[str, Any]) -> None:
    readme = ACTION_README.read_text(encoding="utf-8")
    for name in action["inputs"]:
        assert f"`{name}`" in readme, f"input {name} is not in the action README"
    for name in action["outputs"]:
        assert f"`{name}`" in readme, f"output {name} is not in the action README"


def test_the_action_readme_shows_a_usable_snippet() -> None:
    readme = ACTION_README.read_text(encoding="utf-8")
    assert "uses: blechschmidt/netgraph/.github/actions/netgraph-validate@" in readme
    assert "github/codeql-action/upload-sarif" in readme
    assert "actions/setup-python" in readme, "the action deliberately installs no interpreter"


# --------------------------------------------------------------------------- #
# This repository eats its own cooking
# --------------------------------------------------------------------------- #


def test_ci_runs_the_action_over_the_examples() -> None:
    """The integration is only shipped if this repository exercises it."""
    workflow = load_yaml(WORKFLOW)
    job = workflow["jobs"]["validate-examples"]
    used = [step.get("uses", "") for step in job["steps"]]

    assert used.count("./.github/actions/netgraph-validate") == 2, (
        "the sarif path and the annotation path are both meant to be exercised"
    )
    assert any(entry.startswith("github/codeql-action/upload-sarif") for entry in used)
    assert job["permissions"]["security-events"] == "write"


def test_every_example_inventory_is_covered_by_the_matrix() -> None:
    """The matrix is discovered, not listed, so a new example cannot be forgotten."""
    workflow = load_yaml(WORKFLOW)
    matrix = workflow["jobs"]["validate-examples"]["strategy"]["matrix"]["inventory"]
    assert "needs.discover-examples.outputs.inventories" in matrix

    examples = sorted(path.name for path in (REPO_ROOT / "examples").iterdir() if path.is_dir())
    assert len(examples) >= 2, "the discovery job fails below two, and so should this"


def test_the_ci_documentation_covers_every_format() -> None:
    text = CI_DOC.read_text(encoding="utf-8")
    for name in FORMATS:
        assert f"`{name}`" in text
    for section in ("pre-commit", "The GitHub Action", "The JSON envelope"):
        assert section in text
