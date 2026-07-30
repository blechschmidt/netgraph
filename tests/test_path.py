"""``netgraph path``: endpoint resolution, the two searches, and the report.

The properties asserted here are the ones a user of the command depends on:

* an endpoint resolves the same way whether it was written as a name, a port or
  an address, and every failure names what it could have meant instead;
* the layer-2 walk relays only where the kind of element says it does and only
  inside a VLAN, so an access port in the wrong VLAN is a wall rather than a
  hop — which is the single fact that makes it a *layer-2* walk;
* the layer-3 walk crosses only elements that forward, stays in one address
  family, and reports the address at each end of every hop;
* a redundant pair comes back as two paths, not one, because that is the case a
  reader is most often asking about;
* a tunnel on the path is named with the encapsulation entered and left, nesting
  included, and one that protects nothing is called out;
* a trace that finds nothing says how far it got, so the break is locatable.

The golden files at the bottom pin the *whole* report — text and JSON — against
the two committed examples, so a change to any of the above shows up as a
readable diff rather than as a passing test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.fsio import write_text
from netgraph.loader import Inventory, load_tree
from netgraph.render import Layer, RenderOptions, build_graph, render_text
from netgraph.trace import (
    DEFAULT_MAX_HOPS,
    MAX_PATHS,
    PATH_KIND,
    Link,
    TracedPath,
    TraceError,
    TraceResult,
    Waypoint,
    render_trace,
    resolve_endpoint,
    trace,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden" / "path"


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
def overlay() -> Inventory:
    inventory = load_tree(EXAMPLES / "overlay")
    assert inventory.errors == []
    return inventory


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def device(name: str, *interfaces: str, kind: str = "computer", extra: str = "") -> str:
    return (
        f"apiVersion: netgraph.dev/v1alpha1\n"
        f"kind: {kind}\n"
        f"metadata: {{name: {name}}}\n"
        f"spec:\n"
        f"  interfaces: [{', '.join(interfaces)}]\n"
        f"{extra}"
    )


def access(name: str, vlan: int, address: str = "") -> str:
    """One access port in ``vlan``, optionally addressed."""
    addresses = f", ipv4: [{address}]" if address else ""
    return (
        f"{{name: {name}, type: ethernet, vlan: {{mode: access, access_vlan: {vlan}}}{addresses}}}"
    )


def port(name: str, address: str = "") -> str:
    """One plain ethernet port with no VLAN configuration."""
    return f"{{name: {name}, type: ethernet{f', ipv4: [{address}]' if address else ''}}}"


def cable(name: str, left: str, right: str) -> str:
    return (
        f"apiVersion: netgraph.dev/v1alpha1\n"
        f"kind: cable\n"
        f"metadata: {{name: {name}}}\n"
        f"spec: {{endpoints: [{left}, {right}], medium: copper}}\n"
    )


def inventory_of(tmp_path: Path, *documents: str) -> Inventory:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "inv.yaml").write_text("---\n".join(documents), encoding="utf-8")
    inventory = load_tree(tmp_path)
    assert inventory.errors == [], [str(error) for error in inventory.errors]
    return inventory


def routes(result: TraceResult) -> list[tuple[str, ...]]:
    """The element sequence of every path found, in the order they are ranked."""
    return [path.elements for path in result.paths]


def links_of(result: TraceResult, index: int = 0) -> list[str]:
    return [link.id for link in result.paths[index].links]


# --------------------------------------------------------------------------- #
# Endpoint resolution
# --------------------------------------------------------------------------- #


def test_an_element_name_resolves_without_pinning_a_port(home_lab: Inventory) -> None:
    endpoint = resolve_endpoint(home_lab, "pc-desk")
    assert (endpoint.element, endpoint.interface, endpoint.address) == (
        "hosts/pc-desk",
        None,
        None,
    )
    assert endpoint.kind == "computer"
    assert endpoint.name == "pc-desk"


def test_a_fully_qualified_name_resolves_too(home_lab: Inventory) -> None:
    assert resolve_endpoint(home_lab, "hosts/pc-desk").element == "hosts/pc-desk"


def test_an_element_interface_selector_pins_the_port_and_its_address(
    home_lab: Inventory,
) -> None:
    endpoint = resolve_endpoint(home_lab, "pc-desk:eno1")
    assert endpoint.interface == "eno1"
    assert endpoint.address == "192.168.10.20/24"
    assert str(endpoint) == "hosts/pc-desk:eno1"


def test_an_interface_name_may_hold_slashes(campus: Inventory) -> None:
    """The separator is the first colon; a namespace and a port both use ``/``."""
    endpoint = resolve_endpoint(campus, "sites/north/access/sw-north-acc-01:GigabitEthernet1/0/1")
    assert endpoint.element == "sites/north/access/sw-north-acc-01"
    assert endpoint.interface == "GigabitEthernet1/0/1"


def test_an_address_resolves_to_the_one_interface_holding_it(campus: Inventory) -> None:
    endpoint = resolve_endpoint(campus, "10.1.10.51")
    assert (endpoint.element, endpoint.interface) == ("sites/north/hosts/pc-north-01", "eno1")
    assert endpoint.address == "10.1.10.51/24"


def test_an_address_may_carry_its_prefix_length(campus: Inventory) -> None:
    """Pasted straight out of ``ip addr`` output, which is where it comes from."""
    assert resolve_endpoint(campus, "10.1.10.51/24").element == "sites/north/hosts/pc-north-01"


def test_an_ipv6_address_resolves(campus: Inventory) -> None:
    endpoint = resolve_endpoint(campus, "2001:db8:1:10::51")
    assert endpoint.element == "sites/north/hosts/pc-north-01"
    assert endpoint.address == "2001:db8:1:10::51/64"


def test_an_adapters_upstream_port_is_a_valid_selector(home_lab: Inventory) -> None:
    """§8.1: the upstream port shares the adapter's interface namespace."""
    assert resolve_endpoint(home_lab, "adp-usb-eth:usb0").interface == "usb0"


def test_an_unknown_element_says_how_to_find_the_right_one(home_lab: Inventory) -> None:
    with pytest.raises(TraceError, match="no element named 'nope'") as caught:
        resolve_endpoint(home_lab, "nope")
    assert "netgraph list devices" in str(caught.value)


