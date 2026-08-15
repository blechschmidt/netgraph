"""The release gate, and the version report the release ships.

A release is the one operation in this repository that cannot be undone: a
version on PyPI is taken forever, a tag other people have fetched cannot be
moved, and a GitHub release with the wrong notes is wrong in everybody's mailbox.
So the checks that stand in front of it are asserted here rather than only in the
workflow -- a guard exercised solely by the release it guards is not a guard, it
is a hope.

Five things are checked:

* ``tools/release.py`` refuses a tag that disagrees with ``pyproject.toml``, a
  missing changelog section, an empty one and an undated heading, and extracts
  the right body when everything is in order.
* The repository's own ``pyproject.toml`` and ``CHANGELOG.md`` pass that gate
  right now, so the next tag cannot fail on something a pull request could have
  caught.
* ``.github/workflows/release.yml`` pins every action to a commit SHA, keeps its
  permissions per job, and names the environments the PyPI trusted publisher is
  scoped to.
* ``netgraph --version`` and ``netgraph version --json`` report the package, the
  Python and the Graphviz actually in use, in the shapes the workflow and
  ``docs/commands/version.md`` promise.
* The package still imports on the interpreters ``requires-python`` and the
  classifiers claim -- which is not the one this suite runs on, and is a claim
  a release makes to everybody who installs it.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
import pkgutil
import re
import subprocess
import sys
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import netgraph
from netgraph import __version__
from netgraph.cli import cli
from netgraph.version import (
    REPORT_SCHEMA_VERSION,
    as_dict,
    collect,
    format_text,
    graphviz_version,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RELEASING_DOC = REPO_ROOT / "docs" / "releasing.md"


def load_release_tool() -> ModuleType:
    """Import ``tools/release.py`` as a module.

    Same approach as ``tests/test_docs.py`` uses for the generators: the scripts
    in ``tools/`` are not a package, and copying their logic into the tests would
    defeat the purpose of testing them.
    """
    name = "release_tool"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / "release.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release = load_release_tool()


#: A changelog with everything in order, used as the starting point for the
#: broken variants below.
GOOD_CHANGELOG = """\
# Changelog

## [Unreleased]

_Nothing yet._

## [0.2.0] - 2026-08-01

### Added

- A thing.

### Changed

- Another thing.

## [0.1.0] - 2026-07-30

### Added

- The first thing.

