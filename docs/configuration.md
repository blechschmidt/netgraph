# `netviz.toml`

One optional file at the root of an inventory, holding everything that is true
of *this* network rather than of this invocation: which rules it is graded by,
what its diagrams look like, and where its parse cache goes.

```toml
# netviz.toml
[validate]
strict = false
ignore = ["W103", "NV-C010"]

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
declared the defaults, and `netviz init` scaffolds a fully commented copy.

**Contents**

- [Where the file is looked for](#where-the-file-is-looked-for)
- [`[validate]` — how findings are graded](#validate--how-findings-are-graded)
- [`[render]` — how the inventory is drawn](#render--how-the-inventory-is-drawn)
- [`[profile.<name>]` — named variations](#profilename--named-variations)
- [Precedence](#precedence)
- [Seeing what resolved, and why](#seeing-what-resolved-and-why)
- [Every render setting](#every-render-setting)
- [`[theme]` — this inventory's own styling rules](#theme--this-inventorys-own-styling-rules)
- [`[cache]` — remembering parsed files](#cache--remembering-parsed-files)
- [`[history]` — how far back a timeline goes](#history--how-far-back-a-timeline-goes)
- [`[editor]` — the visual editor's grid](#editor--the-visual-editors-grid)
- [Errors](#errors)

## Where the file is looked for

`netviz.toml` is read from the inventory root — the directory `-i/--inventory`
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

Rules are named by their short id (`E004`, `W103`, `I002`) or by the `NV-*`
alias from [`docs/schema.md` §10](schema.md#10-validation-rules); the two are
interchangeable. `netviz rules` prints the catalogue and
[`docs/validation-rules.md`](validation-rules.md) explains each one.

```toml
[validate]
strict = true
ignore = ["W103"]           # "cable has no length" — we do not record lengths

[validate.severity]
E004 = "warning"            # duplicate address: a warning while we clean up
NV-C010 = "info"
```

`netviz validate --disable RULE` adds to `ignore` for a single run, and a
`netviz/ignore` annotation silences a rule for one element;
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

<!-- norun: needs the netviz.toml above, and both lines write a diagram into the reader's directory -->
```console
$ netviz render --profile poster -o docs/campus.html
$ netviz render --profile review -f svg -o review.svg
```

`--profile` is available on `render`, `watch`, `web` and `path`. Naming a
profile the file does not declare is an error that lists the ones it does —
a mistyped `--profile` that quietly rendered the defaults would produce a
diagram indistinguishable from the one you wanted.

A profile name may hold letters, digits, `-`, `_` and `.`, and must start with a
letter or a digit. Shell completion offers the names of the current inventory:

<!-- norun: a shell completion, which needs the completion hook and a terminal to press TAB in -->
```console
$ netviz render --profile <TAB>
poster   -- sets layer, format, title
review   -- sets collapse-depth, bundle-links, show-ips
l3       -- sets layer, max-addresses
```

## Precedence

Strongest first:

1. an explicit command-line flag,
2. the `[profile.<name>]` block selected with `--profile`,
3. the `[render]` table,
4. netviz's built-in default.

Rung 1 means *explicit*, not *different*. Passing a flag its own default value
still beats the file:

```toml
[render]
depth = 3
```

<!-- norun: needs the netviz.toml above, and the element name is illustrative -->
```console
$ netviz render --neighbors-of sw-core                 # depth 3, from the file
$ netviz render --neighbors-of sw-core --depth 1       # depth 1, from the flag
```

`--depth 1` wins even though `1` is also netviz's built-in default, because
netviz asks Click *where a value came from* rather than comparing it against
the default. There is no way to write a command line that means "use the file's
value" other than leaving the flag out, and no value that is silently ignored
because it happened to match a default.

Environment variables Click is configured to read count as explicit for the same
reason a flag does. A missing file is not a rung: it simply leaves 2 and 3
empty.

## Seeing what resolved, and why

`netviz config show [COMMAND]` prints the settings a bare invocation of that
command resolves to, with the place each value came from:

<!-- norun: the transcript is of the netviz.toml above; no committed inventory declares these profiles -->
```console
$ netviz config show render --profile review
validation
SETTING   VALUE   SOURCE
--------  ------  -------
strict    false   default
ignore    W103    file [validate]
severity  (none)  default

