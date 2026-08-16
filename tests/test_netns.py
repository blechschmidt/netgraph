"""Network namespaces and veth pairs: the model, the rules and the view (§23).

What is covered here is what §23 adds and has no home elsewhere:

* the ``spec.netns`` table and its nesting, which is the one part of the model
  that is a *tree* rather than a flat list — including the cycle a per-entry
  validator cannot see;
* ``interfaces[].peer``, and in particular the symmetry the model insists on,
  because a veth pair described from one end is a pair that cannot be created;
* the four rules the whole inventory is needed for, and the two exemptions the
  feature earns elsewhere (``I002`` on a veth end, ``W111``/``W105`` across
  stacks) — those two matter more than they look, since without them a container
  host is reported once per container;
* the ``netns`` view: what it opens up, what it leaves alone, and that nesting
  survives to arbitrary depth.

The invalid fixtures under ``tests/fixtures/invalid`` pin one finding per rule;
this module pins the *reasons*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from netgraph.errors import SchemaError
from netgraph.export import ExportContext, ExportOptions
from netgraph.export.config import CONFIG_LAYERS, generate
from netgraph.export.manifest import Recorder
from netgraph.loader import Inventory, load_tree
from netgraph.models import (
    API_VERSION,
    ROOT_NETNS,
    InterfaceType,
    NetnsDefinition,
    netns_depth,
    netns_path,
    parse_document,
    resolve_netns_tree,
)
from netgraph.render.graph import (
    NETNS_ID_PREFIX,
    NETNS_KIND,
    EdgeKind,
    FilterSpec,
    Layer,
    NodeType,
    build_graph,
    filter_graph,
    netns_node_id,
)
from netgraph.subnets import subnets_of
from netgraph.validate import validate

# --------------------------------------------------------------------------- #
# Building inventories
# --------------------------------------------------------------------------- #


def device(name: str, *interfaces: dict[str, Any], **spec: Any) -> dict[str, Any]:
    """One ``kind: server`` document, as a mapping ready to be dumped."""
    return {
        "apiVersion": API_VERSION,
        "kind": "server",
        "metadata": {"name": name},
        "spec": {"interfaces": list(interfaces), **spec},
    }


def port(name: str, address: str | None = None, **fields: Any) -> dict[str, Any]:
    """One interface entry; ``ethernet`` unless told otherwise."""
    entry: dict[str, Any] = {"name": name, "type": fields.pop("type", "ethernet"), **fields}
    if address is not None:
        entry["ipv4"] = {"addresses": [address]}
    return entry


def veth(
    host_end: str, far_end: str, netns: str, address: str | None = None
) -> list[dict[str, Any]]:
    """A symmetric pair: the host end in the initial namespace, the far end in ``netns``."""
    return [
        port(host_end, peer=far_end),
        port(far_end, address, peer=host_end, netns=netns),
    ]


def inventory_of(tmp_path: Path, *documents: dict[str, Any]) -> Inventory:
    """Write the documents into one file and load the tree."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "inventory.yaml").write_text(
        "\n---\n".join(yaml.safe_dump(document, sort_keys=False) for document in documents),
        encoding="utf-8",
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == [], "\n".join(str(error) for error in inventory.errors)
    return inventory


def spec_of(**spec: Any) -> Any:
    """Parse one server document and hand back its ``spec``."""
    return parse_document(device("srv", *spec.pop("interfaces"), **spec), source=None).spec


def issues(error: SchemaError) -> list[tuple[str | None, str]]:
    """``(rule, dotted path)`` per issue, which is what a rule test asserts on."""
    return [(issue.rule, ".".join(str(step) for step in issue.path)) for issue in error.issues]


def rules_of(inventory: Inventory) -> list[str]:
    return [finding.rule for finding in validate(inventory)]


# --------------------------------------------------------------------------- #
# The namespace table (§23.1)
# --------------------------------------------------------------------------- #


def test_a_machine_with_no_netns_block_has_exactly_one_stack() -> None:
    """The initial namespace is not declared, and every machine has it."""
    spec = spec_of(interfaces=[port("eth0", "10.0.0.1/24")])
    assert spec.netns == []
    assert spec.netns_names() == (ROOT_NETNS,)
    assert spec.interfaces_in_netns(ROOT_NETNS)[0].name == "eth0"
    assert spec.interfaces[0].netns_name == ROOT_NETNS


