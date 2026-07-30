"""What a rendering carries besides the picture: tooltips, links and ids.

Three attributes travel with a diagram and none of them changes it: the hover
text of :mod:`netgraph.render.details`, the ``URL`` that ``--link-template``
builds, and the stable ``id`` of :mod:`netgraph.render.ids`. They are asserted
at three layers, because each one can fail on its own:

* the **builder** — the records and the text derived from them, with no
  renderer involved;
* the **DOT source** — the attributes are emitted, quoted and bounded;
* the **SVG Graphviz produces** — the layer that decides whether any of it
  reaches a reader, and the only one that can prove an id survived
  Graphviz's own escaping.

The escaping of hostile content is asserted at all three too, next to the
existing label tests in ``test_render.py``: a tooltip is a second place an
inventory's text reaches a published document, and it is the place a reader
trusts most.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from xml.etree import ElementTree

import pytest

from netgraph.errors import RenderError
from netgraph.loader import Inventory, load_tree
from netgraph.render import (
    Graph,
    Layer,
    LinkTemplate,
    RenderOptions,
    build_details,
    build_graph,
    detail_text,
    element_ids,
    render_text,
    supports_interaction,
    to_dot,
    to_html,
    to_image,
)
from netgraph.render.details import MAX_DETAIL_LENGTH
from netgraph.render.ids import slug

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

requires_dot = pytest.mark.skipif(
    shutil.which("dot") is None, reason="the Graphviz 'dot' executable is not installed"
)

#: A template using every placeholder there is, so one expansion exercises them all.
EVERY_FIELD = "https://git.example.invalid/{namespace}/{kind}/{file}#L{line}?n={name}"


@pytest.fixture(scope="module")
def home_lab() -> Inventory:
    inventory = load_tree(EXAMPLES / "home-lab")
    assert inventory.errors == []
    return inventory


@pytest.fixture(scope="module")
def overlay() -> Inventory:
    inventory = load_tree(EXAMPLES / "overlay")
    assert inventory.errors == []
    return inventory


def _without_tooltips(source: str) -> str:
    """``source`` with every tooltip attribute removed, however it was written."""
    stripped = re.sub(r', tooltip="(?:[^"\\]|\\.)*"', "", source)
    return re.sub(r'^ *tooltip="(?:[^"\\]|\\.)*";\n', "", stripped, flags=re.MULTILINE)


def _attributes(source: str, name: str) -> list[str]:
    """Every value of the ``name="..."`` attribute in a DOT source."""
    return re.findall(rf'\b{name}="((?:[^"\\]|\\.)*)"', source)


def _statement(source: str, fqn: str) -> str:
    """The attribute list of the node statement declaring ``fqn``."""
    line = next(line for line in source.splitlines() if line.strip().startswith(f'"{fqn}" ['))
    return line


# --------------------------------------------------------------------------- #
# Tooltips: the same records the web preview shows
# --------------------------------------------------------------------------- #


def test_every_node_and_edge_carries_a_tooltip(home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    source = to_dot(graph)
    # One per node, one per edge; no other construct emits one without a cluster.
    assert len(_attributes(source, "tooltip")) == len(graph.nodes) + len(graph.edges)


def test_a_tooltip_is_the_detail_record_of_the_element_it_is_on(home_lab: Inventory) -> None:
    """The tooltip and the info box must not be able to drift apart."""
    graph = build_graph(home_lab)
    options = RenderOptions()
    details = build_details(graph, options, ids=element_ids(graph))
    ids = element_ids(graph)

    source = to_dot(graph, options)
    record = details[ids.nodes["switches/sw-home"]]
    expected = detail_text(record).replace("\\", "\\\\").replace("\n", "\\n")
    assert expected in _statement(source, "switches/sw-home")


def test_a_node_tooltip_says_what_the_label_had_no_room_for(home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    (tooltip,) = [
        text
        for text in _attributes(to_dot(graph), "tooltip")
        if text.startswith("sw-home [switch]")
    ]
    # The identity, the prose, the ports the label leaves out, and what the
    # element is cabled to — none of which the drawn record table holds.
    assert "namespace: switches" in tooltip
    assert "Eight-port desktop switch in the hallway cabinet" in tooltip
    assert "interfaces (7):" in tooltip
    assert "port1  ethernet  vlan 10 (access)  00:22:07:aa:00:01" in tooltip
    assert "port1 — routers/rtr-home:lan0  (cable, copper, 1Gbps)  vlan 10" in tooltip


def test_an_edge_tooltip_names_both_ends_and_the_medium(home_lab: Inventory) -> None:
    tooltips = _attributes(to_dot(build_graph(home_lab)), "tooltip")
    (cable,) = [text for text in tooltips if text.startswith("cable cables/cbl-rtr-sw")]
    assert "routers/rtr-home:lan0 — switches/sw-home:port1" in cable
    assert "medium: copper, speed: 1Gbps, length: 0.5 m, label: H-001" in cable
    assert "vlans: 10" in cable


def test_a_cluster_tooltip_counts_what_the_box_holds(home_lab: Inventory) -> None:
    source = to_dot(build_graph(home_lab), RenderOptions(group_by_namespace=True))
    assert "namespace hosts\\n5 elements: 1 adapter, 3 computers, 1 server" in source


def test_a_subnet_tooltip_says_how_populated_the_prefix_is(home_lab: Inventory) -> None:
    source = to_dot(build_graph(home_lab, layer=Layer.L3))
    (subnet,) = [
        text for text in _attributes(source, "tooltip") if text.startswith("192.168.10.0/24")
    ]
    assert "[ipv4 subnet]" in subnet
    assert "prefix 192.168.10.0/24: 7 elements, 7 addresses" in subnet


def test_a_tunnel_tooltip_spells_out_the_stack_and_what_protects_it(overlay: Inventory) -> None:
    source = to_dot(build_graph(overlay, layer=Layer.OVERLAY))
    (nested,) = [text for text in _attributes(source, "tooltip") if text.startswith("vx-100 ")]
    assert "[vxlan tunnel]" in nested
    assert "stack: vxlan over ipsec" in nested
    # A cleartext overlay inside an encrypting underlay is confidential, and the
    # tooltip has to say which of the two it is.
    assert "cleartext, carried by" in nested


def test_no_tooltips_leaves_the_document_with_none(home_lab: Inventory) -> None:
    """The escape hatch for a diagram that must carry nothing but the picture."""
    graph = build_graph(home_lab)
    inert = to_dot(graph, RenderOptions(tooltips=False, group_by_namespace=True))
    assert _attributes(inert, "tooltip") == []
    assert "Eight-port desktop switch" not in inert

    # Only the tooltips went: what is drawn is the same document either way.
    detailed = to_dot(graph, RenderOptions(group_by_namespace=True))
    assert _without_tooltips(detailed) == inert


def test_the_display_flags_reach_the_tooltips(home_lab: Inventory) -> None:
    """``--no-show-ips`` is about printing, and a tooltip is printing."""
    graph = build_graph(home_lab)
    bare = to_dot(graph, RenderOptions(show_ips=False, show_vlans=False))
    full = to_dot(graph, RenderOptions())
    assert "192.168.10.20/24" in "\n".join(_attributes(full, "tooltip"))
    assert "192.168.10.20/24" not in "\n".join(_attributes(bare, "tooltip"))
    assert "vlans: 10" in "\n".join(_attributes(full, "tooltip"))
    assert "vlans: 10" not in "\n".join(_attributes(bare, "tooltip"))


def test_a_tooltip_is_bounded_however_long_the_description_is(tmp_path: Path) -> None:
    """A pop-up that covers the diagram it explains is worse than none."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: server\n"
        f"metadata: {{name: srv, description: {'prose ' * 900}}}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]}\n"
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    (tooltip,) = _attributes(to_dot(build_graph(inventory)), "tooltip")
    # The bound plus the sentence saying what was dropped, and nothing else.
    assert MAX_DETAIL_LENGTH < len(tooltip) < MAX_DETAIL_LENGTH + 64
    assert tooltip.endswith("more characters)")


