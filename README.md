# netgraph

Declare your network — switches, routers, hubs, computers, servers, cables,
adapters and tunnels — in a folder tree of YAML files, then render it as a
network graph.

netgraph reads the tree, checks that the documents agree with each other, and
draws the result as SVG, PNG, PDF, Graphviz DOT, Mermaid or JSON. It can also
open the whole thing in a browser — `netgraph web` — where the YAML is edited
on one side, drawn on the other, and every node and link answers a hover with
its interfaces, addresses, VLANs and cabling.

![Layer-2 diagram of the home-lab example: a router, a switch, two computers, a server and a USB-to-Ethernet adapter, annotated with addresses and VLANs](docs/images/home-lab.svg)

<sub>Produced from [`examples/home-lab`](examples/home-lab) with
`netgraph -i examples/home-lab render --layer l2 --title "home-lab — layer 2" -f svg -o docs/images/home-lab.svg`.</sub>

> **Status: early development (0.1.0).** The schema, loader, validator,
> renderers and CLI work end to end; the schema may still change before 1.0.
> See [§12 of the specification](docs/schema.md#12-compatibility-policy).

## Why

Network documentation rots because the diagram and the truth live in different
places. The diagram is a drawing: nothing checks it, nothing regenerates it,
and the day someone re-patches a link it starts lying.

netgraph puts the source of truth in reviewable, diffable YAML next to the rest
of your infrastructure code, and generates the picture on demand. Because the
files are structured rather than drawn, they can be *checked*: a cable that
names a port which no longer exists is an error, not a line that happens to end
in the wrong place.

Field names and value spaces follow RFC 8343 (`ietf-interfaces`), RFC 8344
(`ietf-ip`) and the IEEE 802.1Q bridge model, so an inventory stays comparable
with what a device actually reports — see
[`docs/yang-mapping.md`](docs/yang-mapping.md).

## Installation

netgraph needs **Python 3.10 or newer**.

```bash
pip install -e .            # from a checkout
pip install -e '.[dev]'     # including the development tooling
```

### Graphviz is a system prerequisite

The `svg`, `png` and `pdf` formats are produced by running the Graphviz `dot`
binary, which is a system package rather than a Python one — so `pip` alone is
not enough:

```bash
sudo apt install graphviz        # Debian / Ubuntu
sudo dnf install graphviz        # Fedora / RHEL
brew install graphviz            # macOS
choco install graphviz           # Windows
```

Check it with `dot -V`. The `dot`, `mermaid` and `json` formats are written by
netgraph itself and work without it — so if you only need DOT output to feed
into another tool, you can skip the install.

## Quickstart

Five minutes, three devices, from an empty directory.

In a hurry? [`netgraph init`](#netgraph-init) writes exactly the tree this
section builds — including the editor wiring — and it validates and renders
straight away:

```bash
netgraph init my-network && cd my-network
netgraph validate
netgraph render -f svg -o network.svg
```

The rest of this section builds the same thing by hand, which is the part worth
reading once.

### 1. Make a folder

```bash
mkdir -p my-network/devices my-network/cables && cd my-network
```

The layout is up to you: netgraph loads every `*.yaml` and `*.yml` under the
root, at any depth. Directories become *namespaces*, so `devices/rtr-gw` is the
full name of the router below, and names only have to be unique within their
own folder.

### 2. Declare a router — `devices/rtr-gw.yaml`

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: router
metadata:
  name: rtr-gw
  labels: {site: office}
spec:
  vendor: MikroTik
  vlans:
    - id: 10
      name: office
  interfaces:
    - name: wan0
      type: ethernet
      description: ISP hand-off
      mtu: 1500
      ipv4:
        addresses: [203.0.113.2/30]
    - name: lan0
      type: ethernet
      description: Downlink to the switch
      mac: 00:1e:8c:aa:00:01
      mtu: 1500
      vlan:
        mode: access
        access_vlan: 10
      ipv4:
        addresses: [192.168.10.1/24]
```

Every document has the same four keys: `apiVersion`, `kind`, `metadata` and
`spec`. `203.0.113.2/30` is shorthand — write
`{ip: 203.0.113.2, prefix_length: 30}` instead if you prefer it explicit, or
`netmask: 255.255.255.252` if that is how your notes are written. All three
normalise to the same value.

### 3. Declare a switch — `devices/sw-office.yaml`

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-office
  labels: {site: office}
spec:
  vlans:
    - id: 10
      name: office
  interfaces:
    - name: port1
      type: ethernet
      description: Uplink to rtr-gw
      mtu: 1500
      vlan: {mode: access, access_vlan: 10}
    - name: port2
      type: ethernet
      description: Desk
      mtu: 1500
      vlan: {mode: access, access_vlan: 10}
```

The switch has no IP address at all, which is the point: it is a layer-2
bridge, and its ports carry VLAN membership rather than addressing. (A
management address would go on a `type: vlan` SVI — putting one on a bridge
port is warning `W104`.)

### 4. Declare a computer — `devices/pc-alice.yaml`

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-alice
  labels: {site: office}
spec:
  interfaces:
    - name: eno1
      type: ethernet
      mac: 00:1e:8c:bb:00:01
      mtu: 1500
      ipv4:
        addresses: [192.168.10.20/24]
```

Note what is *absent*: no `vlan` block. The host sends untagged frames and
inherits the VLAN of the access port facing it. That is the expected pairing,
and netgraph knows not to complain about it.

### 5. Connect them — `cables/links.yaml`

One file, two documents, separated by `---`:

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-rtr-sw}
spec:
  endpoints: [rtr-gw:lan0, sw-office:port1]
  medium: copper
  speed: 1Gbps
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-sw-alice}
spec:
  endpoints: [sw-office:port2, pc-alice:eno1]
  medium: copper
  speed: 1Gbps
```

A cable is its own element, not a field on a device. It joins exactly two
interfaces, written as `device:interface`, and the order carries no meaning.

### 6. Check it

```console
$ netgraph validate
no problems found
```

Try breaking something — rename `port2` to `port9` in the cable — and you get
the file, the document index, the line, the rule id and a message that lists
the interfaces the switch actually has:

```console
$ netgraph validate
errors (1):
  cables/links.yaml#1:9  E001  cable 'cables/cbl-sw-alice' endpoint sw-office:port9: 'devices/sw-office' has no interface 'port9'; it declares 'port1', 'port2'

1 error
```

`links.yaml#1:9` is the file, the second document in it (0-based) and line 9.

### 7. Draw it

```bash
netgraph render -f svg -o topology.svg
```

<p align="center"><img src="docs/images/quickstart.svg" alt="The three-device
quickstart topology: pc-alice and rtr-gw both cabled to sw-office, annotated
with addresses, VLANs and port names" width="360"></p>

### 8. Ask questions about it

```console
$ netgraph list devices
NAME               KIND      PORTS  ADDRESS           VLANS
-----------------  --------  -----  ----------------  -----
devices/pc-alice   computer      1  192.168.10.20/24  10
devices/rtr-gw     router        2  203.0.113.2/30    10
devices/sw-office  switch        2  -                 10

$ netgraph list subnets
SUBNET           IP  ADDRESSES  ELEMENTS  VLANS
---------------  --  ---------  --------  -----
192.168.10.0/24   4          2         2  10
203.0.113.0/30    4          1         1  -
```

That is the whole loop: write YAML, validate, render. Everything below is
detail.

While you are still editing, let netgraph run the loop for you:

```bash
netgraph watch --serve
```

Every save re-validates and re-renders, and the page at
<http://127.0.0.1:8080/> updates itself. See
[`netgraph watch`](#netgraph-watch).

The finished inventory is checked in as
[`examples/quickstart`](examples/quickstart), and the test suite validates and
renders it on every run — so if you got a different answer than this page
promised, that is a bug in netgraph rather than in your typing.

## Declaring a 48-port switch without typing it 48 times

A real access layer is dozens of near-identical documents, each of which is
dozens of near-identical ports. Two mechanisms remove the repetition without
weakening any check: both are applied by the loader before validation, so the
validator, the graph and every renderer still see plain interfaces and plain
devices.

**Interface ranges** (`docs/schema.md` §6.2.5). One entry may declare `range`
instead of `name`:

```yaml
interfaces:
  - range: GigabitEthernet1/0/[1-48]
    type: ethernet
    description: Access port {}          # {} and %d are the port number
    enabled: false
    vlan: {mode: access, access_vlan: 10}
```

Several spans expand as an odometer, rightmost fastest — `ge-[0-1]/0/[0-3]`
yields `ge-0/0/0` … `ge-1/0/3` — and the width of a span's low bound is its
zero padding, so `[01-12]` yields `01` … `12`. A document expands to at most
4096 interfaces; `eth[1-99999999]` is a diagnostic, not an out-of-memory kill.

**Device templates** (`docs/schema.md` §6.6). A `kind: template` document is a
named partial device `spec`; a device merges it in with `spec.from`:

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-north-acc-03
spec:
  from: templates/c9200l-48p
  bridge: {address: '00:1b:0d:01:a3:ff'}
  interfaces:
    - name: Vlan99
      ipv4: [10.1.99.13/24]
```

Nine lines instead of two hundred, and
[`examples/campus`](examples/campus/sites/north/access/switches.yaml) has that
switch next to two written out longhand so the two can be compared. The merge
rules are stated exactly in §6.6.1 and are worth knowing in full, but in short:
the device's own keys win, mappings merge key by key, `interfaces` merge by
`name`, and every other list the device declares replaces the template's
outright.

Templates are not elements — they never appear in a graph, in `netgraph list`,
or in validation output. The one place a template does surface is as the source
location of a field it contributed: a value the template got wrong is reported
against the template's file and line, with a note naming the device that
inherited it, so fifty devices do not report fifty copies of one mistake.

```text
templates/access-switch.yaml#0:52  NG-I011  spec.interfaces[2].mtu: mtu 1000 is below
  the IPv6 minimum of 1280 but the interface carries IPv6 addresses (inherited by
  'sw-north-acc-03' through 'spec.from: templates/c9200l-48p')
```

The file and the line are the template's; the note says who tripped over it.

Use `netgraph show NAME --raw` to read a device as written and
`netgraph show NAME` to read it merged.

## CLI reference

```
netgraph [GLOBAL OPTIONS] COMMAND [OPTIONS] [ARGS]
```

Running `netgraph` with no command prints the help. Every command loads the
inventory named by the global `-i/--inventory` option.

**Output discipline: data on stdout, commentary on stderr.** `render` writes
the diagram to stdout when no `--output` is given, so its findings and progress
notes go to stderr. `netgraph render -f json | jq` and
`netgraph validate > report.txt` both do what they look like they do. Colour is
used only when the stream is a terminal.

### Global options

| Option | Default | Effect |
|---|---|---|
| `-i, --inventory PATH` | current directory | Root folder of the YAML inventory tree, or a single YAML file. Must exist. |
| `-q, --quiet` | off | Only report errors. |
| `-v, --verbose` | off | Increase verbosity; repeatable (`-vv`). Progress notes go to stderr. |
| `--color` / `--no-color` | auto | Force coloured output on or off. Auto-detected from the terminal. |
| `-V, --version` | | Print the version and exit. |
| `-h, --help` | | Print help and exit. Works on every subcommand. |

### `netgraph init`

Scaffold a working inventory in an empty or new directory. The tree it writes
validates clean and renders at every layer before a line has been edited, and
each document carries a `yaml-language-server` modeline pointing at a JSON
Schema written alongside it — so the first key you type is completed and the
first typo underlined.

```console
$ netgraph init my-network
created 7 files in my-network:
  netgraph.toml
  .gitignore
  schema/netgraph.schema.json
  devices/rtr-gw.yaml
  devices/sw-office.yaml
  devices/pc-alice.yaml
  cables/links.yaml

next steps:
  cd my-network
  netgraph validate
  netgraph render -f svg -o network.svg
```

| Option | Default | Effect |
|---|---|---|
| `PATH` | current directory | Where to write. Created, with its parents, when it does not exist. |
| `--minimal` | off | Write a single commented envelope template instead of the three-device example topology. The tree then declares no elements at all. |
| `--force` | off | Write into a directory that already holds something, overwriting files of the same name. Without it an occupied directory is refused and left untouched. |
| `--schema` / `--no-schema` | `--schema` | Write `schema/netgraph.schema.json` and the modeline that points each document at it. `--no-schema` leaves the editor unwired. |

The example tree is the one the [quickstart](#quickstart) builds: a router, a
switch, a host and the two cables between them. `netgraph.toml` is written with
every `[validate]` key commented out and explained, and the `.gitignore` keeps
rendered diagrams — `*.svg`, `*.png`, `*.pdf`, `*.dot`, `*.mmd`, `/out/` — out
of the history while leaving the generated schema in it.

### `netgraph validate`

Check the inventory for schema and semantic problems. Exits 1 when anything is
reported as an error, 0 otherwise — so it drops straight into CI.

| Option | Default | Effect |
|---|---|---|
| `--strict` | off | Promote every warning to an error, so any finding fails the run. Can only turn strictness on; `netgraph.toml` decides otherwise. |
| `--disable RULE` | none | Silence a rule by id (`E001`, `NG-C002`, `*`). Repeatable. Adds to what `netgraph.toml` already ignores. |

Findings are grouped by severity, most severe first, and each line reads
`file.yaml#doc:line  RULE  message`. See
[`docs/validation-rules.md`](docs/validation-rules.md) for every rule.

### `netgraph render`

Render the inventory as a network graph. **Validation always runs first**, and
errors refuse the render unless `--force` is given: a diagram silently drawn
from an inventory with a dangling cable is worse than no diagram.

| Option | Default | Effect |
|---|---|---|
| `-f, --format FORMAT` | `dot` | One of `dot`, `svg`, `png`, `pdf`, `mermaid`, `json`. `svg`, `png` and `pdf` need Graphviz; the other three do not. |
| `-o, --output FILE` | stdout | Write to this file instead of stdout. Parent directories are created. Required for `png` and `pdf` when stdout is a terminal. |
| `--layer l1\|l2\|l3\|overlay` | `l1` | Which view to draw — see [Layers](#layers-l1-l2-l3-and-overlay). `l1` is the physical topology, `l2` the same topology annotated with VLANs, `l3` the IP subnets and who is addressed in them, `overlay` the tunnels and what runs inside what. |
| `--title TEXT` | none | Caption for the diagram. |
| `--show-ips` / `--no-show-ips` | on | Print configured IP addresses on the nodes. |
| `--show-vlans` / `--no-show-vlans` | on | Annotate nodes and links with VLAN membership. |
| `--group-by-namespace` | off | Draw each namespace as a visual group (a Graphviz cluster, a Mermaid subgraph). |
| `--icons THEME\|DIR` | off | Draw each element as an icon instead of a plain shape — see [Icons](#icons). `cisco`, `none`, or a directory of your own. Graphviz formats only. |
| `--tooltips` / `--no-tooltips` | on | Carry the full record of every element — interfaces, addresses, VLANs, cabling — as hover text. `dot` and `svg` only; see [Interactive SVG](#interactive-svg-tooltips-links-and-ids). |
| `--link-template URL` | off | Link each element back to the YAML that declares it, e.g. `https://git.example.com/net/blob/main/{file}#L{line}`. `dot` and `svg` only. |
| `--element-ids` | off | Give every node, edge and namespace a stable `id` derived from its name, so the diagram can be deep-linked and styled. `dot` and `svg` only. |
| `--strict` | off | Treat warnings as errors, which then also refuse the render. |
| `--force` | off | Render even when validation failed. The diagram may not match the files. |

**Filters** narrow what is drawn. Values *within* one option are alternatives;
different options are combined with AND, so `--namespace sites/north --kind
switch` keeps the switches of that site only. An unset filter selects
everything, and filtering never changes what the remaining nodes say about
themselves.

| Option | Repeatable | Keeps |
|---|---|---|
| `--namespace NS` | yes | Elements in `NS` or in any namespace below it. |
| `--vlan VID` | yes | Elements participating in that VLAN (1–4094). A host on an untagged access port counts as a member. |
| `--kind KIND` | yes | Elements of that kind: `switch`, `router`, `hub`, `computer`, `server`, `adapter`. A cable is an edge and so is a tunnel, so neither is selectable; both follow whichever elements survive. |
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

```bash
netgraph render -f json | jq '.nodes[].name'
netgraph render -f mermaid -o docs/topology.mmd
netgraph render --vlan 10 --layer l2 -f svg -o vlan-10.svg
netgraph render --layer l3 -f svg -o subnets.svg
netgraph render --neighbors-of sw-dist-01 --depth 2 -f svg -o around-dist.svg
netgraph render --kind switch --kind router --group-by-namespace -o core.dot
```

Mermaid's renderer refuses a diagram of more than 500 edges, and that ceiling is
a secure config a document is not allowed to raise for itself — so GitHub,
GitLab and `mmdc` will not draw one, however valid it is. `render` warns when it
crosses the line and names the filters that would bring it back down; `-f dot`
and `-f svg` have no such limit.

#### Icons

By default a node is a Graphviz shape — a diamond for a router, a 3-D box for a
switch — which keeps a diagram readable with nothing installed. `--icons` swaps
the shape for a picture:

```bash
netgraph render --icons cisco --layer l2 -f svg -o topology.svg
```

![The home-lab example drawn with the bundled cisco theme: a router cylinder, a
switch slab, two monitors, a server tower and a dongle, joined by labelled
links](docs/images/home-lab-icons.svg)

<sub>`netgraph -i examples/home-lab render --layer l2 --icons cisco --title "home-lab — layer 2, cisco icons" -f svg -o docs/images/home-lab-icons.svg`.</sub>

Only *how* a node is drawn changes. The labels, the addresses, the VLANs, the
edges and every filter behave exactly as they do without a theme, and a kind the
theme has no picture for keeps its plain shape rather than disappearing.

**`cisco`** ships with netgraph and covers every kind, including the subnet
clouds of `--layer l3`. The artwork is drawn in the topology idiom Cisco made
the industry convention and is netgraph's own, under the same MIT licence as the
rest of the package — Cisco's published icon library is copyrighted and is not
redistributed here.

**A directory** works just as well, which is how you use that library, or any
other set, if you have it. A theme is nothing but a directory of images named
after the kinds they stand for — `router`, `switch`, `hub`, `computer`,
`server`, `adapter` and `subnet`, with an `.svg`, `.png`, `.jpg` or `.gif`
extension:

```bash
ls my-icons/          # router.png  switch.png  server.png
netgraph render --icons ./my-icons -f svg -o topology.svg
```

Files for kinds you do not cover are simply absent; those nodes keep their plain
shape, so a set of three icons is a usable theme. `--icons none` turns a theme
back off, for a wrapper script that always passes the option.

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

#### Interactive SVG: tooltips, links and ids

An SVG is the artefact that gets committed to a repository or dropped into a
wiki, and it can carry more than the picture. Three attributes travel with it,
none of which changes the drawing:

```bash
netgraph render -f svg --element-ids \
    --link-template 'https://git.example.com/net/blob/main/{file}#L{line}' \
    -o docs/topology.svg
```

| Flag | Honoured by | Ignored by | What it does |
|---|---|---|---|
| `--tooltips` (default on) | `svg`, `dot` | `png`, `pdf`, `mermaid`, `json` | Hover text on every node, edge and namespace box. |
| `--link-template URL` | `svg`, `dot` | `png`, `pdf`, `mermaid`, `json` | Turns each element into a link to the document that declares it. |
| `--element-ids` | `svg`, `dot` | `png`, `pdf`, `mermaid`, `json` | A stable `id` on every node, edge and cluster. |

`-f dot` writes the attributes because a DOT file is the input to somebody
else's `dot`; `-f svg` is where they reach a reader. `png` and `pdf` are
pictures and drop them silently — netgraph warns when you asked for one of the
three and picked a format that cannot carry it. `mermaid` and `json` have
interaction models of their own and ignore all three.

**Tooltips** are the same per-element records `netgraph web` shows in its info
boxes, rendered as plain text — one builder, so a committed diagram and the live
preview cannot disagree. Hovering the switch of the [quickstart](#quickstart)
inventory gives:

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

#### The JSON export

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
attaches to an element rather than to one of its ports. At `--layer l3` a node's
`type` distinguishes a declared `element` from a derived `subnet`.

`--show-ips` and `--show-vlans` control the *per-interface* detail, exactly as
they control what a diagram prints; node and link VLAN membership is always
exported, because it is topology rather than decoration.

### Layers: l1, l2, l3 and overlay

One inventory, four questions. `--layer` picks which one the diagram answers.

| Layer | Nodes | Edges | Annotations | Reach for it when |
|---|---|---|---|---|
| `l1` | devices and adapters | one per cable, one per adapter attachment, one per tunnel | medium, link rate, cable label, length; encapsulation on a tunnel | You are standing at the rack. "Which port is this patched into, and with what?" |
| `l2` | the same | the same | VLAN membership per node and per link, port mode | "Is this host in VLAN 10 all the way to the gateway?" Broadcast domains, trunk pruning, a VLAN that stops one switch short. |
| `l3` | the elements that hold a routable address, **plus one node per IP prefix** | one per address: element ↔ the subnet it is addressed in, labelled with the interface and the address | VLANs the prefix is reachable in | "Why can these two not reach each other?" The addressing plan, gateways, a subnet mask that is one bit off. |
| `overlay` | the elements that terminate a tunnel, **plus one node per tunnel** | one per endpoint, plus one per `over` — this tunnel runs inside that one | encapsulation stack, VNI, MTU budget, what encrypts | "Is this traffic actually protected, and what carries it?" VPNs, VXLAN fabrics, a cleartext overlay somebody assumed was private. |

`l1` and `l2` are the same graph drawn twice. `l3` is a **different graph**:
cables do not appear, because two devices are adjacent at layer 3 when they
share a prefix — not when a cable happens to run between them (a route may
cross three switches; a trunk carries VLANs neither end routes).

![Layer-3 diagram of the home-lab example: five elements joined to the five IP prefixes they are addressed in, each edge labelled with the interface and its address](docs/images/home-lab-l3.svg)

<sub>The same inventory as the diagram at the top of this file, at layer 3:
`netgraph -i examples/home-lab render --layer l3 --title "home-lab — layer 3" -f svg -o docs/images/home-lab-l3.svg`.
The router's loopback and its ISP hand-off are prefixes of their own; the switch
appears only because its management SVI holds an address.</sub>

What layer 3 leaves out is deliberate:

* **Elements with no routable address.** A layer-2-only switch says nothing
  about IP reachability, so it is omitted rather than drawn floating beside the
  subnets it is not in. Give it a management SVI and it appears.
* **Loopback and link-local addresses**, and unnumbered interfaces. `127.0.0.1`,
  `::1` and `fe80::/10` are scoped to one host or one link, so they are not
  prefixes of *this* network.
* **VLAN identity of a prefix.** Grouping is by prefix alone, because that is
  what a routing table keys on. A prefix deliberately re-used in two VLANs
  therefore appears once — which is exactly what `W106` below points out.

The `overlay` layer is a different graph again. Every tunnel becomes a **node**,
joined to each element it terminates on and to the tunnel it runs inside:

![Encapsulation diagram of the overlay example: three routers and a workstation joined to five tunnel nodes, with the VXLAN and GRE tunnels each drawn running inside the IPsec tunnel](docs/images/overlay.svg)

<sub>Produced from [`examples/overlay`](examples/overlay) with
`netgraph -i examples/overlay render --layer overlay --group-by-namespace --title "overlay — encapsulation" -f svg -o docs/images/overlay.svg`.</sub>

A tunnel has to become a node there because nesting is a relation between two
*links*, and a link cannot end on a link — which is why `vxlan over ipsec` is
undrawable at layer 1 and obvious here. Below that layer a point-to-point tunnel
stays a dashed edge, so `netgraph render` shows the VPNs over the physical
topology without a box in the middle of each one.

Two problems are visible only from here, and `netgraph validate` reports both:

| Rule | Fires when |
|---|---|
| [`W105`](docs/validation-rules.md#w105--subnet-with-a-single-member) | Exactly one element is addressed in a prefix — a typo'd prefix length, or a neighbour nobody wrote down. Host routes and point-to-point prefixes are exempt. |
| [`W106`](docs/validation-rules.md#w106--one-address-claimed-twice-in-a-subnet) | Two elements claim the same address in one prefix from different VLANs, so the layer-3 view cannot tell which of them answers. |

`netgraph list subnets` prints the same grouping as a table, and
`render --layer l3 -f json` exports it with a `type` discriminator on every node
(`element` or `subnet`) so a consumer can tell a derived prefix from a declared
device.

### `netgraph watch`

Re-render whenever a file in the inventory changes, optionally serving the
result on a page that reloads itself. Every cycle is the same load, validate
and render `netgraph render` performs, followed by a timestamped status line
and any findings.

```
netgraph watch [-f FORMAT] [-o FILE] [--serve [--host ADDRESS] [--port PORT]] [FILTERS]
```

```
09:41:02  ok       23 nodes, 26 edges → topology.svg (128 ms)
09:41:37  invalid  1 error; keeping the render from before
errors (1):
  sites/hq/links.yaml#0:12  E001  cable 'sites/hq/cbl-07' endpoint sw-hq:port9: no element named 'sw-hq' is declared in this inventory
```

**A failed cycle changes nothing.** The file written by `--output` keeps its
last valid contents and the preview keeps serving the last valid diagram, so a
half-typed document never blanks the picture you are working from. Nothing ends
the loop except Ctrl-C: a syntax error, a deleted root, a `--neighbors-of`
target that no longer resolves are all statuses, not crashes.

Every filter and display option of `netgraph render` applies here too —
`--tooltips`, `--link-template` and `--element-ids` included, which is what
makes `watch -f svg -o topology.svg` keep an interactive diagram up to date —
plus:

| Option | Default | Effect |
|---|---|---|
| `-f, --format FORMAT` | `svg` | As for `render`, but defaulting to `svg` — a live preview wants a picture. |
| `-o, --output FILE` | none | Rewrite this file after every successful render, atomically: a reader sees the old diagram or the new one, never half of each. |
| `--serve` | off | Also host the render over HTTP. The page polls once a second and swaps the diagram in when it changes. |
| `--host ADDRESS` | `127.0.0.1` | Address `--serve` binds to. |
| `--port PORT` | `8080` | Port `--serve` binds to; `0` lets the operating system choose one. |
| `--debounce MS` | `300` | How long a burst of filesystem events is collected before re-rendering. One editor save is several events. |

Only YAML documents, `netgraph.toml` and `.netgraphignore` trigger a render;
an editor swap file or a rendered diagram does not, and neither does anything
under a directory the loader skips (`.git/`, `_drafts/`, …).

**The preview is bound to loopback and stays there unless you say otherwise.**
An inventory describes internal network topology — addresses, VLANs, what is
plugged into what — so `--host` is the explicit act of publishing it, and doing
so prints a warning. The server answers `GET` and `HEAD` on five fixed routes,
never turns a request path into a file name, and refuses a request that reached
a loopback preview under a foreign `Host` header. It is a development server:
do not put it on a hostile network.

```bash
netgraph watch --serve                                   # preview at http://127.0.0.1:8080/
netgraph watch -f svg -o topology.svg                    # just keep a file up to date
netgraph watch --serve --layer l2 --vlan 10 --title vlan10
netgraph watch --serve --host 0.0.0.0 --port 9000        # deliberate, and warned about
```

### `netgraph web`

Edit a YAML document stream in the browser and watch it being drawn, with an
info box on every node and link.

```
netgraph web [SOURCE] [--host ADDRESS] [--port PORT] [--no-open] [--icons THEME|DIR]
```

```bash
netgraph web                                  # opens on the netgraph init example
netgraph web examples/home-lab                # seeded from a folder, flattened into one stream
netgraph web devices/sw-office.yaml           # seeded from one file
kubectl get cm topology -o jsonpath={..yaml} | netgraph web   # or from a pipe
```

![The netgraph web interface: the YAML document stream on the left, the rendered layer-2 diagram on the right, and the info box open on a switch showing its interfaces, addresses, VLANs and links](docs/images/web.png)

<sub>Hovering `sw-home` in [`examples/home-lab`](examples/home-lab): every port,
its addresses and VLAN mode, and what each one is cabled to.</sub>

The page is two panes. On the left is the document stream — one or more
documents separated by `---` — and the problems found in it; on the right is
the diagram, which re-renders about half a second after you stop typing. The
same load, validate and render the command line performs runs on every pass, so
what the page reports is what `netgraph validate` would say about the same
text.

**Hovering a node or a link opens an info box** holding what the diagram has no
room for: every interface with its type, MAC, MTU, addresses and VLAN mode;
every link that terminates on the element, what it runs to and over which port;
and, at layer 3, the prefix a subnet node stands for and who is addressed in
it. Everything it shows is the same data `netgraph render -f json` exports —
the records *are* that export — so the two cannot drift apart, and they are the
same records a committed SVG carries as
[tooltips](#interactive-svg-tooltips-links-and-ids). The element
under the pointer and everything it touches are lifted out of the diagram while
the box is open; click to pin the box, click again or press `Esc` to let go.

Beyond that: the layer, the VLAN filter and the display toggles are in the
header and apply on the next render; the canvas pans with a drag and zooms with
the wheel; clicking a problem puts the cursor on the line that caused it; and
the splitter between the panes moves.

**Broken text still draws.** `netgraph render` refuses an inventory with errors
unless `--force`, because a diagram that disagrees with the files misinforms
whoever is shown it. Here the diagram *is* the feedback and text being edited is
wrong most of the time, so every problem is listed with its line and whatever
resolved is drawn anyway.

| Option | Default | Effect |
|---|---|---|
| `SOURCE` | the `netgraph init` example | Seed for the editor: a file, or a folder whose documents are concatenated. A pipe on stdin wins over both. |
| `--host ADDRESS` | `127.0.0.1` | Address to bind. |
| `--port PORT` | `8081` | Port to bind; `0` lets the operating system choose one. One above the `watch` preview's, so both can run at once. |
| `--open` / `--no-open` | `--open` | Open the page in the default browser once the server is listening. |
| `--icons THEME\|DIR` | none | Draw elements as icons, exactly as for `render`. Chosen here rather than in the browser, because it names a directory on this machine. |

`netgraph.toml` is not read here either — a stream has no folder to look for
one in — so the rules are the built-in defaults plus the `strict` toggle in the
header.

A stream has no folders and therefore **no namespaces**: every element seeded
from a tree lands in the root namespace, and two elements that shared a short
name in different folders will collide. Deep trees belong in
`netgraph watch --serve`; this command is for a snippet, a paste or a file.

The same restrictions apply as to the `watch` preview — loopback by default,
a fixed set of routes, no request path ever turned into a file name, a `Host`
header check — plus two of its own: a request body is capped at 1 MB, and the
SVG is parsed and stripped of anything that could execute or navigate before it
is put into the page. It is a development server: do not put it on a hostile
network.

### `netgraph list`

List what the inventory declares.

```
netgraph list [devices|cables|tunnels|vlans|subnets] [-F table|json|yaml]
```

| Argument | Columns |
|---|---|
| `devices` (default) | name, kind, port count, first routable address, VLANs |
| `cables` | name, medium, speed, both ends, length |
| `tunnels` | name, encapsulation stack, VNI, what protects it, endpoints |
| `vlans` | id, name, member elements, member ports |
| `subnets` | prefix, family, address count, element count, VLANs |

| Option | Default | Effect |
|---|---|---|
| `-F, --output-format` | `table` | `table` is for reading; `json` and `yaml` are for piping, and carry more fields than the table has room for. |

`vlans` and `subnets` are computed from the resolved graph, not from what each
document literally says: a host on an untagged access port is listed as a member
of that VLAN even though it declares none. Loopback and link-local prefixes are
left out of `subnets`, since listing `127.0.0.0/8` once per machine would say
nothing about the addressing plan. `subnets` is the same grouping
[`--layer l3`](#layers-l1-l2-l3-and-overlay) draws, and `tunnels` the same
resolution `--layer overlay` draws, so the tables and the diagrams can never
disagree. The `ENCRYPTED` column reads `underlay` for a tunnel that encrypts
nothing itself but runs inside one that does:

```console
$ netgraph -i examples/overlay list tunnels
NAME                STACK             VNI  ENCRYPTED  ENDS  ENDPOINTS
------------------  ----------------  ---  ---------  ----  ----------------------------------------------
tunnels/wg-mesh     wireguard           -  yes           3  rtr-branch-a:wg0, rtr-branch-b:wg0, rtr-hq:wg0
tunnels/ipsec-hq-b  ipsec               -  yes           2  rtr-branch-b:ipsec0, rtr-hq:ipsec0
tunnels/vx-100      vxlan over ipsec  100  underlay      2  rtr-branch-b:vxlan100, rtr-hq:vxlan100
tunnels/gre-mgmt    gre over ipsec      -  underlay      2  rtr-branch-b:gre1, rtr-hq:gre1
tunnels/ovpn-admin  openvpn             -  yes           2  pc-branch-b:tun0, rtr-hq:ovpn0
```

### `netgraph show`

Print the fully resolved configuration of one element — defaults materialised,
values normalised. This is what netgraph actually works with, rather than what
was typed.

```
netgraph show NAME [-F yaml|json]
```

`NAME` is a fully-qualified name (`sites/hq/sw1`) or a short name that is
unique in the inventory; an ambiguous short name is a usage error listing the
candidates.

| Option | Default | Effect |
|---|---|---|
| `-F, --output-format` | `yaml` | `yaml` or `json`. |
| `--raw`, `--no-expand` | off | Print the document as written instead: an interface `range` still a range, a `spec.from` still a reference. |

Use it to see what a shorthand expanded to: `10.0.0.1/24` becomes `{ip,
prefix_length}`, an access port gains its derived
`acceptable_frames: admit-only-untagged-and-priority-tagged`, and a router's
interfaces gain the `forwarding` it inherited from the device.

`--raw` is the other half of the same question. Diff the two and you have read
the merge:

```bash
diff <(netgraph show sw-north-acc-03 --raw) <(netgraph show sw-north-acc-03)
```

### `netgraph rules`

List the validation rules, their severity and their schema aliases. No options.
The table is printed from the same source the validator uses, so it always
describes the build you are running.

### `netgraph schema`

Print the JSON Schema (2020-12) for netgraph documents, generated from the same
pydantic models the loader uses. Needs no inventory.

| Option | Default | Effect |
|---|---|---|
| `--all` | on | One schema covering every kind, discriminated on `kind`. |
| `-k, --kind KIND` | | Emit the schema for a single kind instead — including `template`. Mutually exclusive with `--all`. |
| `-o, --output FILE` | stdout | Write to a file instead of stdout. |

Point an editor at it and a typo'd key is underlined as you type rather than
found by the next `netgraph validate` — see
[editor setup](#editor-setup-autocompletion-and-inline-errors) below, or let
[`netgraph init`](#netgraph-init) wire it up for you.

### `netgraph completion`

Print the shell completion script for `bash`, `zsh` or `fish` on stdout. Needs
no inventory.

```bash
# bash — needs bash-completion installed
netgraph completion bash > ~/.local/share/bash-completion/completions/netgraph

# zsh — any directory on $fpath will do
mkdir -p ~/.zfunc && netgraph completion zsh > ~/.zfunc/_netgraph
# and, in ~/.zshrc, before compinit:
#   fpath=(~/.zfunc $fpath)

# fish
netgraph completion fish > ~/.config/fish/completions/netgraph.fish
```

Start a new shell afterwards. To try it without installing anything, source it
in the current shell instead — `eval "$(netgraph completion bash)"`,
`eval "$(netgraph completion zsh)"`, `netgraph completion fish | source`.

Commands and flags complete as you would expect, and the values that are worth
completing do too:

| At the cursor | Offers |
|---|---|
| `netgraph show <TAB>` | Every element of the inventory named by `-i`, fully qualified and by short name, described by its kind. |
| `--neighbors-of <TAB>` | The same, minus the cables: a cable is an edge, not a node. |
| `-f/--format <TAB>` | The registered output formats, with what each one produces. |
| `--layer <TAB>` | `l1`, `l2`, `l3`, with what each one draws. |
| `--kind <TAB>` | The element kinds the option accepts — no `cable` on a filter, `cable` included on `netgraph schema`. |
| `--disable <TAB>` | Rule ids with their summaries, `*` included; type `NG-` for the schema aliases. |

The inventory-aware completers read the tree pointed at by `-i`, so
`netgraph -i examples/campus show sites/north/<TAB>` completes that site. They
never fail loudly: a tree that is half-written — which is exactly when you reach
for completion — simply offers nothing. zsh and fish show the descriptions next
to each candidate; bash lists the values alone, as it does for everything.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | The inventory was rejected: `validate` found errors, `render` refused to draw, or `init` refused to write into an occupied directory. |
| 2 | Usage error, or an unusable `netgraph.toml`. |
| 3 | The inventory could not be discovered or read at all. |
| 5 | The rendering could not be produced (Graphviz missing, output not writable, binary format to a terminal). |
| 6 | `watch --serve` or `web` could not bind its address. |
| 130 | Interrupted. |
| 141 | The downstream end of a pipe closed first. |

## Editor setup: autocompletion and inline errors

Inventories are written by hand, so the editor is the first place a mistake can
be caught. `netgraph schema` emits a [JSON Schema 2020-12](https://json-schema.org/draft/2020-12/release-notes)
document generated from the pydantic models; the yaml-language-server behind
VS Code, Neovim and the JetBrains IDEs turns it into completion, hover
documentation and squiggles under bad values.

A generated copy is committed at
[`schema/netgraph.schema.json`](schema/netgraph.schema.json), so you can use it
without installing netgraph first — and
[`netgraph init`](#netgraph-init) writes one into a new inventory with the
modeline below already on every document, which is the setup-free path.

**Per file, no configuration.** The language server reads a modeline on the
first line:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/netgraph/netgraph/main/schema/netgraph.schema.json
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-office
```

A relative path (`$schema=../../schema/netgraph.schema.json`) works too and
keeps the tree usable offline.

**Whole tree, in VS Code.** Install the
[YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)
and add one glob to `.vscode/settings.json`:

```json
{
  "yaml.schemas": {
    "./schema/netgraph.schema.json": ["inventory/**/*.yaml"]
  }
}
```

Neovim's `yamlls` and the JetBrains IDEs take the same mapping. Regenerate the
committed copy with `python tools/gen_json_schema.py`; `tests/test_schema.py`
fails when it drifts from the models.

The schema is versioned alongside `apiVersion` — its `$id` is
`https://netgraph.dev/schema/v1alpha1/element.json`, and a future `v1beta1` gets
its own rather than replacing it.

**It does not replace `netgraph validate`.** The schema sees one document at a
time, so it checks structure, value grammars and rules within a single object.
Whether a cable endpoint names an element that exists, whether names are unique,
whether two ends of a link agree on a VLAN — all of that needs the whole tree
and stays with `netgraph validate`. Keep running it in CI.
[`docs/schema.md` §13](docs/schema.md#13-editor-integration) has the full
comparison and the per-kind setup.

## Configuration

An optional `netgraph.toml` at the inventory root re-grades or silences rules:

```toml
[validate]
strict = false                    # promote surviving warnings to errors
ignore = ["W103", "NG-C010"]      # never report these at all

[validate.severity]
E004 = "warning"                  # re-grade rather than silence
```

Individual elements can opt out with an annotation:

```yaml
metadata:
  name: spare-switch
  annotations:
    netgraph/ignore: "W103, E004"     # or "*" for every rule
```

Both mechanisms, their precedence and what cannot be suppressed are covered in
[`docs/validation-rules.md`](docs/validation-rules.md#suppressing-a-rule).

A `.netgraphignore` file keeps paths out of the inventory entirely. It uses the
subset of `.gitignore` syntax that makes sense for an inventory tree — `!`
negation, trailing `/` for directories, `**`, character classes — and one in a
subdirectory applies to that subtree. Files and directories whose name starts
with `.` or `_` are skipped without one.

### Environment variables

| Variable | Effect |
|---|---|
| `NO_COLOR` | Suppress colour, per [no-color.org](https://no-color.org). |
| `NETGRAPH_YAML_LOADER` | Which YAML parser to use: `auto` (default), `python` or `libyaml`. |

`auto` takes PyYAML's libyaml bindings when the installed wheel carries them —
several times faster on a large inventory — and falls back to the pure-Python
parser otherwise. The two accept exactly the same documents and report the same
line and column for a problem; they differ only in PyYAML's own wording for a
syntax error. Set `python` to pin the slow path, or `libyaml` to refuse to start
without the fast one.

## Examples

Three ready-to-run inventories live under [`examples/`](examples). All of them
load without a schema error and validate clean against every rule, with no
suppressions:

| Inventory | Size | Shows |
|---|---|---|
| [`examples/quickstart`](examples/quickstart) | 3 devices, 2 cables | The walkthrough above, checked in so it stays executable. |
| [`examples/home-lab`](examples/home-lab) | 5 devices, 1 adapter, 4 cables | Router, switch, two computers, a server and a USB-to-Ethernet adapter on a single VLAN. |
| [`examples/campus`](examples/campus) | 22 devices, 22 cables | Nested namespaces across three sites, layer-3 core routers in a fibre backbone ring, VLAN trunks from access to distribution, and one access switch declared from a [template](examples/campus/templates/access-switch.yaml). |

```bash
netgraph -i examples/campus validate
netgraph -i examples/campus render --namespace sites/north --layer l2 -f svg -o north.svg
```

## Documentation

| Document | What it is for |
|---|---|
| [`docs/schema.md`](docs/schema.md) | The specification. Why the schema looks the way it does, with three complete worked examples, and the editor setup in §13. |
| [`docs/schema-reference.md`](docs/schema-reference.md) | Every field, its type, whether it is required, its default and its YANG path. Generated from the models. |
| [`docs/validation-rules.md`](docs/validation-rules.md) | Every rule, its severity, why it matters and how to suppress it. |
| [`docs/yang-mapping.md`](docs/yang-mapping.md) | The relationship to RFC 8343, RFC 8344 and IEEE 802.1Q — including what is deliberately not covered. |
| [`docs/follow-ups.md`](docs/follow-ups.md) | Known gaps, deferred deliberately: what was measured, why it was left, and what a fix would have to do. |

## As a library

```python
from pathlib import Path

from netgraph.config import load_config
from netgraph.loader import load_tree
from netgraph.render import RenderOptions, build_graph, icon_theme, render
from netgraph.validate import validate

root = Path("inventory")
inventory = load_tree(root)
for finding in validate(inventory, load_config(root).validation):
    print(finding)  # inventory/sw1.yaml#0:3: error: E002: ...

options = RenderOptions(show_ips=False, icons=icon_theme("cisco"))
svg = render(build_graph(inventory), "svg", options)
```

`load_tree` never raises for a problem *inside* the tree; unreadable documents
are collected on `inventory.errors`. `validate` never raises either — it
returns findings and lets the caller decide. The package is typed
(`py.typed`) and checked with `mypy --strict`.

Text that never was a folder — a paste, a pipe, a snippet from a ticket — goes
through `load_stream` instead, and `render_source` runs the whole of what
`netgraph web` does per keystroke in one call:

```python
from netgraph.loader import load_stream
from netgraph.render import Layer
from netgraph.web import ViewOptions, render_source

text = Path("topology.yaml").read_text()
inventory = load_stream(text)  # same parser, same schema, same rules

preview = render_source(text, ViewOptions(layer=Layer.L2))
preview.svg  # an <svg> fragment, safe to embed, with an id on every element
preview.details["n0"]  # the info-box record for the first node
preview.problems  # load errors and findings, most severe first
```

`render_source` never raises for anything the text can be wrong about: a syntax
error, a dangling cable and a filter that matches nothing all come back as a
preview whose `status` and `problems` say so, with whatever resolved still
drawn.

## Project layout

```
docs/               specification, generated reference, rule and YANG guides
examples/           four runnable inventories, also used as golden fixtures
schema/             the generated JSON Schema, for editors and CI
tools/              doc and schema generators (checked for drift by the tests),
                    the icon rasteriser, plus the pipeline benchmark harness
src/netgraph/
├── __init__.py     public package surface
├── cli.py          console-script entry point (netgraph)
├── completion.py   shell completion: the scripts, and the value completers
├── console.py      terminal output: tables, colour, TTY detection
├── errors.py       shared exception hierarchy
├── config.py       per-inventory settings (netgraph.toml)
├── scaffold.py     the starter inventory netgraph init writes
├── rules.py        catalogue of validation rules and severities
├── schema.py       JSON Schema emitted for editors (netgraph schema)
├── subnets.py      IP prefixes derived from the configured addresses
├── validate.py     semantic validation engine
├── models/         pydantic models for every element kind
├── loader/         recursive YAML inventory loader
│   ├── tree.py     the walk, and the two-phase build templates make necessary
│   ├── ranges.py   bracket expansion of interfaces[].range
│   ├── templates.py  the template registry and the spec merge
│   └── provenance.py  which file and line each field of a rewritten document came from
├── render/         graph construction and output renderers
│   ├── graph.py    inventory -> nodes, edges, VLAN membership, subnets; filtering
│   ├── dot.py      Graphviz DOT, and the SVG/PNG/PDF it produces
│   ├── details.py  per-element hover records, and the text a tooltip shows
│   ├── ids.py      the stable id each drawn node, edge and cluster carries
│   ├── links.py    --link-template: a URL back to the document behind an element
│   ├── icons.py    icon themes: a directory of images named after element kinds
│   ├── iconsets/   the bundled themes; one directory each, SVG and PNG
│   ├── templates/  the Jinja2 template the DOT document is laid out by
│   ├── mermaid.py  Mermaid flowchart exporter
│   ├── jsonexport.py  canonical JSON graph export
│   └── registry.py    one entry per output format; the CLI reads it, never a list of names
├── httpserve.py    what the two local servers promise: loopback, headers, host check
├── watch/          live re-rendering (netgraph watch)
│   ├── pipeline.py one load -> validate -> render cycle, and its published state
│   ├── loop.py     what counts as a change, and what to do when one arrives
│   └── server.py   the loopback HTTP preview and its self-reloading page
└── web/            the interactive interface (netgraph web)
    ├── preview.py  one parse -> validate -> render pass over a document stream
    ├── svgdoc.py   the Graphviz SVG made safe to embed in a live page
    ├── server.py   five routes over all of it
    └── assets/     the page, its style sheet and its dependency-free client
```

## Development

```bash
pip install -e '.[dev]'
pre-commit install                       # optional, runs the checks on commit

pytest                                   # tests, with coverage configured in pyproject.toml
ruff check . && ruff format --check .     # lint and format
mypy                                     # static type check (strict)

python tools/gen_schema_reference.py     # regenerate docs/schema-reference.md
python tools/gen_json_schema.py          # regenerate schema/netgraph.schema.json

# generate a 1000-device inventory and time every stage over it; --compare-loaders
# additionally times the parse step through both YAML parsers
python tools/bench_pipeline.py --compare-loaders

# break the cost of `validate` down by rule over the same tree
python tools/profile_validate.py --top 10

# capture every command's output over every inventory in the repository, on both
# YAML parser paths, so a refactor can be shown to have changed none of them
tools/snapshot_outputs.sh /tmp/before && tools/snapshot_outputs.sh /tmp/after
diff -r /tmp/before /tmp/after
```

`docs/schema-reference.md` and `schema/netgraph.schema.json` are both generated
from the pydantic models, and `tests/test_docs.py` and `tests/test_schema.py`
fail when either is stale — so a model change that is not
reflected in the reference fails the build. The same file also checks that
every rule has a write-up in `docs/validation-rules.md` and that every relative
link and anchor in the Markdown resolves.

The two committed diagrams are rendered from the checked-in examples:

```bash
netgraph -i examples/home-lab render --layer l2 --title "home-lab — layer 2" \
    -f svg -o docs/images/home-lab.svg
netgraph -i examples/quickstart render -f svg -o docs/images/quickstart.svg
netgraph -i examples/home-lab render --layer l3 --title "home-lab — layer 3" \
    -f svg -o docs/images/home-lab-l3.svg
netgraph -i examples/home-lab render --layer l2 --icons cisco \
    --title "home-lab — layer 2, cisco icons" -f svg -o docs/images/home-lab-icons.svg
```

The bundled icons are drawn as SVG and committed alongside a PNG of each, since
Graphviz cannot read an SVG image in its cairo-backed outputs. After editing
one, re-run the rasteriser — `--check` reports staleness without writing:

```bash
pip install cairosvg                     # only this tool needs it
python tools/render_icons.py
```

## License

[MIT](LICENSE)
