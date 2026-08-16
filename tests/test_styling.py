"""Per-element styling and themes (§22).

Four properties, and the tests below are grouped by which one they defend:

**The vocabulary is closed.** Nothing a manifest can write reaches a DOT
attribute or an mxGraph style string unvalidated. That is not tidiness — both
formats are generated text with delimiters a value could carry — so the tests
that matter most here are the ones that hand the model a hostile value and
insist on a refusal rather than on an escape.

**The ladder is the specification.** Element, then theme (most clauses first,
later declaration breaking a tie), then the icon set, then the built-in palette,
resolved *field by field* so that a theme setting a fill does not take a shape
away. Every rung and every tie-break is asserted directly rather than through a
rendering, because a rendering can only show which value won and not why.

**Nothing changes without a style.** An inventory that says nothing about
appearance renders exactly the bytes it always did. The goldens carry most of
that; what is here is the narrower claim that the resolver, given nothing,
answers the palette and marks every field ``default``.

**A colour survives the round trip.** The draw.io export is the one backend
whose output somebody else's program reads back, and a colour that arrived as a
different colour would be a silent lie about what the inventory says.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from netviz.cli import cli
from netviz.config import parse_config
from netviz.drawio.styles import MX_SHAPES, edge_style, node_style
from netviz.errors import ConfigurationError, RenderError, SchemaError
from netviz.loader import load_tree
from netviz.models import Style
from netviz.models.document import parse_document, parse_theme
from netviz.models.style import NAMED_COLOURS, SHAPES, hex_colour
from netviz.models.theme import THEME_KIND
from netviz.render import Layer, RenderOptions, build_graph
from netviz.render.dot import to_dot
from netviz.render.styles import PLAIN, ResolvedStyle, StyleMap, dot_shape, fade, resolve_style
from netviz.render.theme import BUNDLED_THEMES, StyleTarget, Theme, load_theme, resolve_theme

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STYLED = FIXTURES / "styled"
HOME_LAB = REPO_ROOT / "examples" / "home-lab"


def device(**style: Any) -> Any:
    """One switch carrying ``style``, parsed as the loader parses it."""
    spec: dict[str, Any] = {"interfaces": [{"name": "e0", "type": "ethernet"}]}
    if style:
        spec["style"] = style
    return parse_document(
        {
            "apiVersion": "netviz.dev/v1alpha1",
            "kind": "switch",
            "metadata": {"name": "sw"},
            "spec": spec,
        }
    )


def theme_of(*rules: dict[str, Any], name: str = "t") -> Theme:
    """A theme built from rule literals, through the real parser."""
    document = parse_theme(
        {
            "apiVersion": "netviz.dev/v1alpha1",
            "kind": THEME_KIND,
            "metadata": {"name": name},
            "spec": {"rules": list(rules)},
        }
    )
    return Theme.from_document(document)


# --------------------------------------------------------------------------- #
# The vocabulary is closed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "#fff",
        "#FFFFFF",
        "navy",
        "  navy  ",
        "NAVY",
        "none",
        "transparent",
    ],
)
def test_a_colour_may_be_a_hex_literal_or_a_name(value: str) -> None:
    assert Style(fill=value).fill is not None


@pytest.mark.parametrize(
    "value",
    [
        # The reason this vocabulary is closed. Each of these, passed through,
        # would end a DOT attribute or an mxGraph declaration and start another.
        'red", shape="none',
        "#fff;shape=image;image=data:x",
        "rgb(1,2,3)",
        "#ff",
        "#gggggg",
        "url(#x)",
        42,
        ["navy"],
    ],
)
def test_a_colour_that_is_not_one_is_refused(value: Any) -> None:
    with pytest.raises(Exception) as caught:
        Style(fill=value)
    assert "NV-Z001" in str(caught.value)


def test_a_misspelt_colour_is_answered_with_the_nearest_legal_one() -> None:
    """A diagnostic that only said "not a colour" would send the user to the spec."""
    with pytest.raises(Exception) as caught:
        Style(fill="navvy")
    assert "did you mean 'navy'?" in str(caught.value)


def test_a_misspelt_shape_is_answered_the_same_way() -> None:
    with pytest.raises(Exception) as caught:
        Style(shape="hexagn")
    assert "did you mean 'hexagon'?" in str(caught.value)


def test_a_value_far_from_anything_legal_is_answered_with_the_whole_list() -> None:
    """There is no nearest spelling of ``qqq``, so the answer is the vocabulary."""
    with pytest.raises(Exception) as caught:
        Style(dash="qqq")
    message = str(caught.value)
    assert "expected one of" in message
    assert "dashed" in message


@pytest.mark.parametrize(
    "field, value",
    [
        ("strokeWidth", 0),
        ("strokeWidth", 21),
        ("fontSize", 5),
        ("fontSize", 97),
        ("opacity", -0.1),
        ("opacity", 1.1),
    ],
)
def test_a_number_outside_its_bounds_is_refused(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Style(**{field: value})


@pytest.mark.parametrize(
    "value",
    ["../../etc/passwd", "sub/dir", "router.svg", "", "a b", "/abs", "-lead", "trail-"],
)
def test_an_icon_name_that_could_be_a_path_is_refused(value: str) -> None:
    """An icon is chosen *from* the theme; a manifest never names a file."""
    with pytest.raises(Exception) as caught:
        Style(icon=value)
    assert "NV-Z001" in str(caught.value)


def test_an_empty_style_block_is_refused() -> None:
    """Almost always a key indented one level too far. It would render nothing."""
    with pytest.raises(Exception) as caught:
        Style()
    assert "NV-Z002" in str(caught.value)


def test_a_colour_keeps_the_spelling_the_document_used() -> None:
    """``netviz fmt`` must not turn ``navy`` into ``#1e3a8a`` behind somebody."""
    assert Style(fill="navy").fill == "navy"
    assert hex_colour("navy") == "#1e3a8a"
    assert hex_colour(None) is None
    assert hex_colour("none") == "none"


