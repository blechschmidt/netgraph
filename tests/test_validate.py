"""Tests for the semantic validation engine (``docs/schema.md`` §10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from netgraph.config import (
    CONFIG_FILE_NAME,
    Config,
    ValidationConfig,
    load_config,
    parse_config,
)
from netgraph.errors import ConfigurationError
from netgraph.loader import Inventory, load_tree
from netgraph.models import API_VERSION, IPv4Address, IPv6Address
from netgraph.rules import (
    RULE_IDS,
    RULES,
    WILDCARD,
    Severity,
    known_rule,
    resolve_rule_id,
    rule_for,
)
from netgraph.validate import (
    Finding,
    _reserved_role,
    errors_only,
    has_errors,
    summarise,
    validate,
)

# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def doc(kind: str, name: str, spec: dict[str, Any], **metadata: Any) -> str:
    """Render one element document."""
    return yaml.safe_dump(
        {
            "apiVersion": API_VERSION,
            "kind": kind,
            "metadata": {"name": name, **metadata},
            "spec": spec,
        },
        sort_keys=False,
    )


def cable(name: str, left: str, right: str, **metadata: Any) -> str:
    return doc("cable", name, {"endpoints": [left, right], "medium": "copper"}, **metadata)


def link(name: str, left: str, right: str, **spec: Any) -> str:
    """A cable with extra ``spec`` keys — ``medium``, ``speed``, ``duplex``."""
    return doc("cable", name, {"endpoints": [left, right], "medium": "copper", **spec})


def eth(name: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "type": "ethernet", **fields}


def wifi(name: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "type": "wifi", **fields}


def dongle(
    name: str,
    *,
    attached_to: str | None = None,
    speed: str | None = None,
    downstream: dict[str, Any] | None = None,
) -> str:
    """A USB adapter whose upstream port is ``usb0``.

    The downstream port defaults to a spare one, so an adapter that is only
    scenery for the rule under test contributes no findings of its own.
    """
    upstream: dict[str, Any] = {"name": "usb0", "type": "usb"}
    if attached_to is not None:
        upstream["attached_to"] = attached_to
    if speed is not None:
        upstream["speed"] = speed
    return doc(
        "adapter",
        name,
        {
            "upstream": upstream,
            "interfaces": [downstream or eth("en0", enabled=False, mtu=1500)],
        },
    )


def access(vlan: int) -> dict[str, Any]:
    return {"mode": "access", "access_vlan": vlan}


def trunk(vlans: list[int], native: int | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"mode": "trunk", "trunk_vlans": vlans}
    if native is not None:
        block["native_vlan"] = native
    return block


def loopback(name: str, addresses: list[str]) -> dict[str, Any]:
    """A loopback carrying whichever families ``addresses`` mentions."""
    interface: dict[str, Any] = {"name": name, "type": "loopback"}
    for address in addresses:
        interface.setdefault("ipv6" if ":" in address else "ipv4", []).append(address)
    return interface


def lag(name: str, members: list[str], **fields: Any) -> dict[str, Any]:
    return {"name": name, "type": "lag", "members": members, **fields}


def bridge(name: str, members: list[str], **fields: Any) -> dict[str, Any]:
    return {"name": name, "type": "bridge", "members": members, **fields}


def sub(name: str, parent: str, vid: int, **fields: Any) -> dict[str, Any]:
    """A ``type: vlan`` sub-interface encapsulating ``vid``."""
    return {"name": name, "type": "vlan", "parent": parent, "vlan": access(vid), **fields}


def host_with_mac(mac: str) -> str:
    """One host whose only port carries ``mac`` — the MAC-bit rules' fixture."""
    return doc(
        "computer",
        "pc1",
        {"interfaces": [eth("eth0", mac=mac, mtu=1500, ipv4=["10.0.0.1/30"])]},
    )


def _family(address: str) -> dict[str, Any]:
    """``{"ipv4": [...]}`` or ``{"ipv6": [...]}``, whichever the address is."""
    return {"ipv6" if ":" in address else "ipv4": [address]}


def write(root: Path, **documents: str) -> Path:
    """Write each keyword as ``<name>.yaml``; ``__`` becomes a folder separator."""
    for key, content in documents.items():
        path = root / f"{key.replace('__', '/')}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def load(root: Path, **documents: str) -> Inventory:
    """Write the documents, load the tree and assert it parsed cleanly."""
    write(root, **documents)
    inventory = load_tree(root)
    assert inventory.errors == [], [str(error) for error in inventory.errors]
    return inventory


def join(*documents: str) -> str:
    """Combine documents into one multi-document YAML file."""
    return "---\n".join(documents)


def rules_of(findings: list[Finding]) -> list[str]:
    return [finding.rule for finding in findings]


def only(findings: list[Finding], rule: str) -> list[Finding]:
    return [finding for finding in findings if finding.rule == rule]


#: A switch and a host wired together, with nothing wrong: the baseline every
#: "does not fire" test starts from. The host's address sits in a point-to-point
#: prefix on purpose: the switch is layer-2 and holds none, so a /24 here would
#: be a subnet with a single member and trip W105.
def clean_pair() -> str:
    return join(
        doc(
            "switch",
            "sw1",
            {"interfaces": [eth("Gi0/1", mtu=1500, vlan=access(10))]},
        ),
        doc(
            "computer",
            "pc1",
            {"interfaces": [eth("eth0", mtu=1500, vlan=access(10), ipv4=["10.0.0.1/30"])]},
        ),
        cable("c1", "sw1:Gi0/1", "pc1:eth0"),
    )


def test_a_consistent_inventory_produces_no_findings(tmp_path: Path) -> None:
    assert validate(load(tmp_path, net=clean_pair())) == []


# --------------------------------------------------------------------------- #
# E001 -- unknown endpoints
# --------------------------------------------------------------------------- #


def test_e001_unknown_device(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1")]}),
            cable("c1", "sw1:Gi0/1", "ghost:eth0"),
        ),
    )
    finding = only(validate(inventory), "E001")[0]
    assert finding.severity is Severity.ERROR
    assert "no element named 'ghost'" in finding.message
    # §7.1 sorts the endpoints canonically, which puts 'ghost' first in the
    # model -- but the field path is the one the *document* uses, so that a
    # report underlines the line the reader actually has to fix.
    assert finding.field_path == ("spec", "endpoints", 1)
    assert finding.file == "net.yaml"


def test_e001_unknown_interface_lists_the_declared_ones(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1"), eth("Gi0/2")]}),
            doc("computer", "pc1", {"interfaces": [eth("eth0")]}),
            cable("c1", "sw1:Gi9/9", "pc1:eth0"),
        ),
    )
    finding = only(validate(inventory), "E001")[0]
    assert "has no interface 'Gi9/9'" in finding.message
    assert "'Gi0/1', 'Gi0/2'" in finding.message


def test_e001_endpoint_naming_an_element_without_interfaces(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1")]}),
            cable("c1", "sw1:Gi0/1", "c2:eth0"),
            cable("c2", "sw1:Gi0/1", "sw1:Gi0/1"),
        ),
    )
    messages = [finding.message for finding in only(validate(inventory), "E001")]
    assert any("is a cable, which owns no interfaces" in message for message in messages)


def test_e001_ambiguous_device_reference_names_every_candidate(tmp_path: Path) -> None:
    switch = doc("switch", "sw1", {"interfaces": [eth("Gi0/1")]})
    inventory = load(
        tmp_path,
        a__sw1=switch,
        b__sw1=switch,
        c__link=cable("c1", "sw1:Gi0/1", "sw1:Gi0/1"),
    )
    finding = only(validate(inventory), "E001")[0]
    assert "is ambiguous here" in finding.message
    assert "'a/sw1', 'b/sw1'" in finding.message


def test_e001_abbreviates_an_unreasonably_long_interface_list(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth(f"Gi0/{n}") for n in range(12)]}),
            cable("c1", "sw1:Gi9/9", "sw1:Gi0/0"),
        ),
    )
    assert "and 4 more" in only(validate(inventory), "E001")[0].message


def test_an_adapter_attached_to_an_unknown_host_still_validates(tmp_path: Path) -> None:
    """The dangling attachment is reported as ``E015``, and nothing else derails."""
    inventory = load(
        tmp_path,
        net=doc(
            "adapter",
            "dongle",
            {
                "upstream": {"name": "usb0", "type": "usb", "attached_to": "ghost"},
                "interfaces": [eth("enx0", enabled=False, ipv4=["10.0.0.1/30"])],
            },
        ),
    )
    assert rules_of(validate(inventory)) == ["E015"]


def test_an_adapter_upstream_port_is_a_valid_endpoint(tmp_path: Path) -> None:
    """§8.1: the upstream port is referenceable even though it is not an interface."""
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", vlan=access(1))]}),
            doc(
                "adapter",
                "dock",
                {
                    "upstream": {"name": "usb0", "type": "usb"},
                    "interfaces": [eth("enx0", ipv4=["10.0.0.9/24"])],
                },
            ),
            cable("c1", "sw1:Gi0/1", "dock:usb0"),
        ),
    )
    assert only(validate(inventory), "E001") == []


# --------------------------------------------------------------------------- #
# E002 -- double termination
# --------------------------------------------------------------------------- #


def test_e002_interface_terminated_by_two_cables(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1")]}),
            doc("computer", "pc1", {"interfaces": [eth("eth0")]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0")]}),
            cable("c1", "sw1:Gi0/1", "pc1:eth0"),
            cable("c2", "sw1:Gi0/1", "pc2:eth0"),
        ),
    )
    findings = only(validate(inventory), "E002")
    assert len(findings) == 1
    assert "'sw1:Gi0/1' is terminated by 2 cables: 'c1', 'c2'" in findings[0].message
    # Anchored at the first cable, and both cables can suppress it.
    assert findings[0].elements == ("c1", "c2", "sw1")


def test_e002_reports_a_cable_that_loops_onto_one_interface(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1")]}),
            cable("c1", "sw1:Gi0/1", "sw1:Gi0/1"),
        ),
    )
    findings = only(validate(inventory), "E002")
    assert len(findings) == 1
    assert "both endpoints of cable 'c1'" in findings[0].message


def test_one_cable_per_interface_is_fine(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=clean_pair())), "E002") == []


# --------------------------------------------------------------------------- #
# E003 -- duplicate MAC
# --------------------------------------------------------------------------- #


def test_e003_duplicate_mac_across_devices(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mac="00:11:22:33:44:55")]}),
            doc("computer", "pc1", {"interfaces": [eth("eth0", mac="00-11-22-33-44-55")]}),
        ),
    )
    findings = only(validate(inventory), "E003")
    assert len(findings) == 1
    # Both spellings normalise to the same address, so the clash is still found.
    assert "00:11:22:33:44:55 is used by 2 interfaces" in findings[0].message
    assert findings[0].elements == ("sw1", "pc1")


