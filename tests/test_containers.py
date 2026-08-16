"""Containers: a namespace box you can drop things into, and what that writes.

The gesture this file is about is draw.io's defining one — drag a shape into a
box and it belongs to the box — mapped onto the one structure netviz already
has for belonging: the folder a document sits in (§2). So the properties tested
here are all really the same property said at four levels:

* **A drop is a move.** :func:`~netviz.edit.containers.move_plan` turns a drop
  payload into ``move`` operations, the document is rewritten into the target
  directory, and every reference to it is re-spelled by
  :mod:`netviz.edit.references` — the tree still resolves afterwards.
* **A drop that cannot be a move is refused before anything is written.** A name
  already taken in the target, two dragged documents that would collide with
  each other, a folder the loader would skip: each is a sentence naming both
  sides, and the tree is untouched.
* **A container is geometry.** Resizing one writes ``groups`` into the
  ``kind: layout`` document (§18), where the renderer reads it back — the one
  place a rectangle somebody dragged becomes a number in a file.
* **Folding is not.** ``--collapse`` is a *view*: the drawing changes, the
  fingerprint changes, and no file does.

The browser half — the actual drag — is in ``tests/test_browser.py``, which is
where a gesture can be pressed rather than described.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from netviz.edit import (
    EditError,
    EditSession,
    MoveElement,
    PlacementError,
    check_namespace,
    containers,
    move_plan,
)
from netviz.edit.containers import MAX_MOVES
from netviz.loader import load_tree
from netviz.render import Layer, build_graph
from netviz.validate import validate
from netviz.web.preview import MAX_COLLAPSED, RequestError, ViewOptions, render_inventory
from netviz.web.server import WebServer
from netviz.web.session import Conflict, EditingSession, ReadOnly

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES: Final = REPO_ROOT / "examples"

#: A second switch called ``sw-north-acc-01``, in another site. Nothing is wrong
#: with it — two racks may each hold an ``sw-01`` — right up until somebody
#: drops one into the other's namespace, which is the refusal this file pins.
TWIN: Final = """\
apiVersion: netviz.dev/v1alpha1
kind: switch
metadata:
  name: sw-north-acc-01
spec:
  interfaces:
    - name: eth0
      type: ethernet
