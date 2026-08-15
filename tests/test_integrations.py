"""The CI integrations this repository ships to other repositories.

``.pre-commit-hooks.yaml``, the three composite actions under ``.github/actions/``
and the reusable workflows under ``.github/workflows/`` are consumed
by *other people's* repositories, at a tag, through machinery this test suite
never runs. Nothing else in the project would notice an option renamed out from
under them, a hook whose ``entry`` no longer resolves, or a documented input the
action does not declare — so the promises those files make are asserted against
the CLI here, where a break is cheap.

The render action goes one step further than assertion: its shell is *run*, with
the environment the action file itself says it builds. Quoting, word splitting
and the suffix it derives are not things a schema check can see.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.diagnostics import FORMATS
from netgraph.render import FORMATS as RENDER_FORMATS
from netgraph.render import TEXT_FORMATS, diff_formats, suffix_for, supports_diff
from netgraph.review import MARKER_PREFIX

from platform_marks import (  # isort: skip -- tests/ is on sys.path, not a package
    ON_WINDOWS,
    requires_dot,
    requires_unexpanded_globs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_FILE = REPO_ROOT / ".pre-commit-hooks.yaml"
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "netgraph-validate"
ACTION_FILE = ACTION_DIR / "action.yml"
ACTION_README = ACTION_DIR / "README.md"
RENDER_ACTION_DIR = REPO_ROOT / ".github" / "actions" / "netgraph-render"
RENDER_ACTION_FILE = RENDER_ACTION_DIR / "action.yml"
RENDER_ACTION_README = RENDER_ACTION_DIR / "README.md"
REVIEW_ACTION_DIR = REPO_ROOT / ".github" / "actions" / "netgraph-review"
REVIEW_ACTION_FILE = REVIEW_ACTION_DIR / "action.yml"
REVIEW_ACTION_README = REVIEW_ACTION_DIR / "README.md"
PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "netgraph-pages.yml"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "netgraph-review.yml"
DOGFOOD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review.yml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_DOC = REPO_ROOT / "docs" / "ci.md"
HOME_LAB = REPO_ROOT / "examples" / "home-lab"


def find_bash() -> str | None:
    """The bash GitHub would run a ``shell: bash`` step with, or ``None``.

    The steps are bash — arrays, ``set -f`` — so ``sh`` will not do, and on
    Windows ``shutil.which`` is not the answer either: it finds
    ``C:\\Windows\\System32\\bash.exe`` first, which is the WSL launcher, and on
    a runner with no distribution installed that answers every command with
    "Windows Subsystem for Linux has no installed distributions". GitHub itself
    runs these steps with Git Bash, and so does this. Which one works is
    *measured* rather than assumed, for the same reason
    ``platform_marks._can_symlink`` measures.
    """
    candidates = [r"C:\Program Files\Git\bin\bash.exe"] if ON_WINDOWS else []
    found = shutil.which("bash")
    if found is not None:
        candidates.append(found)
    for candidate in candidates:
        if not Path(candidate).is_file():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "echo ok"], capture_output=True, text=True, timeout=60
            )
        except OSError:  # pragma: no cover - a bash that cannot be started at all
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


#: The bash the action's steps are handed to, or ``None`` where there is none.
BASH = find_bash()

requires_bash = pytest.mark.skipif(
    BASH is None, reason="the action's steps are bash, and there is no working bash here"
)


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


#: Every hook this repository publishes, in file order. A hook id is an API --
#: somebody else's ``.pre-commit-config.yaml`` names it -- so adding one is
#: fine and renaming or removing one is a breaking change.
PUBLISHED_HOOKS = [
    "netgraph-validate",
    "netgraph-test",
    "netgraph-fmt",
    "netgraph-fmt-check",
]


def test_the_hook_file_declares_exactly_the_published_hooks(hooks: list[dict[str, Any]]) -> None:
    assert [hook["id"] for hook in hooks] == PUBLISHED_HOOKS


@pytest.mark.parametrize("hook_id", PUBLISHED_HOOKS)
def test_every_hook_is_declared_the_same_way(hooks: list[dict[str, Any]], hook_id: str) -> None:
    hook = next(entry for entry in hooks if entry["id"] == hook_id)
    assert hook["language"] == "python", "pre-commit builds the venv from pyproject.toml"
    assert hook["require_serial"] is True, "one inventory, one process"
    assert hook["name"] and hook["description"]


def test_validation_takes_no_filenames_and_formatting_takes_them(
    hooks: list[dict[str, Any]],
) -> None:
    """The one place the hooks deliberately differ, and why.

    A cable is only dangling when compared against the devices in the *other*
    files, and an assertion is about the whole network, so both of those are
    properties of the tree. Formatting is a property of a file, so there is no
    reason to walk the tree to do it.
    """
    filenames = {hook["id"]: hook["pass_filenames"] for hook in hooks}
    assert filenames == {
        "netgraph-validate": False,
        "netgraph-test": False,
        "netgraph-fmt": True,
        "netgraph-fmt-check": True,
    }


@pytest.mark.parametrize("hook_id", PUBLISHED_HOOKS)
def test_every_hook_only_runs_when_yaml_changed(hooks: list[dict[str, Any]], hook_id: str) -> None:
    hook = next(entry for entry in hooks if entry["id"] == hook_id)
    pattern = re.compile(hook["files"])
    assert pattern.search("inventory/devices/sw.yaml")
    assert pattern.search("sw.yml")
    assert not pattern.search("README.md")


@pytest.mark.parametrize(
    ("hook_id", "command", "options"),
    [
        ("netgraph-validate", "validate", ("--strict", "--disable", "--output-format")),
        ("netgraph-test", "test", ("--output-format", "--list", "--max-hops")),
        ("netgraph-fmt", "fmt", ("--check", "--diff", "--stdin")),
        ("netgraph-fmt-check", "fmt", ("--check",)),
    ],
)
def test_every_hook_entry_is_a_command_the_cli_actually_has(
    hooks: list[dict[str, Any]], hook_id: str, command: str, options: tuple[str, ...]
) -> None:
    """An ``entry`` is run verbatim by pre-commit; it has to keep resolving."""
    hook = next(entry for entry in hooks if entry["id"] == hook_id)
    words = hook["entry"].split()
    assert words[0] == "netgraph"
    assert words[1] == command

    result = CliRunner().invoke(cli, [*words[1:], "--help"])
    assert result.exit_code == 0
    for option in options:
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
# The render action
# --------------------------------------------------------------------------- #


#: The other half of the promise: what the README and ``docs/ci.md`` say a
#: workflow may set on ``netgraph-render``.
EXPECTED_RENDER_INPUTS = {
    "inventory",
    "format",
    "output",
    "layer",
    "title",
    "theme",
    "args",
    "strict",
    "force",
    "graphviz",
    "install",
    "version",
}
EXPECTED_RENDER_OUTPUTS = {"file", "directory", "bytes"}

#: ``${{ inputs.name }}`` and nothing else. Every value the shell steps read is
#: passed through the ``env`` block in exactly that form, which is what lets a
#: test build the same environment without reimplementing the action.
_INPUT_EXPRESSION = re.compile(r"^\$\{\{\s*inputs\.([a-z-]+)\s*\}\}$")


@pytest.fixture(scope="module")
def render_action() -> dict[str, Any]:
    parsed = load_yaml(RENDER_ACTION_FILE)
    assert isinstance(parsed, dict)
    return parsed


def step_of(action: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(step for step in action["runs"]["steps"] if step.get("id") == step_id)


def environment_for(action: dict[str, Any], step_id: str, values: dict[str, str]) -> dict[str, str]:
    """The env GitHub would build for ``step_id``, from the action's own ``env``.

    Reading the mapping out of the file rather than repeating it here is what
    makes a renamed variable a failure: the step would be handed an environment
    that no longer matches the one it reads.
    """
    defaults = {name: str(spec["default"]) for name, spec in action["inputs"].items()}
    unknown = set(values) - set(defaults)
    assert not unknown, f"no such input: {sorted(unknown)}"

    environment = {}
    for variable, expression in step_of(action, step_id)["env"].items():
        matched = _INPUT_EXPRESSION.match(expression)
        assert matched is not None, f"{variable} is not a plain input reference: {expression}"
        name = matched.group(1)
        environment[variable] = values.get(name, defaults[name])
    return environment


def run_step(
    action: dict[str, Any], step_id: str, values: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run one of the action's steps for real, in a scratch directory."""
    environment = dict(os.environ)
    environment.update(environment_for(action, step_id, values))
    environment["RUNNER_TEMP"] = str(tmp_path / "runner-temp")
    environment["GITHUB_OUTPUT"] = str(tmp_path / "github-output")
    # The action calls ``netgraph``, so the console script has to be findable.
    # ``sysconfig`` is asked where it went rather than assuming "next to the
    # interpreter", because those are the same directory only on POSIX: Windows
    # puts console scripts in ``Scripts\`` beside ``python.exe``. The same
    # lookup, and the same reason, as ``tools/check_examples.py``.
    environment["PATH"] = os.pathsep.join(
        [sysconfig.get_path("scripts"), str(Path(sys.executable).parent), environment["PATH"]]
    )
    Path(environment["RUNNER_TEMP"]).mkdir(parents=True, exist_ok=True)
    Path(environment["GITHUB_OUTPUT"]).touch()

    assert BASH is not None
    return subprocess.run(
        [BASH, "-c", step_of(action, step_id)["run"]],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )


