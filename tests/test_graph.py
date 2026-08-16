"""The networkx view of an inventory: construction, filtering, layers, stats.

The properties asserted here are the ones a caller of :mod:`netviz.graph`
depends on:

* the multigraph says exactly what the resolved graph says — same nodes, same
  edges, same VLAN membership — because both come from one resolution pass;
* parallel cables and self-links survive, which is the whole reason the view is
  a *multi* graph;
* iteration order is inventory order, not set order, so anything derived from
  the graph is reproducible across runs;
* :func:`filter_graph` returns an independent graph: mutating the result cannot
  corrupt the input.
"""

from __future__ import annotations

import re
from pathlib import Path

import networkx as nx
import pytest

from netviz.graph import (
    DOMAIN_TYPE,
    ELEMENT_TYPE,
    SUBNET_ID_PREFIX,
    SUBNET_TYPE,
    VLAN_KIND,
    VLAN_NODE_PREFIX,
    BroadcastDomain,
    Layer,
    UnknownElementError,
    broadcast_domains,
    filter_graph,
    layers,
    ports_of,
    resolve_node,
    stats,
    to_networkx,
)
from netviz.loader import Inventory, load_tree
from netviz.render import build_graph
from netviz.subnets import subnets_of

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


@pytest.fixture(scope="module")
def home_lab() -> Inventory:
    inventory = load_tree(EXAMPLES / "home-lab")
    assert inventory.errors == []
    return inventory


@pytest.fixture(scope="module")
def campus() -> Inventory:
    inventory = load_tree(EXAMPLES / "campus")
    assert inventory.errors == []
    return inventory


@pytest.fixture(scope="module")
def home(home_lab: Inventory) -> nx.MultiGraph:
    return to_networkx(home_lab)


@pytest.fixture(scope="module")
def campus_graph(campus: Inventory) -> nx.MultiGraph:
    return to_networkx(campus)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def document(name: str, *interfaces: str, kind: str = "computer", extra: str = "") -> str:
    """One element document whose interfaces are given as flow mappings."""
    return (
        f"apiVersion: netviz.dev/v1alpha1\n"
        f"kind: {kind}\n"
        f"metadata: {{name: {name}}}\n"
        f"spec:\n"
        f"  interfaces: [{', '.join(interfaces)}]\n"
        f"{extra}"
    )


def cable(name: str, left: str, right: str, *, medium: str = "copper", speed: str = "") -> str:
    return (
        f"apiVersion: netviz.dev/v1alpha1\n"
        f"kind: cable\n"
        f"metadata: {{name: {name}}}\n"
        f"spec: {{endpoints: [{left}, {right}], medium: {medium}{speed}}}\n"
    )


def inventory_of(tmp_path: Path, *documents: str) -> Inventory:
    (tmp_path / "inv.yaml").write_text("---\n".join(documents), encoding="utf-8")
    inventory = load_tree(tmp_path)
    assert inventory.errors == [], [str(error) for error in inventory.errors]
    return inventory


def graph_of(tmp_path: Path, *documents: str, layer: Layer = Layer.L1) -> nx.MultiGraph:
    return to_networkx(inventory_of(tmp_path, *documents), layer=layer)


def edge_ids(graph: nx.MultiGraph) -> list[str]:
    return [key for _, _, key in graph.edges(keys=True)]


def domain_ids(domains: tuple[BroadcastDomain, ...]) -> list[str]:
    return [domain.id for domain in domains]


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_devices_and_adapters_are_nodes_and_cables_are_edges(home: nx.MultiGraph) -> None:
    assert set(home.nodes) == {
        "hosts/adp-usb-eth",
        "hosts/laptop",
        "hosts/pc-desk",
        "hosts/phone",
        "hosts/srv-nas",
        "routers/rtr-home",
        "switches/sw-home",
        "wireless/ap-home",
    }
    assert "cables/cbl-rtr-sw" in edge_ids(home)
    assert not any(node.startswith("cables/") for node in home.nodes)


def test_node_order_follows_inventory_load_order(
    campus: Inventory, campus_graph: nx.MultiGraph
) -> None:
    """Set iteration order is not reproducible; the graph's must be."""
    expected = [fqn for fqn in campus.elements if fqn not in campus.cables]
    assert list(campus_graph.nodes) == expected


