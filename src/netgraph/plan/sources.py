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

__all__ = ["PlanSourceError", "Side", "git_ref", "load_side"]

#: How long to wait for a git subprocess before giving up. Reading a ref out of
#: an object database is a local operation; anything that takes longer than this
#: is a repository asking for a credential on a terminal nobody is watching.
_GIT_TIMEOUT: Final = 60

#: Ceiling on an exported tree, so a mistyped ref pointing at a repository full
#: of binaries cannot fill the temporary directory.
_MAX_ARCHIVE_BYTES: Final = 256 * 1024 * 1024


class PlanSourceError(NetgraphError):
    """One side of a plan cannot be read."""


@dataclass(frozen=True, slots=True)
class Side:
    """One side of a diff: the inventory, and how to describe where it came from."""

    inventory: Inventory
    ref: StateRef


@contextmanager
def git_ref(root: Path, ref: str) -> Iterator[Path]:
    """Export ``ref`` as the inventory root would look at it, in a temp directory.

    Yields the directory. It is removed on the way out, so the caller must have
    finished loading from it before the block ends.

    Raises:
        PlanSourceError: There is no repository, no ``git``, or no such ref.
    """
    repository = _repository_of(root)
    relative = _relative_to(root, repository)
    spec = ref if relative in ("", ".") else f"{ref}:{relative}"
    archive = _archive(repository, spec, ref=ref)
    with TemporaryDirectory(prefix="netgraph-plan-") as directory:
        target = Path(directory)
        _extract(archive, target)
        yield target


def _repository_of(root: Path) -> Path:
    output = _git(
        ["rev-parse", "--show-toplevel"],
        cwd=root if root.is_dir() else root.parent,
        failure=(f"{root} is not inside a git repository, so there is no ref to compare against"),
    )
    return Path(output.decode("utf-8", "replace").strip())


def _relative_to(root: Path, repository: Path) -> str:
    directory = root if root.is_dir() else root.parent
    try:
        relative = directory.resolve().relative_to(repository.resolve())
    except ValueError:  # pragma: no cover - rev-parse just said it is inside
        raise PlanSourceError(f"{root} is not inside {repository}") from None
    return PurePosixPath(relative).as_posix() if relative.parts else ""


def _archive(repository: Path, spec: str, *, ref: str) -> bytes:
    return _git(
        ["archive", "--format=tar", spec],
        cwd=repository,
        failure=(
            f"git cannot read {ref!r}; check that the ref exists and that the inventory "
            f"directory is present in it"
        ),
    )


def _git(arguments: list[str], *, cwd: Path, failure: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise PlanSourceError(
            "git is not on the PATH, so '--from <ref>' cannot read a revision; "
            "compare two folders instead"
        ) from None
    except OSError as error:
        raise PlanSourceError(f"could not run git: {error}") from error
    except subprocess.TimeoutExpired:
        raise PlanSourceError(
            f"git did not answer within {_GIT_TIMEOUT}s; it may be waiting for a credential"
        ) from None
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise PlanSourceError(f"{failure}\n  git: {detail[-1] if detail else 'failed'}")
    if len(completed.stdout) > _MAX_ARCHIVE_BYTES:
        raise PlanSourceError(
            f"the exported tree is larger than {_MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB; "
            f"point --from at the inventory directory rather than at the whole repository"
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
