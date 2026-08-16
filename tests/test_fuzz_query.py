"""Fuzzing the other untrusted edge: the query parser.

The loader reads whatever is on disk; the query parser reads whatever is on the
command line, in an HTTP query string, or in a `query:` key of a document a
colleague wrote. That makes it the second genuine trust boundary in netviz,
and the contract at it is the same four clauses ``tests/test_fuzz_loader.py``
states for the first:

1. **It terminates.** No input makes it loop, and none makes it spend
   super-linear time on a linear amount of text. The language is total by
   construction — a finite tree and one bounded breadth-first search — and this
   is where that claim meets an adversary rather than a docstring.
2. **It fails structurally.** Every rejection is a
   :class:`~netviz.query.QueryError` carrying a span, never a traceback out of
   :mod:`re`, :mod:`ipaddress`, :mod:`fnmatch` or a bare ``IndexError``.
3. **It stays bounded.** A diagnostic echoes a window around the offending
   column rather than the query, so a 4096-character generated expression cannot
   become a 4096-character error line.
4. **The span is real.** Every error points *inside* the text it was given, and
   the caret line is aligned with the echo. An error that pointed past the end
   would be worse than no caret at all.

And one clause the loader's fuzzing has no counterpart for:

5. **Whatever parses, evaluates.** :func:`~netviz.query.parser.parse` resolves
   every attribute and checks every value, so a query that survives it is a
   query that runs against *any* graph without raising. Every mutant that parses
   is therefore run against three real inventories.

The corpus is the language's own surface: one seed per grammar form, plus the
shapes that broke something once. Hypothesis mutates them — truncating,
splicing, unbalancing brackets, swapping operators, repeating — and keeps what
it finds in ``.hypothesis/examples``. ``docs/testing.md`` says how to run a
longer budget than CI's.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from netviz.loader import load_tree
from netviz.query import MAX_QUERY_LENGTH, QueryError, evaluate, parse
from netviz.query.errors import MAX_QUERY_ECHO
from netviz.query.lexer import tokenize
from netviz.render.graph import Layer, build_graph

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

#: The language's surface, one seed per form, plus the near-misses that matter.
#: Kept here rather than in ``tests/fuzz-corpus/`` because a query is one line:
#: a directory of thirty single-line files would be filesystem ceremony around
#: what is legibly a tuple.
CORPUS: Final[tuple[str, ...]] = (
    "*",
    "sw-core",
    "sw-*",
    "kind = switch",
    "kind != switch",
    "kind in (switch, router, hub)",
    "name ~ sw-*-01",
    "name !~ sw-*",
    "name =~ ^sw-(north|south)-",
    "namespace under sites/north",
    "vlan = 99",
    "vlan in (10, 20, 99)",
    "mtu > 1500",
    "ports <= 48",
    "address in 10.20.0.0/16",
    "address in 2001:db8::/32",
    "routable-address in 192.0.2.0/24",
    "label.role = access",
    "has vrf",
    "has label.site",
    "not has address",
    "kind = switch and has vrf",
    "kind = switch or kind = router",
    "kind = switch and (has vrf or has netns)",
    "not (kind = switch and has vrf)",
    "interface[has address]",
    "interface[address in 10.20.0.0/16 and not has vrf]",
    "interface[enabled = false and type = ethernet]",
    "interface[mtu > 9000 or vlan-mode = trunk]",
    "link[medium = fiber]",
    "link[peer-kind = router and speed >= 10000]",
    "netns[depth > 0 and has address]",
    "zone[declared = true and rules > 0]",
    "neighbors of kind = router",
    "within 2 hops of sw-core",
    "within 0 hops of (kind = router)",
    "reachable from kind = router",
    "not within 2 hops of (kind = router or kind = switch)",
    "name = 'a b'",
    'description ~ "*uplink*"',
    "label.role = access and not neighbors of (label.role = distribution)",
    "kind in (switch, router) and not interface[name ~ Vlan* and has address]",
    # The near misses: each of these was, or could be, one character from legal.
    "kind = ",
    "kind = switch and",
    "(kind = switch",
    "interface[",
    "within 2 hops",
    "has",
    "vlan = 5000",
    "name < 5",
    "address in nonsense",
    "name =~ [",
    "",
)

#: How long the whole corpus may take to parse. It is fifty one-line queries;
#: this is a hang detector, not a benchmark.
TIME_BUDGET_SECONDS: Final = 5.0

#: How long one mutant may take. A query is bounded to 4096 characters, so any
#: input at all should parse in microseconds; a second is four orders of
#: magnitude of headroom and still catches a catastrophic backtrack.
MUTANT_BUDGET_SECONDS: Final = 1.0

#: Ceiling on one diagnostic. The echo is bounded to :data:`MAX_QUERY_ECHO`
#: characters and there is prose either side of it, so this is generous — it
#: exists to catch an *unbounded* echo, not to police wording.
MAX_DIAGNOSTIC_LENGTH: Final = 4 * MAX_QUERY_ECHO


def searched(floor: int) -> int:
    """At least ``floor`` examples, and more when the profile asks for more.

    The same helper ``tests/test_fuzz_loader.py`` uses, and for the same reason:
    an explicit ``max_examples`` replaces the profile's rather than combining
    with it, and parsing a one-line query costs microseconds — so this file can
    afford a far higher floor than an inventory property can.
    """
    return max(int(settings().max_examples), floor)


@pytest.fixture(scope="module")
def graphs():
    """Three real graphs, so "it evaluates" means against something.

    Two inventories and two layers: layer 1 has no derived nodes and layer 3 is
    full of them, which is where an evaluator that assumed every node has an
    element would come apart.
    """
    campus = load_tree(REPO_ROOT / "examples" / "campus")
    home = load_tree(REPO_ROOT / "examples" / "home-lab")
    return (
        build_graph(campus),
        build_graph(campus, layer=Layer.L3),
        build_graph(home),
    )


# --------------------------------------------------------------------------- #
# What "handled cleanly" means
# --------------------------------------------------------------------------- #


def assert_handled(text: str) -> object | None:
    """Parse ``text``; return the query, or ``None`` when it was refused.

    Every refusal must be a :class:`QueryError` with a message, a span inside
    the text, and a caret line the same length as the pad it sits under. Any
    other exception is a bug, and letting it escape here is the point.
    """
    try:
        return parse(text)
    except QueryError as error:
        assert error.message, "a diagnostic with no message says nothing"
        assert 0 <= error.offset <= len(text), (
            f"the span starts at {error.offset}, outside a {len(text)}-character query"
        )
        assert error.length >= 1
        assert error.line >= 1 and error.column >= 1
        block = error.annotated()
        assert len(block) <= MAX_DIAGNOSTIC_LENGTH, f"{len(block)}-character diagnostic"
        _assert_caret_is_aligned(block)
        return None


def _assert_caret_is_aligned(block: str) -> None:
    """The caret line, when there is one, sits under the echo and inside it."""
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("^"):
            continue
        assert index > 0, "a caret line with nothing above it points at nothing"
        echo = lines[index - 1]
        # One past the end is legal, and is what an "ends too early" diagnostic
        # points at: the position a term should have been written in. Anything
        # further is a caret under nothing.
        assert len(line) <= len(echo) + 1, (
            f"the caret runs {len(line) - len(echo)} characters past the line it marks"
        )


# --------------------------------------------------------------------------- #
# The corpus itself
# --------------------------------------------------------------------------- #


def test_the_corpus_covers_the_language() -> None:
    """A corpus that quietly shrank would make every property below vacuous."""
    assert len(CORPUS) >= 40, len(CORPUS)
    for form in ("interface[", "link[", "netns[", "zone[", "within ", "reachable ", "neighbors "):
        assert any(form in seed for seed in CORPUS), form


@pytest.mark.parametrize("seed", CORPUS, ids=range(len(CORPUS)))
def test_every_corpus_seed_is_handled(seed: str, graphs) -> None:
    """One test per seed, so a regression names the form that caused it."""
    query = assert_handled(seed)
    if query is None:
        return
    for graph in graphs:
        evaluate(query, graph)


def test_the_whole_corpus_parses_quickly() -> None:
    """A hang detector. Fifty one-line queries is not work."""
    start = time.monotonic()
    for seed in CORPUS:
        assert_handled(seed)
    elapsed = time.monotonic() - start
    assert elapsed < TIME_BUDGET_SECONDS, f"the corpus took {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# The mutations
# --------------------------------------------------------------------------- #


@st.composite
def mutants(draw: st.DrawFn) -> str:
    """A corpus seed, damaged.

    Each mutation is one a real mistyped query exhibits — a bracket that never
    closed, an operator that was reached for and missed, a shell that ate a
    quote, a generator that repeated a clause — rather than random bytes, which
    almost always produce "not a query" and stop there. The point is to reach
    the *near misses*: expressions that tokenize and then fail somewhere deeper,
    and expressions that parse and then have to evaluate.
    """
    seed = draw(st.sampled_from(CORPUS))
    text = seed
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        text = draw(_mutate(text))
    return text


#: Fragments spliced in, each of them syntax in at least one position.
_SHRAPNEL: Final[tuple[str, ...]] = (
    "(",
    ")",
    "[",
    "]",
    ",",
    "=",
    "==",
    "!=",
    "~",
    "!~",
    "=~",
    "<",
    ">",
    "<=",
    ">=",
    "and",
    "or",
    "not",
    "in",
    "under",
    "has",
    "of",
    "hops",
    "within",
    "reachable",
    "neighbors",
    "from",
    "*",
    "?",
    "'",
    '"',
    "\\",
    "-",
    ".",
    "/",
    ":",
    "%",
    "#",
    "interface",
    "link",
    "netns",
    "zone",
    "element",
    "kind",
    "label.",
    "vlan",
    "\n",
    "\t",
    " ",
    "\x00",
    "é",
    "10.0.0.0/8",
    "4294967296",
    "-1",
)


@st.composite
def _mutate(draw: st.DrawFn, text: str) -> str:
    """Apply one damage operation to ``text``."""
    kind = draw(
        st.sampled_from(
            (
                "truncate",
                "splice",
                "insert",
                "delete",
                "duplicate",
                "unbalance",
                "swap-operator",
                "repeat",
                "case",
            )
        )
    )
    if kind == "truncate" and text:
        return text[: draw(st.integers(min_value=0, max_value=len(text)))]
    if kind == "splice":
        other = draw(st.sampled_from(CORPUS))
        cut = draw(st.integers(min_value=0, max_value=len(text)))
        return text[:cut] + other[draw(st.integers(min_value=0, max_value=len(other))) :]
    if kind == "insert":
        at = draw(st.integers(min_value=0, max_value=len(text)))
        return text[:at] + draw(st.sampled_from(_SHRAPNEL)) + text[at:]
    if kind == "delete" and text:
        at = draw(st.integers(min_value=0, max_value=len(text) - 1))
        return text[:at] + text[at + 1 :]
    if kind == "duplicate" and text:
        at = draw(st.integers(min_value=0, max_value=len(text)))
        return text[:at] + text[:at] + text[at:]
    if kind == "unbalance":
        # Drop one bracket of each kind, which is the single most common way a
        # hand-typed query is wrong and the one the parser must not survive.
        for bracket in draw(st.sampled_from(("()", "[]", "(", "["))):
            text = text.replace(bracket, "", 1)
        return text
    if kind == "swap-operator":
        for old in ("=", "~", "<", ">"):
            if old in text:
                return text.replace(old, draw(st.sampled_from(_SHRAPNEL[:15])), 1)
        return text
    if kind == "repeat":
        joiner = draw(st.sampled_from((" and ", " or ", " ", ",")))
        return joiner.join([text] * draw(st.integers(min_value=2, max_value=6)))
    if kind == "case":
        return text.upper() if draw(st.booleans()) else text.title()
    return text


@given(mutants())
@settings(max_examples=searched(400), suppress_health_check=[HealthCheck.too_slow])
def test_a_mutated_query_is_parsed_or_refused_in_writing(text: str) -> None:
    """No mutant escapes as anything but a QueryError with a usable span."""
    assert_handled(text)


@given(mutants())
@settings(max_examples=searched(200), suppress_health_check=[HealthCheck.too_slow])
def test_whatever_parses_evaluates(text: str) -> None:
    """The parser's promise: a query that parses runs against any graph.

    Attributes are resolved and values checked at parse time precisely so that
    evaluation has nothing left to reject — which means a mutant that parses and
    then raises during evaluation is a hole in that check, not a bad query.

    The graphs are built once at module scope and reached through the cache
    rather than through the fixture: Hypothesis and function-scoped fixtures do
    not mix, and rebuilding a campus per example would make this the slowest
    test in the suite.
    """
    query = assert_handled(text)
    if query is None:
        return
    for graph in _graphs():
        evaluate(query, graph)


_CACHED: list[object] = []


def _graphs():
    """The three graphs, built once for the whole module."""
    if not _CACHED:
        campus = load_tree(REPO_ROOT / "examples" / "campus")
        home = load_tree(REPO_ROOT / "examples" / "home-lab")
        _CACHED.extend(
            (build_graph(campus), build_graph(campus, layer=Layer.L3), build_graph(home))
        )
    return _CACHED


@given(mutants())
@settings(max_examples=searched(200), suppress_health_check=[HealthCheck.too_slow])
def test_parsing_is_fast_whatever_it_is_given(text: str) -> None:
    """A bounded input costs bounded time. The totality claim, measured."""
    start = time.monotonic()
    assert_handled(text)
    elapsed = time.monotonic() - start
    assert elapsed < MUTANT_BUDGET_SECONDS, f"{elapsed:.2f}s on {len(text)} characters"


@given(mutants())
@settings(max_examples=searched(200), suppress_health_check=[HealthCheck.too_slow])
def test_tokenizing_never_loses_or_invents_a_character(text: str) -> None:
    """Every token's span lies inside the text, and they do not overlap.

    The invariant every caret in every diagnostic rests on. A lexer that
    reported a span it had not read would put the underline under the wrong
    thing, and nothing downstream could notice.
    """
    try:
        tokens = tokenize(text)
    except QueryError:
        return
    last = 0
    for token in tokens:
        assert 0 <= token.offset <= len(text), token
        assert token.offset + token.length <= len(text), token
        assert token.offset >= last, f"{token} overlaps the token before it"
        last = token.offset + token.length


# --------------------------------------------------------------------------- #
# Generated adversaries
# --------------------------------------------------------------------------- #


@given(st.text(max_size=200))
@settings(max_examples=searched(300), suppress_health_check=[HealthCheck.too_slow])
def test_arbitrary_text_is_handled(text: str) -> None:
    """Not only mutants: any string at all is either a query or a diagnostic."""
    assert_handled(text)


@given(st.integers(min_value=1, max_value=6))
@settings(max_examples=20)
def test_deep_nesting_is_refused_rather_than_recursed(depth: int) -> None:
    """The depth budget, approached from above.

    Python's recursion limit is a crash, not a diagnostic, so the parser has to
    refuse before it reaches one. Multiplied out, this reaches several hundred
    levels — an order of magnitude past the budget.
    """
    levels = depth * 100
    text = "(" * levels + "*" + ")" * levels
    with pytest.raises(QueryError, match="nests more than"):
        parse(text)


@given(st.integers(min_value=1, max_value=4))
@settings(max_examples=20)
def test_an_oversized_query_is_refused_before_it_is_scanned(multiple: int) -> None:
    """The length bound is checked first, so a megabyte is not tokenized."""
    text = "x" * (MAX_QUERY_LENGTH * multiple + 1)
    start = time.monotonic()
    with pytest.raises(QueryError, match="characters long"):
        parse(text)
    assert time.monotonic() - start < MUTANT_BUDGET_SECONDS


@given(st.text(alphabet="()[]", min_size=1, max_size=120))
@settings(max_examples=searched(200))
def test_brackets_alone_are_never_a_query(text: str) -> None:
    """Punctuation with no terms in it must not parse as one."""
    with pytest.raises(QueryError):
        parse(text)


@given(st.text(alphabet="*?[]!-", min_size=1, max_size=40))
@settings(max_examples=searched(200))
def test_a_hostile_glob_is_compiled_or_refused(text: str) -> None:
    """`fnmatch` escapes everything, but an unbalanced class is worth pinning."""
    assert_handled(f"name ~ {text}")


@given(st.text(alphabet="().*+?[]{}|^$\\", min_size=1, max_size=40))
@settings(max_examples=searched(300))
def test_a_hostile_regex_is_compiled_or_refused(text: str) -> None:
    """`=~` compiles at parse time, so a bad pattern is a diagnostic not a crash.

    And a pattern that *does* compile is then run against every node — so this
    also exercises the one place the language could be made to backtrack.
    """
    query = assert_handled(f"name =~ {text}")
    if query is None:
        return
    start = time.monotonic()
    for graph in _graphs():
        evaluate(query, graph)
    assert time.monotonic() - start < MUTANT_BUDGET_SECONDS * 3


@given(st.text(alphabet="0123456789./:abcdef", min_size=1, max_size=50))
@settings(max_examples=searched(200))
def test_a_hostile_address_is_parsed_or_refused(text: str) -> None:
    """`ipaddress` raises several kinds of ValueError; none may escape."""
    assert_handled(f"address in {text}")
