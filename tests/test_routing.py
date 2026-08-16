"""Routing: VRFs, static routes, adjacencies, and the view over them.

Everything §16 adds, in the five places it has to hold together:

* the **model** — the route-distinguisher and OSPF-area grammars, the
  next-hop/prefix family rule, the "a route needs somewhere to send the packet"
  rule and the VRF references inside one ``spec``, each reported at schema time
  with an ``NG-F*`` id and the path of the offending value;
* the **address space** — that a VRF partitions it: :mod:`netviz.subnets`
  groups per instance, ``E004``/``W111`` stop firing across instances, and
  :mod:`netviz.ipam` sizes and aggregates per instance;
* the **validator** — the seven cross-document rules, each on an inventory that
  differs from a clean one in exactly the way the rule is about, so a finding
  cannot be an accident of the fixture;
* the **graph and the renderers** — that a BGP session is resolved by *address*,
  that an OSPF adjacency is derived from the addressing rather than declared,
  and that DOT, Mermaid and JSON say the same thing about both;
* the **CLI and the export** — the ``routing`` layer end to end, and the
  iproute2 script.

``tests/fixtures/invalid/`` holds one file per rule and ``tests/test_examples.py``
insists each fires exactly once there; the inventories here are built inline so a
test can vary one field at a time.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from netviz.cli import cli
from netviz.errors import SchemaError
from netviz.export import EXPORTERS
from netviz.ipam import aggregate, free_space, utilisation_of
from netviz.loader import Inventory, load_tree
from netviz.models import (
    API_VERSION,
    GLOBAL_VRF,
    BgpConfig,
    OspfConfig,
    PolicyAction,
    PolicyRule,
    StaticRoute,
    VrfDefinition,
    parse_document,
)
from netviz.models.routing import (
    AddressFamily,
    normalise_area,
    normalise_fwmark,
    normalise_rd,
)
from netviz.render.details import build_details, detail_text
from netviz.render.dot import to_dot
from netviz.render.graph import EdgeKind, Layer, build_graph
from netviz.render.ids import element_ids
from netviz.render.jsonexport import graph_to_dict
from netviz.render.mermaid import to_mermaid
from netviz.render.options import RenderOptions
from netviz.subnets import subnets_of
from netviz.validate import validate

from platform_marks import requires_posix_shell  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
CAMPUS = REPO_ROOT / "examples" / "campus"


# --------------------------------------------------------------------------- #
# Building inventories
# --------------------------------------------------------------------------- #


def device(name: str, *interfaces: dict[str, Any], **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": spec.pop("kind", "router"),
        "metadata": {"name": name},
        "spec": {"interfaces": list(interfaces), **spec},
    }


def port(name: str, *addresses: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "type": "ethernet", "mtu": 1500, **fields}
    if addresses:
        entry["ipv4"] = [address for address in addresses if ":" not in address]
        v6 = [address for address in addresses if ":" in address]
        if v6:
            entry["ipv6"] = v6
        if not entry["ipv4"]:
            del entry["ipv4"]
    return entry


def cable(name: str, left: str, right: str) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": "cable",
        "metadata": {"name": name},
        "spec": {"endpoints": [left, right], "medium": "copper"},
    }


def inventory_of(root: Path, *documents: dict[str, Any]) -> Inventory:
    """Load an inventory made of the given documents, insisting it parses."""
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
    """``(rule, last path component)`` per issue, which is what a test asserts on."""
    return [(issue.rule, str(issue.path[-1]) if issue.path else "") for issue in exc.value.issues]


def pair(left: dict[str, Any], right: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Two devices and the cable between their ``eth0`` ports."""
    return (
        left,
        right,
        cable("cbl", f"{left['metadata']['name']}:eth0", f"{right['metadata']['name']}:eth0"),
    )


@pytest.fixture
def peers(tmp_path: Path) -> Inventory:
    """Two routers, one link, an eBGP session and an OSPF adjacency. Clean."""
    return inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("lo0", "192.0.2.1/32", type="loopback", mtu=None),
                port("eth0", "10.0.0.1/30"),
                routing={
                    "ospf": {"area": 0, "router_id": "192.0.2.1", "interfaces": ["lo0", "eth0"]},
                    "bgp": {
                        "asn": 65001,
                        "router_id": "192.0.2.1",
                        "neighbors": [{"address": "10.0.0.2", "remote_asn": 65002}],
                    },
                },
            ),
            device(
                "rtr-b",
                port("lo0", "192.0.2.2/32", type="loopback", mtu=None),
                port("eth0", "10.0.0.2/30"),
                routing={
                    "ospf": {"area": "0.0.0.0", "router_id": "192.0.2.2", "interfaces": ["eth0"]},
                    "bgp": {"asn": 65002, "router_id": "192.0.2.2"},
                },
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# The scalars: route distinguishers and OSPF areas
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    ["65000:1", "0:0", "192.0.2.1:1", "4200000000:1", "65535:4294967295", "4200000000:65535"],
)
def test_a_route_distinguisher_of_every_encoding_is_accepted(value: str) -> None:
    """The three RFC 4364 §4.2 types, at both ends of each one's range."""
    assert normalise_rd(value) == value


@pytest.mark.parametrize(
    ("value", "complaint"),
    [
        ("65000", "not a route distinguisher"),
        ("65000:1:2", "not a route distinguisher"),
        ("a:1", "not a route distinguisher"),
        ("4294967296:1", "not an AS number"),
        # Type 2 (four-byte AS) gives the assigned number only two bytes.
        ("4200000000:65536", "out of range"),
        # Type 1 (IPv4) the same.
        ("192.0.2.1:65536", "out of range"),
        ("192.0.2.256:1", "not an IPv4 address"),
    ],
)
def test_a_malformed_route_distinguisher_says_which_half_is_wrong(
    value: str, complaint: str
) -> None:
    with pytest.raises(ValueError, match=complaint):
        normalise_rd(value)


def test_an_unquoted_route_distinguisher_says_to_quote_it() -> None:
    """YAML 1.1 reads ``65000:59`` as a number; the digits cannot be recovered."""
    with pytest.raises(ValueError, match="quote it in the YAML document"):
        normalise_rd(3900059)


@pytest.mark.parametrize(
    ("written", "stored"),
    [(0, "0.0.0.0"), ("0", "0.0.0.0"), ("0.0.0.0", "0.0.0.0"), (1, "0.0.0.1"), (256, "0.0.1.0")],
)
def test_an_ospf_area_normalises_to_a_dotted_quad(written: Any, stored: str) -> None:
    """Both spellings of the backbone area compare equal once stored."""
    assert normalise_area(written) == stored


@pytest.mark.parametrize("written", [-1, 4294967296, "not-an-area", True, 1.5])
def test_a_value_that_is_no_ospf_area_is_refused(written: Any) -> None:
    with pytest.raises(ValueError):
        normalise_area(written)


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


def test_a_route_records_what_it_was_given() -> None:
    route = StaticRoute(prefix="10.0.0.0/8", via="10.1.0.1", dev="eth0", metric=10)  # type: ignore[arg-type]
    assert route.family == "ipv4"
    assert not route.is_default
    assert route.describe() == "10.0.0.0/8 via 10.1.0.1 dev eth0 metric 10"


def test_a_default_route_says_so() -> None:
    assert StaticRoute(prefix="0.0.0.0/0", blackhole=True).is_default  # type: ignore[arg-type]
    assert StaticRoute(prefix="::/0", blackhole=True).is_default  # type: ignore[arg-type]


