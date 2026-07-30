"""Regression guards on the cost of loading and of validating an inventory.

``load_tree`` is the stage every command pays for, and two rounds of work have
gone into it (entries 1 and 5 of ``docs/follow-ups.md``); ``validate`` is the
second cost, and entry 7 is the round that went into that. This file stops
either from being given back unnoticed.

**What is measured, and why it is a ratio.** A wall-clock ceiling in
milliseconds would be worthless here: a shared CI runner varies by more between
two runs of the same commit than any regression this guard is meant to catch,
so the threshold would have to be so generous that it caught nothing. What is
stable across machines is the *shape* of the work — so the test measures two
things in the same process, back to back, on the same tree:

``floor``
    Reading and parsing every document and throwing the result away:
    :func:`~netgraph.loader.documents.read_documents` over the whole tree. This
    is PyYAML's cost, which netgraph does not own.

``full``
    The same tree through :func:`~netgraph.loader.load_tree`: the parse, plus
    pydantic model validation, plus the loader's own bookkeeping.

``full / floor`` is then how much netgraph adds on top of parsing, expressed in
units of the parse itself. Machine speed cancels out of the ratio, and so does
most of the noise, because both halves are slow or fast together.

**The thresholds.** They are deliberately per-parser, because the denominator
differs by a factor of eight between the two:

=============  ===============  ==========  =========
Parser         Before entry 5   Today       Threshold
=============  ===============  ==========  =========
libyaml        1.78-1.79        1.58-1.60   1.70
pure Python    1.16             1.10-1.12   1.25
=============  ===============  ==========  =========

The libyaml row is the guard that does the work, and it was checked in both
directions: it passed on the commit that set it and failed on its parent,
quoting 1.79. The margin either side is real but not generous — 6 % of headroom
above today and 12 % below a full revert — which is the price of a guard sharp
enough to notice anything.

That sharpness is why the libyaml threshold is **1.70 on Linux and 1.95
elsewhere**, and the difference is not a concession, it is the premise failing.
Machine speed cancels out of a ratio only when both halves are the same kind of
work, and here they are not: the floor reads forty files and runs a C parser
over them, the numerator adds pydantic on top, and the balance between
filesystem and interpreter is the thing that differs most between an
ubuntu-24.04 runner and a windows-latest one. The same commit reads 1.58-1.60 on
Linux and 1.72 on Windows — *inside* the 1.60-to-1.79 band this row exists to
discriminate within. So the Linux copy stays sharp, and the other two get the
blunter one: an entry-5-sized regression is invisible to them, a catastrophic
one is not.

Every "today" figure here is from one machine and will not be yours. Each guard
prints its own on every run — ``[perf] validate: 8.27x against a budget of
9.50x (13% headroom)`` — so the number to recalibrate against can be read off
all six CI jobs rather than inferred from the one that happened to fail.

The pure-Python row is honestly weaker. With a parse eight times slower in the
denominator, the model layer would have to roughly triple before the ratio
moved as far as its threshold, so this row would *not* catch a revert of entry
5. It is kept because a catastrophic regression should not be invisible on the
fallback path, not because it is sharp.

**The same technique for** ``validate``. The floor there is not the parse — a
loaded inventory has already paid that — but a **plain walk over every interface
and every address**, which is the smallest pass any rule about addresses could
make. ``validate / walk`` is then how many walks' worth of work the whole rule
set does. Because the inventory is already in memory, the parser in use does not
enter either half, so one threshold covers both paths.

The walk is repeated ``FLOOR_WALKS`` times per sample. One walk is a tenth of a
millisecond on the guard's tree, which is small enough that the timer's own
noise moves the ratio by several per cent; eight of them cost enough to measure
cleanly, and the ratio only has to be compared with itself.

==============  ===============  ==========  =========
Measured        Before entry 7   Today       Threshold
==============  ===============  ==========  =========
validate/floor  21.5-22.0        8.2-8.6     9.5
==============  ===============  ==========  =========

The "today" range widened when the routing rules of §16 landed — not because
those rules cost anything (0.0 ms each by ``tools/profile_validate.py``, and
removing them from ``_CHECKS`` does not move the ratio) but because the context
they build did. Entry 12 of ``docs/follow-ups.md`` has those measurements.

Two things then kept the guard usable at that width, and both are worth knowing
before the threshold is touched again. Every measurement here runs with
coverage's line tracer **paused** (:func:`tracing_paused`), because it costs time
per line executed and so taxes a hundred small functions far more heavily than
one tight loop: instrumented, this ratio read half a point high and failed a CI
run at 9.07 against a threshold of 9.0. And the ratio is the best of
``SAMPLES`` rounds rather than of four, because the floor is the noisier half and
the minimum of a short measurement needs the attempts.

That guard is timed on a **freshly loaded** inventory each round, which matters
since entry 7: ``IPv4Address.network`` is now cached on the model, so a second
``validate`` over one inventory no longer does the work the first one did, and a
warm measurement would flatter every change in this area.

What it catches, measured by reverting each file of entry 7 on its own:

======================  =====  ========
Reverted                Ratio  Caught?
======================  =====  ========
all of entry 7          21.6   yes
``models/interface.py``  13.7  yes
``validate.py``           9.1  yes
``subnets.py``            7.5  no
======================  =====  ========

Those figures are against the ratio as it read when they were taken; each of the
three it catches is a multiple of today's 8.4, not a few per cent above it. It
does *not* catch the ``subnets.py`` piece, which is worth about 9 % of
``validate`` and so sits inside the 11 % of headroom the threshold leaves above
today's worst sample. Buying that last piece would mean a threshold within a few
per cent of the measured spread, which on a shared runner buys flakiness rather
than coverage — 9.0 was exactly that, and it failed a run that had regressed
nothing.

**And the same technique again for the cache.** Entry 14 made a repeated load
incremental; the floor there is the *cold* load itself, and the guard asserts that
a warm one is a small fraction of it — separately for the two tiers, because they
answer different questions ("the next process" and "the next cycle of one
``watch``"):

==============  ==========  =========
Measured        Today       Threshold
==============  ==========  =========
warm/cold disk  0.30-0.34   0.55
warm/cold mem   0.084-0.090 0.20
==============  ==========  =========

Both are the libyaml numbers; through the pure-Python parser the denominator is
five times larger and both ratios fall by a factor of five, so one threshold
covers each path. What this catches is the cache silently ceasing to be
consulted, which puts both ratios at 1.0.

If any of these tests starts failing on a platform without a code change behind
it, the right fix is to raise the number here *and say so in*
``docs/follow-ups.md`` — not to delete the guard.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import coverage
import pytest

from netgraph.loader import Inventory, load_tree
from netgraph.loader.cache import DocumentCache
from netgraph.loader.documents import HAVE_LIBYAML, StrictSafeLoader, read_documents
from netgraph.loader.tree import InventoryFile, iter_inventory_files
from netgraph.models import Adapter, Device
from netgraph.validate import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "tools" / "bench_pipeline.py"

#: ``full / floor`` ceilings, keyed by whether libyaml is the parser in use.
#: See the module docstring for where the numbers come from.
MAX_LOAD_RATIO_LIBYAML = 1.70
MAX_LOAD_RATIO_PURE_PYTHON = 1.25

#: What the libyaml ceiling becomes off the platform it was calibrated on.
#:
#: The premise of a ratio is that machine speed cancels out, and for the two
#: halves of the *validate* guard it does. It does not here, because the two
#: halves are not the same kind of work: the floor reads forty files and runs a C
#: parser over them, the numerator adds pydantic on top, and the balance between
#: filesystem and interpreter is exactly what differs most between an
#: ubuntu-24.04 runner and a windows-latest one. Measured 1.58-1.60 on Linux and
#: 1.72 on Windows, on the same commit — inside the 1.60-to-1.79 band this guard
#: exists to discriminate within, so on Windows it was not discriminating, it was
#: failing.
#:
#: Blunter off Linux, deliberately, and for the same reason the pure-Python row
#: is blunter: an entry-5-sized regression is not visible here, and a
#: catastrophic one still is. The sharp copy runs on all four Linux jobs.
MAX_LOAD_RATIO_LIBYAML_ELSEWHERE = 1.95

#: ``validate / address-walk floor`` ceiling. Parser-independent: both halves
#: run over an inventory that is already in memory. See the module docstring, and
#: entry 12 of ``docs/follow-ups.md`` for the history: 8.5, then 9.0 when the
#: routing model's context building landed, and now 9.5 over a measured 8.2-8.6.
#: A revert of any piece of entry 7's work lands at 9.1 against a *6.9* baseline,
#: which is 11.0 against this one, so the guard keeps what it is for.
MAX_VALIDATE_RATIO = 9.5

#: Ceilings on a *warm* load as a fraction of a cold one, for the two tiers of
#: the parse cache. Measured 0.30-0.34 (disk) and 0.084-0.090 (memory) through
#: libyaml, and 0.04-0.06 / 0.01-0.02 through the pure-Python parser, whose
#: denominator is five times larger; so one pair of thresholds covers both paths
#: and each leaves at least 60 % of headroom. Coverage *helps* here — the warm
#: path executes far fewer traced lines than the parser does — which is why these
#: two are not subject to the caveat in entry 12 of ``docs/follow-ups.md``.
#:
#: What they catch is the cache quietly ceasing to be consulted, which would put
#: both at 1.0. See entry 14 for the full table.
MAX_WARM_DISK_FRACTION = 0.55
MAX_WARM_MEMORY_FRACTION = 0.20

#: Walks per floor sample. One is too short to time cleanly; see the docstring.
FLOOR_WALKS = 8

#: How many rounds the two halves are timed for. The *minimum* of each is
#: taken, not the mean: a timing sample is bounded below by the real cost and
#: unbounded above by whatever else the machine was doing, so the smallest
#: sample is the least contaminated one — which is an argument for taking more of
#: them, since a minimum only gets closer to the truth. Eight rather than four
#: narrowed the validate ratio's spread from 8.06-8.56 to 8.17-8.56 on the
#: machine this was measured on, and the whole file still runs in five seconds.
SAMPLES = 8


def load_harness() -> ModuleType:
    """Import ``tools/bench_pipeline.py`` as a module.

    The tree is generated by the same code that produces the numbers in
    ``docs/follow-ups.md``; a guard measured on a differently-shaped inventory
    could not be compared with the table it is guarding.
    """
    name = "bench_pipeline"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def benchmark_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scaled-down copy of the benchmark inventory: 80 devices, 158 documents.

    Small enough that the suite does not slow down noticeably even on the
    pure-Python parser, large enough that the fixed costs of the walk itself
    (one ``scandir`` per directory) do not dominate the ratio.
    """
    harness = load_harness()
    root = tmp_path_factory.mktemp("benchmark-inventory")
    shape = harness.Shape(sites=2, racks_per_site=3, hosts_per_rack=12)
    harness.generate(root, shape)
    return root


