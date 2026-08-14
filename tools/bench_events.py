#!/usr/bin/env python3
"""Time what one edit costs the browser, polling against pushing.

``tools/bench_incremental.py`` measures the *server's* reload after one edit.
This measures the round trip that reload sits inside: an editor saves one file
and the page has to end up showing the truth again. Before the push channel that
meant, every time, a poll that noticed a second later, a fetch of the whole file
list, and a full Graphviz run — whether or not the drawn layer had moved at all.

Run it against a generated tree::

    python tools/bench_events.py                       # generate, time, report
    python tools/bench_events.py --inventory ./net     # time an existing tree
    python tools/bench_events.py --sites 2 --racks 3   # something smaller

Four measurements, each the median of a handful of rounds:

* **notice** — how long after a write the page learns about it. Polling is the
  interval, so the average is half of it and the worst case is all of it; the
  stream is a queue hand-off.
* **the file list** — ``GET /api/tree`` against ``GET /api/tree?path=<one>``.
  Both answer with the same row for that file; one of them walks the tree,
  hashes every file and serialises the lot to do it.
* **the diagram, when the picture moved** — a full pass. Unavoidable, and the
  number the other rows are measured against.
* **the diagram, when it did not** — the same request with the fingerprint of
  the drawing already on screen. The pass still loads, validates and builds the
  graph; what it skips is the Graphviz run, which is the expensive part.

The last row is the common case, not a contrived one: descriptions, labels,
owners, a device added to a namespace the current view filters out, a comment —
none of them change the drawing of the layer you are looking at, and on a tree
this size each one used to cost a full layout.

The generated tree is ``tools/bench_pipeline.py``'s, so every number here is
comparable with the tables in ``docs/follow-ups.md``.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import threading
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

from netgraph.render.dot import find_dot  # noqa: E402
from netgraph.web.preview import ViewOptions  # noqa: E402
from netgraph.web.session import EditingSession  # noqa: E402

T = TypeVar("T")

#: Rounds per measurement. The median, because the first touch of anything on
#: this path is a page-cache miss and the question is what the *loop* costs.
SAMPLES: Final = 5

#: What the page used to poll at, in milliseconds. The old client's ``POLL_MS``.
POLL_MS: Final = 1000


def sample(call: Callable[[], T], *, repeat: int = SAMPLES) -> tuple[T, float]:
    """Run ``call`` ``repeat`` times; return the last result and the median ms."""
    timings = []
    result: T
    for _ in range(repeat):
        start = time.perf_counter()
        result = call()
        timings.append((time.perf_counter() - start) * 1000)
    return result, statistics.median(timings)


def row(label: str, before: float, after: float, note: str = "") -> None:
    ratio = before / after if after > 0 else float("inf")
    print(f"{label:<34} {before:9.1f} ms  →{after:9.1f} ms   {ratio:6.1f}x  {note}".rstrip())


def notice_latency(session: EditingSession, target: Path, root: Path) -> tuple[float, float]:
    """How long a change takes to reach a client, polling against streaming.

    The polling figure is the interval's own arithmetic — a poll that fires on a
    timer learns about a change that landed at a uniformly random point in the
    interval, so the mean wait is half of it. The streaming figure is measured:
    a real subscriber, on a real thread, waiting on the real bus.
    """
    arrived: list[float] = []
    with session.events.subscribe() as subscription:
        ready = threading.Event()

        def wait() -> None:
            ready.set()
            subscription.wait(10.0)
            arrived.append(time.perf_counter())

        for _ in range(SAMPLES):
            arrived.clear()
            reader = threading.Thread(target=wait, daemon=True)
            reader.start()
            ready.wait(1.0)
            time.sleep(0.01)  # let the reader reach the wait rather than race it
            start = time.perf_counter()
            target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            session.invalidate([str(target.relative_to(root))])
            reader.join(10.0)
            subscription.wait(0)  # drain the rest of the batch
        pushed = (arrived[-1] - start) * 1000 if arrived else float("nan")
    return POLL_MS / 2, pushed


def report(root: Path, *, edits: int) -> None:
    files = yaml_files(root)
    size = sum(path.stat().st_size for path in files)
    print(f"inventory: {root}")
    print(f"           {len(files)} files, {size / 1_000_000:.1f} MB")
    print()

    session = EditingSession(root=root, writable=True)
    session.inventory()
    target = max(files, key=lambda path: path.stat().st_size)
    relative = target.relative_to(root).as_posix()

    print(f"{'':<34} {'polling':>12}   {'push':>12}")
    print("-" * 92)

    polled, pushed = notice_latency(session, target, root)
    row("notice a change", polled, pushed, "mean wait; the stream is a hand-off")

    _, whole = sample(lambda: session.tree(), repeat=edits)
    _, graded = sample(lambda: session.tree([relative]), repeat=edits)
    _, part = sample(lambda: session.tree([relative], diagnostics=False), repeat=edits)
    row("fetch the file list", whole, graded, f"{len(files)} rows → 1 ({relative})")
    row("  … without re-grading it", whole, part, "the applied change already said")

    if find_dot() is None:
        print()
        print("Graphviz is not installed, so the diagram rows are skipped.")
        return

    view = ViewOptions()
    first, drawn = sample(lambda: session.graph(view)[0], repeat=max(2, edits // 2))
    digest = first.graph_hash
    assert digest is not None
    _, skipped = sample(lambda: session.graph(view, known=digest)[0], repeat=edits)
    row("draw after an edit", drawn, drawn, "the picture moved: nothing to skip")
    row("draw after an edit", drawn, skipped, "the picture did not move")

    print()
    print(
        f"one edit that does not touch the drawn layer: "
        f"{polled + whole + drawn:9.1f} ms → {pushed + part + skipped:9.1f} ms "
        f"({(polled + whole + drawn) / (pushed + part + skipped):.1f}x)"
    )
    print(
        f"one edit that does:                          "
        f"{polled + whole + drawn:9.1f} ms → {pushed + part + drawn:9.1f} ms "
        f"({(polled + whole + drawn) / (pushed + part + drawn):.1f}x)"
    )


def _inventory_root(args: argparse.Namespace) -> Iterator[Path]:
    if args.inventory is not None:
        yield Path(args.inventory)
        return
    shape = Shape(sites=args.sites, racks_per_site=args.racks, hosts_per_rack=args.hosts)
    target = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="netgraph-bench-"))
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
    parser.add_argument("--edits", type=int, default=SAMPLES, help="rounds per measurement")
    parser.add_argument("--keep", help="write the tree here and leave it behind")
    parser.add_argument("--inventory", help="time an existing tree instead of generating one")
    args = parser.parse_args(argv)
    for root in _inventory_root(args):
        report(root, edits=args.edits)
    return 0


if __name__ == "__main__":  # pragma: no cover - a harness, not a library
    raise SystemExit(main())
