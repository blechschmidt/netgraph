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
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

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
from netgraph.web.tour import MAX_SCRATCHES, TooLarge, Tours, copy_inventory

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_LAB = REPO_ROOT / "examples" / "home-lab"
#: A tree whose only problems are ones a diagnostic's Fix button can repair.
FIXABLE = REPO_ROOT / "tests" / "fixtures" / "fixable"


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
# Annotations (§21)
# --------------------------------------------------------------------------- #

#: What the canvas writes when somebody drops a note on it: a callout at a point,
#: with the placeholder text the inline editor opens on.
A_NOTE: Final[dict[str, Any]] = {
    "op": "create-annotation",
    "kind": "note",
    "name": "note-1",
    "namespace": "",
    "spec": {"text": "New note", "geometry": {"x": 40.0, "y": 90.0}},
}


def annotations_file(session: EditingSession) -> Path:
    """Where placement puts the three annotation kinds; see edit/placement.py."""
    return session.root / "annotations.yaml"


def test_a_note_dropped_on_the_canvas_becomes_a_document(session: EditingSession) -> None:
    change = session.apply([A_NOTE], revision=session.revision)
    text = annotations_file(session).read_text(encoding="utf-8")
    assert "kind: note" in text
    assert "name: note-1" in text
    assert "New note" in text
    assert change.applied[0]["operation"]["op"] == "create-annotation"
    session.undo()
    assert not annotations_file(session).exists(), "the file went with the last document in it"


def test_dragging_an_unplaced_note_writes_the_whole_geometry_block(
    session: EditingSession,
) -> None:
    """One write, not two, and the reason is NG-G005.

    A note anchored to a switch pins no point. Writing ``spec.geometry.x`` onto
    it would leave a position with no ``y`` -- which §21 refuses -- so the first
    drag has to send the block whole. See ``SetAnnotation``'s docstring and
    ``netgraph/drawio/reconcile.py``, which writes the same gesture the same way.
    """
    session.apply(
        [
            {
                "op": "create-annotation",
                "kind": "note",
                "name": "about-the-switch",
                "spec": {"text": "the noisy one", "anchor": {"element": "switches/sw-home"}},
            }
        ],
        revision=session.revision,
    )
    session.apply(
        [
            {
                "op": "set-annotation",
                "kind": "note",
                "name": "about-the-switch",
                "path": "spec.geometry",
                "value": {"x": 120.0, "y": -40.0},
            }
        ],
        revision=session.revision,
    )
    text = annotations_file(session).read_text(encoding="utf-8")
    assert "x: 120.0" in text and "y: -40.0" in text
    assert "element: switches/sw-home" in text, "the anchor survives being dragged"


def test_a_leaf_at_a_time_drag_onto_a_missing_block_is_refused(
    session: EditingSession,
) -> None:
    """The other half of the rule above, asserted rather than assumed.

    If this ever stopped being true the canvas could go back to writing a
    coordinate at a time; while it is true, the whole-block write is not an
    optimisation but the only spelling that lands.
    """
    session.apply(
        [
            {
                "op": "create-annotation",
                "kind": "note",
                "name": "about-the-switch",
                "spec": {"text": "the noisy one", "anchor": {"element": "switches/sw-home"}},
            }
        ],
        revision=session.revision,
    )
    with pytest.raises((EditError, ValidationRefused)):
        session.apply(
            [
                {
                    "op": "set-annotation",
                    "kind": "note",
                    "name": "about-the-switch",
                    "path": "spec.geometry.x",
                    "value": 120.0,
                }
            ],
            revision=session.revision,
        )


def test_a_placed_note_is_dragged_a_field_at_a_time_and_undone_as_one(
    session: EditingSession,
) -> None:
    """One gesture, one batch, one Ctrl-Z -- which is why the canvas posts both
    coordinates together rather than a request per axis."""
    session.apply([A_NOTE], revision=session.revision)
    before = annotations_file(session).read_text(encoding="utf-8")
    session.apply(
        [
            {
                "op": "set-annotation",
                "kind": "note",
                "name": "note-1",
                "path": "spec.geometry.x",
                "value": 300.0,
            },
            {
                "op": "set-annotation",
                "kind": "note",
                "name": "note-1",
                "path": "spec.geometry.y",
                "value": 220.0,
            },
        ],
        revision=session.revision,
    )
    moved = annotations_file(session).read_text(encoding="utf-8")
    assert "x: 300.0" in moved and "y: 220.0" in moved
    session.undo()
    assert annotations_file(session).read_text(encoding="utf-8") == before