def test_a_tooltip_counts_off_the_interfaces_it_does_not_list(tmp_path: Path) -> None:
    interfaces = "".join(f"    - {{name: port{index}, type: ethernet}}\n" for index in range(1, 21))
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-big}\n"
        "spec:\n  interfaces:\n" + interfaces
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    (tooltip,) = _attributes(to_dot(build_graph(inventory)), "tooltip")
    assert "interfaces (20):" in tooltip
    assert "(+12 more)" in tooltip
    assert "port9" not in tooltip


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


def test_a_template_expands_from_the_document_the_element_came_from(
    home_lab: Inventory,
) -> None:
    source = to_dot(
        build_graph(home_lab), RenderOptions(link_template=LinkTemplate.parse(EVERY_FIELD))
    )
    assert (
        "https://git.example.invalid/switches/switch/switches/sw-home.yaml"
        "#L1?n=switches/sw-home" in _statement(source, "switches/sw-home")
    )


def test_each_cable_links_to_the_line_that_declares_it(home_lab: Inventory) -> None:
    """Six cables in one file, six different lines."""
    template = LinkTemplate.parse("{file}#L{line}")
    urls = _attributes(to_dot(build_graph(home_lab), RenderOptions(link_template=template)), "URL")
    cables = sorted({url for url in urls if url.startswith("cables/links.yaml")})
    assert len(cables) == 6
    assert cables[0] != cables[1]


