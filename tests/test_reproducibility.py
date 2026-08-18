"""Every environment this repository builds comes out of ``uv.lock``.

A dependency range in ``pyproject.toml`` says what netviz *can* work with; it
says nothing about what any particular run actually installed. That gap is where
a green CI run turns red overnight with no commit in between, where a nightly
property failure cannot be reproduced because the versions that found it are
gone, and where a published wheel carries metadata a year-old twine refuses to
read — the last of which cost v0.0.1 three release attempts.

``uv.lock`` closes the gap by naming exact versions, and it only closes it while
every consumer of it actually reads it. That is what this module asserts:

* the lockfile exists, agrees with ``pyproject.toml``, and covers the whole of
  the declared dependency surface — the runtime list, all three extras and both
  dependency groups;
* every workflow step that builds an environment does so with ``--locked`` or
  ``--frozen``, not with a plain ``uv sync`` that would relock in silence;
* the two places that deliberately do *not* use the lockfile — the composite
  actions, which install into other people's repositories, and the ``verify``
  job, whose whole purpose is to resolve the published artefact's ranges from
  scratch — say so in prose, so the omission stays a decision rather than
  becoming an oversight;
* the container image and the pre-commit hooks agree with the lockfile too,
  since an image built from unpinned dependencies and a formatter one patch
  release ahead of CI are the same class of problem.

The commands themselves are matched as text. A workflow is not importable and
GitHub will not tell us what it would have run, so the string in the file is the
only artefact there is; the tests read it the way a reviewer would.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# The TOML parser netviz itself uses. Taken from there rather than imported
# directly because ``tomllib`` only entered the standard library in 3.11 and this
# package supports 3.10, where the dependency is ``tomli`` -- and the matrix
# entry that would trip over a bare ``import tomllib`` is precisely the one this
# module is about.
from netviz.config import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "uv.lock"
PYTHON_VERSION_FILE = REPO_ROOT / ".python-version"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTION_DIR = REPO_ROOT / ".github" / "actions"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# The workflows whose environments are ours to pin. ``netviz-pages.yml`` and
# ``netviz-review.yml`` are reusable workflows for other repositories and
# ``review.yml`` calls one of them, so their install path is the composite
# actions' and is covered by the tests further down instead.
OWN_WORKFLOWS = ("ci.yml", "nightly.yml", "pages.yml", "pypi.yaml")

ACTIONS = ("netviz-validate", "netviz-render", "netviz-review")

# ``astral-sh/setup-uv``, pinned to a commit rather than to a tag. A tag is
# mutable and this action installs a binary that then resolves the whole
# dependency closure, so an unpinned one would be the single largest hole in
# everything above.
SETUP_UV = "astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d"


def _uv() -> str:
    """The uv binary, or a skip naming what to install."""
    found = shutil.which("uv")
    if found is None:  # pragma: no cover -- CI always has it
        pytest.skip(
            "uv is not installed; see https://docs.astral.sh/uv/ or docs/getting-started.md"
        )
    return found


def _toml(path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    return parsed


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    return _toml(PYPROJECT)


@pytest.fixture(scope="module")
def lock() -> dict[str, Any]:
    return _toml(LOCKFILE)


@pytest.fixture(scope="module")
def locked_versions(lock: dict[str, Any]) -> dict[str, str]:
    return {package["name"]: package["version"] for package in lock["package"]}


def _steps(workflow: Path) -> list[dict[str, Any]]:
    """Every step of every job in a workflow, flattened."""
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return [step for job in document.get("jobs", {}).values() for step in (job.get("steps") or [])]


def _run_lines(workflow: Path) -> list[str]:
    """Every line of every ``run:`` block in a workflow, stripped of comments."""
    return [
        stripped
        for step in _steps(workflow)
        for line in str(step.get("run", "")).splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]


# --------------------------------------------------------------------------- #
# The lockfile itself
# --------------------------------------------------------------------------- #


def test_the_lockfile_is_committed() -> None:
    """Without this file none of the ``--locked`` flags below mean anything."""
    assert LOCKFILE.is_file(), "uv.lock is missing; run 'uv lock' and commit it"
    assert LOCKFILE.stat().st_size > 0


def test_the_lockfile_agrees_with_pyproject() -> None:
    """The check every job in CI performs, performed once here as well.

    ``uv lock --check`` re-resolves nothing: it compares the requirements
    recorded in the lockfile against the ones declared in ``pyproject.toml`` and
    exits non-zero when they differ. So this needs no network and answers the
    only question that matters — did whoever edited the dependencies re-lock.
    """
    result = subprocess.run(
        [_uv(), "lock", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "uv.lock is out of date with pyproject.toml; run 'uv lock' and commit the result\n"
        f"{result.stdout}{result.stderr}"
    )


def test_the_lockfile_covers_every_declared_dependency(
    pyproject: dict[str, Any], locked_versions: dict[str, str]
) -> None:
    """Runtime, all three extras and both groups — not just what ``uv sync`` uses.

    A lockfile that resolved only the default set would leave the browser and
    site jobs resolving at install time, which is exactly the drift this is
    here to stop.
    """
    project = pyproject["project"]
    declared: set[str] = set()
    for requirement in project["dependencies"]:
        declared.add(_distribution_name(requirement))
    for extra in project["optional-dependencies"].values():
        declared.update(_distribution_name(requirement) for requirement in extra)
    for group in pyproject["dependency-groups"].values():
        declared.update(_distribution_name(requirement) for requirement in group)

    missing = sorted(declared - set(locked_versions))
    assert not missing, f"declared but not in uv.lock: {missing}"


def _distribution_name(requirement: str) -> str:
    """``ruamel.yaml>=0.18`` -> ``ruamel-yaml``, the name uv.lock records."""
    name = re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def test_the_lockfile_spans_the_supported_interpreters(
    pyproject: dict[str, Any], lock: dict[str, Any]
) -> None:
    """A universal lock, not one resolved for whatever ran ``uv lock``.

    The test matrix runs 3.10, 3.11 and 3.12 from this one file, and the
    classifiers promise 3.13 as well. A lockfile with a narrower floor would
    install nothing on the oldest entry.
    """
    assert lock["requires-python"] == pyproject["project"]["requires-python"]


def test_the_uv_version_is_pinned_and_is_the_one_running(
    pyproject: dict[str, Any],
) -> None:
    """One resolver, named in one place.

    ``astral-sh/setup-uv`` reads this key when a workflow gives it no explicit
    ``version:``, so every job installs the uv named here; uv itself refuses to
    run against a project that names a different one. Together that means a
    contributor's shell and the CI runner cannot read this lockfile with two
    different resolvers.
    """
    required = pyproject["tool"]["uv"]["required-version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", required), (
        f"required-version is {required!r}; an exact version is the point of pinning it"
    )
    reported = subprocess.run(
        [_uv(), "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert required in reported, f"uv reports {reported.strip()!r}, pyproject pins {required}"


def test_the_interpreter_is_pinned_too(pyproject: dict[str, Any]) -> None:
    """Pinned packages on an unpinned interpreter is half a reproduction.

    ``.python-version`` is what ``uv sync`` reads when no ``--python`` is given,
    which is the local case; the CI matrix overrides it per entry on purpose.
    """
    pinned = PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+", pinned), pinned
    floor = pyproject["project"]["requires-python"]
    assert floor.startswith(">=")
    major, minor = (int(part) for part in pinned.split("."))
    floor_major, floor_minor = (int(part) for part in floor.removeprefix(">=").split("."))
    assert (major, minor) >= (floor_major, floor_minor), (
        f".python-version pins {pinned}, below the {floor} the package claims"
    )


def test_the_lockfile_ships_in_the_sdist(pyproject: dict[str, Any]) -> None:
    """This module reads it, so an sdist without it ships a test that cannot run."""
    include = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "/uv.lock" in include
    assert "/.python-version" in include


# --------------------------------------------------------------------------- #
# The workflows
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", OWN_WORKFLOWS)
def test_every_sync_is_locked(name: str) -> None:
    """A plain ``uv sync`` relocks in silence; ``--locked`` fails instead.

    That difference is the whole gate. Without the flag a job whose
    ``pyproject.toml`` had drifted from ``uv.lock`` would quietly resolve
    something nobody reviewed and go green, and the lockfile would become a file
    that is committed but never read.

    ``--frozen`` is accepted as well: it is the stronger statement — install the
    lockfile without consulting ``pyproject.toml`` at all — which is what the
    ``lock`` job's resolvability probe wants.
    """
    workflow = WORKFLOW_DIR / name
    syncs = [line for line in _run_lines(workflow) if re.search(r"\buv sync\b", line)]
    assert syncs, f"{name} builds no environment with uv sync"
    for line in syncs:
        assert "--locked" in line or "--frozen" in line, (
            f"{name} runs an unpinned sync, which would relock in silence: {line!r}"
        )


@pytest.mark.parametrize("name", OWN_WORKFLOWS)
def test_no_workflow_installs_the_project_with_pip(name: str) -> None:
    """pip resolves; that is the behaviour being replaced.

    ``pypi.yaml``'s ``verify`` job is the deliberate exception, and it reaches
    for ``uv pip install`` — uv's resolver and installer without uv's lockfile,
    which is exactly the distinction that job turns on. So what is banned here is
    *pip itself*, not the ``pip`` subcommand name.
    """
    workflow = WORKFLOW_DIR / name
    offenders = [
        line
        for line in _run_lines(workflow)
        # The lookbehinds sit immediately before the executable name, so
        # ``uv pip install`` is exempt while ``./env/bin/pip install`` is not.
        if re.search(r"(?<![-\w])(?<!uv )(?<!uvx )(python -m pip|pip3?)\s+install", line)
        # ``echo "pip install netviz==0.0.2"`` in the release summary is a
        # sentence addressed to a reader, not a command this workflow runs --
        # and it is the right sentence: what a user types is still pip.
        and not re.match(r"(echo|printf)\b", line)
    ]
    assert not offenders, f"{name} still installs with pip: {offenders}"


@pytest.mark.parametrize("name", OWN_WORKFLOWS)
def test_uv_is_installed_by_a_commit_pinned_action(name: str) -> None:
    """The action that installs the resolver cannot itself float on a tag."""
    workflow = WORKFLOW_DIR / name
    uses = [
        str(step["uses"]) for step in _steps(workflow) if "setup-uv" in str(step.get("uses", ""))
    ]
    assert uses, f"{name} never sets up uv"
    for reference in uses:
        assert reference == SETUP_UV, (
            f"{name} pins setup-uv to {reference!r}, not to the reviewed commit {SETUP_UV!r}"
        )


def test_no_workflow_sets_up_python_twice() -> None:
    """uv supplies the interpreter as well as the packages.

    Keeping ``actions/setup-python`` alongside it would mean two tools deciding
    which Python a job runs, and the one that loses is invisible until a matrix
    entry tests the wrong version of the interpreter it claims to test.
    """
    for name in OWN_WORKFLOWS:
        workflow = WORKFLOW_DIR / name
        offenders = [
            str(step.get("uses"))
            for step in _steps(workflow)
            if "actions/setup-python" in str(step.get("uses", ""))
        ]
        assert not offenders, f"{name} still uses actions/setup-python: {offenders}"


def test_ci_fails_fast_on_a_stale_lockfile() -> None:
    """One job, first, saying in seconds what six others would say slowly."""
    document = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    job = document["jobs"]["lock"]
    commands = " ".join(str(step.get("run", "")) for step in job["steps"])
    assert "uv lock --check" in commands


def test_the_verify_job_deliberately_ignores_the_lockfile() -> None:
    """The one job that must resolve for itself, and the reason must be written down.

    ``verify`` installs the built wheel and the built sdist into empty
    environments to prove they stand up where this repository does not exist.
    Handing them the lockfile would answer a question no user of the package can
    ask, and would hide a dependency range that no longer resolves — which is
    precisely the failure that job exists to catch.
    """
    text = (WORKFLOW_DIR / "pypi.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    steps = document["jobs"]["verify"]["steps"]
    commands = " ".join(str(step.get("run", "")) for step in steps)

    assert "uv venv" in commands, "verify should still build its environments with uv"
    assert "uv sync" not in commands, "verify must not install from the lockfile"
    assert "--locked" not in commands
    # The prose, so the omission reads as a decision to the next person.
    assert "deliberately does *not* install from uv.lock" in text, (
        "the verify job must say, in the file, that skipping the lockfile is the check"
    )


def test_the_release_tooling_comes_out_of_the_lockfile() -> None:
    """build, twine and the build backend, pinned like everything else.

    v0.0.1 lost an irreversible step to a twine that had moved: hatchling began
    stamping a newer core-metadata version, the freshly installed twine read it
    happily and the year-old twine inside the pinned publisher image rejected it
    at the upload. Pinning all three is the only spelling of "the tooling did not
    move" that survives a re-run.
    """
    text = (WORKFLOW_DIR / "pypi.yaml").read_text(encoding="utf-8")
    assert "uv sync --locked --only-group release" in text
    # ``--no-isolation`` is what makes the hatchling pin reach the build; without
    # it ``build`` resolves a backend of its own in a throwaway environment.
    assert "python -m build --no-isolation" in text


def test_the_release_group_names_the_build_backend(pyproject: dict[str, Any]) -> None:
    """The backend in ``[build-system] requires`` is resolved fresh unless it is here."""
    release = {_distribution_name(item) for item in pyproject["dependency-groups"]["release"]}
    assert {"build", "twine", "hatchling"} <= release
    backend = {_distribution_name(item) for item in pyproject["build-system"]["requires"]}
    assert backend <= release, (
        f"the build backend {backend - release} is not pinned by the release group"
    )


def test_the_sbom_describes_the_locked_closure() -> None:
    """An SBOM resolved fresh would differ between two runs of the same tag."""
    text = (WORKFLOW_DIR / "pypi.yaml").read_text(encoding="utf-8")
    assert "UV_PROJECT_ENVIRONMENT=sbom-env uv sync --locked --no-dev --no-install-project" in text
    # The wheel that was actually built goes in on top, without dragging its own
    # ranges in behind it.
    assert "uv pip install --python sbom-env --no-deps" in text


# --------------------------------------------------------------------------- #
# The container image
# --------------------------------------------------------------------------- #


def test_the_image_is_built_from_the_lockfile() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "uv sync --locked" in dockerfile
    assert "pip install" not in dockerfile, "the image no longer resolves at build time"


def test_the_image_pins_the_same_uv_as_everything_else(pyproject: dict[str, Any]) -> None:
    """The resolver that wrote the lockfile is the one that reads it here."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    required = pyproject["tool"]["uv"]["required-version"]
    assert f"ghcr.io/astral-sh/uv:{required}" in dockerfile, (
        f"the Dockerfile does not carry uv {required}; bump it with [tool.uv] required-version"
    )


