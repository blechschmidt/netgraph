"""Shell completion: the scripts, and the candidates they ask netgraph for.

The scripts themselves come from click, so what is asserted about them is that
they are generated for each supported shell and agree on the one thing netgraph
chooses — the ``_NETGRAPH_COMPLETE`` variable that ties script to executable.

The completers are ours, and they are exercised the way a shell exercises them:
through :class:`click.shell_completion.ShellComplete`, with the same argument
list a user would have typed. That is deliberate. Calling the functions
directly would prove they return the right strings while saying nothing about
whether ``-i`` was parsed into a context they can reach, which is the part that
breaks.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.shell_completion import CompletionItem, ShellComplete
from click.testing import CliRunner, Result

from netgraph.cli import cli
from netgraph.completion import PROG_NAME, SHELLS, complete_kind, completion_script
from netgraph.completion import _inventory_path as inventory_path
from netgraph.completion import _items as items
from netgraph.completion import _load_elements as load_elements
from netgraph.errors import NetgraphError
from netgraph.models import DOCUMENT_KINDS, KINDS
from netgraph.render import FORMATS, Layer
from netgraph.rules import RULE_IDS

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
QUICKSTART = EXAMPLES / "quickstart"
CAMPUS = EXAMPLES / "campus"

#: Every element of ``examples/quickstart``, fully qualified.
QUICKSTART_ELEMENTS = (
    "cables/cbl-rtr-sw",
    "cables/cbl-sw-alice",
    "devices/pc-alice",
    "devices/rtr-gw",
    "devices/sw-office",
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def complete(args: list[str], incomplete: str = "") -> list[CompletionItem]:
    """What the shell would be offered for ``netgraph <args> <incomplete><TAB>``."""
    completer = ShellComplete(cli, {}, PROG_NAME, "_NETGRAPH_COMPLETE")
    return list(completer.get_completions(args, incomplete))


def values(args: list[str], incomplete: str = "") -> list[str]:
    return [item.value for item in complete(args, incomplete)]


# --------------------------------------------------------------------------- #
# The scripts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shell", SHELLS)
def test_a_script_is_generated_for_every_supported_shell(shell: str) -> None:
    script = completion_script(shell, cli)
    assert script.endswith("\n")
    assert "_NETGRAPH_COMPLETE" in script, "the script must invoke netgraph's own protocol"
    assert PROG_NAME in script


@pytest.mark.parametrize("shell", SHELLS)
def test_the_command_prints_exactly_that_script(runner: CliRunner, shell: str) -> None:
    result: Result = runner.invoke(cli, ["completion", shell], catch_exceptions=False)
    assert result.exit_code == 0
    assert result.stdout == completion_script(shell, cli)


def test_the_zsh_script_registers_itself_with_compdef() -> None:
    """The one shell whose script is inert without it."""
    assert completion_script("zsh", cli).startswith("#compdef netgraph")


def test_an_unsupported_shell_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["completion", "tcsh"], catch_exceptions=False)
    assert result.exit_code == 2
    assert "bash" in result.output


def test_the_generator_refuses_a_shell_click_cannot_write() -> None:
    with pytest.raises(NetgraphError, match="no completion script"):
        completion_script("tcsh", cli)


def test_a_renamed_executable_gets_a_matching_variable() -> None:
    script = completion_script("bash", cli, prog_name="net-graph")
    assert "_NET_GRAPH_COMPLETE" in script


def test_the_shells_are_completed_for_the_completion_command_itself() -> None:
    assert values(["completion"]) == list(SHELLS)


# --------------------------------------------------------------------------- #
# Static value spaces
# --------------------------------------------------------------------------- #


def test_formats_come_from_the_renderer_registry_with_their_descriptions() -> None:
    items = complete(["render", "-f"])
    assert [item.value for item in items] == list(FORMATS)
    assert all(item.help for item in items), "zsh and fish show these next to the value"


def test_watch_completes_the_same_formats() -> None:
    assert values(["watch", "--format"]) == list(FORMATS)


@pytest.mark.parametrize("command", ["render", "watch"])
def test_the_interaction_flags_are_offered_by_both_commands(command: str) -> None:
    """``render`` and ``watch`` share one option set, so completion must agree."""
    offered = values([command], "--")
    for option in ("--tooltips", "--no-tooltips", "--link-template", "--element-ids"):
        assert option in offered, f"{option} is missing from '{command}' completion"


def test_a_prefix_narrows_to_the_link_flag() -> None:
    assert values(["render"], "--link") == ["--link-template"]


def test_layers_come_from_the_enum_and_say_what_they_draw() -> None:
    items = complete(["render", "--layer"])
    assert [item.value for item in items] == [layer.value for layer in Layer]
    described = {item.value: item.help or "" for item in items}
    assert "subnet" in described["l3"]
    assert "tunnel" in described["overlay"]
    assert "panel" in described["physical"]
    assert "rack" in described["rack"]


def test_a_filter_completes_the_kinds_that_are_nodes() -> None:
    """A cable is an edge, and so is a tunnel; neither can be selected."""
    items = complete(["render", "--kind"])
    assert [item.value for item in items] == [
        kind for kind in KINDS if kind not in {"cable", "tunnel"}
    ]
    assert all(item.help for item in items)


def test_the_schema_command_completes_every_kind_including_cable_and_template() -> None:
    assert values(["schema", "--kind"]) == list(DOCUMENT_KINDS)


def test_a_prefix_narrows_the_candidates() -> None:
    assert values(["render", "--kind"], "s") == ["switch", "server"]
    assert values(["render", "-f"], "p") == ["png", "pdf"]


def test_rules_are_completed_by_short_id_with_their_summaries() -> None:
    items = complete(["validate", "--disable"], "E0")
    assert [item.value for item in items] == [rule for rule in RULE_IDS if rule.startswith("E0")]
    assert all(item.help for item in items)


def test_the_wildcard_is_offered_before_the_individual_rules() -> None:
    assert values(["validate", "--disable"])[0] == "*"


def test_the_schema_aliases_are_offered_once_the_prefix_looks_like_one() -> None:
    aliases = values(["validate", "--disable"], "NG-C")
    assert aliases, "the NG-* vocabulary is accepted, so it must be completable"
    assert all(alias.startswith("NG-C") for alias in aliases)
    assert "NG-C002" in aliases
    assert not any(alias.startswith("NG") for alias in values(["validate", "--disable"], "E"))


def test_rule_completion_ignores_case() -> None:
    assert values(["validate", "--disable"], "e00") == values(["validate", "--disable"], "E00")
    assert "NG-C002" in values(["validate", "--disable"], "ng-c0")


# --------------------------------------------------------------------------- #
# The inventory
# --------------------------------------------------------------------------- #


def test_show_completes_every_element_of_the_inventory_named_by_i() -> None:
    offered = values(["-i", str(QUICKSTART), "show"])
    assert offered[: len(QUICKSTART_ELEMENTS)] == list(QUICKSTART_ELEMENTS)
    assert set(offered) == set(QUICKSTART_ELEMENTS) | {
        fqn.rpartition("/")[2] for fqn in QUICKSTART_ELEMENTS
    }, "both spellings resolve, so both are offered"


def test_an_element_carries_its_kind_as_the_description() -> None:
    helps = {item.value: item.help for item in complete(["-i", str(QUICKSTART), "show"])}
    assert helps["devices/rtr-gw"] == "router"
    assert helps["cbl-rtr-sw"] == "cable"


def test_neighbors_of_completes_nodes_but_never_a_cable() -> None:
    offered = values(["-i", str(QUICKSTART), "render", "--neighbors-of"])
    assert "devices/sw-office" in offered
    assert not any("cbl-" in name for name in offered)


def test_watch_completes_neighbours_too() -> None:
    assert "devices/sw-office" in values(["-i", str(QUICKSTART), "watch", "--neighbors-of"])


def test_a_namespace_prefix_narrows_to_that_subtree() -> None:
    offered = values(["-i", str(CAMPUS), "show"], "sites/north/")
    assert offered, "the campus tree is namespaced, so a path prefix must complete"
    assert all(name.startswith("sites/north/") for name in offered)


def test_a_short_name_prefix_reaches_a_nested_element() -> None:
    assert "sw-north-acc-01" in values(["-i", str(CAMPUS), "show"], "sw-north-")


def test_without_i_the_current_directory_is_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(QUICKSTART)
    assert values(["show"], "devices/") == [
        "devices/pc-alice",
        "devices/rtr-gw",
        "devices/sw-office",
    ]


def test_an_inventory_that_does_not_exist_offers_nothing_rather_than_failing(
    tmp_path: Path,
) -> None:
    """A completer runs on every <TAB>; it must never raise or print."""
    assert load_elements(tmp_path / "nowhere", nodes_only=False) == {}


def test_a_broken_document_still_completes_what_did_load(tmp_path: Path) -> None:
    """Half-written YAML is the normal state of a tree while it is being edited."""
    (tmp_path / "ok.yaml").write_text(
        "apiVersion: netgraph.dev/v1alpha1\nkind: switch\nmetadata: {name: sw1}\n"
        "spec: {interfaces: [{name: Gi0/1, type: ethernet}]}\n",
        encoding="utf-8",
    )
    (tmp_path / "broken.yaml").write_text("kind: switch\n  bad indent:\n", encoding="utf-8")
    assert list(load_elements(tmp_path, nodes_only=False)) == ["sw1"]


def test_a_single_file_inventory_completes_its_documents() -> None:
    offered = values(["-i", str(QUICKSTART / "cables" / "links.yaml"), "show"])
    assert offered == ["cbl-rtr-sw", "cbl-sw-alice"]


# --------------------------------------------------------------------------- #
# The completers on their own
#
# The three defensive corners a shell cannot be made to produce: a context with
# no -i in it, a parameter that declares no choices, and the same candidate
# offered twice. Each one is what keeps a completer from raising in a shell.
# --------------------------------------------------------------------------- #


def test_a_context_without_an_inventory_option_falls_back_to_the_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(QUICKSTART)
    assert inventory_path(click.Context(cli.commands["show"])) == Path.cwd()


def test_a_kind_option_that_declares_no_choices_offers_every_kind() -> None:
    offered = complete_kind(click.Context(cli), click.Option(["--kind"]), "")
    assert [item.value for item in offered] == list(DOCUMENT_KINDS)


def test_a_candidate_is_offered_once_however_often_it_is_produced() -> None:
    offered = items([("a", "first"), ("a", "second"), ("b", "")], "")
    assert [(item.value, item.help) for item in offered] == [("a", "first"), ("b", None)]
