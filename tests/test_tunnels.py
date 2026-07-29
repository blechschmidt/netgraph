"""Tunnels: the model, the resolution, the four views and the ten rules.

The other test modules cover tunnels where they touch what was already there —
a golden rendering, an invalid fixture, the completion list. This one covers
what is new and has no home elsewhere:

* the per-type facts of :class:`~netgraph.models.TunnelType`, which are what the
  renderer and the validator both reason about rather than about a string;
* :func:`~netgraph.render.graph.resolve_tunnels`, and in particular the broken
  inventories it must survive because ``--force`` exists;
* the shape a tunnel takes in a diagram — an edge when it joins two elements, a
  node when it joins more or when the overlay layer is asked for;
* nesting, end to end: ``vxlan over ipsec`` through the model, the stack, the
  MTU budget and the "is anything encrypted here?" question.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from netgraph.errors import SchemaError
from netgraph.graph import LINK_EDGE_KINDS, PHYSICAL_EDGE_KINDS, broadcast_domains, to_networkx
from netgraph.loader import Inventory, load_tree
from netgraph.models import (
    API_VERSION,
    MAX_VNI,
    InterfaceType,
    Tunnel,
    TunnelAuth,
    TunnelMode,
    TunnelTransport,
    TunnelType,
    parse_document,
)
from netgraph.render.graph import (
    TUNNEL_ID_PREFIX,
    EdgeKind,
    FilterSpec,
    Layer,
    NodeType,
    build_graph,
    filter_graph,
    resolve_tunnels,
)
from netgraph.validate import validate

# --------------------------------------------------------------------------- #
# Building inventories
# --------------------------------------------------------------------------- #

#: Two routers joined by one cable, each with the tunnel interfaces the tests
#: below terminate their tunnels on. Deliberately not minimal: giving every
#: router the same port set means a test can add any tunnel without editing it.
_ROUTERS = """
apiVersion: netgraph.dev/v1alpha1
kind: router
metadata: {{name: {name}}}
spec:
  interfaces:
    - {{name: eth0, type: ethernet, mtu: 1500, ipv4: [{wan}]}}
    - {{name: wg0, type: tunnel, mtu: 1420, parent: eth0, ipv4: [{wg}]}}
    - {{name: ipsec0, type: tunnel, mtu: 1427, parent: eth0, ipv4: [{ipsec}]}}
    - {{name: vx1, type: tunnel, mtu: 1377}}
"""


def routers(*names: str) -> str:
    """One router document per name, addressed so no two of them collide."""
    return "---\n".join(
        _ROUTERS.format(
            name=name,
            wan=f"198.51.100.{index + 1}/24",
            wg=f"10.9.0.{index + 1}/24",
            ipsec=f"10.8.0.{index + 1}/24",
        )
        for index, name in enumerate(names)
    )


def cable(name: str, left: str, right: str) -> str:
    return f"""
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {{name: {name}}}
spec:
  endpoints: [{left}, {right}]
  medium: copper
"""


def tunnel_document(name: str, **spec: Any) -> dict[str, Any]:
    """A ``tunnel`` document from a spec written as keyword arguments."""
    return {
        "apiVersion": API_VERSION,
        "kind": "tunnel",
        "metadata": {"name": name},
        "spec": spec,
    }


def tunnel(name: str, **spec: Any) -> str:
    """The same document, as the YAML text an inventory file would hold."""
    return yaml.safe_dump(tunnel_document(name, **spec), sort_keys=False)


def inventory_of(root: Path, *documents: str) -> Inventory:
    """Load an inventory made of the given YAML documents, insisting it parses."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "net.yaml").write_text("---\n".join(documents), encoding="utf-8")
    loaded = load_tree(root)
    assert loaded.errors == [], "\n".join(str(error) for error in loaded.errors)
    return loaded


def parsed(name: str = "t", **spec: Any) -> Tunnel:
    """Parse one tunnel document, or raise :class:`SchemaError`."""
    element = parse_document(tunnel_document(name, **spec))
    assert isinstance(element, Tunnel)
    return element


