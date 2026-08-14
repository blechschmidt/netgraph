"""``netgraph web DIR``: the editing session, its API, and its two preconditions.

The properties asserted here are the ones that make the difference between a
scratchpad and an editor, and the ones a user would otherwise discover by losing
work:

* **The tree is addressable.** Every file, every document in it and the element
  each one declares — with the line it starts on — so a node in the diagram can
  be traced to the text that declares it and back.
* **Nothing is written unless the session was opened writable.** Every mutating
  route answers 403 otherwise, and the command line refuses ``--write`` over a
  stream or on a bind that is not loopback.
* **No request becomes a path.** ``..``, an absolute path, a hidden directory
  and a non-YAML suffix are all refused by name.
* **A write states what it replaces.** A whole-file write carries the content
  hash it was read at and a batch of operations carries the tree revision;
  either being stale is a 409 rather than an overwrite.
* **Undo is exact and lives on the server.** Applying, undoing and redoing
  returns the tree byte for byte, and the stack survives the page.
* **Every write goes through the mutation layer.** Comments and formatting
  survive, and an edit that would introduce a new error is refused unless
  forced.
* **A change made outside the session is noticed**, and reaches the page as a
  moved revision.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.edit import EditError, ValidationRefused
from netgraph.web.preview import ViewOptions
from netgraph.web.server import WebServer
from netgraph.web.session import (
    MAX_FILE_BYTES,
    Conflict,
    EditingSession,
    ReadOnly,
    SessionError,
    TreeWatcher,
    relative_path,
)

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_LAB = REPO_ROOT / "examples" / "home-lab"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A private copy of the example inventory, safe to write to."""
    root = tmp_path / "inventory"
    shutil.copytree(HOME_LAB, root)
    return root


@pytest.fixture
def session(tree: Path) -> EditingSession:
    return EditingSession(root=tree, writable=True)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "given",
    [
        "../secrets.yaml",
        "/etc/passwd",
        "switches/../../escape.yaml",
        ".git/config",
        "_drafts/thing.yaml",
        "notes.txt",
        "windows\\path.yaml",
        "",
    ],
)
def test_a_path_that_is_not_a_document_in_the_tree_is_refused(given: str) -> None:
    with pytest.raises(SessionError):
        relative_path(given)


@pytest.mark.parametrize(
    ("given", "expected"),
    [("switches/sw.yaml", "switches/sw.yaml"), ("a/b/c.YML", "a/b/c.YML"), ("x.yaml", "x.yaml")],
)
def test_a_document_path_survives_unchanged(given: str, expected: str) -> None:
    assert relative_path(given) == expected


# --------------------------------------------------------------------------- #
# The tree
# --------------------------------------------------------------------------- #


def test_the_tree_maps_every_element_to_its_file_and_line(session: EditingSession) -> None:
    payload = session.tree()
    paths = [entry["path"] for entry in payload["files"]]
    assert "switches/sw-home.yaml" in paths
    assert paths == sorted(paths), "the list is in load order, which is path order"

    declared = {
        document["address"]: (entry["path"], document["line"])
        for entry in payload["files"]
        for document in entry["documents"]
    }
    assert declared["switches/sw-home"] == ("switches/sw-home.yaml", 1)
    # One file, four cables, four lines: the mapping is per document, not per file.
    cables = [address for address in declared if address.startswith("cables/")]
    assert len(cables) > 1
    assert len({declared[address][1] for address in cables}) == len(cables)


def test_every_file_carries_the_hash_a_write_of_it_must_quote(session: EditingSession) -> None:
    entry = next(f for f in session.tree()["files"] if f["path"] == "switches/sw-home.yaml")
    assert entry["hash"] == session.read_file("switches/sw-home.yaml")["hash"]
    assert entry["size"] == (session.root / "switches" / "sw-home.yaml").stat().st_size


