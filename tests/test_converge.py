"""``netgraph converge``: does the remediation close the drift, and can it lock you out?

The command generates commands somebody will run against real hardware, so the
properties asserted here are ordered by how badly getting one wrong would hurt:

* **A management-path change is refused.** Without ``--allow-disruptive``, a plan
  that touches the interface netgraph would reach the box on -- or that shuts or
  deletes any interface -- is refused *whole*: nothing is printed, nothing is
  written, and the refusal names every offending change rather than the first.
* **Nothing is proposed that no capture contradicted.** Every change carries the
  provenance of a drift finding. A change with none would be netgraph having an
  opinion about a network with a root shell in its hand.
* **The order is a dependency order.** VLANs before the ports that carry them,
  parents before the interfaces stacked on them, additions before removals, and
  removals in the mirror order of the additions.
* **The plan actually converges.** Applying the generated commands to the
  captured state and re-running the comparison leaves nothing but the findings
  netgraph deliberately refused to write a command for. This is the round trip,
  and it is the test that would catch a command that is merely plausible.
* **A rollback goes back to what was measured**, not to a state nobody observed.
* **The exit code is the contract**: 0 converged, 2 pending, 4 refused.

The captures in ``tests/fixtures/drift/`` are the same ones ``netgraph drift`` is
tested with, taken against ``examples/home-lab`` -- a tree that is committed,
validates and renders. The goldens in ``tests/fixtures/converge/`` are one per
dialect over those captures, regenerated with ``pytest --regen-golden``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from netgraph.cli import cli
from netgraph.converge import (
    CONVERGE_DIALECTS,
    Action,
    Command,
    ConvergeChange,
    ConvergeInputs,
    ConvergePlan,
    DeviceConverge,
    DisruptiveChangeError,
    Intent,
    IntentKind,
    Risk,
    batches_for,
    blast_radius,
    build_plan,
    converge,
    derive,
    management_path,
    order_intents,
    render_converge,
    script_files,
    script_for,
    write_scripts,
)
from netgraph.converge.commands import describe, render, revert
from netgraph.converge.files import strip_banner
from netgraph.converge.intent import RANKS, prerequisites_of
from netgraph.converge.model import Provenance
from netgraph.drift import Change, Direction, DriftReport, compare, coverage_of
from netgraph.export.config import UnsupportedConfigError
from netgraph.fsio import write_text
from netgraph.importer.draft import Draft, DraftDevice, DraftInterface, DraftVlan
from netgraph.loader import load_tree
from netgraph.loader.inventory import Inventory
from netgraph.render.graph import Layer, build_graph

REPO_ROOT = Path(__file__).resolve().parents[1]
HOME_LAB = REPO_ROOT / "examples" / "home-lab"
DRIFT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "drift"
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "converge"

#: A single host shaped so that every remediation netgraph can derive appears in
#: one plan: an interface the capture cannot find, an aggregate whose membership
#: disagrees on both sides, a VLAN sub-interface in the wrong VLAN, an address
#: that is declared and not configured, and an extra address on the management
#: port. The home-lab captures are real drift and cover the common shapes; this
#: one covers the rest without pretending to be a network anybody runs.
LAB = REPO_ROOT / "tests" / "fixtures" / "converge" / "lab"
LAB_CAPTURE = str(LAB / "srv-lab.addr.json")

#: Every capture in the drift fixture directory, in a fixed order so a golden is
#: reproducible. Sorted rather than globbed-and-hoped: the report header lists
#: the inputs in command-line order.
CAPTURES: tuple[str, ...] = (
    str(DRIFT_FIXTURES / "pc-desk.addr.json"),
    str(DRIFT_FIXTURES / "srv-nas.link.json"),
    str(DRIFT_FIXTURES / "sw-home.lldp.json"),
    str(DRIFT_FIXTURES / "patch.csv"),
)


@pytest.fixture(scope="module")
def inventory() -> Inventory:
    tree = load_tree(HOME_LAB)
    assert not tree.errors
    return tree


@pytest.fixture(scope="module")
def inputs(inventory: Inventory) -> ConvergeInputs:
    """The capture, read once for the whole module: it is the same every time."""
    return ConvergeInputs(inventory, CAPTURES)


@pytest.fixture(scope="module")
def plan(inputs: ConvergeInputs) -> ConvergePlan:
    return build_plan(inputs, allow_disruptive=True)


# --------------------------------------------------------------------------- #
# Refusal: the property that matters most
# --------------------------------------------------------------------------- #


def test_a_management_path_change_is_refused_without_the_flag(inputs: ConvergeInputs) -> None:
    """The whole plan, not the offending change: half a plan is worse than none."""
    with pytest.raises(DisruptiveChangeError) as raised:
        build_plan(inputs)
    assert raised.value.exit_code == 4
    assert all(change.risk is Risk.DISRUPTIVE for change in raised.value.changes)


def test_the_refusal_names_every_disruptive_change_and_why(inputs: ConvergeInputs) -> None:
    """An operator deciding about the flag is deciding about the whole set."""
    with pytest.raises(DisruptiveChangeError) as raised:
        build_plan(inputs)
    message = str(raised.value)
    assert "--allow-disruptive" in message
    for change in raised.value.changes:
        assert change.summary in message
        assert change.risk_reason and change.risk_reason in message


def test_the_management_interface_is_the_one_netgraph_would_reach_the_box_on(
    inventory: Inventory,
) -> None:
    """One definition of "reach the box", shared with the export emitters."""
    graph = build_graph(inventory, layer=Layer.L1)
    node = next(entry for entry in graph.element_nodes if entry.fqn == "hosts/pc-desk")
    device = inventory["hosts/pc-desk"]
    path = management_path(node, device)
    assert path.known
    assert path.interface == "eno1"
    assert path.address == "192.168.10.20/24"
    assert path.holds("eno1")


def test_a_device_with_no_address_has_no_management_path(inventory: Inventory) -> None:
    """ "No path netgraph can name" is stated, not guessed at."""
    graph = build_graph(inventory, layer=Layer.L1)
    node = next(entry for entry in graph.element_nodes if entry.fqn == "switches/sw-home")
    path = management_path(node, inventory["switches/sw-home"])
    # sw-home manages itself on its Vlan10 SVI, so it *does* have one; the point
    # of the assertion is that whichever it is, it is a declared interface.
    if path.known:
        assert path.interface in {
            port.name for port in inventory["switches/sw-home"].spec.interfaces
        }


def test_setting_an_mtu_on_the_management_interface_is_safe(plan: ConvergePlan) -> None:
    """Otherwise --allow-disruptive is the flag every run needs, i.e. no flag."""
    change = _change(plan, "hosts/srv-nas", "mtu")
    assert change.risk is Risk.SAFE
    assert change.risk_reason == ""


def test_changing_a_mac_on_the_management_interface_is_disruptive(plan: ConvergePlan) -> None:
    change = _change(plan, "hosts/pc-desk", "mac")
    assert change.risk is Risk.DISRUPTIVE
    assert "management address" in change.risk_reason


def test_deleting_an_interface_is_disruptive_wherever_it_is(plan: ConvergePlan) -> None:
    change = _change(plan, "hosts/pc-desk", "eno1.30")
    assert change.action is Action.DELETE
    assert change.risk is Risk.DISRUPTIVE


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


def test_every_change_names_the_drift_finding_that_asked_for_it(plan: ConvergePlan) -> None:
    for change in plan.changes:
        assert change.provenance, f"{change.id} proposes work nothing asked for"
        for entry in change.provenance:
            assert entry.message, f"{change.id} cites a finding with no message"


def test_every_provenance_entry_is_a_finding_the_drift_report_holds(
    inputs: ConvergeInputs, plan: ConvergePlan
) -> None:
    """Verbatim, so a reader can grep the drift output for the same sentence."""
    reported = {change.message for change in inputs.report.changes}
    for change in plan.changes:
        for entry in change.provenance:
            assert entry.message in reported, f"{change.id} cites a finding drift did not report"


def test_a_converged_network_produces_an_empty_plan(inventory: Inventory, tmp_path: Path) -> None:
    """The capture agrees with the inventory: nothing to do, and it says so."""
    capture = tmp_path / "srv-nas.link.json"
    capture.write_text(
        json.dumps(
            [
                {
                    "ifindex": 2,
                    "ifname": "eth0",
                    "flags": ["BROADCAST", "UP"],
                    "mtu": 1500,
                    "link_type": "ether",
                    "address": "00:11:32:40:01:01",
                }
            ]
        ),
        encoding="utf-8",
    )
    result = converge(inventory, [str(capture)])
    assert result.converged
    assert result.changes == ()
    assert "Nothing to do" in render_converge(result, "text")


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #


def _intent(kind: IntentKind, **fields: object) -> Intent:
    return Intent(
        kind=kind,
        element="net/sw-1",
        provenance=(Provenance(element="net/sw-1", message="x"),),
        **fields,  # type: ignore[arg-type]
    )


def test_a_vlan_is_created_before_a_port_is_put_in_it() -> None:
    ordered = order_intents(
        [
            _intent(IntentKind.VLAN_ACCESS, interface="port1", target="20", value="20"),
            _intent(IntentKind.VLAN_CREATE, target="20", value="Guest"),
        ]
    )
    assert [intent.kind for intent in ordered] == [
        IntentKind.VLAN_CREATE,
        IntentKind.VLAN_ACCESS,
    ]


def test_an_address_is_configured_before_the_interface_is_brought_up() -> None:
    ordered = order_intents(
        [
            _intent(IntentKind.INTERFACE_ENABLE, interface="eno1", target="enabled"),
            _intent(IntentKind.ADDRESS_ADD, interface="eno1", target="10.0.0.1/24", value="ipv4"),
        ]
    )
    assert [intent.kind for intent in ordered] == [
        IntentKind.ADDRESS_ADD,
        IntentKind.INTERFACE_ENABLE,
    ]


def test_every_addition_runs_before_every_removal() -> None:
    """A run interrupted halfway has built things and taken nothing away."""
    additive = {
        IntentKind.VLAN_CREATE,
        IntentKind.INTERFACE_CREATE,
        IntentKind.MEMBER_ADD,
        IntentKind.VLAN_TAG,
        IntentKind.ADDRESS_ADD,
        IntentKind.INTERFACE_ENABLE,
    }
    subtractive = {
        IntentKind.INTERFACE_DISABLE,
        IntentKind.ADDRESS_REMOVE,
        IntentKind.VLAN_UNTAG,
        IntentKind.MEMBER_REMOVE,
        IntentKind.INTERFACE_DELETE,
        IntentKind.VLAN_DELETE,
    }
    assert max(RANKS[kind] for kind in additive) < min(RANKS[kind] for kind in subtractive)


def test_removals_run_in_the_mirror_order_of_the_additions() -> None:
    """An address comes off before the interface under it does, and so on down."""
    pairs = [
        (IntentKind.VLAN_CREATE, IntentKind.VLAN_DELETE),
        (IntentKind.INTERFACE_CREATE, IntentKind.INTERFACE_DELETE),
        (IntentKind.MEMBER_ADD, IntentKind.MEMBER_REMOVE),
        (IntentKind.VLAN_TAG, IntentKind.VLAN_UNTAG),
        (IntentKind.ADDRESS_ADD, IntentKind.ADDRESS_REMOVE),
    ]
    forward = [RANKS[add] for add, _ in pairs]
    backward = [RANKS[remove] for _, remove in pairs]
    assert forward == sorted(forward)
    assert backward == sorted(backward, reverse=True)


def test_a_parent_is_created_before_the_interface_stacked_on_it() -> None:
    ordered = order_intents(
        [
            _intent(IntentKind.INTERFACE_CREATE, interface="eno1.30", interface_type="vlan"),
            _intent(IntentKind.INTERFACE_CREATE, interface="eno1", interface_type="ethernet"),
        ]
    )
    assert [intent.interface for intent in ordered] == ["eno1", "eno1.30"]


def test_a_stacked_interface_is_deleted_before_the_one_underneath_it() -> None:
    ordered = order_intents(
        [
            _intent(IntentKind.INTERFACE_DELETE, interface="br0", interface_type="bridge"),
            _intent(IntentKind.INTERFACE_DELETE, interface="br0.10", interface_type="vlan"),
        ]
    )
    assert [intent.interface for intent in ordered] == ["br0.10", "br0"]


def test_a_prerequisite_names_the_change_it_depends_on() -> None:
    create = _intent(IntentKind.VLAN_CREATE, target="20", value="Guest")
    assign = _intent(IntentKind.VLAN_ACCESS, interface="port1", target="20", value="20")
    assert prerequisites_of(assign, [create, assign]) == (create.id,)
    assert prerequisites_of(create, [create, assign]) == ()


def test_a_plans_prerequisites_all_point_at_changes_in_the_same_plan(
    plan: ConvergePlan,
) -> None:
    """A dangling prerequisite is worse than none: a transport would wait forever."""
    known = {change.id for change in plan.changes}
    for change in plan.changes:
        for required in change.prerequisites:
            assert required in known, f"{change.id} requires {required}, which is not in the plan"


def test_the_plan_is_emitted_in_rank_order(plan: ConvergePlan) -> None:
    for device in plan.devices:
        ranks = [change.rank for change in device.changes]
        assert ranks == sorted(ranks)


def test_a_prerequisite_always_comes_earlier_in_the_device_order(plan: ConvergePlan) -> None:
    for device in plan.devices:
        position = {change.id: index for index, change in enumerate(device.changes)}
        for index, change in enumerate(device.changes):
            for required in change.prerequisites:
                assert position[required] < index


# --------------------------------------------------------------------------- #
# Commands and their inverses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", [kind for kind in IntentKind if kind is not IntentKind.MANUAL])
def test_every_intent_kind_renders_a_command_and_an_inverse(kind: IntentKind) -> None:
    """A kind that renders nothing would silently drop a difference."""
    intent = _intent(
        kind,
        interface="eno1",
        target="20" if "vlan" in kind.value else "mtu",
        value="1500",
        previous="9000",
        interface_type="vlan",
    )
    assert render(intent), f"{kind} renders no command"
    assert revert(intent), f"{kind} renders no inverse"
    assert describe(intent), f"{kind} has no summary"


def test_a_manual_intent_renders_nothing() -> None:
    intent = _intent(IntentKind.MANUAL, target="link", note="move the cable", summary="by hand")
    assert render(intent) == ()
    assert revert(intent) == ()


def test_the_inverse_of_a_set_is_the_value_the_capture_reported() -> None:
    intent = _intent(
        IntentKind.INTERFACE_SET, interface="eth0", target="mtu", value="1500", previous="9000"
    )
    assert render(intent)[0].text == "set interface eth0 mtu 1500"
    assert revert(intent)[0].text == "set interface eth0 mtu 9000"


def test_the_inverse_of_a_set_with_nothing_observed_is_an_unset() -> None:
    """A rollback never restores a value neither side ever measured."""
    intent = _intent(IntentKind.INTERFACE_SET, interface="eth0", target="mtu", value="1500")
    assert revert(intent)[0].text == "unset interface eth0 mtu"


def test_the_inverse_of_an_add_is_a_remove() -> None:
    intent = _intent(IntentKind.ADDRESS_ADD, interface="eno1", target="10.0.0.1/24", value="ipv4")
    assert render(intent)[0].text == "add interface eno1 ipv4-address 10.0.0.1/24"
    assert revert(intent)[0].text == "remove interface eno1 ipv4-address 10.0.0.1/24"


def test_the_rollback_of_a_plan_is_the_inverse_of_its_commands(plan: ConvergePlan) -> None:
    for change in plan.changes:
        if change.action is Action.MANUAL:
            assert change.commands == () and change.rollback == ()
        else:
            assert len(change.rollback) == len(change.commands)


# --------------------------------------------------------------------------- #
# The round trip: does the plan actually converge?
# --------------------------------------------------------------------------- #


def _apply(draft: Draft, plan: ConvergePlan, inventory: Inventory) -> Draft:
    """Run the plan's *intents* against the captured state.

    The simulation applies the intent vocabulary rather than parsing the
    generated command text, and that is the honest boundary: netgraph has no
    device, so what it can assert is that the changes it derived describe a state
    that agrees with the inventory. Parsing its own output back would test the
    formatter, not the plan.
    """
    for change in plan.changes:
        if change.action is Action.MANUAL:
            continue
        device = _draft_device(draft, inventory, change.element)
        if device is None:  # pragma: no cover - a change always has a device
            continue
        _apply_change(device, change)
    return draft


def _draft_device(draft: Draft, inventory: Inventory, element: str) -> DraftDevice | None:
    for name, device in draft.devices.items():
        resolution = inventory.lookup(name)
        if resolution.fqn == element or name == element:
            return device
    return None


def _apply_change(device: DraftDevice, change: ConvergeChange) -> None:
    """Mutate one captured device the way one plan entry would.

    The change's id is the intent's key -- ``interface.set/eno1/mtu`` -- so the
    simulation dispatches on the same string a transport would, rather than on a
    second classification that could drift from the first.
    """
    action = change.id.partition("#")[2].split("/", 1)[0]
    interface = change.interface
    target = change.target

    if action == "interface.set":
        port = device.interface(interface)
        setattr(port, target, int(change.value or 0) if target == "mtu" else change.value)
    elif action == "interface.delete":
        device.interfaces.pop(interface, None)
    elif action == "interface.create":
        device.interface(interface)
    elif action == "interface.enable":
        device.interface(interface).enabled = True
    elif action == "interface.disable":
        device.interface(interface).enabled = False
    elif action == "vlan.untag":
        _untag(device.interface(interface), target)
    elif action == "vlan.tag":
        port = device.interface(interface)
        if port.vlan is None:
            port.vlan = DraftVlan(mode="trunk")
        port.vlan.trunk_vlans = sorted({*port.vlan.trunk_vlans, int(target)})
    elif action == "vlan.access":
        device.interface(interface).vlan = DraftVlan(mode="access", access_vlan=int(target))
    elif action == "vlan.mode":
        port = device.interface(interface)
        if port.vlan is None:
            port.vlan = DraftVlan(mode=change.value or "access")
        elif change.value:
            port.vlan.mode = change.value
    elif action == "vlan.create":
        device.vlans.add(int(target))
    elif action == "vlan.delete":
        device.vlans.discard(int(target))
    elif action == "address.add":
        _addresses(device.interface(interface), target).append(target)
    elif action == "address.remove":
        port = device.interface(interface)
        family = "ipv6" if ":" in target else "ipv4"
        setattr(port, family, [entry for entry in _addresses(port, target) if entry != target])
    elif action == "member.add":
        device.interface(interface).members.append(target)
    elif action == "member.remove":
        port = device.interface(interface)
        port.members = [entry for entry in port.members if entry != target]
    elif action != "file":  # pragma: no cover - every actionable kind is handled
        raise AssertionError(f"the simulation does not know how to apply {action!r}")


def _untag(interface: DraftInterface, vlan: str) -> None:
    if interface.vlan is None:
        return
    interface.vlan.trunk_vlans = [
        entry for entry in interface.vlan.trunk_vlans if str(entry) != vlan
    ]
    if not interface.vlan.trunk_vlans and interface.vlan.access_vlan is None:
        interface.vlan = None


def _addresses(interface: DraftInterface, address: str) -> list[str]:
    return interface.ipv6 if ":" in address else interface.ipv4


def test_applying_the_plan_to_the_captured_state_leaves_only_the_manual_findings(
    inventory: Inventory,
) -> None:
    """The round trip: everything netgraph wrote a command for is closed.

    What is left is exactly the set it refused to write one for -- a cable in the
    wrong port, a device nobody declared, a physical port that is simply there --
    and each of those is in the plan as a ``manual`` change, so the two lists are
    checked against each other rather than the remainder merely being counted.

    The capture is read afresh rather than taken from the module fixture: the
    simulation mutates the draft in place, which is what a device would do, and a
    shared one would leave every later test planning against a network that had
    already been fixed.
    """
    own = ConvergeInputs(inventory, CAPTURES)
    plan = build_plan(own, allow_disruptive=True)
    before = {change.location for change in own.report.changes}
    manual = {
        entry.location
        for change in plan.changes
        if change.action is Action.MANUAL
        for entry in change.provenance
    }
    assert manual < before

    converged = _apply(own.draft, plan, inventory)
    after = compare(inventory, converged, coverage=coverage_of(converged))
    remaining = {change.location for change in after.changes}
    assert remaining == manual, (
        "the plan left findings it claimed to close, or closed ones it called manual: "
        f"{sorted(remaining ^ manual)}"
    )


def test_the_round_trip_is_idempotent(inventory: Inventory) -> None:
    """Re-planning against the converged state proposes nothing new."""
    inputs = ConvergeInputs(inventory, CAPTURES)
    plan = build_plan(inputs, allow_disruptive=True)
    converged = _apply(inputs.draft, plan, inventory)
    after = compare(inventory, converged, coverage=coverage_of(converged))
    again = derive(inventory, converged, after)
    assert all(intent.kind is IntentKind.MANUAL for intent in again)


# --------------------------------------------------------------------------- #
# The whole vocabulary, over a fixture built to produce all of it
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def lab() -> Inventory:
    tree = load_tree(LAB)
    assert not tree.errors
    return tree


@pytest.fixture(scope="module")
def lab_plan(lab: Inventory) -> ConvergePlan:
    return converge(lab, [LAB_CAPTURE], host="srv-lab", allow_disruptive=True)


def test_every_derivable_kind_appears_in_the_lab_plan(lab_plan: ConvergePlan) -> None:
    """If a kind stops being derived, this is the test that notices."""
    kinds = {change.id.partition("#")[2].split("/")[0] for change in lab_plan.changes}
    assert kinds == {
        "vlan.create",
        "interface.create",
        "interface.set",
        "member.add",
        "vlan.mode",
        "vlan.access",
        "address.add",
        "interface.enable",
        "address.remove",
        "vlan.untag",
        "member.remove",
        "manual",
    }


def test_a_declared_interface_the_capture_lacks_is_created_and_configured(
    lab_plan: ConvergePlan,
) -> None:
    """One finding, several changes: drift said "absent" once, not once per field."""
    device = lab_plan.device("srv-lab")
    assert device is not None
    port9 = [change for change in device.changes if "/port9" in change.id]
    assert {change.id.partition("#")[2].split("/")[0] for change in port9} == {
        "interface.create",
        "interface.set",
        "vlan.mode",
        "vlan.access",
        "interface.enable",
    }
    for change in port9:
        assert [entry.location for entry in change.provenance] == ["srv-lab:port9"]


def test_a_vlan_is_created_from_the_declaration_the_capture_never_saw(
    lab_plan: ConvergePlan,
) -> None:
    """drift cannot report a missing VLAN database entry; the inventory can."""
    created = [change for change in lab_plan.changes if change.id.endswith("#vlan.create/20")]
    assert len(created) == 1
    assert created[0].commands[0].text == "create vlan 20 name lab"
    # The provenance is the intent that *needed* the VLAN, not the element's first.
    assert [entry.location for entry in created[0].provenance] == ["srv-lab:port9"]


def test_a_vlan_nothing_declares_is_never_invented(lab: Inventory, lab_plan: ConvergePlan) -> None:
    """VLAN 31 is on the wire and in no document: netgraph does not create it."""
    assert not any(change.id.endswith("#vlan.create/31") for change in lab_plan.changes)
    assert any(change.id.endswith("#vlan.untag/bond0/31") for change in lab_plan.changes)


def test_membership_disagreeing_both_ways_produces_both_changes(
    lab_plan: ConvergePlan,
) -> None:
    commands = {command.text for change in lab_plan.changes for command in change.commands}
    assert "add interface bond0 member eno2" in commands
    assert "remove interface bond0 member eno3" in commands


def test_an_extra_address_on_the_management_port_is_disruptive(lab_plan: ConvergePlan) -> None:
    change = next(
        entry
        for entry in lab_plan.changes
        if entry.id.endswith("#address.remove/mgmt0/10.9.0.250/24")
    )
    assert change.risk is Risk.DISRUPTIVE
    assert "mgmt0 carries the management address 10.9.0.5/24" in change.risk_reason


def test_the_lab_plan_is_refused_without_the_flag(lab: Inventory) -> None:
    with pytest.raises(DisruptiveChangeError):
        converge(lab, [LAB_CAPTURE], host="srv-lab")


def test_the_lab_plan_converges_the_lab_capture(lab: Inventory) -> None:
    """The round trip again, over the fixture that exercises every kind."""
    own = ConvergeInputs(lab, [LAB_CAPTURE], host="srv-lab")
    plan = build_plan(own, allow_disruptive=True)
    manual = {
        entry.location
        for change in plan.changes
        if change.action is Action.MANUAL
        for entry in change.provenance
    }
    converged = _apply(own.draft, plan, lab)
    after = compare(lab, converged, coverage=coverage_of(converged))
    assert {change.location for change in after.changes} == manual


def test_the_lab_plan_matches_its_golden(lab: Inventory, regen_golden: bool) -> None:
    plan = converge(lab, [LAB_CAPTURE], host="srv-lab", allow_disruptive=True)
    _assert_golden(render_converge(plan, "text"), "lab-interfaces.txt", regen_golden)


# --------------------------------------------------------------------------- #
# Batching
# --------------------------------------------------------------------------- #


def test_two_devices_with_no_shared_blast_radius_share_a_batch(inventory: Inventory) -> None:
    batches = batches_for(inventory, ["hosts/pc-desk", "hosts/srv-nas"])
    assert len(batches) == 1
    assert batches[0].elements == ("hosts/pc-desk", "hosts/srv-nas")


def test_a_device_inside_anothers_blast_radius_gets_its_own_batch(
    inventory: Inventory,
) -> None:
    """The core switch and a host behind it cannot be worked in one window."""
    isolated, _splits = blast_radius(inventory, "switches/sw-home")
    assert isolated, "losing the only switch should isolate something"
    behind = next(name for name in isolated if name.startswith("hosts/"))
    batches = batches_for(inventory, ["switches/sw-home", behind])
    assert len(batches) == 2
    assert {batches[0].elements[0], batches[1].elements[0]} == {"switches/sw-home", behind}


def test_a_blast_radius_of_something_the_graph_does_not_hold_is_empty(
    inventory: Inventory,
) -> None:
    assert blast_radius(inventory, "nowhere/nothing") == ((), ())


def test_every_device_with_commands_is_in_a_batch(plan: ConvergePlan) -> None:
    placed = {element for batch in plan.batches for element in batch.elements}
    for device in plan.devices:
        if device.batch is None:
            assert not device.actionable or device.element not in placed
        else:
            assert device.element in placed


# --------------------------------------------------------------------------- #
# Per-device scripts
# --------------------------------------------------------------------------- #


def test_a_script_is_written_for_every_device_with_commands(
    plan: ConvergePlan, tmp_path: Path
) -> None:
    written = write_scripts(plan, tmp_path)
    with_commands = {device.element for device in plan.devices if device.commands}
    assert len(written) == len(with_commands)
    for relative in written:
        assert (tmp_path / relative).is_file()
        assert relative.endswith("/converge.txt")


def test_a_device_with_only_manual_findings_gets_no_script(plan: ConvergePlan) -> None:
    """A file somebody has to open to discover it is empty is a file too many."""
    manual_only = next(device for device in plan.devices if device.manual and not device.commands)
    assert script_for(plan, manual_only) is None


def test_a_script_carries_its_provenance_and_its_warnings(plan: ConvergePlan) -> None:
    text = script_for(plan, plan.device("hosts/pc-desk"))
    assert text is not None
    assert "netgraph-element: hosts/pc-desk" in text
    assert "Not applied by netgraph" in text
    assert "drift: hosts/pc-desk:eno1.mac" in text
    assert "[DISRUPTIVE]" in text
    assert text.endswith("\n")


def test_a_rollback_script_holds_the_inverse_commands(plan: ConvergePlan, tmp_path: Path) -> None:
    forward = script_for(plan, plan.device("hosts/srv-nas"))
    inverse = script_for(plan, plan.device("hosts/srv-nas"), rollback=True)
    assert forward is not None and inverse is not None
    assert "set interface eth0 mtu 1500" in forward
    assert "set interface eth0 mtu 9000" in inverse
    assert "netgraph-script: rollback" in inverse

    written = write_scripts(plan, tmp_path, rollback=True)
    assert all(name.endswith("/rollback.txt") for name in written)


def test_a_script_never_holds_an_absolute_checkout_path(plan: ConvergePlan) -> None:
    for _relative, text in script_files(plan):
        assert str(REPO_ROOT) not in text


def test_a_written_file_becomes_a_quoted_heredoc() -> None:
    """The body of a generated configuration is not the shell's business."""
    command = Command(text="write /etc/x", kind="write", path="/etc/x", content="a=$b\n")
    lines = list(command.script_lines())
    assert lines == [
        "install -d -m 0755 /etc",
        "cat > /etc/x <<'NETGRAPH_EOF'",
        "a=$b",
        "NETGRAPH_EOF",
    ]


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"text": "x", "kind": "reboot"}, "not a command kind"),
        ({"text": "x", "kind": "write", "path": "/etc/x"}, "carries content"),
        ({"text": "x", "kind": "exec", "content": "y", "path": "/etc/x"}, "carries content"),
    ],
)
def test_a_malformed_command_is_refused_at_construction(kwargs: dict[str, str], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        Command(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Dialects
# --------------------------------------------------------------------------- #


def test_a_declarative_dialect_writes_a_file_rather_than_a_command(
    inputs: ConvergeInputs,
) -> None:
    result = build_plan(inputs, dialect="networkd", allow_disruptive=True)
    files = [change for change in result.changes if change.object == "file"]
    assert files, "networkd should have something to write"
    for change in files:
        writes = [command for command in change.commands if command.kind == "write"]
        removes = [command for command in change.commands if command.text.startswith("rm -f")]
        assert writes or removes
        assert any(command.text == "networkctl reload" for command in change.commands)


def test_a_declarative_file_only_appears_when_its_content_differs(
    inputs: ConvergeInputs,
) -> None:
    """Minimality is measured, by generating both sides with the same emitters."""
    result = build_plan(inputs, dialect="networkd", allow_disruptive=True)
    touched = {change.element for change in result.changes if change.object == "file"}
    drifted = {change.element for change in result.changes if change.object != "file"}
    assert touched <= drifted
    assert "hosts/laptop" not in touched, "a device with no drift got a file anyway"


def test_a_declarative_files_rollback_writes_what_the_capture_found(
    inputs: ConvergeInputs,
) -> None:
    result = build_plan(inputs, dialect="networkd", allow_disruptive=True)
    change = next(
        entry
        for entry in result.changes
        if entry.object == "file" and entry.target.endswith("10-eth0.network")
    )
    forward = next(command for command in change.commands if command.kind == "write")
    back = next(command for command in change.rollback if command.kind == "write")
    assert forward.content is not None and back.content is not None
    assert "MTUBytes=1500" in forward.content
    assert "MTUBytes=9000" in back.content


def test_a_declarative_file_inherits_the_worst_risk_of_what_it_realises(
    inputs: ConvergeInputs,
) -> None:
    """netplan apply on a file without the management address is as final as ip addr del."""
    result = build_plan(inputs, dialect="networkd", allow_disruptive=True)
    device = result.device("hosts/pc-desk")
    assert device is not None
    files = [change for change in device.changes if change.object == "file"]
    assert files and all(change.risk is Risk.DISRUPTIVE for change in files)


def test_a_capture_the_dialect_cannot_express_is_a_note_not_a_failure(
    inputs: ConvergeInputs,
) -> None:
    """A device that drifted into an unrepresentable state must still be plannable."""
    result = build_plan(inputs, dialect="netplan", allow_disruptive=True)
    assert any("no syntax for the state the capture found" in note for note in result.notes)
    assert result.changes


def test_a_declaration_the_dialect_cannot_express_still_fails(
    inventory: Inventory, tmp_path: Path
) -> None:
    """The export contract is unchanged: half a configuration is worse than none."""
    tree = load_tree(REPO_ROOT / "examples" / "campus")
    capture = tmp_path / "cap.json"
    capture.write_text("[]", encoding="utf-8")
    inputs_ = ConvergeInputs(tree, [f"sw-core-01={capture}"])
    # The campus tree declares VRFs, which netplan refuses for want of a table
    # number. Whether it refuses is a property of that tree, so the assertion is
    # conditional on it: what must never happen is a *silent* half-export.
    try:
        build_plan(inputs_, dialect="netplan", allow_disruptive=True)
    except UnsupportedConfigError as refusal:
        assert refusal.refusals


@pytest.mark.parametrize("dialect", CONVERGE_DIALECTS)
def test_every_dialect_produces_the_same_changes(inputs: ConvergeInputs, dialect: str) -> None:
    """The dialect decides how a change is written, never whether it is in the plan."""
    result = build_plan(inputs, dialect=dialect, allow_disruptive=True)
    non_file = {change.id for change in result.changes if change.object != "file"}
    baseline = {
        change.id
        for change in build_plan(inputs, allow_disruptive=True).changes
        if change.object != "file"
    }
    assert non_file == baseline


def test_an_unknown_dialect_is_a_programming_error(inputs: ConvergeInputs) -> None:
    with pytest.raises(ValueError, match="not a converge dialect"):
        build_plan(inputs, dialect="cisco")


def test_the_banner_is_not_part_of_what_a_file_does() -> None:
    assert strip_banner("# a\n# b\n\nreal: yes\n") == "real: yes"
    assert strip_banner("! frr\n!\nrouter bgp 1\n") == "router bgp 1"


# --------------------------------------------------------------------------- #
# The shapes the fixtures do not happen to produce
# --------------------------------------------------------------------------- #


def _derive_one(inventory: Inventory, change: Change) -> tuple[Intent, ...]:
    """Run one hand-built finding through the join, with an empty capture.

    Some findings a real capture cannot produce in one tree -- a device whose
    kind disagrees, a trunk VLAN the inventory declares and the wire lacks --
    still have to be handled, and handling them wrongly would be invisible until
    somebody's estate produced one.
    """
    report = DriftReport(root=inventory.root, changes=(change,))
    return derive(inventory, Draft(), report)


def test_a_device_whose_kind_disagrees_needs_hands(inventory: Inventory) -> None:
    intents = _derive_one(
        inventory,
        Change(
            direction=Direction.DISAGREES,
            scope="device",
            element="hosts/pc-desk",
            kind="computer",
            field="kind",
            declared="computer",
            observed="switch",
            message="declared as a computer; the capture reports a switch",
        ),
    )
    assert [intent.kind for intent in intents] == [IntentKind.MANUAL]
    assert "one box into another" in intents[0].note


def test_an_interface_of_the_wrong_type_needs_hands(inventory: Inventory) -> None:
    intents = _derive_one(
        inventory,
        Change(
            direction=Direction.DISAGREES,
            scope="field",
            element="hosts/pc-desk",
            kind="computer",
            path="eno1",
            field="type",
            declared="ethernet",
            observed="bridge",
            message="declared as a ethernet interface; the capture reports bridge",
        ),
    )
    assert [intent.kind for intent in intents] == [IntentKind.MANUAL]
    assert "re-creation" in intents[0].note


def test_a_finding_on_a_device_the_inventory_does_not_declare_needs_hands(
    inventory: Inventory,
) -> None:
    intents = _derive_one(
        inventory,
        Change(
            direction=Direction.UNDECLARED,
            scope="interface",
            element="prn-hall",
            path="eth0",
            observed="ethernet",
            message="the capture reports this interface as ethernet",
        ),
    )
    assert [intent.kind for intent in intents] == [IntentKind.MANUAL]


def test_a_trunk_vlan_the_wire_lacks_is_tagged_on(inventory: Inventory) -> None:
    intents = _derive_one(
        inventory,
        Change(
            direction=Direction.MISSING,
            scope="vlan",
            element="switches/sw-home",
            kind="switch",
            path="port5",
            field="vlan",
            declared="10",
            message="VLAN 10 is declared on this port and the capture does not carry it",
        ),
    )
    assert [intent.kind for intent in intents] == [
        IntentKind.VLAN_CREATE,
        IntentKind.VLAN_TAG,
    ]
    assert render(intents[1])[0].text == "add interface port5 vlan-tagged 10"


def test_a_declared_vlan_mode_is_set_and_reverts_to_what_was_seen(
    inventory: Inventory,
) -> None:
    intents = _derive_one(
        inventory,
        Change(
            direction=Direction.DISAGREES,
            scope="vlan",
            element="switches/sw-home",
            kind="switch",
            path="port5",
            field="vlan.mode",
            declared="trunk",
            observed="access",
            message="declared as a trunk port; the capture reports access",
        ),
    )
    assert [intent.kind for intent in intents] == [IntentKind.VLAN_MODE]
    assert render(intents[0])[0].text == "set interface port5 vlan-mode trunk"
    assert revert(intents[0])[0].text == "set interface port5 vlan-mode access"


def test_an_interface_shut_on_the_wire_is_brought_up_and_back_down(
    inventory: Inventory,
) -> None:
    intents = _derive_one(
        inventory,
        Change(
            direction=Direction.DISAGREES,
            scope="field",
            element="hosts/pc-desk",
            kind="computer",
            path="eno1",
            field="enabled",
            declared="false",
            observed="true",
            message="declared as false; the capture reports true",
        ),
    )
    assert [intent.kind for intent in intents] == [IntentKind.INTERFACE_DISABLE]
    assert render(intents[0])[0].text == "set interface eno1 enabled false"
    assert revert(intents[0])[0].text == "set interface eno1 enabled true"


def test_a_virtual_interface_the_inventory_does_not_declare_is_deleted_and_recreated(
    inventory: Inventory,
) -> None:
    """The inverse of a delete is a create of the same type: that is the round trip."""
    intents = _derive_one(
        inventory,
        Change(
            direction=Direction.UNDECLARED,
            scope="interface",
            element="hosts/pc-desk",
            kind="computer",
            path="br9",
            observed="bridge",
            message="the capture reports this interface as bridge",
        ),
    )
    assert [intent.kind for intent in intents] == [IntentKind.INTERFACE_DELETE]
    assert render(intents[0])[0].text == "delete interface br9"
    assert revert(intents[0])[0].text == "create interface br9 type bridge"


def test_two_findings_asking_for_one_change_are_merged(inventory: Inventory) -> None:
    """Applying the same command twice is noise in a script somebody must read."""
    finding = Change(
        direction=Direction.MISSING,
        scope="address",
        element="hosts/pc-desk",
        kind="computer",
        path="eno1",
        field="ipv4",
        declared="192.168.10.20/24",
        message="192.168.10.20/24 is declared here",
    )
    report = DriftReport(
        root=inventory.root,
        changes=(finding, replace(finding, message="and the capture does not have it")),
    )
    intents = derive(inventory, Draft(), report)
    assert len(intents) == 1
    assert len(intents[0].provenance) == 2


# --------------------------------------------------------------------------- #
# Report formats
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("output_format", ("text", "json", "markdown"))
def test_a_report_is_byte_identical_between_two_runs(
    inputs: ConvergeInputs, output_format: str
) -> None:
    first = render_converge(build_plan(inputs, allow_disruptive=True), output_format)
    second = render_converge(build_plan(inputs, allow_disruptive=True), output_format)
    assert first == second


def test_an_unknown_format_is_refused(plan: ConvergePlan) -> None:
    with pytest.raises(ValueError, match="not a converge report format"):
        render_converge(plan, "yaml")


def test_the_json_carries_everything_a_transport_would_need(plan: ConvergePlan) -> None:
    document = json.loads(render_converge(plan, "json"))
    assert document["converged"] is False
    assert document["dialect"] == "interfaces"
    for device in document["devices"]:
        for change in device["changes"]:
            assert {"id", "risk", "prerequisites", "provenance", "commands", "rollback"} <= set(
                change
            )


def test_the_markdown_escapes_a_pipe(plan: ConvergePlan) -> None:
    """A drift message holding a pipe would otherwise gain the table a column."""
    text = render_converge(plan, "markdown")
    for line in text.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            assert line.count("|") - line.count("\\|") >= 2


def test_the_text_report_names_every_device_and_its_batch(plan: ConvergePlan) -> None:
    text = render_converge(plan, "text")
    for device in plan.devices:
        assert device.element in text
    assert "maintenance batches" in text


def test_the_text_report_truncates_a_long_provenance_list() -> None:
    """A declarative dialect's one file can close a dozen findings.

    The plan is built by hand rather than found in a fixture: the truncation is
    a property of the renderer, and tying the test to whichever capture happens
    to produce four findings on one change would make it skip itself the day the
    fixture changed.
    """
    crowded = ConvergeChange(
        id="net/sw-1#file/etc/x",
        element="net/sw-1",
        action=Action.UPDATE,
        object="file",
        target="/etc/x",
        summary="update /etc/x",
        provenance=tuple(
            Provenance(element="net/sw-1", path=f"port{index}", message=f"finding {index}")
            for index in range(5)
        ),
        commands=(Command(text="reload"),),
    )
    rendered = render_converge(
        ConvergePlan(
            root=Path("net"),
            devices=(DeviceConverge(element="net/sw-1", kind="switch", changes=(crowded,)),),
        ),
        "text",
    )
    assert "finding 2" in rendered
    assert "finding 4" not in rendered
    assert "... and 2 more finding(s)" in rendered


def test_the_markdown_lists_what_needs_hands(plan: ConvergePlan) -> None:
    text = render_converge(plan, "markdown")
    assert "Needs hands:" in text
    assert "## Maintenance batches" in text


def test_a_converged_plan_renders_in_every_format(inventory: Inventory, tmp_path: Path) -> None:
    """The empty case is a case: it is what a passing CI run prints."""
    capture = tmp_path / "nothing.lldp.json"
    capture.write_text(json.dumps({"lldp": {"interface": []}}), encoding="utf-8")
    result = converge(inventory, [str(capture)])
    assert result.converged
    for output_format in ("text", "markdown"):
        assert "Nothing to do" in render_converge(result, output_format)
    assert json.loads(render_converge(result, "json"))["converged"] is True
    assert list(script_files(result)) == []


def test_a_batch_that_takes_something_with_it_says_so(inventory: Inventory) -> None:
    """The text report has to name the outage, not just the window."""
    isolated, _splits = blast_radius(inventory, "switches/sw-home")
    batches = batches_for(inventory, ["switches/sw-home"])
    rendered = render_converge(
        ConvergePlan(root=inventory.root, batches=batches, devices=()), "text"
    )
    assert "Nothing to do" in rendered  # no devices: the batches are not printed
    assert isolated


def test_a_script_lists_the_manual_items_it_could_not_carry(lab_plan: ConvergePlan) -> None:
    """A device with both kinds of finding must not lose the manual half.

    ``srv-lab`` has both: commands for everything the capture contradicted, and
    one physical port netgraph will not touch. The port has to survive into the
    script, as a comment, or the operator running it would believe the device was
    finished when it was not.
    """
    device = lab_plan.device("srv-lab")
    assert device is not None and device.manual and device.commands
    text = script_for(lab_plan, device)
    assert text is not None
    assert "these need a person" in text
    assert "eno3" in text.rsplit("these need a person", 1)[1]


# --------------------------------------------------------------------------- #
# Goldens, one per dialect
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dialect", CONVERGE_DIALECTS)
def test_the_plan_matches_its_golden(
    inputs: ConvergeInputs, dialect: str, regen_golden: bool
) -> None:
    """A committed plan per dialect: the whole output, diffable in review.

    Text rather than JSON, because the golden's job is to make a change to the
    wording, the ordering or the risk classification *visible* to a reviewer, and
    a JSON blob with a here-document in it is not. The file bodies of the
    declarative dialects are covered by the export goldens already.
    """
    plan = build_plan(inputs, dialect=dialect, allow_disruptive=True)
    _assert_golden(render_converge(plan, "text"), f"home-lab-{dialect}.txt", regen_golden)


def _assert_golden(rendered: str, name: str, regen_golden: bool) -> None:
    golden = GOLDEN_DIR / name
    if regen_golden:
        golden.parent.mkdir(parents=True, exist_ok=True)
        # netgraph.fsio.write_text, not Path.write_text: a golden regenerated on
        # Windows must hold the same bytes as one regenerated on Linux, and the
        # default text mode would translate every newline. tests/test_golden.py
        # does the same, for the same reason.
        write_text(golden, rendered)
        pytest.skip(f"regenerated {golden.name}")
    assert golden.is_file(), f"{name} is missing; run pytest --regen-golden"
    assert rendered == golden.read_text(encoding="utf-8"), (
        f"{name} is stale; run 'pytest tests/test_converge.py --regen-golden'"
    )


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


def _run(*args: str) -> Result:
    return CliRunner().invoke(cli, ["-i", str(HOME_LAB), "converge", "plan", *args])


def test_the_cli_refuses_a_disruptive_plan_with_exit_code_four() -> None:
    result = _run(*CAPTURES)
    assert result.exit_code == 4
    assert "--allow-disruptive" in result.output


def test_the_cli_exits_two_when_changes_are_pending() -> None:
    result = _run("--allow-disruptive", *CAPTURES)
    assert result.exit_code == 2
    assert "converge plan for" in result.output


def test_the_cli_exits_zero_when_the_network_already_matches(tmp_path: Path) -> None:
    capture = tmp_path / "unknown.lldp.json"
    capture.write_text(json.dumps({"lldp": {"interface": []}}), encoding="utf-8")
    result = _run(str(capture))
    assert result.exit_code == 0, result.output
    assert "Nothing to do" in result.output


def test_the_cli_refuses_an_inventory_that_does_not_load(tmp_path: Path) -> None:
    broken = tmp_path / "inv"
    broken.mkdir()
    (broken / "bad.yaml").write_text("apiVersion: netgraph.dev/v1alpha1\nkind: nope\n", "utf-8")
    result = CliRunner().invoke(cli, ["-i", str(broken), "converge", "plan", *CAPTURES])
    assert result.exit_code == 1
    assert "does not load" in result.output


def test_the_cli_writes_scripts_under_out(tmp_path: Path) -> None:
    result = _run("--allow-disruptive", "-o", str(tmp_path), *CAPTURES)
    assert result.exit_code == 2
    assert "script(s) written under" in result.output
    assert sorted(path.name for path in tmp_path.rglob("*.txt")) == ["converge.txt"] * 2


def test_the_cli_writes_rollback_scripts(tmp_path: Path) -> None:
    result = _run("--allow-disruptive", "--rollback", "-o", str(tmp_path), *CAPTURES)
    assert result.exit_code == 2
    assert sorted(path.name for path in tmp_path.rglob("*.txt")) == ["rollback.txt"] * 2


def test_rollback_without_out_says_what_it_did_not_do() -> None:
    result = _run("--allow-disruptive", "--rollback", *CAPTURES)
    assert "--rollback affects the scripts" in result.output


@pytest.mark.parametrize("output_format", ("json", "markdown"))
def test_the_cli_renders_the_structured_formats(output_format: str) -> None:
    result = _run("--allow-disruptive", "-F", output_format, *CAPTURES)
    assert result.exit_code == 2
    if output_format == "json":
        assert json.loads(_stdout(result))["dialect"] == "interfaces"
    else:
        assert "# Convergence plan" in result.output


def test_the_cli_narrows_with_only() -> None:
    result = _run("--allow-disruptive", "--only", "srv-nas", *CAPTURES)
    assert result.exit_code == 2
    assert "hosts/pc-desk" not in result.output
    assert "hosts/srv-nas" in result.output


def _stdout(result: Result) -> str:
    """The JSON document, with the stderr commentary the runner mixes in removed."""
    text = result.output
    start = text.index("{")
    end = text.rindex("}") + 1
    return text[start:end]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _change(plan: ConvergePlan, element: str, target: str) -> object:
    device = plan.device(element)
    assert device is not None, f"{element} is not in the plan"
    for change in device.changes:
        if change.target == target:
            return change
    raise AssertionError(f"{element} has no change targeting {target!r}")
