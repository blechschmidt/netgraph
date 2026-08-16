# Changelog

All notable changes to netgraph are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), with the `0.x` caveats spelled
out in [`docs/releasing.md`](docs/releasing.md).

What belongs in an entry is what a *user* would notice: a flag, a schema field, a rule, an
output format, an exit code, a diagram that comes out different. Refactors, test additions
and internal performance work are only listed when they change one of those. The rest is in
`git log`.

Every release is cut from the section named after it, and the release workflow refuses to
publish a version whose section is missing or empty — see
[`tools/release.py`](tools/release.py).

## [Unreleased]

### Added

- **Policy-based routing: `spec.route_tables`, `spec.routes[].table` and `spec.routing_policy`
  (§16.2, §16.4).** Everywhere else in §16 a device answers one question about a packet —
  which route in *the* table matches its destination. That is right for most boxes and wrong
  for every edge router anyone has actually built: a guest VLAN out the cheap uplink, a
  marked flow down the tunnel, a lab prefix that must not reach management. Each of those is
  a decision about which *table* to route by, made before the destination is looked at, and
  the schema had nowhere to put it.

  `spec.route_tables[]` declares a table — a name and a number — and `spec.routes[].table`
  places a route in one. The three tables every stack is born with (`main`, `local`,
  `default`) are nameable without being declared and cannot be declared, by either name or
  number (`NG-F015`). A VRF is a routing table too, so `table:` resolves against `spec.vrfs`
  as readily (`NG-F019`) — but `vrf` and `table` on one route are a contradiction rather than
  a refinement (`NG-F018`).

  `spec.routing_policy[]` is the database itself: an ordered list of rules, walked from the
  lowest `priority` upwards, first match deciding — the shape RFC 1812 §5.2.4.3 describes and
  every implementation implements. A rule selects on `src`, `dst`, `fwmark`, `iif`, `oif` and
  `dscp`, optionally inverted, and does one of five things: `lookup` a table, `blackhole`,
  `unreachable`, `prohibit`, or `goto` another priority. `priority` is the rule's identity as
  well as its position, so it is unique per device per family (`NG-F020`); a rule that states
  no `family` and no prefix is installed in both, which is what typing `ip rule` and
  `ip -6 rule` does by hand.

  **Layer 4 is deliberately not a selector.** No `sport`, no `dport`, no protocol: `ip rule`
  grew them late and nothing else agrees on them. The portable way to route by port, by user
  or by application is to mark the packet where marking belongs and match `fwmark` here.

  Three rules report the ways a database goes wrong without going invalid. `W147` — a rule
  looks up a declared table no route is placed in, so the diverted traffic falls through and
  goes exactly where it would have gone anyway. `W148` — a declared table no rule looks up,
  so its routes are never consulted. `W149` — a rule numbered above the catch-all that
  terminates its family's database, so it can never run.

- **The routing view, the routes script and networkd carry it.** `--layer routing` labels a
  router with its rule count and lists its tables, its routes and its database — in *priority*
  order, which is the order the device walks it — on the tooltip and in `-f json`.
  `netgraph export routes` writes the rules beside the routes they select, routes first so no
  rule ever diverts traffic into a table that is still empty; each rule is a `del` of its
  priority followed by an `add`, which is how `ip rule` is made idempotent. A table is written
  by **number** with its name in a trailing comment, since a name resolves only through
  `/etc/iproute2/rt_tables` and this script does not edit that file. `netgraph export networkd`
  writes the database as `[RoutingPolicyRule]` sections; `netgraph export interfaces` projects
  it as `route-table` and `policy` stanzas; frr, netplan and ifupdown name it in their
  manifests with the emitter that does write it.

- **A machine is no longer one network stack: `spec.netns`, `interfaces[].netns` and veth
  pairs (§23).** Everywhere else in the schema a host has one set of interface names, one
  address space and one routing table, and one box on a diagram holds all of it. That is
  exactly right for a switch and a laptop, and it stops being right the moment the host is a
  container host — a server running twelve containers has twelve of each, and an inventory
  that records one of them has recorded one twelfth of the truth.

  `spec.netns[]` declares the namespaces a machine runs; `interfaces[].netns` puts an
  interface in one; an interface that names none is in the machine's **initial** namespace,
  which no document declares because every machine has it. `parent` nests one namespace
  inside another, **to any depth** — a namespace is created from inside exactly one other, so
  the nesting is a tree and the chain always ends at the initial namespace.

  **A namespace is not a VRF and the two compose.** A VRF partitions the routing table of one
  stack; a namespace *is* a second stack, so it partitions the interface names, the
  addresses, the sockets and the routes at once. An interface may name both.

- **A veth pair is two `ethernet` interfaces naming each other, and deliberately not a new
  interface type.** A veth end is `ianaift:ethernetCsmacd` in every respect the rest of the
  schema cares about — it has a MAC, it carries 802.3 frames, it can be a bridge port, it can
  carry a VLAN sub-interface — so a type of its own would mean restating §6.2 for a port that
  behaves identically. What it does not have is a socket, and `interfaces[].peer` is what says
  so: it names the other end, and the other end has to name it back (`NG-N023`), because a
  veth pair is created as a pair and destroyed as a pair and a document describing half of one
  describes something the kernel cannot be asked for.

  Four rules follow. `E049` refuses a cable on a veth end — `E012` cannot catch it, since by
  *type* a veth end is exactly a cabled port. `E050` refuses a bridge or lag that aggregates a
  member in another namespace: one datapath belongs to one stack, and moving a port into a
  namespace is precisely the operation that takes it out of the aggregate. `W146` reports a
  declared namespace nothing is in. `I005` reports a pair with both ends in one namespace —
  legal, and usually a `netns` written on one end and forgotten on the other. `I002` stops
  reporting veth ends as spare ports, which it would otherwise do to every one of them.

- **`--layer netns`, the one view that draws below the machine.** The element node stays and
  stands for the initial namespace — it keeps its kind, its icon, its link to the document and
  its place in a stored arrangement, because it is still the machine — and each declared
  namespace becomes a rounded box beside it, every box of one machine framed together. Solid
  cyan lines are veth pairs: the crossing itself, which no other layer can draw because at
  layer 1 both ends are inside one box. Dotted lines are nesting. Cables are kept and
  re-pointed at the namespace holding the interface they land on, which answers the question
  the view exists for — how does the stack inside this container reach the wire? A machine
  with one stack is drawn only as context for one that has more.

- **`netgraph export interfaces` carries all of it, and the four dialects that cannot
  refuse.** The neutral dialect grows a `netns` stanza and two interface attributes, so a
  machine's namespaces and pairs survive a round trip through `netgraph import` and are
  compared by `netgraph drift`. `netplan`, `networkd`, `ifupdown` and `frr` **write nothing**
  for a device that declares either: those files configure the stack they are applied *in*,
  and a netplan file listing a container's interface puts the container's address on the host.
  The refusal names every field and points at `export interfaces`.

- **`examples/containers/`**, the sixth example inventory: two container hosts running five
  namespaces, one nested inside another, joined by four veth pairs — one set bridged onto a
  host bridge, one set routed over a `/30`. It validates clean, like the other five.

- **A delete now takes everything that cannot outlive it — and the editor stops asking for
  a flag it was always going to be given.** "Cascade" used to mean the cables. It now means
  the whole of what a deleted element leaves behind, in three layers:

  * **Elements**, as before: a link dies with either end, a tunnel with the tunnel it runs
    over, transitively.
  * **Annotations (§21)**, by §21's own coherence rules rather than a table. A note anchored
    to the deleted element *and* placed keeps its text and loses only its anchor; a note that
    is only anchored cannot be drawn without it, so it is a dependent — named in the refusal,
    removed by `--cascade`. An area drops the doomed members and goes only if that would leave
    it with no members, no selector and no rectangle. In one line: **an annotation is removed
    exactly when clearing its references would leave a document the loader refuses.**
  * **Geometry (§18)**, which is never a dependency and is therefore never asked about.
    Deleting one switch out of an arranged home lab used to hand back a tree carrying **eight
    new `W138` warnings** — the switch's position in two views, the waypoints of each of its
    five cables, and the box round the namespace it emptied — and told you to run `netgraph
    layout --prune`. It now hands back the tree it found, minus the switch. This is
    deliberately not `--prune`, which drops every key the current *drawing* lacks and would
    throw away the position of a device merely filtered out of the view.

  `netgraph edit disconnect` grows `--cascade` for the same reason a delete has one, and the
  waypoints of a dropped cable go either way. Two bugs found on the way are under **Fixed**.

- **The editor cascades, and says what that costs before it does it.** On the command line a
  cabled switch is refused and you are told to pass `--cascade`. On a canvas that is theatre:
  somebody who dragged a box to the bin has said what they want. So `Delete` always cascades,
  and what it owes you instead is the truth — once, before the fact, and **not read off the
  picture**.

  The new read-only `GET /api/cascade` asks `netgraph.edit` for the set it will actually
  remove, so the confirmation names what a diagram cannot show you: the tunnel three levels up
  that runs over a cable that runs to the switch, the note anchored to it in a view you are not
  looking at, the group that lists it as a member, and the layout entries that placed all of
  it — each with the reason it goes. The multi-select confirmation used to guess this from the
  drawn edges, which got the cables right and everything else wrong.

  **A delete that takes nothing but what you named does not ask at all**, because a
  confirmation that always appears is one that stops being read. And the whole of it is one
  entry in the undo stack: elements, annotations and geometry come back together, byte for
  byte, on one `Ctrl-Z`.