def test_namespaces_nest_to_any_depth_and_the_chain_ends_at_the_initial_one() -> None:
    """``parent`` is the whole hierarchy; the path is what a reader is shown."""
    spec = spec_of(
        netns=[
            {"name": "a"},
            {"name": "b", "parent": "a"},
            {"name": "c", "parent": "b"},
            {"name": "d", "parent": "c"},
        ],
        interfaces=[port("eth0", "10.0.0.1/24")],
    )
    parents = spec.netns_parents()
    assert parents == {"a": ROOT_NETNS, "b": "a", "c": "b", "d": "c"}
    assert netns_path("d", parents) == ("a", "b", "c", "d")
    assert netns_depth("d", parents) == 4
    assert netns_depth("a", parents) == 1
    # The initial namespace is the root of the tree and is in no chain.
    assert netns_path(ROOT_NETNS, parents) == ()
    assert netns_depth(ROOT_NETNS, parents) == 0


def test_the_initial_namespace_leads_the_listing_and_declaration_order_follows() -> None:
    spec = spec_of(
        netns=[{"name": "zulu"}, {"name": "alpha"}],
        interfaces=[port("eth0", "10.0.0.1/24")],
    )
    assert spec.netns_names() == (ROOT_NETNS, "zulu", "alpha")


def test_an_interface_is_placed_in_the_namespace_it_names() -> None:
    spec = spec_of(
        netns=[{"name": "blue"}],
        interfaces=[port("eth0", "10.0.0.1/24"), port("eth1", "10.1.0.1/24", netns="blue")],
    )
    assert [itf.name for itf in spec.interfaces_in_netns(ROOT_NETNS)] == ["eth0"]
    assert [itf.name for itf in spec.interfaces_in_netns("blue")] == ["eth1"]
    assert spec.netns_entry("blue") is not None
    assert spec.netns_entry("green") is None


@pytest.mark.parametrize(
    "netns,interfaces,rule,path",
    [
        pytest.param(
            [{"name": "a"}, {"name": "a"}],
            [port("eth0")],
            "NG-N020",
            "spec.netns.1.name",
            id="a name declared twice",
        ),
        pytest.param(
            [{"name": "a", "parent": "nope"}],
            [port("eth0")],
            "NG-N021",
            "spec.netns.0.parent",
            id="a parent that does not exist",
        ),
        pytest.param(
            [{"name": "a", "parent": "a"}],
            [port("eth0")],
            "NG-N021",
            "spec.netns.0.parent",
            id="a namespace inside itself",
        ),
        pytest.param(
            [{"name": "a", "parent": "b"}, {"name": "b", "parent": "a"}],
            [port("eth0")],
            "NG-N021",
            "spec.netns.0.parent",
            id="a two-step nesting cycle",
        ),
        pytest.param(
            [
                {"name": "a", "parent": "c"},
                {"name": "b", "parent": "a"},
                {"name": "c", "parent": "b"},
            ],
            [port("eth0")],
            "NG-N021",
            "spec.netns.0.parent",
            id="a three-step nesting cycle",
        ),
        pytest.param(
            [],
            [port("eth0", netns="blue")],
            "NG-N022",
            "spec.interfaces.0.netns",
            id="an interface in an undeclared namespace",
        ),
    ],
)
def test_the_namespace_table_is_refused_when_it_cannot_be_true(
    netns: list[dict[str, Any]], interfaces: list[dict[str, Any]], rule: str, path: str
) -> None:
    """Every one of these needs the whole ``spec`` in view, and no more than it."""
    with pytest.raises(SchemaError) as exc:
        parse_document(device("srv", *interfaces, netns=netns), source=None)
    assert issues(exc.value) == [(rule, path)]


def test_the_message_for_an_unknown_namespace_lists_the_ones_that_exist() -> None:
    """The fix is nearly always one of the names already in the table."""
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device("srv", port("eth0", netns="bleu"), netns=[{"name": "blue"}, {"name": "green"}]),
            source=None,
        )
    assert "'blue', 'green'" in exc.value.issues[0].message