def test_a_route_prefix_with_host_bits_is_refused() -> None:
    """A destination with host bits is a typo or a /32; netviz will not guess."""
    with pytest.raises(ValueError):
        StaticRoute(prefix="10.0.0.1/24", blackhole=True)  # type: ignore[arg-type]


def test_a_next_hop_of_the_other_family_is_ng_f003() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "rtr", port("eth0", "10.0.0.1/30"), routes=[{"prefix": "::/0", "via": "10.0.0.2"}]
            )
        )
    assert ("NG-F003", "via") in issues(exc)


def test_a_route_that_says_nothing_is_ng_f004() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("rtr", port("eth0", "10.0.0.1/30"), routes=[{"prefix": "0.0.0.0/0"}]))
    assert ("NG-F004", "via") in issues(exc)


@pytest.mark.parametrize("key", ["via", "dev"])
def test_a_blackhole_route_with_an_egress_is_ng_f004(key: str) -> None:
    """A route that discards has no next hop and no way out."""
    route = {
        "prefix": "10.9.0.0/24",
        "blackhole": True,
        key: "10.0.0.2" if key == "via" else "eth0",
    }
    with pytest.raises(SchemaError) as exc:
        parse_document(device("rtr", port("eth0", "10.0.0.1/30"), routes=[route]))
    assert ("NG-F004", key) in issues(exc)


def test_two_vrfs_of_one_name_are_ng_f001() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "rtr",
                port("eth0", "10.0.0.1/30"),
                vrfs=[{"name": "blue", "rd": "65001:1"}, {"name": "blue", "rd": "65001:2"}],
            )
        )
    assert ("NG-F001", "name") in issues(exc)


def test_an_interface_binding_to_an_undeclared_vrf_is_ng_f002() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("rtr", port("eth0", "10.0.0.1/30", vrf="blue")))
    assert ("NG-F002", "vrf") in issues(exc)
    assert "declares no 'spec.vrfs' at all" in str(exc.value)


def test_the_vrfs_a_device_does_declare_are_named_in_the_complaint() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "rtr",
                port("eth0", "10.0.0.1/30", vrf="blu"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
            )
        )
    assert "'blue'" in str(exc.value)


def test_a_route_in_an_undeclared_vrf_is_ng_f005() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "rtr",
                port("eth0", "10.0.0.1/30"),
                routes=[{"prefix": "10.9.0.0/24", "blackhole": True, "vrf": "blue"}],
            )
        )
    assert ("NG-F005", "vrf") in issues(exc)


def test_an_ospf_interface_listed_twice_is_ng_f006() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "rtr",
                port("eth0", "10.0.0.1/30"),
                routing={"ospf": {"interfaces": ["eth0", "eth0"]}},
            )
        )
    assert exc.value.issues[0].rule == "NG-F006"


def test_an_ospf_block_with_no_interface_is_refused() -> None:
    with pytest.raises(SchemaError):
        parse_document(
            device("rtr", port("eth0", "10.0.0.1/30"), routing={"ospf": {"interfaces": []}})
        )


def test_one_neighbour_address_declared_twice_is_ng_f007() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "rtr",
                port("eth0", "10.0.0.1/30"),
                routing={
                    "bgp": {
                        "asn": 65001,
                        "neighbors": [
                            {"address": "10.0.0.2", "remote_asn": 65002},
                            {"address": "10.0.0.2", "remote_asn": 65003},
                        ],
                    }
                },
            )
        )
    assert ("NG-F007", "address") in issues(exc)


@pytest.mark.parametrize("asn", [0, 4294967296])
def test_a_reserved_or_oversized_as_number_is_refused(asn: int) -> None:
    with pytest.raises(ValueError):
        BgpConfig(asn=asn)


def test_a_hub_declares_no_routing() -> None:
    """A layer-1 repeater has no IP stack, so it has no routing table either."""
    for key, value in (
        ("vrfs", [{"name": "blue", "rd": "65001:1"}]),
        ("routes", [{"prefix": "0.0.0.0/0", "blackhole": True}]),
        ("routing", {"ospf": {"interfaces": ["eth0"]}}),
    ):
        with pytest.raises(SchemaError) as exc:
            parse_document(
                device("hub", {"name": "eth0", "type": "ethernet"}, kind="hub", **{key: value})
            )
        assert ("NG-H003", key) in issues(exc)


def test_an_adapter_interface_binds_to_no_vrf() -> None:
    """The routing instance belongs to the host the adapter hangs off (§16.1)."""
    with pytest.raises(SchemaError) as exc:
        parse_document(
            {
                "apiVersion": API_VERSION,
                "kind": "adapter",
                "metadata": {"name": "dongle"},
                "spec": {
                    "upstream": {"name": "usb0", "type": "usb"},
                    "interfaces": [port("eth0", "10.0.0.1/30", vrf="blue")],
                },
            }
        )
    assert ("NG-F002", "vrf") in issues(exc)


def test_a_device_looks_its_own_vrfs_up() -> None:
    element = parse_document(
        device(
            "rtr",
            port("eth0", "10.0.0.1/30", vrf="blue"),
            port("eth1", "10.0.1.1/30"),
            vrfs=[{"name": "blue", "rd": "65001:1"}],
        )
    )
    found = element.spec.vrf("blue")
    assert isinstance(found, VrfDefinition) and found.rd == "65001:1"
    assert element.spec.vrf("green") is None
    assert [entry.name for entry in element.spec.interfaces_in("blue")] == ["eth0"]
    assert [entry.name for entry in element.spec.interfaces_in(GLOBAL_VRF)] == ["eth1"]


def test_a_routing_block_reports_its_router_ids_once() -> None:
    """One device giving OSPF and BGP one id is one identity, not a duplicate."""
    element = parse_document(
        device(
            "rtr",
            port("eth0", "10.0.0.1/30"),
            routing={
                "ospf": {"router_id": "192.0.2.1", "interfaces": ["eth0"]},
                "bgp": {"asn": 65001, "router_id": "192.0.2.1"},
            },
        )
    )
    routing = element.spec.routing
    assert routing is not None
    assert [str(value) for value in routing.router_ids] == ["192.0.2.1"]
    assert not routing.is_empty


def test_an_empty_routing_block_says_it_is_empty() -> None:
    assert OspfConfig(interfaces=["eth0"]).area == "0.0.0.0"
    element = parse_document(device("rtr", port("eth0", "10.0.0.1/30"), routing={}))
    assert element.spec.routing is not None and element.spec.routing.is_empty


# --------------------------------------------------------------------------- #
# A VRF partitions the address space
# --------------------------------------------------------------------------- #


@pytest.fixture
def two_instances(tmp_path: Path) -> Inventory:
    """One prefix, twice: once in the global table and once in ``blue``.

    The same *address* on both, which is what makes this the fixture for
    ``E004``: were the two in one instance it would be a duplicate.
    """
    return inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                port("eth1", "10.0.0.1/30", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
            ),
            device(
                "rtr-b",
                port("eth0", "10.0.0.2/30"),
                port("eth1", "10.0.0.2/30", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
            ),
        ),
    )


