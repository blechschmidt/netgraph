# `netgraph validate`

Check the inventory for schema and semantic problems. It exits 1 when anything is
reported as an error and 0 otherwise — so it drops straight into CI, into a
pre-commit hook, or into a `make check` without any wrapping. Nothing is probed
and no device is contacted: every finding comes from the files.

This page is the reference for the command. [`docs/validation.md`](../validation.md)
is the treatment of the three passes, severities and suppression, and
[`docs/validation-rules.md`](../validation-rules.md) is the catalogue of every
rule.

---

## Contents

- [Synopsis](#synopsis)
- [A clean inventory](#a-clean-inventory)
- [An inventory with a problem](#an-inventory-with-a-problem)
- [Warnings, `--strict` and `--disable`](#warnings---strict-and---disable)
- [Repairing what can be repaired](#repairing-what-can-be-repaired)
- [Machine-readable output](#machine-readable-output)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)

---

## Synopsis

<!-- generated: synopsis validate -->
```text
netgraph [GLOBAL OPTIONS] validate [OPTIONS]
```
<!-- /generated -->

## A clean inventory

Nothing to say is said in one line, and the exit code is 0:

<!-- run: -->
```console
$ netgraph -i examples/quickstart validate
no problems found
```

There is no `--summary` and no verbosity level that turns this into a report of
what was checked. If you want to know what the tool would have complained about,
[`netgraph rules`](rules.md) prints the whole vocabulary.

## An inventory with a problem

Findings are grouped by severity, most severe first, and each line reads
`file.yaml#doc:line  RULE  message`. The repository keeps one deliberately broken
fixture per rule, each producing exactly one finding, which makes them the
smallest honest examples available:

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/invalid/e001-unknown-endpoint.yaml validate
errors (1):
  e001-unknown-endpoint.yaml#1:19  E001  cable 'cbl-dangling' endpoint pc-ghost:eth0: no element named 'pc-ghost' is declared in this inventory

1 error
```

`#1:19` is the second document of the file (documents are counted from 0, across
`---` separators) beginning at line 19. `E001` is the id you would name to
suppress the rule, and the message lists every element the finding involves —
here the cable and the endpoint that resolves to nothing.
[How to read a finding](../validation.md#how-to-read-a-finding) takes the line
apart column by column.

`-i` accepts a single YAML file as well as a directory, which is what makes the
invocation above possible. In an inventory tree the location is relative to the
root `-i` names.

## Warnings, `--strict` and `--disable`

A warning is legal-but-probably-wrong, and it does **not** fail the run:

<!-- run: -->
```console
$ netgraph -i tests/fixtures/invalid/w103-orphan-device.yaml validate
warnings (1):
  w103-orphan-device.yaml#0:6  W103  device 'pc-a' terminates no cable and hosts no adapter; it is drawn as an isolated node

1 warning
```

`--strict` promotes every surviving warning to an error, so any finding fails the
run. This is the setting for a pipeline, where a warning nobody has to look at is
a warning nobody looks at:

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/invalid/w103-orphan-device.yaml validate --strict
errors (1):
  w103-orphan-device.yaml#0:6  W103  device 'pc-a' terminates no cable and hosts no adapter; it is drawn as an isolated node

1 error
```

`--strict` can only turn strictness *on*: `strict = true` in `netgraph.toml` makes
it the default for a tree and no command-line flag undoes that.

`--disable RULE` silences a rule by id. It is repeatable, accepts the short id or
either `NG-*` alias, and accepts `*` for all of them. It **adds** to whatever
`netgraph.toml` already ignores and cannot re-enable a rule the file disabled. The
two flags combine in the obvious way — be strict about everything except the
exceptions you have decided are exceptions:

<!-- run: -->
```console
$ netgraph -i tests/fixtures/invalid/w103-orphan-device.yaml validate --strict --disable W103
no problems found
```

Only the pass-3 semantic rules can be disabled. Naming a schema rule is a usage
error rather than a flag that quietly applies to nothing, and the message lists
what you could have meant instead:

<!-- run: rc=2 -->
```console
$ netgraph -i examples/quickstart validate --disable NG-D005
error: --disable: 'NG-D005' is not a known rule id; expected one of E001, E002, E003, E004, E005, E006, E007, E008, E009, E010, E011, E012, E013, E014, E015, E016, E017, E018, E019, E020, E021, E022, E023, E024, E025, E026, E027, E028, E029, E030, E031, E032, E033, E034, E035, E036, E037, E038, E039, E040, E041, E042, E043, E044, E045, E046, E047, E048, E049, E050, W101, W102, W103, W104, W105, W106, W107, W108, W109, W110, W111, W112, W113, W114, W115, W116, W117, W118, W119, W120, W121, W122, W123, W124, W125, W126, W127, W128, W129, W130, W131, W132, W133, W134, W135, W136, W137, W138, W139, W140, W141, W142, W143, W144, W145, W146, W147, W148, W149, W150, W151, W152, W153, W154, I001, I002, I003, I004, I005, an NG-* alias from docs/schema.md §10, or '*'
```

A suppression that belongs to the inventory rather than to one command line
belongs in `netgraph.toml` or in an element's `netgraph/ignore` annotation —
[Saying "not here"](../validation.md#saying-not-here-the-four-suppressions) sets
the four mechanisms side by side, and
[Suppressing a rule](../validation-rules.md#suppressing-a-rule) gives each one in
full.

## Repairing what can be repaired

`--fix` applies the repairs the inventory itself determines and reports
everything else. `--dry-run` prints the unified diff instead of writing:

<!-- run: -->
```console
$ netgraph -i tests/fixtures/fixable validate --fix --dry-run
would fix 3 problems:
  W138  drop 'sw-gone' from the l1 view of layout 'default'
  W108  remove the MAC address from loopback sw-a:Loopback0
  W113  declare VLANs 20, 30 in the 'vlans' database of 'sw-a'
W114 at switches.yaml#0:11 not fixed: 2 repairs are possible; choose one with --choose W114=list|drop
    --choose W114=list  list VLAN 30 in the trunk_vlans of sw-a:GigabitEthernet0/1
    --choose W114=drop  remove the native_vlan of sw-a:GigabitEthernet0/1, so it tags every VLAN it carries

--- a/layout.yaml
+++ b/layout.yaml
@@ -9,4 +9,3 @@
       nodes:
         sw-a: {position: [54, 18]}
         sw-b: {position: [54, 126]}
-        sw-gone: {position: [54, 234]}
--- a/switches.yaml
+++ b/switches.yaml
@@ -16,6 +16,8 @@
   vlans:
     - id: 10
       name: office
+    - id: 20
+    - id: 30
   interfaces:
     - name: GigabitEthernet0/1
       type: ethernet
@@ -26,7 +28,6 @@
         native_vlan: 30
     - name: Loopback0
       type: loopback
-      mac: 00:11:22:33:44:55
       ipv4:
         - 10.255.0.1/32
 ---
warnings (1):
  switches.yaml#0:11  W114  trunk 'sw-a:GigabitEthernet0/1' has native VLAN 30, which is not in its trunk_vlans (10,20); it is carried untagged all the same, so list it

1 warning
```

Three of these four warnings had exactly one sensible repair, so they were made.
The fourth has two, and the command names them rather than choosing:
`--choose W114=list` writes the native VLAN into `trunk_vlans`, `--choose
W114=drop` removes the `native_vlan` instead. With one of those the tree comes
out clean:

<!-- run: -->
```console
$ netgraph -i tests/fixtures/fixable validate --fix --dry-run --choose W114=list
would fix 4 problems:
  W138  drop 'sw-gone' from the l1 view of layout 'default'
  W108  remove the MAC address from loopback sw-a:Loopback0
  W113  declare VLANs 20, 30 in the 'vlans' database of 'sw-a'
  W114  list VLAN 30 in the trunk_vlans of sw-a:GigabitEthernet0/1
--- a/layout.yaml
+++ b/layout.yaml
@@ -9,4 +9,3 @@
       nodes:
         sw-a: {position: [54, 18]}
         sw-b: {position: [54, 126]}
-        sw-gone: {position: [54, 234]}
--- a/switches.yaml
+++ b/switches.yaml
@@ -16,17 +16,18 @@
   vlans:
     - id: 10
       name: office
+    - id: 20
+    - id: 30
   interfaces:
     - name: GigabitEthernet0/1
       type: ethernet
       mtu: 1500
       vlan:
         mode: trunk
-        trunk_vlans: "10,20"
+        trunk_vlans: "10,20,30"
         native_vlan: 30
     - name: Loopback0
       type: loopback
-      mac: 00:11:22:33:44:55
       ipv4:
         - 10.255.0.1/32
 ---
no problems found
```

Every repair is applied on its own and the tree is validated again; a repair is
kept only if the finding it was aimed at is gone and no rule reports more than it
did before. One that fails that test is rolled back to the byte and reported with
the findings it would have introduced, so `--fix` cannot make an inventory worse
and never needs a `--force`.

Writes go through the same path as [`netgraph edit`](edit.md): comments, key
order and quoting survive, and only the lines the repair is about change. `--fix`
needs a *folder*, because an edit session resolves addresses across a whole tree;
pointing `-i` at a single file is a usage error.

[Fixing a finding](../validation-rules.md#fixing-a-finding) lists which rules are
fixable and what each repair does, and `netgraph rules --fixable` prints the same
table.

## Machine-readable output

`-F, --output-format` is `text` to read; `json`, `sarif` or `github` for
automation. The three structured formats put their document on **stdout** and move
the human summary to **stderr**, so the output stays pipeable; `--quiet` drops
that summary and never the document.

* `json` is a documented envelope — tool, inventory root, per-severity counts, a
  `failed` flag and one object per finding with its rule, alias, severity,
  message, element, file, line, column, JSON pointer and help link.
* `sarif` is SARIF 2.1.0, which `github/codeql-action/upload-sarif` and any SARIF
  viewer accept. The rule metadata carries a `helpUri` into
  `docs/validation-rules.md`.
* `github` emits workflow commands that annotate a pull request in place.

<!-- norun: the first line redirects, and the second needs a GitHub Actions log to annotate -->
```bash
netgraph -i inventory validate -F sarif --strict > netgraph.sarif
netgraph -i inventory validate -F github
```

Text output locates a finding at its *document*; the structured formats carry the
line and column of the offending value, which costs a little memory and is why
only they pay for it.

[`docs/ci.md`](../ci.md) documents all three envelopes key by key, plus the
composite GitHub Action and the pre-commit hook this repository ships.

## Arguments

<!-- generated: arguments validate -->
*Takes no positional arguments.*
<!-- /generated -->

## Options

<!-- generated: options validate -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--strict` | — | off | Promote every warning to an error, so any finding fails the run. |
| `--disable` | `RULE` | — | Silence a rule by id (E001, NG-C002, ...). Repeatable. |
| `-F`, `--output-format` | `[text\|json\|sarif\|github]` | `text` | text is for reading; json, sarif and github are for CI. |
| `--fix` | — | off | Repair every problem that has one unambiguous mechanical fix, then report what is left. A fix that would introduce a new finding is undone and reported instead. |
| `-n`, `--dry-run` | — | off | With --fix: print the unified diff the repairs would apply, and write nothing. |
| `--choose` | `RULE=FIX` | — | Pick which repair to use for a rule that offers several, e.g. --choose W114=list. Repeatable. 'netgraph rules --fixable' lists the keys. |
<!-- /generated -->

The global options apply as everywhere else: `-i/--inventory` names the tree,
`-q/--quiet` silences the commentary under the structured formats, and `-v` says
where the inventory was loaded from, how many elements it holds and how many
findings the validator produced.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No errors. Warnings and infos may still have been reported. |
| `1` | At least one **error**, or a document that could not be loaded at all. |
| `2` | Usage error — an unknown option, an unknown rule id in `--disable` — or an unusable `netgraph.toml`. |
| `3` | The inventory could not be discovered or read at all. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

A structured document is written for `0` and `1`. It is not written for the rest:
those are failures to run the check rather than results of it.

## See also

* [`docs/validation.md`](../validation.md) — the three passes, severities,
  suppression and the index of every rule.
* [`docs/validation-rules.md`](../validation-rules.md) — one section per rule, why
  it exists and how to switch it off.
* [`netgraph rules`](rules.md) — the vocabulary `--disable` accepts, printed from
  the build you are running.
* [`docs/ci.md`](../ci.md) — the JSON and SARIF envelopes, the GitHub Action and
  the pre-commit hook.
