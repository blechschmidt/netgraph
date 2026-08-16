#!/usr/bin/env python3
"""Time the case ``netviz watch`` actually runs: reload after one edit.

``tools/bench_pipeline.py`` measures a *cold* pipeline — every file parsed, every
rule run, the graph built from nothing. That is the right measurement for the
first invocation and the wrong one for the loop that matters: an editor saves one
file, and netviz re-reads all 138 of them to draw a diagram that differs in one
node. This harness measures that loop, and the pieces of it, so a claim about
incremental cost can be checked rather than asserted::

    python tools/bench_incremental.py                    # generate, time, report
    python tools/bench_incremental.py --inventory ./net  # time an existing tree
    python tools/bench_incremental.py --edits 5          # more samples per stage

It reports, for the same tree:

* **cold** — ``load_tree`` with no cache, which is what every command did before.
* **cold, filling the cache** — the same load plus writing what it learned. The
  gap is what the first run of a cache costs.
* **warm, cold process** — a second process finding the cache on disk: no YAML
  parse, but every element is deserialised and re-validated.
* **warm, same process** — a second load in one process, which is ``watch``: the
  files that did not change cost a hash and a dictionary lookup.
* **reload after one edit** — the whole point. One file is rewritten between
  loads, so exactly one is parsed.
* **the rest of the cycle** — validate, build the graph, render. None of it is
  incremental, and on a large tree it is what a re-render now spends its time on.

The generated tree is ``tools/bench_pipeline.py``'s, unchanged, so every number
here is comparable with the tables in ``docs/follow-ups.md``.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - convenience for a checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT / "tools") not in sys.path:  # pragma: no cover - importing the sibling harness
    sys.path.insert(0, str(REPO_ROOT / "tools"))

from bench_pipeline import Shape, generate, yaml_files  # noqa: E402

from netviz.loader import load_tree  # noqa: E402
from netviz.loader.cache import DocumentCache  # noqa: E402
from netviz.render import build_graph, render  # noqa: E402
from netviz.validate import validate  # noqa: E402

T = TypeVar("T")

#: How the reported cost of a stage is summarised. The median, because the first
#: sample of anything that touches the file system is a page-cache miss and the
#: question here is what the *loop* costs, not what its first turn costs.
SAMPLES: Final = 5


def sample(call: Callable[[], T], *, repeat: int = SAMPLES) -> tuple[T, float, float]:
    """Run ``call`` ``repeat`` times; return the last result, the median and the min."""
    timings = []
    result: T
    for _ in range(repeat):
        start = time.perf_counter()
        result = call()
        timings.append((time.perf_counter() - start) * 1000)
    return result, statistics.median(timings), min(timings)


class Row:
    """One line of the report, remembered so later lines can compare against it."""

    def __init__(self, baseline: float | None = None) -> None:
        self.baseline = baseline

    def print(self, label: str, median: float, minimum: float, note: str = "") -> float:
        if self.baseline is None:
            self.baseline = median
        ratio = self.baseline / median if median > 0 else float("inf")
        print(f"{label:<32} {median:8.1f} ms  (min {minimum:6.1f})  {ratio:5.2f}x  {note}".rstrip())
        return median


def cache_bytes(directory: Path) -> tuple[int, int]:
    """``(entries, bytes)`` currently on disk under ``directory``."""
    entries = list(directory.rglob("*.ngc"))
    return len(entries), sum(path.stat().st_size for path in entries)


def report(root: Path, *, cache_root: Path, edits: int) -> None:
    files = yaml_files(root)
    size = sum(path.stat().st_size for path in files)
    print(f"inventory: {root}")
    print(f"           {len(files)} files, {size / 1_000_000:.1f} MB")
    print()

    inventory, cold, cold_min = sample(lambda: load_tree(root))
    row = Row(baseline=cold)
    row.print("cold load (no cache)", cold, cold_min, f"{len(inventory)} elements")

    shutil.rmtree(cache_root, ignore_errors=True)
    fill_store = DocumentCache(cache_root)
    _, fill, fill_min = sample(lambda: load_tree(root, cache=fill_store), repeat=1)
    count, occupied = cache_bytes(cache_root)
    row.print(
        "cold load, filling the cache",
        fill,
        fill_min,
        f"{count} entries, {occupied / 1000:.0f} kB on disk",
    )
    stats = fill_store.stats
    print(
        f"{'':<32} {stats.writes} written, {stats.uncacheable} not cacheable "
        f"(templates and what inherits them)"
    )

    # A fresh store per sample: the disk tier is what the *next process* sees, and
    # a store that already answered would answer from memory instead.
    _, disk, disk_min = sample(lambda: load_tree(root, cache=DocumentCache(cache_root)))
    row.print("warm load, cold process", disk, disk_min, "disk tier")

    warm_store = DocumentCache(cache_root)
    load_tree(root, cache=warm_store)
    _, memory, memory_min = sample(lambda: load_tree(root, cache=warm_store))
    row.print("warm load, same process", memory, memory_min, "memory tier")

    edited = _largest(files)
    original = edited.read_bytes()
    try:
        counter = 0

        def edit_and_reload() -> object:
            nonlocal counter
            counter += 1
            edited.write_bytes(original.replace(b"host 01", b"host 01 rev%d" % counter))
            return load_tree(root, cache=warm_store)

        _, reload_ms, reload_min = sample(edit_and_reload, repeat=edits)
    finally:
        edited.write_bytes(original)
    row.print(
        "reload after editing 1 file",
        reload_ms,
        reload_min,
        f"{edited.relative_to(root)} ({len(original) / 1000:.0f} kB)",
    )

    print()
    print("what follows the load, none of it incremental:")
    after = 0.0
    _, validate_ms, validate_min = sample(lambda: validate(inventory))
    after += validate_ms
    print(f"{'validate':<32} {validate_ms:8.1f} ms  (min {validate_min:6.1f})")
    graph, graph_ms, graph_min = sample(lambda: build_graph(inventory))
    after += graph_ms
    print(f"{'build_graph':<32} {graph_ms:8.1f} ms  (min {graph_min:6.1f})")
    _, dot_ms, dot_min = sample(lambda: render(graph, "dot"))
    after += dot_ms
    print(f"{'render (dot)':<32} {dot_ms:8.1f} ms  (min {dot_min:6.1f})")

    print()
    print(
        f"one cycle, cold:        {cold + after:8.1f} ms ({100 * cold / (cold + after):.0f} % load)"
    )
    print(
        f"one cycle, incremental: {reload_ms + after:8.1f} ms "
        f"({100 * reload_ms / (reload_ms + after):.0f} % load), "
        f"{(cold + after) / (reload_ms + after):.2f}x the cold cycle"
    )


def _largest(files: list[Path]) -> Path:
    """The file to edit: the biggest one, so the re-parse is the worst case."""
    return max(files, key=lambda path: path.stat().st_size)


def _inventory_root(args: argparse.Namespace) -> Iterator[Path]:
    if args.inventory is not None:
        yield Path(args.inventory)
        return
    shape = Shape(sites=args.sites, racks_per_site=args.racks, hosts_per_rack=args.hosts)
    target = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="netviz-bench-"))
    if target.exists() and args.keep:
        shutil.rmtree(target)
    files, documents = generate(target, shape)
    print(f"generated {files} files / {documents} documents / {shape.devices} devices")
    try:
        yield target
    finally:
        if not args.keep:
            shutil.rmtree(target, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default = Shape()
    parser.add_argument("--sites", type=int, default=default.sites)
    parser.add_argument("--racks", type=int, default=default.racks_per_site)
    parser.add_argument("--hosts", type=int, default=default.hosts_per_rack)
    parser.add_argument(
        "--edits",
        type=int,
        default=SAMPLES,
        help="how many edit-and-reload rounds to time (median wins)",
    )
    parser.add_argument("--keep", help="write the tree here and leave it behind")
    parser.add_argument("--inventory", help="time an existing tree instead of generating one")
    parser.add_argument(
        "--cache-dir",
        help="where to put the cache under test; a temporary directory by default",
    )
    args = parser.parse_args(argv)

    cache_root = (
        Path(args.cache_dir)
        if args.cache_dir
        else Path(tempfile.mkdtemp(prefix="netviz-bench-cache-"))
    )
    try:
        for root in _inventory_root(args):
            report(root, cache_root=cache_root, edits=args.edits)
    finally:
        if not args.cache_dir:
            shutil.rmtree(cache_root, ignore_errors=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
