"""The shipped example inventories and the one-rule-per-file invalid fixtures.

Three properties are asserted here, and each one protects a different promise:

* ``examples/`` loads with no schema error and validates with **no findings at
  all**. The README of each example says so; this makes the claim testable.
* ``tests/fixtures/invalid/`` holds exactly one file per rule in
  :data:`netgraph.rules.RULE_IDS`, and each file triggers exactly that one
  finding. A new rule without a fixture fails here rather than shipping
  untested.
* Both examples reach a rendered SVG, so a topology that the validator likes
  but Graphviz cannot lay out does not slip through.
"""

from __future__ import annotations

import html
import itertools
from pathlib import Path

import pytest

from netgraph.loader import Inventory, load_tree, namespace_of
from netgraph.models import Adapter, Device, PatchPanel
from netgraph.rules import RULE_IDS
from netgraph.validate import validate

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
INVALID_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "invalid"

#: The inventories under ``examples/``, and what each one is expected to hold.
#: Pinning the counts keeps a stray file or a lost document visible.
EXAMPLE_SHAPES: dict[str, dict[str, int]] = {
    "quickstart": {"devices": 3, "cables": 2, "adapters": 0, "tunnels": 0},
    "home-lab": {"devices": 7, "cables": 6, "adapters": 1, "tunnels": 0},
    "campus": {"devices": 22, "cables": 22, "adapters": 0, "tunnels": 0},
    "overlay": {"devices": 7, "cables": 6, "adapters": 0, "tunnels": 5},
    "patch-room": {
        "devices": 8,
        "cables": 14,
        "adapters": 0,
        "tunnels": 0,
        "patchpanels": 2,
        "pdus": 4,
    },
}

#: ``e002-double-termination.yaml`` -> ``E002``.
INVALID_FILES: dict[str, Path] = {
    path.name.split("-")[0].upper(): path for path in sorted(INVALID_FIXTURES.glob("*.yaml"))
}


def load_example(name: str) -> Inventory:
    """Load one inventory from ``examples/``, insisting it parsed cleanly."""
    inventory = load_tree(EXAMPLES / name)
    assert inventory.errors == [], "\n".join(str(error) for error in inventory.errors)
    return inventory


# --------------------------------------------------------------------------- #
# The example inventories
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(EXAMPLE_SHAPES))
def test_an_example_inventory_loads_without_errors(name: str) -> None:
    inventory = load_example(name)
    shape = EXAMPLE_SHAPES[name]
    assert len(inventory.devices) == shape["devices"]
    assert len(inventory.cables) == shape["cables"]
    assert len(inventory.adapters) == shape["adapters"]
    assert len(inventory.tunnels) == shape["tunnels"]
    assert len(inventory.patchpanels) == shape.get("patchpanels", 0)
    assert len(inventory.pdus) == shape.get("pdus", 0)


@pytest.mark.parametrize("name", sorted(EXAMPLE_SHAPES))
def test_an_example_inventory_validates_clean(name: str) -> None:
    """No findings at all — not merely no errors, and with no suppressions."""
    findings = validate(load_example(name))
    assert findings == [], "\n".join(str(finding) for finding in findings)


@pytest.mark.parametrize("name", sorted(EXAMPLE_SHAPES))
def test_every_cable_endpoint_resolves(name: str) -> None:
    """The graph layer may assume an endpoint reference always lands somewhere."""
    inventory = load_example(name)
    for fqn, cable in inventory.cables.items():
        namespace = namespace_of(fqn)
        for ref in cable.endpoints:
            owner = inventory.resolve(ref.device, namespace=namespace)
            # A patch-panel position terminates a cable exactly as a device
            # port does (§15.1), so it is a legal endpoint too.
            assert isinstance(owner, (Device, Adapter, PatchPanel)), (
                f"{fqn}: {ref} names no cableable element"
            )
            # interface_names() includes an adapter's upstream port, which is a
            # legal endpoint even though it is not in spec.interfaces.
            assert ref.interface in set(owner.interface_names()), f"{fqn}: {ref}"


def test_the_home_lab_covers_every_element_kind_it_advertises() -> None:
    inventory = load_example("home-lab")
    kinds = sorted({element.kind for element in inventory})
    assert kinds == ["adapter", "cable", "computer", "router", "server", "switch"]


