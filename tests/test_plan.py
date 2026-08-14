"""The diff engine and the two commands: ``netgraph.plan``, ``plan``, ``apply``.

Four properties carry the whole feature, and everything here is one of them:

* **Meaning, not text.** Two trees that describe one network diff to nothing,
  however differently they spell it — through templates, ranges and defaults.
* **Renames are renames.** An element that moved name is one entry, not a
  destruction and a creation, and only where the evidence is unambiguous.
* **The order is executable.** A cable is destroyed before the device it lands
  on and created after it, so ``apply`` never hits a refusal the plan caused.
* **A plan is about one state.** It hashes what it was made from and ``apply``
  refuses a tree that has moved on.

The golden files in ``tests/fixtures/plan/`` pin the printed form on the example
inventories. Regenerate them with::

    pytest tests/test_plan.py --regen-golden
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.console import Console
from netgraph.edit import (
    AddInterface,
    Connect,
    CreateElement,
    DeleteElement,
    EditSession,
    Operation,
    RemoveInterface,
    RenameElement,
    SetField,
    UnsetField,
)
from netgraph.fsio import write_text
from netgraph.importer import build_draft, read_inputs
from netgraph.importer.draft import Draft, DraftInterface, DraftVlan
from netgraph.loader import Inventory, load_tree
from netgraph.plan import (
    MISSING,
    Action,
    Address,
    AddressSyntaxError,
    Change,
    FieldChange,
    PathError,
    Plan,
    PlanExecutionError,
    PlanFormatError,
    PlanSourceError,
    Selector,
    StateRef,
    address_of,
    adopt,
    body_of,
    dependencies,
    diff,
    diff_documents,
    document_of,
    fingerprints,
    format_path,
    git_ref,
    operations_for,
    parse_address,
    parse_path,
    plan_from_dict,
    render_plan,
    state_digest,
    summary_line,
    translate,
    write_plan,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
CAPTURES = Path(__file__).resolve().parent / "fixtures" / "drift"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "plan"


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
def twin(tmp_path: Path) -> Path:
    """A second writable copy, for a folder-to-folder plan."""
    root = tmp_path / "twin"
    shutil.copytree(EXAMPLES / "home-lab", root)
    return root


def mutate(root: Path, *operations: Operation) -> None:
    """Apply operations to a tree and write them, so it can be a plan side."""
    session = EditSession(root=root)
    session.apply_all(operations)
    session.commit()


def plan_of(before: Path, after: Path, **kwargs: Any) -> Plan:
    return diff(load_tree(before), load_tree(after), **kwargs)


def text_of(plan: Plan) -> str:
    """The plan as ``netgraph plan`` prints it, without colour."""
    runner = CliRunner()
    with runner.isolation() as (out, _error, _):
        write_plan(Console(color=False), plan)
    return out.getvalue().decode("utf-8")


def addresses(plan: Plan) -> list[str]:
    return [str(change.address) for change in plan]


def entry(plan: Plan, address: str) -> Change:
    for change in plan:
        if str(change.address) == address:
            return change
    raise AssertionError(f"{address} is not in the plan: {addresses(plan)}")


def field(change: Change, path: str) -> FieldChange:
    for item in change.fields:
        if item.text == path:
            return item
    raise AssertionError(f"{path} did not change: {[f.text for f in change.fields]}")


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "type_", "fqn"),
    [
        ("device.core/sw-1", "device", "core/sw-1"),
        ("cable.core/sw1-eth0--rtr-eth1", "cable", "core/sw1-eth0--rtr-eth1"),
        ("device.sw.1", "device", "sw.1"),
        ("pdu.power/pdu-r1-a", "pdu", "power/pdu-r1-a"),
        ("layout.default", "layout", "default"),
    ],
)
def test_an_address_round_trips(text: str, type_: str, fqn: str) -> None:
    """A name may hold a dot, so the type is matched against the closed set."""
    address = parse_address(text)
    assert (address.type, address.fqn) == (type_, fqn)
    assert str(address) == text


@pytest.mark.parametrize("text", ["sw-1", "widget.sw-1", "device.", ""])
def test_an_unparseable_address_is_refused(text: str) -> None:
    with pytest.raises(AddressSyntaxError):
        parse_address(text)


def test_every_device_kind_shares_one_address_type() -> None:
    """Which is what makes switch -> router an update rather than a rebuild."""
    assert address_of("switch", "a/b") == address_of("router", "a/b")
    assert address_of("cable", "a/b") != address_of("switch", "a/b")


def test_an_unknown_kind_has_no_address() -> None:
    with pytest.raises(AddressSyntaxError):
        address_of("widget", "a/b")


@pytest.mark.parametrize(
    ("pattern", "matched"),
    [
        ("device.core/sw-1", True),
        ("core/sw-1", True),
        ("sw-1", True),
        ("device.core/*", True),
        ("cable.*", False),
        ("sw-2", False),
    ],
)
def test_target_patterns_match_three_spellings(pattern: str, matched: bool) -> None:
    assert Address(type="device", fqn="core/sw-1").matches(pattern) is matched


def test_an_address_renames_within_its_namespace() -> None:
    assert str(Address(type="device", fqn="core/sw-1").renamed("sw-2")) == "device.core/sw-2"


# --------------------------------------------------------------------------- #
# Field paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "steps"),
    [
        ("spec.model", ("spec", "model")),
        ("spec.interfaces[2].mtu", ("spec", "interfaces", 2, "mtu")),
        (
            "spec.interfaces[name=eth0].mtu",
            ("spec", "interfaces", Selector("name", "eth0"), "mtu"),
        ),
        ("metadata.labels.site", ("metadata", "labels", "site")),
    ],
)
def test_a_plan_path_round_trips(text: str, steps: tuple[Any, ...]) -> None:
    assert parse_path(text) == steps
    assert format_path(steps) == text


@pytest.mark.parametrize("text", ["", "spec..model", "spec.a[", "spec.a[b]", "spec.a[=1]"])
def test_a_malformed_plan_path_is_refused(text: str) -> None:
    with pytest.raises(PathError):
        parse_path(text)


def test_a_selector_survives_an_insertion() -> None:
    """The whole reason a plan does not store indices."""
    from netgraph.plan.paths import resolve

    before = {"spec": {"interfaces": [{"name": "eth0"}, {"name": "eth1"}]}}
    after = {"spec": {"interfaces": [{"name": "new"}, {"name": "eth0"}, {"name": "eth1"}]}}
    steps = parse_path("spec.interfaces[name=eth1].mtu")
    assert resolve(steps, before) == ("spec", "interfaces", 1, "mtu")
    assert resolve(steps, after) == ("spec", "interfaces", 2, "mtu")


def test_a_selector_that_names_nothing_is_an_error_not_a_guess() -> None:
    from netgraph.plan.paths import resolve

    with pytest.raises(PathError):
        resolve(parse_path("spec.interfaces[name=gone].mtu"), {"spec": {"interfaces": []}})


# --------------------------------------------------------------------------- #
# The document diff
# --------------------------------------------------------------------------- #


def test_absent_and_null_are_different_things() -> None:
    changes = diff_documents({"spec": {"mtu": None}}, {"spec": {}})
    assert len(changes) == 1
    assert changes[0].removed and changes[0].before is None
    assert changes[0].after is MISSING


def test_a_keyed_list_is_matched_by_name_not_by_position() -> None:
    """Inserting at the front is one addition, not one addition and two moves."""
    before = {"spec": {"interfaces": [{"name": "a", "mtu": 1500}, {"name": "b", "mtu": 1500}]}}
    after = {
        "spec": {
            "interfaces": [
                {"name": "c", "mtu": 9000},
                {"name": "a", "mtu": 1500},
                {"name": "b", "mtu": 1500},
            ]
        }
    }
    changes = diff_documents(before, after)
    assert [change.text for change in changes] == ["spec.interfaces[name=c]"]


def test_a_list_with_no_shared_key_is_compared_whole() -> None:
    changes = diff_documents(
        {"spec": {"endpoints": ["a:1", "b:1"]}}, {"spec": {"endpoints": ["a:1", "c:1"]}}
    )
    assert [change.text for change in changes] == ["spec.endpoints"]


def test_a_duplicated_identifier_falls_back_to_a_whole_list_compare() -> None:
    """Matching by a key that is not unique would silently merge two entries."""
    before = {"ports": [{"name": "a", "x": 1}, {"name": "a", "x": 2}]}
    after = {"ports": [{"name": "a", "x": 1}, {"name": "a", "x": 3}]}
    assert [change.text for change in diff_documents(before, after)] == ["ports"]


def test_the_body_carries_no_identity_keys(home: Path) -> None:
    """``metadata.name`` is the subject of a rename, not a field of one."""
    element = load_tree(home).elements["switches/sw-home"]
    assert "name" not in body_of(element).get("metadata", {})
    assert "apiVersion" not in body_of(element)
    assert document_of(element)["metadata"]["name"] == "sw-home"


def test_empty_containers_are_pruned(home: Path) -> None:
    """Pydantic materialises every optional container; a plan should not show them."""
    element = load_tree(home).elements["switches/sw-home"]
    assert "annotations" not in document_of(element)["metadata"]


# --------------------------------------------------------------------------- #
# The diff, end to end
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["home-lab", "campus", "patch-room", "overlay", "quickstart"])
def test_an_inventory_does_not_differ_from_itself(name: str) -> None:
    """Templates, ranges and defaults all resolve to the same thing twice."""
    inventory = load_tree(EXAMPLES / name)
    plan = diff(inventory, load_tree(EXAMPLES / name))
    assert plan.empty, text_of(plan)
    assert summary_line(plan) == "no changes"


def test_a_reformatted_tree_is_not_a_change(home: Path, twin: Path) -> None:
    """A plan is a diff of meaning; ``netgraph fmt`` must not produce one."""
    runner = CliRunner()
    assert runner.invoke(cli, ["-i", str(twin), "fmt"]).exit_code == 0
    assert plan_of(home, twin).empty


def test_a_changed_field_is_one_update(home: Path, twin: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    plan = plan_of(home, twin)
    change = entry(plan, "device.switches/sw-home")
    assert change.action is Action.UPDATE
    assert field(change, "spec.model").after == "CRS310"
    assert summary_line(plan) == "~ 1 to change"


def test_a_removed_field_carries_its_old_value(home: Path, twin: Path) -> None:
    mutate(twin, UnsetField(address="sw-home", path="spec.model"))
    changed = field(entry(plan_of(home, twin), "device.switches/sw-home"), "spec.model")
    assert changed.removed and changed.after is MISSING


def test_an_interface_field_is_addressed_by_name(home: Path, twin: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.interfaces[2].mtu", value=9000))
    change = entry(plan_of(home, twin), "device.switches/sw-home")
    assert field(change, "spec.interfaces[name=port1].mtu").after == 9000


def test_an_added_interface_is_one_entry(home: Path, twin: Path) -> None:
    mutate(twin, AddInterface(address="sw-home", interface={"name": "port9", "type": "ethernet"}))
    change = entry(plan_of(home, twin), "device.switches/sw-home")
    assert field(change, "spec.interfaces[name=port9]").added


def test_a_created_element_carries_its_document(home: Path, twin: Path) -> None:
    mutate(
        twin,
        CreateElement(
            kind="computer",
            name="printer",
            namespace="hosts",
            spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
        ),
    )
    change = entry(plan_of(home, twin), "device.hosts/printer")
    assert change.action is Action.CREATE
    assert change.document is not None
    assert change.document["metadata"]["name"] == "printer"


def test_a_destroyed_element_carries_what_is_about_to_be_lost(home: Path, twin: Path) -> None:
    mutate(twin, DeleteElement(address="cbl-sw-ap"))
    change = entry(plan_of(home, twin), "cable.cables/cbl-sw-ap")
    assert change.action is Action.DELETE
    assert change.document is not None
    assert change.document["spec"]["label"] == "H-005"
    assert change.source is not None and change.source.startswith("cables/links.yaml")


def test_a_kind_change_is_an_update_not_a_rebuild(home: Path, twin: Path) -> None:
    """All five device kinds share one address, so this is one element changing."""
    document = twin / "hosts" / "pc-desk.yaml"
    document.write_text(
        document.read_text(encoding="utf-8").replace("kind: computer", "kind: server"),
        encoding="utf-8",
    )
    change = entry(plan_of(home, twin), "device.hosts/pc-desk")
    assert change.action is Action.UPDATE
    assert field(change, "kind").after == "server"


def test_apply_says_what_to_do_about_a_kind_change(home: Path, twin: Path) -> None:
    """The edit layer refuses it, so the plan has to explain rather than fail obscurely."""
    document = twin / "hosts" / "pc-desk.yaml"
    document.write_text(
        document.read_text(encoding="utf-8").replace("kind: computer", "kind: server"),
        encoding="utf-8",
    )
    change = entry(plan_of(home, twin), "device.hosts/pc-desk")
    with pytest.raises(PlanExecutionError, match="kind"):
        operations_for(change, EditSession(root=home))


# --------------------------------------------------------------------------- #
# Renames
# --------------------------------------------------------------------------- #


def test_a_rename_is_one_entry(home: Path, twin: Path) -> None:
    mutate(twin, RenameElement(address="srv-nas", new_name="nas-01"))
    plan = plan_of(home, twin)
    change = entry(plan, "device.hosts/srv-nas")
    assert change.action is Action.RENAME
    assert str(change.new_address) == "device.hosts/nas-01"
    assert plan.counts()[Action.DELETE] == 0
    assert plan.counts()[Action.CREATE] == 0


def test_a_rename_plus_a_change_is_two_separable_entries(home: Path, twin: Path) -> None:
    mutate(
        twin,
        RenameElement(address="srv-nas", new_name="nas-01"),
        SetField(address="nas-01", path="spec.model", value="DS1522+"),
    )
    plan = plan_of(home, twin)
    assert entry(plan, "device.hosts/srv-nas").action is Action.RENAME
    update = entry(plan, "device.hosts/nas-01")
    assert update.action is Action.UPDATE
    assert field(update, "spec.model").after == "DS1522+"
    # The rename must run first, so the update has something to land on.
    assert addresses(plan).index("device.hosts/srv-nas") < addresses(plan).index(
        "device.hosts/nas-01"
    )


def test_a_renamed_device_does_not_make_its_cables_look_new(home: Path, twin: Path) -> None:
    """The second pass resolves link ends through the first pass's answers."""
    mutate(twin, RenameElement(address="sw-home", new_name="sw-core"))
    plan = plan_of(home, twin)
    assert plan.counts()[Action.RENAME] == 1
    assert plan.counts()[Action.DELETE] == 0
    assert plan.counts()[Action.CREATE] == 0
    # Every cable changed its endpoint spelling and nothing else.
    for change in plan:
        if change.action is Action.UPDATE:
            assert [item.text for item in change.fields] == ["spec.endpoints"]


