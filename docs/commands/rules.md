# `netgraph rules`

List the validation rules, their severity and their schema aliases. The table is
printed from `netgraph.rules.RULES` — the same source the validator reads — so it
always describes the build you are running rather than the build whose
documentation you happen to have open. It needs no inventory and takes no options.

## Synopsis

<!-- generated: synopsis rules -->
```text
netgraph [GLOBAL OPTIONS] rules [OPTIONS]
```
<!-- /generated -->

## Why you would ask

`netgraph rules` is the **vocabulary** for every place a rule can be named:
`--disable` on a command line, `ignore` and `[validate.severity]` in
`netgraph.toml`, and the `netgraph/ignore` annotation on an element. Two questions
it answers directly:

* *A finding says `W113` — what is that, and what is it called in the
  specification?* The row gives the one-line summary and the `NG-*` alias, and
  either spelling works in every suppression mechanism.
* *What could this inventory possibly be told about?* Reading the sixty-three
  summaries end to end takes a couple of minutes and is a surprisingly good way to
  find out what netgraph considers worth knowing about a network.

For the *why* behind a rule — what it exempts, what it costs to ignore, which
element to annotate — go to its section in
[`docs/validation-rules.md`](../validation-rules.md). This command is the index;
that page is the text.

Shell completion knows the same list, so `netgraph validate --disable <TAB>`
offers it without your having to run this command at all — `netgraph completion
bash|zsh|fish` prints the script that installs it.

## What it prints

Four columns: the short id, the default severity, the `NG-*` aliases and the
summary. The order is the report order of the catalogue — errors, then warnings,
then infos, each numbered in the order they were added.

<!-- run: -->
```console
$ netgraph rules
RULE  SEVERITY  ALIASES           SUMMARY
...
E001  error     NG-C002, NG-C003  A cable endpoint references an unknown device or interface.
E002  error     NG-C005           An interface is terminated by more than one cable.
...
W101  warning   NG-I013           An interface has neither IPv4 nor IPv6 and is not a switchport.
...
I003  info      NG-T015           A tunnel listens on a port other than the registered one for its type.
```

Two things the table does not say, both deliberate:

* **The severity is the default**, not necessarily the one your tree uses.
  `[validate.severity]` in [`netgraph.toml`](../configuration.md#validate--how-findings-are-graded)
  re-grades any of these, and `--strict` promotes every warning to an error.
  `netgraph config show` prints what actually resolved for an inventory, and
  [where it came from](../configuration.md#seeing-what-resolved-and-why).
* **The letter of the id is history**, not state. `W` means the rule was *first*
  assigned `warning`; a rule keeps its id when an inventory re-grades it, because
  ids are permanent and a suppression written today has to keep meaning what it
  meant.

Only the semantic rules listed here can be disabled or re-graded. The loading and
schema constraints have `NG-*` ids too — they appear in the `RULE` column of a
report as `NG-D005` or `load` — but they are not suppressible and so are not part
of this vocabulary. See [Pass 2 — schema](../validation-rules.md#pass-2--schema).

## Arguments

<!-- generated: arguments rules -->
*Takes no positional arguments.*
<!-- /generated -->

## Options

<!-- generated: options rules -->
*No options of its own; the global options apply.*
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The table was printed. |
| `2` | Usage error — an unknown option. |
| `141` | The downstream end of a pipe closed first. |

## See also

* [`docs/validation-rules.md`](../validation-rules.md) — one section per rule:
  why it matters, what it exempts, and how to suppress it.
* [`docs/validation.md`](../validation.md) — the three passes, severities, and the
  four ways to silence a finding.
* [`netgraph validate`](validate.md) — the command that reports these rules.
* [`docs/configuration.md`](../configuration.md#validate--how-findings-are-graded) —
  `ignore` and `[validate.severity]` in `netgraph.toml`.