@contextmanager
def tracing_paused() -> Iterator[None]:
    """Stop coverage's line tracer for the duration of a measurement.

    Every ratio in this file compares two pieces of netgraph against each other
    so that the machine cancels out. Coverage breaks that: it costs time *per
    line executed*, so it taxes a hundred small functions far more heavily than
    one tight loop, and the two halves of a ratio are rarely shaped alike. The
    validate guard is the extreme case — 8.0 uninstrumented, 8.5 to 8.7 under
    the tracer, and 9.07 on a CI runner having a bad minute, against a threshold
    of 9.0. Entry 12 of ``docs/follow-ups.md`` recorded that and named this as
    the honest fix.

    Coverage is on by default in ``pyproject.toml``, so this is what the numbers
    in the module docstring were measured under, and what they now mean. The
    handful of lines that go untraced are `validate` and the loader, which
    several hundred other tests execute.

    A no-op when coverage is not running, which is what ``--no-cov`` gives.
    """
    active = coverage.Coverage.current()
    if active is None:
        yield
        return
    active.stop()
    try:
        yield
    finally:
        active.start()


def milliseconds(call: Callable[[], object]) -> float:
    """Time one call, in milliseconds, with the line tracer out of the way."""
    with tracing_paused():
        start = time.perf_counter()
        call()
        return (time.perf_counter() - start) * 1000