def test_the_message_says_so_when_the_device_declares_no_namespaces_at_all() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("srv", port("eth0", netns="blue")), source=None)
    assert "declares no 'spec.netns' at all" in exc.value.issues[0].message


def test_a_namespace_entry_is_a_name_a_parent_and_a_description_and_nothing_else() -> None:
    """No configuration: everything a namespace holds is declared by what names it."""
    assert set(NetnsDefinition.model_fields) == {"name", "parent", "description"}


def test_the_tree_helper_maps_an_unset_parent_to_the_initial_namespace() -> None:
    entries = [NetnsDefinition(name="a"), NetnsDefinition(name="b", parent="a")]
    assert resolve_netns_tree(entries) == {"a": ROOT_NETNS, "b": "a"}


def test_the_path_walk_terminates_on_a_cycle_rather_than_spinning() -> None:
    """``NG-N021`` refuses one, but a half-built document may still be walked."""
    assert netns_path("a", {"a": "b", "b": "a"}) in (("b", "a"), ("a", "b"))


# --------------------------------------------------------------------------- #
# Veth pairs (§23.2)
# --------------------------------------------------------------------------- #


def test_a_veth_end_is_an_ordinary_csmacd_interface() -> None:
    """The whole argument for not adding an interface type is this line."""
    spec = spec_of(interfaces=veth("veth-h", "veth-c", "blue"), netns=[{"name": "blue"}])
    host_end = spec.interface("veth-h")
    assert host_end is not None
    assert host_end.type is InterfaceType.ETHERNET
    assert host_end.type.iana_if_type == "ianaift:ethernetCsmacd"
    assert host_end.is_veth and host_end.is_cableable


def test_a_pair_is_reported_once_from_the_end_declared_first() -> None:
    spec = spec_of(interfaces=veth("veth-h", "veth-c", "blue"), netns=[{"name": "blue"}])
    assert [(a.name, b.name) for a, b in spec.veth_pairs()] == [("veth-h", "veth-c")]


def test_two_pairs_on_one_machine_are_both_found_in_declaration_order() -> None:
    spec = spec_of(
        netns=[{"name": "blue"}, {"name": "green"}],
        interfaces=[*veth("vb-h", "vb-c", "blue"), *veth("vg-h", "vg-c", "green")],
    )
    assert [a.name for a, _ in spec.veth_pairs()] == ["vb-h", "vg-h"]


@pytest.mark.parametrize(
    "interfaces,path",
    [
        pytest.param(
            [port("lo", type="loopback", peer="eth0"), port("eth0", peer="lo")],
            "spec.interfaces.0.peer",
            id="a loopback claiming a peer",
        ),
        pytest.param(
            [port("br0", type="bridge", members=["eth0"], peer="eth0"), port("eth0", peer="br0")],
            "spec.interfaces.0.peer",
            id="a bridge claiming a peer",
        ),
        pytest.param([port("eth0", peer="eth0")], "spec.interfaces.0.peer", id="its own peer"),
        pytest.param(
            [port("eth0", peer="ghost")], "spec.interfaces.0.peer", id="a peer that is not there"
        ),
        pytest.param(
            [port("a", peer="b"), port("b")],
            "spec.interfaces.0.peer",
            id="a peer that names nothing back",
        ),
        pytest.param(
            [port("a", peer="b"), port("b", peer="c"), port("c", peer="b")],
            "spec.interfaces.0.peer",
            id="a peer that names somebody else back",
        ),
    ],
)
def test_a_pairing_that_cannot_be_created_is_refused(
    interfaces: list[dict[str, Any]], path: str
) -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("srv", *interfaces), source=None)
    assert issues(exc.value) == [("NG-N023", path)]


def test_the_asymmetry_message_says_what_the_other_end_names_instead() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device("srv", port("a", peer="b"), port("b", peer="c"), port("c", peer="b")),
            source=None,
        )
    assert "names 'c'" in exc.value.issues[0].message


