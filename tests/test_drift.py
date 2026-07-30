"""``netgraph drift``: does the inventory still describe the network?

The command inverts ``netgraph import``, and inverting it is where the risk
lies. Importing may safely omit anything it did not see; *comparing* may not,
because an omission read as a deletion turns every partial capture into a false
alarm — and every capture is partial. So the properties asserted here are, in
order of how badly getting them wrong would hurt:

* **A blind spot is never drift.** Whatever the dialect cannot see is reported
  as unobserved, with a reason, and does not appear in the tally, does not set
  :attr:`~netgraph.drift.DriftReport.drifted`, and does not fail the run. This
  is checked per dialect and per field shape — a link no dialect reports, an
  address an ``ip -j link show`` never carried, a trunk VLAN set nothing prints.
* **A real difference is always drift.** A MAC that changed, a VLAN that
  appeared, an interface that vanished from a capture that lists them all, a
  patch lead that moved.
* **Agreement is silent.** The overwhelmingly common case is a device that has
  not changed, and a report that says something about it anyway is a report
  nobody will read twice.
* **The exit code is the contract.** ``--fail-on drift`` exits 1 on a
  difference and 0 without one; ``--fail-on none`` exits 0 either way. Nothing
  in the unobserved section moves either.

The fixtures in ``tests/fixtures/drift/`` are captures taken against
``examples/home-lab``, so the declared side of every test below is a tree that
is committed, validates and renders — not a stub written to make an assertion
pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from click.testing import CliRunner, Result

from netgraph.cli import cli, main
from netgraph.diagnostics import JUnitCase, as_junit
from netgraph.drift import (
    CAPABILITIES,
    Capability,
    Change,
    CompareSpec,
    Direction,
    DriftReport,
    Unobserved,
    as_json,
    as_junit_report,
    check_drift,
    compare,
    coverage_of,
    render_drift,
)
from netgraph.drift.report import _root_text
from netgraph.importer import (
    Draft,
    DraftCable,
    DraftDevice,
    DraftInterface,
    DraftVlan,
    ImportSourceError,
)
from netgraph.loader import Inventory, load_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_LAB = REPO_ROOT / "examples" / "home-lab"
FIXTURES = Path(__file__).parent / "fixtures" / "drift"

PC_DESK = FIXTURES / "pc-desk.addr.json"
SW_HOME = FIXTURES / "sw-home.lldp.json"
SRV_NAS_LINK = FIXTURES / "srv-nas.link.json"
PATCH = FIXTURES / "patch.csv"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def home_lab() -> Inventory:
    """The committed example inventory, loaded once for the whole module."""
    return load_tree(HOME_LAB)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str, **kwargs: Any) -> Result:
    return runner.invoke(cli, list(args), catch_exceptions=False, **kwargs)


def run_drift(inventory: Inventory, *paths: Path, **kwargs: Any) -> DriftReport:
    return check_drift(inventory, [str(path) for path in paths], **kwargs)


def changes_at(report: DriftReport, element: str, path: str = "") -> list[Change]:
    return [
        change
        for change in report.changes
        if change.element == element and (not path or change.path == path)
    ]


def blind_at(report: DriftReport, element: str) -> list[Unobserved]:
    return [entry for entry in report.unobserved if entry.element == element]


def directions(changes: list[Change]) -> set[Direction]:
    return {change.direction for change in changes}


# --------------------------------------------------------------------------- #
# The invariant the whole command rests on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("capture", "dialect"),
    [
        pytest.param(PC_DESK, "iproute", id="iproute"),
        pytest.param(SW_HOME, "lldp", id="lldp"),
        pytest.param(PATCH, "csv", id="csv"),
        pytest.param(SRV_NAS_LINK, "iproute", id="iproute-link-only"),
    ],
)
def test_an_unobserved_field_is_never_counted_as_drift(
    home_lab: Inventory, capture: Path, dialect: str
) -> None:
    """Every dialect, every capture: the blind spots stay out of the tally."""
    report = run_drift(home_lab, capture, dialect=dialect)
    assert report.unobserved, f"{capture.name} should have blind spots against a 7-device tree"
    assert len(report.changes) == sum(len(group.changes) for group in report.elements), (
        "the grouping lost or duplicated a difference"
    )
    assert report.drifted is bool(report.changes)
    # The two lists are disjoint in what they claim: nothing reported as a blind
    # spot is also reported as a difference at the same place.
    blind = {(entry.element, entry.path) for entry in report.unobserved}
    for change in report.changes:
        assert (
            change.element,
            change.path,
        ) not in blind or change.direction is not Direction.MISSING


def test_a_capture_of_one_host_does_not_delete_the_rest_of_the_network(
    home_lab: Inventory,
) -> None:
    """The failure mode the command exists to avoid, asserted directly."""
    report = run_drift(home_lab, PC_DESK, dialect="iproute")
    uncovered = {"hosts/laptop", "hosts/phone", "hosts/srv-nas", "routers/rtr-home"}
    assert uncovered <= {entry.element for entry in report.unobserved}
    assert not (uncovered & {change.element for change in report.changes})


# --------------------------------------------------------------------------- #
# iproute
# --------------------------------------------------------------------------- #


def test_iproute_reports_a_changed_mac(home_lab: Inventory) -> None:
    report = run_drift(home_lab, PC_DESK, dialect="iproute")
    (mac,) = [change for change in changes_at(report, "hosts/pc-desk") if change.field == "mac"]
    assert mac.direction is Direction.DISAGREES
    assert mac.path == "eno1"
    assert mac.declared == "3c:97:0e:20:01:01"
    assert mac.observed == "3c:97:0e:20:01:ff"


def test_iproute_reports_a_vlan_the_inventory_does_not_declare(home_lab: Inventory) -> None:
    """The trunk VLAN set is a lower bound, so an extra one is a real difference."""
    report = run_drift(home_lab, PC_DESK, dialect="iproute")
    (vlan,) = [
        change
        for change in changes_at(report, "hosts/pc-desk")
        if change.scope == "vlan" and change.path == "eno1"
    ]
    assert vlan.direction is Direction.UNDECLARED
    assert vlan.observed == "30"


def test_iproute_reports_the_sub_interface_itself_as_undeclared(home_lab: Inventory) -> None:
    (extra,) = [
        change
        for change in changes_at(
            report=run_drift(home_lab, PC_DESK, dialect="iproute"), element="hosts/pc-desk"
        )
        if change.scope == "interface"
    ]
    assert (extra.direction, extra.path, extra.observed) == (
        Direction.UNDECLARED,
        "eno1.30",
        "vlan",
    )


def test_iproute_lists_every_interface_so_a_missing_one_is_drift(
    home_lab: Inventory, tmp_path: Path
) -> None:
    """``ip`` prints the whole link table, so silence about a port is meaningful."""
    capture = tmp_path / "pc-desk.addr.json"
    capture.write_text(
        PC_DESK.read_text(encoding="utf-8").replace('"ifname": "wlp1s0"', '"ifname": "wlp9s9"'),
        encoding="utf-8",
    )
    report = run_drift(home_lab, capture, dialect="iproute")
    missing = [change for change in report.changes if change.direction is Direction.MISSING]
    assert [change.path for change in missing] == ["wlp1s0"]
    assert "lists every interface" in missing[0].message


def test_a_loopback_interface_is_unobserved_rather_than_missing(home_lab: Inventory) -> None:
    """The importer skips ``lo`` by design; the comparison must know that."""
    report = run_drift(home_lab, PC_DESK, dialect="iproute")
    (blind,) = [entry for entry in blind_at(report, "hosts/pc-desk") if entry.path == "lo"]
    assert blind.scope == "interface"
    assert "loopback" in blind.reason
    assert not [change for change in report.changes if change.path == "lo"]


def test_a_link_only_capture_leaves_the_addresses_unobserved(home_lab: Inventory) -> None:
    """``ip -j link show`` is the ``iproute`` dialect and carries no address.

    Coverage therefore cannot come from the dialect alone: without the evidence
    check every declared address would read as removed from the network.
    """
    report = run_drift(home_lab, SRV_NAS_LINK, dialect="iproute")
    families = {
        entry.path for entry in blind_at(report, "hosts/srv-nas") if entry.scope == "address"
    }
    assert families == {"eth0.ipv4", "eth0.ipv6"}
    assert not [change for change in report.changes if change.scope == "address"]
    # The MTU in the same capture *is* observed, and it changed.
    (mtu,) = [change for change in report.changes if change.field == "mtu"]
    assert (mtu.declared, mtu.observed) == ("1500", "9000")


def test_an_address_the_capture_covers_and_lacks_is_drift(
    home_lab: Inventory, tmp_path: Path
) -> None:
    """The other side of the evidence check: an ``addr`` capture does assert."""
    capture = tmp_path / "pc-desk.addr.json"
    capture.write_text(
        PC_DESK.read_text(encoding="utf-8").replace("2001:db8:10::20", "2001:db8:10::ff"),
        encoding="utf-8",
    )
    report = run_drift(home_lab, capture, dialect="iproute")
    addresses = [change for change in report.changes if change.scope == "address"]
    assert directions(addresses) == {Direction.UNDECLARED, Direction.MISSING}
    assert {change.declared or change.observed for change in addresses} == {
        "2001:db8:10::20/64",
        "2001:db8:10::ff/64",
    }


def test_a_wifi_interface_reported_as_ether_is_not_a_difference(home_lab: Inventory) -> None:
    """``ip`` says ``link_type: ether`` about every NIC, wireless ones included."""
    report = run_drift(home_lab, PC_DESK, dialect="iproute")
    assert not [change for change in report.changes if change.path == "wlp1s0"]


def test_a_declared_bridge_reported_as_a_plain_nic_is_a_difference(
    home_lab: Inventory, tmp_path: Path
) -> None:
    """The converse: ``linkinfo`` would have said so, and the capture is complete."""
    capture = tmp_path / "sw-home.addr.json"
    capture.write_text(
        '[{"ifname": "br0", "link_type": "ether", "mtu": 1500, "flags": ["UP"]}]',
        encoding="utf-8",
    )
    report = run_drift(home_lab, capture, dialect="iproute")
    (kind,) = [change for change in report.changes if change.field == "type"]
    assert (kind.declared, kind.observed) == ("bridge", "ethernet")


# --------------------------------------------------------------------------- #
# lldp
# --------------------------------------------------------------------------- #


def test_lldp_reports_a_neighbour_that_no_cable_declares(home_lab: Inventory) -> None:
    report = run_drift(home_lab, SW_HOME, dialect="lldp")
    (link,) = [change for change in report.changes if change.scope == "link"]
    assert link.direction is Direction.UNDECLARED
    assert link.kind == "cable"
    assert link.observed == "prn-hall:eth0 <-> switches/sw-home:port6"


def test_lldp_reports_a_device_the_inventory_does_not_declare(home_lab: Inventory) -> None:
    report = run_drift(home_lab, SW_HOME, dialect="lldp")
    (device,) = [change for change in report.changes if change.scope == "device"]
    assert (device.direction, device.element) == (Direction.UNDECLARED, "prn-hall")


def test_lldp_agrees_with_a_cable_that_is_declared(home_lab: Inventory) -> None:
    """port1 and port2 report exactly what the inventory says; silence is the answer."""
    report = run_drift(home_lab, SW_HOME, dialect="lldp")
    assert not changes_at(report, "cables/cbl-rtr-sw")
    assert not changes_at(report, "cables/cbl-sw-desk")


def test_lldp_does_not_list_every_port_so_a_missing_one_is_unobserved(
    home_lab: Inventory,
) -> None:
    """LLDP shows the ports that saw a neighbour and no others."""
    report = run_drift(home_lab, SW_HOME, dialect="lldp")
    unseen = {entry.path for entry in blind_at(report, "switches/sw-home")}
    assert {"port3", "port4", "port5", "br0", "Vlan10"} <= unseen
    assert not [change for change in report.changes if change.direction is Direction.MISSING]


def test_a_cable_lldp_did_not_see_is_unobserved_not_unplugged(home_lab: Inventory) -> None:
    """A neighbour that does not speak LLDP is invisible, not absent."""
    report = run_drift(home_lab, SW_HOME, dialect="lldp")
    (blind,) = blind_at(report, "cables/cbl-sw-nas")
    assert blind.scope == "link"
    assert "does not speak LLDP" in blind.reason


def test_a_device_kind_lldp_determined_is_compared(home_lab: Inventory, tmp_path: Path) -> None:
    """``rtr-home`` advertises Router and is declared one; make it disagree."""
    capture = tmp_path / "sw-home.lldp.json"
    capture.write_text(
        SW_HOME.read_text(encoding="utf-8").replace(
            '{"type": "Bridge", "enabled": false}', '{"type": "Bridge", "enabled": true}'
        ),
        encoding="utf-8",
    )
    report = run_drift(home_lab, capture, dialect="lldp")
    (kind,) = [change for change in report.changes if change.field == "kind"]
    assert (kind.element, kind.declared, kind.observed) == ("routers/rtr-home", "router", "switch")


def test_the_neutral_computer_fallback_is_not_evidence_of_a_kind(home_lab: Inventory) -> None:
    """``pc-desk`` advertises only Station, which maps to no netgraph kind."""
    report = run_drift(home_lab, SW_HOME, dialect="lldp")
    assert not [change for change in changes_at(report, "hosts/pc-desk") if change.field == "kind"]


# --------------------------------------------------------------------------- #
# csv
# --------------------------------------------------------------------------- #


def test_csv_reports_a_patch_lead_that_moved(home_lab: Inventory) -> None:
    """The pair: the declared link is gone and the observed one is new."""
    report = run_drift(home_lab, PATCH, dialect="csv")
    (missing,) = changes_at(report, "cables/cbl-sw-nas")
    assert missing.direction is Direction.MISSING
    assert "connected to something else" in missing.message
    assert [
        change
        for change in report.changes
        if change.direction is Direction.UNDECLARED and change.scope == "link"
    ]


def test_csv_reports_a_stated_medium_that_disagrees(home_lab: Inventory) -> None:
    report = run_drift(home_lab, PATCH, dialect="csv")
    (medium,) = [change for change in report.changes if change.field == "medium"]
    assert (medium.element, medium.declared, medium.observed) == (
        "cables/cbl-sw-ap",
        "copper",
        "fiber",
    )


def test_a_medium_no_row_stated_is_not_compared(home_lab: Inventory, tmp_path: Path) -> None:
    """``copper`` is the importer's fallback, not an observation."""
    capture = tmp_path / "patch.csv"
    capture.write_text("sw-home,port5,ap-home,eth0\n", encoding="utf-8")
    report = run_drift(home_lab, capture, dialect="csv")
    assert not [change for change in report.changes if change.field == "medium"]


