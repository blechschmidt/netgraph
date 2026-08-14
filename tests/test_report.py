"""``netgraph report`` — the bundle, its links, its determinism and its stamp.

Five properties are asserted here, in this order of importance:

**No broken cross-reference.** A report is a set of files that point at each
other, and a link into a page that does not exist — or at an anchor no page offers
— is the one failure a reader meets immediately and cannot work around. Every
link of every page of every example inventory is resolved against the bundle
itself, in Markdown and in HTML.

**Byte stability.** The pages of an example are committed as goldens, regenerated
with::

    pytest tests/test_report.py --regen-golden

and the browsable report under ``docs/example-report/`` is checked against what
``tools/gen_example_report.py`` would write today. Only the *drawings* are exempt:
an SVG is Graphviz's output and differs between Graphviz releases, so pinning its
bytes would fail on a machine whose Graphviz is a version ahead.

**One derivation.** The tables must be the ones the matching commands print, so
they are compared against :mod:`netgraph.listing`, :mod:`netgraph.ipam` and
:func:`netgraph.export.cables.schedule` rather than against a second expectation
written out here. A per-site page is the same functions over
:func:`netgraph.loader.inventory.subset`, and that narrowing is asserted directly.

**Traceability.** The stamp, the version and the git revision reach every page,
and the stamp can be pinned — which is what makes the byte stability above
possible at all.

**Bounded cost.** A large inventory must produce a report in a time somebody will
wait for, so the generator is run over a generated 100-device tree under a budget.
"""

from __future__ import annotations

import importlib.util
import json
import posixpath
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from click.testing import CliRunner

from netgraph import listing
from netgraph.cli import cli
from netgraph.diagnostics import build_report as build_diagnostics
from netgraph.errors import NetgraphError, RenderError
from netgraph.export import cables as cable_export
from netgraph.fsio import write_text
from netgraph.ipam import build_report as build_ipam_report
from netgraph.loader import Inventory, load_tree, subset
from netgraph.loader.inventory import short_name
from netgraph.render.graph import Layer, build_graph
from netgraph.report import (
    EPOCH_ENV_VAR,
    FORMATS,
    JSON_FILE,
    Bundle,
    Cell,
    Layout,
    Options,
    collect,
    generate,
    git_revision,
    layers_for,
    page_slug,
    resolve_timestamp,
    site_groups,
)
from netgraph.report.stamp import NO_TIMESTAMP

from platform_marks import ON_WINDOWS, requires_dot  # isort: skip -- tests/ is on sys.path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "report"

#: The examples every property below is asserted over. ``home-lab`` is the small
#: one with a wireless AP and an adapter; ``patch-room`` has the panels, racks and
#: PDUs an as-built record is mostly about; ``campus`` is the one with several
#: sites, VRFs and adjacencies; ``overlay`` is the one with tunnels.
EXAMPLE_NAMES = ("home-lab", "patch-room", "campus", "overlay")

#: Pinned in every generated report here, so a golden is a function of the
#: inventory alone. See ``netgraph.report.stamp``.
STAMP = "2026-01-31T09:00:00Z"

#: Pinned for the same reason: the real version changes at every release, and a
#: golden that fails on a version bump teaches nobody anything.
VERSION = "0.0.0-test"

#: ``[text](target)`` in Markdown, and ``![alt](target)``.
_MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
#: Every anchor a Markdown page offers. The generator writes them explicitly.
_MD_ANCHOR = re.compile(r'<a id="([^"]+)"')
#: ``href="target"`` and ``src="target"``, plus the ``xlink:href`` of a drawing.
_HTML_LINK = re.compile(r'(?:xlink:)?(?:href|src)="([^"]+)"')
_HTML_ANCHOR = re.compile(r'\bid="([^"]+)"')


def options_for(export_format: str, **overrides: Any) -> Options:
    """The options every test here generates with: pinned stamp, pinned version."""
    return replace(Options(format=export_format, generated_at=STAMP, version=VERSION), **overrides)


def report_of(name: str, export_format: str = "markdown", **overrides: Any) -> Bundle:
    """The bundle for one example inventory, findings included."""
    inventory = load_tree(EXAMPLES / name)
    from netgraph.validate import validate

    findings = validate(inventory)
    bundle, _ = generate(
        inventory,
        options=options_for(export_format, **overrides),
        diagnostics=build_diagnostics(inventory, findings).diagnostics,
    )
    return bundle


@pytest.fixture(scope="module")
def inventories() -> dict[str, Inventory]:
    return {name: load_tree(EXAMPLES / name) for name in EXAMPLE_NAMES}