def test_every_named_colour_is_a_hex_literal_or_none() -> None:
    """The table is what reaches a renderer, so nothing in it may need parsing."""
    for name, value in NAMED_COLOURS.items():
        assert value == "none" or re.fullmatch(r"#[0-9a-f]{6}", value), name


def test_every_shape_has_a_spelling_in_both_backends() -> None:
    """A shape only one backend can draw would be lost on a draw.io round trip."""
    for shape in SHAPES:
        assert dot_shape(shape), shape
        assert shape in MX_SHAPES, shape


def test_a_shape_netviz_does_not_know_falls_back_rather_than_passing_through() -> None:
    """The table *is* the whole of what may reach a DOT file."""
    assert dot_shape("shape=none, label=") == "box"
    assert dot_shape(None) == "box"


def test_the_style_block_is_accepted_on_every_drawable_kind() -> None:
    """A kind that could not be styled would be the one nobody could restyle."""
    inventory = load_tree(STYLED)
    assert not inventory.errors
    for kind in ("switch", "router", "computer", "cable"):
        assert any(
            element.kind == kind and getattr(element.spec, "style", None) is not None
            for element in inventory.elements.values()
        ) or any(element.kind == kind for element in inventory.elements.values())


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #


def test_with_nothing_declared_every_field_comes_from_the_palette() -> None:
    resolved = resolve_style(
        StyleTarget(kind="switch"), defaults={"fill": "#dcf0dc", "shape": "box3d"}
    )
    assert resolved.fill == "#dcf0dc"
    assert dict(resolved.origin) == {"fill": "default", "shape": "default"}


def test_an_element_beats_a_theme_field_by_field() -> None:
    """The property that keeps a theme's shape when an element only sets a fill."""
    theme = theme_of({"select": {"kind": "switch"}, "style": {"fill": "green", "shape": "box3d"}})
    resolved = resolve_style(
        StyleTarget(kind="switch", style=Style(fill="navy")),
        theme=theme,
        defaults={"fill": "#f5f5f5", "stroke": "#6b7280"},
    )
    assert resolved.fill == "#1e3a8a"
    assert resolved.origin["fill"] == "element"
    assert resolved.shape == "box3d"
    assert resolved.origin["shape"].startswith("theme:")
    # And the rung below both still answers for what neither said.
    assert resolved.stroke == "#6b7280"
    assert resolved.origin["stroke"] == "default"


def test_a_more_specific_selector_wins() -> None:
    theme = theme_of(
        {"select": {"kind": "switch", "role": "core"}, "style": {"fill": "navy"}},
        {"select": {"kind": "switch"}, "style": {"fill": "green"}},
    )
    core = resolve_style(StyleTarget(kind="switch", labels={"role": "core"}), theme=theme)
    plain = resolve_style(StyleTarget(kind="switch"), theme=theme)
    assert core.fill == "#1e3a8a", "two clauses must beat one whatever the order"
    assert plain.fill == "#16a34a"


