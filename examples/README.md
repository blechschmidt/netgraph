# Example inventories

Five complete, self-consistent inventories. All of them load without a single
schema error and validate clean against every rule in `docs/schema.md` §10 — no
suppressions, no `netgraph.toml` exemptions. They double as the golden fixtures
exercised by `tests/test_examples.py`, so a change that silently breaks them
fails the test suite.

| Inventory | Elements | What it demonstrates |
|---|---|---|
| [`quickstart/`](quickstart/) | 3 devices, 2 cables | The walkthrough in the project README, checked in so it stays executable: a router, a switch and a computer in one VLAN. |
| [`home-lab/`](home-lab/) | 5 devices, 1 adapter, 4 cables | The smallest realistic topology: one router, one switch, two computers, a server, and a USB-to-Ethernet adapter on a single VLAN. |
| [`campus/`](campus/) | 22 devices, 22 cables, 1 template | Nested namespaces across three sites, layer-3 core routers in a backbone ring, VLAN trunks between access and distribution switches, fibre uplinks, and one access switch declared from a `kind: template` document instead of by hand. |
| [`overlay/`](overlay/) | 7 devices, 6 cables, 5 tunnels | WireGuard, IPsec, OpenVPN, VXLAN and GRE over one WAN — including VXLAN and GRE nested inside the IPsec tunnel, and a three-ended mesh. |
| [`patch-room/`](patch-room/) | 4 devices, 2 patch panels, 7 cables | Two racks and a structured-cabling plant: every server link crosses two patch panels, and every element records where it is bolted. Draw it with `--layer physical` for the cabling record, `--layer l1` for the spliced topology, and `--layer rack` for the elevations. |

<!-- run: -->
```console
$ netgraph -i examples/home-lab validate
no problems found
```

<!-- norun: writes campus.svg into the reader's directory -->
```console
$ netgraph -i examples/campus render -o campus.svg
```

## Reading them

All five trees follow the layout suggested in `docs/schema.md` §2.5: directories
group elements by role, and the directory a document sits in becomes its
namespace. `examples/campus/sites/north/access/switches.yaml` declaring
`name: sw-north-acc-01` is therefore registered as
`sites/north/access/sw-north-acc-01`.

The directory tree carries no semantics of its own. Cables refer to devices by
their plain `metadata.name`, and the loader resolves each reference outwards —
own namespace first, then each ancestor, then the inventory as a whole (§2.2).
Every element name in these examples is globally unique, so a cable in
`sites/north/cables/` reaches a switch in `sites/north/access/` through the
final, inventory-wide step. A `tunnel` resolves its endpoints the same way,
which is how `examples/overlay/tunnels/` reaches routers in three different
sites.

## Invalid fixtures

`tests/fixtures/invalid/` holds the mirror image: one minimal YAML file per
semantic rule, each triggering exactly that one finding and nothing else. See
the README there.