# --------------------------------------------------------------------------- #
# Dialects together
# --------------------------------------------------------------------------- #


def test_two_dialects_covering_one_device_union_their_coverage(home_lab: Inventory) -> None:
    """LLDP sees sw-home's links, ``ip`` its interfaces; together, both."""
    report = run_drift(home_lab, SW_HOME, PC_DESK, dialect="auto")
    assert set(report.dialects) == {"lldp", "iproute"}
    # pc-desk is in both captures. Its interface set now comes from ``ip``, so
    # the LLDP-only blind spot about it is gone.
    unseen = {entry.path for entry in blind_at(report, "hosts/pc-desk")}
    assert "wlp1s0" not in unseen
    assert "lo" in unseen


def test_the_dialect_of_each_input_is_recorded_on_the_draft() -> None:
    """The join coverage is built from: source name to how it was read."""
    from netgraph.importer import build_draft, read_inputs

    draft = build_draft(read_inputs([str(SW_HOME), str(PC_DESK), str(PATCH)]))
    assert draft.dialects == {
        "sw-home.lldp.json": "lldp",
        "pc-desk.addr.json": "iproute",
        "patch.csv": "csv",
    }
    # ``pc-desk`` is named by all three inputs, so it gets all three capabilities.
    assert coverage_of(draft).dialects_of("pc-desk") == ("csv", "iproute", "lldp")
    assert coverage_of(draft).dialects_of("srv-nas") == ("csv",)


