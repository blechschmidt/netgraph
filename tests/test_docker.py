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

from netviz.cli import cli
from netviz.watch.server import STATUS_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DOC = REPO_ROOT / "docs" / "docker.md"
README = REPO_ROOT / "README.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTAINER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "container.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pypi.yaml"

#: The registry the two publishing workflows write to, spelled the way GHCR
#: requires: lowercase, and the repository's own namespace.
IMAGE = "ghcr.io/blechschmidt/netviz"

#: What ``container.yml`` publishes from the default branch. ``latest`` is
#: pointedly absent -- see :func:`test_a_branch_build_can_never_take_latest`.
#:
#: These are also the only tags that exist in the registry at all, because no
#: version has been released yet: ``latest``, ``X.Y.Z`` and ``X.Y`` are set by
#: ``pypi.yaml`` and it has never run. So they double as the set the docs are
#: allowed to tell a reader to pull -- see
#: :func:`test_no_documented_command_pulls_a_tag_that_does_not_exist`, and add
#: the version tags here when the first release is cut.
DEVELOPMENT_TAGS = ["edge", "main", "sha-"]

#: The tree the smoke test in ``container.yml`` renders. Also the compose file's
#: default, which :func:`test_the_default_inventory_is_an_example_that_exists`
#: checks from the other side.
EXAMPLE_INVENTORY = "home-lab"

#: Where the compose file mounts the inventory, which is also the Dockerfile's
#: ``WORKDIR``: ``-i/--inventory`` defaults to the working directory, so no
#: command in the compose file names the tree it reads.
MOUNT = "/inventory"

#: Every service, and whether it is a server. Adding one means adding it here,
#: to ``.github/workflows/ci.yml`` and to ``docs/docker.md`` -- which the tests
#: below insist on.
SERVICES = ["netviz", "web", "watch"]
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
    """A service's ``command``, expanded, as the argument list netviz receives."""
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
    # The project name decides the container names (netviz-web-1), which
    # docs/docker.md quotes.
    assert compose["name"] == "netviz"
    assert sorted(services) == sorted(SERVICES)


def test_the_one_shot_service_is_the_only_one_behind_a_profile(
    services: dict[str, dict[str, Any]],
) -> None:
    """``docker compose up`` must not start a command that exits immediately.

    ``compose run`` enables a service's own profiles, so putting the CLI behind
    one costs nothing at the point of use and keeps ``up`` to the two servers.
    """
    assert services["netviz"]["profiles"] == ["cli"]
    for name in SERVERS:
        assert "profiles" not in services[name], f"{name} must start with 'docker compose up'"


def test_the_cli_service_defaults_to_help(services: dict[str, dict[str, Any]]) -> None:
    """Run with no arguments it must explain itself, not act.

    A container that guessed what "run netviz" meant would write a file
    nobody asked for.
    """
    assert command_of(services, "netviz") == ["--help"]


