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

<!-- norun: the element name is illustrative and the second line is a comment, not a command -->
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
rather than as a distinction. A reader cannot tell "netgraph has no picture for
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
| `netgraph validate` | 0.95–0.96 s | 0.80–0.81 s | 2.90–2.91 s | 2.73–2.78 s |
| `netgraph render -f dot` | 0.96–0.97 s | 0.80–0.81 s | 2.91 s | 2.70–2.75 s |

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
| bytes per element per view | 543 | 543 | 806 | 848 | 1110 | 650 |
| …with `--icons cisco` | 428 | 893 | 690 | 732 | 1459 | 650 |

The middle columns are each change disabled on its own, so the table says which
row catches which revert: every one of the three is over a threshold in at least
one row, and a full revert is over four of the five. The headroom above today's
worst figure is 20 %, which is tighter than any of the timing guards in entries
5 and 7 would dare be and can afford to be — these are byte counts of a
deterministic renderer with no run-to-run spread at all. What can move them is a
Graphviz release that lays a diagram out differently; if one ever does, raise
the threshold there and record the new number here, do not delete the test.

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
changed — so `detail.js`, `netgraph web` and the `-f json` exporter are
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
diagram was where the size actually hurt: every filter netgraph had removed
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
`netgraph render -f json` now exports the element list behind every box, so a
consumer that wants one can compute it without netgraph guessing which one.

The other bound worth naming: `--collapse-depth` counts from the shallowest
namespace every element shares, which makes depth 1 mean "one node per site" in
the trees people actually have. In a tree with no shared root — several
top-level directories — depth 1 collapses each of them, which is the same rule
producing a different answer, not a special case.

---

## 10. netgraph now depends on a second YAML parser

**Status:** accepted 2026-07-29, deliberately. `ruamel.yaml` is a runtime
dependency, used by `netgraph fmt` and by nothing else.

**Why a second parser at all.** `netgraph fmt` has to preserve comments, blank
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
  turns "run `netgraph fmt`" into a support question.
- *Replacing PyYAML with ruamel everywhere.* Rejected on measurement. The
  loading path is the throughput bottleneck (entries 1 and 5) and is currently
  libyaml-backed; ruamel's round-trip parser is pure Python and much slower, and
  the strictness `netgraph.loader.documents` adds — duplicate-key rejection, the
  YAML 1.2 boolean rule — would all have to be rebuilt on a different API for a
  path that has no use for a single thing round-tripping buys.

**What the dependency is fenced with.**

- **Nothing on the loading path imports it.** `netgraph.fmt` is imported lazily,
  inside `fmt_command`, so `validate` and `render` do not pay its ~30 ms of
  import time — an eighth of what starting the CLI costs at all, and `validate`
  runs in a pre-commit hook. `netgraph.loader` is untouched by this work.
- **The two parsers are checked against each other on every format.**
  `netgraph.fmt.verify` re-reads every formatted file with the *strict* loader
  and compares it against what that loader read before. Nothing is written that
  the two disagree about, so a divergence is a refusal rather than a corruption.
- **The divergences are real, and the fence caught them.** Building this found
  two places where ruamel and PyYAML disagree about the same bytes: ruamel reads
  `1:02` as the string `"1:02"` where PyYAML reads the integer 62, and ruamel
  emits `::1/128` unquoted inside a flow sequence, which PyYAML then refuses to
  parse. Both were found by the verification pass failing on
  `examples/`, not by review. They are handled in
  `netgraph.fmt.scalars` — `is_untouchable` and `plain_survives` respectively —
  and both ask netgraph's own loader for the answer rather than assuming one.

**What would justify revisiting this.** A PyYAML release that can round-trip
comments, or a `fmt` that needs to run on the loading path — neither of which is
in sight. If ruamel became unmaintained, `netgraph.fmt.canonical` is the only
module that imports it, and `docs/format.md` is a specification precise enough
to reimplement against.

---

## 11. A YAML parser can still be crashed from outside netgraph's control

**Status:** bounded 2026-07-29, not eliminated. Raised by the loader fuzz target
added with the property tests (`tests/test_fuzz_loader.py`).

Fuzzing the loader found three ways a document could get past every diagnostic
netgraph writes and reach a failure netgraph does not own. All three are fixed;
what is *not* fixed is the underlying reason they were possible, which is that
the parser is a dependency and its limits are not netgraph's.

**What was found and fixed.**

- **An integer literal of more than 4300 digits.** CPython refuses to convert
  one (CVE-2020-10735), and PyYAML's constructors call `int()` on whatever the
  resolver matched — so `mtu: 999…9` came out of the *parser* as a bare
  `ValueError` about `sys.set_int_max_str_digits`, past a `try` that caught only
  `yaml.YAMLError`. `netgraph.loader.documents` now translates it, and the three
  places netgraph itself calls `int()` on document text — an interface range, a
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

**What remains.** The nesting guard is netgraph putting a fence in front of
somebody else's cliff. It costs a C-speed `str.count` on every document and a
full scan only for one carrying more than `MAX_NESTING_DEPTH` flow openers —
every example inventory in this repository has fewer than 110 in total — so the
price is right, but the guard exists because *libyaml crashes the process*
rather than because 1024 levels of nesting is a meaningful schema limit. A
document that nests 1025 deep is refused with an accurate diagnostic that
describes netgraph's limit and not the real one.

**What would justify revisiting this.** libyaml growing a depth limit of its
own, or PyYAML exposing one. Either would let the guard be dropped in favour of
translating whatever error the parser produced, which is what every other
malformed document already gets.

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
