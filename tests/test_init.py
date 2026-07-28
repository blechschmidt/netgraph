"""``netgraph init``: the tree it writes has to be one netgraph itself accepts.

The whole promise of the command is that the two lines it prints at the end
succeed before anything has been edited, so the assertions here are the same
ones a new user makes in their first minute: the scaffolded tree validates
clean, it renders at every layer, its ``netgraph.toml`` parses, and every
document points at a JSON Schema that is actually there.

The refusal path matters just as much. Scaffolding is a convenience over files
someone may have typed by hand, so writing into an occupied directory needs
``--force`` and must leave what is there untouched without it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from netgraph.cli import cli, main
from netgraph.config import CONFIG_FILE_NAME, ValidationConfig, load_config
from netgraph.loader import load_tree
from netgraph.scaffold import (
    GITIGNORE_FILE_NAME,
    SCHEMA_FILE_NAME,
    ScaffoldError,
    build_scaffold,
    write_scaffold,
)
from netgraph.schema import build_schema

requires_dot = pytest.mark.skipif(
    shutil.which("dot") is None, reason="Graphviz 'dot' is not installed"
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str) -> Result:
    return runner.invoke(cli, list(args), catch_exceptions=False)


@pytest.fixture
def inventory(runner: CliRunner, tmp_path: Path) -> Path:
    """A default ``netgraph init`` in a directory of its own."""
    target = tmp_path / "my-network"
    result = invoke(runner, "init", str(target))
    assert result.exit_code == 0, result.output
    return target


def documents(root: Path) -> list[Path]:
    return sorted(root.rglob("*.yaml"))


# --------------------------------------------------------------------------- #
# The tree is usable
# --------------------------------------------------------------------------- #


def test_the_scaffolded_tree_validates_clean(runner: CliRunner, inventory: Path) -> None:
    result = invoke(runner, "-i", str(inventory), "validate")
    assert result.exit_code == 0
    assert "no problems found" in result.output


def test_the_scaffolded_tree_validates_clean_under_strict(
    runner: CliRunner, inventory: Path
) -> None:
    """Not one warning either, so a strict CI job accepts a fresh init."""
    result = invoke(runner, "-i", str(inventory), "validate", "--strict")
    assert result.exit_code == 0


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
def test_the_scaffolded_tree_renders_at_every_layer(
    runner: CliRunner, inventory: Path, layer: str
) -> None:
    result = invoke(runner, "-i", str(inventory), "render", "--layer", layer, "-f", "json")
    assert result.exit_code == 0
    graph = json.loads(result.stdout)
    assert graph["nodes"], f"layer {layer} drew nothing"
    assert graph["edges"], f"layer {layer} drew no adjacency"


@requires_dot
def test_the_command_the_next_steps_print_produces_an_svg(
    runner: CliRunner, inventory: Path
) -> None:
    """`netgraph render -f svg -o network.svg`, exactly as init advertises it."""
    output = inventory / "network.svg"
    result = invoke(runner, "-i", str(inventory), "render", "-f", "svg", "-o", str(output))
    assert result.exit_code == 0
    assert output.read_text(encoding="utf-8").lstrip().startswith("<?xml")


def test_the_tree_holds_a_router_a_switch_a_host_and_the_cables_between_them(
    inventory: Path,
) -> None:
    loaded = load_tree(inventory)
    assert not loaded.errors
    kinds = sorted(element.kind for element in loaded.elements.values())
    assert kinds == ["cable", "cable", "computer", "router", "switch"]


# --------------------------------------------------------------------------- #
# Editor wiring
# --------------------------------------------------------------------------- #


def test_every_document_carries_a_modeline_pointing_at_the_written_schema(
    inventory: Path,
) -> None:
    schema = inventory / SCHEMA_FILE_NAME
    assert schema.is_file()
    for document in documents(inventory):
        first = document.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("# yaml-language-server: $schema=")
        reference = first.partition("$schema=")[2]
        assert (document.parent / reference).resolve() == schema.resolve()


def test_the_written_schema_is_the_one_this_version_generates(inventory: Path) -> None:
    written = json.loads((inventory / SCHEMA_FILE_NAME).read_text(encoding="utf-8"))
    assert written == build_schema()


def test_the_documents_are_still_valid_yaml_under_the_modeline(inventory: Path) -> None:
    """A modeline is a comment, so it must not disturb the document stream."""
    for document in documents(inventory):
        stream = list(yaml.safe_load_all(document.read_text(encoding="utf-8")))
        assert all(item is None or item["apiVersion"] == "netgraph.dev/v1alpha1" for item in stream)


def test_no_schema_skips_the_wiring_entirely(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "bare"
    assert invoke(runner, "init", str(target), "--no-schema").exit_code == 0

    assert not (target / SCHEMA_FILE_NAME).exists()
    for document in documents(target):
        assert "yaml-language-server" not in document.read_text(encoding="utf-8")
    assert documents(target), "the tree itself is still written"


# --------------------------------------------------------------------------- #
# The supporting files
# --------------------------------------------------------------------------- #


def test_the_generated_config_parses_and_changes_nothing(inventory: Path) -> None:
    """Every key is commented out, so the file is present but inert."""
    config = load_config(inventory)
    assert config.path == inventory / CONFIG_FILE_NAME
    assert config.validation == ValidationConfig()


def test_the_generated_config_explains_the_keys_it_comments_out(inventory: Path) -> None:
    text = (inventory / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    for key in ("[validate]", "strict", "ignore", "[validate.severity]"):
        assert key in text


def test_the_gitignore_covers_rendered_output_but_not_the_schema(inventory: Path) -> None:
    patterns = {
        line.strip()
        for line in (inventory / GITIGNORE_FILE_NAME).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {"*.svg", "*.png", "*.pdf", "*.dot", "*.mmd"} <= patterns
    assert not any(pattern.endswith(".json") for pattern in patterns)


# --------------------------------------------------------------------------- #
# --minimal
# --------------------------------------------------------------------------- #


def test_minimal_declares_no_elements_and_still_validates(
    runner: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "minimal"
    assert invoke(runner, "init", str(target), "--minimal").exit_code == 0

    assert load_tree(target).elements == {}, "the envelope is a template, not a device"
    assert invoke(runner, "-i", str(target), "validate").exit_code == 0

    result = invoke(runner, "-i", str(target), "render", "-f", "json")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["nodes"] == []
    assert "declares no elements" in result.stderr, "and says why it drew nothing"


def test_minimal_still_wires_the_editor(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "minimal"
    assert invoke(runner, "init", str(target), "--minimal").exit_code == 0

    assert (target / SCHEMA_FILE_NAME).is_file()
    template = target / "devices" / "example.yaml"
    assert template.read_text(encoding="utf-8").startswith("# yaml-language-server: $schema=")


# --------------------------------------------------------------------------- #
# Refusing to overwrite
# --------------------------------------------------------------------------- #


def test_an_occupied_directory_is_refused_and_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Driven through ``main``, where a :class:`ScaffoldError` becomes a status."""
    target = tmp_path / "existing"
    target.mkdir()
    (target / CONFIG_FILE_NAME).write_text("# mine\n", encoding="utf-8")

    assert main(["init", str(target)]) == ScaffoldError.exit_code
    message = capsys.readouterr().err
    assert "--force" in message
    assert CONFIG_FILE_NAME in message
    assert (target / CONFIG_FILE_NAME).read_text(encoding="utf-8") == "# mine\n"
    assert not (target / "devices").exists()


