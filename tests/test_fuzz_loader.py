"""Fuzzing the untrusted edge: the loader.

Everything else in netviz reads a tree the loader has already accepted. The
loader reads whatever is on disk — and increasingly that is not a file anybody
wrote: ``netviz import`` emits one from captured device output, a colleague
sends an inventory from a different tool, a pipeline generates one. So the
loader is the only component with a genuine trust boundary, and the contract at
that boundary is not "parses correct input" but:

1. **It terminates.** No input makes it loop, and none makes it spend
   super-linear time on a linear amount of text.
2. **It fails structurally.** Every rejection is a
   :class:`~netviz.errors.NetvizError` or a recorded
   :class:`~netviz.loader.LoadError` naming the file — never a traceback out
   of PyYAML, pydantic, :mod:`ipaddress` or a bare ``KeyError``.
3. **It stays bounded.** A diagnostic quotes back at most
   :data:`~netviz.errors.MAX_ECHOED_VALUE_LENGTH` characters of the value it
   rejected (follow-up 3), so a 200 000-character scalar cannot become a
   200 000-character error line — or a terminal full of escape sequences.
4. **It stays cheap.** A pathological document costs bounded memory, so
   ``[1-99999999]`` is a diagnostic rather than an out-of-memory kill.

The corpus in ``tests/fuzz-corpus/`` is the seed set: one file per way of being
wrong. The properties below mutate it — truncating, splicing, repeating,
re-indenting, wrapping in more nesting — and Hypothesis keeps whatever it finds
interesting in ``.hypothesis/examples``. ``docs/testing.md`` says how to run a
longer budget than CI's.
"""

from __future__ import annotations

import re
import time
import tracemalloc
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from netviz.errors import MAX_ECHOED_VALUE_LENGTH, NetvizError
from netviz.loader import Inventory, LoadError, load_stream, load_tree
from netviz.loader.documents import MAX_NESTING_DEPTH

CORPUS_DIR: Final = Path(__file__).resolve().parent / "fuzz-corpus"

#: Every seed, read as text. Sorted so a failure names the same case on every
#: machine, and collected at import time so each seed is its own test id.
CORPUS: Final[dict[str, str]] = {
    path.name: path.read_text(encoding="utf-8") for path in sorted(CORPUS_DIR.glob("*.yaml"))
}

#: Ceiling on one diagnostic. The longest legal value netviz accepts is a
#: 253-character name and a diagnostic may quote two of them plus prose, so this
#: is generous — it exists to catch an *unbounded* echo, not to police wording.
MAX_DIAGNOSTIC_LENGTH: Final = 4096

#: How long the whole corpus may take. Every seed is a few kilobytes at most;
#: this is a hang detector, not a benchmark.
TIME_BUDGET_SECONDS: Final = 20.0

#: How much a single pathological document may cost. A 200 000-character scalar
#: is 200 KB of input; anything that turns that into tens of megabytes is
#: amplifying, which is the property this bounds.
MEMORY_BUDGET_BYTES: Final = 96 * 1024 * 1024


def searched(floor: int) -> int:
    """At least ``floor`` examples, and more when the profile asks for more.

    The inverse of ``tests/test_properties.py``'s ``capped``. Mutating a corpus
    seed and handing it to the loader costs a few milliseconds, so the cheapest
    profile can afford far more of it than it can of an inventory property —
    and a fuzz pass of twenty-five examples would be a fuzz pass in name only.
    An explicit ``max_examples`` *replaces* the profile's rather than combining
    with it, so the floor has to be taken here.
    """
    return max(int(settings().max_examples), floor)


def test_the_corpus_is_not_empty() -> None:
    """A glob that quietly matched nothing would make every property below vacuous."""
    assert len(CORPUS) >= 20, sorted(CORPUS)


# --------------------------------------------------------------------------- #
# What "handled cleanly" means
# --------------------------------------------------------------------------- #


def load_bytes(payload: bytes, tmp_path: Path) -> Inventory:
    """Put ``payload`` on disk as an inventory and load it.

    Through the filesystem rather than through :func:`load_stream`, because that
    is the path a user's input takes and it is the one with the extra failure
    modes: an undecodable file, a file that disappears, a directory that is not
    readable.
    """
    (tmp_path / "document.yaml").write_bytes(payload)
    return load_tree(tmp_path)