def load_tool(name: str) -> ModuleType:
    """Import a script from ``tools/`` as a module."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Cross-references
# --------------------------------------------------------------------------- #


def links_of(path: str, text: str) -> Iterator[tuple[str, str]]:
    """Every ``(file, anchor)`` one page points at, both formats."""
    pattern = _MD_LINK if path.endswith(".md") else _HTML_LINK
    for target in pattern.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "data:")):
            continue
        file_part, _, anchor = target.partition("#")
        yield (file_part, anchor)


def anchors_of(path: str, text: str) -> set[str]:
    pattern = _MD_ANCHOR if path.endswith(".md") else _HTML_ANCHOR
    return set(pattern.findall(text))


def resolve(path: str, target: str) -> str:
    """A page-relative link resolved back to a bundle-relative path."""
    return posixpath.normpath(posixpath.join(posixpath.dirname(path), target))


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
@pytest.mark.parametrize("export_format", ("markdown", "html"))
def test_no_page_references_a_missing_file_or_anchor(name: str, export_format: str) -> None:
    """The property a reader notices first: every internal link resolves."""
    bundle = report_of(name, export_format)
    texts = {path: bundle.text(path) for path in bundle.paths if not path.endswith(".svg")}
    anchors = {path: anchors_of(path, text) for path, text in texts.items()}

    for path, text in texts.items():
        for file_part, anchor in links_of(path, text):
            normalised = resolve(path, file_part) if file_part else path
            assert normalised in bundle.files, (
                f"{path} links to {file_part!r}, which the bundle does not hold"
            )
            if anchor and not normalised.endswith(".svg"):
                assert anchor in anchors[normalised], (
                    f"{path} links to '{file_part}#{anchor}', and {normalised} has no such anchor"
                )


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_every_element_has_a_page_and_the_overview_links_it(name: str) -> None:
    """A device the report does not name is a device the report forgot."""
    inventory = load_tree(EXAMPLES / name)
    bundle = report_of(name)
    overview = bundle.text("README.md")
    for fqn, element in inventory.elements.items():
        if element.kind in {"cable", "tunnel", "user", "group"}:
            # A link has no page because it is drawn on both of the pages it
            # joins; an identity has none because an account is a row, not a
            # paragraph. Both are named in a table instead, which the identity
            # test below asserts.
            continue
        page = f"devices/{page_slug(fqn)}.md"
        assert page in bundle.files, f"{fqn} has no page"
        assert f"({page})" in overview, f"the overview does not link {page}"


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_every_page_carries_the_version_and_the_stamp(name: str) -> None:
    """A printed page with no provenance on it cannot be traced back."""
    bundle = report_of(name, revision="abc123def456", revision_state="clean")
    for path in bundle.paths:
        if path.endswith(".svg"):
            continue
        text = bundle.text(path)
        assert VERSION in text, f"{path} does not name the netgraph version"
        assert STAMP in text, f"{path} does not carry the generated-at stamp"
        assert "abc123def456" in text, f"{path} does not carry the inventory revision"


# --------------------------------------------------------------------------- #
# One derivation
# --------------------------------------------------------------------------- #


def test_the_element_table_is_the_one_netgraph_list_prints(
    inventories: dict[str, Inventory],
) -> None:
    """The overview's element table is ``netgraph list devices``, cell for cell."""
    inventory = inventories["campus"]
    overview = report_of("campus").text("README.md")
    for row in listing.devices(inventory).rows:
        for cell in row:
            assert cell in overview, f"the overview is missing the cell {cell!r}"


def test_the_address_plan_is_the_one_netgraph_ipam_prints(
    inventories: dict[str, Inventory],
) -> None:
    """Every utilisation row reaches the overview with the same numbers."""
    inventory = inventories["campus"]
    overview = report_of("campus").text("README.md")
    for row in listing.utilisation(build_ipam_report(inventory).rows).rows:
        assert row[0] in overview
        assert row[-1] in overview


def test_the_cable_schedule_is_the_one_the_exporter_writes(
    inventories: dict[str, Inventory],
) -> None:
    """Every run of the pull list appears on the site page, in the same order."""
    from netgraph.export.context import ExportContext, ExportOptions
    from netgraph.export.manifest import Recorder

    inventory = inventories["patch-room"]
    context = ExportContext(
        inventory=inventory,
        graphs={layer: build_graph(inventory, layer=layer) for layer in (Layer.PHYSICAL, Layer.L1)},
        options=ExportOptions(),
        recorder=Recorder(),
    )
    rows = cable_export.schedule(context)
    page = report_of("patch-room").text("sites/root.md")
    for row in rows:
        assert short_name(row.cable) in page, f"the run {row.cable} is missing from the schedule"
    # The cable name is unique, so it doubles as a position marker: the rows must
    # appear in the order the exporter sorted them into, which is rack order.
    positions = [page.index(f"| {short_name(row.cable)} |") for row in rows]
    assert positions == sorted(positions), "the schedule is not in the exporter's order"


def test_a_site_page_is_the_same_tables_over_a_narrowed_inventory(
    inventories: dict[str, Inventory],
) -> None:
    """The scoping is ``subset``, not a second filter written into the report."""
    inventory = inventories["campus"]
    report, layout, _ = collect(inventory, options=options_for("markdown"))
    page = report.page_at(layout.site("sites/north"))
    assert page is not None

    north = subset(
        inventory,
        {fqn for fqn in inventory.elements if fqn.startswith("sites/north/")}
        | set(inventory.cables)
        | set(inventory.tunnels),
    )
    expected = listing.devices(north)
    devices = next(
        table for section in page.sections for table in section.tables if table.key == "devices"
    )
    assert [cells[0] for cells in expected.rows] == [row[0].text for row in devices.rows]


def test_a_link_leaving_a_site_is_reported_rather_than_dropped(
    inventories: dict[str, Inventory],
) -> None:
    """The backbone cables of the campus belong to no site and must still be named."""
    bundle = report_of("campus")
    page = bundle.text("sites/sites_north.md")
    assert "cbl-bb-north-south" in page
    assert "Links leaving this page" in page
    # And they are not in the site's own schedule, which covers its own elements.
    schedule = page[page.index("Cable schedule") : page.index("Links leaving this page")]
    assert "cbl-bb-north-south" not in schedule


def test_a_crossing_link_is_reported_once(inventories: dict[str, Inventory]) -> None:
    """A cable is an edge of more than one layer; a row about it is still one row."""
    page = report_of("overlay").text(f"sites/{page_slug('sites')}.md")
    external = page[page.index("Links leaving this page") :]
    for name in inventories["overlay"].tunnels:
        assert external.count(f"| {short_name(name)} |") <= 1, f"{name} is listed twice"


def test_a_link_to_something_the_report_does_not_document_is_left_out() -> None:
    """A tunnel drawn as a hub node joins its own legs; that is not a link leaving."""
    page = report_of("overlay").text(f"sites/{page_slug('sites')}.md")
    external = page[page.index("Links leaving this page") :]
    assert "tunnels/" not in external, "a derived node is being reported as a far end"