# --------------------------------------------------------------------------- #
# Coverage, on its own
# --------------------------------------------------------------------------- #


def test_a_dialect_nothing_knows_about_sees_nothing() -> None:
    """A hand-built draft claims no coverage it did not declare."""
    draft = Draft(
        devices={"box": DraftDevice(name="box", sources=["x"])}, dialects={"x": "made-up"}
    )
    coverage = coverage_of(draft)
    assert coverage.saw("box")
    assert not coverage.observes_interfaces("box")
    assert not coverage.observes_links("box")
    assert not coverage.observes_addresses("box", "ipv4")
    assert not coverage.observes_members("box")
    assert not coverage.observes_trunk_vlans("box")
    assert not coverage.saw("absent")


def test_capabilities_merge_as_a_union() -> None:
    merged = CAPABILITIES["lldp"].merge(CAPABILITIES["iproute"])
    assert merged == Capability(interfaces=True, links=True, addresses=True, members=True)


def test_address_coverage_needs_evidence_not_just_a_dialect() -> None:
    """The whole point of :meth:`Coverage.observes_addresses`."""
    device = DraftDevice(name="host", sources=["cap"])
    device.interfaces["eth0"] = DraftInterface(name="eth0", mtu=1500)
    draft = Draft(devices={"host": device}, dialects={"cap": "iproute"})
    coverage = coverage_of(draft)
    assert coverage.of("host").addresses
    assert not coverage.observes_addresses("host", "ipv4")

    device.interfaces["eth0"].ipv4 = ["10.0.0.1/24"]
    assert coverage_of(draft).observes_addresses("host", "ipv4")


