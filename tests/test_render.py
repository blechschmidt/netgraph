"""The graph layer and the three renderers.

The properties asserted here are the ones every consumer depends on:

* :func:`build_graph` resolves every reference exactly once, so a renderer can
  assume both ends of an edge name a node that exists.
* VLAN membership answers "which elements are in VLAN 10?" the way a reader
  means it, including hosts that declare no ``vlan`` block of their own.
* Every renderer is deterministic. Golden-file tests and ``git diff`` on a
  committed diagram are worthless otherwise.
* Every output format is reachable through the registry alone, so a front end
  never needs to know which formats exist.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from netgraph.errors import RenderError
from netgraph.loader import Inventory, load_tree
from netgraph.render import (
    FORMATS,
    MERMAID_MAX_EDGES,
    RENDERERS,
    SUBNET_ID_PREFIX,
    SUBNET_KIND,
    TEXT_FORMATS,
    EdgeKind,
    FilterSpec,
    Graph,
    Layer,
    Node,
    NodeType,
    RenderOptions,
    UnknownElementError,
    advisories_for,
    build_graph,
    filter_graph,
    is_binary_format,
    media_type_for,
    render,
    render_dot,
    render_image,
    render_json,
    render_mermaid,
    render_text,
    renderer_for,
    suffix_for,
    to_dot,
    to_image,
    to_json,
    to_mermaid,
)
from netgraph.render.registry import DEFAULT_MEDIA_TYPE

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


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #


def test_devices_and_adapters_become_nodes_but_cables_do_not(home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    assert len(graph.nodes) == len(home_lab.devices) + len(home_lab.adapters)
    assert not any(fqn in graph.nodes for fqn in home_lab.cables)


def test_every_edge_names_two_existing_nodes(campus: Inventory) -> None:
    """The invariant the renderers are allowed to assume."""
    graph = build_graph(campus)
    for edge in graph.edges:
        assert edge.source in graph.nodes, edge.id
        assert edge.target in graph.nodes, edge.id


def test_a_cable_becomes_one_edge_and_an_attachment_becomes_another(
    home_lab: Inventory,
) -> None:
    graph = build_graph(home_lab)
    cables = [edge for edge in graph.edges if edge.kind is EdgeKind.CABLE]
    attachments = [edge for edge in graph.edges if edge.kind is EdgeKind.ATTACHMENT]
    assert len(cables) == len(home_lab.cables)
    # §8.2: `attached_to` is itself a graph edge, and needs no cable document.
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.source == "hosts/laptop"
    assert attachment.target == "hosts/adp-usb-eth"
    assert attachment.target_port == "usb0"


def test_node_order_follows_inventory_load_order(campus: Inventory) -> None:
    expected = [fqn for fqn in campus.elements if fqn not in campus.cables]
    assert list(build_graph(campus).nodes) == expected


def test_an_unresolvable_endpoint_drops_the_edge_and_is_recorded(tmp_path: Path) -> None:
    """``--force`` must still produce a picture, so this cannot raise."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc}\n"
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc:eth0, ghost:eth0], medium: copper}\n"
    )
    graph = build_graph(load_tree(tmp_path))
    assert graph.edges == ()
    assert len(graph.dangling) == 1
    assert "ghost:eth0" in graph.dangling[0]


# --------------------------------------------------------------------------- #
# VLAN membership
# --------------------------------------------------------------------------- #


def test_an_untagged_host_joins_the_vlan_of_the_access_port_it_faces(
    home_lab: Inventory,
) -> None:
    """The host declares no `vlan` block, yet it is in VLAN 10."""
    graph = build_graph(home_lab)
    assert graph.nodes["hosts/pc-desk"].vlans == frozenset({10})


def test_membership_reaches_a_host_through_its_adapter(home_lab: Inventory) -> None:
    """§8.2: collapsing the dongle must not change what the laptop is attached to."""
    graph = build_graph(home_lab)
    assert 10 in graph.nodes["hosts/laptop"].vlans
    assert 10 in graph.nodes["hosts/adp-usb-eth"].vlans


def test_a_trunk_link_carries_the_intersection_of_its_two_ends(campus: Inventory) -> None:
    graph = build_graph(campus)
    uplink = next(
        edge for edge in graph.edges if edge.id == "sites/north/cables/cbl-north-dist-acc01"
    )
    assert uplink.vlans, "a trunk uplink carries VLANs"
    access = graph.nodes["sites/north/access/sw-north-acc-01"]
    assert uplink.vlans <= access.vlans


def test_loopback_addresses_are_kept_but_never_shown(home_lab: Inventory) -> None:
    node = build_graph(home_lab).nodes["hosts/pc-desk"]
    assert "127.0.0.1/8" in node.addresses
    assert "127.0.0.1/8" not in node.routable_addresses
    assert "192.168.10.20/24" in node.routable_addresses


# --------------------------------------------------------------------------- #
# Layer 3: subnets
# --------------------------------------------------------------------------- #


def l3(tmp_path: Path, *documents: str) -> Graph:
    """Load one YAML file holding ``documents`` and build its layer-3 graph."""
    (tmp_path / "inv.yaml").write_text("---\n".join(documents), encoding="utf-8")
    inventory = load_tree(tmp_path)
    assert inventory.errors == [], [str(error) for error in inventory.errors]
    return build_graph(inventory, layer=Layer.L3)


def host(name: str, *interfaces: str, kind: str = "computer") -> str:
    """One element document whose interfaces are given as flow mappings."""
    return (
        f"apiVersion: netgraph.dev/v1alpha1\n"
        f"kind: {kind}\n"
        f"metadata: {{name: {name}}}\n"
        f"spec:\n"
        f"  interfaces: [{', '.join(interfaces)}]\n"
    )


def subnet_of(graph: Graph, prefix: str) -> Node:
    return graph.nodes[f"{SUBNET_ID_PREFIX}{prefix}"]


def memberships(graph: Graph) -> set[tuple[str, str, str, tuple[str, ...]]]:
    """Every edge as ``(element, interface, prefix, addresses)``."""
    return {
        (
            edge.source,
            edge.source_port,
            edge.target.removeprefix(SUBNET_ID_PREFIX),
            edge.addresses,
        )
        for edge in graph.edges
    }


def test_l3_groups_addresses_by_prefix_and_joins_every_holder(tmp_path: Path) -> None:
    graph = l3(
        tmp_path,
        host("pc-a", "{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}"),
        host("pc-b", "{name: eth0, type: ethernet, ipv4: [10.0.0.2/24]}"),
    )
    node = subnet_of(graph, "10.0.0.0/24")
    assert node.is_subnet and node.type is NodeType.SUBNET
    assert node.kind == SUBNET_KIND
    assert node.name == "10.0.0.0/24"
    assert node.element is None and node.ports == ()
    assert node.subnet is not None
    assert node.subnet.elements == ("pc-a", "pc-b")
    assert node.subnet.family == "ipv4"
    assert memberships(graph) == {
        ("pc-a", "eth0", "10.0.0.0/24", ("10.0.0.1/24",)),
        ("pc-b", "eth0", "10.0.0.0/24", ("10.0.0.2/24",)),
    }


