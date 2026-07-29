"""Namespace collapsing and link bundling: the transforms that summarise a graph.

Everything else in the render pipeline answers "show me less of the network".
These two answer "show me all of it, in less space", and that difference is what
the tests here are about: after a collapse or a bundle, every element and every
link must still be *accounted for* — named in an aggregate, or listed in a
bundle — even though the picture no longer draws it. A transform that quietly
dropped a device would produce a diagram that lies, which is worse than one that
is too big to read.

The renderer-facing consequences (a folder shape, a ``weight``, an
``"aggregate"`` node in the JSON) are pinned by the golden files in
``tests/test_golden.py``; what is asserted here is the graph transform itself and
the invariants it promises.
"""

from __future__ import annotations

import json
from ipaddress import ip_network
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result

from netgraph.cli import cli
from netgraph.completion import complete_namespace
from netgraph.errors import count_text
from netgraph.loader import Inventory, load_tree
from netgraph.render import (
    AGGREGATE_ID_PREFIX,
    AGGREGATE_KIND,
    AggregateSpec,
    BundleMode,
    EdgeKind,
    FilterSpec,
    Graph,
    Layer,
    NodeType,
    RenderOptions,
    aggregate_graph,
    build_details,
    build_graph,
    bundle_links,
    collapse_namespaces,
    collapse_targets,
    detail_text,
    element_ids,
    filter_graph,
    graph_to_dict,
    render_text,
    to_dot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def campus() -> Inventory:
    inventory = load_tree(EXAMPLES / "campus")
    assert inventory.errors == []
    return inventory


@pytest.fixture(scope="module")
def lagged() -> Inventory:
    """The fixture tree: one four-member LAG, two spare cross-links beside it."""
    inventory = load_tree(FIXTURES / "aggregate")
    assert inventory.errors == []
    return inventory


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load(root: Path) -> Inventory:
    inventory = load_tree(root)
    assert inventory.errors == [], inventory.errors
    return inventory


# --------------------------------------------------------------------------- #
# Choosing what to collapse
# --------------------------------------------------------------------------- #


def test_depth_is_counted_from_the_shallowest_branching_namespace(campus: Inventory) -> None:
    """``sites/`` holds everything, so it is not a level a reader distinguishes.

    Counting depth from the root instead would make ``--collapse-depth 1`` fold
    the whole campus into one box, which is not an overview of anything.
    """
    graph = build_graph(campus)
    assert collapse_targets(graph, AggregateSpec(collapse_depth=1)) == (
        "sites/north",
        "sites/south",
        "sites/west",
    )


def test_a_deeper_depth_reaches_the_tiers_inside_each_site(campus: Inventory) -> None:
    graph = build_graph(campus)
    targets = collapse_targets(graph, AggregateSpec(collapse_depth=2))
    assert "sites/north/access" in targets
    assert "sites/north" not in targets


def test_a_depth_past_the_deepest_namespace_selects_nothing(campus: Inventory) -> None:
    """Better to collapse nothing and say so than to collapse everything."""
    assert collapse_targets(build_graph(campus), AggregateSpec(collapse_depth=9)) == ()


def test_a_named_namespace_is_taken_as_written(campus: Inventory) -> None:
    graph = build_graph(campus)
    spec = AggregateSpec(collapse=("sites/north/", "/sites/south", ""))
    assert collapse_targets(graph, spec) == ("sites/north", "sites/south")


def test_a_namespace_inside_another_collapsed_one_is_dropped(campus: Inventory) -> None:
    """Collapsing the outer one makes the inner one unobservable, so keeping both
    would only make the result depend on which was applied first."""
    graph = build_graph(campus)
    spec = AggregateSpec(collapse=("sites/north/access", "sites/north"))
    assert collapse_targets(graph, spec) == ("sites/north",)


def test_named_and_derived_namespaces_combine(campus: Inventory) -> None:
    graph = build_graph(campus)
    spec = AggregateSpec(collapse=("sites/north/access",), collapse_depth=2)
    targets = collapse_targets(graph, spec)
    assert targets[0] == "sites/north/access"
    assert "sites/south/hosts" in targets
    # No duplicate, even though the depth derives the same namespace.
    assert targets.count("sites/north/access") == 1


def test_a_tree_with_one_namespace_collapses_to_nothing(tmp_path: Path) -> None:
    """Every namespace *is* the common prefix, so no level is below it."""
    _write(
        tmp_path,
        "only/one.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: computer\n"
        "metadata: {name: pc}\nspec: {interfaces: [{name: eno1, type: ethernet}]}\n",
    )
    graph = build_graph(_load(tmp_path))
    assert collapse_targets(graph, AggregateSpec(collapse_depth=1)) == ()


def test_an_empty_graph_derives_no_namespace(tmp_path: Path) -> None:
    graph = build_graph(_load(tmp_path))
    assert collapse_targets(graph, AggregateSpec(collapse_depth=1)) == ()


# --------------------------------------------------------------------------- #
# Collapsing
# --------------------------------------------------------------------------- #


def test_collapsing_replaces_a_namespace_with_one_node(campus: Inventory) -> None:
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    assert [node.fqn for node in graph.nodes.values()] == [
        f"{AGGREGATE_ID_PREFIX}sites/north",
        f"{AGGREGATE_ID_PREFIX}sites/south",
        f"{AGGREGATE_ID_PREFIX}sites/west",
    ]
    node = graph.nodes[f"{AGGREGATE_ID_PREFIX}sites/north"]
    assert node.kind == AGGREGATE_KIND
    assert node.type is NodeType.AGGREGATE
    assert node.is_aggregate and not node.is_element
    assert node.ports == ()


def test_a_collapsed_node_accounts_for_every_element_it_swallowed(campus: Inventory) -> None:
    """The whole promise of the transform: nothing disappears, it is summarised."""
    before = build_graph(campus)
    after = aggregate_graph(before, AggregateSpec(collapse_depth=1))
    stood_for = [
        element
        for node in after.aggregate_nodes
        for element in node.aggregate.elements  # type: ignore[union-attr]
    ]
    assert sorted(stood_for) == sorted(node.fqn for node in before.element_nodes)


def test_a_collapsed_node_carries_the_census_of_what_is_inside(campus: Inventory) -> None:
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    view = graph.nodes[f"{AGGREGATE_ID_PREFIX}sites/north"].aggregate
    assert view is not None
    assert view.namespace == "sites/north"
    assert view.size == 8
    assert dict(view.by_kind) == {"computer": 2, "router": 1, "server": 1, "switch": 4}
    assert view.summary == "8 elements: 2 computers, 1 router, 1 server, 4 switches"
    # The namespaces it folded in, so a reader can tell a site from a rack.
    assert "sites/north/access" in view.namespaces
    assert view.vlans == frozenset({1, 10, 20, 30, 99})
    assert "10.1.10.0/24" in view.subnets
    # Ordered by family, then network address, then prefix length — the order
    # ``netgraph list subnets`` prints, so the two never disagree.
    networks = [ip_network(prefix) for prefix in view.subnets]
    assert networks == sorted(
        networks, key=lambda net: (net.version, int(net.network_address), net.prefixlen)
    )


def test_a_collapsed_node_sits_in_the_namespace_above_it(campus: Inventory) -> None:
    """So ``--group-by-namespace`` still draws it inside whatever contains it."""
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    assert graph.nodes[f"{AGGREGATE_ID_PREFIX}sites/north"].namespace == "sites"
    assert graph.namespaces == ("sites",)


def test_links_crossing_the_boundary_survive_and_links_inside_it_are_counted(
    campus: Inventory,
) -> None:
    before = build_graph(campus)
    after = aggregate_graph(before, AggregateSpec(collapse_depth=1))

    drawn = {edge.id for edge in after.edges}
    counted = {
        link
        for node in after.aggregate_nodes
        for link in node.aggregate.internal_links  # type: ignore[union-attr]
    }
    # Every link is either drawn or counted, and never both.
    assert drawn | counted == {edge.id for edge in before.edges}
    assert not (drawn & counted)
    assert drawn == {
        "backbone/cbl-bb-north-south",
        "backbone/cbl-bb-south-west",
        "backbone/cbl-bb-west-north",
    }


def test_a_crossing_link_is_re_attached_to_the_collapsed_node(campus: Inventory) -> None:
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse=("sites/north",)))
    backbone = next(edge for edge in graph.edges if edge.id == "backbone/cbl-bb-north-south")
    assert backbone.source == f"{AGGREGATE_ID_PREFIX}sites/north"
    # The far end is untouched, and still names the router it lands on.
    assert backbone.target == "sites/south/core/rtr-south-core-01"
    # The interface survives: which port a site is reached on is a fact about
    # the link, not about the box that was collapsed around it.
    assert backbone.source_port == "xe-0/0/1"