def test_e003_exempts_a_lag_and_its_members(tmp_path: Path) -> None:
    """Bonding really does put one address on every lane; that is not a clash."""
    mac = "aa:bb:cc:dd:ee:ff"
    inventory = load(
        tmp_path,
        net=doc(
            "server",
            "srv1",
            {
                "interfaces": [
                    eth("eno1", mac=mac),
                    eth("eno2", mac=mac),
                    {
                        "name": "bond0",
                        "type": "lag",
                        "mac": mac,
                        "members": ["eno1", "eno2"],
                        "ipv4": ["10.0.0.5/24"],
                    },
                ]
            },
        ),
    )
    assert only(validate(inventory), "E003") == []


def test_e003_exempts_a_vlan_sub_interface_sharing_its_parent_mac(tmp_path: Path) -> None:
    mac = "aa:bb:cc:dd:ee:01"
    inventory = load(
        tmp_path,
        net=doc(
            "router",
            "r1",
            {
                "interfaces": [
                    eth("eth0", mac=mac, ipv4=["10.0.0.1/24"]),
                    {
                        "name": "eth0.30",
                        "type": "vlan",
                        "mac": mac,
                        "parent": "eth0",
                        "vlan": access(30),
                        "ipv4": ["10.30.0.1/24"],
                    },
                ]
            },
        ),
    )
    assert only(validate(inventory), "E003") == []


def test_e003_still_fires_for_two_unrelated_interfaces_on_one_device(tmp_path: Path) -> None:
    mac = "aa:bb:cc:dd:ee:02"
    inventory = load(
        tmp_path,
        net=doc(
            "server",
            "srv1",
            {"interfaces": [eth("eno1", mac=mac), eth("eno2", mac=mac)]},
        ),
    )
    assert len(only(validate(inventory), "E003")) == 1


# --------------------------------------------------------------------------- #
# E004 -- duplicate IP
# --------------------------------------------------------------------------- #


def test_e004_same_address_in_the_same_subnet(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.7/24"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["10.0.0.7/24"])]}),
        ),
    )
    findings = only(validate(inventory), "E004")
    assert len(findings) == 1
    assert "10.0.0.7 in 10.0.0.0/24" in findings[0].message
    assert "the untagged domain" in findings[0].message


def test_e004_covers_ipv6(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv6=["2001:db8::1/64"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv6=["2001:DB8::1/64"])]}),
        ),
    )
    assert len(only(validate(inventory), "E004")) == 1


def test_e004_ignores_the_same_address_in_a_different_vlan(tmp_path: Path) -> None:
    """Two VLANs are two broadcast domains; re-using a prefix in each is normal."""
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "computer",
                "pc1",
                {"interfaces": [eth("eth0", vlan=access(10), ipv4=["10.0.0.7/24"])]},
            ),
            doc(
                "computer",
                "pc2",
                {"interfaces": [eth("eth0", vlan=access(20), ipv4=["10.0.0.7/24"])]},
            ),
        ),
    )
    assert only(validate(inventory), "E004") == []


def test_e004_ignores_the_same_host_address_in_a_different_subnet(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.7/24"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["10.0.1.7/24"])]}),
        ),
    )
    assert only(validate(inventory), "E004") == []


def test_e004_ignores_loopback_addresses(tmp_path: Path) -> None:
    """127.0.0.1 and ::1 are host-scoped, so every machine may declare them.

    RFC 1122 §3.2.1.3 and RFC 4291 §2.5.3: a loopback address never appears on
    a link, so two hosts holding one are not in conflict.
    """
    loopback = {
        "name": "lo",
        "type": "loopback",
        "ipv4": ["127.0.0.1/8"],
        "ipv6": ["::1/128"],
    }
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [loopback]}),
            doc("computer", "pc2", {"interfaces": [loopback]}),
            doc("server", "srv1", {"interfaces": [loopback]}),
        ),
    )
    assert only(validate(inventory), "E004") == []


# --------------------------------------------------------------------------- #
# E005 -- VLAN mismatch
# --------------------------------------------------------------------------- #


def test_e005_access_ports_in_different_vlans(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", vlan=access(10))]}),
            doc("switch", "sw2", {"interfaces": [eth("Gi0/1", vlan=access(20))]}),
            cable("c1", "sw1:Gi0/1", "sw2:Gi0/1"),
        ),
    )
    findings = only(validate(inventory), "E005")
    assert len(findings) == 1
    assert "in VLAN 10" in findings[0].message and "in VLAN 20" in findings[0].message
    assert findings[0].elements == ("c1", "sw1", "sw2")


def test_e005_ignores_a_host_port_without_a_vlan_block(tmp_path: Path) -> None:
    """§11.1: an untagged host facing an access port is the normal arrangement."""
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", vlan=access(10))]}),
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/24"])]}),
            cable("c1", "sw1:Gi0/1", "pc1:eth0"),
        ),
    )
    assert only(validate(inventory), "E005") == []


def test_e005_reports_an_access_port_facing_a_trunk(tmp_path: Path) -> None:
    """§10.5: the access port drops every tagged frame the trunk sends."""
    trunk_block = {"mode": "trunk", "trunk_vlans": "10,20"}
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", vlan=trunk_block)]}),
            doc("switch", "sw2", {"interfaces": [eth("Gi0/1", vlan=access(20))]}),
            cable("c1", "sw1:Gi0/1", "sw2:Gi0/1"),
        ),
    )
    findings = only(validate(inventory), "E005")
    assert len(findings) == 1
    assert "access port 'sw2:Gi0/1' in VLAN 20" in findings[0].message
    assert "trunk port 'sw1:Gi0/1' carrying VLANs 10,20" in findings[0].message


def test_e005_resolves_through_the_lag_master(tmp_path: Path) -> None:
    """§10.6: a cable on a bundled member is governed by the aggregate."""
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "switch",
                "sw1",
                {
                    "interfaces": [
                        eth("Gi0/1"),
                        {
                            "name": "po1",
                            "type": "lag",
                            "members": ["Gi0/1"],
                            "vlan": access(10),
                        },
                    ]
                },
            ),
            doc("switch", "sw2", {"interfaces": [eth("Gi0/1", vlan=access(20))]}),
            cable("c1", "sw1:Gi0/1", "sw2:Gi0/1"),
        ),
    )
    findings = only(validate(inventory), "E005")
    assert len(findings) == 1
    assert "aggregated by 'po1'" in findings[0].message


def two_trunks(left: dict[str, Any], right: dict[str, Any]) -> str:
    """Two switches whose only ports are trunks, cabled together."""
    return join(
        doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=left)]}),
        doc("switch", "sw2", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=right)]}),
        cable("c1", "sw1:Gi0/1", "sw2:Gi0/1"),
    )


def test_e005_reports_an_access_port_that_sorts_first(tmp_path: Path) -> None:
    """§7.1 sorts the endpoints, so the access end can be either of the two."""
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw-a", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=access(20))]}),
            doc("switch", "sw-b", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=trunk([10, 30]))]}),
            cable("c1", "sw-a:Gi0/1", "sw-b:Gi0/1"),
        ),
    )
    findings = only(validate(inventory), "E005")
    assert len(findings) == 1
    assert findings[0].message.index("access port 'sw-a:Gi0/1'") < findings[0].message.index(
        "trunk port 'sw-b:Gi0/1'"
    )


def test_e005_reports_trunks_whose_vlan_sets_are_disjoint(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=two_trunks(trunk([10, 20]), trunk([30, 40])))
    findings = only(validate(inventory), "E005")
    assert len(findings) == 1
    assert "the two sets are disjoint" in findings[0].message
    assert "carrying VLANs 10,20" in findings[0].message


def test_e005_accepts_trunks_that_share_a_vlan(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=two_trunks(trunk([10, 20]), trunk([20, 30])))
    assert only(validate(inventory), "E005") == []


def test_e005_reports_trunks_that_disagree_about_the_native_vlan(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=two_trunks(trunk([10, 20], native=10), trunk([10, 20], native=20)),
    )
    findings = only(validate(inventory), "E005")
    assert len(findings) == 1
    assert "native VLAN 10" in findings[0].message and "native VLAN 20" in findings[0].message


def test_e005_ignores_a_native_vlan_only_one_end_spells_out(tmp_path: Path) -> None:
    """Leaving `native_vlan` off means "the default", not "a different VLAN"."""
    inventory = load(tmp_path, net=two_trunks(trunk([1, 10], native=1), trunk([1, 10])))
    assert only(validate(inventory), "E005") == []


def test_e005_still_reports_a_trunk_whose_native_vlan_the_access_port_matches(
    tmp_path: Path,
) -> None:
    """The untagged VLAN crosses; everything else the trunk carries is dropped."""
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=trunk([10, 20], 10))]}
            ),
            doc("switch", "sw2", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=access(10))]}),
            cable("c1", "sw1:Gi0/1", "sw2:Gi0/1"),
        ),
    )
    findings = only(validate(inventory), "E005")
    assert len(findings) == 1
    assert "only the trunk's native VLAN 10 crosses" in findings[0].message


# --------------------------------------------------------------------------- #
# E006 -- adapter capacity
# --------------------------------------------------------------------------- #


def adapter_with(ports: int | None, count: int) -> str:
    spec: dict[str, Any] = {
        "upstream": {"name": "usb0", "type": "usb"},
        "interfaces": [
            eth(f"enx{index}", ipv4=[f"172.16.0.{index + 1}/24"]) for index in range(count)
        ],
    }
    if ports is not None:
        spec["ports"] = ports
    return doc("adapter", "dock", spec)


def test_e006_more_interfaces_than_ports(tmp_path: Path) -> None:
    findings = only(validate(load(tmp_path, net=adapter_with(2, 3))), "E006")
    assert len(findings) == 1
    assert "declares 3 downstream interfaces but has only 2 ports" in findings[0].message


def test_e006_singular_port_reads_naturally(tmp_path: Path) -> None:
    findings = only(validate(load(tmp_path, net=adapter_with(1, 2))), "E006")
    assert "has only 1 port" in findings[0].message


def test_e006_quiet_when_within_capacity_or_undeclared(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=adapter_with(4, 2))), "E006") == []
    assert only(validate(load(tmp_path, net=adapter_with(None, 9))), "E006") == []


# --------------------------------------------------------------------------- #
# W101 -- unaddressed, unswitched interface
# --------------------------------------------------------------------------- #


def test_w101_interface_with_neither_addresses_nor_vlan(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=doc("computer", "pc1", {"interfaces": [eth("eth0")]}))
    findings = only(validate(inventory), "W101")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert findings[0].field_path == ("spec", "interfaces", 0)


@pytest.mark.parametrize(
    "interface",
    [
        eth("eth0", ipv4=["10.0.0.1/24"]),
        eth("eth0", ipv6=["2001:db8::1/64"]),
        eth("eth0", vlan=access(10)),
        eth("eth0", enabled=False),
    ],
    ids=["ipv4", "ipv6", "switchport", "disabled"],
)
def test_w101_stays_quiet(tmp_path: Path, interface: dict[str, Any]) -> None:
    inventory = load(tmp_path, net=doc("computer", "pc1", {"interfaces": [interface]}))
    assert only(validate(inventory), "W101") == []


