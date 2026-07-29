"""Interface ranges (§6.2.5) and device templates (§6.6).

Both features rewrite a document between parsing and model validation, and both
are therefore invisible to everything downstream — which is exactly what makes
them dangerous to get wrong: a merge that silently drops a field, or an
expansion that renumbers a port, produces a *valid* inventory describing a
different network. So four properties are asserted here, and each one is the
reason a different class of bug cannot survive:

* **Arithmetic.** Expansion order, zero padding, per-index substitution and the
  bounds are pinned value by value. They are the part a user predicts in their
  head while typing.
* **Merge precedence per field kind.** Scalars, nested mappings, the keyed
  ``interfaces`` list and every other list each merge by a different rule, and
  ``docs/schema.md`` §6.6.1 promises exactly which.
* **Provenance.** A field a template got wrong is reported against the
  template's file and line; a field the device overrode against the device.
  Without this the feature makes fifty devices share one wrong diagnostic.
* **Equivalence.** An inventory written with templates renders byte for byte
  like the same inventory written out longhand. That is the whole claim.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.errors import SchemaError
from netgraph.loader import (
    MAX_INTERFACES_PER_DOCUMENT,
    Inventory,
    Provenance,
    RangeError,
    load_stream,
    load_tree,
    parse_range,
    substitute,
)
from netgraph.loader.documents import parse_documents
from netgraph.loader.ranges import expand_interfaces
from netgraph.models import TEMPLATE_SPEC_KEYS, Template, parse_template
from netgraph.render import Layer, RenderOptions, build_graph, render_text
from netgraph.schema import build_schema
from netgraph.validate import validate

API = "netgraph.dev/v1alpha1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def switch(spec: str, *, name: str = "sw1") -> str:
    return f"apiVersion: {API}\nkind: switch\nmetadata: {{name: {name}}}\nspec:\n{spec}"


def template(spec: str, *, name: str = "tpl") -> str:
    return f"apiVersion: {API}\nkind: template\nmetadata: {{name: {name}}}\nspec:\n{spec}"


def names_of(inventory: Inventory, fqn: str) -> list[str]:
    device = inventory.devices[fqn]
    return [interface.name for interface in device.interfaces]


def only_error(inventory: Inventory) -> Any:
    assert len(inventory.errors) == 1, "\n".join(str(e) for e in inventory.errors)
    return inventory.errors[0]


#: A parsed document standing in for provenance where the test does not care
#: which document a field came from.
(STUB_DOCUMENT,) = parse_documents(
    "spec: {}", path=Path("stub.yaml"), relative=PurePosixPath("stub.yaml")
)


# --------------------------------------------------------------------------- #
# Range grammar and arithmetic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("eth[0-3]", ["eth0", "eth1", "eth2", "eth3"]),
        ("GigabitEthernet1/0/[1-4]", [f"GigabitEthernet1/0/{n}" for n in (1, 2, 3, 4)]),
        # Rightmost span fastest: an odometer, not a nesting the other way round.
        (
            "ge-[0-1]/0/[0-2]",
            ["ge-0/0/0", "ge-0/0/1", "ge-0/0/2", "ge-1/0/0", "ge-1/0/1", "ge-1/0/2"],
        ),
        # The low bound fixes the width; the high bound may need more digits.
        ("eth[01-03]", ["eth01", "eth02", "eth03"]),
        ("eth[08-11]", ["eth08", "eth09", "eth10", "eth11"]),
        ("eth[01-100]", [f"eth{n:02d}" for n in range(1, 101)]),
        ("eth[0-0]", ["eth0"]),
        # A span may open or close the name.
        ("[1-2]x", ["1x", "2x"]),
        ("x[1-2]", ["x1", "x2"]),
        ("[1-2]", ["1", "2"]),
        ("a[1-2]b[3-4]c", ["a1b3c", "a1b4c", "a2b3c", "a2b4c"]),
    ],
)
def test_a_range_expands_to_exactly_these_names(pattern: str, expected: list[str]) -> None:
    assert [name for name, _ in parse_range(pattern).expand()] == expected


def test_the_count_is_the_product_of_the_spans() -> None:
    assert parse_range("a[1-4]b[1-3]c[0-1]").count == 24


@pytest.mark.parametrize(
    "pattern",
    [
        "eth0",  # no span at all
        "eth[3-1]",  # inverted
        "eth[0-",  # unterminated
        "eth]0-1[",  # brackets the wrong way round
        "eth[a-b]",  # not decimal
        "eth[0-1]]",  # stray closing bracket
        "eth[0-1][2-3][4-5][6-7][8-9]",  # more spans than allowed
    ],
)
def test_a_malformed_range_is_rejected_with_ng_r002(pattern: str) -> None:
    with pytest.raises(RangeError) as raised:
        parse_range(pattern)
    assert raised.value.rule == "NG-R002"


def test_a_range_must_be_a_string() -> None:
    with pytest.raises(RangeError, match="must be a string"):
        parse_range(48)


@pytest.mark.parametrize(
    ("description", "values", "expected"),
    [
        ("port {}", ("7",), "port 7"),
        ("port %d", ("7",), "port 7"),
        ("slot {0} port {1}", ("1", "7"), "slot 1 port 7"),
        ("{} and {0}", ("3",), "3 and 3"),
        ("{{literal}}", ("1",), "{literal}"),
        ("50%% used", ("1",), "50% used"),
        ("50% used", ("1",), "50% used"),
        ("no placeholder", ("1",), "no placeholder"),
        # Padding travels into the description, so it agrees with the name.
        ("port {}", ("07",), "port 07"),
    ],
)
def test_description_substitution(description: str, values: tuple[str, ...], expected: str) -> None:
    assert substitute(description, values) == expected


@pytest.mark.parametrize("description", ["port {1}", "port {x}", "port {", "port }"])
def test_a_bad_placeholder_is_ng_r005(description: str) -> None:
    with pytest.raises(RangeError) as raised:
        substitute(description, ("1",))
    assert raised.value.rule == "NG-R005"


# --------------------------------------------------------------------------- #
# Expansion inside a document
# --------------------------------------------------------------------------- #


def test_an_expanded_entry_lands_where_it_stood(tmp_path: Path) -> None:
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  interfaces:\n"
            "    - {name: mgmt0, type: ethernet}\n"
            "    - {range: 'eth[0-2]', type: ethernet}\n"
            "    - {name: last0, type: ethernet}\n"
        ),
    )
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert names_of(inventory, "sw1") == ["mgmt0", "eth0", "eth1", "eth2", "last0"]


def test_an_expanded_interface_carries_every_other_field(tmp_path: Path) -> None:
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  vlans: [{id: 10}]\n"
            "  interfaces:\n"
            "    - range: 'eth[0-1]'\n"
            "      type: ethernet\n"
            "      description: port {}\n"
            "      enabled: false\n"
            "      mtu: 9000\n"
            "      vlan: {mode: access, access_vlan: 10}\n"
        ),
    )
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    device = inventory.devices["sw1"]
    for index, name in enumerate(("eth0", "eth1")):
        interface = device.interface(name)
        assert interface is not None
        assert interface.description == f"port {index}"
        assert interface.enabled is False
        assert interface.mtu == 9000
        assert interface.vlan is not None and interface.vlan.access_vlan == 10


def test_expanded_interfaces_do_not_share_nested_objects(tmp_path: Path) -> None:
    """Each expansion is a deep copy: mutating one port must not move another."""
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  vlans: [{id: 10}]\n"
            "  interfaces:\n"
            "    - {range: 'eth[0-1]', type: ethernet, vlan: {mode: access, access_vlan: 10}}\n"
        ),
    )
    device = load_tree(tmp_path).devices["sw1"]
    first, second = device.interfaces
    assert first.vlan is not second.vlan


def test_a_range_over_the_bound_is_ng_r003(tmp_path: Path) -> None:
    write(
        tmp_path,
        "sw.yaml",
        switch(
            f"  interfaces:\n    - {{range: 'eth[1-{MAX_INTERFACES_PER_DOCUMENT + 1}]', type: ethernet}}\n"
        ),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-R003"
    assert str(MAX_INTERFACES_PER_DOCUMENT) in error.message
    assert error.field_path == ("spec", "interfaces", 0, "range")


def test_the_bound_is_per_document_not_per_range(tmp_path: Path) -> None:
    """Two ranges that each fit but together do not are still refused."""
    half = MAX_INTERFACES_PER_DOCUMENT // 2
    write(
        tmp_path,
        "sw.yaml",
        switch(
            f"  interfaces:\n"
            f"    - {{range: 'a[1-{half}]', type: ethernet}}\n"
            f"    - {{range: 'b[1-{half + 1}]', type: ethernet}}\n"
        ),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-R003"
    assert error.field_path == ("spec", "interfaces", 1, "range")


def test_a_range_exactly_at_the_bound_is_accepted(tmp_path: Path) -> None:
    write(
        tmp_path,
        "sw.yaml",
        switch(
            f"  interfaces:\n    - {{range: 'eth[1-{MAX_INTERFACES_PER_DOCUMENT}]', type: ethernet}}\n"
        ),
    )
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert len(inventory.devices["sw1"].interfaces) == MAX_INTERFACES_PER_DOCUMENT


def test_name_and_range_together_are_ng_r001(tmp_path: Path) -> None:
    write(
        tmp_path,
        "sw.yaml",
        switch("  interfaces:\n    - {name: eth9, range: 'eth[0-1]', type: ethernet}\n"),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-R001"
    assert "not both" in error.message


def test_a_range_colliding_with_a_name_quotes_both_locations(tmp_path: Path) -> None:
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  interfaces:\n"
            "    - range: 'eth[0-2]'\n"
            "      type: ethernet\n"
            "    - name: eth1\n"
            "      type: ethernet\n"
        ),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-R004"
    assert "'eth1'" in error.message
    # Both sides are named, each with its own line, so the fix is obvious.
    assert "sw.yaml#0:6" in error.message  # the range entry
    assert "sw.yaml#0:8" in error.message  # the explicit name


def test_two_ranges_that_overlap_collide(tmp_path: Path) -> None:
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  interfaces:\n"
            "    - {range: 'eth[0-2]', type: ethernet}\n"
            "    - {range: 'eth[2-4]', type: ethernet}\n"
        ),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-R004"
    assert "'eth2'" in error.message


def test_two_explicit_duplicates_are_still_ng_i001(tmp_path: Path) -> None:
    """The range checker does not take over a rule the model states better."""
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  interfaces:\n"
            "    - {name: eth0, type: ethernet}\n"
            "    - {name: eth0, type: ethernet}\n"
        ),
    )
    assert only_error(load_tree(tmp_path)).rule == "NG-I001"


def test_a_document_without_ranges_is_left_alone(tmp_path: Path) -> None:
    """The hot path: nothing is copied, so nothing can be copied wrongly."""
    write(tmp_path, "sw.yaml", switch("  interfaces:\n    - {name: eth0, type: ethernet}\n"))
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert names_of(inventory, "sw1") == ["eth0"]


def test_a_non_list_interfaces_value_reaches_the_model_untouched(tmp_path: Path) -> None:
    write(tmp_path, "sw.yaml", switch("  interfaces: not-a-list\n"))
    error = only_error(load_tree(tmp_path))

    assert error.field_path == ("spec", "interfaces")
    assert error.rule is None


def test_a_diagnostic_on_an_expanded_port_points_at_the_range(tmp_path: Path) -> None:
    """One entry produced the port, so one line is where the fix goes."""
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  interfaces:\n"
            "    - name: keep0\n"
            "      type: ethernet\n"
            "    - range: 'eth[0-9]'\n"
            "      type: ethernet\n"
            "      mtu: 1000\n"
            "      ipv6: [2001:db8::1/64]\n"
        ),
    )
    errors = load_tree(tmp_path).errors

    assert errors and {error.rule for error in errors} == {"NG-I011"}
    for error in errors:
        # Every one of the ten ports was produced by the entry at index 1, so
        # every diagnostic lands on its 'mtu' -- line 10. Without the redirect
        # they would point at 'keep0', which is index 1's neighbour in the list
        # as written, or at nothing at all once the index runs past its end.
        assert error.line == 10
        assert error.field_path == ("spec", "interfaces", 1, "mtu")


# --------------------------------------------------------------------------- #
# The template document
# --------------------------------------------------------------------------- #


def test_a_template_spec_accepts_every_device_spec_key() -> None:
    assert "interfaces" in TEMPLATE_SPEC_KEYS
    assert "from" in TEMPLATE_SPEC_KEYS
    assert "endpoints" not in TEMPLATE_SPEC_KEYS


def test_a_template_parses_into_its_own_model() -> None:
    parsed = parse_template(
        {"apiVersion": API, "kind": "template", "metadata": {"name": "t"}, "spec": {"vendor": "x"}}
    )
    assert isinstance(parsed, Template)
    assert parsed.name == "t"
    assert str(parsed) == "template/t"


@pytest.mark.parametrize(
    ("document", "path"),
    [
        ({"apiVersion": API, "kind": "template", "metadata": {"name": "t"}, "spec": 3}, ("spec",)),
        (
            {
                "apiVersion": API,
                "kind": "template",
                "metadata": {"name": "t"},
                "spec": {"endpoints": []},
            },
            ("spec", "endpoints"),
        ),
    ],
)
def test_a_malformed_template_spec_is_ng_m005(document: Any, path: tuple[str, ...]) -> None:
    with pytest.raises(SchemaError) as raised:
        parse_template(document)
    (issue,) = raised.value.issues
    assert issue.rule == "NG-M005"
    assert issue.path == path


def test_a_template_is_not_an_element(tmp_path: Path) -> None:
    write(tmp_path, "t.yaml", template("  vendor: Cisco\n"))
    write(tmp_path, "sw.yaml", switch("  from: tpl\n  interfaces: [{name: e0, type: ethernet}]\n"))
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert list(inventory.elements) == ["sw1"]
    assert "tpl" not in inventory
    assert list(build_graph(inventory).nodes) == ["sw1"]


def test_an_unused_template_is_still_checked(tmp_path: Path) -> None:
    """A broken range in a template nobody uses is reported against the template."""
    write(tmp_path, "t.yaml", template("  interfaces: [{range: 'eth[9-1]', type: ethernet}]\n"))
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-R002"
    assert error.relative == "t.yaml"


def test_a_duplicate_template_name_is_ng_m002(tmp_path: Path) -> None:
    write(tmp_path, "a.yaml", template("  vendor: one\n"))
    write(tmp_path, "b.yaml", template("  vendor: two\n"))
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-M002"
    assert "a.yaml" in error.message
    assert error.relative == "b.yaml"


def test_a_template_and_a_switch_may_share_a_name(tmp_path: Path) -> None:
    """The two indexes are separate; no field ever accepts both."""
    write(tmp_path, "a.yaml", template("  vendor: Cisco\n", name="core"))
    write(
        tmp_path,
        "b.yaml",
        switch("  from: core\n  interfaces: [{name: e0, type: ethernet}]\n", name="core"),
    )
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert inventory.devices["core"].spec.vendor == "Cisco"


def test_an_unknown_template_is_ng_m001(tmp_path: Path) -> None:
    write(tmp_path, "sw.yaml", switch("  from: nope\n"))
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-M001"
    assert error.field_path == ("spec", "from")
    assert "declares no 'kind: template' document" in error.message


def test_an_unknown_template_lists_the_known_ones(tmp_path: Path) -> None:
    write(tmp_path, "t.yaml", template("  vendor: Cisco\n", name="c9200l"))
    write(tmp_path, "sw.yaml", switch("  from: c9300l\n"))
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-M001"
    assert "c9200l" in error.message


def test_an_ambiguous_template_reference_is_ng_m001(tmp_path: Path) -> None:
    write(tmp_path, "a/t.yaml", template("  vendor: one\n"))
    write(tmp_path, "b/t.yaml", template("  vendor: two\n"))
    write(tmp_path, "c/sw.yaml", switch("  from: tpl\n"))
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-M001"
    assert "ambiguous" in error.message
    assert "a/tpl" in error.message and "b/tpl" in error.message


def test_a_non_string_from_is_ng_m001(tmp_path: Path) -> None:
    write(tmp_path, "sw.yaml", switch("  from: [a, b]\n"))
    assert only_error(load_tree(tmp_path)).rule == "NG-M001"


def test_from_on_a_cable_is_ng_m006(tmp_path: Path) -> None:
    write(
        tmp_path,
        "c.yaml",
        f"apiVersion: {API}\nkind: cable\nmetadata: {{name: c1}}\n"
        "spec:\n  from: tpl\n  endpoints: [a:e0, b:e0]\n",
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-M006"
    assert error.field_path == ("spec", "from")


def test_template_inheritance_chains(tmp_path: Path) -> None:
    write(tmp_path, "base.yaml", template("  vendor: Cisco\n  model: base\n", name="base"))
    write(tmp_path, "mid.yaml", template("  from: base\n  model: mid\n", name="mid"))
    write(tmp_path, "sw.yaml", switch("  from: mid\n  interfaces: [{name: e0, type: ethernet}]\n"))
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    spec = inventory.devices["sw1"].spec
    assert (spec.vendor, spec.model) == ("Cisco", "mid")


def test_a_template_cycle_is_ng_m003(tmp_path: Path) -> None:
    write(tmp_path, "a.yaml", template("  from: b\n  vendor: a\n", name="a"))
    write(tmp_path, "b.yaml", template("  from: a\n  vendor: b\n", name="b"))
    write(tmp_path, "sw.yaml", switch("  from: a\n"))
    inventory = load_tree(tmp_path)

    rules = [error.rule for error in inventory.errors]
    assert "NG-M003" in rules
    cycle = next(error for error in inventory.errors if error.rule == "NG-M003")
    assert "->" in cycle.message


def test_a_device_naming_an_invalid_template_is_ng_m004(tmp_path: Path) -> None:
    write(tmp_path, "t.yaml", template("  interfaces: [{range: 'eth[9-1]', type: ethernet}]\n"))
    write(tmp_path, "sw.yaml", switch("  from: tpl\n"))
    inventory = load_tree(tmp_path)

    rules = [error.rule for error in inventory.errors]
    # The template is blamed once, for itself; the device only says it cannot
    # use it. Fifty devices would not produce fifty copies of the first error.
    assert rules == ["NG-R002", "NG-M004"]


# --------------------------------------------------------------------------- #
# Merge precedence, per field kind
# --------------------------------------------------------------------------- #

_TEMPLATE = template(
    "  vendor: Cisco\n"
    "  model: C9200L\n"
    "  bridge: {name: br0, type: customer-vlan-bridge, address: '00:00:00:00:00:ff'}\n"
    "  vlans: [{id: 10, name: staff}, {id: 20, name: lab}]\n"
    "  interfaces:\n"
    "    - {name: br0, type: bridge, members: [eth0, eth1]}\n"
    "    - {name: eth0, type: ethernet, mtu: 1500, vlan: {mode: access, access_vlan: 10}}\n"
    "    - {name: eth1, type: ethernet, mtu: 1500, vlan: {mode: access, access_vlan: 10}}\n"
)


def merged(tmp_path: Path, spec: str) -> Inventory:
    write(tmp_path, "t.yaml", _TEMPLATE)
    write(tmp_path, "sw.yaml", switch(spec))
    return load_tree(tmp_path)


def test_a_scalar_the_device_declares_wins(tmp_path: Path) -> None:
    inventory = merged(tmp_path, "  from: tpl\n  model: C9300L\n")

    assert inventory.errors == []
    spec = inventory.devices["sw1"].spec
    assert spec.model == "C9300L"
    assert spec.vendor == "Cisco"  # untouched


def test_a_nested_mapping_merges_key_by_key(tmp_path: Path) -> None:
    inventory = merged(tmp_path, "  from: tpl\n  bridge: {address: '00:00:00:00:00:11'}\n")

    assert inventory.errors == []
    bridge = inventory.devices["sw1"].spec.bridge
    assert bridge is not None
    assert bridge.address == "00:00:00:00:00:11"
    assert bridge.name == "br0"  # inherited
    assert bridge.type.value == "customer-vlan-bridge"  # inherited


def test_a_list_that_is_not_interfaces_is_replaced_wholesale(tmp_path: Path) -> None:
    """§6.6.1 rule 4: 'vlans' is not keyed, so it does not merge element-wise."""
    inventory = merged(tmp_path, "  from: tpl\n  vlans: [{id: 30, name: voice}]\n")

    assert inventory.errors == []
    assert [vlan.id for vlan in inventory.devices["sw1"].spec.vlans] == [30]


def test_interfaces_merge_by_name_keeping_template_order(tmp_path: Path) -> None:
    inventory = merged(
        tmp_path,
        "  from: tpl\n"
        "  interfaces:\n"
        "    - {name: eth1, description: patched}\n"
        "    - {name: eth9, type: ethernet}\n",
    )

    assert inventory.errors == []
    # Template order first, then the device's own entries, in the device's order.
    assert names_of(inventory, "sw1") == ["br0", "eth0", "eth1", "eth9"]
    device = inventory.devices["sw1"]
    patched = device.interface("eth1")
    assert patched is not None
    assert patched.description == "patched"
    assert patched.mtu == 1500  # inherited
    assert patched.type.value == "ethernet"  # inherited: the entry omitted it


def test_a_partial_override_may_omit_type(tmp_path: Path) -> None:
    inventory = merged(tmp_path, "  from: tpl\n  interfaces: [{name: eth0, mtu: 9000}]\n")

    assert inventory.errors == []
    eth0 = inventory.devices["sw1"].interface("eth0")
    assert eth0 is not None and eth0.mtu == 9000 and eth0.type.value == "ethernet"


def test_a_list_inside_an_interface_is_replaced_not_merged(tmp_path: Path) -> None:
    inventory = merged(tmp_path, "  from: tpl\n  interfaces: [{name: br0, members: [eth0]}]\n")

    assert inventory.errors == []
    br0 = inventory.devices["sw1"].interface("br0")
    assert br0 is not None and br0.members == ["eth0"]


def test_a_nested_mapping_inside_an_interface_merges(tmp_path: Path) -> None:
    inventory = merged(
        tmp_path, "  from: tpl\n  interfaces: [{name: eth0, vlan: {access_vlan: 20}}]\n"
    )

    assert inventory.errors == []
    eth0 = inventory.devices["sw1"].interface("eth0")
    assert eth0 is not None and eth0.vlan is not None
    assert eth0.vlan.access_vlan == 20
    assert eth0.vlan.mode.value == "access"  # inherited


def test_a_device_may_inherit_its_whole_interface_list(tmp_path: Path) -> None:
    inventory = merged(tmp_path, "  from: tpl\n")

    assert inventory.errors == []
    assert names_of(inventory, "sw1") == ["br0", "eth0", "eth1"]


def test_metadata_never_merges(tmp_path: Path) -> None:
    write(
        tmp_path,
        "t.yaml",
        f"apiVersion: {API}\nkind: template\n"
        "metadata: {name: tpl, description: the template, labels: {a: b}}\n"
        "spec: {vendor: Cisco}\n",
    )
    write(tmp_path, "sw.yaml", switch("  from: tpl\n  interfaces: [{name: e0, type: ethernet}]\n"))
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    metadata = inventory.devices["sw1"].metadata
    assert metadata.description is None
    assert metadata.labels == {}


def test_from_never_survives_into_the_element(tmp_path: Path) -> None:
    inventory = merged(tmp_path, "  from: tpl\n")
    dumped = inventory.devices["sw1"].model_dump(mode="json", by_alias=True)

    assert "from" not in dumped["spec"]


def test_a_template_range_expands_before_the_merge_keys_on_names(tmp_path: Path) -> None:
    write(
        tmp_path,
        "t.yaml",
        template(
            "  interfaces:\n    - {range: 'eth[0-3]', type: ethernet, description: 'port {}'}\n"
        ),
    )
    write(
        tmp_path,
        "sw.yaml",
        switch("  from: tpl\n  interfaces: [{name: eth2, description: patched}]\n"),
    )
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert names_of(inventory, "sw1") == ["eth0", "eth1", "eth2", "eth3"]
    device = inventory.devices["sw1"]
    assert device.interface("eth2") is not None
    eth2, eth3 = device.interface("eth2"), device.interface("eth3")
    assert eth2 is not None and eth2.description == "patched"
    assert eth3 is not None and eth3.description == "port 3"


def test_a_device_range_may_extend_a_template(tmp_path: Path) -> None:
    write(tmp_path, "t.yaml", template("  interfaces: [{range: 'eth[0-1]', type: ethernet}]\n"))
    write(
        tmp_path,
        "sw.yaml",
        switch("  from: tpl\n  interfaces: [{range: 'ge[0-1]', type: ethernet}]\n"),
    )
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert names_of(inventory, "sw1") == ["eth0", "eth1", "ge0", "ge1"]


def test_a_template_resolves_across_namespaces(tmp_path: Path) -> None:
    write(tmp_path, "templates/t.yaml", template("  vendor: Cisco\n", name="c9200l"))
    write(
        tmp_path,
        "sites/north/access/sw.yaml",
        switch("  from: templates/c9200l\n  interfaces: [{name: e0, type: ethernet}]\n"),
    )
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert inventory.devices["sites/north/access/sw1"].spec.vendor == "Cisco"


def test_a_template_works_in_a_stream(tmp_path: Path) -> None:
    """The web editor loads a stream, not a tree, and must behave identically."""
    inventory = load_stream(
        template("  vendor: Cisco\n")
        + "---\n"
        + switch("  from: tpl\n  interfaces: [{name: e0, type: ethernet}]\n")
    )

    assert inventory.errors == []
    assert inventory.devices["sw1"].spec.vendor == "Cisco"


def test_a_template_declared_after_its_user_still_resolves(tmp_path: Path) -> None:
    """File order is byte order, so the template usually sorts last."""
    write(tmp_path, "a-switch.yaml", switch("  from: tpl\n"))
    write(tmp_path, "z-template.yaml", template("  interfaces: [{name: e0, type: ethernet}]\n"))
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert names_of(inventory, "sw1") == ["e0"]


def test_a_deferred_document_keeps_its_place_in_load_order(tmp_path: Path) -> None:
    """Merging must not reorder the inventory; renderers depend on load order."""
    write(tmp_path, "0.yaml", switch("  interfaces: [{name: e0, type: ethernet}]\n", name="first"))
    write(tmp_path, "1.yaml", switch("  from: tpl\n", name="second"))
    write(tmp_path, "2.yaml", switch("  interfaces: [{name: e0, type: ethernet}]\n", name="third"))
    write(tmp_path, "3.yaml", template("  interfaces: [{name: e0, type: ethernet}]\n"))
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert list(inventory.elements) == ["first", "second", "third"]


# --------------------------------------------------------------------------- #
# Provenance through the merge
# --------------------------------------------------------------------------- #

_BAD_TEMPLATE = (
    f"apiVersion: {API}\n"
    "kind: template\n"
    "metadata: {name: tpl}\n"
    "spec:\n"
    "  interfaces:\n"
    "    - name: eth0\n"
    "      type: ethernet\n"
    "      mtu: 1000\n"
)


def test_a_field_from_the_template_is_reported_against_the_template(tmp_path: Path) -> None:
    write(tmp_path, "a-template.yaml", _BAD_TEMPLATE)
    write(
        tmp_path,
        "b-switch.yaml",
        switch("  from: tpl\n  interfaces: [{name: eth0, ipv6: [2001:db8::1/64]}]\n"),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-I011"
    assert error.relative == "a-template.yaml"
    assert error.line == 8  # the 'mtu: 1000' line of the template
    assert error.field_path == ("spec", "interfaces", 0, "mtu")
    assert "inherited by 'sw1' through 'spec.from: tpl'" in error.message


def test_a_field_the_device_overrode_is_reported_against_the_device(tmp_path: Path) -> None:
    write(tmp_path, "a-template.yaml", _BAD_TEMPLATE)
    write(
        tmp_path,
        "b-switch.yaml",
        switch(
            "  from: tpl\n"
            "  interfaces:\n"
            "    - name: eth0\n"
            "      mtu: 900\n"
            "      ipv6: [2001:db8::1/64]\n"
        ),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-I011"
    assert error.relative == "b-switch.yaml"
    assert error.line == 8  # the device's own 'mtu: 900'
    assert "inherited by" not in error.message


def test_provenance_survives_a_template_chain(tmp_path: Path) -> None:
    write(tmp_path, "a-base.yaml", _BAD_TEMPLATE)
    write(
        tmp_path,
        "b-mid.yaml",
        f"apiVersion: {API}\nkind: template\nmetadata: {{name: mid}}\nspec:\n  from: tpl\n",
    )
    write(
        tmp_path,
        "c-switch.yaml",
        switch("  from: mid\n  interfaces: [{name: eth0, ipv6: [2001:db8::1/64]}]\n"),
    )
    error = only_error(load_tree(tmp_path))

    # Two hops away, and still the file that actually holds the bad value.
    assert (error.relative, error.line) == ("a-base.yaml", 8)


def test_a_reordered_interface_still_points_at_its_own_line(tmp_path: Path) -> None:
    """The device's entry moves to the template's position; the line must not."""
    write(
        tmp_path,
        "a-template.yaml",
        template(
            "  interfaces:\n    - {name: eth0, type: ethernet}\n    - {name: eth1, type: ethernet}\n"
        ),
    )
    write(
        tmp_path,
        "b-switch.yaml",
        switch("  from: tpl\n  interfaces:\n    - name: eth1\n      mtu: 1\n"),
    )
    error = only_error(load_tree(tmp_path))

    assert error.relative == "b-switch.yaml"
    assert error.line == 8  # 'mtu: 1', not the template's eth1
    # The path is the one that reaches the value *in the file being pointed at*:
    # eth1 is the device's first interface, though it merged into position 1.
    assert error.field_path == ("spec", "interfaces", 0, "mtu")


# --------------------------------------------------------------------------- #
# Equivalence with a longhand inventory
# --------------------------------------------------------------------------- #

_LONGHAND_PORTS = "\n".join(
    f"    - name: GigabitEthernet1/0/{n}\n"
    f"      type: ethernet\n"
    f"      description: Access port {n} - staff\n"
    f"      enabled: false\n"
    f"      mtu: 1500\n"
    f"      vlan: {{mode: access, access_vlan: 10}}"
    for n in range(1, 25)
)

_LONGHAND = f"""apiVersion: {API}
kind: switch
metadata:
  name: sw-acc-01
  labels: {{site: hq}}
