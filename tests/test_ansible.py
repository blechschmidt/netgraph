"""The Ansible integration: the library, the collection, and one real playbook.

Three layers, and each is tested where it can be tested cheaply.

* **The library** (:mod:`netviz.ansible`) decides everything: which hosts exist,
  what a query answers, what a per-host variable is bound to. It is plain Python
  and needs no Ansible, so it is tested here in full — including the property
  that keeps it honest, that the document it builds *is* the one ``netviz export
  ansible-inventory`` writes, plus whatever was asked for.
* **The collection** is data: plugin files with a YAML documentation block that
  ansible-doc parses and Ansible validates its options against. What can be
  checked without Ansible is that every file compiles, that every block is YAML,
  and that every option the code asks for is one the documentation declares —
  which is the failure mode, because ``get_option`` on an undeclared name raises
  at run time and nowhere else.
* **The wiring** needs Ansible, and is checked by running it: a real
  ``ansible-inventory`` over the plugin, and a real ``ansible-playbook``
  rendering the shipped systemd-networkd units from ``examples/home-lab``. Those
  tests skip — naming the command to install — when ansible-core is not on the
  control node running the suite; ``.github/workflows/ci.yml`` has a job that
  installs it, so they are not skipped in CI.

The last of those is the whole feature end to end: an inventory read by the
plugin, a template rendering a query, a value that came out of the YAML tree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from click.testing import CliRunner

from netviz.ansible import (
    COLLECTION,
    DEFAULT_DESTINATION,
    NAMESPACE,
    Answer,
    InventoryOptions,
    InventoryRejected,
    answer,
    build,
    collection_path,
    collections_path,
    forget,
    host_params,
    hosts_of,
    install,
    inventory_document,
    layer_named,
    open_session,
)
from netviz.ansible.collection import galaxy_manifest
from netviz.cli import cli
from netviz.export import export, layers_for
from netviz.export.context import ExportContext, ExportOptions
from netviz.query import QueryError
from netviz.render.graph import Layer

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
EXAMPLES: Final = REPO_ROOT / "examples"
HOME_LAB: Final = EXAMPLES / "home-lab"
CAMPUS: Final = EXAMPLES / "campus"

#: A host of ``examples/home-lab`` that is a device in the schema's sense, and
#: the element it comes from. Both spellings matter: the first is what Ansible
#: knows, the second is what a query matches on.
ROUTER_HOST: Final = "rtr-home.routers"
ROUTER_FQN: Final = "routers/rtr-home"

#: The query a template writes, and the one the docs lead with.
ADDRESSES: Final = "select (device filter .fqn = $fqn).addresses.address"


def ansible_command(name: str) -> Path | None:
    """``name`` as installed *beside this interpreter*, or ``None``.

    Not :func:`shutil.which`. These plugins run in Ansible's own process and
    import netviz there, so the only ansible worth running here is one sharing
    the environment the suite is running in — and a control node that happens to
    have a *system* ansible-playbook on PATH has one that cannot import netviz
    at all. GitHub's ubuntu runner image is exactly that machine, and trusting
    PATH made every ``test`` matrix job run these tests against an interpreter
    that was never going to pass them.
    """
    for suffix in ("", ".exe"):
        candidate = Path(sys.executable).parent / f"{name}{suffix}"
        if candidate.exists():
            return candidate
    return None


ANSIBLE = pytest.mark.skipif(
    ansible_command("ansible-playbook") is None,
    reason="ansible-core is not installed beside this interpreter; "
    "install it with 'uv pip install ansible-core' (see docs/testing.md)",
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def forget_sessions():
    """No test sees a session another test opened.

    The memo is what makes a play fast and what makes a test order-dependent, so
    it is cleared around every one of them.
    """
    forget()
    yield
    forget()


def invoke(runner: CliRunner, *args: str):
    return runner.invoke(cli, list(args), catch_exceptions=False)


# --------------------------------------------------------------------------- #
# The session
# --------------------------------------------------------------------------- #


def test_the_same_root_is_loaded_once() -> None:
    """A play renders forty templates and reads the tree for the first of them."""
    first = open_session(HOME_LAB)
    assert open_session(HOME_LAB) is first
    assert open_session(str(HOME_LAB)) is first, "a string and a path are one root"
    forget(HOME_LAB)
    assert open_session(HOME_LAB) is not first


def test_a_session_that_is_not_shared_is_not_remembered() -> None:
    fresh = open_session(HOME_LAB, reuse=False)
    assert open_session(HOME_LAB) is not fresh


def test_a_broken_tree_is_refused_rather_than_answered(tmp_path: Path) -> None:
    """An answer from documents that did not load reports what is left."""
    (tmp_path / "broken.yaml").write_text(
        "apiVersion: netviz.dev/v1alpha1\nkind: cable\nmetadata:\n  name: c1\n"
        "spec:\n  endpoints:\n    - device: nobody\n      interface: eth0\n"
        "    - device: nobody-else\n      interface: eth0\n",
        encoding="utf-8",
    )
    with pytest.raises(InventoryRejected) as caught:
        open_session(tmp_path)
    assert "would not describe the network" in str(caught.value)
    assert "netviz validate" in str(caught.value)

    forced = open_session(tmp_path, force=True)
    assert forced.inventory.root == tmp_path.resolve()


def test_a_warning_only_stops_a_strict_session(tmp_path: Path) -> None:
    """``strict`` is the same promotion ``netviz validate --strict`` makes."""
    shutil.copytree(HOME_LAB, tmp_path / "net")
    (tmp_path / "net" / "orphan.yaml").write_text(
        "apiVersion: netviz.dev/v1alpha1\nkind: server\nmetadata:\n  name: orphan\n"
        "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n",
        encoding="utf-8",
    )
    tree = tmp_path / "net"
    open_session(tree)  # a warning does not stop it
    forget(tree)
    with pytest.raises(InventoryRejected):
        open_session(tree, strict=True)


def test_both_query_languages_are_answered() -> None:
    """The same dispatch ``netviz query`` makes, on the same first word."""
    session = open_session(HOME_LAB)
    relational = session.ask("select device.fqn filter .kind = 'router'")
    assert relational.relational
    assert relational.rows == (ROUTER_FQN,)

    selector = session.ask("kind = router")
    assert not selector.relational
    assert selector.rows == (ROUTER_FQN,)


def test_a_selector_takes_no_parameters() -> None:
    session = open_session(HOME_LAB)
    with pytest.raises(QueryError) as caught:
        session.ask("kind = router", {"who": "x"})
    assert "relational query" in str(caught.value)


def test_an_answer_counts_and_is_falsy_when_empty() -> None:
    empty = Answer("select device filter false", (), relational=True)
    assert not empty
    assert len(empty) == 0
    assert len(open_session(HOME_LAB).ask("select device")) == 7


def test_the_same_question_is_parsed_once_however_many_hosts_ask_it() -> None:
    """Forty templates over forty hosts is one question, not sixteen hundred.

    The key is the text, the origin and the parameter *types* — never the
    values, which is what makes reusing the tree safe.
    """
    session = open_session(HOME_LAB)
    for host in ("rtr-home.routers", "srv-nas.hosts", "sw-home.switches"):
        session.ask(ADDRESSES, {"fqn": host})
    assert len(session._queries) == 1
    session.ask(ADDRESSES, {"fqn": 10})
    assert len(session._queries) == 2, "a different type is a different parse"


def test_a_layer_is_named_or_refused() -> None:
    assert layer_named(None) is Layer.L2
    assert layer_named("l3") is Layer.L3
    assert layer_named(Layer.L1) is Layer.L1
    with pytest.raises(Exception, match="is not a layer"):
        layer_named("layer-8")


# --------------------------------------------------------------------------- #
# The host a template is being rendered for
# --------------------------------------------------------------------------- #


def test_the_host_being_configured_is_bound_without_being_asked_for() -> None:
    """This is what lets a template pass no arguments at all."""
    rows = answer(HOME_LAB, ADDRESSES, host=ROUTER_HOST)
    assert rows == answer(
        HOME_LAB,
        "select (device filter .fqn = $fqn).addresses.address",
        params={"fqn": ROUTER_FQN},
    )
    assert "192.168.10.1/24" in rows


@pytest.mark.parametrize(
    ("param", "expected"),
    [
        ("host", ROUTER_HOST),
        ("fqn", ROUTER_FQN),
        ("name", "rtr-home"),
        ("kind", "router"),
        ("namespace", "routers"),
    ],
)
def test_every_part_of_a_hosts_identity_is_bound(param: str, expected: str) -> None:
    assert answer(HOME_LAB, f"select ${param}", host=ROUTER_HOST) == [expected]


def test_an_explicit_parameter_wins_over_the_host() -> None:
    """So a template can ask about a host other than the one it is rendering."""
    assert answer(HOME_LAB, "select $name", host=ROUTER_HOST, params={"name": "elsewhere"}) == [
        "elsewhere"
    ]


def test_a_name_that_is_not_a_host_binds_nothing_and_the_query_says_so() -> None:
    with pytest.raises(QueryError) as caught:
        answer(HOME_LAB, ADDRESSES, host="not-a-host")
    assert "no value was supplied for '$fqn'" in str(caught.value)


def test_a_query_with_no_hole_in_it_does_not_build_the_host_table() -> None:
    """The table costs an export, and most queries have no parameter at all."""
    session = open_session(HOME_LAB)
    answer(HOME_LAB, "select device.name", host=ROUTER_HOST)
    assert not session.derived
    answer(HOME_LAB, ADDRESSES, host=ROUTER_HOST)
    assert session.derived


def test_host_params_skips_a_variable_the_exporter_did_not_write() -> None:
    """An absent fact is unbound, not bound to the empty string."""
    assert host_params("h", {"netviz_name": "sw-01", "netviz_kind": ""}) == {
        "host": "h",
        "name": "sw-01",
    }


# --------------------------------------------------------------------------- #
# The inventory document
# --------------------------------------------------------------------------- #


def exported(root: Path) -> dict[str, Any]:
    """What ``netviz export ansible-inventory`` writes for ``root``."""
    session = open_session(root, reuse=False)
    graphs = {layer: session.graph(layer) for layer in layers_for("ansible-inventory")}
    result = export(
        "ansible-inventory",
        lambda recorder: ExportContext(
            inventory=session.inventory,
            graphs=graphs,
            options=ExportOptions(),
            recorder=recorder,
        ),
    )
    document: dict[str, Any] = json.loads(result.payload)
    return document


@pytest.mark.parametrize("root", [HOME_LAB, CAMPUS])
def test_the_plugin_and_the_exporter_are_one_document(root: Path) -> None:
    """The property that keeps the two honest.

    Two implementations of "which hosts are there, and in which groups" would
    drift, and the day they did, an inventory file checked into a repository and
    the plugin that replaces it would disagree about who is a server.
    """
    assert build(open_session(root)) == exported(root)


def test_a_host_variable_that_is_a_query_is_answered_per_host() -> None:
    document = inventory_document(
        HOME_LAB,
        InventoryOptions(host_vars={"mgmt": ADDRESSES}),
    )
    hostvars = document["_meta"]["hostvars"]
    assert "192.168.10.1/24" in hostvars[ROUTER_HOST]["mgmt"]
    assert hostvars["srv-nas.hosts"]["mgmt"] != hostvars[ROUTER_HOST]["mgmt"]


def test_one_row_is_the_value_and_anything_else_is_the_list() -> None:
    """A query that can answer twice always reads as a list; one that cannot does not."""
    document = inventory_document(
        HOME_LAB,
        InventoryOptions(
            host_vars={
                "one": "select $name",
                "several": ADDRESSES,
                "none": "select device.name filter .name = 'nobody'",
            }
        ),
    )
    variables = document["_meta"]["hostvars"][ROUTER_HOST]
    assert variables["one"] == "rtr-home"
    assert isinstance(variables["several"], list)
    assert variables["none"] == []


def test_a_group_that_is_a_query_names_hosts() -> None:
    document = inventory_document(
        HOME_LAB,
        InventoryOptions(groups={"routed": "select device.fqn filter .kind = 'router'"}),
    )
    assert document["routed"]["hosts"] == [ROUTER_HOST]
    assert "routed" in document["all"]["children"]


def test_a_group_query_may_be_a_selector_or_a_shaped_row() -> None:
    document = inventory_document(
        HOME_LAB,
        InventoryOptions(
            groups={
                "selected": "kind = router",
                "shaped": "select device { fqn } filter .kind = 'router'",
            }
        ),
    )
    assert document["selected"]["hosts"] == [ROUTER_HOST]
    assert document["shaped"]["hosts"] == [ROUTER_HOST]


def test_an_empty_group_is_not_written() -> None:
    """A group with no hosts is a group every ansible command warns about."""
    document = inventory_document(
        HOME_LAB,
        InventoryOptions(groups={"nobody": "select device.fqn filter .name = 'nobody'"}),
    )
    assert "nobody" not in document


def test_a_group_query_that_names_nothing_a_host_could_be_is_refused() -> None:
    with pytest.raises(QueryError) as caught:
        inventory_document(HOME_LAB, InventoryOptions(groups={"counted": "select count(device)"}))
    assert "a group is a list of hosts" in str(caught.value)


def test_a_group_query_may_not_replace_a_derived_group() -> None:
    """A group named ``kind_router`` whose membership is something else is a trap."""
    with pytest.raises(Exception, match="already derives"):
        inventory_document(HOME_LAB, InventoryOptions(groups={"kind_router": "kind = switch"}))


def test_a_selection_narrows_which_elements_become_hosts() -> None:
    document = inventory_document(HOME_LAB, InventoryOptions(select="kind = router"))
    assert list(document["_meta"]["hostvars"]) == [ROUTER_HOST]


def test_the_host_table_is_built_once_per_session() -> None:
    session = open_session(HOME_LAB)
    assert hosts_of(session) is hosts_of(session)


# --------------------------------------------------------------------------- #
# The shipped collection
# --------------------------------------------------------------------------- #


def collection_files() -> list[Path]:
    """Every file of the shipped collection, as an install copies them.

    ``__pycache__`` is skipped for the reason the installer skips it: running
    Ansible against the tree in place leaves compiled plugins behind, and they
    are not part of the collection.
    """
    return sorted(
        path
        for path in collection_path().rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def test_the_collection_is_where_ansible_looks_for_one() -> None:
    """``ANSIBLE_COLLECTIONS_PATH`` wants the directory *holding* the tree."""
    assert collections_path().name == "collections"
    assert collection_path() == collections_path() / "ansible_collections" / NAMESPACE / COLLECTION
    assert (collection_path() / "plugins").is_dir()


@pytest.mark.parametrize(
    "relative",
    [
        "plugins/inventory/netviz.py",
        "plugins/lookup/query.py",
        "plugins/filter/netviz.py",
        "plugins/plugin_utils/netviz_api.py",
        "playbooks/systemd_network.yml",
        "playbooks/templates/systemd.network.j2",
        "meta/runtime.yml",
        "README.md",
    ],
)
def test_the_collection_ships_every_file_it_needs(relative: str) -> None:
    assert (collection_path() / relative).is_file()


@pytest.mark.parametrize(
    "relative",
    ["plugins/inventory/netviz.py", "plugins/lookup/query.py", "plugins/filter/netviz.py"],
)
def test_every_plugin_compiles(relative: str) -> None:
    """A syntax error in a plugin is a failure Ansible reports at play time."""
    source = (collection_path() / relative).read_text(encoding="utf-8")
    compile(source, relative, "exec")


@pytest.mark.parametrize("relative", ["plugins/inventory/netviz.py", "plugins/lookup/query.py"])
def test_every_documentation_block_is_yaml(relative: str) -> None:
    """ansible-doc parses these; a tab in the wrong place breaks the plugin."""
    for block in documentation_of(collection_path() / relative).values():
        assert yaml.safe_load(block) is not None


def documentation_of(path: Path) -> dict[str, str]:
    """The ``DOCUMENTATION``/``EXAMPLES``/``RETURN`` blocks of a plugin file."""
    namespace: dict[str, Any] = {}
    source = path.read_text(encoding="utf-8")
    # Only the assignments before the imports: executing the file would need
    # Ansible, and what is wanted is the data.
    for name in ("DOCUMENTATION", "EXAMPLES", "RETURN"):
        marker = f'{name} = """'
        if marker not in source:
            continue
        body = source.split(marker, 1)[1].split('"""', 1)[0]
        namespace[name] = body
    return namespace