def test_equal_specificity_is_broken_by_the_later_declaration() -> None:
    """CSS's rule, and the one a reader guesses right."""
    theme = theme_of(
        {"select": {"kind": "switch"}, "style": {"fill": "green"}},
        {"select": {"role": "core"}, "style": {"fill": "navy"}},
    )
    resolved = resolve_style(StyleTarget(kind="switch", labels={"role": "core"}), theme=theme)
    assert resolved.fill == "#1e3a8a"
    assert resolved.origin["fill"].endswith("#1"), "the second rule is the one that won"


def test_a_selector_with_no_clauses_matches_everything_and_loses_every_tie() -> None:
    theme = theme_of(
        {"style": {"fill": "white", "stroke": "black"}},
        {"select": {"kind": "router"}, "style": {"fill": "navy"}},
    )
    router = resolve_style(StyleTarget(kind="router"), theme=theme)
    switch = resolve_style(StyleTarget(kind="switch"), theme=theme)
    assert router.fill == "#1e3a8a"
    assert router.stroke == "#111827", "the background rule still answers for the rest"
    assert switch.fill == "#ffffff"


@pytest.mark.parametrize(
    "pattern, namespace, matches",
    [
        ("sites/*", "sites/hq", True),
        ("sites/*", "sites/hq/access", False),
        ("sites/**", "sites/hq/access", True),
        ("sites/**", "sites", False),
        ("*", "hosts", True),
        ("*", "sites/hq", False),
    ],
)
def test_a_namespace_glob_does_not_cross_a_separator_unless_asked(
    pattern: str, namespace: str, matches: bool
) -> None:
    """A namespace is a path, and ``sites/*`` has to be able to mean one level."""
    theme = theme_of({"select": {"namespace": pattern}, "style": {"fill": "navy"}})
    resolved = resolve_style(StyleTarget(kind="switch", namespace=namespace), theme=theme)
    assert (resolved.fill == "#1e3a8a") is matches


def test_a_label_clause_may_ask_for_presence_alone() -> None:
    theme = theme_of({"select": {"label": {"tier": "*"}}, "style": {"fill": "navy"}})
    assert resolve_style(StyleTarget(kind="switch", labels={"tier": "x"}), theme=theme).fill
    assert not resolve_style(StyleTarget(kind="switch"), theme=theme).fill


def test_every_clause_of_a_selector_must_hold() -> None:
    theme = theme_of(
        {"select": {"kind": "switch", "namespace": "sites/hq"}, "style": {"fill": "navy"}}
    )
    inside = StyleTarget(kind="switch", namespace="sites/hq")
    elsewhere = StyleTarget(kind="switch", namespace="sites/branch")
    wrong_kind = StyleTarget(kind="router", namespace="sites/hq")
    assert resolve_style(inside, theme=theme).fill == "#1e3a8a"
    assert resolve_style(elsewhere, theme=theme).fill is None
    assert resolve_style(wrong_kind, theme=theme).fill is None


def test_role_is_shorthand_for_the_role_label() -> None:
    by_role = theme_of({"select": {"role": "core"}, "style": {"fill": "navy"}})
    by_label = theme_of({"select": {"label": {"role": "core"}}, "style": {"fill": "navy"}})
    target = StyleTarget(kind="switch", labels={"role": "core"})
    assert resolve_style(target, theme=by_role).fill == resolve_style(target, theme=by_label).fill


def test_a_clause_accepts_a_bare_string_as_well_as_a_list() -> None:
    one = theme_of({"select": {"kind": "switch"}, "style": {"fill": "navy"}})
    many = theme_of({"select": {"kind": ["switch", "router"]}, "style": {"fill": "navy"}})
    target = StyleTarget(kind="switch")
    assert resolve_style(target, theme=one).fill == resolve_style(target, theme=many).fill


def test_no_style_skips_the_two_top_rungs_and_keeps_the_two_below() -> None:
    theme = theme_of({"select": {"kind": "switch"}, "style": {"fill": "green"}})
    resolved = resolve_style(
        StyleTarget(kind="switch", style=Style(fill="navy")),
        theme=theme,
        defaults={"fill": "#f5f5f5"},
        styling=False,
    )
    assert resolved.fill == "#f5f5f5"
    assert dict(resolved.origin) == {"fill": "default"}


