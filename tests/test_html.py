"""The self-contained interactive page: ``netgraph render -f html``.

The output of this format is a *published artefact* — a file that gets emailed,
committed, or served from a static host — so the properties asserted here are
the ones a reader of that file depends on and cannot check for themselves:

* it is one file. Nothing in it is fetched: no CDN, no stylesheet, no font, no
  script, no image URL. The only URLs a page may hold are the ones
  ``--link-template`` put there, and those are links, not loads;
* it works under a strict Content-Security-Policy, which it carries itself: the
  hash of every inline block, no ``'unsafe-inline'``, no ``'unsafe-eval'``;
* it is deterministic, byte for byte, so committing one and re-rendering it is
  a diff of the inventory and not of the renderer;
* it is a *parseable* document — every assertion below goes through an HTML
  parser rather than a substring search, because "the string is in the file" is
  exactly what an escaping bug also looks like.

The escaping battery itself lives next to the DOT and Mermaid ones in
``test_interactive.py``: hostile content is one subject, asserted in one place,
at every layer it can reach.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from netgraph.errors import RenderError
from netgraph.loader import Inventory, load_tree
from netgraph.render import (
    FORMATS,
    Graph,
    Layer,
    LinkTemplate,
    RenderOptions,
    build_graph,
    element_ids,
    html_document,
    render,
    render_layers,
    renderer_for,
    supports_layers,
    to_html,
)
from netgraph.render.html import DATA_ELEMENT_ID, PAGE_KIND

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
ASSETS = REPO_ROOT / "src" / "netgraph" / "render" / "assets"

requires_dot = pytest.mark.skipif(
    shutil.which("dot") is None, reason="the Graphviz 'dot' executable is not installed"
)
requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is not installed; 'node --check' cannot run"
)


# --------------------------------------------------------------------------- #
# Reading the document back
# --------------------------------------------------------------------------- #


class _Document(HTMLParser):
    """Every element and attribute of a page, as a parser sees them.

    Deliberately not a regular expression: an escaping hole shows up as *more
    markup than intended*, which only a parser can tell from text that merely
    looks like markup.
    """

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str, str | None]] = []
        self.texts: dict[str, list[str]] = {}
        self._open: list[str] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend((tag, name, value) for name, value in attrs)
        self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend((tag, name, value) for name, value in attrs)

    def handle_endtag(self, tag: str) -> None:
        while self._open and self._open.pop() != tag:
            continue

    def handle_data(self, data: str) -> None:
        if self._open:
            self.texts.setdefault(self._open[-1], []).append(data)

    def ids(self) -> list[str]:
        return [value or "" for _, name, value in self.attributes if name == "id"]

    def values(self, attribute: str) -> list[str]:
        return [value or "" for _, name, value in self.attributes if name == attribute]

    def content(self, tag: str) -> str:
        return "".join(self.texts.get(tag, []))


def parse(source: str) -> _Document:
    return _Document(source)


def data_of(source: str) -> dict[str, Any]:
    """The records the page carries, read the way its own client reads them."""
    block = re.search(
        rf'<script id="{DATA_ELEMENT_ID}" type="application/json">(.*?)</script>', source, re.S
    )
    assert block is not None, "the page carries no record block"
    parsed: dict[str, Any] = json.loads(block.group(1))
    return parsed


def blocks(source: str, tag: str) -> list[str]:
    """The raw text of every ``tag`` element, as the browser would hash it."""
    return re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", source, re.S)


def policy(source: str) -> str:
    document = parse(source)
    for tag, name, value in document.attributes:
        if tag == "meta" and name == "content" and value and "default-src" in value:
            return value
    raise AssertionError("the page carries no Content-Security-Policy")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def home_lab() -> Inventory:
    return load_tree(EXAMPLES / "home-lab")


@pytest.fixture(scope="module")
def graph(home_lab: Inventory) -> Graph:
    return build_graph(home_lab, layer=Layer.L2)


@pytest.fixture(scope="module")
def page(graph: Graph) -> str:
    return to_html(graph, RenderOptions(title="home lab"))


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


@requires_dot
def test_the_page_is_one_html_document(page: str) -> None:
    assert page.startswith("<!doctype html>")
    document = parse(page)
    assert document.tags.count("html") == 1
    assert document.tags.count("body") == 1
    # One style block and two scripts: the client, and the records it reads.
    assert document.tags.count("style") == 1
    assert document.tags.count("script") == 2
    assert document.content("title") == "home lab"


@requires_dot
def test_the_records_are_the_json_export_keyed_by_element_id(graph: Graph, page: str) -> None:
    data = data_of(page)
    assert data["kind"] == PAGE_KIND
    assert data["apiVersion"] == "netgraph.dev/v1alpha1"
    assert [layer["layer"] for layer in data["layers"]] == ["l2"]

    identity = element_ids(graph)
    elements = data["layers"][0]["elements"]
    assert set(elements) == {*identity.nodes.values(), *identity.edges}

    switch = elements[identity.nodes["switches/sw-home"]]
    assert switch["name"] == "sw-home"
    assert switch["kind"] == "switch"
    # The record is the node-link export: same keys, same spelling, plus the
    # element id and the links cross-reference build_details adds.
    assert switch["id"] == "switches/sw-home"
    assert [port["name"] for port in switch["interfaces"]]
    assert switch["links"], "a switch with cables on it lists them"


@requires_dot
def test_every_record_has_a_shape_and_every_shape_a_record(page: str) -> None:
    """The ids are the whole interface between the picture and the panel."""
    data = data_of(page)
    document = parse(page)
    ids = set(document.ids())
    for view in data["layers"][0]["views"]:
        for element in data["layers"][0]["elements"]:
            assert f"{view['view']}-{element}" in ids, element


@requires_dot
def test_no_id_appears_twice(page: str) -> None:
    """Several drawings of one inventory are several copies of every id."""
    ids = parse(page).ids()
    assert len(ids) == len(set(ids)), "an id is repeated, so getElementById would guess"


@requires_dot
def test_the_page_holds_no_external_reference(page: str) -> None:
    """Nothing to fetch: the file has to work from a mail attachment."""
    document = parse(page)
    for tag, name, value in document.attributes:
        if name in ("src", "srcset", "poster", "data", "codebase", "action", "formaction"):
            raise AssertionError(f"<{tag} {name}={value!r}> would fetch something")
        if name == "href" and value not in ("data:,",):
            raise AssertionError(f"<{tag} href={value!r}> is an external reference")
    # The style sheet pulls nothing in either. Its own comments say so in
    # words, which is why this reads the declarations rather than the file.
    style = re.sub(r"/\*.*?\*/", "", "".join(blocks(page, "style")), flags=re.S)
    assert "@import" not in style
    assert not re.search(r"url\(\s*['\"]?(?!data:)[^)]", style)
    # …and the only absolute URLs in the whole file are the XML namespaces the
    # embedded SVG declares, which are names rather than addresses.
    urls = set(re.findall(r"https?://[^\"'<>\s)]+", page))
    assert urls <= {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"}, urls


@requires_dot
def test_the_page_carries_a_strict_policy_naming_its_own_blocks(page: str) -> None:
    from base64 import b64encode
    from hashlib import sha256

    rules = policy(page)
    assert "default-src 'none'" in rules
    assert "unsafe-inline" not in rules
    assert "unsafe-eval" not in rules
    # Every inline block is allowed by its hash, and nothing else is.
    for tag in ("style", "script"):
        for block in blocks(page, tag):
            digest = b64encode(sha256(block.encode("utf-8")).digest()).decode("ascii")
            assert f"'sha256-{digest}'" in rules, f"an inline <{tag}> is not named by the policy"
    assert rules.count("sha256-") == 3


@requires_dot
def test_two_runs_produce_the_same_bytes(graph: Graph) -> None:
    options = RenderOptions(title="home lab", link_template=LinkTemplate.parse("h://x/{file}"))
    assert to_html(graph, options).encode() == to_html(graph, options).encode()


# --------------------------------------------------------------------------- #
# The drawings
# --------------------------------------------------------------------------- #


def views_of(page: str, layer: int = 0) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = data_of(page)["layers"][layer]["views"]
    return views


@requires_dot
def test_a_drawing_is_embedded_for_every_combination_of_the_toggles(page: str) -> None:
    """A browser cannot lay a graph out, so every view is laid out here."""
    assert [(view["showIps"], view["showVlans"]) for view in views_of(page)] == [
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ]
    assert parse(page).values("data-view") == sorted(
        {view["view"] for view in views_of(page)}, key=lambda name: int(name[1:])
    )
    assert parse(page).tags.count("svg") == len({view["view"] for view in views_of(page)})


@requires_dot
def test_an_option_that_is_off_has_no_drawing_that_prints_it(graph: Graph) -> None:
    """The flags are a ceiling, not a starting state; see the module docstring."""
    page = to_html(graph, RenderOptions(show_ips=False, show_vlans=False))
    assert [(view["showIps"], view["showVlans"]) for view in views_of(page)] == [(False, False)]
    assert parse(page).tags.count("svg") == 1
    # …and the records carry no address either, so a published page cannot be
    # made to give one up by editing its JSON.
    for record in data_of(page)["layers"][0]["elements"].values():
        for port in record.get("interfaces", []):
            assert "addresses" not in port
    assert "10.0.10.1" not in page
    # The toggle for an option nobody can turn on is not offered.
    assert 'id="ng-ips"' not in page
    assert 'id="ng-vlans"' not in page


@requires_dot
def test_identical_drawings_are_embedded_once(tmp_path: Path) -> None:
    """An inventory with no VLAN draws the same with and without them."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-a}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet}]}\n",
        encoding="utf-8",
    )
    page = to_html(build_graph(load_tree(tmp_path)))
    views = views_of(page)
    assert len(views) == 4, "the page still offers every combination"
    assert len({view["view"] for view in views}) < 4, "but shares the drawings that match"
    assert parse(page).tags.count("svg") == len({view["view"] for view in views})