def assert_handled(inventory: Inventory) -> None:
    """The loader came back, and everything it refused it refused in writing."""
    for error in inventory.errors:
        assert isinstance(error, LoadError)
        assert error.message, "a diagnostic with no message says nothing"
        assert error.path is not None
        text = str(error)
        assert len(text) <= MAX_DIAGNOSTIC_LENGTH, f"{len(text)}-character diagnostic: {text[:200]}"


#: A run of one repeated character, which is what every oversized value in the
#: corpus is made of. Long enough that no legal value and no piece of prose can
#: reach it, short enough to catch an echo that forgot to clip.
_RUN = re.compile(r"(.)\1{" + str(MAX_ECHOED_VALUE_LENGTH + 40) + r",}")


def assert_bounded(inventory: Inventory) -> None:
    """No diagnostic echoes an unbounded amount of the document back."""
    for error in inventory.errors:
        match = _RUN.search(str(error))
        assert match is None, (
            f"a diagnostic echoed {len(match.group(0))} repeats of "
            f"{match.group(1)!r}; {MAX_ECHOED_VALUE_LENGTH} is the bound"
        )


# --------------------------------------------------------------------------- #
# The corpus itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(CORPUS), ids=sorted(CORPUS))
def test_every_corpus_seed_is_rejected_cleanly(name: str, tmp_path: Path) -> None:
    """One test per seed, so a regression names the file that caused it."""
    start = time.monotonic()
    inventory = load_bytes(CORPUS[name].encode("utf-8"), tmp_path)
    elapsed = time.monotonic() - start

    assert_handled(inventory)
    assert_bounded(inventory)
    assert elapsed < TIME_BUDGET_SECONDS, f"{name} took {elapsed:.1f}s"


#: Seeds that are not merely awkward but must actually be *refused*: valid YAML
#: netviz has to say no to, or text that is not YAML at all. Listed rather
#: than inferred, so a seed that silently starts loading is noticed.
MUST_FAIL: Final[frozenset[str]] = frozenset(
    {
        "apiversion-wrong.yaml",
        "bare-text.yaml",
        "binary-tag.yaml",
        "cable-endpoint-without-colon.yaml",
        "cable-with-one-endpoint.yaml",
        "deep-flow-nesting-huge.yaml",
        "duplicate-keys.yaml",
        "huge-integer-digit-limit.yaml",
        "patchpanel-digit-limit.yaml",
        "prefix-length-digit-limit.yaml",
        "range-digit-limit.yaml",
        "every-field-wrong-type.yaml",
        "interface-fields-wrong-type.yaml",
        "just-a-scalar.yaml",
        "kind-is-a-number.yaml",
        "kind-unknown.yaml",
        "mac-as-sexagesimal.yaml",
        "merge-key.yaml",
        "metadata-is-a-list.yaml",
        "name-too-long.yaml",
        "huge-integer.yaml",
        "huge-scalar.yaml",
        "patchpanel-huge.yaml",
        "python-name-tag.yaml",
        "python-tag.yaml",
        "range-explosion.yaml",
        "range-many-spans.yaml",
        "recursive-anchor.yaml",
        "spec-is-a-string.yaml",
        "surrogate-escape.yaml",
        "tab-indent.yaml",
        "template-cycle.yaml",
        "top-level-sequence.yaml",
        "truncated-mid-key.yaml",
        "tunnel-with-secrets.yaml",
        "undefined-alias.yaml",
        "unknown-keys-everywhere.yaml",
        "unknown-tag.yaml",
        "unterminated-flow.yaml",
        "unterminated-quote.yaml",
        "yaml11-booleans.yaml",
    }
)


@pytest.mark.parametrize("name", sorted(MUST_FAIL), ids=sorted(MUST_FAIL))
def test_the_hostile_seeds_are_actually_rejected(name: str, tmp_path: Path) -> None:
    """Rejected *and* clean. "No traceback" is worthless if it also means "no check"."""
    assert name in CORPUS, f"{name} is listed as hostile but is not in the corpus"
    inventory = load_bytes(CORPUS[name].encode("utf-8"), tmp_path)
    assert inventory.errors, f"{name} loaded without complaint"
    assert not inventory.elements, f"{name} produced elements: {sorted(inventory.elements)}"


# --------------------------------------------------------------------------- #
# Mutation
# --------------------------------------------------------------------------- #