[Unreleased]: https://example.invalid/compare/v0.2.0...HEAD
[0.2.0]: https://example.invalid/releases/tag/v0.2.0
[0.1.0]: https://example.invalid/releases/tag/v0.1.0
"""


def pyproject_with(version: str) -> str:
    return f'[project]\nname = "netgraph"\nversion = "{version}"\n'


# --------------------------------------------------------------------------- #
# The version in pyproject.toml
# --------------------------------------------------------------------------- #


def test_the_packaged_version_is_read_from_pyproject() -> None:
    assert release.project_version(pyproject_with("1.2.3")) == "1.2.3"


def test_a_pyproject_without_a_version_is_refused() -> None:
    with pytest.raises(release.ReleaseError, match="declares no"):
        release.project_version('[project]\nname = "netgraph"\n')


def test_two_top_level_versions_are_refused_rather_than_guessed_at() -> None:
    """Preferring one silently is how the wrong number gets published."""
    with pytest.raises(release.ReleaseError, match="2 top-level version"):
        release.project_version(pyproject_with("1.2.3") + 'version = "9.9.9"\n')


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0", "0.2.0rc1", "0.2.0a1", "1.0.0.post1"])
def test_a_version_this_project_spells_is_accepted(version: str) -> None:
    release.check_version_syntax(version)


@pytest.mark.parametrize(
    "version",
    [
        "0.2",  # not three components
        "0.2.0-rc1",  # a hyphen, which PEP 440 does not use
        "v0.2.0",  # the tag, not the version
        "0.2.0.1",
        "",
    ],
)
def test_a_version_this_project_does_not_spell_is_refused(version: str) -> None:
    with pytest.raises(release.ReleaseError, match="PEP 440"):
        release.check_version_syntax(version)


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.1.0", False), ("0.2.0rc1", True), ("0.2.0a3", True), ("1.0.0.post1", False)],
)
def test_a_prerelease_is_recognised(version: str, expected: bool) -> None:
    """It decides whether the image also gets ``latest``, so it has to be right.

    A ``.post`` release is not a pre-release: it comes *after* the version it
    names, so it is the newest thing there is.
    """
    assert release.is_prerelease(version) is expected


@pytest.mark.parametrize(("version", "expected"), [("0.1.0", "0.1"), ("1.12.7", "1.12")])
def test_the_minor_line_is_the_moving_image_tag(version: str, expected: str) -> None:
    assert release.minor_line(version) == expected


# --------------------------------------------------------------------------- #
# The tag
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ref", ["refs/tags/v0.1.0", "v0.1.0"])
def test_the_version_is_taken_from_either_spelling_of_the_ref(ref: str) -> None:
    assert release.version_from_ref(ref) == "0.1.0"


def test_a_tag_without_the_v_prefix_is_refused() -> None:
    with pytest.raises(release.ReleaseError, match="does not start with 'v'"):
        release.version_from_ref("refs/tags/0.1.0")


def test_a_tag_that_disagrees_with_the_package_stops_the_release() -> None:
    """The check the whole guard job exists for."""
    with pytest.raises(
        release.ReleaseError, match=re.escape("tag says 0.2.0, pyproject.toml says 0.1.0")
    ):
        release.check_release(pyproject_with("0.1.0"), GOOD_CHANGELOG, "refs/tags/v0.2.0")


def test_a_matching_tag_passes_and_returns_the_section() -> None:
    version, section = release.check_release(
        pyproject_with("0.2.0"), GOOD_CHANGELOG, "refs/tags/v0.2.0"
    )
    assert version == "0.2.0"
    assert section.date == "2026-08-01"
    assert "- A thing." in section.body


def test_a_dry_run_checks_the_version_in_pyproject() -> None:
    """``workflow_dispatch`` has no tag, so the packaged version is the subject."""
    version, section = release.check_release(pyproject_with("0.1.0"), GOOD_CHANGELOG, None)
    assert version == "0.1.0"
    assert "- The first thing." in section.body


# --------------------------------------------------------------------------- #
# The changelog section
# --------------------------------------------------------------------------- #


def test_every_section_is_found_in_file_order() -> None:
    versions = [section.version for section in release.parse_changelog(GOOD_CHANGELOG)]
    assert versions == ["Unreleased", "0.2.0", "0.1.0"]


def test_a_section_body_stops_at_the_next_heading() -> None:
    [section] = [s for s in release.parse_changelog(GOOD_CHANGELOG) if s.version == "0.2.0"]
    assert "### Added" in section.body
    assert "### Changed" in section.body
    assert "The first thing" not in section.body


def test_the_link_definitions_are_not_part_of_the_last_section() -> None:
    """Otherwise every release body would end in a wall of URLs."""
    [section] = [s for s in release.parse_changelog(GOOD_CHANGELOG) if s.version == "0.1.0"]
    assert "https://example.invalid" not in section.body
    assert section.body.strip().endswith("- The first thing.")


def test_a_missing_section_stops_the_release() -> None:
    with pytest.raises(release.ReleaseError, match=re.escape("no '## [0.3.0]' section")):
        release.find_section(GOOD_CHANGELOG, "0.3.0")


def test_an_empty_section_stops_the_release() -> None:
    """A heading with nothing under it is worse than none: it looks like one."""
    text = GOOD_CHANGELOG.replace(
        "### Added\n\n- A thing.\n\n### Changed\n\n- Another thing.\n", ""
    )
    with pytest.raises(release.ReleaseError, match="is empty"):
        release.find_section(text, "0.2.0")


def test_an_undated_release_heading_stops_the_release() -> None:
    text = GOOD_CHANGELOG.replace("## [0.2.0] - 2026-08-01", "## [0.2.0]")
    with pytest.raises(release.ReleaseError, match="carries no date"):
        release.find_section(text, "0.2.0")


def test_the_unreleased_section_needs_no_date() -> None:
    section = release.find_section(GOOD_CHANGELOG, "Unreleased")
    assert section.date is None
    assert section.body.strip() == "_Nothing yet._"


def test_a_duplicated_section_stops_the_release() -> None:
    text = GOOD_CHANGELOG.replace(
        "## [0.1.0] - 2026-07-30", "## [0.2.0] - 2026-07-30\n\n- Again.\n\n## [0.1.0] - 2026-07-30"
    )
    with pytest.raises(release.ReleaseError, match=re.escape("2 '## [0.2.0]' sections")):
        release.find_section(text, "0.2.0")


def test_a_changelog_with_no_sections_at_all_stops_the_release() -> None:
    with pytest.raises(release.ReleaseError, match=re.escape("no '## [version]' sections")):
        release.find_section("# Changelog\n\nNothing here.\n", "0.1.0")


# --------------------------------------------------------------------------- #
# This repository, right now
# --------------------------------------------------------------------------- #


def test_this_repository_would_pass_its_own_release_gate() -> None:
    """The point of the whole file: the next tag cannot fail on today's tree.

    A version bump committed without the changelog entry fails here, on the pull
    request, instead of after the tag has been pushed.
    """
    version, section = release.check_release(
        PYPROJECT.read_text(encoding="utf-8"),
        CHANGELOG.read_text(encoding="utf-8"),
        f"refs/tags/v{__version__ if __version__ != '0.0.0.dev0' else ''}"
        if __version__ != "0.0.0.dev0"
        else None,
    )
    assert section.body.strip()
    # The installed distribution and the source tree have to agree, unless the
    # tree was never installed at all (``__version__`` falls back then).
    if __version__ != "0.0.0.dev0":
        assert version == __version__


def test_the_changelog_keeps_an_unreleased_section_at_the_top() -> None:
    """Where the next entry goes. Without it, entries land in a shipped section."""
    sections = release.parse_changelog(CHANGELOG.read_text(encoding="utf-8"))
    assert sections, "CHANGELOG.md has no sections"
    assert sections[0].version == release.UNRELEASED


def test_the_changelog_sections_are_newest_first() -> None:
    """Keep a Changelog order, and what the release body extraction assumes."""
    versions = [
        section.version
        for section in release.parse_changelog(CHANGELOG.read_text(encoding="utf-8"))
        if section.version != release.UNRELEASED
    ]
    parsed = [tuple(int(part) for part in version.split(".")[:3]) for version in versions]
    assert parsed == sorted(parsed, reverse=True), f"out of order: {versions}"


def test_the_changelog_is_linked_from_the_documentation_index() -> None:
    index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "CHANGELOG.md" in index
    assert "releasing.md" in index


# --------------------------------------------------------------------------- #
# The interpreters the package claims
# --------------------------------------------------------------------------- #


def netgraph_modules() -> list[ModuleType]:
    """Every module of the installed package, imported."""
    modules = [netgraph]
    for info in pkgutil.walk_packages(netgraph.__path__, prefix="netgraph."):
        modules.append(importlib.import_module(info.name))
    return modules


def test_the_whole_package_imports() -> None:
    """A module nothing imports is a module nothing has checked syntax of."""
    assert len(netgraph_modules()) > 100


def test_no_dataclass_default_is_a_mapping_proxy() -> None:
    """``MappingProxyType({})`` as a default is an import error on 3.10 and 3.11.

    ``dataclasses`` refuses a default whose class is unhashable, and
    ``mappingproxy`` only became hashable in 3.12 — so a field written as
    ``origin: Mapping[str, str] = MappingProxyType({})`` imports on the
    interpreter most of us have and raises ``ValueError`` at *import time* on
    the oldest one ``requires-python`` claims. Nothing else notices: ruff's
    RUF009 has ``MappingProxyType`` on its known-immutable list, and mypy's
    ``--python-version 3.11`` does not model the check.

    It has now happened twice: ``netgraph lsp`` in July, and
    ``netgraph.render.styles`` on 2026-08-15, which took the whole 3.11 job with
    it before a single test ran — and, on a 3.11 anybody had installed netgraph
    on, every command that draws anything. The first time it was answered with
    ``test_a_cursor_context_can_be_built_on_every_supported_python``, which
    covers one module; this is the one that covers the package. The spelling
    that works everywhere is the one the rest of the package uses: a
    module-level constant behind a ``field(default_factory=lambda: _EMPTY)``.
    """
    offenders = [
        f"{cls.__module__}.{cls.__qualname__}.{entry.name}"
        for cls in dataclasses_of(netgraph_modules())
        for entry in dataclasses.fields(cls)
        if isinstance(entry.default, MappingProxyType)
    ]
    assert not offenders, (
        "these dataclass fields default to a mappingproxy, which Python 3.10 and "
        f"3.11 refuse at import time; use field(default_factory=...): {offenders}"
    )


def dataclasses_of(modules: Iterable[ModuleType]) -> list[type]:
    """Every dataclass netgraph defines, once each."""
    found: dict[str, type] = {}
    for module in modules:
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and dataclasses.is_dataclass(value)
                and value.__module__.startswith("netgraph")
            ):
                found[f"{value.__module__}.{value.__qualname__}"] = value
    return list(found.values())


# --------------------------------------------------------------------------- #
# The release workflow
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def release_workflow() -> dict[Any, Any]:
    """The parsed workflow.

    Keyed by ``Any`` and not by ``str`` because YAML 1.1 reads the bare word
    ``on`` as the boolean ``True``, so the trigger block genuinely lives under a
    non-string key.
    """
    parsed = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


#: ``uses: owner/repo@ref`` -- with the ``# vX.Y.Z`` comment when there is one.
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)\s*(?:#\s*(?P<comment>.*?))?\s*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


#: Permissions that let a job leave something behind that other people fetch: a
#: commit, a package in the registry, a signature, or an OIDC token that trades
#: for an upload. ``security-events: write`` is pointedly not here -- it writes
#: alerts into this repository's own security tab, produces nothing anybody
#: downloads, and ``ci.yml`` holds it while floating its action pins on purpose.
PUBLISHING_PERMISSIONS = frozenset({"contents", "packages", "id-token", "attestations"})


def privileged_workflows() -> list[Path]:
    """Every workflow in which some job can publish something.

    Membership is derived rather than listed, so the rule below applies to the
    next publishing workflow somebody adds without anyone having to remember to
    name it here. Today that is ``release.yml`` and ``container.yml``.
    """
    privileged = []
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        jobs: dict[str, Any] = workflow.get("jobs", {})
        grants = [workflow.get("permissions") or {}]
        grants += [job.get("permissions") or {} for job in jobs.values()]
        if any(
            value == "write" and name in PUBLISHING_PERMISSIONS
            for grant in grants
            for name, value in grant.items()
        ):
            privileged.append(path)
    return privileged


def workflow_uses(path: Path) -> list[tuple[int, str, str | None]]:
    """Every ``uses:`` in a workflow, with its line and trailing comment."""
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = _USES_RE.match(line)
        if match is not None:
            found.append((number, match.group("ref"), match.group("comment")))
    return found


def privileged_uses() -> list[tuple[str, int, str, str | None]]:
    """Flattened for parametrisation: the file name alongside each ``uses:``."""
    return [
        (path.name, number, ref, comment)
        for path in privileged_workflows()
        for number, ref, comment in workflow_uses(path)
    ]


def test_there_are_privileged_workflows_and_they_use_actions() -> None:
    """Guards the test below against a filter or a regex that matches nothing.

    Both are derived, so either one silently returning an empty list would turn
    the pinning rule into a test that passes by having nothing to check.
    """
    names = {path.name for path in privileged_workflows()}
    assert {"release.yml", "container.yml"} <= names, names
    assert len(privileged_uses()) >= 12


@pytest.mark.parametrize(
    ("name", "line", "ref", "comment"),
    privileged_uses(),
    ids=[f"{name}-line-{n}" for name, n, _, _ in privileged_uses()],
)
def test_every_action_in_a_privileged_workflow_is_pinned_to_a_commit_sha(
    name: str, line: int, ref: str, comment: str | None
) -> None:
    """A tag can be moved; a commit cannot.

    ``ci.yml`` floats on purpose -- a compromised action there can read a
    checkout that is already public. A compromised action in one of *these* runs
    in a job holding a token that publishes to PyPI or pushes to GHCR under this
    project's name, so the pins are not optional. ``docs/releasing.md`` says how
    to bump one.
    """
    if ref.startswith("./"):
        # A workflow or action from this very checkout: pinned by definition.
        return
    _, _, version = ref.partition("@")
    assert _SHA_RE.match(version), (
        f"{name} line {line}: {ref} is pinned to {version!r}, not to a 40-character "
        "commit SHA. Resolve it with 'git ls-remote --tags <url> <tag>'."
    )
    assert comment and re.search(r"v?\d+\.\d+", comment), (
        f"{name} line {line}: {ref} has no '# vX.Y.Z' comment, so nobody can tell "
        "which version the SHA is without asking GitHub."
    )


def test_the_workflow_runs_on_a_tag_and_on_a_manual_dry_run(
    release_workflow: dict[Any, Any],
) -> None:
    # ``on`` is truthy in YAML 1.1, so PyYAML reads the key as the boolean True.
    triggers = release_workflow[True]
    assert triggers["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in triggers


def test_the_default_permission_is_read_only(release_workflow: dict[Any, Any]) -> None:
    assert release_workflow["permissions"] == {"contents": "read"}


def test_a_release_in_flight_is_never_cancelled(release_workflow: dict[Any, Any]) -> None:
    """A run stopped between the upload and the release leaves a version with no notes."""
    assert release_workflow["concurrency"]["cancel-in-progress"] is False


def test_every_job_declares_its_own_permissions(release_workflow: dict[Any, Any]) -> None:
    """The blast radius of a publishing job, written where it can be read."""
    for name, job in release_workflow["jobs"].items():
        if "uses" in job:
            # A reusable-workflow call: its permissions are the caller's grant.
            assert "permissions" in job, f"the {name} job grants nothing to the workflow it calls"
            continue
        assert "permissions" in job, f"the {name} job inherits its permissions instead of saying"


@pytest.mark.parametrize(
    ("job", "permission"),
    [
        ("pypi", "id-token"),
        ("testpypi", "id-token"),
        ("provenance", "attestations"),
        ("image", "packages"),
        ("image", "attestations"),
        ("github-release", "contents"),
    ],
)
def test_the_publishing_jobs_ask_for_exactly_what_they_need(
    release_workflow: dict[Any, Any], job: str, permission: str
) -> None:
    permissions = release_workflow["jobs"][job]["permissions"]
    assert permissions.get(permission) == "write", f"the {job} job cannot {permission}: write"


def test_only_the_release_job_can_write_to_the_repository(
    release_workflow: dict[Any, Any],
) -> None:
    """Nothing that builds or publishes an artefact may also rewrite the repo."""
    writers = [
        name
        for name, job in release_workflow["jobs"].items()
        if job.get("permissions", {}).get("contents") == "write"
    ]
    assert writers == ["github-release"]


def test_no_long_lived_pypi_token_is_referenced() -> None:
    """Trusted Publishing, or nothing. There is no credential here to leak."""
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("PYPI_API_TOKEN", "PYPI_TOKEN", "TWINE_PASSWORD", "password: __token__"):
        assert forbidden not in text, f"release.yml names {forbidden}"


@pytest.mark.parametrize(("job", "environment"), [("pypi", "pypi"), ("testpypi", "testpypi")])
def test_the_upload_jobs_name_the_environment_the_publisher_is_scoped_to(
    release_workflow: dict[Any, Any], job: str, environment: str
) -> None:
    """Without the environment, any workflow here could mint an upload token.

    The names are part of the PyPI trusted-publisher configuration, so they are
    as much an interface as a flag is; ``docs/releasing.md`` documents both.
    """
    assert release_workflow["jobs"][job]["environment"]["name"] == environment
    assert environment in RELEASING_DOC.read_text(encoding="utf-8")


def test_the_real_upload_only_happens_on_a_tag(release_workflow: dict[Any, Any]) -> None:
    jobs = release_workflow["jobs"]
    assert jobs["pypi"]["if"] == "github.event_name == 'push'"
    assert jobs["testpypi"]["if"] == "github.event_name == 'workflow_dispatch'"
    # The dry run must reach TestPyPI having passed everything the real one does.
    for job in ("pypi", "testpypi"):
        assert "verify" in jobs[job]["needs"]


def test_nothing_irreversible_runs_before_the_checks(release_workflow: dict[Any, Any]) -> None:
    """Ordering *is* the safety property, so it is asserted rather than commented."""
    jobs = release_workflow["jobs"]
    for name in ("pypi", "provenance", "image", "github-release"):
        assert "guard" in jobs[name]["needs"] or "verify" in jobs[name]["needs"], (
            f"the {name} job can run without the guard or the verification"
        )
    assert "ci" in jobs["build"]["needs"], "the build does not wait for the CI gate"
    assert "build" in jobs["verify"]["needs"]


def test_the_wheel_is_installed_and_run_on_all_three_platforms(
    release_workflow: dict[Any, Any],
) -> None:
    """The claim in docs/releasing.md, checked against the matrix that makes it."""
    systems = release_workflow["jobs"]["verify"]["strategy"]["matrix"]["os"]
    assert any("ubuntu" in entry for entry in systems)
    assert any("macos" in entry for entry in systems)
    assert any("windows" in entry for entry in systems)

    # The raw text rather than the parsed steps: what matters is the exact shell
    # word, and ``yaml.dump`` re-escapes it into something no assertion can read.
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert '"$bin/netgraph" --version' in text, (
        "the verify job never runs the installed console script by name"
    )
    assert '"$bin/netgraph" version --json' in text
    assert "netgraph-${VERSION}.tar.gz" in text, "the verify job never installs the sdist"
    assert "netgraph-${VERSION}-py3-none-any.whl" in text


def test_the_release_image_is_the_one_every_commit_builds(
    release_workflow: dict[Any, Any],
) -> None:
    """container.yml called as a reusable workflow, for the same reason ``ci`` is.

    A release-only copy of the image build is a copy that drifts, and the release
    is the worst place to find out: the image a version tag publishes has then
    been built by a file nothing rehearsed. Delegating also means a ``v*`` tag
    causes exactly one build of the commit and one push of ``1.2.3``.

    The two architectures are asserted where they are now built, in
    tests/test_docker.py.
    """
    image = release_workflow["jobs"]["image"]
    assert image["uses"] == "./.github/workflows/container.yml"
    assert "steps" not in image, "release.yml builds an image of its own again"

    # The inputs it cannot leave to the ref: a dry run must not push, and the
    # SBOM the GitHub release attaches has to be named after this version.
    assert image["with"]["push"] == "${{ github.event_name == 'push' }}"
    assert image["with"]["sbom-artifact"] == "sbom-image"
    assert "needs.guard.outputs.version" in image["with"]["version"]


def test_the_ci_workflow_can_be_called_by_the_release(release_workflow: dict[Any, Any]) -> None:
    """The release runs the *same* gate, not a copy of a subset of it."""
    ci = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert "workflow_call" in ci[True], "ci.yml cannot be called as a reusable workflow"
    # ... and a tag push must not run it twice, once directly and once through here.
    assert ci[True]["push"]["tags-ignore"] == ["v*"]
    assert release_workflow["jobs"]["ci"]["uses"] == "./.github/workflows/ci.yml"


def test_every_push_triggered_workflow_names_the_branches_it_runs_on() -> None:
    """A ``push`` filter that mentions tags and not branches means "tags only".

    That is a GitHub rule and a silent one: the workflow simply stops being
    triggered, and a workflow that is never triggered has no run to show as red.
    ``ci.yml`` was in exactly that state for three commits — the whole gate,
    absent, with a green-looking history because there was no history at all.
    Asserted for every workflow rather than for that one, because the next
    ``tags-ignore`` will be added to a different file.
    """
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        triggers = yaml.safe_load(path.read_text(encoding="utf-8"))[True]
        push = triggers.get("push")
        if not isinstance(push, dict):
            continue
        names_tags = {"tags", "tags-ignore"} & set(push)
        names_branches = {"branches", "branches-ignore"} & set(push)
        if names_tags and not names_branches:
            # A workflow that is *meant* to be tag-only says so with ``tags``,
            # which is the positive form; ``tags-ignore`` alone is the accident.
            assert "tags" in push, (
                f"{path.name} filters pushes by {sorted(names_tags)} and names no "
                "branches, so GitHub will not run it for any branch push"
            )


# --------------------------------------------------------------------------- #
# The version report
# --------------------------------------------------------------------------- #


def test_the_report_names_the_package_python_and_the_platform() -> None:
    report = collect()
    assert report.netgraph == __version__
    assert re.match(r"^\d+\.\d+\.\d+", report.python)
    assert report.python_implementation
    assert report.platform
    assert report.yaml_parser in {"libyaml", "python"}


def test_the_first_line_is_the_version_and_nothing_else() -> None:
    """``netgraph --version | cut -d' ' -f2`` has to keep working."""
    first = format_text(collect()).splitlines()[0]
    assert first == f"netgraph {__version__}"


def test_the_text_report_names_every_component() -> None:
    text = format_text(collect())
    for label in ("Python", "Graphviz", "Platform", "YAML parser"):
        assert label in text, f"the report never mentions {label}"


def test_the_report_names_the_runtime_dependencies() -> None:
    """The versions that decide how netgraph behaves, for a bug report."""
    dependencies = collect().dependencies
    for name in ("pydantic", "PyYAML", "click", "networkx", "jinja2"):
        assert name in dependencies, f"the report never names {name}"


def test_the_json_report_has_the_documented_shape() -> None:
    document = as_dict(collect())
    assert document["schemaVersion"] == REPORT_SCHEMA_VERSION
    assert document["netgraph"] == __version__
    assert set(document["python"]) == {"version", "implementation", "executable"}
    # An object rather than a string: "absent" and "present but unaskable" are
    # different states and a consumer has to be able to tell them apart.
    assert set(document["graphviz"]) == {"version", "path", "error"}
    assert set(document["platform"]) == {"description", "system", "machine", "os"}
    assert document["yamlParser"] in {"libyaml", "python"}


def test_absent_graphviz_is_reported_as_absent_not_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ``dot``, ``mermaid`` and ``json`` render works without it."""
    monkeypatch.setattr("netgraph.render.dot.find_dot", lambda: None)
    report = collect()
    assert report.graphviz is None
    assert report.graphviz_path is None
    assert report.graphviz_error is None
    assert "not found" in format_text(report)


