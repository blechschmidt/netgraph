"""Tests for the recursive YAML folder loader (``docs/schema.md`` §2)."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest
import yaml

from netgraph.errors import MAX_ECHOED_VALUE_LENGTH, LoaderError, SchemaError
from netgraph.loader import (
    IgnoreRuleSet,
    IgnoreStack,
    Inventory,
    InventoryFile,
    LoadError,
    NodeLoader,
    RawDocument,
    SourceLocation,
    StrictSafeLoader,
    iter_inventory_files,
    load_tree,
    namespace_of,
    qualify,
    read_documents,
    short_name,
)
from netgraph.loader.ignore import compile_rules, parse_ignore_file
from netgraph.loader.tree import _errors_from, _load_file
from netgraph.models import Adapter, Cable, Switch, parse_document

API_VERSION = "netgraph.dev/v1alpha1"

SWITCH = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: {name}
spec:
  interfaces:
    - name: Gi0/1
      type: ethernet
    - name: Gi0/2
      type: ethernet
"""


def write(root: Path, relative: str, content: str) -> Path:
    """Create ``root/relative`` with ``content``, making parent folders."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def switch(name: str) -> str:
    return SWITCH.format(name=name)


def read(loader_class: type[NodeLoader], text: str) -> object:
    """Construct the single document in ``text``.

    ``yaml.load`` cannot be used here: netgraph's loader is selected at import
    time and has no PyYAML base class in common with the alternative, which is
    exactly what ``NodeLoader`` exists to describe.
    """
    loader = loader_class(text)
    try:
        loader.check_node()  # libyaml's parser needs this before ``get_node``
        return loader.construct_document(loader.get_node())
    finally:
        loader.dispose()


@pytest.fixture
def inventory_root(tmp_path: Path) -> Path:
    """A small but representative inventory tree."""
    root = tmp_path / "inventory"
    write(root, "sites/berlin/rack1/sw1.yaml", switch("sw1"))
    write(root, "sites/hq/switches/sw1.yaml", switch("sw1"))
    write(root, "sites/hq/cables/links.yml", CABLE_FILE)
    write(root, "top.yaml", switch("core"))
    return root


CABLE_FILE = """\
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: link-a
spec:
  endpoints:
    - sw1:Gi0/1
    - core:Gi0/2
  medium: copper
---
apiVersion: netgraph.dev/v1alpha1
kind: adapter
metadata:
  name: dongle
spec:
  upstream:
    name: usb0
    type: usb
  interfaces:
    - name: enx0
      type: ethernet