def test_a_directory_occupied_by_something_unrelated_is_still_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing would be overwritten, but "empty or new" is the contract."""
    target = tmp_path / "busy"
    target.mkdir()
    (target / "notes.txt").write_text("keep me\n", encoding="utf-8")

    assert main(["init", str(target)]) == ScaffoldError.exit_code
    message = capsys.readouterr().err
    assert "not empty" in message
    assert "notes.txt" in message


def test_force_writes_over_what_is_there(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    (target / CONFIG_FILE_NAME).write_text("# mine\n", encoding="utf-8")
    (target / "notes.txt").write_text("keep me\n", encoding="utf-8")

    result = invoke(runner, "init", str(target), "--force")
    assert result.exit_code == 0
    assert "[validate]" in (target / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    assert (target / "notes.txt").exists(), "only the scaffolded files are touched"
    assert invoke(runner, "-i", str(target), "validate").exit_code == 0


def test_a_file_where_the_directory_should_go_is_a_usage_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "file.yaml"
    target.write_text("", encoding="utf-8")
    result = invoke(runner, "init", str(target))
    assert result.exit_code == 2


# --------------------------------------------------------------------------- #
# Where it writes
# --------------------------------------------------------------------------- #


def test_missing_parent_directories_are_created(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "net"
    assert invoke(runner, "init", str(target)).exit_code == 0
    assert (target / CONFIG_FILE_NAME).is_file()


def test_the_current_directory_is_the_default_target(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        result = invoke(runner, "init")
        assert result.exit_code == 0
        assert (Path(cwd) / CONFIG_FILE_NAME).is_file()
    assert "cd " not in result.output, "already here; a cd would be noise"


def test_the_report_names_every_file_and_the_next_two_commands(
    runner: CliRunner, tmp_path: Path
) -> None:
    target = tmp_path / "my-network"
    result = invoke(runner, "init", str(target))

    for relative in build_scaffold().paths:
        assert relative in result.output
    assert f"cd {target}" in result.output
    assert "netgraph validate" in result.output
    assert "netgraph render -f svg -o network.svg" in result.output


def test_quiet_scaffolds_without_a_word(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "quiet"
    result = invoke(runner, "-q", "init", str(target))
    assert result.exit_code == 0
    assert result.output == ""
    assert (target / CONFIG_FILE_NAME).is_file()


# --------------------------------------------------------------------------- #
# The scaffold itself
# --------------------------------------------------------------------------- #


def test_the_scaffold_is_built_without_touching_a_filesystem() -> None:
    scaffold = build_scaffold()
    assert scaffold.paths[0] == CONFIG_FILE_NAME
    assert SCHEMA_FILE_NAME in scaffold.files
    assert all(content.endswith("\n") for content in scaffold.files.values())


def test_a_target_that_is_a_file_is_refused_by_the_writer(tmp_path: Path) -> None:
    """The CLI's own type check is not the only guard; the API has one too."""
    target = tmp_path / "file"
    target.write_text("", encoding="utf-8")
    with pytest.raises(ScaffoldError, match="not a directory"):
        write_scaffold(build_scaffold(), target)


def test_a_target_that_cannot_be_listed_is_reported_rather_than_raised_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emptiness check reads the directory, and that read can fail too."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", refuse)
    with pytest.raises(ScaffoldError, match="cannot read"):
        write_scaffold(build_scaffold(), tmp_path)


def test_an_unwritable_target_is_reported_rather_than_raised_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk or a read-only mount is a diagnostic, not a traceback.

    Simulated rather than staged with permissions: the suite has to behave the
    same for the root user of a container, for whom mode 0o500 is no obstacle.
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "write_text", refuse)
    with pytest.raises(ScaffoldError, match="cannot write"):
        write_scaffold(build_scaffold(), tmp_path / "readonly")
