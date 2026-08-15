"""The pull-request review: ``netgraph.review`` and ``netgraph review``.

The comment body is a *pure function* of a
:class:`~netgraph.review.model.Review`, and almost everything here exercises it
that way — a changeset and two lists of diagnostics in, Markdown out, with no
repository, no GitHub and no clock anywhere near it. That split is the point of
the module: the formatting of the thing a team reads on every pull request is
covered by ordinary tests rather than by pushing a branch and looking.

Four properties carry the feature.

* **Only new problems count.** The delta is measured against the base by the
  same fingerprint code scanning tracks alerts by, so a repository with a legacy
  warning adopts the bot green and the next warning turns it red.
* **The body is deterministic.** The same review renders byte-identically, which
  is what makes a comment that is edited in place meaningful rather than noisy.
* **The first line is the marker**, keyed by title, because that is how the
  workflow finds the comment it wrote last time.
* **Nothing an inventory holds can break the Markdown.** A finding's message
  quotes values a stranger wrote; a pipe in one of them must not end a table row.

The golden body in ``tests/fixtures/review/`` pins the whole document. Regenerate
it with::

    pytest tests/test_review.py --regen-golden
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.diagnostics import Diagnostic, as_sarif, build_report, fingerprint
from netgraph.plan.address import parse_address
from netgraph.plan.model import Action, Change, FieldChange, Plan, StateRef
from netgraph.plan.paths import parse_path
from netgraph.review import (
    MARKER_PREFIX,
    Diagram,
    FindingDelta,
    Review,
    Verdict,
    compute_delta,
    group_changes,
    marker,
    render_comment,
    summarise,
)
from netgraph.rules import Severity

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples" / "home-lab"
GOLDEN = Path(__file__).parent / "fixtures" / "review" / "home-lab.md"


# --------------------------------------------------------------------------- #
# Building the values under test
# --------------------------------------------------------------------------- #


def change(
    action: Action,
    address: str,
    kind: str,
    *,
    fields: tuple[str, ...] = (),
    new_address: str | None = None,
) -> Change:
    return Change(
        action=action,
        address=parse_address(address),
        kind=kind,
        fields=tuple(FieldChange(path=parse_path(path), before=1, after=2) for path in fields),
        new_address=None if new_address is None else parse_address(new_address),
    )


def plan_of(*changes: Change) -> Plan:
    return Plan(
        changes=changes,
        source=StateRef(kind="git", description="origin/main"),
        target=StateRef(kind="tree", description="the working tree"),
    )


def finding(
    rule: str = "W103",
    severity: Severity = Severity.WARNING,
    message: str = "something is off",
    *,
    file: str | None = "hosts/pc.yaml",
    line: int | None = 12,
    element: str | None = "hosts/pc",
    pointer: str | None = None,
) -> Diagnostic:
    return Diagnostic(
        rule=rule,
        severity=severity,
        message=message,
        element=element,
        file=file,
        line=line,
        pointer=pointer,
    )


def review_of(**overrides: object) -> Review:
    """A review with everything filled in, for a test to override one field of."""
    settings: dict[str, object] = {
        "plan": plan_of(
            change(Action.CREATE, "device.hosts/pc-new", "computer"),
            change(Action.UPDATE, "device.hosts/laptop", "computer", fields=("spec.mtu",)),
            change(Action.DELETE, "cable.cables/cbl-1", "cable"),
        ),
        "delta": FindingDelta(new=(finding(),)),
        "base": "origin/main",
        "prefix": "inventory",
        "version": "0.1.0",
    }
    settings.update(overrides)
    return Review(**settings)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The marker, and stickiness
# --------------------------------------------------------------------------- #


def test_the_body_begins_with_the_marker_the_workflow_looks_for() -> None:
    body = render_comment(review_of(title="netgraph (campus)"))
    assert body.splitlines()[0] == marker("netgraph (campus)")
    assert body.startswith(MARKER_PREFIX)


def test_two_titles_are_two_markers() -> None:
    """One repository reviewing two inventories keeps two comments."""
    assert marker("campus") != marker("home-lab")


def test_a_title_cannot_break_out_of_the_marker() -> None:
    """The marker is an HTML comment and the title reaches it from a workflow input."""
    hostile = marker("evil --> <script>alert(1)</script>")
    assert hostile.count("-->") == 1
    assert "<script>" not in hostile


def test_the_same_review_renders_byte_identically() -> None:
    """A body that changed on its own would rewrite the comment on every push."""
    assert render_comment(review_of()) == render_comment(review_of())


def test_the_body_ends_with_exactly_one_newline() -> None:
    body = render_comment(review_of())
    assert body.endswith("\n") and not body.endswith("\n\n")


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (FindingDelta(), Verdict.PASSED),
        (FindingDelta(new=(finding(),)), Verdict.WARNED),
        (FindingDelta(new=(finding("E001", Severity.ERROR),)), Verdict.FAILED),
    ],
)
def test_the_verdict_follows_the_delta_and_not_the_report(
    delta: FindingDelta, expected: Verdict
) -> None:
    assert review_of(delta=delta).verdict is expected


def test_a_head_that_does_not_load_is_its_own_verdict() -> None:
    review = review_of(plan=None, broken="The head state does not load: 1 document rejected.")
    assert review.verdict is Verdict.BROKEN
    assert not review.changed


def test_pre_existing_findings_do_not_fail_the_check() -> None:
    """The whole reason a repository with legacy warnings can adopt this."""
    carried = tuple(finding("E001", Severity.ERROR, f"broken {n}") for n in range(3))
    review = review_of(delta=FindingDelta(carried=carried))
    assert review.verdict is Verdict.PASSED
    body = render_comment(review)
    assert "No new findings" in body
    assert "3 pre-existing findings left alone" in body
    assert "does not fail on what this change did not do" in body


def test_the_verdict_line_counts_both_halves() -> None:
    body = render_comment(review_of(delta=FindingDelta(new=(finding("E001", Severity.ERROR),))))
    heading = body.splitlines()[2]
    assert "3 elements change" in heading
    assert "1 new error" in heading


def test_an_unchanged_branch_with_nothing_new_says_only_that() -> None:
    body = render_comment(review_of(plan=plan_of(), delta=FindingDelta()))
    assert "no change to the network" in body.splitlines()[2]
    assert "no new problems" not in body


def test_a_fix_is_reported_even_when_nothing_else_is() -> None:
    body = render_comment(review_of(plan=plan_of(), delta=FindingDelta(fixed=(finding(),))))
    assert "1 problem fixed" in body
    assert "🎉" in body


# --------------------------------------------------------------------------- #
# The changeset table
# --------------------------------------------------------------------------- #


def test_the_changeset_is_grouped_by_kind_alphabetically() -> None:
    summaries = group_changes(
        plan_of(
            change(Action.CREATE, "device.a/one", "computer"),
            change(Action.DELETE, "cable.a/c1", "cable"),
            change(Action.DELETE, "cable.a/c2", "cable"),
        )
    )
    assert [summary.kind for summary in summaries] == ["cable", "computer"]
    assert summaries[0].counts[Action.DELETE] == 2
    assert summaries[0].total == 2


def test_the_kind_table_has_a_row_per_kind_and_a_total() -> None:
    body = render_comment(review_of())
    assert "| `cable` | - | - | - | 1 |" in body
    assert "| `computer` | 1 | 1 | - | - |" in body
    assert "| **total** | 1 | 1 | - | 1 |" in body


def test_one_kind_needs_no_total_row() -> None:
    body = render_comment(
        review_of(plan=plan_of(change(Action.CREATE, "device.a/one", "computer")))
    )
    assert "**total**" not in body


def test_a_rename_names_both_ends() -> None:
    body = render_comment(
        review_of(
            plan=plan_of(
                change(Action.RENAME, "device.a/old", "computer", new_address="device.a/new")
            )
        )
    )
    assert "renamed to `device.a/new`" in body
    assert "| `computer` | - | - | 1 | - |" in body


def test_an_update_names_the_fields_that_moved() -> None:
    body = render_comment(
        review_of(
            plan=plan_of(
                change(
                    Action.UPDATE,
                    "device.a/sw",
                    "switch",
                    fields=("spec.vendor", "spec.model", "spec.mtu", "spec.location", "spec.role"),
                )
            )
        )
    )
    assert "`spec.vendor`, `spec.model`, `spec.mtu`, `spec.location`, and 1 more" in body


def test_the_element_table_is_bounded_and_says_what_it_dropped() -> None:
    changes = [change(Action.CREATE, f"device.a/host-{n}", "computer") for n in range(30)]
    body = render_comment(review_of(plan=plan_of(*changes), max_changes=5))
    assert body.count("| `+` |") == 5
    assert "…and 25 further elements" in body
    assert "plan.json" in body


# --------------------------------------------------------------------------- #
# The findings table
# --------------------------------------------------------------------------- #


def test_a_finding_links_to_its_line_when_the_commit_is_known() -> None:
    body = render_comment(review_of(repository_url="https://github.com/o/r/", head_sha="abc123"))
    assert "https://github.com/o/r/blob/abc123/inventory/hosts/pc.yaml#L12" in body


def test_a_finding_without_a_commit_is_named_and_not_linked() -> None:
    """A link built against a branch would point at whatever it says next week."""
    body = render_comment(review_of())
    assert "`inventory/hosts/pc.yaml:12`" in body
    assert "](https://github.com/o/r" not in body


def test_the_rule_is_linked_to_its_write_up() -> None:
    body = render_comment(review_of(delta=FindingDelta(new=(finding("E001", Severity.ERROR),))))
    assert "docs/validation-rules.md#e001" in body


def test_the_findings_table_is_bounded_too() -> None:
    new = tuple(finding(message=f"problem {n}") for n in range(40))
    body = render_comment(review_of(delta=FindingDelta(new=new), max_findings=3))
    assert body.count("⚠️ warning |") == 3
    assert "…and 37 further new findings" in body


@pytest.mark.parametrize(
    "hostile",
    [
        "a | b",
        "line one\nline two",
        "back`tick",
        "carriage\r\nreturn",
    ],
)
def test_nothing_in_a_message_can_end_a_table_row(hostile: str) -> None:
    """Messages quote values a stranger wrote into an inventory."""
    body = render_comment(review_of(delta=FindingDelta(new=(finding(message=hostile),))))
    row = next(line for line in body.splitlines() if "warning" in line and line.startswith("|"))
    # Four columns, so five cell separators -- and an escaped pipe is not one.
    assert row.replace("\\|", "").count("|") == 5, row
    assert "\n" not in row


def test_a_finding_with_no_file_still_has_a_row() -> None:
    body = render_comment(
        review_of(delta=FindingDelta(new=(finding(file=None, line=None, element="a/b"),)))
    )
    assert "| `a/b` |" in body


# --------------------------------------------------------------------------- #
# The drawing
# --------------------------------------------------------------------------- #


def test_the_inline_summary_is_mermaid_grouped_by_namespace() -> None:
    body = render_comment(review_of())
    assert "```mermaid" in body
    # Grouped by kind first, so the cable's namespace leads and the hosts follow.
    assert 'subgraph ns0 ["cables"]' in body
    assert 'subgraph ns1 ["hosts"]' in body
    assert 'n1["+ pc-new"]:::added' in body
    assert "classDef added" in body
    # Only the classes that were used: an unused classDef is noise in a diagram
    # somebody has to read on a phone.
    assert "classDef renamed" not in body


def test_the_mermaid_summary_is_bounded() -> None:
    changes = [change(Action.CREATE, f"device.a/host-{n}", "computer") for n in range(40)]
    body = render_comment(review_of(plan=plan_of(*changes), max_nodes=4))
    assert body.count(":::added") == 4
    assert 'more["… 36 further elements"]' in body


def test_a_quote_in_a_name_cannot_break_a_mermaid_label() -> None:
    body = render_comment(
        review_of(plan=plan_of(change(Action.CREATE, 'device.a/pc"one', "computer")))
    )
    mermaid = body.split("```mermaid")[1].split("```")[0]
    assert '"+ pc#quot;one"' in mermaid


def test_a_published_diagram_is_embedded_and_an_artifact_is_linked() -> None:
    body = render_comment(
        review_of(
            diagrams=(
                Diagram(label="SVG", path="diff.svg", url="https://example.test/diff.svg"),
                Diagram(label="PNG", path="diff.png"),
            ),
            artifact_url="https://example.test/run/1#artifacts",
            artifact_name="netgraph-review",
        )
    )
    assert '<img src="https://example.test/diff.svg"' in body
    assert "[Download the full diagram (SVG, PNG)](https://example.test/run/1#artifacts)" in body


def test_nothing_is_drawn_when_nothing_changed() -> None:
    body = render_comment(review_of(plan=plan_of(), delta=FindingDelta()))
    assert "```mermaid" not in body
    assert "The change, drawn" not in body


def test_a_broken_head_shows_no_changeset_and_no_drawing() -> None:
    body = render_comment(review_of(plan=None, broken="The head state does not load: 1 rejected."))
    assert "```mermaid" not in body
    assert "What changes" not in body
    assert "does not load" in body


def test_a_caveat_is_quoted_under_the_heading() -> None:
    body = render_comment(review_of(caveats=("The base state does not load either.",))).splitlines()
    assert "> ⚠️ The base state does not load either." in body


def test_a_base_with_no_inventory_says_so() -> None:
    body = render_comment(review_of(delta=FindingDelta(new=(finding(),), base_absent=True)))
    assert "no inventory on the base" in body


def test_strict_is_declared() -> None:
    assert "`--strict`" in render_comment(review_of(strict=True))


# --------------------------------------------------------------------------- #
# The delta
# --------------------------------------------------------------------------- #


def test_a_finding_present_on_both_sides_is_carried() -> None:
    shared = finding()
    delta = compute_delta([shared], [shared])
    assert delta.carried == (shared,) and not delta.new and not delta.fixed


def test_a_finding_the_head_no_longer_has_is_fixed() -> None:
    gone = finding()
    delta = compute_delta([gone], [])
    assert delta.fixed == (gone,) and not delta.new


def test_the_delta_counts_duplicates_rather_than_collapsing_them() -> None:
    """Two of the same problem on the head and one on the base leaves one new."""
    same = finding()
    delta = compute_delta([same], [same, same])
    assert len(delta.new) == 1
    assert len(delta.carried) == 1


def test_moving_a_document_down_a_file_introduces_nothing() -> None:
    """Identity deliberately excludes the line; see ``diagnostics.fingerprint``."""
    before = finding(line=12)
    after = finding(line=99)
    assert compute_delta([before], [after]).new == ()


def test_the_delta_uses_the_identity_code_scanning_tracks_alerts_by() -> None:
    """The bot and the alert list must not disagree about which problem is new."""
    one = finding()
    other = finding(message="a different problem")
    assert fingerprint(one) != fingerprint(other)
    assert compute_delta([one], [other]).new == (other,)


def test_severity_counts_are_per_severity() -> None:
    delta = FindingDelta(
        new=(
            finding("E001", Severity.ERROR),
            finding("W103", Severity.WARNING),
            finding("W104", Severity.WARNING),
        )
    )
    assert delta.of(Severity.ERROR) == 1
    assert delta.of(Severity.WARNING) == 2
    assert len(delta.new_errors) == 1


# --------------------------------------------------------------------------- #
# The summary document
# --------------------------------------------------------------------------- #


def test_the_summary_is_what_a_workflow_gates_on() -> None:
    summary = summarise(review_of(delta=FindingDelta(new=(finding("E001", Severity.ERROR),))))
    assert summary["verdict"] == "failed"
    assert summary["changed"] is True
    assert summary["changes"] == 3
    assert summary["new"] == {"error": 1, "warning": 0, "info": 0, "total": 1}
    assert summary["broken"] is False


def test_the_summary_of_a_broken_head_says_broken() -> None:
    summary = summarise(review_of(plan=None, broken="nope"))
    assert summary["verdict"] == "broken"
    assert summary["broken"] is True
    assert summary["changes"] == 0


# --------------------------------------------------------------------------- #
# The whole document, pinned
# --------------------------------------------------------------------------- #


def test_the_rendered_comment_is_what_it_was(regen_golden: bool) -> None:
    """One golden body, so that a change to any part of the layout is visible."""
    review = review_of(
        title="netgraph (home-lab)",
        delta=FindingDelta(
            new=(
                finding(
                    "E001",
                    Severity.ERROR,
                    "cable 'cables/cbl-1' endpoint pc-desk:eno1: no element named 'pc-desk'",
                    file="cables/links.yaml",
                    line=28,
                    element="cables/cbl-1",
                ),
            ),
            fixed=(finding("W104", Severity.WARNING, "an old problem"),),
            carried=(finding("I002", Severity.INFO, "a standing note"),),
        ),
        diagrams=(Diagram(label="SVG", path="diff.svg"), Diagram(label="PNG", path="diff.png")),
        artifact_url="https://github.com/o/r/actions/runs/42#artifacts",
        artifact_name="netgraph-review",
        repository_url="https://github.com/o/r",
        head_sha="0123456789abcdef",
    )
    body = render_comment(review)
    if regen_golden:  # pragma: no cover - only under --regen-golden
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(body, encoding="utf-8")
    assert body == GOLDEN.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The command, over a real repository
# --------------------------------------------------------------------------- #


class Repo:
    """A git repository with the home lab in it, built a commit at a time."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.inventory = root / "net"
        self.git("init", "-q", ".")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "Tester")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *arguments: str) -> str:
        done = subprocess.run(["git", *arguments], cwd=self.root, check=True, capture_output=True)
        return done.stdout.decode("utf-8", "replace").strip()

    def commit(self, subject: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", subject)
        return self.git("rev-parse", "HEAD")

    def write(self, relative: str, text: str) -> None:
        target = self.inventory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def seed(self) -> str:
        self.inventory.mkdir(parents=True, exist_ok=True)
        shutil.copytree(EXAMPLES, self.inventory, dirs_exist_ok=True)
        return self.commit("Bring the home lab under description")


DANGLING_CABLE = """apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: cbl-nowhere
spec:
  endpoints:
    - rtr-home:lan0
    - nothing-at-all:eth0
  medium: copper
"""

NEW_DEVICE = """apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-new
  labels:
    site: home
spec:
  interfaces:
    - name: eth0
      type: ethernet
      enabled: false
"""


@pytest.fixture(autouse=True, scope="module")
def _needs_git() -> None:
    if shutil.which("git") is None:  # pragma: no cover - git is on every runner
        pytest.skip("git is not installed")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    built = Repo(tmp_path / "work")
    built.seed()
    return built


def run(repo: Repo, *arguments: str) -> object:
    runner = CliRunner()
    return runner.invoke(
        cli,
        ["--inventory", str(repo.inventory), "review", *arguments],
        catch_exceptions=False,
    )


def test_a_branch_that_changes_nothing_passes(repo: Repo) -> None:
    result = run(repo, "--from", "HEAD")
    assert result.exit_code == 0, result.output
    assert "no change to the network" in result.output


def test_an_added_device_is_reported_as_a_change(repo: Repo) -> None:
    repo.write("hosts/newpc.yaml", NEW_DEVICE)
    result = run(repo, "--from", "HEAD")
    assert result.exit_code == 0, result.output
    assert "1 element change" in result.output
    assert "`device.hosts/pc-new`" in result.output


def test_a_new_error_fails_and_a_pre_existing_one_does_not(repo: Repo, tmp_path: Path) -> None:
    """The property the whole feature turns on."""
    # Break the inventory *on the base*, so the error is there before the change.
    (repo.inventory / "hosts" / "pc-desk.yaml").unlink()
    repo.commit("Remove a device the cabling still names")

    summary = tmp_path / "summary.json"
    clean = run(repo, "--from", "HEAD", "--summary-out", str(summary))
    assert clean.exit_code == 0, clean.output
    assert "pre-existing" in clean.output
    standing = json.loads(summary.read_text(encoding="utf-8"))
    assert standing["new"]["total"] == 0
    assert standing["carried"] >= 1, "the base's own error should be carried, not new"

    # Now add a *second* dangling reference, and only that one is reported.
    repo.write("cables/extra.yaml", DANGLING_CABLE)
    introduced = run(repo, "--from", "HEAD", "--summary-out", str(summary))
    assert introduced.exit_code == 1
    assert "nothing-at-all" in introduced.output
    assert "pc-desk" not in introduced.output.split("#### Validation")[1].split("Also:")[0]
    after = json.loads(summary.read_text(encoding="utf-8"))
    assert after["new"]["error"] >= 1
    assert after["carried"] == standing["carried"]


def test_fail_on_never_exits_zero_whatever_it_found(repo: Repo) -> None:
    repo.write("cables/extra.yaml", DANGLING_CABLE)
    result = run(repo, "--from", "HEAD", "--fail-on", "never")
    assert result.exit_code == 0, result.output


def test_fail_on_changes_gates_on_the_changeset(repo: Repo) -> None:
    repo.write("hosts/newpc.yaml", NEW_DEVICE)
    assert run(repo, "--from", "HEAD", "--fail-on", "changes").exit_code == 1
    (repo.inventory / "hosts" / "newpc.yaml").unlink()
    assert run(repo, "--from", "HEAD", "--fail-on", "changes").exit_code == 0


def test_a_head_that_does_not_load_fails_and_says_which_document(repo: Repo) -> None:
    repo.write("hosts/bad.yaml", "apiVersion: netgraph.dev/v1alpha1\nkind: nonsense\n")
    result = run(repo, "--from", "HEAD")
    assert result.exit_code == 1
    assert "the inventory does not load" in result.output
    assert "bad.yaml" in result.output


def test_the_side_documents_are_written_and_agree_with_the_comment(
    repo: Repo, tmp_path: Path
) -> None:
    repo.write("hosts/newpc.yaml", NEW_DEVICE)
    body = tmp_path / "comment.md"
    plan = tmp_path / "plan.json"
    sarif = tmp_path / "netgraph.sarif"
    summary = tmp_path / "summary.json"
    result = run(
        repo,
        "--from",
        "HEAD",
        "--output",
        str(body),
        "--plan-out",
        str(plan),
        "--sarif-out",
        str(sarif),
        "--summary-out",
        str(summary),
    )
    assert result.exit_code == 0, result.output

    written = json.loads(summary.read_text(encoding="utf-8"))
    assert written["changed"] is True
    assert written["changes"] == json.loads(plan.read_text(encoding="utf-8"))["summary"]["total"]
    assert json.loads(sarif.read_text(encoding="utf-8"))["version"] == "2.1.0"
    assert body.read_text(encoding="utf-8").startswith(MARKER_PREFIX)


def test_a_base_with_no_inventory_is_reviewed_rather_than_refused(tmp_path: Path) -> None:
    """The first pull request a repository ever sees has no base to compare to."""
    repo = Repo(tmp_path / "empty")
    (repo.root / "README.md").write_text("nothing here yet\n", encoding="utf-8")
    repo.commit("Empty")
    repo.seed()

    result = run(repo, "--from", "HEAD~1", "--fail-on", "never")
    assert result.exit_code == 0, result.output
    assert "no inventory on the base" in result.output


def test_a_ref_that_does_not_exist_is_a_clean_failure(repo: Repo) -> None:
    result = run(repo, "--from", "no-such-ref")
    assert result.exit_code == 1
    assert "no-such-ref" in result.output


def test_a_diagram_label_defaults_to_the_suffix(repo: Repo, tmp_path: Path) -> None:
    repo.write("hosts/newpc.yaml", NEW_DEVICE)
    drawing = tmp_path / "diff.svg"
    drawing.write_text("<svg/>", encoding="utf-8")
    result = run(
        repo,
        "--from",
        "HEAD",
        "--diagram",
        str(drawing),
        "--artifact-url",
        "https://example.test/run#artifacts",
    )
    assert result.exit_code == 0, result.output
    assert "the full diagram (SVG)" in result.output


def test_an_empty_diagram_is_a_usage_error(repo: Repo) -> None:
    result = run(repo, "--from", "HEAD", "--diagram", "")
    assert result.exit_code == 2
    assert "empty value" in result.output


def test_disable_applies_to_both_sides(repo: Repo) -> None:
    """A rule silenced in this very change must not read as a wave of fixes."""
    repo.write("hosts/newpc.yaml", NEW_DEVICE)
    result = run(repo, "--from", "HEAD", "--disable", "W103", "--fail-on", "never")
    assert result.exit_code == 0, result.output
    assert "W103" not in result.output


def test_the_sarif_and_the_comment_come_from_one_validation(repo: Repo, tmp_path: Path) -> None:
    """Two validations could disagree; the command runs one and shares it."""
    repo.write("hosts/newpc.yaml", NEW_DEVICE)
    sarif = tmp_path / "netgraph.sarif"
    assert run(repo, "--from", "HEAD", "--sarif-out", str(sarif)).exit_code == 0

    from netgraph.loader import load_tree
    from netgraph.validate import validate

    inventory = load_tree(repo.inventory)
    expected = as_sarif(build_report(inventory, validate(inventory)))
    written = json.loads(sarif.read_text(encoding="utf-8"))
    assert [r["ruleId"] for r in written["runs"][0]["results"]] == [
        r["ruleId"] for r in expected["runs"][0]["results"]
    ]


def test_two_folders_can_be_compared_without_a_repository(tmp_path: Path) -> None:
    """``--to`` reads the head from somewhere other than the working tree."""
    before = tmp_path / "before"
    after = tmp_path / "after"
    shutil.copytree(EXAMPLES, before)
    shutil.copytree(EXAMPLES, after)
    (after / "hosts" / "newpc.yaml").write_text(NEW_DEVICE, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--inventory", str(before), "review", "--from", str(before), "--to", str(after)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "`device.hosts/pc-new`" in result.output


def test_a_base_that_does_not_load_is_reviewed_with_a_caveat(repo: Repo) -> None:
    """A branch that was already broken is exactly what this has to cope with."""
    repo.write("hosts/bad.yaml", "apiVersion: netgraph.dev/v1alpha1\nkind: nonsense\n")
    repo.commit("Break it")
    (repo.inventory / "hosts" / "bad.yaml").unlink()

    result = run(repo, "--from", "HEAD", "--fail-on", "never")
    assert result.exit_code == 0, result.output
    assert "The base state does not load either" in result.output


def test_a_finding_survives_a_round_trip_through_the_json_envelope() -> None:
    """``from_record`` is how one run's report reaches another's delta."""
    original = finding("E001", Severity.ERROR, "a problem", pointer="/spec/mtu")
    restored = Diagnostic.from_record(original.as_record())
    assert restored == original
    assert fingerprint(restored) == fingerprint(original)


def test_a_record_missing_a_required_field_is_refused() -> None:
    with pytest.raises(ValueError, match="needs"):
        Diagnostic.from_record({"severity": "error", "message": "no rule here"})


def test_a_position_that_is_not_a_number_is_dropped_rather_than_fatal() -> None:
    """Some other tool's document; the position is decoration, the rule is not."""
    restored = Diagnostic.from_record(
        {"rule": "E001", "severity": "error", "message": "m", "line": "twelve", "document": True}
    )
    assert restored.line is None and restored.index is None
