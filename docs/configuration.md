# `netgraph.toml`

One optional file at the root of an inventory, holding everything that is true
of *this* network rather than of this invocation: which rules it is graded by,
and what its diagrams look like.

```toml
# netgraph.toml
[validate]
strict = false
ignore = ["W103", "NG-C010"]

[validate.severity]
E004 = "warning"

[render]
layer = "l2"
icons = "cisco"
group-by-namespace = true

[profile.poster]
layer = ["l1", "l2", "l3"]
format = "html"
title = "Campus — every layer"

[profile.review]
collapse-depth = 1
bundle-links = true
show-ips = false
```

It is entirely optional. An inventory without one behaves exactly as if it
declared the defaults, and `netgraph init` scaffolds a fully commented copy.

**Contents**

- [Where the file is looked for](#where-the-file-is-looked-for)
- [`[validate]` — how findings are graded](#validate--how-findings-are-graded)
- [`[render]` — how the inventory is drawn](#render--how-the-inventory-is-drawn)
- [`[profile.<name>]` — named variations](#profilename--named-variations)
- [Precedence](#precedence)
- [Seeing what resolved, and why](#seeing-what-resolved-and-why)
- [Every render setting](#every-render-setting)
- [Errors](#errors)

## Where the file is looked for

`netgraph.toml` is read from the inventory root — the directory `-i/--inventory`
names, or the containing directory when `-i` names a single YAML file. It is
never searched for up the tree and never read from `$HOME`: a diagram is a
property of the inventory, and one that changed depending on which directory you
ran the command from would not be reproducible.

Which commands read which half:

| Command | `[validate]` | `[render]`, `[profile.*]` |
|---|---|---|
| `validate`, `render`, `path`, `ipam`, `list`, `show` | yes | — |
| `render`, `watch`, `path --highlight` | yes | yes |
| `web` | no — it edits a stream, not a tree | yes, from the `-i` inventory |

`watch` re-reads the file on every cycle for `[validate]`, so re-grading a rule
takes effect like editing any other file. The `[render]` half is resolved once
when the watch starts, because the command line it was combined with is fixed
for the run; restart the watcher after changing it.

## `[validate]` — how findings are graded

| Key | Type | Default | Meaning |
|---|---|---|---|
| `strict` | boolean | `false` | Promote every warning that survives `ignore` to an error. `--strict` can turn this on; nothing on the command line turns it off. |
| `ignore` | string or array of strings | `[]` | Rules never reported at all. `"*"` disables validation entirely. |
| `severity` | table of rule id → `"error"` \| `"warning"` \| `"info"` | `{}` | Re-grade a rule instead of silencing it. |

Rules are named by their short id (`E004`, `W103`, `I002`) or by the `NG-*`
alias from [`docs/schema.md` §10](schema.md#10-validation-rules); the two are
interchangeable. `netgraph rules` prints the catalogue and
[`docs/validation-rules.md`](validation-rules.md) explains each one.

```toml
[validate]
strict = true
ignore = ["W103"]           # "cable has no length" — we do not record lengths

[validate.severity]
E004 = "warning"            # duplicate address: a warning while we clean up
NG-C010 = "info"
```

`netgraph validate --disable RULE` adds to `ignore` for a single run, and a
`netgraph/ignore` annotation silences a rule for one element;
[`docs/validation-rules.md`](validation-rules.md#suppressing-a-rule) covers the
interaction of all three and what cannot be suppressed.

## `[render]` — how the inventory is drawn

Defaults for every diagram of this inventory. The naming rule is one line long:

> **a key is the long flag without its leading dashes.**

`--collapse-depth 1` is `collapse-depth = 1`, `--no-show-ips` is
`show-ips = false`, and a repeatable option takes an array — or a bare value,
which means the same as an array of one:

```toml
[render]
namespace = "sites/north"           # same as ["sites/north"]
layer = ["l1", "l2"]
```

The full list is [below](#every-render-setting). What is deliberately *not*
here: `--output`, `--force`, and the arguments of `path`. Those say what a
particular run does rather than what this inventory's diagrams look like, and a
file that could silently redirect output to a path you did not name would be a
trap rather than a convenience.

## `[profile.<name>]` — named variations

A profile is a `[render]` table with a name. It **inherits** `[render]` and
overrides only the keys it sets, so the shared decisions stay in one place:

```toml
[render]
icons = "cisco"
group-by-namespace = true

[profile.poster]                  # the wall diagram
layer = ["l1", "l2", "l3"]
format = "html"
title = "Campus — every layer"

[profile.review]                  # the collapsed render for a pull request
collapse-depth = 1
bundle-links = true
show-ips = false

[profile.l3]                      # the addressing view
layer = "l3"
max-addresses = 8
```

<!-- norun: needs the netgraph.toml above, and both lines write a diagram into the reader's directory -->
```console
$ netgraph render --profile poster -o docs/campus.html
$ netgraph render --profile review -f svg -o review.svg
```

`--profile` is available on `render`, `watch`, `web` and `path`. Naming a
profile the file does not declare is an error that lists the ones it does —
a mistyped `--profile` that quietly rendered the defaults would produce a
diagram indistinguishable from the one you wanted.

A profile name may hold letters, digits, `-`, `_` and `.`, and must start with a
letter or a digit. Shell completion offers the names of the current inventory:

<!-- norun: a shell completion, which needs the completion hook and a terminal to press TAB in -->
```console
$ netgraph render --profile <TAB>
poster   -- sets layer, format, title
review   -- sets collapse-depth, bundle-links, show-ips
l3       -- sets layer, max-addresses
```

## Precedence

Strongest first:

1. an explicit command-line flag,
2. the `[profile.<name>]` block selected with `--profile`,
3. the `[render]` table,
4. netgraph's built-in default.

Rung 1 means *explicit*, not *different*. Passing a flag its own default value
still beats the file:

```toml
[render]
depth = 3
```

<!-- norun: needs the netgraph.toml above, and the element name is illustrative -->
```console
$ netgraph render --neighbors-of sw-core                 # depth 3, from the file
$ netgraph render --neighbors-of sw-core --depth 1       # depth 1, from the flag
```

`--depth 1` wins even though `1` is also netgraph's built-in default, because
netgraph asks Click *where a value came from* rather than comparing it against
the default. There is no way to write a command line that means "use the file's
value" other than leaving the flag out, and no value that is silently ignored
because it happened to match a default.

Environment variables Click is configured to read count as explicit for the same
reason a flag does. A missing file is not a rung: it simply leaves 2 and 3
empty.

## Seeing what resolved, and why

`netgraph config show [COMMAND]` prints the settings a bare invocation of that
command resolves to, with the place each value came from:

<!-- norun: the transcript is of the netgraph.toml above; no committed inventory declares these profiles -->
```console
$ netgraph config show render --profile review
validation
SETTING   VALUE   SOURCE
--------  ------  -------
strict    false   default
ignore    W103    file [validate]
severity  (none)  default

settings for 'netgraph render'
configuration: /net/inventory/netgraph.toml
profiles declared: poster, review, l3

SETTING             VALUE    SOURCE
------------------  -------  --------------
layer               l2       file [render]
format              dot      default
...
collapse-depth      1        profile review
bundle-links        true     profile review
show-ips            false    profile review
group-by-namespace  true     file [render]
icons               cisco    file [render]
```

`COMMAND` is `render` (the default), `watch`, `path` or `web`, and each shows
the settings it actually takes: `web` draws no filtered graph, so it lists the
options it has rather than twenty it would ignore.

To see a *particular* invocation resolved — flags included — add `--show-config`
to the command itself. It prints the same table and exits without loading the
inventory or drawing anything:

<!-- norun: needs the 'poster' profile of the netgraph.toml above -->
```console
$ netgraph render --profile poster --title "Q3 review" --show-config
...
title               Q3 review   flag --title
```

## Every render setting

| Key | Type | Default | Flag it mirrors |
|---|---|---|---|
| `layer` | string or array | `"l1"` | `--layer` |
| `format` | string | `"dot"` (`"svg"` for `watch`) | `-f/--format` |
| `namespace` | string or array | none | `--namespace` |
| `vlan` | integer or array | none | `--vlan` |
| `kind` | string or array | none | `--kind` |
| `name` | string or array | none | `--name` |
| `neighbors-of` | string | none | `--neighbors-of` |
| `depth` | integer ≥ 0 | `1` | `--depth` |
| `collapse` | string or array | none | `--collapse` |
| `collapse-depth` | integer ≥ 1 | unset | `--collapse-depth` |
| `bundle-links` | boolean | unset (fold declared LAGs only) | `--bundle-links/--no-bundle-links` |
| `show-ips` | boolean | `true` | `--show-ips/--no-show-ips` |
| `show-vlans` | boolean | `true` | `--show-vlans/--no-show-vlans` |
| `group-by-namespace` | boolean | `false` | `--group-by-namespace` |
| `icons` | string | unset | `--icons` |
| `tooltips` | boolean | `true` | `--tooltips/--no-tooltips` |
| `link-template` | string | unset | `--link-template` |
| `element-ids` | boolean | `false` | `--element-ids` |
| `max-addresses` | integer ≥ 0 | `4` | `--max-addresses` |
| `rankdir` | `"TB"`, `"LR"`, `"BT"` or `"RL"` | `"TB"` | `--rankdir` |
| `title` | string | unset | `--title` |

Values are validated when the file is read, against the same registries the
flags use: `format` accepts exactly what `-f` accepts, `layer` exactly what
`--layer` accepts, and `link-template` is checked for unknown placeholders
before an inventory is loaded rather than after four hundred broken links have
been written to an SVG.

Two keys are worth a note:

**`icons`** takes a bundled theme name (`cisco`), `none`, or a directory of
images named after element kinds. A **relative** directory resolves against the
configuration file, not the working directory — the file lives with the
inventory, so a colleague who runs `netgraph` from a parent folder gets the same
icons.

**`bundle-links`** is genuinely three-valued. Unset means "fold only what the
inventory itself calls one link" — the members of a declared `lag` interface —
which is neither `true` nor `false`. Setting it to `false` turns even that off.

## Errors

An unknown key inside a known table is an error, not a silent no-op, and the
message names the file, the key, and the likely spelling:

<!-- norun: the transcript is of a netgraph.toml with a misspelt key, at an illustrative path -->
```console
$ netgraph render
error: /net/inventory/netgraph.toml: unknown key(s) in [render]: show_ips; did you mean 'show-ips'?
```

A value of the wrong type or outside its range says so in the same terms:

```console
error: /net/inventory/netgraph.toml: render.collapse-depth must be at least 1, got 0
error: /net/inventory/netgraph.toml: profile.poster.format must be one of dot, svg, html, png, pdf, mermaid, json, got 'jpeg'
```

Unknown *top-level tables*, on the other hand, are left alone, so a file shared
with a later netgraph version does not break this one. The asymmetry is
deliberate: a misspelt key inside `[render]` is a typo that would leave you
staring at a diagram wondering why the setting did nothing, while an unknown
table is how a newer feature arrives.

Every failure here exits with status 2, the usage-error status, because an
unusable configuration file is a problem with the invocation rather than with
the network. See [the exit codes](commands/README.md#exit-codes).
