"""``--annotations/--no-annotations``: the §21 layer as a command-line choice.

The annotation layer decides whether a drawing carries the *commentary* written
about it — the notes, areas and legends of §21 — beside the topology. On by
default, because somebody wrote those down about this diagram; off for the
printed page an audit reads, which wants the network and nothing said about it.

What is asserted here is only the wiring: that the flag reaches every command
that draws, that ``netviz.toml`` can set it per inventory and per profile, and
that an explicit flag still beats the file. *How* an annotation is drawn belongs
to the renderer's own tests; this module never looks at a shape, only at whether
the text somebody wrote reached the output at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from netviz.cli import cli
from netviz.config import CONFIG_FILE_NAME
from netviz.render.options import RenderOptions
from netviz.settings import SETTINGS_BY_KEY

#: The note's text, chosen so it cannot collide with a device name, a namespace
#: or anything Graphviz emits of its own accord.
CALLOUT = "the annexe runs on its own feed"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def inventory(tmp_path: Path) -> Path:
    """Two cabled switches and one note anchored to the first of them."""
    tree = tmp_path / "inv"
    tree.mkdir(parents=True)
    (tree / "net.yaml").write_text(
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-a}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: port1\n"
        "      type: ethernet\n"
        "      ipv4: [10.0.0.1/24]\n"
        "---\n"
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: switch\n"
        "metadata: {name: sw-b}\n"
        "spec:\n"
        "  interfaces:\n"
        "    - name: port1\n"
        "      type: ethernet\n"
        "      ipv4: [10.0.0.2/24]\n"
        "---\n"
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: cable\n"
        "metadata: {name: cbl-a-b}\n"
        "spec:\n"
        "  endpoints: [sw-a:port1, sw-b:port1]\n"
        "  medium: copper\n"
        "---\n"
        "apiVersion: netviz.dev/v1alpha1\n"
        "kind: note\n"
        "metadata: {name: why-two-uplinks}\n"
        "spec:\n"
        f"  text: {CALLOUT}\n"
        "  anchor: {element: sw-a}\n",
        encoding="utf-8",
    )
    return tree


def render(runner: CliRunner, inventory: Path, *args: str) -> Result:
    result = runner.invoke(cli, ["-i", str(inventory), "render", "-f", "dot", *args])
    assert result.exit_code == 0, result.output
    return result


def write_config(root: Path, body: str) -> None:
    (root / CONFIG_FILE_NAME).write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# The default, and turning it off
# --------------------------------------------------------------------------- #


def test_the_default_is_on() -> None:
    """Nobody has to ask for the commentary they already wrote down."""
    assert RenderOptions().annotations is True


def test_a_render_carries_the_note_by_default(runner: CliRunner, inventory: Path) -> None:
    assert CALLOUT in render(runner, inventory).output


def test_no_annotations_leaves_the_topology_alone(runner: CliRunner, inventory: Path) -> None:
    """Off drops the commentary and *only* the commentary.

    The two switches and the cable between them are still there: this is a
    display option, not a filter, and a flag about labels that quietly changed
    the topology would be the one bug this whole distinction exists to prevent.
    """
    output = render(runner, inventory, "--no-annotations").output
    assert CALLOUT not in output
    assert "sw-a" in output and "sw-b" in output


@pytest.mark.parametrize("command", ["render", "watch", "path", "layout", "diff"])
def test_every_drawing_command_offers_the_flag(command: str) -> None:
    """A command that builds ``RenderOptions`` and cannot be told this is a gap."""
    params = {
        option
        for param in cli.commands[command].params
        for option in (*param.opts, *param.secondary_opts)
    }
    assert "--annotations" in params and "--no-annotations" in params


# --------------------------------------------------------------------------- #
# netviz.toml
# --------------------------------------------------------------------------- #


def test_the_key_mirrors_the_flag() -> None:
    assert SETTINGS_BY_KEY["annotations"].param == "annotations"


def test_the_render_table_turns_them_off(runner: CliRunner, inventory: Path) -> None:
    write_config(inventory, "[render]\nannotations = false\n")
    assert CALLOUT not in render(runner, inventory).output


def test_a_profile_overrides_the_render_table(runner: CliRunner, inventory: Path) -> None:
    """The printed default, with a profile for the screenshot that wants the callout."""
    write_config(
        inventory, "[render]\nannotations = false\n\n[profile.ticket]\nannotations = true\n"
    )
    assert CALLOUT in render(runner, inventory, "--profile", "ticket").output


def test_the_flag_beats_the_file(runner: CliRunner, inventory: Path) -> None:
    write_config(inventory, "[render]\nannotations = false\n")
    assert CALLOUT in render(runner, inventory, "--annotations").output


def test_show_config_says_where_the_value_came_from(runner: CliRunner, inventory: Path) -> None:
    write_config(inventory, "[render]\nannotations = false\n")
    result = runner.invoke(cli, ["-i", str(inventory), "render", "--show-config"])
    assert result.exit_code == 0, result.output
    line = next(row for row in result.output.splitlines() if row.startswith("annotations"))
    assert "false" in line and "[render]" in line


# --------------------------------------------------------------------------- #
# export drawio
# --------------------------------------------------------------------------- #


def export(runner: CliRunner, inventory: Path, fmt: str, *args: str) -> Result:
    return runner.invoke(cli, ["-i", str(inventory), "export", fmt, *args])


def test_a_drawio_export_carries_the_note_by_default(runner: CliRunner, inventory: Path) -> None:
    result = export(runner, inventory, "drawio")
    assert result.exit_code == 0, result.output
    assert CALLOUT in result.output


def test_a_drawio_export_can_be_asked_for_the_topology_alone(
    runner: CliRunner, inventory: Path
) -> None:
    result = export(runner, inventory, "drawio", "--no-annotations")
    assert result.exit_code == 0, result.output
    assert CALLOUT not in result.output
    assert "sw-a" in result.output


def test_the_flag_is_refused_by_a_format_that_draws_nothing(
    runner: CliRunner, inventory: Path
) -> None:
    """A silently ignored flag is worse than a usage error naming the format."""
    result = export(runner, inventory, "hosts", "--no-annotations")
    assert result.exit_code != 0
    assert "--annotations applies to 'drawio'" in result.output
