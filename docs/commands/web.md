# `netgraph web`

`netgraph web` opens an inventory in the browser and draws it as you edit it,
with an info box on every node and link. It has two faces, and which one you get
is decided by what you point it at:

* **a folder** — an *editing session*. The server holds the loaded tree, the page
  lists its files and the documents in them, and with `--write` the browser can
  change them. This is the one to use for an inventory.
* **anything else** — a file, a pipe, or nothing — a *scratchpad*. One YAML
  document stream, held in the browser, rendered as you type, written nowhere.
  This is the one to use for a snippet or a paste.

## Synopsis

<!-- generated: synopsis web -->
```text
netgraph [GLOBAL OPTIONS] web [OPTIONS] [SOURCE]
```
<!-- /generated -->

![The netgraph web interface: the YAML document stream on the left, the rendered layer-2 diagram on the right, and the info box open on a switch showing its interfaces, addresses, VLANs and links](../images/web.png)

<sub>Hovering `sw-home` in [`examples/home-lab`](../../examples/home-lab): every
port, its addresses and VLAN mode, and what each one is cabled to.</sub>

## The editing session

<!-- norun: every one of these starts a server and never exits -->
```bash
netgraph web ./inventory                # read-only: browse the tree and its diagram
netgraph web ./inventory --write        # read-write: save, undo and redo from the page
```

The window is three panes. On the left is the **inventory**: every file the
loader would read, grouped by namespace, with the documents each one declares
listed under it by kind and name. In the middle is the **file being edited** and
the problems found in the tree. On the right is the **diagram**.

The panes are wired to each other, which is the point of the command:

* **Selecting a file opens it**, whole, in the editor.
* **Selecting a node in the diagram reveals the document that declares it** — the
  right file, scrolled and selected at the right line. The shape carries the
  element's address, the address is in the file list against a file and a line,
  and that mapping comes from the same load the diagram was built from.
* **Clicking a problem navigates to its file *and* line**, opening the file if it
  is not the one on screen.

**Every file's state is shown as it is, not as it would be convenient.** A file
you have typed into is `unsaved`. A file that changed on disk while you had
unsaved edits in it is `conflict`, and netgraph will not resolve that for you: it
says so and leaves both versions alone until you decide. A file that was deleted
underneath you says `deleted on disk`.

### Writing

`--write` is off by default, and it is refused unless the server is bound to
loopback — publishing an endpoint that changes your inventory is not something a
flag should let you do by accident. Without it every read works and every write
answers `403`.

With it:

* **Ctrl-S** writes the open file back. The write carries the content hash the
  file was read at; if the file moved underneath, the save is refused and shown
  as a conflict rather than overwriting the other edit. Saving again after that
  overwrites deliberately.
* **Ctrl-Z / Ctrl-Y** undo and redo. The stack lives **on the server**, so it
  survives a page reload and a second tab sees the same history. Each entry is
  the exact inverse the [mutation layer](../editing.md) produced, so an undo
  restores bytes — the comment style, the quoting and the reference spellings of
  every document a rename rewrote.
* **A change that would break the tree is refused**, listing the problems, and
  can then be written anyway. This is the same gate
  [`netgraph edit`](edit.md) applies: an inventory that already fails
  `validate` can still be edited, one that would gain a *new* error cannot
  without saying so.

Every write goes through `netgraph.edit` — the same typed, reversible,
comment-preserving operations [`netgraph edit`](edit.md) applies from the command
line. The server constructs no YAML of its own, so untouched lines are not
rewritten and a diff of an edit shows the edit.

### Reconciliation

The session does not own the files. `watchfiles` watches the folder exactly as
[`netgraph watch`](watch.md) does, so an edit made in `$EDITOR`, a `git
checkout`, or a second netgraph process bumps the tree revision; the page polls
that number once a second and refetches the file list, the diagram and — if it
is not dirty — the file it has open. If the watch cannot start, the command says
so rather than leaving a page that is quietly stale.

### The API

The page is a client of a small JSON API, and so can anything else be. All of it
is on loopback and none of the write routes exist unless `--write` was given.

| Route | What it answers |
|---|---|
| `GET /api/state` | The tree revision, whether this session writes, and the undo/redo depth. |
| `GET /api/tree` | Every file, its content hash, its documents, and each document's kind, name, address and line. |
| `GET /api/graph?view=l2` | The resolved graph as an embeddable SVG, its info-box records, its problems and its stored [geometry](../editing.md). |
| `GET /api/file/<path>` | One file's text and the hash a write of it must quote. |
| `PUT /api/file/<path>` | That file back: `{"text": …, "hash": …}`. A stale `hash` is `409`; a new error is `422`, listing them; `"force": true` overrides the second, never the first. |
| `POST /api/ops` | `{"revision": …, "ops": [ … ]}` — a batch of [edit operations](../editing.md), applied atomically. Answers with the applied operations, their inverses, the files changed and the tree's diagnostics. |
| `POST /api/undo`, `POST /api/redo` | Move the server-side history one step. |

`<path>` is relative to the inventory root and is checked, not normalised: an
absolute path, a `..`, a component the loader skips and a suffix that is not
YAML are each refused by name. No other request ever becomes a file name.

## The scratchpad

