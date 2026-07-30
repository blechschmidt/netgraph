"""Tests for the content-addressed parse cache (:mod:`netgraph.loader.cache`).

The cache exists to make a repeated load cheap, and every test here is about the
one property that buys: **a hit must be indistinguishable from a parse.** Not
"close enough" — the same elements in the same order, the same diagnostics in the
same order, the same source locations, and the same rendered bytes, over every
inventory this repository commits.

The rest are the failure modes a cache adds and nothing else has: an entry for
bytes that have changed, an entry written by a netgraph that has since changed, an
entry a killed process left half written, an entry somebody edited, two processes
filling the cache at once, and a cache that would otherwise grow without limit.
Each of them has to end as a *parse*, because the alternative to a cache hit is
never an error.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
import zlib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from netgraph.cli import cli
from netgraph.config import CacheConfig, parse_cache, parse_config
from netgraph.errors import ConfigurationError
from netgraph.loader import Inventory, load_tree
from netgraph.loader.cache import (
    CACHE_DIR_ENV_VAR,
    DEFAULT_MAX_BYTES,
    DISABLE_ENV_VAR,
    ENTRY_SUFFIX,
    DocumentCache,
    Identity,
    clear_cache,
    disabled_by_environment,
    inspect_cache,
    inventory_cache_dir,
    open_cache,
    resolve_cache_root,
    source_stamp,
)
from netgraph.render import Layer, build_graph, render
from netgraph.watch import RenderRequest, run_cycle

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

#: Every committed example, which is what "identical over every example" means.
EXAMPLE_TREES = sorted(path for path in EXAMPLES.iterdir() if path.is_dir())

SWITCH = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: {name}
spec:
  interfaces:
    - name: Gi0/1
      type: ethernet
      mtu: {mtu}
"""


def write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def entries(directory: Path) -> list[Path]:
    """Every cache entry under ``directory``, in a stable order."""
    return sorted(directory.rglob(f"*{ENTRY_SUFFIX}"))


def fingerprint_of(inventory: Inventory) -> Any:
    """Everything about an inventory that a caller can observe.

    Element *equality* is pydantic's, so this compares the models field by field
    rather than by identity — which is the point: an element rebuilt from a cache
    entry is a different object and must be an equal one. Source locations are
    compared without their provenance, which is never cached and never present
    when it is in use.
    """
    return {
        "order": list(inventory.elements),
        "elements": dict(inventory.elements),
        "devices": list(inventory.devices),
        "cables": list(inventory.cables),
        "errors": [
            (
                error.message,
                error.relative,
                error.line,
                error.column,
                error.index,
                error.field_path,
                error.rule,
                str(error.path),
            )
            for error in inventory.errors
        ],
        "sources": {
            name: (str(source.path), source.relative, source.index, source.line)
            for name, source in inventory.sources.items()
        },
    }


@pytest.fixture
def store(tmp_path: Path) -> DocumentCache:
    """A cache in a directory of its own, with the real identity."""
    return DocumentCache(tmp_path / "cache")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A two-file inventory: a switch, and a cable joining it to another."""
    root = tmp_path / "inventory"
    write(root, "a/sw1.yaml", SWITCH.format(name="sw1", mtu=1500))
    write(root, "b/sw2.yaml", SWITCH.format(name="sw2", mtu=1500))
    write(
        root,
        "cables.yaml",
        """\
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: c1
spec:
  endpoints:
    - sw1:Gi0/1
    - sw2:Gi0/1
  medium: copper