def test_a_derived_subnet_links_nowhere(home_lab: Inventory) -> None:
    """There is no file that says ``192.168.10.0/24``; a 404 is worse than nothing."""
    template = LinkTemplate.parse("{file}")
    source = to_dot(build_graph(home_lab, layer=Layer.L3), RenderOptions(link_template=template))
    assert "URL=" not in _statement(source, "subnet:192.168.10.0/24")
    # The membership edges are derived too …
    subnet_edges = [line for line in source.splitlines() if ' -- "subnet:' in line]
    assert subnet_edges and not any("URL=" in line for line in subnet_edges)
    # … while the elements they join are not.
    assert "URL=" in _statement(source, "routers/rtr-home")


def test_a_tunnel_links_to_the_tunnel_document(overlay: Inventory) -> None:
    template = LinkTemplate.parse("{file}#{name}")
    source = to_dot(
        build_graph(overlay, layer=Layer.OVERLAY), RenderOptions(link_template=template)
    )
    assert "URL=" in _statement(source, "tunnel:tunnels/vx-100")
    assert "tunnels/vx-100" in _statement(source, "tunnel:tunnels/vx-100")


def test_without_a_template_nothing_is_linked(home_lab: Inventory) -> None:
    assert "URL=" not in to_dot(build_graph(home_lab))


@pytest.mark.parametrize(
    ("template", "reason"),
    [
        ("https://x/{author}", "unknown --link-template placeholder {author}"),
        ("https://x/{}", "does not take positional placeholders"),
        ("https://x/{0}", "unknown --link-template placeholder {0}"),
        ("https://x/{name.upper}", "may not index or attribute-access"),
        ("https://x/{name[0]}", "may not index or attribute-access"),
        ("https://x/{name!r}", "may not carry a conversion or a format spec"),
        ("https://x/{line:04d}", "may not carry a conversion or a format spec"),
        ("https://x/{file", "is not a valid format string"),
    ],
)
def test_a_template_that_would_produce_a_broken_link_is_refused(template: str, reason: str) -> None:
    """Rejected when it is parsed, not once a file full of dead links exists."""
    with pytest.raises(RenderError) as caught:
        LinkTemplate.parse(template)
    assert reason in str(caught.value)


