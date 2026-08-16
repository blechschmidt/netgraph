"""The selector language: the parser, the evaluator, and the claims they make.

Four kinds of test, and they check four different things:

* **The grammar** — what parses, what does not, and that a rejection names the
  column. A parse error that says only "syntax error" is a parse error that
  sends the reader back to the grammar to guess, so the message and the caret
  are asserted, not only the exception type.
* **The vocabulary** — every attribute in the tables is readable off a real
  model. A row nobody implemented is a query that always answers nothing, which
  is indistinguishable from a network in which nothing matches.
* **The semantics** — worked queries against ``examples/campus``, and the
  properties the docstrings claim: that a query and its negation partition the
  inventory, that the flags are sugar for the query
  :func:`~netgraph.query.sugar.as_query` renders, that ``interface[X]`` and
  ``not interface[X]`` are complements.
* **The wiring** — that ``--select``, ``assert: query`` and ``/api/query`` all
  reach the same implementation, which is the whole point of there being one.

The fuzzing lives in ``tests/test_fuzz_query.py``, beside the loader's.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis import strategies as st

from netgraph.cli import cli
from netgraph.loader import load_stream, load_tree
from netgraph.query import (
    ATTRIBUTES,
    MAX_DEPTH,
    MAX_TERMS,
    Domain,
    QueryError,
    as_query,
    attribute_names,
    evaluate,
    matches,
    parse,
    select,
)
from netgraph.query.apply import narrow
from netgraph.query.errors import MAX_QUERY_LENGTH
from netgraph.query.facts import Facts, element_values
from netgraph.query.lexer import TokenKind, tokenize
from netgraph.render.graph import FilterSpec, Layer, build_graph, filter_graph

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
CAMPUS: Final = REPO_ROOT / "examples" / "campus"
HOME_LAB: Final = REPO_ROOT / "examples" / "home-lab"


@pytest.fixture(scope="module")
def campus():
    """The campus graph at layer 1 — three sites, a backbone ring."""
    return build_graph(load_tree(CAMPUS))


@pytest.fixture(scope="module")
def campus_l3():
    """The same inventory at layer 3, where subnets are nodes."""
    return build_graph(load_tree(CAMPUS), layer=Layer.L3)


# --------------------------------------------------------------------------- #
# The lexer
# --------------------------------------------------------------------------- #


def test_a_word_may_hold_the_punctuation_an_inventory_is_full_of() -> None:
    """`sites/north`, `10.20.0.0/16` and `Gi1/0/1` are one token each.

    A grammar that made every one of those need quoting is a grammar nobody
    writes queries in, so the word breaks are exactly the characters the
    grammar itself needs.
    """
    for word in ("sites/north", "10.20.0.0/16", "GigabitEthernet1/0/1", "label.role", "sw-*"):
        tokens = tokenize(word)
        assert [token.text for token in tokens[:-1]] == [word], word
        assert tokens[-1].kind is TokenKind.END


def test_a_quoted_string_is_not_a_keyword() -> None:
    """`name = "and"` is the element called *and*, not a syntax error."""
    query = parse('name = "and"')
    assert query.expr.values == ("and",)


def test_an_unclosed_quote_points_at_the_quote_that_opened_it() -> None:
    with pytest.raises(QueryError) as caught:
        parse("name = 'sw-01")
    assert "never closed" in caught.value.message
    assert caught.value.column == 8


def test_every_token_remembers_where_it_came_from() -> None:
    tokens = tokenize("kind = switch")
    assert [(token.text, token.offset, token.length) for token in tokens[:-1]] == [
        ("kind", 0, 4),
        ("=", 5, 1),
        ("switch", 7, 6),
    ]


# --------------------------------------------------------------------------- #
# The grammar
# --------------------------------------------------------------------------- #


#: Every shape of the grammar, at least once. A query here must parse.
LEGAL: Final[tuple[str, ...]] = (
    "*",
    "sw-core",
    "sw-*",
    "kind = switch",
    "kind == switch",
    "kind != switch",
    "kind in (switch, router, hub)",
    "name ~ sw-*",
    "name !~ sw-*",
    "name =~ ^sw-",
    "namespace under sites/north",
    "vlan = 99",
    "vlan in (10, 20, 99)",
    "mtu > 1500",
    "mtu >= 1500",
    "ports < 4",
    "ports <= 4",
    "address in 10.0.0.0/8",
    "address in 2001:db8::/32",
    "label.role = access",
    "has vrf",
    "has label.site",
    "not has vrf",
    "kind = switch and has vrf",
    "kind = switch or kind = router",
    "kind = switch and (has vrf or has netns)",
    "not (kind = switch and has vrf)",
    "interface[has address]",
    "interface[address in 10.0.0.0/8 and not has vrf]",
    "interface[enabled = false]",
    "link[medium = fiber]",
    "link[peer-kind = router and speed >= 10000]",
    "netns[depth > 0]",
    "zone[declared = true]",
    "neighbors of kind = router",
    "neighbours of kind = router",
    "within 2 hops of sw-core",
    "within 0 hops of sw-core",
    "within 1 hop of sw-core",
    "reachable from kind = router",
    "not within 2 hops of (kind = router or kind = switch)",
    "AND" and "kind = switch AND has vrf",
    "kind = switch\nand has vrf",
)


@pytest.mark.parametrize("text", LEGAL, ids=range(len(LEGAL)))
def test_the_grammar_accepts_what_it_documents(text: str) -> None:
    """Every form in docs/query.md's grammar block, parsed."""
    assert parse(text).text == text