def report(capsys: pytest.CaptureFixture[str], name: str, *, ratio: float, budget: float) -> None:
    """Print what a guard measured, whether or not it passed.

    Every threshold in this file has been recalibrated at least once, and each
    time the only numbers to hand were from the run that *failed* — so "how much
    headroom did we actually have" could only be answered by the sample that had
    none. Printed on every run, on every platform, so the next recalibration
    reads a spread off three runners rather than off one bad minute on one of
    them.

    Inside ``capsys.disabled()``, which is the whole point: pytest captures
    everything a test writes and shows it only when the test fails, so the first
    version of this printed on exactly the runs it was written to stop being the
    only source of data. ``record_property`` was the other candidate, and pytest
    9 warns about it under the ``xunit2`` junit family it also defaults to.
    """
    headroom = (budget - ratio) / budget * 100
    with capsys.disabled():
        print(
            f"\n[perf] {name}: {ratio:.2f}x against a budget of {budget:.2f}x "
            f"({headroom:.0f}% headroom)"
        )


def interleaved_best(
    first: Callable[[], object], second: Callable[[], object], *, samples: int = SAMPLES
) -> tuple[float, float]:
    """Time both calls once per round, ``samples`` rounds; return the two minima.

    Interleaved rather than one after the other, because the two numbers are
    only compared with each other: if the machine stalls for a moment, a round
    that measures both halves during the stall inflates both and the ratio
    survives, where timing all of one and then all of the other would inflate
    just one of them and produce a spurious failure.
    """
    firsts, seconds = [], []
    for _ in range(samples):
        firsts.append(milliseconds(first))
        seconds.append(milliseconds(second))
    return min(firsts), min(seconds)