def test_an_unknown_placeholder_names_the_ones_that_exist() -> None:
    with pytest.raises(RenderError) as caught:
        LinkTemplate.parse("https://x/{path}")
    for field in ("{file}", "{line}", "{name}", "{namespace}", "{kind}"):
        assert field in str(caught.value)


def test_a_literal_brace_is_not_a_placeholder() -> None:
    template = LinkTemplate.parse("https://x/{{literal}}/{name}")
    assert template.fields == {"name"}
    assert template.expand(name="a/b") == "https://x/{literal}/a/b"


def test_the_substituted_values_are_url_encoded() -> None:
    """The template is the operator's; the values are the inventory's."""
    template = LinkTemplate.parse("https://x/{file}?n={name}")
    expanded = template.expand(file="dir/a b.yaml", name='pc-"a"#frag‮\U0001f600')
    assert expanded == ("https://x/dir/a%20b.yaml?n=pc-%22a%22%23frag%E2%80%AE%F0%9F%98%80")
    # A path separator is structure, not content: encoding it would break the URL.
    assert template.expand(file="a/b.yaml", name="x") == "https://x/a/b.yaml?n=x"


def test_a_value_the_element_does_not_have_produces_no_link() -> None:
    template = LinkTemplate.parse("https://x/{file}#L{line}")
    assert template.expand(file="a.yaml", line=None) is None
    assert template.expand(file=None, line=3) is None
    assert template.expand(file="a.yaml", line=3) == "https://x/a.yaml#L3"


def test_the_root_namespace_is_a_value_rather_than_a_missing_one() -> None:
    """An element at the top of the tree is in the root namespace, not nowhere."""
    template = LinkTemplate.parse("https://x/{namespace}/{name}")
    assert template.expand(namespace="", name="pc") == "https://x//pc"


# --------------------------------------------------------------------------- #
# Element ids
# --------------------------------------------------------------------------- #


def test_ids_are_derived_from_the_name_not_from_a_position(home_lab: Inventory) -> None:
    ids = element_ids(build_graph(home_lab))
    assert ids.nodes["switches/sw-home"] == "node-switches_sw-home"
    assert ids.clusters["switches"] == "cluster-switches"
    assert "edge-cables_cbl-rtr-sw" in ids.edges


def test_an_id_survives_a_new_element_appearing_before_it(
    home_lab: Inventory, tmp_path: Path
) -> None:
    """A bookmark in a wiki must not move because someone added a device."""
    before = element_ids(build_graph(home_lab)).nodes["switches/sw-home"]

    for path in sorted((EXAMPLES / "home-lab").rglob("*.yaml")):
        target = tmp_path / path.relative_to(EXAMPLES / "home-lab")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "aaa-new.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-new}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet, ipv4: [10.9.9.9/24]}]}\n"
    )
    grown = load_tree(tmp_path)
    assert grown.errors == []
    assert element_ids(build_graph(grown)).nodes["switches/sw-home"] == before


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sites/hq/sw-core", "sites_hq_sw-core"),
        ("subnet:192.168.10.0/24", "subnet_192.168.10.0_24"),
        ('pc-"quoted"-{braced}', "pc-_quoted_-_braced"),
        ("pc‮evil", "pc_evil"),
        ("pc\U0001f600", "pc"),
    ],
)
def test_a_slug_holds_nothing_an_xml_id_may_not(name: str, expected: str) -> None:
    assert slug(name) == expected
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", slug(name))


def test_a_name_of_nothing_but_unsafe_characters_still_gets_an_id() -> None:
    assert re.fullmatch(r"[0-9a-f]+", slug("///"))
    assert slug("///") != slug("////")


def test_a_very_long_name_is_truncated_but_stays_distinct() -> None:
    first, second = slug("x" * 300 + "a"), slug("x" * 300 + "b")
    assert len(first) < 120 and first != second