def test_w101_skips_hub_ports(tmp_path: Path) -> None:
    """§6.5: a hub port cannot hold an address, so the rule would only add noise."""
    inventory = load(tmp_path, net=doc("hub", "hub1", {"interfaces": [eth("p1"), eth("p2")]}))
    assert only(validate(inventory), "W101") == []


def test_w101_skips_interfaces_that_carry_a_higher_layer(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "server",
            "srv1",
            {
                "interfaces": [
                    eth("eno1"),
                    eth("eno2"),
                    {
                        "name": "bond0",
                        "type": "lag",
                        "members": ["eno1", "eno2"],
                        "ipv4": ["10.0.0.5/24"],
                    },
                ]
            },
        ),
    )
    assert only(validate(inventory), "W101") == []


# --------------------------------------------------------------------------- #
# W102 -- MTU mismatch
# --------------------------------------------------------------------------- #


def test_w102_mtu_mismatch_across_a_cable(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=9000, vlan=access(10))]}),
            doc(
                "computer",
                "pc1",
                {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/24"])]},
            ),
            cable("c1", "sw1:Gi0/1", "pc1:eth0"),
        ),
    )
    findings = only(validate(inventory), "W102")
    assert len(findings) == 1
    assert "MTU 1500" in findings[0].message and "MTU 9000" in findings[0].message


def test_w102_needs_both_ends_to_state_an_mtu(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=9000, vlan=access(10))]}),
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/24"])]}),
            cable("c1", "sw1:Gi0/1", "pc1:eth0"),
        ),
    )
    assert only(validate(inventory), "W102") == []


# --------------------------------------------------------------------------- #
# W103 -- orphan device
# --------------------------------------------------------------------------- #


def test_w103_device_without_cables(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc("computer", "spare", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/30"])]}),
    )
    findings = only(validate(inventory), "W103")
    assert len(findings) == 1
    assert findings[0].elements == ("spare",)


def test_w103_counts_an_adapter_attachment_as_connectivity(tmp_path: Path) -> None:
    """§8.2: ``attached_to`` is itself a graph edge, so the host is not isolated."""
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "laptop", {"interfaces": [eth("wlan0", ipv4=["10.0.0.2/24"])]}),
            doc(
                "adapter",
                "dongle",
                {
                    "upstream": {"name": "usb0", "type": "usb", "attached_to": "laptop"},
                    "interfaces": [eth("enx0", ipv4=["10.0.0.3/24"])],
                },
            ),
        ),
    )
    assert only(validate(inventory), "W103") == []


def test_w103_does_not_pile_onto_a_broken_reference(tmp_path: Path) -> None:
    """A device whose cable names a missing interface is still a cabled device."""
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", vlan=access(10))]}),
            cable("c1", "sw1:Gi9/9", "sw1:Gi0/1"),
        ),
    )
    findings = validate(inventory)
    assert rules_of(only(findings, "E001")) == ["E001"]
    assert only(findings, "W103") == []


# --------------------------------------------------------------------------- #
# W104 -- IP on a layer-2 access port
# --------------------------------------------------------------------------- #


def test_w104_ip_on_an_access_port_of_a_layer2_switch(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {"interfaces": [eth("Gi0/1", vlan=access(10), ipv4=["10.0.0.2/24"])]},
        ),
    )
    findings = only(validate(inventory), "W104")
    assert len(findings) == 1
    assert "put it on a 'vlan' (SVI) interface" in findings[0].message


def test_w104_accepts_a_management_svi(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "interfaces": [
                    eth("Gi0/1", vlan=access(10)),
                    {
                        "name": "vlan10",
                        "type": "vlan",
                        "parent": "Gi0/1",
                        "vlan": access(10),
                        "ipv4": ["10.0.0.2/24"],
                    },
                ]
            },
        ),
    )
    assert only(validate(inventory), "W104") == []


def test_w104_accepts_a_layer3_switch(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "forwarding": {"ipv4": True, "ipv6": False},
                "interfaces": [eth("Gi0/1", vlan=access(10), ipv4=["10.0.0.2/24"])],
            },
        ),
    )
    assert only(validate(inventory), "W104") == []


def test_w104_does_not_apply_to_routers(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "router",
            "r1",
            {"interfaces": [eth("eth0", vlan=access(10), ipv4=["10.0.0.1/24"])]},
        ),
    )
    assert only(validate(inventory), "W104") == []


# --------------------------------------------------------------------------- #
# W105 -- a subnet with a single member
# --------------------------------------------------------------------------- #


def test_w105_a_prefix_only_one_element_is_addressed_in(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/24"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["192.168.0.1/24"])]}),
            cable("c1", "pc1:eth0", "pc2:eth0"),
        ),
    )
    findings = only(validate(inventory), "W105")
    assert len(findings) == 2, "each half of the mis-masked link is alone in its own prefix"
    first = findings[0]
    assert first.severity is Severity.WARNING
    assert "only one element is addressed in subnet '10.0.0.0/24': 'pc1:eth0'" in first.message
    assert "(10.0.0.1/24)" in first.message
    assert first.elements == ("pc1",)
    assert first.field_path == ("spec", "interfaces", 0)


def test_w105_is_quiet_when_the_prefix_is_shared(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/24"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["10.0.0.2/24"])]}),
            cable("c1", "pc1:eth0", "pc2:eth0"),
        ),
    )
    assert only(validate(inventory), "W105") == []


@pytest.mark.parametrize(
    "address",
    [
        # A host route holds one address by definition.
        "10.0.0.1/32",
        "10.0.0.1/31",
        "10.0.0.1/30",
        "2001:db8::1/128",
        "2001:db8::1/127",
        "2001:db8::1/126",
    ],
)
def test_w105_exempts_host_and_point_to_point_prefixes(tmp_path: Path, address: str) -> None:
    """The far end of a point-to-point link is routinely somebody else's router."""
    inventory = load(
        tmp_path, net=doc("computer", "pc1", {"interfaces": [eth("eth0", **_family(address))]})
    )
    assert only(validate(inventory), "W105") == []


def test_w105_counts_elements_rather_than_addresses(tmp_path: Path) -> None:
    """Two ports of one device in one prefix still leave the prefix with one member."""
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", ipv4=["10.0.0.1/24"]),
                    eth("eth1", ipv4=["10.0.0.2/24"]),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "W105")
    assert len(findings) == 1
    assert "'pc1:eth0', 'pc1:eth1'" in findings[0].message


def test_w105_ignores_loopback_and_link_local_addresses(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    {"name": "lo0", "type": "loopback", "ipv4": ["127.0.0.1/8"]},
                    eth("eth0", ipv6=["fe80::1/64"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "W105") == []


# --------------------------------------------------------------------------- #
# W106 -- one address claimed twice inside a subnet
# --------------------------------------------------------------------------- #


def clashing_pair(left: dict[str, Any], right: dict[str, Any]) -> str:
    """Two hosts holding 10.0.0.1/24, each with the given ``vlan`` block."""
    return join(
        doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/24"], **left)]}),
        doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/24"], **right)]}),
    )


def test_w106_the_same_address_in_two_broadcast_domains(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=clashing_pair({"vlan": access(10)}, {"vlan": access(20)}))
    findings = only(validate(inventory), "W106")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.WARNING
    assert "address 10.0.0.1 in subnet '10.0.0.0/24'" in finding.message
    assert "VLAN 10, VLAN 20" in finding.message
    assert finding.elements == ("pc1", "pc2")
    assert finding.field_path == ("spec", "interfaces", 0)
    # E004 scopes itself to one VLAN, so it deliberately says nothing here.
    assert only(validate(inventory), "E004") == []


def test_w106_leaves_a_clash_inside_one_vlan_to_e004(tmp_path: Path) -> None:
    """One mistake, one finding: E004 is the error, so W106 stays quiet."""
    inventory = load(tmp_path, net=clashing_pair({"vlan": access(10)}, {"vlan": access(10)}))
    findings = validate(inventory)
    assert only(findings, "E004"), "the clash is inside one VLAN, which E004 owns"
    assert only(findings, "W106") == []


def test_w106_treats_two_untagged_ports_as_one_domain(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=clashing_pair({}, {}))
    assert only(validate(inventory), "W106") == []
    assert only(validate(inventory), "E004")


def test_w106_reports_an_untagged_port_facing_a_tagged_one(tmp_path: Path) -> None:
    """Different scopes, so E004 is silent — but nothing says they are separated."""
    inventory = load(tmp_path, net=clashing_pair({}, {"vlan": access(20)}))
    findings = only(validate(inventory), "W106")
    assert len(findings) == 1
    assert "the untagged domain, VLAN 20" in findings[0].message


def test_w106_ignores_one_element_holding_the_address_twice(tmp_path: Path) -> None:
    """A single element cannot be in conflict with itself over reachability."""
    inventory = load(
        tmp_path,
        net=doc(
            "router",
            "r1",
            {
                "interfaces": [
                    eth("eth0", ipv4=["10.0.0.1/24"], vlan=access(10)),
                    eth("eth1", ipv4=["10.0.0.1/24"], vlan=access(20)),
                ]
            },
        ),
    )
    assert only(validate(inventory), "W106") == []


def test_w106_ignores_the_same_address_in_a_different_prefix(tmp_path: Path) -> None:
    """A prefix is the unit of the rule: /24 and /25 are two subnets."""
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/24"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/25"])]}),
        ),
    )
    assert only(validate(inventory), "W106") == []


# --------------------------------------------------------------------------- #
# E007 -- cyclic interface stacking
# --------------------------------------------------------------------------- #


def test_e007_reports_a_two_step_stacking_cycle(tmp_path: Path) -> None:
    """NG-I002 catches `parent: self`; nothing per-document catches a longer loop."""
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", ipv4=["10.0.0.1/30"]),
                    sub("vlan-a", "vlan-b", 10),
                    sub("vlan-b", "vlan-a", 10),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "E007")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    message = findings[0].message
    assert "'pc1'" in message
    assert "'pc1:vlan-a'" in message and "'pc1:vlan-b'" in message


def test_e007_reports_each_cycle_once(tmp_path: Path) -> None:
    """Two independent loops are two findings; one loop reached twice is one."""
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", ipv4=["10.0.0.1/30"]),
                    sub("a1", "a2", 10),
                    sub("a2", "a1", 10),
                    sub("b1", "b2", 20),
                    sub("b2", "b1", 20),
                ]
            },
        ),
    )
    assert len(only(validate(inventory), "E007")) == 2


