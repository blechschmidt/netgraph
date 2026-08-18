# `netviz-validate` action

A composite action that installs netviz, runs `netviz validate` over an
inventory, and hands the result to whichever GitHub feature you want to see it
in — code scanning, inline annotations, or a step of your own.

Full write-up, including the pre-commit hook and the shape of the JSON
envelope: [`docs/ci.md`](../../../docs/ci.md).

## Usage

Upload to code scanning, so findings show up as alerts with the rule
descriptions and a link to the write-up of each rule:

```yaml
name: inventory

on: [push, pull_request]

permissions:
  contents: read
  security-events: write

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - id: netviz
        uses: blechschmidt/netviz/.github/actions/netviz-validate@v0.0.2
        with:
          inventory: inventory
          strict: "true"
          # Let the upload run even when the inventory does not validate --
          # otherwise the one run that had something to report never reports it.
          fail-on-error: "false"

      - uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: ${{ steps.netviz.outputs.report }}
          category: netviz

      - if: steps.netviz.outputs.failed == 'true'
        run: exit 1
```

Annotate the diff instead, with no code-scanning upload and no extra
permissions:

```yaml
      - uses: blechschmidt/netviz/.github/actions/netviz-validate@v0.0.2
        with:
          inventory: inventory
          output-format: github
          disable: W105,I002
```

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `inventory` | `.` | Root folder of the YAML tree, or a single YAML file. |
| `strict` | `false` | Promote every warning to an error (`--strict`). |
| `disable` | *(empty)* | Rule ids to silence, separated by commas or whitespace. Short ids (`E001`) and schema aliases (`NV-C002`) both work. |
| `output-format` | `sarif` | `text`, `json`, `sarif` or `github`. |
| `output-file` | *(derived)* | Where to write the document; defaults to `${RUNNER_TEMP}/netviz-validate.<format>`. Ignored by `github` and `text`, which write to the log. |
| `fail-on-error` | `true` | Fail the step when the inventory does not validate. |
| `install` | `true` | Install netviz first. Set to `false` if an earlier step already did. |
| `version` | *(empty)* | pip requirement to install. Empty installs the checkout the action came from, pinning the tool to the ref the workflow chose. |

## Outputs

| Output | Meaning |
|---|---|
| `exit-code` | `netviz validate`'s exit code: `0` clean, `1` findings, `2` usage. |
| `failed` | `'true'` when the inventory did not validate. |
| `report` | Path of the written document; empty for `github` and `text`. |

`python` must already be on `PATH` — use `actions/setup-python` before this
action, as the examples do. That is deliberate: the action does not choose an
interpreter version on your behalf.

If [uv](https://docs.astral.sh/uv/) is on `PATH` and a virtualenv is active, the
install uses `uv pip install` instead — roughly ten times faster on a cold
runner. `astral-sh/setup-uv` with `activate-environment: true` arranges both and
is a drop-in for the `setup-python` step above. Neither is required: pip is the
fallback and behaves identically.

The action carries no lockfile of its own. netviz's `uv.lock` pins the versions
*that* repository develops against; here you are installing a released netviz
into an environment of your own. Pin `version:` — that is the knob with your name
on it.