def test_two_names_that_slug_alike_get_different_ids(tmp_path: Path) -> None:
    """``a/b`` and ``a_b`` are two elements and must stay two ids."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: a_b}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet}]}\n"
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: b}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet}]}\n"
    )
    inventory = load_tree(tmp_path)
    assert inventory.errors == []
    ids = element_ids(build_graph(inventory))
    assert set(ids.nodes) == {"a_b", "a/b"}
    assert len(set(ids.nodes.values())) == 2


def test_the_ids_of_a_document_are_unique(home_lab: Inventory) -> None:
    ids = element_ids(build_graph(home_lab, layer=Layer.L3))
    everything = [*ids.nodes.values(), *ids.edges, *ids.clusters.values()]
    assert len(everything) == len(set(everything))


def test_ids_are_off_unless_asked_for(home_lab: Inventory) -> None:
    assert "id=" not in to_dot(build_graph(home_lab))


# --------------------------------------------------------------------------- #
# The SVG Graphviz produces: the layer that decides whether any of it lands
# --------------------------------------------------------------------------- #


@requires_dot
def test_the_ids_survive_graphviz(home_lab: Inventory) -> None:
    """Graphviz escapes ``-`` as ``&#45;``; an XML parser is what proves it back."""
    graph = build_graph(home_lab)
    options = RenderOptions(element_ids=True, group_by_namespace=True)
    document = ElementTree.fromstring(to_image(graph, options, format="svg"))
    ids = element_ids(graph)

    drawn = {element.get("id") for element in document.iter() if element.get("id")}
    assert set(ids.nodes.values()) <= drawn
    assert set(ids.edges) <= drawn
    assert set(ids.clusters.values()) <= drawn


@requires_dot
def test_a_tooltip_becomes_the_title_a_browser_pops_up(home_lab: Inventory) -> None:
    """``xlink:title`` is not what browsers show; ``<title>`` is."""
    svg = to_image(build_graph(home_lab), RenderOptions(element_ids=True), format="svg")
    document = ElementTree.fromstring(svg)
    titles = {
        element.findtext("{http://www.w3.org/2000/svg}title", default="")
        for element in document.iter()
        if element.get("class") in {"node", "edge"}
    }
    assert any(title.startswith("sw-home [switch]") for title in titles)
    assert any("interfaces (7):" in title for title in titles)
    # The internal identity Graphviz would otherwise have put there is gone.
    assert "switches/sw-home" not in titles


@requires_dot
def test_a_diagram_without_tooltips_keeps_graphvizs_own_titles(home_lab: Inventory) -> None:
    svg = to_image(build_graph(home_lab), RenderOptions(tooltips=False), format="svg").decode()
    assert "<title>switches/sw&#45;home</title>" in svg


@requires_dot
def test_a_link_becomes_an_anchor(home_lab: Inventory) -> None:
    options = RenderOptions(link_template=LinkTemplate.parse("https://git.invalid/{file}"))
    document = ElementTree.fromstring(to_image(build_graph(home_lab), options, format="svg"))
    hrefs = {
        element.get("{http://www.w3.org/1999/xlink}href")
        for element in document.iter("{http://www.w3.org/2000/svg}a")
    }
    assert "https://git.invalid/switches/sw-home.yaml" in hrefs


@requires_dot
def test_a_png_carries_none_of_it(home_lab: Inventory) -> None:
    """The attributes are inert in a raster format rather than an error."""
    options = RenderOptions(
        element_ids=True, link_template=LinkTemplate.parse("https://git.invalid/{file}")
    )
    payload = to_image(build_graph(home_lab), options, format="png")
    assert payload.startswith(b"\x89PNG")
    assert b"git.invalid" not in payload


