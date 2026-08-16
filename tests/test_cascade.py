"""A delete takes everything that cannot outlive it — and nothing else.

Three layers, one gesture, and the invariant that ties them together is at the
bottom of this file: **a cascading delete never leaves a finding behind.** A tree
that validated clean before one still does after, which is the only definition of
"cascade" a person editing a diagram can check without reading the schema.

That is a stronger claim than "the cables go too", and it is the one that was
false: deleting a switch used to take its cables and leave eight ``W138``
warnings naming the coordinates that had placed all of it, plus a ``W142`` for
any note anchored to it. What follows pins each layer, then the invariant over every
arranged and annotated fixture in the repository.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from netgraph.edit import (
    CascadeRequired,
    DeleteElement,
    Disconnect,
    EditSession,
    ValidationRefused,
)
from netgraph.edit import cascade as cascade_module
from netgraph.edit.cascade import describe, plan_cascade
from netgraph.loader import load_tree
from netgraph.validate import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The rules a half-done delete produces, and the reason this module exists.
LITTER = ("W138", "W142", "W143")


@pytest.fixture()
def arranged(tmp_path: Path) -> Path:
    """A writable copy of ``tests/fixtures/arranged`` — home-lab, with geometry."""
    root = tmp_path / "arranged"
    shutil.copytree(FIXTURES / "arranged", root)
    return root


@pytest.fixture()
def campus(tmp_path: Path) -> Path:
    """A writable copy of ``examples/campus`` — namespaces, notes, an area."""
    root = tmp_path / "campus"
    shutil.copytree(EXAMPLES / "campus", root)
    return root


def apply_and_commit(root: Path, *operations: object) -> EditSession:
    session = EditSession(root=root)
    for operation in operations:
        session.apply(operation)  # type: ignore[arg-type]
    session.commit()
    return session


def litter(root: Path) -> list[str]:
    """Every stale-geometry and stale-annotation finding the tree carries."""
    inventory = load_tree(root)
    return [
        f"{finding.rule} {finding.message}"
        for finding in validate(inventory)
        if finding.rule in LITTER
    ]


def documents(path: Path) -> list[dict]:
    return [one for one in yaml.safe_load_all(path.read_text(encoding="utf-8")) if one]


def note(root: Path, name: str) -> dict | None:
    for path in sorted(root.rglob("*.yaml")):
        for document in documents(path):
            if document.get("kind") in ("note", "area") and document["metadata"]["name"] == name:
                return document
    return None


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Geometry (§18)
# --------------------------------------------------------------------------- #


def test_a_cascade_takes_the_coordinates_that_placed_it(arranged: Path) -> None:
    """The bug this module was written for: eight warnings where there were none."""
    assert not litter(arranged), "the fixture starts clean"
    plan = plan_cascade(load_tree(arranged), ["switches/sw-home"])
    assert len(plan.geometry) == 8, "the figure the changelog quotes"

    apply_and_commit(arranged, DeleteElement(address="sw-home", cascade=True))
    assert not litter(arranged)

    nodes = documents(arranged / "layout.yaml")[0]["spec"]["views"]["l1"]["nodes"]
    assert "switches/sw-home" not in nodes
    assert not [key for key in nodes if key.startswith("cables/cbl-sw")], "its cables went too"
    assert "routers/rtr-home" in nodes, "and nothing else moved"


def test_the_box_round_an_emptied_namespace_goes_and_a_shared_one_stays(arranged: Path) -> None:
    """A group key is a namespace, and a namespace is emptied rather than deleted."""
    groups = documents(arranged / "layout.yaml")[0]["spec"]["views"]["l2"]["groups"]
    assert {"switches", "hosts"} <= set(groups)

    apply_and_commit(arranged, DeleteElement(address="sw-home", cascade=True))
    groups = documents(arranged / "layout.yaml")[0]["spec"]["views"]["l2"]["groups"]
    assert "switches" not in groups, "nothing is left in it to box"
    assert "hosts" in groups, "which still holds four"


def test_a_layout_that_places_nothing_is_removed_with_its_file(arranged: Path) -> None:
    """The last entry takes the section, the view, the document and the file."""
    session = EditSession(root=arranged)
    for fqn in sorted(load_tree(arranged).elements):
        if fqn in session.inventory.elements:  # its cables may already have gone
            session.apply(DeleteElement(address=fqn, cascade=True))
    session.commit()
    assert not (arranged / "layout.yaml").exists()


def test_an_element_an_annotation_and_a_layout_in_one_file_all_go(tmp_path: Path) -> None:
    """Three documents in one file, removed in one sweep — the renumbering trap.

    ``remove_document`` shifts the index of everything after it in its file, so
    a delete that removes the element at index 0 and then looks up the note it
    indexed at 2 lands on the layout instead. All three are collected first and
    removed highest-index-first, which is what this pins.
    """
    root = tmp_path / "onefile"
    write(
        root,
        "everything.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n  name: sw\n"
        "spec:\n  interfaces:\n    - name: port1\n      type: ethernet\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: note\n"
        "metadata:\n  name: about-it\n"
        "spec:\n  text: gone with it\n  anchor:\n    element: sw\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: layout\n"
        "metadata:\n  name: layout\n"
        "spec:\n  views:\n    l1:\n      nodes:\n        sw:\n"
        "          position: {x: 1, y: 2}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n  name: sw-other\n"
        "spec:\n  interfaces:\n    - name: port1\n      type: ethernet\n",
    )
    assert not load_tree(root).errors
    apply_and_commit(root, DeleteElement(address="sw", cascade=True))

    inventory = load_tree(root)
    assert not inventory.errors
    assert list(inventory.elements) == ["sw-other"], "and only the right one survived"
    assert not inventory.layouts and not list(inventory.annotations)
    assert not litter(root)


def test_a_hand_arranged_file_keeps_its_comments_through_a_delete(arranged: Path) -> None:
    """A delete is not a re-seed: forty positions must not be rewritten to drop one."""
    text = (arranged / "layout.yaml").read_text(encoding="utf-8")
    write(arranged, "layout.yaml", text.replace("  views:", "  # arranged by hand\n  views:"))
    apply_and_commit(arranged, DeleteElement(address="sw-home", cascade=True))
    after = (arranged / "layout.yaml").read_text(encoding="utf-8")
    assert "# arranged by hand" in after
    assert "position: {x: 474, y: 271}" in after, "still inline, still not requoted"


def test_a_disconnect_takes_the_waypoints_it_routed_through(arranged: Path) -> None:
    """A cable is geometry too: dropping one must not leave its bends behind."""
    edges = documents(arranged / "layout.yaml")[0]["spec"]["views"]["l1"]["edges"]
    assert "cables/cbl-rtr-sw" in edges
    apply_and_commit(arranged, Disconnect(address="cbl-rtr-sw"))
    assert not litter(arranged)
    edges = documents(arranged / "layout.yaml")[0]["spec"]["views"]["l1"]["edges"]
    assert "cables/cbl-rtr-sw" not in edges


def test_a_disconnect_refuses_a_note_it_would_take_and_cascades_over_it(
    arranged: Path,
) -> None:
    """Why ``Disconnect`` grew a ``cascade``: a cable is not only a cable."""
    write(
        arranged,
        "notes.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: note\n"
        "metadata:\n  name: why-this-run\n"
        "spec:\n  text: the uplink\n  anchor:\n    link: cables/cbl-rtr-sw\n",
    )
    session = EditSession(root=arranged)
    with pytest.raises(CascadeRequired) as excinfo:
        session.apply(Disconnect(address="cbl-rtr-sw"))
    assert excinfo.value.dependents == ("note why-this-run",)

    apply_and_commit(arranged, Disconnect(address="cbl-rtr-sw", cascade=True))
    assert note(arranged, "why-this-run") is None
    assert not litter(arranged)


def test_geometry_is_never_a_reason_to_refuse(arranged: Path) -> None:
    """Coordinates are litter, not a dependency: no ``--cascade`` is asked for them."""
    session = EditSession(root=arranged)
    session.apply(DeleteElement(address="cbl-sw-nas"))  # placed, and nothing refers to it
    session.commit()
    assert not litter(arranged)


# --------------------------------------------------------------------------- #
# Annotations (§21)
# --------------------------------------------------------------------------- #


def test_an_anchored_and_placed_note_keeps_its_text_and_loses_its_anchor(campus: Path) -> None:
    """It says what it says; only the line pointing at nothing goes."""
    apply_and_commit(
        campus, DeleteElement(address="sites/north/distribution/sw-north-dist-01", cascade=True)
    )
    kept = note(campus, "mgmt-is-not-routed")
    assert kept is not None
    assert "anchor" not in kept["spec"], "the anchor named something that is gone"
    assert "lives in the *mgmt* VRF" in kept["spec"]["text"], "and the prose is untouched"
    assert kept["spec"]["geometry"]["x"] == 640, "as is the point it was dragged to"
    assert not litter(campus)


def test_a_note_that_is_only_anchored_cannot_survive_and_says_so(arranged: Path) -> None:
    """§21 refuses a note with neither anchor nor point, so it goes rather than break."""
    write(
        arranged,
        "notes.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: note\n"
        "metadata:\n"
        "  name: about-the-switch\n"
        "spec:\n"
        "  text: the only copy of this sentence\n"
        "  anchor:\n"
        "    element: switches/sw-home\n",
    )
    session = EditSession(root=arranged)
    with pytest.raises(CascadeRequired) as excinfo:
        session.apply(DeleteElement(address="sw-home"))
    assert "note about-the-switch" in excinfo.value.dependents
    assert not session.changes, "a refusal leaves the tree exactly as it was"

    apply_and_commit(arranged, DeleteElement(address="sw-home", cascade=True))
    assert note(arranged, "about-the-switch") is None
    assert not litter(arranged)


def test_a_note_anchored_to_a_link_goes_with_the_device_that_link_ends_on(
    arranged: Path,
) -> None:
    """Two hops: the device takes the cable, and the cable takes the note."""
    write(
        arranged,
        "notes.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: note\n"
        "metadata:\n"
        "  name: why-this-run\n"
        "spec:\n"
        "  text: the uplink\n"
        "  anchor:\n"
        "    link: cables/cbl-rtr-sw\n",
    )
    plan = plan_cascade(load_tree(arranged), ["switches/sw-home"])
    assert [doomed.fqn for doomed in plan.annotations] == ["why-this-run"]
    assert "cables/cbl-rtr-sw" in plan.elements

    apply_and_commit(arranged, DeleteElement(address="sw-home", cascade=True))
    assert note(arranged, "why-this-run") is None
    assert not litter(arranged)


def test_an_area_drops_a_doomed_member_and_keeps_the_rest(arranged: Path) -> None:
    write(
        arranged,
        "areas.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: area\n"
        "metadata:\n"
        "  name: the-rack\n"
        "spec:\n"
        "  label: comms cupboard\n"
        "  members: [switches/sw-home, routers/rtr-home]\n",
    )
    apply_and_commit(arranged, DeleteElement(address="sw-home", cascade=True))
    kept = note(arranged, "the-rack")
    assert kept is not None
    assert kept["spec"]["members"] == ["routers/rtr-home"]
    assert kept["spec"]["label"] == "comms cupboard"
    assert not litter(arranged)


def test_an_area_left_enclosing_nothing_goes(arranged: Path) -> None:
    """An empty member list with no selector and no rectangle is refused by §21."""
    write(
        arranged,
        "areas.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: area\n"
        "metadata:\n"
        "  name: just-the-switch\n"
        "spec:\n"
        "  members: [switches/sw-home]\n",
    )
    apply_and_commit(arranged, DeleteElement(address="sw-home", cascade=True))
    assert note(arranged, "just-the-switch") is None
    assert not litter(arranged)


def test_an_area_with_a_rectangle_of_its_own_survives_losing_every_member(
    arranged: Path,
) -> None:
    """A box drawn on the canvas encloses whatever is inside it, including nothing."""
    write(
        arranged,
        "areas.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: area\n"
        "metadata:\n"
        "  name: zone\n"
        "spec:\n"
        "  members: [switches/sw-home]\n"
        "  geometry: {x: 10, y: 20, width: 300, height: 200}\n",
    )
    apply_and_commit(arranged, DeleteElement(address="sw-home", cascade=True))
    kept = note(arranged, "zone")
    assert kept is not None
    assert "members" not in kept["spec"], "the empty list goes with the last entry"
    assert kept["spec"]["geometry"]["width"] == 300
    assert not litter(arranged)


def test_a_legend_is_never_touched(campus: Path) -> None:
    """It names colours and words, never an element, so no delete can reach it."""
    before = (campus / "annotations.yaml").read_text(encoding="utf-8").split("kind: legend")[1]
    apply_and_commit(
        campus, DeleteElement(address="sites/north/distribution/sw-north-dist-01", cascade=True)
    )
    after = (campus / "annotations.yaml").read_text(encoding="utf-8").split("kind: legend")[1]
    assert after == before


# --------------------------------------------------------------------------- #
# Optional references (§17, §19)
# --------------------------------------------------------------------------- #


def test_deleting_one_of_two_feeds_drops_the_redundancy_it_claimed(tmp_path: Path) -> None:
    """``redundant: true`` on one input is a false statement, not a stale one.

    ``NG-E015`` is a *load* error, so leaving the flag behind does not merely
    warn — it takes the device out of the inventory, and every cable that ends
    on it starts reporting a dangling endpoint. That was the whole failure.
    """
    root = tmp_path / "power"
    write(
        root,
        "net.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: pdu\n"
        "metadata:\n  name: pdu-a\n"
        "spec:\n  outlets: 4\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: pdu\n"
        "metadata:\n  name: pdu-b\n"
        "spec:\n  outlets: 4\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata:\n  name: srv\n"
        "spec:\n"
        "  interfaces:\n    - name: eno1\n      type: ethernet\n"
        "  power:\n"
        "    powered_by: outlet\n"
        "    redundant: true\n"
        "    inputs:\n"
        "      - {pdu: pdu-a, outlet: '1'}\n"
        "      - {pdu: pdu-b, outlet: '1'}\n",
    )
    assert not load_tree(root).errors
    apply_and_commit(root, DeleteElement(address="pdu-a", cascade=True))
    inventory = load_tree(root)
    assert not inventory.errors, "the server still loads, so its cables still resolve"
    power = inventory.elements["srv"].spec.power
    assert power is not None
    assert not power.redundant, "one feed does not survive losing a feed"
    assert [entry.pdu for entry in power.inputs] == ["pdu-b"]


@pytest.mark.parametrize(
    ("declared", "survives"),
    [("    powered_by: outlet\n", True), ("", False)],
    ids=["says-something-else-too", "said-only-that"],
)
def test_the_last_feed_takes_the_power_block_only_if_that_was_all_it_said(
    tmp_path: Path, declared: str, survives: bool
) -> None:
    """``powered_by: outlet`` is a fact about the hardware, not about the PDU."""
    root = tmp_path / "power"
    write(
        root,
        "net.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: pdu\n"
        "metadata:\n  name: pdu-a\n"
        "spec:\n  outlets: 4\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata:\n  name: srv\n"
        "spec:\n"
        "  interfaces:\n    - name: eno1\n      type: ethernet\n"
        "  power:\n" + declared + "    inputs:\n"
        "      - {pdu: pdu-a, outlet: '1'}\n",
    )
    apply_and_commit(root, DeleteElement(address="pdu-a", cascade=True))
    inventory = load_tree(root)
    assert not inventory.errors
    assert "power:" in (root / "net.yaml").read_text(encoding="utf-8") if survives else True
    power = inventory.elements["srv"].spec.power
    assert (power is not None) is survives
    assert not power.inputs if power is not None else True
    assert "pdu-a" not in (root / "net.yaml").read_text(encoding="utf-8")


def test_a_group_loses_the_member_and_not_the_group(tmp_path: Path) -> None:
    """A group that loses a member is still a group; a cable that loses an end is not."""
    root = tmp_path / "people"
    write(
        root,
        "net.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: user\n"
        "metadata:\n  name: ada\n"
        "spec: {}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: user\n"
        "metadata:\n  name: grace\n"
        "spec: {}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: group\n"
        "metadata:\n  name: admins\n"
        "spec:\n  members: [ada, grace]\n",
    )
    plan = plan_cascade(load_tree(root), ["ada"])
    assert [(entry.holder, entry.what) for entry in plan.cleared] == [("admins", "a member")]
    apply_and_commit(root, DeleteElement(address="ada", cascade=True))
    inventory = load_tree(root)
    assert not inventory.errors
    assert list(inventory.groups["admins"].spec.members) == ["grace"]


# --------------------------------------------------------------------------- #
# One change, one undo
# --------------------------------------------------------------------------- #


def test_all_three_layers_are_one_change_and_one_undo_restores_them(arranged: Path) -> None:
    write(
        arranged,
        "notes.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: note\n"
        "metadata:\n"
        "  name: about-the-switch\n"
        "spec:\n"
        "  text: the only copy\n"
        "  anchor:\n"
        "    element: switches/sw-home\n",
    )
    before = {
        path.relative_to(arranged).as_posix(): path.read_bytes()
        for path in sorted(arranged.rglob("*.yaml"))
    }
    session = EditSession(root=arranged)
    applied = session.apply(DeleteElement(address="sw-home", cascade=True))
    session.commit()
    assert {"layout.yaml", "notes.yaml"} <= set(applied.files), "one operation touched all three"

    session = EditSession(root=arranged)
    for operation in applied.inverse:
        session.apply(operation)
    session.commit()
    after = {
        path.relative_to(arranged).as_posix(): path.read_bytes()
        for path in sorted(arranged.rglob("*.yaml"))
    }
    assert after == before, "byte for byte, comments and inline flow style included"


# --------------------------------------------------------------------------- #
# The plan, as the editor reads it
# --------------------------------------------------------------------------- #


def test_the_plan_says_why_each_thing_goes(arranged: Path) -> None:
    plan = plan_cascade(load_tree(arranged), ["switches/sw-home"])
    reasons = {entry.address: entry.reason for entry in plan.collateral}
    assert reasons["cables/cbl-rtr-sw"] == "one end of it is switches/sw-home:port1"
    assert all(entry.kind == "cable" for entry in plan.collateral)
    assert plan.takes_more
    assert describe(plan) == ", and 5 elements and 8 geometry entries"


def test_a_plan_that_takes_nothing_else_says_nothing(arranged: Path) -> None:
    """What the editor uses to decide not to ask: a delete of exactly what you named."""
    plan = plan_cascade(load_tree(arranged), ["cables/cbl-sw-nas"])
    assert not plan.takes_more, "so no confirmation is put up"
    assert describe(plan) == ", and 1 geometry entry", "but it is still reported afterwards"
    assert plan.geometry, "placed, and cleaned up without being asked about"


def test_a_tunnel_over_a_tunnel_goes_three_levels_up(tmp_path: Path) -> None:
    """The closure is transitive, which one pass over the references would miss.

    The VXLAN terminates on ``c`` and ``d``, so deleting ``a`` can only reach it
    the long way round: ``a`` takes the IPsec tunnel that ends on it, and the
    IPsec tunnel takes the VXLAN that runs *over* it.
    """
    root = tmp_path / "stacked"
    write(
        root,
        "net.yaml",
        "\n".join(
            [
                *[_switch(name, ["eth0", "tun0"]) for name in ("a", "b", "c", "d")],
                _link("cable", "wire", "a:eth0", "b:eth0", "  medium: copper\n"),
                _link("tunnel", "ipsec", "a:tun0", "b:tun0", "  type: ipsec\n"),
                _link(
                    "tunnel",
                    "vxlan",
                    "c:tun0",
                    "d:tun0",
                    "  type: vxlan\n  vni: 100\n  over: ipsec\n",
                ),
                "",
            ]
        ),
    )
    assert not load_tree(root).errors
    plan = plan_cascade(load_tree(root), ["a"])
    assert set(plan.elements) == {"a", "wire", "ipsec", "vxlan"}
    reasons = {entry.address: entry.reason for entry in plan.collateral}
    assert reasons["ipsec"] == "one end of it is a:tun0"
    assert reasons["vxlan"] == "it runs over ipsec", "reached through the tunnel, not the device"


def _switch(name: str, interfaces: list[str]) -> str:
    ports = "".join(f"    - name: {one}\n      type: ethernet\n" for one in interfaces)
    return (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        f"metadata:\n  name: {name}\n"
        f"spec:\n  interfaces:\n{ports}---"
    )


def _link(kind: str, name: str, a: str, b: str, extra: str) -> str:
    return (
        "apiVersion: netgraph.dev/v1alpha1\n"
        f"kind: {kind}\n"
        f"metadata:\n  name: {name}\n"
        f"spec:\n{extra}  endpoints: [{a}, {b}]\n---"
    )


# --------------------------------------------------------------------------- #
# The invariant
# --------------------------------------------------------------------------- #


def test_the_tunnel_node_prefix_is_still_the_renderers() -> None:
    """cascade.py spells it out rather than importing the renderer; hold them equal."""
    from netgraph.render.graph import TUNNEL_ID_PREFIX as drawn

    assert drawn == cascade_module.TUNNEL_ID_PREFIX


@pytest.mark.parametrize(
    "source",
    [
        FIXTURES / "arranged",
        FIXTURES / "routed",
        FIXTURES / "obstructed",
        FIXTURES / "drawio" / "inventory",
        EXAMPLES / "campus",
        EXAMPLES / "home-lab",
        EXAMPLES / "patch-room",
    ],
    ids=lambda path: path.name,
)
def test_deleting_anything_from_a_clean_tree_leaves_a_clean_tree(
    source: Path, tmp_path: Path
) -> None:
    """The whole claim, over every element of every arranged and annotated fixture.

    Deleting one thing must never add a finding of its own making. The rules
    checked are the three a half-done delete produces — the geometry that placed
    what is gone, the note that is about it, and the area left enclosing nothing
    — measured against what the tree already had, so a fixture carrying a
    pre-existing warning is held to "no worse" rather than to "clean".

    A delete the validation gate *refuses* counts as passing, and is checked to
    have written nothing at all. That is not a loophole: cutting the only cable
    to a camera that declares ``powered_by: poe`` leaves a device claiming power
    over a run that no longer exists, and there is no repair netgraph could make
    without inventing a fact — so it says so and stops, which is the behaviour
    ``--force`` exists to override. What it must never do is write half of it.
    """
    original = tmp_path / "original"
    shutil.copytree(source, original)
    before = set(litter(original))
    refused = 0
    for fqn in sorted(load_tree(original).elements):
        root = tmp_path / "attempt"
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(source, root)
        untouched = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*.yaml"))
        }
        try:
            apply_and_commit(root, DeleteElement(address=fqn, cascade=True))
        except ValidationRefused:
            refused += 1
            now = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*.yaml"))
            }
            assert now == untouched, f"the refused delete of {fqn} wrote something"
            continue
        assert not (set(litter(root)) - before), f"deleting {fqn} left something behind"
    assert refused < len(load_tree(original).elements), "something has to have gone through"