def test_every_edge_endpoint_still_names_a_node(campus: Inventory) -> None:
    """The invariant every renderer is written against."""
    for spec in (AggregateSpec(collapse_depth=1), AggregateSpec(collapse=("sites/north",))):
        graph = aggregate_graph(build_graph(campus), spec)
        for edge in graph.edges:
            assert edge.source in graph.nodes
            assert edge.target in graph.nodes


def test_collapsing_a_namespace_nothing_is_in_changes_nothing(campus: Inventory) -> None:
    graph = build_graph(campus)
    assert collapse_namespaces(graph, ("sites/nowhere",)) is graph


def test_collapsing_leaves_the_uncollapsed_elements_alone(campus: Inventory) -> None:
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse=("sites/north",)))
    assert f"{AGGREGATE_ID_PREFIX}sites/north" in graph.nodes
    assert graph.nodes["sites/south/core/rtr-south-core-01"].is_element
    assert not any(fqn.startswith("sites/north/") for fqn in graph.nodes)


def test_a_subnet_node_is_never_collapsed(campus: Inventory) -> None:
    """A prefix spans whatever namespaces hold it, so it belongs to no site."""
    graph = aggregate_graph(build_graph(campus, layer=Layer.L3), AggregateSpec(collapse_depth=1))
    assert graph.subnet_nodes
    assert all(node.namespace == "" for node in graph.subnet_nodes)
    memberships = [edge for edge in graph.edges if edge.kind is EdgeKind.SUBNET]
    assert memberships
    assert all(edge.source.startswith(AGGREGATE_ID_PREFIX) for edge in memberships)