@requires_dot
def test_the_embedded_svg_is_sized_by_the_page(page: str) -> None:
    document = parse(page)
    for tag, name, _ in document.attributes:
        if tag == "svg":
            assert name not in ("width", "height"), "the page decides how big the diagram is"
    assert 'preserveAspectRatio="xMidYMid meet"' in page


@requires_dot
def test_native_tooltips_are_left_out(page: str) -> None:
    """The page draws the record itself; a second, thinner tooltip over it is noise."""
    assert "<title>" not in page.replace("<title>home lab</title>", "")
    assert "xlink:title" not in page


# --------------------------------------------------------------------------- #
# Layers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def layered(home_lab: Inventory) -> str:
    graphs = [build_graph(home_lab, layer=layer) for layer in (Layer.L1, Layer.L2, Layer.L3)]
    return html_document(graphs)


@requires_dot
def test_several_layers_become_a_switcher(layered: str) -> None:
    data = data_of(layered)
    assert [layer["layer"] for layer in data["layers"]] == ["l1", "l2", "l3"]
    assert data["layers"][0]["label"] == "l1 — physical"
    document = parse(layered)
    assert "ng-layer" in document.ids()
    assert document.tags.count("option") >= 3
    # Layer 3 draws prefixes the physical view has no node for.
    assert any(record["type"] == "subnet" for record in data["layers"][2]["elements"].values())


