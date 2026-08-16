"""A rename carries everything that names the element — geometry included.

The delete half of this was fixed first (``tests/test_cascade.py``); this is the
same defect one operation over, and the same invariant states it: **a rename
never leaves a finding behind, and the renamed element is drawn exactly where it
was.** ``netgraph edit rename A B`` used to rewrite the *references* to ``A`` and
nothing else, so the layout keys that placed it (``W138``), the note anchored to
it and the area enclosing it (``W142``) were left naming a name that no longer
existed — and ``netgraph layout --prune`` then dropped the coordinates rather
than moving them, which is an arrangement lost silently.

The second half of what follows is about *spelling*, and it is the part a delete
never needed: a rename has to write a name down, and it must write it the way the
document it is editing already writes names. ``sw-a`` inside ``sites/hq/``
becomes ``sw-b``, not ``sites/hq/sw-b`` — until the short spelling stops
resolving, which is when keeping it would be a bug rather than a courtesy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.edit import EditSession, MoveElement, RenameElement
from netgraph.edit.references import NameIndex
from netgraph.edit.rename import plan_rename, respelled_key
from netgraph.layout.resolve import resolve_geometry
from netgraph.loader import load_tree
from netgraph.validate import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The rules a half-done rename produces — the same three a half-done delete did.
LITTER = ("W138", "W142", "W143")

#: The views the fixtures arrange, which is where a lost arrangement shows up.
VIEWS = ("l1", "l2", "l3")


@pytest.fixture()
def arranged(tmp_path: Path) -> Path:
    """A writable copy of ``tests/fixtures/arranged`` — home-lab, with geometry."""
    root = tmp_path / "arranged"
    shutil.copytree(FIXTURES / "arranged", root)
    return root


@pytest.fixture()
def campus(tmp_path: Path) -> Path:
    """A writable copy of ``examples/campus`` — namespaces, notes, an area, no layout."""
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


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.yaml"))
    }


def placements(root: Path) -> dict[tuple[str, str, str], object]:
    """Where everything the arrangement places is drawn, as one flat table.

    Resolved rather than read off the files, so it is the arrangement a renderer
    would use: a key the rename left spelled the old way places nothing and
    simply disappears from this table, which is what makes it an assertion about
    the diagram rather than about the YAML.
    """
    inventory = load_tree(root)
    table: dict[tuple[str, str, str], object] = {}
    for view in VIEWS:
        geometry = resolve_geometry(inventory, view)
        for section, entries in (("nodes", geometry.nodes), ("edges", geometry.edges)):
            for key, placement in entries.items():
                table[(view, section, key)] = placement
    return table


def renamed_key(key: str, old: str, new: str) -> str:
    """``key`` as it should read once ``old`` is called ``new``.

    Whole-address comparison rather than a substring replacement, because
    ``sw-b`` is a substring of ``sw-blocker`` and a test that cannot tell them
    apart is not testing anything.
    """
    if key == old:
        return new
    if key.startswith(f"{old}#"):
        return f"{new}{key[len(old) :]}"
    if key == f"tunnel:{old}":
        return f"tunnel:{new}"
    return key


def documents(path: Path) -> list[dict]:
    return [one for one in yaml.safe_load_all(path.read_text(encoding="utf-8")) if one]


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def hub(name: str) -> str:
    return (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: hub\n"
        f"metadata:\n  name: {name}\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
    )


# --------------------------------------------------------------------------- #
# Geometry (§18)
# --------------------------------------------------------------------------- #


def test_a_rename_carries_the_coordinates_that_placed_it(arranged: Path) -> None:
    """The bug this module was written for: an arrangement lost by a rename.

    ``hosts/adp-usb-eth`` is placed three times — a node on l1, a node on l2 and
    the derived ``#upstream`` edge that hangs it off its host — which is the
    ordinary case rather than a contrived one: an element arranged on two
    diagrams has geometry in two layer-specific blocks of the same document.
    """
    assert not litter(arranged), "the fixture starts clean"
    plan = plan_rename(load_tree(arranged), old="hosts/adp-usb-eth", new="hosts/adp-usb-nic")
    assert [(entry.view, entry.section, entry.new_key) for entry in plan.geometry] == [
        ("l1", "nodes", "hosts/adp-usb-nic"),
        ("l1", "edges", "hosts/adp-usb-nic#upstream"),
        ("l2", "nodes", "hosts/adp-usb-nic"),
    ]

    apply_and_commit(arranged, RenameElement(address="hosts/adp-usb-eth", new_name="adp-usb-nic"))
    assert not litter(arranged)

    views = documents(arranged / "layout.yaml")[0]["spec"]["views"]
    assert "hosts/adp-usb-nic" in views["l1"]["nodes"]
    assert "hosts/adp-usb-nic#upstream" in views["l1"]["edges"]
    assert "hosts/adp-usb-nic" in views["l2"]["nodes"]
    assert not [key for key in views["l1"]["nodes"] if "adp-usb-eth" in key]
    assert "hosts/laptop" in views["l1"]["nodes"], "and nothing else moved"


def test_the_renamed_element_is_drawn_where_it_was(arranged: Path) -> None:
    """The regression in the terms a person would state it in.

    Not "the key was rewritten" but "the box is in the same place" — which is
    the whole point of persisting geometry, and what a rename that dropped the
    key silently undid.
    """
    before = placements(arranged)
    apply_and_commit(arranged, RenameElement(address="hosts/adp-usb-eth", new_name="adp-usb-nic"))

    expected = {
        (view, section, renamed_key(key, "hosts/adp-usb-eth", "hosts/adp-usb-nic")): placement
        for (view, section, key), placement in before.items()
    }
    assert placements(arranged) == expected


def test_a_rename_touches_only_the_key_lines_of_the_layout(arranged: Path) -> None:
    """A hand-arranged file is read by people; a rename must not reflow it."""
    before = (arranged / "layout.yaml").read_text(encoding="utf-8").splitlines()
    apply_and_commit(arranged, RenameElement(address="hosts/adp-usb-eth", new_name="adp-usb-nic"))
    after = (arranged / "layout.yaml").read_text(encoding="utf-8").splitlines()

    assert len(before) == len(after)
    differing = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
    assert [old.strip() for old, _ in differing] == [
        "hosts/adp-usb-eth:",
        "hosts/adp-usb-eth#upstream:",
        "hosts/adp-usb-eth:",
    ], "the key stays on the line it was on, so the diff is three lines"
    assert all("adp-usb-nic" in new for _, new in differing)


def test_a_rename_with_no_geometry_writes_no_layout_at_all(campus: Path) -> None:
    """The ordinary inventory has no layout document, and must not acquire one.

    An empty plan has to be an empty plan: no ``spec.views: {}`` written out, no
    layout file created, and every file the rename does not need untouched.
    """
    assert not list(campus.rglob("*layout*")), "campus arranges nothing"
    before = snapshot(campus)

    plan = plan_rename(
        load_tree(campus),
        old="sites/west/access/sw-west-acc-01",
        new="sites/west/access/sw-west-acc-1a",
    )
    assert plan.geometry == ()

    apply_and_commit(
        campus, RenameElement(address="sites/west/access/sw-west-acc-01", new_name="sw-west-acc-1a")
    )
    after = snapshot(campus)
    assert set(after) == set(before), "no file was created or removed"
    unchanged = {name for name in before if before[name] == after[name]}
    assert "annotations.yaml" in unchanged, "no annotation names it, so none was rewritten"


# --------------------------------------------------------------------------- #
# Annotations (§21)
# --------------------------------------------------------------------------- #


def test_a_rename_repoints_a_note_anchor_and_an_area_member(tmp_path: Path) -> None:
    """One element, named by both kinds of annotation at once.

    A note pointing at the switch and the area drawn round the rack it is in are
    two documents and two shapes of reference — a mapping value under
    ``spec.anchor`` and a position in ``spec.members`` — and both have to move.
    """
    write(tmp_path, "racks/sw.yaml", hub("sw-a"))
    write(tmp_path, "racks/spare.yaml", hub("sw-spare"))
    write(
        tmp_path,
        "annotations.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: note\n"
        "metadata:\n  name: about-the-switch\n"
        "spec:\n  text: the one with the loud fan\n  anchor:\n    element: racks/sw-a\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: area\n"
        "metadata:\n  name: rack-3\n"
        "spec:\n  label: Rack 3\n  members:\n    - racks/sw-a\n    - racks/sw-spare\n",
    )
    assert not litter(tmp_path)

    plan = plan_rename(load_tree(tmp_path), old="racks/sw-a", new="racks/sw-b")
    assert [(entry.kind, entry.fqn, entry.path) for entry in plan.annotations] == [
        ("note", "about-the-switch", ("spec", "anchor", "element")),
        ("area", "rack-3", ("spec", "members", 0)),
    ]

    apply_and_commit(tmp_path, RenameElement(address="racks/sw-a", new_name="sw-b"))
    assert not litter(tmp_path)
    note, area = documents(tmp_path / "annotations.yaml")
    assert note["spec"]["anchor"]["element"] == "racks/sw-b"
    assert area["spec"]["members"] == ["racks/sw-b", "racks/sw-spare"]


def test_an_area_selector_is_left_alone(campus: Path) -> None:
    """A selector names a pattern, not an element; rewriting one would be guessing."""
    before = (campus / "annotations.yaml").read_text(encoding="utf-8")
    apply_and_commit(
        campus,
        RenameElement(address="sites/north/distribution/sw-north-dist-01", new_name="sw-north-1a"),
    )
    after = (campus / "annotations.yaml").read_text(encoding="utf-8")
    assert "namespace: sites/north" in after, "the selector is untouched"
    assert before.count("selector") == after.count("selector")
    assert "element: sites/north/distribution/sw-north-1a" in after, "the anchor is not"


# --------------------------------------------------------------------------- #
# Spelling
# --------------------------------------------------------------------------- #


def arrange(root: Path, relative: str, *keys: str) -> None:
    """A layout document at ``relative`` placing ``keys``, ten pixels apart."""
    entries = "".join(
        f"        {key}:\n          position: {{x: {10 * position}, y: 0}}\n"
        for position, key in enumerate(keys)
    )
    write(
        root,
        relative,
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: layout\n"
        "metadata:\n  name: layout\n"
        f"spec:\n  views:\n    l1:\n      nodes:\n{entries}",
    )


def test_a_short_key_stays_short_and_a_qualified_one_stays_qualified(tmp_path: Path) -> None:
    """The spelling rule, in the case that makes it worth having.

    Two documents place the same switch: the one that lives beside it writes
    ``sw-a``, the one at the root writes ``sites/hq/sw-a``. Both are correct and
    a rename must leave both correct *and* recognisable — promoting the first to
    ``sites/hq/sw-b`` would be right and unreadable.
    """
    write(tmp_path, "sites/hq/sw.yaml", hub("sw-a"))
    arrange(tmp_path, "sites/hq/layout.yaml", "sw-a")
    arrange(tmp_path, "layout.yaml", "sites/hq/sw-a")
    assert not litter(tmp_path)

    apply_and_commit(tmp_path, RenameElement(address="sites/hq/sw-a", new_name="sw-b"))
    assert not litter(tmp_path)
    assert list(
        documents(tmp_path / "sites/hq/layout.yaml")[0]["spec"]["views"]["l1"]["nodes"]
    ) == ["sw-b"]
    assert list(documents(tmp_path / "layout.yaml")[0]["spec"]["views"]["l1"]["nodes"]) == [
        "sites/hq/sw-b"
    ]


def test_a_short_key_is_promoted_when_the_new_name_stops_resolving(tmp_path: Path) -> None:
    """A short key resolves outwards, and a rename can take that away.

    The root layout writes ``sw-a`` short, which resolves because exactly one
    element is called that anywhere. Rename it to ``sw-core`` and the short
    spelling becomes ambiguous — there is a ``sites/dc/sw-core`` — so keeping it
    would place the wrong device. It is promoted instead.
    """
    write(tmp_path, "sites/hq/sw.yaml", hub("sw-a"))
    write(tmp_path, "sites/dc/sw.yaml", hub("sw-core"))
    arrange(tmp_path, "layout.yaml", "sw-a", "sites/dc/sw-core")
    assert not litter(tmp_path)

    apply_and_commit(tmp_path, RenameElement(address="sites/hq/sw-a", new_name="sw-core"))
    assert not litter(tmp_path)
    nodes = documents(tmp_path / "layout.yaml")[0]["spec"]["views"]["l1"]["nodes"]
    assert nodes["sites/hq/sw-core"] == {"position": {"x": 0, "y": 0}}
    assert nodes["sites/dc/sw-core"] == {"position": {"x": 10, "y": 0}}, (
        "the other one is where it was"
    )


def test_a_move_across_namespaces_takes_the_geometry_with_it(tmp_path: Path) -> None:
    """``edit move`` renames too — the namespace half — and it is the same code.

    Here the short spelling has to be promoted for the other reason: the key sat
    in the element's own folder and resolved for that reason alone. Once the
    element is somewhere else, ``sw-a`` written in ``sites/hq/`` is a name two
    elements answer to, so it resolves to neither.
    """
    write(tmp_path, "sites/hq/sw.yaml", hub("sw-a"))
    write(tmp_path, "sites/west/sw.yaml", hub("sw-a"))
    arrange(tmp_path, "sites/hq/layout.yaml", "sw-a")
    assert not litter(tmp_path)

    apply_and_commit(tmp_path, MoveElement(address="sites/hq/sw-a", file="sites/dc/sw.yaml"))
    assert not litter(tmp_path)
    assert list(
        documents(tmp_path / "sites/hq/layout.yaml")[0]["spec"]["views"]["l1"]["nodes"]
    ) == ["sites/dc/sw-a"]


def test_a_tunnel_key_keeps_its_prefix(tmp_path: Path) -> None:
    """``tunnel:`` and ``#upstream`` decorate an address; neither is part of it."""
    index = NameIndex(["sites/hq/vx-100", "sites/hq/adp"])
    after = index.replaced("sites/hq/vx-100", "sites/hq/vx-200")
    assert (
        respelled_key("tunnel:sites/hq/vx-100", new="sites/hq/vx-200", namespace="", index=after)
        == "tunnel:sites/hq/vx-200"
    )
    assert (
        respelled_key("tunnel:vx-100", new="sites/hq/vx-200", namespace="sites/hq", index=after)
        == "tunnel:vx-200"
    )