def test_a_self_link_inside_a_collapsed_namespace_disappears_into_it(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "sites/east/sw.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata: {name: sw}\n"
        "spec:\n  interfaces:\n"
        "    - {name: p1, type: ethernet}\n    - {name: p2, type: ethernet}\n",
    )
    _write(
        tmp_path,
        "sites/west/pc.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: computer\nmetadata: {name: pc}\n"
        "spec: {interfaces: [{name: eno1, type: ethernet}]}\n",
    )
    _write(
        tmp_path,
        "cables/loop.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: cable\nmetadata: {name: cbl-loop}\n"
        "spec: {endpoints: [sw:p1, sw:p2], medium: copper}\n",
    )
    graph = aggregate_graph(build_graph(_load(tmp_path)), AggregateSpec(collapse_depth=1))
    assert graph.edges == ()
    view = graph.nodes[f"{AGGREGATE_ID_PREFIX}sites/east"].aggregate
    assert view is not None and view.internal_links == ("cables/cbl-loop",)


def test_filtering_then_collapsing_summarises_only_what_survived(campus: Inventory) -> None:
    """Order matters: the filter decides what exists, the collapse folds it."""
    graph = filter_graph(build_graph(campus), FilterSpec(kinds=("switch",)))
    collapsed = aggregate_graph(graph, AggregateSpec(collapse_depth=1))
    view = collapsed.nodes[f"{AGGREGATE_ID_PREFIX}sites/north"].aggregate
    assert view is not None
    assert dict(view.by_kind) == {"switch": 4}


# --------------------------------------------------------------------------- #
# Bundling
# --------------------------------------------------------------------------- #


def _pair(graph: Graph, left: str, right: str) -> list[object]:
    ends = {left, right}
    return [edge for edge in graph.edges if {edge.source, edge.target} == ends]