def test_hardware_evidence_vetoes_a_weak_match(home: Path, twin: Path) -> None:
    """Two boxes with one port list are still two boxes when the MACs differ."""
    mutate(
        twin,
        DeleteElement(address="phone", cascade=True),
        CreateElement(
            kind="computer",
            name="tablet",
            namespace="hosts",
            spec={"interfaces": [{"name": "en0", "type": "wifi", "mac": "aa:bb:cc:00:00:01"}]},
        ),
    )
    plan = plan_of(home, twin)
    assert plan.counts()[Action.RENAME] == 0


def test_an_explicit_id_beats_every_inference(tmp_path: Path) -> None:
    """Two boxes with nothing in common are still paired when both are pinned."""
    before, after = tmp_path / "a", tmp_path / "b"
    for root, name in ((before, "pp-old"), (after, "pp-new")):
        root.mkdir()
        write_text(
            root / "panels.yaml",
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: patchpanel\n"
            "metadata:\n"
            f"  name: {name}\n"
            "  annotations:\n"
            "    netgraph.dev/id: P-1\n"
            "spec:\n"
            "  ports: 4\n",
        )
    plan = plan_of(before, after)
    assert str(entry(plan, "patchpanel.pp-old").new_address) == "patchpanel.pp-new"


def test_a_relabelled_element_can_be_told_from_a_new_one(home: Path) -> None:
    """A fingerprint is a dict of evidence, strongest first."""
    inventory = load_tree(home)
    keys = fingerprints(
        parse_address("cable.cables/cbl-sw-ap"), inventory.cables["cables/cbl-sw-ap"], inventory
    )
    assert list(keys) == ["ends", "label"]
    assert keys["label"] == ("H-005",)