def issues(exc: pytest.ExceptionInfo[SchemaError]) -> list[tuple[str | None, str]]:
    """``(rule, last path component)`` per issue, which is what a test asserts on."""
    return [(issue.rule, str(issue.path[-1]) if issue.path else "") for issue in exc.value.issues]


@pytest.fixture
def nested(tmp_path: Path) -> Inventory:
    """VXLAN over IPsec between two routers, plus a WireGuard tunnel beside it."""
    return inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("wg", type="wireguard", endpoints=["rtr-a:wg0", "rtr-b:wg0"], mtu=1420),
        tunnel("ipsec", type="ipsec", endpoints=["rtr-a:ipsec0", "rtr-b:ipsec0"], mtu=1427),
        tunnel(
            "vx",
            type="vxlan",
            vni=100,
            endpoints=["rtr-a:vx1", "rtr-b:vx1"],
            over="ipsec",
            mtu=1377,
        ),
    )


# --------------------------------------------------------------------------- #
# The type table
# --------------------------------------------------------------------------- #


def test_every_tunnel_type_states_the_five_facts_the_rest_of_the_tool_uses() -> None:
    """§14.1: the table is the reason ``type`` is an enum and not free text."""
    for member in TunnelType:
        assert member.layer in (2, 3)
        assert isinstance(member.transport, TunnelTransport)
        assert member.overhead_bytes > 0
        assert member.iana_if_type == "ianaift:tunnel"
        # A transport with no port must not claim one, and one with a port must.
        assert (member.default_port is None) != member.transport.has_port


@pytest.mark.parametrize(
    "type_name,layer,port,encrypts",
    [
        ("wireguard", 3, 51820, True),
        ("ipsec", 3, None, True),
        ("openvpn", 3, 1194, True),
        # PPTP's MPPE is broken, so netgraph calls it what it is.
        ("pptp", 3, None, False),
        ("l2tp", 2, 1701, False),
        ("gre", 3, None, False),
        ("vxlan", 2, 4789, False),
        ("geneve", 2, 6081, False),
    ],
)
def test_the_per_type_facts_are_the_published_ones(
    type_name: str, layer: int, port: int | None, encrypts: bool
) -> None:
    member = TunnelType(type_name)
    assert (member.layer, member.default_port, member.encrypts) == (layer, port, encrypts)


def test_a_tunnel_interface_is_not_cableable() -> None:
    """``NG-C009`` and ``NG-T003`` are complementary: a port takes one or the other."""
    assert not InterfaceType.TUNNEL.is_cableable
    assert InterfaceType.TUNNEL.iana_if_type == "ianaift:tunnel"


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


def test_the_type_materialises_the_port_the_mode_and_the_encryption() -> None:
    """§1: a loaded document states its defaults rather than implying them."""
    wireguard = parsed(type="wireguard", endpoints=["a:wg0", "b:wg0"])
    assert (wireguard.spec.port, wireguard.spec.mode, wireguard.spec.encrypted) == (
        51820,
        None,
        True,
    )

    ipsec = parsed(type="ipsec", endpoints=["a:ipsec0", "b:ipsec0"])
    assert (ipsec.spec.port, ipsec.spec.mode, ipsec.spec.encrypted) == (
        None,
        TunnelMode.TUNNEL,
        True,
    )


def test_endpoints_are_sorted_because_a_tunnel_is_undirected() -> None:
    parsed_tunnel = parsed(type="wireguard", endpoints=["z:wg0", "a:wg0"])
    assert [str(ref) for ref in parsed_tunnel.endpoints] == ["a:wg0", "z:wg0"]


def test_a_third_endpoint_makes_the_tunnel_multipoint() -> None:
    point_to_point = parsed(type="wireguard", endpoints=["a:wg0", "b:wg0"])
    mesh = parsed(type="wireguard", endpoints=["a:wg0", "b:wg0", "c:wg0"])
    assert not point_to_point.is_multipoint
    assert mesh.is_multipoint
    assert [str(ref) for ref in mesh.other_ends(mesh.endpoints[0])] == ["b:wg0", "c:wg0"]


