# Testing netgraph

The suite is in two halves, and they answer different questions.

**Example tests** — everything in `tests/` except the two files below — say what
a feature *does*. Each one names an inventory somebody wrote, calls one thing,
and asserts one outcome. When they fail, the failure is a sentence.

**Property tests** — `tests/test_properties.py` and `tests/test_fuzz_loader.py`
— say what netgraph may *never* do, for every input rather than for the ones
somebody thought of. They are driven by [Hypothesis][hypothesis] and by the
strategies in `tests/strategies.py`, which generate whole inventories the loader
accepts: unicode-bearing free text, near-boundary names, interfaces with
consistent types and members, cables whose endpoints resolve, nested tunnel
stacks, adapters, patch panels, templates and interface ranges.

Both run under a plain `pytest`.

```console
$ pip install --editable ".[dev]"
$ pytest
```

## The properties

`tests/test_properties.py` asserts the statements that are universally
quantified, and therefore the ones an example test can only ever sample:

| Property | What it rules out |
|---|---|
| `load(emit(load(x))) == load(x)` | a model field that can be set but not written |
| re-emitting is byte-identical | a value that survives equality but not a diff |
| `fmt(fmt(x)) == fmt(x)` | a formatter that argues with itself |
| `fmt` preserves the loaded model, comments included | a formatter that quietly edits the network |
| `range:` and `spec.from` equal their hand-expansion | a shorthand that means something else |
| the tree does not depend on the file layout | a file behaving as a scope |
| every renderer completes, and its output parses | output nothing downstream can read |
| no free text can become syntax in dot / mermaid / json / html | the injection half |
| no *name* can become syntax either | the half a constrained grammar makes look safe |
| `validate` depends on the inventory and nothing else | findings that move when a file is renamed |
| a traced path only crosses edges the graph has | a route that does not exist |

`tests/test_fuzz_loader.py` covers the one component with a real trust
boundary. The loader reads files a user did not write — `netgraph import`
output, a third-party inventory, a generated tree — so the contract there is not
"parses correct input" but *terminates, fails structurally, bounds its
diagnostics, bounds its memory*. The seed corpus is `tests/fuzz-corpus/`: one
file per way of being wrong, mutated by the test into near misses.

## Profiles

How hard the search works is a profile, chosen with
`NETGRAPH_HYPOTHESIS_PROFILE`. The seed is pinned in `pyproject.toml`
(`--hypothesis-seed=0`) so a run is reproducible whichever profile it uses.

| Profile | Examples | For |
|---|---|---|
| `dev` (default) | 25 | every save; catches a regression in what you just changed |
| `ci` | 50 | every push |
| `deep` | 1000 | nightly, and locally when a property is new |

The number is the *default*; two properties adjust it and say why in a
docstring. The Graphviz-backed ones cap it (`capped()` in
`tests/test_properties.py`), because each example shells out to `dot`. The
mutation fuzzers raise it (`searched()` in `tests/test_fuzz_loader.py`), because
each example is a parse and a twenty-five-example fuzz pass is a fuzz pass in
name only.

```console
$ pytest tests/test_properties.py --no-cov                       # dev, the default
$ NETGRAPH_HYPOTHESIS_PROFILE=deep pytest tests/test_properties.py --no-cov
$ NETGRAPH_HYPOTHESIS_PROFILE=deep pytest tests/test_fuzz_loader.py --no-cov
```

Run the deep profile before trusting a property you have just written: a
property that holds for 25 examples and fails for 300 is worse than no property,
because it will fail on somebody else's pull request.

To search with a different seed — worth doing occasionally, since a pinned seed
searches the same region every time — pass one on the command line, where it
overrides the pinned value:

```console
$ NETGRAPH_HYPOTHESIS_PROFILE=deep pytest tests/test_properties.py --no-cov --hypothesis-seed=$RANDOM
```

## Reproducing a failure

A failing property prints the shrunk counterexample. For the inventory
properties that is an `InventoryPlan`, which is a list of plain mappings:

```pycon
>>> for path, text in plan.per_document().items():
...     print(f"--- {path}\n{text}")
```

Drop those files in a directory and every netgraph command reproduces the
failure by hand.

Hypothesis also writes the example to `.hypothesis/examples/` and replays it
first on the next run, so a fixed bug stays fixed without anybody copying the
input anywhere. CI caches that directory between runs for the same reason; see
`.github/workflows/ci.yml`.

Hypothesis prints a `@reproduce_failure(...)` blob too. It is version-specific
and belongs in a scratch edit, never in a commit — **the permanent record of a
bug is a plain example test**, written next to the property that found it. Every
bug these properties have found so far has one; they are collected under
"Regressions" at the end of `tests/test_properties.py` and of
`tests/test_fuzz_loader.py`.

## The nightly run

`.github/workflows/nightly.yml` runs the `deep` profile with a much larger
budget than a push can afford, restores the cached example database first, and
saves it again afterwards. It runs both YAML parsers, because they differ in
exactly the places a property notices — what raises, at what depth — and it runs
the loader fuzz target under four different seeds rather than one long pass,
because the mutation strategy picks a corpus seed and then damages it, so a
fresh seed reaches a different part of the corpus far more cheaply than more
examples of the same one.

A nightly failure is a real failure. It means the property is false and the
`ci` profile was simply not looking hard enough — so the fix is a fix in `src/`
plus a plain regression example, not a narrower property.

[hypothesis]: https://hypothesis.readthedocs.io/
