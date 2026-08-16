"""``netviz import``: does the tree it writes describe what was captured?

Three properties are asserted throughout, because they are what the command
promises and everything else is detail:

* **Nothing is invented.** A field no capture covered is absent. The tests below
  check the negative — no ``vendor:``, no placeholder address, no ``router``
  conjured out of a host that merely forwards — as well as the positive.
* **The result is usable.** The generated tree loads through the real loader,
  validates without an *error*, and renders. An importer that emits YAML nothing
  downstream accepts would be worse than no importer.
* **Two views of one link are one cable.** LLDP is symmetric, so the dedup is
  not an optimisation; without it every fully-captured network would have twice
  the cables it has.

The JSON fixtures in ``tests/fixtures/import/`` are shaped like real output,
field for field, including the parts netviz ignores — ``qdisc``,
``valid_life_time``, ``info_slave_data`` — because a reader that only works on
trimmed input is a reader that does not work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from netviz.cli import cli, main
from netviz.importer import (
    Draft,
    DraftCable,
    DraftDevice,
    DraftInterface,
    DraftVlan,
    ImportSourceError,
    build_draft,
    build_files,
    element_name,
    interface_name,
    read_csv_links,
    read_inputs,
    read_iproute,
    read_lldp,
    render_device,
    write_files,
)
from netviz.importer.csvlinks import CsvError
from netviz.importer.emit import scalar
from netviz.loader import load_tree
from netviz.rules import Severity
from netviz.validate import validate

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

FIXTURES = Path(__file__).parent / "fixtures" / "import"
PC_ALICE = FIXTURES / "pc-alice.lldp.json"
SW_CORE = FIXTURES / "sw-core-01.lldp.json"
SRV_HYPER = FIXTURES / "srv-hyper.addr.json"
PATCH_PANEL = FIXTURES / "patch-panel.csv"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str, **kwargs: Any) -> Result:
    return runner.invoke(cli, list(args), catch_exceptions=False, **kwargs)


def payload(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def lldp_draft(*paths: Path) -> Draft:
    draft = Draft()
    for path in paths:
        read_lldp(payload(path), source=path.name, host=path.name.split(".")[0], draft=draft)
    draft.prune()
    draft.assign_cable_names()
    return draft


def iproute_draft(path: Path = SRV_HYPER, **kwargs: Any) -> Draft:
    draft = Draft()
    read_iproute(payload(path), source=path.name, host="srv-hyper", draft=draft, **kwargs)
    draft.prune()
    return draft


def documents(text: str) -> list[dict[str, Any]]:
    """Every YAML document of an emitted file, as plain mappings."""
    return [document for document in yaml.safe_load_all(text) if document is not None]


def interfaces_of(device: DraftDevice) -> dict[str, DraftInterface]:
    return device.interfaces


# --------------------------------------------------------------------------- #
# LLDP
# --------------------------------------------------------------------------- #


def test_lldp_turns_a_neighbour_record_into_a_cable() -> None:
    draft = lldp_draft(PC_ALICE)
    cable = next(
        cable for cable in draft.cables.values() if "sw-core-01" in {end[0] for end in cable.key}
    )
    assert cable.key == (("pc-alice", "eno1"), ("sw-core-01", "GigabitEthernet1/0/7"))


def test_lldp_reads_the_kind_from_the_advertised_capabilities() -> None:
    draft = lldp_draft(PC_ALICE, SW_CORE)
    assert draft.devices["sw-core-01"].kind == "switch"
    assert draft.devices["rtr-edge"].kind == "router"


def test_a_host_advertising_no_forwarding_capability_stays_a_computer() -> None:
    """The command must not promote a workstation to a router on a hunch."""
    draft = lldp_draft(SW_CORE)
    alice = draft.devices["pc-alice"]
    assert alice.kind == "computer"
    assert alice.kind_comment is not None and "neutral default" in alice.kind_comment


def test_lldp_creates_a_device_no_capture_covers() -> None:
    """``ap-lobby`` is named only by its neighbour, and is still an element."""
    draft = lldp_draft(PC_ALICE)
    lobby = draft.devices["ap-lobby"]
    assert list(lobby.interfaces) == ["eth0"]
    assert lobby.description == "UniFi AP"


def test_a_port_named_only_by_a_mac_id_takes_its_description_as_a_name() -> None:
    draft = lldp_draft(PC_ALICE)
    port = draft.devices["ap-lobby"].interfaces["eth0"]
    assert port.mac == "3c:ec:ef:11:22:34"


def test_the_two_directions_of_one_adjacency_are_one_cable() -> None:
    both = lldp_draft(PC_ALICE, SW_CORE)
    keys = [cable.key for cable in both.cables.values()]
    assert keys.count((("pc-alice", "eno1"), ("sw-core-01", "GigabitEthernet1/0/7"))) == 1
    # Three distinct adjacencies across the two captures, not four.
    assert len(both.cables) == 3


def test_a_deduplicated_cable_records_both_captures_that_saw_it() -> None:
    both = lldp_draft(PC_ALICE, SW_CORE)
    cable = both.cables[(("pc-alice", "eno1"), ("sw-core-01", "GigabitEthernet1/0/7"))]
    assert sorted(cable.sources) == ["pc-alice.lldp.json", "sw-core-01.lldp.json"]
    assert len(cable.comments) == 2


def test_the_capture_order_does_not_change_the_result() -> None:
    forward = build_files(lldp_draft(PC_ALICE, SW_CORE))
    backward = build_files(lldp_draft(SW_CORE, PC_ALICE))
    assert set(forward) == set(backward)
    assert forward["cables/links.yaml"].count("kind: cable") == 3


def test_a_management_address_is_noted_rather_than_placed_on_an_interface() -> None:
    """LLDP gives no interface and no prefix length, so neither may be invented."""
    draft = lldp_draft(PC_ALICE)
    switch = draft.devices["sw-core-01"]
    assert any("192.0.2.10" in note for note in switch.comments)
    assert not any(port.ipv4 or port.ipv6 for port in switch.interfaces.values())


def test_an_empty_lldp_capture_is_reported_rather_than_crashing() -> None:
    draft = Draft()
    read_lldp({"lldp": {}}, source="empty.json", host="pc1", draft=draft)
    draft.prune()
    assert not draft.cables
    assert any("no LLDP neighbours" in note for note in draft.notes)


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param({"lldp": {"interface": {"eth0": {}}}}, id="interface-as-mapping"),
        pytest.param({"lldp": {"interface": [{"eth0": {}}]}}, id="interface-as-list"),
        pytest.param({"interface": [{"name": "eth0"}]}, id="no-lldp-wrapper"),
        pytest.param({"lldp": []}, id="empty-list"),
        pytest.param([], id="bare-list"),
        pytest.param("nonsense", id="not-a-container"),
    ],
)
def test_lldp_survives_every_shape_lldpd_has_printed(shape: Any) -> None:
    """Two encodings, several releases, no stability guarantee: never traceback."""
    draft = Draft()
    read_lldp(shape, source="odd.json", host="pc1", draft=draft)
    draft.prune()
    assert not draft.cables


def test_lldp_json0_wrapping_is_understood() -> None:
    """``-f json0`` wraps every scalar in ``{"value": ...}`` and everything in lists."""
    draft = Draft()
    read_lldp(
        {
            "lldp": [
                {
                    "interface": [
                        {
                            "name": "eth0",
                            "chassis": [
                                {
                                    "id": [{"type": "mac", "value": "00:11:22:33:44:55"}],
                                    "name": [{"value": "sw-json0"}],
                                    "capability": [{"type": "Bridge", "enabled": True}],
                                }
                            ],
                            "port": [{"id": [{"type": "ifname", "value": "ge-0/0/1"}]}],
                        }
                    ]
                }
            ]
        },
        source="json0.json",
        host="pc1",
        draft=draft,
    )
    draft.prune()
    assert draft.devices["sw-json0"].kind == "switch"
    assert next(iter(draft.cables)) == (("pc1", "eth0"), ("sw-json0", "ge-0/0/1"))


def test_a_neighbour_with_no_system_name_is_named_after_its_chassis_id() -> None:
    draft = Draft()
    read_lldp(
        {
            "lldp": {
                "interface": {
                    "eth0": {
                        "chassis": {"id": {"type": "mac", "value": "aa:bb:cc:dd:ee:ff"}},
                        "port": {"id": {"type": "ifname", "value": "1"}},
                    }
                }
            }
        },
        source="anon.json",
        host="pc1",
        draft=draft,
    )
    draft.prune()
    assert "aa-bb-cc-dd-ee-ff" in draft.devices
    assert any(
        "advertised no system name" in note for note in draft.devices["aa-bb-cc-dd-ee-ff"].comments
    )


def test_a_neighbour_with_no_identity_at_all_is_reported_and_skipped() -> None:
    draft = Draft()
    read_lldp(
        {"lldp": {"interface": {"eth0": {"chassis": {}, "port": {}}}}},
        source="anon.json",
        host="pc1",
        draft=draft,
    )
    draft.prune()
    assert not draft.cables
    assert any("no neighbour chassis" in note for note in draft.notes)


# --------------------------------------------------------------------------- #
# iproute
# --------------------------------------------------------------------------- #


def test_iproute_maps_a_bridge_onto_its_members() -> None:
    bridge = interfaces_of(iproute_draft().devices["srv-hyper"])["br0"]
    assert bridge.type == "bridge"
    assert bridge.members == ["bond0"]
    assert bridge.ipv4 == ["192.168.10.5/24"]
    assert bridge.ipv6 == ["2001:db8:10::5/64"]


def test_iproute_maps_a_bond_onto_a_lag() -> None:
    bond = interfaces_of(iproute_draft().devices["srv-hyper"])["bond0"]
    assert bond.type == "lag"
    assert bond.members == ["eno1", "eno2"]
    assert bond.mtu == 9000


def test_iproute_maps_a_vlan_sub_interface_onto_parent_and_vid() -> None:
    ports = interfaces_of(iproute_draft().devices["srv-hyper"])
    sub = ports["eno3.100"]
    assert (sub.type, sub.parent) == ("vlan", "eno3")
    assert sub.vlan is not None and sub.vlan.access_vlan == 100


def test_a_sub_interfaces_parent_is_made_to_carry_the_vlan_it_encapsulates() -> None:
    """Otherwise ``E009`` rejects an entirely ordinary Linux host."""
    parent = interfaces_of(iproute_draft().devices["srv-hyper"])["eno3"]
    assert parent.vlan is not None
    assert (parent.vlan.mode, parent.vlan.trunk_vlans) == ("trunk", [100])
    assert parent.vlan.comment is not None and parent.vlan.comment.startswith("inferred:")


def test_the_vlan_database_is_written_from_the_ids_that_were_observed() -> None:
    assert iproute_draft().devices["srv-hyper"].vlans == {100}


def test_a_derived_mac_is_not_written_out() -> None:
    """br0, bond0 and eno3.100 all report eno1's MAC; writing it trips ``E003``."""
    ports = interfaces_of(iproute_draft().devices["srv-hyper"])
    assert ports["eno1"].mac == "3c:ec:ef:20:00:01"
    assert [ports[name].mac for name in ("br0", "bond0", "eno3.100")] == [None, None, None]


