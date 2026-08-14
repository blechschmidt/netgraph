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
14. [Tunnels](#14-tunnels)
15. [Patch panels](#15-patch-panels)
16. [Routing](#16-routing)
17. [Power](#17-power)

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

Provenance is tracked **per field**, not merely per document, and it survives
the two rewrites the loader performs before validation: interface range
expansion (§6.2.5) and template merging (§6.6). A value a template supplied is
reported against the template's file and line, with a note naming the device
that inherited it; a value the device overrode is reported against the device.
`netgraph show --raw` prints a document as written, unexpanded and unmerged,
next to the resolved output the same command prints without it.

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
| `kind` | enum | M | — | One of `switch`, `router`, `hub`, `computer`, `server`, `cable`, `adapter`, `tunnel`, `patchpanel`, `pdu`, `template`. Lower-case; other spellings are rejected. |
| `metadata` | mapping | M | — | §3.1 |
| `spec` | mapping | M | — | Shape depends on `kind`: §6 (devices), §7 (cable), §8 (adapter), §14 (tunnel), §15 (patchpanel), §17.1 (pdu), §6.6 (template). |

The first ten kinds are **elements**: each becomes a node or an edge of the
graph. `template` is the eleventh kind and is not an element — it declares a
reusable partial device `spec` and is merged away by the loader (§6.6).

### 3.1 `metadata`

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `name` | name | M | — | Unique within its namespace (§2.2, `NG-N002`). Grammar in §4.1. |
| `description` | string | O | `null` | Free text, may be multi-line. Rendered as a node tooltip. |
| `location` | mapping | O | `null` | Where the hardware physically is: §3.2. |
| `labels` | map[string, string] | O | `{}` | Selector-friendly key/value pairs. Keys match `[a-z0-9]([-a-z0-9_.]*[a-z0-9])?` (≤63 chars) and MAY carry a DNS-style prefix (`example.com/tier`). Values ≤253 chars. The prefix `netgraph.dev/` is reserved for tool-generated labels. |
| `annotations` | map[string, string] | O | `{}` | Per-element input to the tooling. Same key grammar as `labels`, but the `netgraph.dev/` prefix is permitted (annotations exist to carry tool keys) and values may be up to 4096 chars. Annotations are **not** selectable and never affect the graph. |

Labels drive filtering (`netgraph render --select site=hq`) and grouping
(`--group-by rack`), so prefer a small, consistent key set: `site`, `rack`,
`role`, `env`, `owner`.

Annotations are the opposite: they are read by the tool, not by the user. The
one this revision defines is `netgraph/ignore`, which suppresses validation
rules on the element carrying it (§10.11):

```yaml
metadata:
  name: spare-switch
  annotations:
    netgraph/ignore: "W103, E004"   # or "*" for every rule
```

---

### 3.2 `metadata.location`

Where the hardware is. Optional, and available on every kind, because a patch
panel is racked exactly as a server is.

```yaml
metadata:
  name: srv-app-01
  location:
    site: hq
    room: mdf
    rack: r1
    position: 10        # lowest rack unit occupied
    height: 2           # rack units, upwards from `position`
    rack_height: 42     # how tall the cabinet is
```

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `site` | string | O | `null` | Free text. |
| `room` | string | O | `null` | Room or floor within the site. Free text. |
| `rack` | string | O | `null` | Rack identifier, unique within its room. Naming one is what puts the element on an elevation; without it the block is documentation only. |
| `position` | integer | O | `null` | The **lowest** rack unit the element occupies, counted from 1 at the bottom. Requires `rack` (`NG-U004`). 1–100. |
| `height` | integer | O | `1` | How many units it occupies, upwards from `position`. 1–100. |
| `rack_height` | integer | O | `null` | How tall the rack is. Any element in it may declare this; two that disagree are `NG-U003`. Requires `rack` (`NG-U004`). 1–100. |

#### Semantics

* A rack is identified by `(site, room, rack)` together, so two elements are in
  the same cabinet only when all three agree. An unset `site` or `room` is the
  empty string, never a wildcard: an inventory that gives one element a full
  address and another only a rack name has not said the two are in one place.
* `position` is the lowest unit and `height` counts *up*, so a 2U server at
  `position: 10` fills U10 and U11. Two elements whose spans intersect are
  `NG-U001`; an element whose top exceeds `rack_height` is `NG-U002`.
* An element that names a `rack` but no `position` is in the room and nowhere in
  particular. It is not drawn on the elevation, and it collides with nothing.
* `netgraph render --layer rack` draws one front elevation per rack, with empty
  units shown. Free-text `spec.location` (§6.1) is unaffected and stays a label.

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
| `ssid` | Network name: 1 to 32 **octets**, not characters. Any byte sequence; stored exactly as written. | `dot11:ssid` |
| `dbm` | Radiated power in dBm, -30 to 40. Integers are accepted and widened. | — |
| `speed` | Bit rate. Either an integer in bit/s, or `<number><unit>` with unit `bps`, `kbps`, `Mbps`, `Gbps`, `Tbps` (decimal multiples: 1 Gbps = 1 000 000 000 bit/s). Normalised to `uint64` bit/s; rendered back in the largest exact unit. | `yang:gauge64` (`if:speed`) |
| `length` | Non-negative number of metres (`length_m`). | — |
| `watts` | Electrical power: a draw, a rating, a PoE reservation (§17). Strictly positive and at most 1 000 000; integers are accepted and widened, because `draw_watts: 120` is how a nameplate is written. `0 W` is refused — that is the absence of a load, not a load. | — (`eoPower`, RFC 7460, is the MIB counterpart) |

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
| `interfaces` | list[Interface] | C | — | §6.2. MUST contain at least one entry. Required unless `from` supplies them. |
| `from` | element-ref | O | — | §6.6. Names a `kind: template` document whose partial spec is merged underneath this one. |
| `bridge` | Bridge | O | `null` | §6.3. Permitted on `switch`, `router`, `computer`, `server`. |
| `vlans` | list[VlanDef] | O | `[]` | §6.4. VLAN database. Same permission set as `bridge`. |
| `forwarding` | mapping | O | see §6.1.1 | `{ipv4: boolean, ipv6: boolean}`. |
| `power` | PowerConfig | O | `null` | §17.2. What the device draws, which PDU outlets feed it, and how much PoE it hands out. |

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
| `name` | ifname | C | — | Unique per device (`NG-I001`). Exactly one of `name` and `range` is written. |
| `range` | range | C | — | §6.2.5. Declares many interfaces at once; the entry is replaced by its expansion before validation. |
| `type` | enum | M | — | §6.2.1. Optional only in an entry that overrides a template's interface of the same name (§6.6). |
| `description` | string | O | `null` | → `if:description`. |
| `enabled` | boolean | O | `true` | Intended admin state → `if:enabled`. |
| `mac` | mac | O | `null` | → `if:phys-address` (`config false` in RFC 8343; see §9.1). |
| `mtu` | mtu | O | `null` | Layer-2 MTU. See §6.2.2. |
| `ipv4` | AddressFamily | O | `null` | §6.2.3. |
| `ipv6` | AddressFamily | O | `null` | §6.2.3. |
| `vlan` | Vlan | O | `null` | §6.2.4. |
| `wireless` | Wireless | O | `null` | §6.2.6. `type: wifi` only (`NG-W002`). |
| `poe` | PoeConfig | O | `null` | §17.3. This port hands power down the cable. `type: ethernet` or `lag` only (`NG-E006`). |
| `parent` | ifname | C | — | Required for `type: vlan`, optional for `type: tunnel`, MUST NOT appear otherwise. → `if:lower-layer-if`. |
| `members` | list[ifname] | C | — | Required for `type: lag` and `type: bridge`; MUST NOT appear otherwise. → `if:lower-layer-if`. |

#### 6.2.1 Interface `type`

The four **core** types are mandatory for every implementation:

| `type` | iana-if-type identity | Meaning |
|---|---|---|
| `ethernet` | `ianaift:ethernetCsmacd` | Any IEEE 802.3 port, copper or fibre. |
| `wifi` | `ianaift:ieee80211` | IEEE 802.11 radio. |
| `loopback` | `ianaift:softwareLoopback` | Host loopback or router loopback. |
| `bridge` | `ianaift:bridge` | Software bridge / switch SVI parent. Takes `members`. |

Three **extension** types complete the model for the common cases that would
otherwise be inexpressible (sub-interfaces, link aggregation and overlays):

| `type` | iana-if-type identity | Meaning |
|---|---|---|
| `vlan` | `ianaift:l2vlan` | 802.1Q sub-interface. Requires `parent` and `vlan.access_vlan` (the encapsulation VID). |
| `lag` | `ianaift:ieee8023adLag` | Aggregated link. Requires `members`. |
| `tunnel` | `ianaift:tunnel` | The local end of a `tunnel` document (§14): `wg0`, `ipsec0`, `vxlan100`. Holds the *overlay* configuration — the addresses inside the tunnel — while the tunnel document holds the encapsulation. `parent` optionally names the underlay port. |

Only `ethernet`, `wifi` and `lag` can terminate a cable (`NG-C009`); only
`tunnel` can terminate a tunnel (`NG-T003`).

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
| `gateway` | ipv4-address / ipv6-address | O | *unset* | First hop for off-link traffic, written **without** a prefix length. Must lie inside one of this interface's own prefixes (`NG-A013`). |

`gateway` is the one field of these containers that RFC 8344 does not define: a
default route lives in `ietf-routing`
(`rt:routing/…/static-routes/…/next-hop-address`), not in `ietf-ip`. netgraph
keeps it on the interface anyway, because that is where an operator writes it
and where the only check worth making — is the first hop on-link? — can be
made. An IPv6 link-local gateway such as `fe80::1` is exempt from that check.

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

#### 6.2.5 `range` — declaring many interfaces at once

A 48-port access switch is 48 near-identical `interfaces` entries. Writing them
out by hand is the single largest obstacle to describing a real access layer, so
an entry MAY declare `range` instead of `name`:

```yaml
interfaces:
  - range: GigabitEthernet1/0/[1-48]
    type: ethernet
    description: Access port {}
    enabled: false
    mtu: 1500
    vlan: {mode: access, access_vlan: 10}
```

The entry above **is** forty-eight entries. Expansion happens in the loader,
immediately after the document is parsed and before any model validation, so
everything downstream — `netgraph validate`, the graph, every renderer,
`netgraph show`, an editor driven by the JSON Schema — sees an ordinary list of
interfaces and needs no notion of a range at all. A range never appears in
rendered output, and `netgraph show` without `--raw` prints the expansion.

**Grammar.** A range is a string of interface-name characters (§4.1) with one or
more spans `[low-high]` embedded in it. Both bounds are decimal, inclusive, and
`low` MUST NOT exceed `high`. At most four spans per range.

**Ordering.** Several spans expand as an odometer: the rightmost span varies
fastest. `ge-[0-1]/0/[0-3]` yields `ge-0/0/0`, `ge-0/0/1`, `ge-0/0/2`,
`ge-0/0/3`, `ge-1/0/0`, … The expansion lands where the entry stood, so the
interfaces around it keep their relative order.

**Zero padding.** The width of the *low* bound is the width of every value the
span produces. `[01-12]` yields `01` … `12`; `[1-12]` yields `1` … `12`. A high
bound needing more digits simply uses them: `[01-100]` ends at `100`.

**Per-index `description`.** Inside `description`, `{}` and `%d` stand for the
value of the last (fastest-varying) span, and `{0}`, `{1}`, … for a span by
position, left to right. `{{`, `}}` and `%%` are the literal characters; a lone
brace is an error rather than literal text, because it is almost always a typo.
A `%` that does not begin `%d` or `%%` is left alone — a description is prose
and may well say "50% utilised". No other field is substituted into.

```yaml
  - range: ge-[0-1]/0/[0-3]
    type: ethernet
    description: Slot {0}, port {1}      # "Slot 0, port 3", …
```

**Bounds.** One document expands to at most **4096** interfaces in total.
`eth[1-99999999]` is a typo, and the answer to a typo is a diagnostic
(`NG-R003`), not an out-of-memory kill.

**Collisions.** An expanded name that another entry of the same element already
claims — an explicit `name`, or the expansion of another range — is `NG-R004`,
and the diagnostic quotes both source locations. Two *explicitly* named
duplicates remain `NG-I001`.

| ID | Sev. | Rule |
|---|---|---|
| `NG-R001` | error | An interface entry declares exactly one of `name` and `range`. |
| `NG-R002` | error | `range` is a string carrying between one and four well-formed, non-inverted `[low-high]` spans and no stray bracket. |
| `NG-R003` | error | Expanding a document's ranges produces at most 4096 interfaces. |
| `NG-R004` | error | An expanded interface name does not collide with another interface of the same element. |
| `NG-R005` | error | Every `{...}` placeholder in a range `description` is empty or names a span the range declares, and every brace is paired. |

#### 6.2.6 `wireless`

A `wifi` interface without this block is a radio netgraph knows nothing about:
`medium: wireless` joins two of them and the diagram draws a dashed line, but
nothing says which network is on the air, in which direction, or on which
frequency. The block supplies exactly that.

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `role` | enum | M | — | `ap`, `station` or `mesh`. §6.2.6.1. |
| `band` | enum | O | `null` | `2.4GHz`, `5GHz` or `6GHz`. Required alongside `channel` and `width_mhz`. |
| `channel` | integer | O | `null` | The primary 20 MHz channel, as the band numbers it (`NG-W003`). |
| `width_mhz` | enum | O | `null` | `20`, `40`, `80`, `160` or `320`, bounded by the band (`NG-W004`). |
| `tx_power_dbm` | dbm | O | `null` | Radiated power. |
| `bss` | list[Bss] | O | `[]` | The basic service sets this radio beacons or joins. §6.2.6.2. |

##### 6.2.6.1 `role`

| `role` | Meaning |
|---|---|
| `ap` | The radio beacons. It owns the SSIDs, the channel and the frequency width, and bridges each BSS into a VLAN. |
| `station` | A client: it associates to one BSS of one access point. |
| `mesh` | The backhaul radio of a mesh node — a station that relays rather than consumes. Drawn as infrastructure, not as a client. |

A `medium: wireless` cable is an *association*, so it joins exactly one `ap`
radio to one `station` or `mesh` radio (`NG-W007`). Two access points on one
link describe interference rather than a link; two clients describe a link no
frame can cross.

##### 6.2.6.2 `bss[]`

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `ssid` | ssid | M | — | The network name. Unique within one radio (`NG-W005`). |
| `bssid` | mac | O | `null` | MAC address of this BSS. Unique across the inventory among `ap` radios (`NG-W008`). |
| `vlan` | vlan-id | O | `null` | The VLAN this SSID is bridged into; absent means the radio's untagged domain. Checked against the device VLAN database (`NG-V004`) and against the VLANs the AP carries (`NG-W009`). |
| `security` | enum | O | `null` | `open`, `wpa2-psk`, `wpa2-eap`, `wpa3-psk` or `wpa3-eap`. Absent means "not recorded", which is deliberately not the same as `open`. |
| `hidden` | boolean | O | `false` | The SSID is left out of the beacon. It is still on the air. |

On an `ap` radio each entry is one SSID the radio beacons; a dual-SSID access
point has two. On a `station` or `mesh` radio there is at most one entry
(`NG-W006`) — the association — and it names the SSID, and optionally the
BSSID, the radio joined:

```yaml
# On the access point
- name: wlan0
  type: wifi
  mac: '78:8a:20:aa:00:10'
  wireless:
    role: ap
    band: 5GHz
    channel: 36
    width_mhz: 80
    tx_power_dbm: 23
    bss:
      - {ssid: home, bssid: '78:8a:20:aa:00:11', vlan: 10, security: wpa3-psk}
      - {ssid: home-guest, bssid: '78:8a:20:aa:00:12', vlan: 20, security: wpa2-psk}

# On the client
- name: en0
  type: wifi
  wireless:
    role: station
    band: 5GHz
    channel: 36
    bss:
      - {ssid: home, bssid: '78:8a:20:aa:00:11'}
```

**One association per radio.** A cable terminates an interface once
(`NG-C005`), and an association is a cable, so one radio serves one client in
this model. An access point with thirty phones on it is not something an
inventory is meant to enumerate: declare the associations that are part of the
*infrastructure* — a mesh backhaul, a wireless bridge, a fixed client — and
leave the transient ones out.

**Channels are per band.** Channel 1 exists at 2.4 GHz and at 6 GHz and means
5 MHz apart in one case and nearly 3.5 GHz apart in the other, which is why
`channel` without `band` is refused rather than guessed. The legal numbers are
1–14 (2.4 GHz), the 802.11 UNII numbering 32–177 (5 GHz) and 1–233 in steps of
four (6 GHz).

**Frequency overlap** is computed by centring `width_mhz` on the primary
channel; the real centre of a bonded channel depends on which secondary
channels the radio picked, which no document states. `NG-W011` uses the
approximation, which can only make it warn more readily, never less.

The projection onto `ieee802-dot11` is §9.6.

| ID | Sev. | Rule |
|---|---|---|
| `NG-W001` | error | `ssid` is between 1 and 32 octets. |
| `NG-W002` | error | `wireless` appears only on an interface of `type: wifi`. |
| `NG-W003` | error | `channel` names `band`, and is a channel that band numbers. |
| `NG-W004` | error | `width_mhz` names `band`, and is a width that band supports. |
| `NG-W005` | error | `ssid` and `bssid` are each unique within one radio. |
| `NG-W006` | error | A `station` or `mesh` radio lists at most one BSS. |
| `NG-W007` | error | A `medium: wireless` cable joins exactly one `ap` radio to one `station` or `mesh` radio. |
| `NG-W008` | error | A `bssid` is advertised by at most one `ap` radio in the inventory. |
| `NG-W009` | error | An SSID's `vlan` is carried by at least one interface of the access point. |
| `NG-W010` | error | A client radio's SSID is one the access point at the far end advertises. |
| `NG-W011` | warning | Two access points in one broadcast domain do not overlap in frequency. |

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

### 6.6 `template` — reusable partial device specs

Fifty switches wired into the same access layer differ in three fields and agree
in two hundred. A `kind: template` document declares the two hundred once:

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: template
metadata:
  name: c9200l-48p
spec:
  vendor: Cisco
  model: C9200L-48P
  bridge: {name: br0, type: customer-vlan-bridge}
  vlans:
    - {id: 10, name: staff}
    - {id: 99, name: mgmt}
  interfaces:
    - range: GigabitEthernet1/0/[1-48]
      type: ethernet
      description: Access port {}
      enabled: false
      vlan: {mode: access, access_vlan: 10}
    - name: Vlan99
      type: vlan
      parent: br0
      vlan: {mode: access, access_vlan: 99}
```

A device then names it in `spec.from`:

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-acc-07
spec:
  from: templates/c9200l-48p
  interfaces:
    - name: Vlan99
      ipv4: [10.1.99.17/24]
```

`spec.from` uses the ordinary reference grammar of §4.1 and resolves by the
ordinary rules of §2.2 — the device's own namespace first, then each ancestor,
then the whole inventory if the short name is unique — so a template may live
anywhere, including a `templates/` directory next to the sites that use it.
Templates are indexed *separately* from elements: a template and a switch may
share a name, because no field ever accepts both.

A template MAY itself declare `from`. The chain is resolved from the far end
inwards, so the device always merges against one fully-resolved spec. A cycle is
`NG-M003`.

`from` is only meaningful where `spec` is a device spec, so it is accepted on
`switch`, `router`, `hub`, `computer` and `server` and rejected elsewhere
(`NG-M006`).

#### 6.6.1 Merge rules

The merge is between the device's `spec` and the template's `spec`, and nothing
else: `metadata` is the device's own, so a template contributes no name, no
description, no labels and no annotations. `from` itself is consumed and never
appears in the merged spec.

Within `spec`, exactly four rules apply, in this order:

1. **A key only the template declares is inherited.** `vendor`, `model`, the
   VLAN database — whatever the device is silent about.
2. **A key both declare, whose two values are both mappings, merges
   recursively** by these same rules. This is what lets a device write
   `bridge: {address: 00:1b:0d:01:a3:ff}` and keep the template's `bridge.name`
   and `bridge.type`.
3. **`interfaces` merges by interface `name`.** The result is the template's
   interfaces, in the template's order, each merged (by rule 2) with the
   device's entry of the same name where there is one; followed by the device's
   remaining interfaces, in the device's order. Ranges on both sides are
   expanded *before* the match, so a device may override one port out of
   forty-eight by naming it.
4. **Anything else the device declares wins wholesale.** A scalar replaces a
   scalar. A list that is not `interfaces` — `vlans`, `members`, `addresses`,
   `trunk_vlans` — is *replaced*, not concatenated and not merged element by
   element. netgraph has a key for interfaces and for nothing else, and a merge
   rule that only holds sometimes is worse than a rule that never does. A device
   that wants the template's VLAN database plus one more VLAN restates the list.

An interface entry that overrides a template's is a *partial* entry: it states
`name` and the fields it changes, and may omit `type` and everything else. Only
inside a `spec` that declares `from` is that legal; elsewhere `type` is
mandatory as usual.

#### 6.6.2 Templates are not elements

A template never appears in a graph, never appears in `netgraph list`, and is
never validated on its own. It has no interfaces to cable, no address to place
in a subnet, and no node to draw. The only place it surfaces at all is as the
**source location of a field it contributed**: a value the template got wrong is
reported against the template's file and line, with a note naming the device
that inherited it, rather than against the fiftieth device that used it.

That is also why a template's `spec` is checked only for shape — that it is a
mapping of device-spec keys — and not field by field. A `vlan` block is legal on
a switch and illegal on a hub, and a template does not know which it will be
merged into. Deep checking happens on each merged device, where the value
finally has a context that says what it must satisfy.

Use `netgraph show <name> --raw` to see a device as written and `netgraph show
<name>` to see it merged. The pair is how a merge is inspected.

| ID | Sev. | Rule |
|---|---|---|
| `NG-M001` | error | `spec.from` names exactly one `kind: template` document, resolved by §2.2. |
| `NG-M002` | error | Template names are unique within their namespace; the diagnostic names both source locations. |
| `NG-M003` | error | Template inheritance through `from` is acyclic. |
| `NG-M004` | error | A device only inherits from a template that resolved; a template rejected for its own reasons is reported once, against itself. |
| `NG-M005` | error | A `template` document's `spec` is a mapping whose keys are device-spec keys (plus `from`). |
| `NG-M006` | error | `spec.from` appears only on the five device kinds. |

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
* A cable is the *physical* link. Its logical counterpart — WireGuard, IPsec,
  OpenVPN, PPTP, L2TP, GRE, VXLAN, Geneve, and any of them nested inside
  another — is the `tunnel` kind, §14. Section numbers are append-only, which
  is why a kind added after §13 is documented there rather than next to this
  one.

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

### 9.6 Wireless (IEEE 802.11)

`ieee802-dot11` renders the `dot11Xxx` attributes of IEEE Std 802.11-2020
Annex C as YANG leaves. The `wireless` block augments the interface that is the
radio — `if:type = ianaift:ieee80211` — under
`/if:interfaces/if:interface/dot11:wireless-interface`:

| YAML | YANG node |
|---|---|
| `wireless.role` | `…/dot11:station-config/dot11:desired-bss-type` (approximate; `mesh` has no counterpart) |
| `wireless.band` | `…/dot11:phy/dot11:channel-starting-factor` (2407 / 5000 / 5950 MHz) |
| `wireless.channel` | `…/dot11:phy/dot11:current-channel-number` |
| `wireless.width_mhz` | `…/dot11:phy/dot11:current-channel-width` |
| `wireless.tx_power_dbm` | `…/dot11:phy/dot11:current-tx-power-level` (the MIB numbers abstract levels; netgraph records dBm) |
| `bss[].ssid` | `…/dot11:bss/dot11:ssid` (`dot11DesiredSSID` on a client radio) |
| `bss[].bssid` | `…/dot11:bss/dot11:bssid` (`dot11DesiredBSSID` on a client radio) |
| `bss[].security` | `…/dot11:bss/dot11:rsna-enabled` plus `dot11:privacy-invoked` |
| `bss[].vlan` | 802.1Q, not 802.11: the VLAN the AP bridges the BSS into |
| `bss[].hidden` | no YANG node; beacon suppression is vendor configuration |

Associated stations, PHY capabilities, regulatory state and RSN cipher
negotiation are **not** modelled; see
[`docs/yang-mapping.md`](yang-mapping.md#what-netgraph-does-not-model-from-80211).


---

## 10. Validation rules

Every rule has a stable ID. The validator (`netgraph validate`) reports them as
`NG-C005: interface sw-access-01:Gi0/2 is terminated by 2 cables (cbl-a, cbl-b)`.
IDs are permanent: once assigned, an ID is never reused for a different rule.

Severity `error` fails the run (exit code 4, `ValidationError`); `warning` and
`info` are reported but rendering proceeds. `netgraph validate --strict`
promotes every warning to an error. Individual rules can be re-graded or
silenced per inventory (§10.11).

The semantic validator also prints a short id (`E002`, `W103`) alongside the
`NG-*` id; the two vocabularies are interchangeable everywhere a rule can be
named. §10.10 maps them.

### 10.1 Document and naming

| ID | Sev. | Rule |
|---|---|---|
| `NG-D001` | error | The document is a mapping with the four envelope keys; `apiVersion`, `kind`, `metadata`, `spec` are all present. |
| `NG-D002` | error | `apiVersion` is a recognised version string. |
| `NG-D003` | error | `kind` is one of the ten element kinds or `template`, lower-case. |
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
| `NG-A010` | warning | One prefix is claimed by interfaces in two different VLANs, and the two hold addresses of their own. Neither half can ARP for the other, and no router forwards between them. |
| `NG-A011` | warning | A prefix nested inside another is used in a VLAN the wider prefix is not, so hosts in the wider one ARP for addresses they should route to. |
| `NG-A012` | warning | The two interfaces a cable joins are addressed in prefixes that do not overlap, so neither address is inside any prefix on its own link. |
| `NG-A013` | error | An interface's `gateway` is inside none of the prefixes that interface configures for the same family. A link-local IPv6 gateway is exempt. |

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

### 10.9 Ranges and templates

Loader rules: they are checked while the document is being rewritten into the
shape the models validate, so they are reported by every command that loads an
inventory rather than by `netgraph validate` alone, and they have no short-id
alias. §6.2.5 and §6.6 state them in context.

| ID | Sev. | Rule |
|---|---|---|
| `NG-R001` | error | An interface entry declares exactly one of `name` and `range`. |
| `NG-R002` | error | `range` is a string carrying between one and four well-formed, non-inverted `[low-high]` spans and no stray bracket. |
| `NG-R003` | error | Expanding a document's ranges produces at most 4096 interfaces. |
| `NG-R004` | error | An expanded interface name does not collide with another interface of the same element; the diagnostic names both source locations. |
| `NG-R005` | error | Every `{...}` placeholder in a range `description` is empty or names a span the range declares, and every brace is paired. |
| `NG-M001` | error | `spec.from` names exactly one `kind: template` document, resolved by §2.2. |
| `NG-M002` | error | Template names are unique within their namespace; the diagnostic names both source locations. |
| `NG-M003` | error | Template inheritance through `from` is acyclic. |
| `NG-M004` | error | A device only inherits from a template that resolved; a template rejected for its own reasons is reported once, against itself. |
| `NG-M005` | error | A `template` document's `spec` is a mapping whose keys are device-spec keys (plus `from`). |
| `NG-M006` | error | `spec.from` appears only on the five device kinds. |

### 10.10 Rule identifiers

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
| `E016` | error | `NG-T002` | A tunnel endpoint references an unknown element or interface, or a name that stays ambiguous. |
| `E017` | error | `NG-T003` | A tunnel endpoint is not an interface of `type: tunnel`. |
| `E018` | error | `NG-T004` | `over` names no tunnel of this inventory. |
| `E019` | error | `NG-T005` | The `over` references form a cycle, so no tunnel in it reaches the underlay. |
| `W125` | warning | `NG-T006` | A tunnel terminates on an element its `over` underlay does not reach. |
| `W126` | warning | `NG-T011` | A tunnel's `mtu` exceeds its underlay's `mtu` minus its own encapsulation overhead (§14.1). |
| `W127` | warning | `NG-T012` | A tunnel encrypts nothing and no tunnel in its `over` chain does either. |
| `W128` | warning | `NG-T013` | An enabled `type: tunnel` interface is named by no `tunnel` document. |
| `W129` | warning | `NG-T014` | Two tunnels terminating on one element declare the same `vni`. |
| `I003` | info | `NG-T015` | A tunnel's `port` is not the registered port for its type. |
| `E020` | error | `NG-A013` | An interface's `gateway` is on none of the prefixes it configures for that family. A link-local IPv6 gateway is exempt. |
| `W130` | warning | `NG-A010` | One prefix is claimed by interfaces in two VLANs that hold addresses of their own. When the addresses are identical it is `W106`/`E004` instead. |
| `W131` | warning | `NG-A011` | A nested prefix is used in a VLAN its parent prefix is not. |
| `W132` | warning | `NG-A012` | The two ends of a cable are addressed in prefixes that do not overlap. Only families both ends configure are compared. |
| `E028` | error | `NG-W007` | A `medium: wireless` cable joins two radios that are not one `ap` and one `station`/`mesh`. Checked once both ends declare a `wireless` block. |
| `E029` | error | `NG-W008` | Two `ap` radios advertise the same `bssid`. A client's BSS entry repeats the AP's by design and is exempt. |
| `E030` | error | `NG-W009` | An SSID's `vlan` is carried by no interface of the access point. An AP with a port trunking `all` is exempt. |
| `E031` | error | `NG-W010` | A client radio's SSID is not one the `ap` radio at the far end advertises. An AP listing no BSS is exempt. |
| `W134` | warning | `NG-W011` | Two access points that share a broadcast domain are in one band with overlapping channels. |

Ids are permanent (§10), so a suppression written today keeps meaning the same
thing. Where a short id covers two schema ids (`E001`), naming either alias
selects the whole rule.

Three rules are graded more harshly here than in the tables above: `E003`
(`NG-I008`), `E004` (`NG-A004`) and `E010` (`NG-I009`). The first two because a
duplicate address is far more often a copy-paste mistake than a deliberate VRRP
or anycast design; the third because a multicast source address is not a design
at all. Re-grade them per inventory (§10.11) where the exception is real.

### 10.11 Suppressing a rule

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

### 10.12 Patch panels

Numbered after §10.11 rather than beside the other rule tables: section numbers
are append-only (§12), so a group added in a later revision lands at the end.

| ID | Sev. | Rule |
|---|---|---|
| `NG-P001` | error | A cable endpoint on a patch panel names a position the panel declares, spelled `front/<n>` or `rear/<n>`. |
| `NG-P002` | warning | A cabled panel position's coupled position also terminates a cable; a run that stops inside the panel reaches nothing. |
| `NG-P003` | error | A panel position terminates at most one cable. |
| `NG-P004` | error | A patch panel is not named where an active element is required: `upstream.attached_to` and a tunnel endpoint both need one. |
| `NG-P005` | error | A patch run does not come back into a segment it has already crossed. |
| `NG-P006` | error | `spec.ports` is a positive count or comma-separated spans, with no repeats and at most 1024 positions. |
| `NG-P007` | error | Every position `spec.couplers` names is declared by `spec.ports`, and no two front positions share a rear one. |

### 10.13 Physical placement

| ID | Sev. | Rule |
|---|---|---|
| `NG-U001` | error | Two elements in one rack do not occupy overlapping units. |
| `NG-U002` | error | No element extends past the declared `rack_height` of its rack. |
| `NG-U003` | error | Every element in one rack declares the same `rack_height`. |
| `NG-U004` | error | `position` and `rack_height` are only written alongside a `rack`. |

### 10.14 Wireless

| ID | Sev. | Rule |
|---|---|---|
| `NG-W001` | error | `ssid` is between 1 and 32 octets. |
| `NG-W002` | error | `wireless` appears only on an interface of `type: wifi`. |
| `NG-W003` | error | `channel` names `band`, and is a channel that band numbers. |
| `NG-W004` | error | `width_mhz` names `band`, and is a width that band supports. |
| `NG-W005` | error | `ssid` and `bssid` are each unique within one radio. |
| `NG-W006` | error | A `station` or `mesh` radio lists at most one BSS. |
| `NG-W007` | error | A `medium: wireless` cable joins exactly one `ap` radio to one `station` or `mesh` radio. |
| `NG-W008` | error | A `bssid` is advertised by at most one `ap` radio in the inventory. |
| `NG-W009` | error | An SSID's `vlan` is carried by at least one interface of the access point. |
| `NG-W010` | error | A client radio's SSID is one the access point at the far end advertises. |
| `NG-W011` | warning | Two access points in one broadcast domain do not overlap in frequency. |

`NG-W001` to `NG-W006` are schema rules, reported while the document is parsed
and not suppressible; the rest are semantic and carry the short ids of §10.10.

### 10.15 Routing

| ID | Sev. | Rule |
|---|---|---|
| `NG-F001` | error | `vrfs[].name` is unique within a device. |
| `NG-F002` | error | An interface's `vrf` names an entry of the device's `vrfs`; an adapter interface declares none. |
| `NG-F003` | error | A route's `via` is of the same address family as its `prefix`. |
| `NG-F004` | error | A route declares at least one of `via`, `dev` and `blackhole`, and `blackhole` excludes the other two. |
| `NG-F005` | error | A route's `vrf` names an entry of the device's `vrfs`. |
| `NG-F006` | error | `routing.ospf.interfaces` is non-empty and free of duplicates. |
| `NG-F007` | error | `routing.bgp.neighbors[].address` is unique within a device. |
| `NG-F008` | error | A route's next hop is inside a prefix the device configures, on an interface in the route's own VRF. |
| `NG-F009` | error | A route's `dev` names an interface of the device. |
| `NG-F010` | error | Every `routing.ospf.interfaces` entry names an interface of the device. |
| `NG-F011` | error | The two ends of a resolved BGP session agree about both AS numbers. |
| `NG-F012` | error | A router id is claimed by at most one element. |
| `NG-F013` | warning | A BGP neighbour address resolves to an element of the inventory. |
| `NG-F014` | warning | Every declared VRF has at least one interface bound to it. |

`NG-F001` to `NG-F007` are schema rules, reported while the document is parsed
and not suppressible; `NG-F008` to `NG-F014` are semantic and carry the short
ids of §10.10. The group is lettered `F`, for *forwarding*: `NG-R` was spent on
interface ranges (§10.9) long before routing was modelled, and an id, once
assigned, is never reused.

### 10.16 Power

The `NG-E*` group — PDU outlets, device power and PoE — is tabulated beside the
model it constrains, in [§17.8](#178-rules), because half of what each rule says
is a sentence about watts that only makes sense next to the class table. The
severities and the schema/semantic split are stated there.

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

This section is normative for the schema. The same policy for netgraph's *other*
public surfaces — the CLI, the JSON output documents, the exit codes, the rule
ids, the published integrations — and the four things a breaking change to any of
them has to carry, are in [`releasing.md`](releasing.md#what-is-public-api).
Each release records what changed in [`CHANGELOG.md`](../CHANGELOG.md).

### 12.1 Deferred to a later revision

Deliberately out of scope for `v1alpha1`, listed so that nobody designs around
their absence:

* **Spanning tree**: STP/RSTP/MSTP roles and per-port cost.
* **Multi-chassis aggregation**: MLAG/vPC/stacking relationships between
  switch elements.
* **Host-side expansion ports**: a `usb`/`thunderbolt` interface type on
  devices, which would let `upstream.attached_to` name a specific receptacle
  (§8.1).
* **Per-inventory configuration** beyond validation and rendering:
  `netgraph.toml` at the inventory root carries rule suppression and severity
  overrides (§10.11), a `[render]` table of renderer defaults and any number of
  named `[profile.<name>]` blocks — see
  [`docs/configuration.md`](configuration.md). What remains deferred is
  configuration of the *model*: per-inventory defaults for a document's own
  fields, such as a default `medium` for every cable.
* **Templating**: reusable device profiles (`kind: profile`) to remove
  repetition across identically-configured switches.

---

## 13. Editor integration

Everything above describes what a document may contain. A JSON Schema says the
same thing in a form an editor can act on, so a misspelt key is underlined as
you type it rather than discovered by the next `netgraph validate`.

<!-- norun: the first line redirects, and the last writes a schema file into the reader's directory -->
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
| Loader rewrites: `interfaces[].range` and `spec.from` | shape only | yes |

The schema is the fast, local half. It is not a substitute for
`netgraph validate`, and CI should keep running the latter.

`range` and `from` (§6.2.5, §6.6) are consumed by the loader, so the schema
describes their *shape* — the bracket grammar, the reference grammar, that an
interface declares one of `name` and `range`, that a `spec` omitting
`interfaces` must inherit them — but cannot say whether a range collides, whether
it fits inside the 4096-interface bound, or whether the template exists. Those
need the tree.

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

<!-- norun: writes a schema file into the reader's directory -->
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

---

## 14. Tunnels

A tunnel is an undirected **logical** link between two or more interfaces. It is
to a logical topology what a cable (§7) is to a physical one, and a first-class
element for the same reason: it carries its own metadata, has its own identity,
and is validated independently of the devices it joins.

Section numbers in this document are append-only, so the kind added after §13
is documented here rather than between §7 and §8. Everything else about it
mirrors the cable: the same `device:interface` endpoint grammar (§4.2), the
same undirected semantics, the same namespace rules.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: tunnel
metadata:
  name: ipsec-hq-branch
  labels: {site: hq}
spec:
  type: ipsec
  mode: tunnel
  endpoints:
    - rtr-hq:ipsec0
    - rtr-branch:ipsec0
  mtu: 1400
  cipher: aes-256-gcm
  auth: certificate
---
apiVersion: netgraph.dev/v1alpha1
kind: tunnel
metadata:
  name: vx-100
spec:
  type: vxlan
  vni: 100
  endpoints:
    - rtr-hq:vxlan100
    - rtr-branch:vxlan100
  over: ipsec-hq-branch      # VXLAN over IPsec
  mtu: 1350
```

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `type` | enum | M | — | The encapsulation; see §14.1. |
| `endpoints` | list[ifref] | M | — | Two or more entries, each naming a `type: tunnel` interface (`NG-T001`, `NG-T003`). Order is not significant; the loader sorts them. |
| `over` | element ref | O | `null` | The tunnel this one runs inside (§14.3). Absent means it runs over the physical topology. |
| `mode` | enum | C | `tunnel` for `ipsec` and `l2tp` | `tunnel` or `transport` (RFC 4301 §3.2). Rejected for every other type (`NG-T008`). |
| `vni` | uint24 | C | — | Required for `vxlan` and `geneve`, rejected otherwise (`NG-T007`). |
| `port` | uint16 | O | the registered port of `type` | Rejected for `gre` and `ipsec`, which run directly over IP (`NG-T008`). |
| `mtu` | mtu | O | `null` | MTU of the tunnel interface; checked against the underlay by `NG-T011`. |
| `encrypted` | boolean | O | what `type` does | Set it `true` to record that a cleartext type is protected some other way. |
| `cipher` | string | C | `null` | Free text (`chacha20-poly1305`, `aes-256-gcm`). Only on a tunnel that encrypts (`NG-T009`). |
| `auth` | enum | C | `null` | `psk`, `certificate`, `public-key`, `password`. Only on a tunnel that encrypts or authenticates (`NG-T009`). |
| `label` | string | O | `null` | Free-text identifier printed on the edge, as a cable's `label` is. |

### 14.1 Tunnel types

`type` is not a free-text tag: it fixes five facts the renderer and the
validator both use, so a diagram can say what a tunnel actually does rather than
only what it is called.

| `type` | Carries | Outer | Port | Encrypts | Overhead |
|---|---|---|---|---|---|
| `wireguard` | packets (L3) | UDP | 51820 | yes | 80 B |
| `ipsec` | packets (L3) | ESP (IP 50) | — | yes | 73 B |
| `openvpn` | packets (L3) | UDP | 1194 | yes | 69 B |
| `pptp` | packets (L3) | GRE (IP 47) | — | **no** | 40 B |
| `l2tp` | frames (L2) | UDP | 1701 | **no** | 40 B |
| `gre` | packets (L3) | GRE (IP 47) | — | **no** | 24 B |
| `vxlan` | frames (L2) | UDP | 4789 | **no** | 50 B |
| `geneve` | frames (L2) | UDP | 6081 | **no** | 50 B |

* **Carries** decides whether the tunnel is a layer-2 link. A layer-2 tunnel
  extends a broadcast domain across the underlay, so it carries the VLANs its
  endpoints are configured for and `netgraph render --layer l2` annotates it
  exactly as it annotates a trunk. A layer-3 tunnel carries no VLAN.
* **Encrypts** is a property of the technology, not of the deployment. PPTP is
  listed as cleartext deliberately: MPPE is broken, so a PPTP tunnel protects
  nothing. A cleartext tunnel that is not nested inside an encrypting one is
  `NG-T012`.
* **Overhead** is the widely published worst case over IPv4 — the number an
  operator would set an overlay MTU from — not an exact packet layout, which
  varies with cipher, IP version and NAT traversal. `NG-T011` measures an `mtu`
  against it.

`port`, `mode` and `encrypted` are **materialised on load** from this table
(§1), so a loaded document states them explicitly even when the file did not.

### 14.2 What a tunnel does not hold

There is nowhere in this schema to put a private key, a pre-shared key, a
password, a passphrase or a certificate, and the field names people reach for
are rejected **by name** with an explanation rather than as unknown keys
(`NG-T010`). An inventory is a file in version control that gets rendered into
diagrams and pasted into tickets; it is the wrong place for key material, and a
schema that accepted some would be inviting the mistake.

`auth` records the authentication *method* — which is what a reader of a diagram
needs and what an auditor asks for — and `cipher` the negotiated suite.

### 14.3 Semantics

* A tunnel is **undirected**, exactly as a cable is. The endpoint order carries
  no meaning and the loader sorts it for canonical output.
* An endpoint names a **`type: tunnel` interface** (`NG-T003`) — the virtual
  interface the operating system presents (`wg0`, `ipsec0`, `vxlan100`), never
  the physical port the outer packets leave by. That interface holds the
  *overlay* configuration: the addresses inside the tunnel, which is what puts
  both ends in one prefix at layer 3. Its optional `parent` may name the
  underlay port, which is `if:lower-layer-if` (§14.4).
* Unlike a cable, an interface may terminate **several** tunnels: a router that
  is the hub of three VPNs presents three virtual interfaces, but a VTEP may
  legitimately carry several VXLANs. What is checked instead is that two of them
  do not claim the same `vni` on one element (`NG-T014`).
* **Two or more endpoints.** Three or more make the tunnel multipoint — a VXLAN
  mesh, a hub-and-spoke VPN — and it is then drawn as a *node* with one leg per
  endpoint rather than as a line, the same choice a subnet gets at layer 3.
* **`over` nests one tunnel inside another.** `vxlan over ipsec` is written by
  naming the IPsec tunnel in the VXLAN's `over`. The chain is walked outwards to
  give every tunnel a depth, a stack (`("vxlan", "ipsec")`) and the nearest
  underlay that encrypts; it must not loop (`NG-T005`), and each step should
  reach every endpoint of the tunnel above it (`NG-T006`). Nesting is what makes
  a cleartext overlay confidential, so it is what silences `NG-T012`.
* A tunnel is **not a cable**. It is not part of the physical topology, does not
  join two islands for `NG-C014`, and is not something a technician can unplug.
  Renderers draw it dashed and violet, or crimson when nothing in its stack
  encrypts.

### 14.4 YANG mapping

A tunnel has no YANG representation of its own — ietf-interfaces models devices,
not the encapsulation between them, exactly as it does not model a cable (§9.4).
It projects onto the interfaces at its ends:

| YAML | Projection |
|---|---|
| `endpoints[]` | the `if:interface` named by each reference, whose `if:type` is `ianaift:tunnel` |
| `interfaces[].parent` on a `type: tunnel` interface | `if:lower-layer-if` — the underlay port the outer packets leave by |
| `tunnel.mtu` | netgraph-only; RFC 8343 has no layer-2 MTU node (§9.2) |
| `type`, `mode`, `vni`, `port`, `encrypted`, `cipher`, `auth`, `over` | netgraph-only. The IETF tunnel models (`ietf-ipsec`, RFC 9061) sit outside the three this schema maps to; see [`yang-mapping.md`](yang-mapping.md) |

### 14.5 The overlay view

`netgraph render --layer overlay` draws the encapsulation graph: every tunnel
becomes a node, joined to each element it terminates on and to the tunnel it
runs inside. The tunnel has to become a node there because nesting is a relation
between two *links*, and a link cannot end on a link — which is exactly why
`VXLAN over IPsec` is undrawable at layer 1 and obvious here.

Below that layer a point-to-point tunnel stays an edge, so `netgraph render`
shows the VPNs over the physical topology without a box in the middle of each
one. `netgraph list tunnels` prints the same resolution as a table.

---

## 15. Patch panels

A `patchpanel` is a **passive cross-connect**: numbered positions on the front,
the same numbers on the rear, and a fixed coupler joining each front position to
one rear position. Nothing in it powers on, nothing in it makes a decision, and
a frame that enters one side leaves the other unchanged.

It is a separate kind because a real run almost never goes device to device. It
goes switch port → panel front → structured cabling → panel rear → server port,
and an inventory with no panel has to *lie* about that run by cabling the two
devices together directly — losing the two things a patch record exists for:
which position the run occupies, and which position is still free.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: patchpanel
metadata:
  name: pp-mdf-a
  location: {site: hq, room: mdf, rack: r1, position: 42, rack_height: 42}
spec:
  vendor: Panduit
  model: CPPL24WBLY
  form_factor: keystone
  ports: 1-24
```

### 15.1 `spec`

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `vendor` | string | O | `null` | Free text. |
| `model` | string | O | `null` | Free text. |
| `serial` | string | O | `null` | Free text. |
| `form_factor` | string | O | `null` | Descriptive: `keystone`, `fibre-lc`, `coupler`. |
| `ports` | port range | M | — | The positions the panel has: a count (`24`, meaning 1 to 24) or comma-separated spans (`1-24`, `1-12,17-24`). At most 1024, no repeats (`NG-P006`). |
| `couplers` | map[number, number] | O | `null` | Front position → rear position, for a panel that is not wired straight through. Absent means the identity mapping (`NG-P007`). |

A panel owns **no `interfaces` key**. Its ports are derived from `ports`: every
position `n` becomes an interface named `front/<n>` and one named `rear/<n>`, of
`type: ethernet`, with no address, no VLAN and no MAC — a hole with a number.
Writing 48 near-identical entries by hand is exactly the typing the interface
ranges of §6.2.5 exist to avoid, and here there is nothing to vary.

The zero padding of a span follows its *low* bound, as in §6.2.5: `01-12` yields
`01 … 12` and `1-12` yields `1 … 12`.

A cable terminates on a panel position exactly as on a device port (§4.2):

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-sw-pp-07}
spec:
  endpoints:
    - sw-access-01:GigabitEthernet1/0/7
    - pp-mdf-a:front/7
  medium: copper
  length_m: 3
```

### 15.2 Electrical transparency

A panel is **not a hop**, so the same inventory has two honest readings and
`build_graph` offers both.

`netgraph render --layer physical`
: The cabling record. The panel is a node and each cable segment is an edge of
  its own, which is what a technician standing in the room would find.

every other layer (`l1`, `l2`, `l3`, `overlay`, `routing`, `power`)
: The panel is **spliced out**. The run
  `switch → front/7 ⇄ rear/7 → server` becomes the single edge
  `switch → server` it is indistinguishable from, between the two active ports.

Splicing walks the run rather than deleting the panel, because the properties of
the run belong to all of its segments:

| Attribute | Spliced value |
|---|---|
| `medium` | what every segment agrees on; the first segment's otherwise |
| `speed` | the slowest segment — a run is no faster than its worst leg |
| `length_m` | the sum, when every segment declares one; `null` otherwise |
| `label` | the first segment that has one |
| VLANs | derived from the two *active* ports, exactly as a direct cable would be |

The result is that **a spliced graph is the graph the same inventory produces
when the two devices are cabled together directly.** That equivalence is what
makes the panel free to model: adding one to a correct inventory cannot change
any layer but `physical`.

The spliced edge remembers what it crossed. `netgraph render -f json` exports it
as a `patch` object naming the segments and the positions, `netgraph path` names
the panels on the link line — as a pass-through, never as a waypoint, because a
panel takes no decision — and an SVG tooltip lists the same record.

A run that does not arrive anywhere is not spliced. A coupler with nothing
patched into its far side is `NG-P002`, a position with two cables is `NG-P003`,
and a run that closes on itself is `NG-P005`; in each case the incomplete run is
dropped from every spliced layer and stays visible at `--layer physical`.

### 15.3 What a panel is not

* **Not a host.** `upstream.attached_to` on an adapter and a tunnel endpoint
  both require an active element (`NG-P004`). A media converter that looks like
  it wants to be a panel is an `adapter` with `passthrough: false` (§8.2).
* **Not a repeater.** A `hub` is active: it regenerates a signal and joins a
  collision domain, so it *is* a node at every layer. A panel joins nothing; it
  continues one link.
* **Not configurable.** There is nowhere on a panel to put a VLAN, an address or
  an MTU, which is why its ports are derived rather than declared.

### 15.4 YANG mapping

A patch panel has no YANG counterpart: RFC 8343 models interfaces of a *system*,
and a panel is not one. Its derived positions are described here as
`if:interface` entries of type `ianaift:ethernetCsmacd` for internal consistency
only; nothing exports them, and `couplers` is netgraph's own.

---

## 16. Routing

Routing is *state of a box*, not a thing between boxes: a route is written on
one device, and an adjacency is configured on one device towards a neighbour it
names by address. So all three blocks hang off a device's `spec`, which is also
the shape [RFC 8349](https://www.rfc-editor.org/rfc/rfc8349) (`ietf-routing`)
gives it — a control-plane protocol and a routing table live inside a *network
instance*, which is what a VRF is
([RFC 8529](https://www.rfc-editor.org/rfc/rfc8529)).

| Key | Holds |
|---|---|
| `spec.vrfs[]` | The routing instances the device implements (§16.1). |
| `spec.interfaces[].vrf` | Which instance one interface is in (§16.1). |
| `spec.routes[]` | Static routes (§16.2). |
| `spec.routing` | The dynamic protocols it takes part in (§16.3). |

None of them is required, and a device that declares none of them is exactly
what every device in an inventory written before this section was one: routing
is additive, and an inventory that says nothing about it is not wrong, only
silent.

**There is nowhere to put a secret.** As with tunnels (§14.2), a BGP password or
an OSPF authentication key has no field: a secret in an inventory is a secret in
version control, and netgraph has no use for one.

### 16.1 `vrfs[]` — routing instances

```yaml
spec:
  vrfs:
    - name: mgmt
      rd: '65001:99'          # RFC 4364 §4.2; quote it, see below
      description: In-band management
  interfaces:
    - name: Vlan99
      type: vlan
      ipv4:
        addresses: [10.1.99.1/24]
      vrf: mgmt               # names an entry of spec.vrfs
      parent: br0
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `name` | element name | yes | How everything else refers to the instance. Unique within the device (`NG-F001`). |
| `rd` | route distinguisher | yes | `65001:1`, `192.0.2.1:1` or `4200000000:1` — one of the three RFC 4364 §4.2 encodings. |
| `description` | string | no | Free text. |

`interfaces[].vrf` binds one interface to one instance and must name an entry of
the same device's `vrfs` (`NG-F002`). An interface that binds to none is in the
**global instance**, which is where every address is until something says
otherwise.

**Binding is what partitions the address space.** An address only collides with
another address in the same instance, so `10.0.0.1/24` in `blue` and
`10.0.0.1/24` in the global table are two addresses and not a duplicate
(`NG-A004`); two interfaces of one device may hold overlapping prefixes when
they are in different instances (`NG-A006`); and `netgraph list subnets`,
`netgraph ipam` and `--layer l3` each report one row, one prefix and one node
*per instance*. That is the whole reason to model a VRF: it is a routing table
of its own, so it is an address space of its own.

Two devices that use the same `name` are taken to mean the same VRF — that is
what an operator means by "the blue VRF". The route distinguisher is recorded
because MPLS needs it, not because netgraph identifies the instance by it.

A VRF nothing is bound to holds no address and no connected route, so the
isolation it was declared to create does not exist; that is `NG-F014`.

> **Quote the `rd`.** `65001:59` is the base-60 integer 3900059 to a YAML 1.1
> reader and `65001:99` is a string, so the class is quoted whole — exactly as
> MAC addresses are (§5). `netgraph fmt` adds the quotes if you forget them.

An adapter has no `vrfs` of its own and its interfaces may not declare `vrf`
(`NG-F002`): an adapter is a *port* of the host it hangs off, and the routing
instance belongs to that host.

### 16.2 `routes[]` — static routes

```yaml
spec:
  routes:
    - prefix: 0.0.0.0/0
      via: 203.0.113.1
      dev: wan0
      metric: 10
    - prefix: 10.1.0.0/16
      blackhole: true
    - prefix: 0.0.0.0/0
      vrf: mgmt
      blackhole: true
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `prefix` | IPv4/IPv6 prefix | yes | The destination, in canonical CIDR form. Host bits are refused. |
| `via` | IP address | no | The next hop. |
| `dev` | interface name | no | The egress interface. |
| `vrf` | element name | no | The instance holding the route; the global one when unset. |
| `metric` | integer | no | Administrative distance or cost, as this device counts it. |
| `blackhole` | boolean | no | Discard matching packets. Default `false`. |

A route needs somewhere to send the packet, so at least one of `via`, `dev` and
`blackhole` is required, and `blackhole` excludes the other two (`NG-F004`).
`via` is of the same family as `prefix` (`NG-F003`) — a next hop is resolved on
the destination's own address family — and must be **on-link**: inside a prefix
this device configures, on an interface in the route's own instance
(`NG-F008`). An IPv6 link-local next hop is exempt, being on-link by
definition. `dev` names an interface of this device (`NG-F009`).

`prefix` rejects a destination with host bits set. `10.0.0.1/24` as a
destination is either a typo or a `/32`, and guessing which would put a route in
the diagram that the device does not have.

Nothing here computes a best path. `metric` is recorded, routes are not sorted,
and two routes for one prefix are two declarations rather than a decision:
netgraph describes the configuration, and which route wins is the device's
business.

### 16.3 `routing` — dynamic protocols

```yaml
spec:
  routing:
    ospf:
      area: 0.0.0.0           # or the plain number 0
      router_id: 192.0.2.1
      interfaces: [lo0, xe-0/0/0]
    bgp:
      asn: 65001
      router_id: 192.0.2.1
      neighbors:
        - address: 192.0.2.2
          remote_asn: 65001
          description: iBGP to rtr-south-core-01
```

Both blocks are optional and neither implies the other.

**`ospf`**

| Field | Type | Required | Meaning |
|---|---|---|---|
| `area` | area id | no | Dotted quad or plain number; `0` and `0.0.0.0` are the same backbone area and both normalise to `0.0.0.0`. Default `0.0.0.0`. |
| `router_id` | IPv4 address | no | A dotted quad even in an IPv6-only network (RFC 5340 §2.1). |
| `interfaces` | interface names | yes | The interfaces OSPF runs on. Non-empty and free of duplicates (`NG-F006`); each names an interface of this device (`NG-F010`). |

**`bgp`**

| Field | Type | Required | Meaning |
|---|---|---|---|
| `asn` | 1–4294967295 | yes | The local autonomous system. AS 0 is reserved (RFC 7607). |
| `router_id` | IPv4 address | no | The BGP identifier (RFC 4271 §4.2). |
| `neighbors[].address` | IP address | yes | The peer. Unique within the device (`NG-F007`). |
| `neighbors[].remote_asn` | 1–4294967295 | yes | The AS the peer is in. |
| `neighbors[].description` | string | no | Free text. |

A router id is unique across the inventory (`NG-F012`): it names the router
itself, so OSPF drops an adjacency with a neighbour claiming the local id
(RFC 2328 §10.5) and BGP refuses a session with a duplicate identifier
(RFC 4271 §6.8). One device giving OSPF and BGP the same value is *one* identity,
not a duplicate, and is the normal configuration.

One area per device, deliberately. An area border router is a real thing, but
modelling it needs per-interface areas; see §16.5.

### 16.4 Peers are addresses, never names

A BGP neighbour is written as an **address**, because that is what the device is
configured with. netgraph resolves it against every address the inventory
declares, and that resolution is what draws the session in the routing view and
what lets the two ends be compared:

* the peer's own `asn` is checked against `remote_asn` (`NG-F011`) — a
  disagreement is a session that never establishes;
* an address that matches nothing is a **warning**, not an error (`NG-F013`): a
  correct eBGP session towards a transit provider points at an address on
  *their* router, which is not an element of this inventory and never will be.
  What the warning says is that netgraph cannot check the far end, and that the
  diagram has nothing to draw the edge to.

There is deliberately no second reference grammar. A `peer: rtr-south-core-01`
field would be a name that could point somewhere the *device* does not, which is
the one thing an inventory must not be able to say.

An OSPF adjacency is not declared at all. It is **discovered**, so netgraph
derives it the way the protocol does: two interfaces that run OSPF in the same
area and are addressed in one subnet form one. Deriving it from the addressing
rather than from the cables is what makes it right for two routers facing each
other across a layer-2 switch, which no cable joins directly.

### 16.5 What routing does not hold

Deferred, and listed so nobody designs around the absence:

* **Per-interface OSPF areas**, and therefore area border routers, stub and NSSA
  area types, interface costs and network types.
* **Route policy**: prefix lists, route maps, communities, local preference.
  A policy language is a language, and inventing a half of one would make an
  inventory that says what a router does *not* do.
* **Protocols other than OSPF and BGP**: IS-IS, RIP, EIGRP, and the
  redistribution between any two of them.
* **Route reflectors and confederations**: an iBGP mesh here is a set of
  sessions, with no hierarchy over it.
* **BFD, timers, graceful restart** and everything else that tunes a session
  rather than describing one.
* **Learned state**. `spec.routes` is what somebody configured; a routing table
  is what a router computed from it, and comparing the two is
  `netgraph drift`'s business, not the schema's.

### 16.6 The routing view

`netgraph render --layer routing` draws the control plane:

* **Nodes** are the elements that take part in routing — anything declaring
  `routing`, `routes` or `vrfs` — labelled with the AS number and router id
  their peers know them by, and carrying their instances and their static
  routes.
* **Edges** are the adjacencies: a BGP session is drawn solid and labelled with
  the AS pair (`65001 → 65002`, or `iBGP 65001` when both ends are in one AS),
  an OSPF adjacency dotted and labelled with the area.
* **Clusters** are the VRFs. A router with interfaces in exactly one instance is
  drawn inside that instance's box; one that straddles several belongs to no box,
  exactly as a cross-site prefix belongs to no namespace at layer 3, and names
  its instances on its label instead.

Nothing physical appears. Two routers are adjacent here because they exchange
routes, which a cable neither guarantees nor is needed for.

`netgraph export routes` writes the same static routes out as an iproute2
script, one shell function per device; see
[`docs/export.md`](export.md).

### 16.7 YANG mapping

| netgraph | YANG |
|---|---|
| `spec.vrfs[].name` | `/ni:network-instances/ni:network-instance/ni:name` (RFC 8529) |
| `spec.vrfs[].description` | `…/ni:network-instance/ni:description` |
| `spec.vrfs[].rd` | — (RFC 4364 §4.2; `ietf-network-instance` has no node for it) |
| `spec.interfaces[].vrf` | `…/ni:network-instance/ni:vrf-root` — the instance an interface is bound into |
| `spec.routes[].prefix` | `…/rt:static-routes/v4ur:ipv4/v4ur:route/v4ur:destination-prefix` (RFC 8349) |
| `spec.routes[].via` | `…/v4ur:route/v4ur:next-hop/v4ur:next-hop-address` |
| `spec.routes[].dev` | `…/v4ur:route/v4ur:next-hop/v4ur:outgoing-interface` |
| `spec.routes[].blackhole` | `…/v4ur:route/v4ur:next-hop/v4ur:special-next-hop` = `blackhole` |
| `spec.routes[].metric` | — (`ietf-routing` leaves the metric to each protocol) |
| `spec.routing.ospf` | `…/rt:control-plane-protocols/rt:control-plane-protocol` with `type: ospf` |
| `spec.routing.bgp` | the same list entry with `type: bgp` |
| `spec.routing.*.router_id` | — (`ietf-ospf` and `ietf-bgp` model it per protocol instance) |

The IPv6 routes use the `v6ur:` paths of the same module; only the IPv4 ones are
written out above.

---

## 17. Power

An as-built physical document has two halves. §15 and `metadata.location` are the
first — what is bolted where, and what is patched into what. This is the second:
which outlet each power supply is plugged into, how many watts the box draws, and
which ports hand power *down* the cable instead of taking it from an outlet.

It is worth modelling for the same reason cabling is: the mistakes are silent and
expensive. A rack fed from one PDU looks perfect on a topology diagram and fails
as a unit. A PoE budget oversubscribed by two cameras works until the third one
is plugged in and then browns out a switch. A device with no declared power path
is a device nobody will find during a maintenance window.

Three places say something about power, and one new kind holds the sockets:

| Key | Holds |
|---|---|
| `kind: pdu` | The outlets that exist and how many watts may be drawn through them (§17.1). |
| `spec.power` | On a device: what it draws, which outlets feed it, and how much PoE it hands out (§17.2). |
| `spec.interfaces[].poe` | On one port: that the port is power sourcing equipment, and how much of the shared budget it reserves (§17.3). |

None of it is required. An inventory that says nothing about power is not wrong,
only silent — power is additive, exactly as routing is (§16).

**No measured watts.** As everywhere else in this schema there is nowhere to put
a reading: a number a file claims about a live load is stale before the file is
saved. `draw_watts` and `capacity_watts` are the nameplate figures a load
schedule is built from, and comparing them with what a meter says is
`netgraph drift`'s business, not the schema's.

### 17.1 Power distribution units

A `pdu` is the power half of what a patch panel is for data: a strip of numbered
holes, bolted in a rack, that things plug into. Like a panel it is shaped by its
numbering rather than by its configuration — a 24-outlet vertical strip is
twenty-four identical facts — so `outlets` takes the same count-or-range
shorthand `ports` does (§15.1), and for the same reason.

It is an element rather than a field on a device because **two devices share
one**, and that sharing is the fact worth drawing. A `power` block on a server
can say "PSU 1 is fed from outlet 7"; only a document for the PDU itself can
answer "what else is on that strip, and is there capacity left". Both questions
are what a load schedule is, and the second is the one that catches a rack fed
from a single unit.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: pdu
metadata:
  name: pdu-r1-a
  description: Left-hand vertical strip, rack 1
  labels: {site: hq, role: power}
  location: {site: hq, room: mdf, rack: r1, rack_height: 42}
spec:
  vendor: APC
  model: AP8959EU3
  form_factor: 0U
  outlets: 24
  capacity_watts: 3680
  input_feed: A
---
apiVersion: netgraph.dev/v1alpha1
kind: pdu
metadata:
  name: pdu-r1-b
  description: Right-hand vertical strip, rack 1
  labels: {site: hq, role: power}
  location: {site: hq, room: mdf, rack: r1, rack_height: 42}
spec:
  vendor: APC
  model: AP8959EU3
  form_factor: 0U
  outlets: 24
  capacity_watts: 3680
  input_feed: B
```

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `vendor` | string | O | `null` | Free text. |
| `model` | string | O | `null` | Free text. |
| `serial` | string | O | `null` | Free text. |
| `form_factor` | string | O | `null` | Descriptive: `vertical`, `horizontal`, `1U`, `0U`. |
| `outlets` | outlet range | M | — | The outlets the unit has: a count (`24`, meaning 1 to 24) or comma-separated spans (`1-24`, `1-12,17-24`). At most 512, no repeats (`NG-E001`). |
| `capacity_watts` | watts | O | `null` | How many watts may be drawn through the unit in total. `NG-E012` sums the declared loads against it; absent means the rating is not recorded, and nothing is graded. |
| `input_feed` | string (≤64) | O | `null` | Which supply feeds the unit — `A`, `B`, `ups-1`, `utility`. Free text; compared only for equality (`NG-E015`). |

**The `outlets` shorthand** is the one §15.1 uses. A bare count `24` means outlets
1 to 24; spans are written `1-24`, `7`, or `1-12,17-24` for a strip with a gap in
its numbering. The zero padding of a span follows its *low* bound, as in §6.2.5,
so a strip printed `01`…`24` is transcribed `01-24` and yields `01 … 24`, while
`1-24` yields `1 … 24`. The two are different labels and are compared as written:
naming outlet `7` on a strip that calls it `07` puts a cord in a hole nobody can
find, so `NG-E011` reports it. At most 512 outlets, with no number declared twice
(`NG-E001`) — the largest strip anybody ships has 54, and the ceiling is what
bounds what a typo can ask for.

**An outlet is not an interface, and nothing is cabled to a PDU.** A patch-panel
position *is* an interface, because a `cable` document terminates on it. An
outlet is not: a power cord is not a `cable`, it carries no frames, and giving a
PDU forty-eight derived interfaces would put it in the layer-1 topology as a node
nothing connects to. So a `pdu` owns no `interfaces` key at all, is not a legal
cable endpoint, and appears in no data layer. The reference goes the other way
instead — a device's `power.inputs` names `pdu:outlet` (§17.2) — which is also
the direction the fact is discovered in: you read the label on the cord, not on
the strip.

**Placement is `metadata.location`, unchanged** (§3.2). A PDU is racked hardware
like anything else, so it takes the same block, and a `position` is what puts it
in a slot of `netgraph render --layer rack` — where the elevation annotates it
with how full the unit is (§17.5). The two strips above name a rack and *no*
`position`, which is the honest model of a 0U vertical unit: it occupies no rack
unit, so it is in the room without being in a slot, it collides with nothing
(`NG-U001`), and it is not drawn on the elevation. A 1U horizontal strip declares
a `position` like any other 1U box, and appears on it.

**Feeds are what make A/B redundancy expressible.** Two PDUs in one rack are only
independent if they are fed from different places, and the inventory has no way to
know whether they are unless somebody writes it down. `input_feed` is free text on
purpose: what counts as "a different feed" is site knowledge — two utility feeds,
two UPS strings, a UPS and a generator — and netgraph's job is to notice that two
feeds a device calls redundant carry the same name (`NG-E015`), not to decide what
the names mean.

### 17.2 Device power

`spec.power` is a device's power in *both directions*, which is the whole shape of
it. A switch takes power through `inputs` and gives it through
`poe_budget_watts` and the `poe` blocks on its ports; an access point takes power
over its uplink and gives none. One block covers all three because they are one
question — where does the power in this box come from and go to — and splitting
it would put two halves of one answer in two places.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: server
metadata:
  name: srv-app-01
  location: {site: hq, room: mdf, rack: r1, position: 10, height: 2, rack_height: 42}
spec:
  vendor: Dell
  model: PowerEdge R660
  interfaces:
    - name: eno1
      type: ethernet
      mtu: 1500
      ipv4:
        addresses: [10.1.10.11/24]
  power:
    draw_watts:
      typical: 240          # steady state, as configured — what a schedule sums
      maximum: 495          # nameplate; what the breaker has to survive
    redundant: true
    inputs:
      - pdu: pdu-r1-a
        outlet: '7'
        psu: psu1
      - pdu-r1-b:7          # the compact form of the same fact
```

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `draw_watts` | watts \| PowerDraw | O | `null` | The nameplate load. A bare number is shorthand for `{typical: <n>}`. |
| `inputs` | list[PowerInput] | O | `[]` | One entry per power supply, naming the outlet feeding it. At most 8. Empty for a device fed over PoE, or one whose feed is not recorded yet (`NG-E016`). |
| `redundant` | boolean | O | `false` | The feeds are meant to be independent: losing one must not lose the device. Needs at least two `inputs` (`NG-E002`) that land somewhere making the claim true (`NG-E015`). |
| `powered_by` | enum | O | `outlet` | `outlet` or `poe`. `poe` says the device takes its power over its uplink, and excludes `inputs` (`NG-E005`). |
| `poe_budget_watts` | watts | O | `null` | The PoE this device can hand out across every PSE port together (§17.3). `NG-E013` checks the ports fit inside it. |

**`draw_watts`** — the nameplate load of the box:

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `typical` | watts | M | — | Steady-state draw of the box as configured. This is the figure a load schedule sums and the one `NG-E012` grades a PDU against. |
| `maximum` | watts | O | `null` | Nameplate or PSU rating — what a breaker has to survive. MUST NOT be below `typical` (`NG-E003`). |

`draw_watts: 45` is accepted as shorthand for `draw_watts: {typical: 45}`. The
typical figure is the one every load schedule is built from and the only one most
nameplates state, so requiring a mapping to say it would be ceremony. A boolean
is refused with an explanation rather than coerced (`NG-E003`), and a wattage is
strictly positive and at most 1 MW (§5): `0 W` is not a load, it is the absence of
one.

**`inputs[]`** — one power supply and the outlet feeding it:

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `pdu` | element ref | M | — | The PDU. A full element reference, so it may be written fully qualified to pick one of several PDUs sharing a short name (§2.2, `NG-E011`). |
| `outlet` | string (1–16, alphanumeric) | M | — | The outlet as the PDU numbers it. Must exist (`NG-E011`) and must not already feed something else (`NG-E010`). |
| `psu` | string (≤64) | O | `null` | Which supply on the *device* this feeds, e.g. `psu1`. Documentation only. |

An entry may be written as the compact string **`pdu:outlet`** — `pdu-r1-a:7`,
`sites/hq/mdf/pdu-r1-a:B12` — or as the equivalent mapping, which is the same
grammar and the same choice a cable endpoint offers (§4.2), because it is the same
kind of fact: a named thing on a named element. Anything that is not one
identifier either side of one colon is `NG-E002`. The mapping form is what a `psu`
label needs, and the label is worth writing: it is what an operator reads off the
back of a chassis, and it is what makes a diagnostic about "the second input" name
something real. `outlet` is alphanumeric rather than digits alone so that a
two-bank unit printed `A1`…`B12` can be transcribed as it is printed.

At most eight inputs. Four-PSU chassis exist; forty do not, and the bound keeps a
copy-paste accident from becoming a load schedule nobody reads. Two of a device's
own inputs naming one outlet is `NG-E002`; two *different* devices naming one
outlet is `NG-E010`.

**`redundant: true`** is a claim, not a description: losing one feed does not lose
the device. It needs at least two `inputs` to be sayable at all (`NG-E002`), and
`NG-E015` checks the claim is true of where they land — two cords into one strip
are not redundant, and neither are two strips on one `input_feed`.

**`powered_by: poe`** is for the box with no power cord — a ceiling access point,
a camera, a doorbell. Its power path *is* the cable that carries its traffic, so
it declares no `inputs` (`NG-E005`), and the feed is derived by walking that
cable rather than declared (§17.4). `NG-E014` then checks the far end of the walk
offers PoE at all, and offers enough of it.

### 17.3 Power over Ethernet

Declaring an `interfaces[].poe` block is what makes a port **power sourcing
equipment**: a port that hands power down the twisted pairs of the run instead of
only frames. It is permitted on `ethernet` and `lag` only (`NG-E006`) — PoE
travels over copper, so a `loopback`, a `vlan` sub-interface, a `bridge` or a
`tunnel` cannot source it, and `wifi` is refused for the same reason and one
further one: a radio is precisely the port with no cable. `lag` is permitted
because an aggregate of two PoE ports is how a multi-gigabit access point is fed.

| Field | Type | Req. | Default | Notes |
|---|---|---|---|---|
| `standard` | enum | M | — | `802.3af`, `802.3at` or `802.3bt` — which amendment the port implements. |
| `class` | integer 0–8 | C | `null` | The IEEE classification. Refused above the ceiling its `standard` defines, and exclusive with `budget_watts` (`NG-E004`). |
| `budget_watts` | watts | C | `null` | An explicit reservation instead of a class, for a vendor that lets an operator cap a port below what its class allows. Exclusive with `class` (`NG-E004`). |
| `enabled` | boolean | O | `true` | Is the port administratively allowed to source power? A disabled PSE port reserves nothing and powers nothing. |

`spec.power.poe_budget_watts` (§17.2) is the other half of the same fact: the pool
the whole box can hand out across every PSE port together. The ports say what each
one wants and the pool says what there is, so `NG-E013` compares the two — and
which ports count towards it is the question the rest of this section answers.

#### The class table

A class fixes two numbers, and they differ by the cable loss the standard budgets
for over 100 m. Both are here because a switch budget is computed from the first
and a device's draw is compared against the second (IEEE 802.3-2022, clauses 33
and 145):

| `class` | PSE reserves | PD may draw | Defined by |
|---|---|---|---|
| `0` | 15.4 W | 12.95 W | `802.3af` — "unclassified"; reserves the class-3 figure |
| `1` | 4.0 W | 3.84 W | `802.3af` |
| `2` | 7.0 W | 6.49 W | `802.3af` |
| `3` | 15.4 W | 12.95 W | `802.3af` |
| `4` | 30.0 W | 25.5 W | `802.3at` ("PoE+") |
| `5` | 45.0 W | 40.0 W | `802.3bt` ("PoE++", Type 3) |
| `6` | 60.0 W | 51.0 W | `802.3bt` (Type 3) |
| `7` | 75.0 W | 62.0 W | `802.3bt` (Type 4) |
| `8` | 90.0 W | 71.3 W | `802.3bt` (Type 4) |

`standard` therefore fixes which classes exist: `802.3af` stops at class 3,
`802.3at` adds class 4, `802.3bt` adds 5 to 8. A class outside its standard is
`NG-E004` — an `802.3af` port cannot deliver class 4, so declaring one is a
mistake about the hardware rather than a preference, and the diagnostic says which
`standard` would make it true.

#### How much a port reserves

Say it with `class` **or** with `budget_watts`, never both (`NG-E004`): a class
already fixes the reservation, and two answers cannot both be the budget.

* With `class`, the port reserves the **PSE-side** figure for that class, which is
  what the switch actually takes out of its pool.
* With `budget_watts`, the port reserves exactly that.
* With **neither**, the port reserves its standard's maximum — 15.4 W for
  `802.3af`, 30 W for `802.3at`, 90 W for `802.3bt`. That is what a switch with no
  per-port configuration does, and assuming less would make an oversubscribed
  budget look fine, which is the one thing this rule exists to prevent.

A disabled port (`enabled: false`) reserves nothing, whichever of the three
applies: a switch does not set power aside for a PSE it is told not to use.
Recording the block anyway is worth it, because `enabled: false` is the difference
between "no PoE here" and "PoE turned off here" — and the second is what `NG-E014`
names when a camera on that port will not come up.

#### Capability versus allocation

**A `poe` block on an empty port is a capability and takes no budget.** A 48-port
PoE+ switch can source 30 W on every port and is sold with a 740 W supply;
counting all 48 would report every real switch as oversubscribed, and a rule that
fires on correct inventories is worse than no rule. Two things do count towards
`poe_budget_watts`:

* a port the walk of §17.4 found something on — power that is actually being
  drawn;
* a port whose `budget_watts` was written down — because writing it down *is* the
  act of reserving it, whether or not anything is plugged in yet.

So `class` describes the port and `budget_watts` commits the pool, which is the
other reason the two are not interchangeable.

#### A PoE access switch

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-access-01
  location: {site: hq, room: mdf, rack: r1, position: 20, rack_height: 42}
spec:
  vendor: Cisco
  model: C9300-24P
  power:
    draw_watts: 90            # the switch itself, before it hands anything out
    poe_budget_watts: 445
    inputs: [pdu-r1-a:11]
  interfaces:
    - name: GigabitEthernet1/0/1
      type: ethernet
      poe: {standard: 802.3at, class: 4}      # reserves 30 W: the AP
    - name: GigabitEthernet1/0/2
      type: ethernet
      poe: {standard: 802.3af, class: 2}      # reserves 7 W: the camera
    - name: GigabitEthernet1/0/3
      type: ethernet
      poe: {standard: 802.3at}                # capable, nothing on it, 0 W held
    - name: GigabitEthernet1/0/24
      type: ethernet
      poe: {standard: 802.3at, enabled: false}
---
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: ap-floor-1
  description: Ceiling access point; no power cord
spec:
  vendor: Ubiquiti
  model: U6-Pro
  power:
    draw_watts: 22
    powered_by: poe           # excludes 'inputs' (NG-E005)
  interfaces:
    - name: eth0
      type: ethernet
      mtu: 1500
    - name: ra0
      type: wifi
      wireless:
        role: ap
        band: 5g
        channel: 36
        bss:
          - ssid: hq-corp
            security: wpa2-psk
---
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: cam-lobby-01
  description: Lobby dome camera
spec:
  vendor: Axis
  model: M3216-LVE
  power:
    draw_watts: 6.2
    powered_by: poe
  interfaces:
    - name: eth0
      type: ethernet
      mtu: 1500
      ipv4:
        addresses: [10.1.60.21/24]
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-sw-ap-01}
spec:
  endpoints: [sw-access-01:GigabitEthernet1/0/1, ap-floor-1:eth0]
  medium: copper
  category: cat6
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata: {name: cbl-sw-cam-01}
spec:
  endpoints: [sw-access-01:GigabitEthernet1/0/2, cam-lobby-01:eth0]
  medium: copper
  category: cat6
```

The switch allocates 37 W of its 445 W pool: 30 W on `1/0/1` and 7 W on `1/0/2`.
`1/0/3` is a capability and `1/0/24` is switched off, so neither holds any. The AP
draws 22 W and the class-4 port delivers 25.5 W PD-side, so it fits; a class-2
port would not, and that is exactly the `NG-E014` this example is shaped to show
the right side of.

### 17.4 How power paths are resolved

Two ways to be fed, and only one of them is written down.

**An outlet feed is declared by the load.** A device's `spec.power.inputs` names
`pdu:outlet` once per power supply. Nothing on the PDU mentions the device, which
is the right direction twice over: it is the direction the fact is discovered in —
you read the label on the cord — and a PDU carrying a list of its own downstream
devices would be a second place for the same fact to be wrong.

**A PoE feed is derived.** A device that says `powered_by: poe` takes its power
over the run that carries its traffic, so nothing declares the feed at all:
netgraph walks that run to the far end and looks for a `poe` block on the port it
lands on. The walk **crosses patch panels** — a run that enters `front/7`
continues from `rear/7`, because a run through a panel is electrically one run,
for power exactly as for frames (§15.2) — since a ceiling access point patched
through an IDF panel is the normal case and not the exotic one. It gives up after
sixteen hops, because a run that long is a loop (`NG-P005`) or a plant nobody
could trace, and the bound is what keeps a cross-wired pair of panels from
hanging the resolver.

A device with two runs to two switches is fed over whichever actually sources
power, and over the more capable of the two when both do. Every run is recorded
whether or not its far end sources anything, because "the uplink lands on a port
with no PoE" is precisely what `NG-E014` has to be able to say.

A reference that does not resolve is *recorded* rather than dropped: the validator
grades it (`NG-E011`) and the renderer still has to draw the feeds that did
resolve, because `--force` must produce a picture.

**Load sharing.** A dual-corded server draws its load through both cords, so each
of a device's *n* feeds carries `typical / n`. That is the figure summed per PDU,
and it is what capacity is graded against (`NG-E012`), because it is the load the
strip carries in normal operation — the state the plant is in on every day that
nothing has failed. The share is computed once per device, so two cords of one
server always add up to exactly its draw whatever the arithmetic rounds to.

It is not the only figure worth having. When the other unit of an A/B pair fails,
this one carries the **whole** load of everything dual-corded to it, each load
counted once at its full draw. That is the failover figure, and both appear in the
utilisation table (§17.6) and the load schedule (§17.7).

**Only the normal-operation figure is graded.** A pair of PDUs each sized for half
the rack is a design, not an error: that is what redundancy costs, and every
correctly built A/B rack would fail a rule that graded the failover number. So
`NG-E012` reports a strip with more plugged into it than it is rated for, the
failover column is reported without a verdict, and the gap between the two *is*
the redundancy plan, stated where somebody can read it.

A PoE feed's reservation is the PSE-side class figure of the port (§17.3), not the
device's draw: that is what the switch sets aside. Its PD-side counterpart is what
`NG-E014` compares a declared draw against.

### 17.5 The power view

`netgraph render --layer power` draws the distribution plant:

* **Nodes** are the PDUs and everything the inventory says draws or sources power.
  A PDU is not in the topology at all — it owns no interfaces (§17.1) — so its
  node is built for this layer, labelled with how many outlets are used, its load
  against its capacity, and its `input_feed`. Everything else is the node it
  already is at layer 1, with its ports, labels and description intact, and only
  gains what it says about power.
* **Edges** are the feeds, drawn distinctly because they are found differently: an
  `outlet` feed is a cord somebody can unplug, and a `poe` feed is the run of
  §17.4 seen electrically.
* Everything else is discarded. A cable is not a power path — two servers joined
  by a patch lead may be on opposite sides of the room electrically — and a PDU is
  joined to the boxes it feeds by cords no data diagram draws.

`--layer rack` (§3.2) annotates each occupied unit with the same plan: a PDU shows
how full it is, everything else shows what it draws. That is the one question an
elevation cannot otherwise answer, which is whether the rack can take another box.

### 17.6 `netgraph list power`

One row per PDU — feed, outlets, used, free, capacity, load, failover, utilisation
and the number of loads — shaped after the `netgraph ipam` utilisation table and
for the same reason: the question is capacity planning, so the columns are what is
there, what is used, what is left, and the percentage that decides whether
anybody has to act. `LOAD` is the normal-operation figure `NG-E012` grades;
`FAILOVER` is what the unit carries when its partner dies (§17.4). A single-fed
rack has the two the same; an A/B pair does not.

### 17.7 The load schedule

`netgraph export power` writes the electrical counterpart of the pull list: one
row per **feed**, with both ends located — which outlet, on which strip, on which
feed, powering which box in which rack unit, drawing how many watts. A
dual-corded server is two rows, which is the point. A PoE-powered camera is a row
too, marked as a PoE feed so a reader summing outlet loads does not double-count
something that occupies no outlet. `--schedule-format csv` is the sheet somebody
prints and initials; `json` is the same rows plus the per-PDU and per-PSE totals.
See [`docs/export.md`](export.md).

### 17.8 Rules

The group is lettered `E`, for *electrical*. `NG-E001` to `NG-E006` are schema
rules, reported while the document is parsed and not suppressible; `NG-E010` to
`NG-E016` are semantic and carry the short ids of §10.10. `NG-E007` to `NG-E009`
are unassigned, and stay that way: numbering the schema half from 1 and the
semantic half from 10 means a rule added to either does not disturb the other,
and an id, once assigned, is never reused (§10).

| ID | Sev. | Rule |
|---|---|---|
| `NG-E001` | error | A `pdu`'s `spec.outlets` is a positive count or comma-separated spans, with no repeats and at most 512 outlets. |
| `NG-E002` | error | `spec.power` is well formed: an `inputs` entry is `pdu:outlet` or the equivalent mapping, no two of the device's own inputs name one outlet, and `redundant: true` declares at least two inputs. |
| `NG-E003` | error | `draw_watts` is a wattage or a `{typical, maximum}` mapping, and `maximum` is not below `typical`. |
| `NG-E004` | error | An `interfaces[].poe` block declares at most one of `class` and `budget_watts`, and `class` is within the ceiling its `standard` defines. |
| `NG-E005` | error | `powered_by: poe` excludes `inputs`: a device fed over its uplink has no cord. |
| `NG-E006` | error | `poe` appears only on a port a cable terminates on — `type: ethernet` or `type: lag`. |
| `NG-E010` | error | One PDU outlet is claimed by at most one element. Two of *one* device's inputs claiming it is `NG-E002` instead. |
| `NG-E011` | error | Every `inputs` entry resolves: the reference names one declared `pdu` — unambiguously, after the §2.2 lookups — and that unit declares that outlet. |
| `NG-E012` | error | A PDU's normal-operation load does not exceed its `capacity_watts` (§17.4). Silent when no capacity is recorded. |
| `NG-E013` | error | The PoE allocated across the ports that hold budget does not exceed the device's `poe_budget_watts`. Silent when no budget is recorded. |
| `NG-E014` | error | A `powered_by: poe` device reaches a PSE port that is enabled and delivers at least what the device declares it draws. |
| `NG-E015` | error | A device claiming `redundant` is fed from two different PDUs, and from two different `input_feed`s where the units record one. |
| `NG-E016` | warning | A device that declares a `draw_watts` also declares a power path — `inputs`, or `powered_by: poe`. |

`NG-E016` is a warning deliberately. Recording draws before recording the outlets
they are plugged into is the normal order in which an as-built document gets
written, and refusing the half-finished state would make the model unusable
exactly while it is being adopted. It is still worth saying: a load that appears
on no PDU appears on no schedule either, so the rack looks emptier than it is.

`NG-E015` accepts two PDUs that record no `input_feed` at all. Silence is not a
claim, and a rule that treated a missing fact as evidence would punish the
inventory for being incomplete rather than for being wrong.

---

## 18. Layout: diagram geometry

Everything above describes the network. This section describes the *drawing* of
it, and it is the one part of the schema that carries no network fact at all.

A diagram netgraph lays out from scratch on every render cannot be arranged: drag
a switch to where it belongs and the next render puts it back, because nothing in
the tree remembers that you moved it. A `kind: layout` document is what remembers.
Once every node in a view has a position, the renderers reproduce the arrangement
exactly — the same coordinates in the SVG, in the HTML and in the JSON export —
and the layout engine is asked to place nothing.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: layout
metadata:
  name: default
spec:
  views:
    l1:
      nodes:
        core/sw-core:  {position: {x: 240, y: 396}}
        core/rtr-edge: {position: [240, 540]}
        subnet:10.0.0.0/24: {position: {x: 480, y: 396}}
      edges:
        core/cbl-uplink: {waypoints: [{x: 240, y: 470}]}
      groups:
        core: {position: {x: 240, y: 468}, size: {width: 220, height: 260}}
```

### 18.1 It is a sidecar, and not an element

A layout is a document kind, like `template` (§6.6), and not an element. It is
never indexed among the elements, never drawn as a node, never listed by
`netgraph list`, never cabled and never counted. It names elements; it does not
join them.

The alternative — `spec.position` on each device — was considered and rejected;
[`docs/follow-ups.md` §16](follow-ups.md) records the four reasons in full. The
short version:

* the same device sits somewhere different in `l1`, `l3` and `routing`, so one
  field on the device cannot hold the answer;
* a subnet node, a rack elevation and a collapsed namespace are drawn but not
  declared, so they have no `spec` to put a position in;
* a device document is a description of hardware, reviewed as one, and four
  numbers per view interleaved with that is noise in every diff forever;
* an arrangement is dropped, regenerated, `.gitignore`d and reviewed as a unit,
  none of which is expressible once it is spread across a hundred files.

Several layout documents may coexist — one per site, one per audience. They are
merged per view; where two place the same node, the first in load order wins and
`netgraph layout` reports the conflict.

### 18.2 Coordinates

Points (1/72 inch). `x` grows rightwards, `y` grows **upwards**, the origin is
the bottom left of the drawing, and a `position` is the **centre** of what it
places.

That is Graphviz's coordinate system, deliberately and exactly. The whole
mechanism rests on being able to hand a stored arrangement straight back to the
layout engine, and a system that needed converting would be a system that could
be converted wrongly.

A point is written `{x: 240, y: 396}` or, as shorthand, `[240, 396]`. A size is
`{width: 220, height: 90}` or `[220, 90]`. Both spellings mean the same thing,
both are read, and `netgraph layout` leaves whichever one it finds alone when the
value has not changed.

### 18.3 Views

`spec.views` is keyed by the layer being drawn — `physical`, `l1`, `l2`, `l3`,
`overlay`, `routing`, `rack`, `power` — because the same device sits somewhere
different in each. The l3 diagram is a different graph with different neighbours,
not the same diagram recoloured.

An unknown view name is `NG-Y003`.

### 18.4 Keys

A node key is an address, spelled the way references are spelled everywhere else
(§2.2): a short name resolved against the layout document's own namespace, or a
fully-qualified one.

A node the inventory does not declare is keyed by the id the graph gives it:

| Key | What it places |
|---|---|
| `subnet:10.0.0.0/24` | a layer-3 prefix node |
| `tunnel:site/wg0` | a tunnel drawn as a node in the overlay view |
| `rack:hq/comms/r1` | a rack elevation |
| `aggregate:sites/north` | a collapsed namespace |

An edge key is a cable's or a tunnel's address, or the synthetic id of a derived
edge. A group key is a namespace.

A key naming nothing the inventory declares is `NG-Y001` — a **warning**, not an
error. Deleting a switch must not make `netgraph validate` fail, and geometry for
a node that is not in the diagram places nothing. `netgraph layout --prune` drops
them.

### 18.5 What is stored

| Key | Required | Meaning |
|---|---|---|
| `nodes.<address>.position` | yes | the centre of the node |
| `nodes.<address>.size` | no | the box it occupies; the label decides when absent |
| `edges.<address>.waypoints` | yes | spline control points, first endpoint to second |
| `groups.<namespace>.position` | yes | the centre of the cluster box |
| `groups.<namespace>.size` | yes | its extent; nothing else decides how big a cluster is |

`size` on a node is optional *and is not written by `netgraph layout --write`*.
Graphviz derives the same box from the same label on every run, so a stored size
buys the renderer nothing and goes stale the moment a device grows an interface.
It is honoured on read, for an editor that lets somebody resize a box on purpose.

Edge waypoints are honoured but not seeded unless `--waypoints` is asked for: a
computed spline is four control points per link that the render recomputes
identically, while a hand-placed bend is a decision worth keeping.

### 18.6 What a renderer does with it

Per view, and decided from the drawing rather than from the document:

| Stored | Mode | What happens |
|---|---|---|
| nothing | `auto` | the graph is laid out from scratch, exactly as it always was |
| some nodes | `partial` | those are pinned and the engine places the rest around them |
| every node | `fixed` | the engine places nothing; the drawing *is* the arrangement |

In `fixed` mode netgraph also draws the namespace cluster frames itself, from the
stored group boxes, because the no-op layout engine does not draw clusters. A
frame is therefore where you put it rather than wherever a layout happened to
land; its caption sits centred above it rather than inside it, for the reason in
[`docs/follow-ups.md` §17](follow-ups.md).

The `json` renderer publishes the coordinates — a `layout` object per node, per
edge and at the top level — so a client can draw the graph without running
Graphviz at all.

### 18.7 Rules

| Rule | Severity | Statement |
|---|---|---|
| `NG-Y001` | warning | Every key names something the inventory declares. `netgraph layout --prune` drops the rest. |
| `NG-Y002` | error | Two layout documents in one namespace do not share a name. |
| `NG-Y003` | error | A view is one of the layers netgraph draws, a coordinate is a finite number, and a size is positive. |
