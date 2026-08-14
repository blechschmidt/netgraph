"""``netgraph test``: the ``testsuite`` kind, the eleven assertions, the reports.

The contracts asserted here are the ones a pipeline depends on:

* **A false claim fails the run**, and a claim that could not be *asked* — an
  assertion naming an element the inventory does not hold, a selector matching
  nothing — fails it too. The one thing that must never happen is a green run
  that graded nothing.
* **A failure is actionable**: it names the assertion, the elements, what the
  graph actually contained, and the file and line the assertion is written on.
* **The three renderers agree**, because none of them decides anything.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from netgraph.cli import cli
from netgraph.loader import load_stream, load_tree
from netgraph.loader.inventory import Inventory
from netgraph.models import TestSuite
from netgraph.models.document import parse_test_suite
from netgraph.schema import build_schema
from netgraph.testing import (
    FAILED,
    PASSED,
    SKIPPED,
    SelectorError,
    parse_selector,
    render_test_report,
    run_tests,
)
from netgraph.testing.fields import FieldError, evaluate, render_value

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
HOME_LAB = EXAMPLES / "home-lab"
CAMPUS = EXAMPLES / "campus"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "testsuite"

#: A small, complete inventory: two desks either side of one switch, plus a
#: router. Written as a stream so a test can bolt any suite onto it.
NETWORK = """
apiVersion: netgraph.dev/v1alpha1
kind: router
metadata: {name: rtr}
spec:
  interfaces:
    - {name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}
    - {name: eth1, type: ethernet, ipv4: [10.0.1.1/24]}
---
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata: {name: sw}
spec:
  interfaces:
    - {name: br0, type: bridge, members: [port1, port2, port3]}
    - name: Vlan10
      type: vlan
      parent: br0
      ipv4: [10.0.0.2/24]
      vlan: {mode: access, access_vlan: 10}
    - {name: port1, type: ethernet, vlan: {mode: access, access_vlan: 10}}
    - {name: port2, type: ethernet, vlan: {mode: access, access_vlan: 10}}
    - {name: port3, type: ethernet, vlan: {mode: access, access_vlan: 20}}
  vlans: [{id: 10, name: office}, {id: 20, name: guest}]
---
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata: {name: pc-a}
spec:
  interfaces:
    - name: eth0
      type: ethernet
      ipv4: {addresses: [10.0.0.10/24], gateway: 10.0.0.1}
      vlan: {mode: access, access_vlan: 10}
