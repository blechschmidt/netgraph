# `netgraph-render` action

A composite action that installs netgraph and Graphviz, runs `netgraph render`
over an inventory, and leaves the diagram on disk for whatever the workflow does
next — publish it, attach it to a release, upload it as an artifact, or compare
it with the one that was there before.

The default format is `html`: **one self-contained page**, with the diagram, the
layer switcher and the search box in it, and nothing to fetch. That is what makes
the output of a `render` step publishable as-is, which is what
[`netgraph-pages.yml`](../../workflows/netgraph-pages.yml) does with it.

Full write-up: [`docs/ci.md`](../../../docs/ci.md#the-render-action).

## Usage

Publish a diagram of the inventory to GitHub Pages on every push — the whole
job, though the [reusable workflow](../../workflows/netgraph-pages.yml) is
shorter:

```yaml
name: diagram

on:
  push:
    branches: [main]

permissions:
  contents: read
  pages: write
  id-token: write

jobs:
  diagram:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - uses: blechschmidt/netgraph/.github/actions/netgraph-render@main
        with:
          inventory: inventory
          # Three layers become three views behind one switcher.
          layer: l1 l2 l3
          title: ${{ github.repository }}
          output: site/index.html
          strict: "true"

      - uses: actions/upload-pages-artifact@v3
        with:
          path: site
      - id: deploy
        uses: actions/deploy-pages@v4
```

Attach an SVG to the pull request instead, with no Pages and no extra
permission:

```yaml
      - id: diagram
        uses: blechschmidt/netgraph/.github/actions/netgraph-render@main
        with:
          inventory: inventory
          format: svg
          args: --icons cisco --collapse-depth 1

      - uses: actions/upload-artifact@v4
        with:
          name: topology
          path: ${{ steps.diagram.outputs.file }}
```

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `inventory` | `.` | Root folder of the YAML tree, or a single YAML file. |
| `format` | `html` | `dot`, `svg`, `html`, `png`, `pdf`, `mermaid` or `json`. |
| `output` | *(derived)* | Where to write the diagram; parent directories are created. Defaults to `${RUNNER_TEMP}/netgraph.<extension>`. |
| `layer` | *(empty)* | Layers to draw, separated by commas or whitespace. Several is only meaningful for `html`, which draws each and puts a switcher over them. |
| `title` | *(empty)* | Caption for the diagram. |
| `theme` | *(empty)* | `blueprint`, `mono`, `none`, or a path to a `kind: theme` document. |
| `args` | *(empty)* | Further `netgraph render` flags, split on whitespace. |
| `strict` | `false` | Treat validation warnings as errors and refuse to draw. |
| `force` | `false` | Draw even when validation failed. The diagram may then misrepresent the network. |
| `graphviz` | `auto` | Install the `dot` binary. `auto` does so only when the format needs it and the runner does not already have it. |
| `install` | `true` | Install netgraph first. Set to `false` if an earlier step already did. |
| `version` | *(empty)* | pip requirement to install. Empty installs the checkout the action came from, pinning the tool to the ref the workflow chose. |

## Outputs

| Output | Meaning |
|---|---|
| `file` | Path of the diagram that was written. |
| `directory` | The directory holding it — hand this to `actions/upload-pages-artifact`. |
| `bytes` | Size of the diagram in bytes. |

## Notes

`python` must already be on `PATH` — use `actions/setup-python` before this
action, as the examples do. That is deliberate: the action does not choose an
interpreter version on your behalf. Graphviz *is* installed, because it is not a
Python dependency and a missing `dot` is otherwise a failure at the last step of
a job that had already done all the work; `graphviz: false` turns that off for an
image that ships its own.

**A diagram that was not drawn fails the step.** Exit code 0 with bytes on disk
is not proof: the action checks the file it wrote for the shape of the format it
asked for — an `<svg>` element for `html` and `svg`, `graph netgraph` for `dot`,
a `flowchart` header for `mermaid`, a `nodes` array for `json` — so an empty or
truncated page fails here rather than being published.

**On a Windows runner, `args` is protected from the shell twice.** Globbing is
off while the value is split, and the step sets `MSYS=noglob` — because the MSYS
runtime Git Bash is built on expands wildcards in the arguments it hands to a
*native* program, after bash has finished with them. Without it a
`--name 'sw*'` filter would arrive at `netgraph.exe` as whatever the workspace
happened to be holding. Paths are normalised to forward slashes for the same
family of reasons; the `file` and `directory` outputs are reported that way.

**Validation runs first, as it does on the command line.** An inventory with a
dangling cable refuses to render, because a diagram drawn from it would
misrepresent the network. `force: "true"` overrides that for a tree still under
construction; `strict: "true"` goes the other way and makes warnings fatal.
