# `netviz fmt`

Rewrite inventory YAML in its one canonical form — two-space indent, keys in
schema order, one quoting rule, comments and blank lines untouched. The way
`gofmt` and `ruff format` do it for code, so that how a file is laid out is never
what a review is spent on.

[`docs/format.md`](../format.md) defines the canonical form clause by clause,
including what `fmt` deliberately will not do — it canonicalises documents, it
does not repair them. This page is the reference for the command.

## Synopsis

<!-- generated: synopsis fmt -->
```text
netviz [GLOBAL OPTIONS] fmt [OPTIONS] [PATHS]...
```
<!-- /generated -->

## The three modes

**In place** is the default, and the only mode that touches the disk. With no
`PATHS` the global `-i`/`--inventory` decides what is formatted; otherwise each
path may be a folder to walk or a single YAML file, and the same file reached
through two paths is formatted once. Each file is written through a temporary
file in the same directory and then renamed, so an interrupted run leaves either
the old file or the new one and never half of either. It exits 0 when it worked,
whether or not anything changed — the same as `gofmt -w`.

**`--check`** is the CI mode. It writes nothing, lists the files that are not
canonical on stdout, one per line, and exits 1 if there are any. The list is
stdout and the tally is stderr, so `netviz fmt --check | xargs $EDITOR` opens
the files and nothing else.

<!-- run: -->
```console
$ netviz fmt --check examples/campus
0 file(s) would be reformatted, 19 already formatted
```

**`--diff`** writes nothing and prints a unified diff of what would change, and
exits 1 if there is one, so it is usable as a gate too. Paths below the working
directory get git's `a/`/`b/` prefixes, which makes the output a patch you can
feed to `git apply`.

`--check` and `--diff` cannot be combined; asking for both is a usage error.

**`--stdin`** (or the path `-`) reads a YAML stream from stdin and writes the
formatted stream to stdout. Nothing else is printed on success. Discovery does not
apply — a stream is not a file and has no ignore rules — and neither does
in-place rewriting, so this is the mode an editor's "format buffer" command wants.

<!-- norun: shell redirections and a pipeline; the output paths are illustrative -->
```bash
netviz fmt                       # rewrite the inventory -i points at
netviz fmt inventory devices/    # rewrite these paths
netviz fmt --check inventory     # write nothing; exit 1 and list what differs
netviz fmt --diff inventory      # write nothing; print a unified diff
netviz fmt --diff examples | git apply -R
netviz fmt --stdin < devices/sw.yaml > devices/sw.formatted.yaml
```

## Which files are touched

Exactly the files the loader would read, and no others. Discovery is the loader's,
so `.netvizignore` and the dot- and underscore-prefix rules apply exactly as
they do to [`validate`](validate.md) — all of
[`docs/schema.md` §2.1](../schema.md#21-discovery-rules):

* only `*.yaml` and `*.yml`, compared case-insensitively (`NV-L001`);
* nothing under a path component starting with `.` or `_` (`NV-L002`);
* nothing a `.netvizignore` excludes (`NV-L006`).

That is a deliberate limit rather than an incidental one. A file the inventory
ignores may not be netviz YAML at all, and rewriting it would be the formatter
exceeding its remit. A path named outright on the command line is still subject to
the ignore rules of the tree it sits in.

## Formatting never changes what a document means

Every file is read back with the same strict loader `validate` and `render` use,
and compared against what it said before — as its validated model where the
document validates, and as its raw parsed data where it does not, because `fmt`
has to work on files `validate` rejects and still may not change what they say. A
file that fails that comparison is left exactly as it was, and the failure is
reported as a bug in netviz rather than in the file.

Two more properties come with it. **Comments are preserved**: the whole-line
comments of the output are counted against the input's, and a format that lost one
is refused — model comparison is blind to comments by construction, so without a
check of its own nothing would notice them all disappearing. And **formatting is
idempotent**: running it twice produces the same bytes as running it once, without
which `--check` could fail on a file `fmt` had just written and the two modes
would disagree about what canonical means.

All three are tested over every document under `examples/` and `tests/fixtures/`
on every run of the suite. [Safety](../format.md#safety) has the detail.

## Arguments

<!-- generated: arguments fmt -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[PATHS]...` | no | any number | — |
<!-- /generated -->

## Options

<!-- generated: options fmt -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--check` | — | off | Write nothing; exit 1 listing the files that are not canonical. |
| `--diff` | — | off | Write nothing; print a unified diff of what would change. |
| `--stdin` | — | off | Format the YAML stream on stdin and write it to stdout. Same as the path '-'. |
<!-- /generated -->

`-q`/`--quiet` suppresses the tally without suppressing the file list, the diff,
or any error.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything is canonical, or was made canonical. |
| `1` | `--check` or `--diff` found a file that is not canonical, or some file could not be formatted. |
| `2` | Usage error — including `--check` and `--diff` together. |
| `3` | A path does not exist, or the stream on stdin is not well-formed YAML. |

## See also

* [`docs/format.md`](../format.md) — the canonical form clause by clause, what
  `fmt` will not do, and the pre-commit hooks.
* [`netviz validate`](validate.md) — the checks `fmt` deliberately leaves alone,
  such as a scalar YAML reads as a number.
* [`docs/ci.md`](../ci.md) — `fmt --check` and `validate` in the same job.
* [`docs/inventory-layout.md`](../inventory-layout.md) — discovery, namespaces and
  what belongs in which file.