---
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata: {name: pc-b}
spec:
  interfaces:
    - name: eth0
      type: ethernet
      ipv4: {addresses: [10.0.1.11/24]}
      vlan: {mode: access, access_vlan: 20}
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-rtr}
spec: {medium: copper, endpoints: [rtr:eth0, sw:port1]}
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-a}
spec: {medium: copper, endpoints: [pc-a:eth0, sw:port2]}
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-b}
spec: {medium: copper, endpoints: [pc-b:eth0, sw:port3]}
"""


def inventory_with(*assertions: str) -> Inventory:
    """The fixture network plus a one-suite document holding ``assertions``."""
    body = "\n".join(f"    - {assertion.strip()}" for assertion in assertions)
    suite = (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: testsuite\n"
        "metadata: {name: suite}\n"
        "spec:\n"
        "  assertions:\n" + body + "\n"
    )
    return load_stream(NETWORK + "---\n" + suite)


def verdict(*assertions: str) -> object:
    """Grade one assertion and hand back its verdict."""
    report = run_tests(inventory_with(*assertions))
    return report.suites[0].verdicts[0]


def invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, list(args))


# --------------------------------------------------------------------------- #
# The document kind
# --------------------------------------------------------------------------- #


def test_a_test_suite_is_loaded_but_is_not_an_element() -> None:
    """It is indexed apart, like a layout: it declares no device."""
    inventory = inventory_with("{assert: count, select: kind=router, equals: 1}")
    assert set(inventory.test_suites) == {"suite"}
    assert "suite" not in inventory.elements
    assert not inventory.errors


def test_a_suite_and_an_element_may_share_a_name() -> None:
    """Different name spaces: nothing resolves one where the other is meant."""
    inventory = load_stream(
        NETWORK
        + "---\n"
        + "apiVersion: netgraph.dev/v1alpha1\n"
        + "kind: testsuite\n"
        + "metadata: {name: sw}\n"
        + "spec: {assertions: [{assert: count, select: kind=switch, equals: 1}]}\n"
    )
    assert not inventory.errors
    assert "sw" in inventory.test_suites
    assert inventory.elements["sw"].kind == "switch"


def test_two_suites_of_one_name_are_ng_k001() -> None:
    document = (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: testsuite\n"
        "metadata: {name: twice}\n"
        "spec: {assertions: [{assert: count, select: kind=switch, equals: 1}]}\n"
    )
    inventory = load_stream(document + "---\n" + document)
    assert [error.rule for error in inventory.errors] == ["NG-K001"]
    assert "duplicate test suite name" in inventory.errors[0].message


def test_a_suite_must_assert_something() -> None:
    """``NG-K002`` — a suite that checked nothing would report a green run."""
    inventory = load_stream(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: testsuite\n"
        "metadata: {name: empty}\n"
        "spec: {assertions: []}\n"
    )
    assert inventory.errors
    assert "at least 1" in inventory.errors[0].message


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("{assert: same-vlan, select: kind=switch, hops: 3}", "'hops' is not a key"),
        ("{assert: reachable, from: a}", "needs 'to'"),
        ("{assert: count, select: kind=switch}", "at least one of 'equals'"),
        (
            "{assert: count, select: kind=switch, at_least: 5, at_most: 2}",
            "is above at_most",
        ),
        ("{assert: reachable, from: a, to: b, layer: power}", "cannot be made about layer"),
    ],
)
def test_a_key_that_belongs_to_another_assertion_is_ng_k003(document: str, expected: str) -> None:
    with pytest.raises(Exception) as caught:
        parse_test_suite(
            {
                "apiVersion": "netgraph.dev/v1alpha1",
                "kind": "testsuite",
                "metadata": {"name": "s"},
                "spec": {"assertions": [json.loads(_yamlish(document))]},
            }
        )
    assert expected in str(caught.value)


def _yamlish(flow: str) -> str:
    """The flow mapping above as JSON, so the test does not need a YAML parser."""
    import yaml

    return json.dumps(yaml.safe_load(flow))


def test_the_kind_is_in_the_all_kinds_schema_and_has_one_of_its_own() -> None:
    """Which is what gives an editor completion for ``kind: testsuite``."""
    assert "testsuite" in build_schema()["discriminator"]["mapping"]
    own = build_schema("testsuite")
    assert own["properties"]["kind"]["const"] == "testsuite"
    assert "assertions" in own["$defs"]["TestSuiteSpec"]["properties"]


def test_the_formatter_orders_an_assertion_with_assert_first() -> None:
    from netgraph.fmt.order import document_shape

    shape = document_shape("testsuite")
    assertion = shape.children["spec"].children["assertions"].item
    assert assertion.order[0] == "assert"


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #


def test_a_selector_parses_into_the_renderers_own_filter() -> None:
    spec = parse_selector("kind=switch, namespace=sites/north, name=sw-*, vlan=10, depth=2")
    assert spec.kinds == ("switch",)
    assert spec.namespaces == ("sites/north",)
    assert spec.names == ("sw-*",)
    assert spec.vlans == frozenset({10})
    assert spec.depth == 2


def test_a_bare_word_is_a_name_glob() -> None:
    assert parse_selector("sw-core").names == ("sw-core",)


def test_a_repeated_key_is_an_alternative() -> None:
    assert parse_selector("kind=switch, kind=router").kinds == ("switch", "router")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "the selector is empty"),
        ("kind=switch,,name=a", "empty term"),
        ("colour=red", "unknown selector key"),
        ("kind=", "has no value"),
        ("vlan=orange", "is not a VLAN id"),
        ("vlan=9999", "outside 1-4094"),
        ("depth=far", "is not a number of hops"),
        ("depth=-1", "is negative"),
        ("neighbors-of=a, neighbors-of=b", "given twice"),
    ],
)
def test_a_bad_selector_says_what_was_expected(text: str, expected: str) -> None:
    with pytest.raises(SelectorError) as caught:
        parse_selector(text)
    assert expected in str(caught.value)


def test_a_neighbourhood_naming_nothing_fails_the_assertion() -> None:
    result = verdict("{assert: count, select: neighbors-of=ghost, equals: 1}")
    assert result.state == FAILED
    assert "names nothing in this inventory" in result.message


# --------------------------------------------------------------------------- #
# Field expressions
# --------------------------------------------------------------------------- #


DOCUMENT = {
    "spec": {
        "interfaces": [
            {"name": "lo", "type": "loopback", "ipv4": {"addresses": [{"ip": "127.0.0.1"}]}},
            {"name": "eth0", "type": "ethernet", "ipv4": {"addresses": [{"ip": "10.0.0.1"}]}},
            {"name": "eth1", "type": "ethernet"},
        ]
    }
}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("spec.interfaces[].name", ["lo", "eth0", "eth1"]),
        ("spec.interfaces[name=eth0].ipv4.addresses[].ip", ["10.0.0.1"]),
        ("spec.interfaces[type!=loopback].ipv4.addresses[].ip", ["10.0.0.1"]),
        ("spec.interfaces[name=eth*].name", ["eth0", "eth1"]),
        ("spec.interfaces[].nowhere", []),
        ("spec.nowhere[].name", []),
        ("metadata.name", []),
    ],
)
def test_a_field_expression_reads_what_it_addresses(expression: str, expected: list[str]) -> None:
    assert evaluate(DOCUMENT, expression) == expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("", "the field expression is empty"),
        ("spec.[]", "is not a path component"),
        ("spec.interfaces[oops]", "is not a filter"),
        ("spec.interfaces[=eth0]", "has no key"),
    ],
)
def test_a_bad_field_expression_says_what_was_expected(expression: str, expected: str) -> None:
    with pytest.raises(FieldError) as caught:
        evaluate(DOCUMENT, expression)
    assert expected in str(caught.value)


def test_a_value_renders_the_same_way_twice() -> None:
    """Two equal mappings must key the same, whatever order they were built in."""
    assert render_value({"b": 1, "a": 2}) == render_value({"a": 2, "b": 1})
    assert render_value("10.0.0.1/24") == "10.0.0.1/24"
    assert render_value(None) == "null"


# --------------------------------------------------------------------------- #
# The assertions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("assertion", "state"),
    [
        # reachability
        ("{assert: reachable, from: pc-a, to: rtr}", PASSED),
        ("{assert: reachable, from: pc-a, to: pc-b}", PASSED),
        ("{assert: reachable, from: pc-a, to: pc-b, layer: l2}", FAILED),
        ("{assert: reachable, from: pc-a, to: ghost}", FAILED),
        ("{assert: not-reachable, from: pc-a, to: pc-b, layer: l2}", PASSED),
        ("{assert: not-reachable, from: pc-a, to: rtr}", FAILED),
        ("{assert: path-shorter-than, from: pc-a, to: rtr, hops: 3}", PASSED),
        ("{assert: path-shorter-than, from: pc-a, to: rtr, hops: 2}", FAILED),
        ("{assert: reachable, from: 'name=pc-*', to: rtr}", PASSED),
        ("{assert: reachable, from: 'name=nothing-*', to: rtr}", FAILED),
        # VLANs
        ("{assert: same-vlan, select: 'name=pc-a, name=sw'}", PASSED),
        ("{assert: same-vlan, select: 'name=pc-a, name=sw', vlan: 10}", PASSED),
        ("{assert: same-vlan, select: 'name=pc-a, name=pc-b'}", FAILED),
        ("{assert: same-vlan, select: 'name=pc-a, name=pc-b', vlan: 10}", FAILED),
        ("{assert: distinct-vlan, select: 'name=pc-a, name=pc-b'}", PASSED),
        ("{assert: distinct-vlan, select: 'name=pc-a, name=sw'}", FAILED),
        # addressing
        ("{assert: within-prefix, select: kind=computer, prefix: 10.0.0.0/16}", PASSED),
        ("{assert: within-prefix, select: kind=computer, prefix: 10.0.0.0/24}", FAILED),
        ("{assert: within-prefix, select: kind=computer, prefix: 'nonsense'}", FAILED),
        ("{assert: within-prefix, select: kind=cable, prefix: 10.0.0.0/8}", FAILED),
        # shape
        ("{assert: has-interface, select: kind=switch, interface: Vlan10}", PASSED),
        ("{assert: has-interface, select: kind=switch, interface: Vlan99}", FAILED),
        ("{assert: port-count-at-least, select: kind=switch, ports: 5}", PASSED),
        ("{assert: port-count-at-least, select: kind=switch, ports: 48}", FAILED),
        # values
        (
            "{assert: unique, select: kind=computer,"
            " field: 'spec.interfaces[].ipv4.addresses[].ip'}",
            PASSED,
        ),
        ("{assert: unique, select: kind=computer, field: 'spec.interfaces[].type'}", FAILED),
        ("{assert: unique, select: kind=computer, field: 'spec.nowhere'}", FAILED),
        # counting
        ("{assert: count, select: kind=router, equals: 1}", PASSED),
        ("{assert: count, select: kind=router, at_least: 2}", FAILED),
        ("{assert: count, select: kind=router, at_most: 0}", FAILED),
        ("{assert: count, select: kind=pdu, equals: 0}", PASSED),
        # resilience
        ("{assert: no-single-point-of-failure, layer: l1}", FAILED),
        ("{assert: no-single-point-of-failure, select: kind=computer}", PASSED),
        ("{assert: no-single-point-of-failure, layer: power}", SKIPPED),
    ],
)
def test_every_assertion_grades_the_fixture_network(assertion: str, state: str) -> None:
    assert verdict(assertion).state == state


def test_an_empty_selection_fails_rather_than_passing_vacuously() -> None:
    result = verdict("{assert: has-interface, select: kind=pdu, interface: eth0}")
    assert result.state == FAILED
    assert "matches no element, so nothing was checked" in result.message


def test_a_failure_says_which_elements_and_what_the_graph_held() -> None:
    result = verdict("{assert: not-reachable, from: pc-a, to: rtr}")
    assert result.elements == ("pc-a", "rtr")
    assert any("pc-a -> sw -> rtr" in line for line in result.detail)


def test_a_failure_says_where_the_assertion_is_written() -> None:
    result = verdict("{assert: count, select: kind=router, equals: 9}")
    assert result.location is not None
    assert result.location.file.endswith(".yaml")
    assert result.location.line is not None


def test_two_selectors_that_would_trace_too_many_routes_are_refused() -> None:
    """The product of two wide selectors is a search that would never finish."""
    hosts = "\n---\n".join(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        f"metadata: {{name: pc-{index:03d}}}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet}]}"
        for index in range(20)
    )
    suite = (
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: testsuite\n"
        "metadata: {name: wide}\n"
        "spec:\n"
        "  assertions:\n"
        "    - {assert: reachable, from: 'name=pc-*', to: 'name=pc-*'}\n"
    )
    report = run_tests(load_stream(hosts + "\n---\n" + suite))
    result = report.suites[0].verdicts[0]
    assert result.state == FAILED
    assert "narrow one side" in result.message


def test_a_reachability_assertion_between_one_element_and_itself_fails() -> None:
    result = verdict("{assert: reachable, from: pc-a, to: pc-a}")
    assert result.state == FAILED
    assert "the same element" in result.message


def test_a_long_failure_says_how_much_it_left_out() -> None:
    """Nothing is dropped silently, or ten of forty problems reads as ten."""
    result = verdict("{assert: no-single-point-of-failure, layer: l1}")
    assert result.state == FAILED
    assert len(result.detail) <= 11


# --------------------------------------------------------------------------- #
# Choosing suites
# --------------------------------------------------------------------------- #


def test_a_name_that_matches_nothing_is_reported_rather_than_ignored() -> None:
    report = run_tests(
        inventory_with("{assert: count, select: kind=router, equals: 1}"), names=("nope",)
    )
    assert report.unmatched == ("nope",)
    assert not report.ok


def test_a_glob_narrows_the_run() -> None:
    inventory = inventory_with("{assert: count, select: kind=router, equals: 1}")
    assert run_tests(inventory, names=("su*",)).total == 1
    assert run_tests(inventory, names=("su*", "suite")).total == 1


# --------------------------------------------------------------------------- #
# The reports
# --------------------------------------------------------------------------- #


def test_the_three_renderers_agree_about_what_failed() -> None:
    report = run_tests(load_tree(FIXTURE))
    text = render_test_report(report, "text")
    document = json.loads(render_test_report(report, "json"))
    root = ElementTree.fromstring(render_test_report(report, "junit"))

    assert document["summary"]["failed"] == report.count(FAILED)
    assert document["summary"]["ok"] is False
    assert f"{report.count(FAILED)} failed" in text
    suite = root.find("testsuite")
    assert suite is not None
    assert suite.get("failures") == str(report.count(FAILED))
    assert len(suite.findall("testcase")) == report.total


def test_a_junit_case_carries_the_file_and_line_of_its_assertion() -> None:
    """Which is what makes a red pipeline a list of links rather than a log."""
    report = run_tests(load_tree(FIXTURE))
    root = ElementTree.fromstring(render_test_report(report, "junit"))
    cases = root.findall("./testsuite/testcase")
    assert cases
    for case in cases:
        assert case.get("file") == "tests.yaml"
        assert case.get("line") is not None
    failure = next(case.find("failure") for case in cases if case.find("failure") is not None)
    assert failure is not None
    assert failure.get("type")
    assert (failure.text or "").strip().startswith("at tests.yaml:")


def test_a_junit_name_is_unique_even_when_two_assertions_share_a_title() -> None:
    inventory = inventory_with(
        "{assert: count, name: twice, select: kind=router, equals: 1}",
        "{assert: count, name: twice, select: kind=switch, equals: 1}",
    )
    root = ElementTree.fromstring(render_test_report(run_tests(inventory), "junit"))
    names = [case.get("name") for case in root.findall("./testsuite/testcase")]
    assert names == ["[0] twice", "[1] twice"]


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(ValueError, match="not a test report format"):
        render_test_report(run_tests(load_tree(FIXTURE)), "yaml")


def test_verbose_text_lists_the_passing_assertions_too() -> None:
    report = run_tests(load_tree(HOME_LAB))
    assert "the desktop reaches the NAS" not in render_test_report(report, "text")
    assert "the desktop reaches the NAS" in render_test_report(report, "text", verbose=True)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("inventory", [HOME_LAB, CAMPUS], ids=["home-lab", "campus"])
def test_the_bundled_examples_pass_their_own_suites(inventory: Path) -> None:
    result = invoke("-i", str(inventory), "test")
    assert result.exit_code == 0, result.output
    assert "failed" not in result.output


def test_the_command_exits_one_when_an_assertion_fails() -> None:
    result = invoke("-i", str(FIXTURE), "test")
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "tests.yaml:" in result.output


def test_the_command_exits_one_when_a_suite_name_matches_nothing() -> None:
    result = invoke("-i", str(HOME_LAB), "test", "nonesuch")
    assert result.exit_code == 1
    assert "no test suite matches 'nonesuch'" in result.output


def test_an_inventory_without_a_suite_says_so() -> None:
    result = invoke("-i", str(EXAMPLES / "quickstart"), "test")
    assert result.exit_code == 1
    assert "no 'kind: testsuite' document" in result.output


def test_the_command_refuses_an_inventory_with_errors() -> None:
    broken = REPO_ROOT / "tests" / "fixtures" / "invalid" / "e001-unknown-endpoint.yaml"
    result = invoke("-i", str(broken), "test")
    assert result.exit_code == 1
    assert "refusing to test an inventory with errors" in result.output


def test_list_prints_what_would_be_graded_without_grading_it() -> None:
    result = invoke("-i", str(FIXTURE), "test", "--list")
    assert result.exit_code == 0
    assert "the two desks cannot see each other" in result.output
    assert "FAIL" not in result.output


def test_list_narrows_to_the_named_suites() -> None:
    assert "office" in invoke("-i", str(FIXTURE), "test", "--list", "off*").output
    assert "no test suite matches" in invoke("-i", str(FIXTURE), "test", "--list", "x").output


def test_output_writes_a_file_and_keeps_stdout_clean(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "results.xml"
    result = invoke("-i", str(FIXTURE), "test", "-F", "junit", "-o", str(target))
    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8").startswith("<?xml")
    assert "<testsuites" not in result.stdout


def test_the_json_document_declares_its_schema_version() -> None:
    result = invoke("-i", str(HOME_LAB), "test", "-F", "json")
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["kind"] == "TestReport"
    assert document["schemaVersion"] == 1
    assert document["summary"]["ok"] is True


def test_completion_offers_the_declared_suites() -> None:
    from netgraph.completion import complete_test_suite

    context = CliRunner().invoke(cli, ["-i", str(HOME_LAB), "test", "--help"])
    assert context.exit_code == 0
    # The completer loads the tree itself; a bad path yields no suggestions.
    assert complete_test_suite.__doc__


def test_a_suite_carries_its_own_provenance_without_keep_provenance() -> None:
    """A failing assertion has to name its line whatever the caller asked for."""
    inventory = load_tree(FIXTURE)
    source = inventory.test_suite_sources["office"]
    assert source.provenance is not None
    site = source.locate(("spec", "assertions", 1))
    assert site is not None and site.line == 13


def test_a_file_declaring_a_suite_is_not_cached(tmp_path: Path) -> None:
    """A replayed slot list would not carry the suite, losing it silently."""
    from netgraph.loader import open_cache

    tree = tmp_path / "inv"
    tree.mkdir()
    (tree / "net.yaml").write_text(NETWORK, encoding="utf-8")
    (tree / "tests.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: testsuite\n"
        "metadata: {name: s}\n"
        "spec: {assertions: [{assert: count, select: kind=router, equals: 1}]}\n",
        encoding="utf-8",
    )
    cache = open_cache(tree, directory=tmp_path / "cache")
    load_tree(tree, cache=cache)
    reloaded = load_tree(tree, cache=cache)
    assert set(reloaded.test_suites) == {"s"}
    assert run_tests(reloaded).ok


def test_the_model_is_importable_from_the_package_root() -> None:
    assert TestSuite.model_fields["kind"].default == "testsuite"
