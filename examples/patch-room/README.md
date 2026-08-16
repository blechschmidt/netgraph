# `patch-room` — two racks and a structured-cabling plant

A machine room with two cabinets, four active devices, and a 24-position patch
panel at the top of each rack. Every server link leaves rack r1 through a panel
and arrives in rack r2 through the other one; nothing is cabled between the two
racks directly, which is what a real structured-cabling plant looks like.

It exists to demonstrate two things `docs/schema.md` adds in §15 and §3.2:

* **the `patchpanel` kind** — a passive cross-connect, and what it does to the
  graph; and
* **`metadata.location`** — where each element is bolted, and the rack
  elevation that falls out of it.

## What is in it

| Rack | Unit | Element | |
|---|---|---|---|
| r1 | U42 | `pp-r1-a` | 24-position panel |
| r1 | U40 | `rtr-edge-01` | edge router |
| r1 | U38 | `sw-core-01` | core switch |
| r2 | U42 | `pp-r2-a` | 24-position panel |
| r2 | U12–U13 | `srv-db-01` | 2U server |
| r2 | U10–U11 | `srv-app-01` | 2U server |

Seven cables. One of them — `cbl-rtr-sw` — joins two devices in the same rack
directly. The other six are two three-segment runs:

```
sw-core-01:Gi1/0/7 ── cbl-sw-pp07 ── pp-r1-a:front/7
                                     pp-r1-a:rear/7  ── cbl-tie-07 ── pp-r2-a:rear/7
                                                                      pp-r2-a:front/7 ── cbl-pp-app07 ── srv-app-01:eno1
```

`cbl-tie-07` and `cbl-tie-08` are the permanent cabling: pulled once, never
touched. The four patch leads either side of them are what an operator changes.

## The two readings

A patch panel is not a hop, so the same seven cables have two honest readings.

<!-- norun: writes an SVG into the reader's directory -->
```console
$ netviz -i examples/patch-room render --layer physical -o cabling.svg
```

draws six nodes and seven edges: the panels are there and each segment is its
own line. This is the cabling record — what a technician standing in the room
would find.

<!-- norun: writes an SVG into the reader's directory -->
```console
$ netviz -i examples/patch-room render --layer l1 -o topology.svg
```

draws four nodes and three edges. Each run is **spliced** into the single link
it electrically is, between the two active ports, carrying the sum of the
segment lengths (21 m) and the VLAN of the ports at either end. The panels are
gone, because nothing on the network can tell they are there.

The equivalence is exact: the spliced graph is the graph this inventory would
produce if `sw-core-01` were cabled straight to `srv-app-01`. That is what makes
a panel free to model — adding one to a correct inventory changes no layer but
`physical`.

`netviz path` uses the spliced reading and still names the panels, because
"which position is this run in?" is the first question when the link is down:

<!-- run: -->
```console
$ netviz -i examples/patch-room path sw-core-01 srv-app-01
...
   1  network/sw-core-01  [switch]
      out GigabitEthernet1/0/7
      ->  cable cbl-sw-pp07  (copper, 1Gbps, P-007A, 21m)  vlan 10  [via pp-r1-a front/7-rear/7, pp-r2-a rear/7-front/7]
   2  hosts/srv-app-01  [server]
      in  eno1                  10.10.0.11/24
```

## The elevations

<!-- norun: writes an SVG into the reader's directory -->
```console
$ netviz -i examples/patch-room render --layer rack -o racks.svg
```

One box per rack, one row per unit, from U42 at the top down to U1. Occupied
units name what is in them; empty ones are drawn as `·`, because how much room
is left is half of what an elevation is for. A 2U server fills two rows.

Mermaid has no way to express a grid, so `-f mermaid --layer rack` is refused
with an error naming the formats that can.