def test_l3_puts_a_dual_stacked_interface_in_one_subnet_per_family(tmp_path: Path) -> None:
    graph = l3(
        tmp_path,
        host(
            "rtr",
            "{name: eth0, type: ethernet, ipv4: [10.0.0.1/24], ipv6: [2001:db8::1/64]}",
            kind="router",
        ),
        host(
            "pc",
            "{name: eth0, type: ethernet, ipv4: [10.0.0.2/24], ipv6: [2001:db8::2/64]}",
        ),
    )
    assert [node.name for node in graph.subnet_nodes] == ["10.0.0.0/24", "2001:db8::/64"]
    # One edge per family, both from the same interface.
    assert memberships(graph) >= {
        ("rtr", "eth0", "10.0.0.0/24", ("10.0.0.1/24",)),
        ("rtr", "eth0", "2001:db8::/64", ("2001:db8::1/64",)),
    }


def test_l3_keeps_overlapping_prefixes_apart(tmp_path: Path) -> None:
    """``10.0.0.0/16`` and ``10.0.0.0/24`` are two subnets, not one.

    Grouping is by prefix, so a device in the summary route is *not* drawn as a
    member of the subnet it contains — which is exactly the mistake an operator
    is looking for when the mask is one bit off.
    """
    graph = l3(
        tmp_path,
        host("pc-a", "{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}"),
        host("pc-b", "{name: eth0, type: ethernet, ipv4: [10.0.0.2/16]}"),
        host("pc-c", "{name: eth0, type: ethernet, ipv4: [10.0.0.3/16]}"),
    )
    # Sorted by family, then network address, then prefix length: the two share
    # a network address, so the shorter prefix comes first.
    assert [node.name for node in graph.subnet_nodes] == ["10.0.0.0/16", "10.0.0.0/24"]
    assert subnet_of(graph, "10.0.0.0/24").subnet is not None
    assert subnet_of(graph, "10.0.0.0/16").subnet is not None
    assert subnet_of(graph, "10.0.0.0/24").subnet.elements == ("pc-a",)  # type: ignore[union-attr]
    assert subnet_of(graph, "10.0.0.0/16").subnet.elements == ("pc-b", "pc-c")  # type: ignore[union-attr]


def test_l3_collapses_two_addresses_of_one_interface_in_one_prefix(tmp_path: Path) -> None:
    """A second address on the same port is another label, not another adjacency."""
    graph = l3(
        tmp_path,
        host("pc-a", "{name: eth0, type: ethernet, ipv4: [10.0.0.1/24, 10.0.0.9/24]}"),
        host("pc-b", "{name: eth0, type: ethernet, ipv4: [10.0.0.2/24]}"),
    )
    assert ("pc-a", "eth0", "10.0.0.0/24", ("10.0.0.1/24", "10.0.0.9/24")) in memberships(graph)
    assert len([edge for edge in graph.edges if edge.source == "pc-a"]) == 1


def test_l3_draws_two_interfaces_of_one_element_in_one_prefix_separately(
    tmp_path: Path,
) -> None:
    graph = l3(
        tmp_path,
        host(
            "sw",
            "{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}",
            "{name: eth1, type: ethernet, ipv4: [10.0.0.2/24]}",
            kind="switch",
        ),
    )
    assert memberships(graph) == {
        ("sw", "eth0", "10.0.0.0/24", ("10.0.0.1/24",)),
        ("sw", "eth1", "10.0.0.0/24", ("10.0.0.2/24",)),
    }
    assert subnet_of(graph, "10.0.0.0/24").subnet.elements == ("sw",)  # type: ignore[union-attr]


def test_l3_omits_elements_with_nothing_routable(tmp_path: Path) -> None:
    """A layer-2 switch says nothing at layer 3, so it is left out, not floated."""
    graph = l3(
        tmp_path,
        host("pc", "{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}"),
        host(
            "sw",
            "{name: Gi0/1, type: ethernet, vlan: {mode: access, access_vlan: 10}}",
            kind="switch",
        ),
        host("lo-only", "{name: lo0, type: loopback, ipv4: [127.0.0.1/8], ipv6: ['::1/128']}"),
        host("ll-only", "{name: eth0, type: ethernet, ipv6: [fe80::1/64]}"),
        host("bare", "{name: eth0, type: ethernet, enabled: false}"),
    )
    assert set(graph.nodes) == {"pc", f"{SUBNET_ID_PREFIX}10.0.0.0/24"}
    # The same inventory at layer 1 holds every element.
    assert len(build_graph(load_tree(tmp_path)).nodes) == 5


def test_l3_carries_the_vlan_membership_of_the_physical_graph(home_lab: Inventory) -> None:
    """A host on an untagged access port is still in VLAN 10 at layer 3."""
    graph = build_graph(home_lab, layer=Layer.L3)
    assert graph.nodes["hosts/pc-desk"].vlans == frozenset({10})
    # The prefix inherits the VLANs of the interfaces addressed in it.
    assert subnet_of(graph, "192.168.10.0/24").vlans == frozenset({10})
    assert subnet_of(graph, "203.0.113.0/30").vlans == frozenset()


def test_l3_keeps_the_layer_1_dangling_report(tmp_path: Path) -> None:
    """An unresolved cable costs a host the VLAN it inherits, so it is still reported."""
    graph = l3(
        tmp_path,
        host("pc", "{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}"),
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc:eth0, ghost:eth0], medium: copper}\n",
    )
    assert len(graph.dangling) == 1
    assert graph.edges and all(edge.kind is EdgeKind.SUBNET for edge in graph.edges)


def test_l3_is_deterministic_and_free_of_physical_edges(campus: Inventory) -> None:
    first, second = (build_graph(campus, layer=Layer.L3) for _ in range(2))
    assert list(first.nodes) == list(second.nodes)
    assert [edge.id for edge in first.edges] == [edge.id for edge in second.edges]
    assert all(edge.kind is EdgeKind.SUBNET for edge in first.edges)
    assert all(edge.medium == "" for edge in first.edges)
    # Element nodes come first, in inventory order, then the prefixes.
    assert [node.fqn for node in first.element_nodes] == [
        fqn for fqn in first.nodes if not fqn.startswith(SUBNET_ID_PREFIX)
    ]


def test_a_subnet_node_cannot_collide_with_an_element(campus: Inventory) -> None:
    """The ``subnet:`` prefix uses a character the name grammar forbids."""
    graph = build_graph(campus, layer=Layer.L3)
    for node in graph.subnet_nodes:
        assert node.fqn.startswith(SUBNET_ID_PREFIX)
        assert node.fqn.removeprefix(SUBNET_ID_PREFIX) not in campus.elements


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


def test_an_empty_filter_returns_the_same_graph(campus: Inventory) -> None:
    graph = build_graph(campus)
    assert filter_graph(graph, FilterSpec()) is graph


def test_filters_of_different_kinds_are_combined_with_and(campus: Inventory) -> None:
    graph = build_graph(campus)
    filtered = filter_graph(graph, FilterSpec(namespaces=("sites/north",), kinds=("switch",)))
    assert filtered.nodes
    for node in filtered.nodes.values():
        assert node.kind == "switch"
        assert node.namespace.startswith("sites/north")


