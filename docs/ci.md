# netviz in CI

`netviz validate` is built to be a gate. It loads the tree, applies every rule
in [`docs/validation-rules.md`](validation-rules.md), prints what it found and
exits non-zero when anything is an error — so the shortest useful pipeline is
one line:

<!-- run: cwd=examples/quickstart -->
```console
$ netviz --inventory . validate --strict
no problems found
```

`netviz test` is the second gate, and it answers a different question — see
[below](#netviz-test-assertions-as-a-gate).

This page covers the rest: the machine-readable output formats, the three
composite GitHub Actions, the reusable workflows that publish a diagram and
review a pull request, the pre-commit hook, and complete workflows for the ways
findings can reach a pull request.

* [Output formats](#output-formats)
* [Exit codes](#exit-codes)
* [The JSON envelope](#the-json-envelope)
* [SARIF and code scanning](#sarif-and-code-scanning)
* [Inline annotations](#inline-annotations)
* [The GitHub Action](#the-github-action)
* [Workflow: upload SARIF](#workflow-upload-sarif)
* [Workflow: annotate the diff](#workflow-annotate-the-diff)
* [The render action](#the-render-action)
* [Workflow: publish the diagram to GitHub Pages](#workflow-publish-the-diagram-to-github-pages)
* [The review action](#the-review-action)
* [Workflow: review a pull request](#workflow-review-a-pull-request)
* [`netviz test`: assertions as a gate](#netviz-test-assertions-as-a-gate)
* [Workflow: a scheduled drift check](#workflow-a-scheduled-drift-check)
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
`netviz validate -F sarif > report.sarif` writes a file that uploads cleanly
while a person watching the run still sees what happened.

**`--quiet` silences the summary, never the document.** `-q` is about the
commentary; a pipeline that redirects stdout gets the same bytes with and
without it.

**The order is deterministic**: file, then line, then rule id. Two runs over an
unchanged inventory produce byte-identical output, so a report can be committed
and diffed. Every format honours `--strict` and `--disable`, and re-grading a
rule in `netviz.toml` changes the `severity` field, the SARIF `level` and the
workflow command alike.

`--strict` and `--disable` behave exactly as they do for `text`:

<!-- norun: redirects stdout into a file in the reader's directory -->
```console
$ netviz -i inventory validate -F sarif --strict --disable W105 --disable I002 > netviz.sarif
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No errors. Warnings and infos may still have been reported. |
| `1` | At least one **error**, or a document that could not be loaded. |
| `2` | Usage error — an unknown option, an unknown rule id in `--disable` — or an unusable `netviz.toml`. |
| `3` | The inventory path does not exist or cannot be read at all. |

A structured document is written for `0` and `1`. It is *not* written for the
rest: those are failures to run the check, not results of it. The full table is
in the [command reference](commands/README.md#exit-codes).

## The JSON envelope

<!-- norun: the envelope below is from an inventory with a broken cable, at an illustrative path -->
```console
$ netviz -q -i inventory validate -F json
```

```json
{
  "schemaVersion": 1,
  "tool": { "name": "netviz", "version": "0.1.0" },
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
| `tool.version` | string | The netviz release that produced the report. |
| `inventory.root` | string | Absolute path of the directory `findings[].file` is relative to. |
| `inventory.prefix` | string | The same directory relative to the working directory, or `""`. Prepend it to `file` to get a repository-relative path. |
| `summary` | object | One count per severity, plus `total`. Every severity is present, zero included. |
| `failed` | boolean | Would the run have failed? Equivalent to `summary.error > 0`. |

Each entry of `findings`:

| Key | Type | Meaning |
|---|---|---|
| `rule` | string | Canonical rule id (`E001`), or `load` for a problem the loader or the schema rejected the document over. |
| `alias` | string \| null | The `NG-*` identifier from [the schema](schema.md) §10. |
| `severity` | string | `error`, `warning` or `info`, **after** `netviz.toml`, `netviz/ignore`, `--disable` and `--strict`. |
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

<!-- norun: a shell pipeline into jq -->
```console
$ netviz -q -i inventory validate -F json \
    | jq -r '.findings | group_by(.rule) | map({rule: .[0].rule, n: length}) | .[] | "\(.n)\t\(.rule)"'
```

## SARIF and code scanning

`-F sarif` emits a [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/)
log with exactly one run. It is validated against the official schema in
netviz's own test suite, so the format cannot drift.

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

[`.github/actions/netviz-validate`](../.github/actions/netviz-validate/) is
a composite action that installs netviz, runs the check and reports the
result. Its own [README](../.github/actions/netviz-validate/README.md) has the
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

      - id: netviz
        uses: blechschmidt/netgraph/.github/actions/netviz-validate@v0.1.0
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
          sarif_file: ${{ steps.netviz.outputs.report }}
          # Keeps netviz's alerts separate from any other analysis in the repo.
          category: netviz

      - name: Fail the job if the inventory did not validate
        if: steps.netviz.outputs.failed == 'true'
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

      - uses: blechschmidt/netgraph/.github/actions/netviz-validate@v0.1.0
        with:
          inventory: inventory
          output-format: github
          strict: "true"
          # Two rules an inventory under construction legitimately trips.
          disable: W105,I002
```

Without the action, the same thing by hand:

```yaml
      - run: pip install netviz
      - run: netviz --inventory inventory validate --output-format github --strict
```

## The render action

Everything above answers "did this change break the inventory?" The other half
of a pipeline is "what does the network look like now?" — and the answer nobody
reads is the one they have to check out a repository and install Graphviz to see.

[`.github/actions/netviz-render`](../.github/actions/netviz-render/) installs
netviz *and* Graphviz, runs [`netviz render`](commands/render.md), and leaves
the diagram on disk. Its own
[README](../.github/actions/netviz-render/README.md) has the input and output
tables. Three things are worth knowing before reading them.

**`html` is the default format**, and it is the only one that is publishable on
its own: one file, no CDN, no stylesheet to serve beside it, with the layer
switcher, the search box and every element's detail already in it — see
[rendering.md](rendering.md#the-interactive-html-page). `format:` takes the
other six all the same — `svg` and `dot` for something to embed, `png` and `pdf`
for something to print, `mermaid` for a Markdown page, `json` for a step of your
own.

**Graphviz is installed for you**, because it is not a Python dependency and a
missing `dot` is otherwise a failure at the last step of a job that has already
done all the work. `graphviz: auto` — the default — installs it only when the
chosen format needs a layout and the runner does not already have one, so a
self-hosted image that ships Graphviz is left alone, and `mermaid`, `dot` and
`json` never pay for it.

**A diagram that was not drawn fails the step.** The file is checked for the
shape of the format that was asked for, so an empty or truncated page fails in
the job that produced it rather than on the site it was published to.

```yaml
      - id: diagram
        uses: blechschmidt/netgraph/.github/actions/netviz-render@main
        with:
          inventory: inventory
          format: svg
          args: --icons cisco --collapse-depth 1

      - uses: actions/upload-artifact@v4
        with:
          name: topology
          path: ${{ steps.diagram.outputs.file }}
```

## Workflow: publish the diagram to GitHub Pages

A rendered page in an artifact is a page somebody has to download and unzip.
[`netviz-pages.yml`](../.github/workflows/netviz-pages.yml) is a **reusable
workflow** that takes the same render and publishes it, so the inventory
repository grows a live diagram of its own network at a URL — rebuilt from the
YAML on every push, and therefore never the stale export somebody drew in
draw.io eighteen months ago.

```yaml
name: diagram

on:
  push:
    branches: [main]
  pull_request:

jobs:
  diagram:
    permissions:
      contents: read
      # Wanted by the deploy job of the called workflow. A reusable workflow
      # cannot hold more than its caller grants, so the grant is here.
      pages: write
      id-token: write
    uses: blechschmidt/netgraph/.github/workflows/netviz-pages.yml@main
    with:
      inventory: inventory
      # Three layers, one page: the switcher is in the HTML.
      layer: l1 l2 l3
      title: ${{ github.repository }}
      strict: true
      # A pull request renders — that is the gate — and publishes nothing.
      deploy: ${{ github.event_name != 'pull_request' }}
```

Turn Pages on for the repository first, with **GitHub Actions** as the source
(*Settings → Pages → Build and deployment*). The URL the page landed at comes
back as the `page-url` output, and shows up on the run's deployment.

### `runs-on` is an input

```yaml
    with:
      runs-on: '["self-hosted", "linux", "network"]'
```

The network that has an inventory worth drawing is often the one where a
GitHub-hosted runner is not allowed anywhere near the repository. `runs-on`
takes a single label (`ubuntu-latest`, `self-hosted`), a JSON array of labels, or
a JSON runner-group object — anything the `runs-on:` key itself accepts — and
both jobs use it.

On a runner image that has no Python tool cache, set `python-version: ""` to skip
`actions/setup-python` and use the interpreter that is already there; on one that
ships Graphviz, `graphviz: "false"` skips the install.

### The rest of the inputs

| Input | Default | Meaning |
|---|---|---|
| `runs-on` | `ubuntu-latest` | Where both jobs run: a label, a JSON array of labels, or a runner group. |
| `inventory` | `.` | Root folder of the YAML tree, or a single YAML file. |
| `layer` | *(empty)* | Layers to draw, separated by commas or whitespace. Several become one page with a switcher. |
| `title` | *(empty)* | Caption for the diagram. |
| `theme` | *(empty)* | `blueprint`, `mono`, `none`, or a path to a `kind: theme` document. |
| `args` | *(empty)* | Further `netviz render` flags, split on whitespace. |
| `strict` | `false` | Treat validation warnings as errors and publish nothing. |
| `page` | `index.html` | Name of the page inside the published site. |
| `python-version` | `3.12` | Interpreter to set up. Empty uses the runner's own. |
| `version` | *(empty)* | netviz to install, as a pip requirement. |
| `ref` | *(empty)* | Branch, tag or SHA of the calling repository to render. |
| `graphviz` | `auto` | Install `dot`: `auto`, `true` or `false`. |
| `deploy` | `true` | Publish. `false` builds and uploads the artifact and stops. |
| `environment` | `github-pages` | Deployment environment. |

Only the deploy job holds `pages: write` and `id-token: write`; the job that
renders runs read-only, so a workflow that fails to draw the inventory cannot
have reached the Pages API to publish anything. And because `netviz render`
validates before it draws, an inventory with a dangling cable stops the
deployment rather than publishing a diagram that misrepresents the network. A
tree still under construction opts out of that with `args: --force`, which is
also the honest way to spell it: the page is published *despite* the inventory
not validating.

**Several inventories, several pages** is a matrix in the caller, one call per
inventory, each writing a different `page:` — but Pages publishes one artifact
per deployment, so that shape wants a job of your own that renders each with the
action above, assembles the site, and deploys it once.

## The review action

Everything above is about one state of the inventory: does it load, does it
cohere, what does it look like. A pull request asks a different question, and it
is the one a reviewer actually has — **what does this change do?** A green check
on a branch that rewires the core answers it no better than a green check on a
typo fix.

[`.github/actions/netviz-review`](../.github/actions/netviz-review/) answers
it. Given a base and a head it produces four things from machinery that already
exists — [`netviz plan`](commands/plan.md),
[`netviz diff`](commands/diff.md) and
[`netviz validate`](commands/validate.md) — and one that is new,
[`netviz review`](commands/review.md), which is the three of them written up as
one Markdown document:

| File | What it is |
|---|---|
| `comment.md` | The review, as a comment body. Its first line is the sticky marker. |
| `plan.json` | The typed changeset, exactly as `netviz plan --json` writes it. |
| `netviz.sarif` | The head's findings, for `github/codeql-action/upload-sarif`. |
| `summary.json` | The verdict and the counts, for a step that gates on them. |
| `diff.svg`, `diff.png` | The visual diff — additions green, removals red and dashed but still in place, changes amber. |

`netviz review` is a command like any other, so the same review can be read
before anything is pushed:

<!-- norun: needs a repository with two states to compare, which the docs build has no fixture for -->
```console
$ netviz --inventory inventory review --from origin/main
```

## Workflow: review a pull request

[`netviz-review.yml`](../.github/workflows/netviz-review.yml) is the
reusable workflow around it. It runs the action, uploads the bundle, uploads the
SARIF, and posts **one** comment that it edits in place on every push:

```yaml
name: review

on:
  pull_request:

jobs:
  review:
    permissions:
      contents: read
      pull-requests: write
      security-events: write
    uses: blechschmidt/netgraph/.github/workflows/netviz-review.yml@main
    with:
      inventory: inventory
```

### What the comment says

A verdict line first, so that a reader who reads nothing else has still been
told whether to look: *3 elements change, 1 new error introduced*. Then a table
of what is added, changed, renamed and removed, **grouped by kind** — three
cables and a switch is the shape of a change; the same list sorted by action
buries the one switch among the thirty cables. The elements themselves are one
`<details>` below it, bounded, saying how many it left out.

Then the findings this change *introduced*, each linked to the line it is
anchored at in the head commit, with the pre-existing ones counted in a sentence
and no more. Then the drawing.

**The drawing appears three ways**, in descending order of availability, because
GitHub sanitises the HTML in a comment: an inline `<svg>` element is stripped and
an `<img src="data:...">` is refused by the image proxy, so neither of the two
obvious ways of embedding a rendered diagram works at all.

1. A **Mermaid** summary of the changeset — the changed elements, boxed by
   namespace and coloured by action. GitHub renders Mermaid in a comment
   natively, so this one always appears, needs nothing hosted, and costs no
   Graphviz.
2. Any **published** rendering, embedded as an `<img>`. Pass `diagram-url` when
   the SVG is somewhere a browser can fetch it — a Pages site, an object store.
3. The **full diagram**, linked. The SVG and the PNG are uploaded as a run
   artifact, which is a zip behind an authenticated endpoint and so can only ever
   be linked, never embedded.

### Only new problems fail the check

The check compares the head's diagnostics with the **base's**, and fails on the
difference. An inventory carrying three legacy warnings goes green on the first
pull request that touches it; the fourth warning, added by that pull request,
goes red.

This is the whole reason the bot is adoptable. A gate that failed on the
absolute count would be turned off within a day by the first team whose network
has grown a wart nobody has time to fix this quarter — and a gate that is off
catches nothing at all.

Identity is [`netviz.diagnostics.fingerprint`](../src/netviz/diagnostics.py):
the rule, the file, the element, the pointer and the message, and deliberately
**not** the line. Inserting a document above a broken one does not report
everything below it as newly introduced. It is the same fingerprint code
scanning tracks alerts by, so the comment and the alert list cannot disagree
about which problem is new.

Two states that are graded differently would produce nonsense, so `strict:` and
`disable:` are applied to **both** sides: a rule silenced in this very pull
request reads as nothing changing rather than as a wave of fixes.

### `pull_request`, never `pull_request_target`

The workflow is written for the `pull_request` trigger. That trigger checks out
the merge result and hands the job a token that is **read-only** whenever the
pull request comes from a fork — and that is the whole of the defence, because
everything the job does is read YAML the pull request wrote.

`pull_request_target` would run the base branch's workflow with a *writable*
token while the untrusted head is one `git checkout` away. Every published escape
from that pattern has the same shape: a step reads a file the pull request
controls, and the token it is holding can push to the default branch. netviz
parses the head's YAML by design, so that is exactly the trigger it must not be
run under.

**A fork's pull request therefore cannot be commented on.** The workflow
degrades rather than failing:

| | Same-repository branch | Fork |
|---|---|---|
| The review is produced | ✅ | ✅ |
| Written to the job summary | ✅ | ✅ |
| Posted as a comment | ✅ | skipped, with a note in the log |
| Uploaded to code scanning | ✅ | skipped, with a note in the log |
| Fails on a new error | ✅ | ✅ |

The job summary needs no permission at all, so a maintainer reading the run of a
fork's pull request sees exactly what a comment would have carried. The check
still goes red on a new error, which is what a branch protection rule is
watching.

### The comment is sticky

The first line of the body is an HTML comment — `<!-- netviz-review: TITLE -->`
— and the workflow looks for it among the pull request's comments before it
decides whether to post or to edit. A branch pushed twenty times has one review
comment, showing the twentieth state.

`title:` keys that marker, which is what lets one repository review two
inventories: give each its own title, and each keeps its own comment. Give them
the same title and the second job will overwrite the first job's comment.

### The inputs

| Input | Default | Meaning |
|---|---|---|
| `runs-on` | `ubuntu-latest` | Where the job runs: a label, a JSON array of labels, or a runner group. |
| `inventory` | `.` | Root folder of the YAML tree, or a single YAML file. |
| `base` | *(empty)* | What to compare against. Empty is the pull request's base commit. |
| `title` | `netviz` | Heading of the comment, and the key of its sticky marker. |
| `layer` | *(empty)* | Which layer the diff is drawn at. One only — a diff compares one view. |
| `theme` | *(empty)* | `blueprint`, `mono`, `none`, or a path to a `kind: theme` document. |
| `args` | *(empty)* | Further `netviz diff` flags, split on whitespace. |
| `formats` | `svg,png` | Image formats to draw. Empty draws none and needs no Graphviz. |
| `strict` | `false` | Promote every warning to an error, on both sides. |
| `disable` | *(empty)* | Rule ids to silence on both sides. |
| `comment` | `true` | Post the review. `false` writes it to the job summary only. |
| `upload-sarif` | `true` | Upload the head's findings to code scanning. |
| `sarif-category` | `netviz` | One per inventory: uploads sharing a category replace each other. |
| `fail-on-new-errors` | `true` | Fail on an error the base did not have. Pre-existing errors never fail it. |
| `artifact-name` | `netviz-review` | Name of the uploaded bundle. |
| `artifact-retention-days` | `0` | How long to keep it. `0` leaves the repository default. |
| `python-version` | `3.12` | Interpreter to set up. Empty uses the runner's own. |
| `version` | *(empty)* | netviz to install, as a pip requirement. |
| `graphviz` | `auto` | Install `dot`: `auto`, `true` or `false`. |

The outputs — `verdict`, `changed`, `new-errors` and `comment-url` — are there
for a caller that wants to do something else with the answer: label the pull
request, gate a deployment, fan out to a chat channel.

**Several inventories** is a matrix in the caller, one call per inventory, each
with its own `title`, `sarif-category` and `artifact-name`. That is how this
repository reviews its own examples — see
[`.github/workflows/review.yml`](../.github/workflows/review.yml), which is the
dogfooding of everything above.

**Without a pull request**, the workflow still produces the review: give it a
`base` and call it from a `workflow_dispatch`, and everything lands in the job
summary. That is the same path a fork's pull request takes, so it is worth
having a way to run it deliberately rather than discovering how it reads the
first time a fork turns up.

## `netviz test`: assertions as a gate

`validate` answers "do these files cohere?" Every rule it applies is a statement
about inventories *in general* — a cable endpoint resolves, an address is inside
its subnet — which is exactly why none of them can say that the ward switch must
not be the only path to the ward. That is a fact about *this* network, known only
to the people who built it.

[`netviz test`](commands/test.md) grades the facts they wrote down. A
`kind: testsuite` document ([§20](schema.md#20-test-suites-executable-assertions))
holds named assertions — reachable, not-reachable, same VLAN, within a prefix,
unique management addresses, no single point of failure — and the command exits 1
when one of them has stopped being true. It probes nothing and contacts no
device, so it belongs on a pull request beside `validate` rather than on a
schedule:

```yaml
      - run: pip install netviz
      - run: netviz --inventory inventory validate --strict
      - run: netviz --inventory inventory test
```

Both gates in one job, in that order: an inventory that does not load cannot be
meaningfully tested, and `test` refuses to try unless `--force` is given — a
dangling cable is exactly the kind of thing that makes an assertion pass for the
wrong reason.

**`-F junit` is what to publish.** One `<testcase>` per assertion, grouped by
suite, each carrying the file and line of the assertion. GitHub, GitLab and
Jenkins all render it natively, so a failure arrives as a named row somebody can
click rather than as a line in a log:

```yaml
      - run: netviz --inventory inventory test -F junit -o test-results.xml
        continue-on-error: true
      - uses: mikepenz/action-junit-report@v5
        if: always()
        with:
          report_paths: test-results.xml
```

`continue-on-error` plus `if: always()` is the usual pair: the step has to fail
the job, and the report has to be published anyway, or the failures are invisible
in the run that had them.

On GitLab the same two lines, and the report is picked up by name:

```yaml
netviz:
  image: python:3.12-slim
  script:
    - pip install netviz
    - netviz --inventory inventory validate --strict
    - netviz --inventory inventory test -F junit -o test-results.xml
  artifacts:
    when: always
    reports:
      junit: test-results.xml
```

**A test run that checked nothing fails.** A `SUITE` argument matching no suite,
and a selector matching no element, are both errors rather than vacuous passes:
a green pipeline that graded zero assertions is the one failure mode a test
suite exists to prevent.

The two bundled examples ship suites — `examples/home-lab/tests.yaml` and
`examples/campus/tests.yaml` — which are the shortest way to see what a useful
assertion looks like.

## Workflow: a scheduled drift check

`validate` answers "is this inventory self-consistent?" — a question about the
files, which a pull request can settle. [`netviz drift`](commands/drift.md)
answers the other one: "is the network still what the files say?" Nothing in a
pull request can settle that, because the network changes when nobody is looking
at the repository. So it belongs on a schedule rather than on a push.

The shape is: collect on the network, compare in the repository, publish the
result. Collection is not netviz's job — it opens no socket and reads no
credential — so a runner with reach into the network, or a cron job on a jump
host that commits its captures, does that half:

```yaml
name: drift

on:
  schedule:
    # 06:00 UTC daily. Drift is a slow signal; hourly buys nothing and wakes
    # people up over a switch that was being replaced at the time.
    - cron: "0 6 * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  drift:
    runs-on: [self-hosted, network]   # a runner that can reach the devices
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install netviz

      # Collect. Your commands, your credentials, netviz's input format.
      - name: Capture the live network
        run: |
          mkdir -p captures
          for host in $(cat hosts.txt); do
            ssh "$host" 'ip -j addr show'  > "captures/$host.addr.json"
            ssh "$host" 'lldpctl -f json' > "captures/$host.lldp.json"
          done

      - name: Compare the capture with the inventory
        run: |
          netviz --inventory inventory drift \
            --exclude-interface 'veth*' --exclude-interface 'docker*' \
            --output-format junit captures/* > drift.xml

      - name: Publish the report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: drift
          path: drift.xml
```

Five things about that step are the point of the exercise.

**`--fail-on drift` is the default**, so the job goes red on any difference and
green otherwise. Use `--fail-on none` when the run only feeds a dashboard and
something else decides what is worth waking somebody for; the JSON envelope's
`drifted` flag and `summary` counts are there for exactly that.

**A partial capture does not fail the job.** Anything a dialect cannot see is
reported as *unobserved* and never counted as drift, so a host that was down at
06:00, or an `ssh` that timed out, produces more blind spots and no more
failures. That is what makes the schedule survivable; the reasoning is in
[drift and unobserved](commands/drift.md#drift-and-unobserved).

**`--exclude-interface` should match how the capture was taken.** Container and
virtual-ethernet interfaces are not part of a physical topology, and without the
pattern they read as interfaces the inventory failed to declare — the same
patterns [`netviz import`](commands/import.md) is given.

**`-F junit` is the format to publish.** One test case per element, so the row
list stays put between runs: a device goes red when it drifts and green when
somebody fixes it, instead of the report growing and shrinking. Every CI system
renders it, and `if: always()` is what makes the artifact survive the failing
step above it.

**Run `validate` first if the tree is not already gated.** `drift` refuses to
compare against an inventory that does not load, because a document the loader
rejected is absent from the comparison and would make every element in it look
like something the network has and the inventory does not.

Elsewhere the same job is the same three lines. GitLab, which renders JUnit
natively in the merge-request widget:

```yaml
# .gitlab-ci.yml
drift:
  image: python:3.12
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
  script:
    - pip install netviz
    - netviz --inventory inventory drift --output-format junit captures/* > drift.xml
  artifacts:
    when: always
    reports:
      junit: drift.xml
```

## pre-commit

netviz ships `.pre-commit-hooks.yaml`, so an inventory repository can run the
same checks before the commit is written. Four hooks are published:

| Hook | What it does |
|---|---|
| `netviz-validate` | Validates the whole tree. Takes no filenames. |
| `netviz-test` | Grades the tree's own assertions. Takes no filenames. |
| `netviz-fmt` | Rewrites the staged files into canonical form. |
| `netviz-fmt-check` | Reports staged files that are not canonical, rewriting nothing. |

Start with validation:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/blechschmidt/netgraph
    rev: v0.1.0
    hooks:
      - id: netviz-validate
        args: [--strict]
```

The hook validates the tree as a whole rather than the staged files — a cable is
only dangling when compared with the devices in the *other* files — so it takes
no filenames, and runs whenever a commit touches any `.yaml`/`.yml` file.

It validates the current directory. For an inventory that does not sit at the
repository root, override `entry`, which is where the global `--inventory`
option has to go:

```yaml
      - id: netviz-validate
        entry: netviz --inventory inventory validate
        args: [--strict]
        files: ^inventory/.*\.ya?ml$
```

### Assertions

`netviz-test` grades the `kind: testsuite` documents the tree declares, so a
commit that quietly removes somebody's second path fails before it lands. Like
`netviz-validate` it takes no filenames — an assertion is about the whole tree
— and it is worth pairing the two:

```yaml
      - id: netviz-validate
        args: [--strict]
      - id: netviz-test
```

The hook is a no-op cost only when the tree declares no suite, and in that case
it fails rather than passing: see
[`netviz test`](commands/test.md#exit-codes). Leave it out until there is
something to assert.

### Formatting

`netviz-fmt` puts the staged YAML into the canonical form of
[`docs/format.md`](format.md). Unlike the hook above it *does* take filenames —
formatting is per-file, whereas a cable is only dangling when compared against
the devices in the other files:

```yaml
      - id: netviz-fmt
```

It rewrites in place and relies on pre-commit noticing the modification, which
fails the commit whatever the exit status. That is the intended loop: the files
come back fixed and `git add` is the whole remedy. Formatting never changes what
a document means — see [Safety](format.md#safety) — but nothing is committed
without being seen.

For a repository that would rather see the failure than have files rewritten
underneath it, `netviz-fmt-check` reports and changes nothing:

```yaml
      - id: netviz-fmt-check
```

Restrict `files` rather than overriding `entry` for an inventory below the
repository root; this hook is given the paths to work on:

```yaml
      - id: netviz-fmt
        files: ^inventory/.*\.ya?ml$
```

The same check belongs in the build, next to whatever formats the code:

```yaml
      - run: pip install netviz
      - run: netviz fmt --check inventory
```

This repository does exactly that for its own `examples/` tree; the step is in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), and it prints a
`netviz fmt --diff` into the log when it fails so the fix is in the build
output rather than only reproducible locally.

## Other CI systems

Nothing above is GitHub-specific except the two output formats named after it.
Anywhere else, the JSON envelope plus the exit code is the whole interface:

```yaml
# .gitlab-ci.yml
validate:
  image: python:3.12
  script:
    - pip install netviz
    - netviz --inventory inventory validate --strict --output-format json > netviz.json
  artifacts:
    when: always
    paths: [netviz.json]
```

SARIF is not GitHub-specific either — GitLab, Azure DevOps and most static
analysis dashboards ingest it — so `-F sarif` is worth trying before writing a
converter for `-F json`.