def test_a_declared_lag_bundles_by_default(lagged: Inventory) -> None:
    """The inventory already says the four cables are one logical link."""
    graph = aggregate_graph(build_graph(lagged), AggregateSpec())
    cross = _pair(graph, "sites/east/sw-east-01", "sites/east/sw-east-02")
    bundles = [edge for edge in cross if edge.bundle is not None]  # type: ignore[attr-defined]
    assert len(bundles) == 1
    bundle = bundles[0].bundle  # type: ignore[attr-defined]
    assert bundle.size == 4
    assert bundle.is_aggregation
    assert bundle.aggregate == ("Port-channel1", "Port-channel1")
    assert bundle.summary == "lag, 4 members"
    assert bundle.links == tuple(f"cables/cbl-lag-{n}" for n in range(1, 5))
    # The two spare cross-links are parallel but are not the aggregate, so they
    # stay two edges: nothing in the inventory says they are one link.
    assert len(cross) == 3


def test_a_bundled_lag_names_the_aggregate_interface_and_sums_the_rate(
    lagged: Inventory,
) -> None:
    graph = aggregate_graph(build_graph(lagged), AggregateSpec())
    bundle = next(edge for edge in graph.edges if edge.bundle is not None)
    assert (bundle.source_port, bundle.target_port) == ("Port-channel1", "Port-channel1")
    assert bundle.speed == 4_000_000_000
    assert bundle.speed_text == "4Gbps"
    assert bundle.medium == "copper"
    # A member's own length is not the bundle's; the tooltip lists each one.
    assert bundle.length_m is None
    assert bundle.label is None
    assert bundle.vlans == frozenset({10})


def test_bundle_all_folds_every_parallel_link_and_claims_no_aggregate(
    lagged: Inventory,
) -> None:
    """Six cables, four of them a LAG, are six cables — not a six-member LAG."""
    graph = aggregate_graph(build_graph(lagged), AggregateSpec(bundle=BundleMode.ALL))
    cross = _pair(graph, "sites/east/sw-east-01", "sites/east/sw-east-02")
    assert len(cross) == 1
    bundle = cross[0].bundle  # type: ignore[attr-defined]
    assert bundle.size == 6
    assert not bundle.is_aggregation
    assert bundle.aggregate is None
    assert bundle.summary == "6 links"
    assert (cross[0].source_port, cross[0].target_port) == ("", "")  # type: ignore[attr-defined]


def test_no_bundle_links_draws_every_cable(lagged: Inventory) -> None:
    plain = build_graph(lagged)
    graph = aggregate_graph(plain, AggregateSpec(bundle=BundleMode.NONE))
    assert graph is plain
    assert all(edge.bundle is None for edge in graph.edges)


def test_bundling_a_graph_with_nothing_parallel_returns_it_unchanged(
    campus: Inventory,
) -> None:
    """No aggregate is declared in the campus, so the default must cost nothing."""
    graph = build_graph(campus)
    assert bundle_links(graph, BundleMode.LAG) is graph


def test_bundling_an_edgeless_graph_returns_it_unchanged(tmp_path: Path) -> None:
    graph = build_graph(_load(tmp_path))
    assert bundle_links(graph, BundleMode.ALL) is graph


def test_bundling_is_idempotent(lagged: Inventory) -> None:
    """The second pass of :func:`aggregate_graph` must not bundle the bundles."""
    once = bundle_links(build_graph(lagged), BundleMode.ALL)
    assert bundle_links(once, BundleMode.ALL) is once


def test_collapsing_makes_links_parallel_and_the_second_pass_folds_them(
    lagged: Inventory,
) -> None:
    """The two inter-site cables land on different switches, so they are only
    parallel once each site has become one node."""
    spec = AggregateSpec(collapse_depth=1, bundle=BundleMode.ALL)
    graph = aggregate_graph(build_graph(lagged), spec)
    assert len(graph.edges) == 1
    bundle = graph.edges[0].bundle
    assert bundle is not None
    assert bundle.links == ("cables/cbl-east-west", "cables/cbl-east-west-2")
    # Flattened, never nested: the east-side LAG was folded by the first pass
    # and vanished inside the collapse, so it is not a member here.
    assert all(member.bundle is None for member in bundle.edges)


