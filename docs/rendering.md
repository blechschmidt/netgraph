# Drawing the inventory

Every diagram netgraph produces comes out of one pipeline: a layer decides which
question the picture answers, filters and aggregation decide how much of the
network is in it, and a format decides what the artefact is. This page describes
that pipeline once. [`netgraph render`](commands/render.md) writes a file with
it, [`netgraph watch`](commands/watch.md) keeps that file current while you edit,
[`netgraph web`](commands/web.md) serves it, and
[`netgraph path --highlight`](paths.md#drawing-the-answer---highlight) draws a
traced route over it — all four take the same options and mean the same things by
them.

![The home-lab example rendered at layer 2: a router, a switch, an access
point, a server, two workstations, a phone and a USB adapter, joined by
labelled links](images/home-lab.svg)

---

## Contents

- [Layers: one inventory, seven questions](#layers-one-inventory-seven-questions)
  - [`physical` and `l1`: the cabling record and the network](#physical-and-l1-the-cabling-record-and-the-network)
  - [`l2`: the same graph, annotated with VLANs](#l2-the-same-graph-annotated-with-vlans)
  - [`l3`: prefixes and who is addressed in them](#l3-prefixes-and-who-is-addressed-in-them)
  - [`overlay`: tunnels and what runs inside what](#overlay-tunnels-and-what-runs-inside-what)
  - [`routing`: sessions, adjacencies and VRFs](#routing-sessions-adjacencies-and-vrfs)
  - [`rack`: a front elevation per cabinet](#rack-a-front-elevation-per-cabinet)
- [Filters: drawing less of the network](#filters-drawing-less-of-the-network)
- [Aggregation: one node per site, one line per bundle](#aggregation-one-node-per-site-one-line-per-bundle)
- [Icons](#icons)
- [Labelling and layout](#labelling-and-layout)
- [Interactive SVG: tooltips, links and ids](#interactive-svg-tooltips-links-and-ids)
- [Output formats](#output-formats)
  - [The interactive HTML page](#the-interactive-html-page)
  - [The JSON export](#the-json-export)

---

## Layers: one inventory, seven questions

One inventory, seven questions. `--layer` picks which one the diagram answers.

| Layer | Nodes | Edges | Annotations | Reach for it when |
|---|---|---|---|---|
| `physical` | devices, adapters **and patch panels** | one per cable — every segment of a run, drawn separately | the same as `l1` | You are holding a patch lead. "Which position does this run occupy, and which are free?" |
| `l1` | devices and adapters | one per cable, one per adapter attachment, one per tunnel; a run through a patch panel is **one** edge | medium, link rate, cable label, length; encapsulation on a tunnel | You are standing at the rack. "Which port is this patched into, and with what?" |
| `l2` | the same | the same | VLAN membership per node and per link, port mode | "Is this host in VLAN 10 all the way to the gateway?" Broadcast domains, trunk pruning, a VLAN that stops one switch short. |
| `l3` | the elements that hold a routable address, **plus one node per IP prefix** | one per address: element ↔ the subnet it is addressed in, labelled with the interface and the address | VLANs the prefix is reachable in | "Why can these two not reach each other?" The addressing plan, gateways, a subnet mask that is one bit off. |
| `overlay` | the elements that terminate a tunnel, **plus one node per tunnel** | one per endpoint, plus one per `over` — this tunnel runs inside that one | encapsulation stack, VNI, MTU budget, what encrypts | "Is this traffic actually protected, and what carries it?" VPNs, VXLAN fabrics, a cleartext overlay somebody assumed was private. |
| `routing` | the elements that take part in routing — anything declaring `routing`, `routes` or `vrfs` — grouped into one cluster per VRF | one per BGP session (solid, labelled with the AS pair) and one per OSPF adjacency (dotted, labelled with the area) | AS number, router id, area, the instances and static routes each device holds | "Who peers with whom, and in which table?" An iBGP mesh with a gap in it, an AS number typed twice, a VRF nothing is bound to. |
| `rack` | one node per rack named by a `metadata.location` | none — a cable says nothing about where either end is bolted | a front elevation: one row per unit, occupied and empty alike | "How much room is left in that cabinet, and what is above the UPS?" |

The default is `l1`. `-f html` accepts `--layer` more than once and puts a
switcher over the results; every other format holds one layer, and asking for
two is a usage error rather than a silently discarded flag:

<!-- run: rc=2 -->
```console
$ netgraph -i examples/home-lab render --layer l1 --layer l2 -f svg
Usage: netgraph render [OPTIONS]
Try 'netgraph render --help' for help.

Error: --layer was given 2 times, but svg output holds one layer; render each one to its own file, or use a format that holds several (html)
```

### `physical` and `l1`: the cabling record and the network

`physical` and `l1` are the same graph drawn twice, and the difference between
them is the patch panels. A `patchpanel`
([`docs/schema.md` §15](schema.md#15-patch-panels)) is a passive cross-connect: a
run that goes switch → panel front → structured cabling → panel rear → server is
three cables in the inventory and **one link** on the network, because nothing
electrically can tell the panel is there. `physical` draws the cabling record
— the panels and every segment; every other layer *splices* each run into the
single edge it is, between the two active ports, carrying the sum of the segment
lengths and the rate of the slowest one. The result is exactly the graph the
same inventory would produce with the two devices cabled together directly,
which is what makes a panel free to model.

The splice is not a loss of information. [`-f json`](#the-json-export) exports a
`patch` object naming the segments and the positions,
[`netgraph path`](paths.md#patch-panels) names the panels on the link line — as a
pass-through, never as a hop, because a panel takes no decision — and an SVG
tooltip lists the same record:

<!-- run: -->
```console
$ netgraph -i examples/patch-room path sw-core-01 srv-app-01
...
   1  network/sw-core-01  [switch]
      out GigabitEthernet1/0/7
      ->  cable cbl-sw-pp07  (copper, 1Gbps, P-007A, 21m)  vlan 10  [via pp-r1-a front/7-rear/7, pp-r2-a rear/7-front/7]
   2  hosts/srv-app-01  [server]
      in  eno1                  10.10.0.11/24
```

### `l2`: the same graph, annotated with VLANs

`l1` and `l2` are the same graph drawn twice: the same nodes and the same edges,
with VLAN membership printed on both and the port mode — access or trunk — on
the link. It is the layer to reach for when the question is about a broadcast
domain rather than about a cable.

### `l3`: prefixes and who is addressed in them

`l3` is a **different graph**: cables do not appear, because two devices are
adjacent at layer 3 when they share a prefix — not when a cable happens to run
between them (a route may cross three switches; a trunk carries VLANs neither end
routes).

![Layer-3 diagram of the home-lab example: seven elements joined to the five IP
prefixes they are addressed in, each edge labelled with the interface and its
address](images/home-lab-l3.svg)

<sub>The same inventory as the diagram at the top of this page, at layer 3:
`netgraph -i examples/home-lab render --layer l3 --title "home-lab — layer 3" -f svg -o docs/images/home-lab-l3.svg`.
The router's loopback and its ISP hand-off are prefixes of their own; the switch
and the access point appear only because their management SVIs hold an
address.</sub>

What layer 3 leaves out is deliberate:

* **Elements with no routable address.** A layer-2-only switch says nothing
  about IP reachability, so it is omitted rather than drawn floating beside the
  subnets it is not in. Give it a management SVI and it appears.
* **Loopback and link-local addresses**, and unnumbered interfaces. `127.0.0.1`,
  `::1` and `fe80::/10` are scoped to one host or one link, so they are not
  prefixes of *this* network.
* **VLAN identity of a prefix.** Grouping is by prefix, because that is what a
  routing table keys on. A prefix deliberately re-used in two VLANs therefore
  appears once — which is exactly what
  [`W106`](validation-rules.md#w106--one-address-claimed-twice-in-a-subnet)
  points out. A **VRF** is the one thing that does split it: a routing instance
  is a routing table of its own, so `10.0.0.0/24` in `blue` and in the global
  table are two nodes, each labelled with its instance
  ([`docs/schema.md` §16.1](schema.md#161-vrfs--routing-instances)).

Two problems are visible only from the derived layers, and `netgraph validate`
reports both:

| Rule | Fires when |
|---|---|
| [`W105`](validation-rules.md#w105--subnet-with-a-single-member) | Exactly one element is addressed in a prefix — a typo'd prefix length, or a neighbour nobody wrote down. Host routes and point-to-point prefixes are exempt. |
| [`W106`](validation-rules.md#w106--one-address-claimed-twice-in-a-subnet) | Two elements claim the same address in one prefix from different VLANs, so the layer-3 view cannot tell which of them answers. |

[`netgraph list subnets`](commands/list.md) prints the same grouping as a table,
[`netgraph ipam`](ipam.md) sizes it and reports what conflicts, and
`render --layer l3 -f json` exports it with a `type` discriminator on every node
(`element` or `subnet`) so a consumer can tell a derived prefix from a declared
device.

### `overlay`: tunnels and what runs inside what

The `overlay` layer is a different graph again. Every tunnel becomes a **node**,
joined to each element it terminates on and to the tunnel it runs inside:

![Encapsulation diagram of the overlay example: three routers and a workstation
joined to five tunnel nodes, with the VXLAN and GRE tunnels each drawn running
inside the IPsec tunnel](images/overlay.svg)

<sub>Produced from [`examples/overlay`](../examples/overlay) with
`netgraph -i examples/overlay render --layer overlay --group-by-namespace --title "overlay — encapsulation" -f svg -o docs/images/overlay.svg`.</sub>

A tunnel has to become a node there because nesting is a relation between two
*links*, and a link cannot end on a link — which is why `vxlan over ipsec` is
undrawable at layer 1 and obvious here. Below that layer a point-to-point tunnel
stays a dashed edge, so a render shows the VPNs over the physical topology
without a box in the middle of each one.

### `routing`: sessions, adjacencies and VRFs

The `routing` layer draws the control plane
([`docs/schema.md` §16.6](schema.md#166-the-routing-view)). Nodes are the
elements that take part in routing at all, labelled with the AS number and router
id their peers know them by; edges are the sessions and adjacencies between them:

<!-- norun: an excerpt of the flowchart, elided in the middle -->
```console
$ netgraph -i examples/campus render --layer routing -f mermaid
n3(["rtr-north-core-01<br/>[router]<br/>AS 65001<br/>id 192.0.2.1<br/>…"])
…
n3 -- "iBGP 65001 · iBGP to rtr-south-core-01" --- n7
n3 -. "area 0.0.0.0" .- n4
```

The two kinds of edge are resolved differently, because the protocols work
differently:

* a **BGP session** is *declared*, by address, on one or both ends. It is drawn
  once however many times it is declared, solid, and labelled with the AS pair —
  `65001 → 65002` for an external session, `iBGP 65001` when both ends are in one
  AS. A session whose address matches nothing in the inventory is reported as a
  dropped link rather than drawn to a node that does not exist, and as
  [`W135`](validation-rules.md#w135--bgp-neighbour-is-not-in-the-inventory) by
  the validator.
* an **OSPF adjacency** is *discovered*, so netgraph derives it the way the
  protocol does: two interfaces that run OSPF in the same area and are addressed
  in one subnet form one, drawn dotted and labelled with the area. Deriving it
  from the addressing rather than from the cables is what makes it right for two
  routers facing each other across a layer-2 switch, which no cable joins.

**Clusters are VRFs**, and they replace `--group-by-namespace` at this layer: the
grouping the reader asked for by choosing the layer wins over the one a flag
asks for. A router with interfaces in exactly one instance is drawn inside that
instance's box; one that straddles several belongs to no box — the same choice a
cross-site prefix gets at layer 3 — and names its instances on its label instead.

Nothing physical appears. Two routers are adjacent here because they exchange
routes, which a cable neither guarantees nor is needed for.

### `rack`: a front elevation per cabinet

`rack` is not a topology at all. `metadata.location`
([`docs/schema.md` §3.2](schema.md#32-metadatalocation)) records where an element
is bolted — `site`, `room`, `rack`, the lowest unit it occupies (`position`) and
how many it takes (`height`) — and `--layer rack` turns that into one front
elevation per cabinet, units on the vertical axis, empty ones drawn so the free
space is countable. Two things in the same unit are
[`E025`](validation-rules.md#e025--two-elements-occupy-the-same-rack-unit), and
something that would stick out of the top is
[`E026`](validation-rules.md#e026--element-mounted-above-the-top-of-its-rack).

Mermaid has no way to express a grid, so `-f mermaid --layer rack` is refused
with an error naming the formats that can:

<!-- run: rc=2 -->
```console
$ netgraph -i examples/patch-room render --layer rack -f mermaid
Usage: netgraph render [OPTIONS]
Try 'netgraph render --help' for help.

Error: --layer rack draws a front elevation — one row per rack unit, empty units included — and mermaid output has no way to express one; render it as dot, svg, html, png, pdf, json
```

## Filters: drawing less of the network

**Filters** narrow what is drawn. Values *within* one option are alternatives;
different options are combined with AND, so `--namespace sites/north --kind
switch` keeps the switches of that site only. An unset filter selects
everything, and filtering never changes what the remaining nodes say about
themselves.

| Option | Repeatable | Keeps |
|---|---|---|
| `--namespace NS` | yes | Elements in `NS` or in any namespace below it. |
| `--vlan VID` | yes | Elements participating in that VLAN (1–4094). A host on an untagged access port counts as a member. |
| `--kind KIND` | yes | Elements of that kind: `switch`, `router`, `hub`, `computer`, `server`, `adapter`, `patchpanel`. A cable is an edge and so is a tunnel, so neither is selectable; both follow whichever elements survive. |
| `--name GLOB` | yes | Elements whose short **or** fully-qualified name matches the shell-style glob. |
| `--neighbors-of NAME` | no | Only the neighbourhood of one element. An unknown name is a usage error, with suggestions. |
| `--depth N` | no | How many hops `--neighbors-of` reaches. Default 1. |

At `--layer l3` every filter still selects **elements**; the subnet nodes are
derived, so each one survives exactly as long as one selected element is still
addressed in it, and it then reports only those members. `--kind router` draws
the routers and the prefixes they route, never an empty prefix. `--neighbors-of`
counts a subnet as a hop, so depth 1 from a device reaches the prefixes it is
addressed in and depth 2 the other devices in them. `--group-by-namespace`
leaves subnets outside every group: a prefix spanning two sites belongs to
neither.

<!-- run: -->
```console
$ netgraph -i examples/home-lab render --neighbors-of sw-home --depth 1 --kind switch --kind server -f mermaid
flowchart TB
    n0[("srv-nas<br/>[server]<br/>192.168.10.10/24<br/>2001:db8:10::10/64<br/>vlans: 10")]
    n1["sw-home<br/>[switch]<br/>192.168.10.2/24<br/>vlans: 10,20"]
    n2["ap-home<br/>[switch]<br/>192.168.10.3/24<br/>vlans: 10,20"]

    n0 -- "eth0 ↔ port3 · H-003 · 1Gbps" --- n1
    n2 -- "eth0 ↔ port5 · H-005 · 1Gbps" --- n1

    classDef server fill:#eae2f5,stroke:#7c3aed,stroke-width:1px
    classDef switch fill:#dcf0dc,stroke:#16a34a,stroke-width:1px
    class n0 server
    class n1,n2 switch
rendered 3 node(s) and 2 edge(s) as mermaid at layer l1
```

## Aggregation: one node per site, one line per bundle

Every filter above removes detail by removing elements. Past a few hundred
devices that is the wrong question: you do not want *less* of the network, you
want all of it in less space. Two options summarise instead of narrowing, and
both run before the renderers, so `dot`, `svg`, `mermaid`, `json` and `html`
all get the same answer.

| Option | Repeatable | Draws |
|---|---|---|
| `--collapse NS` | yes | One node for `NS` and everything under it, labelled with the namespace, its element count per kind, and the VLANs and prefixes it participates in. |
| `--collapse-depth N` | no | The same, for every namespace `N` levels deep. |
| `--bundle-links` / `--no-bundle-links` | no | Fold every set of parallel links into one edge / fold none. Unset folds declared link aggregations only. |

`--collapse-depth 1` is the site-level overview of a large tree in one flag:

<!-- norun: writes an SVG into the reader's directory -->
```bash
netgraph -i examples/campus render --collapse-depth 1 --group-by-namespace \
  --title "campus — one node per site" -f svg -o campus-collapsed.svg
```

![The campus example collapsed to three nodes: sites/north, sites/south and
sites/west, each labelled with its element counts, VLANs and subnets, joined by
the three backbone fibres](images/campus-collapsed.svg)

Depth is counted from the **shallowest namespace that actually branches**. Every
element of `examples/campus` lives under `sites/`, so that directory is not a
level a reader distinguishes — nothing is outside it — and depth 1 means one
node per site rather than one node for the campus. Depth 2 would be one node per
tier inside each site.

<!-- run: -->
```console
$ netgraph -i examples/campus render --collapse-depth 1 -f mermaid
flowchart TB
    n0[/"sites/north<br/>[namespace]<br/>8 elements: 2 computers, 1 router, 1 server, 4 switches<br/>7 links inside<br/>vlans: 1,10,20,30,99<br/>10.1.0.0/30<br/>10.1.10.0/24<br/>10.1.20.0/24<br/>10.1.99.0/24<br/>(+9 more)"\]
...
rendered 3 node(s) and 3 edge(s) as mermaid at layer l1
```

Nothing is thrown away, only folded:

* Links **crossing** a boundary keep their identity and attach to the collapsed
  node — the three backbone fibres above are the same three cables, with the
  same labels and the same rates.
* Links **inside** one are counted on the label (`7 links inside`) rather than
  drawn, and named in full in the tooltip and in `-f json`.
* The collapsed node takes a tooltip and an `--element-ids` id exactly as a real
  node does, so a collapsed diagram is as deep-linkable as any other.
* `-f json` marks it `"type": "aggregate"` and gives it an `aggregate` object
  listing **every element it stands for**, so a consumer can never mistake one
  box for one device:

<!-- norun: a shell pipeline -->
```console
$ netgraph -i examples/campus render --collapse-depth 1 -f json |
    jq '.nodes[0].aggregate | {namespace, elementCount, countsByKind}'
{
  "namespace": "sites/north",
  "elementCount": 8,
  "countsByKind": { "computer": 2, "router": 1, "server": 1, "switch": 4 }
}
```

**Link bundling** solves the other half. Four cables in a LAG, or three cables
and a tunnel, draw as a band of parallel lines that Graphviz stacks into noise;
a bundle draws one edge, labelled with the count, weighted by it so the layout
pulls the endpoints together, and carrying every member in its tooltip and in
`-f json`.

LAG members are bundled **by default**, because the inventory has already said
they are one logical link — a switch declaring

```yaml
- name: Port-channel1
  type: lag
  members: [GigabitEthernet1/0/1, GigabitEthernet1/0/2,
            GigabitEthernet1/0/3, GigabitEthernet1/0/4]
```

draws one edge labelled `Port-channel1 -- Port-channel1 / lag, 4 members /
4Gbps`, the sum of what the members carry. Nothing is guessed: two spare
cross-links running alongside that LAG stay two edges, and two distinct
port-channels between one pair of switches stay two bundles. `--bundle-links`
goes further and folds every set of parallel links, whatever the reason they are
parallel — a judgement about legibility rather than a claim about the
configuration, so it is opt-in and the resulting edge is *not* called a LAG.
`--no-bundle-links` draws every cable, which is what a cabling document wants.

<!-- norun: the output paths are illustrative -->
```bash
netgraph render --collapse-depth 1 -f svg -o overview.svg      # sites only
netgraph render --collapse sites/north --collapse sites/south  # two of three
netgraph render --collapse-depth 1 --bundle-links -f svg       # one line per pair
netgraph render --no-bundle-links -f dot -o cabling.dot        # every cable
```

Filters and aggregation compose, in that order: the filter decides what exists,
the collapse folds what is left, so `--kind switch --collapse-depth 1` gives one
box per site holding that site's switches and nothing else.

## Icons

By default a node is a Graphviz shape — a diamond for a router, a 3-D box for a
switch — which keeps a diagram readable with nothing installed. `--icons` swaps
the shape for a picture:

<!-- norun: writes an SVG into the reader's directory -->
```bash
netgraph render --icons cisco --layer l2 -f svg -o topology.svg
```

![The home-lab example drawn with the bundled cisco theme: a router cylinder,
two switch slabs, three monitors, a server tower and a dongle, joined by
labelled links](images/home-lab-icons.svg)

<sub>`netgraph -i examples/home-lab render --layer l2 --icons cisco --title "home-lab — layer 2, cisco icons" -f svg -o docs/images/home-lab-icons.svg`.</sub>

Only *how* a node is drawn changes. The labels, the addresses, the VLANs, the
edges and every filter behave exactly as they do without a theme, and a kind the
theme has no picture for keeps its plain shape rather than disappearing.

**`cisco`** ships with netgraph and covers every kind that becomes a node: the
seven hardware kinds, the subnet clouds of `--layer l3`, and the tunnel conduit
of `--layer overlay`. The artwork is drawn in the topology idiom Cisco made the
industry convention and is netgraph's own, under the same MIT licence as the
rest of the package — Cisco's published icon library is copyrighted and is not
redistributed here.

The tunnel glyph is a **conduit**: a bore with a payload going in one end and
coming out the other. There is one for every tunnel type, because encapsulation
is what they have in common and the type is on the label anyway, and it says
nothing at all about confidentiality — a lock would put netgraph's guess about a
security property into a picture, and a reader who did not recognise the glyph
would read its absence as "nothing to say". That stays a colour and a word: a
cleartext tunnel is drawn crimson and labelled `cleartext`, and
[`W127`](validation-rules.md#w127--tunnel-carries-traffic-in-the-clear) says so
in prose. A collapsed namespace gets no icon either — it is not a *thing* with a
picture but a box holding several, and the folder shape says that better.

**A directory** works just as well, which is how you use that library, or any
other set, if you have it. A theme is nothing but a directory of images named
after the kinds they stand for — `router`, `switch`, `hub`, `computer`,
`server`, `adapter`, `patchpanel`, `subnet` and `tunnel`, with an `.svg`,
`.png`, `.jpg` or `.gif` extension:

<!-- norun: the paths are illustrative -->
```bash
ls my-icons/          # router.png  switch.png  server.png
netgraph render --icons ./my-icons -f svg -o topology.svg
```

Files for kinds you do not cover are simply absent; those nodes keep their plain
shape, so a set of three icons is a usable theme. `--icons none` turns a theme
back off, for a wrapper script that always passes the option. When the theme is
named in [`netgraph.toml`](configuration.md#every-render-setting), a **relative**
directory resolves against the configuration file rather than the working
directory, so a colleague who runs `netgraph` from a parent folder gets the same
icons.

Two details are worth knowing:

* **SVG output is self-contained.** Graphviz references an icon by path; netgraph
  embeds the file into the SVG it hands back, so the diagram still draws in a
  README, an email or the `watch` preview.
* **`png` and `pdf` want raster icons.** Graphviz reads an SVG image only when it
  was built against librsvg, and those two outputs go through cairo. The bundled
  theme therefore ships each icon as both an SVG and a PNG and picks per format.
  A theme of your own that holds only SVGs still renders `dot` and `svg`; if
  `png` fails, netgraph says exactly that rather than drawing a diagram with
  holes in it.

`--icons` is ignored, with a warning, by `-f mermaid` and `-f json`: neither has
a picture to put an icon in.

[`src/netgraph/render/iconsets/README.md`](../src/netgraph/render/iconsets/README.md)
documents the bundled themes from the other side — the naming rule, why each
kind is present twice, and how to regenerate a PNG after editing its SVG.

## Labelling and layout

These options change what a node says and where the diagram puts it. None of
them changes which elements are in it.

| Option | Default | Effect |
|---|---|---|
| `--title TEXT` | none | Caption for the diagram. |
| `--show-ips` / `--no-show-ips` | on | Print configured IP addresses on the nodes. |
| `--show-vlans` / `--no-show-vlans` | on | Annotate nodes and links with VLAN membership. |
| `--max-addresses N` | `4` | Longest address list spelled out under a node before it is abbreviated to "and N more". `0` prints the count alone. |
| `--group-by-namespace` | off | Draw each namespace as a visual group (a Graphviz cluster, a Mermaid subgraph). |
| `--rankdir TB\|LR\|BT\|RL` | `TB` | Layout direction. A wide network reads better left to right, a deep one top to bottom. Honoured by the Graphviz backends and by `mermaid`. |
| `--element-ids` | off | Give every node, edge and namespace a stable `id` derived from its name — see [Interactive SVG](#interactive-svg-tooltips-links-and-ids). |

`--show-ips` and `--show-vlans` reach further than the labels: they also control
the hover text of an SVG, the per-interface detail of
[`-f json`](#the-json-export), and what an
[HTML page](#the-interactive-html-page) is even allowed to hold. "Do not print
the addresses" has to mean all of the printing.

## Interactive SVG: tooltips, links and ids

An SVG is the artefact that gets committed to a repository or dropped into a
wiki, and it can carry more than the picture. Three attributes travel with it,
none of which changes the drawing:

<!-- norun: writes an SVG into the reader's directory -->
```bash
netgraph render -f svg --element-ids \
    --link-template 'https://git.example.com/net/blob/main/{file}#L{line}' \
    -o docs/topology.svg
```

| Flag | Honoured by | Ignored by | What it does |
|---|---|---|---|
| `--tooltips` (default on) | `svg`, `dot`, `html` | `png`, `pdf`, `mermaid`, `json` | Hover text on every node, edge and namespace box. |
| `--link-template URL` | `svg`, `dot`, `html` | `png`, `pdf`, `mermaid`, `json` | Turns each element into a link to the document that declares it. |
| `--element-ids` | `svg`, `dot`, `html` | `png`, `pdf`, `mermaid`, `json` | A stable `id` on every node, edge and cluster. Always on in `html`, which is built on the other two. |

`-f dot` writes the attributes because a DOT file is the input to somebody
else's `dot`; `-f svg` is where they reach a reader. `png` and `pdf` are
pictures and drop them silently — netgraph warns when you asked for one of the
three and picked a format that cannot carry it. `mermaid` and `json` have
interaction models of their own and ignore all three.

**Tooltips** are the same per-element records [`netgraph web`](commands/web.md)
shows in its info boxes, rendered as plain text — one builder, so a committed
diagram and the live preview cannot disagree. Hovering the switch of the
[quickstart](getting-started.md) inventory gives:

<!-- tooltip-example -->
```
sw-office [switch]
namespace: devices
labels: site=office
vlans: 10
interfaces (2):
  port1  ethernet  vlan 10 (access)
  port2  ethernet  vlan 10 (access)
links (2):
  port1 — devices/rtr-gw:lan0  (cable, copper, 1Gbps)  vlan 10
  port2 — devices/pc-alice:eno1  (cable, copper, 1Gbps)  vlan 10
```

Every port, including the two the label had no room to annotate, and both
cables — with the far end, its interface, the medium, the rate and the VLAN.

They work in any browser with no JavaScript: netgraph puts the text in the SVG
`<title>` element of each shape, which is the construct browsers have popped up
since SVG 1.1. The text is bounded — long lists are counted off (`(+12 more)`)
and the whole is clipped — so a tooltip never covers the diagram it explains.
`--no-show-ips` and `--no-show-vlans` apply to the hover text as well as to the
labels: "do not print the addresses" has to mean all of the printing.
`--no-tooltips` removes the detail entirely, for a diagram published somewhere
it should carry nothing the picture does not show.

**`--link-template`** is a format string expanded per element. Five
placeholders, and an unknown one is a usage error before the inventory is even
loaded, rather than four hundred broken links in a committed file:

| Placeholder | Expands to |
|---|---|
| `{file}` | Path of the declaring document, relative to the inventory root — `switches/sw-office.yaml`. |
| `{line}` | 1-based line the document starts on. |
| `{name}` | Fully-qualified name — `sites/hq/sw-core`. |
| `{namespace}` | Namespace alone — `sites/hq`, empty at the root. |
| `{kind}` | `switch`, `router`, `cable`, `tunnel`, … |

Every substituted value is percent-encoded (`/` excepted, since a path is
hierarchical), so nothing an inventory contains can escape the URL. A cable
links to the line of the document that declares it, an adapter attachment to the
adapter, a tunnel to the `tunnel` document. A layer-3 prefix node links nowhere:
no file says `192.168.10.0/24`, and a link that 404s is worse than a shape that
is not clickable. So does any element whose line the parser could not report,
when the template asks for `{line}`.

**`--element-ids`** derives an id from the fully-qualified name, so it survives
someone adding a device to the file above it:

```
sites/hq/sw-core   →  id="node-sites_hq_sw-core"
sites/hq/cbl-07    →  id="edge-sites_hq_cbl-07"
sites/hq           →  id="cluster-sites_hq"
```

Anything outside `[A-Za-z0-9_.-]` becomes an underscore, because an XML `id`
may not hold a `/` and because the ids of a published diagram are a second,
unescaped copy of the inventory's names. Two names that reduce to the same slug
get `-2`, `-3` suffixes in graph order. That makes a diagram addressable from
outside:

```html
<a href="topology.svg#node-sites_hq_sw-core">the core switch</a>

<style>
  #node-sites_hq_sw-core polygon { stroke: #dc2626; stroke-width: 3; }
</style>
```

One quirk worth knowing: Graphviz XML-escapes `-` as `&#45;` when it writes an
`id`, so `grep id=\"node-sites_hq_sw-core\"` over the raw file finds nothing.
Every XML parser, browser and stylesheet sees the id unescaped; only a text
search does not.

## Output formats

`-f/--format` decides what the artefact is. `svg`, `html`, `png` and `pdf` need
Graphviz on the `PATH`; `dot`, `mermaid` and `json` do not.

| Format | Needs Graphviz | Reach for it when |
|---|---|---|
| `dot` | no | You want the Graphviz source, to post-process it, to feed somebody else's `dot`, or to diff it. The default. |
| `svg` | yes | The diagram is going into a repository, a wiki or an email. Scales, carries [tooltips, links and ids](#interactive-svg-tooltips-links-and-ids), and embeds its icons. |
| `html` | yes | The diagram is *for somebody else* — one self-contained page that pans, zooms and searches. See [below](#the-interactive-html-page). |
| `png` | yes | Something downstream only takes a raster image. Carries no interactivity. |
| `pdf` | yes | It is going to be printed or attached to a document. |
| `mermaid` | no | It is going into Markdown that GitHub or GitLab renders in place. |
| `json` | no | A program is the reader. See [below](#the-json-export). |

`png` and `pdf` are binary, so `-o/--output` is required for them when stdout is
a terminal. `-o` creates parent directories.

<!-- run: -->
```console
$ netgraph -i examples/quickstart render -f mermaid
flowchart TB
    n0[/"pc-alice<br/>[computer]<br/>192.168.10.20/24<br/>vlans: 10"/]
    n1(["rtr-gw<br/>[router]<br/>203.0.113.2/30<br/>192.168.10.1/24<br/>vlans: 10"])
    n2["sw-office<br/>[switch]<br/>vlans: 10"]

    n1 -- "lan0 ↔ port1 · 1Gbps" --- n2
    n0 -- "eno1 ↔ port2 · 1Gbps" --- n2

    classDef computer fill:#f5f5f5,stroke:#6b7280,stroke-width:1px
    classDef router fill:#dbe9f6,stroke:#2563eb,stroke-width:1px
    classDef switch fill:#dcf0dc,stroke:#16a34a,stroke-width:1px
    class n0 computer
    class n1 router
    class n2 switch
rendered 3 node(s) and 2 edge(s) as mermaid at layer l1
```

Mermaid's renderer refuses a diagram of more than 500 edges, and that ceiling is
a secure config a document is not allowed to raise for itself — so GitHub,
GitLab and `mmdc` will not draw one, however valid it is. netgraph warns when it
crosses the line and names the filters that would bring it back down; `-f dot`
and `-f svg` have no such limit.

### The interactive HTML page

`-f html` writes **one file** that pans, zooms, searches and explains itself,
with nothing to install and nothing to fetch:

<!-- norun: writes an HTML file into the reader's directory -->
```bash
netgraph render -f html --layer l1 --layer l2 --layer l3 \
    --title "home-lab — every layer" -o docs/home-lab.html
```

[**docs/home-lab.html**](home-lab.html) is that command's output, committed:
the home-lab inventory at all three layers, 174 kB, no server. GitHub shows an
`.html` file as source, so download it — or open it from a Pages site — to see
the page itself.

It is the format to reach for when the diagram is *for somebody else*: attach it
to a change request, commit it next to the YAML, publish it to GitHub Pages, or
open it from a `file://` URL on a machine that has never heard of Python. What
you get:

* **pan and zoom** — drag or arrow keys, scroll or `+`/`−`, pinch on a touch
  screen; `f` or **Fit** puts the whole diagram back in the window and **Reset**
  returns the page to how it opened;
* **search** — type a name, an address, a MAC or a VLAN and the matches light up
  while the rest dims, with a result list you can walk by keyboard (`/` focuses
  the box, `Esc` clears it);
* **a detail panel** — click an element for its full resolved configuration:
  every interface, its addresses and VLANs, its MTU and MAC, every cable and
  tunnel that lands on it, and where it sits in an encapsulation stack. These
  are the same records `-f json` exports and [`netgraph web`](commands/web.md)
  shows, rendered by the same code;
* **toggles** — the addresses and the VLAN annotations off and on, and a
  namespace to focus while the rest of the network dims;
* **a layer switcher**, when you passed `--layer` more than once;
* **deep links** — selecting an element puts its id in the URL fragment, and
  opening that URL selects it again. The ids are the `--element-ids` ones, so
  `topology.html#node-sites_hq_sw-core` and `topology.svg#node-sites_hq_sw-core`
  name the same switch.

**Self-contained is meant literally.** The page makes no network requests of any
kind — no CDN, no web font, no stylesheet, no analytics, no image URL. The style
sheet and the client are hand-written vanilla CSS and JavaScript that ship
inside the package and are inlined at render time; there is no bundler, and
netgraph gained no runtime dependency for any of it. The only URLs a page can
hold are the ones `--link-template` was asked for, and those are links a reader
clicks rather than resources the page loads.

The page enforces that on itself: it carries a strict
`Content-Security-Policy` in a `<meta>`, built from the SHA-256 of each inline
block, so it needs neither `'unsafe-inline'` nor `'unsafe-eval'` and a page that
grew a fetch would be refused by the browser rather than quietly making one.
Everything an inventory wrote reaches the page as text — the escaping battery
covers a `</script>` in a description and in a `--title`.

Two consequences of there being no layout engine in a browser are worth knowing:

* **A toggle switches drawings, it does not re-flow one.** Graphviz decided
  where every shape goes, so the page embeds one properly laid out drawing per
  view — each layer, with and without the addresses and the VLANs — and shows
  the one you asked for. Identical drawings are stored once, so an inventory
  with no VLANs pays nothing for the VLAN toggle. That is also the size: expect
  roughly 43 kB of client plus a drawing per view, or ~174 kB for the
  three-layer example above. A view costs its drawing and essentially nothing
  else — the records are stored once for the whole page however many layers
  draw an element, and an `--icons` theme is stored once however many nodes and
  views use it, so turning icons on usually makes a page *smaller* rather than
  larger. [`tools/bench_html.py`](../tools/bench_html.py) is the harness those
  numbers come from.
* **`--no-show-ips` and `--no-show-vlans` are a ceiling, not a starting state.**
  Turning one off means the page holds no drawing that prints it *and* no record
  that carries it, so a published page cannot be talked into giving up an
  address by editing its JSON. Leaving it on means the page opens with it and
  can turn it off.

`--tooltips` is honoured as the hover card the page draws from those records;
`--no-tooltips` leaves clicking as the way in. `--icons`, `--group-by-namespace`
and every filter behave exactly as they do for `-f svg`, because it *is* the
`-f svg` pipeline underneath.

[`netgraph watch`](commands/watch.md) with `-f html -o topology.html` keeps the
file current while you edit, and `--serve` shows the page itself in the preview.

### The JSON export

`-f json` is the machine-readable face of a rendering: the *resolved* topology,
so a consumer gets name resolution, VLAN derivation and adapter attachment
without reimplementing any of it. Every reference is a fully-qualified name and
every collection is ordered deterministically, so two runs over the same
inventory produce byte-identical output and `git diff` on a committed export is
meaningful.

```json
{
  "apiVersion": "netgraph.dev/v1alpha1",
  "kind": "NetworkGraph",
  "layer": "l1",
  "nodes": [
    {
      "id": "hosts/pc-desk", "type": "element", "name": "pc-desk",
      "kind": "computer", "namespace": "hosts", "vlans": [10],
      "interfaces": [
        {"name": "eth0", "type": "ethernetCsmacd", "mac": "…",
         "addresses": ["192.168.10.20/24"], "vlan": {"mode": "access", "vlans": [10]}}
      ]
    }
  ],
  "edges": [
    {
      "id": "cables/cbl-pc", "kind": "cable",
      "endpoints": [{"node": "hosts/pc-desk", "interface": "eth0"},
                    {"node": "switches/sw-home", "interface": "port2"}],
      "medium": "copper", "speed": 1000000000, "speedText": "1Gbps", "vlans": [10]
    }
  ]
}
```

The `apiVersion`/`kind` pair is the schema version: within one `apiVersion`
keys are only ever added, never renamed or removed, and an absent optional key
means *not configured* rather than *unknown*. `title` appears only when
`--title` was given, and `dangling` only under `--force`, so an export that is
missing links says so rather than implying they do not exist. Node `id` is what
every edge endpoint refers to; an endpoint's `interface` is absent when the edge
attaches to an element rather than to one of its ports. A node's `type`
distinguishes a declared `element` from a derived one — a `subnet` at
`--layer l3`, a `tunnel` at `--layer overlay`, an `aggregate` under `--collapse`
— and each adds an object of that name. An `aggregate` node's object carries the
full list of elements it stands for, and a bundled edge carries a `bundle`
object holding every link folded into it, exported by the same code that exports
an unbundled one: a summary is machine-readable as a summary, never as a device
or as a cable.

`--show-ips` and `--show-vlans` control the *per-interface* detail, exactly as
they control what a diagram prints; node and link VLAN membership is always
exported, because it is topology rather than decoration.

For the other machine-readable artefacts an inventory can produce — a CSV cable
schedule, a DNS zone, an SVG rack elevation — see
[`netgraph export`](commands/export.md).

---

## See also

* [`netgraph render`](commands/render.md) — the command reference: synopsis,
  every flag, exit codes.
* [`netgraph.toml`](configuration.md#render--how-the-inventory-is-drawn) — give
  any of these options a default, and collect variations into
  [named profiles](configuration.md#profilename--named-variations).
* [`netgraph watch`](commands/watch.md) and [`netgraph web`](commands/web.md) —
  the same pipeline, redrawn on every save.
* [`netgraph path --highlight`](paths.md#drawing-the-answer---highlight) — one
  traced route drawn over the topology it crosses.
