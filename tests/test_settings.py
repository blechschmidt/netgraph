"""``[render]``, ``[profile.<name>]`` and the precedence ladder between them.

Three things are asserted here, in that order:

* **Parsing.** Every key of ``[render]`` is understood, produces the shape the
  command body expects, and a typo or a wrong type is an error that names the
  file and the key rather than a value silently ignored.
* **Precedence.** Flag beats profile beats ``[render]`` beats built-in default,
  every rung of it — including the one that only exists because Click cannot
  tell "flag absent" from "flag given its default value" by inspection.
* **Discoverability.** ``netviz config show`` and ``--show-config`` say where
  each value came from, ``--profile`` completes, and ``netviz init``
  scaffolds an example that actually parses.

The promise that an inventory *without* a ``[render]`` table renders exactly as
before is asserted by the golden fixtures in ``tests/test_golden.py``, which
run against inventories that have no configuration file at all.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import click
import pytest
from click.shell_completion import ShellComplete
from click.testing import CliRunner, Result

from netviz.cli import CONFIGURABLE, cli, main
from netviz.completion import PROG_NAME, complete_profile

# The TOML parser netviz itself uses. Taken from there rather than imported
# directly because ``tomllib`` only entered the standard library in 3.11 and this
# package supports 3.10, where the dependency is ``tomli``: a bare ``import
# tomllib`` here made the whole module uncollectable on the oldest interpreter
# the project claims to support.
from netviz.config import CONFIG_FILE_NAME, load_config, parse_config, tomllib
from netviz.errors import ConfigurationError
from netviz.render import FORMATS, NODE_KINDS, RANKDIRS, Layer
from netviz.scaffold import build_scaffold
from netviz.settings import (
    SETTINGS,
    SETTINGS_BY_KEY,
    Origin,
    RenderConfig,
    describe_value,
    resolve_settings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CAMPUS = REPO_ROOT / "examples" / "campus"
DOCS = REPO_ROOT / "docs" / "configuration.md"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def write_config(root: Path, body: str) -> Path:
    path = root / CONFIG_FILE_NAME
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def inventory(tmp_path: Path) -> Path:
    """A minimal but real inventory, so a render can actually be run over it."""
    tree = tmp_path / "inv"
    (tree / "devices").mkdir(parents=True)
    (tree / "devices" / "sw.yaml").write_text(
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-core}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: eth0\n"
        "      type: ethernet\n"
        "      ipv4: {addresses: [10.0.0.1/24]}\n",
        encoding="utf-8",
    )
    return tree


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def test_every_key_is_the_long_flag_without_its_dashes() -> None:
    """The single naming rule the documentation states.

    A setting whose key drifts from its flag would need a mapping table in the
    reader's head, which is the thing this design exists to avoid.
    """
    render = cli.commands["render"]
    flags = {option for parameter in render.params for option in parameter.opts}
    for setting in SETTINGS:
        assert setting.flag in flags or f"--{setting.key}/--no-{setting.key}" in str(flags), (
            f"[render] {setting.key} mirrors no flag of 'netviz render'"
        )


def test_every_setting_feeds_a_parameter_render_declares() -> None:
    render = cli.commands["render"]
    declared = {parameter.name for parameter in render.params}
    for setting in SETTINGS:
        assert setting.param in declared, f"'netviz render' has no {setting.param} parameter"


def test_no_setting_can_redirect_output_or_bypass_validation() -> None:
    """The keys deliberately left out, asserted so nobody adds one by reflex."""
    forbidden = {"output", "force", "strict", "show_config", "profile"}
    assert {setting.param for setting in SETTINGS}.isdisjoint(forbidden)


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda setting: setting.key)
def test_every_setting_is_documented(setting: Any) -> None:
    reference = DOCS.read_text(encoding="utf-8")
    assert f"`{setting.key}`" in reference, (
        f"docs/configuration.md documents no '{setting.key}' key"
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_a_full_render_table_parses_into_click_shapes() -> None:
    config = parse_config(
        {
            "render": {
                "layer": ["l1", "l2"],
                "format": "svg",
                "namespace": "sites/north",
                "vlan": [10, 20],
                "kind": ["switch"],
                "name": ["sw-*"],
                "neighbors-of": "sw-core",
                "depth": 2,
                "collapse": "sites/south",
                "collapse-depth": 1,
                "bundle-links": True,
                "show-ips": False,
                "show-vlans": False,
                "group-by-namespace": True,
                "icons": "cisco",
                "tooltips": False,
                "link-template": "https://example.com/{file}#L{line}",
                "element-ids": True,
                "max-addresses": 2,
                "rankdir": "lr",
                "title": "Everything",
            }
        }
    )
    values = config.render.values
    assert values["layers"] == ("l1", "l2")
    assert values["output_format"] == "svg"
    # A bare scalar is a one-element list; the flag it mirrors is repeatable.
    assert values["namespaces"] == ("sites/north",)
    assert values["vlans"] == (10, 20)
    assert values["neighbors_of"] == "sw-core"
    assert values["bundle_links"] is True
    assert values["icons"] is not None and values["icons"].name == "cisco"
    assert values["link_template"].template == "https://example.com/{file}#L{line}"
    assert values["rankdir"] == "LR"  # normalised to what Graphviz spells
    assert values["max_addresses"] == 2


def test_an_absent_key_is_not_a_value() -> None:
    """Unset must be distinguishable from set-to-the-default."""
    config = parse_config({"render": {"show-ips": True}})
    assert "show_ips" in config.render
    assert "show_vlans" not in config.render


def test_no_render_table_is_an_empty_config() -> None:
    config = parse_config({"validate": {"strict": True}})
    assert not config.render and not config.profiles


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ({"show_ips": False}, "did you mean 'show-ips'"),
        ({"showips": False}, "expected one of"),
        ({"show-ips": "yes"}, "render.show-ips must be true or false, got string"),
        ({"depth": "two"}, "render.depth must be an integer, got string"),
        ({"depth": -1}, "render.depth must be at least 0, got -1"),
        ({"collapse-depth": 0}, "render.collapse-depth must be at least 1, got 0"),
        ({"vlan": [0]}, "render.vlan.[0] must be between 1 and 4094, got 0"),
        ({"format": "jpeg"}, "render.format must be one of"),
        ({"layer": ["l9"]}, "render.layer.[0] must be one of"),
        ({"kind": ["cable"]}, "render.kind.[0] must be one of"),
        ({"rankdir": "sideways"}, "render.rankdir must be one of"),
        ({"title": 3}, "render.title must be a string, got integer"),
        ({"title": True}, "render.title must be a string, got boolean"),
        ({"depth": True}, "render.depth must be an integer, got boolean"),
        ({"namespace": [3]}, "render.namespace.[0] must be a string, got integer"),
        ({"icons": "nonexistent-theme"}, "render.icons is not usable"),
        ({"link-template": "https://x/{nope}"}, "render.link-template is not usable"),
    ],
)
def test_a_bad_value_names_the_key(table: dict[str, Any], expected: str) -> None:
    with pytest.raises(ConfigurationError, match=re.escape(expected)):
        parse_config({"render": table})


def test_a_bad_value_names_the_file(tmp_path: Path) -> None:
    write_config(tmp_path, "[render]\ndepth = 'two'\n")
    with pytest.raises(ConfigurationError) as caught:
        load_config(tmp_path)
    assert str(tmp_path / CONFIG_FILE_NAME) in str(caught.value)


def test_the_render_table_must_be_a_table() -> None:
    with pytest.raises(ConfigurationError, match="render must be a table, got string"):
        parse_config({"render": "svg"})


def test_a_relative_icon_directory_resolves_against_the_file(tmp_path: Path) -> None:
    """The file lives with the inventory, not with the shell's cwd."""
    theme = tmp_path / "icons"
    theme.mkdir()
    (theme / "switch.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    write_config(tmp_path, "[render]\nicons = 'icons'\n")
    config = load_config(tmp_path)
    assert config.render.values["icons"].directory == theme


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def test_profiles_are_parsed_in_file_order() -> None:
    config = parse_config(
        {"profile": {"poster": {"format": "html"}, "review": {"collapse-depth": 1}}}
    )
    assert config.profile_names == ("poster", "review")
    assert config.profile("review").values == {"collapse_depth": 1}


def test_a_profile_is_validated_like_the_render_table() -> None:
    with pytest.raises(ConfigurationError, match=re.escape("profile.poster.format must be one of")):
        parse_config({"profile": {"poster": {"format": "jpeg"}}})


def test_an_unusable_profile_name_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="is not usable"):
        parse_config({"profile": {"-poster": {"format": "svg"}}})