settings for 'netviz render'
configuration: /net/inventory/netviz.toml
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

<!-- norun: needs the 'poster' profile of the netviz.toml above -->
```console
$ netviz render --profile poster --title "Q3 review" --show-config
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
| `annotations` | boolean | `true` | `--annotations/--no-annotations` |
| `group-by-namespace` | boolean | `false` | `--group-by-namespace` |
| `icons` | string | unset | `--icons` |
| `theme` | string | unset | `--theme` |
| `style` | boolean | `true` | `--style/--no-style` |
| `tooltips` | boolean | `true` | `--tooltips/--no-tooltips` |
| `link-template` | string | unset | `--link-template` |
| `element-ids` | boolean | `false` | `--element-ids` |
| `max-addresses` | integer ≥ 0 | `4` | `--max-addresses` |
| `rankdir` | `"TB"`, `"LR"`, `"BT"` or `"RL"` | `"TB"` | `--rankdir` |
| `routing` | `"spline"`, `"orthogonal"` or `"straight"` | what the layout documents say, else `"spline"` | `--routing` |
| `avoid` | boolean | `true` | `--avoid/--no-avoid` |
| `title` | string | unset | `--title` |

Values are validated when the file is read, against the same registries the
flags use: `format` accepts exactly what `-f` accepts, `layer` exactly what
`--layer` accepts, and `link-template` is checked for unknown placeholders
before an inventory is loaded rather than after four hundred broken links have
been written to an SVG.

Four keys are worth a note:

**`icons`** takes a bundled theme name (`cisco`), `none`, or a directory of
images named after element kinds. A **relative** directory resolves against the
configuration file, not the working directory — the file lives with the
inventory, so a colleague who runs `netviz` from a parent folder gets the same
icons.

**`theme`** is the same idea for the *stylesheet* rather than the pictures: a
bundled name (`blueprint`, `mono`), `none`, or a path to a `kind: theme` YAML
file, and a relative path resolves against the configuration file for exactly
the reason `icons` does. It is the default for every diagram of this inventory,
so the colours stop depending on somebody's shell history; `--theme` on the
command line overrides it and `--theme none` turns it off.
[`docs/styling.md`](styling.md) is the guide.

**`style`** is the escape hatch, and `style = false` is `--no-style`: draw from
the built-in palette alone, ignoring every style the inventory and the theme
declare. Setting it in the file is for an inventory that carries styles it does
not want in its *default* rendering; most of the time it belongs on the command
line, where it answers "is this diagram odd because of the network or because of
the stylesheet?" for one run. Icons are a separate ladder rung and are
unaffected — `icons = "none"` is the switch for those.

**`bundle-links`** is genuinely three-valued. Unset means "fold only what the
inventory itself calls one link" — the members of a declared `lag` interface —
which is neither `true` nor `false`. Setting it to `false` turns even that off.

**`avoid`** only ever does anything to an *arranged* diagram whose links are
`orthogonal`: it routes them around the boxes they are not attached to instead
of straight across them ([`docs/rendering.md`](rendering.md#routing-around-things)).
A bend somebody placed is never moved by it, and a link whose line already
crosses nothing is left exactly where it was. `avoid = false` is the local
Z-and-L every orthogonal diagram was drawn with before it existed, which is
faster and entirely predictable — worth having for a deliberately schematic
drawing, and worth reaching for if a route ever detours somewhere surprising.

## `[theme]` — this inventory's own styling rules

`[render] theme` *names* a stylesheet. This table *is* one: the rules written
inline, for the inventory that wants three lines of house style and not a second
file to keep beside the manifests.

```toml
[render]
theme = "blueprint"          # the base, by name or by path — optional

[[theme.rules]]
select = {role = ["core"]}
style = {strokeWidth = 3}

