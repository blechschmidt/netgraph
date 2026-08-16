"""Tests for the pydantic element models (``docs/schema.md`` §3-§8)."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from netviz.errors import MAX_ECHOED_VALUE_LENGTH, LoaderError, SchemaError, echo_value
from netviz.models import (
    ELEMENT_MODELS,
    KINDS,
    Adapter,
    BridgeType,
    Cable,
    Computer,
    Device,
    Hub,
    Interface,
    InterfaceRef,
    IPv4Address,
    IPv6Address,
    Router,
    Server,
    Switch,
    UpstreamType,
    VlanSet,
    element_model_for,
    format_bitrate,
    normalise_mac,
    parse_bitrate,
    parse_document,
)
from netviz.models import interface as interface_module
from netviz.models.interface import AcceptableFrames, InterfaceType, VlanMode

API_VERSION = "netviz.dev/v1alpha1"
SCHEMA_DOC = Path(__file__).resolve().parents[1] / "docs" / "schema.md"


def document(kind: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal document envelope around ``spec``."""
    return {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": {"name": name},
        "spec": spec,
    }


def device(kind: str = "switch", **spec: Any) -> dict[str, Any]:
    spec.setdefault("interfaces", [{"name": "eth0", "type": "ethernet"}])
    return document(kind, f"{kind}-1", spec)


def parse_failure(doc: dict[str, Any]) -> SchemaError:
    with pytest.raises(SchemaError) as excinfo:
        parse_document(doc)
    return excinfo.value


def parse_device(doc: dict[str, Any]) -> Device:
    """Parse a document that is expected to be one of the five device kinds."""
    element = parse_document(doc)
    assert isinstance(element, Device)
    return element


def parse_cable(doc: dict[str, Any]) -> Cable:
    element = parse_document(doc)
    assert isinstance(element, Cable)
    return element


def parse_adapter(doc: dict[str, Any]) -> Adapter:
    element = parse_document(doc)
    assert isinstance(element, Adapter)
    return element


# --------------------------------------------------------------------------- #
# Envelope and discriminated union
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("switch", Switch),
        ("router", Router),
        ("hub", Hub),
        ("computer", Computer),
        ("server", Server),
    ],
)
def test_kind_selects_the_device_model(kind: str, expected: type) -> None:
    element = parse_device(device(kind))
    assert isinstance(element, expected)
    assert element.kind == kind
    assert element.name == f"{kind}-1"


def test_cable_and_adapter_kinds() -> None:
    cable = parse_cable(
        document("cable", "cbl-1", {"endpoints": ["a:1", "b:2"], "medium": "copper"})
    )
    assert isinstance(cable, Cable)

    adapter = parse_adapter(
        document(
            "adapter",
            "adp-1",
            {
                "upstream": {"name": "usb0", "type": "usb"},
                "interfaces": [{"name": "enx0", "type": "ethernet"}],
            },
        )
    )
    assert isinstance(adapter, Adapter)


def test_schema_error_is_a_loader_error() -> None:
    assert issubclass(SchemaError, LoaderError)
    assert SchemaError().exit_code == 3


@pytest.mark.parametrize("document_value", [None, [], "switch", 42])
def test_non_mapping_document_is_rejected(document_value: Any) -> None:
    error = parse_failure(document_value)
    assert error.issues[0].rule == "NG-D001"


def test_missing_kind_is_reported_at_the_kind_field() -> None:
    error = parse_failure({"apiVersion": API_VERSION, "metadata": {"name": "x"}, "spec": {}})
    assert error.path == ("kind",)
    assert error.issues[0].rule == "NG-D001"


def test_unknown_kind_is_reported() -> None:
    doc = device()
    doc["kind"] = "gateway"
    error = parse_failure(doc)
    assert error.issues[0].rule == "NG-D003"
    assert "gateway" in str(error)


def test_unknown_api_version_is_reported() -> None:
    doc = device()
    doc["apiVersion"] = "netviz.dev/v2"
    error = parse_failure(doc)
    assert error.location == "apiVersion"
    assert error.issues[0].rule == "NG-D002"


def test_missing_api_version_is_reported() -> None:
    doc = device()
    del doc["apiVersion"]
    error = parse_failure(doc)
    assert error.location == "apiVersion"


def test_unknown_keys_are_rejected_everywhere() -> None:
    error = parse_failure(device(interfaces=[{"name": "eth0", "type": "ethernet", "vlanid": 3}]))
    assert error.issues[0].rule == "NG-D005"
    assert error.location == "spec.interfaces[0].vlanid"


def test_element_serialises_back_to_the_document_shape() -> None:
    element = parse_device(device("router"))
    dumped = element.model_dump(by_alias=True, mode="json")
    assert dumped["apiVersion"] == API_VERSION
    assert dumped["kind"] == "router"
    assert parse_document(dumped) == element


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["sw-access-01", "rtr.edge_1", "a", "A1"])
def test_valid_element_names(name: str) -> None:
    doc = device()
    doc["metadata"]["name"] = name
    assert parse_document(doc).name == name


@pytest.mark.parametrize("name", ["", "-leading", "trailing-", "has space", "a:b", "a--b" * 100])
def test_invalid_element_names(name: str) -> None:
    doc = device()
    doc["metadata"]["name"] = name
    assert parse_failure(doc).location == "metadata.name"


def test_labels_accept_a_dns_prefix() -> None:
    doc = device()
    doc["metadata"]["labels"] = {"site": "hq", "example.com/tier": "edge"}
    assert parse_document(doc).metadata.label("site") == "hq"


@pytest.mark.parametrize(
    "labels",
    [
        {"netviz.dev/generated": "1"},
        {"Site": "hq"},
        {"-site": "hq"},
        {"a/b/c": "x"},
        {"site": "x" * 254},
    ],
)
def test_invalid_labels_are_rejected(labels: dict[str, str]) -> None:
    doc = device()
    doc["metadata"]["labels"] = labels
    assert parse_failure(doc).location == "metadata.labels"