def test_a_scoped_report_names_the_links_that_leave_the_scope(tmp_path: Path) -> None:
    """The point of ``full``: a report of one site still shows its uplinks."""
    CliRunner().invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "campus"),
            "report",
            "--namespace",
            "sites/north",
            "--out",
            str(tmp_path),
            "--no-diagrams",
        ],
        catch_exceptions=False,
    )
    page = (tmp_path / "sites" / f"{page_slug('sites/north')}.md").read_text(encoding="utf-8")
    assert "cbl-bb-north-south" in page, "the backbone uplink is not reported as leaving"


def test_a_filtered_report_keeps_the_cabling_of_what_it_selected(tmp_path: Path) -> None:
    """A selection is elements; the links between them come with it."""
    CliRunner().invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "campus"),
            "report",
            "--namespace",
            "sites/north",
            "--out",
            str(tmp_path),
            "--no-diagrams",
        ],
        catch_exceptions=False,
    )
    page = (tmp_path / "sites" / f"{page_slug('sites/north')}.md").read_text(encoding="utf-8")
    assert "cbl-north-core-dist" in page, "the site's own cabling is missing from the schedule"
    device = tmp_path / "devices" / f"{page_slug('sites/north/core/rtr-north-core-01')}.md"
    assert "cbl-north-core-dist" in device.read_text(encoding="utf-8")


def test_the_site_pages_partition_the_elements(tmp_path: Path) -> None:
    """No element may be documented on two site pages; every one on exactly one."""
    (tmp_path / "sub" / "deeper").mkdir(parents=True)
    for path, name in (
        (tmp_path / "root-sw.yaml", "root-sw"),
        (tmp_path / "sub" / "leaf-sw.yaml", "leaf-sw"),
        (tmp_path / "sub" / "deeper" / "deep-sw.yaml", "deep-sw"),
    ):
        path.write_text(
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: switch\n"
            "metadata:\n"
            f"  name: {name}\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: eth0\n"
            "      type: ethernet\n",
            encoding="utf-8",
        )
    inventory = load_tree(tmp_path)
    report, _layout, _ = collect(inventory, options=options_for("markdown", diagrams=False))
    documented: dict[str, list[str]] = {}
    for page in report.of_kind("site"):
        table = next(
            table for section in page.sections for table in section.tables if table.key == "devices"
        )
        for row in table.rows:
            documented.setdefault(row[0].text, []).append(page.path)
    assert sorted(documented) == ["root-sw", "sub/deeper/deep-sw", "sub/leaf-sw"]
    for element, pages in documented.items():
        assert len(pages) == 1, f"{element} is documented on {pages}"


def test_a_patch_panel_port_map_lists_every_position_and_its_coupling(
    inventories: dict[str, Inventory],
) -> None:
    """A free port is what a reader planning a move is looking for.

    Both sides, and the coupling between them: the port names are ``front/7`` and
    ``rear/7`` — a bare position number is not a port, and a table keyed by one
    would report every position of every panel as free.
    """
    page = report_of("patch-room").text("sites/root.md")
    panel = inventories["patch-room"].patchpanels["panels/pp-r1-a"]
    for port in panel.interface_names():
        assert f"| {port} |" in page, f"the port map does not list {port}"
        coupled = panel.coupling[port]
        assert f"| {port} | {coupled} |" in page, f"{port} is not shown coupled to {coupled}"
    # And the position a cable lands on names it, rather than reading as free.
    assert "| front/7 | rear/7 | cbl-sw-pp07 |" in page


def test_a_device_page_carries_its_interfaces_addresses_and_links() -> None:
    """The four things a device page exists for, on one page."""
    page = report_of("patch-room").text("devices/network_sw-core-01.md")
    assert "10.10.0.2/24" in page  # an address, as configured
    assert "access 10" in page  # the VLAN configuration of a port
    assert "members: GigabitEthernet1/0/2" in page  # bridge membership
    assert "cbl-sw-pp07" in page  # a cable that terminates on it
    assert "U38" in page  # where it is bolted
    assert "48 W" in page  # what it draws


def test_a_device_page_names_the_run_a_patched_cable_belongs_to() -> None:
    """The cable ends at the panel; the *run* ends at the server, and both matter.

    At the cabling layer no edge crosses a panel — the panels are nodes there — so
    a page that only read that layer could never fill this in.
    """
    page = report_of("patch-room", diagrams=False).text("devices/network_sw-core-01.md")
    assert "pp-r1-a front/7→rear/7" in page, "the panels the run crosses are missing"
    assert "run sw-core-01:GigabitEthernet1/0/7 - srv-app-01:eno1" in page


def test_a_device_page_carries_its_routing() -> None:
    """VRFs, static routes and adjacencies, from the routing view."""
    bundle = report_of("campus")
    core = bundle.text("devices/sites_north_core_rtr-north-core-01.md")
    assert "## Routing" in core
    assert "10.1.0.0/16 blackhole metric 250" in core, "the static route is missing"
    assert "bgp" in core, "the iBGP session is missing"
    assert "ospf" in core, "the OSPF adjacency is missing"

    distribution = bundle.text("devices/sites_north_distribution_sw-north-dist-01.md")
    assert "mgmt" in distribution, "the management VRF is missing"
    assert "65001:99" in distribution, "the route distinguisher is missing"


def test_the_wireless_plan_is_the_one_netgraph_list_bss_prints(
    inventories: dict[str, Inventory],
) -> None:
    page = report_of("home-lab").text("sites/root.md")
    for row in listing.bss(inventories["home-lab"]).rows:
        assert row[0] in page, f"the BSS plan is missing {row[0]}"


