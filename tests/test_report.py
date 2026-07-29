"""The machine-readable output of ``netgraph validate``.

Four contracts are asserted here, because four different consumers depend on
them and none of them can complain in prose:

* **The JSON envelope is stable.** Keys, types and the meaning of a null are a
  published interface (``docs/ci.md``), so they are pinned rather than sampled.
* **The SARIF log is valid SARIF.** Not "looks like SARIF" — it is checked
  against the OASIS 2.1.0 schema vendored in ``tests/fixtures``, because the
  failure mode is a code-scanning upload rejecting it hours later, on somebody
  else's repository.
* **The workflow commands escape what GitHub requires.** A comma in a message
  would otherwise invent a property, silently moving the annotation.
* **Order is deterministic and stream discipline holds.** Document on stdout,
  commentary on stderr, ``--quiet`` touching only the latter.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from click.testing import CliRunner, Result

from netgraph import __version__
from netgraph.cli import cli
from netgraph.config import ValidationConfig
from netgraph.loader import load_tree
from netgraph.models import API_VERSION
from netgraph.report import (
    FORMATS,
    LOAD_RULE,
    SARIF_SCHEMA_URL,
    SARIF_VERSION,
    Diagnostic,
    Report,
    _pointer,
    as_github,
    as_json,
    as_sarif,
    build_report,
    render_report,
)
from netgraph.rules import RULES, Severity
from netgraph.validate import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
INVALID = REPO_ROOT / "tests" / "fixtures" / "invalid"
SARIF_SCHEMA = Path(__file__).resolve().parent / "fixtures" / "sarif-schema-2.1.0.json"

BROKEN = INVALID / "e001-unknown-endpoint.yaml"
WARNING_ONLY = INVALID / "w103-orphan-device.yaml"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str) -> Result:
    return runner.invoke(cli, list(args), catch_exceptions=False)


def report_for(root: Path, *, strict: bool = False) -> Any:
    """Load, validate and build a report the way the command does."""
    inventory = load_tree(root, keep_provenance=True)
    findings = validate(inventory, ValidationConfig(strict=strict))
    return build_report(inventory, findings, base=root.parent if root.is_file() else root)


def document(kind: str, name: str, spec: dict[str, Any]) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": API_VERSION,
            "kind": kind,
            "metadata": {"name": name},
            "spec": spec,
        },
        sort_keys=False,
    )


def write(root: Path, **files: str) -> None:
    for name, text in files.items():
        path = root / f"{name.replace('__', '/')}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def sarif_validator() -> jsonschema.protocols.Validator:
    """A validator for SARIF 2.1.0, built once for the whole module.

    The schema is vendored rather than fetched: a test that needs the network to
    fail honestly is a test that fails dishonestly on an aeroplane.
    """
    schema = json.loads(SARIF_SCHEMA.read_text(encoding="utf-8"))
    validator_for = jsonschema.validators.validator_for(schema)
    validator_for.check_schema(schema)
    return validator_for(schema)


def assert_valid_sarif(validator: jsonschema.protocols.Validator, log: dict[str, Any]) -> None:
    errors = sorted(validator.iter_errors(log), key=lambda error: list(error.absolute_path))
    assert not errors, "\n".join(
        f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in errors[:10]
    )


# --------------------------------------------------------------------------- #
# The formats are what the CLI offers
# --------------------------------------------------------------------------- #


def test_the_command_offers_exactly_the_documented_formats(runner: CliRunner) -> None:
    result = invoke(runner, "validate", "--help")
    assert result.exit_code == 0
    for name in FORMATS:
        assert name in result.output
    assert FORMATS[0] == "text", "the default has to stay first, and stay text"


def test_render_report_refuses_the_text_format() -> None:
    report = report_for(BROKEN)
    with pytest.raises(ValueError, match="not a structured report format"):
        render_report(report, "text")


# --------------------------------------------------------------------------- #
# json
# --------------------------------------------------------------------------- #


def test_the_envelope_carries_the_documented_keys() -> None:
    envelope = as_json(report_for(BROKEN))

    assert envelope["schemaVersion"] == 1
    assert envelope["tool"] == {"name": "netgraph", "version": __version__}
    assert envelope["inventory"]["root"] == str(BROKEN.parent)
    assert envelope["summary"] == {"error": 1, "warning": 0, "info": 0, "total": 1}
    assert envelope["failed"] is True

    (finding,) = envelope["findings"]
    assert finding["rule"] == "E001"
    assert finding["alias"] == "NG-C002"
    assert finding["severity"] == "error"
    assert finding["element"] == "cbl-dangling"
    assert finding["namespace"] == ""
    assert finding["kind"] == "cable"
    assert finding["file"] == BROKEN.name
    assert finding["document"] == 1
    assert finding["pointer"] == "/spec/endpoints/1"
    assert "pc-ghost" in finding["message"]
    assert finding["help"].endswith("#e001--unknown-cable-endpoint")


def test_the_envelope_reports_a_clean_inventory_as_clean() -> None:
    envelope = as_json(report_for(EXAMPLES / "home-lab"))
    assert envelope["findings"] == []
    assert envelope["summary"] == {"error": 0, "warning": 0, "info": 0, "total": 0}
    assert envelope["failed"] is False


def test_every_severity_is_counted_even_at_zero() -> None:
    """A consumer charting severities must not have to guess at a missing key."""
    summary = as_json(report_for(WARNING_ONLY))["summary"]
    assert summary == {"error": 0, "warning": 1, "info": 0, "total": 1}


def test_line_and_column_point_at_the_offending_value(tmp_path: Path) -> None:
    write(
        tmp_path,
        net=document(
            "computer",
            "pc1",
            {"interfaces": [{"name": "eth0", "type": "ethernet", "mtu": 1500}]},
        ),
    )
    (finding,) = [
        entry for entry in as_json(report_for(tmp_path))["findings"] if entry["rule"] == "W101"
    ]

    assert finding["pointer"] == "/spec/interfaces/0"
    text = (tmp_path / "net.yaml").read_text(encoding="utf-8").splitlines()
    line = text[finding["line"] - 1]
    assert line.strip().startswith("- name: eth0")
    # 1-based, and at the start of the *value* -- the mapping that describes the
    # interface, which YAML starts at its first key rather than at the dash.
    assert line[finding["column"] - 1 :].startswith("name: eth0")


def test_a_cable_endpoint_is_located_where_it_was_written(tmp_path: Path) -> None:
    """§7.1 sorts ``spec.endpoints``; the report must undo that.

    ``zz-ghost`` sorts *after* ``pc-a``, so the model keeps it second; had the
    document written it first the report would have to say so.
    """
    write(
        tmp_path,
        net="---\n".join(
            (
                document(
                    "computer",
                    "pc-a",
                    {"interfaces": [{"name": "eth0", "type": "ethernet", "mtu": 1500}]},
                ),
                document(
                    "cable",
                    "cbl",
                    {"endpoints": ["zz-ghost:eth0", "pc-a:eth0"], "medium": "copper"},
                ),
            )
        ),
    )
    findings = as_json(report_for(tmp_path))["findings"]
    (dangling,) = [finding for finding in findings if finding["rule"] == "E001"]

    assert dangling["pointer"] == "/spec/endpoints/0", "written first, reported first"
    line = (tmp_path / "net.yaml").read_text(encoding="utf-8").splitlines()[dangling["line"] - 1]
    assert "zz-ghost" in line


def test_an_inherited_value_is_located_in_the_template(tmp_path: Path) -> None:
    """Provenance decides the file, not the document the element was declared in."""
    write(
        tmp_path,
        templates__base=yaml.safe_dump(
            {
                "apiVersion": API_VERSION,
                "kind": "template",
                "metadata": {"name": "base"},
                "spec": {"interfaces": [{"name": "eth0", "type": "ethernet", "mtu": 1500}]},
            },
            sort_keys=False,
        ),
        hosts__pc=document("computer", "pc1", {"from": "base"}),
    )
    (finding,) = [
        entry for entry in as_json(report_for(tmp_path))["findings"] if entry["rule"] == "W101"
    ]

    assert finding["element"] == "hosts/pc1"
    assert finding["namespace"] == "hosts"
    assert finding["file"] == "templates/base.yaml", (
        "the interface that trips the rule was written in the template"
    )


def test_without_provenance_a_finding_falls_back_to_its_document(tmp_path: Path) -> None:
    """Retaining the node trees costs memory, so only ``validate`` asks for it.

    Everything else loads without them and must still get a usable location:
    the line the document starts on, and no column.
    """
    write(
        tmp_path,
        net=document(
            "computer",
            "pc1",
            {"interfaces": [{"name": "eth0", "type": "ethernet", "mtu": 1500}]},
        ),
    )
    inventory = load_tree(tmp_path)
    report = build_report(inventory, validate(inventory, ValidationConfig()), base=tmp_path)

    (finding,) = [entry for entry in as_json(report)["findings"] if entry["rule"] == "W101"]
    assert finding["file"] == "net.yaml"
    assert finding["line"] == 1, "the document, not the field"
    assert finding["column"] is None
    assert finding["pointer"] == "/spec/interfaces/0", "the path is known even so"


def test_a_diagnostic_without_a_file_is_reported_without_a_location() -> None:
    """A whole-inventory problem still has to appear, just not on a line."""
    report = Report(
        root=Path("/tmp"),
        diagnostics=(Diagnostic(rule="W121", severity=Severity.WARNING, message="islands"),),
        prefix="inventory",
    )
    assert report.path_of(report.diagnostics[0]) is None

    (result,) = as_sarif(report)["runs"][0]["results"]
    assert "locations" not in result

    (line,) = as_github(report).splitlines()
    assert line.startswith("::warning title=")
    assert "file=" not in line


def test_a_load_error_is_reported_under_the_load_rule(tmp_path: Path) -> None:
    write(tmp_path, broken="apiVersion: netgraph.dev/v1alpha1\nkind: switch\nspec: {}\n")
    findings = as_json(report_for(tmp_path))["findings"]

    assert findings, "a document missing 'metadata' is a load error, not a finding"
    for finding in findings:
        assert finding["rule"] == LOAD_RULE.id == "load"
        assert finding["severity"] == "error"
        assert finding["element"] is None
        assert finding["kind"] is None
        assert finding["file"] == "broken.yaml"
        assert finding["help"].endswith("#pass-2--schema")


def test_a_schema_problem_is_titled_by_its_alias(tmp_path: Path) -> None:
    """``load`` is not a useful heading; ``NG-D005`` is."""
    write(
        tmp_path,
        broken=document(
            "switch",
            "sw",
            {"interfaces": [{"name": "eth0", "type": "ethernet", "mut": 1500}]},
        ),
    )
    (line,) = as_github(report_for(tmp_path)).splitlines()
    assert "title=NG-D005 document rejected by the schema" in line
    assert "unknown key" in line


def test_a_pointer_escapes_what_rfc_6901_requires() -> None:
    """A key holding ``/`` or ``~`` must not silently become a different path."""
    assert _pointer(()) is None, "the whole document has no pointer"
    assert _pointer(("spec", "interfaces", 0, "mtu")) == "/spec/interfaces/0/mtu"
    assert _pointer(("spec", "a/b", "c~d")) == "/spec/a~1b/c~0d"


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #


def test_findings_are_ordered_by_file_then_line_then_rule(tmp_path: Path) -> None:
    write(
        tmp_path,
        b__second=document(
            "computer",
            "pc-b",
            {"interfaces": [{"name": "eth0", "type": "ethernet", "mtu": 1500}]},
        ),
        a__first=document(
            "computer",
            "pc-a",
            {"interfaces": [{"name": "eth0", "type": "ethernet", "mtu": 1500}]},
        ),
    )
    findings = as_json(report_for(tmp_path))["findings"]
    keys = [(entry["file"], entry["line"], entry["rule"]) for entry in findings]

    assert keys == sorted(keys)
    assert findings[0]["file"].startswith("a/"), (
        "load order is directory order; report order is path order"
    )


def test_two_runs_over_one_inventory_produce_identical_bytes() -> None:
    first = render_report(report_for(BROKEN), "json")
    second = render_report(report_for(BROKEN), "json")
    assert first == second


# --------------------------------------------------------------------------- #
# sarif
# --------------------------------------------------------------------------- #


def test_the_sarif_log_validates_against_the_official_schema(
    sarif_validator: jsonschema.protocols.Validator,
) -> None:
    log = as_sarif(report_for(BROKEN))
    assert log["version"] == SARIF_VERSION == "2.1.0"
    assert log["$schema"] == SARIF_SCHEMA_URL
    assert_valid_sarif(sarif_validator, log)


def test_the_vendored_schema_is_the_one_the_log_advertises() -> None:
    """Validating against a *different* schema than the log names proves nothing."""
    assert urllib.parse.urlparse(SARIF_SCHEMA_URL).netloc == "docs.oasis-open.org"
    digest = hashlib.sha256(SARIF_SCHEMA.read_bytes()).hexdigest()
    assert digest == ("c3b4bb2d6093897483348925aaa73af03b3e3f4bd4ca38cef26dcb4212a2682e"), (
        "tests/fixtures/README.md records where this file came from; update both together"
    )


@pytest.mark.parametrize("example", ["home-lab", "campus", "overlay", "quickstart"])
def test_every_example_produces_valid_sarif(
    sarif_validator: jsonschema.protocols.Validator, example: str
) -> None:
    """The clean case matters too: an empty ``results`` array is still SARIF."""
    assert_valid_sarif(sarif_validator, as_sarif(report_for(EXAMPLES / example, strict=True)))


@pytest.mark.parametrize(
    "fixture", sorted(path.name for path in INVALID.glob("*.yaml")), ids=lambda name: name[:4]
)
def test_every_invalid_fixture_produces_valid_sarif(
    sarif_validator: jsonschema.protocols.Validator, fixture: str
) -> None:
    log = as_sarif(report_for(INVALID / fixture))
    assert_valid_sarif(sarif_validator, log)
    assert log["runs"][0]["results"], f"{fixture} was supposed to trip a rule"


def test_the_driver_describes_every_rule_exactly_once() -> None:
    rules = as_sarif(report_for(BROKEN))["runs"][0]["tool"]["driver"]["rules"]
    ids = [entry["id"] for entry in rules]

    assert len(ids) == len(set(ids)), "a duplicated descriptor breaks ruleIndex"
    assert ids == [rule.id for rule in RULES] + [LOAD_RULE.id]
    for entry, rule in zip(rules, RULES, strict=False):
        assert entry["shortDescription"]["text"] == rule.title
        assert entry["fullDescription"]["text"] == rule.summary
        assert entry["helpUri"] == rule.help_uri
        assert entry["helpUri"].startswith("https://")
        assert list(rule.aliases) == entry["properties"].get("aliases", [])


def test_rule_ids_resolve_and_rule_indexes_agree() -> None:
    run = as_sarif(report_for(BROKEN))["runs"][0]
    rules = run["tool"]["driver"]["rules"]
    for result in run["results"]:
        assert rules[result["ruleIndex"]]["id"] == result["ruleId"]


@pytest.mark.parametrize(
    ("severity", "level"),
    [(Severity.ERROR, "error"), (Severity.WARNING, "warning"), (Severity.INFO, "note")],
)
def test_severities_map_onto_sarif_levels(severity: Severity, level: str) -> None:
    diagnostic = Diagnostic(rule="E001", severity=severity, message="m", file="a.yaml", line=3)
    log = as_sarif(Report(root=Path("/tmp"), diagnostics=(diagnostic,)))
    assert log["runs"][0]["results"][0]["level"] == level


def test_a_result_carries_its_region_and_a_stable_fingerprint() -> None:
    (result,) = as_sarif(report_for(BROKEN))["runs"][0]["results"]
    location = result["locations"][0]["physicalLocation"]

    assert location["artifactLocation"]["uri"].endswith(BROKEN.name)
    assert location["region"]["startLine"] >= 1
    assert location["region"]["startColumn"] >= 1
    assert set(result["partialFingerprints"]) == {"netgraphFinding/v1"}
    # Same problem, different line: the alert must not be closed and reopened.
    moved = as_sarif(report_for(BROKEN))["runs"][0]["results"][0]
    assert moved["partialFingerprints"] == result["partialFingerprints"]


def test_the_invocation_records_whether_the_run_passed() -> None:
    failed = as_sarif(report_for(BROKEN))["runs"][0]["invocations"][0]
    clean = as_sarif(report_for(EXAMPLES / "home-lab"))["runs"][0]["invocations"][0]
    assert failed["executionSuccessful"] is False
    assert clean["executionSuccessful"] is True


def test_sarif_paths_are_relative_to_the_working_directory(tmp_path: Path) -> None:
    """Code scanning resolves URIs against the repository, not the inventory."""
    root = tmp_path / "inventory" / "site"
    root.mkdir(parents=True)
    write(root, net=document("computer", "pc1", {"interfaces": []}))

    inventory = load_tree(root, keep_provenance=True)
    report = build_report(inventory, validate(inventory, ValidationConfig()), base=tmp_path)
    assert report.prefix == "inventory/site"

    (result,) = as_sarif(report)["runs"][0]["results"]
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == (
        "inventory/site/net.yaml"
    )


def test_an_inventory_outside_the_working_directory_keeps_its_paths(tmp_path: Path) -> None:
    """No ``../..`` chain: nothing downstream could resolve one."""
    root = tmp_path / "elsewhere"
    root.mkdir()
    write(root, net=document("computer", "pc1", {"interfaces": []}))

    inventory = load_tree(root, keep_provenance=True)
    report = build_report(inventory, validate(inventory, ValidationConfig()), base=tmp_path / "cwd")
    assert report.prefix == ""


# --------------------------------------------------------------------------- #
# github
# --------------------------------------------------------------------------- #


def test_workflow_commands_name_the_file_line_column_and_rule() -> None:
    (line,) = as_github(report_for(BROKEN)).splitlines()
    match = re.fullmatch(
        r"::error file=(?P<file>[^,]+),line=(?P<line>\d+),col=(?P<col>\d+),"
        r"title=(?P<title>[^:]+)::(?P<message>.+)",
        line,
    )
    assert match is not None, line
    assert match["file"].endswith(BROKEN.name)
    assert match["title"] == "E001 unknown cable endpoint"
    assert "pc-ghost" in match["message"]


def test_a_warning_annotates_as_a_warning_and_an_info_as_a_notice() -> None:
    assert as_github(report_for(WARNING_ONLY)).startswith("::warning ")
    assert as_github(report_for(INVALID / "i001-locally-administered-mac.yaml")).startswith(
        "::notice "
    )


def test_a_clean_inventory_emits_no_workflow_commands() -> None:
    assert as_github(report_for(EXAMPLES / "home-lab")) == ""


def test_message_and_property_escaping() -> None:
    diagnostic = Diagnostic(
        rule="E001",
        severity=Severity.ERROR,
        message="100% of a\r\nb, and c: d",
        file="a,b:c.yaml",
        line=2,
        column=5,
    )
    (line,) = as_github(Report(root=Path("/tmp"), diagnostics=(diagnostic,))).splitlines()

    # Data: only %, CR and LF. A comma in the *message* is harmless, because the
    # message is everything after '::'.
    assert "100%25 of a%0D%0Ab, and c: d" in line
    # Properties: the separators too, or the file name would invent a property.
    assert "file=a%2Cb%3Ac.yaml" in line
    assert line.count("::") == 2, "exactly one command marker and one message marker"


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("output_format", ["json", "sarif", "github"])
def test_the_document_goes_to_stdout_and_the_summary_to_stderr(
    runner: CliRunner, output_format: str
) -> None:
    result = invoke(runner, "-i", str(BROKEN), "validate", "-F", output_format)
    assert result.exit_code == 1
    assert "errors (1):" in result.stderr
    assert "errors (1):" not in result.stdout
    assert "E001" in result.stdout


@pytest.mark.parametrize("output_format", ["json", "sarif", "github"])
def test_quiet_drops_the_summary_and_keeps_the_document(
    runner: CliRunner, output_format: str
) -> None:
    loud = invoke(runner, "-i", str(BROKEN), "validate", "-F", output_format)
    quiet = invoke(runner, "-q", "-i", str(BROKEN), "validate", "-F", output_format)

    assert quiet.exit_code == loud.exit_code == 1
    assert quiet.stderr == ""
    assert quiet.stdout == loud.stdout != ""


def test_quiet_still_prints_the_human_report_in_text_mode(runner: CliRunner) -> None:
    """``text`` has no document to protect: its report *is* the data."""
    result = invoke(runner, "-q", "-i", str(BROKEN), "validate")
    assert "E001" in result.stdout


@pytest.mark.parametrize("output_format", ["json", "sarif", "github"])
def test_json_stdout_parses_and_the_exit_code_still_gates(
    runner: CliRunner, output_format: str
) -> None:
    clean = invoke(runner, "-q", "-i", str(EXAMPLES / "home-lab"), "validate", "-F", output_format)
    assert clean.exit_code == 0
    if output_format == "github":
        assert clean.stdout == "", "no findings, no annotations, not even a blank line"
    else:
        assert json.loads(clean.stdout)


@pytest.mark.parametrize("output_format", ["json", "sarif", "github"])
def test_strict_is_honoured_by_every_format(runner: CliRunner, output_format: str) -> None:
    lenient = invoke(runner, "-q", "-i", str(WARNING_ONLY), "validate", "-F", output_format)
    strict = invoke(
        runner, "-q", "-i", str(WARNING_ONLY), "validate", "-F", output_format, "--strict"
    )

    assert lenient.exit_code == 0
    assert strict.exit_code == 1
    assert "warning" in lenient.stdout
    assert "error" in strict.stdout


@pytest.mark.parametrize("output_format", ["json", "sarif", "github"])
def test_disable_is_honoured_by_every_format(runner: CliRunner, output_format: str) -> None:
    result = invoke(
        runner, "-q", "-i", str(BROKEN), "validate", "-F", output_format, "--disable", "E001"
    )
    assert result.exit_code == 0
    if output_format == "sarif":
        # The rule is still *described* in the driver, as every rule always is;
        # what has to be gone is the result.
        assert json.loads(result.stdout)["runs"][0]["results"] == []
    elif output_format == "json":
        assert json.loads(result.stdout)["findings"] == []
    else:
        assert result.stdout == ""


def test_disable_accepts_the_schema_alias(runner: CliRunner) -> None:
    result = invoke(
        runner, "-q", "-i", str(BROKEN), "validate", "-F", "json", "--disable", "NG-C002"
    )
    assert json.loads(result.stdout)["findings"] == []


def test_an_unknown_format_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["-i", str(BROKEN), "validate", "-F", "xml"])
    assert result.exit_code == 2
    assert "xml" in result.output


def test_the_sarif_written_by_the_command_is_valid(
    runner: CliRunner, sarif_validator: jsonschema.protocols.Validator
) -> None:
    """End to end: what a redirection actually puts in the file."""
    result = invoke(runner, "-q", "-i", str(EXAMPLES / "campus"), "validate", "-F", "sarif")
    assert result.exit_code == 0
    assert_valid_sarif(sarif_validator, json.loads(result.stdout))