# --------------------------------------------------------------------------- #
# Which format honours which flag
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("format", "carries"),
    [
        ("dot", True),
        ("svg", True),
        ("png", False),
        ("pdf", False),
        ("mermaid", False),
        ("json", False),
    ],
)
def test_the_registry_knows_which_formats_carry_the_attributes(format: str, carries: bool) -> None:
    assert supports_interaction(format) is carries


def test_an_unknown_format_carries_nothing_rather_than_raising() -> None:
    assert supports_interaction("ascii-art") is False


@pytest.mark.parametrize("format", ["mermaid", "json"])
def test_the_other_text_backends_ignore_the_options(format: str, home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    options = RenderOptions(
        element_ids=True,
        tooltips=False,
        link_template=LinkTemplate.parse("https://git.invalid/{file}"),
    )
    assert render_text(graph, format, options) == render_text(graph, format, RenderOptions())


# --------------------------------------------------------------------------- #
# Hostile content, in the tooltip context
# --------------------------------------------------------------------------- #

#: Everything a name could carry that a tooltip must not act on: markup, a
#: quote that would close the DOT string, a backslash that would escape what
#: follows, a newline, a right-to-left override and an astral-plane character.
HOSTILE_TEXT = (
    '</TABLE>><script>alert("x")</script>\\ & "quoted"\nsecond line\u0007\u202e\U0001f4a3'
)


def _hostile_record() -> dict[str, object]:
    return {
        "id": HOSTILE_TEXT,
        "type": "element",
        "name": HOSTILE_TEXT,
        "kind": HOSTILE_TEXT,
        "namespace": HOSTILE_TEXT,
        "description": HOSTILE_TEXT,
        "labels": {HOSTILE_TEXT: HOSTILE_TEXT},
        "vlans": [10],
        "interfaces": [{"name": HOSTILE_TEXT, "type": "ethernet", "addresses": [HOSTILE_TEXT]}],
        "links": [
            {"element": "e", "kind": "cable", "interface": HOSTILE_TEXT, "peer": HOSTILE_TEXT}
        ],
    }


def test_the_text_builder_drops_what_cannot_be_printed() -> None:
    """Layer one: control characters and bidi overrides never reach the text."""
    text = detail_text(_hostile_record())
    assert "‮" not in text and "" not in text
    # A description spanning two lines is collapsed onto one, so a name cannot
    # push the rest of the tooltip out of view …
    assert text.count("\n") == len(text.splitlines()) - 1
    assert "second line" in text
    # … and everything printable survives, including the astral-plane character.
    assert "\U0001f4a3" in text
    assert '<script>alert("x")</script>' in text


def _hostile_graph(tmp_path: Path) -> Graph:
    """A one-node graph whose every string is hostile, injected below the loader.

    The schema refuses such a name (§4.1), which is exactly why it is injected
    here: the escaping is the renderer's own responsibility and must not depend
    on a validator a later refactor could move or relax.
    """
    import dataclasses

    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-a}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.1/24]}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-b}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.2/24]}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc-a:eth0, pc-b:eth0], medium: copper, label: plain}\n"
    )
    loaded = build_graph(load_tree(tmp_path))
    left, right = loaded.nodes.values()
    left = dataclasses.replace(
        left,
        fqn=HOSTILE_TEXT,
        name=HOSTILE_TEXT,
        ports=(dataclasses.replace(left.ports[0], name=HOSTILE_TEXT),),
    )
    edge = dataclasses.replace(
        loaded.edges[0], source=left.fqn, target=right.fqn, label=HOSTILE_TEXT
    )
    return Graph(
        root=loaded.root,
        nodes={left.fqn: left, right.fqn: right},
        edges=(edge,),
        layer=loaded.layer,
        sources=loaded.sources,
    )