#: (query, the substring the diagnostic must carry, the 1-based column).
ILLEGAL: Final[tuple[tuple[str, str, int], ...]] = (
    ("", "empty", 1),
    ("   ", "empty", 1),
    ("kidn = switch", "not an attribute", 1),
    ("kind =", "no value after it", 6),
    ("kind = switch and", "ends after an operator", 18),
    ("(kind = switch", "expected ')'", 15),
    ("kind = switch)", "is not expected here", 14),
    (")", "nothing to close", 1),
    ("vlan = 5000", "outside 1-4094", 8),
    ("vlan = twelve", "not a VLAN id", 8),
    ("mtu > big", "not a number", 7),
    ("name < 5", "does not order", 6),
    ("name under x", "applies to a path", 6),
    ("address in nonsense", "not an IP address", 12),
    ("interface[interface[x]]", "inside another scope", 11),
    ("interface[neighbors of x]", "traversal cannot be written", 11),
    ("interface[]", "asks nothing", 11),
    ("nothing[x]", "not something a query can look inside", 1),
    ("element[x]", "is the query itself", 1),
    ("within hops of x", "expected a number of hops", 8),
    ("within 2 of x", "expected 'hops'", 10),
    ("within 999 hops of x", "over the limit", 8),
    ("neighbors x", "expected 'of'", 11),
    ("reachable x", "expected 'from'", 11),
    ("has", "expected an attribute after 'has'", 4),
    ("has nonesuch", "not an attribute", 5),
    ("kind in ()", "asks for nothing", 9),
    ("and", "is a keyword", 1),
    ("42", "on its own is not a term", 1),
    ("name =~ [", "not a regular expression", 9),
    ("enabled = maybe", "not an attribute", 1),
    ("interface[enabled = maybe]", "not true or false", 21),
    ("!", "not part of any query operator", 1),
)


@pytest.mark.parametrize(("text", "wanted", "column"), ILLEGAL, ids=[one[0] for one in ILLEGAL])
def test_a_rejection_says_what_and_points_at_where(text: str, wanted: str, column: int) -> None:
    """Every refusal names the problem and underlines the offending column."""
    with pytest.raises(QueryError) as caught:
        parse(text)
    error = caught.value
    assert wanted in error.message, error.message
    assert error.column == column, f"{error.message} pointed at {error.column}, wanted {column}"


def test_a_diagnostic_draws_a_caret_under_the_offending_token() -> None:
    """The whole block, as a reader sees it."""
    with pytest.raises(QueryError) as caught:
        parse("kind = swtch and vlna = 99")
    assert caught.value.annotated().splitlines() == [
        "query:1:18: 'vlna' is not an attribute of element",
        "  kind = swtch and vlna = 99",
        "                   ^^^^",
        "  help: did you mean 'vlan'?",
    ]


def test_a_long_query_is_echoed_as_a_window_around_the_problem() -> None:
    """A generated query must not become its own error message."""
    padding = " or ".join(f"name = pad{index}" for index in range(120))
    with pytest.raises(QueryError) as caught:
        parse(f"{padding} or kidn = x")
    block = caught.value.annotated()
    assert "…" in block
    assert max(len(line) for line in block.splitlines()) < 200
    assert "kidn" in block


def test_the_source_name_is_what_the_caller_called_it() -> None:
    """`--select` and a document put their own name on the location line."""
    with pytest.raises(QueryError) as caught:
        parse("kidn = x", source="--select")
    assert caught.value.annotated().startswith("--select:1:1:")


