"""The scratch inventories the guided tour edits, so the real one is untouched.

The tour in ``assets/tour.js`` does not mime its steps. It creates a device,
connects it to a neighbour, moves the document into another file, opens the
changes drawer on the resulting YAML, and undoes the lot — every one of those a
real batch through :mod:`netgraph.edit`, because a tour that only *described*
the write would prove nothing about the mapping between the picture and the
files, which is the single thing it exists to demonstrate.

So it is given files of its own. :meth:`Tours.open` copies the open inventory
into a temporary directory and puts a second, always-writable
:class:`~netgraph.web.session.EditingSession` over the copy; every route in
:mod:`netgraph.web.server` selects it with ``?scratch=<token>`` and answers from
it instead. Nothing about the real session changes — not its revision, not its
undo stack, not one byte on disk — and a read-only session can therefore take
the tour too, which is exactly the session somebody exploring is most likely to
have open.

Three properties this module is responsible for:

**Only inventory files are copied.** :func:`copy_inventory` walks the tree the
way :mod:`netgraph.loader.tree` reads it — YAML suffixes, no component starting
with ``.`` or ``_`` — plus ``netgraph.toml``, because the copy should render
with the same defaults the original does. A repository's history, its virtual
environments and its rendered SVGs are not part of an inventory and are not
copied.

**The copy is bounded.** :data:`MAX_FILES` and :data:`MAX_BYTES` stop a
mistyped ``netgraph web /`` from filling a disk, and the refusal names what it
counted so the number is actionable rather than mysterious.

**Nothing outlives the tab that asked for it.** A scratch is closed when the
tour finishes, when the page unloads (``navigator.sendBeacon``), when the server
stops, and, for the tab that crashed instead of doing any of those, when it has
gone :data:`TTL_SECONDS` without a request. :data:`MAX_SCRATCHES` caps how many
can exist at once.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Final

from netgraph.loader.tree import YAML_SUFFIXES
from netgraph.render import IconTheme
from netgraph.web.session import EditingSession, SessionError

__all__ = [
    "CONFIG_NAME",
    "MAX_BYTES",
    "MAX_FILES",
    "MAX_SCRATCHES",
    "TOUR_END_PATH",
    "TOUR_PATH",
    "TTL_SECONDS",
    "Scratch",
    "Tours",
    "copy_inventory",
]

#: Start a tour: ``POST``, answered with the token every later request quotes.
TOUR_PATH: Final = "/api/tour"
#: End one and delete its files. ``POST``, and deliberately not a ``DELETE``:
#: ``navigator.sendBeacon`` — the only request a closing tab is allowed to make
#: — can only send a POST.
TOUR_END_PATH: Final = "/api/tour/end"

#: Copied beside the YAML so the scratch renders with the inventory's own
#: defaults; a tour whose diagrams look nothing like yours teaches the wrong
#: page. Read from the root only, which is the only place it is read from.
CONFIG_NAME: Final = "netgraph.toml"

#: Most files a scratch copy may hold. Well above the thousand-device tree
#: ``tools/bench_editor.py`` measures, and far below anything that would take
#: long enough to notice.
MAX_FILES: Final = 20_000
#: Most bytes of YAML a scratch copy may hold.
MAX_BYTES: Final = 64 * 1024 * 1024
#: Most tours that may be running at once. One per tab, and nobody has ten.
MAX_SCRATCHES: Final = 8
#: How long an untouched scratch survives. The backstop for a tab that closed
#: without saying so; every other path deletes it immediately.
TTL_SECONDS: Final = 3600.0


class TooLarge(SessionError):
    """The inventory is bigger than a scratch copy of it should be."""


@dataclass
class Scratch:
    """One tour's copy of an inventory, and the session over it."""

    #: What a request quotes in ``?scratch=`` to be answered from this copy.
    #: Unguessable, and not a credential: the server answers to this machine
    #: only, and a token names a temporary directory rather than a permission.
    token: str
    #: The temporary directory. Deleted by :meth:`close`.
    root: Path
    #: Always writable, whatever the session it was copied from allows.
    session: EditingSession
    #: The inventory it is a copy of, for the tour's first card to name.
    origin: Path
    #: An element the tour can cable its new device to, or ``""`` when the
    #: inventory has no device and the tour must create the far end itself.
    peer: str
    #: How many files were copied. Shown, because "a copy of your inventory" is
    #: a claim and a number is evidence.
    files: int
    #: Monotonic clock of the last request that named this scratch.
    touched: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        """What ``POST /api/tour`` answers with."""
        return {
            "scratch": self.token,
            "root": str(self.root),
            "origin": str(self.origin),
            "peer": self.peer,
            "files": self.files,
        }

    def close(self) -> None:
        """Drop the session's streams and delete the copy."""
        self.session.close()
        # ``ignore_errors`` because this runs from a request thread, from a
        # beacon nobody is waiting for, and from server shutdown: a scratch that
        # cannot be removed — a file held open by a virus scanner on Windows is
        # the realistic case — is a temporary directory the operating system
        # will collect, not a reason to fail any of the three.
        shutil.rmtree(self.root, ignore_errors=True)