@pytest.mark.parametrize("relative", ["plugins/inventory/netviz.py", "plugins/lookup/query.py"])
def test_every_option_the_code_reads_is_one_the_documentation_declares(relative: str) -> None:
    """The failure mode: ``get_option`` on an undeclared name raises at play time.

    Nothing else catches it — the documentation is data, the call is code, and
    the two are only joined when Ansible has already started a play.
    """
    path = collection_path() / relative
    declared = set(yaml.safe_load(documentation_of(path)["DOCUMENTATION"])["options"])
    if "constructed" in (
        yaml.safe_load(documentation_of(path)["DOCUMENTATION"]).get(
            "extends_documentation_fragment"
        )
        or []
    ):
        declared |= {"strict", "compose", "groups", "keyed_groups", "use_extra_vars"}
    read = set(re.findall(r'get_option\(\s*"([^"]+)"', path.read_text(encoding="utf-8")))
    assert read <= declared, f"undeclared: {sorted(read - declared)}"


def test_the_generated_galaxy_manifest_is_the_installed_version() -> None:
    manifest = yaml.safe_load(galaxy_manifest())
    assert manifest["namespace"] == NAMESPACE
    assert manifest["name"] == COLLECTION
    assert manifest["version"] == version("netviz")


def test_installing_writes_the_whole_collection_and_a_manifest(tmp_path: Path) -> None:
    written = install(tmp_path)
    into = tmp_path / "ansible_collections" / NAMESPACE / COLLECTION
    assert {path.relative_to(into) for path in written} == {
        path.relative_to(collection_path()) for path in collection_files()
    } | {Path("galaxy.yml")}
    assert (into / "plugins" / "lookup" / "query.py").read_text(encoding="utf-8") == (
        collection_path() / "plugins" / "lookup" / "query.py"
    ).read_text(encoding="utf-8")