# --------------------------------------------------------------------------- #
# The comparison core, against hand-built drafts
# --------------------------------------------------------------------------- #


def _draft(*interfaces: DraftInterface, dialect: str = "iproute") -> Draft:
    """One iproute-observed ``pc-desk`` holding exactly these interfaces."""
    device = DraftDevice(name="pc-desk", sources=["capture"])
    for interface in interfaces:
        device.add_interface(interface)
    return Draft(devices={"pc-desk": device}, dialects={"capture": dialect})


def test_a_field_the_inventory_leaves_unset_cannot_drift(home_lab: Inventory) -> None:
    """Silence is not an assertion: ``sw-home:br0`` declares no MAC."""
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(
        DraftInterface(name="br0", type="bridge", members=["port1"], mac="00:22:07:aa:00:00")
    )
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    assert not [change for change in report.changes if change.field == "mac"]


def test_a_declared_trunk_vlan_the_capture_cannot_see_is_unobserved(
    home_lab: Inventory,
) -> None:
    """``sw-home:port5`` trunks 10 and 20; ``ip`` prints neither."""
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(
        DraftInterface(
            name="port5",
            vlan=DraftVlan(mode="trunk", trunk_vlans=[10], comment="inferred: from eno1.10"),
        )
    )
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    (blind,) = [entry for entry in report.unobserved if entry.path == "port5.vlan.trunk_vlans"]
    assert blind.items == ("20",)
    assert "lower" not in blind.reason  # it says *why*, not merely *that*
    assert not [change for change in report.changes if change.path == "port5"]