def test_a_namespace_filter_includes_descendants(campus: Inventory) -> None:
    filtered = filter_graph(build_graph(campus), FilterSpec(namespaces=("sites/north",)))
    assert "sites/north/core/rtr-north-core-01" in filtered.nodes
    assert "sites/south/core/rtr-south-core-01" not in filtered.nodes


def test_a_name_filter_matches_globs_on_short_and_qualified_names(campus: Inventory) -> None:
    graph = build_graph(campus)
    assert {node.name for node in filter_graph(graph, FilterSpec(names=("sw-*-acc-*",)))} == {
        f"sw-{site}-acc-{index}" for site in ("north", "south", "west") for index in ("01", "02")
    } | {"sw-north-acc-03"}
    by_path = set(filter_graph(graph, FilterSpec(names=("sites/north/hosts/*",))).nodes)
    assert by_path == {fqn for fqn in graph.nodes if fqn.startswith("sites/north/hosts/")}
    assert by_path, "the campus example declares hosts in that namespace"


def test_edges_survive_only_when_both_their_ends_do(campus: Inventory) -> None:
    filtered = filter_graph(build_graph(campus), FilterSpec(kinds=("router",)))
    for edge in filtered.edges:
        assert edge.source in filtered.nodes and edge.target in filtered.nodes


@pytest.mark.parametrize(
    ("depth", "expected"),
    [
        (0, 1),
        # The access switch faces its distribution switch and two hosts.
        (1, 4),
    ],
)
def test_neighbors_of_reaches_exactly_depth_hops(
    campus: Inventory, depth: int, expected: int
) -> None:
    filtered = filter_graph(
        build_graph(campus), FilterSpec(neighbors_of="sw-north-acc-01", depth=depth)
    )
    assert len(filtered.nodes) == expected
    assert "sites/north/access/sw-north-acc-01" in filtered.nodes


def test_neighbors_of_traverses_through_nodes_the_other_filters_drop(
    campus: Inventory,
) -> None:
    """The seed reaches the core router two hops away, through a switch."""
    filtered = filter_graph(
        build_graph(campus),
        FilterSpec(neighbors_of="sw-north-acc-01", depth=2, kinds=("router",)),
    )
    assert set(filtered.nodes) == {"sites/north/core/rtr-north-core-01"}


def test_neighbors_of_an_unknown_element_is_reported(campus: Inventory) -> None:
    with pytest.raises(UnknownElementError):
        filter_graph(build_graph(campus), FilterSpec(neighbors_of="nope"))


def test_a_vlan_filter_keeps_the_elements_in_that_broadcast_domain(campus: Inventory) -> None:
    graph = build_graph(campus)
    filtered = filter_graph(graph, FilterSpec(vlans=frozenset({20})))
    assert filtered.nodes
    for node in filtered.nodes.values():
        assert 20 in node.vlans


# --------------------------------------------------------------------------- #
# Filtering at layer 3
# --------------------------------------------------------------------------- #


def test_a_kind_filter_keeps_the_prefixes_the_kept_elements_route(campus: Inventory) -> None:
    """A subnet is not selected by --kind; it follows whichever elements are."""
    filtered = filter_graph(build_graph(campus, layer=Layer.L3), FilterSpec(kinds=("router",)))
    assert {node.kind for node in filtered.element_nodes} == {"router"}
    assert filtered.subnet_nodes, "the routers are addressed, so their prefixes stay"
    for node in filtered.subnet_nodes:
        assert node.subnet is not None
        assert node.subnet.members, "an empty prefix must be dropped, not drawn"
        assert set(node.subnet.elements) <= set(filtered.nodes)
    for edge in filtered.edges:
        assert edge.source in filtered.nodes and edge.target in filtered.nodes


def test_a_namespace_filter_narrows_a_prefix_to_its_surviving_members(
    campus: Inventory,
) -> None:
    """The backbone /30 joins two sites; keeping one site keeps one member."""
    graph = build_graph(campus, layer=Layer.L3)
    backbone = subnet_of(graph, "198.51.100.0/30")
    assert len(backbone.subnet.elements) == 2  # type: ignore[union-attr]

    filtered = filter_graph(graph, FilterSpec(namespaces=("sites/north",)))
    narrowed = subnet_of(filtered, "198.51.100.0/30")
    assert narrowed.subnet is not None
    assert narrowed.subnet.elements == ("sites/north/core/rtr-north-core-01",)
    # The prefix of a site that is gone entirely goes with it.
    assert f"{SUBNET_ID_PREFIX}10.2.10.0/24" not in filtered.nodes


def test_a_name_filter_that_keeps_nothing_leaves_no_floating_prefixes(
    campus: Inventory,
) -> None:
    filtered = filter_graph(build_graph(campus, layer=Layer.L3), FilterSpec(names=("nothing-*",)))
    assert filtered.is_empty


def test_neighbors_of_at_layer_3_counts_a_prefix_as_a_hop(campus: Inventory) -> None:
    graph = build_graph(campus, layer=Layer.L3)
    seed = "sites/north/hosts/pc-north-01"

    one = filter_graph(graph, FilterSpec(neighbors_of=seed, depth=1))
    assert [node.fqn for node in one.element_nodes] == [seed]
    assert one.subnet_nodes, "depth 1 reaches the prefixes the host is addressed in"

    two = filter_graph(graph, FilterSpec(neighbors_of=seed, depth=2))
    peers = {node.fqn for node in two.element_nodes}
    assert seed in peers
    # The gateway of the host's own VLAN shares one of those prefixes.
    assert "sites/north/distribution/sw-north-dist-01" in peers


def test_neighbors_of_resolves_a_prefix_by_name(campus: Inventory) -> None:
    """A subnet's short name is its prefix, so it can be asked about directly."""
    filtered = filter_graph(
        build_graph(campus, layer=Layer.L3),
        FilterSpec(neighbors_of="10.1.10.0/24", depth=1),
    )
    assert [node.name for node in filtered.subnet_nodes] == ["10.1.10.0/24"]
    assert filtered.element_nodes
    for node in filtered.element_nodes:
        assert node.fqn in filtered.subnet_nodes[0].subnet.elements  # type: ignore[union-attr]


def test_a_vlan_filter_at_layer_3_keeps_the_prefixes_of_that_domain(campus: Inventory) -> None:
    filtered = filter_graph(build_graph(campus, layer=Layer.L3), FilterSpec(vlans=frozenset({20})))
    assert filtered.element_nodes and filtered.subnet_nodes
    for node in filtered.element_nodes:
        assert 20 in node.vlans
    # A routed prefix carries no VLAN, so it survives only through its members.
    for node in filtered.subnet_nodes:
        assert node.subnet is not None and node.subnet.members


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("output_format", ["dot", "mermaid", "json"])
def test_a_text_rendering_is_deterministic(campus: Inventory, output_format: str) -> None:
    graph = build_graph(campus)
    assert render(graph, output_format) == render(build_graph(campus), output_format)


def test_to_dot_and_render_dot_are_the_same_renderer(home_lab: Inventory) -> None:
    """``to_dot``/``to_image`` are canonical; the ``render_*`` names still work."""
    graph = build_graph(home_lab)
    assert to_dot(graph) == render_dot(graph)
    assert to_image is render_image


