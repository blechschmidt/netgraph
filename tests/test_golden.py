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
from dataclasses import dataclass
from pathlib import Path

import pytest

from netgraph.fsio import write_text
from netgraph.loader import Inventory, load_tree
from netgraph.render import (
    GRAPH_KIND,
    AggregateSpec,
    BundleMode,
    Layer,
    LinkTemplate,
    RenderOptions,
    aggregate_graph,
    build_graph,
    load_theme,
    render_text,
    suffix_for,
)
from netgraph.render.dot import find_dot

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden"

#: The text formats a golden is kept for.
FORMATS = ("dot", "mermaid", "json")

#: The stylesheet the ``styled`` fixture is drawn with (§22). Loaded from the
#: tree it styles rather than declared here, so the golden pins down the file a
#: reader can open beside it.
STYLED_THEME = load_theme(str(FIXTURES / "styled" / "theme.yaml"))


@dataclass(frozen=True)
class Case:
    """One inventory rendered under one set of display options."""

    #: Stem of the golden file, e.g. ``campus-l2-grouped``.
    name: str
    #: Directory under ``examples/`` — or, with :attr:`fixture` set, under
    #: ``tests/fixtures/``.
    example: str
    layer: Layer
    options: RenderOptions
    #: Which formats this case is kept for. Options that only one backend
    #: honours — tooltips, links, element ids — would otherwise commit two more
    #: byte-identical copies of a Mermaid and a JSON golden per case.
    formats: tuple[str, ...] = FORMATS
    #: The aggregation applied between building the graph and rendering it, as
    #: ``netgraph render`` applies it. ``None`` renders the graph as built,
    #: which is what every case predating ``--collapse`` does.
    aggregate: AggregateSpec | None = None
    #: Is ``example`` a test fixture rather than a published example? A tree
    #: that exists only to exercise a transform — one four-member LAG and two
    #: spare cross-links — teaches nobody anything, so it does not belong in
    #: ``examples/``.
    fixture: bool = False

    @property
    def root(self) -> Path:
        return (FIXTURES if self.fixture else EXAMPLES) / self.example

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
        # The control plane: an iBGP mesh, six OSPF adjacencies nobody declared,
        # and the management VRF as a cluster. ``group_by_namespace`` is on to
        # pin down that the layer's own grouping wins over it (§16.8).
        name="campus-routing",
        example="campus",
        layer=Layer.ROUTING,
        options=RenderOptions(group_by_namespace=True, title="Campus, routing"),
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
    Case(
        # The site-level overview: three sites, three backbone fibres, and the
        # sixteen links inside them counted rather than drawn. ``element_ids``
        # is on because a collapsed node has to be as addressable as a real one
        # — that is the property the entry in the README depends on.
        name="campus-l1-collapsed",
        example="campus",
        layer=Layer.L1,
        options=RenderOptions(title="Campus, collapsed to sites", element_ids=True),
        aggregate=AggregateSpec(collapse_depth=1),
    ),
    Case(
        # One namespace collapsed by name, the rest drawn in full: the mixed
        # graph, where an aggregate node and the devices it does *not* stand for
        # share a diagram.
        name="campus-l1-collapsed-north",
        example="campus",
        layer=Layer.L1,
        options=RenderOptions(show_ips=False, show_vlans=False),
        aggregate=AggregateSpec(collapse=("sites/north",)),
        formats=("dot", "json"),
    ),
    Case(
        # Appearance, with nothing to say about it: the same tree the themed case
        # below draws, with every style block in it ignored. The two goldens
        # differ by exactly what §22 does, which is what makes the pair worth
        # keeping rather than either one alone.
        name="styled-l1-plain",
        example="styled",
        layer=Layer.L1,
        options=RenderOptions(title="Styled, unstyled", styling=False),
        fixture=True,
    ),
    Case(
        # Every rung of the ladder at once: an element that wins outright, one
        # that sets a single field and inherits the rest, two switches told apart
        # only by a role, a namespace rule, a label rule, a styled cable, and an
        # opacity that reaches the output as an alpha pair on a colour.
        name="styled-l1-themed",
        example="styled",
        layer=Layer.L1,
        options=RenderOptions(title="Styled, themed", theme=STYLED_THEME),
        fixture=True,
    ),
    Case(
        # The default: a declared four-member LAG drawn as one edge, the two
        # spare cross-links beside it drawn as two, because nothing in the
        # inventory says they are one link.
        name="aggregate-l1-lag",
        example="aggregate",
        layer=Layer.L1,
        options=RenderOptions(title="LAG bundled by default"),
        aggregate=AggregateSpec(),
        fixture=True,
    ),
    Case(
        # ``--bundle-links``: every parallel link folded, so the six east-west
        # cables become one edge that is deliberately *not* called a LAG.
        name="aggregate-l1-bundled",
        example="aggregate",
        layer=Layer.L1,
        options=RenderOptions(title="Every parallel link bundled"),
        aggregate=AggregateSpec(bundle=BundleMode.ALL),
        fixture=True,
    ),
    Case(
        # ``--no-bundle-links``: eight cables, eight edges. The behaviour every
        # release before this one had, kept reachable and kept pinned.
        name="aggregate-l1-unbundled",
        example="aggregate",
        layer=Layer.L1,
        options=RenderOptions(title="Nothing bundled"),
        aggregate=AggregateSpec(bundle=BundleMode.NONE),
        fixture=True,
        formats=("dot", "json"),
    ),
    Case(
        # Both transforms at once, which is the combination a large tree is
        # rendered with: the sites collapse, and the links that only became
        # parallel *because* they collapsed are folded by the second pass.
        name="aggregate-l1-collapsed-bundled",
        example="aggregate",
        layer=Layer.L1,
        options=RenderOptions(title="Collapsed and bundled", group_by_namespace=True),
        aggregate=AggregateSpec(collapse_depth=1, bundle=BundleMode.ALL),
        fixture=True,
        formats=("dot", "json"),
    ),
    Case(
        # The cabling record: the patch panels are nodes and each segment of a
        # run is an edge of its own. This is the only layer that draws them.
        name="patch-room-physical",
        example="patch-room",
        layer=Layer.PHYSICAL,
        options=RenderOptions(title="Patch room, cabling"),
    ),
    Case(
        # The same inventory with every run spliced into the one link it
        # electrically is. Held beside the case above so a change to the splice
        # shows up as a diff between two files a reader can compare.
        name="patch-room-l1",
        example="patch-room",
        layer=Layer.L1,
        options=RenderOptions(title="Patch room, topology"),
    ),
    Case(
        name="patch-room-l2",
        example="patch-room",
        layer=Layer.L2,
        options=RenderOptions(),
        formats=("dot", "json"),
    ),
    Case(
        # The elevations. Mermaid cannot express a grid and is refused by the
        # backend (``RenderError``), which is asserted separately.
        name="patch-room-rack",
        example="patch-room",
        layer=Layer.RACK,
        options=RenderOptions(title="Patch room, elevations"),
        formats=("dot", "json"),
    ),
    Case(
        # The power distribution (§17.5): the PDUs, everything they feed, and the
        # two PoE runs -- one of them across a patch panel, which is the case the
        # feed walk exists for. Held in all three formats because a power feed is
        # an ordinary edge and every backend has to draw one.
        name="patch-room-power",
        example="patch-room",
        layer=Layer.POWER,
        options=RenderOptions(title="Patch room, power"),
    ),
    Case(
        # A stored arrangement (§18), fully placed: every node carries a ``pos``,
        # every link its spline control points, and the document asks for the
        # no-op layout engine. This golden is what makes the arrangement a
        # *contract* — a change to how geometry is emitted shows up as a diff in
        # coordinates a reader can check against ``layout.yaml`` by eye.
        name="arranged-l1-fixed",
        example="arranged",
        layer=Layer.L1,
        options=RenderOptions(),
        formats=("dot", "json"),
        fixture=True,
    ),
    Case(
        # The same tree's other view, which carries group boxes instead of
        # waypoints. ``neato`` draws no clusters, so the frames are emitted as a
        # ``_background`` of xdot operations; nothing else in the matrix pins
        # that down, and getting it wrong loses every namespace frame silently.
        name="arranged-l2-grouped",
        example="arranged",
        layer=Layer.L2,
        options=RenderOptions(group_by_namespace=True, title="Arranged, layer 2"),
        formats=("dot", "json"),
        fixture=True,
    ),
    Case(
        # An orthogonal, waypointed diagram (§18): right angles everywhere
        # except one link that asks for a straight line, a trunk dragged through
        # two hand-placed bends, and a label nudged off it. Between them these
        # cover every part of a link's geometry that reaches the DOT — the
        # computed ``pos``, the per-link override, and the ``lp`` that pins a
        # label — and they are *typed* numbers rather than seeded ones, so this
        # golden moves only when the emission changes.
        name="routed-l1-orthogonal",
        example="routed",
        layer=Layer.L1,
        options=RenderOptions(),
        formats=("dot", "json"),
        fixture=True,
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
    """Every tree the matrix renders, loaded once for the whole session."""
    loaded = {}
    for case in CASES:
        if case.example in loaded:
            continue
        inventory = load_tree(case.root)
        assert inventory.errors == [], f"{case.example} does not load cleanly: {inventory.errors}"
        loaded[case.example] = inventory
    return loaded


def _render(case: Case, inventories: dict[str, Inventory], format: str) -> str:
    graph = build_graph(inventories[case.example], layer=case.layer)
    # The same order ``netgraph render`` uses: resolve, then summarise, then
    # draw. A golden produced any other way would pin a pipeline nobody runs.
    graph = aggregate_graph(graph, case.aggregate)
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
        # ``netgraph.fsio.write_text`` rather than ``Path.write_text``: a golden
        # is a byte-for-byte artefact, and regenerating one on Windows through
        # Python's text mode would rewrite every line ending in the file. See
        # ``.gitattributes``, which keeps the committed copy at LF for the same
        # reason.
        write_text(golden, actual)
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


def test_a_rebuilt_collapsed_graph_renders_identically(inventories: dict[str, Inventory]) -> None:
    """Aggregation groups by namespace and by node pair, so it must not use sets loosely."""
    case = CASES_BY_NAME["campus-l1-collapsed"]
    inventory = inventories[case.example]
    renders = {
        render_text(
            aggregate_graph(build_graph(inventory, layer=case.layer), case.aggregate),
            "dot",
            case.options,
        )
        for _ in range(2)
    }
    assert len(renders) == 1


def test_reloading_the_inventory_reproduces_the_golden() -> None:
    """A second load from disk yields the same bytes.

    The load order of a directory tree drives node order, so this catches a
    renderer that depends on the order the filesystem happened to return.
    """
    case = CASES_BY_NAME["home-lab-l1"]
    inventory = load_tree(case.root)
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
    if not find_dot():
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
    if case.layer is not Layer.RACK:
        # An elevation has no edges by construction: a cable between two boxes
        # says nothing about where either one is bolted.
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
    types = {node["type"] for node in payload["nodes"]}
    assert types <= {"element", "subnet", "tunnel", "aggregate", "rack"}
    if case.layer is Layer.L3:
        assert any(node["type"] == "subnet" for node in payload["nodes"])
    elif case.layer is Layer.OVERLAY:
        assert any(node["type"] == "tunnel" for node in payload["nodes"])
    elif case.layer is Layer.RACK:
        # An elevation holds racks and nothing else: the elements are inside
        # them, as slots, rather than beside them as nodes.
        assert types == {"rack"}
        assert all(node["rack"]["slots"] for node in payload["nodes"])
    else:
        # Below the overlay layer a tunnel is an edge, unless it joins more than
        # two endpoints and has no line shape to take. A collapsed namespace is
        # a node at every layer.
        assert types <= {"element", "tunnel", "aggregate"}
    collapses = case.aggregate is not None and case.aggregate.collapses
    assert ("aggregate" in types) == collapses


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
        # A routing instance is part of the identity: two VRFs may hold one
        # prefix, and the two nodes have to stay distinguishable (§16.1).
        instance = f"{subnet['vrf']}/" if subnet.get("vrf") else ""
        suffix = f" (vrf {subnet['vrf']})" if subnet.get("vrf") else ""
        assert node["id"] == f"subnet:{instance}{subnet['prefix']}"
        assert node["name"] == f"{subnet['prefix']}{suffix}"
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


#: The cases whose graph was summarised rather than drawn in full.
AGGREGATE_CASES = tuple(
    case
    for case in CASES
    if case.aggregate is not None and case.aggregate.collapses and "json" in case.formats
)

#: The cases whose graph folds parallel links into one edge.
BUNDLE_CASES = tuple(
    case for case in CASES if case.name.startswith("aggregate-") and "json" in case.formats
)


@pytest.mark.parametrize("case", AGGREGATE_CASES, ids=lambda case: case.name)
def test_the_collapsed_json_golden_says_what_each_box_stands_for(case: Case) -> None:
    """A consumer must be able to get from one box back to the devices behind it."""
    payload = json.loads(case.golden("json").read_text(encoding="utf-8"))
    aggregates = [node for node in payload["nodes"] if node["type"] == "aggregate"]
    assert aggregates, "an aggregate case with no aggregate node would assert nothing"

    ids = {node["id"] for node in payload["nodes"]}
    drawn = {edge["id"] for edge in payload["edges"]}
    for node in aggregates:
        assert node["kind"] == "namespace"
        assert node["interfaces"] == []
        summary = node["aggregate"]
        assert node["id"] == f"ns:{summary['namespace']}"
        assert node["name"] == summary["namespace"]
        # The elements it stands for are gone from the document — that is what
        # collapsing *is* — so they must be named, and named in full.
        assert summary["elements"]
        assert not (set(summary["elements"]) & ids)
        assert summary["elementCount"] == len(summary["elements"])
        assert sum(summary["countsByKind"].values()) == len(summary["elements"])
        # And so must the links that vanished inside it, or a reader counting
        # cables on the page would conclude the site has none.
        assert not (set(summary["internalLinks"]) & drawn)


@pytest.mark.parametrize("case", BUNDLE_CASES, ids=lambda case: case.name)
def test_the_bundled_json_golden_lists_every_link_it_folded(case: Case) -> None:
    """A bundle is a summary of links, not a link: it has to name its members."""
    payload = json.loads(case.golden("json").read_text(encoding="utf-8"))
    bundles = [edge for edge in payload["edges"] if "bundle" in edge]
    if case.aggregate is not None and case.aggregate.bundle is BundleMode.NONE:
        assert bundles == [], "--no-bundle-links must draw every link as itself"
        return
    assert bundles, "a bundling case with no bundle would assert nothing"

    ids = {node["id"] for node in payload["nodes"]}
    for edge in bundles:
        bundle = edge["bundle"]
        assert bundle["size"] == len(bundle["links"]) >= 2
        assert edge["id"] == f"{bundle['links'][0]['id']}#bundle"
        # A member is exported exactly as an unbundled link is, and is never
        # itself a bundle: folding flattens.
        for link in bundle["links"]:
            assert "bundle" not in link
            assert {end["node"] for end in link["endpoints"]} <= ids
        # The drawn rate is the sum of what was folded in: four 1G cables are
        # 4G, which is the whole reason an operator builds a LAG.
        assert edge["speed"] == sum(link["speed"] for link in bundle["links"])


@pytest.mark.parametrize("case", MERMAID_CASES, ids=lambda case: case.name)
def test_the_mermaid_golden_declares_a_flowchart(case: Case) -> None:
    text = case.golden("mermaid").read_text(encoding="utf-8")
    assert re.search(r"^flowchart ", text, re.MULTILINE)

    # Identifiers are positional (n0, n1, …) because a fully-qualified name is
    # not a legal Mermaid id. They must be dense and start at zero, or the
    # ``class nN kind`` lines at the foot would style the wrong nodes. They are
    # *numbered* in graph order rather than in emission order, so a layer that
    # groups its nodes — a VRF cluster at ``--layer routing``, §16.8 — declares
    # them out of sequence; what has to hold is that the set is exactly n0..nN.
    declared = re.findall(r"^ +(n\d+)[(\[>{]", text, re.MULTILINE)
    assert sorted(declared, key=lambda name: int(name[1:])) == [
        f"n{index}" for index in range(len(declared))
    ]

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
    # ``path/`` holds the trace snapshots, which ``tests/test_path.py`` owns and
    # guards the same way.
    actual = {path for path in GOLDEN_DIR.iterdir() if path.is_file() and path.suffix != ".md"}
    assert actual == expected


def test_goldens_are_free_of_machine_specific_paths() -> None:
    """A golden carrying an absolute path would fail on another checkout."""
    for case in CASES:
        for format in case.formats:
            text = case.golden(format).read_text(encoding="utf-8")
            assert str(REPO_ROOT) not in text
            assert "/home/" not in text
            assert "/root/" not in text