def test_an_inline_theme_is_appended_so_it_wins_a_tie() -> None:
    """A ``netviz.toml`` has the last word without restating what it agrees with."""
    named = theme_of({"select": {"kind": "switch"}, "style": {"fill": "green"}}, name="named")
    inline = theme_of({"select": {"kind": "switch"}, "style": {"fill": "navy"}}, name="inline")
    combined = resolve_theme(named, inline=inline)
    assert combined is not None
    assert resolve_style(StyleTarget(kind="switch"), theme=combined).fill == "#1e3a8a"
    # But it does not beat a *more specific* rule in the theme it extends.
    specific = theme_of(
        {"select": {"kind": "switch", "role": "core"}, "style": {"fill": "red"}}, name="named"
    )
    beaten = resolve_theme(specific, inline=inline)
    assert beaten is not None
    target = StyleTarget(kind="switch", labels={"role": "core"})
    assert resolve_style(target, theme=beaten).fill == "#dc2626"


def test_resolving_with_no_named_theme_still_uses_the_inline_one() -> None:
    inline = theme_of({"style": {"fill": "navy"}})
    assert resolve_theme(None, inline=inline) is inline
    assert resolve_theme(None) is None


# --------------------------------------------------------------------------- #
# Opacity, which is the one field that changes a colour rather than an attribute
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "colour, opacity, expected",
    [
        ("#1e3a8a", 0.5, "#1e3a8a80"),
        ("#abc", 0.5, "#aabbcc80"),
        ("#1e3a8a", 1, "#1e3a8a"),
        ("#1e3a8a", 0, "#1e3a8a00"),
        ("none", 0.5, "none"),
        (None, 0.5, None),
        ("#1e3a8a", None, "#1e3a8a"),
    ],
)
def test_opacity_folds_into_the_alpha_channel(
    colour: str | None, opacity: float | None, expected: str | None
) -> None:
    assert fade(colour, opacity) == expected


# --------------------------------------------------------------------------- #
# Themes as documents
# --------------------------------------------------------------------------- #


def test_a_bundled_theme_is_keyed_by_the_name_it_calls_itself() -> None:
    """``--theme blueprint`` and "rule 4 of theme blueprint" must be one name."""
    for key, theme in BUNDLED_THEMES.items():
        assert theme.name == key


def test_both_bundled_themes_load_and_style_something() -> None:
    assert set(BUNDLED_THEMES) == {"blueprint", "mono"}
    inventory = load_tree(HOME_LAB)
    graph = build_graph(inventory, layer=Layer.L1)
    for name, theme in BUNDLED_THEMES.items():
        styles = StyleMap.build(graph, theme=theme)
        origins = {origin for style in styles.nodes.values() for origin in style.origin.values()}
        assert any(origin.startswith("theme:") for origin in origins), name


def test_a_theme_with_no_rules_is_refused() -> None:
    """It would validate, render identically to nothing, and say so to nobody."""
    with pytest.raises(SchemaError) as caught:
        parse_theme(
            {
                "apiVersion": "netviz.dev/v1alpha1",
                "kind": THEME_KIND,
                "metadata": {"name": "empty"},
                "spec": {"rules": []},
            }
        )
    assert "NV-Z004" in str(caught.value)


def test_a_theme_kept_beside_the_inventory_is_checked_but_not_applied() -> None:
    """The obvious place to keep a stylesheet must not be an error (§22.3)."""
    inventory = load_tree(STYLED)
    assert not inventory.errors, [str(error) for error in inventory.errors]
    assert "house" in inventory.themes
    # And it is not applied by being there: a render that names no theme draws
    # the palette's answer, not the file's.
    graph = build_graph(inventory, layer=Layer.L1)
    plain = StyleMap.build(graph)
    assert not any(
        origin.startswith("theme:")
        for style in plain.nodes.values()
        for origin in style.origin.values()
    )


def test_a_theme_that_is_not_a_file_is_a_render_error() -> None:
    with pytest.raises(RenderError) as caught:
        load_theme("no-such-theme")
    assert "blueprint" in str(caught.value), "the message lists what is built in"


def test_a_theme_that_is_a_directory_says_so_and_names_the_other_flag(tmp_path: Path) -> None:
    """``--icons`` takes a directory and ``--theme`` takes a file; the two get mixed up."""
    with pytest.raises(RenderError) as caught:
        load_theme(str(tmp_path))
    assert "--icons" in str(caught.value)


