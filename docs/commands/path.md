# `netgraph path`

`netgraph path SRC DST` traces how one element reaches another, and what the
traffic crosses on the way — hop by hop, layer-aware, over the topology the
files declare. Nothing is pinged and no device is contacted, which is the point:
it tells you what your documentation says should happen, and that is exactly the
thing to compare against what does. This page is the reference for the command;
[`docs/paths.md`](../paths.md) is the full treatment of how the search decides,
what the JSON carries, and what the trace deliberately does not model.

---

## Contents

- [Synopsis](#synopsis)
- [Naming the two ends](#naming-the-two-ends)
- [What the trace searches](#what-the-trace-searches)
- [A switched path](#a-switched-path)
- [A routed path](#a-routed-path)
- [Several paths, and none](#several-paths-and-none)
- [Drawing the answer: `--highlight`](#drawing-the-answer---highlight)
- [Output for a program](#output-for-a-program)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)

---

## Synopsis

<!-- generated: synopsis path -->
```text
netgraph [GLOBAL OPTIONS] path [OPTIONS] SRC DST
```
<!-- /generated -->

## Naming the two ends

`SRC` and `DST` are each one of three spellings, and which one you meant is
decided by the shape of the argument rather than by a flag:

- an **IP address** — `10.1.10.51` — pins the element, the interface *and* the
  address. A prefix length is accepted and ignored, so you can paste straight
  out of `ip addr`.
- an **`element:interface`** selector — `sw-hq:Ethernet49/1` — pins the element
  and the port, and the trace must leave or arrive by that port. This is how you
  tell a redundant pair apart.
- an **element name** — `pc-alice`, or the fully-qualified
  `sites/north/hosts/pc-north-01` — pins the element, and any of its ports may
  be used.

An address is usually the right spelling, because an address is what a ticket, a
log line or a packet capture actually carries. The three cannot collide, and an
argument that resolves to nothing is a usage error that names what it could have
meant instead — [Naming the two ends](../paths.md#naming-the-two-ends) has the
disambiguation rules, the failure messages, and why loopback and link-local
addresses are not searched.

## What the trace searches

The trace tries the layers in the order traffic does.

*Layer 2* walks the physical topology — cables, adapter attachments and layer-2
tunnels — relaying only where the kind of element says it does: a hub repeats,
an adapter is transparent, a switch forwards between two of its ports, and a
router, computer or server is where a frame **stops**. VLAN membership prunes
the walk: an untagged host port narrows nothing, a trunk narrows to what it
carries, and an access port in the wrong VLAN is a wall. Whatever survives is
the VLAN the trace *assumed*, and is reported as such. `--vlan` forces one
instead, and then layer 3 is not searched at all — a VLAN is a layer-2 fact, so
asking about one is asking a layer-2 question.

*Layer 3* takes over when the two ends are in no common broadcast domain. Two
elements are one hop apart when they hold an address in the same prefix — the
same grouping `netgraph list subnets` prints and `render --layer l3` draws — and
an element in the middle is only crossed when `spec.forwarding` says it
forwards. The whole route stays in one address family, and each hop names the
prefix and the address at both ends of it.

Overlays need no special case. A layer-2 tunnel carries its VLANs, so the
layer-2 walk crosses it exactly as it crosses a trunk; a layer-3 tunnel has both
ends in one prefix, so the routed walk crosses it exactly as it crosses a link.
Either way the hop is labelled with the encapsulation entered and left, nesting
included, and with what protects it. A tunnel that encrypts nothing, and that
nothing in its `over` chain encrypts either, is marked `CLEARTEXT` on the hop
and warned about on stderr — the same fact
[`W127`](../validation-rules.md#w127--tunnel-carries-traffic-in-the-clear)
reports about an inventory, reported about a *route*. A cleartext VXLAN inside
one data centre is fine; the same tunnel between two branch offices is not, and
only a trace can tell the two apart.

Validation runs before the trace, and errors refuse it for the same reason they
refuse a render: a dangling cable is exactly the kind of thing that makes a path
wrong. `--strict` promotes warnings to errors; `--force` traces anyway and says
that the answer may not match the files.

[How the trace works](../paths.md#how-the-trace-works) has the per-kind table,
the forwarding rules, the tunnel labelling and how patch panels are spliced out.

## A switched path

<!-- run: -->
```console
$ netgraph -i examples/home-lab path laptop srv-nas
hosts/laptop -> hosts/srv-nas: 1 path
  source       hosts/laptop  [computer]
  destination  hosts/srv-nas  [server]
  layer        2, switched
  vlan         10 (assumed by the trace)

path 1 of 1 · 3 hops · vlan 10
   1  hosts/laptop  [computer]
      ->  attachment adp-usb-eth  (copper, 5Gbps, usb)
   2  hosts/adp-usb-eth  [adapter]
      in  usb0
      out enx001122334455       192.168.10.30/24, 2001:db8:10::30/64
      ->  cable cbl-sw-dongle  (copper, 1Gbps, H-004, 10m)  vlan 10
   3  switches/sw-home  [switch]
      in  port4
      out port3
      ->  cable cbl-sw-nas  (copper, 1Gbps, H-003, 0.5m)  vlan 10
   4  hosts/srv-nas  [server]
      in  eth0                  192.168.10.10/24, 2001:db8:10::10/64
```

The header says what was resolved and at which layer; each hop names the
element, the ingress and egress interfaces with their addresses, and the link
crossed with its medium, rate, label and length.

## A routed path

<!-- run: -->
```console
$ netgraph -i examples/campus path 10.1.10.51 10.1.20.11
sites/north/hosts/pc-north-01:eno1 -> sites/north/hosts/srv-north-01:eth0: 1 path
  source       sites/north/hosts/pc-north-01:eno1  [computer]  10.1.10.51/24
  destination  sites/north/hosts/srv-north-01:eth0  [server]  10.1.20.11/24
  layer        3, routed (ipv4)
  note         no layer-2 path: the two elements are in no common broadcast domain, so the trace looked for a routed one

path 1 of 1 · 2 hops · ipv4
   1  sites/north/hosts/pc-north-01  [computer]
      out eno1                  10.1.10.51/24
      ->  subnet 10.1.10.0/24  10.1.10.51/24 -> 10.1.10.1/24
   2  sites/north/distribution/sw-north-dist-01  [switch]
      in  Vlan10                10.1.10.1/24
      out Vlan20                10.1.20.1/24
      ->  subnet 10.1.20.0/24  10.1.20.1/24 -> 10.1.20.11/24
   3  sites/north/hosts/srv-north-01  [server]
      in  eth0                  10.1.20.11/24
```

Both hosts hang off the *same access switch* — one hop apart physically, and in
VLAN 10 and VLAN 20 — so the traffic goes up to the distribution switch's SVIs
and back down. That is the answer a diagram alone will not give you.

## Several paths, and none

**Every distinct path is found**, where distinct means the sequence of elements
*and links* differs — so two cables in a LAG are two paths, not one, and the
redundant pair you were checking for is visible. The shortest is printed by
default and `--all` prints the rest. Enumeration is bounded: `--max-hops`
(default 16) abandons a longer route, and the search stops after 64 distinct
paths, which is reported rather than passed over in silence.

**No path is an answer, not an error.** It comes back with the layers that were
searched and how far each got, so the break is locatable, and the command exits
1 — a reachability assertion drops straight into CI:

<!-- run: rc=1 -->
```console
$ netgraph -i examples/campus path pc-north-01 sw-north-acc-01:GigabitEthernet1/0/3
sites/north/hosts/pc-north-01 -> sites/north/access/sw-north-acc-01:GigabitEthernet1/0/3: no path
  source       sites/north/hosts/pc-north-01  [computer]
  destination  sites/north/access/sw-north-acc-01:GigabitEthernet1/0/3  [switch]
  note         no layer-2 path: the two elements are in no common broadcast domain, so the trace looked for a routed one

no path from sites/north/hosts/pc-north-01 to sites/north/access/sw-north-acc-01 within 16 hops.
  layer 2: reached 2 elements; the furthest was sites/north/access/sw-north-acc-01 at 1 hop
  layer 3: reached 22 elements; the furthest was sites/south/access/sw-south-acc-01 at 5 hops
```

The furthest element is the last place the traffic could still have got to, so
the break is between it and whatever should have come next.

## Drawing the answer: `--highlight`

`--highlight` draws path elements and links bold and crimson and dims everything
else. Nothing is removed — a traced path is visibly *one route through* a
topology rather than the topology itself, which is what `--neighbors-of` cannot
show you. The diagram is built at the layer the path was found at, and every
display option [`netgraph render`](render.md) takes applies to it (`--show-ips`,
`--group-by-namespace`, `--icons`, `--tooltips`, `--link-template`,
`--element-ids`, `--title`) — it is the same renderer, not a fork of it.

`-f` and `-o` describe that diagram and therefore both require `--highlight`;
given without it they are a usage error rather than a flag that quietly did
nothing. Without `-o` the diagram owns stdout and the hop-by-hop report moves to
stderr, which is the same split `render` uses. `--all` widens the highlight to
every reported route.

<!-- norun: writes a diagram into the reader's directory -->
```bash
netgraph -i examples/campus path pc-north-01 pc-south-01 --highlight -f svg -o path.svg
```

There is a picture of the result, and what emphasis does to node colour and line
style, in
[Drawing the answer](../paths.md#drawing-the-answer---highlight).

## Output for a program

`-F json` is the same trace as a document: a `source`, a `destination`, the
layer, and `paths` as two arrays — `waypoints` and the `links` between them. It
always carries **every** path, whatever `--all` says, because `--all` is a
decision about how much to put on a screen and a program that asked for the
routes wants the routes. When nothing was found, `paths` is empty and
`frontiers` says how far each layer's search reached. The key-by-key contract is
in [JSON output](../paths.md#json-output).

<!-- norun: the element names are illustrative and the last line is a shell pipeline -->
```bash
netgraph path pc-alice srv-backup                        # the shortest route
netgraph path 10.1.10.51 10.2.20.11 --all                # every route, by address
netgraph path sw-hq:Ethernet49/1 sw-hq:Ethernet50/1      # can one switch bridge these?
netgraph path rtr-hq rtr-branch-b --vlan 100             # inside one broadcast domain
netgraph path pc-alice srv-backup --highlight -f svg -o path.svg
netgraph path pc-alice srv-backup -F json | jq '.paths[0].links[].id'
```

## Arguments

<!-- generated: arguments path -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `SRC` | yes | 1 | — |
| `DST` | yes | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options path -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--vlan` | `VID` | — | Trace inside this VLAN instead of letting the trace derive one. Forces a layer-2 answer: a VLAN is a layer-2 fact, so no routed path is looked for. |
| `--all` | — | off | Report every distinct path, not only the shortest. A redundant pair is the point. |
| `--max-hops` | `INTEGER, 1-64` | `16` | Abandon a route that crosses more links than this. |
| `-F`, `--output-format` | `[text\|json]` | `text` | text is the hop-by-hop report; json is the same trace for tooling. |
| `--highlight` | — | off | Also render the whole inventory with the traced path emphasised and everything else dimmed. Choose the format with -f and the destination with -o. |
| `-f`, `--format` | `[dot\|svg\|html\|png\|pdf]` | `dot` | Format of the --highlight diagram. |
| `-o`, `--output` | `FILE` | — | Write the --highlight diagram to this file instead of stdout. |
| `--show-ips`, `--no-show-ips` | — | `--show-ips` | Print configured IP addresses on the nodes. |
| `--show-vlans`, `--no-show-vlans` | — | `--show-vlans` | Annotate nodes and links with VLAN membership. |
| `--annotations`, `--no-annotations` | — | `--annotations` | Draw the notes, areas and legends the inventory declares for this view. Turn them off for a diagram that should carry the topology and nothing written about it — a printed page for an audit — and leave them on for one that is being read rather than checked, where the callout is the reason the screenshot is worth attaching to the ticket. |
| `--group-by-namespace` | — | off | Draw each namespace as a visual group. |
| `--icons` | `THEME\|DIR` | — | Draw each element as an icon instead of a plain shape. Built in: cisco, none. A directory of images named after element kinds (router.svg, switch.png, ...) also works. Graphviz formats only. |
| `--theme` | `NAME\|PATH` | — | Apply a stylesheet: selectors by kind, name, namespace, role or label, each mapping onto a style block. Built in: blueprint, mono, none. A path to a 'kind: theme' YAML file also works. An element's own spec.style still wins. |
| `--style`, `--no-style` | — | `--style` | Honour the styles the inventory and the theme declare. --no-style draws the plain diagram from the built-in palette alone, which is the way to read a topology whose stylesheet is in the way. Icons are unaffected: use --icons none. |
| `--tooltips`, `--no-tooltips` | — | `--tooltips` | Carry the full detail of each element — interfaces, addresses, VLANs, cabling — as hover text. Reaches a reader in svg output; png and pdf have nowhere to put it. |
| `--link-template` | `URL` | — | Link each element back to the YAML that declares it, e.g. 'https://git.example.com/net/blob/main/{file}#L{line}'. Placeholders: {file}, {line}, {name}, {namespace}, {kind}. dot and svg only. |
| `--element-ids` | — | off | Give every node, edge and namespace a stable id derived from its name, so the diagram can be deep-linked and styled from outside. dot and svg only. |
| `--max-addresses` | `N` | `4` | Longest address list spelled out under a node before it is abbreviated to 'and N more'. 0 prints the count alone. |
| `--rankdir` | `[tb\|lr\|bt\|rl]` | TB, top to bottom | Layout direction. A wide network reads better left to right; a deep one top to bottom. Honoured by the Graphviz backends and by mermaid. |
| `--routing` | `[spline\|orthogonal\|straight]` | whatever the inventory's layout documents say, else spline | How links are drawn between the bends they are pinned through: 'spline' is the curve Graphviz draws, 'orthogonal' right angles, 'straight' segment to segment. A default: a link that pins a style of its own keeps it. Honoured by the Graphviz backends, the JSON export and the editor. 'netgraph layout --write' records it in the view it arranges, so the choice is the inventory's rather than the command line's from then on. |
| `--avoid`, `--no-avoid` | — | avoid | Route orthogonal links around the boxes they are not attached to instead of straight across them. Only applies to an arranged diagram drawn with '--routing orthogonal': a spline has nothing to route around, and an unarranged one is routed by Graphviz, which already avoids nodes. A bend you placed yourself is never moved — routing fills the segments between them. '--no-avoid' is the local Z-and-L every orthogonal diagram was drawn with before this existed. |
| `--title` | `TEXT` | — | Caption for the diagram. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Proceed even when validation failed. The result may not match the files. |
| `--profile` | `NAME` | — | Apply the [profile.NAME] block of netgraph.toml on top of its [render] table. Explicit flags still win over both. |
| `--show-config` | — | off | Print the settings this invocation resolves to, and where each one came from, then exit without doing any work. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| 0 | A path was found. |
| 1 | There is no path, or the inventory was rejected and `--force` was not given. |
| 2 | Usage error: an argument that names no element, interface or address, or `-f`/`-o` without `--highlight`. |
| 3 | The inventory could not be discovered or read at all. |
| 5 | The `--highlight` diagram could not be produced: Graphviz is missing, or the output path is not writable. |
| 130 | Interrupted. |
| 141 | The downstream end of a pipe closed first. |

## See also

* [`docs/paths.md`](../paths.md) — how each layer decides, more worked examples,
  the JSON contract, and what the trace does not model.
* [`netgraph render`](render.md) and [`docs/rendering.md`](../rendering.md) — the
  diagram `--highlight` emphasises a route on.
* [`docs/validation.md`](../validation.md) — the checks that run before a trace,
  and what `--strict` changes.
* [`docs/ci.md`](../ci.md) — using the exit code as a reachability assertion.