def test_a_prefix_in_two_instances_is_two_subnets(two_instances: Inventory) -> None:
    subnets = subnets_of(two_instances)
    assert [(subnet.vrf, subnet.prefix) for subnet in subnets] == [
        (GLOBAL_VRF, "10.0.0.0/30"),
        ("blue", "10.0.0.0/30"),
    ]
    # The global instance leads, whatever the interfaces' declaration order.
    assert subnets[0].sort_key < subnets[1].sort_key
    assert subnets[0].label == "10.0.0.0/30"
    assert subnets[1].label == "10.0.0.0/30 (vrf blue)"


def test_one_address_in_two_instances_is_not_a_duplicate(two_instances: Inventory) -> None:
    """``E004`` partitions by instance: a VRF is what makes the reuse legal."""
    assert "E004" not in rules_of(two_instances)


def test_one_address_twice_in_one_instance_is_still_e004(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
            ),
            device(
                "rtr-b",
                port("eth0", "10.0.0.1/30", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
            ),
        ),
    )
    findings = [finding for finding in validate(inventory) if finding.rule == "E004"]
    assert len(findings) == 1
    assert "of VRF 'blue'" in findings[0].message


def test_overlapping_prefixes_in_two_instances_are_not_w111(tmp_path: Path) -> None:
    """Each instance has a routing table, so there is no ambiguous egress."""
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/24"),
                port("eth1", "10.0.0.1/24", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/24")),
        ),
    )
    assert "W111" not in rules_of(inventory)


def test_overlapping_prefixes_in_one_instance_are_still_w111(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device("rtr-a", port("eth0", "10.0.0.1/24"), port("eth1", "10.0.0.2/16")),
            device("rtr-b", port("eth0", "10.0.0.3/24")),
        ),
    )
    assert "W111" in rules_of(inventory)


def test_ipam_sizes_each_instance_separately(two_instances: Inventory) -> None:
    rows = utilisation_of(subnets_of(two_instances))
    assert [(row.vrf, row.prefix, row.assigned) for row in rows] == [
        (GLOBAL_VRF, "10.0.0.0/30", 2),
        ("blue", "10.0.0.0/30", 2),
    ]
    assert rows[0].record()["vrf"] == ""
    assert rows[1].record()["vrf"] == "blue"


def test_aggregate_never_merges_across_instances(tmp_path: Path) -> None:
    """Two halves of a supernet in two tables do not fill it."""
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/25"),
                port("eth1", "10.0.0.129/25", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/25")),
        ),
    )
    rows = aggregate(utilisation_of(subnets_of(inventory)))
    assert [(row.vrf, row.prefix) for row in rows] == [
        (GLOBAL_VRF, "10.0.0.0/25"),
        ("blue", "10.0.0.128/25"),
    ]


def test_free_space_can_be_asked_per_instance(two_instances: Inventory) -> None:
    subnets = subnets_of(two_instances)
    prefix = subnets[0].network.supernet(new_prefix=24)
    # Both instances allocate the same /30, so scoping to one leaves the rest of
    # the /24 free either way; the unscoped answer is the same because the two
    # allocations coincide, which is exactly why the argument exists.
    assert free_space(prefix, subnets, vrf="blue") == free_space(prefix, subnets)
    assert all(block.prefixlen >= 25 for block in free_space(prefix, subnets, vrf="blue"))


# --------------------------------------------------------------------------- #
# The validator
# --------------------------------------------------------------------------- #


def test_the_peer_fixture_is_clean(peers: Inventory) -> None:
    """Every rule below is a single-field change away from this inventory."""
    assert rules_of(peers) == []