@requires_dot
def test_one_layer_has_no_switcher(page: str) -> None:
    assert "ng-layer" not in parse(page).ids()


@requires_dot
def test_the_same_element_in_two_layers_keeps_one_record_per_layer(layered: str) -> None:
    data = data_of(layered)
    first, second = data["layers"][0]["elements"], data["layers"][1]["elements"]
    shared = set(first) & set(second)
    assert shared, "the physical and VLAN views draw the same devices"
    # The ids repeat across layers, which is exactly why the drawings prefix
    # them: the page holds both and must not confuse the two.
    ids = parse(layered).ids()
    assert len(ids) == len(set(ids))


def test_only_html_holds_more_than_one_layer() -> None:
    assert {name for name in FORMATS if supports_layers(name)} == {"html"}
    assert not supports_layers("no-such-format")


@requires_dot
def test_asking_another_format_for_two_layers_is_refused(home_lab: Inventory) -> None:
    graphs = [build_graph(home_lab, layer=layer) for layer in (Layer.L1, Layer.L2)]
    with pytest.raises(RenderError, match="holds one layer"):
        render_layers(graphs, "svg")
    # One graph is the ordinary render, whatever the format.
    assert render_layers(graphs[:1], "dot") == render(graphs[0], "dot")


def test_rendering_no_layer_at_all_is_refused() -> None:
    with pytest.raises(RenderError, match="no layer was selected"):
        render_layers([], "html")
    with pytest.raises(RenderError, match="at least one layer"):
        html_document([])


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


