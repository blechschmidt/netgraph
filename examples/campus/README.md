# campus

Three sites, 22 devices, 22 cables. A classic three-tier campus: layer-3 core
routers joined in a fibre backbone ring, layer-3 distribution switches carrying
the VLAN gateways, and layer-2 access switches trunking up to them — with one
OSPF area over the lot, an iBGP mesh between the cores, and management in a VRF
of its own.

```text
campus/
├── netviz.toml                       # per-inventory configuration (all defaults)
├── annotations.yaml                    # the notes, areas and legends of §21
├── backbone/cables.yaml                # the three inter-site fibres
├── templates/access-switch.yaml        # a 48-port access switch, declared once
└── sites/
    ├── north/
    │   ├── core/rtr-north-core-01.yaml
    │   ├── distribution/sw-north-dist-01.yaml
    │   ├── access/switches.yaml        # three documents; the third uses the template
    │   ├── hosts/hosts.yaml            # three documents
    │   └── cables/links.yaml           # seven documents
    ├── south/                          # same shape, two access switches
    └── west/                           # same shape, two access switches
```

The three sites are structurally identical apart from `sw-north-acc-03`, which
makes the inventory a useful diff target: anything else that differs between
`sites/north` and `sites/south` beyond the site index is a mistake.

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

VLAN 99 is in the `mgmt` VRF (`rd 65001:99`) on every switch that has an SVI in
it, which is why `10.<i>.99.0/24` is listed under an instance of its own below:
a VRF is a routing table of its own, so it is an address space of its own
(§16.1).

VLAN 30 (`voice`) is declared in every VLAN database and trunked everywhere,
but has no access port yet — the state a campus is usually in halfway through a
telephony rollout.

That plan is what the layer-3 view draws: 33 prefixes, each joined to the
elements addressed in it, with the three backbone `/30`s as the only subnets
that span two sites.

<!-- run: -->
```console
$ netviz -i examples/campus list subnets
VRF   SUBNET              IP  ADDRESSES  ELEMENTS  VLANS
----  ------------------  --  ---------  --------  -----
-     10.1.0.0/30          4          2         2  -
-     10.1.10.0/24         4          3         3  10
...
-     2001:db8:ff:3::/64   6          2         2  -
mgmt  10.1.99.0/24         4          4         4  99
mgmt  10.2.99.0/24         4          3         3  99
mgmt  10.3.99.0/24         4          3         3  99
```

<!-- norun: writes an SVG into the reader's directory -->
```bash
netviz -i examples/campus render --layer l3 --namespace sites/north -f svg -o north-l3.svg
```

## Routing

One IGP, one AS, and one VRF (§16):

```text
                 iBGP, AS 65001, on the loopbacks
        rtr-north-core-01 ═══════════════════ rtr-south-core-01
                 ╚════════ rtr-west-core-01 ════════╝

  ospf area 0.0.0.0 over the three backbone /30s and, per site,
  over the core-to-distribution /30

  vrf mgmt (rd 65001:99): the Vlan99 SVI of every dist and access switch
```

* **OSPF** runs in area `0.0.0.0` on the core loopbacks, the backbone fibres and
  the core-to-distribution uplinks; on the distribution switches it runs on the
  uplink and the two user SVIs. Nobody declares an adjacency — netviz derives
  them the way the protocol does, from two OSPF interfaces addressed in one
  subnet (§16.6), which is what produces the nine edges of the routing view.
* **BGP** is a three-router iBGP mesh in AS 65001, peering on the loopbacks. The
  peer is written as an *address*, so the session resolves against
  `192.0.2.<i>/32` and the AS numbers of both ends are checked against each
  other (`NG-F011`).
* **Static routes**: each core carries a discard route for its own site summary,
  so the parts of `10.<i>.0.0/16` that are not deployed yet do not follow a
  default route back out over the backbone, plus one route pinning the next
  site's management prefix to the clockwise fibre. Each distribution switch has a
  default route into its core, and a discard default *inside* the `mgmt`
  instance — management is deliberately not routed off-site.
* **Policy-based routing** on `rtr-west-core-01` alone (§16.4), because one
  example of it is worth more than three copies. The West lab VLAN is not
  allowed to share the campus default: `spec.route_tables` declares
  `lab-egress` (table 100), two default routes of the two families are placed in
  it, and `spec.routing_policy` is what sends anything from `10.3.20.0/24` —
  and anything `spec.firewall` marked `0x1` — to that table instead of to
  `main`. The refusal above it, `prohibit` from the lab to the management
  prefix, is numbered *below* the diversion for the reason the walk demands: the
  first matching rule decides, so a rule after a `lookup` never runs.
* **A firewall** on the same router (§24), and it is there because the policy
  database above needs it. A firewall mark does not survive the wire — it is
  metadata inside one kernel, gone the moment the packet leaves — so the box
  that *routes* by `0x1` is the box that has to *set* it. `spec.zones` divides
  the router into `campus` and `backbone` (`lo0` is in neither: traffic to a
  loopback terminates on the machine, and the zone for that is `local`), and
  `spec.firewall` states a default-deny input chain with the three things that
  reach the router — an established-connection rule, iBGP from the backbone,
  SSH from the management VLAN — plus the `mark` rule the routing policy reads.
  `W152` and `W153` are what would have caught the two halves drifting apart.

