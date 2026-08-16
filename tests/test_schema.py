"""The JSON Schema, checked against an independent validator.

A schema generated from the models is only worth having if it agrees with them.
``jsonschema`` is that second opinion: it knows nothing about pydantic, so when
it and :func:`~netgraph.models.parse_document` reach the same verdict on a
document, the schema is doing its job.

Five promises are asserted here:

* the emitted schema is itself a valid JSON Schema 2020-12 document;
* every document under ``examples/`` is accepted — the schema must never flag a
  file the loader is happy with, or an editor becomes noise;
* every spelling of every shorthand in §5 is accepted, by ``jsonschema`` *and*
  by the models, because the examples only exercise one spelling of each;
* every deliberately-broken document is rejected, again by both, so the two
  cannot quietly disagree;
* ``schema/netgraph.schema.json`` is what the models produce right now, the
  same drift guard ``docs/schema-reference.md`` has.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

import jsonschema
import pytest
from click.testing import CliRunner

from netgraph import schema as schema_module
from netgraph.cli import cli
from netgraph.errors import SchemaError
from netgraph.loader.documents import read_documents
from netgraph.models import DOCUMENT_KINDS, KINDS, element_model_for, fielddocs, parse_document
from netgraph.schema import SCHEMA_DIALECT, UnknownKindError, build_schema, schema_id

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
GENERATOR = REPO_ROOT / "tools" / "gen_json_schema.py"
COMMITTED = REPO_ROOT / "schema" / "netgraph.schema.json"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def validator() -> jsonschema.Draft202012Validator:
    """A validator for the all-kinds schema."""
    return jsonschema.Draft202012Validator(build_schema())


@pytest.fixture(scope="module")
def per_kind() -> dict[str, jsonschema.Draft202012Validator]:
    return {kind: jsonschema.Draft202012Validator(build_schema(kind)) for kind in DOCUMENT_KINDS}


def example_documents() -> list[tuple[str, dict[str, Any]]]:
    """Every YAML document under ``examples/``, read the way the loader reads it."""
    documents: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(EXAMPLES.rglob("*.yaml")):
        relative = PurePosixPath(path.relative_to(REPO_ROOT).as_posix())
        for index, raw in enumerate(read_documents(path, relative=relative)):
            documents.append((f"{path.relative_to(REPO_ROOT)}#{index}", raw.data))
    return documents


EXAMPLE_DOCUMENTS = example_documents()


def load_generator() -> ModuleType:
    """Import ``tools/gen_json_schema.py`` as a module."""
    name = "gen_json_schema"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# The schema is a schema
# --------------------------------------------------------------------------- #


def test_the_examples_are_not_empty() -> None:
    """A silent zero here would make every parametrised test below vacuous."""
    assert len(EXAMPLE_DOCUMENTS) > 20


def test_the_all_kinds_schema_is_valid_json_schema() -> None:
    schema = build_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == SCHEMA_DIALECT
    assert schema["$id"] == schema_id()


@pytest.mark.parametrize("kind", KINDS)
def test_each_single_kind_schema_is_valid_json_schema(kind: str) -> None:
    schema = build_schema(kind)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"] == schema_id(kind)
    assert schema["properties"]["kind"]["const"] == kind


def test_the_schema_is_json_serialisable() -> None:
    """It is written to a file and served over HTTP; it must round-trip."""
    assert json.loads(json.dumps(build_schema())) == build_schema()


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(UnknownKindError, match="loadbalancer"):
        build_schema("loadbalancer")


def test_kind_is_required_in_every_branch() -> None:
    """Without it a document missing ``kind`` matches every branch at once."""
    schema = build_schema()
    for name, pointer in schema["discriminator"]["mapping"].items():
        model = element_model_for(name)
        # ``template``, ``layout`` and ``testsuite`` have no element model
        # behind them, so their branch is found through the pointer rather than
        # by guessing what the definition is called.
        definition_name = model.__name__ if model is not None else pointer.rpartition("/")[2]
        definition = schema["$defs"][definition_name]
        assert "kind" in definition["required"]
        assert "default" not in definition["properties"]["kind"]


def test_field_descriptions_are_carried_over() -> None:
    """The point of the exercise: hover text in the editor."""
    interface = build_schema()["$defs"]["PartialInterface"]["properties"]
    assert interface["mac"]["title"] == "mac"
    assert "EUI-48" in interface["mac"]["description"]
    # The YANG path travels with the description, and Markdown, not RST.
    assert "`…/if:phys-address`" in interface["mac"]["description"]
    assert "``" not in interface["mac"]["description"]


def test_every_definition_is_reachable() -> None:
    """A ``$ref`` pointing at nothing is a schema that silently accepts anything."""
    schema = build_schema()
    payload = json.dumps(schema)
    for name in schema["$defs"]:
        assert f'"#/$defs/{name}"' in payload, f"{name} is defined but never referenced"


# --------------------------------------------------------------------------- #
# Agreement with the models: the examples
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source,document", EXAMPLE_DOCUMENTS, ids=[s for s, _ in EXAMPLE_DOCUMENTS]
)
def test_every_example_document_validates(
    source: str,
    document: dict[str, Any],
    validator: jsonschema.Draft202012Validator,
) -> None:
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    assert not errors, f"{source}: {errors[0].json_path}: {errors[0].message}"


def test_every_example_document_matches_its_own_kind_only(
    per_kind: dict[str, jsonschema.Draft202012Validator],
) -> None:
    """``-k`` narrows: a cable must not validate against the switch schema."""
    for source, document in EXAMPLE_DOCUMENTS:
        kind = document["kind"]
        assert per_kind[kind].is_valid(document), source
        for other in KINDS:
            if other != kind:
                assert not per_kind[other].is_valid(document), f"{source} also matched {other}"


# --------------------------------------------------------------------------- #
# Agreement with the models: the shorthands
# --------------------------------------------------------------------------- #
#
# The examples are written in one house style, so they exercise one spelling of
# each shorthand. A schema that only accepts that spelling would look correct
# here and underline a perfectly good document in someone else's tree. Every
# alternative §5 permits is therefore checked against both validators.


def _document(kind: str, spec: Any, **metadata: Any) -> dict[str, Any]:
    return {
        "apiVersion": "netgraph.dev/v1alpha1",
        "kind": kind,
        "metadata": {"name": "el", **metadata},
        "spec": spec,
    }


def _switch(*interfaces: Any, **spec: Any) -> dict[str, Any]:
    return _document("switch", {"interfaces": list(interfaces) or [_port()], **spec})


def _port(**overrides: Any) -> dict[str, Any]:
    return {"name": "e0", "type": "ethernet", **overrides}


#: Documents that both validators must *accept*. The mirror image of
#: :data:`BROKEN`, and the half that keeps the schema from becoming noise.
ACCEPTED: dict[str, dict[str, Any]] = {
    "a MAC in colon form": _switch(_port(mac="00:1e:8c:00:10:01")),
    "a MAC in dash form": _switch(_port(mac="00-1E-8C-00-10-01")),
    "a MAC in Cisco dotted form": _switch(_port(mac="001e.8c00.1001")),
    "an explicit null for an optional field": _switch(_port(mac=None, mtu=None)),
    "an address as a CIDR string": _switch(_port(ipv4=["10.0.0.1/24"])),
    "an address as a mapping": _switch(
        _port(ipv4={"addresses": [{"ip": "10.0.0.1", "prefix_length": 24}]})
    ),
    "an address with a netmask": _switch(
        _port(ipv4={"addresses": [{"ip": "10.0.0.1", "netmask": "255.255.255.0"}]})
    ),
    "a /0 and a /32": _switch(_port(ipv4=["0.0.0.0/0", "192.0.2.1/32"])),
    "an IPv6 address as a string": _switch(_port(mtu=1500, ipv6=["2001:db8::1/64"])),
    "an IPv4-mapped IPv6 address": _switch(_port(mtu=1500, ipv6=["::ffff:192.0.2.1/128"])),
    "a full IPv4 container": _switch(
        _port(ipv4={"enabled": True, "mtu": 1500, "addresses": ["10.0.0.1/24"]})
    ),
    "a trunk with a list of VLANs": _switch(_port(vlan={"mode": "trunk", "trunk_vlans": [10, 20]})),
    "a trunk with a single VLAN id": _switch(_port(vlan={"mode": "trunk", "trunk_vlans": 10})),
    "a trunk with a range string": _switch(
        _port(vlan={"mode": "trunk", "trunk_vlans": "10,20,100-110"})
    ),
    "a trunk carrying every VLAN": _switch(_port(vlan={"mode": "trunk", "trunk_vlans": "all"})),
    "a trunk carrying none": _switch(_port(vlan={"mode": "trunk", "trunk_vlans": "none"})),
    "a trunk with a native VLAN": _switch(
        _port(vlan={"mode": "trunk", "trunk_vlans": [10], "native_vlan": 1})
    ),
    "an access port without an access_vlan": _switch(_port(vlan={"mode": "access"})),
    "a VLAN sub-interface": _switch(
        _port(),
        _port(name="e0.10", type="vlan", parent="e0", vlan={"mode": "access", "access_vlan": 10}),
    ),
    "a LAG": _switch(
        _port(name="e0"), _port(name="e1"), _port(name="bond0", type="lag", members=["e0", "e1"])
    ),
    "an endpoint as a string": _document(
        "cable", {"endpoints": ["sw:e0", "pc:eno1"], "medium": "copper"}
    ),
    "an endpoint as a mapping": _document(
        "cable",
        {
            "endpoints": [{"device": "sw", "interface": "e0"}, "pc:eno1"],
            "medium": "copper",
        },
    ),
    "a fully-qualified endpoint": _document(
        "cable",
        {"endpoints": ["sites/berlin/rack1/sw1:eth0", "pc:eno1"], "medium": "copper"},
    ),
    "a bit rate with a unit": _document(
        "cable", {"endpoints": ["a:e0", "b:e0"], "medium": "copper", "speed": "1Gbps"}
    ),
    "a bit rate in bit/s": _document(
        "cable", {"endpoints": ["a:e0", "b:e0"], "medium": "copper", "speed": 1000000000}
    ),
    "a fractional bit rate": _document(
        "cable", {"endpoints": ["a:e0", "b:e0"], "medium": "copper", "speed": "2.5Gbps"}
    ),
    "an integer cable length": _document(
        "cable", {"endpoints": ["a:e0", "b:e0"], "medium": "copper", "length_m": 8}
    ),
}


@pytest.mark.parametrize("case", sorted(ACCEPTED), ids=sorted(ACCEPTED))
def test_a_valid_document_is_accepted_by_the_schema(
    case: str, validator: jsonschema.Draft202012Validator
) -> None:
    errors = sorted(
        validator.iter_errors(ACCEPTED[case]), key=lambda error: list(error.absolute_path)
    )
    assert not errors, f"the schema refused {case!r}: {errors[0].json_path}: {errors[0].message}"


@pytest.mark.parametrize("case", sorted(ACCEPTED), ids=sorted(ACCEPTED))
def test_a_valid_document_is_accepted_by_the_models(case: str) -> None:
    """If pydantic refuses one of these, the case is wrong, not the schema."""
    parse_document(ACCEPTED[case])


# --------------------------------------------------------------------------- #
# Agreement with the models: documents that must be refused
# --------------------------------------------------------------------------- #


#: Each case is a document that must be refused twice over: once by the JSON
#: Schema, once by the pydantic models. A case that only one of them rejects is
#: a case where the editor and ``netgraph validate`` would disagree.
BROKEN: dict[str, dict[str, Any]] = {
    "unknown kind": _document("loadbalancer", {"interfaces": [_port()]}),
    "missing kind": {
        "apiVersion": "netgraph.dev/v1alpha1",
        "metadata": {"name": "el"},
        "spec": {"interfaces": [_port()]},
    },
    "unknown apiVersion": {**_switch(), "apiVersion": "netgraph.dev/v2"},
    "unknown top-level key": {**_switch(), "extras": {}},
    "unknown field on an interface": _switch(_port(descrption="typo")),
    "unknown field in metadata": _document("switch", {"interfaces": [_port()]}, tags=["a"]),
    "malformed MAC": _switch(_port(mac="00:1e:8c:00:10")),
    "MAC with mixed separators": _switch(_port(mac="00:1e-8c:00:10:01")),
    "MAC read as a number": _switch(_port(mac=123456)),
    "VLAN id above the range": _document(
        "switch", {"vlans": [{"id": 4095}], "interfaces": [_port()]}
    ),
    "VLAN id below the range": _switch(_port(vlan={"mode": "access", "access_vlan": 0})),
    "VLAN set with a stray separator": _switch(
        _port(vlan={"mode": "trunk", "trunk_vlans": "10;20"})
    ),
    "native_vlan on an access port": _switch(
        _port(vlan={"mode": "access", "native_vlan": 5}),
    ),
    "trunk without trunk_vlans": _switch(_port(vlan={"mode": "trunk"})),
    "access_vlan on a trunk port": _switch(
        _port(vlan={"mode": "trunk", "trunk_vlans": [10], "access_vlan": 5})
    ),
    "members on a plain ethernet port": _switch(_port(members=["e1"])),
    "parent on a plain ethernet port": _switch(_port(parent="e1")),
    "unknown interface type": _switch(_port(type="tokenring")),
    "element name with a leading dash": _document("switch", {"interfaces": [_port()]}, name="-sw"),
    "IPv4 octet out of range": _switch(_port(ipv4=["10.0.0.300/24"])),
    "IPv4 prefix out of range": _switch(_port(ipv4=["10.0.0.1/33"])),
    "IPv4 address without a prefix": _switch(_port(ipv4=["10.0.0.1"])),
    "IPv6 prefix out of range": _switch(_port(ipv6=["2001:db8::1/129"])),
    "both prefix_length and netmask": _switch(
        _port(
            ipv4={
                "addresses": [{"ip": "10.0.0.1", "prefix_length": 24, "netmask": "255.255.255.0"}]
            }
        )
    ),
    "neither prefix_length nor netmask": _switch(_port(ipv4={"addresses": [{"ip": "10.0.0.1"}]})),
    "MTU below the RFC 8344 floor": _switch(_port(mtu=17)),
    "an interface list that is empty": _document("switch", {"interfaces": []}),
    "a bare endpoint reference": _document(
        "cable", {"endpoints": ["sw:e0", "bare"], "medium": "copper"}
    ),
    "an endpoint with two colons": _document(
        "cable", {"endpoints": ["sw:e0", "a:b:c"], "medium": "copper"}
    ),
    "an unparsable bit rate": _document(
        "cable", {"endpoints": ["a:e0", "b:e0"], "medium": "copper", "speed": "1 gigabit"}
    ),
    "a negative cable length": _document(
        "cable", {"endpoints": ["a:e0", "b:e0"], "medium": "copper", "length_m": -1}
    ),
    "an unknown medium": _document(
        "cable", {"endpoints": ["a:e0", "b:e0"], "medium": "string-and-tin-cans"}
    ),
    "a quoted boolean": _switch(_port(enabled="true")),
    "an adapter with no upstream": _document("adapter", {"interfaces": [_port()]}),
}


@pytest.mark.parametrize("case", sorted(BROKEN), ids=sorted(BROKEN))
def test_a_broken_document_is_refused_by_the_schema(
    case: str, validator: jsonschema.Draft202012Validator
) -> None:
    assert not validator.is_valid(BROKEN[case]), f"the schema accepted {case!r}"


@pytest.mark.parametrize("case", sorted(BROKEN), ids=sorted(BROKEN))
def test_a_broken_document_is_refused_by_the_models(case: str) -> None:
    """The other half of the agreement: what the schema refuses, pydantic refuses."""
    with pytest.raises(SchemaError):
        parse_document(BROKEN[case])


# --------------------------------------------------------------------------- #
# The drift guards
# --------------------------------------------------------------------------- #
#
# Every table in ``netgraph.schema`` names models by string. A rename that is
# not followed through would otherwise leave a shorthand unwidened and a schema
# that quietly refuses valid documents, so each table checks itself. These tests
# prove the checks fire.


def test_a_stale_shorthand_override_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(schema_module._SHORTHANDS, "IPv7Config", dict)
    with pytest.raises(RuntimeError, match="IPv7Config"):
        build_schema()


def test_a_stale_scalar_override_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(schema_module._SCALAR_PROPERTIES, ("Interface", "eui64"), {})
    with pytest.raises(RuntimeError, match=r"Interface\.eui64"):
        build_schema()


def test_a_stale_conditional_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(schema_module._CONDITIONALS, "PortChannel", [])
    with pytest.raises(RuntimeError, match="PortChannel"):
        build_schema()


def test_a_single_kind_schema_tolerates_absent_definitions() -> None:
    """A cable schema has no ``Interface``; the guards must only bind on ``--all``."""
    schema = build_schema("cable")
    assert "Interface" not in schema["$defs"]
    assert "InterfaceRef" in schema["$defs"]


def test_an_undocumented_field_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(fielddocs.FIELD_DOCS, ("Metadata", "labels"))
    with pytest.raises(RuntimeError, match=r"Metadata\.labels"):
        build_schema()


def test_a_documented_field_that_no_longer_exists_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(fielddocs.FIELD_DOCS, ("Metadata", "owner"), fielddocs.Doc("gone"))
    with pytest.raises(RuntimeError, match=r"Metadata\.owner"):
        build_schema()


def test_a_required_scalar_property_keeps_its_annotations() -> None:
    """No model currently makes a MAC or a bit rate mandatory, so check directly.

    :func:`_replace_value_schema` has to handle both shapes; only the optional
    one is reachable through :func:`build_schema` today.
    """
    target = {"type": "string", "title": "mac", "description": "kept"}
    schema_module._replace_value_schema(target, {"type": "string", "pattern": "^a$"})
    assert target == {
        "type": "string",
        "pattern": "^a$",
        "title": "mac",
        "description": "kept",
    }


# --------------------------------------------------------------------------- #
# The committed copy
# --------------------------------------------------------------------------- #


def test_the_committed_schema_is_up_to_date() -> None:
    generator = load_generator()
    assert COMMITTED.read_text(encoding="utf-8") == generator.build(), (
        f"{COMMITTED.relative_to(REPO_ROOT)} is out of date; run 'python tools/gen_json_schema.py'"
    )


def test_the_generator_check_mode_agrees() -> None:
    assert load_generator().main(["--check"]) == 0


def test_the_generator_rewrites_the_file(tmp_path: Path) -> None:
    generator = load_generator()
    target = tmp_path / "nested" / "netgraph.schema.json"
    assert generator.main(["--check", "-o", str(target)]) == 1
    assert generator.main(["-o", str(target)]) == 0
    assert generator.main(["--check", "-o", str(target)]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["$id"] == schema_id()


def test_the_generator_can_emit_one_kind(capsys: pytest.CaptureFixture[str]) -> None:
    assert load_generator().main(["--kind", "cable"]) == 0
    assert json.loads(capsys.readouterr().out)["$id"] == schema_id("cable")


def test_the_generator_refuses_to_check_a_single_kind() -> None:
    with pytest.raises(SystemExit):
        load_generator().main(["--check", "--kind", "cable"])


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_command_writes_the_schema_to_stdout() -> None:
    result = CliRunner().invoke(cli, ["schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == build_schema()


def test_the_command_writes_the_same_bytes_as_the_generator() -> None:
    """``netgraph schema > file`` and the tool must produce one artefact."""
    result = CliRunner().invoke(cli, ["schema"])
    assert result.output == load_generator().build()


def test_the_command_emits_one_kind() -> None:
    result = CliRunner().invoke(cli, ["schema", "--kind", "adapter"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == build_schema("adapter")


def test_the_command_rejects_an_unknown_kind() -> None:
    result = CliRunner().invoke(cli, ["schema", "--kind", "loadbalancer"])
    assert result.exit_code == 2
    assert "loadbalancer" in result.output


def test_the_command_rejects_all_together_with_kind() -> None:
    result = CliRunner().invoke(cli, ["schema", "--all", "--kind", "cable"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_all_is_the_default() -> None:
    assert CliRunner().invoke(cli, ["schema", "--all"]).output == (
        CliRunner().invoke(cli, ["schema"]).output
    )


def test_the_command_writes_a_file(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "schema.json"
    result = CliRunner().invoke(cli, ["schema", "-o", str(target)])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert json.loads(target.read_text(encoding="utf-8")) == build_schema()


def test_the_command_needs_no_inventory(tmp_path: Path) -> None:
    """It describes the schema, not a tree; an empty directory must be fine."""
    result = CliRunner().invoke(cli, ["-i", str(tmp_path), "schema"])
    assert result.exit_code == 0, result.output