def test_a_next_hop_outside_every_prefix_is_e032(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routes=[{"prefix": "192.168.0.0/16", "via": "10.9.9.1"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    (finding,) = [entry for entry in validate(inventory) if entry.rule == "E032"]
    assert "10.0.0.0/30 in the global instance" in finding.message
    assert finding.field_path == ("spec", "routes", 0, "via")


def test_a_next_hop_in_another_vrf_is_not_on_link(tmp_path: Path) -> None:
    """A VRF is a routing table of its own, so its addresses are not reachable."""
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                port("eth1", "10.1.0.1/30", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
                # The next hop is on 'blue', the route is in the global table.
                routes=[{"prefix": "192.168.0.0/16", "via": "10.1.0.2"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    assert "E032" in rules_of(inventory)


def test_a_next_hop_in_its_own_vrf_is_on_link(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                port("eth1", "10.1.0.1/30", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
                routes=[{"prefix": "192.168.0.0/16", "via": "10.1.0.2", "vrf": "blue"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    assert "E032" not in rules_of(inventory)


def test_a_link_local_next_hop_is_on_link_by_definition(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "2001:db8::1/64"),
                routes=[{"prefix": "2001:db8:1::/48", "via": "fe80::1", "dev": "eth0"}],
            ),
            device("rtr-b", port("eth0", "2001:db8::2/64")),
        ),
    )
    assert "E032" not in rules_of(inventory)


def test_a_next_hop_on_another_interface_than_dev_is_e032(tmp_path: Path) -> None:
    """``dev`` narrows the check: the next hop is on that link or nowhere."""
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                port("eth1", "10.1.0.1/30", enabled=False),
                routes=[{"prefix": "192.168.0.0/16", "via": "10.1.0.2", "dev": "eth0"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    (finding,) = [entry for entry in validate(inventory) if entry.rule == "E032"]
    assert "interface 'eth0'" in finding.message


def test_a_device_with_no_address_of_the_family_says_so(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routes=[{"prefix": "2001:db8::/48", "via": "2001:db8::1"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    (finding,) = [entry for entry in validate(inventory) if entry.rule == "E032"]
    assert "configures no IPv6 address" in finding.message


def test_an_unknown_dev_is_e033_and_not_also_e032(tmp_path: Path) -> None:
    """One mistake, one finding: the on-link check is skipped for a bad ``dev``."""
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routes=[{"prefix": "192.168.0.0/16", "via": "10.9.9.1", "dev": "eth9"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    assert [entry for entry in rules_of(inventory) if entry.startswith("E03")] == ["E033"]


def test_an_ospf_interface_the_device_lacks_is_e034(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routing={"ospf": {"area": 0, "interfaces": ["eth0", "eth9"]}},
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    (finding,) = [entry for entry in validate(inventory) if entry.rule == "E034"]
    assert "'eth9'" in finding.message and "it has 'eth0'" in finding.message


def test_an_as_number_the_peer_disagrees_with_is_e035(peers: Inventory, tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path / "mismatch",
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routing={
                    "bgp": {
                        "asn": 65001,
                        "neighbors": [{"address": "10.0.0.2", "remote_asn": 65099}],
                    }
                },
            ),
            device(
                "rtr-b",
                port("eth0", "10.0.0.2/30"),
                routing={"bgp": {"asn": 65002}},
            ),
        ),
    )
    (finding,) = [entry for entry in validate(inventory) if entry.rule == "E035"]
    assert "as AS 65099" in finding.message and "declares AS 65002" in finding.message
    assert finding.elements == ("rtr-a", "rtr-b")


def test_a_peer_that_models_no_bgp_is_silent(tmp_path: Path) -> None:
    """An inventory may model the box without modelling its control plane."""
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routing={
                    "bgp": {
                        "asn": 65001,
                        "neighbors": [{"address": "10.0.0.2", "remote_asn": 65002}],
                    }
                },
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    assert rules_of(inventory) == []


def test_two_elements_claiming_one_router_id_is_e036(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routing={"ospf": {"router_id": "192.0.2.1", "interfaces": ["eth0"]}},
            ),
            device(
                "rtr-b",
                port("eth0", "10.0.0.2/30"),
                routing={"bgp": {"asn": 65002, "router_id": "192.0.2.1"}},
            ),
        ),
    )
    (finding,) = [entry for entry in validate(inventory) if entry.rule == "E036"]
    assert finding.elements == ("rtr-a", "rtr-b")


def test_one_device_reusing_its_own_router_id_is_not_e036(peers: Inventory) -> None:
    """OSPF and BGP sharing an id is one identity; the fixture does exactly that."""
    assert "E036" not in rules_of(peers)


def test_a_neighbour_nobody_is_addressed_at_is_w135(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routing={
                    "bgp": {
                        "asn": 65001,
                        "neighbors": [
                            {
                                "address": "198.51.100.9",
                                "remote_asn": 65002,
                                "description": "transit",
                            }
                        ],
                    }
                },
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    (finding,) = [entry for entry in validate(inventory) if entry.rule == "W135"]
    assert "(transit)" in finding.message
    assert finding.field_path == ("spec", "routing", "bgp", "neighbors", 0, "address")


def test_a_vrf_no_interface_is_bound_to_is_w136(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
                routes=[{"prefix": "10.9.0.0/24", "blackhole": True, "vrf": "blue"}],
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    (finding,) = [entry for entry in validate(inventory) if entry.rule == "W136"]
    assert "rd 65001:1" in finding.message
    assert "1 route placed in it" in finding.message


def test_a_bound_vrf_is_not_w136(two_instances: Inventory) -> None:
    assert "W136" not in rules_of(two_instances)


# --------------------------------------------------------------------------- #
# The routing view
# --------------------------------------------------------------------------- #


def test_the_routing_layer_draws_only_the_elements_that_route(peers: Inventory) -> None:
    graph = build_graph(peers, layer=Layer.ROUTING)
    assert sorted(graph.nodes) == ["rtr-a", "rtr-b"]
    assert all(node.routing is not None for node in graph.nodes.values())


def test_an_element_with_no_routing_state_is_left_out(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routes=[{"prefix": "10.9.0.0/24", "blackhole": True}],
            ),
            device("pc", port("eth0", "10.0.0.2/30"), kind="computer"),
        ),
    )
    graph = build_graph(inventory, layer=Layer.ROUTING)
    assert list(graph.nodes) == ["rtr-a"]


def test_a_bgp_session_is_drawn_once_however_often_it_is_declared(peers: Inventory) -> None:
    """Both ends may declare it; it is one session either way."""
    graph = build_graph(peers, layer=Layer.ROUTING)
    sessions = [edge for edge in graph.edges if edge.kind is EdgeKind.BGP]
    assert len(sessions) == 1
    (session,) = sessions
    assert {session.source, session.target} == {"rtr-a", "rtr-b"}
    assert session.label == "65001 → 65002"
    assert session.adjacency is not None and not session.adjacency.is_internal


def test_an_ibgp_session_says_so(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routing={
                    "bgp": {
                        "asn": 65001,
                        "neighbors": [{"address": "10.0.0.2", "remote_asn": 65001}],
                    }
                },
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30"), routing={"bgp": {"asn": 65001}}),
        ),
    )
    (session,) = build_graph(inventory, layer=Layer.ROUTING).edges
    assert session.label == "iBGP 65001"
    assert session.adjacency is not None and session.adjacency.is_internal


def test_a_session_pointed_at_nothing_is_dropped_rather_than_drawn(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routing={
                    "bgp": {
                        "asn": 65001,
                        "neighbors": [{"address": "198.51.100.9", "remote_asn": 65002}],
                    }
                },
            ),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    graph = build_graph(inventory, layer=Layer.ROUTING)
    assert [edge.kind for edge in graph.edges] == []
    assert any("198.51.100.9" in message for message in graph.dangling)


def test_an_ospf_adjacency_is_derived_from_the_addressing(peers: Inventory) -> None:
    """Nobody declares it: two OSPF interfaces in one subnet form one."""
    graph = build_graph(peers, layer=Layer.ROUTING)
    (adjacency,) = [edge for edge in graph.edges if edge.kind is EdgeKind.OSPF]
    assert {adjacency.source, adjacency.target} == {"rtr-a", "rtr-b"}
    assert adjacency.label == "area 0.0.0.0"
    assert {adjacency.source_port, adjacency.target_port} == {"eth0"}


def test_two_areas_form_no_adjacency(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                routing={"ospf": {"area": 0, "interfaces": ["eth0"]}},
            ),
            device(
                "rtr-b",
                port("eth0", "10.0.0.2/30"),
                routing={"ospf": {"area": 1, "interfaces": ["eth0"]}},
            ),
        ),
    )
    assert build_graph(inventory, layer=Layer.ROUTING).edges == ()


def test_a_shared_subnet_without_ospf_on_it_forms_no_adjacency(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30"),
                port("eth1", "10.1.0.1/30", enabled=False),
                routing={"ospf": {"area": 0, "interfaces": ["eth1"]}},
            ),
            device(
                "rtr-b",
                port("eth0", "10.0.0.2/30"),
                routing={"ospf": {"area": 0, "interfaces": ["eth0"]}},
            ),
        ),
    )
    assert build_graph(inventory, layer=Layer.ROUTING).edges == ()


def test_a_dual_stacked_link_is_one_adjacency(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30", "2001:db8::1/64"),
                routing={"ospf": {"area": 0, "interfaces": ["eth0"]}},
            ),
            device(
                "rtr-b",
                port("eth0", "10.0.0.2/30", "2001:db8::2/64"),
                routing={"ospf": {"area": 0, "interfaces": ["eth0"]}},
            ),
        ),
    )
    assert len(build_graph(inventory, layer=Layer.ROUTING).edges) == 1


def test_a_router_in_one_instance_is_clustered_by_it(two_instances: Inventory) -> None:
    graph = build_graph(two_instances, layer=Layer.ROUTING)
    assert graph.clusters == ("blue",)
    assert {node.fqn for node in graph.nodes_in_cluster("blue")} == {"rtr-a", "rtr-b"}


def test_a_router_straddling_two_instances_is_in_no_cluster(tmp_path: Path) -> None:
    inventory = inventory_of(
        tmp_path,
        *pair(
            device(
                "rtr-a",
                port("eth0", "10.0.0.1/30", vrf="blue"),
                port("eth1", "10.1.0.1/30", vrf="green", enabled=False),
                vrfs=[{"name": "blue", "rd": "65001:1"}, {"name": "green", "rd": "65001:2"}],
            ),
            device(
                "rtr-b",
                port("eth0", "10.0.0.2/30", vrf="blue"),
                vrfs=[{"name": "blue", "rd": "65001:1"}],
            ),
        ),
    )
    graph = build_graph(inventory, layer=Layer.ROUTING)
    assert graph.nodes["rtr-a"].cluster == ""
    assert graph.nodes["rtr-b"].cluster == "blue"
    assert graph.clusters == ("blue",)


def test_the_routing_view_keeps_the_static_routes_and_instances(two_instances: Inventory) -> None:
    graph = build_graph(two_instances, layer=Layer.ROUTING)
    view = graph.nodes["rtr-a"].routing
    assert view is not None
    assert view.vrfs == (("blue", "65001:1"),)
    assert view.bound_vrfs == ("blue",)
    assert not view.speaks_bgp and not view.speaks_ospf


def test_the_routing_view_can_be_filtered_like_any_other(peers: Inventory) -> None:
    from netviz.render.graph import FilterSpec, filter_graph

    graph = filter_graph(build_graph(peers, layer=Layer.ROUTING), FilterSpec(names=("rtr-a",)))
    assert list(graph.nodes) == ["rtr-a"]
    assert graph.edges == ()
    assert graph.nodes["rtr-a"].routing is not None


# --------------------------------------------------------------------------- #
# The renderers
# --------------------------------------------------------------------------- #


def test_dot_labels_a_router_with_its_as_and_id(peers: Inventory) -> None:
    source = to_dot(build_graph(peers, layer=Layer.ROUTING))
    assert "[router, AS 65001, id 192.0.2.1, ospf area 0.0.0.0]" in source
    assert 'label="65001 → 65002"' in source
    assert 'label="area 0.0.0.0"' in source


def test_dot_draws_a_session_solid_and_an_adjacency_dotted(peers: Inventory) -> None:
    """The one distinction the routing view exists to make, in the line style."""
    graph = build_graph(peers, layer=Layer.ROUTING)
    lines = [line for line in to_dot(graph).splitlines() if " -- " in line]
    session = next(line for line in lines if "65001 → 65002" in line)
    adjacency = next(line for line in lines if "area 0.0.0.0" in line)
    assert "style=solid" in session
    assert "style=dotted" in adjacency


def test_dot_boxes_each_vrf(two_instances: Inventory) -> None:
    source = to_dot(build_graph(two_instances, layer=Layer.ROUTING))
    assert 'label="vrf blue"' in source
    assert "subgraph cluster_vrf_0" in source


def test_dot_prefers_the_layers_own_grouping_over_namespaces(two_instances: Inventory) -> None:
    source = to_dot(
        build_graph(two_instances, layer=Layer.ROUTING),
        RenderOptions(group_by_namespace=True),
    )
    assert source.count("subgraph") == 1


def test_mermaid_carries_the_same_two_labels(peers: Inventory) -> None:
    source = to_mermaid(build_graph(peers, layer=Layer.ROUTING))
    assert '-- "65001 → 65002" ---' in source
    assert '-. "area 0.0.0.0" .-' in source
    assert "AS 65001<br/>id 192.0.2.1<br/>ospf area 0.0.0.0" in source


def test_mermaid_boxes_each_vrf(two_instances: Inventory) -> None:
    assert 'subgraph vrf0["vrf blue"]' in to_mermaid(
        build_graph(two_instances, layer=Layer.ROUTING)
    )


def test_json_exports_the_routing_object(peers: Inventory) -> None:
    document = graph_to_dict(build_graph(peers, layer=Layer.ROUTING))
    node = next(entry for entry in document["nodes"] if entry["id"] == "rtr-a")
    assert node["routing"] == {
        "asn": 65001,
        "routerId": "192.0.2.1",
        "ospfArea": "0.0.0.0",
        "ospfInterfaces": ["lo0", "eth0"],
    }
    session = next(edge for edge in document["edges"] if edge["kind"] == "bgp")
    assert session["adjacency"] == {
        "protocol": "bgp",
        "peerAddress": "10.0.0.2",
        "asns": [65001, 65002],
        "internal": False,
    }
    adjacency = next(edge for edge in document["edges"] if edge["kind"] == "ospf")
    assert adjacency["adjacency"] == {"protocol": "ospf", "area": "0.0.0.0"}
    assert "medium" not in session


def test_json_names_the_cluster_and_the_instances(two_instances: Inventory) -> None:
    document = graph_to_dict(build_graph(two_instances, layer=Layer.ROUTING))
    node = next(entry for entry in document["nodes"] if entry["id"] == "rtr-a")
    assert node["cluster"] == "blue"
    assert node["routing"]["vrfs"] == [{"name": "blue", "rd": "65001:1"}]


def test_a_subnet_node_names_its_instance(two_instances: Inventory) -> None:
    document = graph_to_dict(build_graph(two_instances, layer=Layer.L3))
    instances = [
        entry["subnet"].get("vrf") for entry in document["nodes"] if entry["type"] == "subnet"
    ]
    assert instances == [None, "blue"]


def test_a_tooltip_says_who_the_router_is_and_what_it_carries(two_instances: Inventory) -> None:
    graph = build_graph(two_instances, layer=Layer.ROUTING)
    ids = element_ids(graph)
    details = build_details(graph, RenderOptions(), ids=ids)
    text = detail_text(details[ids.nodes["rtr-a"]])
    assert "vrfs: blue (rd 65001:1)" in text


def test_an_adjacency_tooltip_names_the_protocol_and_the_session(peers: Inventory) -> None:
    graph = build_graph(peers, layer=Layer.ROUTING)
    ids = element_ids(graph)
    details = build_details(graph, RenderOptions(), ids=ids)
    session = next(index for index, edge in enumerate(graph.edges) if edge.kind is EdgeKind.BGP)
    identity = ids.edge(session)
    assert identity is not None
    text = detail_text(details[identity])
    assert "bgp AS 65001 to AS 65002 (eBGP)" in text
    assert "neighbour 10.0.0.2" in text


# --------------------------------------------------------------------------- #
# The CLI and the export
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_the_cli_renders_the_routing_layer(runner: CliRunner) -> None:
    result = runner.invoke(
        cli,
        ["-q", "-i", str(CAMPUS), "render", "--layer", "routing", "-f", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["layer"] == "routing"
    kinds = [edge["kind"] for edge in document["edges"]]
    assert kinds.count("bgp") == 3  # one iBGP mesh over three routers
    assert kinds.count("ospf") == 6  # three backbone links, three core-to-dist


def test_the_cli_says_why_the_routing_view_is_empty(runner: CliRunner, tmp_path: Path) -> None:
    inventory_of(
        tmp_path,
        *pair(
            device("rtr-a", port("eth0", "10.0.0.1/30")),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    result = runner.invoke(
        cli,
        ["-i", str(tmp_path), "render", "--layer", "routing", "-f", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "nothing to draw in the routing view" in result.output


def test_list_subnets_grows_a_vrf_column_only_when_needed(
    runner: CliRunner, tmp_path: Path, two_instances: Inventory
) -> None:
    without = runner.invoke(cli, ["-i", str(CAMPUS), "list", "subnets"], catch_exceptions=False)
    assert "VRF" in without.output  # the campus example has a mgmt instance

    plain = tmp_path / "plain"
    inventory_of(
        plain,
        *pair(
            device("rtr-a", port("eth0", "10.0.0.1/30")),
            device("rtr-b", port("eth0", "10.0.0.2/30")),
        ),
    )
    result = runner.invoke(cli, ["-i", str(plain), "list", "subnets"], catch_exceptions=False)
    assert "VRF" not in result.output


@requires_posix_shell
def test_the_route_script_is_valid_shell(tmp_path: Path) -> None:
    """``sh -n`` is the only opinion worth having about generated shell."""
    script = tmp_path / "routes.sh"
    result = CliRunner().invoke(
        cli,
        ["-i", str(CAMPUS), "export", "routes", "-o", str(script)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    text = script.read_text(encoding="utf-8")
    assert "ip -4 route replace blackhole 10.1.0.0/16 metric 250" in text
    assert "ip -4 route replace 0.0.0.0/0 via 10.1.0.1 dev Ethernet52/1 metric 10" in text
    assert "ip -4 route replace blackhole 0.0.0.0/0 vrf mgmt" in text
    checked = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_the_route_script_refuses_a_device_it_does_not_know(tmp_path: Path) -> None:
    script = tmp_path / "routes.sh"
    CliRunner().invoke(
        cli, ["-i", str(CAMPUS), "export", "routes", "-o", str(script)], catch_exceptions=False
    )
    ran = subprocess.run(["sh", str(script), "nobody"], capture_output=True, text=True)
    assert ran.returncode == 1
    assert "no routing declared for 'nobody'" in ran.stderr


def test_the_route_script_records_what_it_left_out(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    CliRunner().invoke(
        cli,
        [
            "-i",
            str(CAMPUS),
            "export",
            "routes",
            "-o",
            str(tmp_path / "r.sh"),
            "--manifest",
            str(manifest),
        ],
        catch_exceptions=False,
    )
    record = json.loads(manifest.read_text(encoding="utf-8"))
    reasons = {entry["reason"] for entry in record["skipped"]}
    assert "no-routes" in reasons
    # The cores declare 'routing' as well, which the script deliberately omits.
    assert "not-representable" in reasons


def test_the_export_registry_declares_the_new_format() -> None:
    exporter = EXPORTERS["routes"]
    assert exporter.layers == (Layer.L1,)
    assert exporter.suffix == ".sh"
    assert "static routes only" in exporter.lossy


# --------------------------------------------------------------------------- #
# The example inventory
# --------------------------------------------------------------------------- #


def test_the_campus_example_declares_the_routing_it_documents() -> None:
    inventory = load_tree(CAMPUS)
    assert inventory.errors == []
    assert validate(inventory) == []

    for site, index in (("north", 1), ("south", 2), ("west", 3)):
        core = inventory.devices[f"sites/{site}/core/rtr-{site}-core-01"]
        routing = core.spec.routing
        assert routing is not None and routing.bgp is not None and routing.ospf is not None
        assert routing.bgp.asn == 65001
        assert len(routing.bgp.neighbors) == 2, "an iBGP mesh over three routers"
        assert str(routing.bgp.router_id) == f"192.0.2.{index}"
        assert routing.ospf.area == "0.0.0.0"
        assert core.spec.routes[0].describe().startswith(f"10.{index}.0.0/16 blackhole")

        distribution = inventory.devices[f"sites/{site}/distribution/sw-{site}-dist-01"]
        assert [vrf.name for vrf in distribution.spec.vrfs] == ["mgmt"]
        svi = distribution.interface("Vlan99")
        assert svi is not None and svi.vrf == "mgmt"


def test_the_campus_management_prefixes_are_in_their_own_instance() -> None:
    subnets = subnets_of(load_tree(CAMPUS))
    mgmt = [subnet for subnet in subnets if subnet.vrf == "mgmt"]
    assert [subnet.prefix for subnet in mgmt] == [
        "10.1.99.0/24",
        "10.2.99.0/24",
        "10.3.99.0/24",
    ]
    # Every element addressed in one is bound to the instance, so the prefix is
    # not split between the global table and the VRF.
    assert all(len(subnet.elements) >= 3 for subnet in mgmt)


# --------------------------------------------------------------------------- #
# Policy-based routing: the model (§16.2, §16.4)
# --------------------------------------------------------------------------- #


def rule(priority: int, **fields: Any) -> dict[str, Any]:
    """One ``spec.routing_policy`` entry, ``lookup main`` unless told otherwise."""
    return {"priority": priority, **({"table": "main"} if "action" not in fields else {}), **fields}


def policy_device(*rules: dict[str, Any], **spec: Any) -> dict[str, Any]:
    """A router with two ports and the given policy database."""
    return device(
        "rtr",
        port("eth0", "10.0.0.1/24"),
        port("eth1", "10.9.0.1/24"),
        routing_policy=list(rules),
        **spec,
    )


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        (1, "0x1"),
        ("1", "0x1"),
        ("0x1", "0x1"),
        ("0XFF", "0xff"),
        ("0x1/0xff", "0x1/0xff"),
        ("1/255", "0x1/0xff"),
        (0, "0x0"),
    ],
)
def test_a_firewall_mark_normalises_to_hexadecimal(written: Any, stored: str) -> None:
    """A mark is a bit field, so two spellings of one mark have to compare equal."""
    assert normalise_fwmark(written) == stored


@pytest.mark.parametrize(
    "written",
    [
        "0x100/0xff",  # every compared bit is masked away: matches nothing
        "0x1/",
        "green",
        True,
        4_294_967_296,
        ["0x1"],
    ],
)
def test_a_value_that_is_no_firewall_mark_is_refused(written: Any) -> None:
    with pytest.raises(ValueError):
        normalise_fwmark(written)


def test_a_policy_rule_records_what_it_was_given() -> None:
    entry = PolicyRule.model_validate(
        {"priority": 100, "src": "10.20.0.0/16", "table": "uplink-b", "dscp": 46}
    )
    assert entry.priority == 100
    assert entry.action is PolicyAction.LOOKUP
    assert entry.selectors == ("from 10.20.0.0/16", "dscp 46")
    assert entry.describe() == "100: from 10.20.0.0/16 dscp 46 lookup uplink-b"
    assert not entry.is_catch_all


def test_a_rule_with_no_selector_matches_everything() -> None:
    entry = PolicyRule.model_validate({"priority": 32766, "table": "main"})
    assert entry.is_catch_all
    assert entry.describe() == "32766: all lookup main"


@pytest.mark.parametrize(
    ("fields", "families"),
    [
        ({"src": "10.0.0.0/8"}, ("ipv4",)),
        ({"dst": "fd00::/8"}, ("ipv6",)),
        ({"fwmark": "0x1"}, ("ipv4", "ipv6")),
        ({"family": "ipv6"}, ("ipv6",)),
        # A stated family wins over a derived one only when the two agree, which
        # NG-F017 is what enforces; here it is the *absence* of a prefix that
        # leaves the field to decide.
        ({"family": "ipv4", "fwmark": "0x1"}, ("ipv4",)),
    ],
)
def test_a_rule_is_installed_in_the_families_it_selects(
    fields: dict[str, Any], families: tuple[str, ...]
) -> None:
    entry = PolicyRule.model_validate({"priority": 1, "table": "main", **fields})
    assert tuple(family.value for family in entry.families) == families


def test_a_declared_reserved_table_is_ng_f015() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("rtr", port("eth0"), route_tables=[{"name": "main", "id": 5}]))
    assert issues(exc) == [("NG-F015", "name")]


def test_a_table_numbered_at_a_reserved_id_is_ng_f015() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("rtr", port("eth0"), route_tables=[{"name": "mine", "id": 254}]))
    assert issues(exc) == [("NG-F015", "id")]
    assert "reserved for 'main'" in str(exc.value)


@pytest.mark.parametrize(
    ("tables", "key"),
    [
        ([{"name": "a", "id": 1}, {"name": "a", "id": 2}], "name"),
        ([{"name": "a", "id": 1}, {"name": "b", "id": 1}], "id"),
    ],
)
def test_a_table_declared_twice_is_ng_f015(tables: list[dict[str, Any]], key: str) -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(device("rtr", port("eth0"), route_tables=tables))
    assert issues(exc) == [("NG-F015", key)]


def test_a_lookup_with_no_table_is_ng_f016() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(policy_device({"priority": 1}))
    assert issues(exc) == [("NG-F016", "table")]


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"action": "blackhole", "table": "main"}, "table"),
        ({"action": "goto"}, "goto"),
        ({"action": "blackhole", "goto": 5}, "goto"),
        ({"action": "goto", "goto": 1}, "goto"),  # a jump that does not go forwards
    ],
)
def test_an_action_that_disagrees_with_its_argument_is_ng_f016(
    fields: dict[str, Any], key: str
) -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(policy_device({"priority": 1, **fields}))
    assert issues(exc) == [("NG-F016", key)]


def test_a_goto_that_jumps_forwards_is_accepted() -> None:
    element = parse_document(policy_device({"priority": 1, "action": "goto", "goto": 200}))
    assert element.spec.routing_policy[0].describe() == "1: all goto 200"


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"src": "10.0.0.0/8", "dst": "fd00::/8"}, "dst"),
        ({"family": "ipv6", "src": "10.0.0.0/8"}, "src"),
        ({"invert": True}, "invert"),
    ],
)
def test_incoherent_selectors_are_ng_f017(fields: dict[str, Any], key: str) -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(policy_device(rule(1, **fields)))
    assert issues(exc) == [("NG-F017", key)]


def test_a_route_naming_both_a_vrf_and_a_table_is_ng_f018() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "rtr",
                port("eth0", "10.0.0.1/24"),
                vrfs=[{"name": "blue", "rd": "65000:1"}],
                routes=[{"prefix": "0.0.0.0/0", "via": "10.0.0.2", "vrf": "blue", "table": "main"}],
            )
        )
    assert issues(exc) == [("NG-F018", "table")]