def test_an_adapter_has_no_stack_to_join_and_no_namespace_to_be_in() -> None:
    """§23 is about machines with a kernel; a dongle is not one."""
    for key, rule in (("netns", "NG-N022"), ("peer", "NG-N023")):
        document = {
            "apiVersion": API_VERSION,
            "kind": "adapter",
            "metadata": {"name": "dock"},
            "spec": {
                "upstream": {"name": "usb0", "type": "usb"},
                "interfaces": [{"name": "eth0", "type": "ethernet", key: "blue"}],
            },
        }
        with pytest.raises(SchemaError) as exc:
            parse_document(document, source=None)
        assert issues(exc.value) == [(rule, f"spec.interfaces.0.{key}")]


# --------------------------------------------------------------------------- #
# The rules that need the whole inventory (§23.4)
# --------------------------------------------------------------------------- #


def test_a_cable_on_a_veth_end_is_refused_although_its_type_permits_one(tmp_path: Path) -> None:
    """``E012`` cannot catch this: by type a veth end is exactly a cabled port."""
    inventory = inventory_of(
        tmp_path,
        device("srv-a", *veth("veth-h", "veth-c", "blue", "10.1.0.2/30"), netns=[{"name": "blue"}]),
        device("srv-b", port("eth0", "10.1.0.5/30")),
        {
            "apiVersion": API_VERSION,
            "kind": "cable",
            "metadata": {"name": "cbl"},
            "spec": {"endpoints": ["srv-a:veth-h", "srv-b:eth0"], "medium": "copper"},
        },
    )
    assert "E049" in rules_of(inventory)
    assert "E012" not in rules_of(inventory)


def test_an_aggregate_may_not_reach_into_another_stack(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("br0", "10.1.0.1/30", type="bridge", members=["veth-h"]),
            port("veth-h", peer="veth-c", netns="blue"),
            port("veth-c", "10.2.0.1/30", peer="veth-h"),
            netns=[{"name": "blue"}],
        ),
    )
    assert "E050" in rules_of(inventory)


def test_a_vlan_sub_interface_may_cross_a_namespace_and_is_not_reported(tmp_path: Path) -> None:
    """The one place §23 deliberately does *not* constrain §6.2's stacking."""
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eth0", "10.0.0.1/24", vlan={"mode": "trunk", "trunk_vlans": [10]}),
            port(
                "eth0.10",
                "10.1.0.1/24",
                type="vlan",
                parent="eth0",
                netns="blue",
                vlan={"mode": "access", "access_vlan": 10},
            ),
            netns=[{"name": "blue"}],
        ),
    )
    assert "E050" not in rules_of(inventory)


def test_a_namespace_nothing_is_in_is_a_warning_and_names_what_it_nests(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eth0", "10.0.0.1/24"),
            *veth("veth-h", "veth-c", "inner", "10.1.0.1/24"),
            netns=[{"name": "outer"}, {"name": "inner", "parent": "outer"}],
        ),
    )
    findings = [f for f in validate(inventory) if f.rule == "W146"]
    assert len(findings) == 1
    assert "'outer'" in findings[0].message
    assert "1 namespace ('inner')" in findings[0].message


def test_a_pair_that_crosses_a_boundary_is_silent_and_one_that_does_not_is_info(
    tmp_path: Path,
) -> None:
    crossing = inventory_of(
        tmp_path / "crossing",
        device("srv-a", *veth("veth-h", "veth-c", "blue", "10.1.0.1/30"), netns=[{"name": "blue"}]),
    )
    assert "I005" not in rules_of(crossing)

    inside = inventory_of(
        tmp_path / "inside",
        device("srv-a", port("a", "10.1.0.1/30", peer="b"), port("b", "10.2.0.1/30", peer="a")),
    )
    assert "I005" in rules_of(inside)


def test_a_veth_end_is_not_a_spare_port(tmp_path: Path) -> None:
    """``I002`` would otherwise fire on every end of every pair, actionably on none."""
    inventory = inventory_of(
        tmp_path,
        device("srv-a", *veth("veth-h", "veth-c", "blue", "10.1.0.1/30"), netns=[{"name": "blue"}]),
    )
    uncabled = [f for f in validate(inventory) if f.rule == "I002"]
    assert [f.message for f in uncabled] == []