def test_a_file_the_loader_would_skip_is_not_listed(session: EditingSession) -> None:
    (session.root / "README.txt").write_text("not a document\n", encoding="utf-8")
    (session.root / "_drafts").mkdir()
    (session.root / "_drafts" / "wip.yaml").write_text("kind: device\n", encoding="utf-8")
    session.invalidate()
    paths = [entry["path"] for entry in session.tree()["files"]]
    assert "README.txt" not in paths
    assert "_drafts/wip.yaml" not in paths


def test_a_broken_document_is_still_listed_and_reported(session: EditingSession) -> None:
    (session.root / "switches" / "sw-home.yaml").write_text("kind: [\n", encoding="utf-8")
    session.invalidate()
    payload = session.tree()
    assert "switches/sw-home.yaml" in [entry["path"] for entry in payload["files"]]
    assert any("switches/sw-home.yaml" in item["location"] for item in payload["diagnostics"])


@requires_dot
def test_the_graph_is_the_tree_and_says_which_revision_it_drew(session: EditingSession) -> None:
    preview, revision = session.graph(ViewOptions())
    assert preview.status.is_ok
    assert preview.nodes and preview.svg
    assert revision == session.revision


# --------------------------------------------------------------------------- #
# Reading a file
# --------------------------------------------------------------------------- #


def test_reading_a_file_answers_its_text_and_hash(session: EditingSession) -> None:
    body = session.read_file("switches/sw-home.yaml")
    assert body["text"] == (session.root / "switches" / "sw-home.yaml").read_text(encoding="utf-8")
    assert len(body["hash"]) == 64


def test_reading_a_file_that_is_not_there_says_so(session: EditingSession) -> None:
    with pytest.raises(SessionError, match="does not exist"):
        session.read_file("switches/nothing.yaml")


def test_a_file_too_big_for_a_browser_is_refused(session: EditingSession) -> None:
    target = session.root / "huge.yaml"
    target.write_bytes(b"# " + b"x" * MAX_FILE_BYTES)
    with pytest.raises(SessionError, match="opens up to"):
        session.read_file("huge.yaml")


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def test_a_read_only_session_refuses_every_write(tree: Path) -> None:
    session = EditingSession(root=tree, writable=False)
    body = session.read_file("switches/sw-home.yaml")
    with pytest.raises(ReadOnly):
        session.write_file("switches/sw-home.yaml", body["text"], base_hash=body["hash"])
    with pytest.raises(ReadOnly):
        session.apply(
            [{"op": "set", "address": "switches/sw-home", "path": "spec.model", "value": "X"}]
        )
    with pytest.raises(ReadOnly):
        session.undo()
    assert session.read_file("switches/sw-home.yaml")["hash"] == body["hash"]


def test_a_whole_file_write_lands_and_moves_the_revision(session: EditingSession) -> None:
    before = session.read_file("switches/sw-home.yaml")
    text = "# rewritten in the browser\n" + before["text"]
    change = session.write_file("switches/sw-home.yaml", text, base_hash=before["hash"])
    assert change.revision > 1
    assert set(change.files) == {"switches/sw-home.yaml"}
    assert (session.root / "switches" / "sw-home.yaml").read_text(encoding="utf-8") == text


def test_a_write_that_changes_nothing_changes_nothing(session: EditingSession) -> None:
    """A save of untouched text must not bump the revision or grow the history.

    Otherwise every other tab refetches the tree, and the undo stack fills with
    steps that undo nothing.
    """
    before = session.read_file("switches/sw-home.yaml")
    change = session.write_file("switches/sw-home.yaml", before["text"], base_hash=before["hash"])
    assert change.files == {}
    assert change.revision == 1
    assert session.state()["undo"] == 0


def test_a_stale_hash_is_a_conflict_and_not_an_overwrite(session: EditingSession) -> None:
    body = session.read_file("switches/sw-home.yaml")
    target = session.root / "switches" / "sw-home.yaml"
    target.write_text(body["text"] + "\n# somebody else was here\n", encoding="utf-8")
    session.invalidate()

    with pytest.raises(Conflict) as refused:
        session.write_file("switches/sw-home.yaml", "kind: device\n", base_hash=body["hash"])
    assert refused.value.path == "switches/sw-home.yaml"
    assert refused.value.hash != body["hash"]
    assert "somebody else was here" in target.read_text(encoding="utf-8")


