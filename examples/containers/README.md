# `containers/` — network namespaces and veth pairs

Two container hosts on one lab switch, and what is *inside* the hosts: five
network namespaces, one of them nested inside another, joined by four veth
pairs. It is the worked example for [§23](../../docs/schema.md#23-network-namespaces-and-veth-pairs).

```
sw-lab:ether1 ── srv-host-a:eno1
                 srv-host-a, initial namespace
                   br-tenants ─┬─ veth-blue-h  ⇄ veth-blue     → netns blue
                               └─ veth-green-h ⇄ veth-green    → netns green
                 srv-host-a, netns blue
                   veth-web-h ⇄ veth-web                       → netns blue/blue-web

sw-lab:ether2 ── srv-host-b:eno1
                 srv-host-b, initial namespace
                   veth-sbx-h ⇄ veth-sbx                       → netns sandbox
```

## What it demonstrates

**A namespace is a whole second network stack.** `srv-host-a` declares three in
`spec.netns`; each is a separate set of interface names, addresses and routes
inside one machine. Interfaces move into one with `interfaces[].netns`, and an
interface that names none is in the machine's initial namespace — the one no
document declares, because every machine has it.

**Nesting.** `blue-web` names `parent: blue`: it was created from inside `blue`
and is reached only through a veth pair that never touches the initial namespace
at all. The chain always ends at the initial namespace, and there is no depth
limit.

**A veth pair is two `ethernet` interfaces, not a new type.** Each end names the
other with `peer`, and the pairing has to be symmetric — a veth pair is created
as a pair and destroyed as a pair, so a document describing half of one
describes something that cannot be asked for.

**The two shapes a pair comes in**, one on each host:

* **bridged** — `srv-host-a` puts every host-side end into `br-tenants` and
  gives *the bridge* the tenants' gateway address, so the containers and the
  host bridge share `10.30.0.0/24`;
* **routed** — `srv-host-b` addresses both ends of its pair out of a `/30`, so
  the sandbox is reached over a route rather than a broadcast domain.

**A namespace partitions the address space, and netgraph knows it.** The `/30`
on both ends of a pair would be overlapping prefixes on one machine
([`W111`](../../docs/validation-rules.md#w111--overlapping-prefixes-on-one-element))
if the two ends were in one stack. They are not, so it is not reported — and
`10.30.0.0/24` is not a
[lonely subnet](../../docs/validation-rules.md#w105--subnet-with-a-single-member)
either, because the three parties in it are three stacks of one machine rather
than one element.

## Drawing it

The `netns` layer is the one view that draws below the machine: the element node
stands for the initial namespace, each declared namespace is a rounded box
beside it, and every box of one machine is drawn inside a frame named after it.

<!-- norun: writes containers.svg into the reader's directory -->
```console
$ netgraph -i examples/containers render --layer netns -o containers.svg
```

Solid cyan lines are veth pairs — the crossing itself, which no other layer can
draw, because at layer 1 both ends are inside one box. Dotted slate lines are
nesting: that stack created this one. Cables are kept, re-pointed at the
namespace holding the interface they land on, which is what answers the question
the view exists for — how does the stack inside this container reach the wire?

Every other layer draws the two hosts as one box each, which is exactly right
for them: `--layer l1` is the cabling, `--layer l2` the VLAN, `--layer l3` the
four prefixes.

That is also this view's limit, and the reason it exists: at layer 3 a container
and the machine hosting it are still one node, so `netgraph path` cannot trace
out of a container through its own host. See follow-up 23 in
[`docs/follow-ups.md`](../../docs/follow-ups.md).

<!-- run: -->
```console
$ netgraph -i examples/containers validate
no problems found
```