# --------------------------------------------------------------------------- #
# A namespace partitions the address space (§23.1)
# --------------------------------------------------------------------------- #


def test_two_stacks_of_one_machine_may_hold_the_same_address(tmp_path: Path) -> None:
    """The ordinary shape of two containers built from one image."""
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            *veth("vb-h", "vb-c", "blue", "172.17.0.2/16"),
            *veth("vg-h", "vg-c", "green", "172.17.0.2/16"),
            netns=[{"name": "blue"}, {"name": "green"}],
        ),
    )
    assert "E004" not in rules_of(inventory)


def test_one_stack_holding_an_address_twice_is_still_an_error(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eth0", "10.0.0.1/24", netns="blue"),
            port("eth1", "10.0.0.1/24", netns="blue"),
            netns=[{"name": "blue"}],
        ),
    )
    assert "E004" in rules_of(inventory)
    duplicate = next(f for f in validate(inventory) if f.rule == "E004")
    assert "namespace 'blue'" in duplicate.message


def test_the_two_ends_of_a_routed_pair_are_not_overlapping_prefixes(tmp_path: Path) -> None:
    """Both ends of a ``/30`` on one machine — and not one host with two egresses."""
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("veth-h", "10.1.0.1/30", peer="veth-c"),
            port("veth-c", "10.1.0.2/30", peer="veth-h", netns="blue"),
            netns=[{"name": "blue"}],
        ),
    )
    assert "W111" not in rules_of(inventory)


def test_two_ports_of_one_stack_in_one_prefix_still_overlap(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eth0", "10.1.0.1/24", netns="blue"),
            port("eth1", "10.1.0.2/24", netns="blue"),
            netns=[{"name": "blue"}],
        ),
    )
    overlaps = [f for f in validate(inventory) if f.rule == "W111"]
    assert len(overlaps) == 1
    assert "in namespace 'blue'" in overlaps[0].message


def test_a_subnet_counts_stacks_rather_than_machines(tmp_path: Path) -> None:
    """Three parties on a container host's bridge are three, not one (``W105``)."""
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("br0", "10.30.0.1/24", type="bridge", members=["vb-h", "vg-h"]),
            *veth("vb-h", "vb-c", "blue", "10.30.0.11/24"),
            *veth("vg-h", "vg-c", "green", "10.30.0.12/24"),
            netns=[{"name": "blue"}, {"name": "green"}],
        ),
    )
    subnet = next(s for s in subnets_of(inventory) if s.prefix == "10.30.0.0/24")
    assert subnet.elements == ("srv-a",)
    assert subnet.stacks == (("srv-a", ROOT_NETNS), ("srv-a", "blue"), ("srv-a", "green"))
    assert "W105" not in rules_of(inventory)


# --------------------------------------------------------------------------- #
# The netns view (§23.3)
# --------------------------------------------------------------------------- #


@pytest.fixture
def host(tmp_path: Path) -> Inventory:
    """One container host with a nested namespace, and a switch it is cabled to."""
    return inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eno1", "10.0.0.11/24"),
            *veth("vb-h", "vb-c", "blue", "10.30.0.11/24"),
            port("vw-h", "10.31.0.1/30", peer="vw-c", netns="blue"),
            port("vw-c", "10.31.0.2/30", peer="vw-h", netns="web"),
            netns=[{"name": "blue"}, {"name": "web", "parent": "blue"}],
        ),
        {
            "apiVersion": API_VERSION,
            "kind": "switch",
            "metadata": {"name": "sw-a"},
            "spec": {"interfaces": [port("p1")]},
        },
        {
            "apiVersion": API_VERSION,
            "kind": "cable",
            "metadata": {"name": "cbl"},
            "spec": {"endpoints": ["srv-a:eno1", "sw-a:p1"], "medium": "copper"},
        },
    )


