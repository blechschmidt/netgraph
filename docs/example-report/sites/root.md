# (inventory root)

As-built record of the inventory root.

[← patch-room — as-built network documentation](../README.md)


<a id="summary"></a>

## Summary

| Field | Value |
|:---|:---|
| Namespace | (inventory root) |
| Elements | 14 |
| Cables | 14 |
| Tunnels | 0 |
| Subnets | 3 |

- [Report overview](../README.md)


<a id="diagrams"></a>

## Diagrams

One drawing per layer, from the same inventory as the tables below.

**Cabling, patch panels included** — 10 node(s), 14 link(s)

![Cabling, patch panels included (physical)](../diagrams/root-physical.svg)

**Physical topology** — 8 node(s), 7 link(s)

![Physical topology (l1)](../diagrams/root-l1.svg)

**VLANs** — 8 node(s), 7 link(s)

![VLANs (l2)](../diagrams/root-l2.svg)

**IP subnets** — 11 node(s), 10 link(s)

![IP subnets (l3)](../diagrams/root-l3.svg)

**Rack elevations** — 2 node(s), 0 link(s)

![Rack elevations (rack)](../diagrams/root-rack.svg)

**Power: PDUs and feeds** — 11 node(s), 12 link(s)

![Power: PDUs and feeds (power)](../diagrams/root-power.svg)


<a id="devices"></a>

## Elements

Everything documented on this page, with a page each.

<a id="table-devices"></a>

**Elements**

| NAME | KIND | PORTS | ADDRESS | VLANS |
|:---|:---|---:|:---|:---|
| [hosts/ap-ceiling-01](../devices/hosts_ap-ceiling-01.md) | switch | 4 | 10.10.0.4/24 | 10 |
| [hosts/cam-lobby-01](../devices/hosts_cam-lobby-01.md) | computer | 1 | 10.10.0.31/24 | 10 |
| [hosts/laptop-lobby](../devices/hosts_laptop-lobby.md) | computer | 1 | 10.10.0.41/24 | 10 |
| [hosts/srv-app-01](../devices/hosts_srv-app-01.md) | server | 2 | 10.10.0.11/24 | - |
| [hosts/srv-db-01](../devices/hosts_srv-db-01.md) | server | 2 | 10.10.0.12/24 | - |
| [network/rtr-edge-01](../devices/network_rtr-edge-01.md) | router | 2 | 192.0.2.1/32 | - |
| [network/sw-access-01](../devices/network_sw-access-01.md) | switch | 5 | 10.10.0.3/24 | 10 |
| [network/sw-core-01](../devices/network_sw-core-01.md) | switch | 6 | 10.10.0.2/24 | 10 |
| [panels/pp-r1-a](../devices/panels_pp-r1-a.md) | patchpanel | 48 | - | 10 |
| [panels/pp-r2-a](../devices/panels_pp-r2-a.md) | patchpanel | 48 | - | 10 |
| [power/pdu-r1-a](../devices/power_pdu-r1-a.md) | pdu | 0 | - | - |
| [power/pdu-r1-b](../devices/power_pdu-r1-b.md) | pdu | 0 | - | - |
| [power/pdu-r2-a](../devices/power_pdu-r2-a.md) | pdu | 0 | - | - |
| [power/pdu-r2-b](../devices/power_pdu-r2-b.md) | pdu | 0 | - | - |


<a id="addressing"></a>

## Address plan

Every prefix an address sits in, and how full it is.

<a id="table-subnets"></a>

**Subnets**

| PREFIX | IP | VLANS | HOSTS | USED | FREE | UTIL | DEVICES |
|:---|---:|:---|---:|---:|---:|---:|---:|
| 10.0.0.0/30 | 4 | - | 2 | 2 | 0 | 100.0% | 2 |
| 10.10.0.0/24 | 4 | 10 | 254 | 7 | 247 | 2.8% | 7 |
| 192.0.2.1/32 | 4 | - | 1 | 1 | 0 | 100.0% | 1 |

HOSTS is the usable capacity of the prefix, USED the distinct addresses declared in it. Loopback and link-local prefixes are left out: they are scoped to one host or one link and say nothing about the plan.


<a id="vlans"></a>

## VLANs

Every VLAN, the prefixes carried in it and the elements that are in it.

<a id="table-vlan-summary"></a>

**VLANs**