<!-- norun: every one of these starts a server and never exits -->
```bash
netgraph web                                  # opens on the netgraph init example
netgraph web devices/sw-office.yaml           # seeded from one file
kubectl get cm topology -o jsonpath={..yaml} | netgraph web   # or from a pipe
```

The left pane holds a document stream — one or more documents separated by `---`
— and re-renders about half a second after you stop typing. **Nothing is
written.** The seed is read once, at startup; after that the stream lives in the
browser and every pass happens in memory, so the command cannot damage the file
it was seeded from and equally will not save your work: copy the text out before
you close the tab.

A stream has no folders and therefore **no namespaces**, and no file to write
back to. That is why `--write` is refused here and why a folder opens a session
instead.

## Both faces

**Hovering a node or a link opens an info box** holding what the diagram has no
room for: every interface with its type, MAC, MTU, addresses and VLAN mode; every
link that terminates on the element, what it runs to and over which port; and, at
layer 3, the prefix a subnet node stands for and who is addressed in it.
Everything it shows is the same data `netgraph render -f json` exports — the
records *are* that export — so the two cannot drift apart, and they are the same
records a committed SVG carries as tooltips
([`docs/rendering.md`](../rendering.md)). The element under the pointer and
everything it touches are lifted out of the diagram while the box is open; click
to pin the box, click again or press `Esc` to let go.

Beyond that: the layer, the VLAN filter and the display toggles are in the header
and apply on the next render; the canvas pans with a drag and zooms with the
wheel; and the splitter between the panes moves.

**Broken text still draws.** `netgraph render` refuses an inventory with errors
unless `--force`, because a diagram that disagrees with the files misinforms
whoever is shown it. Here the diagram *is* the feedback and text being edited is
wrong most of the time, so every problem is listed with its file and line and
whatever resolved is drawn anyway.

`netgraph.toml` decides how this machine *draws*: the `[render]` table and
`--profile` of the inventory named by `-i` — the current directory by default —
supply the settings this command has, `--icons` above all. A session also reads
the `[validate]` table of the folder it has open, so the problems it lists are
the ones [`netgraph validate`](validate.md) would list in that tree; a stream has
no folder of its own to look in and uses the built-in defaults plus the `strict`
toggle in the header.

`--icons` is chosen on the command line rather than in the browser for the same
reason `--write` is: it may name a directory on this machine, and a page has no
business reading one.

## The server

The same restrictions apply as to the [`watch` preview](watch.md) — loopback by
default, so publishing the interface with `--host` is an explicit act and is
warned about; a fixed set of routes; no request path ever turned into a file name;
a `Host` header check that keeps a loopback bind from being reached through a
rebound DNS name — plus three of its own: a request body is capped at 1 MB, the
SVG is parsed and stripped of anything that could execute or navigate before it is
put into the page, and no write route exists at all without `--write` on a
loopback bind. It is a development server: do not put it on a hostile network.

The default port is 8081, one above the `watch` preview's, so a watch run and an
editing session can be open at the same time. `--port 0` lets the operating system
choose one instead. `--open` — on by default — points the default browser at the
page once the server is listening; `--no-open` prints the URL and leaves the
browser alone, which is what you want over SSH.

## Arguments

<!-- generated: arguments web -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[SOURCE]` | no | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options web -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--host` | `ADDRESS` | `127.0.0.1` | Address to bind. The default keeps the interface on this machine; an inventory describes internal topology, so publishing it is an explicit act. |
| `--port` | `INTEGER, 0-65535` | `8081` | Port to bind. 0 lets the operating system choose one. |
| `--open`, `--no-open` | — | `--open` | Open the interface in the default browser once it is listening. |
| `--icons` | `THEME\|DIR` | — | Draw each element as an icon instead of a plain shape. Built in: cisco, none. Chosen here rather than in the browser, because it names a directory on this machine. |
| `--write`, `--read-only` | — | --read-only | Let the browser change the inventory. Only for a SOURCE folder, only on a loopback bind, and never by default: an editor that can write is a decision. |
| `--profile` | `NAME` | — | Apply the [profile.NAME] block of netgraph.toml on top of its [render] table. Explicit flags still win over both. |
| `--show-config` | — | off | Print the settings this invocation resolves to, and where each one came from, then exit without doing any work. |
<!-- /generated -->

## Exit codes

The interface is ended by Ctrl-C, which is how the command is meant to finish.
Text that does not parse is reported in the page, not by the process.

| Code | Meaning |
|---|---|
| 0 | The server ran and was stopped with Ctrl-C. |
| 2 | Usage error, an unusable `netgraph.toml`, or `--write` where it cannot be given. |
| 3 | A `SOURCE` folder could not be read. |
| 6 | The address could not be bound — usually something else on port 8081. |

## See also

* [`netgraph edit`](edit.md) — the same operations from the command line, and the
  layer every write in the browser goes through.
* [`netgraph watch`](watch.md) — the same live diagram without an editor, for a
  second screen.
* [`docs/editing.md`](../editing.md) — what an operation is, what an inverse
  promises, and how geometry is stored.
* [`docs/rendering.md`](../rendering.md) — the layers and display options the
  header exposes, and the tooltips the info box shares its records with.
* [`docs/inventory-layout.md`](../inventory-layout.md) — why a folder tree means
  namespaces and a stream does not.
* [`docs/validation.md`](../validation.md) — the problems the middle pane lists.
