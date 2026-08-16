# `obstructed`

A diagram arranged **on purpose** so that every orthogonal route the local rule
draws runs across a box it is not attached to. It exists to reproduce the defect
[`docs/follow-ups.md` §19](../../../docs/follow-ups.md) recorded, so that the
fix for it is measured rather than admired:

```console
$ tools/route_crossings.py tests/fixtures/obstructed

              crossings  links cut  re-routed  median ms
--------------------------------------------------------
--no-avoid            5          5          0        0.1
--avoid               0          0          3        3.0
```

Five, then zero. `tests/test_avoid.py` asserts both numbers, so neither the
defect nor the fix can go quiet.

## Why each device is here

Every position is typed, round, and chosen to cause one specific crossing.
`sw-mid`, `sw-blocker` and `sw-wall` are cabled to nothing at all — they are
obstacles, and `NV-W103` says so about each of them, which is correct and is the
only thing this inventory is warned about.

| Device | Position | What it is for |
|---|---|---|
| `sw-a`, `sw-b` | `(0, 0)`, `(900, 0)` | The top row. |
| `sw-mid` | `(450, 0)` | Exactly between them. The Z route `cbl-a-b` gets runs straight through it, because the Z turns half way along its dominant axis and that is where `sw-mid` is. |
| `sw-c`, `sw-d` | `(0, -300)`, `(900, -300)` | The bottom row, three cables wide. |
| `sw-blocker` | `(450, -300)` | The same crossing again, but for `cbl-c-d-1`, `-2` and `-3` at once — so the fix has to get three parallel cables past one box **as a bundle**, in lanes, rather than as three separate detours that fan out and re-converge. |
| `sw-wall` | `(0, -460)` | A wide box across the corridor between `rtr` and `sw-c`. |
| `rtr` | `(450, -620)` | Behind the wall. `cbl-rtr-c` carries one bend at `(700, -620)` that a person placed; the L the leg *after* it takes runs down `x = 0` straight through `sw-wall`. Routing has to fill that leg and leave the bend exactly where it is. |

`cbl-a-c` and `cbl-b-d` are the control: two vertical cables with nothing in
their way. They must come out of a render with avoidance on **byte-identically**
to a render with it off, and `tests/test_avoid.py` checks that they do. A fix
that redraws the whole diagram to remove five crossings is not a fix.

## What it is deliberately not

Not a plausible network. The devices carry one VLAN and no addresses beyond what
`NV-W101` needs to stay quiet, because every fact in here that is not a
coordinate is a fact a reader of this fixture has to skip past. For a diagram
that is *both* arranged and realistic see [`routed`](../routed/), which is the
`home-lab` example with a hand-placed route; it happens to demonstrate the same
defect (three of its seven cables were drawn across devices) and is where the
worked example in [`docs/rendering.md`](../../../docs/rendering.md) comes from.
