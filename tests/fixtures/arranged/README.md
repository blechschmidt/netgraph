# `arranged`

The `home-lab` example with a stored arrangement (§18) beside it, for the
goldens in `tests/test_golden.py`.

It exists because a published example must not carry one. An arrangement is a
decision about a *drawing*, and `examples/` is what a reader copies to learn
what an inventory looks like; pinning the coordinates there would teach that a
diagram needs them, and would make every example diff on every layout change.
So the coordinates live here, next to the tests that assert what they produce.

`layout.yaml` was generated, and is regenerated the same way:

```bash
netviz -i tests/fixtures/arranged layout --write --layer l1 --waypoints
netviz -i tests/fixtures/arranged layout --write --layer l2 --group-by-namespace
```

Two views, deliberately different, so the goldens cover both halves of the
feature:

* **`l1`** carries edge waypoints, which `--waypoints` seeds and which the
  render turns into spline control points. It has no group boxes.
* **`l2`** carries group boxes, which only a `--group-by-namespace` render draws
  — and which netviz draws *itself*, from these numbers, because the no-op
  layout engine draws no clusters.

The numbers are Graphviz's own, run through the render path until they stopped
moving, so a render of this tree reproduces them exactly. That is what
`test_a_stored_arrangement_round_trips_through_render_unchanged` asserts against
the examples; the goldens here assert the *document* that carries them.
