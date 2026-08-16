# netgraph

[![Live demo](https://img.shields.io/badge/demo-try%20it%20in%20your%20browser-0b5ed7)](https://blechschmidt.github.io/netgraph/demo/)

Declare your network — switches, routers, hubs, computers, servers, cables, adapters,
tunnels and patch panels — in a folder tree of YAML files, then render it as a network
graph.

netgraph reads the tree, checks that the documents agree with each other, and draws the
result as SVG, PNG, PDF, Graphviz DOT, Mermaid or JSON. It can also open the whole thing
in a browser — `netgraph web` — where the YAML is edited on one side, drawn on the other,
and every node and link answers a hover with its interfaces, addresses, VLANs and cabling.
That page is driveable entirely from the keyboard, `Ctrl-K` for the command palette and `?`
for the list; see [the bindings](docs/commands/web.md#the-keyboard).

![Layer-2 diagram of the home-lab example: a router, a switch, an access point, three computers, a server and a USB-to-Ethernet adapter, annotated with addresses, VLANs and the SSID a phone is associated to](docs/images/home-lab.svg)

<sub>Produced from [`examples/home-lab`](examples/home-lab) with
`netgraph -i examples/home-lab render --layer l2 --title "home-lab — layer 2" -f svg -o docs/images/home-lab.svg`.</sub>

**Nothing to install to look:** every example inventory in this repository is published as
a live diagram at **<https://blechschmidt.github.io/netgraph/demo/>** — switch layers,
filter by VLAN, hover a node for its interfaces and addresses. Those pages are the output
of `netgraph render -f html`, not a demo built to look like it.

> **Status: early development (0.1.0).** The schema, loader, validator, renderers and CLI
> work end to end; the schema may still change before 1.0. See
> [§12 of the specification](docs/schema.md#12-compatibility-policy).

## Why

Network documentation rots because the diagram and the truth live in different places.
The diagram is a drawing: nothing checks it, nothing regenerates it, and the day someone
re-patches a link it starts lying.

netgraph puts the source of truth in reviewable, diffable YAML next to the rest of your
infrastructure code, and generates the picture on demand. Because the files are structured
rather than drawn, they can be *checked*: a cable that names a port which no longer exists
is an error, not a line that happens to end in the wrong place.

Field names and value spaces follow RFC 8343 (`ietf-interfaces`), RFC 8344 (`ietf-ip`) and
the IEEE 802.1Q bridge model, so an inventory stays comparable with what a device actually
reports — see [`docs/yang-mapping.md`](docs/yang-mapping.md).

## Installation

netgraph needs **Python 3.10 or newer**.

<!-- norun: installs from PyPI into the reader's environment -->
```bash
pip install netgraph
pipx install netgraph        # or, to keep it out of your project environments
uv tool install netgraph     # same idea, faster
```

Check what you got — the version, and the Python and Graphviz it found:

<!-- norun: the Graphviz and Python versions are properties of the reader's machine -->
```bash
netgraph --version
netgraph version --json      # the same report, for a bug report
```

From a checkout instead, when you are working on netgraph itself or want an unreleased
change:

```bash
pip install -e .            # from a checkout
pip install -e '.[dev]'     # including the development tooling
```

The `svg`, `png`, `pdf` and `html` formats are produced by running the Graphviz `dot`
binary, which is a system package rather than a Python one:

```bash
sudo apt install graphviz        # Debian / Ubuntu
sudo dnf install graphviz        # Fedora / RHEL
brew install graphviz            # macOS
choco install graphviz           # Windows
```

Check it with `dot -V`. The `dot`, `mermaid` and `json` formats are written by netgraph
itself and need none of that, so if you only want DOT to feed into another tool you can
skip the install.

Linux, macOS and Windows are all tested in CI. On Windows and macOS Graphviz is often
installed without landing on `PATH`; netgraph looks in the usual install locations too,
and `NETGRAPH_DOT` names the binary outright when that is not enough. Full instructions —
including the PowerShell completion script and the `.gitattributes` an inventory kept in
Git on Windows wants — are in
[`docs/getting-started.md`](docs/getting-started.md#on-windows-and-macos).

Or install nothing at all. The published image already has Graphviz in it, and runs the
CLI, the live preview and the browser editor:

<!-- norun: needs a Docker daemon -->
```bash
docker run --rm -v "$PWD:/inventory:ro" ghcr.io/blechschmidt/netgraph:latest validate
```

See [`docs/docker.md`](docs/docker.md), which also covers
[`docker-compose.yml`](docker-compose.yml) for the two servers.

## Quickstart

Five commands. `init` writes a small, valid inventory — devices, cables and a suite of
assertions about them; edit it into your own network.

<!-- norun: writes a directory in the reader's workspace, and stops in an editor -->
```bash
netgraph init my-network && cd my-network
$EDITOR devices/sw-office.yaml          # your switches, routers, hosts
netgraph validate                       # do the documents agree with each other?
netgraph test                           # do they still say what the network is for?
netgraph render -f svg -o network.svg   # draw it
```

The tree it writes looks like this — the folder a document sits in becomes its namespace,
and nothing else about the layout carries meaning:

```text
my-network/
├── netgraph.toml          # optional: default flags for this inventory
├── schema/                # JSON Schema, wired into your editor
├── devices/
│   ├── rtr-gw.yaml        # kind: router
│   ├── sw-office.yaml     # kind: switch
│   └── pc-alice.yaml      # kind: computer
└── cables/
    └── links.yaml         # which port is patched into which port
```

A device is a name, a kind and its interfaces; a cable is two endpoints:

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-office
spec:
  interfaces:
    - name: port1
      type: ethernet
      vlan: { mode: access, vid: 10 }
    - name: port2
      type: ethernet
      vlan: { mode: access, vid: 10 }
```

That inventory is checked in as [`examples/quickstart`](examples/quickstart), so the
following really is what it prints:

<!-- run: cwd=examples/quickstart -->
```console
$ netgraph validate
no problems found
$ netgraph list devices
NAME               KIND      PORTS  ADDRESS           VLANS
-----------------  --------  -----  ----------------  -----
devices/pc-alice   computer      1  192.168.10.20/24  10
devices/rtr-gw     router        2  203.0.113.2/30    10
devices/sw-office  switch        2  -                 10
```

![Layer-1 diagram of the quickstart inventory: a router, a switch and a computer](docs/images/quickstart.svg)

Point a cable at a port that does not exist and `validate` says so, with the file, the
line and a rule id — which is the whole point. The step-by-step version of this, built by
hand and explained line by line, is
[`docs/getting-started.md`](docs/getting-started.md).

**Already have a network?** [`netgraph import`](docs/commands/import.md) builds the first
inventory from output you collect on the devices themselves — LLDP neighbours, `ip -j addr
show`, or the cabling list you already keep — so it starts a diff away from correct
instead of a weekend of typing:

<!-- norun: needs captures collected on live devices, and uses a shell pipeline -->
```bash
lldpctl -f json > collected/"$(hostname -s)".lldp.json    # on each device
netgraph import -o my-network collected/*.json
```

Once the tree exists, [`netgraph drift`](docs/commands/drift.md) reads the same captures
the other way round — the inventory becomes an assertion about the network, and the
command reports where reality disagrees. What a capture cannot see is reported as
*unobserved* rather than as a deletion, so a partial capture never reads as the network
having been dismantled, and `--fail-on drift` makes it a gate for a nightly job.

## The commands

`netgraph [GLOBAL OPTIONS] COMMAND [OPTIONS] [ARGS]`. The inventory is named once, with
the global `-i/--inventory`, and defaults to the current directory. Data goes to stdout
and commentary to stderr, so `netgraph render -f json | jq` and `netgraph validate >
report.txt` both do what they look like they do.

<!-- generated: command-index base=docs/commands/ -->
| Command | What it does | Reference |
|---|---|---|
| [`netgraph init`](docs/commands/init.md) | Scaffold a new inventory, ready to validate and render. | [init.md](docs/commands/init.md) |
| [`netgraph import captures`](docs/commands/import.md) | Build a first inventory from output captured on live devices. | [import.md](docs/commands/import.md) |
| [`netgraph import drawio`](docs/commands/import.md) | Bring an edited draw.io diagram back as a reviewable changeset. | [import.md](docs/commands/import.md) |
| [`netgraph drift`](docs/commands/drift.md) | Compare a live network against the declared inventory. | [drift.md](docs/commands/drift.md) |
| [`netgraph validate`](docs/commands/validate.md) | Check the inventory; the gate for CI and pre-commit. | [validate.md](docs/commands/validate.md) |
| [`netgraph test`](docs/commands/test.md) | Grade the assertions the inventory declares about itself. | [test.md](docs/commands/test.md) |
| [`netgraph fmt`](docs/commands/fmt.md) | Rewrite inventory YAML into the canonical form. | [fmt.md](docs/commands/fmt.md) |
| [`netgraph edit set`](docs/commands/edit.md) | Set a field on an element, in place, comments and all. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit unset`](docs/commands/edit.md) | Remove a field from an element. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit create`](docs/commands/edit.md) | Declare a new element and place its document. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit copy`](docs/commands/edit.md) | Copy an element or a whole namespace, links and all. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit duplicate`](docs/commands/edit.md) | Copy an element into the namespace it is already in. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit delete`](docs/commands/edit.md) | Remove an element, and what cannot survive it. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit rename`](docs/commands/edit.md) | Rename an element and every reference to it. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit move`](docs/commands/edit.md) | Move an element's document to another file. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit connect`](docs/commands/edit.md) | Cable two interfaces together. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit disconnect`](docs/commands/edit.md) | Remove a cable. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit add-interface`](docs/commands/edit.md) | Add an interface to an element. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit remove-interface`](docs/commands/edit.md) | Remove an interface from an element. | [edit.md](docs/commands/edit.md) |
| [`netgraph edit apply`](docs/commands/edit.md) | Apply operations given as JSON; the programmatic face. | [edit.md](docs/commands/edit.md) |
| [`netgraph plan`](docs/commands/plan.md) | Diff two inventory states into a reviewable changeset. | [plan.md](docs/commands/plan.md) |
| [`netgraph diff`](docs/commands/diff.md) | Draw the difference between two inventory states as one diagram. | [diff.md](docs/commands/diff.md) |
| [`netgraph review`](docs/commands/review.md) | Write a change up as one pull-request review: changeset, diagram, new findings. | [review.md](docs/commands/review.md) |
| [`netgraph apply`](docs/commands/apply.md) | Execute a plan against the inventory files. | [apply.md](docs/commands/apply.md) |
| [`netgraph converge plan`](docs/commands/converge.md) | Turn drift into an ordered, per-device remediation plan. | [converge.md](docs/commands/converge.md) |
| [`netgraph log`](docs/commands/log.md) | List the commits that changed the inventory, and what each one changed. | [log.md](docs/commands/log.md) |
| [`netgraph render`](docs/commands/render.md) | Draw the graph as SVG, PNG, PDF, DOT, Mermaid, JSON or HTML. | [render.md](docs/commands/render.md) |
| [`netgraph layout`](docs/commands/layout.md) | Store the diagram's arrangement, so a hand-placed node stays put. | [layout.md](docs/commands/layout.md) |
| [`netgraph watch`](docs/commands/watch.md) | Re-render on every save, optionally serving the result. | [watch.md](docs/commands/watch.md) |
| [`netgraph web`](docs/commands/web.md) | Edit the YAML and see the diagram side by side in a browser. | [web.md](docs/commands/web.md) |
| [`netgraph lsp`](docs/commands/lsp.md) | Serve completion, diagnostics and rename to an editor over LSP. | [lsp.md](docs/commands/lsp.md) |
| [`netgraph path`](docs/commands/path.md) | Trace how two elements reach each other, hop by hop. | [path.md](docs/commands/path.md) |
| [`netgraph impact`](docs/commands/impact.md) | Simulate a failure: blast radius, single points of failure, promises. | [impact.md](docs/commands/impact.md) |
| [`netgraph list`](docs/commands/list.md) | Tabulate devices, cables, tunnels, VLANs, BSSs or subnets. | [list.md](docs/commands/list.md) |
| [`netgraph query`](docs/commands/query.md) | Select elements with one selector language, and print what matched. | [query.md](docs/commands/query.md) |
| [`netgraph ipam`](docs/commands/ipam.md) | Report utilisation, free space, overlaps and aggregates. | [ipam.md](docs/commands/ipam.md) |
| [`netgraph export`](docs/commands/export.md) | Emit hosts files, DNS zones, Ansible, Prometheus, cable lists. | [export.md](docs/commands/export.md) |
| [`netgraph report`](docs/commands/report.md) | Write the as-built documentation: a page per site and per device. | [report.md](docs/commands/report.md) |
| [`netgraph show`](docs/commands/show.md) | Print one element as it was resolved, expansions included. | [show.md](docs/commands/show.md) |
| [`netgraph rules`](docs/commands/rules.md) | List the validation rules and their ids. | [rules.md](docs/commands/rules.md) |
| [`netgraph schema`](docs/commands/schema.md) | Write the JSON Schema for editor completion. | [schema.md](docs/commands/schema.md) |
| [`netgraph config show`](docs/commands/config.md) | Show the resolved settings and where each value came from. | [config.md](docs/commands/config.md) |
| [`netgraph cache info`](docs/commands/cache.md) | Report where the parse cache is and what is in it. | [cache.md](docs/commands/cache.md) |
| [`netgraph cache clear`](docs/commands/cache.md) | Delete this inventory's cached documents. | [cache.md](docs/commands/cache.md) |
| [`netgraph completion`](docs/commands/completion.md) | Print the shell completion script. | [completion.md](docs/commands/completion.md) |
| [`netgraph version`](docs/commands/version.md) | Report the netgraph, Python and Graphviz versions in use. | [version.md](docs/commands/version.md) |
<!-- /generated -->

Every flag of every command, the global options and the exit codes:
[`docs/commands/`](docs/commands/README.md). Those tables are generated from the CLI
itself and checked by the test suite, so they cannot drift from the code.

## One inventory, several questions

The files describe the network once; each command asks something different of them.

| You want to know | Ask |
|---|---|
| does the documentation contradict itself? | [`netgraph validate`](docs/validation.md) |
| does the network still do what we built it to do? | [`netgraph test`](docs/commands/test.md) |
| what does the physical topology look like? what does VLAN 10 reach? which subnets exist? | [`netgraph render --layer l1\|l2\|l3`](docs/rendering.md) |
| what runs inside which tunnel? | [`netgraph render --layer overlay`](docs/rendering.md#overlay-tunnels-and-what-runs-inside-what) |
| which SSID is on which channel, in which VLAN? | [`netgraph list bss`](docs/commands/list.md) |
| what is in rack 3, and at which units? | [`netgraph render --layer rack`](docs/rendering.md#rack-a-front-elevation-per-cabinet) |
| how does this host reach that one, hop by hop? | [`netgraph path`](docs/paths.md) |
| how do I move a switch on the diagram and have it stay there? | [`netgraph layout`](docs/commands/layout.md) |
| how full is that /24, and where is the next free /28? | [`netgraph ipam`](docs/ipam.md) |
| what should `/etc/hosts`, the DNS zone, the Ansible inventory, the pull list or the routing script say? | [`netgraph export`](docs/export.md) |
| what should this device's netplan, systemd-networkd, ifupdown, FRR, nftables or WireGuard configuration say? | [`netgraph export netplan`](docs/export.md#device-configuration-the-seven-dialects) |
| can I hand the diagram to somebody who only has draw.io, and take their edits back? | [`netgraph export drawio`](docs/drawio.md) |
| what do I hand over as the as-built documentation? | [`netgraph report`](docs/commands/report.md), and [an example of what it writes](docs/example-report/) |
| what did that template and that interface range actually expand to? | [`netgraph show`](docs/commands/show.md) |

Two of them are worth seeing. Nothing in the YAML declares a subnet: the layer-3 view and
`list subnets` both derive the prefixes from the addresses the interfaces carry, which is
why they cannot disagree with the configuration,

<!-- run: -->
```console
$ netgraph -i examples/home-lab list subnets
SUBNET            IP  ADDRESSES  ELEMENTS  VLANS
----------------  --  ---------  --------  -----
192.0.2.1/32       4          1         1  -
192.168.10.0/24    4          7         7  10
203.0.113.0/30     4          1         1  -
2001:db8::1/128    6          1         1  -
2001:db8:10::/64   6          5         5  10
```

and `netgraph path` traces the route two hosts in different sites actually take, naming
the ingress and egress port at every hop:

<!-- run: -->
```console
$ netgraph -i examples/campus path pc-north-01 pc-south-01
sites/north/hosts/pc-north-01 -> sites/south/hosts/pc-south-01: 2 paths
  source       sites/north/hosts/pc-north-01  [computer]
  destination  sites/south/hosts/pc-south-01  [computer]
  layer        3, routed (ipv4)
  showing      the shortest; pass --all for the rest
  note         no layer-2 path: the two elements are in no common broadcast domain, so the trace looked for a routed one
...
```

## Where to go next

[`docs/README.md`](docs/README.md) is the index, with an "if you want to X, read Y" table
over the whole set. The pages a new reader wants first:

| Page | What it answers |
|---|---|
| [`docs/getting-started.md`](docs/getting-started.md) | Install it, build a three-device inventory by hand, validate, render, interrogate. Ends with the editor wiring. |
| [`docs/inventory-layout.md`](docs/inventory-layout.md) | How to organise the files: namespaces, references, templates, interface ranges, and a layout for a multi-site estate. |
| [`docs/rendering.md`](docs/rendering.md) | The nine layers, the filters, namespace collapsing, link bundling, icon themes, stored arrangements, and what each output format is for. |
| [`docs/validation.md`](docs/validation.md) | What the checks are, what a finding means, and the four ways to say "not here". |
| [`docs/schema-reference.md`](docs/schema-reference.md) | Every field of every kind, with its type, default and YANG counterpart. |
| [`docs/schema.md`](docs/schema.md) | The normative specification, if you want the reasoning as well as the rules. |
| [`docs/ci.md`](docs/ci.md) | Making a pull request fail when the inventory stops adding up. |
| [`docs/configuration.md`](docs/configuration.md) | `netgraph.toml`, so you type the flags once instead of every time. |
| [`docs/lsp.md`](docs/lsp.md) | `netgraph lsp` in VS Code, Neovim, Helix and Emacs: diagnostics, completion, hover, rename and quick fixes on the YAML you are typing. |
| [`docs/docker.md`](docs/docker.md) | The image and the compose file: the CLI, the preview and the editor in a container. |

## Examples

Five complete, self-consistent inventories live under [`examples/`](examples/README.md) —
from the three-device quickstart to a 22-device campus with nested namespaces, a
five-tunnel overlay with VXLAN inside IPsec, and a patch room with two racks of structured
cabling. All of them validate clean with no suppressions, and the test suite renders them
on every commit, so they stay executable rather than aspirational.

<!-- run: -->
```console
$ netgraph -i examples/patch-room validate
no problems found
```

## As a library

Everything the CLI does is available from Python: `load_tree` reads a folder into an
`Inventory`, `validate` returns findings, `build_graph` resolves the topology, and the
renderers are pure functions of the graph. See
[Using it as a library](docs/architecture.md#using-it-as-a-library).

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md) has the dev setup and the gates (ruff, ruff format,
mypy, pytest with a coverage floor), plus recipes for adding a validation rule or a
renderer. [`docs/architecture.md`](docs/architecture.md) is the ten-minute orientation:
the pipeline is `load_tree` → `validate` → `build_graph` → `filter`/`aggregate` →
renderers, and each stage's invariants are written down.

## License

[MIT](LICENSE)