def test_a_hostile_tooltip_stays_inside_its_dot_string(tmp_path: Path) -> None:
    """Layer two: the quoting of the DOT document holds."""
    source = to_dot(_hostile_graph(tmp_path), RenderOptions(element_ids=True))

    tooltips = _attributes(source, "tooltip")
    assert tooltips, "the graph exists to produce tooltips"
    for tooltip in tooltips:
        # The quote is escaped, the backslash doubled exactly once, and the
        # newline is the two-character DOT escape rather than a real one.
        assert '"' not in tooltip.replace('\\"', "")
        assert "\n" not in tooltip
        assert re.search(r"(?<!\\)\\(?![\\\"n])", tooltip) is None

    # Braces and markup are inert inside the quoting, and never outside it.
    for line in source.splitlines():
        if not line.strip().startswith('"'):
            continue  # a graph attribute or the enclosing braces, written here
        assert _outside_quoted_strings(line).count("{") == 0
        assert _outside_quoted_strings(line).count("}") == 0
    # The label terminator does not appear early: two nodes, two ends.
    assert source.count(">];") == 2


def test_a_hostile_name_cannot_reach_an_element_id(tmp_path: Path) -> None:
    """An id is a second, unescaped copy of a name, so it holds ASCII only."""
    source = to_dot(_hostile_graph(tmp_path), RenderOptions(element_ids=True))
    for value in _attributes(source, "id"):
        assert re.fullmatch(r"(?:node|edge|cluster)-[A-Za-z0-9_.-]+", value), value


def _outside_quoted_strings(line: str) -> str:
    """``line`` with every DOT quoted string removed, escapes respected."""
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(line):
        char = line[index]
        if in_string and char == "\\":
            index += 2
            continue
        if char == '"':
            in_string = not in_string
        elif not in_string:
            out.append(char)
        index += 1
    assert not in_string, f"unbalanced quoting in {line!r}"
    return "".join(out)


@requires_dot
def test_graphviz_renders_a_hostile_tooltip_as_text_rather_than_markup(tmp_path: Path) -> None:
    """Layer three: the artefact a browser opens, parsed as the XML it claims to be."""
    payload = to_image(
        _hostile_graph(tmp_path),
        RenderOptions(
            element_ids=True, link_template=LinkTemplate.parse("https://git.invalid/{name}")
        ),
        format="svg",
    )
    document = ElementTree.fromstring(payload)  # unparseable XML fails here

    titles = [element.text or "" for element in document.iter("{http://www.w3.org/2000/svg}title")]
    assert any('<script>alert("x")</script>' in title for title in titles), (
        "the text must survive as text"
    )
    # … and never as markup: the document holds no script element at all.
    assert not list(document.iter("{http://www.w3.org/2000/svg}script"))
    assert b"<script" not in payload

    # The link built from the same hostile name is percent-encoded, so nothing
    # in it can close the attribute or introduce a second one.
    hrefs = [
        element.get("{http://www.w3.org/1999/xlink}href", "")
        for element in document.iter("{http://www.w3.org/2000/svg}a")
    ]
    assert any(href.startswith("https://git.invalid/") for href in hrefs)
    for href in hrefs:
        assert '"' not in href and "<" not in href and " " not in href


# --------------------------------------------------------------------------- #
# Hostile content, in the HTML page
# --------------------------------------------------------------------------- #
#
# A fourth layer, and the one with the most ways to go wrong: an HTML document
# has an element context, an attribute context, a ``<style>`` context and a
# ``<script>`` context, and the last two end at a literal ``</script>`` or
# ``</style>`` whatever the JSON or the CSS around them says.


def _hostile_inventory(root: Path) -> Inventory:
    """An inventory whose *description* is hostile, written as YAML would carry it.

    The name is dealt with by ``_hostile_graph`` below the loader; a description
    is free text the schema accepts, so it can be written into a document and
    take the whole loader with it — which is the path a real inventory would
    take.
    """
    import yaml

    document = {
        "apiVersion": "netgraph.dev/v1alpha1",
        "kind": "computer",
        "metadata": {"name": "pc-a", "description": HOSTILE_TEXT},
        "spec": {"interfaces": [{"name": "eth0", "type": "ethernet", "ipv4": ["10.0.0.1/24"]}]},
    }
    (root / "inv.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True)
        + "---\n"
        + "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-b}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet, ipv4: [10.0.0.2/24]}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc-a:eth0, pc-b:eth0], medium: copper, label: plain}\n",
        encoding="utf-8",
    )
    inventory = load_tree(root)
    assert not inventory.errors, inventory.errors
    return inventory