def test_the_home_lab_puts_two_ssids_on_one_access_point() -> None:
    """The wireless side of the example, as ``examples/home-lab/README.md`` says."""
    inventory = load_example("home-lab")
    radio = inventory.devices["wireless/ap-home"].spec.interface("wlan0")
    assert radio is not None and radio.wireless is not None
    assert radio.wireless.role.is_ap
    assert radio.wireless.channel_text == "36/5GHz"
    assert radio.wireless.ssids == ("home", "home-guest")
    # Each SSID lands in a VLAN the uplink trunk carries, which is NG-W009.
    assert [entry.vlan for entry in radio.wireless.bss] == [10, 20]

    phone = inventory.devices["hosts/phone"].spec.interface("en0")
    assert phone is not None and phone.wireless is not None
    assert not phone.wireless.role.is_ap
    assert phone.wireless.ssids == ("home",)


def test_the_home_lab_joins_the_laptop_through_the_adapter() -> None:
    """The laptop owns no cabled port; ``attached_to`` is what connects it."""
    inventory = load_example("home-lab")
    adapter = inventory.adapters["hosts/adp-usb-eth"]
    assert adapter.upstream.attached_to == "laptop"
    assert inventory.resolve_fqn("laptop", namespace="hosts") == "hosts/laptop"

    cabled = {ref.device for cable in inventory.cables.values() for ref in cable.endpoints}
    assert "laptop" not in cabled, "an attached_to host must not also be cabled (NG-X005)"


def test_the_campus_nests_namespaces_three_deep_across_three_sites() -> None:
    inventory = load_example("campus")
    namespaces = set(inventory.namespaces)
    for site in ("north", "south", "west"):
        assert f"sites/{site}/core" in namespaces
        assert f"sites/{site}/distribution" in namespaces
        assert f"sites/{site}/access" in namespaces
        assert f"sites/{site}/hosts" in namespaces
        assert f"sites/{site}/cables" in namespaces
        assert f"sites/{site}/core/rtr-{site}-core-01" in inventory.devices


def test_the_campus_core_routers_forward_ip() -> None:
    inventory = load_example("campus")
    for site in ("north", "south", "west"):
        router = inventory.devices[f"sites/{site}/core/rtr-{site}-core-01"]
        assert router.kind == "router"
        forwarding = router.spec.forwarding
        assert forwarding is not None and forwarding.ipv4 and forwarding.ipv6


def test_the_campus_trunks_access_to_distribution() -> None:
    """Both ends of an access uplink are trunk ports carrying the same VLANs."""
    inventory = load_example("campus")
    for site in ("north", "south", "west"):
        distribution = inventory.devices[f"sites/{site}/distribution/sw-{site}-dist-01"]
        for access_id, port in (("01", "Ethernet49/1"), ("02", "Ethernet50/1")):
            access = inventory.devices[f"sites/{site}/access/sw-{site}-acc-{access_id}"]
            uplink = access.interface("TenGigabitEthernet1/1/1")
            downlink = distribution.interface(port)
            assert uplink is not None and uplink.vlan is not None
            assert downlink is not None and downlink.vlan is not None
            assert uplink.vlan.mode.value == "trunk"
            assert downlink.vlan.mode.value == "trunk"
            assert uplink.vlan.vlan_ids() == downlink.vlan.vlan_ids()


def test_the_campus_backbone_and_uplinks_run_over_fibre() -> None:
    inventory = load_example("campus")
    fibre = {fqn for fqn, cable in inventory.cables.items() if cable.spec.medium.value == "fiber"}
    # Three backbone links plus, per site, one core uplink and two access trunks,
    # plus the third North access trunk on the templated switch.
    assert len(fibre) == 13
    assert {
        "backbone/cbl-bb-north-south",
        "backbone/cbl-bb-south-west",
        "backbone/cbl-bb-west-north",
    } <= fibre
    for site in ("north", "south", "west"):
        assert f"sites/{site}/cables/cbl-{site}-core-dist" in fibre
        assert f"sites/{site}/cables/cbl-{site}-dist-acc01" in fibre
        assert f"sites/{site}/cables/cbl-{site}-dist-acc02" in fibre


# --------------------------------------------------------------------------- #
# The invalid fixtures
# --------------------------------------------------------------------------- #


def test_there_is_one_invalid_fixture_per_rule() -> None:
    assert sorted(INVALID_FILES) == sorted(RULE_IDS)


