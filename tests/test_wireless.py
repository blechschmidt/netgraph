"""Wireless: the radio model, the five semantic rules and the L2 annotation.

Everything §6.2.6 adds, in the four places it has to hold together:

* the **model** — the band/channel plan, the widths a band supports, the BSS
  list and the one-association-per-client rule, all reported at schema time
  with an ``NG-W*`` id and the path of the offending value;
* the **validator** — the five cross-document rules, each on an inventory that
  differs from a clean one in exactly the way the rule is about, so a finding
  cannot be an accident of the fixture;
* the **renderers** — that a radio link is annotated ``SSID @ channel/band`` at
  layer 2, keeps its dashed styling, and says the same thing in DOT, Mermaid,
  JSON and a tooltip;
* the **CLI** — ``list bss``, in all three output formats.

``tests/fixtures/invalid/`` holds one file per rule and ``tests/test_examples.py``
insists each fires exactly once there; the inventories here are built inline so
a test can vary one field at a time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.errors import SchemaError
from netgraph.loader import Inventory, load_tree
from netgraph.models import (
    API_VERSION,
    Band,
    Bss,
    Interface,
    RadioRole,
    Security,
    WirelessConfig,
    parse_document,
)
from netgraph.models.interface import CHANNEL_WIDTHS
from netgraph.render.details import _wireless_text
from netgraph.render.dot import to_dot
from netgraph.render.graph import Layer, build_graph
from netgraph.render.jsonexport import graph_to_dict
from netgraph.render.mermaid import to_mermaid
from netgraph.validate import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_LAB = REPO_ROOT / "examples" / "home-lab"


# --------------------------------------------------------------------------- #
# Building inventories
# --------------------------------------------------------------------------- #


def radio(role: str = "ap", **wireless: Any) -> dict[str, Any]:
    """One ``wifi`` interface with a ``wireless`` block, as a document fragment."""
    return {
        "name": "wlan0",
        "type": "wifi",
        "mtu": 1500,
        "wireless": {"role": role, **wireless},
    }


def device(name: str, *interfaces: dict[str, Any], **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": spec.pop("kind", "computer"),
        "metadata": {"name": name},
        "spec": {"interfaces": list(interfaces), **spec},
    }


def link(name: str, left: str, right: str, medium: str = "wireless") -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": "cable",
        "metadata": {"name": name},
        "spec": {"endpoints": [left, right], "medium": medium},
    }


def inventory_of(root: Path, *documents: dict[str, Any]) -> Inventory:
    """Load an inventory made of the given documents, insisting it parses."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "net.yaml").write_text(
        "---\n".join(yaml.safe_dump(document, sort_keys=False) for document in documents),
        encoding="utf-8",
    )
    loaded = load_tree(root)
    assert loaded.errors == [], "\n".join(str(error) for error in loaded.errors)
    return loaded


def rules_of(inventory: Inventory) -> list[str]:
    return [finding.rule for finding in validate(inventory)]


def parse_radio(**wireless: Any) -> WirelessConfig:
    """Parse one interface carrying ``wireless``, or raise :class:`SchemaError`."""
    element = parse_document(device("pc", radio(**wireless)))
    interface = element.spec.interfaces[0]
    assert isinstance(interface, Interface) and interface.wireless is not None
    return interface.wireless


def issues(exc: pytest.ExceptionInfo[SchemaError]) -> list[tuple[str | None, str]]:
    """``(rule, last path component)`` per issue, which is what a test asserts on."""
    return [(issue.rule, str(issue.path[-1]) if issue.path else "") for issue in exc.value.issues]


@pytest.fixture
def association(tmp_path: Path) -> Inventory:
    """One access point, one station, one association. Clean."""
    return inventory_of(
        tmp_path,
        device("ap", radio("ap", band="5GHz", channel=36, width_mhz=80, bss=[{"ssid": "lab"}])),
        device(
            "pc",
            {
                **radio("station", band="5GHz", channel=36, bss=[{"ssid": "lab"}]),
                "ipv4": ["10.0.0.2/30"],
            },
        ),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )


