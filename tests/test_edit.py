"""The write path: ``netgraph.edit`` and ``netgraph edit``.

The promises this module holds to account are the four in ``docs/editing.md``:

* **lossless** — an edit changes the hunk it meant to change and nothing else,
  which is asserted by diffing every touched file rather than by eyeballing one;
* **reversible** — an operation followed by its inverse restores the tree byte
  for byte, on every operation, not only on the ones with a tidy opposite;
* **reference-aware** — a rename rewrites every mention across the tree in the
  spelling its author chose, and a delete refuses or cascades;
* **safe** — a file that moved on disk is a conflict, and an edit that would add
  an error is refused.

``tests/test_edit_properties.py`` states the first two universally; this module
pins the behaviours and the refusals.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.edit import (
    AddInterface,
    AddressError,
    CascadeRequired,
    ConflictError,
    Connect,
    CreateElement,
    DeleteElement,
    Disconnect,
    EditError,
    EditSession,
    FileFacts,
    MoveElement,
    NameIndex,
    OperationError,
    PlacementError,
    Problem,
    RemoveFile,
    RemoveInterface,
    RenameElement,
    RoundTripError,
    SetField,
    UnsetField,
    ValidationRefused,
    WriteFile,
    YamlDocument,
    YamlFile,
    choose_file,
    format_field_path,
    operation_from_dict,
    operations_from_json,
    operations_to_json,
    parse_field_path,
)
from netgraph.edit.paths import MISSING, get_field, set_field, unset_field
from netgraph.edit.placement import check_file, normalise_file
from netgraph.edit.references import (
    Reference,
    ReferenceRole,
    drop_reference,
    references_of,
    rewrite_reference,
)
from netgraph.loader import load_tree

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A writable copy of ``examples/home-lab``."""
    root = tmp_path / "home-lab"
    shutil.copytree(EXAMPLES / "home-lab", root)
    return root