""",
    )
    return root


# --------------------------------------------------------------------------- #
# A hit is a parse
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("example", EXAMPLE_TREES, ids=lambda path: path.name)
def test_a_cache_hit_is_indistinguishable_from_a_cold_load(example: Path, tmp_path: Path) -> None:
    """Over every committed example, warm and cold produce the same inventory.

    Three loads, because the cache has three paths and all of them are used in
    anger: the one that fills it, the one a *later process* takes off disk, and
    the one a second load in the same process takes out of memory.
    """
    cold = load_tree(example)

    filling = DocumentCache(tmp_path / "cache")
    warm_written = load_tree(example, cache=filling)
    from_disk = load_tree(example, cache=DocumentCache(tmp_path / "cache"))
    same_process = load_tree(example, cache=filling)

    expected = fingerprint_of(cold)
    for label, inventory in (
        ("while filling", warm_written),
        ("from disk", from_disk),
        ("from memory", same_process),
    ):
        assert fingerprint_of(inventory) == expected, f"{example.name} differs {label}"


@pytest.mark.parametrize("example", EXAMPLE_TREES, ids=lambda path: path.name)
def test_a_cache_hit_renders_the_same_bytes(example: Path, tmp_path: Path) -> None:
    """The property that matters downstream: identical output, not just models."""
    store = DocumentCache(tmp_path / "cache")
    load_tree(example, cache=store)
    warm = load_tree(example, cache=DocumentCache(tmp_path / "cache"))
    cold = load_tree(example)

    for layer in (Layer.L1, Layer.L2, Layer.L3):
        for fmt in ("dot", "mermaid", "json"):
            assert render(build_graph(warm, layer=layer), fmt) == render(
                build_graph(cold, layer=layer), fmt
            ), f"{example.name} {layer} {fmt}"


def test_a_rejected_document_is_reported_identically_from_the_cache(tmp_path: Path) -> None:
    """A broken file is a *conclusion* worth caching, and it must read the same."""
    root = tmp_path / "inventory"
    write(root, "sw.yaml", SWITCH.format(name="sw1", mtu="not-a-number"))
    write(root, "bad.yaml", "kind: switch\n  bad indentation\n")
    write(root, "unknown.yaml", "apiVersion: netgraph.dev/v1alpha1\nkind: toaster\nmetadata: {}\n")

    cold = load_tree(root)
    assert len(cold.errors) == 3

    store = DocumentCache(tmp_path / "cache")
    load_tree(root, cache=store)
    warm = load_tree(root, cache=DocumentCache(tmp_path / "cache"))

    assert fingerprint_of(warm) == fingerprint_of(cold)
    assert [str(error) for error in warm.errors] == [str(error) for error in cold.errors]


def test_a_duplicate_name_is_still_found_through_the_cache(tmp_path: Path) -> None:
    """``NG-N002`` is decided while indexing, so it cannot be cached per file."""
    root = tmp_path / "inventory"
    write(root, "one.yaml", SWITCH.format(name="sw1", mtu=1500))
    write(root, "two.yaml", SWITCH.format(name="sw1", mtu=9000))

    store = DocumentCache(tmp_path / "cache")
    load_tree(root, cache=store)
    warm = load_tree(root, cache=DocumentCache(tmp_path / "cache"))

    assert [error.rule for error in warm.errors] == ["NG-N002"]
    assert fingerprint_of(warm) == fingerprint_of(load_tree(root))


def test_the_same_bytes_in_two_namespaces_do_not_share_an_entry(tmp_path: Path) -> None:
    """The key carries the relative path, because the namespace comes from it."""
    root = tmp_path / "inventory"
    write(root, "a/sw.yaml", SWITCH.format(name="sw", mtu=1500))
    write(root, "b/sw.yaml", SWITCH.format(name="sw", mtu=1500))

    store = DocumentCache(tmp_path / "cache")
    inventory = load_tree(root, cache=store)

    assert sorted(inventory.elements) == ["a/sw", "b/sw"]
    assert len(entries(store.directory)) == 2
    assert fingerprint_of(load_tree(root, cache=store)) == fingerprint_of(load_tree(root))


def test_the_cache_is_bypassed_when_provenance_is_kept(tree: Path, store: DocumentCache) -> None:
    """Provenance is the node tree, which is exactly what an entry does not hold."""
    inventory = load_tree(tree, keep_provenance=True, cache=store)

    assert entries(store.directory) == []
    assert store.stats.hits == 0 and store.stats.misses == 0
    assert all(source.provenance is not None for source in inventory.sources.values())


# --------------------------------------------------------------------------- #
# Invalidation
# --------------------------------------------------------------------------- #


def test_editing_one_file_invalidates_only_its_own_entry(tree: Path, store: DocumentCache) -> None:
    load_tree(tree, cache=store)
    before = store.stats.writes
    assert before == 3

    (tree / "a/sw1.yaml").write_text(SWITCH.format(name="sw1", mtu=9000), encoding="utf-8")
    store.stats.hits = store.stats.misses = store.stats.writes = 0
    inventory = load_tree(tree, cache=store)

    assert (store.stats.hits, store.stats.misses, store.stats.writes) == (2, 1, 1)
    assert inventory.devices["a/sw1"].spec.interfaces[0].mtu == 9000
    # The old entry is still there -- content-addressed, so undoing the edit hits
    # again rather than re-parsing.
    assert len(entries(store.directory)) == 4


def test_restoring_the_previous_bytes_hits_the_previous_entry(
    tree: Path, store: DocumentCache
) -> None:
    original = (tree / "a/sw1.yaml").read_text(encoding="utf-8")
    load_tree(tree, cache=store)
    (tree / "a/sw1.yaml").write_text(SWITCH.format(name="sw1", mtu=9000), encoding="utf-8")
    load_tree(tree, cache=store)

    (tree / "a/sw1.yaml").write_text(original, encoding="utf-8")
    store.stats.misses = store.stats.writes = 0
    load_tree(tree, cache=store)

    assert (store.stats.misses, store.stats.writes) == (0, 0)


def test_touching_a_file_without_changing_it_still_hits(tree: Path, store: DocumentCache) -> None:
    """No timestamp is part of the key. ``make``-style caches get this wrong."""
    load_tree(tree, cache=store)
    for path in tree.rglob("*.yaml"):
        os.utime(path, (time.time() + 60, time.time() + 60))

    store.stats.hits = store.stats.misses = 0
    load_tree(tree, cache=store)

    assert (store.stats.hits, store.stats.misses) == (3, 0)


def test_a_version_bump_invalidates_every_entry(tree: Path, tmp_path: Path) -> None:
    """A new netgraph asks different questions, so it must not read old answers."""
    directory = tmp_path / "cache"
    before = DocumentCache(directory)
    load_tree(tree, cache=before)
    assert len(entries(directory)) == 3

    after = DocumentCache(directory, identity=replace(before.identity, netgraph="99.0.0"))
    inventory = load_tree(tree, cache=after)

    assert after.stats.hits == 0
    assert after.stats.misses == 3
    assert len(entries(directory)) == 6, "the old generation is left alone, not overwritten"
    assert after.generation_dir != before.generation_dir
    assert fingerprint_of(inventory) == fingerprint_of(load_tree(tree))


@pytest.mark.parametrize(
    "change",
    [
        {"format_version": 99},
        {"netgraph": "99.0.0"},
        {"api_version": "netgraph.dev/v2"},
        {"parser": "SomeOtherStrictSafeLoader"},
        {"dependencies": (("pydantic", "0.0.0"),)},
        {"source_stamp": "0000000000000000"},
    ],
    ids=lambda change: next(iter(change)),
)
def test_every_input_of_the_identity_changes_the_fingerprint(change: dict[str, Any]) -> None:
    """Each of these is a reason yesterday's conclusion may be wrong today."""
    current = Identity.current()
    assert replace(current, **change).fingerprint != current.fingerprint