def test_other_ends_refuses_an_endpoint_that_is_not_one() -> None:
    mesh = parsed(type="wireguard", endpoints=["a:wg0", "b:wg0"])
    with pytest.raises(KeyError, match="is not an endpoint"):
        mesh.other_ends(parsed(type="gre", endpoints=["x:gre0", "y:gre0"]).endpoints[0])


@pytest.mark.parametrize(
    "spec,rule,field",
    [
        # NG-T001: two endpoints at least, and no repeats.
        ({"type": "gre", "endpoints": ["a:gre0"]}, "NG-T001", "endpoints"),
        ({"type": "gre", "endpoints": ["a:gre0", "a:gre0"]}, "NG-T001", "1"),
        # NG-T007: the VNI belongs to VXLAN and Geneve, and to nothing else.
        ({"type": "vxlan", "endpoints": ["a:vx0", "b:vx0"]}, "NG-T007", "vni"),
        ({"type": "geneve", "endpoints": ["a:gv0", "b:gv0"]}, "NG-T007", "vni"),
        ({"type": "gre", "endpoints": ["a:g", "b:g"], "vni": 1}, "NG-T007", "vni"),
        # NG-T008: mode is IPsec's, and a port needs a transport that has one.
        ({"type": "gre", "endpoints": ["a:g", "b:g"], "mode": "tunnel"}, "NG-T008", "mode"),
        ({"type": "gre", "endpoints": ["a:g", "b:g"], "port": 47}, "NG-T008", "port"),
        ({"type": "ipsec", "endpoints": ["a:i", "b:i"], "port": 500}, "NG-T008", "port"),
        # NG-T009: nothing to describe on a tunnel that protects nothing.
        ({"type": "gre", "endpoints": ["a:g", "b:g"], "cipher": "aes"}, "NG-T009", "cipher"),
        ({"type": "gre", "endpoints": ["a:g", "b:g"], "auth": "psk"}, "NG-T009", "auth"),
    ],
)
def test_a_field_that_makes_no_sense_for_the_type_is_refused(
    spec: dict[str, Any], rule: str, field: str
) -> None:
    with pytest.raises(SchemaError) as exc:
        parsed(**spec)
    assert (rule, field) in issues(exc)


def test_declaring_a_cleartext_tunnel_encrypted_unlocks_the_cipher_fields() -> None:
    """A deployment that protects GRE some other way must be able to say so."""
    protected = parsed(
        type="gre",
        endpoints=["a:gre0", "b:gre0"],
        encrypted=True,
        cipher="aes-256-gcm",
        auth="certificate",
    )
    assert protected.encrypts
    assert protected.spec.auth is TunnelAuth.CERTIFICATE


@pytest.mark.parametrize(
    "key", ["private_key", "preshared_key", "psk", "password", "secret", "passphrase"]
)
def test_key_material_is_refused_by_name_rather_than_as_an_unknown_key(key: str) -> None:
    """§14.2: ``NG-T010`` exists so the message explains *why*, not just *that*."""
    with pytest.raises(SchemaError) as exc:
        parsed(type="wireguard", endpoints=["a:wg0", "b:wg0"], **{key: "hunter2"})
    (issue,) = exc.value.issues
    assert issue.rule == "NG-T010"
    assert "never stores secrets" in issue.message


def test_the_vni_is_a_24_bit_field() -> None:
    assert parsed(type="vxlan", vni=MAX_VNI, endpoints=["a:v", "b:v"]).spec.vni == MAX_VNI
    with pytest.raises(SchemaError):
        parsed(type="vxlan", vni=MAX_VNI + 1, endpoints=["a:v", "b:v"])


