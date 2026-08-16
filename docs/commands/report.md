# `netviz report`

`netviz report` writes the document a network engineer is actually asked to hand
over: an as-built record with a page per site and a page per device, carrying the
diagrams, the address plan, the VLAN matrix, the cable schedule, the rack
positions, the wireless plan and the open validation findings. Every table comes
from the same derivation the matching command prints — `netviz list`,
`netviz ipam`, `netviz export cable-list` — so no two pages of one report can
disagree with each other or with the diagram above them.

---

## Contents

- [Synopsis](#synopsis)
- [What a bundle looks like](#what-a-bundle-looks-like)
- [The three formats](#the-three-formats)
- [What each page carries](#what-each-page-carries)
- [A machine that runs more than one network stack](#a-machine-that-runs-more-than-one-network-stack)
- [Sites, and how a namespace becomes one](#sites-and-how-a-namespace-becomes-one)
- [Scoping a report](#scoping-a-report)
- [Traceability: the stamp, the version and the revision](#traceability-the-stamp-the-version-and-the-revision)
- [Deterministic output, and committing a report](#deterministic-output-and-committing-a-report)
- [Editing the layout with `--template`](#editing-the-layout-with---template)
- [Validation runs first](#validation-runs-first)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis report -->
```text
netviz [GLOBAL OPTIONS] report [OPTIONS]
```
<!-- /generated -->

## What a bundle looks like

`--out DIR` names a directory; the layout inside it is fixed:

```text
DIR/
├── README.md                        the overview (index.html for -f html)
├── sites/
│   └── sites_north.md               one page per site
├── devices/
│   └── sites_north_core_rtr-north-core-01.md   one page per element
└── diagrams/
    └── sites_north-l1.svg           one drawing per page per layer
```

A file name is the element's fully-qualified name reduced to a slug, lower-cased,
with a numeric suffix when two names would collide — always assigned in sorted
order, so a page never moves because a directory was walked in a different order.
A drawing is named after the page that holds it, for the same reason.

`-f html` writes `index.html` and `.html` pages instead, and embeds each drawing
in the page that references it rather than writing `diagrams/`.

## The three formats

| `-f` | What it is for |
|---|---|
| `markdown` | The default. Committed next to the inventory and reviewed as a diff; renders on any forge. |
| `html` | One self-contained site: the style sheet is inlined into every page, nothing is fetched, and **every device in every diagram is a link to that device's page**. Print styles included, because an as-built record gets printed. |
| `json` | The whole document — pages, sections, tables, cells and their cross-references — in one file, for downstream tooling. Goes to stdout when `--out` is omitted. |

<!-- run: -->
```console
$ netviz -q -i examples/home-lab report -f json --generated-at none --revision ''
{
  "meta": {
    "title": "home-lab — as-built network documentation",
    "inventory": "home-lab",
    "netviz": "0.0.1",
    "generatedAt": null,
    "revision": null,
    "revisionState": null,
    "scope": "the whole inventory",
    "format": "json",
...
```

## What each page carries

**The overview** — the provenance block, one row per site, the whole-inventory
diagrams, every open validation finding, every element, the address plan, the VLAN
tables, and the tunnels and PDUs when the inventory has any.

**A site page** — the layer diagrams for that site; its elements; the address plan
with utilisation from [`netviz ipam`](ipam.md); a VLAN summary and a
VLAN-to-subnet-to-element matrix; the cable schedule from the same rows
[`netviz export cable-list`](export.md) writes, one row per run with the patch
panels named; a port map per patch panel, free positions included; the BSS and
SSID plan; the PDU load schedule; the links that leave the page; and the findings
anchored to its elements.

**A device page** — identity and metadata; placement (site, room, rack, unit,
height) and power feeds; interfaces with their addresses, VLANs, MTU, VRF, LAG or
bridge membership and radios; the network namespaces it runs, when it runs any;
every cable and tunnel that terminates on it, with the far end linked and the
panels a run crosses; its VRFs, static routes and BGP or OSPF adjacencies; and the
diagrams it appears in.

Nothing is silently absent: a table with no rows still appears, saying that the
inventory declares none of whatever it is about.

## A machine that runs more than one network stack

A device page describes one machine, and since [§23](../schema.md#23-network-namespaces-and-veth-pairs)
a machine may run several network stacks. Two things on the page say so, and both
are **conditional** — a report of an inventory that declares no `spec.netns` is
byte-for-byte what it was before the feature existed:

* a **`NETNS` column** in the interface table, added only when at least one
  interface on *that page* is in a stack other than the machine's initial one. It
  sits beside `VRF` because the two compose: a namespace is a whole second stack,
  a VRF partitions the routing table of one stack, and `netns: blue` with
  `vrf: red` names the `red` instance of the `blue` one;
* a **Network namespaces section**, drawn only on a device that declares
  `spec.netns` or a veth pair.

The section holds the namespace tree — the initial namespace first, every declared
one indented under the namespace it was created from, with the interfaces homed in
each and the addresses they carry — then the veth pairs, **one row per end**, so a
pair is named from both sides of the boundary it crosses. Where a declared
namespace holds static routes or policy rules of its own, they follow in a table
each; a route is placed by the interface it leaves by, which is what `dev` says.

<!-- norun: an excerpt of a page the command writes into --out -->
```markdown
| NAMESPACE      | PARENT    | INTERFACES         | ADDRESSES                    |
|:---------------|:----------|:-------------------|:-----------------------------|
| (initial)      | —         | eno1, br-tenants   | 10.20.0.11/24, 10.30.0.1/24  |
| └─ blue        | (initial) | veth-blue, veth-…  | 10.30.0.11/24, 10.31.0.1/30  |
| └─ └─ blue-web | blue      | veth-web           | 10.31.0.2/30                 |
| └─ green       | (initial) | veth-green         | 10.30.0.12/24                |
```

The section and the routing section link to each other and neither restates the
other's half: a VRF is described once, in *Routing*, and the namespace section
only says which stack holds it.

`examples/containers` is the worked example — two hosts that run namespaces and
one lab switch that does not, so both branches are in the same report. The `json`
format is the one that fits in a transcript; the section is a section like any
other, keyed `netns`:

<!-- run: -->
```console
$ netviz -q -i examples/containers report -f json --generated-at none --revision ''
...
          "key": "netns",
          "title": "Network namespaces",
          "blurb": "The network stacks this machine runs, and the veth pairs that join them.",
...
              "rows": [
                [
                  "(initial)",
                  "—",
                  "eno1, br-tenants, veth-blue-h, veth-green-h",
                  "10.20.0.11/24, 10.30.0.1/24",
                  "The machine itself; the stack every other section on this page is about."
                ],
                [
                  "└─ blue",
                  "(initial)",
                  "veth-blue, veth-web-h",
                  "10.30.0.11/24, 10.31.0.1/30",
                  "Tenant blue."
                ],
...
```

## Sites, and how a namespace becomes one

A "site" is a namespace. Which namespace depends on the tree, and by default
`netviz report` counts one level below the namespace every element shares — the
same definition `--collapse-depth` uses. A campus laid out as
`sites/<site>/<tier>` therefore gets a page per site rather than one per tier,
while a home lab whose directories are `routers/`, `switches/` and `hosts/` gets a
single site page, because splitting *that* tree would put every cable in it on
none of the pages.

`--group-depth N` overrides the count. `--group-depth 0` puts the whole selection
on one site page; `--group-depth 2` would give the campus a page per tier.

A cable or a tunnel is documented on a site page when **everything it joins** is
on that page. The ones that cross a boundary are listed under *Links leaving this
page*, with the site they are documented on — so a per-site report never quietly
loses the uplinks.

## Scoping a report

`--namespace`, `--kind`, `--name`, `--vlan` and `--neighbors-of`/`--depth` are the
filters [`netviz render`](render.md) takes, and they mean the same thing here.
They select *elements*; the pages, tables and diagrams are then built from what
survived, and the scope is printed in the provenance block of every page.

<!-- norun: writes a directory into the reader's tree -->
```console
$ netviz -i examples/campus report --namespace sites/north --out docs/north
wrote 18 files (389.7 kB) to docs/north
```

## Traceability: the stamp, the version and the revision

Every page carries the netviz version, the generated-at stamp and the
inventory's git revision when there is one — `abc123456789 (clean)`, or
`(modified)` when tracked files under the inventory root differ from the commit,
because then the report does *not* describe the commit it names.

* `--generated-at WHEN` pins the stamp to an ISO-8601 timestamp, or to `none` to
  leave it out. Without it, `SOURCE_DATE_EPOCH` is honoured, and the current UTC
  time is used when that is unset too.
* `--revision REV` records `REV` instead of asking git — for a pipeline that knows
  the commit better than the work tree does. `--revision ''` says nothing.

## Deterministic output, and committing a report

Two runs over one inventory produce byte-identical files: page names are slugs
with collisions resolved in sorted order, every table is sorted, the JSON is
dumped in a fixed key order, and files are written with `\n` endings on every
platform. The generated-at stamp is the one variable, which is why it can be
pinned:

<!-- norun: needs a writable directory and two runs to compare -->
```console
$ netviz -i examples/home-lab report --out docs/as-built --generated-at none
$ git diff --exit-code docs/as-built   # nothing changed, or the inventory did
```

`--out` is never emptied behind your back. Files under it that this report did not
write — the pages of a device that has since been deleted — are reported, and
removed only when you pass `--prune`.

## Editing the layout with `--template`

The pages are Jinja2 templates in `netviz/report/templates`: `overview`, `site`
and `device`, in a `.md.j2` and a `.html.j2` variant each, over the shared macros
in `macros.md.j2` and `macros.html.j2`. `--template DIR` puts a directory in front
of them, one file at a time: a `DIR` holding only `device.md.j2` overrides the
device page and leaves everything else as it was.

A template receives `page`, `report`, `meta`, `link` (which resolves a
cross-reference relative to the page being written) and, for HTML, `stylesheet`
and `csp`. It should not derive facts — every value it prints is already on the
model, which is what keeps a custom layout from disagreeing with the standard one.

## Validation runs first

A report presents an inventory as authoritative, so the same gate
[`netviz export`](export.md) applies is applied here: errors refuse the run
unless `--force` is given, and `--strict` promotes warnings to errors. Findings
that do *not* stop the run are still written into the report — on the overview,
and on the site page of every element they name.

## Options

<!-- generated: options report -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-f`, `--format` | `[markdown\|html\|json]` | `markdown` | markdown is committed next to the inventory and diffed; html is one self-contained site, where a device in a diagram links to its page; json is the whole document in one file, for downstream tooling. |
| `-o`, `--out` | `DIRECTORY` | — | Directory to write the bundle into; created if absent. Required for markdown and html, which write several files. json writes one document, and goes to stdout when no directory is named. |
| `--template` | `DIR` | — | Take page templates from this directory before the bundled ones. A directory holding only 'device.md.j2' overrides the device page and nothing else. |
| `--layer` | `[physical\|l1\|l2\|l3\|overlay\|routing\|rack\|power\|identity\|netns\|security]` | every layer the inventory declares something for | Draw this layer on every page, instead of the ones the inventory has earned. Repeatable, and honoured verbatim: a layer with nothing in it is reported as empty rather than dropped. |
| `--title` | `TEXT` | — | Title for the overview page. |
| `--group-depth` | `N` | 1 when the namespace tree branches below the site level, else 0 | How many namespace levels below the shared prefix one site page covers. 0 puts the whole selection on a single site page. |
| `--diagrams`, `--no-diagrams` | — | `--diagrams` | Draw the layer diagrams. Off writes the tables alone, which is faster and needs no Graphviz; each figure then says so rather than going missing. |
| `--generated-at` | `WHEN` | $SOURCE_DATE_EPOCH if set, otherwise the current time | Pin the generated-at stamp to this ISO-8601 timestamp, or to 'none' to leave it out. The stamp is the only part of a report that is not a function of the inventory, so pinning it is what makes two runs byte-identical. |
| `--revision` | `REV` | the inventory's git commit, when it is in a work tree | Record this as the inventory's revision instead of asking git for it. |
| `--prune` | — | off | Delete the .md, .html, .svg and .json files in --out that this report does not write — the pages of an element that has since been deleted. They are reported either way. |
| `--namespace` | `NS` | — | Keep only elements in this namespace or below it. Repeatable. |
| `--vlan` | `VID` | — | Keep only elements participating in this VLAN. Repeatable. |
| `--kind` | `[switch\|router\|firewall\|hub\|computer\|server\|adapter\|patchpanel\|pdu\|user\|group]` | — | Keep only elements of this kind. Repeatable. |
| `--name` | `GLOB` | — | Keep only elements whose name matches this glob. Repeatable. |
| `--neighbors-of` | `NAME` | — | Keep only the neighbourhood of this element. |
| `--depth` | `INTEGER, >= 0` | `1` | How many hops --neighbors-of reaches. |
| `--select` | `QUERY` | — | Keep only the elements this query selects, e.g. "kind = switch and not has vrf". The flags above are sugar for the equivalent query and are combined with it; 'netviz query --explain' prints which. See docs/query.md. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Proceed even when validation failed. The result may not match the files. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The report was written. Stale files reported under `--out` are not a failure. |
| `1` | The inventory was rejected — validation found errors, or `--strict` promoted a warning — and `--force` was not given. Nothing is written. |
| `2` | Usage error — an unknown format, or `markdown`/`html` without `--out`. |
| `3` | The inventory could not be discovered or read at all. |
| `5` | The bundle could not be written, or a `--template` failed to render. |
| `130` | Interrupted. |

## See also

* [`netviz list`](list.md), [`netviz ipam`](ipam.md) and
  [`netviz export`](export.md) — the commands whose tables the report reuses.
* [`netviz render`](render.md) — the diagrams, and the filters `report` shares.
* [`docs/example-report/`](../example-report/) — the committed report of
  `examples/patch-room`, browsable as it would be handed over.
* [`docs/architecture.md`](../architecture.md) — where the report generator sits
  in the pipeline.