@st.composite
def mutants(draw: st.DrawFn) -> str:
    """A corpus seed, damaged.

    Each mutation is one a real broken file exhibits — a transfer that stopped
    half way, a merge conflict that spliced two files together, an editor that
    re-indented, a generator that repeated a block — rather than random bytes,
    which almost always produce "not valid YAML" and stop there. The point is to
    reach the *near misses*: documents that parse and then fail somewhere deeper.
    """
    seed = draw(st.sampled_from(sorted(CORPUS)))
    text = CORPUS[seed]
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        text = draw(_mutate(text))
    return text


@st.composite
def _mutate(draw: st.DrawFn, text: str) -> str:
    """Apply one damage operation to ``text``."""
    kind = draw(
        st.sampled_from(
            (
                "truncate",
                "splice",
                "duplicate-line",
                "drop-line",
                "reindent",
                "wrap",
                "repeat-document",
                "insert",
                "swap-lines",
            )
        )
    )
    lines = text.splitlines(keepends=True)
    if kind == "truncate" and text:
        return text[: draw(st.integers(min_value=0, max_value=len(text)))]
    if kind == "splice":
        other = CORPUS[draw(st.sampled_from(sorted(CORPUS)))]
        cut = draw(st.integers(min_value=0, max_value=len(text)))
        return text[:cut] + other[draw(st.integers(min_value=0, max_value=len(other))) :]
    if kind == "duplicate-line" and lines:
        index = draw(st.integers(min_value=0, max_value=len(lines) - 1))
        return "".join((*lines[:index], lines[index], *lines[index:]))
    if kind == "drop-line" and lines:
        index = draw(st.integers(min_value=0, max_value=len(lines) - 1))
        return "".join((*lines[:index], *lines[index + 1 :]))
    if kind == "reindent" and lines:
        pad = " " * draw(st.integers(min_value=1, max_value=4))
        index = draw(st.integers(min_value=0, max_value=len(lines) - 1))
        return "".join(
            pad + line if position == index else line for position, line in enumerate(lines)
        )
    if kind == "wrap":
        depth = draw(st.integers(min_value=1, max_value=40))
        return "wrapped: " + "[" * depth + "\n" + text + "\n" + "]" * depth + "\n"
    if kind == "repeat-document":
        return (text + "\n---\n") * draw(st.integers(min_value=2, max_value=4))
    if kind == "insert":
        fragment = draw(
            st.sampled_from(
                ("\t", "\x00", "﻿", "---\n", "...\n", "*a\n", "&a\n", "!!str ", "#", ": ")
            )
        )
        cut = draw(st.integers(min_value=0, max_value=len(text)))
        return text[:cut] + fragment + text[cut:]
    if kind == "swap-lines" and len(lines) >= 2:
        first = draw(st.integers(min_value=0, max_value=len(lines) - 1))
        second = draw(st.integers(min_value=0, max_value=len(lines) - 1))
        swapped = list(lines)
        swapped[first], swapped[second] = swapped[second], swapped[first]
        return "".join(swapped)
    return text


@settings(max_examples=searched(200), suppress_health_check=[HealthCheck.too_slow])
@given(mutants())
def test_a_mutated_document_never_escapes_the_loader(payload: str) -> None:
    """Whatever comes out is an inventory with diagnostics, or a netviz error.

    ``load_stream`` rather than a directory walk, so that 200 examples cost 200
    parses and no filesystem at all; the filesystem-specific failure modes are
    covered by the corpus tests above, which do go through disk.
    """
    start = time.monotonic()
    try:
        inventory = load_stream(payload, name="fuzz.yaml")
    except NetvizError:
        # A structured refusal is a correct outcome.
        return
    elapsed = time.monotonic() - start

    assert_handled(inventory)
    assert_bounded(inventory)
    assert elapsed < TIME_BUDGET_SECONDS


@settings(max_examples=searched(100), suppress_health_check=[HealthCheck.too_slow])
@given(mutants())
def test_a_mutated_document_is_read_the_same_way_twice(payload: str) -> None:
    """Loading is a function of the text: no ordering, no state, no clock."""
    try:
        first = load_stream(payload, name="fuzz.yaml")
        second = load_stream(payload, name="fuzz.yaml")
    except NetvizError:
        return
    assert [str(error) for error in first.errors] == [str(error) for error in second.errors]
    assert sorted(first.elements) == sorted(second.elements)


# --------------------------------------------------------------------------- #
# Bytes the filesystem can hold and a decoder cannot
# --------------------------------------------------------------------------- #

