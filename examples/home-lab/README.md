# home-lab

The smallest inventory that still exercises every element kind a home network
needs: a router, a switch, two computers, a server, and a USB-to-Ethernet
adapter. Everything lives in one VLAN.

```text
home-lab/
├── routers/rtr-home.yaml
├── switches/sw-home.yaml
├── hosts/
│   ├── pc-desk.yaml
│   ├── laptop.yaml
│   ├── adp-usb-eth.yaml
│   └── srv-nas.yaml
└── cables/links.yaml          # four documents in one file
```

## Topology

```text
                      ┌────────────┐
   ISP ───── wan0 ────┤  rtr-home  │
                      └─────┬──────┘
                       lan0 │ cbl-rtr-sw
                      ┌─────┴──────┐
                      │  sw-home   │  VLAN 10 "home", SVI Vlan10
                      └─┬───┬────┬─┘
             cbl-sw-desk│   │    │cbl-sw-dongle
                        │   │    │
        ┌───────┐  ┌────┴──┐│  ┌─┴─────────────┐    ┌────────┐
        │pc-desk│  │srv-nas││  │ adp-usb-eth   │╌usb╌│ laptop │
        └───────┘  └───────┘   └───────────────┘    └────────┘
```

The laptop has no Ethernet port of its own. It is joined to the graph by the
adapter's `upstream.attached_to: laptop`, **not** by a cable — declaring both
would be `NG-X005`. The dashed edge above is that attachment.

## Address plan

Single VLAN 10 (`home`), dual stack.

| Element | Interface | IPv4 | IPv6 |
|---|---|---|---|
| `rtr-home` | `lo0` | `192.0.2.1/32` | `2001:db8::1/128` |
| `rtr-home` | `wan0` | `203.0.113.2/30` | — |
| `rtr-home` | `lan0` | `192.168.10.1/24` | `2001:db8:10::1/64` |
| `sw-home` | `Vlan10` | `192.168.10.2/24` | — |
| `srv-nas` | `eth0` | `192.168.10.10/24` | `2001:db8:10::10/64` |
| `pc-desk` | `eno1` | `192.168.10.20/24` | `2001:db8:10::20/64` |
| `adp-usb-eth` | `enx001122334455` | `192.168.10.30/24` | `2001:db8:10::30/64` |

## Details worth copying

* **The switch is layer-2 only.** Its management address sits on the `Vlan10`
  SVI — a `type: vlan` interface parented on the `br0` bridge — rather than on
  a bridge port, which would be `NG-V009`.
* **Hosts declare no `vlan` block** even though the switch ports facing them
  are access ports in VLAN 10. That is the expected pairing: the host is
  untagged and inherits the port's VLAN, so `NG-C011` stays quiet.
* **`port5` and the two Wi-Fi radios are `enabled: false`.** A disabled
  interface is exempt from `NG-I013` and from `NG-C015`, so spare capacity can
  be documented without generating warnings.
* **`rtr-home:wan0` is annotated instead.** It is up, and it terminates no cable
  because the ISP at the far end is not an element of this inventory. The
  `netgraph/ignore: "NG-C015"` annotation on the router says so once, where the
  next reader will look for the reason.
* **Every cabled interface states `mtu: 1500`**, so the two ends of each link
  agree and `NG-C010` stays quiet.