def parse_floor(files: list[InventoryFile]) -> None:
    """Parse every document and discard it: the cost netgraph does not own."""
    for entry in files:
        for _ in read_documents(entry.path, relative=entry.relative):
            pass


def address_walk_floor(inventory: Inventory) -> int:
    """Visit every interface and every address ``FLOOR_WALKS`` times over, and
    derive nothing.

    The floor for ``validate``: five of its rules are statements about
    addresses, so none of them can cost less than one of these passes, and
    anything the ratio measures above the floor is work netgraph chose to do.
    Deliberately touches no :mod:`ipaddress` object — a floor that warmed the
    caches entry 7 added would move whenever they did.
    """
    seen = 0
    for _ in range(FLOOR_WALKS):
        for element in inventory.elements.values():
            if not isinstance(element, (Device, Adapter)):
                continue
            for interface in element.interfaces:
                for address in interface.addresses():
                    seen += address.prefix_length
    return seen


def test_the_generated_tree_is_the_shape_the_guard_assumes(benchmark_tree: Path) -> None:
    """A guard on a tree that silently shrank to nothing would pass forever."""
    inventory = load_tree(benchmark_tree)

    assert not inventory.errors, inventory.errors[:3]
    assert len(inventory) == 158
    assert len(inventory.devices) == 80