def test_a_tunnel_interface_may_name_its_underlay_port_and_nothing_else_may() -> None:
    """``NG-I002``: ``parent`` is ``if:lower-layer-if``, not a free-form pointer."""
    document = yaml.safe_load("""
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata: {name: pc}
spec:
  interfaces:
    - {name: eth0, type: ethernet}
    - {name: wg0, type: tunnel, parent: eth0}
""")
    parse_document(document)

    document["spec"]["interfaces"][1] = {"name": "eth1", "type": "ethernet", "parent": "eth0"}
    with pytest.raises(SchemaError) as exc:
        parse_document(document)
    assert ("NG-I002", "parent") in issues(exc)


def test_a_tunnel_interface_must_not_be_its_own_parent() -> None:
    document = yaml.safe_load("""
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata: {name: pc}
spec:
  interfaces:
    - {name: wg0, type: tunnel, parent: wg0}
""")
    with pytest.raises(SchemaError) as exc:
        parse_document(document)
    assert ("NG-I002", "parent") in issues(exc)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def test_resolution_gives_every_tunnel_its_stack_and_its_protector(
    nested: Inventory,
) -> None:
    views = {view.fqn: view for view in resolve_tunnels(nested)[0]}
    assert set(views) == {"wg", "ipsec", "vx"}

    assert views["ipsec"].stack == ("ipsec",)
    assert views["ipsec"].depth == 0
    assert views["ipsec"].encrypted_by is None

    inner = views["vx"]
    assert inner.stack == ("vxlan", "ipsec")
    assert inner.stack_text == "vxlan over ipsec"
    assert inner.depth == 1
    assert inner.over == "ipsec"
    # VXLAN encrypts nothing; the IPsec tunnel under it does.
    assert not inner.encrypted
    assert inner.protected
    assert inner.encrypted_by == "ipsec"
    # The whole stack costs its MTU, not just the outermost header.
    assert inner.overhead_bytes == TunnelType.VXLAN.overhead_bytes + TunnelType.IPSEC.overhead_bytes
    assert "vni 100" in inner.summary
    assert "encrypted underlay" in inner.summary


def test_a_tunnel_with_an_unresolvable_endpoint_is_dropped_and_reported(
    tmp_path: Path,
) -> None:
    """``--force`` renders a broken inventory, so the graph layer may not raise."""
    loaded = inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("half", type="wireguard", endpoints=["rtr-a:wg0", "ghost:wg0"]),
        tunnel("bad-port", type="wireguard", endpoints=["rtr-a:ipsec0", "rtr-b:nosuch"]),
    )
    views, dangling = resolve_tunnels(loaded)

    # 'half' loses one of two endpoints and cannot be drawn; 'bad-port' loses
    # one of two as well, for a different reason. Both are reported by name.
    assert [view.fqn for view in views] == []
    assert any("ghost:wg0" in problem for problem in dangling)
    assert any("rtr-b:nosuch" in problem for problem in dangling)
    assert sum("fewer than two endpoints" in problem for problem in dangling) == 2


def test_a_multipoint_tunnel_survives_losing_one_endpoint(tmp_path: Path) -> None:
    loaded = inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("mesh", type="wireguard", endpoints=["rtr-a:wg0", "rtr-b:wg0", "ghost:wg0"]),
    )
    (view,), dangling = resolve_tunnels(loaded)
    assert [str(end) for end in view.ends] == ["rtr-a:wg0", "rtr-b:wg0"]
    assert not view.is_multipoint
    assert any("ghost:wg0" in problem for problem in dangling)


def test_an_over_that_names_no_tunnel_leaves_the_tunnel_unnested(tmp_path: Path) -> None:
    loaded = inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("vx", type="vxlan", vni=1, endpoints=["rtr-a:vx1", "rtr-b:vx1"], over="cbl"),
    )
    (view,), dangling = resolve_tunnels(loaded)
    assert view.over is None
    assert view.stack == ("vxlan",)
    assert any("'over' names no known tunnel" in problem for problem in dangling)


