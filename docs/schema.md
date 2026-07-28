# netgraph YAML schema specification

Version: `netgraph.dev/v1alpha1`
Status: draft — normative for netgraph 0.1.x

This document specifies the on-disk YAML format that netgraph reads. It is the
contract between the inventory author, the loader (`netgraph.loader`), the
typed models (`netgraph.models`) and the renderers (`netgraph.render`).

Field names and value spaces are derived from the following standard data
models:

| Short name | Module | Prefix | Source |
|---|---|---|---|
| ietf-interfaces | `ietf-interfaces` (rev. 2018-02-20) | `if` | [RFC 8343](https://www.rfc-editor.org/rfc/rfc8343) |
| ietf-ip | `ietf-ip` (rev. 2018-02-14) | `ip` | [RFC 8344](https://www.rfc-editor.org/rfc/rfc8344) |
| iana-if-type | `iana-if-type` | `ianaift` | [RFC 7224](https://www.rfc-editor.org/rfc/rfc7224) + IANA registry |
| ietf-yang-types / ietf-inet-types | `ietf-yang-types`, `ietf-inet-types` | `yang`, `inet` | [RFC 6991](https://www.rfc-editor.org/rfc/rfc6991) |
| dot1q-bridge | `ieee802-dot1q-bridge` | `dot1q` | IEEE Std 802.1Q-2018 (802.1Qcp YANG) |
| dot1q-types | `ieee802-dot1q-types` | `dot1qtypes` | IEEE Std 802.1Q-2018 |

Every YAML field that has a standard counterpart is mapped to its YANG path in
[§9 YANG mapping](#9-yang-mapping). netgraph is a *documentation and
visualisation* tool, not a configuration agent: it therefore records **intended**
state, and happily accepts values for nodes that the YANG models declare
`config false` (for example `if:phys-address` and `if:speed`). Those cases are
called out individually.

**See also.** This document explains *why* the schema is shaped the way it is.
Three companions answer narrower questions:
[`schema-reference.md`](schema-reference.md) is the per-field lookup table,
generated from the pydantic models;
[`validation-rules.md`](validation-rules.md) documents every rule this release
actually enforces and how to suppress it; [`yang-mapping.md`](yang-mapping.md)
expands §9 with the reasoning and with what is deliberately left uncovered.

## Contents

1. [Conventions](#1-conventions)
2. [Inventory layout and loading](#2-inventory-layout-and-loading)
3. [Document envelope](#3-document-envelope)
4. [Names and references](#4-names-and-references)
5. [Scalar types](#5-scalar-types)
6. [Device kinds](#6-device-kinds)
7. [Cables](#7-cables)
8. [Adapters](#8-adapters)
9. [YANG mapping](#9-yang-mapping)
10. [Validation rules](#10-validation-rules)
11. [Worked examples](#11-worked-examples)
12. [Compatibility policy](#12-compatibility-policy)
13. [Editor integration](#13-editor-integration)

---

## 1. Conventions

* The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY**
  are used as in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).
* YAML keys are `snake_case`. Where a field is derived from a YANG node, the
  hyphens of the YANG identifier become underscores: `prefix-length` →
  `prefix_length`, `phys-address` → `mac` (renamed for readability, see §9).
  The three envelope keys `apiVersion`, `kind` and `metadata` keep their
  Kubernetes-style spelling because they are envelope, not model, fields.
  `apiVersion` is the **only** camelCase key in the whole schema.
* **Unknown keys are rejected** (`NG-D005`). Silently ignoring a misspelt
  `trunk_vlan` would produce a diagram that disagrees with the file, which is
  the exact failure mode this tool exists to prevent.
* Enumerated values are lower-case and hyphen-free unless quoted from a
  standard (for example `admit-all-frames`).
* Defaults are applied by the loader, not by the renderer. A document and its
  fully defaulted, normalised form MUST render identically.
* All parsing is done with a YAML 1.1 safe loader. Aliases and anchors are
  supported *within a single document*; custom tags are rejected.

### 1.1 Reading the tables

Each field table uses these columns:

* **Field** — the YAML key, `path.to.key` relative to the section's root.
* **Type** — see [§5](#5-scalar-types).
* **Req.** — `M` mandatory, `O` optional, `C` conditional (condition in Notes).
* **Default** — value applied when the key is absent.

---

## 2. Inventory layout and loading

An *inventory* is a directory tree. netgraph walks it recursively and loads
every YAML document it finds. Folders are for humans — group by site, by rack,
by tenant, whatever suits the team. Cross-references (§4.2) work across any file
in the tree; the only thing a folder contributes is a *namespace* (§2.2) that
keeps names short without making them collide.

### 2.1 Discovery rules

| ID | Rule |
|---|---|
| `NG-L001` | Files matching `*.yaml` or `*.yml` (case-insensitive) are loaded. All other files are ignored. |
| `NG-L002` | Path components whose basename starts with `.` or `_` are skipped, including directories. Use `_scratch/` for work in progress. |
| `NG-L003` | Symbolic links are followed, but a link that escapes the inventory root, forms a cycle, or reaches a directory already loaded through another path is an error. |
| `NG-L004` | A file MAY contain several documents separated by `---`. Empty documents are skipped silently, but they still consume a document index. |
| `NG-L005` | Load order is deterministic: files sorted by their byte-wise POSIX path relative to the inventory root, then by document index within the file. Renderers rely on this for stable output. |
| `NG-L006` | A `.netgraphignore` file excludes paths from the walk. The syntax is the `.gitignore` subset described in §2.3; a file in a subdirectory applies to that subtree and overrides its parents. |
| `NG-L007` | A mapping key that appears twice in the same block is an error. Silently keeping the last value would make the diagram disagree with the file. |
| `NG-L008` | Files are read as UTF-8 (a leading BOM is tolerated) with a safe loader. Custom tags — `!Ref`, `!!python/...` — are rejected; anchors and aliases are supported within a single document. |

Loading is *total*: a file that cannot be read, a YAML syntax error, a schema
violation and a duplicate name are all reported with their location and the walk
continues, so one broken file cannot hide the rest of the inventory.

### 2.2 Namespaces and name resolution

An element's **fully-qualified name** is the directory holding its document,
relative to the inventory root, plus its `metadata.name`. A `switch` named
`sw1` declared in `sites/berlin/rack1/sw1.yaml` is therefore
`sites/berlin/rack1/sw1`, and one declared at the root is just `sw1`.

References (§4.2) are written with the plain name and resolved *outwards*:

1. the namespace of the referring document,
2. each ancestor namespace, nearest first, the root last,
3. the inventory as a whole — but only when exactly one element carries that
   name; otherwise the reference is ambiguous and every candidate is named in
   the diagnostic (`NG-N002`).

So two racks may each hold a `sw1` without qualification, and a device at the
root is visible from everywhere. A reference MAY also be written fully qualified
(`sites/berlin/rack1/sw1`), which is tried relative to the current namespace
first and as an absolute name second.

### 2.3 `.netgraphignore`

Optional, one per directory, applying to that directory and everything below it.
Blank lines and `#` comments are skipped; `!` negates a pattern and the last
matching rule wins; a trailing `/` restricts a rule to directories; a pattern
containing a `/` anywhere but at the end is anchored to the directory holding
the file, otherwise it matches a basename at any depth; `*` and `?` do not cross
`/`, `**` does. As in git, a path below an excluded directory cannot be
re-included.

```text
vendor/                 # a directory, anywhere below this file
*.bak.yaml              # a basename pattern, at any depth
/staging.yaml           # only in this directory
generated/**            # everything below generated/
!generated/keep.yaml    # ... except this one (the parent is not excluded)
```

### 2.4 Provenance

The loader attaches the source location (`file`, `document index`, `line`) to
every element. It is **not** a user-writable field; anything the user puts under
a reserved provenance key is rejected. Diagnostics quote it as
`sites/hq/switches/sw-access-01.yaml#0:17`.

### 2.5 Suggested layout

```text
inventory/
├── sites/
│   ├── hq/
│   │   ├── routers/rtr-edge-01.yaml
│   │   ├── switches/sw-access-01.yaml
│   │   ├── hosts/pc-alice.yaml
│   │   └── cables/hq-links.yaml          # several documents in one file
│   └── lab/
│       └── lab.yaml
├── .netgraphignore                       # optional exclusions (NG-L006)
└── _drafts/                              # skipped (NG-L002)
```

`sw-access-01` above is fully qualified as `sites/hq/switches/sw-access-01`; a
cable in `sites/hq/cables/` refers to it as plain `sw-access-01` because the
lookup walks up to `sites/hq` and finds it there (§2.2).

---

## 3. Document envelope

Every document is a mapping with exactly four top-level keys.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-access-01
  description: Access switch, HQ ground floor
  labels:
    site: hq
    rack: g-01
spec:
  ...
```

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `apiVersion` | string | M | — | MUST be `netgraph.dev/v1alpha1` for this revision. See §12. |
| `kind` | enum | M | — | One of `switch`, `router`, `hub`, `computer`, `server`, `cable`, `adapter`. Lower-case; other spellings are rejected. |
| `metadata` | mapping | M | — | §3.1 |
| `spec` | mapping | M | — | Shape depends on `kind`: §6 (devices), §7 (cable), §8 (adapter). |

### 3.1 `metadata`

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `name` | name | M | — | Unique within its namespace (§2.2, `NG-N002`). Grammar in §4.1. |
| `description` | string | O | `null` | Free text, may be multi-line. Rendered as a node tooltip. |
| `labels` | map[string, string] | O | `{}` | Selector-friendly key/value pairs. Keys match `[a-z0-9]([-a-z0-9_.]*[a-z0-9])?` (≤63 chars) and MAY carry a DNS-style prefix (`example.com/tier`). Values ≤253 chars. The prefix `netgraph.dev/` is reserved for tool-generated labels. |
| `annotations` | map[string, string] | O | `{}` | Per-element input to the tooling. Same key grammar as `labels`, but the `netgraph.dev/` prefix is permitted (annotations exist to carry tool keys) and values may be up to 4096 chars. Annotations are **not** selectable and never affect the graph. |

Labels drive filtering (`netgraph render --select site=hq`) and grouping
(`--group-by rack`), so prefer a small, consistent key set: `site`, `rack`,
`role`, `env`, `owner`.

Annotations are the opposite: they are read by the tool, not by the user. The
one this revision defines is `netgraph/ignore`, which suppresses validation
rules on the element carrying it (§10.10):

```yaml
metadata:
  name: spare-switch
  annotations:
    netgraph/ignore: "W103, E004"   # or "*" for every rule
```

---

## 4. Names and references

### 4.1 Name grammar

```abnf
name        = label *( ( "-" / "_" / "." ) label )
label       = ALPHA-DIGIT *( ALPHA-DIGIT )
ALPHA-DIGIT = %x41-5A / %x61-7A / %x30-39   ; A-Z a-z 0-9
```

Element names are 1–253 characters, case-sensitive, and **MUST NOT** contain
`:` — the colon is the reference separator (§4.2).

Interface names are 1–64 characters and use a wider set, because vendors do:

```abnf
ifname = 1*( ALPHA-DIGIT / "-" / "_" / "." / "/" )
```

Examples: `eno1`, `eth0`, `GigabitEthernet1/0/1`, `ge-0/0/1`, `bond0.30`,
`enx001122334455`. Interface names are unique within a device (`NG-I001`) and
map directly to the `if:interface` list key, which is a plain `string` in
RFC 8343.

### 4.2 Interface references

Cables and adapters point at interfaces with a two-part reference:

```abnf
ifref = name ":" ifname
```

`sw-access-01:GigabitEthernet1/0/1`. The device part MUST resolve to a
declared element of kind `switch`, `router`, `hub`, `computer`, `server` or
`adapter`; the interface part MUST resolve to an interface declared on that
element (`NG-C002`, `NG-C003`).

An equivalent mapping form is accepted and normalises to the same value — use
it when a name would otherwise need quoting:

```yaml
endpoints:
  - device: sw-access-01
    interface: GigabitEthernet1/0/1
```

A *device* reference (no colon) is used by `adapter.spec.upstream.attached_to`
and resolves to the element as a whole.

---

## 5. Scalar types

| Type | Definition | YANG counterpart |
|---|---|---|
| `name` | §4.1 | list key (`string`) |
| `ifname` | §4.1 | `if:name` (`string`) |
| `ifref` | §4.2 | — (netgraph construct) |
| `boolean` | YAML `true`/`false`. `yes`/`no`/`on`/`off` are **rejected** to avoid the YAML 1.1 Norway problem. | `boolean` |
| `mac` | Six octets. Canonical form `xx:xx:xx:xx:xx:xx`, lower-case. `XX-XX-XX-XX-XX-XX` and `xxxx.xxxx.xxxx` are accepted and normalised. | `yang:phys-address` |
| `ipv4-address` | Dotted quad, no zone. | `inet:ipv4-address-no-zone` |
| `ipv6-address` | RFC 4291 text form, no zone. Normalised to RFC 5952 lower-case compressed form. | `inet:ipv6-address-no-zone` |
| `prefix-length` | Integer, 0–32 (v4) / 0–128 (v6). | `uint8` |
| `netmask` | Dotted quad, IPv4 only. | `yang:dotted-quad` |
| `mtu` | Integer ≥ 68 for IPv4, ≥ 1280 for IPv6, ≤ 65535. | `uint16` / `uint32` |
| `vlan-id` | Integer 1–4094. 0 (priority-tagged) and 4095 (reserved) are rejected. | `dot1qtypes:vlanid` |
| `vlan-set` | List of `vlan-id`, inclusive range strings (`"100-110"`), or the literal `all` (= 1–4094) / `none`. Normalised to a sorted, coalesced set. | `dot1qtypes:vid-range-type` |
| `speed` | Bit rate. Either an integer in bit/s, or `<number><unit>` with unit `bps`, `kbps`, `Mbps`, `Gbps`, `Tbps` (decimal multiples: 1 Gbps = 1 000 000 000 bit/s). Normalised to `uint64` bit/s; rendered back in the largest exact unit. | `yang:gauge64` (`if:speed`) |
| `length` | Non-negative number of metres (`length_m`). | — |

---

## 6. Device kinds

`switch`, `router`, `hub`, `computer` and `server` share one `spec` shape. They
differ only in which fields are permitted (§6.5) and in how the renderer draws
them. `computer` and `server` are structurally identical; the distinction is
purely presentational (workstation vs. rack-mount glyph) and for label-free
filtering.

### 6.1 Device `spec`

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `vendor` | string | O | `null` | Free text, e.g. `Cisco`. |
| `model` | string | O | `null` | e.g. `C9300-48P`. |
| `serial` | string | O | `null` | Asset tracking; never rendered by default. |
| `location` | string | O | `null` | Human-readable, e.g. `HQ / G-01 / U12`. |
| `interfaces` | list[Interface] | M | — | §6.2. MUST contain at least one entry. |
| `bridge` | Bridge | O | `null` | §6.3. Permitted on `switch`, `router`, `computer`, `server`. |
| `vlans` | list[VlanDef] | O | `[]` | §6.4. VLAN database. Same permission set as `bridge`. |
| `forwarding` | mapping | O | see §6.1.1 | `{ipv4: boolean, ipv6: boolean}`. |

#### 6.1.1 `forwarding`

Maps to `ip:ipv4/forwarding` and `ip:ipv6/forwarding`, which RFC 8344 defines
per interface. netgraph declares it once per device as the device-wide default;
an interface MAY override it (`interfaces[].ipv4.forwarding`).

Default by kind: `true` for `router`, `false` for every other kind. This
matches the RFC 8344 default (`false`) for hosts while keeping router documents
free of boilerplate.

### 6.2 `interfaces[]`

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `name` | ifname | M | — | Unique per device (`NG-I001`). |
| `type` | enum | M | — | §6.2.1. |
| `description` | string | O | `null` | → `if:description`. |
| `enabled` | boolean | O | `true` | Intended admin state → `if:enabled`. |
| `mac` | mac | O | `null` | → `if:phys-address` (`config false` in RFC 8343; see §9.1). |
| `mtu` | mtu | O | `null` | Layer-2 MTU. See §6.2.2. |
| `ipv4` | AddressFamily | O | `null` | §6.2.3. |
| `ipv6` | AddressFamily | O | `null` | §6.2.3. |
| `vlan` | Vlan | O | `null` | §6.2.4. |
| `parent` | ifname | C | — | Required for `type: vlan`; MUST NOT appear otherwise. → `if:lower-layer-if`. |
| `members` | list[ifname] | C | — | Required for `type: lag` and `type: bridge`; MUST NOT appear otherwise. → `if:lower-layer-if`. |

#### 6.2.1 Interface `type`

The four **core** types are mandatory for every implementation:

| `type` | iana-if-type identity | Meaning |
|---|---|---|
| `ethernet` | `ianaift:ethernetCsmacd` | Any IEEE 802.3 port, copper or fibre. |
| `wifi` | `ianaift:ieee80211` | IEEE 802.11 radio. |
| `loopback` | `ianaift:softwareLoopback` | Host loopback or router loopback. |
| `bridge` | `ianaift:bridge` | Software bridge / switch SVI parent. Takes `members`. |

Two **extension** types complete the model for the common cases that would
otherwise be inexpressible (sub-interfaces and link aggregation):

| `type` | iana-if-type identity | Meaning |
|---|---|---|
| `vlan` | `ianaift:l2vlan` | 802.1Q sub-interface. Requires `parent` and `vlan.access_vlan` (the encapsulation VID). |
| `lag` | `ianaift:ieee8023adLag` | Aggregated link. Requires `members`. |

#### 6.2.2 `mtu`

RFC 8343 has **no** interface-level MTU leaf — the only standard MTU leaves are
per address family in RFC 8344 (`ip:ipv4/mtu`, `uint16`, min 68;
`ip:ipv6/mtu`, `uint32`, min 1280). netgraph therefore treats
`interfaces[].mtu` as the layer-2 MTU and propagates it to both families unless
they override it. See §9.2 for the exact mapping and the
`ietf-interfaces-common` note.

#### 6.2.3 `ipv4` / `ipv6`

Canonical form mirrors the RFC 8344 containers:

```yaml
ipv4:
  enabled: true
  forwarding: false
  mtu: 1500
  addresses:
    - ip: 10.10.10.1
      prefix_length: 24
```

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `enabled` | boolean | O | `true` | → `ip:ipv4/enabled`, `ip:ipv6/enabled`. |
| `forwarding` | boolean | O | device `spec.forwarding` | → `ip:*/forwarding`. |
| `mtu` | mtu | O | interface `mtu` | → `ip:ipv4/mtu`, `ip:ipv6/mtu`. |
| `addresses` | list[Address] | O | `[]` | Key is `ip`; duplicates are an error (`NG-A002`). |

Address entries:

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `ip` | ipv4-address / ipv6-address | M | — | → `ip:address/ip`. |
| `prefix_length` | prefix-length | C | — | → `ip:address/prefix-length`. Exactly one of `prefix_length` / `netmask`. |
| `netmask` | netmask | C | — | IPv4 only → `ip:address/netmask`. RFC 8344 gates this on the `ipv4-non-contiguous-netmasks` feature; netgraph accepts it and normalises contiguous masks to `prefix_length`. |

**Shorthands.** Both are normalised to the canonical form on load, so tooling
downstream of the loader only ever sees the long form:

```yaml
ipv4:
  addresses: [10.10.10.1/24]      # address string  → {ip, prefix_length}
ipv6: [2001:db8:10::1/64]         # bare list       → {addresses: [...]}
```

#### 6.2.4 `vlan`

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `mode` | enum | M | — | `access` or `trunk`. |
| `access_vlan` | vlan-id | C | `1` | Required in `access` mode; forbidden in `trunk` mode. |
| `trunk_vlans` | vlan-set | C | — | Required in `trunk` mode; forbidden in `access` mode. |
| `native_vlan` | vlan-id | O | `null` | `trunk` mode only. Untagged VLAN on the trunk. |
| `ingress_filtering` | boolean | O | `true` | → `dot1q:bridge-port/enable-ingress-filtering`. |
| `acceptable_frames` | enum | O | derived | `admit-all-frames`, `admit-only-VLAN-tagged-frames`, `admit-only-untagged-and-priority-tagged`. Derivation in §9.3. |

`access`/`trunk` are operational vocabulary, not 802.1Q vocabulary. §9.3 gives
the exact translation into `dot1q:bridge-port` leaves plus VLAN registration
entries — read it before assuming a vendor-specific meaning.

### 6.3 `bridge`

Declares the 802.1Q bridge component that the device's switching ports belong
to. Optional: a switch with a single implicit bridge does not need it.

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `name` | name | O | `metadata.name` | → `dot1q:bridge/name`. |
| `type` | enum | O | `customer-vlan-bridge` | `customer-vlan-bridge`, `provider-bridge`, `provider-edge-bridge`, `two-port-mac-relay-bridge`, `mac-bridge` → `dot1q:bridge/bridge-type` identity. |
| `address` | mac | O | `null` | → `dot1q:bridge/address`. |

### 6.4 `vlans[]`

The VLAN database. Declaring a VLAN here is optional but recommended: it gives
the VLAN a name for rendering and lets the validator flag ports that reference
an undeclared VLAN (`NG-V004`, a warning).

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `id` | vlan-id | M | — | → `dot1q:vlan/vid`. Unique per device (`NG-V001`). |
| `name` | string (≤32) | O | `null` | → `dot1q:vlan/name` (`dot1qtypes:name-type`). |
| `description` | string | O | `null` | netgraph-only. |

### 6.5 Per-kind constraints

| | `switch` | `router` | `hub` | `computer` | `server` |
|---|---|---|---|---|---|
| `interfaces[].vlan` | ✔ | ✔ | ✘ `NG-H001` | ✔ | ✔ |
| `interfaces[].ipv4` / `ipv6` | ✔ | ✔ | ✘ `NG-H002` | ✔ | ✔ |
| `bridge`, `vlans` | ✔ | ✔ | ✘ `NG-H003` | ✔ | ✔ |
| `forwarding` default | `false` | `true` | n/a | `false` | `false` |
| default glyph | switch | router | hub | workstation | rack server |

A hub is a layer-1 repeater: it has no MAC table, no VLAN awareness and no IP
stack, so those fields are errors rather than warnings. `interfaces[].mac` is
accepted on a hub (some managed hubs have one) but ignored by the renderer.

---

## 7. Cables

A cable is an undirected physical link between exactly two interfaces. It is a
first-class element so that it can carry its own metadata (label, length,
category) and be validated independently of the devices it joins.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: cbl-rtr01-sw01
  labels: {site: hq}
spec:
  endpoints:
    - rtr-edge-01:ge-0/0/1
    - sw-access-01:GigabitEthernet0/1
  medium: copper
  speed: 1Gbps
  length_m: 2
  category: cat6
  connector: rj45
  label: A-014
```

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `endpoints` | list[ifref] | M | — | Exactly two entries (`NG-C001`). Order is not significant; the loader sorts them for canonical output. |
| `medium` | enum | M | — | `copper`, `fiber`, `wireless`. |
| `speed` | speed | O | `null` | Negotiated link rate → `if:speed` on both endpoints (§9.4). |
| `duplex` | enum | O | `full` | `full`, `half`. `half` is only meaningful on a `copper` link into a `hub`. |
| `length_m` | length | O | `null` | Forbidden when `medium: wireless` (`NG-C007`). |
| `category` | string | C | `null` | Copper: `cat5e`, `cat6`, `cat6a`, `cat7`, `cat8`, `dac`. Fiber: `om3`, `om4`, `om5`, `os2`. Forbidden for `wireless` (`NG-C007`). |
| `connector` | string | O | `null` | `rj45`, `lc`, `sc`, `mpo`, `sfp+`, `qsfp28`, … Free text; not validated against `medium`. |
| `label` | string | O | `null` | Physical cable-label / patch-panel identifier printed on the edge. |

### 7.1 Semantics

* A cable is **undirected**. `[a:1, b:2]` and `[b:2, a:1]` describe the same
  link and produce the same graph edge and the same canonical JSON export.
* An interface may terminate **at most one** cable (`NG-C005`). Multi-access
  media are modelled by an explicit `hub` element or, for radio, by a
  `wireless` cable per associated station.
* `medium: wireless` requires **both** endpoints to be `type: wifi`
  (`NG-C006`). It represents an association, not a physical cable; renderers
  draw it dashed.
* A cable between two interfaces of the same device is permitted (loopback
  cables and MLAG peer links on a single logical switch exist) but raises a
  warning (`NG-C004`).

---

## 8. Adapters

An adapter is a device that presents one or more network interfaces over a
non-network host port: USB-to-Ethernet dongles, Thunderbolt docks, PCIe NICs
seen as removable inventory. Modelling them explicitly keeps the *physical*
truth ("the dongle is the thing that breaks") while still letting the renderer
collapse them into the host.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: adapter
metadata:
  name: adp-usb-eth-01
spec:
  vendor: Anker
  model: A83130A1
  form_factor: usb-ethernet
  passthrough: true
  upstream:
    name: usb0
    type: usb
    speed: 5Gbps
    attached_to: laptop-01
  interfaces:
    - name: enx001122334455
      type: ethernet
      mac: 00:11:22:33:44:55
      mtu: 1500
      ipv4:
        addresses: [192.168.50.61/24]
```

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `vendor`, `model`, `serial`, `location` | string | O | `null` | As §6.1. |
| `form_factor` | string | O | `null` | Descriptive: `usb-ethernet`, `dock`, `media-converter`, `sfp-module`. |
| `passthrough` | boolean | O | `true` | Rendering hint, §8.2. |
| `ports` | uint (≥1) | O | `null` | Downstream network ports the hardware physically provides. Declaring it lets the validator catch an inventory that has outgrown the device (`NG-X008`); leaving it out disables that check. |
| `upstream` | Upstream | M | — | §8.1. |
| `interfaces` | list[Interface] | M | — | §6.2, at least one. Every entry MUST be `type: ethernet`, `wifi` or `lag` (`NG-X003`). |

### 8.1 `upstream`

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `name` | ifname | M | — | Port name on the adapter side, e.g. `usb0`. Referenceable as `adp-usb-eth-01:usb0`. |
| `type` | enum | M | — | `usb`, `usb-c`, `thunderbolt`, `pcie`, `m2`, `sfp`, `internal`. |
| `speed` | speed | O | `null` | Host-bus rate, e.g. `5Gbps` for USB 3.0. |
| `attached_to` | name | O | `null` | The host device the adapter is plugged into, e.g. `laptop-01`. A **device** reference, not an `ifref`: `v1alpha1` has no interface type for a host-side USB/Thunderbolt receptacle, so there is nothing to point at. Unresolvable references are an error (`NG-X001`); the `device:port` form is reserved for a future revision.<br>An adapter chained behind another adapter (dongle in a dock) names the dock here. |

`upstream.type` maps to `ianaift:usb` for `usb`/`usb-c` and to `ianaift:other`
for the rest — the IANA registry has no Thunderbolt/PCIe identity. The upstream
port is *not* an entry in `spec.interfaces`: it carries no L2/L3 configuration
and must not accumulate addresses.

### 8.2 Graph semantics

* The `attached_to` reference creates a graph edge **host → adapter** with
  `medium: copper` and `speed = upstream.speed`. No `cable` document is needed
  or permitted for the host attachment; use a cable only for what leaves the
  adapter's downstream ports.
* An adapter with no `attached_to` is a free-standing node (spare in a drawer,
  or a media converter in a run). It raises `NG-X002` (warning) if any of its
  downstream interfaces is cabled, since a cabled-but-unattached adapter is
  almost always an omission.
* `passthrough: true` tells the renderer it MAY collapse the adapter, drawing
  its downstream interfaces as if they belonged to the host and folding the
  adapter's name into the edge label. `netgraph render --no-collapse-adapters`
  overrides this. `passthrough: false` (media converters, docks that switch)
  forces a distinct node.
* Collapsing never changes connectivity: the path
  `host → adapter → cable → peer` is preserved either way, so reachability
  analyses are unaffected by the rendering choice.
* Within the adapter, each downstream interface is stacked on the upstream
  port: `if:lower-layer-if` of `enx001122334455` is `usb0`, and
  `if:higher-layer-if` of `usb0` lists the downstream interfaces (§9.5).

---

## 9. YANG mapping

This section is normative for anyone exporting netgraph data to NETCONF/RESTCONF
or comparing an inventory against a live device. Paths are written with the
module prefixes from the table at the top of this document. `«dev»` stands for
the device the interface belongs to; netgraph has no single YANG node for "a
device", so `metadata.name` is the datastore boundary, not a data node.

### 9.1 Interface (RFC 8343)

| YAML | YANG path | YANG type | Notes |
|---|---|---|---|
| `interfaces[].name` | `/if:interfaces/if:interface/if:name` | `string` | List key. |
| `interfaces[].description` | `…/if:description` | `string` | |
| `interfaces[].type` | `…/if:type` | `identityref` → `ianaift:*` | Identity per §6.2.1. |
| `interfaces[].enabled` | `…/if:enabled` | `boolean`, default `true` | Intended admin state. Compare against `if:admin-status` when diffing live state. |
| `interfaces[].mac` | `…/if:phys-address` | `yang:phys-address` | **`config false` in RFC 8343.** netgraph stores the intended/burned-in address; an exporter targeting a live datastore MUST NOT write it. |
| `interfaces[].parent` | `…/if:lower-layer-if` | `leafref` list | `config false`. Single-element list for `type: vlan`. |
| `interfaces[].members[]` | `…/if:lower-layer-if` | `leafref` list | `config false`. One entry per member, for `type: lag` and `type: bridge`. |
| *(derived)* | `…/if:higher-layer-if` | `leafref` list | `config false`. Computed as the inverse of `parent`/`members`; never written by hand. |
| `cable.speed` | `…/if:speed` | `yang:gauge64` | **`config false`.** See §9.4. |

RFC 8343 nodes netgraph deliberately does **not** model: `if:if-index`,
`if:last-change`, `if:oper-status`, `if:statistics`, `if:link-up-down-trap-enable`.
They are operational counters or SNMP artefacts with no place in a
source-of-truth document.

### 9.2 IP (RFC 8344)

`ietf-ip` augments `/if:interfaces/if:interface`; the prefix `…` below expands
to that path.

| YAML | YANG path | YANG type | Notes |
|---|---|---|---|
| `interfaces[].ipv4.enabled` | `…/ip:ipv4/ip:enabled` | `boolean`, default `true` | |
| `interfaces[].ipv4.forwarding` | `…/ip:ipv4/ip:forwarding` | `boolean`, default `false` | Device-level `spec.forwarding.ipv4` supplies the default. |
| `interfaces[].ipv4.mtu` | `…/ip:ipv4/ip:mtu` | `uint16`, range 68..max | |
| `interfaces[].ipv4.addresses[].ip` | `…/ip:ipv4/ip:address/ip:ip` | `inet:ipv4-address-no-zone` | List key. |
| `interfaces[].ipv4.addresses[].prefix_length` | `…/ip:ipv4/ip:address/ip:prefix-length` | `uint8`, 0..32 | `choice subnet`, case `prefix-length`. |
| `interfaces[].ipv4.addresses[].netmask` | `…/ip:ipv4/ip:address/ip:netmask` | `yang:dotted-quad` | `choice subnet`, case `netmask`; gated on feature `ipv4-non-contiguous-netmasks`. |
| `interfaces[].ipv6.enabled` | `…/ip:ipv6/ip:enabled` | `boolean`, default `true` | |
| `interfaces[].ipv6.forwarding` | `…/ip:ipv6/ip:forwarding` | `boolean`, default `false` | |
| `interfaces[].ipv6.mtu` | `…/ip:ipv6/ip:mtu` | `uint32`, min 1280 | |
| `interfaces[].ipv6.addresses[].ip` | `…/ip:ipv6/ip:address/ip:ip` | `inet:ipv6-address-no-zone` | List key. |
| `interfaces[].ipv6.addresses[].prefix_length` | `…/ip:ipv6/ip:address/ip:prefix-length` | `uint8`, 0..128 | Mandatory in RFC 8344 — there is no netmask case for IPv6. |

`interfaces[].mtu` has **no** RFC 8343 counterpart. On export it is written to
both `ip:ipv4/mtu` and `ip:ipv6/mtu` (subject to their range limits, so a
layer-2 MTU below 1280 is not propagated to IPv6). The nearest standards-track
home for a true layer-2 MTU is `if-cmn:mtu` in the `ietf-interfaces-common`
draft, which is not yet an RFC; when it lands, `interfaces[].mtu` will map
there directly.

Not modelled from RFC 8344: `ip:neighbor` lists (ARP/NDP caches are
operational), `ip:dup-addr-detect-transmits`, `ip:autoconf`,
`ip:address/ip:origin` and `ip:address/ip:status` (all `config false`).

### 9.3 VLAN (IEEE 802.1Q)

802.1Q has no "access port" or "trunk port" — those are vendor CLI abstractions
over three independent knobs: the port VLAN ID, the acceptable-frame-types
filter, and per-VLAN egress/untagged membership. netgraph expands them like
this.

**Port configuration** — augment `/if:interfaces/if:interface/dot1q:bridge-port`:

| YAML | YANG leaf | Value |
|---|---|---|
| `vlan.access_vlan` (mode `access`) | `dot1q:pvid` | `access_vlan` |
| `vlan.native_vlan` (mode `trunk`) | `dot1q:pvid` | `native_vlan`, or `1` if omitted |
| `vlan.ingress_filtering` | `dot1q:enable-ingress-filtering` | as given, default `true` |
| `vlan.acceptable_frames` | `dot1q:acceptable-frame` | as given, else derived below |
| `bridge.name` | `dot1q:component-name` | the component this port belongs to |
| *(from device kind)* | `dot1q:port-type` | `dot1q:c-vlan-bridge-port` for `customer-vlan-bridge`, `dot1q:d-bridge-port` for `mac-bridge` |

Derivation of `acceptable_frames` when not stated explicitly:

| Mode | `native_vlan` | `dot1q:acceptable-frame` |
|---|---|---|
| `access` | — | `admit-only-untagged-and-priority-tagged` |
| `trunk` | present | `admit-all-frames` |
| `trunk` | absent | `admit-only-VLAN-tagged-frames` |

**VLAN membership** — entries in
`/dot1q:bridges/dot1q:bridge[name=«bridge.name»]/dot1q:component/dot1q:bridge-vlan/dot1q:vlan`:

| Situation | Effect on the VLAN entry with `dot1q:vid = V` |
|---|---|
| `mode: access`, `access_vlan: V` | port added to `dot1q:egress-ports` **and** `dot1q:untagged-ports` |
| `mode: trunk`, `V ∈ trunk_vlans` | port added to `dot1q:egress-ports` (tagged) |
| `mode: trunk`, `native_vlan: V` | port added to `dot1q:egress-ports` **and** `dot1q:untagged-ports` |

The VLAN database itself:

| YAML | YANG path | YANG type |
|---|---|---|
| `vlans[].id` | `…/dot1q:bridge-vlan/dot1q:vlan/dot1q:vid` | `dot1qtypes:vlanid` (1..4094) |
| `vlans[].name` | `…/dot1q:bridge-vlan/dot1q:vlan/dot1q:name` | `dot1qtypes:name-type` (≤32) |
| `bridge.name` | `/dot1q:bridges/dot1q:bridge/dot1q:name` | `string` |
| `bridge.address` | `/dot1q:bridges/dot1q:bridge/dot1q:address` | `yang:mac-address` |
| `bridge.type` | `/dot1q:bridges/dot1q:bridge/dot1q:bridge-type` | `identityref` |

`trunk_vlans` is stored as `dot1qtypes:vid-range-type` (for example
`"10,20,100-110"`) and expanded to individual VLAN entries on export.

For a `type: vlan` sub-interface, `vlan.access_vlan` is the encapsulation VID.
It maps to `dot1q:pvid` on the sub-port and identifies the `l2vlan` interface's
VLAN; the parent trunk port MUST list that VID in its `trunk_vlans` or as its
`native_vlan` (`NG-V005`). A `bridge` parent — where a switch's SVI hangs —
carries the union of its members' VLAN sets instead, since the bridge itself
declares no `vlan` block.

### 9.4 Cable

A cable has no YANG representation — 802.1Q and ietf-interfaces model devices,
not the wire between them. Its fields project onto both endpoint interfaces:

| YAML | Projection |
|---|---|
| `cable.speed` | `if:speed` on both endpoint interfaces (`yang:gauge64`, bit/s, `config false`) |
| `cable.medium` | no YANG node; informs the `ianaift` identity choice at export time (`ethernetCsmacd` regardless of copper/fibre; `ieee80211` for `wireless`) |
| `cable.duplex`, `length_m`, `category`, `connector`, `label` | netgraph-only, physical-plant metadata |

If both endpoints and the cable declare a speed, they MUST agree (`NG-C008`).

### 9.5 Adapter

| YAML | Projection |
|---|---|
| `upstream.name` | `/if:interfaces/if:interface/if:name` on the adapter |
| `upstream.type` | `if:type` = `ianaift:usb` (`usb`, `usb-c`) or `ianaift:other` |
| `upstream.speed` | `if:speed` on the upstream interface |
| `interfaces[]` | ordinary `if:interface` entries, each with `if:lower-layer-if = [upstream.name]` |
| *(derived)* | `if:higher-layer-if` on the upstream port lists every downstream interface |
| `upstream.attached_to` | no YANG node; a netgraph topology edge |

---

## 10. Validation rules

Every rule has a stable ID. The validator (`netgraph validate`) reports them as
`NG-C005: interface sw-access-01:Gi0/2 is terminated by 2 cables (cbl-a, cbl-b)`.
IDs are permanent: once assigned, an ID is never reused for a different rule.

Severity `error` fails the run (exit code 4, `ValidationError`); `warning` and
`info` are reported but rendering proceeds. `netgraph validate --strict`
promotes every warning to an error. Individual rules can be re-graded or
silenced per inventory (§10.10).

The semantic validator also prints a short id (`E002`, `W103`) alongside the
`NG-*` id; the two vocabularies are interchangeable everywhere a rule can be
named. §10.9 maps them.

### 10.1 Document and naming

| ID | Sev. | Rule |
|---|---|---|
| `NG-D001` | error | The document is a mapping with the four envelope keys; `apiVersion`, `kind`, `metadata`, `spec` are all present. |
| `NG-D002` | error | `apiVersion` is a recognised version string. |
| `NG-D003` | error | `kind` is one of the seven defined kinds, lower-case. |
| `NG-D004` | error | `spec` matches the shape required by `kind`. |
| `NG-D005` | error | No unknown keys anywhere in the document. |
| `NG-N001` | error | `metadata.name` matches the name grammar (§4.1). |
| `NG-N002` | error | `metadata.name` is unique within its namespace (§2.2), across all kinds; the diagnostic names both source locations. A name reused in a *different* namespace is allowed, and only reported when a reference to it stays ambiguous after the namespace and ancestor lookups have failed. |
| `NG-N003` | error | Label keys and values match the constraints in §3.1. |

### 10.2 Interfaces

| ID | Sev. | Rule |
|---|---|---|
| `NG-I001` | error | Interface names are unique within their device. |
| `NG-I002` | error | `parent` is present exactly for `type: vlan` and resolves to an interface on the same device. |
| `NG-I003` | error | `members` is present exactly for `type: lag` and `type: bridge`, is non-empty, has no duplicates, and every entry resolves to an interface on the same device. |
| `NG-I004` | error | Interface stacking (`parent`/`members`) is acyclic. |
| `NG-I005` | error | A `lag`/`bridge` member is not itself a member of another aggregate, and is not the `parent` of a VLAN sub-interface. |
| `NG-I006` | warning | A `lag`/`bridge` member carries its own `ipv4`/`ipv6` addresses. Addresses belong on the aggregate. |
| `NG-I007` | warning | `mac` is set on a `loopback` interface. |
| `NG-I008` | warning | Two interfaces anywhere in the inventory share the same `mac`. Legitimate for VRRP/CARP and for a `parent`/sub-interface pair, which are exempt. |
| `NG-I009` | warning | `mac` has the multicast bit (least-significant bit of the first octet) set — never valid as a source address. |
| `NG-I010` | info | `mac` is locally administered (second-least-significant bit of the first octet set). |
| `NG-I011` | error | `mtu` is within `[68, 65535]`, and within `[1280, 65535]` if the interface has IPv6 addresses. |
| `NG-I012` | warning | A device declares no `ethernet`, `wifi` or `lag` interface, so it can never be cabled. |
| `NG-I013` | warning | An interface has neither `ipv4` nor `ipv6` addresses and no `vlan` block, so it neither routes nor switches. Hub ports, disabled interfaces, and interfaces another one is stacked on (LAG members, the `parent` of a sub-interface) are exempt. |

### 10.3 Addresses

| ID | Sev. | Rule |
|---|---|---|
| `NG-A001` | error | Exactly one of `prefix_length` / `netmask` per IPv4 address; `prefix_length` is mandatory for IPv6. |
| `NG-A002` | error | Addresses are unique within an address family on one interface (the RFC 8344 list key). |
| `NG-A003` | error | `netmask` is contiguous, or the inventory opts in to non-contiguous masks. |
| `NG-A004` | warning | The same IP address is assigned on two different devices. VRRP/anycast are legitimate; the warning exists because typos are more common. |
| `NG-A005` | warning | An address is the network or broadcast address of its own prefix (does not apply to `/31`, `/32`, `/127`, `/128`). |
| `NG-A006` | warning | Two interfaces on the same device hold overlapping prefixes. |
| `NG-A007` | warning | A loopback interface carries a prefix other than `/32` (v4) or `/128` (v6). |
| `NG-A008` | warning | Exactly one element is addressed in a prefix. Host routes and point-to-point prefixes (at most two host addresses: `/30`–`/32`, `/126`–`/128`) are exempt — the peer of an ISP hand-off is not a declared device. |
| `NG-A009` | warning | Two elements claim the same address inside one prefix while sitting in different broadcast domains. When they share one, `NG-A004` reports it instead. |

### 10.4 VLANs

| ID | Sev. | Rule |
|---|---|---|
| `NG-V001` | error | `vlans[].id` is unique within a device. |
| `NG-V002` | error | `access_vlan` is present in `access` mode and absent in `trunk` mode; `trunk_vlans` vice versa. |
| `NG-V003` | error | `native_vlan` only appears in `trunk` mode. |
| `NG-V004` | warning | A port references a VLAN that the device's `vlans` list does not declare (suppressed when the device declares no `vlans` at all). |
| `NG-V005` | error | For `type: vlan`, the `parent` interface is in `trunk` mode and its `trunk_vlans` (or `native_vlan`) contain the sub-interface's `access_vlan`. |
| `NG-V006` | warning | `native_vlan` is not listed in `trunk_vlans`. It is implicitly added, because a PVID is always a member of its port's VLAN set. |
| `NG-V007` | warning | `trunk_vlans: all` on a port facing a host rather than another switch. |
| `NG-V008` | warning | A `lag` member declares its own `vlan` block that differs from the aggregate's (§10.6). |
| `NG-V009` | warning | An `access` port of a layer-2-only switch (a `switch` that forwards neither IPv4 nor IPv6) carries an IP address. Management addresses belong on a `type: vlan` SVI, which is exempt. |

### 10.5 Cables and topology

| ID | Sev. | Rule |
|---|---|---|
| `NG-C001` | error | `endpoints` has exactly two entries. |
| `NG-C002` | error | Each endpoint's device part resolves to a declared element. |
| `NG-C003` | error | Each endpoint's interface part resolves to an interface on that element (the adapter upstream port counts). |
| `NG-C004` | warning | Both endpoints are on the same device. |
| `NG-C005` | error | An interface terminates at most one cable. |
| `NG-C006` | error | `medium: wireless` requires both endpoints to be `type: wifi`; a non-wireless medium requires neither endpoint to be `type: wifi`. |
| `NG-C007` | error | `length_m` and `category` are absent when `medium: wireless`. |
| `NG-C008` | warning | Endpoint interface speeds and `cable.speed` disagree. |
| `NG-C009` | error | An endpoint is a `loopback`, `vlan` or `bridge` interface. Only physical interfaces (`ethernet`, `wifi`) and `lag` interfaces can be cabled — and cabling a `lag` is itself flagged by `NG-C012`. |
| `NG-C010` | warning | `mtu` differs between the two endpoints. A classic cause of silent path-MTU failures. |
| `NG-C011` | warning | VLAN configuration mismatch across a link: two access ports with different `access_vlan`; an access port facing a trunk; trunks whose `trunk_vlans` sets are disjoint or whose `native_vlan` differs. Resolved through the LAG master when an endpoint is a `lag` member (§10.6). |
| `NG-C012` | warning | An endpoint is a `lag` interface rather than one of its members. Aggregates are logical; cable the physical members. |
| `NG-C013` | warning | `duplex: half` on a link that does not involve a `hub`. |
| `NG-C014` | warning | The topology graph is disconnected. Reported once, listing each component's smallest member name. |
| `NG-C015` | info | An interface is `enabled: true` but terminates no cable. |
| `NG-C016` | warning | A device terminates no cable and neither hosts nor is an adapter attachment: an orphan node. An `attached_to` edge counts as connectivity (§8.2), and a device whose cable names a missing *interface* is still cabled, so `NG-C002`/`NG-C003` are not compounded by this rule. |

### 10.6 LAG resolution

When a cable endpoint is a member of a `lag`, VLAN and MTU checks
(`NG-C010`, `NG-C011`) use the aggregate's configuration, not the member's.
Members are expected to carry no `vlan` block of their own; one that does
triggers `NG-V008` (warning) unless it matches the master exactly.

### 10.7 Hubs

| ID | Sev. | Rule |
|---|---|---|
| `NG-H001` | error | A hub interface declares `vlan`. |
| `NG-H002` | error | A hub interface declares `ipv4` or `ipv6`. |
| `NG-H003` | error | A hub declares `bridge`, `vlans` or `forwarding`. |
| `NG-H004` | error | Every hub interface is `type: ethernet`. |
| `NG-H005` | warning | Two devices attached to the same hub have addresses in different subnets — a hub is a single broadcast domain. |

### 10.8 Adapters

| ID | Sev. | Rule |
|---|---|---|
| `NG-X001` | error | `upstream.attached_to` is a bare device name (no `:`) that resolves to a declared device. |
| `NG-X002` | warning | An adapter has cabled downstream interfaces but no `attached_to`. |
| `NG-X003` | error | Every entry in an adapter's `interfaces` is `type: ethernet`, `wifi` or `lag`. |
| `NG-X004` | error | `upstream.name` does not collide with any `interfaces[].name` on the same adapter. |
| `NG-X005` | error | A `cable` references an adapter's upstream port while `attached_to` is also set — the host attachment is declared exactly once. |
| `NG-X006` | error | Adapter attachment is acyclic: an adapter chain (dock → dongle → host) must not loop. |
| `NG-X007` | warning | `attached_to` points at a `hub` or `switch`. Adapters attach to hosts; a media converter between switches should be modelled with `passthrough: false` and cables on both sides. |
| `NG-X008` | error | An adapter declares more entries in `interfaces` than `spec.ports` says the hardware has. Not checked when `ports` is absent. |

### 10.9 Rule identifiers

The semantic validator (`netgraph.validate`) reports the cross-document rules —
and the per-element judgements that a single document cannot settle — under
short ids. Each is an alias of the `NG-*` rule above it, and both spellings are
accepted wherever a rule is named — in `netgraph.toml`, in a `netgraph/ignore`
annotation, and on the command line. The letter is the severity the rule was
first assigned: `E` error, `W` warning, `I` info.

| Short id | Sev. | Schema id | Rule |
|---|---|---|---|
| `E001` | error | `NG-C002`, `NG-C003` | A cable endpoint references an unknown device or interface. |
| `E002` | error | `NG-C005` | An interface is terminated by more than one cable. |
| `E003` | error | `NG-I008` | The same MAC address is used by two interfaces. Stacked interfaces (a LAG and its members, a sub-interface and its parent) share one address by design and are exempt. |
| `E004` | error | `NG-A004` | The same IP address is assigned twice within one prefix *and* one VLAN. Re-using a prefix in a different VLAN is not a clash. |
| `E005` | error | `NG-C011` | The two ends of a link disagree about VLANs: two access ports in different VLANs, an access port facing a trunk, trunks whose VLAN sets are disjoint, or two trunks that each name a different `native_vlan`. Resolved through the LAG master (§10.6). |
| `E006` | error | `NG-X008` | An adapter declares more downstream interfaces than it has ports. |
| `W101` | warning | `NG-I013` | An interface has neither IPv4 nor IPv6 and is not a switchport. |
| `W102` | warning | `NG-C010` | The two endpoints of a cable disagree about the MTU. |
| `W103` | warning | `NG-C016` | A device terminates no cable and hosts no adapter. |
| `W104` | warning | `NG-V009` | An access port of a layer-2-only switch carries an IP address. |
| `W105` | warning | `NG-A008` | A subnet holds exactly one element, so its prefix length may be wrong or its neighbour missing. Host and point-to-point prefixes are exempt. |
| `W106` | warning | `NG-A009` | Two elements claim the same address in one subnet, in different VLANs — the clash `E004` scopes away, seen from layer 3. |
| `E007` | error | `NG-I004` | Interface stacking through `parent`/`members` contains a cycle longer than the self-reference `NG-I002`/`NG-I003` already reject. |
| `E008` | error | `NG-I005` | A `lag`/`bridge` member is claimed by a second aggregate, is an aggregate itself, or carries a `vlan` sub-interface. A `lag` inside a `bridge` is exempt: that is how a bridged bond is expressed. |
| `E009` | error | `NG-V005` | A `type: vlan` sub-interface's VID is not carried by its `parent`. A `bridge` parent is resolved through the union of its members' VLAN sets. |
| `E010` | error | `NG-I009` | A `mac` has the multicast bit set. |
| `W107` | warning | `NG-I006` | A `lag`/`bridge` member carries its own `ipv4`/`ipv6` addresses. |
| `W108` | warning | `NG-I007` | A `loopback` interface declares a `mac`. |
| `W109` | warning | `NG-I012` | A device declares no `ethernet`, `wifi` or `lag` interface. Adapters are exempt — `NG-X003` already restricts them to those types. |
| `W110` | warning | `NG-A005` | An address is the network or broadcast address of its own prefix. In IPv6 the all-zeros host part is reported as the subnet-router anycast address. |
| `W111` | warning | `NG-A006` | Two *different* interfaces of one element hold overlapping prefixes. Loopback and link-local addresses are excluded. |
| `W112` | warning | `NG-A007` | A `loopback` carries a prefix other than `/32` or `/128`. The host-scoped loopback addresses (`127.0.0.0/8`, `::1`) are exempt, so the `127.0.0.1/8` every OS configures is not reported. |
| `W113` | warning | `NG-V004` | A port references a VLAN the device's `vlans` database omits. Devices with no database, ports trunking `all`, and VLAN 1 — the 802.1Q Default VLAN — are exempt. |
| `W114` | warning | `NG-V006` | A trunk's `native_vlan` is not listed in its `trunk_vlans`. |
| `W115` | warning | `NG-V007` | A port trunking `all` is cabled to a host rather than to another switch. Resolved through the LAG master (§10.6). |
| `W116` | warning | `NG-V008` | A `lag` member declares a `vlan` block differing from its aggregate's. |
| `I001` | info | `NG-I010` | A `mac` is locally administered rather than vendor-assigned. |
| `E011` | error | `NG-C006` | `medium: wireless` requires both endpoints to be `type: wifi`; any other medium requires neither to be. An adapter's upstream port is a host bus, so it counts as wired. |
| `E012` | error | `NG-C009` | A cable endpoint is a `loopback`, `vlan` or `bridge` interface. |
| `E013` | error | `NG-X005` | A cable lands on an adapter's upstream port while `attached_to` is set as well. |
| `E014` | error | `NG-X006` | The `attached_to` references form a cycle. |
| `E015` | error | `NG-X001` | `attached_to` names no declared element, stays ambiguous, or names something that owns no interfaces. The grammar half of `NG-X001` is a schema rule (§10.8) and is not suppressible; this half is. |
| `W117` | warning | `NG-C004` | Both endpoints of one cable land on the same element. The same-*port* case is `E002`. |
| `W118` | warning | `NG-C008` | A cable's `speed` disagrees with the speed its endpoint declares — in practice an adapter's `upstream.speed` (§8.1), the only endpoint speed the schema carries. |
| `W119` | warning | `NG-C012` | A cable endpoint is a `lag` aggregate rather than one of its members. |
| `W120` | warning | `NG-C013` | `duplex: half` on a link where neither end belongs to a `hub`. |
| `W121` | warning | `NG-C014` | The topology graph is disconnected. Reported once, naming each island's smallest member. Islands of one element are left to `W103`. |
| `W122` | warning | `NG-H005` | Two elements cabled into one hub share no prefix. Chained hubs count as one collision domain; the two address families are checked separately. |
| `W123` | warning | `NG-X002` | An adapter has cabled downstream ports but neither an `attached_to` nor a cable on its upstream port. |
| `W124` | warning | `NG-X007` | `attached_to` points at a `hub` or a `switch` rather than at a host. |
| `I002` | info | `NG-C015` | An interface is `enabled: true` but terminates no cable. `lag` aggregates are exempt: `NG-C012` asks for the members to be cabled. |

Ids are permanent (§10), so a suppression written today keeps meaning the same
thing. Where a short id covers two schema ids (`E001`), naming either alias
selects the whole rule.

Three rules are graded more harshly here than in the tables above: `E003`
(`NG-I008`), `E004` (`NG-A004`) and `E010` (`NG-I009`). The first two because a
duplicate address is far more often a copy-paste mistake than a deliberate VRRP
or anycast design; the third because a multicast source address is not a design
at all. Re-grade them per inventory (§10.10) where the exception is real.

### 10.10 Suppressing a rule

Two mechanisms, both additive; a rule is silenced if either applies.

**Per inventory** — `netgraph.toml` at the inventory root:

```toml
[validate]
strict = false                    # promote surviving warnings to errors
ignore = ["W103", "NG-C010"]      # never report these at all

[validate.severity]
E004 = "warning"                  # re-grade rather than silence
```

An unknown rule id here is an error: a suppression that silently applies to
nothing is worse than a failed run. Unknown keys inside `[validate]` are
rejected for the same reason, while unknown *top-level* tables are ignored so a
file shared with a later version still loads.

**Per element** — the `netgraph/ignore` annotation (§3.1), whose value is a
list of ids separated by commas, semicolons or spaces:

```yaml
metadata:
  name: media-converter
  annotations:
    netgraph/ignore: "W101 W103"
```

A finding names every element it involves, so annotating *either* end of a
cable suppresses a finding about that cable. An unknown id in an annotation is
ignored rather than fatal — inventory data must not be able to abort a run —
and therefore simply fails to suppress anything.

---

## 11. Worked examples

Three complete, self-consistent inventories. Each one validates clean against
§10 except where a warning is called out deliberately. The paths in the tree
listings below name the example each section is written around; they are
illustrative, and the inventories that actually ship are
[`examples/home-lab/`](../examples/home-lab) (§11.1 and §11.2 combined into one
small topology) and [`examples/campus/`](../examples/campus) (§11.3 scaled up
to three sites). Both are loaded, validated and rendered by
`tests/test_examples.py`, so they cannot drift away from this specification
without failing the test suite.

### 11.1 Small office

Router on a stick: one physical trunk to an access switch, two user VLANs
terminated on sub-interfaces, a management SVI on the switch, dual-stack
addressing.

```text
examples/small-office/
├── routers/rtr-edge-01.yaml
├── switches/sw-access-01.yaml
├── hosts/pc-alice.yaml
├── hosts/srv-nas-01.yaml
└── cables/hq-links.yaml
```

**`routers/rtr-edge-01.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: router
metadata:
  name: rtr-edge-01
  description: |
    HQ edge router. Terminates the ISP hand-off and routes between the
    user (10) and IoT (20) VLANs.
  labels:
    site: hq
    role: edge
    env: prod
spec:
  vendor: Juniper
  model: SRX300
  location: HQ / G-01 / U1
  forwarding:
    ipv4: true
    ipv6: true
  interfaces:
    - name: lo0
      type: loopback
      description: Router ID and management target
      ipv4:
        addresses:
          - ip: 192.0.2.1
            prefix_length: 32
      ipv6:
        addresses:
          - ip: 2001:db8::1
            prefix_length: 128

    - name: ge-0/0/0
      type: ethernet
      description: ISP hand-off
      mac: 00:05:86:00:00:00
      mtu: 1500
      ipv4:
        addresses:
          - ip: 198.51.100.2
            prefix_length: 30

    - name: ge-0/0/1
      type: ethernet
      description: Trunk to sw-access-01
      mac: 00:05:86:00:00:01
      mtu: 1500
      vlan:
        mode: trunk
        trunk_vlans: [1, 10, 20, 99]
        native_vlan: 1

    - name: ge-0/0/1.10
      type: vlan
      parent: ge-0/0/1
      description: Users gateway
      vlan:
        mode: access
        access_vlan: 10
      ipv4:
        addresses: [10.10.10.1/24]
      ipv6:
        addresses: [2001:db8:10::1/64]

    - name: ge-0/0/1.20
      type: vlan
      parent: ge-0/0/1
      description: IoT gateway
      vlan:
        mode: access
        access_vlan: 20
      ipv4:
        addresses: [10.10.20.1/24]
```

`ge-0/0/1` carries no addresses: it is a pure layer-2 trunk, and the layer-3
configuration lives on the two `vlan` sub-interfaces. Both sub-interface VIDs
appear in the parent's `trunk_vlans`, satisfying `NG-V005`.

**`switches/sw-access-01.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-access-01
  description: Access switch, HQ ground floor
  labels:
    site: hq
    rack: g-01
    role: access
spec:
  vendor: Cisco
  model: C9200-24P
  location: HQ / G-01 / U2
  bridge:
    name: br0
    type: customer-vlan-bridge
    address: 00:1b:0d:63:c2:00
  vlans:
    - id: 1
      name: default
    - id: 10
      name: users
    - id: 20
      name: iot
    - id: 99
      name: mgmt
  interfaces:
    - name: br0
      type: bridge
      description: Switching instance
      members:
        - GigabitEthernet0/1
        - GigabitEthernet0/2
        - GigabitEthernet0/3
        - GigabitEthernet0/4

    - name: Vlan99
      type: vlan
      parent: br0
      description: In-band management
      vlan:
        mode: access
        access_vlan: 99
      ipv4:
        addresses: [10.10.99.2/24]

    - name: GigabitEthernet0/1
      type: ethernet
      description: Uplink to rtr-edge-01
      mtu: 1500
      vlan:
        mode: trunk
        trunk_vlans: [1, 10, 20, 99]
        native_vlan: 1

    - name: GigabitEthernet0/2
      type: ethernet
      description: Desk 1 - alice
      mtu: 1500
      vlan:
        mode: access
        access_vlan: 10

    - name: GigabitEthernet0/3
      type: ethernet
      description: IoT patch, currently unused
      enabled: false
      vlan:
        mode: access
        access_vlan: 20

    - name: GigabitEthernet0/4
      type: ethernet
      description: NAS
      mtu: 1500
      vlan:
        mode: access
        access_vlan: 10
```

`Vlan99` is a `vlan` sub-interface of the `bridge` interface rather than of a
physical port — that is how a switch virtual interface is expressed here. Note
that `NG-V005` is satisfied because the bridge's member port `Gi0/1` trunks
VLAN 99; `br0` itself carries no `vlan` block, and the validator resolves a
bridge parent by taking the union of its members' VLAN sets.

**`hosts/pc-alice.yaml`** and **`hosts/srv-nas-01.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-alice
  description: Alice's workstation
  labels: {site: hq, role: workstation, owner: alice}
spec:
  vendor: Dell
  model: OptiPlex 7010
  interfaces:
    - name: lo
      type: loopback
      ipv4:
        addresses: [127.0.0.1/8]
      ipv6:
        addresses: ["::1/128"]
    - name: eno1
      type: ethernet
      mac: 3c:97:0e:11:22:33
      mtu: 1500
      ipv4:
        addresses: [10.10.10.50/24]
      ipv6:
        addresses: [2001:db8:10::50/64]
    - name: wlp2s0
      type: wifi
      mac: 3c:97:0e:11:22:34
      enabled: false
      description: Disabled by policy while docked
```

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: server
metadata:
  name: srv-nas-01
  description: Backup and file server
  labels: {site: hq, role: storage, env: prod}
spec:
  vendor: Synology
  model: DS923+
  location: HQ / G-01 / U4
  interfaces:
    - name: lo
      type: loopback
      ipv4:
        addresses: [127.0.0.1/8]
    - name: eth0
      type: ethernet
      mac: 00:11:32:aa:bb:cc
      mtu: 1500
      ipv4:
        addresses: [10.10.10.10/24]
      ipv6:
        addresses: [2001:db8:10::10/64]
```

**`cables/hq-links.yaml`** — one file, three documents:

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: cbl-rtr01-sw01
  labels: {site: hq}
spec:
  endpoints:
    - rtr-edge-01:ge-0/0/1
    - sw-access-01:GigabitEthernet0/1
  medium: copper
  speed: 1Gbps
  category: cat6
  connector: rj45
  length_m: 1.5
  label: A-014
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: cbl-sw01-alice
spec:
  endpoints:
    - sw-access-01:GigabitEthernet0/2
    - pc-alice:eno1
  medium: copper
  speed: 1Gbps
  category: cat6
  length_m: 12
  label: B-002
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: cbl-sw01-nas
spec:
  endpoints:
    - sw-access-01:GigabitEthernet0/4
    - srv-nas-01:eth0
  medium: copper
  speed: 1Gbps
  category: cat6a
  length_m: 2
  label: A-021
```

The hosts declare no `vlan` block while the switch ports they face are
`access` ports. That is correct and does not trigger `NG-C011`: an untagged
host on an access port is the expected pairing, and the host inherits the
port's VLAN.

Two ports terminate no cable, and the difference between them is the whole of
`NG-C015` (info). `Gi0/3` says `enabled: false`, which documents the spare
patch and silences the rule at the same time. `ge-0/0/0` is up and faces an ISP
that is not an element of this inventory, so it *is* reported — annotate the
router with `netgraph/ignore: "NG-C015"` to say that the far end lives outside
the tree on purpose.

### 11.2 Lab bench: adapter, hub and a wireless link

A laptop with no built-in Ethernet reaches a legacy lab segment through a
USB-to-Ethernet dongle and a 100 Mbit hub, and reaches the lab access point
over Wi-Fi. This example exercises `adapter`, `hub`, `medium: wireless` and
`duplex: half`.

```text
examples/lab-bench/
├── bench.yaml          # laptop, adapter, hub, legacy PC, access point
└── cables.yaml
```

**`bench.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: laptop-01
  description: Bench laptop; no built-in Ethernet
  labels: {site: lab, role: bench, owner: bob}
spec:
  vendor: Lenovo
  model: X1 Carbon Gen 11
  interfaces:
    - name: lo
      type: loopback
      ipv4:
        addresses: [127.0.0.1/8]
    - name: wlp0s20f3
      type: wifi
      mac: 8c:8c:aa:00:11:22
      mtu: 1500
      ipv4:
        addresses: [192.168.50.60/24]
      ipv6:
        addresses: [2001:db8:50::60/64]
---
apiVersion: netgraph.dev/v1alpha1
kind: adapter
metadata:
  name: adp-usb-eth-01
  description: USB 3.0 gigabit dongle, lives in the bench drawer
  labels: {site: lab, asset: "A-4471"}
spec:
  vendor: Anker
  model: A83130A1
  serial: "AK2231007744"
  form_factor: usb-ethernet
  passthrough: true
  upstream:
    name: usb0
    type: usb
    speed: 5Gbps
    attached_to: laptop-01
  interfaces:
    - name: enx001122334455
      type: ethernet
      description: Dongle Ethernet port
      mac: 00:11:22:33:44:55
      mtu: 1500
      ipv4:
        addresses: [192.168.50.61/24]
---
apiVersion: netgraph.dev/v1alpha1
kind: hub
metadata:
  name: hub-lab-01
  description: 4-port 100BASE-TX repeater; single collision domain
  labels: {site: lab}
spec:
  vendor: Netgear
  model: EN104TP
  location: Lab / bench 3
  interfaces:
    - name: p1
      type: ethernet
      description: Dongle
    - name: p2
      type: ethernet
      description: Legacy PC
    - name: p3
      type: ethernet
      description: Access point uplink
    - name: p4
      type: ethernet
      description: Spare
      enabled: false
---
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-legacy-01
  description: DOS test machine for the serial rig
  labels: {site: lab, role: test}
spec:
  vendor: IBM
  model: PC 300GL
  interfaces:
    - name: eth0
      type: ethernet
      mac: 00:04:ac:de:ad:01
      mtu: 1500
      ipv4:
        addresses:
          - ip: 192.168.50.20
            netmask: 255.255.255.0
---
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: ap-lab-01
  description: Lab access point, bridging Wi-Fi onto the bench segment
  labels: {site: lab, role: wireless}
spec:
  vendor: Ubiquiti
  model: U6-Lite
  bridge:
    name: br0
    type: mac-bridge
  interfaces:
    - name: br0
      type: bridge
      members: [wlan0, eth0]
    - name: wlan0
      type: wifi
      description: SSID lab-bench
      mac: 78:45:58:00:0a:01
      mtu: 1500
    - name: eth0
      type: ethernet
      description: Uplink to hub-lab-01
      mac: 78:45:58:00:0a:02
      mtu: 1500
```

The hub declares no `vlan`, no addresses and no `bridge`/`vlans` block — those
are errors on a hub (`NG-H001`–`NG-H003`). The access point is modelled as a
`switch` with a `mac-bridge` (802.1D) component: it forwards frames but is not
VLAN-aware, which is exactly what `mac-bridge` means. `pc-legacy-01` uses the
`netmask` form; the loader normalises `255.255.255.0` to `prefix_length: 24`.

**`cables.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-dongle-hub}
spec:
  endpoints: [adp-usb-eth-01:enx001122334455, hub-lab-01:p1]
  medium: copper
  speed: 100Mbps
  category: cat5e
  length_m: 1
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-legacy-hub}
spec:
  endpoints: [pc-legacy-01:eth0, hub-lab-01:p2]
  medium: copper
  speed: 10Mbps
  duplex: half
  category: cat5e
  length_m: 3
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-ap-hub}
spec:
  endpoints: [ap-lab-01:eth0, hub-lab-01:p3]
  medium: copper
  speed: 100Mbps
  category: cat5e
  length_m: 2
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: assoc-laptop-ap
  description: 802.11ax association, 5 GHz
spec:
  endpoints: [laptop-01:wlp0s20f3, ap-lab-01:wlan0]
  medium: wireless
  speed: 866Mbps
```

Points worth noting:

* There is **no** cable between `laptop-01` and `adp-usb-eth-01`. The host
  attachment comes from `upstream.attached_to`; adding a cable as well would be
  `NG-X005`.
* `duplex: half` on `cbl-legacy-hub` does not warn (`NG-C013`) because one
  endpoint is a hub.
* Everything on the hub is in `192.168.50.0/24`, satisfying `NG-H005`.
* With `passthrough: true` the default rendering draws
  `laptop-01 —(usb)— hub-lab-01` with the dongle folded into the edge label;
  `netgraph render --no-collapse-adapters` draws `adp-usb-eth-01` as its own
  node. Connectivity is identical either way.
* The graph is connected: the laptop reaches the bench segment twice, once over
  Wi-Fi via `ap-lab-01` and once over USB via the hub, so `NG-C014` stays quiet.

### 11.3 Data-centre rack: dual-homed server, LAG, routed fibre uplinks

One spine router, a pair of top-of-rack switches with a peer link, a
dual-homed server bonding two 10G ports into a trunked LAG with per-VLAN
sub-interfaces, a single-homed application server, and an out-of-band IPMI
port. Exercises `lag`, `vlan` sub-interfaces, jumbo frames, `/31` and `/127`
point-to-point links, fibre and DAC media.

```text
examples/dc-rack/
├── fabric/rtr-spine-01.yaml
├── fabric/sw-tor-a.yaml
├── fabric/sw-tor-b.yaml
├── compute/srv-db-01.yaml
├── compute/srv-app-01.yaml
└── cables.yaml
```

**`fabric/rtr-spine-01.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: router
metadata:
  name: rtr-spine-01
  description: Spine / rack gateway
  labels: {site: dc1, rack: r07, role: spine, env: prod}
spec:
  vendor: Arista
  model: DCS-7280SR
  location: DC1 / R07 / U40
  forwarding: {ipv4: true, ipv6: true}
  interfaces:
    - name: Loopback0
      type: loopback
      description: Router ID / BGP source
      ipv4:
        addresses: [198.51.100.1/32]
      ipv6:
        addresses: [2001:db8::1/128]

    - name: Ethernet1
      type: ethernet
      description: To sw-tor-a
      mac: 00:1c:73:00:07:01
      mtu: 9214
      ipv4:
        addresses: [10.255.0.0/31]
      ipv6:
        addresses: [2001:db8:ffff::/127]

    - name: Ethernet2
      type: ethernet
      description: To sw-tor-b
      mac: 00:1c:73:00:07:02
      mtu: 9214
      ipv4:
        addresses: [10.255.0.2/31]
      ipv6:
        addresses: [2001:db8:ffff::2/127]
```

**`fabric/sw-tor-a.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-tor-a
  description: Top-of-rack A, R07
  labels: {site: dc1, rack: r07, role: tor, env: prod}
spec:
  vendor: Arista
  model: DCS-7050SX3
  location: DC1 / R07 / U41
  bridge:
    name: br0
    type: customer-vlan-bridge
    address: 00:1c:73:0a:00:00
  vlans:
    - id: 1
      name: default
    - id: 30
      name: app
    - id: 40
      name: db
    - id: 99
      name: ipmi
  interfaces:
    - name: Ethernet49
      type: ethernet
      description: Routed uplink to rtr-spine-01
      mac: 00:1c:73:0a:00:31
      mtu: 9214
      ipv4:
        addresses: [10.255.0.1/31]
      ipv6:
        addresses: ["2001:db8:ffff::1/127"]

    - name: Ethernet50
      type: ethernet
      description: Peer link to sw-tor-b
      mac: 00:1c:73:0a:00:32
      mtu: 9214
      vlan:
        mode: trunk
        trunk_vlans: all
        native_vlan: 1

    - name: Ethernet11
      type: ethernet
      description: srv-db-01 bond member A
      mtu: 9214
      vlan:
        mode: trunk
        trunk_vlans: [30, 40]

    - name: Ethernet12
      type: ethernet
      description: srv-app-01
      mtu: 9214
      vlan:
        mode: access
        access_vlan: 30

    - name: Management1
      type: ethernet
      description: srv-db-01 IPMI
      mtu: 1500
      vlan:
        mode: access
        access_vlan: 99
```

`Ethernet49` is a routed port: it carries addresses and **no** `vlan` block, so
it never becomes a `dot1q:bridge-port`. `Ethernet11` is a trunk with no
`native_vlan`, which derives `dot1q:acceptable-frame =
admit-only-VLAN-tagged-frames` (§9.3) — the server tags every frame.
`trunk_vlans: all` on the peer link expands to `1-4094`.

**`fabric/sw-tor-b.yaml`** — the B-side twin. It differs from `sw-tor-a` only
in its name, MAC/IP suffixes and the absence of the `srv-app-01` and IPMI
ports.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-tor-b
  description: Top-of-rack B, R07
  labels: {site: dc1, rack: r07, role: tor, env: prod}
spec:
  vendor: Arista
  model: DCS-7050SX3
  location: DC1 / R07 / U42
  bridge:
    name: br0
    type: customer-vlan-bridge
    address: 00:1c:73:0b:00:00
  vlans:
    - id: 1
      name: default
    - id: 30
      name: app
    - id: 40
      name: db
    - id: 99
      name: ipmi
  interfaces:
    - name: Ethernet49
      type: ethernet
      description: Routed uplink to rtr-spine-01
      mac: 00:1c:73:0b:00:31
      mtu: 9214
      ipv4:
        addresses: [10.255.0.3/31]
      ipv6:
        addresses: ["2001:db8:ffff::3/127"]

    - name: Ethernet50
      type: ethernet
      description: Peer link to sw-tor-a
      mac: 00:1c:73:0b:00:32
      mtu: 9214
      vlan:
        mode: trunk
        trunk_vlans: all
        native_vlan: 1

    - name: Ethernet11
      type: ethernet
      description: srv-db-01 bond member B
      mtu: 9214
      vlan:
        mode: trunk
        trunk_vlans: [30, 40]
```

**`compute/srv-db-01.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: server
metadata:
  name: srv-db-01
  description: PostgreSQL primary, dual-homed to both ToRs
  labels: {site: dc1, rack: r07, role: database, env: prod}
spec:
  vendor: Dell
  model: PowerEdge R650
  serial: "JH4K2N3"
  location: DC1 / R07 / U12
  interfaces:
    - name: lo
      type: loopback
      ipv4:
        addresses: [127.0.0.1/8]
      ipv6:
        addresses: ["::1/128"]

    - name: eno1
      type: ethernet
      description: bond0 member, to sw-tor-a
      mac: b4:96:91:00:0d:01
      mtu: 9000

    - name: eno2
      type: ethernet
      description: bond0 member, to sw-tor-b
      mac: b4:96:91:00:0d:02
      mtu: 9000

    - name: bond0
      type: lag
      description: LACP 802.3ad across both ToRs
      members: [eno1, eno2]
      mac: b4:96:91:00:0d:01
      mtu: 9000
      vlan:
        mode: trunk
        trunk_vlans: [30, 40]

    - name: bond0.30
      type: vlan
      parent: bond0
      description: Application network
      mtu: 9000
      vlan:
        mode: access
        access_vlan: 30
      ipv4:
        addresses: [10.30.0.11/24]
      ipv6:
        addresses: [2001:db8:30::11/64]

    - name: bond0.40
      type: vlan
      parent: bond0
      description: Storage/replication network
      mtu: 9000
      vlan:
        mode: access
        access_vlan: 40
      ipv4:
        addresses: [10.40.0.11/24]

    - name: ipmi0
      type: ethernet
      description: Out-of-band BMC
      mac: b4:96:91:00:0d:03
      mtu: 1500
      ipv4:
        addresses: [10.99.0.11/24]
```

`bond0` deliberately repeats `eno1`'s MAC — that is what Linux bonding does.
`NG-I008` (duplicate MAC) exempts a member/aggregate pair, so this is silent.
The bond members carry no addresses and no `vlan` block of their own; VLAN and
MTU checks on their cables resolve through `bond0` (§10.6).

**`compute/srv-app-01.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: server
metadata:
  name: srv-app-01
  description: Stateless application node
  labels: {site: dc1, rack: r07, role: app, env: prod}
spec:
  vendor: Dell
  model: PowerEdge R450
  location: DC1 / R07 / U14
  interfaces:
    - name: lo
      type: loopback
      ipv4:
        addresses: [127.0.0.1/8]
    - name: eno1
      type: ethernet
      mac: b4:96:91:00:0e:01
      mtu: 9000
      ipv4:
        addresses: [10.30.0.12/24]
      ipv6:
        addresses: [2001:db8:30::12/64]
```

**`cables.yaml`**

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-spine-tora, labels: {site: dc1, rack: r07}}
spec:
  endpoints: [rtr-spine-01:Ethernet1, sw-tor-a:Ethernet49]
  medium: fiber
  speed: 10Gbps
  category: om4
  connector: lc
  length_m: 5
  label: R07-F-001
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-spine-torb, labels: {site: dc1, rack: r07}}
spec:
  endpoints: [rtr-spine-01:Ethernet2, sw-tor-b:Ethernet49]
  medium: fiber
  speed: 10Gbps
  category: om4
  connector: lc
  length_m: 5
  label: R07-F-002
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-tora-torb-peer, description: MLAG peer link}
spec:
  endpoints: [sw-tor-a:Ethernet50, sw-tor-b:Ethernet50]
  medium: fiber
  speed: 10Gbps
  category: om4
  connector: lc
  length_m: 1
  label: R07-F-003
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-tora-db01}
spec:
  endpoints: [sw-tor-a:Ethernet11, srv-db-01:eno1]
  medium: copper
  speed: 10Gbps
  category: dac
  connector: sfp+
  length_m: 2
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-torb-db01}
spec:
  endpoints: [sw-tor-b:Ethernet11, srv-db-01:eno2]
  medium: copper
  speed: 10Gbps
  category: dac
  connector: sfp+
  length_m: 2
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-tora-app01}
spec:
  endpoints: [sw-tor-a:Ethernet12, srv-app-01:eno1]
  medium: copper
  speed: 10Gbps
  category: dac
  connector: sfp+
  length_m: 2
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-tora-db01-ipmi}
spec:
  endpoints: [sw-tor-a:Management1, srv-db-01:ipmi0]
  medium: copper
  speed: 1Gbps
  category: cat6
  connector: rj45
  length_m: 3
```

Known limitations this example makes visible:

* **MTU.** The switches use 9214 (Arista's layer-2 maximum) throughout while
  the servers use 9000. The spine↔ToR links agree at 9214, so `NG-C010` stays
  quiet there; the three server-facing links do not agree, so `NG-C010` warns
  on `cbl-tora-db01`, `cbl-torb-db01` and `cbl-tora-app01`. The example keeps
  the mismatch on purpose: this is the common real-world state, and surfacing
  it is the point. Setting the ToR server ports to 9000 silences the warning.
* **MLAG.** `bond0` spans two physically separate switches. The schema records
  the cabling faithfully but has no vocabulary for the MLAG relationship
  between `sw-tor-a` and `sw-tor-b`; the peer link's `description` and a
  `role: tor` label are the only hints. Modelling multi-chassis aggregation is
  deferred (§12).
* `NG-C012` does not fire: the cables terminate on `eno1`/`eno2`, the physical
  members, not on `bond0`.

---

## 12. Compatibility policy

`apiVersion` is `«group»/«version»`. The group is `netgraph.dev`; the version
follows the Kubernetes convention: `v1alpha1` may change incompatibly between
minor releases, `v1beta1` only between majors, `v1` never.

Within one `apiVersion`:

* Adding an **optional** field with a backward-compatible default is a
  non-breaking change and MAY happen in a patch release.
* Adding a value to an enum, or adding a new `kind`, is non-breaking for
  readers of older documents but makes newer documents unreadable by older
  netgraph versions. It requires a minor release.
* Renaming or removing a field, changing a default, tightening a constraint, or
  changing a rule's severity from `warning` to `error` is breaking and requires
  a version bump.
* Validation-rule IDs (§10) are permanent. A retired rule's ID is tombstoned,
  never reused.

The loader accepts documents whose `apiVersion` it recognises and rejects
everything else with `NG-D002` rather than guessing. When a second version
exists, documents of different versions may coexist in one inventory; the
loader converts older documents to the current internal model on read.

### 12.1 Deferred to a later revision

Deliberately out of scope for `v1alpha1`, listed so that nobody designs around
their absence:

* **Routing**: static routes, BGP/OSPF adjacencies, VRFs.
  (`ietf-routing`, RFC 8349.)
* **Spanning tree**: STP/RSTP/MSTP roles and per-port cost.
* **Multi-chassis aggregation**: MLAG/vPC/stacking relationships between
  switch elements.
* **Host-side expansion ports**: a `usb`/`thunderbolt` interface type on
  devices, which would let `upstream.attached_to` name a specific receptacle
  (§8.1).
* **Wireless detail**: SSIDs, bands, channels, and BSS-to-SSID mapping.
* **Per-inventory configuration** beyond validation: `netgraph.toml` at the
  inventory root already carries rule suppression and severity overrides
  (§10.10); default labels and renderer settings are still deferred.
* **Templating**: reusable device profiles (`kind: profile`) to remove
  repetition across identically-configured switches.

---

## 13. Editor integration

Everything above describes what a document may contain. A JSON Schema says the
same thing in a form an editor can act on, so a misspelt key is underlined as
you type it rather than discovered by the next `netgraph validate`.

```console
$ netgraph schema > netgraph.schema.json          # every kind, in one schema
$ netgraph schema --kind cable                    # just one kind
$ netgraph schema -o schema/netgraph.schema.json
```

The output is [JSON Schema 2020-12](https://json-schema.org/draft/2020-12/release-notes),
generated from the same pydantic models the loader uses. `--all` is the default
and produces a union discriminated on `kind`, so one schema covers every file
in a tree; `-k`/`--kind` narrows it to a single kind when a directory holds only
one. A generated copy is committed at
[`schema/netgraph.schema.json`](../schema/netgraph.schema.json) and refreshed by
`python tools/gen_json_schema.py`; the test suite fails when it drifts from the
models.

### 13.1 What the schema checks

| | Schema | `netgraph validate` |
|---|---|---|
| Unknown or misspelt keys | yes (`NG-D005`) | yes |
| Required keys, enum values, numeric ranges | yes | yes |
| Value grammars: MAC, bit rate, VLAN set, CIDR, names | yes | yes |
| Rules within one object: `native_vlan` on an access port, `members` on an `ethernet` port | yes | yes |
| Anything that needs a second document: a cable endpoint resolving, name uniqueness, an address matching its subnet | **no** | yes |

The schema is the fast, local half. It is not a substitute for
`netgraph validate`, and CI should keep running the latter.

### 13.2 Per-file modeline

`netgraph init` writes both halves of this section into a new inventory — the
schema next to the tree, and the modeline below on every document it
generates — so a scaffolded tree is wired up before it is first opened. What
follows is for a tree that already exists.

The yaml-language-server reads a comment on the first line. It needs no editor
configuration at all, which makes it the right choice for a small tree or for a
file you want to be self-describing:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/netgraph/netgraph/main/schema/netgraph.schema.json
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-office
```

A relative path works too, and keeps the tree usable offline:

```yaml
# yaml-language-server: $schema=../../schema/netgraph.schema.json
```

### 13.3 VS Code

Adding one entry to `.vscode/settings.json` covers a whole inventory without
touching the files. The [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)
provides the language server; the glob is matched against the workspace-relative
path:

```json
{
  "yaml.schemas": {
    "./schema/netgraph.schema.json": [
      "inventory/**/*.yaml",
      "examples/**/*.yaml"
    ]
  },
  "yaml.customTags": [],
  "yaml.validate": true
}
```

Neovim (`nvim-lspconfig` with `yamlls`) and the JetBrains IDEs take the same
`yaml.schemas` mapping; JetBrains also accepts the schema under
*Settings → Languages & Frameworks → Schemas and DTDs → JSON Schema Mappings*.

When a directory holds one kind, generate that kind's schema and narrow the
mapping to it — completion then offers only the fields that belong there, and a
`kind: switch` document in `cables/` is an error rather than a valid file in the
wrong place:

```console
$ netgraph schema -k cable -o schema/netgraph-cable.schema.json
```

```json
{
  "yaml.schemas": {
    "./schema/netgraph-cable.schema.json": ["inventory/**/cables/*.yaml"]
  }
}
```

### 13.4 Versioning

The schema is versioned with the documents it describes. Its `$id` carries the
`apiVersion` it belongs to:

```
https://netgraph.dev/schema/v1alpha1/element.json
https://netgraph.dev/schema/v1alpha1/cable.json
```

A future `v1beta1` gets its own `$id` alongside this one rather than replacing
it, so a tree pinned to `netgraph.dev/v1alpha1` keeps validating against the
schema that matches it. §12's compatibility rules apply to the schema exactly as
they apply to the format: within one `apiVersion` the schema only ever grows
optional fields.

### 13.5 A caveat about YAML

The schema constrains the *data* a YAML document parses to, so anything the
parser decides before the schema sees it is out of reach. The one that bites in
practice is an unquoted MAC address: a YAML 1.1 parser reads `12:34:56:12:34:56`
as a sexagesimal integer, and by the time either the schema or the loader looks
at it the original digits are gone. Quote MAC addresses (§5). netgraph's own
loader detects the case and says so; an editor will simply report that a number
is not a string.