def test_installing_over_an_installation_is_refused_unless_forced(tmp_path: Path) -> None:
    install(tmp_path)
    with pytest.raises(Exception, match="already exists"):
        install(tmp_path)
    assert install(tmp_path, force=True)


def test_the_default_destination_is_the_one_ansible_galaxy_uses() -> None:
    assert DEFAULT_DESTINATION.as_posix().endswith(".ansible/collections")


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_path_command_prints_a_collections_path(runner: CliRunner) -> None:
    result = invoke(runner, "ansible", "path")
    assert result.exit_code == 0
    # The path is the last line and is the whole of stdout; the commentary about
    # which collection it holds goes to stderr, so the command can be substituted
    # into a shell assignment.
    assert Path(result.output.strip().splitlines()[-1]) == collections_path()


def test_the_install_command_writes_and_says_where(runner: CliRunner, tmp_path: Path) -> None:
    result = invoke(runner, "ansible", "install", str(tmp_path))
    assert result.exit_code == 0
    assert (tmp_path / "ansible_collections" / NAMESPACE / COLLECTION / "galaxy.yml").is_file()
    assert "ANSIBLE_COLLECTIONS_PATH" in result.output


def test_installing_twice_is_an_error_with_a_way_out(runner: CliRunner, tmp_path: Path) -> None:
    invoke(runner, "ansible", "install", str(tmp_path))
    result = invoke(runner, "ansible", "install", str(tmp_path))
    assert result.exit_code == 1
    assert "force" in result.output


