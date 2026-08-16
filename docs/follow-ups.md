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

`netviz.loader.documents` no longer subclasses `yaml.SafeLoader` directly. The
strictness lives in `_StrictLoaderMixin` and is mixed over `yaml.SafeLoader` and
`yaml.CSafeLoader` alike; the module selects one at import time and binds it to
`StrictSafeLoader`. `NETVIZ_YAML_LOADER` overrides that choice — `python` to
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
| `netviz validate` | 2.82 s | 0.85 s |
| `netviz render -f dot` | 2.88 s | 0.90 s |
| `netviz render -f svg` | 3.48 s | 1.55 s |

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
`NETVIZ_YAML_LOADER=python`, and a step ahead of the tests fails the job if
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
`Inventory.lookup` (`src/netviz/loader/inventory.py:239`) opens with
`if "/" in name:` and implements exactly the documented relative-then-absolute
order. What blocks it is one level up: `InterfaceRef.device` is typed
`ElementName`, whose pattern is the grammar for *declaring* a name
(`metadata.name`), and that grammar has no `/`. The reference never reaches
`lookup`.

The inconsistency is visible from the CLI, which resolves the very same string
happily, because it calls `lookup` directly:

<!-- norun: the element name is illustrative and the second line is a comment, not a command -->
```console
$ netviz show sites/hq/sw1        # works
$ # the same name in a cable endpoint  -> schema error
```

So the `"/" in name` branch of `lookup` is currently reachable only from `show`
and `--neighbors-of`, never from a document.

**Consequence.** Two elements sharing a short name in different namespaces
cannot both be cabled from a common `cables/` directory — there is no way to
disambiguate the endpoint. That is the exact case §2.2 introduces the syntax to
solve.

**Why it was deferred.** Fixing it widens the set of documents netviz accepts.
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

`netviz.errors.echo_value` now renders a rejected value for a diagnostic:
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
inside the document, so netviz cannot emit anything that fixes it. GitHub and
GitLab render with the default, so a Mermaid diagram of an inventory this size
will not display there.

