# `netgraph layout`

Store the diagram's arrangement in the inventory, so a diagram that has been
arranged stays arranged.

Everywhere else in netgraph, the picture is derived: you describe the network and
Graphviz decides where things go. That is the right default and it is what
`render` still does. But it means the diagram cannot be *edited* — drag a switch
to where it belongs and the next render puts it back, because nothing in the
tree remembers that you moved it. `netgraph layout` is what makes the
arrangement part of the model: a `kind: layout` document holding a position per
node — and, when asked, the route each link takes and the style it is drawn in —
scoped by view, loaded, validated and edited like everything else.

Once a view is arranged, `render` reproduces it exactly — the same coordinates in
the SVG, the same coordinates in the HTML, the same coordinates in the JSON
export.

[`docs/schema.md` §18](../schema.md#18-layout-diagram-geometry) is the document
format. [`docs/rendering.md`](../rendering.md#stored-arrangements) is how the
renderers honour it. This page is the reference for the command.

## Synopsis

<!-- generated: synopsis layout -->
```text
netgraph [GLOBAL OPTIONS] layout [OPTIONS]
```
<!-- /generated -->

## The four things it does

**With no flags it reports.** One row per view: how much of the drawing the
stored arrangement decides, how many nodes are placed, and how many stored keys
name nothing the diagram has.

<!-- norun: the numbers depend on which example is arranged -->
```console
$ netgraph layout --layer l1 --layer l2
layout documents: layout
VIEW  MODE     NODES  EDGES  GROUPS  STALE
----  -------  -----  -----  ------  -----
l1    fixed    8/8    0/7    0/0     -
l2    partial  6/8    0/7    0/0     1
```

`MODE` is the decision a render makes from what is stored:

| Mode | What is stored | What a render does |
|---|---|---|
| `auto` | nothing for this view | lays the graph out from scratch, exactly as it always did |
| `partial` | some of the nodes | pins those and lets the engine place the rest around them |
| `fixed` | every node | reproduces the arrangement point for point, with the layout engine placing nothing |

**`--write` seeds and completes.** On a view with no arrangement it runs the
automatic layout once and persists the result, which is what makes the diagram
editable from then on. On a view that is already arranged it places only what is
*not* yet placed — adding a switch and re-seeding must not throw away an
afternoon of arranging. `--replace` is how you ask for the whole view to be laid
out afresh.

<!-- norun: each of these writes to the inventory it is pointed at -->
```bash
netgraph layout --write                       # place what is not placed yet
netgraph layout --write --replace             # lay every node out afresh
netgraph layout --write --engine circo        # ... with a different engine
netgraph layout --write --layer l1 --layer l3 # arrange two views
netgraph layout --write --dry-run             # print the diff, write nothing
```

What is written is a **fixed point**: the coordinates stored are the coordinates
the next render produces, not the ones the seeding engine happened to report.
(The two differ — a no-op render normalises the drawing to the origin — and an
arrangement that only settled on the *second* render would be a poor thing to
promise.)

**`--prune` drops what is gone.** Deleting a switch leaves its coordinates
behind. They draw nothing, but they accumulate, and `W138` reports them until
this clears them. A prune on a clean tree writes no files.

**`--clear` goes back to automatic.** The arrangement for the selected views is
dropped; a layout document left holding nothing is removed, and so is its file if
it held nothing else.

## The display options are inputs, not decoration

`--show-ips`, `--show-vlans`, `--group-by-namespace`, `--icons`, `--rankdir` and
the rest are on this command for a reason that is easy to miss: **a label decides
how big a node is, and how big the nodes are decides where the layout puts them.**
An arrangement seeded with `--no-show-ips` and rendered with addresses on is an
arrangement of boxes that are now too small for their contents.

So seed with the options you render with. Better, put them in
[`netgraph.toml`](../configuration.md) — this command reads `[render]` and
`--profile` exactly as `render` does, which is the only way to be sure the two
cannot drift apart.

The *filter* options are deliberately absent. An arrangement covers a view; one
seeded from three of a hundred devices would leave the other ninety-seven
unplaced and the diagram permanently half-arranged.

## What it stores, and what it does not

Positions, in points, `y` upwards, a position being the centre of the node —
Graphviz's coordinate system, unchanged, because the whole point is to be able to
hand it straight back.

Group boxes too, when the render groups by namespace: the no-op layout engine
does not draw clusters, so netgraph draws them itself from the stored box, which
means the frame is where you put it rather than wherever a layout happened to
land. Its caption sits centred above the frame rather than inside it —
[`docs/follow-ups.md` §17](../follow-ups.md) explains why that is not a matter
of taste.

**`--waypoints` also stores the routes**, as the *interior* bends of each link —
the two ends of a route are always the nodes themselves, so a stored route
survives either device being dragged. It is off by default because a seeded
route is a handful of points per link that the render recomputes identically,
and that is a lot of noise for no decision; a *hand-placed* bend is a decision,
and the flag is how you get a starting point to drag one from. It brings the
**sizes** of the nodes those routes leave from with it, which is the one case
where a stored size is worth having: a route netgraph computes has to stop at
the shape it runs into, and netgraph cannot measure a label. Every other node is
left sized by its label, because Graphviz derives the same box from the same
label on every run and a stored size would go stale the moment a device grew an
interface ([`docs/follow-ups.md` §16](../follow-ups.md)).

**`--routing` records how this view's links are drawn** — `spline`,
`orthogonal` or `straight` — as `views.<view>.routing`. The same flag every
other command takes, doing both jobs at once here: the seeding run draws in that
style, and `--write` then records it, so what is stored is what was on screen.
It is a *default*: a link that pins a style of its own keeps it. To take the
setting back out, delete the one line it wrote — a generated file that has to be
hand-edited to be undone is a poor thing, but a `--routing ""` that meant
"remove" would be one keystroke away from `--routing` meaning "leave alone", and
that is worse.

A style pinned on *one link*, and where a link's label sits, are decisions a
person takes rather than ones a layout run can seed, so they are written by the
canvas editor of [`netgraph web`](web.md) instead;
[`docs/rendering.md`](../rendering.md#links-are-geometry-too) is what all three
draw as.

## Writes go through the edit layer

Every change is a [`netgraph edit`](edit.md) operation
(`set-geometry`), which means it inherits the whole write path: comments and
formatting in a hand-arranged file survive, `--dry-run` shows the exact hunk,
the tree is loaded and validated as it *would be* before anything is written,
and a file that changed on disk since it was read is refused rather than
overwritten.

It also means re-seeding an unchanged diagram writes nothing at all — a stored
`position: [240, 396]` and a computed `{x: 240, y: 396}` are recognised as the
same position, so a generated file does not churn on spelling. A position is
written on one line for the same reason: a diff of an arrangement should read as
a list of what moved.

## Arguments and flags

<!-- generated: arguments layout -->
*Takes no positional arguments.*
<!-- /generated -->

<!-- generated: options layout -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--layer` | `[physical\|l1\|l2\|l3\|overlay\|routing\|rack\|power\|identity]` | `l1` | Which view to arrange. Repeatable; each view is arranged separately. |
| `--engine` | `[dot\|neato\|fdp\|sfdp\|circo\|twopi]` | `dot` | Graphviz engine to lay the diagram out with when seeding. dot is the hierarchical layout netgraph draws with; circo suits a ring, fdp and neato a flat mesh. |
| `--write` | — | off | Run the layout once and store the result, making the arrangement editable. |
| `--clear` | — | off | Drop the stored arrangement, so the view is laid out from scratch again. |
| `--replace` | — | off | With --write, lay every node out afresh instead of keeping what is already arranged and placing only the rest. |
| `--prune` | — | off | Drop geometry for elements the inventory no longer declares. |
| `--waypoints` | — | off | Also store the bends each link is routed through, and the sizes of the nodes they leave from. Off by default: the render recomputes an equivalent route from the node positions, and a handful of points per link is a lot of noise. Turn it on to get a starting point that can then be dragged. |
| `--name` | `NAME` | `layout` | metadata.name of the layout document to write into or create. |
| `--namespace` | `PATH` | — | Folder to declare the layout document in. The inventory root by default. |
| `--file` | `PATH` | — | File to write a new layout document to, relative to the inventory root. Chosen by the layout conventions when absent. |
| `--show-ips`, `--no-show-ips` | — | `--show-ips` | Print configured IP addresses on the nodes. |
| `--show-vlans`, `--no-show-vlans` | — | `--show-vlans` | Annotate nodes and links with VLAN membership. |
| `--group-by-namespace` | — | off | Draw each namespace as a visual group. |
| `--icons` | `THEME\|DIR` | — | Draw each element as an icon instead of a plain shape. Built in: cisco, none. A directory of images named after element kinds (router.svg, switch.png, ...) also works. Graphviz formats only. |
| `--tooltips`, `--no-tooltips` | — | `--tooltips` | Carry the full detail of each element — interfaces, addresses, VLANs, cabling — as hover text. Reaches a reader in svg output; png and pdf have nowhere to put it. |
| `--link-template` | `URL` | — | Link each element back to the YAML that declares it, e.g. 'https://git.example.com/net/blob/main/{file}#L{line}'. Placeholders: {file}, {line}, {name}, {namespace}, {kind}. dot and svg only. |
| `--element-ids` | — | off | Give every node, edge and namespace a stable id derived from its name, so the diagram can be deep-linked and styled from outside. dot and svg only. |
| `--max-addresses` | `N` | `4` | Longest address list spelled out under a node before it is abbreviated to 'and N more'. 0 prints the count alone. |
| `--rankdir` | `[tb\|lr\|bt\|rl]` | TB, top to bottom | Layout direction. A wide network reads better left to right; a deep one top to bottom. Honoured by the Graphviz backends and by mermaid. |
| `--routing` | `[spline\|orthogonal\|straight]` | whatever the inventory's layout documents say, else spline | How links are drawn between the bends they are pinned through: 'spline' is the curve Graphviz draws, 'orthogonal' right angles, 'straight' segment to segment. A default: a link that pins a style of its own keeps it. Honoured by the Graphviz backends, the JSON export and the editor. 'netgraph layout --write' records it in the view it arranges, so the choice is the inventory's rather than the command line's from then on. |
| `--title` | `TEXT` | — | Caption for the diagram. |
| `--profile` | `NAME` | — | Apply the [profile.NAME] block of netgraph.toml on top of its [render] table. Explicit flags still win over both. |
| `--show-config` | — | off | Print the settings this invocation resolves to, and where each one came from, then exit without doing any work. |
| `-n`, `--dry-run` | — | off | Write nothing; print the unified diff the edit would apply. |
| `--json` | — | off | Print the applied operations and their inverses as JSON, so a caller can keep an undo stack. |
| `--force` | — | off | Write even when the edit would introduce a new error. The check for files that changed on disk is never skipped. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The arrangement was reported, or written. |
| `1` | The inventory has errors, or the edit would introduce one, or a file changed on disk. |
| `2` | Usage error — `--write` and `--clear` together, `--replace` without `--write`. |
| `3` | The inventory could not be read. |
| `5` | Graphviz is not installed, or the layout failed. |

## See also

* [`docs/schema.md` §18](../schema.md#18-layout-diagram-geometry) — the
  `kind: layout` document, field by field.
* [`docs/rendering.md`](../rendering.md#stored-arrangements) — how `svg`, `html`
  and `json` honour an arrangement, and what each publishes;
  [Links are geometry too](../rendering.md#links-are-geometry-too) is the same
  for bends, routing styles and label positions.
* [`netgraph edit`](edit.md) — the write path this command goes through, and its
  two gates.
* [`docs/follow-ups.md` §16](../follow-ups.md) — why the geometry is a sidecar
  and not a field on each element.
* [`W138`](../validation-rules.md#w138--stale-diagram-geometry) — the warning
  `--prune` clears.
