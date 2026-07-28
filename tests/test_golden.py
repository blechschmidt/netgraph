"""Renderer snapshot tests against committed golden files.

The property under test is *byte-for-byte stability*. The other render tests
assert that particular facts reach the output; these assert that nothing else
moved. That is what makes ``netgraph render -f dot > topology.dot`` a file worth
committing: a diff in it means the inventory changed, not that the renderer
reshuffled its attribute order.

The goldens live in ``tests/fixtures/golden/`` and are regenerated with::

    pytest tests/test_golden.py --regen-golden

Regeneration rewrites every snapshot, so the resulting ``git diff`` *is* the
review: an intentional renderer change shows up as a readable diff, and an
accidental one shows up next to it.

A golden must not embed anything machine-specific. Nothing here renders an
absolute path — :attr:`Graph.root` is deliberately absent from all three
formats — so the files are identical on every checkout.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from netgraph.loader import Inventory, load_tree
from netgraph.render import (
    GRAPH_KIND,
    Layer,
    RenderOptions,
    build_graph,
    render_text,
    suffix_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"

#: The text formats a golden is kept for.
FORMATS = ("dot", "mermaid", "json")


@dataclass(frozen=True)
class Case:
    """One inventory rendered under one set of display options."""

    #: Stem of the golden file, e.g. ``campus-l2-grouped``.
    name: str
    #: Directory under ``examples/``.
    example: str
    layer: Layer
    options: RenderOptions

    def golden(self, format: str) -> Path:
        return GOLDEN_DIR / f"{self.name}{suffix_for(format)}"


#: The matrix is chosen so that every display option that changes the output is
#: exercised by at least one golden, and so that both examples are covered at
#: every layer.
CASES = (
    Case(
        name="home-lab-l1",
        example="home-lab",
        layer=Layer.L1,
        options=RenderOptions(),
    ),
    Case(
        name="home-lab-l2",
        example="home-lab",
        layer=Layer.L2,
        options=RenderOptions(title="Home lab, layer 2"),
    ),
    Case(
        name="campus-l1-plain",
        example="campus",
        layer=Layer.L1,
        # The minimal rendering: no addresses, no VLANs, no groups.
        options=RenderOptions(show_ips=False, show_vlans=False),
    ),
    Case(
        name="campus-l2-grouped",
        example="campus",
        layer=Layer.L2,
        options=RenderOptions(group_by_namespace=True, title="Campus", max_addresses=2),
    ),
    Case(
        name="home-lab-l3",
        example="home-lab",
        layer=Layer.L3,
        options=RenderOptions(title="Home lab, layer 3"),
    ),
    Case(
        # Grouping and layer 3 together: the subnets belong to no namespace, so
        # this pins down that they stay outside every cluster.
        name="campus-l3-grouped",
        example="campus",
        layer=Layer.L3,
        options=RenderOptions(group_by_namespace=True, title="Campus, layer 3"),
    ),
)

#: The cases whose graph holds subnet nodes.
L3_CASES = tuple(case for case in CASES if case.layer is Layer.L3)

CASES_BY_NAME = {case.name: case for case in CASES}


@pytest.fixture(scope="session")
def inventories() -> dict[str, Inventory]:
    """Both example trees, loaded once for the whole session."""
    loaded = {}
    for example in sorted({case.example for case in CASES}):
        inventory = load_tree(EXAMPLES / example)
        assert inventory.errors == [], f"{example} does not load cleanly: {inventory.errors}"
        loaded[example] = inventory
    return loaded


def _render(case: Case, inventories: dict[str, Inventory], format: str) -> str:
    graph = build_graph(inventories[case.example], layer=case.layer)
    return render_text(graph, format, case.options)


# --------------------------------------------------------------------------- #
# The snapshots
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("format", FORMATS)
def test_rendering_matches_its_golden_file(
    case: Case,
    format: str,
    inventories: dict[str, Inventory],
    regen_golden: bool,
) -> None:
    actual = _render(case, inventories, format)
    golden = case.golden(format)

    if regen_golden:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="utf-8")
        pytest.skip(f"regenerated {golden.name}")

    assert golden.exists(), (
        f"missing golden {golden.relative_to(REPO_ROOT)}; "
        f"create it with 'pytest tests/test_golden.py --regen-golden'"
    )
    expected = golden.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{format} rendering of {case.name} drifted from its golden file. "
        f"If the change is intended, rerun with --regen-golden and review the diff."
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("format", FORMATS)
def test_rendering_is_reproducible_within_a_process(
    case: Case, format: str, inventories: dict[str, Inventory]
) -> None:
    """Two renderings of one graph agree, so a golden can exist at all.

    Set iteration order is stable within a process, so this would not catch an
    unsorted set on its own — but combined with the committed golden, which was
    produced by a *different* process, it pins the ordering down.
    """
    assert _render(case, inventories, format) == _render(case, inventories, format)


def test_a_rebuilt_graph_renders_identically(inventories: dict[str, Inventory]) -> None:
    """Rebuilding from the same inventory must not reshuffle anything."""
    case = CASES_BY_NAME["campus-l2-grouped"]
    inventory = inventories[case.example]
    first = render_text(build_graph(inventory, layer=case.layer), "dot", case.options)
    second = render_text(build_graph(inventory, layer=case.layer), "dot", case.options)
    assert first == second


def test_reloading_the_inventory_reproduces_the_golden() -> None:
    """A second load from disk yields the same bytes.

    The load order of a directory tree drives node order, so this catches a
    renderer that depends on the order the filesystem happened to return.
    """
    case = CASES_BY_NAME["home-lab-l1"]
    inventory = load_tree(EXAMPLES / case.example)
    assert inventory.errors == []
    actual = render_text(build_graph(inventory, layer=case.layer), "dot", case.options)
    assert actual == case.golden("dot").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The goldens are not merely stable — they are well-formed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_the_dot_golden_is_something_graphviz_accepts(case: Case) -> None:
    """A stable file that ``dot`` rejects would be a stable bug."""
    graphviz = pytest.importorskip("graphviz")
    if not shutil.which("dot"):
        pytest.skip("the Graphviz 'dot' executable is not installed")
    source = case.golden("dot").read_text(encoding="utf-8")
    # ``pipe`` raises CalledProcessError on a syntax error, which fails the test.
    assert graphviz.Source(source, engine="dot").pipe(format="svg")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_the_json_golden_parses_and_carries_the_envelope(case: Case) -> None:
    payload = json.loads(case.golden("json").read_text(encoding="utf-8"))
    assert payload["kind"] == GRAPH_KIND
    assert payload["layer"] == case.layer.value
    assert payload["nodes"], "a golden with no nodes would assert nothing"
    assert payload["edges"], "a golden with no edges would assert nothing"

    # Every edge endpoint names a node the same document declares. This is the
    # invariant the exporter promises its consumers, checked on the artefact
    # rather than on an in-memory graph.
    ids = {node["id"] for node in payload["nodes"]}
    for edge in payload["edges"]:
        endpoints = edge["endpoints"]
        assert len(endpoints) == 2
        for endpoint in endpoints:
            assert endpoint["node"] in ids

    # ``show_ips`` and ``show_vlans`` control per-port detail (§ jsonexport).
    ports = [port for node in payload["nodes"] for port in node["interfaces"]]
    if not case.options.show_ips:
        assert not any("addresses" in port for port in ports)
    if not case.options.show_vlans:
        assert not any("vlans" in port for port in ports)

    # Every node says which kind of thing it is, at every layer.
    assert {node["type"] for node in payload["nodes"]} <= {"element", "subnet"}
    if case.layer is not Layer.L3:
        assert all(node["type"] == "element" for node in payload["nodes"])


@pytest.mark.parametrize("case", L3_CASES, ids=lambda case: case.name)
def test_the_l3_json_golden_separates_subnets_from_elements(case: Case) -> None:
    """A consumer must be able to tell a derived prefix from a declared device."""
    payload = json.loads(case.golden("json").read_text(encoding="utf-8"))
    subnets = [node for node in payload["nodes"] if node["type"] == "subnet"]
    elements = [node for node in payload["nodes"] if node["type"] == "element"]
    assert subnets and elements

    for node in subnets:
        assert node["kind"] == "subnet"
        assert node["interfaces"] == []
        subnet = node["subnet"]
        assert node["id"] == f"subnet:{subnet['prefix']}"
        assert node["name"] == subnet["prefix"]
        assert subnet["family"] in ("ipv4", "ipv6")
        assert subnet["elements"], "an empty subnet must not be drawn at all"
        assert set(subnet["elements"]) <= {node["id"] for node in elements}
    for node in elements:
        assert "subnet" not in node

    # Every edge is a membership: one element end, one subnet end, addressed.
    ids = {node["id"] for node in subnets}
    for edge in payload["edges"]:
        assert edge["kind"] == "subnet"
        assert "medium" not in edge
        assert edge["addresses"]
        first, second = edge["endpoints"]
        assert first["node"] not in ids and first["interface"]
        assert second["node"] in ids and "interface" not in second


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_the_mermaid_golden_declares_a_flowchart(case: Case) -> None:
    text = case.golden("mermaid").read_text(encoding="utf-8")
    assert re.search(r"^flowchart ", text, re.MULTILINE)

    # Identifiers are positional (n0, n1, …) because a fully-qualified name is
    # not a legal Mermaid id. They must be dense and start at zero, or the
    # ``class nN kind`` lines at the foot would style the wrong nodes.
    declared = re.findall(r"^ +(n\d+)[(\[>{]", text, re.MULTILINE)
    assert declared == [f"n{index}" for index in range(len(declared))]

    # Every link joins two declared ids.
    for left, right in re.findall(r"^ +(n\d+) +(?:--|==|-\.).*? (n\d+)$", text, re.MULTILINE):
        assert left in declared
        assert right in declared


def test_every_case_has_a_golden_for_every_format() -> None:
    """A format that quietly lost its snapshot must not pass by absence."""
    missing = [
        str(case.golden(format).relative_to(REPO_ROOT))
        for case in CASES
        for format in FORMATS
        if not case.golden(format).exists()
    ]
    assert missing == []


def test_no_stray_golden_files() -> None:
    """Every committed golden belongs to a case, so renames leave no orphans."""
    expected = {case.golden(format) for case in CASES for format in FORMATS}
    actual = {path for path in GOLDEN_DIR.iterdir() if path.suffix != ".md"}
    assert actual == expected


def test_goldens_are_free_of_machine_specific_paths() -> None:
    """A golden carrying an absolute path would fail on another checkout."""
    for case in CASES:
        for format in FORMATS:
            text = case.golden(format).read_text(encoding="utf-8")
            assert str(REPO_ROOT) not in text
            assert "/home/" not in text
            assert "/root/" not in text