@pytest.fixture()
def campus(tmp_path: Path) -> Path:
    """A writable copy of ``examples/campus`` — namespaces, templates, ranges."""
    root = tmp_path / "campus"
    shutil.copytree(EXAMPLES / "campus", root)
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    """Every YAML file below ``root``, as bytes, keyed by relative path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.yaml"))
    }


def changed_lines(before: str, after: str) -> tuple[list[str], list[str]]:
    """The lines that differ, as (removed, added), ignoring order."""
    old, new = before.splitlines(), after.splitlines()
    removed = [line for line in old if line not in new]
    added = [line for line in new if line not in old]
    return removed, added


def apply_and_commit(root: Path, *operations: object) -> EditSession:
    session = EditSession(root=root)
    for operation in operations:
        session.apply(operation)  # type: ignore[arg-type]
    session.commit()
    return session


# --------------------------------------------------------------------------- #
# Field paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("spec", ("spec",)),
        ("spec.model", ("spec", "model")),
        ("spec.interfaces[0].mtu", ("spec", "interfaces", 0, "mtu")),
        ("metadata.labels.site", ("metadata", "labels", "site")),
        ("spec.a[1][2]", ("spec", "a", 1, 2)),
    ],
)
def test_a_field_path_parses_and_formats_back(text: str, expected: tuple[object, ...]) -> None:
    assert parse_field_path(text) == expected
    assert format_field_path(expected) == text


@pytest.mark.parametrize("text", ["", "   ", "spec.", ".spec", "spec..model", "spec[a]", "a[1]b"])
def test_a_malformed_field_path_is_refused(text: str) -> None:
    with pytest.raises(OperationError):
        parse_field_path(text)


# --------------------------------------------------------------------------- #
# Round-tripping files
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    sorted((EXAMPLES).rglob("*.yaml")),
    ids=lambda path: path.relative_to(EXAMPLES).as_posix(),
)
def test_every_example_file_round_trips_byte_for_byte(path: Path) -> None:
    """Reading and rendering a file back must be the identity, for every file."""
    text = path.read_text(encoding="utf-8")
    parsed = YamlFile.parse(text, relative=path.name)
    assert parsed.render() == text
    assert all(document.faithful for document in parsed.documents)


def test_a_leading_comment_and_a_trailing_one_both_survive() -> None:
    text = (
        "# what this file is for\n\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: hub\n"
        "metadata:\n"
        "  name: h1\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eth0\n"
        "      type: ethernet\n"
        "# and a word at the end\n"
    )
    parsed = YamlFile.parse(text, relative="h.yaml")
    assert parsed.preamble == "# what this file is for\n\n"
    assert parsed.render() == text


def test_crlf_and_a_byte_order_mark_are_restored() -> None:
    text = "﻿kind: hub\r\nmetadata:\r\n  name: h1\r\n"
    parsed = YamlFile.parse(text, relative="h.yaml")
    assert parsed.bom and parsed.newline == "\r\n"
    assert parsed.render() == text


def test_an_inline_document_marker_renders_but_refuses_to_be_edited() -> None:
    text = "---\nkind: hub\n---  {kind: hub}\n"
    parsed = YamlFile.parse(text, relative="h.yaml")
    assert parsed.render() == text
    with pytest.raises(RoundTripError, match="inline"):
        parsed.documents[1].touch()


def test_a_file_of_nothing_but_comments_has_no_documents() -> None:
    text = "# nothing here yet\n"
    parsed = YamlFile.parse(text, relative="empty.yaml")
    assert not parsed.documents and parsed.is_empty
    assert parsed.render() == text


def test_a_file_that_is_not_yaml_is_refused() -> None:
    with pytest.raises(RoundTripError):
        YamlFile.parse("kind: [unclosed\n", relative="broken.yaml")


# --------------------------------------------------------------------------- #
# set / unset
# --------------------------------------------------------------------------- #


def test_setting_a_field_changes_one_line(home: Path) -> None:
    before = (home / "switches/sw-home.yaml").read_text(encoding="utf-8")
    apply_and_commit(home, SetField(address="sw-home", path="spec.model", value="C9300"))
    after = (home / "switches/sw-home.yaml").read_text(encoding="utf-8")
    removed, added = changed_lines(before, after)
    assert removed == ["  model: TL-SG108E"]
    assert added == ["  model: C9300"]


def test_setting_a_field_leaves_every_other_file_untouched(home: Path) -> None:
    before = snapshot(home)
    apply_and_commit(home, SetField(address="sw-home", path="spec.model", value="C9300"))
    after = snapshot(home)
    assert set(before) == set(after)
    assert {path for path in before if before[path] != after[path]} == {"switches/sw-home.yaml"}


def test_a_new_field_lands_in_schema_order_in_a_canonical_file(home: Path) -> None:
    """A canonical file stays canonical: the key goes where ``fmt`` would put it."""
    apply_and_commit(home, SetField(address="sw-home", path="spec.serial", value="X1"))
    text = (home / "switches/sw-home.yaml").read_text(encoding="utf-8")
    assert "\n  serial: X1\n" in text
    result = CliRunner().invoke(cli, ["-i", str(home), "fmt", "--check"])
    assert result.exit_code == 0, result.output


def test_the_inverse_of_setting_an_absent_field_is_an_unset(home: Path) -> None:
    session = EditSession(root=home)
    applied = session.apply(SetField(address="sw-home", path="spec.serial", value="X1"))
    assert applied.inverse == (UnsetField(address="switches/sw-home", path="spec.serial"),)


def test_the_inverse_of_overwriting_a_field_restores_the_file(home: Path) -> None:
    session = EditSession(root=home)
    applied = session.apply(SetField(address="sw-home", path="spec.model", value="C9300"))
    (inverse,) = applied.inverse
    assert isinstance(inverse, WriteFile)
    assert "TL-SG108E" in inverse.text


def test_unsetting_removes_the_field(home: Path) -> None:
    apply_and_commit(home, UnsetField(address="sw-home", path="spec.location"))
    text = (home / "switches/sw-home.yaml").read_text(encoding="utf-8")
    assert "location:" not in text


def test_unsetting_something_that_is_not_there_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(OperationError, match="no such field"):
        session.apply(UnsetField(address="sw-home", path="spec.serial"))
    assert not session.changes


@pytest.mark.parametrize("path", ["kind", "metadata", "spec", "apiVersion"])
def test_the_envelope_cannot_be_unset(home: Path, path: str) -> None:
    session = EditSession(root=home)
    with pytest.raises(OperationError, match="envelope"):
        session.apply(UnsetField(address="sw-home", path=path))


@pytest.mark.parametrize("path", ["kind", "metadata.name"])
def test_identity_is_not_settable(home: Path, path: str) -> None:
    session = EditSession(root=home)
    with pytest.raises(OperationError, match="not settable"):
        session.apply(SetField(address="sw-home", path=path, value="x"))


def test_a_refused_operation_leaves_the_tree_untouched(home: Path) -> None:
    """Not even a document marked as about to change may be re-emitted."""
    before = snapshot(home)
    session = EditSession(root=home)
    with pytest.raises(OperationError):
        session.apply(UnsetField(address="sw-home", path="spec.nothing"))
    assert session.changes == {}
    assert snapshot(home) == before


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


def test_an_unknown_address_names_itself(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(AddressError, match="no element called 'nope'") as excinfo:
        session.apply(SetField(address="nope", path="spec.model", value="x"))
    assert excinfo.value.address == "nope"


def test_an_ambiguous_address_lists_every_candidate(tmp_path: Path) -> None:
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "sw.yaml").write_text(
            "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: sw\n"
            "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n",
            encoding="utf-8",
        )
    session = EditSession(root=tmp_path)
    with pytest.raises(AddressError, match="ambiguous") as excinfo:
        session.apply(SetField(address="sw", path="spec.vendor", value="x"))
    assert excinfo.value.candidates == ("a/sw", "b/sw")


def test_a_fully_qualified_address_disambiguates(campus: Path) -> None:
    apply_and_commit(
        campus,
        SetField(address="sites/north/access/sw-north-acc-01", path="spec.serial", value="S1"),
    )
    inventory = load_tree(campus)
    assert inventory.devices["sites/north/access/sw-north-acc-01"].spec.serial == "S1"


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


SERVER_SPEC = {"interfaces": [{"name": "eth0", "type": "ethernet"}]}


def test_creating_an_element_writes_a_file_named_after_it(home: Path) -> None:
    apply_and_commit(
        home,
        CreateElement(kind="server", name="srv-new", namespace="hosts", spec=SERVER_SPEC),
    )
    assert (home / "hosts/srv-new.yaml").is_file()
    assert "hosts/srv-new" in load_tree(home).devices


def test_a_created_document_is_canonical(home: Path) -> None:
    apply_and_commit(
        home,
        CreateElement(kind="server", name="srv-new", namespace="hosts", spec=SERVER_SPEC),
    )
    result = CliRunner().invoke(cli, ["-i", str(home), "fmt", "--check"])
    assert result.exit_code == 0, result.output


def test_creating_something_the_schema_refuses_is_refused_as_a_spec_problem(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="does not match the schema"):
        session.apply(
            CreateElement(kind="server", name="srv-new", spec={"interfaces": "not a list"})
        )
    assert not session.changes


def test_a_duplicate_name_in_one_namespace_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="already exists"):
        session.apply(
            CreateElement(kind="server", name="pc-desk", namespace="hosts", spec=SERVER_SPEC)
        )


def test_the_inverse_of_a_create_is_a_delete(home: Path) -> None:
    session = EditSession(root=home)
    applied = session.apply(
        CreateElement(kind="server", name="srv-new", namespace="hosts", spec=SERVER_SPEC)
    )
    assert applied.inverse == (DeleteElement(address="hosts/srv-new"),)


def test_creating_and_deleting_again_leaves_no_trace(home: Path) -> None:
    before = snapshot(home)
    session = EditSession(root=home)
    applied = session.apply(
        CreateElement(kind="server", name="srv-new", namespace="new-wing", spec=SERVER_SPEC)
    )
    session.apply_all(applied.inverse)
    assert session.changes == {}
    session.commit()
    assert snapshot(home) == before
    assert not (home / "new-wing").exists()


def test_metadata_travels_into_the_document(home: Path) -> None:
    apply_and_commit(
        home,
        CreateElement(
            kind="server",
            name="srv-new",
            namespace="hosts",
            spec=SERVER_SPEC,
            metadata={"description": "a new box", "labels": {"site": "home"}},
        ),
    )
    element = load_tree(home).devices["hosts/srv-new"]
    assert element.metadata.description == "a new box"
    assert element.metadata.labels == {"site": "home"}


def test_a_metadata_name_that_contradicts_the_operation_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(OperationError, match=r"metadata\.name"):
        session.apply(
            CreateElement(
                kind="server", name="srv-new", spec=SERVER_SPEC, metadata={"name": "other"}
            )
        )


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #


def facts(*entries: tuple[str, tuple[str, ...], tuple[str, ...]]) -> dict[str, FileFacts]:
    return {
        relative: FileFacts(relative=relative, kinds=kinds, names=names)
        for relative, kinds, names in entries
    }


def test_a_new_link_joins_the_file_that_already_holds_links() -> None:
    existing = facts(("cables/links.yaml", ("cable", "cable"), ("a", "b")))
    assert (
        choose_file(kind="cable", namespace="cables", name="c", files=existing)
        == "cables/links.yaml"
    )


def test_a_new_link_with_nowhere_to_go_gets_the_conventional_file() -> None:
    assert choose_file(kind="cable", namespace="site", name="c", files={}) == "site/cables.yaml"
    assert choose_file(kind="tunnel", namespace="", name="t", files={}) == "tunnels.yaml"


def test_a_new_device_does_not_join_a_file_named_after_another_device() -> None:
    existing = facts(("hosts/pc-desk.yaml", ("computer",), ("pc-desk",)))
    assert (
        choose_file(kind="computer", namespace="hosts", name="pc-two", files=existing)
        == "hosts/pc-two.yaml"
    )


def test_a_new_device_joins_a_collection_of_devices() -> None:
    existing = facts(("hosts/hosts.yaml", ("computer", "computer"), ("a", "b")))
    assert (
        choose_file(kind="computer", namespace="hosts", name="c", files=existing)
        == "hosts/hosts.yaml"
    )


def test_the_busiest_file_wins_and_ties_break_by_path() -> None:
    existing = facts(
        ("z/cables.yaml", ("cable",), ("a",)),
        ("a/cables.yaml", ("cable",), ("b",)),
    )
    assert choose_file(kind="cable", namespace="a", name="c", files=existing) == "a/cables.yaml"


@pytest.mark.parametrize(
    "requested",
    ["/abs/x.yaml", "../x.yaml", "notes.txt", "_drafts/x.yaml", ".hidden/x.yaml", ""],
)
def test_a_file_the_loader_would_not_read_is_refused(requested: str) -> None:
    with pytest.raises(PlacementError):
        check_file(requested)


def test_a_file_whose_folder_is_not_the_namespace_is_refused() -> None:
    with pytest.raises(PlacementError, match="folder is its namespace"):
        normalise_file("a/x.yaml", namespace="b")


def test_an_explicit_file_is_honoured(home: Path) -> None:
    apply_and_commit(
        home,
        CreateElement(
            kind="server",
            name="srv-new",
            namespace="hosts",
            spec=SERVER_SPEC,
            file="hosts/extra.yaml",
        ),
    )
    assert (home / "hosts/extra.yaml").is_file()


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def test_deleting_a_cabled_device_is_refused_and_names_the_cables(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(CascadeRequired) as excinfo:
        session.apply(DeleteElement(address="sw-home"))
    assert excinfo.value.dependents == (
        "cables/cbl-rtr-sw",
        "cables/cbl-sw-ap",
        "cables/cbl-sw-desk",
        "cables/cbl-sw-dongle",
        "cables/cbl-sw-nas",
    )
    assert not session.changes


def test_cascading_takes_the_cables_with_it(home: Path) -> None:
    apply_and_commit(home, DeleteElement(address="sw-home", cascade=True))
    inventory = load_tree(home)
    assert "switches/sw-home" not in inventory.elements
    assert not any(fqn.startswith("cables/cbl-sw") for fqn in inventory.cables)
    assert not inventory.errors


def test_deleting_the_last_document_deletes_the_file_and_the_folder(home: Path) -> None:
    apply_and_commit(home, DeleteElement(address="sw-home", cascade=True))
    assert not (home / "switches").exists()


def test_deleting_one_document_of_many_leaves_the_others_alone(home: Path) -> None:
    before = (home / "cables/links.yaml").read_text(encoding="utf-8")
    apply_and_commit(home, Disconnect(address="cbl-sw-nas"))
    after = (home / "cables/links.yaml").read_text(encoding="utf-8")
    removed, added = changed_lines(before, after)
    assert added == []
    assert "  name: cbl-sw-nas" in removed
    assert "  name: cbl-sw-desk" not in removed


def test_an_adapters_attached_to_is_cleared_rather_than_followed(home: Path) -> None:
    """An optional reference does not take its holder with it."""
    apply_and_commit(home, DeleteElement(address="sw-home", cascade=True))
    inventory = load_tree(home)
    adapter = inventory.adapters["hosts/adp-usb-eth"]
    assert adapter.spec.upstream.attached_to is not None  # it hangs off the laptop, not the switch
    assert "hosts/adp-usb-eth" in inventory.elements


def test_disconnecting_something_that_is_not_a_cable_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="not a cable"):
        session.apply(Disconnect(address="sw-home"))


# --------------------------------------------------------------------------- #
# rename
# --------------------------------------------------------------------------- #


def test_renaming_rewrites_every_reference(home: Path) -> None:
    apply_and_commit(home, RenameElement(address="sw-home", new_name="sw-hall"))
    inventory = load_tree(home)
    assert not inventory.errors
    assert "switches/sw-hall" in inventory.devices
    text = (home / "cables/links.yaml").read_text(encoding="utf-8")
    assert "sw-home:" not in text
    assert text.count("sw-hall:") == 5


def test_renaming_touches_only_the_reference_lines(home: Path) -> None:
    before = (home / "cables/links.yaml").read_text(encoding="utf-8")
    apply_and_commit(home, RenameElement(address="sw-home", new_name="sw-hall"))
    after = (home / "cables/links.yaml").read_text(encoding="utf-8")
    removed, added = changed_lines(before, after)
    assert all("sw-home:" in line for line in removed)
    assert all("sw-hall:" in line for line in added)


def test_renaming_to_the_same_name_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="already called"):
        session.apply(RenameElement(address="sw-home", new_name="sw-home"))


def test_renaming_onto_an_existing_name_is_refused(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: a\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n---\n"
        "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: b\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n",
        encoding="utf-8",
    )
    session = EditSession(root=tmp_path)
    with pytest.raises(EditError, match="already exists"):
        session.apply(RenameElement(address="a", new_name="b"))


def test_a_qualified_reference_stays_qualified(tmp_path: Path) -> None:
    """The spelling an author chose is a choice, and it is kept."""
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "sw.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: sw\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n",
        encoding="utf-8",
    )
    (tmp_path / "pc.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: computer\nmetadata:\n  name: pc\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n",
        encoding="utf-8",
    )
    (tmp_path / "link.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: cable\nmetadata:\n  name: c1\n"
        "spec:\n  endpoints:\n    - site/sw:eth0\n    - pc:eth0\n  medium: copper\n",
        encoding="utf-8",
    )
    apply_and_commit(tmp_path, RenameElement(address="site/sw", new_name="sw2"))
    text = (tmp_path / "link.yaml").read_text(encoding="utf-8")
    assert "- site/sw2:eth0" in text
    assert not load_tree(tmp_path).errors


# --------------------------------------------------------------------------- #
# move
# --------------------------------------------------------------------------- #


def test_moving_within_a_namespace_carries_the_document_verbatim(home: Path) -> None:
    original = (home / "hosts/pc-desk.yaml").read_text(encoding="utf-8")
    apply_and_commit(home, MoveElement(address="pc-desk", file="hosts/desktops.yaml"))
    assert not (home / "hosts/pc-desk.yaml").exists()
    assert (home / "hosts/desktops.yaml").read_text(encoding="utf-8") == original


def test_the_inverse_of_a_move_within_a_namespace_is_a_move(home: Path) -> None:
    session = EditSession(root=home)
    applied = session.apply(MoveElement(address="pc-desk", file="hosts/desktops.yaml"))
    assert applied.inverse == (
        MoveElement(address="hosts/pc-desk", file="hosts/pc-desk.yaml", index=0),
    )


def test_moving_to_another_folder_changes_the_namespace_and_the_references(home: Path) -> None:
    apply_and_commit(home, MoveElement(address="pc-desk", file="office/pc-desk.yaml"))
    inventory = load_tree(home)
    assert "office/pc-desk" in inventory.devices
    assert not inventory.errors


def test_moving_somewhere_it_already_is_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="already in"):
        session.apply(MoveElement(address="pc-desk", file="hosts/pc-desk.yaml"))


def test_moving_onto_a_colliding_name_is_refused(tmp_path: Path) -> None:
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "sw.yaml").write_text(
            "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: sw\n"
            "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n",
            encoding="utf-8",
        )
    session = EditSession(root=tmp_path)
    with pytest.raises(EditError, match="would collide"):
        session.apply(MoveElement(address="a/sw", file="b/sw.yaml"))


# --------------------------------------------------------------------------- #
# connect / interfaces
# --------------------------------------------------------------------------- #


def test_connecting_derives_a_name_and_places_the_cable(campus: Path) -> None:
    session = EditSession(root=campus)
    applied = session.apply(
        Connect(
            a="sw-north-acc-01:GigabitEthernet1/0/3",
            b="sw-south-acc-01:GigabitEthernet1/0/3",
        )
    )
    session.commit()
    assert applied.inverse == (Disconnect(address="sites/cbl-sw-north-acc-01-sw-south-acc-01"),)
    inventory = load_tree(campus)
    assert "sites/cbl-sw-north-acc-01-sw-south-acc-01" in inventory.cables


def test_connecting_to_a_port_that_does_not_exist_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="no interface called 'nope'"):
        session.apply(Connect(a="sw-home:nope", b="pc-desk:eno1"))


@pytest.mark.parametrize("endpoint", ["sw-home", "sw-home:a:b", "sw-home:"])
def test_a_malformed_endpoint_is_refused(home: Path, endpoint: str) -> None:
    session = EditSession(root=home)
    with pytest.raises(OperationError, match="not an endpoint"):
        session.apply(Connect(a=endpoint, b="pc-desk:eno1"))


def test_a_cable_spec_is_carried_through(campus: Path) -> None:
    apply_and_commit(
        campus,
        Connect(
            a="sw-north-acc-01:GigabitEthernet1/0/3",
            b="sw-south-acc-01:GigabitEthernet1/0/3",
            spec={"medium": "fiber", "speed": "10Gbps", "label": "X-1"},
            name="cbl-x",
            namespace="backbone",
        ),
    )
    cable = load_tree(campus).cables["backbone/cbl-x"]
    assert cable.spec.medium.value == "fiber"
    assert cable.spec.label == "X-1"


def test_adding_an_interface_appends_it(home: Path) -> None:
    session = EditSession(root=home)
    applied = session.apply(
        AddInterface(address="sw-home", interface={"name": "port6", "type": "ethernet"})
    )
    session.commit()
    assert applied.inverse == (RemoveInterface(address="switches/sw-home", name="port6"),)
    device = load_tree(home).devices["switches/sw-home"]
    assert device.interface("port6") is not None


def test_adding_an_interface_that_is_already_there_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="already has an interface"):
        session.apply(AddInterface(address="sw-home", interface={"name": "port1"}))


def test_an_interface_with_no_name_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(OperationError, match="needs a 'name'"):
        session.apply(AddInterface(address="sw-home", interface={"type": "ethernet"}))


def test_adding_an_interface_to_something_without_any_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="no interfaces"):
        session.apply(AddInterface(address="cbl-sw-nas", interface={"name": "x"}))


def test_removing_a_cabled_interface_is_refused_then_cascades(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(CascadeRequired) as excinfo:
        session.apply(RemoveInterface(address="sw-home", name="port3"))
    assert excinfo.value.dependents == ("cables/cbl-sw-nas",)

    apply_and_commit(home, RemoveInterface(address="sw-home", name="port3", cascade=True))
    inventory = load_tree(home)
    assert inventory.devices["switches/sw-home"].interface("port3") is None
    assert "cables/cbl-sw-nas" not in inventory.cables


def test_removing_an_interface_a_template_contributed_is_refused(campus: Path) -> None:
    session = EditSession(root=campus)
    with pytest.raises(EditError, match=r"range.*template"):
        session.apply(
            RemoveInterface(address="sw-north-acc-03", name="GigabitEthernet1/0/1", cascade=True)
        )


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #


def test_the_references_of_a_cable_are_its_endpoints(home: Path) -> None:
    inventory = load_tree(home)
    cable = inventory.cables["cables/cbl-sw-nas"]
    found = list(references_of("cables/cbl-sw-nas", cable))
    assert [reference.role for reference in found] == [ReferenceRole.ENDPOINT] * 2
    assert {reference.target for reference in found} == {"sw-home", "srv-nas"}


def test_an_adapters_upstream_is_a_reference(home: Path) -> None:
    inventory = load_tree(home)
    adapter = inventory.adapters["hosts/adp-usb-eth"]
    roles = {reference.role for reference in references_of("hosts/adp-usb-eth", adapter)}
    assert ReferenceRole.ATTACHED_TO in roles


def test_the_name_index_resolves_the_way_the_loader_does(campus: Path) -> None:
    inventory = load_tree(campus)
    index = NameIndex(inventory.elements)
    for fqn in inventory.elements:
        namespace = fqn.rpartition("/")[0]
        for written in (fqn.rpartition("/")[2], fqn):
            assert index.lookup(written, namespace) == inventory.resolve_fqn(
                written, namespace=namespace
            )


# --------------------------------------------------------------------------- #
# Reading and writing a value at a path
# --------------------------------------------------------------------------- #


def document() -> dict[str, object]:
    return {"spec": {"model": "X", "interfaces": [{"name": "eth0"}], "count": 1}}


def test_getting_a_value_that_is_not_there_is_missing() -> None:
    data = document()
    assert get_field(data, ("spec", "model")) == "X"
    assert get_field(data, ("spec", "nope")) is MISSING
    assert get_field(data, ("spec", "interfaces", 9)) is MISSING
    assert get_field(data, ("spec", "model", "deeper")) is MISSING


def test_setting_creates_the_mappings_on_the_way() -> None:
    data: dict[str, object] = {}
    set_field(data, ("spec", "power", "draw_watts"), 30)
    assert data == {"spec": {"power": {"draw_watts": 30}}}


def test_setting_a_sequence_entry_that_exists_and_one_that_does_not() -> None:
    data = document()
    set_field(data, ("spec", "interfaces", 0), {"name": "eth1"})
    assert data["spec"]["interfaces"] == [{"name": "eth1"}]  # type: ignore[index]
    with pytest.raises(OperationError, match="no entry 1 to set"):
        set_field(data, ("spec", "interfaces", 1), {})


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ((), "cannot be empty"),
        (("spec", "count", "deeper"), "not a mapping"),
        (("spec", "model", "a", "b"), "not a mapping"),
        (("spec", "interfaces", 9, "name"), "no such entry"),
    ],
)
def test_setting_through_something_that_is_not_a_mapping_is_refused(
    path: tuple[object, ...], message: str
) -> None:
    with pytest.raises(OperationError, match=message):
        set_field(document(), path, "x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ((), "cannot be empty"),
        (("spec", "nope"), "no such field to remove"),
        (("spec", "interfaces", 9), "no entry to remove"),
        (("spec", "nope", "deeper"), "no such field"),
    ],
)
def test_unsetting_what_is_not_there_is_refused(path: tuple[object, ...], message: str) -> None:
    with pytest.raises(OperationError, match=message):
        unset_field(document(), path)  # type: ignore[arg-type]


def test_unsetting_a_sequence_entry_returns_it() -> None:
    data = document()
    assert unset_field(data, ("spec", "interfaces", 0)) == {"name": "eth0"}
    assert data["spec"]["interfaces"] == []  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Rewriting a reference in either spelling
# --------------------------------------------------------------------------- #


def endpoint_reference(index: int = 0) -> Reference:
    return Reference(
        source="c1",
        role=ReferenceRole.ENDPOINT,
        path=("spec", "endpoints", index),
        target="sw1",
        detail="eth0",
    )


def test_the_mapping_spelling_of_an_endpoint_is_rewritten_in_place() -> None:
    data = {"spec": {"endpoints": [{"device": "sw1", "interface": "eth0"}]}}
    assert rewrite_reference(data, endpoint_reference(), "sw2") is True
    assert data["spec"]["endpoints"][0] == {"device": "sw2", "interface": "eth0"}
    settled = Reference(
        source="c1",
        role=ReferenceRole.ENDPOINT,
        path=("spec", "endpoints", 0),
        target="sw2",
        detail="eth0",
    )
    assert rewrite_reference(data, settled, "sw2") is False


def test_rewriting_finds_the_entry_the_path_missed() -> None:
    """The written order is a hint; the text is what decides. See follow-up 15."""
    data = {"spec": {"endpoints": ["other:eth9", "sw1:eth0"]}}
    assert rewrite_reference(data, endpoint_reference(0), "sw2") is True
    assert data["spec"]["endpoints"] == ["other:eth9", "sw2:eth0"]


def test_rewriting_refuses_when_nothing_reads_as_the_reference() -> None:
    data = {"spec": {"endpoints": ["other:eth9"]}}
    with pytest.raises(EditError, match="template that declares it"):
        rewrite_reference(data, endpoint_reference(), "sw2")


def test_a_plain_scalar_reference_is_rewritten_whole() -> None:
    data = {"spec": {"over": "outer"}}
    reference = Reference(
        source="t1", role=ReferenceRole.OVER, path=("spec", "over"), target="outer"
    )
    assert rewrite_reference(data, reference, "outer2") is True
    assert data["spec"]["over"] == "outer2"
    assert (
        rewrite_reference(
            data,
            Reference(source="t1", role=ReferenceRole.OVER, path=("spec", "over"), target="outer2"),
            "outer2",
        )
        is False
    )


def test_dropping_a_power_input_removes_the_entry_not_the_block() -> None:
    data = {"spec": {"power": {"inputs": ["pdu-a:1", "pdu-b:2"]}}}
    reference = Reference(
        source="sw1",
        role=ReferenceRole.POWER_INPUT,
        path=("spec", "power", "inputs", 1),
        target="pdu-b",
        detail="2",
    )
    drop_reference(data, reference)
    assert data["spec"]["power"]["inputs"] == ["pdu-a:1"]


def test_dropping_a_scalar_reference_removes_the_key() -> None:
    data = {"spec": {"upstream": {"name": "usb", "attached_to": "laptop"}}}
    reference = Reference(
        source="dongle",
        role=ReferenceRole.ATTACHED_TO,
        path=("spec", "upstream", "attached_to"),
        target="laptop",
    )
    drop_reference(data, reference)
    assert data["spec"]["upstream"] == {"name": "usb"}


def test_a_reference_prints_with_and_without_its_detail() -> None:
    assert str(endpoint_reference()) == "c1 spec.endpoints[0] -> sw1:eth0"
    assert (
        str(Reference(source="t", role=ReferenceRole.OVER, path=("spec", "over"), target="o"))
        == "t spec.over -> o"
    )


def test_a_deleted_pdu_takes_the_power_block_with_the_last_input(tmp_path: Path) -> None:
    """``powered_by: outlet`` with no inputs is refused by the schema (§17)."""
    (tmp_path / "tree.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: pdu\nmetadata:\n  name: pdu-a\n"
        "spec:\n  outlets: 4\n---\n"
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n  name: sw\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
        "  power:\n    inputs:\n      - pdu-a:1\n",
        encoding="utf-8",
    )
    session = EditSession(root=tmp_path)
    with pytest.raises(CascadeRequired) as excinfo:
        session.apply(DeleteElement(address="pdu-a"))
    assert excinfo.value.dependents == ("sw",)

    apply_and_commit(tmp_path, DeleteElement(address="pdu-a", cascade=True))
    text = (tmp_path / "tree.yaml").read_text(encoding="utf-8")
    assert "power:" not in text
    assert "name: sw" in text
    assert not load_tree(tmp_path).errors


# --------------------------------------------------------------------------- #
# Moving between namespaces, and the primitives
# --------------------------------------------------------------------------- #


def two_sites(root: Path) -> None:
    """Two namespaces, each with a ``sw``, and a cable in one of them."""
    for folder in ("a", "b"):
        (root / folder).mkdir(parents=True)
        (root / folder / "sw.yaml").write_text(
            "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: sw\n"
            "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
            "    - name: eth1\n      type: ethernet\n",
            encoding="utf-8",
        )
    (root / "a" / "link.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: cable\nmetadata:\n  name: c1\n"
        "spec:\n  endpoints:\n    - sw:eth0\n    - sw:eth1\n  medium: copper\n",
        encoding="utf-8",
    )


def test_moving_a_document_re_qualifies_the_references_it_makes(tmp_path: Path) -> None:
    """A plain name resolves outwards, so a document that moves can change meaning."""
    two_sites(tmp_path)
    assert load_tree(tmp_path).cables["a/c1"].endpoints[0].device == "sw"

    apply_and_commit(tmp_path, MoveElement(address="a/c1", file="b/link.yaml"))
    text = (tmp_path / "b/link.yaml").read_text(encoding="utf-8")
    assert "- a/sw:eth0" in text and "- a/sw:eth1" in text
    inventory = load_tree(tmp_path)
    assert not inventory.errors
    assert inventory.resolve_fqn("a/sw", namespace="b") == "a/sw"


HUB = (
    "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: {name}\n"
    "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
)


def test_the_primitives_write_and_remove_a_whole_file(tmp_path: Path) -> None:
    (tmp_path / "one.yaml").write_text(HUB.format(name="one"), encoding="utf-8")
    session = EditSession(root=tmp_path)
    session.apply(RemoveFile(path="one.yaml"))
    session.apply(WriteFile(path="two.yaml", text=HUB.format(name="two")))
    session.commit()
    assert not (tmp_path / "one.yaml").exists()
    assert list(load_tree(tmp_path).devices) == ["two"]


def test_the_gate_still_judges_a_primitive(home: Path) -> None:
    """A ``write-file`` is a shortcut past the round-trip machinery, not past the checks."""
    session = EditSession(root=home)
    session.apply(RemoveFile(path="hosts/phone.yaml"))
    with pytest.raises(ValidationRefused, match="1 new problem"):
        session.commit()
    assert (home / "hosts/phone.yaml").is_file()


@pytest.mark.parametrize(
    "operation", [RemoveFile(path="notes.txt"), WriteFile(path="_x.yaml", text="")]
)
def test_a_primitive_outside_the_inventory_is_refused(home: Path, operation: object) -> None:
    session = EditSession(root=home)
    with pytest.raises(PlacementError):
        session.apply(operation)  # type: ignore[arg-type]


def test_removing_a_file_that_is_not_there_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="does not exist"):
        session.apply(RemoveFile(path="hosts/ghost.yaml"))


def test_editing_something_an_earlier_operation_deleted_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    session.apply(RemoveFile(path="hosts/phone.yaml"))
    with pytest.raises(EditError, match="deleted by an earlier operation"):
        session.tree.open("hosts/phone.yaml")


def test_reading_a_file_that_is_not_utf8_is_refused(home: Path) -> None:
    (home / "hosts/binary.yaml").write_bytes(b"kind: sw\xefitch\n")
    session = EditSession(root=home)
    with pytest.raises(EditError, match="cannot read"):
        session.tree.open("hosts/binary.yaml")


def test_asking_for_a_document_that_is_not_there_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="no document #9"):
        session.tree.document("hosts/phone.yaml", 9)
    with pytest.raises(EditError, match="no document #9"):
        session.tree.remove_document("hosts/phone.yaml", 9)


def test_creating_a_file_that_is_already_there_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="already exists"):
        session.tree.create("hosts/phone.yaml")


def test_a_second_cable_between_the_same_two_ports_gets_the_long_name(home: Path) -> None:
    """The naming ladder: short, then port-qualified, then a counter."""
    session = EditSession(root=home)
    session.apply(AddInterface(address="sw-home", interface={"name": "port6", "type": "ethernet"}))
    session.apply(AddInterface(address="srv-nas", interface={"name": "eth1", "type": "ethernet"}))
    first = session.apply(Connect(a="sw-home:port6", b="srv-nas:eth1"))
    session.commit()
    # The two ends sit in different folders, so the cable lands in the root
    # namespace that contains both of them.
    assert first.inverse == (Disconnect(address="cbl-sw-home-srv-nas"),)

    session = EditSession(root=home)
    session.apply(AddInterface(address="sw-home", interface={"name": "port7", "type": "ethernet"}))
    session.apply(AddInterface(address="srv-nas", interface={"name": "eth2", "type": "ethernet"}))
    second = session.apply(Connect(a="sw-home:port7", b="srv-nas:eth2"))
    session.commit()
    assert second.inverse == (Disconnect(address="cbl-sw-home-port7-srv-nas-eth2"),)
    assert not load_tree(home).errors


def test_a_cable_name_that_is_taken_twice_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    with pytest.raises(EditError, match="already exists; give the cable another name"):
        session.apply(
            Connect(a="sw-home:port1", b="pc-desk:eno1", name="cbl-rtr-sw", namespace="cables")
        )


def test_removing_a_port_removes_the_vlan_interfaces_stacked_on_it(tmp_path: Path) -> None:
    """A sub-interface cannot outlive its parent (§6.2.4)."""
    (tmp_path / "sw.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n  name: sw\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
        "    - name: eth1\n      type: ethernet\n"
        "    - name: Vlan10\n      type: vlan\n      parent: eth0\n"
        "      vlan:\n        mode: access\n        access_vlan: 10\n",
        encoding="utf-8",
    )
    session = EditSession(root=tmp_path)
    with pytest.raises(CascadeRequired) as excinfo:
        session.apply(RemoveInterface(address="sw", name="eth0"))
    assert excinfo.value.dependents == ("Vlan10",)

    apply_and_commit(tmp_path, RemoveInterface(address="sw", name="eth0", cascade=True))
    text = (tmp_path / "sw.yaml").read_text(encoding="utf-8")
    assert "Vlan10" not in text and "eth0" not in text
    assert "eth1" in text
    assert not load_tree(tmp_path).errors


def test_a_problem_prints_its_location_and_rule() -> None:
    assert str(Problem(rule="E001", location="a.yaml#0:3", message="boom")) == (
        "a.yaml#0:3: E001: boom"
    )


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #


def test_an_edit_that_would_break_the_tree_is_refused(home: Path) -> None:
    session = EditSession(root=home)
    session.apply(SetField(address="cbl-sw-nas", path="spec.endpoints[0]", value="ghost:eth0"))
    problems = session.check()
    assert problems and any("ghost" in problem.message for problem in problems)
    with pytest.raises(ValidationRefused) as excinfo:
        session.commit()
    assert excinfo.value.problems
    assert snapshot(home)["cables/links.yaml"] != session.changes["cables/links.yaml"].encode()


def test_force_writes_it_anyway(home: Path) -> None:
    session = EditSession(root=home)
    session.apply(SetField(address="cbl-sw-nas", path="spec.endpoints[0]", value="ghost:eth0"))
    assert session.commit(force=True) == ("cables/links.yaml",)
    assert "ghost:eth0" in (home / "cables/links.yaml").read_text(encoding="utf-8")


def test_a_pre_existing_problem_does_not_block_an_unrelated_edit(home: Path) -> None:
    """An inventory that already fails is exactly when an editor is needed."""
    apply_and_commit(home, DeleteElement(address="sw-home", cascade=True))
    assert load_tree(home).errors == []
    session = EditSession(root=home)
    session.apply(SetField(address="pc-desk", path="spec.vendor", value="Lenovo"))
    assert session.check() == ()
    assert session.commit() == ("hosts/pc-desk.yaml",)


def test_a_file_that_changed_on_disk_is_a_conflict(home: Path) -> None:
    session = EditSession(root=home)
    session.apply(SetField(address="sw-home", path="spec.serial", value="X1"))
    target = home / "switches/sw-home.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "\n# somebody else\n", encoding="utf-8")
    with pytest.raises(ConflictError) as excinfo:
        session.commit()
    assert excinfo.value.path == "switches/sw-home.yaml"
    assert "# somebody else" in target.read_text(encoding="utf-8")


def test_a_conflict_is_not_overridden_by_force(home: Path) -> None:
    session = EditSession(root=home)
    session.apply(SetField(address="sw-home", path="spec.serial", value="X1"))
    (home / "switches/sw-home.yaml").unlink()
    with pytest.raises(ConflictError):
        session.commit(force=True)


def test_a_file_created_under_the_session_is_a_conflict(home: Path) -> None:
    session = EditSession(root=home)
    session.apply(CreateElement(kind="server", name="srv-new", namespace="hosts", spec=SERVER_SPEC))
    (home / "hosts/srv-new.yaml").write_text("# taken\n", encoding="utf-8")
    with pytest.raises(ConflictError, match="created by something else"):
        session.commit()


# --------------------------------------------------------------------------- #
# Batches
# --------------------------------------------------------------------------- #


def test_a_batch_sees_what_the_previous_operation_did(home: Path) -> None:
    session = EditSession(root=home)
    session.apply(
        CreateElement(
            kind="server",
            name="srv-new",
            namespace="hosts",
            spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
        )
    )
    session.apply(SetField(address="hosts/srv-new", path="spec.vendor", value="Supermicro"))
    # The second operation resolved an address the first one invented; the third
    # is refused for a reason only the first two could have produced.
    with pytest.raises(EditError, match="no interface called 'port6'"):
        session.apply(Connect(a="sw-home:port6", b="srv-new:eth0"))
    assert session.commit()
    assert load_tree(home).devices["hosts/srv-new"].spec.vendor == "Supermicro"


def test_a_batch_is_judged_as_one_change(home: Path) -> None:
    """An operation that is only valid once a later one has run is fine."""
    session = EditSession(root=home)
    session.apply(
        CreateElement(
            kind="server",
            name="srv-new",
            namespace="hosts",
            spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
        )
    )
    session.apply(AddInterface(address="sw-home", interface={"name": "port6", "type": "ethernet"}))
    session.apply(Connect(a="sw-home:port6", b="srv-new:eth0"))
    assert session.check() == ()
    assert session.commit()
    assert not load_tree(home).errors


# --------------------------------------------------------------------------- #
# Corners
# --------------------------------------------------------------------------- #


def test_the_cable_name_ladder_falls_through_to_a_counter(tmp_path: Path) -> None:
    """Both derived names taken by other elements, so a serial is appended."""
    (tmp_path / "tree.yaml").write_text(
        HUB.format(name="a")
        + "---\n"
        + HUB.format(name="b")
        + "---\n"
        + HUB.format(name="cbl-a-b")
        + "---\n"
        + HUB.format(name="cbl-a-eth0-b-eth0"),
        encoding="utf-8",
    )
    session = EditSession(root=tmp_path)
    applied = session.apply(Connect(a="a:eth0", b="b:eth0"))
    assert applied.inverse == (Disconnect(address="cbl-a-eth0-b-eth0-2"),)


UNFAITHFUL = (
    "apiVersion: netgraph.dev/v1alpha1\nkind: hub\nmetadata:\n  name: h\n"
    "spec:\n  interfaces:\n  - name: eth0\n    type: ethernet\n"
    '  location: "\\U0001F4A1"\n'
)


def test_a_document_that_does_not_round_trip_gets_no_semantic_inverse(tmp_path: Path) -> None:
    """See entry 15's neighbour in ``docs/editing.md``: exactness is the condition."""
    (tmp_path / "h.yaml").write_text(UNFAITHFUL, encoding="utf-8")
    session = EditSession(root=tmp_path)
    applied = session.apply(
        AddInterface(address="h", interface={"name": "eth1", "type": "ethernet"})
    )
    assert [operation.op for operation in applied.inverse] == ["write-file"]