def test_an_administratively_down_port_keeps_its_state() -> None:
    ports = interfaces_of(iproute_draft().devices["srv-hyper"])
    assert ports["enp5s0"].enabled is False
    assert ports["eno1"].enabled is True


def test_link_and_host_scope_addresses_are_dropped() -> None:
    ports = interfaces_of(iproute_draft().devices["srv-hyper"])
    assert ports["eno3"].ipv6 == []
    assert all("fe80" not in address for address in ports["br0"].ipv6)


def test_the_loopback_is_not_imported_and_the_reason_is_reported() -> None:
    draft = iproute_draft()
    assert "lo" not in draft.devices["srv-hyper"].interfaces
    assert any("kernel loopback" in note for note in draft.notes)


def test_a_wireguard_interface_is_reported_rather_than_guessed_at() -> None:
    draft = iproute_draft()
    assert "wg0" not in draft.devices["srv-hyper"].interfaces
    assert any("wireguard tunnel" in note for note in draft.notes)


def test_a_dynamically_assigned_address_says_so() -> None:
    sub = interfaces_of(iproute_draft().devices["srv-hyper"])["eno3.100"]
    assert any("dynamically" in comment for comment in sub.comments)


def test_exclude_keeps_kernel_plumbing_out() -> None:
    with_veth = iproute_draft().devices["srv-hyper"].interfaces
    without = iproute_draft(exclude=["veth*"]).devices["srv-hyper"].interfaces
    assert "veth7f3a1b0" in with_veth
    assert "veth7f3a1b0" not in without


