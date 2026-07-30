# `netgraph render`

Draw the inventory once and write the result somewhere. `render` is the
one-shot form of netgraph's rendering pipeline: pick a layer, narrow or summarise
the topology, and hand back a DOT file, an SVG, a self-contained HTML page, a
PNG, a PDF, a Mermaid flowchart or the resolved graph as JSON.

Every concept behind the flags — what each layer shows, how the filters combine,
what aggregation folds, what a theme is, what the JSON and HTML artefacts
contain — lives in [`docs/rendering.md`](../rendering.md), because
[`watch`](watch.md), [`web`](web.md) and [`path --highlight`](../paths.md) share
it. This page is the reference for the command itself.

## Synopsis

<!-- generated: synopsis render -->
```text
netgraph [GLOBAL OPTIONS] render [OPTIONS]
```
<!-- /generated -->

## Validation runs first

**Validation always runs before the render**, and errors refuse it: a diagram
silently drawn from an inventory with a dangling cable is worse than no diagram.

* `--force` renders anyway, with a warning on stderr, and marks what is missing
  — `-f json` grows a `dangling` key so an incomplete export says so.
* `--strict` promotes warnings to errors, which then also refuse the render.
  This is the setting for CI, where the diagram is an artefact somebody will
  trust.

Diagnostics go to **stderr**, always, because stdout may be the diagram itself.
That is what makes `netgraph render -f svg > topology.svg` safe.

See [`netgraph validate`](validate.md) for the checks and
[`docs/validation.md`](../validation.md) for how they are graded.

## Choosing a layer and a format

`--layer` picks the question the diagram answers: `l1` (the default) for the
physical topology, `l2` for the same topology annotated with VLANs, `l3` for the
IP subnets and who is addressed in them, `overlay` for the tunnels and what runs
inside what, `routing` for the BGP sessions and OSPF adjacencies clustered by
VRF, `physical` for the cabling record with its patch panels, and `rack` for a
front elevation per rack. The table in
[Layers](../rendering.md#layers-one-inventory-seven-questions) says what each one
draws and when to reach for it.

`-f/--format` decides what the artefact is. `svg`, `html`, `png` and `pdf` need
Graphviz on the `PATH`; `dot`, `mermaid` and `json` do not. The comparison table
is in [Output formats](../rendering.md#output-formats).

`--layer` is repeatable **only** for `-f html`, which draws each layer and puts a
switcher over them. Every other format holds one layer, and asking for two is a
usage error.

`-o/--output` writes to a file instead of stdout and creates parent directories
on the way. It is required for `png` and `pdf` when stdout is a terminal —
netgraph will not spray a binary at your prompt.

## Worked examples

A layer-3 view of one router and the prefixes it routes. The subnet nodes are
derived, so `--kind router` keeps the prefixes the surviving routers are
addressed in and never an empty one:

<!-- run: -->
```console
$ netgraph -i examples/home-lab render --layer l3 --kind router -f mermaid
flowchart TB
    n0(["rtr-home<br/>[router]<br/>vlans: 10"])
    n1("192.0.2.1/32<br/>[ipv4 subnet]")
    n2("192.168.10.0/24<br/>[ipv4 subnet]<br/>vlans: 10")
    n3("203.0.113.0/30<br/>[ipv4 subnet]")
    n4("2001:db8::1/128<br/>[ipv6 subnet]")
    n5("2001:db8:10::/64<br/>[ipv6 subnet]<br/>vlans: 10")

    n0 -- "lo0 · 192.0.2.1/32" --- n1
    n0 -- "lo0 · 2001:db8::1/128" --- n4
    n0 -- "wan0 · 203.0.113.2/30" --- n3
    n0 -- "lan0 · 192.168.10.1/24" --- n2
    n0 -- "lan0 · 2001:db8:10::1/64" --- n5

    classDef router fill:#dbe9f6,stroke:#2563eb,stroke-width:1px
    classDef subnet fill:#e0f2f1,stroke:#0f766e,stroke-width:1px
    class n0 router
    class n1,n2,n3,n4,n5 subnet
rendered 6 node(s) and 5 edge(s) as mermaid at layer l3
```

A warning is emitted when a flag cannot reach the format you picked, rather than
the flag being dropped in silence:

<!-- run: -->
```console
$ netgraph -i examples/quickstart render --icons cisco -f mermaid
flowchart TB
...
warning: --icons is ignored for mermaid output, which has no picture to put an icon in; the formats that draw icons are dot, svg, html, png, pdf
rendered 3 node(s) and 2 edge(s) as mermaid at layer l1
```

The everyday invocations, illustrative paths and all:

<!-- norun: a shell pipeline, and the output paths are illustrative -->
```bash
netgraph render -f json | jq '.nodes[].name'
netgraph render -f mermaid -o docs/topology.mmd
netgraph render --vlan 10 --layer l2 -f svg -o vlan-10.svg
netgraph render --layer l3 -f svg -o subnets.svg
netgraph render --neighbors-of sw-dist-01 --depth 2 -f svg -o around-dist.svg
netgraph render --kind switch --kind router --group-by-namespace -o core.dot
netgraph render --collapse-depth 1 --group-by-namespace -f svg -o overview.svg
netgraph render -f html --layer l1 --layer l2 --layer l3 -o topology.html
```

## Retyping none of it

Every option except `-o/--output`, `--force` and `--show-config` can be given a
default in `netgraph.toml`, so a team retypes none of them — see
[`[render]`](../configuration.md#render--how-the-inventory-is-drawn) and the
[full key list](../configuration.md#every-render-setting). A key is the long flag
without its leading dashes.

`--profile NAME` applies the
[`[profile.NAME]`](../configuration.md#profilename--named-variations) block on top
of `[render]`; explicit flags still win over both. `--show-config` prints the
settings this invocation resolves to, and where each one came from, then exits
without doing any work — which is the fastest way to find out why a diagram does
not look the way you expected.

## Arguments

<!-- generated: arguments render -->
*Takes no positional arguments.*
<!-- /generated -->

## Options

<!-- generated: options render -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-f`, `--format` | `[dot\|svg\|html\|png\|pdf\|mermaid\|json]` | `dot` | Output format. dot: Graphviz DOT source; svg: SVG image, via Graphviz; html: self-contained interactive page, via Graphviz; png: PNG image, via Graphviz; pdf: PDF document, via Graphviz; mermaid: Mermaid flowchart, for embedding in Markdown; json: node-link JSON, for downstream tooling. |
| `-o`, `--output` | `FILE` | — | Write to this file instead of stdout. |
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
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The diagram was produced. |
| `1` | The inventory was rejected: validation found errors (or, under `--strict`, warnings) and `--force` was not given. |
| `2` | Usage error, or an unusable `netgraph.toml` — including two `--layer` values for a format that holds one, and `--layer rack` with `-f mermaid`. |
| `3` | The inventory could not be discovered or read at all. |
| `5` | The rendering could not be produced: Graphviz is missing, the output path is not writable, or a binary format was aimed at a terminal. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

## See also

* [`docs/rendering.md`](../rendering.md) — the layers, the filters, aggregation,
  icons, tooltips, the HTML page and the JSON shape, in full.
* [`netgraph watch`](watch.md) and [`netgraph web`](web.md) — the same pipeline,
  redrawn on every save.
* [`netgraph path --highlight`](../paths.md#drawing-the-answer---highlight) — one
  traced route drawn over the topology it crosses.
* [`docs/configuration.md`](../configuration.md) — render defaults and named
  profiles.
* [`netgraph export`](export.md) — the other artefacts one inventory can
  produce.