| VLAN | NAME | ELEMENTS | PORTS |
|---:|:---|---:|---:|
| 10 | servers | 7 | 12 |

Membership is derived: a host on an untagged access port counts as a member even though it declares no VLAN itself.

<a id="table-vlan-matrix"></a>

**VLAN, subnet and element matrix**

| VLAN | NAME | ELEMENT | PORTS | MODE | SUBNETS |
|---:|:---|:---|:---|:---|:---|
| 10 | servers | [ap-ceiling-01](../devices/hosts_ap-ceiling-01.md) | Vlan10, eth0 | access | 10.10.0.0/24 |
| 10 | servers | [cam-lobby-01](../devices/hosts_cam-lobby-01.md) | eth0 | access | 10.10.0.0/24 |
| 10 | servers | [laptop-lobby](../devices/hosts_laptop-lobby.md) | wlan0 | access | 10.10.0.0/24 |
| 10 | servers | [srv-app-01](../devices/hosts_srv-app-01.md) | — | — | 10.10.0.0/24 |
| 10 | servers | [srv-db-01](../devices/hosts_srv-db-01.md) | — | — | 10.10.0.0/24 |
| 10 | servers | [sw-access-01](../devices/network_sw-access-01.md) | Vlan10, GigabitEthernet1/0/1, GigabitEthernet1/0/2, GigabitEthernet1/0/24 | access | 10.10.0.0/24 |
| 10 | servers | [sw-core-01](../devices/network_sw-core-01.md) | Vlan10, GigabitEthernet1/0/2, GigabitEthernet1/0/7, GigabitEthernet1/0/8 | access | 10.10.0.0/24 |

A row with no port is an element that reaches the VLAN over a link rather than by configuring it — a host behind an access port, or a hub.


<a id="cabling"></a>

## Cabling

What an installer would carry into the room.

<a id="table-cable-schedule"></a>

**Cable schedule**

