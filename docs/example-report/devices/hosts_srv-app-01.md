# srv-app-01

server hosts/srv-app-01

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | srv-app-01 |
| Qualified name | hosts/srv-app-01 |
| Kind | server |
| Site | [(inventory root)](../sites/root.md) |
| Description | Application server, rack r2. Patched through position 7. |
| Vendor | HPE |
| Model | ProLiant DL380 Gen11 |
| Serial | — |
| Declared in | hosts/servers.yaml#0:1 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| env | prod |
| role | application |
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
| Position | U10 |
| Height | 2U |
| Power draw | 420 W |
| Power maximum | 800 W |
| Power capacity | — |

<a id="table-feeds"></a>

**Power feeds**

| KIND | SOURCE | OUTLET | PORT | RESERVED |
|:---|:---|:---|:---|---:|
| outlet | [pdu-r2-a](power_pdu-r2-a.md) | 1 | — | 210 W |
| outlet | [pdu-r2-b](power_pdu-r2-b.md) | 1 | — | 210 W |

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

| NAME | TYPE | UP | MAC | MTU | ADDRESSES | VLANS | VRF | AGGREGATION | DESCRIPTION |
|:---|:---|:---|:---|---:|:---|:---|:---|:---|:---|
| lo | loopback | yes | — | — | 127.0.0.1/8 | — | — | — | — |
| eno1 | ethernet | yes | 3c:d9:2b:0a:00:11 | 1500 | 10.10.0.11/24 | — | — | — | — |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| eno1 | cbl-pp-app07 | P-007B | copper | 1Gbps | 2m | [pp-r2-a](panels_pp-r2-a.md) | front/7 | pp-r1-a front/7→rear/7, pp-r2-a rear/7→front/7 (run sw-core-01:GigabitEthernet1/0/7 - srv-app-01:eno1) |

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