def test_the_source_stamp_follows_the_installed_sources() -> None:
    """It is a digest of netgraph's own files, so it is stable within a run."""
    assert source_stamp() == source_stamp()
    assert len(source_stamp()) == 16
    assert source_stamp() in dict(Identity.current().describe()).values()


def test_a_template_file_is_never_cached(tmp_path: Path, store: DocumentCache) -> None:
    """Its meaning belongs to the files that inherit it, not to its own bytes."""
    root = tmp_path / "inventory"
    write(
        root,
        "templates.yaml",
        """\
apiVersion: netgraph.dev/v1alpha1
kind: template
metadata:
  name: access
spec:
  vendor: Cisco
  interfaces:
    - name: Gi0/1
      type: ethernet
""",
    )
    write(
        root,
        "sw.yaml",
        """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw1
spec:
  from: access
""",
    )

    inventory = load_tree(root, cache=store)

    assert not inventory.errors
    assert inventory.devices["sw1"].spec.vendor == "Cisco"
    assert entries(store.directory) == []
    assert store.stats.uncacheable == 2, "the template's file, and the file that inherits it"
    assert fingerprint_of(load_tree(root, cache=store)) == fingerprint_of(load_tree(root))


def test_a_template_alongside_a_plain_document_leaves_both_uncached(
    tmp_path: Path, store: DocumentCache
) -> None:
    """A file is cached whole or not at all; a template in it disqualifies it."""
    root = tmp_path / "inventory"
    write(
        root,
        "mixed.yaml",
        "apiVersion: netgraph.dev/v1alpha1\n"
        "kind: template\n"
        "metadata:\n  name: base\n"
        "spec:\n  vendor: Cisco\n"
        "---\n" + SWITCH.format(name="sw1", mtu=1500),
    )

    load_tree(root, cache=store)

    assert entries(store.directory) == []
    assert store.stats.uncacheable == 1


# --------------------------------------------------------------------------- #
# Damaged entries
# --------------------------------------------------------------------------- #


