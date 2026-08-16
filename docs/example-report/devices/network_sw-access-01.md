# sw-access-01

switch network/sw-access-01

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | sw-access-01 |
| Qualified name | network/sw-access-01 |
| Kind | switch |
| Site | [(inventory root)](../sites/root.md) |
| Description | PoE access switch in rack r2. Its uplink leaves the rack through the panel above it, exactly as the server links do; its two PoE ports feed a ceiling access point and a lobby camera, neither of which has a power cord. The switch itself is dual-corded: 'psu1' to the A-side strip, 'psu2' to the B-side one. Both strips are in this rack, and they are on different input feeds, which is what makes 'redundant: true' true (NG-E015). |
| Vendor | Cisco |
| Model | C9300-24P |
| Serial | — |
| Declared in | network/sw-access-01.yaml#0:1 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| env | prod |
| role | access |
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
| Rack | r2 |
| Position | U8 |
| Height | 1U |
| Power draw | 55 W |
| Power maximum | 435 W |
| Power capacity | — |

<a id="table-feeds"></a>

**Power feeds**

| KIND | SOURCE | OUTLET | PORT | RESERVED |
|:---|:---|:---|:---|---:|
| outlet | [pdu-r2-a](power_pdu-r2-a.md) | 3 | — | 27.5 W |
| outlet | [pdu-r2-b](power_pdu-r2-b.md) | 3 | — | 27.5 W |

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

| NAME | TYPE | UP | MAC | MTU | ADDRESSES | VLANS | VRF | AGGREGATION | DESCRIPTION |
|:---|:---|:---|:---|---:|:---|:---|:---|:---|:---|
| br0 | bridge | yes | — | — | — | — | — | members: GigabitEthernet1/0/1, GigabitEthernet1/0/2, GigabitEthernet1/0/24 | Switching instance |
| Vlan10 | vlan | yes | — | — | 10.10.0.3/24 | access 10 | — | under br0 | In-band management address of the access switch |
| GigabitEthernet1/0/1 | ethernet | yes | 00:1b:0d:0b:00:01 | 1500 | — | access 10 | — | member of br0 | ap-ceiling-01, patched through pp-r2-a position 9. The class-4 reservation is what the 802.3at PSE takes out of the 370 W pool for this port. |
| GigabitEthernet1/0/2 | ethernet | yes | 00:1b:0d:0b:00:02 | 1500 | — | access 10 | — | member of br0 | cam-lobby-01, cabled directly. A class-2 camera. |
| GigabitEthernet1/0/24 | ethernet | yes | 00:1b:0d:0b:00:18 | 1500 | — | access 10 | — | member of br0 | Uplink to sw-core-01, patched through pp-r2-a position 24 |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| GigabitEthernet1/0/24 | cbl-pp-access24 | P-024B | copper | 1Gbps | 2m | [pp-r2-a](panels_pp-r2-a.md) | front/24 | pp-r1-a front/24→rear/24, pp-r2-a rear/24→front/24 (run sw-core-01:GigabitEthernet1/0/2 - sw-access-01:GigabitEthernet1/0/24) |
| GigabitEthernet1/0/1 | cbl-access-pp09 | P-009A | copper | 1Gbps | 1m | [pp-r2-a](panels_pp-r2-a.md) | front/9 | pp-r2-a front/9→rear/9 (run sw-access-01:GigabitEthernet1/0/1 - ap-ceiling-01:eth0) |
| GigabitEthernet1/0/2 | cbl-access-cam | P-010 | copper | 1Gbps | 12m | [cam-lobby-01](hosts_cam-lobby-01.md) | eth0 | — |

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