@requires_dot
def test_link_template_anchors_survive_into_the_page(graph: Graph) -> None:
    options = RenderOptions(link_template=LinkTemplate.parse("https://git.invalid/{file}#L{line}"))
    page = to_html(graph, options)
    hrefs = [
        value
        for tag, name, value in parse(page).attributes
        if tag == "a" and name in ("href", "xlink:href") and value
    ]
    assert hrefs, "the diagram carries the links it was asked for"
    assert all(href.startswith("https://git.invalid/") for href in hrefs)
    assert "Ctrl-click" in page, "and the page says how to follow one"


@requires_dot
def test_without_a_template_the_page_holds_no_anchor(page: str) -> None:
    assert "a" not in parse(page).tags


# --------------------------------------------------------------------------- #
# The assets
# --------------------------------------------------------------------------- #


def asset_files() -> Iterator[Path]:
    yield from sorted(ASSETS.glob("*.js"))
    yield from sorted(ASSETS.glob("*.css"))


def code_of(asset: Path) -> str:
    """``asset`` with its comments removed.

    These files document themselves in prose that names the very constructs
    they must not use — "no @import", "no eval" — so a search over the whole
    text would find the promise instead of a breach of it.
    """
    text = re.sub(r"/\*.*?\*/", "", asset.read_text(encoding="utf-8"), flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


@pytest.mark.parametrize("asset", list(asset_files()), ids=lambda path: path.name)
def test_no_asset_reaches_the_network(asset: Path) -> None:
    """Inlined verbatim, so a URL in one of these is a URL in every page."""
    text = code_of(asset)
    assert not re.search(r"https?://(?!www\.w3\.org)", text), asset.name
    assert "@import" not in text
    for forbidden in ("fetch(", "XMLHttpRequest", "importScripts", "eval(", "new Function"):
        assert forbidden not in text, f"{asset.name} would need more than the policy allows"


@requires_node
@pytest.mark.parametrize(
    "asset", [p for p in asset_files() if p.suffix == ".js"], ids=lambda p: p.name
)
def test_the_client_parses(asset: Path) -> None:
    """A syntax error in an inlined script is a page that does nothing at all."""
    subprocess.run(["node", "--check", str(asset)], check=True, capture_output=True)


def test_the_web_preview_serves_the_same_detail_renderer() -> None:
    """One file, two front ends: see netgraph/render/assets/detail.js."""
    from netgraph.web import asset

    assert asset("detail.js") == (ASSETS / "detail.js").read_bytes()


@requires_dot
def test_the_page_inlines_exactly_those_files(page: str) -> None:
    script = "".join(blocks(page, "script")[1:])
    for name in ("detail.js", "page.js"):
        assert (ASSETS / name).read_text(encoding="utf-8") in script
    assert (ASSETS / "page.css").read_text(encoding="utf-8") in "".join(blocks(page, "style"))


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


@requires_dot
def test_a_dropped_cable_is_named_in_the_page(tmp_path: Path) -> None:
    """Under --force the picture is missing links; the page has to say so."""
    (tmp_path / "inv.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: computer\n"
        "metadata: {name: pc-a}\n"
        "spec: {interfaces: [{name: eth0, type: ethernet}]}\n"
        "---\n"
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl}\n"
        "spec: {endpoints: [pc-a:eth0, pc-gone:eth0], medium: copper}\n",
        encoding="utf-8",
    )
    graph = build_graph(load_tree(tmp_path))
    assert graph.dangling
    assert data_of(to_html(graph))["layers"][0]["dangling"] == list(graph.dangling)


