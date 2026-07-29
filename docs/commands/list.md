# `netgraph list`

`netgraph list` prints what the inventory declares, one subject at a time: the
devices, the cables, the tunnels, the VLANs or the subnets. It answers "what is
in here, and how much of it?" without drawing anything — which is what you want
when the question is a count, a spelling or a missing row rather than a shape.

---

## Synopsis

<!-- generated: synopsis list -->
```text
netgraph [GLOBAL OPTIONS] list [OPTIONS] [devices|cables|tunnels|vlans|subnets]
```
<!-- /generated -->

---

## The subject argument

The single argument picks the subject, and defaults to `devices`. Each subject
has its own columns, chosen so that one terminal-width row says the useful thing
about one element:

| Subject | Columns |
|---|---|
| `devices` (the default) | `NAME`, `KIND`, `PORTS`, `ADDRESS`, `VLANS` |
| `cables` | `NAME`, `MEDIUM`, `SPEED`, `A END`, `B END`, `LENGTH` |
| `tunnels` | `NAME`, `STACK`, `VNI`, `ENCRYPTED`, `ENDS`, `ENDPOINTS` |
| `vlans` | `VLAN`, `NAME`, `ELEMENTS`, `PORTS` |
| `subnets` | `SUBNET`, `IP`, `ADDRESSES`, `ELEMENTS`, `VLANS` |

The columns that are not self-evident:

| Column | Subject | What it means |
|---|---|---|
| `NAME` | all but `vlans` | The fully-qualified name — the namespace the document sits in, then the element's own name. |
| `PORTS` | `devices` | How many interfaces the element declares, expanded ranges included. |
| `ADDRESS` | `devices` | The **first routable** address. There is room for one, and loopback is not the one that says where an element sits. |
| `VLANS` | `devices`, `subnets` | VLAN ids, ranges compacted (`10,20,99-101`), `-` for none. |
| `SPEED`, `LENGTH` | `cables` | As the document gives them, formatted; `-` where it says nothing. |
| `A END`, `B END` | `cables` | The two `element:interface` endpoints, in the order the document lists them. |
| `STACK` | `tunnels` | The resolved encapsulation stack, outermost last: `vxlan over ipsec`. |
| `ENCRYPTED` | `tunnels` | `yes`, `no`, or `underlay` — see below. |
| `ENDS` | `tunnels` | How many endpoints the tunnel has; a mesh has more than two. |
| `IP` | `subnets` | The address family, as `4` or `6`. |
| `ADDRESSES`, `ELEMENTS` | `subnets` | How many addresses are claimed in the prefix, and how many elements hold one. |
| `ELEMENTS`, `PORTS` | `vlans` | How many elements are members, and how many of their interfaces carry the VLAN. |

## Computed, not transcribed

`vlans` and `subnets` are computed from the resolved graph, not from what each
document literally says: a host on an untagged access port is listed as a member
of that VLAN even though it declares none. Loopback and link-local prefixes are
left out of `subnets`, since listing `127.0.0.0/8` once per machine would say
nothing about the addressing plan.