def test_open_findings_are_carried_into_the_report(tmp_path: Path) -> None:
    """A report of an inventory with a warning in it says so, on the overview."""
    (tmp_path / "one.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n"
        "  name: sw-1\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eth0\n"
        "      type: ethernet\n",
        encoding="utf-8",
    )
    from netgraph.validate import validate

    inventory = load_tree(tmp_path)
    findings = validate(inventory)
    assert findings, "the fixture was meant to produce at least one finding"
    bundle, _ = generate(
        inventory,
        options=options_for("markdown", diagrams=False),
        diagnostics=build_diagnostics(inventory, findings).diagnostics,
    )
    overview = bundle.text("README.md")
    for finding in findings:
        assert finding.rule in overview, f"{finding.rule} is missing from the report"


# --------------------------------------------------------------------------- #
# Determinism and the bundle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("export_format", FORMATS)
def test_two_runs_produce_identical_bytes(export_format: str) -> None:
    """The property that lets a report be committed and reviewed as a diff."""
    first = report_of("patch-room", export_format)
    second = report_of("patch-room", export_format)
    assert first.files == second.files


def test_the_json_document_holds_every_page_and_its_links() -> None:
    """``-f json`` is the whole document, not a summary of it."""
    document = json.loads(report_of("campus", "json").text(JSON_FILE))
    assert document["meta"]["netgraph"] == VERSION
    assert document["meta"]["generatedAt"] == STAMP
    kinds = {page["kind"] for page in document["pages"]}
    assert kinds == {"overview", "site", "device"}
    linked = {
        cell["page"]
        for page in document["pages"]
        for section in page["sections"]
        for table in section["tables"]
        for row in table["rows"]
        for cell in row
        if isinstance(cell, dict) and cell.get("page")
    }
    paths = {page["path"] for page in document["pages"]}
    assert linked <= paths, f"the document links pages it does not hold: {linked - paths}"


def test_an_unwritable_destination_is_a_netgraph_error(tmp_path: Path) -> None:
    """The bundle is a document; a failure to write it is not a traceback."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(NetgraphError, match="cannot write the report"):
        report_of("home-lab", diagrams=False).write(blocked)


def test_a_cell_keeps_its_cross_reference_in_the_json_document() -> None:
    """A linked cell is an object; a plain one is the string, and nothing else."""
    assert Cell(text="sw-1").to_json() == "sw-1"
    assert Cell(text="sw-1", page="devices/sw-1.md", fragment="links").to_json() == {
        "text": "sw-1",
        "page": "devices/sw-1.md",
        "anchor": "links",
    }
    assert Cell(text="E002", url="https://example.test/rules#e002").to_json() == {
        "text": "E002",
        "url": "https://example.test/rules#e002",
    }


def test_writing_a_bundle_creates_the_layout_and_reports_stale_files(tmp_path: Path) -> None:
    bundle = report_of("home-lab", diagrams=False)
    stale = bundle.write(tmp_path)
    assert stale == ()
    assert (tmp_path / "README.md").is_file()
    assert (tmp_path / "devices").is_dir()

    (tmp_path / "devices" / "gone.md").write_text("stale\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("mine\n", encoding="utf-8")
    assert bundle.write(tmp_path) == ("devices/gone.md",)
    assert (tmp_path / "devices" / "gone.md").is_file(), "nothing is deleted without --prune"

    assert bundle.write(tmp_path, prune=True) == ("devices/gone.md",)
    assert not (tmp_path / "devices" / "gone.md").exists()
    assert (tmp_path / "notes.txt").is_file(), "a file no report writes is left alone"


def test_a_page_name_is_a_slug_of_the_qualified_name() -> None:
    assert page_slug("sites/hq/sw-core") == "sites_hq_sw-core"
    assert page_slug("SW-CORE") == "sw-core", "a page name is lower-cased for the filesystem"


def test_two_names_that_slug_the_same_get_distinct_pages() -> None:
    """Collisions are resolved in sorted order, so a page never moves."""
    layout = Layout.build("markdown", devices=("b/sw", "b_sw", "a/x"), sites=())
    assert layout.device("a/x") == "devices/a_x.md"
    assert sorted((layout.device("b/sw"), layout.device("b_sw"))) == [
        "devices/b_sw-2.md",
        "devices/b_sw.md",
    ]


def test_a_link_is_relative_to_the_page_that_holds_it() -> None:
    layout = Layout.build("html", devices=("hq/sw",), sites=("hq",))
    assert layout.link(layout.device("hq/sw"), frm="index.html") == "devices/hq_sw.html"
    assert layout.link(layout.device("hq/sw"), frm="sites/hq.html") == "../devices/hq_sw.html"
    assert (
        layout.link(layout.index, frm="devices/hq_sw.html", fragment="contents")
        == "../index.html#contents"
    )


# --------------------------------------------------------------------------- #
# Sites and layers
# --------------------------------------------------------------------------- #


def test_a_branching_tree_gets_a_page_per_site(inventories: dict[str, Inventory]) -> None:
    assert site_groups(inventories["campus"]) == ("sites/north", "sites/south", "sites/west")


def test_a_flat_tree_gets_one_page(inventories: dict[str, Inventory]) -> None:
    """Splitting ``routers/``, ``switches/``, ``hosts/`` would strand every cable."""
    assert site_groups(inventories["home-lab"]) == ("",)


def test_group_depth_overrides_the_grouping(inventories: dict[str, Inventory]) -> None:
    assert site_groups(inventories["campus"], depth=0) == ("sites",)
    assert len(site_groups(inventories["campus"], depth=2)) == 12


def test_a_layer_that_is_not_drawn_still_supplies_its_facts() -> None:
    """``--layer`` chooses pictures, not facts: a PDU is placed either way."""
    for layers in ((), (Layer.RACK,)):
        bundle = report_of("patch-room", layers=layers, diagrams=False)
        page = bundle.text("devices/power_pdu-r1-a.md")
        assert "| Rack | r1 |" in page, f"the placement is missing with --layer {layers}"
        site = bundle.text("sites/root.md")
        assert "VLAN, subnet and element matrix" in site
        assert "| 10 | servers |" in site, f"the VLAN matrix is empty with --layer {layers}"


def test_a_rack_elevation_is_credited_to_the_elements_in_it() -> None:
    """The one drawing that shows a device's rack unit must link from its page."""
    page = report_of("patch-room").text("devices/network_sw-core-01.md")
    assert "(rack)" in page, "the rack elevation is not listed among the diagrams"