def test_creating_a_file_that_is_already_there_is_a_conflict(session: EditingSession) -> None:
    with pytest.raises(Conflict, match="already exists"):
        session.write_file("switches/sw-home.yaml", "kind: device\n", base_hash=None)


def test_a_write_that_would_break_the_tree_is_refused_unless_forced(
    session: EditingSession,
) -> None:
    body = session.read_file("switches/sw-home.yaml")
    with pytest.raises(ValidationRefused) as refused:
        session.write_file("switches/sw-home.yaml", "kind: nonsense\n", base_hash=body["hash"])
    assert refused.value.problems
    assert (session.root / "switches" / "sw-home.yaml").read_text(encoding="utf-8") == body["text"]

    session.write_file(
        "switches/sw-home.yaml", "kind: nonsense\n", base_hash=body["hash"], force=True
    )
    assert (session.root / "switches" / "sw-home.yaml").read_text(
        encoding="utf-8"
    ) == "kind: nonsense\n"


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


def test_an_operation_goes_through_the_mutation_layer(session: EditingSession) -> None:
    """Which is to say: the untouched lines are not rewritten."""
    before = (session.root / "switches" / "sw-home.yaml").read_text(encoding="utf-8")
    change = session.apply(
        [{"op": "set", "address": "switches/sw-home", "path": "spec.model", "value": "C9300"}],
        revision=session.revision,
    )
    after = (session.root / "switches" / "sw-home.yaml").read_text(encoding="utf-8")
    assert "model: C9300" in after
    changed = [
        (old, new)
        for old, new in zip(before.splitlines(), after.splitlines(), strict=True)
        if old != new
    ]
    assert changed == [("  model: TL-SG108E", "  model: C9300")]
    assert change.applied[0]["operation"]["op"] == "set"
    assert change.inverse, "an applied operation always says how to undo it"


def test_a_batch_is_applied_or_not_at_all(session: EditingSession) -> None:
    before = (session.root / "switches" / "sw-home.yaml").read_text(encoding="utf-8")
    with pytest.raises(EditError):
        session.apply(
            [
                {
                    "op": "set",
                    "address": "switches/sw-home",
                    "path": "spec.model",
                    "value": "C9300",
                },
                {"op": "set", "address": "switches/nope", "path": "spec.model", "value": "X"},
            ]
        )
    assert (session.root / "switches" / "sw-home.yaml").read_text(encoding="utf-8") == before


def test_a_batch_decided_against_an_older_tree_is_refused(session: EditingSession) -> None:
    stale = session.revision
    session.invalidate()
    with pytest.raises(Conflict, match="reload before editing"):
        session.apply(
            [{"op": "set", "address": "switches/sw-home", "path": "spec.model", "value": "X"}],
            revision=stale,
        )


def test_an_unknown_operation_is_a_request_error(session: EditingSession) -> None:
    with pytest.raises(SessionError):
        session.apply([{"op": "detonate", "address": "switches/sw-home"}])
    with pytest.raises(SessionError, match="no operations"):
        session.apply([])


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def test_undo_and_redo_return_the_tree_byte_for_byte(session: EditingSession) -> None:
    path = session.root / "switches" / "sw-home.yaml"
    original = path.read_text(encoding="utf-8")

    session.apply(
        [{"op": "rename", "address": "switches/sw-home", "new_name": "sw-core"}],
        revision=session.revision,
    )
    renamed = path.read_text(encoding="utf-8")
    assert "sw-core" in renamed

    session.undo()
    assert path.read_text(encoding="utf-8") == original
    assert session.state() | {"undo": 0, "redo": 1} == session.state()

    session.redo()
    assert path.read_text(encoding="utf-8") == renamed
    assert session.state()["undo"] == 1
    assert session.state()["redo"] == 0