Nothing changed in the output, then — the warning is the fix. Both the number
and the check live in `src/netviz/render/mermaid.py` (`MERMAID_MAX_EDGES` and
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
| pydantic + netviz model validators | 162 ms |
| cyclic garbage collection | 86 ms |
| loader bookkeeping, file reads | ~60 ms |

So model validation was **27 %** of the load, not 74 %, and the second largest
item — 18 % of it — was not netviz's code at all but the garbage collector.
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
| `netviz validate` | 1.06 s | 0.94 s | 3.00 s | 2.91 s |
| `netviz render -f dot` | 1.15 s | 1.03 s | 3.14 s | 2.95 s |
| `netviz render -f svg` | 1.82 s | 1.71 s | 3.88 s | 3.66 s |

Peak RSS is 60–69 MB before and 60–68 MB after: as with entry 1, the win is
time, not memory.

`load_tree` is now **54 %** of the pipeline through libyaml, down from 62 %, and
`validate` — untouched here — has become the second cost at 30 %. That is where
a third pass would go. Entry 7 is that pass; it found the guess above right for
once, and cut `validate` by 3.1×.

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

## 6. ~~A tunnel has no icon, so `--icons` falls back to a shape for it~~ — drawn

**Status:** closed 2026-07-29. The bundled theme ships `tunnel.svg` and
`tunnel.png`, and `tunnel` is in `ICON_KINDS`.

The original entry left three questions and argued for one answer each. All
three were answered the way it predicted, so what follows is what was actually
drawn and why the drawing is safe.

**1. Should a tunnel be a glyph at all? Yes.** The alternative the entry floated
— leaving tunnels as the only shapes on a page of icons — reads as an oversight
rather than as a distinction. A reader cannot tell "netviz has no picture for
this" from "this theme is incomplete", and the encapsulation view (`--layer
overlay`) is *entirely* tunnels: with no glyph it was a page of violet hexagons
with icons only at the edges. Mixing two visual languages in one diagram was the
complaint, and adding the glyph is the only fix that removes it.

**2. One glyph, or one per type? One.** `TunnelType` has eight members and would
be nine tomorrow; eight pieces of artwork kept in step with an enum is a
maintenance cost paid forever to say something the label already says, since
every tunnel node and edge is annotated with its stack (`vxlan over ipsec`). The
one glyph draws what all eight have in common: a **conduit** — a bore, with a
payload entering one end and leaving the other. That is encapsulation, and it is
the only thing a picture of a tunnel can honestly mean.

**3. Can an icon carry "cleartext"? No, and it does not try.** This was the
question with a real hazard behind it, and the entry's reasoning stands: a lock
on the encrypted glyph makes its *absence* the carrier of "this is in the
clear", and absence is not something a reader notices. Confidentiality stays
where it already was — the crimson edge, the word `cleartext` on the label, and
`W127` in prose — and the conduit is identical whether the tunnel is WireGuard
or GRE. The one thing an icon must not do is make a security property quieter
than it was, and this one leaves it exactly as loud.

The consequence for a theme author is unchanged: `ICON_KINDS` grew by one entry,
a theme without `tunnel.svg` still falls back to the violet hexagon, and
`IconTheme.kinds()` reports what a theme actually covers. `AGGREGATE_KIND`
(entry 9) was deliberately *not* added: a collapsed namespace is not a thing
with a picture but a box holding several, and Graphviz's `folder` shape says so
better than any glyph would.

---

## 7. ~~`validate` is the second cost in the pipeline~~ — fixed, 3.1×

**Status:** closed 2026-07-29. Entry 5 named this target: with `load_tree` at
54 % of the pipeline, `validate` was second at 30 %, and neither of the two
prior passes had touched it. This is that third pass.

### The harness

`tools/profile_validate.py`, committed alongside `tools/bench_pipeline.py` and
generating the same default tree — **1056 devices in 2106 documents across 138
files, 1.2 MB of YAML**, through libyaml. It breaks the cost down **by rule**
rather than by function, because `validate` is a fixed list of checks over one
prepared context: a function-level profile spreads a rule's cost over the
helpers it shares with a dozen others (`_linked_endpoints`, `_q`, `_join`) and
hides which *rule* is worth attacking. Each check is timed end to end, including
the engine work its drafts cause — the suppression test, the `Finding`
construction, the source lookup — since that work exists only because the rule
yielded something. `_build_context` is charged to no rule.

Every pass is **cold**: the inventory is reloaded before each sample and the
rules are timed once each, in report order, exactly as `validate` runs them.
That distinction did not exist before this entry and does now, which is the
first thing to say about the numbers below. Minimum of nine passes.

### What the profile said

`validate` over the benchmark tree, before any change: **236.6 ms**, 2143
findings. Both columns come from the same harness on the same machine.

| Item | Before | After | |
|---|---|---|---|
| `_build_context` | 68.2 ms | 42.0 ms | 1.6× |
| `W110` reserved address | 49.7 ms | 2.7 ms | **18×** |
| `E004` duplicate IP | 34.5 ms | 4.9 ms | **7.0×** |
| `W111` overlapping prefixes | 27.5 ms | 4.0 ms | **6.9×** |
| `W112` loopback prefix | 20.2 ms | 1.4 ms | **14×** |
| `I001` locally administered MAC | 6.4 ms | 6.3 ms | — |
| `E007` stacking cycle | 2.3 ms | 2.0 ms | — |
| `E003` duplicate MAC | 2.2 ms | 2.0 ms | — |
| `E008` member is aggregated | 1.8 ms | 1.8 ms | — |
| `W101` unaddressed interface | 1.8 ms | 1.5 ms | — |
| `I002` uncabled interface | 1.6 ms | 1.4 ms | — |
| the other 41 rules, summed | 15.7 ms | 11.9 ms | — |
| **`validate`, whole** | **236.6 ms** | **76.9 ms** | **3.08×** |

Four rules were 56 % of `validate` on their own, and with `_build_context` the
same five items were 85 %. Every one of the five walks addresses, and `I001` —
the only rule in the table that reports anything at all, 2100 findings — was
2.7 %. So the cost was not in reporting; it was in deriving. Specifically, it
was in deriving **the same `ipaddress` prefix over and over**.

### The four changes

In descending order of what they were worth. Reverting each file on its own,
cold `validate` over the benchmark tree, minimum of five fresh processes:

| Reverted on its own | Cold `validate` | That file is worth |
|---|---|---|
| nothing — this commit | 86.6 ms | — |
| `models/interface.py` | 151.8 ms | 65 ms |
| `validate.py` | 140.8 ms | 54 ms |
| `subnets.py` | 96.3 ms | 10 ms |
| all three | 235.1 ms | 149 ms |

The three do not add to the whole (65 + 54 + 10 = 129, not 149) and cannot: the
rule changes only avoid *building* a prefix while the model is not caching them,
and the model cache only avoids *rebuilding* one while the rules still ask for
it. Each number above is what that file is worth given the other two.

**An address derives its prefix once** (`IPv4Address.network` and
`IPv6Address.network` in `models/interface.py`, **65 ms**). Both
were plain properties reading `self.interface.network`, so every one of the five
consumers that asks an address which prefix it is in rebuilt one. They are now
`functools.cached_property`. Two smaller things came with it: the intermediate
`ipaddress.IPv4Interface` is skipped, since it constructs exactly this network
internally and discards the rest (3.5 µs against 6.4 µs on this machine); and
the *integer* form of the address is handed over rather than the object, because
`ipaddress` re-parses an address object it is given back out of `str(address)` —
a full RFC 5952 compression and re-parse for IPv6, which is why v6 gains most
(6.6 µs → 1.4 µs). The value is a pure function of two fields that are never
written after validation, which is what makes caching it invisible.

**`W110` asks the address, not a network object** (`_reserved_role`,
49.7 → 2.7 ms). It needed three facts — `num_addresses`, `network_address`,
`broadcast_address` — and each of those builds *further* address objects, per
address rather than per prefix. It now computes them from the address's host
bits, which is the definition rather than an approximation: a prefix holds at
most two addresses exactly when it has at most one host bit, the network address
is the one whose host bits are all zero, the IPv4 directed broadcast the one
whose host bits are all one. The change matters more than the arithmetic
suggests, because W110 was the **only** rule in the module that asked a loopback
address for its prefix: nothing else in a run looks at `127.0.0.1/8` or `::1/128`
that way, so it was materialising 2000 prefixes for nobody. `W112` had the same
shape in miniature — it read `address.network.version`, which is the model's own
type — and is fixed the same way (20.2 → 1.4 ms).

**Two maps are derived once instead of per consumer** (`_build_context`,
68.2 → 42.0 ms). `subnets_of` keyed its grouping by `str(network)` and rendered
every placement's address twice; it now keys by the network object — which
compares and hashes exactly as its spelling does, so the grouping is the same —
and renders a prefix once per prefix rather than once per address in it, 90
renderings on this tree instead of 2106. `_resolve_endpoint` and
`_resolve_tunnel_end` called `Device.interface(name)`, a linear scan of
`spec.interfaces`, when `_build_context` was already building the
name-to-interface map two rules later; the map is now built first and the
resolvers read it. `NG-I001` makes interface names unique within an element, so
the map and the scan cannot disagree.

**`E004` groups on objects and renders only what it reports** (34.5 → 4.9 ms).
Its key was `(str(address.ip), str(address.network), scope)`; it is now the
`ipaddress` objects themselves. Nearly every address in a healthy inventory is
alone in its group, so the two spellings that used to be computed for all 4122
addresses are now computed for the handful a finding actually names. The same
motivation put `_pair_endpoints` in the context: ten rules read the endpoints
two at a time and each was rebuilding the by-cable grouping.

### Measured

Same harness, same tree, same machine as entry 5: `tools/bench_pipeline.py`
defaults, **1056 devices in 2106 documents across 138 files, 1.2 MB of YAML**,
median of three. Both columns were re-measured here rather than copied.

One row of that table needs a caveat this entry created. The harness loads once
and then times each stage three times over that one inventory, so its `validate`
row is now the *second and third* run — and since an address caches its prefix
on first use, those are cheaper than the run a user pays for. The first run over
a freshly loaded tree is given its own row, and it is the honest one.

Three before/after rounds were run alternately rather than one column and then
the other, so that a machine drifting under load cannot be mistaken for a
result; the ranges below are across those rounds.

Through libyaml:

| Stage | Before | After | Speed-up |
|---|---|---|---|
| `load_tree` | 425–437 ms | 431–434 ms | — |
| `validate` (harness, 2nd/3rd run) | 236–244 ms | 74–85 ms | 3.0× |
| `validate` (first run, fresh tree) | 263–280 ms | 111–115 ms | **2.4×** |
| `build_graph` | 43.5–44.6 ms | 42–47 ms | — |
| `render` (dot / mermaid / json) | 32 / 4.6 / 50 ms | 32 / 4.7 / 51 ms | — |

Through the pure-Python parser:

| Stage | Before | After | Speed-up |
|---|---|---|---|
| `load_tree` | 2336–2363 ms | 2334–2365 ms | — |
| `validate` | 265 ms | 72–93 ms | 3.2× |
| `build_graph` | 43 ms | 45 ms | — |
| `render` (dot / mermaid / json) | 31 / 4.4 / 49 ms | 33 / 4.8 / 51 ms | — |

`validate` is parser-independent, as it should be: it runs on an inventory that
is already in memory. So unlike entries 1 and 5, this pass helps the fallback
path exactly as much as the fast one — which is most of what it is worth on the
pure-Python path, where the load still dwarfs everything.

End to end, including interpreter start, best of three:

| Command | libyaml before | libyaml after | pure Python before | pure Python after |
|---|---|---|---|---|
| `netviz validate` | 0.95–0.96 s | 0.80–0.81 s | 2.90–2.91 s | 2.73–2.78 s |
| `netviz render -f dot` | 0.96–0.97 s | 0.80–0.81 s | 2.91 s | 2.70–2.75 s |

There is no `render -f svg` row: the benchmark tree reports 42 `E009` errors, so
`render` stops before it reaches Graphviz and would measure the same work as the
`-f dot` row.

Peak RSS is 63.0–63.4 MB before and 63.7–64.0 MB after — **about 0.6 MB more**,
and that is the price of the cache rather than noise: roughly 2100
`IPv4Network` and `IPv6Network` objects are now held for the life of the process
instead of being built and dropped. Unlike entries 1 and 5, this one is not free
in memory. It is a good trade at this size and would still be at ten times it.

### The regression guard

`tests/test_performance.py` gains a second guard, alongside the load one and
built the same way: a ratio against a cheap in-process floor, best of four, so
that machine speed cancels and a shared CI runner cannot fail it spuriously.

The floor for `validate` is not the parse — a loaded inventory has already paid
that — but a **plain walk over every interface and every address**, repeated
eight times per sample. Five rules are statements about addresses, so none of
them can cost less than one such walk; `validate / floor` is how many walks'
worth of work the rule set does on top. One walk is a tenth of a millisecond on
the guard's 80-device tree, small enough that the timer's own noise moves the
ratio by several per cent, which is why the floor is eight of them. Because both
halves run on an inventory that is already in memory, the parser does not enter
either, and one threshold covers both paths rather than entry 5's two.

Each round loads the tree afresh. That is new and it matters: a second
`validate` over one inventory no longer does the work the first one did, so a
warm measurement would flatter every change in this area — including a partial
revert of this entry.

| | Before this entry | Today | Threshold |
|---|---|---|---|
| `validate` / floor | 21.5–22.0 | 6.9–7.2 | 8.5 |

Checked in both directions, and per file, which is the honest way to state what
a guard covers:

| Reverted | Ratio | Caught |
|---|---|---|
| all of entry 7 | 21.0 | yes |
| `models/interface.py` only | 13.6 | yes |
| `validate.py` only | 8.9 | yes |
| `subnets.py` only | 7.5 | **no** |

So it catches a full revert with 2.5× to spare and each of the two large pieces
on its own, and it does **not** catch the `subnets.py` piece, which is worth
about 9 % of `validate` — under the 17 % of headroom the threshold leaves above
today's worst sample. Buying that last piece would mean a threshold within 4 %
of the measured spread, which on a shared runner buys flakiness rather than
coverage. As with entry 5: if this ever needs raising for a platform rather than
for a regression, raise it and record it here, do not delete the test.

### Measured and rejected

Four candidates were profiled and not taken, with their numbers, so a fourth
pass does not re-derive them. All shares are of the 76.9 ms `validate` this
entry ends at, which is the point — each of these was worth attacking at
236.6 ms and is not worth it now.

- **One shared address index in `_Context`, replacing five walks.** `W110`,
  `W111`, `W112`, `E004` and `subnets_of` each walk owners × interfaces ×
  addresses. One such walk costs **1.07 ms**, so all five are about **5.4 ms,
  7 %** — and `subnets_of` could not read the index anyway: it is a public
  function the renderer and `list subnets` call with an `Inventory` and no
  validation context, which is deliberate, since a subnet in a diagram and a
  subnet in a finding must be the same object. 7 % to couple four rules to one
  derived table and still not close the fifth.
- **A shared cache of prefix objects across addresses in one prefix.** The 2106
  routable addresses on this tree sit in **90 distinct prefixes**, so the
  dedup rate is excellent: building them costs **3.59 ms**, and with a cache
  **0.97 ms**. That is **2.6 ms, 3.4 %**, for process-global mutable state —
  the same trade entry 5 rejected for scalar values at 2.6 %, and it is no more
  attractive here. The per-instance `cached_property` gets the large win
  (once per address instead of once per consumer) with no shared state at all;
  this would only get the remainder (once per prefix instead of once per
  address).
- **Merging the 17 per-owner scans into one pass.** Seventeen rules open with
  `for fqn, owner in ctx.owners.items()`. A bare walk of owners × interfaces is
  **0.24 ms**, so all seventeen are about **4.1 ms, 5.3 %**. Taking it would
  fuse seventeen independent, individually readable rules — each currently a
  straight loop with its own docstring explaining one idea — into a single loop
  with seventeen branches. That is the largest readability cost available in
  this module, for 5 %.
- **Caching `Interface.addresses()`.** It builds two tuples and concatenates
  them on every call: **0.74 ms** for one pass over every interface, about
  **3.7 ms, 4.8 %** across the five that make one. Rejected on a different
  ground from the others: `network` is safe to cache because it derives from two
  fields nothing writes after validation, whereas `addresses()` derives from
  `interface.ipv4` and `interface.ipv6`, which `resolve_address_family_defaults`
  *does* write to after the model is constructed. It does not touch the address
  lists today — but a cache whose safety depends on that staying true is a
  different kind of object from one whose safety is structural.

Two things are worth recording as **not** candidates. `I001` is 6.3 ms and
produces 2100 findings, which is 3 µs per finding for the message, the source
lookup and the `Finding` itself; that is the cost of the output, not of
deriving it. And the remaining 41 rules together are 11.9 ms, so no single rule
left is worth more than about 2 ms.

### What did not change

Every diagnostic. Each of the four changes is on a path where the result is
determined by data the change does not touch: `network` is a pure function of
two immutable fields, `_reserved_role`'s arithmetic is the definition of the
two boundaries it tests, grouping on an `ipaddress` object partitions exactly as
grouping on its spelling does, and the interface-by-name map answers what a
linear scan of the same list answers.

That is asserted rather than argued.
`test_reserved_role_agrees_with_ipaddress` runs the old network-based
formulation as an oracle against the new one over 950 addresses — every prefix
length of both families, at the network address, the all-ones host part, one
either side of each, and two interior positions — and requires the answers to
be identical, having first checked that the oracle actually fires all three of
its roles on that sweep.
`test_network_is_what_ip_interface_would_have_derived` pins the cached prefix
against `ipaddress.ip_interface(...).network` for every prefix length of both
families, and `test_caching_the_prefix_leaves_the_model_itself_untouched` pins
the one way a `cached_property` could leak out of a pydantic model — it writes
into the instance `__dict__`, which is where field values live — by requiring
equality, `model_dump`, `model_dump_json`, `model_fields_set` and the JSON
Schema to be unchanged after the prefix is read.

Checked once more from the outside, at the level a user sees. The `validate`
report, `validate --strict`, `list devices`, `list cables`, `list tunnels`,
`list vlans`, `list subnets`, and the DOT, Mermaid and JSON renderings of all
three layers — stdout and stderr separately — for **every inventory under
`examples/`, all 51 fixtures in `tests/fixtures/invalid/`, and the 1056-device
benchmark tree**, on **both YAML parser paths**: 3584 captured files,
**byte-for-byte identical** before and after. `tools/snapshot_outputs.sh` is the
harness, committed so the next pass can repeat it.

---

## 8. ~~An HTML page grows with the number of views~~ — fixed, 1.4× to 2.2×

**Status:** closed 2026-07-29. A view now costs its drawing and nothing else,
and a drawing costs 29 % fewer bytes than it did — 59 % fewer with `--icons`.

The original entry recorded the growth and named two possible fixes: a flag that
embeds fewer views, and storing the drawings as diffs against a base layout.
Neither was taken, because measuring first said the bytes were somewhere else
entirely. What follows is where they actually were.

### The harness

`tools/bench_html.py`, committed alongside `tools/bench_pipeline.py` and using
its generator, so a size here is comparable with a size there. It renders a
matrix of inventories × layer stacks and reports five numbers per page, chosen
because they fail in different ways: **bytes** (what a mail attachment costs),
**gzip** (what a static host costs), **dom** (elements the browser builds),
**paint** (first paint of the default view) and **switch** (median of ten layer
switches, each timed through a forced layout of the drawing that just became
visible, so the figure is work rather than the next vsync).

The two timing columns need a browser. `--browser` drives Chromium through
`playwright-core`, which is deliberately *not* a dependency of this project: the
byte columns are the ones the entry turns on, and they need nothing but Python
and Graphviz. Both were taken here on Chrome for Testing 149, second pass over the list,
so no page is paying for a cold browser.

### Measured, before

Every page rendered with the defaults (`--show-ips`, `--show-vlans`), so each
layer contributes up to four drawings. `generated/N` is `bench_pipeline`'s tree
at the stated size.

| Page | Views | Bytes | gzip | DOM | Paint | Switch |
|---|---|---|---|---|---|---|
| `home-lab` l1 | 4 | 86,388 | 19,287 | 344 | 25–37 ms | — |
| `home-lab` l1+l2 | 8 | 124,485 | 22,160 | 602 | 26–30 ms | 1.7 ms |
| `home-lab` l1+l2+l3 | 12 | 190,346 | 29,967 | 1,001 | 28–32 ms | 1.8 ms |
| `campus` l1 | 4 | 267,326 | 36,315 | 1,437 | 38–44 ms | — |
| `campus` l1+l2 | 8 | 462,503 | 55,725 | 2,587 | 46–52 ms | 7.3 ms |
| `campus` l1+l2+l3 | 12 | 879,890 | 104,712 | 4,876 | 67–76 ms | 9.5 ms |
| generated/1 site l1+l2+l3 | 12 | 301,049 | 37,055 | 1,629 | 35–41 ms | 2.6 ms |
| generated/3 sites l1 | 4 | 509,146 | 52,914 | 2,938 | 58–61 ms | — |
| generated/3 sites l1+l2 | 8 | 937,695 | 87,822 | 5,529 | 98–108 ms | 14 ms |
| generated/3 sites l1+l2+l3 | 12 | 1,584,584 | 158,483 | 9,140 | 108–120 ms | 14.1 ms |

The same matrix with `--icons cisco`, which is where the growth was worst:

| Page | Views | Bytes | gzip | DOM |
|---|---|---|---|---|
| `home-lab` l1+l2+l3 | 12 | 245,475 | 29,758 | 953 |
| `campus` l1+l2+l3 | 12 | 1,115,352 | 100,253 | 4,480 |
| generated/3 sites l1+l2+l3 | 12 | 2,205,617 | 171,501 | 8,816 |

Marginal cost of one more view, taken as `(12-view page − 4-view page) / 8`:

| Inventory | Plain | `--icons cisco` |
|---|---|---|
| `home-lab` | 12,995 | 17,783 |
| `campus` | 76,570 | 98,336 |
| generated/1 site | 22,542 | 31,809 |
| generated/3 sites | 134,430 | 187,506 |

### Where the bytes were

`--breakdown` splits a page into the parts that scale differently. On
`campus l1+l2+l3`, 879,890 bytes:

| Part | Bytes | Share |
|---|---|---|
| drawings | 604,544 | 69 % |
| records | 229,797 | 26 % |
| the client and the style sheet | 41,069 | 5 % |

So the first thing the split said is that the original entry's own summary was
wrong in a way that mattered: the fixed cost is ~40 kB *and* a quarter of the
page was records, which grew per **layer** and had nothing to do with the
drawings at all. Then, inside each of the two large parts:

**The drawings were 36 % repeated font attributes.** Graphviz states
`font-family`, `font-size` and `text-anchor` on every `<text>` element it emits
— 2,660 of them across these twelve drawings, carrying one distinct
`font-family` and two distinct values of each of the other two. That is
**217,664 bytes** of `campus l1+l2+l3`, more than the whole record block.

**With `--icons`, the artwork was 37 % of the drawings.** A theme reaches a
rendering as a `data:` URI per *node*, and a page draws every node once per
view: 396 occurrences on `campus l1+l2+l3`, **313,464 bytes**, of which
**3,994 bytes were distinct**. A duplication factor of 78.

**The records were duplicated per layer.** `l1` and `l2` draw the same elements,
and their record blocks were byte-identical: 55,577 bytes each. Across all three
layers, splitting each record into its `links` cross-reference and everything
else showed that the *everything else* is identical wherever an id repeats,
without exception — 149,224 bytes of record body reduce to **81,026 bytes** of
distinct bodies, with 68,396 bytes of genuinely per-layer links.

What the split also said is what *not* to do. Deflating the drawings
individually gives 604,544 → 79,762 bytes, and deflating them as one stream
gives 73,635 — an 8 % gain from cross-drawing sharing against a 7.6× gain from
sharing *within* a drawing. So the redundancy the original entry proposed to
attack, between views, is the small half; the redundancy inside one view is the
large one, and it can be removed as plain markup rather than as a compressed
blob a client has to unpack.

### The three changes

In descending order of what they are worth. Measured by disabling one at a time
and re-rendering `campus l1+l2+l3`:

| Reverted on its own | Plain | `--icons cisco` | Costs |
|---|---|---|---|
| nothing — this commit | 624,237 | 539,500 | — |
| the icon library | 624,237 | 859,699 | +320,199 with icons |
| the font hoisting | 800,483 | 715,746 | +176,246 |
| the record pool | 706,471 | 621,734 | +82,234 |
| all three | 882,717 | 1,118,179 | |

Each row is this commit with one change disabled, so it isolates that change;
none of them reproduces the old page exactly, because the client and the layer
index grew slightly and stay grown in every row.

**Each icon is stored once for the whole page** (`IconLibrary` in
`render/fragment.py`, **320 kB on `campus l1+l2+l3`**, and nothing at all on a
page without `--icons`). Every inline picture becomes a `<symbol>` in one
`<defs>` the page holds once, and every node that drew it becomes a `<use>`
naming that symbol and keeping the box Graphviz computed for it. A symbol
carries no viewport of its own, so the `<use>`'s width and height are the
viewport and the `<image>` inside fills it with the fit the original asked for
— which is why the pair draws what the single `<image>` drew. Two nodes share a
symbol when both would have written the same bytes fitted the same way, and not
otherwise. The consequence is worth stating plainly: `--icons` now usually makes
a page *smaller*, because a glyph replaces the polygon-and-polylines a shape was
drawn with, and it is paid for once.

**Each inherited font property is stated once per drawing**
(`_hoist_text_attributes`, **176 kB**). These are inherited properties, so the
dominant value moves to the drawing's root and is deleted from every `<text>`
that named it; the minority keep theirs, where it overrides what they now
inherit. The soundness condition is the interesting part: an attribute can only
move if **every** `<text>` carries it, because one that carried none would start
inheriting a value it never had. `font-weight` is exactly that case — Graphviz
writes it on the bold device name and on nothing else — so it is checked per
attribute and per drawing rather than from a list of names known to be safe, and
`font-weight` is in fact never hoisted.

**Each record is stored once for the whole page** (`_Pool` in `render/html.py`,
**82 kB**). The page carries two content-addressed pools — `records` and `links`
— and each layer holds a pair of indices per element id, `-1` in the second
position meaning "this record has no `links`", which is every edge. Keying on
the serialised form rather than on the element id is deliberate: it needs no
assumption about what a layer may and may not change, and two records that
differ in any way end up as two entries. `page.js` puts the two back together
once per layer, the first time that layer is shown.

The three do not add to the whole, and cannot: the icon library and the font
hoisting both shrink the same drawings, and a byte removed by one is not there
for the other to remove.

### Measured, after

Same harness, same machine, same inventories.

| Page | Views | Bytes | gzip | DOM | Paint | Switch |
|---|---|---|---|---|---|---|
| `home-lab` l1 | 4 | 77,590 | 19,738 | 344 | 30 ms | — |
| `home-lab` l1+l2 | 8 | 98,203 | 22,301 | 602 | 27 ms | 1.6 ms |
| `home-lab` l1+l2+l3 | 12 | 148,661 | 30,044 | 1,001 | 37 ms | 1.7 ms |
| `campus` l1 | 4 | 212,468 | 35,298 | 1,437 | 37 ms | — |
| `campus` l1+l2 | 8 | 308,116 | 49,305 | 2,587 | 46 ms | 7.2 ms |
| `campus` l1+l2+l3 | 12 | 624,237 | 95,199 | 4,876 | 63 ms | 9.4 ms |
| generated/1 site l1+l2+l3 | 12 | 224,659 | 36,792 | 1,629 | 34 ms | 2.7 ms |
| generated/3 sites l1 | 4 | 400,506 | 52,825 | 2,938 | 51 ms | — |
| generated/3 sites l1+l2 | 8 | 618,408 | 81,911 | 5,529 | 62 ms | 14.1 ms |
| generated/3 sites l1+l2+l3 | 12 | 1,109,191 | 146,755 | 9,140 | 107 ms | 14.2 ms |

With `--icons cisco`:

| Page | Views | Bytes | gzip | DOM |
|---|---|---|---|---|
| `home-lab` l1+l2+l3 | 12 | 139,263 | 28,648 | 967 |
| `campus` l1+l2+l3 | 12 | 539,500 | 79,883 | 4,492 |
| generated/3 sites l1+l2+l3 | 12 | 1,020,217 | 130,178 | 8,826 |

Side by side, on the twelve-view pages, which is the shape the entry was opened
about:

| Page | Before | After | |
|---|---|---|---|
| `home-lab` | 190,346 | 148,661 | 1.28× |
| `campus` | 879,890 | 624,237 | 1.41× |
| generated/3 sites | 1,584,584 | 1,109,191 | 1.43× |
| `home-lab` `--icons` | 245,475 | 139,263 | **1.76×** |
| `campus` `--icons` | 1,115,352 | 539,500 | **2.07×** |
| generated/3 sites `--icons` | 2,205,617 | 1,020,217 | **2.16×** |

And the number the entry is really about — one more view:

| Inventory | Before | After | | Before, icons | After, icons | |
|---|---|---|---|---|---|---|
| `home-lab` | 12,995 | 8,884 | 1.46× | 17,783 | 7,544 | 2.36× |
| `campus` | 76,570 | 51,471 | 1.49× | 98,336 | 42,928 | 2.29× |
| generated/1 site | 22,542 | 15,009 | 1.50× | 31,809 | 13,486 | 2.36× |
| generated/3 sites | 134,430 | 88,586 | 1.52× | 187,506 | 80,151 | 2.34× |

Three columns did **not** move, and saying so is the honest half of the result.

**gzip barely improves**: `campus l1+l2+l3` goes 104,712 → 95,199, 1.10×,
against 1.41× uncompressed, and on the small pages it goes very slightly *up*
(`home-lab l1`: 19,287 → 19,738) because the client grew by ~1.8 kB and there is
now less redundancy left for deflate to earn its keep on. That is arithmetic
rather than disappointment: this pass removed by construction most of what
deflate was removing anyway. A page **served** with `Content-Encoding: gzip` was
never the problem; a page **emailed** was, and that is the column that moved.

**DOM node count is unchanged**, to the element. A `<use>` is an element where
an `<image>` was, an attribute moved is not an element, and a pooled record is
not in the DOM at all. Nothing here reduces the number of shapes a browser
builds, because the shapes are the drawing.

**Paint and switch track the bytes loosely and are dominated by the layout.**
First paint on `campus l1+l2+l3` goes 67–76 ms to 63 ms and on generated/3 sites
108–120 ms to 107 ms; a switch is unchanged at 9.4 ms and 14.2 ms, which is what
it should be — switching is unhiding a drawing the browser already built, and
this pass did not change how many elements that drawing has.

### The regression guard

`tests/test_html.py::test_an_extra_view_costs_its_drawing_and_little_else`,
parametrised over no icons and `--icons cisco`. It renders `campus` twice, once
holding l1 and once holding l1 and l2, and compares them. l1 and l2 draw *the
same elements* — the second is the first annotated with VLANs — so the second
page must differ from the first by the four drawings it gained and by as little
else as possible.

Three bounds, because the three payloads fail differently and no one number
catches all of them. The first two are ratios, so they describe the shape of the
output rather than a Graphviz release; the third is a byte count, which is what
it takes to notice a payload that grew *inside* a drawing, since such a payload
inflates any denominator taken from the drawings and hides itself there.

| | today | icons | fonts | records | all | max |
|---|---|---|---|---|---|---|
| page / drawing bytes | 1.03 | 1.03 | 1.02 | 1.61 | 1.41 | 1.10 |
| …with `--icons cisco` | 1.04 | 1.02 | 1.02 | 1.78 | 1.28 | 1.10 |
| record block, 2 layers / 1 | 1.04 | 1.04 | 1.04 | 2.00 | 2.00 | 1.15 |
| bytes per element per view | 543 | 543 | 806 | 848 | 1110 | 780 |
| …with `--icons cisco` | 428 | 893 | 690 | 732 | 1459 | 780 |

The middle columns are each change disabled on its own, so the table says which
row catches which revert: every one of the three is over a threshold in at least
one row, and a full revert is over four of the five. These are byte counts of a
deterministic renderer with no run-to-run spread at all, so the thresholds can
be — and the two ratios are — far tighter than any of the timing guards in
entries 5 and 7 would dare be.

The last two rows are the exception, and 2026-08-15 is when that showed. They
are byte counts of a *drawing*, and a drawing is Graphviz's output: 96 % of what
an added view costs is the SVG itself (595 bytes per element on `campus`, of
which 569 are inside an `<svg>`). So the figure moves with the Graphviz release
and not with anything in this repository — 543 → 595 on the 2.43 that
`ubuntu-24.04` ships, and 704 on the 15.x that `macos-14` and `windows-latest`
now install, where the same layout is spelled more verbosely. The two ratios did
not move at all, which is what says the growth is Graphviz's and not the page's.

The threshold was raised from 650 to **780** for that: above the fattest figure
any runner in the matrix produces today, with 11 % headroom, and still below
every reverted column in both Graphviz generations — the smallest of those is
806 on 2.43, and each is a good fifth larger on 15.x. That is the whole of the
room there is. Raising it further would start letting a real regression through,
so the next release that moves these numbers wants the table re-measured rather
than another bump.

Two sharper guards sit next to it, because a ratio is a blunt instrument for a
property that can be stated exactly.
`test_the_font_attributes_are_stated_once_and_still_resolve_the_same` requires a
drawing to hold exactly one `font-family`, and
`test_an_icon_is_stored_once_however_many_views_draw_it` requires the count of
`data:` URIs in a three-layer page to equal the number of *distinct* icons in
it — 5 on `campus`, where it was 396.

### What did not change

The picture, and the page's behaviour around it.

**The picture is identical, and that is checked against a browser rather than
argued.** `campus l1+l2+l3` rendered before and after this entry, opened in
Chromium and screenshotted, is **byte-identical** without icons — the font
hoisting is invisible, as inheritance says it must be. With `--icons` it is
identical to within **215 subpixels of 3,780,000, at a maximum intensity
difference of 8/255**, all of them on icon edges: a `<use>` establishes a nested
viewport and the rasteriser rounds inside it slightly differently. That is
antialiasing, it is confined to the artwork, and it is recorded here rather than
rounded away.

**The interactive behaviour is identical**, checked the same way: a script drove
both pages through selecting a node, reading the detail panel, hovering for the
card, searching, dimming a namespace, switching layers and selecting a node at
the new one, both toggles, `/`, `f` and an arrow key, and finally re-opening the
page at an element's deep link. Every observation matched between the two, with
no console error on either, and the only difference in the whole transcript is
that the new page has 22 `<use>` elements where the old one had none — all 22
resolving to a box with a non-zero width, which is the check that they actually
found their symbols.

**The Content-Security-Policy is unchanged in kind and stricter in nothing.**
Still `default-src 'none'; img-src data:` with a hash per inline block, no
`'unsafe-inline'`, no `'unsafe-eval'`, no `style=` attribute anywhere and no
markup assigned from data: the drawings are still server-rendered elements in
the document, not a blob the client unpacks. The only new references a page
holds are `#`-fragments from a `<use>` to a `<symbol>` in the same document,
which fetch nothing;
`test_every_same_document_reference_names_something_the_page_holds` requires
each of them to name an id the page actually has.

**The records themselves are unchanged.** A record is still the `-f json` export
plus an element id and a links cross-reference — only *where it is stored*
changed — so `detail.js`, `netviz web` and the `-f json` exporter are
untouched by this entry.

Three test expectations changed, each because it was asserting the old storage
rather than the property it was named for:
`test_the_records_are_the_json_export_keyed_by_element_id` and
`test_a_hostile_description_stays_inside_the_record_block` now read the pools
(through one shared `records_of` helper that is the client's own four lines of
reassembly), and `test_the_prepared_svg_scales_with_its_box` now looks for
`width=` on the root element rather than anywhere in the document, since a
hoisted symbol legitimately fills its box with one. `docs/home-lab.html` is
re-rendered: 192,066 → 149,847 bytes.

### What is deliberately not done

**Deltas between views.** The original entry proposed them and the measurement
argued against: the four variants of one layer differ in *layout*, not in
labelling — hiding the addresses shrinks every box, which moves every
coordinate — so a delta between two of them is nearly the whole drawing. The
8 % that deflating all twelve as one stream buys over deflating them separately
is the honest ceiling on what any cross-view sharing was worth here.

**Compressing the drawings into the page.** `DecompressionStream` would take
`campus l1+l2+l3`'s drawings from 428 kB to 69 kB deflated, 93 kB once base64 makes it embeddable, which is a
larger win than everything above. It is not taken, and the reason is the
Content-Security-Policy posture rather than the browser support: the drawings
would arrive as text and be turned into elements by the client, which is exactly
the "no markup is ever assigned from data" property the page is built on and the
policy exists to keep honest. Trading that for bytes is a different decision
from the ones in this entry, and not one to make silently.

**A flag that embeds fewer views.** Also proposed by the original entry, also
not taken — for the reason it gave itself: the toggles are why the format
exists. With a view now costing its drawing and nothing else, the case for a
flag that removes one is weaker than it was, not stronger.

---

## 9. ~~A 1000-device diagram is a hairball~~ — fixed, 30× the layout, 200× the file

**Status:** closed 2026-07-29. `--collapse`, `--collapse-depth` and
`--bundle-links` summarise a graph instead of narrowing it.

Entries 1, 5 and 7 all measured the *pipeline* on `tools/bench_pipeline.py`'s
default tree and made it fast. None of them measured the **diagram**, and the
diagram was where the size actually hurt: every filter netviz had removed
detail by removing elements, so a reader of a 1056-device tree could ask for a
part of the network but never for a summary of the whole of it. `dot` will lay
1056 nodes out — but the result is 3.5 MB of SVG that no one can read.

### The harness

`tools/bench_pipeline.py --aggregate`, on the same default tree — **1056 devices
in 2106 documents across 138 files, 1.2 MB of YAML**, through libyaml. It times
the transform *and* the Graphviz layout, and measures the SVG, because the
transform is linear in the graph and `dot` is superlinear in it: the whole
return on collapsing a tree is what the layout no longer has to do.

Two trees are timed. The default has one uplink per rack switch and therefore no
parallel links at all — which is why `--bundle-links` is a no-op on it, and that
is worth recording rather than hiding. `--uplinks 4` gives every rack switch a
four-member LAG to its site router, which is the shape bundling exists for; the
flag defaults to 1 so the tree entries 1, 5 and 7 measured is unchanged.

### Measured, default tree (1056 nodes, 1050 edges)

| Aggregation | Nodes | Edges | Transform | Layout | SVG |
|---|---|---|---|---|---|
| `--no-bundle-links` | 1056 | 1050 | — | 835 ms | 3 486 kB |
| *(default: LAG only)* | 1056 | 1050 | 1.5 ms | 781 ms | 3 486 kB |
| `--bundle-links` | 1056 | 1050 | 2.1 ms | 790 ms | 3 486 kB |
| `--collapse-depth 1` | **6** | **0** | 22.3 ms | **28.5 ms** | **16.7 kB** |
| `--collapse-depth 2` | 12 | 42 | 22.3 ms | 39.9 ms | 90.8 kB |

### Measured, four-member LAG per rack (`--uplinks 4`: 1056 nodes, 1176 edges)

| Aggregation | Nodes | Edges | Transform | Layout | SVG |
|---|---|---|---|---|---|
| `--no-bundle-links` | 1056 | 1176 | — | 919 ms | 3 658 kB |
| *(default: LAG only)* | 1056 | **1050** | 3.8 ms | 795 ms | 3 568 kB |
| `--bundle-links` | 1056 | 1050 | 3.3 ms | 794 ms | 3 546 kB |
| `--collapse-depth 1` | 6 | 0 | 23.0 ms | 28.7 ms | 16.7 kB |
| `--collapse-depth 2` | 12 | 42 | 24.1 ms | 55.2 ms | 167 kB |

### What the numbers say

* **Collapsing is the lever that matters.** `--collapse-depth 1` turns 1056
  nodes into 6 and 835 ms of layout into 28 ms — **29× faster, and 209× smaller**
  — because Graphviz's cost is in the node and edge count and collapsing removes
  almost all of both. The transform costs 22 ms, i.e. 3 % of what it saves, and
  is dominated by re-deriving each namespace's subnet list from its members'
  addresses.
* **Bundling is a legibility change, not a performance one.** Folding 126 LAG
  members into 42 edges buys 13 % of the layout and 2 % of the file. That is the
  honest result: bundling exists so a four-cable LAG reads as one link, not so a
  large tree renders faster. It is on by default anyway because it costs 4 ms and
  removes a band of stacked parallel lines that says nothing.
* **The default is nearly free.** LAG bundling on a tree with no LAG in it costs
  1.5 ms on 1050 edges and returns the graph object unchanged, so a rendering of
  an inventory that declares no aggregate is byte-identical to what it was.

### What is deliberately not done

`--collapse` does not *summarise the summary*: an aggregate node lists its
element count per kind, its VLANs and its prefixes, but not, say, the internal
diameter or the oversubscription ratio. Those are analyses, and
`netviz render -f json` now exports the element list behind every box, so a
consumer that wants one can compute it without netviz guessing which one.

The other bound worth naming: `--collapse-depth` counts from the shallowest
namespace every element shares, which makes depth 1 mean "one node per site" in
the trees people actually have. In a tree with no shared root — several
top-level directories — depth 1 collapses each of them, which is the same rule
producing a different answer, not a special case.

---

## 10. netviz now depends on a second YAML parser

**Status:** accepted 2026-07-29, deliberately. `ruamel.yaml` is a runtime
dependency, used by `netviz fmt` and by nothing else.

**Why a second parser at all.** `netviz fmt` has to preserve comments, blank
lines, quoting style and whether a collection was written flow or block. PyYAML
discards all four during parsing — that is not a gap in it, it is most of why it
is fast — and there is no configuration that changes this. A formatter built on
PyYAML would have to reproduce the source layout from a token stream it does not
keep, which is writing a round-trip parser and calling it something else.

**What was considered and rejected.**

- *Reimplementing round-trip parsing.* A YAML parser that keeps comment
  positions and scalar styles is thousands of lines and is where formatters go
  wrong. It would also be a third opinion in this repository about what a plain
  scalar means, and the divergences recorded below show that two is already
  enough to have to reconcile.
- *Making `fmt` an optional extra.* Rejected: it is a published pre-commit hook
  and a documented CI step, and an extra that half the users do not install
  turns "run `netviz fmt`" into a support question.
- *Replacing PyYAML with ruamel everywhere.* Rejected on measurement. The
  loading path is the throughput bottleneck (entries 1 and 5) and is currently
  libyaml-backed; ruamel's round-trip parser is pure Python and much slower, and
  the strictness `netviz.loader.documents` adds — duplicate-key rejection, the
  YAML 1.2 boolean rule — would all have to be rebuilt on a different API for a
  path that has no use for a single thing round-tripping buys.

**What the dependency is fenced with.**

- **Nothing on the loading path imports it.** `netviz.fmt` is imported lazily,
  inside `fmt_command`, so `validate` and `render` do not pay its ~30 ms of
  import time — an eighth of what starting the CLI costs at all, and `validate`
  runs in a pre-commit hook. `netviz.loader` is untouched by this work.
- **The two parsers are checked against each other on every format.**
  `netviz.fmt.verify` re-reads every formatted file with the *strict* loader
  and compares it against what that loader read before. Nothing is written that
  the two disagree about, so a divergence is a refusal rather than a corruption.
- **The divergences are real, and the fence caught them.** Building this found
  two places where ruamel and PyYAML disagree about the same bytes: ruamel reads
  `1:02` as the string `"1:02"` where PyYAML reads the integer 62, and ruamel
  emits `::1/128` unquoted inside a flow sequence, which PyYAML then refuses to
  parse. Both were found by the verification pass failing on
  `examples/`, not by review. They are handled in
  `netviz.fmt.scalars` — `is_untouchable` and `plain_survives` respectively —
  and both ask netviz's own loader for the answer rather than assuming one.

**What would justify revisiting this.** A PyYAML release that can round-trip
comments, or a `fmt` that needs to run on the loading path — neither of which is
in sight. If ruamel became unmaintained, `netviz.fmt.canonical` is the only
module that imports it, and `docs/format.md` is a specification precise enough
to reimplement against.

---

## 11. A YAML parser can still be crashed from outside netviz's control

**Status:** bounded 2026-07-29, not eliminated. Raised by the loader fuzz target
added with the property tests (`tests/test_fuzz_loader.py`).

Fuzzing the loader found three ways a document could get past every diagnostic
netviz writes and reach a failure netviz does not own. All three are fixed;
what is *not* fixed is the underlying reason they were possible, which is that
the parser is a dependency and its limits are not netviz's.

**What was found and fixed.**

- **An integer literal of more than 4300 digits.** CPython refuses to convert
  one (CVE-2020-10735), and PyYAML's constructors call `int()` on whatever the
  resolver matched — so `mtu: 999…9` came out of the *parser* as a bare
  `ValueError` about `sys.set_int_max_str_digits`, past a `try` that caught only
  `yaml.YAMLError`. `netviz.loader.documents` now translates it, and the three
  places netviz itself calls `int()` on document text — an interface range, a
  patch-panel port range, a prefix length — bound the digit count first.
- **Nesting deeper than the parser's stack.** The pure-Python composer recurses
  once per level and raised an uncatchable-in-practice `RecursionError` at a few
  thousand; libyaml's composer recurses in C and **segfaults** at around thirty
  thousand, which no `except` clause can catch at all. Which of the two runs
  depends on the PyYAML wheel, so one file was a traceback on one machine and a
  killed process on another. `MAX_NESTING_DEPTH` now bounds it before either
  parser sees the text, measured with the *scanner*, which is iterative in both.
- **A patch-panel port range expanded before it was counted.** `ports:
  1-999999999` built a billion strings and then checked the limit. The check is
  arithmetic now.

**What remains.** The nesting guard is netviz putting a fence in front of
somebody else's cliff. It costs a C-speed `str.count` on every document and a
full scan only for one carrying more than `MAX_NESTING_DEPTH` flow openers —
every example inventory in this repository has fewer than 110 in total — so the
price is right, but the guard exists because *libyaml crashes the process*
rather than because 256 levels of nesting is a meaningful schema limit. A
document that nests 257 deep is refused with an accurate diagnostic that
describes netviz's limit and not the real one.

The number was 1024 until it was measured against the parser it was protecting
rather than against the one that crashes. The pure-Python composer spends two
Python frames per level, so under CPython's default recursion limit it gives out
somewhere past 450 — and sooner when netviz is called from a stack that is
already deep. A limit above that ceiling is a limit at which the two parsers
still disagree, which is the one thing this guard exists to prevent: a document
*at* the documented maximum was refused by one and accepted by the other.

**What would justify revisiting this.** libyaml growing a depth limit of its
own, or PyYAML exposing one. Either would let the guard be dropped in favour of
translating whatever error the parser produced, which is what every other
malformed document already gets.

---

## 12. The `validate` timing guard is at its limit under coverage

**Status:** closed 2026-07-30. The honest fix this entry named — measuring both
halves under the same conditions — is now what the guard does; the rest of the
entry is the history that led there and is kept for the numbers in it.

`tests/test_performance.py::test_validating_costs_no_more_than_its_budget_above_an_address_walk`
compares `validate` against an address-walk floor and asserted a ratio of at most
**8.5**. That number came from entry 7, where the measured spread was 6.9–7.2 —
17 % of headroom. Measured on this machine while adding the routing rules:

| | uninstrumented | under `pytest-cov` |
|---|---|---|
| before §16 | 7.8–8.2 | 8.50–8.56 |
| after §16 | 8.0–8.1 | 8.50–8.68 |

The instrumented column is the one CI reads, because coverage is on by default in
`pyproject.toml`. Coverage traces per *line executed*, and `validate` is a hundred
small functions where the floor is one tight loop — so the ratio is systematically
higher under it, and the guard was already failing about two runs in five
**before** the routing rules existed.

**That the routing rules are not the cause is measured, not assumed.**
`tools/profile_validate.py` reports 0.0 ms for each of `E032`–`E036`, `W135` and
`W136` on a 1056-device tree, and commenting all seven out of `_CHECKS` leaves the
instrumented ratio at 8.57–8.65 — indistinguishable from having them. What §16 did
add was context building, and that is why `_collect_routing` is one pass gated on
`_routes_anything`, and why the address index behind a BGP peer lookup is built
only when a session needs resolving.

The threshold is now **9.0**, which keeps the property the guard exists for: entry
7 records that reverting the `validate.py` half of it alone gives 9.1 against a
"today" of 6.9–7.2, so the same revert against a today of 8.6 lands far above 9.0.

**How it was closed.** 9.0 was not enough either: CI read **9.07** on a commit
that had regressed nothing, which is what a threshold 5 % above the measured
spread does on a shared runner. Two changes, in the order they matter.

*The tracer is now paused for the duration of every measurement in the file*
(`tracing_paused` in `tests/test_performance.py`, `Coverage.current().stop()` and
`.start()` around each timed call). This is the first of the two options this
entry offered, and it is the one that removes a *systematic* error rather than
budgeting for it: coverage costs time per line executed, so the hundred small
functions of `validate` pay far more than the floor's one tight loop, and the
ratio read half a point high for a reason that has nothing to do with netviz.
The lines that go untraced during the measurement are `validate` and the loader,
which several hundred other tests execute; total coverage did not move.

*The best of eight rounds rather than four*, because the floor is the noisier
half — a tenth of a millisecond per walk — and a minimum only gets closer to the
truth with more attempts.

| | before | after |
|---|---|---|
| measured ratio, under `pytest --cov` | 8.50–9.07 | 8.17–8.56 |
| threshold | 9.0 | 9.5 |
| headroom above the worst sample | 0–6 % | 11 % |

Confirmed across the matrix afterwards: 7.04 to 8.48 over six jobs, the worst of
them Windows, against 9.5.

The threshold moved with it, and the guard keeps what it is for: entry 7 records
that reverting the `validate.py` half of its work alone gives 9.1 against a
"today" of 6.9, which is 11.0 against a today of 8.4 — well clear of 9.5, as are
the two larger reverts.

**And the *load* guard's premise does not survive leaving Linux.** One commit,
all six CI jobs, once both guards began reporting what they measured:

| job | load/floor | validate/floor |
|---|---|---|
| ubuntu-24.04 3.10 / 3.11 / 3.12, libyaml | 1.59 / 1.56 / 1.60 | 7.31 / 7.54 / 7.48 |
| ubuntu-24.04 3.12, pure Python | 1.09 | 7.31 |
| macos-14 3.12, libyaml | 1.46 | 7.04 |
| windows-latest 3.12, libyaml | 1.76 | 8.48 |

The three Linux libyaml jobs agree within 0.04. Windows is 0.16 above them and
macOS 0.13 below — both inside the 1.60-to-1.79 band that guard exists to
discriminate within, so against the old threshold of 1.70 it was not
discriminating on Windows, it was failing.

That is the ratio premise failing rather than noise. Machine speed cancels out
of a ratio when both halves are the same kind of work, and the two halves here
are not: the floor reads forty files and runs a C parser over them, the
numerator adds pydantic on top, and the balance between filesystem and
interpreter is exactly what differs most between those two runners. The
`validate` guard is unaffected because both of *its* halves run over an
inventory already in memory.

So the libyaml ceiling is 1.70 on Linux and 1.95 elsewhere. The sharp copy runs
on all four Linux jobs, which is where it was calibrated and where a pull
request will meet it; the other two get a threshold that catches a catastrophic
regression and not an entry-5-sized one, on the same terms the pure-Python row
has always been kept.

**What to watch.** The Linux ceiling has 4–8 % of headroom over a measured
1.57–1.64, which is thin. Both guards now print `[perf] <name>: <ratio>x against
a budget of <budget>x (<n>% headroom)` on every job, so the next person to touch
either number can read the spread off six jobs instead of guessing from the one
that failed. If the Linux row starts flaking, that log is the input, and this
entry is where the new number gets written down.

**2026-08-16: the `validate` guard did not survive leaving Linux either, and
"unaffected" above was one commit's worth of evidence.** CI read **9.85** on
`10f9284c`, windows-latest, against the 9.5 set here — a commit that had
regressed nothing. This is what the `[perf]` line was added for, so the
replacement number was read off the runners rather than guessed. Forty-eight
samples, the four CI runs of 2026-08-15 and -16:

| job | validate/floor | samples |
|---|---|---|
| ubuntu-24.04, all four jobs | 7.19–8.64 | 32 |
| macos-14 3.12 | 6.87–8.33 | 8 |
| windows-latest 3.12 | 8.54–9.85 | 8 |

Windows is not noisier around the same centre — it sits about 0.8 above it, with
a median of 8.8 — so 9.5 left that one job 7 % of headroom while leaving the
other five 10–28 %, and 7 % is inside what a shared runner moves in a bad
minute. The 9.85 was 12 % above its own median, not an outlier from the pooled
spread.

Both halves being memory-resident makes the *parser* cancel out of this ratio,
which is what the paragraph above got right; it does not make the interpreter
cancel out, and the numerator is a hundred small rule functions where the floor
is one tight loop. That is the same shape of premise failure as the load guard's,
and it gets the same shape of fix: `MAX_VALIDATE_RATIO_WINDOWS = 11.0`, 12 %
above the worst sample seen and 25 % above the median, with 9.5 unchanged
everywhere else.

Blunter, and still worth running. Scaled from the 6.9 baseline entry 7's catch
table is written against to the 8.8 this platform has, a revert of `validate.py`
reads 11.6 there and one of `models/interface.py` reads 17.5 — so both pieces the
sharp copy catches are still caught, the first of them only just. That last
margin is the reason 9.5 stays on the other five jobs instead of everyone moving
up to 11.0.

---

## 13. `netviz --version --json` is not the spelling that works

**Status:** deliberate, 2026-07-30. The machine-readable report is
`netviz version --json`.

`--version` is an *eager* Click option: its callback runs before any other
parameter is processed, which is why `netviz --version` answers from a directory
holding no inventory and does not care whether `-i` names a path that exists.
Click parses the whole argument list before running any callback, though, so
`netviz --version --json` fails during parsing with `No such option '--json'` —
the eager callback never gets the chance to notice the second flag.

Three ways to make that exact spelling work were considered and none is worth it:

- **A hidden `--json` on the group.** Click processes eager parameters in the order
  they appear on the command line, so `--version` would still be handled first and
  would print text having silently ignored `--json`. Worse than an error.
- **Make `--version` non-eager** and print from the group body. Then `-i` is
  validated first, so `netviz -i /nonexistent --version` would fail to report a
  version — precisely when a user most wants one.
- **An optional value** (`--version=json`). This one works, but it is a spelling
  nobody guesses, and it would sit next to `netviz version --json` doing the same
  job.

So the report lives on a command instead: `netviz version` for the text and
`netviz version --json` for the document, with `-V`/`--version` kept as the eager
shortcut for the text form. The flag's own help text names the command, and
`docs/commands/version.md` documents both.

**What would justify revisiting this.** Click growing a way to order eager
parameters independently of the command line, at which point the hidden-flag
approach becomes correct rather than merely tempting.

---

## 14. ~~Every command re-parses the whole tree~~ — fixed, 3.3× cold-process, 21× in-process

**Status:** closed 2026-07-30. `netviz.loader.cache` remembers a parsed file by
the hash of its bytes; `netviz cache info|clear` and `--no-cache` are the
controls.

Entries 1, 5 and 7 cut the constant factors of `load_tree` and `validate` — 3.3×,
1.41× and 3.1× — but every one of them left the work *O(inventory) per
invocation*. That is felt worst where the least has changed: `watch` re-renders on
a keystroke-sized edit, `validate` runs from a pre-commit hook on a two-line diff,
the web preview reloads.

### The harness

`tools/bench_incremental.py` (new), on `tools/bench_pipeline.py`'s default tree —
**1056 devices in 2106 documents across 138 files, 1.2 MB of YAML**, through
libyaml, median of five. It times the same tree six ways and, unlike
`bench_pipeline.py`, it edits a file between two loads, because that is the case
the cache exists for and the only one whose number can be quoted about `watch`.

### Measured

| | Load | Of a cold load |
|---|---|---|
| cold, no cache (what every command did) | 443 ms | 1.00 |
| cold, filling the cache | 529 ms | 1.19 |
| **warm, next process** (disk tier) | **135 ms** | **0.30** |
| **warm, same process** (memory tier) | **21 ms** | **0.05** |
| **reload after editing one 15 kB file** | **30 ms** | **0.07** |

138 entries, **171 kB on disk** for 1.2 MB of YAML — the elements serialise to
2.26 MB of JSON and zlib takes that to 171 kB, a 13× fold that is worth the 1.9 ms
it costs to undo.

### Where the warm 135 ms goes

| Step | Cost |
|---|---|
| read all 138 inventory files and hash them | 2.2 ms |
| read the 138 cache entries | 4.5 ms |
| `zlib.decompress` both sections of each | 1.9 ms |
| `json.loads` the per-file bookkeeping | 0.8 ms |
| **pydantic re-validating the elements** | **~120 ms** |

So the disk tier is *entirely* pydantic. Reconstruction goes back through the same
validators the document went through — which is what makes a tampered entry
harmless — and those validators do not know the values already passed once. The
memory tier skips them, and that is the whole of the 135 ms → 21 ms difference.

### The cycle, and what now dominates it

The load is incremental. **Nothing after it is**: reference resolution,
validation and the graph build all run over the whole inventory, from models that
are already in memory.

| Stage of one `watch` cycle | Cold | Incremental |
|---|---|---|
| load | 443 ms | 30 ms |
| `validate` | 89 ms | 89 ms |
| `build_graph` | 43 ms | 43 ms |
| `render -f dot` | 100 ms | 100 ms |
| **total** | **676 ms** | **263 ms** |

**2.57× the cycle, not 14×.** The load was 66 % of a cold cycle and is 12 % of an
incremental one, so the honest summary is that this entry closed the load and
opened the next question: `validate`, `build_graph` and the renderer are now 88 %
of a re-render, and none of them is incremental. That is entry 15's problem, and
it is a harder one — a finding can depend on any pair of elements in the tree, so
"re-validate only what changed" needs a dependency graph rather than a hash.

Two things follow from the same numbers and are worth saying plainly:

* **Filling the cache costs 19 %.** A CI runner that starts empty and is thrown
  away pays that for nothing, which is why `docs/configuration.md` tells it to set
  `NETVIZ_NO_CACHE=1` or to persist the directory rather than leaving the
  default in place.
* **The win is much larger on the pure-Python parser** — 0.06 rather than 0.30 of
  a cold load — because the denominator is five times bigger there. A machine
  whose PyYAML has no libyaml bindings gets the most out of this.

### The design, and the two things it refuses to do

The key is `sha256(identity, relative path, file bytes)`. No timestamp: a file
rewritten identically hits, a `git checkout` of an old revision hits again, a
`touch` changes nothing. The *identity* is the netviz version, the document
`apiVersion`, the selected YAML parser, the pydantic and PyYAML versions, and a
digest over the mtimes and sizes of netviz's own sources — that last one so
that editing a validator invalidates the cache in a source checkout, where the
version number would not move.

**Not a pickle.** An entry is a header line, then two zlib sections: the
bookkeeping as JSON, and the elements as pydantic's own JSON. It is reconstructed
through the validators, so an entry somebody has written into can be *refused*
but cannot construct an object, let alone run code.

**Not everything is cached.** A file declaring a `kind: template`, or a device
inheriting one with `spec.from`, depends on another file's bytes, so a key over
one file cannot see it change; those stay on the slow path and are counted. Nor
is anything cached under `validate --format json|sarif|github`, which keeps the
per-field provenance that *is* the YAML node tree.

### The regression guard

`tests/test_performance.py::test_a_warm_load_costs_a_fraction_of_a_cold_one`
asserts the two ratios above against 0.55 and 0.20 — measured 0.30-0.34 and
0.084-0.090, so 60 % of headroom each. It is the one guard in that file that is
*helped* by coverage (the warm path executes far fewer traced lines than the
parser does) rather than squeezed by it, which is the concern entry 12 records.

`tests/test_cache.py` holds the correctness half: over every committed example, a
hit produces the same elements in the same order, the same diagnostics in the same
order, the same source locations and the same rendered bytes as a cold load. Then
one test per failure mode a cache introduces — bytes that changed, a version that
changed, an entry truncated, an entry filled with random bytes, an entry whose
body is not zlib, an entry written for a different key, an entry edited into
something the models reject, a half-written temporary file, four processes filling
one cache at once, and a cache swept back under its cap. Every one of them has to
end as a parse, because the alternative to a hit is never an error.

### Measured and rejected

**Reconstructing the models without validation.** ~120 ms of the warm 135 ms is
pydantic, and `model_construct` would skip it — recursively, by hand, for twenty
models. It was rejected on two counts: it is the property that makes a tampered
entry harmless, and a hand-written reconstructor that drifts from the models is a
class of bug with no symptom other than a wrong diagram. A validation *context*
that let each cross-field check opt out on a trusted payload would be the
supported way to buy most of it back, and it would touch every validator in the
model layer; that is a change worth making on its own evidence, not as part of a
cache.

**`exclude_defaults` on the serialisation** would have cut the 2.26 MB of JSON
substantially. It also drops `kind`, which is the discriminator of the element
union, so the payload no longer validates at all. Not pursued further: the
compressed size is 171 kB either way, and the JSON parse is 5 % of the warm load.

**`fsync` per entry** was in the first version and cost **405 ms** for 138 files,
turning a 19 % fill overhead into 92 %. Entries are now written atomically but not
durably (`write_bytes_atomically(sync=False)`): a cache that does not survive a
power cut is worth less than the 3 ms per file, and a torn entry is a case the
decoder already has to handle because a killed process produces the same thing.

**One pack file per inventory** instead of 138 entries would have made the reads
one `open` instead of 138 — worth 4 ms of 135. It was rejected for what it costs
elsewhere: every write rewrites the whole pack, LRU eviction becomes all-or-
nothing, and two processes filling it concurrently lose each other's work rather
than merely racing on one key.

---

## 15. A cached element forgets which endpoint was written first

**Status:** open, and not currently reachable from any command.

`InterfaceRef.document_index` records where an endpoint sat in the *document*
before `sort_endpoints` moved it (§7.1), so that a diagnostic about
`spec.endpoints[1]` points at the line that actually holds it rather than at the
other end of the cable. It is a `PrivateAttr`, and the parse cache stores an
element as **pydantic serialises it** — sorted — so a cache hit reconstructs the
endpoints in canonical order and every `document_index` comes back as the
canonical position.

Nothing surfaces it today. The only consumer is `_Endpoint.field_path` in
`netviz.validate`, whose field paths reach the user solely through the
machine-readable `validate` formats — and those pass `keep_provenance=True`,
which disables the cache by construction. The text format reports a document,
not a field.

It was found by the edit layer, which needed the same fact for a different
reason: to rewrite the right endpoint of a cable when an element is renamed.
That is why `netviz.edit.references.locate_reference` does not trust the
index it is given. It uses it as a hint, checks that the value there reads as
the reference the model reported, and otherwise searches the sibling entries for
the unique one that does — which is the right behaviour regardless, since a
document may write its endpoints in either order.

A fix would have to make the serialised form carry the written order, most
plausibly by emitting `spec.endpoints` in document order and letting
`sort_endpoints` re-derive the index on the way back in. That is a change to a
model serializer shared by `netviz show` and by every consumer of
`model_dump`, so it wants its own change and its own golden review rather than
being smuggled in beside an unrelated feature.

---

## 16. Diagram geometry is a sidecar, not a field on each element

**Status:** decided and implemented; recorded here because the alternative is
the obvious one and somebody will propose it again.

`netviz layout` had to put a node's position *somewhere*, and there were two
plausible places:

**(a) On the element.** `spec.position: {x: 240, y: 396}` on each device, next
to its interfaces.

**(b) In its own document.** `kind: layout`, keyed by element address, scoped by
view — which is what was built (§18 of `docs/schema.md`,
`netviz.models.layout`).

(b) won on four counts, and the fourth is the one that settles it.

**One element, several positions.** The same switch is drawn in `l1`, `l2`,
`l3`, `overlay`, `routing` and `power`, and it sits somewhere different in each
— the l3 diagram is a different graph with different neighbours, not the same
diagram recoloured. A single field on the device cannot hold six answers, so (a)
becomes `spec.positions.l1`, `spec.positions.l3`, … on every device: the sidecar
schema, inlined into a hundred files.

**Not everything drawn is declared.** A layer-3 prefix node, a tunnel drawn as a
box, a rack elevation, a collapsed namespace — none of these is an element, and
none has a `spec` to put a position in. (b) keys by *node id* and takes them in
its stride (`subnet:10.0.0.0/24`, `rack:hq/comms/r1`); (a) would need a second,
sidecar mechanism for exactly those, which is (b) with extra steps.

**A model file should be readable.** A device document is a description of
hardware — ports, addresses, VLANs — and it is reviewed as one. Four numbers per
view interleaved with that is noise in every diff of every device forever, and
the numbers change on a drag rather than on a change to the network. Keeping
them apart means `git log -- switches/` still answers "what changed about the
switches".

**An arrangement is a unit.** It is dropped as a unit (`--clear`), regenerated
as a unit (`--write`), and reviewed as a unit. It can be `.gitignore`d by a team
that does not want one, or committed by a team that does; a second arrangement
for a different audience is a second document rather than a second field on
ninety-seven devices. None of that is expressible if the geometry is spread
across the model.

### What the choice costs

Two things, both accepted.

*A layout key can go stale.* Deleting a switch leaves its coordinates behind,
where a field on the element would have gone with it. That is `W138`, a warning
rather than an error — a diagram must not stop validating because a device was
retired — and `netviz layout --prune` is the fix. The rule only checks keys
that name *elements*; a `subnet:` key can only be judged against a drawing, and
`--prune` builds one, so a prune removes a little more than the rule reports.

*A layout file is uncacheable.* The parse cache stores elements, and a layout is
not one, so a file declaring one stays on the slow path — the same treatment
`kind: template` gets, for the same reason (`netviz.loader.tree._Builder.harvest`).
An arrangement is one small file, so this is measured in microseconds; it would
stop being acceptable if geometry were ever moved *into* the element files,
which is one more reason not to.

### What was deliberately not stored

**Node sizes.** `NodeGeometry.size` exists in the schema and is honoured on read,
but `--write` does not seed it. Graphviz derives a node's box from its label on
every run, so a stored size buys the renderer nothing — and it goes stale the
moment a device grows an interface, in a way that only a client drawing from the
JSON export would ever notice. It stays in the schema for a canvas editor that
lets somebody resize a box on purpose.

**Edge waypoints.** Stored and honoured, but not seeded unless `--waypoints` is
given. A seeded spline is four control points per link that the render
recomputes identically from the node positions; a *hand-placed* bend is a
decision, and that is what the flag is for.

---

## 17. Graphviz 2.43 segfaults on text in a `_background`

**Status:** worked around, and the workaround is why an arranged diagram's
namespace captions sit where they do.

A fixed arrangement is drawn by `neato -n2`, and `neato` does not draw clusters —
only `dot` and `fdp` do. So the namespace frames a `--group-by-namespace` render
would otherwise lose have to be drawn by netviz, from the boxes the
arrangement stores. The obvious mechanism is the `_background` graph attribute,
which takes xdot draw operations and which Graphviz grows the canvas to fit.

### What was measured

Rectangles are fine. **Text is not.** A `T` operation inside a `_background`
segfaults Graphviz 2.43.0 — the version Debian 12 and Ubuntu 22.04/24.04 ship —
under both `dot` and `neato -n2`:

```console
$ printf 'graph g { graph [_background="c 7 -#000000 T 16 194 -1 33 5 -hosts"]; x -- y; }' | dot -Tsvg
Segmentation fault (core dumped)
```

Worse than a plain failure, it is *conditional*: the same document with a node
carrying an HTML-like label renders fine, because something else has established
a font by the time the background is drawn. So it is a landmine rather than a
limitation — a diagram would render for months and then crash when a device was
deleted. It was found by the first golden that put a caption in a background,
which is exactly what a golden is for.

Every variant was tried: all three justifications, with and without a preceding
colour operation, with and without an `F` font operation, integer and float
coordinates. All segfault. Only the polygon operations survive.

### What netviz does instead

`_background` carries the rectangles alone. Each caption is emitted as an
ordinary `shape=plaintext` node with a `pos`, which every engine handles, which
cannot crash, and which has the side benefit of being inside the drawing's
bounding box without any special pleading.

The caption is centred **above** its frame rather than inside it at the left,
where `labeljust=l` puts a real cluster's label, and both halves of that are
forced rather than chosen:

* *Centred*, because netviz does not measure text. An estimated left edge would
  be visibly wrong for a short name or a long one; a centred caption is exactly
  where it says it is.
* *Above*, because a node placed inside the frame touches whatever the
  arrangement put near the top of it — and `neato` responds to two touching nodes
  by abandoning spline routing **for the whole graph**, which was measured on the
  `arranged` fixture: every edge in the diagram went straight. Eleven points of
  vertical space is a poor price for that.

### If this is revisited

Check whether the crash survives in Graphviz 9.x before reaching for `T` again;
if it does not, the constraint is a packaging question rather than a design one,
and the caption could move inside the frame *only* if the touching-nodes problem
is solved too — a zero-sized caption node avoids the touch but earns a "size too
small for label" warning per frame, which is not an improvement.

---

## 18. ~~The editor polls, and a second tab is a race~~ — fixed, 9.3× an edit

**Status:** closed 2026-08-14. `netviz.web.events` is the push channel,
`GET /api/events` serves it, `netviz.web.presence` is who else is connected.

`session.js` polled `/api/state` once a second and, whenever the revision moved,
refetched the whole file list and re-rendered the diagram from scratch. Three
costs, and the third is the one that hurt on a real inventory:

* half a second of latency, on average, before an edit made anywhere showed up;
* a walk of every file in the tree — read, hash, serialise — to learn what *one*
  of them now hashes to;
* a full Graphviz run on every revision, including the many that do not change
  the drawing at all. A description, an owner label, a comment, a device added to
  a namespace the current view filters out: the picture is byte-for-byte what it
  was, and it was laid out again anyway.

And with two tabs open, none of it was announced: each found out about the
other's writes a second late and only as "the number moved".

### The harness

`tools/bench_events.py` (new), on `tools/bench_pipeline.py`'s default tree —
**1056 devices in 2106 documents across 138 files, 1.2 MB of YAML** — median of
five rounds, Graphviz 2.43. It measures the round trip an edit sits inside rather
than the reload inside it, which is `tools/bench_incremental.py`'s job.

### Measured

| One edit | Polling | Push | |
|---|---|---|---|
| notice the change | 500 ms | 0.4 ms | mean wait; the interval's arithmetic against a queue hand-off |
| fetch the file list | 107 ms | 95 ms | 138 rows → 1 |
| … and not re-grade the tree | 107 ms | 3.3 ms | the applied change already carried the diagnostics |
| draw it, picture moved | 1121 ms | 1121 ms | nothing to skip; this is the floor |
| draw it, picture unmoved | 1121 ms | 182 ms | **6.2×** — the layout is skipped, everything before it is not |
| **total, drawn layer untouched** | **1728 ms** | **185 ms** | **9.3×** |
| **total, drawn layer changed** | **1728 ms** | **1125 ms** | **1.5×** |

Two of those rows are worth reading twice.

**The file list's win is almost entirely the diagnostics.** Answering for one
file instead of 138 saves 12 ms; *not* re-validating the tree to grade it saves
92 more. So `/api/tree?path=` carries `diagnostics=0`, and the client passes it
exactly when it already has this revision's findings — which, after its own
write, it does, because the change response carries them. A partial fetch that
re-graded the tree would have been a rounding error.

**The skipped layout still costs 182 ms.** The fingerprint is the DOT document,
so producing it means loading, validating and building the graph; only Graphviz
is skipped. That is 84 % of the render and it is the part that grows worst with
the graph, but the remaining 182 ms is the same "nothing after the load is
incremental" wall entry 14 left standing, and this entry does not move it.

### The design, and what it refuses to do

**The stream is an optimisation, never a channel of authority.** Every fact it
carries is answerable by a plain `GET`; `?since=` on `/api/state` replays the
same events, with the same ids, out of the same ring buffer, into the same
client-side handlers. So the fallback is not a lesser code path — a page that
lost the stream behaves identically a fraction of a second later, and the
`curl`-and-plain-`GET` clients (every test that predates this) never learn the
stream exists. Nothing is writable through it and no write is gated on it.

**A client that fell behind is told, not patched.** Ids are monotonic and
replayed from a bounded ring; a `Last-Event-ID` that has fallen out of it opens
with `resync` rather than a plausible-looking partial replay, and a subscriber
whose own queue overflows is resynchronised rather than buffered. A patch applied
to a state the client cannot have is worse than a refetch, every time.

**Presence blocks nothing.** It expires on a timer, and a timer is a bad thing to
hold a lock on: an inventory that can be locked by closing a laptop lid is worse
than one where two people can collide. The revision precondition in `apply` and
the content hash in `write_file` remain the only gates, and the concurrency tests
in `tests/test_web_events.py` are written against those, not against the badge.

### What is deliberately not done

* **No WebSocket.** SSE is one direction, which is the direction the data goes;
  it reconnects and resumes by itself in every browser that matters; and it is
  ~120 lines on `http.server` against a framing implementation and a handshake.
  The client's writes are ordinary requests and are better for it — they get
  status codes.
* **No server-side SVG cache.** The fingerprint says "you already have this", not
  "here it is again": the client keeps the drawing, per view, and the server
  keeps nothing. Caching SVGs for a hundred (view, revision) pairs to save a
  round trip on a loopback socket is memory spent on the wrong problem.
* **No operational transform, no merge.** Two clients editing one file still
  produces a `409` for the second, with what is really on disk attached. Merging
  YAML that two people edited is a research project; telling them the truth is
  not.

---

## 19. ~~Orthogonal routes go through nodes, not around them~~ — fixed, and every crossing is gone

**Status:** closed 2026-08-15. `netviz.layout.avoid` is the router,
`tools/route_crossings.py` the measurement, `tests/fixtures/obstructed` the
reproduction, `tests/test_avoid.py` the guard.

`spec.routing: orthogonal` broke each leg of a link into horizontal and vertical
runs and avoided nothing: a Z route between two devices with a third sitting
between them drew a line straight across the third one's box. On a hand-arranged
diagram that is the most visible thing wrong with the picture, and it is now
gone.

### The reproduction, and the number

The original entry had no number, which is why it survived four releases: a
defect measured only by looking at a picture cannot be shown to be fixed.
`tools/route_crossings.py` counts **(link, box) pairs** — one drawn polyline
passing through the rectangle of a node it is not an endpoint of — for any
inventory, with and without avoidance, and prints the wall clock beside it.

`tests/fixtures/obstructed` (new) is eight devices arranged on purpose so that
every kind of crossing happens: a switch exactly between two others, a wide box
across a corridor, three parallel cables that have to get past one obstacle
together, one link with a bend somebody placed, and two links with nothing in
their way at all.

| | Crossings | Links cut | Re-routed | Median ms |
|---|---|---|---|---|
| `tests/fixtures/obstructed`, `--no-avoid` | **5** | 5 | 0 | 0.1 |
| `tests/fixtures/obstructed`, `--avoid` | **0** | 0 | 3 | 3.0 |
| `tests/fixtures/routed`, `--no-avoid` | **3** | 3 | 0 | 0.1 |
| `tests/fixtures/routed`, `--avoid` | **0** | 0 | 3 | 4.0 |

`tests/fixtures/routed` is the worked example in
[`docs/rendering.md`](rendering.md#a-worked-example-an-orthogonal-waypointed-diagram)
and has been committed since task 87. Nobody had noticed that three of its seven
cables were drawn across devices, which is the entry's own point about
unmeasured defects made twice over.

### What was built

**`netviz/layout/avoid.py`.** Every placed node is inflated by a clearance
into an obstacle; so is a free-standing `kind: area` and a placed `kind: note`
(an area that names *members* is not — it is a zone drawn behind the devices it
encloses, and treating it as solid would make every cable terminating inside it
unroutable). Their edges and centre-lines are the *Hanan grid*, and an A\* over
`(x index, y index, arrival axis)` finds the cheapest path. Cost is length, plus
a penalty per bend, per crossing of a line already drawn, and per already-occupied
channel. The arrival axis is in the state because without it the search finds the
shortest *staircase* rather than the shortest route.

**The output is a waypoint list** — the same list `SetLinkGeometry` stores and
the same list a person produces by dragging a bend — so `netviz.layout.routing`
is untouched. It still draws the line, locally, one leg at a time, and
`web/assets/links.js` still mirrors it exactly. The original entry's objection
that a global router would break the mirror is answered by not putting the router
in the mirrored layer: the canvas is *told* the waypoints rather than deriving
them, and `tests/test_browser.py`'s route-parity test is unchanged and still
passes.

**Three promises, and each is a test.** A bend somebody placed is never moved —
routing fills the legs *between* pinned points — and never dropped either, even
when the detour leaves it collinear with its neighbours and the simplifier would
have taken it out (that one was a real bug, found by the `routed` fixture). A
link that already keeps clear of everything is left byte-identical. And nothing
is written to anybody's files: a computed route is recomputed every render, is
published beside the authored bends as `layout.routed` in `-f json` and to the
editor canvas, and becomes permanent only when somebody presses `Shift-R`
(**Pin the computed route**), at which point it is an authored route like any
other.

**Bundles route as bundles.** Three cables between one pair of switches share one
searched route and are drawn as lanes beside it, one `FAN_GAP` apart, on
whichever side is clear — rather than three independent searches that fan out,
take different ways round the same box and re-converge. The side is chosen by
trying both and counting what each runs into, because the routed line is normally
hugging an obstacle at exactly the clearance and half a centred bundle would be
pushed back into it.

### The cost, on 784 devices

`tools/bench_editor.py --no-browser` measures the routing layer against a
generated tree, arranged by Graphviz first. 238 files, 1554 elements, 770 links:

| | Median ms | What happened |
|---|---|---|
| route every link, cold | **78** | 84 searches, 84 links moved, 1428 states popped |
| route every link, nothing moved | **28** | 0 searches, 770 routes reused |
| **re-route after one node moves** | **40** | **4 of 770 links searched**, 763 reused |

Four searches, not seven hundred and seventy. That is the whole point of the
`RouteCache`: a link is re-searched only when one of the three things it actually
depended on changed — an endpoint moved or was resized, a bend was added or
removed, or the line it was drawn as now crosses a box it did not before. Which
is exactly "somebody dragged a switch onto my cable", and nothing else.

Two things were measured and fixed on the way, both of which had made routing
cost `O(links × nodes)` rather than `O(links)`:

* `Router.blocked` — the question "does this link need routing at all?", asked
  once per link per render — scanned every obstacle. Through the spatial index
  instead: **67 ms → 17 ms** cold on a 216-link drawing, **68 ms → 7 ms** warm.
* The clean-link pass re-derived every untouched line on every render, which on
  that drawing was the entire cost of routing while the searches it exists to
  avoid were a twentieth of it. Cached routes are now handed back whole.

### Where the ceiling is

**Three cut-offs, all reported, none silent.** A window with more than
`Budget.max_cells` grid points is not searched; a search that pops more than
`Budget.max_expansions` states is abandoned; a link with no clear orthogonal
route at all (two devices drawn on top of each other, a corridor narrower than
the clearance) has none to find. Each falls back to the local Z or L — the
diagram is never worse than it was — and each produces a `Detour` naming the link
and the number that was hit. Neither fixture reaches any of them; a diagram that
does will say so rather than quietly stop avoiding things half way through.

**The remaining ceiling is the drag preview.** While a bend is being dragged the
canvas draws the *local* line, because that is what its mirror of
`netviz.layout.routing` computes and the router does not run in the browser.
On release the server answers with the avoided route and the line snaps to it.
For the gesture avoidance exists for — dragging a device, not a bend — there is
no preview to be wrong, since the whole drawing is refetched. Closing it properly
means either porting the search to JavaScript, which is the faithfulness problem
the original entry raised, or routing the one dragged link on the server inside
the drag, which is a round trip per pointer move. Neither is obviously right, so
neither was guessed at.

**Determinism has a small asterisk.** The congestion term depends on what was
routed *before*, and a route served from the cache was computed against a
slightly different drawing. The shape is the same and the crossing count is the
same; the exact channel a detour picks may not be. Every command-line render
starts cold, so nothing committed to a file is affected, and
`RouteCache.invalidate()` is the blunt instrument for a session that wants the
cold answer.

**Bundles are not re-lane-assigned incrementally.** If any member of a parallel
group has to move, the whole group is searched again. That is deliberate —
re-deriving one lane while its neighbours came out of the cache is how four
parallel cables stop being parallel — but it means a bundle is the coarsest unit
the cache has.

---

## 20. ~~The editor had never been opened on a large inventory~~ — fixed, and where the ceilings are

**Status:** closed 2026-08-14. `tools/bench_editor.py` is the harness,
`tests/test_editor_performance.py` the guard.

Every editor feature to date was built and tested against `examples/home-lab`:
five devices. `tools/bench_pipeline.py` has generated a **1056-device,
2106-document, 138-file** tree since entry 5, and nobody had ever pointed
`netviz web` at it.

### The harness

`tools/bench_editor.py` (new) starts the real `WebServer` over a real
`EditingSession` — the same objects `netviz web --write` builds, with the same
parse cache — points the Playwright Chromium from `tests/test_browser.py` at it,
and measures the interactions rather than the functions: navigation to first
paint, a `set` on one field timed to the moment the page has caught up, a write
made behind the session's back timed to the same, a fifty-node `set-geometry`,
a wheel gesture, a pan, and what the tab is holding while all of that happens.

A probe installed before the page's own scripts stamps every `fetch` and every
mutation of the canvas, so the timings are the page's own clock rather than the
round trip to it, and nothing about what the page does is changed by measuring it.

### Measured

| One thousand devices | Before | After | |
|---|---|---|---|
| cold open, to first paint | 1565 ms | 1345 ms | 4.77 MB → 3.80 MB over the wire |
| edit one field, picture unmoved | 1736 ms | 635 ms | **2.7×**; 1.62 MB → 0.16 MB |
| edit one field, picture moves | 2589 ms | 1751 ms | a rename; Graphviz is most of it |
| a write from outside, to the canvas | 816 ms | 605 ms | |
| move a 50-node selection | 2056 ms | 950 ms | **2.2×** |
| **redraw after dragging a node** | **58 152 ms** | **2 119 ms** | **27×** |
| DOM elements, zoomed in | 12 682 | 2 872 | culling |
| problem rows rebuilt per answer | 2 101 | 200 | |

### The four suspects, and what the profile said about each

**Whole-SVG replacement on every change — refuted.** `app.js` already sends the
fingerprint of the drawing it holds and, when the server agrees the picture has
not moved, does not touch the DOM at all. That is the common edit. When the
picture *has* moved the 2 MB SVG has to be replaced, and parsing it is about
200 ms of a 1.8 s cycle — not where the time was. Nothing was changed here.

**Re-running the pipeline per keystroke instead of the entry-14 cache — half
confirmed, and in the half nobody had looked at.** `netviz web` does pass the
cache, and a reload after one edit is 25 ms. But `EditSession` — the *write*
path — loads the tree three times per batch (the baseline, the tree between
operations, the tree the validation gate compares against) and passed the cache
to none of the overlaid loads. Its docstring explained why: "an overlaid file's
bytes are not the bytes on disk". True of the overlaid file, which `load_tree`
takes the overlay branch for anyway — and false of the other 137. That was 1.25 s
of parsing per edit, for nothing.

The validator was the same shape of mistake one level up: **one edit graded the
tree four times** — twice in the write path (which is a comparison, so two is
correct) and then once each for the file-list fetch and the diagram fetch, over
objects that had not moved between them. `EditingSession.findings` memoises it,
keyed by the *identity* of the inventory and of the settings rather than by a
revision number, so a reload or a config change invalidates it and nothing has to
remember to.

**The whole state payload on every event — confirmed, though not where the entry
predicted.** Events have carried deltas since entry 18. What did not was the
*diagnostics*: this tree reports 2 101 findings, 2 100 of them from one
informational rule, and every answer carried all of them — 538 kB on the ops
response, on the one-file tree fetch and on the diagram fetch alike, and 2 101
DOM rows rebuilt from each. Answers now carry the 200 most severe and say how
many they kept back; the page says so too.

**Every node in the DOM regardless of viewport — confirmed.** 12 682 SVG
elements, 93 191 DOM nodes in the tab. See the culling section below.

### The thing that was not on the list, and dominated everything

**Drag one node and every subsequent redraw took 58 seconds.** Not on the
suspect list because nobody had done it. A drawing with *some* positions stored
is `LayoutMode.PARTIAL`, and partial mode is two Graphviz runs: one to place the
nodes that have no position, and then `neato -n2` to draw the completed
arrangement. Two separate causes, each worth about half:

| Nodes | `dot`, nothing stored | 50 stored, before | after |
|---:|---:|---:|---:|
| 19 | 47 ms | 38 ms | 38 ms |
| 68 | 59 ms | 558 ms | 136 ms |
| 198 | 98 ms | 1 671 ms | 318 ms |
| 412 | 187 ms | 8 158 ms | 682 ms |
| 1056 | 675 ms | 58 152 ms | 2 119 ms |

*The probe run was routing edges it then discards.* The first run exists to read
node coordinates back; `complete_layout` uses `drawing.nodes` and nothing else.
It was nonetheless asking `neato` to route the edges, and `neato`'s spline router
on nodes it did not choose the positions of is superlinear: 52 seconds with
routing on, 0.54 s with it off, for identical positions and therefore an
identical final drawing. `to_dot` grew a `route_edges` flag and the two probe
runs pass `False`. Nothing a reader sees depends on it — the drawing that is
*shown* is the second run, which routes everything exactly as before.

*The overlap repair was quadratic.* Undoing Graphviz's scale reintroduces the
overlaps it removed, so `netviz.layout.graphviz.separate` pushes boxes apart —
and it compared every pair on every one of up to 24 passes. On this tree that is
thirteen million comparisons and 4.7 s. Two boxes can only overlap if their
centres are within one box of each other, so the nodes are now bucketed into a
grid of that size and each is tried against the nine cells it can reach. The
surviving pairs are tried **in the order they always were**, and the result is
checked against the old implementation over 300 random cases: identical, node for
node.

### Viewport culling and level of detail

`src/netviz/web/assets/cull.js` (new). Above 400 groups, every node and link
outside the viewport plus half a screen has its *contents* moved into a detached
fragment; the `<g>` stays, empty. Zoomed in, that is 140 of 2106 elements drawn
and 2 872 DOM nodes instead of 12 682.

The `<g>` stays because everything else on the page addresses an element by the
id of its group — the focus ring, remote selections, the info box, the link
overlay, the outline — and removing it would break all of them for exactly the
elements a person is most likely to be looking for. An empty group has no box, no
paint and no hit test; its dozen children are what cost something.

Culling needs to know where everything is, and `getBBox` cannot answer for an
element whose contents are parked. So every box is measured once, when the
drawing arrives, and **that index is then the answer for everybody** — including
`a11y.js`'s arrow navigation, which used to call `getBBox` once per candidate per
keypress, which on a 2106-element diagram was two thousand forced layouts per
arrow key. Off-screen elements stay navigable, findable and selectable; five
browser tests hold that.

Below 0.45 screen pixels per drawing unit the labels and the icons come off — at
that scale they cost a repaint each and say nothing — and each namespace grows a
dashed frame with its name and member count on it. The shapes stay: seeing where
things are is the reason to be zoomed out that far.

Two smaller things fell out of the same measurements. The client's view cache is
now bounded by bytes as well as by count — six drawings of a thousand devices was
twenty-five megabytes held for layers nobody had open. And the **zoom ceiling was
a constant and should not have been**: the SVG is sized to the canvas, so a
five-device diagram starts near life size and a thousand-device one at a
four-hundredth of it, and twelve times a four-hundredth is still illegible. It is
measured per drawing now, so a label can always be reached.

### The ceilings, said out loud

Where an interaction cannot be made fast, the page says so rather than appearing
to hang, and the number is here rather than only in somebody's memory.

| Ceiling | Measured | What the page does about it |
|---|---|---|
| first layout of 1056 nodes | 675 ms of Graphviz, ~1.3 s to first paint | the status line counts the seconds and says a large inventory is a real layout |
| a redraw that moves the picture | ~1.8 s | same |
| a redraw after a drag (partial arrangement) | ~2.1 s, 3× an unarranged one | same; `netviz layout --write` places the rest and takes it to 0.3 s |
| the drawing at 1× | the whole 2106 elements, unculled | it *is* all on screen; the level of detail is what applies here |
| the tab's heap | 8 MB at first paint, ~27 MB after switching layers | bounded by the byte cap on the view cache |
| problems reported | 200 per answer | "and N more, not listed here. Run `netviz validate` for all of them." |

The first three are Graphviz, and Graphviz is not ours. What is ours is not
pretending otherwise: a progress indicator that counts beats a frozen tab, and a
canvas that is drawing 140 of 2106 elements says so and says how to reach the
rest.

### What is deliberately not done

* **No server-side incremental SVG.** Patching the changed subtrees of a
  Graphviz drawing would mean owning the layout, because moving one node moves
  its neighbours. The fingerprint already makes the common edit cost no layout at
  all, and the honest way to make a *changed* picture cheap is a stored
  arrangement — which is `netviz layout --write`, and which takes the same
  redraw to 0.3 s.
* **No virtualised list for the file tree.** 138 rows is not a problem, and
  2106 documents are not rows: the tree lists files.
* **No collapsing of namespaces into single nodes at low zoom.** The frames are
  drawn over the shapes rather than instead of them. Hiding the shapes would cut
  a repaint that culling does not reach — at 1× the whole diagram is on screen —
  but it would also remove the only thing a zoomed-out diagram is *for*, which is
  seeing the shape of the network.

### The guard

`tests/test_editor_performance.py`, in the ordinary `pytest` run, and mostly by
counting rather than timing: files parsed per edit (1), validator runs per edit
(3), problems per answer (≤ 200), growth of the separation pass when the drawing
quadruples (≤ 6×), and one ratio — a partly-arranged layout against an
unarranged one (≤ 6×, measured 0.8× to 3.6×, was 86×). Every one prints its
figure whether or not it passed, and the CI job collects those lines into its
step summary.

---

## 21. ~~A rename leaves the geometry keyed by the old name~~ — fixed

**Status:** closed 2026-08-16. `netviz.edit.rename` is the plan,
`tests/test_rename.py` the guard.

Found while making a delete take its geometry with it (`netviz.edit.cascade`),
and it was the same defect one operation over. `netviz edit rename sw-a sw-b`
rewrote every *reference* to `sw-a` — a cable end, a tunnel's `over`, an
adapter's `attached_to` — and nothing else, so the **layout keys** that placed
it, a note's anchor and an area's member list were left naming a name that no
longer existed: a `W138`, possibly a `W142`, and an arrangement lost silently,
because the element was then drawn wherever the engine put it and `netviz
layout --prune` dropped the coordinates rather than moving them.

### What changed

`plan_rename(inventory, old=…, new=…)` returns what else a rename has to
rewrite, computed without touching anything, the way `plan_cascade` does for a
delete — and it reuses the two pieces the cascade had already built rather than
re-deciding either question: `placed_element` says which element a layout key
depends on, `annotation_references` walks a note's anchor and an area's members.
`_repoint` in `netviz.edit.apply` carries the plan out, so `edit move` gets it
too — a move that changes an element's namespace is a rename of its address.

The third part, which a delete never needed because a delete never has to *write*
a name, is the spelling rule, and it is `reference_text`'s: the shape the author
chose first, then the shapes that are still correct, and the fully-qualified name
last. So a layout in `sites/hq/` that wrote `sw-a` writes `sw-b`, one at the root
that wrote `sites/hq/sw-a` writes `sites/hq/sw-b`, and a short key is promoted
only when it stops resolving — which is what a rename across namespaces, or onto
a short name a second element already answers to, actually does to it.

A key is re-spelled in place rather than re-appended: `ruamel`'s `insert` puts it
back at the position it held, with the comment that was beside it, so renaming a
device arranged in three blocks of a hand-edited layout file is a three-line
diff. The derived ids §18 allows are carried too — `adp-usb-eth#upstream` and
`tunnel:sites/hq/vx-100` decorate an address at either end, and neither
decoration is part of the name.

`tests/fixtures/drawio/arranged-edited.plan.json`, the reproduction this entry
named, now carries `hosts/srv-web` at the coordinates `hosts/srv-app` had. That
took one more change than the rename itself: `netviz import drawio` orders the
geometry write *after* the renames and builds it from the arrangement the tree
held before the import, so it was putting the old key straight back into the file
the rename had just fixed — the same shape as the deleted-key bug fixed one entry
earlier, and fixed the same way, by teaching `_geometry_operations` what the
import is about to rename.

### The guard

`tests/test_rename.py`, and the invariant is the cascade's one operation over:
**a rename never leaves a finding behind, and everything the arrangement places
is still drawn exactly where it was.** Both are asserted for every element of
every arranged and annotated fixture in the repository. The second is the one
that matters, and the one "no new warning" would not catch on its own: it
compares the *resolved* geometry, so a key left spelled the old way simply stops
placing anything and shows up as a box that moved.

### Left out deliberately

An area's `selector`. It names a pattern, not an element, and netviz cannot
tell whether the pattern was meant to match the old name or merely happened to —
`namespace: sites/north` survives a rename inside that namespace, and a
`labels:` query may or may not. Rewriting one would be guessing, and §21 already
reports what a selector matches.

Group keys, for the opposite reason: a group key is a *namespace*, and renaming
an element never renames the folder it is in. Renaming a namespace is
`netviz edit move` over every element in it, and each of those moves carries
its own geometry.

Nothing was done about a layout key spelled short in a document where a short key
never resolved — `tunnel:vx-100` at the root of a tree whose tunnel is in
`sites/hq/`. It placed nothing before the rename and places nothing after it;
`W138` does not report it because a `:` makes the key a derived id it cannot
judge, and inventing a diagnostic for it belongs with `netviz layout --prune`,
which builds the drawing that could answer it.

---

## 22. ~~The as-built report says nothing about network namespaces~~ — fixed, and nothing else moved

**Status:** closed 2026-08-16. `netviz.report.pages._netns_section` is the
section, `tests/fixtures/report/containers-markdown.txt` the golden.

`netviz report` writes a page per device, and that page is where an operator
looks for "what is this machine". Since §23 a machine may run several network
stacks, and the report showed none of them: the interface table had a `VRF`
column and no `NETNS` one, and there was no section listing `spec.netns` the way
the routing section lists `spec.vrfs`.

Not fixed with §23 itself, deliberately. Every column in that table is drawn for
every device of every inventory, so a `NETNS` column is a column of dashes on
all 22 pages of `examples/campus`, and the report's golden fixtures under
`docs/example-report/` would move for a feature none of those devices uses.

### What changed

Both additions are **conditional**, which is what the entry was waiting for.

* **A `NETNS` column**, added to the interface table only when at least one
  interface *on that page* is in a stack other than the machine's initial one. It
  sits immediately before `VRF`, because the two compose rather than compete: a
  namespace is a whole second stack, a VRF partitions the routing table of one
  stack, and `netns: blue` with `vrf: red` names the `red` instance of the `blue`
  one.
* **A *Network namespaces* section**, drawn only on a device that declares
  `spec.netns` or a veth pair — the shape the wireless section already had. It
  carries the namespace tree (the initial namespace first, every declared one
  indented under the namespace it was created from, with the interfaces homed in
  each and the addresses they carry), the veth pairs **one row per end** so a pair
  is named from both sides, and — where a *declared* namespace holds them — that
  namespace's own static routes and policy rules.

The tree is indented with `└─` and not with spaces: a Markdown table cell
collapses runs of whitespace, so indentation made of spaces would have arrived at
the reader as a flat list.

Routes and rules are placed by the interface they name — `dev` for a route,
`iif`/`oif` for a rule — because that is the only thing in the document that says
which stack an entry is installed in. Only the ones a declared namespace holds are
repeated here; everything in the initial namespace stays the routing section's
alone, and the two sections link to each other rather than each stating the other's
half. A VRF is therefore described once, in *Routing*, and named here only to say
which stack it is an instance of.

`examples/containers` grew the routes and the policy rule that exercise it —
`srv-host-b` now declares the same prefix twice, once in its own stack and once in
the sandbox's, which is the thing two stacks *means* — and both branches are
committed: `srv-host-a` and `srv-host-b` have the section, `sw-lab` does not.
`docs/example-report/`, `tests/fixtures/report/home-lab-*.txt` and
`overlay-markdown.txt` are byte-identical to what they were before this, which is
the other half of the claim.

Namespaces remain visible everywhere they already were: `netviz show`,
`render -f json` (every port carries `netns` and `peer`, at every layer), the
tooltips and the detail panel of a rendering, `export interfaces`, and
`--layer netns`.

## 23. `netviz path` cannot trace out of a container, because layer 3 draws one node per machine

<!-- norun: the transcript is elided; the command exits 1, which is the point -->
```console
$ netviz -i examples/containers path 10.30.0.11 10.20.0.12
no path from hosts/srv-host-a to hosts/srv-host-b within 16 hops.
```

There is a path, and it is obvious: the container's address is in
`10.30.0.0/24`, the host's bridge is the gateway for that prefix, the host
forwards, and its uplink is in `10.20.0.0/24` with `srv-host-b`. Written between
two *different* machines — a router with a leg in each prefix — the same
topology traces in one hop.

The reason is not the trace. It is that the layer-3 graph puts **one node per
element**, so the container and the machine hosting it are the same node, and a
search that must pass *through* a node it started on finds a path of length zero
and rejects it. §23 did not introduce this — a host routing between two of its
own prefixes has always looked like this — but namespaces make it the ordinary
case rather than an oddity, because a container's address is always behind the
machine that runs it.

The fix is to give the layer-3 graph one node per *stack* rather than per
element, which is exactly what `--layer netns` already does for layer 1. It is
not a small change: an element that splits into several nodes at one layer and
not at others touches stored geometry (§18), the filters, what an annotation
encloses, and what the editor lets you select. That decision wants making on its
own rather than as a rider on the model.

Until then, `--layer netns` shows the topology the trace cannot walk, and a
trace between two addresses in the *same* prefix, or between two machines, is
unaffected.

## 24. Two containers on one host cannot both have an `eth0`

Raised while writing `examples/docker`, which is what
[§23](schema.md#23-network-namespaces-and-veth-pairs) looks like at the scale a
container runtime produces it: six namespaces on one machine, and every one of
them with an interface the runtime called `eth0`.

`NG-I001` says interface names are unique **within their device**, and every
interface of every namespace of a machine is on one device. So the document
cannot say what `ip link` says:

```yaml
    - name: eth0            # in c-web
      netns: c-web
    - name: eth0            # in c-api -- refused, NG-I001
      netns: c-api
```

That is a contradiction with §23's own premise. The section opens by saying a
machine running twelve containers has *twelve interface name spaces*, and then
the identity rule flattens them back into one. The example works around it by
naming each end `<container>-eth<n>` and saying so, which is readable and is not
what the machine would tell you.

Scoping the name to the pair `(netns, name)` is the correct model and is not a
small change, because that pair would have to become the interface's identity
**everywhere it is written down**: a cable endpoint (`srv:eth0` would name six
interfaces), a zone's `interfaces`, a bridge's `members`, `parent`, `peer`, a
route's `dev`, a policy rule's `iif`/`oif`, the node and port ids the renderers
emit, stored geometry (§18), every `edit` operation's target, the LSP's
definitions, `export interfaces`, `drift` and `plan`. Each of those is a place
where a reference resolves today by a single string, and a reference that could
match six interfaces needs a spelling that says which — `c-web/eth0`, most
likely, since the namespace tree already has that shape.

It also interacts with entry 23: the layer-3 graph draws one node per element,
and the same change is what would let it draw one per stack. Both want doing at
once, and neither wants doing as a rider on an example.

Until then: name a container's interfaces so they are unique on the machine.
Nothing else in the model is affected — `netns`, `peer`, the addresses and the
routes are all placed correctly, and `--layer netns` draws the right picture.

## 25. A private prefix behind a masquerade is not globally unique, and the inventory says it is

The other thing `examples/docker` could not write down. Two Docker hosts with
default settings both have a `docker0` at `172.17.0.1/16`, both hand out
`172.17.0.2` to their first container, and both are correct: the prefix is local
to the machine and every packet leaving it is masqueraded to the uplink address,
so nothing on the wire ever sees the collision.

netviz derives subnets from addresses across the *whole* inventory (§17). Two
hosts holding `172.17.0.1` are therefore one subnet with two claimants, which is
[`W106`](validation-rules.md#w106--one-address-claimed-twice-in-a-subnet) —
a real finding about a real duplicate address, reported about a design that is
deliberate and universal. The same applies to a swarm overlay's distributed
gateway, where *every* node's `br0` carries the network's `.1` on purpose.

The example sidesteps it: `srv-dock-01` and `srv-dock-02` are given different
pools, and the overlay bridges different addresses. That is a lie of omission
about how Docker allocates, and the example says so in both places rather than
letting a reader copy the shape and wonder why their own inventory reports
clashes.

The fix is a scope for an address, the way §16.1 gave routes one with a VRF and
§23.1 gave interfaces one with a namespace. The natural spelling is a per-network
flag that says *this prefix is translated at the machine boundary*, either on the
bridge that holds the gateway (`nat: masquerade`, which the firewall block
already knows) or as a property of the derived subnet. Then W106, W105 and W111
would compare addresses within a scope rather than across the inventory, and the
comparison would be the one an operator actually makes.

Deriving it from the firewall block alone is tempting and wrong: a masquerade
rule is evidence, not a declaration, and an inventory that stopped reporting
duplicate addresses the moment somebody wrote a NAT rule would have made the
finding depend on an unrelated field.

## 26. An interface that can never terminate a cable, and only a veth end can say so

The third thing `examples/docker` could not write down, raised while adding
`srv-dock-03`: a macvlan slave, an ipvlan slave and a slirp4netns tap.

`I002` says an enabled interface terminates no cable, and it is an *info*
because the two readings are equally likely — a spare port, or a cable document
nobody wrote. It exempts one kind of interface, and the reason is in the code:

> A veth end is `ethernet` and can never be cabled (`E049`), so the finding
> would be true of every one of them and actionable on none: the "spare port"
> reading does not apply to an interface that has no socket in the first place.

Exactly that argument applies to the other three. A macvlan or ipvlan slave is
created *on a parent* — `ip link add link eno1.22 type macvlan` — and moved into
a namespace; a tap is created by a userspace process holding the other side as a
file descriptor. None of them is a port, none of them will ever have a wire in
it, and `enabled: false` would be a lie about a link that is up.

The exemption cannot be widened to "any interface in a namespace". A physical
NIC moved into a container with `ip link set eth1 netns blue` is still a port
with a socket, and "you moved a NIC into a container and never wrote the cable"
is a finding worth keeping. What distinguishes the three is what created them,
and the schema has no word for that: `type` is one of seven values and none of
them is `macvlan`, `ipvlan` or `tap`, while `peer` is the only field that says
"this interface was made as half of something else".

So the example annotates the device with `netviz/ignore: NG-C015` and a
comment naming all four interfaces, and `tests.yaml` names them a second time in
the query that asserts every other interface in a namespace is one end of a
veth pair. Two lists of the same four names, kept in step by hand, is the cost.

The fix is a type — `macvlan` and `ipvlan` joining the enum, with `parent`
required on both (they are stacked interfaces, exactly as a `vlan` is, and
`_PARENT_TYPES` already exists for that) and `is_cableable` false. A `tap` is a
third, with no parent at all. Each then falls out of `I002`, out of `E012`, and
into `--layer netns` as its own edge to the parent it is a slave of — which is
the line the diagram is missing today, because a macvlan container currently
hangs off its machine by a nesting edge and nothing else, and the interface it
is really attached to is drawn nowhere. It interacts with nothing in entry 24's
list, since no reference to these interfaces resolves by anything but the name
they already have.

## 27. A diff drops the namespace boxes, because it never collapses anything

Found while fixing the style inspector, which had the same shape of bug on the
same route and is now fixed: `/api/diff` published no resolved styles, so the
panel emptied itself for as long as the changes drawer or the history scrubber
was open. It publishes no `containers` either, and that half is still open —
with `group_by_namespace` on, `/api/graph?view=l1` reports four container frames
for `examples/home-lab` and `/api/diff` of the same view reports none. The
editor's whole container layer is drawn off that payload, so opening the drawer
takes the boxes off the page, and with them the drop targets, the headers and
the fold triangles.

It is not the one line the styles fix was. `render_diff` builds its two graphs
and draws the overlay over their union; it never calls `collapse_targets` or
`collapse_namespaces`, so a diff also silently ignores whatever the user has
folded. Publishing containers without that would describe frames for namespaces
the diff has not folded and the drawing beside it has, which is worse than
publishing none. The fix is therefore to fold in `render_diff` the way
`render_inventory` folds — after the filters, before the fingerprint, keeping
the unfolded graph for the container payload — which also means deciding what a
folded namespace's aggregate node should be marked as when its members changed
in different directions. Probably "changed" whenever any member is anything but
untouched, but that is a claim about a shape nobody has drawn yet, and it should
be drawn before it is asserted.

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
  or `exec` in `src/`. The one subprocess is Graphviz: `netviz.render.dot`
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
  dependencies. Both the `netviz` console script and `python -m netviz` work
  from outside the source tree — version, `validate`, `render -f dot`,
  `render -f svg -o`, and `watch`. Exit codes are 1 for a rejected inventory and
  0 for a clean one; `watch` stops cleanly on SIGINT (0) and SIGTERM (143). The
  sdist carries `src`, `tests`, `examples`, `docs` and `tools`, so the shipped
  test suite can actually run.