def test_two_scopes_cannot_share_a_drawing(tmp_path: Path) -> None:
    """Two namespaces that slug the same are two pages, so also two drawings."""
    for namespace, name in (("North", "sw-a"), ("north", "sw-b")):
        directory = tmp_path / "sites" / namespace
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.yaml").write_text(
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: switch\n"
            "metadata:\n"
            f"  name: {name}\n"
            "spec:\n"
            "  interfaces:\n"
            "    - name: eth0\n"
            "      type: ethernet\n",
            encoding="utf-8",
        )
    report, _layout, _ = collect(load_tree(tmp_path), options=options_for("markdown"))
    drawings = [
        diagram.path
        for page in report.of_kind("site")
        for section in page.sections
        for diagram in section.diagrams
    ]
    assert len(drawings) == len(set(drawings)), f"two pages share a drawing: {drawings}"


def test_the_drawn_layers_are_the_ones_the_inventory_earned(
    inventories: dict[str, Inventory],
) -> None:
    assert layers_for(inventories["home-lab"]) == (Layer.L1, Layer.L2, Layer.L3)
    assert Layer.PHYSICAL in layers_for(inventories["patch-room"])
    assert Layer.POWER in layers_for(inventories["patch-room"])
    assert Layer.RACK in layers_for(inventories["patch-room"])
    assert Layer.OVERLAY in layers_for(inventories["overlay"])
    assert Layer.ROUTING in layers_for(inventories["campus"])


def test_an_explicit_layer_is_honoured_even_when_it_is_empty(
    inventories: dict[str, Inventory],
) -> None:
    assert layers_for(inventories["home-lab"], (Layer.OVERLAY,)) == (Layer.OVERLAY,)
    report, layout, _ = collect(
        inventories["home-lab"], options=options_for("markdown", layers=(Layer.OVERLAY,))
    )
    page = report.page_at(layout.index)
    assert page is not None
    drawings = [diagram for section in page.sections for diagram in section.diagrams]
    assert [diagram.layer for diagram in drawings] == ["overlay"]


def test_a_report_without_diagrams_says_so_rather_than_omitting_them() -> None:
    bundle = report_of("home-lab", diagrams=False)
    assert "--no-diagrams" in bundle.text("README.md")
    assert not [path for path in bundle.paths if path.endswith(".svg")]


def test_a_layer_that_cannot_be_laid_out_is_a_note_and_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graphviz is optional for every other artefact, and for this one too."""
    monkeypatch.setattr("netgraph.report.diagrams.find_dot", lambda: None)
    bundle, diagrams = generate(load_tree(EXAMPLES / "home-lab"), options=options_for("markdown"))
    overview = bundle.text("README.md")
    assert "Graphviz is not installed" in overview
    assert not [path for path in bundle.paths if path.endswith(".svg")]
    assert diagrams.problems == [], "a missing Graphviz is a fact, not a problem to report"


def test_a_layout_that_fails_is_reported_once_and_costs_no_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken Graphviz must not take the schedule and the address plan with it."""

    def explode(*_arguments: object, **_keywords: object) -> bytes:
        raise RenderError("dot died")

    monkeypatch.setattr("netgraph.report.diagrams.find_dot", lambda: "/usr/bin/dot")
    monkeypatch.setattr("netgraph.report.diagrams.to_image", explode)
    bundle, diagrams = generate(load_tree(EXAMPLES / "home-lab"), options=options_for("markdown"))
    overview = bundle.text("README.md")
    assert "could not be laid out" in overview
    assert "Address plan" in overview, "the tables are gone with the drawing"
    assert diagrams.problems, "the failure is not reported to the caller"


def test_a_json_report_does_not_claim_a_flag_that_was_not_given() -> None:
    """``-f json`` holds no drawing because it is JSON, not because of a flag."""
    document = json.loads(report_of("home-lab", "json").text(JSON_FILE))
    notes = {
        diagram["note"]
        for page in document["pages"]
        for section in page["sections"]
        for diagram in section["diagrams"]
    }
    assert notes, "the layers are not recorded at all"
    assert not any("--no-diagrams" in note for note in notes), notes


# --------------------------------------------------------------------------- #
# The stamp and the revision
# --------------------------------------------------------------------------- #


def test_a_pinned_timestamp_is_used_verbatim() -> None:
    assert resolve_timestamp("2026-01-31T09:00:00Z") == "2026-01-31T09:00:00Z"
    assert resolve_timestamp("2026-01-31T10:00:00+01:00") == "2026-01-31T09:00:00Z"
    assert resolve_timestamp("2026-01-31 09:00:00") == "2026-01-31T09:00:00Z"


def test_no_timestamp_leaves_the_stamp_out() -> None:
    assert resolve_timestamp(NO_TIMESTAMP) == ""


def test_the_reproducible_builds_variable_is_honoured() -> None:
    assert resolve_timestamp("", environ={EPOCH_ENV_VAR: "1769850000"}).endswith("Z")
    assert resolve_timestamp("", environ={EPOCH_ENV_VAR: "0"}) == "1970-01-01T00:00:00Z"