def test_e007_leaves_an_acyclic_stack_alone(tmp_path: Path) -> None:
    """A LAG under a bridge under an SVI is three levels and perfectly legal."""
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "interfaces": [
                    eth("Gi0/1", mtu=1500, vlan=access(10)),
                    eth("Gi0/2", mtu=1500, vlan=access(10)),
                    lag("bond0", ["Gi0/1", "Gi0/2"], mtu=1500),
                    bridge("br0", ["bond0"]),
                    sub("Vlan10", "br0", 10, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "E007") == []


# --------------------------------------------------------------------------- #
# E008 -- a member that is not free to be aggregated
# --------------------------------------------------------------------------- #


def test_e008_reports_a_port_claimed_by_two_aggregates(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500),
                    eth("eth1", mtu=1500),
                    lag("bond0", ["eth0", "eth1"], mtu=1500, vlan=access(10)),
                    lag("bond1", ["eth1"], mtu=1500, vlan=access(10)),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "E008")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    for name in ("'pc1:eth1'", "'pc1:bond0'", "'pc1:bond1'"):
        assert name in findings[0].message


def test_e008_reports_an_aggregate_enslaved_to_an_aggregate(tmp_path: Path) -> None:
    """A bridge inside a LAG is the wrong way up; only lag-in-bridge is real."""
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "interfaces": [
                    eth("Gi0/1", mtu=1500, vlan=access(10)),
                    bridge("br0", ["Gi0/1"]),
                    lag("bond0", ["br0"], mtu=1500, vlan=access(10)),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "E008")
    assert len(findings) == 1
    assert "'sw1:br0'" in findings[0].message and "'sw1:bond0'" in findings[0].message


def test_e008_reports_a_sub_interface_on_an_aggregated_port(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, vlan=trunk([10])),
                    lag("bond0", ["eth0"], mtu=1500, vlan=access(10)),
                    sub("eth0.10", "eth0", 10, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "E008")
    assert len(findings) == 1
    assert "'pc1:eth0.10'" in findings[0].message


def test_e008_allows_a_lag_inside_a_bridge(tmp_path: Path) -> None:
    """`br0` with `members: [bond0, eth2]` is how Linux bridges a bond."""
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "interfaces": [
                    eth("Gi0/1", mtu=1500, vlan=access(10)),
                    eth("Gi0/2", mtu=1500, vlan=access(10)),
                    eth("Gi0/3", mtu=1500, vlan=access(10)),
                    lag("bond0", ["Gi0/1", "Gi0/2"], mtu=1500),
                    bridge("br0", ["bond0", "Gi0/3"]),
                    sub("Vlan10", "br0", 10, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "E008") == []


# --------------------------------------------------------------------------- #
# E009 -- a sub-interface VLAN the parent does not carry
# --------------------------------------------------------------------------- #


def test_e009_reports_a_vid_absent_from_the_parent_trunk(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, vlan=trunk([10])),
                    sub("eth0.20", "eth0", 20, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "E009")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "'pc1:eth0.20'" in findings[0].message
    assert "'pc1:eth0'" in findings[0].message
    assert "VLAN 20" in findings[0].message


def test_e009_reports_a_parent_that_is_not_a_trunk_at_all(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, vlan=access(10)),
                    sub("eth0.20", "eth0", 20, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert "1 VLAN (10)" in only(validate(inventory), "E009")[0].message


def test_e009_accepts_a_vid_carried_as_the_native_vlan(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, vlan=trunk([10, 20], native=30)),
                    sub("eth0.30", "eth0", 30, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "E009") == []


def test_e009_resolves_a_bridge_parent_through_its_members(tmp_path: Path) -> None:
    """docs/schema.md §11.1: `Vlan99` hangs off `br0`, whose port trunks VLAN 99."""
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw-access-01",
            {
                "vlans": [{"id": 1}, {"id": 10}, {"id": 20}, {"id": 99}],
                "interfaces": [
                    bridge("br0", ["GigabitEthernet0/1", "GigabitEthernet0/2"]),
                    sub("Vlan99", "br0", 99, ipv4=["10.10.99.2/30"]),
                    eth("GigabitEthernet0/1", mtu=1500, vlan=trunk([1, 10, 20, 99], native=1)),
                    eth("GigabitEthernet0/2", mtu=1500, vlan=access(10)),
                ],
            },
        ),
    )
    assert only(validate(inventory), "E009") == []


def test_e009_accepts_any_vid_under_a_parent_trunking_all(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "interfaces": [
                    eth("Gi0/1", mtu=1500, vlan={"mode": "trunk", "trunk_vlans": "all"}),
                    sub("Vlan777", "Gi0/1", 777, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "E009") == []


def test_e009_accepts_the_schema_lag_sub_interfaces(tmp_path: Path) -> None:
    """docs/schema.md §11.3: `bond0.30`/`bond0.40` on a bond trunking 30 and 40."""
    inventory = load(
        tmp_path,
        net=doc(
            "server",
            "srv-db-01",
            {
                "interfaces": [
                    eth("eno1", mtu=9000, mac="b4:96:91:00:0d:01"),
                    eth("eno2", mtu=9000, mac="b4:96:91:00:0d:02"),
                    lag(
                        "bond0",
                        ["eno1", "eno2"],
                        mtu=9000,
                        mac="b4:96:91:00:0d:01",
                        vlan=trunk([30, 40]),
                    ),
                    sub("bond0.30", "bond0", 30, mtu=9000, ipv4=["10.30.0.9/30"]),
                    sub("bond0.40", "bond0", 40, mtu=9000, ipv4=["10.40.0.9/30"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "E009") == []


# --------------------------------------------------------------------------- #
# E010 / I001 -- the two MAC address bits
# --------------------------------------------------------------------------- #


def test_e010_reports_a_multicast_source_address(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=host_with_mac("01:00:5e:00:00:01"))
    findings = only(validate(inventory), "E010")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "'pc1:eth0'" in findings[0].message
    assert "01:00:5e:00:00:01" in findings[0].message


def test_e010_leaves_a_unicast_address_alone(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=host_with_mac("00:1e:8c:00:00:01"))), "E010") == []


def test_i001_reports_a_locally_administered_address(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=host_with_mac("02:00:00:00:00:01"))
    findings = only(validate(inventory), "I001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO
    assert "02:00:00:00:00:01" in findings[0].message


def test_i001_leaves_a_vendor_assigned_address_alone(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=host_with_mac("00:1e:8c:00:00:01"))), "I001") == []


@pytest.mark.parametrize(
    ("mac", "expected"),
    [
        ("00:1e:8c:00:00:01", []),
        ("01:00:5e:00:00:01", ["E010"]),
        ("02:00:00:00:00:01", ["I001"]),
        ("03:00:00:00:00:01", ["E010", "I001"]),
    ],
)
def test_the_two_low_bits_of_the_first_octet_are_independent(
    tmp_path: Path, mac: str, expected: list[str]
) -> None:
    findings = validate(load(tmp_path, net=host_with_mac(mac)))
    assert [f.rule for f in findings if f.rule in {"E010", "I001"}] == expected


def test_stacking_checks_tolerate_an_adapter_upstream_member(tmp_path: Path) -> None:
    """An adapter's `members` may name the upstream port, which is not an interface.

    `NG-X004` puts the upstream port in the same name space as the downstream
    ones, so it resolves for the schema but has no `Interface` behind it. Every
    stacking rule has to step over it rather than trip on it.
    """
    inventory = load(
        tmp_path,
        net=doc(
            "adapter",
            "dock",
            {
                "upstream": {"name": "usb0", "type": "usb"},
                "interfaces": [
                    eth("en0", enabled=False, mtu=1500),
                    lag("bond0", ["en0", "usb0"], mtu=1500, ipv4=["10.0.0.1/30"]),
                ],
            },
        ),
    )
    assert rules_of(validate(inventory)) == []


def test_e009_survives_a_cyclic_bridge_stack(tmp_path: Path) -> None:
    """Resolving a bridge parent walks members; E007's cycle must not hang it."""
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "interfaces": [
                    bridge("br0", ["br1"]),
                    bridge("br1", ["br0"]),
                    sub("Vlan10", "br0", 10, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    rules = rules_of(validate(inventory))
    assert "E007" in rules and "E009" in rules


def test_e009_accepts_a_bridge_whose_member_trunks_all(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "interfaces": [
                    bridge("br0", ["Gi0/1"]),
                    eth("Gi0/1", mtu=1500, vlan={"mode": "trunk", "trunk_vlans": "all"}),
                    sub("Vlan777", "br0", 777, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "E009") == []


# --------------------------------------------------------------------------- #
# W107 -- addresses on an aggregate member
# --------------------------------------------------------------------------- #


def test_w107_reports_an_address_on_a_lag_member(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"]),
                    lag("bond0", ["eth0"], mtu=1500, vlan=access(10)),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "W107")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "'pc1:eth0'" in findings[0].message and "'pc1:bond0'" in findings[0].message
    assert "10.0.0.1/30" in findings[0].message


def test_w107_leaves_the_aggregate_itself_alone(tmp_path: Path) -> None:
    """The whole point: addresses belong on `bond0`, and there they are."""
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500),
                    lag("bond0", ["eth0"], mtu=1500, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "W107") == []


# --------------------------------------------------------------------------- #
# W108 -- a MAC on a loopback
# --------------------------------------------------------------------------- #


def test_w108_reports_a_mac_on_a_loopback(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    {
                        "name": "lo",
                        "type": "loopback",
                        "mac": "00:1e:8c:00:00:01",
                        "ipv4": ["127.0.0.1/8"],
                    },
                    eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "W108")
    assert len(findings) == 1
    assert "'pc1:lo'" in findings[0].message


def test_w108_leaves_a_loopback_without_a_mac_alone(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {"interfaces": [loopback("lo", ["127.0.0.1/8"]), eth("eth0", mtu=1500)]},
        ),
    )
    assert only(validate(inventory), "W108") == []


# --------------------------------------------------------------------------- #
# W109 -- a device that cannot be cabled
# --------------------------------------------------------------------------- #


def test_w109_reports_a_device_with_only_a_loopback(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc("computer", "pc1", {"interfaces": [loopback("lo", ["127.0.0.1/8"])]}),
    )
    findings = only(validate(inventory), "W109")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "'pc1'" in findings[0].message and "loopback" in findings[0].message


@pytest.mark.parametrize("itype", ["ethernet", "wifi"])
def test_w109_is_satisfied_by_one_cableable_port(tmp_path: Path, itype: str) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    loopback("lo", ["127.0.0.1/8"]),
                    {"name": "p0", "type": itype, "enabled": False},
                ]
            },
        ),
    )
    assert only(validate(inventory), "W109") == []


def test_w109_leaves_adapters_alone(tmp_path: Path) -> None:
    """NG-X003 already restricts an adapter to the three cableable types."""
    inventory = load(
        tmp_path,
        net=doc(
            "adapter",
            "dongle",
            {
                "upstream": {"name": "usb0", "type": "usb"},
                "interfaces": [eth("enx0", mtu=1500, ipv4=["10.0.0.1/30"])],
            },
        ),
    )
    assert only(validate(inventory), "W109") == []


# --------------------------------------------------------------------------- #
# W110 -- the network and broadcast addresses of a prefix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("address", "role"),
    [
        ("10.0.0.0/24", "network address"),
        ("10.0.0.255/24", "broadcast address"),
        ("2001:db8::/64", "subnet-router anycast address"),
    ],
)
def test_w110_reports_a_reserved_address(tmp_path: Path, address: str, role: str) -> None:
    inventory = load(
        tmp_path,
        net=doc("computer", "pc1", {"interfaces": [eth("eth0", mtu=1500, **_family(address))]}),
    )
    findings = only(validate(inventory), "W110")
    assert len(findings) == 1
    assert role in findings[0].message
    assert "'pc1:eth0'" in findings[0].message


@pytest.mark.parametrize(
    "address",
    ["10.0.0.1/24", "10.0.0.0/31", "10.0.0.1/32", "2001:db8::/127", "2001:db8::/128"],
)
def test_w110_exempts_host_and_point_to_point_prefixes(tmp_path: Path, address: str) -> None:
    inventory = load(
        tmp_path,
        net=doc("computer", "pc1", {"interfaces": [eth("eth0", mtu=1500, **_family(address))]}),
    )
    assert only(validate(inventory), "W110") == []


def _reserved_role_via_ipaddress(address: IPv4Address | IPv6Address) -> str | None:
    """``_reserved_role`` as it read before entry 7 of ``docs/follow-ups.md``.

    The rule used to ask an :mod:`ipaddress` network object three questions;
    it now computes the same answer from the address's host bits, because the
    network form materialised a prefix per address for a rule that almost never
    reports anything. This is the original, kept here as the oracle.
    """
    network = address.network
    if network.num_addresses <= 2:
        return None
    if address.ip == network.network_address:
        return "subnet-router anycast address" if network.version == 6 else "network address"
    if network.version == 4 and address.ip == network.broadcast_address:
        return "broadcast address"
    return None


def _sweep_addresses() -> list[IPv4Address | IPv6Address]:
    """Every prefix length of both families, at the host positions that matter.

    For each prefix length: the network address itself, the all-ones host part,
    one either side of each, and an interior address -- so the two boundaries
    the rule is about are hit exactly and missed by one in both directions.
    """
    swept: list[IPv4Address | IPv6Address] = []
    for width, base, model in ((32, 0x0A000000, IPv4Address), (128, 0x20010DB8 << 96, IPv6Address)):
        for prefix_length in range(width + 1):
            host_bits = width - prefix_length
            span = 1 << host_bits
            network = base & ~(span - 1) & ((1 << width) - 1)
            offsets = {0, 1, span - 1, span - 2, span // 2, span // 3}
            for offset in sorted(offset for offset in offsets if 0 <= offset < span):
                swept.append(
                    model(ip=network + offset, prefix_length=prefix_length)  # type: ignore[arg-type]
                )
    return swept


def test_reserved_role_agrees_with_ipaddress() -> None:
    """W110's arithmetic form answers exactly what the network form answered."""
    swept = _sweep_addresses()
    roles = [_reserved_role_via_ipaddress(address) for address in swept]

    # A sweep on which the oracle never fires would pass whatever the rule did.
    assert len(swept) > 500
    assert set(roles) == {
        None,
        "network address",
        "broadcast address",
        "subnet-router anycast address",
    }

    for address, expected in zip(swept, roles, strict=True):
        assert _reserved_role(address) == expected, address


# --------------------------------------------------------------------------- #
# W111 -- overlapping prefixes on one element
# --------------------------------------------------------------------------- #


def test_w111_reports_two_ports_in_one_prefix(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, ipv4=["10.0.0.1/24"]),
                    eth("eth1", mtu=1500, ipv4=["10.0.0.2/24"]),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "W111")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "'pc1:eth0'" in findings[0].message and "'pc1:eth1'" in findings[0].message


def test_w111_reports_a_prefix_containing_another(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, ipv4=["10.0.0.1/24"]),
                    eth("eth1", mtu=1500, ipv4=["10.0.1.1/16"]),
                ]
            },
        ),
    )
    assert len(only(validate(inventory), "W111")) == 1