@pytest.mark.parametrize("rule_id", sorted(INVALID_FILES))
def test_an_invalid_fixture_is_schema_valid(rule_id: str) -> None:
    """The fixtures exercise *semantic* rules, so they must parse cleanly."""
    inventory = load_tree(INVALID_FILES[rule_id])
    assert inventory.errors == [], "\n".join(str(error) for error in inventory.errors)


@pytest.mark.parametrize("rule_id", sorted(INVALID_FILES))
def test_an_invalid_fixture_triggers_exactly_its_own_rule(rule_id: str) -> None:
    findings = validate(load_tree(INVALID_FILES[rule_id]))
    assert [finding.rule for finding in findings] == [rule_id], "\n".join(
        str(finding) for finding in findings
    )


@pytest.mark.parametrize("rule_id", sorted(INVALID_FILES))
def test_an_invalid_fixture_names_the_elements_it_blames(rule_id: str) -> None:
    inventory = load_tree(INVALID_FILES[rule_id])
    (finding,) = validate(inventory)
    assert finding.elements, "a finding must name something suppressible"
    # A layout document (§18) is not an element, but ``W138`` is reported
    # against one and its annotations do suppress the finding, so it counts.
    assert all(fqn in inventory or fqn in inventory.layouts for fqn in finding.elements)
    assert finding.source is not None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render_svg(inventory: Inventory) -> str:
    """Lay the inventory out with Graphviz and return the SVG.

    A deliberately minimal stand-in for the real renderer, which lands in a
    later step: devices and adapters become nodes, cables and ``attached_to``
    attachments become edges. It exists to prove that the example topologies
    survive an actual layout pass, not to define the output format.

    Cables and tunnels are both *links*, so neither becomes a node here — the
    real renderer draws a multipoint tunnel as one, but reproducing that rule
    would make this helper a second implementation of the thing it is meant to
    be independent of.
    """
    import graphviz

    graph = graphviz.Graph("netgraph")
    for fqn, element in inventory.elements.items():
        if fqn in inventory.cables or fqn in inventory.tunnels:
            continue
        graph.node(fqn, label=element.metadata.name)

    for fqn, cable in inventory.cables.items():
        namespace = namespace_of(fqn)
        ends = [inventory.resolve_fqn(ref.device, namespace=namespace) for ref in cable.endpoints]
        left, right = ends
        assert left is not None and right is not None
        graph.edge(left, right, label=cable.metadata.name)

    for fqn, adapter in inventory.adapters.items():
        host = adapter.upstream.attached_to
        if host is None:
            continue
        host_fqn = inventory.resolve_fqn(host, namespace=namespace_of(fqn))
        assert host_fqn is not None
        graph.edge(host_fqn, fqn, style="dashed", label=adapter.upstream.type.value)

    for fqn, tunnel in inventory.tunnels.items():
        namespace = namespace_of(fqn)
        ends = [inventory.resolve_fqn(ref.device, namespace=namespace) for ref in tunnel.endpoints]
        assert all(end is not None for end in ends)
        # A point-to-multipoint tunnel is a chain here rather than a star; the
        # count the caller asserts is what matters, not the shape.
        for left, right in itertools.pairwise(ends):
            graph.edge(str(left), str(right), style="dotted", label=tunnel.metadata.name)

    return str(graph.pipe(format="svg", encoding="utf-8"))


@requires_dot
@pytest.mark.parametrize("name", sorted(EXAMPLE_SHAPES))
def test_an_example_inventory_renders_to_svg(name: str) -> None:
    inventory = load_example(name)
    svg = _render_svg(inventory)

    assert "<svg" in svg
    assert svg.rstrip().endswith("</svg>")
    # One node per element that is neither a cable nor a tunnel — a patch panel
    # and a PDU are each one, because this helper draws the *physical* reading:
    # a panel is where two cable segments meet and a PDU is where a power cord
    # ends. One edge per cable, per adapter attachment, and per leg of a tunnel.
    shape = EXAMPLE_SHAPES[name]
    legs = sum(len(tunnel.endpoints) - 1 for tunnel in inventory.tunnels.values())
    assert svg.count('class="node"') == (
        shape["devices"] + shape["adapters"] + shape.get("patchpanels", 0) + shape.get("pdus", 0)
    )
    assert svg.count('class="edge"') == shape["cables"] + shape["adapters"] + legs
    # Graphviz writes a hyphen as the character reference '&#45;'.
    text = html.unescape(svg)
    for device in inventory.devices.values():
        assert device.metadata.name in text