def test_a_rename_takes_every_reference_with_it_and_undo_puts_them_back(
    session: EditingSession,
) -> None:
    cables = session.root / "cables" / "links.yaml"
    before = cables.read_text(encoding="utf-8")
    session.apply(
        [{"op": "rename", "address": "switches/sw-home", "new_name": "sw-core"}],
        revision=session.revision,
    )
    assert "sw-core:" in cables.read_text(encoding="utf-8")
    session.undo()
    assert cables.read_text(encoding="utf-8") == before


def test_a_new_edit_forgets_the_redo_branch(session: EditingSession) -> None:
    session.apply(
        [{"op": "set", "address": "switches/sw-home", "path": "spec.model", "value": "A"}],
        revision=session.revision,
    )
    session.undo()
    assert session.state()["redo"] == 1
    session.apply(
        [{"op": "set", "address": "switches/sw-home", "path": "spec.model", "value": "B"}],
        revision=session.revision,
    )
    assert session.state()["redo"] == 0


def test_undoing_nothing_says_so(session: EditingSession) -> None:
    with pytest.raises(SessionError, match="nothing to undo"):
        session.undo()
    with pytest.raises(SessionError, match="nothing to redo"):
        session.redo()


def test_the_history_labels_what_it_would_put_back(session: EditingSession) -> None:
    session.apply(
        [{"op": "set", "address": "switches/sw-home", "path": "spec.model", "value": "A"}],
        revision=session.revision,
    )
    assert "set" in (session.state()["undoLabel"] or "")


# --------------------------------------------------------------------------- #
# Reconciliation with the disk
# --------------------------------------------------------------------------- #