def test_w111_reports_one_pair_of_prefixes_once(tmp_path: Path) -> None:
    """Two addresses per port in one overlap is one mistake, not four."""
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, ipv4=["10.0.0.1/24", "10.0.0.2/24"]),
                    eth("eth1", mtu=1500, ipv4=["10.0.0.3/24", "10.0.0.4/24"]),
                ]
            },
        ),
    )
    assert len(only(validate(inventory), "W111")) == 1


def test_w111_exempts_two_addresses_on_one_interface(tmp_path: Path) -> None:
    """§10.3 is about two *interfaces*; a secondary address is an alias."""
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/24", "10.0.0.2/24"])]},
        ),
    )
    assert only(validate(inventory), "W111") == []


def test_w111_exempts_loopback_and_link_local_addresses(tmp_path: Path) -> None:
    """Every host has 127.0.0.1/8, and fe80::/64 is per-link by definition."""
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    loopback("lo", ["127.0.0.1/8", "::1/128"]),
                    eth("eth0", mtu=1500, ipv6=["fe80::1/64"]),
                    eth("eth1", mtu=1500, ipv6=["fe80::2/64"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "W111") == []


def test_w111_does_not_cross_address_families(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"]),
                    eth("eth1", mtu=1500, ipv6=["2001:db8::1/127"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "W111") == []


# --------------------------------------------------------------------------- #
# W112 -- a loopback wider than a host route
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("address", ["192.0.2.1/30", "2001:db8::1/127"])
def test_w112_reports_a_routed_loopback_with_a_wide_prefix(tmp_path: Path, address: str) -> None:
    inventory = load(
        tmp_path,
        net=doc("router", "r1", {"interfaces": [loopback("lo0", [address]), eth("eth0")]}),
    )
    findings = only(validate(inventory), "W112")
    assert len(findings) == 1
    assert "'r1:lo0'" in findings[0].message
    assert address in findings[0].message


@pytest.mark.parametrize("address", ["192.0.2.1/32", "2001:db8::1/128"])
def test_w112_accepts_a_host_route(tmp_path: Path, address: str) -> None:
    inventory = load(
        tmp_path,
        net=doc("router", "r1", {"interfaces": [loopback("lo0", [address]), eth("eth0")]}),
    )
    assert only(validate(inventory), "W112") == []


def test_w112_exempts_the_host_scoped_loopback_prefixes(tmp_path: Path) -> None:
    """RFC 1122 §3.2.1.3 reserves 127.0.0.0/8; every OS configures 127.0.0.1/8."""
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {"interfaces": [loopback("lo", ["127.0.0.1/8", "::1/128"]), eth("eth0")]},
        ),
    )
    assert only(validate(inventory), "W112") == []


def test_w112_says_nothing_about_a_physical_port(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc("computer", "pc1", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/24"])]}),
    )
    assert only(validate(inventory), "W112") == []


# --------------------------------------------------------------------------- #
# W113 -- a VLAN the device does not declare
# --------------------------------------------------------------------------- #


def test_w113_reports_a_port_in_an_undeclared_vlan(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "vlans": [{"id": 10, "name": "users"}],
                "interfaces": [eth("Gi0/1", mtu=1500, vlan=access(20))],
            },
        ),
    )
    findings = only(validate(inventory), "W113")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "'sw1:Gi0/1'" in findings[0].message and "'sw1'" in findings[0].message
    assert "20" in findings[0].message


def test_w113_lists_every_missing_vlan_of_a_trunk(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "vlans": [{"id": 10}],
                "interfaces": [eth("Gi0/1", mtu=1500, vlan=trunk([10, 20, 30]))],
            },
        ),
    )
    assert "20, 30" in only(validate(inventory), "W113")[0].message


def test_w113_says_nothing_when_no_vlan_database_is_declared(tmp_path: Path) -> None:
    """§6.4 makes `vlans` optional: an absent database is not an empty one."""
    inventory = load(
        tmp_path,
        net=doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=access(20))]}),
    )
    assert only(validate(inventory), "W113") == []


def test_w113_exempts_the_802_1q_default_vlan(tmp_path: Path) -> None:
    """VLAN 1 exists on every bridge, and is what `access_vlan` defaults to."""
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "vlans": [{"id": 10}],
                "interfaces": [
                    eth("Gi0/1", mtu=1500, vlan={"mode": "access"}),
                    eth("Gi0/2", mtu=1500, vlan=trunk([10], native=1)),
                ],
            },
        ),
    )
    assert only(validate(inventory), "W113") == []


def test_w113_exempts_a_port_trunking_all(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {
                "vlans": [{"id": 10}],
                "interfaces": [
                    eth("Gi0/1", mtu=1500, vlan={"mode": "trunk", "trunk_vlans": "all"})
                ],
            },
        ),
    )
    assert only(validate(inventory), "W113") == []


# --------------------------------------------------------------------------- #
# W114 -- a native VLAN outside the trunk set
# --------------------------------------------------------------------------- #


def test_w114_reports_a_native_vlan_missing_from_the_trunk(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {"interfaces": [eth("Gi0/1", mtu=1500, vlan=trunk([10], native=20))]},
        ),
    )
    findings = only(validate(inventory), "W114")
    assert len(findings) == 1
    assert "'sw1:Gi0/1'" in findings[0].message
    assert "20" in findings[0].message


def test_w114_accepts_a_native_vlan_that_is_listed(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw1",
            {"interfaces": [eth("Gi0/1", mtu=1500, vlan=trunk([1, 10], native=1))]},
        ),
    )
    assert only(validate(inventory), "W114") == []


def test_w114_accepts_a_native_vlan_inside_trunk_all(tmp_path: Path) -> None:
    """docs/schema.md §11.3: the MLAG peer link trunks `all` with `native_vlan: 1`."""
    inventory = load(
        tmp_path,
        net=doc(
            "switch",
            "sw-tor-a",
            {
                "interfaces": [
                    eth(
                        "Ethernet50",
                        mtu=9214,
                        vlan={"mode": "trunk", "trunk_vlans": "all", "native_vlan": 1},
                    )
                ]
            },
        ),
    )
    assert only(validate(inventory), "W114") == []


# --------------------------------------------------------------------------- #
# W115 -- every VLAN trunked to a host
# --------------------------------------------------------------------------- #


def trunk_all_to(kind: str) -> str:
    return join(
        doc(
            "switch",
            "sw1",
            {"interfaces": [eth("Gi0/1", mtu=1500, vlan={"mode": "trunk", "trunk_vlans": "all"})]},
        ),
        doc(kind, "far", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"])]}),
        cable("c1", "sw1:Gi0/1", "far:eth0"),
    )


@pytest.mark.parametrize("kind", ["computer", "server"])
def test_w115_reports_a_host_on_the_far_end(tmp_path: Path, kind: str) -> None:
    findings = only(validate(load(tmp_path, net=trunk_all_to(kind))), "W115")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    for name in ("'sw1:Gi0/1'", "'far:eth0'", "'c1'"):
        assert name in findings[0].message


def test_w115_says_nothing_between_two_switches(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=trunk_all_to("switch"))), "W115") == []


def test_w115_says_nothing_about_a_bounded_trunk(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=trunk([10, 20]))]}),
            doc(
                "computer",
                "pc1",
                {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"])]},
            ),
            cable("c1", "sw1:Gi0/1", "pc1:eth0"),
        ),
    )
    assert only(validate(inventory), "W115") == []