def test_renames_can_be_turned_off(home: Path, twin: Path) -> None:
    mutate(twin, RenameElement(address="srv-nas", new_name="nas-01"))
    plan = plan_of(home, twin, renames=False)
    assert plan.counts()[Action.RENAME] == 0
    assert plan.counts()[Action.DELETE] == 1
    assert plan.counts()[Action.CREATE] == 1


def test_an_ambiguous_pairing_is_left_alone(tmp_path: Path) -> None:
    """Two indistinguishable panels cannot be told apart, so neither is renamed."""
    before, after = tmp_path / "a", tmp_path / "b"
    for root in (before, after):
        root.mkdir()
    write_text(
        before / "panels.yaml",
        _documents(("patchpanel", "pp-1"), ("patchpanel", "pp-2")),
    )
    write_text(
        after / "panels.yaml",
        _documents(("patchpanel", "pp-a"), ("patchpanel", "pp-b")),
    )
    plan = plan_of(before, after)
    assert plan.counts()[Action.RENAME] == 0
    assert plan.counts()[Action.DELETE] == 2


def _documents(*elements: tuple[str, str]) -> str:
    return "---\n".join(
        "apiVersion: netgraph.dev/v1alpha1\n"
        f"kind: {kind}\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  ports: 4\n"
        for kind, name in elements
    )


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #


def test_a_cable_is_destroyed_before_the_device_it_lands_on(home: Path, twin: Path) -> None:
    mutate(twin, DeleteElement(address="pc-desk", cascade=True))
    order = addresses(plan_of(home, twin))
    assert order.index("cable.cables/cbl-sw-desk") < order.index("device.hosts/pc-desk")


def test_a_device_is_created_before_the_cable_that_lands_on_it(home: Path, twin: Path) -> None:
    mutate(
        twin,
        CreateElement(
            kind="computer",
            name="printer",
            namespace="hosts",
            spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
        ),
        AddInterface(address="sw-home", interface={"name": "port6", "type": "ethernet"}),
        Connect(a="printer:eth0", b="sw-home:port6", name="cbl-printer"),
    )
    order = addresses(plan_of(home, twin))
    cable = next(address for address in order if address.endswith("cbl-printer"))
    assert order.index("device.hosts/printer") < order.index(cable)