def _elements(source: str) -> tuple[list[str], list[tuple[str, str, str | None]]]:
    """Every element and attribute of ``source``, as a browser's parser sees them."""
    from html.parser import HTMLParser

    class _Reader(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.tags: list[str] = []
            self.attributes: list[tuple[str, str, str | None]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.tags.append(tag)
            self.attributes.extend((tag, name, value) for name, value in attrs)

        handle_startendtag = handle_starttag

    reader = _Reader()
    reader.feed(source)
    return reader.tags, reader.attributes


@requires_dot
def test_a_hostile_name_cannot_add_an_element_to_the_html_page(tmp_path: Path) -> None:
    """Layer four: the page, parsed as the HTML it claims to be."""
    page = to_html(
        _hostile_graph(tmp_path),
        RenderOptions(
            title=HOSTILE_TEXT, link_template=LinkTemplate.parse("https://x.invalid/{name}")
        ),
    )
    tags, attributes = _elements(page)

    # The hostile text holds ``<script>`` and ``</TABLE>>``; the document holds
    # exactly the elements this renderer wrote.
    assert tags.count("script") == 2, "the client and the records, and nothing else"
    assert tags.count("style") == 1
    assert not {"iframe", "object", "embed", "img", "form", "base"} & set(tags)
    # No attribute anywhere can run anything.
    assert not [name for _, name, _ in attributes if name.startswith("on")]
    # The ``</script>`` in the name never appears as one: two elements, two
    # closing tags, and the copies inside the JSON are \u-escaped.
    assert page.count("</script>") == 2
    assert page.count("</style>") == 1


@requires_dot
def test_a_hostile_description_stays_inside_the_record_block(tmp_path: Path) -> None:
    """``</script>`` in a description ends the data block, unless it is escaped."""
    import json

    inventory = _hostile_inventory(tmp_path)
    page = to_html(build_graph(inventory), RenderOptions())

    raw = re.search(r'<script id="netgraph-data"[^>]*>(.*?)</script>', page, re.S)
    assert raw is not None
    assert "</script>" not in raw.group(1)
    assert "\\u003c/script\\u003e" in raw.group(1)

    # …and JSON.parse gives the reader's page the characters back, unchanged
    # but for what cannot be printed at all. The records sit in one pool for
    # the whole page — see ``html.py`` — so that is where the description is;
    # a layer's own index holds nothing but integers.
    records = json.loads(raw.group(1))["records"]
    descriptions = [record.get("description") for record in records if record.get("description")]
    assert descriptions, "the description reaches the records"
    for description in descriptions:
        assert '<script>alert("x")</script>' in description
        assert "‮" not in description and "" not in description
        assert "\U0001f4a3" in description


@requires_dot
def test_a_hostile_name_cannot_reach_an_id_or_a_link_in_the_page(tmp_path: Path) -> None:
    page = to_html(
        _hostile_graph(tmp_path),
        RenderOptions(link_template=LinkTemplate.parse("https://git.invalid/{name}")),
    )
    _, attributes = _elements(page)
    for tag, name, value in attributes:
        if name == "id" and tag in ("g", "svg", "a", "polygon", "text", "path"):
            assert re.fullmatch(r"v\d+-[A-Za-z0-9_.-]+", value or ""), value
        if name in ("href", "xlink:href") and tag == "a":
            assert (value or "").startswith("https://git.invalid/")
            assert '"' not in (value or "") and "<" not in (value or "")