def test_the_view_draws_one_node_per_stack_and_the_element_is_the_initial_one(
    host: Inventory,
) -> None:
    graph = build_graph(host, layer=Layer.NETNS)
    assert set(graph.nodes) == {
        "srv-a",
        netns_node_id("srv-a", "blue"),
        netns_node_id("srv-a", "web"),
        "sw-a",
    }
    machine = graph.nodes["srv-a"]
    assert machine.type is NodeType.ELEMENT and machine.kind == "server"
    assert machine.netns is not None and machine.netns.is_root
    # The machine's own node holds only the ports that never left its stack.
    assert [p.name for p in machine.ports] == ["eno1", "vb-h"]


def test_the_initial_namespace_keeps_the_element_identity(host: Inventory) -> None:
    """A second id for it would mean no stored arrangement could be shared."""
    assert netns_node_id("srv-a", ROOT_NETNS) == "srv-a"
    assert netns_node_id("srv-a", "blue") == f"{NETNS_ID_PREFIX}srv-a:blue"


def test_a_derived_namespace_node_carries_its_path_and_its_ports(host: Inventory) -> None:
    graph = build_graph(host, layer=Layer.NETNS)
    web = graph.nodes[netns_node_id("srv-a", "web")]
    assert web.type is NodeType.NETNS and web.kind == NETNS_KIND and web.is_netns
    assert web.netns is not None
    assert web.netns.path == ("blue", "web")
    assert web.netns.parent == "blue"
    assert web.netns.depth == 2
    assert web.name == "blue/web"
    assert [p.name for p in web.ports] == ["vw-c"]
    assert web.netns.addresses == ("10.31.0.2/30",)


def test_every_stack_of_one_machine_is_boxed_together(host: Inventory) -> None:
    graph = build_graph(host, layer=Layer.NETNS)
    assert graph.clusters == ("srv-a",)
    assert {node.fqn for node in graph.nodes_in_cluster("srv-a")} == {
        "srv-a",
        netns_node_id("srv-a", "blue"),
        netns_node_id("srv-a", "web"),
    }
    # The switch is context, not a subject: it is drawn, and it is not boxed.
    assert graph.nodes["sw-a"].cluster == ""


def test_a_veth_pair_is_drawn_between_the_stacks_it_joins(host: Inventory) -> None:
    graph = build_graph(host, layer=Layer.NETNS)
    veths = {(e.source, e.target): e for e in graph.edges if e.kind is EdgeKind.VETH}
    assert set(veths) == {
        ("srv-a", netns_node_id("srv-a", "blue")),
        (netns_node_id("srv-a", "blue"), netns_node_id("srv-a", "web")),
    }
    crossing = veths[("srv-a", netns_node_id("srv-a", "blue"))]
    assert (crossing.source_port, crossing.target_port) == ("vb-h", "vb-c")


def test_nesting_is_an_edge_from_the_stack_that_created_the_stack(host: Inventory) -> None:
    graph = build_graph(host, layer=Layer.NETNS)
    nesting = {(e.source, e.target) for e in graph.edges if e.kind is EdgeKind.NESTING}
    assert nesting == {
        ("srv-a", netns_node_id("srv-a", "blue")),
        (netns_node_id("srv-a", "blue"), netns_node_id("srv-a", "web")),
    }


def test_a_cable_is_re_pointed_at_the_stack_holding_the_port_it_lands_on(
    tmp_path: Path,
) -> None:
    """The question the view exists for: how does this container reach the wire?"""
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eno1", "10.0.0.11/24", netns="blue"),
            port("veth-h", "10.1.0.1/30", peer="veth-c"),
            port("veth-c", "10.1.0.2/30", peer="veth-h", netns="blue"),
            netns=[{"name": "blue"}],
        ),
        {
            "apiVersion": API_VERSION,
            "kind": "switch",
            "metadata": {"name": "sw-a"},
            "spec": {"interfaces": [port("p1")]},
        },
        {
            "apiVersion": API_VERSION,
            "kind": "cable",
            "metadata": {"name": "cbl"},
            "spec": {"endpoints": ["srv-a:eno1", "sw-a:p1"], "medium": "copper"},
        },
    )
    graph = build_graph(inventory, layer=Layer.NETNS)
    cable = next(edge for edge in graph.edges if edge.kind is EdgeKind.CABLE)
    assert {cable.source, cable.target} == {netns_node_id("srv-a", "blue"), "sw-a"}