# --------------------------------------------------------------------------- #
# MAC addresses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00:11:22:33:44:55", "00:11:22:33:44:55"),
        ("00-11-22-33-44-55", "00:11:22:33:44:55"),
        ("0011.2233.4455", "00:11:22:33:44:55"),
        ("3C:97:0E:AA:BB:CC", "3c:97:0e:aa:bb:cc"),
        ("3c97.0eAA.BBcc", "3c:97:0e:aa:bb:cc"),
    ],
)
def test_mac_normalisation(raw: str, expected: str) -> None:
    assert normalise_mac(raw) == expected
    interface = Interface(name="eth0", type=InterfaceType.ETHERNET, mac=raw)
    assert interface.mac == expected


@pytest.mark.parametrize(
    "raw",
    [
        "00:11:22:33:44",
        "00:11:22:33:44:55:66",
        "zz:11:22:33:44:55",
        "001122334455",
        "00:11-22:33:44:55",
        "",
    ],
)
def test_invalid_macs_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalise_mac(raw)


def test_mac_swallowed_by_yaml_sexagesimal_gets_a_helpful_error() -> None:
    # YAML 1.1 resolves ``12:34:56:12:34:56`` to an integer.
    with pytest.raises(ValueError, match="quote it"):
        normalise_mac(1_234_567)


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


def test_ipv4_shorthand_and_canonical_form_agree() -> None:
    shorthand = IPv4Address.model_validate("10.10.10.1/24")
    canonical = IPv4Address.model_validate({"ip": "10.10.10.1", "prefix_length": 24})
    assert shorthand == canonical
    assert shorthand.ip == ipaddress.IPv4Address("10.10.10.1")
    assert shorthand.network == ipaddress.IPv4Network("10.10.10.0/24")
    assert str(shorthand.netmask) == "255.255.255.0"


def test_ipv4_netmask_is_normalised_to_a_prefix_length() -> None:
    address = IPv4Address.model_validate({"ip": "192.168.50.20", "netmask": "255.255.255.0"})
    assert address.prefix_length == 24
    assert "netmask" not in address.model_dump()


def test_non_contiguous_netmask_is_rejected() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        IPv4Address.model_validate({"ip": "10.0.0.1", "netmask": "255.0.255.0"})


def test_prefix_length_and_netmask_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        IPv4Address.model_validate({"ip": "10.0.0.1", "prefix_length": 24, "netmask": "255.0.0.0"})


def test_ipv4_requires_a_subnet() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        IPv4Address.model_validate({"ip": "10.0.0.1"})


def test_ipv6_is_normalised_to_rfc_5952() -> None:
    address = IPv6Address.model_validate("2001:0DB8:0000:0000:0000:0000:0000:0001/64")
    assert str(address.ip) == "2001:db8::1"
    assert address.prefix_length == 64


def test_ipv6_rejects_a_netmask_and_requires_a_prefix_length() -> None:
    with pytest.raises(ValueError, match="IPv4 only"):
        IPv6Address.model_validate({"ip": "2001:db8::1", "netmask": "255.255.255.0"})
    with pytest.raises(ValueError, match="required"):
        IPv6Address.model_validate({"ip": "2001:db8::1"})


def test_address_family_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError):
        IPv4Address.model_validate("2001:db8::1/64")
    with pytest.raises(ValueError):
        IPv6Address.model_validate("10.0.0.1/24")


def test_zone_indices_are_rejected() -> None:
    with pytest.raises(ValueError, match="zone"):
        IPv6Address.model_validate("fe80::1%eth0/64")


# --------------------------------------------------------------------------- #
# The address fast path
# --------------------------------------------------------------------------- #
#
# ``_plain_address`` short-circuits ``ipaddress.ip_interface`` for the one
# spelling nearly every address in an inventory uses. It is an optimisation, so
# the property that matters is not what it does but that it is *invisible*: for
# every value, accepted or rejected, the model must reach exactly the same
# outcome with it as without it. The tests below assert that by running each
# spelling twice -- once normally, once with the fast path forced to decline --
# and comparing the results.

#: Spellings chosen to land on both sides of every branch of the fast path:
#: prefix lengths that are and are not plain decimals, netmasks, out-of-range
#: lengths, the wrong family, and malformed addresses.
ADDRESS_SPELLINGS: list[tuple[int, str]] = [
    (4, "10.10.10.1/24"),
    (4, "10.0.0.1/32"),
    (4, "0.0.0.0/0"),
    # A leading zero is still a decimal prefix length, not an octal one.
    (4, "10.0.0.1/024"),
    # The RFC 8344 netmask spelling, contiguous and not.
    (4, "10.0.0.1/255.255.255.0"),
    (4, "10.0.0.1/255.0.255.0"),
    (4, "10.0.0.1/33"),
    (4, "10.0.0.1/-1"),
    (4, "10.0.0.1/"),
    (4, "10.0.0.1/24/32"),
    # Arabic-Indic digits: ``str.isdigit`` accepts them and ``int`` converts
    # them, but ``ipaddress`` does not -- hence the ``isascii`` guard.
    (4, "10.0.0.1/٢٤"),
    (4, "10.0.0.256/24"),
    (4, "10.0.0.1 /24"),
    (4, "2001:db8::1/64"),
    (4, "not an address/24"),
    (6, "2001:db8::1/64"),
    (6, "2001:0DB8:0000:0000:0000:0000:0000:0001/64"),
    (6, "::/0"),
    (6, "::1/128"),
    (6, "2001:db8::1/0064"),
    (6, "2001:db8::1/129"),
    (6, "2001:db8::1/255.255.255.0"),
    (6, "2001:db8::g/64"),
    (6, "10.0.0.1/24"),
]


def address_outcome(family: int, text: str) -> tuple[str, str]:
    """Validate ``text`` as an address of ``family``; report value or error."""
    model = IPv4Address if family == 4 else IPv6Address
    try:
        parsed = model.model_validate(text)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        return ("error", str(exc))
    return ("ok", f"{parsed.ip}/{parsed.prefix_length}")