def test_a_file_this_session_deleted_can_be_made_again(tmp_path: Path) -> None:
    (tmp_path / "one.yaml").write_text(HUB.format(name="one"), encoding="utf-8")
    session = EditSession(root=tmp_path)
    assert session.tree.exists("one.yaml")
    session.apply(RemoveFile(path="one.yaml"))
    assert not session.tree.exists("one.yaml")
    session.apply(WriteFile(path="one.yaml", text=HUB.format(name="again")))
    session.commit()
    assert list(load_tree(tmp_path).devices) == ["again"]


def test_placement_does_not_reuse_a_file_this_session_deleted(tmp_path: Path) -> None:
    (tmp_path / "cables.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: cable\nmetadata:\n  name: c1\n"
        "spec:\n  endpoints:\n    - a:eth0\n    - b:eth0\n  medium: copper\n",
        encoding="utf-8",
    )
    (tmp_path / "hubs.yaml").write_text(
        HUB.format(name="a") + "---\n" + HUB.format(name="b"), encoding="utf-8"
    )
    session = EditSession(root=tmp_path)
    session.apply(RemoveFile(path="cables.yaml"))
    assert "cables.yaml" not in session.tree.facts(session.inventory)


def test_a_document_inserted_first_inherits_the_marker_style(tmp_path: Path) -> None:
    """A file that opened with an explicit ``---`` still does."""
    parsed = YamlFile.parse("---\nkind: hub\n", relative="h.yaml")
    parsed.insert(0, YamlDocument(text="kind: hub\n"))
    assert parsed.render() == "---\nkind: hub\n---\nkind: hub\n"