"""


def _write_shadowed(root: Path) -> None:
    """Two switches called ``sw1``, in two folders, and a cable naming one bare."""
    for namespace, name, kind in (
        ("a", "sw1", "switch"),
        ("b", "sw1", "switch"),
        ("a", "pc1", "computer"),
    ):
        directory = root / namespace
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.yaml").write_text(
            "apiVersion: netviz.dev/v1alpha1\n"
            f"kind: {kind}\n"
            f"metadata:\n  name: {name}\n"
            "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n",
            encoding="utf-8",
        )
    (root / "a" / "cables.yaml").write_text(
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata:\n  name: link\n"
        "spec:\n  medium: copper\n  endpoints:\n    - sw1:eth0\n    - pc1:eth0\n",
        encoding="utf-8",
    )


@pytest.fixture()
def campus(tmp_path: Path) -> Path:
    """A writable copy of ``examples/campus`` — three sites, nested namespaces."""
    root = tmp_path / "campus"
    shutil.copytree(EXAMPLES / "campus", root)
    return root


def facts_of(root: Path) -> dict[str, Any]:
    """The file facts a drop is planned against, as the session builds them."""
    session = EditSession(root=root)
    return dict(session.tree.facts(session.inventory))


def plan_for(root: Path, addresses: list[str], namespace: str) -> Any:
    """The plan one drop would produce, against the tree as it is on disk."""
    session = EditSession(root=root)
    return move_plan(session.inventory, addresses, namespace=namespace, files=facts_of(root))


def drop(root: Path, addresses: list[str], namespace: str) -> EditSession:
    """Apply one drop and write it, answering with the session that did."""
    session = EditSession(root=root)
    plan = move_plan(session.inventory, addresses, namespace=namespace, files=facts_of(root))
    session.apply_all(plan.operations)
    session.commit()
    return session


# --------------------------------------------------------------------------- #
# The plan: a drop payload becomes `netviz edit move`
# --------------------------------------------------------------------------- #


def test_a_drop_becomes_one_move_per_document(campus: Path) -> None:
    plan = plan_for(campus, ["sites/north/access/sw-north-acc-01"], "sites/south/access")
    assert [operation.to_dict() for operation in plan.operations] == [
        {
            "op": "move",
            "address": "sites/north/access/sw-north-acc-01",
            "file": "sites/south/access/switches.yaml",
        }
    ]
    assert plan.moves[0].target == "sites/south/access/sw-north-acc-01"
    assert plan.describe() == ("moved sites/north/access/sw-north-acc-01 into sites/south/access")


def test_the_target_file_is_the_placement_convention_and_not_a_guess(campus: Path) -> None:
    """A dropped switch joins the switches; a dropped cable joins the cables.

    The whole reason the drop is planned on the server: a browser knows which
    rectangle the pointer was over and nothing about which files exist.
    """
    switch = plan_for(campus, ["sites/north/access/sw-north-acc-01"], "sites/south/access")
    cable = plan_for(campus, ["sites/north/cables/cbl-north-acc01-pc01"], "sites/south/cables")
    assert switch.moves[0].file == "sites/south/access/switches.yaml"
    assert cable.moves[0].file == "sites/south/cables/links.yaml"


def test_a_drop_into_a_namespace_that_does_not_exist_yet_names_a_new_file(campus: Path) -> None:
    """A folder is made by putting something in it; that is all a namespace is."""
    plan = plan_for(campus, ["sites/north/access/sw-north-acc-01"], "sites/north/racks/r1")
    assert plan.moves[0].file == "sites/north/racks/r1/sw-north-acc-01.yaml"


def test_a_drop_on_empty_canvas_moves_to_the_root(campus: Path) -> None:
    plan = plan_for(campus, ["sites/north/access/sw-north-acc-01"], "")
    assert plan.namespace == ""
    assert plan.moves[0].target == "sw-north-acc-01"
    # No switch lives at the root yet, so the placement convention gives this one
    # a file of its own rather than inventing a collection for it.
    assert plan.moves[0].file == "sw-north-acc-01.yaml"


def test_a_drop_where_it_already_is_writes_nothing(campus: Path) -> None:
    """A drag that ends where it started is how somebody decides not to move."""
    plan = plan_for(campus, ["sites/north/access/sw-north-acc-01"], "sites/north/access")
    assert not plan
    assert plan.operations == ()
    assert plan.unchanged == ("sites/north/access/sw-north-acc-01",)


def test_dropping_a_multi_selection_is_one_batch(campus: Path) -> None:
    plan = plan_for(
        campus,
        ["sites/north/access/sw-north-acc-01", "sites/north/access/sw-north-acc-02"],
        "sites/south/access",
    )
    assert len(plan.operations) == 2
    assert all(isinstance(operation, MoveElement) for operation in plan.operations)
    assert plan.describe() == "moved 2 elements into sites/south/access"


def test_dropping_a_container_keeps_the_subtree_its_own_shape(campus: Path) -> None:
    """A rack dragged into a site arrives as a rack, not as its contents."""
    plan = plan_for(campus, ["sites/north/access"], "sites/south")
    assert [move.target for move in plan.moves] == [
        "sites/south/access/sw-north-acc-01",
        "sites/south/access/sw-north-acc-02",
        "sites/south/access/sw-north-acc-03",
    ]


def test_a_nested_pair_is_carried_by_the_outer_namespace(campus: Path) -> None:
    """Dragging a site *and* a rack inside it moves the site, rack and all.

    The rack keeps its place under the site rather than being lifted out and
    dropped beside it, which is what matching the *inner* root would have done.
    """
    plan = plan_for(campus, ["sites/north", "sites/north/access"], "estate")
    assert "estate/north/access/sw-north-acc-01" in {move.target for move in plan.moves}


# --------------------------------------------------------------------------- #
# What the move actually does to the files
# --------------------------------------------------------------------------- #


def test_the_document_lands_in_the_folder_and_leaves_the_old_one(campus: Path) -> None:
    drop(campus, ["sites/north/access/sw-north-acc-01"], "sites/south/access")
    moved = (campus / "sites/south/access/switches.yaml").read_text(encoding="utf-8")
    left = (campus / "sites/north/access/switches.yaml").read_text(encoding="utf-8")
    assert "name: sw-north-acc-01" in moved
    assert "name: sw-north-acc-01" not in left


def test_the_document_travels_verbatim(campus: Path) -> None:
    """A move is a move, not a reformat: every comment and blank line survives."""
    before = (campus / "sites/north/access/switches.yaml").read_text(encoding="utf-8")
    wanted = before[before.index("apiVersion") : before.index("name: sw-north-acc-02")]
    drop(campus, ["sites/north/access/sw-north-acc-01"], "sites/south/access")
    after = (campus / "sites/south/access/switches.yaml").read_text(encoding="utf-8")
    assert wanted.split("---")[0].strip() in after


def test_every_reference_still_resolves_after_the_move(campus: Path) -> None:
    """The point of routing a drop through the mutation layer at all.

    Four cables terminate on this switch from another namespace. After it moves
    the inventory must still load with no new complaint — which is only true if
    the references were re-spelled where the old spelling stopped meaning it.
    """
    before = [finding.rule for finding in validate(load_tree(campus))]
    drop(campus, ["sites/north/access/sw-north-acc-01"], "sites/south/access")
    inventory = load_tree(campus)
    assert not inventory.errors
    assert "sites/south/access/sw-north-acc-01" in inventory.elements
    assert [finding.rule for finding in validate(inventory)] == before


def test_a_move_that_breaks_a_bare_reference_re_qualifies_it(tmp_path: Path) -> None:
    """When the short name stops meaning it, the reference is re-spelled.

    ``a/cable`` names ``sw1`` bare, which resolves to ``a/sw1`` because the two
    are in the same folder. Drop ``a/sw1`` into ``c`` and the bare name no longer
    resolves at all — there is a ``b/sw1`` as well, so nothing outside ``a`` can
    guess. The move has to leave the cable naming the switch unambiguously, and
    this is the whole reason a drop goes through the mutation layer.
    """
    root = tmp_path / "shadowed"
    _write_shadowed(root)
    assert load_tree(root).lookup("sw1", namespace="a").fqn == "a/sw1"

    drop(root, ["a/sw1"], "c")

    inventory = load_tree(root)
    assert not inventory.errors
    assert "c/sw1" in inventory.elements
    cables = (root / "a/cables.yaml").read_text(encoding="utf-8")
    assert "c/sw1:eth0" in cables
    assert inventory.cables["a/link"].spec.endpoints[0].device == "c/sw1"


def test_undoing_a_drop_puts_the_tree_back_byte_for_byte(campus: Path) -> None:
    before = {
        path.relative_to(campus).as_posix(): path.read_bytes()
        for path in sorted(campus.rglob("*.yaml"))
    }
    session = drop(campus, ["sites/north/access/sw-north-acc-01"], "sites/south/access")
    undo = EditSession(root=campus)
    for applied in reversed(session.applied):
        undo.apply_all(applied.inverse)
    undo.commit()
    after = {
        path.relative_to(campus).as_posix(): path.read_bytes()
        for path in sorted(campus.rglob("*.yaml"))
    }
    assert after == before


# --------------------------------------------------------------------------- #
# Refusals, every one of them before a byte is written
# --------------------------------------------------------------------------- #


def test_a_drop_onto_a_taken_name_is_refused_and_names_both(tmp_path: Path) -> None:
    root = tmp_path / "campus"
    shutil.copytree(EXAMPLES / "campus", root)
    (root / "sites/west/access/twin.yaml").write_text(TWIN, encoding="utf-8")
    before = {path: path.read_bytes() for path in sorted(root.rglob("*.yaml"))}

    with pytest.raises(EditError) as raised:
        plan_for(root, ["sites/north/access/sw-north-acc-01"], "sites/west/access")

    assert "sites/west/access/sw-north-acc-01" in str(raised.value)
    assert "cannot share a name" in str(raised.value)
    assert {path: path.read_bytes() for path in sorted(root.rglob("*.yaml"))} == before


def test_two_dragged_documents_that_would_collide_are_refused(tmp_path: Path) -> None:
    """The refusal a person reaches by selecting two racks and dropping both."""
    root = tmp_path / "campus"
    shutil.copytree(EXAMPLES / "campus", root)
    (root / "sites/west/access/twin.yaml").write_text(TWIN, encoding="utf-8")

    with pytest.raises(EditError) as raised:
        plan_for(
            root,
            ["sites/north/access/sw-north-acc-01", "sites/west/access/sw-north-acc-01"],
            "sites/south/access",
        )

    assert "would both become" in str(raised.value)


@pytest.mark.parametrize(
    "namespace",
    ["../escape", "sites/../..", "_private", "sites/.hidden", "sites/_scratch/x"],
)
def test_a_namespace_the_loader_would_skip_is_refused(namespace: str) -> None:
    with pytest.raises(PlacementError):
        check_namespace(namespace)


def test_the_root_namespace_is_legal_and_is_the_empty_string() -> None:
    assert check_namespace("") == ""
    assert check_namespace("/") == ""
    # A leading and a trailing slash are the same folder, because "/sites/north/"
    # is how a person types what the tree calls "sites/north".
    assert check_namespace("/sites/north/") == "sites/north"


def test_an_address_that_names_nothing_is_refused(campus: Path) -> None:
    with pytest.raises(EditError) as raised:
        plan_for(campus, ["there-is-no-such-switch"], "sites/south")
    assert "there is no element or namespace" in str(raised.value)


def test_dropping_nothing_is_refused(campus: Path) -> None:
    with pytest.raises(EditError, match="nothing was named"):
        plan_for(campus, [], "sites/south")


def test_a_drop_larger_than_a_gesture_is_refused(
    campus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drag that would rewrite the estate is a script, not a gesture.

    The bound is lowered rather than the tree grown: what is being tested is
    that the ceiling exists and names the command to use instead, not how many
    documents fit under it.
    """
    session = EditSession(root=campus)
    assert len(session.inventory.elements) <= MAX_MOVES, (
        "the real bound is meant to be well above a real drop"
    )
    monkeypatch.setattr(containers, "MAX_MOVES", 2)
    with pytest.raises(EditError, match="netviz edit move"):
        move_plan(session.inventory, ["sites/north"], namespace="estate")