def test_an_inferred_trunk_does_not_contradict_a_declared_mode(home_lab: Inventory) -> None:
    """The block netgraph reasoned its way to is not evidence about the mode."""
    device = DraftDevice(name="rtr-home", sources=["capture"])
    device.add_interface(
        DraftInterface(
            name="lan0",
            vlan=DraftVlan(mode="trunk", trunk_vlans=[10], comment="inferred: from lan0.10"),
        )
    )
    draft = Draft(devices={"rtr-home": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("rtr-home",)))
    assert not [change for change in report.changes if change.field == "vlan.mode"]


def test_an_observed_access_vlan_is_compared(home_lab: Inventory) -> None:
    """A VLAN sub-interface is a real observation, comment-free."""
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(
        DraftInterface(name="port1", vlan=DraftVlan(mode="access", access_vlan=99))
    )
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    (vlan,) = [change for change in report.changes if change.field == "vlan.access_vlan"]
    assert (vlan.declared, vlan.observed) == ("10", "99")


def test_bridge_membership_is_compared_in_both_directions(home_lab: Inventory) -> None:
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(
        DraftInterface(name="br0", type="bridge", members=["port1", "port2", "port9"])
    )
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    members = [change for change in report.changes if change.field == "members"]
    assert directions(members) == {Direction.UNDECLARED, Direction.MISSING}
    assert {change.observed for change in members if change.observed} == {"port9"}
    assert {change.declared for change in members if change.declared} == {"port3", "port4", "port5"}


def test_membership_no_dialect_reported_is_unobserved(home_lab: Inventory) -> None:
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(DraftInterface(name="br0", type="bridge"))
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "lldp"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    (blind,) = [entry for entry in report.unobserved if entry.path == "br0.members"]
    assert len(blind.items) == 5
    assert not [change for change in report.changes if change.field == "members"]


def test_an_address_is_compared_however_it_is_spelled(home_lab: Inventory) -> None:
    """``2001:DB8:10:0:0:0:0:20/64`` and ``2001:db8:10::20/64`` are one address."""
    draft = _draft(DraftInterface(name="eno1", ipv6=["2001:DB8:10:0000:0000:0000:0000:0020/64"]))
    report = compare(home_lab, draft, spec=CompareSpec(only=("pc-desk",)))
    assert not [change for change in report.changes if change.scope == "address"]


def test_an_address_the_capture_cannot_parse_is_compared_verbatim(home_lab: Inventory) -> None:
    draft = _draft(DraftInterface(name="eno1", ipv4=["not-an-address"]))
    report = compare(home_lab, draft, spec=CompareSpec(only=("pc-desk",)))
    assert "not-an-address" in {change.observed for change in report.changes}


def test_a_cable_whose_endpoint_does_not_resolve_is_left_to_the_validator(
    tmp_path: Path,
) -> None:
    """``E001``'s business, not drift's: the network is not to blame."""
    (tmp_path / "tree.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc}\n"
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet}]\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec:\n"
        "  endpoints: [pc:eth0, ghost:eth0]\n"
        "  medium: copper\n",
        encoding="utf-8",
    )
    inventory = load_tree(tmp_path)
    device = DraftDevice(name="pc", sources=["capture"])
    device.add_interface(DraftInterface(name="eth0"))
    draft = Draft(devices={"pc": device}, dialects={"capture": "iproute"})
    report = compare(inventory, draft)
    assert not [change for change in report.changes if change.scope == "link"]
    assert not [entry for entry in report.unobserved if entry.scope == "link"]


def test_an_observed_name_matching_a_cable_is_not_treated_as_a_device(
    home_lab: Inventory,
) -> None:
    """A capture naming a host ``cbl-sw-nas`` describes a box, not that cable."""
    device = DraftDevice(name="cbl-sw-nas", sources=["capture"])
    device.add_interface(DraftInterface(name="eth0"))
    draft = Draft(devices={"cbl-sw-nas": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("cbl-sw-nas",)))
    (undeclared,) = [change for change in report.changes if change.scope == "device"]
    assert undeclared.direction is Direction.UNDECLARED


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def test_only_narrows_the_comparison_to_matching_elements(home_lab: Inventory) -> None:
    report = run_drift(home_lab, PC_DESK, dialect="iproute", spec=CompareSpec(only=("pc-desk",)))
    assert report.compared == ("hosts/pc-desk",)
    assert {change.element for change in report.changes} == {"hosts/pc-desk"}
    assert report.filtered