def test_a_reference_path_that_indexes_a_mapping_falls_back_to_a_search() -> None:
    data = {"spec": {"endpoints": {"device": "sw1", "interface": "eth0"}}}
    reference = Reference(
        source="c1",
        role=ReferenceRole.ENDPOINT,
        path=("spec", "endpoints", 0),
        target="sw1",
        detail="eth0",
    )
    with pytest.raises(EditError, match="does not read as"):
        rewrite_reference(data, reference, "sw2")


def test_a_reference_beside_something_that_is_not_a_reference_is_still_found() -> None:
    data = {"spec": {"endpoints": [42, "sw1:eth0"]}}
    assert rewrite_reference(data, endpoint_reference(0), "sw2") is True
    assert data["spec"]["endpoints"] == [42, "sw2:eth0"]


# --------------------------------------------------------------------------- #
# The JSON form
# --------------------------------------------------------------------------- #


ROUND_TRIP_OPERATIONS = [
    CreateElement(kind="switch", name="a", namespace="n", spec={"x": 1}, metadata={"y": 2}),
    CreateElement(kind="switch", name="a", file="n/a.yaml"),
    DeleteElement(address="a", cascade=True),
    RenameElement(address="a", new_name="b"),
    MoveElement(address="a", file="b.yaml", index=2),
    SetField(address="a", path="spec.model", value=[1, 2]),
    UnsetField(address="a", path="spec.model"),
    AddInterface(address="a", interface={"name": "eth0"}, index=1),
    RemoveInterface(address="a", name="eth0", cascade=True),
    Connect(a="a:eth0", b="b:eth0", spec={"medium": "fiber"}, name="c", namespace="n"),
    Disconnect(address="c"),
    WriteFile(path="a.yaml", text="kind: hub\n"),
    RemoveFile(path="a.yaml"),
]


