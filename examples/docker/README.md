# `docker/` — a container runtime at the scale it really runs at

Three Docker hosts on one lab switch, sixteen network namespaces between them,
thirteen veth pairs, a swarm overlay stretched between two of them, and the
firewall zones that decide what crosses. It is
[§23](../../docs/schema.md#23-network-namespaces-and-veth-pairs) and
[§24](../../docs/schema.md#24-firewalls-zones-and-policy) together, on the
one kind of machine where they cannot be read apart:
[`containers/`](../containers/) is the same idea small enough to hold in your
head, and this is what a runtime does to it.

```
sw-dock-01:ether1,3 ── srv-dock-01     docker0, br-app, br-data, 7 namespaces
                         c-legacy, c-web, c-api, c-db      containers
                         ov-app                            the overlay's sandbox
                         dind ─ dind-ci                    a second daemon, and its container
sw-dock-01:ether2,4 ── srv-dock-02     br-edge, 2 namespaces
                         c-svc, ov-app
sw-dock-01:ether5   ── srv-dock-03     br-pod, eno1.22, 7 namespaces
                         c-pod                             two containers, one stack
                         c-mv, c-ipv                       on the LAN, no veth pair
                         rl-alice ─ rl-alice-app ─ rl-alice-build
                         rl-bob                            a second user's daemon
```

## What each host is for

**`srv-dock-01` — the networks `docker network ls` prints.** The default bridge,
a user-defined bridge, an `--internal` one, and a swarm overlay whose bridge and
VXLAN device live in a sandbox namespace that belongs to the *network* rather
than to any container. Plus Docker-in-Docker: a second daemon with a `docker0`
of its own, and a container under that. Its firewall is `iptables -S` on a
Docker host written as zones — `DOCKER-USER`, the isolation chains that make two
user-defined networks two networks, the per-network masquerade, the hairpin
rule, and `-p 443:8443` as the two rules it really is. A policy route sends one
network's marked HTTPS out of the second uplink, which is
[§16.4](../../docs/schema.md#164-routing_policy--policy-based-routing) and §24 meeting.

**`srv-dock-02` — the other end of the overlay.** One bridge, one container, and
the half of the picture that shows what a VXLAN is for: two containers on two
machines in one broadcast domain that no cable in this inventory carries. Its
firewall is much shorter, and what survives the shrinking is what every Docker
host has.

**`srv-dock-03` — the three shapes that break the pattern.** A pod, two drivers
that use no veth pair, and two rootless daemons. See below.

## The things only the third host says

**A pod is a namespace, not a machine.** `c-pod` holds one interface and runs
two containers: `docker run --network container:app` starts the sidecar in the
namespace the app already has. One veth pair, one address, one routing table,
and traffic between the two that never becomes a packet on any link — which is
exactly why nothing in the diagram draws it, and why a Kubernetes pod is
modelled here as one namespace rather than as two of anything.

**Two networks that enter a namespace without a veth pair.** `c-mv` is on a
macvlan network and `c-ipv` on an ipvlan one, both parented on `eno1.22`. Their
interfaces are moved into the namespace as slaves of that parent, so neither
names a `peer`, neither is a bridge port, and both are addressed out of the
lab's own `10.20.2.0/24` rather than a private pool — a macvlan container *is* on
the LAN. In the [`netns` view](#drawing-it) they hang off the machine by a
nesting line and nothing else, which is the truth: nothing on this host carries
their traffic.

**The host holds no address on the segment its own containers are on.** VLAN 22
is trunked to `srv-dock-03` and terminated on `eno1.22`, which has no address:
the parent of a macvlan network carries none, because the machine is not a
member of the network it is carrying. `tests.yaml` asserts it, because giving
that interface an address is a one-line change that silently makes the host a
member.

**Rootless Docker is a network the host cannot see.** `rl-alice` is the
namespace `rootlesskit` makes before a user's `dockerd` starts. Nothing in the
host's stack points into it — slirp4netns holds a tap device inside and a file
descriptor outside, so there is no veth pair, no bridge port, and no interface
for a firewall rule to name. Inside it: a `docker0`, a container
(`rl-alice-app`), and inside *that* a network namespace per build from
buildkit's CNI worker (`rl-alice-build`). Three levels below the machine, and
nothing stops a fourth.

**Two stacks, one address, no clash.** `rl-bob` is a second user's daemon, and
its tap carries the same `10.0.2.100/24` alice's does, because slirp4netns gives
every namespace it is asked about the same numbering. netgraph does not report a
duplicate, for the same reason it does not report `dind-docker0` twice over on
`srv-dock-01`: a namespace is an address space.

**A host firewall that does not grow with the container count.** `srv-dock-03`
runs eight containers behind three zones and three rules. That is not an
omission — it is what those drivers do. A macvlan frame leaves by the parent
without passing this stack's forward hook, and the rootless daemon's NAT is done
in userspace inside a namespace, so a `nat` table on the machine cannot reach
`172.29.0.0/24` at all. The `pod` network is the only container network here
that the host routes, and it is the only one with a rule and a masquerade.

## Drawing it

<!-- norun: writes docker.svg into the reader's directory -->
```console
$ netgraph -i examples/docker render --layer netns -o docker.svg
```

The `netns` layer is the one view that draws below the machine: the element node
is the initial namespace, each declared namespace is a rounded box, every box of
one machine sits in a frame named after it, solid cyan lines are veth pairs and
dotted slate lines are nesting. `--layer security` draws the zones and what the
policy lets cross between them; `--layer overlay` draws the VXLAN alone;
`--layer l1` and `--layer l2` draw what a cable tester and a switch see, which
is three servers and five wires.

## What it could not write down

Three limitations, each recorded rather than hidden, because a reader who copies
the shape will meet all of them:

* **Two containers on one host cannot both have an `eth0`**
  ([follow-up 24](../../docs/follow-ups.md)). Interface names are unique within
  a device, so every container's interfaces are written `<container>-eth<n>`.
  That is readable and is not what `ip link` would tell you.
* **A private prefix behind a masquerade is not globally unique, and the
  inventory says it is** ([follow-up 25](../../docs/follow-ups.md)). Every real
  Docker host has `docker0` at `172.17.0.1/16`; giving each host here a pool of
  its own is a lie of omission, and both places where it is told say so.
* **There is no interface type that says "macvlan slave" or "tap"**
  ([follow-up 26](../../docs/follow-ups.md)). `I002` exempts veth ends because
  they can never terminate a cable; the four interfaces on `srv-dock-03` that
  also never can are not distinguishable from a spare port, so the device
  carries a `netgraph/ignore: NG-C015` annotation saying which four and why.

## Checking it

<!-- run: -->
```console
$ netgraph -i examples/docker validate
no problems found
```

<!-- run: -->
```console
$ netgraph -i examples/docker test
ok    containers  13 passed  (what the three Docker hosts promise about their containers)

13 passed in 1 suite
```

`tests.yaml` is where the design's claims are written as assertions: that every
declared namespace holds an interface, that nothing is nested deeper than a
build inside a rootless container *and* that the build is still three levels
down, that every overlay interface carries the VXLAN's reduced MTU, that every
container interface is one end of a veth pair or one of the four that cannot be,
and that no container network reaches the wire unmasqueraded.
