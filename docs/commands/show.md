# `netgraph show`

Print the fully resolved configuration of one element — ranges expanded, template
merged, defaults materialised, values normalised. This is what netgraph actually
works with, rather than what was typed, and `--raw` prints the other half: the
document exactly as it stands in the file.

Use it whenever a diagram, a finding or an `ipam` figure disagrees with your
reading of a document. One of the two is wrong about what the file says, and this
command settles which.

---

## Contents

- [Synopsis](#synopsis)
- [What "resolved" means](#what-resolved-means)
- [Naming an element](#naming-an-element)
- [Reading a merge](#reading-a-merge)
- [Reading a default](#reading-a-default)
- [Output formats](#output-formats)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)

---

## Synopsis

<!-- generated: synopsis show -->
```text
netgraph [GLOBAL OPTIONS] show [OPTIONS] NAME
```
<!-- /generated -->

## What "resolved" means

The loader rewrites a document twice before the models ever see it, and the models
then fill in everything the schema defaults. Four things happen, and `show`
displays the result of all four:

* **Interface ranges are expanded.** A single `range: GigabitEthernet1/0/[1-48]`
  entry becomes forty-eight interfaces, each with the port number substituted into
  its `description` ([§6.2.5](../schema.md#625-range--declaring-many-interfaces-at-once)).
* **`spec.from` is merged away.** The template's keys are merged under the
  device's own, `interfaces` by `name`
  ([§6.6.1](../schema.md#661-merge-rules)). The `from` key itself is gone from the
  output, because there is nothing left for it to point at.
* **Defaults are materialised.** An interface gains `enabled: true` and its
  `mtu`; a `switch` gains `forwarding: {ipv4: false, ipv6: false}` and a `router`
  the same two as true; an access port gains the derived
  `acceptable_frames: admit-only-untagged-and-priority-tagged` and
  `ingress_filtering: true`; a bridge gains its `type`.
* **Values are normalised.** `10.0.0.1/24` written as a shorthand string becomes
  `{ip, prefix_length}` under `addresses`, a MAC is lower-cased into its canonical
  spelling, and a VLAN list is compacted.

Everything downstream — the validator, the graph, every renderer,
[`netgraph ipam`](ipam.md) — reads that resolved form and nothing else.

## Naming an element

`NAME` is either a [fully-qualified name](../inventory-layout.md#folders-are-namespaces)
such as `sites/north/access/sw-north-acc-03`, or a short name that is unique in
the inventory. A short name that matches two elements is a usage error listing
both candidates, and a name that matches nothing points you at
[`netgraph list devices`](list.md). Shell completion completes `NAME` against the
inventory, so the qualified names rarely need typing in full.

Templates are not elements and cannot be shown: they have no name in the element
namespace at all. Read one through a device that inherits it.

If some document in the tree failed to load, `show` says so on stderr and answers
anyway — the question was about one element, and refusing because an unrelated
file is broken would be unhelpful. Run
[`netgraph validate`](validate.md) for the details.

## Reading a merge

`--raw` (spelled `--no-expand` if that reads better in a script) prints the
document as written: an interface `range` still a range, a `spec.from` still a
reference. Diff the two outputs and you have read the merge.

Here is the templated campus switch as it stands in the file — nine lines of
`spec`:

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

And the same switch resolved: 51 interfaces, the vendor and model the template
supplied, the VLAN database, the management VRF, and every one of the
forty-eight access ports the template's one `range` entry stood for. The middle
is elided here, not by the command:

<!-- run: -->
```console
$ netgraph -i examples/campus show sw-north-acc-03
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-north-acc-03
  description: Access switch 03, North campus - third floor riser.
  labels:
    site: north
    role: access
    env: prod
  annotations: {}
spec:
  vendor: Cisco
  model: C9200L-48P
  location: Building A, Hauptstrasse 1 / floor 3 / IDF-3
  interfaces:
...
  - name: GigabitEthernet1/0/1
    type: ethernet
    description: Access port 1 - staff
    enabled: false
    mtu: 1500
    vlan:
      mode: access
      access_vlan: 10
      ingress_filtering: true
      acceptable_frames: admit-only-untagged-and-priority-tagged
...
  bridge:
    name: br0
    type: customer-vlan-bridge
    address: 00:1b:0d:01:a3:ff
  vlans:
  - id: 10
    name: staff
  - id: 20
    name: lab
  - id: 30
    name: voice
  - id: 99
    name: mgmt
  forwarding:
    ipv4: false
    ipv6: false
  netns: []
  vrfs:
  - name: mgmt
    rd: 65001:99
    description: In-band management
  routes: []
```

The `description: Access port 1 - staff` is worth pausing on: the template wrote
`Access port {} - staff` once, and the `{}` became the port number as the range
expanded.

With a shell that has process substitution, the diff is the merge:

<!-- norun: a shell pipeline with process substitution -->
```bash
diff <(netgraph show sw-north-acc-03 --raw) <(netgraph show sw-north-acc-03)
```

## Reading a default

Nothing needs a template for the two outputs to differ. A three-line host
document is the shortest demonstration of what the schema fills in — and of the
address normalisation, which is the one that surprises people:

<!-- run: -->
```console
$ netgraph -i examples/quickstart show pc-alice --raw
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-alice
  labels:
    site: office
spec:
  interfaces:
  - name: eno1
    type: ethernet
    mac: 00:1e:8c:bb:00:01
    mtu: 1500
    ipv4:
      addresses:
      - 192.168.10.20/24
```

<!-- run: -->
```console
$ netgraph -i examples/quickstart show pc-alice
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-alice
  labels:
    site: office
  annotations: {}
spec:
  interfaces:
  - name: eno1
    type: ethernet
    enabled: true
    mac: 00:1e:8c:bb:00:01
    mtu: 1500
    ipv4:
      enabled: true
      forwarding: false
      mtu: 1500
      addresses:
      - ip: 192.168.10.20
        prefix_length: 24
  vlans: []
  forwarding:
    ipv4: false
    ipv6: false
  netns: []
  vrfs: []
  routes: []
```

`192.168.10.20/24` is one string in the file and two fields in the model, and it
is the two fields that every rule about addressing is written against. The
per-family `mtu` and `forwarding` under `ipv4` are the RFC 8344 fields
([§9.2](../schema.md#92-ip-rfc-8344)) — inherited from the interface and the
device respectively, which is why a router's interfaces come back with
`forwarding: true` without saying so anywhere.

## Output formats

`-F/--output-format` chooses `yaml` (the default, for reading) or `json` (for
piping). Both carry the same document, and `--raw` applies to either — the raw
JSON of a document written in YAML is a convenient way to hand a single element to
something that is not netgraph.

<!-- norun: a shell pipeline -->
```bash
netgraph -i examples/campus show sw-north-acc-03 -F json | jq '.spec.interfaces | length'
```

## Arguments

<!-- generated: arguments show -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `NAME` | yes | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options show -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-F`, `--output-format` | `[yaml\|json]` | `yaml` | Serialisation of the resolved document. |
| `--raw`, `--no-expand` | — | off | Print the document as written: ranges unexpanded, 'from' unmerged. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The element was printed. |
| `2` | Usage error: `NAME` matches nothing, or is ambiguous and every candidate is listed. |
| `3` | The inventory could not be discovered or read at all, or the file `--raw` re-read has picked up a syntax error since the load. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

Documents that failed to load do not change the exit code: they earn a warning on
stderr and the element you asked for is still printed.

## See also

* [`docs/inventory-layout.md`](../inventory-layout.md#declaring-a-48-port-switch-without-typing-it-48-times)
  — interface ranges and device templates, which are what makes `--raw` worth
  having.
* [`docs/schema.md` §2.4](../schema.md#24-provenance) — how a field's file and line
  survive expansion and merging, so a template's mistake is reported once.
* [`netgraph list`](list.md) — every element's qualified name, when you need to
  find out what to pass here.
* [`netgraph validate`](validate.md) — the checks that run against the resolved
  form this command prints.