@pytest.mark.parametrize("operation", ROUND_TRIP_OPERATIONS, ids=lambda op: op.op)
def test_every_operation_round_trips_through_json(operation: object) -> None:
    payload = operation.to_dict()  # type: ignore[attr-defined]
    assert operation_from_dict(json.loads(json.dumps(payload))) == operation
    assert operation.describe()  # type: ignore[attr-defined]


def test_a_list_and_a_single_object_are_both_accepted() -> None:
    one = '{"op": "delete", "address": "a"}'
    assert operations_from_json(one) == (DeleteElement(address="a"),)
    assert operations_from_json(f"[{one}]") == (DeleteElement(address="a"),)


def test_operations_render_as_a_json_list() -> None:
    text = operations_to_json([DeleteElement(address="a")])
    assert json.loads(text) == [{"op": "delete", "address": "a"}]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("", "no operations"),
        ("{", "cannot read"),
        ("3", "got int"),
        ("[3]", "must be a JSON object"),
        ("[{}]", "must carry an 'op'"),
        ('[{"op": "nope"}]', "unknown operation"),
        ('[{"op": "delete"}]', "missing required key"),
        ('[{"op": "delete", "address": "a", "x": 1}]', "unknown key"),
    ],
)
def test_a_malformed_operation_says_what_is_wrong(payload: str, message: str) -> None:
    with pytest.raises(OperationError, match=message):
        operations_from_json(payload)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def run(root: Path, *args: str, stdin: str | None = None) -> object:
    return CliRunner().invoke(cli, ["-i", str(root), "edit", *args], input=stdin)


