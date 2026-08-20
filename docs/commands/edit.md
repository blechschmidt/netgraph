# `netviz edit`

Change the inventory through typed operations rather than through a text editor:
rename a switch and every reference to it, delete a device and the cables that
terminate on it, set a field, cable two ports together. Each one is checked
before it is written, is reversible exactly, and leaves every line it did not
mean to change byte for byte as it was.

[`docs/editing.md`](../editing.md) explains the operation model — what an
operation is, what its inverse is, and why the write path is built this way. This
page is the reference for the command.

## Contents

- [The operations](#the-operations)
- [Addresses and field paths](#addresses-and-field-paths)
- [Three things it always does](#three-things-it-always-does)
- [`--dry-run`: see the diff first](#--dry-run-see-the-diff-first)
- [`--json`: the undo stack](#--json-the-undo-stack)
- [Operations as JSON](#operations-as-json)
- [Where a new document goes](#where-a-new-document-goes)
- [Exit status](#exit-status)
- [Reference](#reference)

---

## The operations

<!-- norun: illustrative one-liners over an inventory that is not in the repository -->
```bash
netviz edit set core-sw spec.model 'C9300'            # write a field
netviz edit unset core-sw spec.location               # remove one
netviz edit create switch sw-new --namespace sites/hq # declare an element
netviz edit delete sw-old --cascade                   # remove one, and its cables
netviz edit rename sw-old sw-new                      # and every reference to it
netviz edit move sw-new sites/hq/access/sw-new.yaml   # another file, another namespace
netviz edit connect sw-new:Gi1/0/1 pc-desk:eno1       # a cable between two ports
netviz edit disconnect cbl-sw-new-pc-desk             # and back out again
netviz edit add-interface sw-new Gi1/0/2              # a port
netviz edit remove-interface sw-new Gi1/0/2 --cascade # and what terminated on it
```

Each subcommand applies exactly one operation. `netviz edit apply` takes any
number of them as JSON, which is how a script or an editor drives it.

One operation has no subcommand of its own: `set-geometry`, which writes one
view of a [`kind: layout`](../schema.md#18-layout-diagram-geometry) document.
Coordinates are not something anybody types, so it is reached through
[`netviz layout`](layout.md) — or through `apply`, which is how a canvas will
reach it when a node is dragged.

## Addresses and field paths

An **address** is anything that names one element: a fully-qualified name
(`sites/hq/access/sw-01`), or a short name when exactly one element in the
inventory carries it (`sw-01`). It is resolved the way every reference in the
inventory is resolved ([§2.2](../schema.md#22-namespaces-and-name-resolution)),
so a name that matches two elements is refused with both spelled out rather than
guessed at.

A **field path** names a value inside a document, in the notation the
diagnostics already use: `spec.model`, `spec.interfaces[2].mtu`,
`metadata.labels.site`. Mappings on the way to the value are created if they are
not there; a sequence entry is not, because `spec.interfaces[7]` on a device
with three ports is a typo rather than an instruction.

A **value** is read as a YAML scalar, so `1500` is a number, `true` is a boolean
and `[10, 20]` is a list — the same reading the value would have got had it been
typed into the document by hand. `--string` switches that off for the times a
model number really is `1500`.

## Three things it always does

**It preserves everything it did not change.** Comments, blank lines, key order
and quoting style survive; a document nothing touched is written back as the
exact bytes it was read as. A diff of an edit is the edit.

**It refuses to break the tree.** Before writing, the tree is loaded as it *would
be* and validated. If the edit would introduce an error the inventory does not
already have, nothing is written and the new problems are listed. Existing
problems are not held against you — an inventory that fails `validate` is exactly
when an editor is most useful — and `--force` overrides the check when you know
better. Warnings never block a write.

**It refuses to clobber.** Every file it reads is hashed, and the hash is checked
again immediately before the write. A file that changed on disk in between — your
editor, a `git checkout`, another netviz — is reported and the edit is dropped.
`--force` does *not* skip this: overwriting somebody else's work is not something
a flag can mean.

## `--dry-run`: see the diff first

`-n`/`--dry-run` writes nothing and prints the unified diff it would have
written, with git's `a/`/`b/` prefixes so `git apply` accepts it:

<!-- norun: writes to the working tree; the transcript would depend on run order -->
```console
$ netviz -i examples/home-lab edit set sw-home spec.model 'TL-SG108PE' --dry-run
--- a/switches/sw-home.yaml
+++ b/switches/sw-home.yaml
@@ -8,7 +8,7 @@
     role: access
 spec:
   vendor: TP-Link
-  model: TL-SG108E
+  model: TL-SG108PE
   location: Home / hallway cabinet
   interfaces:
     - name: br0
```

The validation gate still runs, so a dry run that would have been refused says
so.

## `--json`: the undo stack

`--json` prints the applied operations, the operations that undo them, and the
files touched. Keeping the `inverse` list and feeding it back to
`netviz edit apply` is a complete undo:

<!-- norun: writes to the working tree, and the JSON embeds whole files -->
```bash
netviz edit rename sw-home sw-hall --json > /tmp/change.json
jq '.inverse' /tmp/change.json | netviz edit apply --force
```

Undo is byte-exact: after applying the inverses the tree is the tree you
started with, comment for comment. [`docs/editing.md`](../editing.md#inverses)
explains why some inverses are `write-file` rather than the obvious opposite
operation.

## Operations as JSON

`netviz edit apply` reads one JSON object or a list of them from stdin, or
from `-f`/`--file`. Every object carries an `op` and the keys that operation
takes:

<!-- norun: illustrative payload; the elements are not in this repository -->
```bash
netviz edit apply <<'JSON'
[{"op": "create", "kind": "switch", "name": "sw-new", "namespace": "sites/hq",
  "spec": {"interfaces": [{"name": "Gi1/0/1", "type": "ethernet"}]}},
 {"op": "connect", "a": "sw-new:Gi1/0/1", "b": "pc-desk:eno1"},
 {"op": "set", "address": "sites/hq/sw-new", "path": "spec.vendor", "value": "Cisco"}]
JSON
```

The list is applied in order and judged as one change, so an operation that is
only valid once a later one has run — a cable to a device the previous operation
created — is fine. `netviz edit` with no subcommand is the same thing.

The full set of operations and their keys is in
[`docs/editing.md`](../editing.md#the-operations).

## Where a new document goes

`create` and `connect` place their document themselves unless `--file` says
otherwise, following the conventions in
[`docs/inventory-layout.md`](../inventory-layout.md):

* into the file that already holds elements of that kind in that namespace —
  unless that file is named after the single element it holds, which is the
  layout's own marker for "this device owns a file";
* otherwise into a new file: `cables.yaml` or `tunnels.yaml` for a link, since
  links belong in a collection, and `<name>.yaml` for anything else.

`--file` is checked rather than obeyed blindly: it has to be a YAML file inside
the inventory that the loader would actually read, and its folder has to be the
namespace asked for, because a document's folder *is* its namespace.

Deleting the last document of a file deletes the file, and the folders that empty
out under it.

## Exit status

| Code | When |
|---|---|
| 0 | The edit was applied (or, with `--dry-run`, could have been). |
| 1 | It was refused: an unknown address, a dangling reference, a new validation error, or a file that changed on disk. |
| 2 | Usage error — an unknown flag, a malformed field path, unparseable JSON. |

Nothing is ever half-written: a refusal writes no file at all.

## Reference

### `netviz edit`

<!-- generated: synopsis edit -->
```text
netviz [GLOBAL OPTIONS] edit [OPTIONS] [COMMAND] [ARGS]...
```
<!-- /generated -->

<!-- generated: options edit -->
*No options of its own; the global options apply.*
<!-- /generated -->

### `netviz edit set`

<!-- generated: synopsis edit set -->
```text
netviz [GLOBAL OPTIONS] edit set [OPTIONS] ADDRESS PATH VALUE
```
<!-- /generated -->

<!-- generated: options edit set -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--string` | — | off | Take VALUE literally instead of reading it as YAML, so 1500 stays a string. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit unset`

<!-- generated: synopsis edit unset -->
```text
netviz [GLOBAL OPTIONS] edit unset [OPTIONS] ADDRESS PATH
```
<!-- /generated -->

<!-- generated: options edit unset -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit create`

<!-- generated: synopsis edit create -->
```text
netviz [GLOBAL OPTIONS] edit create [OPTIONS] {switch|router|firewall|hub|computer|server|cable|adapter|tunnel|patchpanel|pdu|user|group} NAME
```
<!-- /generated -->

<!-- generated: options edit create -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--namespace` | `TEXT` | — | Folder to declare it in, relative to the inventory root. The root by default. |
| `--spec` | `TEXT` | `{}` | The element's spec, as JSON. |
| `--metadata` | `TEXT` | `{}` | Description, labels and annotations, as JSON. |
| `--file` | `TEXT` | — | File to write it to, relative to the inventory root. Chosen by the layout conventions when absent. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit copy`

Copies an element — or a whole namespace, subtree and all — into a new document.
The copy keeps the original's comments, gets a free name (`sw1` → `sw1-copy` →
`sw1-copy-2`), and loses the fields two elements in one inventory cannot both
have. A cable whose two ends are both in the copied set is cloned and rewired to
the clones; one with only a single end in it is left behind and named. See
[the copy chapter of `docs/editing.md`](../editing.md#copying-cutting-and-pasting)
for the whole table.

<!-- generated: synopsis edit copy -->
```text
netviz [GLOBAL OPTIONS] edit copy [OPTIONS] ADDRESS
```
<!-- /generated -->

<!-- generated: options edit copy -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--to` | `TEXT` | — | Namespace to write the copies into; the folder each original is in by default. The empty string is the inventory root. |
| `--name` | `TEXT` | — | metadata.name of the copy. Derived from the original's when absent; only meaningful when copying one element. |
| `--suffix` | `TEXT` | `copy` | What a derived name gets before its counter: sw1 -> sw1-copy -> sw1-copy-2. |
| `--keep-unique` | — | off | Keep the MAC addresses, fixed IP addresses, serials and outlets a copy normally drops. The result usually fails validation; use it when the copy is a starting point you are about to edit. |
| `--view` | `[physical\|l1\|l2\|l3\|ipam\|overlay\|routing\|rack\|power\|identity\|netns\|security]` | — | Place the copies in this view's stored geometry, offset from the originals. Nothing is written to a layout document when absent. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit duplicate`

`netviz edit copy` with no `--to`: the copy lands in the namespace the
original is in. The same operation under the name a diagram editor gives it —
`Ctrl-D` in `netviz web` writes exactly this.

<!-- generated: synopsis edit duplicate -->
```text
netviz [GLOBAL OPTIONS] edit duplicate [OPTIONS] ADDRESS
```
<!-- /generated -->

<!-- generated: options edit duplicate -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--name` | `TEXT` | — | metadata.name of the copy. Derived from the original's when absent; only meaningful when copying one element. |
| `--suffix` | `TEXT` | `copy` | What a derived name gets before its counter: sw1 -> sw1-copy -> sw1-copy-2. |
| `--keep-unique` | — | off | Keep the MAC addresses, fixed IP addresses, serials and outlets a copy normally drops. The result usually fails validation; use it when the copy is a starting point you are about to edit. |
| `--view` | `[physical\|l1\|l2\|l3\|ipam\|overlay\|routing\|rack\|power\|identity\|netns\|security]` | — | Place the copies in this view's stored geometry, offset from the originals. Nothing is written to a layout document when absent. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit delete`

<!-- generated: synopsis edit delete -->
```text
netviz [GLOBAL OPTIONS] edit delete [OPTIONS] ADDRESS
```
<!-- /generated -->

<!-- generated: options edit delete -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--cascade` | — | off | Also delete the cables and tunnels that terminate on it, and the notes and areas that cannot be drawn without it, and clear the optional references to it. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit rename`

<!-- generated: synopsis edit rename -->
```text
netviz [GLOBAL OPTIONS] edit rename [OPTIONS] ADDRESS NEW_NAME
```
<!-- /generated -->

<!-- generated: options edit rename -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit move`

<!-- generated: synopsis edit move -->
```text
netviz [GLOBAL OPTIONS] edit move [OPTIONS] ADDRESS FILE
```
<!-- /generated -->

<!-- generated: options edit move -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit connect`

<!-- generated: synopsis edit connect -->
```text
netviz [GLOBAL OPTIONS] edit connect [OPTIONS] A B
```
<!-- /generated -->

<!-- generated: options edit connect -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--medium` | `[copper\|fiber\|wireless]` | `copper` | What the link is made of. |
| `--speed` | `TEXT` | — | Negotiated link rate, e.g. 1Gbps. |
| `--label` | `TEXT` | — | The identifier printed on the cable. |
| `--name` | `TEXT` | — | metadata.name of the cable; derived from the endpoints when absent. |
| `--namespace` | `TEXT` | — | Folder to declare it in. The nearest folder containing both ends by default. |
| `--file` | `TEXT` | — | File to write it to, relative to the inventory root. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit disconnect`

<!-- generated: synopsis edit disconnect -->
```text
netviz [GLOBAL OPTIONS] edit disconnect [OPTIONS] ADDRESS
```
<!-- /generated -->

<!-- generated: options edit disconnect -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--cascade` | — | off | Also delete the notes and areas that cannot be drawn without the cable. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit add-interface`

<!-- generated: synopsis edit add-interface -->
```text
netviz [GLOBAL OPTIONS] edit add-interface [OPTIONS] ADDRESS NAME
```
<!-- /generated -->

<!-- generated: options edit add-interface -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--type` | `TEXT` | `ethernet` | Interface type, as spec.interfaces[].type spells it. |
| `--description` | `TEXT` | — | What the port is for. |
| `--field` | `PATH=VALUE` | — | Any other key of the interface, e.g. --field mtu=9000. Repeatable. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit remove-interface`

<!-- generated: synopsis edit remove-interface -->
```text
netviz [GLOBAL OPTIONS] edit remove-interface [OPTIONS] ADDRESS NAME
```
<!-- /generated -->

<!-- generated: options edit remove-interface -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--cascade` | — | off | Also remove the cables and tunnels that terminate on the interface. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

### `netviz edit apply`

<!-- generated: synopsis edit apply -->
```text
netviz [GLOBAL OPTIONS] edit apply [OPTIONS]
```
<!-- /generated -->

<!-- generated: options edit apply -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-f`, `--file` | `FILE` | — | Read the operations from this file instead of from stdin. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

## See also

- [`docs/editing.md`](../editing.md) — the operation model, in prose.
- [`docs/inventory-layout.md`](../inventory-layout.md) — the conventions placement follows.
- [`netviz fmt`](fmt.md) — the other command that writes YAML, and the canonical form `edit` keeps a canonical file in.
- [`netviz validate`](validate.md) — the check the write gate runs.
