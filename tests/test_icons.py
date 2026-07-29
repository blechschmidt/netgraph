"""Icon themes: resolving one, drawing with it, and shipping the bundled set.

The properties asserted here are the ones a diagram drawn with ``--icons``
depends on:

* A theme is a directory of files named after element kinds, and resolving one
  either succeeds or says exactly what was looked for.
* Turning icons on changes only *how* a node is drawn. The topology, the labels
  and the edges are the same document they were without them, and a kind the
  theme has no picture for keeps its plain shape rather than vanishing.
* An SVG rendering stays a single self-contained file: the icons are embedded,
  not referenced by a path that only exists on the machine that drew it.
* The bundled theme covers every kind netgraph can draw, in both an SVG and a
  raster form, because Graphviz's ability to read the two differs by output.
"""

from __future__ import annotations

import base64
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from netgraph.errors import RenderError
from netgraph.loader import Inventory, load_tree
from netgraph.render import (
    BUNDLED_THEMES,
    FORMATS,
    ICON_KINDS,
    SUBNET_KIND,
    IconTheme,
    Layer,
    RenderOptions,
    build_graph,
    icon_theme,
    render,
    supports_icons,
    theme_choices,
    to_dot,
)
from netgraph.render.icons import CISCO, ICON_SUFFIXES, NO_ICONS, suffix_order

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

requires_dot = pytest.mark.skipif(
    shutil.which("dot") is None, reason="Graphviz 'dot' is not installed"
)


@pytest.fixture(scope="module")
def home_lab() -> Inventory:
    inventory = load_tree(EXAMPLES / "home-lab")
    assert inventory.errors == []
    return inventory


def _node_block(source: str, fqn: str) -> str:
    """The attribute list and label of one node, from ``[`` to ``>];``."""
    start = source.index(f'"{fqn}" [')
    return source[start : source.index(">];", start)]


def _edge_lines(source: str) -> list[str]:
    return [line for line in source.splitlines() if " -- " in line]


def _theme(directory: Path, *kinds: str, suffix: str = ".png") -> IconTheme:
    """A theme directory holding a one-byte file per named kind."""
    directory.mkdir(parents=True, exist_ok=True)
    for kind in kinds:
        (directory / f"{kind}{suffix}").write_bytes(b"\x00")
    return icon_theme(str(directory)) or pytest.fail("the theme resolved to nothing")


# --------------------------------------------------------------------------- #
# Resolving a theme
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("spec", [None, "", "   ", NO_ICONS])
def test_nothing_and_none_mean_no_icons(spec: str | None) -> None:
    """``--icons none`` has to be sayable, to override a theme set elsewhere."""
    assert icon_theme(spec) is None


def test_a_built_in_theme_resolves_by_name() -> None:
    assert icon_theme("cisco") is CISCO
    assert BUNDLED_THEMES["cisco"] is CISCO
    assert list(theme_choices()) == ["cisco", NO_ICONS]


def test_an_already_resolved_theme_passes_through() -> None:
    """A caller using the library may build its own theme rather than name one."""
    theme = IconTheme(name="mine", directory=Path("/nowhere"))
    assert icon_theme(theme) is theme


def test_an_unknown_theme_names_the_built_in_ones(tmp_path: Path) -> None:
    with pytest.raises(RenderError) as caught:
        icon_theme(str(tmp_path / "nope"))
    assert "cisco" in str(caught.value)
    assert "directory that exists" in str(caught.value)


def test_a_file_is_not_a_theme(tmp_path: Path) -> None:
    icons = tmp_path / "icons.zip"
    icons.write_bytes(b"")
    with pytest.raises(RenderError, match="file, not a directory"):
        icon_theme(str(icons))


def test_a_directory_without_a_usable_icon_says_what_was_looked_for(tmp_path: Path) -> None:
    (tmp_path / "Router.bmp").write_bytes(b"")
    with pytest.raises(RenderError) as caught:
        icon_theme(str(tmp_path))
    message = str(caught.value)
    assert all(kind in message for kind in ICON_KINDS)
    assert ".png" in message