# --------------------------------------------------------------------------- #
# The session: the route the browser actually calls
# --------------------------------------------------------------------------- #


def test_the_session_moves_the_file_and_records_one_gesture(campus: Path) -> None:
    session = EditingSession(root=campus, writable=True)
    change = session.reparent(
        ["sites/north/access/sw-north-acc-01"], namespace="sites/south/access"
    )
    assert sorted(change.files) == [
        "sites/north/access/switches.yaml",
        "sites/south/access/switches.yaml",
    ]
    assert [gesture.label for gesture in session.journal()] == [
        "moved sites/north/access/sw-north-acc-01 into sites/south/access"
    ]
    assert "name: sw-north-acc-01" in (campus / "sites/south/access/switches.yaml").read_text(
        encoding="utf-8"
    )


def test_a_read_only_session_refuses_the_drop(campus: Path) -> None:
    session = EditingSession(root=campus)
    with pytest.raises(ReadOnly):
        session.reparent(["sites/north/access/sw-north-acc-01"], namespace="sites/south")


def test_a_drop_decided_against_an_older_tree_is_refused(campus: Path) -> None:
    session = EditingSession(root=campus, writable=True)
    with pytest.raises(Conflict):
        session.reparent(
            ["sites/north/access/sw-north-acc-01"],
            namespace="sites/south/access",
            revision=session.revision + 1,
        )


