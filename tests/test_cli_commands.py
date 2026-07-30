"""The behaviour of the four subcommands, as a user experiences them.

The contracts asserted here are the ones a script depends on:

* **Exit codes.** ``validate`` and ``render`` answer "is this inventory usable?"
  with their status, so CI can branch on it without parsing output.
* **Stream discipline.** Data on stdout, commentary on stderr. In particular
  ``render`` must write *nothing* to stdout when it refuses, or a shell
  redirection would silently produce a truncated diagram file.
* **Colour degrades.** Piped output carries no ANSI escapes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from netgraph.cli import cli, main
from netgraph.errors import ConfigurationError, RenderError
from netgraph.render import MERMAID_MAX_EDGES

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path, not a package

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
HOME_LAB = EXAMPLES / "home-lab"
CAMPUS = EXAMPLES / "campus"
OVERLAY = EXAMPLES / "overlay"
INVALID = REPO_ROOT / "tests" / "fixtures" / "invalid"

#: An inventory whose only problem is an error, and one whose only problem is a
#: warning. Both hold a single document, so the output is easy to reason about.
BROKEN = INVALID / "e001-unknown-endpoint.yaml"
WARNING_ONLY = INVALID / "w103-orphan-device.yaml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str) -> Result:
    return runner.invoke(cli, list(args), catch_exceptions=False)


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("example", ["home-lab", "campus"])
def test_a_clean_inventory_validates_with_status_zero(runner: CliRunner, example: str) -> None:
    result = invoke(runner, "-i", str(EXAMPLES / example), "validate")
    assert result.exit_code == 0
    assert "no problems found" in result.output


def test_an_error_fails_the_run_and_is_reported(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(BROKEN), "validate")
    assert result.exit_code == 1
    assert "errors (1):" in result.output
    assert "E001" in result.output
    assert "pc-ghost" in result.output, "the report names what is wrong"


def test_a_warning_alone_does_not_fail_the_run(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(WARNING_ONLY), "validate")
    assert result.exit_code == 0
    assert "warnings (1):" in result.output
    assert "W103" in result.output


def test_strict_promotes_a_warning_into_a_failure(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(WARNING_ONLY), "validate", "--strict")
    assert result.exit_code == 1
    assert "errors (1):" in result.output


def test_disable_silences_a_rule(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(WARNING_ONLY), "validate", "--strict", "--disable", "W103")
    assert result.exit_code == 0
    assert "no problems found" in result.output


def test_disable_rejects_an_unknown_rule(capsys: pytest.CaptureFixture[str]) -> None:
    """A suppression that names no rule is a mistake, not a no-op.

    Driven through ``main`` because the translation from
    :class:`~netgraph.errors.ConfigurationError` to an exit status happens there.
    """
    assert (
        main(["-i", str(HOME_LAB), "validate", "--disable", "E999"]) == ConfigurationError.exit_code
    )
    assert "not a known rule id" in capsys.readouterr().err


def test_a_document_that_does_not_parse_is_an_error(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("this: [is: bad\n  yaml\n")
    result = invoke(runner, "-i", str(tmp_path), "validate")
    assert result.exit_code == 1
    assert "errors (1):" in result.output


def test_findings_are_grouped_with_errors_before_warnings(
    runner: CliRunner, tmp_path: Path
) -> None:
    (tmp_path / "broken.yaml").write_text("this: [is: bad\n  yaml\n")
    (tmp_path / "orphan.yaml").write_text(WARNING_ONLY.read_text())
    result = invoke(runner, "-i", str(tmp_path), "validate")

    assert result.exit_code == 1
    assert result.output.index("errors (") < result.output.index("warnings (")
    assert "1 error, 1 warning" in result.output


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def test_render_writes_dot_to_stdout_by_default(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "render")
    assert result.exit_code == 0
    assert result.stdout.startswith("graph netgraph {")


@pytest.mark.parametrize(
    ("output_format", "marker"),
    [("dot", "graph netgraph {"), ("mermaid", "flowchart TB"), ("json", '"kind": "NetworkGraph"')],
)
def test_each_text_format_reaches_stdout(
    runner: CliRunner, output_format: str, marker: str
) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "render", "-f", output_format)
    assert result.exit_code == 0
    assert marker in result.stdout


def test_render_writes_to_the_named_file_and_leaves_stdout_clean(
    runner: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "nested" / "graph.dot"
    result = invoke(runner, "-i", str(HOME_LAB), "render", "-o", str(target))

    assert result.exit_code == 0
    assert target.read_text().startswith("graph netgraph {")
    assert result.stdout == "", "the diagram went to the file, not to stdout"


def test_icons_draws_the_named_theme(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "render", "--icons", "cisco")
    assert result.exit_code == 0
    assert "imagepath=" in result.stdout
    assert '<IMG SCALE="TRUE" SRC="router.png"/>' in result.stdout


def test_icons_none_draws_no_icons(runner: CliRunner) -> None:
    """The off switch has to exist for a wrapper that always passes --icons."""
    result = invoke(runner, "-i", str(HOME_LAB), "render", "--icons", "none")
    assert result.exit_code == 0
    assert "imagepath=" not in result.stdout


def test_an_unknown_icon_theme_is_a_usage_error(runner: CliRunner) -> None:
    """Reported before the inventory is loaded, with the option named."""
    result = invoke(runner, "-i", str(HOME_LAB), "render", "--icons", "ciscoo")
    assert result.exit_code == 2
    assert "'--icons'" in result.output
    assert "cisco" in result.output


def test_a_format_that_cannot_draw_icons_says_so(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "render", "-f", "mermaid", "--icons", "cisco")
    assert result.exit_code == 0
    assert "--icons is ignored for mermaid" in result.stderr
    assert "flowchart TB" in result.stdout


def test_render_refuses_a_broken_inventory_and_writes_no_diagram(runner: CliRunner) -> None:
    """The refusal must leave stdout empty: `netgraph render > f.dot` truncates f."""
    result = invoke(runner, "-i", str(BROKEN), "render")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "refusing to render" in result.stderr
    assert "E001" in result.stderr


def test_force_renders_anyway_and_says_so(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(BROKEN), "render", "--force")
    assert result.exit_code == 0
    assert result.stdout.startswith("graph netgraph {")
    assert "--force" in result.stderr
    # The cable that could not be resolved is named, not silently missing.
    assert "dropped from the graph" in result.stderr


def test_a_warning_does_not_block_a_render(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(WARNING_ONLY), "render")
    assert result.exit_code == 0
    assert result.stdout.startswith("graph netgraph {")
    assert "W103" in result.stderr


def test_strict_makes_a_warning_block_a_render(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(WARNING_ONLY), "render", "--strict")
    assert result.exit_code == 1
    assert result.stdout == ""


def test_diagnostics_never_pollute_the_diagram(runner: CliRunner) -> None:
    """Everything on stdout must be valid JSON, findings included."""
    result = invoke(runner, "-i", str(WARNING_ONLY), "render", "-f", "json")
    assert result.exit_code == 0
    json.loads(result.stdout)
    assert "W103" in result.stderr


@pytest.mark.parametrize(
    ("flag", "value", "expected"),
    [
        ("--kind", "router", {"rtr-north-core-01", "rtr-south-core-01", "rtr-west-core-01"}),
        ("--namespace", "sites/north/hosts", {"pc-north-01", "pc-north-02", "srv-north-01"}),
        (
            "--name",
            "sw-north-acc-*",
            {"sw-north-acc-01", "sw-north-acc-02", "sw-north-acc-03"},
        ),
    ],
)
def test_a_filter_narrows_the_rendered_graph(
    runner: CliRunner, flag: str, value: str, expected: set[str]
) -> None:
    result = invoke(runner, "-i", str(CAMPUS), "render", "-f", "json", flag, value)
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert {node["name"] for node in document["nodes"]} == expected


def test_neighbors_of_with_depth(runner: CliRunner) -> None:
    result = invoke(
        runner,
        "-i",
        str(CAMPUS),
        "render",
        "-f",
        "json",
        "--neighbors-of",
        "sw-north-acc-01",
        "--depth",
        "1",
    )
    assert result.exit_code == 0
    names = {node["name"] for node in json.loads(result.stdout)["nodes"]}
    assert "sw-north-acc-01" in names
    assert "sw-north-dist-01" in names
    assert "rtr-north-core-01" not in names, "the core router is two hops away"


def test_neighbors_of_an_unknown_element_is_a_usage_error(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(CAMPUS), "render", "--neighbors-of", "nope")
    assert result.exit_code == 2
    assert "no element named 'nope'" in result.output


def test_a_vlan_filter_selects_a_broadcast_domain(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(CAMPUS), "render", "-f", "json", "--vlan", "20")
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["nodes"]
    for node in document["nodes"]:
        assert 20 in node["vlans"]


def test_filters_that_select_nothing_warn_rather_than_fail(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(CAMPUS), "render", "-f", "json", "--name", "nothing-*")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["nodes"] == []
    assert "selected no elements" in result.stderr


def test_an_unaddressed_inventory_at_layer_3_blames_the_addressing_not_the_filters(
    runner: CliRunner, tmp_path: Path
) -> None:
    """No filter was given, so "the filters selected no elements" would misdirect."""
    (tmp_path / "sw.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw1}\n"
        "spec:\n"
        "  interfaces: [{name: Gi0/1, type: ethernet, vlan: {mode: access, access_vlan: 10}}]\n",
        encoding="utf-8",
    )
    result = invoke(runner, "-i", str(tmp_path), "render", "--layer", "l3", "-f", "json")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["nodes"] == []
    assert "no element carries a routable address" in result.stderr
    assert "selected no elements" not in result.stderr

    # …and the same for an overlay view of an inventory that declares no tunnel.
    overlay = invoke(runner, "-i", str(tmp_path), "render", "--layer", "overlay", "-f", "json")
    assert overlay.exit_code == 0
    assert json.loads(overlay.stdout)["nodes"] == []
    assert "declares no tunnel" in overlay.stderr


def test_the_overlay_layer_draws_the_encapsulation_stack(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(OVERLAY), "render", "--layer", "overlay", "-f", "json")
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["layer"] == "overlay"

    tunnels = {node["name"]: node["tunnel"] for node in document["nodes"] if "tunnel" in node}
    assert tunnels["vx-100"]["stack"] == ["vxlan", "ipsec"]
    nesting = [edge for edge in document["edges"] if edge["kind"] == "encapsulation"]
    assert {edge["endpoints"][0]["node"] for edge in nesting} == {
        "tunnel:tunnels/vx-100",
        "tunnel:tunnels/gre-mgmt",
    }


def test_display_flags_reach_the_renderer(runner: CliRunner) -> None:
    bare = invoke(runner, "-i", str(HOME_LAB), "render", "--no-show-ips", "--no-show-vlans")
    full = invoke(runner, "-i", str(HOME_LAB), "render")
    assert "192.168.10.20/24" not in bare.stdout
    assert "192.168.10.20/24" in full.stdout
    # Membership, not the word: the campus switches own an interface *named*
    # ``Vlan10`` whose type is ``vlan``, and hiding VLAN annotations is not a
    # licence to rename an interface.
    assert "vlan 10" not in bare.stdout
    assert "vlan 10" in full.stdout


def test_group_by_namespace_and_layer_reach_the_renderer(runner: CliRunner) -> None:
    grouped = invoke(runner, "-i", str(CAMPUS), "render", "--group-by-namespace")
    assert "subgraph cluster_" in grouped.stdout

    logical = invoke(runner, "-i", str(HOME_LAB), "render", "--layer", "l2")
    assert "vlan 10" in logical.stdout


def test_the_interaction_flags_reach_the_renderer(runner: CliRunner) -> None:
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "render",
        "--element-ids",
        "--link-template",
        "https://git.example.invalid/{file}#L{line}",
    )
    assert result.exit_code == 0
    assert 'id="node-switches_sw-home"' in result.stdout
    assert 'URL="https://git.example.invalid/switches/sw-home.yaml#L1"' in result.stdout
    assert "tooltip=" in result.stdout


def test_no_tooltips_strips_the_detail_from_the_output(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "render", "--no-tooltips")
    assert result.exit_code == 0
    assert "tooltip=" not in result.stdout


def test_an_unknown_link_placeholder_is_a_usage_error(runner: CliRunner) -> None:
    """Reported before the inventory is even loaded, with the options listed."""
    result = invoke(runner, "-i", str(HOME_LAB), "render", "--link-template", "https://x/{author}")
    assert result.exit_code == 2
    assert "unknown --link-template placeholder {author}" in result.output
    assert "{file}" in result.output and "{line}" in result.output


def test_watch_takes_the_same_flags(runner: CliRunner) -> None:
    """``render`` and ``watch`` share one option set; a flag on one is on both."""
    help_text = invoke(runner, "watch", "--help").output
    for option in ("--link-template", "--element-ids", "--tooltips"):
        assert option in help_text
    result = invoke(runner, "-i", str(HOME_LAB), "watch", "--link-template", "{unknown}")
    assert result.exit_code == 2
    assert "unknown --link-template placeholder {unknown}" in result.output


@requires_dot
def test_a_format_that_cannot_carry_the_attributes_says_so(
    runner: CliRunner, tmp_path: Path
) -> None:
    output = tmp_path / "out.png"
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "render",
        "-f",
        "png",
        "-o",
        str(output),
        "--element-ids",
        "--link-template",
        "https://x/{file}",
    )
    assert result.exit_code == 0
    assert "--link-template, --element-ids are ignored for png output" in result.stderr
    assert "dot, svg" in result.stderr


@requires_dot
def test_a_raster_render_that_asked_for_nothing_extra_is_quiet(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Warning on every PNG would train the reader to ignore the warnings."""
    output = tmp_path / "out.png"
    result = invoke(runner, "-i", str(HOME_LAB), "render", "-f", "png", "-o", str(output))
    assert result.exit_code == 0
    assert "ignored for png" not in result.stderr


