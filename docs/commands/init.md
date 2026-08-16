# `netviz init`

An empty directory is a bad place to start from: the document envelope has four
keys and an `apiVersion` nobody remembers, and the JSON Schema that would have
told your editor about both has to be found and wired up by hand. `netviz init`
writes a tree that is already correct — it validates clean and renders at every
layer before a line has been edited, and each document points at a schema written
alongside it, so the first key you type is completed and the first typo
underlined.

---

## Contents

- [Synopsis](#synopsis)
- [What it writes](#what-it-writes)
  - [`netviz.toml`](#netviztoml)
  - [`.gitignore`](#gitignore)
  - [`schema/netviz.schema.json` and the modelines](#schemanetvizschemajson-and-the-modelines)
  - [The example topology](#the-example-topology)
  - [`--minimal`: the envelope and nothing else](#--minimal-the-envelope-and-nothing-else)
- [Where it writes, and when it refuses](#where-it-writes-and-when-it-refuses)
- [A worked example](#a-worked-example)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis init -->
```text
netviz [GLOBAL OPTIONS] init [OPTIONS] [PATH]
```
<!-- /generated -->

---

## What it writes

Seven files by default, in this order — configuration first, then the editor
wiring, then the documents:

```text
my-network/
├── netviz.toml                  # every setting, commented out and explained
├── .gitignore                     # rendered diagrams are output, not source
├── schema/netviz.schema.json    # editor wiring, deliberately committed
├── devices/rtr-gw.yaml
├── devices/sw-office.yaml
├── devices/pc-alice.yaml
└── cables/links.yaml              # two documents in one file
```

Nothing else happens: no git repository is created, no CI workflow is added, and
no file outside the target directory is touched. Which files appear depends only
on `--minimal` and `--schema` / `--no-schema`:

| File | Written | Contents |
|---|---|---|
| `netviz.toml` | always | Every `[validate]`, `[render]` and `[profile.*]` key commented out, with the default it would change. |
| `.gitignore` | always | Rendered diagrams and `/out/`, and a comment saying why the schema is *not* ignored. |
| `schema/netviz.schema.json` | unless `--no-schema` | The JSON Schema this netviz version generates, the same document `netviz schema` prints. |
| `devices/rtr-gw.yaml` | unless `--minimal` | The router: a WAN hand-off, a downlink, VLAN 10. |
| `devices/sw-office.yaml` | unless `--minimal` | The switch: two access ports, no address. |
| `devices/pc-alice.yaml` | unless `--minimal` | The host: one addressed interface, no VLAN block. |
| `cables/links.yaml` | unless `--minimal` | Two cable documents in one file, separated by `---`. |
| `devices/example.yaml` | with `--minimal` | One fully commented envelope template, in place of the four documents above. |

The generated content lives in
[`src/netviz/scaffold.py`](../../src/netviz/scaffold.py), where building the
tree is a pure function and writing it is the only part that touches a disk —
which is how `tests/test_init.py` can assert that the tree validates clean, that
it validates clean under `--strict`, that it renders at layers `l1`, `l2` and
`l3`, and that the schema written is the one this version generates, rather than
assuming any of it.

### `netviz.toml`

The file is generated *fully commented* on purpose: what is commented out is
exactly what netviz already does, so uncommenting a line is a visible decision
rather than a guess. It covers

* `[validate]` — `strict`, and `ignore` for rules you never want reported;
* `[validate.severity]` — re-grading a single rule to `"error"`, `"warning"` or
  `"info"` instead of silencing it;
* `[render]` — how *this* inventory is drawn when the command line does not say
  otherwise. Every key is a long flag of `netviz render` without its leading
  dashes, so `--collapse-depth 1` is `collapse-depth = 1` and `--no-show-ips` is
  `show-ips = false`;
* `[profile.poster]` and `[profile.review]` — two named profiles, as examples of
  one entry per diagram you produce regularly.

Because every line is commented, the file changes nothing until you edit it:
`netviz config show` on a fresh tree reports the built-in defaults, each with
the place it came from. See [`docs/configuration.md`](../configuration.md).

### `.gitignore`

The YAML tree is the source of truth and `netviz render` regenerates diagrams
from it, so the generated file ignores `*.dot`, `*.mmd`, `*.pdf`, `*.png`, `*.svg`
and `/out/`, with a comment inviting you to drop the line for a format you
publish on purpose — a `network.svg` committed for a README, say.

`schema/netviz.schema.json` is deliberately *not* ignored. It is editor wiring,
and a fresh checkout should offer completion before netviz is installed;
the file says so, and says to refresh it with
`netviz schema -o schema/netviz.schema.json`.

### `schema/netviz.schema.json` and the modelines

With `--schema` (the default) two things happen together: the schema is written,
and every generated document gets a first line pointing at it —

```yaml
# yaml-language-server: $schema=../schema/netviz.schema.json
apiVersion: netviz.dev/v1alpha1
kind: router
```

The reference is relative rather than the published `$id`, so the tree keeps
working offline and inside a container, and checks against the schema of the
netviz version that wrote it rather than of whatever is published today. The
depth is computed per file, so a document one directory down gets `../` and a
document at the root would get none.

`--no-schema` skips both halves — no schema file, no modelines — and the "next
steps" report drops the line about installing a language server. It is opt-out
rather than opt-in because an editor with nothing to complete from is the
situation `init` exists to fix. The per-editor setup is in
[`docs/getting-started.md`](../getting-started.md#editor-setup-autocompletion-and-inline-errors)
and, in full, in [`docs/schema.md` §13](../schema.md#13-editor-integration).

### The example topology

The four documents are the tree the
[getting-started walkthrough](../getting-started.md) builds and the one checked
in as [`examples/quickstart`](../../examples/quickstart): a router, a switch, a
host, and the two cables between them. A reader who follows both is not shown
two different networks.

They are also annotated, because the interesting part of an example is the
reasoning:

* `rtr-gw` carries a `netviz/ignore: NG-C015` annotation, with a comment
  explaining that `wan0` faces an ISP which is not an element of this inventory
  and so terminates no cable on purpose. Saying so on the one element that has a
  reason is what an exception looks like; deleting the rule for everybody would
  not be.
* `sw-office` notes that a bridge carries VLAN membership on its ports and no
  address of its own, and that a management address belongs on a `type: vlan`
  SVI (putting one on a bridge port is `W104`).
* `pc-alice` notes the *absence* of a `vlan` block: the host sends untagged
  frames and inherits the VLAN of the access port facing it, which netviz knows
  not to complain about.
* `cables/links.yaml` notes that it holds two documents separated by `---`, and
  that a cable is an element in its own right rather than a field on a device.

### `--minimal`: the envelope and nothing else

`--minimal` writes `devices/example.yaml` instead of the four documents. Every
line of it is a comment, so the tree declares no elements at all and still
validates clean — the point is to show the four keys and the values they take,
not to hand out a network someone has to delete before writing their own. It
lists the available kinds, shows one interface with a VLAN, and points at
`netviz schema --kind switch` for the full grammar of a single kind.

A tree with no elements renders an empty graph, and `netviz render` says so
with a warning rather than failing. `--minimal` still wires the editor unless you
also pass `--no-schema`.

---

## Where it writes, and when it refuses

`PATH` defaults to the current directory, and is created along with any missing
parents when it does not exist. The "next steps" report prints `cd` only when it
is needed — the two commands under it are run from the inventory root, which is
already the shell's directory when `init` was given no argument.

Scaffolding is a one-shot convenience over files you would otherwise type by
hand, so it never overwrites them by accident. Without `--force`, a target that
holds anything at all is refused and left untouched, and the message names what
is in the way: the files that would have been overwritten when there are any —
the likeliest mistake is running `init` twice —

<!-- norun: the transcript is of a directory the reader already scaffolded -->

```console
$ netviz init my-network
error: my-network would overwrite netviz.toml, .gitignore, schema/netviz.schema.json, devices/rtr-gw.yaml, devices/sw-office.yaml, devices/pc-alice.yaml, cables/links.yaml; pass --force to write anyway, or name an empty directory
```

— and otherwise the first few entries the directory holds:

<!-- norun: the transcript is of a directory outside the repository -->

```console
$ netviz init notes
error: notes is not empty (it holds a, b, c, d, e, ...); pass --force to write anyway, or name an empty directory
```

`--force` writes anyway, overwriting files of the same name and leaving
everything else alone. A `PATH` that exists and is not a directory is a usage
error, caught before anything is written.

---

## A worked example

From nothing to a validated, rendered inventory:

<!-- norun: writes a new inventory into the reader's directory -->

```console
$ netviz init my-network
created 7 files in my-network:
  netviz.toml
  .gitignore
  schema/netviz.schema.json
  devices/rtr-gw.yaml
  devices/sw-office.yaml
  devices/pc-alice.yaml
  cables/links.yaml

next steps:
  cd my-network
  netviz validate
  netviz render -f svg -o network.svg

  each document points at schema/netviz.schema.json; install a yaml-language-server (the VS Code YAML extension, nvim's yamlls) for completion and inline errors
```

Both printed commands succeed as they stand — that is the property the tree is
built for. `netviz -q init …` scaffolds without a word, which is what you want
inside a script; the files written are the same either way.

For the empty-envelope variant with no editor wiring:

<!-- norun: writes a new inventory into the reader's directory -->

```console
$ netviz init --minimal --no-schema my-network
created 3 files in my-network:
  netviz.toml
  .gitignore
  devices/example.yaml

next steps:
  cd my-network
  netviz validate
  netviz render -f svg -o network.svg
```

If you already have a network, [`netviz import`](import.md) is usually the
better first command: it builds the same shape of tree out of LLDP neighbours,
`ip -j addr show` output or the cabling list you already keep, so the first
inventory is a diff away from correct. See
[`docs/importing.md`](../importing.md).

---

## Arguments

<!-- generated: arguments init -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[PATH]` | no | 1 | `.` |
<!-- /generated -->

---

## Options

<!-- generated: options init -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--minimal` | — | off | Write the commented envelope template instead of the example topology. |
| `--force` | — | off | Write into a directory that already holds something, overwriting files of the same name. |
| `--schema`, `--no-schema` | — | `--schema` | Write the JSON Schema next to the tree and point each document at it with a yaml-language-server modeline, so an editor completes and checks as you type. |
<!-- /generated -->

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The tree was written. |
| `1` | netviz refused to write: the target is not empty and `--force` was not given, it exists and is not a directory, or it cannot be listed or written to. Nothing is written in any of these cases. |
| `2` | Usage error — an unknown option, or a `PATH` that names an existing file. |

---

## See also

* [`docs/getting-started.md`](../getting-started.md) — the same tree built by
  hand, then validated, rendered and queried.
* [`netviz import`](import.md) — scaffold from a network that already exists.
* [`docs/configuration.md`](../configuration.md) — the `netviz.toml` keys the
  generated file comments out.
* [`netviz schema`](schema.md) — regenerate or narrow the schema `init` writes.