def test_the_command_writes_and_reports(home: Path) -> None:
    result = run(home, "set", "sw-home", "spec.model", "C9300")
    assert result.exit_code == 0, result.output
    assert "switches/sw-home.yaml" in result.output
    assert "model: C9300" in (home / "switches/sw-home.yaml").read_text(encoding="utf-8")


def test_dry_run_writes_nothing_and_prints_a_diff(home: Path) -> None:
    before = snapshot(home)
    result = run(home, "set", "sw-home", "spec.model", "C9300", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "--- a/switches/sw-home.yaml" in result.output
    assert "+  model: C9300" in result.output
    assert snapshot(home) == before


def test_a_value_is_read_as_yaml_unless_string_is_asked_for(home: Path) -> None:
    assert run(home, "set", "sw-home", "spec.interfaces[2].mtu", "9000").exit_code == 0
    assert load_tree(home).devices["switches/sw-home"].spec.interfaces[2].mtu == 9000
    assert run(home, "set", "sw-home", "spec.model", "1500", "--string").exit_code == 0
    assert load_tree(home).devices["switches/sw-home"].spec.model == "1500"


def test_the_json_output_carries_the_inverse(home: Path) -> None:
    result = run(home, "rename", "sw-home", "sw-hall", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"][0]["operation"]["op"] == "rename"
    assert {entry["op"] for entry in payload["inverse"]} == {"write-file"}
    assert payload["written"] == ["cables/links.yaml", "switches/sw-home.yaml"]


def test_the_json_inverse_undoes_the_change(home: Path) -> None:
    before = snapshot(home)
    result = run(home, "rename", "sw-home", "sw-hall", "--json")
    inverse = json.dumps(json.loads(result.stdout)["inverse"])
    assert run(home, "apply", stdin=inverse).exit_code == 0
    assert snapshot(home) == before


def test_a_refusal_exits_one_and_lists_the_dependents(home: Path) -> None:
    result = run(home, "delete", "sw-home")
    assert result.exit_code == 1
    assert "cables/cbl-sw-nas" in result.output


def test_the_group_with_no_subcommand_reads_stdin(home: Path) -> None:
    payload = '{"op": "set", "address": "sw-home", "path": "spec.serial", "value": "S1"}'
    result = CliRunner().invoke(cli, ["-i", str(home), "edit"], input=payload)
    assert result.exit_code == 0, result.output
    assert load_tree(home).devices["switches/sw-home"].spec.serial == "S1"


def test_operations_can_come_from_a_file(home: Path, tmp_path: Path) -> None:
    source = tmp_path / "ops.json"
    source.write_text(
        '[{"op": "set", "address": "sw-home", "path": "spec.serial", "value": "S1"}]',
        encoding="utf-8",
    )
    assert run(home, "apply", "-f", str(source)).exit_code == 0
    assert load_tree(home).devices["switches/sw-home"].spec.serial == "S1"


def test_an_edit_that_changes_nothing_says_so(home: Path) -> None:
    result = run(home, "set", "sw-home", "spec.model", "TL-SG108E")
    assert result.exit_code == 0, result.output
    assert "nothing to change" in result.output


def test_the_command_creates_connects_and_disconnects(home: Path) -> None:
    assert run(home, "add-interface", "sw-home", "port6", "--field", "mtu=9000").exit_code == 0
    assert (
        run(
            home,
            "create",
            "server",
            "srv-new",
            "--namespace",
            "hosts",
            "--spec",
            json.dumps(SERVER_SPEC),
        ).exit_code
        == 0
    )
    assert run(home, "connect", "sw-home:port6", "srv-new:eth0", "--speed", "1Gbps").exit_code == 0
    inventory = load_tree(home)
    assert not inventory.errors
    (cable,) = [fqn for fqn in inventory.cables if "srv-new" in fqn]
    assert run(home, "disconnect", cable).exit_code == 0
    assert "srv-new" not in str(load_tree(home).cables)


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--spec", "not json"), ("--metadata", "[]")],
)
def test_a_bad_json_flag_is_a_usage_error(home: Path, flag: str, value: str) -> None:
    result = run(home, "create", "server", "srv-new", flag, value)
    assert result.exit_code == 2


def test_a_bad_field_assignment_is_a_usage_error(home: Path) -> None:
    result = run(home, "add-interface", "sw-home", "port6", "--field", "nonsense")
    assert result.exit_code == 2


def test_the_command_moves_and_unsets(home: Path) -> None:
    assert run(home, "unset", "sw-home", "spec.location").exit_code == 0
    assert run(home, "move", "sw-home", "network/sw-home.yaml").exit_code == 0
    assert "network/sw-home" in load_tree(home).devices