def test_a_theme_file_with_a_bad_colour_names_the_nearest_spelling(tmp_path: Path) -> None:
    path = tmp_path / "theme.yaml"
    path.write_text(
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: theme\n"
        "metadata: {name: t}\n"
        "spec:\n"
        "  rules:\n"
        "    - style: {fill: navvy}\n",
        encoding="utf-8",
    )
    with pytest.raises(RenderError) as caught:
        load_theme(str(path))
    assert "did you mean 'navy'?" in str(caught.value)


def test_an_inline_theme_table_is_parsed_by_the_same_models() -> None:
    config = parse_config(
        {"theme": {"rules": [{"select": {"kind": "router"}, "style": {"fill": "red"}}]}}
    )
    assert config.theme is not None
    assert len(config.theme.rules) == 1


def test_a_bad_inline_theme_table_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as caught:
        parse_config({"theme": {"rules": [{"style": {"fill": "reddd"}}]}})
    assert "did you mean 'red'?" in str(caught.value)


def test_an_unknown_key_in_the_theme_table_is_a_typo_not_a_feature() -> None:
    with pytest.raises(ConfigurationError) as caught:
        parse_config({"theme": {"rulez": []}})
    assert "rulez" in str(caught.value)


# --------------------------------------------------------------------------- #
# What reaches the backends
# --------------------------------------------------------------------------- #


def styled_graph(**options: Any) -> str:
    inventory = load_tree(STYLED)
    graph = build_graph(inventory, layer=Layer.L1)
    return to_dot(graph, RenderOptions(**options))


def test_a_declared_style_reaches_the_dot_attributes() -> None:
    theme = load_theme(str(STYLED / "theme.yaml"))
    source = styled_graph(theme=theme)
    core = next(line for line in source.splitlines() if '"sites/hq/rtr-core"' in line)
    assert 'fillcolor="#1e3a8a"' in core
    assert "shape=hexagon" in core
    assert "penwidth=4" in core
    assert "fontsize=14" in core
    assert 'fontcolor="#ffffff"' in core


def test_a_styled_cable_reaches_the_edge_attributes() -> None:
    theme = load_theme(str(STYLED / "theme.yaml"))
    source = styled_graph(theme=theme)
    line = next(
        row for row in source.splitlines() if '"sites/hq/rtr-core" -- "sites/hq/sw-hq-a"' in row
    )
    assert 'color="#dc2626"' in line
    assert "style=dotted" in line
    assert "penwidth=3" in line


def test_no_style_draws_the_plain_diagram() -> None:
    """The escape hatch drops *both* top rungs, not only the theme.

    A ``--no-style`` that still honoured ``spec.style`` would answer the wrong
    question: the one it is for is "is this diagram odd because of the network
    or because of what somebody wrote about how it looks?".
    """
    theme = load_theme(str(STYLED / "theme.yaml"))
    plain = styled_graph(styling=False)
    assert styled_graph(theme=theme, styling=False) == plain
    assert 'fillcolor="#1e3a8a"' not in plain, "the element's own style goes too"
    assert 'fillcolor="#dbe9f6"' in plain, "and the palette answers instead"
    # ...which is a different picture from the one the same tree draws with no
    # theme named, because the elements still speak there.
    assert styled_graph() != plain


def test_an_unstyled_inventory_is_unchanged_by_the_feature_existing() -> None:
    """The claim the goldens make, made once directly."""
    inventory = load_tree(HOME_LAB)
    graph = build_graph(inventory, layer=Layer.L1)
    styles = StyleMap.build(graph)
    for style in styles.nodes.values():
        assert set(style.origin.values()) == {"default"}
    assert PLAIN.to_dict() == {"from": {}}


