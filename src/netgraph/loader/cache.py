"""A content-addressed cache of parsed, model-validated documents.

Every command starts by re-reading the whole tree. That is the right default —
the files on disk are the only state netgraph trusts — but it is also the same
work every time, and the loops where it hurts are the ones where almost nothing
changed: ``netgraph watch`` re-renders on a keystroke-sized edit, ``validate``
runs from a pre-commit hook, the web preview reloads. On the 1056-device
benchmark tree the parse and model-validation of an untouched file is 100 % of a
440 ms load and 0 % of the new information.

So a file that has been parsed once is remembered by *what it contains* rather
than by when it was touched:

    key = sha256(identity, relative path, file bytes)

Nothing about the file's timestamp enters into it. A file rewritten with the
same bytes hits, a file whose mtime alone changed hits, a checkout that restores
an old version hits again, and two inventories that share a file share nothing —
the relative path is part of the key, because it is what decides the element's
namespace and the ``relative`` of every diagnostic the file produces.

**Identity** (:class:`Identity`) is the other half of the key and the half that
makes it safe. A cached entry is a *conclusion* about bytes — that they mean this
element, or that they are wrong in this way — and the code that drew the
conclusion is as much an input as the bytes were. So the key folds in the
netgraph version, the document ``apiVersion``, the YAML parser actually selected
(libyaml and the pure-Python parser word a syntax error differently), the
pydantic and PyYAML versions, and a stamp over the mtimes and sizes of
netgraph's own sources. That last one is what makes the cache safe to have
switched on while *developing* netgraph: editing a validator changes the stamp,
which changes every key, and yesterday's conclusions are simply not asked for.

**What is stored** is the element as pydantic serialises it, plus the
diagnostics the file produced. Never a pickle: the cache directory is an
ordinary directory, and a format that could instantiate arbitrary objects from
it would turn "somebody can write to your cache" into "somebody can run code as
you". Reconstruction goes back through the very same validators the document
went through, so an entry that has been tampered with cannot smuggle a model
past them — the worst it can do is be rejected, or be a *different valid*
inventory, which is exactly what writing to the inventory itself would achieve.

**What is not stored.** A file that declares a ``kind: template``, or a device
that inherits one with ``spec.from``, is never cached: its meaning depends on
another file's bytes, and a cache keyed on one file cannot see that. Those files
are re-parsed on every load. Neither is anything cached when provenance is kept
(``netgraph validate --format json``), because provenance *is* the YAML node
tree.

Failure is never fatal. A truncated entry, a corrupt one, an unwritable
directory, a full disk: every one of them degrades to "parse the file", which is
what the loader would have done anyway. :attr:`DocumentCache.stats` records what
happened so ``netgraph cache info`` and ``-v`` can say so.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import zlib
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import pydantic
import yaml
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

import netgraph
from netgraph.fsio import write_bytes_atomically
from netgraph.loader.documents import StrictSafeLoader
from netgraph.loader.inventory import LoadError
from netgraph.models import API_VERSION, Element

__all__ = [
    "CACHE_DIR_ENV_VAR",
    "DEFAULT_MAX_BYTES",
    "DISABLE_ENV_VAR",
    "ENTRY_SUFFIX",
    "FORMAT_VERSION",
    "CacheInfo",
    "CacheStats",
    "CachedFile",
    "CachedSlot",
    "DocumentCache",
    "Identity",
    "clear_cache",
    "inspect_cache",
    "inventory_cache_dir",
    "open_cache",
    "resolve_cache_root",
]

#: Bumped when the on-disk shape of an entry changes in a way an older or newer
#: netgraph would misread. It is part of :class:`Identity`, so a bump invalidates
#: every entry rather than risking one being misparsed.
FORMAT_VERSION: Final = 1

#: Overrides where the cache lives, for a CI runner that wants it on a scratch
#: volume or inside a workspace it already caches. Beats ``[cache] dir``.
CACHE_DIR_ENV_VAR: Final = "NETGRAPH_CACHE_DIR"

#: Set to any of :data:`_TRUTHY` to switch the cache off for a whole environment,
#: which is what a container image or a CI job wants: one variable rather than
#: ``--no-cache`` threaded through every invocation.
DISABLE_ENV_VAR: Final = "NETGRAPH_NO_CACHE"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: ``sys.platform``, laundered through a plain ``str``. See
#: :func:`resolve_cache_root`, which is the only thing that asks: a literal
#: ``sys.platform == "..."`` is a compile-time constant to a type checker, and the
#: branch it folds away is the one the other two runners need checked.
PLATFORM: str = sys.platform

#: Extension of one cache entry. Named so that a human who finds the directory
#: can tell what they are looking at, and so a stray file of any other name is
#: left strictly alone.
ENTRY_SUFFIX: Final = ".ngc"

#: How much of one inventory's cache is kept before the least recently used
#: entries are dropped. The 1056-device benchmark tree occupies 0.4 MB, so this
#: holds every generation of a very large inventory and is really a guard
#: against a cache nobody ever looks at growing without limit.
DEFAULT_MAX_BYTES: Final = 64 * 1024 * 1024

#: How much of one *process's* cache is held in memory. This is the tier that
#: makes ``watch`` incremental: a file whose bytes have not changed since the
#: previous cycle costs a hash and a dictionary lookup, with no file to open, no
#: decompression and no re-validation. Counted in source bytes, which is the one
#: size known before an entry is built.
DEFAULT_MEMORY_BYTES: Final = 32 * 1024 * 1024

#: Fraction of the cap a sweep evicts down to, so that a cache sitting exactly
#: at the limit does not sweep on every single run.
_SWEEP_TARGET: Final = 0.9

#: An entry read but not written keeps its recency by having its mtime bumped —
#: but only when the recorded one is already this old. A load that hits a
#: thousand entries would otherwise pay a thousand pointless ``utime`` calls
#: every time it runs, and LRU only has to be accurate to the sweep interval.
_TOUCH_AFTER_SECONDS: Final = 3600.0

#: Serialises a whole file's worth of elements in one call. Per-file rather than
#: per-element because it is a single crossing into pydantic-core for a file that
#: usually holds tens of documents: on the benchmark tree that is 138 calls
#: instead of 2106, and measurably faster for it.
_ELEMENTS: Final[TypeAdapter[list[Element]]] = TypeAdapter(list[Element])

#: First line of an entry: the magic, then the key it must answer for, then the
#: length of each of the two compressed sections. The key is repeated inside the
#: file so that a hash collision in the *name* — or a file moved between
#: directories by a well-meaning backup tool — is a miss rather than a wrong
#: answer.
_MAGIC: Final = b"netgraph-cache/1"


# --------------------------------------------------------------------------- #
# What a cached entry is a conclusion about
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Identity:
    """Everything besides the file's own bytes that decides what it means.

    Two netgraphs that agree on all of this will draw the same conclusion from
    the same bytes; two that differ anywhere might not, and must therefore not
    read each other's entries. The fingerprint of this record names the
    *generation* directory entries are filed under, so a change here neither
    deletes nor misreads what the previous generation wrote — it stops asking
    for it, and the sweep reclaims it as the oldest thing in the cache.
    """

    #: :data:`FORMAT_VERSION`, the shape of the file on disk.
    format_version: int
    #: The netgraph distribution's own version.
    netgraph: str
    #: ``apiVersion`` the models accept, from ``docs/schema.md`` §3.
    api_version: str
    #: Class name of the selected YAML loader: the two parsers word a syntax
    #: error differently, and a cached diagnostic is a cached wording.
    parser: str
    #: ``(distribution, version)`` for the libraries whose behaviour is baked
    #: into an entry, sorted.
    dependencies: tuple[tuple[str, str], ...]
    #: A digest over netgraph's own source files; see :func:`source_stamp`.
    source_stamp: str

    @classmethod
    def current(cls) -> Identity:
        """The identity of this interpreter, this netgraph, this checkout."""
        return cls(
            format_version=FORMAT_VERSION,
            netgraph=netgraph.__version__,
            api_version=API_VERSION,
            parser=StrictSafeLoader.__name__,
            dependencies=(
                ("pydantic", pydantic.VERSION),
                ("python", f"{sys.version_info.major}.{sys.version_info.minor}"),
                ("pyyaml", yaml.__version__),
            ),
            source_stamp=source_stamp(),
        )

    @property
    def fingerprint(self) -> str:
        """Short digest naming the generation directory. Stable across runs."""
        digest = hashlib.sha256()
        for part in (
            str(self.format_version),
            self.netgraph,
            self.api_version,
            self.parser,
            *(f"{name}={version}" for name, version in self.dependencies),
            self.source_stamp,
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:16]

    @property
    def generation(self) -> str:
        """Directory name: the version, readably, then the fingerprint."""
        return f"{_slug(self.netgraph)}-{self.fingerprint}"

    def describe(self) -> tuple[tuple[str, str], ...]:
        """Label/value pairs for ``netgraph cache info``."""
        return (
            ("format", str(self.format_version)),
            ("netgraph", self.netgraph),
            ("apiVersion", self.api_version),
            ("parser", self.parser),
            *((name, version) for name, version in self.dependencies),
            ("sources", self.source_stamp),
        )


@lru_cache(maxsize=1)
def source_stamp() -> str:
    """A digest over the size and mtime of every netgraph source file.

    The version number is the honest answer to "which netgraph produced this
    entry?" only for an installed release. In a source checkout — which is where
    the models and their validators are being *changed* — it stays
    ``0.0.0.dev0`` across every edit, and a cache keyed on it alone would serve
    conclusions drawn by code that no longer exists. Stat-ing the package is
    cheap (a few hundred microseconds for ~90 files, once per process) and
    catches every edit, including one that only touches a validator's body and
    would be invisible to a schema hash.

    Two identical installs made at different times stamp differently, and a
    ``touch`` invalidates without changing behaviour. Both cost a cold cache and
    nothing else, which is the right way round for a wrong answer.

    Degrades to a constant when the package cannot be walked at all — a zipimport
    or a frozen build — in which case the version and dependency versions are
    what is left, and that is the same guarantee every other tool offers.
    """
    root = Path(netgraph.__file__).resolve().parent
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*.py")):
            info = path.stat()
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(f"\0{info.st_size}\0{info.st_mtime_ns}\0".encode())
    except OSError:  # pragma: no cover - an unreadable installation
        return "unstattable"
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# What is cached
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CachedSlot:
    """One document of a file: the element it became, or why it did not.

    Rejected documents are kept, and kept *in place*, because the order the
    loader records its diagnostics in is part of what a cache hit has to
    reproduce exactly — a duplicate-name error is emitted while indexing, in
    slot order, interleaved with these.
    """

    #: The validated element, or ``None`` when the document was rejected.
    element: Element | None = None
    #: 0-based document index within the file.
    index: int = 0
    #: 1-based line the document starts on, when the parser reported one.
    line: int | None = None
    #: Why the document was rejected. Non-empty exactly when
    #: :attr:`element` is ``None``.
    errors: tuple[LoadError, ...] = ()


@dataclass(frozen=True, slots=True)
class CachedFile:
    """Everything one file contributed to an inventory."""

    #: One entry per non-empty document, in file order.
    slots: tuple[CachedSlot, ...] = ()
    #: Problems with the file as a whole — a syntax error, an unreadable byte
    #: sequence — recorded before any of its documents are indexed.
    errors: tuple[LoadError, ...] = ()

    @property
    def elements(self) -> tuple[Element, ...]:
        return tuple(slot.element for slot in self.slots if slot.element is not None)


@dataclass(slots=True)
class CacheStats:
    """What one process's use of the cache came to."""

    hits: int = 0
    #: Hits served without touching the file system at all.
    memory_hits: int = 0
    misses: int = 0
    writes: int = 0
    #: Entries found but unusable — truncated, corrupt, or for another key.
    rejected: int = 0
    #: Entries dropped by the sweep to stay under the cap.
    evicted: int = 0
    #: Files the loader refused to cache: templates, and what inherits them.
    uncacheable: int = 0
    #: The first thing that went wrong, when something did.
    problem: str | None = None

    def summary(self) -> str:
        """One line for ``-v``."""
        return (
            f"cache: {self.hits} hit(s) ({self.memory_hits} in memory), {self.misses} miss(es), "
            f"{self.writes} written, {self.uncacheable} not cacheable"
        )