def test_every_action_group_runs_in_its_phase(home: Path, twin: Path) -> None:
    mutate(
        twin,
        DeleteElement(address="cbl-sw-ap"),
        RenameElement(address="srv-nas", new_name="nas-01"),
        CreateElement(
            kind="computer",
            name="printer",
            namespace="hosts",
            spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
        ),
        SetField(address="sw-home", path="spec.model", value="CRS310"),
    )
    plan = plan_of(home, twin)
    seen = [change.action for change in plan]
    assert seen.index(Action.DELETE) < seen.index(Action.RENAME) < seen.index(Action.CREATE)
    assert seen[-1] is Action.UPDATE


def test_the_dependency_graph_points_at_what_must_exist_first(home: Path) -> None:
    inventory = load_tree(home)
    graph = dependencies(inventory)
    cable = parse_address("cable.cables/cbl-sw-desk")
    assert parse_address("device.hosts/pc-desk") in graph[cable]
    assert graph[parse_address("device.hosts/pc-desk")] == frozenset()


def test_a_plan_is_deterministic(home: Path, twin: Path) -> None:
    mutate(
        twin,
        DeleteElement(address="cbl-sw-ap"),
        RenameElement(address="srv-nas", new_name="nas-01"),
    )
    assert text_of(plan_of(home, twin)) == text_of(plan_of(home, twin))


# --------------------------------------------------------------------------- #
# The plan file and the state hash
# --------------------------------------------------------------------------- #


def test_a_plan_round_trips_through_its_file(home: Path, twin: Path) -> None:
    mutate(
        twin,
        RenameElement(address="srv-nas", new_name="nas-01"),
        DeleteElement(address="cbl-sw-ap"),
        SetField(address="sw-home", path="spec.interfaces[2].mtu", value=9000),
        CreateElement(
            kind="computer",
            name="printer",
            namespace="hosts",
            spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
        ),
    )
    plan = plan_of(
        home,
        twin,
        source=StateRef(kind="tree", description="a", digest="sha256:x"),
        target=StateRef(kind="folder", description="b"),
    )
    restored = plan_from_dict(json.loads(render_plan(plan, "json")))
    assert restored.changes == plan.changes
    assert restored.source == plan.source
    assert restored.target == plan.target


def test_a_plan_of_another_schema_version_is_refused() -> None:
    with pytest.raises(PlanFormatError):
        plan_from_dict({"schemaVersion": 99, "changes": []})


@pytest.mark.parametrize("payload", ["a string", {"schemaVersion": 1, "changes": "no"}])
def test_a_document_that_is_not_a_plan_is_refused(payload: Any) -> None:
    with pytest.raises(PlanFormatError):
        plan_from_dict(payload)


def test_an_unknown_format_is_refused(home: Path) -> None:
    with pytest.raises(ValueError, match="unknown plan format"):
        render_plan(Plan(), "yaml")


def test_the_state_hash_ignores_formatting(home: Path, twin: Path) -> None:
    before = state_digest(load_tree(home))
    CliRunner().invoke(cli, ["-i", str(twin), "fmt"])
    assert state_digest(load_tree(twin)) == before


def test_the_state_hash_notices_a_field(home: Path) -> None:
    before = state_digest(load_tree(home))
    mutate(home, SetField(address="sw-home", path="spec.model", value="CRS310"))
    assert state_digest(load_tree(home)) != before


def test_targeting_narrows_a_plan(home: Path, twin: Path) -> None:
    mutate(
        twin,
        SetField(address="sw-home", path="spec.model", value="CRS310"),
        SetField(address="pc-desk", path="spec.model", value="XPS"),
    )
    plan = plan_of(home, twin)
    assert len(plan.select(["device.switches/*"])) == 1
    assert len(plan.select(["pc-desk"])) == 1
    assert len(plan.select([])) == 2
    assert len(plan.select(["nothing-*"])) == 0


def test_targeting_a_rename_matches_either_end(home: Path, twin: Path) -> None:
    mutate(twin, RenameElement(address="srv-nas", new_name="nas-01"))
    plan = plan_of(home, twin)
    assert len(plan.select(["nas-01"])) == 1
    assert len(plan.select(["srv-nas"])) == 1


# --------------------------------------------------------------------------- #
# Translation into edit operations
# --------------------------------------------------------------------------- #


def kinds(operations: tuple[Operation, ...]) -> list[str]:
    return [type(operation).__name__ for operation in operations]


def test_each_entry_becomes_the_operation_it_should(home: Path, twin: Path) -> None:
    mutate(
        twin,
        RenameElement(address="srv-nas", new_name="nas-01"),
        DeleteElement(address="cbl-sw-ap"),
        CreateElement(
            kind="computer",
            name="printer",
            namespace="hosts",
            spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
        ),
        SetField(address="sw-home", path="spec.interfaces[2].mtu", value=9000),
        UnsetField(address="sw-home", path="spec.model"),
        AddInterface(address="sw-home", interface={"name": "port6", "type": "ethernet"}),
        RemoveInterface(address="pc-desk", name="wlp1s0"),
    )
    plan = plan_of(home, twin)
    session = EditSession(root=home)
    produced = {str(change.address): kinds(ops) for change, ops in translate(plan, session)}
    assert produced["device.hosts/srv-nas"] == ["RenameElement"]
    assert produced["cable.cables/cbl-sw-ap"] == ["DeleteElement"]
    assert produced["device.hosts/printer"] == ["CreateElement"]
    assert produced["device.hosts/pc-desk"] == ["RemoveInterface"]
    assert set(produced["device.switches/sw-home"]) == {"SetField", "UnsetField", "AddInterface"}


def test_a_selector_is_resolved_against_the_tree_it_is_written_to(home: Path, twin: Path) -> None:
    """The index in the operation is the one the document has, not the plan's."""
    mutate(twin, SetField(address="sw-home", path="spec.interfaces[2].mtu", value=9000))
    plan = plan_of(home, twin)
    session = EditSession(root=home)
    (operation,) = operations_for(entry(plan, "device.switches/sw-home"), session)
    assert isinstance(operation, SetField)
    assert operation.path == "spec.interfaces[2].mtu"


def test_an_entry_about_an_element_that_is_gone_is_refused(home: Path) -> None:
    change = Change(
        action=Action.UPDATE,
        address=parse_address("device.hosts/ghost"),
        kind="computer",
        fields=(FieldChange(path=("spec", "model"), after="x"),),
    )
    with pytest.raises(PlanExecutionError, match="no such element"):
        operations_for(change, EditSession(root=home))


def test_a_create_with_no_document_is_refused(home: Path) -> None:
    change = Change(
        action=Action.CREATE, address=parse_address("device.hosts/ghost"), kind="computer"
    )
    with pytest.raises(PlanExecutionError, match="no document"):
        operations_for(change, EditSession(root=home))


