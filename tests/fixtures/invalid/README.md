# Invalid fixtures

One minimal YAML file per semantic rule of `netviz.validate`. Each file is
**schema-valid** — it loads without a single `LoadError` — and produces
**exactly one** finding, of exactly the rule its name carries. That property is
asserted by `tests/test_examples.py`, together with the requirement that every
id in `netviz.rules.RULE_IDS` has a file here.

| File | Rule | Schema id | Trigger |
|---|---|---|---|
| `e001-unknown-endpoint.yaml` | `E001` | `NV-C002` | A cable names `pc-ghost`, which is not declared. |
| `e002-double-termination.yaml` | `E002` | `NV-C005` | Two cables land on `pc-a:eth0`. |
| `e003-duplicate-mac.yaml` | `E003` | `NV-I008` | Two unrelated ports share `00:1e:8c:00:00:01`. |
| `e004-duplicate-ip.yaml` | `E004` | `NV-A004` | `10.0.0.1/24` is claimed by two untagged ports. |
| `e005-vlan-mismatch.yaml` | `E005` | `NV-C011` | Access VLAN 10 is cabled to access VLAN 20. |
| `e006-adapter-capacity.yaml` | `E006` | `NV-X008` | `ports: 1`, two downstream interfaces. |
| `w101-unaddressed-interface.yaml` | `W101` | `NV-I013` | `pc-a:eth1` has no address and no `vlan` block. |
| `w102-mtu-mismatch.yaml` | `W102` | `NV-C010` | MTU 1500 cabled to MTU 9000. |
| `w103-orphan-device.yaml` | `W103` | `NV-C016` | A lone device, no cables at all. |
| `w104-ip-on-access-port.yaml` | `W104` | `NV-V009` | A layer-2 switch holds an IP on a bridge port. |
| `w105-lonely-subnet.yaml` | `W105` | `NV-A008` | `pc-a:eth1` is the only interface in `192.168.99.0/24`. |
| `w106-subnet-address-clash.yaml` | `W106` | `NV-A009` | `10.0.0.1/24` is claimed by two hosts, in VLAN 10 and VLAN 20. |
| `e007-stacking-cycle.yaml` | `E007` | `NV-I004` | Two `vlan` interfaces are each other's `parent`. |
| `e008-doubly-aggregated-member.yaml` | `E008` | `NV-I005` | `eth1` is a member of both `bond0` and `bond1`. |
| `e009-subinterface-vlan.yaml` | `E009` | `NV-V005` | `eth0.20` needs VLAN 20; `eth0` trunks only VLAN 10. |
| `e010-multicast-mac.yaml` | `E010` | `NV-I009` | `01:00:5e:00:00:01` has the multicast bit set. |
| `w107-addresses-on-lag-member.yaml` | `W107` | `NV-I006` | The address sits on `eth0` rather than on `bond0`. |
| `w108-mac-on-loopback.yaml` | `W108` | `NV-I007` | `lo` declares a `mac`. |
| `w109-no-cableable-interface.yaml` | `W109` | `NV-I012` | `pc-a` owns nothing but a loopback. |
| `w110-reserved-address.yaml` | `W110` | `NV-A005` | `10.0.0.0/24` is the prefix's own network address. |
| `w111-overlapping-prefixes.yaml` | `W111` | `NV-A006` | `eth0` and `eth1` are both in `10.0.0.0/24`. |
| `w112-loopback-prefix.yaml` | `W112` | `NV-A007` | A routed loopback carries `192.0.2.1/30`, not a `/32`. |
| `w113-undeclared-vlan.yaml` | `W113` | `NV-V004` | An access port is in VLAN 20; `vlans` declares only 10. |
| `w114-native-vlan-not-trunked.yaml` | `W114` | `NV-V006` | `native_vlan: 20` with `trunk_vlans: [10]`. |
| `w115-trunk-all-to-host.yaml` | `W115` | `NV-V007` | `trunk_vlans: all` is cabled to a workstation. |
| `w116-lag-member-vlan.yaml` | `W116` | `NV-V008` | `eth0` says VLAN 20, `bond0` says VLAN 10. |
| `i001-locally-administered-mac.yaml` | `I001` | `NV-I010` | `02:00:00:00:00:01` is not a vendor-assigned address. |
| `e011-wireless-medium.yaml` | `E011` | `NV-C006` | A copper cable ends on a `type: wifi` port. |
| `e012-uncableable-endpoint.yaml` | `E012` | `NV-C009` | The cable lands on `pc-a:lo`, a loopback. |
| `e013-upstream-cabled-and-attached.yaml` | `E013` | `NV-X005` | `attached_to: laptop` *and* a cable to `laptop:eth0`. |
| `e014-attachment-cycle.yaml` | `E014` | `NV-X006` | `adp-a` and `adp-b` are attached to each other. |
| `e015-unknown-attachment.yaml` | `E015` | `NV-X001` | `attached_to: ghost-host`, which is not declared. |
| `w117-self-link.yaml` | `W117` | `NV-C004` | `cbl-loop` joins `sw-a:port1` to `sw-a:port2`. |
| `w118-speed-mismatch.yaml` | `W118` | `NV-C008` | A 5Gbps upstream port on a 1Gbps cable. |
| `w119-lag-endpoint.yaml` | `W119` | `NV-C012` | The cable ends on `bond0`, not on a member. |
| `w120-half-duplex.yaml` | `W120` | `NV-C013` | `duplex: half` between two computers. |
| `w121-disconnected-topology.yaml` | `W121` | `NV-C014` | Two cabled pairs that never meet. |
| `w122-hub-subnets.yaml` | `W122` | `NV-H005` | Two hosts on one hub, in `10.0.0.0/30` and `10.0.1.0/30`. |
| `w123-unattached-adapter.yaml` | `W123` | `NV-X002` | A cabled dongle with no `attached_to`. |
| `w124-attached-to-switch.yaml` | `W124` | `NV-X007` | `attached_to` names a switch. |
| `i002-uncabled-interface.yaml` | `I002` | `NV-C015` | `pc-a:eth1` is enabled and nothing is patched into it. |
| `e016-unknown-tunnel-endpoint.yaml` | `E016` | `NV-T002` | A tunnel names `pc-ghost`, which is not declared. |
| `e017-tunnel-endpoint-type.yaml` | `E017` | `NV-T003` | A tunnel terminates on `pc-b:eth0`, an ethernet port. |
| `e018-unknown-underlay.yaml` | `E018` | `NV-T004` | `over: ipsec-core`, which is not declared. |
| `e019-encapsulation-cycle.yaml` | `E019` | `NV-T005` | Two IPsec tunnels each run inside the other. |
| `w125-underlay-does-not-reach.yaml` | `W125` | `NV-T006` | An OpenVPN mesh reaches `pc-c`; its WireGuard underlay does not. |
| `w126-tunnel-mtu.yaml` | `W126` | `NV-T011` | MTU 1420 inside an MTU 1420 tunnel, minus 69 bytes of OpenVPN. |
| `w127-cleartext-tunnel.yaml` | `W127` | `NV-T012` | A GRE tunnel over nothing that encrypts. |
| `w128-unused-tunnel-interface.yaml` | `W128` | `NV-T013` | `pc-a:wg0` exists; no `tunnel` document names it. |
| `w129-vni-clash.yaml` | `W129` | `NV-T014` | Two VXLANs on `pc-a` both claim VNI 100. |
| `i003-nonstandard-tunnel-port.yaml` | `I003` | `NV-T015` | WireGuard on 51821 rather than 51820. |
| `e032-next-hop-off-link.yaml` | `E032` | `NV-F008` | A route via `10.9.9.1`; the router is only in `10.0.0.0/30`. |
| `e033-route-device-unknown.yaml` | `E033` | `NV-F009` | `dev: eth9`, a typo for `eth0`. |
| `e034-ospf-interface-unknown.yaml` | `E034` | `NV-F010` | OSPF is enabled on `eth9`, which the router has not got. |
| `e035-bgp-asn-mismatch.yaml` | `E035` | `NV-F011` | `remote_asn: 65002` towards a router that declares AS 65003. |
| `e036-duplicate-router-id.yaml` | `E036` | `NV-F012` | Both routers claim router id `192.0.2.1`. |
| `w135-bgp-neighbour-unresolved.yaml` | `W135` | `NV-F013` | A peer at `198.51.100.9`, which nothing here is addressed at. |
| `w136-vrf-with-no-interface.yaml` | `W136` | `NV-F014` | VRF `blue` is declared; no interface binds to it. |

