# `netviz impact`

`netviz impact` answers the question an inventory exists to answer and a
diagram cannot: **what breaks if this dies.** It removes elements from the
resolved graph, derives every layer again without them, and reports what stops
being reachable — or, given nothing to remove, enumerates the single points of
failure that were there all along, ranked by how much each one would cost.

Nothing is pinged and no device is contacted. The answer is what your
documentation says would happen, which is exactly the thing to compare against
what does.

---

## Contents

- [Synopsis](#synopsis)
- [Naming what fails](#naming-what-fails)
- [Where reachability is measured from](#where-reachability-is-measured-from)
- [`--fail`: the blast radius](#--fail-the-blast-radius)
- [Power failures cascade](#power-failures-cascade)
- [`--path`: assertions that survive, or do not](#--path-assertions-that-survive-or-do-not)
- [`--spof`: single points of failure](#--spof-single-points-of-failure)
- [`--redundancy`: promises the files make](#--redundancy-promises-the-files-make)
- [Failure mode in the editor](#failure-mode-in-the-editor)
- [Output for a program](#output-for-a-program)
- [What this does not model](#what-this-does-not-model)
- [Performance](#performance)
- [Options](#options)
- [Exit codes](#exit-codes)

---

## Synopsis

<!-- generated: synopsis impact -->
```text
netviz [GLOBAL OPTIONS] impact [OPTIONS]
```
<!-- /generated -->

## Naming what fails

`--fail` takes four spellings, tried in this order so no later one can shadow an
earlier:

- a **fully-qualified name** — `sites/north/access/sw-north-acc-01` — which is
  unambiguous by construction;
- a **kind-qualified name** — `device/sw1`, `cable/rack1-a3`, `pdu/pdu-r1-a` —
  which is how a change ticket names a thing, and which tells a switch and a
  cable that share a short name apart. `device` covers the five device kinds and
  `link` covers cables and tunnels; every other kind is itself;
- a **relative reference**, resolved the way every reference in the inventory is;
- a **short name**, when exactly one element in the tree carries it.

A name that matches nothing, or several things, is a usage error that lists what
it could have meant instead:

<!-- norun: the wrapped error message is illustrative; no example inventory has three sw-01 -->
```console
$ netviz impact --fail sw-01
Error: Invalid value for '--fail': 'sw-01' is ambiguous: it matches 3 elements.
Qualify it with a namespace or a kind, e.g. 'device/sw-01'.
```

`--fail` is repeatable, and several failures are simulated **together** — a
maintenance window that takes out a switch and re-patches a rack is one event,
and analysing the two separately would miss exactly the case where each is
survivable and the pair is not.

## Where reachability is measured from

"Unreachable" needs a *from*. By default that is the **designated gateways**: the
elements holding an address that some other interface names as its `gateway`.
That is the one definition the files already carry, and it is the right one — a
router nobody points a default route at is not what anybody loses service
through.

Failing that, in order: the elements of kind `router`, with the report saying so;
and if the inventory has neither, reachability is not measured at all and only
the partitions are reported. `--from` overrides all of it and takes the same
spellings as `--fail`.

## `--fail`: the blast radius

<!-- run: cwd=examples/campus rc=1 -->
```console
$ netviz impact --fail device/sw-north-acc-01 --layer l1
1 element removed:
  sw-north-acc-01 (switch)

reachable from 1 gateway: sw-north-dist-01

l1 (physical): 19 of 22 elements still reachable
  2 elements isolated:
    sites/north/hosts/pc-north-01
    sites/north/hosts/srv-north-01
  sites/north/hosts is now in 3 pieces, was 1:
    1. pc-north-01
    2. srv-north-01
    3. pc-north-02

summary: 2 isolated at worst
```

Three things are reported per layer, and each answers a different question.

**Isolated** is the direct answer: elements that could reach a gateway and now
cannot. A failed element is never in this list — it is gone, not stranded — and
neither is anything that was already unreachable before, which is counted
separately so a pre-existing island is not mistaken for new damage.

**Partitioned namespaces** answer the question the count does not. "The
inventory is in four pieces" is not actionable; "`sites/north` is in two" is,
because a namespace is a site, a floor or a rack, and a site split in half is an
outage with an address.

**The layers disagree on purpose.** Layer 1 is the plant, layer 2 the broadcast
domains, layer 3 the routed adjacency, and a failure that halves a VLAN without
partitioning the cabling is a real and different thing from one that cuts a
cable. The layers are derived again from the *pruned inventory* rather than
patched — a broadcast domain is found by walking the links that carry a VLAN, so
deleting a cable from a finished layer-2 graph would leave the domains it split
none the wiser.

## Power failures cascade

`src/netviz/power.py` already resolves every feed, so a PDU is not a special
case in the topology — it is a source, and everything whose *only* remaining
source is gone goes with it, transitively:

<!-- norun: the inventory is the two-PDU rack of the section, which is not an example tree -->
```console
$ netviz impact --fail pdu/pdu-a --from rtr-core
1 element removed:
  pdu-a (pdu)

2 elements lost power as a consequence:
  sw-poe (switch, lost pdu-a)
  ap-1 (computer, lost sw-poe)
```

The cause named is the source that actually went dark — the switch above the
access point, not the PDU two steps up — because an operator restoring service
works up the chain from what they can see.

A device with two cords into two PDUs survives either. A device with two cords
into *one* survives neither, and that is what `E042` and `--spof` are for.

## `--path`: assertions that survive, or do not

`--path SRC=DST` re-runs the [trace engine](path.md) on both sides of the failure
and reports whether the route survived. Each end takes the spellings `netviz
path` takes; `=` separates them, because a colon already means
`element:interface` and an arrow has to be quoted in every shell there is.

<!-- norun: the note is wrapped to the page width; the command prints it on one line -->
```console
$ netviz impact --fail sw-north-acc-01 --path pc-north-01=srv-north-01
paths:
  broken    pc-north-01 → srv-north-01  (1 route the trace still finds crosses a
            prefix or a tunnel whose two ends are no longer physically connected,
            so it is not counted)
```

That note is the one correction this command makes to the trace engine, and it
is worth understanding. The layer-3 search calls two elements adjacent when they
hold addresses in one prefix — the right definition for a *route*, and not a
sufficient one for a *frame*. Unplug the switch between two hosts in
`10.1.10.0/24` and they are still in that prefix. So every route is checked twice
over: once by the trace engine, once against the physical graph, and a route
whose hops are not physically realisable is not counted as surviving. Reporting
one as intact is the most dangerous thing this command could get wrong.

A status is one of `unchanged`, `degraded` (fewer routes than before, at least
one left), `broken` (none left) or `missing` (there was no route even before, so
this says nothing about the failure).

## `--spof`: single points of failure

With no `--fail`, `impact` enumerates instead of simulating. Every articulation
point and every bridge of every requested layer, ranked by how many endpoints
each one isolates:

<!-- run: cwd=examples/campus -->
```console
$ netviz impact --spof --limit 5
reachable from 1 gateway: sw-north-dist-01

38 single points of failure, worst 5 shown:
  isolates  layer  what
        21  l1     sw-north-dist-01 (switch, articulation point)
        21  l3     sw-north-dist-01 (switch, articulation point)
        15  l1     cbl-north-core-dist (cable, bridge)
        14  l1     rtr-north-core-01 (router, articulation point)
        14  l3     rtr-north-core-01 (router, articulation point)
```

Four things about that list.

**One of a redundant pair is not a bridge.** Two cables in a LAG join the same
pair of switches; cutting one leaves the other, and neither is reported.

**An anchor can be a single point of failure without being an articulation
point.** Lose the only gateway and everything downstream is cut off from the
network even though the graph never split. The ranking is therefore computed for
every element rather than only for the cut vertices — `sole anchor` is what the
report calls this case.

**A VLAN is not a thing you can unplug.** Layer 2 and layer 3 have nodes standing
for broadcast domains and for IP prefixes, and both are cut vertices of a real
graph. Neither is reported: an item on a maintenance plan that no engineer can
act on is worse than no item.

**Power is swept separately.** A device whose cabling is textbook redundant and
whose two supplies are both in one PDU has a single point of failure that no
amount of graph theory over the cables will find. A PDU feeding a switch that
sources PoE for six access points is reported as isolating all seven.

`--limit` is the cutoff for large inventories — a thousand-device tree has more
than a thousand single points of failure, and answering a question with a
thousand rows is not answering it. The report always says how many there were in
total, so a truncated list never reads as the whole answer. `--min-isolated`
raises the floor: `--min-isolated 5` asks only about the failures that would cost
five endpoints or more.

## `--redundancy`: promises the files make

An inventory records what the network *is*. An annotation records what it is
*for*:

```yaml
metadata:
  name: sw-ward-01
  annotations:
    netviz/redundancy: "gateway, power"
```

`gateway` says losing any one element must not cut this one off from its default
gateway. `power` says losing any one power source must not switch it off. Both
are design intent, both are the kind of intent that quietly stops being true when
somebody re-patches a rack, and both are graded by ordinary validation rules —
[`E047`](../validation-rules.md#e047--declared-gateway-redundancy-is-not-met),
[`E048`](../validation-rules.md#e048--declared-power-redundancy-is-not-met) and
[`W141`](../validation-rules.md#w141--unknown-redundancy-expectation).

Ordinary means `netviz validate` gates on them too, which is the point: CI
fails the change that removed somebody's second path, and `netviz impact
--redundancy` is where you go to find out why.

<!-- norun: the finding is wrapped to the page width; the command prints it on one line -->
```console
$ netviz impact --redundancy
1 redundancy expectation not met:
  E047 element 'sites/north/hosts/pc-north-01' declares a 'gateway' redundancy
  expectation, but 3 single failures would cut it off from its gateway 10.1.10.1
  on 'sites/north/distribution/sw-north-dist-01': sites/north/access/sw-north-acc-01,
  cable sites/north/cables/cbl-north-dist-acc01, cable
  sites/north/cables/cbl-north-acc01-pc01. Add a second path, or drop the expectation.
```

`-F sarif` and `-F github` are accepted **with `--redundancy` only**, and carry
the findings alone. Both describe problems in files, and "losing sw-core-01
strands 43 hosts" is not a problem in a file — it is a property of a network that
no line of YAML is guilty of. See [`docs/ci.md`](../ci.md) for the upload.

## Failure mode in the editor

`netviz web DIR` has the same analysis on the canvas. **Alt-F** puts the
session in failure mode; clicking an element greys out everything its loss would
isolate and the status line names the count. Escape or Alt-F again puts the
diagram back.

It is read-only from end to end. The route behind it builds a throwaway inventory
and writes nothing — no file, no revision, no undo entry — which is why leaving
the mode is instant and why it is available in a read-only session.

## Output for a program

`-F json` is the whole analysis: every layer, every isolated element by name,
every partition with its fragments, every checked path, and the ranked single
points of failure with `total` and `reported` beside them so a truncated list is
never mistaken for a complete one.

```json
{
  "schemaVersion": 1,
  "tool": { "name": "netviz", "version": "0.1.0" },
  "modes": ["fail"],
  "anchors": { "elements": ["sites/north/distribution/sw-north-dist-01"], "source": "gateways" },
  "failed": [{ "element": "sites/north/access/sw-north-acc-01", "kind": "switch",
               "cause": "requested", "spec": "device/sw-north-acc-01" }],
  "layers": [{ "layer": "l1", "served": { "before": 22, "after": 19 },
               "isolated": ["sites/north/hosts/pc-north-01"], "partitioned": [] }],
  "summary": { "isolated": 2, "brokenPaths": 0, "violations": 0, "impacted": true }
}
```

Keys are only ever added, never renamed; `schemaVersion` is bumped only for a
change that could break a consumer. Ordering is fixed everywhere: two runs over
an unchanged tree produce byte-identical output, so a report can be committed,
diffed and reviewed.

## What this does not model

Being explicit about this is the difference between a tool that is trusted and
one that is believed.

- **Capacity.** A surviving path is reported as surviving whether it carries the
  load or melts. Nothing here knows what a link is carrying.
- **Protocol convergence.** A path that exists after the failure is reported as
  existing. Whether STP, OSPF or BGP finds it, and how long that takes, is not
  modelled — the inventory records adjacency, not timers.
- **Correlated failure.** Two PDUs on one building supply are caught, because
  `input_feed` records the supply. Two switches in one rack that a fire would
  take together are not: the model has the rack, and inferring intent from
  co-location would be guessing.
- **Partial failure.** An element is up or gone. A switch with a failed line card
  or a link running at a tenth of its rate is neither, and there is nowhere in
  the schema to say so.

## Performance

Measured by `tools/bench_impact.py` on the 1056-device tree
`tools/bench_pipeline.py` generates — 2106 documents in 138 files, 1.2 MB of
YAML. A generated tree is the *worst* case for `--spof`: a tree has no redundancy
at all, so every internal node is an articulation point and every cable is a
bridge.

| Stage | Time | What it is |
|---|---|---|
| `load_tree` | 493 ms | Parsing the tree. The floor under every command. |
| derive anchors | 1.3 ms | One scan of every interface for a declared `gateway`. |
| build views (l1, l2, l3) | 247 ms | One resolution pass plus the broadcast-domain walk. |
| `--fail` (one element) | 606 ms | The whole simulation: both sets of views, and the prune between them. |
| `--spof` (three layers + power) | 300 ms | 1104 single points of failure found and ranked; the worst 25 named. |
| ↳ `analyse()` on layer 1 alone | 5.7 ms | Every articulation point, every bridge and every isolation count, in one pass. |
| `--redundancy` | 112 ms | The validation pass, with an expectation on all 42 rack switches. |

Median of five runs, Python 3.12, libyaml. Two of those numbers are the design.

`analyse()` at **5.7 ms for 1056 nodes** is what makes `--spof` usable at all.
The obvious implementation — remove each candidate, re-traverse — is O(V·(V+E)),
which on this tree is a few million node visits per layer and takes minutes.
[`netviz.connectivity`](../../src/netviz/connectivity.py) gets every
articulation point, every bridge *and* every isolation count out of one
depth-first search, from the subtree sizes and the number of anchors each subtree
contains. The identities of the isolated endpoints are materialised only for the
entries the report prints, which is the other thing `--limit` is for.

`--fail` at **2.5× the cost of building the views once** is the price of
exactness, and it is deliberate: the second pass re-derives the layers from the
pruned inventory rather than patching the first, which is the only way the
broadcast domains and the routed adjacency come out right. It is affordable
because a `--fail` run does it twice in total rather than once per candidate.

## Options

<!-- generated: options impact -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--fail` | `ELEMENT` | — | Remove this element and report what breaks. Takes a name, a fully-qualified name, or a kind-qualified one such as 'device/sw1' or 'cable/rack1-a3'. Repeatable: several failures are simulated together. |
| `--from` | `ELEMENT` | — | Measure reachability from here instead of from the designated gateways. Repeatable. |
| `--path` | `SRC=DST` | — | Re-run this trace on both sides of the failure and report whether it survived. Repeatable. Each end takes the spellings 'netviz path' takes. |
| `--spof` | — | off | Enumerate the single points of failure instead of simulating one, ranked by how many endpoints each isolates. The default when no --fail is given. |
| `--redundancy` | — | off | Check the redundancy expectations elements declare in their 'netviz/redundancy' annotation, and report E047, E048 and W141 through the ordinary rule machinery. |
| `--layer` | `[l1\|l2\|l3]` | — | Which views to analyse. Repeatable; all three by default. |
| `--limit` | `N` | `25` | Report at most this many single points of failure. 0 reports every one of them. |
| `--min-isolated` | `N` | `1` | Ignore a single point of failure that isolates fewer endpoints than this. |
| `-F`, `--output-format` | `[text\|json\|sarif\|github]` | `text` | text is the report; json is the whole analysis for tooling. sarif and github carry the --redundancy findings alone, for a code-scanning upload. |
| `--fail-on` | `[impact\|none]` | `impact` | Exit 1 when something was isolated, a checked path broke or a declared expectation was not met, or never. Enumerating single points of failure never fails the run. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Proceed even when validation failed. The result may not match the files. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The analysis ran. Nothing was isolated, no checked path broke, and every declared expectation was met — or `--fail-on none` was given. |
| 1 | A simulated failure isolated something, a checked path broke, or an expectation was not met. Also: the inventory was rejected and `--force` was not given. |
| 2 | Usage error: `--fail`, `--from` or `--path` names nothing or names too much, or `-F sarif` was asked for without `--redundancy`. |
| 3 | The inventory could not be discovered or read at all. |
| 130 | Interrupted. |
| 141 | The downstream end of a pipe closed first. |

Enumerating single points of failure never fails the run. Every network of any
size has them, and a command that exited non-zero for saying so is a command
nobody could put in a pipeline.

## See also

* [`netviz path`](path.md) and [`docs/paths.md`](../paths.md) — the trace
  engine `--path` re-runs, and what it does and does not decide.
* [`netviz validate`](validate.md) and
  [`docs/validation-rules.md`](../validation-rules.md) — `E047`, `E048` and
  `W141`, and how to suppress or re-grade them.
* [`netviz list power`](list.md) — the feeds and budgets the power sweep reads.
* [`netviz web`](web.md) — the editor, and the failure-mode overlay.
* [`docs/ci.md`](../ci.md) — gating a pull request on the exit code.