def test_an_over_naming_a_tunnel_that_was_itself_dropped_is_reported(
    tmp_path: Path,
) -> None:
    loaded = inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("ipsec", type="ipsec", endpoints=["rtr-a:ipsec0", "ghost:ipsec0"]),
        tunnel("vx", type="vxlan", vni=1, endpoints=["rtr-a:vx1", "rtr-b:vx1"], over="ipsec"),
    )
    (view,), dangling = resolve_tunnels(loaded)
    assert view.fqn == "vx"
    assert view.over is None
    assert any("which is not drawn" in problem for problem in dangling)


def test_an_encapsulation_loop_is_reported_once_rather_than_once_per_member(
    tmp_path: Path,
) -> None:
    loaded = inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("one", type="ipsec", endpoints=["rtr-a:ipsec0", "rtr-b:ipsec0"], over="two"),
        tunnel("two", type="ipsec", endpoints=["rtr-a:wg0", "rtr-b:wg0"], over="one"),
    )
    views, dangling = resolve_tunnels(loaded)
    loops = [problem for problem in dangling if "loops" in problem]
    assert len(loops) == 1
    assert "one over two over one" in loops[0]
    # Both tunnels are still drawable; only the chain walk stops at the repeat.
    assert {view.fqn for view in views} == {"one", "two"}


# --------------------------------------------------------------------------- #
# What a tunnel looks like in a graph
# --------------------------------------------------------------------------- #


def test_a_point_to_point_tunnel_is_an_edge_and_a_multipoint_one_is_a_node(
    tmp_path: Path,
) -> None:
    loaded = inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b", "rtr-c"),
        cable("cbl-ab", "rtr-a:eth0", "rtr-b:eth0"),
        cable("cbl-bc", "rtr-b:eth0", "rtr-c:eth0"),
        tunnel("pair", type="wireguard", endpoints=["rtr-a:wg0", "rtr-b:wg0"]),
        tunnel("mesh", type="ipsec", endpoints=["rtr-a:ipsec0", "rtr-b:ipsec0", "rtr-c:ipsec0"]),
    )
    graph = build_graph(loaded)

    # Three routers, plus one node for the mesh and none for the pair.
    assert [node.fqn for node in graph.tunnel_nodes] == [f"{TUNNEL_ID_PREFIX}mesh"]
    assert len(graph.element_nodes) == 3

    pair = next(edge for edge in graph.edges if edge.id == "pair")
    assert (pair.source, pair.target) == ("rtr-a", "rtr-b")
    assert (pair.source_port, pair.target_port) == ("wg0", "wg0")
    # A tunnel runs over whatever the underlay provides, so it claims no medium.
    assert pair.medium == ""
    assert pair.is_logical

    legs = [edge for edge in graph.edges if edge.kind is EdgeKind.TUNNEL and edge.id != "pair"]
    assert len(legs) == 3
    assert {edge.target for edge in legs} == {f"{TUNNEL_ID_PREFIX}mesh"}
    assert legs[0].name == "mesh"


def test_the_overlay_layer_makes_every_tunnel_a_node_and_nesting_an_edge(
    nested: Inventory,
) -> None:
    graph = build_graph(nested, layer=Layer.OVERLAY)

    assert {node.fqn for node in graph.tunnel_nodes} == {
        f"{TUNNEL_ID_PREFIX}{name}" for name in ("wg", "ipsec", "vx")
    }
    assert all(node.type is NodeType.TUNNEL for node in graph.tunnel_nodes)
    assert {view.fqn for view in graph.tunnels} == {"wg", "ipsec", "vx"}

    # The one edge that cannot exist at layer 1: it joins two links.
    (nesting,) = [edge for edge in graph.edges if edge.kind is EdgeKind.ENCAPSULATION]
    assert (nesting.source, nesting.target) == (
        f"{TUNNEL_ID_PREFIX}vx",
        f"{TUNNEL_ID_PREFIX}ipsec",
    )
    assert nesting.label == "vxlan"

    # A tunnel node carries the document's own metadata, not a derived stand-in.
    node = graph.nodes[f"{TUNNEL_ID_PREFIX}vx"]
    assert node.kind == "tunnel"
    assert node.labels == {}
    assert node.ports == ()