def test_an_svg_render_is_never_warned_at(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "render",
        "-f",
        "dot",
        "--element-ids",
        "--tooltips",
        "--link-template",
        "https://x/{file}",
    )
    assert "ignored" not in result.stderr


def test_layer_3_draws_the_subnets_and_leaves_the_cables_out(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "render", "--layer", "l3")
    assert result.exit_code == 0
    assert '"subnet:192.168.10.0/24" [shape=box' in result.stdout
    assert "<B>192.168.10.0/24</B>" in result.stdout
    assert "[ipv4 subnet]" in result.stdout
    # Cable labels belong to layer 1; at layer 3 nothing is drawn from a cable.
    assert "cbl-" not in result.stdout
    # Every edge lands on a prefix: no element is joined directly to another.
    assert result.stdout.count(" -- ") == result.stdout.count(' -- "subnet:')


def test_layer_3_reports_the_node_count_it_actually_drew(runner: CliRunner) -> None:
    """The count on stderr must include the derived subnets, not just the elements."""
    result = invoke(runner, "-i", str(HOME_LAB), "render", "--layer", "l3", "-f", "json")
    document = json.loads(result.stdout)
    assert f"rendered {len(document['nodes'])} node(s)" in result.stderr
    assert len([node for node in document["nodes"] if node["type"] == "subnet"]) == 5