The point of computing them is that they cannot disagree with the pictures.
`subnets` is the same grouping
[`--layer l3`](../rendering.md#l3-prefixes-and-who-is-addressed-in-them) draws,
and `tunnels` the same resolution
[`--layer overlay`](../rendering.md#overlay-tunnels-and-what-runs-inside-what)
draws. A tunnel whose endpoints do not resolve is still listed — you are most
likely running the command *because* something is wrong — with its stack left at
its own type.

The `ENCRYPTED` column reads `underlay` for a tunnel that encrypts nothing
itself but runs inside one that does:

<!-- run: -->
```console
$ netgraph -i examples/overlay list tunnels
NAME                STACK             VNI  ENCRYPTED  ENDS  ENDPOINTS
------------------  ----------------  ---  ---------  ----  ----------------------------------------------
tunnels/wg-mesh     wireguard           -  yes           3  rtr-branch-a:wg0, rtr-branch-b:wg0, rtr-hq:wg0
tunnels/ipsec-hq-b  ipsec               -  yes           2  rtr-branch-b:ipsec0, rtr-hq:ipsec0
tunnels/vx-100      vxlan over ipsec  100  underlay      2  rtr-branch-b:vxlan100, rtr-hq:vxlan100
tunnels/gre-mgmt    gre over ipsec      -  underlay      2  rtr-branch-b:gre1, rtr-hq:gre1
tunnels/ovpn-admin  openvpn             -  yes           2  pc-branch-b:tun0, rtr-hq:ovpn0
```

A document that will not load is reported as a warning on stderr and its
elements are simply absent from the table — `list` answers about what *did*
load, and refusing to answer because an unrelated file is broken would not help.
Run [`netgraph validate`](../validation.md) for the details.

## Output formats

`-F table` is for reading and is the default. `-F json` and `-F yaml` are for
piping, and both carry **more fields than the table has room for**: the short
name and the namespace as well as the qualified name, every address rather than
the first, the bit rate as an integer rather than `1Gbps`, a cable's duplex and
label, a tunnel's transport, port, MTU and `over` chain, the full member list of
a VLAN or a prefix, and the `file#document:line` each element was read from.

There is no `csv` here: a listing is a document with nested lists in it, and
flattening one into a single row is a decision better made by whatever consumes
it. [`netgraph ipam -F csv`](ipam.md) and
[`netgraph export cable-list`](export.md) are the commands that produce a
spreadsheet on purpose.

<!-- run: -->
```console
$ netgraph -i examples/home-lab list devices
NAME               KIND      PORTS  ADDRESS           VLANS
-----------------  --------  -----  ----------------  -----
hosts/adp-usb-eth  adapter       1  192.168.10.30/24  10
hosts/laptop       computer      2  -                 10
hosts/pc-desk      computer      3  192.168.10.20/24  10
hosts/srv-nas      server        2  192.168.10.10/24  10
routers/rtr-home   router        3  192.0.2.1/32      10
switches/sw-home   switch        7  192.168.10.2/24   10
```

`hosts/laptop` shows `-` because it has no routable address of its own: it
reaches the network through the USB adapter on the row above it.

## When a listing beats a diagram

A diagram is the right answer to a question about *shape*. A listing is the right
answer to everything else, and is usually faster to act on:

* **Counting and spotting the gap.** Six devices where you expected seven is one
  glance at `list devices`; finding the missing box in a rendered graph is not.
* **Spelling.** `list devices` is the canonical source of the fully-qualified
  names that [`netgraph show`](show.md), `--neighbors-of` and
  [`netgraph path`](path.md) take.
* **Reviewing an addressing plan.** `list subnets` fits a whole campus on a
  screen, where the layer-3 diagram of one does not. When the question is *how
  full* a prefix is rather than *what* prefixes exist, go on to
  [`netgraph ipam`](ipam.md).
* **No Graphviz.** `list` needs nothing but Python, so it works in a container
  where a render exits 5.
* **Diffing.** Two `-F json` listings diff cleanly; two `.svg` files do not.

---

## Arguments

<!-- generated: arguments list -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[devices\|cables\|tunnels\|vlans\|subnets]` | no | 1 | `devices` |
<!-- /generated -->

---

## Options

<!-- generated: options list -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-F`, `--output-format` | `[table\|json\|yaml]` | `table` | table is for reading; json and yaml are for piping. |
<!-- /generated -->

---

## Exit codes

`list` reports what loaded rather than judging it, so it has no failure of its
own: validation is not run and findings do not change the code.

| Code | Meaning |
|---|---|
| `0` | The listing was printed, even if it was empty or some documents were skipped. |
| `2` | Usage error — an unknown subject, or an unknown `-F` format. |
| `3` | The inventory could not be discovered or read at all. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

---

## See also

* [`netgraph show`](show.md) — one element in full, once `list` has told you its
  name.
* [`netgraph ipam`](ipam.md) and [`docs/ipam.md`](../ipam.md) — `list subnets`
  says which prefixes exist; `ipam` says whether the plan is healthy.
* [`docs/rendering.md`](../rendering.md#layers-one-inventory-six-questions) — the
  layers whose groupings `subnets` and `tunnels` print as tables.
* [`docs/validation.md`](../validation.md) — the command to run when `list` warns
  that a document is missing from its output.
