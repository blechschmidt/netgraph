# home-lab

The smallest inventory that still exercises every element kind a home network
needs: a router, a switch, an access point, two computers, a phone, a server,
and a USB-to-Ethernet adapter. One user VLAN, plus a guest VLAN that exists on
the air and on one trunk. Two people, one robot and two groups, so the thing the
cabling cannot say — who may touch any of it — is written down beside it.

```text
home-lab/
├── routers/rtr-home.yaml
├── switches/sw-home.yaml
├── wireless/ap-home.yaml
├── hosts/
│   ├── pc-desk.yaml
│   ├── laptop.yaml
│   ├── phone.yaml
│   ├── adp-usb-eth.yaml
│   └── srv-nas.yaml
├── people/
│   ├── accounts.yaml          # three users in one file
│   └── groups.yaml            # two groups, one nested in the other
└── cables/links.yaml          # six documents in one file
```

## Topology

```text
                        ┌────────────┐
     ISP ───── wan0 ────┤  rtr-home  │
                        └─────┬──────┘
                         lan0 │ cbl-rtr-sw
                        ┌─────┴──────┐
                        │  sw-home   │  VLAN 10 "home" + VLAN 20 "guest"
                        └─┬──┬──┬──┬─┘  SVI Vlan10
         cbl-sw-desk ─────┘  │  │  └───── cbl-sw-ap
          cbl-sw-nas ────────┘  └──────── cbl-sw-dongle

  ┌─────────┐   ┌─────────┐   ┌─────────────┐        ┌──────────┐
  │ pc-desk │   │ srv-nas │   │ adp-usb-eth │╌ usb ╌ │  laptop  │
  └─────────┘   └─────────┘   └─────────────┘        └──────────┘

                        ┌────────────┐
                        │  ap-home   │  wlan0: home (VLAN 10),
                        └─────┬──────┘         home-guest (VLAN 20)
                              ╎ wl-ap-phone — home @ 36/5GHz
                        ┌─────┴──────┐
                        │   phone    │
                        └────────────┘
```

The laptop has no Ethernet port of its own. It is joined to the graph by the
adapter's `upstream.attached_to: laptop`, **not** by a cable — declaring both
would be `NG-X005`. The phone is joined by `wl-ap-phone`, a `medium: wireless`
cable: an association *is* a link, and the two `wireless` blocks at its ends are
what let the layer-2 view label it `home @ 36/5GHz`.

## Address plan

VLAN 10 (`home`) dual stack; VLAN 20 (`guest`) carries no addressed element.

| Element | Interface | IPv4 | IPv6 |
|---|---|---|---|
| `rtr-home` | `lo0` | `192.0.2.1/32` | `2001:db8::1/128` |
| `rtr-home` | `wan0` | `203.0.113.2/30` | — |
| `rtr-home` | `lan0` | `192.168.10.1/24` | `2001:db8:10::1/64` |
| `sw-home` | `Vlan10` | `192.168.10.2/24` | — |
| `ap-home` | `Vlan10` | `192.168.10.3/24` | — |
| `srv-nas` | `eth0` | `192.168.10.10/24` | `2001:db8:10::10/64` |
| `pc-desk` | `eno1` | `192.168.10.20/24` | `2001:db8:10::20/64` |
| `adp-usb-eth` | `enx001122334455` | `192.168.10.30/24` | `2001:db8:10::30/64` |
| `phone` | `en0` | `192.168.10.40/24` | `2001:db8:10::40/64` |

## Wireless

`ap-home:wlan0` is one 5 GHz radio serving two SSIDs:

| SSID | BSSID | VLAN | Security |
|---|---|---|---|
| `home` | `78:8a:20:aa:00:11` | 10 | `wpa3-psk` |
| `home-guest` | `78:8a:20:aa:00:12` | 20 | `wpa2-psk` |

`netgraph list bss` prints exactly that table, with the client radios in it too.
Both VLANs reach the uplink trunk `ap-home:eth0` ↔ `sw-home:port5`, which is
what keeps `NG-W009` quiet: an SSID mapped to a VLAN the access point carries
nowhere is an error, because clients would associate and reach nothing.

## Details worth copying

* **The switch is layer-2 only.** Its management address sits on the `Vlan10`
  SVI — a `type: vlan` interface parented on the `br0` bridge — rather than on
  a bridge port, which would be `NG-V009`. The access point is modelled the same
  way: it is a `switch` whose radio is one more bridge port.
* **Hosts declare no `vlan` block** even though the switch ports facing them
  are access ports in VLAN 10. That is the expected pairing: the host is
  untagged and inherits the port's VLAN, so `NG-C011` stays quiet. The phone
  does not declare one either: its VLAN comes from the SSID it joined.
* **The unused Wi-Fi radios are `enabled: false`.** `pc-desk` and `laptop` both
  have one. A disabled interface is exempt from `NG-I013` and from `NG-C015`, so
  spare capacity can be documented without generating warnings.
* **`rtr-home:wan0` is annotated instead.** It is up, and it terminates no cable
  because the ISP at the far end is not an element of this inventory. The
  `netgraph/ignore: "NG-C015"` annotation on the router says so once, where the
  next reader will look for the reason.
* **Membership is written on the group and nowhere else.** `household` names
  `admins` rather than repeating `ana`, so she is in both without being listed
  twice; `netgraph list groups` walks the nesting and says the group holds two
  members and reaches two people. `netgraph list users` prints the reverse index,
  which is the one fact about a person their own document cannot state.
* **`backup` is `type: service`.** It belongs to no group, which for a robot is
  the normal shape rather than an oversight — so `I004` says nothing about it.
  Marking it as a service account is what makes that distinction.
* **Every cabled interface states `mtu: 1500`**, so the two ends of each link
  agree and `NG-C010` stays quiet. The two radios do too — a link is a link.
