# pp-r1-a

patchpanel panels/pp-r1-a

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | pp-r1-a |
| Qualified name | panels/pp-r1-a |
| Kind | patchpanel |
| Site | [(inventory root)](../sites/root.md) |
| Description | 24-position keystone panel at the top of rack r1. Its front positions face the switch below it; its rear positions terminate the permanent cabling to rack r2. Positions 1 to 6 are spare, which is what a panel is for. |
| Vendor | Panduit |
| Model | CPPL24WBLY |
| Serial | — |
| Declared in | panels/panels.yaml#0:1 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| role | cross-connect |
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
| Position | U42 |
| Height | 1U |

<a id="table-feeds"></a>

**Power feeds**

_No power feed is declared for this element._

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

| NAME | TYPE | UP | MAC | MTU | ADDRESSES | VLANS | VRF | AGGREGATION | DESCRIPTION |
|:---|:---|:---|:---|---:|:---|:---|:---|:---|:---|
| front/1 | ethernet | yes | — | — | — | — | — | — | front position 1 |
| front/2 | ethernet | yes | — | — | — | — | — | — | front position 2 |
| front/3 | ethernet | yes | — | — | — | — | — | — | front position 3 |
| front/4 | ethernet | yes | — | — | — | — | — | — | front position 4 |
| front/5 | ethernet | yes | — | — | — | — | — | — | front position 5 |
| front/6 | ethernet | yes | — | — | — | — | — | — | front position 6 |
| front/7 | ethernet | yes | — | — | — | — | — | — | front position 7 |
| front/8 | ethernet | yes | — | — | — | — | — | — | front position 8 |
| front/9 | ethernet | yes | — | — | — | — | — | — | front position 9 |
| front/10 | ethernet | yes | — | — | — | — | — | — | front position 10 |
| front/11 | ethernet | yes | — | — | — | — | — | — | front position 11 |
| front/12 | ethernet | yes | — | — | — | — | — | — | front position 12 |
| front/13 | ethernet | yes | — | — | — | — | — | — | front position 13 |
| front/14 | ethernet | yes | — | — | — | — | — | — | front position 14 |
| front/15 | ethernet | yes | — | — | — | — | — | — | front position 15 |
| front/16 | ethernet | yes | — | — | — | — | — | — | front position 16 |
| front/17 | ethernet | yes | — | — | — | — | — | — | front position 17 |
| front/18 | ethernet | yes | — | — | — | — | — | — | front position 18 |
| front/19 | ethernet | yes | — | — | — | — | — | — | front position 19 |
| front/20 | ethernet | yes | — | — | — | — | — | — | front position 20 |
| front/21 | ethernet | yes | — | — | — | — | — | — | front position 21 |
| front/22 | ethernet | yes | — | — | — | — | — | — | front position 22 |
| front/23 | ethernet | yes | — | — | — | — | — | — | front position 23 |
| front/24 | ethernet | yes | — | — | — | — | — | — | front position 24 |
| rear/1 | ethernet | yes | — | — | — | — | — | — | rear position 1 |
| rear/2 | ethernet | yes | — | — | — | — | — | — | rear position 2 |
| rear/3 | ethernet | yes | — | — | — | — | — | — | rear position 3 |
| rear/4 | ethernet | yes | — | — | — | — | — | — | rear position 4 |
| rear/5 | ethernet | yes | — | — | — | — | — | — | rear position 5 |
| rear/6 | ethernet | yes | — | — | — | — | — | — | rear position 6 |
| rear/7 | ethernet | yes | — | — | — | — | — | — | rear position 7 |
| rear/8 | ethernet | yes | — | — | — | — | — | — | rear position 8 |
| rear/9 | ethernet | yes | — | — | — | — | — | — | rear position 9 |
| rear/10 | ethernet | yes | — | — | — | — | — | — | rear position 10 |
| rear/11 | ethernet | yes | — | — | — | — | — | — | rear position 11 |
| rear/12 | ethernet | yes | — | — | — | — | — | — | rear position 12 |
| rear/13 | ethernet | yes | — | — | — | — | — | — | rear position 13 |
| rear/14 | ethernet | yes | — | — | — | — | — | — | rear position 14 |
| rear/15 | ethernet | yes | — | — | — | — | — | — | rear position 15 |
| rear/16 | ethernet | yes | — | — | — | — | — | — | rear position 16 |
| rear/17 | ethernet | yes | — | — | — | — | — | — | rear position 17 |
| rear/18 | ethernet | yes | — | — | — | — | — | — | rear position 18 |
| rear/19 | ethernet | yes | — | — | — | — | — | — | rear position 19 |
| rear/20 | ethernet | yes | — | — | — | — | — | — | rear position 20 |
| rear/21 | ethernet | yes | — | — | — | — | — | — | rear position 21 |
| rear/22 | ethernet | yes | — | — | — | — | — | — | rear position 22 |
| rear/23 | ethernet | yes | — | — | — | — | — | — | rear position 23 |
| rear/24 | ethernet | yes | — | — | — | — | — | — | rear position 24 |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| front/7 | cbl-sw-pp07 | P-007A | copper | 1Gbps | 1m | [sw-core-01](network_sw-core-01.md) | GigabitEthernet1/0/7 | pp-r1-a front/7→rear/7, pp-r2-a rear/7→front/7 (run sw-core-01:GigabitEthernet1/0/7 - srv-app-01:eno1) |
| rear/7 | cbl-tie-07 | T-007 | copper | 1Gbps | 18m | [pp-r2-a](panels_pp-r2-a.md) | rear/7 | pp-r1-a front/7→rear/7, pp-r2-a rear/7→front/7 (run sw-core-01:GigabitEthernet1/0/7 - srv-app-01:eno1) |
| front/8 | cbl-sw-pp08 | P-008A | copper | 1Gbps | 1m | [sw-core-01](network_sw-core-01.md) | GigabitEthernet1/0/8 | pp-r1-a front/8→rear/8, pp-r2-a rear/8→front/8 (run sw-core-01:GigabitEthernet1/0/8 - srv-db-01:eno1) |
| rear/8 | cbl-tie-08 | T-008 | copper | 1Gbps | 18m | [pp-r2-a](panels_pp-r2-a.md) | rear/8 | pp-r1-a front/8→rear/8, pp-r2-a rear/8→front/8 (run sw-core-01:GigabitEthernet1/0/8 - srv-db-01:eno1) |
| front/24 | cbl-sw-pp24 | P-024A | copper | 1Gbps | 1m | [sw-core-01](network_sw-core-01.md) | GigabitEthernet1/0/2 | pp-r1-a front/24→rear/24, pp-r2-a rear/24→front/24 (run sw-core-01:GigabitEthernet1/0/2 - sw-access-01:GigabitEthernet1/0/24) |
| rear/24 | cbl-tie-24 | T-024 | copper | 1Gbps | 18m | [pp-r2-a](panels_pp-r2-a.md) | rear/24 | pp-r1-a front/24→rear/24, pp-r2-a rear/24→front/24 (run sw-core-01:GigabitEthernet1/0/2 - sw-access-01:GigabitEthernet1/0/24) |

One row per cable that physically terminates here. VIA names the panels the run this cable is a segment of crosses, and the two ends of that run; the site page's cable schedule has every segment of it. An attachment row is an adapter's upstream rather than a cable somebody pulled.

<a id="table-tunnels"></a>

**Tunnels**

_No tunnel terminates on this element._


<a id="diagrams"></a>

## Diagrams

The drawings this element is in.

- [Cabling, patch panels included (physical)](../sites/root.md#diagrams)
- [Rack elevations (rack)](../sites/root.md#diagrams)


---

netgraph 0.1.0, generated 2026-07-30T00:00:00Z.
