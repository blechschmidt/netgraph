"""What a pull request does to the network, as data.

Everything here is a value. A :class:`Review` is assembled by
:mod:`netgraph.review.run` from things that read the world, and rendered by
:mod:`netgraph.review.comment` without touching it again, so the two halves can
be tested apart: the formatting over fixtures, the loading over a repository.

The one piece of judgement that lives here rather than in the formatting is
:class:`Verdict` — whether the check passes. It is computed from the *delta*
(:func:`compute_delta`) and never from the head report alone, because a
repository with a legacy warning has to be able to adopt the bot without a red
baseline: what a pull request did not introduce is not its author's to fix.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from netgraph.diagnostics import Diagnostic, fingerprint
from netgraph.plan.model import Action, Change, Plan
from netgraph.rules import Severity

__all__ = [
    "ACTION_LABELS",
    "SUMMARY_SCHEMA_VERSION",
    "Diagram",
    "FindingDelta",
    "KindSummary",
    "Review",
    "Verdict",
    "compute_delta",
    "group_changes",
    "summarise",
]

#: Version of the document :func:`summarise` writes. Bumped when a field it
#: already had changes meaning; adding one does not need it.
SUMMARY_SCHEMA_VERSION: Final = 1

#: What each action is called in a table heading, in the order the columns go.
ACTION_LABELS: Final[Mapping[Action, str]] = {
    Action.CREATE: "Added",
    Action.UPDATE: "Changed",
    Action.RENAME: "Renamed",
    Action.DELETE: "Removed",
}


class Verdict(str, Enum):
    """The one-line answer, and what the check exits with.

    ``BROKEN`` is its own outcome rather than a kind of ``FAILED``: an inventory
    that does not load has no changeset at all, so the comment cannot say what
    changed and has to say so instead of reporting an empty plan.
    """

    #: Nothing new is wrong. There may be changes, and there may be findings the
    #: base already had.
    PASSED = "passed"
    #: New warnings or infos, no new errors.
    WARNED = "warned"
    #: New errors.
    FAILED = "failed"
    #: The head state does not load, so nothing could be compared.
    BROKEN = "broken"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Diagram:
    """One rendering of the visual diff, and where a reader can reach it.

    ``url`` is a *published* image — GitHub Pages, an object store — which the
    comment embeds. An artifact has no such URL: it is a zip behind an
    authenticated endpoint, so it is linked and never embedded. Both may be set
    for the same drawing, and either may be absent.
    """

    #: What the link says: ``SVG``, ``PNG``.
    label: str
    #: Path inside the uploaded artifact, if it was uploaded.
    path: str | None = None
    #: Directly embeddable image URL, if it was published somewhere.
    url: str | None = None

    @property
    def embeddable(self) -> bool:
        return self.url is not None


@dataclass(frozen=True, slots=True)
class KindSummary:
    """How many elements of one kind each action touches."""

    kind: str
    counts: Mapping[Action, int]
    #: The changes themselves, in plan order, for the per-element detail.
    changes: tuple[Change, ...] = ()

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True, slots=True)
class FindingDelta:
    """The head's diagnostics minus the base's, both ways round.

    Identity is :func:`netgraph.diagnostics.fingerprint` — rule, file, element,
    pointer and message, deliberately not the line — so moving a document down a
    file does not report every problem in it as newly introduced.
    """

    #: Present on the head and not on the base. This is what the check gates on.
    new: tuple[Diagnostic, ...] = ()
    #: Present on the base and not on the head: what the change repaired.
    fixed: tuple[Diagnostic, ...] = ()
    #: Present on both. Pre-existing, and not this pull request's to answer for.
    carried: tuple[Diagnostic, ...] = ()
    #: The base state had no inventory at all — a first pull request, or a tree
    #: that moved. Everything is then "new", and the comment says why.
    base_absent: bool = False

    def of(self, severity: Severity, diagnostics: Iterable[Diagnostic] | None = None) -> int:
        """How many of ``diagnostics`` (:attr:`new` by default) are ``severity``."""
        chosen = self.new if diagnostics is None else diagnostics
        return sum(1 for diagnostic in chosen if diagnostic.severity is severity)

    @property
    def new_errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.new if d.severity is Severity.ERROR)

    @property
    def empty(self) -> bool:
        return not self.new and not self.fixed


@dataclass(frozen=True, slots=True)
class Review:
    """Everything one pull-request comment says, before it is written out."""

    #: The changeset from base to head, or ``None`` when the head does not load
    #: and there was nothing to compare.
    plan: Plan | None = None
    #: The head's diagnostics against the base's.
    delta: FindingDelta = field(default_factory=FindingDelta)
    #: Every diagnostic the head has, new or not, for the counts in the summary.
    findings: tuple[Diagnostic, ...] = ()

    # -- where this came from -------------------------------------------
    #: How the base is named in the heading — a ref, a branch, a folder.
    base: str = "base"
    #: How the head is named. Empty when the head is the working tree.
    head: str = ""
    #: The inventory root as seen from the repository root, ``""`` at the root.
    #: Prefixed onto every file path so a link resolves in the checkout.
    prefix: str = ""
    #: ``https://github.com/owner/repo``, for linking a finding to its line.
    repository_url: str | None = None
    #: The commit the head was read at, so the link is a permalink.
    head_sha: str | None = None

    # -- what was drawn, and where it went -------------------------------
    diagrams: tuple[Diagram, ...] = ()
    #: Where the uploaded artifact can be downloaded — in practice the run page.
    artifact_url: str | None = None
    #: What that artifact is called, for the link text.
    artifact_name: str | None = None

    # -- presentation ----------------------------------------------------
    #: Names the comment and its sticky marker, so two inventories reviewed in
    #: one repository each keep their own comment.
    title: str = "netgraph"
    #: Both sides were validated with ``--strict``, which the comment says so a
    #: reader is not surprised by an error where the rule says warning.
    strict: bool = False
    #: Why there is no plan, when there is none: the load errors, worded.
    broken: str | None = None
    #: Anything a reader has to know to trust the numbers — most often that the
    #: base state did not load, so the changeset counts what could be read of it
    #: and nothing more. Rendered as a quote under the heading.
    caveats: tuple[str, ...] = ()
    #: How many element rows the changeset table holds before it is truncated.
    #: A GitHub comment is capped at 65536 characters and a big refactor would
    #: sail past it, so the table is bounded and says what it left out.
    max_changes: int = 40
    #: The same, for the findings table.
    max_findings: int = 25
    #: The same, for the nodes of the inline diagram.
    max_nodes: int = 30
    #: Version of the tool that produced the review, for the footer.
    version: str = ""

    @property
    def changed(self) -> bool:
        """Does this pull request change the network at all?"""
        return self.plan is not None and not self.plan.empty

    @property
    def verdict(self) -> Verdict:
        """The one-word outcome. See :class:`Verdict`."""
        if self.plan is None:
            return Verdict.BROKEN
        if self.delta.new_errors:
            return Verdict.FAILED
        if self.delta.new:
            return Verdict.WARNED
        return Verdict.PASSED

    @property
    def kinds(self) -> tuple[KindSummary, ...]:
        """The changeset grouped by element kind. Empty when nothing changed."""
        return () if self.plan is None else group_changes(self.plan)

    def path_of(self, diagnostic: Diagnostic) -> str | None:
        """``diagnostic``'s file as a path from the repository root.

        The same join :class:`netgraph.diagnostics.Report` does for SARIF, and
        for the same reason: a diagnostic's file is relative to the inventory
        root, and a link has to resolve against the checkout.
        """
        if diagnostic.file is None:
            return None
        return f"{self.prefix}/{diagnostic.file}" if self.prefix else diagnostic.file

    def link_to(self, diagnostic: Diagnostic) -> str | None:
        """A permalink to the line ``diagnostic`` is anchored at, when there is one.

        ``None`` unless the repository and the commit are both known: a link
        built against a branch name would point at whatever that branch says
        next week, which is not what a review comment is for.
        """
        path = self.path_of(diagnostic)
        if path is None or self.repository_url is None or not self.head_sha:
            return None
        anchor = f"#L{diagnostic.line}" if diagnostic.line is not None else ""
        return f"{self.repository_url.rstrip('/')}/blob/{self.head_sha}/{path}{anchor}"


def summarise(review: Review) -> dict[str, Any]:
    """``review`` as the small JSON document a workflow gates on.

    A pull-request check has to answer "did this fail?" without reading the
    comment, and a workflow that grepped the prose would break the first time a
    sentence was reworded. Everything the shell needs is here and nothing else:
    the verdict, the counts, and whether the head loaded at all.
    """
    delta = review.delta
    return {
        "schemaVersion": SUMMARY_SCHEMA_VERSION,
        "tool": {"name": "netgraph", "version": review.version},
        "verdict": review.verdict.value,
        "broken": review.plan is None,
        "changed": review.changed,
        "changes": 0 if review.plan is None else len(review.plan),
        "new": {
            **{str(severity): delta.of(severity) for severity in Severity},
            "total": len(delta.new),
        },
        "fixed": len(delta.fixed),
        "carried": len(delta.carried),
        "baseAbsent": delta.base_absent,
    }


def compute_delta(
    base: Sequence[Diagnostic], head: Sequence[Diagnostic], *, base_absent: bool = False
) -> FindingDelta:
    """Subtract ``base``'s diagnostics from ``head``'s, keeping report order.

    Duplicates are counted rather than collapsed: two identical findings on the
    head and one on the base leave one new. A set would quietly lose the second,
    and "you added another of these" is exactly what a reviewer wants told.
    """
    remaining: dict[str, int] = {}
    for diagnostic in base:
        key = fingerprint(diagnostic)
        remaining[key] = remaining.get(key, 0) + 1

    new: list[Diagnostic] = []
    carried: list[Diagnostic] = []
    for diagnostic in head:
        key = fingerprint(diagnostic)
        if remaining.get(key):
            remaining[key] -= 1
            carried.append(diagnostic)
        else:
            new.append(diagnostic)

    # What is left in ``remaining`` is what the head no longer reports. The
    # count matters here too, so the base is walked again rather than rebuilt
    # from the keys: order is the report's, not a dictionary's.
    surplus = dict(remaining)
    fixed: list[Diagnostic] = []
    for diagnostic in base:
        key = fingerprint(diagnostic)
        if surplus.get(key):
            surplus[key] -= 1
            fixed.append(diagnostic)

    return FindingDelta(
        new=tuple(new),
        fixed=tuple(fixed),
        carried=tuple(carried),
        base_absent=base_absent,
    )


def group_changes(plan: Plan) -> tuple[KindSummary, ...]:
    """The changeset grouped by element kind, kinds in alphabetical order.

    Grouping by kind and not by action is what makes the table readable at a
    glance: "three cables and a switch" is the shape of the change, whereas a
    list sorted by action buries the one switch among thirty cables.
    """
    order: list[str] = []
    counts: dict[str, dict[Action, int]] = {}
    changes: dict[str, list[Change]] = {}
    for change in plan:
        if change.kind not in counts:
            order.append(change.kind)
            counts[change.kind] = dict.fromkeys(ACTION_LABELS, 0)
            changes[change.kind] = []
        counts[change.kind][change.action] += 1
        changes[change.kind].append(change)
    return tuple(
        KindSummary(kind=kind, counts=counts[kind], changes=tuple(changes[kind]))
        for kind in sorted(order)
    )
