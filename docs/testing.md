# Testing netgraph

The suite is in three halves, and they answer different questions.

**Example tests** — everything in `tests/` except the five files below — say what
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

**The two protocol layers** — `tests/test_browser.py` and `tests/test_lsp.py` —
say what the two editors do *through the thing that connects them to a client*.
The first is the only place in the repository that executes the page's CSS and
JavaScript; the second is the only place that speaks JSON-RPC frames. See
[The browser layer](#the-browser-layer) and
[The language server](#the-language-server).

The first two and `tests/test_lsp.py` run under a plain `pytest`.
`tests/test_browser.py` skips unless a browser is installed, so a plain `pytest`
never fails for the want of one.

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

Two more jobs run outside that matrix, on Linux only, because what they check is
not a property of the platform: `browser` opens `netgraph web` in a real
Chromium ([below](#the-browser-layer)), and `docker` builds and drives the image.

Three things are **not** covered anywhere, and are worth knowing:

* **Python 3.13.** Claimed in the PyPI classifiers, not in the matrix.
* **Docker.** The `docker` job is Linux-only; the image is a Linux image.
* **A browser that is not Chromium**, and `netgraph watch`'s served page, which
  is still asserted through HTTP and through the DOM it emits rather than by
  something that renders it.

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

## The browser layer

`netgraph web` is about fourteen hundred lines of CSS and JavaScript, and until
`tests/test_browser.py` existed nothing executed any of it: `tests/test_web.py`,
`tests/test_web_session.py` and `tests/test_web_events.py` stop at the HTTP
boundary, so a regression in `app.js` shipped green. That file starts the real
server over a temporary copy of `examples/home-lab` on an ephemeral loopback
port, opens it in a headless Chromium through [Playwright][playwright], and
asserts what a person would see.

Some of it opens **two** pages against one server — `open_editor(beside=…)` —
because a shared session is a thing two browsers do to each other: a badge
appearing in one tab because somebody started typing in the other, an undo
issued here landing there, a save crossing without either page refetching the
whole tree.

```console
$ pip install --editable ".[dev,browser]"
$ playwright install chromium
$ pytest -m browser --no-cov
```

`--no-cov` because this layer is deliberately outside the coverage gate: it
imports very little Python and would only dilute a number that is about the
package. `-m browser` selects it; `-m 'not browser'` leaves it out of a run that
wants everything else.

Playwright is in the **`browser` extra rather than in `dev`**, so `pip install
'.[dev]'` stays what it was. Without it — or with it but without the browser it
drives — the whole module skips and says which command to run. It is never a hard
failure for a contributor who has neither, and `NETGRAPH_INSTALL_BROWSER=1` turns
the second command above into something the suite does for itself, which is how
the CI job is wired.

### What it asserts

| Test | The behaviour |
|---|---|
| the page boots and draws the inventory | every asset loads, `/api/state` is fetched, and there are as many shapes on screen as the server has records |
| typing in the text pane re-renders the diagram | the scratchpad's whole contract: text in, diagram out, nothing on disk |
| hovering a node opens the info box | field by field against the record `/api/graph` carries, so the picture and the JSON export cannot drift |
| clicking a node reveals its declaring document | a shape carries an address, an address has a file and a line, and the editor goes there — the 1:1 mapping the command exists for |
| a diagnostic row jumps to its location | `cables/broken.yaml#0:4` opens that file with line 4 selected |
| saving writes the file and `Ctrl-Z` puts it back | on disk *and* in the pane, with the diagram redrawn both times |
| dragging a node writes geometry to disk | **skips today** — see below |
| drawing a link produces a cable | **skips today** — see below |
| a read-only session disables every mutating control | no control that would write, *and* a 403 from every route that could, asked from the page's own origin |
| the poll notices a change made outside the session | `$EDITOR` writes the file; a clean pane adopts it, a dirty one is marked conflicted and left alone |
| a stale save is refused rather than clobbering | the content hash the save quotes is what refuses it, whether or not anything noticed first |
| the page has no accessibility violations | axe-core over the session, its dialogs and the changes drawer, in both colour schemes — see below |
| every node and link carries a role and a label | the SVG's semantics, checked against what the inventory says rather than against "some string is present" |
| the outline reads the view as text | one entry per drawn element, off screen until focused and a real panel once it is |
| a keyboard-only session creates, connects and undoes | the whole editor without one mouse event, asserted on the files it wrote |
| the palette finds a command and an element | one field over commands, addresses and paths, each row printing its own key |
| the shortcut sheet comes from the registered bindings | compared against `/api/bindings`, because a test with its own copy of the table would be a third copy |

The two direct-manipulation tests **perform their gesture for real** and skip only
when the tree did not move — which it does not, because a drag on the current
canvas pans the diagram and writes nothing. They are not `xfail` and not
commented out: the day the canvas grows the gesture, they start asserting its
outcome without being touched. The skip reason says so in the run's own output.

### The accessibility gate

Four of the tests above run [axe-core][axe] over the page and **fail on any
violation**, which is what makes it a gate rather than a report. What they check
is deliberately scoped:

* **the standards, and only the standards** — `wcag2a`, `wcag2aa`, `wcag21a`,
  `wcag21aa`. axe's `best-practice` tag carries opinions a page embedding a
  Graphviz drawing trips over for reasons unrelated to whether it can be used,
  and a gate that shouts about taste is a gate people learn to ignore;
* **the page as it really is** — with the file list drawn, the diagram annotated
  and the dialogs open. A page audited empty is a page audited without the half
  that is hard;
* **both colour schemes.** One palette cannot clear 4.5:1 against both a white
  and a near-black background, so `app.css` declares two and the dark one is
  audited under `emulate_media(color_scheme="dark")`. The test asserts the dark
  tokens actually took before believing the audit.

axe-core rides in the `browser` extra as `axe-core-python`, which vendors the
checker rather than fetching it, so the gate cannot change under you overnight.
Without it those four tests skip and the rest of the module runs.

Separately, `tests/test_web.py` holds the *keyboard* gate, and it needs no
browser: every command in `netgraph.web.bindings` must have a handler registered
in the page's JavaScript and every handler must have an entry in the table, no
chord may be bound twice in one scope, and `docs/commands/web.md` must contain
the table as generated. A shortcut that is documented and dead fails there, a
second after it is typed.

### Two properties every test here has

**The console is an assertion.** Every message the page logs is collected, and a
test fails if any of them was an error — an uncaught exception, a 404 for an
asset, a fetch that came back wrong. That one check catches most asset
regressions without anybody having to predict them, and it is why the tests read
as short as they do. A test that drives a refusal *on purpose* allows it by
status (`editor.console.allow("403")`), so the allowance is visible where it is
needed rather than global.

**A failure leaves evidence.** A screenshot, the page's HTML and the whole console
log are written under `.browser-artifacts/` for any test that fails —
`NETGRAPH_BROWSER_ARTIFACTS` moves that, and the `browser` job in
`.github/workflows/ci.yml` points it at a directory it uploads. A browser failure
nobody can reproduce is a browser failure nobody fixes.

### Debugging one

```console
$ pytest -m browser --no-cov -k reveals -x
$ PWDEBUG=1 pytest -m browser --no-cov -k reveals -x   # opens the inspector
```

`PWDEBUG=1` is Playwright's own switch: it runs headed, pauses on every action
and lets you step through the test against the live page. The temporary
inventory each test copies is a real directory under pytest's `tmp_path`, so
whatever the test wrote is still there to look at afterwards.

[axe]: https://github.com/dequelabs/axe-core
[playwright]: https://playwright.dev/python/

## The language server

`tests/test_lsp.py` is the browser layer's opposite number for
[`netgraph lsp`](lsp.md), and it is built on the same principle: a component
defined by what it puts on a wire has to be tested through that wire.

Every test drives a real
[`LanguageServer`](../src/netgraph/lsp/server.py) over real pipes, in
`Content-Length` frames, with a forty-line client in the module itself. Nothing
calls a handler directly. That is not thoroughness for its own sake — the bugs
that make a language server work in one editor and hang in the next all live in
the layers a direct call skips: the framing, the byte-versus-character length,
the gate that refuses everything before `initialize`, and the order a
notification arrives in relative to a response.

Two of them are worth knowing about because they are easy to write badly:

* **Waiting for the right publication.** Diagnostics are published when the
  server's queue runs dry, so an edit may legitimately produce two publications
  for one file. `Driver.wait_for_diagnostics` takes a predicate, and a test that
  expects an edit to *change* the answer passes one. Asserting on the next
  publication instead is a race that passes locally and fails in CI.
* **A buffer shadows the file it is opened over.** Opening a document with
  invented text takes the elements that file declared out of the tree — which is
  correct, and which makes a test that opens a made-up cable over
  `cables/links.yaml` and then expects to complete against those cables a test
  about nothing. The completion tests open new paths for that reason.

`TestCommand` runs the real `netgraph lsp` as a subprocess, once with the pipe
closed and once with it deliberately left open. The second is there because a
client is entitled to send `exit` and keep stdin open; before
`netgraph.cli._exit_from_stdio` existed, that shut the interpreter down with a
thread inside a blocking read and aborted the process, which an editor reports
as a crash immediately after a clean shutdown.

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

`tests/test_fixes.py` asserts the one the repair path lives or dies by:

| Property | What it rules out |
|---|---|
| repairing an inventory leaves no rule reporting more than it did | a `--fix` that trades one problem for another |

The generated inventories are *valid*, so on their own they would only exercise
the empty path. Each example is given a layout document placing elements nobody
declared, which guarantees at least one repair per run and leaves whatever the
generator happened to produce to be quantified over as well. The same statement
is asserted example-wise over every inventory in the repository — `examples/`,
`tests/fixtures/fixable/` and every single-rule fixture in
`tests/fixtures/invalid/`.

`tests/test_fuzz_loader.py` covers the one component with a real trust
boundary. The loader reads files a user did not write — `netgraph import`
output, a third-party inventory, a generated tree — so the contract there is not
"parses correct input" but *terminates, fails structurally, bounds its
diagnostics, bounds its memory*. The seed corpus is `tests/fuzz-corpus/`: one
file per way of being wrong, mutated by the test into near misses.

## The performance guards

Two files stop an optimisation from being given back unnoticed, and both run in
the ordinary `pytest` invocation — there is no separate job to forget to look at.

`tests/test_performance.py` guards the two stages every command pays for,
loading and validating, as **ratios** against a floor measured in the same
process: `load_tree` against a raw parse of the same tree, `validate` against a
plain walk over every address. A wall-clock ceiling would be worthless on a
shared runner; a ratio cancels the machine out.

`tests/test_editor_performance.py` guards the editor's own loop — the round trip
between changing one field and the diagram agreeing again — and guards it mostly
by **counting**, because most of what regressed there is countable:

| Guarded | Catches |
|---|---|
| one edit parses one file | the parse cache dropped from the write path |
| one edit validates three times | a request handler grading the tree for itself |
| an answer carries at most 200 problems | a payload that grows with the inventory |
| separating 4× the nodes costs ≤ 6× the comparisons | the overlap sweep going quadratic |
| a half-arranged layout costs ≤ 6× an unarranged one | the layout probe routing edges it discards |

An integer is the same integer on a laptop and on a runner having a bad minute,
which is why the counts are preferred wherever counting will do.

Every guard in both files prints what it measured whether or not it passed —

```
[perf] validate: 8.20x against a budget of 9.50x (14% headroom)
[perf] partial layout: 2.12x the auto layout (399 ms against 188 ms) …
```

— and the CI job collects those lines into its step summary, so the next
recalibration reads a spread off six green runners rather than off the one that
failed. The numbers behind each threshold, and what reverting each optimisation
does to them, are in [`follow-ups.md`](follow-ups.md).

`tools/bench_editor.py` is the harness the editor's figures come from: it starts
the real server over a generated tree of any size, drives it with the same
Chromium the browser layer uses, and reports cold open, the re-render after one
field, the write-to-canvas latency, the tab's heap and DOM, a fifty-node move
and how much of the drawing is being materialised. It is not part of the suite —
it takes a couple of minutes and needs a browser — but it is what a threshold
here should be re-measured with before it is changed.

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