spec:
  vendor: Cisco
  model: C9200L-24P
  location: HQ / IDF-1
  bridge:
    name: br0
    type: customer-vlan-bridge
    address: '00:00:5e:00:53:ff'
  vlans:
    - {{id: 10, name: staff}}
    - {{id: 99, name: mgmt}}
  interfaces:
    - name: br0
      type: bridge
      description: Switching instance
      members: [{", ".join(f"GigabitEthernet1/0/{n}" for n in range(1, 25))}]
    - name: Vlan99
      type: vlan
      parent: br0
      description: In-band management
      vlan: {{mode: access, access_vlan: 99}}
      ipv4: [10.0.99.11/24]
{_LONGHAND_PORTS}
"""

_WITH_TEMPLATE_TPL = f"""apiVersion: {API}
kind: template
metadata:
  name: c9200l-24p
spec:
  vendor: Cisco
  model: C9200L-24P
  bridge:
    name: br0
    type: customer-vlan-bridge
  vlans:
    - {{id: 10, name: staff}}
    - {{id: 99, name: mgmt}}
  interfaces:
    - name: br0
      type: bridge
      description: Switching instance
      members: [{", ".join(f"GigabitEthernet1/0/{n}" for n in range(1, 25))}]
    - name: Vlan99
      type: vlan
      parent: br0
      description: In-band management
      vlan: {{mode: access, access_vlan: 99}}
    - range: GigabitEthernet1/0/[1-24]
      type: ethernet
      description: Access port {{}} - staff
      enabled: false
      mtu: 1500
      vlan: {{mode: access, access_vlan: 10}}