<!-- norun: writes an SVG into the reader's directory -->
```bash
netviz -i examples/campus render --layer routing -f svg -o campus-routing.svg
```

The same static routes, as a script to apply. `netviz export routes` writes one
shell function per device and a dispatcher over them; the north core's function is

<!-- norun: an excerpt of a twelve-function script, quoted rather than piped -->
```sh
# sites/north/core/rtr-north-core-01
netviz_routes_sites_north_core_rtr_north_core_01() {
    ip -4 route replace blackhole 10.1.0.0/16 metric 250
    ip -4 route replace 10.2.99.0/24 via 198.51.100.2 dev xe-0/0/1 metric 200
}
```

## One switch declared from a template

`sw-north-acc-01` and `sw-north-acc-02` are written out in full: every port,
every VLAN, every trunk. `sw-north-acc-03`, in the same file, is nine lines,
because it inherits [`templates/access-switch.yaml`](templates/access-switch.yaml)
through `spec.from`:

```yaml
spec:
  from: templates/c9200l-48p
  location: Building A, Hauptstrasse 1 / floor 3 / IDF-3
  bridge:
    address: 00:1b:0d:01:a3:ff
  interfaces:
    - name: Vlan99
      ipv4:
        addresses: [10.1.99.13/24]
    - name: TenGigabitEthernet1/1/1
      mac: 00:1b:0d:01:a3:11
```

The template supplies the vendor and model, the VLAN database, the bridge, the
management SVI, the fibre uplink, and — through one `range` entry — all
forty-eight access ports, each with its own numbered description. The switch
supplies only what is particular to it: where it is, its bridge address, its
management address, and the MAC of its uplink. Interfaces merge by name, so
naming `Vlan99` adds an address to the template's SVI rather than replacing it.

The template lives in the `templates/` namespace and is reached from
`sites/north/access` by the ordinary reference rules (§2.2). It is **not** an
element: it does not appear in `netviz list devices`, in any diagram, or in
validation output. Read the merge either way round:

<!-- norun: both lines carry a trailing shell comment, and each prints a document of its own -->
```bash
netviz -i examples/campus show sw-north-acc-03 --raw   # as written
netviz -i examples/campus show sw-north-acc-03         # as merged
```

## What is said *about* the diagram

[`annotations.yaml`](annotations.yaml) is the §21 layer: three notes, three
areas and two legends, one of each shape the schema allows, in the file a reader
looking for "how do I write one of these" should be sent to.

| Document | Kind | What it demonstrates | Views |
|---|---|---|---|
| `why-fibre` | `note` | Anchored to a **link**, so the leader follows the cable. | every one |
| `mgmt-is-not-routed` | `note` | Anchored to an **element** *and* placed at a point — what dragging an anchored note produces. | `l3` |
| `voice-rollout` | `note` | Free-floating: pinned by `geometry.x`/`y` with no anchor, because it is about the inventory rather than about any one element. | `l2` |
| `backbone-ring` | `area` | Explicit `members`, because "the three cores" is a list and not a query. | `l3` |
| `site-north` | `area` | A `selector` over a namespace — the declarative form of `--collapse sites/north`, boxed rather than folded. | `l2` |
| `on-the-generator` | `area` | An explicit `geometry` rectangle: a region of the *paper*, which encloses whatever the arrangement puts in it. | `l1` |
| `key` | `legend` | `auto: layers`, so its rows are whatever the drawing actually drew and cannot go stale. | `l3` |
| `media` | `legend` | Written-out `entries`, for what the colours mean to *this* campus. | `physical` |

`spec.views` is why no two of them crowd one drawing: an annotation with no
`views` appears in every picture, and one that lists them appears only in those.

**None of the eight changes what netviz concludes.** They add no node and no
edge at any layer, move no hop in `netviz path`, write no line of generated
configuration and raise no finding — `netviz -i examples/campus validate`
still prints `no problems found`. That is asserted separately in
`tests/test_annotations.py`; §21 of `docs/schema.md` says why it has to be.

Render any layer to see them, or turn them off to see the network without its
commentary:

<!-- norun: writes an SVG into the reader's directory -->
```bash
netviz -i examples/campus render --layer l3 -f svg -o campus-l3.svg
netviz -i examples/campus render --layer l3 --no-annotations -f svg -o plain.svg
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
* **The templated switch's spare ports are `enabled: false`.** The template
  ships all forty-eight that way and a switch enables the ones it has patched,
  which is both what the hardware does and what keeps `I002` (`NG-C015`) quiet
  for a port nobody has run a cable to yet.
* **MAC addresses come from real vendor OUIs.** A locally administered address
  (`02:…`) is legal, and `I001` (`NG-I010`) reports it as information because no
  OUI lookup can trace one back to hardware.