def test_only_accepts_a_fully_qualified_glob(home_lab: Inventory) -> None:
    report = run_drift(home_lab, PC_DESK, dialect="iproute", spec=CompareSpec(only=("hosts/*",)))
    assert report.compared == ("hosts/pc-desk",)


def test_exclude_removes_an_element_and_its_links(home_lab: Inventory) -> None:
    report = run_drift(
        home_lab, SW_HOME, dialect="lldp", spec=CompareSpec(exclude=("prn-hall", "sw-home"))
    )
    assert not [change for change in report.changes if change.scope == "link"]
    assert "prn-hall" not in {change.element for change in report.changes}


def test_exclude_beats_only(home_lab: Inventory) -> None:
    spec = CompareSpec(only=("pc-desk",), exclude=("pc-desk",))
    report = run_drift(home_lab, PC_DESK, dialect="iproute", spec=spec)
    assert not report.changes
    assert not report.compared


def test_exclude_interface_keeps_a_declared_port_out_of_the_missing_list(
    home_lab: Inventory,
) -> None:
    """The capture was told not to look, so its silence proves nothing."""
    spec = CompareSpec(only=("pc-desk",), ignore_interfaces=("wlp*",))
    report = run_drift(home_lab, PC_DESK, dialect="iproute", spec=spec)
    assert not [change for change in report.changes if change.path == "wlp1s0"]
    (blind,) = [entry for entry in report.unobserved if entry.path == "wlp1s0"]
    assert "--exclude-interface" in blind.reason


# --------------------------------------------------------------------------- #
# The report shapes
# --------------------------------------------------------------------------- #


def test_the_report_is_ordered_deterministically(home_lab: Inventory) -> None:
    first = run_drift(home_lab, SW_HOME, PC_DESK, PATCH)
    second = run_drift(home_lab, PATCH, PC_DESK, SW_HOME)
    assert [change.order for change in first.changes] == sorted(
        change.order for change in first.changes
    )
    assert {change.location for change in first.changes} == {
        change.location for change in second.changes
    }


def test_the_json_envelope_carries_the_run_and_both_lists(home_lab: Inventory) -> None:
    report = run_drift(home_lab, PC_DESK, dialect="iproute", spec=CompareSpec(only=("pc-desk",)))
    payload = as_json(report)
    assert payload["schemaVersion"] == 1
    assert payload["tool"]["name"] == "netgraph"
    assert payload["capture"] == {
        "inputs": ["pc-desk.addr.json"],
        "dialects": ["iproute"],
        "devices": ["pc-desk"],
    }
    assert payload["drifted"] is True
    assert payload["summary"]["total"] == len(payload["drift"]) == 3
    assert payload["summary"]["unobserved"] == len(payload["unobserved"])
    assert payload["compared"] == ["hosts/pc-desk"]
    assert {record["direction"] for record in payload["drift"]} == {"undeclared", "disagrees"}
    assert all(record["reason"] for record in payload["unobserved"])
    assert render_drift(report, "json").startswith("{")


def test_the_junit_document_parses_and_counts_what_it_claims(home_lab: Inventory) -> None:
    report = run_drift(home_lab, PC_DESK, dialect="iproute", spec=CompareSpec(only=("pc-desk",)))
    root = ElementTree.fromstring(as_junit_report(report))
    suite = root.find("testsuite")
    assert suite is not None
    cases = suite.findall("testcase")
    assert int(suite.attrib["tests"]) == len(cases)
    assert int(suite.attrib["failures"]) == len(suite.findall("testcase/failure"))
    assert int(suite.attrib["skipped"]) == len(suite.findall("testcase/skipped"))
    failure = suite.find("testcase[@name='hosts/pc-desk']/failure")
    assert failure is not None
    assert failure.attrib["message"].startswith("3 differences")
    assert "eno1.mac" in (failure.text or "")


def test_an_element_that_agrees_is_a_passing_junit_case(home_lab: Inventory) -> None:
    """Six devices must show six rows, not only the broken ones."""
    report = run_drift(home_lab, SW_HOME, dialect="lldp")
    root = ElementTree.fromstring(as_junit_report(report))
    names = {case.attrib["name"] for case in root.findall("testsuite/testcase")}
    assert "cables/cbl-rtr-sw" in names
    passing = root.find("testsuite/testcase[@name='cables/cbl-rtr-sw']")
    assert passing is not None and len(passing) == 0