def test_an_unknown_interface_lists_the_ones_that_exist(home_lab: Inventory) -> None:
    with pytest.raises(TraceError) as caught:
        resolve_endpoint(home_lab, "pc-desk:eth9")
    assert "has no interface 'eth9'" in str(caught.value)
    assert "eno1" in caught.value.candidates


def test_an_unknown_address_says_it_is_not_configured(home_lab: Inventory) -> None:
    with pytest.raises(TraceError, match="no interface in this inventory is addressed"):
        resolve_endpoint(home_lab, "203.0.113.99")


def test_a_loopback_address_is_refused_with_the_reason(campus: Inventory) -> None:
    """Every host declares 127.0.0.1, so it identifies nothing."""
    with pytest.raises(TraceError, match="deliberately not searched"):
        resolve_endpoint(campus, "127.0.0.1")


def test_an_address_on_two_interfaces_lists_both(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("pc-a", port("eth0", "10.0.0.1/24")),
        device("pc-b", port("eth0", "10.0.0.1/24")),
    )
    with pytest.raises(TraceError, match="is configured on 2 interfaces") as caught:
        resolve_endpoint(inventory, "10.0.0.1")
    assert caught.value.candidates == ("pc-a:eth0", "pc-b:eth0")


def test_an_ambiguous_short_name_lists_every_candidate(tmp_path: Path) -> None:
    for folder in ("north", "south"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "inv.yaml").write_text(
            device("sw", port("ge-0"), kind="switch"), encoding="utf-8"
        )
    inventory = load_tree(tmp_path)
    with pytest.raises(TraceError, match="is ambiguous") as caught:
        resolve_endpoint(inventory, "sw")
    assert caught.value.candidates == ("north/sw", "south/sw")


def test_a_tunnel_is_a_link_and_cannot_be_an_endpoint(overlay: Inventory) -> None:
    with pytest.raises(TraceError, match="is a tunnel, which is a link") as caught:
        resolve_endpoint(overlay, "wg-mesh")
    assert "rtr-hq:wg0" in caught.value.candidates


def test_a_cable_cannot_be_an_endpoint(home_lab: Inventory) -> None:
    with pytest.raises(TraceError, match="owns no interfaces"):
        resolve_endpoint(home_lab, "cbl-rtr-sw")


def test_an_empty_endpoint_is_refused(home_lab: Inventory) -> None:
    with pytest.raises(TraceError, match="is empty"):
        resolve_endpoint(home_lab, "   ")


# --------------------------------------------------------------------------- #
# Layer 2
# --------------------------------------------------------------------------- #


def test_two_hosts_in_one_vlan_trace_through_the_switch(home_lab: Inventory) -> None:
    result = trace(home_lab, "pc-desk", "srv-nas")
    assert result.layer is Layer.L2
    assert routes(result) == [("hosts/pc-desk", "switches/sw-home", "hosts/srv-nas")]
    assert links_of(result) == ["cables/cbl-sw-desk", "cables/cbl-sw-nas"]
    assert result.paths[0].vlans == frozenset({10})


def test_each_waypoint_names_the_ports_it_is_entered_and_left_by(home_lab: Inventory) -> None:
    path = trace(home_lab, "pc-desk", "srv-nas").paths[0]
    origin, middle, terminus = path.waypoints
    assert (origin.ingress, origin.egress) == (None, "eno1")
    assert (middle.ingress, middle.egress) == ("port2", "port3")
    assert (terminus.ingress, terminus.egress) == ("eth0", None)
    assert origin.is_origin and terminus.is_terminus


def test_an_adapter_is_transparent(home_lab: Inventory) -> None:
    """§8.2: collapsing an adapter into its host must not change connectivity."""
    result = trace(home_lab, "laptop", "pc-desk")
    assert routes(result) == [
        ("hosts/laptop", "hosts/adp-usb-eth", "switches/sw-home", "hosts/pc-desk")
    ]
    assert result.paths[0].links[0].kind == "attachment"
    # §8.1: the host end of an attachment names no interface.
    assert result.paths[0].waypoints[0].egress is None


def test_a_hub_relays_everything_on_its_shared_segment(tmp_path: Path) -> None:
    """A repeater has no MAC table and no VLANs (§6.5): every port hears every frame."""
    inventory = inventory_of(
        tmp_path,
        device("hub1", port("p1"), port("p2"), port("p3"), kind="hub"),
        device("pc-a", port("eth0")),
        device("pc-b", port("eth0")),
        device("pc-c", port("eth0")),
        cable("c-a", "hub1:p1", "pc-a:eth0"),
        cable("c-b", "hub1:p2", "pc-b:eth0"),
        cable("c-c", "hub1:p3", "pc-c:eth0"),
    )
    for far in ("pc-b", "pc-c"):
        assert routes(trace(inventory, "pc-a", far)) == [("pc-a", "hub1", far)]


def test_an_access_port_in_another_vlan_is_not_a_path(campus: Inventory) -> None:
    """The one fact that makes this a layer-2 walk rather than a connectivity walk.

    ``pc-north-01`` (VLAN 10) and ``srv-north-01`` (VLAN 20) hang off the *same*
    switch, so they are one hop apart physically and in different broadcast
    domains logically. The trace must fall through to layer 3.
    """
    result = trace(campus, "pc-north-01", "srv-north-01")
    assert result.layer is Layer.L3
    assert any("no layer-2 path" in note for note in result.notes)


def test_a_trunk_carries_the_vlan_across_switches(campus: Inventory) -> None:
    result = trace(campus, "pc-north-01", "pc-north-02")
    assert result.layer is Layer.L2
    assert result.paths[0].vlans == frozenset({10})
    assert [waypoint.element for waypoint in result.paths[0].waypoints] == [
        "sites/north/hosts/pc-north-01",
        "sites/north/access/sw-north-acc-01",
        "sites/north/distribution/sw-north-dist-01",
        "sites/north/access/sw-north-acc-02",
        "sites/north/hosts/pc-north-02",
    ]


def test_a_trunked_route_reports_every_vlan_it_is_feasible_in(campus: Inventory) -> None:
    """Two trunks agree on five VLANs; the trace assumed all of them, and says so."""
    result = trace(campus, "sw-north-acc-01", "sw-north-acc-02")
    assert result.paths[0].vlans == frozenset({1, 10, 20, 30, 99})