def _corrupt(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda blob: b"", id="empty"),
        pytest.param(lambda blob: blob[: len(blob) // 2], id="truncated"),
        pytest.param(lambda blob: blob[:40], id="header-only"),
        pytest.param(lambda blob: b"garbage, not a cache entry at all\n", id="garbage"),
        pytest.param(lambda blob: os.urandom(len(blob)), id="random-bytes"),
        pytest.param(
            lambda blob: blob.replace(b"netgraph-cache/1", b"netgraph-cache/9"), id="magic"
        ),
        pytest.param(
            lambda blob: blob.split(b"\n", 1)[0] + b"\n" + b"x" * (len(blob) - 40),
            id="body-not-zlib",
        ),
    ],
)
def test_a_damaged_entry_falls_back_to_a_full_parse(
    tree: Path, tmp_path: Path, damage: Any
) -> None:
    """Every way an entry can be unusable ends in a parse, never in an error."""
    directory = tmp_path / "cache"
    load_tree(tree, cache=DocumentCache(directory))
    victim = entries(directory)[0]
    _corrupt(victim, damage(victim.read_bytes()))

    store = DocumentCache(directory)
    inventory = load_tree(tree, cache=store)

    assert fingerprint_of(inventory) == fingerprint_of(load_tree(tree))
    assert store.stats.rejected + store.stats.misses >= 1
    assert store.stats.writes >= 1, "the damaged entry was replaced"

    # And the replacement is good: the run after this one pays nothing, rather
    # than tripping over the same file forever.
    third = DocumentCache(directory)
    load_tree(tree, cache=third)
    assert (third.stats.hits, third.stats.rejected, third.stats.misses) == (3, 0, 0)


def _entry_holding(directory: Path, marker: bytes) -> Path:
    """The entry whose serialised elements contain ``marker``.

    Which file lands in which shard is a property of a hash, so a test that
    tampers with a *particular* value has to go looking for it rather than take
    the first entry it finds.
    """
    for path in entries(directory):
        header, rest = path.read_bytes().split(b"\n", 1)
        head = int(header.split(b" ")[2])
        if marker in zlib.decompress(rest[head:]):
            return path
    raise AssertionError(f"no cache entry holds {marker!r}")


def test_an_entry_holding_a_valid_but_foreign_document_is_refused(
    tree: Path, tmp_path: Path
) -> None:
    """The key is checked inside the file, not only in its name.

    An entry that decodes perfectly but was written for other bytes is the one
    corruption a checksum cannot catch, so the key is stored as well as used.
    """
    directory = tmp_path / "cache"
    load_tree(tree, cache=DocumentCache(directory))
    first, second = entries(directory)[0], entries(directory)[1]
    first.write_bytes(second.read_bytes())

    store = DocumentCache(directory)
    inventory = load_tree(tree, cache=store)

    assert store.stats.rejected == 1
    assert fingerprint_of(inventory) == fingerprint_of(load_tree(tree))


def test_an_entry_whose_element_is_no_longer_valid_is_refused(tree: Path, tmp_path: Path) -> None:
    """Reconstruction goes through the validators, so a tampered entry is a miss."""
    directory = tmp_path / "cache"
    load_tree(tree, cache=DocumentCache(directory))
    victim = _entry_holding(directory, b'"mtu":1500')

    header, rest = victim.read_bytes().split(b"\n", 1)
    magic, key, head_len, _ = header.split(b" ")
    head = int(head_len)
    meta, body = rest[:head], rest[head:]
    tampered = zlib.decompress(body).replace(b'"mtu":1500', b'"mtu":"enormous"')
    packed = zlib.compress(tampered, 1)
    victim.write_bytes(b"%s %s %d %d\n" % (magic, key, head, len(packed)) + meta + packed)

    store = DocumentCache(directory)
    inventory = load_tree(tree, cache=store)

    assert store.stats.rejected == 1
    assert fingerprint_of(inventory) == fingerprint_of(load_tree(tree))


def test_an_entry_is_never_python_bytecode_or_a_pickle(tree: Path, store: DocumentCache) -> None:
    """The format is a header, then zlib: nothing that can construct an object."""
    load_tree(tree, cache=store)
    for entry in entries(store.directory):
        blob = entry.read_bytes()
        assert blob.startswith(b"netgraph-cache/1 ")
        header, rest = blob.split(b"\n", 1)
        _, _, head_len, tail_len = header.split(b" ")
        assert int(head_len) + int(tail_len) == len(rest)
        meta = json.loads(zlib.decompress(rest[: int(head_len)]))
        assert set(meta) == {"slots", "errors"}
        assert isinstance(json.loads(zlib.decompress(rest[int(head_len) :])), list)