"""


# -- namespaces and indexing ---------------------------------------------


def test_namespace_is_derived_from_the_directory(inventory_root: Path) -> None:
    inventory = load_tree(inventory_root)

    assert inventory.errors == []
    assert sorted(inventory.elements) == [
        "core",
        "sites/berlin/rack1/sw1",
        "sites/hq/cables/dongle",
        "sites/hq/cables/link-a",
        "sites/hq/switches/sw1",
    ]
    assert isinstance(inventory["sites/berlin/rack1/sw1"], Switch)
    assert set(inventory.cables) == {"sites/hq/cables/link-a"}
    assert set(inventory.adapters) == {"sites/hq/cables/dongle"}
    assert set(inventory.devices) == {"core", "sites/berlin/rack1/sw1", "sites/hq/switches/sw1"}
    assert inventory.namespaces == (
        "",
        "sites/berlin/rack1",
        "sites/hq/cables",
        "sites/hq/switches",
    )


def test_source_locations_record_file_index_and_line(inventory_root: Path) -> None:
    inventory = load_tree(inventory_root)

    cable = inventory.source_of("sites/hq/cables/link-a")
    adapter = inventory.source_of("sites/hq/cables/dongle")
    assert cable is not None and adapter is not None
    assert cable.relative == "sites/hq/cables/links.yml"
    assert (cable.index, cable.line) == (0, 1)
    assert (adapter.index, adapter.line) == (1, 11)
    assert str(adapter) == "sites/hq/cables/links.yml#1:11"
    assert adapter.path.is_file()


def test_files_are_loaded_in_byte_wise_path_order(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    for relative in ("b.yaml", "a/z.yaml", "a/a.yaml", "Z.yaml"):
        write(root, relative, switch(relative.replace("/", "-").replace(".yaml", "")))

    files = [entry.relative.as_posix() for entry in iter_inventory_files(root)]

    assert files == ["Z.yaml", "a/a.yaml", "a/z.yaml", "b.yaml"]
    assert list(load_tree(root).elements) == ["Z", "a/a-a", "a/a-z", "b"]


def test_container_protocol(inventory_root: Path) -> None:
    inventory = load_tree(inventory_root)

    assert len(inventory) == 5
    assert "core" in inventory
    assert inventory.get("nope") is None
    assert {element.metadata.name for element in inventory} == {
        "core",
        "sw1",
        "link-a",
        "dongle",
    }
    assert set(inventory.interface_owners) == set(inventory.devices) | set(inventory.adapters)
    assert "elements=5" in repr(inventory)


@pytest.mark.parametrize(
    ("namespace", "name", "expected"),
    [("", "sw1", "sw1"), ("a/b", "sw1", "a/b/sw1")],
)
def test_qualify_and_split(namespace: str, name: str, expected: str) -> None:
    assert qualify(namespace, name) == expected
    assert namespace_of(expected) == namespace
    assert short_name(expected) == name


# -- reference resolution -------------------------------------------------


def test_reference_resolves_in_the_local_namespace_first(inventory_root: Path) -> None:
    inventory = load_tree(inventory_root)

    assert inventory.resolve_fqn("sw1", namespace="sites/berlin/rack1") == "sites/berlin/rack1/sw1"
    assert inventory.resolve_fqn("sw1", namespace="sites/hq/switches") == "sites/hq/switches/sw1"


def test_reference_falls_back_to_ancestors_then_to_the_root(inventory_root: Path) -> None:
    # ``core`` lives at the root, ``link-a`` two levels down.
    assert load_tree(inventory_root).resolve_fqn("core", namespace="sites/hq/cables") == "core"


def test_ambiguous_reference_reports_every_candidate(inventory_root: Path) -> None:
    resolution = load_tree(inventory_root).lookup("sw1", namespace="sites/hq/cables")

    assert not resolution
    assert resolution.element is None
    assert resolution.ambiguous == ("sites/berlin/rack1/sw1", "sites/hq/switches/sw1")


def test_unique_short_name_resolves_from_any_namespace(inventory_root: Path) -> None:
    inventory = load_tree(inventory_root)

    element = inventory.resolve("dongle", namespace="sites/berlin/rack1")

    assert isinstance(element, Adapter)


def test_qualified_reference_is_tried_relative_then_absolute(inventory_root: Path) -> None:
    inventory = load_tree(inventory_root)

    assert inventory.resolve_fqn("switches/sw1", namespace="sites/hq") == "sites/hq/switches/sw1"
    assert inventory.resolve_fqn("sites/hq/switches/sw1") == "sites/hq/switches/sw1"
    assert inventory.resolve_fqn("sites/nope/sw1") is None


def test_unknown_reference_resolves_to_nothing(inventory_root: Path) -> None:
    assert load_tree(inventory_root).resolve("ghost") is None
    assert load_tree(inventory_root).lookup("ghost").ambiguous == ()


def test_names_in_namespace(inventory_root: Path) -> None:
    assert load_tree(inventory_root).names_in("sites/hq/cables") == {
        "link-a": "sites/hq/cables/link-a",
        "dongle": "sites/hq/cables/dongle",
    }


# -- discovery rules ------------------------------------------------------


def test_hidden_and_underscore_paths_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "keep.yaml", switch("keep"))
    write(root, "_drafts/wip.yaml", switch("wip"))
    write(root, ".hidden/secret.yaml", switch("secret"))
    write(root, "_ignored.yaml", switch("ignored"))
    write(root, ".dotfile.yaml", switch("dotfile"))

    assert list(load_tree(root).elements) == ["keep"]


def test_only_yaml_suffixes_are_loaded(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "a.yaml", switch("a"))
    write(root, "b.yml", switch("b"))
    write(root, "c.YAML", switch("c"))
    write(root, "d.json", "{}")
    write(root, "e.txt", "not yaml")

    assert sorted(load_tree(root).elements) == ["a", "b", "c"]


def test_multi_document_files_and_empty_documents(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "many.yaml", f"---\n{switch('a')}---\n\n---\n{switch('b')}")

    inventory = load_tree(root)

    assert inventory.errors == []
    assert sorted(inventory.elements) == ["a", "b"]
    # The empty document keeps its slot so indices still match the separators.
    source = inventory.source_of("b")
    assert source is not None and source.index == 2


def test_anchors_and_aliases_work_within_a_document(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(
        root,
        "anchor.yaml",
        """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: anchored
