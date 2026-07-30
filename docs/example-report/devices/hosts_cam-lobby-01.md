# cam-lobby-01

computer hosts/cam-lobby-01

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | cam-lobby-01 |
| Qualified name | hosts/cam-lobby-01 |
| Kind | computer |
| Site | [(inventory root)](../sites/root.md) |
| Description | Lobby camera, cabled straight to the access switch and powered over that cable. A class-2 PSE port delivers 6.49 W at the powered device, which is what its 5 W draw is checked against (NG-E014). |
| Vendor | Axis |
| Model | P3265-LV |
| Serial | — |
| Declared in | hosts/poe.yaml#1:75 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| env | prod |
| role | camera |
| site | hq |

<a id="table-annotations"></a>

**Annotations**

_This element carries no annotation._


<a id="placement"></a>

## Placement and power

| Field | Value |
|:---|:---|
| Site | — |
| Room | — |
| Rack | — |
| Position | — |
| Height | 1U |
| Power draw | 5 W |
| Power maximum | 5 W |
| Power capacity | — |

<a id="table-feeds"></a>

**Power feeds**

| KIND | SOURCE | OUTLET | PORT | RESERVED |
|:---|:---|:---|:---|---:|
| poe | [sw-access-01](network_sw-access-01.md) | — | GigabitEthernet1/0/2 | 7 W |

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

| NAME | TYPE | UP | MAC | MTU | ADDRESSES | VLANS | VRF | AGGREGATION | DESCRIPTION |
|:---|:---|:---|:---|---:|:---|:---|:---|:---|:---|
| eth0 | ethernet | yes | ac:cc:8e:0d:00:01 | 1500 | 10.10.0.31/24 | access 10 | — | — | PoE uplink to sw-access-01 |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| eth0 | cbl-access-cam | P-010 | copper | 1Gbps | 12m | [sw-access-01](network_sw-access-01.md) | GigabitEthernet1/0/2 | — |

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
- [Power: PDUs and feeds (power)](../sites/root.md#diagrams)


---

netgraph 0.1.0, generated 2026-07-30T00:00:00Z.
