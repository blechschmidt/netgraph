"""The history timeline: the git plumbing, ``netgraph log``, and the editor's frames.

Every test here builds its own repository. A fixture repository committed to
this one would be a repository inside a repository — ``git`` refuses to be told
about it cleanly, and the interesting shapes (a revision that does not load, a
revision from before the inventory folder existed, a root commit) are three
lines to make and impossible to keep readable as a checked-in tree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.config import DEFAULT_MAX_REVISIONS, parse_config
from netgraph.errors import ConfigurationError
from netgraph.history import (  # the log format, under test below
    _FIELD,
    _RECORD,
    Commit,
    FrameCache,
    HistoryError,
    Timeline,
    _parse_log,
    summarise,
)
from netgraph.loader import load_tree
from netgraph.plan import diff as diff_states
from netgraph.plan.address import parse_address
from netgraph.plan.sources import (
    MissingInventory,
    PlanSourceError,
    check_revision,
    git_ref,
)
from netgraph.web.preview import ViewOptions
from netgraph.web.session import EditingSession, SessionError

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "home-lab"

#: A minimal second switch, for a commit that adds a device.
LAB_SWITCH = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-lab
spec:
  interfaces:
    - name: eth1
      type: ethernet
"""


# --------------------------------------------------------------------------- #
# Building a repository to read
# --------------------------------------------------------------------------- #


class Repo:
    """A git repository with an inventory in it, built a commit at a time."""

    def __init__(self, root: Path, prefix: str = "net") -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.prefix = prefix
        self.inventory = root / prefix if prefix else root
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

    def remove(self, relative: str) -> None:
        (self.inventory / relative).unlink()

    def seed(self) -> str:
        """Copy the home lab in and commit it."""
        self.inventory.mkdir(parents=True, exist_ok=True)
        shutil.copytree(EXAMPLES, self.inventory, dirs_exist_ok=True)
        return self.commit("Bring the home lab under description")


@pytest.fixture(autouse=True, scope="module")
def _needs_git() -> None:
    if shutil.which("git") is None:  # pragma: no cover - git is on every runner
        pytest.skip("git is not installed")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """A repository whose ``net/`` folder holds the home lab, one commit deep."""
    built = Repo(tmp_path / "work")
    built.seed()
    return built


@pytest.fixture
def history(tmp_path: Path) -> Repo:
    """Five commits: describe, change, add, break, unbreak."""
    built = Repo(tmp_path / "work")
    built.seed()

    router = built.inventory / "routers" / "rtr-home.yaml"
    router.write_text(
        router.read_text(encoding="utf-8").replace("model: ", "model: RB5009 "),
        encoding="utf-8",
    )
    built.commit("Swap the edge router")

    built.write("switches/sw-lab.yaml", LAB_SWITCH)
    built.commit("Add a lab switch")

    built.write("broken.yaml", "this: [is not\n")
    built.commit("Break the tree on purpose")

    built.remove("broken.yaml")
    built.commit("Unbreak the tree")
    return built


# --------------------------------------------------------------------------- #
# The plumbing
# --------------------------------------------------------------------------- #


def test_a_utc_stamp_is_read_whichever_way_git_spelled_it() -> None:
    """``%aI`` is ``+00:00`` in most builds of git and ``Z`` in some.

    Both are ISO 8601 and both are the same instant, and Python only accepts
    both from 3.11: on 3.10 the ``Z`` form made every timeline command raise
    ``ValueError: Invalid isoformat string``, which is exactly the kind of
    difference a CI matrix exists to find and a test exists to keep found.
    """

    def one(stamp: str) -> Commit:
        record = _FIELD.join(("a" * 40, "", "Tester", "t@example.invalid", stamp, "Subject"))
        return next(iter(_parse_log(record + _RECORD)))

    assert one("2026-08-14T15:47:25Z").when == one("2026-08-14T15:47:25+00:00").when
    assert one("2026-08-14T15:47:25Z").when.utcoffset() is not None
    # An offset that is not UTC is left exactly alone.
    assert one("2026-08-14T15:47:25+02:00").when.hour == 15


