# `netviz config`

`netviz config show` prints the settings a command resolves to, with the place
each value came from — a profile, the `[render]` table, or netviz's own default.
It is the command to reach for when a diagram does not look the way you asked
for, because it separates "the file does not say what I think it says" from "the
setting does not do what I think it does".

`config` is a group, and `show` is its only subcommand today. There is nothing to
`config set`: `netviz.toml` is a file you edit and commit, not state a tool
keeps for you.

---

## Synopsis

<!-- generated: synopsis config show -->
```text
netviz [GLOBAL OPTIONS] config show [OPTIONS] [render|watch|path|web]
```
<!-- /generated -->

## The `COMMAND` argument

Settings are per command, so the argument says whose to resolve: `render`,
`watch`, `path` or `web`. It defaults to `render`, which is the one with settings
of its own; the other three take the subset that applies to them — `watch` and
`web` because they call the renderer, and `path` because `--highlight` draws a
diagram. Each therefore shows the settings it *actually* takes rather than the
whole `[render]` vocabulary, so a key listed here is a key that command will read.

Anything else is a usage error: `fmt` and `validate` are configured by
`[validate]`, which has no per-command shape and is printed above the per-command
table on every run.

## Reading the report

Two tables come out: the `[validate]` half of the file, which has no per-command
shape, and then the settings of the command you named.

<!-- run: -->
```console
$ netviz -i examples/campus config show render
SETTING   VALUE   SOURCE
--------  ------  -------
strict    false   default
ignore    (none)  default
severity  (none)  default


SETTING             VALUE    SOURCE
------------------  -------  -------
layer               l1       default
format              dot      default
...
title               (unset)  default
validation
settings for 'netviz render'
...
```

The three headings are commentary and go to stderr, which is why they arrive
after the tables when the two streams are captured separately, as above; on a
terminal `validation` sits above the first table and `settings for 'netviz
render'` above the second, and `-q` leaves you the tables alone.

The last elided line names the `netviz.toml` that was found, or reads
`configuration: none (netviz.toml not found; built-in defaults in use)` when
there is none — that line alone answers "is my file even being read?". A tree
that declares profiles lists them next, so a mistyped `--profile` is visible
before you go looking for the block.

`VALUE` is the effective value as the command will see it. `(none)` is an empty
repeatable option and `(unset)` a setting with no default at all — neither is the
string "none".

### What `SOURCE` means

The column names the rung of the precedence ladder that supplied the value, and
there are four:

| `SOURCE` | Meaning |
|---|---|
| `default` | Nobody said anything; this is netviz's built-in value. |
| `file [render]` | The `[render]` table of this inventory's `netviz.toml`. |
| `profile <name>` | The `[profile.<name>]` block, which outranks `[render]`. |
| `flag --x` | The command line. **It never appears here**, because `config show` resolves a *bare* invocation. |

That last row is the thing to understand about this command: no flags are in
play, so what you are shown is what the file does to a bare `netviz COMMAND`.
To see one *invocation* resolved, flags included, pass `--show-config` to the
command itself. The full ladder is
[Precedence](../configuration.md#precedence).

## `--profile NAME`

`--profile NAME` resolves as if `--profile NAME` had been given to the command,
which is how you check a profile without producing a diagram from it:

<!-- norun: the inventory, its path and its profiles are illustrative -->
```console
$ netviz config show render --profile review
settings for 'netviz render'
configuration: /net/inventory/netviz.toml
profiles declared: poster, review

SETTING             VALUE   SOURCE
------------------  ------  --------------
layer               l2      file [render]
collapse-depth      1       profile review
show-ips            false   profile review
```

Two rungs are visible at once here: `layer` came from `[render]`, and the two
settings the profile overrides say so by name. A profile the file does not
declare is an error rather than a silent fallback, and the message says which are
declared:

<!-- norun: the message names the reader's own netviz.toml -->
```console
$ netviz -i examples/campus config show render --profile review
error: examples/campus/netviz.toml: no profile 'review': this inventory declares no [profile.<name>] block. Add one to netviz.toml at the inventory root
```

## Debugging a setting that is not taking effect

Work down the report, in this order:

1. **Is the file being read?** The `configuration:` line names it. If it says
   `none`, the file is not where netviz looks — beside the inventory root, or
   above it. See
   [Where the file is looked for](../configuration.md#where-the-file-is-looked-for).
2. **Is the key spelled as a flag?** Every `[render]` key is a long option of
   `netviz render` without its leading dashes, so `--collapse-depth 1` is
   `collapse-depth = 1` and `--no-show-ips` is `show-ips = false`. A key netviz
   does not know is an error, not a silent no-op, so a run that succeeds and
   still shows `default` means the key is right and something outranks it.
3. **Does this command take the setting at all?** `config show path` lists ten
   settings, not twenty-two. A `[render]` key the command never reads will not
   appear in its table.
4. **Is a profile overriding it?** `profile <name>` in `SOURCE` says so.
   Re-running without `--profile` shows what the plain file does.
5. **Is a flag overriding it?** Nothing here can tell you, by construction.
   Re-run the real command with `--show-config` and look for `flag --…` in the
   same column — see
   [Seeing what resolved, and why](../configuration.md#seeing-what-resolved-and-why).

An unusable `netviz.toml` — bad TOML, an unknown key, a value of the wrong
type — fails every command including this one, with the line and what was
expected. Those messages are catalogued in
[Errors](../configuration.md#errors).

## Arguments

<!-- generated: arguments config show -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[render\|watch\|path\|web]` | no | 1 | `render` |
<!-- /generated -->

## Options

<!-- generated: options config show -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--profile` | `NAME` | — | Resolve as if --profile NAME had been given. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The settings were printed. |
| `2` | Usage error — a `COMMAND` that is not configurable, an unusable `netviz.toml`, or a `--profile` the file does not declare. |
| `3` | The inventory could not be discovered or read at all. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

## See also

* [`docs/configuration.md`](../configuration.md) — every key of `netviz.toml`,
  the profile mechanism, the precedence ladder and the error messages.
* [`netviz render`](render.md) — the command whose settings this resolves, and
  the `--show-config` that resolves one invocation of it.
* [`netviz init`](init.md) — writes a fully commented `netviz.toml` in which
  every line is the default it would change.