#: Byte sequences with no text form. These cannot be committed as corpus files —
#: the repository's own tooling reads every ``.yaml`` it ships as UTF-8 — so
#: they are built here and written to a temporary directory instead.
UNDECODABLE: Final[dict[str, bytes]] = {
    "lone-continuation": b"apiVersion: \xff\xfe\nkind: switch\n",
    "truncated-utf8": "kind: switché".encode()[:-1],
    "utf16-le": "kind: switch\n".encode("utf-16-le"),
    "nul-bytes": b"api\x00Version: netviz.dev/v1alpha1\n",
    "latin1": "description: café".encode("latin-1"),
    "random-binary": bytes(range(256)),
}


@pytest.mark.parametrize("name", sorted(UNDECODABLE), ids=sorted(UNDECODABLE))
def test_a_file_that_is_not_text_is_reported_not_raised(name: str, tmp_path: Path) -> None:
    """A YAML file need not be UTF-8; the loader still has to say so in writing."""
    inventory = load_bytes(UNDECODABLE[name], tmp_path)
    assert_handled(inventory)
    assert_bounded(inventory)
    assert inventory.errors, f"{name} was accepted as an inventory"


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #

#: Documents whose *input* is small and whose naive expansion is not. Each one
#: is a way of asking the loader to do unbounded work with bounded typing.
AMPLIFIERS: Final[dict[str, str]] = {
    "interface-range": (
        "apiVersion: netviz.dev/v1alpha1\nkind: switch\nmetadata:\n  name: a\n"
        "spec:\n  interfaces:\n    - range: e[1-999999999]\n      type: ethernet\n"
    ),
    "interface-range-product": (
        "apiVersion: netviz.dev/v1alpha1\nkind: switch\nmetadata:\n  name: a\n"
        "spec:\n  interfaces:\n    - range: e[1-9999][1-9999][1-9999]\n      type: ethernet\n"
    ),
    "patch-panel": (
        "apiVersion: netviz.dev/v1alpha1\nkind: patchpanel\nmetadata:\n  name: p\n"
        "spec:\n  ports: 1-999999999\n"
    ),
    "billion-laughs": (
        "a: &a [" + ", ".join(["x"] * 30) + "]\n"
        "b: &b [" + ", ".join(["*a"] * 30) + "]\n"
        "c: &c [" + ", ".join(["*b"] * 30) + "]\n"
        "d: &d [" + ", ".join(["*c"] * 30) + "]\n"
        "e: [" + ", ".join(["*d"] * 30) + "]\n"
    ),
    "deep-flow-nesting": "a: " + "[" * 5000 + "]" * 5000 + "\n",
    "huge-scalar": (
        "apiVersion: netviz.dev/v1alpha1\nkind: switch\nmetadata:\n  name: a\n"
        "spec:\n  vendor: " + "Z" * 1_000_000 + "\n"
    ),
}


@pytest.mark.parametrize("name", sorted(AMPLIFIERS), ids=sorted(AMPLIFIERS))
def test_an_amplifying_document_costs_bounded_time_and_memory(name: str) -> None:
    """Bounded typing must buy bounded work.

    Measured with :mod:`tracemalloc` rather than resident set size: RSS is the
    allocator's business and swings with the garbage collector, whereas the peak
    of what Python itself allocated is exactly the amplification being bounded.
    """
    payload = AMPLIFIERS[name]
    tracemalloc.start()
    try:
        start = time.monotonic()
        try:
            inventory = load_stream(payload, name="fuzz.yaml")
        except NetvizError:
            inventory = None
        elapsed = time.monotonic() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert elapsed < TIME_BUDGET_SECONDS, f"{name} took {elapsed:.1f}s"
    assert peak < MEMORY_BUDGET_BYTES, f"{name} peaked at {peak / 1e6:.0f} MB"
    if inventory is not None:
        assert_handled(inventory)
        assert_bounded(inventory)
        assert not inventory.elements, f"{name} produced elements"


# --------------------------------------------------------------------------- #
# Regressions
# --------------------------------------------------------------------------- #
#
# One plain example per bug the properties above found, so that each says out
# loud what it was. The corpus seeds them too, but a seed only asserts "handled
# cleanly"; these assert *what* the handling now is.