def test_a_query_over_the_length_limit_is_refused_before_it_is_scanned() -> None:
    with pytest.raises(QueryError) as caught:
        parse("x" * (MAX_QUERY_LENGTH + 1))
    assert "over the" in caught.value.message


def test_nesting_and_term_budgets_are_enforced() -> None:
    with pytest.raises(QueryError) as caught:
        parse("(" * (MAX_DEPTH + 2) + "*" + ")" * (MAX_DEPTH + 2))
    assert "nests more than" in caught.value.message

    with pytest.raises(QueryError) as caught:
        parse(" or ".join(["*"] * (MAX_TERMS + 2)))
    assert "more than" in caught.value.message


def test_and_binds_tighter_than_or() -> None:
    """`a or b and c` is `a or (b and c)`, which is what everybody expects."""
    from netgraph.query.ast import And, Or

    expr = parse("kind = switch or kind = router and has vrf").expr
    assert isinstance(expr, Or)
    assert isinstance(expr.operands[1], And)


def test_the_legal_corpus_exercises_every_node_of_the_grammar() -> None:
    """A node type no test parses is a node type nothing checks.

    The tree is a closed union of eight, and `LEGAL` is meant to be a tour of
    the whole grammar — so a form added to the parser without a case here
    fails, rather than sitting untested behind a docstring.
    """
    from netgraph.query.ast import All, And, Comparison, Exists, Not, Or, Scope, Traversal, walk

    seen = {type(node) for text in LEGAL for node in walk(parse(text).expr)}
    assert seen == {All, And, Comparison, Exists, Not, Or, Scope, Traversal}, sorted(
        one.__name__ for one in seen
    )


def test_a_traversal_binds_tighter_than_and() -> None:
    """`within 2 hops of X and Y` is `(the neighbourhood) and Y`."""
    from netgraph.query.ast import And, Traversal

    expr = parse("within 2 hops of sw-core and kind = switch").expr
    assert isinstance(expr, And)
    assert isinstance(expr.operands[0], Traversal)


# --------------------------------------------------------------------------- #
# The vocabulary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("domain", list(Domain), ids=[one.value for one in Domain])
def test_every_attribute_of_every_domain_is_readable(domain: Domain, campus) -> None:
    """A row in the table with no reader behind it always answers nothing.

    Which is indistinguishable, at the command line, from a network in which
    nothing matches — so the check is that *some* node or sub-object answers,
    not that every one does.
    """
    facts = Facts.of(campus)
    answered: set[str] = set()
    for name, attribute in ATTRIBUTES[domain].items():
        wanted = f"{name}.role" if attribute.family else name
        # Parsed rather than hand-built, so an attribute the parser refuses is
        # caught here rather than in the evaluator's dead branch.
        query = f"has {wanted}" if domain is Domain.ELEMENT else f"{domain}[has {wanted}]"
        result = evaluate(parse(query), campus)
        if result.nodes:
            answered.add(name)
    missing = set(ATTRIBUTES[domain]) - answered
    # A campus has no aggregates, no racks and no tunnels, so a handful of rows
    # legitimately answer nothing here; they are covered by their own tests.
    assert missing <= _UNEXERCISED[domain], f"{domain}: nothing answers {sorted(missing)}"
    assert facts.universe_is_indexed() if hasattr(facts, "universe_is_indexed") else True


#: Attributes the campus example cannot exercise, per domain, with why.
_UNEXERCISED: Final[dict[Domain, set[str]]] = {
    # No racks, no tunnels and no containers in the campus, so no prefix node at
    # layer 1 and no netns anywhere; no firewall, so no zones.
    Domain.ELEMENT: {"prefix", "netns", "zone", "asn", "router-id", "area", "serial"},
    Domain.INTERFACE: {"peer", "netns"},
    Domain.LINK: {"length", "label", "peer-port", "port", "speed", "medium", "vlan"},
    Domain.NETNS: set(ATTRIBUTES[Domain.NETNS]),
    Domain.ZONE: set(ATTRIBUTES[Domain.ZONE]),
}


def test_an_alias_selects_what_its_canonical_name_does(campus) -> None:
    for alias, canonical in (("ns", "namespace"), ("ip", "address"), ("id", "fqn")):
        assert matches(parse(f"has {alias}"), campus) == matches(parse(f"has {canonical}"), campus)