def test_the_clock_is_the_last_resort() -> None:
    moment = datetime(2026, 1, 31, 9, 0, 0, tzinfo=timezone.utc)
    assert resolve_timestamp("", environ={}, now=moment) == "2026-01-31T09:00:00Z"


@pytest.mark.parametrize("value", ("yesterday", "2026-13-01", ""))
def test_an_unusable_timestamp_is_refused(value: str) -> None:
    """A typo must not fall back to the clock: the stamp would then be a lie."""
    if not value:
        pytest.skip("the empty string means 'not given', which is not an error")
    with pytest.raises(NetgraphError, match="ISO-8601"):
        resolve_timestamp(value)


@pytest.mark.parametrize("value", ("tuesday", "99999999999999999999"))
def test_an_unusable_source_date_epoch_is_refused(value: str) -> None:
    """Not a number, and a number no platform can turn into a date: one error."""
    with pytest.raises(NetgraphError, match=EPOCH_ENV_VAR):
        resolve_timestamp("", environ={EPOCH_ENV_VAR: value})


def test_a_directory_outside_a_work_tree_has_no_revision(tmp_path: Path) -> None:
    assert git_revision(tmp_path) is None


def test_a_work_tree_reports_its_commit_and_whether_it_is_clean(tmp_path: Path) -> None:
    if subprocess.run(["git", "--version"], capture_output=True, check=False).returncode:
        pytest.skip("git is not installed")  # pragma: no cover - depends on the machine
    run = ["git", "-C", str(tmp_path)]
    subprocess.run([*run, "init", "-q"], check=True)
    subprocess.run([*run, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*run, "config", "user.name", "t"], check=True)
    (tmp_path / "a.yaml").write_text("a: 1\n", encoding="utf-8")
    subprocess.run([*run, "add", "."], check=True)
    subprocess.run([*run, "commit", "-qm", "one"], check=True)

    revision = git_revision(tmp_path)
    assert revision is not None and len(revision.commit) == 12
    assert revision.state == "clean"

    (tmp_path / "a.yaml").write_text("a: 2\n", encoding="utf-8")
    modified = git_revision(tmp_path)
    assert modified is not None and modified.state == "modified"


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def test_a_template_directory_overrides_one_page(tmp_path: Path) -> None:
    """``--template DIR`` replaces the templates it holds and no others."""
    (tmp_path / "device.md.j2").write_text(
        "# {{ page.title }}\n\nonly this, for {{ meta.inventory }}\n", encoding="utf-8"
    )
    inventory = load_tree(EXAMPLES / "home-lab")
    bundle, _ = generate(
        inventory, options=options_for("markdown", diagrams=False), templates=tmp_path
    )
    assert bundle.text("devices/routers_rtr-home.md") == ("# rtr-home\n\nonly this, for home-lab\n")
    assert "## Address plan" in bundle.text("README.md"), "the overview is untouched"


def test_a_template_that_fails_is_reported_as_such(tmp_path: Path) -> None:
    (tmp_path / "overview.md.j2").write_text("{{ nope.missing }}\n", encoding="utf-8")
    inventory = load_tree(EXAMPLES / "home-lab")
    with pytest.raises(NetgraphError, match=re.escape("overview.md.j2")):
        generate(inventory, options=options_for("markdown", diagrams=False), templates=tmp_path)


def test_a_missing_template_directory_is_refused(tmp_path: Path) -> None:
    inventory = load_tree(EXAMPLES / "home-lab")
    with pytest.raises(NetgraphError, match="not a directory"):
        generate(
            inventory,
            options=options_for("markdown", diagrams=False),
            templates=tmp_path / "absent",
        )


#: The link target a hostile namespace would open. ``javascript:`` is the worst
#: case and is what runs on POSIX; Windows forbids a colon in a path, so the
#: target there is an ordinary relative one. Nothing is lost by the substitution:
#: what the test is about is whether the ``]`` in front of it is escaped, and the
#: scheme only says why that matters.
_SMUGGLED_TARGET = "(evil.md)" if ON_WINDOWS else "(javascript:alert(1))"

#: A directory name that would close a link label and open a target of its own.
SMUGGLING_NAMESPACE = f"a]{_SMUGGLED_TARGET}"


def test_a_namespace_cannot_smuggle_a_link_into_a_page(tmp_path: Path) -> None:
    """A namespace is a *directory name*, and nothing validates it as a name.

    So the text of every cell is escaped for the construct it sits in — including
    the brackets of the link it may be wrapped in, which are what would otherwise
    let a directory called ``a](javascript:…)`` publish a live link.
    """
    directory = tmp_path / SMUGGLING_NAMESPACE
    directory.mkdir()
    (directory / "sw.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n"
        "  name: sw-1\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eth0\n"
        "      type: ethernet\n",
        encoding="utf-8",
    )
    bundle, _ = generate(load_tree(tmp_path), options=options_for("markdown", diagrams=False))
    # An unescaped ``]`` closes a link label; the escaped form is a literal
    # bracket to a Markdown parser and closes nothing, which is the whole point.
    smuggled = re.compile(r"(?<!\\)\]" + re.escape(_SMUGGLED_TARGET))
    escaped = f"a\\]{_SMUGGLED_TARGET}"
    exercised = False
    for path in bundle.paths:
        text = bundle.text(path)
        assert not smuggled.search(text), f"{path} carries the namespace as a live link"
        exercised = exercised or escaped in text
    assert exercised, "the fixture stopped exercising this: no page names the namespace"