def test_vlan_forces_the_search_and_skips_layer_three(campus: Inventory) -> None:
    result = trace(campus, "pc-north-01", "pc-north-02", vlan=20)
    assert not result.found
    assert result.forced_vlan == 20
    # Layer 3 was never attempted: a VLAN is a layer-2 question.
    assert result.attempted == (Layer.L2,)
    assert any("--vlan" in note for note in result.notes)


def test_a_router_does_not_relay_frames(tmp_path: Path) -> None:
    """A frame arriving at a router has arrived; getting past it is layer 3's job."""
    inventory = inventory_of(
        tmp_path,
        device("rtr", port("ge-0"), port("ge-1"), kind="router"),
        device("pc-a", port("eth0")),
        device("pc-b", port("eth0")),
        cable("c-a", "rtr:ge-0", "pc-a:eth0"),
        cable("c-b", "rtr:ge-1", "pc-b:eth0"),
    )
    assert not trace(inventory, "pc-a", "pc-b").found


def test_a_lag_comes_back_as_two_paths(tmp_path: Path) -> None:
    """Two cables between one pair of switches are two routes, not one."""
    inventory = inventory_of(
        tmp_path,
        device("sw-a", port("ge-0"), port("ge-1"), port("ge-2"), kind="switch"),
        device("sw-b", port("ge-0"), port("ge-1"), port("ge-2"), kind="switch"),
        device("pc-a", port("eth0")),
        device("pc-b", port("eth0")),
        cable("lag-0", "sw-a:ge-0", "sw-b:ge-0"),
        cable("lag-1", "sw-a:ge-1", "sw-b:ge-1"),
        cable("c-a", "sw-a:ge-2", "pc-a:eth0"),
        cable("c-b", "sw-b:ge-2", "pc-b:eth0"),
    )
    result = trace(inventory, "pc-a", "pc-b")
    assert len(result.paths) == 2
    assert routes(result) == [("pc-a", "sw-a", "sw-b", "pc-b")] * 2
    assert [links_of(result, index)[1] for index in (0, 1)] == ["lag-0", "lag-1"]
    # The default report shows one of them; --all shows both.
    assert len(result.selected(all_paths=False)) == 1
    assert len(result.selected(all_paths=True)) == 2


def test_a_ring_terminates_and_reports_both_ways_round(tmp_path: Path) -> None:
    """Three switches in a loop: two routes, and the search does not spin."""
    inventory = inventory_of(
        tmp_path,
        *(
            device(f"sw-{name}", port("ge-0"), port("ge-1"), kind="switch")
            for name in ("a", "b", "c")
        ),
        cable("ring-ab", "sw-a:ge-0", "sw-b:ge-1"),
        cable("ring-bc", "sw-b:ge-0", "sw-c:ge-1"),
        cable("ring-ca", "sw-c:ge-0", "sw-a:ge-1"),
    )
    result = trace(inventory, "sw-a", "sw-b")
    assert routes(result) == [("sw-a", "sw-b"), ("sw-a", "sw-c", "sw-b")]


def test_max_hops_abandons_a_route_that_is_too_long(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *(device(f"sw-{index}", port("ge-0"), port("ge-1"), kind="switch") for index in range(4)),
        *(cable(f"c-{index}", f"sw-{index}:ge-0", f"sw-{index + 1}:ge-1") for index in range(3)),
    )
    assert trace(inventory, "sw-0", "sw-3").found
    assert not trace(inventory, "sw-0", "sw-3", max_hops=2).found
    assert trace(inventory, "sw-0", "sw-3", max_hops=2).max_hops == 2


def test_a_port_selector_picks_which_of_two_links_is_used(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("sw-a", port("ge-0"), port("ge-1"), port("ge-2"), kind="switch"),
        device("sw-b", port("ge-0"), port("ge-1"), port("ge-2"), kind="switch"),
        device("pc-b", port("eth0")),
        cable("lag-0", "sw-a:ge-0", "sw-b:ge-0"),
        cable("lag-1", "sw-a:ge-1", "sw-b:ge-1"),
        cable("c-b", "sw-b:ge-2", "pc-b:eth0"),
    )
    result = trace(inventory, "sw-a:ge-1", "pc-b")
    assert len(result.paths) == 1
    assert links_of(result) == ["lag-1", "c-b"]


def test_a_broken_segment_names_the_furthest_element_reached(tmp_path: Path) -> None:
    """Two islands: the answer must say where the traffic stopped."""
    inventory = inventory_of(
        tmp_path,
        device("sw-a", port("ge-0"), port("ge-1"), kind="switch"),
        device("sw-b", port("ge-0"), kind="switch"),
        device("pc-a", port("eth0")),
        device("pc-b", port("eth0")),
        cable("c-a", "sw-a:ge-0", "pc-a:eth0"),
        cable("c-b", "sw-b:ge-0", "pc-b:eth0"),
    )
    result = trace(inventory, "pc-a", "pc-b")
    assert not result.found
    frontier = result.frontiers[0]
    assert (frontier.layer, frontier.furthest, frontier.reached) == (Layer.L2, "sw-a", 2)
    assert "the furthest was sw-a" in render_trace(result, "text")


def test_an_isolated_source_says_it_reaches_nothing(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("pc-a", port("eth0")),
        device("pc-b", port("eth0")),
    )
    report = render_trace(trace(inventory, "pc-a", "pc-b"), "text")
    assert "reaches nothing at all" in report


def test_two_ports_of_one_switch_are_a_zero_hop_path(home_lab: Inventory) -> None:
    result = trace(home_lab, "sw-home:port1", "sw-home:port3")
    assert result.found
    assert result.paths[0].hops == 0
    assert result.paths[0].waypoints[0].vlans == frozenset({10})
    assert "never leaves it" in render_trace(result, "text")


def test_one_element_named_twice_is_a_zero_hop_path(home_lab: Inventory) -> None:
    """Without a port on either end there is no switching decision to make."""
    result = trace(home_lab, "sw-home", "sw-home")
    assert result.found and result.paths[0].hops == 0
    assert result.paths[0].waypoints[0].element == "switches/sw-home"