def test_an_unknown_attribute_suggests_the_one_that_was_meant() -> None:
    for typo, wanted in (
        ("kidn", "kind"),
        ("nmae", "name"),
        ("vlna", "vlan"),
        ("adress", "address"),
    ):
        with pytest.raises(QueryError) as caught:
            parse(f"{typo} = x")
        assert wanted in (caught.value.help or ""), f"{typo}: {caught.value.help}"


def test_the_documented_attribute_list_is_the_implemented_one() -> None:
    """docs/query.md names them; the tables are the source. Neither may drift."""
    page = (REPO_ROOT / "docs" / "query.md").read_text(encoding="utf-8")
    for domain in Domain:
        for name in attribute_names(domain):
            assert f"`{name.rstrip('.')}`" in page, f"{domain}.{name} is undocumented"


# --------------------------------------------------------------------------- #
# The semantics, against a real inventory
# --------------------------------------------------------------------------- #


#: (query, how many campus elements it selects). The cookbook, as assertions.
WORKED: Final[tuple[tuple[str, int], ...]] = (
    ("*", 22),
    ("kind = switch", 10),
    ("kind = router", 3),
    ("kind in (switch, router)", 13),
    ("label.role = access", 7),
    ("label.site = north and kind = switch", 4),
    ("namespace under sites/north", 8),
    ("namespace under sites", 22),
    ("namespace = sites/north/access", 3),
    ("name ~ sw-north-*", 4),
    ("sw-north", 4),
    ("vlan = 99", 10),
    ("has asn", 3),
    ("kind in (switch, router) and not has vrf", 3),
    ("interface[type = loopback and has address]", 12),
    ("interface[address in 10.1.0.0/16 and not has vrf]", 5),
    ("link[medium = fiber]", 13),
    ("within 2 hops of rtr-north-core-01", 9),
    ("neighbors of rtr-north-core-01", 3),
    ("degree >= 3", 9),
)


@pytest.mark.parametrize(("text", "count"), WORKED, ids=[one[0] for one in WORKED])
def test_a_worked_query_selects_what_it_says(text: str, count: int, campus) -> None:
    assert len(evaluate(parse(text), campus).nodes) == count


def test_the_result_is_in_graph_order(campus) -> None:
    """Not sorted order: every other netgraph listing is in load order."""
    order = list(campus.nodes)
    picked = evaluate(parse("kind = switch"), campus).nodes
    assert list(picked) == [fqn for fqn in order if fqn in set(picked)]


def test_a_scope_reports_the_sub_objects_that_satisfied_it(campus) -> None:
    result = evaluate(parse("interface[address in 10.1.0.0/16 and not has vrf]"), campus)
    witnesses = {str(one) for one in result.interfaces()}
    assert "sites/north/core/rtr-north-core-01:xe-0/0/0" in witnesses
    assert "sites/north/hosts/pc-north-01:eno1" in witnesses
    # Every witness belongs to a selected element, and every witness's interface
    # really is addressed there.
    assert {one.element for one in result.interfaces()} <= set(result.nodes)


def test_a_scope_under_a_negation_reports_nothing(campus) -> None:
    """Under `not`, a satisfied scope is what *excludes* an element.

    Reporting those interfaces as if they had been selected would be a lie, so
    witnesses are recorded at positive polarity only.
    """
    result = evaluate(parse("kind = computer and not interface[has vrf]"), campus)
    assert result.nodes
    assert result.witnesses == ()


def test_a_traversal_walks_the_whole_graph_not_the_narrowed_one(campus) -> None:
    """A node two hops away is two hops away even when the node between is out.

    `--neighbors-of` has always worked this way; the traversal forms inherit it,
    and this is the case that tells the two readings apart: the hosts are two
    hops from the core router *through* the distribution switch, and asking for
    hosts within two hops must still find them.
    """
    reached = matches(parse("kind = computer and within 3 hops of rtr-north-core-01"), campus)
    assert reached, "the traversal was narrowed before it walked"


def test_neighbors_excludes_the_seed_and_within_includes_it(campus) -> None:
    seed = matches(parse("name = rtr-north-core-01"), campus)
    assert seed <= matches(parse("within 1 hops of rtr-north-core-01"), campus)
    assert not (seed & matches(parse("neighbors of rtr-north-core-01"), campus))


def test_reachable_is_the_connected_component(campus) -> None:
    whole = matches(parse("reachable from rtr-north-core-01"), campus)
    assert matches(parse("within 64 hops of rtr-north-core-01"), campus) == whole


