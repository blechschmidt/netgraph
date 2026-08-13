# How netgraph is put together

This is the orientation note for somebody about to change the code. It says what the
pipeline is, which module owns each stage, what each stage may assume about its input and
what it promises the next one — so that a change can be made in one place instead of five.
For how to build, test and submit it, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Contents

- [The pipeline](#the-pipeline)
- [Stage 1: load](#stage-1-load)
- [Stage 2: validate](#stage-2-validate)
- [Stage 3: build the graph](#stage-3-build-the-graph)
- [Stage 4: narrow and summarise](#stage-4-narrow-and-summarise)
- [Stage 5: render](#stage-5-render)
- [The side branches](#the-side-branches)
- [Cross-cutting pieces](#cross-cutting-pieces)
- [Module map](#module-map)
- [Using it as a library](#using-it-as-a-library)
- [Design rules a change must not break](#design-rules-a-change-must-not-break)
- [See also](#see-also)

## The pipeline

Five stages, each a plain function, each independently testable. The type on each arrow is
the whole contract between one stage and the next.

```text
     Path
      │   loader.load_tree / load_stream
      │   walk, parse, expand ranges, merge templates, index by qualified name
      ▼
  Inventory ───────► validate.validate ───────► list[Finding]
      │                                              │
      │                                              ▼
      │                                   diagnostics.build_report
      │                               (text | json | sarif | github)
      │   render.graph.build_graph
      │   resolve names, VLANs, tunnels, panels, prefixes — exactly once
      ▼
    Graph
      │   render.graph.filter_graph          remove, …
      │   render.aggregate.aggregate_graph   … then summarise, in that order
      ▼
    Graph
      │   render.render(graph, format, options)
      ▼
  str | bytes
```

`cli._build_graph` is the only place the CLI knows this order; `watch.pipeline.run_cycle`
and `web.preview.render_source` walk the same five stages for their own front ends. Nothing
else re-implements it.

## Stage 1: load

**Owner:** `src/netgraph/loader/` — `tree.py` (the walk), `documents.py` (the strict YAML
parser), `inventory.py` (the index), `cache.py` (parsed files, remembered by content), plus
`ranges.py`, `templates.py`, `provenance.py`, `ignore.py`.

```python
def load_tree(
    root: Path, *, keep_provenance: bool = False, cache: DocumentCache | None = None
) -> Inventory: ...
def load_stream(
    text: str, *, name: str = STREAM_NAME, keep_provenance: bool = False
) -> Inventory: ...
```

`cache` makes a *repeated* load incremental without making it stateful: every file is still
read and hashed on every load, so the tree on disk remains the only thing that decides the
result, and what is skipped is turning bytes that have been seen before back into elements.
A hit is indistinguishable from a parse — same elements, same diagnostics, same order — which
is what `tests/test_cache.py` asserts over every committed example. See
[`docs/configuration.md`](configuration.md#cache--remembering-parsed-files).

**May assume** nothing. `root` is checked, and a path that does not exist or is neither a
directory nor a YAML file is the *only* thing that raises (`LoaderError`). Everything a
user can get wrong *inside* the tree is data.

**Guarantees to every later stage.** *Every element is a validated model*, parsed against
the pydantic model of its `kind` before it is indexed, so a rule, a graph builder or a
renderer may rely on field types, bounds and enum membership without re-checking them; and
it is *qualified*, indexed under its directory plus `metadata.name`, so
`sites/berlin/rack1/sw1.yaml` becomes `sites/berlin/rack1/sw1`, with lookups going
namespace-first then global. *Shorthands are already gone*: `interfaces[].range` is
expanded and `spec.from` merged against its template during the load, so no later stage
re-parses YAML or re-expands anything — an inventory written with templates renders byte
for byte like the same inventory written out longhand. *Order is deterministic* — files in
byte-wise order of their relative POSIX path, documents in file order (`NG-L005`) — which
is what makes every rendering and every golden file stable. And loading is *total*, a
syntax error, schema violation, unreadable file or duplicate name being recorded on
`inventory.errors` as a `LoadError` while the walk continues, and *safe*, because only
`StrictSafeLoader` is used, so a hostile document cannot construct arbitrary Python
objects.

`keep_provenance=True` additionally records which file, line and column each *field* came
from, so a semantic finding can be reported at the line that caused it. It is off by
default because the redirect tables hold the YAML node trees alive — 18 MB retained instead
of 5 MB on a 628-element tree — and only the machine-readable `validate` formats need it.

## Stage 2: validate

**Owner:** `src/netgraph/validate.py`, with the catalogue in `rules.py` and the suppression
settings in `config.py`.

```python
def validate(inventory: Inventory, config: ValidationConfig | None = None) -> list[Finding]: ...
```

**May assume** the loader's guarantees: every document parses and matches the schema of its
own `kind`, so a check never re-validates a field. What it may *not* assume is that the
documents agree with each other — that is the question this stage asks.

**Guarantees.** It never raises for an inventory problem; it reports, and the caller
decides. It never mutates the inventory — nothing in `validate.py` calls `Inventory.add`
or `Inventory.record`, and
`tests/test_properties.py::test_validate_is_a_function_of_the_inventory_alone` pins it.
One problem is one finding: a duplicate address shared by five interfaces is a single
finding naming all five, anchored at the first declaration in load order. Order is
stable — source file, then position in the file, then severity and rule id. And
suppression is a filter rather than a branch: the engine skips disabled rules and drops
annotated findings afterwards, so a check cannot behave differently when a rule is
re-graded.

Each check is a generator `Callable[[_Context], Iterator[_Draft]]` registered in `_CHECKS`, a
tuple of `(rule_id, check)` pairs whose order must equal `rules.RULE_IDS`. `_build_context`
resolves every cable endpoint, tunnel end, attachment and placement once, so a check is a
loop over resolved records rather than a second resolver.

## Stage 3: build the graph

**Owner:** `src/netgraph/render/graph.py`.

```python
def build_graph(inventory: Inventory, *, layer: Layer = Layer.L1) -> Graph: ...
```

**May assume** an `Inventory` — but **not** that `validate` has run: a cable whose endpoint
does not resolve is dropped and recorded on `Graph.dangling`, because a half-written
inventory still has to draw.

**Guarantees.** `Graph` is frozen: nodes a mapping in load order, edges a tuple, and every
edge references two nodes that exist, so a renderer never has to check. Resolution happens
exactly once, here — name resolution, VLAN membership, adapter attachment, patch-panel
splicing, tunnel and encapsulation stacks and layer-3 prefix derivation are all this one
pass, which is why a diagram, a traced path, an export and a networkx view cannot disagree
about what is connected to what. `Graph.sources` says where each element was written, for
`--link-template`, and is deliberately not part of what a renderer draws.

`Layer` decides what the graph *is*: `physical` keeps patch panels and their segments, `l1`
splices each run into one edge, `l2` annotates that with VLAN membership, `l3` replaces
cables with prefixes as nodes, `overlay` makes tunnels nodes, and `rack` builds no topology
at all — one node per rack holding its elevation. `rack_elevations`, `resolve_tunnels` and
`splice_patch_panels` expose parts of the same pass.

## Stage 4: narrow and summarise

**Owners:** `render/graph.py` (filtering) and `render/aggregate.py` (aggregation).

```python
def filter_graph(graph: Graph, spec: FilterSpec) -> Graph: ...
def aggregate_graph(graph: Graph, spec: AggregateSpec | None = None) -> Graph: ...
```

**May assume** a `Graph` from stage 3, or from itself — both are `Graph → Graph`, so they
compose. **Guarantees** a `Graph` with the same invariants. The two run in this order and
only this order, and `cli._build_graph` says why: filtering decides *what exists*, aggregation
folds *what is left*. Reversed, `--kind switch` could empty a collapsed node of everything it
claims to stand for.

`FilterSpec` removes: `namespaces`, `vlans`, `kinds`, `names`, `neighbors_of`/`depth`. Values
within one field are alternatives, different fields are combined with AND, and every field
selects *elements* — so a derived layer-3 subnet node survives exactly as long as one selected
element still has an address in it. `AggregateSpec` removes nothing: `collapse` and
`collapse_depth` replace a namespace with one node that says which elements it stands for, and
`bundle` folds parallel links into one edge carrying the count. `aggregate_graph` returns
`graph` itself when nothing applies, so a pipeline that never aggregates is byte-identical.

## Stage 5: render

**Owner:** `src/netgraph/render/` — the registry in `registry.py`, the backends in `dot.py`,
`html.py`, `mermaid.py` and `jsonexport.py`, and the shared display decisions in
`options.py`, `details.py`, `ids.py`, `links.py`, `icons.py`, `highlight.py`, `fragment.py`.

```python
def render(graph: Graph, format: str, options: RenderOptions | None = None) -> bytes: ...
def render_text(graph: Graph, format: str, options: RenderOptions | None = None) -> str: ...
def render_layers(
    graphs: Sequence[Graph], format: str, options: RenderOptions | None = None
) -> bytes: ...
```

**May assume** a `Graph` and a `RenderOptions`, and nothing else: a backend never sees the
`Inventory`, never reads `netgraph.toml`, and never decides what exists.

**Guarantees.** `render` always returns `bytes` — text formats UTF-8 encoded, image formats
as Graphviz produced them — so a caller writing to a file or stdout needs no per-format
branching; `render_text` is for callers that want a string and have already excluded the
binary formats. Output is byte-for-byte stable for a given graph and options, which
`tests/test_golden.py` asserts and which is what makes `netgraph render -f dot >
topology.dot` a file worth committing.

`RENDERERS` in `registry.py` is the single declaration of a format. Every fact a front end
could want — suffix, media type, whether the output is binary, whether it can draw icons,
a highlight, a rack elevation or several layers at once, what content-security policy it
needs, what to warn about at a given graph size — is a field on its `Renderer`. No front
end branches on a format name: `-f`'s choices, the help text, the preview server's content
type and the size advisories all derive from that mapping.

## The side branches

Everything else hangs off one of the five stages and adds no sixth.

| Branch | Attaches after | Entry point |
|---|---|---|
| `diagnostics.py` | stage 2 | `build_report(inventory, findings, *, base=None)`, then `render_report(report, output_format)` |
| `subnets.py` | stage 1 | `subnets_of(inventory) -> tuple[Subnet, ...]` — the prefixes the configured addresses imply, one group per `(vrf, prefix)` |
| `ipam.py` | stage 1, via `subnets.py` | `build_report(inventory, config=None, *, aggregated=False)`; `conflicts()` calls `validate` rather than re-deriving anything |
| `graph.py` (top level) | stage 3 | `to_networkx(source, *, layer=None) -> nx.MultiGraph`, then its own `filter_graph`, `layers`, `broadcast_domains`, `stats` |
| `trace/` | stage 3 | `trace(inventory, source, destination, *, vlan=None, …) -> TraceResult` |
| `export/` | stage 4 | `export(export_format, context_factory) -> ExportResult`, over the same filtered `Graph` a diagram is drawn from |
| `listing.py` | stage 3 | `LISTINGS[subject](inventory) -> Listing` — the tables `netgraph list` prints, and the ones a report shows |
| `report/` | stages 2, 3 and 4 | `generate(inventory, *, options, diagnostics, …) -> (Bundle, Diagrams)` — the as-built document, built from `listing.py`, `ipam.py`, `export/cables.py`, `power.py` and the layer graphs |
| `fmt/` | before stage 1 | `format_paths(roots, *, mode) -> Summary` — its own round-trip parser, never on the loading path |
| `edit/` | before stage 1, gated on 1 and 2 | `EditSession(root).apply(operation)`, then `.diff()` / `.commit()` — the only write path, and the only thing that loads the tree *as it would be* through `loader.Overlay` |
| `importer/` | before stage 1 | `read_inputs` → `build_draft` → `build_files` → `write_files`, producing a tree stage 1 then loads |
| `schema.py` | stage 1's models | `build_schema(kind=None) -> dict[str, Any]` — the JSON Schema an editor consumes |
| `watch/` | all five | `run_cycle(request) -> CycleResult`, repeated by `run_watch`, published through `LiveRender`, served by `PreviewServer` |
| `web/` | all five, on a string | `render_source(source, view=None) -> Preview`, over `load_stream` rather than `load_tree` |

Four are worth a sentence more. `netgraph.graph` sits *beside* the renderers, not under
them: it hands the graph `build_graph` already resolved to networkx so that connectivity
questions are answered by graph algorithms, and it declares its own `filter_graph` — over
`nx.MultiGraph`, with keyword predicates rather than a `FilterSpec` — so check which one an
import means. `fmt/` is the one part that does not share a parser with the rest:
`ruamel.yaml`'s round-trip loader keeps the comments, blank lines and quoting style a
formatter cannot discard, every formatted document is handed back to the strict loader
before it is written (`fmt/verify.py`), and nothing on the loading path imports it, so
`validate` pays nothing for it. `edit/` is the mirror of `fmt/` and shares its parser for the same reason, but not its
remit: `fmt` rewrites whole files and `edit` re-emits only the documents an operation
named, so that every other byte of a file survives an edit. It is the one branch that runs
*two* stages of the pipeline as a check — it loads and validates the tree it would write,
through `loader.Overlay`, and refuses if the edit would introduce a new error. And
`export/` is scoped like a render on purpose: the same
`FilterSpec` narrows both, so `--namespace sites/north --kind switch` means the same thing
to a diagram and to a hosts file, and each emitter records what it had to drop
(`export/manifest.py`) instead of dropping it quietly.

## Cross-cutting pieces

**`errors.py` and `diagnostics.py` — diagnostics.** `errors.py` holds the exception hierarchy:
`NetgraphError` and its subclasses `ConfigurationError`, `LoaderError` (with
`SchemaError`/`SchemaIssue`), `ValidationError` and `RenderError`. The CLI catches the
base class, so a new failure mode gets a clean message by inheriting from it rather than
by adding a `try` in `cli.py`. It also owns the text helpers every diagnostic uses —
`count_text`, `clip_text`, `echo_value`, `compact_ids` — which is why no message
concatenates an unbounded value into itself. `diagnostics.py` turns load errors *and* findings
into one sorted stream of `Diagnostic` records and serialises it as JSON, SARIF 2.1.0 or
GitHub workflow commands; its `LOAD_RULE` pseudo-rule gives a *load* error a rule id and
a documented section too, so no diagnostic reaches a user without one.

**`settings.py` and `config.py` — `netgraph.toml` to Click parameters.** `config.py` reads
the file: `[validate]` (`ignore`, `severity`, `strict`) and, handing them straight over,
`[render]` and every `[profile.<name>]` block. Unknown keys inside a known table are
rejected — a misspelt `ingore = [...]` that silently did nothing would be worse than a
failed run — while unknown *top-level* tables are left alone so a file shared with a later
version still works. `settings.py` owns the one naming rule (**a key is the long flag
without its leading dashes**), the `SETTINGS` registry mapping each key to a Click
parameter and a parser, and the precedence ladder in `resolve_settings`: explicit flag,
then the selected profile, then `[render]`, then the Click default. Each result is a
`Resolution` carrying its `Origin`, which `netgraph config show` prints as provenance.

**`rules.py` — the single rule catalogue.** Every rule the validator can report is declared
here exactly once, with a permanent short id (`E###`, `W###`, `I###`), a default severity,
a one-line summary, its `NG-*` schema aliases and the `title` that `Rule.anchor` and
`Rule.help_uri` derive a deep link into [validation-rules.md](validation-rules.md) from.
Ids are permanent: once assigned, one is never reused, so a suppression in somebody's
inventory keeps meaning what it meant. Keeping the catalogue in its own module is what
lets `config.py` resolve and check rule ids without importing the validator.

Three smaller ones. `console.py` holds tables, colour and TTY detection, and a `Console` is
handed to a command rather than constructed by it, so `--quiet`, `--verbose` and `--color`
are honoured without each command re-deciding. `httpserve.py` holds what the two local
servers promise — loopback binding, the default content-security policy, security headers,
a Host check — because `watch` and `web` are different applications but what they promise
about *being a local server* has to be identical.

`fsio.py` holds the three questions about writing a file that must have the same answer
everywhere netgraph runs: what a line ending is (`\n`, never the platform's, because a
canonical form and a golden file are defined in bytes), how a file is replaced (through a
sibling temporary and `os.replace`, retried for the sharing violation only Windows
raises), and what a generated file may be called (not `nul.yaml`, which is a device on
Windows and not a file). Each of those had two implementations and one of them was
missing the newline argument, which is the shape of bug this module exists to make
impossible: `tests/test_platform.py` fails if any call site goes back to
`Path.write_text`.

## Module map

Verified against the tree: every path below exists.

| Path | What lives there |
|---|---|
| `docs/` | specification, generated reference, rule, CI and YANG guides |
| `examples/` | five runnable inventories, also used as golden fixtures |
| `schema/netgraph.schema.json` | the generated JSON Schema, for editors and CI |
| `.github/actions/netgraph-validate/` | the composite action that runs `validate` in a workflow |
| `.pre-commit-hooks.yaml` | the `netgraph-validate` hook, for inventory repositories |
| `tools/` | doc and schema generators (checked for drift by the tests), the example checker, the icon rasteriser, the pipeline and page benchmarks |
| `src/netgraph/__init__.py` | public package surface |
| `src/netgraph/cli.py` | console-script entry point (`netgraph`) |
| `src/netgraph/completion.py` | shell completion: the scripts, and the value completers |
| `src/netgraph/console.py` | terminal output: tables, colour, TTY detection |
| `src/netgraph/errors.py` | shared exception hierarchy, and the diagnostic text helpers |
| `src/netgraph/config.py` | per-inventory settings (`netgraph.toml`); `settings.py` owns the `[render]` table, the named profiles and the precedence ladder |
| `src/netgraph/scaffold.py` | the starter inventory `netgraph init` writes |
| `src/netgraph/httpserve.py` | what the two local servers promise: loopback, headers, host check, and the socket options that differ by platform |
| `src/netgraph/fsio.py` | one newline policy, one atomic replace, one reserved-file-name rule, for every platform |
| `src/netgraph/rules.py` | catalogue of validation rules and severities |
| `src/netgraph/diagnostics.py` | `validate` as json, SARIF 2.1.0 and GitHub workflow commands |
| `src/netgraph/schema.py` | JSON Schema emitted for editors (`netgraph schema`) |
| `src/netgraph/subnets.py` | IP prefixes derived from the configured addresses, partitioned by routing instance; `ipam.py` adds utilisation, free space and conflicts over them |
| `src/netgraph/validate.py` | semantic validation engine |
| `src/netgraph/graph.py` | the same resolved topology as a `networkx.MultiGraph`, for analysis |
| `src/netgraph/models/` | pydantic models for every element kind; `fielddocs.py` holds one prose description and YANG path per field, for both generators |
| `src/netgraph/loader/` | recursive YAML inventory loader: `tree.py` the walk and the two-phase build templates make necessary, `documents.py` the strict safe parser and the libyaml / pure-Python choice, `inventory.py` the index and `LoadError`, `ranges.py` bracket expansion of `interfaces[].range`, `templates.py` the registry and the spec merge, `provenance.py` which file and line each field came from, `ignore.py` `.netgraphignore` with gitignore semantics, `cache.py` the content-addressed store of parsed-and-validated files |
| `src/netgraph/render/` | graph construction and output renderers: `graph.py` turns an inventory into nodes, edges, VLAN membership and subnets and filters them, `aggregate.py` collapses namespaces and bundles links, `options.py` is `RenderOptions` (what to draw, never what exists), `registry.py` is one entry per format — the CLI reads it, never a list of names |
| `src/netgraph/render/dot.py` | Graphviz DOT and the SVG/PNG/PDF it produces, laid out by `templates/graph.dot.j2`; `html.py` the self-contained interactive page (`-f html`) from `templates/page.html.j2`; `mermaid.py` the flowchart exporter; `jsonexport.py` the canonical JSON graph |
| `src/netgraph/render/details.py` | per-element hover records and tooltip text; `ids.py` the stable id each drawn node, edge and cluster carries; `links.py` the `--link-template` URL back to the document; `highlight.py` the emphasis a reader asked for; `icons.py` icon themes (a directory of images named after element kinds, the bundled ones under `iconsets/`) |
| `src/netgraph/render/fragment.py` | the Graphviz SVG made embeddable, for the page and the preview; `assets/` holds the style sheet, the client and the record renderer `netgraph web` shares with it — inlined, never fetched |
| `src/netgraph/trace/` | reachability tracing (`netgraph path`): `endpoints.py` resolves what the user typed, `engine.py` searches layer 2 then layer 3, `model.py` holds the result, `report.py` renders it as text or JSON |
| `src/netgraph/export/` | `netgraph export`: six operational artefacts, with `context.py` for the values every emitter reads, `names.py` for folding a name into each target grammar, `manifest.py` for what was dropped |
| `src/netgraph/listing.py` | the tables of `netgraph list`, in a form a report can show: headers, alignment, formatted cells and the same rows as records |
| `src/netgraph/report/` | `netgraph report`: the as-built document. `collect.py` works out the scopes and the shared derivations, `pages.py` says what each page carries, `model.py` is the format-independent document, `layout.py` the file names and cross-references, `diagrams.py` the drawings and the links inside them, `write.py` the templates and the escaping, `bundle.py` the files and how they are written, `stamp.py` the timestamp and the git revision; `templates/` and `assets/` are the editable layout (`--template DIR`) |
| `src/netgraph/fmt/` | the canonical form of an inventory file (`netgraph fmt`): `canonical.py` shapes it, `order.py` holds the key order, `verify.py` re-reads it strictly, `runner.py` walks the paths |
| `src/netgraph/edit/` | the write path (`netgraph edit`, and the web editor to come): `operations.py` is the closed set of typed changes and their JSON form, `apply.py` turns each into a change and its inverse, `roundtrip.py` holds a file as documents that can be edited without touching the others, `references.py` reads the references off the models and re-spells them, `placement.py` decides where a new document goes, `tree.py` journals and hashes what may be written, `session.py` runs the validation and conflict gates |
| `src/netgraph/importer/` | `netgraph import`: a first inventory from live-network output. `run.py` reads the inputs, sniffs each dialect and writes the tree; `lldp.py` turns `lldpctl`/`lldpcli` neighbour records into cables, both ends at once; `iproute.py` turns `ip -j link`/`addr` into one host's interfaces, bridges, bonds and VLANs; `csvlinks.py` reads `device,port,device,port` rows (and says why not NetJSON); `draft.py` is the neutral inventory every reader appends to, and the dedup; `emit.py` writes it as commented YAML in `docs/schema.md` field order |
| `src/netgraph/watch/` | live re-rendering (`netgraph watch`): `pipeline.py` is one load → validate → render cycle and its published state, `loop.py` decides what counts as a change, `server.py` is the loopback preview and its self-reloading page |
| `src/netgraph/web/` | the interactive interface (`netgraph web`): `preview.py` is one pass over a document stream, `svgdoc.py` is `render/fragment.py` with the preview's answers filled in, `server.py` is five routes over all of it, `assets/` the dependency-free client |

## Using it as a library

The package is typed (`py.typed`) and checked with `mypy --strict`, so the stages above are
usable directly. Nothing in them needs the CLI.

```python
from pathlib import Path

from netgraph.config import load_config
from netgraph.loader import load_tree
from netgraph.render import RenderOptions, build_graph, icon_theme, render
from netgraph.validate import validate

root = Path("inventory")
inventory = load_tree(root)
for finding in validate(inventory, load_config(root).validation):
    print(finding)  # inventory/sw1.yaml#0:3: error: E002: ...

options = RenderOptions(show_ips=False, icons=icon_theme("cisco"))
svg = render(build_graph(inventory), "svg", options)
```

`load_tree` never raises for a problem *inside* the tree; unreadable documents are collected
on `inventory.errors`. `validate` never raises either. Text that never was a folder — a
paste, a pipe, a snippet from a ticket — goes through `load_stream` instead, and
`render_source` runs the whole of what `netgraph web` does per keystroke in one call:

```python
from netgraph.loader import load_stream
from netgraph.render import Layer
from netgraph.web import ViewOptions, render_source

text = Path("topology.yaml").read_text()
inventory = load_stream(text)  # same parser, same schema, same rules

preview = render_source(text, ViewOptions(layer=Layer.L2))
preview.svg  # an <svg> fragment, safe to embed, with an id on every element
preview.details["node-sw-office"]  # the info box, keyed by the drawn element's id
preview.problems  # load errors and findings, most severe first
```

The keys of `preview.details` are the ids `render/ids.py` gives the drawn elements — `node-`
or `edge-` followed by a slug of the fully-qualified name, so `node-sw-office` and
`edge-cables_cbl-rtr-sw`. They are the same ids `-f svg` and `-f json` carry, which keeps a
hover, a tooltip and an export from drifting apart. And `render_source` never raises for
anything the text can be wrong about: a syntax error, a dangling cable and a filter that
matches nothing all come back as a preview whose `status` and `problems` say so, with
whatever resolved still drawn.

## Design rules a change must not break

Each is load-bearing somewhere else in the tree, and a test names most of them.

1. **Models forbid unknown keys.** `NetgraphModel` sets `extra="forbid"` (`models/base.py`,
   `NG-D005`): silently ignoring a misspelt key would produce a diagram that disagrees with
   the file, which is the failure mode this tool exists to prevent. It is also what puts
   `additionalProperties: false` into the JSON Schema.
2. **The loader normalises so that no later stage re-parses.** Ranges are expanded and
   templates merged during the load, and nothing below `loader/` and `fmt/` imports a YAML
   parser — `cli.py` uses `yaml.safe_dump` for *output* only — so an inventory cannot mean
   one thing to `validate` and another to `render`.
3. **Validation never mutates the inventory.** `validate` reads and returns `list[Finding]`;
   a check that wrote something down would be one whose result depended on run order.
4. **Renderers are pure functions of `(Graph, RenderOptions)`.** A backend gets no
   `Inventory`, no configuration and no filter. `RenderOptions` says *what to draw*;
   `FilterSpec` decided *what exists* before any renderer ran — so turning a label off can
   never change the topology a reader sees.
5. **Every user-visible diagnostic goes through `rules.py`**, so it has a permanent id, a
   severity and a `title` from which its section in
   [validation-rules.md](validation-rules.md) is derived. Load errors are not exempt:
   `report.LOAD_RULE` gives them the same three. `tests/test_docs.py` fails when a rule has
   no section, a wrong severity, a missing alias or no "Suppress with" line, and
   `tests/test_examples.py` fails when it has no fixture under `tests/fixtures/invalid/`.
6. **One entry per output format, in `render/registry.py`.** No front end may branch on a
   format name; something new to know about a format is a new field on `Renderer`.
7. **Output is deterministic, and identifiers are permanent.** Load order, finding order,
   node and edge order and every export's collation are explicitly sorted, never left to
   dict order or directory traversal — `tests/test_golden.py` and the export fixtures would
   notice. And a rule id, a `NodeType`, a JSON export key or a drawn element's id appears in
   somebody's suppression list, style sheet or downstream tool: add, do not renumber.

## See also

* [CONTRIBUTING.md](../CONTRIBUTING.md) — the gates, the recipes, how to run CI's checks.
* [Testing netgraph](testing.md) — the example half and the property half of the suite,
  and the Hypothesis profiles.
* [The inventory schema](schema.md) — the specification the models and the loader
  implement, section by section.
* [Validation rules](validation-rules.md) — the write-up behind every id in `rules.py`.
