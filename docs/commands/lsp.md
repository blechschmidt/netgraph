# `netviz lsp`

`netviz lsp` is netviz in your editor: the same diagnostics `netviz
validate` prints, on the line that caused them, as you type; completion that
offers the ports a switch actually has; a rename that rewrites every reference
in every file with the comments intact.

It is a Language Server Protocol 3.17 server over stdio, so you do not run it —
your editor does. [`docs/lsp.md`](../lsp.md) is the setup guide, with the
configuration for VS Code and Neovim. This page is the reference for the
command.

## Synopsis

<!-- generated: synopsis lsp -->
```text
netviz [GLOBAL OPTIONS] lsp [OPTIONS]
```
<!-- /generated -->

The transport is stdin and stdout, framed as the protocol's base protocol
requires. Nothing else is written to either: a message meant for a human goes to
the client as `window/logMessage`, and `--log` is there for the rest, because
several editors treat anything on stderr as a crashed server.

## What it answers, and what answers it

Nothing here is a second implementation of anything. Each capability is the
command you already run, wired to a request:

| Capability | Answered by |
|---|---|
| `textDocument/publishDiagnostics` | [`netviz validate`](validate.md) |
| `textDocument/completion` | [`netviz schema`](schema.md), plus the names in the tree |
| `textDocument/hover` | the loader's provenance and the resolved references |
| `textDocument/definition`, `textDocument/references` | the same reference table [`netviz edit rename`](edit.md) uses |
| `textDocument/rename` | [`netviz edit rename`](edit.md) |
| `textDocument/formatting` | [`netviz fmt`](fmt.md) |
| `textDocument/codeAction`, `codeAction/resolve` | [`netviz validate --fix`](validate.md) |
| `textDocument/documentSymbol` | the elements each file declares |

That is the point rather than an implementation note. A diagnostic you see in
the editor and a diagnostic that fails CI are the same sentence about the same
line, and a rename made from the editor is byte-for-byte the rename
`netviz edit rename` would have made.

## The folder, and the file

**With a folder open** — which is what your editor tells the server in
`initialize` — the whole tree is loaded and every check means what it says.
Cross-document rules are the reason to use netviz at all: whether a cable
endpoint resolves is not a question one file can answer.

**With a lone file open** there is no tree, so the rules that need one are held
back rather than reported against a document that cannot satisfy them. A cable
file on its own would otherwise light up with "endpoint references an unknown
device" on every line. Everything a single document can be wrong about by itself
— its syntax, its schema, its own interfaces, addresses and VLANs — is still
reported. [`docs/lsp.md`](../lsp.md#the-lone-file) lists exactly which rules are
held back.

`-i`/`--inventory` is only the fallback for the second case. An editor that
opened a workspace knows better than the shell the server was spawned from.

## Unsaved buffers, and the folder underneath

The server always answers about the text on your screen: every open buffer is
overlaid on the tree before it is loaded, so a diagnostic about an edit you have
not saved is still correct, and a rename computed from an unsaved buffer rewrites
what is in it rather than what is on disk.

It also watches the folder, the same way [`netviz watch`](watch.md) does and
with the same filter, so a `git checkout` or a `netviz edit` run in a terminal
refreshes the diagnostics without your editor having to notice. `--no-watch`
turns that off for a client that does its own file watching and sends
`workspace/didChangeWatchedFiles`.

## Options

<!-- generated: options lsp -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--stdio` | — | on | Speak the protocol over stdin and stdout. The only transport; accepted because most clients pass it. |
| `--watch`, `--no-watch` | — | `--watch` | Watch the folder, so an edit made outside the editor refreshes diagnostics. |
| `--log` | `FILE` | — | Append a trace of what the server did to FILE. Never stderr: some clients treat anything there as a crash. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The client sent `shutdown` and then `exit`, as the protocol requires. |
| `1` | The client sent `exit` without `shutdown`, closed the stream, or wrote a frame that could not be parsed. |

## See also

* [`docs/lsp.md`](../lsp.md) — setting it up in VS Code and Neovim, and what to
  do when it is not working.
* [`netviz validate`](validate.md) — the same checks from the command line, and
  the `--fix` catalogue the code actions come from.
* [`docs/validation-rules.md`](../validation-rules.md) — every rule, which the
  `NG-*` code on each diagnostic links to.
* [`netviz web`](web.md) — the other editor: the diagram and the YAML side by
  side, in a browser.
