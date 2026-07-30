"""When a report was generated, from what, and by which version of the tool.

A printed report outlives the terminal it was produced in. Six months later the
only questions that matter about it are "is this current?" and "which commit of
the inventory is this?", and neither can be answered by a document that does not
say. So every page carries three facts: the netgraph version, the time of
generation, and the git revision of the inventory when there is one.

The stamp is the one part of a report that is *not* a function of the inventory,
which puts it at odds with the promise that the same input produces the same
bytes. Both are kept, by taking the clock from the environment rather than
reading it:

* ``--generated-at`` pins it to whatever the caller says, including ``none``,
  which leaves it out altogether;
* ``SOURCE_DATE_EPOCH`` — the reproducible-builds convention — is honoured when
  no flag was given, so a pipeline that already sets it gets identical output for
  free;
* otherwise the current UTC time is used, truncated to the second.

The git revision is discovered rather than assumed, and never fatally: no git, no
work tree, a shallow checkout with no HEAD, a repository too broken to answer —
all of them leave the field empty and the rest of the report intact. A document
generator has no business failing because it could not find a version-control
system.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from netgraph.errors import NetgraphError

__all__ = [
    "EPOCH_ENV_VAR",
    "NO_TIMESTAMP",
    "Revision",
    "git_revision",
    "resolve_timestamp",
]

#: The reproducible-builds environment variable, honoured when no timestamp was
#: named on the command line. https://reproducible-builds.org/docs/source-date-epoch/
EPOCH_ENV_VAR: Final = "SOURCE_DATE_EPOCH"

#: What ``--generated-at`` takes to leave the stamp out entirely.
NO_TIMESTAMP: Final = "none"

#: How long to wait for git. A report is not worth blocking on a repository that
#: is not answering, and the two commands below are the cheapest git has.
_GIT_TIMEOUT_SECONDS: Final = 5.0

#: Length of the abbreviated commit id. Twelve is what git itself grows to on a
#: large repository, and is short enough to read off a printed page.
_ABBREV: Final = 12

#: A commit id, as ``rev-parse`` writes it. Matched rather than trusted: the
#: output goes into a document, and a git that answered something unexpected must
#: not be able to put it there.
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{7,64}")


@dataclass(frozen=True, slots=True)
class Revision:
    """The commit an inventory was at, and whether it had been edited since."""

    #: Abbreviated commit id.
    commit: str
    #: True when tracked files under the inventory root differ from the commit,
    #: which is exactly the case where the report does *not* describe the commit
    #: it names — so it is reported rather than hidden.
    modified: bool = False

    @property
    def state(self) -> str:
        """``clean`` or ``modified``, for the line under the title."""
        return "modified" if self.modified else "clean"


def resolve_timestamp(
    given: str = "", *, environ: dict[str, str] | None = None, now: datetime | None = None
) -> str:
    """The generated-at stamp, as ISO-8601 UTC to the second, or ``""``.

    Args:
        given: ``--generated-at``. An ISO-8601 timestamp, ``none`` to omit the
            stamp, or empty to fall back to the environment and then the clock.
        environ: Where :data:`EPOCH_ENV_VAR` is read from; defaults to the
            process environment.
        now: Injected clock, for the tests. Defaults to the real one.

    Raises:
        NetgraphError: ``given`` is neither ``none`` nor a timestamp Python can
            parse. Failing here is the point: a report whose stamp silently fell
            back to the clock because of a typo is a report that lies about when
            it was made.
    """
    if given:
        if given.strip().lower() == NO_TIMESTAMP:
            return ""
        return _format(_parse(given))

    values = os.environ if environ is None else environ
    epoch = values.get(EPOCH_ENV_VAR, "").strip()
    if epoch:
        # Every way this can fail is one error to the user: a value that is not a
        # number, and one that is a number no platform can turn into a date.
        try:
            moment = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            raise NetgraphError(
                f"{EPOCH_ENV_VAR}={epoch!r} is not a usable number of seconds since the epoch "
                f"({exc}); unset it, or pass --generated-at"
            ) from exc
        return _format(moment)

    return _format(now if now is not None else datetime.now(tz=timezone.utc))


def _parse(text: str) -> datetime:
    """One ISO-8601 timestamp, assumed UTC when it names no offset."""
    candidate = text.strip()
    # ``fromisoformat`` learned to read a trailing ``Z`` in 3.11 and netgraph
    # supports 3.10, so the one spelling everybody writes is normalised here.
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise NetgraphError(
            f"--generated-at {text!r} is not an ISO-8601 timestamp: {exc}. "
            f"Write it as '2026-01-31T09:00:00Z', or as '{NO_TIMESTAMP}' to leave it out."
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _format(moment: datetime) -> str:
    """``2026-01-31T09:00:00Z`` — UTC, to the second, one spelling."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_revision(root: Path) -> Revision | None:
    """The commit ``root`` sits at, or ``None`` when that cannot be established.

    Two questions, two commands. ``rev-parse`` answers "which commit", and a
    ``status`` narrowed to the inventory root answers "does this report describe
    it". The second is scoped on purpose: an inventory committed inside a larger
    repository is not stale because something else in that repository was edited.
    """
    commit = _git(root, "rev-parse", f"--short={_ABBREV}", "HEAD")
    if commit is None or _COMMIT_RE.fullmatch(commit) is None:
        return None
    # ``--`` then the directory: without the separator a path that looks like a
    # revision would be read as one.
    status = _git(root, "status", "--porcelain", "--untracked-files=no", "--", ".")
    return Revision(commit=commit, modified=bool(status))


def _git(root: Path, *arguments: str) -> str | None:
    """Run one git command inside ``root``, or return ``None`` if it cannot be.

    Absent git, an unreadable directory, a non-repository, a timeout and a
    non-zero exit are all the same answer here — "no revision is available" —
    because none of them is a problem with the report.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "replace").strip()