def test_a_dot_that_is_not_graphviz_is_reported_with_the_reason(tmp_path: Path) -> None:
    """The usual cause is ``NETGRAPH_DOT`` pointing at the wrong thing."""
    missing = tmp_path / "not-dot"
    version, error = graphviz_version(str(missing))
    assert version is None
    assert error


def test_a_dot_that_prints_nonsense_is_reported_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("netgraph.render.dot.find_dot", lambda: str(tmp_path / "dot"))
    monkeypatch.setattr(
        "netgraph.version.graphviz_version", lambda executable: (None, "printed 'hi'")
    )
    report = collect()
    assert report.graphviz is None
    assert report.graphviz_path is not None
    text = format_text(report)
    assert "unknown" in text and "printed 'hi'" in text


def test_a_dot_that_never_answers_is_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--version`` has to come back. It is the first thing a stuck user types."""

    def hang(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="dot", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", hang)
    version, error = graphviz_version("dot")
    assert version is None
    assert error is not None and "did not answer" in error


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected"),
    [
        (0, b"dot - graphviz version 12.1.0 (0)", None),
        (0, b"", "printed 'nothing'"),
        (1, b"something went wrong", "exited 1"),
        (0, b"this is not graphviz", "printed 'this is not graphviz'"),
    ],
)
def test_the_banner_is_read_rather_than_assumed(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: bytes, expected: str | None
) -> None:
    """Four things ``dot -V`` can do, and the four different things to say back."""

    def fake(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["dot", "-V"], returncode=returncode, stdout=b"", stderr=stderr
        )

    monkeypatch.setattr(subprocess, "run", fake)
    version, error = graphviz_version("dot")
    if expected is None:
        assert version == "12.1.0" and error is None
    else:
        assert version is None and error is not None and expected in error


def test_the_banner_is_also_read_from_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some builds and some wrappers write it there instead."""

    def fake(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["dot", "-V"],
            returncode=0,
            stdout=b"dot - graphviz version 2.43.0 (0)\n",
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake)
    assert graphviz_version("dot") == ("2.43.0", None)


