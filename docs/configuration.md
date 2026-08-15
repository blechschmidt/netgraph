# `netgraph.toml`

One optional file at the root of an inventory, holding everything that is true
of *this* network rather than of this invocation: which rules it is graded by,
what its diagrams look like, and where its parse cache goes.

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
- [`[cache]` — remembering parsed files](#cache--remembering-parsed-files)
- [`[history]` — how far back a timeline goes](#history--how-far-back-a-timeline-goes)
- [`[editor]` — the visual editor's grid](#editor--the-visual-editors-grid)
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

`[cache]` is read by every command that loads an inventory, which is every command
in the table above. It is also the one table whose absence cannot be noticed: an
inventory that says nothing about the cache gets the default one.

`watch` re-reads the file on every cycle for `[validate]`, so re-grading a rule
takes effect like editing any other file. The `[render]` half is resolved once
when the watch starts, because the command line it was combined with is fixed
for the run; restart the watcher after changing it. So is `[cache]`, for the same
reason.

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
| `routing` | `"spline"`, `"orthogonal"` or `"straight"` | what the layout documents say, else `"spline"` | `--routing` |
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

## `[cache]` — remembering parsed files

Every command re-reads the inventory from disk, because the files are the only
state netgraph trusts. Turning those bytes back into validated models is the
expensive half of that, so a file that has been parsed once is remembered:

```toml
[cache]
enabled = true          # the default; false opts this inventory out entirely
dir = ".netgraph-cache" # relative to *this file*; default is the platform's cache dir
max-size = "64MB"       # least recently used entries are dropped past this
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Use the cache. `--no-cache` and `NETGRAPH_NO_CACHE` can turn it off; nothing turns it back on, so an inventory that has opted out stays opted out. |
| `dir` | string | platform cache directory | Base directory. A relative path resolves against this file, not the working directory. `NETGRAPH_CACHE_DIR` outranks it. |
| `max-size` | integer bytes, or a string like `"256MB"`, `"1GiB"` | `64MB` | Cap on what is kept for this inventory. `0` keeps nothing on disk. |

This is the one table that configures *how* netgraph runs rather than what it
produces. It earns its place because it is the one thing a shared inventory may
need to say about the machines it is used on — a CI runner whose home directory is
read-only, a repository that wants the cache inside a directory it already
archives between builds.

### Exactly what is stored

One file per inventory file, named after a SHA-256 of the file's **contents**, the
path it has **within the inventory**, and the **identity** of the netgraph that
read it (see `netgraph cache info`). Each holds, zlib-compressed:

* every element of that file as pydantic serialises it — the same JSON
  `netgraph show -o json` prints, after ranges are expanded and defaults applied;
* every diagnostic the file produced, with its line, column and rule id, so a
  broken document is reported identically without being parsed again.

No timestamp is part of the key, so a file rewritten with identical bytes hits, a
`git checkout` of an old revision hits again, and a `touch` changes nothing.
Nothing is ever stored as a pickle: entries are reconstructed through the very
same validators the document went through, so a tampered entry can be rejected
but cannot execute anything or smuggle a model past the schema.

**Where it goes**, strongest first: `NETGRAPH_CACHE_DIR`, then `[cache] dir`,
then `XDG_CACHE_HOME/netgraph`, then the platform's own answer
(`%LOCALAPPDATA%\netgraph\Cache` on Windows, `~/Library/Caches/netgraph` on
macOS, `~/.cache/netgraph` elsewhere). Under it, one directory per inventory,
named after the tree and a digest of its absolute path.

**What is not cached:** files declaring a `kind: template` and devices that
inherit one with `spec.from`, because their meaning depends on another file's
bytes; and anything at all under `netgraph validate --format json|sarif|github`,
which keeps the per-field provenance that a cache entry does not hold.

A corrupt, truncated or half-written entry is not an error: it is a miss, and the
file is parsed. So is an unwritable cache directory or a full disk.
[`netgraph cache info`](commands/cache.md) reports the directory, the entry count
and the identity; `netgraph cache clear` empties it; `-v` prints the hit and miss
counts of a single run.

### Turning it off

Three ways, for three scopes:

| Scope | How |
|---|---|
| One run | `netgraph --no-cache …` — the flag is global and goes before the subcommand. |
| A whole environment: CI, a container image, a pre-commit hook | `NETGRAPH_NO_CACHE=1` in the environment. |
| This inventory, for everybody | `[cache] enabled = false` in `netgraph.toml`. |

**In CI, leaving it on is usually the wrong default** — not because it is unsafe
but because it is pointless: a fresh runner starts with an empty cache and pays
the ~20 % cost of *filling* one nobody will read. Either set
`NETGRAPH_NO_CACHE=1`, or point `NETGRAPH_CACHE_DIR` at a directory the runner
restores between builds and get the hit instead:

```yaml
# GitHub Actions: keep the cache between runs rather than disabling it
- uses: actions/cache@v4
  with:
    path: .netgraph-cache
    key: netgraph-${{ hashFiles('**/*.yaml') }}
- run: netgraph validate
  env:
    NETGRAPH_CACHE_DIR: .netgraph-cache
```

Container images that run one command and exit should set `NETGRAPH_NO_CACHE=1`;
this project's own image instead points `XDG_CACHE_HOME` at `/tmp`, so a
read-only home directory is not a problem either way.

## `[history]` — how far back a timeline goes

```toml
[history]
max-revisions = 100
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max-revisions` | integer ≥ 1 | `100` | Most revisions of the inventory one range may hold before it is refused. |

Read by [`netgraph log`](commands/log.md) and by the timeline in
[`netgraph web`](commands/web.md#the-history-timeline). It exists because
reading history is not free: summarising one commit means loading the inventory
on both sides of it, and *drawing* one means a Graphviz layout as well. A
hundred is already more history than a scrubber can address a pixel at a time,
and a repository with a decade of commits would otherwise turn one command into
several hundred renders.

The two consumers spend it differently, on purpose. `netgraph log` **refuses** a
range wider than this and says so, because a range is something you asked for by
name. The editor **truncates** to the newest and says how many there are,
because a scrubber that shows nothing is not a better answer than a scrubber
that shows the recent past. `netgraph log --max-revisions` overrides the file
for one invocation.

## `[editor]` — the visual editor's grid

```toml
[editor]
grid = 20
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `grid` | number > 0 | `20` | Pitch in points that **snap to grid** rounds a selection's positions to. |

Read by [`netgraph web`](commands/web.md), which offers it as one of the
[alignment commands](editing.md#arranging-a-selection) on a multi-selection.
Points, because everything in a `kind: layout` document is points (§18); twenty
is half of Graphviz's default rank separation and a little under a node's
height, so a snapped diagram lines up without every device landing on its
neighbour.

It is in the file rather than in the browser because snapping writes real
coordinates into a real document. Everybody editing this inventory should snap
to the same lattice, or the second person's tidy-up quietly undoes the first's.

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
