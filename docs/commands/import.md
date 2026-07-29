# `netgraph import`

`netgraph import` builds a first inventory out of output you already collected on
live devices: LLDP neighbour tables, `ip -j` captures and `device,port,device,port`
cabling lists become a `devices/` and `cables/` tree in the layout
[`netgraph init`](init.md) writes. No host is contacted and no credential is
read — you run the collection command and hand netgraph what it printed. This
page is the reference; [importing a live network](../importing.md) is the task,
including what to collect and how to correct what comes out.

---

## Contents

- [Synopsis](#synopsis)
- [What it reads](#what-it-reads)
- [Naming the host a capture came from](#naming-the-host-a-capture-came-from)
- [Writing, and refusing to](#writing-and-refusing-to)
- [A worked example](#a-worked-example)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis import -->
```text
netgraph [GLOBAL OPTIONS] import [OPTIONS] [NAME=]INPUT...
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

## Arguments

<!-- generated: arguments import -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[NAME=]INPUT...` | no | any number | — |
<!-- /generated -->

---

## Options

<!-- generated: options import -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--from` | `[auto\|lldp\|iproute\|csv]` | `auto` | Input dialect. 'auto' sniffs each input on its own, so one run may mix all three: lldp is 'lldpctl -f json', iproute is 'ip -j link show' or 'ip -j addr show', csv is 'device,port,device,port' cabling rows. |
| `--host` | `NAME` | — | Device every input was captured on. An lldp or iproute capture never names its own host. Without this the name comes from the file name, or from a 'NAME=path' argument. |
| `-o`, `--output` | `DIRECTORY` | current directory | Inventory root to write the devices/ and cables/ tree into. |
| `--dry-run` | — | off | Print the tree to stdout and write nothing. |
| `--force` | — | off | Overwrite files that are already in the output tree. Without it they are refused. |
| `--schema`, `--no-schema` | — | `--schema` | Point each generated document at schema/netgraph.schema.json with a yaml-language-server modeline, writing the schema when the tree does not already hold one. |
| `--exclude` | `PATTERN` | — | Leave out interfaces whose name matches this glob. Applies to 'iproute' captures, where 'veth*' and 'docker*' are rarely part of a physical topology. Repeatable. |
<!-- /generated -->

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The tree was written (or printed) and holds no error-level finding. |
| 1 | The generated tree does not validate, or nothing could be imported from the inputs at all. The files are still written, so you can see what went wrong. |
| 2 | Usage error, or an unusable `netgraph.toml`. |
| 3 | An input was missing, unreadable, not UTF-8, oversized, not the dialect it was given as — or a file in the output tree would have been clobbered without `--force`. |

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