def test_two_port_channels_between_one_pair_stay_two_edges(tmp_path: Path) -> None:
    """The key includes the aggregate names, so two LAGs are two logical links."""
    ports = "".join(f"    - {{name: e{n}, type: ethernet}}\n" for n in (1, 2, 3, 4))
    for name in ("sw-a", "sw-b"):
        _write(
            tmp_path,
            f"{name}.yaml",
            "apiVersion: netgraph.dev/v1alpha1\nkind: switch\n"
            f"metadata: {{name: {name}}}\nspec:\n  interfaces:\n"
            "    - {name: Po1, type: lag, members: [e1, e2]}\n"
            "    - {name: Po2, type: lag, members: [e3, e4]}\n" + ports,
        )
    _write(
        tmp_path,
        "cables.yaml",
        "---\n".join(
            "apiVersion: netgraph.dev/v1alpha1\nkind: cable\n"
            f"metadata: {{name: cbl-{n}}}\n"
            f"spec: {{endpoints: [sw-a:e{n}, sw-b:e{n}], medium: copper}}\n"
            for n in (1, 2, 3, 4)
        ),
    )
    graph = aggregate_graph(build_graph(_load(tmp_path)), AggregateSpec())
    assert len(graph.edges) == 2
    assert [edge.source_port for edge in graph.edges] == ["Po1", "Po2"]
    assert all(edge.bundle is not None and edge.bundle.size == 2 for edge in graph.edges)


def test_an_aggregate_declared_on_one_end_only_still_bundles(tmp_path: Path) -> None:
    """A one-sided port-channel is a misconfiguration, not a reason to draw four
    cables where the operator sees one."""
    _write(
        tmp_path,
        "sw-a.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata: {name: sw-a}\nspec:\n"
        "  interfaces:\n    - {name: Po1, type: lag, members: [e1, e2]}\n"
        "    - {name: e1, type: ethernet}\n    - {name: e2, type: ethernet}\n",
    )
    _write(
        tmp_path,
        "sw-b.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata: {name: sw-b}\nspec:\n"
        "  interfaces:\n    - {name: e1, type: ethernet}\n    - {name: e2, type: ethernet}\n",
    )
    _write(
        tmp_path,
        "cables.yaml",
        "---\n".join(
            "apiVersion: netgraph.dev/v1alpha1\nkind: cable\n"
            f"metadata: {{name: cbl-{n}}}\n"
            f"spec: {{endpoints: [sw-a:e{n}, sw-b:e{n}], medium: copper}}\n"
            for n in (1, 2)
        ),
    )
    graph = aggregate_graph(build_graph(_load(tmp_path)), AggregateSpec())
    assert len(graph.edges) == 1
    bundle = graph.edges[0].bundle
    assert bundle is not None and bundle.aggregate == ("Po1", "")


def test_a_mixed_bundle_reports_the_most_physical_kind_and_no_medium(
    tmp_path: Path,
) -> None:
    """Three cables and a tunnel are a link a technician can unplug, and a
    tunnel runs over no medium, so the bundle claims none."""
    for name in ("sw-a", "sw-b"):
        _write(
            tmp_path,
            f"{name}.yaml",
            "apiVersion: netgraph.dev/v1alpha1\nkind: router\n"
            f"metadata: {{name: {name}}}\nspec:\n  interfaces:\n"
            "    - {name: e1, type: ethernet, ipv4: {addresses: [10.0.0.%s/30]}}\n"
            "    - {name: wg0, type: tunnel, parent: e1}\n" % ("1" if name == "sw-a" else "2"),
        )
    _write(
        tmp_path,
        "links.yaml",
        "apiVersion: netgraph.dev/v1alpha1\nkind: cable\nmetadata: {name: cbl-1}\n"
        "spec: {endpoints: [sw-a:e1, sw-b:e1], medium: copper, speed: 1Gbps}\n"
        "---\napiVersion: netgraph.dev/v1alpha1\nkind: tunnel\nmetadata: {name: vpn}\n"
        "spec:\n  type: wireguard\n  endpoints: [sw-a:wg0, sw-b:wg0]\n",
    )
    graph = aggregate_graph(build_graph(_load(tmp_path)), AggregateSpec(bundle=BundleMode.ALL))
    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert edge.kind is EdgeKind.CABLE
    assert edge.medium == ""
    # The tunnel declares no rate, so it contributes nothing rather than zeroing
    # the sum of what the cable does declare.
    assert edge.speed == 1_000_000_000
    assert edge.bundle is not None and edge.bundle.size == 2
    # A tunnel folded into a bundle is still a tunnel the graph draws.
    assert [view.name for view in graph.tunnels] == ["vpn"]