@requires_dot
def test_an_html_page_allows_exactly_the_style_it_inlines(tmp_path: Path) -> None:
    """The CSP is a hash of the inlined block, so the two have to be one string.

    A newline between them is enough for a browser to refuse the style sheet and
    render the whole bundle unstyled — which no test that only reads the markup
    would ever notice.
    """
    from base64 import b64encode
    from hashlib import sha256
    from html import unescape

    page = report_of("home-lab", "html").text("index.html")
    inlined = page.partition("<style>")[2].partition("</style>")[0]
    digest = b64encode(sha256(inlined.encode("utf-8")).digest()).decode("ascii")
    # The policy sits in an attribute, so its quotes arrive HTML-escaped; a
    # browser unescapes them before reading it and so does this.
    declared = re.search(r'Content-Security-Policy" content="([^"]+)"', page)
    assert declared is not None, "the page carries no policy at all"
    policy = unescape(declared.group(1))
    assert f"style-src 'sha256-{digest}'" in policy, "the page's own policy refuses its style"
    assert "script-src 'none'" in policy, "a report page runs nothing"


def test_inventory_text_cannot_become_markup(tmp_path: Path) -> None:
    """A description is data. A report is published. The two must not mix."""
    (tmp_path / "one.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata:\n"
        "  name: sw-1\n"
        '  description: "<script>alert(1)</script> and a | pipe"\n'
        "spec:\n"
        "  interfaces:\n"
        "    - name: eth0\n"
        "      type: ethernet\n",
        encoding="utf-8",
    )
    inventory = load_tree(tmp_path)
    for export_format in ("markdown", "html"):
        bundle, _ = generate(inventory, options=options_for(export_format, diagrams=False))
        for path in bundle.paths:
            text = bundle.text(path)
            assert "<script>" not in text, f"{path} carries a live script tag"
    markdown = report_bundle_text(tmp_path, "devices/sw-1.md")
    assert r"\|" in markdown, "a pipe in a description would break the table it is in"


def report_bundle_text(root: Path, path: str) -> str:
    inventory = load_tree(root)
    bundle, _ = generate(inventory, options=options_for("markdown", diagrams=False))
    return bundle.text(path)


# --------------------------------------------------------------------------- #
# Goldens
# --------------------------------------------------------------------------- #


def transcript(bundle: Bundle) -> str:
    """A whole bundle as one comparable document.

    One golden per report rather than one per page: a report is read as a set of
    pages that agree with each other, and a diff that shows a table moving from
    one page to another is more useful than twenty files changing at once. A
    drawing is named and not embedded — see the module docstring.
    """
    lines: list[str] = []
    for path in bundle.paths:
        if path.endswith(".svg"):
            lines.append(f"=== {path} (drawing, not compared) ===\n")
            continue
        lines.append(f"=== {path} ===\n{bundle.text(path)}")
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("name", "export_format"),
    (
        ("home-lab", "markdown"),
        ("home-lab", "json"),
        ("overlay", "markdown"),
    ),
)
@requires_dot
def test_the_bundle_matches_its_golden(name: str, export_format: str, regen_golden: bool) -> None:
    actual = transcript(report_of(name, export_format))
    golden = GOLDEN / f"{name}-{export_format}.txt"
    if regen_golden:
        golden.parent.mkdir(parents=True, exist_ok=True)
        # netgraph.fsio.write_text, not Path.write_text: a golden is a
        # byte-for-byte artefact and Python's text mode would rewrite every line
        # ending on Windows. See .gitattributes.
        write_text(golden, actual)
        pytest.skip(f"regenerated {golden.name}")
    assert golden.exists(), (
        f"missing golden {golden.relative_to(REPO_ROOT)}; "
        "create it with 'pytest tests/test_report.py --regen-golden'"
    )
    assert actual == golden.read_text(encoding="utf-8"), (
        f"the {export_format} report of {name} drifted from its golden. "
        "If the change is intended, rerun with --regen-golden and review the diff."
    )


@requires_dot
def test_the_committed_example_report_is_up_to_date() -> None:
    """``docs/example-report/`` is what the generator would write today."""
    tool = load_tool("gen_example_report")
    stale = tool.check(tool.build())
    assert not stale, (
        f"docs/example-report/ is stale: {stale}. Run 'python tools/gen_example_report.py'."
    )


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def run_cli(*arguments: str) -> object:
    return CliRunner().invoke(cli, list(arguments), catch_exceptions=False)


def test_markdown_needs_an_output_directory() -> None:
    result = CliRunner().invoke(
        cli, ["-i", str(EXAMPLES / "home-lab"), "report"], catch_exceptions=False
    )
    assert result.exit_code == 2
    assert "--out" in result.output


