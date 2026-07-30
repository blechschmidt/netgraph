"""The files a report consists of, and writing them to a directory.

A :class:`Bundle` is an ordered mapping of bundle-relative path to bytes. Keeping
it in memory until the whole report has been rendered buys three things:

* **an all-or-nothing write.** A template that fails halfway through leaves the
  destination untouched rather than half a report on disk;
* **a testable artefact.** The golden tests compare a mapping, and never need a
  temporary directory to find out what would have been written;
* **a stale-file report.** Because the bundle knows every file it owns, it can say
  which files in the destination it did *not* write — the pages of a device that
  has since been deleted, which would otherwise sit in a published report forever
  looking current.

Writing is deliberately conservative: it creates directories and overwrites the
files of the bundle, and it deletes nothing unless ``prune`` asks it to. ``--out``
points at a directory a person chose, and a documentation generator that quietly
removes files from one is a documentation generator nobody points at anything
twice.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from netgraph.errors import NetgraphError
from netgraph.fsio import write_bytes_atomically

__all__ = ["Bundle"]


@dataclass(frozen=True, slots=True)
class Bundle:
    """Every file of one report, keyed by its path relative to ``--out``."""

    files: Mapping[str, bytes] = field(default_factory=dict)

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path in the bundle, sorted."""
        return tuple(sorted(self.files))

    @property
    def size(self) -> int:
        """Total bytes the bundle would write."""
        return sum(len(payload) for payload in self.files.values())

    def text(self, path: str) -> str:
        """One file as text. For the tests, and for a caller inspecting a page.

        Raises:
            KeyError: The bundle holds no such file.
        """
        return self.files[path].decode("utf-8")

    def merged(self, extra: Mapping[str, bytes]) -> Bundle:
        """This bundle plus ``extra`` — the diagrams, usually."""
        return Bundle(files={**self.files, **extra})

    def write(self, destination: Path, *, prune: bool = False) -> tuple[str, ...]:
        """Write every file under ``destination``.

        Args:
            destination: The directory to write into; created if absent.
            prune: Delete files under ``destination`` that this bundle does not
                hold. Off by default, and reported either way — see
                :meth:`stale`.

        Returns:
            The stale paths: files that were already there and are not part of
            this report. They have been deleted when ``prune`` is set, and are
            merely reported otherwise.

        Raises:
            NetgraphError: The destination cannot be written.
        """
        stale = self.stale(destination)
        try:
            for path in self.paths:
                target = destination / PurePosixPath(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                # Atomically, so a reader refreshing a page mid-write sees the
                # old file or the new one and never half of either.
                write_bytes_atomically(target, self.files[path], sync=False)
            if prune:
                for path in stale:
                    target = destination / PurePosixPath(path)
                    target.unlink(missing_ok=True)
                    _prune_empty(target.parent, stop=destination)
        except OSError as exc:
            raise NetgraphError(
                f"cannot write the report to {destination}: {exc.strerror or exc}"
            ) from exc
        return stale

    def stale(self, destination: Path) -> tuple[str, ...]:
        """Files already under ``destination`` that this report does not hold.

        Only the file *kinds* a report writes are considered — ``.md``, ``.html``,
        ``.svg`` and ``.json``. A reader who put a photograph of the rack next to
        the pages did so on purpose, and it is not this command's business to call
        it stale.
        """
        if not destination.is_dir():
            return ()
        mine = set(self.files)
        return tuple(
            sorted(
                path
                for path in _walk(destination)
                if path not in mine and PurePosixPath(path).suffix in _OWNED_SUFFIXES
            )
        )


#: The suffixes a report claims ownership of; see :meth:`Bundle.stale`.
_OWNED_SUFFIXES = frozenset({".md", ".html", ".svg", ".json"})


def _walk(directory: Path) -> Iterator[str]:
    """Every file under ``directory``, as a POSIX path relative to it."""
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            yield PurePosixPath(path.relative_to(directory)).as_posix()


def _prune_empty(directory: Path, *, stop: Path) -> None:
    """Remove ``directory`` and its parents up to ``stop``, while they are empty.

    A bundle whose last device page moved out of ``devices/`` should not leave the
    directory behind: the layout is part of what a reader reads. Only the
    directories a deletion just emptied are considered — an empty directory of the
    reader's own, sitting beside the report, is none of this command's business.
    """
    current = directory.resolve()
    root = stop.resolve()
    while current != root and root in current.parents:
        if any(current.iterdir()):
            return
        current.rmdir()
        current = current.parent