def test_a_partial_directory_theme_covers_only_what_it_holds(tmp_path: Path) -> None:
    theme = _theme(tmp_path / "partial", "router", "switch")
    assert theme.kinds() == ("router", "switch")
    assert theme.file_for("router") == "router.png"
    assert theme.file_for("server") is None
    assert not theme.is_empty


def test_a_theme_is_asked_only_for_the_kinds_it_was_given(tmp_path: Path) -> None:
    theme = _theme(tmp_path / "partial", "router", "switch")
    assert dict(theme.files(["switch", "server", "switch"])) == {"switch": "switch.png"}


# --------------------------------------------------------------------------- #
# Which file, for which output
# --------------------------------------------------------------------------- #


def test_svg_output_prefers_a_vector_icon_and_everything_else_a_raster_one() -> None:
    """Graphviz reads an SVG image only in its own SVG output; see icons.py."""
    assert suffix_order("svg")[0] == ".svg"
    for target in ("dot", "png", "pdf"):
        assert suffix_order(target)[0] == ".png"


def test_the_preference_picks_between_two_files_of_the_same_kind(tmp_path: Path) -> None:
    theme = _theme(tmp_path / "both", "router", suffix=".png")
    (theme.directory / "router.svg").write_bytes(b"<svg/>")
    assert theme.file_for("router", prefer=suffix_order("svg")) == "router.svg"
    assert theme.file_for("router", prefer=suffix_order("png")) == "router.png"


def test_a_theme_with_only_one_format_is_used_for_every_output(tmp_path: Path) -> None:
    """Preference, not requirement: a PNG-only pack still draws SVG output."""
    theme = _theme(tmp_path / "raster", "router")
    assert theme.file_for("router", prefer=suffix_order("svg")) == "router.png"


# --------------------------------------------------------------------------- #
# The bundled theme
# --------------------------------------------------------------------------- #


def test_the_bundled_theme_draws_every_kind_netgraph_has() -> None:
    """Including the derived layer-3 subnet node, which is not an element kind."""
    assert CISCO.kinds() == ICON_KINDS
    assert SUBNET_KIND in ICON_KINDS


@pytest.mark.parametrize("kind", ICON_KINDS)
def test_every_bundled_icon_ships_as_both_vector_and_raster(kind: str) -> None:
    for suffix in (".svg", ".png"):
        path = CISCO.directory / f"{kind}{suffix}"
        assert path.is_file(), f"{path} is missing; run tools/render_icons.py"
        assert path.stat().st_size > 0


@pytest.mark.parametrize("kind", ICON_KINDS)
def test_a_bundled_raster_icon_is_a_png_big_enough_to_print(kind: str) -> None:
    """A PNG header, read directly: the file has to be one, not merely named one."""
    payload = (CISCO.directory / f"{kind}.png").read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", payload[16:24])
    assert width >= 256 and height >= 128, (width, height)


def test_a_bundled_vector_icon_declares_the_size_graphviz_scales_from() -> None:
    """Graphviz sizes an SVG from its own attributes; without them it draws nothing."""
    for kind in ICON_KINDS:
        text = (CISCO.directory / f"{kind}.svg").read_text(encoding="utf-8")
        assert re.search(r'\bwidth="\d+"', text), kind
        assert re.search(r'\bheight="\d+"', text), kind
        assert "viewBox=" in text, kind


