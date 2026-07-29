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
libyaml        1.78-1.79        1.52-1.53   1.70
pure Python    1.16             1.10-1.12   1.25
=============  ===============  ==========  =========

The libyaml row is the guard that does the work, and it was checked in both
directions: it passes on this commit and fails on its parent, quoting 1.79.
The margin either side is real but not generous — roughly 11 % of headroom
above today and 5 % below a full revert — which is the price of a guard sharp
enough to notice anything.

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
validate/floor  21.5-22.0        6.9-7.2     8.5
==============  ===============  ==========  =========

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

So the guard is honest rather than complete: it catches a full revert with
2.5x to spare and each of the two large pieces on its own, and it does *not*
catch the ``subnets.py`` piece, which is worth about 9 % of ``validate`` — under
the 17 % of headroom the threshold leaves above today's worst sample. Buying
that last piece would mean a threshold within 4 % of the measured spread, which
on a shared runner buys flakiness rather than coverage.

If either test starts failing on a platform without a code change behind it, the
right fix is to raise the number here *and say so in* ``docs/follow-ups.md`` —
not to delete the guard.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from netgraph.loader import Inventory, load_tree
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

#: ``validate / address-walk floor`` ceiling. Parser-independent: both halves
#: run over an inventory that is already in memory. See the module docstring.
MAX_VALIDATE_RATIO = 8.5

#: Walks per floor sample. One is too short to time cleanly; see the docstring.
FLOOR_WALKS = 8

#: How many rounds the two halves are timed for. The *minimum* of each is
#: taken, not the mean: a timing sample is bounded below by the real cost and
#: unbounded above by whatever else the machine was doing, so the smallest
#: sample is the least contaminated one.
SAMPLES = 4


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


def milliseconds(call: Callable[[], object]) -> float:
    """Time one call, in milliseconds."""
    start = time.perf_counter()
    call()
    return (time.perf_counter() - start) * 1000


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


def test_loading_costs_no_more_than_its_budget_above_the_parse(benchmark_tree: Path) -> None:
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
    budget = MAX_LOAD_RATIO_LIBYAML if libyaml_in_use else MAX_LOAD_RATIO_PURE_PYTHON

    assert ratio <= budget, (
        f"load_tree is {ratio:.2f}x the raw parse ({full:.1f} ms against {floor:.1f} ms) "
        f"through {StrictSafeLoader.__name__}, over the budget of {budget:.2f}x. Either "
        f"an optimisation from docs/follow-ups.md was undone, or new per-document work "
        f"was added to the loader or the models. Profile with 'python tools/bench_pipeline.py' "
        f"before changing this threshold."
    )


def test_validating_costs_no_more_than_its_budget_above_an_address_walk(
    benchmark_tree: Path,
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

    assert ratio <= MAX_VALIDATE_RATIO, (
        f"validate is {ratio:.2f}x the address-walk floor "
        f"({full:.1f} ms against {floor:.1f} ms), over the budget of "
        f"{MAX_VALIDATE_RATIO:.1f}x. Either an optimisation from entry 7 of "
        f"docs/follow-ups.md was undone, or a rule grew per-address work. Profile with "
        f"'python tools/profile_validate.py' before changing this threshold."
    )