def test_a_timeline_lists_the_commits_that_touched_the_inventory(history: Repo) -> None:
    """Newest first, with the subject, author and date a scrubber shows."""
    timeline = Timeline.open(history.inventory)
    commits = timeline.commits()

    assert [commit.subject for commit in commits] == [
        "Unbreak the tree",
        "Break the tree on purpose",
        "Add a lab switch",
        "Swap the edge router",
        "Bring the home lab under description",
    ]
    assert all(commit.author == "Tester" for commit in commits)
    assert all(commit.email == "t@example.invalid" for commit in commits)
    assert all(len(commit.hash) == 40 for commit in commits)
    assert all(commit.abbrev == commit.hash[:9] for commit in commits)
    assert commits[-1].parents == ()
    assert commits[0].parents == (commits[1].hash,)


def test_a_commit_outside_the_inventory_is_not_listed(repo: Repo) -> None:
    """The pathspec is what makes this the *inventory's* history."""
    (repo.root / "README.md").write_text("nothing to do with the network\n", encoding="utf-8")
    repo.commit("Write a readme")

    assert [c.subject for c in Timeline.open(repo.inventory).commits()] == [
        "Bring the home lab under description"
    ]


def test_the_tree_hash_is_the_inventory_directory_not_the_commit(history: Repo) -> None:
    """Two commits that leave the inventory identical share a key."""
    timeline = Timeline.open(history.inventory)
    commits = {commit.subject: commit for commit in timeline.commits()}

    # "Break" added a file and "Unbreak" removed it again, so the tree either
    # side of the pair is the same object.
    assert commits["Unbreak the tree"].tree == commits["Add a lab switch"].tree
    assert commits["Break the tree on purpose"].tree != commits["Add a lab switch"].tree
    assert timeline.tree_of("HEAD") == commits["Unbreak the tree"].tree


def test_a_range_is_exclusive_at_its_older_end(history: Repo) -> None:
    """``a..b``, as git means it: ``a`` is the state ``b`` is drawn against."""
    timeline = Timeline.open(history.inventory)
    commits = timeline.commits()
    oldest, newest = commits[-1], commits[1]

    within = timeline.commits(since=oldest.hash, until=newest.hash)

    assert [commit.subject for commit in within] == [
        "Break the tree on purpose",
        "Add a lab switch",
        "Swap the edge router",
    ]


def test_a_limit_takes_the_newest(history: Repo) -> None:
    assert [c.subject for c in Timeline.open(history.inventory).commits(limit=2)] == [
        "Unbreak the tree",
        "Break the tree on purpose",
    ]


def test_a_range_wider_than_the_bound_is_refused_before_anything_is_read(
    history: Repo,
) -> None:
    """The bound is the point: a scrubber over a repository is not a feature."""
    timeline = Timeline.open(history.inventory, max_revisions=2)

    with pytest.raises(HistoryError) as caught:
        timeline.commits()

    assert "5 revisions" in str(caught.value)
    assert "bound of 2" in str(caught.value)
    assert "max-revisions" in str(caught.value)
    # A limit inside the bound is honoured; the bound is about what may be asked
    # for, not about how many come back.
    assert len(timeline.commits(limit=2)) == 2


def test_the_bound_is_at_least_one(history: Repo) -> None:
    assert Timeline.open(history.inventory, max_revisions=0).max_revisions == 1


def test_a_revision_that_does_not_load_is_shown_as_such(history: Repo) -> None:
    """Never skipped: a commit that broke the tree is the one worth stopping on."""
    timeline = Timeline.open(history.inventory)
    broken = next(c for c in timeline.commits() if c.subject == "Break the tree on purpose")

    revision = timeline.revision(broken.hash, tree=broken.tree)

    assert not revision.ok
    assert not revision.missing
    assert revision.error is not None
    assert broken.abbrev in revision.error
    assert "does not load" in revision.error

    frame = timeline.frame(broken)
    assert not frame.ok
    assert frame.plan is None
    assert frame.error == revision.error
    assert frame.summary == revision.error
    assert frame.to_dict()["error"] == revision.error


def test_the_frame_after_a_broken_revision_says_which_side_broke(history: Repo) -> None:
    timeline = Timeline.open(history.inventory)
    commits = timeline.commits()
    unbreak, broken = commits[0], commits[1]

    frame = timeline.frame(unbreak)

    assert not frame.ok
    assert frame.error is not None
    assert broken.abbrev in frame.error


