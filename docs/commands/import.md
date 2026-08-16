# `netgraph import`

`netgraph import` brings something from outside the tree into the inventory.
There are two sources, and they are different jobs:

| Form | Reads | Produces |
|---|---|---|
| `netgraph import captures/*.json` | what live devices printed | a `devices/` and `cables/` tree, written |
| `netgraph import drawio FILE` | a diagram somebody edited in draw.io | a [`netgraph plan`](plan.md) changeset, confirmed |

The first is the original signature and needs no sub-command: anything that is
not the name of one is read as a capture file, so `netgraph import caps/*.json`
means what it always did. It is spelled `netgraph import captures …` when you
want to be explicit.

This page is the reference for both. [Importing a live network](../importing.md)
is the task for the first; [draw.io round trips](../drawio.md) is the task for
the second, including the one thing a reader most needs — what a draw.io user may
and may not safely change.

---

## Contents

- [Synopsis](#synopsis)
- [What it reads](#what-it-reads)
- [Naming the host a capture came from](#naming-the-host-a-capture-came-from)
- [Writing, and refusing to](#writing-and-refusing-to)
- [A worked example](#a-worked-example)
- [`netgraph import drawio`](#netgraph-import-drawio)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis import -->
```text
netgraph [GLOBAL OPTIONS] import [OPTIONS] COMMAND [ARGS]...
```
<!-- /generated -->

<!-- generated: synopsis import captures -->
```text
netgraph [GLOBAL OPTIONS] import captures [OPTIONS] [NAME=]INPUT...
```
<!-- /generated -->

<!-- generated: synopsis import drawio -->
```text
netgraph [GLOBAL OPTIONS] import drawio [OPTIONS] FILE
```
<!-- /generated -->

---

## What it reads

An input is a file, or `-` for standard input, and several may be given: they are
merged into one inventory. `--from` names the dialect, and its default `auto`
sniffs each input on its own, so one run may mix all three:

| `--from` | Sniffed as | Produced by |
|---|---|---|
| `lldp` | a JSON object with an `lldp` (or `interface`) key | `lldpctl -f json`, `lldpcli -f json show neighbors`; `-f json0` is understood too |
| `iproute` | a JSON array of link records | `ip -j link show`, `ip -j addr show`; pass both for one host and they merge |
| `csv` | anything that is not JSON | any spreadsheet or script that writes `device,port,device,port[,medium[,label]]` rows |

Sniffing is what makes `netgraph import collected/*` work on a directory holding
all three. An input that is JSON but no capture netgraph knows is refused with
the advice to name the dialect; a malformed one is refused with the parser's own
message. [What each dialect reads](../importing.md#what-each-dialect-reads) goes
through the three in detail.

`--exclude PATTERN` leaves out interfaces whose name matches a glob. It applies
to `iproute` captures, where `veth*` and `docker*` are rarely part of a physical
topology, and it is repeatable.

---

## Naming the host a capture came from

An `lldpctl` or `ip` capture describes one host and never says which, so the name
comes from the first of these that applies — most explicit first:

1. `NAME=INPUT` on the argument: `netgraph import sw-core=neighbors.json`;
2. `--host NAME`, which applies to **every** input of the run, so
   `--host pc1 link.json addr.json` means the obvious thing;
3. the file name up to its first dot: `sw-core-01.lldp.json` → `sw-core-01`.

A name taken from a file name is recorded as such in the generated document. A
capture with no name from any of the three — piped in as `-` with no `--host` —
is refused, and so is a `--host` that is not a legal
[element name](../schema.md#41-name-grammar). A CSV needs none of this: every row
names both of its devices. See
[naming the host](../importing.md#naming-the-host-a-capture-came-from).

---

## Writing, and refusing to

`-o/--output` is the inventory root to write into, the current directory by
default; it is created if it does not exist. Files already in that tree are never
overwritten without `--force`, and every clash is named at once rather than one
per run, because an import is re-run repeatedly while the capture set grows.
`--force` replaces those files wholesale, hand edits included, so once you have
started editing prefer a fresh directory and a diff.

`--dry-run` prints the tree to stdout — the same bytes that would be written —
and writes nothing; the run report and the validation findings go to stderr, so
`netgraph import --dry-run … > tree.yaml` keeps the two apart. `--schema` (the
default) points each document at `schema/netgraph.schema.json` with a
`yaml-language-server` modeline and writes that schema when the tree does not
already hold one; `--no-schema` leaves the modeline off.

Re-running on the same captures produces the same bytes, so the command is safe
to put in a script.

---

## A worked example

`tests/fixtures/import/` holds the captures netgraph's own tests import: two LLDP
neighbour tables, one `ip -j addr show` and a patch list exported from a
spreadsheet. Reading all four in one run — dialects sniffed, hosts named after the
files — gives seven devices and seven cables:

<!-- run: cwd=tests/fixtures/import -->
```console
$ netgraph import --dry-run --exclude 'veth*' sw-core-01.lldp.json pc-alice.lldp.json srv-hyper.addr.json patch-panel.csv
# ===== devices/ap-lobby.yaml =====
...
# ===== cables/links.yaml =====
...
4 notes about what was not imported:
...
dry run: 8 files would be written to ., plus schema/netgraph.schema.json

imported 7 devices and 7 cables from 4 inputs

warnings (15):
...
15 warnings, 1 info
...
```

Drop `--dry-run` and add `-o net` to write it. Three things in that report are
worth reading rather than skipping: the notes say what was left out and why, the
`...` in the tree hides comments marking everything netgraph *concluded*, and the
closing paragraph names the findings an imported tree is expected to trip. The
whole of [importing a live network](../importing.md) is about those three.

---

## `netgraph import drawio`

A diagram that came back from draw.io is reconciled by the identity netgraph
stamped into each cell when it exported it — never by the label and never by the
position. Four gestures carry:

| On the canvas | In the inventory |
|---|---|
| a cell moved | a geometry write, and nothing else |
| a label retyped | `rename`, with every reference to it rewritten |
| a cell deleted | `delete`, cascading to what cannot survive it |
| two cells newly joined | `connect`, on the first free port at each end |

Everything becomes a [`netgraph edit`](edit.md) operation and is shown as a
[`netgraph plan`](plan.md) changeset before a single file is touched. `--dry-run`
prints the changeset and the diff and writes nothing; without `--auto-approve`
you are asked to confirm.

`--view` is read from the file and is needed only for a diagram netgraph did not
export. `--geometry`/`--no-geometry`, `--renames`/`--no-renames`,
`--deletions`/`--no-deletions` and `--connections`/`--no-connections` each turn
one of the four gestures off. `--name`, `--namespace` and `--file` say where new
geometry is written; geometry for something an existing layout document already
places goes back into *that* document. `--force` writes even if the result would
introduce a new validation error, and `--json` reports the notes and the session
as JSON.

A cell that is simply missing is a deletion only when the file says it held the
whole view. Export narrowed by `--namespace` and nothing is ever deleted on the
strength of it: absence proves nothing about a diagram that was filtered before
it was drawn. A file netgraph did not export carries no identity at all, so
nothing is reconciled; it is read anyway and reported cell by cell, with the kind
each one looks like, and netgraph will not invent hardware from a rectangle.

<!-- norun: needs a diagram that has been round-tripped through draw.io -->
```console
$ netgraph export drawio -o site.drawio      # hand this out
$ netgraph import drawio site.drawio -n      # see what came back
$ netgraph import drawio site.drawio         # apply it
```

---

## Arguments

<!-- generated: arguments import captures -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[NAME=]INPUT...` | no | any number | — |
<!-- /generated -->

<!-- generated: arguments import drawio -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `FILE` | yes | 1 | — |
<!-- /generated -->

---

## Options

### `netgraph import captures`

<!-- generated: options import captures -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--from` | `[auto\|lldp\|iproute\|csv\|netplan\|networkd\|ifupdown\|frr\|wireguard\|interfaces]` | `auto` | Input dialect. 'auto' sniffs each input on its own, so one run may mix all nine: lldp is 'lldpctl -f json', iproute is 'ip -j link show' or 'ip -j addr show', csv is 'device,port,device,port' cabling rows, and netplan, networkd, ifupdown, frr, wireguard and interfaces are a device's running configuration in the same dialects 'netgraph export' writes. |
| `--host` | `NAME` | — | Device every input was captured on. An lldp or iproute capture never names its own host. Without this the name comes from the file name, or from a 'NAME=path' argument. |
| `-o`, `--output` | `DIRECTORY` | current directory | Inventory root to write the devices/ and cables/ tree into. |
| `--dry-run` | — | off | Print the tree to stdout and write nothing. |
| `--force` | — | off | Overwrite files that are already in the output tree. Without it they are refused. |
| `--schema`, `--no-schema` | — | `--schema` | Point each generated document at schema/netgraph.schema.json with a yaml-language-server modeline, writing the schema when the tree does not already hold one. |
| `--exclude` | `PATTERN` | — | Leave out interfaces whose name matches this glob. Applies to 'iproute' captures, where 'veth*' and 'docker*' are rarely part of a physical topology. Repeatable. |
<!-- /generated -->

### `netgraph import drawio`

<!-- generated: options import drawio -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--view` | `[physical\|l1\|l2\|l3\|overlay\|routing\|rack\|power\|identity\|netns]` | the view the file says it was exported from | Which view the diagram draws. Read from the file for anything netgraph exported; needed only for a diagram netgraph did not write. |
| `--geometry`, `--no-geometry` | — | `--geometry` | Carry cells that were dragged back as stored geometry. |
| `--renames`, `--no-renames` | — | `--renames` | Carry a retyped label back as a rename, rewriting every reference to it. |
| `--deletions`, `--no-deletions` | — | `--deletions` | Carry a deleted cell back as a deleted element. Never applied to a diagram that was exported from a filtered view, whichever way this is set. |
| `--connections`, `--no-connections` | — | `--connections` | Carry an edge drawn in draw.io back as a cable on the first free port at each end. |
| `--name` | `NAME` | `layout` | metadata.name of the layout document new geometry goes into. Geometry for something an existing layout already places is written back into that one instead. |
| `--namespace` | `PATH` | — | Folder to declare a new layout document in. The inventory root by default. |
| `--file` | `PATH` | — | File to write a new layout document to. Chosen by the layout conventions when absent. |
| `--auto-approve` | — | off | Do not ask before writing. For automation; a person should read the changeset first. |
| `-n`, `--dry-run` | — | off | Write nothing; print the changeset and the unified diff it would produce. |
| `--force` | — | off | Write even if the result would introduce new validation errors. |
| `--json` | — | off | Report as JSON. |
<!-- /generated -->

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The tree was written (or printed) and holds no error-level finding. |
| 1 | The generated tree does not validate, or nothing could be imported from the inputs at all. The files are still written, so you can see what went wrong. |
| 2 | Usage error, or an unusable `netgraph.toml`. |
| 3 | An input was missing, unreadable, not UTF-8, oversized, not the dialect it was given as — or a file in the output tree would have been clobbered without `--force`. `import drawio` also uses it for a file that is not a draw.io diagram it can read. |

Warnings and infos do not fail the run: an imported inventory is partial by
construction and
[legitimately has findings](../importing.md#the-findings-afterwards-are-expected).
The configuration that decides which of them are reported is the *output* tree's
`netgraph.toml`, not the working directory's, so importing into a tree that
already ignores a rule does not produce a report contradicting it.

---

## See also

* [Importing a live network](../importing.md) — what to collect, what is inferred
  and what to fix afterwards.
* [`netgraph init`](init.md) — a first tree for a network that does not exist yet.
* [`netgraph fmt`](fmt.md) — put the generated YAML into the canonical form
  before committing it.
* [`netgraph validate`](validate.md) — the same check `import` runs, on demand.
* [draw.io round trips](../drawio.md) — handing a diagram out, and what comes
  back.
* [`netgraph export`](export.md) — the other half of the round trip,
  `export drawio`.