def test_a_change_made_outside_the_session_moves_the_revision(session: EditingSession) -> None:
    before = session.revision
    assert session.inventory().elements["switches/sw-home"].metadata.description != "elsewhere"
    (session.root / "switches" / "sw-home.yaml").write_text(
        re.sub(
            r"^  description: .*$",
            "  description: elsewhere",
            session.read_file("switches/sw-home.yaml")["text"],
            count=1,
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
    session.invalidate(["switches/sw-home.yaml"])
    assert session.revision > before
    assert session.inventory().elements["switches/sw-home"].metadata.description == "elsewhere"


def test_the_watcher_reports_a_change_and_stops_cleanly(session: EditingSession) -> None:
    """The watch is real ``watchfiles`` over the real folder; the test only
    asserts that it starts, notices one write and stops."""
    seen: list[tuple[str, ...]] = []
    watcher = TreeWatcher(
        session, debounce_ms=50, on_change=lambda batch: seen.append(tuple(batch))
    )
    with watcher:
        deadline = 60
        before = session.revision
        while deadline and session.revision == before:
            (session.root / "switches" / "sw-home.yaml").write_text(
                f"# touched {deadline}\n"
                + session.read_file("switches/sw-home.yaml")["text"].lstrip("#"),
                encoding="utf-8",
            )
            _sleep(0.1)
            deadline -= 1
    assert session.revision > before
    assert seen
    assert watcher.error is None


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture
def served(session: EditingSession) -> Iterator[str]:
    with WebServer.create(session=session, host="127.0.0.1", port=0) as server:
        yield server.url.rstrip("/")


def call(base: str, path: str, method: str = "GET", body: Any = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_api_answers_the_tree_and_one_file(served: str) -> None:
    status, state = call(served, "/api/state")
    assert status == 200
    assert state["mode"] == "session" and state["writable"]

    status, tree = call(served, "/api/tree")
    assert status == 200
    assert "switches/sw-home.yaml" in [entry["path"] for entry in tree["files"]]

    status, body = call(served, "/api/file/switches/sw-home.yaml")
    assert status == 200
    assert body["path"] == "switches/sw-home.yaml"
    assert len(body["hash"]) == 64


@requires_dot
def test_the_api_draws_the_view_it_is_asked_for(served: str) -> None:
    status, body = call(served, "/api/graph?view=l2&show_ips=0&vlans=10")
    assert status == 200
    assert body["status"] == "ok"
    assert body["svg"].startswith("<svg")
    assert body["revision"] >= 1


def test_a_request_path_never_becomes_a_file_name(served: str) -> None:
    for path in ("/api/file/../../etc/passwd", "/api/file/.git/config", "/api/file/x.txt"):
        status, body = call(served, path)
        assert status == 400, path
        assert body["status"] == "failed"


def test_the_api_applies_undoes_and_redoes(served: str) -> None:
    _, tree = call(served, "/api/tree")
    status, change = call(
        served,
        "/api/ops",
        "POST",
        {
            "revision": tree["revision"],
            "ops": [
                {"op": "set", "address": "switches/sw-home", "path": "spec.model", "value": "C9300"}
            ],
        },
    )
    assert status == 200
    assert change["undo"] == 1 and change["redo"] == 0
    assert change["files"]["switches/sw-home.yaml"]["state"] == "written"
    assert change["inverse"]

    status, undone = call(served, "/api/undo", "POST")
    assert status == 200 and undone["undo"] == 0 and undone["redo"] == 1

    status, redone = call(served, "/api/redo", "POST")
    assert status == 200 and redone["undo"] == 1 and redone["redo"] == 0


def test_a_stale_write_is_a_409_carrying_what_is_really_there(served: str) -> None:
    status, body = call(
        served,
        "/api/file/switches/sw-home.yaml",
        "PUT",
        {"text": "kind: device\n", "hash": "0" * 64},
    )
    assert status == 409
    assert body["conflict"]["path"] == "switches/sw-home.yaml"
    assert body["conflict"]["hash"] != "0" * 64


def test_a_write_that_would_break_the_tree_is_a_422_listing_the_problems(served: str) -> None:
    _, body = call(served, "/api/file/switches/sw-home.yaml")
    status, refusal = call(
        served,
        "/api/file/switches/sw-home.yaml",
        "PUT",
        {"text": "kind: nonsense\n", "hash": body["hash"]},
    )
    assert status == 422
    assert refusal["problems"]


def test_a_read_only_session_serves_but_does_not_write(tree: Path) -> None:
    session = EditingSession(root=tree, writable=False)
    with WebServer.create(session=session, host="127.0.0.1", port=0) as server:
        base = server.url.rstrip("/")
        assert call(base, "/api/tree")[0] == 200
        assert call(base, "/api/state")[1]["writable"] is False
        status, body = call(
            base, "/api/file/switches/sw-home.yaml", "PUT", {"text": "kind: device\n"}
        )
        assert status == 403
        assert "--write" in body["message"]
        assert call(base, "/api/undo", "POST")[0] == 403


def test_the_scratchpad_has_no_write_routes_at_all() -> None:
    with WebServer.create(source="", host="127.0.0.1", port=0) as server:
        base = server.url.rstrip("/")
        assert call(base, "/api/state")[1]["mode"] == "stream"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "/api/tree", timeout=10)
        assert missing.value.code == 404
        request = urllib.request.Request(
            base + "/api/file/x.yaml",
            method="PUT",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=10)
        assert refused.value.code == 405


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def run(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args))


def test_write_over_a_stream_is_refused_with_the_reason() -> None:
    result = run("web", "--write", "--no-open")
    assert result.exit_code == 2
    assert "a document stream has none" in result.output


def test_write_on_a_published_bind_is_refused(tree: Path) -> None:
    result = run("web", str(tree), "--write", "--host", "0.0.0.0", "--no-open")
    assert result.exit_code == 2
    assert "would publish an endpoint that changes this inventory" in result.output


def test_the_command_documents_both_faces() -> None:
    result = run("web", "--help")
    assert result.exit_code == 0
    assert "--write" in result.output
    assert "--read-only" in result.output
