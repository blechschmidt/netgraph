# netviz in your editor

`netviz lsp` is a Language Server Protocol 3.17 server for inventory YAML. It
gives an editor the things netviz knows and a YAML mode cannot:

* **Diagnostics** — everything [`netviz validate`](commands/validate.md) reports,
  on the line and column that caused it, as you type. Each carries its `NV-*`
  rule id as the diagnostic code and a link to that rule's own section of
  [`docs/validation-rules.md`](validation-rules.md).
* **Completion** — the keys the schema allows at the cursor with their
  documentation, the values of every enum, and, for a reference, *the element
  names actually in your tree*: typing `- ` under a cable's `endpoints` offers
  your switches, and typing `sw-home:` offers the ports that switch has.
* **Hover** — what a reference resolves to. A cable endpoint hovers as the
  device, the port, its addresses, its VLAN and what is already cabled to it.
* **Go to definition and find references** — jump from a cable to the switch it
  lands on, or ask a switch which cables terminate on it, across the whole
  folder.
* **Rename** — through the same write path as
  [`netviz edit rename`](commands/edit.md), so every reference in every file is
  rewritten, in the spelling its author chose, with comments preserved.
* **Format** — [`netviz fmt`](commands/fmt.md), as format-on-save.
* **Quick fixes** — the repairs from
  [`netviz validate --fix`](validation-rules.md#fixing-a-finding), offered on
  the finding they repair.

There is no separate install: the server is part of netviz, and the command is
`netviz lsp`. There is also no new dependency — LSP's transport is JSON-RPC in
`Content-Length` frames, and netviz speaks it directly.

* [Before you start](#before-you-start)
* [VS Code](#vs-code)
* [Neovim](#neovim)
* [Helix, Emacs, and everything else](#helix-emacs-and-everything-else)
* [The JSON Schema, and when to use it instead](#the-json-schema-and-when-to-use-it-instead)
* [The lone file](#the-lone-file)
* [How it behaves](#how-it-behaves)
* [When it is not working](#when-it-is-not-working)

## Before you start

netviz has to be on the `PATH` of the process your editor spawns, which is not
always the shell you tested it in.

<!-- run: -->
```console
$ netviz lsp --help
Usage: netviz lsp [OPTIONS]

  Serve inventory YAML to an editor over the Language Server Protocol.
...
```

If your editor cannot find it, give the absolute path to the executable in the
configuration below — `command = "/home/you/.venvs/netviz/bin/netviz"` — or
run it as `python -m netviz lsp`.

## VS Code

The repository ships the client under [`editors/vscode/`](../editors/vscode).

**The quick way**, without building anything: install any generic LSP client
extension and point it at the command. With
[vscode-glspc](https://marketplace.visualstudio.com/items?itemName=SanaAjani.taskrunnercode)
or a similar bridge, the settings are the ones in
[`editors/vscode/settings.json`](../editors/vscode/settings.json), which you can
copy into your workspace's `.vscode/settings.json`:

```json
{
  "files.associations": { "**/*.yaml": "yaml" },
  "yaml.schemas": {
    "./schema/netviz.schema.json": ["**/*.yaml", "**/*.yml"]
  },
  "netviz.lsp.command": "netviz",
  "netviz.lsp.args": ["lsp"]
}
```

**The real client** is
[`editors/vscode/package.json`](../editors/vscode/package.json) and
[`editors/vscode/extension.js`](../editors/vscode/extension.js): about forty
lines of `vscode-languageclient`, which is all a client needs to be. To run it
from source:

```bash
cd editors/vscode
npm install
code --extensionDevelopmentPath="$PWD" /path/to/your/inventory
```

It contributes one setting, `netviz.server.path`, for a netviz that is not
on the `PATH`, and one command, **netviz: Restart language server**.

Turn on format-on-save for YAML and `netviz fmt` runs on every save:

```json
{
  "[yaml]": {
    "editor.formatOnSave": true,
    "editor.defaultFormatter": "netviz.netviz"
  }
}
```

## Neovim

Neovim 0.11 or newer, using the built-in client. No plugin is required.

```lua
-- ~/.config/nvim/lsp/netviz.lua
return {
  cmd = { 'netviz', 'lsp' },
  filetypes = { 'yaml' },
  -- An inventory is a folder, and the folder is what makes cross-document
  -- checks mean anything. netviz.toml marks the root when there is one;
  -- otherwise the repository is a good guess.
  root_markers = { 'netviz.toml', '.netvizignore', '.git' },
}
```

```lua
-- ~/.config/nvim/init.lua
vim.lsp.enable('netviz')

vim.api.nvim_create_autocmd('BufWritePre', {
  pattern = { '*.yaml', '*.yml' },
  callback = function() vim.lsp.buf.format({ timeout_ms = 2000 }) end,
})
```

On Neovim 0.10 and earlier, register it with `lspconfig` instead:

```lua
local configs = require('lspconfig.configs')
configs.netviz = {
  default_config = {
    cmd = { 'netviz', 'lsp' },
    filetypes = { 'yaml' },
    root_dir = require('lspconfig.util').root_pattern('netviz.toml', '.netvizignore', '.git'),
  },
}
require('lspconfig').netviz.setup({})
```

`gd` goes to a definition, `grr` lists references, `grn` renames and `gra` offers
the quick fixes; those are Neovim's own defaults for a server that provides them.

## Helix, Emacs, and everything else

Any client that can spawn a process and speak LSP over stdio works. The command
is `netviz lsp` and the language id is `yaml`.

Helix, in `languages.toml`:

```toml
[language-server.netviz]
command = "netviz"
args = ["lsp"]

[[language]]
name = "yaml"
language-servers = ["netviz", "yaml-language-server"]
```

Listing netviz alongside a general YAML server is deliberate and works: the two
answer different questions, and Helix merges what they return.

Emacs, with `eglot`:

```elisp
(add-to-list 'eglot-server-programs '(yaml-mode . ("netviz" "lsp")))
```

## The JSON Schema, and when to use it instead

netviz publishes [`schema/netviz.schema.json`](../schema/netviz.schema.json),
and [`netviz schema`](commands/schema.md) regenerates it. Point a YAML language
server at it and you get key completion and structural validation without running
netviz at all.

That is a real option, and it is strictly less than the server gives you:

| | JSON Schema | `netviz lsp` |
|---|---|---|
| Keys, types, enums, field documentation | yes | yes |
| One document is well-formed | yes | yes |
| Does this cable endpoint resolve? | no | yes |
| Which ports does this switch have? | no | yes |
| Is this address already used in this subnet? | no | yes |
| Go to definition, find references, rename | no | yes |
| Quick fixes | no | yes |

Running both is fine and is what the VS Code settings above do. The schema
catches a misspelt key before the server has loaded, and the server catches
everything that spans more than one document.

## The lone file

If your editor opens a single file rather than a folder — `nvim switch.yaml` from
anywhere — there is no tree to resolve against. The server says so by holding
back the rules that can only be judged against one, rather than reporting them
against a document that has no way to satisfy them:

| Rule | Why it needs the folder |
|---|---|
| `NV-C002`/`NV-C003` (`E001`) | whether a cable endpoint resolves |
| `E015`, `E016`, `E018`, `E043` | whether an adapter's host, a tunnel's endpoint or underlay, or a group's member exists |
| `E021`, `E023`, `E038` | whether a panel position or a PDU outlet exists |
| `W103`, `W121`, `I002` | whether anything is cabled to this at all |
| `W125`, `W128`, `W133`, `W135`, `W137`, `W138` | whether the other end of something is declared anywhere |

Everything else is reported in full. Rename needs the folder too — netviz will
not rewrite references it cannot see — and says so rather than renaming half of
them.

Opening the folder is always better. This mode exists so that opening one file is
useful rather than alarming.

## How it behaves

**It answers about what is on your screen.** Every open buffer is overlaid on the
tree before it is loaded, so an unsaved edit is part of the inventory as far as
every answer is concerned — including a rename, which rewrites the buffer's text
rather than the file's.

**It reloads rather than patches.** An inventory loads in milliseconds and its
documents cross-reference each other; a model kept in step by hand would drift
the first time something surprised it. Diagnostics are recomputed when the queue
of incoming messages runs dry, so a burst of keystrokes costs one reload rather
than one per character.

**It watches the folder** exactly as [`netviz watch`](commands/watch.md) does,
with the same filter and the same debounce, so `git checkout`, `netviz fmt` or a
colleague's `rsync` refreshes the diagnostics without your editor noticing
anything. `--no-watch` turns it off.

**It respects `netviz.toml`.** The `[validate]` section — `ignore`, `severity`,
`strict` — grades the findings the same way it does for
[`netviz validate`](commands/validate.md), so a rule your inventory has decided
not to care about is not squiggled at you either.
[`docs/configuration.md`](configuration.md) has the file.

**A quick fix is applied under the same gate as `--fix`.** The repair is thrown
away unless re-validating shows the finding gone and no rule reporting more than
it did before. An action that would make things worse is not offered.

## When it is not working

**Nothing at all happens.** The server is started by the editor, so the first
question is whether the editor could start it. Run `netviz lsp --log
/tmp/netviz-lsp.log` from your editor's configuration and look at the file; if
it is not created, the command never ran. In VS Code the *Output* panel has a
`netviz` channel; in Neovim, `:LspLog` and `:checkhealth vim.lsp`.

**Diagnostics appear but references do not resolve.** The client opened a file
rather than a folder. `:lua =vim.lsp.get_clients()[1].root_dir` in Neovim, or the
`rootUri` in the log, says which. See [the lone file](#the-lone-file).

**Diagnostics are for the wrong lines.** A client and a server have to agree on
how a column is counted. netviz negotiates `positionEncoding` and implements
UTF-8, UTF-16 and UTF-32; if your client advertises none of them the protocol's
default, UTF-16, is used. This is only ever visible on a line with a character
outside the basic multilingual plane.

**It is slow on a large inventory.** Every keystroke reloads the tree. The parse
cache does not apply while a buffer is open — the bytes being validated are not
the bytes on disk — so the cost is a full parse of the folder. On a tree where
that is noticeable, `netviz --no-cache validate` is the same measurement from
the command line, and
[`tools/bench_incremental.py`](../tools/bench_incremental.py) is the benchmark it
came from.

**A rename is refused.** Interface names are not renameable: rewriting one has to
rewrite the cable endpoints that land on it, and the write path has no operation
for that yet. Element names are. `prepareRename` says which is which before you
type anything.

## See also

* [`netviz lsp`](commands/lsp.md) — the command's own reference page.
* [`docs/validation.md`](validation.md) — what the diagnostics mean and how to
  silence one.
* [`docs/editing.md`](editing.md) — the write path the rename and the quick fixes
  go through.
* [`netviz web`](commands/web.md) — the other editor, where the diagram is the
  thing you edit.