def test_the_inventory_command_prints_what_the_plugin_would(runner: CliRunner) -> None:
    result = invoke(
        runner,
        "-i",
        str(HOME_LAB),
        "ansible",
        "inventory",
        "--list",
        "--var",
        f"mgmt={ADDRESSES}",
        "--group",
        "routed=select device.fqn filter .kind = 'router'",
    )
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    expected = inventory_document(
        HOME_LAB,
        InventoryOptions(
            host_vars={"mgmt": ADDRESSES},
            groups={"routed": "select device.fqn filter .kind = 'router'"},
        ),
    )
    # The one thing the command adds over the builder, for the same reason the
    # plugin adds it: a template driven by this as a dynamic inventory script
    # needs to know which tree the answer came from.
    assert document["all"]["vars"].pop("netviz_root") == str(HOME_LAB)
    assert document == expected


def test_the_inventory_command_refuses_a_malformed_assignment(runner: CliRunner) -> None:
    result = invoke(runner, "-i", str(HOME_LAB), "ansible", "inventory", "--var", "oops")
    assert result.exit_code == 2
    assert "NAME=QUERY" in result.output


def test_the_inventory_command_reports_a_bad_query(runner: CliRunner) -> None:
    result = invoke(
        runner, "-i", str(HOME_LAB), "ansible", "inventory", "--var", "x=select device.nonsense"
    )
    assert result.exit_code == 2
    assert "nonsense" in result.output


