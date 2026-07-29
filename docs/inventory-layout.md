# Laying out an inventory

An inventory is a directory tree of YAML documents, and netgraph imposes almost
nothing on its shape. This page is for the point just after the tutorial: you
know what a document looks like, and now you have to decide how many files there
are, what goes in each one, and which folder each one sits in — before the answer
is decided for you by forty documents nobody wants to move.

The normative rules live in the specification;
[`docs/schema.md` §2](schema.md#2-inventory-layout-and-loading) is the section
this page paraphrases, and it is the one to read when two statements disagree.

---

## Contents

- [One document per file, or several](#one-document-per-file-or-several)
- [Folders are namespaces](#folders-are-namespaces)
- [How a reference is resolved](#how-a-reference-is-resolved)
- [Which files the loader reads](#which-files-the-loader-reads)
- [The ten kinds](#the-ten-kinds)
- [A layout for a small network](#a-layout-for-a-small-network)
- [A layout for an estate](#a-layout-for-an-estate)
- [Declaring a 48-port switch without typing it 48 times](#declaring-a-48-port-switch-without-typing-it-48-times)
- [Annotations](#annotations)
- [Keeping the tree tidy](#keeping-the-tree-tidy)

---

## One document per file, or several

Both are accepted. A file MAY hold one document, or several separated by `---`
(`NG-L004`), and the loader treats the two the same way: a document is a document
wherever it was found, and every diagnostic quotes the file *and* the document
index within it, `sites/hq/switches/sw-access-01.yaml#0:17`.

So the choice is a review choice, not a technical one.

* **One document per file** is what you want for anything somebody will edit on
  its own. A 48-port switch is a hundred lines; giving it a file named after it
  means a diff touching that switch touches one file, and `git log` on the file
  is the history of the switch.
* **Several documents in one file** is what you want for things that are only
  ever true together. Cables are the obvious case: a cable is meaningless
  without the two devices it joins, and twenty of them in `cables/links.yaml`
  read as a patch record. The same goes for a handful of near-identical hosts.

The examples in this repository use both, in exactly that split — `examples/campus`
gives every core and distribution device its own file and collects each site's
cables and hosts into one apiece.

Empty documents are skipped silently but still consume an index, which is why
[`netgraph fmt`](commands/fmt.md) writes an empty document as an explicit `null`
rather than deleting it: dropping it would renumber every document after it and
move the line every diagnostic points at.

## Folders are namespaces

An element's **fully-qualified name** is the directory holding its document,
relative to the inventory root, plus its `metadata.name`. A `switch` named `sw1`
in `sites/berlin/rack1/sw1.yaml` is `sites/berlin/rack1/sw1`; one declared at the
root is just `sw1`. `metadata.name` has to be unique only within its own
namespace (`NG-N002`), so two racks may each hold a `sw1`.

Folders are otherwise for humans. Group by site, by rack, by tenant, by whatever
the team already says out loud — cross-references work across any file in the
tree, and the only thing a folder contributes is the namespace. Nothing about a
folder changes how a device is validated or drawn.

The qualified names are what `netgraph list` prints, and they are the names you
give to any command that takes one:

<!-- run: -->
```console
$ netgraph -i examples/campus list devices
NAME                                       KIND      PORTS  ADDRESS        VLANS
-----------------------------------------  --------  -----  -------------  -------------
sites/north/access/sw-north-acc-01         switch        6  10.1.99.11/24  1,10,20,30,99
...
sites/west/hosts/pc-west-02                computer      2  10.3.10.52/24  10
```

## How a reference is resolved

Cables, adapters and tunnels point at interfaces with a two-part reference,
`sw-access-01:GigabitEthernet1/0/1` — the device part, a colon, the interface
name ([§4.2](schema.md#42-interface-references)). The device part is a plain
name, and plain names are resolved **outwards**:

1. the namespace of the referring document,
2. each ancestor namespace, nearest first, the root last,
3. the inventory as a whole — but only when exactly one element carries that
   name; otherwise the reference is ambiguous and every candidate is named in the
   diagnostic (`NG-N002`).

A reference MAY also be written fully qualified, `sites/berlin/rack1/sw1`, which
is tried relative to the current namespace first and as an absolute name second.
[§2.2](schema.md#22-namespaces-and-name-resolution) states the rule normatively.

Two consequences are worth designing around. An element at the root is visible
from everywhere, which makes the root the right place for the handful of things
every site refers to. And a name that is unique across the whole inventory never
needs qualifying at all, however deep it sits — which is why the campus example
prefixes device names with their site: `sw-north-acc-01` is reachable from
`sites/north/cables/` because steps 1 and 2 miss and step 3 finds exactly one
match.

## Which files the loader reads

The walk is recursive from the inventory root and the rules are few
([§2.1](schema.md#21-discovery-rules)):

* only `*.yaml` and `*.yml`, compared case-insensitively; every other file is
  ignored (`NG-L001`). A `README.md` or a `netgraph.toml` beside your documents
  is not a problem.
* nothing under a path component whose basename starts with `.` or `_`,
  directories included (`NG-L002`). `_drafts/` and `_scratch/` are the idiom for
  work in progress, and `.git/` costs nothing to skip.
* nothing a `.netgraphignore` excludes (`NG-L006`).
* symbolic links are followed, but one that escapes the root, forms a cycle or
  reaches an already-loaded directory is an error (`NG-L003`).

Load order is deterministic — files sorted by their byte-wise POSIX path relative
to the root, then by document index — so renderers produce stable output
(`NG-L005`).

Loading is *total*: an unreadable file, a YAML syntax error, a schema violation
and a duplicate name are all reported with their location and the walk
continues. One broken file cannot hide the rest of the inventory.

### `.netgraphignore`

Optional, one per directory, applying to that directory and everything below it;
a file in a subdirectory overrides its parents. The syntax is the `.gitignore`
subset described in [§2.3](schema.md#23-netgraphignore):

```text
vendor/                 # a directory, anywhere below this file
*.bak.yaml              # a basename pattern, at any depth
/staging.yaml           # only in this directory
generated/**            # everything below generated/
!generated/keep.yaml    # ... except this one (the parent is not excluded)
```

Reach for it when the tree is shared with something else — a Kubernetes overlay,
a vendor export, an Ansible role — and the `_`-prefix trick would mean renaming
directories that another tool owns.

Discovery is one implementation, used by every command, so a file the inventory
would not read is also a file `fmt` will not rewrite and `validate` will not
complain about.

## The ten kinds

Every document declares a `kind`, and the `kind` decides the shape of its `spec`.
Nine kinds are **elements** — each becomes a node or an edge of the graph. The
tenth, `template`, is not.

| `kind` | What it is for | Specification |
|---|---|---|
| `switch` | A VLAN-aware bridge. Layer-2 by default: it does not forward IP unless told to. | [§6](schema.md#6-device-kinds) |
| `router` | A device that forwards IP; `forwarding` is true for both families by default. | [§6](schema.md#6-device-kinds) |
| `hub` | A layer-1 repeater. Takes no VLANs, no addresses and no bridge — one collision domain. | [§6.5](schema.md#65-per-kind-constraints) |
| `computer` | An end host, drawn as a workstation. | [§6](schema.md#6-device-kinds) |
| `server` | An end host, drawn as a rack-mount server. Structurally identical to `computer`. | [§6](schema.md#6-device-kinds) |
| `cable` | One undirected physical link between exactly two interfaces. Owns no interfaces of its own. | [§7](schema.md#7-cables) |
| `adapter` | Interfaces presented over a non-network host port — a USB dock, a Thunderbolt bridge. | [§8](schema.md#8-adapters) |
| `tunnel` | An undirected logical link between two or more `tunnel` interfaces; `over` nests one inside another. | [§14](schema.md#14-tunnels) |
| `patchpanel` | A passive cross-connect. Its front and rear ports are derived from `ports`, and it is not a hop. | [§15](schema.md#15-patch-panels) |
| `template` | A named partial device `spec`, merged into every device that names it in `spec.from`. Not an element. | [§6.6](schema.md#66-template--reusable-partial-device-specs) |

[`docs/schema-reference.md`](schema-reference.md#element-kinds) is the generated
field-by-field table for each of them, and
[`netgraph schema`](commands/schema.md) emits the JSON Schema an editor can
check a document against as you type.

There is no rule that one folder holds one kind, and no rule that it does not.
Grouping cables into `cables/` is a habit worth having because it puts the patch
record in one place; grouping every switch in the estate into `switches/` is
usually a mistake, because it puts two sites in one namespace and buys nothing.

## A layout for a small network

For a house, a lab bench or a single office, one level is enough. Name the
folders after what the things are, and let every name be globally unique — with
a dozen devices, nothing will ever be ambiguous:

```text
home-lab/
├── routers/rtr-home.yaml
├── switches/sw-home.yaml
├── hosts/
│   ├── pc-desk.yaml
│   ├── laptop.yaml
│   ├── srv-nas.yaml
│   └── adp-usb-eth.yaml            # an adapter is a document like any other
└── cables/links.yaml               # every cable, one file
```

That is [`examples/home-lab`](../examples/home-lab/) exactly. The qualified name
of the switch is `switches/sw-home`, and no cable in `cables/links.yaml` ever
writes that prefix: the inventory-wide lookup finds the one `sw-home`.

If even that is more structure than you want, put every document in one file at
the root and split it later. Nothing in a document mentions its own path, so
moving a file only ever changes qualified names — and only the references that
were *already* qualified need touching.

## A layout for an estate

Once there is more than one site, make the site the top-level namespace and repeat
one shape inside it. Two things follow: a reference written inside a site resolves
inside that site first, and a rendering can be narrowed to one site with
`--namespace sites/north` ([`docs/rendering.md`](rendering.md)).

[`examples/campus`](../examples/campus/) is three sites, 22 devices and 22 cables
in that shape:

```text
campus/
├── netgraph.toml                       # per-inventory configuration
├── backbone/cables.yaml                # the three inter-site fibres
├── templates/access-switch.yaml        # a 48-port access switch, declared once
└── sites/
    ├── north/
    │   ├── core/rtr-north-core-01.yaml
    │   ├── distribution/sw-north-dist-01.yaml
    │   ├── access/switches.yaml        # three documents; the third uses the template
    │   ├── hosts/hosts.yaml            # three documents
    │   └── cables/links.yaml           # seven documents
    ├── south/                          # same shape, two access switches
    └── west/                           # same shape, two access switches
```

Read it as a set of decisions:

* **The site is the namespace, and the tier is the folder below it.** `core`,
  `distribution`, `access`, `hosts`, `cables` — five folders that mean the same
  thing in every site, which is what makes `sites/north` and `sites/south`
  diffable against each other. Anything that differs beyond the site index is a
  mistake, and that is a review technique the layout gives you for free.
* **Cables live in the site whose devices they join**, and the three that join
  two sites live in `backbone/` at the root, because they belong to neither.
* **Shared things live at the root**, where every site can see them:
  `templates/` here, and a `netgraph.toml` for the settings the whole inventory
  should share ([`docs/configuration.md`](configuration.md)).
* **Room is a namespace too**, when a site is big enough to have them:
  `sites/hq/mdf/`, `sites/hq/idf-3/`. That is a different question from where the
  hardware physically *is*, which is `metadata.location`
  ([§3.2](schema.md#32-metadatalocation)) and drives the rack elevation, not the
  name.

Per-file device documents in the tiers you edit rarely and carefully, multi-document
files for hosts and cables you edit in batches: that is the split described
[above](#one-document-per-file-or-several), applied.

## Declaring a 48-port switch without typing it 48 times

A real access layer is dozens of near-identical documents, each of which is
dozens of near-identical ports. Two mechanisms remove the repetition without
weakening any check: both are applied by the loader before validation, so the
validator, the graph and every renderer still see plain interfaces and plain
devices.

### Interface ranges

One entry may declare `range` instead of `name`
([§6.2.5](schema.md#625-range--declaring-many-interfaces-at-once)):

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

### Device templates

A `kind: template` document is a named partial device `spec`; a device merges it
in with `spec.from` ([§6.6](schema.md#66-template--reusable-partial-device-specs)):

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
[`examples/campus`](../examples/campus/sites/north/access/switches.yaml) has that
switch next to two written out longhand so the two can be compared. The merge
rules are stated exactly in [§6.6.1](schema.md#661-merge-rules) and are worth
knowing in full, but in short: the device's own keys win, mappings merge key by
key, `interfaces` merge by `name`, and every other list the device declares
replaces the template's outright.

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

Use [`netgraph show NAME --raw`](commands/show.md) to read a device as written
and `netgraph show NAME` to read it merged:

<!-- run: -->
```console
$ netgraph -i examples/campus show sw-north-acc-03 --raw
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-north-acc-03
  description: Access switch 03, North campus - third floor riser.
  labels:
    site: north
    role: access
    env: prod
spec:
  location: Building A, Hauptstrasse 1 / floor 3 / IDF-3
  interfaces:
  - name: Vlan99
    ipv4:
      addresses:
      - 10.1.99.13/24
  - name: TenGigabitEthernet1/1/1
    mac: 00:1b:0d:01:a3:11
  from: templates/c9200l-48p
  bridge:
    address: 00:1b:0d:01:a3:ff
```

Without `--raw` the same command prints 51 interfaces.

## Annotations

`metadata.labels` are yours and drive filtering and grouping —
`netgraph render --select site=hq`, `--group-by rack` — so a small consistent key
set (`site`, `rack`, `role`, `env`, `owner`) pays off. `metadata.annotations` are
the opposite: they are read by the tool, not by you, and never affect the graph.

The one annotation this revision defines is `netgraph/ignore`, which suppresses
validation rules on the element carrying it:

```yaml
metadata:
  name: spare-switch
  annotations:
    netgraph/ignore: "W103, E004"   # or "*" for every rule
```

Ids may be separated by commas, semicolons or spaces, and `*` means every rule.
It is documented as a field in [§3.1](schema.md#31-metadata), and as one of the
three ways to silence a rule in
[`docs/validation-rules.md`](validation-rules.md#3-per-element-with-an-annotation)
— which is also where you will find the argument for annotating the element the
exception genuinely belongs to rather than the nearest one.

## Keeping the tree tidy

Three habits stop a growing tree from becoming a review problem.

**Format it.** [`netgraph fmt`](commands/fmt.md) rewrites every document in one
canonical form — two-space indent, keys in schema order, one quoting rule,
comments and blank lines untouched — so a diff is never about layout. It uses the
loader's discovery, so it rewrites exactly the files the inventory reads and
nothing else.

<!-- run: -->
```console
$ netgraph fmt --check examples/campus
0 file(s) would be reformatted, 17 already formatted
```

**Let your editor check it.** `netgraph schema` writes a JSON Schema, and a
one-line modeline at the top of a file gives you completion and inline errors
before you ever run the tool. See
[Editor setup](getting-started.md#editor-setup-autocompletion-and-inline-errors)
and [`docs/schema.md` §13](schema.md#13-editor-integration).

**Validate it in CI.** [`netgraph validate`](commands/validate.md) is the check
that a cable's far end exists, that two devices do not claim one address, that
both ends of a link agree about VLANs — the things no per-file schema can see.
[`docs/ci.md`](ci.md) has the workflow and the pre-commit hooks; the `--check`
form of `fmt` belongs in the same job.

## See also

* [`docs/schema.md` §2](schema.md#2-inventory-layout-and-loading) — the normative
  discovery, namespace and provenance rules this page paraphrases.
* [`docs/getting-started.md`](getting-started.md) — the eight-step tutorial that
  builds the first tree.
* [`netgraph show`](commands/show.md) — read one element as written, or as
  netgraph resolved it.
* [`docs/importing.md`](importing.md) — generating a first tree from LLDP, an
  `ip` dump, a CSV or a packet capture rather than typing one.