def test_an_uninstalled_dependency_is_left_out_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``watchfiles`` and ``ruamel.yaml`` back lazily-imported commands.

    An environment without them still runs `validate` and `render`, so it must
    still be able to produce a version report.
    """
    real = metadata.version

    def absent(name: str) -> str:
        if name == "watchfiles":
            raise metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr("netgraph.version.metadata.version", absent)
    dependencies = collect().dependencies
    assert "watchfiles" not in dependencies
    assert "pydantic" in dependencies


# --------------------------------------------------------------------------- #
# ... through the CLI, which is what a user types
# --------------------------------------------------------------------------- #


def test_the_version_flag_prints_the_report_and_exits() -> None:
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == f"netgraph {__version__}"
    assert "Graphviz" in result.output


def test_the_version_flag_needs_no_inventory(tmp_path: Path) -> None:
    """Eager, so it answers from a directory holding nothing at all."""
    result = CliRunner().invoke(cli, ["--inventory", str(tmp_path), "--version"])
    assert result.exit_code == 0
    assert result.output.startswith("netgraph ")


def test_the_short_flag_is_the_same_report() -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["-V"]).output == runner.invoke(cli, ["--version"]).output


def test_the_version_command_prints_the_same_text_as_the_flag() -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["version"]).output == runner.invoke(cli, ["--version"]).output


def test_the_version_command_emits_parseable_json() -> None:
    result = CliRunner().invoke(cli, ["version", "--json"])
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["netgraph"] == __version__
    assert document["schemaVersion"] == REPORT_SCHEMA_VERSION


def test_the_version_command_is_documented() -> None:
    """It is a command, so ``docs/commands/`` has a page -- and it says --json."""
    page = REPO_ROOT / "docs" / "commands" / "version.md"
    text = page.read_text(encoding="utf-8")
    assert "--json" in text
    assert "schemaVersion" in text