def test_each_element_kind_gets_its_own_shape(campus: Inventory) -> None:
    """The kind must be readable from the glyph before a label is read."""
    graph = build_graph(campus)
    source = to_dot(graph)
    expected = {
        "router": "diamond",
        "switch": "box3d",
        "computer": "rectangle",
        "server": "cylinder",
        "adapter": "ellipse",
    }
    seen = set()
    for node in graph.element_nodes:
        block = _node_block(source, node.fqn)
        assert f"shape={expected[node.kind]}" in block, node.fqn
        seen.add(node.kind)
    assert seen >= {"router", "switch", "computer", "server"}, "the example got thinner"


def test_an_adapter_is_drawn_provisionally(home_lab: Inventory) -> None:
    """§8.2: an adapter may be collapsed into its host, so it is drawn dashed."""
    source = to_dot(build_graph(home_lab))
    assert 'style="filled,dashed"' in _node_block(source, "hosts/adp-usb-eth")
    assert 'style="filled,dashed"' not in _node_block(source, "hosts/laptop")


def _edge_line(source: str, left: str, right: str) -> str:
    return next(
        line
        for line in source.splitlines()
        if f'"{left}" -- "{right}"' in line or f'"{right}" -- "{left}"' in line
    )


def test_a_links_medium_decides_its_line_style(campus: Inventory) -> None:
    """Solid copper, bold fibre, dashed wireless — and colour repeats it."""
    graph = build_graph(campus)
    source = to_dot(graph)
    styles = {
        edge.medium: re.search(r"style=(\w+)", _edge_line(source, edge.source, edge.target))
        for edge in graph.edges
        if edge.kind is EdgeKind.CABLE
    }
    assert styles["copper"] is not None and styles["copper"].group(1) == "solid"
    assert styles["fiber"] is not None and styles["fiber"].group(1) == "bold"


def test_a_wireless_link_is_dashed(tmp_path: Path) -> None:
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc}\n"
        "spec: {interfaces: [{name: wlan0, type: wifi}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: router\n"
        "metadata: {name: ap}\n"
        "spec: {interfaces: [{name: wlan0, type: wifi}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: air}\n"
        "spec: {endpoints: [pc:wlan0, ap:wlan0], medium: wireless}\n"
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    assert "style=dashed" in _edge_line(to_dot(build_graph(inventory)), "pc", "ap")


def test_link_speed_is_encoded_as_pen_width(campus: Inventory) -> None:
    """A reader must be able to rank two links by rate without reading a label."""
    graph = build_graph(campus)
    source = to_dot(graph)
    widths = {
        edge.speed: float(match.group(1))
        for edge in graph.edges
        if edge.speed is not None
        for match in [re.search(r"penwidth=([\d.]+)", _edge_line(source, edge.source, edge.target))]
        if match is not None
    }
    assert len(widths) > 1, "the example must hold links of differing rates"
    # Faster is never thinner, and the fastest is strictly the widest.
    by_speed = sorted(widths.items())
    assert [width for _, width in by_speed] == sorted(width for _, width in by_speed)
    assert by_speed[0][1] < by_speed[-1][1]


def test_a_link_of_unknown_rate_gets_no_pen_width(tmp_path: Path) -> None:
    """An undeclared rate must not be drawn as though it were the slowest."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-a}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-b}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc-a:eth0, pc-b:eth0], medium: copper}\n"
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    assert "penwidth=" not in _edge_line(to_dot(build_graph(inventory)), "pc-a", "pc-b")


def test_a_sub_gigabit_link_is_the_thinnest_step(tmp_path: Path) -> None:
    """Below the slowest threshold the width is stated, not left to the default."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-a}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: hub\n"
        "metadata: {name: hub-old}\n"
        "spec: {interfaces: [{name: p1, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc-a:eth0, hub-old:p1], medium: copper, speed: 100Mbps}\n"
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    source = to_dot(build_graph(inventory))
    assert "penwidth=1.0" in _edge_line(source, "pc-a", "hub-old")
    # A hub is a box, told apart from a computer by its palette (see _NODE_STYLE).
    assert "shape=box," in _node_block(source, "hub-old")
    assert '"#f0e6d2"' in _node_block(source, "hub-old")


def test_an_interface_with_many_addresses_is_abbreviated(tmp_path: Path) -> None:
    """``max_addresses`` bounds one interface's row, not just a node's list."""
    addresses = ", ".join(f"10.0.0.{index}/24" for index in range(1, 7))
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata: {name: srv}\n"
        f"spec: {{interfaces: [{{name: eth0, type: ethernet, ipv4: [{addresses}]}}]}}\n"
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    label = _node_label(to_dot(build_graph(inventory), RenderOptions(max_addresses=2)), "srv")
    assert "10.0.0.1/24, 10.0.0.2/24, (+4 more)" in label
    assert "10.0.0.5/24" not in label


def test_a_run_of_vlans_is_drawn_as_a_range(tmp_path: Path) -> None:
    """A trunk carrying 10..13 reads as ``10-13``, not as four numbers."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw}\n"
        "spec:\n"
        "  vlans: [{id: 10}, {id: 11}, {id: 12}, {id: 13}, {id: 20}]\n"
        "  interfaces:\n"
        "    - name: uplink\n"
        "      type: ethernet\n"
        "      vlan: {mode: trunk, trunk_vlans: [10, 11, 12, 13, 20]}\n"
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    assert "vlan 10-13,20" in _node_block(to_dot(build_graph(inventory)), "sw")


def test_an_unrunnable_dot_binary_is_reported(
    home_lab: Inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``which`` found it but exec failed — a distinct fault from 'not installed'."""

    def _explodes(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", _explodes)
    with pytest.raises(RenderError, match="could not run"):
        render(build_graph(home_lab), "svg")


def test_an_edge_label_names_the_interface_at_each_end(home_lab: Inventory) -> None:
    source = to_dot(build_graph(home_lab))
    line = _edge_line(source, "routers/rtr-home", "switches/sw-home")
    assert re.search(r'label="lan0 -- port1', line)

    # An attachment has no interface on the host side, so only one end is named
    # rather than a label with a blank half.
    attachment = _edge_line(source, "hosts/laptop", "hosts/adp-usb-eth")
    assert re.search(r'label="usb0', attachment)
    assert " -- usb0" not in attachment.partition("label=")[2]


def test_a_node_label_lists_each_interface_with_its_address_and_vlan(
    home_lab: Inventory,
) -> None:
    block = _node_block(to_dot(build_graph(home_lab)), "routers/rtr-home")
    assert "<B>rtr-home</B>" in block
    assert "[router]" in block
    # One row per interface that has something to say, addresses beside the port
    # that holds them rather than pooled under the node.
    assert re.search(r"<TD[^>]*>lan0</TD><TD[^>]*>[^<]*192\.168\.10\.1/24[^<]*</TD>", block)
    assert re.search(r"<TD[^>]*>wan0</TD><TD[^>]*>203\.0\.113\.2/30</TD>", block)
    assert "vlan 10" in block


def test_a_node_with_many_interfaces_counts_the_rest_off(tmp_path: Path) -> None:
    """A 48-port switch must not push the topology off the page."""
    interfaces = "".join(
        f"    - {{name: port{index}, type: ethernet, ipv4: [10.0.0.{index}/24]}}\n"
        for index in range(1, 21)
    )
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-big}\n"
        "spec:\n"
        "  interfaces:\n" + interfaces
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    block = _node_block(to_dot(build_graph(inventory)), "sw-big")
    assert "(+12 more interfaces)" in block
    assert block.count("<TR>") == 2 + 8 + 1, "two headers, eight ports, one summary"