@requires_dot
def test_the_backend_is_reachable_under_both_of_its_names(graph: Graph) -> None:
    from netgraph.render import render_html

    assert render_html(graph) == to_html(graph)
    assert render(graph, "html").decode("utf-8") == to_html(graph)


def test_the_format_is_registered_as_what_it_is() -> None:
    renderer = renderer_for("html")
    assert renderer.suffix == ".html"
    assert renderer.media_type == "text/html; charset=utf-8"
    assert not renderer.binary
    assert renderer.is_text and renderer.holds_layers
    assert renderer.interactive and renderer.supports_icons and renderer.supports_highlight
    assert renderer.csp is not None and "frame-ancestors 'self'" in renderer.csp


# --------------------------------------------------------------------------- #
# The embeddable fragment
# --------------------------------------------------------------------------- #

STANDALONE = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<!-- Generated by graphviz -->
<svg width="100pt" height="50pt" viewBox="0 0 100 50"
  xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
<g id="node-a" class="node" clip-path="url(#clip1)" onclick="alert(1)">
<title>a tooltip</title>
<a xlink:href="https://git.invalid/a" xlink:title="also a tooltip" target="_blank">
<polygon id="shape1" points="0,0 1,1"/>
</a>
<image xlink:href="data:image/png;base64,AAA"/>
<script>alert(2)</script>
</g>
</svg>
"""


def test_the_fragment_is_the_svg_element_and_nothing_around_it() -> None:
    from netgraph.render.fragment import fragment

    out = fragment(STANDALONE)
    assert out.startswith("<svg") and out.rstrip().endswith("</svg>")
    assert "<?xml" not in out and "DOCTYPE" not in out
    assert 'width="100pt"' not in out and 'height="50pt"' not in out
    assert 'viewBox="0 0 100 50"' in out
    assert 'preserveAspectRatio="xMidYMid meet"' in out


def test_the_fragment_can_neither_run_nor_navigate_by_default() -> None:
    from netgraph.render.fragment import fragment

    out = fragment(STANDALONE)
    assert "script" not in out
    assert "onclick" not in out
    assert "git.invalid" not in out, "an anchor is dropped unless links were asked for"
    assert "<a" not in out, "and so is the tab stop it left behind"
    assert "a tooltip" not in out, "the native tooltips go with them"
    assert 'target="_blank"' not in out
    # The inlined icon stays: a browser runs nothing inside an image.
    assert "data:image/png;base64,AAA" in out


def test_the_fragment_keeps_what_the_caller_asks_for() -> None:
    from netgraph.render.fragment import fragment

    out = fragment(STANDALONE, tooltips=True, links=True)
    assert "<title>a tooltip</title>" in out
    assert "https://git.invalid/a" in out
    assert "also a tooltip" in out
    # …and never what could execute, whatever was asked for.
    assert "script" not in out and "onclick" not in out


def test_the_fragment_prefixes_every_id_and_every_reference_to_one() -> None:
    from netgraph.render.fragment import fragment

    out = fragment(STANDALONE, prefix="v7")
    assert 'id="v7-node-a"' in out
    assert 'id="v7-shape1"' in out
    assert "url(#v7-clip1)" in out
    # A same-document href moves with the id it names, or it points at nothing.
    same = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1">'
    same += b'<use id="u" href="#shape"/><g id="shape"/></svg>'
    assert 'href="#v7-shape"' in fragment(same, prefix="v7")


def test_a_prefix_that_could_not_be_an_id_is_refused() -> None:
    from netgraph.render.fragment import fragment

    for bad in ("7v", "a b", "a<b", "-x"):
        with pytest.raises(RenderError, match="cannot prefix an XML id"):
            fragment(STANDALONE, prefix=bad)


def test_something_that_is_not_a_sizeable_svg_is_refused() -> None:
    from netgraph.render.fragment import fragment

    with pytest.raises(RenderError, match="could not be parsed"):
        fragment(b"<svg")
    with pytest.raises(RenderError, match="expected an SVG document"):
        fragment(b"<html/>")
    with pytest.raises(RenderError, match="no viewBox"):
        fragment(b'<svg xmlns="http://www.w3.org/2000/svg" width="1pt"/>')


def test_the_web_preview_still_gets_the_inert_fragment() -> None:
    """``netgraph web`` embeds a diagram built from text being typed."""
    from netgraph.web import prepare

    out = prepare(STANDALONE)
    assert "<title>" not in out and "git.invalid" not in out and "script" not in out


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


@requires_dot
def test_render_writes_a_page_to_a_file(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from netgraph.cli import cli

    destination = tmp_path / "topology.html"
    result = CliRunner().invoke(
        cli,
        ["-i", str(EXAMPLES / "home-lab"), "render", "-f", "html", "-o", str(destination)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    page = destination.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert data_of(page)["kind"] == PAGE_KIND


@requires_dot
def test_render_puts_several_layers_in_one_page(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from netgraph.cli import cli

    destination = tmp_path / "topology.html"
    result = CliRunner().invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "home-lab"),
            "render",
            "-f",
            "html",
            "--layer",
            "l1",
            "--layer",
            "l3",
            "--layer",
            "l1",
            "-o",
            str(destination),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # The repeat is one layer, not two: a switcher offering l1 twice is a bug.
    assert [layer["layer"] for layer in data_of(destination.read_text())["layers"]] == ["l1", "l3"]
    assert "at layer l1, l3" in result.stderr


def test_asking_svg_for_two_layers_is_a_usage_error(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from netgraph.cli import cli

    result = CliRunner().invoke(
        cli,
        [
            "-i",
            str(EXAMPLES / "home-lab"),
            "render",
            "-f",
            "svg",
            "--layer",
            "l1",
            "--layer",
            "l2",
            "-o",
            str(tmp_path / "x.svg"),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    assert "holds one layer" in result.output
    assert not (tmp_path / "x.svg").exists()


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #


@requires_dot
def test_a_watch_cycle_renders_several_layers_into_one_page() -> None:
    from netgraph.watch.pipeline import RenderRequest, Status, run_cycle

    result = run_cycle(
        RenderRequest(
            inventory=EXAMPLES / "home-lab",
            output_format="html",
            layers=(Layer.L1, Layer.L2),
        )
    )
    assert result.status is Status.OK, result.message
    assert result.payload is not None
    assert [layer["layer"] for layer in data_of(result.payload.decode())["layers"]] == ["l1", "l2"]
    # Both layers are counted, so the status line does not under-report the work.
    assert result.nodes == 12 and result.edges == 10


# --------------------------------------------------------------------------- #
# The committed example
# --------------------------------------------------------------------------- #

EXAMPLE_PAGE = REPO_ROOT / "docs" / "home-lab.html"


def test_the_committed_example_is_a_page_of_this_shape() -> None:
    """docs/home-lab.html is what a reader is invited to open; it must open.

    Its *bytes* are deliberately not compared against a fresh render: the
    drawings come from whatever Graphviz is installed, and pinning them here
    would make the suite fail on a machine with a different one rather than on
    a change worth noticing.
    """
    assert EXAMPLE_PAGE.is_file(), "docs/home-lab.html is missing; see the README for the command"
    page = EXAMPLE_PAGE.read_text(encoding="utf-8")
    data = data_of(page)
    assert data["kind"] == PAGE_KIND
    assert [layer["layer"] for layer in data["layers"]] == ["l1", "l2", "l3"]
    assert data["layers"][0]["elements"], "the example carries its records"

    document = parse(page)
    ids = document.ids()
    assert len(ids) == len(set(ids))
    for tag, name, value in document.attributes:
        assert name not in ("src", "data", "srcset"), f"<{tag} {name}> fetches something"
        if name == "href":
            assert value == "data:,", f"<{tag} href={value!r}> is an external reference"


def test_an_anchor_may_not_carry_a_fragment_of_its_own() -> None:
    """``<a href="#x">`` inside the diagram would move the page's selection."""
    from netgraph.render.fragment import fragment

    payload = (
        b'<svg xmlns="http://www.w3.org/2000/svg" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1 1">'
        b'<a xlink:href="#node-elsewhere"><polygon points="0,0"/></a></svg>'
    )
    assert "node-elsewhere" not in fragment(payload, links=True)