"""

_WITH_TEMPLATE_SW = f"""apiVersion: {API}
kind: switch
metadata:
  name: sw-acc-01
  labels: {{site: hq}}
spec:
  from: c9200l-24p
  location: HQ / IDF-1
  bridge:
    address: '00:00:5e:00:53:ff'
  interfaces:
    - name: Vlan99
      ipv4: [10.0.99.11/24]
"""


@pytest.fixture
def twin_inventories(tmp_path: Path) -> tuple[Inventory, Inventory]:
    longhand = tmp_path / "longhand"
    templated = tmp_path / "templated"
    write(longhand, "sw.yaml", _LONGHAND)
    write(templated, "sw.yaml", _WITH_TEMPLATE_SW)
    write(templated, "z-template.yaml", _WITH_TEMPLATE_TPL)
    return load_tree(longhand), load_tree(templated)


def test_the_twins_both_load_clean(twin_inventories: tuple[Inventory, Inventory]) -> None:
    for inventory in twin_inventories:
        assert inventory.errors == [], "\n".join(str(e) for e in inventory.errors)
        assert list(inventory.elements) == ["sw-acc-01"]


def test_the_twins_resolve_to_the_same_element(
    twin_inventories: tuple[Inventory, Inventory],
) -> None:
    longhand, templated = twin_inventories
    assert longhand.devices["sw-acc-01"].model_dump(
        mode="json", by_alias=True
    ) == templated.devices["sw-acc-01"].model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize("layer", [Layer.L1, Layer.L2, Layer.L3])
@pytest.mark.parametrize("output_format", ["dot", "mermaid", "json"])
def test_the_twins_render_byte_identically(
    twin_inventories: tuple[Inventory, Inventory], layer: Layer, output_format: str
) -> None:
    longhand, templated = twin_inventories
    options = RenderOptions(title="twins")

    def rendered(inventory: Inventory) -> str:
        return render_text(build_graph(inventory, layer=layer), output_format, options=options)

    assert rendered(longhand) == rendered(templated)


def test_the_twins_validate_identically(
    twin_inventories: tuple[Inventory, Inventory],
) -> None:
    longhand, templated = twin_inventories
    assert [str(f) for f in validate(longhand)] == [str(f) for f in validate(templated)]


# --------------------------------------------------------------------------- #
# The CLI and the JSON Schema
# --------------------------------------------------------------------------- #


def run(args: list[str]) -> Any:
    return CliRunner().invoke(cli, args, catch_exceptions=False)


@pytest.mark.parametrize("flag", ["--raw", "--no-expand"])
def test_show_raw_prints_the_document_as_written(tmp_path: Path, flag: str) -> None:
    write(tmp_path, "sw.yaml", _WITH_TEMPLATE_SW)
    write(tmp_path, "z-template.yaml", _WITH_TEMPLATE_TPL)

    result = run(["-i", str(tmp_path), "show", "sw-acc-01", flag])

    assert result.exit_code == 0
    document = yaml.safe_load(result.output)
    assert document["spec"]["from"] == "c9200l-24p"
    assert [entry["name"] for entry in document["spec"]["interfaces"]] == ["Vlan99"]


def test_show_without_raw_prints_the_merged_document(tmp_path: Path) -> None:
    write(tmp_path, "sw.yaml", _WITH_TEMPLATE_SW)
    write(tmp_path, "z-template.yaml", _WITH_TEMPLATE_TPL)

    result = run(["-i", str(tmp_path), "show", "sw-acc-01", "-F", "json"])

    assert result.exit_code == 0
    document = json.loads(result.output)
    assert "from" not in document["spec"]
    assert len(document["spec"]["interfaces"]) == 26
    assert document["spec"]["vendor"] == "Cisco"


def test_show_raw_of_a_range_keeps_the_range(tmp_path: Path) -> None:
    write(tmp_path, "sw.yaml", switch("  interfaces: [{range: 'eth[0-3]', type: ethernet}]\n"))

    result = run(["-i", str(tmp_path), "show", "sw1", "--raw"])

    assert result.exit_code == 0
    assert yaml.safe_load(result.output)["spec"]["interfaces"][0]["range"] == "eth[0-3]"


def test_the_schema_describes_both_loader_keys() -> None:
    schema = build_schema()
    interface = schema["$defs"]["PartialInterface"]

    assert interface["properties"]["range"]["type"] == "string"
    assert {"oneOf": [{"required": ["name"]}, {"required": ["range"]}]} in interface["allOf"]
    assert "type" not in interface.get("required", [])
    assert schema["$defs"]["Interface"]["required"] == ["type"]

    device_spec = schema["$defs"]["DeviceSpec"]
    assert device_spec["properties"]["from"]["type"] == "string"
    assert "interfaces" not in device_spec.get("required", [])


def test_the_schema_carries_a_template_branch() -> None:
    schema = build_schema()

    assert schema["discriminator"]["mapping"]["template"] == "#/$defs/Template"
    assert {"$ref": "#/$defs/Template"} in schema["oneOf"]
    assert "required" not in schema["$defs"]["TemplateSpec"]


def test_a_single_kind_schema_exists_for_template() -> None:
    schema = build_schema("template")

    assert schema["$id"].endswith("/template.json")
    assert schema["properties"]["kind"]["const"] == "template"
    # The carrier model used to collect the ``$defs`` is gone again, and so is
    # everything only it referenced.
    assert "Switch" not in schema["$defs"]
    assert "DeviceSpec" not in schema["$defs"]
    assert "TemplateSpec" in schema["$defs"]


def test_every_template_schema_definition_is_reachable() -> None:
    schema = build_schema("template")
    payload = json.dumps(schema)
    for name in schema["$defs"]:
        assert f'"#/$defs/{name}"' in payload, f"{name} is defined but never referenced"


def test_the_expander_leaves_a_rangeless_list_identical() -> None:
    entries = [{"name": "eth0", "type": "ethernet"}]
    expansion = expand_interfaces(
        entries, prefix=("spec", "interfaces"), provenance=Provenance(base=STUB_DOCUMENT)
    )

    assert expansion.entries is entries
    assert expansion.expanded is False
    assert expansion.redirects == {}


# --------------------------------------------------------------------------- #
# Corners
# --------------------------------------------------------------------------- #


def test_a_bad_range_description_is_reported_at_load(tmp_path: Path) -> None:
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  interfaces:\n"
            "    - range: 'eth[0-1]'\n"
            "      type: ethernet\n"
            "      description: 'port {4}'\n"
        ),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-R005"
    assert error.field_path == ("spec", "interfaces", 0, "description")


def test_an_entry_with_neither_name_nor_range_survives_expansion(tmp_path: Path) -> None:
    """It is the model's job to say 'name is required', not the expander's."""
    write(
        tmp_path,
        "sw.yaml",
        switch(
            "  interfaces:\n"
            "    - {range: 'eth[0-1]', type: ethernet}\n"
            "    - {type: ethernet}\n"
            "    - a-scalar\n"
        ),
    )
    errors = load_tree(tmp_path).errors

    # Both survive expansion untouched and are rejected by the model. The
    # indices are the ones in the file -- 1 and 2, not the 2 and 3 they occupy
    # after the range ahead of them expanded -- because that is what the reader
    # can find.
    assert [(error.field_path, error.line) for error in errors] == [
        (("spec", "interfaces", 1, "name"), 7),
        (("spec", "interfaces", 2), 8),
    ]


def test_an_interface_without_a_name_is_appended_by_the_merge(tmp_path: Path) -> None:
    write(tmp_path, "t.yaml", template("  interfaces: [{name: e0, type: ethernet}]\n"))
    write(tmp_path, "sw.yaml", switch("  from: tpl\n  interfaces: [{type: ethernet}]\n"))
    errors = load_tree(tmp_path).errors

    # The nameless entry keeps its own place and its own diagnostic, rather than
    # being silently dropped or matched against something.
    assert [error.field_path for error in errors] == [("spec", "interfaces", 0, "name")]


def test_a_device_with_from_and_a_bad_range_reports_the_range(tmp_path: Path) -> None:
    write(tmp_path, "t.yaml", template("  vendor: Cisco\n"))
    write(
        tmp_path,
        "sw.yaml",
        switch("  from: tpl\n  interfaces: [{range: 'e[9-1]', type: ethernet}]\n"),
    )
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-R002"


def test_a_template_whose_parent_is_unknown_is_ng_m001(tmp_path: Path) -> None:
    write(tmp_path, "t.yaml", template("  from: missing\n  vendor: Cisco\n"))
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-M001"
    assert error.relative == "t.yaml"


def test_a_qualified_reference_that_matches_nothing_is_ng_m001(tmp_path: Path) -> None:
    write(tmp_path, "templates/t.yaml", template("  vendor: Cisco\n"))
    write(tmp_path, "sw.yaml", switch("  from: elsewhere/tpl\n"))
    error = only_error(load_tree(tmp_path))

    assert error.rule == "NG-M001"


def test_a_bare_reference_resolves_globally_when_the_name_is_unique(tmp_path: Path) -> None:
    write(tmp_path, "a/t.yaml", template("  vendor: Cisco\n"))
    write(
        tmp_path, "b/sw.yaml", switch("  from: tpl\n  interfaces: [{name: e0, type: ethernet}]\n")
    )
    inventory = load_tree(tmp_path)

    assert inventory.errors == []
    assert inventory.devices["b/sw1"].spec.vendor == "Cisco"


def test_a_template_rejected_by_its_envelope_is_reported_once(tmp_path: Path) -> None:
    write(
        tmp_path,
        "t.yaml",
        f"apiVersion: {API}\nkind: template\nmetadata: {{name: 'not a name'}}\nspec: {{}}\n",
    )
    error = only_error(load_tree(tmp_path))

    assert error.field_path == ("metadata", "name")
    assert error.relative == "t.yaml"