def test_json_goes_to_stdout_without_an_output_directory() -> None:
    result = CliRunner().invoke(
        cli,
        ["-q", "-i", str(EXAMPLES / "home-lab"), "report", "-f", "json", "--generated-at", "none"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["meta"]["generatedAt"] is None


def test_the_command_writes_a_bundle(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "home-lab"),
            "report",
            "--out",
            str(tmp_path),
            "--no-diagrams",
            "--generated-at",
            STAMP,
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert (tmp_path / "README.md").is_file()
    assert STAMP in (tmp_path / "README.md").read_text(encoding="utf-8")


def test_the_filters_scope_the_report(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "campus"),
            "report",
            "--namespace",
            "sites/north",
            "--out",
            str(tmp_path),
            "--no-diagrams",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    pages = {path.name for path in (tmp_path / "devices").iterdir()}
    assert all(name.startswith("sites_north") for name in pages), pages
    overview = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "namespace=sites/north" in overview, "the scope is not stated on the page"


def test_an_unknown_neighbour_is_a_usage_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "campus"),
            "report",
            "--neighbors-of",
            "nope",
            "--out",
            str(tmp_path),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    assert "no element named 'nope'" in result.output


def test_an_inventory_with_errors_is_refused_unless_forced(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory"
    inventory.mkdir()
    (inventory / "one.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata:\n"
        "  name: cbl-1\n"
        "spec:\n"
        "  endpoints:\n"
        "    - nowhere:eth0\n"
        "    - elsewhere:eth0\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    arguments = ["-i", str(inventory), "report", "--out", str(out), "--no-diagrams"]
    refused = CliRunner().invoke(cli, arguments, catch_exceptions=False)
    assert refused.exit_code == 1
    assert not out.exists()

    forced = CliRunner().invoke(cli, [*arguments, "--force"], catch_exceptions=False)
    assert forced.exit_code == 0, forced.output
    overview = (out / "README.md").read_text(encoding="utf-8")
    for error in load_tree(inventory).errors:
        assert error.message in overview, "the problem that refused the run is not in the report"


@requires_dot
def test_the_html_bundle_links_a_diagram_to_the_device_pages(tmp_path: Path) -> None:
    """The interactive half: a shape in a drawing is an anchor to its own page."""
    result = CliRunner().invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "patch-room"),
            "report",
            "-f",
            "html",
            "--out",
            str(tmp_path),
            "--generated-at",
            "none",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    page = (tmp_path / "sites" / "root.html").read_text(encoding="utf-8")
    assert "<svg" in page, "the drawing is embedded rather than referenced"
    assert 'xlink:href="../devices/network_sw-core-01.html"' in page
    # Self-contained means nothing is fetched: every reference on the page is
    # relative to the bundle. ``xmlns`` is deliberately not checked — an XML
    # namespace is an identifier, not a URL anything resolves.
    external = [
        target
        for target in _HTML_LINK.findall(page)
        if target.startswith(("http://", "https://", "//"))
    ]
    assert external == [], f"the page would fetch {external}"
    assert "Content-Security-Policy" in page


def test_prune_leaves_a_readers_own_directory_alone(tmp_path: Path) -> None:
    """Pruning removes what it emptied, not every empty directory it can see."""
    bundle = report_of("home-lab", diagrams=False)
    bundle.write(tmp_path)
    (tmp_path / "photos").mkdir()
    (tmp_path / "devices" / "gone.md").write_text("stale\n", encoding="utf-8")

    bundle.write(tmp_path, prune=True)
    assert (tmp_path / "photos").is_dir(), "an empty directory of the reader's was removed"
    assert (tmp_path / "devices").is_dir(), "a directory the report still writes into was removed"


def test_prune_removes_a_directory_it_emptied(tmp_path: Path) -> None:
    bundle = report_of("home-lab", diagrams=False)
    bundle.write(tmp_path)
    (tmp_path / "diagrams").mkdir()
    (tmp_path / "diagrams" / "old.svg").write_text("<svg/>\n", encoding="utf-8")

    assert bundle.write(tmp_path, prune=True) == ("diagrams/old.svg",)
    assert not (tmp_path / "diagrams").exists()


def test_prune_is_reported_and_optional(tmp_path: Path) -> None:
    arguments = [
        "-i",
        str(EXAMPLES / "home-lab"),
        "report",
        "--out",
        str(tmp_path),
        "--no-diagrams",
    ]
    CliRunner().invoke(cli, arguments, catch_exceptions=False)
    (tmp_path / "devices" / "old.md").write_text("stale\n", encoding="utf-8")

    kept = CliRunner().invoke(cli, arguments, catch_exceptions=False)
    assert "devices/old.md" in kept.output
    assert (tmp_path / "devices" / "old.md").is_file()

    pruned = CliRunner().invoke(cli, [*arguments, "--prune"], catch_exceptions=False)
    assert "devices/old.md" in pruned.output
    assert not (tmp_path / "devices" / "old.md").exists()


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #

#: What the generator may cost over a 100-device inventory, in seconds. Generous
#: by an order of magnitude: this is a regression guard against an accidental
#: quadratic — a per-device walk of every edge, or a graph rebuilt per page — and
#: not a benchmark. The drawings are excluded because Graphviz dominates them and
#: is not netgraph's cost to bound.
COST_BUDGET_SECONDS = 20.0


@pytest.fixture(scope="module")
def large_inventory(tmp_path_factory: pytest.TempPathFactory) -> Inventory:
    """A generated tree of 100 devices across 4 sites, from the bench harness."""
    harness = load_tool("bench_pipeline")
    root = tmp_path_factory.mktemp("report-inventory")
    harness.generate(root, harness.Shape(sites=4, racks_per_site=2, hosts_per_rack=10))
    return load_tree(root)


def test_a_large_inventory_is_documented_within_its_budget(large_inventory: Inventory) -> None:
    start = time.perf_counter()
    bundle, _ = generate(large_inventory, options=options_for("markdown", diagrams=False))
    elapsed = time.perf_counter() - start
    assert len(bundle.files) > len(large_inventory.devices), "a page per device, plus the sites"
    assert elapsed < COST_BUDGET_SECONDS, (
        f"documenting {len(large_inventory.devices)} devices took {elapsed:.1f}s, "
        f"over the {COST_BUDGET_SECONDS:.0f}s budget"
    )


def test_a_large_bundle_still_has_no_broken_link(large_inventory: Inventory) -> None:
    """Scale is where a link breaks: two names that slug the same, a missing page."""
    bundle, _ = generate(large_inventory, options=options_for("markdown", diagrams=False))
    texts = {path: bundle.text(path) for path in bundle.paths}
    anchors = {path: anchors_of(path, text) for path, text in texts.items()}
    for path, text in texts.items():
        for file_part, anchor in links_of(path, text):
            target = resolve(path, file_part) if file_part else path
            assert target in bundle.files, f"{path} links to the missing {target}"
            if anchor:
                assert anchor in anchors[target]