def run_render_step(
    action: dict[str, Any], values: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    return run_step(action, "render", values, tmp_path)


def outputs_of(tmp_path: Path) -> dict[str, str]:
    text = (tmp_path / "github-output").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if line)


def test_the_render_action_is_a_composite_action(render_action: dict[str, Any]) -> None:
    assert render_action["runs"]["using"] == "composite"
    assert render_action["name"] and render_action["description"]
    for step in render_action["runs"]["steps"]:
        assert step["shell"] == "bash" or "uses" in step


def test_the_render_action_declares_the_documented_inputs_and_outputs(
    render_action: dict[str, Any],
) -> None:
    assert set(render_action["inputs"]) == EXPECTED_RENDER_INPUTS
    assert set(render_action["outputs"]) == EXPECTED_RENDER_OUTPUTS
    for name, spec in render_action["inputs"].items():
        assert spec["description"].strip(), f"input {name} is undocumented"
        assert "default" in spec, f"input {name} has no default, so it is effectively required"


def test_the_render_action_defaults_agree_with_the_cli(render_action: dict[str, Any]) -> None:
    inputs = render_action["inputs"]
    assert inputs["format"]["default"] in RENDER_FORMATS
    assert inputs["inventory"]["default"] == "."
    # Booleans are strings in the Actions expression language; anything else
    # would silently compare unequal to 'true' in the step's shell.
    for name in ("strict", "force", "install"):
        assert inputs[name]["default"] in {"true", "false"}, name
    assert inputs["graphviz"]["default"] == "auto"


def test_the_render_action_knows_every_format_the_cli_has(render_action: dict[str, Any]) -> None:
    """A typo in ``format`` must fail loudly, not fall through to a default."""
    script = step_of(render_action, "render")["run"]
    assert "|".join(RENDER_FORMATS) in script, "the guard is not the CLI's format list"
    # And every one of them is checked for the shape it should have; a format
    # with no arm would be published on the strength of its byte count alone.
    checked = set(re.findall(r'^\s*(\w+)\) expect="', script, re.MULTILINE))
    assert checked == set(RENDER_FORMATS)


def test_the_render_action_derives_the_suffix_the_renderer_would(
    render_action: dict[str, Any],
) -> None:
    """The default output path is ``netgraph`` plus the format's own extension."""
    script = step_of(render_action, "render")["run"]
    odd = {name for name in RENDER_FORMATS if suffix_for(name) != f".{name}"}
    assert odd == {"mermaid"}, (
        f"{sorted(odd)} no longer take their own name as an extension, so the "
        "action's single special case is not enough"
    )
    assert 'mermaid) extension="mmd" ;;' in script


