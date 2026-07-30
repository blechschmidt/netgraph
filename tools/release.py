#!/usr/bin/env python3
"""The checks a release must pass before anything is published.

Publishing is the one operation in this repository that cannot be undone: a
version number on PyPI is taken forever, a tag other people have fetched cannot
be moved, and a GitHub release with the wrong notes is a release whose notes are
wrong in everybody's mailbox. So the three facts that must agree are asserted
here, in a script the test suite runs, rather than in the workflow -- where they
would only ever be exercised by the release they were supposed to guard.

The three facts:

1. **The tag and the package agree.** ``v0.2.0`` must be ``version = "0.2.0"`` in
   ``pyproject.toml``. A mismatch means either the bump was forgotten or the tag
   was a typo, and both produce an artefact whose file name lies about it.
2. **The changelog has a section for that version, and it is not empty.** A
   release with no entry is a release nobody can read; a heading with nothing
   under it is worse, because it looks like one.
3. **The notes are extractable.** The body of that section becomes the GitHub
   release body, so it is cut out here and written to a file the workflow
   uploads, rather than reconstructed by shell.

Usage::

    python tools/release.py check --ref refs/tags/v0.1.0   # the release gate
    python tools/release.py check                          # dry run: use pyproject's version
    python tools/release.py version                        # print the packaged version
    python tools/release.py notes 0.1.0 --output notes.md   # the release body

``check`` writes ``version=``/``tag=``/``notes=`` lines to ``$GITHUB_OUTPUT``
when it is set, so the workflow reads the values it just verified instead of
parsing ``pyproject.toml`` a second time in bash.

``tests/test_release.py`` drives every function below against both good and bad
inputs, which is the point: the guard is only worth having if a broken release
fails in CI on the pull request that broke it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
PYPROJECT: Final = REPO_ROOT / "pyproject.toml"
CHANGELOG: Final = REPO_ROOT / "CHANGELOG.md"

#: ``## [0.1.0] - 2026-07-30`` or ``## [Unreleased]``. The link-reference form of
#: the version is what Keep a Changelog specifies and what the compare links at
#: the bottom of the file hang off, so it is required rather than tolerated.
_HEADING_RE: Final = re.compile(
    r"^##\s+\[(?P<version>[^\]]+)\]" r"(?:\s*[-–]\s*(?P<date>\S+))?\s*$"
)

#: ``version = "0.1.0"`` in the ``[project]`` table. Read with a regex and not
#: with ``tomllib`` on purpose: this script is also the thing that runs before
#: the package is installed, on an interpreter that may predate ``tomllib``, and
#: the single assignment it needs is not worth a dependency.
_VERSION_RE: Final = re.compile(r"^version\s*=\s*[\"'](?P<version>[^\"']+)[\"']\s*$", re.MULTILINE)

#: What ``0.x`` releases of this project are allowed to look like: three dotted
#: numbers, optionally a PEP 440 pre-release or post-release suffix. Enforced so
#: that a tag like ``v0.2`` or ``v0.2.0-rc1`` (a hyphen, which PEP 440 does not
#: use) is refused at the gate rather than by ``twine`` after the build.
_PEP440_RE: Final = re.compile(
    r"^(?P<release>\d+\.\d+\.\d+)"
    r"(?:(?P<pre>(?:a|b|rc)\d+))?"
    r"(?:\.post(?P<post>\d+))?"
    r"(?:\.dev(?P<dev>\d+))?$"
)

#: The section every changelog carries and no release may be cut from.
UNRELEASED: Final = "Unreleased"


class ReleaseError(Exception):
    """A release that must not proceed. The message says what to fix."""


@dataclass(frozen=True)
class Section:
    """One ``## [version]`` block of the changelog."""

    #: The version as spelled in the heading: ``0.1.0``, or ``Unreleased``.
    version: str
    #: The date, if the heading carried one. ``None`` for ``Unreleased``.
    date: str | None
    #: Everything under the heading up to the next one, stripped of blank
    #: leading and trailing lines. This is the GitHub release body.
    body: str
    #: 1-based line number of the heading, for error messages that can be
    #: clicked.
    line: int