def test_w115_resolves_through_the_lag_master(tmp_path: Path) -> None:
    """§10.6: the member is cabled, the aggregate carries the VLAN set."""
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "switch",
                "sw1",
                {
                    "interfaces": [
                        eth("Gi0/1", mtu=1500),
                        lag(
                            "Po1",
                            ["Gi0/1"],
                            mtu=1500,
                            vlan={"mode": "trunk", "trunk_vlans": "all"},
                        ),
                    ]
                },
            ),
            doc(
                "computer",
                "pc1",
                {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"])]},
            ),
            cable("c1", "sw1:Gi0/1", "pc1:eth0"),
        ),
    )
    findings = only(validate(inventory), "W115")
    assert len(findings) == 1
    assert "aggregated by 'Po1'" in findings[0].message


# --------------------------------------------------------------------------- #
# W116 -- a LAG member contradicting its aggregate
# --------------------------------------------------------------------------- #


def test_w116_reports_a_member_with_a_different_vlan_block(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, vlan=access(20)),
                    lag("bond0", ["eth0"], mtu=1500, vlan=access(10), ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    findings = only(validate(inventory), "W116")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "'pc1:eth0'" in findings[0].message and "'pc1:bond0'" in findings[0].message
    assert "access VLAN 20" in findings[0].message and "access VLAN 10" in findings[0].message


def test_w116_accepts_a_member_that_repeats_the_aggregate(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, vlan=access(10)),
                    lag("bond0", ["eth0"], mtu=1500, vlan=access(10), ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert only(validate(inventory), "W116") == []


def test_w116_accepts_a_silent_member(tmp_path: Path) -> None:
    """docs/schema.md §11.3: `eno1`/`eno2` carry no `vlan` block of their own."""
    inventory = load(
        tmp_path,
        net=doc(
            "server",
            "srv-db-01",
            {
                "interfaces": [
                    eth("eno1", mtu=9000),
                    eth("eno2", mtu=9000),
                    lag(
                        "bond0",
                        ["eno1", "eno2"],
                        mtu=9000,
                        vlan=trunk([30, 40]),
                        ipv4=["10.30.0.11/30"],
                    ),
                ]
            },
        ),
    )
    assert only(validate(inventory), "W116") == []


def test_w116_reports_a_member_whose_aggregate_says_nothing(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=doc(
            "computer",
            "pc1",
            {
                "interfaces": [
                    eth("eth0", mtu=1500, vlan=access(20)),
                    lag("bond0", ["eth0"], mtu=1500, ipv4=["10.0.0.1/30"]),
                ]
            },
        ),
    )
    assert "no 'vlan' block" in only(validate(inventory), "W116")[0].message


# --------------------------------------------------------------------------- #
# E011 -- medium versus endpoint type
# --------------------------------------------------------------------------- #


def test_e011_reports_a_copper_cable_into_a_radio(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [wifi("wlan0", ipv4=["10.0.0.1/30"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["10.0.0.2/30"])]}),
            cable("c1", "pc1:wlan0", "pc2:eth0"),
        ),
    )
    findings = only(validate(inventory), "E011")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "'medium: copper'" in findings[0].message
    assert "'pc1:wlan0'" in findings[0].message


def test_e011_reports_a_wireless_link_with_a_wired_end(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [wifi("wlan0", ipv4=["10.0.0.1/30"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["10.0.0.2/30"])]}),
            link("c1", "pc1:wlan0", "pc2:eth0", medium="wireless"),
        ),
    )
    findings = only(validate(inventory), "E011")
    assert len(findings) == 1
    assert "'medium: wireless'" in findings[0].message
    assert "'pc2:eth0' is not 'type: wifi'" in findings[0].message


def test_e011_accepts_a_wireless_link_between_two_radios(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [wifi("wlan0", ipv4=["10.0.0.1/30"])]}),
            doc("computer", "pc2", {"interfaces": [wifi("wlan0", ipv4=["10.0.0.2/30"])]}),
            link("c1", "pc1:wlan0", "pc2:wlan0", medium="wireless"),
        ),
    )
    assert only(validate(inventory), "E011") == []


def test_e011_treats_an_adapter_upstream_port_as_wired(tmp_path: Path) -> None:
    """§8.1: the upstream port is a host bus, so a copper cable belongs on it."""
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/30"])]}),
            dongle("dock"),
            cable("c1", "pc1:eth0", "dock:usb0"),
        ),
    )
    assert only(validate(inventory), "E011") == []


# --------------------------------------------------------------------------- #
# E012 -- an endpoint that cannot take a plug
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("port", ["lo", "br0", "Vlan10"])
def test_e012_reports_a_software_interface_as_an_endpoint(tmp_path: Path, port: str) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "switch",
                "sw1",
                {
                    "interfaces": [
                        loopback("lo", ["192.0.2.1/32"]),
                        eth("Gi0/1", enabled=False, mtu=1500),
                        bridge("br0", ["Gi0/1"]),
                        sub("Vlan10", "br0", 10, ipv4=["10.0.0.1/30"]),
                    ]
                },
            ),
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.2/30"])]}),
            cable("c1", f"sw1:{port}", "pc1:eth0"),
        ),
    )
    findings = only(validate(inventory), "E012")
    assert len(findings) == 1
    assert f"'sw1:{port}'" in findings[0].message
    # The ports it could have meant are spelled out.
    assert "'Gi0/1'" in findings[0].message


def test_e012_accepts_the_three_cableable_types(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=clean_pair())), "E012") == []


# --------------------------------------------------------------------------- #
# E013 / W123 -- how an adapter says which host it is plugged into
# --------------------------------------------------------------------------- #


def test_e013_reports_an_upstream_port_that_is_attached_and_cabled(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "laptop", {"interfaces": [eth("eth0", enabled=False)]}),
            dongle("dock", attached_to="laptop"),
            cable("c1", "dock:usb0", "laptop:eth0"),
        ),
    )
    findings = only(validate(inventory), "E013")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "'dock:usb0'" in findings[0].message and "'laptop'" in findings[0].message


def test_e013_accepts_a_cabled_upstream_port_without_an_attachment(tmp_path: Path) -> None:
    """A media converter declares its host with a cable *instead of* attached_to."""
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "laptop", {"interfaces": [eth("eth0", enabled=False)]}),
            dongle("dock"),
            cable("c1", "dock:usb0", "laptop:eth0"),
        ),
    )
    assert only(validate(inventory), "E013") == []


def test_w123_reports_a_cabled_adapter_with_no_host(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            dongle("dock", downstream=eth("en0", mtu=1500, ipv4=["10.0.0.1/30"])),
            doc("computer", "pc1", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.2/30"])]}),
            cable("c1", "dock:en0", "pc1:eth0"),
        ),
    )
    findings = only(validate(inventory), "W123")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "'en0'" in findings[0].message


def test_w123_is_silent_when_the_adapter_is_attached(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "laptop", {"interfaces": [eth("eth0", enabled=False)]}),
            dongle("dock", attached_to="laptop", downstream=eth("en0", ipv4=["10.0.0.1/30"])),
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.2/30"])]}),
            cable("c1", "dock:en0", "pc1:eth0"),
        ),
    )
    assert only(validate(inventory), "W123") == []


def test_w123_is_silent_when_the_upstream_port_is_cabled_instead(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            dongle("dock", downstream=eth("en0", ipv4=["10.0.0.1/30"])),
            doc(
                "computer",
                "pc1",
                {"interfaces": [eth("eth0", ipv4=["10.0.0.2/30"]), eth("eth1", enabled=False)]},
            ),
            cable("c1", "dock:en0", "pc1:eth0"),
            cable("c2", "dock:usb0", "pc1:eth1"),
        ),
    )
    assert only(validate(inventory), "W123") == []


def test_w123_is_silent_for_an_adapter_nothing_is_patched_into(tmp_path: Path) -> None:
    """§8.2: a free-standing adapter is a spare in a drawer, not a mistake."""
    inventory = load(tmp_path, net=dongle("dock"))
    assert only(validate(inventory), "W123") == []


# --------------------------------------------------------------------------- #
# E014 / E015 / W124 -- where an attachment points
# --------------------------------------------------------------------------- #


def test_e014_reports_two_adapters_attached_to_each_other(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(dongle("dock", attached_to="hub-usb"), dongle("hub-usb", attached_to="dock")),
    )
    findings = only(validate(inventory), "E014")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].message.count("->") == 2
    assert set(findings[0].elements) == {"dock", "hub-usb"}


def test_e014_accepts_a_chain_that_reaches_a_host(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "laptop", {"interfaces": [eth("eth0", enabled=False)]}),
            dongle("dock", attached_to="laptop"),
            dongle("dongle", attached_to="dock"),
        ),
    )
    assert only(validate(inventory), "E014") == []


def test_e015_reports_an_attachment_that_names_nothing(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=dongle("dock", attached_to="ghost"))
    findings = only(validate(inventory), "E015")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "'ghost'" in findings[0].message
    assert findings[0].field_path == ("spec", "upstream", "attached_to")


def test_e015_reports_an_ambiguous_attachment(tmp_path: Path) -> None:
    """§2.2: a short name matching two elements resolves to neither."""
    inventory = load(
        tmp_path,
        a__host=doc("computer", "laptop", {"interfaces": [eth("eth0", enabled=False)]}),
        b__host=doc("computer", "laptop", {"interfaces": [eth("eth0", enabled=False)]}),
        c__dock=dongle("dock", attached_to="laptop"),
    )
    findings = only(validate(inventory), "E015")
    assert len(findings) == 1
    assert "is ambiguous here" in findings[0].message
    assert "'a/laptop'" in findings[0].message and "'b/laptop'" in findings[0].message


def test_e015_reports_an_attachment_to_something_that_owns_no_interfaces(
    tmp_path: Path,
) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/30"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", ipv4=["10.0.0.2/30"])]}),
            cable("c1", "pc1:eth0", "pc2:eth0"),
            dongle("dock", attached_to="c1"),
        ),
    )
    findings = only(validate(inventory), "E015")
    assert len(findings) == 1
    assert "which is a cable" in findings[0].message


def test_e015_is_silent_when_the_attachment_resolves(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "laptop", {"interfaces": [eth("eth0", enabled=False)]}),
            dongle("dock", attached_to="laptop"),
        ),
    )
    assert only(validate(inventory), "E015") == []


@pytest.mark.parametrize("kind", ["switch", "hub"])
def test_w124_reports_an_adapter_hanging_off_network_gear(tmp_path: Path, kind: str) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("switch" if kind == "switch" else "hub", "gear", {"interfaces": [eth("port1")]}),
            dongle("dock", attached_to="gear"),
        ),
    )
    findings = only(validate(inventory), "W124")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert f"which is a {kind}" in findings[0].message
    assert findings[0].elements == ("dock", "gear")