spec:
  interfaces:
    - &port
      name: Gi0/1
      type: ethernet
    - <<: *port
      name: Gi0/2
""",
    )

    inventory = load_tree(root)

    element = inventory["anchored"]
    assert isinstance(element, Switch)
    assert [interface.name for interface in element.interfaces] == ["Gi0/1", "Gi0/2"]


def test_a_single_yaml_file_may_be_loaded_directly(tmp_path: Path) -> None:
    path = write(tmp_path, "one.yaml", switch("only"))

    inventory = load_tree(path)

    assert list(inventory.elements) == ["only"]
    source = inventory.source_of("only")
    assert source is not None and source.relative == "one.yaml"


def test_missing_or_unusable_root_raises(tmp_path: Path) -> None:
    with pytest.raises(LoaderError, match="does not exist"):
        load_tree(tmp_path / "nope")

    other = write(tmp_path, "notes.txt", "hello")
    with pytest.raises(LoaderError, match="not a directory or a YAML file"):
        load_tree(other)


def test_an_empty_tree_loads_to_an_empty_inventory(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    root.mkdir()

    inventory = load_tree(root)

    assert len(inventory) == 0
    assert not inventory.has_errors


def test_unreadable_directory_is_reported_not_raised(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "ok.yaml", switch("ok"))
    locked = root / "locked"
    locked.mkdir()
    write(root, "locked/hidden.yaml", switch("hidden"))
    locked.chmod(0o000)
    try:
        inventory = load_tree(root)
    finally:
        locked.chmod(0o755)

    if os.geteuid() == 0:  # pragma: no cover - root ignores directory permissions
        pytest.skip("running as root, permissions are not enforced")
    assert list(inventory.elements) == ["ok"]
    assert any("cannot read directory" in error.message for error in inventory.errors)


# -- .netgraphignore ------------------------------------------------------


def test_ignore_file_excludes_directories_and_globs(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, ".netgraphignore", "vendor/\n*.bak.yaml\n/only-at-root.yaml\n")
    write(root, "keep.yaml", switch("keep"))
    write(root, "only-at-root.yaml", switch("rooted"))
    write(root, "deep/only-at-root.yaml", switch("deep-rooted"))
    write(root, "vendor/thing.yaml", switch("vendored"))
    write(root, "deep/old.bak.yaml", switch("stale"))

    assert sorted(load_tree(root).elements) == ["deep/deep-rooted", "keep"]


def test_ignore_negation_reincludes_a_file(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, ".netgraphignore", "*.gen.yaml\n!keep.gen.yaml\n")
    write(root, "a.gen.yaml", switch("a"))
    write(root, "keep.gen.yaml", switch("keep"))

    assert list(load_tree(root).elements) == ["keep"]


def test_nested_ignore_file_applies_to_its_subtree(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "sub/.netgraphignore", "skip.yaml\n")
    write(root, "skip.yaml", switch("root-skip"))
    write(root, "sub/skip.yaml", switch("sub-skip"))
    write(root, "sub/keep.yaml", switch("sub-keep"))

    assert sorted(load_tree(root).elements) == ["root-skip", "sub/sub-keep"]


def test_double_star_matches_across_directories(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, ".netgraphignore", "a/**/generated/\n")
    write(root, "a/b/generated/x.yaml", switch("gen"))
    write(root, "a/generated/y.yaml", switch("gen2"))
    write(root, "a/b/kept.yaml", switch("kept"))

    assert list(load_tree(root).elements) == ["a/b/kept"]


def test_unreadable_ignore_file_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    (root).mkdir()
    (root / ".netgraphignore").write_bytes(b"\xff\xfe not utf-8")
    write(root, "keep.yaml", switch("keep"))

    inventory = load_tree(root)

    assert list(inventory.elements) == ["keep"]
    assert any(".netgraphignore" in error.message for error in inventory.errors)


@pytest.mark.parametrize(
    ("pattern", "path", "is_dir", "expected"),
    [
        ("build", "build", True, True),
        ("build", "a/build", True, True),
        ("build/", "build", False, False),
        ("/build", "a/build", True, False),
        ("a/b", "a/b", False, True),
        ("a/b", "x/a/b", False, False),
        ("*.tmp", "a/x.tmp", False, True),
        # A pattern with no trailing slash matches directories too, and nothing
        # below an excluded directory can be re-included.
        ("*.tmp", "a/x.tmp/y", False, True),
        ("x/*", "x/y/z", False, True),
        ("?.yaml", "a.yaml", False, True),
        ("[0-9].yaml", "3.yaml", False, True),
        ("[!0-9].yaml", "3.yaml", False, False),
        ("a/**", "a/b/c", False, True),
        ("**/gen", "x/y/gen", True, True),
        (r"\#literal", "#literal", False, True),
    ],
)
def test_ignore_pattern_translation(pattern: str, path: str, is_dir: bool, expected: bool) -> None:
    stack = IgnoreStack().push(rules_from(pattern))

    assert stack.is_ignored(path, is_dir=is_dir) is expected


def rules_from(text: str) -> IgnoreRuleSet:
    """Compile ``text`` as a root-level ``.netgraphignore``."""
    return IgnoreRuleSet(base="", source=Path("."), rules=tuple(compile_rules(text.splitlines())))


def test_comments_and_blank_lines_are_skipped() -> None:
    assert compile_rules(["", "   ", "# comment", "!", "/", "//"]) == []


def test_closing_bracket_may_open_a_character_class() -> None:
    stack = IgnoreStack().push(rules_from("[]x].yaml"))

    assert stack.is_ignored("x.yaml", is_dir=False)
    assert stack.is_ignored("].yaml", is_dir=False)


def test_parse_ignore_file_reads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / ".netgraphignore"
    path.write_text("# comment\nbuild/\n", encoding="utf-8")

    rule_set = parse_ignore_file(path, base="sub")

    assert len(rule_set.rules) == 1
    assert rule_set.rules[0].pattern == "build/"
    assert rule_set.verdict("sub/build", is_dir=True) is True
    assert rule_set.verdict("other/build", is_dir=True) is None


# -- error collection -----------------------------------------------------


def test_schema_errors_are_collected_with_field_paths_and_lines(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(
        root,
        "bad.yaml",
        """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: broken