def test_retyping_a_note_rewrites_only_its_text(session: EditingSession) -> None:
    session.apply([A_NOTE], revision=session.revision)
    session.apply(
        [
            {
                "op": "set-annotation",
                "kind": "note",
                "name": "note-1",
                "path": "spec.text",
                "value": "Two lines\nof it",
            }
        ],
        revision=session.revision,
    )
    text = annotations_file(session).read_text(encoding="utf-8")
    assert "Two lines" in text and "of it" in text
    assert "New note" not in text
    assert "x: 40.0" in text, "the position is untouched by a retype"


def test_deleting_an_annotation_takes_nothing_with_it(session: EditingSession) -> None:
    """No cascade, and there never will be one: nothing refers to an annotation."""
    session.apply(
        [
            A_NOTE,
            {
                "op": "create-annotation",
                "kind": "area",
                "name": "the-rack",
                "spec": {"label": "The rack", "members": ["switches/sw-home"]},
            },
        ],
        revision=session.revision,
    )
    session.apply(
        [{"op": "delete-annotation", "kind": "note", "name": "note-1"}],
        revision=session.revision,
    )
    text = annotations_file(session).read_text(encoding="utf-8")
    assert "name: note-1" not in text
    assert "name: the-rack" in text, "its neighbour in the file is untouched"
    assert (session.root / "switches" / "sw-home.yaml").exists()


def test_an_area_dragged_off_its_members_carries_its_extent(session: EditingSession) -> None:
    """A zone with a position and no size is drawn round its members again, so a
    drag that did not send the extent would silently spring back."""
    session.apply(
        [
            {
                "op": "create-annotation",
                "kind": "area",
                "name": "the-rack",
                "spec": {"label": "The rack", "members": ["switches/sw-home"]},
            }
        ],
        revision=session.revision,
    )
    session.apply(
        [
            {
                "op": "set-annotation",
                "kind": "area",
                "name": "the-rack",
                "path": "spec.geometry",
                "value": {"x": 10.0, "y": 20.0, "width": 300.0, "height": 200.0},
            }
        ],
        revision=session.revision,
    )
    text = annotations_file(session).read_text(encoding="utf-8")
    assert "width: 300.0" in text and "height: 200.0" in text


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


@requires_dot
def test_the_graph_route_carries_the_annotations_and_the_query_turns_them_off(
    served: str, tree: Path
) -> None:
    """The canvas drags what this payload says is there, so the route has to say.

    ``?annotations=0`` is spelled the way every other view toggle is, so a link
    to a plainer picture is one somebody can type into an address bar.
    """
    (tree / "annotations.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: note\n"
        "metadata:\n"
        "  name: why-here\n"
        "spec:\n"
        "  text: because\n"
        "  geometry: {x: 40, y: 90}\n",
        encoding="utf-8",
    )
    status, body = call(served, "/api/graph?view=l1")
    assert status == 200
    assert [note["fqn"] for note in body["annotations"]["notes"]] == ["why-here"]

    status, plain = call(served, "/api/graph?view=l1&annotations=0")
    assert status == 200
    assert plain["annotations"] is None
    assert plain["graphHash"] != body["graphHash"]


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


# --------------------------------------------------------------------------- #
# The changes drawer: the journal, the diff overlay, the handover
# --------------------------------------------------------------------------- #


def test_a_gesture_is_one_journal_entry(session: EditingSession) -> None:
    """Deleting a switch is one entry even though it is five operations."""
    session.apply([{"op": "delete", "address": "srv-nas", "cascade": True}])
    entries = session.journal()
    assert len(entries) == 1
    assert entries[0].label.startswith("delete srv-nas")
    assert len(entries[0].operations) >= 1


def test_a_batch_that_writes_nothing_is_not_logged(session: EditingSession) -> None:
    """A log of attempts is not a log of changes."""
    read = session.read_file("hosts/pc-desk.yaml")
    session.write_file("hosts/pc-desk.yaml", read["text"], base_hash=read["hash"])
    assert session.journal() == ()


def test_an_entry_carries_the_yaml_it_produced(session: EditingSession) -> None:
    """The hunk is the point: what was written, in the form a reviewer reads."""
    session.apply(
        [{"op": "set", "address": "pc-desk", "path": "spec.model", "value": "OptiPlex 7020"}]
    )
    hunk = session.journal()[0].hunk
    assert "--- a/hosts/pc-desk.yaml" in hunk
    assert "+++ b/hosts/pc-desk.yaml" in hunk
    assert "-  model: OptiPlex 7010" in hunk
    assert "+  model: OptiPlex 7020" in hunk


