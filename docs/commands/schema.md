# `netgraph schema`

Print the JSON Schema (2020-12) for netgraph documents, generated from the same
pydantic models the loader uses. Point an editor at it and a typo'd key is
underlined as you type rather than found by the next `netgraph validate`. It needs
no inventory — the schema describes the document format, not your network.

## Synopsis

<!-- generated: synopsis schema -->
```text
netgraph [GLOBAL OPTIONS] schema [OPTIONS]
```
<!-- /generated -->

## What you get

By default one schema covering every kind, discriminated on `kind`, so a single
`yaml.schemas` entry matching a glob is enough for a whole inventory tree:

<!-- run: -->
```console
$ netgraph schema
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://netgraph.dev/schema/v1alpha1/element.json",
  "title": "netgraph element document",
...
```

`-k, --kind KIND` emits the schema for a single kind instead — including
`template` — which is what you want when a document is mapped to a schema by
directory rather than by content, because the editor then offers only the fields
that belong to that kind:

<!-- run: -->
```console
$ netgraph schema --kind switch
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://netgraph.dev/schema/v1alpha1/switch.json",
  "title": "netgraph switch document",
...
```

`--all` is the default and exists so you can say so explicitly. Asking for both is
a usage error rather than a flag that quietly loses:

<!-- run: rc=2 -->
```console
$ netgraph schema --all --kind switch
Usage: netgraph schema [OPTIONS]
Try 'netgraph schema --help' for help.

Error: --all and --kind are mutually exclusive.
```

`-o, --output FILE` writes to a file instead of stdout, creating parent
directories as needed. That is the form worth putting in a `make` target:

<!-- norun: writes into the reader's directory -->
```bash
netgraph schema -o schema/netgraph.schema.json
```

The `$id` is versioned alongside `apiVersion` —
`https://netgraph.dev/schema/v1alpha1/element.json` — and a future `v1beta1` gets
its own rather than replacing it.

## Wiring an editor to it

The schema is only useful once a language server can find it, and there are three
ways to arrange that:

* **Let [`netgraph init`](init.md) do it.** With `--schema` (the default) it
  writes `schema/netgraph.schema.json` into the new tree *and* puts a
  `# yaml-language-server: $schema=…` modeline on every document it generates,
  with the relative depth computed per file. See
  [`schema/netgraph.schema.json` and the modelines](init.md#schemanetgraphschemajson-and-the-modelines).
* **Use the copy committed in this repository.**
  [`schema/netgraph.schema.json`](../../schema/netgraph.schema.json) is the exact
  document `netgraph schema` prints, so an editor, a pre-commit hook or a CI job
  can reach it by path or by URL without installing netgraph first.
  `tests/test_schema.py` fails when it drifts from the models; refresh it with
  `netgraph schema -o schema/netgraph.schema.json`, or with
  `python tools/gen_json_schema.py` in a checkout that has no netgraph installed.
* **Do it by hand**, per file with a modeline or per tree with a `yaml.schemas`
  glob. [Editor setup](../getting-started.md#editor-setup-autocompletion-and-inline-errors)
  has the modeline, the VS Code settings block and the Neovim and JetBrains
  equivalents; [`docs/schema.md` §13](../schema.md#13-editor-integration) has the
  per-kind mapping and a caveat about what YAML will and will not let a schema
  see.

**It does not replace `netgraph validate`.** A JSON Schema sees one document at a
time, so it checks structure, value grammars and the rules inside a single object
— the [schema pass](../validation.md#the-three-passes), roughly. Whether a cable
endpoint names an element that exists, whether names are unique, whether the two
ends of a link agree about a VLAN: all of that needs the whole tree and stays with
[`netgraph validate`](validate.md). Keep running it in CI.

## Arguments

<!-- generated: arguments schema -->
*Takes no positional arguments.*
<!-- /generated -->

## Options

<!-- generated: options schema -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-k`, `--kind` | `[switch\|router\|hub\|computer\|server\|cable\|adapter\|tunnel\|patchpanel\|pdu\|user\|group\|template\|layout]` | — | Emit the schema for a single document kind instead of all of them. |
| `--all` | — | off | Emit one schema covering every kind, discriminated on 'kind'. The default. |
| `-o`, `--output` | `FILE` | — | Write to this file instead of stdout. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The schema was printed or written. |
| `2` | Usage error — `--all` together with `--kind`, or a `--kind` that is not a document kind. |
| `141` | The downstream end of a pipe closed first. |

There is no `1`: the command reads no inventory, so it has nothing to reject. An
`--output` path that cannot be written fails with the operating system's own
error.

## See also

* [`docs/schema-reference.md`](../schema-reference.md) — the same fields as a
  human-readable lookup table, generated from the same models.
* [`docs/getting-started.md`](../getting-started.md#editor-setup-autocompletion-and-inline-errors)
  — wiring the schema into VS Code, Neovim and the JetBrains IDEs.
* [`netgraph init`](init.md) — writes the schema and the modelines into a new
  inventory for you.
* [`netgraph validate`](validate.md) — the checks a single-document schema cannot
  make.
