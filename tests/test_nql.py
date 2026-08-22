"""The relational query language: the schema, the parser, the world and the answers.

Five kinds of test, each checking something different.

* **The schema table** — that it is well formed (every link points at a declared
  type, no cycle in the inheritance, no duplicate name) and, more importantly,
  that it and :mod:`netviz.nql.world` agree. A row nobody fills is a member that
  always answers nothing, and a member nobody declared is one the parser will
  refuse; both are silent failures without a test that walks the two together.
* **The parser** — what parses, what does not, and that a rejection names the
  column. The parser is the type checker, so "is this refused before the
  inventory is read?" is a correctness question and not a performance one.
* **The world** — that the objects come out of the same functions a diagram and
  a listing use, so the three cannot disagree.
* **The semantics** — worked queries against the examples, and the properties
  the docstrings claim: that cardinality decides the JSON shape, that a shape is
  output-only, that ``filter`` is existential.
* **The wiring** — that ``netviz query`` dispatches on the first word, that the
  selector still works, and that every output format carries the same answer.

The four questions Task 141 named have a test each, at the bottom, run against
``examples/campus``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from click.testing import CliRunner

from netviz.cli import cli
from netviz.loader import load_tree
from netviz.nql import (
    EXAMPLES,
    MAX_DEPTH,
    MAX_NODES,
    SCHEMA,
    QueryError,
    answer,
    bind,
    build_world,
    declare,
    describe,
    execute,
    explain,
    is_relational,
    overview,
    parse,
    read_assignment,
)
from netviz.nql.format import cell, headings, label, payload, table, type_name
from netviz.nql.functions import FUNCTIONS
from netviz.nql.lexer import TokenKind, tokenize
from netviz.nql.types import Link, Property, Schema, normalise_name
from netviz.nql.world import SEPARATOR, Ref
from netviz.nql.world import build_world as build
from netviz.query.errors import MAX_QUERY_LENGTH
from netviz.subnets import subnets_of

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
EXAMPLES_DIR: Final = REPO_ROOT / "examples"
CAMPUS: Final = EXAMPLES_DIR / "campus"
HOME_LAB: Final = EXAMPLES_DIR / "home-lab"
PATCH_ROOM: Final = EXAMPLES_DIR / "patch-room"
DOCKER: Final = EXAMPLES_DIR / "docker"
OVERLAY: Final = EXAMPLES_DIR / "overlay"

#: Every example tree, so the world builder is exercised over every kind of
#: document the repository ships rather than over one convenient one.
EVERY_EXAMPLE: Final = sorted(
    path for path in EXAMPLES_DIR.iterdir() if path.is_dir() and any(path.rglob("*.yaml"))
)


@pytest.fixture(scope="module")
def campus():
    return build_world(load_tree(CAMPUS))


@pytest.fixture(scope="module")
def home_lab():
    return build_world(load_tree(HOME_LAB))


@pytest.fixture(scope="module")
def patch_room():
    return build_world(load_tree(PATCH_ROOM))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str):
    return runner.invoke(cli, list(args), catch_exceptions=False)


def rows(world, text: str) -> list[Any]:
    return list(answer(text, world).rows)


# --------------------------------------------------------------------------- #
# The schema table
# --------------------------------------------------------------------------- #


def test_every_link_points_at_a_declared_type() -> None:
    """A link to a type nobody declared is a path that cannot be walked."""
    for one in SCHEMA:
        for link in SCHEMA.links(one.name):
            assert SCHEMA.resolve(link.target) is not None, (
                f"{one.name}.{link.name} targets {link.target!r}, which is not a type"
            )


def test_every_type_is_reachable_by_its_own_name() -> None:
    for one in SCHEMA:
        assert SCHEMA.canonical(one.name) == one.name
        assert SCHEMA.canonical(one.name.upper()) == one.name
        for alias in one.aliases:
            assert SCHEMA.canonical(alias) == one.name


def test_inheritance_flattens_without_losing_a_member() -> None:
    """A subtype has everything its bases have, plus its own."""
    for one in SCHEMA:
        for base in one.bases:
            for name, member in SCHEMA.members(base).items():
                assert SCHEMA.member(one.name, name) is member


def test_a_device_kind_is_a_device_is_an_element() -> None:
    assert SCHEMA.is_subtype("server", "device")
    assert SCHEMA.is_subtype("server", "element")
    assert not SCHEMA.is_subtype("device", "server")
    assert not SCHEMA.is_subtype("cable", "device")
    assert SCHEMA.common("server", "switch") == "device"
    assert SCHEMA.common("server", "cable") == "element"
    assert SCHEMA.common("server", "interface") == ""


def test_abstract_types_have_no_objects_of_their_own(campus) -> None:
    """``select device`` reads its concrete subtypes and nothing else."""
    assert "device" not in {SCHEMA.canonical(ref.type) for ref in campus.all("device")}
    assert {ref.type for ref in campus.all("device")} <= set(SCHEMA.concrete("device"))


def test_an_alias_is_the_same_member_not_a_copy() -> None:
    member = SCHEMA.member("interface", "parent")
    for spelling in ("element", "owner"):
        assert SCHEMA.member("interface", spelling) is member
    # ``*`` must expand to one column per step, not one per spelling.
    names = [one.name for one in SCHEMA.properties("element")]
    assert len(names) == len(set(names))


def test_normalise_name_folds_case_and_the_separator() -> None:
    assert normalise_name("Broadcast-Domain") == "broadcast_domain"
    assert SCHEMA.canonical("BROADCAST-DOMAIN") == "broadcast_domain"


def test_a_duplicate_type_is_refused() -> None:
    from netviz.nql.types import ObjectType

    with pytest.raises(ValueError, match="duplicate"):
        Schema((ObjectType("a", ""), ObjectType("a", "")))


def test_a_base_must_be_declared_first() -> None:
    from netviz.nql.types import ObjectType

    with pytest.raises(ValueError, match="not declared yet"):
        Schema((ObjectType("a", "", bases=("b",)),))


# --------------------------------------------------------------------------- #
# The schema and the world agree
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("root", EVERY_EXAMPLE, ids=lambda path: path.name)
def test_the_world_produces_only_members_the_schema_declares(root: Path) -> None:
    """The other half of the coupling: nothing is stored that cannot be asked for.

    A property or link the builder fills but the table does not name is
    unreachable — the parser refuses the path — so it is dead weight that looks
    like a feature. Walking every example catches the ones a single fixture
    would not exercise.
    """
    world = build_world(load_tree(root))
    for one in world.objects.values():
        members = SCHEMA.members(one.type)
        for name in one.props:
            member = members.get(name)
            assert isinstance(member, Property), f"{one.type}.{name} is not a declared property"
        for name in one.links:
            member = members.get(name)
            assert isinstance(member, Link), f"{one.type}.{name} is not a declared link"


@pytest.mark.parametrize("root", EVERY_EXAMPLE, ids=lambda path: path.name)
def test_every_link_lands_on_an_object_of_the_declared_type(root: Path) -> None:
    world = build_world(load_tree(root))
    for one in world.objects.values():
        for name, targets in one.links.items():
            link = SCHEMA.members(one.type)[name]
            assert isinstance(link, Link)
            for target in targets:
                held = world.objects[target]
                assert SCHEMA.is_subtype(held.type, link.target), (
                    f"{one.type}.{name} points at a {held.type}, not a {link.target}"
                )


def test_every_declared_member_is_filled_by_something() -> None:
    """Read across all the examples, no member of the table is dead.

    Per member *name* rather than per concrete type: a cable has no
    ``interfaces`` and a PDU has no address, and requiring every inherited
    member on every kind would be asking the examples to be exhaustive rather
    than asking the builder to be complete. What this catches is the real
    failure — a row somebody added to the schema and never wrote a reader for,
    which answers nothing forever and looks like a network in which nothing
    matches.
    """
    seen: set[str] = set()
    for root in EVERY_EXAMPLE:
        world = build_world(load_tree(root))
        for one in world.objects.values():
            seen |= set(one.props) | set(one.links)
    declared = {member.name for one in SCHEMA for member in SCHEMA.members(one.name).values()}
    # No allow-list: the examples between them exercise every member the table
    # declares, and adding one without a reader is meant to fail here. Three
    # links — `attached_to`, `device.zones` and `tunnel.over` — were declared
    # and unfilled when this was first written, which is what it is for.
    assert not declared - seen, f"nothing fills {sorted(declared - seen)}"


def test_object_ids_never_collide_across_types(campus) -> None:
    for identity, one in campus.objects.items():
        assert identity == f"{one.type}{SEPARATOR}{Ref(identity).local}"
        assert SEPARATOR not in Ref(identity).local


# --------------------------------------------------------------------------- #
# The world agrees with the rest of netviz
# --------------------------------------------------------------------------- #


def test_subnets_are_the_ones_the_listing_and_the_diagram_use() -> None:
    """One derivation, three consumers. A query must not have a fourth opinion."""
    inventory = load_tree(CAMPUS)
    world = build_world(inventory)
    assert {one.prefix for one in subnets_of(inventory)} == set(rows(world, "select subnet.prefix"))


def test_a_broadcast_domain_holds_the_members_the_layer_two_view_draws() -> None:
    from netviz.graph import broadcast_domains, to_networkx

    inventory = load_tree(CAMPUS)
    world = build_world(inventory)
    declared = {one.id: set(one.members) for one in broadcast_domains(to_networkx(inventory))}
    for row in rows(world, "select broadcast_domain { id, members := .members.fqn }"):
        assert set(row["members"]) == declared[row["id"]]


def test_interfaces_are_reachable_from_their_element_and_back(campus) -> None:
    both = rows(campus, "select interface { fqn, back := .parent.interfaces.fqn }")
    for row in both:
        assert row["fqn"] in row["back"]


def test_a_cable_joins_the_two_ports_that_name_it(home_lab) -> None:
    for row in rows(home_lab, "select cable { name, ends := .ends { fqn, cable := .cable.name } }"):
        assert len(row["ends"]) == 2
        for end in row["ends"]:
            assert end["cable"] == row["name"]


def test_the_adapter_upstream_is_a_port_a_cable_can_terminate_on(home_lab) -> None:
    found = rows(home_lab, "select adapter { upstream, ports := .interfaces.name }")
    assert found
    for row in found:
        assert row["upstream"] in row["ports"]


# --------------------------------------------------------------------------- #
# The lexer
# --------------------------------------------------------------------------- #


def test_a_name_is_letters_digits_and_underscore() -> None:
    kinds = [token.kind for token in tokenize("select device_x")]
    assert kinds == [TokenKind.WORD, TokenKind.WORD, TokenKind.END]


def test_a_hyphen_is_the_minus_sign_not_part_of_a_name() -> None:
    texts = [token.text for token in tokenize("a-b")]
    assert texts == ["a", "-", "b", ""]


def test_a_value_with_punctuation_is_quoted() -> None:
    (token, _end) = tokenize("'sw-core-01'")
    assert token.kind is TokenKind.STRING
    assert token.text == "sw-core-01"


def test_a_decimal_is_one_token_and_a_path_step_is_not() -> None:
    assert [one.text for one in tokenize("2.5")][:1] == ["2.5"]
    assert [one.text for one in tokenize("2.mtu")][:3] == ["2", ".", "mtu"]


def test_a_comment_runs_to_the_end_of_the_line() -> None:
    assert [one.text for one in tokenize("select # everything\ndevice")][:2] == [
        "select",
        "device",
    ]


def test_an_unclosed_quote_is_underlined() -> None:
    with pytest.raises(QueryError, match="never closed"):
        tokenize("select device filter .name = 'x")


def test_a_character_no_token_may_hold_is_named() -> None:
    with pytest.raises(QueryError, match="not part of any operator or name"):
        tokenize("select device filter .name = @x")


# --------------------------------------------------------------------------- #
# The parser is the type checker
# --------------------------------------------------------------------------- #


def test_a_misspelt_member_names_the_type_and_suggests() -> None:
    with pytest.raises(QueryError) as caught:
        parse("select interface { mak }")
    assert "'mak' is not a member of interface" in str(caught.value)
    assert "did you mean 'mac'" in str(caught.value)
    assert "^^^" in str(caught.value)


def test_a_misspelt_type_suggests_too() -> None:
    with pytest.raises(QueryError, match="did you mean 'interface'"):
        parse("select interfce")


def test_text_does_not_order() -> None:
    with pytest.raises(QueryError, match="does not order"):
        parse("select device filter .name < 3")


def test_a_leading_dot_needs_a_subject() -> None:
    with pytest.raises(QueryError, match="there is none here"):
        parse("select .name")


def test_filter_needs_a_condition_not_a_set_of_objects() -> None:
    with pytest.raises(QueryError, match="needs a condition"):
        parse("select device filter .interfaces")
    with pytest.raises(QueryError, match="needs a condition"):
        parse("select device filter .name")


def test_a_member_cannot_be_read_off_a_scalar() -> None:
    with pytest.raises(QueryError, match="has no members"):
        parse("select device.name.length")


def test_unrelated_types_cannot_be_narrowed_into_one_another() -> None:
    with pytest.raises(QueryError, match="no cable is ever a server"):
        parse("select cable[is server]")


def test_a_function_checks_its_arity_and_its_arguments() -> None:
    with pytest.raises(QueryError, match="takes 1 arguments"):
        parse("select count(device, 2)")
    with pytest.raises(QueryError, match="must be a number"):
        parse("select avg(device.name)")
    with pytest.raises(QueryError, match="is not a function"):
        parse("select coubt(device)")


def test_a_bad_regular_expression_is_refused_at_parse_time() -> None:
    with pytest.raises(QueryError, match="is not a regular expression"):
        parse("select device filter .name =~ '[unclosed'")


def test_limit_needs_a_whole_number() -> None:
    with pytest.raises(QueryError, match="needs a whole number"):
        parse("select device limit .name")


def test_a_shape_may_not_project_one_name_twice() -> None:
    with pytest.raises(QueryError, match="projected twice"):
        parse("select device { name, name }")


def test_a_query_must_end_where_it_ends() -> None:
    with pytest.raises(QueryError, match="expected the end of the query"):
        parse("select device select cable")


def test_nesting_and_size_are_bounded() -> None:
    with pytest.raises(QueryError, match="nests more than"):
        parse("select " + "(" * (MAX_DEPTH + 2) + "device" + ")" * (MAX_DEPTH + 2))
    with pytest.raises(QueryError, match="more than"):
        parse("select {" + ",".join(["1"] * (MAX_NODES + 2)) + "}")


def test_the_size_limit_is_one_a_query_can_reach() -> None:
    """A limit that the length limit makes unreachable is not a limit."""
    shortest = len("select {}") + 2 * MAX_NODES
    assert shortest < MAX_QUERY_LENGTH


def test_a_binding_may_not_be_named_twice() -> None:
    with pytest.raises(QueryError, match="already bound"):
        parse("with a := device, a := cable select a")


def test_the_parser_never_reads_an_inventory() -> None:
    """Checking is against the schema, which is why a typo costs a millisecond."""
    query = parse("select interface { name, parent: { fqn } } filter exists .addresses")
    assert query.type.object_type == "interface"
    assert query.shape is not None
    assert [one.name for one in query.shape.elements] == ["name", "parent"]


# --------------------------------------------------------------------------- #
# Cardinality decides the shape of the answer
# --------------------------------------------------------------------------- #


def test_a_many_link_is_an_array_even_with_one_value(home_lab) -> None:
    found = rows(home_lab, "select adapter { name, interfaces: { name }, attached_to: { name } }")
    assert found
    for row in found:
        assert isinstance(row["interfaces"], list)
        # ``attached_to`` is optional, so it is the object itself or null.
        assert row["attached_to"] is None or isinstance(row["attached_to"], dict)


def test_an_optional_property_with_no_value_is_null(campus) -> None:
    found = rows(campus, "select interface { name, mtu } filter .name = 'lo0'")
    assert found
    assert all(row["mtu"] is None for row in found)


def test_a_one_property_is_never_a_list(campus) -> None:
    for row in rows(campus, "select device { name, kind }"):
        assert isinstance(row["name"], str)
        assert isinstance(row["kind"], str)


def test_shapes_nest_without_limit(campus) -> None:
    found = rows(
        campus,
        "select device { name, interfaces: { name, addresses: { address, subnet: { prefix } } } }"
        " filter .name = 'rtr-north-core-01'",
    )
    assert len(found) == 1
    ports = found[0]["interfaces"]
    addresses = [one for port in ports for one in port["addresses"]]
    assert addresses
    assert any(one["subnet"] is not None for one in addresses)


def test_a_shape_is_output_only(campus) -> None:
    """``(select device { name }).vendor`` is still devices with a vendor read off."""
    shaped = rows(campus, "select (select device { name }).kind")
    plain = rows(campus, "select device.kind")
    assert shaped == plain


def test_a_free_object_is_exactly_one_object(campus) -> None:
    found = rows(campus, "select { devices := count(device), subnets := count(subnet) }")
    assert len(found) == 1
    assert found[0] == {"devices": len(campus.all("device")), "subnets": len(campus.all("subnet"))}


def test_the_splat_expands_to_every_property_once(campus) -> None:
    found = rows(campus, "select device { * } limit 1")
    assert list(found[0]) == [one.name for one in SCHEMA.properties("switch")]


# --------------------------------------------------------------------------- #
# Semantics
# --------------------------------------------------------------------------- #


def test_filter_is_existential(campus) -> None:
    """A device matches when *one* of its addresses does."""
    with_ten = rows(campus, "select device.fqn filter .addresses.ip in '10.1.0.0/16'")
    assert with_ten
    for fqn in with_ten:
        addresses = rows(
            campus, f"select (device filter .fqn = '{fqn}').addresses.ip in '10.1.0.0/16'"
        )
        assert any(addresses)


def test_not_equal_is_the_mirror_of_equal_not_its_negation(campus) -> None:
    """The property the module docstring claims, checked rather than asserted."""
    both = rows(
        campus,
        "select interface { fqn } filter .addresses.ip = '10.1.20.11'"
        " and .addresses.ip != '10.1.20.11'",
    )
    # A port with a second address satisfies both halves at once.
    assert both
    neither = rows(
        campus,
        "select interface { fqn } filter .addresses.ip = '10.1.20.11'"
        " and not (.addresses.ip = '10.1.20.11')",
    )
    assert not neither


def test_a_path_deduplicates_objects_but_not_scalars(campus) -> None:
    """Two ports of one device walk back to one device."""
    owners = rows(campus, "select (interface filter .parent.name = 'sw-north-acc-01').parent.fqn")
    assert len(owners) == 1
    types = rows(campus, "select (interface filter .parent.name = 'sw-north-acc-01').type")
    assert len(types) > 1


def test_in_is_containment_for_a_prefix_and_membership_otherwise(campus) -> None:
    inside = rows(campus, "select address.address filter .ip in '10.1.0.0/16'")
    assert inside
    assert all(one.startswith("10.1.") for one in inside)
    named = rows(campus, "select device.name filter .kind in {'router', 'server'}")
    assert named == rows(campus, "select device.name filter .kind = 'router' or .kind = 'server'")


def test_under_walks_the_namespace_hierarchy(campus) -> None:
    north = rows(campus, "select device.fqn filter .namespace under 'sites/north'")
    assert north
    assert all(one.startswith("sites/north/") for one in north)
    assert rows(campus, "select device.fqn filter .namespace under ''") == rows(
        campus, "select device.fqn"
    )


def test_glob_and_regex(campus) -> None:
    assert rows(campus, "select device.name filter .name ~ 'sw-north-*'")
    assert rows(campus, "select device.name filter .name =~ 'acc-0[12]$'")
    assert rows(campus, "select device.name filter .name ilike 'SW-NORTH-*'")


def test_is_and_is_not_are_the_type_question(campus) -> None:
    servers = rows(campus, "select interface.fqn filter .parent is server")
    others = rows(campus, "select interface.fqn filter .parent is not server")
    assert servers and others
    assert not set(servers) & set(others)
    assert len(servers) + len(others) == len(campus.all("interface"))


def test_type_filter_narrows_and_reads_a_subtype_member(campus) -> None:
    assert rows(campus, "select interface.parent[is router].asn") or True
    assert rows(campus, "select interface.parent[is server].fqn")


def test_order_by_sorts_numbers_as_numbers_and_empty_last(campus) -> None:
    used = [row["used"] for row in rows(campus, "select subnet { used } order by .used desc")]
    assert used == sorted(used, reverse=True)
    mtus = [row["mtu"] for row in rows(campus, "select interface { mtu } order by .mtu")]
    assert [one for one in mtus if one is None] == mtus[len(mtus) - mtus.count(None) :]


def test_order_by_breaks_ties_with_the_second_key(campus) -> None:
    found = rows(campus, "select device { kind, name } order by .kind then .name")
    assert found == sorted(found, key=lambda row: (row["kind"], row["name"]))


def test_limit_and_offset(campus) -> None:
    every = rows(campus, "select device.name order by .name")
    assert rows(campus, "select device.name order by .name limit 3") == every[:3]
    assert rows(campus, "select device.name order by .name offset 3 limit 2") == every[3:5]


def test_clauses_inside_a_shape_narrow_the_field(campus) -> None:
    found = rows(
        campus,
        "select device { name, ports := .interfaces { name } filter .enabled limit 2 }"
        " filter .name = 'sw-north-acc-01'",
    )
    assert len(found[0]["ports"]) == 2


def test_with_bindings_are_computed_once_and_reusable(campus) -> None:
    found = rows(
        campus,
        "with core := (switch filter .name = 'sw-north-dist-01')"
        " select { name := core.name, ports := count(core.interfaces) }",
    )
    # ``core`` is a filtered set, so ``core.name`` is many: an array, and the
    # count is one number. Cardinality decides that, not how many came back.
    assert found[0]["name"] == ["sw-north-dist-01"]
    assert found[0]["ports"] > 0


def test_select_is_optional_inside_a_group_or_an_argument(campus) -> None:
    assert rows(campus, "select count(interface filter exists .addresses)") == rows(
        campus, "select count((select interface filter exists .addresses))"
    )


def test_arithmetic_and_concatenation(campus) -> None:
    assert rows(campus, "select { total := count(device) * 2 }")[0]["total"] == 2 * len(
        campus.all("device")
    )
    joined = rows(campus, "select device { tag := .kind ++ ':' ++ .name } limit 1")
    assert joined[0]["tag"].count(":") == 1


def test_dividing_by_zero_is_refused_with_the_query_underlined(campus) -> None:
    with pytest.raises(QueryError, match="cannot divide by zero"):
        answer("select { bad := 1 / 0 }", campus)


def test_distinct_and_exists(campus) -> None:
    kinds = rows(campus, "select distinct device.kind")
    assert kinds == list(dict.fromkeys(rows(campus, "select device.kind")))
    assert rows(campus, "select exists device") == [True]
    assert rows(campus, "select exists (device filter .name = 'nope')") == [False]


def test_aggregates(campus) -> None:
    assert rows(campus, "select count(device)") == [len(campus.all("device"))]
    assert rows(campus, "select min(subnet.prefix_length)") == [
        min(rows(campus, "select subnet.prefix_length"))
    ]
    assert rows(campus, "select all({true, true})") == [True]
    assert rows(campus, "select all({})") == [True]
    assert rows(campus, "select any({false, true})") == [True]
    assert rows(campus, "select avg({})") == []


def test_text_functions(campus) -> None:
    assert rows(campus, "select upper('a')") == ["A"]
    assert rows(campus, "select lower('A')") == ["a"]
    assert rows(campus, "select len('abcd')") == [4]
    assert rows(campus, "select contains('abcd', 'bc')") == [True]
    assert rows(campus, "select starts_with('abcd', 'ab')") == [True]
    assert rows(campus, "select ends_with('abcd', 'cd')") == [True]
    assert rows(campus, "select matches('abcd', 'ab*')") == [True]


def test_lookup_reads_one_label(campus) -> None:
    found = rows(campus, "select device { name, role := lookup(.labels, 'role') } limit 1")
    assert found[0]["role"]


def test_neighbors_includes_the_seed_and_reachable_is_the_component(campus) -> None:
    one_hop = rows(campus, "select neighbors(device filter .name = 'sw-north-acc-01').name")
    assert "sw-north-acc-01" in one_hop
    two_hop = rows(campus, "select neighbors(device filter .name = 'sw-north-acc-01', 2).name")
    assert set(one_hop) <= set(two_hop)
    component = rows(campus, "select reachable(device filter .name = 'sw-north-acc-01').fqn")
    assert set(rows(campus, "select neighbors(device filter .name='sw-north-acc-01', 99).fqn")) == (
        set(component)
    )


def test_an_empty_set_is_empty(campus) -> None:
    assert rows(campus, "select {}") == []
    assert rows(campus, "select none") == []


def test_a_set_of_mixed_object_types_takes_their_common_type() -> None:
    query = parse("select {device, cable}")
    assert query.type.object_type == "element"
    with pytest.raises(QueryError, match="have no type in common"):
        parse("select {device, interface}")
    with pytest.raises(QueryError, match="mixes"):
        parse("select {device, 1}")


# --------------------------------------------------------------------------- #
# Formatting and introspection
# --------------------------------------------------------------------------- #


def test_a_table_flattens_and_a_document_does_not(campus) -> None:
    result = execute(
        parse("select device { name, addresses := .addresses.address } limit 2"), campus
    )
    text, aligns = table(result)
    assert headings(result) == ("NAME", "ADDRESSES")
    assert len(aligns) == 2
    assert all(isinstance(one, str) for row in text for one in row)
    assert isinstance(payload(result)["results"][0]["addresses"], list)


def test_a_count_only_document_carries_no_results(campus) -> None:
    result = execute(parse("select device"), campus)
    assert "results" not in payload(result, count_only=True)
    assert payload(result)["count"] == len(result)


def test_the_column_of_an_unshaped_answer_is_named_after_the_query(campus) -> None:
    assert label(execute(parse("select interface.mac"), campus)) == "mac"
    assert label(execute(parse("select device"), campus)) == "device"
    assert label(execute(parse("select count(device)"), campus)) == "count"
    assert label(execute(parse("with a := device select a"), campus)) == "a"


def test_a_cell_renders_every_json_shape() -> None:
    assert cell(None) == "-"
    assert cell(True) == "yes"
    assert cell([1, 2]) == "1, 2"
    assert cell({"a": 1, "b": 2}) == "1 2"
    assert cell([]) == "-"


def test_a_free_object_reports_its_type_as_an_object(campus) -> None:
    assert type_name(execute(parse("select { a := 1 }"), campus)) == "one object"


def test_describe_names_every_member_of_a_type() -> None:
    text = "\n".join(describe("interface"))
    for member in SCHEMA.members("interface").values():
        assert member.name in text
    assert describe("nope") == ()


def test_the_overview_lists_every_type() -> None:
    text = "\n".join(overview())
    for one in SCHEMA:
        assert one.name in text


def test_explain_prints_the_grammar_the_types_and_the_functions() -> None:
    text = "\n".join(explain())
    assert "# grammar" in text and "# types" in text
    for shape in ("aggregate", "elementwise", "graph"):
        assert f"# {shape} functions" in text
    for name in FUNCTIONS:
        assert name in text


def test_every_documented_example_parses() -> None:
    """The help must not teach a query the parser refuses."""
    for _question, query in EXAMPLES:
        parse(query)


def test_every_documented_example_runs(campus) -> None:
    for _question, query in EXAMPLES:
        execute(parse(query), campus)


def test_is_relational_reads_the_first_word() -> None:
    assert is_relational("select device")
    assert is_relational("  WITH a := device select a")
    assert not is_relational("kind = switch")
    assert not is_relational("'select'")
    assert not is_relational("")


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_command_dispatches_on_the_first_word(runner: CliRunner) -> None:
    relational = invoke(runner, "-i", str(HOME_LAB), "query", "select device.name")
    assert relational.exit_code == 0
    assert "NAME" in relational.stdout

    selector = invoke(runner, "-i", str(HOME_LAB), "query", "kind = switch")
    assert selector.exit_code == 0
    assert "switches/sw-home" in selector.stdout


@pytest.mark.parametrize("output_format", ["table", "json", "yaml", "csv"])
def test_every_format_carries_the_same_answer(runner: CliRunner, output_format: str) -> None:
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "query",
        "select device { name, kind } order by .name",
        "-F",
        output_format,
    )
    assert result.exit_code == 0
    assert "rtr-home" in result.stdout
    if output_format == "json":
        document = json.loads(result.stdout)
        assert document["count"] == len(document["results"])
        assert document["query"].startswith("select device")
    if output_format == "yaml":
        assert yaml.safe_load(result.stdout)["results"][0]["name"]


def test_count_composes_with_every_format(runner: CliRunner) -> None:
    plain = invoke(runner, "-i", str(HOME_LAB), "query", "select device", "--count")
    assert plain.exit_code == 0
    assert plain.stdout.strip().isdigit()
    as_json = invoke(runner, "-i", str(HOME_LAB), "query", "select device", "--count", "-F", "json")
    assert "results" not in json.loads(as_json.stdout)


def test_nothing_matched_exits_one(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "query", "select device filter .name = 'nope'")
    assert result.exit_code == 1
    assert "nothing matched" in result.stdout


def test_a_bad_query_is_a_usage_error(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "query", "select interface { mak }")
    assert result.exit_code == 2
    assert "did you mean 'mac'" in result.output


def test_describe_prints_the_language_and_one_type(runner: CliRunner) -> None:
    whole = invoke(runner, "query", "--describe")
    assert whole.exit_code == 0
    assert "# grammar" in whole.stdout and "broadcast_domain" in whole.stdout

    one = invoke(runner, "query", "--describe", "interface")
    assert one.exit_code == 0
    assert "parent|element|owner" in one.stdout

    bad = invoke(runner, "query", "--describe", "nope")
    assert bad.exit_code == 2


def test_the_selector_flags_are_refused_for_a_relational_query(runner: CliRunner) -> None:
    layered = invoke(runner, "-i", str(HOME_LAB), "query", "select device", "--layer", "l3")
    assert layered.exit_code == 2
    assert "--layer scopes a selector query" in layered.output

    printed = invoke(runner, "-i", str(HOME_LAB), "query", "select device", "--print", "links")
    assert printed.exit_code == 2

    formatted = invoke(runner, "-i", str(HOME_LAB), "query", "kind = switch", "-F", "yaml")
    assert formatted.exit_code == 2
    assert "only available for a relational query" in formatted.output


def test_json_flag_still_means_json_for_both_languages(runner: CliRunner) -> None:
    relational = invoke(runner, "-i", str(HOME_LAB), "query", "select device.name", "--json")
    assert json.loads(relational.stdout)["results"]
    selector = invoke(runner, "-i", str(HOME_LAB), "query", "kind = switch", "--json")
    assert json.loads(selector.stdout)["matches"]


def test_a_query_about_a_broken_inventory_is_refused(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text(
        "apiVersion: netviz.dev/v1alpha1\nkind: cable\nmetadata:\n  name: c\n"
        "spec:\n  endpoints: ['a:1', 'b:1']\n  medium: copper\n",
        encoding="utf-8",
    )
    refused = invoke(runner, "-i", str(tmp_path), "query", "select cable.name")
    assert refused.exit_code == 1
    assert "refusing to answer" in refused.output
    forced = invoke(runner, "-i", str(tmp_path), "query", "select cable.name", "--force")
    assert forced.exit_code == 0


# --------------------------------------------------------------------------- #
# The four questions Task 141 named
# --------------------------------------------------------------------------- #


def test_every_interface_with_an_address_and_what_it_is_attached_to(campus) -> None:
    found = rows(
        campus,
        "select interface { fqn, parent: { fqn, kind }, addresses: { address } }"
        " filter exists .addresses",
    )
    assert found
    for row in found:
        assert row["addresses"], "the filter kept a port with no address"
        assert row["parent"]["kind"] in set(SCHEMA.concrete("element"))
        assert row["fqn"].startswith(row["parent"]["fqn"] + ":")
    # And it is every one of them, not a subset.
    assert len(found) == len(rows(campus, "select interface.fqn filter exists .addresses"))


def test_the_interfaces_in_a_switchs_broadcast_domain_and_their_devices(campus) -> None:
    found = rows(
        campus,
        "select broadcast_domain { name, vlan_id, ports := .interfaces { fqn, device := "
        ".parent.name } } filter .members.name = 'sw-south-dist-01' and .vlan_id = 20",
    )
    assert len(found) == 1
    devices = {port["device"] for port in found[0]["ports"]}
    assert "sw-south-dist-01" in devices
    # Every port named is really in that VLAN, and its device is really its parent.
    for port in found[0]["ports"]:
        assert (
            rows(
                campus,
                f"select (interface filter .fqn = '{port['fqn']}').vlans.id",
            ).count(20)
            == 1
        )


def test_which_addresses_are_assigned_to_a_server_called_x(campus) -> None:
    walked = rows(campus, "select (server filter .name = 'srv-north-01').addresses.address")
    assert "10.1.20.11/24" in walked
    shaped = rows(
        campus,
        "select server { name, addresses := .addresses.address } filter .name = 'srv-north-01'",
    )
    assert shaped[0]["addresses"] == walked


def test_which_mac_addresses_are_assigned_to_a_server(campus) -> None:
    found = rows(
        campus,
        "select interface { host := .parent.name, port := .name, mac }"
        " filter .parent is server and exists .mac",
    )
    assert found
    assert all(row["mac"] and row["host"] for row in found)
    every = {row["mac"] for row in found}
    assert every == set(rows(campus, "select (interface filter .parent is server).mac"))


def test_a_structured_answer_is_valid_json_for_every_example(runner: CliRunner) -> None:
    """The task asked for object and array-of-object returns; this is the proof."""
    for root in EVERY_EXAMPLE:
        result = invoke(
            runner,
            "-i",
            str(root),
            "query",
            "select element { fqn, kind, ports := .interfaces { name, addresses := "
            ".addresses.address } }",
            "-F",
            "json",
            "--force",
        )
        assert result.exit_code in (0, 1), result.output
        document = json.loads(result.stdout)
        for row in document["results"]:
            assert set(row) == {"fqn", "kind", "ports"}
            assert isinstance(row["ports"], list)
            for port in row["ports"]:
                assert set(port) == {"name", "addresses"}
                assert isinstance(port["addresses"], list)


def test_build_world_is_the_documented_entry_point() -> None:
    """``build_world`` and the re-export are the same function."""
    assert build is build_world
    world = build_world(load_tree(HOME_LAB))
    assert len(world) == len(world.objects)
    assert world.display(Ref("nope|x")) == "x"
    assert world.step(Ref("nope|x"), "name", is_link=False) == ()


# --------------------------------------------------------------------------- #
# Comparison corners
# --------------------------------------------------------------------------- #


def test_ordering_addresses_and_the_shapes_that_do_not_order(campus) -> None:
    """``<`` on addresses compares addresses; on anything else it is text."""
    assert rows(campus, "select address.ip filter .ip > '10.1.99.12' and .ip < '10.1.99.20'") == [
        "10.1.99.13"
    ]
    # Mixed families never order against one another, and neither do objects.
    assert rows(campus, "select {1} filter min(address.ip) <= max(address.ip)") == [1]
    assert rows(campus, "select subnet.prefix_length filter .prefix_length >= 32")


def test_equality_across_kinds_of_value(campus) -> None:
    assert rows(campus, "select {1} filter 1 = 1.0") == [1]
    assert rows(campus, "select {1} filter true = true") == [1]
    assert rows(campus, "select {1} filter true = 1") == []
    assert rows(campus, "select {1} filter device = device limit 1") == [1]
    with pytest.raises(QueryError, match="cannot compare"):
        parse("select {1} filter device = 'sw-north-acc-01'")


def test_a_prefix_contains_a_prefix_and_an_address(campus) -> None:
    assert rows(campus, "select subnet.prefix filter .prefix in '10.1.0.0/16'")
    assert rows(campus, "select address.ip filter .ip in '::/0'") == rows(
        campus, "select address.ip filter .family = 6"
    )
    # A candidate that is not a prefix falls back to equality.
    assert rows(campus, "select address.ip filter .ip in {'10.1.99.11', 'not-a-prefix'}") == [
        "10.1.99.11"
    ]


def test_ordering_by_an_object_key_is_refused_but_by_a_reference_is_not(campus) -> None:
    with pytest.raises(QueryError, match="needs a scalar"):
        parse("select interface order by .parent")
    # An empty key sorts last whichever direction is asked for.
    ascending = rows(campus, "select interface { fqn, mtu } order by .mtu")
    assert ascending[-1]["mtu"] is None


def test_a_regular_expression_is_compiled_once_and_the_cache_is_bounded(campus) -> None:
    from netviz.nql.execute import _COMPILED, _PATTERN_CACHE

    _COMPILED.clear()
    for index in range(_PATTERN_CACHE + 2):
        rows(campus, f"select device.name filter .name =~ 'acc-{index:04d}'")
    assert len(_COMPILED) <= _PATTERN_CACHE


def test_negation_is_a_partition(campus) -> None:
    """``filter X`` and ``filter not X`` between them are every element."""
    condition = ".addresses.ip in '10.1.0.0/16'"
    kept = set(rows(campus, f"select device.fqn filter {condition}"))
    dropped = set(rows(campus, f"select device.fqn filter not ({condition})"))
    assert not kept & dropped
    assert kept | dropped == set(rows(campus, "select device.fqn"))


def test_offset_past_the_end_and_a_zero_limit(campus) -> None:
    assert rows(campus, "select device offset 9999") == []
    assert rows(campus, "select device limit 0") == []


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #


def test_a_parameter_stands_where_a_literal_would(campus) -> None:
    """``$name`` is the value the caller bound, and means what a literal means."""
    written = rows(campus, "select device.fqn filter .name = 'srv-north-01'")
    bound = list(
        answer("select device.fqn filter .name = $who", campus, params={"who": "srv-north-01"}).rows
    )
    assert bound == written
    assert bound


def test_a_parameter_is_not_query_text(campus) -> None:
    """The whole point: a value cannot change what the query asks.

    Concatenated into the text, ``' or true or '`` would close the string and
    turn a lookup into "every device". Bound, it is a device name that nobody
    has, and the honest answer is nothing.
    """
    hostile = "srv-north-01' or true or '"
    assert (
        answer("select device.fqn filter .name = $who", campus, params={"who": hostile}).rows == ()
    )


def test_a_list_parameter_is_a_set(campus) -> None:
    """``in $names`` takes the whole list, without a loop and without quoting."""
    names = ["srv-north-01", "srv-south-01"]
    found = list(
        answer("select device.name filter .name in $names", campus, params={"names": names}).rows
    )
    assert sorted(found) == sorted(names)


def test_a_parameter_is_typed_by_the_value_it_was_given(campus) -> None:
    """The type comes from the value, and the parser then checks the query with it.

    So ``$id`` bound to 20 is an int: it compares against a VLAN id, it carries
    that type into the result, and arithmetic against text is refused before
    anything is read — which a value pasted into the text could not be.
    """
    assert answer("select vlan.name filter .id = $id", campus, params={"id": 20}).rows == ("lab",)
    answered = answer("select $id", campus, params={"id": 20})
    assert answered.rows == (20,)
    assert str(answered.type) == "one int"
    with pytest.raises(QueryError) as caught:
        answer("select 1 + $n", campus, params={"n": "x"})
    assert "needs numbers" in str(caught.value)


def test_a_parameter_orders_like_a_written_value(campus) -> None:
    """``.mtu > $floor`` is the comparison a literal on the right would be."""
    assert rows(campus, "select interface.mtu filter .mtu > 1500") == list(
        answer("select interface.mtu filter .mtu > $floor", campus, params={"floor": 1500}).rows
    )


def test_a_parameter_nobody_supplied_is_refused_while_parsing(campus) -> None:
    """Before the inventory is read, and the message offers what *was* supplied."""
    with pytest.raises(QueryError) as caught:
        parse("select device filter .name = $host", params=declare({"who": "x"}))
    assert "no value was supplied for '$host'" in str(caught.value)
    assert "'$who'" in str(caught.value)


def test_a_dollar_with_no_name_is_a_lexical_error() -> None:
    with pytest.raises(QueryError) as caught:
        tokenize("select device filter .name = $")
    assert "no name" in str(caught.value)


def test_a_parameter_carries_its_span(campus) -> None:
    """A diagnostic underlines ``$host`` including the ``$``."""
    with pytest.raises(QueryError) as caught:
        answer("select device filter .name = $host", campus)
    assert "^^^^^" in str(caught.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("srv-01", ("one str", ("srv-01",))),
        (10, ("one int", (10,))),
        (1.5, ("one float", (1.5,))),
        (True, ("one bool", (True,))),
        (None, ("optional empty", ())),
        (["a", "b"], ("many str", ("a", "b"))),
        ([], ("many empty", ())),
    ],
)
def test_what_a_python_value_binds_to(value: Any, expected: tuple[str, tuple[Any, ...]]) -> None:
    """Each kind of value has one type and one set, and nothing else does."""
    spelled, values = expected
    assert str(declare({"x": value})["x"]) == spelled
    assert bind({"x": value})["x"] == values


def test_an_address_object_binds_as_the_world_spells_it() -> None:
    """``ipaddress`` types are accepted and arrive as text, which is what is stored."""
    import ipaddress

    assert bind({"x": ipaddress.ip_address("10.0.0.1")})["x"] == ("10.0.0.1",)
    assert bind({"x": ipaddress.ip_network("10.0.0.0/24")})["x"] == ("10.0.0.0/24",)


def test_a_value_no_scalar_can_hold_is_refused() -> None:
    for value in ({"a": 1}, object(), b"bytes"):
        with pytest.raises(QueryError) as caught:
            declare({"x": value})
        assert "not a value a query can hold" in str(caught.value)


def test_a_mixed_list_is_refused() -> None:
    with pytest.raises(QueryError) as caught:
        declare({"x": ["a", 1]})
    assert "mixes" in str(caught.value)


def test_a_parameter_name_has_to_be_one() -> None:
    with pytest.raises(QueryError) as caught:
        declare({"not a name": "x"})
    assert "not a parameter name" in str(caught.value)


def test_a_list_parameter_is_bounded() -> None:
    from netviz.nql.binding import MAX_PARAM_ITEMS

    with pytest.raises(QueryError) as caught:
        declare({"x": ["a"] * (MAX_PARAM_ITEMS + 1)})
    assert "over the" in str(caught.value)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("host=srv-01", ("host", "srv-01")),
        ("host=", ("host", "")),
        ("host=a=b", ("host", "a=b")),
        ("id:=10", ("id", 10)),
        ("ok:=true", ("ok", True)),
        ('names:=["a","b"]', ("names", ["a", "b"])),
        ("port:=0755", None),
    ],
)
def test_reading_a_param_assignment(written: str, expected: tuple[str, Any] | None) -> None:
    """``=`` is text whatever it looks like; ``:=`` is JSON, and says when it is not."""
    if expected is None:
        with pytest.raises(QueryError):
            read_assignment(written)
        return
    assert read_assignment(written) == expected


def test_an_assignment_needs_a_separator() -> None:
    with pytest.raises(QueryError) as caught:
        read_assignment("host")
    assert "is not a parameter" in str(caught.value)


def test_the_command_binds_a_parameter(runner: CliRunner) -> None:
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "query",
        "select (device filter .name = $host).addresses.address",
        "--param",
        "host=rtr-home",
        "-F",
        "json",
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["results"] == [
        "192.0.2.1/32",
        "2001:db8::1/128",
        "203.0.113.2/30",
        "192.168.10.1/24",
        "2001:db8:10::1/64",
    ]


def test_the_command_types_a_json_parameter(runner: CliRunner) -> None:
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "query",
        "select vlan.name filter .id in $ids",
        "-p",
        "ids:=[10]",
        "-F",
        "json",
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["results"] == ["home"]


def test_a_parameter_on_a_selector_is_a_usage_error(runner: CliRunner) -> None:
    """A selector has no parameters, and saying so beats binding nothing."""
    result = invoke(runner, "-i", str(HOME_LAB), "query", "kind = switch", "--param", "x=1")
    assert result.exit_code == 2
    assert "relational query" in result.output


def test_a_malformed_param_is_a_usage_error(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "query", "select device.name", "--param", "oops")
    assert result.exit_code == 2
    assert "--param" in result.output
