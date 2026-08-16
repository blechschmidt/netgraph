# Diff fixtures

The *right-hand side* of the comparisons `tests/test_diff.py` draws. Each tree
is a copy of something published, with a change already made to it, so a diff
against the original is a real changeset rather than one assembled in a test.

| Tree | Copy of | What was changed |
|---|---|---|
| `home-lab-proposed/` | `examples/home-lab` | `pc-desk`'s model string; `ap-home` renamed to `ap-attic`; `srv-nas` deleted with its cable; `pc-new` and `cbl-sw-new` added |
| `arranged-proposed/` | `tests/fixtures/arranged` | `srv-nas` deleted with its cable, and `layout.yaml` left alone |

Every change was made with `netviz edit`, so the files are exactly what the
mutation layer writes — comments, key order and all.

## Why `arranged-proposed` leaves `layout.yaml` stale

Because that is the case the promise is about. A diff must draw a removed node
**where it was**, so that a deletion does not reshuffle the diagram and hide
itself in the churn it caused. Here the arrangement still names `srv-nas` and
`cbl-sw-nas`, and `netviz diff` uses it; the golden pins the coordinates.

The tree therefore validates with three `NV-W138` warnings — an arrangement
naming something the inventory no longer declares. That is not an oversight: it
is the state a tree is *in* between deleting a device and running `netviz
layout --prune`, which is exactly when someone wants to look at the diff.