# --------------------------------------------------------------------------- #
# The band and channel plan
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("band", "channel", "centre"),
    [
        (Band.B2_4, 1, 2412),
        (Band.B2_4, 6, 2437),
        (Band.B2_4, 13, 2472),
        # Channel 14 is the exception: 12 MHz above 13, not 5.
        (Band.B2_4, 14, 2484),
        (Band.B5, 36, 5180),
        (Band.B5, 165, 5825),
        (Band.B6, 1, 5955),
        (Band.B6, 233, 7115),
    ],
)
def test_a_channel_maps_to_its_centre_frequency(band: Band, channel: int, centre: int) -> None:
    assert band.centre_mhz(channel) == centre


def test_the_same_channel_number_is_a_different_frequency_in_each_band() -> None:
    """Why `channel` requires `band`: 1 is 2412 MHz or 5955 MHz."""
    assert Band.B2_4.centre_mhz(1) != Band.B6.centre_mhz(1)
    assert 1 in Band.B2_4.channels and 1 in Band.B6.channels


def test_a_band_numbers_only_its_own_channels() -> None:
    assert Band.B2_4.channels == frozenset(range(1, 15))
    assert 36 in Band.B5.channels and 36 not in Band.B2_4.channels
    # The 6 GHz 20 MHz channels step by four; 2 is not one of them.
    assert 5 in Band.B6.channels and 2 not in Band.B6.channels
    with pytest.raises(KeyError):
        Band.B5.centre_mhz(37)


def test_only_6ghz_has_room_for_320_mhz() -> None:
    assert Band.B2_4.widths == frozenset({20, 40})
    assert Band.B5.widths == frozenset({20, 40, 80, 160})
    assert Band.B6.widths == frozenset(CHANNEL_WIDTHS)


def test_the_span_is_centred_on_the_primary_channel() -> None:
    wireless = parse_radio(band="5GHz", channel=36, width_mhz=80)
    assert wireless.span_mhz() == (5140.0, 5220.0)
    # A radio with no channel occupies nothing this rule can compare.
    assert parse_radio(band="5GHz").span_mhz() is None
    # 20 MHz is assumed when the width is not stated.
    assert parse_radio(band="2.4GHz", channel=1).span_mhz() == (2402.0, 2422.0)


# --------------------------------------------------------------------------- #
# The model (NG-W001 … NG-W006)
# --------------------------------------------------------------------------- #


def test_a_radio_reports_its_channel_the_way_a_diagram_labels_it() -> None:
    assert parse_radio(band="5GHz", channel=36).channel_text == "36/5GHz"
    # A band with no channel still says something; nothing at all says None.
    assert parse_radio(band="6GHz").channel_text == "6GHz"
    assert parse_radio().channel_text is None


def test_wireless_is_refused_on_anything_but_a_radio() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device("pc", {"name": "eth0", "type": "ethernet", "wireless": {"role": "ap"}})
        )
    assert issues(exc) == [("NG-W002", "wireless")]


@pytest.mark.parametrize(
    ("wireless", "rule", "field"),
    [
        ({"band": "2.4GHz", "channel": 36}, "NG-W003", "channel"),
        ({"band": "6GHz", "channel": 2}, "NG-W003", "channel"),
        ({"channel": 6}, "NG-W003", "channel"),
        ({"width_mhz": 40}, "NG-W003", "width_mhz"),
        ({"band": "2.4GHz", "channel": 6, "width_mhz": 80}, "NG-W004", "width_mhz"),
        ({"band": "5GHz", "channel": 36, "width_mhz": 320}, "NG-W004", "width_mhz"),
    ],
)
def test_the_frequency_settings_have_to_agree_with_the_band(
    wireless: dict[str, Any], rule: str, field: str
) -> None:
    with pytest.raises(SchemaError) as exc:
        parse_radio(**wireless)
    assert issues(exc) == [(rule, field)]


def test_a_channel_outside_every_plan_is_a_range_error() -> None:
    """234 is not a channel of any band, so the scalar bound catches it first."""
    with pytest.raises(SchemaError) as exc:
        parse_radio(band="6GHz", channel=234)
    assert [issue.path[-1] for issue in exc.value.issues] == ["channel"]


