# campus

Three sites, 21 devices, 21 cables. A classic three-tier campus: layer-3 core
routers joined in a fibre backbone ring, layer-3 distribution switches carrying
the VLAN gateways, and layer-2 access switches trunking up to them.

```text
campus/
├── netgraph.toml                       # per-inventory configuration (all defaults)
├── backbone/cables.yaml                # the three inter-site fibres
└── sites/
    ├── north/
    │   ├── core/rtr-north-core-01.yaml
    │   ├── distribution/sw-north-dist-01.yaml
    │   ├── access/switches.yaml        # two documents
    │   ├── hosts/hosts.yaml            # three documents
    │   └── cables/links.yaml           # six documents
    ├── south/                          # same shape
    └── west/                           # same shape
```

The three sites are structurally identical, which makes the inventory a useful
diff target: anything that differs between `sites/north` and `sites/south`
beyond the site index is a mistake.

## Namespaces

Directories become namespaces (§2.2), so the access switch above is fully
qualified as `sites/north/access/sw-north-acc-01` — three levels deep. Names
are site-prefixed and therefore globally unique, which is what lets a cable in
`sites/north/cables/` name `sw-north-acc-01` without qualification: the
namespace and ancestor lookups miss, and the inventory-wide lookup finds
exactly one match.

## Topology

```text
        rtr-north-core-01 ══════ rtr-south-core-01
                 ║       backbone       ║
                 ╚═══ rtr-west-core-01 ═╝

  per site:   rtr-<site>-core-01
                     │ routed fibre, 10.<i>.0.0/30, no VLAN tag
              sw-<site>-dist-01          VLAN gateways (SVIs), forwarding: true
                  │           │
          VLAN trunk       VLAN trunk    10, 20, 30, 99 tagged; native 1
                  │           │
       sw-<site>-acc-01   sw-<site>-acc-02
          │        │              │
      pc-…-01  srv-…-01        pc-…-02
```

Access-to-distribution links are `medium: fiber`, `10Gbps`, MTU 9000, and carry
a `vlan: {mode: trunk}` block on both ends. Desk links are `medium: copper`,
`1Gbps`, MTU 1500, and untagged.

## Address plan

Site index `i` is 1 (north), 2 (south), 3 (west).

| Scope | IPv4 | IPv6 |
|---|---|---|
| Core loopback | `192.0.2.<i>/32` | `2001:db8::<i>/128` |
| Core ↔ distribution | `10.<i>.0.0/30` | `2001:db8:<i>::/64` |
| VLAN 10 `staff` | `10.<i>.10.0/24` | `2001:db8:<i>:10::/64` |
| VLAN 20 `lab` | `10.<i>.20.0/24` | `2001:db8:<i>:20::/64` |
| VLAN 99 `mgmt` | `10.<i>.99.0/24` | — |
| Backbone north↔south | `198.51.100.0/30` | `2001:db8:ff:1::/64` |
| Backbone south↔west | `198.51.100.4/30` | `2001:db8:ff:2::/64` |
| Backbone west↔north | `198.51.100.8/30` | `2001:db8:ff:3::/64` |

VLAN 30 (`voice`) is declared in every VLAN database and trunked everywhere,
but has no access port yet — the state a campus is usually in halfway through a
telephony rollout.

That plan is what the layer-3 view draws: 33 prefixes, each joined to the
elements addressed in it, with the three backbone `/30`s as the only subnets
that span two sites.

```bash
netgraph -i examples/campus list subnets
netgraph -i examples/campus render --layer l3 --namespace sites/north -f svg -o north-l3.svg
```

## Details worth copying

* **The distribution switch is a layer-3 switch**, not a bridge: it declares
  `forwarding: {ipv4: true, ipv6: true}` and hosts the `Vlan10`, `Vlan20` and
  `Vlan99` SVIs that are the gateways for the site. Because it forwards IP,
  `NG-V009` does not apply to it.
* **Its uplink `Ethernet52/1` carries addresses and no `vlan` block.** That is
  how a routed port is expressed: no bridge-port configuration at all. The core
  router's `xe-0/0/0` is its untagged counterpart.
* **Access switches are layer-2 only** and keep their management address on the
  `Vlan99` SVI.
* **Both ends of every link agree on the MTU** — 9000 across the fibre core,
  1500 to the desk — so `NG-C010` stays quiet.
* **Trunk ports face trunk ports.** `NG-C011` compares access VLANs across a
  link; two trunks are compared on their VLAN sets, which are identical here.
* **Every trunk lists its native VLAN in `trunk_vlans`.** VLAN 1 is untagged on
  those ports whether or not it is written down, so writing it down is what
  makes the document and the port agree — `W114` (`NG-V006`) is the rule that
  says so.
* **MAC addresses come from real vendor OUIs.** A locally administered address
  (`02:…`) is legal, and `I001` (`NG-I010`) reports it as information because no
  OUI lookup can trace one back to hardware.
