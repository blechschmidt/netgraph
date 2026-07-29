"""The container this repository ships: ``Dockerfile`` and ``docker-compose.yml``.

Neither file is exercised by any other test — an image is built by Docker, not by
pytest — so nothing in the project would notice a service whose ``command`` names
a flag that has been renamed, a published port that no longer matches the port
the server inside actually binds, a variable with no default that turns
``docker compose up`` into an error, or a preview whose health check polls a route
that has moved. The promises those two files make are asserted here against the
CLI itself, where a break is cheap.

What is deliberately *not* here is a build: ``tests`` must pass on a machine with
no Docker daemon. The ``docker`` job in ``.github/workflows/ci.yml`` builds the
image for real and drives each service through it, and
:func:`test_the_ci_workflow_exercises_every_service` keeps that job in step with
the services declared here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import click
import pytest
import yaml

from netgraph.cli import cli
from netgraph.watch.server import STATUS_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DOC = REPO_ROOT / "docs" / "docker.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Where the compose file mounts the inventory, which is also the Dockerfile's
#: ``WORKDIR``: ``-i/--inventory`` defaults to the working directory, so no
#: command in the compose file names the tree it reads.
MOUNT = "/inventory"

#: Every service, and whether it is a server. Adding one means adding it here,
#: to ``.github/workflows/ci.yml`` and to ``docs/docker.md`` -- which the tests
#: below insist on.
SERVICES = ["netgraph", "web", "watch"]
SERVERS = ["web", "watch"]

#: ``${NAME}`` or ``${NAME:-default}``.
_INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


# --------------------------------------------------------------------------- #
# Reading the files
# --------------------------------------------------------------------------- #


def expand(value: str) -> str:
    """Substitute ``${VAR:-default}`` the way compose would with no environment.

    Compose is not run here, so this is how a test sees the values a plain
    ``docker compose up`` in a fresh clone would use. A variable with no default
    expands to the empty string, which
    :func:`test_every_interpolated_variable_has_a_default` forbids separately.
    """
    return _INTERPOLATION.sub(lambda match: match["default"] or "", value)


def interpolations(text: str) -> set[str]:
    """Every variable name the text interpolates."""
    return {match["name"] for match in _INTERPOLATION.finditer(text)}


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """``docker-compose.yml``, with its YAML merge keys already resolved."""
    parsed = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parsed = compose["services"]
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_example() -> dict[str, str]:
    """``.env.example`` as the assignments it makes, comments dropped."""
    settings: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, value = stripped.partition("=")
        settings[name] = value
    return settings


def command_of(services: dict[str, dict[str, Any]], name: str) -> list[str]:
    """A service's ``command``, expanded, as the argument list netgraph receives."""
    words = services[name]["command"]
    assert isinstance(words, list), f"{name}: spell the command as a list, not a shell string"
    return [expand(str(word)) for word in words]


def parameters(command: click.Command) -> dict[str, click.Parameter]:
    """Every option of one CLI command, keyed by each spelling it accepts."""
    found: dict[str, click.Parameter] = {}
    for parameter in command.params:
        for spelling in (*parameter.opts, *parameter.secondary_opts):
            found[spelling] = parameter
    return found


def flags_and_values(words: list[str]) -> list[tuple[str, str | None]]:
    """Pair each ``--flag`` in an argument list with the value that follows it."""
    pairs: list[tuple[str, str | None]] = []
    for index, word in enumerate(words):
        if not word.startswith("-"):
            continue
        following = words[index + 1] if index + 1 < len(words) else None
        pairs.append((word, None if following is None or following.startswith("-") else following))
    return pairs


# --------------------------------------------------------------------------- #
# The services
# --------------------------------------------------------------------------- #


def test_the_compose_file_declares_exactly_the_documented_services(
    compose: dict[str, Any], services: dict[str, dict[str, Any]]
) -> None:
    # The project name decides the container names (netgraph-web-1), which
    # docs/docker.md quotes.
    assert compose["name"] == "netgraph"
    assert sorted(services) == sorted(SERVICES)