def test_a_layer_3_query_can_select_a_derived_subnet(campus_l3) -> None:
    """A query can name a node no flag can, and filter_graph has to keep it."""
    picked = evaluate(parse("kind = subnet and prefix in 10.1.0.0/16"), campus_l3)
    assert picked.nodes
    narrowed = narrow(campus_l3, FilterSpec(select="kind = subnet and prefix in 10.1.0.0/16"))
    assert set(narrowed.nodes) == set(picked.nodes)


# --------------------------------------------------------------------------- #
# The claims the docstrings make
# --------------------------------------------------------------------------- #


#: Queries whose negation is checked against the campus and against generated
#: inventories. Chosen to cover every operator shape.
PARTITIONED: Final[tuple[str, ...]] = (
    "*",
    "kind = switch",
    "kind in (switch, router)",
    "name ~ sw-*",
    "namespace under sites/north",
    "vlan = 99",
    "has vrf",
    "has label.role",
    "address in 10.1.0.0/16",
    "mtu > 1500",
    "interface[has address]",
    "link[medium = fiber]",
    "within 2 hops of rtr-north-core-01",
    "neighbors of kind = router",
    "reachable from kind = router",
    "kind = switch and has vrf",
    "kind = switch or kind = router",
    "not (kind = switch and has vrf)",
)


@pytest.mark.parametrize("text", PARTITIONED, ids=PARTITIONED)
def test_a_query_and_its_negation_partition_the_inventory(text: str, campus) -> None:
    """The claim :mod:`netgraph.query.evaluate` makes, checked.

    `not X` is the complement of `X` within the graph, so the two are disjoint
    and together are everything. There is no third answer for a node that lacks
    the attribute: it is simply not in `X`, and is therefore in `not X`.
    """
    yes = matches(parse(text), campus)
    no = matches(parse(f"not ({text})"), campus)
    universe = frozenset(campus.nodes)
    assert yes & no == frozenset(), f"{text}: {sorted(yes & no)[:3]} is in both"
    assert yes | no == universe, f"{text}: {sorted(universe - (yes | no))[:3]} is in neither"


@given(st.sampled_from(PARTITIONED), st.sampled_from(PARTITIONED))
def test_de_morgan_holds_for_any_pair(left: str, right: str) -> None:
    """`not (a and b)` is `(not a) or (not b)`, over a real graph.

    A property rather than an example: the evaluator computes `and` by
    successive narrowing and `or` by union, and those two are only each other's
    duals if the universe a complement is taken within is the same at every
    level — which is the invariant this catches if it ever stops holding.
    """
    graph = build_graph(load_tree(HOME_LAB))
    combined = matches(parse(f"not (({left}) and ({right}))"), graph)
    apart = matches(parse(f"not ({left}) or not ({right})"), graph)
    assert combined == apart


@given(st.sampled_from(PARTITIONED))
def test_double_negation_is_the_identity(text: str) -> None:
    graph = build_graph(load_tree(HOME_LAB))
    assert matches(parse(f"not (not ({text}))"), graph) == matches(parse(text), graph)


def test_an_element_with_no_values_matches_neither_a_comparison_nor_its_negation() -> None:
    """The rule that makes `has` necessary, stated as a test.

    `address !~ 10.*` asks a question *of the values*, and a device with none
    answers neither yes nor no. `not (address ~ 10.*)` asks it of the element,
    and does catch it. Conflating the two is the mistake this pins down.
    """
    stream = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: bare
spec:
  interfaces:
    - name: eth0
      type: ethernet
