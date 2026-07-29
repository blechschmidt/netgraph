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

Entry 5 is that further pass. It found the guess above only partly right — the
model validation was 27 % of the load, not the majority — and closes the
remaining 1.4× on `load_tree` anyway.

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

## 5. ~~`load_tree` is still the bottleneck after libyaml~~ — fixed, 1.41×

**Status:** closed 2026-07-28. Entry 1 named the next target; this is the pass
that took it, and the first thing it found was that the target was misnamed.

### What the profile actually said

Entry 1 predicted that pydantic model validation was what was left. It was not
the majority of it. Timing the stages of `load_tree` separately on the same
1056-device tree, through libyaml:

| Inside `load_tree` | Before |
|---|---|
| libyaml compose (C) | 150 ms |
| PyYAML's Python constructor | 130 ms |
| pydantic + netgraph model validators | 162 ms |
| cyclic garbage collection | 86 ms |
| loader bookkeeping, file reads | ~60 ms |

So model validation was **27 %** of the load, not 74 %, and the second largest
item — 18 % of it — was not netgraph's code at all but the garbage collector.
Three changes came out of that, in descending order of what they were worth.

**The collector is held off for the duration of a load** (`_deferred_gc` in
`loader/tree.py`, **86 ms**). Loading is the worst possible shape for a
generational collector: millions of short-lived objects — node trees and the
mappings built from them, discarded one document at a time — while the *live*
set, the elements, only grows. Every collection is a full walk of an
ever-larger graph that is almost entirely reachable, and there are hundreds of
them. None of that garbage can form a cycle, so reference counting frees it
without help, which is why peak RSS is unchanged (measured: 64 MB either way).
The previous state is captured and restored, so a caller that had already
disabled the collector keeps it disabled and an exception mid-walk does not
leave it off.

**An address is parsed once instead of three times** (`_plain_address` in
`models/interface.py`, **72 ms**, cutting the model layer from 162 ms to 90 ms).
`10.0.0.1/24` went through `ipaddress.ip_interface`, which guesses the family by
trying IPv4 and then IPv6 and builds a whole network object to recover a number
the document had just stated; the result was then rendered back to a string, and
pydantic parsed that string a second time. The fast path recognises the one
spelling almost every address uses — a literal address of the expected family
and a decimal prefix length — and hands pydantic the object `ipaddress` already
built. It returns `None` for anything else, and the general path, which owns
every diagnostic, runs unchanged. `_check_unique_addresses` likewise compares
`ipaddress` objects rather than rendering each address back to text to hash it.

