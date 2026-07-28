# quickstart

The three-device inventory built step by step in the
[project README](../../README.md#quickstart). It is checked in so that the
walkthrough is executable rather than aspirational: the test suite loads it,
validates it and renders it on every run, and `docs/images/quickstart.svg` in
the README is produced from it.

```text
quickstart/
├── devices/
│   ├── rtr-gw.yaml
│   ├── sw-office.yaml
│   └── pc-alice.yaml
└── cables/links.yaml          # two documents in one file
```

```text
   ISP ──── wan0 ┌────────┐
                 │ rtr-gw │  203.0.113.2/30, 192.168.10.1/24
                 └───┬────┘
              lan0   │ cbl-rtr-sw
                 ┌───┴──────┐
                 │sw-office │  VLAN 10 "office", no IP of its own
                 └───┬──────┘
             port2   │ cbl-sw-alice
                 ┌───┴──────┐
                 │ pc-alice │  192.168.10.20/24
                 └──────────┘
```

## What it demonstrates

* **Folders are namespaces.** The router's full name is `devices/rtr-gw`;
  names only have to be unique within their own folder.
* **The switch has no address.** It is a layer-2 bridge, so its ports carry
  VLAN membership instead. A management address would belong on a `type: vlan`
  SVI — putting one on a bridge port is `W104`.
* **The host declares no `vlan` block.** `pc-alice` sends untagged frames and
  inherits the VLAN of the access port facing it. That pairing is expected, so
  `E005` stays quiet.
* **Every cabled interface states `mtu: 1500`**, so the two ends of each link
  agree and `W102` stays quiet.
* **The WAN port is annotated, not hidden.** `wan0` faces an ISP that is not an
  element here, so it terminates no cable — `I002` (`NG-C015`). A
  `netgraph/ignore` annotation on `rtr-gw` says that is deliberate, which is
  what an exception should look like.

```bash
netgraph -i examples/quickstart validate
netgraph -i examples/quickstart render -f svg -o topology.svg
```
