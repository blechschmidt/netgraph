# Testing netgraph

The suite is in two halves, and they answer different questions.

**Example tests** — everything in `tests/` except the three files below — say what
a feature *does*. Each one names an inventory somebody wrote, calls one thing,
and asserts one outcome. When they fail, the failure is a sentence.

**Property tests** — `tests/test_properties.py`, `tests/test_edit_properties.py`
and `tests/test_fuzz_loader.py` — say what netgraph may *never* do, for every
input rather than for the ones somebody thought of. They are driven by
[Hypothesis][hypothesis] and by the
strategies in `tests/strategies.py`, which generate whole inventories the loader
accepts: unicode-bearing free text, near-boundary names, interfaces with
consistent types and members, cables whose endpoints resolve, nested tunnel
stacks, adapters, patch panels, templates and interface ranges.

Both run under a plain `pytest`.

```console
$ pip install --editable ".[dev]"
$ pytest
```

The suite runs with the parse cache **on**, redirected to a temporary directory
for the session (`isolate_the_parse_cache` in `tests/conftest.py`). It is not
switched off on purpose: the cache is on in every command, so a suite that
disabled it would leave the warm path tested only by `tests/test_cache.py` while
every golden, transcript and end-to-end assertion kept exercising the cold one.
Nothing is read from or written to your own cache.

## Platforms

netgraph is published for Python 3.10–3.13 and is used on all three desktop
platforms, so all three are in CI. They are not covered to the same depth, and
the table says which is which rather than leaving it to be inferred from
`.github/workflows/ci.yml`.

| Platform | Python | What runs | Depth |
|---|---|---|---|
| `ubuntu-24.04` | 3.10, 3.11, 3.12 | the whole suite, both YAML parsers, `ruff`, `mypy`, `netgraph fmt --check`, the coverage gate at 85 % | **primary** — every gate, every version |
| `macos-14` (Apple Silicon) | 3.12 | the whole suite, `ruff`, `mypy`, `netgraph fmt --check`, coverage at 85 % | **full** — one interpreter |
| `windows-latest` | 3.12 | the same, coverage at 80 % | **full minus the POSIX-only tests** — see below |

One Python version each on macOS and Windows, and the middle of the supported
range. What those jobs are there to catch — a path separator, a line ending, a
socket option, a filesystem-event backend, a rename over an open file — is a
property of the operating system rather than of the interpreter, so a second
version would double the cost and check the same thing twice. The interpreter
range is covered on Linux, where it is cheapest.

Three things are **not** covered anywhere, and are worth knowing:

* **Python 3.13.** Claimed in the PyPI classifiers, not in the matrix.
* **Docker.** The `docker` job is Linux-only; the image is a Linux image.
* **The `netgraph web` and `netgraph watch` browser front ends** are asserted
  through HTTP and through the DOM they emit, never through a real browser.

### What is skipped on Windows, and why

Six tests, all in `tests/test_loader.py` and `tests/test_routing.py`. Each one is
skipped for a **capability the platform does not have**, never for a platform:

| Skipped | Capability | Marked with |
|---|---|---|
| an unreadable directory is reported, not raised | POSIX permission bits — `chmod(0o000)` on Windows sets a read-only flag and leaves the directory readable, so there is nothing for the loader to report | `requires_posix_permissions` |
| five symlink cases: escaping the root, a cycle, reached twice, followed, dangling | creating a symlink needs `SeCreateSymbolicLinkPrivilege`, which an unelevated CI process does not hold. *Measured*, not assumed, so these do run on a machine with Developer Mode on | `requires_symlinks` |
| a FIFO is not loaded; a FIFO is not a valid root | `os.mkfifo` does not exist on Windows, and a Windows named pipe is not a filesystem entry the loader could walk into | `requires_mkfifo` |
| the generated route script passes `sh -n` | no POSIX shell. The script's *content* is still asserted line by line there; only the second opinion from `sh` is missing | `requires_posix_shell` |

One more is skipped on **macOS** rather than on Windows, and by the same rule.
`docs/commands/completion.md` documents what `netgraph completion bash` prints;
Click inspects the host's bash first and adds a warning on anything older than
4.4, which is what Apple has shipped as `/bin/bash` since 2007. The transcript is
correct, and so is the extra line — so the block is skipped where the shell adds
it (`HAVE_BASH_COMPLETION`, measured exactly as Click measures it) rather than
documented twice.

The marks live in `tests/platform_marks.py`, one per capability, with the reason
in the mark rather than in a comment — so a skipped run says why in its own
output. `tests/test_platform.py::test_no_test_module_skips_a_whole_platform`
fails if a skip is ever written as `skipif(sys.platform == "win32")` instead,
because that is how "the platform lacks this" quietly becomes "netgraph is
broken here and nobody is looking".

The Windows coverage floor is 80 rather than 85 for exactly those six tests: the
lines they exercise are counted as missed, and failing the job for an honest skip
would make it look like a regression.

### Platform behaviour asserted everywhere

`tests/test_platform.py` is the other half, and it runs on all three platforms
by design. Most of what it checks is *about* Windows — the newline policy, the
retry around `os.replace`, the reserved device names, the Graphviz search, the
socket option, the PowerShell completion script — and guarding it with `skipif`
would mean none of it was checked until somebody happened to run the suite there.
So the platform-dependent branch is reached by naming the platform
(`monkeypatch.setattr(os, "name", "nt")`) and the platform-independent contract
is asserted directly.

That leaves three things only a real runner can settle, which is what the two
jobs buy: a real `os.replace` against a real open handle, a real
`ReadDirectoryChangesW` / FSEvents watcher, and a checkout under
`core.autocrlf=true` — which is what `.gitattributes` exists to neutralise, and
what `netgraph fmt --check examples` on the Windows job proves it did.

The PowerShell completion script is the one Windows-shaped thing checked *by the
real shell* on every platform. `pwsh` is preinstalled on all three runner images,
so the generated script is parsed by PowerShell's own parser, registered with
`Register-ArgumentCompleter`, and then driven through
`[CommandCompletion]::CompleteInput` — the same entry point the shell uses on Tab.
That last test completes an element name out of an inventory whose path contains a
space, which is the case that would break if the words travelled to Python
whitespace-separated instead of newline-separated.

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

`tests/test_edit_properties.py` asserts the two that the write path lives or dies
by:

| Property | What it rules out |
|---|---|
| a sequence of operations, undone, restores the tree byte for byte | an undo that quietly restyles a file |
| the edited tree and a tree written in that shape are one network | an editor that produces trees nobody would write |

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