def test_one_port_named_on_both_ends_is_a_zero_hop_path(home_lab: Inventory) -> None:
    """A frame does not have to cross a switch to arrive where it started."""
    result = trace(home_lab, "sw-home:port1", "sw-home:port1")
    assert result.found and result.paths[0].hops == 0


def test_two_ports_of_one_host_are_not_a_path(home_lab: Inventory) -> None:
    """A workstation does not bridge its NIC to its Wi-Fi radio."""
    result = trace(home_lab, "pc-desk:eno1", "pc-desk:wlp1s0")
    assert not result.found
    assert any("does not relay between two of its own ports" in note for note in result.notes)


def test_two_ports_of_one_switch_in_different_vlans_are_not_a_path(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("sw", access("ge-0", 10), access("ge-1", 20), kind="switch"),
    )
    result = trace(inventory, "sw:ge-0", "sw:ge-1")
    assert not result.found
    assert any("share no VLAN" in note for note in result.notes)
    assert "nothing crosses" in render_trace(result, "text")


def test_a_self_link_is_not_a_hop(tmp_path: Path) -> None:
    """A cable with both ends on one device changes nothing about reachability."""
    inventory = inventory_of(
        tmp_path,
        device("sw", port("ge-0"), port("ge-1"), port("ge-2"), kind="switch"),
        device("pc", port("eth0")),
        cable("loopback", "sw:ge-0", "sw:ge-1"),
        cable("c", "sw:ge-2", "pc:eth0"),
    )
    assert links_of(trace(inventory, "sw", "pc")) == ["c"]


# --------------------------------------------------------------------------- #
# Layer 3
# --------------------------------------------------------------------------- #


def test_a_routed_path_hops_router_by_router(campus: Inventory) -> None:
    result = trace(campus, "pc-north-01", "srv-north-01")
    assert result.layer is Layer.L3
    assert routes(result) == [
        (
            "sites/north/hosts/pc-north-01",
            "sites/north/distribution/sw-north-dist-01",
            "sites/north/hosts/srv-north-01",
        )
    ]
    assert links_of(result) == ["10.1.10.0/24", "10.1.20.0/24"]


def test_each_routed_hop_reports_the_address_at_both_ends(campus: Inventory) -> None:
    link = trace(campus, "pc-north-01", "srv-north-01").paths[0].links[0]
    assert link.subnet == "10.1.10.0/24"
    assert link.addresses == ("10.1.10.51/24", "10.1.10.1/24")


def test_a_redundant_backbone_comes_back_as_two_routed_paths(campus: Inventory) -> None:
    """The ring means north reaches south directly or the long way round."""
    result = trace(campus, "pc-north-01", "pc-south-01")
    assert len(result.paths) == 2
    assert result.paths[0].hops < result.paths[1].hops
    assert "sites/west/core/rtr-west-core-01" in result.paths[1].elements
    assert "sites/west/core/rtr-west-core-01" not in result.paths[0].elements


def test_an_element_that_does_not_forward_is_not_a_via(tmp_path: Path) -> None:
    """A workstation with two NICs is a destination, not a router."""
    two_homed = device("pc-mid", port("eth0", "10.0.0.2/24"), port("eth1", "10.0.1.2/24"))
    router = device(
        "rtr-mid",
        port("eth0", "10.0.0.2/24"),
        port("eth1", "10.0.1.2/24"),
        kind="router",
    )
    ends = (
        device("pc-a", port("eth0", "10.0.0.1/24")),
        device("pc-b", port("eth0", "10.0.1.1/24")),
    )
    assert not trace(inventory_of(tmp_path / "a", two_homed, *ends), "pc-a", "pc-b").found
    assert trace(inventory_of(tmp_path / "b", router, *ends), "pc-a", "pc-b").found


def test_a_layer_three_switch_that_declares_forwarding_is_a_via(campus: Inventory) -> None:
    """``sw-north-dist-01`` routes between its SVIs because §6.1.1 says it does."""
    result = trace(campus, "pc-north-01", "srv-north-01")
    assert result.paths[0].waypoints[1].kind == "switch"


def test_a_routed_path_picks_ipv4_when_both_ends_have_one(campus: Inventory) -> None:
    result = trace(campus, "pc-north-01", "srv-north-01")
    assert result.paths[0].family == "ipv4"


def test_an_address_endpoint_decides_the_family(campus: Inventory) -> None:
    result = trace(campus, "2001:db8:1:10::51", "2001:db8:1:20::11")
    assert result.paths[0].family == "ipv6"
    assert links_of(result) == ["2001:db8:1:10::/64", "2001:db8:1:20::/64"]


def test_two_families_in_one_question_is_refused(campus: Inventory) -> None:
    with pytest.raises(TraceError, match="does not change address family"):
        trace(campus, "10.1.10.51", "2001:db8:1:20::11")


def test_an_unaddressed_element_says_it_is_absent_from_layer_three(
    home_lab: Inventory,
) -> None:
    """The laptop's only addresses are on ``lo``, so it is not in the routed graph."""
    result = trace(home_lab, "laptop", "rtr-home:wan0")
    assert not result.found
    assert any("no routable address" in note for note in result.notes)


def test_a_family_only_one_end_has_says_so(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("rtr", port("eth0", "10.0.0.1/24"), port("eth1", "10.0.1.1/24"), kind="router"),
        device("pc-a", "{name: eth0, type: ethernet, ipv6: [2001:db8::1/64]}"),
        device("pc-b", port("eth0", "10.0.1.2/24")),
    )
    result = trace(inventory, "pc-a", "pc-b")
    assert not result.found
    assert any("share no address family" in note for note in result.notes)


def test_an_address_endpoint_pins_which_prefix_the_first_hop_uses(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "rtr",
            port("eth0", "10.0.0.1/24"),
            "{name: eth1, type: ethernet, ipv4: [10.0.1.1/24, 10.0.2.1/24]}",
            kind="router",
        ),
        device("pc-a", port("eth0", "10.0.0.9/24")),
        device("pc-b", "{name: eth0, type: ethernet, ipv4: [10.0.2.9/24]}"),
    )
    result = trace(inventory, "10.0.0.9", "10.0.2.9")
    assert links_of(result) == ["10.0.0.0/24", "10.0.2.0/24"]


# --------------------------------------------------------------------------- #
# Overlays
# --------------------------------------------------------------------------- #