def test_a_machine_with_one_stack_is_left_alone(tmp_path: Path) -> None:
    inventory = inventory_of(tmp_path, device("srv-a", port("eth0", "10.0.0.1/24")))
    graph = build_graph(inventory, layer=Layer.NETNS)
    assert graph.is_empty


def test_a_pair_inside_the_initial_namespace_still_opens_the_machine(tmp_path: Path) -> None:
    """It declares no ``netns``, and the pair is still a fact worth drawing."""
    inventory = inventory_of(
        tmp_path,
        device("srv-a", port("a", "10.1.0.1/30", peer="b"), port("b", "10.2.0.1/30", peer="a")),
    )
    graph = build_graph(inventory, layer=Layer.NETNS)
    assert set(graph.nodes) == {"srv-a"}
    veths = [edge for edge in graph.edges if edge.kind is EdgeKind.VETH]
    assert len(veths) == 1 and veths[0].source == veths[0].target == "srv-a"


def test_nothing_more_than_one_hop_from_an_opened_machine_is_drawn(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eno1", "10.0.0.1/24"),
            *veth("v-h", "v-c", "blue", "10.1.0.1/24"),
            netns=[{"name": "blue"}],
        ),
        {
            "apiVersion": API_VERSION,
            "kind": "switch",
            "metadata": {"name": "sw-a"},
            "spec": {"interfaces": [port("p1"), port("p2")]},
        },
        device("srv-far", port("eth0", "10.0.0.9/24")),
        {
            "apiVersion": API_VERSION,
            "kind": "cable",
            "metadata": {"name": "cbl-near"},
            "spec": {"endpoints": ["srv-a:eno1", "sw-a:p1"], "medium": "copper"},
        },
        {
            "apiVersion": API_VERSION,
            "kind": "cable",
            "metadata": {"name": "cbl-far"},
            "spec": {"endpoints": ["srv-far:eth0", "sw-a:p2"], "medium": "copper"},
        },
    )
    graph = build_graph(inventory, layer=Layer.NETNS)
    assert "sw-a" in graph.nodes
    assert "srv-far" not in graph.nodes


def test_every_other_layer_still_draws_the_machine_as_one_box(host: Inventory) -> None:
    """The netns view is the only one that opens it up, and that is the point."""
    for layer in (Layer.L1, Layer.L2):
        graph = build_graph(host, layer=layer)
        assert set(graph.nodes) == {"srv-a", "sw-a"}
        assert not [edge for edge in graph.edges if edge.kind is EdgeKind.VETH]


def test_a_port_carries_its_stack_and_its_peer_at_every_layer(host: Inventory) -> None:
    """Which stack an address is in is a fact about the address, not about a view."""
    graph = build_graph(host, layer=Layer.L1)
    ports = {port.name: port for port in graph.nodes["srv-a"].ports}
    assert ports["eno1"].netns == "" and ports["eno1"].peer is None
    assert ports["vb-c"].netns == "blue" and ports["vb-c"].peer == "vb-h"
    assert ports["vb-h"].is_veth and not ports["eno1"].is_veth


# --------------------------------------------------------------------------- #
# The neutral dialect round-trips it (§23.1, §23.2)
# --------------------------------------------------------------------------- #


def config_for(inventory: Inventory, dialect: str) -> Any:
    """Generate one configuration dialect for the whole inventory, as the CLI does."""
    graphs = {
        layer: filter_graph(build_graph(inventory, layer=layer), FilterSpec())
        for layer in CONFIG_LAYERS
    }
    return generate(
        dialect,
        ExportContext(
            inventory=inventory,
            graphs=graphs,
            options=ExportOptions(),
            recorder=Recorder(),
        ),
    )


def interfaces_conf(inventory: Inventory) -> str:
    """The ``interfaces`` dialect for the whole inventory, as one string."""
    return "\n".join(content for _, content in config_for(inventory, "interfaces").files())