def test_the_overlay_layer_keeps_only_the_elements_that_terminate_a_tunnel(
    tmp_path: Path,
) -> None:
    loaded = inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b", "rtr-c"),
        cable("cbl-ab", "rtr-a:eth0", "rtr-b:eth0"),
        cable("cbl-bc", "rtr-b:eth0", "rtr-c:eth0"),
        tunnel("wg", type="wireguard", endpoints=["rtr-a:wg0", "rtr-b:wg0"]),
    )
    graph = build_graph(loaded, layer=Layer.OVERLAY)
    assert {node.fqn for node in graph.element_nodes} == {"rtr-a", "rtr-b"}
    # No cable survives: at this layer adjacency means "agreed to encapsulate".
    assert all(edge.kind is not EdgeKind.CABLE for edge in graph.edges)


def test_an_inventory_with_no_tunnel_has_an_empty_overlay_view(tmp_path: Path) -> None:
    loaded = inventory_of(
        tmp_path, routers("rtr-a", "rtr-b"), cable("cbl", "rtr-a:eth0", "rtr-b:eth0")
    )
    assert build_graph(loaded, layer=Layer.OVERLAY).is_empty


def test_the_layer_3_view_needs_no_special_case_for_a_tunnel(nested: Inventory) -> None:
    """The overlay *is* a shared prefix at layer 3, which is the honest drawing."""
    graph = build_graph(nested, layer=Layer.L3)
    assert not graph.tunnel_nodes
    assert all(edge.kind is EdgeKind.SUBNET for edge in graph.edges)
    prefixes = {node.subnet.prefix for node in graph.subnet_nodes if node.subnet}
    # wg0 and ipsec0 at both ends put the two routers in one prefix each.
    assert {"10.9.0.0/24", "10.8.0.0/24"} <= prefixes


def test_a_filter_keeps_a_tunnel_only_while_an_endpoint_survives(nested: Inventory) -> None:
    graph = build_graph(nested, layer=Layer.OVERLAY)
    kept = filter_graph(graph, FilterSpec(names=("rtr-a",)))

    assert {node.fqn for node in kept.element_nodes} == {"rtr-a"}
    # Every tunnel still has one end, so every tunnel node survives — narrowed
    # to the endpoint the reader can actually see.
    for node in kept.tunnel_nodes:
        assert node.tunnel is not None
        assert [end.element for end in node.tunnel.ends] == ["rtr-a"]

    nothing = filter_graph(graph, FilterSpec(names=("rtr-nonexistent*",)))
    assert nothing.is_empty


# --------------------------------------------------------------------------- #
# VLANs across a layer-2 tunnel
# --------------------------------------------------------------------------- #


def _stretched(tmp_path: Path, type_name: str, **extra: Any) -> Inventory:
    """Two routers whose ``vx1`` ports are both access ports in VLAN 100."""
    documents = routers("rtr-a", "rtr-b").replace(
        "{name: vx1, type: tunnel, mtu: 1377}",
        "{name: vx1, type: tunnel, mtu: 1377, vlan: {mode: access, access_vlan: 100}}",
    )
    return inventory_of(
        tmp_path,
        documents,
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("t", type=type_name, endpoints=["rtr-a:vx1", "rtr-b:vx1"], **extra),
    )


def test_a_layer_2_tunnel_carries_the_vlans_its_ends_are_configured_for(
    tmp_path: Path,
) -> None:
    """VXLAN extends a broadcast domain; that is the whole reason it exists."""
    graph = build_graph(_stretched(tmp_path, "vxlan", vni=100), layer=Layer.L2)
    (edge,) = [edge for edge in graph.edges if edge.kind is EdgeKind.TUNNEL]
    assert edge.vlans == frozenset({100})
    assert graph.nodes["rtr-a"].vlans == frozenset({100})


def test_a_layer_3_tunnel_carries_no_vlan_however_its_ends_are_configured(
    tmp_path: Path,
) -> None:
    graph = build_graph(_stretched(tmp_path, "wireguard"), layer=Layer.L2)
    (edge,) = [edge for edge in graph.edges if edge.kind is EdgeKind.TUNNEL]
    assert edge.vlans == frozenset()