def test_dot_is_an_undirected_graph_naming_every_node(home_lab: Inventory) -> None:
    source = render_dot(build_graph(home_lab))
    assert source.startswith("graph netgraph {")
    assert source.rstrip().endswith("}")
    assert " -- " in source
    assert "->" not in source
    for fqn in build_graph(home_lab).nodes:
        assert f'"{fqn}"' in source


def test_dot_escapes_quotes_and_newlines_in_a_label(tmp_path: Path) -> None:
    """A description carrying a quote must not end the attribute early."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        'metadata: {name: pc, description: "a \\"quoted\\" name\\nsecond line"}\n'
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
    )
    source = render_dot(build_graph(load_tree(tmp_path)))
    assert r"\"quoted\"" in source
    assert r"\n" in source
    # Every attribute list is still balanced, i.e. nothing escaped the quoting.
    assert source.count('tooltip="') == 1


#: A name carrying every character that can end a DOT token early: the double
#: quote that closes a quoted string, the braces that open and close a subgraph,
#: and a backslash that would otherwise escape whatever followed it.
HOSTILE_NAME = 'pc-"quoted"-{braced}-back\\slash'


def test_a_device_name_carrying_quotes_or_braces_is_refused_by_the_schema(
    tmp_path: Path,
) -> None:
    """First line of defence: such a name never survives loading (§4.1)."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: 'pc-\"quoted\"-{braced}'}\n"
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors, "the element name grammar must reject quotes and braces"
    assert inventory.devices == {}


def _hostile_graph(tmp_path: Path) -> Graph:
    """A two-node graph whose node names are hostile to DOT's tokeniser.

    The names are injected below the loader on purpose. The schema already
    refuses them, so this is the renderer being tested *on its own* — quoting is
    the renderer's job and must not silently depend on an upstream validator
    that a future refactor could move or relax.
    """
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-a}\n"
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-b}\n"
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.2/24]}]\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc-a:eth0, pc-b:eth0], medium: copper}\n"
    )
    loaded = build_graph(load_tree(tmp_path))
    assert len(loaded.nodes) == 2 and len(loaded.edges) == 1

    left, right = loaded.nodes.values()
    left = dataclasses.replace(left, fqn=HOSTILE_NAME, name=HOSTILE_NAME)
    right = dataclasses.replace(right, fqn=f"{HOSTILE_NAME}-2", name=f"{HOSTILE_NAME}-2")
    edge = dataclasses.replace(
        loaded.edges[0],
        source=left.fqn,
        target=right.fqn,
        source_port='eth"0',
        target_port="eth{0}",
    )
    return Graph(
        root=loaded.root,
        nodes={left.fqn: left, right.fqn: right},
        edges=(edge,),
        layer=loaded.layer,
    )


def test_dot_escapes_a_device_named_with_quotes_and_braces(tmp_path: Path) -> None:
    """Quotes are backslash-escaped and braces stay inert inside the quoting."""
    source = render_dot(_hostile_graph(tmp_path))

    # The quote is escaped wherever the name appears: node id, label, both
    # endpoints of the edge, and the port labels.
    assert r"\"quoted\"" in source
    assert r"pc-\"quoted\"" in source
    # The backslash is doubled, and doubled only once, so the escape this
    # renderer introduces is not itself re-escaped.
    assert r"back\\slash" in source
    assert r"back\\\slash" not in source

    for line in source.splitlines():
        if not line.strip().startswith('"'):
            continue
        # Braces inside a quoted string are literal to DOT, so they must be
        # passed through rather than mangled -- but they must never appear
        # outside the quoting, where they would open a subgraph.
        assert _outside_quoted_strings(line).count("{") == 0
        assert _outside_quoted_strings(line).count("}") == 0

    # The braces really did survive, i.e. the assertion above is not passing
    # because the renderer stripped them.
    assert "{braced}" in source


def _outside_quoted_strings(line: str) -> str:
    """``line`` with every DOT quoted string removed, escapes respected."""
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(line):
        char = line[index]
        if in_string and char == "\\":
            index += 2  # skip the escaped character, whatever it is
            continue
        if char == '"':
            in_string = not in_string
        elif not in_string:
            out.append(char)
        index += 1
    assert not in_string, f"unbalanced quoting in {line!r}"
    return "".join(out)


def test_a_tooltip_escapes_the_same_name_the_label_does(tmp_path: Path) -> None:
    """The hover text is a second copy of the name, in the other escaping context.

    A tooltip is a DOT quoted string, so it takes backslash escapes where the
    record label takes entities. ``tests/test_interactive.py`` asserts the rest
    of what a tooltip carries; this is the escaping, next to the label's.
    """
    source = render_dot(_hostile_graph(tmp_path))
    tooltips = re.findall(r'\btooltip="((?:[^"\\]|\\.)*)"', source)
    assert tooltips, "every node and edge carries one"

    hostile = [text for text in tooltips if "quoted" in text]
    assert hostile, "the hostile name reaches the tooltip"
    for text in hostile:
        # Quotes escaped, backslash doubled exactly once, no real newline.
        assert r"\"quoted\"" in text
        assert r"back\\slash" in text
        assert r"back\\\slash" not in text
        assert "\n" not in text


def test_graphviz_parses_a_diagram_whose_device_names_carry_quotes_and_braces(
    tmp_path: Path,
) -> None:
    """The real proof: ``dot`` itself accepts the escaped source.

    Hand-checking the escaping can only confirm what the test author imagined;
    feeding it to Graphviz confirms what Graphviz accepts.
    """
    if shutil.which("dot") is None:
        pytest.skip("the Graphviz 'dot' executable is not installed")
    svg = render_image(_hostile_graph(tmp_path), format="svg").decode("utf-8")
    # Graphviz round-trips the node name into the SVG, XML-escaped.
    assert "quoted" in svg and "braced" in svg


#: Markup that would close the record label's table and open a new element if it
#: reached the HTML-like label unescaped. The record label is a second escaping
#: context beside the DOT quoted string, with entities rather than backslashes.
HTML_HOSTILE = '</TD></TR></TABLE>><B>injected</B><TABLE><TR><TD>a&b"c'


def _html_hostile_graph(tmp_path: Path) -> Graph:
    """A graph whose name, interface and description all carry HTML markup.

    Injected below the loader for the same reason as :func:`_hostile_graph`: the
    escaping is the renderer's own responsibility and must not depend on an
    upstream validator.
    """
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        "metadata: {name: srv}\n"
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
    )
    loaded = build_graph(load_tree(tmp_path))
    (node,) = loaded.nodes.values()
    node = dataclasses.replace(
        node,
        name=HTML_HOSTILE,
        ports=(dataclasses.replace(node.ports[0], name=HTML_HOSTILE),),
    )
    return Graph(root=loaded.root, nodes={node.fqn: node}, edges=(), layer=loaded.layer)