def test_the_inventory_command_refuses_a_broken_tree(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("kind: nonsense\n", encoding="utf-8")
    result = invoke(runner, "-i", str(tmp_path), "ansible", "inventory")
    assert result.exit_code == 1


# --------------------------------------------------------------------------- #
# The wiring, with Ansible actually running
# --------------------------------------------------------------------------- #


def project(tmp_path: Path, config: str) -> Path:
    """An Ansible project pointing at a copy of ``examples/home-lab``."""
    shutil.copytree(HOME_LAB, tmp_path / "net")
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "netviz.yml").write_text(config, encoding="utf-8")
    return tmp_path


def plain(value: Any) -> Any:
    """``value`` with Ansible's unsafe-string wrappers taken off.

    ansible-core marks strings that came from an inventory plugin as untrusted
    for templating, and ``ansible-inventory --list`` serialises that marking as
    ``{"__ansible_unsafe": "..."}``. It is a property of the dump rather than of
    the value — a play sees the string — so a comparison against what netviz
    produced has to look through it.
    """
    if isinstance(value, dict):
        if set(value) == {"__ansible_unsafe"}:
            return value["__ansible_unsafe"]
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def run_ansible(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run an Ansible command with the shipped collection on its path."""
    environment = dict(os.environ)
    environment["ANSIBLE_COLLECTIONS_PATH"] = str(collections_path())
    environment["ANSIBLE_LOCALHOST_WARNING"] = "False"
    environment["ANSIBLE_INVENTORY_UNPARSED_WARNING"] = "False"
    environment["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
    found = ansible_command(command[0])
    assert found is not None, f"{command[0]} is not installed beside {sys.executable}"
    return subprocess.run(
        [str(found), *command[1:]],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )


@ANSIBLE
def test_ansible_reads_the_inventory_through_the_plugin(tmp_path: Path) -> None:
    """A real ``ansible-inventory``, over the real plugin, over the real tree."""
    root = project(
        tmp_path,
        "plugin: netviz.netviz.netviz\n"
        "root: ../net\n"
        "query_vars:\n"
        f"  netviz_mgmt: {ADDRESSES}\n"
        "query_groups:\n"
        "  routed: select device.fqn filter .kind = 'router'\n"
        "keyed_groups:\n"
        "  - key: netviz_kind\n"
        "    prefix: k\n",
    )
    result = run_ansible(["ansible-inventory", "-i", "inventory/netviz.yml", "--list"], cwd=root)
    assert result.returncode == 0, result.stderr
    document = plain(json.loads(result.stdout))
    hostvars = document["_meta"]["hostvars"]
    declared = exported(root / "net")["_meta"]["hostvars"]

    assert ROUTER_HOST in hostvars, "the plugin produced the hosts the exporter names"
    assert set(hostvars) == set(declared)
    assert hostvars[ROUTER_HOST]["ansible_host"] == declared[ROUTER_HOST]["ansible_host"]
    assert hostvars[ROUTER_HOST]["netviz_interfaces"] == declared[ROUTER_HOST]["netviz_interfaces"]
    assert "192.168.10.1/24" in hostvars[ROUTER_HOST]["netviz_mgmt"], "a query answered per host"
    assert hostvars[ROUTER_HOST]["netviz_root"] == str((root / "net").resolve()), (
        "the tree is announced, so a lookup in a template needs no arguments"
    )
    assert ROUTER_HOST in document["routed"]["hosts"], "a group whose members a query named"
    assert ROUTER_HOST in document["k_router"]["hosts"], "and Ansible's own constructed groups"


@ANSIBLE
def test_a_relative_root_is_relative_to_the_configuration_file(tmp_path: Path) -> None:
    """The file and the tree are checked in together; the working directory is not."""
    root = project(tmp_path, "plugin: netviz.netviz.netviz\nroot: ../net\n")
    (root / "elsewhere").mkdir()
    result = run_ansible(
        ["ansible-inventory", "-i", "../inventory/netviz.yml", "--list"], cwd=root / "elsewhere"
    )
    assert result.returncode == 0, result.stderr
    assert ROUTER_HOST in plain(json.loads(result.stdout))["_meta"]["hostvars"]


@ANSIBLE
def test_a_template_renders_a_systemd_unit_from_the_inventory(tmp_path: Path) -> None:
    """The whole feature, end to end, as the task that asked for it put it.

    The playbook is the one the collection ships. Every address in the unit came
    out of ``examples/home-lab`` by way of a query with a parameter in it, and
    nothing was gathered from a machine.
    """
    root = project(tmp_path, "plugin: netviz.netviz.netviz\nroot: ../net\n")
    units = root / "build" / "units"
    result = run_ansible(
        [
            "ansible-playbook",
            "netviz.netviz.systemd_network",
            "-i",
            "inventory/netviz.yml",
            "-e",
            f"netviz_units={units}",
        ],
        cwd=root,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    unit = (units / ROUTER_HOST / "10-wan0.network").read_text(encoding="utf-8")
    assert "[Match]\nName=wan0" in unit
    assert "Address=203.0.113.2/30" in unit, "the address the YAML declares"
    assert "MTUBytes=1500" in unit
    assert "routers/rtr-home" in unit, "the element it was generated from"

    assert not (units / ROUTER_HOST / "10-lo0.network").exists(), (
        "the loopback is systemd's own business, and the query says so"
    )
    written = sorted(path.name for path in (units / ROUTER_HOST).iterdir())
    assert written == ["10-lan0.network", "10-wan0.network"]


@ANSIBLE
def test_a_value_cannot_change_what_a_query_asks(tmp_path: Path) -> None:
    """Through Ansible, with a hostile value, in the form a template writes it."""
    root = project(tmp_path, "plugin: netviz.netviz.netviz\nroot: ../net\n")
    (root / "probe.yml").write_text(
        "- hosts: localhost\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - ansible.builtin.debug:\n"
        "        msg: \"{{ query('netviz.netviz.query', 'select device.name filter"
        " .name = $who', params={'who': who}) }}\"\n",
        encoding="utf-8",
    )
    result = run_ansible(
        [
            "ansible-playbook",
            "probe.yml",
            "-i",
            "inventory/netviz.yml",
            "-e",
            json.dumps({"who": "rtr-home' or true or '", "netviz_root": str(root / "net")}),
        ],
        cwd=root,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"msg": []' in result.stdout, "no device is called that, and none was matched"


@ANSIBLE
def test_ansible_doc_reads_every_plugin(tmp_path: Path) -> None:
    """The documentation blocks are the plugin's interface; ansible-doc is the check."""
    for kind, name in (
        ("lookup", "netviz.netviz.query"),
        ("inventory", "netviz.netviz.netviz"),
    ):
        result = run_ansible(["ansible-doc", "-t", kind, name], cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "netviz" in result.stdout
