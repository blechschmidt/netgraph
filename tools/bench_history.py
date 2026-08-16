#!/usr/bin/env python3
"""Time the history timeline: is stepping a frame of a 1056-device tree interactive?

The scrubber in ``netviz web`` repaints the canvas on every step, so the
question this harness exists to answer is not "can it render a large network" —
``tools/bench_pipeline.py`` answered that — but **what one step costs**, which is
a different sum:

* a ``git archive`` of the inventory at one revision, out of the object database,
* a ``load_tree`` of what came out,
* a :func:`~netviz.plan.diff` against the revision before it,
* two graph builds and one Graphviz layout.

Of those, only the first three are per-*revision*; the second and third are
shared between neighbouring frames, which is why the timeline caches inventories
by tree hash. And the whole answer is cached by the *pair* of tree hashes, which
is why scrubbing back over ground already covered has to be free.

So it reports four numbers, and the third and fourth are the ones a claim about
interactivity rests on::

    python tools/bench_history.py                     # generate, commit, time
    python tools/bench_history.py --commits 12        # a longer history
    python tools/bench_history.py --inventory ./net   # an existing repository
    python tools/bench_history.py --keep out/history  # leave the repository behind

* **list the commits** — one ``git log`` and one ``cat-file --batch-check``, for
  the whole range. Paid once when the scrubber opens.
* **first frame, cold** — nothing cached: two revisions read and loaded, then
  rendered.
* **step a frame** — the case that has to stay interactive. The previous frame's
  *after* state is this frame's *before* state, so one revision is read rather
  than two.
* **step back to a frame already drawn** — the frame cache. A dictionary lookup
  and nothing else; if this is not near zero the cache is not keyed correctly.

The tree is ``tools/bench_pipeline.py``'s, unchanged, so the numbers line up with
every other table. The repository is built here rather than found, so the shape
of the history is known: one commit adds a rack of hosts, one changes an
address, one renames a switch, and so on round again.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - convenience for a checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))

from netviz.history import Timeline  # noqa: E402
from netviz.loader import load_tree  # noqa: E402
from netviz.render.dot import DOT_EXECUTABLE  # noqa: E402
from netviz.web.preview import ViewOptions, render_inventory  # noqa: E402
from netviz.web.session import EditingSession  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_pipeline import Shape, generate  # noqa: E402

T = TypeVar("T")

#: Where the inventory goes inside the generated repository. A subdirectory
#: rather than the root, because that is the shape a real repository has and the
#: shape that makes the pathspec and the ``<rev>:<prefix>`` lookups do work.
PREFIX: Final = "net"

#: How many commits the generated history holds. Enough to step through and see
#: the caches behave; small enough that generating it is not the benchmark.
COMMITS: Final = 8

#: What a step is allowed to cost, as a multiple of what *one plain render of
#: the same tree* costs. The bar cannot be an absolute number of milliseconds:
#: laying out 1056 nodes is around 700 ms of Graphviz on this machine whether it
#: is a frame of the history or the working tree, and a timeline cannot be
#: quicker than the diagram it is a timeline of. What it can promise is that
#: stepping is *the same order of work* as drawing — one more inventory read and
#: one changeset on top of a render that was going to happen anyway — and that
#: coming back to a frame costs nothing at all.
STEP_BUDGET: Final = 2.5

#: What a *revisit* is allowed to cost, absolutely. This one is a real bar: it
#: is a dictionary lookup, and anything that looks like work means the cache is
#: keyed wrongly.
REVISIT_MS: Final = 50.0


@dataclass(frozen=True, slots=True)
class Timing:
    label: str
    median: float
    lowest: float
    samples: int

    def line(self, *, bar: float | None = None, unit: str = "ms") -> str:
        verdict = ""
        if bar is not None:
            verdict = "   OK" if self.median <= bar else f"   OVER {bar:.0f} {unit}"
        return (
            f"{self.label:<38} {self.median:8.1f} ms   "
            f"(min {self.lowest:.1f}, n={self.samples}){verdict}"
        )


def timed(label: str, call: Callable[[], T], *, repeat: int) -> Timing:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return Timing(
        label=label,
        median=statistics.median(samples),
        lowest=min(samples),
        samples=len(samples),
    )


# --------------------------------------------------------------------------- #
# Building a history
# --------------------------------------------------------------------------- #


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def build_repository(root: Path, shape: Shape, *, commits: int) -> int:
    """Generate the tree under ``root/net`` and commit a history over it.

    Returns the number of commits that touched the inventory.
    """
    inventory = root / PREFIX
    inventory.mkdir(parents=True, exist_ok=True)
    files, documents = generate(inventory, shape)
    print(f"generated {files} files, {documents} documents, {shape.devices} devices")

    git(root, "init", "-q", ".")
    git(root, "config", "user.email", "bench@example.invalid")
    git(root, "config", "user.name", "bench")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Describe the network")

    for index in range(1, commits):
        _mutate(inventory, shape, index)
        git(root, "add", "-A")
        git(root, "commit", "-qm", f"Revision {index}: {_MUTATIONS[index % len(_MUTATIONS)]}")
    return commits


#: What each generated commit says it did, in the order they are applied.
_MUTATIONS: Final[tuple[str, ...]] = (
    "retire a host",
    "readdress a host",
    "re-label a switch",
)


def _mutate(inventory: Path, shape: Shape, index: int) -> None:
    """Make one commit's worth of change to the tree, deterministically.

    Small changes on purpose: a commit that rewrites the whole inventory would
    measure the loader, and the loader is measured elsewhere. What a timeline
    steps over is mostly small commits, and a small commit is the case where the
    fixed costs — the export, the load, the layout — dominate.
    """
    rack = (index % shape.racks_per_site) + 1
    hosts = inventory / f"sites/s01/racks/r{rack:02d}/hosts.yaml"
    text = hosts.read_text(encoding="utf-8")
    what = index % len(_MUTATIONS)
    if what == 0:
        # Retire a host: drop the last document in the file.
        head, _, _ = text.rpartition("---\n")
        hosts.write_text(head or text, encoding="utf-8")
    elif what == 1:
        hosts.write_text(text.replace("10.1.", "10.9.", 1), encoding="utf-8")
    else:
        switch = inventory / f"sites/s01/racks/r{rack:02d}/sw-s01-r{rack:02d}.yaml"
        source = switch.read_text(encoding="utf-8")
        switch.write_text(
            source.replace("model: ", f"model: rev{index} ", 1) if "model: " in source else source,
            encoding="utf-8",
        )


# --------------------------------------------------------------------------- #
# The measurement
# --------------------------------------------------------------------------- #


def report(root: Path, *, repeat: int) -> None:
    view = ViewOptions()
    inventory = root / PREFIX

    # The baseline every other number is judged against: what it costs this
    # editor to draw this tree once, with no history involved at all.
    plain = _session(root)
    plain.graph(view)
    baseline = timed(
        "render the working tree (baseline)",
        lambda: render_inventory(load_tree(inventory), view),
        repeat=max(2, repeat // 2),
    )
    print(baseline.line())

    listing = timed(
        "list the commits",
        lambda: Timeline.open(inventory).commits(),
        repeat=repeat,
    )
    print(listing.line())

    commits = Timeline.open(inventory).commits()
    print(f"  {len(commits)} revisions of the inventory, newest first")
    if len(commits) < 3:
        print("  not enough history to step through; pass --commits")
        return

    # Everything below goes through the session, because the session is what the
    # browser talks to and the caches that make this interactive live there.
    session = _session(root)
    cold = timed(
        "first frame, nothing cached",
        lambda: _drawn(_session(root).frame(commits[1].hash, view)),
        repeat=max(2, repeat // 2),
    )
    print(cold.line())

    # A step: the frame before this one has just been drawn, so its *after*
    # state is this frame's *before* state and one revision is read rather than
    # two — and the parse cache means that read parses the files the commit
    # touched rather than the two thousand it did not.
    session.frame(commits[len(commits) - 1].hash, view)
    position = {"index": len(commits) - 1}

    def step() -> None:
        position["index"] = max(1, position["index"] - 1)
        _drawn(session.frame(commits[position["index"]].hash, view))

    budget = baseline.median * STEP_BUDGET
    stepping = timed("step to the next frame", step, repeat=min(repeat, len(commits) - 2) or 1)
    print(stepping.line(bar=budget))

    revisit = timed(
        "step back to a frame already drawn",
        lambda: _drawn(session.frame(commits[1].hash, view)),
        repeat=repeat,
    )
    print(revisit.line(bar=REVISIT_MS))

    print()
    ratio = stepping.median / baseline.median if baseline.median else 0.0
    print(
        f"A step costs {ratio:.2f}x what drawing this tree costs at all "
        f"(budget {STEP_BUDGET:.1f}x), and coming back to a frame costs "
        f"{revisit.median:.1f} ms. A timeline cannot be quicker than the diagram "
        f"it is a timeline of; what it must not do is add a second cost on top, "
        f"or pay the first one twice."
    )


def _drawn(payload: dict[str, object]) -> None:
    """Fail loudly rather than timing an error page."""
    if payload.get("status") == "failed":
        raise SystemExit(f"a frame did not draw: {payload.get('message')}")


def _session(root: Path) -> EditingSession:
    return EditingSession(root=root / PREFIX)


def _repository(args: argparse.Namespace) -> Iterator[Path]:
    if args.inventory:
        yield Path(args.inventory).resolve()
        return
    target = Path(args.keep).resolve() if args.keep else Path(tempfile.mkdtemp(prefix="ng-hist-"))
    if args.keep and target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    shape = Shape(sites=args.sites, racks_per_site=args.racks, hosts_per_rack=args.hosts)
    try:
        build_repository(target, shape, commits=args.commits)
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
    parser.add_argument("--commits", type=int, default=COMMITS)
    parser.add_argument("--repeat", type=int, default=3, help="samples per stage (median wins)")
    parser.add_argument("--keep", help="build the repository here and leave it behind")
    parser.add_argument(
        "--inventory",
        help="time an existing repository; the inventory is expected at <path>/" + PREFIX,
    )
    args = parser.parse_args(argv)

    if shutil.which(DOT_EXECUTABLE) is None:
        print(f"{DOT_EXECUTABLE} is not on the PATH; a frame cannot be drawn without it")
        return 1
    if shutil.which("git") is None:
        print("git is not on the PATH; there is no history to read")
        return 1

    for root in _repository(args):
        report(root, repeat=args.repeat)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