def test_an_unwritable_cache_directory_is_not_an_error(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache that cannot be written is a slow netgraph, not a broken one."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("netgraph.loader.cache.write_bytes_atomically", refuse)
    store = DocumentCache(tmp_path / "cache")

    inventory = load_tree(tree, cache=store)

    assert fingerprint_of(inventory) == fingerprint_of(load_tree(tree))
    assert store.stats.writes == 0
    assert store.stats.problem is not None and "cannot write" in store.stats.problem


def test_an_unreadable_inventory_file_is_reported_not_cached(
    tree: Path, store: DocumentCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loader's own diagnostic, on the path that reads the bytes for hashing."""
    real = Path.read_bytes

    def refuse(self: Path) -> bytes:
        if self.name == "sw1.yaml":
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    inventory = load_tree(tree, cache=store)

    assert [error.relative for error in inventory.errors] == ["a/sw1.yaml"]
    assert "cannot read file" in inventory.errors[0].message


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def _load_in_subprocess(root: str, directory: str) -> int:
    """Load ``root`` through a cache at ``directory``. Runs in a child process."""
    from netgraph.loader import load_tree as load
    from netgraph.loader.cache import DocumentCache as Store

    inventory = load(Path(root), cache=Store(Path(directory)))
    return len(inventory.elements)


def test_concurrent_processes_do_not_corrupt_the_cache(tmp_path: Path) -> None:
    """Four processes fill one cache at once; every entry must still decode.

    Entries are written to a temporary file and renamed, so a reader sees a whole
    entry or no entry. Two processes racing on the same key write identical bytes
    by construction — the key *is* the content — so the loser of the race is not
    a problem either.
    """
    root = tmp_path / "inventory"
    for index in range(40):
        write(root, f"d{index // 8}/sw{index}.yaml", SWITCH.format(name=f"sw{index}", mtu=1500))
    directory = tmp_path / "cache"

    context = multiprocessing.get_context("spawn")
    with context.Pool(4) as pool:
        counts = pool.starmap(_load_in_subprocess, [(str(root), str(directory))] * 4)

    assert counts == [40] * 4
    store = DocumentCache(directory)
    inventory = load_tree(root, cache=store)
    assert fingerprint_of(inventory) == fingerprint_of(load_tree(root))
    assert store.stats.rejected == 0, "a torn entry would show up here"
    assert store.stats.hits == 40


def test_a_half_written_entry_is_not_visible(tree: Path, tmp_path: Path) -> None:
    """The temporary file a killed process leaves is not mistaken for an entry."""
    directory = tmp_path / "cache"
    store = DocumentCache(directory)
    load_tree(tree, cache=store)
    entry = entries(directory)[0]
    temporary = entry.with_name(f".{entry.name}.netgraph.tmp")
    temporary.write_bytes(b"half a")

    reader = DocumentCache(directory)
    load_tree(tree, cache=reader)

    assert reader.stats.hits == 3
    assert reader.stats.rejected == 0


# --------------------------------------------------------------------------- #
# Size and eviction
# --------------------------------------------------------------------------- #


def test_the_cache_is_swept_back_under_its_cap(tmp_path: Path) -> None:
    """Past the cap, the least recently used entries go first."""
    root = tmp_path / "inventory"
    for index in range(30):
        write(root, f"sw{index}.yaml", SWITCH.format(name=f"sw{index}", mtu=1500))
    directory = tmp_path / "cache"

    unbounded = DocumentCache(directory)
    load_tree(root, cache=unbounded)
    written = entries(directory)
    total = sum(path.stat().st_size for path in written)
    assert len(written) == 30

    # Age half of them, then fill the cache from a *different* inventory sharing
    # the directory. Loading this tree again would not do: reading an entry
    # refreshes its recency, which is the whole point of the mtime, so the aged
    # half would be the newest by the time the sweep ran.
    old = time.time() - 86_400
    for path in written[:15]:
        os.utime(path, (old, old))
    other = tmp_path / "other"
    write(other, "sw99.yaml", SWITCH.format(name="sw99", mtu=1500))

    # A cap of 0.7 of what is there, so the sweep has to drop about thirteen of
    # the thirty-one entries: fewer than the fifteen aged ones, which is what
    # makes *which* ones go a statement about recency rather than about the
    # arbitrary ordering of thirty entries written in the same millisecond.
    cap = int(total * 0.7)
    capped = DocumentCache(directory, max_bytes=cap)
    load_tree(other, cache=capped)

    survivors = entries(directory)
    assert capped.stats.evicted > 0
    assert sum(path.stat().st_size for path in survivors) <= cap
    assert all(path.exists() for path in written[15:]), "the recent entries survived"
    assert not all(path.exists() for path in written[:15]), "the aged ones went first"


def test_a_read_only_run_never_sweeps(tree: Path, tmp_path: Path) -> None:
    """A cache that is only read cannot have outgrown its cap."""
    directory = tmp_path / "cache"
    load_tree(tree, cache=DocumentCache(directory))

    store = DocumentCache(directory, max_bytes=0)
    load_tree(tree, cache=store)

    assert store.stats.evicted == 0
    assert len(entries(directory)) == 3


def test_one_store_serving_two_trees_does_not_confuse_them(tmp_path: Path) -> None:
    """The memory tier hands back decoded diagnostics, which name a real file.

    Two trees holding the same bytes at the same relative path share a disk key
    by design — the entry is the same conclusion — but their diagnostics point at
    two different files, so the tier that skips decoding must not.
    """
    broken = SWITCH.format(name="sw1", mtu="not-a-number")
    first = tmp_path / "first"
    second = tmp_path / "second"
    write(first, "sw.yaml", broken)
    write(second, "sw.yaml", broken)
    store = DocumentCache(tmp_path / "cache")

    one = load_tree(first, cache=store)
    two = load_tree(second, cache=store)

    assert [str(error.path) for error in one.errors] == [str(first / "sw.yaml")]
    assert [str(error.path) for error in two.errors] == [str(second / "sw.yaml")]


def test_the_memory_tier_is_bounded(tree: Path, tmp_path: Path) -> None:
    """A long-running watch cannot grow without limit either."""
    store = DocumentCache(tmp_path / "cache", memory_bytes=1)
    load_tree(tree, cache=store)
    load_tree(tree, cache=store)

    assert store.stats.memory_hits <= 1
    assert store.stats.hits == 3, "the disk tier still answers"


def test_clearing_removes_only_cache_entries(tree: Path, tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    load_tree(tree, cache=DocumentCache(directory))
    bystander = directory / "please-do-not-delete-me.txt"
    bystander.write_text("mine", encoding="utf-8")

    removed, freed = clear_cache(directory)

    assert removed == 3
    assert freed > 0
    assert entries(directory) == []
    assert bystander.read_text(encoding="utf-8") == "mine"


def test_inspecting_counts_the_current_and_the_stale_generation(tree: Path, tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    current = DocumentCache(directory)
    load_tree(tree, cache=current)
    stale = DocumentCache(directory, identity=replace(current.identity, netgraph="0.0.1"))
    load_tree(tree, cache=stale)

    info = inspect_cache(directory, identity=current.identity)

    assert info.entries == 3
    assert info.stale_entries == 3
    assert info.used_bytes > 0 and info.stale_bytes > 0
    assert info.exists


def test_inspecting_a_directory_that_does_not_exist_is_empty(tmp_path: Path) -> None:
    info = inspect_cache(tmp_path / "absent")

    assert (info.entries, info.used_bytes, info.stale_entries) == (0, 0, 0)
    assert not info.exists


# --------------------------------------------------------------------------- #
# Where it lives
# --------------------------------------------------------------------------- #


def test_the_environment_variable_outranks_everything(tmp_path: Path) -> None:
    directory, origin = resolve_cache_root(
        configured=tmp_path / "from-toml",
        environ={CACHE_DIR_ENV_VAR: str(tmp_path / "from-env"), "XDG_CACHE_HOME": "/xdg"},
    )

    assert directory == tmp_path / "from-env"
    assert origin == CACHE_DIR_ENV_VAR


def test_the_configuration_file_outranks_xdg(tmp_path: Path) -> None:
    directory, origin = resolve_cache_root(
        configured=tmp_path / "from-toml", environ={"XDG_CACHE_HOME": "/xdg"}
    )

    assert directory == tmp_path / "from-toml"
    assert "netgraph.toml" in origin


def test_xdg_cache_home_is_honoured(tmp_path: Path) -> None:
    directory, origin = resolve_cache_root(environ={"XDG_CACHE_HOME": str(tmp_path)})

    assert directory == tmp_path / "netgraph"
    assert origin == "XDG_CACHE_HOME"


def test_the_platform_default_is_used_when_nothing_says_otherwise() -> None:
    directory, origin = resolve_cache_root(environ={})

    assert directory.is_absolute()
    assert "netgraph" in str(directory).lower()
    assert origin


def test_two_inventories_get_two_directories(tmp_path: Path) -> None:
    base = tmp_path / "base"
    first = tmp_path / "one" / "inventory"
    second = tmp_path / "two" / "inventory"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    a, b = inventory_cache_dir(first, base), inventory_cache_dir(second, base)

    assert a != b
    assert a.name.startswith("inventory-") and b.name.startswith("inventory-")
    assert a.parent == base / "inventories"


def test_a_single_file_inventory_gets_a_directory_too(tmp_path: Path) -> None:
    """``-i one.yaml`` is a supported inventory, so it must be cacheable."""
    path = write(tmp_path, "one.yaml", SWITCH.format(name="sw1", mtu=1500))
    store = DocumentCache(tmp_path / "cache")

    inventory = load_tree(path, cache=store)
    warm = load_tree(path, cache=DocumentCache(tmp_path / "cache"))

    assert list(inventory.elements) == ["sw1"]
    assert fingerprint_of(warm) == fingerprint_of(inventory)
    assert inventory_cache_dir(path, tmp_path).name.startswith("one.yaml-")


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_disable_variable_is_believed(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DISABLE_ENV_VAR, value)
    assert disabled_by_environment()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
def test_anything_else_leaves_the_cache_on(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DISABLE_ENV_VAR, value)
    assert not disabled_by_environment()


def test_open_cache_creates_nothing(tmp_path: Path) -> None:
    """A run that only hits, or one whose cache is unwritable, leaves no trace."""
    store = open_cache(tmp_path, environ={CACHE_DIR_ENV_VAR: str(tmp_path / "cache")})

    assert not (tmp_path / "cache").exists()
    assert store.max_bytes == DEFAULT_MAX_BYTES


# --------------------------------------------------------------------------- #
# The [cache] table
# --------------------------------------------------------------------------- #


def test_the_default_table_is_the_default_cache() -> None:
    assert parse_cache({}) == CacheConfig()
    assert CacheConfig().enabled
    assert CacheConfig().max_bytes == DEFAULT_MAX_BYTES


def test_the_table_is_read_from_the_configuration_file(tmp_path: Path) -> None:
    config = parse_config(
        {"cache": {"enabled": False, "dir": "cache", "max-size": "8MB"}},
        path=tmp_path / "netgraph.toml",
    )

    assert not config.cache.enabled
    assert config.cache.directory == tmp_path / "cache"
    assert config.cache.max_bytes == 8_000_000


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1024, 1024),
        ("512", 512),
        ("64kB", 64_000),
        ("2MB", 2_000_000),
        ("1GB", 1_000_000_000),
        ("1KiB", 1024),
        ("1.5MiB", 1_572_864),
        ("0", 0),
    ],
)
def test_a_size_may_carry_a_unit(value: Any, expected: int) -> None:
    assert parse_cache({"max-size": value}).max_bytes == expected


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"enabled": "yes"}, "must be true or false"),
        ({"dir": ""}, "non-empty path"),
        ({"dir": 7}, "non-empty path"),
        ({"max-size": "many"}, "is not a size"),
        ({"max-size": "10 furlongs"}, "names no unit"),
        ({"max-size": True}, "byte count"),
        ({"maxsize": 1}, "unknown key"),
    ],
)
def test_an_unusable_table_says_what_is_wrong(table: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_cache(table)


def test_the_table_must_be_a_table() -> None:
    with pytest.raises(ConfigurationError, match="must be a table"):
        parse_cache([1, 2, 3])


def test_no_cache_only_ever_turns_the_cache_off() -> None:
    assert not CacheConfig().with_overrides(no_cache=True).enabled
    assert not CacheConfig(enabled=False).with_overrides(no_cache=False).enabled


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the cache at a directory of this test's own."""
    directory = tmp_path / "cli-cache"
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(directory))
    monkeypatch.delenv(DISABLE_ENV_VAR, raising=False)
    yield directory