def test_link_and_addr_captures_of_one_host_merge() -> None:
    """``ip -j link show`` first, then ``ip -j addr show``: one device, both facts."""
    records = payload(SRV_HYPER)
    links = [
        {key: value for key, value in record.items() if key != "addr_info"} for record in records
    ]
    draft = Draft()
    read_iproute(links, source="link.json", host="srv-hyper", draft=draft)
    read_iproute(records, source="addr.json", host="srv-hyper", draft=draft)
    draft.prune()
    device = draft.devices["srv-hyper"]
    assert device.interfaces["br0"].ipv4 == ["192.168.10.5/24"]
    assert device.sources == ["link.json", "addr.json"]


def test_an_aggregate_with_no_members_is_dropped_with_a_reason() -> None:
    """``members`` may not be empty, so an unloadable document is not written."""
    draft = Draft()
    read_iproute(
        [{"ifname": "br-empty", "link_type": "ether", "linkinfo": {"info_kind": "bridge"}}],
        source="lonely.json",
        host="pc1",
        draft=draft,
    )
    draft.prune()
    assert "pc1" not in draft.devices
    assert any("no enslaved interface" in note for note in draft.notes)


def test_a_vlan_interface_without_a_parent_is_dropped_with_a_reason() -> None:
    draft = Draft()
    read_iproute(
        [{"ifname": "orphan.5", "link_type": "ether", "linkinfo": {"info_kind": "vlan"}}],
        source="orphan.json",
        host="pc1",
        draft=draft,
    )
    draft.prune()
    assert any("no parent link or no VLAN id" in note for note in draft.notes)


@pytest.mark.parametrize("shape", [{}, [], "text", {"links": "no"}, [1, 2, 3]])
def test_iproute_survives_input_that_is_not_a_capture(shape: Any) -> None:
    draft = Draft()
    read_iproute(shape, source="odd.json", host="pc1", draft=draft)
    draft.prune()
    assert not draft.devices


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #


def test_csv_rows_become_cables_and_both_devices() -> None:
    draft = Draft()
    read_csv_links(PATCH_PANEL.read_text(encoding="utf-8"), source="patch.csv", draft=draft)
    assert len(draft.cables) == 4
    assert "printer-hp" in draft.devices
    assert list(draft.devices["printer-hp"].interfaces) == ["eth0"]


def test_csv_honours_the_optional_medium_and_label_columns() -> None:
    draft = Draft()
    read_csv_links("a,1,b,2,fiber,A-014\n", source="p.csv", draft=draft)
    cable = next(iter(draft.cables.values()))
    assert (cable.medium, cable.medium_stated, cable.label) == ("fiber", True, "A-014")


def test_a_row_without_a_medium_says_the_value_was_filled_in() -> None:
    draft = Draft()
    read_csv_links("a,1,b,2\n", source="p.csv", draft=draft)
    cable = next(iter(draft.cables.values()))
    assert (cable.medium, cable.medium_stated) == ("copper", False)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a,1,b\n", "expected 4 to 6 fields"),
        ("a,1,b,2,glass\n", "is not a medium"),
        ("!!!,1,b,2\n", "holds no character"),
        ("a,!!!,b,2\n", "holds no character"),
    ],
)
def test_a_malformed_csv_row_is_refused_with_its_row_number(text: str, message: str) -> None:
    with pytest.raises(CsvError, match=message):
        read_csv_links(text, source="p.csv", draft=Draft())