| RUN | SEGMENT | CABLE | LABEL | MEDIUM | CATEGORY | SPEED | LENGTH | A END | A PORT | A POSITION | B END | B PORT | B POSITION |
|:---|:---|:---|:---|:---|:---|---:|---:|:---|:---|:---|:---|:---|:---|
| — | — | cbl-rtr-sw | C-001 | copper | cat6 | 1Gbps | 1m | [rtr-edge-01](../devices/network_rtr-edge-01.md) | ge-0/0/0 | hq / mdf / r1 U40 | [sw-core-01](../devices/network_sw-core-01.md) | GigabitEthernet1/0/1 | hq / mdf / r1 U38 |
| sw-core-01:GigabitEthernet1/0/2 - sw-access-01:GigabitEthernet1/0/24 | 1 of 3 | cbl-sw-pp24 | P-024A | copper | cat6 | 1Gbps | 1m | [pp-r1-a](../devices/panels_pp-r1-a.md) | front/24 | hq / mdf / r1 U42 | [sw-core-01](../devices/network_sw-core-01.md) | GigabitEthernet1/0/2 | hq / mdf / r1 U38 |
| sw-core-01:GigabitEthernet1/0/7 - srv-app-01:eno1 | 1 of 3 | cbl-sw-pp07 | P-007A | copper | cat6 | 1Gbps | 1m | [pp-r1-a](../devices/panels_pp-r1-a.md) | front/7 | hq / mdf / r1 U42 | [sw-core-01](../devices/network_sw-core-01.md) | GigabitEthernet1/0/7 | hq / mdf / r1 U38 |
| sw-core-01:GigabitEthernet1/0/8 - srv-db-01:eno1 | 1 of 3 | cbl-sw-pp08 | P-008A | copper | cat6 | 1Gbps | 1m | [pp-r1-a](../devices/panels_pp-r1-a.md) | front/8 | hq / mdf / r1 U42 | [sw-core-01](../devices/network_sw-core-01.md) | GigabitEthernet1/0/8 | hq / mdf / r1 U38 |
| sw-core-01:GigabitEthernet1/0/2 - sw-access-01:GigabitEthernet1/0/24 | 2 of 3 | cbl-tie-24 | T-024 | copper | cat6a | 1Gbps | 18m | [pp-r1-a](../devices/panels_pp-r1-a.md) | rear/24 | hq / mdf / r1 U42 | [pp-r2-a](../devices/panels_pp-r2-a.md) | rear/24 | hq / mdf / r2 U42 |
| sw-core-01:GigabitEthernet1/0/7 - srv-app-01:eno1 | 2 of 3 | cbl-tie-07 | T-007 | copper | cat6a | 1Gbps | 18m | [pp-r1-a](../devices/panels_pp-r1-a.md) | rear/7 | hq / mdf / r1 U42 | [pp-r2-a](../devices/panels_pp-r2-a.md) | rear/7 | hq / mdf / r2 U42 |
| sw-core-01:GigabitEthernet1/0/8 - srv-db-01:eno1 | 2 of 3 | cbl-tie-08 | T-008 | copper | cat6a | 1Gbps | 18m | [pp-r1-a](../devices/panels_pp-r1-a.md) | rear/8 | hq / mdf / r1 U42 | [pp-r2-a](../devices/panels_pp-r2-a.md) | rear/8 | hq / mdf / r2 U42 |
| sw-core-01:GigabitEthernet1/0/2 - sw-access-01:GigabitEthernet1/0/24 | 3 of 3 | cbl-pp-access24 | P-024B | copper | cat6 | 1Gbps | 2m | [pp-r2-a](../devices/panels_pp-r2-a.md) | front/24 | hq / mdf / r2 U42 | [sw-access-01](../devices/network_sw-access-01.md) | GigabitEthernet1/0/24 | hq / mdf / r2 U8 |
| sw-core-01:GigabitEthernet1/0/7 - srv-app-01:eno1 | 3 of 3 | cbl-pp-app07 | P-007B | copper | cat6 | 1Gbps | 2m | [pp-r2-a](../devices/panels_pp-r2-a.md) | front/7 | hq / mdf / r2 U42 | [srv-app-01](../devices/hosts_srv-app-01.md) | eno1 | hq / mdf / r2 U10 |
| sw-core-01:GigabitEthernet1/0/8 - srv-db-01:eno1 | 3 of 3 | cbl-pp-db08 | P-008B | copper | cat6 | 1Gbps | 2m | [pp-r2-a](../devices/panels_pp-r2-a.md) | front/8 | hq / mdf / r2 U42 | [srv-db-01](../devices/hosts_srv-db-01.md) | eno1 | hq / mdf / r2 U12 |
| sw-access-01:GigabitEthernet1/0/1 - ap-ceiling-01:eth0 | 1 of 2 | cbl-access-pp09 | P-009A | copper | cat6 | 1Gbps | 1m | [pp-r2-a](../devices/panels_pp-r2-a.md) | front/9 | hq / mdf / r2 U42 | [sw-access-01](../devices/network_sw-access-01.md) | GigabitEthernet1/0/1 | hq / mdf / r2 U8 |
| sw-access-01:GigabitEthernet1/0/1 - ap-ceiling-01:eth0 | 2 of 2 | cbl-pp-ap09 | T-009 | copper | cat6a | 1Gbps | 24m | [ap-ceiling-01](../devices/hosts_ap-ceiling-01.md) | eth0 | — | [pp-r2-a](../devices/panels_pp-r2-a.md) | rear/9 | hq / mdf / r2 U42 |
| — | — | air-lobby | — | wireless | — | 1200Mbps | — | [ap-ceiling-01](../devices/hosts_ap-ceiling-01.md) | wlan0 | — | [laptop-lobby](../devices/hosts_laptop-lobby.md) | wlan0 | — |
| — | — | cbl-access-cam | P-010 | copper | cat6 | 1Gbps | 12m | [cam-lobby-01](../devices/hosts_cam-lobby-01.md) | eth0 | — | [sw-access-01](../devices/network_sw-access-01.md) | GigabitEthernet1/0/2 | hq / mdf / r2 U8 |

One row per cable document, which is one run somebody pulls: a link through a patch panel is several. RUN names the end-to-end link and SEGMENT which leg of it this is. 'netgraph export cable-list' writes the same rows as CSV, with the full location of both ends.

<a id="table-panel-panels_pp-r1-a"></a>

**Patch panel pp-r1-a**