# --------------------------------------------------------------------------- #
# Diagram geometry
# --------------------------------------------------------------------------- #


ARRANGED = Path(__file__).resolve().parent / "fixtures" / "arranged"


@pytest.fixture()
def arranged(tmp_path: Path) -> Path:
    """A writable copy of the hand-arranged fixture, layout document and all."""
    root = tmp_path / "arranged"
    shutil.copytree(ARRANGED, root)
    return root


@pytest.fixture()
def arranged_twin(tmp_path: Path) -> Path:
    root = tmp_path / "arranged-twin"
    shutil.copytree(ARRANGED, root)
    return root


def test_geometry_is_part_of_the_state(arranged: Path, arranged_twin: Path) -> None:
    """A layout is inventory data, so a plan that ignored it would be incomplete."""
    (arranged_twin / "layout.yaml").unlink()
    plan = plan_of(arranged, arranged_twin)
    change = entry(plan, "layout.layout")
    assert change.action is Action.DELETE


def test_a_moved_node_is_a_geometry_update(arranged: Path, arranged_twin: Path) -> None:
    document = arranged_twin / "layout.yaml"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "position: {x: 1176, y: 271}", "position: {x: 1200, y: 300}"
        ),
        encoding="utf-8",
    )
    change = entry(plan_of(arranged, arranged_twin), "layout.layout")
    assert change.action is Action.UPDATE
    # The whole view travels with it: geometry is written a view at a time.
    assert change.document is not None


def test_geometry_is_applied_a_view_at_a_time(arranged: Path, arranged_twin: Path) -> None:
    document = arranged_twin / "layout.yaml"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "position: {x: 1176, y: 271}", "position: {x: 1200, y: 300}"
        ),
        encoding="utf-8",
    )
    plan = plan_of(arranged, arranged_twin)
    session = EditSession(root=arranged)
    (operation,) = operations_for(entry(plan, "layout.layout"), session)
    # Only the view that moved: rewriting the others would put hunks in the diff
    # that the plan never claimed.
    assert type(operation).__name__ == "SetGeometry"
    assert operation.view == "l1"
    assert operation.nodes["hosts/adp-usb-eth"]["position"] == {"x": 1200, "y": 300}


def test_a_geometry_plan_applies_and_converges(
    arranged: Path, arranged_twin: Path, tmp_path: Path
) -> None:
    document = arranged_twin / "layout.yaml"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "position: {x: 1176, y: 271}", "position: {x: 1200, y: 300}"
        ),
        encoding="utf-8",
    )
    out = tmp_path / "geometry.plan"
    assert (
        run("-i", str(arranged), "plan", "--to", str(arranged_twin), "-out", str(out)).exit_code
        == 0
    )
    result = run("-i", str(arranged), "apply", str(out), "--auto-approve")
    assert result.exit_code == 0, result.output
    assert plan_of(arranged, arranged_twin).empty


def test_a_dropped_view_is_cleared(arranged: Path, arranged_twin: Path) -> None:
    document = arranged_twin / "layout.yaml"
    text = document.read_text(encoding="utf-8")
    document.write_text(text[: text.index("    l2:")], encoding="utf-8")
    plan = plan_of(arranged, arranged_twin)
    operations = operations_for(entry(plan, "layout.layout"), EditSession(root=arranged))
    cleared = [operation for operation in operations if operation.clears]  # type: ignore[attr-defined]
    assert [operation.view for operation in cleared] == ["l2"]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Adoption from a capture
# --------------------------------------------------------------------------- #


def adoption_of(root: Path, *inputs: str) -> Inventory:
    draft = build_draft(read_inputs([str(CAPTURES / name) for name in inputs]))
    result = adopt(load_tree(root), draft)
    assert not result.rejected, result.rejected
    return result.inventory


def test_a_capture_adopts_only_what_it_observed(home: Path) -> None:
    plan = diff(load_tree(home), adoption_of(home, "patch.csv"))
    assert {str(change.address) for change in plan} == {
        "device.switches/sw-home",
        "cable.cables/cbl-sw-ap",
        "cable.cables/cbl-sw-nas",
    }


def test_a_partial_capture_never_proposes_a_cull(home: Path) -> None:
    """Nothing the capture could not see may be deleted by adopting it."""
    for capture in ("patch.csv", "sw-home.lldp.json", "pc-desk.addr.json"):
        plan = diff(load_tree(home), adoption_of(home, capture))
        assert plan.counts()[Action.DELETE] == 0, f"{capture}: {text_of(plan)}"


def test_a_repatched_cable_keeps_its_document(home: Path) -> None:
    """Matched on its label, so its length, category and comments survive."""
    change = entry(diff(load_tree(home), adoption_of(home, "patch.csv")), "cable.cables/cbl-sw-nas")
    assert change.action is Action.UPDATE
    assert field(change, "spec.endpoints").after == ["srv-nas:eth0", "sw-home:port6"]


def test_an_unobserved_field_is_left_alone_and_reported(home: Path) -> None:
    draft = build_draft(read_inputs([str(CAPTURES / "patch.csv")]))
    result = adopt(load_tree(home), draft)
    assert any("kept the cable" in note for note in result.unobserved)
    assert any("kept interface" in note for note in result.unobserved)


def test_an_undeclared_device_is_created(home: Path) -> None:
    plan = diff(load_tree(home), adoption_of(home, "sw-home.lldp.json"))
    change = entry(plan, "device.prn-hall")
    assert change.action is Action.CREATE
    assert change.kind == "computer"


def test_an_inferred_trunk_is_merged_not_substituted(home: Path) -> None:
    """No dialect reports a whole VLAN set, so an observation only ever adds."""
    plan = diff(load_tree(home), adoption_of(home, "pc-desk.addr.json"))
    change = entry(plan, "device.hosts/pc-desk")
    vlans = field(change, "spec.interfaces[name=eno1].vlan")
    assert vlans.after["mode"] == "trunk"
    assert "30" in str(vlans.after["trunk_vlans"])


def test_adoption_leaves_the_source_inventory_alone(home: Path) -> None:
    inventory = load_tree(home)
    before = state_digest(inventory)
    adopt(inventory, build_draft(read_inputs([str(CAPTURES / "patch.csv")])))
    assert state_digest(inventory) == before


