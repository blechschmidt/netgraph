# `netgraph review`

`netgraph review` writes up a change to the network the way a reviewer would
want it: **what changed**, **what that broke that was not already broken**, and
**the change drawn**. It is [`netgraph plan`](plan.md),
[`netgraph diff`](diff.md) and [`netgraph validate`](validate.md) reduced to one
Markdown document.

The document is the body of a pull-request comment — that is what it was built
for, and [`docs/ci.md`](../ci.md#workflow-review-a-pull-request) has the action
and the workflow that post it. But it is a command like any other, so the review
of a branch can be read before it is pushed:

<!-- norun: needs a repository with two states to compare, which the docs build has no fixture for -->
```console
$ netgraph --inventory inventory review --from origin/main
```

`netgraph review` writes nothing to the inventory and never talks to a device.

## Contents

- [Synopsis](#synopsis)
- [Where the two sides come from](#where-the-two-sides-come-from)
- [What the document says](#what-the-document-says)
- [Only new problems fail it](#only-new-problems-fail-it)
- [The drawing](#the-drawing)
- [The side documents](#the-side-documents)
- [When the head does not load](#when-the-head-does-not-load)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis review -->
```text
netgraph [GLOBAL OPTIONS] review [OPTIONS]
```
<!-- /generated -->

---

## Where the two sides come from

`--from` is required, because a review is a comparison and there is no sensible
default for "before". Both sides are read exactly as [`netgraph
plan`](plan.md#where-the-two-sides-come-from) reads them: a path that exists as
a directory is a folder, anything else is handed to `git` and exported
read-only.

| Invocation | Before | After |
|---|---|---|
| `review --from origin/main` | the git ref | the working tree |
| `review --from HEAD` | the last commit | the working tree |
| `review --from a --to b` | folder `a` | folder `b` |

Two cases that would be a mistake anywhere else are not mistakes here:

- **The base has no inventory at all.** A repository whose YAML arrived in this
  very change has nothing to compare against. The review says so and treats
  every finding as new, rather than refusing on the first pull request it sees.
- **The base does not load.** A branch that was broken before the change is the
  branch this command exists to review. Its load errors become base findings
  like any other, and the delta subtracts them; the document carries a note that
  anything the rejected documents declared is counted as an addition.

## What the document says

**A verdict line**, so that a reader who reads nothing else has been told
whether to look further:

```text
### ❌ netgraph — 3 elements change, 1 new error introduced
```

**A table of what changed, grouped by kind.** Three cables and a switch is the
shape of a change; the same list sorted by action buries the one switch among
the thirty cables.

| Kind | Added | Changed | Renamed | Removed |
|---|---:|---:|---:|---:|
| `cable` | 3 | - | - | 1 |
| `switch` | - | 1 | - | - |

The elements themselves follow in a collapsed block, one row each, bounded by
`--max-changes` and saying how many it left out.

**The findings this change introduced**, each with its rule linked to the
write-up in [`validation-rules.md`](../validation-rules.md) and its location
linked to the line — when `--repository-url` and `--head-sha` say where the
lines are. Everything that is *not* new is one sentence: how many were fixed,
how many were left alone.

**The drawing.** See [below](#the-drawing).

## Only new problems fail it

Both states are validated, and what the document reports is the difference. An
inventory carrying three legacy warnings is reviewed green on the first change
that touches it; the fourth warning, added by that change, is reported.

Identity is the fingerprint `netgraph validate -F sarif` puts on each result —
the rule, the file, the element, the pointer and the message, and deliberately
**not** the line — so inserting a document above a broken one does not report
everything below it as newly introduced, and the review agrees with GitHub's
code-scanning alert list about which problem is new.

`--strict` and `--disable` are applied to **both** sides. Grading them
differently would report a rule silenced in this very change as a wave of fixes.

## The drawing

`netgraph review` draws nothing itself. It embeds a small **Mermaid** summary of
the changeset — the changed elements, boxed by namespace, coloured by action —
which renders natively in a GitHub comment, and it links whatever
[`netgraph diff`](diff.md) has already written:

<!-- norun: writes files into the reader's directory -->
```console
$ netgraph -i inventory diff --from origin/main -f svg -o diff.svg
$ netgraph -i inventory diff --from origin/main -f png -o diff.png
$ netgraph -i inventory review --from origin/main --diagram diff.svg --diagram diff.png \
      --artifact-url "$RUN_URL#artifacts" --artifact-name netgraph-review
```

`--diagram` takes `[LABEL=]PATH`; the label defaults to the suffix, upper-cased.
A rendering given with `--diagram` alone is **linked**, through
`--artifact-url`. One given with `--diagram-url` is **embedded** as an `<img>`.

The distinction is GitHub's, not netgraph's: a comment body is sanitised, so an
inline `<svg>` element is stripped and an `<img src="data:...">` is refused by
the image proxy. Only a URL a browser can fetch — a published page, an object
store — can be embedded, and a run artifact is a zip behind an authenticated
endpoint. That is why the Mermaid summary is there: it is the one drawing that
always appears.

## The side documents

Three optional files, so that the run which produced the comment also produced
everything else a pipeline wants, from the same load and the same validation:

| Flag | What it writes |
|---|---|
| `--plan-out` | The changeset, byte-identical to `netgraph plan --json`. Not written when the head does not load. |
| `--sarif-out` | The head's findings as SARIF, for `github/codeql-action/upload-sarif`. |
| `--summary-out` | The verdict and the counts, for a workflow step that gates on them. |

`--summary-out` is what a shell should read; nothing should parse the comment.

```json
{
  "schemaVersion": 1,
  "tool": {"name": "netgraph", "version": "0.1.0"},
  "verdict": "failed",
  "broken": false,
  "changed": true,
  "changes": 3,
  "new": {"error": 1, "warning": 1, "info": 1, "total": 3},
  "fixed": 0,
  "carried": 2,
  "baseAbsent": false
}
```

`verdict` is one of `passed` (nothing new is wrong), `warned` (new warnings or
infos, no new errors), `failed` (new errors) or `broken` (the head state does
not load).

## When the head does not load

There is no changeset and no diagram, and the document says so instead of
reporting a change nobody made — a document that was rejected is absent from the
inventory, so diffing against it would read as a deletion. The findings table
still lists every rejected document, which is what a reviewer needs in exactly
that case. The verdict is `broken`, and `--fail-on` exits 1 for it whatever else
it was set to, short of `never`.

## Options

<!-- generated: options review -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--from` | `REF\|DIR` | — | The state before the change: a git ref (origin/main) or a folder. This is the baseline every finding is measured against, so a problem it already had is not reported as one this change introduced. |
| `--to` | `REF\|DIR` | — | The state after the change. Defaults to the inventory as it is on disk. |
| `-o`, `--output` | `FILE` | — | Write the comment body here instead of to stdout. |
| `--plan-out` | `FILE` | — | Also write the changeset as JSON, the document 'netgraph plan --json' writes. |
| `--summary-out` | `FILE` | — | Also write the verdict and the counts as a small JSON document, so a workflow can gate on them without reading the comment's prose. |
| `--sarif-out` | `FILE` | — | Also write the head state's diagnostics as SARIF, for a code-scanning upload. It is the same validation the comment reports, so the two cannot disagree. |
| `--diagram` | `[LABEL=]PATH` | — | A rendering of the visual diff to link, as 'netgraph diff -o' wrote it. The label defaults to the suffix, upper-cased. Repeatable. |
| `--diagram-url` | `[LABEL=]URL` | — | Where a rendering is published, if it is somewhere a browser can fetch it. Those are embedded in the comment; a --diagram with no URL is only linked. Repeatable. |
| `--artifact-url` | `URL` | — | Where the whole bundle can be downloaded -- in CI, the run that produced it. |
| `--artifact-name` | `NAME` | — | What that bundle is called, for the link text. |
| `--repository-url` | `URL` | — | Base URL of the repository, https://github.com/owner/repo. With --head-sha it turns every finding into a permalink to the line it is anchored at. |
| `--head-sha` | `SHA` | — | Commit the head state is, so the links are permalinks rather than branch links. |
| `--title` | `TEXT` | `netgraph` | Heading of the comment, and the key of its sticky marker. Two inventories reviewed in one repository need two titles, or they will overwrite each other's comment. |
| `--strict` | — | off | Promote every warning to an error, on both sides of the comparison. |
| `--disable` | `RULE` | — | Silence a rule by id, on both sides. Repeatable. |
| `--no-renames` | — | off | Report every rename as a deletion beside a creation rather than as one move. |
| `--max-changes` | `N` | `40` | How many elements the changeset table names before it says how many are left. |
| `--max-findings` | `N` | `25` | The same, for the table of new findings. |
| `--fail-on` | `[new-errors\|new-findings\|changes\|never]` | `new-errors` | What exits 1. 'new-errors' is the one a pull-request check should use: an error the base already had is not this change's to fix, and gating on it would mean no repository with a legacy finding could ever adopt the check. |
<!-- /generated -->

## Exit codes

| Code | When |
|---|---|
| 0 | The review was written, and `--fail-on` is satisfied. |
| 1 | `--fail-on` says so — by default, the change introduced an error the base did not have, or the head state does not load. Also: a ref that cannot be read. |
| 2 | Usage: no `--from`, or an empty `--diagram`. |

The document is written **before** the exit code is decided, so a workflow that
uploads it always has something to upload.

## See also

- [`netgraph plan`](plan.md) — the changeset on its own, as text or JSON.
- [`netgraph diff`](diff.md) — the changeset as a picture.
- [`netgraph validate`](validate.md) — the findings on their own, in four formats.
- [`docs/ci.md`](../ci.md#workflow-review-a-pull-request) — the action, the reusable workflow, and the sticky comment.
- [`docs/validation-rules.md`](../validation-rules.md) — what each rule id means.
