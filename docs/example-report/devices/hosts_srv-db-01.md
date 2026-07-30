# srv-db-01

server hosts/srv-db-01

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | srv-db-01 |
| Qualified name | hosts/srv-db-01 |
| Kind | server |
| Site | [(inventory root)](../sites/root.md) |
| Description | Database server, rack r2. Patched through position 8. |
| Vendor | HPE |
| Model | ProLiant DL380 Gen11 |
| Serial | — |
| Declared in | hosts/servers.yaml#1:46 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| env | prod |
| role | database |
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
| Position | U12 |
| Height | 2U |
| Power draw | 510 W |
| Power maximum | 800 W |
| Power capacity | — |

<a id="table-feeds"></a>

**Power feeds**

| KIND | SOURCE | OUTLET | PORT | RESERVED |
|:---|:---|:---|:---|---:|
| outlet | [pdu-r2-a](power_pdu-r2-a.md) | 2 | — | 255 W |
| outlet | [pdu-r2-b](power_pdu-r2-b.md) | 2 | — | 255 W |

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

| NAME | TYPE | UP | MAC | MTU | ADDRESSES | VLANS | VRF | AGGREGATION | DESCRIPTION |
|:---|:---|:---|:---|---:|:---|:---|:---|:---|:---|
| lo | loopback | yes | — | — | 127.0.0.1/8 | — | — | — | — |
| eno1 | ethernet | yes | 3c:d9:2b:0a:00:12 | 1500 | 10.10.0.12/24 | — | — | — | — |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| eno1 | cbl-pp-db08 | P-008B | copper | 1Gbps | 2m | [pp-r2-a](panels_pp-r2-a.md) | front/8 | pp-r1-a front/8→rear/8, pp-r2-a rear/8→front/8 (run sw-core-01:GigabitEthernet1/0/8 - srv-db-01:eno1) |

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

netgraph 0.1.0, generated 2026-07-30T00:00:00Z.