def test_an_entry_names_the_element_it_is_about(session: EditingSession) -> None:
    """Which is what "reveal this in the file" needs; a diff only has lines."""
    session.apply([{"op": "rename", "address": "sw-home", "new_name": "sw-attic"}])
    assert session.journal()[0].addresses == ("sw-home",)


def test_an_entry_carries_the_command_that_would_replay_it(session: EditingSession) -> None:
    session.apply([{"op": "delete", "address": "srv-nas", "cascade": True}])
    assert session.journal()[0].commands == (
        f"netgraph -i {session.root} edit delete srv-nas --cascade",
    )


def test_the_handover_is_the_whole_session_in_order(session: EditingSession) -> None:
    session.apply(
        [{"op": "set", "address": "pc-desk", "path": "spec.model", "value": "OptiPlex 7020"}]
    )
    session.apply([{"op": "delete", "address": "srv-nas", "cascade": True}])
    commands = session.changes()["commands"]
    assert len(commands) == 2
    assert "edit set pc-desk spec.model" in commands[0]
    assert "edit delete srv-nas --cascade" in commands[1]


def test_a_revert_restores_the_file_byte_for_byte(session: EditingSession) -> None:
    path = session.root / "hosts/pc-desk.yaml"
    original = path.read_bytes()
    session.apply(
        [{"op": "set", "address": "pc-desk", "path": "spec.model", "value": "OptiPlex 7020"}]
    )
    assert path.read_bytes() != original

    session.revert(1)
    assert path.read_bytes() == original


def test_a_revert_is_a_new_change_not_a_rewind(session: EditingSession) -> None:
    """So the log keeps both, and the revert is itself undoable."""
    session.apply(
        [{"op": "set", "address": "pc-desk", "path": "spec.model", "value": "OptiPlex 7020"}]
    )
    session.revert(1)
    entries = session.journal()
    assert [entry.reverted for entry in entries] == [True, False]
    assert entries[1].label.startswith("revert ")


def test_a_second_revert_of_one_entry_is_refused(session: EditingSession) -> None:
    session.apply(
        [{"op": "set", "address": "pc-desk", "path": "spec.model", "value": "OptiPlex 7020"}]
    )
    session.revert(1)
    with pytest.raises(SessionError, match="already been put back"):
        session.revert(1)


def test_reverting_a_change_this_session_never_made_is_refused(
    session: EditingSession,
) -> None:
    with pytest.raises(SessionError, match="no change numbered 7"):
        session.revert(7)


def test_a_revert_against_a_stale_revision_is_refused(session: EditingSession) -> None:
    session.apply(
        [{"op": "set", "address": "pc-desk", "path": "spec.model", "value": "OptiPlex 7020"}]
    )
    with pytest.raises(Conflict):
        session.revert(1, revision=1)


def test_a_read_only_session_reads_the_log_but_reverts_nothing(tree: Path) -> None:
    session = EditingSession(root=tree, writable=False)
    assert session.changes()["entries"] == []
    with pytest.raises(ReadOnly):
        session.revert(1)


@requires_dot
def test_the_diff_is_drawn_against_where_the_session_started(
    session: EditingSession,
) -> None:
    """Including the very first change, which is the one a lazy baseline loses."""
    session.apply(
        [{"op": "set", "address": "pc-desk", "path": "spec.model", "value": "OptiPlex 7020"}]
    )
    preview, revision = session.diff(ViewOptions())
    assert revision == session.revision
    assert preview.diff is not None
    assert preview.diff["nodes"]["hosts/pc-desk"] == "changed"
    assert preview.diff["changeset"]["summary"]["update"] == 1
    assert preview.message == "1 changed"


@requires_dot
def test_a_removed_element_is_still_drawn_in_the_diff(session: EditingSession) -> None:
    session.apply([{"op": "delete", "address": "srv-nas", "cascade": True}])
    preview, _ = session.diff(ViewOptions())
    assert preview.diff is not None
    assert preview.diff["nodes"]["hosts/srv-nas"] == "removed"
    assert "srv-nas" in (preview.svg or "")