def test_a_key_a_stale_entry_already_claims_is_taken_over(tmp_path: Path) -> None:
    """Renaming onto a key the layout already holds must not duplicate it.

    A layout may carry a key for an element that no longer exists — that is what
    ``W138`` is for. Renaming a live element onto that spelling has to leave one
    entry, and it has to be the live one's coordinates.
    """
    write(tmp_path, "racks/sw.yaml", hub("sw-a"))
    arrange(tmp_path, "layout.yaml", "racks/sw-a", "racks/sw-b")
    assert litter(tmp_path), "racks/sw-b is stale to begin with"

    apply_and_commit(tmp_path, RenameElement(address="racks/sw-a", new_name="sw-b"))
    nodes = documents(tmp_path / "layout.yaml")[0]["spec"]["views"]["l1"]["nodes"]
    assert nodes == {"racks/sw-b": {"position": {"x": 0, "y": 0}}}
    assert not litter(tmp_path)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_command_leaves_a_tree_that_passes_strict_validation(arranged: Path) -> None:
    """End to end, in the two commands the follow-up entry names.

    ``--strict`` is what makes this a regression test rather than a smell test:
    every warning is an error, so the ``W138`` a rename used to leave behind
    fails the run.
    """
    runner = CliRunner()
    assert runner.invoke(cli, ["-i", str(arranged), "validate", "--strict"]).exit_code == 0

    renamed = runner.invoke(
        cli, ["-i", str(arranged), "edit", "rename", "hosts/adp-usb-eth", "adp-usb-nic"]
    )
    assert renamed.exit_code == 0, renamed.output
    assert "layout.yaml" in renamed.output, "the layout is reported as changed"

    checked = runner.invoke(cli, ["-i", str(arranged), "validate", "--strict"])
    assert checked.exit_code == 0, checked.output


# --------------------------------------------------------------------------- #
# The invariant
# --------------------------------------------------------------------------- #


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
def test_renaming_anything_in_a_clean_tree_leaves_a_clean_tree(
    source: Path, tmp_path: Path
) -> None:
    """The whole claim, over every element of every arranged and annotated fixture.

    Two assertions per element, and the second is the one the first would not
    catch: no new finding, *and* every node still drawn exactly where it was,
    under whatever name it now has. A rename that quietly dropped a key passes
    "no new finding" only until ``layout --prune`` runs; it never passes this.
    """
    original = tmp_path / "original"
    shutil.copytree(source, original)
    before_litter = set(litter(original))
    before_places = placements(original)

    for fqn in sorted(load_tree(original).elements):
        root = tmp_path / "attempt"
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(source, root)
        short = fqn.rsplit("/", 1)[-1]
        apply_and_commit(root, RenameElement(address=fqn, new_name=f"{short}-renamed"))

        assert not (set(litter(root)) - before_litter), f"renaming {fqn} left something behind"
        moved = {
            (view, section, renamed_key(key, fqn, f"{fqn}-renamed")): placement
            for (view, section, key), placement in before_places.items()
        }
        assert placements(root) == moved, f"renaming {fqn} moved something on the diagram"
