# rtr-edge-01

router network/rtr-edge-01

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | rtr-edge-01 |
| Qualified name | network/rtr-edge-01 |
| Kind | router |
| Site | [(inventory root)](../sites/root.md) |
| Description | Edge router for the machine room. It routes between the server VLAN and the outside world, and reaches the core switch over a routed (untagged) copper link in the same rack — the one run in this inventory that does not cross a patch panel. |
| Vendor | Juniper |
| Model | SRX345 |
| Serial | — |
| Declared in | network/rtr-edge-01.yaml#0:1 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| env | prod |
| role | edge |
| site | hq |

<a id="table-annotations"></a>

**Annotations**

_This element carries no annotation._


<a id="placement"></a>

## Placement and power

| Field | Value |
|:---|:---|
| Site | hq |
| Room | mdf |
| Rack | r1 |
| Position | U40 |
| Height | 1U |
| Power draw | 35 W |
| Power maximum | 150 W |
| Power capacity | — |

<a id="table-feeds"></a>

**Power feeds**

| KIND | SOURCE | OUTLET | PORT | RESERVED |
|:---|:---|:---|:---|---:|
| outlet | [pdu-r1-a](power_pdu-r1-a.md) | 2 | — | 17.5 W |
| outlet | [pdu-r1-b](power_pdu-r1-b.md) | 2 | — | 17.5 W |

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

| NAME | TYPE | UP | MAC | MTU | ADDRESSES | VLANS | VRF | AGGREGATION | DESCRIPTION |
|:---|:---|:---|:---|---:|:---|:---|:---|:---|:---|
| lo0 | loopback | yes | — | — | 192.0.2.1/32 | — | — | — | Router ID and management target |
| ge-0/0/0 | ethernet | yes | 00:05:86:0a:00:00 | 1500 | 10.0.0.1/30 | — | — | — | Routed link to sw-core-01. No 'vlan' block: this port is a layer-3 interface, not a bridge port. |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| ge-0/0/0 | cbl-rtr-sw | C-001 | copper | 1Gbps | 1m | [sw-core-01](network_sw-core-01.md) | GigabitEthernet1/0/1 | — |

One row per cable that physically terminates here. VIA names the panels the run this cable is a segment of crosses, and the two ends of that run; the site page's cable schedule has every segment of it. An attachment row is an adapter's upstream rather than a cable somebody pulled.

<a id="table-tunnels"></a>

**Tunnels**

_No tunnel terminates on this element._


<a id="diagrams"></a>

## Diagrams

The drawings this element is in.

- [Cabling, patch panels included (physical)](../sites/root.md#diagrams)
- [Physical topology (l1)](../sites/root.md#diagrams)
- [VLANs (l2)](../sites/root.md#diagrams)
- [IP subnets (l3)](../sites/root.md#diagrams)
- [Rack elevations (rack)](../sites/root.md#diagrams)
- [Power: PDUs and feeds (power)](../sites/root.md#diagrams)


---

netviz 0.0.1, generated 2026-07-30T00:00:00Z.