@pytest.mark.parametrize(
    ("family", "text"),
    ADDRESS_SPELLINGS,
    ids=[f"v{family}-{text}" for family, text in ADDRESS_SPELLINGS],
)
def test_the_address_fast_path_is_invisible(
    family: int, text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every spelling validates identically with and without the fast path."""
    with_fast_path = address_outcome(family, text)
    monkeypatch.setattr(interface_module, "_plain_address", lambda text, family: None, raising=True)
    assert address_outcome(family, text) == with_fast_path


def test_the_address_fast_path_is_actually_taken() -> None:
    """Otherwise the equivalence test above would be comparing one path to itself."""
    assert interface_module._plain_address("10.0.0.1/24", 4) == {
        "ip": ipaddress.IPv4Address("10.0.0.1"),
        "prefix_length": 24,
    }
    assert interface_module._plain_address("2001:db8::1/64", 6) == {
        "ip": ipaddress.IPv6Address("2001:db8::1"),
        "prefix_length": 64,
    }

    # And the general path is not reached at all for the common spelling.
    def refuse(_: object) -> None:
        raise AssertionError("ipaddress.ip_interface was called on the fast path")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ipaddress, "ip_interface", refuse)
        assert str(IPv4Address.model_validate("10.0.0.1/24")) == "10.0.0.1/24"
        assert str(IPv6Address.model_validate("2001:db8::1/64")) == "2001:db8::1/64"


def test_bare_address_list_shorthand() -> None:
    element = parse_device(
        device(
            interfaces=[
                {
                    "name": "eth0",
                    "type": "ethernet",
                    "mtu": 1500,
                    "ipv4": ["10.0.0.1/24", "10.0.1.1/24"],
                    "ipv6": {"addresses": ["2001:db8::1/64"]},
                }
            ]
        )
    )
    interface = element.spec.interfaces[0]
    assert interface.ipv4 is not None and len(interface.ipv4.addresses) == 2
    assert interface.ipv6 is not None and interface.ipv6.addresses[0].prefix_length == 64


def test_duplicate_addresses_are_rejected() -> None:
    error = parse_failure(
        device(
            interfaces=[
                {"name": "eth0", "type": "ethernet", "ipv4": ["10.0.0.1/24", "10.0.0.1/25"]}
            ]
        )
    )
    assert "duplicate address" in str(error)


def test_family_defaults_are_materialised_from_the_interface_and_device() -> None:
    router = parse_device(
        document(
            "router",
            "rtr-1",
            {
                "interfaces": [
                    {
                        "name": "ge-0/0/0",
                        "type": "ethernet",
                        "mtu": 9000,
                        "ipv4": ["10.0.0.1/24"],
                        "ipv6": ["2001:db8::1/64"],
                    }
                ]
            },
        )
    )
    assert router.spec.forwarding is not None
    assert router.spec.forwarding.ipv4 is True  # §6.1.1: routers forward by default
    interface = router.spec.interfaces[0]
    assert interface.ipv4 is not None and interface.ipv4.forwarding is True
    assert interface.ipv4.mtu == 9000
    assert interface.ipv6 is not None and interface.ipv6.mtu == 9000


def test_host_forwarding_defaults_to_false() -> None:
    host = parse_device(
        document(
            "computer",
            "pc-1",
            {"interfaces": [{"name": "eno1", "type": "ethernet", "ipv4": ["10.0.0.5/24"]}]},
        )
    )
    assert host.spec.forwarding is not None and host.spec.forwarding.ipv4 is False
    assert host.spec.interfaces[0].ipv4 is not None
    assert host.spec.interfaces[0].ipv4.forwarding is False


def test_layer2_mtu_below_1280_is_not_propagated_to_ipv6() -> None:
    element = parse_device(
        device(interfaces=[{"name": "eth0", "type": "ethernet", "mtu": 576, "ipv6": {}}])
    )
    interface = element.spec.interfaces[0]
    assert interface.ipv4 is None
    assert interface.ipv6 is not None and interface.ipv6.mtu is None


# --------------------------------------------------------------------------- #
# MTU
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mtu", [67, 0, -1, 65536])
def test_mtu_outside_the_valid_range_is_rejected(mtu: int) -> None:
    error = parse_failure(device(interfaces=[{"name": "eth0", "type": "ethernet", "mtu": mtu}]))
    assert error.location == "spec.interfaces[0].mtu"


@pytest.mark.parametrize("mtu", [68, 1500, 9000, 9216, 65535])
def test_mtu_inside_the_valid_range_is_accepted(mtu: int) -> None:
    element = parse_device(device(interfaces=[{"name": "eth0", "type": "ethernet", "mtu": mtu}]))
    assert element.spec.interfaces[0].mtu == mtu


def test_ipv6_needs_at_least_the_minimum_mtu() -> None:
    error = parse_failure(
        device(
            interfaces=[
                {"name": "eth0", "type": "ethernet", "mtu": 1000, "ipv6": ["2001:db8::1/64"]}
            ]
        )
    )
    assert error.issues[0].rule == "NG-I011"
    assert error.location == "spec.interfaces[0].mtu"


# --------------------------------------------------------------------------- #
# VLANs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("vlan_id", [0, 4095, -1, 100000])
def test_vlan_ids_outside_1_4094_are_rejected(vlan_id: int) -> None:
    error = parse_failure(
        device(
            interfaces=[
                {
                    "name": "eth0",
                    "type": "ethernet",
                    "vlan": {"mode": "access", "access_vlan": vlan_id},
                }
            ]
        )
    )
    assert error.location == "spec.interfaces[0].vlan.access_vlan"


@pytest.mark.parametrize(
    ("raw", "expected", "size"),
    [
        ("all", "1-4094", 4094),
        ("none", "none", 0),
        (10, "10", 1),
        ([10, 20], "10,20", 2),
        ([10, 11, 12], "10-12", 3),
        (["100-110"], "100-110", 11),
        ("10,20,100-110", "10,20,100-110", 13),
        ([20, 10, "12-14", 15, 11], "10-15,20", 7),
    ],
)
def test_vlan_set_normalisation(raw: Any, expected: str, size: int) -> None:
    vlan_set = VlanSet.model_validate(raw)
    assert vlan_set.to_string() == expected
    assert len(vlan_set) == size


def test_vlan_set_membership_and_serialisation() -> None:
    vlan_set = VlanSet.model_validate(["10-20", 30])
    assert 15 in vlan_set
    assert 25 not in vlan_set
    assert VlanSet.model_validate(vlan_set.model_dump()) == vlan_set
    assert vlan_set.isdisjoint(VlanSet.model_validate([40, 50]))
    assert not vlan_set.isdisjoint(VlanSet.model_validate("20-40"))


@pytest.mark.parametrize("raw", ["4095", "0", "10-4095", "20-10", "ten", "10..20"])
def test_invalid_vlan_sets_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        VlanSet.model_validate(raw)


def test_access_port_defaults_and_derived_frame_filter() -> None:
    element = parse_device(
        device(interfaces=[{"name": "eth0", "type": "ethernet", "vlan": {"mode": "access"}}])
    )
    vlan = element.spec.interfaces[0].vlan
    assert vlan is not None
    assert vlan.mode is VlanMode.ACCESS
    assert vlan.access_vlan == 1
    assert vlan.pvid == 1
    assert vlan.acceptable_frames is AcceptableFrames.UNTAGGED_ONLY
    assert vlan.ingress_filtering is True


def test_trunk_frame_filter_derivation() -> None:
    tagged_only = (
        parse_device(
            device(
                interfaces=[
                    {
                        "name": "eth0",
                        "type": "ethernet",
                        "vlan": {"mode": "trunk", "trunk_vlans": [30, 40]},
                    }
                ]
            )
        )
        .spec.interfaces[0]
        .vlan
    )
    assert tagged_only is not None
    assert tagged_only.acceptable_frames is AcceptableFrames.TAGGED_ONLY
    assert tagged_only.pvid == 1
    assert tagged_only.vlan_ids() == frozenset({30, 40})

    with_native = (
        parse_device(
            device(
                interfaces=[
                    {
                        "name": "eth0",
                        "type": "ethernet",
                        "vlan": {"mode": "trunk", "trunk_vlans": "all", "native_vlan": 1},
                    }
                ]
            )
        )
        .spec.interfaces[0]
        .vlan
    )
    assert with_native is not None
    assert with_native.acceptable_frames is AcceptableFrames.ALL
    assert with_native.pvid == 1


@pytest.mark.parametrize(
    ("vlan", "rule"),
    [
        ({"mode": "access", "trunk_vlans": [10]}, "NG-V002"),
        ({"mode": "access", "native_vlan": 10}, "NG-V003"),
        ({"mode": "trunk", "access_vlan": 10}, "NG-V002"),
        ({"mode": "trunk"}, "NG-V002"),
    ],
)
def test_vlan_mode_consistency(vlan: dict[str, Any], rule: str) -> None:
    error = parse_failure(device(interfaces=[{"name": "eth0", "type": "ethernet", "vlan": vlan}]))
    assert error.issues[0].rule == rule


def test_duplicate_vlan_definition_is_rejected() -> None:
    error = parse_failure(device(vlans=[{"id": 10, "name": "a"}, {"id": 10, "name": "b"}]))
    assert error.issues[0].rule == "NG-V001"
    assert error.location == "spec.vlans[1].id"


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #


def test_interface_names_must_be_unique_within_a_device() -> None:
    error = parse_failure(
        device(interfaces=[{"name": "eth0", "type": "ethernet"}, {"name": "eth0", "type": "wifi"}])
    )
    assert error.issues[0].rule == "NG-I001"
    assert error.location == "spec.interfaces[1].name"


@pytest.mark.parametrize("name", ["", " ", "eth 0", "a" * 65, "sw:eth0"])
def test_interface_names_must_match_the_grammar(name: str) -> None:
    error = parse_failure(device(interfaces=[{"name": name, "type": "ethernet"}]))
    assert error.location == "spec.interfaces[0].name"


def test_a_device_needs_at_least_one_interface() -> None:
    error = parse_failure(device(interfaces=[]))
    assert error.location == "spec.interfaces"


def test_vlan_subinterface_requires_a_resolvable_parent() -> None:
    element = parse_device(
        device(
            interfaces=[
                {
                    "name": "eth0",
                    "type": "ethernet",
                    "vlan": {"mode": "trunk", "trunk_vlans": [10]},
                },
                {
                    "name": "eth0.10",
                    "type": "vlan",
                    "parent": "eth0",
                    "vlan": {"mode": "access", "access_vlan": 10},
                },
            ]
        )
    )
    assert element.spec.interfaces[1].lower_layer_if == ("eth0",)

    error = parse_failure(
        device(
            interfaces=[
                {
                    "name": "eth0.10",
                    "type": "vlan",
                    "parent": "missing",
                    "vlan": {"mode": "access", "access_vlan": 10},
                }
            ]
        )
    )
    assert error.issues[0].rule == "NG-I002"
    assert error.location == "spec.interfaces[0].parent"


def test_parent_is_only_allowed_on_vlan_interfaces() -> None:
    error = parse_failure(
        device(interfaces=[{"name": "eth0", "type": "ethernet", "parent": "eth1"}])
    )
    assert error.issues[0].rule == "NG-I002"


def test_vlan_interface_requires_a_parent() -> None:
    error = parse_failure(
        device(interfaces=[{"name": "eth0.10", "type": "vlan", "vlan": {"mode": "access"}}])
    )
    assert error.issues[0].rule == "NG-I002"


@pytest.mark.parametrize("aggregate", ["lag", "bridge"])
def test_aggregates_require_resolvable_members(aggregate: str) -> None:
    element = parse_device(
        device(
            interfaces=[
                {"name": "eno1", "type": "ethernet"},
                {"name": "eno2", "type": "ethernet"},
                {"name": "agg0", "type": aggregate, "members": ["eno1", "eno2"]},
            ]
        )
    )
    assert element.spec.interfaces[2].lower_layer_if == ("eno1", "eno2")

    error = parse_failure(
        device(interfaces=[{"name": "agg0", "type": aggregate, "members": ["nope"]}])
    )
    assert error.issues[0].rule == "NG-I003"


@pytest.mark.parametrize(
    "interface",
    [
        {"name": "agg0", "type": "lag"},
        {"name": "agg0", "type": "lag", "members": []},
        {"name": "agg0", "type": "lag", "members": ["a", "a"]},
        {"name": "agg0", "type": "lag", "members": ["agg0"]},
        {"name": "eth0", "type": "ethernet", "members": ["a"]},
    ],
)
def test_invalid_member_lists(interface: dict[str, Any]) -> None:
    error = parse_failure(device(interfaces=[interface, {"name": "a", "type": "ethernet"}]))
    assert error.issues[0].rule == "NG-I003"


def test_interface_helpers() -> None:
    element = parse_device(
        device(
            interfaces=[
                {"name": "lo", "type": "loopback", "ipv4": ["127.0.0.1/8"]},
                {"name": "eth0", "type": "ethernet", "ipv6": ["2001:db8::1/64"]},
            ]
        )
    )
    assert element.interface("eth0") is not None
    assert element.interface("nope") is None
    assert list(element.interface_names()) == ["lo", "eth0"]
    assert element.interfaces[0].has_ipv4_addresses
    assert not element.interfaces[0].is_cableable
    assert element.interfaces[1].is_cableable
    assert len(element.interfaces[1].addresses()) == 1
    assert InterfaceType.ETHERNET.iana_if_type == "ianaift:ethernetCsmacd"


# --------------------------------------------------------------------------- #
# Per-kind constraints (§6.5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "rule"),
    [
        (
            {"interfaces": [{"name": "p1", "type": "ethernet", "vlan": {"mode": "access"}}]},
            "NG-H001",
        ),
        ({"interfaces": [{"name": "p1", "type": "ethernet", "ipv4": ["10.0.0.1/24"]}]}, "NG-H002"),
        (
            {
                "interfaces": [{"name": "p1", "type": "ethernet"}],
                "bridge": {"type": "mac-bridge"},
            },
            "NG-H003",
        ),
        ({"interfaces": [{"name": "p1", "type": "ethernet"}], "vlans": [{"id": 10}]}, "NG-H003"),
        (
            {
                "interfaces": [{"name": "p1", "type": "ethernet"}],
                "forwarding": {"ipv4": False, "ipv6": False},
            },
            "NG-H003",
        ),
        ({"interfaces": [{"name": "p1", "type": "wifi"}]}, "NG-H004"),
    ],
)
def test_hub_rejects_layer2_and_layer3_configuration(spec: dict[str, Any], rule: str) -> None:
    error = parse_failure(document("hub", "hub-1", spec))
    assert error.issues[0].rule == rule


def test_hub_accepts_a_plain_repeater() -> None:
    hub = parse_device(
        document(
            "hub",
            "hub-lab-01",
            {"interfaces": [{"name": "p1", "type": "ethernet", "mac": "00:11:22:33:44:55"}]},
        )
    )
    assert hub.spec.forwarding is None
    assert hub.default_glyph == "hub"


def test_bridge_name_defaults_to_the_element_name() -> None:
    element = parse_device(device(bridge={"type": "customer-vlan-bridge"}))
    assert element.spec.bridge is not None
    assert element.spec.bridge.name == "switch-1"


# --------------------------------------------------------------------------- #
# Cables
# --------------------------------------------------------------------------- #


def test_cable_endpoints_are_parsed_sorted_and_serialised() -> None:
    cable = parse_cable(
        document(
            "cable",
            "cbl-1",
            {
                "endpoints": ["sw-b:Gi0/2", {"device": "rtr-a", "interface": "ge-0/0/1"}],
                "medium": "copper",
                "speed": "1Gbps",
                "length_m": 2,
                "category": "cat6",
            },
        )
    )
    assert [str(ref) for ref in cable.endpoints] == ["rtr-a:ge-0/0/1", "sw-b:Gi0/2"]
    assert cable.spec.speed == 1_000_000_000
    assert cable.spec.length_m == pytest.approx(2.0)
    assert not cable.is_self_link
    assert cable.other_end(cable.endpoints[0]) == cable.endpoints[1]
    dumped = cable.model_dump(mode="json")
    assert dumped["spec"]["endpoints"] == ["rtr-a:ge-0/0/1", "sw-b:Gi0/2"]


def test_cable_endpoint_order_does_not_matter() -> None:
    def build(endpoints: list[str]) -> Cable:
        parsed = parse_cable(
            document("cable", "cbl-1", {"endpoints": endpoints, "medium": "copper"})
        )
        assert isinstance(parsed, Cable)
        return parsed

    assert build(["a:1", "b:2"]) == build(["b:2", "a:1"])


def test_self_link_is_allowed_but_detectable() -> None:
    cable = parse_cable(
        document("cable", "cbl-1", {"endpoints": ["a:1", "a:2"], "medium": "copper"})
    )
    assert cable.is_self_link


@pytest.mark.parametrize("endpoints", [["a:1"], ["a:1", "b:2", "c:3"]])
def test_cable_needs_exactly_two_endpoints(endpoints: list[str]) -> None:
    error = parse_failure(document("cable", "c", {"endpoints": endpoints, "medium": "copper"}))
    assert error.issues[0].rule == "NG-C001"


@pytest.mark.parametrize("endpoint", ["nocolon", "a:b:c", ":iface", "dev:"])
def test_malformed_endpoint_references(endpoint: str) -> None:
    error = parse_failure(
        document("cable", "c", {"endpoints": [endpoint, "b:2"], "medium": "copper"})
    )
    assert error.location.startswith("spec.endpoints")


@pytest.mark.parametrize("field", ["length_m", "category"])
def test_wireless_links_reject_physical_plant_fields(field: str) -> None:
    spec: dict[str, Any] = {"endpoints": ["a:1", "b:2"], "medium": "wireless"}
    spec[field] = 2 if field == "length_m" else "cat6"
    error = parse_failure(document("cable", "c", spec))
    assert error.issues[0].rule == "NG-C007"


def test_unknown_medium_is_rejected() -> None:
    error = parse_failure(
        document("cable", "c", {"endpoints": ["a:1", "b:2"], "medium": "smoke-signal"})
    )
    assert error.location == "spec.medium"


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


def adapter_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "upstream": {"name": "usb0", "type": "usb", "speed": "5Gbps", "attached_to": "laptop-01"},
        "interfaces": [{"name": "enx001122334455", "type": "ethernet", "mtu": 1500}],
    }
    spec.update(overrides)
    return spec


def test_adapter_defaults_and_helpers() -> None:
    adapter = parse_adapter(document("adapter", "adp-1", adapter_spec()))
    assert isinstance(adapter, Adapter)
    assert adapter.spec.passthrough is True
    assert adapter.upstream.speed == 5_000_000_000
    assert adapter.upstream.type.iana_if_type == "ianaift:usb"
    assert adapter.upstream.attached_to == "laptop-01"
    assert list(adapter.interface_names()) == ["usb0", "enx001122334455"]
    assert adapter.interface("enx001122334455") is not None


def test_adapter_address_family_defaults_to_no_forwarding() -> None:
    adapter = parse_adapter(
        document(
            "adapter",
            "adp-1",
            adapter_spec(
                interfaces=[
                    {
                        "name": "enx0",
                        "type": "ethernet",
                        "mtu": 1500,
                        "ipv4": ["192.168.50.61/24"],
                    }
                ]
            ),
        )
    )
    interface = adapter.spec.interfaces[0]
    assert interface.ipv4 is not None
    assert interface.ipv4.forwarding is False
    assert interface.ipv4.mtu == 1500


def test_adapter_upstream_must_not_collide_with_a_downstream_port() -> None:
    error = parse_failure(
        document(
            "adapter", "adp-1", adapter_spec(interfaces=[{"name": "usb0", "type": "ethernet"}])
        )
    )
    assert error.issues[0].rule == "NG-X004"


@pytest.mark.parametrize("interface_type", ["loopback", "bridge", "vlan"])
def test_adapter_downstream_types_are_restricted(interface_type: str) -> None:
    interface: dict[str, Any] = {"name": "port0", "type": interface_type}
    if interface_type == "vlan":
        interface |= {"parent": "port1", "vlan": {"mode": "access", "access_vlan": 10}}
    if interface_type == "bridge":
        interface |= {"members": ["port1"]}
    error = parse_failure(
        document(
            "adapter",
            "adp-1",
            adapter_spec(
                interfaces=[interface, {"name": "port1", "type": "ethernet"}],
            ),
        )
    )
    assert error.issues[0].rule in {"NG-X003", "NG-I002", "NG-I003"}


def test_adapter_upstream_reference_must_be_a_device_name() -> None:
    error = parse_failure(
        document(
            "adapter",
            "adp-1",
            adapter_spec(upstream={"name": "usb0", "type": "usb", "attached_to": "laptop-01:usb1"}),
        )
    )
    assert error.location == "spec.upstream.attached_to"


# --------------------------------------------------------------------------- #
# Bit rates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1Gbps", 1_000_000_000),
        ("1 Gbps", 1_000_000_000),
        ("866Mbps", 866_000_000),
        ("100kbps", 100_000),
        ("2.5Gbps", 2_500_000_000),
        (1_000, 1_000),
        ("1000bps", 1_000),
    ],
)
def test_bitrate_parsing(raw: Any, expected: int) -> None:
    assert parse_bitrate(raw) == expected


@pytest.mark.parametrize("raw", ["1Gb", "fast", "1.5bps", "", "Gbps"])
def test_invalid_bitrates_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_bitrate(raw)


@pytest.mark.parametrize(
    ("bits", "expected"),
    [(1_000_000_000, "1Gbps"), (866_000_000, "866Mbps"), (1_500, "1500bps"), (10_000, "10kbps")],
)
def test_bitrate_formatting(bits: int, expected: str) -> None:
    assert format_bitrate(bits) == expected


# --------------------------------------------------------------------------- #
# The worked examples in the specification must parse
# --------------------------------------------------------------------------- #


def _spec_documents() -> list[tuple[str, dict[str, Any]]]:
    text = SCHEMA_DOC.read_text(encoding="utf-8")
    section = text[text.index("## 11. Worked examples") : text.index("## 12. Compatibility")]
    documents: list[tuple[str, dict[str, Any]]] = []
    for block_index, block in enumerate(re.findall(r"```yaml\n(.*?)```", section, re.DOTALL)):
        for doc_index, loaded in enumerate(yaml.safe_load_all(block)):
            if isinstance(loaded, dict):
                name = loaded.get("metadata", {}).get("name", "?")
                documents.append((f"block{block_index}#{doc_index}:{name}", loaded))
    return documents


@pytest.mark.parametrize(
    ("label", "document_value"), _spec_documents(), ids=[label for label, _ in _spec_documents()]
)
def test_worked_examples_parse(label: str, document_value: dict[str, Any]) -> None:
    element = parse_document(document_value, source=label)
    assert element.name == document_value["metadata"]["name"]
    # Round-trip: the normalised form parses back into an equal element.
    assert parse_document(element.model_dump(by_alias=True, mode="json")) == element


def test_worked_examples_cover_every_kind() -> None:
    kinds = {doc["kind"] for _, doc in _spec_documents()}
    assert kinds == {"switch", "router", "hub", "computer", "server", "cable", "adapter"}


def test_interface_ref_equality_and_hashing() -> None:
    ref = InterfaceRef.model_validate("sw-1:Gi0/1")
    assert ref == InterfaceRef.model_validate({"device": "sw-1", "interface": "Gi0/1"})
    assert ref == "sw-1:Gi0/1"
    assert len({ref, InterfaceRef.model_validate("sw-1:Gi0/1")}) == 1


# --------------------------------------------------------------------------- #
# Helper API used by the loader, validator and renderers
# --------------------------------------------------------------------------- #


def test_element_model_lookup() -> None:
    assert element_model_for("switch") is Switch
    assert element_model_for("cable") is Cable
    assert element_model_for("nope") is None
    assert len(ELEMENT_MODELS) == len(KINDS)


def test_element_str_and_name() -> None:
    element = parse_device(device("server"))
    assert str(element) == "server/server-1"
    assert element.has_interfaces is True
    assert Cable.has_interfaces is False


def test_bridge_type_maps_to_a_port_type() -> None:
    assert BridgeType.MAC.port_type == "dot1q:d-bridge-port"
    assert BridgeType.CUSTOMER_VLAN.port_type == "dot1q:c-vlan-bridge-port"


def test_vlan_database_lookup() -> None:
    element = parse_device(device(vlans=[{"id": 10, "name": "users"}]))
    found = element.spec.vlan(10)
    assert found is not None and found.name == "users"
    assert element.spec.vlan(11) is None


def test_upstream_type_iana_mapping() -> None:
    assert UpstreamType.USB_C.iana_if_type == "ianaift:usb"
    assert UpstreamType.THUNDERBOLT.iana_if_type == "ianaift:other"


def test_cable_other_end_rejects_a_foreign_reference() -> None:
    cable = parse_cable(document("cable", "c", {"endpoints": ["a:1", "b:2"], "medium": "copper"}))
    with pytest.raises(KeyError):
        cable.other_end(InterfaceRef.model_validate("z:9"))
    assert cable.endpoints[0] != 42
    assert InterfaceRef.model_validate(cable.endpoints[0]) == cable.endpoints[0]


def test_trunk_pvid_follows_the_native_vlan() -> None:
    element = parse_device(
        device(
            interfaces=[
                {
                    "name": "eth0",
                    "type": "ethernet",
                    "vlan": {"mode": "trunk", "trunk_vlans": [10, 20], "native_vlan": 99},
                }
            ]
        )
    )
    vlan = element.interfaces[0].vlan
    assert vlan is not None
    assert vlan.pvid == 99
    assert vlan.vlan_ids() == frozenset({10, 20, 99})


def test_address_helpers() -> None:
    v6 = IPv6Address.model_validate("2001:db8::1/64")
    assert v6.network == ipaddress.IPv6Network("2001:db8::/64")
    assert v6.interface.ip == v6.ip
    assert str(v6) == "2001:db8::1/64"
    v4 = IPv4Address.model_validate("10.0.0.1/8")
    assert str(v4) == "10.0.0.1/8"


def test_network_is_what_ip_interface_would_have_derived() -> None:
    """The cached, directly-built prefix is the one the round trip produced.

    ``network`` used to be ``ipaddress.ip_interface(...).network``, rebuilt on
    every access. It is now a :func:`functools.cached_property` that constructs
    the network from the address's integer form, which skips the interface
    object and the ``str`` round trip :mod:`ipaddress` pays when handed an
    address object back (entry 7 of ``docs/follow-ups.md``). This pins the two
    against each other over every prefix length of both families, which is what
    makes that change invisible rather than merely plausible.
    """
    for text, width in (("10.11.12.13", 32), ("2001:db8:1234:5678:9abc:def0:1234:5678", 128)):
        model = IPv4Address if width == 32 else IPv6Address
        for prefix_length in range(width + 1):
            address = model.model_validate(f"{text}/{prefix_length}")
            expected = ipaddress.ip_interface(f"{text}/{prefix_length}").network
            assert address.network == expected
            assert str(address.network) == str(expected)
            # Cached: the second read is the same object, not merely an equal one.
            assert address.network is address.network


def test_caching_the_prefix_leaves_the_model_itself_untouched() -> None:
    """Reading ``network`` must not change equality, dumps or the JSON Schema.

    ``cached_property`` writes into the instance ``__dict__``, which is also
    where pydantic keeps field values, so that is the one way this change could
    leak out of the model.
    """
    address = IPv4Address.model_validate("10.0.0.1/24")
    twin = IPv4Address.model_validate("10.0.0.1/24")
    dumped = address.model_dump()
    as_json = address.model_dump_json()
    schema = IPv4Address.model_json_schema()

    assert address.network == ipaddress.IPv4Network("10.0.0.0/24")

    assert address == twin
    assert address.model_dump() == dumped
    assert address.model_dump_json() == as_json
    assert IPv4Address.model_json_schema() == schema
    assert "network" not in dumped
    assert address.model_fields_set == {"ip", "prefix_length"}


def test_vlan_set_helpers() -> None:
    empty = VlanSet()
    assert not empty
    assert str(empty) == "none"
    assert "10" not in empty
    assert list(VlanSet.model_validate("10-12")) == [10, 11, 12]
    assert VlanSet.model_validate({"ranges": [(20, 21), (10, 11)]}).to_string() == "10-11,20-21"


@pytest.mark.parametrize("value", [None, 3.5, b"00:11:22:33:44:55"])
def test_mac_rejects_non_string_values(value: Any) -> None:
    with pytest.raises(ValueError):
        normalise_mac(value)


@pytest.mark.parametrize("value", [True, None, 1.5])
def test_bitrate_rejects_unusable_values(value: Any) -> None:
    with pytest.raises(ValueError):
        parse_bitrate(value)


def test_bitrate_accepts_a_whole_float() -> None:
    assert parse_bitrate(1000.0) == 1000


@pytest.mark.parametrize(
    "key",
    ["/name", "a" * 64, "UPPER.example.com/tier", "x" * 254 + "/tier", "-bad.example/tier"],
)
def test_more_invalid_label_keys(key: str) -> None:
    doc = device()
    doc["metadata"]["labels"] = {key: "v"}
    assert parse_failure(doc).location == "metadata.labels"


def test_interface_ref_mapping_form_rejects_unknown_keys() -> None:
    """Every problem in a document is reported, not just the first one."""
    error = parse_failure(
        document(
            "cable",
            "c",
            {"endpoints": [{"device": "a", "port": "1"}, "b:2"], "medium": "copper"},
        )
    )
    assert len(error.issues) == 2
    assert [issue.location for issue in error.issues] == [
        "spec.endpoints[0].interface",
        "spec.endpoints[0].port",
    ]
    assert error.issues[1].rule == "NG-D005"
    assert "2 schema errors" in str(error)


# --------------------------------------------------------------------------- #
# Bounded diagnostics
# --------------------------------------------------------------------------- #

#: Long enough that no bounded diagnostic can contain it, short enough to build.
PATHOLOGICAL = "x" * 200_000

#: A generous ceiling on a single diagnostic line. The prose around the value is
#: a few hundred characters at most, and ``repr`` can expand a character into an
#: escape, so this is well above what a bounded message costs and well below the
#: 200 135 characters entry 3 of ``docs/follow-ups.md`` measured.
BOUND = 2_000


def test_echo_value_leaves_a_short_value_alone() -> None:
    assert echo_value("aa:bb") == "'aa:bb'"
    assert echo_value(1234) == "1234"
    # ``repr`` rather than the value itself, so a newline stays on one line.
    assert echo_value("a\nb") == "'a\\nb'"


def test_echo_value_echoes_the_longest_value_that_fits() -> None:
    exact = "y" * MAX_ECHOED_VALUE_LENGTH
    assert echo_value(exact) == repr(exact)
    assert "more characters" not in echo_value(exact)


def test_echo_value_elides_one_character_past_the_limit() -> None:
    over = "y" * (MAX_ECHOED_VALUE_LENGTH + 1)
    assert echo_value(over) == f"{'y' * MAX_ECHOED_VALUE_LENGTH!r}… (+1 more characters)"


def test_echo_value_bounds_a_value_that_is_not_a_string() -> None:
    """A YAML scalar need not be a string, and a list has no prefix of its own."""
    rendered = echo_value(list(range(10_000)))
    assert len(rendered) < BOUND
    assert rendered.startswith("[0, 1, 2,")
    assert "more characters" in rendered


def test_a_pathological_mac_is_not_echoed_in_full() -> None:
    with pytest.raises(ValueError) as excinfo:
        normalise_mac(PATHOLOGICAL)
    message = str(excinfo.value)
    assert len(message) < BOUND
    assert message.startswith("'xxx")
    assert f"(+{200_000 - MAX_ECHOED_VALUE_LENGTH} more characters)" in message
    # The advice survives the truncation; only the value is clipped.
    assert "expected xx:xx:xx:xx:xx:xx" in message


def test_a_short_mac_is_still_echoed_verbatim() -> None:
    """The whole point of quoting the value is that a typo is visible."""
    with pytest.raises(ValueError, match=re.escape("'zz:11:22:33:44:55' is not a MAC address")):
        normalise_mac("zz:11:22:33:44:55")


@pytest.mark.parametrize(
    ("what", "build"),
    [
        ("mac", lambda big: device(interfaces=[{"name": "eth0", "type": "ethernet", "mac": big}])),
        (
            "speed",
            lambda big: device(interfaces=[{"name": "eth0", "type": "ethernet", "speed": big}]),
        ),
        (
            "vlan set",
            lambda big: device(
                interfaces=[
                    {
                        "name": "eth0",
                        "type": "ethernet",
                        "vlan": {"mode": "trunk", "trunk_vlans": big},
                    }
                ]
            ),
        ),
        (
            "ipv4 address",
            lambda big: device(
                "router",
                interfaces=[{"name": "eth0", "type": "ethernet", "ipv4": {"addresses": [big]}}],
            ),
        ),
        (
            "netmask",
            lambda big: device(
                "router",
                interfaces=[
                    {
                        "name": "eth0",
                        "type": "ethernet",
                        "ipv4": {"addresses": [{"ip": "10.0.0.1", "netmask": big}]},
                    }
                ],
            ),
        ),
        (
            "cable endpoint",
            lambda big: document("cable", "c1", {"endpoints": [big, "b:2"], "medium": "copper"}),
        ),
        ("label key", lambda big: _with_labels(device(), {big: "v"})),
        ("annotation key", lambda big: _with_annotations(device(), {big: "v"})),
        ("unknown key", lambda big: _with_extra_key(device(), big)),
        ("kind", lambda big: {**device(), "kind": big}),
    ],
)
def test_no_rejected_value_reaches_a_diagnostic_in_full(what: str, build: Any) -> None:
    """Entry 3 of ``docs/follow-ups.md``: every echo of a rejected value is clipped."""
    error = parse_failure(build(PATHOLOGICAL))
    rendered = str(error)
    assert len(rendered) < BOUND, f"{what} diagnostic is {len(rendered)} characters"
    assert PATHOLOGICAL[:1000] not in rendered


def _with_labels(doc: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
    doc["metadata"]["labels"] = labels
    return doc


def _with_annotations(doc: dict[str, Any], annotations: dict[str, str]) -> dict[str, Any]:
    doc["metadata"]["annotations"] = annotations
    return doc


def _with_extra_key(doc: dict[str, Any], key: str) -> dict[str, Any]:
    doc["spec"][key] = "v"
    return doc
