# pdu-r2-a

pdu power/pdu-r2-a

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | pdu-r2-a |
| Qualified name | power/pdu-r2-a |
| Kind | pdu |
| Site | [(inventory root)](../sites/root.md) |
| Description | A-side strip in rack r2, on the same utility feed as pdu-r1-a. Horizontal rather than vertical, so unlike the r1 pair it occupies a rack unit and appears on the elevation with its utilisation beside it. |
| Vendor | APC |
| Model | AP7921B |
| Serial | — |
| Declared in | power/pdus.yaml#2:48 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| feed | a |
| role | power |
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
| Position | U1 |
| Height | 1U |
| Power draw | — |
| Power maximum | — |
| Power capacity | 1840 W |

<a id="table-feeds"></a>

**Power feeds**

_No power feed is declared for this element._

A dual-corded element has one row per cord.


<a id="interfaces"></a>

## Interfaces

<a id="table-interfaces"></a>

**Interfaces**

_This element declares no interface._

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.

<a id="table-outlets"></a>

**Outlets**

| OUTLET | FEEDS |
|:---|:---|
| 1 | srv-app-01 |
| 2 | srv-db-01 |
| 3 | sw-access-01 |
| 4 | — |
| 5 | — |
| 6 | — |
| 7 | — |
| 8 | — |


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

_Nothing is cabled to this element._

One row per cable that physically terminates here. VIA names the panels the run this cable is a segment of crosses, and the two ends of that run; the site page's cable schedule has every segment of it. An attachment row is an adapter's upstream rather than a cable somebody pulled.

<a id="table-tunnels"></a>

**Tunnels**

_No tunnel terminates on this element._


<a id="diagrams"></a>

## Diagrams

The drawings this element is in.

- [Rack elevations (rack)](../sites/root.md#diagrams)
- [Power: PDUs and feeds (power)](../sites/root.md#diagrams)


---

netgraph 0.1.0, generated 2026-07-30T00:00:00Z.
