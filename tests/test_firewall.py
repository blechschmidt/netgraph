"""Firewalls: zones, filter policy, NAT, and everything §24 has to hold together.

Five places, the same five §16 has to hold together in:

* the **model** — the port-range grammar, the action/field agreement rules, the
  family coherence between a protocol and a prefix, and the zone references
  inside one ``spec``, each reported at schema time with an ``NG-B*`` id and the
  path of the offending value;
* the **derivations** — that a hook comes from the two zone fields rather than
  from a field of its own, that a chain is walked in priority order, and that
  ``unzoned_interfaces`` leaves out the two kinds of port for which "in no zone"
  is the truth rather than an omission;
* the **validator** — the five cross-cutting rules, each on a device that
  differs from a clean one in exactly the way the rule is about, so a finding
  cannot be an accident of the fixture. The two mark rules are the interesting
  pair: they are the two halves of §24.3 and each is silent without the other;
* the **graph and the renderers** — that the ``security`` layer draws zones and
  zone pairs, that a pair's verdict is what colours it, and that DOT, Mermaid
  and JSON say the same thing about all of it;
* the **export** — the nftables ruleset, and the one thing it refuses.

``tests/fixtures/invalid/`` holds one file per rule and ``tests/test_examples.py``
insists each fires exactly once there; the inventories here are built inline so a
test can vary one field at a time.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.errors import SchemaError
from netgraph.export.config import CONFIG_DIALECTS, CONFIG_LAYERS, generate
from netgraph.export.config.model import UnsupportedConfigError
from netgraph.export.context import ExportContext, ExportOptions
from netgraph.export.manifest import Recorder
from netgraph.importer.config import sniff
from netgraph.loader import Inventory, load_tree
from netgraph.models import (
    API_VERSION,
    LOCAL_ZONE,
    FirewallAction,
    FirewallHook,
    FirewallRule,
    NatRule,
    NatType,
    Zone,
    parse_document,
)
from netgraph.models.firewall import normalise_port_range
from netgraph.models.routing import AddressFamily
from netgraph.render.details import build_details, detail_text
from netgraph.render.dot import to_dot
from netgraph.render.graph import (
    ANY_ZONE,
    EdgeKind,
    FilterSpec,
    Layer,
    build_graph,
    filter_graph,
    zone_node_id,
)
from netgraph.render.ids import element_ids
from netgraph.render.jsonexport import graph_to_dict
from netgraph.render.mermaid import to_mermaid
from netgraph.render.options import RenderOptions
from netgraph.render.palette import edge_palette_key
from netgraph.validate import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
CAMPUS = REPO_ROOT / "examples" / "campus"

#: The semantic rules §24 added, for a test that only cares about those.
_SECTION_24 = frozenset({"W150", "W151", "W152", "W153", "W154"})


# --------------------------------------------------------------------------- #
# Building inventories
# --------------------------------------------------------------------------- #


def device(name: str, *interfaces: dict[str, Any], **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": spec.pop("kind", "firewall"),
        "metadata": {"name": name},
        "spec": {"interfaces": list(interfaces), **spec},
    }


def port(name: str, *addresses: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "type": "ethernet", "mtu": 1500, **fields}
    if addresses:
        entry["ipv4"] = {"addresses": list(addresses)}
    return entry


def rule(priority: int, **fields: Any) -> dict[str, Any]:
    return {"priority": priority, "action": fields.pop("action", "accept"), **fields}


def inventory_of(root: Path, *documents: dict[str, Any]) -> Inventory:
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


def issues(exc: pytest.ExceptionInfo[SchemaError]) -> list[tuple[str | None, str]]:
    return [(issue.rule, str(issue.path[-1]) if issue.path else "") for issue in exc.value.issues]


def spec_of(**fields: Any) -> Any:
    """One firewall device, parsed, for a test that only needs its ``spec``."""
    return parse_document(device("fw", port("eth0", "10.0.0.1/30"), **fields)).spec


# --------------------------------------------------------------------------- #
# Port ranges (NG-B005)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        (443, "443"),
        ("443", "443"),
        (" 443 ", "443"),
        ("1000-2000", "1000-2000"),
        ("1000 - 2000", "1000-2000"),
        # A range of one is a port, and is stored as one: two documents that
        # spell one selector differently have to compare equal.
        ("443-443", "443"),
    ],
)
def test_a_port_selector_normalises(written: Any, stored: str) -> None:
    assert normalise_port_range(written) == stored


@pytest.mark.parametrize(
    "written",
    [True, 0, 65536, "0", "65536", "", "http", "1000-", "-2000", "2000-1000", 3.5, None],
)
def test_a_value_that_is_no_port_selector_is_refused(written: Any) -> None:
    with pytest.raises(ValueError, match=r"port|boolean"):
        normalise_port_range(written)


def test_a_backwards_range_says_both_readings() -> None:
    """Neither is picked, because picking one would rewrite what was written."""
    with pytest.raises(ValueError, match=r"either 2000-2000 or 1000-2000"):
        normalise_port_range("2000-1000")


# --------------------------------------------------------------------------- #
# The zone table (NG-B001 to NG-B004)
# --------------------------------------------------------------------------- #


def test_a_zone_holds_the_interfaces_it_names() -> None:
    spec = spec_of(zones=[{"name": "wan", "interfaces": ["eth0"]}])
    zone = spec.zone("wan")
    assert zone is not None and zone.describe() == "wan (eth0)"
    assert spec.zone_of("eth0") is zone
    assert spec.has_zone("wan") and spec.has_zone(LOCAL_ZONE)
    assert not spec.has_zone("dmz")


def test_local_may_not_be_declared() -> None:
    """``NG-B001``: the machine is not one of the parts its interfaces divide into."""
    with pytest.raises(SchemaError) as exc:
        parse_document(device("fw", port("eth0"), zones=[{"name": LOCAL_ZONE}]))
    assert issues(exc) == [("NG-B001", "name")]


def test_a_zone_is_declared_once() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("fw", port("eth0"), zones=[{"name": "wan"}, {"name": "wan"}]))
    assert issues(exc) == [("NG-B001", "name")]


def test_a_zone_holds_only_interfaces_the_device_has() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("fw", port("eth0"), zones=[{"name": "wan", "interfaces": ["eth9"]}]))
    assert issues(exc) == [("NG-B002", "0")]


def test_an_interface_is_in_at_most_one_zone() -> None:
    """``NG-B003``: the defining property, and what makes 'from lan' a statement."""
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "fw",
                port("eth0"),
                zones=[
                    {"name": "wan", "interfaces": ["eth0"]},
                    {"name": "lan", "interfaces": ["eth0"]},
                ],
            )
        )
    assert issues(exc) == [("NG-B003", "0")]
    assert "at most one zone" in str(exc.value)


def test_a_rule_names_a_zone_the_device_has() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "fw",
                port("eth0"),
                zones=[{"name": "wan", "interfaces": ["eth0"]}],
                firewall={"rules": [rule(10, src_zone="dmz")]},
            )
        )
    assert issues(exc) == [("NG-B004", "src_zone")]
    # The undeclared 'local' is offered alongside the declared ones, because it
    # is an equally valid answer to what somebody meant.
    assert "'local'" in str(exc.value) and "'wan'" in str(exc.value)


def test_a_rule_from_a_zone_to_itself_is_refused() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "fw",
                port("eth0"),
                zones=[{"name": "wan", "interfaces": ["eth0"]}],
                firewall={"rules": [rule(10, src_zone="wan", dst_zone="wan")]},
            )
        )
    assert issues(exc) == [("NG-B004", "dst_zone")]


def test_a_nat_rule_names_a_zone_the_device_has() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "fw",
                port("eth0"),
                firewall={"nat": [{"type": "masquerade", "dst_zone": "wan"}]},
            )
        )
    assert issues(exc) == [("NG-B004", "dst_zone")]


def test_a_hub_declares_neither_zones_nor_a_firewall() -> None:
    """``NG-H003``: no IP stack means nothing to filter with."""
    with pytest.raises(SchemaError) as exc:
        parse_document(device("hub1", port("eth0"), kind="hub", zones=[{"name": "wan"}]))
    assert issues(exc) == [("NG-H003", "zones")]


# --------------------------------------------------------------------------- #
# The rule (NG-B005 to NG-B009)
# --------------------------------------------------------------------------- #


def one_rule(**fields: Any) -> FirewallRule:
    return FirewallRule.model_validate({"priority": 10, "action": "accept", **fields})


@pytest.mark.parametrize(
    ("fields", "path"),
    [
        ({"action": "mark"}, "mark"),
        ({"action": "accept", "mark": "0x1"}, "mark"),
        ({"action": "accept", "log_prefix": "hi"}, "log_prefix"),
        ({"dst_ports": ["22"]}, "dst_ports"),
        ({"protocol": "gre", "dst_ports": ["22"]}, "dst_ports"),
        ({"protocol": "tcp", "dst_ports": ["22", "22"]}, "1"),
        ({"ct_state": ["new", "new"]}, "1"),
        ({"src": "10.0.0.0/8", "dst": "2001:db8::/32"}, "dst"),
        ({"family": "ipv6", "src": "10.0.0.0/8"}, "src"),
        ({"protocol": "icmp", "family": "ipv6"}, "protocol"),
        ({"protocol": "icmpv6", "src": "10.0.0.0/8"}, "protocol"),
        ({"invert": True}, "invert"),
    ],
)
def test_a_rule_that_contradicts_itself_is_refused(fields: dict[str, Any], path: str) -> None:
    with pytest.raises(ValueError, match=r"NG-B005|rule|port|mark|log_prefix"):
        one_rule(**fields)


def test_an_action_is_stated_and_never_defaulted() -> None:
    """A rule whose action nobody wrote is a rule nobody finished writing."""
    with pytest.raises(ValueError):
        FirewallRule.model_validate({"priority": 10})


def test_a_rule_selects_only_on_interfaces_the_device_has() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("fw", port("eth0"), firewall={"rules": [rule(10, iif="eth9")]}))
    assert issues(exc) == [("NG-B009", "iif")]


def test_two_rules_of_one_family_may_not_share_a_priority() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device("fw", port("eth0"), firewall={"rules": [rule(10), rule(10, action="drop")]})
        )
    assert issues(exc) == [("NG-B008", "priority")]


def test_two_rules_of_different_families_may_share_a_priority() -> None:
    """The two chains are separate lists; a priority is unique within one."""
    spec = spec_of(
        firewall={
            "rules": [
                rule(10, family="ipv4"),
                rule(10, family="ipv6", action="drop"),
            ]
        }
    )
    assert len(spec.firewall.rules_in(family=AddressFamily.IPV4)) == 1
    assert len(spec.firewall.rules_in(family=AddressFamily.IPV6)) == 1


@pytest.mark.parametrize(
    ("fields", "hooks"),
    [
        ({"dst_zone": LOCAL_ZONE}, (FirewallHook.INPUT,)),
        ({"src_zone": LOCAL_ZONE}, (FirewallHook.OUTPUT,)),
        ({"src_zone": "lan", "dst_zone": "wan"}, (FirewallHook.FORWARD,)),
        ({"src_zone": "lan"}, (FirewallHook.FORWARD, FirewallHook.INPUT)),
        ({"dst_zone": "wan"}, (FirewallHook.FORWARD, FirewallHook.OUTPUT)),
        ({}, (FirewallHook.INPUT, FirewallHook.FORWARD, FirewallHook.OUTPUT)),
    ],
)
def test_the_hook_is_derived_from_the_zones(
    fields: dict[str, Any], hooks: tuple[FirewallHook, ...]
) -> None:
    """§24.2: the zones already said, so the schema never asks."""
    assert one_rule(**fields).hooks == hooks


@pytest.mark.parametrize(
    ("fields", "families"),
    [
        ({}, (AddressFamily.IPV4, AddressFamily.IPV6)),
        ({"family": "ipv6"}, (AddressFamily.IPV6,)),
        ({"src": "10.0.0.0/8"}, (AddressFamily.IPV4,)),
        ({"dst": "2001:db8::/32"}, (AddressFamily.IPV6,)),
        ({"protocol": "icmp"}, (AddressFamily.IPV4,)),
        ({"protocol": "icmpv6"}, (AddressFamily.IPV6,)),
    ],
)
def test_the_family_is_derived_when_something_says_it(
    fields: dict[str, Any], families: tuple[AddressFamily, ...]
) -> None:
    assert one_rule(**fields).families == families


def test_an_action_knows_whether_it_ends_the_walk() -> None:
    assert FirewallAction.ACCEPT.is_terminal and FirewallAction.ACCEPT.permits
    assert FirewallAction.DROP.is_terminal and FirewallAction.DROP.denies
    assert FirewallAction.REJECT.is_terminal and FirewallAction.REJECT.denies
    # The whole reason the list is not "accept or drop": these two let the packet
    # carry on to the rule that decides.
    assert not FirewallAction.MARK.is_terminal
    assert not FirewallAction.LOG.is_terminal


def test_a_rule_reads_as_a_sentence() -> None:
    written = one_rule(
        src_zone="lan",
        dst_zone="wan",
        src="10.0.0.0/8",
        protocol="tcp",
        dst_ports=["443", "8000-8080"],
        ct_state=["new"],
    )
    assert written.describe() == (
        "10: lan -> wan src 10.0.0.0/8 tcp dport 443,8000-8080 ct new accept"
    )


def test_a_rule_with_no_selector_matches_everything_reaching_its_hooks() -> None:
    assert one_rule().is_catch_all
    assert not one_rule(protocol="tcp").is_catch_all
    # ``invert`` on nothing is refused, so an inverted rule always has a selector
    # and is therefore never a catch-all.
    assert not one_rule(protocol="tcp", invert=True).is_catch_all


def test_a_chain_is_walked_in_priority_order() -> None:
    spec = spec_of(
        firewall={"rules": [rule(200, action="drop"), rule(100), rule(150, action="reject")]}
    )
    assert [entry.priority for entry in spec.firewall.rules_in()] == [100, 150, 200]


# --------------------------------------------------------------------------- #
# NAT (NG-B006)
# --------------------------------------------------------------------------- #


def nat(**fields: Any) -> NatRule:
    return NatRule.model_validate(fields)


@pytest.mark.parametrize(
    ("fields", "path"),
    [
        ({"type": "snat"}, "to_address"),
        ({"type": "dnat"}, "to_address"),
        ({"type": "masquerade", "to_address": "10.0.0.1"}, "to_address"),
        ({"type": "redirect", "to_address": "10.0.0.1"}, "to_address"),
        ({"type": "redirect"}, "to_port"),
        ({"type": "masquerade", "to_port": 8080}, "to_port"),
        ({"type": "dnat", "to_address": "10.0.0.1", "dst_ports": ["80"]}, "dst_ports"),
        ({"type": "snat", "to_address": "10.0.0.1", "dst": "2001:db8::/32"}, "dst"),
    ],
)
def test_a_translation_that_contradicts_itself_is_refused(
    fields: dict[str, Any], path: str
) -> None:
    with pytest.raises(ValueError, match=r"NG-B006|rule|port|address"):
        nat(**fields)


def test_a_translation_reads_as_a_sentence() -> None:
    entry = nat(
        type="dnat",
        src_zone="wan",
        dst_zone=LOCAL_ZONE,
        protocol="tcp",
        dst_ports=["443"],
        to_address="10.0.0.5",
        to_port=8443,
    )
    assert entry.describe() == "dnat wan -> local tcp dport 443 to 10.0.0.5:8443"
    assert nat(type="masquerade", dst_zone="wan").describe() == "masquerade any -> wan"


def test_a_direction_decides_which_fields_a_translation_needs() -> None:
    assert NatType.SNAT.is_source and NatType.SNAT.needs_address
    assert NatType.MASQUERADE.is_source and not NatType.MASQUERADE.needs_address
    assert not NatType.DNAT.is_source and NatType.DNAT.needs_address
    assert not NatType.REDIRECT.is_source and not NatType.REDIRECT.needs_address


# --------------------------------------------------------------------------- #
# The defaults (NG-B007)
# --------------------------------------------------------------------------- #


def test_the_defaults_are_deny_deny_permit() -> None:
    """The shape whose failure mode is a broken service, not an open network."""
    policy = spec_of(firewall={}).firewall
    assert policy.default_input is FirewallAction.DROP
    assert policy.default_forward is FirewallAction.DROP
    assert policy.default_output is FirewallAction.ACCEPT
    assert policy.is_default_deny
    assert policy.default_for(FirewallHook.INPUT) is FirewallAction.DROP
    assert policy.default_for(FirewallHook.OUTPUT) is FirewallAction.ACCEPT


@pytest.mark.parametrize("action", ["mark", "log"])
def test_a_default_has_to_decide_the_packet(action: str) -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("fw", port("eth0"), firewall={"default_input": action}))
    assert issues(exc) == [("NG-B007", "default_input")]


# --------------------------------------------------------------------------- #
# The validator (W150 to W154)
# --------------------------------------------------------------------------- #


def two_boxes(tmp_path: Path, **spec: Any) -> Inventory:
    """One firewall wired to one router, so nothing but §24 can be reported."""
    return inventory_of(
        tmp_path,
        device("fw", port("eth0", "10.0.0.1/30"), **spec),
        device("rtr", port("eth0", "10.0.0.2/30"), kind="router"),
        {
            "apiVersion": API_VERSION,
            "kind": "cable",
            "metadata": {"name": "cbl"},
            "spec": {"endpoints": ["fw:eth0", "rtr:eth0"], "medium": "copper"},
        },
    )


def test_a_clean_firewall_reports_nothing(tmp_path: Path) -> None:
    inventory = two_boxes(
        tmp_path,
        zones=[{"name": "wan", "interfaces": ["eth0"]}],
        firewall={"rules": [rule(10, src_zone="wan", dst_zone=LOCAL_ZONE, action="drop")]},
    )
    assert rules_of(inventory) == []


def test_a_zone_with_no_interface_counts_the_rules_that_cannot_match(tmp_path: Path) -> None:
    inventory = two_boxes(
        tmp_path,
        zones=[{"name": "wan", "interfaces": ["eth0"]}, {"name": "dmz"}],
        firewall={"rules": [rule(10, src_zone="dmz", dst_zone="wan")]},
    )
    (finding,) = validate(inventory)
    assert finding.rule == "W150"
    # The count is the difference between a placeholder and a broken policy.
    assert "1 rule naming it can never match" in finding.message


def test_a_loopback_and_a_lag_member_are_not_unzoned(tmp_path: Path) -> None:
    """§24.1: for those two, "in no zone" is the truth rather than an omission."""
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            {"name": "lo0", "type": "loopback", "ipv4": {"addresses": ["192.0.2.1/32"]}},
            port("eth0"),
            port("eth1"),
            {"name": "bond0", "type": "lag", "mtu": 1500, "members": ["eth0", "eth1"]},
            zones=[{"name": "lan", "interfaces": ["bond0"]}],
            firewall={"rules": [rule(10, src_zone="lan", dst_zone=LOCAL_ZONE)]},
        ),
    )
    assert "W151" not in rules_of(inventory)


def test_an_interface_outside_the_partition_is_reported_once(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            port("eth1"),
            port("eth2"),
            zones=[{"name": "lan", "interfaces": ["eth0"]}],
            firewall={"rules": [rule(10, src_zone="lan", dst_zone=LOCAL_ZONE)]},
        ),
    )
    findings = [finding for finding in validate(inventory) if finding.rule == "W151"]
    # One finding naming both, not one per port: the answer to all of them is
    # the same edit.
    assert len(findings) == 1
    assert "eth1" in findings[0].message and "eth2" in findings[0].message


def test_a_device_with_no_zones_never_reports_an_unzoned_interface(tmp_path: Path) -> None:
    inventory = two_boxes(tmp_path, firewall={"rules": [rule(10, action="drop")]})
    assert "W151" not in rules_of(inventory)


def test_a_mark_nothing_reads_is_reported(tmp_path: Path) -> None:
    inventory = two_boxes(
        tmp_path,
        zones=[{"name": "wan", "interfaces": ["eth0"]}],
        firewall={"rules": [rule(10, dst_zone="wan", action="mark", mark="0x2")]},
    )
    (finding,) = validate(inventory)
    assert finding.rule == "W152"
    assert "does not leave the machine" in finding.message


def test_a_mark_nothing_writes_is_reported(tmp_path: Path) -> None:
    inventory = two_boxes(
        tmp_path,
        zones=[{"name": "wan", "interfaces": ["eth0"]}],
        firewall={"rules": [rule(10, dst_zone="wan")]},
        routing_policy=[{"priority": 100, "fwmark": "0x1", "table": "main"}],
    )
    (finding,) = validate(inventory)
    assert finding.rule == "W153"
    assert "never matches" in finding.message


def test_the_two_mark_rules_are_silent_when_the_loop_is_closed(tmp_path: Path) -> None:
    """§24.3: each half is silent alone and wrong only together."""
    inventory = two_boxes(
        tmp_path,
        zones=[{"name": "wan", "interfaces": ["eth0"]}],
        firewall={"rules": [rule(10, dst_zone="wan", action="mark", mark="0x1")]},
        routing_policy=[{"priority": 100, "fwmark": "0x1", "table": "main"}],
    )
    assert rules_of(inventory) == []


def test_a_device_with_no_firewall_never_reports_a_mark_nothing_writes(tmp_path: Path) -> None:
    """A device whose filtering nobody wrote down may well be marking."""
    inventory = two_boxes(
        tmp_path, routing_policy=[{"priority": 100, "fwmark": "0x1", "table": "main"}]
    )
    assert "W153" not in rules_of(inventory)


def test_a_rule_above_the_one_that_closes_its_chain_is_reported(tmp_path: Path) -> None:
    inventory = two_boxes(
        tmp_path,
        firewall={
            "rules": [
                rule(100, dst_zone=LOCAL_ZONE, action="drop"),
                rule(200, dst_zone=LOCAL_ZONE, protocol="tcp", dst_ports=["22"]),
            ]
        },
    )
    (finding,) = validate(inventory)
    assert finding.rule == "W154"
    assert "can never run" in finding.message


def test_a_catch_all_shadows_nothing_in_another_hook(tmp_path: Path) -> None:
    """Per hook: an input closer says nothing about the forward chain."""
    inventory = two_boxes(
        tmp_path,
        zones=[{"name": "wan", "interfaces": ["eth0"]}, {"name": "lan"}],
        firewall={
            "rules": [
                # Closes the *forward* chain, and only that one: two real zones
                # put it there and nowhere else.
                rule(100, src_zone="lan", dst_zone="wan", action="drop"),
                # Input only, and numbered above the closer. Still reachable.
                rule(200, dst_zone=LOCAL_ZONE, protocol="tcp", dst_ports=["22"]),
            ]
        },
    )
    assert "W154" not in rules_of(inventory)


def test_a_marking_catch_all_shadows_nothing(tmp_path: Path) -> None:
    """It does something to the packet and the walk carries on — the whole point."""
    inventory = two_boxes(
        tmp_path,
        zones=[{"name": "wan", "interfaces": ["eth0"]}],
        firewall={
            "rules": [
                rule(10, action="mark", mark="0x1"),
                rule(20, dst_zone="wan", action="accept"),
            ]
        },
        routing_policy=[{"priority": 100, "fwmark": "0x1", "table": "main"}],
    )
    assert "W154" not in rules_of(inventory)


# --------------------------------------------------------------------------- #
# The security layer
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def campus() -> Inventory:
    loaded = load_tree(CAMPUS)
    assert loaded.errors == []
    return loaded


def test_the_layer_draws_zones_and_the_pairs_between_them(campus: Inventory) -> None:
    graph = build_graph(campus, layer=Layer.SECURITY)
    owner = "sites/west/core/rtr-west-core-01"
    assert set(graph.nodes) == {
        zone_node_id(owner, name) for name in ("campus", "backbone", LOCAL_ZONE, ANY_ZONE)
    }
    # None of the topology survives: a cable says nothing about what may cross.
    assert all(edge.kind is EdgeKind.POLICY for edge in graph.edges)
    assert all(node.cluster == owner for node in graph.nodes.values())


def test_a_zone_node_carries_the_interfaces_in_it(campus: Inventory) -> None:
    graph = build_graph(campus, layer=Layer.SECURITY)
    node = graph.nodes[zone_node_id("sites/west/core/rtr-west-core-01", "backbone")]
    assert node.security is not None
    assert node.security.interfaces == ("xe-0/0/1", "xe-0/0/2")
    assert node.security.is_declared
    assert [entry.name for entry in node.ports] == ["xe-0/0/1", "xe-0/0/2"]


def test_the_two_zones_nobody_declares_say_so(campus: Inventory) -> None:
    graph = build_graph(campus, layer=Layer.SECURITY)
    owner = "sites/west/core/rtr-west-core-01"
    for name in (LOCAL_ZONE, ANY_ZONE):
        view = graph.nodes[zone_node_id(owner, name)].security
        assert view is not None and not view.is_declared and not view.interfaces


def test_a_pair_is_directed(campus: Inventory) -> None:
    """*lan to wan* is a different statement from *wan to lan*."""
    graph = build_graph(campus, layer=Layer.SECURITY)
    owner = "sites/west/core/rtr-west-core-01"
    edge = next(
        edge
        for edge in graph.edges
        if edge.policy and edge.policy.source == "campus" and edge.policy.target == "backbone"
    )
    assert edge.source == zone_node_id(owner, "campus")
    assert edge.target == zone_node_id(owner, "backbone")


@pytest.mark.parametrize(
    ("rules", "verdict"),
    [
        ([rule(10, dst_zone="wan", action="accept")], "open"),
        ([rule(10, dst_zone="wan", action="drop")], "closed"),
        ([rule(10, dst_zone="wan", action="reject")], "closed"),
        (
            [rule(10, dst_zone="wan", action="accept"), rule(20, dst_zone="wan", action="drop")],
            "conditional",
        ),
        # Nothing terminal at all: the decision is further down the chain, and
        # this edge is not the place that says.
        ([rule(10, dst_zone="wan", action="log")], "conditional"),
    ],
)
def test_a_pair_is_coloured_by_its_verdict(
    tmp_path: Path, rules: list[dict[str, Any]], verdict: str
) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            zones=[{"name": "wan", "interfaces": ["eth0"]}],
            firewall={"rules": rules},
        ),
    )
    graph = build_graph(inventory, layer=Layer.SECURITY)
    (edge,) = graph.edges
    assert edge.policy is not None and edge.policy.verdict == verdict
    assert edge_palette_key(edge) == f"policy-{verdict}"


def test_a_label_counts_rather_than_lists_past_a_handful(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            zones=[{"name": "wan", "interfaces": ["eth0"]}],
            firewall={
                "rules": [
                    rule(index, dst_zone="wan", protocol="tcp", dst_ports=[str(index)])
                    for index in range(10, 60, 10)
                ]
            },
        ),
    )
    graph = build_graph(inventory, layer=Layer.SECURITY)
    (edge,) = graph.edges
    assert edge.policy is not None and edge.policy.label() == ("5 rules",)


def test_a_device_with_zones_and_no_policy_is_drawn_as_its_zones(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path, device("fw", port("eth0"), zones=[{"name": "wan", "interfaces": ["eth0"]}])
    )
    graph = build_graph(inventory, layer=Layer.SECURITY)
    assert list(graph.nodes) == [zone_node_id("fw", "wan")]
    assert graph.edges == ()


def test_the_renderers_agree_about_the_layer(campus: Inventory) -> None:
    graph = build_graph(campus, layer=Layer.SECURITY)
    options = RenderOptions()
    dot = to_dot(graph, options)
    mermaid = to_mermaid(graph, options)
    payload = graph_to_dict(graph, options)

    assert "campus" in dot and "backbone" in dot
    assert "campus" in mermaid and "backbone" in mermaid
    zones = {entry["zone"]["name"] for entry in payload["nodes"]}
    assert zones == {"campus", "backbone", LOCAL_ZONE, ANY_ZONE}
    for entry in payload["edges"]:
        assert entry["policy"]["verdict"] in {"open", "closed", "conditional"}
        # A decision runs over no medium.
        assert "medium" not in entry


def test_the_tooltip_says_which_zones_were_never_declared(campus: Inventory) -> None:
    """A reader who thinks 'any' is a configured zone has misread the picture."""
    graph = build_graph(campus, layer=Layer.SECURITY)
    identity = element_ids(graph)
    details = build_details(graph, ids=identity)
    owner = "sites/west/core/rtr-west-core-01"
    text = detail_text(details[identity.node(zone_node_id(owner, ANY_ZONE))])
    assert "not declared" in text and "named no zone" in text

    declared = detail_text(details[identity.node(zone_node_id(owner, "campus"))])
    assert "not declared" not in declared
    assert "xe-0/0/0" in declared


def test_an_edge_tooltip_lists_the_whole_chain_of_the_pair(campus: Inventory) -> None:
    graph = build_graph(campus, layer=Layer.SECURITY)
    identity = element_ids(graph)
    details = build_details(graph, ids=identity)
    index = next(
        position
        for position, entry in enumerate(graph.edges)
        if entry.policy and entry.policy.source == "campus" and entry.policy.target == "backbone"
    )
    text = detail_text(details[identity.edge(index)])
    assert "campus -> backbone: conditional" in text
    assert "mark 0x1" in text


def test_the_cli_renders_the_layer() -> None:
    result = CliRunner().invoke(
        cli, ["-i", str(CAMPUS), "render", "--layer", "security", "-f", "mermaid"]
    )
    assert result.exit_code == 0, result.output
    assert "campus" in result.output and "backbone" in result.output


# --------------------------------------------------------------------------- #
# The nftables export
# --------------------------------------------------------------------------- #


def context_for(inventory: Inventory) -> ExportContext:
    """The input an export gets from the CLI, built the way the CLI builds it."""
    graphs = {
        layer: filter_graph(build_graph(inventory, layer=layer), FilterSpec())
        for layer in CONFIG_LAYERS
    }
    return ExportContext(
        inventory=inventory, graphs=graphs, options=ExportOptions(), recorder=Recorder()
    )


def ruleset(inventory: Inventory) -> str:
    """The generated ``etc/nftables.conf`` of the one device that has one."""
    config = generate("nftables", context_for(inventory))
    (device_config,) = config.devices
    (file,) = device_config.files
    assert file.path == "etc/nftables.conf"
    return file.content


def test_the_ruleset_states_the_zones_the_chains_and_the_defaults(campus: Inventory) -> None:
    text = ruleset(campus)
    assert "destroy table inet netgraph" in text
    assert "table inet netgraph {" in text
    assert "set zone_campus {" in text and 'elements = { "xe-0/0/0" }' in text
    # The default is written *and* attributed, because it is the most
    # consequential word in the file.
    assert "policy drop;  # spec.firewall.default_input" in text
    assert "policy accept;  # spec.firewall.default_forward" in text


def test_a_rule_is_written_into_every_hook_its_zones_put_it_in(campus: Inventory) -> None:
    text = ruleset(campus)
    # The catch-all ct-state rule names no zone, so it is in all three chains.
    assert text.count("ct state established,related accept") == 3
    # The zone pair rule is forward only.
    assert text.count("meta mark set 0x1") == 1


def test_a_rule_carries_its_priority_into_the_comment(campus: Inventory) -> None:
    """``nft list ruleset`` prints it back, so the box can be read against the YAML."""
    text = ruleset(campus)
    assert 'comment "100 Lab VPN egress, marked for the lab-egress table"' in text


def test_nothing_is_inferred(tmp_path: Path) -> None:
    """No ct-state rule the document did not ask for, no loopback, no limit."""
    inventory = inventory_of(
        tmp_path, device("fw", port("eth0"), firewall={"rules": [rule(10, action="drop")]})
    )
    body = ruleset(inventory).split("destroy table", 1)[1]
    assert "ct state" not in body
    assert "iif lo" not in body and "iifname lo" not in body
    assert "limit rate" not in body
    # Three chains, their stated defaults, and the one rule -- written into all
    # three because it names no zone. Nothing else at all.
    assert body.count("accept") == 1  # the stated default_output policy
    assert body.count("policy drop") == 2  # the two stated deny defaults
    assert body.count('drop comment "10"') == 3  # the rule, once per chain


def test_a_reject_default_becomes_a_drop_policy_and_a_trailing_rule(tmp_path: Path) -> None:
    """A chain policy cannot reject: it has to build a packet."""
    inventory = inventory_of(
        tmp_path, device("fw", port("eth0"), firewall={"default_input": "reject"})
    )
    text = ruleset(inventory)
    assert "policy drop;  # spec.firewall.default_input" in text
    assert 'reject comment "default_input: reject"' in text


def test_nat_chains_are_written_only_when_a_translation_needs_them(tmp_path: Path) -> None:
    bare = inventory_of(tmp_path / "bare", device("fw", port("eth0"), firewall={}))
    assert "type nat hook" not in ruleset(bare)

    translating = inventory_of(
        tmp_path / "nat",
        device(
            "fw",
            port("eth0"),
            zones=[{"name": "wan", "interfaces": ["eth0"]}],
            firewall={
                "nat": [
                    {"type": "masquerade", "dst_zone": "wan"},
                    {
                        "type": "dnat",
                        "src_zone": "wan",
                        "protocol": "tcp",
                        "dst_ports": ["443"],
                        "to_address": "10.0.0.5",
                        "to_port": 8443,
                    },
                ]
            },
        ),
    )
    text = ruleset(translating)
    assert "chain postrouting {" in text and "oifname @zone_wan masquerade" in text
    assert "chain prerouting {" in text and "dnat ip to 10.0.0.5:8443" in text


def test_an_inverted_rule_is_refused_rather_than_written_backwards(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            firewall={"rules": [rule(10, protocol="tcp", invert=True, action="drop")]},
        ),
    )
    with pytest.raises(UnsupportedConfigError) as exc:
        ruleset(inventory)
    assert "invert" in str(exc.value)


def test_a_device_with_no_firewall_is_declined_by_name(campus: Inventory) -> None:
    context = context_for(campus)
    generate("nftables", context)
    declined = [
        skip
        for skip in context.recorder.sealed("nftables").skipped
        if "declares no 'spec.firewall'" in skip.detail
    ]
    assert declined, "a device without a firewall should be named, not silently dropped"


def test_a_zone_table_with_no_policy_is_declined_with_its_count(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path, device("fw", port("eth0"), zones=[{"name": "wan", "interfaces": ["eth0"]}])
    )
    context = context_for(inventory)
    generate("nftables", context)
    (skip,) = [
        entry for entry in context.recorder.sealed("nftables").skipped if entry.subject == "fw"
    ]
    assert "1 zone and no policy over them" in skip.detail


def test_the_generated_ruleset_sniffs_as_its_own_dialect(campus: Inventory) -> None:
    text = ruleset(campus)
    assert sniff(text) == "nftables"
    # And without the banner, from the table header alone.
    body = text.split("destroy table", 1)[1]
    assert sniff(body) == "nftables"


def test_the_dialect_is_registered_with_the_rest() -> None:
    entry = CONFIG_DIALECTS["nftables"]
    assert entry.suffix == ".conf" and entry.comment == "#"
    assert "invert" in entry.lossy


def test_the_cli_exports_the_ruleset() -> None:
    result = CliRunner().invoke(
        cli,
        ["-i", str(CAMPUS), "export", "nftables", "--name", "rtr-west-core-01"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "table inet netgraph {" in result.stdout


# --------------------------------------------------------------------------- #
# The firewall device kind
# --------------------------------------------------------------------------- #


def test_a_firewall_forwards_by_default() -> None:
    parsed = parse_document(device("fw", port("eth0")))
    assert parsed.kind == "firewall"
    assert parsed.spec.forwarding is not None
    assert parsed.spec.forwarding.ipv4 and parsed.spec.forwarding.ipv6
    assert parsed.default_glyph == "firewall"


def test_a_router_may_filter_too(tmp_path: Path) -> None:
    """Filtering is a function, not a box (§24)."""
    inventory = inventory_of(
        tmp_path,
        device(
            "rtr",
            port("eth0"),
            kind="router",
            zones=[{"name": "wan", "interfaces": ["eth0"]}],
            firewall={"rules": [rule(10, src_zone="wan", dst_zone=LOCAL_ZONE, action="drop")]},
        ),
    )
    # An unconnected one-device inventory reports the usual cabling findings;
    # what matters here is that nothing in section 24 objects to the kind.
    assert not [rule_id for rule_id in rules_of(inventory) if rule_id in _SECTION_24]
    graph = build_graph(inventory, layer=Layer.SECURITY)
    assert list(graph.nodes) == [zone_node_id("rtr", "wan"), zone_node_id("rtr", LOCAL_ZONE)]


def test_the_kind_is_selectable_as_a_filter() -> None:
    result = CliRunner().invoke(
        cli, ["-i", str(CAMPUS), "render", "--kind", "firewall", "-f", "json"]
    )
    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- #
# The corners the examples do not reach
# --------------------------------------------------------------------------- #


def test_every_selector_reaches_the_sentence() -> None:
    """One rule carrying all of them, so no clause is only ever untested."""
    written = one_rule(
        src="10.0.0.0/8",
        dst="192.0.2.0/24",
        iif="eth0",
        oif="eth1",
        protocol="udp",
        src_ports=["1024-65535"],
        dst_ports=["53"],
        ct_state=["new", "established"],
        invert=True,
    )
    assert written.describe() == (
        "10: any -> any not src 10.0.0.0/8 dst 192.0.2.0/24 iif eth0 oif eth1 "
        "udp sport 1024-65535 dport 53 ct new,established accept"
    )
    assert written.zones == ()


def test_an_empty_zone_says_so_rather_than_printing_nothing() -> None:
    assert Zone(name="dmz").describe() == "dmz (no interface)"


def test_a_translation_names_every_zone_it_states() -> None:
    entry = nat(type="masquerade", src_zone="lan", dst_zone="wan")
    assert entry.zones == ("lan", "wan")
    assert entry.families == (AddressFamily.IPV4, AddressFamily.IPV6)
    assert nat(type="masquerade", family="ipv6").families == (AddressFamily.IPV6,)


def test_a_translation_states_its_family_against_its_addresses() -> None:
    with pytest.raises(ValueError, match=r"one translation rewrites one address family"):
        nat(type="snat", family="ipv6", to_address="10.0.0.1")


def test_a_policy_names_the_zones_it_refers_to_once_each() -> None:
    policy = spec_of(
        zones=[{"name": "wan", "interfaces": ["eth0"]}, {"name": "lan"}],
        firewall={
            "rules": [rule(10, src_zone="lan", dst_zone="wan"), rule(20, src_zone="lan")],
            "nat": [{"type": "masquerade", "dst_zone": "wan"}],
        },
    ).firewall
    assert policy.zones_named() == ("lan", "wan")
    assert policy.marks() == ()
    assert policy.describe() == "2 rule(s), 1 translation(s), default drop/drop/accept"


def test_a_marked_packet_carries_the_mark_into_the_summary() -> None:
    policy = spec_of(
        firewall={
            "rules": [
                rule(10, action="mark", mark="0x1"),
                rule(20, action="mark", mark="0x1"),
                rule(30, action="mark", mark="0x2/0xff"),
            ]
        }
    ).firewall
    # Once each, in the order the chain writes them.
    assert policy.marks() == ("0x1", "0x2/0xff")


def test_a_logging_rule_carries_its_prefix_into_the_ruleset(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            firewall={
                "rules": [
                    rule(10, action="log", log_prefix="dropped: "),
                    rule(20, dst="192.0.2.0/24", action="drop"),
                ]
            },
        ),
    )
    text = ruleset(inventory)
    assert 'log prefix "dropped: "' in text
    assert "ip daddr 192.0.2.0/24 drop" in text


def test_a_translation_selects_on_addresses_too(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            firewall={
                "nat": [
                    {
                        "type": "snat",
                        "src": "10.0.0.0/8",
                        "dst": "192.0.2.0/24",
                        "to_address": "203.0.113.5",
                    }
                ]
            },
        ),
    )
    text = ruleset(inventory)
    assert "ip saddr 10.0.0.0/8 ip daddr 192.0.2.0/24 snat ip to 203.0.113.5" in text


def test_an_empty_zone_is_named_in_the_export_manifest(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("fw", port("eth0"), zones=[{"name": "dmz"}], firewall={"rules": [rule(10)]}),
    )
    context = context_for(inventory)
    generate("nftables", context)
    skips = context.recorder.sealed("nftables").skipped
    assert any("holds no interface" in entry.detail for entry in skips)
    # The set is still written: a rule references it, and a missing one is a
    # parse error rather than a rule that matches nothing.
    assert "set zone_dmz {" in ruleset(inventory)


def test_a_lockout_is_named_before_it_is_applied(tmp_path: Path) -> None:
    """The one generated file that can lock an operator out of the box."""
    inventory = inventory_of(
        tmp_path, device("fw", port("eth0"), firewall={"default_output": "drop"})
    )
    context = context_for(inventory)
    generate("nftables", context)
    skips = context.recorder.sealed("nftables").skipped
    assert any("reaching a package mirror" in entry.detail for entry in skips)


def test_the_policy_database_is_named_with_the_emitter_that_writes_it(campus: Inventory) -> None:
    context = context_for(campus)
    generate("nftables", context)
    skips = context.recorder.sealed("nftables").skipped
    assert any("netgraph export routes" in entry.detail for entry in skips)


def test_a_zone_name_is_folded_into_an_nftables_identifier(tmp_path: Path) -> None:
    """§4.1 allows a dot and a hyphen where an nftables identifier allows neither."""
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            zones=[{"name": "dmz-a.1", "interfaces": ["eth0"]}],
            firewall={"rules": [rule(10, src_zone="dmz-a.1", dst_zone=LOCAL_ZONE)]},
        ),
    )
    text = ruleset(inventory)
    assert "set zone_dmz_a_1 {" in text
    assert "iifname @zone_dmz_a_1 accept" in text


def test_the_importer_reads_the_ruleset_back_and_says_what_it_dropped(campus: Inventory) -> None:
    from netgraph.importer.config import read_nftables
    from netgraph.importer.draft import Draft

    draft = Draft()
    read_nftables(ruleset(campus), source="nftables.conf", host="fw", draft=draft)
    (note,) = [entry for entry in draft.notes if "states" in entry]
    assert "2 zones [campus, backbone]" in note
    assert "3 chains [input, forward, output]" in note
    assert "defaults input=drop" in note
    # Nothing is claimed: a draft has no zone and no rule to hold.
    assert draft.device("fw").interfaces == {}


def test_the_importer_says_nothing_about_a_ruleset_with_nothing_in_it() -> None:
    from netgraph.importer.config import read_nftables
    from netgraph.importer.draft import Draft

    draft = Draft()
    read_nftables("", source="empty.conf", host="fw", draft=draft)
    assert list(draft.notes) == []
    assert draft.device("fw").sources == ["empty.conf"]


def test_a_closer_shadows_only_the_zone_pair_it_names(tmp_path: Path) -> None:
    """``lan -> wan accept`` says nothing about a packet from ``wan`` to ``dmz``.

    The error in the other direction is the worse one: a finding claiming a
    working rule is dead is a finding that gets the rule deleted.
    """
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            port("eth1"),
            port("eth2"),
            zones=[
                {"name": "lan", "interfaces": ["eth0"]},
                {"name": "wan", "interfaces": ["eth1"]},
                {"name": "dmz", "interfaces": ["eth2"]},
            ],
            firewall={
                "rules": [
                    rule(200, src_zone="lan", dst_zone="wan"),
                    # A different pair entirely, and numbered above.
                    rule(300, src_zone="wan", dst_zone="dmz"),
                ]
            },
        ),
    )
    assert "W154" not in rules_of(inventory)


def test_a_closer_naming_neither_zone_shadows_the_whole_chain(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            zones=[{"name": "lan", "interfaces": ["eth0"]}, {"name": "wan"}],
            firewall={
                "rules": [
                    rule(100, action="drop"),
                    rule(200, src_zone="lan", dst_zone="wan"),
                ]
            },
        ),
    )
    findings = [entry for entry in validate(inventory) if entry.rule == "W154"]
    assert len(findings) == 1
    assert "'100: any -> any drop'" in findings[0].message


def test_a_half_stated_closer_shadows_what_it_covers(tmp_path: Path) -> None:
    """An unstated half is every zone; a stated one is only itself."""
    inventory = inventory_of(
        tmp_path,
        device(
            "fw",
            port("eth0"),
            port("eth1"),
            zones=[
                {"name": "lan", "interfaces": ["eth0"]},
                {"name": "wan", "interfaces": ["eth1"]},
            ],
            firewall={
                "rules": [
                    # From lan to anywhere: covers lan -> wan below it.
                    rule(100, src_zone="lan", action="drop"),
                    rule(200, src_zone="lan", dst_zone="wan"),
                    # From wan, which the closer above says nothing about.
                    rule(300, src_zone="wan", dst_zone="lan"),
                ]
            },
        ),
    )
    findings = [entry for entry in validate(inventory) if entry.rule == "W154"]
    assert len(findings) == 1
    assert "200: lan -> wan accept" in findings[0].message


# --------------------------------------------------------------------------- #
# The second opinion
# --------------------------------------------------------------------------- #

#: ``nft --check``, which parses a ruleset without touching the kernel. The one
#: gate here that is not netgraph reading its own output: every assertion above
#: says the file holds what the inventory states, and this one says the file is
#: a file nftables would take. It caught two real defects when it was written —
#: a NAT chain at the wrong hook priority, and a translation nft refuses in an
#: ``inet`` table without a family — neither of which any amount of string
#: comparison would have found.
requires_nft = pytest.mark.skipif(
    shutil.which("nft") is None,
    reason="no 'nft' to syntax-check the generated ruleset with",
)


@requires_nft
@pytest.mark.parametrize("example", ["campus"])
def test_the_generated_ruleset_is_one_nftables_would_take(example: str, tmp_path: Path) -> None:
    inventory = load_tree(REPO_ROOT / "examples" / example)
    path = tmp_path / "nftables.conf"
    path.write_text(ruleset(inventory), encoding="utf-8")
    result = subprocess.run(
        [shutil.which("nft") or "nft", "--check", "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@requires_nft
def test_every_construct_this_emitter_writes_parses(tmp_path: Path) -> None:
    """One device using every field §24 has, checked by nft rather than by eye."""
    inventory = inventory_of(
        tmp_path / "inv",
        device(
            "fw",
            port("eth0"),
            port("eth1"),
            port("eth2"),
            zones=[
                {"name": "wan", "interfaces": ["eth0"], "description": 'the "outside"'},
                {"name": "lan", "interfaces": ["eth1"]},
                {"name": "dmz-a.1", "interfaces": ["eth2"]},
            ],
            firewall={
                "default_input": "reject",
                "default_forward": "drop",
                "default_output": "accept",
                "rules": [
                    rule(10, ct_state=["established", "related"]),
                    rule(20, dst_zone=LOCAL_ZONE, protocol="icmp", family="ipv4"),
                    rule(30, dst_zone=LOCAL_ZONE, protocol="icmpv6", family="ipv6"),
                    rule(
                        40,
                        src_zone="lan",
                        dst_zone=LOCAL_ZONE,
                        protocol="tcp",
                        src_ports=["1024-65535"],
                        dst_ports=["22", "443"],
                    ),
                    rule(
                        50,
                        src_zone="lan",
                        dst_zone="wan",
                        protocol="udp",
                        dst_ports=["1194"],
                        action="mark",
                        mark="0x1/0xff",
                    ),
                    rule(
                        60, src_zone="wan", dst_zone="dmz-a.1", protocol="sctp", dst_ports=["2905"]
                    ),
                    rule(
                        70,
                        src="10.0.0.0/8",
                        dst="192.0.2.0/24",
                        action="log",
                        log_prefix="netgraph: ",
                    ),
                    rule(80, src="2001:db8::/32", protocol="gre", action="drop"),
                    rule(90, iif="eth0", oif="eth1", action="reject"),
                ],
                "nat": [
                    {"type": "masquerade", "dst_zone": "wan"},
                    {"type": "snat", "src_zone": "lan", "to_address": "203.0.113.5"},
                    {
                        "type": "dnat",
                        "src_zone": "wan",
                        "protocol": "tcp",
                        "dst_ports": ["443"],
                        "to_address": "10.0.0.5",
                        "to_port": 8443,
                    },
                    {"type": "dnat", "src_zone": "wan", "to_address": "2001:db8::5"},
                    {
                        "type": "redirect",
                        "src_zone": "lan",
                        "protocol": "tcp",
                        "dst_ports": ["80"],
                        "to_port": 3128,
                    },
                ],
            },
            routing_policy=[{"priority": 100, "fwmark": "0x1/0xff", "table": "main"}],
        ),
    )
    path = tmp_path / "nftables.conf"
    path.write_text(ruleset(inventory), encoding="utf-8")
    result = subprocess.run(
        [shutil.which("nft") or "nft", "--check", "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\n---\n{path.read_text()}"


# --------------------------------------------------------------------------- #
# Filtering the layer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "kept"),
    [
        (FilterSpec(), 4),
        # The predicates reach the zones through the device, because nothing at
        # this layer stands for the box itself.
        (FilterSpec(names=("rtr-west-core-01",)), 4),
        (FilterSpec(names=("sites/west/core/rtr-west-core-01",)), 4),
        (FilterSpec(names=("sw-north-acc-01",)), 0),
        (FilterSpec(namespaces=("sites/west",)), 4),
        (FilterSpec(namespaces=("sites/north",)), 0),
        (FilterSpec(kinds=("router",)), 4),
        (FilterSpec(kinds=("switch",)), 0),
    ],
)
def test_the_layer_filters_by_the_device_a_zone_belongs_to(
    campus: Inventory, spec: FilterSpec, kept: int
) -> None:
    graph = filter_graph(build_graph(campus, layer=Layer.SECURITY), spec)
    assert len(graph.nodes) == kept
    # A filter never leaves an edge with an end it removed.
    assert not [
        edge
        for edge in graph.edges
        if edge.source not in graph.nodes or edge.target not in graph.nodes
    ]


def test_a_zone_carries_the_kind_of_the_device_it_is_on(campus: Inventory) -> None:
    graph = build_graph(campus, layer=Layer.SECURITY)
    payload = graph_to_dict(graph, RenderOptions())
    assert {entry["zone"]["elementKind"] for entry in payload["nodes"]} == {"router"}
