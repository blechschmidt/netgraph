# Running netviz in a container

netviz is a Python package with one dependency it cannot vendor: the Graphviz `dot`
binary, which draws every `svg`, `png`, `pdf` and `html` render. On a machine where
neither a Python environment nor a system package is welcome — a shared CI runner, a
jump host, a colleague's laptop — a container is the shortest route from a folder of
YAML to a diagram.

Every push to the default branch publishes one, so there is nothing to build:

<!-- norun: needs a Docker daemon -->
```bash
docker run --rm -v "$PWD:/inventory:ro" ghcr.io/blechschmidt/netviz:main validate
```

**`main` is the tag to pull, not `latest`.** No version has been released yet, so the
registry holds no `latest` and no `X.Y.Z`: the tags that exist today are `main`, `edge` and
one `sha-…` per commit, all described under [the development
image](#the-development-image). `latest` is reserved for releases and cannot be reached by
a branch build, so an unqualified `docker pull ghcr.io/blechschmidt/netviz` finds nothing
at all rather than finding unreleased work.

## The image

`ghcr.io/blechschmidt/netviz`, built from the [`Dockerfile`](../Dockerfile) in this
repository by [`.github/workflows/container.yml`](../.github/workflows/container.yml) —
on every push to every branch, and again from [`pypi.yaml`](../.github/workflows/pypi.yaml)
when a version is tagged, after the guard, the CI gate and the cross-platform verification
have passed.

| | |
|---|---|
| Registry | `ghcr.io/blechschmidt/netviz` — no login needed to pull |
| Tags today | `main`, `edge`, `sha-…` — see [the development image](#the-development-image) |
| Tags a release adds | `X.Y.Z` (exact), `X.Y` (follows patch releases), `latest` (the newest non-pre-release) |
| Platforms | `linux/amd64`, `linux/arm64` |
| Provenance | a build provenance attestation and an SBOM, both attached to the manifest |

Pin a tag in anything that matters. A moving tag — `main`, `edge`, and `latest` once it
exists — is convenient on a laptop and is the wrong choice in a pipeline, where a commit
you did not ask for should not change what your build does. Until there is a version to
pin, the immutable tag is the commit:

<!-- norun: needs a Docker daemon, and names a commit that may have been pruned -->
```bash
docker pull ghcr.io/blechschmidt/netviz:sha-1a2b3c4
```

The image is signed with GitHub's build provenance, so you can check that the thing you
pulled came from this repository's workflow and not from somewhere else. Branch builds
carry it too, so there is nothing to wait for:

<!-- norun: needs the gh CLI and a Docker daemon -->
```bash
gh attestation verify oci://ghcr.io/blechschmidt/netviz:main \
  --repo blechschmidt/netviz
```

## The development image

Everything in the registry today is unreleased work, built and pushed by
[`.github/workflows/container.yml`](../.github/workflows/container.yml) on **every push to
every branch**, and rebuilt weekly against a fresh base image:

| Tag | Means | Moves |
|---|---|---|
| `edge` | the tip of `main` | yes, on every commit and every Monday |
| `main`, `feature-x` | the branch it was built from | yes, on every push to that branch |
| `sha-1a2b3c4` | one commit, exactly | never |

A branch tag is rewritten in place on each push, so the registry holds one per branch
rather than one per commit — `sha-…` is the tag that accumulates, and the one to write down
if you need to come back to the same bytes later. A branch name containing a slash becomes
a dash: `feature/vlans` publishes as `feature-vlans`.

<!-- norun: needs a Docker daemon -->
```bash
docker run --rm -v "$PWD:/inventory:ro" ghcr.io/blechschmidt/netviz:edge validate
```

Same file, same steps, same two platforms, same provenance attestation and SBOM as a
release — one workflow builds and pushes every image this project publishes, and the tags
are the only thing that differs between a branch build and a release. So `gh attestation
verify` works on these too. What differs is only the promise: `edge` is whatever passed CI
most recently, not something anyone decided to release. It is the tag to reach for when a
fix has landed and you would rather not wait for the version that carries it; a branch tag
is how to run a colleague's work without a Python environment.

**`latest` is never a development build**, which is why there is none to pull yet. It
follows releases and nothing else, so an unqualified `docker pull
ghcr.io/blechschmidt/netviz` cannot land on unreleased work — today it resolves to
nothing at all. A branch build has no way to reach it: `latest` is set only when
`pypi.yaml` asks for it, and it only asks once its guard confirms the version is not a
pre-release. That split is enforced in `tests/test_docker.py`, not just intended, along
with the rule that no example on this page tells you to pull a tag that does not exist.

The weekly rebuild exists because the image is `python:3.12-slim` plus Debian's Graphviz,
and neither takes its security updates from this repository. Without it, `edge` would age
into whatever its base image happened to be on the day some unrelated commit last touched
`src/`.

Pull requests get the build but not the push: both architectures are compiled and the
resulting image is run — `--version`, a Graphviz probe and a real render through the
entrypoint — before anything reaches the registry. The credential that can write to GHCR
is held by a separate job that only ever runs on an already-merged commit.

## Or with compose, for the two servers

[`docker-compose.yml`](../docker-compose.yml) wires up the mount, the ports and the user id
for the three ways the tool is used — one command at a time, as a live preview, and as the
browser editor:

<!-- norun: needs a Docker daemon, and the last two start servers that never exit -->
```bash
docker compose run --rm netviz validate      # the CLI, one shot
docker compose up web                          # the browser editor, http://127.0.0.1:8081/
docker compose up watch                        # the live preview,   http://127.0.0.1:8080/
```

The compose file **builds** the image from the checkout it sits in rather than pulling the
published one, and that is deliberate: it is the development path, where the point is to
run the code in front of you. The first `docker compose run` builds it; after that it is
reused, and `docker compose build --pull` is how a change to your `src/` reaches the
container — the image holds an installed copy, not a mount of the source. To run the
*published* image instead, use `docker run` as above, or edit the two lines of
`docker-compose.yml` that say `build:` and `image:`.

## What is in the image

A two-stage build: the first stage installs the package into a virtual environment, the
second copies that environment into a fresh `python:3.12-slim` next to Graphviz and the
DejaVu fonts. What ships is the environment and those packages — no compiler, no pip
cache, no copy of the source tree, no test suite. About 230 MB, most of it Graphviz and
the interpreter.

Three properties are worth knowing, because commands you type inherit them:

* **The entrypoint is `netviz` itself.** Everything after the image or the service name
  is netviz's own argument list, so `docker compose run --rm netviz list devices` is
  `netviz list devices`. With no arguments it prints `--help` rather than guessing.
* **The working directory is `/inventory`,** which is where the compose file mounts your
  tree. netviz's `-i/--inventory` defaults to the working directory, so no command needs
  to name it.
* **It runs unprivileged,** as uid 1000 by default, with no capabilities, no privilege
  escalation, a read-only root filesystem and a tmpfs on `/tmp`. Nothing netviz does
  needs more than that.

## The three services

| Service | What it is | Port | The mount |
|---|---|---|---|
| `netviz` | Any single command: `validate`, `render`, `list`, `export`, `fmt`, `path`, `ipam`, … | — | read-write |
| `web` | [`netviz web`](commands/web.md): edit YAML in one pane, see the diagram in the other | 8081 | read-only |
| `watch` | [`netviz watch --serve`](commands/watch.md): re-render on every save, served on a page that reloads itself | 8080 | read-only |

`docker compose up` with no service named starts both servers; naming one starts only that one.
The `netviz` service sits behind a compose profile (`cli`) so that it is *not* started
that way — a one-shot command has nothing to keep running, and `up` would report it as a
container that exited. `docker compose run` enables the profile itself, so you never name
it.

The servers mount the inventory **read-only**, because neither writes: `watch` renders to
memory and serves it, and `web` holds the document stream in the browser. The `netviz`
service mounts it read-write, because `render -o`, `fmt`, `export -o`, `init` and `import`
all write into the tree.

Anything the CLI can do, it can do here:

<!-- norun: needs a Docker daemon -->
```bash
docker compose run --rm netviz validate --strict --output-format json
docker compose run --rm netviz render --layer l2 --vlan 10 -f svg -o vlan10.svg
docker compose run --rm netviz export ansible-inventory -o inventory.yaml
docker compose run --rm netviz path pc-alice rtr-gw
```

## Pointing it at your own inventory

The default is `examples/home-lab` from this checkout, so that a fresh clone draws
something before it describes a network of its own. Your own tree is one variable:

<!-- norun: needs a Docker daemon, and names a path on the reader's machine -->
```bash
NETVIZ_INVENTORY=~/net/my-network docker compose run --rm netviz validate
```

or, since typing that on every command gets old, copy [`.env.example`](../.env.example) to
`.env` — compose reads it automatically — and set it there. Every variable the compose
file reads has a default and `.env.example` documents all of them with the value they fall
back to, so the file is a convenience and never a requirement. It is git-ignored: it names
paths and ids on one machine, which is not a fact about this repository.

There are nine variables, and they are the whole configuration surface:
`NETVIZ_INVENTORY` (which tree), `NETVIZ_UID` and `NETVIZ_GID` (who writes),
`NETVIZ_BIND`, `NETVIZ_WEB_PORT` and `NETVIZ_WATCH_PORT` (who can reach the two
servers, and on which host ports), `NETVIZ_LAYER` and `NETVIZ_ICONS` (what the diagram
looks like), and `NETVIZ_YAML_LOADER` (`auto` for libyaml, `python` to force the
pure-Python parser — the same switch CI flips, see [testing.md](testing.md)). Each is
described below or in `.env.example`; anything else is an edit to the compose file, which
is a starting point rather than an interface.

## Files it writes, and who owns them

A container that writes to a bind mount writes as whatever user it runs as, and a file
owned by uid 1000 on a host where you are uid 1002 is a small daily annoyance. So the
compose file runs the image as `${NETVIZ_UID:-1000}:${NETVIZ_GID:-1000}`:

<!-- norun: needs a Docker daemon -->
```bash
NETVIZ_UID=$(id -u) NETVIZ_GID=$(id -g) \
  docker compose run --rm netviz render -f svg -o topology.svg
```

Put those two in `.env` once and every render lands owned by you. The ids need no account
in the image — netviz reads no passwd entry, and `HOME` is set to the tmpfs so that
anything reaching for a home directory (fontconfig, above all) finds a writable one.

## Publishing the ports

Both servers bind `0.0.0.0` *inside* the container, which sounds worse than it is: a
container's loopback is its own, so binding netviz to 127.0.0.1 there would make it
unreachable from the machine running Docker. What decides who can reach it is the
published port, and that is on loopback by default:

```yaml
ports:
  - "${NETVIZ_BIND:-127.0.0.1}:${NETVIZ_WEB_PORT:-8081}:8081"
```

`NETVIZ_BIND=0.0.0.0` publishes to everyone who can reach the host. That is the same
decision `--host` is outside a container, and it deserves the same pause: an inventory
describes internal topology — addresses, VLANs, what is plugged into what.

Because the *container-side* bind is a wildcard, netviz prints its usual warning on
startup — `the preview is bound to every interface` — and inside a container published to
loopback that warning overstates the exposure. It is left in place rather than suppressed:
the process cannot see the port mapping, and a server that decided for itself that its
wildcard bind was fine would be wrong the one time it mattered.

Both servers are development servers. They answer a fixed set of routes, never turn a
request path into a file name, and cap what they will read — but they are not hardened,
and a container does not make them so. Do not publish one to a hostile network.

## When the watcher sees nothing

`watch` re-renders on filesystem events, and a bind mount does not always deliver them:
Docker Desktop on macOS and Windows, anything reached over NFS, anything crossing a VM
boundary. The symptom is a preview that renders once at startup and then never again
although you are saving files. The fix is to poll:

<!-- norun: needs a Docker daemon, and starts a server that never exits -->
```bash
WATCHFILES_FORCE_POLLING=1 docker compose up watch
```

On Linux, where the events do arrive, leave it unset — polling a large tree costs CPU for
nothing.

`NETVIZ_LAYER` chooses what the preview draws (`l1`, `l2`, `l3`, `overlay`, `rack`) and
`NETVIZ_ICONS` chooses the icon theme (`none`, `cisco`). Anything beyond those two is an
edit to the service's `command:` — every flag of [`netviz render`](commands/render.md)
applies to `watch`, and the compose file is a starting point, not an interface.

## Without compose

Nothing above needs compose; it only saves typing. The equivalents, against the published
image:

<!-- norun: needs a Docker daemon, and the last starts a server that never exits -->
```bash
image=ghcr.io/blechschmidt/netviz:main

docker run --rm -v "$PWD:/inventory:ro" "$image" validate
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/inventory" \
  "$image" render -f svg -o topology.svg
docker run --rm --init -p 127.0.0.1:8080:8080 -v "$PWD:/inventory:ro" \
  "$image" watch --serve --host 0.0.0.0
```

Substitute `netviz:local` after a `docker build -t netviz:local .` to run your own
checkout instead.

`--init` matters for the servers. netviz is PID 1 in the container and a Python process
that has installed no `SIGTERM` handler ignores that signal when it is PID 1 — so without
an init, `docker stop` waits out the ten-second grace period and kills it. With one, it
stops in about a second. The compose file sets `init: true` for the same reason.

## In a pipeline

The image is a way to run `netviz validate` on a runner with no Python:

<!-- norun: needs a Docker daemon, and is a fragment of somebody else's pipeline -->
```bash
docker run --rm -v "$PWD:/inventory:ro" ghcr.io/blechschmidt/netviz:sha-1a2b3c4 \
  validate --strict --output-format github
```

One exact commit rather than a moving tag, for the reason above: a pipeline whose behaviour
changes because somebody else pushed to `main` is a pipeline that will fail on a day nobody
touched it. Substitute the short SHA you want; once versions are released, `X.Y.Z` is the
same promise with a friendlier name.

Exit codes and output formats are unchanged by the container — `0` clean, `1` findings,
and `--output-format json|sarif|github` as documented in [ci.md](ci.md). If your runner
does have Python, the [GitHub Action](ci.md#the-github-action) and the
[pre-commit hooks](ci.md#pre-commit) are lighter: no image to build, no daemon to reach.

## What version am I running

The image's entrypoint is netviz itself, so:

<!-- norun: needs a Docker daemon; the versions are properties of the image -->
```bash
docker run --rm ghcr.io/blechschmidt/netviz:main version --json
```

That prints the netviz, Python and Graphviz versions inside the container, which is the
first thing worth pasting into a bug report about a render — see
[`netviz version`](commands/version.md). The image also carries the usual OCI labels, so
`docker inspect` answers the same question without running anything:

<!-- norun: needs a Docker daemon -->
```bash
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' \
  ghcr.io/blechschmidt/netviz:main
```

## How this is kept honest

[`tests/test_docker.py`](../tests/test_docker.py) reads both files and asserts what this
page claims: that every service's command is a real netviz invocation with real flags,
that the published ports are the defaults those commands actually bind, that the servers'
mount is read-only and the CLI service's is not, that every variable has a default, that
`.env.example` documents exactly the variables the compose file reads, and that the image
ends up unprivileged with `netviz` as its entrypoint. The `docker` job in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) then builds the image for real,
runs a command through each service and fetches a page from each server, so a compose file
that parses but does not work fails there.

The published image gets the same treatment before it is published: the `build` job of
[`.github/workflows/container.yml`](../.github/workflows/container.yml) builds `linux/amd64`,
loads it, runs `--version`, checks that Graphviz inside it answers, renders an example to
SVG through the entrypoint — and only then builds both architectures and pushes. So an image
that cannot draw never reaches the registry. See
[`docs/releasing.md`](releasing.md#what-the-release-workflow-does).

And once it is pushed, the same workflow checks it again from the outside, because the job
that did the pushing is the wrong place to ask whether anyone else can pull. That job is
logged in to GHCR and already holds every layer; it cannot tell a public package from a
private one, or a working tag from a digest it happens to have cached. So a third job with
no registry permission at all runs
[`tools/verify_published_image.py`](../tools/verify_published_image.py) against the tag that
was just published. You can run it yourself, on anything:

<!-- norun: needs a Docker daemon and pulls ~250 MB -->
```bash
tools/verify_published_image.py --image ghcr.io/blechschmidt/netviz:edge
```

It asks the registry for an anonymous pull token — which GHCR grants only for a public
package — resolves the tag, checks the index really carries both architectures and the
provenance and SBOM attachments, then pulls it cold and runs it: bare `docker run` prints
help, the entrypoint is `netviz` and reports the version this repository declares,
Graphviz answers, a read-only mount validates and renders, and a writable one comes back
owned by the user who asked rather than by root. `--registry-only` drops everything needing
a daemon, for a quick "is it there and is it public".
