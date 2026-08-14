# draw.io round trips

netgraph's pitch is *draw.io for infrastructure, with the YAML as the source of
truth*. This page is where that meets the actual tool: how to hand a diagram to
somebody who has never installed netgraph, and how to bring back what they did to
it.

The loop is two commands:

<!-- norun: the second command needs a diagram that has been out to draw.io and back -->
```console
$ netgraph export drawio -o site.drawio      # hand this out
$ netgraph import drawio site.drawio         # bring it back
```

Between them, the file is an ordinary `.drawio` document. It opens in
[app.diagrams.net](https://app.diagrams.net), in the draw.io desktop app, in the
VS Code extension and in Confluence, needs nothing installed beside it, and can
be mailed to somebody who will never read a line of YAML.

---

## Contents

- [What the exported file is](#what-the-exported-file-is)
- [What a draw.io user may safely change](#what-a-drawio-user-may-safely-change)
- [What they may not](#what-they-may-not)
- [Bringing it back](#bringing-it-back)
- [Deletions, and the one rule that keeps them safe](#deletions-and-the-one-rule-that-keeps-them-safe)
- [A diagram netgraph did not write](#a-diagram-netgraph-did-not-write)
- [The two encodings](#the-two-encodings)
- [Limits](#limits)
- [See also](#see-also)

---

## What the exported file is

`netgraph export drawio` writes one mxGraph model of one view:

* **One vertex per node**, drawn with the shipped icon for its kind, inlined as a
  data URI. Nothing is fetched when the file is opened, so it draws the same on a
  machine that has never seen netgraph.
* **One edge per link** — cable, tunnel, attachment, adjacency, power feed — in
  the same colours and line styles a rendering uses, carrying the bends
  [stored for it](schema.md#18-layout-diagram-geometry) when it has any.
* **One container frame per namespace**, so dragging a site carries its devices.
* **The stored arrangement** ([`netgraph layout`](commands/layout.md)), so the
  file opens already arranged rather than as a heap draw.io lays out afresh. A
  node nothing has placed is put on a deterministic grid and marked as such; see
  [limits](#limits).

`--view` picks the view — `l1` by default, but `l2`, `l3`, `routing`, `rack` and
the rest all export. `--icons none` draws coloured boxes instead of icons, and
`--no-frames` leaves the namespace containers out.

Every cell also carries what makes the round trip possible: a block of custom
attributes in netgraph's own XML namespace.

| Attribute | Holds |
|---|---|
| `netgraph:role` | `node`, `link`, `group` or `metadata` |
| `netgraph:node` / `netgraph:link` | the key the arrangement is stored under |
| `netgraph:name` | the fully-qualified name of the element it stands for |
| `netgraph:kind` | `switch`, `cable`, `namespace`, … |
| `netgraph:document` | the file the element is written in |
| `netgraph:hash` | a digest of the element as it was when exported |
| `netgraph:x` / `netgraph:y` | where the cell was when it left netgraph |

**Identity lives there and nowhere else.** Not in the label, not in the position,
not in the cell id. That is deliberate, and it is what makes the four gestures
below unambiguous: a cell is the same element however far it has been dragged and
whatever it now says on the canvas — so a *changed label* is free to mean
something, and what it means is a rename.

---

## What a draw.io user may safely change

Four things, and each has exactly one meaning coming back:

| On the canvas | In the inventory |
|---|---|
| **Move a cell** | a geometry write — the arrangement, and nothing else |
| **Retype a label** | `rename`, with every reference to it rewritten across the tree |
| **Delete a cell** | `delete`, cascading to the cables that cannot survive it |
| **Draw an edge between two cells** | `connect`, on the first free port at each end |

Dragging the bends of a link is a fifth, and lands as a waypoint write. Dragging
a namespace frame moves everything inside it, because the frame is a real mxGraph
container.

You can also do as much as you like that netgraph will simply ignore: add a
legend, a title block, a note, an arrow pointing at the thing you want somebody
to look at. Cells netgraph did not write are reported on import and left exactly
where they are — in the diagram, out of the inventory.

---

## What they may not

Not *forbidden* — draw.io will let anybody do anything — but these do not come
back, and it is worth saying so in the mail you attach the file to:

* **Do not delete the invisible metadata cell.** It carries the view and the
  coordinate origin. It is locked and undeletable in draw.io's own UI, so this
  takes effort; without it the file is read as a diagram netgraph did not write.
* **Do not copy and paste netgraph cells.** A copy carries the original's
  identity attributes, so two cells would claim to be one element. Draw a plain
  shape instead and it will be reported as unmapped, which is the honest outcome.
* **Do not retype a label into something that is not a
  [name](schema.md#41-name-grammar).** `core switch (2nd floor)` is not a name;
  it is reported and the element is not renamed. `core-switch-2` is.
* **Do not resize a node** and expect it to stick. The position round-trips; the
  size does not.
* **Do not expect a changed colour, font or style to mean anything.** Style is
  regenerated from the kind on every export.
* **Do not edit anything that is not on the diagram.** Interfaces, addresses,
  VLANs, routing and hardware detail are not in the file at all — there is
  nothing there to change. Those edits belong in the YAML, in
  [`netgraph edit`](commands/edit.md) or in the [web editor](commands/web.md).

---

## Bringing it back

`netgraph import drawio FILE` reconciles the file against the inventory as it is
*now* — not as it was when the file was exported — and expresses everything it
finds as [`netgraph edit`](commands/edit.md) operations. Those go through
[`netgraph plan`](commands/plan.md), so what you are shown is the ordinary
changeset, and nothing is written until you confirm it:

```text
$ netgraph import drawio site.drawio -n
site.drawio (l1 view): 1 moved, 1 renamed, 1 deleted
netgraph plan: inventory → site.drawio

  - cable.cables/wl-ap-phone  [cable]
  - device.hosts/phone  [computer]
  → device.hosts/srv-nas → device.hosts/srv-store  [server]
  ~ cable.cables/cbl-sw-nas  [cable]
      ~ spec.endpoints: [srv-nas:eth0, …] -> [srv-store:eth0, …]
  ~ layout.layout  [layout]
      ~ spec.views.l1.nodes.hosts/pc-desk.position.x: 847.0 -> 947.0

Plan: ~ 2 to change, - 2 to destroy, → 1 to rename.
```

Read that plan the way you read any other. The geometry write is in it because
an arrangement is inventory too — a `kind: layout` document, addressable and
diffable like everything else.

`-n`/`--dry-run` shows the changeset and the unified diff and writes nothing.
`--auto-approve` skips the confirmation, for a pipeline. Each of the four
gestures can be turned off on its own: `--no-geometry`, `--no-renames`,
`--no-deletions`, `--no-connections`.

**A diagram exported from an older state of the tree still imports.** Each cell
carries a digest of the element it stood for, so an element that has changed
since is *reported* — the geometry and the label are still applied, and you are
told you are applying them to a moved target. Refusing the whole import over
somebody else's unrelated commit would lose the reviewer's work.

**Re-importing an unedited file changes nothing.** That is a property netgraph's
own test suite asserts over every published example: export, import, and the plan
is empty. A move is measured against the position netgraph stamped into the cell,
so a position netgraph *invented* — because nothing had been arranged — is not
written back when it comes home untouched. Nobody's arrangement gets committed by
accident.

---

## Deletions, and the one rule that keeps them safe

A missing cell is only a deletion when the file said it held the **whole view**.

`netgraph export drawio` stamps `netgraph:scope="complete"` when nothing narrowed
the export, and `partial` when `--namespace`, `--kind`, `--vlan`, `--name` or
`--neighbors-of` did. Import a partial diagram and nothing is deleted at all,
whatever `--deletions` says; the elements that are not in it are counted and
reported instead.

The reasoning is that absence proves nothing about a diagram that was filtered
before it was drawn — and the failure mode of getting it wrong is deleting a
site. If deletions are meant to come back, export without a filter.

---

## A diagram netgraph did not write

A hand-drawn `.drawio` file carries no identity attributes, so there is nothing
to reconcile it against: every cell in it is either a new element or noise, and
netgraph cannot tell which. It is read anyway, and reported cell by cell — the
kind each one looks like, from its shape style and its label, and a note for
each one that could not be placed. Nothing is written.

That is the honest answer rather than a limitation. Inferring a `computer` from a
rectangle would put hardware in an inventory that nobody owns, and the person who
would find out is whoever trusts the inventory six months later.

To make a diagram netgraph *can* reconcile, start from `netgraph export drawio`
— on an empty inventory if you have to — and edit that.

---

## The two encodings

draw.io writes a diagram either as plain XML or as
`base64(deflate(uri-encode(xml)))` inside the `<diagram>` element. It reads both,
and so does netgraph: `netgraph import drawio` decides from what is in the file.

netgraph *writes* the plain form by default, because a diagram that is text is a
diagram that reviews, diffs and merges, which is the whole argument for keeping
the YAML as the source of truth. `--compress` writes the compact form instead —
about a fifth of the size, and what you want if the file is destined for an
attachment rather than for a repository.

---

## Limits

* **The file is a picture, not the model.** Names, kinds, links and coordinates.
  No interfaces, no addresses, no VLANs, no routing, no hardware detail. `netgraph
  export drawio --help` says so, and the export manifest on stderr says what was
  left out of each run.
* **One view per file.** Export the `l1` view and the `l3` view separately;
  importing one never touches the other's geometry.
* **Node sizes do not round-trip.** Position does; resizing a cell in draw.io
  changes what the diagram looks like there and nothing in the inventory.
* **A new edge lands on the first free port at each end.** draw.io has no way to
  say which port, so netgraph picks — the first cablable interface that nothing
  already terminates on. Where there is none, the edge is reported rather than
  forced onto an occupied port. Say which port you meant with
  [`netgraph edit connect`](commands/edit.md).
* **A new *node* is not created.** A rectangle a draw.io user drew becomes a
  note, not a device: see [above](#a-diagram-netgraph-did-not-write).
* **An unarranged inventory exports onto a grid.** It is deterministic and
  readable and it is not a layout. Run
  [`netgraph layout --write`](commands/layout.md) first and the file opens the
  way your diagrams actually look.

---

## See also

* [`netgraph export`](commands/export.md) — the command reference, and the seven
  other formats.
* [`netgraph import`](commands/import.md) — the command reference for the return
  trip.
* [`netgraph layout`](commands/layout.md) — storing the arrangement, which is
  what makes the exported file open already arranged.
* [`netgraph plan`](commands/plan.md) — the changeset an import is shown as.
* [Editing the inventory](editing.md) — the write path everything here goes
  through.
