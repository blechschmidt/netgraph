# `routed`

The `home-lab` example arranged **and routed**: an orthogonal diagram with one
hand-placed route, one nudged label and one link that disagrees with the view.
It is the fixture behind the goldens in `tests/test_golden.py` and behind the
worked example in [`docs/rendering.md`](../../../docs/rendering.md).

It exists separately from [`arranged`](../arranged/) because the two pin down
different halves of §18. `arranged` is what `netviz layout --write` *produces*
— coordinates an engine chose, seeded and settled — and its numbers change
whenever Graphviz's layout does. This one is what a **person** produces, and its
numbers must not change at all: they are typed, round, and chosen so that a
reader can check the DOT against them by eye.

Four things are pinned here, and nothing else in the suite pins any of them
together:

* **`spec.routing: orthogonal`** — the inventory-wide default, which reaches the
  DOT as `splines=ortho` for an engine-laid-out drawing and as an explicit `pos`
  per link for this one, which is fully placed.
* **`edges.cables/cbl-rtr-sw.waypoints`** — two bends, dragging the router-to-
  switch trunk up and over the diagram instead of straight through it. They are
  *interior* points: the route's two ends are the nodes, so moving either device
  carries the bends along.
* **`edges.cables/cbl-rtr-sw.label`** — the same trunk's annotation slid back
  along the route and lifted off it, which is what keeps the two cables running
  underneath legible. It reaches the DOT as an `lp`, honoured only by the no-op
  engine, which is the one place a label position can be pinned at all.
* **`edges.cables/wl-ap-phone.routing: straight`** — one link overriding the
  view. A radio association is not a cable and reads better as the straight line
  it physically is; that it can say so, and that the rest of the diagram stays
  orthogonal around it, is the whole point of a per-link style.

The node positions and sizes were seeded from `arranged` and then left alone.
The sizes are not decoration: a route netviz computes has to stop at the shape
it runs into, and netviz cannot measure a label, so
`netviz layout --write --waypoints` records the box of every node a stored
route leaves from. Without them the render falls back to a default box, says so,
and clips the route early.
