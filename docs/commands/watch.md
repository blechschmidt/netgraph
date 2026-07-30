# `netgraph watch`

`netgraph watch` re-renders whenever a file in the inventory changes, optionally
serving the result on a page that reloads itself. Every cycle is the same load,
validate and render [`netgraph render`](render.md) performs, followed by a
timestamped status line and any findings. It is the command to leave running in
a second terminal while you edit YAML in the first.

## Synopsis

<!-- generated: synopsis watch -->
```text
netgraph [GLOBAL OPTIONS] watch [OPTIONS]
```
<!-- /generated -->

## The status line

One line per cycle, with the time, the status and what came of it:

```
09:41:02  ok       23 nodes, 26 edges → topology.svg (128 ms)
09:41:37  invalid  1 error; keeping the render from before
errors (1):
  sites/hq/links.yaml#0:12  E001  cable 'sites/hq/cbl-07' endpoint sw-hq:port9: no element named 'sw-hq' is declared in this inventory
```

**A failed cycle changes nothing.** The file written by `--output` keeps its last
valid contents and the preview keeps serving the last valid diagram, so a
half-typed document never blanks the picture you are working from — which is why
the line says *keeping the render from before* rather than reporting a size of
zero. Nothing ends the loop except Ctrl-C: a syntax error, a deleted root, a
`--neighbors-of` target that no longer resolves are all statuses, not crashes.

With `--output` the file is rewritten atomically, so a reader — a browser with
the SVG open, a static site build — sees the old diagram or the new one, never
half of each. Naming a file inside the tree being watched is fine: the render's
own output is excluded from the watch, which is what keeps the loop from feeding
itself forever.

Giving neither `--output` nor `--serve` is allowed and warned about: each render
is then checked and discarded, which is occasionally what you want and usually
not what you meant.

## What triggers a render

Only YAML documents, `netgraph.toml` and `.netgraphignore` trigger a render; an
editor swap file or a rendered diagram does not, and neither does anything under
a directory the loader skips (`.git/`, `_drafts/`, …) — the same rule the loader
itself applies, so what the watcher reacts to is what the inventory contains. A
single-file inventory is watched through its directory, because editors replace a
file rather than rewrite it and a watch on the file itself would not survive the
first save.

`--debounce` (300 ms by default) is how long a burst of filesystem events is
collected before re-rendering. One editor save is several events — a temporary
file, a rename, a metadata change — and re-rendering each of them would render
three times and show the middle one.

## Every render option applies

Every filter and display option of `netgraph render` applies here too —
`--tooltips`, `--link-template` and `--element-ids` included, which is what makes
`watch -f svg -o topology.svg` keep an *interactive* diagram up to date. `-f
html` works the same way, repeated `--layer` included, so `watch -f html -o
topology.html --serve` gives you the whole interactive page, re-rendered as you
type. `-f` defaults to `svg` here rather than `dot`, because a live preview wants
a picture.

[`docs/rendering.md`](../rendering.md) explains the layers, the filters, the
aggregation and the display options themselves; this command adds nothing to
them and changes none of their defaults.

## The live preview

`--serve` also hosts the render over HTTP. The page polls once a second and swaps
the diagram in when it changes — polling rather than server-sent events keeps the
client a dozen lines of dependency-free JavaScript.

**The preview is bound to loopback and stays there unless you say otherwise.** An
inventory describes internal network topology — addresses, VLANs, what is plugged
into what — so `--host` is the explicit act of publishing it, and doing so prints
a warning. The server answers `GET` and `HEAD` on five fixed routes, never turns
a request path into a file name, and refuses a request that reached a loopback
preview under a foreign `Host` header. It is a development server: do not put it
on a hostile network.

`--host` and `--port` describe that server, so both require `--serve`; given
without it they are a usage error rather than a flag that appeared to work and
never did. `--port 0` lets the operating system choose one, and the address
actually bound is printed.

<!-- norun: every one of these starts a server or a loop and never exits -->
```bash
netgraph watch --serve                                   # preview at http://127.0.0.1:8080/
netgraph watch -f svg -o topology.svg                    # just keep a file up to date
netgraph watch --serve --layer l2 --vlan 10 --title vlan10
netgraph watch --serve --host 0.0.0.0 --port 9000        # deliberate, and warned about
```

## Arguments

<!-- generated: arguments watch -->
*Takes no positional arguments.*
<!-- /generated -->

## Options