def test_a_layer_two_tunnel_extends_the_broadcast_domain(overlay: Inventory) -> None:
    """VLAN 100 crosses the VXLAN, which is the whole reason the overlay exists."""
    result = trace(overlay, "rtr-hq", "rtr-branch-b", vlan=100)
    assert result.layer is Layer.L2
    assert links_of(result) == ["tunnels/vx-100"]
    assert result.paths[0].vlans == frozenset({100})


def test_a_nested_tunnel_reports_the_whole_stack(overlay: Inventory) -> None:
    view = trace(overlay, "rtr-hq", "rtr-branch-b", vlan=100).paths[0].links[0].tunnel
    assert view is not None
    assert view.stack == ("vxlan", "ipsec")
    assert view.stack_text == "vxlan over ipsec"
    assert view.encrypted_by == "tunnels/ipsec-hq-b"
    assert "vxlan over ipsec" in render_trace(
        trace(overlay, "rtr-hq", "rtr-branch-b", vlan=100), "text"
    )


def test_a_layer_three_tunnel_is_not_crossed_by_a_layer_two_walk(overlay: Inventory) -> None:
    """WireGuard carries packets, so it extends no broadcast domain."""
    assert not trace(overlay, "rtr-hq", "rtr-branch-a", vlan=100).found


def test_a_routed_hop_over_a_tunnel_names_the_encapsulation(overlay: Inventory) -> None:
    result = trace(overlay, "pc-branch-a", "srv-hq")
    tunnel = result.paths[0].links[1].tunnel
    assert tunnel is not None and tunnel.fqn == "tunnels/wg-mesh"
    assert tunnel.type == "wireguard" and tunnel.encrypted


def test_the_shortest_routed_path_prefers_the_overlay_over_the_underlay(
    overlay: Inventory,
) -> None:
    """Three hops through the mesh beats four through the provider edge."""
    result = trace(overlay, "pc-branch-a", "srv-hq")
    assert result.paths[0].hops == 3
    assert "wan/wan-core" not in result.paths[0].elements
    assert any("wan/wan-core" in path.elements for path in result.paths)


def test_a_cleartext_tunnel_on_the_path_is_called_out(tmp_path: Path) -> None:
    """The same fact ``W127`` reports of an inventory, reported of a route."""
    inventory = inventory_of(
        tmp_path,
        device(
            "rtr-a",
            port("eth0", "198.51.100.1/30"),
            "{name: gre0, type: tunnel, parent: eth0, ipv4: [10.0.0.1/30]}",
            kind="router",
        ),
        device(
            "rtr-b",
            port("eth0", "198.51.100.2/30"),
            "{name: gre0, type: tunnel, parent: eth0, ipv4: [10.0.0.2/30]}",
            kind="router",
        ),
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: tunnel\n"
        "metadata: {name: gre-open}\n"
        "spec: {type: gre, endpoints: [rtr-a:gre0, rtr-b:gre0]}\n",
    )
    result = trace(inventory, "10.0.0.1", "10.0.0.2")
    assert [view.fqn for view in result.cleartext_tunnels] == ["gre-open"]
    assert "CLEARTEXT" in render_trace(result, "text")


def test_a_protected_tunnel_is_not_called_out(overlay: Inventory) -> None:
    """Nesting silences it: a VXLAN inside IPsec is protected by the underlay."""
    result = trace(overlay, "rtr-hq", "rtr-branch-b", vlan=100)
    assert result.cleartext_tunnels == ()
    assert "encrypted by tunnels/ipsec-hq-b" in render_trace(result, "text")


def test_a_multipoint_layer_two_tunnel_is_walked_end_to_end(tmp_path: Path) -> None:
    """A tunnel drawn as a node is still one hop: a frame does not stop at it."""
    documents = [
        device(
            f"rtr-{name}",
            port("eth0", f"198.51.100.{index + 1}/24"),
            "{name: vx0, type: tunnel, parent: eth0, vlan: {mode: access, access_vlan: 50}}",
            kind="router",
        )
        for index, name in enumerate(("a", "b", "c"))
    ]
    inventory = inventory_of(
        tmp_path,
        *documents,
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: tunnel\n"
        "metadata: {name: vx-mesh}\n"
        "spec: {type: vxlan, vni: 50, endpoints: [rtr-a:vx0, rtr-b:vx0, rtr-c:vx0]}\n",
    )
    result = trace(inventory, "rtr-a", "rtr-c", vlan=50)
    assert routes(result) == [("rtr-a", "rtr-c")]
    link = result.paths[0].links[0]
    assert link.id == "vx-mesh"
    # Both legs travel with the hop, so --highlight emphasises the whole tunnel.
    assert link.graph_edges == ("vx-mesh#rtr-a:vx0", "vx-mesh#rtr-c:vx0")
    assert link.graph_nodes == ("tunnel:vx-mesh",)


def test_a_dense_mesh_stops_at_the_path_cap_and_says_so(tmp_path: Path) -> None:
    """A full mesh has combinatorially many routes; the cap is reported, not hidden."""
    size = 8
    documents = [
        device(
            f"sw-{index}",
            *(port(f"ge-{other}") for other in range(size)),
            kind="switch",
        )
        for index in range(size)
    ]
    documents += [
        cable(f"c-{left}-{right}", f"sw-{left}:ge-{right}", f"sw-{right}:ge-{left}")
        for left in range(size)
        for right in range(left + 1, size)
    ]
    result = trace(inventory_of(tmp_path, *documents), "sw-0", f"sw-{size - 1}")
    assert result.truncated
    assert len(result.paths) >= MAX_PATHS
    assert "the search stopped after" in render_trace(result, "text")


def test_a_routed_search_stops_at_the_cap_too(tmp_path: Path) -> None:
    """The same guard on the layer-3 walk, where a full mesh of /24s is one prefix apart."""
    size = 8
    documents = [
        device(
            f"rtr-{index}",
            *(
                port(f"eth-{other}", f"10.{min(index, other)}.{max(index, other)}.{index + 1}/24")
                for other in range(size)
                if other != index
            ),
            kind="router",
        )
        for index in range(size)
    ]
    result = trace(inventory_of(tmp_path, *documents), "rtr-0", f"rtr-{size - 1}")
    assert result.layer is Layer.L3
    assert result.truncated