def test_a_command_fills_the_cache_and_the_next_one_hits(
    tree: Path, isolated: Path, runner: CliRunner
) -> None:
    first = runner.invoke(cli, ["-i", str(tree), "-v", "validate"])
    assert first.exit_code == 0, first.output
    assert "0 hit(s)" in first.output and "3 written" in first.output

    second = runner.invoke(cli, ["-i", str(tree), "-v", "validate"])
    assert second.exit_code == 0, second.output
    assert "3 hit(s)" in second.output and "0 written" in second.output


def test_no_cache_writes_nothing(tree: Path, isolated: Path, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["-i", str(tree), "--no-cache", "-v", "validate"])

    assert result.exit_code == 0, result.output
    assert "cache:" not in result.output
    assert not isolated.exists()


def test_the_disable_variable_reaches_the_command_line(
    tree: Path, isolated: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DISABLE_ENV_VAR, "1")

    result = runner.invoke(cli, ["-i", str(tree), "validate"])

    assert result.exit_code == 0, result.output
    assert not isolated.exists()


def test_the_table_can_switch_the_cache_off(tree: Path, isolated: Path, runner: CliRunner) -> None:
    (tree / "netgraph.toml").write_text("[cache]\nenabled = false\n", encoding="utf-8")

    result = runner.invoke(cli, ["-i", str(tree), "validate"])

    assert result.exit_code == 0, result.output
    assert not isolated.exists()