def test_the_lockfile_reaches_the_build_context() -> None:
    """``.dockerignore`` is an allowlist, so a new file has to be let in by name.

    Without this the ``COPY`` fails outright rather than falling back to an
    unlocked install — which is the right way round, but only once somebody has
    put the entry back.
    """
    assert "!uv.lock" in DOCKERIGNORE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The hooks
# --------------------------------------------------------------------------- #


def test_the_ruff_hook_is_the_locked_ruff(locked_versions: dict[str, str]) -> None:
    """The formatter that runs at the commit and the one that gates CI.

    When these differ the symptom is a commit that was formatted locally and is
    "not formatted" in CI, with a diff nobody can reproduce until they notice the
    two versions. Neither file can see the other, so the agreement is asserted
    here.
    """
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    revisions = {
        repository["repo"]: repository["rev"]
        for repository in config["repos"]
        if "rev" in repository
    }
    hook = revisions["https://github.com/astral-sh/ruff-pre-commit"]
    assert hook == f"v{locked_versions['ruff']}", (
        f"the ruff hook is pinned at {hook}, uv.lock pins ruff {locked_versions['ruff']}; "
        "run 'uv lock' and 'pre-commit autoupdate' together"
    )


def test_a_stale_lockfile_is_caught_at_the_commit() -> None:
    """Editing dependencies without re-locking should cost a second, not a CI run."""
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    hooks = [hook for repository in config["repos"] for hook in repository["hooks"]]
    lock_hook = next((hook for hook in hooks if hook["id"] == "uv-lock-check"), None)
    assert lock_hook is not None, ".pre-commit-config.yaml has no uv.lock freshness hook"
    assert lock_hook["entry"] == "uv lock --check"
    assert lock_hook["pass_filenames"] is False


