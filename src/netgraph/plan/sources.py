"""Where the two sides of a plan come from: the tree, a folder, or a git ref.

Both sides of a diff are an :class:`~netgraph.loader.Inventory`, and this module
is the only place that knows there is more than one way to get one. A folder is
loaded directly. A git ref is exported to a temporary directory and loaded from
there — with ``git archive`` rather than ``git stash``, ``git checkout`` or a
worktree, because a command that reads history must not be able to disturb the
working tree it is being run against.

``git`` itself is not a dependency: a tree that is not in a repository, or a
machine with no ``git`` on its path, simply cannot use ``--from <ref>``, and
says so.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Final

from netgraph.errors import NetgraphError
from netgraph.loader import Inventory, load_tree
from netgraph.plan.model import StateRef
from netgraph.plan.state import state_digest

__all__ = [
    "GIT_TIMEOUT",
    "MAX_OUTPUT_BYTES",
    "MissingInventory",
    "PlanSourceError",
    "Side",
    "check_revision",
    "git",
    "git_ref",
    "inventory_prefix",
    "load_side",
    "repository_of",
]

#: How long to wait for a git subprocess before giving up. Reading a ref out of
#: an object database is a local operation; anything that takes longer than this
#: is a repository asking for a credential on a terminal nobody is watching.
GIT_TIMEOUT: Final = 60

#: Ceiling on what one git command may answer with, so a mistyped ref pointing
#: at a repository full of binaries cannot fill the temporary directory.
MAX_OUTPUT_BYTES: Final = 256 * 1024 * 1024


class PlanSourceError(NetgraphError):
    """One side of a plan cannot be read."""


class MissingInventory(PlanSourceError):
    """The ref exists, and the inventory directory does not exist in it.

    Its own type because it is the one failure that is not a mistake: a
    repository grew its ``netgraph/`` folder at some commit, and every revision
    before that one legitimately has nothing to draw. A timeline says so per
    frame and carries on; a single ``--from`` still refuses.
    """


@dataclass(frozen=True, slots=True)
class Side:
    """One side of a diff: the inventory, and how to describe where it came from."""

    inventory: Inventory
    ref: StateRef


def check_revision(rev: str) -> str:
    """Refuse a "revision" that git would read as an option, and return it.

    ``git log`` and ``git archive`` take options that write files
    (``--output=``) and run programs (``--upload-pack=``), and a revision goes
    into the same argument list. Nothing that reaches this is trusted to be a
    revision: ``netgraph web`` takes one from a query string, and a page this
    server did not write can put anything there. A leading ``-`` is the whole
    of what makes an argument an option to git, so that is the whole of the
    check — and it is done here rather than at each call site, because the one
    that forgets is the one that matters.

    Raises:
        PlanSourceError: ``rev`` is empty, starts with ``-``, or holds a
            character no revision may hold.
    """
    if not rev:
        raise PlanSourceError("a revision is needed; nothing was given")
    if rev.startswith("-"):
        raise PlanSourceError(
            f"{rev!r} is not a revision: a leading '-' would be read as an option by git"
        )
    if any(character in rev for character in "\0\n\r"):
        raise PlanSourceError("a revision holds no newline or null byte")
    return rev


@contextmanager
def git_ref(root: Path, ref: str) -> Iterator[Path]:
    """Export ``ref`` as the inventory root would look at it, in a temp directory.

    Yields the directory. It is removed on the way out, so the caller must have
    finished loading from it before the block ends.

    Raises:
        MissingInventory: The ref resolves and holds no inventory directory.
        PlanSourceError: There is no repository, no ``git``, no such ref, or
            ``ref`` is not a revision at all (see :func:`check_revision`).
    """
    check_revision(ref)
    repository = repository_of(root)
    relative = inventory_prefix(root, repository)
    spec = ref if relative in ("", ".") else f"{ref}:{relative}"
    archive = _archive(repository, spec, ref=ref, prefix=relative)
    with TemporaryDirectory(prefix="netgraph-plan-") as directory:
        target = Path(directory)
        _extract(archive, target)
        yield target


def repository_of(root: Path) -> Path:
    """The work tree ``root`` sits in.

    Raises:
        PlanSourceError: It sits in none, or ``git`` cannot be run.
    """
    output = git(
        ["rev-parse", "--show-toplevel"],
        cwd=root if root.is_dir() else root.parent,
        failure=(f"{root} is not inside a git repository, so there is no ref to compare against"),
    )
    return Path(output.decode("utf-8", "replace").strip())


def inventory_prefix(root: Path, repository: Path) -> str:
    """Where the inventory sits inside ``repository``, as a posix path.

    ``""`` when the inventory *is* the repository root, which is the one case a
    caller has to spell differently: ``<rev>:`` is not a tree-ish and ``<rev>``
    is.
    """
    directory = root if root.is_dir() else root.parent
    try:
        relative = directory.resolve().relative_to(repository.resolve())
    except ValueError:  # pragma: no cover - rev-parse just said it is inside
        raise PlanSourceError(f"{root} is not inside {repository}") from None
    return PurePosixPath(relative).as_posix() if relative.parts else ""


def _archive(repository: Path, spec: str, *, ref: str, prefix: str) -> bytes:
    try:
        return git(
            ["archive", "--format=tar", spec],
            cwd=repository,
            failure=(
                f"git cannot read {ref!r}; check that the ref exists and that the inventory "
                f"directory is present in it"
            ),
        )
    except PlanSourceError:
        # Two very different situations reach the same non-zero exit: a ref
        # nobody has ever heard of, and a ref from before the inventory
        # existed. Only the second is worth continuing past, so it is worth
        # one extra call to tell them apart.
        if prefix and _resolves(repository, ref) and not _resolves(repository, spec):
            raise MissingInventory(
                f"{ref} has no {prefix!r} directory in it, so there is no inventory to read "
                f"at that revision"
            ) from None
        raise


def _resolves(repository: Path, spec: str) -> bool:
    """Does ``spec`` name an object in this repository?"""
    try:
        git(["rev-parse", "--verify", "--quiet", f"{spec}^{{}}"], cwd=repository, failure="")
    except MissingInventory:  # pragma: no cover - _archive is the only caller
        return False
    except PlanSourceError:
        return False
    return True


def git(arguments: list[str], *, cwd: Path, failure: str, stdin: bytes | None = None) -> bytes:
    """Run one git command in ``cwd`` and return its stdout.

    The only place in netgraph that starts a git process for reading history,
    so the timeout, the "git is not installed" message and the output ceiling
    are decided once.

    Raises:
        PlanSourceError: git is absent, timed out, failed, or answered with
            more bytes than :data:`MAX_OUTPUT_BYTES`.
    """
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise PlanSourceError(
            "git is not on the PATH, so a revision cannot be read; compare two folders instead"
        ) from None
    except OSError as error:
        raise PlanSourceError(f"could not run git: {error}") from error
    except subprocess.TimeoutExpired:
        raise PlanSourceError(
            f"git did not answer within {GIT_TIMEOUT}s; it may be waiting for a credential"
        ) from None
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise PlanSourceError(f"{failure}\n  git: {detail[-1] if detail else 'failed'}")
    if len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise PlanSourceError(
            f"git answered with more than {MAX_OUTPUT_BYTES // (1024 * 1024)} MiB; "
            f"point the inventory at its own directory rather than at the whole repository"
        )
    return completed.stdout


def _extract(archive: bytes, target: Path) -> None:
    """Unpack a tar stream, refusing anything that is not a plain file under it.

    ``tarfile`` will happily write through an absolute path or a ``..`` entry,
    and ``extractall``'s ``filter`` argument is not available on every
    interpreter this package supports. Walking the members is both portable and
    exact about what is allowed: regular files, nothing else.
    """
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            if not member.isfile():
                continue
            destination = _safe_path(target, member.name)
            if destination is None:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:  # pragma: no cover - isfile() said otherwise
                continue
            with source, destination.open("wb") as handle:
                handle.write(source.read())


def _safe_path(target: Path, name: str) -> Path | None:
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        return None
    destination = target.joinpath(*candidate.parts)
    try:
        destination.resolve().relative_to(target.resolve())
    except ValueError:  # pragma: no cover - the parts check already caught it
        return None
    return destination


def load_side(root: Path, *, kind: str, description: str) -> Side:
    """Load an inventory from ``root`` and describe it as a plan side."""
    inventory = load_tree(root)
    return Side(
        inventory=inventory,
        ref=StateRef(kind=kind, description=description, digest=state_digest(inventory)),
    )