def test_cache_info_reports_an_empty_cache(tree: Path, isolated: Path, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["-i", str(tree), "cache", "info"])

    assert result.exit_code == 0, result.output
    assert "enabled        true" in result.output
    assert str(isolated) in result.output
    assert CACHE_DIR_ENV_VAR in result.output
    assert "nothing cached yet" in result.output
    assert "apiVersion" in result.output


def test_cache_info_counts_what_is_there(tree: Path, isolated: Path, runner: CliRunner) -> None:
    assert runner.invoke(cli, ["-i", str(tree), "validate"]).exit_code == 0

    result = runner.invoke(cli, ["-i", str(tree), "cache", "info"])

    assert result.exit_code == 0, result.output
    assert "entries        3" in result.output
    assert "nothing cached" not in result.output


def test_cache_info_names_the_reason_the_cache_is_off(
    tree: Path, isolated: Path, runner: CliRunner
) -> None:
    result = runner.invoke(cli, ["-i", str(tree), "--no-cache", "cache", "info"])

    assert result.exit_code == 0, result.output
    assert "false (--no-cache)" in result.output


def test_cache_clear_empties_this_inventory(tree: Path, isolated: Path, runner: CliRunner) -> None:
    assert runner.invoke(cli, ["-i", str(tree), "validate"]).exit_code == 0

    result = runner.invoke(cli, ["-i", str(tree), "cache", "clear"])

    assert result.exit_code == 0, result.output
    assert "3 entries" in result.output
    assert entries(isolated) == []