@pytest.mark.parametrize("kind", ["computer", "server", "router"])
def test_w124_accepts_a_host(tmp_path: Path, kind: str) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc(kind, "host", {"interfaces": [eth("eth0", enabled=False)]}),
            dongle("dock", attached_to="host"),
        ),
    )
    assert only(validate(inventory), "W124") == []


# --------------------------------------------------------------------------- #
# W117 -- a cable that goes nowhere new
# --------------------------------------------------------------------------- #


def test_w117_reports_a_cable_between_two_ports_of_one_element(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "switch",
                "sw1",
                {
                    "interfaces": [
                        eth("Gi0/1", mtu=1500, vlan=access(10)),
                        eth("Gi0/2", mtu=1500, vlan=access(10)),
                    ]
                },
            ),
            cable("c1", "sw1:Gi0/1", "sw1:Gi0/2"),
        ),
    )
    findings = only(validate(inventory), "W117")
    assert len(findings) == 1
    assert "'Gi0/1', 'Gi0/2'" in findings[0].message


def test_w117_leaves_the_same_port_twice_to_e002(tmp_path: Path) -> None:
    """One mistake, one finding: E002 already says a cable joins two interfaces."""
    inventory = load(
        tmp_path,
        net=join(
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=access(10))]}),
            cable("c1", "sw1:Gi0/1", "sw1:Gi0/1"),
        ),
    )
    rules = rules_of(validate(inventory))
    assert "E002" in rules and "W117" not in rules


def test_w117_accepts_a_cable_between_two_elements(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=clean_pair())), "W117") == []


# --------------------------------------------------------------------------- #
# W118 -- speed
# --------------------------------------------------------------------------- #


def test_w118_reports_a_bus_rate_the_cable_contradicts(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            dongle("dock", speed="5Gbps"),
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/30"])]}),
            link("c1", "dock:usb0", "pc1:eth0", speed="1Gbps"),
        ),
    )
    findings = only(validate(inventory), "W118")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "1Gbps" in findings[0].message and "5Gbps" in findings[0].message


def test_w118_accepts_a_cable_that_agrees(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            dongle("dock", speed="5Gbps"),
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/30"])]}),
            link("c1", "dock:usb0", "pc1:eth0", speed="5Gbps"),
        ),
    )
    assert only(validate(inventory), "W118") == []


def test_w118_says_nothing_when_either_side_is_silent(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            dongle("dock", speed="5Gbps"),
            doc("computer", "pc1", {"interfaces": [eth("eth0", ipv4=["10.0.0.1/30"])]}),
            cable("c1", "dock:usb0", "pc1:eth0"),
        ),
    )
    assert only(validate(inventory), "W118") == []


# --------------------------------------------------------------------------- #
# W119 -- cabling a bundle instead of its lanes
# --------------------------------------------------------------------------- #


def bonded_host(name: str, address: str, *, cabled: str) -> str:
    """A host whose two ports are bundled into ``bond0``.

    ``cabled`` names the port the fixture's cable lands on; the others are
    marked spare so that ``I002`` has nothing to add.
    """
    return doc(
        "computer",
        name,
        {
            "interfaces": [
                eth("eth0", enabled=cabled == "eth0", mtu=1500),
                eth("eth1", enabled=False, mtu=1500),
                lag("bond0", ["eth0", "eth1"], mtu=1500, ipv4=[address]),
            ]
        },
    )


def test_w119_reports_a_cable_on_the_aggregate(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            bonded_host("pc1", "10.0.0.1/30", cabled="bond0"),
            doc("computer", "pc2", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.2/30"])]}),
            cable("c1", "pc1:bond0", "pc2:eth0"),
        ),
    )
    findings = only(validate(inventory), "W119")
    assert len(findings) == 1
    assert "'pc1:bond0'" in findings[0].message
    assert "'eth0', 'eth1'" in findings[0].message


def test_w119_accepts_a_cable_on_a_member(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            bonded_host("pc1", "10.0.0.1/30", cabled="eth0"),
            doc("computer", "pc2", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.2/30"])]}),
            cable("c1", "pc1:eth0", "pc2:eth0"),
        ),
    )
    assert only(validate(inventory), "W119") == []


# --------------------------------------------------------------------------- #
# W120 -- half duplex
# --------------------------------------------------------------------------- #


def test_w120_reports_half_duplex_between_two_switched_ports(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("computer", "pc1", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"])]}),
            doc("computer", "pc2", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.2/30"])]}),
            link("c1", "pc1:eth0", "pc2:eth0", duplex="half"),
        ),
    )
    findings = only(validate(inventory), "W120")
    assert len(findings) == 1
    assert "'duplex: half'" in findings[0].message


def test_w120_accepts_half_duplex_on_a_hub_link(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc("hub", "hub1", {"interfaces": [eth("port1")]}),
            doc("computer", "pc1", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"])]}),
            link("c1", "hub1:port1", "pc1:eth0", duplex="half"),
        ),
    )
    assert only(validate(inventory), "W120") == []


def test_w120_accepts_the_default_full_duplex(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=clean_pair())), "W120") == []


# --------------------------------------------------------------------------- #
# W121 -- a topology in pieces
# --------------------------------------------------------------------------- #


def wired_pair(prefix: str, first: str, second: str) -> str:
    """Two hosts cabled to each other inside ``prefix`` (a /30)."""
    network = prefix.rsplit(".", 1)[0]
    return join(
        doc(
            "computer",
            first,
            {"interfaces": [eth("eth0", mtu=1500, ipv4=[f"{network}.1/30"])]},
        ),
        doc(
            "computer",
            second,
            {"interfaces": [eth("eth0", mtu=1500, ipv4=[f"{network}.2/30"])]},
        ),
        cable(f"cbl-{first}-{second}", f"{first}:eth0", f"{second}:eth0"),
    )


def test_w121_reports_two_islands_once(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        a=wired_pair("10.0.0.0", "pc-a", "pc-b"),
        b=wired_pair("10.0.1.0", "pc-c", "pc-d"),
    )
    findings = only(validate(inventory), "W121")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "2 islands" in findings[0].message
    # Each island is named by its alphabetically smallest member, with its size.
    assert "'pc-a' (2 elements)" in findings[0].message
    assert "'pc-c' (2 elements)" in findings[0].message
    assert findings[0].elements == ("pc-a", "pc-c")


def test_w121_leaves_a_lone_device_to_w103(tmp_path: Path) -> None:
    inventory = load(tmp_path, a=wired_pair("10.0.0.0", "pc-a", "pc-b"), b=orphan("spare"))
    rules = rules_of(validate(inventory))
    assert "W103" in rules and "W121" not in rules


def test_w121_counts_an_attachment_as_a_link(tmp_path: Path) -> None:
    """§8.2: `attached_to` is an edge, so a dongle's host is not a second island."""
    inventory = load(
        tmp_path,
        a=wired_pair("10.0.0.0", "pc-a", "pc-b"),
        b=join(
            doc("computer", "laptop", {"interfaces": [eth("eth0", enabled=False)]}),
            dongle("dock", attached_to="laptop", downstream=eth("en0", enabled=False)),
        ),
    )
    findings = only(validate(inventory), "W121")
    assert len(findings) == 1
    assert "'dock' (2 elements)" in findings[0].message


def test_w121_is_silent_for_one_connected_topology(tmp_path: Path) -> None:
    assert only(validate(load(tmp_path, net=clean_pair())), "W121") == []


# --------------------------------------------------------------------------- #
# W122 -- one hub, one broadcast domain
# --------------------------------------------------------------------------- #


def on_a_hub(*peers: tuple[str, str], hubs: int = 1) -> str:
    """``peers`` hosts cabled into ``hubs`` chained repeaters."""
    documents = [
        doc("hub", f"hub{n}", {"interfaces": [eth(f"port{i}") for i in range(len(peers) + 2)]})
        for n in range(hubs)
    ]
    documents += [
        doc("computer", name, {"interfaces": [eth("eth0", mtu=1500, **_family(address))]})
        for name, address in peers
    ]
    documents += [
        cable(f"cbl-{name}", f"hub{index % hubs}:port{index}", f"{name}:eth0")
        for index, (name, _) in enumerate(peers)
    ]
    documents += [
        cable(f"cbl-hub{n}", f"hub{n}:port{len(peers) + 1}", f"hub{n + 1}:port{len(peers) + 1}")
        for n in range(hubs - 1)
    ]
    return join(*documents)


def test_w122_reports_two_hosts_on_one_hub_in_different_subnets(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=on_a_hub(("pc-a", "10.0.0.1/30"), ("pc-b", "10.0.1.1/30")))
    findings = only(validate(inventory), "W122")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "'pc-a:eth0' in 10.0.0.0/30" in findings[0].message
    assert "'pc-b:eth0' in 10.0.1.0/30" in findings[0].message
    assert set(findings[0].elements) == {"hub0", "pc-a", "pc-b"}


def test_w122_accepts_two_hosts_in_one_subnet(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=on_a_hub(("pc-a", "10.0.0.1/24"), ("pc-b", "10.0.0.2/24")))
    assert only(validate(inventory), "W122") == []


def test_w122_checks_the_two_address_families_apart(tmp_path: Path) -> None:
    """A v4-only host beside a v6-only one is a dual-stack rollout, not a clash."""
    inventory = load(tmp_path, net=on_a_hub(("pc-a", "10.0.0.1/30"), ("pc-b", "2001:db8::1/126")))
    assert only(validate(inventory), "W122") == []


def test_w122_ignores_hub_peers_that_hold_no_address(tmp_path: Path) -> None:
    """A bridge port and an adapter's upstream bus say nothing about subnets."""
    inventory = load(
        tmp_path,
        net=join(
            doc("hub", "hub0", {"interfaces": [eth(f"port{n}") for n in range(4)]}),
            doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=1500, vlan=access(10))]}),
            dongle("dock"),
            doc("computer", "pc-a", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"])]}),
            cable("c1", "hub0:port0", "sw1:Gi0/1"),
            cable("c2", "hub0:port1", "dock:usb0"),
            cable("c3", "hub0:port2", "pc-a:eth0"),
        ),
    )
    assert only(validate(inventory), "W122") == []


def test_w122_treats_chained_hubs_as_one_collision_domain(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=on_a_hub(("pc-a", "10.0.0.1/30"), ("pc-b", "10.0.1.1/30"), hubs=2),
    )
    findings = only(validate(inventory), "W122")
    assert len(findings) == 1
    assert set(findings[0].elements) == {"hub0", "hub1", "pc-a", "pc-b"}


# --------------------------------------------------------------------------- #
# I002 -- an enabled port with nothing in it
# --------------------------------------------------------------------------- #


def test_i002_reports_an_enabled_port_with_no_cable(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "computer",
                "pc1",
                {
                    "interfaces": [
                        eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"]),
                        eth("eth1", mtu=1500, ipv4=["10.0.1.1/30"]),
                    ]
                },
            ),
            doc("computer", "pc2", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.2/30"])]}),
            cable("c1", "pc1:eth0", "pc2:eth0"),
        ),
    )
    findings = only(validate(inventory), "I002")
    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO
    assert "'pc1:eth1'" in findings[0].message
    assert "enabled: false" in findings[0].message