def test_a_revision_with_no_inventory_folder_says_so(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "work")
    (repo.root / "README.md").write_text("empty\n", encoding="utf-8")
    repo.commit("Before there was an inventory")
    repo.seed()

    timeline = Timeline.open(repo.inventory)
    revision = timeline.revision("HEAD~1")

    assert not revision.ok
    assert revision.missing
    assert revision.error is not None
    assert "'net'" in revision.error
    assert "does not exist" in revision.error


def test_the_commit_that_adds_the_inventory_reads_as_everything_added(
    tmp_path: Path,
) -> None:
    """A side that predates the folder is an empty network, and the frame says so.

    The alternative — refusing the frame — would hide the one commit in which
    the whole network appears, which is the opposite of honest.
    """
    repo = Repo(tmp_path / "work")
    (repo.root / "README.md").write_text("empty\n", encoding="utf-8")
    repo.commit("Before there was an inventory")
    repo.seed()

    timeline = Timeline.open(repo.inventory)
    frame = timeline.frame(timeline.commits()[0])

    assert frame.ok
    assert frame.note == "the inventory did not exist before this commit"
    assert "devices added" in frame.summary
    assert "did not exist before" in frame.summary


def test_a_root_commit_is_diffed_against_nothing(repo: Repo) -> None:
    timeline = Timeline.open(repo.inventory)
    frame = timeline.frame(timeline.commits()[0])

    assert frame.ok
    assert frame.plan is not None
    assert frame.note is None
    assert "7 devices added" in frame.summary


def test_a_commit_that_only_reformats_says_nothing_changed(repo: Repo) -> None:
    """A commit can touch the YAML without changing the network it describes."""
    router = repo.inventory / "routers" / "rtr-home.yaml"
    router.write_text(router.read_text(encoding="utf-8") + "\n# a passing thought\n", "utf-8")
    repo.commit("Add a comment")

    timeline = Timeline.open(repo.inventory)
    frame = timeline.frame(timeline.commits()[0])

    assert frame.ok
    assert frame.summary == "no change to the network"


def test_a_named_revision_is_resolved_even_when_it_touched_nothing(repo: Repo) -> None:
    """``commit()`` answers about the revision asked for, not the nearest change."""
    (repo.root / "README.md").write_text("outside\n", encoding="utf-8")
    head = repo.commit("Touch nothing in the inventory")

    found = Timeline.open(repo.inventory).commit("HEAD")

    assert found.hash == head
    assert found.subject == "Touch nothing in the inventory"


def test_an_unknown_revision_is_refused(repo: Repo) -> None:
    with pytest.raises(HistoryError) as caught:
        Timeline.open(repo.inventory).commit("no-such-ref")
    assert "no-such-ref" in str(caught.value)


def test_a_tree_outside_a_repository_has_no_timeline(tmp_path: Path) -> None:
    shutil.copytree(EXAMPLES, tmp_path / "net")

    with pytest.raises(HistoryError) as caught:
        Timeline.open(tmp_path / "net")

    assert "not inside a git repository" in str(caught.value)


def test_the_states_of_neighbouring_frames_are_loaded_once(history: Repo) -> None:
    """Keyed by tree hash, so a linear walk loads one inventory per step."""
    timeline = Timeline.open(history.inventory)
    commits = timeline.commits()

    timeline.frame(commits[2])  # loads "Add a lab switch" and its parent
    before = timeline._states.hits
    timeline.frame(commits[3])  # its parent's state is the one just loaded

    assert timeline._states.hits > before


def test_the_trees_of_a_frame_are_known_without_loading_it(history: Repo) -> None:
    timeline = Timeline.open(history.inventory)
    commits = timeline.commits()

    before, after = timeline.trees(commits[2])

    assert after == commits[2].tree
    assert before == commits[3].tree
    assert timeline.trees(commits[-1]) == (None, commits[-1].tree)


