# `netviz apply`

`netviz apply` executes a plan written by [`netviz plan`](plan.md) against
the inventory **files**. Each changeset entry becomes one or more of the typed
operations [`netviz edit`](edit.md) is built from, so comments, key order and
formatting survive, and the same validation gate applies: an edit that would
introduce a new error is refused before anything is written.

> **Applying to the live network is deliberately out of scope.** This command
> writes YAML and nothing else. It opens no session to a device, reads no
> credential, and there is no flag that makes it. The loop it closes runs the
> other way: adopt what the network reports into the declared inventory.

## Contents

- [Synopsis](#synopsis)
- [The loop it closes](#the-loop-it-closes)
- [What each entry becomes](#what-each-entry-becomes)
- [Three things it always does](#three-things-it-always-does)
- [`--target`: applying a subset](#--target-applying-a-subset)
- [`--dry-run`: see the diff first](#--dry-run-see-the-diff-first)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis apply -->
```text
netviz [GLOBAL OPTIONS] apply [OPTIONS] PLAN
```
<!-- /generated -->

`PLAN` is a file written by `netviz plan -out`. The inventory it is applied to
comes from the global `-i/--inventory`, and must be the one the plan was made
from — see [the state hash](plan.md#plan-files-and-the-state-hash).

---

## The loop it closes

<!-- norun: writes files, against an inventory that is not in the repository -->
```console
$ netviz -i net drift caps/*.json           # the network disagrees with the files
$ netviz -i net plan --from-live caps/*.json -out drift.plan
$ netviz -i net apply drift.plan            # make the files say what the network does
```

`drift` reports. `plan --from-live` turns the report into a changeset that adopts
what the network says. `apply` writes it. Nothing in that sequence contacts a
device: you collect the output, and netviz reads what you collected.

The same three commands work for a proposal that came from a person rather than
from a capture — `plan --to ../proposed -out change.plan` then `apply
change.plan` — which is how a reviewed change lands exactly as it was reviewed.

---

## What each entry becomes

| Entry | Operations |
|---|---|
| `create` | [`create`](edit.md#netviz-edit-create) with the planned body. Where the document goes is left to the placement rules, which put it where the tree's own conventions say. |
| `delete` | [`delete`](edit.md#netviz-edit-delete), never cascading. A plan that deletes a device also deletes the cables on it, in that order; a cascade would be `apply` doing something the plan did not say. |
| `rename` | [`rename`](edit.md#netviz-edit-rename), which rewrites every reference to the old name as it goes. |
| `update` | One `set` or `unset` per field — except an interface appearing or disappearing whole, which is [`add-interface`](edit.md#netviz-edit-add-interface) or [`remove-interface`](edit.md#netviz-edit-remove-interface). |
| a `layout` address | `set-geometry`, one view at a time, because that is the unit geometry is written in. |

A plan stores a field path with the list entry *named*
(`spec.interfaces[name=eth0].mtu`). `apply` resolves the name to the index the
edit layer wants against the document as it stands at that moment in the run, so
a plan that adds two interfaces and then sets a field on the first still lands on
the right one. A name that no longer selects anything is an error, not a guess.

---

## Three things it always does

**It checks the plan is about this tree.** Before anything else, the inventory is
hashed and compared with the hash the plan recorded. A tree that has moved on
since — a colleague pushed, a script ran, the branch changed — is refused, and
the fix is to re-run `netviz plan` and read the new one. There is no flag to
skip this: a plan applied to a state it was not made from is not a description of
what will happen.

**It asks.** The summary and one line per change are printed, then a
confirmation. `--auto-approve` skips it, for automation; a closed stdin is a no,
not a yes. `--dry-run` never asks, because it never writes.

**It validates.** The tree is loaded as it *would be* and checked, and an edit
that would add an error is refused with the problems listed and nothing written.
The comparison is per rule and by count, so an inventory that already fails
`validate` can still be applied to — not making it worse is the bar, not absolute
cleanliness. `--force` writes anyway; the state check is never skipped by it.

Every file the run touched is also hashed when it is read and checked again
immediately before the write, so a file that changed on disk under the command
is a refusal rather than a silent overwrite.

---

## `--target`: applying a subset

`--target` keeps only the changes a glob selects, matched against the address
(`device.core/sw-1`), the qualified name (`core/sw-1`) or the short name
(`sw-1`). It is how a plan with one contentious entry in it still gets the rest
applied:

<!-- norun: writes files, against an inventory that is not in the repository -->
```bash
netviz apply drift.plan --target 'device.core/*'       # one namespace
netviz apply drift.plan --target sw-core-01            # one element
netviz apply drift.plan --target 'cable.*' --target ap-1
```

A rename is matched at either end: targeting the new name plainly asks for the
rename that produces it.

The entries that survive the filter keep their order, so the dependency
guarantees still hold. What the filter cannot do is make an inconsistent subset
consistent — selecting a cable whose device the plan also creates will be refused
at the validation gate, which is the right answer.

---

## `--dry-run`: see the diff first

`-n/--dry-run` writes nothing and prints the unified diff the plan would produce,
in `git apply` form. It is the second review, after the plan itself: the plan
says what changes, the diff says what the files will look like.

<!-- norun: the output depends on the inventory being edited -->
```console
$ netviz -i net apply drift.plan --dry-run
Plan: ~ 1 to change.
  ~ device.switches/sw-home  [switch]
--- a/switches/sw-home.yaml
+++ b/switches/sw-home.yaml
@@ -35,7 +35,7 @@
       type: ethernet
       description: Uplink to rtr-home
       mac: '00:22:07:aa:00:01'
-      mtu: 1500
+      mtu: 9000
       vlan:
         mode: access
set switches/sw-home spec.interfaces[2].mtu = 9000
1 operation(s) from drift.plan
would change 1 file(s): switches/sw-home.yaml
```

`--json` prints the applied operations and their inverses, the same envelope
[`netviz edit --json`](edit.md#--json-the-undo-stack) produces, so a caller can
keep an undo stack.

---

## Arguments

<!-- generated: arguments apply -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `PLAN` | yes | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options apply -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--target` | `ADDRESS` | — | Apply only the changes this glob selects, matched against the address (device.core/sw-1), the qualified name or the short name. Repeatable. |
| `--auto-approve` | — | off | Do not ask before writing. For automation; a person should read the plan first. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the plan would produce. |
| `--force` | — | off | Write even if the result would introduce new validation errors. The state check is never skipped: a plan is only ever applied to the tree it was made from. |
| `--json` | — | off | Report as JSON. |
<!-- /generated -->

## Exit codes

| Code | When |
|---|---|
| 0 | The plan was applied, or there was nothing in it to apply. |
| 1 | The file is not a plan, the tree has moved on since the plan was made, the inventory does not load, the confirmation was declined, an entry could not be expressed against this tree, or the result would introduce a new error. |
| 2 | Usage: no plan file, or one that does not exist. |

Nothing is ever partially written: every refusal happens before the first byte
reaches the disk.

## See also

- [`netviz plan`](plan.md) — producing the changeset this executes.
- [`netviz edit`](edit.md) — the operations it is built from, one gesture at a time.
- [`netviz drift`](drift.md) — the read half of the loop.
- [`docs/editing.md`](../editing.md) — the write path, in prose.