# --------------------------------------------------------------------------- #
# The composite actions, which deliberately do not use the lockfile
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ACTIONS)
def test_the_actions_prefer_uv_but_do_not_require_it(name: str) -> None:
    """They run in other people's repositories, where uv may not exist.

    So the fast path is taken only when uv is present *and* a virtualenv is
    active — the second condition because ``uv pip install`` into a bare system
    interpreter is refused outright on the distributions that mark themselves
    externally managed, and an action that fails there would be worse than a slow
    one. pip remains the fallback and is never wrong.
    """
    text = (ACTION_DIR / name / "action.yml").read_text(encoding="utf-8")
    assert 'command -v uv >/dev/null 2>&1 && [ -n "${VIRTUAL_ENV:-}" ]' in text
    assert "python -m pip install --disable-pip-version-check" in text


@pytest.mark.parametrize("name", ACTIONS)
def test_the_actions_say_why_they_carry_no_lockfile(name: str) -> None:
    """A missing pin is only a decision while the reason is written next to it."""
    text = (ACTION_DIR / name / "action.yml").read_text(encoding="utf-8")
    assert "Note what is deliberately absent: uv.lock" in text
    assert "uv sync" not in text, "the action must not install this repository's pins"


# --------------------------------------------------------------------------- #
# The documentation
# --------------------------------------------------------------------------- #


def test_contributing_documents_the_locked_setup() -> None:
    """The first command a contributor runs has to build the environment CI builds.

    A CONTRIBUTING that still said ``pip install -e '.[dev]'`` would hand every
    new contributor a different set of versions from the one every gate in this
    repository runs, and the difference would surface as a test failure they did
    not cause.
    """
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "uv sync --extra dev" in text
    assert "uv run pytest" in text


def test_the_locked_environment_can_actually_be_built() -> None:
    """``--locked`` resolves offline against the lockfile, so this needs no index.

    ``--dry-run`` reports what would change without touching the environment the
    suite is running in, which matters: syncing for real here would uninstall
    whatever the developer added on top.
    """
    result = subprocess.run(
        [_uv(), "sync", "--locked", "--all-extras", "--all-groups", "--dry-run"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin", "HOME": str(Path.home())},
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
