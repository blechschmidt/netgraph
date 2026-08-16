#!/usr/bin/env python3
"""Check that the published container is there, is public, and actually runs.

``container.yml`` already pulls its own push back by digest and runs it, which
catches a manifest that uploaded but cannot be resolved. What that step cannot
check is everything that makes the image usable *by someone else*:

* the package is **public** -- a workflow runner is logged in to GHCR, so it
  cannot tell a world-readable package from one only it can see. GHCR creates a
  package private on first push and nothing in a workflow flips it, so the
  failure mode is silent: green pipeline, and ``docker pull`` on any other
  machine says ``denied``. That is the one this script exists for.
* the **tag** resolves. The workflow verifies ``@sha256:…``; a reader follows
  ``docs/docker.md`` and types ``:main`` or ``:edge``.
* the **entrypoint** is netviz with no arguments needed, the way
  ``ENTRYPOINT ["netviz"]`` promises, and bare ``docker run IMAGE`` prints
  help rather than doing something nobody asked for.
* the image can **draw** -- Graphviz present and findable, an inventory
  rendered from a read-only mount, as an unprivileged user, in the working
  directory the compose file and the docs both assume.

Registry checks speak HTTPS directly and need no Docker daemon and no
credentials; that is the point, since a credential would defeat the test. The
runtime checks need a daemon and are skipped, loudly, without one.

    tools/verify_published_image.py
    tools/verify_published_image.py --image ghcr.io/blechschmidt/netviz:main
    tools/verify_published_image.py --registry-only --json

Exit status is 0 when every check that ran passed, 1 when one failed, and 2 for
a usage error. A skipped check does not fail the run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: What ``docs/docker.md`` tells a reader to pull when they want the tip of
#: ``main``. Every push to the default branch moves it, so it is the tag that is
#: always there to check -- unlike ``latest``, which exists only after a release.
DEFAULT_IMAGE = "ghcr.io/blechschmidt/netviz:edge"

#: The inventory rendered through the entrypoint. Committed, small, and the same
#: tree ``container.yml``'s own smoke test uses.
EXAMPLE_INVENTORY = REPO_ROOT / "examples" / "home-lab"

#: Both halves of the claim ``docs/docker.md`` makes under "Platforms".
REQUIRED_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})

#: An OCI index and a Docker manifest list mean the same thing here, and GHCR
#: serves whichever the ``Accept`` header asks for. Both are listed so that this
#: keeps working whichever buildx decides to push.
INDEX_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)

MANIFEST_ACCEPT = ", ".join(
    [
        *sorted(INDEX_TYPES),
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

#: How long any one network call or container run may take. Long enough for a
#: cold pull of a ~250 MB image on a slow link, short enough that a hung daemon
#: fails the script rather than the job's overall limit.
TIMEOUT = 600


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class Check:
    """One thing that was asserted, and how it went."""

    name: str
    status: str  # "pass", "fail" or "skip"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "fail"


@dataclass
class Report:
    """Everything that was asserted, in the order it was asserted."""

    image: str
    checks: list[Check] = field(default_factory=list)

    def record(self, name: str, status: str, detail: str = "") -> Check:
        check = Check(name, status, detail)
        self.checks.append(check)
        icon = {"pass": "ok  ", "fail": "FAIL", "skip": "skip"}[status]
        print(f"  {icon}  {name}" + (f" -- {detail}" if detail else ""), flush=True)
        return check

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if check.status == "fail"]

    @property
    def skipped(self) -> list[Check]:
        return [check for check in self.checks if check.status == "skip"]


@contextmanager
def checking(report: Report, name: str) -> Iterator[Callable[[str], None]]:
    """Record one check, turning whatever went wrong inside into its failure.

    The body calls the yielded function to attach detail to a pass; raising
    :class:`Failure` -- or anything else, including a network error or a
    ``KeyError`` from a manifest shaped differently than expected -- makes it a
    fail with the exception as the reason. That way a check cannot accidentally
    pass by crashing, and one broken check does not abandon the rest.
    """

    detail = ""

    def detailed(text: str) -> None:
        nonlocal detail
        detail = text

    try:
        yield detailed
    except Failure as error:
        report.record(name, "fail", str(error))
    except Exception as error:
        report.record(name, "fail", f"{type(error).__name__}: {error}")
    else:
        report.record(name, "pass", detail)


class Failure(Exception):
    """A check's own verdict, as opposed to something unexpected going wrong."""