def test_the_render_action_installs_graphviz_for_exactly_the_formats_that_need_it(
    render_action: dict[str, Any],
) -> None:
    """A format netgraph writes by itself must not pay for the install.

    The text formats are emitted by netgraph directly — except ``html``, which
    embeds an SVG that Graphviz laid out, and so needs ``dot`` as much as
    ``svg`` does.
    """
    script = step_of(render_action, "graphviz")["run"]
    needs_dot = set(RENDER_FORMATS) - (set(TEXT_FORMATS) - {"html"})
    guard = re.search(r"^\s*([\w|]+)\) needs_dot=true ;;", script, re.MULTILINE)
    assert guard is not None, "the guard is no longer a single case arm"
    assert set(guard.group(1).split("|")) == needs_dot


@requires_bash
def test_the_graphviz_step_installs_nothing_for_a_format_that_needs_no_layout(
    render_action: dict[str, Any], tmp_path: Path
) -> None:
    """Run rather than read: the branch that decides not to spend a minute."""
    result = run_step(render_action, "graphviz", {"format": "mermaid"}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "not installing Graphviz" in result.stdout


@requires_bash
@requires_dot
def test_the_graphviz_step_leaves_a_runner_that_already_has_it_alone(
    render_action: dict[str, Any], tmp_path: Path
) -> None:
    """``auto`` on a self-hosted image that ships Graphviz reaches no package manager."""
    result = run_step(render_action, "graphviz", {"format": "html"}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "apt-get" not in result.stdout and "apt-get" not in result.stderr
    assert "graphviz version" in result.stderr, "dot -V is the proof it found one"


def test_the_render_action_readme_documents_every_input_and_output(
    render_action: dict[str, Any],
) -> None:
    readme = RENDER_ACTION_README.read_text(encoding="utf-8")
    for name in render_action["inputs"]:
        assert f"`{name}`" in readme, f"input {name} is not in the action README"
    for name in render_action["outputs"]:
        assert f"`{name}`" in readme, f"output {name} is not in the action README"


def test_the_render_action_readme_shows_a_usable_snippet() -> None:
    readme = RENDER_ACTION_README.read_text(encoding="utf-8")
    assert "uses: blechschmidt/netgraph/.github/actions/netgraph-render@" in readme
    assert "actions/setup-python" in readme, "the action deliberately installs no interpreter"
    assert "upload-pages-artifact" in readme


@requires_bash
@requires_dot
@pytest.mark.parametrize("format", RENDER_FORMATS)
def test_the_render_step_recognises_what_the_renderer_writes(
    render_action: dict[str, Any], tmp_path: Path, format: str
) -> None:
    """Every shape check has to match the thing it is checking.

    The markers are literals in a shell script — ``<svg``, ``graph netgraph``,
    ``%PDF`` — and the renderers they describe are free to change. Rendering
    each format and letting the step judge its own output is what ties the two
    together; a marker that stopped matching would otherwise turn into a step
    that fails on a diagram that is perfectly good.
    """
    result = run_render_step(
        render_action,
        {"inventory": str(HOME_LAB), "format": format, "output": str(tmp_path / f"out.{format}")},
        tmp_path,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@requires_bash
@requires_dot
def test_the_render_step_writes_the_page_and_reports_where(
    render_action: dict[str, Any], tmp_path: Path
) -> None:
    """The whole contract of the step, run rather than read."""
    output = tmp_path / "site" / "index.html"
    result = run_render_step(
        render_action,
        {
            "inventory": str(HOME_LAB),
            "format": "html",
            "output": str(output),
            "layer": "l1,l2",
            "title": "a title with spaces",
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr

    page = output.read_text(encoding="utf-8")
    assert "<h1>a title with spaces</h1>" in page, "the title was word-split on its way through"
    drawn = re.findall(r'<option value="\d+">(l\d)[^<]*</option>', page)
    assert drawn == ["l1", "l2"], "the layer list did not reach the renderer as two layers"

    assert output.parent.is_dir(), "the render was given a directory that did not exist yet"

    reported = outputs_of(tmp_path)
    # Forward slashes whatever the platform spells them with: the step
    # normalises the path it was given, because the shell splits on '/' alone.
    assert reported["file"] == output.as_posix()
    assert reported["directory"] == output.parent.as_posix()
    assert int(reported["bytes"]) == len(output.read_bytes())


@requires_bash
def test_the_render_step_defaults_the_output_to_the_runner_temp(
    render_action: dict[str, Any], tmp_path: Path
) -> None:
    """And names it with the format's extension, ``.mmd`` included."""
    result = run_render_step(
        render_action, {"inventory": str(HOME_LAB), "format": "mermaid"}, tmp_path
    )
    assert result.returncode == 0, result.stderr

    written = Path(outputs_of(tmp_path)["file"])
    assert written == tmp_path / "runner-temp" / "netgraph.mmd"
    assert written.read_text(encoding="utf-8").startswith("flowchart")


@requires_bash
def test_the_render_step_passes_extra_arguments_through(
    render_action: dict[str, Any], tmp_path: Path
) -> None:
    """``args`` is word-split and handed to the CLI as written."""
    output = tmp_path / "wide.dot"
    result = run_render_step(
        render_action,
        {
            "inventory": str(HOME_LAB),
            "format": "dot",
            "output": str(output),
            "args": "--rankdir lr --no-show-ips",
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr

    dot = output.read_text(encoding="utf-8")
    assert "rankdir=LR" in dot
    assert "192.168" not in dot, "--no-show-ips never reached the renderer"


@requires_bash
@requires_unexpanded_globs
def test_the_render_step_does_not_expand_a_glob_in_its_arguments(
    render_action: dict[str, Any], tmp_path: Path
) -> None:
    """A ``--name 'sw*'`` filter is a filter, not a file listing.

    Run in a directory that happens to hold a file called ``sw-…``, an
    unprotected word split would hand netgraph that filename, which matches no
    element — so the diagram would come back either empty or unfiltered rather
    than wrong in a way anybody notices.
    """
    (tmp_path / "sw-a-file-not-a-device").touch()
    output = tmp_path / "filtered.dot"
    result = run_render_step(
        render_action,
        {
            "inventory": str(HOME_LAB),
            "format": "dot",
            "output": str(output),
            "args": "--name sw*",
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr

    dot = output.read_text(encoding="utf-8")
    assert "sw-home" in dot, "the glob was expanded, so the filter matched nothing"
    assert "pc-desk" not in dot, "nothing was filtered at all"


@requires_bash
def test_the_render_step_refuses_a_format_that_does_not_exist(
    render_action: dict[str, Any], tmp_path: Path
) -> None:
    result = run_render_step(
        render_action, {"inventory": str(HOME_LAB), "format": "svgz"}, tmp_path
    )
    assert result.returncode == 2
    assert "::error::" in result.stdout
    assert "svgz" in result.stdout


@requires_bash
@requires_dot
def test_the_render_step_fails_when_the_file_is_not_the_format_it_asked_for(
    render_action: dict[str, Any], tmp_path: Path
) -> None:
    """The shape check, provoked: a render that writes over a decoy.

    ``--show-config`` makes ``netgraph render`` print its resolved settings and
    exit without drawing, so the file it was told to write is left as it was —
    which is exactly the "exit 0, bytes on disk, no diagram" case the check
    exists for.
    """
    output = tmp_path / "not-a-page.svg"
    output.write_text("this is not an svg\n", encoding="utf-8")
    result = run_render_step(
        render_action,
        {
            "inventory": str(HOME_LAB),
            "format": "svg",
            "output": str(output),
            "args": "--show-config",
        },
        tmp_path,
    )
    assert result.returncode == 1
    assert "does not look like svg" in result.stdout


# --------------------------------------------------------------------------- #
# The reusable workflow
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def pages_workflow() -> dict[str, Any]:
    parsed = load_yaml(PAGES_WORKFLOW)
    assert isinstance(parsed, dict)
    return parsed


def triggers_of(workflow: dict[str, Any]) -> dict[str, Any]:
    """``on:`` is the YAML 1.1 boolean ``True`` once safe_load has had it."""
    return workflow.get("on", workflow.get(True))


#: What a caller may set. Each one is in the table in ``docs/ci.md``.
EXPECTED_PAGES_INPUTS = {
    "runs-on",
    "inventory",
    "layer",
    "title",
    "theme",
    "args",
    "strict",
    "page",
    "python-version",
    "version",
    "ref",
    "graphviz",
    "deploy",
    "environment",
}


def test_the_pages_workflow_is_reusable_and_only_reusable(
    pages_workflow: dict[str, Any],
) -> None:
    """No ``push`` trigger: it publishes a site, and only when asked to.

    A repository that also publishes something else from Pages would otherwise
    find this workflow overwriting it on the next commit — which is precisely
    what would happen here, where ``pages.yml`` maintains the demo site.
    """
    assert list(triggers_of(pages_workflow)) == ["workflow_call"]


def test_the_pages_workflow_declares_the_documented_inputs(
    pages_workflow: dict[str, Any],
) -> None:
    call = triggers_of(pages_workflow)["workflow_call"]
    assert set(call["inputs"]) == EXPECTED_PAGES_INPUTS
    for name, spec in call["inputs"].items():
        assert spec["description"].strip(), f"input {name} is undocumented"
        assert "default" in spec, f"input {name} has no default, so it is effectively required"
        assert spec["type"] in {"string", "boolean"}, name
    assert set(call["outputs"]) == {"page-url"}


def test_the_pages_workflow_defaults_match_the_action(
    pages_workflow: dict[str, Any], render_action: dict[str, Any]
) -> None:
    """The two files describe the same knobs; a default in only one of them drifts."""
    call = triggers_of(pages_workflow)["workflow_call"]["inputs"]
    shared = set(call) & set(render_action["inputs"])
    assert shared >= {"inventory", "layer", "title", "theme", "args", "strict", "graphviz"}
    for name in shared:
        expected = render_action["inputs"][name]["default"]
        actual = call[name]["default"]
        # ``strict`` is a real boolean in a workflow input and a string in an
        # action input; everything else compares directly.
        assert str(actual).lower() == str(expected).lower(), name


def test_every_job_of_the_pages_workflow_honours_the_runs_on_input(
    pages_workflow: dict[str, Any],
) -> None:
    """The whole point of the input is that neither job is pinned to a runner."""
    jobs = pages_workflow["jobs"]
    assert set(jobs) == {"build", "deploy"}
    for name, job in jobs.items():
        runs_on = job["runs-on"]
        assert "inputs['runs-on']" in runs_on, f"{name} does not use the runs-on input"
        # A JSON array or object has to survive as one, or a label set and a
        # runner group would both arrive as a single nonsense label.
        assert "fromJSON" in runs_on, f"{name} cannot take a JSON array of labels"


def test_only_the_deploy_job_of_the_pages_workflow_can_write(
    pages_workflow: dict[str, Any],
) -> None:
    """A render that fails cannot have reached the Pages API to publish anything."""
    jobs = pages_workflow["jobs"]
    assert "permissions" not in jobs["build"], "the build job inherits the read-only default"
    assert pages_workflow["permissions"] == {"contents": "read"}
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["deploy"]["if"] == "inputs.deploy"
    assert jobs["deploy"]["needs"] == "build"
    assert jobs["deploy"]["environment"]["name"] == "${{ inputs.environment }}"


def test_the_pages_workflow_renders_through_the_action(pages_workflow: dict[str, Any]) -> None:
    """One renderer, not two: the page is whatever ``netgraph render`` writes."""
    steps = pages_workflow["jobs"]["build"]["steps"]
    render = next(step for step in steps if "netgraph-render" in step.get("uses", ""))
    assert render["with"]["format"] == "html", "only the self-contained page can be published as-is"
    for name in ("inventory", "layer", "title", "theme", "args", "strict", "graphviz", "version"):
        assert f"inputs.{name}" in str(render["with"][name]), f"{name} never reaches the action"

    uploaded = next(step for step in steps if "upload-pages-artifact" in step.get("uses", ""))
    assert render["with"]["output"].startswith(uploaded["with"]["path"]), (
        "the page is written somewhere other than the directory that gets uploaded"
    )


def test_the_pages_workflow_renders_with_the_action_that_belongs_to_it(
    pages_workflow: dict[str, Any],
) -> None:
    """The action is netgraph's own, at the commit the caller pinned the workflow at.

    ``uses:`` takes no expression, so an action referenced by name would have to
    name a fixed ref — and a workflow called ``@v0.1.0`` that then reached for
    the action on ``main`` would be pinned in name only. Checking netgraph out
    at ``github.job_workflow_sha`` and using the local path is what keeps the
    two halves the same release.
    """
    steps = pages_workflow["jobs"]["build"]["steps"]
    checkout = next(
        step for step in steps if step.get("with", {}).get("repository") == "blechschmidt/netgraph"
    )
    assert checkout["with"]["ref"] == "${{ github.job_workflow_sha }}"
    assert checkout["with"]["persist-credentials"] is False

    render = next(step for step in steps if "netgraph-render" in step["uses"])
    # Derived from where the action actually lives, so moving the directory
    # fails here rather than in somebody's deployment.
    inside = RENDER_ACTION_DIR.relative_to(REPO_ROOT).as_posix()
    assert render["uses"] == f"./{checkout['with']['path']}/{inside}"
    assert steps.index(checkout) < steps.index(render), "the action is used before it is fetched"


# --------------------------------------------------------------------------- #
# This repository eats its own cooking
# --------------------------------------------------------------------------- #


def test_ci_renders_an_example_through_the_render_action() -> None:
    """The published-diagram path, exercised without publishing a second site."""
    workflow = load_yaml(WORKFLOW)
    steps = workflow["jobs"]["render-examples"]["steps"]
    used = [step.get("uses", "") for step in steps]
    assert used.count("./.github/actions/netgraph-render") == 2, (
        "the Graphviz path and a format that needs no Graphviz are both meant to be exercised"
    )
    formats = {
        step.get("with", {}).get("format", "html")
        for step in steps
        if step.get("uses") == "./.github/actions/netgraph-render"
    }
    assert formats == {"html", "mermaid"}


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
    for section in (
        "pre-commit",
        "The GitHub Action",
        "The JSON envelope",
        "The render action",
        "Workflow: publish the diagram to GitHub Pages",
    ):
        assert section in text


def test_the_ci_documentation_covers_every_input_of_the_reusable_workflow(
    pages_workflow: dict[str, Any],
) -> None:
    """The workflow has no README of its own; this page is where a caller looks."""
    text = CI_DOC.read_text(encoding="utf-8")
    for name in triggers_of(pages_workflow)["workflow_call"]["inputs"]:
        assert f"`{name}`" in text, f"docs/ci.md never mentions the {name} input"
    for name in RENDER_FORMATS:
        assert f"`{name}`" in text


# --------------------------------------------------------------------------- #
# The review action
# --------------------------------------------------------------------------- #


#: The third promise: what the README and ``docs/ci.md`` say a workflow may set
#: on ``netgraph-review``.
EXPECTED_REVIEW_INPUTS = {
    "inventory",
    "base",
    "head",
    "head-sha",
    "title",
    "layer",
    "theme",
    "diagram-args",
    "strict",
    "disable",
    "formats",
    "artifact-name",
    "artifact-url",
    "diagram-url",
    "output-directory",
    "fail-on-new-errors",
    "graphviz",
    "install",
    "version",
}
EXPECTED_REVIEW_OUTPUTS = {
    "comment",
    "plan",
    "sarif",
    "directory",
    "diagrams",
    "verdict",
    "changed",
    "new-errors",
    "new-findings",
    "failed",
}


@pytest.fixture(scope="module")
def review_action() -> dict[str, Any]:
    parsed = load_yaml(REVIEW_ACTION_FILE)
    assert isinstance(parsed, dict)
    return parsed


def test_the_review_action_is_a_composite_action(review_action: dict[str, Any]) -> None:
    assert review_action["runs"]["using"] == "composite"
    assert review_action["name"] and review_action["description"]
    for step in review_action["runs"]["steps"]:
        assert step["shell"] == "bash" or "uses" in step


def test_the_review_action_declares_the_documented_inputs_and_outputs(
    review_action: dict[str, Any],
) -> None:
    assert set(review_action["inputs"]) == EXPECTED_REVIEW_INPUTS
    assert set(review_action["outputs"]) == EXPECTED_REVIEW_OUTPUTS
    for name, spec in review_action["inputs"].items():
        assert spec["description"].strip(), f"input {name} is undocumented"
        assert "default" in spec, f"input {name} has no default, so the harness cannot build one"
    assert review_action["inputs"]["base"]["required"] is True, "a review has no default baseline"


def test_the_review_action_defaults_agree_with_the_cli(review_action: dict[str, Any]) -> None:
    inputs = review_action["inputs"]
    assert inputs["inventory"]["default"] == "."
    # Booleans are strings in the Actions expression language; anything else
    # would silently compare unequal to 'true' in the step's shell.
    for name in ("strict", "install", "fail-on-new-errors"):
        assert inputs[name]["default"] in {"true", "false"}, name
    assert inputs["graphviz"]["default"] == "auto"
    # The default drawing is what a review wants, and both formats must exist.
    for name in str(inputs["formats"]["default"]).split(","):
        assert name in RENDER_FORMATS, name
        assert supports_diff(name), f"{name} cannot say what changed"


def test_the_review_action_offers_only_formats_that_can_draw_a_diff(
    review_action: dict[str, Any],
) -> None:
    """A format with no overlay would produce a picture that says nothing changed."""
    script = step_of(review_action, "review")["run"]
    guard = re.search(r"^\s*([\w|]+)\) ;;", script, re.MULTILINE)
    assert guard is not None, "the format guard is no longer a single case arm"
    assert set(guard.group(1).split("|")) == set(diff_formats())


def test_the_review_action_never_fails_before_it_has_written_the_review(
    review_action: dict[str, Any],
) -> None:
    """The one run with something to report must not be the one that reports nothing."""
    script = step_of(review_action, "review")["run"]
    assert "--fail-on never" in script, "netgraph must not exit before the outputs are set"
    assert script.index("GITHUB_OUTPUT") < script.index('NETGRAPH_FAIL}" = "true"')


def test_the_review_action_reads_the_summary_rather_than_the_prose(
    review_action: dict[str, Any],
) -> None:
    """A step that grepped the comment would break on the first rewording."""
    script = step_of(review_action, "review")["run"]
    assert "--summary-out" in script
    assert "summary.json" in script
    for key in ("verdict", "changed", "new-errors", "new-findings", "failed"):
        assert f'"{key}"' in script, f"{key} is never derived from the summary document"


@pytest.mark.parametrize(
    "path",
    [ACTION_FILE, RENDER_ACTION_FILE, REVIEW_ACTION_FILE],
    ids=lambda path: path.parent.name,
)
def test_no_input_description_holds_an_expression(path: Path) -> None:
    """``${{ ... }}`` in a description is evaluated, and fails the whole action.

    An action file's descriptions go through the same template evaluator as its
    steps, and ``github`` is not a context an action may read — so a description
    that quotes ``github.event.pull_request.base.sha`` the way a workflow would
    write it stops the action loading, with an error naming a line nobody
    thought was code. Found on a real runner; kept here so it is found here.
    """
    action = load_yaml(path)
    for section in ("inputs", "outputs"):
        for name, spec in action.get(section, {}).items():
            assert "${{" not in spec["description"], f"{path.parent.name}: {section}.{name}"


def test_the_review_action_readme_documents_every_input_and_output(
    review_action: dict[str, Any],
) -> None:
    readme = REVIEW_ACTION_README.read_text(encoding="utf-8")
    for name in review_action["inputs"]:
        assert f"`{name}`" in readme, f"input {name} is not in the action README"
    for name in review_action["outputs"]:
        assert f"`{name}`" in readme, f"output {name} is not in the action README"


def test_the_review_action_readme_shows_a_usable_snippet() -> None:
    readme = REVIEW_ACTION_README.read_text(encoding="utf-8")
    assert "uses: blechschmidt/netgraph/.github/actions/netgraph-review@" in readme
    assert "actions/setup-python" in readme, "the action deliberately installs no interpreter"
    assert "github/codeql-action/upload-sarif" in readme
    assert "pull_request_target" in readme, "the trigger it must not be run under goes unsaid"


def build_repository(root: Path) -> None:
    """A one-commit repository with the home lab in ``inventory/``."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(HOME_LAB, root / "inventory", dirs_exist_ok=True)
    for arguments in (
        ("init", "-q", "."),
        ("config", "user.email", "t@example.invalid"),
        ("config", "user.name", "Tester"),
        ("config", "commit.gpgsign", "false"),
        ("add", "-A"),
        ("commit", "-qm", "Bring the home lab under description"),
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def fetch_step(action: dict[str, Any], values: dict[str, str], tmp_path: Path) -> Any:
    """The step that makes sure the base commit is in this clone."""
    return run_step(action, "fetch", values, tmp_path)


@requires_bash
def test_a_review_with_no_base_is_refused_before_anything_is_loaded(
    review_action: dict[str, Any], tmp_path: Path
) -> None:
    """Guessing the other side of a comparison is worse than refusing."""
    result = fetch_step(review_action, {"base": ""}, tmp_path)
    assert result.returncode == 2
    assert "base is required" in result.stdout


@requires_bash
def test_a_base_that_is_a_folder_needs_no_git_at_all(
    review_action: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "yesterday").mkdir()
    result = fetch_step(review_action, {"base": "yesterday"}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "nothing to fetch" in result.stdout


@requires_bash
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_base_already_in_the_clone_is_not_fetched_again(
    review_action: dict[str, Any], tmp_path: Path
) -> None:
    build_repository(tmp_path)
    result = fetch_step(review_action, {"base": "HEAD"}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "already in this clone" in result.stdout


@requires_bash
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_base_that_is_not_in_the_clone_is_fetched_and_made_resolvable(
    review_action: dict[str, Any], tmp_path: Path
) -> None:
    """The shape of a pull-request checkout: one branch, one commit deep.

    A fetch by *name* leaves ``FETCH_HEAD`` and no ref called ``main``, so a
    step that stopped at "the fetch succeeded" would hand netgraph a ref git
    cannot read. This is that case, and it failed on a real runner before the
    step gave the commit the name it was asked for.
    """
    origin = tmp_path / "origin"
    build_repository(origin)
    # Named here rather than left to init.defaultBranch, which differs between
    # machines and would make this test pass or fail by accident.
    subprocess.run(["git", "branch", "-qM", "main"], cwd=origin, check=True)
    subprocess.run(["git", "branch", "-q", "topic"], cwd=origin, check=True)
    (origin / "inventory" / "README.md").write_text("changed on main\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "Move main on"], cwd=origin, check=True)

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", "topic", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "main"], cwd=clone, capture_output=True
        ).returncode
        != 0
    ), "the clone is meant to start without the base"

    result = run_step(review_action, "fetch", {"base": "main"}, clone)
    assert result.returncode == 0, result.stderr
    resolved = subprocess.run(
        ["git", "rev-parse", "main^{commit}"], cwd=clone, capture_output=True, text=True
    )
    assert resolved.returncode == 0, "the base was fetched and still names no commit"


@requires_bash
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_review_step_writes_the_bundle_and_reports_the_verdict(
    review_action: dict[str, Any], tmp_path: Path
) -> None:
    """Run the shell, not read it: quoting and word splitting are not schema.

    ``dot`` rather than ``svg`` so the step needs no Graphviz — the format is
    the one thing being held constant here; what is under test is the plumbing.
    """
    build_repository(tmp_path)
    (tmp_path / "inventory" / "hosts" / "extra.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata:\n  name: pc-extra\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n      enabled: false\n",
        encoding="utf-8",
    )

    result = run_step(
        review_action,
        "review",
        {
            "inventory": "inventory",
            "base": "HEAD",
            "formats": "dot",
            "fail-on-new-errors": "false",
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr

    outputs = outputs_of(tmp_path)
    assert outputs["changed"] == "true"
    assert outputs["verdict"] in {"passed", "warned"}
    assert outputs["new-errors"] == "0"
    assert outputs["failed"] == "false"

    directory = Path(outputs["directory"])
    assert (
        (directory / "comment.md").read_text(encoding="utf-8").startswith("<!-- netgraph-review:")
    )
    assert json.loads((directory / "summary.json").read_text(encoding="utf-8"))["changed"] is True
    assert (
        json.loads((directory / "plan.json").read_text(encoding="utf-8"))["summary"]["total"] == 1
    )
    assert (directory / "netgraph.sarif").is_file()
    assert outputs["diagrams"].endswith("diff.dot")


@requires_bash
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_review_step_fails_only_when_the_caller_asked_and_a_new_error_exists(
    review_action: dict[str, Any], tmp_path: Path
) -> None:
    build_repository(tmp_path)
    (tmp_path / "inventory" / "cables" / "extra.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata:\n  name: cbl-nowhere\n"
        "spec:\n  endpoints:\n    - rtr-home:lan0\n    - nowhere:eth0\n  medium: copper\n",
        encoding="utf-8",
    )
    values = {"inventory": "inventory", "base": "HEAD", "formats": ""}

    tolerant = run_step(
        review_action, "review", {**values, "fail-on-new-errors": "false"}, tmp_path
    )
    assert tolerant.returncode == 0, tolerant.stderr
    assert outputs_of(tmp_path)["failed"] == "true"

    (tmp_path / "github-output").write_text("", encoding="utf-8")
    strictly = run_step(review_action, "review", {**values, "fail-on-new-errors": "true"}, tmp_path)
    assert strictly.returncode == 1
    assert "errors the base did not have" in strictly.stdout


# --------------------------------------------------------------------------- #
# The review workflow
# --------------------------------------------------------------------------- #


#: What a caller may set. Each one is in the table in ``docs/ci.md``.
EXPECTED_REVIEW_WORKFLOW_INPUTS = {
    "runs-on",
    "inventory",
    "base",
    "title",
    "layer",
    "theme",
    "args",
    "formats",
    "strict",
    "disable",
    "comment",
    "upload-sarif",
    "sarif-category",
    "fail-on-new-errors",
    "artifact-name",
    "artifact-retention-days",
    "python-version",
    "version",
    "graphviz",
}


@pytest.fixture(scope="module")
def review_workflow() -> dict[str, Any]:
    parsed = load_yaml(REVIEW_WORKFLOW)
    assert isinstance(parsed, dict)
    return parsed


def steps_of(workflow: dict[str, Any], job: str = "review") -> list[dict[str, Any]]:
    return workflow["jobs"][job]["steps"]


def test_the_review_workflow_is_reusable_and_only_reusable(
    review_workflow: dict[str, Any],
) -> None:
    """It posts comments; it does so when a caller asks and not on its own schedule."""
    assert list(triggers_of(review_workflow)) == ["workflow_call"]


def test_the_review_workflow_declares_the_documented_inputs(
    review_workflow: dict[str, Any],
) -> None:
    call = triggers_of(review_workflow)["workflow_call"]
    assert set(call["inputs"]) == EXPECTED_REVIEW_WORKFLOW_INPUTS
    for name, spec in call["inputs"].items():
        assert spec["description"].strip(), f"input {name} is undocumented"
        assert "default" in spec, f"input {name} has no default, so it is effectively required"
        assert spec["type"] in {"string", "boolean", "number"}, name
    assert set(call["outputs"]) == {"verdict", "changed", "new-errors", "comment-url"}


def test_the_review_workflow_defaults_match_the_action(
    review_workflow: dict[str, Any], review_action: dict[str, Any]
) -> None:
    """The two files describe the same knobs; a default in only one of them drifts."""
    call = triggers_of(review_workflow)["workflow_call"]["inputs"]
    shared = set(call) & set(review_action["inputs"])
    assert shared >= {"inventory", "title", "layer", "theme", "formats", "strict", "graphviz"}
    for name in shared:
        if name == "fail-on-new-errors":
            # Deliberately different: the action reports and the workflow gates,
            # so that the comment is published before anything goes red.
            continue
        expected = review_action["inputs"][name]["default"]
        assert str(call[name]["default"]).lower() == str(expected).lower(), name


def test_the_review_job_honours_the_runs_on_input(review_workflow: dict[str, Any]) -> None:
    runs_on = review_workflow["jobs"]["review"]["runs-on"]
    assert "inputs['runs-on']" in runs_on
    assert "fromJSON" in runs_on, "a JSON array of labels has to survive as one"


def test_the_review_workflow_asks_for_exactly_the_three_permissions(
    review_workflow: dict[str, Any],
) -> None:
    """``docs/ci.md`` names these three; a fourth would be a new thing to justify."""
    assert review_workflow["permissions"] == {"contents": "read"}
    assert review_workflow["jobs"]["review"]["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "security-events": "write",
    }


def test_the_review_workflow_reviews_with_the_action_that_belongs_to_it(
    review_workflow: dict[str, Any],
) -> None:
    """Pinned in fact and not only in name; the same reasoning as the pages workflow."""
    steps = steps_of(review_workflow)
    checkout = next(
        step for step in steps if step.get("with", {}).get("repository") == "blechschmidt/netgraph"
    )
    assert checkout["with"]["ref"] == "${{ github.job_workflow_sha }}"
    assert checkout["with"]["persist-credentials"] is False

    review = next(step for step in steps if "netgraph-review" in step.get("uses", ""))
    inside = REVIEW_ACTION_DIR.relative_to(REPO_ROOT).as_posix()
    assert review["uses"] == f"./{checkout['with']['path']}/{inside}"
    assert steps.index(checkout) < steps.index(review)


def test_every_input_of_the_review_workflow_reaches_the_action_or_a_step(
    review_workflow: dict[str, Any],
) -> None:
    """An input nothing reads is a promise the workflow does not keep."""
    body = REVIEW_WORKFLOW.read_text(encoding="utf-8")
    for name in triggers_of(review_workflow)["workflow_call"]["inputs"]:
        assert body.count(f"inputs.{name}") + body.count(f"inputs['{name}']") >= 1, (
            f"the {name} input is declared and never used"
        )


def test_the_review_workflow_publishes_before_it_fails(review_workflow: dict[str, Any]) -> None:
    """Every artefact is out before anything goes red, and the gate is last."""
    steps = steps_of(review_workflow)
    names = [step.get("name", "") for step in steps]
    review = next(step for step in steps if "netgraph-review" in step.get("uses", ""))
    assert review["with"]["fail-on-new-errors"] == "false", "the action must only report"

    gate = names.index("Fail on errors this change introduced")
    assert gate == len(steps) - 1, "the gate is not the last step"
    for published in ("Upload the review bundle", "Write the review to the job summary"):
        assert names.index(published) < gate
    assert steps[gate]["if"] == (
        "inputs.fail-on-new-errors && steps.review.outputs.failed == 'true'"
    )


def test_the_job_summary_is_written_unconditionally(review_workflow: dict[str, Any]) -> None:
    """It needs no permission, so it is the one place a fork's review appears."""
    step = next(
        entry
        for entry in steps_of(review_workflow)
        if entry.get("name", "").endswith("job summary")
    )
    assert "if" not in step
    assert "GITHUB_STEP_SUMMARY" in step["run"]


@pytest.mark.parametrize("name", ["Upload the SARIF report", "Post the review comment"])
def test_the_writing_steps_are_skipped_on_a_fork(
    review_workflow: dict[str, Any], name: str
) -> None:
    """A fork's ``pull_request`` token is read-only whatever ``permissions`` says."""
    step = next(entry for entry in steps_of(review_workflow) if entry.get("name") == name)
    condition = " ".join(step["if"].split())
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition


def test_the_comment_is_sticky_and_finds_itself_by_the_marker(
    review_workflow: dict[str, Any],
) -> None:
    """One comment per pull request per title, edited rather than appended to."""
    step = next(
        entry
        for entry in steps_of(review_workflow)
        if entry.get("name") == "Post the review comment"
    )
    script = step["run"]
    assert MARKER_PREFIX in script, "the marker the body carries is not the one searched for"
    assert "--method PATCH" in script and "--method POST" in script
    assert script.index("--method PATCH") < script.index("--method POST"), (
        "editing has to be tried before posting, or every push adds a comment"
    )


def test_the_review_workflow_documents_the_trigger_it_must_not_be_run_under() -> None:
    """The one thing a reader has to be told before copying this file."""
    body = REVIEW_WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request_target" in body
    assert "read-only" in body


# --------------------------------------------------------------------------- #
# This repository reviews its own inventories
# --------------------------------------------------------------------------- #


def test_the_dogfood_workflow_calls_the_reusable_one(review_workflow: dict[str, Any]) -> None:
    workflow = load_yaml(DOGFOOD_WORKFLOW)
    job = workflow["jobs"]["review"]
    assert job["uses"] == f"./{REVIEW_WORKFLOW.relative_to(REPO_ROOT).as_posix()}"
    assert job["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "security-events": "write",
    }
    for name in job["with"]:
        assert name in triggers_of(review_workflow)["workflow_call"]["inputs"], name


def test_the_dogfood_workflow_can_be_run_without_a_pull_request() -> None:
    """The path a fork's pull request takes, exercised deliberately.

    A manual run has no pull request to take a base from and none to comment
    on, so it names its own base and lands in the job summary — which is the
    same code path, and the same output, a fork gets.
    """
    workflow = load_yaml(DOGFOOD_WORKFLOW)
    dispatch = triggers_of(workflow)["workflow_dispatch"]
    assert "base" in dispatch["inputs"]
    assert "inputs.base" in str(workflow["jobs"]["review"]["with"]["base"])


def test_the_dogfood_workflow_reviews_every_example() -> None:
    """Discovered, not listed, so a new example cannot be forgotten."""
    workflow = load_yaml(DOGFOOD_WORKFLOW)
    matrix = workflow["jobs"]["review"]["strategy"]["matrix"]["inventory"]
    assert "needs.discover.outputs.inventories" in matrix

    triggers = triggers_of(workflow)
    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    assert "examples/**" in triggers["pull_request"]["paths"]


def test_each_reviewed_inventory_keeps_its_own_comment_and_alerts() -> None:
    """Two inventories sharing a title or a category would overwrite each other."""
    settings = load_yaml(DOGFOOD_WORKFLOW)["jobs"]["review"]["with"]
    for name in ("title", "sarif-category", "artifact-name"):
        assert "matrix.inventory" in str(settings[name]), f"{name} is not per inventory"


def test_the_ci_documentation_covers_every_input_of_the_review_workflow(
    review_workflow: dict[str, Any],
) -> None:
    """The workflow has no README of its own; this page is where a caller looks."""
    text = CI_DOC.read_text(encoding="utf-8")
    for name in triggers_of(review_workflow)["workflow_call"]["inputs"]:
        assert f"`{name}`" in text, f"docs/ci.md never mentions the {name} input"
    for section in ("The review action", "Workflow: review a pull request"):
        assert section in text
    assert "pull_request_target" in text, "the trigger the workflow refuses goes unexplained"