def test_an_address_family_only_one_end_holds_is_reported(tmp_path: Path) -> None:
    """An IPv6 argument against an element with no IPv6 address."""
    inventory = inventory_of(
        tmp_path,
        device("pc-a", "{name: eth0, type: ethernet, ipv6: [2001:db8::1/64]}"),
        device("pc-b", port("eth0", "10.0.0.2/24")),
    )
    result = trace(inventory, "2001:db8::1", "pc-b")
    assert not result.found
    assert any("has no ipv6 address" in note for note in result.notes)


def test_a_zero_hop_path_rejects_a_mismatched_link_count() -> None:
    """The waypoint/link invariant is asserted where it is built, not assumed."""
    waypoint = Waypoint(element="sw", kind="switch")
    with pytest.raises(ValueError, match="must have 0 link"):
        TracedPath(
            waypoints=(waypoint,), links=(Link(id="x", kind="cable", source="a", target="b"),)
        )


def test_a_path_iterates_its_waypoints(home_lab: Inventory) -> None:
    path = trace(home_lab, "pc-desk", "srv-nas").paths[0]
    assert [waypoint.element for waypoint in path] == list(path.elements)
    assert path.waypoints[0].name == "pc-desk"


def test_a_result_with_no_paths_has_no_shortest(tmp_path: Path) -> None:
    inventory = inventory_of(tmp_path, device("pc-a", port("eth0")), device("pc-b", port("eth0")))
    assert trace(inventory, "pc-a", "pc-b").shortest is None


def test_the_json_report_lists_the_cleartext_tunnels_of_a_path(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device(
            "rtr-a",
            port("eth0", "198.51.100.1/30"),
            "{name: gre0, type: tunnel, parent: eth0, ipv4: [10.0.0.1/30]}",
            kind="router",
        ),
        device(
            "rtr-b",
            port("eth0", "198.51.100.2/30"),
            "{name: gre0, type: tunnel, parent: eth0, ipv4: [10.0.0.2/30]}",
            kind="router",
        ),
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: tunnel\n"
        "metadata: {name: gre-open}\n"
        "spec: {type: gre, endpoints: [rtr-a:gre0, rtr-b:gre0]}\n",
    )
    path = json.loads(render_trace(trace(inventory, "10.0.0.1", "10.0.0.2"), "json"))["paths"][0]
    assert path["cleartextTunnels"] == ["gre-open"]


def test_an_empty_vlan_set_renders_as_nothing(home_lab: Inventory) -> None:
    """A routed path declares no VLAN, and the report must not print an empty one."""
    report = render_trace(trace(home_lab, "pc-desk", "rtr-home:wan0"), "text")
    assert "vlan " not in report.partition("path 1")[0]


def test_a_multipoint_tunnel_hairpinned_on_one_element_is_not_a_hop(tmp_path: Path) -> None:
    """Two ends of one tunnel on one router join it to itself, which is no hop."""
    inventory = inventory_of(
        tmp_path,
        device(
            "rtr-a",
            port("eth0", "198.51.100.1/24"),
            "{name: vx0, type: tunnel, parent: eth0, vlan: {mode: access, access_vlan: 50}}",
            "{name: vx1, type: tunnel, parent: eth0, vlan: {mode: access, access_vlan: 50}}",
            kind="router",
        ),
        device(
            "rtr-b",
            port("eth0", "198.51.100.2/24"),
            "{name: vx0, type: tunnel, parent: eth0, vlan: {mode: access, access_vlan: 50}}",
            kind="router",
        ),
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: tunnel\n"
        "metadata: {name: vx-mesh}\n"
        "spec: {type: vxlan, vni: 50, endpoints: [rtr-a:vx0, rtr-a:vx1, rtr-b:vx0]}\n",
    )
    result = trace(inventory, "rtr-a", "rtr-b", vlan=50)
    assert routes(result) == [("rtr-a", "rtr-b")]


# --------------------------------------------------------------------------- #
# Highlighting
# --------------------------------------------------------------------------- #


def test_a_highlight_names_the_elements_and_links_of_the_path(home_lab: Inventory) -> None:
    highlight = trace(home_lab, "pc-desk", "srv-nas").highlight(all_paths=False)
    assert highlight.nodes == {"hosts/pc-desk", "switches/sw-home", "hosts/srv-nas"}
    assert highlight.edges == {"cables/cbl-sw-desk", "cables/cbl-sw-nas"}
    assert not highlight.is_empty


def test_a_layer_three_highlight_covers_the_prefix_nodes(campus: Inventory) -> None:
    highlight = trace(campus, "pc-north-01", "srv-north-01").highlight(all_paths=False)
    assert "subnet:10.1.10.0/24" in highlight.nodes
    assert "sites/north/hosts/pc-north-01:eno1#10.1.10.0/24" in highlight.edges


def test_all_paths_widens_the_highlight_to_every_route(campus: Inventory) -> None:
    result = trace(campus, "pc-north-01", "pc-south-01")
    assert len(result.highlight(all_paths=True).nodes) > len(
        result.highlight(all_paths=False).nodes
    )


def test_a_failed_trace_highlights_nothing(tmp_path: Path) -> None:
    inventory = inventory_of(tmp_path, device("pc-a", port("eth0")), device("pc-b", port("eth0")))
    assert trace(inventory, "pc-a", "pc-b").highlight(all_paths=True).is_empty


def test_the_dot_renderer_emphasises_the_path_and_dims_the_rest(home_lab: Inventory) -> None:
    result = trace(home_lab, "pc-desk", "srv-nas")
    graph = build_graph(home_lab, layer=Layer.L2)
    highlighted = render_text(
        graph, "dot", RenderOptions(highlight=result.highlight(all_paths=False))
    )
    plain = render_text(graph, "dot", RenderOptions())

    assert "#b91c1c" in highlighted and "#b91c1c" not in plain
    assert "#a1a1aa" in highlighted and "#a1a1aa" not in plain
    # The kind's own fill survives on the path, so a switch still looks like one.
    assert '"switches/sw-home" [shape=box3d, fillcolor="#dcf0dc", color="#b91c1c"' in highlighted
    assert '"routers/rtr-home" [shape=diamond, fillcolor="#fafafa"' in highlighted


