# netviz in a container: the CLI, the live preview and the browser editor.
#
# Two stages, so what ships carries no build tooling, no pip cache and no copy
# of the source tree -- only the virtual environment the build produced and the
# one dependency netviz cannot vendor: the Graphviz ``dot`` binary behind
# every svg, png, pdf and html render. Being in the image is the point of the
# image; a container is the shortest route to "draw this inventory" on a machine
# where installing Python packages and system packages is not welcome.
#
# Build and run it directly:
#
#     docker build -t netviz:local .
#     docker run --rm -v "$PWD:/inventory:ro" netviz:local validate
#
# or through ``docker-compose.yml``, which wires up the mount, the ports and the
# user id for you. See docs/docker.md.

# Both stages share it, so the interpreter the wheel is built against is the one
# that runs it. 3.12 rather than 3.13 for the same reason CI tops out there.
ARG PYTHON_VERSION=3.12


# --------------------------------------------------------------------------- #
# Stage 1: build the virtual environment
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS build

# uv, out of Astral's distroless image -- a single static binary, no apt and no
# bootstrap install script piped from the network. Both a tag and a digest: the
# tag names the same version ``[tool.uv] required-version`` in pyproject.toml
# does, so the resolver that wrote uv.lock is the resolver that reads it here and
# ``uv sync`` refuses outright if the two ever drift; the digest is what makes
# that a fact rather than a hope, since a tag can be moved. It is the multi-arch
# index digest, so ``linux/amd64`` and ``linux/arm64`` both resolve from it --
# ``container.yml`` builds the image for both.
COPY --from=ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    # The venv this stage produces and the next stage copies. Naming it here is
    # what lets ``uv sync`` build it in place instead of ``.venv`` under /src.
    UV_PROJECT_ENVIRONMENT=/opt/netviz \
    # Byte-compiled once, at build time, rather than on the first run of every
    # container -- which for a read-only rootfs is never, so the interpreter
    # would re-parse the whole package on each start.
    UV_COMPILE_BYTECODE=1

WORKDIR /src

# Only what the environment is built from. ``README.md`` and ``LICENSE`` are not
# decoration here: pyproject.toml points its ``readme`` and ``license`` at them,
# so the build fails without them. ``uv.lock`` is what makes the image
# reproducible -- rebuild this Dockerfile a year from now and it installs the
# same pydantic, the same PyYAML and the same closure underneath them.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# ``--locked`` rather than a plain sync: a lockfile that disagrees with
# pyproject.toml fails the build here instead of silently resolving something
# nobody reviewed into a published image.
#
# ``--no-dev`` because nothing in this image runs the test suite, and
# ``--no-editable`` because the next stage copies /opt/netviz and leaves /src
# behind -- an editable install would ship a .pth file pointing at a directory
# that does not exist in the runtime image.
RUN uv sync --locked --no-dev --no-editable \
    && /opt/netviz/bin/netviz --version


# --------------------------------------------------------------------------- #
# Stage 2: the image that ships
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="netviz" \
      org.opencontainers.image.description="Declare network elements in YAML and render them as network graphs." \
      org.opencontainers.image.source="https://github.com/blechschmidt/netviz" \
      org.opencontainers.image.documentation="https://github.com/blechschmidt/netviz/blob/main/docs/docker.md" \
      org.opencontainers.image.licenses="MIT"

# ``graphviz`` for the raster and vector renders; ``fonts-dejavu-core`` because
# a slim image has no fonts at all and Graphviz then draws labels as boxes.
# ``fc-cache`` builds the font index once here rather than on every first render
# in every container.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        graphviz \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache --force \
    && dot -V

COPY --from=build /opt/netviz /opt/netviz

ENV PATH="/opt/netviz/bin:${PATH}" \
    # Status lines from ``netviz watch`` have to reach ``docker logs`` as they
    # happen, not when a pipe buffer fills.
    PYTHONUNBUFFERED=1 \
    # The venv is already byte-compiled and the root filesystem may be
    # read-only; writing .pyc files at runtime would only produce warnings.
    PYTHONDONTWRITEBYTECODE=1 \
    # ``docker-compose.yml`` runs this image as the *host* user's id so that
    # rendered files land owned by whoever asked for them. That id has no entry
    # in /etc/passwd and therefore no home directory, so anything reaching for
    # one -- fontconfig's cache above all -- is pointed at a path that exists
    # and is writable in every configuration, including a read-only rootfs with
    # a tmpfs on /tmp.
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp

# Present so ``docker run`` with no mount lands somewhere sane rather than
# creating a root-owned directory under a non-root user.
RUN install --directory --owner=1000 --group=1000 /inventory

# Unprivileged by default. Numeric, because the compose file overrides it with
# the host's id and a name would suggest that id is looked up.
USER 1000:1000
WORKDIR /inventory

# The image *is* the command: ``docker run … netviz:local validate`` reads the
# way the tool does. ``--help`` rather than a default action, because guessing
# what an operator meant by "run netviz" is how a container writes a file
# nobody asked for.
ENTRYPOINT ["netviz"]
CMD ["--help"]
