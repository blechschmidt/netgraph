"""Writing a configuration tree to disk, and refusing to write over somebody else.

Nothing else in :mod:`netgraph.export` touches the filesystem: an emitter returns
a string and the CLI decides where it goes. A configuration set cannot work that
way — it is a tree, and ``--out DIR`` has to create directories and decide what to
do about what is already in them — so the filesystem half lives here, on its own,
where it can be reasoned about once.

The rule about overwriting is the only interesting decision, and it is deliberately
not the one :func:`netgraph.importer.write_files` uses. An imported tree is
hand-edited immediately, so clobbering it is a real loss and ``--force`` is the
right gate. A generated configuration is the opposite: it says *do not edit* in
its first line, regenerating it is the normal operation, and a command that
demanded ``--force`` on every run in a pipeline would train everybody to pass
``--force`` on every run, which is the same as having no gate at all.

So the gate is narrower and, for that reason, worth something:

**A file netgraph generated is overwritten.** It carries the banner
(:func:`netgraph.banner.parse_banner`), so there is no doubt.

**A file netgraph did not generate is refused**, with every clash listed at once
and ``--force`` named. That is the case worth stopping: an operator who pointed
``--out`` at ``/etc`` rather than at a staging directory, which is a mistake this
command makes easy and expensive.

**Nothing is ever deleted.** A device dropped from the inventory leaves its
directory behind, and a stale configuration that nobody applies is harmless while
a directory tree removed by a tool is not. :func:`stale_files` finds them and the
CLI reports the count, so the operator can remove them knowing what they were.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Final

from netgraph.banner import DIALECT_KEY, parse_banner
from netgraph.errors import NetgraphError
from netgraph.export.config.model import ConfigSet
from netgraph.fsio import write_text

__all__ = ["ConfigWriteError", "stale_files", "write_config"]

#: How many clashing paths a refusal names before summarising the rest. The same
#: figure ``netgraph import`` uses, for the same reason: a wall of paths is not
#: a diagnostic.
MAX_LISTED_CLASHES: Final = 8

#: How much of an existing file is read to look for the banner. The banner is the
#: first six lines of anything netgraph writes; a few kilobytes is generous and
#: bounds the cost of pointing ``--out`` at a directory full of disk images.
_BANNER_BYTES: Final = 4096


class ConfigWriteError(NetgraphError):
    """Raised when a configuration tree cannot be written where it was asked to.

    Shares its exit status with :class:`~netgraph.errors.LoaderError`: from the
    operator's side both mean netgraph could not do the file operation it was
    pointed at.
    """

    exit_code = 3


def write_config(
    config: ConfigSet, target: Path, *, force: bool = False, inventory_root: Path | None = None
) -> list[Path]:
    """Write ``config`` under ``target``, one directory per device.

    Args:
        config: The set to write, from
            :func:`~netgraph.export.config.generate`.
        target: The output directory. Created, with its parents, when absent.
        force: Overwrite files netgraph did not generate, and write inside the
            inventory tree.
        inventory_root: Where the inventory was loaded from, when the caller
            knows. See :func:`_refuse_inside_inventory`.

    Returns:
        The files written, in the order the set holds them.

    Raises:
        ConfigWriteError: ``target`` is inside the inventory, is not a directory,
            holds a file netgraph did not write, or a write failed.
    """
    if target.exists() and not target.is_dir():
        raise ConfigWriteError(f"{target} exists and is not a directory")
    if not force:
        _refuse_inside_inventory(target, inventory_root)
        _refuse_foreign(config, target)

    written: list[Path] = []
    for relative, content in config.files():
        path = target.joinpath(*PurePosixPath(relative).parts)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # LF on every platform; see netgraph.fsio. A generated configuration
            # is compared against the next generation of itself, and against the
            # running config of a device that is almost certainly not Windows.
            write_text(path, content)
        except OSError as exc:
            raise ConfigWriteError(f"cannot write {path}: {exc.strerror or exc}") from exc
        written.append(path)
    return written


def stale_files(config: ConfigSet, target: Path) -> tuple[Path, ...]:
    """Files under ``target`` netgraph generated that this run did not rewrite.

    A device removed from the inventory, a filter narrowed since the last run, or
    a dialect swapped for another all leave files behind. They are found rather
    than deleted: what to do about a configuration nobody generates any more is
    an operator's decision, and a tool that removed a tree it merely *believed*
    it owned would be a tool nobody points at ``/etc``.
    """
    if not target.is_dir():
        return ()
    keep = {target.joinpath(*PurePosixPath(relative).parts) for relative, _ in config.files()}
    return tuple(sorted(path for path in _generated_under(target) if path not in keep))


def _refuse_inside_inventory(target: Path, inventory_root: Path | None) -> None:
    """Refuse to write a configuration tree into the inventory it came from.

    ``netplan``'s output is a ``.yaml`` file, and the loader reads every ``.yaml``
    under the inventory root. Writing one there does not merely clutter the tree:
    the *next* command fails to load it, with a diagnostic about a document that
    has no ``kind`` and no obvious relation to what the operator did. The
    generated tree is a build artefact and belongs outside the source, the same
    way a rendered diagram does.

    ``--force`` still allows it, because "outside the source" is a convention and
    somebody's layout will disagree — but they will have said so.
    """
    if inventory_root is None:
        return
    try:
        resolved, root = target.resolve(), inventory_root.resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved at all
        return
    if resolved != root and root not in resolved.parents:
        return
    # ``target`` as it was typed, not as it resolves: the message is read next to
    # the command that produced it, and an absolute path nobody wrote is noise.
    raise ConfigWriteError(
        f"refusing to write a configuration tree into the inventory: {target} is under the "
        f"inventory root, and the loader reads every YAML document there -- so the next "
        f"command would try to load the generated files as elements. Point --out outside "
        f"the inventory, or pass --force"
    )


def _refuse_foreign(config: ConfigSet, target: Path) -> None:
    """Refuse the write if it would clobber a file netgraph did not write."""
    clashes = [
        relative
        for relative, _ in config.files()
        if _is_foreign(target.joinpath(*PurePosixPath(relative).parts))
    ]
    if not clashes:
        return
    listed = ", ".join(clashes[:MAX_LISTED_CLASHES])
    if len(clashes) > MAX_LISTED_CLASHES:
        listed += f", and {len(clashes) - MAX_LISTED_CLASHES} more"
    raise ConfigWriteError(
        f"refusing to overwrite {len(clashes)} file(s) under {target} that netgraph did not "
        f"generate: {listed}; pass --force to replace them, or point --out at a directory "
        f"this command owns"
    )


def _is_foreign(path: Path) -> bool:
    """Does ``path`` exist and lack netgraph's banner?"""
    return path.exists() and _banner_dialect(path) is None


def _generated_under(root: Path) -> Iterator[Path]:
    """Every file below ``root`` carrying netgraph's banner."""
    for path in root.rglob("*"):
        if path.is_file() and _banner_dialect(path) is not None:
            yield path


def _banner_dialect(path: Path) -> str | None:
    """The dialect a generated file says it is, or ``None`` for anything else.

    Both comment markers are tried, because FRR's is ``!`` and every other
    dialect's is ``#``, and a reader here does not know which dialect wrote the
    file — that is what it is asking.

    An unreadable file, or one that is not UTF-8, answers ``None``: it is
    certainly not something netgraph wrote, which is exactly the answer the
    callers need.
    """
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            head = handle.read(_BANNER_BYTES)
    except (OSError, UnicodeDecodeError):
        return None
    for marker in ("#", "!"):
        dialect = parse_banner(head, marker).get(DIALECT_KEY)
        if dialect is not None:
            return dialect
    return None