def test_i002_is_silent_for_a_port_marked_spare(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "computer",
                "pc1",
                {
                    "interfaces": [
                        eth("eth0", mtu=1500, ipv4=["10.0.0.1/30"]),
                        eth("eth1", enabled=False, mtu=1500),
                    ]
                },
            ),
            doc("computer", "pc2", {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.2/30"])]}),
            cable("c1", "pc1:eth0", "pc2:eth0"),
        ),
    )
    assert only(validate(inventory), "I002") == []


def test_i002_says_nothing_about_types_that_cannot_be_cabled(tmp_path: Path) -> None:
    """Loopbacks, SVIs and bridges have no socket; a lag is cabled by its members."""
    inventory = load(
        tmp_path,
        net=join(
            doc(
                "switch",
                "sw1",
                {
                    "interfaces": [
                        loopback("lo", ["192.0.2.1/32"]),
                        eth("Gi0/1", mtu=1500, vlan=access(10)),
                        bridge("br0", ["Gi0/1"]),
                        sub("Vlan10", "br0", 10, ipv4=["10.0.0.1/30"]),
                        eth("Gi0/2", enabled=False, mtu=1500),
                        lag("po1", ["Gi0/2"], mtu=1500),
                    ]
                },
            ),
            doc("computer", "pc1", {"interfaces": [eth("eth0", mtu=1500, vlan=access(10))]}),
            cable("c1", "sw1:Gi0/1", "pc1:eth0"),
        ),
    )
    assert only(validate(inventory), "I002") == []


# --------------------------------------------------------------------------- #
# Suppression: annotations
# --------------------------------------------------------------------------- #


def orphan(name: str = "spare", **metadata: Any) -> str:
    """A device with no cable: W103 and nothing else.

    The address is in a point-to-point prefix, which W105 exempts — a lone
    device in a /24 would otherwise also be a subnet with a single member — and
    the port is marked spare, which is what keeps I002 quiet about it.
    """
    return doc(
        "computer",
        name,
        {"interfaces": [eth("eth0", enabled=False, ipv4=["10.0.0.1/30"])]},
        **metadata,
    )


@pytest.mark.parametrize(
    "value",
    ["W103", "NG-C016", "w103", "*", "all", "any", "E001, W103", "E001 W103", "E001;W103"],
)
def test_annotation_suppresses_a_rule(tmp_path: Path, value: str) -> None:
    inventory = load(tmp_path, net=orphan(annotations={"netgraph/ignore": value}))
    assert only(validate(inventory), "W103") == []


def test_the_reserved_prefix_spelling_is_accepted_too(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=orphan(annotations={"netgraph.dev/ignore": "W103"}))
    assert only(validate(inventory), "W103") == []


def test_an_annotation_only_silences_the_rules_it_names(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=orphan(annotations={"netgraph/ignore": "E001"}))
    assert rules_of(validate(inventory)) == ["W103"]


def test_an_unknown_rule_id_in_an_annotation_silences_nothing(tmp_path: Path) -> None:
    """Failing open keeps a typo from hiding the finding it was aimed at."""
    inventory = load(tmp_path, net=orphan(annotations={"netgraph/ignore": "W1O3"}))
    assert rules_of(validate(inventory)) == ["W103"]


def test_either_element_of_a_finding_can_suppress_it(tmp_path: Path) -> None:
    """A finding names every element it involves, so annotating one end is enough."""
    net = join(
        doc("switch", "sw1", {"interfaces": [eth("Gi0/1", mtu=9000, vlan=access(10))]}),
        doc(
            "computer",
            "pc1",
            {"interfaces": [eth("eth0", mtu=1500, ipv4=["10.0.0.1/24"])]},
            annotations={"netgraph/ignore": "W102"},
        ),
        cable("c1", "sw1:Gi0/1", "pc1:eth0"),
    )
    assert only(validate(load(tmp_path, net=net)), "W102") == []


def test_annotations_are_not_labels(tmp_path: Path) -> None:
    """Labels drive ``--select``; an ignore list there must not silence anything."""
    inventory = load(tmp_path, net=orphan(labels={"netgraph-ignore": "W103"}))
    assert rules_of(validate(inventory)) == ["W103"]


# --------------------------------------------------------------------------- #
# Suppression: netgraph.toml
# --------------------------------------------------------------------------- #


def test_config_ignore_silences_a_rule(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=orphan())
    config = ValidationConfig(ignore=frozenset({"W103"}))
    assert validate(inventory, config) == []


def test_config_wildcard_disables_validation(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=orphan())
    assert validate(inventory, ValidationConfig(ignore=frozenset({WILDCARD}))) == []


def test_config_severity_override(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=orphan())
    config = ValidationConfig(severity={"W103": Severity.INFO})
    assert validate(inventory, config)[0].severity is Severity.INFO


def test_strict_promotes_warnings_but_not_info(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=orphan())
    assert validate(inventory, ValidationConfig(strict=True))[0].severity is Severity.ERROR
    demoted = ValidationConfig(strict=True, severity={"W103": Severity.INFO})
    assert validate(inventory, demoted)[0].severity is Severity.INFO


def test_config_file_round_trip(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text(
        """
        [validate]
        strict = true
        ignore = ["W101", "NG-C016"]

        [validate.severity]
        E004 = "warning"
        """,
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.path == tmp_path / CONFIG_FILE_NAME
    assert not config.is_default
    # The NG-* alias resolves to the short id it shares a rule with.
    assert config.validation.ignore == frozenset({"W101", "W103"})
    assert config.validation.severity == {"E004": Severity.WARNING}
    assert config.validation.strict is True


def test_a_missing_config_file_is_not_an_error(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config == Config()
    assert config.is_default
    assert config.validation.ignore == frozenset()


def test_a_named_config_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_config(tmp_path / "absent.toml")


def test_a_config_file_may_be_named_directly(tmp_path: Path) -> None:
    path = tmp_path / "custom.toml"
    path.write_text("[validate]\nstrict = true\n", encoding="utf-8")
    assert load_config(path).validation.strict is True


def test_an_unreadable_config_file_is_reported(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).mkdir()
    with pytest.raises(ConfigurationError, match="cannot be read"):
        load_config(tmp_path)


def test_broken_toml_is_reported_as_a_configuration_error(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("[validate\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid TOML"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"validate": {"ingore": []}}, "unknown key"),
        ({"validate": {"ignore": "E999"}}, "not a known rule id"),
        ({"validate": {"ignore": [1]}}, "must be a rule id string"),
        ({"validate": {"ignore": 7}}, "must be a rule id or a list"),
        ({"validate": {"strict": "yes"}}, "must be true or false"),
        ({"validate": {"severity": []}}, "must be a table"),
        ({"validate": {"severity": {"E001": "fatal"}}}, "is not a severity"),
        ({"validate": {"severity": {"E001": 3}}}, "must be one of"),
        ({"validate": {"severity": {"*": "info"}}}, "cannot be re-graded"),
        ({"validate": "off"}, "must be a table"),
    ],
)
def test_unusable_configuration_is_rejected(data: dict[str, Any], expected: str) -> None:
    with pytest.raises(ConfigurationError, match=expected):
        parse_config(data)


def test_unknown_top_level_tables_are_left_alone(tmp_path: Path) -> None:
    """A file shared with a newer netgraph must not break this one."""
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "[render]\nengine = 'dot'\n[validate]\nstrict = true\n", encoding="utf-8"
    )
    assert load_config(tmp_path).validation.strict is True


def test_command_line_overrides_layer_on_top_of_the_file(tmp_path: Path) -> None:
    base = ValidationConfig(ignore=frozenset({"W101"}))
    merged = base.with_overrides(strict=True, ignore=["NG-C016"])
    assert merged.ignore == frozenset({"W101", "W103"})
    assert merged.strict is True
    # The original is untouched.
    assert base.ignore == frozenset({"W101"}) and base.strict is False


def test_an_unknown_rule_on_the_command_line_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="--disable"):
        ValidationConfig().with_overrides(ignore=["nonsense"])


# --------------------------------------------------------------------------- #
# Findings and the rule catalogue
# --------------------------------------------------------------------------- #


def test_findings_are_ordered_by_location_then_severity(tmp_path: Path) -> None:
    inventory = load(
        tmp_path,
        b__second=orphan("second"),
        a__first=join(
            doc("computer", "first", {"interfaces": [eth("eth0")]}),
            cable("c1", "first:eth0", "ghost:eth0"),
        ),
    )
    findings = validate(inventory)
    assert [finding.file for finding in findings] == [
        "a/first.yaml",
        "a/first.yaml",
        "b/second.yaml",
    ]
    # Sorted by file, then by document within the file: the host declaration
    # (document 0) precedes the cable (document 1), and the other folder last.
    assert rules_of(findings) == ["W101", "E001", "W103"]


def test_finding_rendering_and_helpers(tmp_path: Path) -> None:
    findings = validate(load(tmp_path, net=orphan()))
    finding = findings[0]
    assert finding.element == "spare"
    assert finding.file == "net.yaml"
    assert finding.location.startswith("net.yaml#0")
    assert str(finding) == f"{finding.location}: warning: W103: {finding.message}"
    assert not has_errors(findings)
    assert errors_only(findings) == []
    assert summarise(findings) == {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 0}


def test_a_finding_without_a_source_degrades_gracefully() -> None:
    finding = Finding(rule="E001", severity=Severity.ERROR, message="detached")
    assert finding.file is None
    assert finding.location == "-"
    assert finding.element is None


def test_validate_defaults_to_the_stock_configuration(tmp_path: Path) -> None:
    inventory = load(tmp_path, net=orphan())
    assert validate(inventory) == validate(inventory, ValidationConfig())


def test_every_rule_has_a_check_and_a_unique_id() -> None:
    from netgraph.validate import _CHECKS

    assert [rule_id for rule_id, _ in _CHECKS] == list(RULE_IDS)
    assert len(set(RULE_IDS)) == len(RULES)


def test_rule_ids_and_aliases_are_unique() -> None:
    names = [name for rule in RULES for name in rule.names]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize(("token", "expected"), [("e001", "E001"), ("NG-C005", "E002")])
def test_rule_ids_resolve_case_insensitively(token: str, expected: str) -> None:
    assert resolve_rule_id(token) == expected
    assert known_rule(token)
    assert rule_for(token).id == expected


def test_resolving_an_unknown_rule_id() -> None:
    assert not known_rule("E999")
    assert resolve_rule_id("E999", strict=False) == "E999"
    with pytest.raises(KeyError):
        resolve_rule_id("E999")
    with pytest.raises(KeyError):
        rule_for("E999")


def test_severity_ordering_and_rendering() -> None:
    assert Severity.ERROR.rank < Severity.WARNING.rank < Severity.INFO.rank
    assert Severity.ERROR.is_fatal and not Severity.WARNING.is_fatal
    assert str(Severity.WARNING) == "warning"


def test_rule_summary_rendering() -> None:
    assert str(rule_for("E001")).startswith("E001 (error): ")