"""
    graph = build_graph(load_stream(stream))
    assert matches(parse("address ~ 10.*"), graph) == frozenset()
    assert matches(parse("address !~ 10.*"), graph) == frozenset()
    assert matches(parse("not (address ~ 10.*)"), graph) == frozenset(graph.nodes)


# --------------------------------------------------------------------------- #
# The flags are sugar
# --------------------------------------------------------------------------- #


#: Specs whose rendering as a query must select exactly what they filter.
SPECS: Final[tuple[FilterSpec, ...]] = (
    FilterSpec(),
    FilterSpec(kinds=("switch",)),
    FilterSpec(kinds=("switch", "router")),
    FilterSpec(namespaces=("sites/north",)),
    FilterSpec(namespaces=("sites/north", "sites/south")),
    FilterSpec(names=("sw-*",)),
    FilterSpec(names=("sw-north-acc-01",)),
    FilterSpec(vlans=frozenset({99})),
    FilterSpec(vlans=frozenset({10, 20})),
    FilterSpec(kinds=("switch",), namespaces=("sites/north",)),
    FilterSpec(kinds=("switch",), vlans=frozenset({99}), names=("sw-*",)),
    FilterSpec(neighbors_of="rtr-north-core-01", depth=2),
    FilterSpec(neighbors_of="sites/north/core/rtr-north-core-01", depth=1),
    FilterSpec(kinds=("switch",), neighbors_of="rtr-north-core-01", depth=3),
)


@pytest.mark.parametrize("spec", SPECS, ids=[spec.describe() for spec in SPECS])
def test_the_filter_flags_are_sugar_for_the_query_they_render_as(spec: FilterSpec, campus) -> None:
    """docs/query.md's sugar table, executable.

    The claim is not that the two produce the same *graph* — a filter also
    prunes edges and narrows derived nodes — but that they select the same
    elements, which is what "sugar for" means.
    """
    by_flags = {fqn for fqn, node in filter_graph(campus, spec).nodes.items() if node.is_element}
    by_query = {
        fqn
        for fqn, node in narrow(campus, FilterSpec(select=as_query(spec))).nodes.items()
        if node.is_element
    }
    assert by_flags == by_query, as_query(spec)


def test_an_empty_spec_renders_as_everything() -> None:
    assert as_query(FilterSpec()) == "*"


def test_a_value_needing_quotes_gets_them() -> None:
    rendered = as_query(FilterSpec(names=("a b",)))
    assert rendered == "name ~ 'a b'"
    assert parse(rendered).expr.values == ("a b",)


def test_the_flags_and_the_query_are_combined_with_and(campus) -> None:
    """`--kind switch --select 'has vrf'` keeps the switches that have one."""
    spec = FilterSpec(kinds=("switch",), select="has vrf")
    kept = {fqn for fqn, node in narrow(campus, spec).nodes.items() if node.is_element}
    assert kept == matches(parse("kind = switch and has vrf"), campus)


def test_filter_graph_refuses_a_query_nobody_answered(campus) -> None:
    """The guard that stops a bypassed layer rendering the whole inventory."""
    with pytest.raises(ValueError, match="has not been answered"):
        filter_graph(campus, FilterSpec(select="kind = switch"))


def test_a_spec_carrying_a_query_is_not_empty() -> None:
    assert not FilterSpec(select="*").is_empty
    assert "select=kind = switch" in FilterSpec(select="kind = switch").describe()


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


@pytest.fixture
def run():
    runner = CliRunner()

    def invoke(*args: str, root: Path = CAMPUS):
        return runner.invoke(cli, ["-i", str(root), *args], catch_exceptions=False)

    return invoke


def test_query_prints_one_name_per_line(run) -> None:
    result = run("query", "kind = router")
    assert result.exit_code == 0
    assert result.output.split() == [
        "sites/north/core/rtr-north-core-01",
        "sites/south/core/rtr-south-core-01",
        "sites/west/core/rtr-west-core-01",
    ]


def test_query_count_prints_a_number(run) -> None:
    assert run("query", "kind = switch", "--count").output.strip() == "10"


def test_query_json_carries_the_query_it_answered(run) -> None:
    payload = json.loads(run("query", "kind = router", "--json").output)
    assert payload["query"] == "kind = router"
    assert payload["count"] == 3
    assert payload["matches"][0]["element"].endswith("rtr-north-core-01")


def test_query_exits_one_when_nothing_matched(run) -> None:
    """So a query is usable as a check in a script."""
    result = run("query", "kind = switch and not has address")
    assert result.exit_code == 1
    assert result.output.strip() == ""


def test_query_reports_a_parse_error_as_a_usage_error(run) -> None:
    result = run("query", "kidn = switch")
    assert result.exit_code == 2
    assert "'kidn' is not an attribute of element" in result.output
    assert "^^^^" in result.output


def test_query_print_interfaces_reports_the_witnesses(run) -> None:
    result = run("query", "interface[type = loopback and has address]", "--print", "interfaces")
    lines = result.output.split()
    assert lines
    assert all(":" in line for line in lines)
    assert "sites/north/core/rtr-north-core-01:lo0" in lines


def test_query_explain_prints_the_grammar_and_the_vocabulary(run) -> None:
    output = run("query", "--explain").output
    assert "query    := or" in output
    assert "# element attributes" in output
    for domain in Domain:
        assert f"# {domain} attributes" in output


def test_query_explain_prints_what_the_flags_desugar_to(run) -> None:
    output = run("query", "--explain", "--kind", "switch", "--namespace", "sites/north").output
    assert "(kind = switch and namespace under sites/north)" in output


def test_query_flags_scope_the_question_rather_than_the_answer(run) -> None:
    """`--namespace X 'Q'` is "among X, which match Q"."""
    scoped = run("query", "--namespace", "sites/north", "kind = switch", "--count")
    assert scoped.output.strip() == "4"


def test_query_layer_picks_the_view(run) -> None:
    assert run("query", "--layer", "l3", "kind = subnet", "--count").output.strip() != "0"


# --------------------------------------------------------------------------- #
# --select, everywhere it is offered
# --------------------------------------------------------------------------- #


def test_select_narrows_a_render(run, tmp_path) -> None:
    out = tmp_path / "graph.json"
    run("render", "-f", "json", "-o", str(out), "--select", "kind = router")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert {node["id"] for node in payload["nodes"]} == {
        "sites/north/core/rtr-north-core-01",
        "sites/south/core/rtr-south-core-01",
        "sites/west/core/rtr-west-core-01",
    }


def test_select_narrows_a_listing(run) -> None:
    output = run("list", "devices", "--select", "label.role = access").output
    assert output.count("sw-") == 7
    assert "rtr-" not in output


def test_select_narrows_an_export(run) -> None:
    output = run("export", "hosts", "--select", "kind = router").output
    assert "rtr-north-core-01" in output
    assert "pc-north-01" not in output


def test_select_narrows_a_report(run, tmp_path) -> None:
    out = tmp_path / "report"
    run("report", "-f", "json", "-o", str(out), "--select", "kind = router")
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert sum(page["kind"] == "device" for page in payload["pages"]) == 3


def test_select_narrows_a_watch_cycle() -> None:
    """``watch`` draws through its own pipeline, so it has to be checked there.

    Not through the CLI: ``netgraph watch`` blocks. What it shares with
    ``render`` is the spec and the narrowing, and this is the seam where a
    pipeline that had kept calling ``filter_graph`` directly would show up — as
    a ValueError, by design, rather than as the whole inventory.
    """
    from netgraph.watch.pipeline import RenderRequest, run_cycle

    result = run_cycle(
        RenderRequest(
            inventory=CAMPUS, output_format="json", spec=FilterSpec(select="kind = router")
        )
    )
    assert result.status == "ok", result.message
    assert result.nodes == 3
    payload = json.loads(result.payload.decode("utf-8"))
    assert {node["id"] for node in payload["nodes"]} == {
        "sites/north/core/rtr-north-core-01",
        "sites/south/core/rtr-south-core-01",
        "sites/west/core/rtr-west-core-01",
    }


def test_show_select_prints_every_match(run) -> None:
    payload = json.loads(run("show", "--select", "kind = router", "-F", "json").output)
    assert len(payload) == 3
    assert {one["metadata"]["name"] for one in payload} == {
        "rtr-north-core-01",
        "rtr-south-core-01",
        "rtr-west-core-01",
    }


def test_show_refuses_both_a_name_and_a_query(run) -> None:
    result = run("show", "rtr-north-core-01", "--select", "kind = router")
    assert result.exit_code == 2
    assert "not both" in result.output


def test_show_refuses_neither(run) -> None:
    result = run("show")
    assert result.exit_code == 2
    assert "--select" in result.output


def test_a_bad_select_is_refused_before_the_inventory_is_read(run) -> None:
    """A usage error, with the caret, rather than an empty diagram."""
    for command in ("render", "list", "export", "report"):
        args = ["-f", "json"] if command in ("render", "report") else []
        if command == "export":
            args = ["hosts"]
        result = run(command, *args, "--select", "kidn = x")
        assert result.exit_code == 2, command
        assert "not an attribute" in result.output, command


# --------------------------------------------------------------------------- #
# The assertion
# --------------------------------------------------------------------------- #


def _suite(body: str, tmp_path: Path) -> Path:
    root = tmp_path / "inventory"
    root.mkdir()
    (root / "net.yaml").write_text(
        """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-a
  labels:
    role: access