@pytest.mark.parametrize("width", [0, 30, 640, "80"])
def test_a_width_that_is_not_a_channel_width_is_refused(width: Any) -> None:
    with pytest.raises(SchemaError):
        parse_radio(band="6GHz", width_mhz=width)


def test_an_ssid_is_bounded_in_octets_not_characters() -> None:
    """IEEE 802.11 carries 32 *bytes*; twenty umlauts are forty of them."""
    assert parse_radio(bss=[{"ssid": "a" * 32}]).ssids == ("a" * 32,)
    with pytest.raises(SchemaError) as exc:
        parse_radio(bss=[{"ssid": "ä" * 20}])
    assert "octets" in str(exc.value)
    with pytest.raises(SchemaError):
        parse_radio(bss=[{"ssid": ""}])


def test_an_ssid_that_is_not_a_string_is_refused_rather_than_stringified() -> None:
    """An unquoted `5` is a number to YAML; turning it into "5" would hide that."""
    with pytest.raises(SchemaError):
        parse_radio(bss=[{"ssid": 5}])


def test_a_radio_may_not_declare_one_ssid_or_one_bssid_twice() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_radio(bss=[{"ssid": "lab"}, {"ssid": "lab"}])
    assert issues(exc) == [("NG-W005", "ssid")]

    with pytest.raises(SchemaError) as exc:
        parse_radio(
            bss=[
                {"ssid": "lab", "bssid": "02:00:5e:00:00:01"},
                {"ssid": "guest", "bssid": "02-00-5E-00-00-01"},
            ]
        )
    assert issues(exc) == [("NG-W005", "bssid")]


@pytest.mark.parametrize("role", ["station", "mesh"])
def test_a_client_radio_joins_one_bss_at_a_time(role: str) -> None:
    with pytest.raises(SchemaError) as exc:
        parse_radio(role=role, bss=[{"ssid": "a"}, {"ssid": "b"}])
    assert issues(exc) == [("NG-W006", "bss")]
    # An access point serving two SSIDs is the ordinary case.
    assert len(parse_radio(role="ap", bss=[{"ssid": "a"}, {"ssid": "b"}]).bss) == 2


def test_the_bss_defaults_are_the_honest_ones() -> None:
    """`security` unset means "not recorded", which is not the same as `open`."""
    entry = Bss(ssid="lab")
    assert entry.security is None
    assert entry.hidden is False
    assert entry.bssid is None and entry.vlan is None
    assert Security.OPEN.is_encrypted is False
    assert all(value.is_encrypted for value in Security if value is not Security.OPEN)


def test_a_role_knows_which_side_of_the_association_it_is() -> None:
    assert RadioRole.AP.is_ap and not RadioRole.AP.is_client
    assert RadioRole.STATION.is_client and RadioRole.MESH.is_client


def test_a_bssid_is_normalised_like_every_other_mac() -> None:
    wireless = parse_radio(bss=[{"ssid": "lab", "bssid": "78-8A-20-AA-00-11"}])
    assert wireless.bss[0].bssid == "78:8a:20:aa:00:11"


# --------------------------------------------------------------------------- #
# E028 — the link is an association
# --------------------------------------------------------------------------- #


def test_a_clean_association_reports_nothing(association: Inventory) -> None:
    assert validate(association) == []


