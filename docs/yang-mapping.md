# YANG mapping

netgraph's field names and value spaces are not invented. They are taken from
the standard data models a real device already speaks, so that an inventory
stays comparable with what the hardware reports.

This document explains the relationship: which standards netgraph borrows from,
how each YAML key lands on a YANG node, where netgraph deliberately departs
from the models, and — the part that usually matters most — **which parts of
those models netgraph does not cover, and why**.

The per-field lookup table lives in
[`docs/schema-reference.md`](schema-reference.md); the normative path list is
[§9 of the specification](schema.md#9-yang-mapping). This document is the
reasoning around both.

* [The models netgraph borrows from](#the-models-netgraph-borrows-from)
* [Intended state, not a datastore](#intended-state-not-a-datastore)
* [RFC 8343 — interfaces](#rfc-8343--interfaces)
* [RFC 8344 — IP](#rfc-8344--ip)
* [IEEE 802.1Q — bridging and VLANs](#ieee-8021q--bridging-and-vlans)
* [IEEE 802.11 — radios, SSIDs and BSSs](#ieee-80211--radios-ssids-and-bsss)
* [Deliberately not covered](#deliberately-not-covered)
* [Things with no YANG home](#things-with-no-yang-home)
* [If you want to export](#if-you-want-to-export)

## The models netgraph borrows from

| Short name | Module | Prefix | Source |
|---|---|---|---|
| ietf-interfaces | `ietf-interfaces` (rev. 2018-02-20) | `if` | [RFC 8343](https://www.rfc-editor.org/rfc/rfc8343) |
| ietf-ip | `ietf-ip` (rev. 2018-02-14) | `ip` | [RFC 8344](https://www.rfc-editor.org/rfc/rfc8344) |
| iana-if-type | `iana-if-type` | `ianaift` | [RFC 7224](https://www.rfc-editor.org/rfc/rfc7224) + the IANA registry |
| ietf-yang-types, ietf-inet-types | — | `yang`, `inet` | [RFC 6991](https://www.rfc-editor.org/rfc/rfc6991) |
| dot1q-bridge | `ieee802-dot1q-bridge` | `dot1q` | IEEE Std 802.1Q-2018 (the 802.1Qcp YANG modules) |
| dot1q-types | `ieee802-dot1q-types` | `dot1qtypes` | IEEE Std 802.1Q-2018 |
| dot11 | `ieee802-dot11` | `dot11` | IEEE Std 802.11-2020, Annex C (the MIB the module renders) |

Borrowing has three concrete benefits, and they are the reason the schema looks
the way it does rather than like a drawing tool's file format:

1. **The value spaces are already decided.** A VLAN id is 1–4094 because
   `dot1qtypes:vlanid` says so, not because someone picked a range. An IPv6 MTU
   starts at 1280 because `ip:ipv6/mtu` does.
2. **An inventory can be diffed against a live device.** Pull the running
   configuration over NETCONF or RESTCONF, and the field names line up. What
   the network *should* be and what it *is* become comparable without a
   translation layer nobody maintains.
3. **The hard questions are already answered.** 802.1Q spent decades getting
   VLAN semantics right; adopting its model is cheaper and more correct than
   re-deriving "what does a trunk port actually do" from vendor documentation.

## Intended state, not a datastore

netgraph is a documentation and visualisation tool. It records **intended**
state: what the network is supposed to be, as reviewed and merged by humans.
That single decision explains most of the divergences below.

**It accepts `config false` nodes.** RFC 8343 marks `if:phys-address`,
`if:speed` and `if:lower-layer-if` as operational state — a device reports
them, you do not set them. netgraph lets you write them anyway, because a
burned-in MAC address and a negotiated link rate are exactly the sort of fact
an inventory exists to record. An exporter targeting a live datastore must not
write them; they are documentation here, not configuration.

**There is no "device" node.** YANG models a single device's datastore, so
there is no data node meaning "a device". In netgraph, `metadata.name` is the
datastore boundary: everything under one document's `spec` is what that one
device would report. This is why a `cable` — which is inherently *between* two
datastores — has no YANG representation at all.

**Defaults are materialised.** RFC 8344 defaults `ip:*/forwarding` to false; a
netgraph `router` defaults it to true. Once a document is loaded, the resolved
value is present rather than implied, which is what `netgraph show` prints. The
YANG default is still the fallback when nothing else supplies one.

## RFC 8343 — interfaces

`spec.interfaces[]` is `/if:interfaces/if:interface`, keyed by name.

| YAML | YANG path | Notes |
|---|---|---|
| `interfaces[].name` | `…/if:name` | List key. |
| `interfaces[].description` | `…/if:description` | |
| `interfaces[].type` | `…/if:type` | `identityref` into the `iana-if-type` registry; see the table below. |
| `interfaces[].enabled` | `…/if:enabled` | Intended admin state. Diff against `if:admin-status`, not against `if:oper-status`. |
| `interfaces[].mac` | `…/if:phys-address` | `config false`. Restricted to EUI-48 and normalised. |
| `interfaces[].parent` | `…/if:lower-layer-if` | `config false`. One entry, for `type: vlan`. |
| `interfaces[].members[]` | `…/if:lower-layer-if` | `config false`. One entry per member, for `lag` and `bridge`. |
| *(derived)* | `…/if:higher-layer-if` | `config false`. The inverse of the above; never written by hand. |
| `cable.speed` | `…/if:speed` | `config false`. Projected onto both endpoints — see [below](#things-with-no-yang-home). |

`…` expands to `/if:interfaces/if:interface`.

### Interface types

netgraph offers six types, each mapping to one IANA identity. Only the three
marked cableable can terminate a cable.

| `type` | `if:type` identity | Cableable |
|---|---|---|
| `ethernet` | `ianaift:ethernetCsmacd` | yes |
| `wifi` | `ianaift:ieee80211` | yes |
| `lag` | `ianaift:ieee8023adLag` | yes |
| `loopback` | `ianaift:softwareLoopback` | no |
| `bridge` | `ianaift:bridge` | no |
| `vlan` | `ianaift:l2vlan` | no |

The IANA registry has hundreds of identities. Six is not a limitation of the
mapping; it is the set that a *topology* document needs. Adding another is a
one-line change if a real inventory turns out to need it — but a type that no
diagram would draw differently earns nothing.

### What netgraph does not model from RFC 8343

| Node | Why not |
|---|---|
| `if:if-index` | An SNMP artefact. It is assigned by the device, changes across reboots on some platforms, and means nothing in a source-of-truth document. |
| `if:oper-status`, `if:last-change` | Operational state that changes by the second. An inventory that recorded it would be wrong immediately after being written. |
| `if:statistics` | Counters. Same reason, more so. |
| `if:link-up-down-trap-enable` | SNMP notification configuration, not topology. |
| `if:admin-status` | Redundant with `enabled`, which is the intended-state half of the same fact. |

The rule behind all five: **if a device's answer would change without anyone
editing the inventory, netgraph does not store it.** See
[Deliberately not covered](#deliberately-not-covered) for the general form.

## RFC 8344 — IP

`ietf-ip` augments `/if:interfaces/if:interface`, so `…` below expands to that
path.

| YAML | YANG path | Type |
|---|---|---|
| `interfaces[].ipv4.enabled` | `…/ip:ipv4/ip:enabled` | `boolean`, default `true` |
| `interfaces[].ipv4.forwarding` | `…/ip:ipv4/ip:forwarding` | `boolean`, default `false` |
| `interfaces[].ipv4.mtu` | `…/ip:ipv4/ip:mtu` | `uint16`, 68…65535 |
| `interfaces[].ipv4.addresses[].ip` | `…/ip:ipv4/ip:address/ip:ip` | `inet:ipv4-address-no-zone`; list key |
| `interfaces[].ipv4.addresses[].prefix_length` | `…/ip:ipv4/ip:address/ip:prefix-length` | `uint8`, 0…32 |
| `interfaces[].ipv4.addresses[].netmask` | `…/ip:ipv4/ip:address/ip:netmask` | `yang:dotted-quad`; input only |
| `interfaces[].ipv6.enabled` | `…/ip:ipv6/ip:enabled` | `boolean`, default `true` |
| `interfaces[].ipv6.forwarding` | `…/ip:ipv6/ip:forwarding` | `boolean`, default `false` |
| `interfaces[].ipv6.mtu` | `…/ip:ipv6/ip:mtu` | `uint32`, min 1280 |
| `interfaces[].ipv6.addresses[].ip` | `…/ip:ipv6/ip:address/ip:ip` | `inet:ipv6-address-no-zone`; list key |
| `interfaces[].ipv6.addresses[].prefix_length` | `…/ip:ipv6/ip:address/ip:prefix-length` | `uint8`, 0…128 |

RFC 8344 models the subnet as a `choice` with a `prefix-length` case and a
`netmask` case. netgraph accepts both spellings on input and stores the prefix
length, so a document and its normalised form always compare equal. IPv6 has no
netmask case in the RFC either, which is why `prefix_length` is mandatory
there.

Addresses are zone-free by construction: `inet:ipv4-address-no-zone` is the
type RFC 8344 uses, and a `fe80::1%eth0` in an inventory would tie the document
to one host's interface naming.

### The MTU gap

`interfaces[].mtu` — the layer-2 MTU — has **no RFC 8343 counterpart**. The
IETF interface model only has per-address-family MTUs; the layer-2 one lives in
the `ietf-interfaces-common` draft as `if-cmn:mtu`, which is not an RFC.

netgraph models it anyway, because a link MTU mismatch is one of the most
common and most miserable misconfigurations there is (`W102` exists precisely
to catch it). On export it is written to `ip:ipv4/mtu` and `ip:ipv6/mtu`,
subject to their own ranges — so an MTU below 1280 propagates to IPv4 only.
When `if-cmn:mtu` becomes standards-track, `interfaces[].mtu` maps there
directly and this paragraph goes away.

### What netgraph does not model from RFC 8344

| Node | Why not |
|---|---|
| `ip:neighbor` (ARP / NDP caches) | Operational state. A static ARP entry is configuration, but it is a routing detail rather than a topology one. |
| `ip:address/ip:origin`, `ip:address/ip:status` | `config false`: how an address was learned and whether DAD has finished. |
| `ip:dup-addr-detect-transmits` | A tuning knob with no effect on what the diagram shows. |
| `ip:autoconf` (SLAAC parameters) | Same. An autoconfigured address is not knowable from the inventory anyway. |

There is also no routing model at all: `ietf-routing` (RFC 8349), static
routes, routing protocol configuration and VRFs are out of scope.

## IEEE 802.1Q — bridging and VLANs

This is where netgraph deviates most visibly, and deliberately.

**802.1Q has no "access port" and no "trunk port."** Those are vendor CLI
abstractions layered over three independent knobs:

* the port VLAN id (`dot1q:pvid`) applied to untagged ingress frames,
* the acceptable-frame-types filter (`dot1q:acceptable-frame`),
* per-VLAN egress and untagged membership lists.

Nobody documents a network in those terms, and a schema that demanded it would
be unusable. So netgraph keeps `mode: access | trunk` as the authoring
vocabulary and expands it to the standard nodes mechanically.

### Port configuration

Augmenting `/if:interfaces/if:interface/dot1q:bridge-port`:

| YAML | YANG leaf | Value |
|---|---|---|
| `vlan.access_vlan` (mode `access`) | `dot1q:pvid` | `access_vlan` |
| `vlan.native_vlan` (mode `trunk`) | `dot1q:pvid` | `native_vlan`, or 1 when omitted |
| `vlan.ingress_filtering` | `dot1q:enable-ingress-filtering` | as given, default true |
| `vlan.acceptable_frames` | `dot1q:acceptable-frame` | as given, else derived |
| `bridge.name` | `dot1q:component-name` | the component the port belongs to |
| *(from the device kind)* | `dot1q:port-type` | `dot1q:c-vlan-bridge-port`, or `dot1q:d-bridge-port` for a `mac-bridge` |

When `acceptable_frames` is not stated, it is derived:

| Mode | `native_vlan` | `dot1q:acceptable-frame` |
|---|---|---|
| `access` | — | `admit-only-untagged-and-priority-tagged` |
| `trunk` | present | `admit-all-frames` |
| `trunk` | absent | `admit-only-VLAN-tagged-frames` |

This is the whole of the abstraction. An access port admits untagged frames and
tags them with its PVID; a trunk without a native VLAN admits only tagged
frames; a trunk with one admits both. Stating `acceptable_frames` explicitly
overrides the derivation for the rare port that does something else.

### VLAN membership

Membership is not a property of the port in 802.1Q — it is a property of the
VLAN, which lists the ports. Entries live in
`/dot1q:bridges/dot1q:bridge[name=«bridge.name»]/dot1q:component/dot1q:bridge-vlan/dot1q:vlan`:

| Situation | Effect on the VLAN entry with `dot1q:vid = V` |
|---|---|
| `mode: access`, `access_vlan: V` | port joins `dot1q:egress-ports` **and** `dot1q:untagged-ports` |
| `mode: trunk`, `V ∈ trunk_vlans` | port joins `dot1q:egress-ports` (tagged) |
| `mode: trunk`, `native_vlan: V` | port joins `dot1q:egress-ports` **and** `dot1q:untagged-ports` |

`trunk_vlans` is a `dot1qtypes:vid-range-type` — the `"10,20,100-110"` string
form — and expands to individual VLAN entries on export.

### The VLAN database and the bridge

| YAML | YANG path |
|---|---|
| `vlans[].id` | `…/dot1q:bridge-vlan/dot1q:vlan/dot1q:vid` |
| `vlans[].name` | `…/dot1q:bridge-vlan/dot1q:vlan/dot1q:name` (≤ 32 characters) |
| `bridge.name` | `/dot1q:bridges/dot1q:bridge/dot1q:name` |
| `bridge.address` | `/dot1q:bridges/dot1q:bridge/dot1q:address` |
| `bridge.type` | `/dot1q:bridges/dot1q:bridge/dot1q:bridge-type` |

A `type: vlan` sub-interface (an SVI) carries its encapsulation VID in
`vlan.access_vlan`, which maps to `dot1q:pvid` on the sub-port and identifies
the `l2vlan` interface's VLAN.

### What netgraph does not model from 802.1Q

802.1Q is enormous. netgraph implements the VLAN bridging core and nothing
else:

| Area | Why not |
|---|---|
| Spanning tree (MSTP, RSTP), `dot1q:mstp`, port roles and costs | Protocol state and tuning. STP decides which of the links you drew are *forwarding right now*; the inventory documents that the links exist. Drawing the active tree needs live state, not a file. |
| Provider bridging: S-VLANs, `dot1q:c-vlan-registration`, PEB/PB port types | The `bridge.type` identity accepts `provider-bridge` and `provider-edge-bridge` so the device can be labelled honestly, but no provider-bridging *configuration* is modelled. Almost no inventory needs it, and half-modelling it would be worse than not. |
| MVRP / GVRP dynamic VLAN registration | Dynamic membership by definition. A VLAN that appears on a port because a protocol put it there is operational state. |
| Priority and QoS: `dot1q:priority-regeneration`, PCP maps, traffic classes, shapers | These change how frames are *treated*, never where they *go*. Nothing in a topology diagram would differ. |
| The filtering database (learned MAC entries) | Operational state, and volatile. |
| Time-sensitive networking (802.1Qbv/Qbu/CB), PTP | Whole standards of their own, orthogonal to topology. |
| Port mirroring, storm control, private VLANs | Vendor-specific in practice, and none of them change the drawn graph. |
| Link aggregation control (802.1AX / LACP: rates, keys, selection) | netgraph models the *fact* that ports are aggregated, via `type: lag` and `members`. How the bundle negotiates itself is protocol configuration. |

The consistent test: **would the diagram be different?** If the answer is no,
the node is not modelled.

## IEEE 802.11 — radios, SSIDs and BSSs

The 802.11 management model is a MIB first and a YANG module second: IEEE
publishes it as `ieee802-dot11`, whose containers and leaves are the `dot11Xxx`
attributes of Annex C rendered in YANG's hyphenated lower-case spelling. That
is the spelling used below, and the paths augment the RFC 8343 interface the
radio is:

```
/if:interfaces/if:interface[if:type='ianaift:ieee80211']/dot11:wireless-interface
```

An interface of `type: wifi` already maps to `ianaift:ieee80211` (§9.1). The
`wireless` block of §6.2.6 is what hangs beneath it.

### The radio

| YAML | YANG node | Notes |
|---|---|---|
| `wireless.role` | `…/dot11:station-config/dot11:desired-bss-type` | Approximate. The MIB distinguishes infrastructure from independent BSS, not "which side am I"; `ap` and `station` are that distinction seen from the two ends, and `mesh` has no counterpart at all — 802.11s mesh STAs are a separate subtree netgraph does not model. |
| `wireless.band` | `…/dot11:phy/dot11:channel-starting-factor` | This is genuinely how 802.11 disambiguates the band: the starting factor anchors channel numbering (2407, 5000 and 5950 MHz), which is exactly why netgraph refuses a `channel` without a `band`. |
| `wireless.channel` | `…/dot11:phy/dot11:current-channel-number` | The primary 20 MHz channel. |
| `wireless.width_mhz` | `…/dot11:phy/dot11:current-channel-width` | 802.11n added the attribute; the 320 MHz value is 802.11be. |
| `wireless.tx_power_dbm` | `…/dot11:phy/dot11:current-tx-power-level` | Approximate. The MIB numbers up to eight abstract *power levels* per PHY and states their dBm elsewhere; netgraph records the dBm, because that is what a survey and a regulatory limit are both written in. |

### The service sets

| YAML | YANG node | Notes |
|---|---|---|
| `bss[].ssid` | `…/dot11:bss/dot11:ssid` (`dot11DesiredSSID` on a client) | An octet string of 1–32 bytes in both models. |
| `bss[].bssid` | `…/dot11:bss/dot11:bssid` | On an AP this is the address the radio beacons with; on a client, `dot11DesiredBSSID`. |
| `bss[].security` | `…/dot11:bss/dot11:rsna-enabled` + `dot11:privacy-invoked` | One enum standing for several booleans: `open` is neither, and the four WPA values are RSNA with a PSK or an 802.1X authentication server. The cipher suite negotiated on top is not modelled. |
| `bss[].hidden` | — | Beacon SSID suppression is vendor configuration; 802.11 has no attribute for it, and it is recorded because it explains why a network is missing from a scan. |
| `bss[].vlan` | — | 802.1Q, not 802.11: it is the VLAN the AP bridges that BSS into, and it is checked against the device's VLAN database and its ports (`NG-V004`, `NG-W009`). |

### What netgraph does not model from 802.11

| Area | Why not |
|---|---|
| Associated stations: `dot11:association-table`, RSSI, rates, last-seen | Operational state, and the most volatile in the whole model. An association a file claims is stale before the file is saved. netgraph records the associations that are *infrastructure* — a mesh backhaul, a wireless bridge, a fixed client — as `medium: wireless` links, and nothing else. |
| PHY detail: modulation, MCS sets, guard interval, spatial streams, beamforming | Capability and negotiation. Two radios differing only in MCS draw the same diagram. |
| Regulatory: country codes, DFS state, channel availability | The channel a radio is *allowed* to use is a function of where it is; netgraph records the channel it is set to. DFS radar events are live state. |
| 802.11r/k/v roaming, band steering, airtime fairness | Behaviour on top of the topology, and vendor-specific in practice. |
| RSN detail: cipher suites, AKM lists, PMK caching, 802.1X server addresses | `security` records what a reader of a diagram needs — is it open, is there a passphrase, is there an authentication server. The suite negotiation belongs to the device configuration. |
| Multiple radios per band, radio resource management | A radio is an interface here. An AP with three radios declares three `wifi` interfaces, which is the honest model; what an RRM controller then does with them is not. |

The same test applies: **would the diagram be different?**

## Deliberately not covered

The four tables above — [RFC 8343](#what-netgraph-does-not-model-from-rfc-8343),
[RFC 8344](#what-netgraph-does-not-model-from-rfc-8344),
[802.1Q](#what-netgraph-does-not-model-from-8021q) and
[802.11](#what-netgraph-does-not-model-from-80211) — are not a to-do list. They
are the scope boundary, and every entry falls into one of three buckets:

1. **Operational state.** Anything a device's answer would change without
   anyone editing the inventory: `if:oper-status`, counters, ARP and NDP
   caches, the filtering database, learned VLAN registrations. Monitoring
   systems own these; this file owns intent, and a file that claimed to know
   them would be wrong within seconds of being written.
2. **Protocol behaviour.** Spanning tree, LACP negotiation, MVRP, PTP.
   These decide what happens *on* the topology; the inventory says what the
   topology *is*. Drawing the active spanning tree needs live state, and
   pretending a file can supply it would produce a diagram that is confidently
   wrong.
3. **Configuration that changes nothing you would draw.** QoS and priority
   maps, DAD tuning, SNMP trap enables, storm control. If two inventories
   differing only in that node would render identically, the node is not
   modelled.

netgraph also models no routing at all — no `ietf-routing`, static routes, or
VRFs. It draws what is *connected*, not what is *reachable*; the two questions
have different right answers and answering both in one picture makes both
worse.

A tool that stored everything would be a configuration-management system with
an inventory attached, and it would rot for exactly the reason the diagrams it
replaced did: nobody would keep the parts nobody reads correct.

## Things with no YANG home

Three netgraph concepts have no standard counterpart, because the standards
model devices and these are not device configuration.

### Cables

A cable is *between* two datastores, so neither ietf-interfaces nor 802.1Q has
anywhere to put it. netgraph makes it a first-class element anyway — it is the
single most important fact in a topology document — and projects its fields
onto the endpoints:

| YAML | Projection |
|---|---|
| `cable.speed` | `if:speed` on both endpoint interfaces (`yang:gauge64`, bit/s, `config false`) |
| `cable.medium` | No YANG node. Informs the `ianaift` identity at export time: `ethernetCsmacd` for copper and fibre alike, `ieee80211` for `wireless`. |
| `cable.duplex`, `length_m`, `category`, `connector`, `label` | netgraph-only physical-plant metadata |

`medium` distinguishing copper from fibre while both export as
`ethernetCsmacd` is not an oversight: the IANA identity describes the MAC
layer, and a 10GBASE-SR link is `ethernetCsmacd` too. The medium is drawn
differently because a fibre run and a patch cable are different objects to the
person holding the diagram, not because the device sees them differently.

### Adapters

USB dongles, docks and media converters are hardware that presents interfaces
without being a device anyone configures.

| YAML | Projection |
|---|---|
| `upstream.name` | `if:name` on the adapter |
| `upstream.type` | `if:type` = `ianaift:usb` for `usb`/`usb-c`, `ianaift:other` otherwise |
| `upstream.speed` | `if:speed` on the upstream interface |
| `interfaces[]` | ordinary `if:interface` entries, each with `if:lower-layer-if = [upstream.name]` |
| *(derived)* | `if:higher-layer-if` on the upstream port lists every downstream interface |
| `upstream.attached_to` | No YANG node; a netgraph topology edge |

IANA registers no identity for Thunderbolt, PCIe, M.2 or SFP as a *host bus*,
which is why everything outside USB collapses to `ianaift:other`. The
distinction is preserved in the YAML because it is worth drawing.

### Namespaces, labels and annotations

The directory a document lives in becomes its namespace; `metadata.labels` and
`metadata.annotations` follow Kubernetes conventions. None of this is YANG —
it is inventory organisation, which YANG has no opinion about because a
datastore is not a folder tree.

## If you want to export

netgraph does not ship a NETCONF or RESTCONF exporter. If you write one, three
rules keep it honest:

1. **Never write a `config false` node** to a live datastore: `if:phys-address`,
   `if:speed`, `if:lower-layer-if` and `if:higher-layer-if` are all read-only
   there. netgraph stores them as documentation of intent.
2. **Expand, do not copy.** `vlan.mode` and `trunk_vlans` are netgraph
   vocabulary. The datastore wants `dot1q:pvid`, `dot1q:acceptable-frame` and
   per-VLAN port lists, produced by the tables above.
3. **Materialise defaults first.** Load the document through
   `netgraph.loader`; the models resolve `forwarding`, the address-family MTUs,
   `bridge.name` and `acceptable_frames`. `netgraph show NAME` prints exactly
   what an exporter would see.

<!-- norun: pipes into an exporter the reader writes, over an illustrative inventory -->
```bash
netgraph -i inventory show sw-access-01 -F json | your-exporter
```

The JSON graph export (`netgraph render -f json`) is the other direction: the
resolved *topology*, nodes and edges, for tools that care about connectivity
rather than device configuration.