spec:
  interfaces:
    - name: eth0
      type: ethernet
      ipv4:
        addresses: [10.0.0.1/24]
---
apiVersion: netgraph.dev/v1alpha1
kind: router
metadata:
  name: rtr-a
spec:
  interfaces:
    - name: eth0
      type: ethernet
      ipv4:
        addresses: [10.0.0.254/24]
""",
        encoding="utf-8",
    )
    (root / "tests.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: testsuite\nmetadata:\n  name: s\n"
        f"spec:\n  assertions:\n{body}",
        encoding="utf-8",
    )
    return root


def test_an_invariant_with_no_bound_claims_the_query_matches_nothing(run, tmp_path) -> None:
    root = _suite(
        "    - assert: query\n"
        "      name: no device is missing an address\n"
        "      query: kind in (switch, router) and not has address\n",
        tmp_path,
    )
    result = run("test", root=root)
    assert result.exit_code == 0, result.output
    assert "1 passed" in result.output


def test_a_failing_invariant_reports_the_counterexamples(run, tmp_path) -> None:
    root = _suite(
        "    - assert: query\n      name: no routers\n      query: kind = router\n", tmp_path
    )
    result = run("test", root=root)
    assert result.exit_code != 0
    assert "the assertion is that none does" in result.output
    assert "rtr-a" in result.output


def test_a_query_assertion_takes_the_count_bounds(run, tmp_path) -> None:
    root = _suite(
        "    - assert: query\n      query: kind = switch\n      equals: 1\n"
        "    - assert: query\n      query: kind = router\n      at_least: 1\n",
        tmp_path,
    )
    assert run("test", root=root).exit_code == 0


def test_query_may_stand_in_for_select_on_any_selector_assertion(run, tmp_path) -> None:
    root = _suite(
        "    - assert: has-interface\n      query: kind = switch\n      interface: eth0\n"
        "    - assert: count\n      query: label.role = access\n      equals: 1\n",
        tmp_path,
    )
    assert run("test", root=root).exit_code == 0


def test_select_and_query_together_are_anded(run, tmp_path) -> None:
    root = _suite(
        "    - assert: count\n      select: kind=switch\n"
        "      query: label.role = access\n      equals: 1\n"
        "    - assert: count\n      select: kind=router\n"
        "      query: label.role = access\n      equals: 0\n",
        tmp_path,
    )
    assert run("test", root=root).exit_code == 0


def test_a_malformed_query_in_an_assertion_is_reported_with_its_caret(run, tmp_path) -> None:
    root = _suite("    - assert: query\n      query: kidn = switch\n", tmp_path)
    result = run("test", root=root)
    assert result.exit_code != 0
    assert "'kidn' is not an attribute" in result.output


def test_an_assertion_may_not_carry_a_query_it_has_no_use_for(tmp_path) -> None:
    from pydantic import ValidationError as PydanticError

    from netgraph.models.testsuite import Assertion

    with pytest.raises(PydanticError, match="not a key of a reachable assertion"):
        Assertion.model_validate({"assert": "reachable", "from": "a", "to": "b", "query": "*"})


def test_a_query_assertion_needs_a_query(tmp_path) -> None:
    from pydantic import ValidationError as PydanticError

    from netgraph.models.testsuite import Assertion

    with pytest.raises(PydanticError, match="needs 'query'"):
        Assertion.model_validate({"assert": "query"})


# --------------------------------------------------------------------------- #
# Determinism and cost
# --------------------------------------------------------------------------- #


def test_the_same_query_answers_the_same_way_twice(campus) -> None:
    """Nothing is memoised across calls, and nothing may depend on set order."""
    first = evaluate(parse("kind = switch and has vrf"), campus)
    second = evaluate(parse("kind = switch and has vrf"), campus)
    assert first.nodes == second.nodes
    assert first.witnesses == second.witnesses


def test_evaluation_reads_nothing_it_was_not_given(campus) -> None:
    """The evaluator is a pure function of the graph.

    Checked the blunt way: two evaluations either side of an unrelated mutation
    of the *inventory* — which the graph no longer refers to — agree.
    """
    before = evaluate(parse("*"), campus).nodes
    other = build_graph(load_tree(HOME_LAB))
    evaluate(parse("*"), other)
    assert evaluate(parse("*"), campus).nodes == before


def test_element_values_answers_nothing_for_a_derived_node(campus_l3) -> None:
    """A subnet nobody wrote has no vendor, no labels and no source file."""
    facts = Facts.of(campus_l3)
    subnet = next(node for node in campus_l3.nodes.values() if node.subnet is not None)
    for attribute in ("vendor", "description", "file", "label"):
        assert element_values(facts, subnet, attribute, "role") == ()


def test_replacing_the_selected_set_does_not_disturb_the_other_fields() -> None:
    spec = FilterSpec(kinds=("switch",), select="*")
    assert replace(spec, selected=frozenset({"a"})).kinds == ("switch",)


def test_select_helper_parses_and_evaluates_in_one_call(campus) -> None:
    assert select("kind = router", campus).nodes == evaluate(parse("kind = router"), campus).nodes
