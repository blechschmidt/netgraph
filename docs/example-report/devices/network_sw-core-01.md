# sw-core-01

switch network/sw-core-01

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | sw-core-01 |
| Qualified name | network/sw-core-01 |
| Kind | switch |
| Site | [(inventory root)](../sites/root.md) |
| Description | Core switch, rack r1. Its two server ports leave the rack through the patch panel above it: nothing is cabled from here to rack r2 directly, which is what a structured-cabling plant looks like. |
| Vendor | Cisco |
| Model | C9300-24T |
| Serial | — |
| Declared in | network/sw-core-01.yaml#0:1 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| env | prod |
| role | core |
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
| Position | U38 |
| Height | 1U |
| Power draw | 48 W |
| Power maximum | 125 W |
| Power capacity | — |

<a id="table-feeds"></a>

**Power feeds**

| KIND | SOURCE | OUTLET | PORT | RESERVED |
|:---|:---|:---|:---|---:|
| outlet | [pdu-r1-a](power_pdu-r1-a.md) | 1 | — | 24 W |
| outlet | [pdu-r1-b](power_pdu-r1-b.md) | 1 | — | 24 W |

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

| NAME | TYPE | UP | MAC | MTU | ADDRESSES | VLANS | VRF | AGGREGATION | DESCRIPTION |
|:---|:---|:---|:---|---:|:---|:---|:---|:---|:---|
| br0 | bridge | yes | — | — | — | — | — | members: GigabitEthernet1/0/2, GigabitEthernet1/0/7, GigabitEthernet1/0/8 | Switching instance |
| Vlan10 | vlan | yes | — | — | 10.10.0.2/24 | access 10 | — | under br0 | Server gateway on the switch, for in-band management |
| GigabitEthernet1/0/1 | ethernet | yes | 00:1b:0d:0a:00:01 | 1500 | 10.0.0.2/30 | — | — | — | Routed link to rtr-edge-01. No 'vlan' block: this port is a layer-3 interface, not a bridge port. |
| GigabitEthernet1/0/2 | ethernet | yes | 00:1b:0d:0a:00:02 | 1500 | — | access 10 | — | member of br0 | Uplink to sw-access-01 in rack r2, patched through pp-r1-a position 24. |
| GigabitEthernet1/0/7 | ethernet | yes | 00:1b:0d:0a:00:07 | 1500 | — | access 10 | — | member of br0 | srv-app-01, patched through pp-r1-a position 7 |
| GigabitEthernet1/0/8 | ethernet | yes | 00:1b:0d:0a:00:08 | 1500 | — | access 10 | — | member of br0 | srv-db-01, patched through pp-r1-a position 8 |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| GigabitEthernet1/0/1 | cbl-rtr-sw | C-001 | copper | 1Gbps | 1m | [rtr-edge-01](network_rtr-edge-01.md) | ge-0/0/0 | — |
| GigabitEthernet1/0/7 | cbl-sw-pp07 | P-007A | copper | 1Gbps | 1m | [pp-r1-a](panels_pp-r1-a.md) | front/7 | pp-r1-a front/7→rear/7, pp-r2-a rear/7→front/7 (run sw-core-01:GigabitEthernet1/0/7 - srv-app-01:eno1) |
| GigabitEthernet1/0/8 | cbl-sw-pp08 | P-008A | copper | 1Gbps | 1m | [pp-r1-a](panels_pp-r1-a.md) | front/8 | pp-r1-a front/8→rear/8, pp-r2-a rear/8→front/8 (run sw-core-01:GigabitEthernet1/0/8 - srv-db-01:eno1) |
| GigabitEthernet1/0/2 | cbl-sw-pp24 | P-024A | copper | 1Gbps | 1m | [pp-r1-a](panels_pp-r1-a.md) | front/24 | pp-r1-a front/24→rear/24, pp-r2-a rear/24→front/24 (run sw-core-01:GigabitEthernet1/0/2 - sw-access-01:GigabitEthernet1/0/24) |

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

netviz 0.0.2, generated 2026-07-30T00:00:00Z.