def test_a_record_label_escapes_markup_rather_than_embedding_it(tmp_path: Path) -> None:
    """An inventory name must not be able to inject HTML-like label syntax."""
    source = to_dot(_html_hostile_graph(tmp_path))
    label = _node_label(source, "srv")

    # The dangerous characters survive as entities, so the text is preserved …
    assert "&lt;/TD&gt;&lt;/TR&gt;&lt;/TABLE&gt;" in label
    assert "a&amp;b&#34;c" in label
    # … and never as markup: the label opens and closes exactly one table, and
    # the only <B> is the one this renderer emits for the node's own name.
    assert label.count("<TABLE") == label.count("</TABLE>") == 1
    assert label.count("<B>") == label.count("</B>") == 1
    assert "<B>injected</B>" not in label
    # The label terminator must not appear early either.
    assert source.count(">];") == 1


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz 'dot' is not installed")
def test_graphviz_accepts_a_record_label_full_of_markup(tmp_path: Path) -> None:
    """The real proof: ``dot`` parses it, and without a warning."""
    svg = to_image(_html_hostile_graph(tmp_path), format="svg").decode("utf-8")
    # Graphviz round-trips the text into the SVG rather than acting on it.
    assert "injected" in svg
    assert "<b>injected</b>" not in svg.lower()


def test_mermaid_and_json_also_survive_a_hostile_device_name(tmp_path: Path) -> None:
    """The sibling exporters must not be the weak link."""
    graph = _hostile_graph(tmp_path)

    mermaid = render_mermaid(graph)
    # Mermaid has no escape syntax inside a label, only entities, so the quote
    # becomes &quot;. Braces are deliberately *not* escaped: inside a quoted
    # label they are literal text, and entity-encoding them would put "&#123;"
    # in front of a reader for no reason.
    assert "&quot;quoted&quot;" in mermaid
    assert "{braced}" in mermaid
    # Every label is a balanced quoted string, i.e. nothing ended one early.
    assert mermaid.count('"') % 2 == 0
    for label in re.findall(r'"([^"]*)"', mermaid):
        assert '"' not in label

    payload = json.loads(render_json(graph))
    assert payload["nodes"][0]["name"] == HOSTILE_NAME


def test_show_flags_control_what_a_label_carries(home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    bare = render_dot(graph, RenderOptions(show_ips=False, show_vlans=False))
    full = render_dot(graph, RenderOptions(show_ips=True, show_vlans=True))
    assert "192.168.10.20/24" not in bare
    assert "192.168.10.20/24" in full
    # Assert on the node's own record label, not merely on an edge tooltip:
    # both flags have to reach the interface rows.
    stripped = _node_label(bare, "hosts/pc-desk")
    detailed = _node_label(full, "hosts/pc-desk")
    assert "192.168.10.20/24" not in stripped
    assert "192.168.10.20/24" in detailed
    assert "vlan 10" not in _node_label(bare, "switches/sw-home")
    assert "vlan 10" in _node_label(full, "switches/sw-home")
    # An interface row exists only when it has something to say.
    assert "eno1" not in stripped
    assert "eno1" in detailed
    # The flags are display-only: they never remove a node or an edge.
    assert bare.count(" -- ") == full.count(" -- ")


def test_group_by_namespace_emits_one_cluster_per_namespace(campus: Inventory) -> None:
    graph = build_graph(campus)
    grouped = render_dot(graph, RenderOptions(group_by_namespace=True))
    assert "subgraph cluster_" in grouped
    for namespace in graph.namespaces:
        assert f'label="{namespace}"' in grouped
    assert "subgraph" not in render_dot(graph, RenderOptions(group_by_namespace=False))


def _edge_labels(source: str) -> list[str]:
    """The ``label="..."`` of every edge line in a DOT source."""
    return [
        match.group(1)
        for line in source.splitlines()
        if " -- " in line
        for match in [re.search(r'\blabel="([^"]*)"', line)]
        if match is not None
    ]


def test_the_layer_decides_whether_a_link_is_labelled_physically_or_logically(
    home_lab: Inventory,
) -> None:
    """At l1 a link is labelled with rate and medium, at l2 with its VLANs.

    The physical detail is not lost at l2, it moves to the tooltip — so this
    asserts on the labels rather than on the source as a whole.
    """
    physical = _edge_labels(render_dot(build_graph(home_lab, layer=Layer.L1)))
    logical = _edge_labels(render_dot(build_graph(home_lab, layer=Layer.L2)))

    assert any("1Gbps" in label for label in physical)
    assert not any("1Gbps" in label for label in logical)
    assert any("vlan 10" in label for label in logical)


def test_at_layer_3_the_addresses_move_from_the_nodes_onto_the_edges(
    home_lab: Inventory,
) -> None:
    """Each address is drawn where it says which interface holds it."""
    routed = render_dot(build_graph(home_lab, layer=Layer.L3))
    labels = _edge_labels(routed)
    assert any("192.168.10.20/24" in label for label in labels)
    # The node label of that host no longer repeats its address list.
    assert "192.168.10.20/24" not in _node_label(routed, "hosts/pc-desk")
    assert "eno1" in routed, "the interface is drawn at the element end of the edge"


def _node_block(source: str, fqn: str) -> str:
    """The full statement declaring ``fqn``, HTML-like label included.

    A node is no longer one line: the record label spans a ``<TABLE>``, which is
    the point — a new interface is a one-line diff rather than a rewritten line.
    """
    lines = source.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip().startswith(f'"{fqn}" ['))
    end = next(index for index in range(start, len(lines)) if lines[index].rstrip().endswith("];"))
    return "\n".join(lines[start : end + 1])


def _node_label(source: str, fqn: str) -> str:
    """The record label of ``fqn`` alone — what the diagram actually draws.

    A node statement also carries a tooltip, and the tooltip deliberately says
    more than the label does (:mod:`netgraph.render.details`). A test about what
    is *drawn* has to look at the label, or it passes on the strength of hover
    text nobody can see in a PNG.
    """
    block = _node_block(source, fqn)
    return block.partition("label=<")[2]


def _node_tooltip(source: str, fqn: str) -> str:
    """The ``tooltip="..."`` of ``fqn``, still DOT-escaped."""
    match = re.search(r'\btooltip="((?:[^"\\]|\\.)*)"', _node_block(source, fqn))
    assert match is not None, f"{fqn} carries no tooltip"
    return match.group(1)


def test_dot_draws_a_subnet_in_its_own_shape_and_palette(home_lab: Inventory) -> None:
    graph = build_graph(home_lab, layer=Layer.L3)
    source = render_dot(graph)
    block = _node_block(source, "subnet:192.168.10.0/24")
    assert "[ipv4 subnet]" in block
    assert 'style="filled,rounded"' in block
    # A distinct palette entry: none of the element kinds uses this fill.
    assert '"#e0f2f1"' in block
    for node in graph.element_nodes:
        assert '"#e0f2f1"' not in _node_block(source, node.fqn)
    assert "7 elements" in block, "the tooltip says how populated the prefix is"


