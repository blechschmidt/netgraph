# `netviz-review` action

A composite action that reviews a change to a netviz inventory: it produces
the typed changeset between two refs, draws the visual diff, validates both
sides and writes the Markdown body of one pull-request comment saying all three.

It posts nothing itself. Posting is the workflow's job — use the reusable
[`netviz-review.yml`](../../workflows/netviz-review.yml), which does the
sticky comment, the artifact upload and the SARIF upload, or wire up your own
with the outputs below.

Full write-up: [`docs/ci.md`](../../../docs/ci.md), under *Workflow: review a
pull request*.

## Usage

The whole review, posted as a sticky comment, is one `uses:` of the reusable
workflow. Reach for this action directly when you want the pieces:

```yaml
name: review

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write
  security-events: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - id: netviz
        uses: blechschmidt/netviz/.github/actions/netviz-review@v0.0.2
        with:
          inventory: inventory
          base: ${{ github.event.pull_request.base.sha }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          # Report first, fail afterwards: a step that stopped here would never
          # publish the review that explains why it stopped.
          fail-on-new-errors: "false"

      - uses: actions/upload-artifact@v4
        with:
          name: netviz-review
          path: ${{ steps.netviz.outputs.directory }}

      - uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: ${{ steps.netviz.outputs.sarif }}
          category: netviz

      - run: cat "${{ steps.netviz.outputs.comment }}" >>"$GITHUB_STEP_SUMMARY"

      - if: steps.netviz.outputs.failed == 'true'
        run: exit 1
```

## What it does

1. Installs Graphviz and netviz, unless told not to.
2. Fetches `base` if the clone does not hold it. A `pull_request` checkout is
   one commit deep, so the base commit usually has to be fetched — this does it
   for you, one object deep, rather than making every caller remember
   `fetch-depth: 0`.
3. Draws `netviz diff` in each of `formats`. Each drawing is allowed to fail:
   a missing Graphviz costs the review its picture and not its changeset.
4. Runs `netviz review`, which loads both states, validates both, diffs them
   and writes five files into `output-directory`:

   | File | What |
   |---|---|
   | `comment.md` | The comment body. Its first line is the sticky marker. |
   | `plan.json` | The changeset, as `netviz plan --json` writes it. Absent when the head does not load. |
   | `netviz.sarif` | The head's findings, for `upload-sarif`. |
   | `summary.json` | The verdict and the counts, for a step that gates on them. |
   | `diff.svg`, `diff.png` | Whatever was drawn. |
   | `outputs.txt` | The step outputs below, as written to `$GITHUB_OUTPUT`. |

5. Sets the outputs, and fails only if `fail-on-new-errors` is on **and** the
   change introduced an error the base did not have.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `inventory` | `.` | Root folder of the YAML tree, or a single YAML file. |
| `base` | *(required)* | The state before the change: a git ref or a folder. On a pull request, `${{ github.event.pull_request.base.sha }}`. |
| `head` | *(empty)* | The state after it. Empty is the working tree, which is what the checkout already holds. |
| `head-sha` | *(empty)* | Commit the head is, so findings link to their lines. Empty leaves them unlinked rather than pointing at a moving branch. |
| `title` | `netviz` | Heading of the comment, and the key of its sticky marker. Two inventories need two titles. |
| `layer` | *(empty)* | Which layer the diff is drawn at. One only — a diff compares one view. |
| `theme` | *(empty)* | Stylesheet for the diagram. |
| `diagram-args` | *(empty)* | Further `netviz diff` flags, split on whitespace. |
| `strict` | `false` | Promote every warning to an error, on **both** sides. |
| `disable` | *(empty)* | Rule ids to silence on both sides, separated by commas or whitespace. |
| `formats` | `svg,png` | Image formats to draw. Empty draws none and needs no Graphviz. |
| `artifact-name` | `netviz-review` | What the bundle is called, for the link text. |
| `artifact-url` | *(derived)* | Where it can be downloaded. Defaults to this run's page, whose artifact list is the one URL known before the upload happens. |
| `diagram-url` | *(empty)* | Where a rendering is published, `LABEL=URL` per line. Only such a URL can be **embedded**; an artifact is a zip behind an authenticated endpoint, so it is linked. |
| `output-directory` | `${RUNNER_TEMP}/netviz-review` | Where the files above go. |
| `fail-on-new-errors` | `false` | Fail this step on an error the base did not have. Pre-existing errors never fail it. |
| `graphviz` | `auto` | Install `dot`: only when a chosen format needs it and the runner lacks it. |
| `install` | `true` | Install netviz first. |
| `version` | *(empty)* | pip requirement to install. Empty installs the checkout the action came from, pinning the tool to the ref the workflow chose. |

## Outputs

| Output | Meaning |
|---|---|
| `comment` | Path of the Markdown body. |
| `plan` | Path of the changeset JSON. |
| `sarif` | Path of the SARIF, for `github/codeql-action/upload-sarif`. |
| `directory` | Directory holding all of them, for `actions/upload-artifact`. |
| `diagrams` | The diagrams that were drawn, comma-separated. |
| `verdict` | `passed`, `warned`, `failed` or `broken`. |
| `changed` | `'true'` when the change touches the network at all. |
| `new-errors` | Errors this change introduced that the base did not have. |
| `new-findings` | The same for every severity. |
| `failed` | `'true'` when there is at least one new error, or the head does not load. |

## Notes

**Only new problems fail it.** The check compares the head's findings with the
base's, by the same fingerprint code scanning tracks alerts by, so a repository
carrying a legacy warning can adopt this without a red baseline. See
[`docs/ci.md`](../../../docs/ci.md#only-new-problems-fail-the-check).

**Fork pull requests.** This action runs happily on a fork's pull request — it
only reads. What cannot happen there is the *posting*: GitHub hands a fork's
`pull_request` run a read-only token whatever `permissions:` says. The reusable
workflow handles that by writing the whole review to the job summary instead.
Do not reach for `pull_request_target` to get around it; see the header comment
of [`netviz-review.yml`](../../workflows/netviz-review.yml) for why.

**`python` must already be on `PATH`** — use `actions/setup-python` before this
action, as the example does. That is deliberate: the action does not choose an
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

