# `drawio`

The fixtures for the draw.io round trip (`tests/test_drawio.py`,
[`docs/drawio.md`](../../../docs/drawio.md)).

## `inventory/`

A five-device tree, arranged, deliberately small enough that the exported
diagram can be read in full and deliberately shaped so that each of the four
gestures a draw.io user can perform has somewhere to land:

* `hosts/pc-1` is uncabled, and `devices/sw-access` keeps `port3` and `port4`
  spare, so **an edge drawn in draw.io has a free port at each end** to become a
  cable on. The `I002` infos that reports are the point of the fixture, not a
  defect in it.
* `hosts/pc-2` exists only to be **deleted** by the edited diagram.
* `hosts/srv-app` is **renamed** by it, and `cables/cbl-access-app` terminates
  on it, so the rename has a reference elsewhere in the tree to rewrite.
* `layout.yaml` places all five, so the export reproduces a *stored*
  arrangement rather than inventing one — which is the case that matters, since
  an invented position is deliberately not written back.

Regenerate the arrangement with:

```bash
netviz -i tests/fixtures/drawio/inventory layout --write --layer l1
```

## The diagrams

All three are written by `python tools/gen_drawio_fixtures.py`:

| File | What it is |
|---|---|
| `arranged-l1.drawio` | What `netviz export drawio` produces from `inventory/`. A golden: a byte-for-byte diff of it is the review of a change to the emitter. |
| `arranged-l1-compressed.drawio` | The same model in the deflate+base64 encoding draw.io's desktop app writes by default, so the reader is exercised against both. |
| `arranged-edited.drawio` | `arranged-l1.drawio` with the four gestures applied by hand: `pc-1` moved 120 points, `srv-app` relabelled `srv-web`, `pc-2`'s cell deleted, and an edge drawn from `pc-1` to `sw-access`. |

The edits are applied by the generator as **text substitutions** rather than
being committed as an opaque blob, so `git diff` between the two diagrams is
reviewable and the fixture cannot quietly stop testing what it says it tests.

## `arranged-edited.plan.json`

The changeset `arranged-edited.drawio` must produce, committed so that a change
in what an edit *means* is a diff somebody has to approve. Regenerate it with:

```bash
pytest tests/test_drawio.py --regen-golden
```

The state digest is elided from it: it names the temporary directory the test
copies the inventory into, and what it asserts — that a plan is only applied to
the tree it was made from — is `netviz apply`'s to test.
