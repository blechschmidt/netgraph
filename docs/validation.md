# Validating an inventory

`netgraph validate` answers one question: **is this inventory usable?** This page
is about working with the answer — how the three passes fit together, what a
finding's parts mean, how a severity becomes an exit code, and the four ways to
tell netgraph that your network is the exception.

[`docs/validation-rules.md`](validation-rules.md) is the other half: one section
per rule, why the rule is worth having, and how to switch that one off. Every
report netgraph emits deep-links into it, so you rarely have to find a rule
there by hand. [`netgraph validate`](commands/validate.md) is the flag-by-flag
reference for the command itself.

---

## Contents

- [The three passes](#the-three-passes)
- [How to read a finding](#how-to-read-a-finding)
- [Severities, and the exit code that follows](#severities-and-the-exit-code-that-follows)
- [`--strict`, and when you want it](#--strict-and-when-you-want-it)
- [Saying "not here": the four suppressions](#saying-not-here-the-four-suppressions)
- [Output for a machine](#output-for-a-machine)
- [Every rule](#every-rule)

---

## The three passes

Validation is not one check but three, run in a fixed order, each one depending
on the previous having succeeded.

**Pass 1 — discovery** walks the folder tree and decides what is part of the
inventory at all: which files are read, which paths are skipped, which documents
a multi-document file holds, and whether two of them answer to the same name.
Most of it is not configurable, because it is not a judgement — it is how the
loader works. It is documented as a pass anyway, because "my file is not being
loaded" is a validation question in practice.
[Pass 1 — discovery](validation-rules.md#pass-1--discovery) has the five loading
rules and the two problems this stage reports.

**Pass 2 — schema** parses each surviving document into the model for its `kind`.
Everything here is an error and none of it can be suppressed: a document that
does not parse is not in the graph, and no severity setting can make a missing
element benign. This is also where `interfaces[].range` is expanded and
`spec.from` is merged, which is why a mistake in a template is reported against
the template rather than fifty times against the devices that inherit it.
[Pass 2 — schema](validation-rules.md#pass-2--schema) lists the constraints by
area, and [`docs/schema-reference.md`](schema-reference.md) is the field-by-field
lookup table behind them.

**Pass 3 — semantics** asks the interesting question: the documents all parse,
but do they agree with *each other*? Cables must land on interfaces that exist,
addresses must be unique where uniqueness is physical, the two ends of a link
must be configured compatibly. These are the only rules that can be disabled,
re-graded or suppressed, because they are the only ones that are judgements —
a network can be built that way, badly, and an inventory that means to describe
it must be able to say so.
[Pass 3 — semantics](validation-rules.md#pass-3--semantics) writes each one up
individually.

### Why an earlier error hides later ones

A pass can only judge what reached it. A file discovery skipped is not in the
inventory, so nothing is ever said about its contents. A document that lost a
name collision, or that failed the schema, is not in the graph — so the pass-3
rules that would have had an opinion about it never see it, and the rules that
*do* fire are often about the hole it left rather than about the mistake itself.

The practical consequence is worth internalising: **fix errors from the top and
re-run.** A report can get *longer* after a fix, not shorter, and that is the
system working. The repository's own broken-fixture directory shows it at scale
— one file per rule, each reusing the names `pc-a` and `pc-b`, so loading the
whole directory as a single inventory makes almost every document lose a name
collision in pass 1:

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/invalid validate
...
  e002-double-termination.yaml#0:4             NG-N002  metadata.name: duplicate element name 'pc-a' (first declared at e001-unknown-endpoint.yaml#0:7); this document is ignored
...
  e005-vlan-mismatch.yaml#1:18           I002  interface 'sw-b:GigabitEthernet0/1' is enabled but terminates no cable; mark it 'enabled: false' if the port is spare
...
```

Not one of the pass-3 findings in that report is the finding the file it names
was written to demonstrate. `e005-vlan-mismatch.yaml` is about a VLAN mismatch;
what it reports here is a spare port, because the cable that would have created
the mismatch was in a document pass 1 dropped. Load the file on its own and the
rule it is about is the only thing it says — which is exactly why
[`tests/fixtures/invalid/README.md`](../tests/fixtures/invalid/README.md) insists
they be loaded one at a time.

Passes 1 and 2 are how an inventory is loaded, so *every* command that reads the
tree runs them and reports what they found. Pass 3 runs wherever a wrong answer
would matter. [`render`](commands/render.md), [`path`](commands/path.md),
[`watch`](commands/watch.md) and [`export`](export.md) validate before they
produce anything and refuse an inventory with errors unless `--force` is given;
each of them also takes `--strict`. [`import`](commands/import.md) validates the
tree it has just written, and says which of the findings an incomplete capture is
expected to trip. `validate` is simply the command whose *only* job is to report.

## How to read a finding

A text finding is three columns — location, rule, message:

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/invalid/e001-unknown-endpoint.yaml validate
errors (1):
  e001-unknown-endpoint.yaml#1:19  E001  cable 'cbl-dangling' endpoint pc-ghost:eth0: no element named 'pc-ghost' is declared in this inventory

1 error
```

**The location.** `e001-unknown-endpoint.yaml#1:19` is the file relative to the
inventory root, then the index of the document *within* that file — 0-based,
counting `---` separators — then a line number. In text output the line is where
the document begins, which is the anchor a person needs to find it. The
structured formats carry the line **and column of the offending value** instead,
because a code-scanning UI puts a squiggle under a character rather than
scrolling you to a document.

**The rule id.** `E001` is the id the validator uses everywhere a rule can be
named: `--disable`, `[validate]` in `netgraph.toml`, the `netgraph/ignore`
annotation. The letter is the severity the rule was *first assigned* — `E` error,
`W` warning, `I` info — and it does not change when an inventory re-grades the
rule, so `E004 = "warning"` is a perfectly ordinary line to write. Ids are
permanent: once assigned, an id is never reused for a different rule, so a
suppression written today keeps meaning what it meant.

**The `NG-*` alias.** Every rule also answers to the identifier
[§10 of the specification](schema.md#10-validation-rules) gives it — `E001` is
`NG-C002` and `NG-C003`. Both spellings work in every suppression mechanism. The
aliases exist so the published specification and the implementation cannot drift
apart, and the structured formats report both.

**The message names every element involved**, not just the one the finding is
anchored at, because any of them can suppress it. A finding about a cable names
the cable *and* both endpoints, and an annotation on any of the three silences
it.

**The help URI.** In `json`, `sarif` and `github` output each finding carries a
permanent link to its section of `docs/validation-rules.md`, built from the rule
id and title in the code — so a stranger reading a CI annotation can open the
write-up without knowing anything about netgraph's docs layout:

<!-- run: rc=1 -->
```console
$ netgraph -q -i tests/fixtures/invalid/e001-unknown-endpoint.yaml validate -F github
::error file=tests/fixtures/invalid/e001-unknown-endpoint.yaml,line=26,col=7,title=E001 unknown cable endpoint::cable 'cbl-dangling' endpoint pc-ghost:eth0: no element named 'pc-ghost' is declared in this inventory
```

Line 26, column 7 is `- pc-ghost:eth0` — the value that is wrong, not the
document that holds it. The `title` is the rule id plus the heading of its
section, and the same string is the SARIF `name`; the SARIF `helpUri` is the full
link. `-q` above is what dropped the human summary and left only the workflow
command; see [Output for a machine](#output-for-a-machine).

## Severities, and the exit code that follows

| Severity | Meaning | Effect |
|---|---|---|
| `error` | The inventory does not describe a network that could exist. | `validate` exits 1; `render` refuses to draw unless `--force`. |
| `warning` | Legal, but usually a mistake. | Reported; the run succeeds. |
| `info` | Worth knowing. | Reported; the run succeeds. |

Findings are grouped by severity, most severe first, and within a group ordered
by file, then position, then rule id. The order is deterministic, so two runs
over an unchanged inventory produce byte-identical output and a report can be
committed and diffed.

The exit code answers the same question the command does:

| Code | Meaning |
|---|---|
| `0` | No errors. Warnings and infos may still have been reported. |
| `1` | At least one **error**, or a document that could not be loaded at all. |
| `2` | Usage error — an unknown option, or an unknown rule id in `--disable` — or an unusable `netgraph.toml`. |
| `3` | The inventory could not be discovered or read at all. |

`2` and `3` are failures to *run* the check rather than results of it, which is
why no structured document is written for them.

A severity is not fixed by the code. `[validate.severity]` in
[`netgraph.toml`](configuration.md#validate--how-findings-are-graded) re-grades
any pass-3 rule, and a handful of rules are already graded more harshly than the
specification proposes for reasons written up in
[Where this differs from the specification](validation-rules.md#where-this-differs-from-the-specification).

## `--strict`, and when you want it

`--strict` promotes every surviving warning to an error, so any finding fails the
run. Nothing else changes: the same findings are reported, in the same order,
with the same ids.

<!-- run: -->
```console
$ netgraph -i tests/fixtures/invalid/w103-orphan-device.yaml validate
warnings (1):
  w103-orphan-device.yaml#0:6  W103  device 'pc-a' terminates no cable and hosts no adapter; it is drawn as an isolated node

1 warning
```

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/invalid/w103-orphan-device.yaml validate --strict
errors (1):
  w103-orphan-device.yaml#0:6  W103  device 'pc-a' terminates no cable and hosts no adapter; it is drawn as an isolated node

1 error
```

**Want it in CI**, and want it off at your desk. A warning is netgraph's way of
saying "this is legal, and it is usually a mistake" — which is a useful thing to
be told while you are editing and a bad thing to have merged. The pattern that
works is `--strict` in the pipeline plus explicit suppressions for the warnings
your network really does trip, so the exceptions are written down in the
inventory instead of tolerated by everybody's habit.

`--strict` can only turn strictness *on*. `strict = true` in `netgraph.toml`
makes it the default for a tree, and there is deliberately no `--no-strict` to
undo that from a command line: the file is where a decision about the inventory
belongs. `--strict` is also taken by [`render`](commands/render.md),
[`path`](commands/path.md), [`watch`](commands/watch.md) and
[`export`](export.md), where it decides whether a warning is enough to refuse the
artefact.

## Saying "not here": the four suppressions

Four mechanisms, all additive — a finding is silenced if any of them applies:

1. **`--disable RULE` on the command line.** Repeatable, accepts either spelling
   of an id and the wildcards `*`, `all` and `any`. It *adds* to whatever
   `netgraph.toml` already ignores and cannot re-enable something the file
   disabled. Best for a one-off: narrowing a noisy report while you work through
   the rest of it.
2. **`ignore` and `[validate.severity]` in `netgraph.toml`.** The per-inventory
   decision, versioned with the tree, and the only place that can *re-grade* a
   rule rather than silence it. See
   [`docs/configuration.md`](configuration.md#validate--how-findings-are-graded).
3. **The `netgraph/ignore` annotation on an element.** The per-element
   exception — `netgraph/ignore: "W103, E004"` in an element's
   `metadata.annotations`. Because a finding names every element it involves,
   annotating either end of a cable silences a finding about that cable, and so
   does annotating the cable. Put it on the element the exception genuinely
   belongs to: that is where the next reader will look for the explanation, and a
   comment beside it is worth more than the annotation.
4. **Nothing at all.** Load and schema errors are not suppressible by design.

The order of application is `ignore` first, then the severity override, then
`--strict` — so a rule listed in `ignore` is never reported whatever its severity
says, and a rule re-graded down to `warning` is still promoted back to `error`
under `--strict`.

Naming a schema rule is a usage error rather than a setting that quietly applies
to nothing, and the message lists what you could have meant:

<!-- run: rc=2 -->
```console
$ netgraph -i examples/quickstart validate --disable NG-D005
error: --disable: 'NG-D005' is not a known rule id; expected one of E001, E002, E003, E004, E005, E006, E007, E008, E009, E010, E011, E012, E013, E014, E015, E016, E017, E018, E019, E020, E021, E022, E023, E024, E025, E026, E027, W101, W102, W103, W104, W105, W106, W107, W108, W109, W110, W111, W112, W113, W114, W115, W116, W117, W118, W119, W120, W121, W122, W123, W124, W125, W126, W127, W128, W129, W130, W131, W132, W133, I001, I002, I003, an NG-* alias from docs/schema.md §10, or '*'
```

An unknown id in an *annotation* is ignored rather than fatal — inventory data
must not be able to abort a run — and therefore simply fails to suppress
anything.

[Suppressing a rule](validation-rules.md#suppressing-a-rule) has all four in
full: the exact TOML, the annotation grammar, the accepted separators, and what
happens to an unknown key inside `[validate]`. Each rule's own section ends with
a **Suppress with** line naming its ids and the elements worth annotating.

## Output for a machine

`-F, --output-format` selects one of four:

| Format | For |
|---|---|
| `text` *(default)* | Reading. Findings grouped by severity, most severe first. |
| `json` | A step of your own — `jq`, a dashboard, a diff of two runs. |
| `sarif` | SARIF 2.1.0, for `github/codeql-action/upload-sarif` or any SARIF viewer. |
| `github` | GitHub Actions workflow commands that annotate a pull request in place. |

The three structured formats put their document on **stdout** and move the human
summary to **stderr**, so the output stays pipeable:
`netgraph validate -F sarif > netgraph.sarif` writes a file a code-scanning
upload accepts while a person watching the run still sees what happened.
`--quiet` drops that summary and never the document. All four honour `--strict`
and `--disable`, and re-grading a rule in `netgraph.toml` changes the JSON
`severity`, the SARIF `level` and the workflow command alike.

<!-- norun: both lines redirect, and the second needs a GitHub Actions log to annotate -->
```bash
netgraph -i inventory validate -F sarif --strict > netgraph.sarif
netgraph -i inventory validate -F github
```

[`docs/ci.md`](ci.md) is the reference for all of this: the key-by-key JSON
envelope, what goes into the SARIF run, the composite GitHub Action and the
pre-commit hook this repository ships, and complete workflows for the two ways
findings can reach a pull request.

## Every rule

The table below is generated from `netgraph.rules.RULES`, the same catalogue the
validator and [`netgraph rules`](commands/rules.md) read, so it describes the
build in this repository and cannot drift from it. Each id links to the rule's
section in [`docs/validation-rules.md`](validation-rules.md), where the write-up
says why the rule exists, what it deliberately exempts, and how to suppress it.

<!-- generated: rule-index -->
| Id | Schema id | Severity | Rule |
|---|---|---|---|
| [`E001`](validation-rules.md#e001--unknown-cable-endpoint) | `NG-C002`, `NG-C003` | error | unknown cable endpoint |
| [`E002`](validation-rules.md#e002--interface-terminated-by-more-than-one-cable) | `NG-C005` | error | interface terminated by more than one cable |
| [`E003`](validation-rules.md#e003--duplicate-mac-address) | `NG-I008` | error | duplicate MAC address |
| [`E004`](validation-rules.md#e004--duplicate-ip-address) | `NG-A004` | error | duplicate IP address |
| [`E005`](validation-rules.md#e005--vlan-mismatch-across-a-link) | `NG-C011` | error | VLAN mismatch across a link |
| [`E006`](validation-rules.md#e006--adapter-over-capacity) | `NG-X008` | error | adapter over capacity |
| [`E007`](validation-rules.md#e007--cyclic-interface-stacking) | `NG-I004` | error | cyclic interface stacking |
| [`E008`](validation-rules.md#e008--a-member-is-not-free-to-be-aggregated) | `NG-I005` | error | a member is not free to be aggregated |
| [`E009`](validation-rules.md#e009--sub-interface-vlan-not-carried-by-its-parent) | `NG-V005` | error | sub-interface VLAN not carried by its parent |
| [`E010`](validation-rules.md#e010--multicast-mac-address) | `NG-I009` | error | multicast MAC address |
| [`E011`](validation-rules.md#e011--medium-disagrees-with-the-endpoint-type) | `NG-C006` | error | medium disagrees with the endpoint type |
| [`E012`](validation-rules.md#e012--cable-terminates-on-an-interface-with-no-socket) | `NG-C009` | error | cable terminates on an interface with no socket |
| [`E013`](validation-rules.md#e013--host-attachment-declared-twice) | `NG-X005` | error | host attachment declared twice |
| [`E014`](validation-rules.md#e014--cyclic-adapter-attachment) | `NG-X006` | error | cyclic adapter attachment |
| [`E015`](validation-rules.md#e015--attached_to-names-nothing-that-could-host-the-adapter) | `NG-X001` | error | attached_to names nothing that could host the adapter |
| [`E016`](validation-rules.md#e016--unknown-tunnel-endpoint) | `NG-T002` | error | unknown tunnel endpoint |
| [`E017`](validation-rules.md#e017--tunnel-endpoint-is-not-a-tunnel-interface) | `NG-T003` | error | tunnel endpoint is not a tunnel interface |
| [`E018`](validation-rules.md#e018--over-names-no-tunnel) | `NG-T004` | error | over names no tunnel |
| [`E019`](validation-rules.md#e019--cyclic-tunnel-encapsulation) | `NG-T005` | error | cyclic tunnel encapsulation |
| [`E020`](validation-rules.md#e020--first-hop-is-not-on-link) | `NG-A013` | error | first hop is not on-link |
| [`E021`](validation-rules.md#e021--cable-on-a-position-the-patch-panel-does-not-have) | `NG-P001` | error | cable on a position the patch panel does not have |
| [`E022`](validation-rules.md#e022--patch-panel-position-terminated-twice) | `NG-P003` | error | patch-panel position terminated twice |
| [`E023`](validation-rules.md#e023--patch-panel-where-an-active-element-is-required) | `NG-P004` | error | patch panel where an active element is required |
| [`E024`](validation-rules.md#e024--patch-run-loops-back-into-its-own-panel) | `NG-P005` | error | patch run loops back into its own panel |
| [`E025`](validation-rules.md#e025--two-elements-occupy-the-same-rack-unit) | `NG-U001` | error | two elements occupy the same rack unit |
| [`E026`](validation-rules.md#e026--element-mounted-above-the-top-of-its-rack) | `NG-U002` | error | element mounted above the top of its rack |
| [`E027`](validation-rules.md#e027--rack-declared-with-two-heights) | `NG-U003` | error | rack declared with two heights |
| [`W101`](validation-rules.md#w101--interface-neither-routes-nor-switches) | `NG-I013` | warning | interface neither routes nor switches |
| [`W102`](validation-rules.md#w102--mtu-mismatch-across-a-link) | `NG-C010` | warning | MTU mismatch across a link |
| [`W103`](validation-rules.md#w103--orphan-device) | `NG-C016` | warning | orphan device |
| [`W104`](validation-rules.md#w104--ip-address-on-an-access-port) | `NG-V009` | warning | IP address on an access port |
| [`W105`](validation-rules.md#w105--subnet-with-a-single-member) | `NG-A008` | warning | subnet with a single member |
| [`W106`](validation-rules.md#w106--one-address-claimed-twice-in-a-subnet) | `NG-A009` | warning | one address claimed twice in a subnet |
| [`W107`](validation-rules.md#w107--addresses-on-an-aggregate-member) | `NG-I006` | warning | addresses on an aggregate member |
| [`W108`](validation-rules.md#w108--mac-address-on-a-loopback) | `NG-I007` | warning | MAC address on a loopback |
| [`W109`](validation-rules.md#w109--device-that-cannot-be-cabled) | `NG-I012` | warning | device that cannot be cabled |
| [`W110`](validation-rules.md#w110--network-or-broadcast-address-assigned) | `NG-A005` | warning | network or broadcast address assigned |
| [`W111`](validation-rules.md#w111--overlapping-prefixes-on-one-element) | `NG-A006` | warning | overlapping prefixes on one element |
| [`W112`](validation-rules.md#w112--loopback-with-a-non-host-prefix) | `NG-A007` | warning | loopback with a non-host prefix |
| [`W113`](validation-rules.md#w113--undeclared-vlan-referenced) | `NG-V004` | warning | undeclared VLAN referenced |
| [`W114`](validation-rules.md#w114--native-vlan-missing-from-trunk_vlans) | `NG-V006` | warning | native VLAN missing from trunk_vlans |
| [`W115`](validation-rules.md#w115--every-vlan-trunked-to-a-host) | `NG-V007` | warning | every VLAN trunked to a host |
| [`W116`](validation-rules.md#w116--lag-member-contradicts-its-aggregate) | `NG-V008` | warning | LAG member contradicts its aggregate |
| [`W117`](validation-rules.md#w117--both-ends-of-a-cable-on-one-element) | `NG-C004` | warning | both ends of a cable on one element |
| [`W118`](validation-rules.md#w118--cable-and-endpoint-disagree-about-speed) | `NG-C008` | warning | cable and endpoint disagree about speed |
| [`W119`](validation-rules.md#w119--cable-terminates-on-a-lag-aggregate) | `NG-C012` | warning | cable terminates on a LAG aggregate |
| [`W120`](validation-rules.md#w120--half-duplex-without-a-hub) | `NG-C013` | warning | half duplex without a hub |
| [`W121`](validation-rules.md#w121--disconnected-topology) | `NG-C014` | warning | disconnected topology |
| [`W122`](validation-rules.md#w122--one-hub-two-subnets) | `NG-H005` | warning | one hub, two subnets |
| [`W123`](validation-rules.md#w123--cabled-adapter-with-no-host) | `NG-X002` | warning | cabled adapter with no host |
| [`W124`](validation-rules.md#w124--adapter-attached-to-a-hub-or-a-switch) | `NG-X007` | warning | adapter attached to a hub or a switch |
| [`W125`](validation-rules.md#w125--overlay-reaches-past-its-underlay) | `NG-T006` | warning | overlay reaches past its underlay |
| [`W126`](validation-rules.md#w126--tunnel-mtu-does-not-fit-its-underlay) | `NG-T011` | warning | tunnel MTU does not fit its underlay |
| [`W127`](validation-rules.md#w127--tunnel-carries-traffic-in-the-clear) | `NG-T012` | warning | tunnel carries traffic in the clear |
| [`W128`](validation-rules.md#w128--tunnel-interface-named-by-no-tunnel) | `NG-T013` | warning | tunnel interface named by no tunnel |
| [`W129`](validation-rules.md#w129--two-tunnels-share-a-vni-on-one-element) | `NG-T014` | warning | two tunnels share a VNI on one element |
| [`W130`](validation-rules.md#w130--prefix-claimed-by-two-broadcast-domains) | `NG-A010` | warning | prefix claimed by two broadcast domains |
| [`W131`](validation-rules.md#w131--nested-prefix-in-a-different-broadcast-domain) | `NG-A011` | warning | nested prefix in a different broadcast domain |
| [`W132`](validation-rules.md#w132--address-outside-every-prefix-on-its-link) | `NG-A012` | warning | address outside every prefix on its link |
| [`W133`](validation-rules.md#w133--patch-run-stops-inside-the-panel) | `NG-P002` | warning | patch run stops inside the panel |
| [`I001`](validation-rules.md#i001--locally-administered-mac-address) | `NG-I010` | info | locally administered MAC address |
| [`I002`](validation-rules.md#i002--enabled-interface-terminates-no-cable) | `NG-C015` | info | enabled interface terminates no cable |
| [`I003`](validation-rules.md#i003--tunnel-on-a-non-standard-port) | `NG-T015` | info | tunnel on a non-standard port |
<!-- /generated -->

## See also

* [`docs/validation-rules.md`](validation-rules.md) — the normative catalogue,
  one section per rule, and the full treatment of suppression.
* [`netgraph validate`](commands/validate.md) — the command reference, with the
  flag tables and worked transcripts.
* [`docs/ci.md`](ci.md) — the machine-readable envelopes, the GitHub Action and
  the pre-commit hook.
* [`docs/configuration.md`](configuration.md#validate--how-findings-are-graded) —
  `[validate]` and `[validate.severity]` in `netgraph.toml`.