spec:
  interfaces:
    - name: eth0
      type: ethernet
      mtu: 3
  bogus: true
""",
    )
    write(root, "good.yaml", switch("fine"))

    inventory = load_tree(root)

    assert list(inventory.elements) == ["fine"], "one bad file must not hide the others"
    assert [(error.field_path, error.line) for error in inventory.errors] == [
        (("spec", "interfaces", 0, "mtu"), 9),
        (("spec", "bogus"), 10),
    ]
    assert inventory.errors[1].rule == "NG-D005"
    assert str(inventory.errors[0]).startswith("bad.yaml#0:9: spec.interfaces[0].mtu: ")


def test_missing_key_is_located_at_the_closest_known_node(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(
        root,
        "no-spec.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata:\n  name: nospec\n",
    )

    (error,) = load_tree(root).errors

    assert error.field_path == ("spec",)
    assert error.rule == "NG-D001"
    assert error.line == 1


def test_yaml_syntax_error_reports_line_and_column(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "broken.yaml", "kind: switch\n  metadata: oops\n")

    (error,) = load_tree(root).errors

    assert error.line == 2
    assert error.column is not None
    assert "mapping values are not allowed" in error.message
    assert error.location.startswith("broken.yaml:2:")


def test_duplicate_mapping_keys_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "dup.yaml", "kind: switch\nkind: router\n")

    (error,) = load_tree(root).errors

    assert "duplicate key 'kind'" in error.message


def test_a_pathological_duplicate_key_is_not_echoed_in_full(tmp_path: Path) -> None:
    """Entry 3 of ``docs/follow-ups.md``: a rejected value is quoted, not dumped."""
    # PyYAML caps a *simple* key at 1024 characters and reports anything longer
    # as a syntax error before the duplicate check ever sees it, so this is as
    # pathological as a duplicate key gets.
    key = "k" * 900
    root = tmp_path / "inv"
    write(root, "dup.yaml", f"{key}: a\n{key}: b\n")

    (error,) = load_tree(root).errors

    assert "found duplicate key 'kkk" in error.message
    assert error.message.endswith(f"(+{900 - MAX_ECHOED_VALUE_LENGTH} more characters)")
    assert len(error.message) < 400


def test_unknown_kind_is_reported_once(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(
        root,
        "alien.yaml",
        f"apiVersion: {API_VERSION}\nkind: firewall\nmetadata: {{}}\nspec: {{}}\n",
    )

    (error,) = load_tree(root).errors

    assert error.rule == "NG-D003"
    assert error.field_path == ("kind",)


def test_scalar_document_is_reported_as_a_document_error(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "scalar.yaml", "just a string\n")

    (error,) = load_tree(root).errors

    assert error.rule == "NG-D001"
    assert "must be a mapping" in error.message


def test_duplicate_names_in_one_namespace_keep_the_first(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "a.yaml", switch("sw"))
    write(root, "b.yaml", switch("sw"))
    write(root, "sub/c.yaml", switch("sw"))

    inventory = load_tree(root)

    assert sorted(inventory.elements) == ["sub/sw", "sw"]
    (error,) = inventory.errors
    assert error.rule == "NG-N002"
    assert error.relative == "b.yaml"
    assert "first declared at a.yaml#0:1" in error.message


def test_non_utf8_file_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    root.mkdir()
    (root / "latin.yaml").write_bytes(b"kind: sw\xefitch\n")

    (error,) = load_tree(root).errors

    assert "not valid UTF-8" in error.message


def test_load_error_location_without_a_file() -> None:
    assert LoadError(message="boom").location == "-"
    assert str(LoadError(message="boom", rule="NG-D001")) == "-: NG-D001: boom"


# -- safety ---------------------------------------------------------------


def test_python_tags_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    marker = tmp_path / "pwned"
    write(
        root,
        "evil.yaml",
        f"kind: !!python/object/apply:pathlib.Path.touch [!!python/object/apply:pathlib.Path ['{marker}']]\n",
    )

    (error,) = load_tree(root).errors

    assert "could not determine a constructor" in error.message
    assert not marker.exists()


def test_custom_tags_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "tagged.yaml", "kind: !Ref other\n")

    (error,) = load_tree(root).errors

    assert "could not determine a constructor" in error.message


@pytest.mark.parametrize("literal", ["yes", "no", "on", "off", "y", "n"])
def test_yaml_11_booleans_stay_strings(tmp_path: Path, literal: str) -> None:
    path = write(tmp_path, "b.yaml", f"value: {literal}\n")

    (document,) = read_documents(path, relative=PurePosixPath("b.yaml"))

    assert document.data == {"value": literal}


def test_real_booleans_still_parse(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(
        root,
        "flags.yaml",
        """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: flags