# --------------------------------------------------------------------------- #
# The registry, anonymously
# --------------------------------------------------------------------------- #


def split_reference(reference: str) -> tuple[str, str, str] | None:
    """``ghcr.io/owner/name:tag`` -> registry, repository, tag.

    A digest is accepted in place of a tag; ``@sha256:…`` is returned as the tag
    because that is what the manifest endpoint takes either way. ``None`` means
    the reference names no registry -- ``netviz:local``, an image that exists
    only in a daemon -- for which the registry half of this script has nothing
    to ask and the runtime half still has everything.
    """

    if "@" in reference:
        name, _, digest = reference.partition("@")
        tag = digest
    elif ":" in reference.rsplit("/", 1)[-1]:
        name, _, tag = reference.rpartition(":")
    else:
        name, tag = reference, "latest"

    registry, _, repository = name.partition("/")
    if "." not in registry or not repository:
        return None
    return registry, repository, tag


def anonymous_token(registry: str, repository: str) -> str:
    """A pull token for an unauthenticated caller, or raise.

    GHCR hands one out for a public package and refuses for a private or absent
    one, which is exactly the distinction this script is for. The refusal is a
    403 with a ``DENIED`` body rather than a 401 challenge, so it has to be read
    off the error rather than left to urllib.
    """

    url = f"https://{registry}/token?scope=repository:{repository}:pull&service={registry}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace").strip()
        raise Failure(
            f"{registry} refused an anonymous pull token for {repository} "
            f"(HTTP {error.code}): {body}"
        ) from error
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise Failure(f"no token in the response from {url}")
    return str(token)


def fetch_manifest(
    registry: str, repository: str, tag: str, token: str
) -> tuple[dict[str, Any], str]:
    """The manifest a tag resolves to, and the digest it resolved to."""

    url = f"https://{registry}/v2/{repository}/manifests/{tag}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            manifest = json.load(response)
            digest = response.headers.get("Docker-Content-Digest", "")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace").strip()
        raise Failure(f"{tag} does not resolve (HTTP {error.code}): {body}") from error
    return manifest, digest


def platforms_of(index: dict[str, Any]) -> set[str]:
    """The real architectures in an index.

    Provenance and SBOM attestations ride in the same index as entries whose
    platform is ``unknown/unknown``. They are not architectures, and counting
    them would let an index with one architecture and two attestations look like
    three platforms.
    """

    found = set()
    for entry in index.get("manifests", []):
        platform = entry.get("platform") or {}
        if platform.get("architecture") in (None, "unknown"):
            continue
        found.add(f"{platform.get('os')}/{platform['architecture']}")
    return found


def attestation_count(index: dict[str, Any]) -> int:
    """Entries referring back to another manifest -- buildx's attestations."""

    return sum(
        1
        for entry in index.get("manifests", [])
        if (entry.get("annotations") or {}).get("vnd.docker.reference.type")
    )


