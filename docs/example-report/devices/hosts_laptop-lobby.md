# laptop-lobby

computer hosts/laptop-lobby

[← patch-room — as-built network documentation](../README.md) · [site](../sites/root.md)

<a id="identity"></a>

## Identity

| Field | Value |
|:---|:---|
| Name | laptop-lobby |
| Qualified name | hosts/laptop-lobby |
| Kind | computer |
| Site | [(inventory root)](../sites/root.md) |
| Description | A fixed workstation on the lobby desk, associated to hq-staff. It is here so the access point's radio is a link and not a spare port; a wireless association is an ordinary cable of `medium: wireless`. |
| Vendor | Lenovo |
| Model | ThinkPad T14 |
| Serial | — |
| Declared in | hosts/poe.yaml#2:106 |

<a id="table-labels"></a>

**Labels**

| KEY | VALUE |
|:---|:---|
| role | workstation |
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
| wlan0 | wifi | yes | 8c:8c:aa:0e:00:01 | 1500 | 10.10.0.41/24 | access 10 | — | — | Associated to hq-staff on 5 GHz |

Addresses are as configured, prefix length included. VLANS reads 'mode: ids', with the native VLAN named where a trunk has one.

<a id="table-radios"></a>

**Radios and BSSs**

| SSID | RADIO | ROLE | CHANNEL | BSSID | VLAN | SECURITY |
|:---|:---|:---|:---|:---|---:|:---|
| hq-staff | [hosts/laptop-lobby:wlan0](hosts_laptop-lobby.md) | station | 44/5GHz | - | - | wpa2-eap |

The wireless detail of this element, one row per SSID per radio.


<a id="links"></a>

## Links

What is plugged into this element, and what runs over it.

<a id="table-cables"></a>

**Cables**

| PORT | CABLE | LABEL | MEDIUM | SPEED | LENGTH | FAR END | FAR PORT | VIA |
|:---|:---|:---|:---|---:|---:|:---|:---|:---|
| wlan0 | air-lobby | — | wireless | 1200Mbps | — | [ap-ceiling-01](hosts_ap-ceiling-01.md) | wlan0 | — |

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


---

netviz 0.0.3, generated 2026-07-30T00:00:00Z.