def project_version(pyproject: str) -> str:
    """The ``version`` of the ``[project]`` table.

    Takes the *first* assignment in the file, which is the project's own: the
    ``[build-system]`` table above it has no ``version`` key, and every table
    below is tool configuration. A second one appearing later would be a
    surprise, so it is reported rather than silently preferred.
    """
    matches = _VERSION_RE.findall(pyproject)
    if not matches:
        raise ReleaseError("pyproject.toml declares no 'version = \"...\"' at the top level")
    if len(matches) > 1:
        raise ReleaseError(
            f"pyproject.toml declares {len(matches)} top-level version assignments "
            f"({', '.join(matches)}); the release gate cannot tell which is the package's"
        )
    return str(matches[0])


def version_from_ref(ref: str) -> str:
    """``refs/tags/v0.1.0`` -> ``0.1.0``.

    Both the full ref and a bare tag are accepted, because ``github.ref`` gives
    the first and a human typing the command gives the second.
    """
    tag = ref.rsplit("/", 1)[-1] if ref.startswith("refs/") else ref
    if not tag.startswith("v"):
        raise ReleaseError(f"tag {tag!r} does not start with 'v'; releases are tagged 'vX.Y.Z'")
    return tag[1:]


def check_version_syntax(version: str) -> None:
    """Refuse a version PyPI would accept but this project does not spell."""
    if _PEP440_RE.match(version) is None:
        raise ReleaseError(
            f"version {version!r} is not 'X.Y.Z' with an optional PEP 440 suffix "
            "(a1, b2, rc1, .post1, .dev1); see docs/releasing.md"
        )


def is_prerelease(version: str) -> bool:
    """Whether this version is a pre-release, by PEP 440's definition.

    Decides two things in the workflow: whether the container image also gets the
    ``latest`` tag, and whether the GitHub release is marked as a pre-release. A
    ``.post`` release is *not* a pre-release -- it comes after the version it
    names, so it is the newest thing there is.
    """
    match = _PEP440_RE.match(version)
    if match is None:
        raise ReleaseError(f"version {version!r} is not a version this project spells")
    return match.group("pre") is not None or match.group("dev") is not None


def minor_line(version: str) -> str:
    """``0.1.3`` -> ``0.1``: the moving tag the image also gets."""
    check_version_syntax(version)
    major, minor, _ = version.split(".", 2)
    return f"{major}.{minor}"


def parse_changelog(text: str) -> list[Section]:
    """Every ``## [version]`` section, in file order.

    The heading itself is not part of the body, and the ``[version]: url``
    link-reference definitions at the bottom of the file are not part of the last
    section -- otherwise every release body would end in a wall of URLs.
    """
    lines = text.splitlines()
    starts = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _HEADING_RE.match(line)) is not None
    ]

    sections: list[Section] = []
    for position, (index, match) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        body = [
            line
            for line in lines[index + 1 : end]
            # A link-reference definition is metadata for the whole file that
            # happens to sit under the last heading.
            if not re.match(r"^\[[^\]]+\]:\s+\S+", line)
        ]
        sections.append(
            Section(
                version=match.group("version").strip(),
                date=(match.group("date") or None),
                body="\n".join(body).strip("\n"),
                line=index + 1,
            )
        )
    return sections