- **`netgraph review`, and a pull-request bot built on it.** A green check answers "does this
  branch validate?" A reviewer wants "what does this change do, and what did it break that was
  not already broken?" — and neither half of that is in a check mark. `netgraph review --from
  origin/main` writes both, as one Markdown document: a verdict line, a table of what is added,
  changed, renamed and removed grouped by element kind, the findings the change *introduced*,
  and the change drawn.

  **Only new problems fail it.** The head's diagnostics are measured against the base's by the
  same fingerprint `-F sarif` puts on each result — the rule, the file, the element, the
  pointer and the message, deliberately not the line — so a repository carrying a legacy
  warning adopts the check green and the next warning turns it red. `--strict` and `--disable`
  apply to *both* sides, so a rule silenced in the change itself reads as nothing changing
  rather than as a wave of fixes.

  Three side documents come out of the same load and the same validation, so nothing downstream
  can disagree with the comment: `--plan-out` (the changeset `netgraph plan --json` writes),
  `--sarif-out` (for a code-scanning upload) and `--summary-out` (the verdict and the counts,
  for a workflow step that gates on them without parsing prose).

  A base that has no inventory at all, and a base that does not load, are both reviewed rather
  than refused: the first pull request a repository ever sees, and a `main` that is already
  broken, are exactly the cases a bot has to survive to be adopted.

- **`.github/actions/netgraph-review` and `.github/workflows/netgraph-review.yml`.** The action
  draws the diff to SVG and PNG, produces the changeset and the SARIF, and writes the comment
  body. The reusable workflow posts it as **one sticky comment**, edited in place on every push
  rather than added to, uploads the bundle as an artifact and the findings to code scanning,
  and fails only on an error the base did not have. `docs/ci.md` has the input tables, the
  shape of the comment, and why the workflow uses `pull_request` and never
  `pull_request_target` — on a fork's pull request it degrades to the job summary rather than
  asking for a token that cannot safely exist.

  The diagram appears three ways because a GitHub comment is sanitised and neither an inline
  `<svg>` nor a `data:` image survives it: a Mermaid summary of the changeset that always
  renders, an `<img>` for anything the caller published to a URL, and a link to the uploaded
  artifact.

  This repository reviews its own `examples/` with it — `.github/workflows/review.yml`.

- **`netgraph converge plan`: drift, joined to the configuration emitters, as an ordered
  per-device remediation.** `netgraph drift` said how the live network differs from the
  inventory and `netgraph export config` said what a device would run if it agreed; nothing
  joined them. This does. It takes the same captures `drift` takes and produces, per device,
  the minimal ordered set of changes that would move it from what the capture found to what
  the inventory declares — each one carrying the drift finding that asked for it, a `safe`
  or `disruptive` classification, its prerequisites, the commands that perform it and the
  commands that undo it.

  **netgraph never applies any of it, and there is no flag that adds a transport.** The
  command reads capture files and writes a plan and per-device `.txt` scripts; a person runs
  them. The security surface stays "reads files, writes files". The plan type is shaped so a
  transport *could* consume it later — every change has a stable id, prerequisites, a risk
  and an inverse — but that would be a separate program.

  A plan touching the path a device is managed on, or shutting or deleting any interface, is
  **refused whole** unless `--allow-disruptive`, and the refusal names every offending change
  rather than the first. The management interface is the one `netgraph export` already picks
  for `ansible_host` and a scrape target, plus everything it is stacked on and the VLAN it
  lives in.

  Changes come out in a dependency order — VLANs before the ports that carry them, parents
  before what is stacked on them, addresses before routing, every addition before every
  removal and removals in the mirror order — and are grouped into maintenance batches by the
  existing `netgraph impact` engine, so two devices share a window only when neither is in
  the other's blast radius.

  `--dialect` picks `interfaces` (netgraph's own imperative grammar, one line per change, and
  the default) or one of `netplan`, `networkd`, `ifupdown`, `frr` and `wireguard`, whose
  remediation is genuinely the generated file plus a reload — computed by running the
  existing emitters over both the declared and the observed state and keeping only the files
  that differ. `--format text|json|markdown`, `-o DIR` for the scripts, `--rollback` for the
  inverse ones, and an exit-code contract mirroring `netgraph plan`: **0** converged, **2**
  changes pending, **4** refused. See
  [`docs/commands/converge.md`](docs/commands/converge.md).

- **Namespaces are containers you can drag things into.** The editor draws a frame per
  namespace level whenever the diagram is grouped by namespace, captioned with the namespace
  and how many elements are under it, with a triangle that folds it into the single node
  `netgraph render --collapse` would draw. Dropping an element — or a multi-selection, or a
  whole container — inside a frame runs `netgraph edit move`: the document is rewritten into
  that directory, and every cable, tunnel, group, layout and annotation that referred to it
  is re-spelled. Dropping on empty canvas moves it to the root.

  A drop is refused **before** anything is written, naming both sides: a name already taken
  in the target namespace, two dragged documents that would collide with each other, or a
  folder the loader would skip. The new `POST /api/reparent` and
  `netgraph.edit.containers.move_plan` are the one place that decides, so the browser's drop
  and the command line's move cannot diverge — including which *file* the document lands in,
  which stays the placement convention's answer.

  Containers can also be resized, and the rectangle is stored in the `groups` section of a
  `kind: layout` document keyed by namespace — the first thing that writes one from the
  canvas. Handles are only offered on an arranged diagram and only for a namespace Graphviz
  boxes, because anywhere else the engine sizes the cluster on every run and a written box
  would be a number nothing reads.

  Right-clicking a frame offers the container's own rows: fold it, create an element in it,
  paste into it — a paste being a drop, so the copies land in that namespace.
  **New namespace…** in the canvas and container menus makes a folder by putting the first
  document in it; there is deliberately no operation that makes an empty one, because a
  folder netgraph reads is one holding a document. `f` folds the container under the pointer
  or the one holding the focused element. See
  [`docs/editing.md`](docs/editing.md#containers-dragging-a-document-into-a-namespace) and
  [`docs/inventory-layout.md`](docs/inventory-layout.md#rearranging-the-tree-from-the-diagram).

- **A clipboard, in the editor and on the command line.** `Ctrl-C`, `Ctrl-X`, `Ctrl-V` and
  `Ctrl-D` over the existing multi-selection, and `netgraph edit copy` /
  `netgraph edit duplicate` for the same thing without a browser. On this canvas they do not
  move shapes about, they write documents — so a copy is three decisions rather than a
  memcpy, and all three are made in `netgraph.edit.clipboard` where the browser, the command
  line and a script get the same answer.

  The **name** is deduplicated in a series (`sw1` → `sw1-copy` → `sw1-copy-2`, and a copy of
  a copy re-joins the series rather than nesting it; `--suffix` and `--name` override it).
  The **fields two elements in one inventory cannot both have** are dropped — MAC addresses,
  fixed IP addresses, serials, BSSIDs, PDU outlets, router ids, a rack unit; the table is in
  [`docs/editing.md`](docs/editing.md#the-fields-a-copy-cannot-keep) and `--keep-unique`
  turns it off. The **links** follow the set: a cable with both ends in the selection is
  cloned and rewired to the clones, one with a single end in it is left behind and *named*,
  and copying a cable on its own is refused rather than silently landing a second cable on a
  port that has one. Copying a namespace copies its subtree. Everything else — the vendor,
  the VLAN database, the comments somebody wrote beside them — comes across verbatim,
  because the copy starts as the original document's own text.

  Pasted elements are placed in the current view's stored geometry, offset from the
  originals or centred on the point a right-click named, so a paste lands where it was
  asked for rather than being re-laid-out. `Ctrl-C` also puts a **serialised fragment** —
  JSON holding the copied documents, their namespaces and their positions — on the system
  clipboard, so a piece of one inventory can be pasted into another window, another
  inventory, or a text editor.

- **An action that draws the inventory, and a reusable workflow that publishes it.** A
  pipeline could already gate a pull request on the inventory validating; what it could not
  do was show anybody the network.
  [`.github/actions/netgraph-render`](.github/actions/netgraph-render/) installs netgraph
  *and* Graphviz — which is not a Python dependency, and whose absence is otherwise
  discovered at the last step of a job that has already done all the work — runs
  `netgraph render`, and reports where the diagram landed. It defaults to `format: html`,
  the one self-contained format: layer switcher, search box and every element's detail in a
  single file with nothing to fetch, and so publishable as it stands. `layer: l1 l2 l3`
  becomes three views behind one switcher, `args` carries anything the action does not name
  an input for, and `graphviz: auto` skips the install for the formats netgraph writes
  itself. A file that is not the format it was asked for fails the step rather than the
  site.

  [`.github/workflows/netgraph-pages.yml`](.github/workflows/netgraph-pages.yml) is that
  render plus a deployment: an inventory repository that calls it gets a live diagram of its
  own network at a URL, rebuilt from the YAML on every push, instead of the export somebody
  drew in draw.io eighteen months ago. **`runs-on` is an input** — a single label, a JSON
  array of labels, or a runner group — because the network worth drawing is often the one
  where a GitHub-hosted runner is not allowed near the repository; `python-version: ""` and
  `graphviz: false` are there for the same self-hosted image. Only the deploy job holds
  `pages: write`, so a render that fails cannot have reached the Pages API, and
  `deploy: false` gives a pull request the render as a gate without replacing the published
  site. See [`docs/ci.md`](docs/ci.md#the-render-action).

- **Per-element styling, with a theme layer and an editor style panel.** Every diagram
  looked identical, because appearance was entirely implicit. Now it is inventory data:
  an optional `style` block ([§22](docs/schema.md#22-per-element-styling-and-themes)) on
  every drawable kind and on cables and tunnels, carrying `fill`, `stroke`, `strokeWidth`,
  `dash`, `fontColor`, `fontSize`, `shape`, `icon` and `opacity`. The vocabulary is closed
  — a hex literal or one of twenty-six named colours, and a small enum or a bounded number
  for everything else — because these values end up inside Graphviz attributes and mxGraph
  style strings, and a free-form pass-through would be an injection. A typo is answered
  with the nearest legal spelling (`'navvy' is not a colour … did you mean 'navy'?`),
  under `NG-Z001`.

  **Themes.** A `kind: theme` document maps selectors — by kind, name, namespace glob,
  role or label — onto style blocks. `netgraph render --theme NAME|PATH` applies one; two
  ship (`blueprint`, `mono`); `[render] theme` in `netgraph.toml` sets the default and a
  `[theme]` table declares an inventory's own rules inline. Precedence is documented and
  resolved field by field: an element's own `style`, then the theme's rules (most clauses
  first, a tie broken by the later declaration), then the icon set, then the built-in
  palette — so a theme that sets a fill does not take a shape away.

  **Everywhere it is drawn.** The Graphviz backends and the draw.io export honour all nine
  fields; a colour chosen in a manifest opens in draw.io as that colour. `-f json` publishes
  the resolved style beside each node and edge with a `from` map naming which layer chose
  each value. Mermaid restates the palette as `classDef` rules and has nowhere to put a
  per-element one, so it ignores them.

  **In the editor.** <kbd>Ctrl-Shift-Y</kbd> — or **Change how it looks…** in the context
  menu — opens a docked inspector showing the resolved style of the selection, which layer
  each value came from, and the controls to change it. A change is a batched `Set` on
  `spec.style.*` over the whole selection: one entry in the changes drawer, one
  <kbd>Ctrl-Z</kbd>. **Reset to theme** *unsets* the field rather than writing the
  inherited value, because writing it would pin today's theme into the document and break
  the inheritance the button is named after.

  `--no-style` renders the plain diagram from the built-in palette alone — the answer to
  "is this odd because of the network or because of the stylesheet?" — and `W144`/`W145`
  catch a style that defeats itself: an element faded to nothing, a label the colour of the
  box behind it. See [`docs/styling.md`](docs/styling.md).

- **Annotations in the editor.** The notes, areas and legends of
  [§21](docs/schema.md#21-diagram-annotations-notes-areas-and-legends) could be written in
  YAML and drawn by every backend, and were the one part of an inventory the canvas could
  not touch. Now: <kbd>Shift-N</kbd> — or **New note** in the canvas menu — drops a note
  where the pointer is and opens it for typing; right-clicking an element or a link and
  choosing **Note about it…** anchors the note to that instead, so it follows the device
  when the diagram is laid out again. A note is retyped by double-clicking it
  (<kbd>Ctrl-Enter</kbd> writes, <kbd>Esc</kbd> abandons), moved by dragging it and resized
  by its corner; a zone pinned to a rectangle is dragged by its outline and resized by its
  corners; <kbd>Delete</kbd> removes whichever is selected. A zone drawn round its
  *members* has no box to move, so dragging one is refused with that sentence rather than
  quietly turned into a rectangle.

  Every gesture is one `create-annotation`, `set-annotation` or `delete-annotation` batch
  through the same write path a rename takes — one entry in the changes drawer, one
  <kbd>Ctrl-Z</kbd>, comments and key order intact. An annotation that has never been
  placed gets its whole `spec.geometry` block in a single write, because an `x` with no `y`
  is a position that places nothing.

  <kbd>Alt-N</kbd>, and a **notes** checkbox beside **IPs** and **VLANs**, hides the lot —
  a per-view switch that writes nothing, because commentary is never topology. The
  `/api/graph` answer carries an `annotations` payload beside its `geometry`, in the form
  `netgraph render -f json` publishes, and `&annotations=0` leaves it out.

- **Multi-select, bulk edit and alignment in the editor.** The canvas could only ever act
  on one focused element, which was the largest remaining gap against draw.io. It now has
  a real selection: drag on the paper for a rubber band, <kbd>Shift</kbd>- or
  <kbd>Ctrl</kbd>-click to add or take away, <kbd>Ctrl-A</kbd> for the whole view,
  <kbd>Shift</kbd>-arrow to extend along the links, <kbd>Esc</kbd> to clear.

  The selection is a set of **element addresses**, not of shapes, which is what lets it
  survive a redraw and lets an element culled off screen stay in it on a thousand-device
  inventory. It is drawn as a halo over the drawing rather than as a class on each shape —
  a culled element has no shape left to mark — and it is mirrored into the accessible
  outline, where a screen reader hears the count and each selected entry as pressed.

  With more than one thing selected, **Delete asks once**, listing what goes *and* the
  cables that will dangle as a result, and writes the lot as one entry in the undo stack;
  **Set a field**, **Remove a field** and **Move to another file** apply to every selected
  element in one batch. Behind them is `netgraph.edit.Batch`: N typed operations across N
  documents as a single transaction — all-or-nothing, one conflict check, one save, one
  inverse. A batch whose seventh operation is refused leaves the tree byte-identical to
  the one it started on.

- **Align, distribute and snap-to-grid.** Nine commands that mean nothing about a single
  shape: `align.left/centre/right/top/middle/bottom`, `distribute.horizontal/vertical` and
  `geometry.snap`, from the palette or by right-clicking inside a selection. Each is
  computed by `netgraph.edit.arrange` against the tree's `kind: layout` documents and
  emitted as one `set-geometry` per document that loses an entry — so a whole alignment is
  one reviewable YAML diff and one <kbd>Ctrl-Z</kbd>, and an entry that did not move comes
  out byte-identical. The grid pitch is the inventory's, in `netgraph.toml`:

  ```toml
  [editor]
  grid = 20     # points; the default
  ```

- **A context menu in the editor.** Right-clicking the diagram now offers the handful of
  commands that make sense where you clicked: on an element, on a link, and on the paper
  between them, where `New ▸` lists every element kind and creating one is two clicks and
  a name. Until now the only way to reach a command was `Ctrl-K`, which you had to know
  about first.

  It is a *view* of the existing commands, not a second set of them. Every row runs a
  binding from `netgraph.web.bindings` under that binding's own id, and the same table now
  declares which rows each target offers — so a menu cannot offer a gesture the keyboard
  does not have, and `docs/commands/web.md` generates the menus beside the shortcuts.
  Every row prints its own chord, the way a palette row does.

  What the pointer adds is a target: right-clicking a shape **focuses it first**, so
  `Delete it` deletes the one under the cursor rather than the one the keyboard was left
  on, and the menu's heading is that element's address so there is no doubt which. A row
  that cannot run is greyed with the reason rather than hidden, <kbd>Shift-F10</kbd> and
  the menu key open it from the keyboard, the arrow keys walk it and <kbd>Esc</kbd> leaves
  it. Right-clicking a *bend* still removes that bend and shows no menu.

- **A published demo site: <https://blechschmidt.github.io/netgraph/>.** Until now nothing
  in the project let a stranger see what netgraph does without first installing Python
  *and* Graphviz. The site is the whole documentation set as browsable pages, and — the
  part that matters — every example inventory rendered by `netgraph render -f html`, so
  the layers, the filters and the per-element detail are clickable at
  <https://blechschmidt.github.io/netgraph/demo/>.

  Those pages are the command's own output rather than a viewer written to look like it,
  which is what keeps them honest: there is no second front end to fall behind the first.
  `.github/workflows/pages.yml` rebuilds and deploys on every push to `main`, runs on pull
  requests without deploying, and **fails if any example fails to render** — a demo site
  quietly missing the inventory that stopped working would be a regression nobody sees.
  The builder is `tools/build_site.py`, one command to run locally, and its one dependency
  is the new `site` extra.

  `docs/getting-started.md` now opens with *Try it without installing*, and the README
  carries a badge pointing at the demos.

- **A first-run guided tour in the editor.** `netgraph web DIR` opens a canvas and a
  command palette with four dozen entries, and nothing on screen said which of them was
  the point. The tour says it in about sixty seconds: it creates a device, cables it to
  one of yours, moves its document into another file, opens the changes drawer on the YAML
  all three gestures wrote, and undoes the lot — proving that every shape on the canvas is
  a document and that the mapping runs both ways.

  Every step is a real batch through `netgraph.edit`, because a tour that mimed its writes
  would demonstrate the one thing it exists to demonstrate least well. What makes that
  safe is where the writes land: starting the tour copies the inventory's documents into a
  temporary directory and points the page at a second, always-writable session over the
  copy, so **your files are never touched** — and a read-only session can take the tour
  too, which is the session somebody exploring is most likely to have open. The copy is
  deleted when the tour finishes, when it is skipped, when the tab closes and when the
  server stops.

  It is offered once, on a first visit; `Esc` declines it for good and `Ctrl-K` → *Take the
  guided tour* runs it again. Keyboard-driven throughout, audited by axe-core like every
  other dialog on that page, and covered end to end in `tests/test_browser.py`.

- **`netgraph export` now writes the configuration a device would actually run.** Six new
  formats — `netplan`, `networkd`, `ifupdown`, `frr`, `wireguard` and `interfaces` —
  generate `etc/netplan/10-netgraph.yaml`, a `.network`/`.netdev` pair per stacked link,
  `etc/network/interfaces`, `etc/frr/frr.conf`, a wg-quick `.conf` per tunnel, and
  netgraph's own vendor-neutral grammar for every device the other five have nothing to
  say about.

  Everything netgraph exported until now was *about* the network — a hosts file, a zone,
  a pull list, a monitoring target. None of them is the network. The configuration a
  device runs is, and until this existed the inventory was a document beside the truth
  rather than the source of it: somebody still typed the addresses into the box, and the
  typing is where the two started to disagree.

  Nothing is invented. A value the inventory does not state is not in the output; where a
  dialect *requires* one netgraph deliberately does not hold — a WireGuard private key, a
  wifi passphrase — an un-runnable `REPLACE-ME` is written instead, because an inventory
  holding key material would be a secret in version control (`docs/schema.md` §14.2).
  Three values are derived rather than read, and each is a derivation from stated facts
  rather than a guess: a peer's WireGuard `Endpoint`, from the underlay port its tunnel
  interface names and the address that port declares; a static route's interface, from the
  port whose prefix covers the next hop, which `E032` already requires to be on-link; and
  whether a `vlan` block is a filter on a port or a description of a broadcast domain.

  A field a dialect cannot express is a **refusal**, not a silent omission: the whole run
  fails with exit code `4`, every refusal names the field as the document spells it
  (`spec.interfaces[2].vlan`), and nothing is written — a configuration missing one field
  is a device that is almost what the inventory says, with nothing in the file to say
  which part. A field merely outside a dialect's remit is a manifest **skip** naming the
  dialect that does cover it, and the file is still written.

  `--out DIR` writes the tree: one directory per device, named after its fully-qualified
  name, each file at the path the device keeps it at. A file netgraph generated is
  overwritten; one it did not is refused until `--force`; nothing is ever deleted, and
  stale files from an earlier run are reported. Without `--out`, a single device goes to
  stdout, so `netgraph export netplan --name pc-desk | ssh pc-desk 'cat >…'` works and a
  wider selection is a usage error rather than a stream nobody can split.

  Every generated file carries `netgraph-dialect`, `netgraph-element` and one
  `netgraph-source` per inventory document behind it. `netgraph drift` and
  `netgraph import` read those keys and the same six dialects back, so
  generate-then-compare needs neither `--from` nor `--host` and the round trip is exact.
  `frr` and `wireguard` describe part of a device rather than all of it and are therefore
  additive-only in a drift report; the other four are whole-device inputs. `docs/export.md`
  has the full treatment and `docs/commands/drift.md` the capability table.

- **`netgraph test`: the inventory can now be tested the way code is.** A new
  `kind: testsuite` document (`docs/schema.md` §20) holds named assertions about the
  network, and the command grades them and exits non-zero when one has stopped being true.

  `netgraph validate` answers "do these files cohere?" — a cable endpoint resolves, an
  address is inside its subnet. Every rule it applies is a statement about inventories *in
  general*, which is exactly why none of them can say that the ward switch must not be the
  only path to the ward. That is a fact about *this* network, known only to the people who
  built it, and until it is written down it survives only as long as the person who
  remembers it.

  Eleven assertions: `reachable` / `not-reachable` between two endpoints on a named layer,
  `path-shorter-than` a hop count, `same-vlan` / `distinct-vlan`, `within-prefix`,
  `has-interface`, `port-count-at-least`, `unique` over a field expression, `count`
  comparisons, and `no-single-point-of-failure`. All of them run over the graphs
  `netgraph render` draws and the search `netgraph path` runs, so a failing test and a
  drawn diagram cannot disagree about what is connected to what.

  Selectors reuse the filter vocabulary `netgraph render` already parses — `select:
  kind=switch, namespace=sites/north, name=sw-*` — so nobody has to learn a second query
  language, and `from`/`to` take the three spellings `netgraph path` takes plus a selector,
  which turns "every access switch reaches the core" into one line.

  A failure names the assertion, the elements, what the graph actually contained, and the
  **file and line the assertion is written on**, taken from the loader's provenance, so an
  editor and a CI annotation both link straight to it. `-F json` is the whole run for a
  script; `-F junit` is the XML GitHub, GitLab and Jenkins all render natively, one
  `<testcase>` per assertion with the source location as attributes. `--list` prints what
  would be graded without grading it.

  A run that checked nothing fails: an empty selection, a `SUITE` glob matching no suite,
  and an inventory declaring no suite are all errors rather than vacuous passes. Suites
  ship for both bundled examples (`examples/home-lab/tests.yaml`,
  `examples/campus/tests.yaml`); `docs/commands/test.md` is the reference and `docs/ci.md`
  has the pipeline snippets.

- **The editor is usable on a thousand-device inventory.** Every feature so far had been
  built against a five-device example. `tools/bench_editor.py` (new) opens the 1056-device,
  2106-document tree `tools/bench_pipeline.py` generates in a real browser and reports what
  a person actually waits through — cold open, the re-render after one field, the latency
  from a write to the canvas, the tab's heap and DOM, and a fifty-node move.

  The worst of what it found was not on anybody's list: **drag one node and every redraw
  afterwards took 58 seconds.** A drawing with *some* positions stored is laid out twice,
  and the first of those runs — which exists only to read coordinates back — was also being
  asked to route the edges, whose answer it discards. `neato`'s spline router on nodes it
  did not place is superlinear. The probe run no longer routes anything, the overlap repair
  that follows it buckets nodes into a grid instead of comparing every pair, and the same
  redraw is now 2.1 s for the identical drawing.

  Three more, each half a second of every edit: the write path reparsed the whole tree three
  times because the parse cache reached the read path and not the write one; the validator
  ran four times over objects that had not moved; and every answer carried all 2101 findings
  — 538 kB and that many DOM rows, for a list nobody reads past the first screen of.

  In the browser, a drawing above four hundred elements is now **culled to the viewport**
  plus a margin: 140 of 2106 elements drawn and 2 872 DOM nodes instead of 12 682. Zoomed
  out past the point where a device name is a smudge, the labels and icons come off and each
  namespace grows a frame with its name on it. Nothing about *reaching* an element changes —
  the arrow keys, the outline, the palette and find-in-diagram work from the records rather
  than from the drawing, and selecting something off screen brings it back.

  And the page says what it cannot make fast: a layout that has not come back counts the
  seconds rather than sitting still, and a culled canvas says how much it is drawing and how
  to reach the rest. The measured ceilings are in
  [`docs/follow-ups.md`](docs/follow-ups.md) entry 20; `tests/test_editor_performance.py`
  stops any of it being given back.

  | 1056 devices | Before | After |
  |---|---|---|
  | cold open | 1565 ms | 1345 ms |
  | edit one field | 1736 ms | 635 ms |
  | move a 50-node selection | 2056 ms | 950 ms |
  | redraw after dragging a node | 58 152 ms | 2 119 ms |

- **A round trip with draw.io: `netgraph export drawio` and `netgraph import drawio`.**
  netgraph's pitch is "draw.io for infrastructure, with the YAML as the source of truth".
  This is where it meets the actual tool: a diagram can be handed to a stakeholder who has
  never installed netgraph, edited in draw.io, and brought back as a reviewable changeset.

  **Export.** `netgraph export drawio` writes an mxGraph model of one view — `--view` picks
  which of the nine — carrying the stored arrangement (§18), so the file opens *already
  arranged* rather than as a heap draw.io lays out afresh. One vertex per node, one edge per
  link with its waypoints, a container frame per namespace so dragging a site carries its
  devices, and the shipped icons inlined as data URIs so the file is self-contained.
  `--icons`, `--frames/--no-frames` and `--compress/--no-compress` say how; the plain
  encoding is the default because a diagram that is text is a diagram that reviews and
  diffs, and draw.io opens both.

  **Identity, not labels.** Each cell carries `netgraph:name`, `netgraph:kind`,
  `netgraph:document`, `netgraph:hash` and the coordinates it left netgraph at. That is what
  makes the label free to *mean* something on the way back.

  **Import.** `netgraph import drawio FILE` reconciles by those attributes. A cell that
  moved becomes a geometry write, one whose label was retyped becomes a `rename` with every
  reference rewritten, one that is gone becomes a cascading `delete`, and an edge somebody
  drew becomes a `connect` on the first free port at each end. Everything is expressed as
  `netgraph edit` operations and shown as a `netgraph plan` changeset, confirmed before a
  single file moves. `--dry-run`, `--auto-approve` and a switch per gesture.

  **What it will not do.** A missing cell is a deletion only when the file says it held the
  whole view: export narrowed by `--namespace` and nothing is ever deleted on the strength
  of it. A file netgraph did not export carries no identity, so nothing is reconciled — it
  is read and reported cell by cell, with the kind each one looks like, and netgraph will
  not invent hardware from a rectangle. Re-importing an untouched export changes nothing at
  all, which the suite asserts as an empty plan over every published example and every view.

  `netgraph import` is now a group, and its original signature still works unchanged:
  anything that is not the name of a sub-command is read as a capture file, so `netgraph
  import caps/*.json` means what it always did.

  [`docs/drawio.md`](docs/drawio.md) is the workflow, including what a draw.io user may and
  may not safely change.

- **Links are first-class geometry: waypoints, routing styles and label positions.** A
  `kind: layout` document already said where every node went; it now says how every cable
  gets there. In a hand-arranged diagram that was the last thing that did not stay where
  its author left it — an edge was whatever Graphviz decided, on every render.

  **Waypoints.** `spec.views.<view>.edges.<address>.waypoints` is the list of bends a link
  is routed through. They are *interior* points: the two ends of a route are the nodes
  themselves, so dragging a device carries its cables along instead of stranding them. In
  the editor, click a link to select it, double-click the line to drop a bend where you
  clicked, drag a bend to move it, drag the hollow midpoint handle to insert and place one
  in a single motion, and right-click a bend to remove it. From the keyboard, `b` adds a
  bend, `Shift-B` straightens the link and `r` sets its routing style. Every one of them is
  a `set-link-geometry` operation through the same comment-preserving write path as any
  other edit, so a bend dropped in a browser is a hunk in a YAML file.

  **Routing styles.** `spline` (the curve Graphviz draws, unchanged and still the default),
  `orthogonal` (right angles) and `straight` (segment to segment) — settable per link, per
  view (`views.<view>.routing`) and per inventory (`spec.routing`), most specific winning.
  `--routing` on `render`, `watch`, `diff` and `path`, and `routing` in the `[render]` table
  of `netgraph.toml`, set a *default* that a link pinning its own style still beats;
  `netgraph layout --write --routing STYLE` records the view's. For a fully arranged view
  netgraph computes each route itself and writes it into the Graphviz `pos`, which is the
  only way a per-link style can be expressed at all — Graphviz has a graph-wide `splines`
  and nothing per edge. For a view Graphviz is laying out, only that graph-wide attribute is
  available, and netgraph now says out loud what it could not honour rather than emitting a
  document Graphviz quietly draws differently.

  **Label positions.** `edges.<address>.label` is `{at, offset}` — how far along the route
  the annotation sits, and how far off the line. Stored on the link rather than as a
  coordinate, so it survives both endpoints being dragged, which is the whole reason a label
  gets nudged. Applied by the DOT, SVG, PNG, PDF and HTML renderers, and published by JSON.

  **Parallel links and self-links.** Two cables between one pair of devices no longer land
  on the same line in a fixed drawing: they are fanned 14 points apart, centred so a lone
  link is not moved, and each gets a grab handle of its own. A bundle folded by
  `--bundle-links` counts once. A self-link is drawn as a ring standing off its node, so
  four VLANs terminating on one switch are four rings rather than one thick one.

  `netgraph render -f json` publishes both halves per edge: what the inventory pinned
  (`waypoints`, `routing`, `label`) and, for an arranged view, the line that was drawn
  (`route`, `controls`, `drawnAs`). See
  [`docs/rendering.md`](docs/rendering.md#links-are-geometry-too), which carries a worked
  example checked against a committed golden.

- **A history timeline: `netgraph log`, two revisions on `netgraph diff`, and a scrubber
  under the canvas.** The inventory is a folder of YAML in a repository, which means its
  whole history is renderable — and nothing else in this space can show you when a network
  became what it is.

  **`netgraph log`** lists the commits that touched the inventory, newest first, with a
  one-line summary of the changeset each one carries: `3 devices added, 1 link removed,
  2 addresses moved`. The summary is a real changeset, computed by the same code
  `netgraph plan` and `netgraph diff` use, so a commit that only reformatted a file says
  `no change to the network`. `--from`/`--to` take a range, `-n` a count, `--json` a
  document per commit, and `--no-summary` the commit list alone with nothing read.

  **`netgraph diff --from <rev> --to <rev>`** now reads any two revisions — a tag against a
  tag, `HEAD~10` against `HEAD` — with `--to` still defaulting to the working tree. Both
  sides come out of the object database with `git archive`: nothing is checked out, the
  index is untouched, and an uncommitted change is neither used nor disturbed.

  **In the editor**, `History` (`Ctrl-Shift-H`) opens a scrubber along the bottom of the
  canvas over those commits. The diagram repaints as the diff overlay for the selected
  commit against its parent, with the subject, author, date and summary beside the control;
  `Alt-Left`/`Alt-Right` step, `Alt-P` plays through the range. Positions come from the
  layout document *as that revision had it*, so a diagram that was arranged stays arranged
  as you scrub.

  It is honest about its edges. A revision whose inventory does not load is shown as such —
  and stops the playback — rather than being skipped; a revision from before the inventory
  folder existed reads as an empty network rather than as a failure, and says so; a range
  wider than `[history] max-revisions` (100 by default) is refused by `netgraph log` and
  truncated-with-a-count by the editor rather than becoming two hundred Graphviz runs.
  Frames are cached by the pair of tree hashes they sit between, so scrubbing back over
  ground already covered is instant, and neighbouring revisions share their loaded state
  and their parsed files. `tools/bench_history.py` measures all of it against the
  1056-device benchmark tree.

  [`docs/commands/log.md`](docs/commands/log.md),
  [`docs/commands/diff.md`](docs/commands/diff.md#two-revisions) and
  [`docs/commands/web.md`](docs/commands/web.md#the-history-timeline) document it.

- **`netgraph lsp`, a language server, so the editor knows what netgraph knows.** An
  inventory is written by hand in a plain text editor, and until now the editor could be
  told the shape of one document — through the published JSON Schema — but nothing about
  the tree it belongs to. The server closes that: LSP 3.17 over stdio, no new dependency,
  started by your editor rather than by you.

  Diagnostics are `netgraph validate`'s, on the line and column that caused them, carrying
  the `NG-*` rule id as the diagnostic code and a link to that rule's section of
  [`docs/validation-rules.md`](docs/validation-rules.md). Completion is the JSON Schema for
  keys, enums and their documentation, *and the tree* for references: typing under a
  cable's `endpoints` offers the switches you have, and `sw-home:` offers the ports that
  switch has. Hover resolves a reference to the device, the port, its addresses, its VLAN
  and what is already cabled to it. Go-to-definition and find-references work across the
  whole folder. Rename goes through the same write path as `netgraph edit rename`, so every
  reference in every file is rewritten with the comments intact. Formatting is
  `netgraph fmt`; the code actions are the `--fix` catalogue.

  It answers about the text on your screen — unsaved buffers are overlaid on the tree
  before it is loaded — and it watches the folder the way `netgraph watch` does, so an edit
  made in a terminal refreshes the diagnostics. Opened on a lone file rather than a folder,
  the checks that can only be judged against a whole tree are held back rather than
  reported against a document that cannot satisfy them.

  [`docs/lsp.md`](docs/lsp.md) has the setup for VS Code, Neovim, Helix and Emacs, and a
  minimal VS Code client ships in [`editors/vscode/`](editors/vscode).

- **`netgraph validate --fix` repairs what the inventory itself determines, and the editor
  puts a `fix` button on each of those diagnostics.** Half the value of a diagnostic is
  knowing what to do about it, and for a good part of the catalogue the tree already says:
  a `kind: layout` document placing an element that has been deleted, a MAC address on a
  software loopback, a port trunking a VLAN its device's database does not declare, a VRF
  nothing is bound to, a group still listing somebody who has left, a cable endpoint naming
  a port one letter away from one that exists.

  `--fix` applies every repair that has exactly one reading and reports the rest;
  `--fix --dry-run` prints the unified diff and writes nothing; `--choose W114=list` decides
  a rule that has two. Writes go through the same path as `netgraph edit`, so comments, key
  order and quoting survive and only the lines the repair is about change.

  **A fix never introduces a finding.** Each is applied on its own and the tree is validated
  again; unless the finding it was aimed at is gone and no rule reports more than it did
  before, the bytes are put back and the refusal is printed with the findings it would have
  added. So "remove the cable" is offered, and refused on a two-device inventory where it
  would orphan a device — which is a decision for a person.

  `netgraph rules --fixable` lists what can be repaired and what each repair does;
  [`docs/validation-rules.md`](docs/validation-rules.md#fixing-a-finding) says the same,
  generated from the table so it cannot drift.

- **A fourteenth edit operation, `append`**, which adds one entry to a sequence and creates
  the sequence if it is absent. `set` cannot add a list entry that does not exist yet, and
  replacing a whole list to add one would rewrite the comments beside the entries already
  in it. Its inverse is an `unset` of the position it wrote.

- **The editor can be driven entirely from the keyboard, and read without a screen.**
  `netgraph web` was becoming pointer-only, which is where visual tools stop being usable
  for the people who work fastest in them.

  **`Ctrl-K` opens a command palette** over every command the page has — every `netgraph
  edit` operation, every view and layer toggle, open-file, go-to-element, validate, the
  changes drawer — searched in one field alongside every element address and file path in
  the inventory. Each row prints the key that runs it, so the palette teaches the bindings;
  a command that cannot run now is greyed with the reason rather than hidden. **`?` opens
  the shortcut sheet.**

  **The diagram is navigable.** `Tab` reaches the canvas, the arrow keys walk it — preferring
  the elements the focused one is *linked to*, so a path is followed rather than a grid
  swept — `Enter` opens the inspector, and `n`, `c`, `F2` and `Delete` are the create,
  connect, rename and delete gestures. The focus ring is deliberately not the selection
  ring: solid violet against a long dash, with another client's selection a short one.

  **The SVG is no longer inert.** Every node and link carries a role and a label built from
  the same record the info box uses — *"sw-home, switch, 8 interfaces, linked to
  routers/rtr-home on port1"* — the canvas announces which element is current, `Alt-4`
  opens the whole view as a textual outline, and every applied, refused or reverted gesture
  is announced in a live region, once.

  The interface now follows `prefers-color-scheme` with a palette per scheme (one set of
  colours cannot clear 4.5:1 against both a white and a near-black background) and honours
  `prefers-reduced-motion`. The diff legend prints `+`, `~` and `−` and three line styles
  beside its three hues, so the encoding survives a greyscale print and a red-green reader.

  It is gated: `tests/test_browser.py` runs axe-core over the page in both colour schemes
  and fails CI on any WCAG 2.1 AA violation, and drives one end-to-end test — create a
  device, cable it, undo both — without dispatching a mouse event. The bindings live in
  `netgraph.web.bindings`, are served at `GET /api/bindings`, and are what
  [`docs/commands/web.md`](docs/commands/web.md) documents, generated; a shortcut that is
  documented and dead fails the suite.

- **The editor pushes instead of polling, and a second tab is a feature rather than a
  race.** `netgraph web DIR` used to check a revision number once a second and, whenever it
  moved, refetch the whole file list and re-lay-out the whole diagram. It now opens a
  server-sent-events stream, `GET /api/events`, that says *what* moved the moment it does:
  `tree-changed`, `file-changed`, `history-changed`, `disk-changed`, `presence`.

  Two consequences, both measurable. A save of one file refetches **that file's row**
  (`GET /api/tree?path=…`), not the tree. And a revision that does not change the picture on
  screen **does not redraw it**: the page sends the fingerprint of the drawing it is
  showing, and the server compares it with the DOT this revision would produce and answers
  `unchanged` rather than running Graphviz. On a 1056-device tree, editing a description
  went from 1.7 s to 185 ms; `tools/bench_events.py` is the harness and
  [`docs/follow-ups.md`](docs/follow-ups.md) entry 18 has the table.

  **It falls back.** A buffering proxy, a browser without `EventSource`, a stream that will
  not open — any of them drops the page back to polling `/api/state`, which replays the same
  events with the same ids out of the same ring buffer into the same handlers. A client that
  makes plain `GET`s, `curl` included, never has to know the stream exists, and no write is
  gated on having read one. An indicator above the file list says which path you are on.

- **Presence and soft locking in the editor.** Every connected page is listed, what somebody
  else has selected is drawn on the canvas as a faint dashed halo, and a file another client
  has unsaved edits in is badged `in use`.

  Advisory throughout: it blocks nothing, it expires by itself if a tab goes away without
  saying so, and the only things that can refuse a write remain the content hash of a
  whole-file save and the tree revision of an operation batch. A lock a heartbeat can hold is
  a way to lock an inventory by closing a laptop lid. Two new routes carry it:
  `POST /api/presence` and the `clients` list on `GET /api/state`.

- **`netgraph diff`, and a changes drawer in the editor: a changeset, drawn.** `netgraph
  plan` already answered *what changed* and every renderer already answered *what the
  network looks like*; nothing put the two together. `netgraph diff` renders one diagram
  holding both states — added elements and links **green**, removed ones **red and dashed
  but still in place**, changed ones **amber with a badge naming the fields that moved**,
  everything untouched **faded**.

  A removed node keeps the position its layout document gave it. A deletion that reshuffled
  the diagram would hide itself in the churn it caused, which is the one thing a change
  review cannot afford.

  Two things decide the marks and there is no third opinion about what changed. **Presence**
  in the two drawings decides added and removed — the only thing that can answer for a
  derived node, since nothing declares `subnet:10.0.0.0/24`. **The plan** decides everything
  finer: that an element was updated rather than merely still present, which of its fields
  moved, and that a box is the same device under another name. A rename is therefore one
  amber box badged `was <old address>`, not a red box beside a green one.

  The two sides come from wherever `netgraph plan` reads them, plus `--against HEAD` (the
  same side as `--from`, spelled the way the question is asked) and `--plan FILE`, which
  executes a saved plan into an edit session that is never committed — so what is drawn on
  the right is the text `netgraph apply` would write, not a reconstruction of it. Every
  `render` format is supported except Mermaid, which can neither colour a node nor hold a
  changeset beside one and says so rather than drawing a diagram in which nothing
  distinguishes the deleted switch. `-f json` publishes a `diff` object on every node and
  edge — untouched ones included — plus the whole changeset under `changeset`.

- **A changes drawer in `netgraph web --write`.** It lists every gesture made in the
  session — one entry per gesture, not per operation, so deleting a switch is one line
  rather than five — each with the YAML hunk it wrote as a unified diff, a click on its
  label that reveals the document it changed at its line, and a per-entry **Revert**.

  A revert is a new change, not a rewind: it applies the gesture's own inverse as a fresh
  edit, which is itself logged and itself undoable, so reverting the third of ten gestures
  leaves the other nine alone — and fails, loudly and without writing, when one of them
  depended on what the third one did.

  Opening the drawer repaints the canvas as a diff against the state the session started
  from, or against `git HEAD` when the inventory is in a repository, so an afternoon's
  editing can be reviewed as a diagram before it is committed. Three new API routes carry
  it: `GET /api/changes`, `GET /api/diff?against=session|git` and `POST /api/revert`.

- **A handover button.** *Copy commands* hands the session over as a list of `netgraph
  edit` invocations, in the order they happened, for a pull-request description or somebody
  else's terminal. The rendering is never lossy: an operation a subcommand takes exactly
  becomes that subcommand, and one it does not becomes `netgraph edit apply -f -` with the
  operation's own JSON on standard input. There is deliberately no third case where a
  rendering approximates an operation.

- **`user` and `group`, two new element kinds: who the network is for.** Every other kind
  in the schema answers *what is there*. These answer *whose is it, and who may touch it* —
  the question an audit asks first and the one an inventory of boxes and cables cannot
  answer at all. Both are ordinary elements: they load, validate, format, diff, apply,
  render and export through exactly the machinery every other kind goes through, and
  neither owns interfaces, so an identity terminates no cable and appears in no data layer.

  A `user` carries the account — `login` (defaulting to `metadata.name`), `full_name`,
  `email`, `uid`, a `type` of `person`/`service`/`shared`, a `status` of
  `active`/`suspended`/`departed`, and `ssh_keys`. Public keys only: a pasted **private**
  key is refused with an explanation, which is the mistake the check exists for.

  A `group` carries `members`, `gid` and `email`. A member may name a `user` **or another
  `group`**, so a hierarchy is expressible: `everyone` holds `engineering` holds `ana`.
  Membership is written on the group and nowhere else — a `user` does not list its groups,
  because two spellings of one fact are how an inventory starts disagreeing with itself —
  and the reverse index is derived where it is needed.

  Ten new rules, lettered `S` for *subject*: `NG-S001`–`NG-S003` on the documents, and
  `E043`–`E046`, `W139`, `W140` and `I004` on the tree. The one worth knowing about is
  **`W140`**: a group that still lists somebody whose account is `departed`. Deleting a
  leaver's document removes them from the inventory *and* from every group naming them,
  losing exactly the list of access somebody has to go and revoke. `status: departed` keeps
  that worklist visible until it has been worked through.

- **`netgraph render --layer identity`**, the ninth layer: the users and groups, joined by
  membership, and no hardware whatsoever. A user is drawn as an oval and a group as a
  folder, in a rose palette no element kind had taken, and both have an icon in the bundled
  theme. A membership edge runs from the group to the member — the direction the fact is
  written in. Everything else is discarded, for the same reason the power view discards the
  cabling: a cable between two servers says nothing about who may log into either.

- **`netgraph list users` and `netgraph list groups`.** `users` prints a `GROUPS` column,
  which is the one fact about a person their own document cannot state. `groups` prints two
  member counts: `MEMBERS` is what the document names, `PEOPLE` is how many accounts the
  group reaches once the nesting has been walked — the number an access rule actually
  grants to, and the number no single document holds. Both tables also appear on the
  Identity section of `netgraph report`.

- **`netgraph plan` and `netgraph apply`: a typed changeset between two inventory states.**
  The inventory is meant to be a source of truth that can be *diffed and applied*, and until
  now only the read half existed: `netgraph drift` compared a live network against the
  declaration and reported. The general diff engine and the write half are now here.

  Every element has a stable **address** — `device.core/sw-1`, `cable.core/uplink` — whose
  type is a category rather than the document's `kind`, so `kind: switch` → `kind: router`
  is an update of one element rather than the destruction of one and the creation of
  another. `netgraph plan` diffs two loaded inventories into an ordered changeset of
  `create`, `update`, `delete` and `rename` entries, each with the address and the
  field-level before/after pairs, and prints it in the terraform shape
  (`+ 3 to add, ~ 5 to change, - 1 to destroy`).

  Three things make the output worth reading. **Renames are detected structurally** — by
  serial, MAC, link ends, cable label, rack slot or a `netgraph.dev/id` annotation — so a
  renamed switch is one entry rather than a delete plus a create, and only where the
  evidence names exactly one element on each side. **The entries are in dependency order**:
  a cable is destroyed before the device it terminates on and created after it. And **the
  comparison is of meaning, not text** — templates merged, ranges expanded, defaults filled
  in — so a tree somebody has just run `netgraph fmt` over produces an empty plan.

  The two sides come from wherever they can: `--from <git-ref>` against the working tree
  (read with `git archive`, so the working tree is never disturbed), two folders with
  `--from`/`--to`, or `--from-live` reusing the `import` and `drift` collectors. The last
  is the one that closes the loop: the desired state is *the declaration with the
  observations written into it*, never the capture rendered as YAML, so a partial capture
  proposes corrections and never a cull — a declared cable is only removed where a port
  contradicts it, a re-patched lead keeps its document, and a trunk's VLAN set is merged
  rather than substituted.

  `netgraph apply` executes a plan against the **files**, translating each entry into the
  `netgraph edit` operations from the previous release, so comments, key order and
  formatting survive and the same validation gate applies. `netgraph plan -out drift.plan
  && netgraph apply drift.plan` adopts what the network reports into the inventory. The
  plan file records a hash of the state it was made from and apply refuses a tree that has
  moved on; `--target` applies a subset, `--auto-approve` skips the confirmation, `-n`
  prints the diff instead of writing it. `--json` and `--fail-on changes` are for CI.

  **Applying to the live network is deliberately out of scope.** `netgraph apply` writes
  YAML and nothing else: it opens no session to a device and there is no flag that makes
  it. See [`docs/commands/plan.md`](docs/commands/plan.md) and
  [`docs/commands/apply.md`](docs/commands/apply.md).

- **`netgraph layout`, and a diagram that stays where you put it.** Until now the picture
  was derived: Graphviz laid the graph out afresh on every render, so a diagram could not
  be *arranged* — move a node and the next render moved it back. Geometry is now
  first-class, optional inventory data. A `kind: layout` document holds a position per
  node, keyed by element address and scoped by view (`l1`, `l3`, `routing`, …), because the
  same switch sits somewhere different in each. It is a **sidecar** rather than a field on
  each device, so model files stay free of pixels and an arrangement can be dropped,
  regenerated or versioned on its own — the reasoning is recorded in
  [`docs/follow-ups.md` §16](docs/follow-ups.md).

  `netgraph layout --write` runs the automatic layout once and persists the result, which
  is what makes the diagram editable from then on; running it again places what is *not*
  yet placed rather than discarding an afternoon of arranging (`--replace` asks for the
  whole view afresh, `--engine` picks a different Graphviz engine). `--clear` goes back to
  automatic, `--prune` drops geometry for elements that no longer exist. With no flags it
  reports what is arranged and what has gone stale. Writes go through the `netgraph edit`
  path, so comments and formatting survive and `--dry-run` shows the exact hunk.

  The renderers honour it with no flag of their own. When every node in a view is placed,
  Graphviz runs in **no-op layout mode** and the output reproduces the arrangement point
  for point — verified by a test that renders a seeded diagram and reads the coordinates
  back out. When only some are, those are pinned, the engine places the rest around them,
  and anything still overlapping is separated without moving a node somebody placed by
  hand. `svg`, `png`, `pdf` and `html` are the same Graphviz run and `json` publishes the
  same coordinates in the same units, so a browser can draw the graph itself. Namespace
  frames in a fixed layout are drawn from the stored group boxes, since the no-op engine
  draws no clusters; their captions sit above the frame rather than inside it, for the
  reason in [`docs/follow-ups.md` §17](docs/follow-ups.md). See
  [`docs/commands/layout.md`](docs/commands/layout.md),
  [`docs/schema.md` §18](docs/schema.md#18-layout-diagram-geometry) and
  [`docs/rendering.md`](docs/rendering.md#stored-arrangements).

- **`W138` / `NG-Y001`, stale diagram geometry.** A warning — never an error, because
  deleting a switch must not make `netgraph validate` fail — naming each layout key that
  no longer resolves. `netgraph layout --prune` is the fix.

- **`netgraph edit`, the write path.** The first way to *change* an inventory that is as
  careful as the way netgraph reads one. Eleven typed operations — create, delete, rename,
  move, set, unset, add-interface, remove-interface, connect, disconnect, and any of them as
  JSON on stdin — each applied through a round-trip parser, so comments, blank lines, key
  order and quoting style survive byte for byte and a diff of an edit is the edit. Each one
  is **reversible exactly**: it returns the operations that undo it, so an undo stack is a
  list and undo restores the tree comment for comment. Each one is **reference-aware**: a
  rename rewrites every mention of the element across the whole tree, keeping the spelling
  each document chose, and a delete either takes the cables and tunnels that terminate on
  the element with it (`--cascade`) or refuses and names them. New documents are **placed**
  by the conventions in [`docs/inventory-layout.md`](docs/inventory-layout.md), and the last
  document leaving a file takes the file — and the folder — with it. Two gates stand between
  an edit and the disk: the tree is loaded and validated *as it would be* and the write is
  refused if it would introduce a new error (`--force` overrides), and every file is hashed
  when it is read and checked again before it is written, so a file that changed underneath
  is a reported conflict rather than a lost edit. `--dry-run` prints the unified diff it
  would write, `--json` prints the applied operations and their inverses. See
  [`docs/commands/edit.md`](docs/commands/edit.md) and
  [`docs/editing.md`](docs/editing.md).

- **A container image published on every push.** `ghcr.io/blechschmidt/netgraph` now also
  carries unreleased work, tagged after the ref it was built from: `<branch>` for every
  branch pushed (slashes become dashes, so `feature/vlans` is `feature-vlans`),
  `sha-<commit>` for the exact commit, and `edge` for the tip of the default branch. So a
  fix that has landed — or a colleague's branch — can be run without a Python environment
  and without waiting for the release that carries it. Same two platforms, same provenance
  attestation and SBOM as a release, because it is now the same workflow: a `v*.*.*` tag
  builds through the same file and takes the semantic version tags `X.Y.Z` and `X.Y` from
  the tag itself. `latest` is unchanged and still follows releases only — a branch build has
  no way to reach it — so an unqualified `docker pull ghcr.io/blechschmidt/netgraph` cannot
  land on unreleased work. The image is rebuilt weekly against a fresh `python:3.12-slim`
  and Graphviz, and every pull request now builds it for both architectures and runs it
  before anything can be merged. See
  [`docs/docker.md`](docs/docker.md#the-development-image).

- **`netgraph report`, the as-built documentation.** One command writes the document an
  engineer is asked to hand over: an overview, a page per site and a page per device, with
  the layer diagrams, the address plan and its utilisation, a VLAN-to-subnet-to-device
  matrix, the cable schedule with the patch panels named, the port map of every panel, the
  BSS and SSID plan, the PDU load schedule, each device's interfaces, placement, links and
  routing, and the open validation findings — so a report never presents an invalid
  inventory as authoritative. `--format markdown` (the default) is committed next to the
  inventory and reviewed as a diff; `--format html` is one self-contained site where every
  device in every diagram links to its own page; `--format json` is the whole document in
  one file. Every table comes from the same derivation the matching command prints, so no
  two pages can disagree. The output is byte-identical between runs, `--generated-at` pins
  the one part that is not, and every page carries the netgraph version and the inventory's
  git revision. `--template DIR` overrides the page templates one file at a time. See
  [`docs/commands/report.md`](docs/commands/report.md) and
  [`docs/example-report/`](docs/example-report/), which is one, committed.

- **Power as a modelled layer.** A `pdu` element kind with numbered outlets, a `spec.power`
  block on every device (draw, redundant inputs naming `<pdu>:<outlet>`, PoE budget and
  per-port PoE), a `power` layer that draws which strip feeds what, `netgraph list power`
  for the load schedule, `netgraph export power`, and seven rules (`E037`–`E042`, `W137`)
  covering a claimed redundancy that is not one, an over-subscribed strip and a PoE budget
  that does not add up.

- **A parse cache, on by default.** A file that has been parsed once is remembered, keyed by
  the hash of its bytes together with the netgraph, parser and model versions that read them,
  so it cannot go stale. A repeated load costs 0.30 of a cold one in a new process and 0.05 in
  a running `netgraph watch`, where a re-render now re-parses only the file that was saved.
  Nothing about a timestamp enters the key: a `touch` changes nothing and a `git checkout` of
  a revision seen before hits again.
- **`netgraph cache info`** reports where the cache is, what is in it, and the identity an
  entry is keyed by; **`netgraph cache clear`** empties it, `--all` for every inventory.
- **`--no-cache`**, a global flag, parses everything and remembers nothing.
  `NETGRAPH_NO_CACHE=1` does the same for a whole environment, and `NETGRAPH_CACHE_DIR`
  moves the cache — both of which is what a CI job wants. See
  [`docs/configuration.md`](docs/configuration.md#cache--remembering-parsed-files).
- **`[cache]` in `netgraph.toml`** — `enabled`, `dir` and `max-size`, for an inventory that
  needs to say where its cache goes on the machines it is used on.

### Fixed

- **The address rules now know that a namespace partitions the address space.** Without this
  a perfectly ordinary container host is reported once per container: `E004` on two containers
  built from one image, `W111` on both ends of every routed veth pair, `W105` on the bridge
  every container hangs off. `E004` now scopes a duplicate to the *stack* — and to the machine,
  because unlike a VRF name a namespace name means nothing outside the host that runs it;
  `W111` groups by namespace as well as by VRF; and `W105` counts stacks rather than machines,
  so three containers on one bridge are three parties and not one lonely element.

- **Deleting one of a server's two PDUs took the server with it.** Clearing a power input is
  right; leaving `redundant: true` behind on the one feed that is left is not, because that
  flag claims the device survives losing a feed and one feed does not. `E042` / `NG-E015` is
  a *load* error, so the server stopped loading altogether and every cable that ended on it
  started reporting a dangling endpoint. The flag now goes with the feed it was about.

- **`netgraph import drawio` put back the coordinates of the node it had just deleted.** The
  geometry write is built from the arrangement the tree held *before* the import, which still
  places everything the import removes, and it runs last — so a diagram with one node deleted
  and one dragged came back with a stale `W138` per node removed. The deleted keys are taken
  out of it, using the same closure the delete itself runs.

- **The editor said "saved" and "unsaved changes" at the same time.** Typing into a file
  and then putting the text back the way it was left the badge up, the Save button enabled
  and the file listed as `unsaved` in everybody else's browser — for ever, because nothing
  short of opening another file cleared it. The page read its new state out of the list of
  files the write had touched, and a write whose bytes were already there touches none. The
  badge is about the gap between the pane and the file, not about whether the filesystem
  moved, and it now says so.

  Three more of the same shape, found by the tests written to reproduce that one:

  * **A change made on the diagram silently threw away unsaved typing in the pane.** A
    canvas gesture is applied to the file, so a page holding unsaved text in that file has
    two documents in hand — and it used to adopt the file and drop the text without asking.
    The change still goes to the file; the pane is now marked `conflict` and keeps your
    text, which is exactly what it already did when `$EDITOR` was the one that moved
    underneath.
  * **Deleting an element left its file on screen, badged `deleted on disk`.** The editor
    blaming the filesystem for something it had just done itself, over a document one
    Ctrl-S would have put straight back. The pane now closes — unless it holds unsaved
    text, in which case that text is the only copy left and it stays, badged for what it is.
  * **"save again to overwrite it" could not be taken up.** A file changed on disk under
    unsaved edits is a `conflict`, and the toast that says so offers the way out. Saving
    again stopped quoting a precondition — meaning "over whatever is there" — but an absent
    hash is how the write route spells *create*, so the second Ctrl-S came back `already
    exists; open it before writing to it` and the offer was a lie. The page now adopts the
    hash the refusal reported and quotes *that*, so the retry writes over the version it was
    told about and a file that moves a third time in between is refused again.

  Also: a `file-changed` event that overtook the response to the write that caused it could
  badge a plain Ctrl-S as somebody else's conflict. The page now ignores the echo of its own
  writes, as it already did for `tree-changed`.
- **Nothing could be drawn on Python 3.11: importing `netgraph.render` raised `ValueError`.**
  Two fields of the style resolver defaulted to a `mappingproxy`, which `dataclasses`
  refuses before 3.12 and refuses at *import* time — so on the 3.11 the package claims to
  support, `netgraph render`, `watch`, `web`, `export` and everything else that reaches the
  renderer failed on the import, not on the work. This is the second time the same shape of
  bug has shipped (`netgraph lsp`, above), so the guard is now the package rather than the
  module: `tests/test_release.py::test_no_dataclass_default_is_a_mapping_proxy` imports
  every netgraph module and inspects every dataclass field, on whatever interpreter you run
  it with.
- **`netgraph plan` named a folder with backslashes on Windows.** The header of a plan, and
  the `description` recorded in a plan file, spelled `--to examples/home-lab` back as
  `examples\home-lab` — so the same command against the same two trees produced a different
  document depending on who ran it. Every path netgraph prints for a person to read now goes
  through one function, `netgraph.fsio.display_path`, which is where the "relative to here,
  forward slashes" rule that `netgraph fmt`, `drift` and `test` already followed now lives.
- **Orthogonal links were drawn straight across the devices they passed.** An arranged
  diagram with `routing: orthogonal` routed each leg locally and avoided nothing, so a
  cable between two switches with a third between them was drawn across the third one's
  box — the most visible thing wrong with a hand-arranged picture, recorded as
  [`docs/follow-ups.md` §19](docs/follow-ups.md) and left there for four releases because
  nobody had a number for it. There is one now: `tools/route_crossings.py` counts the
  links whose line runs across a box they are not attached to, and on the committed
  fixtures it goes from five and three to **zero**.

  Routes now go **around**. Every placed node — and every free-standing `area` and placed
  `note` — becomes an obstacle, and an A\* over their Hanan grid finds the cheapest way
  past, charging for bends, for crossing a line already drawn and for crowding a channel.
  Several links between the same two devices route as one bundle in parallel lanes rather
  than as separate detours that fan out and re-converge.

  **Nothing you placed is moved, and nothing is written to your files.** A bend you
  dragged is authoritative — routing fills the segments between bends and never touches
  one — and a link whose line already keeps clear renders byte-identically to before. A
  computed route is recomputed every render; `-f json` publishes it as `layout.routed`
  beside the bends the inventory pins, and in `netgraph web` `Shift-R` (**Pin the computed
  route**) is what makes one permanent. `--no-avoid`, or `avoid = false` in `[render]`,
  turns it off. See [`docs/rendering.md`](docs/rendering.md#routing-around-things).
- **netgraph could not be imported on Python 3.11, and the timeline crashed on 3.10.** Two
  separate instances of the same shape of problem, both found by the CI matrix and neither
  reachable from the version the work was done on. `netgraph lsp` declared a `mappingproxy`
  as a plain dataclass default, which is refused before 3.12 and refused at *import* time,
  so every LSP command raised `ValueError: mutable default` on 3.11. And `netgraph history`
  parsed git's `%aI` with `datetime.fromisoformat`, which before 3.11 accepts `+00:00` and
  not the `Z` some builds of git write, so a timeline over such a repository raised
  `ValueError: Invalid isoformat string` on 3.10. Both are one line; both now have a test.
- **Clicking a node in `netgraph web` did not open the document that declares it.** The
  page passed the SVG element id where the tree is keyed by element address, so the lookup
  matched nothing and the click silently did nothing at all — the one mapping the command
  exists for.
- **`Ctrl-Z` in `netgraph web` left the editor showing text that was nowhere on disk.** The
  undo restored the file correctly and the pane kept the version it had just replaced,
  under a badge that said there was nothing unsaved. The pane is now reloaded from the file
  the undo produced.
- **A change made outside `netgraph web` was noticed and then ignored.** The open file was
  compared against the *previous* file list rather than the one the change had just been
  fetched into, so the hashes always matched and the one file that had moved on disk was
  the one thing left stale. Editing an inventory in `$EDITOR` with the browser open now
  reloads it there, or marks it conflicted when the pane has unsaved edits.
- **A refused request left the connection unusable in both local servers.** Every refusal
  that answers without reading the request body — a 404 for an unknown route, a 403 from a
  read-only session, the host check's 421 — stranded that body in the socket, where HTTP/1.1
  keep-alive made the *next* request on the same connection parse out of it. The symptom
  was a `501 Unsupported method` naming a fragment of JSON, on a request that was perfectly
  well formed.
- **netgraph could not start on Python 3.11.** Every command raised `ValueError: mutable
  default <class 'mappingproxy'>` while importing the configuration layer. 3.10 and 3.12
  were unaffected, which is why it survived a full test matrix — the interpreter in the
  middle was the only one that refuses that spelling of a dataclass default.
- **A lone surrogate escape now fails to load under either YAML parser.** `description:
  "\ud800"` names a code point UTF-8 cannot encode, so every artefact netgraph writes would
  have raised on it. libyaml refused it and the pure-Python parser accepted it, meaning
  whether an inventory loaded depended on which PyYAML wheel was installed.
- **The nesting-depth guard is now a limit both parsers survive.** It was 1024, which is
  past the pure-Python composer's own ceiling, so a document exactly at the documented limit
  was refused there and accepted with libyaml. It is 256 — still four hundred times deeper
  than the schema goes, and the same answer on both.
- **`W129` reported its two tunnels in the order the files happened to be walked in.**
  Splitting one inventory across directories differently changed the finding's text, and
  where one pair of tunnels clashed on two elements, which element was named.
- **`netgraph drift` wrote the inventory path with backslashes on Windows.** Every other
  path netgraph prints uses forward slashes.
- **`netgraph fmt` raised a traceback on a document `netgraph validate` accepts.** The
  round-trip parser the formatter uses resolves `-._` as a float and then fails to convert
  it; netgraph's own loader reads the same scalar as the string it plainly is. It is now a
  diagnostic naming the file, like every other thing the formatter cannot read.
- **Cache entries went missing when several netgraph processes filled one cache at once.**
  Every write went through a scratch file named after its destination, so two processes
  storing the same document wrote through the same file — on Windows, one of forty entries
  would end up never written at all, and stay a cache miss for good. Each writer now has
  its own.

## [0.1.0] - 2026-07-30

First release. netgraph reads a folder tree of YAML documents describing a network, checks
that the documents agree with each other, and renders the result.

### Added

- **The inventory format.** `apiVersion: netgraph.dev/v1alpha1` documents in nine kinds —
  `switch`, `router`, `hub`, `computer`, `server`, `adapter`, `cable`, `tunnel` and
  `patchpanel` — discovered recursively under a root folder, where the folder a document
  sits in becomes its namespace. Field names and value spaces follow RFC 8343
  (`ietf-interfaces`), RFC 8344 (`ietf-ip`) and the IEEE 802.1Q bridge model. Normative
  specification in [`docs/schema.md`](docs/schema.md).
- **Interfaces, addressing and VLANs.** Physical and logical interfaces with MAC addresses,
  IPv4/IPv6 addresses and prefixes, DHCP, `access`/`trunk`/`routed` port modes, native and
  tagged VLANs, LAGs, bridges and sub-interfaces. Interface *ranges*
  (`ethernet-1/1..1/48`) and reusable device templates so a 48-port switch is not 48 blocks
  of YAML.
- **Wireless detail.** SSIDs, bands, channels and widths, and the BSS-to-SSID mapping, with
  station associations drawn as links.
- **Routing.** VRFs, static routes and protocol adjacencies (OSPF, BGP, IS-IS), following
  RFC 8349, plus a `routing` layer that draws them.
- **Tunnels as a first-class kind.** WireGuard, IPsec, OpenVPN, PPTP, GRE, L2TP and VXLAN,
  including tunnels carried inside other tunnels.
- **Passive plant.** Patch panels with derived ports, racks, rack units and a rack-elevation
  view.
- **`netgraph validate`** — three passes (schema, reference resolution, semantics) over a
  catalogue of graded rules, each with an `NG-*` alias, a documented reason and a fix.
  `--strict` promotes warnings, `--disable` silences by id or alias, and the machine-readable
  forms are `--output-format json|sarif|github` for pipelines, code scanning and inline
  annotations. Exit codes: `0` clean, `1` findings, `2` usage.
- **`netgraph render`** — seven layers (`l1`, `l2`, `l3`, `overlay`, `routing`, `rack`,
  `logical`) to `svg`, `png`, `pdf`, `dot`, `mermaid`, `json` and a self-contained
  interactive `html` page. Filters by namespace, VLAN, kind and neighbourhood; namespace
  collapsing and link bundling for inventories too large to read whole; `--icons cisco` for
  device pictures. SVG output carries per-element tooltips, `--link-template` links back to
  the YAML, and stable element ids for deep-linking.
- **`netgraph web`** and **`netgraph watch`** — the inventory edited in one browser pane and
  drawn in the other, and a live preview that re-renders on every save.
- **`netgraph path`** — trace how two elements reach each other, at any layer, with the
  answer optionally drawn.
- **`netgraph ipam`** — subnet utilisation, free space, the next free block, aggregation and
  overlap detection.
- **`netgraph export`** — hosts file, DNS zone, DHCP reservations, Ansible inventory and
  Prometheus targets, generated from the same documents.
- **`netgraph import`** — bootstrap a first inventory from LLDP, `ip -j addr`, `show`
  command output or a cabling CSV.
- **`netgraph drift`** — the declared inventory compared against what the live network
  reports, with per-element coverage so an unchecked device is not silently counted as
  agreeing.
- **`netgraph fmt`** — one canonical form for inventory YAML, with `--check` and `--diff`.
- **`netgraph list`**, **`show`**, **`rules`**, **`schema`**, **`config`** — interrogate an
  inventory, the rule catalogue and the resolved configuration from the shell.
- **`netgraph init`** — scaffold a small, valid inventory, including the JSON Schema and the
  editor wiring.
- **`netgraph completion`** — completion scripts for bash, zsh, fish and PowerShell, with
  completion of element names, namespaces, kinds, layers, formats, profiles and rule ids.
- **`netgraph version`** — the netgraph, Python and Graphviz versions in use, the selected
  YAML parser and the resolved dependency versions; `--json` for pasting into a bug report.
  `netgraph --version` prints the same text.
- **JSON Schema output** (`netgraph schema`) so an editor underlines a typo'd key as it is
  typed, checked into `schema/` and wired up by `netgraph init`.
- **`netgraph.toml`** — per-inventory render defaults and named profiles, so the flags a
  diagram needs live next to the inventory instead of in shell history.
- **CI integrations** — a `netgraph-validate` composite GitHub Action, three `pre-commit`
  hooks (`netgraph-validate`, `netgraph-fmt`, `netgraph-fmt-check`) and a documented GitLab
  recipe.
- **Published artefacts.** `pip install netgraph` (also `pipx` and `uv tool install`), from
  PyPI via Trusted Publishing, and a `linux/amd64` + `linux/arm64` container image at
  `ghcr.io/blechschmidt/netgraph` that already has Graphviz in it and runs unprivileged on a
  read-only root filesystem. The wheel, the sdist and the image carry build provenance
  attestations, and each release attaches an SBOM for the wheel's dependency closure and one
  for the image. [`docs/releasing.md`](docs/releasing.md) records what the version number
  promises and which surfaces it promises it about.
- **A compose file** for the three ways the tool is used in a container — one command at a
  time, as a live preview, and as the browser editor.
- **Windows and macOS support**, tested in CI. Graphviz installed without landing on `PATH`
  is found in the documented install locations, and `NETGRAPH_DOT` names the binary outright.

### Changed

- `netgraph validate` is about 3.1× faster on a 10 000-element inventory, and loading is
  about 1.4× faster; both were driven by a committed profiler rather than by guesswork
  (`tools/profile_validate.py`, `tools/bench_pipeline.py`).
- The `html` output no longer grows with the number of layers in it: the views share one
  document instead of each carrying a copy.
- The documentation was reorganised into a lean `README.md` and a navigable `docs/` set with
  one page per command; every flag table is generated from the CLI and every shell transcript
  is either executed by the test suite or marked with the reason it cannot be.

### Fixed

- Six loader and renderer defects found by property-based and fuzz testing, all of them cases
  where a hand-written but unusual document was mis-parsed or crashed rather than being
  reported: see `tests/test_properties.py` and `tests/test_fuzz_loader.py` for the
  regression examples.
- Mermaid front matter escaped `"` but not `\`, so a title containing a backslash produced a
  diagram Mermaid would not parse.

[Unreleased]: https://github.com/blechschmidt/netgraph/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/blechschmidt/netgraph/releases/tag/v0.1.0
