# Contributing to netgraph

This is the practical guide: how to get a checkout running, how to run every gate
exactly as CI runs it, and step-by-step recipes for the two changes people most
often want to make — adding a validation rule and adding an output format. For
what the code *is* — the pipeline, who owns which stage, what each stage may
assume — read [docs/architecture.md](docs/architecture.md) first.

## Contents

- [Getting a checkout running](#getting-a-checkout-running)
- [The gates](#the-gates)
- [pre-commit](#pre-commit)
- [The test suite](#the-test-suite)
- [Generated artefacts](#generated-artefacts)
- [Documentation conventions](#documentation-conventions)
- [Recipe: add a validation rule](#recipe-add-a-validation-rule)
- [Recipe: add a renderer](#recipe-add-a-renderer)
- [Profiling and benchmarks](#profiling-and-benchmarks)
- [Commits and pull requests](#commits-and-pull-requests)
- [The changelog](#the-changelog)
- [See also](#see-also)

---

## Getting a checkout running

Python 3.10 or newer, and **Graphviz** — the `graphviz` package on PyPI is only a
wrapper around the `dot` binary, which it does not bundle. Without the system
package the SVG, PNG, PDF and HTML tests fail.

```bash
sudo apt-get install --no-install-recommends graphviz   # or: brew install graphviz
dot -V
```

Then a virtual environment and an editable install with the dev extras:

```bash
python -m venv .venv
. .venv/bin/activate
pip install --editable '.[dev]'
```

That pulls in pytest, pytest-cov, ruff, mypy, hypothesis, jsonschema, pre-commit,
the `graphviz` wrapper (used by the tests as an independent second opinion on the
layout) and `types-PyYAML`. Check the console script is wired up:

<!-- run: cwd=examples/quickstart -->
```console
$ netgraph validate
no problems found
```

CI runs on `ubuntu-24.04` against Python 3.10, 3.11 and 3.12, and one extra job on
3.12 with `NETGRAPH_YAML_LOADER=python` to keep the pure-Python YAML parser from
becoming dead code. If you touch `loader/documents.py` or the model layer, run
your tests both ways:

```bash
NETGRAPH_YAML_LOADER=python pytest
```

It also runs the whole suite on `windows-latest` and `macos-14`, on 3.12 each.
[`docs/testing.md`](docs/testing.md#platforms) says what each platform covers and
which six tests are skipped on Windows, with the capability each one needs. Two
rules follow from it and are worth knowing before you write the code, not after
CI tells you:

* **Write files through `netgraph.fsio`**, never `Path.write_text`. It is the one
  call that silently varies by platform — Python's text mode translates `\n` to
  `\r\n` on Windows — and the canonical form `netgraph fmt` enforces is defined in
  bytes. `tests/test_platform.py` fails if a new call site reintroduces it.
* **Skip on a capability, not on a platform.** The marks in
  `tests/platform_marks.py` are per capability (`requires_symlinks`,
  `requires_mkfifo`, …) and carry their reason. A bare
  `skipif(sys.platform == "win32")` is how "the platform lacks this" quietly
  becomes "netgraph is broken here and nobody is looking", so it is a test failure
  rather than a convention.

## The gates

These are the commands `.github/workflows/ci.yml` runs, in the order it runs
them. All of them must pass; **CI must be green before a pull request is
merged.**

```bash
ruff check .                    # CI adds --output-format=github
ruff format --check .           # CI adds --diff, to put a patch in the log
mypy src/                       # strict; CI adds --python-version per matrix entry
```

`ruff` and `mypy` read their whole configuration from `pyproject.toml`, so there
are no flags to remember: line length 100, the `E W F I B C4 UP SIM RUF` rule set
with `E501` left to the formatter, and `strict = true` with
`warn_unreachable = true` over `src/netgraph`, `tests` and `tools`. Note that
`ruff format` also formats Python code blocks **embedded in Markdown**, so a
snippet in a document can fail the gate.

The YAML under `examples/` is documentation, and documentation that does not
follow the format netgraph documents is documentation arguing with itself — so
there is a fourth lint gate:

<!-- run: cwd=. -->
```console
$ netgraph fmt --check examples
0 file(s) would be reformatted, 45 already formatted
```

Use `netgraph fmt --diff examples` to see what differs and `netgraph fmt examples`
to apply it. See [docs/format.md](docs/format.md) for the canonical form itself.

Then the suite, with the coverage floor. CI spells the flags out even though
`addopts` and `fail_under` in `pyproject.toml` already carry them, so that the
command is correct on its own and a local edit cannot quietly lower the gate:

```bash
pytest \
    --cov \
    --cov-fail-under=85 \
    --cov-report=xml:coverage.xml \
    --cov-report=html:htmlcov \
    --junitxml=junit.xml
```

A plain `pytest` is the same run with the terminal report instead: coverage is on
by default so that the threshold cannot be forgotten. The floor of 85% is there
to catch a regression, not to describe the current state — the suite sits well
above it.

When you run a *subset*, the gate will trip on the modules you did not touch.
Pass `--no-cov` for those runs:

```bash
pytest tests/test_render.py --no-cov
```

CI also has four jobs beyond `test`: `discover-examples` and `validate-examples`
run the composite action, the SARIF upload and the annotation format over every
inventory under `examples/` (so a broken integration breaks here rather than in
somebody else's pipeline), `render-examples` installs netgraph **without** the
dev extras and renders every example to SVG, checking that a plain
`pip install netgraph` can draw the documented inventories, and `docker` builds
the image and drives all three services of `docker-compose.yml` — the CLI, the
editor and the live preview — because a compose file that parses is not a compose
file that works. See [docs/ci.md](docs/ci.md) and [docs/docker.md](docs/docker.md).

## pre-commit

`.pre-commit-config.yaml` is a local mirror of the lint gates. Installing it is
optional but saves a round trip:

```bash
pre-commit install
pre-commit run --all-files
```

Two deliberate differences from CI, neither of which changes the verdict: the
ruff hooks run with `--fix` because locally a repair is more useful than a
report, and they add `markdown` to `types_or` because CI runs `ruff check .` /
`ruff format --check .` over everything and ruff handles Python embedded in
Markdown. Everything else comes from `[tool.ruff]` in `pyproject.toml`, so the
rule set cannot drift between the two. Keep the `ruff` rev in
`.pre-commit-config.yaml` in step with the `ruff` pin in the `dev` extra, or the
hook and CI can disagree about what "formatted" means.

There is also a `check-yaml` hook over `examples/`, because a YAML syntax error
in an inventory otherwise surfaces as a loader failure well after the commit.

Do not confuse `.pre-commit-config.yaml` with `.pre-commit-hooks.yaml`: the
latter is what netgraph *publishes*, the `netgraph-validate` hook that other
people's inventory repositories install.

## The test suite

The layout is flat: one `tests/test_<area>.py` per area, plus

* `tests/conftest.py` — the shared fixtures and the Hypothesis profile registration;
* `tests/strategies.py` — the inventory strategies the property tests draw from;
* `tests/fixtures/invalid/` — exactly one minimal document per validation rule;
* `tests/fixtures/golden/` — the committed renderer snapshots;
* `tests/fixtures/export/` — the committed export artefacts;
* `tests/fuzz-corpus/` — one seed file per way of being wrong, for the loader fuzzer.

Two files are not example tests. `tests/test_properties.py` and
`tests/test_fuzz_loader.py` state what netgraph may *never* do, for every input
rather than for the ones somebody thought of, and how hard they search is a
Hypothesis profile chosen with `NETGRAPH_HYPOTHESIS_PROFILE`: `dev` (25 examples)
is the default, CI runs `ci` (50) and the nightly workflow runs `deep` (1000).

```bash
NETGRAPH_HYPOTHESIS_PROFILE=deep pytest tests/test_properties.py --no-cov
NETGRAPH_HYPOTHESIS_PROFILE=deep pytest tests/test_fuzz_loader.py --no-cov
```

Run the deep profile before trusting a property you have just written: a property
that holds for 25 examples and fails for 300 is worse than no property, because it
will fail on somebody else's pull request. [docs/testing.md](docs/testing.md) has
the full list of properties, the profile table and how to reproduce a failure from
the example database.

## Generated artefacts

Four things in the tree are derived from the code, and a test fails when any of
them is stale — so a change that is not reflected in its generated artefact fails
the build rather than shipping a reference that has drifted.

```bash
python tools/gen_schema_reference.py     # regenerate docs/schema-reference.md
python tools/gen_json_schema.py          # regenerate schema/netgraph.schema.json
python tools/gen_docs.py                 # regenerate the generated regions in docs/
python tools/check_examples.py           # run every documented netgraph example
```

**`tools/gen_schema_reference.py`** writes `docs/schema-reference.md` from
`model_fields` plus the prose and YANG paths in
`netgraph.models.fielddocs.FIELD_DOCS`. A field with no entry, or an entry naming
a field that no longer exists, aborts the generator rather than producing a
quietly incomplete document. `--check` exits 1 instead of writing;
`tests/test_docs.py` runs that path.

**`tools/gen_json_schema.py`** writes `schema/netgraph.schema.json` from
`netgraph.schema.build_schema()`. `--check` is the same drift guard, run by
`tests/test_schema.py`; `--kind` emits the schema for one kind to somewhere else.

**`tools/gen_docs.py`** rewrites the machine-derived *regions* of the
documentation in place — the ones fenced by
`<!-- generated: … -->` … `<!-- /generated -->`. Region kinds are
`synopsis <command path>`, `options <command path>`,
`arguments <command path>`, `command-index base=<prefix>` and `rule-index`; the
first three are read off Click's own decorators, the last off
`netgraph.rules.RULES`. Everything outside the markers is prose written by a
human. `--check` exits 1 if a region is out of date, and `tests/test_docs.py`
runs it, so a flag added to the CLI without regenerating the docs fails the
suite.

**`tools/check_examples.py`** executes the documentation. Every fenced
`console`/`bash`/`sh`/`shell` block that invokes `netgraph` must carry a marker on
the line above it, and the tool runs the `run` ones and diffs what they print. Use
`--list` to see what is checked and what is excused, `--update` to rewrite the
output of failing `run` blocks, and a list of paths to narrow it to some files:

```bash
python tools/check_examples.py --list
python tools/check_examples.py docs/architecture.md CONTRIBUTING.md
```

Two more, neither of them a gate. The committed diagrams are rendered from the
checked-in examples:

<!-- norun: rewrites the committed SVGs under docs/images/ -->
```bash
netgraph -i examples/home-lab render --layer l2 --title "home-lab — layer 2" \
    -f svg -o docs/images/home-lab.svg
netgraph -i examples/quickstart render -f svg -o docs/images/quickstart.svg
netgraph -i examples/home-lab render --layer l3 --title "home-lab — layer 3" \
    -f svg -o docs/images/home-lab-l3.svg
netgraph -i examples/home-lab render --layer l2 --icons cisco \
    --title "home-lab — layer 2, cisco icons" -f svg -o docs/images/home-lab-icons.svg
```

And the bundled icons are drawn as SVG and committed alongside a PNG of each,
since Graphviz cannot read an SVG image in its cairo-backed outputs. After
editing one, re-run the rasteriser — `--check` reports staleness without writing:

```bash
pip install cairosvg                     # only this tool needs it
python tools/render_icons.py
```

## Documentation conventions

* **Every netgraph example carries a marker.** Immediately above the fence (blank
  lines allowed), exactly one of:

      <!-- run: cwd=examples/quickstart -->
      <!-- norun: starts a server and never exits -->

  A `run` block is *executed by the test suite* and its transcript must match byte
  for byte. `cwd=` is relative to the repository root (`cwd=.` is the default and
  may be omitted); add `rc=1` to assert the exit code of the last command. A line
  containing only `...` matches any run of output lines. Each command line starts
  with `$ ` and must be a bare `netgraph …` invocation — no pipes, no `&&`, no
  redirects, no environment prefixes. A `norun` needs a real reason: it needs a
  live device, it writes into the reader's directory, it starts a server, the
  paths are illustrative, or it uses a shell pipeline. The point of requiring one
  of the two on every block is that neither state is the silent default.

  Produce a transcript by running the command rather than by typing it:

  ```bash
  NO_COLOR=1 COLUMNS=80 python -m netgraph -i examples/home-lab list vlans
  ```

* **Command flag tables are generated, never typed.** Pages under
  `docs/commands/` carry the `synopsis`, `arguments` and `options` regions
  described above. Write the prose a table cannot carry — what the command is
  for, when you want it, how the important flags interact, one worked example —
  and leave the regions to `tools/gen_docs.py`.

* **Relative links only, and they must resolve, anchor included.**
  `tests/test_docs.py` walks every link and every `#fragment` in every Markdown
  file in the repository.

* **Register.** Precise, plain, second-person; explain *why* before *what*. No
  marketing, no emoji, no "simply". Prose wrapped at about 88 columns, `—` for em
  dashes. [docs/paths.md](docs/paths.md) and [docs/format.md](docs/format.md) are
  the pages to imitate.

---

## Recipe: add a validation rule

Five steps, four files. Skip any one of them and a named test fails — which is the
point: a rule cannot ship undocumented, unfixtured or unnumbered. Follow `W133`
(*patch run stops inside the panel*) through the tree as the worked example.

**1. Allocate the id in `src/netgraph/rules.py`.** Append a `Rule` to the `RULES`
tuple, after the last rule of the same severity class. Ids are permanent: take the
next free number, never reuse one, never renumber.

```python
Rule(
    "W133",
    Severity.WARNING,
    "A cabled patch-panel position is coupled to one nothing is patched into.",
    ("NG-P002",),
    title="patch run stops inside the panel",
)
```

The letter is the *default* severity — `E` error, `W` warning, `I` info — and the
rule keeps its id when an inventory re-grades it. The tuple is the `NG-*` alias
from `docs/schema.md` §10, which keeps the published specification and the
implementation from drifting apart; both spellings are accepted everywhere a rule
can be named. `title` is what `Rule.anchor` and `Rule.help_uri` build the deep
link out of, so it must match the heading you write in step 3 exactly.

*Fails without this step:*
`tests/test_validate.py::test_every_rule_has_a_check_and_a_unique_id`.

**2. Implement the check in `src/netgraph/validate.py`.** Write a
`Callable[[_Context], Iterator[_Draft]]` next to the other checks of its section,
and register it in `_CHECKS` **in the same position the rule has in `RULES`**:

```python
_CHECKS: Final[tuple[tuple[str, Check], ...]] = (
    # ... the checks before it, in RULES order
    ("W133", _check_dangling_patch),
    # ... the checks after it
)
```

Read what `_build_context` already resolved — `ctx.endpoints`,
`ctx.panel_terminations`, `ctx.placements`, the subnets — instead of re-resolving
a reference; that is what keeps a finding and a diagram in agreement. Yield one
`_Draft` per problem, naming *every* element involved (a finding is suppressed by
an annotation on any of them) and carrying the `field_path` of the value at fault
so the diagnostic can point at a line. Never inspect the configuration: the engine
skips disabled rules and drops annotated findings for you. The docstring is where
the reasoning goes — `_check_dangling_patch` explains why half a patched run is a
warning rather than an error.

*Fails without this step:* the same
`test_every_rule_has_a_check_and_a_unique_id`, which asserts
`[rule_id for rule_id, _ in _CHECKS] == list(RULE_IDS)`.

**3. Write the section in `docs/validation-rules.md`.** Under the right severity
heading, in id order, with the shape every other rule uses:

```markdown
#### `W133` — patch run stops inside the panel

*Alias: `NG-P002`. Severity: warning.*

A patch-panel position terminates a cable, and the position its coupler leads
to terminates none.

**Why it matters.** …

**Suppress with** `W133` / `NG-P002`, or an annotation on the cable or the
panel. …
```

The `####` heading text must be exactly `` `<id>` — <title> ``, because `Rule.anchor`
computes the fragment from the id and the title with the same slug rule GitHub uses:
lower-cased, everything that is not a word character, a space or a hyphen dropped,
spaces turned into hyphens — the em dash leaving the doubled hyphen in
`w133--patch-run-stops-inside-the-panel`. That anchor is the SARIF `helpUri` and the
GitHub annotation title, so a heading reworded without touching the catalogue ships
links that 404 in somebody's code-scanning UI.

*Fails without this step:* `tests/test_docs.py::test_every_rule_is_documented`
(no section, or a severity or alias the section does not mention),
`::test_every_rule_title_matches_its_heading` (the anchor does not resolve) and
`::test_the_rule_document_explains_how_to_suppress_each_rule` (no
**Suppress with** line).

**4. Add a fixture in `tests/fixtures/invalid/`.** One file, named
`<lowercase id>-<slug>.yaml` — `w133-patch-run-stops-in-panel.yaml` — that is
**schema-valid** (it loads with no `LoadError` at all) and produces **exactly
one** finding, of exactly this rule. Add its row to
`tests/fixtures/invalid/README.md`, whose table names the file, the rule, the
schema id and the trigger in one sentence.

*Fails without this step:*
`tests/test_examples.py::test_there_is_one_invalid_fixture_per_rule`, and then
`::test_an_invalid_fixture_is_schema_valid`,
`::test_an_invalid_fixture_triggers_exactly_its_own_rule` and
`::test_an_invalid_fixture_names_the_elements_it_blames`.

**5. Add a behaviour test.** The fixture proves the rule fires once on one
document; a test in `tests/test_validate.py` (or the area file — `W133` lives in
`tests/test_patchpanels.py`) proves it fires on the case you meant and stays quiet
on the near miss. Assert on the finding's `rule`, its `elements` and, where the
wording carries the diagnosis, its `message`.

Finally: run `python tools/gen_docs.py` so the `rule-index` region picks the rule up,
and confirm the shipped inventories still validate clean under the new rule — the
`validate-examples` job and `tests/test_examples.py` both assume they do.

<!-- run: cwd=. -->
```console
$ netgraph -i examples/campus validate --strict
no problems found
```

---

## Recipe: add a renderer

A new output format is one module and one registry entry. Nothing else should need
to change, and if it does, that is a bug in
[`render/registry.py`](src/netgraph/render/registry.py)'s design rather than a
reason to add a branch.

**1. Write the backend.** A new module under `src/netgraph/render/`, exposing one
function with the shape the registry expects:

```python
def to_graphml(graph: Graph, options: RenderOptions | None = None) -> str: ...
```

The contract is the whole of stages 3 to 5 in
[docs/architecture.md](docs/architecture.md#stage-5-render): you get a frozen
`Graph` whose every edge references two nodes that exist, and a `RenderOptions`
that says how much detail to draw — `show_ips`, `show_vlans`,
`group_by_namespace`, `title`, `max_addresses`, `rankdir`, `icons`, `tooltips`,
`link_template`, `element_ids`, `highlight`. Honour what your format can express
and ignore the rest; `RenderOptions` says *what to draw*, never *what exists*, so
none of it may change the topology. Do not read the inventory, the filesystem or
`netgraph.toml`, and iterate `graph.nodes` and `graph.edges` in the order they come
in — output must be byte-for-byte reproducible.

For a binary format, produce `bytes` and leave `to_text` unset; `dot.py` is the
model, and the three image formats are one `_image_renderer` call each because
they all lay the graph out through it.

**2. Register it in `src/netgraph/render/registry.py`.** One entry in `RENDERERS`,
in help-text order, declaring everything a front end could ask:

```python
_text_renderer(
    "graphml",
    "GraphML, for yEd and Gephi",
    ".graphml",
    "application/graphml+xml",
    to_graphml,
    draws_racks=False,
)
```

The fields to think about are `suffix`, `media_type`, `binary` (SVG is an image
but text on the wire, so this is narrower than "not a text format"),
`supports_icons`, `interactive` (does it carry tooltips, links and element ids?),
`supports_highlight`, `draws_racks` (a rack elevation is a *grid*, so a format
whose node label is a caption should say `False` rather than emit a box that
silently omits the empty units), `to_document` (only `html` can hold several
layers), `csp` and `advise` for size-dependent warnings. Export the public names
from `render/__init__.py`.

**3. The `-f` choice needs no work.** `cli.py` builds it from
`FORMATS = tuple(RENDERERS)` and its help text from `_describe_formats()`, and
`--icons`, `--highlight` and `--layer` are all filtered through `supports_icons()`,
`supports_highlight()` and `supports_layers()`. What *is* worth checking is that the
derived lists came out right. `netgraph render --layer rack -f graphml` should either
work or refuse with a message that names the formats which can draw an elevation;
`netgraph path --highlight -f graphml` should offer the format only if you set
`supports_highlight=True`, because `path`'s `-f` choices are `HIGHLIGHT_FORMATS`; and
`netgraph watch -f graphml --serve` should serve it under your `media_type`.

**4. Golden fixtures.** If the format is text, add it to `FORMATS` in
`tests/test_golden.py` and regenerate:

```bash
pytest tests/test_golden.py --regen-golden
```

Regeneration rewrites every snapshot, so the resulting `git diff` *is* the review.
Check the new files into `tests/fixtures/golden/`, one per `Case` that lists your
format, and confirm they embed nothing machine-specific — `Graph.root` is
deliberately absent from every format, which is what makes a golden identical on
every checkout. Add a parse assertion beside the existing ones
(`test_the_mermaid_golden_declares_a_flowchart` is the pattern) so a snapshot that
matches but is not valid in your format still fails, and extend
`tests/test_render.py` with the facts you claim reach the output.

The property tests need one edit each. `TEXT_FORMATS` in `tests/test_properties.py`
is derived from `RENDERERS`, so `test_every_text_renderer_completes_and_parses`
picks a new text format up on its own — but its `_parse` helper dispatches on the
format name, so add a branch there or the output goes unchecked. The escaping
properties name `dot`, `mermaid` and `json` explicitly; add yours, because "no free
text and no name can become *syntax*" is the property a new grammar most needs.

**5. Documentation.** Mention the format in
[docs/rendering.md](docs/rendering.md), and add a page under `docs/commands/` only
if it grows options of its own. The `-f` table in
[docs/commands/render.md](docs/commands/render.md) is a generated region, so run
`python tools/gen_docs.py` and commit the result rather than editing it. If you add
a worked example, mark it (`run` where the output is reproducible, `norun` with a
reason otherwise) and check it with `python tools/check_examples.py`.

---

## Profiling and benchmarks

None of these are gates; they are how a performance claim gets made.

```bash
# generate a 1000-device inventory and time every stage over it; --compare-loaders
# additionally times the parse step through both YAML parsers
python tools/bench_pipeline.py --compare-loaders

# break the cost of `validate` down by rule over the same tree
python tools/profile_validate.py --top 10

# measure what an interactive HTML page costs, and how that cost grows
python tools/bench_html.py --breakdown

# capture every command's output over every inventory in the repository, on both
# YAML parser paths, so a refactor can be shown to have changed none of them
tools/snapshot_outputs.sh /tmp/before && tools/snapshot_outputs.sh /tmp/after
diff -r /tmp/before /tmp/after
```

A measured result belongs in [docs/follow-ups.md](docs/follow-ups.md), which is
also where a gap you are deliberately *not* closing goes. Each entry records what
was measured, why it was not fixed in place, and what a fix would have to do; an
entry that is later closed keeps its place in the list, rewritten with what was
actually achieved, so a number in it can be compared with the next one. Add the
follow-up in the same pull request as the decision it records — a `pyproject.toml`
comment or a module docstring may cite the entry number, as the `ruamel.yaml`
dependency cites entry 9.

## Commits and pull requests

`git log` is the register to match. A subject line is an imperative sentence
saying what the change achieves, capitalised, no trailing full stop, no prefix or
tag:

```text
Add `netgraph export`, five operational artefacts from one inventory
Model passive patch panels and rack placement
Cut validate by 3.1x, profile-driven
Stop an HTML page growing with the number of views
Format the Python block in docs/export.md
```

The body carries the reasoning, wrapped at about 76 columns: what was wrong, what
was measured, what was decided and what was deliberately left out. A substantial
change uses `##` sections — *What was added*, *The six bugs, all fixed here* — and
a performance change quotes its before-and-after table. Say why a trade-off was
made, not just that it was; a reviewer reading it a year later is the audience.

Before opening the pull request:

1. every gate above passes locally, including `netgraph fmt --check examples`;
2. every generated artefact is regenerated and committed;
3. new behaviour has an example test, and a new invariant has a property test;
4. new documentation has its markers and `python tools/check_examples.py` reports
   0 failures;
5. anything a user would notice has a bullet under `## [Unreleased]` in
   [CHANGELOG.md](CHANGELOG.md) — see [The changelog](#the-changelog);
6. CI is green. It has to be — the generator drift checks, the link checks and the
   documented transcripts are all part of the suite, so a red run means something
   in the tree contradicts something else in the tree.

## The changelog

[CHANGELOG.md](CHANGELOG.md) has an `## [Unreleased]` section at the top. A change a
*user* would notice goes there in the same pull request, under `### Added`,
`### Changed`, `### Fixed` or `### Removed`:

* a command, a flag, a schema field, a validation rule, an output format, an exit code;
* a diagram that comes out different, or a diagnostic that now fires where it did not.

A refactor, a test, an internal performance win and a documentation edit do not, unless
they change one of the above. `git log` is the record for those, and a changelog padded
with them is a changelog nobody reads.

Two entries are not optional, because a reader of the changelog is deciding whether to
upgrade:

* **A breaking change to a public surface** — the CLI, the `netgraph.dev/v1alpha1`
  schema, a JSON output document, an exit code, a rule id, a published integration.
  It needs a `### Changed`/`### Removed` bullet naming the old shape and the new one,
  plus a migration line saying literally what to edit. The full list of public surfaces
  and the four things such a change needs are in
  [docs/releasing.md](docs/releasing.md#what-is-public-api).
* **A fix for something previously released**, so somebody on the old version can tell
  whether it was their bug.

The release workflow refuses to publish a version whose changelog section is missing or
empty, and [tests/test_release.py](tests/test_release.py) checks the file's shape on every
pull request — so an entry forgotten here is caught long before a tag is pushed.
[docs/releasing.md](docs/releasing.md) has the rest: the versioning policy and how a
release is actually cut.

## See also

* [docs/architecture.md](docs/architecture.md) — the pipeline, the module map and
  the invariants a change must not break.
* [docs/testing.md](docs/testing.md) — the property and fuzz halves of the suite,
  the Hypothesis profiles and how to reproduce a failure.
* [docs/getting-started.md](docs/getting-started.md) — netgraph from a user's side,
  which is worth reading once before changing it.
* [docs/follow-ups.md](docs/follow-ups.md) — the deferred-work list, and the format
  an entry takes.
* [docs/releasing.md](docs/releasing.md) — what a `0.x` version promises, which surfaces
  are public API, and the mechanics of cutting a release.