# --------------------------------------------------------------------------- #
# The specification itself
# --------------------------------------------------------------------------- #


def test_the_default_specification_is_not_empty() -> None:
    """LAG bundling is on by default, and it changes graphs that declare one."""
    assert not AggregateSpec().is_empty
    assert AggregateSpec(bundle=BundleMode.NONE).is_empty
    assert not AggregateSpec(bundle=BundleMode.NONE, collapse_depth=1).is_empty


def test_a_specification_describes_itself() -> None:
    spec = AggregateSpec(collapse=("sites/north",), collapse_depth=2, bundle=BundleMode.ALL)
    assert spec.describe() == "collapse=sites/north, collapse-depth=2, bundle=all"
    assert AggregateSpec().describe() == "bundle=lag"
    assert str(BundleMode.LAG) == "lag"


def test_no_specification_at_all_leaves_the_graph_alone(campus: Inventory) -> None:
    graph = build_graph(campus)
    assert aggregate_graph(graph, None) is graph
    assert aggregate_graph(graph) is graph


def test_a_long_kind_census_is_bounded(campus: Inventory) -> None:
    """A label listing every kind an inventory can hold would not fit on one."""
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    view = graph.nodes[f"{AGGREGATE_ID_PREFIX}sites/north"].aggregate
    assert view is not None
    stretched = type(view)(
        namespace=view.namespace,
        elements=view.elements,
        by_kind={f"kind{index}": 1 for index in range(9)},
    )
    assert stretched.kind_text.endswith("(+3 more kinds)")
    assert stretched.summary.startswith("8 elements: 1 kind0")


def test_an_empty_namespace_summary_says_only_its_size() -> None:
    from netgraph.render.aggregate import AggregateView

    assert AggregateView(namespace="sites/north").summary == "0 elements"


# --------------------------------------------------------------------------- #
# What the renderers make of it
# --------------------------------------------------------------------------- #


def test_a_collapsed_node_is_as_addressable_as_a_real_one(campus: Inventory) -> None:
    """``--element-ids`` and the tooltip are the whole interface between a
    diagram and anything that inspects it, so a summary must carry both."""
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    identity = element_ids(graph)
    assert identity.node(f"{AGGREGATE_ID_PREFIX}sites/north") == "node-ns_sites_north"

    source = to_dot(graph, RenderOptions(element_ids=True))
    assert 'id="node-ns_sites_north"' in source
    assert "shape=folder" in source
    assert "collapsed namespace sites/north: 8 elements" in source
    assert "7 links inside, not drawn" in source
    # It stands for elements nobody wrote as one document, so it links nowhere.
    assert 'URL="' not in source


def test_a_bundled_edge_pulls_its_endpoints_together(lagged: Inventory) -> None:
    """Graphviz ``weight``: four cables should pull four times as hard as one."""
    graph = aggregate_graph(build_graph(lagged), AggregateSpec())
    source = to_dot(graph)
    assert "weight=4" in source
    assert "lag, 4 members" in source
    assert "cables/cbl-lag-4" in source  # every member named in the tooltip


def test_the_json_export_marks_an_aggregate_and_lists_what_it_stands_for(
    campus: Inventory,
) -> None:
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    document = graph_to_dict(graph)
    node = next(node for node in document["nodes"] if node["id"] == "ns:sites/north")
    assert node["type"] == "aggregate"
    assert node["kind"] == AGGREGATE_KIND
    summary = node["aggregate"]
    assert summary["elementCount"] == 8
    assert "sites/north/core/rtr-north-core-01" in summary["elements"]
    assert summary["countsByKind"] == {"computer": 2, "router": 1, "server": 1, "switch": 4}
    assert len(summary["internalLinks"]) == 7
    assert summary["namespaces"] == [
        "sites/north/access",
        "sites/north/core",
        "sites/north/distribution",
        "sites/north/hosts",
    ]


