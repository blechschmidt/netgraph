"""Regression guards on what one edit in ``netviz web`` costs.

``tests/test_performance.py`` guards the two stages every command pays for,
loading and validating. This file guards the *editor*: the loop between a person
changing one field and the diagram agreeing with them again. Four rounds of work
went into that loop (entry 20 of ``docs/follow-ups.md``), and every one of them
is the kind of thing that comes back silently — an extra ``load_tree`` inside a
write path, an extra ``validate`` inside a request handler, a payload that grows
with the inventory rather than with what changed.

**Counted, not timed, wherever counting will do.** The guards in
``test_performance.py`` are ratios because the things they compare are both
wall-clock and there is nothing else to compare. Here there usually is something
better. "How many files does one edit parse" and "how many times does one edit
validate the tree" are integers; they are the same integers on a laptop and on a
loaded shared runner; and they fail loudly on exactly the regression that
matters rather than on a bad minute. A threshold that cannot flake is worth more
than one that is merely tight.

What is guarded, and what each catches
--------------------------------------

===========================  ============  ==========  ==================================
Guard                        Before        Today       Catches
===========================  ============  ==========  ==================================
files parsed per edit        3 x the tree  1           the parse cache dropped from the
                                                       write path
``validate`` calls per edit  4             2           a request handler validating for
                                                       itself instead of asking the
                                                       session
problems per answer          every one     <= 200      the payload growing with the
                                                       inventory
growth of the separation      16x per 4x   4-5x per   the separation pass going back to
pass                                       4x          comparing everything with
                                                       everything
partial layout / auto        86x           0.8-3.6x    the probe run for a partial
                                                       arrangement being asked to route
                                                       edges it then discards
===========================  ============  ==========  ==================================

The last one is the only ratio, and it is a ratio for the usual reason: both
halves are a Graphviz run over the same graph on the same machine, so the
machine cancels out. It is also the one with real headroom, because it is the
one whose measurement is not an integer. Across the sizes entry 20 measured:

======  ========  ==============  =====
Nodes   ``dot``   50 pinned       Ratio
======  ========  ==============  =====
19       48 ms      38 ms         0.8x
68       59 ms     136 ms         2.3x
198      96 ms     318 ms         3.3x
412     191 ms     682 ms         3.6x
1056    710 ms   2 119 ms         3.0x
======  ========  ==============  =====

It was 86x on the last row. Guarded at 6x: comfortably above the spread, and
nowhere near the regression it exists to catch.

If one of these starts failing without a change behind it, raise the number
*and say so in* ``docs/follow-ups.md`` — do not delete the guard.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest

from netviz.edit import session as edit_session
from netviz.layout import graphviz as layout_graphviz
from netviz.layout.geometry import Placement
from netviz.loader import load_tree
from netviz.loader.cache import DocumentCache
from netviz.render import build_graph, filter_graph
from netviz.render.dot import to_image
from netviz.web import session as web_session
from netviz.web.preview import MAX_PROBLEMS, ViewOptions
from netviz.web.session import EditingSession

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path
from test_performance import (  # isort: skip -- the harness loader and the timer
    load_harness,
    milliseconds,
    tracing_paused,
)

#: How many files one ``set`` on one field may parse.
#:
#: One: the file it changed. Everything else in the tree is byte-for-byte what
#: the cache already holds. It was three times the whole tree — the write path
#: loads the inventory three times to judge an edit (the baseline, the tree
#: between operations, the tree the gate compares against) and passed the cache
#: to none of the overlaid loads.
MAX_PARSES_PER_EDIT: Final = 1

#: How many times one edit may run the validator, counted across both modules
#: that call it on this path.
#:
#: Three, and all three are over different trees. The write path grades the tree
#: as it *is* and as it *would be*, because the gate is "does this edit make
#: anything worse" and that is a comparison; the session then grades the tree it
#: wrote, to report on it.
#:
#: What is not allowed back is the fourth and fifth — the file-list handler and
#: the diagram handler each grading the same objects a third and fourth time,
#: which is what :meth:`EditingSession.findings` exists to prevent, and which is
#: what the round trip below actually exercises.
MAX_VALIDATIONS_PER_EDIT: Final = 3

#: What a partially-arranged layout may cost, as a multiple of laying the same
#: graph out from nothing. See entry 20; it was 86x, and is 0.8x to 3.6x across
#: every size measured there.
MAX_PARTIAL_LAYOUT_RATIO: Final = 6.0

#: How much the separation pass's comparison count may grow when the drawing
#: quadruples in size.
#:
#: Two boxes can only overlap if they are within one box of each other, so the
#: work grows with the number of *neighbours* — which, at a fixed density, means
#: linearly in the number of nodes. Six for a fourfold increase leaves room for
#: the extra sweeps a bigger overlapping lattice needs to settle, and is a long
#: way below the sixteen an exhaustive pairwise sweep costs.
MAX_SEPARATION_GROWTH: Final = 6.0


@pytest.fixture(scope="module")
def editor_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An inventory big enough for these guards to mean something.

    The same generator ``tools/bench_editor.py`` uses, at a size the suite can
    afford: 200 devices in about 400 documents. Every guard here is a count or a
    ratio, so the size only has to be large enough that the thing being counted
    is not swamped by fixed costs.
    """
    harness = load_harness()
    root = tmp_path_factory.mktemp("editor-inventory")
    harness.generate(root, harness.Shape(sites=3, racks_per_site=4, hosts_per_rack=15))
    return root


