# `netviz completion`

`netviz completion SHELL` prints a shell completion script on stdout for
`bash`, `zsh`, `fish` or `powershell`. Installing it gets you the commands and
flags, which any
Click program gives you for free — and, more usefully, the values that depend on
*your* inventory: element names, namespaces, rule ids and the profiles your
`netviz.toml` declares. The command itself needs no inventory; the completers
it installs do.

---

## Synopsis

<!-- generated: synopsis completion -->
```text
netviz [GLOBAL OPTIONS] completion [OPTIONS] {bash|zsh|fish|powershell}
```
<!-- /generated -->

## Installing it

One line per shell. The script is plain text on stdout, so redirect it where the
shell looks:

<!-- norun: writes into the reader's shell configuration directories -->
```bash
# bash — needs bash-completion installed
netviz completion bash > ~/.local/share/bash-completion/completions/netviz

# zsh — any directory on $fpath will do
mkdir -p ~/.zfunc && netviz completion zsh > ~/.zfunc/_netviz
# and, in ~/.zshrc, before compinit:
#   fpath=(~/.zfunc $fpath)

# fish
netviz completion fish > ~/.config/fish/completions/netviz.fish
```

PowerShell — on Windows, or `pwsh` anywhere — does not source a file; it
evaluates the script. One line, and the same line goes in `$PROFILE` to have it
in every session:

<!-- norun: writes into the reader's PowerShell profile -->
```powershell
netviz completion powershell | Out-String | Invoke-Expression

# permanently:
Add-Content -Path $PROFILE -Value 'netviz completion powershell | Out-String | Invoke-Expression'
```

Start a new shell afterwards. To try it without installing anything, source it in
the current shell instead — `eval "$(netviz completion bash)"`,
`eval "$(netviz completion zsh)"`, `netviz completion fish | source`.

What is actually written is a small dispatcher: it re-invokes `netviz` with a
`_NETVIZ_COMPLETE` variable set and turns the answer into candidates. Nothing is
baked into the script, which is why a netviz upgrade that adds a command or a
format needs no reinstall.

<!-- run: -->
```console
$ netviz completion bash
_netviz_completion() {
    local IFS=$'\n'
    local response
...
_netviz_completion_setup;
```

Only these four are on offer. Click generates the first three; `powershell` is
netviz's own generator (`PowerShellComplete` in `netviz/completion.py`),
because PowerShell's completion protocol is a registered script block rather
than a `compgen` call. Anything else would need a generator of its own rather
than a flag, and asking for one says so:

<!-- run: rc=2 -->
```console
$ netviz completion tcsh
Usage: netviz completion [OPTIONS] {bash|zsh|fish|powershell}
Try 'netviz completion --help' for help.

Error: Invalid value for '{bash|zsh|fish|powershell}': 'tcsh' is not one of 'bash', 'zsh', 'fish', 'powershell'.
```

## What gets completed

Commands and flags complete as you would expect. Beyond them, every value space
worth completing has a completer, and each candidate carries a description:

| At the cursor | Offers |
|---|---|
| `netviz show <TAB>` | Every element of the inventory named by `-i`, fully qualified and by short name, described by its kind. |
| `--neighbors-of <TAB>` | The same, minus the cables: a cable is an edge, not a node, so completing one would offer a name the filter then rejects. |
| `--namespace`, `--collapse <TAB>` | Every namespace holding an element and every ancestor of one, outermost first, with how many elements each covers. |
| `-f/--format <TAB>` | The registered output formats, with what each one produces. |
| `netviz export <TAB>` | The five export formats, with the artefact each writes. |
| `--layer <TAB>` | `physical`, `l1`, `l2`, `l3`, `overlay`, `rack`, with what each one draws. |
| `--kind <TAB>` | The element kinds the option accepts — no `cable` on a filter, `cable` included on `netviz schema`. |
| `--disable <TAB>` | Rule ids with their summaries, `*` included; type `NV-` for the schema aliases. |
| `--profile <TAB>` | The `[profile.<name>]` blocks of the inventory's `netviz.toml`, each described by the settings it overrides. |

Two details follow from how the lists are built rather than from a decision about
completion. The kinds offered are the ones *that parameter* accepts, so `--kind`
on a filter and `--kind` on [`netviz schema`](schema.md) stay correct without
two lists being maintained. And every completer answers from the same registry
the command itself uses, so a format, kind, layer or rule added elsewhere
completes without a line of completion code changing.

zsh and fish show the descriptions next to each candidate; bash lists the values
alone, as it does for everything.

## Inventory-aware completion loads the inventory

This is the caveat worth knowing. The completers for elements, namespaces and
profiles **read the tree pointed at by `-i`** — the `-i` already on the command
line you are typing, so
`netviz -i examples/campus show sites/north/<TAB>` completes that site, and
without `-i` they read the current directory. There is no cache and no daemon:
each `<TAB>` loads and resolves the documents, which is what makes the names
correct and what makes a very large tree perceptibly slower to complete than a
small one.

Because of that they are written to **never fail loudly**. A tree that is
half-written — which is exactly when you reach for completion — simply offers
nothing: the load errors are collected rather than raised, a `netviz.toml` with
a typo in it yields an empty profile list rather than a traceback, and a `-i`
naming a directory that does not exist yet is not an error. A completer that
printed a diagnostic would corrupt the command line the user is in the middle of,
and one that raised would look like a broken install.

The script itself is generated from nothing but the command tree, so
`netviz completion` works before any inventory exists — installing completion is
a reasonable first thing to do after installing netviz, and
[`netviz init`](init.md) the second.

## Arguments

<!-- generated: arguments completion -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `{bash\|zsh\|fish\|powershell}` | yes | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options completion -->
*No options of its own; the global options apply.*
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The script was printed. |
| `2` | Usage error — a shell netviz cannot generate for. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

No inventory is read, so the codes for a missing or invalid tree cannot occur.

## See also

* [`docs/getting-started.md`](../getting-started.md#installation) — installing
  netviz, and the editor wiring that does for YAML what this does for the shell.
* [`netviz show`](show.md) and [`netviz list`](list.md) — the element names
  completion offers, and the command that lists them all.
* [`docs/configuration.md`](../configuration.md#profilename--named-variations) —
  the profile blocks `--profile <TAB>` reads.