def test_the_json_export_publishes_the_resolved_style_and_its_provenance(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    output = tmp_path / "graph.json"
    result = runner.invoke(
        cli,
        [
            "-i",
            str(STYLED),
            "render",
            "-f",
            "json",
            "--theme",
            str(STYLED / "theme.yaml"),
            "-o",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(output.read_text(encoding="utf-8"))
    core = next(node for node in document["nodes"] if node["id"] == "sites/hq/rtr-core")
    assert core["style"]["fill"] == "#1e3a8a"
    assert core["style"]["from"]["fill"] == "element"
    switch = next(node for node in document["nodes"] if node["id"] == "sites/hq/sw-hq-a")
    assert switch["style"]["from"]["fill"].startswith("theme:house#")
    cable = next(edge for edge in document["edges"] if edge["id"] == "cables/cbl-core-hq")
    assert cable["style"]["stroke"] == "#dc2626"


# --------------------------------------------------------------------------- #
# draw.io: a colour has to survive the trip
# --------------------------------------------------------------------------- #


def test_the_drawio_style_carries_the_resolved_colours() -> None:
    style = ResolvedStyle(
        fill="#1e3a8a", stroke="#ffffff", font_color="#ffffff", shape="hexagon", font_size=14
    )
    mx = node_style(style)
    assert "fillColor=#1e3a8a;" in mx
    assert "strokeColor=#ffffff;" in mx
    assert "fontColor=#ffffff;" in mx
    assert "fontSize=14;" in mx
    assert MX_SHAPES["hexagon"] in mx
    # An mxGraph style is split on both characters, so neither may appear inside
    # a value. That is the whole reason the vocabulary is closed.
    for declaration in (part for part in mx.split(";") if part):
        assert declaration.count("=") == 1, declaration


def test_a_drawio_edge_carries_the_resolved_colour_and_pattern() -> None:
    mx = edge_style(ResolvedStyle(stroke="#dc2626", dash="dotted", stroke_width=3))
    assert "strokeColor=#dc2626;" in mx
    assert "dashed=1;" in mx
    assert "strokeWidth=3;" in mx


def test_opacity_reaches_drawio_as_a_percentage() -> None:
    mx = node_style(ResolvedStyle(fill="#ffffff", opacity=0.4))
    assert "opacity=40;" in mx


@pytest.mark.parametrize("view", ["l1", "l2"])
def test_a_drawio_export_preserves_every_colour_it_was_given(view: str) -> None:
    """The round-trip property, asserted where it can actually be checked.

    Not "export then import then compare" — the importer deliberately reads back
    topology and geometry and *not* appearance, because a draw.io user recolouring
    a box has not changed the network. What has to hold is the half that a
    reader sees: every colour the resolver decided on is in the file, spelled
    the same way, so opening it shows the diagram netviz drew.
    """
    from netviz.drawio.build import BuildOptions, build_diagram
    from netviz.drawio.mxfile import write_mxfile

    inventory = load_tree(STYLED)
    graph = build_graph(inventory, layer=Layer(view))
    theme = load_theme(str(STYLED / "theme.yaml"))
    styles = StyleMap.build(graph, theme=theme, output="svg")
    payload = write_mxfile(
        build_diagram(graph, inventory, BuildOptions(view=view, theme=theme)), compress=False
    )
    for fqn in graph.nodes:
        style = styles.node(fqn)
        assert style.fill is not None
        assert f"fillColor={style.fill};" in payload, fqn
        assert f"strokeColor={style.stroke};" in payload, fqn
    for index, edge in enumerate(graph.edges):
        stroke = styles.edge(index).faded_stroke
        assert f"strokeColor={stroke};" in payload, edge.id


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


def test_a_theme_that_does_not_exist_is_a_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["-i", str(STYLED), "render", "--theme", "nope"])
    assert result.exit_code == 2
    assert "unknown theme" in result.output


def test_the_theme_and_style_settings_come_from_the_config_file(tmp_path: Path) -> None:
    root = tmp_path / "inventory"
    root.mkdir()
    for source in sorted(STYLED.rglob("*.yaml")):
        target = root / source.relative_to(STYLED)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "netviz.toml").write_text('[render]\ntheme = "theme.yaml"\n', encoding="utf-8")

    runner = CliRunner()
    themed = runner.invoke(cli, ["-i", str(root), "render", "-f", "dot"])
    assert themed.exit_code == 0, themed.output
    assert 'fillcolor="#1e3a8a"' in themed.output, "a relative theme resolves against the file"

    # And the flag turns it back off without the file having to change.
    plain = runner.invoke(cli, ["-i", str(root), "render", "-f", "dot", "--no-style"])
    assert plain.exit_code == 0, plain.output
    assert 'fillcolor="#1e3a8a"' not in plain.output


def test_the_web_command_takes_a_theme() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["web", "--help"])
    assert result.exit_code == 0
    assert "--theme" in result.output