@pytest.fixture
def editor(editor_tree: Path, tmp_path: Path) -> EditingSession:
    """A writable session over it, cache warmed, as ``netviz web --write`` builds one."""
    session = EditingSession(
        root=editor_tree, writable=True, cache=DocumentCache(tmp_path / "cache")
    )
    session.inventory()  # fill the cache, which is what a running editor has done
    return session


def a_device(session: EditingSession) -> str:
    addresses = sorted(
        address
        for address, element in session.inventory().elements.items()
        if element.kind in {"switch", "router", "computer", "server"}
    )
    return addresses[len(addresses) // 2]


@contextmanager
def counting(target: Any, name: str) -> Iterator[list[int]]:
    """Count calls to ``target.name`` for the duration, without changing what it does."""
    tally = [0]
    original = getattr(target, name)

    def counted(*arguments: Any, **keywords: Any) -> Any:
        tally[0] += 1
        return original(*arguments, **keywords)

    setattr(target, name, counted)
    try:
        yield tally
    finally:
        setattr(target, name, original)


def note(capsys: pytest.CaptureFixture[str], line: str) -> None:
    """Print a figure whether or not the guard passed; see ``test_performance.report``."""
    with capsys.disabled():
        print(f"\n[perf] {line}")


# --------------------------------------------------------------------------- #
# The write path
# --------------------------------------------------------------------------- #


def test_the_generated_tree_is_the_shape_these_guards_assume(editor_tree: Path) -> None:
    """A guard on a tree that silently shrank to nothing would pass forever."""
    inventory = load_tree(editor_tree)
    assert not inventory.errors, inventory.errors[:3]
    assert len(inventory.devices) == 195
    assert len(inventory) == 387


def test_one_edit_parses_only_the_file_it_changed(
    editor: EditingSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parse cache reaches the write path, not just the read path.

    Counted through the cache's own miss counter, which is exactly "a file whose
    bytes this process has not seen": the edited file, and nothing else. Before
    the fix this read three times the number of files in the tree, because
    ``EditSession`` loaded the inventory three times per batch and passed the
    cache to none of the overlaid loads.
    """
    address = a_device(editor)
    editor.apply([{"op": "set", "address": address, "path": "metadata.description", "value": "a"}])
    assert editor.cache is not None
    before = editor.cache.stats.misses

    editor.apply([{"op": "set", "address": address, "path": "metadata.description", "value": "b"}])
    parsed = editor.cache.stats.misses - before

    files = len(list(editor_files(editor)))
    note(capsys, f"parses per edit: {parsed} of {files} files (budget {MAX_PARSES_PER_EDIT})")
    assert parsed <= MAX_PARSES_PER_EDIT, (
        f"one edit parsed {parsed} files out of {files}; at most "
        f"{MAX_PARSES_PER_EDIT} should be new bytes. Either EditSession stopped passing "
        f"its cache to load_tree, or a load was added that does not. See entry 20 of "
        f"docs/follow-ups.md."
    )


def editor_files(session: EditingSession) -> Iterator[Path]:
    yield from sorted(session.root.rglob("*.yaml"))


def test_one_edit_validates_the_tree_three_times_and_no_more(
    editor: EditingSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole round trip an edit produces, counted at the validator.

    Not just the write: the file-list fetch and the diagram fetch the page makes
    straight afterwards are part of the same edit as far as the person doing it
    is concerned, and each of them used to grade the tree for itself.
    """
    address = a_device(editor)
    view = ViewOptions()
    editor.graph(view)  # the page is already showing something

    with counting(web_session, "validate") as reads, counting(edit_session, "validate") as writes:
        editor.apply(
            [{"op": "set", "address": address, "path": "metadata.description", "value": "c"}]
        )
        editor.tree(diagnostics=True)
        editor.graph(view)
    tally = [reads[0] + writes[0]]

    note(
        capsys,
        f"validations per edit: {tally[0]} ({writes[0]} in the write path, "
        f"{reads[0]} answering the page) against a budget of {MAX_VALIDATIONS_PER_EDIT}",
    )
    assert tally[0] <= MAX_VALIDATIONS_PER_EDIT, (
        f"one edit ran the validator {tally[0]} times over the same tree; at most "
        f"{MAX_VALIDATIONS_PER_EDIT} are load-bearing. Something asked for findings "
        f"without going through EditingSession.findings. See entry 20 of "
        f"docs/follow-ups.md."
    )


# --------------------------------------------------------------------------- #
# What goes over the wire
# --------------------------------------------------------------------------- #


def test_an_answer_carries_a_bounded_number_of_problems(
    editor: EditingSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """The payload is bounded by the cap, not by the size of the inventory.

    An inventory of this size reports a finding per device from one purely
    informational rule. Sending every one of them on every answer — the write's,
    the file list's and the diagram's alike — was half a megabyte per keystroke
    and that many DOM rows rebuilt each time.
    """
    found = len(editor.diagnostics())
    assert found > MAX_PROBLEMS, (
        f"this tree reports only {found} problems, which is under the cap of "
        f"{MAX_PROBLEMS}, so this guard would pass without the cap existing"
    )

    tree = editor.tree(diagnostics=True)
    preview, _ = editor.graph(ViewOptions())
    drawn = preview.to_dict()

    note(
        capsys,
        f"problems: {found} found, {len(tree['diagnostics'])} in the file list, "
        f"{len(drawn['problems'])} in the diagram (cap {MAX_PROBLEMS})",
    )
    for name, rows, omitted in (
        ("the file list", tree["diagnostics"], tree["diagnosticsOmitted"]),
        ("the diagram", drawn["problems"], drawn["problemsOmitted"]),
    ):
        assert len(rows) <= MAX_PROBLEMS, f"{name} carried {len(rows)} problems"
        # And says so, because a list that quietly stopped at the cap would read
        # as an inventory with exactly that many problems.
        assert len(rows) + omitted == found, f"{name} lost count of what it left out"

    # The whole answer, serialised, as the browser receives it.
    bytes_ = len(json.dumps(tree))
    note(capsys, f"the file list is {bytes_ / 1000:.0f} kB with {found} problems found")


# --------------------------------------------------------------------------- #
# Drawing an arrangement somebody is halfway through making
# --------------------------------------------------------------------------- #


def arrange(session: EditingSession, count: int) -> None:
    """Place ``count`` nodes by hand, which is what dragging a selection writes."""
    addresses = sorted(
        address
        for address, element in session.inventory().elements.items()
        if element.kind != "cable"
    )[:count]
    session.apply(
        [
            {
                "op": "set-geometry",
                "view": "l1",
                "nodes": {
                    address: {"position": {"x": 100 + (index % 20) * 90, "y": 100 + index * 4}}
                    for index, address in enumerate(addresses)
                },
            }
        ]
    )


def grid_of(count: int) -> dict[str, Placement]:
    """``count`` boxes on a lattice tight enough that neighbours really do collide.

    Deliberately the hard case. A drawing whose nodes are already clear of each
    other separates in one pass and would pass this guard whatever the sweep
    did; this one makes every node overlap the four around it, so the pass has
    real work to do and the only question is how much of the *rest* of the
    drawing it looks at while doing it.
    """
    side = int(count**0.5) + 1
    return {
        f"n{index:04d}": Placement(
            x=(index % side) * 60.0, y=(index // side) * 40.0, width=54.0, height=36.0
        )
        for index in range(count)
    }


def comparisons(nodes: dict[str, Placement]) -> int:
    with counting(layout_graphviz, "_overlap") as tally:
        layout_graphviz.separate(nodes, fixed=set(list(nodes)[: len(nodes) // 12]))
    return tally[0]


def test_separating_a_partial_arrangement_is_not_quadratic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The separation pass compares neighbours, not everything with everything.

    Counted at ``_overlap``, which is the comparison, and asserted as a *shape*
    rather than as a constant: four times the nodes, at the same density, must
    not cost sixteen times the comparisons. The constant is not the interesting
    part and it depends on how badly the arrangement overlaps; the exponent is
    the thing that made a partially-arranged redraw of the benchmark tree spend
    4.7 seconds in thirteen million calls to this one function.
    """
    small, large = 400, 1600
    few, many = comparisons(grid_of(small)), comparisons(grid_of(large))

    growth = many / few
    quadratic = (large / small) ** 2
    note(
        capsys,
        f"separation: {small} nodes cost {few} comparisons, {large} cost {many} — "
        f"{growth:.1f}x for {large // small}x the nodes "
        f"(budget {MAX_SEPARATION_GROWTH:.1f}x, quadratic would be {quadratic:.0f}x)",
    )
    assert growth <= MAX_SEPARATION_GROWTH, (
        f"quadrupling the drawing multiplied the separation pass's comparisons by "
        f"{growth:.1f}, over the budget of {MAX_SEPARATION_GROWTH:.1f}; a sweep over every "
        f"pair would be {quadratic:.0f}. The grid in netviz.layout.graphviz._Grid has "
        f"stopped narrowing the candidates. See entry 20 of docs/follow-ups.md."
    )


@requires_dot
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Graphviz timings on the Windows runner are too noisy for a ratio",
)
def test_a_half_finished_arrangement_costs_a_few_auto_layouts(
    editor: EditingSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drawing a partly-placed diagram stays within a few times drawing an unplaced one.

    The one ratio here, and it is one for the usual reason: both halves are
    Graphviz over the same graph in the same process, so the machine cancels out.
    It was 86x on the benchmark tree — drag one node and every redraw took a
    minute — because ``neato``'s spline router is superlinear on nodes it did not
    place, and because the separation pass above was quadratic.
    """
    options = ViewOptions()

    def draw() -> None:
        inventory = load_tree(editor.root)
        graph = filter_graph(build_graph(inventory, layer=options.layer), options.filter_spec)
        to_image(graph, options.render_options, format="svg")

    with tracing_paused():
        draw()  # warm Graphviz's font list and the page cache
    auto = min(milliseconds(draw) for _ in range(3))

    arrange(editor, 50)
    partial = min(milliseconds(draw) for _ in range(3))

    ratio = partial / auto
    headroom = (MAX_PARTIAL_LAYOUT_RATIO - ratio) / MAX_PARTIAL_LAYOUT_RATIO * 100
    note(
        capsys,
        f"partial layout: {ratio:.2f}x the auto layout ({partial:.0f} ms against "
        f"{auto:.0f} ms) against a budget of {MAX_PARTIAL_LAYOUT_RATIO:.1f}x "
        f"({headroom:.0f}% headroom)",
    )
    assert ratio <= MAX_PARTIAL_LAYOUT_RATIO, (
        f"a partly-arranged drawing costs {ratio:.2f}x an unarranged one "
        f"({partial:.0f} ms against {auto:.0f} ms), over the budget of "
        f"{MAX_PARTIAL_LAYOUT_RATIO:.1f}x. Either netviz.render.dot.unroutable stopped "
        f"holding the spline router back, or netviz.layout.graphviz.separate went "
        f"quadratic again. Measure with 'python tools/bench_editor.py' before changing "
        f"this threshold; see entry 20 of docs/follow-ups.md."
    )