def test_an_unknown_profile_lists_the_ones_that_exist() -> None:
    config = parse_config({"profile": {"poster": {}, "review": {}}})
    with pytest.raises(ConfigurationError, match=r"no profile 'postre'; .*poster, review"):
        config.profile("postre")


def test_an_unknown_profile_without_any_profiles_says_so() -> None:
    with pytest.raises(ConfigurationError, match="declares no"):
        parse_config({}).profile("poster")


def test_no_profile_asked_for_is_not_an_error() -> None:
    assert parse_config({}).profile(None) is None


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #


def resolve(
    *,
    params: dict[str, Any],
    given: frozenset[str] = frozenset(),
    render: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, tuple[Any, Origin]]:
    resolutions = resolve_settings(
        params=params,
        given=given,
        render=RenderConfig(values=render or {}),
        profile=None if profile is None else RenderConfig(name="p", values=profile),
    )
    return {item.setting.param: (item.value, item.origin) for item in resolutions}


def test_the_built_in_default_is_the_bottom_rung() -> None:
    assert resolve(params={"depth": 1}) == {"depth": (1, Origin.DEFAULT)}


def test_the_file_beats_the_built_in_default() -> None:
    assert resolve(params={"depth": 1}, render={"depth": 3}) == {"depth": (3, Origin.FILE)}