def test_a_patch_panel_port_range_is_counted_before_it_is_expanded() -> None:
    """``ports: 1-999999999`` killed the process.

    ``parse_port_range`` appended every position of a span to a list and checked
    the total against ``MAX_PANEL_PORTS`` only once the span had finished, so
    eight keystrokes asked for a billion strings and the check was reached by
    nobody: the process was OOM-killed first. The bound is arithmetic now.
    """
    tracemalloc.start()
    try:
        inventory = load_stream(
            "apiVersion: netviz.dev/v1alpha1\nkind: patchpanel\nmetadata:\n  name: p\n"
            "spec:\n  ports: 1-999999999\n",
            name="regression.yaml",
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < MEMORY_BUDGET_BYTES, f"peaked at {peak / 1e6:.0f} MB"
    assert [error.rule for error in inventory.errors] == ["NG-P006"]
    assert "1024 positions" in inventory.errors[0].message


@pytest.mark.parametrize(
    ("label", "document"),
    [
        (
            "interface mtu",
            "apiVersion: netviz.dev/v1alpha1\nkind: switch\nmetadata:\n  name: a\n"
            "spec:\n  interfaces:\n    - name: e0\n      type: ethernet\n      mtu: {digits}\n",
        ),
        (
            "vlan id",
            "apiVersion: netviz.dev/v1alpha1\nkind: switch\nmetadata:\n  name: a\n"
            "spec:\n  interfaces:\n    - name: e0\n      type: ethernet\n"
            "      vlan: {{mode: access, access_vlan: {digits}}}\n",
        ),
        ("bare mapping value", "a: {digits}\n"),
    ],
)
def test_an_integer_literal_python_refuses_to_convert_is_a_diagnostic(
    label: str, document: str
) -> None:
    """A 5000-digit number escaped the loader as a bare ``ValueError``.

    CPython refuses to convert an integer literal of more than 4300 digits
    (CVE-2020-10735), and PyYAML's constructors call :func:`int` on whatever the
    resolver matched — so the exception came out of the *parser*, where nothing
    was catching anything but ``yaml.YAMLError``, and reached the caller as a
    traceback about ``sys.set_int_max_str_digits``.
    """
    inventory = load_stream(document.format(digits="9" * 5000), name="regression.yaml")
    assert inventory.errors, label
    assert not inventory.elements
    assert_handled(inventory)
    assert_bounded(inventory)


def test_a_deeply_nested_document_is_refused_before_it_reaches_the_parser() -> None:
    """Deep nesting crashed one parser and raised past the other.

    The pure-Python composer recurses once per level and raised an uncaught
    ``RecursionError`` at a few thousand; libyaml's composer recurses in C and
    took the whole process down with a segmentation fault at around thirty
    thousand, which no ``except`` clause can catch. Which of the two runs
    depends on the PyYAML wheel, so the same file was a traceback on one machine
    and a killed process on another. The depth is now bounded before either sees
    the text.
    """
    depth = MAX_NESTING_DEPTH + 1
    inventory = load_stream("a: " + "[" * depth + "]" * depth, name="regression.yaml")

    assert len(inventory.errors) == 1
    assert f"more than {MAX_NESTING_DEPTH} levels deep" in inventory.errors[0].message
    assert not inventory.elements


def test_a_document_at_the_nesting_limit_still_loads() -> None:
    """The guard is a ceiling, not a fence: what is under it is untouched."""
    depth = MAX_NESTING_DEPTH
    inventory = load_stream("a: " + "[" * depth + "]" * depth, name="regression.yaml")
    # ``kind`` is missing, which is the *schema*'s complaint and not the guard's.
    assert [error.rule for error in inventory.errors] == ["NG-D001"]


def test_an_oversized_range_span_is_not_echoed_in_full() -> None:
    """The ``range`` diagnostic quoted the whole 5000-digit span back.

    Every other diagnostic goes through :func:`~netviz.errors.echo_value`,
    which clips; this one interpolated ``match.group()`` with ``!r`` and produced
    a 5297-character error line. Found by
    :func:`test_a_mutated_document_never_escapes_the_loader` via ``assert_bounded``.
    """
    inventory = load_stream(
        "apiVersion: netviz.dev/v1alpha1\nkind: switch\nmetadata:\n  name: a\n"
        "spec:\n  interfaces:\n    - range: e[1-" + "9" * 5000 + "]\n      type: ethernet\n",
        name="regression.yaml",
    )
    assert [error.rule for error in inventory.errors] == ["NG-R003"]
    # Two clipped echoes plus prose and a location prefix; the point is that it
    # is a bounded multiple of the echo limit rather than of the input.
    assert len(str(inventory.errors[0])) < 4 * MAX_ECHOED_VALUE_LENGTH
    assert_bounded(inventory)