def synthetic(dialect: str, **interfaces: DraftInterface) -> Draft:
    """A capture of ``srv-nas`` alone, with the coverage ``dialect`` implies."""
    draft = Draft()
    device = draft.device("srv-nas")
    device.interfaces.update(interfaces)
    draft.dialects["capture"] = dialect
    device.sources.append("capture")
    return draft


def adopted(root: Path, draft: Draft) -> Plan:
    result = adopt(load_tree(root), draft)
    assert not result.rejected, result.rejected
    return diff(load_tree(root), result.inventory)


def test_a_complete_address_list_replaces_the_declared_one(home: Path) -> None:
    """``ip -j addr show`` lists every address, so what it omits really is gone."""
    draft = synthetic(
        "iproute", eth0=DraftInterface(name="eth0", ipv4=["192.168.10.99/24"], mtu=1500)
    )
    change = entry(adopted(home, draft), "device.hosts/srv-nas")
    addresses = field(change, "spec.interfaces[name=eth0].ipv4.addresses").after
    assert addresses == [{"ip": "192.168.10.99", "prefix_length": 24}]
    assert field(change, "spec.interfaces[name=eth0].ipv4.addresses").before == [
        {"ip": "192.168.10.10", "prefix_length": 24}
    ]


def test_an_incomplete_capture_only_adds_addresses(home: Path) -> None:
    """``lldp`` vouches for nothing it did not print, so the declared ones stand."""
    draft = synthetic("lldp", eth0=DraftInterface(name="eth0", ipv4=["192.168.10.99/24"]))
    change = entry(adopted(home, draft), "device.hosts/srv-nas")
    addresses = field(change, "spec.interfaces[name=eth0].ipv4.addresses").after
    assert {entry_["ip"] for entry_ in addresses} == {"192.168.10.10", "192.168.10.99"}


def test_an_address_spelt_differently_is_not_a_change(home: Path) -> None:
    """``2001:DB8:10::10/64`` and ``2001:db8:10::10/64`` are one address."""
    draft = synthetic(
        "iproute",
        eth0=DraftInterface(
            name="eth0", ipv4=["192.168.10.10/24"], ipv6=["2001:DB8:10::10/64"], mtu=1500
        ),
    )
    plan = adopted(home, draft)
    assert plan.empty, text_of(plan)


def test_bridge_membership_is_observed_only_where_it_is_complete(home: Path) -> None:
    lldp = synthetic("lldp", br0=DraftInterface(name="br0", type="bridge", members=["eth0"]))
    change = entry(adopted(home, lldp), "device.hosts/srv-nas")
    assert field(change, "spec.interfaces[name=br0]").added


@pytest.mark.parametrize(
    ("declared", "observed", "expected"),
    [("10,20", [30], [10, 20, 30]), ("10-12", [11], None), (None, [30], [30])],
)
def test_a_trunk_vlan_set_is_read_in_every_spelling(
    declared: str | None, observed: list[int], expected: list[int] | None
) -> None:
    """A ``vlan-set`` may be a list, a comma run or a range; a capture is a list."""
    from netgraph.plan.live import _adopt_vlan, _vlan_ids

    entry_: dict[str, Any] = {"name": "eth0"}
    if declared is not None:
        entry_["vlan"] = {"mode": "trunk", "trunk_vlans": declared}
    changed = _adopt_vlan(
        entry_, DraftInterface(name="eth0", vlan=DraftVlan(mode="trunk", trunk_vlans=observed))
    )
    if expected is None:
        assert changed is (declared is None)
    else:
        assert _vlan_ids(entry_["vlan"]["trunk_vlans"]) == expected


def test_a_new_cable_sits_in_the_namespace_both_ends_share(home: Path) -> None:
    from netgraph.plan.live import _common_namespace

    assert _common_namespace("sites/hq/a", "sites/hq/b") == "sites/hq"
    assert _common_namespace("hosts/a", "switches/b") == ""


def test_an_undeclared_device_carries_what_the_capture_said_about_it(home: Path) -> None:
    result = adopt(
        load_tree(home),
        build_draft(read_inputs([str(CAPTURES / "sw-home.lldp.json")])),
    )
    document = document_of(result.inventory.elements["prn-hall"])
    assert document["metadata"]["description"].startswith("Brother")


# --------------------------------------------------------------------------- #
# The commands
# --------------------------------------------------------------------------- #


def run(*args: str, **kwargs: Any) -> Any:
    return CliRunner().invoke(cli, list(args), **kwargs)


def test_plan_needs_something_to_compare_against(home: Path) -> None:
    result = run("-i", str(home), "plan")
    assert result.exit_code == 2
    assert "nothing to compare against" in result.output


def test_plan_refuses_two_desired_sides(home: Path, twin: Path) -> None:
    result = run("-i", str(home), "plan", "--from-live", "--to", str(twin))
    assert result.exit_code == 2


def test_plan_refuses_captures_without_from_live(home: Path) -> None:
    result = run("-i", str(home), "plan", "--to", str(home), str(CAPTURES / "patch.csv"))
    assert result.exit_code == 2
    assert "only read with --from-live" in result.output


def test_plan_prints_the_summary(home: Path, twin: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    result = run("-i", str(home), "plan", "--to", str(twin))
    assert result.exit_code == 0
    assert "Plan: ~ 1 to change." in result.output
    assert "spec.model: TL-SG108E -> CRS310" in result.output


def test_plan_fails_on_changes_when_asked(home: Path, twin: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    assert run("-i", str(home), "plan", "--to", str(twin), "--fail-on", "changes").exit_code == 1
    assert run("-i", str(home), "plan", "--to", str(home), "--fail-on", "changes").exit_code == 0


def test_plan_writes_a_file_that_apply_reads(home: Path, twin: Path, tmp_path: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    out = tmp_path / "change.plan"
    assert run("-i", str(home), "plan", "--to", str(twin), "-out", str(out)).exit_code == 0
    stored = plan_from_dict(json.loads(out.read_text(encoding="utf-8")))
    assert stored.source.digest == state_digest(load_tree(home))

    result = run("-i", str(home), "apply", str(out), "--auto-approve")
    assert result.exit_code == 0, result.output
    assert plan_of(home, twin).empty


def test_apply_refuses_a_plan_made_against_another_state(
    home: Path, twin: Path, tmp_path: Path
) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))
    mutate(home, SetField(address="pc-desk", path="spec.model", value="moved on"))

    result = run("-i", str(home), "apply", str(out), "--auto-approve")
    assert result.exit_code == 1
    assert "made against a different state" in result.output


def test_apply_asks_before_writing(home: Path, twin: Path, tmp_path: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))

    declined = run("-i", str(home), "apply", str(out), input="n\n")
    assert declined.exit_code == 1
    assert "aborted" in declined.output
    assert plan_of(home, twin).empty is False

    accepted = run("-i", str(home), "apply", str(out), input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert plan_of(home, twin).empty


def test_apply_dry_run_writes_nothing(home: Path, twin: Path, tmp_path: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))
    result = run("-i", str(home), "apply", str(out), "--dry-run")
    assert result.exit_code == 0
    assert "+  model: CRS310" in result.output or "+  model: CRS310" in result.output.replace(
        "\r", ""
    )
    assert not plan_of(home, twin).empty


