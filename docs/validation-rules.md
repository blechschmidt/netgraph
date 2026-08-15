# Validation rules

`netgraph validate` answers one question: **is this inventory usable?** It
answers it in three passes, and this document lists every rule each pass can
report, why the rule is worth having, and how to switch it off when your
network is the exception.

* [How findings are reported](#how-findings-are-reported)
* [Pass 1 — discovery](#pass-1--discovery)
* [Pass 2 — schema](#pass-2--schema)
* [Pass 3 — semantics](#pass-3--semantics)
* [Fixing a finding](#fixing-a-finding)
* [Suppressing a rule](#suppressing-a-rule)
* [Where this differs from the specification](#where-this-differs-from-the-specification)

## How findings are reported

Every problem is printed as `location  rule  message`, grouped by severity,
most severe first:

<!-- norun: 'inventory' is an illustrative tree, and the findings are two rules shown together -->
```console
$ netgraph -i inventory validate
errors (1):
  cables/links.yaml#2:8   E001  cable 'cbl-sw-desk' endpoint 'sw-home:port9': 'sw-home' has no interface 'port9'; it declares 'port1', 'port2', …

warnings (1):
  hosts/laptop.yaml#0:1   W103  device 'laptop' terminates no cable and hosts no adapter; it is drawn as an isolated node

1 error, 1 warning
```

`cables/links.yaml#2:8` is the file, the index of the document within it
(0-based, counting `---` separators) and the line. A message names **every**
element involved, not just the anchor, because any of them can suppress it.

| Severity | Meaning | Effect |
|---|---|---|
| `error` | The inventory does not describe a network that could exist. | `validate` exits 1; `render` refuses to draw unless `--force`. |
| `warning` | Legal, but usually a mistake. | Reported; the run succeeds. |
| `info` | Worth knowing. | Reported; the run succeeds. |

`--strict` promotes every surviving warning to an error. Severities can also be
re-graded per inventory — see [Suppressing a rule](#suppressing-a-rule).

For a machine, `-F json`, `-F sarif` and `-F github` report the same findings
with the rule id, the schema alias, the file, the line and the column of the
offending value, and a link back to this document. See [`docs/ci.md`](ci.md).

Rule ids are permanent. Once assigned, an id is never reused for a different
rule, so a suppression written today keeps meaning what it meant.

## Pass 1 — discovery

Walking the folder tree. These are not findings you can configure; they are how
the loader decides what is part of the inventory at all. They are listed here
because "my file is not being loaded" is a validation question in practice.

| ID | Severity | Rule | Why it matters |
|---|---|---|---|
| `NG-L001` | — | Only `*.yaml` and `*.yml` (case-insensitive) are loaded. | A `README.md` or a `.j2` template next to the inventory is not an element. |
| `NG-L002` | — | Path components starting with `.` or `_`, and anything matched by a `.netgraphignore`, are skipped. | Keeps `.git/`, editor backups and work-in-progress drafts out of the graph without moving them elsewhere. |
| `NG-L003` | error | Symlinks are followed, but one that leaves the inventory root or revisits a directory is an error. | A symlink loop would hang the walk; one pointing outside the root would silently pull in documents nobody reviewing the tree can see. |
| `NG-L004` | — | A file may hold several documents separated by `---`; empty ones are skipped but still consume a document index. | The index in `links.yaml#2` keeps pointing at the third `---` block even after one is emptied. |
| `NG-L005` | — | Files load in byte-wise order of their relative path, documents in file order. | Makes every later stage deterministic, so a diagram only changes when the inventory does. |

Two further problems are reported at this stage and always fail the run:

| ID | Severity | Rule | Why it matters |
|---|---|---|---|
| `NG-N002` | error | `metadata.name` is unique within its namespace, across all kinds. The first declaration wins and the duplicate document is dropped. | Two elements answering to one name make every reference to it ambiguous. The diagnostic names both source locations. |
| *(none)* | error | The file is not well-formed UTF-8 YAML, uses an unsupported tag, or repeats a mapping key. | Reported with the rule column `load`. A document that did not parse is missing from the graph entirely. |

A name reused in a *different* namespace is fine. Namespaces come from the
directory layout, so `sites/north/sw-01` and `sites/south/sw-01` coexist.

## Pass 2 — schema

Each document is parsed into the model for its `kind`. Anything here is an
**error** and **cannot be suppressed**: a document that does not parse is not
in the graph, and no severity setting can make a missing element benign.

Where the model attaches a rule id, it appears in the rule column; where it
does not, the column reads `load` and the message carries the field path
(`spec.interfaces[1].ipv4.addresses[0]: exactly one of 'prefix_length' or
'netmask' is required`). The constraint is enforced either way.

### Document and naming

| ID | Rule | Why it matters |
|---|---|---|
| `NG-D001` | The document is a mapping carrying `apiVersion`, `kind`, `metadata` and `spec`. | Anything else is not a netgraph document; guessing at its intent would be worse than refusing it. |
| `NG-D002` | `apiVersion` is a version this build understands (`netgraph.dev/v1alpha1`). | A document written for a later schema may mean something different by the same keys. |
| `NG-D003` | `kind` is one of the ten element kinds or `template`, lower-case. | `kind` selects the shape of `spec`; an unknown kind has no shape to check against. |
| `NG-D004` | `spec` matches the shape required by `kind`. | The whole point of declaring the kind. |
| `NG-D005` | No unknown keys anywhere in the document. | The one failure mode this tool exists to prevent: a misspelt `mtu:`/`mut:` that was silently ignored would produce a diagram that disagrees with the file. |
| `NG-N001` | `metadata.name` matches the name grammar. | Names end up as graph node ids and as the left half of every `device:interface` reference. |
| `NG-N003` | Label and annotation keys match the Kubernetes key grammar; label keys may not use the reserved `netgraph.dev/` prefix. | Labels are user vocabulary that selectors match on; the tool keeps its own prefix so a future built-in label cannot collide with yours. |

### Interfaces

| ID | Rule | Why it matters |
|---|---|---|
| `NG-I001` | Interface names are unique within their element. | A cable endpoint names one interface; two candidates make the reference meaningless. |
| `NG-I002` | `parent` is present exactly for `type: vlan`, is not the interface itself, and resolves to an interface on the same element. | A sub-interface without a parent has nothing to be a sub-interface of. |
| `NG-I003` | `members` is present exactly for `type: lag` and `type: bridge`, is non-empty, free of duplicates, does not include the aggregate itself, and every entry resolves to an interface on the same element. | An aggregate is defined entirely by what it aggregates. |
| `NG-I011` | `mtu` is within 68–65535, and at least 1280 when the interface carries IPv6 addresses. | RFC 8200 sets 1280 as the IPv6 minimum link MTU; below it, IPv6 on that interface cannot work. |

### Addresses

| ID | Rule | Why it matters |
|---|---|---|
| `NG-A001` | Exactly one of `prefix_length` or `netmask` per IPv4 address; `prefix_length` is mandatory for IPv6. | An address without a prefix says nothing about which subnet it is in, which is most of what an address is for. |
| `NG-A002` | Addresses are unique within one address family on one interface. | `ip` is RFC 8344's list key: a repeat is not a second address, it is a contradiction. |
| `NG-A003` | A `netmask` is contiguous. | Non-contiguous masks are a feature flag in RFC 8344 and unsupported hardware behaviour nearly everywhere else. |

### VLANs

| ID | Rule | Why it matters |
|---|---|---|
| `NG-V001` | `vlans[].id` is unique within a device. | The VLAN database is a keyed list; a duplicate id means two names for one VLAN. |
| `NG-V002` | `access_vlan` in access mode, `trunk_vlans` in trunk mode — never crossed. | The two describe incompatible port behaviours; a port that declared both would have no single correct expansion to 802.1Q. |
| `NG-V003` | `native_vlan` appears only in trunk mode. | A native VLAN is the untagged VLAN *of a trunk*; on an access port every frame is untagged already. |

### Cables

| ID | Rule | Why it matters |
|---|---|---|
| `NG-C001` | `endpoints` has exactly two entries. | A cable with one end goes nowhere; one with three is a hub, which is a separate kind. |
| `NG-C007` | `length_m` and `category` are absent when `medium: wireless`. | Radio has no cable to measure or specify. |

### Wireless

Checked while a `wifi` interface's `wireless` block is parsed (schema §6.2.6).

| ID | Rule | Why it matters |
|---|---|---|
| `NG-W001` | `ssid` is between 1 and 32 octets. | 802.11 carries the SSID as a counted octet string; a longer name cannot be beaconed, and the limit is on bytes, so a non-Latin name runs out sooner than its length suggests. |
| `NG-W002` | `wireless` appears only on `type: wifi`. | The block describes a radio. On an ethernet port it would be configuration nothing implements. |
| `NG-W003` | `channel` names `band`, and is a channel that band numbers. | Channel 1 exists at 2.4 GHz and at 6 GHz and means two different frequencies; without the band there is nothing to resolve it against. |
| `NG-W004` | `width_mhz` names `band`, and is a width that band supports. | There is no room for 80 MHz at 2.4 GHz, and 320 MHz is an 802.11be feature of the 6 GHz band alone. |
| `NG-W005` | `ssid` and `bssid` are each unique within one radio. | The BSS list is keyed by both; a repeat is two descriptions of one service set. |
| `NG-W006` | A `station` or `mesh` radio lists at most one BSS. | A client radio is associated to exactly one BSS at a time. Several entries describe a history, not a state. |

### Hubs

A hub is a layer-1 repeater. It has no MAC table, no VLAN awareness and no IP
stack, so declaring any of those would describe hardware that is not a hub.

| ID | Rule | Why it matters |
|---|---|---|
| `NG-H001` | A hub interface must not declare `vlan`. | A repeater cannot tag, filter or assign VLANs. |
| `NG-H002` | A hub interface must not declare `ipv4` or `ipv6`. | No IP stack to hold the address. |
| `NG-H003` | A hub must not declare `bridge`, `vlans` or `forwarding`. | Same reason, at device level. |
| `NG-H004` | Every hub interface is `type: ethernet`. | The other types are all logical constructs a repeater does not implement. |

### Adapters

| ID | Rule | Why it matters |
|---|---|---|
| `NG-X001` | `upstream.attached_to` is a bare element name, never a `device:interface` reference. | An adapter plugs into a *host*, not into one of its network ports; the name grammar rejects the colon. |
| `NG-X003` | Every downstream interface is `type: ethernet`, `wifi` or `lag`. | An adapter presents physical ports; loopbacks and SVIs belong to the host's own stack. |
| `NG-X004` | `upstream.name` does not collide with any downstream `interfaces[].name`. | Both are reachable as `adapter:port`, so a collision makes the cable endpoint ambiguous. |

### Power

Checked while a `pdu` document, a device's `spec.power` block or an interface's
`poe` block is parsed (schema §17).

| ID | Rule | Why it matters |
|---|---|---|
| `NG-E001` | A PDU's `spec.outlets` is a count (`24`) or comma-separated `[low-high]` spans (`1-12,17-24`), non-inverted, free of repeats, and totals at most 512 outlets. | Outlets are referred to by number from a device's `power.inputs`, so a strip whose numbering did not expand has no holes for anything to plug into. The bound is what stops `1-99999999` from becoming a load schedule nobody can read. |
| `NG-E002` | An entry of `power.inputs` is `pdu:outlet` with exactly one colon; no two inputs of one device name the same outlet; `redundant: true` requires at least two inputs. | The reference is the same grammar a cable endpoint uses, for the same reason: a named thing on a named element. A device cannot plug two of its own supplies into one hole, and it cannot survive losing a feed it only has one of. |
| `NG-E003` | `draw_watts` is a number of watts rather than a boolean, and `maximum` is not below `typical`. | `typical` is what a load schedule sums and `maximum` is what a breaker has to survive; a maximum under the typical draw is a transposition of the two, and a load schedule built from it is wrong in the safe-looking direction. |
| `NG-E004` | A `poe` block states its reservation once — a `class` or a `budget_watts`, never both — and the class is one its `standard` defines. | Two answers cannot both be the budget. A class above the standard's ceiling — class 4 on an `802.3af` port — is a mistake about the hardware rather than a preference: that port cannot deliver 30 W however it is configured. |
| `NG-E005` | `powered_by: poe` excludes `inputs`. | A device fed over its uplink has no power cord, so an outlet feeding it is a contradiction rather than extra detail; one of the two facts is wrong and the file cannot say which. |
| `NG-E006` | `poe` appears only on `type: ethernet` or `type: lag`. | PoE travels over the twisted pairs of a run, so a loopback, an SVI, a bridge, a tunnel or a radio has no copper to hand power down. `lag` is allowed because an aggregate of two PoE ports is how a multi-gigabit access point is fed. |

### Ranges and templates

Checked while the document is rewritten into the shape the models validate:
`interfaces[].range` is expanded and `spec.from` is merged (schema §6.2.5,
§6.6). Because the rewrite happens before parsing, these are reported by every
command that loads an inventory, and they cannot be suppressed either.

A diagnostic on a field a template supplied names the **template's** file and
line, with a note saying which device inherited it. That is the point of the
feature: fifty devices sharing one template do not report fifty copies of its
one mistake.

| ID | Rule | Why it matters |
|---|---|---|
| `NG-R001` | An interface entry declares exactly one of `name` and `range`. | Both would leave it unclear whether the entry is one interface or forty-eight. |
| `NG-R002` | `range` carries one to four well-formed, non-inverted `[low-high]` spans and no stray bracket. | A silently ignored bracket would produce an interface literally named `eth[0-47]`. |
| `NG-R003` | A document expands to at most 4096 interfaces. | `eth[1-99999999]` is a typo, and the answer to a typo is a diagnostic rather than an out-of-memory kill. |
| `NG-R004` | An expanded name collides with nothing else on the element. | Two ports answering to one name make every cable endpoint naming it ambiguous. Both source locations are quoted. |
| `NG-R005` | Every `{...}` in a range `description` is empty or names a span the range declares, and braces are paired. | `{1}` on a one-span range is a mistake worth catching; a lone brace is almost always one too. |
| `NG-M001` | `spec.from` names exactly one `kind: template` document. | A reference that resolves to nothing, or to two things, cannot be merged. |
| `NG-M002` | Template names are unique within their namespace. | Same reason as `NG-N002`, in the separate index templates live in. |
| `NG-M003` | Template inheritance through `from` is acyclic. | A cycle has no far end to start merging from. |
| `NG-M004` | A device only inherits from a template that itself resolved. | The template's own errors are reported once, against the template; the device says only that it cannot use it. |
| `NG-M005` | A `template` document's `spec` is a mapping of device-spec keys. | It is a partial device spec; a key no device has could never be merged into one. |
| `NG-M006` | `spec.from` appears only on the five device kinds. | A cable has no device spec for a template to contribute to. |
| `NG-K001` | Test suite names are unique within their namespace. | Same reason as `NG-N002`, in the separate index suites live in. The first declaration wins, which keeps loading deterministic. |
| `NG-K002` | A `testsuite` document's `spec.assertions` holds between one and 1024 entries. | A suite that asserts nothing reports a green run having checked nothing, which is worse than no suite at all. |
| `NG-K003` | Every key an assertion carries belongs to the assertion its `assert` names, every key that assertion requires is present, and a `count` compares against a bound it can satisfy. | `hops` on a `same-vlan` is a bound nobody is checking; ignoring it silently is how a suite comes to mean less than it reads. |

## Pass 3 — semantics

Every document has parsed. Do they agree with **each other**? These
eighty-two rules are the only ones that can be suppressed, re-graded or
disabled — they are judgements about a whole inventory rather than facts about
one document.

Some of them are judgements about a whole *element* rather than a whole
inventory — a stacking cycle needs nothing but the device that declares it.
They live here rather than in [pass 2](#pass-2--schema) because they are
judgements: a network can be built that way, badly, and an inventory that means
to describe it must be able to say so.

`netgraph rules` prints this table from the same source the validator uses.

### Errors

#### `E001` — unknown cable endpoint

*Alias: `NG-C002`, `NG-C003`. Severity: error.*

A cable endpoint names a device that is not declared, an element that owns no
interfaces (another cable), an interface that element does not have, or a name
that stays ambiguous after the namespace lookup.

**Why it matters.** This is the single most common inventory mistake: a port
renamed on the device but not in the file. The cable is silently absent from
the graph, so the diagram shows a host that is not connected to anything — the
exact wrong answer, delivered confidently. The message lists the interfaces the
element *does* declare, which usually makes the typo obvious.

**Suppress with** `E001` or either alias. Annotating the *cable* works, and so
does annotating the element the endpoint was meant to name.

#### `E002` — interface terminated by more than one cable

*Alias: `NG-C005`. Severity: error.*

Two cables land on the same `element:interface`, or both ends of one cable land
on the same port.

**Why it matters.** A physical port takes one cable. Two means one of the two
cable documents is stale — usually a link that was re-patched and documented
twice — and the graph would draw a topology that cannot be built.

**Suppress with** `E002` / `NG-C005`, or an annotation on either cable or on
the element holding the port.

#### `E003` — duplicate MAC address

*Alias: `NG-I008`. Severity: error.*

Two interfaces anywhere in the inventory declare the same `mac`. Interfaces in
one stacking group — a LAG and its members, a VLAN sub-interface and its parent
— legitimately share one hardware address and are counted once.

**Why it matters.** Two live interfaces in one broadcast domain with the same
MAC make the forwarding table flap between ports; traffic goes to whichever
spoke last. In an inventory it almost always means a copy-pasted device
document whose addresses were never edited.

**Suppress with** `E003` / `NG-I008`, or an annotation on either element.
VRRP and CARP virtual addresses are the legitimate case.

#### `E004` — duplicate IP address

*Alias: `NG-A004`. Severity: error.*

The same address, in the same prefix, in the same VLAN, on two interfaces.
Re-using a prefix in a *different* VLAN is not a clash, and loopback addresses
(`127.0.0.1`, `::1`) are exempt — they are scoped to the host that holds them.

**Why it matters.** An address collision inside one broadcast domain breaks
both hosts intermittently and is miserable to diagnose from the outside. The
scoping by VLAN means the rule stays quiet for the ordinary case of the same
RFC 1918 prefix re-used per site.

**Suppress with** `E004` / `NG-A004`, or an annotation on any element involved.
Anycast and VRRP are the legitimate cases.

#### `E005` — VLAN mismatch across a link

*Alias: `NG-C011`. Severity: error.*

The two ends of a link do not agree about VLANs, in one of four ways:

| Shape | What the link actually carries |
|---|---|
| Two **access** ports with different `access_vlan`s | Nothing: the ends are in different broadcast domains. |
| An **access** port facing a **trunk** | At most the trunk's native VLAN; every tagged VLAN is dropped at the access end. |
| Two **trunks** whose VLAN sets are disjoint | Nothing: no VID is a member of both. |
| Two **trunks** that each name a `native_vlan`, and name different ones | Tagged VLANs cross correctly; untagged frames cross *between* two broadcast domains. |

When an endpoint is a LAG member the check uses the aggregate's configuration,
because VLAN membership is a property of the bundle rather than one lane.

**Why it matters.** Each shape produces a link that looks perfectly cabled and
carries less than the diagram implies — in the first three cases, nothing at
all. A port left in VLAN 1 after a move is the classic cause of the first; the
second is what a switchport reconfigured at one end only looks like.

**Suppress with** `E005` / `NG-C011`, or an annotation on the cable or on
either device. An access port deliberately parked on a trunk's native VLAN is
the legitimate case for the second shape, and a trunk whose two ends name the
native VLAN differently on purpose — vendor defaults differ — for the fourth.

Two exemptions keep the rule quiet where the network is not the exception:

* A port with no `vlan` block at all is not a mismatch. An untagged host facing
  an access port is the normal pairing, and the host correctly says nothing
  about VLANs.
* A `native_vlan` that only *one* end spells out is not a mismatch either.
  Leaving it off means "the default", and reporting the pair would fire on
  every trunk written against a vendor configuration that states it separately.

#### `E006` — adapter over capacity

*Alias: `NG-X008`. Severity: error.*

An adapter declares more entries in `interfaces` than `spec.ports` says the
hardware has. Not checked when `ports` is omitted.

**Why it matters.** It catches an inventory that outgrew the device — ports
added to the document as the network grew, past what the dongle physically has.
Declaring `ports` is opt-in precisely so that this check exists only where you
have stated the ground truth.

**Suppress with** `E006` / `NG-X008`, or an annotation on the adapter.

#### `E007` — cyclic interface stacking

*Alias: `NG-I004`. Severity: error.*

The `parent`/`members` graph of one element contains a loop: `vlan-a` is a
sub-interface of `vlan-b` which is a sub-interface of `vlan-a`, or two LAGs each
list the other as a member. The message spells the loop out as a chain.

**Why it matters.** [Pass 2](#pass-2--schema) rejects only the one-step case —
`NG-I002` forbids an interface being its own `parent`, `NG-I003` an aggregate
listing itself. A longer loop leaves every individual document legal while
describing hardware in which each interface would have to sit on top of the
next. Nothing can be built from it, and anything that walks the stack has to
defend itself against it.

**Suppress with** `E007` / `NG-I004`, or an annotation on the element. There is
no legitimate case; if this fires, one of the `parent` or `members` entries
names the wrong interface.

#### `E008` — a member is not free to be aggregated

*Alias: `NG-I005`. Severity: error.*

A port listed in a `lag`'s or `bridge`'s `members` is not available to be
aggregated, in one of three ways: it is claimed by a second aggregate as well,
it is an aggregate itself, or a `type: vlan` sub-interface is stacked on it.

**Why it matters.** An aggregate owns its members' frames. Two of them cannot
own the same port; a sub-interface of an enslaved port would be waiting for
traffic the aggregate has already taken. Each shape describes hardware that
cannot be built, and each is what a half-finished re-cabling looks like — the
member moved to the new bond, the old bond never edited.

A `lag` inside a `bridge` is the one legitimate nesting and is exempt: `br0`
with `members: [bond0, eth2]` is how every Linux host bridges a bond.

**Suppress with** `E008` / `NG-I005`, or an annotation on the element.

#### `E009` — sub-interface VLAN not carried by its parent

*Alias: `NG-V005`. Severity: error.*

A `type: vlan` interface encapsulates a VID that its `parent` does not carry:
the parent is not a trunk at all, or trunks a set the VID is not in.

A `bridge` parent — where an SVI normally hangs — is resolved through its
members, so `Vlan99` on `br0` is carried as long as some port of the bridge is
in VLAN 99. A parent trunking `all` carries everything and can never be wrong.

**Why it matters.** A sub-interface receives exactly the frames its parent tags
with that VID. If the parent never tags them, the sub-interface is configured,
addressed, drawn in the diagram, and dead. It is the usual result of adding a
VLAN to a router and forgetting the switchport.

**Suppress with** `E009` / `NG-V005`, or an annotation on the element.

#### `E010` — multicast MAC address

*Alias: `NG-I009`. Severity: error.*

Bit 0 of a MAC's first octet — the least-significant bit, so an odd first octet
— marks a group address (IEEE 802-2014 §8.2). `01:00:5e:00:00:01` is one.

**Why it matters.** A group address can be a frame's destination but never its
source, so no interface can have one. It is always a mistyped or misread octet.

Graded an **error**, where §10.2 of [the schema](schema.md) proposes a warning.
This follows the precedent of `E003` and `E004` (§10.10): those are re-graded
because a typo is likelier than a deliberate VRRP design, and this one has not
even got the deliberate design — there is no configuration in which a multicast
source address is what was meant.

**Suppress with** `E010` / `NG-I009`, or an annotation on the element.

#### `E011` — medium disagrees with the endpoint type

*Alias: `NG-C006`. Severity: error.*

`medium: wireless` requires **both** endpoints to be `type: wifi`, and any other
medium requires **neither** of them to be. An adapter's upstream port is a host
bus rather than a radio, so it counts as wired.

**Why it matters.** A wireless cable is not a cable: §7.1 uses it to model one
station's *association* with an access point, which is why renderers draw it
dashed and why `length_m` and `category` are refused on it (`NG-C007`). Pointed
at a copper port it describes a radio link to something with no antenna;
conversely a copper run into a `wifi` port has nowhere to plug in. Both are what
a medium corrected on the cable but not on the port — or the reverse — looks
like.

**Suppress with** `E011` / `NG-C006`, or an annotation on the cable or either
element.

#### `E012` — cable terminates on an interface with no socket

*Alias: `NG-C009`. Severity: error.*

An endpoint is a `loopback`, `vlan` or `bridge` interface. Only `ethernet`,
`wifi` and `lag` can be cabled — and cabling a `lag` is its own finding,
[`W119`](#w119--cable-terminates-on-a-lag-aggregate).

**Why it matters.** Those three types are software: a loopback has no medium,
and an SVI or a bridge sits *above* the ports that do. The cable describes a
plug with nowhere to go, and the physical port it was meant for is left looking
free — so the diagram is wrong twice. The message lists the ports of the element
that can actually take the cable.

**Suppress with** `E012` / `NG-C009`, or an annotation on the cable or the
element holding the port. There is no legitimate case.

#### `E013` — host attachment declared twice

*Alias: `NG-X005`. Severity: error.*

A cable terminates on an adapter's `upstream` port while `upstream.attached_to`
is set as well.

**Why it matters.** §8.2 says the attachment is declared exactly once:
`attached_to` *is* the graph edge, and no cable document is needed or permitted
for it. Both spellings at once give the adapter two upstream links where the
hardware has one plug, and once they drift apart — the dongle is moved, one of
the two documents is edited — nothing says which of them is current.

**Suppress with** `E013` / `NG-X005`, or an annotation on the adapter or the
cable. Cabling the upstream port *instead of* setting `attached_to` is legal and
silent: that is how a media converter or a dock fed from another adapter is
written.

#### `E014` — cyclic adapter attachment

*Alias: `NG-X006`. Severity: error.*

The `attached_to` references form a loop: a dock plugged into a dongle plugged
back into the dock. The message spells the loop out as a chain.

**Why it matters.** No chain of adapters in the loop ever reaches a host, so
there is no machine the ports belong to. Everything that walks the chain — the
renderer's adapter collapsing, the VLAN propagation of §8.2 — would have to
defend itself against a walk that never terminates.

**Suppress with** `E014` / `NG-X006`, or an annotation on any adapter in the
loop. There is no legitimate case; one of the `attached_to` values names the
wrong element.

#### `E015` — `attached_to` names nothing that could host the adapter

*Alias: `NG-X001`. Severity: error.*

`upstream.attached_to` names an element that is not declared, a short name that
stays ambiguous after the namespace lookup (§2.2), or an element that owns no
interfaces — a cable.

[Pass 2](#pass-2--schema) checks the *grammar* of the reference under the same
id: a bare element name, never a `device:interface`. Whether it lands on
anything is a question about the whole inventory, so it is answered here and is
suppressible, while the grammar is not.

**Why it matters.** The renderer drops an attachment it cannot resolve, so
without this rule the host would be drawn floating next to its own dongle with
nothing but a `dropped from the graph:` line on stderr to say why. It is the
`attached_to` twin of [`E001`](#e001--unknown-cable-endpoint), and it has the
same usual cause: a machine renamed in its own document and not in the
adapter's.

**Suppress with** `E015` / `NG-X001`, or an annotation on the adapter. There is
no legitimate case — an adapter that is genuinely plugged into nothing should
leave `attached_to` out, which says so exactly.

#### `E016` — unknown tunnel endpoint

*Alias: `NG-T002`. Severity: error.*

A tunnel endpoint names an element that is not declared, an element that owns no
interfaces (a cable, or another tunnel), an interface that element does not
have, or a name that stays ambiguous after the namespace lookup.

**Why it matters.** The `tunnel` twin of
[`E001`](#e001--unknown-cable-endpoint), and it fails the same way: the graph
layer drops a tunnel whose ends it cannot resolve, so the diagram shows two
sites with no overlay between them and says nothing about why. The message lists
the interfaces the element *does* declare.

**Suppress with** `E016` / `NG-T002`. Annotating the *tunnel* works, and so does
annotating the element the endpoint was meant to name.

#### `E017` — tunnel endpoint is not a tunnel interface

*Alias: `NG-T003`. Severity: error.*

A tunnel endpoint resolves to an interface whose `type` is not `tunnel` —
usually the physical port the outer packets leave by.

**Why it matters.** The endpoint of a tunnel is the *virtual* interface the
operating system presents: `wg0`, `ipsec0`, `vxlan100`. Landing the tunnel on
`eth0` instead draws the overlay on top of the very link that carries it, and
puts the tunnel's inner addresses on the underlay port, where the layer-3 view
would then place both in the wrong subnet. Declare a `tunnel` interface and, if
you want to record which port the outer packets use, point its `parent` at the
physical one.

**Suppress with** `E017` / `NG-T003`, or an annotation on the tunnel or on the
element holding the port.

#### `E018` — `over` names no tunnel

*Alias: `NG-T004`. Severity: error.*

`spec.over` names an element that is not declared, one that is not a tunnel, or
a short name that stays ambiguous after the namespace lookup (§2.2).

**Why it matters.** `over` is the only thing that says a tunnel is nested. A
reference that resolves to nothing silently demotes `vxlan over ipsec` to a bare
VXLAN, which changes the answer to the question the diagram is most often drawn
to answer — is this traffic encrypted? A tunnel that genuinely runs straight
over the physical topology omits `over`, which says so exactly.

**Suppress with** `E018` / `NG-T004`, or an annotation on the tunnel.

#### `E019` — cyclic tunnel encapsulation

*Alias: `NG-T005`. Severity: error.*

The `over` references form a loop: a VXLAN carried by an IPsec tunnel carried by
that VXLAN. The message spells the loop out as a chain.

**Why it matters.** No tunnel in the loop ever reaches the underlay network, so
none of them can carry a packet. Everything that walks the chain — the
encapsulation stack a rendering prints, the MTU budget of
[`W126`](#w126--tunnel-mtu-does-not-fit-its-underlay), the protection lookup of
[`W127`](#w127--tunnel-carries-traffic-in-the-clear) — would otherwise have to
defend itself against a walk that never terminates.

**Suppress with** `E019` / `NG-T005`, or an annotation on any tunnel in the
loop. There is no legitimate case; one of the `over` values names the wrong
tunnel.

#### `E020` — first hop is not on-link

*Alias: `NG-A013`. Severity: error.*

An interface declares `ipv4.gateway` or `ipv6.gateway`, and that address is
inside none of the prefixes the same interface configures for the same family.

**Why it matters.** A first hop is reached by ARP or by neighbour discovery,
never by routing — that is the whole point of it. An address outside every
on-link prefix therefore cannot be resolved, so the host has no way to send the
packet that would teach it how to reach the gateway. The two usual causes are a
prefix length shortened without the gateway being moved, and a gateway copied
from the subnet next door.

An IPv6 link-local gateway is exempt: `fe80::1` is on-link by definition, and
the interface's own link-local address is autoconfigured rather than written
down, so there is no declared prefix for it to be inside of.

Reported by [`netgraph ipam`](ipam.md) as well as by `netgraph validate`; the
IPAM report calls this rule rather than re-deriving it.

**Suppress with** `E020` / `NG-A013`, or an annotation on the element. The
legitimate case is an unnumbered or point-to-point link whose peer address is
deliberately outside the local prefix — rare enough to be worth annotating.

#### `E021` — cable on a position the patch panel does not have

*Alias: `NG-P001`. Severity: error.*

A cable terminates on a patch-panel position that `spec.ports` does not
declare, or on a port name that is not `front/<n>` or `rear/<n>`.

**Why it matters.** A panel's positions are derived from a range rather than
written out, so `front/25` on a 24-position panel is not a mistake anyone can
see by looking at the panel document — it is only visible next to the range,
which is what this diagnostic puts in front of the reader. The consequence is
worse than a missing device port: because a panel is spliced out below
`--layer physical`, the run simply disappears from the diagram and the switch
port at the near end looks free.

This is `E001`'s job everywhere else. It is separate here because listing the
48 interface names of a 24-position panel would bury the one fact that matters,
which is the range.

**Suppress with** `E021` / `NG-P001`, or an annotation on the cable or the
panel. There is no legitimate case: a plug goes in a hole that exists.

#### `E022` — patch-panel position terminated twice

*Alias: `NG-P003`. Severity: error.*

Two cables are patched into the same position of one panel.

**Why it matters.** A coupler takes one plug per side. This is the same
impossibility `E002` reports about a device port, and it is worse here for the
same reason `E021` is: the panel is invisible below `--layer physical`, so
rather than an obviously overloaded port the reader gets a run silently spliced
through whichever cable happened to be declared first, and a second run that
vanishes.

**Suppress with** `E022` / `NG-P003`, or an annotation on either cable or on
the panel. No legitimate case exists.

#### `E023` — patch panel where an active element is required

*Alias: `NG-P004`. Severity: error.*

An adapter's `upstream.attached_to`, or a tunnel endpoint, names a patch panel.

**Why it matters.** A panel is passive. It has no host bus for a dongle to hang
off and no operating system to terminate a WireGuard or IPsec tunnel on, so
both spellings describe hardware that does not exist. Both come from the same
misreading — treating the panel as the device on the other side of it — and the
fix is the same: name the active element, and let the panel carry the cable
segments between them.

**Suppress with** `E023` / `NG-P004`, or an annotation on the adapter, the
tunnel or the panel. No legitimate case exists; a media converter that looks
like it wants this spelling is an `adapter` with `passthrough: false` (§8.2).

#### `E024` — patch run loops back into its own panel

*Alias: `NG-P005`. Severity: error.*

Following a run through the couplers arrives back at a cable segment it has
already crossed.

**Why it matters.** A run has to reach something that can send or receive. One
that closes on itself never will: it is a circle of copper between holes, and
at layer 2 it is a broadcast storm waiting for the last cable to go in. The
graph layer drops such a run rather than splicing it, so without this rule the
only trace of it would be a link that quietly is not drawn.

The usual cause is a rear-to-rear patch between two panels that were already
joined front to front — the tie cable that was added twice, from each end.

**Suppress with** `E024` / `NG-P005`, or an annotation on any cable in the run
or on either panel. No legitimate case exists.

#### `E025` — two elements occupy the same rack unit

*Alias: `NG-U001`. Severity: error.*

Two elements whose `metadata.location` names the same `site`, `room` and `rack`
claim overlapping units.

**Why it matters.** Two things cannot be bolted to the same four screw holes.
In practice this catches the `position` copied from the row above and never
changed, and the 2U server whose `height` was left at the default of 1 — both
of which produce an elevation that looks plausible and is off by one for
everything above the collision.

`position` is the **lowest** unit an element occupies and `height` counts
upwards, so a 2U device at `position: 10` fills U10 and U11.

**Suppress with** `E025` / `NG-U001`, or an annotation on either element. The
one case worth annotating is two half-width devices sharing a shelf, which the
model has no way to express.

#### `E026` — element mounted above the top of its rack

*Alias: `NG-U002`. Severity: error.*

An element's highest unit — `position + height - 1` — is above the
`rack_height` declared for its rack.

**Why it matters.** It does not fit. The arithmetic is exactly the part a
person does by hand and gets wrong, which is the whole reason
`metadata.location` is structured rather than free text: a 4U panel at U40 of a
42U cabinet ends at U43.

The rule is silent when no element in the rack declares `rack_height`: without
a declared top there is no bound to check against, and inventing one from the
tallest occupant would only ever agree with itself.

**Suppress with** `E026` / `NG-U002`, or an annotation on the element. No
legitimate case exists once the height is declared; if the cabinet really is
taller, correct `rack_height`.

#### `E027` — rack declared with two heights

*Alias: `NG-U003`. Severity: error.*

Two elements in one rack declare different values for
`metadata.location.rack_height`.

**Why it matters.** A rack has one height. Until the disagreement is settled
`E026` has no bound it can trust, so a second, quieter error is hiding behind
this one. The usual causes are a `rack` name reused in another room — in which
case `site` or `room` is what is wrong, not the height — and a number that was
guessed on one document and measured on another.

**Suppress with** `E027` / `NG-U003`, or an annotation on any of the elements
involved. No legitimate case exists.

#### `E028` — wireless link is not an association

*Alias: `NG-W007`. Severity: error.*

A `medium: wireless` cable joins two radios that are not one `ap` and one
client: either both beacon, or neither does. Checked only once **both** ends
declare a `wireless` block — an absent block says "not modelled", and
[`E011`](#e011--medium-disagrees-with-the-endpoint-type) already owns the case
where an end is not a radio at all.

**Why it matters.** An 802.11 link has a direction a cable does not: one radio
beacons and the other joins it. Two access points on one link is a description
of interference rather than of a link, and two clients is a link no frame ever
crosses, because neither end will beacon for the other to find. Both are what a
copy-pasted radio block looks like. A mesh node's backhaul is the legitimate
cousin of the second case and is written as `role: mesh` against the
`role: ap` radio it associates to.

**Suppress with** `E028` / `NG-W007`, or an annotation on the cable or either
element.

#### `E029` — duplicate BSSID

*Alias: `NG-W008`. Severity: error.*

Two `ap` radios in the inventory advertise the same `bssid`. Client radios are
exempt: a station's BSS entry records the BSSID it *joined*, so repeating the
access point's is what makes it the same service set.

**Why it matters.** The wireless `E003`. A BSSID identifies one basic service
set to every client in earshot, so two of them answering to one address means
frames for one arrive at the other and a roam between them is invisible — to
the client, and to anyone reading a capture. In practice it is a second access
point cloned from the first with the radio section left untouched. Repeats
*within* one radio never reach here: `NG-W005` rejects the document.

**Suppress with** `E029` / `NG-W008`, or an annotation on either element.

#### `E030` — SSID VLAN is carried nowhere on the access point

*Alias: `NG-W009`. Severity: error.*

An `ap` radio maps an SSID to a VLAN, and no interface of that access point is
a member of it. An access point with a port trunking `all` carries whatever is
asked of it and is exempt.

**Why it matters.** An SSID with a `vlan` is a bridge between the air and that
VLAN. If no port of the device is in it, the far side of the bridge is missing:
clients associate, get an address from nowhere and reach nothing — while the
inventory, and the access point's own configuration page, both list the network
as present. The usual cause is a guest SSID added to the radio without adding
the VLAN to the uplink trunk.

[`W113`](#w113--undeclared-vlan-referenced) is the neighbouring, weaker
statement: the VLAN is not in the device's `vlans` database. This one is about
the ports.

**Suppress with** `E030` / `NG-W009`, or an annotation on the access point.

#### `E031` — associated to an SSID the access point does not advertise

*Alias: `NG-W010`. Severity: error.*

A client radio's BSS names an SSID that the `ap` radio at the other end of the
link does not beacon. An access point that lists no BSS at all is not modelling
its SSIDs, so there is nothing to contradict and the rule stays quiet.

**Why it matters.** The association names a BSS, and the BSS is the access
point's to define. An SSID that appears on the client and nowhere on the AP is
either a typo or a record of the network as it used to be — a renamed SSID that
the client documents never caught up with. Either way the link drawn from it
does not exist, and the layer-2 diagram labels it with a network that is not on
the air.

**Suppress with** `E031` / `NG-W010`, or an annotation on either element.

#### `E032` — next hop is not on-link

*Alias: `NG-F008`. Severity: error.*

A static route's `via` lies outside every prefix the device configures in the
route's own routing instance, on the interface the route names if it names one.
Three exemptions, each for a next hop that is on-link *by definition* rather
than by a prefix somebody wrote down: an IPv6 link-local next hop (`fe80::1`),
which is how an unnumbered link is written; a blackhole route, which has no next
hop; and a route whose `dev` the device has not got, which is `E033` and would
otherwise be reported twice.

**Why it matters.** A next hop is resolved by ARP or neighbour discovery, never
by routing, so an off-link one has no way of being reached: the router would
have to route in order to reach the address that tells it how to route. The
usual causes are a prefix length that was shortened without the next hop moving,
and a next hop copied from a neighbouring subnet.

The **VRF** half of the check is the part that is easy to miss. A routing
instance is a routing table of its own (schema §16.1), so a next hop that sits
on an interface in a *different* instance is not merely in another subnet — it is
unreachable from this table by construction, however adjacent the two addresses
look on the page.

**Suppress with** `E032` / `NG-F008`, or an annotation on the device.

#### `E033` — route sends out of an unknown interface

*Alias: `NG-F009`. Severity: error.*

A route's `dev` names an interface the device does not have. The finding lists
the interfaces it does have, because the cause is nearly always a typo or a port
that was renamed.

**Why it matters.** `dev` is how a route is pointed at an egress rather than at
an address — an unnumbered point-to-point link, or a route into a tunnel. A name
that resolves to nothing is a route the device would refuse to install, which
leaves the destination unreachable while the inventory says it is served.

**Suppress with** `E033` / `NG-F009`, or an annotation on the device.

#### `E034` — OSPF runs on an interface the device does not have

*Alias: `NG-F010`. Severity: error.*

An entry of `routing.ospf.interfaces` names no interface of the device.

**Why it matters.** An area is only as wide as the interfaces that run it, so a
name that resolves to nothing is an adjacency that will never come up. In an
inventory it is worse than that: the link *looks* like it is in the IGP, so a
reader of the routing view counts on a path that does not exist.

**Suppress with** `E034` / `NG-F010`, or an annotation on the device.

#### `E035` — BGP session disagrees about an AS number

*Alias: `NG-F011`. Severity: error.*

A `neighbors[].remote_asn` contradicts the `asn` the peer declares for itself.
The peer is found by resolving the neighbour address against every address the
inventory configures (schema §16.4), so the check only applies to a session
whose far end is an element here; a peer that declares no `routing.bgp` at all is
silent, because an inventory may model the box without modelling its control
plane.

Reported per *claim*, from the document that makes it: a typo on one side is one
mistake, and two sides typed differently are two.

**Why it matters.** The OPEN message carries the local AS, and a peer that
expected a different one closes the session (RFC 4271 §6.2). The result is a
session that never establishes while both configurations look plausible in
isolation — which is exactly the failure an inventory holding both ends can catch
and a device holding one cannot.

**Suppress with** `E035` / `NG-F011`, or an annotation on either element.

#### `E036` — duplicate router id

*Alias: `NG-F012`. Severity: error.*

Two elements declare the same `router_id`. One device giving OSPF and BGP the
same value is **one** identity rather than a duplicate — it is the normal
configuration — so the ids of a device are de-duplicated before they are
counted.

**Why it matters.** A router id names the router itself. OSPF drops a hello from
a neighbour claiming the local id (RFC 2328 §10.5) and BGP refuses a session with
a duplicate identifier (RFC 4271 §6.8), so every adjacency between the two stays
down. The cause is nearly always a router built by copying its neighbour's
configuration, which makes it the mistake most likely to be in an inventory
twice.

**Suppress with** `E036` / `NG-F012`, or an annotation on either element.

#### `E037` — PDU outlet claimed twice

*Alias: `NG-E010`. Severity: error.*

Two elements name the same `pdu:outlet` in their `power.inputs`. Two inputs of
*one* device naming one outlet is refused earlier, by the model (`NG-E002`), so
every finding here involves at least two elements as well as the PDU, and the
message names all of them.

**Why it matters.** An outlet takes one plug. Unlike the patch-panel version of
the same impossibility ([`E022`](#e022--patch-panel-position-terminated-twice))
this is usually not a typo about the number: it is a *second* device someone
added to a rack that had no spare socket, and whoever wrote it down read the
label off the cord already in the hole. The consequence is that the PDU's own
figures stay plausible — a doubled claim occupies one outlet, so the free-outlet
count is right and the rack looks as though it has room — while one of the two
devices is in fact fed from somewhere nobody recorded.

**Suppress with** `E037` / `NG-E010`, or an annotation on either element or on
the PDU. There is no legitimate case: a hole takes one plug. A splitter or a
short strip chained off an outlet is its own `pdu` document, naming that supply
in `input_feed`.

#### `E038` — power input names no outlet that exists

*Alias: `NG-E011`. Severity: error.*

A `power.inputs` entry does not resolve. Four ways to get there, reported apart
because the fix differs: no element answers to the name before the colon, the
name is ambiguous across namespaces, it resolves to something that is not a
`pdu`, or the PDU is right and the outlet number is not. The last spelling
quotes the range the PDU declares.

**Why it matters.** A PDU's outlets come from a range rather than being written
out, so `25` on a 24-outlet strip is invisible in the document that names it —
which is why the diagnostic puts the range in front of the reader, exactly as
[`E021`](#e021--cable-on-a-position-the-patch-panel-does-not-have) does for a
panel. The consequence is a load that lands on no PDU: it drops out of the load
schedule and out of the utilisation table, so the strip appears to have capacity
it does not and the device appears to have no recorded power at all. The common
cause is the transcription `7` for an outlet printed `07` — numbers are compared
as written, deliberately, because a strip labelled `01`…`24` and one labelled
`1`…`24` are different labels and quietly accepting either would put a cord in a
hole nobody can find.

**Suppress with** `E038` / `NG-E011`, or an annotation on the device or on the
PDU it names — on any of the candidates, when the name was ambiguous. There is
no legitimate case; when the feed genuinely is not modelled, leave `inputs` off
and accept [`W137`](#w137--declared-draw-with-no-power-path) instead of pointing
at an outlet that does not exist.

#### `E039` — PDU load exceeds its capacity

*Alias: `NG-E012`. Severity: error.*

The draws landing on a PDU's outlets add up to more than its `capacity_watts`.
The sum is the **normal-operation share**: a dual-corded server draws its load
through both cords, so it contributes half its `draw_watts` to each of its two
PDUs. The other figure — the whole draw of everything corded to the unit, which
is what it carries when its partner fails — is printed by `netgraph list power`
and `netgraph export power`, and is deliberately not graded here.

**Why it matters.** A strip carrying more than it is rated for trips its breaker
and takes down everything on it, in a rack where no single document is wrong.
That is the mistake this catches: the rack that grew one server at a time until
the total passed the strip's rating, which nobody notices because each addition
looked like it fitted.

Grading the normal share rather than the failover figure is what makes the rule
usable. An A/B pair each sized for half a rack is a correct design, and reporting
it as an error would fire on every rack that got redundancy right — the racks
that most need the other findings in this section.

Silent when the PDU records no `capacity_watts`: there is nothing to compare
against, and inventing a rating for a strip nobody measured would turn a missing
fact into a false error. The load is still totalled and reported, without a
verdict.

**Suppress with** `E039` / `NG-E012`, or an annotation on the PDU or on any of
the loads. A strip genuinely over its rating has no legitimate case; what is
worth annotating is a schedule whose draws are known to be pessimistic, because
they were copied from PSU ratings rather than from what the equipment actually
pulls.

#### `E040` — PoE allocation exceeds the budget

*Alias: `NG-E013`. Severity: error.*

The PoE reserved by a device's PSE ports adds up to more than its
`power.poe_budget_watts`. A port reserves the PSE-side figure for its `class`,
or its `budget_watts`, or its standard's maximum when it states neither — which
is what a switch with no per-port configuration does.

Only ports that actually *hold* budget are counted: one that feeds something the
cable walk found, and one whose `budget_watts` was written down, which is the act
of reserving it.

**Why it matters.** That last restriction is the whole design of the rule. A
`poe` block on an empty port is a **capability**, not an allocation: a 48-port
PoE+ switch can source 30 W on every port and is sold with a supply of around
740 W, so it is deliberately oversubscribed by a factor of two. Counting all 48
ports would report every real switch as broken and the rule would be worthless
exactly where it matters.

What is graded is power promised to something. Past the budget a switch stops
powering ports — by priority, or by class, or by port number, depending on the
vendor — so the device that goes dark is not the one that was just added, and the
symptom appears somewhere nobody is working. Hence the message spells out every
counted port and its share.

**Suppress with** `E040` / `NG-E013`, or an annotation on the switch or on any
device it feeds. Silent when `poe_budget_watts` is not recorded at all. The one
case worth annotating is a chassis with a second supply fitted that the budget
was never updated for — though correcting the budget is the better fix, since it
is also what the utilisation table reads.

#### `E041` — PoE-powered device has no PoE uplink

*Alias: `NG-E014`. Severity: error.*

A device declares `powered_by: poe`, so the run carrying its traffic is its only
power path, and that path does not deliver. Three shapes, all of them a device
that will not come up: no cable leaves it at all; every run it has lands on a
port with no `poe` block, or with a disabled one; or a run does source power, but
less than the device says it draws. The walk crosses patch panels, because a run
through a coupler is electrically one run for power exactly as it is for frames.

The draw is compared against the **PD-side** class figure — 25.5 W for class 4,
not the 30 W the PSE sets aside — because the difference between the two tables
is precisely the cable loss IEEE 802.3 budgets for 100 m of copper. A device that
drew the PSE figure at the far end of a run would be outside the standard, so
grading against it would accept a link that does not work.

**Why it matters.** A ceiling access point or a camera has no power cord. If the
port it is patched to is not a PSE, it never boots — and every other document
about it is correct: the cable exists, the addressing exists, the link is drawn.
Two mistakes produce it. The device was moved to a spare port on a switch whose
PoE ports are only the first 24, so the run is right and the power is not. Or the
class is too small for the load — a class-2 port under a 20 W access point, which
powers up, associates, and browns out the moment the radios are busy.
`enabled: false` left over from the port's previous tenant is the third and
quietest version.

**Suppress with** `E041` / `NG-E014`, or an annotation on the device or on the
switch at the far end of its uplink. The legitimate case is an inline midspan
injector between the two, which the inventory has no element for; annotate the
device, because "where does this get its power" is the next reader's question.

#### `E042` — redundant power that is not redundant

*Alias: `NG-E015`. Severity: error.*

A device declares `redundant: true`, but the outlet feeds that resolved are not
independent — either every input lands on one PDU, or the inputs land on
different PDUs that record the same `input_feed`. Fewer than two *declared*
inputs is refused by the model (`NG-E002`) and an input that failed to resolve is
[`E038`](#e038--power-input-names-no-outlet-that-exists)'s finding, so a device
whose only problem is a typo is not also accused of a false claim.

A PDU recording no `input_feed` is not evidence either way, so two different PDUs
with nothing written down are accepted: silence is not a claim. `input_feed` is
free text and the rule compares two of them for equality and nothing more —
whether two names describe two failure domains is site knowledge.

**Why it matters.** Two cords into one strip is the cheap version of the mistake:
the strip, its breaker and its own supply cord all remain single points of
failure, so the second PSU buys nothing except the belief that there is one. The
subtler and far more common version is two PDUs fed from one supply — the racking
is right and the electrical plan is not — and it survives every visual
inspection, because such a rack looks exactly like a rack that is A/B fed. Both
fail as a unit at the one moment the redundancy was bought for. `redundant: true`
is also a claim somebody acts on during a maintenance window, when the question
is whether feed A can be dropped with everything still up.

**Suppress with** `E042` / `NG-E015`, or an annotation on the device or on any
PDU feeding it. Worth annotating when the feed names are coarser than the failure
domains they stand for — two independent UPS strings both written `utility`, say
— but naming the feeds apart is the better fix, because it makes the inventory
right for every other device in that rack too.

#### `E043` — group member does not exist

*Alias: `NG-S010`. Severity: error.*

A `group`'s `spec.members` names something the inventory does not declare, or
names something ambiguously — the same two failures every reference in this
schema can have, reported apart because the fix differs. An ambiguous name is one
that matched several elements globally after the namespace-local and ancestor
lookups both failed; the finding lists the candidates, and writing the reference
fully qualified settles it.

**Why it matters.** A group is a list of who may do something, and an access rule
written against it is exactly as true as that list. A member that resolves to
nothing is not a cosmetic problem: it is a person the group silently does not
include, and nothing about reading the file says so. This is the one place the
mistake is visible.

**Suppress with** `E043` / `NG-S010`, or an annotation on the group. There is no
good reason to: a member netgraph cannot resolve is a member no consumer of the
inventory can resolve either.

#### `E044` — group member is not an identity

*Alias: `NG-S011`. Severity: error.*

A member resolved, but to something that is not a `user` and not a `group` — a
switch, a cable, a PDU. Names are unique within a namespace across all kinds
(`NG-N002`) but not across the tree, so this is nearly always a name collision: a
group meant `alice` in the `people/` directory and got the workstation called
`alice` in `desks/`.

**Why it matters.** Resolving it anyway would draw a membership edge between a
group and a switch in the identity view and put the switch in the group's
headcount, which is a picture of something that cannot be true. Refusing it also
makes the collision findable, which is what actually needs fixing — usually by
writing the member fully qualified.

**Suppress with** `E044` / `NG-S011`, or an annotation on the group or on the
element it landed on. Nothing legitimate is behind this one.

#### `E045` — group membership cycle

*Alias: `NG-S012`. Severity: error.*

Groups contain groups, and somewhere the containment closes a loop:
`everyone` → `engineering` → `everyone`. The finding names the loop in the order
it was walked, starting from the group the search entered it through. A group
naming *itself* is refused earlier, by the model, which needs no inventory to see
it and can therefore point at the line.

**Why it matters.** A cyclic group has no membership. Expanding it does not
terminate, so no consumer can answer "who is in this?" — not netgraph, not the
directory the inventory is applied to, not the person reading the file. Nesting
is the whole point of groups and the loop is its one failure mode, so it is
checked rather than hoped for.

**Suppress with** `E045` / `NG-S012`, or an annotation on any group in the loop.
Suppressing it does not make the membership computable; it only stops netgraph
saying so.

#### `E046` — duplicate account identifier

*Alias: `NG-S013`. Severity: error.*

Two `user` documents claim one `login`, two claim one `uid`, or two `group`
documents claim one `gid`. A `login` defaults to `metadata.name`, so two users of
that name in two namespaces trip this even though neither document writes a
`login` at all — which is the intended reading, not an accident of it.

Namespaces deliberately make no difference. Two switches called `sw-1` in two
sites are two switches; two accounts called `alice` in two directories are one
person's login written twice, and the reason to record a login rather than lean
on `metadata.name` is that the account namespace is the estate, not the folder.

**Why it matters.** All three are *the* key of the account in the system that
consumes them. Two users with one login are one account with two owners and two
sets of keys; two uids that collide make every file one of them creates readable
and writable by the other, which is a permission boundary that silently does not
exist. Neither shows up as a conflict anywhere except here.

**Suppress with** `E046` / `NG-S013`, or an annotation on either claimant. The
one defensible case is an estate that deliberately reuses a uid across two
disjoint systems netgraph models as one tree; naming the two accounts apart is
the better fix, because every export of this inventory has the same problem.

### Warnings

#### `E047` — declared gateway redundancy is not met

*Severity: error.*

An element carries `netgraph/redundancy: gateway` — a promise that no *single*
failure can cut it off from its default gateway — and the topology does not keep
it. The finding names every failure that would: each cut vertex and each bridge
lying between the element and the gateway, by the name a person would use for
it.

The gateway is not a kind or a flag. It is the element holding the address some
interface of this one names as its `gateway`, which is the one definition the
files already carry. An IPv6 link-local first hop (`fe80::1`) is exempt: it is
on-link by definition and is almost never written down as an address of the
router that answers for it, exactly as [`E020`](#e020--first-hop-is-not-on-link)
exempts it.

Three shapes, reported apart because the fix differs:

* **nothing to check** — the element declares the expectation and no interface
  declares a `gateway`, or the gateway address is configured on nothing in this
  inventory. Add one, or drop the expectation;
* **nothing to lose** — there is no path to the gateway even now. That is a
  worse problem than a non-redundant one, and is said as such;
* **one path** — the ordinary case. A path exists and one element or one cable
  carries all of it.

**Why it matters.** Redundancy is the one property of a network that is
invisible in the state where it is working. A second uplink that was
decommissioned, a pair that was re-patched into one switch, a spare that was
quietly reused — none of them changes anything anybody can see until the day the
first path fails, and then all of them do. Writing the expectation down is what
turns "somebody will notice" into a build failure on the pull request that
removed it.

**How it is decided.** "Survives any single failure" is exactly "two-connected",
so the check is a search for the separators between the two elements
([`netgraph.connectivity.separators`](../src/netgraph/connectivity.py)) rather
than a simulation — an exact answer in one pass over the graph instead of an
approximate one in a thousand. Two cables between the same pair of devices count
as two: cutting one leaves the other. A run through a patch panel is one path,
and the panel position is named when it is the thing in the way (§15.2).

**Suppress with** `E047`, or an annotation on the element. The honest fix is
usually to drop the expectation: an annotation claiming redundancy that does not
exist is worse than no annotation, because it reads in review as though somebody
checked.

**See also.** [`netgraph impact`](commands/impact.md), which reports this rule
alongside the simulation that explains it, and `netgraph impact --spof`, which
finds the same separators without being asked about a particular element.

#### `E048` — declared power redundancy is not met

*Severity: error.*

An element carries `netgraph/redundancy: power` — a promise that no *single*
failure can switch it off — and the feeds do not keep it. The finding names
every source whose loss would take the element with it.

Distinct from [`E042`](#e042--redundant-power-that-is-not-redundant), which
grades a device's own `power.redundant` claim about its two cords. This grades
the stronger statement, and grades it over the whole feed chain rather than over
the inputs of one document, so it catches what `E042` cannot see:

* a device with **one cord**, which makes no `redundant` claim to be graded;
* a device fed **over PoE** by a switch that is itself on one PDU — the
  redundancy has to hold two steps up, and only the walk finds that;
* two PDUs on **one building supply**, through their `input_feed`.

An element that `E042` already reports is left alone. Two findings for one
mistake teach people to read neither.

**Why it matters.** Power is where redundancy is most often assumed and least
often checked. Cabling redundancy is visible on a diagram and gets reviewed;
which strip a cord is in is visible only from behind the rack, and a dual-corded
server with both cords in the A side is the single most common way a
"fully redundant" rack goes dark.

**Suppress with** `E048`, or an annotation on the element. As with `E047`, the
fix is a second independent feed or an honest annotation.

#### `W101` — interface neither routes nor switches

*Alias: `NG-I013`. Severity: warning.*

An interface has no IPv4 or IPv6 address and no `vlan` block. Exempt: hub ports
(they cannot hold either), `enabled: false` interfaces, and any interface that
another one is stacked on — LAG members and the parent of a sub-interface carry
the lower layer, not the addresses.

**Why it matters.** Such an interface does nothing. It is usually a half-written
document: the port was added and the addressing never followed. If the port is
genuinely spare, say so with `enabled: false` and the warning goes away on its
own.

**Suppress with** `W101` / `NG-I013`, or an annotation on the element.

#### `W102` — MTU mismatch across a link

*Alias: `NG-C010`. Severity: warning.*

The two endpoints of a cable declare different `mtu` values. Resolved through
the LAG master when an endpoint is a member.

**Why it matters.** A classic cause of silent path-MTU failures: small packets
and pings work, large transfers stall. It is invisible until someone copies a
big file, and by then nobody suspects the diagram.

**Suppress with** `W102` / `NG-C010`, or an annotation on the cable or either
device. An intentional jumbo-frame boundary is the legitimate case.

#### `W103` — orphan device

*Alias: `NG-C016`. Severity: warning.*

A device terminates no cable, hosts no adapter, and is not the target of an
adapter's `attached_to`.

**Why it matters.** It will be drawn as an isolated node floating next to the
topology. Either a cable document is missing, or the device really is spare —
and if it is spare, saying so explicitly is better documentation than a silent
island.

**Suppress with** `W103` / `NG-C016`, or an annotation on the device. Spare
hardware and cold standby are the legitimate cases.

A device whose cable names a *missing interface* still counts as cabled, so
`E001` is never compounded by this warning.

#### `W104` — IP address on an access port

*Alias: `NG-V009`. Severity: warning.*

An `access`-mode port of a layer-2-only switch — a `switch` that forwards
neither IPv4 nor IPv6 — carries an IP address. A `type: vlan` interface (an
SVI) is exempt.

**Why it matters.** A bridge port is not where a management address lives; it
belongs on an SVI. Modelling it on the port produces an address that no real
switch would answer on, and hides which VLAN management actually sits in.

**Suppress with** `W104` / `NG-V009`, or an annotation on the switch.

#### `W105` — subnet with a single member

*Alias: `NG-A008`. Severity: warning.*

Exactly one element in the inventory is addressed inside a prefix. Prefixes that
can hold at most two hosts are exempt — `/30`, `/31` and `/32`, and `/126` to
`/128` — because a host route holds one address by definition and the far end of
a point-to-point link is routinely somebody else's router.

**Why it matters.** It is what a typo'd prefix length looks like from the
outside: `10.0.0.5/32` where `/24` was meant, or `/25` where the plan says `/24`,
splits a subnet into halves that cannot reach each other while every individual
document still looks right. The other reading is just as useful — the neighbour
exists but was never written down, so the diagram is missing a device. Only the
layer-3 view can show this at all, which is why the rule arrived with it; see
[`--layer l3`](rendering.md#layers-one-inventory-nine-questions).

**Suppress with** `W105` / `NG-A008`, or an annotation on the element holding
the address. A deliberately sparse management prefix, and a link whose peer is
outside the inventory on purpose, are the legitimate cases.

#### `W106` — one address claimed twice in a subnet

*Alias: `NG-A009`. Severity: warning.*

Two different elements hold the same address inside one prefix, in different
broadcast domains. When two of the claimants share a VLAN, [`E004`](#e004--duplicate-ip-address)
reports the clash as an error instead and this rule stays quiet, so one mistake
is never reported twice.

**Why it matters.** `E004` deliberately scopes a duplicate address to one VLAN,
because re-using a prefix per broadcast domain is a normal design. Layer 3 has
no VLAN column — neither does a routing table — so it draws one subnet with two
claimants, and an operator working from that picture cannot tell which of them
answers at the address. Either the address plan re-uses more than it meant to,
or the two ports belong in one VLAN and one of them is misconfigured.

**Suppress with** `W106` / `NG-A009`, or an annotation on either element.
Deliberate per-VLAN re-use of a whole prefix — the same gateway address in every
site's user VLAN — is the legitimate case.

#### `W107` — addresses on an aggregate member

*Alias: `NG-I006`. Severity: warning.*

An interface listed in a `lag`'s or `bridge`'s `members` carries its own `ipv4`
or `ipv6` addresses.

**Why it matters.** The aggregate is the interface the network sees. An address
on one lane is reachable only while that lane is up, which is precisely what
bonding exists to avoid — and on a bridge member it is not reachable at all,
because the bridge has already taken the frames. Almost always it means the
addressing was written before the bond was, and never moved up.

**Suppress with** `W107` / `NG-I006`, or an annotation on the element.

#### `W108` — MAC address on a loopback

*Alias: `NG-I007`. Severity: warning.*

A `type: loopback` interface declares a `mac`.

**Why it matters.** A software loopback has no medium and so no hardware
address. One written here was copied from a physical port, which means it is
also about to collide with that port under [`E003`](#e003--duplicate-mac-address).

**Suppress with** `W108` / `NG-I007`, or an annotation on the element.

#### `W109` — device that cannot be cabled

*Alias: `NG-I012`. Severity: warning.*

A device declares no `ethernet`, `wifi` or `lag` interface. Adapters are exempt:
`NG-X003` already restricts them to exactly those three types at schema time.

**Why it matters.** Only those types can terminate a cable (`NG-C009`), so the
device can never appear on a link however many cables are written for it. A
machine reached only through an adapter is the legitimate reading — and the
example inventory's dongle-only laptop is exactly that — but far more often the
physical port was simply never added to the document.

**Suppress with** `W109` / `NG-I012`, or an annotation on the device. A host
whose only connectivity is an adapter attachment is the legitimate case.

#### `W110` — network or broadcast address assigned

*Alias: `NG-A005`. Severity: warning.*

An address is the network or the broadcast address of its own prefix:
`10.0.0.0/24` or `10.0.0.255/24`. In IPv6 the all-zeros host part is the
subnet-router anycast address (RFC 4291 §2.6.1) and is reported the same way;
IPv6 has no broadcast address to report. Prefixes with no host part to speak of
— `/31`, `/32`, `/127`, `/128` — are exempt, because RFC 3021 and RFC 6164 give
both addresses of a point-to-point link to the two ends.

**Why it matters.** Neither address can be assigned to an interface, so the
document describes a host that cannot exist. It is what an off-by-one in an
address plan looks like, and what happens when a prefix is pasted where an
address was meant.

**Suppress with** `W110` / `NG-A005`, or an annotation on the element.

#### `W111` — overlapping prefixes on one element

*Alias: `NG-A006`. Severity: warning.*

Two *different* interfaces of one element hold addresses in prefixes that
overlap — most often the same prefix twice. Loopback and link-local addresses
are excluded, since `fe80::/64` on every port is how link-local works rather
than a clash. Two addresses on **one** interface are exempt by §10.3's own
wording: a secondary address inside the primary's prefix is an ordinary alias.

**Why it matters.** The routing table has no way to choose between the two
ports for traffic in the overlap; which one wins is a property of the operating
system rather than of the design. A `/16` where a `/24` was meant, or a port
left in the old subnet after a renumbering, both look like this.

**Suppress with** `W111` / `NG-A006`, or an annotation on the element.
Deliberate multi-homing into one subnet is the legitimate case.

#### `W112` — loopback with a non-host prefix

*Alias: `NG-A007`. Severity: warning.*

A `type: loopback` interface carries a prefix other than `/32` (IPv4) or `/128`
(IPv6). The host-scoped loopback addresses are exempt — `127.0.0.1/8` is what
every operating system configures, and RFC 1122 §3.2.1.3 reserves the whole of
`127.0.0.0/8` for it — so the rule only speaks about routed loopbacks.

**Why it matters.** A routed loopback is a single address the IGP advertises as
a host route. A `/24` on one claims a whole subnet that exists on no wire, and
every router that believes the advertisement black-holes the rest of it.

**Suppress with** `W112` / `NG-A007`, or an annotation on the element.

#### `W113` — undeclared VLAN referenced

*Alias: `NG-V004`. Severity: warning.*

A port is a member of a VLAN that the device's `vlans` database does not
declare. A device with **no** `vlans` at all is skipped entirely: §6.4 makes the
database optional, so its absence says "not modelled here" rather than "none
exist". A port trunking `all` is skipped for the same reason — it names no VLAN
in particular.

VLAN 1 never counts as undeclared: 802.1Q gives every bridge a Default VLAN
that nobody configures, and the schema itself defaults `access_vlan` to it, so
reporting it would fire on every port that simply left the field out.

**Why it matters.** The VLAN database is what gives a VLAN a name in the
rendering and what the switch will actually create. A port in a VLAN missing
from it is either a typo'd id or a VLAN that was never added to the switch, and
both look identical until traffic stops.

**Suppress with** `W113` / `NG-V004`, or an annotation on the device. Partially
modelled VLAN databases are the legitimate case — though deleting the `vlans`
list entirely says so more clearly, and silences the rule outright.

#### `W114` — native VLAN missing from `trunk_vlans`

*Alias: `NG-V006`. Severity: warning.*

A trunk's `native_vlan` is not listed in its `trunk_vlans`.

**Why it matters.** The native VLAN is the one the port sends and receives
untagged, so it is a member of the port's VLAN set whether or not it appears in
the list. The document then reads as carrying one VLAN fewer than the port
does — exactly the quiet disagreement between file and hardware this tool
exists to surface. Writing it out changes nothing operationally and makes the
diagram agree with the port.

**Suppress with** `W114` / `NG-V006`, or an annotation on the element. Vendor
configurations that spell the native VLAN separately from the allowed list —
which is most of them — are the legitimate case.

#### `W115` — every VLAN trunked to a host

*Alias: `NG-V007`. Severity: warning.*

A port whose `trunk_vlans` is `all` is cabled to a computer, a server or an
adapter rather than to another switch. Resolved through the LAG master when the
endpoint is a member.

**Why it matters.** `trunk_vlans: all` between switches is ordinary. Pointed at
a host it hands the whole VLAN estate to a machine that needs one or two of
them: broadcast traffic nobody planned for, and the standard prerequisite for
VLAN hopping. Trunking only the VLANs the host needs costs nothing.

**Suppress with** `W115` / `NG-V007`, or an annotation on the cable or either
element. A hypervisor or a router-on-a-stick is the legitimate case.

#### `W116` — LAG member contradicts its aggregate

*Alias: `NG-V008`. Severity: warning.*

A `lag` member declares a `vlan` block that differs from the aggregate's. A
member with no block of its own — the normal shape — is silent.

**Why it matters.** [§10.6](schema.md#106-lag-resolution) resolves VLAN and MTU
checks on a member through its aggregate, so the member's own block is never
what a link is checked against. When the two disagree, which one a reader
believes is a coin toss, and the one the validator believes is the aggregate's.

**Suppress with** `W116` / `NG-V008`, or an annotation on the element.

#### `W117` — both ends of a cable on one element

*Alias: `NG-C004`. Severity: warning.*

The two endpoints of one cable land on the same element. The degenerate case
where they name the same *port* is [`E002`](#e002--interface-terminated-by-more-than-one-cable)
instead, so one mistake is never reported twice.

**Why it matters.** It is legal — a loopback plug on a test port and an MLAG
peer-link on one logical switch both look like this — but far more often the
cable document was copied and its second endpoint never edited. The link then
adds no path to the topology while the neighbour it was meant to reach is left
undrawn.

**Suppress with** `W117` / `NG-C004`, or an annotation on the cable or the
element. Loopback plugs and single-chassis peer links are the legitimate cases.

#### `W118` — cable and endpoint disagree about speed

*Alias: `NG-C008`. Severity: warning.*

A cable's `speed` differs from the speed its endpoint declares. In this schema
an interface has no `speed` of its own — the wire decides it — with one
exception: an adapter's `upstream.speed` is the *host bus* rate (§8.1), and that
is where the two can contradict each other.

**Why it matters.** §9.4 projects `cable.speed` onto `if:speed` at both ends, so
the two values cannot both be true and an export to NETCONF would have to pick
one. A gigabit dongle written into a ten-gigabit run is the shape this catches,
and it is usually a link budget nobody re-checked after the hardware changed.

**Suppress with** `W118` / `NG-C008`, or an annotation on the cable or the
adapter. A bus faster than the port it feeds — USB 3.0 at 5 Gbps behind a
gigabit Ethernet jack — is the legitimate case, and is worth writing down.

#### `W119` — cable terminates on a LAG aggregate

*Alias: `NG-C012`. Severity: warning.*

An endpoint is a `lag` interface rather than one of its members.

**Why it matters.** A bundle is logical: the wires land on the members. Cabling
the aggregate draws one link where the inventory means several, so the diagram
understates both the port count and the redundancy the bundle exists to provide
— and a reader planning a maintenance window cannot see which lanes go where.
The message names the members to cable instead.

**Suppress with** `W119` / `NG-C012`, or an annotation on the cable or the
element. A deliberately abstracted diagram — one line for a bundle whose lane
detail is out of scope — is the legitimate case.

#### `W120` — half duplex without a hub

*Alias: `NG-C013`. Severity: warning.*

A cable declares `duplex: half` and neither endpoint belongs to a `hub`.

**Why it matters.** Half duplex means the two ends share the medium and have to
arbitrate for it, which is what a repeater's collision domain requires and what
a switched port does not have. Between two switch ports it is either a
speed/duplex negotiation that failed — the classic cause of a link that passes
pings and collapses under load — or a value copied from a document that
described a hub.

**Suppress with** `W120` / `NG-C013`, or an annotation on the cable or either
element. Legacy gear pinned to half duplex on purpose is the legitimate case.

#### `W121` — disconnected topology

*Alias: `NG-C014`. Severity: warning.*

The topology graph falls into more than one island. Reported **once** for the
whole inventory, naming each island's alphabetically smallest member and how
many elements it holds. Cables and `attached_to` attachments both count as
links (§8.2).

Islands of a **single** element are left to [`W103`](#w103--orphan-device),
which says the same thing about a lone device in better words. This rule is
about the case that looks fine locally: two halves of a network that are each
internally cabled and never meet.

**Why it matters.** Every device in each island terminates a cable, so nothing
else complains — and yet no packet can cross from one to the other. It is what a
missing backbone link looks like, and what happens when a site is added to an
inventory before the cable that joins it. On a large diagram the two halves may
simply be laid out next to each other and read as one network.

**Suppress with** `W121` / `NG-C014`, or an annotation on any of the elements
the message names. Deliberately separate networks in one inventory — an
out-of-band management island, an air-gapped lab — are the legitimate case; if
they are separate on purpose, separate inventories usually document that better.

#### `W122` — one hub, two subnets

*Alias: `NG-H005`. Severity: warning.*

Two elements cabled into one hub hold addresses that share no prefix. Hubs
cabled to each other are examined as a single collision domain. The two address
families are checked separately, and only routable addresses count — link-local
and loopback addresses are not a subnet anybody chose.

**Why it matters.** A hub is a repeater: every port sees every frame, so
everything plugged into one is a single broadcast domain and belongs in a single
prefix. Ports in prefixes that do not meet are wired together and still cannot
reach each other, which is a particularly confusing failure because the cabling
is visibly correct.

**Suppress with** `W122` / `NG-H005`, or an annotation on the hub or on either
element. A hub used as a passive tap or a span port, where the far end is
deliberately in another prefix, is the legitimate case.

#### `W123` — cabled adapter with no host

*Alias: `NG-X002`. Severity: warning.*

An adapter has at least one cabled downstream port but no `upstream.attached_to`
— and its upstream port terminates no cable either.

**Why it matters.** §8.2 calls a free-standing adapter a spare in a drawer or a
media converter in a run. Once something is patched into its downstream ports it
is neither: the dongle is in use, and the machine it presents those ports to was
left out of the inventory. The host is then missing from the diagram entirely.

**Suppress with** `W123` / `NG-X002`, or an annotation on the adapter. A media
converter mid-run is the legitimate case, and spelling it with a cable on the
*upstream* port instead says so precisely — which silences this rule outright.

#### `W124` — adapter attached to a hub or a switch

*Alias: `NG-X007`. Severity: warning.*

`upstream.attached_to` points at a `switch` or a `hub`.

**Why it matters.** An adapter is a port of the machine it plugs into, so its
host is something with a bus — a computer, a server, a router. Network gear
takes a cable. A media converter sitting between two switches is the
configuration that tempts this spelling, and §8.2 gives it a better one:
`passthrough: false` with a cable on each side, which draws the converter as the
distinct node it is rather than folding it into a switch.

**Suppress with** `W124` / `NG-X007`, or an annotation on the adapter or the
device. An SFP module modelled as an adapter of the switch it sits in is the
legitimate case.

#### `W125` — overlay reaches past its underlay

*Alias: `NG-T006`. Severity: warning.*

A tunnel names an `over`, but at least one element it terminates on is not an
endpoint of that underlay tunnel.

**Why it matters.** `vxlan over ipsec` only works where the IPsec tunnel
actually goes. An overlay that terminates on a third site whose outer packets
have no protected path is drawn joining places that cannot in fact reach each
other that way — and, worse, is drawn as protected when the traffic to that one
endpoint is not. Either the underlay is missing an endpoint or the overlay has
one too many.

**Suppress with** `W125` / `NG-T006`, or an annotation on either tunnel or on
the stranded element. The legitimate case is an underlay that netgraph only
partly models — a provider MPLS cloud declared as a two-ended tunnel between the
sites that matter.

#### `W126` — tunnel MTU does not fit its underlay

*Alias: `NG-T011`. Severity: warning.*

A tunnel declares an `mtu` larger than its underlay's `mtu` minus the
encapsulation overhead of its own type.

**Why it matters.** Encapsulation is not free: every header in the stack comes
off the payload the overlay can carry. VXLAN costs 50 bytes, WireGuard 80, IPsec
about 73, GRE 24. An overlay MTU that ignores them produces packets the underlay
has to fragment or drop, which is the classic "small transfers work, large ones
hang" failure — invisible until someone copies a big file, and by then nobody
suspects the diagram. `netgraph list tunnels` prints the stack the budget is
computed over.

**Suppress with** `W126` / `NG-T011`, or an annotation on either tunnel. The
overheads netgraph uses are the widely published worst case over IPv4; a
deployment that has measured its own and knows it fits is the legitimate case.

#### `W127` — tunnel carries traffic in the clear

*Alias: `NG-T012`. Severity: warning.*

A tunnel's type encrypts nothing — `gre`, `vxlan`, `geneve`, `l2tp` or `pptp` —
and no tunnel in its `over` chain does either. PPTP counts as cleartext however
it is configured: MPPE is broken.

**Why it matters.** This is the single most expensive thing a network diagram
can get wrong. "There is a tunnel between the sites" reads as "the traffic is
protected", and for half the tunnel types in this schema it is not. Inside a
data centre that is perfectly correct and the rule is noise; across the internet
it is a finding worth stopping for. Nesting silences it — a VXLAN inside an
IPsec tunnel is protected by the underlay, which is exactly why `over` exists —
and so does `encrypted: true`, which records that the deployment protects it
some other way.

**Suppress with** `W127` / `NG-T012`, or an annotation on the tunnel. An
inventory that is entirely one data centre fabric will want
`ignore = ["W127"]` in `netgraph.toml`.

#### `W128` — tunnel interface named by no tunnel

*Alias: `NG-T013`. Severity: warning.*

An interface of `type: tunnel` is `enabled: true` and no `tunnel` document names
it as an endpoint.

**Why it matters.** The overlay counterpart of
[`I002`](#i002--enabled-interface-terminates-no-cable), but a warning rather
than information: a spare *physical* port is a normal thing to own, while a
virtual interface exists only because something configured it. One with no
tunnel document describes one end of something the inventory never states the
other end of, so the diagram shows a port that goes nowhere. Either the tunnel
document is missing or the interface is left over from one that was deleted.

**Suppress with** `W128` / `NG-T013`, or an annotation on the element. Saying
`enabled: false` on the interface silences it *and* tells the next reader the
overlay is not in service.

#### `W129` — two tunnels share a VNI on one element

*Alias: `NG-T014`. Severity: warning.*

Two VXLAN or Geneve tunnels terminating on the same element declare the same
`vni`.

**Why it matters.** A VNI names a virtual network *on a VTEP*. Two tunnels
reusing one on the same element are either the same overlay written twice — one
of the two documents is stale — or two overlays that will bridge into each
other, joining broadcast domains the diagram shows as separate.

**Suppress with** `W129` / `NG-T014`, or an annotation on either tunnel or on
the element. A VNI deliberately reused across a hub-and-spoke mesh, written as
several point-to-point tunnels rather than one multipoint one, is the legitimate
case — and writing it as one multipoint tunnel says it better.

#### `W130` — prefix claimed by two broadcast domains

*Alias: `NG-A010`. Severity: warning.*

One prefix holds addresses on interfaces that declare *different* VLANs. This is
the address-plan overlap that is not a nesting: neither claim contains the
other, the two simply collide on the same space.

**Why it matters.** A prefix is the address space of one segment. Every host in
it believes every address in it is reachable by ARP, and half of them are in the
other VLAN and are not. Nothing routes between the two either: a router will not
forward between two interfaces it considers to be on the same subnet. The usual
cause is a subnet document copied to a second VLAN without its addressing being
changed.

Only interfaces that declare a `vlan` block count. A host on an access port
declares none — its broadcast domain is a property of the switch it is cabled
to, not of its own document — so counting "untagged" as a domain of its own
would fire on the ordinary pairing of a router sub-interface with the hosts it
serves. Two ports of *one* element are left to
[`W111`](#w111--overlapping-prefixes-on-one-element).

When every domain holds exactly the same addresses, nothing is reported here:
that is one address claimed twice, and
[`W106`](#w106--one-address-claimed-twice-in-a-subnet) and
[`E004`](#e004--duplicate-ip-address) say it more sharply, with the offending
address named.

Reported by [`netgraph ipam`](ipam.md) as the overlapping-prefix conflict.

**Suppress with** `W130` / `NG-A010`, or an annotation on any element addressed
in the prefix. The legitimate case is a deliberately duplicated plan — two
identical lab pods, isolated from each other on purpose.

#### `W131` — nested prefix in a different broadcast domain

*Alias: `NG-A011`. Severity: warning.*

One prefix sits inside another, and the two are used in disjoint sets of VLANs:
`10.0.0.0/16` on VLAN 10 with `10.0.5.0/24` on VLAN 20 beneath it.

**Why it matters.** Nesting on its own is normal — a summarising router and the
segments underneath it describe one plan at two levels. It stops being normal
across a VLAN boundary, because the wider prefix tells its own segment that
every address of the narrower one is on-link. Those hosts will ARP for addresses
they should be routing to, and get no answer. This is the shape a mask typo
takes: a `/16` where a `/24` was meant.

As with [`W130`](#w130--prefix-claimed-by-two-broadcast-domains), only
interfaces that declare a `vlan` block are compared.

Reported by [`netgraph ipam`](ipam.md) as the nested-prefix conflict.

**Suppress with** `W131` / `NG-A011`, or an annotation on any element addressed
in either prefix. The legitimate case is a summary address deliberately
configured on a different VLAN from the segments it summarises.

#### `W132` — address outside every prefix on its link

*Alias: `NG-A012`. Severity: warning.*

The two interfaces a cable joins are both addressed in one family, and no prefix
on either end overlaps a prefix on the other.

**Why it matters.** A cable is one segment, and there is no room inside it for a
router. An address configured on it that lies outside every prefix the far end
declares is outside every prefix on its own link, so the two ends cannot exchange
a single packet. The usual cause is a host that kept the addressing of the desk
it was moved from.

Only families *both* ends configure are compared, so a switchport — which
carries no address at all — says nothing here, and the ordinary host-to-access-
port link is quiet. A dual-stack pair that agrees on IPv6 while disagreeing on
IPv4 is still reported: the IPv4 half is still broken. Both ends are resolved
through the LAG master first (§10.6), and a cable landing on an interface with
no socket is left to [`E012`](#e012--cable-terminates-on-an-interface-with-no-socket).

Reported by [`netgraph ipam`](ipam.md) as the outside-every-declared-prefix
conflict.

**Suppress with** `W132` / `NG-A012`, or an annotation on either element. The
legitimate case is a link that is deliberately unnumbered on one side, or one
whose peer is addressed by an ISP out of a range this inventory does not model.

#### `W133` — patch run stops inside the panel

*Alias: `NG-P002`. Severity: warning.*

A patch-panel position terminates a cable, and the position its coupler leads
to terminates none.

**Why it matters.** Half a run. The cable was pulled, the front was patched,
and the rear position was left for later — so the port at the near end is not
connected to anything, however patched the inventory makes it look. This is the
single most common real patch-record error, and the one a diagram cannot show
you: below `--layer physical` the incomplete run is dropped, and the port
simply appears unused.

A warning rather than an error because "left for later" is also a legitimate
state to record: the position is reserved, the cable exists, and the inventory
is telling the truth about a job that is half done. Render `--layer physical`
to see the segment that does exist.

**Suppress with** `W133` / `NG-P002`, or an annotation on the cable or the
panel. Annotating it is the right move for a position deliberately held for a
run that is not yet needed.

#### `W134` — access points on overlapping channels

*Alias: `NG-W011`. Severity: warning.*

Two `ap` radios in one broadcast domain are in the same band and their channels
overlap. "One broadcast domain" is read as both halves of the phrase: the two
elements are joined by the topology, *and* the radios put a common VLAN on the
air — an SSID with no `vlan` counting as the untagged domain. VLAN 10 on two
unconnected islands is two domains that share a number, exactly as in
`netgraph.graph.broadcast_domains`.

**Why it matters.** Two access points bridging one domain are there to extend
each other's coverage, and that only works if they are on different
frequencies. Radios sharing spectrum take turns rather than working in
parallel, so the pair delivers roughly the throughput of one — and the symptom
is "the Wi-Fi is slow in the middle of the house", which nobody traces back to
a channel plan. At 2.4 GHz only 1, 6 and 11 are non-overlapping; the finding
prints both frequency spans so the gap, or the lack of one, is visible.

A warning rather than an error, for two reasons. A deliberate same-channel
deployment exists — a repeater has no choice but to sit on its parent's channel
— and the schema records no geometry, so netgraph cannot know whether the two
are three metres or three floors apart.

Overlap is computed by centring `width_mhz` on the primary channel; the true
centre of a bonded channel depends on secondary channels no document states.
The approximation can only make the rule warn more readily, never less.

**Suppress with** `W134` / `NG-W011`, or an annotation on either element.

#### `W135` — BGP neighbour is not in the inventory

*Alias: `NG-F013`. Severity: warning.*

A `neighbors[].address` matches no address the inventory configures, so the far
end of the session cannot be found.

**Why it matters.** A warning rather than an error, deliberately: a perfectly
correct eBGP session towards a transit provider points at an address on *their*
router, which is not an element of this inventory and never will be. What the
warning says is what is lost — netgraph cannot check the AS numbers or the
reachability of the far end (`E035`), and the routing view has nothing to draw
the edge to, so the session is listed in the graph's dropped-link report instead.

The other cause is a typo in an address that *was* meant to be internal, and
that is worth a line of output. Between the two, silence would be the wrong
default and an error would fail every inventory with an upstream.

**Suppress with** `W135` / `NG-F013`, or an annotation on the device — which is
what to do for a genuinely external peer.

#### `W136` — VRF with no interface bound to it

*Alias: `NG-F014`. Severity: warning.*

A device declares a `vrfs` entry that no `interfaces[].vrf` names. The finding
also counts the static routes placed in the instance, because those are what
cannot work.

**Why it matters.** A routing instance is a table plus the interfaces that feed
it. With nothing bound, it holds no address and no connected route, so every
route placed in it can never resolve a next hop — and, worse, the partition it
was declared to create does not exist: the addresses somebody meant to isolate
are all still in the global table, colliding with each other as if the VRF had
never been written.

A warning rather than an error because a VRF declared ahead of the interfaces
that will join it is a normal state for an inventory to be in halfway through a
migration.

**Suppress with** `W136` / `NG-F014`, or an annotation on the device.

#### `W137` — declared draw with no power path

*Alias: `NG-E016`. Severity: warning.*

A device declares `power.draw_watts`, declares no `power.inputs`, and is not
`powered_by: poe` — so it says how much it draws and nothing about where from. A
device that declares inputs which fail to resolve is
[`E038`](#e038--power-input-names-no-outlet-that-exists)'s finding and is not
reported here as well.

**Why it matters.** A load on no PDU is a load on no schedule. Every strip in the
rack then reports a utilisation that is right about what it was told and wrong
about the rack, which is the failure mode a load schedule exists to prevent — and
the device is one nobody will find when the question is what goes dark if feed B
is dropped.

A warning rather than an error, deliberately. Recording draws before recording
the outlets they are plugged into is the normal order in which an as-built
document gets written: the nameplate figures come off an equipment list, and the
outlet numbers come from walking the rack with a torch. Refusing the
half-finished state would make the model unusable exactly while it is being
adopted, which is the wrong trade for a fact that is missing rather than wrong.

**Suppress with** `W137` / `NG-E016`, or an annotation on the device. The
legitimate case is a device fed from something the inventory does not model as a
`pdu` — a wall socket, a bench supply, a UPS nothing has a document for — where
the draw is worth recording and there is no outlet to name.

#### `W138` — stale diagram geometry

*Alias: `NG-Y001`. Severity: warning.*

A `kind: layout` document (§18) places something the inventory does not declare
— usually a device that has since been deleted, sometimes a name that was
mistyped. The finding names the layout, the view and the key, and points at the
line inside the layout document that holds it.

Only *element addresses* are judged. A derived node has an id no document
declares — `subnet:10.0.0.0/24`, `tunnel:site/wg0`, `rack:hq/comms/r1` — and
whether one still exists is a question about a particular drawing rather than
about the inventory, so those are left alone here. `netgraph layout --prune`
builds the drawing and answers it, which is why a prune removes a little more
than this reports. A group key is a namespace, and the inventory does know every
namespace it has, so those are checked.

**Why it matters.** Not because it draws anything wrong: geometry for a node
that is not in the diagram places nothing. It matters because an arrangement is
supposed to be reviewable. Coordinates for three devices that were
decommissioned last year are three entries a reader has to work out are dead,
and they accumulate — a layout file that is half history is one nobody trusts to
say where anything is.

A warning rather than an error, deliberately, and this one is not a close call:
deleting a switch must not make `netgraph validate` fail. The whole point of
keeping geometry in a sidecar is that the model can be changed without asking
the diagram's permission.

**Suppress with** `W138` / `NG-Y001`, or an annotation on the layout document.
The fix is normally `netgraph layout --prune`, which drops exactly these
entries and writes nothing else.

#### `W139` — group with no members

*Alias: `NG-S014`. Severity: warning.*

A `group` declares `members: []`, or omits `members` entirely.

**Why it matters.** Anything granted to an empty group is granted to nobody,
and — this is the part that costs an afternoon — it *looks* granted. The group
exists, the rule referencing it exists, and the access does not. The failure is
discovered on the day somebody needed it, which is always the worst day to
discover it.

A warning rather than an error, because an empty group is a normal intermediate
state: a group created before the people who will be in it, or one deliberately
emptied and kept so the name stays reserved. Both are reasonable, and neither is
worth refusing to render an inventory over.

**Suppress with** `W139` / `NG-S014`, or an annotation on the group. Worth
annotating a group that is empty on purpose and expected to stay so — a
placeholder for a team that does not exist yet, say — with the reason in
`metadata.description`.

#### `W140` — departed user still in a group

*Alias: `NG-S015`. Severity: warning.*

A group lists a `user` whose `status` is `departed`. Only `person` accounts are
judged: a `shared` or `service` account has nobody to depart.

**Why it matters.** This is the rule `status` exists for. The tempting response
to somebody leaving is to delete their `user` document, which removes the person
from the inventory *and* from every group naming them — losing exactly the list
somebody has to work through. The memberships are the access still to be revoked;
they cannot be revoked from a record that no longer exists. Marking the account
`departed` keeps the work visible until it has been done, and this finding is the
worklist.

**Suppress with** `W140` / `NG-S015`, or an annotation on the group or on the
user. The legitimate case is a group that is deliberately a historical record —
who was on a project — rather than a grant of access.

#### `W141` — unknown redundancy expectation

*Severity: warning.*

A `netgraph/redundancy` annotation names something this build does not grade, or
names it on an element there is nothing to grade. Two shapes:

* an **unrecognised token** — a typo, or an expectation a newer netgraph
  understands. The finding echoes the token verbatim and lists what is accepted;
* an expectation on an element that **owns no interfaces and takes no power** — a
  cable, a tunnel, a user, a group. There is no topology and no feed to hold it
  to, so the annotation grades nothing.

A warning rather than an error on purpose. An annotation is where a newer
netgraph will put things this build has never heard of, and refusing to load an
inventory because of a word in a comment-shaped field would make the annotation
useless for exactly the forward compatibility it exists for.

**Why it matters.** An expectation nothing grades is a promise nobody is
keeping, and it is worse than silence: it reads in review as though the property
were being checked. The failure mode is a pull request approved because
`netgraph/redundancy: gatway` was in the diff.

**Suppress with** `W141`, or an annotation on the element — appropriate when the
inventory is shared with a newer netgraph that does understand the token.

#### `W142` — annotation about something that is gone

*Alias: `NG-G001`. Severity: warning.*

A diagram annotation (§21) names an element the inventory does not declare. Two
documents can do it: a `note` whose `anchor` points at an `element` or a `link`
that has been deleted or was mistyped, and an `area` whose `members` list one.
The finding names the annotation, the reference it could not resolve, and the
line inside the document that holds it.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: note
metadata:
  name: why-two-uplinks
spec:
  text: The second uplink is for the annexe, which is on its own feed.
  anchor:
    element: sw-gone      # deleted last quarter — W142
```

A `legend` never trips it. Its entries are colours and words, so it names
nothing that could go stale — which is the whole argument for `auto: layers`,
the form of key that is generated from what the drawing actually drew.

**Why it matters.** Not because it draws anything wrong: a note whose anchor is
missing simply loses its leader line, and an area's dead member drops out of the
hull. It matters because an annotation is *prose about the network*, and prose
goes stale silently. A callout explaining why `sw-gone` had two uplinks still
reads, in the file and in review, as though it explained something — and the one
reader who needs it is the one who will act on it.

A warning rather than an error, and this one is not a close call. An annotation
is presentational, and §21's central promise is that it is **barred from
changing what the tool concludes**: adding a note cannot move a hop in `netgraph
path`, cannot appear in a generated configuration, and cannot fail a build. A
rule about one that could fail a build would be exactly the leak that promise
exists to prevent. Deleting a switch must not stop `netgraph validate` because
somebody once wrote a note about it.

**Suppress with** `W142` / `NG-G001`, or an annotation on the note or area
itself — an annotation document carries `metadata.annotations` like any other,
and `netgraph/ignore` on one silences findings about it. The legitimate case is
a note about equipment that has been removed and whose removal is the point:
"the old core switch sat here; the fibre it used is still in the duct". The fix
otherwise is to re-point the anchor or drop the member.

#### `W143` — area that encloses nothing

*Alias: `NG-G004`. Severity: warning.*

An `area` whose `selector` matches no element of the inventory. The box would be
drawn round an empty set, so it is not drawn at all.

```yaml
apiVersion: netgraph.dev/v1alpha1
kind: area
metadata:
  name: dmz
spec:
  label: DMZ
  selector:
    labels:
      zone: dmz         # nothing carries this label — W143
```

Only a *selector* is judged. An explicit `members` list that has gone stale is
[`W142`](#w142--annotation-about-something-that-is-gone)'s finding, reported
per name, which says more than "this box is empty"; and an area with an explicit
`geometry` is never reported, because a rectangle drawn on the canvas encloses
whatever happens to be inside it and "nothing yet" is a legitimate state for one.

**Why it matters.** An empty box reads as a claim, and the claim is false. A
`DMZ` zone that matches nothing looks on the diagram exactly like a DMZ with
nothing in it — which is a thing somebody might rely on — when what actually
happened is that the label was renamed, or the namespace moved, and the selector
was not updated with it. The picture keeps its caption and loses its contents.

A warning rather than an error, for the same reason as `W142` and with the same
force: an annotation is presentational and cannot be allowed to change what the
tool concludes, so a rule about one can never fail a build. It is also the rule
most likely to be right tomorrow — an area written for a zone that is about to
be populated is a normal thing to commit ahead of the elements.

**Suppress with** `W143` / `NG-G004`, or an annotation on the area. The
legitimate case is exactly that one: a zone declared before what goes in it.
Otherwise the fix is in the selector — check the `namespace` prefix, the label
key and its value, and the `kinds` — or turn it into a `members` list, which
says which elements were meant and reports each one that is missing.

### Info

#### `I001` — locally administered MAC address

*Alias: `NG-I010`. Severity: info.*

Bit 1 of the MAC's first octet — the second-least-significant — says the address
was assigned by the operator rather than drawn from a vendor's OUI (IEEE
802-2014 §8.2). `02:00:00:00:00:01` is one.

**Why it matters.** Not much, which is why it is information rather than a
complaint: virtual machines, bonds and anonymised documentation all use these
addresses deliberately. It is worth printing because an address no vendor issued
cannot be looked up when tracing a port back to hardware, and because a
hand-written address is the kind that gets duplicated into
[`E003`](#e003--duplicate-mac-address).

**Suppress with** `I001` / `NG-I010`, or an annotation on the element. If an
inventory uses locally administered addresses throughout, `ignore = ["I001"]`
in `netgraph.toml` is the right place to say so once.

#### `I002` — enabled interface terminates no cable

*Alias: `NG-C015`. Severity: info.*

An interface is `enabled: true` and nothing is patched into it. Only the types a
cable can terminate on are considered ([`E012`](#e012--cable-terminates-on-an-interface-with-no-socket)),
and `lag` aggregates are excluded: [`W119`](#w119--cable-terminates-on-a-lag-aggregate)
says the wires land on the members, so an aggregate that terminates no cable is
correct by construction.

**Why it matters.** Information rather than a complaint, because a spare port is
a normal thing to own and an uplink whose far end is outside the inventory — an
ISP hand-off — is normal too. It is printed because the inverse reading is just
as likely: the port is in use and the cable document was never written. A port
list with the unused ports marked is also what makes a patching decision
possible without walking to the rack.

**Suppress with** `I002` / `NG-C015`, or an annotation on the element. Better
still, say `enabled: false` on the port: the finding goes away *and* the next
reader learns that the port is spare on purpose. That is what the example
inventories do for their spare switch ports; their WAN interfaces, which face an
ISP that is not an element, carry a `netgraph/ignore` annotation instead.

An inventory that models patch panels or fully populated switches will see a lot
of these. `ignore = ["I002"]` in `netgraph.toml` turns the whole rule off for
that inventory in one line.

#### `I003` — tunnel on a non-standard port

*Alias: `NG-T015`. Severity: info.*

A tunnel declares a `port` other than the registered one for its type:
WireGuard 51820, OpenVPN 1194, L2TP 1701, VXLAN 4789, Geneve 6081. GRE and IPsec
run directly over IP and carry no port, so they never trip it.

**Why it matters.** Information rather than a complaint: moving WireGuard off
51820 to dodge a scanner or to run two instances is a normal thing to do. It is
printed because the port is the one fact a firewall rule needs, and the one most
likely to have been copied from the tunnel next to it in the file.

**Suppress with** `I003` / `NG-T015`, or an annotation on the tunnel. An
inventory that moves every tunnel off its default port should say
`ignore = ["I003"]` in `netgraph.toml` once.

#### `I004` — person in no group

*Alias: `NG-S016`. Severity: info.*

A `user` with `type: person` and `status: active` is named by no group's
`members`. A `service` or `shared` account is not judged — being in no group is
the normal shape of one — and neither is a suspended or departed account, which
is what `W140` is about.

**Why it matters.** Information rather than a complaint: plenty of estates grant
a person access directly and put them in nothing. It is printed because the
opposite reading — "this person is in no group, therefore they have no access" —
is a claim an auditor wants confirmed rather than assumed, and because an account
that was *meant* to be in a group and is not looks exactly like this.

**Suppress with** `I004` / `NG-S016`, or an annotation on the user. An inventory
that models people without modelling groups at all should say
`ignore = ["I004"]` in `netgraph.toml` once.

## Fixing a finding

Some of these rules describe a problem whose repair the inventory already
determines. A layout document places a switch that has been deleted; there is
nothing to decide, the entry is dead. `netgraph validate --fix` applies exactly
those repairs and reports everything else:

<!-- norun: 'inventory' is an illustrative tree, chosen to show one fix and one refusal -->
```console
$ netgraph -i inventory validate --fix
fixed 2 problems:
  W108  remove the MAC address from loopback sw-a:lo0
  W138  drop 'ghost-device' from the l1 view of layout 'layout'
W114 at net.yaml#0:6 not fixed: 2 repairs are possible; choose one with --choose W114=list|drop
    --choose W114=list  list VLAN 30 in the trunk_vlans of sw-a:eth0
    --choose W114=drop  remove the native_vlan of sw-a:eth0, so it tags every VLAN it carries

wrote 1 file: net.yaml
```

`--fix --dry-run` prints the unified diff instead and writes nothing. Writes go
through [the same path as `netgraph edit`](editing.md), so comments, key order
and quoting survive: only the lines the repair is about change.

**A fix never makes an inventory worse.** Each one is applied on its own and the
tree is validated again; the fix is kept only if the finding it was aimed at has
gone and no rule reports more than it did before. Otherwise the bytes are put
back and the reason is printed — which is why "remove the cable" is refused on a
two-device inventory where removing it would orphan a device. That is a decision
for a person, and the tool says so rather than making it.

**Where two repairs are plausible, both are offered and neither is picked.** A
trunk whose `native_vlan` is missing from `trunk_vlans` is either a list that is
short or a leftover `native_vlan`; the document cannot say which. `--fix` leaves
it alone and names the choices; `--choose W114=list` decides it.

The web editor puts a **Fix** button on each of these diagnostics, and applies
the same operations as one logged, revertible gesture.

### What is fixable

Generated from `netgraph.fixes.FIXES`, so this table cannot drift from the code.

<!-- generated: fix-index -->
| Rule | Severity | What `--fix` does | `--choose` |
|---|---|---|---|
| [`E001`](#e001--unknown-cable-endpoint) | error | Re-points the endpoint at the port it was misspelled from, declares the missing interface, or removes the cable. | `retarget` — writes the one declared port whose name is a near miss<br>`declare` — adds the interface the cable expects to the element<br>`remove` — deletes the cable |
| [`W108`](#w108--mac-address-on-a-loopback) | warning | Removes the MAC address from the loopback. | — |
| [`W113`](#w113--undeclared-vlan-referenced) | warning | Adds the VLANs the port is a member of to the device's 'vlans' database. | — |
| [`W114`](#w114--native-vlan-missing-from-trunk_vlans) | warning | Lists the native VLAN in 'trunk_vlans', or removes the 'native_vlan'. | `list` — adds the native VLAN to the trunk's VLAN set<br>`drop` — removes 'native_vlan', so the port tags everything it carries |
| [`W136`](#w136--vrf-with-no-interface-bound-to-it) | warning | Removes the VRF declaration, when nothing at all references it. | — |
| [`W138`](#w138--stale-diagram-geometry) | warning | Drops the stale entry from the layout document, as 'netgraph layout --prune' would. | — |
| [`W140`](#w140--departed-user-still-in-a-group) | warning | Removes the departed account from the group, unless it is the last member. | — |
<!-- /generated -->

`netgraph rules --fixable` prints the same table.

### What is not, and why

Three kinds of problem look mechanical and are not:

* **A rule with two readings and no tie-breaker.** Offered as a choice where the
  readings are enumerable (`W114`, `E001`), and left alone where they are not: an
  address in the wrong prefix (`W132`) could be repaired by changing the address
  or the prefix, and which one is right is a fact about the network, not about
  the file.
* **A load error.** An interface declared twice or a document that does not parse
  never reaches the semantic pass, so the element is not in the inventory and
  there is nothing for a producer — which is a function *of the inventory* — to
  read. Those are reported with the rule column `load` and repaired by hand.
* **A spelling the loader has already normalised.** A MAC written `00-11-22-…`
  or a prefix written `10.0.0.1/255.255.255.0` is canonicalised on load, so no
  finding is ever raised about it. [`netgraph fmt`](commands/fmt.md) rewrites the
  file itself.

## Suppressing a rule

Four mechanisms, all additive. A finding is silenced if any of them applies.
Only the [pass 3](#pass-3--semantics) rules can be suppressed; naming a
schema rule is a usage error:

<!-- run: rc=2 -->
```console
$ netgraph validate --disable NG-D005
error: --disable: 'NG-D005' is not a known rule id; expected one of E001, E002, E003, E004, E005, E006, E007, E008, E009, E010, E011, E012, E013, E014, E015, E016, E017, E018, E019, E020, E021, E022, E023, E024, E025, E026, E027, E028, E029, E030, E031, E032, E033, E034, E035, E036, E037, E038, E039, E040, E041, E042, E043, E044, E045, E046, E047, E048, W101, W102, W103, W104, W105, W106, W107, W108, W109, W110, W111, W112, W113, W114, W115, W116, W117, W118, W119, W120, W121, W122, W123, W124, W125, W126, W127, W128, W129, W130, W131, W132, W133, W134, W135, W136, W137, W138, W139, W140, W141, W142, W143, I001, I002, I003, I004, an NG-* alias from docs/schema.md §10, or '*'
```

Every mechanism accepts both spellings of an id — `W102` and `NG-C010` select
the same rule — plus the wildcards `*`, `all` and `any`.

### 1. On the command line

<!-- norun: flag forms with trailing shell comments, and neither line names an inventory -->
```bash
netgraph validate --disable W103 --disable NG-C010   # repeatable
netgraph validate --strict                           # warnings become errors
```

`--disable` adds to whatever `netgraph.toml` already ignores; it cannot
re-enable a rule the file disabled. `--strict` can only turn strictness on —
the file decides otherwise.

### 2. Per inventory, in `netgraph.toml`

The file sits at the root of the inventory tree and is entirely optional.

```toml
[validate]
strict = false                    # promote surviving warnings to errors
ignore = ["W103", "NG-C010"]      # never report these at all

[validate.severity]
E004 = "warning"                  # re-grade rather than silence
W101 = "info"
```

`ignore` also accepts a bare string (`ignore = "W103"`). Order of application:
`ignore` first, then the `severity` override, then `strict`. So a rule listed
in `ignore` is never reported whatever its severity says, and a rule re-graded
to `warning` is still promoted to `error` under `--strict`.

An unknown rule id in this file is an error, and so is an unknown key inside
`[validate]`: a suppression that silently applies to nothing would send you
hunting for a setting that never took effect. Unknown *top-level* tables are
left alone, so a file shared with a later netgraph version still loads.

### 3. Per element, with an annotation

```yaml
metadata:
  name: spare-switch
  annotations:
    netgraph/ignore: "W103, E004"      # or "*" for every rule
```

Ids may be separated by commas, semicolons or spaces. `netgraph.dev/ignore` is
accepted as well as `netgraph/ignore`.

Because a finding names every element it involves, annotating **either end** of
a cable suppresses a finding about that cable, and annotating the cable
suppresses it too. Pick whichever element the exception genuinely belongs to —
that is where the next reader will look for the explanation.

An unknown id in an annotation is *ignored* rather than fatal, and therefore
simply fails to suppress anything. Inventory data must not be able to abort a
run, but a typo here must not hide the finding it was aimed at either.

### 4. Nothing at all

Load errors and schema errors are not suppressible by design, and `render`
refuses to draw an inventory with errors unless you pass `--force`. That is not
an oversight: a diagram silently drawn from an inventory with a dangling cable
is worse than no diagram.

## Where this differs from the specification

[`docs/schema.md`](schema.md) §10 is the design target, and as of this release
**every rule it specifies is enforced**. Each has a write-up in
[pass 3](#pass-3--semantics) above and keeps the severity §10 gives it, except
where noted below. The ids are permanent whatever happens to the rules.

Three of the implemented rules are graded more harshly than §10.2 and §10.3
suggest, following §10.10:

* `E003` (`NG-I008`) and `E004` (`NG-A004`) are errors rather than warnings,
  because a duplicate address is far more often a copy-paste mistake than a
  deliberate VRRP or anycast design.
* [`E010`](#e010--multicast-mac-address) (`NG-I009`) is an error rather than a
  warning, because a multicast source address is not a design decision at all —
  no interface can have one.

Re-grade any of them in `netgraph.toml` if your inventory is the exception.

Seven further rules carry a carve-out that §10 does not spell out, in each case
because the rule as written would fire on the configuration everybody has:

* [`W112`](#w112--loopback-with-a-non-host-prefix) (`NG-A007`) exempts the
  host-scoped loopback addresses, so the `127.0.0.1/8` every operating system
  configures is not reported.
* [`W113`](#w113--undeclared-vlan-referenced) (`NG-V004`) exempts VLAN 1, the
  802.1Q Default VLAN, which exists on every bridge without being declared and
  is what `access_vlan` defaults to.
* [`E008`](#e008--a-member-is-not-free-to-be-aggregated) (`NG-I005`) exempts a
  `lag` nested inside a `bridge`, which is how a bridged bond is expressed.
* [`E005`](#e005--vlan-mismatch-across-a-link) (`NG-C011`) reports a differing
  `native_vlan` only when *both* trunks spell one out; leaving it off means "the
  default", not "a different VLAN".
* [`W117`](#w117--both-ends-of-a-cable-on-one-element) (`NG-C004`) stays quiet
  when both endpoints name the same *port*, which
  [`E002`](#e002--interface-terminated-by-more-than-one-cable) already reports.
* [`W121`](#w121--disconnected-topology) (`NG-C014`) ignores islands of a single
  element, which are [`W103`](#w103--orphan-device)'s finding.
* [`I002`](#i002--enabled-interface-terminates-no-cable) (`NG-C015`) ignores
  `lag` aggregates, since [`W119`](#w119--cable-terminates-on-a-lag-aggregate)
  asks for the members to be cabled instead.

Finally, two rules are reported *both* by the validator and by the renderer,
which drops what it cannot draw: a cable with an unresolvable endpoint
([`E001`](#e001--unknown-cable-endpoint)) and an `attached_to` that names no
element ([`E015`](#e015--attached_to-names-nothing-that-could-host-the-adapter))
each produce a suppressible finding *and* a `dropped from the graph:` line on
stderr naming what is missing from the picture. The finding says the inventory
is wrong; the stderr line says what the diagram in front of you is therefore
missing, which is worth saying even under `render --force`.