def test_mermaid_draws_a_subnet_as_a_rounded_node_of_its_own_class(
    home_lab: Inventory,
) -> None:
    output = render_mermaid(build_graph(home_lab, layer=Layer.L3))
    assert "classDef subnet fill:#e0f2f1" in output
    subnet_line = next(line for line in output.splitlines() if "192.168.10.0/24" in line)
    assert subnet_line.strip().startswith("n")
    assert '("192.168.10.0/24<br/>[ipv4 subnet]' in subnet_line
    # The class assignment names every subnet node, so the styling actually lands.
    graph = build_graph(home_lab, layer=Layer.L3)
    assignment = next(line for line in output.splitlines() if line.strip().endswith(" subnet"))
    assert len(assignment.split()[1].split(",")) == len(graph.subnet_nodes)


def test_json_export_discriminates_subnet_nodes_from_elements(home_lab: Inventory) -> None:
    graph = build_graph(home_lab, layer=Layer.L3)
    document = json.loads(render_json(graph))
    assert document["layer"] == "l3"

    by_type: dict[str, list[dict[str, object]]] = {}
    for node in document["nodes"]:
        by_type.setdefault(str(node["type"]), []).append(node)
    assert len(by_type["subnet"]) == len(graph.subnet_nodes)
    assert len(by_type["element"]) == len(graph.element_nodes)

    subnet = next(node for node in by_type["subnet"] if node["id"] == "subnet:192.168.10.0/24")
    assert subnet["subnet"] == {
        "prefix": "192.168.10.0/24",
        "family": "ipv4",
        "addresses": [
            "192.168.10.30/24",
            "192.168.10.20/24",
            "192.168.10.40/24",
            "192.168.10.10/24",
            "192.168.10.1/24",
            "192.168.10.2/24",
            "192.168.10.3/24",
        ],
        "elements": [
            "hosts/adp-usb-eth",
            "hosts/pc-desk",
            "hosts/phone",
            "hosts/srv-nas",
            "routers/rtr-home",
            "switches/sw-home",
            "wireless/ap-home",
        ],
    }
    assert subnet["interfaces"] == []
    assert all("subnet" not in node for node in by_type["element"])

    edge = next(edge for edge in document["edges"] if edge["id"].startswith("switches/sw-home:"))
    assert edge["kind"] == "subnet"
    assert edge["addresses"] == ["192.168.10.2/24"]
    assert "medium" not in edge, "a membership runs over no medium"


def test_json_export_still_carries_a_medium_below_layer_3(home_lab: Inventory) -> None:
    document = json.loads(render_json(build_graph(home_lab)))
    assert all("medium" in edge for edge in document["edges"])
    assert all(node["type"] == "element" for node in document["nodes"])


def test_mermaid_uses_safe_identifiers_and_entity_escapes(home_lab: Inventory) -> None:
    output = render_mermaid(build_graph(home_lab))
    assert output.startswith("flowchart TB")
    # Fully-qualified names hold '/', which Mermaid cannot use as an identifier.
    assert "hosts/laptop[" not in output
    assert "n0" in output
    assert "<br/>" in output
    assert "classDef switch" in output


def test_mermaid_groups_namespaces_into_subgraphs(campus: Inventory) -> None:
    graph = build_graph(campus)
    grouped = render_mermaid(graph, RenderOptions(group_by_namespace=True))

    assert "subgraph ns" in grouped
    assert grouped.count("end") >= len(graph.namespaces)
    for namespace in graph.namespaces:
        assert f'"{namespace}"' in grouped
    # Every node still appears exactly once, inside its group.
    for identifier in (f"n{index}" for index in range(len(graph.nodes))):
        assert grouped.count(f"    {identifier}[") + grouped.count(f"        {identifier}[") >= 0
    assert "subgraph" not in render_mermaid(graph, RenderOptions(group_by_namespace=False))


def test_mermaid_escapes_a_label_that_would_break_the_parser(tmp_path: Path) -> None:
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        'metadata: {name: pc, description: "a \\"quoted\\" <name> & more"}\n'
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
    )
    output = render_mermaid(build_graph(load_tree(tmp_path)))
    # The description is not drawn, but the node label must still be well formed.
    assert output.count('"') % 2 == 0


def test_mermaid_titles_go_into_front_matter(home_lab: Inventory) -> None:
    output = render_mermaid(build_graph(home_lab), RenderOptions(title="My network"))
    assert output.startswith("---\ntitle: ")
    assert "My network" in output


#: Titles whose punctuation is meaningful to a YAML parser. The front matter is
#: the one part of a Mermaid document read as YAML rather than by the flowchart
#: grammar, so a backslash is an escape character there: ``C:\Users`` reads as
#: the invalid escape ``\U`` and a trailing backslash escapes the closing quote.
FRONT_MATTER_TITLES = [
    r"Site C:\Users\net topology",
    "ends with a backslash \\",
    r"back \ slash",
    r"literal \n here",
    'quotes " and \\ mixed',
    "unicode ümläut 中文 \U0001f4a1",
    "braces { } and [ brackets ]",
]


@pytest.mark.parametrize("title", FRONT_MATTER_TITLES)
def test_mermaid_front_matter_stays_valid_yaml(home_lab: Inventory, title: str) -> None:
    """The title survives the round trip and the block still parses."""
    output = render_mermaid(build_graph(home_lab), RenderOptions(title=title))
    _, front_matter, _ = output.split("---", 2)

    # Parsed the way Mermaid parses it. An unescaped backslash makes this raise.
    assert yaml.safe_load(front_matter) == {"title": title}


def test_mermaid_front_matter_collapses_a_multiline_title(home_lab: Inventory) -> None:
    """A title is one line: the front matter has no room for a block scalar."""
    output = render_mermaid(build_graph(home_lab), RenderOptions(title="first line\nsecond\tline"))
    _, front_matter, _ = output.split("---", 2)
    assert yaml.safe_load(front_matter) == {"title": "first line second line"}


def test_json_export_is_valid_and_carries_the_resolved_topology(home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    document = json.loads(render_json(graph))

    assert document["apiVersion"] == "netgraph.dev/v1alpha1"
    assert document["kind"] == "NetworkGraph"
    assert document["layer"] == "l1"
    assert [node["id"] for node in document["nodes"]] == list(graph.nodes)
    assert len(document["edges"]) == len(graph.edges)

    for edge in document["edges"]:
        # Every endpoint is a fully-qualified name, so a consumer never has to
        # reimplement the namespace-first resolution rules of §2.2.
        for endpoint in edge["endpoints"]:
            assert endpoint["node"] in graph.nodes


def test_json_export_reports_what_it_had_to_drop(tmp_path: Path) -> None:
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc}\n"
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc:eth0, ghost:eth0], medium: copper}\n"
    )
    document = json.loads(render_json(build_graph(load_tree(tmp_path))))
    assert document["dangling"], "a forced export must admit the links it is missing"