def test_loading_costs_no_more_than_its_budget_above_the_parse(
    benchmark_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``load_tree`` stays within its documented multiple of the raw parse."""
    files = iter_inventory_files(benchmark_tree)

    def parse() -> None:
        parse_floor(files)

    def load() -> None:
        load_tree(benchmark_tree)

    # The tree was written moments ago, so the first pass over it also pays for
    # a cold page cache and for every lazily-built pydantic validator. Warm
    # both halves before anything is timed.
    for _ in range(2):
        parse()
        load()

    floor, full = interleaved_best(parse, load)
    ratio = full / floor

    libyaml_in_use = HAVE_LIBYAML and StrictSafeLoader.__name__ == "CStrictSafeLoader"
    if not libyaml_in_use:
        budget = MAX_LOAD_RATIO_PURE_PYTHON
    elif sys.platform.startswith("linux"):
        budget = MAX_LOAD_RATIO_LIBYAML
    else:
        budget = MAX_LOAD_RATIO_LIBYAML_ELSEWHERE
    report(capsys, "load", ratio=ratio, budget=budget)

    assert ratio <= budget, (
        f"load_tree is {ratio:.2f}x the raw parse ({full:.1f} ms against {floor:.1f} ms) "
        f"through {StrictSafeLoader.__name__}, over the budget of {budget:.2f}x. Either "
        f"an optimisation from docs/follow-ups.md was undone, or new per-document work "
        f"was added to the loader or the models. Profile with 'python tools/bench_pipeline.py' "
        f"before changing this threshold."
    )


def test_validating_costs_no_more_than_its_budget_above_an_address_walk(
    benchmark_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``validate`` stays within its documented multiple of the walk floor."""
    # Warm the lazily-built pydantic validators and the page cache before
    # anything is timed, exactly as the load guard does.
    for _ in range(2):
        address_walk_floor(load_tree(benchmark_tree))

    floors: list[float] = []
    fulls: list[float] = []
    for _ in range(SAMPLES):
        # A fresh inventory per round: entry 7 caches each address's prefix on
        # the model, so the second validate over one inventory is not the
        # measurement anybody's `netgraph validate` pays for. The walk is timed
        # first because it is the half that must stay cold-independent.
        inventory = load_tree(benchmark_tree)
        floors.append(milliseconds(lambda tree=inventory: address_walk_floor(tree)))
        fulls.append(milliseconds(lambda tree=inventory: validate(tree)))

    floor, full = min(floors), min(fulls)
    ratio = full / floor
    report(capsys, "validate", ratio=ratio, budget=MAX_VALIDATE_RATIO)

    assert ratio <= MAX_VALIDATE_RATIO, (
        f"validate is {ratio:.2f}x the address-walk floor "
        f"({full:.1f} ms against {floor:.1f} ms), over the budget of "
        f"{MAX_VALIDATE_RATIO:.1f}x. Either an optimisation from entry 7 of "
        f"docs/follow-ups.md was undone, or a rule grew per-address work. Profile with "
        f"'python tools/profile_validate.py' before changing this threshold."
    )


def test_a_warm_load_costs_a_fraction_of_a_cold_one(benchmark_tree: Path, tmp_path: Path) -> None:
    """The cache actually skips the parse, on disk and in memory both.

    Same technique as the two guards above: two things timed in the same process
    against each other, so the machine cancels out. The floor here *is* the cold
    load, which makes this the one ratio in the file where a number below the
    threshold is the passing case.

    A fresh :class:`DocumentCache` per disk sample, because a store that has
    already answered would answer from memory the second time and the two tiers
    would stop being separable.
    """
    directory = tmp_path / "cache"
    warm_store = DocumentCache(directory)

    def cold() -> None:
        load_tree(benchmark_tree)

    def from_disk() -> None:
        load_tree(benchmark_tree, cache=DocumentCache(directory))

    def from_memory() -> None:
        load_tree(benchmark_tree, cache=warm_store)

    # Fill the cache, then warm both paths: the first read of an entry pays for a
    # cold page cache, and the first load of any kind builds pydantic's
    # validators.
    for _ in range(2):
        cold()
        from_disk()
        from_memory()

    cold_ms, disk_ms = interleaved_best(cold, from_disk)
    _, memory_ms = interleaved_best(cold, from_memory)

    assert disk_ms / cold_ms <= MAX_WARM_DISK_FRACTION, (
        f"a warm load off disk is {disk_ms / cold_ms:.2f} of a cold one "
        f"({disk_ms:.1f} ms against {cold_ms:.1f} ms), over the budget of "
        f"{MAX_WARM_DISK_FRACTION:.2f}. Either the cache stopped being consulted, or "
        f"reconstructing the models grew more expensive than parsing them. Measure with "
        f"'python tools/bench_incremental.py' before changing this threshold."
    )
    assert memory_ms / cold_ms <= MAX_WARM_MEMORY_FRACTION, (
        f"a second load in one process is {memory_ms / cold_ms:.2f} of a cold one "
        f"({memory_ms:.1f} ms against {cold_ms:.1f} ms), over the budget of "
        f"{MAX_WARM_MEMORY_FRACTION:.2f}. This is the tier 'netgraph watch' reloads "
        f"through; see entry 14 of docs/follow-ups.md."
    )