def test_a_header_row_is_skipped_but_a_port_called_port1_is_not() -> None:
    draft = Draft()
    read_csv_links("switch,port_a,peer,peer_port\nsw,port1,pc,port2\n", source="p.csv", draft=draft)
    assert next(iter(draft.cables)) == (("pc", "port2"), ("sw", "port1"))


def test_an_empty_csv_is_reported_rather_than_refused() -> None:
    draft = Draft()
    read_csv_links("# nothing here\n\n", source="p.csv", draft=draft)
    assert not draft.cables
    assert any("no cable rows" in note for note in draft.notes)


# --------------------------------------------------------------------------- #
# Names and scalars
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sw-core-01", "sw-core-01"),
        ("sw-core.example.com", "sw-core.example.com"),
        ("SW CORE 01", "SW-CORE-01"),
        ("sw--core", "sw-core"),
        ("-sw-", "sw"),
        ("sw (rack 4)", "sw-rack-4"),
        ("!!!", None),
        ("", None),
    ],
)
def test_element_names_are_made_legal_deterministically(raw: str, expected: str | None) -> None:
    assert element_name(raw)[0] == expected


def test_a_renamed_value_reports_the_original() -> None:
    name, original = element_name("Port 1")
    assert (name, original) == ("Port-1", "Port 1")
    assert element_name("port1")[1] is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Gi0/1", "Gi0/1"), ("eno1.100", "eno1.100"), ("Port 1", "Port-1"), ("###", None)],
)
def test_interface_names_keep_the_slash_an_element_name_may_not(
    raw: str, expected: str | None
) -> None:
    assert interface_name(raw)[0] == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("eno1", "eno1"),
        ("00:1e:8c:aa:00:01", "00:1e:8c:aa:00:01"),
        ("2001:db8::1/64", "2001:db8::1/64"),
        ("yes", '"yes"'),
        ("Null", '"Null"'),
        ("12:30:45", '"12:30:45"'),
        ("1500", '"1500"'),
        ("desk: alice", '"desk: alice"'),
        ("- dash", '"- dash"'),
        ("with #hash", '"with #hash"'),
        (1500, "1500"),
        (True, "true"),
        (False, "false"),
    ],
)
def test_scalars_are_quoted_exactly_when_they_have_to_be(value: Any, expected: str) -> None:
    assert scalar(value) == expected


@pytest.mark.parametrize(
    "value", ["eno1", "yes", "12:30:45", "1500", "desk: alice", "# hash", "a\nb", "ä", '"q"', "*"]
)
def test_every_emitted_scalar_survives_a_yaml_round_trip(value: str) -> None:
    assert yaml.safe_load(f"key: {scalar(value)}") == {"key": value}


# --------------------------------------------------------------------------- #
# Emitting
# --------------------------------------------------------------------------- #


def test_a_device_document_writes_its_fields_in_schema_order() -> None:
    device = build_draft(read_inputs([str(SRV_HYPER)])).devices["srv-hyper"]
    document = documents(render_device(device))[0]
    assert list(document) == ["apiVersion", "kind", "metadata", "spec"]
    assert list(document["spec"]) == ["interfaces", "vlans"]
    first = document["spec"]["interfaces"][0]
    assert list(first)[:2] == ["name", "type"]


def test_an_interface_writes_its_fields_in_schema_order() -> None:
    device = DraftDevice(
        name="pc",
        interfaces={
            "eth0": DraftInterface(
                name="eth0",
                type="ethernet",
                description="d",
                enabled=False,
                mac="00:11:22:33:44:55",
                mtu=1500,
                ipv4=["10.0.0.1/24"],
                ipv6=["2001:db8::1/64"],
            )
        },
    )
    entry = documents(render_device(device))[0]["spec"]["interfaces"][0]
    assert list(entry) == ["name", "type", "description", "enabled", "mac", "mtu", "ipv4", "ipv6"]


def test_nothing_is_written_that_no_capture_covered() -> None:
    files = build_files(build_draft(read_inputs([str(SRV_HYPER), str(PC_ALICE)])))
    for text in files.values():
        for document in documents(text):
            spec = document.get("spec", {})
            assert "vendor" not in spec and "serial" not in spec and "location" not in spec
        assert "TODO" not in text


def test_every_inference_is_marked_as_one() -> None:
    files = build_files(build_draft(read_inputs([str(SRV_HYPER)])))
    device = files["devices/srv-hyper.yaml"]
    assert "# inferred: nothing in the captured output states what this device is" in device
    assert "kind: computer" in device


def test_multiple_addresses_are_written_as_a_block_list() -> None:
    device = DraftDevice(
        name="pc",
        interfaces={"eth0": DraftInterface(name="eth0", ipv4=["10.0.0.1/24", "10.0.1.1/24"])},
    )
    text = render_device(device)
    assert "addresses:\n          - 10.0.0.1/24\n          - 10.0.1.1/24" in text
    assert documents(text)[0]["spec"]["interfaces"][0]["ipv4"]["addresses"] == [
        "10.0.0.1/24",
        "10.0.1.1/24",
    ]


def test_the_schema_modeline_can_be_turned_off() -> None:
    device = DraftDevice(name="pc", interfaces={"e": DraftInterface(name="e")})
    assert "yaml-language-server" in render_device(device, schema=True)
    assert "yaml-language-server" not in render_device(device, schema=False)