def test_a_drop_where_it_already_is_does_not_move_the_revision(campus: Path) -> None:
    session = EditingSession(root=campus, writable=True)
    before = session.revision
    change = session.reparent(
        ["sites/north/access/sw-north-acc-01"], namespace="sites/north/access"
    )
    assert change.files == {}
    assert session.revision == before


def test_a_refused_drop_leaves_the_session_at_the_same_revision(tmp_path: Path) -> None:
    root = tmp_path / "campus"
    shutil.copytree(EXAMPLES / "campus", root)
    (root / "sites/west/access/twin.yaml").write_text(TWIN, encoding="utf-8")
    session = EditingSession(root=root, writable=True)
    before = session.revision

    with pytest.raises(EditError):
        session.reparent(["sites/north/access/sw-north-acc-01"], namespace="sites/west/access")

    assert session.revision == before
    assert "name: sw-north-acc-01" in (root / "sites/north/access/switches.yaml").read_text(
        encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Over HTTP: the route the drop actually posts to
# --------------------------------------------------------------------------- #


def call(base: str, path: str, body: Any) -> tuple[int, Any]:
    """One POST against the running server, with its body or its refusal."""
    request = urllib.request.Request(
        base + path,
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture()
def served(campus: Path) -> Iterator[str]:
    session = EditingSession(root=campus, writable=True)
    with WebServer.create(session=session, host="127.0.0.1", port=0) as server:
        yield server.url.rstrip("/")


def test_the_route_moves_the_document(served: str, campus: Path) -> None:
    status, change = call(
        served,
        "/api/reparent",
        {
            "addresses": ["sites/north/access/sw-north-acc-01"],
            "namespace": "sites/south/access",
        },
    )
    assert status == 200
    assert sorted(change["files"]) == [
        "sites/north/access/switches.yaml",
        "sites/south/access/switches.yaml",
    ]
    assert "name: sw-north-acc-01" in (campus / "sites/south/access/switches.yaml").read_text(
        encoding="utf-8"
    )


def test_the_route_takes_the_empty_namespace_to_mean_the_root(served: str) -> None:
    """The one string field where ``""`` is the value and not a client bug."""
    status, change = call(
        served,
        "/api/reparent",
        {"addresses": ["sites/north/access/sw-north-acc-01"], "namespace": ""},
    )
    assert status == 200
    assert "sw-north-acc-01.yaml" in change["files"]


def test_the_route_refuses_a_drop_it_cannot_make(served: str) -> None:
    status, refused = call(
        served,
        "/api/reparent",
        {"addresses": ["there-is-no-such-switch"], "namespace": "sites/south"},
    )
    assert status == 400
    assert "there is no element or namespace" in refused["message"]


def test_the_route_refuses_a_stale_revision(served: str) -> None:
    status, refused = call(
        served,
        "/api/reparent",
        {
            "addresses": ["sites/north/access/sw-north-acc-01"],
            "namespace": "sites/south/access",
            "revision": 99,
        },
    )
    assert status == 409
    assert "reload before moving" in refused["message"]


def test_a_read_only_server_refuses_the_route(campus: Path) -> None:
    session = EditingSession(root=campus)
    with WebServer.create(session=session, host="127.0.0.1", port=0) as server:
        status, refused = call(
            server.url.rstrip("/"),
            "/api/reparent",
            {"addresses": ["sites/north/access/sw-north-acc-01"], "namespace": "estate"},
        )
    assert status == 403
    assert "--write" in refused["message"]


# --------------------------------------------------------------------------- #
# What the page is told: the container payload
# --------------------------------------------------------------------------- #


@requires_dot
def test_every_namespace_level_is_published_as_a_container() -> None:
    """One frame per level, not one per Graphviz cluster; see ``_containers``."""
    preview = render_inventory(load_tree(EXAMPLES / "campus"), ViewOptions(group_by_namespace=True))
    by_namespace = {entry["namespace"]: entry for entry in preview.containers}
    assert "sites" in by_namespace, "the level between the root and a site is a container too"
    assert by_namespace["sites"]["depth"] == 1
    assert by_namespace["sites"]["parent"] == ""
    assert by_namespace["sites/north"]["parent"] == "sites"
    assert by_namespace["sites/north/access"]["label"] == "access"


@requires_dot
def test_only_a_namespace_graphviz_boxes_can_be_resized() -> None:
    """``boxed`` is the difference between a stored rectangle and a hull."""
    preview = render_inventory(load_tree(EXAMPLES / "campus"), ViewOptions(group_by_namespace=True))
    by_namespace = {entry["namespace"]: entry for entry in preview.containers}
    assert by_namespace["sites/north/access"]["boxed"] is True
    assert by_namespace["sites/north"]["boxed"] is False, (
        "a level holding no element of its own has nowhere for a resize to go"
    )


@requires_dot
def test_a_container_counts_everything_at_or_below_it() -> None:
    preview = render_inventory(load_tree(EXAMPLES / "campus"), ViewOptions(group_by_namespace=True))
    by_namespace = {entry["namespace"]: entry for entry in preview.containers}
    assert by_namespace["sites/north"]["count"] == sum(
        by_namespace[level]["count"] for level in by_namespace if level.startswith("sites/north/")
    )


@requires_dot
def test_an_ungrouped_drawing_has_no_containers_at_all() -> None:
    """The contract the whole editor layer is switched off by; see ``_containers``."""
    preview = render_inventory(load_tree(EXAMPLES / "campus"), ViewOptions())
    assert list(preview.containers) == []


# --------------------------------------------------------------------------- #
# Folding: a view, and only a view
# --------------------------------------------------------------------------- #


@requires_dot
def test_folding_a_namespace_draws_it_as_one_node(campus: Path) -> None:
    inventory = load_tree(campus)
    open_ = render_inventory(inventory, ViewOptions(group_by_namespace=True))
    shut = render_inventory(
        inventory, ViewOptions(group_by_namespace=True, collapse=("sites/north",))
    )
    assert shut.nodes < open_.nodes
    assert shut.graph_hash != open_.graph_hash


@requires_dot
def test_a_folded_container_still_says_what_it_stands_for(campus: Path) -> None:
    """So it can be unfolded, and so its header still counts what is inside."""
    preview = render_inventory(
        load_tree(campus), ViewOptions(group_by_namespace=True, collapse=("sites/north",))
    )
    by_namespace = {entry["namespace"]: entry for entry in preview.containers}
    assert by_namespace["sites/north"]["collapsed"] is True
    assert by_namespace["sites/north"]["count"] == 8
    assert by_namespace["sites/north"]["element"] == "node-ns_sites_north"
    assert by_namespace["sites/north/access"]["hidden"] is True, (
        "a container inside a folded one is not on the page and must not be dropped on"
    )


@requires_dot
def test_folding_writes_nothing(campus: Path) -> None:
    before = {path: path.read_bytes() for path in sorted(campus.rglob("*.yaml"))}
    render_inventory(
        load_tree(campus), ViewOptions(group_by_namespace=True, collapse=("sites/north",))
    )
    assert {path: path.read_bytes() for path in sorted(campus.rglob("*.yaml"))} == before


def test_the_collapse_option_is_read_off_a_query_string() -> None:
    options = ViewOptions.from_query({"collapse": ["sites/north,/sites/south/"]})
    assert options.collapse == ("sites/north", "sites/south")


def test_a_request_cannot_ask_for_an_unbounded_fold() -> None:
    with pytest.raises(RequestError, match="more than"):
        ViewOptions.from_request({"collapse": [f"ns{index}" for index in range(MAX_COLLAPSED + 1)]})


def test_a_collapse_that_is_not_namespaces_is_refused() -> None:
    with pytest.raises(RequestError, match="namespace"):
        ViewOptions.from_request({"collapse": [1, 2]})


# --------------------------------------------------------------------------- #
# Geometry: a resized container is a number in a file
# --------------------------------------------------------------------------- #

#: What resizing the ``sites/north/access`` frame posts: one ``set-geometry``
#: naming one group, exactly as ``containers.js`` builds it.
RESIZE: Final[dict[str, Any]] = {
    "op": "set-geometry",
    "view": "l1",
    "groups": {
        "sites/north/access": {
            "position": {"x": 240.0, "y": 468.0},
            "size": {"width": 320.0, "height": 260.0},
        }
    },
}


def test_resizing_a_container_writes_its_box_into_the_layout(campus: Path) -> None:
    EditingSession(root=campus, writable=True).apply([RESIZE])

    layouts = [
        path
        for path in sorted(campus.rglob("*.yaml"))
        if "kind: layout" in path.read_text(encoding="utf-8")
    ]
    assert layouts, "a resize has to land in a kind: layout document"
    text = "\n".join(path.read_text(encoding="utf-8") for path in layouts)
    assert "groups:" in text
    assert "sites/north/access:" in text
    assert "width: 320" in text and "height: 260" in text


def test_the_written_box_is_read_back_as_the_container_geometry(campus: Path) -> None:
    """The round trip that makes a resize mean something: written, then honoured."""
    EditingSession(root=campus, writable=True).apply([RESIZE])
    geometry = build_graph(load_tree(campus), layer=Layer.L1).geometry
    box = geometry.groups["sites/north/access"]
    assert (box.width, box.height) == (320.0, 260.0)
    assert (box.x, box.y) == (240.0, 468.0)


def test_a_container_size_is_only_written_when_somebody_sets_one(campus: Path) -> None:
    """Seeded on purpose and never by accident; see ``docs/follow-ups.md`` §16."""
    assert not any("groups:" in path.read_text(encoding="utf-8") for path in campus.rglob("*.yaml"))
    EditingSession(root=campus, writable=True).apply([RESIZE])
    written = [
        path for path in campus.rglob("*.yaml") if "groups:" in path.read_text(encoding="utf-8")
    ]
    assert len(written) == 1


def test_undoing_a_resize_removes_the_box_again(campus: Path) -> None:
    session = EditingSession(root=campus, writable=True)
    session.apply([RESIZE])
    session.undo()
    assert not any(
        "sites/north/access:" in path.read_text(encoding="utf-8")
        for path in campus.rglob("*.yaml")
        if "kind: layout" in path.read_text(encoding="utf-8")
    )