def test_a_filter_at_layer_3_keeps_the_prefixes_of_what_it_kept(runner: CliRunner) -> None:
    result = invoke(
        runner, "-i", str(CAMPUS), "render", "--layer", "l3", "--kind", "router", "-f", "json"
    )
    document = json.loads(result.stdout)
    assert {node["kind"] for node in document["nodes"]} == {"router", "subnet"}
    routers = {node["id"] for node in document["nodes"] if node["kind"] == "router"}
    for node in document["nodes"]:
        if node["kind"] == "subnet":
            assert node["subnet"]["elements"], "an empty prefix must not be drawn"
            assert set(node["subnet"]["elements"]) <= routers


def test_an_unknown_format_is_a_usage_error(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "render", "-f", "ps")
    assert result.exit_code == 2


@pytest.mark.parametrize("output_format", ["png", "pdf"])
def test_binary_output_is_never_dumped_on_a_terminal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    """Writing a PNG to a TTY garbles the terminal and helps nobody."""
    monkeypatch.setattr("netgraph.cli._is_a_terminal", lambda stream: True)
    status = main(["-i", str(HOME_LAB), "render", "-f", output_format])

    assert status == RenderError.exit_code
    assert "--output" in capsys.readouterr().err


def test_a_directory_destination_is_rejected_before_any_work(
    runner: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "taken"
    target.mkdir()
    result = invoke(runner, "-i", str(HOME_LAB), "render", "-o", str(target))
    assert result.exit_code == 2
    assert "is a directory" in result.output


def test_an_unwritable_destination_is_reported(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The parent cannot be created because a regular file sits in its path."""
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    target = blocker / "sub" / "graph.dot"

    assert main(["-i", str(HOME_LAB), "render", "-o", str(target)]) == RenderError.exit_code
    assert "cannot write" in capsys.readouterr().err


@requires_dot
@pytest.mark.parametrize(
    ("output_format", "magic"),
    [("svg", b"<svg"), ("png", b"\x89PNG"), ("pdf", b"%PDF")],
)
def test_an_image_format_reaches_the_file(
    runner: CliRunner, tmp_path: Path, output_format: str, magic: bytes
) -> None:
    target = tmp_path / f"graph.{output_format}"
    result = invoke(runner, "-i", str(HOME_LAB), "render", "-f", output_format, "-o", str(target))
    assert result.exit_code == 0
    assert magic in target.read_bytes()[:1024]


def star_inventory(path: Path, edges: int) -> Path:
    """A switch with ``edges`` access ports, one computer cabled to each.

    One cable is one Mermaid edge, so the graph has exactly ``edges`` of them.
    Written as a single multi-document file: the point of the fixture is the
    edge count, and a thousand separate files would only make it slower.
    """
    ports = [f"Gi0/{index}" for index in range(1, edges + 1)]
    documents: list[dict[str, object]] = [
        {
            "apiVersion": "netgraph.dev/v1alpha1",
            "kind": "switch",
            "metadata": {"name": "sw1"},
            "spec": {
                "interfaces": [
                    {
                        "name": port,
                        "type": "ethernet",
                        "vlan": {"mode": "access", "access_vlan": 10},
                    }
                    for port in ports
                ]
            },
        }
    ]
    for index, port in enumerate(ports, start=1):
        documents.append(
            {
                "apiVersion": "netgraph.dev/v1alpha1",
                "kind": "computer",
                "metadata": {"name": f"pc{index}"},
                "spec": {
                    "interfaces": [
                        {
                            "name": "eth0",
                            "type": "ethernet",
                            "ipv4": {"addresses": [f"10.0.{index // 250}.{index % 250 + 1}/16"]},
                        }
                    ]
                },
            }
        )
        documents.append(
            {
                "apiVersion": "netgraph.dev/v1alpha1",
                "kind": "cable",
                "metadata": {"name": f"c{index}"},
                "spec": {"medium": "copper", "endpoints": [f"sw1:{port}", f"pc{index}:eth0"]},
            }
        )

    target = path / "star.yaml"
    target.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    return target


def test_a_mermaid_render_at_the_edge_limit_does_not_warn(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The limit is inclusive: Mermaid draws 500 edges and refuses 501."""
    inventory = star_inventory(tmp_path, MERMAID_MAX_EDGES)
    result = invoke(runner, "-i", str(inventory), "render", "-f", "mermaid")
    assert result.exit_code == 0
    assert f"{MERMAID_MAX_EDGES} edge(s)" in result.stderr
    assert "Mermaid" not in result.stderr


def test_a_mermaid_render_over_the_edge_limit_warns(runner: CliRunner, tmp_path: Path) -> None:
    """Entry 4 of ``docs/follow-ups.md``: the output is correct, the viewer refuses it."""
    inventory = star_inventory(tmp_path, MERMAID_MAX_EDGES + 1)
    result = invoke(runner, "-i", str(inventory), "render", "-f", "mermaid")

    assert result.exit_code == 0
    assert f"has {MERMAID_MAX_EDGES + 1} edges" in result.stderr
    assert f"limit of {MERMAID_MAX_EDGES}" in result.stderr
    # The three ways out, all of them actionable without leaving the CLI.
    for hint in ("--namespace", "--kind", "--neighbors-of", "-f dot", "-f svg"):
        assert hint in result.stderr
    # The diagram itself is unchanged: warning on the way out, not censoring.
    edge_lines = [line for line in result.stdout.splitlines() if re.match(r"\s+n\d+ ", line)]
    assert len(edge_lines) == MERMAID_MAX_EDGES + 1


@pytest.mark.parametrize("output_format", ["dot", "json"])
def test_the_edge_limit_belongs_to_mermaid_alone(
    runner: CliRunner, tmp_path: Path, output_format: str
) -> None:
    inventory = star_inventory(tmp_path, MERMAID_MAX_EDGES + 1)
    result = invoke(runner, "-i", str(inventory), "render", "-f", output_format)
    assert result.exit_code == 0
    assert "Mermaid" not in result.stderr


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_list_defaults_to_devices(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "list")
    assert result.exit_code == 0
    assert "NAME" in result.stdout
    assert "switches/sw-home" in result.stdout


@pytest.mark.parametrize("what", ["devices", "cables", "tunnels", "vlans", "subnets"])
def test_every_listing_renders_in_every_format(runner: CliRunner, what: str) -> None:
    # The overlay example is the one that holds all five, so it is what proves
    # every listing has something to print.
    table = invoke(runner, "-i", str(OVERLAY), "list", what)
    assert table.exit_code == 0
    assert table.stdout.strip()

    as_json = invoke(runner, "-i", str(OVERLAY), "list", what, "-F", "json")
    assert as_json.exit_code == 0
    records = json.loads(as_json.stdout)
    assert isinstance(records, list) and records

    as_yaml = invoke(runner, "-i", str(OVERLAY), "list", what, "-F", "yaml")
    assert as_yaml.exit_code == 0
    assert yaml.safe_load(as_yaml.stdout) == records


def test_the_tunnel_listing_shows_the_stack_rather_than_only_the_type() -> None:
    """A reader asking "is this encrypted?" must not have to resolve `over`."""
    runner = CliRunner()
    records = json.loads(invoke(runner, "-i", str(OVERLAY), "list", "tunnels", "-F", "json").stdout)
    by_name = {record["name"]: record for record in records}

    vxlan = by_name["tunnels/vx-100"]
    assert vxlan["stack"] == ["vxlan", "ipsec"]
    assert vxlan["over"] == "tunnels/ipsec-hq-b"
    assert (vxlan["encrypted"], vxlan["protected"]) == (False, True)
    assert (vxlan["vni"], vxlan["layer"], vxlan["port"]) == (100, 2, 4789)

    assert by_name["tunnels/wg-mesh"]["stack"] == ["wireguard"]
    assert len(by_name["tunnels/wg-mesh"]["endpoints"]) == 3

    table = invoke(runner, "-i", str(OVERLAY), "list", "tunnels").stdout
    assert "vxlan over ipsec" in table
    assert "underlay" in table


def test_the_tunnel_listing_still_prints_a_tunnel_it_cannot_resolve(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A reader runs this command *because* something is wrong."""
    (tmp_path / "net.yaml").write_text(
        (INVALID / "e016-unknown-tunnel-endpoint.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = invoke(runner, "-i", str(tmp_path), "list", "tunnels", "-F", "json")
    assert result.exit_code == 0
    (record,) = json.loads(result.stdout)
    assert record["name"] == "tun-ghost"
    # Unresolvable, so it has no stack of its own beyond its declared type.
    assert record["stack"] == ["wireguard"]


def test_the_device_listing_names_every_element_that_becomes_a_node(runner: CliRunner) -> None:
    records = json.loads(
        invoke(runner, "-i", str(HOME_LAB), "list", "devices", "-F", "json").stdout
    )
    assert {record["name"] for record in records} == {
        "hosts/adp-usb-eth",
        "hosts/laptop",
        "hosts/pc-desk",
        "hosts/phone",
        "hosts/srv-nas",
        "routers/rtr-home",
        "switches/sw-home",
        "wireless/ap-home",
    }


def test_the_home_lab_declares_no_tunnel(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "list", "tunnels")
    assert result.exit_code == 0
    assert "no tunnels declared" in result.stdout


def test_the_cable_listing_shows_both_endpoints(runner: CliRunner) -> None:
    records = json.loads(invoke(runner, "-i", str(HOME_LAB), "list", "cables", "-F", "json").stdout)
    assert len(records) == 6
    for record in records:
        assert len(record["endpoints"]) == 2


def test_the_vlan_listing_counts_participants(runner: CliRunner) -> None:
    records = json.loads(invoke(runner, "-i", str(HOME_LAB), "list", "vlans", "-F", "json").stdout)
    assert [record["id"] for record in records] == [10, 20]
    assert records[0]["name"] == "home"
    # Every element of the home lab is in VLAN 10 except the phone — the laptop
    # included, which reaches it through its USB dongle rather than through a
    # VLAN block. The phone is on the air, and its radio declares no VLAN.
    assert len(records[0]["elements"]) == 7
    assert "hosts/laptop" in records[0]["elements"]
    # The guest VLAN reaches the access point and stops at the switch.
    assert records[1]["name"] == "guest"
    assert records[1]["elements"] == ["switches/sw-home", "wireless/ap-home"]


def test_the_subnet_listing_omits_host_scoped_prefixes(runner: CliRunner) -> None:
    """127.0.0.0/8 is on every host and says nothing about the addressing plan."""
    records = json.loads(
        invoke(runner, "-i", str(HOME_LAB), "list", "subnets", "-F", "json").stdout
    )
    subnets = {record["subnet"] for record in records}
    assert "192.168.10.0/24" in subnets
    assert "127.0.0.0/8" not in subnets
    assert "::1/128" not in subnets


def test_an_empty_inventory_lists_nothing_without_failing(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = invoke(runner, "-i", str(tmp_path), "list", "devices")
    assert result.exit_code == 0
    assert "no devices" in result.stdout


def test_a_listing_notes_documents_it_could_not_load(runner: CliRunner, tmp_path: Path) -> None:
    """An unrelated broken file must not hide the answer, nor be hidden itself."""
    (tmp_path / "good.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc}\n"
        "spec:\n"
        "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
    )
    (tmp_path / "broken.yaml").write_text("this: [is: bad\n  yaml\n")
    result = invoke(runner, "-i", str(tmp_path), "list", "devices")

    assert result.exit_code == 0
    assert "pc" in result.stdout
    assert "could not be loaded" in result.stderr


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #


def test_show_prints_the_resolved_document(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "show", "sw-home")
    assert result.exit_code == 0

    document = yaml.safe_load(result.stdout)
    assert document["kind"] == "switch"
    assert document["metadata"]["name"] == "sw-home"
    assert document["apiVersion"] == "netgraph.dev/v1alpha1"


def test_show_materialises_the_defaults(runner: CliRunner) -> None:
    """§6.1.1: a router forwards by default, and the output says so explicitly."""
    document = json.loads(
        invoke(runner, "-i", str(HOME_LAB), "show", "rtr-home", "-F", "json").stdout
    )
    assert document["spec"]["forwarding"] == {"ipv4": True, "ipv6": True}


def test_show_accepts_a_fully_qualified_name(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(CAMPUS), "show", "sites/north/core/rtr-north-core-01")
    assert result.exit_code == 0
    assert yaml.safe_load(result.stdout)["metadata"]["name"] == "rtr-north-core-01"


def test_show_reports_an_unknown_name(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "show", "nope")
    assert result.exit_code == 2
    assert "no element named 'nope'" in result.output


def test_show_refuses_an_ambiguous_short_name(runner: CliRunner, tmp_path: Path) -> None:
    for directory in ("a", "b"):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "twin.yaml").write_text(
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: computer\n"
            "metadata: {name: twin}\n"
            "spec:\n"
            "  interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]\n"
        )
    result = invoke(runner, "-i", str(tmp_path), "show", "twin")

    assert result.exit_code == 2
    assert "ambiguous" in result.output
    assert "a/twin" in result.output and "b/twin" in result.output


def test_show_can_address_a_cable(runner: CliRunner) -> None:
    document = yaml.safe_load(invoke(runner, "-i", str(HOME_LAB), "show", "cbl-rtr-sw").stdout)
    assert document["kind"] == "cable"
    assert len(document["spec"]["endpoints"]) == 2


# --------------------------------------------------------------------------- #
# Global behaviour
# --------------------------------------------------------------------------- #


def test_piped_output_carries_no_ansi_escapes(runner: CliRunner) -> None:
    """CliRunner is not a terminal, so this is the piped case."""
    result = invoke(runner, "-i", str(BROKEN), "validate")
    assert "\x1b[" not in result.output


def test_color_can_be_forced_on(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--color", "-i", str(BROKEN), "validate"], color=True)
    assert "\x1b[" in result.output


def test_no_color_wins_over_the_terminal(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--no-color", "-i", str(BROKEN), "validate"], color=True)
    assert "\x1b[" not in result.output


def test_force_color_styles_output_that_is_not_going_to_a_terminal(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """https://force-color.org, and what CI sets so a log keeps its colours.

    Set through ``monkeypatch`` rather than read from the environment the suite
    happens to run in: ``tests/conftest.py`` clears both of these for the whole
    session precisely so that no other assertion depends on them, which leaves
    the two branches here as the only place either is exercised.
    """
    monkeypatch.setenv("FORCE_COLOR", "1")
    result = invoke(runner, "-i", str(BROKEN), "validate")
    assert "\x1b[" in result.output


def test_no_color_wins_over_force_color(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both set is a contradiction, and no-color.org says which way it resolves."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    result = runner.invoke(cli, ["-i", str(BROKEN), "validate"], color=True)
    assert "\x1b[" not in result.output


def test_a_single_yaml_file_is_a_valid_inventory(runner: CliRunner) -> None:
    """`load_tree` accepts one file, so the CLI must not insist on a directory."""
    result = invoke(runner, "-i", str(HOME_LAB / "switches" / "sw-home.yaml"), "list", "devices")
    assert result.exit_code == 0
    assert "sw-home" in result.stdout


def test_the_rules_command_documents_every_rule(runner: CliRunner) -> None:
    from netgraph.rules import RULE_IDS

    result = invoke(runner, "rules")
    assert result.exit_code == 0
    for rule_id in RULE_IDS:
        assert rule_id in result.stdout