def test_rendering_without_a_highlight_is_unchanged(home_lab: Inventory) -> None:
    """The two new DOT attributes must not appear unless a highlight asked for them."""
    plain = render_text(build_graph(home_lab), "dot", RenderOptions())
    assert "fontcolor" not in plain.split("subgraph")[0].partition("node [")[2]


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def test_the_text_report_is_hop_by_hop(home_lab: Inventory) -> None:
    report = render_trace(trace(home_lab, "pc-desk", "srv-nas"), "text")
    assert "hosts/pc-desk  [computer]" in report
    assert "out eno1" in report
    assert "cable cbl-sw-desk  (copper, 1Gbps, H-002, 8m)  vlan 10" in report
    assert report.endswith("\n")


def test_the_json_report_carries_the_document_envelope(home_lab: Inventory) -> None:
    document = json.loads(render_trace(trace(home_lab, "pc-desk", "srv-nas"), "json"))
    assert document["apiVersion"] == "netgraph.dev/v1alpha1"
    assert document["kind"] == PATH_KIND
    assert document["found"] is True
    assert document["layer"] == "l2"
    assert document["maxHops"] == DEFAULT_MAX_HOPS


def test_the_json_report_pairs_waypoints_with_links(home_lab: Inventory) -> None:
    path = json.loads(render_trace(trace(home_lab, "pc-desk", "srv-nas"), "json"))["paths"][0]
    assert len(path["links"]) == len(path["waypoints"]) - 1 == path["hops"]
    assert path["waypoints"][1]["ingress"]["interface"] == "port2"
    assert path["links"][0]["endpoints"][0] == {"node": "hosts/pc-desk", "interface": "eno1"}


def test_the_json_report_keeps_every_path_whatever_all_says(campus: Inventory) -> None:
    """``--all`` shapes a screen; a program asked for the routes and gets them."""
    result = trace(campus, "pc-north-01", "pc-south-01")
    for all_paths in (False, True):
        document = json.loads(render_trace(result, "json", all_paths=all_paths))
        assert len(document["paths"]) == 2 == document["pathCount"]


def test_the_json_report_names_a_tunnel_and_what_protects_it(overlay: Inventory) -> None:
    result = trace(overlay, "rtr-hq", "rtr-branch-b", vlan=100)
    tunnel = json.loads(render_trace(result, "json"))["paths"][0]["links"][0]["tunnel"]
    assert tunnel["stack"] == ["vxlan", "ipsec"]
    assert (tunnel["encrypted"], tunnel["protected"]) == (False, True)
    assert tunnel["encryptedBy"] == "tunnels/ipsec-hq-b"


def test_a_failed_json_report_carries_the_frontiers(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        device("pc-a", port("eth0", "10.0.0.1/24")),
        device("pc-b", port("eth0", "10.0.1.1/24")),
    )
    document = json.loads(render_trace(trace(inventory, "pc-a", "pc-b"), "json"))
    assert document["found"] is False and document["paths"] == []
    assert [entry["layer"] for entry in document["frontiers"]] == ["l2", "l3"]


def test_a_report_is_reproducible(campus: Inventory) -> None:
    """Two traces of one inventory agree, so a golden file can exist at all."""
    for output_format in ("text", "json"):
        first = render_trace(trace(campus, "pc-north-01", "pc-south-01"), output_format)
        second = render_trace(trace(campus, "pc-north-01", "pc-south-01"), output_format)
        assert first == second


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, example: str, *arguments: str):  # type: ignore[no-untyped-def]
    return runner.invoke(
        cli, ["-i", str(EXAMPLES / example), "path", *arguments], catch_exceptions=False
    )


def test_the_command_prints_the_report_and_exits_zero(runner: CliRunner) -> None:
    result = invoke(runner, "campus", "pc-north-01", "pc-north-02")
    assert result.exit_code == 0
    assert "path 1 of 1" in result.output


def test_no_path_exits_non_zero(runner: CliRunner) -> None:
    """So a reachability assertion drops straight into CI."""
    result = invoke(runner, "campus", "pc-north-01", "pc-north-02", "--vlan", "20")
    assert result.exit_code == 1
    assert "no path" in result.output


def test_a_bad_endpoint_is_a_usage_error(runner: CliRunner) -> None:
    result = invoke(runner, "campus", "nope", "pc-north-02")
    assert result.exit_code == 2
    assert "no element named 'nope'" in result.output


def test_all_reports_every_path(runner: CliRunner) -> None:
    one = invoke(runner, "campus", "pc-north-01", "pc-south-01")
    every = invoke(runner, "campus", "pc-north-01", "pc-south-01", "--all")
    assert "path 2 of 2" not in one.output
    assert "path 2 of 2" in every.output
    assert "pass --all" in one.output


def test_json_output_is_parseable(runner: CliRunner) -> None:
    result = invoke(runner, "campus", "-F", "json", "pc-north-01", "srv-north-01")
    assert json.loads(result.output)["kind"] == PATH_KIND


