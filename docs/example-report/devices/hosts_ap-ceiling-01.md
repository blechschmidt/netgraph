# ap-ceiling-01

switch hosts/ap-ceiling-01

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | ap-ceiling-01 |
| Qualified name | hosts/ap-ceiling-01 |
| Kind | switch |
| Site | [(inventory root)](../sites/root.md) |
| Description | Ceiling access point above the machine room, powered over its uplink: 'powered_by: poe' says the run carrying its traffic is its only power path, so it declares no 'power.inputs' at all. The far end of that run is sw-access-01:GigabitEthernet1/0/1, reached across pp-r2-a — a run through a panel is electrically one run for power exactly as it is for frames. |
| Vendor | Cisco |
| Model | C9120AXI |
| Serial | — |
| Declared in | hosts/poe.yaml#0:1 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| env | prod |
| role | wireless |
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
| Power draw | 22 W |
| Power maximum | 25.5 W |
| Power capacity | — |

<a id="table-feeds"></a>

**Power feeds**

| KIND | SOURCE | OUTLET | PORT | RESERVED |
|:---|:---|:---|:---|---:|
| poe | [sw-access-01](network_sw-access-01.md) | — | GigabitEthernet1/0/1 | 30 W |

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

| NAME | TYPE | UP | MAC | MTU | ADDRESSES | VLANS | VRF | AGGREGATION | DESCRIPTION |
|:---|:---|:---|:---|---:|:---|:---|:---|:---|:---|
| br0 | bridge | yes | — | — | — | — | — | members: eth0, wlan0 | Bridges the radio onto the wired uplink |
| Vlan10 | vlan | yes | — | — | 10.10.0.4/24 | access 10 | — | under br0 | In-band management address of the access point |
| eth0 | ethernet | yes | 70:69:5a:0c:00:01 | 1500 | — | access 10 | — | member of br0 | PoE uplink, patched through pp-r2-a position 9 |
| wlan0 | wifi | yes | 70:69:5a:0c:00:10 | 1500 | — | — | — | member of br0 | 5 GHz radio, one SSID |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.

<a id="table-radios"></a>

**Radios and BSSs**

| SSID | RADIO | ROLE | CHANNEL | BSSID | VLAN | SECURITY |
|:---|:---|:---|:---|:---|---:|:---|
| hq-staff | [hosts/ap-ceiling-01:wlan0](hosts_ap-ceiling-01.md) | ap | 44/5GHz | 70:69:5a:0c:00:11 | 10 | wpa2-eap |

The wireless detail of this element, one row per SSID per radio.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| eth0 | cbl-pp-ap09 | T-009 | copper | 1Gbps | 24m | [pp-r2-a](panels_pp-r2-a.md) | rear/9 | pp-r2-a front/9→rear/9 (run sw-access-01:GigabitEthernet1/0/1 - ap-ceiling-01:eth0) |
| wlan0 | air-lobby | — | wireless | 1200Mbps | — | [laptop-lobby](hosts_laptop-lobby.md) | wlan0 | — |

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

netviz 0.0.2, generated 2026-07-30T00:00:00Z.