def test_the_junit_writer_escapes_what_xml_forbids() -> None:
    """A device description read off a switch can hold anything at all."""
    document = as_junit(
        "s",
        [
            JUnitCase(
                classname="c", name='a "quoted" & <angled> name', failure="1 < 2", detail="\x00"
            )
        ],
    )
    root = ElementTree.fromstring(document)
    case = root.find("testsuite/testcase")
    assert case is not None
    assert case.attrib["name"] == 'a "quoted" & <angled> name'
    failure = case.find("failure")
    assert failure is not None and "\x00" not in (failure.text or "")


def test_render_drift_rejects_a_format_it_does_not_write(home_lab: Inventory) -> None:
    with pytest.raises(ValueError, match="not a structured drift format"):
        render_drift(DriftReport(root=home_lab.root), "text")


def test_the_text_header_prefers_a_relative_root(tmp_path: Path) -> None:
    assert _root_text(Path.cwd()) == "."
    assert _root_text(tmp_path / "elsewhere") == str(tmp_path / "elsewhere")


def test_an_empty_report_says_so(home_lab: Inventory, runner: CliRunner, tmp_path: Path) -> None:
    capture = tmp_path / "pc-desk.addr.json"
    capture.write_text(
        '[{"ifname": "eno1", "link_type": "ether", "mtu": 1500, "flags": ["UP"],'
        ' "address": "3c:97:0e:20:01:01"}]',
        encoding="utf-8",
    )
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "drift",
        "--only",
        "pc-desk",
        "--exclude-interface",
        "*",
        str(capture),
    )
    assert result.exit_code == 0
    assert "no drift" in result.output


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_drift_exits_one_when_the_network_disagrees(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "drift", str(PC_DESK))
    assert result.exit_code == 1
    assert "3c:97:0e:20:01:ff" in result.output
    assert "unobserved" in result.output


def test_fail_on_none_reports_the_same_drift_and_exits_zero(runner: CliRunner) -> None:
    """The gate is the only thing ``--fail-on`` moves."""
    gated = invoke(runner, "-i", str(HOME_LAB), "drift", str(PC_DESK))
    open_gate = invoke(runner, "-i", str(HOME_LAB), "drift", "--fail-on", "none", str(PC_DESK))
    assert (gated.exit_code, open_gate.exit_code) == (1, 0)
    assert gated.output == open_gate.output


def test_drift_exits_zero_when_the_network_agrees(runner: CliRunner, tmp_path: Path) -> None:
    """A capture of exactly what the inventory declares fails nothing."""
    capture = tmp_path / "srv-nas.addr.json"
    capture.write_text(
        '[{"ifname": "eth0", "link_type": "ether", "mtu": 1500, "flags": ["UP"],'
        ' "address": "00:11:32:40:01:01", "addr_info": ['
        '{"family": "inet", "local": "192.168.10.10", "prefixlen": 24, "scope": "global"},'
        '{"family": "inet6", "local": "2001:db8:10::10", "prefixlen": 64, "scope": "global"}]}]',
        encoding="utf-8",
    )
    result = invoke(runner, "-i", str(HOME_LAB), "drift", "--only", "srv-nas", str(capture))
    assert result.exit_code == 0, result.output
    assert "no drift: 1 element compared" in result.output


def test_the_structured_document_goes_to_stdout_and_the_summary_to_stderr(
    runner: CliRunner,
) -> None:
    result = invoke(
        runner, "-i", str(HOME_LAB), "drift", "--only", "pc-desk", "-F", "json", str(PC_DESK)
    )
    assert result.exit_code == 1
    assert result.stdout.lstrip().startswith("{")
    assert "difference" in result.stderr


def test_quiet_drops_the_summary_but_not_the_document(runner: CliRunner) -> None:
    result = invoke(
        runner, "-q", "-i", str(HOME_LAB), "drift", "--only", "pc-desk", "-F", "json", str(PC_DESK)
    )
    assert result.stdout.lstrip().startswith("{")
    assert result.stderr == ""


def test_drift_refuses_an_inventory_that_does_not_load(runner: CliRunner, tmp_path: Path) -> None:
    """A rejected document is absent from the comparison and would read as drift."""
    (tmp_path / "broken.yaml").write_text("apiVersion: netgraph.dev/v1alpha1\nkind: nope\n")
    result = invoke(runner, "-i", str(tmp_path), "drift", str(PC_DESK))
    assert result.exit_code == 1
    assert "refusing to compare" in result.output


def test_drift_reports_an_input_it_cannot_read(capsys: Any, tmp_path: Path) -> None:
    """Through ``main``, which is what turns the error into an exit status."""
    code = main(["-i", str(HOME_LAB), "drift", str(tmp_path / "nothing.json")])
    assert code == ImportSourceError.exit_code == 3
    assert "no such file" in capsys.readouterr().err