**A plain string key is not constructed twice** (`_reject_duplicate_keys` in
`loader/documents.py`, **16 ms**, visible as the parse step's own 294 → 278 ms).
The duplicate-key check ran PyYAML's constructor over every key of every
mapping, and `construct_mapping` then ran it again. For a scalar node tagged
`!!str` the constructed value *is* `node.value`, so it is read directly;
anything else — an int key, a bool, a sequence — still goes through the
constructor, which is what keeps `1` and `'1'` distinct and `1` and `01` the
same key.

### Measured

Same harness, same tree, same machine as entry 1's table: `tools/bench_pipeline.py`
defaults, **1056 devices in 2106 documents across 138 files, 1.2 MB of YAML**.
Both columns were re-measured here rather than copied, so they are comparable
with each other; they are *not* comparable with entry 1's absolute numbers,
which came off a different machine (this one is about twice as slow on
`validate`). Median of five.

Through libyaml:

| Stage | Before | After | Speed-up |
|---|---|---|---|
| `load_tree` | 592 ms | 421 ms | **1.41×** |
| `validate` | 235 ms | 240 ms | — |
| `build_graph` | 43 ms | 43 ms | — |
| `render` (dot / mermaid / json) | 30 / 4.4 / 44 ms | 32 / 4.6 / 46 ms | — |
| **total** | **949 ms** | **786 ms** | **1.21×** |

Through the pure-Python parser:

| Stage | Before | After | Speed-up |
|---|---|---|---|
| `load_tree` | 2487 ms | 2333 ms | 1.07× |
| `validate` | 242 ms | 234 ms | — |
| `build_graph` | 45 ms | 43 ms | — |
| `render` (dot / mermaid / json) | 32 / 4.7 / 44 ms | 30 / 4.5 / 46 ms | — |
| **total** | **2854 ms** | **2691 ms** | **1.06×** |

The fallback path gains almost nothing, and that is not a disappointment but
arithmetic: its parser is 8× slower, so it spends 2.2 s of a 2.3 s load inside
PyYAML, where none of these three changes reach. The parse step itself:

| Parse only, 2106 documents | Before | After |
|---|---|---|
| pure Python | 2169 ms | 2174 ms |
| libyaml | 294 ms | 278 ms |

End to end, including interpreter start:

| Command | libyaml before | libyaml after | pure Python before | pure Python after |
|---|---|---|---|---|
| `netgraph validate` | 1.06 s | 0.94 s | 3.00 s | 2.91 s |
| `netgraph render -f dot` | 1.15 s | 1.03 s | 3.14 s | 2.95 s |
| `netgraph render -f svg` | 1.82 s | 1.71 s | 3.88 s | 3.66 s |

Peak RSS is 60–69 MB before and 60–68 MB after: as with entry 1, the win is
time, not memory.

`load_tree` is now **54 %** of the pipeline through libyaml, down from 62 %, and
`validate` — untouched here — has become the second cost at 30 %. That is where
a third pass would go.

### The regression guard

`tests/test_performance.py` fails if the load gives this back. It generates a
scaled-down tree with the same harness (80 devices, 158 documents), then times
two things in the same process, interleaved, best of four: the raw parse over
the tree, and `load_tree` over it. The assertion is on the **ratio**, because a
wall-clock ceiling on a shared CI runner would have to be so generous it caught
nothing, whereas machine speed cancels out of `full / floor`.

| Parser | Before this entry | Today | Threshold |
|---|---|---|---|
| libyaml | 1.78–1.79 | 1.52–1.53 | 1.70 |
| pure Python | 1.16 | 1.10–1.12 | 1.25 |

The libyaml row was checked in both directions: it passes on this commit and
fails on its parent, quoting 1.79. The margin is real but not generous — 11 %
of headroom above today, 5 % below a full revert — which is the price of a
guard sharp enough to notice anything. The pure-Python row is honestly weaker:
with a parse 8× slower in the denominator the model layer would have to roughly
triple before it moved that far, so it would *not* catch a revert of this entry.
It is kept so that a catastrophic regression is not invisible on the fallback
path. Should the threshold ever need raising for a platform rather than for a
regression, raising it here and recording it in this entry is the intended fix,
not deleting the test.

### Measured and rejected

Four candidates were profiled and not taken. Recorded with their numbers so the
next pass does not re-derive them.

- **A bounded cache for repeated scalar values.** Worth **1.6 ms of 62 ms**
  (2.6 %) on the document-parsing stage, for process-global mutable state. The
  premise does not hold on the values that matter: 2100 of the 2142 MAC
  addresses in the tree are distinct, so a MAC cache is pure overhead. Label
  keys do repeat — 5 distinct keys across 2106 documents — but checking one is
  already only a `rpartition` and two regex matches. Bit rates repeat hardest
  (2 distinct values across 1050 cables) and `parse_bitrate` does not appear in
  the profile at all.
- **Loosening the model config.** `revalidate_instances` is already `never`, so
  there is no redundant re-validation of submodels to remove. `validate_default`
  is the one setting that costs something — turning it off is worth **5 ms of
  62 ms**, under 1 % of a load — and it is on deliberately, so that a default
  that stops matching its own field is caught. Not a trade worth making.
- **Overriding PyYAML's implicit-tag resolver.** `Resolver.resolve` is called
  once per scalar (118 800 times on this tree) and allocates a list on every
  call to concatenate a wildcard table that is always empty. A replacement that
  avoids it, verified to produce byte-identical node trees over the whole tree,
  is worth **5 ms of 271 ms** — 2 %, for a reimplementation of a PyYAML core
  method that every diagnostic's line numbers depend on.
- **Parsing files in parallel.** Prototyped with four forked workers on this
  four-core machine, with the pool already warm, no diagnostic ordering and no
  error handling: **427 ms → 300 ms**, 1.42×. That is the optimistic bound and
  it is not attractive. Handing the parsed elements back costs 58 ms to pickle
  and **162 ms to unpickle in the parent** — serial work that no number of
  workers reduces, and already more than half the total. Add pool start-up
  (which dominates on a small inventory, the common case), `spawn` instead of
  `fork` off Linux, and the machinery to keep diagnostics deterministically
  ordered, and it buys a fraction of a factor for a large amount of new failure
  surface.

### What did not change

Every diagnostic. The three changes are on paths that are only reached when a
value is *accepted*: the fast address path declines and defers to
`ipaddress.ip_interface` for anything it does not recognise, the duplicate-key
fast path only reads a value PyYAML would have constructed identically, and
deferring the collector cannot alter a result at all. That is asserted rather
than argued: `test_the_address_fast_path_is_invisible` runs 24 address
spellings — netmasks, out-of-range prefixes, non-ASCII digits, the wrong
family, malformed literals — twice, once with the fast path forced to decline,
and requires the accepted value or the error text to be identical; and
`test_keys_equal_after_construction_are_duplicates` pins the key pairs that must
still collide after construction. The full suite, the golden fixtures and both
parser paths are otherwise untouched.

Checked once more from the outside, at the level a user sees: the DOT, Mermaid
and JSON renderings, the `validate` report and the subnet listing of every
inventory under `examples/` and of the 1056-device benchmark tree, plus the
diagnostics of all 41 documents in `tests/fixtures/invalid/`, are **byte-for-byte
identical** before and after — and identical again through the pure-Python
parser.

---

## 6. A tunnel has no icon, so `--icons` falls back to a shape for it

**Status:** open. Raised while adding the `tunnel` kind (2026-07-29).

`--icons THEME` draws each node as its kind's picture
(`netgraph.render.icons.ICON_KINDS`). The bundled themes carry pictures for the
six hardware kinds, and `tunnel` was deliberately **not** added to that tuple: a
tunnel is not hardware, the Cisco topology idiom the bundled artwork follows has
no glyph for one, and inventing a lock or a pipe would put netgraph's guess
about a security property into a picture rather than into a label — which is
exactly what `W127` and the crimson edge exist to say in words.

The consequence is visible rather than broken: with a theme in use, hardware is
drawn as icons and a tunnel node keeps its violet hexagon. That mixes two visual
languages in one diagram, which is worth fixing eventually.

A fix would have to decide, in this order:

1. Whether a tunnel *should* be a glyph at all, or whether the encapsulation
   view reads better with the tunnels as the only shapes on a page of icons.
2. If it should: one glyph for every type, or one per type? Eight glyphs is a
   lot of artwork to keep in step with `TunnelType`, and the type is already on
   the label. One is likely right.
3. Whether an icon can carry "cleartext" at all, or whether that has to stay a
   colour and a word. It has to stay a colour and a word: a reader who does not
   recognise the glyph would read a missing lock as "nothing to say".

Until then, `IconTheme.files` simply returns no file for `tunnel` and the shape
is used, which is the documented fallback for a theme with a missing picture —
so nothing fails, and a theme that *does* ship `tunnel.svg` already works.

---

## 7. `validate` is the second cost in the pipeline — profiled

**Status:** open, being worked on (2026-07-29). Entry 5 named this target: with
`load_tree` at 54 % of the pipeline, `validate` is second at 30 %, and neither
of the two prior passes touched it. This entry is that third pass. The profile
below was taken **before any change**, so the "measured and rejected" section
this entry will grow can be checked against it.

### The harness

`tools/profile_validate.py`, committed alongside `tools/bench_pipeline.py` and
generating the same default tree — **1056 devices in 2106 documents across 138
files, 1.2 MB of YAML**, through libyaml. It breaks the cost down **by rule**
rather than by function, because `validate` is a fixed list of checks over one
prepared context: a function-level profile spreads a rule's cost over the
helpers it shares with a dozen others (`_linked_endpoints`, `_q`, `_join`) and
hides which *rule* is worth attacking. Each check is timed end to end over one
shared context, including the engine work its drafts cause — the suppression
test, the `Finding` construction, the source lookup — since that work exists
only because the rule yielded something. `_build_context` is charged to no rule
and broken down separately. Minimum of seven runs.

### Before

`validate` over the benchmark tree: **220–229 ms**, 2143 findings.

| Item | Before | Share |
|---|---|---|
| `_build_context` — `subnets_of` | 42–43 ms | 19 % |
| `_build_context` — endpoint resolution | 7.6 ms | 3 % |
| `_build_context` — per-owner maps | 3.8 ms | 2 % |
| `_build_context` — suppressions | 0.4 ms | — |
| **`_build_context` total** | **60–62 ms** | **27 %** |
| `W110` reserved address | 49–50 ms | 22 % |
| `E004` duplicate IP | 33.5 ms | 15 % |
| `W111` overlapping prefixes | 26–27 ms | 12 % |
| `W112` loopback prefix | 19–21 ms | 9 % |
| `I001` locally administered MAC | 6.1 ms | 3 % |
| `E007` stacking cycle | 1.9 ms | 1 % |
| `E008` member is aggregated | 1.7 ms | 1 % |
| `W101` unaddressed interface | 1.4 ms | 1 % |
| the other 43 rules, summed | 14 ms | 6 % |
| engine + final sort (residual) | 7–14 ms | 3–6 % |

Four rules are 58 % of `validate` on their own, and with `subnets_of` the same
five items are **77 %**. Every one of the five walks addresses, and `I001` — the
only rule in the table that actually reports anything, 2100 findings — is 3 %.
So the cost is not in reporting; it is in deriving.



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