def test_the_profile_beats_the_file() -> None:
    resolved = resolve(params={"depth": 1}, render={"depth": 3}, profile={"depth": 5})
    assert resolved == {"depth": (5, Origin.PROFILE)}


def test_a_profile_inherits_what_it_does_not_override() -> None:
    resolved = resolve(
        params={"depth": 1, "show_ips": True},
        render={"depth": 3, "show_ips": False},
        profile={"depth": 5},
    )
    assert resolved["depth"] == (5, Origin.PROFILE)
    assert resolved["show_ips"] == (False, Origin.FILE)


def test_the_flag_beats_everything() -> None:
    resolved = resolve(
        params={"depth": 9},
        given=frozenset({"depth"}),
        render={"depth": 3},
        profile={"depth": 5},
    )
    assert resolved == {"depth": (9, Origin.FLAG)}


def test_a_flag_given_its_own_default_value_still_beats_the_file() -> None:
    """The rung that only exists because of ``get_parameter_source``.

    ``--depth 1`` and no ``--depth`` at all leave the same value in
    ``ctx.params``; only the parameter source distinguishes them, and a user who
    types a value must not be overruled by a file.
    """
    resolved = resolve(params={"depth": 1}, given=frozenset({"depth"}), render={"depth": 3})
    assert resolved == {"depth": (1, Origin.FLAG)}


def test_a_setting_the_command_does_not_take_is_skipped() -> None:
    """``web`` draws no filtered graph, so ``[render] vlan`` does not apply."""
    resolved = resolve(params={"icons": None}, render={"vlans": (10,), "icons": None})
    assert set(resolved) == {"icons"}


# --------------------------------------------------------------------------- #
# The ladder, end to end through the CLI
# --------------------------------------------------------------------------- #


def render_dot(runner: CliRunner, inventory: Path, *args: str) -> Result:
    return runner.invoke(cli, ["-i", str(inventory), "render", "-f", "dot", *args])


def test_the_render_table_changes_the_diagram(runner: CliRunner, inventory: Path) -> None:
    result = render_dot(runner, inventory)
    assert "rankdir=TB" in result.output

    write_config(inventory, "[render]\nrankdir = 'LR'\ntitle = 'From the file'\n")
    result = render_dot(runner, inventory)
    assert result.exit_code == 0
    assert "rankdir=LR" in result.output and "From the file" in result.output