def test_a_comment_cannot_escape_into_the_document() -> None:
    """A chassis description holding a newline must not end the comment."""
    device = DraftDevice(name="pc", interfaces={"e": DraftInterface(name="e")})
    device.note("evil\nkind: router\nmore")
    document = documents(render_device(device))[0]
    assert document["kind"] == "computer"


def test_a_pathological_value_cannot_flood_the_document() -> None:
    device = DraftDevice(name="pc", interfaces={"e": DraftInterface(name="e")})
    device.note("x" * 50_000)
    assert len(render_device(device)) < 5_000


def test_the_single_link_between_two_devices_is_named_after_them() -> None:
    draft = lldp_draft(PC_ALICE, SW_CORE)
    assert {cable.name for cable in draft.cables.values()} >= {"cbl-pc-alice-sw-core-01"}


def test_parallel_links_all_take_the_long_form_so_no_name_depends_on_the_other() -> None:
    draft = Draft()
    read_csv_links("a,1,b,1\na,2,b,2\n", source="p.csv", draft=draft)
    draft.assign_cable_names()
    assert sorted(cable.name for cable in draft.cables.values()) == ["cbl-a-1-b-1", "cbl-a-2-b-2"]


def test_two_devices_whose_names_differ_only_in_case_get_two_files() -> None:
    draft = Draft()
    for name in ("SW1", "sw1"):
        draft.device(name).interface("eth0")
    assert sorted(build_files(draft)) == ["devices/SW1.yaml", "devices/sw1-2.yaml"]


# --------------------------------------------------------------------------- #
# Reading inputs
# --------------------------------------------------------------------------- #


def test_the_host_comes_from_the_file_name_when_nothing_else_says() -> None:
    entry = read_inputs([str(PC_ALICE)])[0]
    assert (entry.host, entry.host_from_filename) == ("pc-alice", True)


def test_host_overrides_the_file_name() -> None:
    entry = read_inputs([str(PC_ALICE)], host="somewhere-else")[0]
    assert (entry.host, entry.host_from_filename) == ("somewhere-else", False)


def test_a_name_equals_path_argument_names_one_input() -> None:
    entries = read_inputs([f"first={PC_ALICE}", str(SW_CORE)], host="fallback")
    assert [entry.host for entry in entries] == ["first", "fallback"]


def test_a_path_holding_an_equals_sign_is_still_a_path(tmp_path: Path) -> None:
    path = tmp_path / "a=b.csv"
    path.write_text("a,1,b,2\n", encoding="utf-8")
    assert read_inputs([str(path)])[0].name == "a=b.csv"


def test_stdin_is_read_once_and_only_once() -> None:
    import io

    assert read_inputs(["-"], host="pc1", stdin=io.StringIO("[]"))[0].text == "[]"
    with pytest.raises(ImportSourceError, match="only once"):
        read_inputs(["-", "-"], host="pc1", stdin=io.StringIO("[]"))


@pytest.mark.parametrize(
    ("specs", "message"),
    [
        ([], "no input given"),
        (["/nonexistent/capture.json"], "no such file"),
    ],
)
def test_unreadable_inputs_are_refused(specs: list[str], message: str) -> None:
    with pytest.raises(ImportSourceError, match=message):
        read_inputs(specs)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(ImportSourceError, match="is empty"):
        read_inputs([str(path)])


def test_a_directory_is_refused_with_the_glob_to_use(tmp_path: Path) -> None:
    with pytest.raises(ImportSourceError, match="is a directory"):
        read_inputs([str(tmp_path)])


def test_a_host_that_is_not_a_legal_name_is_refused() -> None:
    with pytest.raises(ImportSourceError, match="not a usable element name"):
        read_inputs([str(PC_ALICE)], host="not a name!")


def test_non_utf8_input_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    path.write_bytes(b"\xff\xfe not text")
    with pytest.raises(ImportSourceError, match="not UTF-8"):
        read_inputs([str(path)])


# --------------------------------------------------------------------------- #
# Dialects
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [(PC_ALICE, "lldp"), (SRV_HYPER, "iproute"), (PATCH_PANEL, "csv")],
)
def test_the_dialect_of_each_input_is_sniffed(path: Path, expected: str) -> None:
    """Sniffing is what makes ``netviz import collected/*`` work."""
    draft = build_draft(read_inputs([str(path)]))
    assert bool(draft.devices)
    forced = build_draft(read_inputs([str(path)]), dialect=expected)
    assert set(forced.devices) == set(draft.devices)


def test_malformed_json_is_refused_with_the_parser_message(tmp_path: Path) -> None:
    path = tmp_path / "broken.lldp.json"
    path.write_text('{"lldp": ', encoding="utf-8")
    with pytest.raises(ImportSourceError, match="not valid JSON"):
        build_draft(read_inputs([str(path)]))


def test_json_that_is_no_known_capture_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    path.write_text('{"something": "else"}', encoding="utf-8")
    with pytest.raises(ImportSourceError, match="name the dialect with --from"):
        build_draft(read_inputs([str(path)]))


def test_a_capture_that_names_no_host_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    path.write_text("[]", encoding="utf-8")
    entries = read_inputs([str(path)])
    entries[0].host = None
    with pytest.raises(ImportSourceError, match="does not name it"):
        build_draft(entries, dialect="iproute")


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def test_writing_refuses_to_clobber_without_force(tmp_path: Path) -> None:
    files = build_files(build_draft(read_inputs([str(PATCH_PANEL)])))
    write_files(files, tmp_path)
    (tmp_path / "cables" / "links.yaml").write_text("mine\n", encoding="utf-8")
    with pytest.raises(ImportSourceError, match="refusing to overwrite"):
        write_files(files, tmp_path)
    assert (tmp_path / "cables" / "links.yaml").read_text(encoding="utf-8") == "mine\n"

    write_files(files, tmp_path, force=True)
    assert "kind: cable" in (tmp_path / "cables" / "links.yaml").read_text(encoding="utf-8")


