"""The pull-request review: what a change does to the network, written up.

A team whose network lives in git reviews it the way it reviews code — in a pull
request — and the question a reviewer actually has is not "does this branch
validate?" but "what does *this change* do, and what does it break that was not
already broken?". Neither question is answered by a green check.

This package answers both, and it is deliberately split in two:

:mod:`netviz.review.model`
    The data. A :class:`~netviz.review.model.Review` is a changeset
    (:class:`~netviz.plan.model.Plan`), the head inventory's diagnostics, the
    base inventory's diagnostics, and enough context to link a finding to a line
    on GitHub. :func:`~netviz.review.model.compute_delta` subtracts one side
    from the other using :func:`netviz.diagnostics.fingerprint`, which is the
    same identity code scanning tracks alerts by — so the bot and the alert list
    cannot disagree about which problem is new.

:mod:`netviz.review.comment`
    The formatting. :func:`~netviz.review.comment.render_comment` is a pure
    function from a :class:`~netviz.review.model.Review` to the Markdown body
    of one comment. Nothing in it reaches the network, the filesystem or the
    clock, so the whole of the bot's output is covered by ordinary tests.

:mod:`netviz.review.run`
    The part that reads the world: load both sides, validate both, plan the
    difference. Everything CI-shaped — where the diagram was published, which
    run produced it — arrives as an argument rather than an environment lookup.

The write-up of the action and the workflow built on this is in ``docs/ci.md``.
"""

from __future__ import annotations

from netviz.review.comment import MARKER_PREFIX, marker, render_comment
from netviz.review.model import (
    SUMMARY_SCHEMA_VERSION,
    Diagram,
    FindingDelta,
    KindSummary,
    Review,
    Verdict,
    compute_delta,
    group_changes,
    summarise,
)
from netviz.review.run import build_review

__all__ = [
    "MARKER_PREFIX",
    "SUMMARY_SCHEMA_VERSION",
    "Diagram",
    "FindingDelta",
    "KindSummary",
    "Review",
    "Verdict",
    "build_review",
    "compute_delta",
    "group_changes",
    "marker",
    "render_comment",
    "summarise",
]