def test_a_layer_2_tunnel_merges_two_sites_into_one_broadcast_domain(
    tmp_path: Path,
) -> None:
    """A VLAN carried across an overlay is one domain, not two sharing a number."""
    graph = to_networkx(build_graph(_stretched(tmp_path, "vxlan", vni=100), layer=Layer.L2))
    (domain,) = broadcast_domains(graph)
    assert domain.vlan == 100
    assert set(domain.members) == {"rtr-a", "rtr-b"}
    # …but nobody can unplug it, so the physical view still leaves it out.
    assert str(EdgeKind.TUNNEL) in LINK_EDGE_KINDS
    assert str(EdgeKind.TUNNEL) not in PHYSICAL_EDGE_KINDS


# --------------------------------------------------------------------------- #
# Validation beyond the one-finding fixtures
# --------------------------------------------------------------------------- #


def test_a_well_formed_nested_inventory_reports_nothing_about_its_tunnels(
    nested: Inventory,
) -> None:
    reported = {finding.rule for finding in validate(nested)}
    assert not {rule for rule in reported if rule.endswith(("16", "17", "18", "19"))}
    assert not reported & {"W125", "W126", "W127", "W128", "W129", "I003"}


def test_a_cleartext_tunnel_is_forgiven_as_soon_as_something_above_it_encrypts(
    tmp_path: Path,
) -> None:
    """W127 asks "is this protected?", not "does this type encrypt?"."""
    unprotected = inventory_of(
        tmp_path / "a",
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("vx", type="vxlan", vni=1, endpoints=["rtr-a:vx1", "rtr-b:vx1"]),
    )
    assert "W127" in {finding.rule for finding in validate(unprotected)}

    declared = inventory_of(
        tmp_path / "b",
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("vx", type="vxlan", vni=1, endpoints=["rtr-a:vx1", "rtr-b:vx1"], encrypted=True),
    )
    assert "W127" not in {finding.rule for finding in validate(declared)}


def test_a_layer_2_tunnel_endpoint_is_exempt_from_the_unaddressed_warning(
    tmp_path: Path,
) -> None:
    """W101: a VXLAN port carries frames, so it is a switchport in all but name."""
    layer2 = inventory_of(
        tmp_path / "l2",
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("vx", type="vxlan", vni=1, endpoints=["rtr-a:vx1", "rtr-b:vx1"], over="wg"),
        tunnel("wg", type="wireguard", endpoints=["rtr-a:wg0", "rtr-b:wg0"]),
    )
    assert "W101" not in {finding.rule for finding in validate(layer2)}

    # The same unaddressed port with a *layer-3* tunnel on it is a real finding:
    # a GRE interface with no address routes nothing.
    layer3 = inventory_of(
        tmp_path / "l3",
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel("gre", type="gre", endpoints=["rtr-a:vx1", "rtr-b:vx1"], over="wg"),
        tunnel("wg", type="wireguard", endpoints=["rtr-a:wg0", "rtr-b:wg0"]),
    )
    assert "W101" in {finding.rule for finding in validate(layer3)}


def test_an_encapsulation_cycle_does_not_hang_the_mtu_or_protection_walks(
    tmp_path: Path,
) -> None:
    """Every rule that walks ``over`` has to terminate on an inventory E019 rejects."""
    looped = inventory_of(
        tmp_path,
        routers("rtr-a", "rtr-b"),
        cable("cbl", "rtr-a:eth0", "rtr-b:eth0"),
        tunnel(
            "one", type="vxlan", vni=1, endpoints=["rtr-a:vx1", "rtr-b:vx1"], over="two", mtu=1400
        ),
        tunnel(
            "two", type="vxlan", vni=2, endpoints=["rtr-a:wg0", "rtr-b:wg0"], over="one", mtu=1400
        ),
    )
    reported = {finding.rule for finding in validate(looped)}
    assert "E019" in reported
    # Neither loop member encrypts and neither reaches one that does.
    assert "W127" in reported