def test_writing_into_a_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "afile"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ImportSourceError, match="not a directory"):
        write_files({"a.yaml": "x"}, target)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


@pytest.fixture
def imported(runner: CliRunner, tmp_path: Path) -> Path:
    target = tmp_path / "net"
    result = invoke(
        runner,
        "import",
        "-o",
        str(target),
        "--exclude",
        "veth*",
        str(PC_ALICE),
        str(SW_CORE),
        str(SRV_HYPER),
        str(PATCH_PANEL),
    )
    assert result.exit_code == 0, result.output
    return target


def test_the_imported_tree_loads_without_an_error(imported: Path) -> None:
    inventory = load_tree(imported)
    assert not inventory.errors
    assert len(inventory.devices) == 7
    assert len(inventory.cables) == 7


def test_the_imported_tree_validates_with_no_error_level_finding(imported: Path) -> None:
    findings = validate(load_tree(imported))
    assert [finding for finding in findings if finding.severity is Severity.ERROR] == []


def test_the_imported_tree_renders(runner: CliRunner, imported: Path) -> None:
    result = invoke(runner, "-i", str(imported), "render", "-f", "json")
    assert result.exit_code == 0, result.output
    assert '"nodes"' in result.output


@requires_dot
@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
def test_the_imported_tree_renders_at_every_layer(
    runner: CliRunner, imported: Path, tmp_path: Path, layer: str
) -> None:
    output = tmp_path / f"{layer}.svg"
    result = invoke(
        runner, "-i", str(imported), "render", "--layer", layer, "-f", "svg", "-o", str(output)
    )
    assert result.exit_code == 0, result.output
    assert output.read_text(encoding="utf-8").startswith("<?xml")


