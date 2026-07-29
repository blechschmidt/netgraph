# Validation rules

`netgraph validate` answers one question: **is this inventory usable?** It
answers it in three passes, and this document lists every rule each pass can
report, why the rule is worth having, and how to switch it off when your
network is the exception.

* [How findings are reported](#how-findings-are-reported)
* [Pass 1 — discovery](#pass-1--discovery)
* [Pass 2 — schema](#pass-2--schema)
* [Pass 3 — semantics](#pass-3--semantics)
* [Suppressing a rule](#suppressing-a-rule)
* [Where this differs from the specification](#where-this-differs-from-the-specification)

## How findings are reported

Every problem is printed as `location  rule  message`, grouped by severity,
most severe first:

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
| `NG-D003` | `kind` is one of the eight element kinds or `template`, lower-case. | `kind` selects the shape of `spec`; an unknown kind has no shape to check against. |
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

## Pass 3 — semantics

Every document has parsed. Do they agree with **each other**? These
fifty-one rules are the only ones that can be suppressed, re-graded or
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

### Warnings

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
[`--layer l3`](../README.md#layers-l1-l2-l3-and-overlay).

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

## Suppressing a rule

Four mechanisms, all additive. A finding is silenced if any of them applies.
Only the forty-one [pass 3](#pass-3--semantics) rules can be suppressed; naming a
schema rule is a usage error:

```console
$ netgraph validate --disable NG-D005
error: --disable: 'NG-D005' is not a known rule id; expected one of E001, …, I001,
an NG-* alias from docs/schema.md §10, or '*'
```

Every mechanism accepts both spellings of an id — `W102` and `NG-C010` select
the same rule — plus the wildcards `*`, `all` and `any`.

### 1. On the command line

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