## Load them one at a time

Every file re-uses the names `pc-a`, `pc-b` and `cbl-a-b`, so loading this
directory as a single inventory would collide on `NV-N002` and drown the rule
under test. `load_tree` accepts a single YAML file as its root, which is how the
tests read them:

```python
inventory = load_tree(Path("tests/fixtures/invalid/e002-double-termination.yaml"))
assert [finding.rule for finding in validate(inventory)] == ["E002"]
```

## Adding one

Isolating a single finding is the whole point and is fiddly, because the
warnings interact. Two rules bite in particular:

* **W103** fires for any device that terminates no cable and hosts no adapter.
  Most fixtures therefore need a second device and a cable to keep quiet.
* **W101** fires for any enabled, non-hub interface with neither an address nor
  a `vlan` block, and it is not exempted by being uncabled. Give every
  interface an address, a `vlan` block, or `enabled: false`.
* **W105** fires for any prefix that only one element is addressed in, which a
  two-document fixture nearly always produces. Either give the address a peer,
  or put it in a point-to-point prefix (`/30`–`/32`, `/126`–`/128`), which the
  rule exempts — several fixtures here use a `/30` for exactly that reason.
* **W109** fires for any device with no `ethernet`, `wifi` or `lag` port, and
  **W111** for any element with two ports in one prefix — which the `/30` trick
  above makes easy to walk into if two of them share a network address.
* **I001** and **E010** read the first octet of every `mac`, so a made-up
  address is rarely neutral. `00:1e:8c:…` is globally administered and unicast,
  and is what the fixtures here use when the MAC is not the point.
* **I002** fires for every enabled `ethernet`/`wifi` port that terminates no
  cable, which is most ports in a minimal fixture. Mark the ones that are not
  the point `enabled: false` — that is the advice the finding itself gives —
  or cable them. `lag` aggregates are exempt and need neither.
* **W121** fires when the inventory falls into two or more islands of *two or
  more* elements each. A lone device is W103's, not W121's, so a fixture with
  one cabled pair plus one orphan is safe. A tunnel is not a physical link and
  does not join two islands.
* **W127** fires for every `gre`, `vxlan`, `geneve`, `l2tp` and `pptp` tunnel
  that is not nested inside an encrypting one. A fixture that needs a tunnel but
  is not about encryption should use `wireguard`, `ipsec` or `openvpn`.
* **W128** fires for every enabled `tunnel` interface no `tunnel` document names,
  so a fixture that declares one has to use it — or say `enabled: false`.
* **W136** fires for every declared VRF with no interface bound to it, so a
  fixture that needs a VRF for something else has to bind one.

Keep the file to the smallest set of documents that still triggers the rule,
and add a comment at the top saying what makes it fire.