def test_the_neutral_dialect_carries_the_namespaces_and_the_pairs(tmp_path: Path) -> None:
    """Its contract is that it holds one device's interface configuration *completely*.

    A dialect that quietly dropped `spec.netns` would still say "I refuse
    nothing", which is the one thing this format is not allowed to get wrong —
    and `netgraph drift` reads the file back, so what it loses it also stops
    comparing.
    """
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eno1", "10.0.0.1/24"),
            *veth("vb-h", "vb-c", "blue", "10.1.0.1/24"),
            port("vw-h", "10.2.0.1/30", peer="vw-c", netns="blue"),
            port("vw-c", "10.2.0.2/30", peer="vw-h", netns="web"),
            netns=[{"name": "blue", "description": "tenant"}, {"name": "web", "parent": "blue"}],
        ),
    )
    text = interfaces_conf(inventory)
    assert "netns blue\n    description tenant" in text
    assert "netns web\n    parent blue" in text
    assert "    netns blue\n    peer vb-h" in text


def test_the_importer_reads_back_what_the_dialect_wrote(tmp_path: Path) -> None:
    """The pairing and the nesting both survive; neither is guessable."""
    from netgraph.importer.config.neutral import read_interfaces
    from netgraph.importer.draft import Draft

    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eno1", "10.0.0.1/24"),
            *veth("vb-h", "vb-c", "blue", "10.1.0.1/24"),
            netns=[{"name": "blue"}, {"name": "web", "parent": "blue"}],
        ),
    )
    text = interfaces_conf(inventory)

    draft = Draft()
    read_interfaces(text, source="capture.conf", host="srv-a", draft=draft)
    read_back = draft.device("srv-a")
    assert read_back.comments == [], "the reader had to guess at something"
    assert read_back.netns == {"blue": "", "web": "blue"}
    assert read_back.interfaces["vb-c"].netns == "blue"
    assert read_back.interfaces["vb-c"].peer == "vb-h"
    assert read_back.interfaces["vb-h"].netns is None


def test_a_filter_that_keeps_a_machine_keeps_the_stacks_inside_it(host: Inventory) -> None:
    """``--name srv-a`` on the one layer that draws containers means the containers too."""
    graph = filter_graph(build_graph(host, layer=Layer.NETNS), FilterSpec(names=("srv-a",)))
    assert set(graph.nodes) == {
        "srv-a",
        netns_node_id("srv-a", "blue"),
        netns_node_id("srv-a", "web"),
    }
    assert {edge.kind for edge in graph.edges} == {EdgeKind.VETH, EdgeKind.NESTING}


def test_a_filter_that_drops_a_machine_drops_its_stacks_with_it(host: Inventory) -> None:
    graph = filter_graph(build_graph(host, layer=Layer.NETNS), FilterSpec(names=("sw-a",)))
    assert set(graph.nodes) == {"sw-a"}


def test_a_dialect_with_no_namespace_syntax_refuses_rather_than_lying(tmp_path: Path) -> None:
    """A netplan file for a container host would address the wrong stack.

    The value is within netplan's remit and netplan has no syntax for it, which
    is exactly what `Unsupported` is for: writing the file would put the
    container's address on the machine, and on a host running two containers out
    of one image the two would collide where the inventory says they do not.
    """
    from netgraph.export.config import UnsupportedConfigError

    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eno1", "10.0.0.1/24"),
            *veth("vb-h", "vb-c", "blue", "10.1.0.1/24"),
            netns=[{"name": "blue"}],
        ),
    )
    for dialect in ("netplan", "networkd", "ifupdown"):
        with pytest.raises(UnsupportedConfigError) as exc:
            config_for(inventory, dialect)
        fields = {refusal.field for refusal in exc.value.refusals}
        assert "spec.netns[0]" in fields
        assert {"spec.interfaces[1].peer", "spec.interfaces[2].peer"} <= fields


def test_the_neutral_dialect_refuses_nothing_at_all(tmp_path: Path) -> None:
    """Its contract, and the reason the refusals above can point at it."""
    inventory = inventory_of(
        tmp_path,
        device(
            "srv-a",
            port("eno1", "10.0.0.1/24"),
            *veth("vb-h", "vb-c", "blue", "10.1.0.1/24"),
            netns=[{"name": "blue"}],
        ),
    )
    assert config_for(inventory, "interfaces").files()
