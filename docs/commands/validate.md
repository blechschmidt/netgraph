# `netgraph validate`

Check the inventory for schema and semantic problems. It exits 1 when anything is
reported as an error and 0 otherwise — so it drops straight into CI, into a
pre-commit hook, or into a `make check` without any wrapping. Nothing is probed
and no device is contacted: every finding comes from the files.

This page is the reference for the command. [`docs/validation.md`](../validation.md)
is the treatment of the three passes, severities and suppression, and
[`docs/validation-rules.md`](../validation-rules.md) is the catalogue of every
rule.

---

## Contents

- [Synopsis](#synopsis)
- [A clean inventory](#a-clean-inventory)
- [An inventory with a problem](#an-inventory-with-a-problem)
- [Warnings, `--strict` and `--disable`](#warnings---strict-and---disable)
- [Machine-readable output](#machine-readable-output)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)

---

## Synopsis

<!-- generated: synopsis validate -->
```text
netgraph [GLOBAL OPTIONS] validate [OPTIONS]
```
<!-- /generated -->

## A clean inventory

Nothing to say is said in one line, and the exit code is 0:

<!-- run: -->
```console
$ netgraph -i examples/quickstart validate
no problems found
```

There is no `--summary` and no verbosity level that turns this into a report of
what was checked. If you want to know what the tool would have complained about,
[`netgraph rules`](rules.md) prints the whole vocabulary.

## An inventory with a problem

Findings are grouped by severity, most severe first, and each line reads
`file.yaml#doc:line  RULE  message`. The repository keeps one deliberately broken
fixture per rule, each producing exactly one finding, which makes them the
smallest honest examples available:

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/invalid/e001-unknown-endpoint.yaml validate
errors (1):
  e001-unknown-endpoint.yaml#1:19  E001  cable 'cbl-dangling' endpoint pc-ghost:eth0: no element named 'pc-ghost' is declared in this inventory

1 error
```

`#1:19` is the second document of the file (documents are counted from 0, across
`---` separators) beginning at line 19. `E001` is the id you would name to
suppress the rule, and the message lists every element the finding involves —
here the cable and the endpoint that resolves to nothing.
[How to read a finding](../validation.md#how-to-read-a-finding) takes the line
apart column by column.

`-i` accepts a single YAML file as well as a directory, which is what makes the
invocation above possible. In an inventory tree the location is relative to the
root `-i` names.

## Warnings, `--strict` and `--disable`

A warning is legal-but-probably-wrong, and it does **not** fail the run:

<!-- run: -->
```console
$ netgraph -i tests/fixtures/invalid/w103-orphan-device.yaml validate
warnings (1):
  w103-orphan-device.yaml#0:6  W103  device 'pc-a' terminates no cable and hosts no adapter; it is drawn as an isolated node

1 warning
```

`--strict` promotes every surviving warning to an error, so any finding fails the
run. This is the setting for a pipeline, where a warning nobody has to look at is
a warning nobody looks at:

<!-- run: rc=1 -->
```console
$ netgraph -i tests/fixtures/invalid/w103-orphan-device.yaml validate --strict
errors (1):
  w103-orphan-device.yaml#0:6  W103  device 'pc-a' terminates no cable and hosts no adapter; it is drawn as an isolated node

1 error
```

`--strict` can only turn strictness *on*: `strict = true` in `netgraph.toml` makes
it the default for a tree and no command-line flag undoes that.

`--disable RULE` silences a rule by id. It is repeatable, accepts the short id or
either `NG-*` alias, and accepts `*` for all of them. It **adds** to whatever
`netgraph.toml` already ignores and cannot re-enable a rule the file disabled. The
two flags combine in the obvious way — be strict about everything except the
exceptions you have decided are exceptions:

<!-- run: -->
```console
$ netgraph -i tests/fixtures/invalid/w103-orphan-device.yaml validate --strict --disable W103
no problems found
```

Only the pass-3 semantic rules can be disabled. Naming a schema rule is a usage
error rather than a flag that quietly applies to nothing, and the message lists
what you could have meant instead:

<!-- run: rc=2 -->
```console
$ netgraph -i examples/quickstart validate --disable NG-D005
error: --disable: 'NG-D005' is not a known rule id; expected one of E001, E002, E003, E004, E005, E006, E007, E008, E009, E010, E011, E012, E013, E014, E015, E016, E017, E018, E019, E020, E021, E022, E023, E024, E025, E026, E027, E028, E029, E030, E031, E032, E033, E034, E035, E036, W101, W102, W103, W104, W105, W106, W107, W108, W109, W110, W111, W112, W113, W114, W115, W116, W117, W118, W119, W120, W121, W122, W123, W124, W125, W126, W127, W128, W129, W130, W131, W132, W133, W134, W135, W136, I001, I002, I003, an NG-* alias from docs/schema.md §10, or '*'
```

A suppression that belongs to the inventory rather than to one command line
belongs in `netgraph.toml` or in an element's `netgraph/ignore` annotation —
[Saying "not here"](../validation.md#saying-not-here-the-four-suppressions) sets
the four mechanisms side by side, and
[Suppressing a rule](../validation-rules.md#suppressing-a-rule) gives each one in
full.

## Machine-readable output

`-F, --output-format` is `text` to read; `json`, `sarif` or `github` for
automation. The three structured formats put their document on **stdout** and move
the human summary to **stderr**, so the output stays pipeable; `--quiet` drops
that summary and never the document.

* `json` is a documented envelope — tool, inventory root, per-severity counts, a
  `failed` flag and one object per finding with its rule, alias, severity,
  message, element, file, line, column, JSON pointer and help link.
* `sarif` is SARIF 2.1.0, which `github/codeql-action/upload-sarif` and any SARIF
  viewer accept. The rule metadata carries a `helpUri` into
  `docs/validation-rules.md`.
* `github` emits workflow commands that annotate a pull request in place.

<!-- norun: the first line redirects, and the second needs a GitHub Actions log to annotate -->
```bash
netgraph -i inventory validate -F sarif --strict > netgraph.sarif
netgraph -i inventory validate -F github
```

Text output locates a finding at its *document*; the structured formats carry the
line and column of the offending value, which costs a little memory and is why
only they pay for it.

[`docs/ci.md`](../ci.md) documents all three envelopes key by key, plus the
composite GitHub Action and the pre-commit hook this repository ships.

## Arguments

<!-- generated: arguments validate -->
*Takes no positional arguments.*
<!-- /generated -->

## Options

<!-- generated: options validate -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--strict` | — | off | Promote every warning to an error, so any finding fails the run. |
| `--disable` | `RULE` | — | Silence a rule by id (E001, NG-C002, ...). Repeatable. |
| `-F`, `--output-format` | `[text\|json\|sarif\|github]` | `text` | text is for reading; json, sarif and github are for CI. |
<!-- /generated -->

The global options apply as everywhere else: `-i/--inventory` names the tree,
`-q/--quiet` silences the commentary under the structured formats, and `-v` says
where the inventory was loaded from, how many elements it holds and how many
findings the validator produced.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No errors. Warnings and infos may still have been reported. |
| `1` | At least one **error**, or a document that could not be loaded at all. |
| `2` | Usage error — an unknown option, an unknown rule id in `--disable` — or an unusable `netgraph.toml`. |
| `3` | The inventory could not be discovered or read at all. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

A structured document is written for `0` and `1`. It is not written for the rest:
those are failures to run the check rather than results of it.

## See also

* [`docs/validation.md`](../validation.md) — the three passes, severities,
  suppression and the index of every rule.
* [`docs/validation-rules.md`](../validation-rules.md) — one section per rule, why
  it exists and how to switch it off.
* [`netgraph rules`](rules.md) — the vocabulary `--disable` accepts, printed from
  the build you are running.
* [`docs/ci.md`](../ci.md) — the JSON and SARIF envelopes, the GitHub Action and
  the pre-commit hook.