def test_render_returns_bytes_for_every_text_format(home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    for output_format in ("dot", "mermaid", "json"):
        payload = render(graph, output_format)
        assert isinstance(payload, bytes)
        assert payload.decode("utf-8")


def test_an_unknown_format_is_refused(home_lab: Inventory) -> None:
    with pytest.raises(RenderError, match="unknown output format"):
        render(build_graph(home_lab), "postscript")


def test_render_image_refuses_a_format_graphviz_does_not_produce(home_lab: Inventory) -> None:
    with pytest.raises(RenderError, match="not a Graphviz image format"):
        render_image(build_graph(home_lab), format="mermaid")


def test_render_text_refuses_a_binary_format(home_lab: Inventory) -> None:
    with pytest.raises(RenderError, match="not a text format"):
        render_text(build_graph(home_lab), "png")


def test_a_missing_graphviz_executable_is_explained(
    home_lab: Inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message has to name the fix; a bare FileNotFoundError does not."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(RenderError, match="Install Graphviz") as caught:
        render(build_graph(home_lab), "svg")
    # It must say what is missing, how to get it, and what to do meanwhile.
    message = str(caught.value)
    assert "'dot' executable was not found" in message
    assert "apt install graphviz" in message
    assert "--format dot" in message


def test_a_failing_layout_reports_what_graphviz_said(
    home_lab: Inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero exit must surface dot's own diagnostic, not just the status."""

    def _fails(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["dot"], returncode=1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(subprocess, "run", _fails)
    with pytest.raises(RenderError, match="Graphviz failed to render svg: boom"):
        render(build_graph(home_lab), "svg")


def test_a_layout_that_exceeds_the_timeout_is_reported_as_such(
    home_lab: Inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung layout must not hang the caller, and must say how to narrow it."""

    def _hangs(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="dot", timeout=120)

    monkeypatch.setattr(subprocess, "run", _hangs)
    with pytest.raises(RenderError, match="did not finish laying out"):
        render(build_graph(home_lab), "svg")


def test_an_empty_layout_is_not_passed_off_as_an_image(
    home_lab: Inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero exit with no output is a failure, not a zero-byte diagram."""

    def _silent(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["dot"], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _silent)
    with pytest.raises(RenderError, match="produced no svg output"):
        render(build_graph(home_lab), "svg")


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz 'dot' is not installed")
@pytest.mark.parametrize("output_format", ["svg", "png", "pdf"])
def test_graphviz_lays_the_example_out(home_lab: Inventory, output_format: str) -> None:
    payload = render(build_graph(home_lab), output_format)
    magic = {"svg": b"<svg", "png": b"\x89PNG", "pdf": b"%PDF"}[output_format]
    assert magic in payload[:1024]


def test_an_empty_graph_still_renders(tmp_path: Path) -> None:
    """A filter that selects nothing produces an empty diagram, not a crash."""
    graph = build_graph(load_tree(tmp_path))
    assert graph.is_empty
    assert render_dot(graph).startswith("graph netgraph {")
    assert render_mermaid(graph).startswith("flowchart TB")
    assert json.loads(render_json(graph))["nodes"] == []


# --------------------------------------------------------------------------- #
# The renderer registry
# --------------------------------------------------------------------------- #


def test_the_canonical_names_are_the_registered_backends(home_lab: Inventory) -> None:
    """``to_mermaid``/``to_json`` are canonical; the ``render_*`` names still work."""
    assert render_mermaid is to_mermaid
    assert render_json is to_json
    graph = build_graph(home_lab)
    assert RENDERERS["mermaid"].to_text is to_mermaid
    assert RENDERERS["json"].to_text is to_json
    assert RENDERERS["dot"].to_text is to_dot
    assert RENDERERS["dot"].text(graph) == to_dot(graph)


def test_the_format_lists_are_derived_from_the_registry() -> None:
    """One source of truth: nothing enumerates formats a second time."""
    assert tuple(RENDERERS) == FORMATS
    assert tuple(name for name, r in RENDERERS.items() if r.is_text) == TEXT_FORMATS
    assert set(TEXT_FORMATS) == {"dot", "html", "mermaid", "json"}


@pytest.mark.parametrize("output_format", FORMATS)
def test_every_registered_format_is_fully_described(output_format: str) -> None:
    """A front end can answer every question it has from the entry alone."""
    renderer = renderer_for(output_format)
    assert renderer.name == output_format
    assert renderer.description
    assert renderer.suffix.startswith(".")
    assert renderer.media_type
    assert suffix_for(output_format) == renderer.suffix
    assert media_type_for(output_format) == renderer.media_type
    assert is_binary_format(output_format) is renderer.binary


def test_suffixes_do_not_collide() -> None:
    """Two formats sharing an extension would overwrite each other's output."""
    suffixes = [renderer.suffix for renderer in RENDERERS.values()]
    assert len(set(suffixes)) == len(suffixes)


def test_only_png_and_pdf_are_binary() -> None:
    """SVG is an image, but it is text on the wire and safe on a terminal."""
    binary = {name for name, renderer in RENDERERS.items() if renderer.binary}
    assert binary == {"png", "pdf"}


@pytest.mark.parametrize("output_format", TEXT_FORMATS)
def test_a_text_format_renders_through_the_registry(
    home_lab: Inventory, output_format: str
) -> None:
    """``render`` is exactly the registered backend, UTF-8 encoded."""
    graph = build_graph(home_lab)
    renderer = renderer_for(output_format)
    text = render_text(graph, output_format)
    assert text == renderer.text(graph)
    assert render(graph, output_format) == text.encode("utf-8")


def test_renderer_for_refuses_an_unknown_format() -> None:
    with pytest.raises(RenderError, match="unknown output format 'postscript'") as caught:
        renderer_for("postscript")
    # The message has to say what *is* available, or it is a dead end.
    for name in FORMATS:
        assert name in str(caught.value)


def test_an_image_format_has_no_text_form(home_lab: Inventory) -> None:
    with pytest.raises(RenderError, match="not a text format") as caught:
        renderer_for("pdf").text(build_graph(home_lab))
    assert "dot, html, mermaid, json" in str(caught.value)


def test_an_unknown_format_is_served_as_a_download() -> None:
    """A media type the server cannot name must not be sniffable by a browser."""
    assert media_type_for("postscript") == DEFAULT_MEDIA_TYPE
    assert DEFAULT_MEDIA_TYPE == "application/octet-stream"


def test_an_unknown_format_has_no_advisories() -> None:
    """Advisories are advisory; reporting the bad format is ``render``'s job."""
    assert advisories_for("postscript", nodes=1, edges=10_000) == ()


@pytest.mark.parametrize("output_format", [name for name in FORMATS if name != "mermaid"])
def test_only_mermaid_has_a_size_limit(output_format: str) -> None:
    assert advisories_for(output_format, nodes=1_000, edges=100_000) == ()


def test_mermaid_advises_only_above_its_edge_limit() -> None:
    """The limit is inclusive: exactly ``MERMAID_MAX_EDGES`` edges still draws."""
    assert advisories_for("mermaid", nodes=10, edges=MERMAID_MAX_EDGES) == ()
    advisories = advisories_for("mermaid", nodes=10, edges=MERMAID_MAX_EDGES + 1)
    assert len(advisories) == 1
    assert f"{MERMAID_MAX_EDGES + 1} edges" in advisories[0]
    assert f"limit of {MERMAID_MAX_EDGES}" in advisories[0]


def test_the_registry_cannot_be_mutated_by_a_caller() -> None:
    """Backends are declared in one module, not registered at a distance."""
    with pytest.raises(TypeError):
        RENDERERS["ascii"] = RENDERERS["dot"]  # type: ignore[index]
