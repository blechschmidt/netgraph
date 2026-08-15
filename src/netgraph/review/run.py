"""Read both sides of a pull request and assemble the :class:`Review`.

This is the half that touches the world: it exports the base ref, loads two
inventories, validates both and diffs them. Everything it produces is a value,
so :mod:`netgraph.review.comment` never has to.

Two decisions are made here rather than in the formatting.

**The base is allowed not to exist.** A repository whose inventory arrived in
this very pull request has no base to compare against, and a bot that fell over
on the first pull request it ever saw would never be adopted. That case is
reported (:attr:`~netgraph.review.model.FindingDelta.base_absent`) and the
review carries on with an empty base.

**The base is allowed not to load.** A tree that was broken before the change is
exactly the tree this bot exists to let a team adopt it on: its load errors
become base diagnostics like any other, and the delta subtracts them. Only the
*head* failing to load stops the comparison, because a plan built against a
half-read tree reports every rejected document as a deletion.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from netgraph import __version__
from netgraph.config import ValidationConfig
from netgraph.diagnostics import Report, build_report
from netgraph.errors import count_text
from netgraph.loader import Inventory, load_tree
from netgraph.plan import diff as diff_states
from netgraph.plan.model import Plan, StateRef
from netgraph.plan.sources import MissingInventory, git_ref
from netgraph.plan.state import state_digest
from netgraph.review.model import Diagram, Review, compute_delta
from netgraph.validate import validate

__all__ = ["ReviewResult", "build_review"]

#: How the two sides are named when the caller gave no better name.
_WORKING_TREE: Final = "the working tree"


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """What :func:`build_review` produced, and the artefacts beside it.

    The two reports are handed back rather than folded into the review because
    they have another consumer: the head report is what the SARIF upload is
    rendered from, and rendering it a second time from a second validation run
    could disagree with the comment.
    """

    review: Review
    #: Every diagnostic of the head state, for ``-F sarif``.
    head: Report
    #: The same for the base, so a caller can show what was already broken.
    base: Report
    #: The changeset, or ``None`` when the head does not load.
    plan: Plan | None


def build_review(
    *,
    root: Path,
    base: str,
    head: str | None = None,
    head_inventory: Inventory | None = None,
    config: ValidationConfig | None = None,
    renames: bool = True,
    diagrams: tuple[Diagram, ...] = (),
    artifact_url: str | None = None,
    artifact_name: str | None = None,
    repository_url: str | None = None,
    head_sha: str | None = None,
    prefix: str | None = None,
    title: str = "netgraph",
    strict: bool = False,
    max_changes: int = 40,
    max_findings: int = 25,
) -> ReviewResult:
    """Compare ``base`` with ``head`` and write up what a reviewer should see.

    Args:
        root: The inventory root, and the directory the git ref is resolved
            against.
        base: A git ref or a directory holding the state before the change.
        head: The same for the state after it. ``None`` is the working tree.
        head_inventory: The working tree, already loaded, when ``head`` is
            ``None``. The caller has usually loaded it once already, and the
            global ``-i`` may name a single file rather than a directory —
            which only the caller knows how to honour.
        config: Suppressions and severity overrides, applied to *both* sides.
            Grading the two differently would report a rule regraded in this
            very pull request as a wave of new findings.
        renames: Detect renames rather than reporting delete-plus-create.
        diagrams: Where the visual diff was written, for the links.
        artifact_url: Where the uploaded bundle can be downloaded.
        artifact_name: What that bundle is called.
        repository_url: ``https://github.com/owner/repo``, for permalinks.
        head_sha: The commit the head was read at, for the same.
        prefix: The inventory root as seen from the repository root. ``None``
            derives it the way SARIF does, from the working directory — which
            in CI is the checkout.
        title: Names the comment, and keys its sticky marker.
        strict: Only recorded, for the comment: the grading itself is
            ``config``'s, which the caller built with the same flag.
        max_changes: Rows the changeset table holds before it is truncated.
        max_findings: The same for the findings table.
    """
    with ExitStack() as stack:
        before, before_ref = _side(stack, root, base)
        if head is None:
            after = head_inventory if head_inventory is not None else load_tree(root)
            after_ref = StateRef(kind="tree", description=_WORKING_TREE, digest=state_digest(after))
        else:
            exported, after_ref = _side(stack, root, head)
            # A head ref with no inventory in it is not a mistake either: this
            # change deleted the tree. An empty inventory diffs to exactly that,
            # and refusing would hide the largest change a review can carry.
            after = exported if exported is not None else Inventory(root=root)

        base_report = _report(before, config) if before is not None else None
        head_report = _report(after, config)
        plan, broken = _compare(before, after, before_ref, after_ref, renames=renames)
        caveats = _caveats(before)

    delta = compute_delta(
        base_report.diagnostics if base_report is not None else (),
        head_report.diagnostics,
        base_absent=base_report is None,
    )
    review = Review(
        plan=plan,
        delta=delta,
        findings=head_report.diagnostics,
        base=base,
        head="" if head is None else head,
        prefix=_prefix_of(root) if prefix is None else prefix,
        repository_url=repository_url,
        head_sha=head_sha,
        diagrams=diagrams,
        artifact_url=artifact_url,
        artifact_name=artifact_name,
        title=title,
        strict=strict,
        broken=broken,
        caveats=caveats,
        max_changes=max_changes,
        max_findings=max_findings,
        version=__version__,
    )
    return ReviewResult(
        review=review,
        head=head_report,
        base=base_report if base_report is not None else Report(root=root),
        plan=plan,
    )


def _side(stack: ExitStack, root: Path, spec: str) -> tuple[Inventory | None, StateRef]:
    """Load a folder or a git ref, the way ``netgraph plan`` loads one.

    ``None`` for an inventory that is not in that ref at all — see the module
    docstring. Every other failure is the caller's to report: a ref that does
    not resolve is a mistake, not a state.
    """
    folder = Path(spec)
    if folder.is_dir():
        inventory = load_tree(folder)
        return inventory, StateRef(kind="folder", description=spec, digest=state_digest(inventory))
    try:
        exported = stack.enter_context(git_ref(root, spec))
    except MissingInventory:
        return None, StateRef(kind="git", description=spec)
    inventory = load_tree(exported)
    return inventory, StateRef(kind="git", description=spec, digest=state_digest(inventory))


def _prefix_of(root: Path) -> str:
    """``root`` as seen from the working directory, POSIX style.

    Computed by building an empty report rather than by hand, so the paths the
    comment links and the paths the SARIF upload carries are joined by one piece
    of code and cannot disagree about where the inventory sits.
    """
    return build_report(Inventory(root=root), ()).prefix


def _report(inventory: Inventory, config: ValidationConfig | None) -> Report:
    """One side's diagnostics, load errors and findings together.

    ``base=root`` is deliberately not passed: the paths a review links are
    joined with the caller's own ``prefix``, which is the inventory's place in
    the *repository* — and the base side was loaded from a temporary export
    whose place in the filesystem says nothing.
    """
    return build_report(inventory, validate(inventory, config))


def _compare(
    before: Inventory | None,
    after: Inventory,
    before_ref: StateRef,
    after_ref: StateRef,
    *,
    renames: bool,
) -> tuple[Plan | None, str | None]:
    """The changeset, or the reason there is none."""
    if after.errors:
        return None, (
            f"The head state does not load: "
            f"{count_text(len(after.errors), 'document was', 'documents were')} rejected. "
            f"Every one of them is in the table below."
        )
    empty = before if before is not None else Inventory(root=after.root)
    return diff_states(empty, after, source=before_ref, target=after_ref, renames=renames), None


def _caveats(before: Inventory | None) -> tuple[str, ...]:
    """What a reader has to know before trusting the changeset.

    A base whose documents were rejected is still compared against — that is the
    whole point of a delta — but the rejected ones are *absent* from the base
    inventory, so anything they declared reads as an addition. Saying so is
    cheaper than refusing, and refusing is what would stop a team with a broken
    ``main`` from ever seeing a review at all.
    """
    if before is None or not before.errors:
        return ()
    return (
        f"The base state does not load either: "
        f"{count_text(len(before.errors), 'document was', 'documents were')} rejected there, "
        f"so anything they declared is counted below as an addition.",
    )