@requires_dot
def test_an_untouched_session_diffs_to_nothing(session: EditingSession) -> None:
    preview, _ = session.diff(ViewOptions())
    assert preview.diff is not None
    assert preview.diff["counts"] == {"added": 0, "changed": 0, "removed": 0}
    assert "nothing has changed yet" in preview.message


def test_a_baseline_the_session_does_not_have_is_refused(session: EditingSession) -> None:
    with pytest.raises(SessionError, match="unknown baseline"):
        session.diff(ViewOptions(), against="yesterday")


def test_git_is_offered_only_inside_a_repository(session: EditingSession) -> None:
    """An option that always fails is not an option."""
    assert session.baselines() == ("session",)
    assert not session.in_repository()


@requires_dot
def test_the_repository_baseline_reads_head(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(HOME_LAB, root)
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present on every supported runner
        pytest.skip("git is not installed")
    for arguments in (
        ["init", "-q"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "initial"],
    ):
        subprocess.run([git, *arguments], cwd=root, check=True, capture_output=True)

    session = EditingSession(root=root, writable=True)
    assert session.baselines() == ("session", "git")
    session.apply([{"op": "delete", "address": "srv-nas", "cascade": True}])
    preview, _ = session.diff(ViewOptions(), against="git")
    assert preview.diff is not None
    assert preview.diff["nodes"]["hosts/srv-nas"] == "removed"


@requires_dot
def test_the_api_serves_the_log_the_diff_and_the_revert(served: str) -> None:
    status, empty = call(served, "/api/changes")
    assert status == 200
    assert empty["entries"] == [] and empty["baselines"] == ["session"]

    status, _ = call(
        served,
        "/api/ops",
        "POST",
        {"ops": [{"op": "set", "address": "pc-desk", "path": "spec.model", "value": "X"}]},
    )
    assert status == 200

    status, log = call(served, "/api/changes")
    assert status == 200
    assert len(log["entries"]) == 1
    assert log["entries"][0]["revertible"] is True
    assert log["commands"] and "edit set pc-desk" in log["commands"][0]

    status, drawn = call(served, "/api/diff?view=l1&against=session")
    assert status == 200
    assert drawn["against"] == "session"
    assert drawn["diff"]["nodes"]["hosts/pc-desk"] == "changed"

    status, done = call(served, "/api/revert", "POST", {"id": 1})
    assert status == 200
    assert done["revision"] > log["revision"]

    status, refused = call(served, "/api/revert", "POST", {"id": "one"})
    assert status == 400
    assert "must be the number of the change" in refused["message"]


def test_the_api_refuses_an_unknown_baseline(served: str) -> None:
    status, body = call(served, "/api/diff?against=yesterday")
    assert status == 400
    assert "unknown baseline" in body["message"]


def test_the_ordinary_graph_route_carries_no_diff(served: str) -> None:
    """An absent key must not mean two different things; see Preview.diff."""
    status, body = call(served, "/api/graph?view=l1")
    assert status == 200
    assert body["diff"] is None


# --------------------------------------------------------------------------- #
# Fixing a diagnostic
# --------------------------------------------------------------------------- #


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    """A writable copy of the repairable tree."""
    root = tmp_path / "fixable"
    root.mkdir()
    for path in sorted(FIXABLE.glob("*.yaml")):
        (root / path.name).write_bytes(path.read_bytes())
    return root


@pytest.fixture
def repairable(broken: Path) -> EditingSession:
    return EditingSession(root=broken, writable=True)


def diagnostic(session: EditingSession, rule: str) -> dict[str, Any]:
    payload = session.tree(diagnostics=True)["diagnostics"]
    return next(entry for entry in payload if entry["rule"] == rule)


def test_a_diagnostic_carries_the_repairs_on_offer(repairable: EditingSession) -> None:
    assert diagnostic(repairable, "W138")["fixes"] == [
        {"key": "prune", "title": "drop 'sw-gone' from the l1 view of layout 'default'"}
    ]
    assert [fix["key"] for fix in diagnostic(repairable, "W114")["fixes"]] == ["list", "drop"]


def test_a_diagnostic_nothing_can_repair_offers_nothing(session: EditingSession) -> None:
    for problem in session.tree(diagnostics=True)["diagnostics"]:
        assert "fixes" not in problem


def test_fixing_writes_the_file_and_is_one_undoable_gesture(
    repairable: EditingSession, broken: Path
) -> None:
    problem = diagnostic(repairable, "W108")
    change = repairable.fix(problem["rule"], problem["message"])

    assert "mac:" not in (broken / "switches.yaml").read_text(encoding="utf-8")
    assert change.undo_depth == 1
    assert [entry["rule"] for entry in change.to_dict()["diagnostics"]] == ["W138", "W113", "W114"]

    entry = repairable.changes()["entries"][0]
    assert entry["label"].startswith("fix W108: remove the MAC address")
    repairable.undo()
    assert "mac:" in (broken / "switches.yaml").read_text(encoding="utf-8")


def test_a_fix_can_be_reverted_from_the_journal(repairable: EditingSession) -> None:
    problem = diagnostic(repairable, "W138")
    repairable.fix(problem["rule"], problem["message"])
    entry = repairable.changes()["entries"][0]
    repairable.revert(entry["id"])
    assert diagnostic(repairable, "W138")["message"] == problem["message"]


def test_a_rule_with_two_repairs_has_to_be_told_which(repairable: EditingSession) -> None:
    problem = diagnostic(repairable, "W114")
    with pytest.raises(SessionError, match="more than one repair"):
        repairable.fix(problem["rule"], problem["message"])
    with pytest.raises(SessionError, match="offers list, drop"):
        repairable.fix(problem["rule"], problem["message"], key="nope")
    repairable.fix(problem["rule"], problem["message"], key="drop")
    assert not any(
        entry["rule"] == "W114" for entry in repairable.tree(diagnostics=True)["diagnostics"]
    )


def test_a_finding_the_tree_no_longer_reports_is_refused(repairable: EditingSession) -> None:
    with pytest.raises(SessionError, match="no longer reports"):
        repairable.fix("W138", "a message from another inventory")


def test_a_finding_with_no_repair_is_refused(session: EditingSession, tree: Path) -> None:
    (tree / "orphan.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: computer\nmetadata:\n  name: pc-lost\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n      mtu: 1500\n"
        "      ipv4:\n        - 10.9.9.9/24\n",
        encoding="utf-8",
    )
    problem = diagnostic(session, "W103")
    with pytest.raises(SessionError, match="no mechanical fix"):
        session.fix(problem["rule"], problem["message"])


def test_a_repair_that_would_make_things_worse_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "orphan"
    root.mkdir()
    (root / "net.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n  name: sw-a\n"
        "spec:\n  interfaces:\n    - name: port1\n      type: ethernet\n      mtu: 1500\n"
        "---\napiVersion: netgraph.dev/v1alpha1\nkind: computer\nmetadata:\n  name: pc-a\n"
        "spec:\n  interfaces:\n    - name: eno1\n      type: ethernet\n      mtu: 1500\n"
        "      ipv4:\n        - 10.0.0.9/24\n"
        "---\napiVersion: netgraph.dev/v1alpha1\nkind: cable\nmetadata:\n  name: cbl-1\n"
        "spec:\n  endpoints:\n    - sw-a:missing\n    - pc-a:eno1\n  medium: copper\n",
        encoding="utf-8",
    )
    before = (root / "net.yaml").read_text(encoding="utf-8")
    session = EditingSession(root=root, writable=True)
    problem = diagnostic(session, "E001")
    with pytest.raises(SessionError, match="would introduce"):
        session.fix(problem["rule"], problem["message"], key="remove")
    assert (root / "net.yaml").read_text(encoding="utf-8") == before


def test_a_read_only_session_does_not_fix(broken: Path) -> None:
    session = EditingSession(root=broken, writable=False)
    problem = diagnostic(session, "W138")
    with pytest.raises(ReadOnly):
        session.fix(problem["rule"], problem["message"])


def test_fixing_takes_the_revision_as_a_precondition(repairable: EditingSession) -> None:
    problem = diagnostic(repairable, "W138")
    with pytest.raises(Conflict):
        repairable.fix(problem["rule"], problem["message"], revision=99)


def test_the_api_fixes_a_diagnostic(served_repairable: str) -> None:
    status, tree = call(served_repairable, "/api/tree")
    problem = next(entry for entry in tree["diagnostics"] if entry["rule"] == "W113")
    status, change = call(
        served_repairable,
        "/api/fix",
        "POST",
        {"rule": problem["rule"], "message": problem["message"], "revision": tree["revision"]},
    )
    assert status == 200
    assert not any(entry["rule"] == "W113" for entry in change["diagnostics"])

    status, refused = call(served_repairable, "/api/fix", "POST", {"rule": "W113"})
    assert status == 400
    assert "must be a non-empty string" in refused["message"]

    status, refused = call(
        served_repairable,
        "/api/fix",
        "POST",
        {"rule": "W114", "message": problem["message"], "fix": 7},
    )
    assert status == 400
    assert "non-empty string when it is given" in refused["message"]


@pytest.fixture
def served_repairable(repairable: EditingSession) -> Iterator[str]:
    with WebServer.create(session=repairable, host="127.0.0.1", port=0) as server:
        yield server.url.rstrip("/")


# --------------------------------------------------------------------------- #
# The guided tour's scratch copies
# --------------------------------------------------------------------------- #


def _yaml(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in (".yaml", ".yml")
    }


def test_a_scratch_is_a_copy_of_the_documents_and_nothing_else(tmp_path: Path) -> None:
    """``copy_inventory`` copies what the loader reads, and leaves the rest.

    A README beside the tree, a rendered SVG, a ``.git`` directory and a
    ``_drafts`` folder are all things a real inventory folder has and none of
    them are the inventory. Copying them would make the tour slower and would
    put files in a temporary directory that nobody asked to duplicate.
    """
    source = tmp_path / "inventory"
    shutil.copytree(HOME_LAB, source)
    (source / "netgraph.toml").write_text("[render]\nlayer = 'l2'\n", encoding="utf-8")
    (source / "notes.txt").write_text("not an inventory document\n", encoding="utf-8")
    (source / "diagram.svg").write_text("<svg/>\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config.yaml").write_text("kind: not-read\n", encoding="utf-8")
    (source / "_drafts").mkdir()
    (source / "_drafts" / "wip.yaml").write_text("kind: skipped\n", encoding="utf-8")

    destination = tmp_path / "copy"
    destination.mkdir()
    copied = copy_inventory(source, destination)

    assert copied == len(_yaml(source)) - 2, "the skipped documents were copied anyway"
    assert _yaml(destination) == {
        name: text for name, text in _yaml(source).items() if not name.startswith((".", "_"))
    }
    assert (destination / "netgraph.toml").is_file(), "the copy renders with other defaults"
    assert not (destination / "notes.txt").exists()
    assert not (destination / "diagram.svg").exists()
    assert not (destination / ".git").exists()
    assert not (destination / "_drafts").exists()


def test_a_tour_edits_the_copy_and_leaves_the_tree_alone(tree: Path) -> None:
    """The whole promise, at the level the browser is not involved in."""
    session = EditingSession(root=tree, writable=False)
    before = _yaml(tree)
    tours = Tours()

    scratch = tours.open(session)
    assert scratch.root != tree
    assert scratch.session.writable, "a read-only session must still be tourable"
    assert scratch.origin == tree
    assert scratch.files == len(before)
    assert scratch.peer in session.inventory().devices

    scratch.session.apply(
        [
            {
                "op": "create",
                "kind": "switch",
                "name": "sw-tour",
                "namespace": "",
                "spec": {"interfaces": [{"name": "eth0", "type": "ethernet"}]},
            }
        ]
    )
    assert _yaml(scratch.root) != before, "the tour wrote nothing"
    assert _yaml(tree) == before, "the tour wrote to the inventory"
    assert session.revision == 1, "the real session's revision moved"

    root = scratch.root
    assert tours.close(scratch.token) is True
    assert not root.exists()
    assert tours.close(scratch.token) is False, "closing twice is not two tours"
    assert _yaml(tree) == before


def test_the_routes_answer_from_the_scratch_only_when_asked(served: str, tree: Path) -> None:
    """``?scratch=`` is the whole of the substitution, and it is opt-in.

    Two clients, one server: the one that is on a tour sees its copy, and the
    one that is not sees the tree. That is the property that lets a tour run in
    a session somebody else has open.
    """
    before = _yaml(tree)
    status, started = call(served, "/api/tour", "POST")
    assert status == 200
    token = started["scratch"]
    assert started["root"] != str(tree)
    assert started["origin"] == str(tree)
    assert started["mode"] == "session" and started["writable"]

    # The tab that is not on the tour is unaffected, and says so.
    status, state = call(served, "/api/state")
    assert status == 200 and state["root"] == str(tree)
    assert "scratch" not in state

    # The one that is, is answered from the copy — and can write, which the
    # session it is a copy of also could here, so the interesting half is where.
    status, state = call(served, f"/api/state?scratch={token}")
    assert status == 200 and state["scratch"] == token and state["root"] == started["root"]

    status, applied = call(
        served,
        f"/api/ops?scratch={token}",
        "POST",
        {
            "ops": [
                {
                    "op": "create",
                    "kind": "switch",
                    "name": "sw-tour",
                    "namespace": "",
                    "spec": {"interfaces": [{"name": "eth0", "type": "ethernet"}]},
                }
            ]
        },
    )
    assert status == 200, applied
    assert _yaml(tree) == before

    status, tour_tree = call(served, f"/api/tree?scratch={token}")
    assert status == 200
    assert any("sw-tour" in entry["path"] for entry in tour_tree["files"])
    status, real_tree = call(served, "/api/tree")
    assert not any("sw-tour" in entry["path"] for entry in real_tree["files"])

    status, ended = call(served, f"/api/tour/end?scratch={token}", "POST")
    assert status == 200 and ended["ended"] is True
    assert not Path(started["root"]).exists()
    assert _yaml(tree) == before


def test_a_stale_token_is_answered_from_the_tree_rather_than_refused(served: str) -> None:
    """A reloaded tab whose scratch has gone gets an ordinary session back.

    The alternative — a 400 on every route — leaves a page with nothing to draw
    and nothing to do about it. Degrading to the tree is safe because the tree
    is only writable if the command line said so.
    """
    status, state = call(served, "/api/state?scratch=nothing-of-the-sort")
    assert status == 200
    assert "scratch" not in state


def test_the_scratchpad_has_no_inventory_to_tour() -> None:
    """``netgraph web`` on a stream copies nothing, and says why."""
    with WebServer.create(source="", host="127.0.0.1", port=0) as server:
        status, body = call(server.url.rstrip("/"), "/api/tour", "POST")
    assert status == 400
    assert "scratchpad" in body["message"]


def test_only_so_many_tours_may_run_at_once(tree: Path) -> None:
    """A cap, because each one is a copy of the tree on the disk."""
    session = EditingSession(root=tree, writable=False)
    tours = Tours()
    try:
        opened = [tours.open(session) for _ in range(MAX_SCRATCHES)]
        assert len(tours) == MAX_SCRATCHES
        with pytest.raises(SessionError, match="already running"):
            tours.open(session)
    finally:
        tours.close_all()
    assert len(tours) == 0
    for scratch in opened:
        assert not scratch.root.exists()


def test_an_inventory_too_big_to_copy_is_refused_rather_than_copied(
    tree: Path, monkeypatch: Any
) -> None:
    """The bound exists so a mistyped root cannot fill a disk.

    Lowered here rather than by generating twenty thousand files: the number is
    a policy and the refusal is the behaviour, and only one of the two is worth
    two minutes of a test run.
    """
    monkeypatch.setattr("netgraph.web.tour.MAX_FILES", 1)
    tours = Tours()
    with pytest.raises(TooLarge, match="too big to tour"):
        tours.open(EditingSession(root=tree))
    assert len(tours) == 0, "the half-made copy was left behind"


def test_an_untouched_tour_expires(tree: Path, monkeypatch: Any) -> None:
    """The backstop for the tab that crashed instead of saying goodbye."""
    tours = Tours()
    scratch = tours.open(EditingSession(root=tree))
    monkeypatch.setattr("netgraph.web.tour.TTL_SECONDS", -1.0)
    assert tours.open(EditingSession(root=tree)).token != scratch.token
    assert not scratch.root.exists()
    assert tours.get(scratch.token) is None
    tours.close_all()


def test_stopping_the_server_deletes_every_copy(session: EditingSession) -> None:
    """Ctrl-C on ``netgraph web`` should not leave a tree in /tmp."""
    with WebServer.create(session=session, host="127.0.0.1", port=0) as server:
        base = server.url.rstrip("/")
        roots = [Path(call(base, "/api/tour", "POST")[1]["root"]) for _ in range(2)]
        assert all(root.is_dir() for root in roots)
    assert not any(root.exists() for root in roots)


# --------------------------------------------------------------------------- #
# Arranging a selection
# --------------------------------------------------------------------------- #
#
# The session's half of align/distribute/snap: the revision precondition, one
# entry in the undo stack, and the refusals. The arithmetic itself is
# ``tests/test_edit_batch.py``'s.

#: The l1 view of the example inventory, arranged — three nodes ragged enough
#: for an alignment to have something to do.
RAGGED_LAYOUT = """\
apiVersion: netgraph.dev/v1alpha1
kind: layout
metadata:
  name: default
spec:
  views:
    l1:
      nodes:
        switches/sw-home: {position: {x: 10, y: 300}}
        hosts/pc-desk: {position: {x: 40, y: 200}}
        hosts/srv-nas: {position: {x: 70, y: 100}}
"""

ARRANGE_THREE = ["switches/sw-home", "hosts/pc-desk", "hosts/srv-nas"]


@pytest.fixture
def ragged(tree: Path) -> EditingSession:
    (tree / "layout.yaml").write_text(RAGGED_LAYOUT, encoding="utf-8", newline="\n")
    return EditingSession(root=tree, writable=True)


def test_aligning_is_one_change_and_one_undo(ragged: EditingSession) -> None:
    before = ragged.revision
    change = ragged.arrange(
        "align.left", view="l1", addresses=ARRANGE_THREE, revision=ragged.revision
    )
    assert change.revision != before
    assert list(change.files) == ["layout.yaml"], "one file, however many nodes moved"
    assert change.undo_depth == 1, "a whole alignment is one step to undo"
    text = (ragged.root / "layout.yaml").read_text(encoding="utf-8")
    assert text.count("x: 10") == 3

    ragged.undo()
    assert (ragged.root / "layout.yaml").read_text(encoding="utf-8") == RAGGED_LAYOUT


def test_aligning_something_already_aligned_writes_nothing(ragged: EditingSession) -> None:
    ragged.arrange("align.left", view="l1", addresses=ARRANGE_THREE)
    settled = ragged.revision
    change = ragged.arrange("align.left", view="l1", addresses=ARRANGE_THREE)
    assert change.files == {}
    assert change.revision == settled, "a no-op must not move the revision"
    assert change.undo_depth == 1, "nor grow the undo stack"


def test_snapping_uses_the_grid_from_netgraph_toml(tree: Path) -> None:
    (tree / "layout.yaml").write_text(RAGGED_LAYOUT, encoding="utf-8", newline="\n")
    (tree / "netgraph.toml").write_text("[editor]\ngrid = 100\n", encoding="utf-8", newline="\n")
    session = EditingSession(root=tree, writable=True)
    assert session.grid() == 100
    assert session.state()["grid"] == 100

    session.arrange("snap", view="l1", addresses=ARRANGE_THREE)
    text = (session.root / "layout.yaml").read_text(encoding="utf-8")
    assert "x: 0" in text and "x: 100" in text


def test_an_unknown_arrangement_is_refused(ragged: EditingSession) -> None:
    with pytest.raises(SessionError, match="unknown arrangement"):
        ragged.arrange("align.diagonally", view="l1", addresses=ARRANGE_THREE)


def test_an_arrangement_decided_against_an_older_tree_is_refused(
    ragged: EditingSession,
) -> None:
    stale = ragged.revision
    ragged.invalidate()
    with pytest.raises(Conflict, match="reload before arranging"):
        ragged.arrange("align.left", view="l1", addresses=ARRANGE_THREE, revision=stale)


def test_a_read_only_session_will_not_arrange(tree: Path) -> None:
    (tree / "layout.yaml").write_text(RAGGED_LAYOUT, encoding="utf-8", newline="\n")
    with pytest.raises(ReadOnly):
        EditingSession(root=tree).arrange("align.left", view="l1", addresses=ARRANGE_THREE)


def test_arranging_an_unarranged_view_says_what_is_missing(session: EditingSession) -> None:
    with pytest.raises(EditError, match="0 of the 3 selected"):
        session.arrange("align.left", view="l1", addresses=ARRANGE_THREE)


def test_the_arrange_route_writes_and_refuses_over_http(ragged: EditingSession) -> None:
    with WebServer.create(session=ragged, host="127.0.0.1", port=0) as server:
        base = server.url.rstrip("/")
        status, body = call(
            base,
            "/api/arrange",
            "POST",
            {"command": "align.left", "view": "l1", "addresses": ARRANGE_THREE},
        )
        assert status == 200, body
        assert list(body["files"]) == ["layout.yaml"]

        # The two shapes of a bad request, each named rather than 500ed.
        status, body = call(base, "/api/arrange", "POST", {"view": "l1", "addresses": ["a"]})
        assert status == 400 and "command" in body["message"]
        status, body = call(
            base, "/api/arrange", "POST", {"command": "snap", "view": "l1", "addresses": []}
        )
        assert status == 400 and "addresses" in body["message"]