@pytest.mark.parametrize("name", SERVERS)
def test_every_server_command_is_a_command_the_cli_has(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    """The image's entrypoint is ``netviz``, so ``command`` is its argv."""
    words = command_of(services, name)
    subcommand = cli.commands.get(words[0])
    assert subcommand is not None, f"{name}: 'netviz {words[0]}' is not a command"

    known = parameters(subcommand)
    for flag, _ in flags_and_values(words[1:]):
        assert flag in known, f"{name}: '{words[0]}' has no {flag}"


@pytest.mark.parametrize("name", SERVERS)
def test_every_enumerated_value_is_one_the_cli_accepts(
    services: dict[str, dict[str, Any]], name: str
) -> None:
    """``--layer l1``, ``--icons none``: the defaults of NETVIZ_LAYER and friends.

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

    netviz binds 127.0.0.1 by default so that a diagram of internal topology
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
    """``127.0.0.1:8081:8081`` -- and 8081 is what ``netviz web`` defaults to.

    The compose file passes no ``--port``, so the container side is netviz's
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
    assert host_ip == "127.0.0.1", "publish to loopback by default; NETVIZ_BIND opts out"


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
    assert modes == {"netviz": "rw", "web": "ro", "watch": "ro"}


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

    netviz is PID 1 in the container, and a Python process that installed no
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
    default = expand("${NETVIZ_INVENTORY:-./examples/home-lab}")
    assert (REPO_ROOT / default).is_dir()
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    for match in _INTERPOLATION.finditer(text):
        if match["name"] == "NETVIZ_INVENTORY":
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
    assert "COPY --from=build /opt/netviz /opt/netviz" in dockerfile
    assert "pip install ." in dockerfile


def test_the_image_carries_graphviz_and_a_font(dockerfile: str) -> None:
    """``dot`` is the one dependency netviz cannot vendor.

    A slim image has no fonts either, and Graphviz without one draws every label
    as a row of boxes -- which is the kind of failure that looks like a netviz
    bug.
    """
    assert "graphviz" in dockerfile
    assert "fonts-dejavu-core" in dockerfile
    # Proof the install worked, at build time rather than in somebody's first
    # render.
    assert "dot -V" in dockerfile


def test_the_image_is_the_cli(dockerfile: str) -> None:
    assert 'ENTRYPOINT ["netviz"]' in dockerfile
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


# --------------------------------------------------------------------------- #
# Publishing the image: .github/workflows/container.yml
# --------------------------------------------------------------------------- #
#
# ``container.yml`` and ``pypi.yaml`` both push to the same repository in GHCR,
# and the whole design is the line between them: the release owns the version
# tags and ``latest``, the container workflow owns ``edge`` and everything named
# after a commit. Cross that line and an unqualified ``docker pull`` starts
# returning unreleased work, which is the failure this section exists to make
# impossible to introduce quietly.


@pytest.fixture(scope="module")
def container_workflow() -> dict[Any, Any]:
    """The parsed workflow.

    Keyed by ``Any`` because YAML 1.1 reads the bare word ``on`` as the boolean
    ``True``, so the trigger block genuinely lives under a non-string key.
    """
    parsed = yaml.safe_load(CONTAINER_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def steps_of(workflow: dict[Any, Any], job: str) -> str:
    """Every step of a job, dumped back to text.

    The raw YAML rather than the structure: what these tests assert on is the
    exact word passed to an action or a shell, and picking it out of the parsed
    tree means knowing which key of which step holds it, which is precisely the
    detail that moves.
    """
    return yaml.dump(workflow["jobs"][job]["steps"])


def test_the_container_workflow_builds_on_every_branch_and_pull_request(
    container_workflow: dict[Any, Any],
) -> None:
    """An image only built at release time breaks halfway through a release."""
    triggers = container_workflow[True]
    assert triggers["push"]["branches"] == ["**"]
    assert "pull_request" in triggers
    # A ``v*`` push is pypi.yaml's; building it here too would race it for the
    # same tags in the same registry.
    assert triggers["push"]["tags-ignore"] == ["v*"]


def test_the_base_image_is_rebuilt_on_a_schedule(container_workflow: dict[Any, Any]) -> None:
    """``edge`` is python:3.12-slim plus Debian's Graphviz.

    Neither takes its security updates from this repository, so without a
    rebuild the published image ages into whatever its base was on the day some
    unrelated commit last touched ``src/``.
    """
    assert container_workflow[True]["schedule"], "nothing rebuilds the image against a fresh base"


def test_the_default_permission_is_read_only(container_workflow: dict[Any, Any]) -> None:
    assert container_workflow["permissions"] == {"contents": "read"}


def test_only_the_publishing_job_can_write_to_the_registry(
    container_workflow: dict[Any, Any],
) -> None:
    """The job that executes a pull request's Dockerfile holds no credential.

    That is the reason the build and the push are two jobs rather than one with
    an ``if:`` on half its steps: ``packages: write`` is granted to a job that
    only ever runs on an already-merged ref.
    """
    writers = [
        name
        for name, job in container_workflow["jobs"].items()
        if job.get("permissions", {}).get("packages") == "write"
    ]
    assert writers == ["publish"]
    assert container_workflow["jobs"]["build"]["permissions"] == {"contents": "read"}


def test_the_publish_job_waits_for_the_image_to_have_been_run(
    container_workflow: dict[Any, Any],
) -> None:
    assert container_workflow["jobs"]["publish"]["needs"] == "build"


def test_nothing_is_published_from_a_pull_request(
    container_workflow: dict[Any, Any],
) -> None:
    """Every push publishes; a pull request never does.

    A fork's token could not push anyway, and a same-repo one should not: a pull
    request builds code nobody has reviewed yet, and the image it produced would
    sit in the registry indistinguishable from one built from a merged commit.
    """
    condition = " ".join(container_workflow["jobs"]["publish"]["if"].split())
    assert "github.event_name != 'pull_request'" in condition

    # The one other way not to push: asking for a build without one, either by
    # unticking the box on a manual run or through pypi.yaml's dry run, which
    # arrives here as a workflow_call still carrying the caller's event name.
    assert "github.event_name != 'workflow_dispatch' || inputs.push" in condition

    # And nothing narrower than that. A branch filter here would mean the branch
    # tag below is produced for a branch nobody can pull.
    assert "default_branch" not in condition, (
        "the publish job is restricted to one branch, so branch tags are unreachable"
    )


def test_a_branch_build_can_never_take_latest(container_workflow: dict[Any, Any]) -> None:
    """``latest`` is what an unqualified ``docker pull`` gets.

    So it has to keep meaning "the newest release" and never "whatever main was
    this morning", nor "the newest pre-release". Two things enforce that, and
    both are asserted here because either alone would let it through:

    ``latest=false`` turns off ``docker/metadata-action``'s ``latest=auto``,
    which would otherwise hand ``latest`` to *any* semver tag, pre-release
    included. The single remaining source of it is an input, which only
    pypi.yaml passes and only when its guard says the version is not a
    pre-release -- a push to a branch leaves it unset, and unset is false.
    """
    steps = steps_of(container_workflow, "publish")
    assert "latest=false" in steps

    produced = [line for line in steps.splitlines() if "value=latest" in line]
    assert len(produced) == 1, f"expected exactly one source of :latest, found {produced}"
    assert "enable=${{ inputs.latest == true }}" in produced[0], (
        "container.yml can tag :latest without pypi.yaml having asked for it"
    )

    # And the other half of the contract: the release still asks for it, and
    # still withholds it from a pre-release.
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    latest = release["jobs"]["image"]["with"]["latest"]
    assert "needs.guard.outputs.prerelease == 'false'" in latest


@pytest.mark.parametrize(
    ("tag", "produced"),
    [
        ("X.Y.Z", "type=semver,pattern={{version}}"),
        ("X.Y", "type=semver,pattern={{major}}.{{minor}}"),
    ],
)
def test_a_version_tag_produces_the_semantic_version_tags(
    container_workflow: dict[Any, Any], tag: str, produced: str
) -> None:
    """A push of ``v1.2.3`` publishes ``1.2.3`` and the ``1.2`` line it belongs to.

    ``type=semver`` reads the version off the ref and is inert on any ref that is
    not a tag shaped like one, which is what lets the branch tags and the version
    tags share one unconditional list.
    """
    assert produced in steps_of(container_workflow, "publish"), (
        f"container.yml no longer produces the {tag} tag for a v*.*.* push"
    )


def test_a_version_tag_reaches_the_container_workflow_exactly_once(
    container_workflow: dict[Any, Any],
) -> None:
    """One build of the commit, one push of ``1.2.3``.

    The image is built by one file, and a ``v*`` tag reaches it through
    pypi.yaml's call and through nothing else. Were container.yml also
    triggered by the tag directly, two runs would build the same commit and race
    to push the same version tag, and whichever finished last would silently
    decide what it resolves to.
    """
    assert container_workflow[True]["push"]["tags-ignore"] == ["v*"], (
        "container.yml also triggers on version tags, so it races pypi.yaml"
    )
    assert "workflow_call" in container_workflow[True], (
        "container.yml cannot be called, so pypi.yaml has to build its own image"
    )

    release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    image = release["jobs"]["image"]
    assert image["uses"] == "./.github/workflows/container.yml"
    assert "steps" not in image, "pypi.yaml still builds an image of its own"


@pytest.mark.parametrize("tag", DEVELOPMENT_TAGS)
def test_every_documented_development_tag_is_produced(
    container_workflow: dict[Any, Any], tag: str
) -> None:
    """The three tags docs/docker.md promises, against the metadata that makes them."""
    steps = steps_of(container_workflow, "publish")
    produced = {
        "edge": "type=raw,value=edge,enable={{is_default_branch}}",
        "main": "type=ref,event=branch",
        "sha-": "type=sha",
    }[tag]
    assert produced in steps, f"container.yml no longer produces the {tag!r} tag"


def test_both_architectures_are_built_before_and_during_the_push(
    container_workflow: dict[Any, Any],
) -> None:
    """arm64 is half the machines this image is pulled onto.

    Building it in the ``build`` job as well as the ``publish`` one is what makes
    an arm64 break a red pull request instead of a red default branch.
    """
    for job in ("build", "publish"):
        assert "linux/amd64,linux/arm64" in steps_of(container_workflow, job), (
            f"the {job} job never builds arm64"
        )


def test_the_image_is_run_before_it_is_published(container_workflow: dict[Any, Any]) -> None:
    """Built is not working: the console script, Graphviz and a real render."""
    assert "load: true" in steps_of(container_workflow, "build"), (
        "nothing loads the image into the daemon to run it"
    )
    # The raw file rather than the parsed steps, because what matters here is
    # the exact shell word and ``yaml.dump`` re-escapes every quote in it into
    # something no assertion can read. Same reasoning as tests/test_release.py.
    text = CONTAINER_WORKFLOW.read_text(encoding="utf-8")
    assert "netviz:smoke --version" in text
    assert "netviz:smoke version --json" in text
    assert f'-v "$PWD/examples/{EXAMPLE_INVENTORY}:{MOUNT}:ro" netviz:smoke' in text, (
        "the smoke test never renders a real inventory through the entrypoint"
    )


def test_the_published_manifest_is_pulled_back_and_run() -> None:
    """The one check that a locally-built image cannot stand in for.

    A push can succeed and still leave a tag that resolves to nothing, or an
    index whose entry for an architecture points at the wrong blob. Pulling by
    digest and running it is the only way to find that out here rather than in
    somebody's ``docker run``.
    """
    text = CONTAINER_WORKFLOW.read_text(encoding="utf-8")
    assert 'ref="${IMAGE}@${DIGEST}"' in text, "the image is not pulled back by digest"
    assert 'docker pull --quiet "$ref"' in text
    assert 'docker run --rm "$ref" --version' in text
    assert "imagetools inspect --raw" in text, "the index's architectures are never checked"


def test_the_image_carries_provenance_and_an_sbom(container_workflow: dict[Any, Any]) -> None:
    """Same guarantee the release gives, so ``edge`` can be traced too."""
    steps = steps_of(container_workflow, "publish")
    assert "provenance: mode=max" in steps
    assert "sbom: true" in steps
    assert "attest-build-provenance" in steps
    assert "push-to-registry: true" in steps


def test_both_publishing_workflows_name_the_same_image(
    container_workflow: dict[Any, Any],
) -> None:
    """Two files, one repository in the registry. A typo in either forks it."""
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert container_workflow["env"]["IMAGE"] == release["env"]["IMAGE"]
    assert container_workflow["env"]["IMAGE"] == "ghcr.io/${{ github.repository }}"


def test_the_image_build_cache_is_scoped(
    container_workflow: dict[Any, Any],
) -> None:
    """An unscoped ``type=gha`` is one bucket for every workflow in the repository.

    Anything else that calls buildx would then evict these entries, which turns
    the cheap rebuild this workflow relies on into a full one -- and a full one
    means the emulated arm64 layers, which are the expensive part -- at the least
    convenient moment.
    """
    assert container_workflow["env"]["CACHE_SCOPE"]
    # Both jobs, or the publish job's build is not a hit on what build produced
    # and the arm64 layers are paid for twice in the same run.
    for job in ("build", "publish"):
        assert "scope=${{ env.CACHE_SCOPE }}" in steps_of(container_workflow, job), (
            f"the {job} job's buildx cache is unscoped"
        )


def test_the_docs_page_documents_the_published_development_tags() -> None:
    """A tag nobody can find out about is a tag nobody uses."""
    page = DOC.read_text(encoding="utf-8")
    assert "container.yml" in page, "docs/docker.md does not say what publishes the dev image"
    for tag in ("edge", "sha-"):
        assert f"`{tag}" in page, f"docs/docker.md does not document the {tag!r} tag"
    assert f"{IMAGE}:edge" in page


#: A fenced code block, fence included. What a reader copies.
_FENCED_BLOCK = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)

#: ``ghcr.io/blechschmidt/netviz:<tag>``, capturing the tag.
_IMAGE_REFERENCE = re.compile(re.escape(IMAGE) + r":([A-Za-z0-9][\w.-]*)")


@pytest.mark.parametrize("page", [DOC, README], ids=lambda p: p.name)
def test_no_documented_command_pulls_a_tag_that_does_not_exist(page: Path) -> None:
    """A command in the docs has to work when it is pasted into a terminal.

    Both pages told the reader to ``docker run ghcr.io/…/netviz:latest``, and
    ``latest`` has never been pushed: it is set only by ``pypi.yaml``, and no
    version has been released. So the first thing anybody following the README
    typed came back ``manifest unknown``.

    The tags that do exist are the ones ``container.yml`` produces on every
    push, which is what ``DEVELOPMENT_TAGS`` lists -- ``main`` being the one to
    reach for, since it is the tip of the default branch. Prose may still
    describe ``latest`` and ``X.Y.Z``, because what a release *will* publish is
    worth documenting; a code block may not, because it is an instruction.
    """
    for block in _FENCED_BLOCK.findall(page.read_text(encoding="utf-8")):
        for tag in _IMAGE_REFERENCE.findall(block):
            assert any(tag.startswith(known) for known in DEVELOPMENT_TAGS), (
                f"{page.name} tells the reader to pull {IMAGE}:{tag}, "
                f"which no workflow has published; the pullable tags are {DEVELOPMENT_TAGS}"
            )


# --------------------------------------------------------------------------- #
# The image as a stranger sees it: the ``verify`` job and its script
# --------------------------------------------------------------------------- #
#
# Everything above this line is checked by a runner that is logged in to the
# registry and holds every layer locally. That runner cannot see the two things
# a reader of docs/docker.md depends on -- that the package is public, and that
# the *tag* resolves -- so those are checked by a separate job with no registry
# credential, driving tools/verify_published_image.py. These tests keep that job
# from quietly acquiring the credential that would make it vacuous.


def test_the_published_image_is_verified_without_a_credential(
    container_workflow: dict[Any, Any],
) -> None:
    """GHCR makes a package private on first push, and no workflow flips it.

    A logged-in runner cannot tell that apart from a public one: it pulls
    happily either way. So the check has to run somewhere that has nothing --
    which means the moment this job gains ``packages: read``, it stops proving
    anything at all.
    """
    job = container_workflow["jobs"]["verify"]
    assert job["needs"] == "publish", "nothing verifies the image after it is pushed"
    assert job["permissions"] == {"contents": "read"}, (
        "the verify job holds a registry permission, so it cannot tell public from private"
    )

    steps = steps_of(container_workflow, "verify")
    assert "docker logout ghcr.io" in steps, (
        "a credential left over from an earlier step would defeat the anonymous pull"
    )
    assert "tools/verify_published_image.py" in steps
    # The tag, not the digest -- the publish job already did the digest, and a
    # reader types a tag.
    assert "needs.publish.outputs.tags" in steps, (
        "the verify job does not check a tag the publish job actually pushed"
    )


def test_the_verifier_checks_what_the_docs_page_promises() -> None:
    """Each claim docs/docker.md makes about the image, asserted by the script.

    The page tells a reader they need no login, that both architectures are
    there, and that ``docker run … validate`` works on a folder of YAML. Those
    are the three the verifier exists to hold up, so a page edited to promise
    something new should not silently go unchecked.
    """
    script = (REPO_ROOT / "tools" / "verify_published_image.py").read_text(encoding="utf-8")
    for platform in ("linux/amd64", "linux/arm64"):
        assert platform in script
    assert "anonymous_token" in script, "nothing checks that the package is public"
    assert f"{IMAGE}:edge" in script, "the verifier does not default to the documented dev tag"
    assert MOUNT in script, "the verifier never mounts an inventory where the image expects one"


def test_the_manifest_is_annotated_at_the_index_level(
    container_workflow: dict[Any, Any],
) -> None:
    """``docker/metadata-action`` annotates the per-architecture manifests only.

    Its default for ``DOCKER_METADATA_ANNOTATIONS_LEVELS`` is ``manifest``,
    which leaves the index -- the thing a tag actually resolves to -- carrying
    nothing. Registry UIs, scanners and ``imagetools inspect`` all read the
    index and none of them descend into an architecture to find a title, so the
    published tag looks unlabelled. Observed on ``:edge``, hence this test.
    """
    meta = next(
        step for step in container_workflow["jobs"]["publish"]["steps"] if step.get("id") == "meta"
    )
    levels = meta.get("env", {}).get("DOCKER_METADATA_ANNOTATIONS_LEVELS", "")
    assert "index" in levels.split(","), (
        "annotations land on the architectures only, so the tag itself is unlabelled"
    )


def test_custom_metadata_reaches_both_the_config_and_the_manifest(
    container_workflow: dict[Any, Any],
) -> None:
    """``labels`` and ``annotations`` are separate inputs writing to separate places.

    The action derives its ``annotations`` output from the labels it generated
    itself, *not* from the ``labels`` input, so anything custom passed only
    there reaches ``docker inspect`` and never the registry. Both inputs have to
    carry it, and both have to carry the same thing -- which is why the values
    are named once in the job's environment rather than written out twice.
    """
    job = container_workflow["jobs"]["publish"]
    meta = next(step for step in job["steps"] if step.get("id") == "meta")

    for key in ("description", "documentation"):
        variable = key.upper()
        assert variable in job["env"], f"the {key} is not named once for both inputs"
        for input_name in ("labels", "annotations"):
            assert (
                f"org.opencontainers.image.{key}=${{{{ env.{variable} }}}}"
                in meta["with"][input_name]
            ), f"the custom {key} never reaches the image's {input_name}"