def test_the_committed_rasters_are_what_the_vectors_would_produce() -> None:
    """The PNGs are generated from the SVGs, so an edited SVG must not orphan one.

    Skipped where cairosvg is absent, which is most machines and CI: it is
    deliberately not a netgraph dependency, and every other property of the
    committed files is asserted above without it.
    """
    pytest.importorskip("cairosvg", reason="only tools/render_icons.py needs it")
    generator = REPO_ROOT / "tools" / "render_icons.py"
    result = subprocess.run(
        [sys.executable, str(generator), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_the_theme_directory_holds_nothing_but_icons() -> None:
    """A stray file would ship in the wheel and mean nothing to the resolver."""
    unexpected = {
        path.name
        for path in CISCO.directory.iterdir()
        if not (path.stem in ICON_KINDS and path.suffix in ICON_SUFFIXES)
    }
    assert unexpected == set()


# --------------------------------------------------------------------------- #
# Drawing with a theme
# --------------------------------------------------------------------------- #


def test_without_a_theme_the_output_is_unchanged(home_lab: Inventory) -> None:
    source = to_dot(build_graph(home_lab))
    assert "imagepath" not in source
    assert "<IMG" not in source


def test_a_themed_node_is_drawn_as_its_icon_instead_of_a_shape(home_lab: Inventory) -> None:
    source = to_dot(build_graph(home_lab), RenderOptions(icons=CISCO))
    block = _node_block(source, "routers/rtr-home")
    assert '<IMG SCALE="TRUE" SRC="router.png"/>' in block
    # The shape and the fill are taken away: the icon is the glyph now, and a
    # box drawn behind it would be a second, contradicting one.
    assert "shape=none" in block
    assert 'style=""' in block


def test_the_theme_directory_appears_once_as_imagepath(home_lab: Inventory) -> None:
    """One machine-specific string per document, not one per node."""
    source = to_dot(build_graph(home_lab), RenderOptions(icons=CISCO))
    assert source.count("imagepath=") == 1
    assert f'imagepath="{CISCO.directory}"' in source
    assert str(CISCO.directory) not in source.partition("\n  node [")[2]


def test_every_element_kind_in_the_example_gets_its_own_icon(home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    source = to_dot(graph, RenderOptions(icons=CISCO))
    seen = set()
    for node in graph.element_nodes:
        assert f'SRC="{node.kind}.png"' in _node_block(source, node.fqn), node.fqn
        seen.add(node.kind)
    assert seen >= {"router", "switch", "computer", "server", "adapter"}


def test_a_subnet_node_is_drawn_as_a_subnet_icon(home_lab: Inventory) -> None:
    graph = build_graph(home_lab, layer=Layer.L3)
    source = to_dot(graph, RenderOptions(icons=CISCO))
    subnet = next(node for node in graph.nodes.values() if node.is_subnet)
    assert f'SRC="{SUBNET_KIND}.png"' in _node_block(source, subnet.fqn)


def test_a_kind_the_theme_cannot_draw_keeps_its_plain_shape(
    home_lab: Inventory, tmp_path: Path
) -> None:
    """A partial theme produces a mixed diagram, not a diagram with holes in it."""
    theme = _theme(tmp_path / "routers-only", "router")
    source = to_dot(build_graph(home_lab), RenderOptions(icons=theme))
    assert 'SRC="router.png"' in _node_block(source, "routers/rtr-home")
    switch = _node_block(source, "switches/sw-home")
    assert "<IMG" not in switch
    assert "shape=box3d" in switch


def test_icons_change_nothing_but_the_glyph(home_lab: Inventory) -> None:
    """Labels, edges and tooltips are the same document either way."""
    graph = build_graph(home_lab)
    plain = to_dot(graph)
    themed = to_dot(graph, RenderOptions(icons=CISCO))
    assert _edge_lines(themed) == _edge_lines(plain)
    assert themed.count("<TABLE") == plain.count("<TABLE")
    assert "<B>rtr-home</B>" in themed


def test_a_themed_rendering_is_deterministic(home_lab: Inventory) -> None:
    options = RenderOptions(icons=CISCO)
    assert to_dot(build_graph(home_lab), options) == to_dot(build_graph(home_lab), options)


def test_a_graph_of_kinds_the_theme_does_not_cover_carries_no_imagepath(
    home_lab: Inventory, tmp_path: Path
) -> None:
    theme = _theme(tmp_path / "hubs-only", "hub")
    source = to_dot(build_graph(home_lab), RenderOptions(icons=theme))
    assert "imagepath" not in source
    assert "<IMG" not in source


def test_the_dot_format_takes_the_portable_icon(home_lab: Inventory) -> None:
    """``-f dot`` is laid out later, by a Graphviz whose ``-T`` is unknown here."""
    source = to_dot(build_graph(home_lab), RenderOptions(icons=CISCO), target="dot")
    assert 'SRC="router.png"' in source
    vector = to_dot(build_graph(home_lab), RenderOptions(icons=CISCO), target="svg")
    assert 'SRC="router.svg"' in vector


# --------------------------------------------------------------------------- #
# Image output
# --------------------------------------------------------------------------- #


@requires_dot
def test_an_svg_rendering_embeds_its_icons(home_lab: Inventory) -> None:
    """The file has to draw correctly away from the theme directory."""
    payload = render(build_graph(home_lab), "svg", RenderOptions(icons=CISCO))
    assert b'xlink:href="router.svg"' not in payload
    assert str(CISCO.directory).encode() not in payload

    embedded = re.findall(rb'xlink:href="data:image/svg\+xml;base64,([^"]+)"', payload)
    assert embedded, "no icon was embedded"
    decoded = {base64.b64decode(chunk) for chunk in embedded}
    assert decoded <= {(CISCO.directory / f"{kind}.svg").read_bytes() for kind in ICON_KINDS}


@requires_dot
def test_embedding_leaves_the_rest_of_the_document_alone(home_lab: Inventory) -> None:
    graph = build_graph(home_lab)
    plain = render(graph, "svg")
    themed = render(graph, "svg", RenderOptions(icons=CISCO))
    # Every node, edge and line of text the plain rendering had, it still has.
    # (Graphviz writes a name with a hyphen as ``rtr&#45;home``, so the labels
    # are counted rather than searched for.)
    assert themed.count(b"<title>") == plain.count(b"<title>")
    assert themed.count(b"<text ") == plain.count(b"<text ")
    assert themed.count(b'class="edge"') == plain.count(b'class="edge"')


@requires_dot
def test_a_raster_rendering_draws_the_icons(home_lab: Inventory) -> None:
    """PNG goes through cairo, which every build can hand a raster icon to.

    That it returned at all is the assertion: an icon Graphviz could not read is
    a warning on a zero exit status, and ``to_image`` turns that into an error
    rather than handing back a diagram with holes in it.
    """
    payload = render(build_graph(home_lab), "png", RenderOptions(icons=CISCO))
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert payload != render(build_graph(home_lab), "png")


def test_an_icon_graphviz_could_not_read_is_reported(
    home_lab: Inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dot warns and carries on, leaving a hole; a hole is not a rendering."""
    complaint = b'Warning: No loadimage plugin for "svg:cairo"\n' * 12

    def _warns(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"%PDF", stderr=complaint)

    monkeypatch.setattr(subprocess, "run", _warns)
    with pytest.raises(RenderError) as caught:
        render(build_graph(home_lab), "pdf", RenderOptions(icons=CISCO))
    message = str(caught.value)
    assert "librsvg" in message and "-f svg" in message
    # The same sentence once, not once per node that could not be drawn.
    assert message.count("No loadimage plugin") == 1


def test_the_same_warning_without_icons_is_left_alone(
    home_lab: Inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layout warnings are not this renderer's to escalate."""

    def _warns(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"%PDF", stderr=b"Warning: No or improper image\n"
        )

    monkeypatch.setattr(subprocess, "run", _warns)
    assert render(build_graph(home_lab), "pdf") == b"%PDF"


# --------------------------------------------------------------------------- #
# What the registry says
# --------------------------------------------------------------------------- #


def test_only_the_graphviz_formats_claim_to_draw_icons() -> None:
    drawn = {name for name in FORMATS if supports_icons(name)}
    # ``html`` embeds an SVG Graphviz laid out, so it inherits the icons with it.
    assert drawn == {"dot", "svg", "html", "png", "pdf"}
    assert not supports_icons("no-such-format")