def test_highlight_writes_a_diagram_to_a_file(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "path.dot"
    result = invoke(
        runner,
        "campus",
        "pc-north-01",
        "srv-north-01",
        "--highlight",
        "-f",
        "dot",
        "-o",
        str(target),
    )
    assert result.exit_code == 0
    document = target.read_text(encoding="utf-8")
    assert "#b91c1c" in document and "#a1a1aa" in document
    # The report still goes to stdout when the diagram has a file of its own.
    assert "path 1 of 1" in result.output


def test_highlight_without_output_puts_the_diagram_on_stdout(runner: CliRunner) -> None:
    result = invoke(runner, "home-lab", "pc-desk", "srv-nas", "--highlight")
    assert result.exit_code == 0
    # stdout is the diagram's; the hop-by-hop report moves to stderr.
    assert result.stdout.startswith("graph netgraph {")
    assert "path 1 of 1" in result.stderr


def test_the_diagram_options_are_refused_without_highlight(runner: CliRunner) -> None:
    for flag, value in (("-f", "svg"), ("-o", "x.dot")):
        result = invoke(runner, "campus", "pc-north-01", "pc-north-02", flag, value)
        assert result.exit_code == 2
        assert "add --highlight to draw one" in result.output


def test_a_cleartext_tunnel_is_warned_about_on_stderr(runner: CliRunner, tmp_path: Path) -> None:
    root = tmp_path / "inv"
    root.mkdir()
    inventory_of(
        root,
        device(
            "rtr-a",
            port("eth0", "198.51.100.1/30"),
            "{name: gre0, type: tunnel, parent: eth0, ipv4: [10.0.0.1/30]}",
            kind="router",
        ),
        device(
            "rtr-b",
            port("eth0", "198.51.100.2/30"),
            "{name: gre0, type: tunnel, parent: eth0, ipv4: [10.0.0.2/30]}",
            kind="router",
        ),
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: tunnel\n"
        "metadata: {name: gre-open}\n"
        "spec: {type: gre, endpoints: [rtr-a:gre0, rtr-b:gre0]}\n",
    )
    result = CliRunner().invoke(
        cli, ["-i", str(root), "path", "--force", "10.0.0.1", "10.0.0.2"], catch_exceptions=False
    )
    assert "encrypts nothing" in result.output
    assert "W127" in result.output


def test_an_inventory_with_errors_is_refused_without_force(
    runner: CliRunner, tmp_path: Path
) -> None:
    root = tmp_path / "broken"
    root.mkdir()
    (root / "inv.yaml").write_text(
        "---".join(
            [
                device("pc-a", port("eth0")),
                cable("dangling", "pc-a:eth0", "ghost:eth0"),
            ]
        ),
        encoding="utf-8",
    )
    result = runner.invoke(cli, ["-i", str(root), "path", "pc-a", "pc-a"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "refusing to trace an inventory with errors" in result.output


# --------------------------------------------------------------------------- #
# Golden files
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    """One trace over one example, reported both ways."""

    name: str
    example: str
    source: str
    destination: str
    vlan: int | None = None
    all_paths: bool = False
    #: Also keep a DOT golden of the highlighted rendering.
    highlight: bool = False


#: Chosen so that every shape the report can take is pinned by at least one
#: file: a switched path, a routed one, a redundant pair, an overlay hop with a
#: nested stack, an adapter attachment, and a trace that finds nothing.
CASES = (
    Case("home-lab-adapter", "home-lab", "laptop", "pc-desk", highlight=True),
    Case("campus-l2", "campus", "pc-north-01", "pc-north-02"),
    Case("campus-l3", "campus", "10.1.10.51", "10.2.20.11", all_paths=True),
    Case(
        "campus-none",
        "campus",
        "pc-north-01",
        "sw-north-acc-01:GigabitEthernet1/0/3",
    ),
    Case("overlay-vxlan", "overlay", "rtr-hq", "rtr-branch-b", vlan=100),
    Case("overlay-l3", "overlay", "pc-branch-a", "srv-hq"),
)

INVENTORIES: dict[str, Inventory] = {}


def _inventory(example: str) -> Inventory:
    if example not in INVENTORIES:
        inventory = load_tree(EXAMPLES / example)
        assert inventory.errors == [], f"{example} does not load cleanly"
        INVENTORIES[example] = inventory
    return INVENTORIES[example]


def _result(case: Case) -> TraceResult:
    return trace(_inventory(case.example), case.source, case.destination, vlan=case.vlan)


def _rendered(case: Case, suffix: str) -> str:
    if suffix == ".dot":
        result = _result(case)
        graph = build_graph(_inventory(case.example), layer=result.layer or Layer.L1)
        options = RenderOptions(highlight=result.highlight(all_paths=case.all_paths))
        return render_text(graph, "dot", options)
    return render_trace(_result(case), suffix.lstrip("."), all_paths=case.all_paths)


def _suffixes(case: Case) -> tuple[str, ...]:
    return (".txt", ".json", ".dot") if case.highlight else (".txt", ".json")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_trace_matches_its_golden_file(case: Case, regen_golden: bool) -> None:
    for suffix in _suffixes(case):
        golden = GOLDEN_DIR / f"{case.name}{suffix}"
        actual = _rendered(case, suffix)
        if regen_golden:
            # ``netgraph.fsio.write_text`` rather than ``Path.write_text``: a
            # golden is a byte-for-byte artefact, and regenerating one on Windows
            # through Python's text mode would rewrite every line ending in the
            # file. See ``.gitattributes``, which keeps the committed copies at
            # LF for the same reason.
            write_text(golden, actual)
            continue
        assert golden.exists(), (
            f"missing golden {golden.relative_to(REPO_ROOT)}; "
            f"create it with 'pytest tests/test_path.py --regen-golden'"
        )
        assert actual == golden.read_text(encoding="utf-8"), (
            f"the {suffix} report for {case.name} drifted from its golden file. "
            f"If the change is intended, rerun with --regen-golden and review the diff."
        )
    if regen_golden:
        pytest.skip(f"regenerated the goldens for {case.name}")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_trace_is_reproducible(case: Case) -> None:
    assert _rendered(case, ".txt") == _rendered(case, ".txt")


def test_no_stray_golden_files() -> None:
    """Every committed snapshot belongs to a case, so a rename leaves no orphan."""
    expected = {GOLDEN_DIR / f"{case.name}{suffix}" for case in CASES for suffix in _suffixes(case)}
    actual = {path for path in GOLDEN_DIR.iterdir() if path.suffix != ".md"}
    assert actual == expected


def test_goldens_are_free_of_machine_specific_paths() -> None:
    """A snapshot carrying an absolute path would fail on another checkout."""
    for case in CASES:
        for suffix in _suffixes(case):
            text = (GOLDEN_DIR / f"{case.name}{suffix}").read_text(encoding="utf-8")
            assert str(REPO_ROOT) not in text
            assert "/root/" not in text


def test_the_documented_path_example_is_what_netgraph_produces() -> None:
    """The worked example in the tutorial, traced rather than typed.

    A sample of output in a document is a promise about the tool, and the only
    kind of promise that survives a refactor is one a test makes.
    """
    guide = (REPO_ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    block = guide.partition("<!-- path-example -->")[2]
    documented = block.partition("$ netgraph path pc-alice rtr-gw\n")[2].partition("```")[0]
    assert documented, "the path example is missing from docs/getting-started.md"

    produced = render_trace(trace(load_tree(EXAMPLES / "quickstart"), "pc-alice", "rtr-gw"), "text")
    assert produced == documented