def test_the_graph_matches_the_resolved_graph_it_came_from(campus: Inventory) -> None:
    resolved = build_graph(campus)
    graph = to_networkx(resolved)
    assert list(graph.nodes) == list(resolved.nodes)
    # networkx yields edges node-major, so compare membership, not sequence.
    assert set(edge_ids(graph)) == {edge.id for edge in resolved.edges}
    assert len(edge_ids(graph)) == len(resolved.edges)


def test_nodes_carry_kind_namespace_and_the_interface_list(home: nx.MultiGraph) -> None:
    data = home.nodes["switches/sw-home"]
    assert data["kind"] == "switch"
    assert data["namespace"] == "switches"
    assert data["name"] == "sw-home"
    assert data["node_type"] == ELEMENT_TYPE
    assert data["interfaces"] == tuple(port.name for port in data["ports"])
    assert "port1" in data["interfaces"]
    assert data["vlans"] == frozenset({10, 20})


def test_edges_carry_endpoints_medium_speed_and_vlans(home: nx.MultiGraph) -> None:
    data = home.edges["routers/rtr-home", "switches/sw-home", "cables/cbl-rtr-sw"]
    assert data["kind"] == "cable"
    assert (data["source"], data["source_port"]) == ("routers/rtr-home", "lan0")
    assert (data["target"], data["target_port"]) == ("switches/sw-home", "port1")
    assert data["medium"] == "copper"
    assert data["speed"] == 1_000_000_000
    assert data["vlans"] == frozenset({10})


def test_an_adapter_attachment_is_an_edge_of_its_own(home: nx.MultiGraph) -> None:
    data = home.edges["hosts/laptop", "hosts/adp-usb-eth", "hosts/adp-usb-eth#upstream"]
    assert data["kind"] == "attachment"
    # §8.1: the host end names no interface.
    assert data["source_port"] == ""
    assert data["target_port"] != ""


def test_parallel_cables_stay_distinct_edges(tmp_path: Path) -> None:
    """A LAG is two links. A simple graph would lose the redundant one."""
    graph = graph_of(
        tmp_path,
        document(
            "sw-a",
            "{name: ge-0, type: ethernet}",
            "{name: ge-1, type: ethernet}",
            kind="switch",
        ),
        document(
            "sw-b",
            "{name: ge-0, type: ethernet}",
            "{name: ge-1, type: ethernet}",
            kind="switch",
        ),
        cable("lag-0", "sw-a:ge-0", "sw-b:ge-0"),
        cable("lag-1", "sw-a:ge-1", "sw-b:ge-1"),
    )
    assert graph.number_of_edges() == 2
    assert graph.number_of_edges("sw-a", "sw-b") == 2
    assert sorted(edge_ids(graph)) == ["lag-0", "lag-1"]


def test_a_self_link_survives(tmp_path: Path) -> None:
    graph = graph_of(
        tmp_path,
        document(
            "sw-a",
            "{name: ge-0, type: ethernet}",
            "{name: ge-1, type: ethernet}",
            kind="switch",
        ),
        cable("loop", "sw-a:ge-0", "sw-a:ge-1"),
    )
    assert graph.number_of_edges() == 1
    assert nx.number_of_selfloops(graph) == 1
    assert ports_of(graph.edges["sw-a", "sw-a", "loop"], "sw-a") == ("ge-0", "ge-1")


def test_ports_of_resolves_the_end_regardless_of_adjacency_order(home: nx.MultiGraph) -> None:
    data = home.edges["routers/rtr-home", "switches/sw-home", "cables/cbl-rtr-sw"]
    assert ports_of(data, "switches/sw-home") == ("port1",)
    assert ports_of(data, "routers/rtr-home") == ("lan0",)
    assert ports_of(data, "hosts/pc-desk") == ()


def test_graph_metadata_carries_root_layer_and_dangling(tmp_path: Path) -> None:
    graph = graph_of(
        tmp_path,
        document("pc", "{name: eth0, type: ethernet}"),
        cable("cbl", "pc:eth0", "ghost:eth0"),
    )
    assert graph.graph["root"] == tmp_path
    assert graph.graph["layer"] == "l1"
    assert len(graph.graph["dangling"]) == 1
    assert "ghost:eth0" in graph.graph["dangling"][0]
    assert graph.number_of_edges() == 0