# --------------------------------------------------------------------------- #
# Where the cache lives
# --------------------------------------------------------------------------- #


def resolve_cache_root(*, configured: Path | None = None, environ: Any = None) -> tuple[Path, str]:
    """The base directory every inventory's cache sits under, and why.

    The ladder, strongest first:

    1. :data:`CACHE_DIR_ENV_VAR`, for a CI runner or a container that has
       somewhere better to put it than the home directory it may not have.
    2. ``[cache] dir`` in the inventory's ``netgraph.toml``, already resolved
       against that file by :mod:`netgraph.config`.
    3. ``XDG_CACHE_HOME``, honoured on every platform because it is the variable
       a Docker image sets and the one this project's own image sets.
    4. The platform's own answer: ``%LOCALAPPDATA%`` on Windows,
       ``~/Library/Caches`` on macOS, ``~/.cache`` elsewhere.

    The last rung is compared against :data:`PLATFORM` rather than against
    ``sys.platform`` directly, and that is not a stylistic choice. mypy treats a
    literal ``sys.platform == "..."`` in a condition as a compile-time constant
    for the platform it is *running on*, so under ``warn_unreachable`` the two
    branches this function does not take became an error on the two runners that
    do not take them — and the ``~/.cache`` line was unreachable code on macOS.
    Going through a plain ``str`` makes all three branches ordinary code, checked
    on every platform instead of skipped on two.

    Returns:
        The directory and a short phrase naming the rung it came from, which is
        what ``netgraph cache info`` prints — "the cache is not where I think it
        is" being the only interesting question about a cache directory.
    """
    env = os.environ if environ is None else environ
    override = (env.get(CACHE_DIR_ENV_VAR) or "").strip()
    if override:
        return Path(override).expanduser(), f"{CACHE_DIR_ENV_VAR}"
    if configured is not None:
        return configured, "netgraph.toml [cache] dir"
    xdg = (env.get("XDG_CACHE_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "netgraph", "XDG_CACHE_HOME"
    if PLATFORM == "win32":  # pragma: no cover - platform fork
        local = (env.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "netgraph" / "Cache", "LOCALAPPDATA"
    if PLATFORM == "darwin":  # pragma: no cover - platform fork
        return Path.home() / "Library" / "Caches" / "netgraph", "~/Library/Caches"
    return Path.home() / ".cache" / "netgraph", "~/.cache"


def inventory_cache_dir(root: Path, base: Path) -> Path:
    """Where one inventory's entries go, under the base directory.

    The name carries the inventory's own directory name so a human can tell the
    directories apart, and a digest of its resolved absolute path so two trees
    with the same basename cannot collide. Nothing is keyed on the *content* of
    the tree, so the directory survives every edit.
    """
    resolved = _resolved(root)
    digest = hashlib.sha256(str(resolved).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return base / "inventories" / f"{_slug(resolved.name) or 'inventory'}-{digest}"


def _resolved(root: Path) -> Path:
    try:
        return root.resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved at all
        return root.absolute()


def _slug(text: str) -> str:
    """``text`` reduced to what is safe in a directory name on every platform."""
    kept = "".join(char if char.isalnum() or char in "-._" else "-" for char in text)
    return kept.strip("-.")[:48]


def disabled_by_environment(environ: Any = None) -> bool:
    """Is :data:`DISABLE_ENV_VAR` set to something that means yes?"""
    env = os.environ if environ is None else environ
    return (env.get(DISABLE_ENV_VAR) or "").strip().lower() in _TRUTHY


def open_cache(
    root: Path,
    *,
    directory: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    environ: Any = None,
) -> DocumentCache:
    """Open the cache for the inventory at ``root``.

    Nothing is created and nothing is read here: the directory is made on the
    first write, so a run that only hits — or one whose cache is unwritable —
    leaves no trace.
    """
    base, origin = resolve_cache_root(configured=directory, environ=environ)
    return DocumentCache(
        inventory_cache_dir(root, base),
        max_bytes=max_bytes,
        origin=origin,
    )


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


@dataclass(eq=False)
class DocumentCache:
    """Parsed files, keyed by their content, over two tiers.

    The memory tier is what makes a *reload* incremental and the disk tier is
    what makes the *next process* fast; both are keyed identically, so an entry
    written by one run is found by the next.

    Elements handed out by the memory tier are the same objects the previous
    load used. Nothing in netgraph mutates a loaded element — only the importer
    mutates models, and those are its own draft types — and a caller that wants
    to must copy first.
    """

    #: This inventory's directory. Created on the first write.
    directory: Path
    #: Cap on the bytes kept on disk for this inventory, across generations.
    max_bytes: int = DEFAULT_MAX_BYTES
    #: Cap on the source bytes whose entries are held in memory.
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    #: Which rung of :func:`resolve_cache_root` chose the base directory.
    origin: str = ""
    identity: Identity = field(default_factory=Identity.current)
    stats: CacheStats = field(default_factory=CacheStats)
    _memory: OrderedDict[str, tuple[int, CachedFile]] = field(
        default_factory=OrderedDict, repr=False
    )
    _memory_used: int = field(default=0, repr=False)
    _wrote: bool = field(default=False, repr=False)
    #: Set when the directory itself turned out to be unusable, so the run stops
    #: trying once per file and starts behaving like ``--no-cache``.
    _broken: bool = field(default=False, repr=False)

    @property
    def generation_dir(self) -> Path:
        """Where entries for *this* identity live."""
        return self.directory / self.identity.generation

    # -- keys ------------------------------------------------------------

    def key_for(self, relative: str, content: bytes) -> str:
        """The key for ``content`` read from ``relative`` within the inventory.

        The identity is folded in as its fingerprint rather than the generation
        directory doing the work alone, so an entry that is somehow read from
        the wrong generation still fails its own self-check.
        """
        digest = hashlib.sha256()
        digest.update(self.identity.fingerprint.encode("ascii"))
        digest.update(b"\0")
        digest.update(relative.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        digest.update(content)
        return digest.hexdigest()

    def path_for(self, key: str) -> Path:
        """The file one key is stored in, sharded so no directory grows huge."""
        return self.generation_dir / key[:2] / f"{key[2:]}{ENTRY_SUFFIX}"

    # -- reading ---------------------------------------------------------

    def get(self, key: str, *, path: Path, relative: str) -> CachedFile | None:
        """The cached result for ``key``, or ``None`` to parse the file.

        ``path`` and ``relative`` are not part of what is stored — they are the
        same for every entry of one file, and storing them would mean a cache
        that breaks when the tree is moved. They are put back onto the
        diagnostics here.
        """
        remembered = self._memory.get(_in_memory(key, path))
        if remembered is not None:
            self._memory.move_to_end(_in_memory(key, path))
            self.stats.hits += 1
            self.stats.memory_hits += 1
            return remembered[1]

        blob = self._read(key)
        if blob is None:
            self.stats.misses += 1
            return None
        entry = _decode(blob, key=key, path=path, relative=relative)
        if entry is None:
            # A truncated, corrupt or foreign file. Drop it: it will never
            # decode, and leaving it behind means paying for it on every run.
            self.stats.rejected += 1
            self.stats.misses += 1
            self._discard(self.path_for(key))
            return None
        self.stats.hits += 1
        self._remember(key, len(blob), entry, path=path)
        return entry

    def _read(self, key: str) -> bytes | None:
        """The bytes of one entry, refreshing its recency. ``None`` if absent."""
        if self._broken:
            return None
        path = self.path_for(key)
        try:
            with path.open("rb") as handle:
                blob = handle.read()
                # ``fstat`` on the handle already open, rather than a second
                # ``stat`` on the path: this runs once per file of the inventory.
                modified = os.fstat(handle.fileno()).st_mtime
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._note(f"cannot read cache entry: {exc.strerror or exc}")
            return None
        self._maybe_touch(path, modified)
        return blob

    def _maybe_touch(self, path: Path, modified: float) -> None:
        """Bump an entry's mtime when it is stale enough for LRU to care.

        By path rather than by file descriptor, because ``os.utime`` accepts a
        descriptor only where ``os.supports_fd`` says so, and on Windows it does
        not — where it would raise ``NotImplementedError``, which is not an
        ``OSError`` and would not be absorbed here.
        """
        if time.time() - modified <= _TOUCH_AFTER_SECONDS:
            return
        try:
            os.utime(path)
        except OSError:  # pragma: no cover - a read-only cache directory
            return

    # -- writing ---------------------------------------------------------

    def put(self, key: str, entry: CachedFile, *, path: Path | None = None) -> None:
        """Remember what a file produced. Never raises."""
        try:
            blob = _encode(entry, key=key)
        except _Uncacheable as exc:
            self.stats.uncacheable += 1
            self._note(str(exc))
            return
        self._remember(key, len(blob), entry, path=path)
        if self._broken:
            return
        try:
            # Atomic but not durable: a temporary file plus a rename, so a reader
            # sees a whole entry or none, and no ``fsync``, because a cache that
            # survives a power cut is worth less than the 3 ms per file it costs.
            # A crash can therefore leave an entry that decodes to nothing, which
            # is the case ``_decode`` exists to absorb.
            write_bytes_atomically(self.path_for(key), blob, sync=False)
        except OSError as exc:
            # An unwritable cache is a slow netgraph, not a broken one. Stop
            # trying for the rest of the run rather than failing once per file.
            self._broken = True
            self._note(f"cannot write to {self.directory}: {exc.strerror or exc}")
            return
        self.stats.writes += 1
        self._wrote = True

    def not_cacheable(self) -> None:
        """Record that a file was deliberately not offered to the cache."""
        self.stats.uncacheable += 1

    def flush(self) -> None:
        """Finish a load: sweep the cache back under its cap if it grew.

        Only after a run that wrote something. A cache that is only ever read
        cannot have outgrown the cap, and a sweep per read would turn every
        ``netgraph list`` into a directory walk.
        """
        if self._wrote and not self._broken:
            self.sweep()
        self._wrote = False

    def sweep(self) -> None:
        """Drop least recently used entries until the cap is respected.

        Recency is the entry's mtime, which :meth:`_read` refreshes and every
        write sets. That makes a stale *generation* — everything written by a
        netgraph that has since been upgraded — the oldest thing in the cache
        and therefore the first to go, which is how a version bump reclaims its
        own space without a migration step.
        """
        entries = list(_walk(self.directory))
        total = sum(size for _, size, _ in entries)
        if total <= self.max_bytes:
            return
        entries.sort(key=lambda item: item[2])
        target = int(self.max_bytes * _SWEEP_TARGET)
        for path, size, _ in entries:
            if total <= target:
                break
            if self._discard(path):
                self.stats.evicted += 1
                total -= size
        _prune_empty(self.directory)

    def _discard(self, path: Path) -> bool:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - another process got there first
            return False
        return True

    # -- the memory tier -------------------------------------------------

    def _remember(self, key: str, size: int, entry: CachedFile, *, path: Path | None) -> None:
        """Hold a decoded entry for the next load in this process."""
        memory_key = _in_memory(key, path)
        previous = self._memory.pop(memory_key, None)
        if previous is not None:
            self._memory_used -= previous[0]
        self._memory[memory_key] = (size, entry)
        self._memory_used += size
        while self._memory_used > self.memory_bytes and len(self._memory) > 1:
            _, (dropped, _) = self._memory.popitem(last=False)
            self._memory_used -= dropped

    def _note(self, problem: str) -> None:
        if self.stats.problem is None:
            self.stats.problem = problem


def _in_memory(key: str, path: Path | None) -> str:
    """Key of the memory tier: the disk key, plus the file's absolute path.

    The disk key carries the path *within* the inventory, which is all an entry
    on disk needs — the directory it sits in already says which inventory it
    belongs to, and the decoder is handed the absolute path by the caller. The
    memory tier hands back an already-decoded entry instead, diagnostics and
    all, so one store used for two trees that share a file must not answer for
    the wrong one.
    """
    return key if path is None else f"{key}\0{path}"


# --------------------------------------------------------------------------- #
# The on-disk form of one entry
# --------------------------------------------------------------------------- #


class _Uncacheable(Exception):
    """An entry that must not be written, with the reason. Never escapes."""


def _encode(entry: CachedFile, *, key: str) -> bytes:
    """Serialise one file's worth of results.

    Two compressed sections, because the elements are handed to pydantic-core as
    one contiguous JSON array — validating a whole file in a single call is the
    fastest way back in — while the small amount of bookkeeping around them is
    ordinary JSON this module reads itself.
    """
    meta = json.dumps(
        {
            "slots": [_encode_slot(slot) for slot in entry.slots],
            "errors": [_encode_error(error) for error in entry.errors],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    body = _ELEMENTS.dump_json(list(entry.elements))
    head = zlib.compress(meta, 1)
    tail = zlib.compress(body, 1)
    header = b"%s %s %d %d\n" % (_MAGIC, key.encode("ascii"), len(head), len(tail))
    return header + head + tail


def _encode_slot(slot: CachedSlot) -> dict[str, Any]:
    if slot.element is None:
        return {"errors": [_encode_error(error) for error in slot.errors]}
    return {"index": slot.index, "line": slot.line}


def _encode_error(error: LoadError) -> dict[str, Any]:
    """One diagnostic, without the two fields the reader already knows.

    ``path`` and ``relative`` are dropped and checked rather than stored: an
    entry is keyed by one file's bytes at one relative path, so a diagnostic
    that names a *different* file cannot be replayed from it. The only way to
    get one is a template, and the loader refuses to offer those — this is the
    assertion that keeps that promise honest.
    """
    if error.relative is None:
        raise _Uncacheable("a diagnostic without a file cannot be cached")
    return {
        "message": error.message,
        "relative": error.relative,
        "line": error.line,
        "column": error.column,
        "index": error.index,
        "path": list(error.field_path),
        "rule": error.rule,
    }


def _decode(blob: bytes, *, key: str, path: Path, relative: str) -> CachedFile | None:
    """Rebuild one entry, or return ``None`` if it cannot be trusted.

    Every step is a way for this to fail and none of them is exceptional: the
    process that wrote the file may have been killed halfway, the disk may have
    lied, an older netgraph may have written a shape this one does not know. The
    answer to all of it is the same and is never an exception — parse the file.
    """
    try:
        header, rest = blob.split(b"\n", 1)
        magic, stored_key, head_len, tail_len = header.split(b" ")
        if magic != _MAGIC or stored_key.decode("ascii") != key:
            return None
        head, tail = int(head_len), int(tail_len)
        if head + tail != len(rest):
            return None
        # zlib carries an Adler-32 of what it compressed, so a body that was
        # truncated or altered fails here rather than becoming a plausible
        # element.
        meta = json.loads(zlib.decompress(rest[:head]))
        elements = _ELEMENTS.validate_json(zlib.decompress(rest[head:]))
    except (
        ValueError,  # a malformed header, bad JSON, a bad length
        UnicodeDecodeError,
        zlib.error,  # a truncated or corrupt body
        PydanticValidationError,  # an element this netgraph does not accept
    ):
        return None
    try:
        return _rebuild(meta, elements, path=path, relative=relative)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _rebuild(
    meta: Any, elements: Sequence[Element], *, path: Path, relative: str
) -> CachedFile | None:
    """Put the bookkeeping back around the validated elements."""
    if not isinstance(meta, dict):
        return None
    remaining = iter(elements)
    slots: list[CachedSlot] = []
    for raw in meta["slots"]:
        if "errors" in raw:
            slots.append(
                CachedSlot(
                    errors=tuple(
                        _decode_error(error, path=path, relative=relative)
                        for error in raw["errors"]
                    )
                )
            )
            continue
        element = next(remaining, None)
        if element is None:  # more slots than elements: not a file we wrote
            return None
        slots.append(
            CachedSlot(element=element, index=int(raw["index"]), line=_optional(raw["line"]))
        )
    if next(remaining, None) is not None:  # more elements than slots
        return None
    return CachedFile(
        slots=tuple(slots),
        errors=tuple(
            _decode_error(error, path=path, relative=relative) for error in meta["errors"]
        ),
    )


def _decode_error(raw: Any, *, path: Path, relative: str) -> LoadError:
    stored = str(raw["relative"])
    return LoadError(
        message=str(raw["message"]),
        # The stored value is compared, not trusted: it is always this file, and
        # anything else means the entry was not written for this path.
        path=path if stored == relative else None,
        relative=stored,
        line=_optional(raw["line"]),
        column=_optional(raw["column"]),
        index=_optional(raw["index"]),
        field_path=tuple(part if isinstance(part, int) else str(part) for part in raw["path"]),
        rule=None if raw["rule"] is None else str(raw["rule"]),
    )


def _optional(value: Any) -> int | None:
    return None if value is None else int(value)


# --------------------------------------------------------------------------- #
# Inspecting and clearing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CacheInfo:
    """What ``netgraph cache info`` reports."""

    #: This inventory's directory, whether or not it exists.
    directory: Path
    #: Which rung of :func:`resolve_cache_root` chose the base directory.
    origin: str
    #: Is the cache switched on for this inventory at all?
    enabled: bool
    #: Why not, when it is not.
    reason: str = ""
    identity: Identity = field(default_factory=Identity.current)
    max_bytes: int = DEFAULT_MAX_BYTES
    #: Entries and bytes filed under the *current* identity.
    entries: int = 0
    used_bytes: int = 0
    #: Entries and bytes left behind by another netgraph or another format.
    stale_entries: int = 0
    stale_bytes: int = 0

    @property
    def exists(self) -> bool:
        return self.entries > 0 or self.stale_entries > 0


def inspect_cache(
    directory: Path,
    *,
    origin: str = "",
    enabled: bool = True,
    reason: str = "",
    identity: Identity | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> CacheInfo:
    """Count what is on disk for one inventory, current and stale."""
    resolved = identity or Identity.current()
    current = resolved.generation
    entries = used = stale_entries = stale = 0
    for path, size, _ in _walk(directory):
        if current in path.parts:
            entries += 1
            used += size
        else:
            stale_entries += 1
            stale += size
    return CacheInfo(
        directory=directory,
        origin=origin,
        enabled=enabled,
        reason=reason,
        identity=resolved,
        max_bytes=max_bytes,
        entries=entries,
        used_bytes=used,
        stale_entries=stale_entries,
        stale_bytes=stale,
    )


def clear_cache(directory: Path) -> tuple[int, int]:
    """Delete every entry under ``directory``. Returns ``(entries, bytes)``.

    Only files this module wrote are removed, and the directories that held
    them. A cache directory somebody has pointed at their home folder by mistake
    therefore loses nothing but ``*.ngc`` — which is the whole cache, and
    nothing else.
    """
    removed = freed = 0
    for path, size, _ in _walk(directory):
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - a locked or read-only entry
            continue
        removed += 1
        freed += size
    _prune_empty(directory)
    return removed, freed


def _walk(directory: Path) -> Iterator[tuple[Path, int, float]]:
    """Every entry under ``directory`` as ``(path, size, mtime)``.

    Uses ``scandir`` and the stat the directory read already provides, because
    this runs over every entry of the cache and a separate ``stat`` per file
    would double the cost of a sweep.
    """
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.name.endswith(ENTRY_SUFFIX):
                            info = entry.stat()
                            yield Path(entry.path), info.st_size, info.st_mtime
                    except OSError:  # pragma: no cover - vanished mid-walk
                        continue
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:  # pragma: no cover - an unreadable cache directory
            continue


def _prune_empty(directory: Path) -> None:
    """Remove the shard and generation directories a sweep emptied."""
    for path in sorted(_directories(directory), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue


def _directories(directory: Path) -> Iterable[Path]:
    found: list[Path] = []
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        found.append(Path(entry.path))
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return found
