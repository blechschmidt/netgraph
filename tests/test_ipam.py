"""``netgraph ipam``: sizing, free space, aggregation and the conflict report.

The properties asserted here are the ones an operator's next action depends on:

* a prefix is sized the way the protocols size it — RFC 3021 for the IPv4
  ``/31``, RFC 6164 for the IPv6 ``/127``, RFC 4291 §2.6.1 for everything wider
  in v6 — because a capacity that is two out is a capacity nobody trusts;
* free space is *subtractive and per subnet*, so what the command offers is
  space that can actually be handed out, and ``--next-free`` never returns a
  block that overlaps a declared one;
* aggregation only collapses a supernet both of whose halves are declared, so a
  summary can never claim space that is in fact free;
* the conflicts are the validator's, not a second implementation: the same rule
  ids, the same severities, and the same response to a suppression;
* both address families work alone and together, on the dual-stack fixture.

The three fixtures under ``tests/fixtures/ipam/`` are the shapes that are easy
to get wrong: a dual-stack site, a tree with both kinds of prefix overlap, and
point-to-point links at ``/31`` and ``/127``.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.config import ValidationConfig
from netgraph.ipam import (
    DEFAULT_SIZE,
    IPAM_RULES,
    Utilisation,
    aggregate,
    allocations_within,
    build_report,
    conflicts,
    format_capacity,
    format_utilisation,
    free_space,
    next_free,
    parse_prefix,
    parse_size,
    usable_addresses,
    utilisation_of,
)
from netgraph.loader import Inventory, load_tree
from netgraph.rules import RULE_IDS
from netgraph.subnets import subnets_of

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ipam"
INVALID = Path(__file__).resolve().parent / "fixtures" / "invalid"

DUAL_STACK = FIXTURES / "dual-stack.yaml"
OVERLAPPING = FIXTURES / "overlapping.yaml"
POINT_TO_POINT = FIXTURES / "point-to-point.yaml"


def load(path: Path) -> Inventory:
    inventory = load_tree(path)
    assert inventory.errors == [], "\n".join(str(error) for error in inventory.errors)
    return inventory


@pytest.fixture(scope="module")
def dual_stack() -> Inventory:
    return load(DUAL_STACK)


@pytest.fixture(scope="module")
def overlapping() -> Inventory:
    return load(OVERLAPPING)


@pytest.fixture(scope="module")
def point_to_point() -> Inventory:
    return load(POINT_TO_POINT)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(cli, list(args), catch_exceptions=False)


def net(text: str):  # type: ignore[no-untyped-def]
    return ipaddress.ip_network(text)


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("10.0.0.0/24", 254),
        ("10.0.0.0/30", 2),
        # RFC 3021: both addresses of a /31 belong to the two routers.
        ("10.0.0.0/31", 2),
        ("10.0.0.1/32", 1),
        ("10.0.0.0/8", 2**24 - 2),
        # RFC 4291 §2.6.1 reserves the all-zeros interface id, and there is no
        # broadcast address to lose.
        ("2001:db8::/64", 2**64 - 1),
        ("2001:db8::/126", 3),
        ("2001:db8::/127", 2),  # RFC 6164
        ("2001:db8::1/128", 1),
    ],
)
def test_a_prefix_is_sized_the_way_its_rfc_sizes_it(prefix: str, expected: int) -> None:
    assert usable_addresses(net(prefix)) == expected


def test_no_prefix_has_a_negative_or_zero_capacity() -> None:
    """A /31 sized as 2^n - 2 would report zero usable addresses, and a /32 -1."""
    for length in range(0, 33):
        assert usable_addresses(net(f"0.0.0.0/{length}")) >= 1
    for length in range(0, 129):
        assert usable_addresses(net(f"::/{length}")) >= 1


@pytest.mark.parametrize(
    ("capacity", "host_bits", "expected"),
    [
        (254, 8, "254"),
        (0, 8, "0"),
        (2**19 - 2, 19, "524286"),
        (2**20 - 2, 20, "2^20"),
        (2**64 - 1, 64, "2^64"),
    ],
)
def test_a_capacity_too_wide_for_a_column_is_a_power_of_two(
    capacity: int, host_bits: int, expected: str
) -> None:
    assert format_capacity(capacity, host_bits=host_bits) == expected


def test_a_capacity_renders_without_being_told_its_prefix() -> None:
    """The library is usable without a network to hand; the exponent is derived."""
    assert format_capacity(254) == "254"
    assert format_capacity(2**64) == "2^65"


@pytest.mark.parametrize(
    ("assigned", "capacity", "expected"),
    [
        (0, 254, "0.0%"),
        (3, 254, "1.2%"),
        (254, 254, "100.0%"),
        # In use, but too sparse to round to anything: "empty" and "two hosts in
        # a /64" must not print the same.
        (2, 2**64 - 1, "<0.1%"),
        (0, 2**64 - 1, "0.0%"),
    ],
)
def test_utilisation_distinguishes_empty_from_nearly_empty(
    assigned: int, capacity: int, expected: str
) -> None:
    assert format_utilisation(assigned, capacity) == expected


# --------------------------------------------------------------------------- #
# Utilisation
# --------------------------------------------------------------------------- #


def test_utilisation_reports_both_families_of_a_dual_stack_tree(dual_stack: Inventory) -> None:
    rows = {row.prefix: row for row in utilisation_of(subnets_of(dual_stack))}
    assert set(rows) == {
        "10.10.10.0/24",
        "10.10.20.0/24",
        "2001:db8:a:10::/64",
        "2001:db8:a:20::/64",
    }

    v4 = rows["10.10.10.0/24"]
    assert (v4.version, v4.family) == (4, "ipv4")
    assert (v4.capacity, v4.assigned, v4.devices) == (254, 3, 3)
    assert v4.free == 251
    assert v4.vlans == (10, 20)  # the router's trunk carries both

    v6 = rows["2001:db8:a:10::/64"]
    assert (v6.version, v6.family) == (6, "ipv6")
    assert (v6.capacity, v6.assigned, v6.devices) == (2**64 - 1, 3, 3)


def test_the_two_families_are_sized_independently(dual_stack: Inventory) -> None:
    """The same three hosts fill 1.2 % of a /24 and nothing at all of a /64."""
    rows = {row.prefix: row for row in utilisation_of(subnets_of(dual_stack))}
    v4, v6 = rows["10.10.10.0/24"], rows["2001:db8:a:10::/64"]
    assert v4.assigned == v6.assigned
    assert format_utilisation(v4.assigned, v4.capacity) == "1.2%"
    assert format_utilisation(v6.assigned, v6.capacity) == "<0.1%"


def test_a_point_to_point_link_is_full_rather_than_impossible(
    point_to_point: Inventory,
) -> None:
    rows = {row.prefix: row for row in utilisation_of(subnets_of(point_to_point))}
    for prefix in ("10.0.0.0/31", "10.0.1.0/30", "2001:db8::/127"):
        row = rows[prefix]
        assert (row.capacity, row.assigned, row.free) == (2, 2, 0), prefix
        assert format_utilisation(row.assigned, row.capacity) == "100.0%"


def test_an_address_claimed_twice_occupies_one_slot() -> None:
    """``assigned`` counts addresses, not placements: one address, one slot."""
    inventory = load(INVALID / "w106-subnet-address-clash.yaml")
    (row,) = [
        entry for entry in utilisation_of(subnets_of(inventory)) if entry.prefix == "10.0.0.0/24"
    ]
    assert row.assigned == 1
    assert row.devices == 2


def test_rows_are_ordered_by_family_then_address_then_length(dual_stack: Inventory) -> None:
    rows = utilisation_of(subnets_of(dual_stack))
    assert [row.version for row in rows] == sorted(row.version for row in rows)
    assert list(rows) == sorted(rows, key=lambda row: row.sort_key)


def test_a_utilisation_row_records_itself_for_json(dual_stack: Inventory) -> None:
    (row,) = [
        entry for entry in utilisation_of(subnets_of(dual_stack)) if entry.prefix == "10.10.20.0/24"
    ]
    assert row.record() == {
        # Always present, even for the global instance: a CSV needs a stable
        # header, and an absent column would shift every field after it.
        "vrf": "",
        "prefix": "10.10.20.0/24",
        "family": "ipv4",
        "vlans": [],
        "capacity": 254,
        "assigned": 2,
        "free": 252,
        "utilisation": round(2 / 254, 6),
        "devices": 2,
        "aggregated": [],
    }


def test_a_row_with_no_capacity_reports_no_utilisation() -> None:
    """Defensive: nothing derived from a real prefix can reach it, but the
    record must stay serialisable if anything ever constructs one."""
    row = Utilisation(prefix="10.0.0.0/24", network=net("10.0.0.0/24"))
    assert row.record()["utilisation"] is None
    assert row.free == 0


# --------------------------------------------------------------------------- #
# Free space
# --------------------------------------------------------------------------- #


def test_free_space_is_the_holes_between_the_allocations(dual_stack: Inventory) -> None:
    subnets = subnets_of(dual_stack)
    blocks = free_space(net("10.10.0.0/16"), subnets)
    assert "10.10.10.0/24" not in {str(block) for block in blocks}
    assert "10.10.20.0/24" not in {str(block) for block in blocks}
    # Nothing free overlaps anything allocated, and nothing is lost.
    covered = sum(block.num_addresses for block in blocks)
    assert covered == 2**16 - 2 * 256


def test_adjacent_free_blocks_are_reported_as_the_block_that_can_be_handed_out(
    dual_stack: Inventory,
) -> None:
    """Free space is summarised, not enumerated: the eight /24s of 10.10.0.0/21
    are one free /21, because that is the block an operator can hand out."""
    blocks = [str(block) for block in free_space(net("10.10.0.0/16"), subnets_of(dual_stack))]
    assert blocks[:3] == ["10.10.0.0/21", "10.10.8.0/23", "10.10.11.0/24"]
    assert free_space(net("10.10.12.0/22"), subnets_of(dual_stack)) == (net("10.10.12.0/22"),)


def test_a_fully_allocated_prefix_has_no_free_space(dual_stack: Inventory) -> None:
    assert free_space(net("10.10.10.0/24"), subnets_of(dual_stack)) == ()


def test_a_prefix_of_the_other_family_is_entirely_free(dual_stack: Inventory) -> None:
    """An IPv4 allocation consumes nothing of an IPv6 plan."""
    assert free_space(net("2001:db8:ff::/48"), subnets_of(dual_stack)) == (net("2001:db8:ff::/48"),)


def test_allocation_happens_a_subnet_at_a_time(dual_stack: Inventory) -> None:
    allocated = allocations_within(net("10.10.0.0/16"), subnets_of(dual_stack))
    assert allocated == (net("10.10.10.0/24"), net("10.10.20.0/24"))


def test_an_address_whose_own_prefix_is_wider_consumes_only_a_host_route() -> None:
    """A summary written as 10.30.1.11/16 must not swallow a /24 plan.

    Consuming its own prefix would consume the whole plan the operator is
    asking about, which is never the answer they want.
    """
    inventory = load(OVERLAPPING)
    allocated = allocations_within(net("10.30.1.0/24"), subnets_of(inventory))
    assert allocated == (net("10.30.1.11/32"), net("10.30.1.12/32"))


def test_free_space_of_the_ipv6_side_is_computed_without_enumerating_it(
    dual_stack: Inventory,
) -> None:
    blocks = free_space(net("2001:db8:a::/48"), subnets_of(dual_stack))
    assert net("2001:db8:a:10::/64") not in blocks
    assert sum(block.num_addresses for block in blocks) == 2**80 - 2 * 2**64


# --------------------------------------------------------------------------- #
# The next free block
# --------------------------------------------------------------------------- #


def test_next_free_returns_the_lowest_block_that_fits(dual_stack: Inventory) -> None:
    subnets = subnets_of(dual_stack)
    assert next_free(net("10.10.0.0/16"), 24, subnets) == net("10.10.0.0/24")
    assert next_free(net("10.10.10.0/23"), 25, subnets) == net("10.10.11.0/25")
    assert next_free(net("10.10.8.0/22"), 23, subnets) == net("10.10.8.0/23")


def test_next_free_never_overlaps_an_allocation(dual_stack: Inventory) -> None:
    subnets = subnets_of(dual_stack)
    block = next_free(net("10.10.8.0/22"), 26, subnets)
    assert block is not None
    assert not any(block.overlaps(subnet.network) for subnet in subnets if subnet.version == 4)


def test_next_free_searches_a_v6_plan_without_enumerating_it(dual_stack: Inventory) -> None:
    assert next_free(net("2001:db8:a::/48"), 64, subnets_of(dual_stack)) == net("2001:db8:a::/64")


@pytest.mark.parametrize(
    ("prefix", "size"),
    [
        ("10.10.10.0/24", 8),  # wider than the prefix it must fit inside
        ("10.10.10.0/24", 24),  # the prefix is entirely allocated
        ("10.10.10.0/24", 26),  # nothing of any size is left in it
        ("10.0.0.0/24", 33),  # not a prefix length at all
    ],
)
def test_next_free_answers_none_rather_than_guessing(
    dual_stack: Inventory, prefix: str, size: int
) -> None:
    assert next_free(net(prefix), size, subnets_of(dual_stack)) is None


def test_the_default_block_size_follows_the_family() -> None:
    """RFC 4291 §2.5.4 makes the /64 the unit of an IPv6 plan; SLAAC needs it."""
    assert DEFAULT_SIZE == {4: 24, 6: 64}


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def rows_of(*prefixes: str) -> tuple[Utilisation, ...]:
    return tuple(
        Utilisation(
            prefix=prefix,
            network=net(prefix),
            assigned=1,
            devices=1,
            capacity=usable_addresses(net(prefix)),
        )
        for prefix in prefixes
    )


def test_both_halves_of_a_supernet_collapse_into_it() -> None:
    (row,) = aggregate(rows_of("10.0.0.0/25", "10.0.0.128/25"))
    assert row.prefix == "10.0.0.0/24"
    assert row.members == ("10.0.0.0/25", "10.0.0.128/25")
    assert row.is_aggregate


def test_a_supernet_with_one_half_declared_is_left_alone() -> None:
    """Collapsing it would let the summary claim the empty half as in use."""
    assert [row.prefix for row in aggregate(rows_of("10.0.0.0/25"))] == ["10.0.0.0/25"]
    assert [row.prefix for row in aggregate(rows_of("10.0.0.0/25", "10.1.0.0/25"))] == [
        "10.0.0.0/25",
        "10.1.0.0/25",
    ]


def test_aggregation_repeats_to_a_fixed_point() -> None:
    """Four /26s are one /24, not two /25s."""
    (row,) = aggregate(rows_of("10.0.0.0/26", "10.0.0.64/26", "10.0.0.128/26", "10.0.0.192/26"))
    assert row.prefix == "10.0.0.0/24"
    assert len(row.members) == 4


def test_an_aggregate_capacity_is_the_sum_of_its_children() -> None:
    """Two /25s lose four addresses between them; a /24 over them loses two.

    The sum is what the plan can hold, so the sum is what is reported.
    """
    (row,) = aggregate(rows_of("10.0.0.0/25", "10.0.0.128/25"))
    assert row.capacity == 126 + 126
    assert row.capacity != usable_addresses(net("10.0.0.0/24"))
    assert row.assigned == 2
    assert row.devices == 2


def test_aggregation_keeps_the_families_apart() -> None:
    prefixes = ("10.0.0.0/25", "10.0.0.128/25", "2001:db8::/65", "2001:db8:0:0:8000::/65")
    assert [row.prefix for row in aggregate(rows_of(*prefixes))] == [
        "10.0.0.0/24",
        "2001:db8::/64",
    ]


def test_a_default_route_has_no_supernet_to_collapse_into() -> None:
    assert [row.prefix for row in aggregate(rows_of("0.0.0.0/0"))] == ["0.0.0.0/0"]


def test_aggregation_unions_the_vlans_it_folds() -> None:
    left, right = rows_of("10.0.0.0/25", "10.0.0.128/25")
    (row,) = aggregate(
        (
            Utilisation(**{**_fields(left), "vlans": (10,)}),
            Utilisation(**{**_fields(right), "vlans": (20, 10)}),
        )
    )
    assert row.vlans == (10, 20)


def _fields(row: Utilisation) -> dict[str, object]:
    return {
        "prefix": row.prefix,
        "network": row.network,
        "vlans": row.vlans,
        "assigned": row.assigned,
        "devices": row.devices,
        "members": row.members,
        "capacity": row.capacity,
    }


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #


def test_every_ipam_rule_is_a_real_rule() -> None:
    """The report filters the validator by id; a typo would silently drop one."""
    assert set(IPAM_RULES) <= set(RULE_IDS)
    assert len(set(IPAM_RULES)) == len(IPAM_RULES)


def test_the_overlap_fixture_reports_both_kinds_of_overlap(overlapping: Inventory) -> None:
    found = conflicts(overlapping)
    assert [finding.rule for finding in found] == ["W130", "W131"]
    assert "10.20.0.0/24" in found[0].message
    assert "VLAN 10" in found[0].message and "VLAN 30" in found[0].message
    assert "10.30.7.0/24" in found[1].message and "10.30.0.0/16" in found[1].message


def test_a_gateway_off_its_own_subnet_is_an_error() -> None:
    inventory = load(INVALID / "e020-gateway-off-link.yaml")
    (finding,) = conflicts(inventory)
    assert finding.rule == "E020"
    assert finding.severity.is_fatal
    assert "10.9.9.1" in finding.message


def test_a_link_local_gateway_is_on_link_by_definition(dual_stack: Inventory) -> None:
    """``fe80::1`` is the ordinary IPv6 first hop and must not be reported."""
    assert conflicts(dual_stack) == ()
    interface = dual_stack.devices["pc-user"].interface("eth0")
    assert interface is not None and interface.ipv6 is not None
    assert str(interface.ipv6.gateway) == "fe80::1"


def test_a_gateway_is_read_per_family(dual_stack: Inventory) -> None:
    interface = dual_stack.devices["pc-user"].interface("eth0")
    assert interface is not None
    assert [version for version, _ in interface.gateways()] == [4, 6]


def test_conflicts_honour_a_suppression_exactly_as_validate_does(
    overlapping: Inventory,
) -> None:
    settings = ValidationConfig().with_overrides(ignore=["W130"])
    assert [finding.rule for finding in conflicts(overlapping, settings)] == ["W131"]


def test_conflicts_honour_an_alias_in_a_suppression(overlapping: Inventory) -> None:
    settings = ValidationConfig().with_overrides(ignore=["NG-A011"])
    assert [finding.rule for finding in conflicts(overlapping, settings)] == ["W130"]


def test_the_shipped_examples_have_a_healthy_address_plan() -> None:
    for name in ("quickstart", "home-lab", "campus", "overlay"):
        assert conflicts(load(EXAMPLES / name)) == (), name


def test_the_campus_example_demonstrates_a_gateway() -> None:
    """The field the docs describe is exercised by a shipped inventory.

    A `gateway` nothing declares would be a field documented and never seen,
    and `E020` would be a rule only its own fixture ever reaches.
    """
    campus = load(EXAMPLES / "campus")
    interface = campus.devices["sites/north/hosts/pc-north-01"].interface("eno1")
    assert interface is not None and interface.ipv4 is not None and interface.ipv6 is not None
    gateway = interface.ipv4.gateway
    assert gateway is not None
    # On-link in the host's own prefix, which is what NG-A013 checks, and the
    # address of the Vlan10 SVI that actually routes for the segment.
    assert gateway in next(iter(interface.ipv4.addresses)).network
    assert str(gateway) == "10.1.10.1"
    assert str(interface.ipv6.gateway) == "fe80::1"


def test_the_report_carries_both_halves(overlapping: Inventory) -> None:
    report = build_report(overlapping)
    assert not report.aggregated
    assert len(report.rows) == 3
    assert len(report.findings) == 2
    assert report.assigned == 8
    assert report.of_family(6) == ()
    assert len(report.of_family(4)) == 3


def test_the_report_aggregates_on_request() -> None:
    report = build_report(load(POINT_TO_POINT), aggregated=True)
    assert report.aggregated
    # 10.0.0.0/31 and 10.0.1.0/30 are not siblings, so nothing folds.
    assert [row.prefix for row in report.rows] == [
        "10.0.0.0/31",
        "10.0.1.0/30",
        "2001:db8::/127",
    ]


# --------------------------------------------------------------------------- #
# Parsing the command line's arguments
# --------------------------------------------------------------------------- #


def test_a_prefix_may_be_written_with_host_bits_set() -> None:
    """``--free 10.1.0.1/22`` means the /22 the operator was looking at."""
    assert parse_prefix("10.1.0.1/22") == net("10.1.0.0/22")
    assert parse_prefix(" 2001:db8::5/64 ") == net("2001:db8::/64")


@pytest.mark.parametrize("text", ["", "   ", "nonsense", "10.0.0.0/33", "10.0.0.256/24"])
def test_a_prefix_that_is_not_one_says_so(text: str) -> None:
    with pytest.raises(ValueError, match=r"prefix|not an IP prefix"):
        parse_prefix(text)


@pytest.mark.parametrize(("text", "version", "expected"), [("24", 4, 24), ("/64", 6, 64)])
def test_a_size_may_be_written_with_or_without_its_slash(
    text: str, version: int, expected: int
) -> None:
    assert parse_size(text, version) == expected


@pytest.mark.parametrize(("text", "version"), [("/33", 4), ("129", 6), ("wide", 4), ("²⁴", 4)])
def test_a_size_that_is_not_a_prefix_length_says_so(text: str, version: int) -> None:
    with pytest.raises(ValueError, match="prefix length"):
        parse_size(text, version)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_default_report_is_a_table_and_a_conflict_list(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(DUAL_STACK), "ipam")
    assert result.exit_code == 0
    assert "PREFIX" in result.output and "UTIL" in result.output
    assert "10.10.10.0/24" in result.output
    assert "2^64" in result.output
    assert "conflicts" in result.output
    assert "no problems found" in result.output


def test_the_table_is_byte_identical_between_runs(runner: CliRunner) -> None:
    """Sorting is deterministic, so a diff of two runs is a diff of the tree."""
    first = invoke(runner, "-i", str(EXAMPLES / "campus"), "ipam")
    second = invoke(runner, "-i", str(EXAMPLES / "campus"), "ipam")
    assert first.output == second.output


def test_a_family_filter_leaves_the_other_family_out(runner: CliRunner) -> None:
    v4 = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--family", "ipv4")
    assert "10.10.10.0/24" in v4.output
    assert "2001:db8" not in v4.output

    v6 = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--family", "ipv6")
    assert "2001:db8:a:10::/64" in v6.output
    assert "10.10.10.0/24" not in v6.output


def test_the_json_report_carries_both_halves(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(OVERLAPPING), "ipam", "-F", "json")
    payload = json.loads(result.stdout)
    assert [entry["prefix"] for entry in payload["subnets"]] == [
        "10.20.0.0/24",
        "10.30.0.0/16",
        "10.30.7.0/24",
    ]
    assert [entry["rule"] for entry in payload["conflicts"]] == ["W130", "W131"]
    assert payload["conflicts"][0]["alias"] == "NG-A010"
    # The exact integer, not the abbreviated 2^n the table prints.
    assert payload["subnets"][1]["capacity"] == 65534


def test_the_json_report_spells_a_v6_capacity_out(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(DUAL_STACK), "ipam", "-F", "json", "--family", "ipv6")
    payload = json.loads(result.stdout)
    assert payload["subnets"][0]["capacity"] == 2**64 - 1


def test_the_csv_report_is_one_table(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(OVERLAPPING), "ipam", "-F", "csv")
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [row["prefix"] for row in rows] == [
        "10.20.0.0/24",
        "10.30.0.0/16",
        "10.30.7.0/24",
    ]
    assert rows[0]["vlans"] == "10 30"
    assert rows[0]["capacity"] == "254"


def test_the_csv_report_says_what_it_left_out(runner: CliRunner) -> None:
    """Silence would read as "no conflicts", which is the opposite of the truth."""
    result = invoke(runner, "-i", str(OVERLAPPING), "ipam", "-F", "csv")
    assert "2 conflicts not shown" in result.stderr
    quiet = invoke(runner, "-q", "-i", str(OVERLAPPING), "ipam", "-F", "csv")
    assert "not shown" not in quiet.stderr
    assert quiet.stdout == result.stdout


def test_the_conflicts_can_be_asked_for_on_their_own(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(OVERLAPPING), "ipam", "--conflicts")
    assert "PREFIX" not in result.output
    assert "W130" in result.output and "W131" in result.output

    as_csv = invoke(runner, "-i", str(OVERLAPPING), "ipam", "--conflicts", "-F", "csv")
    rows = list(csv.DictReader(io.StringIO(as_csv.stdout)))
    assert [row["rule"] for row in rows] == ["W130", "W131"]
    assert rows[0]["alias"] == "NG-A010"
    assert rows[0]["severity"] == "warning"

    as_json = invoke(runner, "-i", str(OVERLAPPING), "ipam", "--conflicts", "-F", "json")
    payload = json.loads(as_json.stdout)
    assert "subnets" not in payload
    assert len(payload["conflicts"]) == 2


def test_an_error_severity_conflict_fails_the_run(runner: CliRunner) -> None:
    result = invoke(
        runner, "-i", str(INVALID / "e020-gateway-off-link.yaml"), "ipam", "--conflicts"
    )
    assert result.exit_code == 1
    assert "E020" in result.output


def test_a_warning_severity_conflict_does_not(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(OVERLAPPING), "ipam")
    assert result.exit_code == 0


def test_free_space_is_listed_as_cidr_blocks(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--free", "10.10.0.0/16")
    assert result.exit_code == 0
    assert "BLOCK" in result.output
    assert "10.10.10.0/24" not in result.output  # allocated
    assert "10.10.0.0/21" in result.output


def test_free_space_says_when_there_is_none(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--free", "10.10.10.0/24")
    assert result.exit_code == 0
    assert "fully allocated" in result.output


def test_free_space_serialises(runner: CliRunner) -> None:
    as_json = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--free", "10.10.0.0/22", "-F", "json")
    payload = json.loads(as_json.stdout)
    assert payload["prefix"] == "10.10.0.0/22"
    assert payload["allocated"] == []
    assert payload["free"] == [{"block": "10.10.0.0/22", "family": "ipv4", "capacity": 1022}]

    as_csv = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--free", "10.10.0.0/22", "-F", "csv")
    assert as_csv.stdout.splitlines() == ["BLOCK,IP,HOSTS", "10.10.0.0/22,4,1022"]


def test_next_free_prints_a_bare_prefix_so_it_pipes(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--next-free", "10.10.0.0/16")
    assert result.exit_code == 0
    assert result.stdout.strip() == "10.10.0.0/24"


def test_next_free_takes_a_size_with_or_without_a_slash(runner: CliRunner) -> None:
    for size in ("26", "/26"):
        result = invoke(
            runner, "-i", str(DUAL_STACK), "ipam", "--next-free", "10.10.10.0/22", "--size", size
        )
        assert result.stdout.strip() == "10.10.8.0/26"


def test_next_free_defaults_to_a_64_for_ipv6(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--next-free", "2001:db8:a::/48")
    assert result.stdout.strip() == "2001:db8:a::/64"


def test_next_free_fails_when_there_is_no_room(runner: CliRunner) -> None:
    result = invoke(
        runner, "-i", str(DUAL_STACK), "ipam", "--next-free", "10.10.10.0/24", "--size", "8"
    )
    assert result.exit_code == 1
    assert "no free /8" in result.stderr


def test_next_free_serialises(runner: CliRunner) -> None:
    result = invoke(
        runner, "-i", str(DUAL_STACK), "ipam", "--next-free", "10.10.0.0/16", "-F", "json"
    )
    assert json.loads(result.stdout) == {
        "prefix": "10.10.0.0/16",
        "size": 24,
        "next": "10.10.0.0/24",
    }

    as_csv = invoke(
        runner, "-i", str(DUAL_STACK), "ipam", "--next-free", "10.10.0.0/16", "-F", "csv"
    )
    assert as_csv.stdout.splitlines() == ["PREFIX,SIZE,NEXT", "10.10.0.0/16,24,10.10.0.0/24"]


def test_a_json_next_free_with_no_room_reports_null(runner: CliRunner) -> None:
    result = invoke(
        runner,
        "-i",
        str(DUAL_STACK),
        "ipam",
        "--next-free",
        "10.10.10.0/24",
        "--size",
        "8",
        "-F",
        "json",
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["next"] is None
    assert "no free /8" in result.stderr


def test_aggregation_adds_a_parts_column(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(EXAMPLES / "campus"), "ipam", "--aggregate")
    assert "PARTS" in result.output
    assert "198.51.100.0/29" in result.output  # two /30s, folded


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--free", "10.0.0.0/8", "--next-free", "10.0.0.0/8"), "ask different questions"),
        (("--size", "24"), "--size only means something with --next-free"),
        (("--free", "10.0.0.0/8", "--aggregate"), "which --free replaces"),
        (("--next-free", "10.0.0.0/8", "--conflicts"), "which --next-free replaces"),
        (("--conflicts", "--aggregate"), "prints no utilisation table"),
        (("--family", "ipv4", "--conflicts"), "--family applies to the utilisation table"),
        (("--free", "nonsense"), "is not an IP prefix"),
        (("--next-free", "10.0.0.0/8", "--size", "wide"), "is not a prefix length"),
    ],
)
def test_an_option_is_never_silently_ignored(
    runner: CliRunner, args: tuple[str, ...], message: str
) -> None:
    result = invoke(runner, "-i", str(DUAL_STACK), "ipam", *args)
    assert result.exit_code == 2
    assert message in result.output


def test_colour_appears_only_when_it_is_asked_for(runner: CliRunner) -> None:
    plain = invoke(runner, "--no-color", "-i", str(EXAMPLES / "campus"), "ipam")
    assert "\x1b[" not in plain.output

    coloured = invoke(runner, "--color", "-i", str(EXAMPLES / "campus"), "ipam")
    assert "\x1b[" in coloured.output
    # A 100 %-utilised /30 is worth acting on, so it is red.
    assert "\x1b[31m100.0%\x1b[0m" in coloured.output


def test_quiet_silences_the_commentary_but_never_the_data(runner: CliRunner) -> None:
    loud = invoke(runner, "-i", str(DUAL_STACK), "ipam", "--free", "10.10.0.0/16")
    quiet = invoke(runner, "-q", "-i", str(DUAL_STACK), "ipam", "--free", "10.10.0.0/16")
    assert "free space in" in loud.stderr
    assert "free space in" not in quiet.stderr
    assert "10.10.0.0/21" in quiet.stdout


def test_a_tree_with_no_addresses_says_so(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "sw.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n  name: sw\n"
        "spec:\n  interfaces:\n"
        "    - name: Ethernet1\n      type: ethernet\n      enabled: false\n",
        encoding="utf-8",
    )
    result = invoke(runner, "-i", str(tmp_path), "ipam")
    assert result.exit_code == 0
    assert "no addresses declared" in result.output