def check_registry(report: Report, reference: str) -> None:
    """Everything checkable without a Docker daemon or a credential."""

    parts = split_reference(reference)
    if parts is None:
        print(f"\nregistry: none named in {reference!r}")
        report.record("the package is public", "skip", "a local reference names no registry")
        return
    registry, repository, tag = parts
    print(f"\nregistry: {registry}/{repository}, tag {tag}")

    token = ""
    with checking(report, "the package is public (anonymous pull token granted)") as detail:
        token = anonymous_token(registry, repository)
        detail("no credential needed, as docs/docker.md promises")

    if not token:
        report.record(
            "the tag resolves to a manifest",
            "skip",
            "no anonymous access; nothing further can be read",
        )
        return

    index: dict[str, Any] = {}
    with checking(report, "the tag resolves to a manifest") as detail:
        index, digest = fetch_manifest(registry, repository, tag, token)
        detail(digest or "resolved, but the registry sent no digest header")

    if not index:
        return

    with checking(report, "it is a multi-architecture index") as detail:
        media_type = index.get("mediaType", "")
        if media_type not in INDEX_TYPES:
            raise Failure(f"{tag} is a single manifest, not an index: {media_type!r}")
        detail(media_type)

    with checking(report, "both published architectures are in it") as detail:
        found = platforms_of(index)
        missing = REQUIRED_PLATFORMS - found
        if missing:
            raise Failure(f"missing {sorted(missing)}; the index has {sorted(found) or 'none'}")
        detail(", ".join(sorted(found)))

    with checking(report, "the index carries provenance and an SBOM") as detail:
        count = attestation_count(index)
        if count < len(REQUIRED_PLATFORMS):
            raise Failure(
                f"{count} attestation manifest(s); expected one provenance and one SBOM "
                f"per architecture"
            )
        detail(f"{count} attestation manifests")

    with checking(report, "the index is annotated with where it came from") as detail:
        annotations = index.get("annotations") or {}
        source = annotations.get("org.opencontainers.image.source")
        if not source:
            raise Failure(
                "no org.opencontainers.image.source annotation on the index -- "
                "the tag looks unlabelled to anything reading the manifest list"
            )
        detail(source)


# --------------------------------------------------------------------------- #
# The image, run
# --------------------------------------------------------------------------- #


def docker(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        input=stdin,
        check=False,
    )


def run_image(reference: str, *args: str, mounts: tuple[str, ...] = ()) -> str:
    """``docker run`` the image, returning stdout or raising with the reason."""

    command = ["run", "--rm", *mounts, reference, *args]
    result = docker(*command)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        raise Failure(
            f"`docker {' '.join(command)}` exited {result.returncode}: "
            + (tail[-1] if tail else "no output")
        )
    return result.stdout


def declared_version() -> str:
    """The version in ``pyproject.toml``.

    Read the same way ``container.yml`` reads it, and for the same reason: a
    literal here would have to be edited in the commit that bumps the version,
    and a check edited alongside what it checks is not a check.
    """

    for line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise Failure("no version in pyproject.toml")