def test_the_one_shot_service_is_the_only_one_behind_a_profile(
    services: dict[str, dict[str, Any]],
) -> None:
    """``docker compose up`` must not start a command that exits immediately.

    ``compose run`` enables a service's own profiles, so putting the CLI behind
    one costs nothing at the point of use and keeps ``up`` to the two servers.
    """
    assert services["netgraph"]["profiles"] == ["cli"]
    for name in SERVERS:
        assert "profiles" not in services[name], f"{name} must start with 'docker compose up'"


def test_the_cli_service_defaults_to_help(services: dict[str, dict[str, Any]]) -> None:
    """Run with no arguments it must explain itself, not act.

    A container that guessed what "run netgraph" meant would write a file
    nobody asked for.
    """
    assert command_of(services, "netgraph") == ["--help"]


@pytest.mark.parametrize("name", SERVERS)
def test_every_server_command_is_a_command_the_cli_has(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    """The image's entrypoint is ``netgraph``, so ``command`` is its argv."""
    words = command_of(services, name)
    subcommand = cli.commands.get(words[0])
    assert subcommand is not None, f"{name}: 'netgraph {words[0]}' is not a command"

    known = parameters(subcommand)
    for flag, _ in flags_and_values(words[1:]):
        assert flag in known, f"{name}: '{words[0]}' has no {flag}"


@pytest.mark.parametrize("name", SERVERS)
def test_every_enumerated_value_is_one_the_cli_accepts(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    """``--layer l1``, ``--icons none``: the defaults of NETGRAPH_LAYER and friends.

    A value dropped from a Choice would otherwise only be found by starting the
    container and reading why it exited 2.
    """
    words = command_of(services, name)
    known = parameters(cli.commands[words[0]])
    for flag, value in flags_and_values(words[1:]):
        parameter_type = known[flag].type
        if isinstance(parameter_type, click.Choice) and value is not None:
            assert value in parameter_type.choices, f"{name}: {flag} {value} is not a choice"


@pytest.mark.parametrize("name", SERVERS)
def test_every_server_binds_every_interface_inside_the_container(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    """A container's loopback is its own.

    netgraph binds 127.0.0.1 by default so that a diagram of internal topology
    is not published by accident; inside a container that default would make the
    server unreachable from the machine running Docker, and the published port
    below is what actually decides who can reach it.
    """
    words = command_of(services, name)
    assert ("--host", "0.0.0.0") in flags_and_values(words)


def test_the_editor_does_not_try_to_open_a_browser(
    services: dict[str, dict[str, Any]],
) -> None:
    assert "--no-open" in command_of(services, "web")


def test_the_preview_actually_serves_something(services: dict[str, dict[str, Any]]) -> None:
    """``watch`` without ``--serve`` or ``--output`` renders into the void."""
    assert "--serve" in command_of(services, "watch")


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SERVERS)
def test_the_published_port_is_the_port_the_command_binds(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    """``127.0.0.1:8081:8081`` -- and 8081 is what ``netgraph web`` defaults to.

    The compose file passes no ``--port``, so the container side is netgraph's
    own default. If that default moved, the mapping would publish a port nothing
    listens on.
    """
    published = services[name]["ports"]
    assert len(published) == 1
    host_ip, host_port, container_port = expand(str(published[0])).rsplit(":", 2)

    words = command_of(services, name)
    default = parameters(cli.commands[words[0]])["--port"].default
    assert container_port == str(default)
    # Equal on both sides so that a URL from the container's own log -- or from
    # this page's documentation -- is one the host can open.
    assert host_port == container_port
    assert host_ip == "127.0.0.1", "publish to loopback by default; NETGRAPH_BIND opts out"


def test_the_two_servers_do_not_share_a_port(services: dict[str, dict[str, Any]]) -> None:
    """A watch run and an editing session must be able to be open at once."""
    ports = {name: expand(str(services[name]["ports"][0])) for name in SERVERS}
    assert len(set(ports.values())) == len(SERVERS), ports


@pytest.mark.parametrize("name", SERVERS)
def test_the_health_check_asks_for_a_route_the_server_answers(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    """Health checks reach loopback *inside* the container, on a real route.

    The route is imported rather than spelled out, so renaming it in the server
    fails here instead of quietly turning the container unhealthy.
    """
    routes = {"web": "/", "watch": STATUS_PATH}
    test = services[name]["healthcheck"]["test"]
    assert test[0] == "CMD", "exec form: no shell in the middle to misquote the URL"
    # No curl in a slim image, and no reason to add one: the interpreter that
    # serves the page can ask it for itself.
    assert test[1:3] == ["python", "-c"]

    container_port = expand(str(services[name]["ports"][0])).rsplit(":", 1)[1]
    assert f"http://127.0.0.1:{container_port}{routes[name]}" in test[3]


# --------------------------------------------------------------------------- #
# The mount, and what it is allowed to do
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_mounts_the_inventory_where_the_cli_looks_for_it(
    services: dict[str, dict[str, Any]], name: str, dockerfile: str
) -> None:
    volumes = services[name]["volumes"]
    assert len(volumes) == 1
    _, target, _ = expand(str(volumes[0])).rsplit(":", 2)
    assert target == MOUNT
    assert f"WORKDIR {MOUNT}" in dockerfile, "the mount is only implicit because it is the cwd"


def test_the_servers_cannot_write_to_the_inventory_and_the_cli_can(
    services: dict[str, dict[str, Any]],
) -> None:
    """Read-only is not decoration: it is why a preview cannot damage the tree.

    ``watch`` renders to memory and ``web`` keeps the document stream in the
    browser, so neither has anything to write. ``render -o``, ``fmt``, ``export
    -o``, ``init`` and ``import`` do, which is the one service mounted rw.
    """
    modes = {name: expand(str(services[name]["volumes"][0])).rsplit(":", 1)[1] for name in SERVICES}
    assert modes == {"netgraph": "rw", "web": "ro", "watch": "ro"}


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_runs_unprivileged(services: dict[str, dict[str, Any]], name: str) -> None:
    service = services[name]
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["read_only"] is True
    # The one writable path, because Graphviz and fontconfig need somewhere; the
    # Dockerfile points HOME and XDG_CACHE_HOME at it.
    assert service["tmpfs"] == ["/tmp"]
    assert expand(service["user"]) == "1000:1000"


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_gets_an_init(services: dict[str, dict[str, Any]], name: str) -> None:
    """Without one, ``docker compose down`` takes the full grace period.

    netgraph is PID 1 in the container, and a Python process that installed no
    SIGTERM handler ignores the signal when it is PID 1; the daemon then waits
    ten seconds and kills it. tini forwards it to a child instead, where the
    default disposition applies.
    """
    assert services[name]["init"] is True


# --------------------------------------------------------------------------- #
# Variables
# --------------------------------------------------------------------------- #


def test_every_interpolated_variable_has_a_default() -> None:
    """``docker compose up`` in a fresh clone must work with no ``.env`` at all."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    for match in _INTERPOLATION.finditer(text):
        assert match["default"] is not None, f"${{{match['name']}}} has no default"


def test_the_default_inventory_is_an_example_that_exists() -> None:
    """A clone with no network of its own described still draws something."""
    default = expand("${NETGRAPH_INVENTORY:-./examples/home-lab}")
    assert (REPO_ROOT / default).is_dir()
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    for match in _INTERPOLATION.finditer(text):
        if match["name"] == "NETGRAPH_INVENTORY":
            assert (REPO_ROOT / match["default"]).is_dir(), match["default"]


def test_env_example_documents_exactly_the_variables_compose_reads(
    env_example: dict[str, str],
) -> None:
    """Neither an undocumented knob nor a documented one that does nothing."""
    assert set(env_example) == interpolations(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_env_example_repeats_the_compose_defaults(env_example: dict[str, str]) -> None:
    """A copied ``.env`` must not silently change behaviour.

    ``cp .env.example .env`` is the documented first step, so every value in it
    has to be the value compose would have used anyway.
    """
    defaults: dict[str, str] = {}
    for match in _INTERPOLATION.finditer(COMPOSE_FILE.read_text(encoding="utf-8")):
        assert match["default"] is not None
        defaults.setdefault(match["name"], match["default"])
    assert env_example == defaults


def test_the_polling_escape_hatch_is_off_by_default(env_example: dict[str, str]) -> None:
    """Bind mounts on Docker Desktop and NFS deliver no filesystem events.

    watchfiles reads this itself and treats an empty value as "decide from the
    platform", which is what a Linux host wants: polling a large tree costs CPU
    for nothing.
    """
    assert env_example["WATCHFILES_FORCE_POLLING"] == ""


# --------------------------------------------------------------------------- #
# The image
# --------------------------------------------------------------------------- #


def test_the_dockerfile_builds_in_two_stages(dockerfile: str) -> None:
    """The shipped stage must carry no build tooling and no source tree."""
    stages = re.findall(r"^FROM\s+\S+\s+AS\s+(\w+)", dockerfile, re.MULTILINE)
    assert stages == ["build", "runtime"]
    assert "COPY --from=build /opt/netgraph /opt/netgraph" in dockerfile
    assert "pip install ." in dockerfile


def test_the_image_carries_graphviz_and_a_font(dockerfile: str) -> None:
    """``dot`` is the one dependency netgraph cannot vendor.

    A slim image has no fonts either, and Graphviz without one draws every label
    as a row of boxes -- which is the kind of failure that looks like a netgraph
    bug.
    """
    assert "graphviz" in dockerfile
    assert "fonts-dejavu-core" in dockerfile
    # Proof the install worked, at build time rather than in somebody's first
    # render.
    assert "dot -V" in dockerfile


def test_the_image_is_the_cli(dockerfile: str) -> None:
    assert 'ENTRYPOINT ["netgraph"]' in dockerfile
    assert 'CMD ["--help"]' in dockerfile


def test_the_image_runs_as_a_non_root_user(dockerfile: str) -> None:
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.MULTILINE)
    assert users == ["1000:1000"], "numeric: the compose file substitutes the host's own id"


def test_the_image_points_home_at_the_writable_path(dockerfile: str) -> None:
    """An arbitrary uid has no home directory, and fontconfig wants one.

    ``user:`` in the compose file is the *host's* id, which has no passwd entry
    in the image; without this, fontconfig writes a warning to stderr on every
    render it cannot cache.
    """
    assert "HOME=/tmp" in dockerfile
    assert "XDG_CACHE_HOME=/tmp" in dockerfile


def test_the_build_context_is_an_allowlist(dockerfile: str) -> None:
    """Everything excluded, then the four paths the wheel is built from let back in.

    The failure modes point opposite ways: a forgotten exclusion ships a
    virtualenv, a private inventory or a .git history into the build context,
    while a forgotten inclusion fails the build loudly.
    """
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"

    allowed = {line.removeprefix("!") for line in lines if line.startswith("!")}
    copied = {
        source
        for match in re.finditer(r"^COPY\s+(?!--from)(?P<sources>.+)$", dockerfile, re.MULTILINE)
        for source in match["sources"].split()[:-1]
    }
    assert copied <= allowed, f"the build copies {copied - allowed}, which is excluded"


def test_the_sdist_ships_the_container_files() -> None:
    """This module reads four files; an sdist without them cannot run the suite."""
    manifest = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for path in (DOCKERFILE, COMPOSE_FILE, DOCKERIGNORE, ENV_EXAMPLE):
        assert f'"/{path.name}"' in manifest, f"{path.name} is missing from the sdist include"


# --------------------------------------------------------------------------- #
# The documentation and the pipeline
# --------------------------------------------------------------------------- #


def test_the_docs_page_covers_every_service_and_variable(env_example: dict[str, str]) -> None:
    page = DOC.read_text(encoding="utf-8")
    for name in SERVICES:
        assert f"`{name}`" in page, f"docs/docker.md does not mention the {name} service"
    for variable in env_example:
        assert variable in page, f"docs/docker.md does not mention {variable}"


def test_the_ci_workflow_exercises_every_service() -> None:
    """A compose file that parses is not a compose file that works.

    So the ``docker`` job builds the image and drives each service through it.
    Adding a service here without adding it there would leave it unrun.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["docker"]
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "docker compose build" in script
    for name in SERVICES:
        assert name in script, f"the docker job never runs the {name} service"