class Tours:
    """Every scratch copy this server is holding, by token.

    Shared by every request thread, so all of it is under one lock. Nothing here
    blocks for long: opening copies files, and everything else is a dict.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scratches: dict[str, Scratch] = {}

    def open(self, session: EditingSession, *, icons: IconTheme | None = None) -> Scratch:
        """Copy ``session``'s tree and put a writable session over the copy.

        Args:
            session: The inventory being toured. Read only — this never writes
                through it, bumps its revision or touches its history.
            icons: The theme the real session draws with, so the scratch draws
                the same way.

        Returns:
            The scratch, already registered under its token.

        Raises:
            TooLarge: The tree is beyond :data:`MAX_FILES` or :data:`MAX_BYTES`.
            SessionError: :data:`MAX_SCRATCHES` tours are already running.
        """
        self._expire()
        with self._lock:
            if len(self._scratches) >= MAX_SCRATCHES:
                raise SessionError(
                    f"{MAX_SCRATCHES} guided tours are already running on this server; "
                    "finish or close one before starting another"
                )
        root = Path(mkdtemp(prefix="netgraph-tour-"))
        try:
            files = copy_inventory(session.root, root)
            scratch = Scratch(
                token=uuid.uuid4().hex,
                root=root,
                session=EditingSession(root=root, writable=True, icons=icons),
                origin=Path(session.root),
                peer=_peer(session),
                files=files,
            )
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        with self._lock:
            self._scratches[scratch.token] = scratch
        return scratch

    def get(self, token: str | None) -> Scratch | None:
        """The scratch a request named, or ``None`` when it named none.

        A token that is not one is ``None`` rather than a refusal: the request
        is then answered from the real session, which is read-only unless the
        command line said otherwise, so a stale token from a reloaded tab
        degrades into an ordinary request instead of an error nobody can act on.
        """
        if not token:
            return None
        with self._lock:
            scratch = self._scratches.get(token)
            if scratch is not None:
                scratch.touched = time.monotonic()
        return scratch

    def close(self, token: str | None) -> bool:
        """End one tour. ``True`` when there was one to end."""
        with self._lock:
            scratch = self._scratches.pop(token or "", None)
        if scratch is None:
            return False
        scratch.close()
        return True

    def close_all(self) -> None:
        """End every tour. Called when the server stops."""
        with self._lock:
            scratches = list(self._scratches.values())
            self._scratches.clear()
        for scratch in scratches:
            scratch.close()

    def _expire(self) -> None:
        """Drop scratches nothing has asked about for :data:`TTL_SECONDS`."""
        cutoff = time.monotonic() - TTL_SECONDS
        with self._lock:
            stale = [
                token for token, scratch in self._scratches.items() if scratch.touched < cutoff
            ]
            dropped = [self._scratches.pop(token) for token in stale]
        for scratch in dropped:
            scratch.close()

    def __len__(self) -> int:
        with self._lock:
            return len(self._scratches)


def copy_inventory(source: Path, destination: Path) -> int:
    """Copy the inventory documents under ``source`` into ``destination``.

    What the loader would read, and nothing else: files with a YAML suffix, no
    path component starting with ``.`` or ``_``, plus ``netgraph.toml`` at the
    root. Symbolic links are followed as files and never as directories, so a
    link out of the tree copies the one file it names rather than the world.

    Returns:
        How many files were copied.

    Raises:
        TooLarge: More than :data:`MAX_FILES` files or :data:`MAX_BYTES` bytes.
    """
    source = Path(source)
    destination = Path(destination)
    copied = 0
    total = 0
    for path in sorted(_documents(source)):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        size = path.stat().st_size
        copied += 1
        total += size
        if copied > MAX_FILES:
            raise TooLarge(
                f"the guided tour copies the inventory, and this one has more than "
                f"{MAX_FILES} documents; it is too big to tour"
            )
        if total > MAX_BYTES:
            raise TooLarge(
                f"the guided tour copies the inventory, and this one is over "
                f"{MAX_BYTES // (1024 * 1024)} MiB of YAML; it is too big to tour"
            )
        shutil.copyfile(path, target)
    config = source / CONFIG_NAME
    if config.is_file():
        shutil.copyfile(config, destination / CONFIG_NAME)
    return copied


def _documents(root: Path) -> list[Path]:
    """Every YAML file the loader would read below ``root``."""
    found: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:  # pragma: no cover - unreadable directory
            continue
        for entry in entries:
            if entry.name.startswith((".", "_")):
                continue
            if entry.is_dir() and not entry.is_symlink():
                stack.append(entry)
            elif entry.is_file() and entry.name.lower().endswith(YAML_SUFFIXES):
                found.append(entry)
    return found


def _peer(session: EditingSession) -> str:
    """A device the tour's new switch can be cabled to, or ``""``.

    The first device in load order, which is stable across runs of the same
    inventory and is therefore the same device in the screenshot and in the
    test. Chosen here rather than in the browser because the page sees drawn
    shapes and a filtered layer, and this needs a device that certainly has a
    ``spec.interfaces`` list to add a port to.

    An inventory with no device at all — a folder holding only cables, or an
    empty one — answers ``""``, and the tour creates both ends itself.
    """
    try:
        inventory = session.inventory()
    except Exception:  # pragma: no cover - a tree that will not load has no peer
        return ""
    for address in inventory.devices:
        return address
    return ""