[[theme.rules]]
select = {namespace = ["sites/hq/**"]}
style = {fill = "#f8fafc", fontColor = "slate"}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `rules` | array of tables | `[]` | The rules, in declaration order. Each is a `select` table and a `style` table, exactly the `spec.rules` of a `kind: theme` document ([`docs/schema.md` §22.3](schema.md#223-kind-theme--a-stylesheet)). |

`select` takes the five clauses — `kind`, `name`, `namespace`, `role`, `label` —
and `style` the nine fields of a style block. They are not a second dialect:
the entries go through the very same models a theme file does, so a mistyped
colour here is refused with the same wording, and under the same rule id, that
it would get in a `theme.yaml`. A table declaring no rules at all is an error
for the same reason an empty theme file is — it would parse, change nothing, and
tell you nothing.

**The rules are appended after whatever `--theme` or `[render] theme` named**,
rather than merged into it, and that is the whole mechanism. Precedence reads
equally specific rules back to front ([§22.4](schema.md#224-precedence-the-ladder)),
so an appended rule **wins a tie** against the named theme without having to
restate the rules it agrees with — which is exactly what an inventory adjusting
a bundled theme wants. It does not let it beat a *more specific* rule, because
specificity is still read first: a rule that must beat one states at least as
many conditions as it does.

Appending is also why the two are still tellable apart afterwards. The composed
stylesheet calls itself `blueprint+theme`, and the inline rules keep their
positions at the end of the list, so a `from` entry of `theme:blueprint+theme#16`
in [`-f json`](rendering.md#the-json-export) — one past the last of the sixteen
rules `blueprint` itself declares — names your file rather than netviz's.

Three consequences worth knowing:

* With no named theme, the table is the whole theme. `[theme]` alone is a
  perfectly good house style.
* A `[profile.<name>]` may name a different `theme`; the inline rules are
  appended to whichever one won, because they are this inventory's own rather
  than a default somebody picked.
* `--no-style` ignores them along with everything else declared, since it draws
  from the bottom two rungs of the ladder only.

It is a **top-level table, not a render setting**: it is not one value with one
source, so it does not appear in `netviz config show` beside `theme` and it
cannot be overridden from the command line. Turn it off by turning styling off.
[`docs/styling.md`](styling.md#a-default-for-the-inventory) works the interaction
of the two through with a rendered example.

## `[cache]` — remembering parsed files

Every command re-reads the inventory from disk, because the files are the only
state netviz trusts. Turning those bytes back into validated models is the
expensive half of that, so a file that has been parsed once is remembered:

```toml
[cache]
enabled = true          # the default; false opts this inventory out entirely
dir = ".netviz-cache" # relative to *this file*; default is the platform's cache dir
max-size = "64MB"       # least recently used entries are dropped past this
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Use the cache. `--no-cache` and `NETVIZ_NO_CACHE` can turn it off; nothing turns it back on, so an inventory that has opted out stays opted out. |
| `dir` | string | platform cache directory | Base directory. A relative path resolves against this file, not the working directory. `NETVIZ_CACHE_DIR` outranks it. |
| `max-size` | integer bytes, or a string like `"256MB"`, `"1GiB"` | `64MB` | Cap on what is kept for this inventory. `0` keeps nothing on disk. |

This is the one table that configures *how* netviz runs rather than what it
produces. It earns its place because it is the one thing a shared inventory may
need to say about the machines it is used on — a CI runner whose home directory is
read-only, a repository that wants the cache inside a directory it already
archives between builds.

### Exactly what is stored

One file per inventory file, named after a SHA-256 of the file's **contents**, the
path it has **within the inventory**, and the **identity** of the netviz that
read it (see `netviz cache info`). Each holds, zlib-compressed:

* every element of that file as pydantic serialises it — the same JSON
  `netviz show -o json` prints, after ranges are expanded and defaults applied;
* every diagnostic the file produced, with its line, column and rule id, so a
  broken document is reported identically without being parsed again.

No timestamp is part of the key, so a file rewritten with identical bytes hits, a
`git checkout` of an old revision hits again, and a `touch` changes nothing.
Nothing is ever stored as a pickle: entries are reconstructed through the very
same validators the document went through, so a tampered entry can be rejected
but cannot execute anything or smuggle a model past the schema.

**Where it goes**, strongest first: `NETVIZ_CACHE_DIR`, then `[cache] dir`,
then `XDG_CACHE_HOME/netviz`, then the platform's own answer
(`%LOCALAPPDATA%\netviz\Cache` on Windows, `~/Library/Caches/netviz` on
macOS, `~/.cache/netviz` elsewhere). Under it, one directory per inventory,
named after the tree and a digest of its absolute path.

**What is not cached:** files declaring a `kind: template` and devices that
inherit one with `spec.from`, because their meaning depends on another file's
bytes; and anything at all under `netviz validate --format json|sarif|github`,
which keeps the per-field provenance that a cache entry does not hold.

A corrupt, truncated or half-written entry is not an error: it is a miss, and the
file is parsed. So is an unwritable cache directory or a full disk.
[`netviz cache info`](commands/cache.md) reports the directory, the entry count
and the identity; `netviz cache clear` empties it; `-v` prints the hit and miss
counts of a single run.

### Turning it off

Three ways, for three scopes:

| Scope | How |
|---|---|
| One run | `netviz --no-cache …` — the flag is global and goes before the subcommand. |
| A whole environment: CI, a container image, a pre-commit hook | `NETVIZ_NO_CACHE=1` in the environment. |
| This inventory, for everybody | `[cache] enabled = false` in `netviz.toml`. |

**In CI, leaving it on is usually the wrong default** — not because it is unsafe
but because it is pointless: a fresh runner starts with an empty cache and pays
the ~20 % cost of *filling* one nobody will read. Either set
`NETVIZ_NO_CACHE=1`, or point `NETVIZ_CACHE_DIR` at a directory the runner
restores between builds and get the hit instead:

```yaml
# GitHub Actions: keep the cache between runs rather than disabling it
- uses: actions/cache@v4
  with:
    path: .netviz-cache
    key: netviz-${{ hashFiles('**/*.yaml') }}
- run: netviz validate
  env:
    NETVIZ_CACHE_DIR: .netviz-cache
```

Container images that run one command and exit should set `NETVIZ_NO_CACHE=1`;
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

Read by [`netviz log`](commands/log.md) and by the timeline in
[`netviz web`](commands/web.md#the-history-timeline). It exists because
reading history is not free: summarising one commit means loading the inventory
on both sides of it, and *drawing* one means a Graphviz layout as well. A
hundred is already more history than a scrubber can address a pixel at a time,
and a repository with a decade of commits would otherwise turn one command into
several hundred renders.

The two consumers spend it differently, on purpose. `netviz log` **refuses** a
range wider than this and says so, because a range is something you asked for by
name. The editor **truncates** to the newest and says how many there are,
because a scrubber that shows nothing is not a better answer than a scrubber
that shows the recent past. `netviz log --max-revisions` overrides the file
for one invocation.

## `[editor]` — the visual editor's grid

```toml
[editor]
grid = 20
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `grid` | number > 0 | `20` | Pitch in points that **snap to grid** rounds a selection's positions to. |

Read by [`netviz web`](commands/web.md), which offers it as one of the
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

<!-- norun: the transcript is of a netviz.toml with a misspelt key, at an illustrative path -->
```console
$ netviz render
error: /net/inventory/netviz.toml: unknown key(s) in [render]: show_ips; did you mean 'show-ips'?
```

A value of the wrong type or outside its range says so in the same terms:

```console
error: /net/inventory/netviz.toml: render.collapse-depth must be at least 1, got 0
error: /net/inventory/netviz.toml: profile.poster.format must be one of dot, svg, html, png, pdf, mermaid, json, got 'jpeg'
```

Unknown *top-level tables*, on the other hand, are left alone, so a file shared
with a later netviz version does not break this one. The asymmetry is
deliberate: a misspelt key inside `[render]` is a typo that would leave you
staring at a diagram wondering why the setting did nothing, while an unknown
table is how a newer feature arrives.

Every failure here exits with status 2, the usage-error status, because an
unusable configuration file is a problem with the invocation rather than with
the network. See [the exit codes](commands/README.md#exit-codes).