def test_a_rule_looking_up_an_undeclared_table_is_ng_f019() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(policy_device(rule(1, table="nowhere")))
    assert issues(exc) == [("NG-F019", "table")]
    assert "'default', 'local', 'main'" in str(exc.value), "the reserved three are offered"


def test_a_route_placed_in_an_undeclared_table_is_ng_f019() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(
            device(
                "rtr",
                port("eth0", "10.0.0.1/24"),
                routes=[{"prefix": "0.0.0.0/0", "via": "10.0.0.2", "table": "nowhere"}],
            )
        )
    assert issues(exc) == [("NG-F019", "table")]


def test_a_vrf_is_a_table_a_rule_may_look_up() -> None:
    """§16.2: a VRF *is* a routing table, so it resolves without being declared twice."""
    element = parse_document(
        device(
            "rtr",
            port("eth0", "10.0.0.1/24", vrf="blue"),
            vrfs=[{"name": "blue", "rd": "65000:1"}],
            routing_policy=[rule(1, table="blue")],
        )
    )
    assert element.spec.has_table("blue")
    # ...and netviz has no *number* for it, which is what every emitter that
    # needs one has to refuse over.
    assert element.spec.table_id("blue") is None


def test_two_rules_of_one_family_at_one_priority_are_ng_f020() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(policy_device(rule(100, src="10.0.0.0/8"), rule(100, src="10.9.0.0/16")))
    assert issues(exc) == [("NG-F020", "priority")]