def test_a_resolved_graph_is_accepted_without_a_second_resolution(campus: Inventory) -> None:
    resolved = build_graph(campus, layer=Layer.L3)
    graph = to_networkx(resolved, layer=Layer.L3)
    assert graph.graph["layer"] == "l3"


def test_a_contradicting_layer_is_refused(campus: Inventory) -> None:
    resolved = build_graph(campus, layer=Layer.L3)
    with pytest.raises(ValueError, match="contradicts the resolved graph"):
        to_networkx(resolved, layer=Layer.L1)


def test_the_layer_3_graph_holds_subnet_nodes(home_lab: Inventory) -> None:
    graph = to_networkx(home_lab, layer=Layer.L3)
    subnet = f"{SUBNET_ID_PREFIX}192.168.10.0/24"
    assert graph.nodes[subnet]["node_type"] == SUBNET_TYPE
    assert graph.nodes[subnet]["members"] == graph.nodes[subnet]["subnet"].elements
    assert "hosts/pc-desk" in graph.nodes[subnet]["members"]


def test_construction_is_deterministic(home_lab: Inventory) -> None:
    first, second = to_networkx(home_lab), to_networkx(home_lab)
    assert list(first.nodes) == list(second.nodes)
    assert edge_ids(first) == edge_ids(second)


# --------------------------------------------------------------------------- #
# Node resolution
# --------------------------------------------------------------------------- #


def test_resolve_node_accepts_a_fully_qualified_or_a_short_name(home: nx.MultiGraph) -> None:
    assert resolve_node(home, "switches/sw-home") == "switches/sw-home"
    assert resolve_node(home, "sw-home") == "switches/sw-home"


def test_resolve_node_reports_an_unknown_name(home: nx.MultiGraph) -> None:
    with pytest.raises(UnknownElementError) as excinfo:
        resolve_node(home, "nowhere")
    assert excinfo.value.name == "nowhere"
    assert excinfo.value.candidates == ()