<!-- generated: options watch -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-f`, `--format` | `[dot\|svg\|html\|png\|pdf\|mermaid\|json]` | `svg` | Output format. Defaults to svg, which is what a live preview wants. |
| `-o`, `--output` | `FILE` | — | Rewrite this file after every successful render. |
| `--namespace` | `NS` | — | Keep only elements in this namespace or below it. Repeatable. |
| `--vlan` | `VID` | — | Keep only elements participating in this VLAN. Repeatable. |
| `--kind` | `[switch\|router\|hub\|computer\|server\|adapter\|patchpanel]` | — | Keep only elements of this kind. Repeatable. |
| `--name` | `GLOB` | — | Keep only elements whose name matches this glob. Repeatable. |
| `--neighbors-of` | `NAME` | — | Keep only the neighbourhood of this element. |
| `--depth` | `INTEGER, >= 0` | `1` | How many hops --neighbors-of reaches. |
| `--collapse` | `NS` | — | Replace this namespace and everything under it with one node, labelled with what it holds. Links crossing the boundary attach to it; links inside it are counted rather than drawn. Repeatable. |
| `--collapse-depth` | `N` | — | Collapse every namespace N levels deep, counted from the shallowest one that branches: '--collapse-depth 1' is the site-level overview of a tree laid out as sites/<site>/<tier>. |
| `--bundle-links`, `--no-bundle-links` | — | — | Draw parallel links between the same pair of elements as one edge, with the count in the label. Members of a declared 'lag' interface are bundled either way unless --no-bundle-links is given, since the inventory already says they are one logical link. |
| `--show-ips`, `--no-show-ips` | — | `--show-ips` | Print configured IP addresses on the nodes. |
| `--show-vlans`, `--no-show-vlans` | — | `--show-vlans` | Annotate nodes and links with VLAN membership. |
| `--group-by-namespace` | — | off | Draw each namespace as a visual group. |
| `--icons` | `THEME\|DIR` | — | Draw each element as an icon instead of a plain shape. Built in: cisco, none. A directory of images named after element kinds (router.svg, switch.png, ...) also works. Graphviz formats only. |
| `--tooltips`, `--no-tooltips` | — | `--tooltips` | Carry the full detail of each element — interfaces, addresses, VLANs, cabling — as hover text. Reaches a reader in svg output; png and pdf have nowhere to put it. |
| `--link-template` | `URL` | — | Link each element back to the YAML that declares it, e.g. 'https://git.example.com/net/blob/main/{file}#L{line}'. Placeholders: {file}, {line}, {name}, {namespace}, {kind}. dot and svg only. |
| `--element-ids` | — | off | Give every node, edge and namespace a stable id derived from its name, so the diagram can be deep-linked and styled from outside. dot and svg only. |
| `--max-addresses` | `N` | `4` | Longest address list spelled out under a node before it is abbreviated to 'and N more'. 0 prints the count alone. |
| `--rankdir` | `[tb\|lr\|bt\|rl]` | TB, top to bottom | Layout direction. A wide network reads better left to right; a deep one top to bottom. Honoured by the Graphviz backends and by mermaid. |
| `--title` | `TEXT` | — | Caption for the diagram. |
| `--layer` | `[physical\|l1\|l2\|l3\|overlay\|routing\|rack]` | `l1` | l1 draws the physical topology; l2 annotates it with VLANs; l3 draws IP subnets and the elements addressed in them; overlay draws the tunnels; routing draws the BGP sessions and OSPF adjacencies, clustered by VRF; physical adds the patch panels l1 splices out; rack draws a front elevation per rack. Repeatable for -f html, which draws each layer and puts a switcher over them. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Proceed even when validation failed. The result may not match the files. |
| `--profile` | `NAME` | — | Apply the [profile.NAME] block of netgraph.toml on top of its [render] table. Explicit flags still win over both. |
| `--show-config` | — | off | Print the settings this invocation resolves to, and where each one came from, then exit without doing any work. |
| `--serve` | — | off | Also host the render over HTTP, on a page that reloads itself. |
| `--host` | `ADDRESS` | `127.0.0.1` | Address --serve binds to. The default keeps the preview on this machine; an inventory describes internal topology, so publishing it is an explicit act. |
| `--port` | `INTEGER, 0-65535` | `8080` | Port --serve binds to. 0 lets the operating system choose one. |
| `--debounce` | `MS` | `300` | How long a burst of filesystem events is collected before re-rendering. |
<!-- /generated -->

## Exit codes

A watch is ended by Ctrl-C, which is how the command is meant to finish and not a
failure. Everything an inventory can do wrong is a status line instead of an exit
status.

| Code | Meaning |
|---|---|
| 0 | The loop ran and was stopped with Ctrl-C. |
| 2 | Usage error: `--host` or `--port` without `--serve`, an unusable `netgraph.toml`. |
| 6 | `--serve` could not bind its address — usually a preview already running on that port. |

## See also

* [`netgraph render`](render.md) and [`docs/rendering.md`](../rendering.md) — the
  render every cycle performs, and every option this command inherits.
* [`netgraph web`](web.md) — the same live feedback for a document stream you
  edit in the browser rather than a tree you edit on disk.
* [`docs/validation.md`](../validation.md) — the findings a cycle reports, and
  what `--strict` promotes.