def test_the_json_export_marks_a_bundle_and_exports_its_members_in_full(
    lagged: Inventory,
) -> None:
    graph = aggregate_graph(build_graph(lagged), AggregateSpec())
    document = graph_to_dict(graph)
    edge = next(edge for edge in document["edges"] if "bundle" in edge)
    bundle = edge["bundle"]
    assert bundle["size"] == 4
    assert [link["id"] for link in bundle["links"]] == [f"cables/cbl-lag-{n}" for n in range(1, 5)]
    assert bundle["aggregate"] == [
        {"node": "sites/east/sw-east-01", "interface": "Port-channel1"},
        {"node": "sites/east/sw-east-02", "interface": "Port-channel1"},
    ]
    # A member carries its own length and label, which the bundle cannot.
    assert bundle["links"][0]["lengthM"] == 2.0


def test_mermaid_draws_a_collapsed_namespace_as_its_own_shape(campus: Inventory) -> None:
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    text = render_text(graph, "mermaid")
    assert 'n0[/"sites/north<br/>[namespace]' in text
    assert "classDef namespace" in text
    assert "7 links inside" in text


def test_the_display_options_reach_a_collapsed_label(campus: Inventory) -> None:
    """``--no-show-ips`` has to mean all of the printing, or the flag is a trap."""
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    bare = RenderOptions(show_ips=False, show_vlans=False)
    for format in ("dot", "mermaid"):
        text = render_text(graph, format, bare)
        assert "10.1.10.0/24" not in text
        assert "vlan 1,10,20,30,99" not in text
        assert "vlans: 1,10,20,30,99" not in text


def test_a_namespace_with_nothing_inside_it_says_nothing_about_links(
    lagged: Inventory,
) -> None:
    """One element in a namespace has nothing to cable to, so the summary must
    not print ``0 links inside``."""
    graph = aggregate_graph(build_graph(lagged), AggregateSpec(collapse=("sites/west",)))
    record = next(
        detail
        for detail in build_details(graph).values()
        if detail.get("id") == f"{AGGREGATE_ID_PREFIX}sites/west"
    )
    text = detail_text(record)
    assert "collapsed namespace sites/west: 1 element: 1 switch" in text
    assert "inside, not drawn" not in text
    assert "elements: sites/west/sw-west-01" in text
    # Few enough subnets to list in full, in Mermaid as well as in the tooltip.
    assert "(+" not in render_text(graph, "mermaid")


