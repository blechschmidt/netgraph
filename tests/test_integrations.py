"""The CI integrations this repository ships to other repositories.

``.pre-commit-hooks.yaml``, the two composite actions under ``.github/actions/``
and the reusable workflow ``.github/workflows/netgraph-pages.yml`` are consumed
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
from netgraph.render import TEXT_FORMATS, suffix_for

from platform_marks import (  # isort: skip -- tests/ is on sys.path, not a package
    ON_WINDOWS,
    requires_dot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_FILE = REPO_ROOT / ".pre-commit-hooks.yaml"
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "netgraph-validate"
ACTION_FILE = ACTION_DIR / "action.yml"
ACTION_README = ACTION_DIR / "README.md"
RENDER_ACTION_DIR = REPO_ROOT / ".github" / "actions" / "netgraph-render"
RENDER_ACTION_FILE = RENDER_ACTION_DIR / "action.yml"
RENDER_ACTION_README = RENDER_ACTION_DIR / "README.md"
PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "netgraph-pages.yml"
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
        if matched is None:
            # A constant, such as MSYS=noglob. It has to be set here too, or the
            # step is run in an environment GitHub would not have given it.
            assert "${{" not in expression, f"{variable} is neither an input nor a constant"
            environment[variable] = expression
            continue
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
@pytest.mark.skipif(
    ON_WINDOWS,
    reason=(
        "the MSYS runtime Git Bash is built on expands wildcards in the arguments it hands "
        "to a native program, after the shell has finished with them; 'set -f' cannot reach "
        "that, and the step sets MSYS=noglob instead"
    ),
)
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