| PORT | COUPLED TO | CABLE | FAR END | FAR PORT |
|:---|:---|:---|:---|:---|
| front/1 | rear/1 | — | — | — |
| front/2 | rear/2 | — | — | — |
| front/3 | rear/3 | — | — | — |
| front/4 | rear/4 | — | — | — |
| front/5 | rear/5 | — | — | — |
| front/6 | rear/6 | — | — | — |
| front/7 | rear/7 | cbl-sw-pp07 | [sw-core-01](../devices/network_sw-core-01.md) | GigabitEthernet1/0/7 |
| front/8 | rear/8 | cbl-sw-pp08 | [sw-core-01](../devices/network_sw-core-01.md) | GigabitEthernet1/0/8 |
| front/9 | rear/9 | — | — | — |
| front/10 | rear/10 | — | — | — |
| front/11 | rear/11 | — | — | — |
| front/12 | rear/12 | — | — | — |
| front/13 | rear/13 | — | — | — |
| front/14 | rear/14 | — | — | — |
| front/15 | rear/15 | — | — | — |
| front/16 | rear/16 | — | — | — |
| front/17 | rear/17 | — | — | — |
| front/18 | rear/18 | — | — | — |
| front/19 | rear/19 | — | — | — |
| front/20 | rear/20 | — | — | — |
| front/21 | rear/21 | — | — | — |
| front/22 | rear/22 | — | — | — |
| front/23 | rear/23 | — | — | — |
| front/24 | rear/24 | cbl-sw-pp24 | [sw-core-01](../devices/network_sw-core-01.md) | GigabitEthernet1/0/2 |
| rear/1 | front/1 | — | — | — |
| rear/2 | front/2 | — | — | — |
| rear/3 | front/3 | — | — | — |
| rear/4 | front/4 | — | — | — |
| rear/5 | front/5 | — | — | — |
| rear/6 | front/6 | — | — | — |
| rear/7 | front/7 | cbl-tie-07 | [pp-r2-a](../devices/panels_pp-r2-a.md) | rear/7 |
| rear/8 | front/8 | cbl-tie-08 | [pp-r2-a](../devices/panels_pp-r2-a.md) | rear/8 |
| rear/9 | front/9 | — | — | — |
| rear/10 | front/10 | — | — | — |
| rear/11 | front/11 | — | — | — |
| rear/12 | front/12 | — | — | — |
| rear/13 | front/13 | — | — | — |
| rear/14 | front/14 | — | — | — |
| rear/15 | front/15 | — | — | — |
| rear/16 | front/16 | — | — | — |
| rear/17 | front/17 | — | — | — |
| rear/18 | front/18 | — | — | — |
| rear/19 | front/19 | — | — | — |
| rear/20 | front/20 | — | — | — |
| rear/21 | front/21 | — | — | — |
| rear/22 | front/22 | — | — | — |
| rear/23 | front/23 | — | — | — |
| rear/24 | front/24 | cbl-tie-24 | [pp-r2-a](../devices/panels_pp-r2-a.md) | rear/24 |

Every position of the panel, front and rear, patched or not. COUPLED TO is the position on the other side the run continues through.

<a id="table-panel-panels_pp-r2-a"></a>

**Patch panel pp-r2-a**

