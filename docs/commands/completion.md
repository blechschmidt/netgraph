# `netgraph completion`

`netgraph completion SHELL` prints a shell completion script on stdout for
`bash`, `zsh` or `fish`. Installing it gets you the commands and flags, which any
Click program gives you for free — and, more usefully, the values that depend on
*your* inventory: element names, namespaces, rule ids and the profiles your
`netgraph.toml` declares. The command itself needs no inventory; the completers
it installs do.

---

## Synopsis

<!-- generated: synopsis completion -->
```text
netgraph [GLOBAL OPTIONS] completion [OPTIONS] {bash|zsh|fish}
```
<!-- /generated -->

## Installing it

One line per shell. The script is plain text on stdout, so redirect it where the
shell looks:

<!-- norun: writes into the reader's shell configuration directories -->
```bash
# bash — needs bash-completion installed
netgraph completion bash > ~/.local/share/bash-completion/completions/netgraph

# zsh — any directory on $fpath will do
mkdir -p ~/.zfunc && netgraph completion zsh > ~/.zfunc/_netgraph
# and, in ~/.zshrc, before compinit:
#   fpath=(~/.zfunc $fpath)

# fish
netgraph completion fish > ~/.config/fish/completions/netgraph.fish
```

Start a new shell afterwards. To try it without installing anything, source it in
the current shell instead — `eval "$(netgraph completion bash)"`,
`eval "$(netgraph completion zsh)"`, `netgraph completion fish | source`.

What is actually written is a small dispatcher: it re-invokes `netgraph` with a
`_NETGRAPH_COMPLETE` variable set and turns the answer into candidates. Nothing is
baked into the script, which is why a netgraph upgrade that adds a command or a
format needs no reinstall.

<!-- run: -->
```console
$ netgraph completion bash
_netgraph_completion() {
    local IFS=$'\n'
    local response
...
_netgraph_completion_setup;
```

Only these three shells are on offer, because Click can generate for exactly
these three; anything else would need its own generator rather than a flag, and
asking for one says so:

<!-- run: rc=2 -->
```console
$ netgraph completion pwsh
Usage: netgraph completion [OPTIONS] {bash|zsh|fish}
Try 'netgraph completion --help' for help.

Error: Invalid value for '{bash|zsh|fish}': 'pwsh' is not one of 'bash', 'zsh', 'fish'.
```

## What gets completed

Commands and flags complete as you would expect. Beyond them, every value space
worth completing has a completer, and each candidate carries a description:

| At the cursor | Offers |
|---|---|
| `netgraph show <TAB>` | Every element of the inventory named by `-i`, fully qualified and by short name, described by its kind. |
| `--neighbors-of <TAB>` | The same, minus the cables: a cable is an edge, not a node, so completing one would offer a name the filter then rejects. |
| `--namespace`, `--collapse <TAB>` | Every namespace holding an element and every ancestor of one, outermost first, with how many elements each covers. |
| `-f/--format <TAB>` | The registered output formats, with what each one produces. |
| `netgraph export <TAB>` | The five export formats, with the artefact each writes. |
| `--layer <TAB>` | `physical`, `l1`, `l2`, `l3`, `overlay`, `rack`, with what each one draws. |
| `--kind <TAB>` | The element kinds the option accepts — no `cable` on a filter, `cable` included on `netgraph schema`. |
| `--disable <TAB>` | Rule ids with their summaries, `*` included; type `NG-` for the schema aliases. |
| `--profile <TAB>` | The `[profile.<name>]` blocks of the inventory's `netgraph.toml`, each described by the settings it overrides. |

Two details follow from how the lists are built rather than from a decision about
completion. The kinds offered are the ones *that parameter* accepts, so `--kind`
on a filter and `--kind` on [`netgraph schema`](schema.md) stay correct without
two lists being maintained. And every completer answers from the same registry
the command itself uses, so a format, kind, layer or rule added elsewhere
completes without a line of completion code changing.

zsh and fish show the descriptions next to each candidate; bash lists the values
alone, as it does for everything.

## Inventory-aware completion loads the inventory

This is the caveat worth knowing. The completers for elements, namespaces and
profiles **read the tree pointed at by `-i`** — the `-i` already on the command
line you are typing, so
`netgraph -i examples/campus show sites/north/<TAB>` completes that site, and
without `-i` they read the current directory. There is no cache and no daemon:
each `<TAB>` loads and resolves the documents, which is what makes the names
correct and what makes a very large tree perceptibly slower to complete than a
small one.

Because of that they are written to **never fail loudly**. A tree that is
half-written — which is exactly when you reach for completion — simply offers
nothing: the load errors are collected rather than raised, a `netgraph.toml` with
a typo in it yields an empty profile list rather than a traceback, and a `-i`
naming a directory that does not exist yet is not an error. A completer that
printed a diagnostic would corrupt the command line the user is in the middle of,
and one that raised would look like a broken install.

The script itself is generated from nothing but the command tree, so
`netgraph completion` works before any inventory exists — installing completion is
a reasonable first thing to do after installing netgraph, and
[`netgraph init`](init.md) the second.

## Arguments

<!-- generated: arguments completion -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `{bash\|zsh\|fish}` | yes | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options completion -->
*No options of its own; the global options apply.*
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The script was printed. |
| `2` | Usage error — a shell netgraph cannot generate for. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

No inventory is read, so the codes for a missing or invalid tree cannot occur.

## See also

* [`docs/getting-started.md`](../getting-started.md#installation) — installing
  netgraph, and the editor wiring that does for YAML what this does for the shell.
* [`netgraph show`](show.md) and [`netgraph list`](list.md) — the element names
  completion offers, and the command that lists them all.
* [`docs/configuration.md`](../configuration.md#profilename--named-variations) —
  the profile blocks `--profile <TAB>` reads.