def test_a_long_subnet_list_is_counted_off_in_mermaid(campus: Inventory) -> None:
    """Mermaid has no tooltip to move the tail to, so the label has to say how
    much it left out."""
    graph = aggregate_graph(build_graph(campus), AggregateSpec(collapse_depth=1))
    text = render_text(graph, "mermaid", RenderOptions(max_addresses=2))
    assert "(+11 more)" in text


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def test_collapse_depth_draws_the_site_level_overview() -> None:
    result = _invoke(
        "-i", str(EXAMPLES / "campus"), "render", "-f", "json", "--collapse-depth", "1"
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [node["id"] for node in payload["nodes"]] == [
        "ns:sites/north",
        "ns:sites/south",
        "ns:sites/west",
    ]
    assert len(payload["edges"]) == 3
    assert "rendered 3 node(s) and 3 edge(s)" in result.stderr


def test_collapse_takes_a_namespace_by_name_and_repeats() -> None:
    result = _invoke(
        "-i",
        str(EXAMPLES / "campus"),
        "render",
        "-f",
        "json",
        "--collapse",
        "sites/north",
        "--collapse",
        "sites/south",
    )
    assert result.exit_code == 0
    ids = {node["id"] for node in json.loads(result.stdout)["nodes"]}
    assert {"ns:sites/north", "ns:sites/south"} <= ids
    assert "sites/west/core/rtr-west-core-01" in ids


def test_collapsing_a_namespace_that_is_not_there_is_reported() -> None:
    """Silence would be indistinguishable from a namespace that is simply small."""
    result = _invoke(
        "-i", str(EXAMPLES / "campus"), "render", "-f", "json", "--collapse", "sites/nowhere"
    )
    assert result.exit_code == 0
    assert "nothing was collapsed" in result.stderr


def test_a_depth_that_matches_no_namespace_is_reported() -> None:
    result = _invoke(
        "-i", str(EXAMPLES / "campus"), "render", "-f", "json", "--collapse-depth", "9"
    )
    assert result.exit_code == 0
    assert "no namespace is 9 level(s) below" in result.stderr


def test_one_collapse_inside_another_is_reported() -> None:
    result = _invoke(
        "-i",
        str(EXAMPLES / "campus"),
        "render",
        "-f",
        "json",
        "--collapse",
        "sites/north",
        "--collapse",
        "sites/north/access",
    )
    assert result.exit_code == 0
    assert "--collapse 'sites/north/access' folded nothing" in result.stderr


@pytest.mark.parametrize(
    ("flags", "edges"),
    [
        ((), 6),
        (("--bundle-links",), 4),
        (("--no-bundle-links",), 9),
    ],
)
def test_the_bundling_flags_decide_how_many_edges_are_drawn(
    flags: tuple[str, ...], edges: int
) -> None:
    result = _invoke("-i", str(FIXTURES / "aggregate"), "render", "-f", "json", *flags)
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)["edges"]) == edges


def test_the_two_transforms_compose_on_the_command_line() -> None:
    result = _invoke(
        "-i",
        str(FIXTURES / "aggregate"),
        "render",
        "-f",
        "json",
        "--collapse-depth",
        "1",
        "--bundle-links",
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["nodes"]) == 2
    assert len(payload["edges"]) == 1
    assert payload["edges"][0]["bundle"]["size"] == 2


def test_a_filter_and_a_collapse_compose_on_the_command_line() -> None:
    result = _invoke(
        "-i",
        str(EXAMPLES / "campus"),
        "render",
        "-f",
        "json",
        "--kind",
        "router",
        "--collapse-depth",
        "1",
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert all(node["aggregate"]["countsByKind"] == {"router": 1} for node in payload["nodes"])


def test_render_help_documents_both_transforms() -> None:
    result = _invoke("render", "--help")
    assert result.exit_code == 0
    for flag in ("--collapse ", "--collapse-depth", "--bundle-links / --no-bundle-links"):
        assert flag in result.output


def test_namespaces_are_offered_for_completion() -> None:
    """``--collapse sites/<TAB>`` is the affordance that makes the flag usable."""
    context = click.Context(cli, info_name="netgraph")
    context.params["inventory"] = EXAMPLES / "campus"
    offered = [
        item.value for item in complete_namespace(context, click.Option(["--collapse"]), "sites/no")
    ]
    assert offered[0] == "sites/north"
    assert set(offered) == {"sites/north"} | {
        f"sites/north/{tier}" for tier in ("access", "core", "distribution", "hosts")
    }

    every = complete_namespace(context, click.Option(["--collapse"]), "")
    # Outermost first, and ``sites`` is offered even though no element sits
    # directly in it: the flag matches a namespace and everything below it.
    assert every[0].value == "sites"
    assert every[0].help == "22 elements"
    assert "sites/north/access" in {item.value for item in every}


# --------------------------------------------------------------------------- #
# The shared counting helper this work needed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("number", "noun", "expected"),
    [
        (1, "switch", "1 switch"),
        (4, "switch", "4 switches"),
        (2, "element", "2 elements"),
        (0, "link", "0 links"),
        (3, "box", "3 boxes"),
        (2, "dish", "2 dishes"),
    ],
)
def test_counting_pluralises_a_sibilant_noun(number: int, noun: str, expected: str) -> None:
    assert count_text(number, noun) == expected


def test_counting_accepts_an_irregular_plural() -> None:
    assert count_text(2, "address", "addresses") == "2 addresses"
    assert count_text(1, "address", "addresses") == "1 address"