def test_two_access_points_on_one_link_are_not_an_association(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("ap-a", {**radio("ap", bss=[{"ssid": "lab"}]), "ipv4": ["10.0.0.1/30"]}),
        device("ap-b", {**radio("ap", bss=[{"ssid": "lab"}]), "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap-a:wlan0", "ap-b:wlan0"),
    )
    (finding,) = validate(inventory)
    assert finding.rule == "E028"
    assert "two 'ap' radios" in finding.message
    assert finding.elements == ("wl", "ap-a", "ap-b")


def test_two_clients_on_one_link_have_nothing_to_associate_to(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("pc-a", {**radio("station"), "ipv4": ["10.0.0.1/30"]}),
        device("pc-b", {**radio("mesh"), "ipv4": ["10.0.0.2/30"]}),
        link("wl", "pc-a:wlan0", "pc-b:wlan0"),
    )
    (finding,) = validate(inventory)
    assert finding.rule == "E028"
    assert "joins no 'ap' radio" in finding.message


@pytest.mark.parametrize("role", ["station", "mesh"])
def test_an_ap_to_client_link_is_what_the_rule_wants(tmp_path: Path, role: str) -> None:
    inventory = inventory_of(
        tmp_path,
        device("ap", {**radio("ap", bss=[{"ssid": "lab"}]), "ipv4": ["10.0.0.1/30"]}),
        device("pc", {**radio(role, bss=[{"ssid": "lab"}]), "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    assert rules_of(inventory) == []


def test_a_radio_that_models_no_role_is_left_alone(tmp_path: Path) -> None:
    """An absent `wireless` block says "not modelled", not "not an access point"."""
    inventory = inventory_of(
        tmp_path,
        device("ap", {**radio("ap", bss=[{"ssid": "lab"}]), "ipv4": ["10.0.0.1/30"]}),
        device("pc", {"name": "wlan0", "type": "wifi", "mtu": 1500, "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    assert rules_of(inventory) == []


def test_a_wired_link_between_two_access_points_is_not_this_rule(tmp_path: Path) -> None:
    """E028 is about associations. Two APs cabled together is ordinary."""
    inventory = inventory_of(
        tmp_path,
        device(
            "ap-a",
            {"name": "eth0", "type": "ethernet", "mtu": 1500, "ipv4": ["10.0.0.1/30"]},
            {**radio("ap", bss=[{"ssid": "lab"}]), "enabled": False},
        ),
        device(
            "ap-b",
            {"name": "eth0", "type": "ethernet", "mtu": 1500, "ipv4": ["10.0.0.2/30"]},
            {**radio("ap", bss=[{"ssid": "lab"}]), "enabled": False},
        ),
        link("cbl", "ap-a:eth0", "ap-b:eth0", medium="copper"),
    )
    assert rules_of(inventory) == []


# --------------------------------------------------------------------------- #
# E029 — one BSSID, one BSS
# --------------------------------------------------------------------------- #


def test_two_access_points_may_not_share_a_bssid(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "ap-a",
            {"name": "eth0", "type": "ethernet", "mtu": 1500, "ipv4": ["10.0.0.1/30"]},
            {**radio("ap", bss=[{"ssid": "lab", "bssid": "02:00:5e:00:00:01"}]), "enabled": False},
        ),
        device(
            "ap-b",
            {"name": "eth0", "type": "ethernet", "mtu": 1500, "ipv4": ["10.0.0.2/30"]},
            {
                **radio("ap", bss=[{"ssid": "guest", "bssid": "02:00:5e:00:00:01"}]),
                "enabled": False,
            },
        ),
        link("cbl", "ap-a:eth0", "ap-b:eth0", medium="copper"),
    )
    (finding,) = validate(inventory)
    assert finding.rule == "E029"
    assert "02:00:5e:00:00:01" in finding.message
    assert finding.field_path == ("spec", "interfaces", 1, "wireless", "bss", 0, "bssid")


def test_a_client_repeating_its_access_point_s_bssid_is_the_point(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "ap",
            {
                **radio("ap", bss=[{"ssid": "lab", "bssid": "78:8a:20:aa:00:11"}]),
                "ipv4": ["10.0.0.1/30"],
            },
        ),
        device(
            "pc",
            {
                **radio("station", bss=[{"ssid": "lab", "bssid": "78:8a:20:aa:00:11"}]),
                "ipv4": ["10.0.0.2/30"],
            },
        ),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    assert rules_of(inventory) == []


# --------------------------------------------------------------------------- #
# E030 / W113 — the VLAN behind an SSID
# --------------------------------------------------------------------------- #


def access_point(vlan: int | None, *, declared: list[int], carried: int | None) -> dict[str, Any]:
    """An AP whose SSID maps to ``vlan`` and whose uplink carries ``carried``."""
    uplink: dict[str, Any] = {"name": "eth0", "type": "ethernet", "mtu": 1500}
    if carried is not None:
        uplink["vlan"] = {"mode": "access", "access_vlan": carried}
    else:
        uplink["ipv4"] = ["10.0.0.1/30"]
    return device(
        "ap",
        uplink,
        {**radio("ap", bss=[{"ssid": "lab", "vlan": vlan}]), "enabled": False},
        vlans=[{"id": identifier} for identifier in declared],
    )


def test_an_ssid_mapped_to_a_vlan_the_ap_does_not_carry(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        access_point(20, declared=[20], carried=None),
        device("pc", {"name": "eth0", "type": "ethernet", "mtu": 1500, "ipv4": ["10.0.0.2/30"]}),
        link("cbl", "ap:eth0", "pc:eth0", medium="copper"),
    )
    (finding,) = validate(inventory)
    assert finding.rule == "E030"
    assert "VLAN 20" in finding.message
    assert finding.field_path == ("spec", "interfaces", 1, "wireless", "bss", 0, "vlan")


def test_an_ssid_whose_vlan_reaches_a_port_is_fine(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        access_point(20, declared=[20], carried=20),
        device(
            "pc",
            {
                "name": "eth0",
                "type": "ethernet",
                "mtu": 1500,
                "vlan": {"mode": "access", "access_vlan": 20},
            },
        ),
        link("cbl", "ap:eth0", "pc:eth0", medium="copper"),
    )
    assert "E030" not in rules_of(inventory)


def test_a_port_trunking_all_carries_whatever_the_ssid_asks_for(tmp_path: Path) -> None:
    """`trunk_vlans: all` cannot be wrong about a VLAN, so E030 stays quiet."""
    inventory = inventory_of(
        tmp_path,
        device(
            "ap",
            {
                "name": "eth0",
                "type": "ethernet",
                "mtu": 1500,
                "vlan": {"mode": "trunk", "trunk_vlans": "all"},
            },
            {**radio("ap", bss=[{"ssid": "lab", "vlan": 20}]), "enabled": False},
            kind="switch",
            vlans=[{"id": 20}],
        ),
        device(
            "sw",
            {
                "name": "eth0",
                "type": "ethernet",
                "mtu": 1500,
                "vlan": {"mode": "trunk", "trunk_vlans": "all"},
            },
            kind="switch",
            vlans=[{"id": 20}],
        ),
        link("cbl", "ap:eth0", "sw:eth0", medium="copper"),
    )
    assert "E030" not in rules_of(inventory)


def test_an_ssid_vlan_missing_from_the_database_is_the_ordinary_vlan_warning(
    tmp_path: Path,
) -> None:
    """W113 covers the BSS `vlan` exactly as it covers a port's."""
    inventory = inventory_of(
        tmp_path,
        access_point(30, declared=[20], carried=20),
        device(
            "pc",
            {
                "name": "eth0",
                "type": "ethernet",
                "mtu": 1500,
                "vlan": {"mode": "access", "access_vlan": 20},
            },
        ),
        link("cbl", "ap:eth0", "pc:eth0", medium="copper"),
    )
    findings = {finding.rule: finding for finding in validate(inventory)}
    assert set(findings) == {"W113", "E030"}
    assert "SSID 'lab'" in findings["W113"].message
    assert "does not declare in 'vlans'" in findings["W113"].message


# --------------------------------------------------------------------------- #
# E031 — the SSID the client joined
# --------------------------------------------------------------------------- #


def test_a_client_on_an_ssid_the_ap_does_not_beacon(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("ap", {**radio("ap", bss=[{"ssid": "lab"}]), "ipv4": ["10.0.0.1/30"]}),
        device("pc", {**radio("station", bss=[{"ssid": "labb"}]), "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    (finding,) = validate(inventory)
    assert finding.rule == "E031"
    assert "'labb'" in finding.message and "'lab'" in finding.message
    assert finding.field_path == ("spec", "interfaces", 0, "wireless", "bss", 0, "ssid")


def test_an_access_point_modelling_no_ssid_contradicts_nothing(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("ap", {**radio("ap"), "ipv4": ["10.0.0.1/30"]}),
        device("pc", {**radio("station", bss=[{"ssid": "lab"}]), "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    assert rules_of(inventory) == []


def test_one_of_several_advertised_ssids_is_enough(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "ap",
            {
                **radio("ap", bss=[{"ssid": "lab"}, {"ssid": "guest"}]),
                "ipv4": ["10.0.0.1/30"],
            },
        ),
        device("pc", {**radio("station", bss=[{"ssid": "guest"}]), "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    assert rules_of(inventory) == []


# --------------------------------------------------------------------------- #
# W134 — co-channel access points
# --------------------------------------------------------------------------- #


def two_access_points(
    left: dict[str, Any], right: dict[str, Any], *, joined: bool = True
) -> list[dict[str, Any]]:
    """Two APs, each with a wired port, cabled together when ``joined``."""
    documents = [
        device(
            f"ap-{suffix}",
            {"name": "eth0", "type": "ethernet", "mtu": 1500, "ipv4": [f"10.0.0.{index}/30"]},
            {**radio("ap", **wireless), "enabled": False},
        )
        for index, (suffix, wireless) in enumerate((("a", left), ("b", right)), start=1)
    ]
    if joined:
        documents.append(link("cbl", "ap-a:eth0", "ap-b:eth0", medium="copper"))
    return documents


def test_two_access_points_on_overlapping_channels(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *two_access_points(
            {"band": "2.4GHz", "channel": 1, "width_mhz": 20, "bss": [{"ssid": "lab"}]},
            {"band": "2.4GHz", "channel": 3, "width_mhz": 20, "bss": [{"ssid": "lab"}]},
        ),
    )
    (finding,) = validate(inventory)
    assert finding.rule == "W134"
    assert "2402-2422 MHz and 2412-2432 MHz" in finding.message
    assert finding.elements == ("ap-a", "ap-b")


def test_the_non_overlapping_2_4ghz_channels_are_quiet(tmp_path: Path) -> None:
    """1, 6 and 11 are the reason the rule is worth having."""
    inventory = inventory_of(
        tmp_path,
        *two_access_points(
            {"band": "2.4GHz", "channel": 1, "width_mhz": 20, "bss": [{"ssid": "lab"}]},
            {"band": "2.4GHz", "channel": 6, "width_mhz": 20, "bss": [{"ssid": "lab"}]},
        ),
    )
    assert rules_of(inventory) == []


def test_a_wide_channel_reaches_further_than_a_narrow_one(tmp_path: Path) -> None:
    """1 and 6 do not overlap at 20 MHz; at 40 MHz they do."""
    inventory = inventory_of(
        tmp_path,
        *two_access_points(
            {"band": "2.4GHz", "channel": 1, "width_mhz": 40, "bss": [{"ssid": "lab"}]},
            {"band": "2.4GHz", "channel": 6, "width_mhz": 40, "bss": [{"ssid": "lab"}]},
        ),
    )
    assert rules_of(inventory) == ["W134"]


def test_the_two_bands_of_one_access_point_do_not_interfere(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *two_access_points(
            {"band": "2.4GHz", "channel": 1, "bss": [{"ssid": "lab"}]},
            {"band": "5GHz", "channel": 36, "bss": [{"ssid": "lab"}]},
        ),
    )
    assert rules_of(inventory) == []


def test_a_radio_with_no_channel_cannot_be_compared(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *two_access_points(
            {"band": "2.4GHz", "bss": [{"ssid": "lab"}]},
            {"band": "2.4GHz", "channel": 1, "bss": [{"ssid": "lab"}]},
        ),
    )
    assert rules_of(inventory) == []


def test_two_islands_on_one_channel_are_two_broadcast_domains(tmp_path: Path) -> None:
    """Same channel, same VLAN, no path between them: not one domain."""
    inventory = inventory_of(
        tmp_path,
        *two_access_points(
            {"band": "2.4GHz", "channel": 1, "bss": [{"ssid": "lab"}]},
            {"band": "2.4GHz", "channel": 1, "bss": [{"ssid": "lab"}]},
            joined=False,
        ),
    )
    # W103 for each orphan, W121 for the split; W134 is not among them.
    assert "W134" not in rules_of(inventory)


def test_access_points_serving_different_vlans_are_different_domains(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *two_access_points(
            {"band": "2.4GHz", "channel": 1, "bss": [{"ssid": "a", "vlan": 10}]},
            {"band": "2.4GHz", "channel": 1, "bss": [{"ssid": "b", "vlan": 20}]},
        ),
    )
    # The SSIDs bridge into VLANs neither AP carries, which is E030's business;
    # what matters here is that the two radios are not compared.
    assert "W134" not in rules_of(inventory)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_the_l2_view_labels_a_radio_link_with_ssid_channel_and_band(
    association: Inventory,
) -> None:
    source = to_dot(build_graph(association, layer=Layer.L2))
    (edge,) = [line for line in source.splitlines() if " -- " in line]
    assert "lab @ 36/5GHz" in edge
    # …and keeps the dashed wireless styling it had before.
    assert "style=dashed" in edge and '"#2563eb"' in edge


def test_mermaid_says_the_same_thing_on_one_line(association: Inventory) -> None:
    source = to_mermaid(build_graph(association, layer=Layer.L2))
    (edge,) = [line for line in source.splitlines() if "-." in line]
    assert "lab @ 36/5GHz" in edge


def test_the_l1_view_keeps_saying_wireless(association: Inventory) -> None:
    """Layer 1 is about the medium; the SSID belongs to the layer-2 label.

    The tooltip carries the association at every layer — it is the place a
    reader goes for the full record — so the assertion is about the label.
    """
    source = to_dot(build_graph(association, layer=Layer.L1))
    (edge,) = [line for line in source.splitlines() if " -- " in line]
    label = edge.partition('label="')[2].partition('"')[0]
    assert "wireless" in label and "36/5GHz" not in label


def test_the_client_decides_which_ssid_the_link_is_on(tmp_path: Path) -> None:
    """The AP beacons two networks; the association names one of them."""
    inventory = inventory_of(
        tmp_path,
        device(
            "ap",
            {
                **radio("ap", band="5GHz", channel=44, bss=[{"ssid": "lab"}, {"ssid": "guest"}]),
                "ipv4": ["10.0.0.1/30"],
            },
        ),
        device("pc", {**radio("station", bss=[{"ssid": "guest"}]), "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    (edge,) = build_graph(inventory, layer=Layer.L2).edges
    assert edge.wireless is not None
    assert edge.wireless.describe() == "guest @ 44/5GHz"
    assert edge.wireless.access_point == "ap:wlan0"


def test_an_ap_that_nobody_named_an_ssid_on_lists_what_it_beacons(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "ap",
            {
                **radio("ap", band="6GHz", channel=37, bss=[{"ssid": "lab"}, {"ssid": "guest"}]),
                "ipv4": ["10.0.0.1/30"],
            },
        ),
        device("pc", {**radio("station"), "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    (edge,) = build_graph(inventory, layer=Layer.L2).edges
    assert edge.wireless is not None
    assert edge.wireless.describe() == "lab, guest @ 37/6GHz"


def test_a_radio_link_that_models_nothing_gets_no_annotation(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("ap", {"name": "wlan0", "type": "wifi", "mtu": 1500, "ipv4": ["10.0.0.1/30"]}),
        device("pc", {"name": "wlan0", "type": "wifi", "mtu": 1500, "ipv4": ["10.0.0.2/30"]}),
        link("wl", "ap:wlan0", "pc:wlan0"),
    )
    (edge,) = build_graph(inventory, layer=Layer.L2).edges
    assert edge.wireless is None
    assert "@" not in to_dot(build_graph(inventory, layer=Layer.L2))


def test_the_json_export_carries_the_association(association: Inventory) -> None:
    document = graph_to_dict(build_graph(association, layer=Layer.L2))
    (edge,) = [edge for edge in document["edges"] if edge["id"] == "wl"]
    assert edge["wireless"] == {
        "ssids": ["lab"],
        "band": "5GHz",
        "channel": 36,
        "widthMhz": 80,
        "accessPoint": "ap:wlan0",
    }


def test_a_tooltip_repeats_what_the_label_says(association: Inventory) -> None:
    source = to_dot(build_graph(association, layer=Layer.L2))
    (edge,) = [line for line in source.splitlines() if " -- " in line]
    assert "wireless: lab @ 36/5GHz" in edge


@pytest.mark.parametrize(
    ("wireless", "expected"),
    [
        ({"ssids": ["lab"], "band": "5GHz", "channel": 36}, "lab @ 36/5GHz"),
        # A band with no channel: the tuning is all that is known.
        ({"band": "5GHz"}, "5GHz"),
        # An SSID with no frequency at all: no stray '@'.
        ({"ssids": ["lab", "guest"]}, "lab, guest"),
        # A radio link whose ends state a role and nothing else.
        ({}, "—"),
    ],
)
def test_the_tooltip_says_only_what_the_document_states(
    wireless: dict[str, Any], expected: str
) -> None:
    assert _wireless_text(wireless) == expected


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_list_bss_prints_one_row_per_ssid_per_radio(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["-i", str(HOME_LAB), "list", "bss"], catch_exceptions=False)
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["SSID", "RADIO", "ROLE", "CHANNEL", "BSSID", "VLAN", "SECURITY"]
    assert any("home-guest" in line and "78:8a:20:aa:00:12" in line for line in lines)
    # The client radio is in the table too, so "who is on this SSID?" is answerable.
    assert any("hosts/phone:en0" in line and "station" in line for line in lines)


def test_list_bss_is_the_same_data_in_every_format(runner: CliRunner) -> None:
    as_json = runner.invoke(
        cli, ["-i", str(HOME_LAB), "list", "bss", "-F", "json"], catch_exceptions=False
    )
    records = json.loads(as_json.stdout)
    assert [record["ssid"] for record in records] == ["home", "home", "home-guest"]
    guest = records[-1]
    assert guest["element"] == "wireless/ap-home"
    assert guest["interface"] == "wlan0"
    assert guest["role"] == "ap"
    assert guest["band"] == "5GHz"
    assert guest["channel"] == 36
    assert guest["widthMhz"] == 80
    assert guest["txPowerDbm"] == 23
    assert guest["vlan"] == 20
    assert guest["security"] == "wpa2-psk"
    assert guest["hidden"] is False
    assert guest["source"].startswith("wireless/ap-home.yaml")

    as_yaml = runner.invoke(
        cli, ["-i", str(HOME_LAB), "list", "bss", "-F", "yaml"], catch_exceptions=False
    )
    assert yaml.safe_load(as_yaml.stdout) == records


def test_an_inventory_with_no_radio_says_so(runner: CliRunner, tmp_path: Path) -> None:
    inventory_of(
        tmp_path,
        device("pc-a", {"name": "eth0", "type": "ethernet", "mtu": 1500, "ipv4": ["10.0.0.1/30"]}),
        device("pc-b", {"name": "eth0", "type": "ethernet", "mtu": 1500, "ipv4": ["10.0.0.2/30"]}),
        link("cbl", "pc-a:eth0", "pc-b:eth0", medium="copper"),
    )
    result = runner.invoke(cli, ["-i", str(tmp_path), "list", "bss"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no bss declared" in result.stdout


def test_show_prints_the_radio_block(runner: CliRunner) -> None:
    """`show` resolves the document, so the BSS table comes with it."""
    result = runner.invoke(cli, ["-i", str(HOME_LAB), "show", "ap-home"], catch_exceptions=False)
    assert result.exit_code == 0
    document = yaml.safe_load(result.stdout)
    (wlan0,) = [
        interface for interface in document["spec"]["interfaces"] if interface["name"] == "wlan0"
    ]
    assert wlan0["wireless"]["role"] == "ap"
    assert wlan0["wireless"]["channel"] == 36
    assert [entry["ssid"] for entry in wlan0["wireless"]["bss"]] == ["home", "home-guest"]
    assert wlan0["wireless"]["bss"][1]["security"] == "wpa2-psk"
