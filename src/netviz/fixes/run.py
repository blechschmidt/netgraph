"""Applying fixes, one at a time, and proving each one made the tree better.

The loop is deliberately not "compute every fix, apply them all". Two fixes born
of the same document contradict each other — two stale layout keys each produce
the surviving *rest* of their section, so applying the second would put the first
one back — and a fix computed against a tree three edits old is a fix for a tree
that no longer exists. So: validate, take one finding, apply its repair, validate
again, repeat. Every fix is therefore computed against the tree the previous one
left behind.

The second validation is not bookkeeping, it is the gate. A fix is **kept** only
when the finding it was aimed at is gone and no rule reports more than it did
before; otherwise the operations are undone and the fix is reported as refused,
with the findings it would have introduced. That is the promise the whole feature
rests on — running ``--fix`` cannot make an inventory worse — and it is checked
against the tree in front of the user rather than argued from the producers.

Refusal is a normal outcome, not a bug. "Delete the cable" is a real repair for a
cable that lands nowhere, and in a two-device inventory it also orphans a device;
the user is the one who gets to decide that, and the report says so.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from netviz.config import ValidationConfig
from netviz.edit.errors import EditError
from netviz.edit.operations import Operation, RemoveFile, WriteFile
from netviz.edit.session import EditSession
from netviz.errors import count_text
from netviz.fixes.model import Fix
from netviz.fixes.producers import fixes_for
from netviz.loader.inventory import Inventory
from netviz.validate import Finding, validate

__all__ = [
    "AppliedFix",
    "FixOutcome",
    "FixReport",
    "Offer",
    "SkippedFix",
    "apply_fix",
    "offers_for",
    "repair",
]

#: Ceiling on the number of fixes one run applies. A fix must remove the finding
#: it was aimed at, so the loop terminates on its own; this is the backstop that
#: turns a bug in that argument into a bounded run rather than a hung one.
MAX_FIXES: Final = 500


@dataclass(frozen=True, slots=True)
class Offer:
    """One finding and the repairs available for it."""

    finding: Finding
    fixes: tuple[Fix, ...]

    @property
    def is_ambiguous(self) -> bool:
        """Does the choice belong to the user rather than to the tool?"""
        return len(self.fixes) > 1

    def choose(self, key: str | None) -> Fix | None:
        """The fix ``key`` names, or the only one when there is only one."""
        if key is None:
            return self.fixes[0] if len(self.fixes) == 1 else None
        return next((fix for fix in self.fixes if fix.key == key), None)


@dataclass(frozen=True, slots=True)
class AppliedFix:
    """A repair that was applied and left the tree no worse."""

    finding: Finding
    fix: Fix
    #: Files the repair touched, in path order.
    files: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        return f"{self.finding.rule}  {self.fix.title}"


@dataclass(frozen=True, slots=True)
class SkippedFix:
    """A repair that was on offer and was not applied, and why not."""

    finding: Finding
    fixes: tuple[Fix, ...]
    reason: str
    #: Findings the repair would have introduced, when that is the reason.
    introduced: tuple[Finding, ...] = ()

    @property
    def summary(self) -> str:
        return f"{self.finding.rule}  {self.reason}"


@dataclass(frozen=True, slots=True)
class FixReport:
    """What one ``--fix`` run did, and what it left behind."""

    applied: tuple[AppliedFix, ...] = ()
    skipped: tuple[SkippedFix, ...] = ()
    #: Every finding still standing when the run stopped.
    remaining: tuple[Finding, ...] = ()
    #: Files the run would write, or wrote; ``None`` means removed.
    changes: Mapping[str, str | None] = field(default_factory=dict)
    #: The unified diff of all of it, for ``--dry-run``.
    diff: str = ""
    #: Files actually written. Empty for a dry run.
    written: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def to_dict(self) -> dict[str, object]:
        """The JSON form, for a caller that wants to act on the outcome."""
        return {
            "applied": [
                {
                    "rule": entry.finding.rule,
                    "location": entry.finding.location,
                    "message": entry.finding.message,
                    "fix": entry.fix.key,
                    "title": entry.fix.title,
                    "operations": [op.to_dict() for op in entry.fix.operations],
                    "files": list(entry.files),
                }
                for entry in self.applied
            ],
            "skipped": [
                {
                    "rule": entry.finding.rule,
                    "location": entry.finding.location,
                    "message": entry.finding.message,
                    "reason": entry.reason,
                    "choices": [{"key": fix.key, "title": fix.title} for fix in entry.fixes],
                    "introduced": [str(finding) for finding in entry.introduced],
                }
                for entry in self.skipped
            ],
            "remaining": [str(finding) for finding in self.remaining],
            "files": {
                path: ("deleted" if text is None else "written")
                for path, text in sorted(self.changes.items())
            },
            "written": list(self.written),
        }


def offers_for(
    findings: Sequence[Finding], inventory: Inventory, *, limit: int | None = None
) -> tuple[Offer, ...]:
    """The repairs available for each of ``findings``, in report order.

    Findings with nothing on offer are left out, so an empty result means
    "nothing here can be repaired mechanically".
    """
    found: list[Offer] = []
    for finding in findings:
        fixes = fixes_for(finding, inventory)
        if fixes:
            found.append(Offer(finding=finding, fixes=fixes))
        if limit is not None and len(found) >= limit:
            break
    return tuple(found)


def repair(
    session: EditSession,
    *,
    settings: ValidationConfig | None = None,
    choices: Mapping[str, str] | None = None,
    limit: int = MAX_FIXES,
) -> FixReport:
    """Apply every unambiguous fix the inventory admits, one at a time.

    Args:
        session: The tree to repair. Operations are applied to it and left
            pending; committing or throwing them away is the caller's decision,
            which is what makes ``--dry-run`` the same code path as ``--fix``.
        settings: How to grade findings, so the run sees what
            ``netviz validate`` would see in this tree.
        choices: Rule id to fix key, for the rules that offer more than one
            repair. A rule with a choice recorded here is applied like any
            unambiguous one; a rule without stays reported and untouched.
        limit: Ceiling on the number of fixes applied. See :data:`MAX_FIXES`.

    Returns:
        What was applied, what was refused and why, and what is left.
    """
    picked = dict(choices or {})
    applied: list[AppliedFix] = []
    skipped: list[SkippedFix] = []
    #: Findings already refused, so the loop does not retry them for ever.
    refused: set[tuple[str, str]] = set()

    findings = validate(session.inventory, settings)
    while len(applied) < limit:
        candidate = _next_candidate(session, findings, picked, refused)
        if candidate is None:
            break
        offer, fix = candidate
        outcome = _attempt(session, offer, fix, before=findings, settings=settings)
        if isinstance(outcome, SkippedFix):
            skipped.append(outcome)
            refused.add(_identity(offer.finding))
            # No re-validation: a refused repair puts the bytes back, so the
            # findings in hand are still the findings of the tree.
            continue
        applied.append(outcome[0])
        findings = outcome[1]

    skipped.extend(
        SkippedFix(
            finding=offer.finding,
            fixes=offer.fixes,
            reason=(
                f"{len(offer.fixes)} repairs are possible; choose one with "
                f"--choose {offer.finding.rule}=" + "|".join(fix.key for fix in offer.fixes)
            ),
        )
        for offer in offers_for(findings, session.inventory)
        if offer.is_ambiguous
        and offer.choose(picked.get(offer.finding.rule)) is None
        and _identity(offer.finding) not in refused
    )
    return FixReport(
        applied=tuple(applied),
        skipped=tuple(skipped),
        remaining=tuple(findings),
        changes=dict(session.changes),
        diff=session.diff(),
    )


def _next_candidate(
    session: EditSession,
    findings: Sequence[Finding],
    choices: Mapping[str, str],
    refused: set[tuple[str, str]],
) -> tuple[Offer, Fix] | None:
    """The next finding to repair, and the repair to try, or ``None``."""
    for offer in offers_for(findings, session.inventory):
        if _identity(offer.finding) in refused:
            continue
        fix = offer.choose(choices.get(offer.finding.rule))
        if fix is not None:
            return offer, fix
    return None


@dataclass(frozen=True, slots=True)
class FixOutcome:
    """What happened when one fix was tried."""

    #: Findings the repair would have added. Non-empty means it was rolled back.
    introduced: tuple[Finding, ...] = ()
    #: The finding it was aimed at is still there, so the repair repaired
    #: nothing. Also a rollback: an edit that changes the file without changing
    #: the diagnostic is an edit nobody asked for.
    survived: bool = False
    #: Every finding of the tree the fix left behind, when it was kept.
    findings: tuple[Finding, ...] = ()
    #: Files the repair changed, when it was kept.
    files: tuple[str, ...] = ()

    @property
    def kept(self) -> bool:
        """Did the repair stand?"""
        return not self.introduced and not self.survived

    @property
    def reason(self) -> str:
        """Why it did not, in one line."""
        if self.survived and not self.introduced:
            return "the repair left the finding in place"
        return (
            f"the repair would introduce {count_text(len(self.introduced), 'new finding')}: "
            + "; ".join(finding.message for finding in self.introduced)
        )


def apply_fix(
    session: EditSession,
    finding: Finding,
    fix: Fix,
    *,
    settings: ValidationConfig | None = None,
    before: Sequence[Finding] | None = None,
) -> FixOutcome:
    """Apply one fix to ``session`` and judge it, keeping it only if it stands.

    The judgement is the whole point, and it is made against the tree rather
    than argued from the producer: the finding the fix was aimed at has to be
    gone, and no rule may report more than it did before. When either fails the
    session is rolled back to the bytes it held, so a caller can try the next
    repair against a session that shows no sign of this one.

    Rolling back goes through the file primitives rather than through the
    operations' own inverses, because an inverse is only applicable to the tree
    the operation was applied to: undoing ``add-interface`` is
    ``remove-interface``, which rightly refuses to remove a port a cable now
    lands on — and that cable is the very finding being repaired.

    Args:
        session: The tree to repair. Left holding the change when the fix
            stands, and untouched when it does not.
        finding: What the fix is aimed at.
        fix: The repair to try.
        settings: How to grade findings.
        before: The findings of the tree as it is, when the caller already has
            them. Re-derived when not.

    Raises:
        EditError: The operations could not be applied. The session is rolled
            back first, so a repair whose second operation is refused leaves no
            trace of its first — which a bare
            :meth:`~netviz.edit.EditSession.apply_all` would not, a batch
            being atomic only per operation.
    """
    pending = dict(session.changes)
    baseline = validate(session.inventory, settings) if before is None else before
    try:
        results = session.apply_all(fix.operations)
    except EditError:
        _restore(session, pending)
        raise
    after = validate(session.inventory, settings)
    introduced = _regressions(baseline, after)
    survived = _identity(finding) in {_identity(entry) for entry in after}
    if introduced or survived:
        _restore(session, pending)
        return FixOutcome(introduced=introduced, survived=survived)
    return FixOutcome(
        findings=tuple(after),
        files=tuple(sorted({name for result in results for name in result.files})),
    )


def _attempt(
    session: EditSession,
    offer: Offer,
    fix: Fix,
    *,
    before: Sequence[Finding],
    settings: ValidationConfig | None,
) -> tuple[AppliedFix, list[Finding]] | SkippedFix:
    """One repair inside the loop: try it, and turn the outcome into a report."""
    try:
        outcome = apply_fix(session, offer.finding, fix, settings=settings, before=before)
    except EditError as exc:
        return SkippedFix(
            finding=offer.finding,
            fixes=offer.fixes,
            reason=f"the repair could not be applied: {exc}",
        )
    if not outcome.kept:
        return SkippedFix(
            finding=offer.finding,
            fixes=offer.fixes,
            reason=outcome.reason,
            introduced=outcome.introduced,
        )
    return AppliedFix(finding=offer.finding, fix=fix, files=outcome.files), list(outcome.findings)


def _restore(session: EditSession, pending: Mapping[str, str | None]) -> None:
    """Put every file back to the text it held before a refused fix ran.

    ``pending`` is the session's change set from before the attempt: a file in
    it goes back to that text, a file absent from it goes back to what is on
    disk, and a file the fix created is removed. Restoring the exact bytes is
    what makes the attempt invisible — the file drops out of the change set
    altogether rather than staying in it with an empty diff.
    """
    operations: list[Operation] = []
    for relative in sorted(set(session.changes) | set(pending)):
        wanted = pending[relative] if relative in pending else session.tree.original_of(relative)
        if wanted == session.changes.get(relative, _UNCHANGED):
            continue
        operations.append(
            RemoveFile(path=relative) if wanted is None else WriteFile(path=relative, text=wanted)
        )
    session.apply_all(operations)


#: Stands for "this file is not in the change set", which is a different thing
#: from "it is in the change set as deleted" (``None``).
_UNCHANGED: Final = object()


def _regressions(before: Sequence[Finding], after: Sequence[Finding]) -> tuple[Finding, ...]:
    """The findings ``after`` has that ``before`` did not, counted per rule.

    Per rule and by count rather than message by message, for the same reason
    :meth:`netviz.edit.EditSession.check` does it that way: repairing one
    problem legitimately changes the *wording* of another finding that names the
    same element, and a gate that called that a regression would refuse every
    useful fix. A rule that reports more than it did is the signal.
    """
    old = Counter(finding.rule for finding in before)
    new = Counter(finding.rule for finding in after)
    worse = {rule for rule, count in new.items() if count > old[rule]}
    if not worse:
        return ()
    seen = {_identity(finding) for finding in before}
    return tuple(
        finding for finding in after if finding.rule in worse and _identity(finding) not in seen
    )


def _identity(finding: Finding) -> tuple[str, str]:
    """What makes two findings the same problem: the rule and what it said."""
    return (finding.rule, finding.message)
