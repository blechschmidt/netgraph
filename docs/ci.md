# netgraph in CI

`netgraph validate` is built to be a gate. It loads the tree, applies every rule
in [`docs/validation-rules.md`](validation-rules.md), prints what it found and
exits non-zero when anything is an error — so the shortest useful pipeline is
one line:

```console
$ netgraph --inventory inventory validate --strict
```

This page covers the rest: the machine-readable output formats, the composite
GitHub Action, the pre-commit hook, and complete workflows for the two ways
findings can reach a pull request.

* [Output formats](#output-formats)
* [Exit codes](#exit-codes)
* [The JSON envelope](#the-json-envelope)
* [SARIF and code scanning](#sarif-and-code-scanning)
* [Inline annotations](#inline-annotations)
* [The GitHub Action](#the-github-action)
* [Workflow: upload SARIF](#workflow-upload-sarif)
* [Workflow: annotate the diff](#workflow-annotate-the-diff)
* [pre-commit](#pre-commit)
* [Other CI systems](#other-ci-systems)

## Output formats

`-F, --output-format` selects one of four:

| Format | Goes to | For |
|---|---|---|
| `text` *(default)* | stdout | Reading. Findings grouped by severity, most severe first. |
| `json` | stdout | A step of your own — `jq`, a dashboard, a diff of two runs. |
| `sarif` | stdout | `github/codeql-action/upload-sarif`, or any SARIF viewer. |
| `github` | stdout | GitHub Actions annotations, straight in the log. |

Three properties hold for all of them.

**stdout is the document, stderr is the commentary.** Under the three
structured formats the human summary moves to stderr, so
`netgraph validate -F sarif > report.sarif` writes a file that uploads cleanly
while a person watching the run still sees what happened.

**`--quiet` silences the summary, never the document.** `-q` is about the
commentary; a pipeline that redirects stdout gets the same bytes with and
without it.

**The order is deterministic**: file, then line, then rule id. Two runs over an
unchanged inventory produce byte-identical output, so a report can be committed
and diffed. Every format honours `--strict` and `--disable`, and re-grading a
rule in `netgraph.toml` changes the `severity` field, the SARIF `level` and the
workflow command alike.

`--strict` and `--disable` behave exactly as they do for `text`:

```console
$ netgraph -i inventory validate -F sarif --strict --disable W105 --disable I002 > netgraph.sarif
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No errors. Warnings and infos may still have been reported. |
| `1` | At least one **error**, or a document that could not be loaded. |
| `2` | Usage error — an unknown option, an unknown rule id in `--disable` — or an unusable `netgraph.toml`. |
| `3` | The inventory path does not exist or cannot be read at all. |

A structured document is written for `0` and `1`. It is *not* written for the
rest: those are failures to run the check, not results of it. The full table is
in the [README](../README.md#exit-codes).

## The JSON envelope

```console
$ netgraph -q -i inventory validate -F json
```

```json
{
  "schemaVersion": 1,
  "tool": { "name": "netgraph", "version": "0.1.0" },
  "inventory": { "root": "/home/ops/net/inventory", "prefix": "inventory" },
  "summary": { "error": 1, "warning": 0, "info": 0, "total": 1 },
  "failed": true,
  "findings": [
    {
      "rule": "E001",
      "alias": "NG-C002",
      "severity": "error",
      "message": "cable 'cables/cbl-core-desk' endpoint pc-desk:eth0: no element named 'pc-desk' is declared in this inventory",
      "element": "cables/cbl-core-desk",
      "namespace": "cables",
      "kind": "cable",
      "file": "cables/links.yaml",
      "document": 0,
      "line": 8,
      "column": 7,
      "pointer": "/spec/endpoints/1",
      "help": "https://github.com/blechschmidt/netgraph/blob/main/docs/validation-rules.md#e001--unknown-cable-endpoint"
    }
  ]
}
```

| Key | Type | Meaning |
|---|---|---|
| `schemaVersion` | integer | Version of *this* envelope. Bumped only when a key is renamed or removed; a new optional key does not bump it. |
| `tool.version` | string | The netgraph release that produced the report. |
| `inventory.root` | string | Absolute path of the directory `findings[].file` is relative to. |
| `inventory.prefix` | string | The same directory relative to the working directory, or `""`. Prepend it to `file` to get a repository-relative path. |
| `summary` | object | One count per severity, plus `total`. Every severity is present, zero included. |
| `failed` | boolean | Would the run have failed? Equivalent to `summary.error > 0`. |

Each entry of `findings`:

| Key | Type | Meaning |
|---|---|---|
| `rule` | string | Canonical rule id (`E001`), or `load` for a problem the loader or the schema rejected the document over. |
| `alias` | string \| null | The `NG-*` identifier from [the schema](schema.md) §10. |
| `severity` | string | `error`, `warning` or `info`, **after** `netgraph.toml`, `netgraph/ignore`, `--disable` and `--strict`. |
| `message` | string | One line, naming every element involved. |
| `element` | string \| null | Fully-qualified name of the element the finding is anchored to. `null` for a load error, which has no element yet. |
| `namespace` | string \| null | Namespace of `element`; `""` at the inventory root. |
| `kind` | string \| null | `switch`, `router`, `cable`, … |
| `file` | string \| null | POSIX path relative to `inventory.root`. |
| `document` | integer \| null | 0-based index of the document within `file`, counting `---` separators. |
| `line`, `column` | integer \| null | 1-based position of the offending **value**. |
| `pointer` | string \| null | [RFC 6901](https://datatracker.ietf.org/doc/html/rfc6901) pointer to the offending field, `null` for a whole-document problem. |
| `help` | string | Permanent link to the write-up of the rule. |

`line`, `column` and `pointer` follow the loader's provenance, so a value a
device inherited from a `template` is reported at the line of the **template**,
which is the line that has to change. `file` moves with it.

Counting warnings by rule, for a dashboard:

```console
$ netgraph -q -i inventory validate -F json \
    | jq -r '.findings | group_by(.rule) | map({rule: .[0].rule, n: length}) | .[] | "\(.n)\t\(.rule)"'
```

## SARIF and code scanning

`-F sarif` emits a [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/)
log with exactly one run. It is validated against the official schema in
netgraph's own test suite, so the format cannot drift.

Every rule is described once in `runs[0].tool.driver.rules`, **whether or not it
fired** — a driver that only lists the rules that happened to trigger tells a
reader nothing about what was checked. Each descriptor carries the id, a
`name` (`UnknownCableEndpoint`), a short and a full description, the default
level, and a `helpUri` pointing at the rule's section of
[`docs/validation-rules.md`](validation-rules.md). The `NG-*` aliases travel in
`properties.aliases`.

Severities map as SARIF requires: `error` → `error`, `warning` → `warning`,
`info` → `note`.

Results carry a `partialFingerprints` entry built from the rule, the file, the
element and the field — deliberately **not** from the line, so inserting a
document above a broken one does not close an alert and open an identical new
one.

Loader and schema problems are filed under the id `load` rather than under an
`NG-*` id, and `load` is described in the driver like any other rule. That keeps
every `ruleId` resolvable, which is what preserves the severity and the help
link in the code-scanning UI; the specific `NG-D005`-style id is still in the
finding's `alias` and in the JSON envelope.

## Inline annotations

`-F github` writes [workflow commands](https://docs.github.com/actions/reference/workflow-commands-for-github-actions):

```text
::error file=inventory/cables/links.yaml,line=8,col=7,title=E001 unknown cable endpoint::cable 'cables/cbl-core-desk' endpoint pc-desk:eth0: no element named 'pc-desk' is declared in this inventory
```

`info` findings become `::notice`, so they annotate the diff without colouring
the check run. Paths are relative to the directory the command ran in — the
repository root in a normal job — which is what makes the annotation land on the
right file even when the inventory is in a subdirectory.

This needs no `security-events` permission and no upload, at the cost of the
annotations disappearing with the run.

## The GitHub Action

[`.github/actions/netgraph-validate`](../.github/actions/netgraph-validate/) is
a composite action that installs netgraph, runs the check and reports the
result. Its own [README](../.github/actions/netgraph-validate/README.md) has the
full input and output tables; the two workflows below are the reason it exists.

`python` must already be on `PATH` — put `actions/setup-python` before it. The
action deliberately does not pick an interpreter for you.

## Workflow: upload SARIF

Findings become code-scanning alerts, with the rule description and the help
link attached, and they persist across runs.

```yaml
name: inventory

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read
  # Required by upload-sarif. Nothing else in this workflow needs it.
  security-events: write

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - id: netgraph
        uses: blechschmidt/netgraph/.github/actions/netgraph-validate@v0.1.0
        with:
          inventory: inventory
          strict: "true"
          output-format: sarif
          # The upload has to happen even when the inventory does not validate.
          # Failing here would mean the one run with something to report is the
          # one run that reports nothing.
          fail-on-error: "false"

      - uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: ${{ steps.netgraph.outputs.report }}
          # Keeps netgraph's alerts separate from any other analysis in the repo.
          category: netgraph

      - name: Fail the job if the inventory did not validate
        if: steps.netgraph.outputs.failed == 'true'
        run: exit 1
```

## Workflow: annotate the diff

No extra permission, no upload, no alert history: findings appear on the changed
lines of the pull request and are gone when the run is.

```yaml
name: inventory

on: [push, pull_request]

permissions:
  contents: read

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - uses: blechschmidt/netgraph/.github/actions/netgraph-validate@v0.1.0
        with:
          inventory: inventory
          output-format: github
          strict: "true"
          # Two rules an inventory under construction legitimately trips.
          disable: W105,I002
```

Without the action, the same thing by hand:

```yaml
      - run: pip install netgraph
      - run: netgraph --inventory inventory validate --output-format github --strict
```

## pre-commit

netgraph ships `.pre-commit-hooks.yaml`, so an inventory repository can run the
same check before the commit is written:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/blechschmidt/netgraph
    rev: v0.1.0
    hooks:
      - id: netgraph-validate
        args: [--strict]
```

The hook validates the tree as a whole rather than the staged files — a cable is
only dangling when compared with the devices in the *other* files — so it takes
no filenames, and runs whenever a commit touches any `.yaml`/`.yml` file.

It validates the current directory. For an inventory that does not sit at the
repository root, override `entry`, which is where the global `--inventory`
option has to go:

```yaml
      - id: netgraph-validate
        entry: netgraph --inventory inventory validate
        args: [--strict]
        files: ^inventory/.*\.ya?ml$
```

## Other CI systems

Nothing above is GitHub-specific except the two output formats named after it.
Anywhere else, the JSON envelope plus the exit code is the whole interface:

```yaml
# .gitlab-ci.yml
validate:
  image: python:3.12
  script:
    - pip install netgraph
    - netgraph --inventory inventory validate --strict --output-format json > netgraph.json
  artifacts:
    when: always
    paths: [netgraph.json]
```

SARIF is not GitHub-specific either — GitLab, Azure DevOps and most static
analysis dashboards ingest it — so `-F sarif` is worth trying before writing a
converter for `-F json`.