def test_one_priority_in_two_families_is_two_rules() -> None:
    """The two databases are separate lists, so they do not collide."""
    element = parse_document(policy_device(rule(100, src="10.0.0.0/8"), rule(100, src="fd00::/8")))
    assert len(element.spec.routing_policy) == 2


def test_a_rule_selecting_on_an_unknown_interface_is_ng_f021() -> None:
    with pytest.raises(SchemaError) as exc:
        parse_document(policy_device(rule(1, oif="eth9")))
    assert issues(exc) == [("NG-F021", "oif")]


def test_the_database_is_walked_in_priority_order() -> None:
    element = parse_document(
        policy_device(rule(300, src="10.0.0.0/8"), rule(100, src="10.9.0.0/16"), rule(200))
    )
    walk = element.spec.policy_in(AddressFamily.IPV4)
    assert [entry.priority for entry in walk] == [100, 200, 300]
    # The IPv6 database holds only the rule that selects on nothing v4-specific.
    assert [entry.priority for entry in element.spec.policy_in(AddressFamily.IPV6)] == [200]


def test_a_device_finds_the_routes_and_rules_of_a_table() -> None:
    element = parse_document(
        device(
            "rtr",
            port("eth0", "10.0.0.1/24"),
            route_tables=[{"name": "alt", "id": 100}],
            routes=[
                {"prefix": "0.0.0.0/0", "via": "10.0.0.2"},
                {"prefix": "10.9.0.0/16", "via": "10.0.0.3", "table": "alt"},
            ],
            routing_policy=[rule(1, table="alt", src="10.0.0.0/24")],
        )
    )
    spec = element.spec
    # A route naming no table is in 'main', which is the table it is actually in.
    assert [route.prefix.compressed for route in spec.routes_in("main")] == ["0.0.0.0/0"]
    assert [route.prefix.compressed for route in spec.routes_in("alt")] == ["10.9.0.0/16"]
    assert [entry.priority for entry in spec.policy_for("alt")] == [1]
    assert spec.route_table("alt") is not None and spec.route_table("main") is None
    assert spec.table_id("alt") == 100 and spec.table_id("main") == 254