def find_section(text: str, version: str) -> Section:
    """The section for ``version``, or a :class:`ReleaseError` saying so.

    Empty is a failure, not a warning: the whole reason to check the changelog
    before publishing is that "I will write the entry afterwards" never survives
    contact with a released version number.
    """
    sections = parse_changelog(text)
    if not sections:
        raise ReleaseError(
            "CHANGELOG.md has no '## [version]' sections; is it in Keep a Changelog form?"
        )

    matched = [section for section in sections if section.version == version]
    if not matched:
        known = ", ".join(section.version for section in sections[:6])
        raise ReleaseError(
            f"CHANGELOG.md has no '## [{version}]' section (it has: {known}). "
            f"Move the '## [{UNRELEASED}]' entries under a '## [{version}] - <date>' heading."
        )
    if len(matched) > 1:
        raise ReleaseError(
            f"CHANGELOG.md has {len(matched)} '## [{version}]' sections, on lines "
            f"{', '.join(str(section.line) for section in matched)}"
        )

    section = matched[0]
    if not section.body.strip():
        raise ReleaseError(
            f"the '## [{version}]' section of CHANGELOG.md (line {section.line}) is empty; "
            "a release with no entry is a release nobody can read"
        )
    if section.version != UNRELEASED and section.date is None:
        raise ReleaseError(
            f"the '## [{version}]' heading of CHANGELOG.md (line {section.line}) carries no date; "
            f"write it as '## [{version}] - YYYY-MM-DD'"
        )
    return section


def check_release(pyproject: str, changelog: str, ref: str | None) -> tuple[str, Section]:
    """The whole gate. Returns the version and its changelog section.

    With no ``ref`` -- the ``workflow_dispatch`` dry run -- the packaged version
    is taken as the intended one, so a dry run checks exactly what the next tag
    would check.
    """
    packaged = project_version(pyproject)
    check_version_syntax(packaged)

    if ref is not None:
        tagged = version_from_ref(ref)
        if tagged != packaged:
            raise ReleaseError(
                f"tag says {tagged}, pyproject.toml says {packaged}. "
                "Bump the version, commit, then move the tag onto that commit."
            )

    return packaged, find_section(changelog, packaged)


def _write_github_output(**values: str) -> None:
    """Append ``key=value`` lines to ``$GITHUB_OUTPUT``, if there is one.

    Every value here is a version, a tag or a path -- no newlines -- so the
    plain form is enough and the heredoc delimiter dance is not needed.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"cannot read {path.name}: {exc.strerror or exc}") from exc


def _command_check(args: argparse.Namespace) -> int:
    version, section = check_release(_read(PYPROJECT), _read(CHANGELOG), args.ref)

    notes = Path(args.notes)
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(section.body + "\n", encoding="utf-8")

    print(f"version {version}")
    print(f"changelog section on line {section.line}, dated {section.date}")
    print(f"{len(section.body.splitlines())} lines of notes -> {notes}")
    _write_github_output(**_outputs(version), notes=str(notes))
    return 0


def _outputs(version: str) -> dict[str, str]:
    """The values the workflow reads back out of ``$GITHUB_OUTPUT``."""
    return {
        "version": version,
        "tag": f"v{version}",
        "minor": minor_line(version),
        "prerelease": "true" if is_prerelease(version) else "false",
    }


def _command_version(args: argparse.Namespace) -> int:
    version = project_version(_read(PYPROJECT))
    check_version_syntax(version)
    print(version)
    _write_github_output(**_outputs(version))
    return 0


def _command_notes(args: argparse.Namespace) -> int:
    section = find_section(_read(CHANGELOG), args.version)
    if args.output:
        Path(args.output).write_text(section.body + "\n", encoding="utf-8")
    else:
        print(section.body)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="verify the tag, the version and the changelog agree")
    check.add_argument(
        "--ref",
        default=None,
        help="the tag being released ('refs/tags/v0.1.0' or 'v0.1.0'). "
        "Omit for a dry run against the version in pyproject.toml.",
    )
    check.add_argument(
        "--notes",
        default="release-notes.md",
        help="where to write the changelog section that becomes the release body",
    )
    check.set_defaults(func=_command_check)

    version = sub.add_parser("version", help="print the version in pyproject.toml")
    version.set_defaults(func=_command_version)

    notes = sub.add_parser("notes", help="print one version's changelog section")
    notes.add_argument("version", help="the version to extract, e.g. 0.1.0")
    notes.add_argument("--output", default=None, help="write to this file instead of stdout")
    notes.set_defaults(func=_command_notes)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ReleaseError as exc:
        # ``::error::`` so the message lands as an annotation on the run rather
        # than only in the log, where a failing release is easy to misread.
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