def test_cache_clear_all_empties_every_inventory(
    tree: Path, tmp_path: Path, isolated: Path, runner: CliRunner
) -> None:
    other = tmp_path / "other"
    write(other, "sw.yaml", SWITCH.format(name="sw9", mtu=1500))
    assert runner.invoke(cli, ["-i", str(tree), "validate"]).exit_code == 0
    assert runner.invoke(cli, ["-i", str(other), "validate"]).exit_code == 0
    assert len(entries(isolated)) == 4

    result = runner.invoke(cli, ["-i", str(tree), "cache", "clear", "--all"])

    assert result.exit_code == 0, result.output
    assert "every inventory" in result.output
    assert entries(isolated) == []


def test_clearing_an_empty_cache_says_so(tree: Path, isolated: Path, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["-i", str(tree), "cache", "clear"])

    assert result.exit_code == 0, result.output
    assert "0 entries" in result.output


def test_a_broken_configuration_file_does_not_break_an_uncached_command(
    tree: Path, isolated: Path, runner: CliRunner
) -> None:
    """The cache asking about a table must not add a failure mode to `list`."""
    (tree / "netgraph.toml").write_text("[render]\nshow_ips = false\n", encoding="utf-8")

    result = runner.invoke(cli, ["-i", str(tree), "list", "devices"])

    assert result.exit_code == 0, result.output
    assert "sw1" in result.output


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --------------------------------------------------------------------------- #
# The loop the cache exists for
# --------------------------------------------------------------------------- #


def test_a_watch_cycle_reparses_only_what_changed(tree: Path, store: DocumentCache) -> None:
    """The incremental claim, at the level ``netgraph watch`` runs it."""
    request = RenderRequest(inventory=tree, output_format="dot", cache=store)

    first = run_cycle(request)
    assert first.status.is_ok, first.message
    assert (store.stats.hits, store.stats.writes) == (0, 3)

    store.stats.hits = store.stats.misses = store.stats.writes = 0
    unchanged = run_cycle(request)
    assert unchanged.status.is_ok
    assert (store.stats.hits, store.stats.misses, store.stats.writes) == (3, 0, 0)
    assert unchanged.payload == first.payload

    (tree / "a/sw1.yaml").write_text(SWITCH.format(name="sw1", mtu=9000), encoding="utf-8")
    store.stats.hits = store.stats.misses = store.stats.writes = 0
    edited = run_cycle(request)

    assert edited.status.is_ok
    assert (store.stats.hits, store.stats.misses, store.stats.writes) == (2, 1, 1)
    assert edited.payload == run_cycle(RenderRequest(inventory=tree, output_format="dot")).payload


def test_a_watch_cycle_survives_a_file_becoming_unparseable(
    tree: Path, store: DocumentCache
) -> None:
    """A half-typed document must not be remembered as a good one."""
    request = RenderRequest(inventory=tree, output_format="dot", cache=store)
    good = run_cycle(request)
    assert good.status.is_ok

    (tree / "a/sw1.yaml").write_text("spec: [unclosed\n", encoding="utf-8")
    broken = run_cycle(request)
    assert not broken.status.is_ok

    (tree / "a/sw1.yaml").write_text(SWITCH.format(name="sw1", mtu=1500), encoding="utf-8")
    recovered = run_cycle(request)

    assert recovered.status.is_ok
    assert recovered.payload == good.payload