def test_the_inventory_may_be_the_repository_root(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir(parents=True)
    repo = Repo(tmp_path / "work", prefix="")
    repo.seed()

    timeline = Timeline.open(repo.inventory)

    assert timeline.prefix == ""
    assert len(timeline.commits()) == 1
    assert timeline.frame(timeline.commits()[0]).ok


# --------------------------------------------------------------------------- #
# git_ref's two failures
# --------------------------------------------------------------------------- #


def test_git_ref_tells_a_missing_folder_from_a_missing_ref(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "work")
    (repo.root / "README.md").write_text("empty\n", encoding="utf-8")
    repo.commit("Before there was an inventory")
    repo.seed()

    with pytest.raises(MissingInventory) as missing, git_ref(repo.inventory, "HEAD~1"):
        pass  # pragma: no cover - the context manager raises on entry
    assert "no 'net' directory" in str(missing.value)

    with pytest.raises(PlanSourceError) as unknown, git_ref(repo.inventory, "no-such-ref"):
        pass  # pragma: no cover - the context manager raises on entry
    assert not isinstance(unknown.value, MissingInventory)


def test_a_revision_that_is_really_a_git_option_is_refused(history: Repo, tmp_path: Path) -> None:
    """``git log --output=<file>`` writes a file, and a revision is user input.

    ``netgraph web`` takes one from a query string, so a page this server did
    not write could otherwise ask it to write anywhere it can reach. The guard
    is one leading character, and it is checked at every door.
    """
    written = tmp_path / "written-by-git.tar"
    timeline = Timeline.open(history.inventory)
    hostile = f"--output={written}"

    for call in (
        lambda: timeline.commit(hostile),
        lambda: timeline.tree_of(hostile),
        lambda: timeline.commits(since=hostile),
        lambda: timeline.commits(until=hostile),
        lambda: timeline.count(until=hostile),
    ):
        with pytest.raises(HistoryError) as caught:
            call()
        assert "would be read as an option" in str(caught.value)

    with pytest.raises(PlanSourceError), git_ref(history.inventory, hostile):
        pass  # pragma: no cover - the context manager raises on entry

    assert not written.exists(), "git was handed an option dressed as a revision"


def test_the_frame_route_refuses_an_option_dressed_as_a_revision(
    history: Repo, tmp_path: Path
) -> None:
    written = tmp_path / "written-by-git.tar"
    session = EditingSession(root=history.inventory)

    with pytest.raises(SessionError) as caught:
        session.frame(f"--output={written}", ViewOptions())

    assert "would be read as an option" in str(caught.value)
    assert not written.exists()


@pytest.mark.parametrize("hostile", ["", "-x", "main\nrm -rf /", "main\x00"])
def test_check_revision_names_what_it_refuses(hostile: str) -> None:
    with pytest.raises(PlanSourceError):
        check_revision(hostile)


def test_check_revision_passes_a_real_one_through() -> None:
    assert check_revision("origin/main~2^{commit}") == "origin/main~2^{commit}"


# --------------------------------------------------------------------------- #
# Summarising a changeset
# --------------------------------------------------------------------------- #


def _plan(before: str, after: str, tmp_path: Path) -> object:
    left, right = tmp_path / "before", tmp_path / "after"
    for target, text in ((left, before), (right, after)):
        target.mkdir(parents=True, exist_ok=True)
        (target / "net.yaml").write_text(text, encoding="utf-8")
    return diff_states(load_tree(left), load_tree(right))


_ONE_SWITCH = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata: {name: sw-1}
spec:
  interfaces:
    - name: eth0
      type: ethernet
      ipv4:
        addresses: [10.0.0.1/24]
"""


def test_a_summary_counts_what_moved_by_what_it_is(tmp_path: Path) -> None:
    plan = _plan("", _ONE_SWITCH, tmp_path)
    assert summarise(plan) == "1 device added"


def test_a_summary_pluralises(tmp_path: Path) -> None:
    second = _ONE_SWITCH.replace("sw-1", "sw-2").replace("10.0.0.1", "10.0.0.2")
    after = f"{_ONE_SWITCH}---\n{second}"
    assert summarise(_plan("", after, tmp_path)) == "2 devices added"


def test_a_summary_names_an_address_rather_than_the_device_holding_it(
    tmp_path: Path,
) -> None:
    """ "2 addresses moved" is the sentence a network's history is read for."""
    moved = _ONE_SWITCH.replace("10.0.0.1/24", "10.0.0.9/24")
    assert summarise(_plan(_ONE_SWITCH, moved, tmp_path)) == "1 address moved"


def test_a_summary_says_when_a_commit_left_the_network_alone(tmp_path: Path) -> None:
    assert summarise(_plan(_ONE_SWITCH, _ONE_SWITCH, tmp_path)) == "no change to the network"


def test_a_removal_is_counted_as_one(tmp_path: Path) -> None:
    assert summarise(_plan(_ONE_SWITCH, "", tmp_path)) == "1 device removed"


def test_a_summary_orders_by_kind_and_names_every_action(tmp_path: Path) -> None:
    """Built by hand: the point is the wording, not the rename detector."""
    from netgraph.plan.model import Action, Change, Plan

    plan = Plan(
        changes=(
            Change(action=Action.DELETE, address=parse_address("cable.cbl-1"), kind="cable"),
            Change(action=Action.CREATE, address=parse_address("device.sw-1"), kind="switch"),
            Change(action=Action.RENAME, address=parse_address("device.sw-2"), kind="switch"),
        )
    )

    assert summarise(plan) == "1 device added, 1 device renamed, 1 link removed"


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #


def test_the_frame_cache_is_least_recently_used() -> None:
    cache: FrameCache[str] = FrameCache(size=2)
    cache.put("a", "one")
    cache.put("b", "two")

    assert cache.get("a") == "one"  # "a" is now the most recent
    cache.put("c", "three")

    assert cache.get("b") is None  # "b" fell out, not "a"
    assert cache.get("a") == "one"
    assert cache.get("c") == "three"
    assert len(cache) == 2


def test_the_frame_cache_counts_its_hits_and_misses() -> None:
    cache: FrameCache[int] = FrameCache()
    cache.get("nothing")
    cache.put("something", 1)
    cache.get("something")

    assert (cache.hits, cache.misses) == (1, 1)
    cache.clear()
    assert cache.get("something") is None


# --------------------------------------------------------------------------- #
# The [history] table
# --------------------------------------------------------------------------- #


def test_the_bound_is_configurable() -> None:
    assert parse_config({}).history.max_revisions == DEFAULT_MAX_REVISIONS
    assert parse_config({"history": {"max-revisions": 7}}).history.max_revisions == 7


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ({"max": 5}, "unknown key(s) in [history]: max"),
        ({"max-revisions": 0}, "must be at least 1"),
        ({"max-revisions": "many"}, "must be a whole number"),
        ({"max-revisions": True}, "must be a whole number"),
    ],
)
def test_a_bad_history_table_is_refused(table: dict[str, object], expected: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        parse_config({"history": table})
    assert expected in str(caught.value)


def test_a_history_table_that_is_not_a_table_is_refused() -> None:
    with pytest.raises(ConfigurationError) as caught:
        parse_config({"history": 7})
    assert "'history' must be a table" in str(caught.value)


# --------------------------------------------------------------------------- #
# netgraph log
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_log_lists_each_commit_and_what_it_did(runner: CliRunner, history: Repo) -> None:
    result = runner.invoke(cli, ["-i", str(history.inventory), "log"])

    assert result.exit_code == 0, result.output
    assert "Add a lab switch" in result.output
    assert "1 device added" in result.output
    assert "7 devices added" in result.output
    assert "Tester" in result.output


def test_log_shows_a_revision_that_does_not_load_rather_than_skipping_it(
    runner: CliRunner, history: Repo
) -> None:
    result = runner.invoke(cli, ["-i", str(history.inventory), "log"])

    assert result.exit_code == 0, result.output
    assert "Break the tree on purpose" in result.output
    assert "! the inventory at" in result.output
    assert "does not load" in result.output


def test_log_limits_and_ranges(runner: CliRunner, history: Repo) -> None:
    commits = Timeline.open(history.inventory).commits()

    limited = runner.invoke(cli, ["-i", str(history.inventory), "log", "-n", "2"])
    assert limited.output.count("Tester") == 2

    ranged = runner.invoke(
        cli,
        ["-i", str(history.inventory), "log", "--from", commits[-1].hash, "--to", commits[2].hash],
    )
    assert "Bring the home lab" not in ranged.output
    assert "Add a lab switch" in ranged.output


def test_log_without_summaries_reads_nothing(runner: CliRunner, history: Repo) -> None:
    result = runner.invoke(cli, ["-i", str(history.inventory), "log", "--no-summary"])

    assert result.exit_code == 0, result.output
    assert "Add a lab switch" in result.output
    assert "1 device added" not in result.output
    assert "does not load" not in result.output


def test_log_json_carries_the_commit_and_its_changeset(runner: CliRunner, history: Repo) -> None:
    result = runner.invoke(cli, ["-i", str(history.inventory), "log", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["maxRevisions"] == DEFAULT_MAX_REVISIONS
    assert payload["range"] == {"from": None, "to": "HEAD"}
    added = next(c for c in payload["commits"] if c["subject"] == "Add a lab switch")
    assert added["summary"] == "1 device added"
    assert added["changes"]["create"] == 1
    assert added["error"] is None
    assert len(added["hash"]) == 40
    broken = next(c for c in payload["commits"] if c["subject"] == "Break the tree on purpose")
    assert "does not load" in broken["error"]


def test_log_json_without_summaries_lists_the_commits_alone(
    runner: CliRunner, history: Repo
) -> None:
    result = runner.invoke(cli, ["-i", str(history.inventory), "log", "--json", "--no-summary"])

    payload = json.loads(result.stdout)
    assert "summary" not in payload["commits"][0]
    assert payload["commits"][0]["subject"] == "Unbreak the tree"


def test_log_refuses_a_range_wider_than_the_bound(runner: CliRunner, history: Repo) -> None:
    result = runner.invoke(
        cli, ["-i", str(history.inventory), "log", "--max-revisions", "2", "-n", "50"]
    )

    assert result.exit_code == 1
    assert "more than the bound of 2" in result.output


def test_the_bound_may_come_from_netgraph_toml(runner: CliRunner, history: Repo) -> None:
    (history.inventory / "netgraph.toml").write_text(
        "[history]\nmax-revisions = 2\n", encoding="utf-8"
    )

    result = runner.invoke(cli, ["-i", str(history.inventory), "log", "-n", "50"])

    assert result.exit_code == 1
    assert "bound of 2" in result.output


def test_log_outside_a_repository_says_so(runner: CliRunner, tmp_path: Path) -> None:
    shutil.copytree(EXAMPLES, tmp_path / "net")

    result = runner.invoke(cli, ["-i", str(tmp_path / "net"), "log"])

    assert result.exit_code == 1
    assert "not inside a git repository" in result.output


def test_log_says_when_nothing_has_touched_the_inventory(runner: CliRunner, tmp_path: Path) -> None:
    repo = Repo(tmp_path / "work")
    (repo.root / "README.md").write_text("empty\n", encoding="utf-8")
    repo.commit("Nothing to do with a network")
    repo.inventory.mkdir(parents=True, exist_ok=True)
    shutil.copytree(EXAMPLES, repo.inventory, dirs_exist_ok=True)

    result = runner.invoke(cli, ["-i", str(repo.inventory), "log"])

    assert result.exit_code == 0, result.output
    assert "no commit has touched this inventory" in result.output


# --------------------------------------------------------------------------- #
# netgraph diff, over two revisions
# --------------------------------------------------------------------------- #


def test_diff_draws_one_revision_against_another(runner: CliRunner, history: Repo) -> None:
    commits = Timeline.open(history.inventory).commits()

    result = runner.invoke(
        cli,
        [
            "-i",
            str(history.inventory),
            "diff",
            "--from",
            commits[3].hash,
            "--to",
            commits[2].hash,
            "-f",
            "json",
            "-o",
            str(history.root / "out.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "1 added" in result.output
    payload = json.loads((history.root / "out.json").read_text(encoding="utf-8"))
    assert payload["diff"]["counts"]["added"] == 1


def test_diff_from_a_revision_with_no_inventory_says_so(runner: CliRunner, tmp_path: Path) -> None:
    repo = Repo(tmp_path / "work")
    (repo.root / "README.md").write_text("empty\n", encoding="utf-8")
    repo.commit("Before there was an inventory")
    repo.seed()

    result = runner.invoke(
        cli, ["-i", str(repo.inventory), "diff", "--from", "HEAD~1", "-f", "json", "-o", "-"]
    )

    assert result.exit_code == 1
    assert "no 'net' directory" in result.output


# --------------------------------------------------------------------------- #
# The editor's routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def session(history: Repo) -> Iterator[EditingSession]:
    yield EditingSession(root=history.inventory)


def test_the_session_lists_the_history(session: EditingSession) -> None:
    payload = session.history()

    assert payload["bound"] == DEFAULT_MAX_REVISIONS
    assert payload["root"] == "net"
    assert [c["subject"] for c in payload["commits"]][:2] == [
        "Unbreak the tree",
        "Break the tree on purpose",
    ]
    assert all("tree" in commit for commit in payload["commits"])


def test_a_session_outside_a_repository_has_no_history(tmp_path: Path) -> None:
    shutil.copytree(EXAMPLES, tmp_path / "net")

    with pytest.raises(SessionError) as caught:
        EditingSession(root=tmp_path / "net").history()

    assert "not inside a git repository" in str(caught.value)


def test_the_session_draws_a_frame_as_the_diff_against_its_parent(
    session: EditingSession,
) -> None:
    added = next(c for c in session.history()["commits"] if c["subject"] == "Add a lab switch")

    frame = session.frame(added["hash"], ViewOptions())

    assert frame["status"] == "ok"
    assert frame["hash"] == added["hash"]
    assert frame["subject"] == "Add a lab switch"
    assert frame["summary"] == "1 device added"
    assert frame["changes"]["create"] == 1
    assert frame["diff"]["counts"]["added"] == 1
    assert "<svg" in frame["svg"]
    assert "sw-lab" in frame["svg"]


def test_a_frame_of_a_broken_revision_is_a_failure_with_a_reason(
    session: EditingSession,
) -> None:
    """Shown, not skipped, and not an exception either."""
    broken = next(
        c for c in session.history()["commits"] if c["subject"] == "Break the tree on purpose"
    )

    frame = session.frame(broken["hash"], ViewOptions())

    assert frame["status"] == "failed"
    assert frame["svg"] is None
    assert "does not load" in frame["message"]
    assert frame["error"] == frame["message"]


def test_a_frame_of_an_unknown_revision_is_refused(session: EditingSession) -> None:
    with pytest.raises(SessionError) as caught:
        session.frame("no-such-thing", ViewOptions())
    assert "no-such-thing" in str(caught.value)


def test_a_frame_already_drawn_is_not_drawn_again(session: EditingSession) -> None:
    """The cache is keyed by tree hash, which is what makes scrubbing back free."""
    added = next(c for c in session.history()["commits"] if c["subject"] == "Add a lab switch")

    first = session.frame(added["hash"], ViewOptions())
    misses = session._frames.misses
    second = session.frame(added["hash"], ViewOptions())

    assert session._frames.misses == misses  # nothing was recomputed
    assert session._frames.hits >= 1
    assert second["svg"] == first["svg"]
    assert second["summary"] == first["summary"]


def test_two_commits_with_the_same_pair_of_trees_share_a_frame(
    session: EditingSession,
) -> None:
    """A revert draws the same picture as the change it reverted, backwards.

    Keying by tree hash rather than by commit is what makes that a hit; the
    commit's own facts are still the ones the answer carries.
    """
    commits = session.history()["commits"]
    unbreak, broken = commits[0], commits[1]
    session.frame(broken["hash"], ViewOptions())

    frame = session.frame(unbreak["hash"], ViewOptions())

    assert frame["hash"] == unbreak["hash"]
    assert frame["subject"] == "Unbreak the tree"


def test_a_frame_may_be_answered_with_a_fingerprint_the_caller_holds(
    session: EditingSession,
) -> None:
    added = next(c for c in session.history()["commits"] if c["subject"] == "Add a lab switch")
    first = session.frame(added["hash"], ViewOptions())

    again = session.frame(added["hash"], ViewOptions(), known=first["graphHash"])

    assert again["unchanged"] is True
    assert again["svg"] is None
    assert again["summary"] == first["summary"]


def test_a_frame_is_drawn_at_the_layer_it_is_asked_for(session: EditingSession) -> None:
    from netgraph.render import Layer

    added = next(c for c in session.history()["commits"] if c["subject"] == "Add a lab switch")

    l1 = session.frame(added["hash"], ViewOptions(layer=Layer.L1))
    l3 = session.frame(added["hash"], ViewOptions(layer=Layer.L3))

    assert l1["svg"] != l3["svg"]
    assert l1["graphHash"] != l3["graphHash"]


def test_positions_come_from_the_layout_at_that_revision(history: Repo) -> None:
    """A diagram that was arranged stays arranged as you scrub back to it."""
    history.write(
        "layout.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: layout\n"
        "metadata: {name: layout}\n"
        "spec:\n"
        "  views:\n"
        "    l1:\n"
        "      nodes:\n"
        "        routers/rtr-home:\n"
        "          position: {x: 1234, y: 4321}\n",
    )
    history.commit("Arrange the diagram")
    session = EditingSession(root=history.inventory)
    commits = session.history()["commits"]

    arranged = session.frame(commits[0]["hash"], ViewOptions())
    before = session.frame(commits[1]["hash"], ViewOptions())

    assert arranged["geometry"] is not None
    assert arranged["geometry"]["mode"] == "partial"
    # The revision before the layout document was committed has none, which is
    # the point: each frame is arranged the way *that* revision arranged it.
    assert before["geometry"] is None


def test_the_editor_truncates_a_long_history_rather_than_refusing_it(history: Repo) -> None:
    """A scrubber that shows nothing because there is too much is no answer.

    The bound still applies — it decides how many frames may be reached — but
    the count comes back with the list, so the page can say it is showing the
    newest two of five.
    """
    (history.inventory / "netgraph.toml").write_text(
        "[history]\nmax-revisions = 2\n", encoding="utf-8"
    )
    session = EditingSession(root=history.inventory)

    assert session.timeline().max_revisions == 2
    payload = session.history()

    assert len(payload["commits"]) == 2
    assert payload["total"] == 5
    assert payload["truncated"] is True
    assert payload["bound"] == 2


def test_the_session_reuses_one_timeline(session: EditingSession) -> None:
    assert session.timeline() is session.timeline()


def test_a_view_option_is_part_of_the_frame_key(session: EditingSession) -> None:
    from netgraph.render import Layer

    added = next(c for c in session.history()["commits"] if c["subject"] == "Add a lab switch")
    session.frame(added["hash"], ViewOptions())
    misses = session._frames.misses

    session.frame(added["hash"], replace(ViewOptions(), layer=Layer.L2))

    assert session._frames.misses == misses + 1


# --------------------------------------------------------------------------- #
# The HTTP face
# --------------------------------------------------------------------------- #


def test_the_routes_answer_the_same_thing_the_session_does(history: Repo) -> None:
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from netgraph.web import WebServer

    session = EditingSession(root=history.inventory)
    with WebServer.create(source="", session=session, host="127.0.0.1", port=0) as server:
        with urlopen(f"{server.url}/api/history") as answer:
            listing = json.load(answer)
        assert listing["commits"][0]["subject"] == "Unbreak the tree"

        added = next(c for c in listing["commits"] if c["subject"] == "Add a lab switch")
        with urlopen(f"{server.url}/api/frame?rev={added['hash']}&view=l1") as answer:
            frame = json.load(answer)
        assert frame["summary"] == "1 device added"
        assert "<svg" in frame["svg"]

        with pytest.raises(HTTPError) as caught:
            urlopen(f"{server.url}/api/frame")
        assert caught.value.code == 400


def test_a_commit_record_round_trips_to_json() -> None:
    from datetime import datetime, timezone

    commit = Commit(
        hash="a" * 40,
        parents=("b" * 40,),
        author="Ada",
        email="ada@example.invalid",
        when=datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc),
        subject="Describe the network",
        tree="c" * 40,
    )

    payload = commit.to_dict()

    assert payload["abbrev"] == "a" * 9
    assert payload["date"] == "2026-08-14T09:30:00+00:00"
    assert commit.date == "2026-08-14"
    assert commit.parent == "b" * 40
    assert (
        Commit(hash="a" * 40, parents=(), author="", email="", when=commit.when, subject="").parent
        is None
    )
