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
    LinkTemplate,
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
    #: Which formats this case is kept for. Options that only one backend
    #: honours — tooltips, links, element ids — would otherwise commit two more
    #: byte-identical copies of a Mermaid and a JSON golden per case.
    formats: tuple[str, ...] = FORMATS

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
    Case(
        # Tunnels over the physical topology: the point-to-point ones as edges,
        # the three-ended mesh as a node, and the two nested tunnels labelled
        # with the stack they run in.
        name="overlay-l1",
        example="overlay",
        layer=Layer.L1,
        options=RenderOptions(title="Overlay, layer 1"),
    ),
    Case(
        # VLAN 100 crosses the VXLAN, which is the whole reason a layer-2 tunnel
        # carries VLANs at all.
        name="overlay-l2",
        example="overlay",
        layer=Layer.L2,
        options=RenderOptions(title="Overlay, layer 2"),
    ),
    Case(
        # Everything an SVG carries besides the picture: a tooltip per node,
        # edge and cluster, a link back to the declaring document, and a stable
        # id on all three. Grouped, because a cluster is the one of the three
        # that has nowhere else to be pinned.
        name="home-lab-l1-interactive",
        example="home-lab",
        layer=Layer.L1,
        options=RenderOptions(
            group_by_namespace=True,
            element_ids=True,
            link_template=LinkTemplate.parse(
                "https://git.example.com/net/blob/main/{file}#L{line}"
            ),
        ),
        formats=("dot",),
    ),
    Case(
        # The same inventory with the detail turned off: nothing but the
        # picture, which is what a diagram published somewhere it should carry
        # no addresses needs.
        name="home-lab-l1-inert",
        example="home-lab",
        layer=Layer.L1,
        options=RenderOptions(tooltips=False, show_ips=False, show_vlans=False),
        formats=("dot",),
    ),
    Case(
        # The encapsulation graph: every tunnel a node, and 'over' an edge
        # between two of them. Grouped, so a tunnel keeping its own namespace —
        # unlike a subnet, which keeps none — is pinned down too.
        name="overlay-overlay",
        example="overlay",
        layer=Layer.OVERLAY,
        options=RenderOptions(group_by_namespace=True, title="Overlay, encapsulation"),
    ),
)

#: The cases a Mermaid golden is kept for.
MERMAID_CASES = tuple(case for case in CASES if "mermaid" in case.formats)

#: The cases a JSON golden is kept for.
JSON_CASES = tuple(case for case in CASES if "json" in case.formats)

#: The cases whose graph holds subnet nodes.
L3_CASES = tuple(case for case in CASES if case.layer is Layer.L3 and "json" in case.formats)

#: The cases whose graph holds tunnel nodes for every tunnel.
OVERLAY_CASES = tuple(
    case for case in CASES if case.layer is Layer.OVERLAY and "json" in case.formats
)

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


def _skip_unkept(case: Case, format: str) -> None:
    """Skip a (case, format) pair the matrix deliberately keeps no golden for."""
    if format not in case.formats:
        pytest.skip(f"{case.name} keeps no {format} golden")


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
    _skip_unkept(case, format)
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
    _skip_unkept(case, format)
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


@pytest.mark.parametrize("case", JSON_CASES, ids=lambda case: case.name)
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
    assert {node["type"] for node in payload["nodes"]} <= {"element", "subnet", "tunnel"}
    if case.layer is Layer.L3:
        assert any(node["type"] == "subnet" for node in payload["nodes"])
    elif case.layer is Layer.OVERLAY:
        assert any(node["type"] == "tunnel" for node in payload["nodes"])
    else:
        # Below the overlay layer a tunnel is an edge, unless it joins more than
        # two endpoints and has no line shape to take.
        assert all(node["type"] in {"element", "tunnel"} for node in payload["nodes"])


@pytest.mark.parametrize("case", OVERLAY_CASES, ids=lambda case: case.name)
def test_the_overlay_json_golden_carries_the_encapsulation_stack(case: Case) -> None:
    """The nesting a reader came for has to survive into the exported document."""
    payload = json.loads(case.golden("json").read_text(encoding="utf-8"))
    tunnels = [node for node in payload["nodes"] if node["type"] == "tunnel"]
    assert tunnels

    for node in tunnels:
        assert node["kind"] == "tunnel"
        assert node["interfaces"] == []
        tunnel = node["tunnel"]
        assert node["id"] == f"tunnel:{tunnel['id']}"
        # The stack always starts with the tunnel's own type, so a consumer can
        # read ``["vxlan", "ipsec"]`` as "vxlan over ipsec" without re-resolving.
        assert tunnel["stack"][0] == tunnel["type"]
        assert tunnel["depth"] == len(tunnel["stack"]) - 1
        assert ("over" in tunnel) == (tunnel["depth"] > 0)
        # A cleartext tunnel is only "protected" when something above it encrypts.
        assert tunnel["protected"] == (tunnel["encrypted"] or "encryptedBy" in tunnel)

    nested = [node["tunnel"] for node in tunnels if node["tunnel"]["depth"] > 0]
    assert nested, "the overlay example exists to exercise nesting"
    for tunnel in nested:
        assert f"tunnel:{tunnel['over']}" in {node["id"] for node in tunnels}

    encapsulation = [edge for edge in payload["edges"] if edge["kind"] == "encapsulation"]
    assert len(encapsulation) == len(nested)


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


@pytest.mark.parametrize("case", MERMAID_CASES, ids=lambda case: case.name)
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
        for format in case.formats
        if not case.golden(format).exists()
    ]
    assert missing == []


def test_no_stray_golden_files() -> None:
    """Every committed golden belongs to a case, so renames leave no orphans."""
    expected = {case.golden(format) for case in CASES for format in case.formats}
    actual = {path for path in GOLDEN_DIR.iterdir() if path.suffix != ".md"}
    assert actual == expected


def test_goldens_are_free_of_machine_specific_paths() -> None:
    """A golden carrying an absolute path would fail on another checkout."""
    for case in CASES:
        for format in case.formats:
            text = case.golden(format).read_text(encoding="utf-8")
            assert str(REPO_ROOT) not in text
            assert "/home/" not in text
            assert "/root/" not in text
