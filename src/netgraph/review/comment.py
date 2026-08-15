"""One pull-request comment, rendered from a :class:`~netgraph.review.model.Review`.

:func:`render_comment` is a pure function: same review in, byte-identical
Markdown out, no clock, no filesystem, no network. That is what makes the whole
of the bot's output testable without a GitHub — ``tests/test_review.py`` renders
fixture plans and asserts on the text — and it is also what makes the comment
*sticky*. A body that changed on every render would be indistinguishable from a
body that changed because the branch did, and the workflow that edits the
comment in place would rewrite it on every push for nothing.

The first line is an HTML comment holding :func:`marker`, which is how the
workflow finds the comment it wrote last time. It is keyed by the review's title
so two inventories reviewed in one repository keep one comment each.

**What renders and what does not.** GitHub sanitises comment HTML: an inline
``<svg>`` element is stripped, and ``<img src="data:...">`` is refused by the
image proxy. So the drawing appears three ways, in descending order of
availability — a Mermaid summary of the changeset, which always renders; an
``<img>`` for any diagram the caller published to a URL; and a link to the
uploaded artifact, which needs a session to download but is the full drawing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Final

from netgraph.diagnostics import Diagnostic
from netgraph.errors import count_text
from netgraph.plan.model import ACTION_SIGILS, Action, Change
from netgraph.review.model import ACTION_LABELS, KindSummary, Review, Verdict
from netgraph.rules import Severity

__all__ = [
    "MARKER_PREFIX",
    "marker",
    "render_comment",
]

#: What every marker starts with. The workflow greps for this to decide whether
#: a comment is one of ours before it looks at the title.
MARKER_PREFIX: Final = "<!-- netgraph-review:"

#: The one-word outcome, as a reader sees it at the top of the comment.
_VERDICT_BADGES: Final[dict[Verdict, str]] = {
    Verdict.PASSED: "✅",
    Verdict.WARNED: "⚠️",
    Verdict.FAILED: "❌",
    Verdict.BROKEN: "💥",
}

#: The severity of one finding, in the table.
_SEVERITY_BADGES: Final[dict[Severity, str]] = {
    Severity.ERROR: "❌ error",
    Severity.WARNING: "⚠️ warning",
    Severity.INFO: "💡 info",
}

#: Mermaid class per action, and the fill/stroke each is drawn with. The palette
#: is GitHub's own diff green/red/amber so the inline summary reads the same way
#: the rendered diff does.
_MERMAID_CLASSES: Final[dict[Action, tuple[str, str]]] = {
    Action.CREATE: ("added", "fill:#dafbe1,stroke:#1a7f37,color:#0e2f18"),
    Action.UPDATE: ("changed", "fill:#fff8c5,stroke:#9a6700,color:#341a00"),
    Action.RENAME: ("renamed", "fill:#ddf4ff,stroke:#0969da,color:#0a3069"),
    Action.DELETE: ("removed", "fill:#ffebe9,stroke:#cf222e,color:#4c0a0a"),
}


def marker(title: str) -> str:
    """The hidden line that identifies this comment among a pull request's.

    Keyed by title rather than by inventory path so that a repository which
    reviews two inventories names them itself, and a path that moves does not
    orphan the comment that was tracking it.
    """
    return f"{MARKER_PREFIX} {_plain(title)} -->"


def render_comment(review: Review) -> str:
    """The Markdown body of one review comment. Pure.

    The order is the order a reviewer reads in: the verdict, then what changed,
    then what is newly wrong with it, then the picture. Someone who reads only
    the first line has still been told whether to look further.
    """
    return "\n".join(_lines(review)).rstrip("\n") + "\n"


def _lines(review: Review) -> Iterator[str]:
    yield marker(review.title)
    yield ""
    yield f"### {_VERDICT_BADGES[review.verdict]} {review.title} — {_verdict_line(review)}"
    yield ""
    yield _provenance(review)
    yield ""
    for caveat in review.caveats:
        yield f"> ⚠️ {caveat}"
        yield ""

    if review.broken is not None:
        yield from _broken(review)
    else:
        yield from _changeset(review)

    yield from _findings(review)
    yield from _diagram(review)
    yield from _footer(review)


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


def _verdict_line(review: Review) -> str:
    """The half-sentence after the title: what changed, and what it broke.

    Both halves are always present when there is one to give, because either on
    its own misleads: "no new problems" over a rewrite of the whole network
    reads as "nothing happened".
    """
    if review.plan is None:
        return "the inventory does not load"
    if review.plan.empty:
        # Nothing changed and nothing is different about the findings either:
        # ", but no new problems" would be answering a question nobody asked.
        if review.delta.empty:
            return "no change to the network"
        return "no change to the network" + _problem_clause(review, joiner=", but ")
    changed = count_text(len(review.plan), "element")
    return f"{changed} change" + _problem_clause(review, joiner=", ")


def _problem_clause(review: Review, *, joiner: str) -> str:
    """``, 2 new errors``, ``, no new problems`` — the second half of the verdict."""
    delta = review.delta
    if not delta.new:
        fixed = f" ({count_text(len(delta.fixed), 'problem')} fixed)" if delta.fixed else ""
        return f"{joiner}no new problems{fixed}"
    parts = [
        count_text(delta.of(severity), _NOUNS[severity])
        for severity in Severity
        if delta.of(severity)
    ]
    return f"{joiner}**{_and_list(parts)}** introduced"


#: What each severity is called when it is counted in a sentence.
_NOUNS: Final[dict[Severity, str]] = {
    Severity.ERROR: "new error",
    Severity.WARNING: "new warning",
    Severity.INFO: "new info",
}


def _provenance(review: Review) -> str:
    """The line under the heading: which two states, and which inventory."""
    head = review.head or "this branch"
    parts = [f"`{_plain(review.base)}` → `{_plain(head)}`"]
    if review.prefix:
        parts.append(f"inventory `{_plain(review.prefix)}`")
    if review.strict:
        parts.append("`--strict`")
    if review.delta.base_absent:
        parts.append("**no inventory on the base**, so every finding below is new")
    return " · ".join(parts)


def _broken(review: Review) -> Iterator[str]:
    """Said instead of a changeset when the head state does not load.

    There is no plan to show — a document that was rejected is absent from the
    inventory, and diffing against it would read as a deletion — so the comment
    says what happened rather than reporting a change nobody made.
    """
    assert review.broken is not None
    yield f"> {review.broken}"
    yield ">"
    yield "> The changeset and the diagram are both omitted: a state that does not load"
    yield "> cannot be compared, and a plan built from a half-read tree would report"
    yield "> every rejected document as a deletion."
    yield ""


# --------------------------------------------------------------------------- #
# The changeset
# --------------------------------------------------------------------------- #


def _changeset(review: Review) -> Iterator[str]:
    plan = review.plan
    if plan is None or plan.empty:
        return
    kinds = review.kinds
    yield "#### What changes"
    yield ""
    yield from _kind_table(kinds)
    yield ""
    yield from _element_details(review, kinds)
    yield ""


def _kind_table(kinds: Sequence[KindSummary]) -> Iterator[str]:
    """One row per element kind, one column per action, totals at the bottom."""
    actions = tuple(ACTION_LABELS)
    yield "| Kind | " + " | ".join(ACTION_LABELS[action] for action in actions) + " |"
    yield "|---|" + "---:|" * len(actions)
    for summary in kinds:
        cells = " | ".join(_count_cell(summary.counts.get(action, 0)) for action in actions)
        yield f"| `{_cell(summary.kind)}` | {cells} |"
    if len(kinds) > 1:
        totals = " | ".join(
            _count_cell(sum(summary.counts.get(action, 0) for summary in kinds))
            for action in actions
        )
        yield f"| **total** | {totals} |"


def _count_cell(count: int) -> str:
    """A zero is a dash: a table of dashes with three numbers in it is scannable."""
    return str(count) if count else "-"


def _element_details(review: Review, kinds: Sequence[KindSummary]) -> Iterator[str]:
    """The per-element rows, folded away behind a summary.

    Collapsed because the kind table above is the answer for most readers, and a
    change touching two hundred cables would otherwise bury the findings under
    it.
    """
    changes = [change for summary in kinds for change in summary.changes]
    shown = changes[: review.max_changes]
    dropped = len(changes) - len(shown)
    yield "<details>"
    yield f"<summary>{count_text(len(changes), 'element')}, one by one</summary>"
    yield ""
    yield "| | Element | Kind | Detail |"
    yield "|---|---|---|---|"
    for change in shown:
        yield (
            f"| `{ACTION_SIGILS[change.action]}` | `{_cell(str(change.address))}` "
            f"| `{_cell(change.kind)}` | {_detail(change)} |"
        )
    yield ""
    if dropped:
        yield (
            f"…and {count_text(dropped, 'further element')}. The whole changeset is in the "
            f"`plan.json` of the run's artifact, or run `netgraph plan` locally."
        )
        yield ""
    yield "</details>"


def _detail(change: Change) -> str:
    """The right-hand cell: which fields moved, or what the whole element does."""
    if change.action is Action.RENAME:
        return f"renamed to `{_cell(str(change.new_address))}`"
    if change.action is Action.CREATE:
        return "new element"
    if change.action is Action.DELETE:
        return "element removed"
    if not change.fields:  # pragma: no cover - the differ emits no fieldless update
        return "changed"
    paths = [f"`{_cell(field.text)}`" for field in change.fields[:4]]
    if len(change.fields) > 4:
        paths.append(f"and {len(change.fields) - 4} more")
    return ", ".join(paths)


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #


def _findings(review: Review) -> Iterator[str]:
    """What this change broke — and, separately, what it fixed.

    Only the *new* findings get a table. The pre-existing ones are counted in a
    sentence and no more: naming them would put a repository's whole legacy in
    every comment, and none of it is the pull request's to answer for.
    """
    delta = review.delta
    yield "#### Validation"
    yield ""
    if not delta.new:
        yield f"No new findings. {_standing(review)}"
        yield ""
        return

    shown = delta.new[: review.max_findings]
    dropped = len(delta.new) - len(shown)
    yield "| | Rule | Where | What |"
    yield "|---|---|---|---|"
    for diagnostic in shown:
        yield (
            f"| {_SEVERITY_BADGES[diagnostic.severity]} | {_rule_cell(diagnostic)} "
            f"| {_where_cell(review, diagnostic)} | {_cell(diagnostic.message)} |"
        )
    yield ""
    if dropped:
        yield f"…and {count_text(dropped, 'further new finding')}."
        yield ""
    yield _standing(review)
    yield ""


def _standing(review: Review) -> str:
    """The sentence about everything that is not new.

    It always says something, because "nothing else" and "nothing at all" are
    different answers and a reader who is told neither will go and look.
    """
    delta = review.delta
    parts: list[str] = []
    if delta.fixed:
        parts.append(f"**{count_text(len(delta.fixed), 'pre-existing problem')} fixed** 🎉")
    if delta.carried:
        parts.append(
            f"{count_text(len(delta.carried), 'pre-existing finding')} left alone — the "
            f"check does not fail on what this change did not do"
        )
    if parts:
        return f"Also: {_and_list(parts)}."
    if delta.new:
        return "Nothing else was reported: the base state is clean."
    return "The inventory is clean on both sides."


def _rule_cell(diagnostic: Diagnostic) -> str:
    """The rule id, linked to its write-up, with the schema alias beside it."""
    identifier = diagnostic.rule
    help_uri = diagnostic.descriptor.help_uri
    text = f"[`{identifier}`]({help_uri})" if help_uri else f"`{identifier}`"
    return f"{text} `{diagnostic.alias}`" if diagnostic.alias else text


def _where_cell(review: Review, diagnostic: Diagnostic) -> str:
    """The file and line, linked to the commit when the repository is known."""
    path = review.path_of(diagnostic)
    if path is None:
        return f"`{_cell(diagnostic.element)}`" if diagnostic.element else "—"
    where = f"{path}:{diagnostic.line}" if diagnostic.line is not None else path
    link = review.link_to(diagnostic)
    return f"[`{_cell(where)}`]({link})" if link else f"`{_cell(where)}`"


# --------------------------------------------------------------------------- #
# The drawing
# --------------------------------------------------------------------------- #


def _diagram(review: Review) -> Iterator[str]:
    """The picture, in whichever of the three forms are available."""
    if review.plan is None or review.plan.empty:
        return
    yield "#### The change, drawn"
    yield ""
    yield from _mermaid(review)
    yield ""
    for diagram in review.diagrams:
        if diagram.embeddable:
            yield f'<img src="{diagram.url}" alt="{_plain(review.title)} diff, {diagram.label}">'
            yield ""
    links = _artifact_links(review)
    if links:
        yield links
        yield ""


def _artifact_links(review: Review) -> str:
    """One line pointing at the full drawing, wherever it was uploaded."""
    if review.artifact_url is None:
        return ""
    formats = ", ".join(diagram.label for diagram in review.diagrams if diagram.path)
    name = review.artifact_name or "the run artifact"
    what = f"the full diagram ({formats})" if formats else "the full diagram"
    return f"📎 [Download {what}]({review.artifact_url}) from `{_plain(name)}`."


def _mermaid(review: Review) -> Iterator[str]:
    """The changeset as a Mermaid flowchart, grouped into namespaces.

    Mermaid rather than the rendered SVG because GitHub strips ``<svg>`` from
    comment bodies and refuses a ``data:`` image, so this is the only drawing
    that is certain to appear. It draws the *changed* elements only — the whole
    topology belongs in the artifact, where it is not competing with the text.
    """
    plan = review.plan
    assert plan is not None
    changes = [change for summary in review.kinds for change in summary.changes]
    shown = changes[: review.max_nodes]
    dropped = len(changes) - len(shown)

    yield "```mermaid"
    yield "flowchart LR"
    node = 0
    used: set[Action] = set()
    for position, (group, members) in enumerate(_by_namespace(shown)):
        indent = "  "
        if group:
            yield f'  subgraph ns{position} ["{_label(group)}"]'
            indent = "    "
        for change in members:
            style = _MERMAID_CLASSES[change.action][0]
            yield f'{indent}n{node}["{_node_label(change)}"]:::{style}'
            node += 1
            used.add(change.action)
        if group:
            yield "  end"
    if dropped:
        yield f'  more["… {count_text(dropped, "further element")}"]'
    for action in ACTION_LABELS:
        if action in used:
            name, style = _MERMAID_CLASSES[action]
            yield f"  classDef {name} {style};"
    yield "```"


def _by_namespace(changes: Sequence[Change]) -> Iterable[tuple[str, list[Change]]]:
    """``changes`` grouped by the namespace of the address, in first-seen order."""
    groups: dict[str, list[Change]] = {}
    for change in changes:
        groups.setdefault(change.address.namespace, []).append(change)
    return list(groups.items())


def _node_label(change: Change) -> str:
    """``+ pc-new`` — the sigil and the local name, the namespace being the box."""
    name = change.address.name
    if change.action is Action.RENAME and change.new_address is not None:
        name = f"{name} → {change.new_address.name}"
    return _label(f"{ACTION_SIGILS[change.action]} {name}")


# --------------------------------------------------------------------------- #
# The footer
# --------------------------------------------------------------------------- #


def _footer(review: Review) -> Iterator[str]:
    version = f"netgraph {review.version}" if review.version else "netgraph"
    yield (
        f"<sub>{version} · this comment is edited in place on every push · "
        f"reproduce it with <code>netgraph review --from {_plain(review.base)}</code></sub>"
    )


# --------------------------------------------------------------------------- #
# Escaping
# --------------------------------------------------------------------------- #


def _cell(text: str) -> str:
    """``text`` as one Markdown table cell.

    A pipe would end the cell and a newline would end the row, and both reach
    here from an inventory: a finding's message quotes a value the author wrote.
    Backticks are neutralised too, since most cells are already code-spanned.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "'")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )


def _plain(text: str) -> str:
    """``text`` with the characters that would end an HTML attribute or comment.

    The marker is an HTML comment and the ``alt`` of an image is an attribute;
    a title holding ``-->`` or a quote would break out of either.
    """
    return (
        text.replace("<", "").replace(">", "").replace('"', "'").replace("\n", " ").strip() or "?"
    )


def _label(text: str) -> str:
    """``text`` as a Mermaid node label inside double quotes.

    Mermaid has no backslash escape inside a quoted string; a literal quote is
    written as the HTML entity, which its own parser substitutes back. This is
    not :func:`_plain`, which turns a quote into an apostrophe: that is right for
    an HTML attribute and would silently rewrite an element's name here.
    """
    flattened = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
    return (
        flattened.replace("<", "").replace(">", "").replace('"', "#quot;").replace("]", "") or "?"
    )


def _and_list(parts: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if not parts:  # pragma: no cover - every caller has already checked
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"