def test_a_profile_overrides_the_render_table(runner: CliRunner, inventory: Path) -> None:
    write_config(
        inventory,
        "[render]\ntitle = 'base'\nrankdir = 'LR'\n\n[profile.poster]\ntitle = 'poster'\n",
    )
    result = render_dot(runner, inventory, "--profile", "poster")
    assert result.exit_code == 0
    assert "poster" in result.output
    # Inherited from [render] rather than reset to the built-in default.
    assert "rankdir=LR" in result.output


def test_a_flag_overrides_the_profile(runner: CliRunner, inventory: Path) -> None:
    write_config(inventory, "[render]\ntitle = 'base'\n\n[profile.poster]\ntitle = 'poster'\n")
    result = render_dot(runner, inventory, "--profile", "poster", "--title", "typed")
    assert "typed" in result.output and "poster" not in result.output


def test_an_explicit_default_value_beats_the_file(runner: CliRunner, inventory: Path) -> None:
    write_config(inventory, "[render]\nmax-addresses = 1\n")
    resolved = runner.invoke(
        cli, ["-i", str(inventory), "render", "--max-addresses", "4", "--show-config"]
    )
    assert re.search(r"max-addresses\s+4\s+flag --max-addresses", resolved.output)


def test_an_unknown_profile_is_reported_and_nothing_is_drawn(
    inventory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Driven through ``main``, where the exit status is decided."""
    write_config(inventory, "[profile.poster]\ntitle = 'poster'\n")
    status = main(["-i", str(inventory), "render", "-f", "dot", "--profile", "nope"])
    captured = capsys.readouterr()
    assert status == ConfigurationError.exit_code
    assert "no profile 'nope'" in captured.err
    assert "graph netviz" not in captured.out


def test_a_broken_file_refuses_the_render(
    inventory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(inventory, "[render]\nrankdir = 'sideways'\n")
    status = main(["-i", str(inventory), "render", "-f", "dot"])
    assert status == ConfigurationError.exit_code
    assert "rankdir must be one of" in capsys.readouterr().err


def test_the_layer_default_reaches_the_graph(runner: CliRunner, inventory: Path) -> None:
    write_config(inventory, "[render]\nlayer = 'l3'\n")
    result = runner.invoke(cli, ["-i", str(inventory), "render", "-f", "json"])
    assert result.exit_code == 0
    assert '"layer": "l3"' in result.output


def test_the_format_default_reaches_the_renderer(runner: CliRunner, inventory: Path) -> None:
    write_config(inventory, "[render]\nformat = 'mermaid'\n")
    result = runner.invoke(cli, ["-i", str(inventory), "render"])
    assert result.exit_code == 0
    assert "flowchart TB" in result.output


def test_path_takes_the_display_defaults(runner: CliRunner) -> None:
    """``path --highlight`` draws with the same options ``render`` does."""
    result = runner.invoke(
        cli, ["-i", str(CAMPUS), "path", "pc-north-01", "pc-north-02", "--show-config"]
    )
    assert result.exit_code == 0
    assert "show-ips" in result.output
    # The trace report's -F is not the diagram's -f, so 'format' is not offered.
    assert "\nformat " not in result.output


@pytest.mark.parametrize("command", CONFIGURABLE)
def test_every_configurable_command_takes_a_profile(command: str) -> None:
    options = {opt for parameter in cli.commands[command].params for opt in parameter.opts}
    assert "--profile" in options and "--show-config" in options


# --------------------------------------------------------------------------- #
# Discoverability
# --------------------------------------------------------------------------- #


def test_config_show_reports_the_provenance_of_every_rung(
    runner: CliRunner, inventory: Path
) -> None:
    write_config(
        inventory,
        "[validate]\nstrict = true\n\n[render]\nrankdir = 'LR'\n\n"
        "[profile.review]\nshow-ips = false\n",
    )
    result = runner.invoke(
        cli, ["-i", str(inventory), "config", "show", "render", "--profile", "review"]
    )
    assert result.exit_code == 0
    assert "strict    true    file [validate]" in result.output
    assert re.search(r"rankdir\s+LR\s+file \[render\]", result.output)
    assert re.search(r"show-ips\s+false\s+profile review", result.output)
    assert re.search(r"depth\s+1\s+default", result.output)
    assert "profiles declared: review" in result.output


def test_config_show_without_a_file_says_so(runner: CliRunner, inventory: Path) -> None:
    result = runner.invoke(cli, ["-i", str(inventory), "config", "show"])
    assert result.exit_code == 0
    assert "built-in defaults in use" in result.output


def test_config_show_defaults_to_render(runner: CliRunner, inventory: Path) -> None:
    assert (
        "settings for 'netviz render'"
        in runner.invoke(cli, ["-i", str(inventory), "config", "show"]).output
    )


@pytest.mark.parametrize("command", CONFIGURABLE)
def test_config_show_resolves_every_configurable_command(
    runner: CliRunner, inventory: Path, command: str
) -> None:
    result = runner.invoke(cli, ["-i", str(inventory), "config", "show", command])
    assert result.exit_code == 0, result.output
    assert f"settings for 'netviz {command}'" in result.output


def test_config_show_lists_only_what_web_takes(runner: CliRunner, inventory: Path) -> None:
    result = runner.invoke(cli, ["-i", str(inventory), "config", "show", "web"])
    assert "icons" in result.output and "collapse-depth" not in result.output


def test_config_show_rejects_an_unknown_profile(
    inventory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(inventory, "[profile.poster]\ntitle = 'x'\n")
    status = main(["-i", str(inventory), "config", "show", "--profile", "nope"])
    assert status == ConfigurationError.exit_code
    assert "no profile 'nope'" in capsys.readouterr().err


def test_show_config_draws_nothing(runner: CliRunner, inventory: Path) -> None:
    result = runner.invoke(cli, ["-i", str(inventory), "render", "--show-config"])
    assert result.exit_code == 0
    assert "graph netviz" not in result.output


@pytest.mark.parametrize("command", ["render", "watch", "path", "web"])
def test_show_config_exits_before_doing_any_work(
    runner: CliRunner, inventory: Path, command: str
) -> None:
    """A watch that started a filesystem loop, or a web server that bound a
    port, would make ``--show-config`` unusable as a quick question."""
    arguments = ["a", "b"] if command == "path" else []
    result = runner.invoke(cli, ["-i", str(inventory), command, *arguments, "--show-config"])
    assert result.exit_code == 0, result.output
    assert f"settings for 'netviz {command}'" in result.output


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #


def complete(args: list[str], incomplete: str = "") -> list[str]:
    completer = ShellComplete(cli, {}, PROG_NAME, "_NETVIZ_COMPLETE")
    return [item.value for item in completer.get_completions(args, incomplete)]


def test_profiles_complete_from_the_inventorys_file(inventory: Path) -> None:
    write_config(inventory, "[profile.poster]\ntitle = 'x'\n\n[profile.review]\nshow-ips = false\n")
    assert complete(["-i", str(inventory), "render", "--profile"]) == ["poster", "review"]


def test_a_completed_profile_says_what_it_overrides(inventory: Path) -> None:
    write_config(inventory, "[profile.review]\nshow-ips = false\ncollapse-depth = 1\n")
    context = click.Context(cli.commands["render"])
    context.params["inventory"] = inventory
    items = complete_profile(context, click.Option(["--profile"]), "")
    assert items[0].help == "sets collapse-depth, show-ips"


def test_a_broken_configuration_offers_nothing_rather_than_failing(inventory: Path) -> None:
    write_config(inventory, "[profile.review]\nshow-ips = 'maybe'\n")
    assert complete(["-i", str(inventory), "render", "--profile"]) == []


def test_an_inventory_without_a_file_offers_nothing(inventory: Path) -> None:
    assert complete(["-i", str(inventory), "render", "--profile"]) == []


@pytest.mark.parametrize("command", CONFIGURABLE)
def test_the_profile_flag_is_offered_by_every_configurable_command(command: str) -> None:
    assert "--profile" in complete([command], "--pro")


# --------------------------------------------------------------------------- #
# The scaffold
# --------------------------------------------------------------------------- #


def test_the_scaffolded_example_parses_once_uncommented() -> None:
    """A commented example nobody runs is an example that rots.

    Every commented *setting* of the generated ``netviz.toml`` — a table
    header or a ``key = value``, as distinct from the prose around them — is
    uncommented and fed back through the parser, so a key renamed in
    :data:`SETTINGS` without the scaffold following fails here.
    """
    generated = build_scaffold().files[CONFIG_FILE_NAME]
    body = "\n".join(_uncommented(generated))
    config = parse_config(tomllib.loads(body))
    assert config.profile_names == ("poster", "review")
    assert config.render.values["rankdir"] == "LR"
    assert config.render.values["layers"] == ("l2",)
    assert config.validation.strict is False


def _uncommented(text: str) -> list[str]:
    """The commented-out TOML of the scaffold, with the prose left behind."""
    toml = re.compile(r"^(\[[a-z][\w.]*\]|[a-z][a-z0-9-]* *=)")
    lines = [line.removeprefix("# ") for line in text.splitlines() if line.startswith("# ")]
    return [line for line in lines if toml.match(line)]


def test_the_scaffold_mentions_the_render_table() -> None:
    generated = build_scaffold().files[CONFIG_FILE_NAME]
    assert "[render]" in generated and "[profile.poster]" in generated


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "(unset)"),
        (True, "true"),
        (False, "false"),
        ((), "(none)"),
        (("l1", "l2"), "l1, l2"),
        (frozenset({20, 10}), "10, 20"),
        (4, "4"),
        ("LR", "LR"),
    ],
)
def test_a_value_is_shown_as_one_readable_cell(value: Any, expected: str) -> None:
    assert describe_value(value) == expected


def test_the_choices_come_from_the_registries_they_belong_to() -> None:
    """A format or a layer added elsewhere is accepted here without an edit."""
    for name in FORMATS:
        assert parse_config({"render": {"format": name}}).render.values["output_format"] == name
    for layer in Layer:
        assert parse_config({"render": {"layer": layer.value}}).render.values["layers"] == (
            layer.value,
        )
    for kind in NODE_KINDS:
        assert parse_config({"render": {"kind": kind}}).render.values["kinds"] == (kind,)
    for rankdir in RANKDIRS:
        assert parse_config({"render": {"rankdir": rankdir}}).render.values["rankdir"] == rankdir


def test_the_keys_a_profile_sets_are_reported_in_registry_order() -> None:
    block = parse_config({"profile": {"p": {"title": "x", "layer": "l1"}}}).profile("p")
    assert block.keys == ("layer", "title")
    assert list(SETTINGS_BY_KEY).index("layer") < list(SETTINGS_BY_KEY).index("title")


def test_a_toml_date_is_reported_by_its_own_type_name() -> None:
    """TOML has types netviz has no key for; the diagnostic still names one."""
    import datetime

    expected = re.escape("render.title must be a string, got date")
    with pytest.raises(ConfigurationError, match=expected):
        parse_config({"render": {"title": datetime.date(2026, 7, 29)}})


def test_the_profile_table_must_be_a_table_of_tables() -> None:
    with pytest.raises(ConfigurationError, match="profile must be a table of named profiles"):
        parse_config({"profile": "poster"})


def test_an_origin_prints_as_the_word_the_report_uses() -> None:
    assert [str(origin) for origin in Origin] == ["flag", "profile", "file", "default"]


def test_a_theme_and_a_template_are_shown_by_name() -> None:
    values = parse_config(
        {"render": {"icons": "cisco", "link-template": "https://x/{file}"}}
    ).render.values
    assert describe_value(values["icons"]) == "cisco"
    assert describe_value(values["link_template"]) == "https://x/{file}"


def test_an_absolute_icon_directory_is_taken_as_it_stands(tmp_path: Path) -> None:
    theme = tmp_path / "elsewhere"
    theme.mkdir()
    (theme / "router.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    write_config(tmp_path, f"[render]\nicons = '{theme}'\n")
    assert load_config(tmp_path).render.values["icons"].directory == theme
