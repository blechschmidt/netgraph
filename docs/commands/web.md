# `netgraph web`

`netgraph web` edits a YAML document stream in the browser and draws it as you
type, with an info box on every node and link. It is the command for a snippet, a
paste or a single file — a scratchpad with a validator and a renderer attached,
rather than a way to maintain a tree. For a tree, use
[`netgraph watch --serve`](watch.md).

## Synopsis

<!-- generated: synopsis web -->
```text
netgraph [GLOBAL OPTIONS] web [OPTIONS] [SOURCE]
```
<!-- /generated -->

![The netgraph web interface: the YAML document stream on the left, the rendered layer-2 diagram on the right, and the info box open on a switch showing its interfaces, addresses, VLANs and links](../images/web.png)

<sub>Hovering `sw-home` in [`examples/home-lab`](../../examples/home-lab): every
port, its addresses and VLAN mode, and what each one is cabled to.</sub>

## The two panes

On the left is the document stream — one or more documents separated by `---` —
and the problems found in it; on the right is the diagram, which re-renders about
half a second after you stop typing. The same load, validate and render the
command line performs runs on every pass, so what the page reports is what
[`netgraph validate`](validate.md) would say about the same text.

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
wheel; clicking a problem puts the cursor on the line that caused it; and the
splitter between the panes moves.

**Broken text still draws.** `netgraph render` refuses an inventory with errors
unless `--force`, because a diagram that disagrees with the files misinforms
whoever is shown it. Here the diagram *is* the feedback and text being edited is
wrong most of the time, so every problem is listed with its line and whatever
resolved is drawn anyway.

## Seeding the editor, and what becomes of your edits

`SOURCE` is the seed. It may be a file, or a folder whose documents are
concatenated into one stream; `-` reads the stream from standard input, as does a
pipe, and a pipe wins over both. With no `SOURCE` at all the editor opens on the
same example topology [`netgraph init`](init.md) writes, so there is always
something on screen to take apart.

<!-- norun: every one of these starts a server and never exits -->
```bash
netgraph web                                  # opens on the netgraph init example
netgraph web examples/home-lab                # seeded from a folder, flattened into one stream
netgraph web devices/sw-office.yaml           # seeded from one file
kubectl get cm topology -o jsonpath={..yaml} | netgraph web   # or from a pipe
```

**Nothing is written.** The seed is read once, at startup; after that the stream
lives in the browser and every pass — parse, validate, render — happens in
memory. No file is opened and none is created, so the command cannot damage the
inventory it was seeded from, and equally it will not save your work: copy the
text out of the pane before you close the tab.

A stream has no folders and therefore **no namespaces**: every element seeded from
a tree lands in the root namespace, and two elements that shared a short name in
different folders will collide. Seeding from a nested folder says so on stderr.
Deep trees belong in `netgraph watch --serve`; this command is for a snippet, a
paste or a file.

`netgraph.toml` decides very little here. The `[render]` table and `--profile` of
the inventory named by `-i` — the current directory by default — supply the
settings this command has, `--icons` above all: that file describes how *this
machine* draws, not what the text being edited means. The validation rules are the
built-in defaults plus the `strict` toggle in the header, because the stream has
no folder of its own to look for a configuration in.

`--icons` is chosen on the command line rather than in the browser for the same
reason: it may name a directory on this machine, and a page has no business
reading one.

## The server

The same restrictions apply as to the [`watch` preview](watch.md) — loopback by
default, so publishing the interface with `--host` is an explicit act and is
warned about; a fixed set of routes; no request path ever turned into a file name;
a `Host` header check that keeps a loopback bind from being reached through a
rebound DNS name — plus two of its own: a request body is capped at 1 MB, and the
SVG is parsed and stripped of anything that could execute or navigate before it is
put into the page. It is a development server: do not put it on a hostile network.

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
| `--profile` | `NAME` | — | Apply the [profile.NAME] block of netgraph.toml on top of its [render] table. Explicit flags still win over both. |
| `--show-config` | — | off | Print the settings this invocation resolves to, and where each one came from, then exit without doing any work. |
<!-- /generated -->

## Exit codes

The interface is ended by Ctrl-C, which is how the command is meant to finish.
Text that does not parse is reported in the page, not by the process.

| Code | Meaning |
|---|---|
| 0 | The server ran and was stopped with Ctrl-C. |
| 2 | Usage error, or an unusable `netgraph.toml`. |
| 3 | A `SOURCE` folder could not be read. |
| 6 | The address could not be bound — usually something else on port 8081. |

## See also

* [`netgraph watch`](watch.md) — the same live feedback for an inventory on disk,
  namespaces and `netgraph.toml` included.
* [`docs/rendering.md`](../rendering.md) — the layers and display options the
  header exposes, and the tooltips the info box shares its records with.
* [`docs/inventory-layout.md`](../inventory-layout.md) — why a folder tree means
  namespaces and a stream does not.
* [`docs/validation.md`](../validation.md) — the problems the left pane lists.