# --------------------------------------------------------------------------- #
# Policy-based routing: the validator
# --------------------------------------------------------------------------- #


@pytest.fixture
def diverted(tmp_path: Path) -> dict[str, Any]:
    """One router that diverts a prefix into a table of its own. Clean."""
    return device(
        "rtr",
        port("eth0", "10.0.0.1/24"),
        port("eth1", "10.9.0.1/24"),
        route_tables=[{"name": "alt", "id": 100}],
        routes=[{"prefix": "0.0.0.0/0", "via": "10.9.0.2", "dev": "eth1", "table": "alt"}],
        routing_policy=[rule(100, src="10.0.0.0/24", table="alt"), rule(32766)],
    )


def test_a_diverted_prefix_with_both_halves_is_clean(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    inventory = inventory_of(tmp_path, diverted)
    assert [found for found in rules_of(inventory) if found.startswith("W14")] == []


def test_a_rule_looking_up_an_empty_table_is_w147(tmp_path: Path, diverted: dict[str, Any]) -> None:
    diverted["spec"]["routes"] = []
    inventory = inventory_of(tmp_path, diverted)
    assert "W147" in rules_of(inventory)
    finding = next(entry for entry in validate(inventory) if entry.rule == "W147")
    assert "alt (table 100)" in finding.message
    assert finding.field_path == ("spec", "route_tables", 0, "name")


def test_a_table_no_rule_looks_up_is_w148(tmp_path: Path, diverted: dict[str, Any]) -> None:
    diverted["spec"]["routing_policy"] = [rule(32766)]
    inventory = inventory_of(tmp_path, diverted)
    assert "W148" in rules_of(inventory)
    finding = next(entry for entry in validate(inventory) if entry.rule == "W148")
    assert "1 route placed in it is never consulted" in finding.message


def test_w147_and_w148_cannot_both_fire_for_one_table(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    """One needs a rule and no route, the other a route and no rule."""
    diverted["spec"]["routes"] = []
    diverted["spec"]["routing_policy"] = [rule(32766)]
    found = rules_of(inventory_of(tmp_path, diverted))
    assert "W148" in found and "W147" not in found


def test_a_rule_below_a_catch_all_is_w149(tmp_path: Path, diverted: dict[str, Any]) -> None:
    diverted["spec"]["routing_policy"] = [
        rule(32766),
        rule(32800, src="10.0.0.0/24", table="alt"),
    ]
    inventory = inventory_of(tmp_path, diverted)
    assert "W149" in rules_of(inventory)
    finding = next(entry for entry in validate(inventory) if entry.rule == "W149")
    assert "'32766: all lookup main'" in finding.message
    assert finding.field_path == ("spec", "routing_policy", 1, "priority")


def test_a_catch_all_in_one_family_shadows_nothing_in_the_other(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    diverted["spec"]["routing_policy"] = [
        rule(32766, family="ipv4"),
        rule(32800, dst="fd00::/8", table="alt"),
    ]
    assert "W149" not in rules_of(inventory_of(tmp_path, diverted))


def test_a_goto_catch_all_shadows_nothing(tmp_path: Path, diverted: dict[str, Any]) -> None:
    """A ``goto`` jumps forward, so what it jumps to is still reached."""
    diverted["spec"]["routing_policy"] = [
        {"priority": 100, "action": "goto", "goto": 200},
        rule(200, src="10.0.0.0/24", table="alt"),
    ]
    assert "W149" not in rules_of(inventory_of(tmp_path, diverted))


# --------------------------------------------------------------------------- #
# Policy-based routing: the view, and the export
# --------------------------------------------------------------------------- #


def test_the_routing_view_carries_the_tables_and_the_database(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    graph = build_graph(inventory_of(tmp_path, diverted), layer=Layer.ROUTING)
    view = graph.nodes["rtr"].routing
    assert view is not None
    assert view.tables == (("alt", 100),)
    # In priority order, which is the order the device walks it.
    assert view.policy == ("100: from 10.0.0.0/24 lookup alt", "32766: all lookup main")
    assert view.routes_by_policy
    assert "2 policy rules" in view.describe()


def test_a_device_with_policy_and_nothing_else_is_still_in_the_routing_view(
    tmp_path: Path,
) -> None:
    inventory = inventory_of(tmp_path, policy_device(rule(32766)))
    graph = build_graph(inventory, layer=Layer.ROUTING)
    assert "rtr" in graph.nodes


def test_json_exports_the_policy_database(tmp_path: Path, diverted: dict[str, Any]) -> None:
    graph = build_graph(inventory_of(tmp_path, diverted), layer=Layer.ROUTING)
    payload = graph_to_dict(graph, RenderOptions())
    routing = payload["nodes"][0]["routing"]
    assert routing["tables"] == [{"name": "alt", "id": 100}]
    assert routing["policy"][0] == "100: from 10.0.0.0/24 lookup alt"


def test_the_tooltip_names_the_tables_and_the_rules(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    graph = build_graph(inventory_of(tmp_path, diverted), layer=Layer.ROUTING)
    details = build_details(graph)
    text = detail_text(details[element_ids(graph).node("rtr") or ""])
    assert "tables: alt (100)" in text
    assert "100: from 10.0.0.0/24 lookup alt" in text


def test_the_route_script_writes_the_rules_beside_the_routes(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    inventory_of(tmp_path / "inv", diverted)
    script = tmp_path / "routes.sh"
    CliRunner().invoke(
        cli,
        ["-i", str(tmp_path / "inv"), "export", "routes", "-o", str(script)],
        catch_exceptions=False,
    )
    written = script.read_text(encoding="utf-8")
    # The table is named by *number*, with its name in a trailing comment: a name
    # resolves only through /etc/iproute2/rt_tables, which this script does not edit.
    assert "ip -4 route replace 0.0.0.0/0 via 10.9.0.2 dev eth1 table 100  # alt" in written
    # 'ip rule' has no 'replace', so idempotence is a del-then-add at the priority.
    assert "ip -4 rule del priority 100 2>/dev/null || :" in written
    assert "ip -4 rule add priority 100 from 10.0.0.0/24 lookup 100  # alt" in written
    assert "ip -4 rule add priority 32766 from all lookup 254  # main" in written
    # The routes come first: a rule applied before the table it selects is filled
    # diverts traffic into an empty table.
    assert written.index("route replace 0.0.0.0/0") < written.index("rule add priority 100")


@requires_posix_shell
def test_the_route_script_with_rules_is_valid_shell(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    inventory_of(tmp_path / "inv", diverted)
    script = tmp_path / "routes.sh"
    CliRunner().invoke(
        cli,
        ["-i", str(tmp_path / "inv"), "export", "routes", "-o", str(script)],
        catch_exceptions=False,
    )
    ran = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr


def test_a_device_with_only_policy_is_not_skipped_as_routeless(tmp_path: Path) -> None:
    inventory_of(tmp_path / "inv", policy_device(rule(32766)))
    manifest = tmp_path / "manifest.json"
    CliRunner().invoke(
        cli,
        [
            "-i",
            str(tmp_path / "inv"),
            "export",
            "routes",
            "-o",
            str(tmp_path / "r.sh"),
            "--manifest",
            str(manifest),
        ],
        catch_exceptions=False,
    )
    record = json.loads(manifest.read_text(encoding="utf-8"))
    assert record["counts"]["emitted"] == 1
    assert record["skipped"] == []


def _written(out: Path) -> str:
    """Everything an ``export -o`` wrote, however many files it chose to write.

    A multi-device export writes a tree; a single-device one writes the one file,
    banner-separated. A test about the *content* should not have to know which.
    """
    if out.is_file():
        return out.read_text(encoding="utf-8")
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(out.rglob("*")) if path.is_file()
    )


def test_networkd_writes_the_database_as_routing_policy_rules(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    inventory_of(tmp_path / "inv", diverted)
    out = tmp_path / "nd"
    CliRunner().invoke(
        cli,
        ["-i", str(tmp_path / "inv"), "export", "networkd", "-o", str(out)],
        catch_exceptions=False,
    )
    written = _written(out)
    assert "[RoutingPolicyRule]" in written
    assert "From=10.0.0.0/24" in written
    assert "Table=100" in written


def test_the_neutral_dialect_projects_the_tables_and_the_database(
    tmp_path: Path, diverted: dict[str, Any]
) -> None:
    inventory_of(tmp_path / "inv", diverted)
    out = tmp_path / "cfg"
    CliRunner().invoke(
        cli,
        ["-i", str(tmp_path / "inv"), "export", "interfaces", "-o", str(out)],
        catch_exceptions=False,
    )
    written = _written(out)
    assert "route-table alt\n    id 100" in written
    assert "policy 100\n    from 10.0.0.0/24\n    action lookup\n    table alt" in written


def test_the_campus_west_core_routes_by_policy() -> None:
    """§16.4 end to end, on the one example that declares it."""
    core = load_tree(CAMPUS).devices["sites/west/core/rtr-west-core-01"]
    spec = core.spec
    assert [table.describe() for table in spec.route_tables] == ["lab-egress (table 100)"]
    assert [entry.describe() for entry in spec.policy_in(AddressFamily.IPV4)] == [
        "90: from 10.3.20.0/24 to 10.3.99.0/24 prohibit",
        "100: from 10.3.20.0/24 lookup lab-egress",
        "110: fwmark 0x1 lookup lab-egress",
        "32766: all lookup main",
    ]
    # Both families are placed in the table, so the mark rule -- which is in both
    # databases -- finds a route whichever one it matches in.
    assert {route.family for route in spec.routes_in("lab-egress")} == {"ipv4", "ipv6"}
