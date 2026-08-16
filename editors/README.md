# Editor integration

netviz talks to editors two ways, and they compose:

* **[`netviz lsp`](../docs/lsp.md)** — a Language Server Protocol 3.17 server
  over stdio. Diagnostics, completion driven by the schema *and* by the names in
  your tree, hover, go to definition, find references, rename, formatting and
  quick fixes. Part of netviz; nothing to install separately.
* **[`schema/netviz.schema.json`](../schema/netviz.schema.json)** — the JSON
  Schema, for any YAML language server. Key completion and single-document
  validation with no netviz process running.

[`docs/lsp.md`](../docs/lsp.md) is the setup guide, and has the configuration for
VS Code, Neovim, Helix and Emacs, plus a table of what each of the two options
can and cannot answer.

## What is here

| Path | What it is |
|---|---|
| [`vscode/`](vscode) | A minimal VS Code client: `package.json`, forty lines of `extension.js`, and a `settings.json` you can copy into a workspace instead of building anything. |

Every other editor in the guide needs no code — the command is `netviz lsp` and
the language id is `yaml`.
