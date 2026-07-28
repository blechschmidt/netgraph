# Follow-ups

Known gaps, deferred deliberately rather than forgotten. Each entry records what
was measured, why it was not fixed in place, and what a fix would have to do.
An entry that has since been closed keeps its place in the list, rewritten with
what was actually achieved, so a number in it can be compared with the next one.

Raised by the end-to-end review of 2026-07-27, which audited YAML safety, the
Graphviz invocation, DOT/Mermaid escaping, diagnostic leakage, performance on a
1000-device inventory, and the built wheel. Everything not listed here passed;
one bug found in that pass (Mermaid front-matter escaping) was fixed rather than
deferred.

---

## 1. ~~`load_tree` is the throughput bottleneck~~ — fixed, 3.3× end to end

**Status:** closed 2026-07-28. Parsing goes through libyaml where PyYAML has it.

`netgraph.loader.documents` no longer subclasses `yaml.SafeLoader` directly. The
strictness lives in `_StrictLoaderMixin` and is mixed over `yaml.SafeLoader` and
`yaml.CSafeLoader` alike; the module selects one at import time and binds it to
`StrictSafeLoader`. `NETGRAPH_YAML_LOADER` overrides that choice — `python` to
force the pure-Python parser, `libyaml` to demand the fast one and fail loudly on
a build without the bindings.

### Measured

The 1000-device tree the original entry timed was never committed, so the
harness now is: `tools/bench_pipeline.py` generates one and times every stage.
Its defaults produce **1056 devices in 2106 documents across 138 files, 1.2 MB
of YAML** — the same device count as before in denser files. Both parsers are
timed on that one tree in the same run, so the ratios below are exact even
though the absolute numbers are not comparable with the original table's.

| Stage | Pure Python | libyaml | Speed-up |
|---|---|---|---|
| `load_tree` | 2563 ms | 615 ms | 4.2× |
| `validate` | 111 ms | 113 ms | — |
| `build_graph` | 42 ms | 43 ms | — |
| `render` (dot / mermaid / json) | 9 / 4 / 47 ms | 10 / 5 / 48 ms | — |
| **total** | **2776 ms** | **834 ms** | **3.3×** |

Isolating the parse step — the same 2106 documents read through both loaders —
gives **2173 ms against 294 ms, 7.4×**, which is the ~8× the original entry
predicted. `load_tree` gains less than that because the pydantic model
validation inside it is untouched; it is now 74 % of the pipeline rather than
91 %, and that validation is what a further pass would have to attack.

End to end, including interpreter start, on the same tree:

| Command | Pure Python | libyaml |
|---|---|---|
| `netgraph validate` | 2.82 s | 0.85 s |
| `netgraph render -f dot` | 2.88 s | 0.90 s |
| `netgraph render -f svg` | 3.48 s | 1.55 s |

Peak RSS is 57–65 MB either way: libyaml buys time, not memory.

### How the guarantees are held

`tests/test_yaml_loader.py` parametrises every guarantee over both bases and
skips — rather than silently drops — the libyaml cases on a build without the
bindings. `yes`/`no`/`on`/`off` stay strings, duplicate keys are rejected,
`!!python/object/apply` and unknown tags are refused, merge keys keep their
non-duplicate status, and `start_mark` line and column agree exactly, compared
node by node over every document shipped under `examples/` as well as over
synthetic edge cases.

What is deliberately *not* pinned is PyYAML's own wording for a syntax error,
which differs between the two — "mapping values are not allowed **here**"
against "**in this context**". Only the marks are load-bearing, and the two
places the suite asserted on wording now use a message both bases agree on.

CI runs the suite on both paths. The `python` entry in the test matrix sets
`NETGRAPH_YAML_LOADER=python`, and a step ahead of the tests fails the job if
the loader actually selected is not the one that entry asked for — so the
fallback is exercised rather than assumed.

### Found on the way

The pure-Python `Reader` scans the whole document for unprintable characters in
its *constructor*, where libyaml only trips over one when it reaches it.
`read_documents` built its loader outside the `try`, so a control character
anywhere in an inventory raised a bare `yaml.reader.ReaderError` straight past
`load_tree`'s handler and ended the process instead of being reported. The
loader is now constructed inside, and both paths report it as an ordinary
`YamlSyntaxError`.

---

## 2. A fully-qualified reference is rejected, contradicting the specification

**Severity:** correctness. Documented behaviour that does not work.

[`docs/schema.md` §2.2](schema.md) states:

> A reference MAY also be written fully qualified (`sites/berlin/rack1/sw1`),
> which is tried relative to the current namespace first and as an absolute name
> second.

It does not work. A cable endpoint naming an element that way fails to load:

```
spec.endpoints[0].device: String should match pattern '^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$'
```

The resolution logic is not the problem — it is already written and correct.
`Inventory.lookup` (`src/netgraph/loader/inventory.py:239`) opens with
`if "/" in name:` and implements exactly the documented relative-then-absolute
order. What blocks it is one level up: `InterfaceRef.device` is typed
`ElementName`, whose pattern is the grammar for *declaring* a name
(`metadata.name`), and that grammar has no `/`. The reference never reaches
`lookup`.

The inconsistency is visible from the CLI, which resolves the very same string
happily, because it calls `lookup` directly:

```console
$ netgraph show sites/hq/sw1        # works
$ # the same name in a cable endpoint  -> schema error
```

So the `"/" in name` branch of `lookup` is currently reachable only from `show`
and `--neighbors-of`, never from a document.

**Consequence.** Two elements sharing a short name in different namespaces
cannot both be cabled from a common `cables/` directory — there is no way to
disambiguate the endpoint. That is the exact case §2.2 introduces the syntax to
solve.

**Why it was deferred.** Fixing it widens the set of documents netgraph accepts.
That is a schema-surface change with golden-fixture and specification
consequences, and a review pass should not make one silently.

**What a fix must do.** Split the type: keep `ElementName` as the *declaration*
grammar and add a reference type that also admits `/`-separated segments (each
segment matching `ElementName`), then apply it to `InterfaceRef.device` and any
other reference field. Both `lookup` branches then need document-level tests —
the qualified branch has none today.

---

## 3. ~~A malformed scalar is echoed into diagnostics at unbounded length~~ — fixed

**Status:** closed 2026-07-28. Every echo of a rejected value is bounded.

Diagnostics never dumped file contents — that was checked specifically, and
still holds: a document carrying secrets in `metadata.description`,
`metadata.labels` and an unrelated field reports its errors without echoing any
of them. Only the single offending value appears, which is what the user has to
go and fix. What was unbounded was the length of that one value.

`netgraph.errors.echo_value` now renders a rejected value for a diagnostic:
`repr` of the whole value up to `MAX_ECHOED_VALUE_LENGTH` (120) characters, and
past that a `repr` of the prefix followed by `… (+N more characters)`. The
200 000-character `mac:` that produced a **200 135-character** line now produces
a **280-character** one, prefix and location included:

```
sw.yaml#0:8  load  spec.interfaces[0].mac: 'xxx…120 characters…xxx'… (+199880 more
characters) is not a MAC address; expected xx:xx:xx:xx:xx:xx, XX-XX-XX-XX-XX-XX
or xxxx.xxxx.xxxx
```

A value at or under the limit is still echoed verbatim, through `repr`, so a
trailing space or a homoglyph in a short typo stays visible — that echo is the
whole reason the value is quoted at all.

### Where it is applied

Every site in `models/` and in the loader that interpolates a value the user
wrote: MAC addresses, bit rates and VLAN tokens (`models/scalars.py`), IPv4/IPv6
addresses and netmasks (`models/interface.py`), cable endpoints
(`models/cable.py`), label and annotation keys (`models/metadata.py`), an
unknown `kind` and an unknown key (`models/document.py`), and a duplicate
mapping key (`loader/documents.py`). `validate.py` was swept and needed nothing:
it runs on parsed models, so everything it quotes is already length-bounded by
the field that accepted it.

Two things the first pass missed, both found by the test that asserts a bound
rather than a wording:

- **A nested exception re-echoes the value.** `ipaddress` reports
  `'xxx…' is not a valid netmask`, so interpolating `{exc}` put the whole value
  back. `clip_text` bounds prose that is already a message; `echo_value` is for
  a value, which needs quoting as well as bounding.
- **The path is a value too.** An unknown key becomes the *location* of its own
  diagnostic, so `format_path` clips each component; clipping only the message
  left a 200 177-character line.

---

## 4. ~~A large inventory exceeds Mermaid's default 500-edge limit~~ — warned about

**Status:** closed 2026-07-28. The output is unchanged; the CLI says so on stderr.

The 1056-device inventory renders to Mermaid that Mermaid's own parser accepts —
but only once `maxEdges` is raised. At the default it reports:

> Edge limit exceeded. 500 edges found, but the limit is 500. Initialize mermaid
> with maxEdges set to a higher number to allow more edges. You cannot set this
> config via configuration inside the diagram as it is a secure config.

The limit is enforced by the *renderer*, and deliberately cannot be lifted from
inside the document, so netgraph cannot emit anything that fixes it. GitHub and
GitLab render with the default, so a Mermaid diagram of an inventory this size
will not display there.

Nothing changed in the output, then — the warning is the fix. Both the number
and the check live in `src/netgraph/render/mermaid.py` (`MERMAID_MAX_EDGES` and
`mermaid_advisories`), and `render` and `watch` reach them through the renderer
registry: each asks "anything to say about a graph this size?" without knowing
which backend has a limit. They emit:

```
warning: this diagram has 501 edges, over Mermaid's limit of 500: GitHub, GitLab
and mermaid-cli will refuse to draw it, and the limit cannot be raised from
inside the document. Cut the graph down with --namespace, --kind or
--neighbors-of, or use '-f dot' or '-f svg', which have no such ceiling
```

The limit is inclusive, and both sides of it are tested: 500 edges renders in
silence, 501 warns. `-f dot` and `-f json` never warn, because neither has a
ceiling to warn about.

---

## Checked and found sound

Recorded so a later reviewer knows these were examined rather than skipped.

- **YAML is loaded only through `StrictSafeLoader`.** It is the sole loader in
  the codebase; `yaml.load`, `FullLoader` and `UnsafeLoader` appear nowhere.
  `!!python/object/apply` and unknown tags are refused. Since entry 1 that name
  binds to the same strictness over either of two parser bases, both of which
  are held to this by `tests/test_yaml_loader.py`.
- **Alias expansion bombs are contained.** A nine-deep `&a`/`*a` bomb — in value
  position and in key position — is handled in well under a second inside a 2 GB
  address-space cap, because PyYAML resolves an alias to the same object rather
  than a copy, making expansion O(n) rather than O(9ⁿ). The key-position variant
  is rejected outright as an unhashable key.
- **No shell is involved anywhere.** There is no `os.system`, `shell=`, `eval`
  or `exec` in `src/`. The one subprocess is Graphviz: `netgraph.render.dot`
  runs `dot` through `subprocess.run` with a fixed argument *list*
  (`[<resolved dot>, '-Tsvg']`), never a string and never a shell, resolving the
  executable with `shutil.which` and feeding the DOT source over **stdin**. No
  user-controlled string is ever a command argument, let alone a shell word;
  `format` is checked against the `IMAGE_FORMATS` allowlist before it reaches
  the `-T` flag, and the call is bounded by a timeout.
- **Symlink traversal out of the inventory is refused.** `_within_root` resolves
  each link and rejects anything not under the root; revisits and cycles are
  reported (`NG-L003`).
- **Output paths.** `--output` is a direct command-line argument, so a path
  outside the tree is the user's stated intent, not a traversal. Nothing derives
  an output path from inventory *content*, which is where traversal would matter.
- **The preview server serves memory, not the filesystem.** Five fixed routes,
  no request path is ever turned into a file name, loopback-only by default with
  a `Host`-header check against DNS rebinding, and a strict
  `Content-Security-Policy`.
- **DOT escaping holds.** Quotes, braces, newlines, backslashes, `<script>`,
  RTL overrides, tabs and astral-plane characters — placed in descriptions,
  cable labels, `--title` and in *namespace* names, which come from directory
  names and are therefore arbitrary — all produce DOT that Graphviz 2.43 lays
  out without a warning, at every layer and with and without `--group-by-namespace`.
- **Mermaid escaping holds** for flowchart labels, verified by parsing the output
  with Mermaid's own parser under jsdom. The front matter did *not* hold; that
  bug is fixed (`_front_matter` now escapes backslashes) and covered by
  `test_mermaid_front_matter_stays_valid_yaml`.
- **Packaging.** The wheel installs into a clean venv and pulls only its declared
  dependencies. Both the `netgraph` console script and `python -m netgraph` work
  from outside the source tree — version, `validate`, `render -f dot`,
  `render -f svg -o`, and `watch`. Exit codes are 1 for a rejected inventory and
  0 for a clean one; `watch` stops cleanly on SIGINT (0) and SIGTERM (143). The
  sdist carries `src`, `tests`, `examples`, `docs` and `tools`, so the shipped
  test suite can actually run.