def test_apply_targets_a_subset(home: Path, twin: Path, tmp_path: Path) -> None:
    mutate(
        twin,
        SetField(address="sw-home", path="spec.model", value="CRS310"),
        SetField(address="pc-desk", path="spec.model", value="XPS"),
    )
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))
    result = run("-i", str(home), "apply", str(out), "--target", "sw-home", "--auto-approve")
    assert result.exit_code == 0, result.output
    remaining = plan_of(home, twin)
    assert addresses(remaining) == ["device.hosts/pc-desk"]


def test_apply_preserves_comments(home: Path, twin: Path, tmp_path: Path) -> None:
    """The whole reason it goes through the edit layer."""
    target = home / "switches" / "sw-home.yaml"
    comments = [line for line in target.read_text(encoding="utf-8").splitlines() if "#" in line]
    mutate(twin, SetField(address="sw-home", path="spec.interfaces[2].mtu", value=9000))
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))
    assert run("-i", str(home), "apply", str(out), "--auto-approve").exit_code == 0
    after = target.read_text(encoding="utf-8")
    assert all(comment in after for comment in comments)
    assert "mtu: 9000" in after


def test_apply_refuses_something_that_is_not_a_plan(home: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.plan"
    write_text(broken, "not json at all")
    result = run("-i", str(home), "apply", str(broken))
    assert result.exit_code == 1
    assert "is not a netgraph plan" in result.output


def test_apply_reports_nothing_to_do(home: Path, tmp_path: Path) -> None:
    out = tmp_path / "empty.plan"
    run("-i", str(home), "plan", "--to", str(home), "-out", str(out))
    result = run("-i", str(home), "apply", str(out))
    assert result.exit_code == 0
    assert "nothing to apply" in result.output


def test_plan_refuses_a_tree_that_does_not_load(home: Path, twin: Path) -> None:
    write_text(twin / "broken.yaml", "kind: switch\napiVersion: [\n")
    result = run("-i", str(home), "plan", "--to", str(twin))
    assert result.exit_code == 1
    assert "does not load" in result.output


def test_plan_json_puts_the_document_on_stdout(home: Path, twin: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    result = run("-i", str(home), "plan", "--to", str(twin), "--json")
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["summary"]["update"] == 1
    assert "Plan: ~ 1 to change." in result.stderr


def test_plan_from_live_end_to_end(home: Path) -> None:
    result = run("-i", str(home), "plan", "--from-live", str(CAPTURES / "patch.csv"))
    assert result.exit_code == 0
    assert "cable.cables/cbl-sw-nas" in result.output


def test_plan_lists_what_it_left_alone_when_asked(home: Path) -> None:
    quiet = run("-i", str(home), "plan", "--from-live", str(CAPTURES / "patch.csv"))
    assert "declared items the capture could not vouch for" in quiet.output
    loud = run("-i", str(home), "-v", "plan", "--from-live", str(CAPTURES / "patch.csv"))
    assert "unobserved: cables/cbl-sw-dongle" in loud.output


def test_plan_narrows_an_adoption(home: Path) -> None:
    everything = run("-i", str(home), "plan", "--from-live", str(CAPTURES / "patch.csv"))
    assert "device.switches/sw-home" in everything.output
    narrowed = run(
        "-i",
        str(home),
        "plan",
        "--from-live",
        "--exclude",
        "sw-home",
        str(CAPTURES / "patch.csv"),
    )
    assert "device.switches/sw-home" not in narrowed.output


def test_plan_can_report_a_rename_as_a_rebuild(home: Path, twin: Path) -> None:
    mutate(twin, RenameElement(address="srv-nas", new_name="nas-01"))
    detected = run("-i", str(home), "plan", "--to", str(twin))
    assert "→ 1 to rename" in detected.output
    plain = run("-i", str(home), "plan", "--to", str(twin), "--no-renames")
    assert "to rename" not in plain.output
    assert "1 to add" in plain.output


def test_apply_reports_as_json(home: Path, twin: Path, tmp_path: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))
    result = run("-i", str(home), "apply", str(out), "--auto-approve", "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout[result.stdout.index("{") :])
    assert document["written"] == ["switches/sw-home.yaml"]


def test_apply_refuses_a_plan_with_no_source_state(home: Path, tmp_path: Path) -> None:
    """A plan that cannot say what it was made from cannot be applied safely."""
    out = tmp_path / "anonymous.plan"
    write_text(out, render_plan(Plan(changes=()), "json"))
    result = run("-i", str(home), "apply", str(out))
    assert result.exit_code == 1
    assert "records no source state" in result.output


def test_apply_refuses_a_tree_that_does_not_load(home: Path, twin: Path, tmp_path: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))
    write_text(home / "broken.yaml", "kind: switch\napiVersion: [\n")
    result = run("-i", str(home), "apply", str(out), "--auto-approve")
    assert result.exit_code == 1
    assert "does not load" in result.output


def test_apply_targets_nothing_and_says_so(home: Path, twin: Path, tmp_path: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))
    result = run("-i", str(home), "apply", str(out), "--target", "no-such-thing")
    assert result.exit_code == 0
    assert "selected no change" in result.output


def test_plan_warns_when_a_target_matches_nothing(home: Path, twin: Path) -> None:
    mutate(twin, SetField(address="sw-home", path="spec.model", value="CRS310"))
    result = run("-i", str(home), "plan", "--to", str(twin), "--target", "no-such-thing")
    assert result.exit_code == 0
    assert "matched no change" in result.output


# --------------------------------------------------------------------------- #
# Git refs
# --------------------------------------------------------------------------- #


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def repository(home: Path) -> Path:
    git(home, "init", "-q", ".")
    git(home, "add", "-A")
    git(home, "commit", "-qm", "initial")
    return home


def test_a_git_ref_is_the_other_side_of_the_plan(repository: Path) -> None:
    mutate(repository, SetField(address="sw-home", path="spec.model", value="CRS310"))
    result = run("-i", str(repository), "plan", "--from", "HEAD")
    assert result.exit_code == 0
    assert "spec.model: TL-SG108E -> CRS310" in result.output


def test_a_git_ref_does_not_touch_the_working_tree(repository: Path) -> None:
    before = {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in sorted(repository.rglob("*.yaml"))
    }
    run("-i", str(repository), "plan", "--from", "HEAD")
    after = {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in sorted(repository.rglob("*.yaml"))
    }
    assert after == before


def test_an_unknown_ref_is_reported(repository: Path) -> None:
    result = run("-i", str(repository), "plan", "--from", "no-such-ref")
    assert result.exit_code == 1
    assert "git cannot read" in result.output


def test_a_tree_outside_a_repository_cannot_use_a_ref(tmp_path: Path, home: Path) -> None:
    """``git rev-parse`` answers for the whole filesystem, so this may still be
    inside somebody's repository; either refusal is the right one."""
    with pytest.raises(PlanSourceError), git_ref(home, "definitely-not-a-ref"):
        pass  # pragma: no cover - git_ref raises before it yields


# --------------------------------------------------------------------------- #
# Golden plans
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    """One golden: an example, a mutation of it, and the plan between them."""

    name: str
    example: str
    operations: tuple[Operation, ...] = ()
    capture: str | None = None


CASES: tuple[Case, ...] = (
    Case(name="home-lab-unchanged", example="home-lab"),
    Case(
        name="home-lab-edited",
        example="home-lab",
        operations=(
            RenameElement(address="srv-nas", new_name="nas-01"),
            SetField(address="nas-01", path="spec.model", value="DS923+"),
            DeleteElement(address="cbl-sw-ap"),
            AddInterface(address="sw-home", interface={"name": "port6", "type": "ethernet"}),
            CreateElement(
                kind="computer",
                name="printer",
                namespace="hosts",
                metadata={"description": "Laser printer in the study"},
                spec={"vendor": "Brother", "interfaces": [{"name": "eth0", "type": "ethernet"}]},
            ),
            Connect(a="printer:eth0", b="sw-home:port6", name="cbl-sw-printer"),
        ),
    ),
    Case(name="home-lab-live", example="home-lab", capture="patch.csv"),
    Case(
        name="patch-room-edited",
        example="patch-room",
        operations=(
            SetField(address="sw-core-01", path="spec.interfaces[2].mtu", value=9000),
            UnsetField(address="rtr-edge-01", path="spec.model"),
            DeleteElement(address="cam-lobby-01", cascade=True),
        ),
    ),
    Case(
        name="campus-templated",
        example="campus",
        operations=(
            # A device that inherits from a template: the diff is of the merged
            # element, so only the field that moved shows up.
            SetField(
                address="sites/north/access/sw-north-acc-01",
                path="spec.vendor",
                value="Arista",
            ),
        ),
    ),
)


def golden_plan(case: Case, tmp_path: Path) -> Plan:
    source = tmp_path / "source"
    shutil.copytree(EXAMPLES / case.example, source)
    if case.capture is not None:
        return diff(
            load_tree(source),
            adoption_of(source, case.capture),
            source=StateRef(kind="tree", description=case.example),
            target=StateRef(kind="live", description=f"the live network ({case.capture})"),
        )
    target = tmp_path / "target"
    shutil.copytree(EXAMPLES / case.example, target)
    if case.operations:
        mutate(target, *case.operations)
    return diff(
        load_tree(source),
        load_tree(target),
        source=StateRef(kind="tree", description=case.example),
        target=StateRef(
            kind="folder",
            description=f"{case.example} (edited)" if case.operations else case.example,
        ),
    )


EDITS: tuple[Operation, ...] = (
    RenameElement(address="srv-nas", new_name="nas-01"),
    SetField(address="nas-01", path="spec.model", value="DS1522+"),
    UnsetField(address="nas-01", path="spec.vendor"),
    DeleteElement(address="cbl-sw-ap"),
    AddInterface(address="sw-home", interface={"name": "port6", "type": "ethernet"}),
    RemoveInterface(address="pc-desk", name="wlp1s0"),
    CreateElement(
        kind="computer",
        name="printer",
        namespace="hosts",
        spec={"interfaces": [{"name": "eth0", "type": "ethernet"}]},
    ),
    Connect(a="printer:eth0", b="sw-home:port6", name="cbl-sw-printer"),
)


def test_an_address_list_is_written_whole(home: Path, twin: Path, tmp_path: Path) -> None:
    """A list with no entry-level operation is compared whole, so it stays writable."""
    mutate(
        twin,
        SetField(
            address="srv-nas",
            path="spec.interfaces[1].ipv4.addresses",
            value=["192.168.10.10/24", "192.168.10.11/24"],
        ),
    )
    plan = plan_of(home, twin)
    changed = field(
        entry(plan, "device.hosts/srv-nas"), "spec.interfaces[name=eth0].ipv4.addresses"
    )
    assert len(changed.after) == 2
    out = tmp_path / "change.plan"
    run("-i", str(home), "plan", "--to", str(twin), "-out", str(out))
    assert run("-i", str(home), "apply", str(out), "--auto-approve").exit_code == 0
    assert plan_of(home, twin).empty


def test_applying_a_plan_makes_the_next_one_empty(home: Path, twin: Path, tmp_path: Path) -> None:
    """The round trip: every action, executed, converges in one pass."""
    mutate(twin, *EDITS)
    out = tmp_path / "change.plan"
    assert run("-i", str(home), "plan", "--to", str(twin), "-out", str(out)).exit_code == 0
    result = run("-i", str(home), "apply", str(out), "--auto-approve")
    assert result.exit_code == 0, result.output
    remaining = plan_of(home, twin)
    assert remaining.empty, text_of(remaining)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_the_printed_plan_matches_its_golden(
    case: Case, tmp_path: Path, regen_golden: bool
) -> None:
    """Byte-for-byte stability of the text form, on the example inventories."""
    produced = text_of(golden_plan(case, tmp_path))
    path = GOLDEN_DIR / f"{case.name}.txt"
    if regen_golden:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        write_text(path, produced)
        return
    assert path.exists(), f"missing golden {path}; run pytest --regen-golden"
    assert produced == path.read_text(encoding="utf-8")