def check_runtime(report: Report, reference: str, expect_version: str | None) -> None:
    """Everything that needs the image actually running."""

    print(f"\nruntime: {reference}")

    if shutil.which("docker") is None:
        report.record("the image pulls", "skip", "no docker on PATH")
        return

    if docker("version", "--format", "{{.Server.Version}}").returncode != 0:
        report.record("the image pulls", "skip", "no reachable Docker daemon")
        return

    pulled = split_reference(reference) is None
    if pulled:
        report.record(
            "the image pulls without logging in", "skip", "already local; nothing to pull"
        )
    else:
        with checking(report, "the image pulls without logging in") as detail:
            # Whatever this machine already holds under that tag is not what is
            # being tested; a stale local copy would let a tag that no longer
            # exists pass every check below.
            docker("image", "rm", "--force", reference)
            result = docker("pull", "--quiet", reference)
            if result.returncode != 0:
                raise Failure((result.stderr or result.stdout).strip().splitlines()[-1])
            pulled = True
            detail(result.stdout.strip())

    if not pulled:
        return

    with checking(report, "bare `docker run` prints help and exits clean") as detail:
        output = run_image(reference)
        if "Usage:" not in output:
            raise Failure(f"expected usage text, got {output[:120]!r}")
        detail(f"{len(output.splitlines())} lines of help")

    with checking(report, "the entrypoint is netviz itself") as detail:
        # No `netviz` in the argument list: ENTRYPOINT supplies it, which is
        # the whole claim being made by `docker run IMAGE validate`.
        reported = run_image(reference, "--version").strip().splitlines()[0]
        if not reported.startswith("netviz "):
            raise Failure(f"`--version` reported {reported!r}")
        if expect_version and reported != f"netviz {expect_version}":
            raise Failure(f"reported {reported!r}, expected version {expect_version}")
        detail(reported)

    with checking(report, "Graphviz is present and netviz can find it") as detail:
        report_json = json.loads(run_image(reference, "version", "--json"))
        graphviz = report_json.get("graphviz") or {}
        if not graphviz.get("version"):
            raise Failure(f"no usable Graphviz in the image: {graphviz}")
        detail(f"{graphviz['version']} at {graphviz.get('path')}")

    with checking(report, "it runs unprivileged, in /inventory") as detail:
        # Read off the tool rather than by overriding the entrypoint with `id`:
        # what matters is the context netviz itself runs in.
        env = json.loads(run_image(reference, "version", "--json"))
        detail(str(env.get("python", {}).get("version", "")).split()[0])
        inspected = docker(
            "image", "inspect", "--format", "{{.Config.User}}|{{.Config.WorkingDir}}", reference
        )
        user, _, workdir = inspected.stdout.strip().partition("|")
        if user in ("", "root", "0", "0:0"):
            raise Failure(f"the image runs as {user or 'root'}")
        if workdir != "/inventory":
            raise Failure(f"working directory is {workdir!r}, not /inventory")
        detail(f"user {user}, workdir {workdir}")

    with checking(report, "a read-only inventory validates through the entrypoint") as detail:
        output = run_image(
            reference,
            "validate",
            mounts=("-v", f"{EXAMPLE_INVENTORY}:/inventory:ro"),
        )
        detail(output.strip().splitlines()[-1] if output.strip() else "clean")

    with checking(report, "it renders SVG from a read-only mount") as detail:
        svg = run_image(
            reference,
            "render",
            "--layer",
            "l2",
            "-f",
            "svg",
            mounts=("-v", f"{EXAMPLE_INVENTORY}:/inventory:ro"),
        )
        if "<svg" not in svg:
            raise Failure(f"no SVG in {len(svg)} bytes of output")
        detail(f"{len(svg)} bytes")

    with checking(report, "it writes into a mounted directory as the calling user") as detail:
        # The other half of the read-only case, and the one a real user hits:
        # `-u $(id -u)` with a writable mount has to leave a file behind that
        # they own, not one owned by root or by uid 1000.
        caller = os.getuid()
        with tempfile.TemporaryDirectory() as scratch:
            run_image(
                reference,
                # No -i: WORKDIR is the mount, which is why nothing in
                # docker-compose.yml names the tree it reads either.
                "render",
                "-o",
                "/out/topology.svg",
                mounts=(
                    "--user",
                    str(caller),
                    "-v",
                    f"{EXAMPLE_INVENTORY}:/inventory:ro",
                    "-v",
                    f"{scratch}:/out",
                ),
            )
            written = Path(scratch) / "topology.svg"
            if not written.is_file():
                raise Failure("the render wrote nothing to the mounted directory")
            if written.stat().st_uid != caller:
                raise Failure(f"the file landed owned by uid {written.stat().st_uid}, not {caller}")
            detail(f"{written.stat().st_size} bytes, owned by uid {caller}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--image", default=DEFAULT_IMAGE, help=f"image reference (default {DEFAULT_IMAGE})"
    )
    parser.add_argument(
        "--registry-only",
        action="store_true",
        help="skip everything that needs a Docker daemon",
    )
    parser.add_argument(
        "--expect-version",
        nargs="?",
        const="",
        default=None,
        metavar="X.Y.Z",
        help="require `--version` to report this; bare, the version in pyproject.toml",
    )
    parser.add_argument(
        "--json", action="store_true", help="also write the report as JSON to stdout"
    )
    arguments = parser.parse_args(argv)

    expect = arguments.expect_version
    if expect == "":
        expect = declared_version()

    report = Report(image=arguments.image)
    print(f"verifying {arguments.image}")
    check_registry(report, arguments.image)
    if arguments.registry_only:
        report.record("the image runs", "skip", "--registry-only")
    else:
        check_runtime(report, arguments.image, expect)

    passed = sum(1 for check in report.checks if check.status == "pass")
    print(f"\n{passed} passed, {len(report.failed)} failed, {len(report.skipped)} skipped")
    for check in report.failed:
        print(f"  FAIL  {check.name}: {check.detail}")

    if arguments.json:
        print(
            json.dumps(
                {
                    "image": report.image,
                    "checks": [vars(check) for check in report.checks],
                    "failed": len(report.failed),
                },
                indent=2,
            )
        )

    return 1 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)