spec:
  interfaces:
    - name: Gi0/1
      type: ethernet
      enabled: false
""",
    )

    element = load_tree(root)["flags"]

    assert isinstance(element, Switch)
    assert element.interfaces[0].enabled is False


def test_norway_problem_is_rejected_by_the_model(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(
        root,
        "norway.yaml",
        """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: norway
spec:
  interfaces:
    - name: Gi0/1
      type: ethernet
      enabled: no
""",
    )

    (error,) = load_tree(root).errors

    assert error.field_path == ("spec", "interfaces", 0, "enabled")


def test_the_strict_boolean_rule_does_not_leak_into_pyyaml() -> None:
    """Sanity check on the resolver surgery; ``tests/test_yaml_loader.py``
    repeats it against both parser bases, along with every other guarantee."""
    assert yaml.safe_load("value: yes") == {"value": True}
    assert read(StrictSafeLoader, "value: yes\n") == {"value": "yes"}


# -- symlinks (NG-L003) ---------------------------------------------------


def test_symlink_that_escapes_the_root_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    outside = tmp_path / "outside"
    write(outside, "secret.yaml", switch("secret"))
    write(root, "keep.yaml", switch("keep"))
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "secret.yaml").symlink_to(outside / "secret.yaml")

    inventory = load_tree(root)

    assert list(inventory.elements) == ["keep"]
    assert len(inventory.errors) == 2
    assert all("escapes the inventory root" in error.message for error in inventory.errors)


def test_symlink_cycle_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "sub/keep.yaml", switch("keep"))
    (root / "sub" / "loop").symlink_to(root, target_is_directory=True)

    inventory = load_tree(root)

    assert list(inventory.elements) == ["sub/keep"]
    (error,) = inventory.errors
    assert "forms a cycle" in error.message


def test_directory_reachable_twice_is_loaded_once(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "a/keep.yaml", switch("keep"))
    (root / "b").symlink_to(root / "a", target_is_directory=True)

    inventory = load_tree(root)

    assert list(inventory.elements) == ["a/keep"]
    (error,) = inventory.errors
    assert "already part of the inventory" in error.message


def test_symlinked_file_inside_the_root_is_followed(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "real/sw.yaml", switch("sw"))
    (root / "linked.yaml").symlink_to(root / "real" / "sw.yaml")

    inventory = load_tree(root)

    # Same element declared twice under different namespaces: both load.
    assert sorted(inventory.elements) == ["real/sw", "sw"]
    assert inventory.errors == []


# -- Inventory bookkeeping ------------------------------------------------


def test_add_reports_a_clash_and_record_appends(tmp_path: Path) -> None:
    element = parse_document(
        {
            "apiVersion": API_VERSION,
            "kind": "cable",
            "metadata": {"name": "c1"},
            "spec": {"endpoints": ["a:eth0", "b:eth0"], "medium": "copper"},
        }
    )
    inventory = Inventory(root=tmp_path)
    source = SourceLocation(path=tmp_path / "c.yaml", relative="c.yaml", index=0, line=1)

    assert inventory.add(element, namespace="ns", source=source) == "ns/c1"
    assert inventory.add(element, namespace="ns", source=source) is None
    assert isinstance(inventory["ns/c1"], Cable)
    assert inventory.namespace_for("ns/c1") == "ns"

    inventory.record(LoadError(message="boom"))
    assert inventory.has_errors


# -- edge cases -----------------------------------------------------------


def test_ignore_stack_ignores_empty_rule_sets() -> None:
    stack = IgnoreStack()

    assert not stack
    assert stack.push(None) is stack
    assert stack.push(rules_from("# only a comment")) is stack
    assert not stack.is_ignored("anything.yaml", is_dir=False)


def test_unterminated_character_class_is_a_literal_bracket() -> None:
    stack = IgnoreStack().push(rules_from("[abc.yaml"))

    assert stack.is_ignored("[abc.yaml", is_dir=False)


def test_line_for_degrades_gracefully(tmp_path: Path) -> None:
    path = write(tmp_path, "d.yaml", "spec:\n  interfaces:\n    - name: eth0\n")

    (document,) = read_documents(path, relative=PurePosixPath("d.yaml"))

    assert document.line_for(("spec", "interfaces", 0, "name")) == 3
    # An unusable path component stops at the deepest node that does exist.
    assert document.line_for(("spec", "interfaces", "nope")) == 3
    assert document.line_for(("spec", "interfaces", 7)) == 3
    assert document.line_for(("spec", "interfaces", 0, "name", "deeper")) == 3


def test_line_for_without_a_node() -> None:
    document = RawDocument(data={}, path=Path("x.yaml"), relative=PurePosixPath("x.yaml"), index=0)

    assert document.line is None
    assert document.line_for(("spec",)) is None


def test_a_scanner_error_at_the_very_start_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "tabs.yaml", "\tkind: switch\n")

    (error,) = load_tree(root).errors

    assert error.line == 1
    assert error.path is not None and error.path.name == "tabs.yaml"


def test_special_files_are_not_loaded(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "keep.yaml", switch("keep"))
    os.mkfifo(root / "pipe.yaml")

    inventory = load_tree(root)

    assert list(inventory.elements) == ["keep"]
    assert inventory.errors == []


def test_a_file_that_disappears_is_reported(tmp_path: Path) -> None:
    inventory = Inventory(root=tmp_path)
    entry = InventoryFile(path=tmp_path / "gone.yaml", relative=PurePosixPath("gone.yaml"))

    _load_file(entry, inventory)

    (error,) = inventory.errors
    assert "cannot read file" in error.message
    assert error.relative == "gone.yaml"


def test_a_schema_error_without_issues_still_yields_one_record() -> None:
    document = RawDocument(data={}, path=Path("x.yaml"), relative=PurePosixPath("x.yaml"), index=0)

    (error,) = _errors_from(
        SchemaError("something went wrong"), document=document, relative="x.yaml"
    )

    assert error.message == "something went wrong"
    assert error.field_path == ()


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("a/**", "a/b/c.yaml", True),
        ("a/**", "a", False),
        ("**", "anything/here.yaml", True),
        ("logs/**/*.yaml", "logs/2024/x.yaml", True),
        (r"a\*b.yaml", "a*b.yaml", True),
        (r"a\*b.yaml", "axb.yaml", False),
    ],
)
def test_more_ignore_patterns(pattern: str, path: str, expected: bool) -> None:
    assert IgnoreStack().push(rules_from(pattern)).is_ignored(path, is_dir=False) is expected


def test_a_dangling_symlink_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    write(root, "keep.yaml", switch("keep"))
    (root / "dangling.yaml").symlink_to(tmp_path / "nowhere.yaml")

    inventory = load_tree(root)

    assert list(inventory.elements) == ["keep"]


def test_root_that_is_neither_a_directory_nor_a_file_is_rejected(tmp_path: Path) -> None:
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)

    with pytest.raises(LoaderError, match="not a directory"):
        load_tree(pipe)