| PORT | COUPLED TO | CABLE | FAR END | FAR PORT |
|:---|:---|:---|:---|:---|
| front/1 | rear/1 | — | — | — |
| front/2 | rear/2 | — | — | — |
| front/3 | rear/3 | — | — | — |
| front/4 | rear/4 | — | — | — |
| front/5 | rear/5 | — | — | — |
| front/6 | rear/6 | — | — | — |
| front/7 | rear/7 | cbl-pp-app07 | [srv-app-01](../devices/hosts_srv-app-01.md) | eno1 |
| front/8 | rear/8 | cbl-pp-db08 | [srv-db-01](../devices/hosts_srv-db-01.md) | eno1 |
| front/9 | rear/9 | cbl-access-pp09 | [sw-access-01](../devices/network_sw-access-01.md) | GigabitEthernet1/0/1 |
| front/10 | rear/10 | — | — | — |
| front/11 | rear/11 | — | — | — |
| front/12 | rear/12 | — | — | — |
| front/13 | rear/13 | — | — | — |
| front/14 | rear/14 | — | — | — |
| front/15 | rear/15 | — | — | — |
| front/16 | rear/16 | — | — | — |
| front/17 | rear/17 | — | — | — |
| front/18 | rear/18 | — | — | — |
| front/19 | rear/19 | — | — | — |
| front/20 | rear/20 | — | — | — |
| front/21 | rear/21 | — | — | — |
| front/22 | rear/22 | — | — | — |
| front/23 | rear/23 | — | — | — |
| front/24 | rear/24 | cbl-pp-access24 | [sw-access-01](../devices/network_sw-access-01.md) | GigabitEthernet1/0/24 |
| rear/1 | front/1 | — | — | — |
| rear/2 | front/2 | — | — | — |
| rear/3 | front/3 | — | — | — |
| rear/4 | front/4 | — | — | — |
| rear/5 | front/5 | — | — | — |
| rear/6 | front/6 | — | — | — |
| rear/7 | front/7 | cbl-tie-07 | [pp-r1-a](../devices/panels_pp-r1-a.md) | rear/7 |
| rear/8 | front/8 | cbl-tie-08 | [pp-r1-a](../devices/panels_pp-r1-a.md) | rear/8 |
| rear/9 | front/9 | cbl-pp-ap09 | [ap-ceiling-01](../devices/hosts_ap-ceiling-01.md) | eth0 |
| rear/10 | front/10 | — | — | — |
| rear/11 | front/11 | — | — | — |
| rear/12 | front/12 | — | — | — |
| rear/13 | front/13 | — | — | — |
| rear/14 | front/14 | — | — | — |
| rear/15 | front/15 | — | — | — |
| rear/16 | front/16 | — | — | — |
| rear/17 | front/17 | — | — | — |
| rear/18 | front/18 | — | — | — |
| rear/19 | front/19 | — | — | — |
| rear/20 | front/20 | — | — | — |
| rear/21 | front/21 | — | — | — |
| rear/22 | front/22 | — | — | — |
| rear/23 | front/23 | — | — | — |
| rear/24 | front/24 | cbl-tie-24 | [pp-r1-a](../devices/panels_pp-r1-a.md) | rear/24 |

Every position of the panel, front and rear, patched or not. COUPLED TO is the position on the other side the run continues through.


<a id="wireless"></a>

## Wireless

Every BSS: which radio beacons which SSID, on what channel, into which VLAN.

<a id="table-bss"></a>

**BSS and SSID plan**

| SSID | RADIO | ROLE | CHANNEL | BSSID | VLAN | SECURITY |
|:---|:---|:---|:---|:---|---:|:---|
| hq-staff | [hosts/ap-ceiling-01:wlan0](../devices/hosts_ap-ceiling-01.md) | ap | 44/5GHz | 70:69:5a:0c:00:11 | 10 | wpa2-eap |
| hq-staff | [hosts/laptop-lobby:wlan0](../devices/hosts_laptop-lobby.md) | station | 44/5GHz | - | - | wpa2-eap |

One row per SSID per radio: a dual-band AP serving three networks has six.


<a id="power"></a>

## Power

Every PDU, what it feeds and how full it is.

<a id="table-pdus"></a>

**PDU load schedule**

| PDU | FEED | OUTLETS | USED | FREE | CAPACITY | LOAD | FAILOVER | UTIL | LOADS |
|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|
| [pdu-r1-a](../devices/power_pdu-r1-a.md) | utility-a | 24 | 2 | 22 | 3680 | 41.5 | 83 | 1.1% | 2 |
| [pdu-r1-b](../devices/power_pdu-r1-b.md) | ups-1 | 24 | 2 | 22 | 3680 | 41.5 | 83 | 1.1% | 2 |
| [pdu-r2-a](../devices/power_pdu-r2-a.md) | utility-a | 8 | 3 | 5 | 1840 | 492.5 | 985 | 26.8% | 3 |
| [pdu-r2-b](../devices/power_pdu-r2-b.md) | ups-1 | 8 | 3 | 5 | 1840 | 492.5 | 985 | 26.8% | 3 |

LOAD is the normal-operation figure, FAILOVER what this unit carries when its partner dies. The gap between them is the redundancy plan.


<a id="external"></a>

## Links leaving this page

Where this part of the network is joined to the rest of it.

<a id="table-external-links"></a>

**External links**

_Nothing on this page is joined to an element outside it._

These links are deliberately absent from the diagrams and the cable schedule above, which cover this page's elements only. A far end with no page of its own is outside what this report documents at all.


<a id="findings"></a>

## Validation findings

<a id="table-findings"></a>

**Findings**

_The validator reports nothing about this inventory._

Findings anchored to an element of this site.


---

netgraph 0.1.0, generated 2026-07-30T00:00:00Z.