def test_an_ambiguous_short_name_lists_every_match(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for folder in ("a", "b"):
        (tmp_path / folder / "inv.yaml").write_text(
            document("pc", "{name: eth0, type: ethernet}"), encoding="utf-8"
        )
    graph = to_networkx(load_tree(tmp_path))
    with pytest.raises(UnknownElementError) as excinfo:
        resolve_node(graph, "pc")
    assert sorted(excinfo.value.candidates) == ["a/pc", "b/pc"]


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


def test_no_predicate_keeps_everything_but_returns_an_independent_graph(
    home: nx.MultiGraph,
) -> None:
    result = filter_graph(home)
    assert list(result.nodes) == list(home.nodes)
    assert edge_ids(result) == edge_ids(home)
    result.remove_node("switches/sw-home")
    assert home.has_node("switches/sw-home"), "the input must not be a view into the result"


def test_filtering_preserves_input_order(campus_graph: nx.MultiGraph) -> None:
    result = filter_graph(campus_graph, kinds=["switch"])
    assert list(result.nodes) == [
        fqn for fqn in campus_graph.nodes if campus_graph.nodes[fqn]["kind"] == "switch"
    ]


def test_filter_by_kind(home: nx.MultiGraph) -> None:
    result = filter_graph(home, kinds=["switch", "router"])
    assert set(result.nodes) == {"switches/sw-home", "wireless/ap-home", "routers/rtr-home"}
    assert edge_ids(result) == ["cables/cbl-rtr-sw", "cables/cbl-sw-ap"]


def test_filter_by_namespace_includes_descendants(campus_graph: nx.MultiGraph) -> None:
    result = filter_graph(campus_graph, namespaces=["sites/north"])
    assert result.number_of_nodes()
    assert all(data["namespace"].startswith("sites/north") for _, data in result.nodes(data=True))
    assert "sites/north/access/sw-north-acc-01" in result


def test_the_root_namespace_selects_everything(home: nx.MultiGraph) -> None:
    assert set(filter_graph(home, namespaces=[""]).nodes) == set(home.nodes)


def test_filter_by_name_regex_matches_fqn_and_short_name(campus_graph: nx.MultiGraph) -> None:
    by_short = filter_graph(campus_graph, name_regex=r"^pc-north-")
    assert set(by_short.nodes) == {
        "sites/north/hosts/pc-north-01",
        "sites/north/hosts/pc-north-02",
    }
    by_path = filter_graph(campus_graph, name_regex=r"^sites/north/hosts/")
    assert by_path.number_of_nodes()
    assert all(fqn.startswith("sites/north/hosts/") for fqn in by_path.nodes)


def test_a_compiled_pattern_is_accepted(home: nx.MultiGraph) -> None:
    assert set(filter_graph(home, name_regex=re.compile("^sw-")).nodes) == {"switches/sw-home"}


def test_filter_by_vlan_keeps_the_untagged_host_of_an_access_port(home: nx.MultiGraph) -> None:
    result = filter_graph(home, vlans=[10])
    assert "hosts/pc-desk" in result, "the host declares no vlan block yet sits in VLAN 10"


def test_filter_by_an_absent_vlan_empties_the_graph(home: nx.MultiGraph) -> None:
    assert filter_graph(home, vlans=[4000]).number_of_nodes() == 0


def test_a_link_carrying_none_of_the_requested_vlans_is_dropped(
    campus_graph: nx.MultiGraph,
) -> None:
    """Both ends may survive on other grounds; the link is still not in the domain."""
    result = filter_graph(campus_graph, vlans=[10])
    for _, _, key, data in result.edges(keys=True, data=True):
        assert not data["vlans"] or data["vlans"] & {10}, key


def test_a_parallel_link_outside_the_requested_vlan_is_dropped(tmp_path: Path) -> None:
    """Both switches are in VLAN 10 and 20; only the link carrying 10 belongs."""
    graph = graph_of(
        tmp_path,
        document(
            "sw-a",
            "{name: ge-0, type: ethernet, vlan: {mode: access, access_vlan: 10}}",
            "{name: ge-1, type: ethernet, vlan: {mode: access, access_vlan: 20}}",
            kind="switch",
        ),
        document(
            "sw-b",
            "{name: ge-0, type: ethernet, vlan: {mode: access, access_vlan: 10}}",
            "{name: ge-1, type: ethernet, vlan: {mode: access, access_vlan: 20}}",
            kind="switch",
        ),
        cable("cbl-ten", "sw-a:ge-0", "sw-b:ge-0"),
        cable("cbl-twenty", "sw-a:ge-1", "sw-b:ge-1"),
    )
    result = filter_graph(graph, vlans=[10])
    assert set(result.nodes) == {"sw-a", "sw-b"}
    assert edge_ids(result) == ["cbl-ten"]


def test_predicates_combine_with_and(campus_graph: nx.MultiGraph) -> None:
    result = filter_graph(campus_graph, namespaces=["sites/north"], kinds=["switch"])
    assert set(result.nodes) == {
        fqn
        for fqn, data in campus_graph.nodes(data=True)
        if data["kind"] == "switch" and data["namespace"].startswith("sites/north")
    }


def test_neighbors_of_at_depth_zero_keeps_only_the_seed(home: nx.MultiGraph) -> None:
    result = filter_graph(home, neighbors_of="sw-home", depth=0)
    assert set(result.nodes) == {"switches/sw-home"}


def test_neighbors_of_widens_with_depth(home: nx.MultiGraph) -> None:
    one = filter_graph(home, neighbors_of="hosts/laptop", depth=1)
    two = filter_graph(home, neighbors_of="hosts/laptop", depth=2)
    assert set(one.nodes) == {"hosts/laptop", "hosts/adp-usb-eth"}
    assert set(two.nodes) == {"hosts/laptop", "hosts/adp-usb-eth", "switches/sw-home"}


def test_neighbors_of_traverses_nodes_the_other_predicates_would_remove(
    home: nx.MultiGraph,
) -> None:
    """The switch two hops away is reachable only through the adapter."""
    result = filter_graph(home, neighbors_of="hosts/laptop", depth=2, kinds=["computer", "switch"])
    assert set(result.nodes) == {"hosts/laptop", "switches/sw-home"}


def test_neighbors_of_rejects_an_unknown_element(home: nx.MultiGraph) -> None:
    with pytest.raises(UnknownElementError):
        filter_graph(home, neighbors_of="nowhere")


def test_filtering_narrows_a_subnet_to_the_members_that_survived(home_lab: Inventory) -> None:
    graph = to_networkx(home_lab, layer=Layer.L3)
    result = filter_graph(graph, kinds=["router"])
    for fqn, data in result.nodes(data=True):
        if data["node_type"] != SUBNET_TYPE:
            continue
        assert data["members"] == ("routers/rtr-home",), fqn
        assert data["subnet"].elements == ("routers/rtr-home",)


def test_a_subnet_nobody_left_is_addressed_in_is_dropped(home_lab: Inventory) -> None:
    graph = to_networkx(home_lab, layer=Layer.L3)
    result = filter_graph(graph, kinds=["router"])
    prefixes = {fqn for fqn, data in result.nodes(data=True) if data["node_type"] == SUBNET_TYPE}
    router_prefixes = {
        subnet.prefix for subnet in subnets_of(home_lab) if "routers/rtr-home" in subnet.elements
    }
    assert prefixes == {f"{SUBNET_ID_PREFIX}{prefix}" for prefix in router_prefixes}


def test_a_subnet_named_directly_survives_even_when_emptied(home_lab: Inventory) -> None:
    graph = to_networkx(home_lab, layer=Layer.L3)
    seed = f"{SUBNET_ID_PREFIX}192.168.10.0/24"
    result = filter_graph(graph, neighbors_of=seed, depth=0)
    assert set(result.nodes) == {seed}


# --------------------------------------------------------------------------- #
# Layers
# --------------------------------------------------------------------------- #


def test_l1_holds_every_cable_and_attachment(home: nx.MultiGraph) -> None:
    view = layers(home).l1
    assert set(view.nodes) == set(home.nodes)
    assert edge_ids(view) == edge_ids(home)
    assert view.graph["layer"] == "l1"


def test_l1_of_a_routed_graph_keeps_no_logical_edge(home_lab: Inventory) -> None:
    view = layers(to_networkx(home_lab, layer=Layer.L3)).l1
    assert view.number_of_edges() == 0
    assert not any(fqn.startswith(SUBNET_ID_PREFIX) for fqn in view.nodes)


def test_l2_joins_every_member_to_its_broadcast_domain(home: nx.MultiGraph) -> None:
    view = layers(home).l2
    domain = f"{VLAN_NODE_PREFIX}10"
    assert view.nodes[domain]["kind"] == VLAN_KIND
    assert view.nodes[domain]["node_type"] == DOMAIN_TYPE
    assert view.nodes[domain]["vlan"] == 10
    assert view.graph["layer"] == "l2"
    # Everything but the phone, which is on the wireless side of the access
    # point: its SSID is in VLAN 10, but the radio declares no `vlan` block, so
    # the link that joins it carries no VLAN of its own.
    assert set(view.neighbors(domain)) == set(home.nodes) - {"hosts/phone"}
    edge = view.edges["switches/sw-home", domain, f"switches/sw-home#{domain}"]
    assert edge["kind"] == VLAN_KIND
    assert "port1" in edge["interfaces"]


def test_a_host_with_no_vlan_block_still_joins_through_its_access_port(
    home: nx.MultiGraph,
) -> None:
    view = layers(home).l2
    assert view.has_edge("hosts/pc-desk", f"{VLAN_NODE_PREFIX}10")
    # It declares no `vlan` block, so it contributes no interface to the label.
    assert (
        view.edges["hosts/pc-desk", f"{VLAN_NODE_PREFIX}10", "hosts/pc-desk#vlan:10"]["interfaces"]
        == ()
    )


def test_membership_reaches_a_host_through_its_adapter(home: nx.MultiGraph) -> None:
    """§8.2: collapsing the dongle must not change what the laptop is attached to."""
    domain = next(d for d in broadcast_domains(home) if d.vlan == 10)
    assert "hosts/laptop" in domain.members
    assert "hosts/adp-usb-eth#upstream" in domain.links


def test_elements_in_no_vlan_drop_out_of_the_l2_view(tmp_path: Path) -> None:
    graph = graph_of(
        tmp_path,
        document("pc-a", "{name: eth0, type: ethernet}"),
        document("pc-b", "{name: eth0, type: ethernet}"),
        cable("cbl", "pc-a:eth0", "pc-b:eth0"),
    )
    result = layers(graph)
    assert result.domains == ()
    assert result.l2.number_of_nodes() == 0
    assert result.l1.number_of_edges() == 1


def test_one_vlan_on_two_unjoined_islands_is_two_domains(tmp_path: Path) -> None:
    """A VLAN id is a label on a link, not a network."""
    access = "{name: ge-0, type: ethernet, vlan: {mode: access, access_vlan: 10}}"
    graph = graph_of(
        tmp_path,
        document("sw-a", access, kind="switch"),
        document("pc-a", "{name: eth0, type: ethernet}"),
        cable("cbl-a", "sw-a:ge-0", "pc-a:eth0"),
        document("sw-b", access, kind="switch"),
        document("pc-b", "{name: eth0, type: ethernet}"),
        cable("cbl-b", "sw-b:ge-0", "pc-b:eth0"),
    )
    domains = broadcast_domains(graph)
    assert domain_ids(domains) == ["vlan:10#1", "vlan:10#2"]
    assert domains[0].members == ("sw-a", "pc-a")
    assert domains[1].members == ("sw-b", "pc-b")
    assert domains[0].index == 1 and domains[1].index == 2
    assert domains[0].name == "vlan10#1"
    assert not domains[0].is_isolated
    view = layers(graph).l2
    assert nx.number_connected_components(view) == 2


def test_a_trunk_that_prunes_a_vlan_splits_the_domain(tmp_path: Path) -> None:
    """The uplink carries 10 only, so VLAN 20 cannot cross it."""
    graph = graph_of(
        tmp_path,
        document(
            "sw-a",
            "{name: ge-0, type: ethernet, vlan: {mode: trunk, trunk_vlans: [10]}}",
            "{name: ge-1, type: ethernet, vlan: {mode: access, access_vlan: 20}}",
            kind="switch",
        ),
        document(
            "sw-b",
            "{name: ge-0, type: ethernet, vlan: {mode: trunk, trunk_vlans: [10]}}",
            "{name: ge-1, type: ethernet, vlan: {mode: access, access_vlan: 20}}",
            kind="switch",
        ),
        cable("uplink", "sw-a:ge-0", "sw-b:ge-0"),
    )
    by_vlan = {domain.id: domain.members for domain in broadcast_domains(graph)}
    assert by_vlan["vlan:10"] == ("sw-a", "sw-b")
    assert by_vlan["vlan:20#1"] == ("sw-a",)
    assert by_vlan["vlan:20#2"] == ("sw-b",)


def test_a_vlan_declared_but_carried_nowhere_is_an_isolated_domain(tmp_path: Path) -> None:
    """§6.4: a VLAN in the database exists even when no port references it."""
    graph = graph_of(
        tmp_path,
        document(
            "sw-a",
            "{name: ge-0, type: ethernet}",
            kind="switch",
            extra="  vlans: [{id: 99, name: quarantine}]\n",
        ),
    )
    (domain,) = broadcast_domains(graph)
    assert domain.id == "vlan:99"
    assert domain.members == ("sw-a",)
    assert domain.links == ()
    assert domain.is_isolated


def test_domains_are_ordered_by_vlan_id_then_by_member_order(campus_graph: nx.MultiGraph) -> None:
    domains = broadcast_domains(campus_graph)
    assert [domain.vlan for domain in domains] == sorted(domain.vlan for domain in domains)
    order = list(campus_graph.nodes)
    for domain in domains:
        assert list(domain.members) == sorted(domain.members, key=order.index)


def test_the_campus_sites_are_separate_broadcast_domains(campus_graph: nx.MultiGraph) -> None:
    """The backbone is routed, so VLAN 10 does not span the three sites."""
    tens = [domain for domain in broadcast_domains(campus_graph) if domain.vlan == 10]
    assert len(tens) == 3
    assert all(len({fqn.split("/")[1] for fqn in domain.members}) == 1 for domain in tens)


def test_layers_is_deterministic(campus_graph: nx.MultiGraph) -> None:
    first, second = layers(campus_graph), layers(campus_graph)
    assert domain_ids(first.domains) == domain_ids(second.domains)
    assert list(first.l2.nodes) == list(second.l2.nodes)
    assert edge_ids(first.l2) == edge_ids(second.l2)


def test_a_domain_view_can_be_filtered(home: nx.MultiGraph) -> None:
    view = layers(home).l2
    result = filter_graph(view, kinds=["switch"])
    domain = f"{VLAN_NODE_PREFIX}10"
    assert set(result.nodes) == {"switches/sw-home", "wireless/ap-home", domain, "vlan:20"}
    assert result.nodes[domain]["members"] == ("switches/sw-home", "wireless/ap-home")


def test_a_domain_with_no_surviving_member_is_dropped(home: nx.MultiGraph) -> None:
    view = layers(home).l2
    assert filter_graph(view, kinds=["hub"]).number_of_nodes() == 0


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def test_stats_counts_nodes_edges_vlans_and_subnets(
    home_lab: Inventory, home: nx.MultiGraph
) -> None:
    summary = stats(home)
    assert summary.nodes == home.number_of_nodes()
    assert summary.edges == home.number_of_edges()
    assert summary.elements == summary.nodes
    assert summary.vlans == 2
    assert summary.subnets == len(subnets_of(home_lab))
    assert summary.namespaces == 4
    assert summary.components == 1
    assert summary.by_kind == {
        "adapter": 1,
        "computer": 3,
        "router": 1,
        "server": 1,
        "switch": 2,
    }


def test_stats_counts_the_same_subnets_at_every_layer(campus: Inventory) -> None:
    expected = len(subnets_of(campus))
    assert stats(to_networkx(campus)).subnets == expected
    assert stats(to_networkx(campus, layer=Layer.L3)).subnets == expected


def test_stats_excludes_loopback_addresses_from_the_subnet_count(tmp_path: Path) -> None:
    graph = graph_of(
        tmp_path,
        document("pc", "{name: lo, type: loopback, ipv4: [127.0.0.1/8]}"),
    )
    assert stats(graph).subnets == 0


def test_stats_separates_derived_nodes_from_elements(home_lab: Inventory) -> None:
    summary = stats(to_networkx(home_lab, layer=Layer.L3))
    assert summary.elements < summary.nodes
    assert sum(summary.by_kind.values()) == summary.elements


def test_stats_counts_components(tmp_path: Path) -> None:
    graph = graph_of(
        tmp_path,
        document("pc-a", "{name: eth0, type: ethernet}"),
        document("pc-b", "{name: eth0, type: ethernet}"),
    )
    assert stats(graph).components == 2


def test_stats_of_an_empty_graph(tmp_path: Path) -> None:
    graph = filter_graph(
        graph_of(tmp_path, document("pc", "{name: eth0, type: ethernet}")), kinds=["switch"]
    )
    summary = stats(graph)
    assert summary.as_dict() == {
        "nodes": 0,
        "edges": 0,
        "elements": 0,
        "vlans": 0,
        "tunnels": 0,
        "subnets": 0,
        "namespaces": 0,
        "components": 0,
        "by_kind": {},
    }


def test_stats_is_json_serialisable(campus_graph: nx.MultiGraph) -> None:
    import json

    payload = json.dumps(stats(campus_graph).as_dict())
    assert json.loads(payload)["nodes"] == campus_graph.number_of_nodes()


def test_an_edge_whose_endpoint_is_not_an_element_never_resurrects_it(
    home: nx.MultiGraph,
) -> None:
    """networkx's ``add_edge`` auto-creates missing nodes; the L1 view must not.

    Reclassifying a node is the shortest way to produce a physical edge with an
    endpoint the view drops. The guard exists so that such an edge disappears
    with its endpoint instead of quietly putting the node back.
    """
    graph = filter_graph(home)
    graph.nodes["switches/sw-home"]["node_type"] = SUBNET_TYPE
    view = layers(graph).l1
    assert not view.has_node("switches/sw-home")
    assert edge_ids(view) == ["hosts/adp-usb-eth#upstream", "cables/wl-ap-phone"]