def test_drift_reads_standard_input(runner: CliRunner) -> None:
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "drift",
        "--host",
        "pc-desk",
        "--only",
        "pc-desk",
        "-",
        input=PC_DESK.read_text(encoding="utf-8"),
    )
    assert result.exit_code == 1
    assert "eno1.mac" in result.output or "3c:97:0e:20:01:ff" in result.output


def test_the_named_dialect_is_used_rather_than_sniffed(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "drift", "--from", "lldp", str(PC_DESK))
    # An ``ip`` capture read as LLDP yields no neighbour, hence no comparison.
    assert "no drift" in result.output or "unobserved" in result.output


def test_drift_needs_an_input(capsys: Any) -> None:
    assert main(["-i", str(HOME_LAB), "drift"]) == 3
    assert "no input given" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The corners
# --------------------------------------------------------------------------- #


def test_a_stacking_type_lldp_could_not_have_seen_is_unobserved(home_lab: Inventory) -> None:
    """The other branch of the ``type`` judgement: no coverage, no verdict.

    An LLDP capture creates the ports it saw a neighbour on as plain
    ``ethernet``, which says nothing about whether they are bridges — so a
    declared bridge reported that way must not become a difference.
    """
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(DraftInterface(name="br0", type="ethernet"))
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "lldp"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    assert not [change for change in report.changes if change.field == "type"]
    (blind,) = [entry for entry in report.unobserved if entry.path == "br0" and entry.items]
    assert "type" in blind.items


def test_a_cable_speed_that_disagrees_is_reported(home_lab: Inventory) -> None:
    """No dialect states one today, so the path is exercised directly."""
    draft = Draft(dialects={"capture": "csv"})
    for name in ("rtr-home", "sw-home"):
        device = draft.device(name)
        device.observed_in("capture")
        device.interface("lan0" if name == "rtr-home" else "port1")
    draft.add_cable(
        DraftCable(
            endpoints=(("rtr-home", "lan0"), ("sw-home", "port1")),
            speed="100Mbps",
            label="H-009",
            sources=["capture"],
        )
    )
    draft.assign_cable_names()
    report = compare(home_lab, draft)
    fields = {change.field: (change.declared, change.observed) for change in report.changes}
    assert fields["speed"] == ("1Gbps", "100Mbps")
    assert fields["label"] == ("H-001", "H-009")


def test_a_declared_vlan_set_is_named_in_the_message(home_lab: Inventory) -> None:
    """``the inventory declares VLAN 10, 20 here`` — the set, not just its size."""
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(
        DraftInterface(
            name="port5",
            vlan=DraftVlan(mode="trunk", trunk_vlans=[10, 20, 30], comment="inferred: stacked"),
        )
    )
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    (vlan,) = [change for change in report.changes if change.scope == "vlan"]
    assert vlan.observed == "30"
    assert "VLAN 10, 20" in vlan.message


def test_a_passive_patch_panel_is_matched_without_comparing_its_ports(
    tmp_path: Path,
) -> None:
    """A panel configures nothing, so there is nothing of its own to disagree."""
    (tmp_path / "tree.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: patchpanel\n"
        "metadata: {name: pp-a}\n"
        "spec:\n"
        "  ports: 4\n",
        encoding="utf-8",
    )
    inventory = load_tree(tmp_path)
    device = DraftDevice(name="pp-a", kind="switch", sources=["capture"])
    device.add_interface(DraftInterface(name="1"))
    draft = Draft(devices={"pp-a": device}, dialects={"capture": "lldp"})
    report = compare(inventory, draft)
    assert not report.changes
    assert report.compared == ("pp-a",)


def test_membership_the_capture_covers_but_did_not_report_is_unobserved(
    home_lab: Inventory,
) -> None:
    """An ``iproute`` capture that found no member at all has not disproved one."""
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(DraftInterface(name="br0", type="bridge"))
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    assert not [change for change in report.changes if change.field == "members"]
    assert [entry for entry in report.unobserved if entry.path == "br0.members"]


def test_an_interface_with_no_vlan_on_either_side_says_nothing(home_lab: Inventory) -> None:
    device = DraftDevice(name="sw-home", sources=["capture"])
    device.add_interface(DraftInterface(name="br0", type="bridge", members=["port1"]))
    draft = Draft(devices={"sw-home": device}, dialects={"capture": "iproute"})
    report = compare(home_lab, draft, spec=CompareSpec(only=("sw-home",)))
    assert not [change for change in report.changes if change.scope == "vlan"]
    assert not [entry for entry in report.unobserved if entry.scope == "vlan"]