def test_the_import_report_says_which_findings_are_expected(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = invoke(runner, "import", "-o", str(tmp_path / "net"), str(PC_ALICE))
    assert result.exit_code == 0, result.output
    assert "are expected of an imported tree" in result.output
    assert "not errors in what was imported" in result.output


def test_the_import_report_lists_what_was_left_out(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(runner, "import", "-o", str(tmp_path / "net"), str(SRV_HYPER))
    assert "kernel loopback" in result.output
    assert "wireguard tunnel" in result.output


def test_dry_run_writes_nothing_and_prints_the_tree(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "net"
    result = invoke(runner, "import", "--dry-run", "-o", str(target), str(PATCH_PANEL))
    assert result.exit_code == 0, result.output
    assert not target.exists()
    assert "# ===== devices/printer-hp.yaml =====" in result.output
    assert "kind: cable" in result.output


def test_the_dry_run_output_is_the_tree_that_would_be_written(
    runner: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "net"
    dry = invoke(runner, "import", "--dry-run", "-o", str(target), str(PATCH_PANEL))
    invoke(runner, "import", "-o", str(target), str(PATCH_PANEL))
    for path in sorted(target.rglob("*.yaml")):
        assert path.read_text(encoding="utf-8").rstrip("\n") in dry.output


def test_import_reads_a_capture_from_stdin(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(
        runner,
        "import",
        "--host",
        "pc1",
        "--dry-run",
        "-",
        input=SRV_HYPER.read_text(encoding="utf-8"),
    )
    assert result.exit_code == 0, result.output
    assert "name: pc1" in result.output


def test_an_import_that_produces_nothing_fails(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "none.json"
    path.write_text('{"lldp": {}}', encoding="utf-8")
    result = invoke(runner, "import", "--host", "pc1", "--dry-run", str(path))
    assert result.exit_code == 1
    assert "nothing was imported" in result.output


def test_the_written_tree_carries_a_schema_and_a_modeline(imported: Path) -> None:
    assert (imported / "schema" / "netviz.schema.json").is_file()
    text = (imported / "devices" / "pc-alice.yaml").read_text(encoding="utf-8")
    assert text.startswith("# yaml-language-server: $schema=../schema/netviz.schema.json")


def test_no_schema_leaves_the_editor_unwired(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "net"
    invoke(runner, "import", "--no-schema", "-o", str(target), str(PATCH_PANEL))
    assert not (target / "schema").exists()
    assert "yaml-language-server" not in (target / "cables" / "links.yaml").read_text("utf-8")


def test_a_second_import_into_the_same_tree_is_refused(imported: Path) -> None:
    """Driven through ``main``, where the error becomes an exit status."""
    before = (imported / "cables" / "links.yaml").read_text(encoding="utf-8")
    assert main(["import", "-o", str(imported), str(PATCH_PANEL)]) == ImportSourceError.exit_code
    assert (imported / "cables" / "links.yaml").read_text(encoding="utf-8") == before


def test_force_replaces_what_is_already_there(imported: Path) -> None:
    assert main(["import", "-o", str(imported), "--force", str(PATCH_PANEL)]) == 0
    assert "patch-panel.csv" in (imported / "cables" / "links.yaml").read_text(encoding="utf-8")


def test_import_is_idempotent(runner: CliRunner, imported: Path, tmp_path: Path) -> None:
    """Re-running on the same captures must produce the same bytes."""
    again = tmp_path / "again"
    invoke(
        runner,
        "import",
        "-o",
        str(again),
        "--exclude",
        "veth*",
        str(PC_ALICE),
        str(SW_CORE),
        str(SRV_HYPER),
        str(PATCH_PANEL),
    )
    for path in sorted(imported.rglob("*.yaml")):
        twin = again / path.relative_to(imported)
        assert twin.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")


def test_a_draft_cable_between_pruned_devices_is_dropped() -> None:
    draft = Draft()
    draft.device("ghost")
    draft.add_cable(DraftCable(endpoints=(("ghost", "e0"), ("also-ghost", "e0"))))
    draft.prune()
    assert not draft.cables and not draft.devices
    assert any("was dropped because" in note for note in draft.notes)


# --------------------------------------------------------------------------- #
# Diagnostics: every skip is reported, none is silent
# --------------------------------------------------------------------------- #


def lldp_notes(record: dict[str, Any], *, host: str = "pc1") -> tuple[Draft, list[str]]:
    draft = Draft()
    read_lldp({"lldp": {"interface": [record]}}, source="odd.json", host=host, draft=draft)
    return draft, draft.notes


@pytest.mark.parametrize(
    ("record", "message"),
    [
        pytest.param({"via": "LLDP"}, "carries no name", id="unnamed-interface"),
        pytest.param({"name": "###", "via": "LLDP"}, "holds no usable", id="unusable-interface"),
        pytest.param(
            {"name": "eth0", "chassis": {"###": {"id": {}}}},
            "holds no usable characters",
            id="unusable-neighbour",
        ),
        pytest.param(
            {"name": "eth0", "chassis": {"sw": {"descr": "x"}}, "port": {}},
            "no usable port id",
            id="portless-neighbour",
        ),
        pytest.param(
            {"name": "eth0", "chassis": {"sw": {}}, "port": {"id": {"value": "###"}}},
            "holds no usable characters",
            id="unusable-port",
        ),
        pytest.param(
            {"name": "eth0", "chassis": {"id": {"type": "mac"}}},
            "neither a system name nor a chassis id",
            id="anonymous-chassis",
        ),
    ],
)
def test_every_lldp_record_netviz_cannot_use_is_reported(
    record: dict[str, Any], message: str
) -> None:
    draft, notes = lldp_notes(record)
    assert any(message in note for note in notes), notes
    assert not draft.cables


def test_a_renamed_port_records_what_the_device_calls_it() -> None:
    draft, _ = lldp_notes(
        {
            "name": "Uplink 1",
            "chassis": {"sw core": {"descr": "x"}},
            "port": {"id": {"type": "ifname", "value": "Port 3"}},
        }
    )
    local = draft.devices["pc1"].interfaces["Uplink-1"]
    assert any("'Uplink 1'" in comment for comment in local.comments)
    switch = draft.devices["sw-core"]
    assert any("'sw core'" in comment for comment in switch.comments)
    assert any("'Port 3'" in comment for comment in switch.interfaces["Port-3"].comments)


def test_several_management_addresses_are_all_noted() -> None:
    draft, _ = lldp_notes(
        {
            "name": "eth0",
            "chassis": {"sw": {"mgmt-ip": ["192.0.2.1", "2001:db8::1"], "descr": "x"}},
            "port": {"id": {"type": "ifname", "value": "1"}},
        }
    )
    notes = draft.devices["sw"].comments
    assert sum("management address" in note for note in notes) == 2


def test_a_capability_flag_spelled_as_a_string_still_counts() -> None:
    draft, _ = lldp_notes(
        {
            "name": "eth0",
            "chassis": {"sw": {"capability": {"type": "Repeater", "enabled": "true"}}},
            "port": {"id": {"type": "ifname", "value": "1"}},
        }
    )
    assert draft.devices["sw"].kind == "hub"


def test_a_link_record_with_no_name_is_reported() -> None:
    draft = Draft()
    read_iproute([{"mtu": 1500}], source="odd.json", host="pc1", draft=draft)
    assert any("no 'ifname'" in note for note in draft.notes)


def test_a_kernel_type_with_no_netviz_equivalent_says_which_it_was() -> None:
    draft = Draft()
    read_iproute(
        [{"ifname": "vrf-red", "link_type": "ether", "linkinfo": {"info_kind": "vrf"}}],
        source="odd.json",
        host="pc1",
        draft=draft,
    )
    assert any("vrf" in note and "no netviz interface type" in note for note in draft.notes)


def test_a_veth_style_link_is_written_as_ethernet_and_says_so() -> None:
    draft = Draft()
    read_iproute(
        [{"ifname": "dummy0", "link_type": "ether", "linkinfo": {"info_kind": "dummy"}}],
        source="odd.json",
        host="pc1",
        draft=draft,
    )
    port = draft.devices["pc1"].interfaces["dummy0"]
    assert port.type == "ethernet"
    assert any("'dummy' link" in comment for comment in port.comments)


def test_a_qinq_sub_interface_is_flagged_rather_than_silently_treated_as_dot1q() -> None:
    draft = Draft()
    read_iproute(
        [
            {"ifname": "eno1", "link_type": "ether"},
            {
                "ifname": "eno1.7",
                "link": "eno1",
                "link_type": "ether",
                "linkinfo": {"info_kind": "vlan", "info_data": {"id": 7, "protocol": "802.1ad"}},
            },
        ],
        source="odd.json",
        host="pc1",
        draft=draft,
    )
    port = draft.devices["pc1"].interfaces["eno1.7"]
    assert any("802.1ad" in comment for comment in port.comments)


def test_an_infiniband_hardware_address_is_not_written_as_a_mac() -> None:
    draft = Draft()
    read_iproute(
        [{"ifname": "ib0", "link_type": "ether", "address": ":".join(["00"] * 20)}],
        source="odd.json",
        host="pc1",
        draft=draft,
    )
    assert draft.devices["pc1"].interfaces["ib0"].mac is None


def test_a_capture_wrapped_in_an_object_is_still_read() -> None:
    draft = Draft()
    read_iproute(
        {"links": [{"ifname": "eno1", "link_type": "ether"}]},
        source="wrapped.json",
        host="pc1",
        draft=draft,
    )
    assert "eno1" in draft.devices["pc1"].interfaces


def test_two_inputs_that_disagree_about_a_kind_keep_the_first_and_say_so() -> None:
    draft = Draft()
    draft.device("box").refine_kind("switch", "from input A")
    draft.device("box").refine_kind("router", "from input B")
    assert draft.devices["box"].kind == "switch"
    assert any(
        "another input reported this device as a router" in note
        for note in draft.devices["box"].comments
    )


def test_a_kind_a_later_input_observed_displaces_the_neutral_default() -> None:
    draft = Draft()
    draft.device("box")
    draft.device("box").refine_kind("router", "observed")
    assert draft.devices["box"].kind == "router"
    assert draft.devices["box"].kind_comment == "observed"


def test_two_links_whose_generated_names_collide_stay_apart() -> None:
    """``a`` + ``b-c`` and ``a-b`` + ``c`` both read ``cbl-a-b-c``."""
    draft = Draft()
    read_csv_links("a,1,b-c,1\na-b,1,c,1\n", source="p.csv", draft=draft)
    draft.assign_cable_names()
    assert sorted(cable.name for cable in draft.cables.values()) == ["cbl-a-b-c", "cbl-a-b-c-2"]


def test_a_stated_medium_survives_the_deduplication_of_an_adjacency() -> None:
    draft = Draft()
    read_csv_links("a,1,b,2\n", source="first.csv", draft=draft)
    read_csv_links("b,2,a,1,fiber\n", source="second.csv", draft=draft)
    cable = next(iter(draft.cables.values()))
    assert (cable.medium, cable.medium_stated) == ("fiber", True)


def test_two_captures_of_one_port_merge_their_vlan_blocks() -> None:
    """A trunk seen twice is one trunk carrying the union of both VLAN sets."""
    device = DraftDevice(name="sw")
    device.add_interface(DraftInterface(name="e", vlan=DraftVlan(mode="trunk", trunk_vlans=[10])))
    device.add_interface(DraftInterface(name="e", vlan=DraftVlan(mode="trunk", trunk_vlans=[20])))
    vlan = device.interfaces["e"].vlan
    assert vlan is not None and vlan.trunk_vlans == [10, 20]


def test_a_more_specific_type_displaces_the_ethernet_fallback() -> None:
    """A neighbour that only named the port cannot know it is a bond."""
    device = DraftDevice(name="sw")
    device.add_interface(DraftInterface(name="bond0"))
    device.add_interface(DraftInterface(name="bond0", type="lag", members=["e1"]))
    assert device.interfaces["bond0"].type == "lag"


def test_a_refusal_summarises_a_long_list_of_clashes(tmp_path: Path) -> None:
    files = {f"devices/d{index}.yaml": "x\n" for index in range(12)}
    write_files(files, tmp_path)
    with pytest.raises(ImportSourceError, match="and 4 more"):
        write_files(files, tmp_path)


def test_an_unwritable_target_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``Path.open`` rather than ``Path.write_text``: netviz.fsio opens with an
    # explicit ``newline=""`` (see the module docstring there), so a patch on
    # ``write_text`` would intercept nothing and this test would assert that a
    # successful write raises.
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", refuse)
    with pytest.raises(ImportSourceError, match="cannot write"):
        write_files({"a.yaml": "x"}, tmp_path)


def test_an_empty_stdin_is_refused() -> None:
    import io

    with pytest.raises(ImportSourceError, match="stdin was empty"):
        read_inputs(["-"], stdin=io.StringIO("   "))


def test_an_oversized_input_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from netviz.importer import run as run_module

    path = tmp_path / "huge.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(run_module, "MAX_INPUT_BYTES", 1)
    with pytest.raises(ImportSourceError, match="past the"):
        read_inputs([str(path)])


def test_the_output_trees_own_configuration_governs_the_report(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Importing into a tree that ignores a rule must not report it anyway."""
    target = tmp_path / "net"
    target.mkdir()
    (target / "netviz.toml").write_text('[validate]\nignore = ["W101"]\n', encoding="utf-8")
    result = invoke(runner, "import", "-o", str(target), str(PC_ALICE))
    assert result.exit_code == 0, result.output
    assert "W101" not in result.output


def test_an_imported_tree_that_does_not_validate_fails_the_run(tmp_path: Path) -> None:
    """The files are still written — you cannot fix what you cannot read."""
    target = tmp_path / "net"
    target.mkdir()
    (target / "netviz.toml").write_text('[validate.severity]\nW101 = "error"\n', encoding="utf-8")
    assert main(["import", "-o", str(target), str(PC_ALICE)]) == 1
    assert (target / "cables" / "links.yaml").is_file()
