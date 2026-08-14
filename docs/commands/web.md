# `netgraph web`

`netgraph web` opens an inventory in the browser and draws it as you edit it,
with an info box on every node and link. It has two faces, and which one you get
is decided by what you point it at:

* **a folder** — an *editing session*. The server holds the loaded tree, the page
  lists its files and the documents in them, and with `--write` the browser can
  change them. This is the one to use for an inventory.
* **anything else** — a file, a pipe, or nothing — a *scratchpad*. One YAML
  document stream, held in the browser, rendered as you type, written nowhere.
  This is the one to use for a snippet or a paste.

## Synopsis

<!-- generated: synopsis web -->
```text
netgraph [GLOBAL OPTIONS] web [OPTIONS] [SOURCE]
```
<!-- /generated -->

![The netgraph web interface: the YAML document stream on the left, the rendered layer-2 diagram on the right, and the info box open on a switch showing its interfaces, addresses, VLANs and links](../images/web.png)

<sub>Hovering `sw-home` in [`examples/home-lab`](../../examples/home-lab): every
port, its addresses and VLAN mode, and what each one is cabled to.</sub>

## The editing session

<!-- norun: every one of these starts a server and never exits -->
```bash
netgraph web ./inventory                # read-only: browse the tree and its diagram
netgraph web ./inventory --write        # read-write: save, undo and redo from the page
```

The window is three panes. On the left is the **inventory**: every file the
loader would read, grouped by namespace, with the documents each one declares
listed under it by kind and name. In the middle is the **file being edited** and
the problems found in the tree. On the right is the **diagram**.

The panes are wired to each other, which is the point of the command:

* **Selecting a file opens it**, whole, in the editor.
* **Selecting a node in the diagram reveals the document that declares it** — the
  right file, scrolled and selected at the right line. The shape carries the
  element's address, the address is in the file list against a file and a line,
  and that mapping comes from the same load the diagram was built from.
* **Clicking a problem navigates to its file *and* line**, opening the file if it
  is not the one on screen.
* **A problem with a mechanical repair grows a `fix` button**, which applies it
  as one logged, undoable gesture. Where a rule admits two repairs there are two
  buttons and no default, because choosing between them is not the tool's to
  make. It is the same catalogue `netgraph validate --fix` uses, under the same
  gate: the repair is thrown away unless re-validating shows the finding gone and
  no rule reporting more than it did.
  [Fixing a finding](../validation-rules.md#fixing-a-finding) lists them.

**Every file's state is shown as it is, not as it would be convenient.** A file
you have typed into is `unsaved`. A file that changed on disk while you had
unsaved edits in it is `conflict`, and netgraph will not resolve that for you: it
says so and leaves both versions alone until you decide. A file that was deleted
underneath you says `deleted on disk`.

### Writing

`--write` is off by default, and it is refused unless the server is bound to
loopback — publishing an endpoint that changes your inventory is not something a
flag should let you do by accident. Without it every read works and every write
answers `403`.

With it:

* **Ctrl-S** writes the open file back. The write carries the content hash the
  file was read at; if the file moved underneath, the save is refused and shown
  as a conflict rather than overwriting the other edit. Saving again after that
  overwrites deliberately.
* **Ctrl-Z / Ctrl-Y** undo and redo. The stack lives **on the server**, so it
  survives a page reload and a second tab sees the same history. Each entry is
  the exact inverse the [mutation layer](../editing.md) produced, so an undo
  restores bytes — the comment style, the quoting and the reference spellings of
  every document a rename rewrote.
* **A change that would break the tree is refused**, listing the problems, and
  can then be written anyway. This is the same gate
  [`netgraph edit`](edit.md) applies: an inventory that already fails
  `validate` can still be edited, one that would gain a *new* error cannot
  without saying so.

Every write goes through `netgraph.edit` — the same typed, reversible,
comment-preserving operations [`netgraph edit`](edit.md) applies from the command
line. The server constructs no YAML of its own, so untouched lines are not
rewritten and a diff of an edit shows the edit.

### Reviewing what you changed

The **Changes** button opens a drawer listing every gesture the session made,
newest first. One entry per gesture rather than per operation: deleting a switch
is one line, not the five operations it became. Each carries

* **the YAML hunk it wrote**, as a unified diff with `a/`–`b/` prefixes, so it
  pastes into a patch file without editing;
* **its label**, which reveals the document it changed at its line — the same
  mapping a click on the diagram uses, run from the log instead;
* **Revert**, which puts that one change back.

**A revert is a new change, not a rewind.** It applies the gesture's own inverse
as a fresh edit, which is itself logged and itself undoable, so reverting the
third of ten gestures leaves the other nine alone. When one of those nine
depended on what the third one did, the revert is refused with the reason and
nothing is written — which is the honest outcome, and the one an undo stack
cannot give you.

Opening the drawer also **repaints the canvas as a diff**: added elements green,
removed ones red and dashed but still in place, changed ones amber with a badge
naming the fields that moved, everything untouched faded. The `since` menu
chooses what it is drawn against —

| | |
|---|---|
| `this session started` | the tree as this page first saw it. The default: "what have I done this afternoon" is the question, and neither git nor the undo stack answers it. |
| `git HEAD` | `HEAD` as the inventory root looks in it. Offered only when the root is in a repository, because an option that always fails is not an option. |

It is the same overlay [`netgraph diff`](diff.md) draws, from the same
changeset — the drawer and the diagram are two views of one answer.

**Copy commands** hands the whole session over as a list of
[`netgraph edit`](edit.md) invocations, in the order they happened, for a
pull-request description or somebody else's terminal. The rendering is never
lossy; see [`docs/editing.md`](../editing.md#as-a-script).

### The history timeline

The inventory is a folder of YAML in a repository, so its whole history is
renderable. **History**, above the canvas, opens a scrubber along the bottom of
it, over the commits [`netgraph log`](log.md) lists — and the diagram becomes
the diff each one carries against its parent as you step:

```text
 ▶  ◀◀  ────────────●───────  ▶▶   Now   ×
 e58b3c3a0  Add a lab switch   Scrubber · 2026-08-14   1 device added
```

Oldest on the left. Beside the control is the commit itself — the abbreviated
hash, its subject, its author and date, and the one-line summary of what it did
to the network — because a picture with a hash under it places nothing in time.

| Control | |
|---|---|
| the slider | Any commit in the range. It repaints as it is dragged; ground already covered comes back out of the cache. |
| `◀◀` / `▶▶` | One commit older or newer. `Alt-Left` and `Alt-Right`. |
| `▶` | Play: step forward by itself, about a frame a second, until the newest revision or until one that will not load. `Alt-P`. |
| **Now** | Leave the history and draw the working tree again. `Escape` does the same. |

**Each frame is arranged the way that revision arranged it.** Both sides of the
diff are read out of the commit, [layout document](layout.md) included, so a
diagram that was hand-placed in March is still hand-placed when you scrub back
to March — and one placed since is not retro-fitted onto a picture that never
had it.

The history and the [changes drawer](#reviewing-what-you-changed) are two
overlays on one canvas, so opening either puts the other away. The legend means
the same four things in both.

#### What it will not pretend

* **A revision whose inventory does not load** stops the playback and says which
  revision and why, in the place the summary would have gone. It is not skipped:
  a commit that broke the tree is the one worth stopping on.
* **A revision from before the inventory existed** is an empty network, not an
  error, so the commit that first added the folder reads as the whole network
  arriving — noted as such beside the summary.
* **A repository with more history than the bound** is truncated to the newest,
  and the bar says "the newest 100 of 312 revisions" rather than implying that
  is all there ever was. The bound is `[history] max-revisions` in
  `netgraph.toml`, 100 by default; see [`netgraph log`](log.md#the-bound), where
  an explicit range wider than it is refused outright rather than truncated.
* **A tree that is not in a repository** says so where the commit would be,
  rather than offering a control that does nothing.

#### What it costs

A frame is one inventory read, one changeset and one Graphviz layout — the same
work drawing the tree at all costs, plus the read and the diff. Two things keep
that interactive:

* **Rendered frames are cached by tree hash**, in pairs: the before tree and the
  after tree. Scrubbing back over ground already covered is a dictionary lookup,
  and so is a revert, a cherry-pick or a rebase that lands on a pair of trees
  already drawn.
* **Neighbouring revisions share their loaded state and their parsed files.**
  Stepping reads one revision rather than two, and the [parse
  cache](cache.md) means that read parses the files the commit touched rather
  than the two thousand it did not.

`tools/bench_history.py` measures it. On the 1056-device benchmark tree, where
one plain render of the working tree is about 1.5 s, a step is about 1.5× that
and a frame already drawn comes back in about 13 ms.

### Reconciliation

The session does not own the files. `watchfiles` watches the folder exactly as
[`netgraph watch`](watch.md) does, so an edit made in `$EDITOR`, a `git
checkout`, or a second netgraph process bumps the tree revision — and the page is
**told**, over a server-sent-events stream, the moment it happens. If the watch
cannot start, the command says so rather than leaving a page that is quietly
stale.

The event says *what* moved, which is what makes the page's response
proportionate to the change:

* a save of one file refetches **that file's row**, not the file list;
* a revision that does not change the drawing of the layer on screen **does not
  redraw it** — the page sends the fingerprint of the picture it is showing and
  the server answers "unchanged" rather than running Graphviz. Editing a
  description on a 1056-device tree went from 1.7 s to 185 ms; see
  [`docs/follow-ups.md`](../follow-ups.md) entry 18 for the harness and the
  numbers.

**The stream is an optimisation, and the page works without it.** A proxy that
buffers responses, a browser without `EventSource`, a stream that will not open:
any of them drops the page back to polling `/api/state` once a second, which
replays the very same events out of the server's ring buffer. The indicator above
the file list says which of the two you are on. Nothing is writable through the
stream and no write depends on having read one.

### More than one client

A session is shared — two tabs, or two people on the same machine — and it says
so rather than leaving them to collide:

* every connected page is listed above the file list, with what it is looking at
  and what it is editing in the tooltip;
* what somebody else has **selected** is drawn on the canvas as a faint dashed
  halo, distinct from the highlight your own hover gives;
* a file somebody else has **unsaved edits in** is badged `in use`.

**All of that is advisory.** It blocks nothing: the row still opens, the file
still saves, and presence expires by itself if a tab goes away without saying so.
The only things that can refuse a write are the ones that are checks on the tree
itself — the content hash of a whole-file save and the tree revision of an
operation batch. A soft lock built on a heartbeat would be a way to lock an
inventory by closing a laptop lid.

So the conflict story is unchanged by having company: two clients saving the same
file gives the second a `409` carrying what is really on disk; a save racing an
`$EDITOR` write gives the same; and an undo issued in one tab rewrites the files
and moves the buttons in the other, which is what a server-side history means.

### The API

The page is a client of a small JSON API, and so can anything else be. All of it
is on loopback and none of the write routes exist unless `--write` was given.

| Route | What it answers |
|---|---|
| `GET /api/bindings` | Every command the page has, its section, its keys, what it needs and what it does — plus the element kinds this build knows. Answered in both faces; it is `netgraph.web.bindings`, which is also what the table [below](#the-bindings) is generated from. |
| `GET /api/state` | The tree revision, whether this session writes, the undo/redo depth, and who else is connected. `?since=<id>` adds the events published after that id — the polling client's half of the push channel. `?client=<id>` keeps that client's presence alive. |
| `GET /api/events` | A `text/event-stream` of `tree-changed`, `file-changed`, `history-changed`, `disk-changed`, `presence`, opening with `hello` and beating every 15 s. Resumes from `Last-Event-ID`; a resume point older than the ring buffer opens with `resync`, meaning refetch. |
| `POST /api/presence` | `{"client": …, "selection": [ … ], "editing": [ … ]}` — what this client is looking at and has unsaved edits in; answers with everybody. `{"leaving": true}` drops it at once. Advisory, and the one route here that a read-only session still accepts, because it writes nothing. |
| `GET /api/tree` | Every file, its content hash, its documents, and each document's kind, name, address and line. `?path=a.yaml&path=b.yaml` answers for those files only, with `partial: true` and a `missing` list; `?diagnostics=0` leaves out the findings, which cost a validation of the whole tree either way. |
| `GET /api/graph?view=l2` | The resolved graph as an embeddable SVG, its info-box records, its problems and its stored [geometry](../editing.md). `graphHash` fingerprints the drawing; passing it back as `?known=` answers `unchanged: true` with no SVG when this revision would draw the same picture, having skipped the layout. |
| `GET /api/file/<path>` | One file's text and the hash a write of it must quote. |
| `PUT /api/file/<path>` | That file back: `{"text": …, "hash": …}`. A stale `hash` is `409`; a new error is `422`, listing them; `"force": true` overrides the second, never the first. |
| `POST /api/ops` | `{"revision": …, "ops": [ … ]}` — a batch of [edit operations](../editing.md), applied atomically. Answers with the applied operations, their inverses, the files changed and the tree's diagnostics. |
| `POST /api/undo`, `POST /api/redo` | Move the server-side history one step. |
| `GET /api/changes` | The session's log — one entry per gesture, with its hunk, the files and addresses it touched, and the `netgraph edit` lines that replay it — plus the whole session as one command list and the baselines this tree can be diffed against. |
| `GET /api/diff?against=session` | The same payload `/api/graph` answers, drawn as a diff, with `diff` holding the marks per node and edge and `diff.changeset` the whole [plan](plan.md). `against=git` compares with `HEAD`. |
| `GET /api/history` | The commits that changed this inventory, newest first, each with its hash, parents, author, date, subject and the hash of the inventory tree at it. `bound` is the ceiling, `total` how many there are and `truncated` whether the list is the newest of more. `?limit=` asks for fewer. |
| `GET /api/frame?rev=<commit>` | One of them, drawn as the diff against its parent: the `/api/diff` payload plus the commit's own facts and the one-line `summary` of what it did. `?known=` works as it does on `/api/graph`. A revision that will not load answers `200` with `status: failed` and the reason — it is a fact about the history, not a bad request. |
| `POST /api/revert` | `{"id": 3, "revision": …}` — put one logged gesture back. |
| `POST /api/fix` | `{"rule": "W138", "message": …, "fix": "prune", "revision": …}` — apply the mechanical repair for one diagnostic, as one gesture. The finding is named by rule and message, not by its place in the list, so a stale list is refused rather than misapplied. `fix` picks between the repairs a rule offers more than one of. |

`<path>` is relative to the inventory root and is checked, not normalised: an
absolute path, a `..`, a component the loader skips and a suffix that is not
YAML are each refused by name. No other request ever becomes a file name.

## The keyboard

**Everything this page does is reachable without a pointer**, and the page says
so out loud rather than making you find out. There are three ways in, and they
are three views of one table:

* **`Ctrl-K` — the command palette.** Every command below, fuzzy-matched by name,
  plus everything the page can *go to*: every element address in the diagram and
  every file path in the inventory. Each row prints its own shortcut, so the
  palette is also how the bindings are learnt. A command that cannot run now is
  shown greyed with the reason — "this session is read-only; restart it with
  `--write`" — rather than quietly missing.
* **`?` — the shortcut sheet.** The table below, in a dialog.
* **the keys themselves.**

A chord written `Ctrl-…` means the platform's command modifier: ⌘ on a Mac. A
single letter is a *canvas* gesture and fires only while the diagram has focus —
`n` creates a device there and types an `n` in the YAML pane, which is the
distinction the **Where** column makes.

### Driving the diagram

`Tab` reaches the canvas like any other control; the diagram is one stop on the
page's tab order and never a trap. From there the arrow keys walk it, preferring
the elements the focused one is **linked to**, so a path is followed rather than
a grid swept. `Enter` opens the inspector — and, in a session, the document that
declares the element, at its line. `n`, `c` and `Delete` are the create, connect
and delete gestures; each opens a small prompt whose element field is already
filled in with whatever is focused, so the same command works from the palette
with nothing focused at all.

Two rings, deliberately different: **focus** is a solid violet halo — where the
keyboard is — and **selection** is a long dash, with somebody else's selection a
short one. Three patterns, not three shades, so they are told apart without
colour.

### Routing a cable

A link is geometry as much as a node is
([`docs/schema.md` §18](../schema.md#18-layout-diagram-geometry)), and once the
view is arranged it can be routed here. Click a link to select it; **double-click**
the line to drop a bend where you clicked; drag a bend to move it; drag the
hollow **midpoint handle** to insert a bend and place it in one motion;
**right-click** a bend to remove it. A label the inventory has already pinned
gets a handle of its own, which slides it along the route and lifts it off.

Each of those is also a command, because a bend that can only be placed with a
mouse is a bend somebody working from the keyboard cannot place at all: `b` adds
one half way along the selected link, `Shift-B` straightens it, `r` sets its
routing style — spline, orthogonal or straight, on this link alone — and the
palette puts a moved label back on the line. On a view that is not arranged they
refuse with the fix, `netgraph layout --write`, rather than doing nothing: a
diagram Graphviz is still routing has nowhere to keep a bend.

Nothing here is a browser-side model of the arrangement. Letting go of a handle
posts one `set-link-geometry` operation
([`docs/editing.md`](../editing.md#the-operations)), the server rewrites the
`kind: layout` document through the same comment-preserving path
[`netgraph layout`](layout.md) uses, and the canvas repaints from the render
that follows — so what you see here is what `netgraph render` draws, and the
gesture is one entry in the changes drawer with a YAML hunk under it. The line
*under the cursor* is drawn by a port of netgraph's own router, which
`tests/test_browser.py` runs against the Python it mirrors on every CI run, so a
drag cannot land somewhere the render would not.

### Without a screen

The rendered SVG is inert by default: Graphviz emits shapes, not semantics. This
page adds them from the same records the info box is built from, so there is one
description of an element rather than two that drift:

* every node and link carries a role and an `aria-label` — *"sw-home, switch, 8
  interfaces, linked to rtr-edge on eth0"*;
* the canvas is an `application` that says which element is current, so arrowing
  around it is announced;
* **`Alt-4` opens the outline** — the whole view as a list, one line per element,
  which is both the screen-reader fallback and the fastest way to find something
  by name. It is off screen until focused and a real panel once it is;
* every applied, refused and reverted gesture is announced in a live region,
  once. A refusal interrupts; everything else is polite.

The interface follows `prefers-color-scheme` with a palette per scheme — every
colour clears 4.5:1 against its own background, which a single palette used on
both cannot — and honours `prefers-reduced-motion`. Where a colour carries
meaning it is never alone: the diff legend prints `+`, `~` and `−`, the same
sigils the diagram draws, and the three diff line styles differ as well as the
three hues.

`tests/test_browser.py` runs axe-core over the page on every CI run and fails on
a new WCAG 2.1 AA violation, and drives one end-to-end test — create a device,
cable it, undo both — without dispatching a single mouse event.

### The bindings

<!-- generated: keybindings -->
**Everywhere**

| Keys | Command | Where | Needs | What it does |
|---|---|---|---|---|
| `Ctrl-K` / `Ctrl-Shift-P` | Command palette | anywhere | — | Every command on this page, searched by name — and every element address and file path in the inventory, so one field is also 'go to'. |
| `?` / `F1` | Keyboard shortcuts | anywhere | — | This table, rendered from the bindings the page actually registered. |
| `Escape` | Close what is open | anywhere | — | The palette, the reference, a prompt, the changes drawer, the inspector — in that order. |
| `Alt-1` | Focus the inventory list | anywhere | a folder | The file list. Arrow keys move down it; Enter opens a file. |
| `Alt-2` | Focus the YAML pane | anywhere | — | The text of the open document. Escape leaves it again. |
| `Alt-3` | Focus the diagram | anywhere | — | Puts a focus ring on an element and turns on the gestures below. |
| `Alt-4` | Focus the diagram outline | anywhere | — | The diagram as a list a screen reader can read straight through: one line per element, with what it is linked to. |
| `Ctrl-Enter` | Render now | anywhere | — | Draw the diagram again without waiting for the editor to settle. |
| `Ctrl-Shift-Enter` | Validate the inventory | anywhere | — | Re-run the checks and move focus to the problems list. |

**Moving around**

| Keys | Command | Where | Needs | What it does |
|---|---|---|---|---|
| `ArrowRight` / `ArrowLeft` / `ArrowUp` / `ArrowDown` | Move to the adjacent element | the diagram | — | Steps to the nearest element in that direction, preferring one this element is linked to — so a whole path can be walked with one hand. |
| `l` / `Shift-L` | Cycle this element's links | the diagram | — | Focuses each cable or tunnel that terminates here in turn, so a link can be inspected or removed without a pointer. Tab is left alone: the diagram is one stop on the page's tab order, never a trap. |
| `Home` | First element | the diagram | — | The first element of the outline, which is the diagram in reading order. |
| `End` | Last element | the diagram | — | The last element of the outline. |
| `Enter` | Open the inspector | the diagram | a focused element | Everything known about the focused element, and — in a session — the document that declares it, opened at its line. |
| `Space` | Pin the inspector | the diagram | a focused element | Keeps the inspector up, and tells the other tabs what this one is looking at. |
| `Ctrl-G` | Go to element… | anywhere | — | The palette, opened over element addresses alone. |
| `Ctrl-O` | Open file… | anywhere | a folder | The palette, opened over the inventory's file paths alone. |

**Editing the inventory**

| Keys | Command | Where | Needs | What it does |
|---|---|---|---|---|
| `n` | Create an element… | the diagram | `--write` | Asks for a kind and a name, and writes the document. 'netgraph edit create'. |
| `c` | Connect this element… | the diagram | `--write` | Cables the focused element to another, port to port. 'netgraph edit connect'. |
| `Delete` / `Backspace` | Delete the focused element | the diagram | `--write` | Removes the element, or the cable when a link is focused. Asks first. 'netgraph edit delete' / 'disconnect'. |
| `F2` | Rename the focused element… | the diagram | `--write` | Renames it and every reference to it. 'netgraph edit rename'. |
| `e` | Set a field… | the diagram | `--write` | A dotted path and a YAML value, on the focused element. 'netgraph edit set'. |
| *palette only* | Remove a field… | anywhere | `--write` | 'netgraph edit unset'. |
| *palette only* | Move to another file… | anywhere | `--write` | Moves the element's document into a different file. 'netgraph edit move'. |
| *palette only* | Disconnect a cable… | anywhere | `--write` | Removes a cable, leaving both devices. 'netgraph edit disconnect'. |
| `b` | Add a bend to the focused link | the diagram | `--write` | Drops a waypoint half way along the link, which the route then passes through. Double-clicking the line does the same at the point clicked. |
| `Shift-B` | Straighten the focused link | the diagram | `--write` | Clears every bend, leaving the link to run directly between its two devices. The routing style and the label position are kept. |
| `r` | Change how the link is routed… | the diagram | `--write` | Spline, orthogonal or straight, on this link alone. Clearing it takes the view's default back. Honoured by 'netgraph render' as well as here. |
| *palette only* | Put the link's label back on the line | anywhere | `--write` | Undoes a nudged label, leaving it half way along the route where the renderer puts one nobody has moved. |
| `i` | Add an interface… | the diagram | `--write` | 'netgraph edit add-interface'. |
| *palette only* | Remove an interface… | anywhere | `--write` | 'netgraph edit remove-interface'. |

**The view**

| Keys | Command | Where | Needs | What it does |
|---|---|---|---|---|
| *palette only* | Switch layer… | anywhere | — | Physical, l1, l2, l3, overlay, routing, rack, power, identity. |
| `]` | Next layer | anywhere | — | The next entry of the layer menu. |
| `[` | Previous layer | anywhere | — | The previous entry of the layer menu. |
| `Alt-I` | Toggle IP addresses | anywhere | — | Whether the picture prints addresses. The inspector shows them either way. |
| `Alt-V` | Toggle VLANs | anywhere | — | Whether the picture prints VLAN membership. |
| `Alt-G` | Toggle namespace grouping | anywhere | — | Collapse each namespace into one box. |
| `Alt-S` | Toggle strict | anywhere | — | Report warnings as errors. |
| *palette only* | Filter by VLAN… | anywhere | — | Keep only elements participating in the VLANs given. |
| `0` | Fit the diagram | anywhere | — | Undo the panning and zooming. |
| `Plus` / `=` | Zoom in | anywhere | — | Around the middle of the canvas, so nothing jumps off screen. |
| `Minus` | Zoom out | anywhere | — | Around the middle of the canvas. |

**Files and history**

| Keys | Command | Where | Needs | What it does |
|---|---|---|---|---|
| `Ctrl-S` | Save the open file | anywhere | `--write` | Writes it back, stating the hash it was opened at. |
| `Ctrl-Z` | Undo | anywhere | `--write` | The session's stack, not the browser's: it puts files back on disk. |
| `Ctrl-Shift-Z` / `Ctrl-Y` | Redo | anywhere | `--write` | Applies the last undone change again. |
| `Ctrl-B` | Changes drawer | anywhere | a folder | This session's changes, and the diagram repainted as the diff they add up to. |
| *palette only* | Copy the equivalent commands | anywhere | a folder | The session as a 'netgraph edit' script somebody else can review or run. |
| `Ctrl-Shift-H` | History timeline | anywhere | a folder | A scrubber over the commits that changed this inventory. The diagram becomes the diff the selected commit carries against its parent, arranged as that revision arranged it. |
| `Alt-ArrowLeft` | Older revision | anywhere | a folder | One commit back along the timeline. Stops the playback if it is running. |
| `Alt-ArrowRight` | Newer revision | anywhere | a folder | One commit forward along the timeline. |
| `Alt-P` | Play the history | anywhere | a folder | Step through the range by itself, a frame at a time, until the newest revision or until one that will not load. |
<!-- /generated -->

## The scratchpad

<!-- norun: every one of these starts a server and never exits -->
```bash
netgraph web                                  # opens on the netgraph init example
netgraph web devices/sw-office.yaml           # seeded from one file
kubectl get cm topology -o jsonpath={..yaml} | netgraph web   # or from a pipe
```

The left pane holds a document stream — one or more documents separated by `---`
— and re-renders about half a second after you stop typing. **Nothing is
written.** The seed is read once, at startup; after that the stream lives in the
browser and every pass happens in memory, so the command cannot damage the file
it was seeded from and equally will not save your work: copy the text out before
you close the tab.

A stream has no folders and therefore **no namespaces**, and no file to write
back to. That is why `--write` is refused here and why a folder opens a session
instead.

## Both faces

**Hovering a node or a link opens an info box** holding what the diagram has no
room for: every interface with its type, MAC, MTU, addresses and VLAN mode; every
link that terminates on the element, what it runs to and over which port; and, at
layer 3, the prefix a subnet node stands for and who is addressed in it.
Everything it shows is the same data `netgraph render -f json` exports — the
records *are* that export — so the two cannot drift apart, and they are the same
records a committed SVG carries as tooltips
([`docs/rendering.md`](../rendering.md)). The element under the pointer and
everything it touches are lifted out of the diagram while the box is open; click
to pin the box, click again or press `Esc` to let go.

Beyond that: the layer, the VLAN filter and the display toggles are in the header
and apply on the next render; the canvas pans with a drag and zooms with the
wheel; and the splitter between the panes moves.

**Broken text still draws.** `netgraph render` refuses an inventory with errors
unless `--force`, because a diagram that disagrees with the files misinforms
whoever is shown it. Here the diagram *is* the feedback and text being edited is
wrong most of the time, so every problem is listed with its file and line and
whatever resolved is drawn anyway.

`netgraph.toml` decides how this machine *draws*: the `[render]` table and
`--profile` of the inventory named by `-i` — the current directory by default —
supply the settings this command has, `--icons` above all. A session also reads
the `[validate]` table of the folder it has open, so the problems it lists are
the ones [`netgraph validate`](validate.md) would list in that tree; a stream has
no folder of its own to look in and uses the built-in defaults plus the `strict`
toggle in the header.

`--icons` is chosen on the command line rather than in the browser for the same
reason `--write` is: it may name a directory on this machine, and a page has no
business reading one.

## The server

The same restrictions apply as to the [`watch` preview](watch.md) — loopback by
default, so publishing the interface with `--host` is an explicit act and is
warned about; a fixed set of routes; no request path ever turned into a file name;
a `Host` header check that keeps a loopback bind from being reached through a
rebound DNS name — plus three of its own: a request body is capped at 1 MB, the
SVG is parsed and stripped of anything that could execute or navigate before it is
put into the page, and no write route exists at all without `--write` on a
loopback bind. It is a development server: do not put it on a hostile network.

### The event stream and presence, against the same threat model

Both are new surface, and both are held to the rules above rather than excused
from them.

* **Same bind, same `Host` check.** `/api/events` and `/api/presence` are
  ordinary routes on the same handler, so a request that reached a loopback bind
  under another name is refused with `421` before either is entered. A page on a
  hostile origin therefore cannot open the stream and read your topology out of
  it — which matters more here than elsewhere, because a stream keeps delivering.
  `connect-src 'self'` in the [Content-Security-Policy](../architecture.md) says
  the same thing from the other side.
* **The stream is read-only, and so is presence.** Nothing about the inventory
  can be changed through either. `/api/presence` writes to an in-memory list and
  is the one route a read-only session still accepts, because "who else is
  looking" is useful to two people browsing and touches no file. On a bind you
  published with `--host`, anyone who can reach it can read the stream and add
  themselves to that list — which is a nuisance rather than a compromise, and
  one more reason publishing is an explicit act.
* **A client id is a name, never a permission.** Ids are issued by the server and
  a request that invents one gets a fresh identity rather than somebody else's
  entry. The id travels with a write only so the page can recognise its own
  change in the events and skip a reload; nothing is authorised by it, and a
  request that claims another client's id can do nothing this one could not.
* **Neither is unbounded.** At most 32 streams and 64 clients per session, at
  most 64 selected addresses and dirty paths per client, a 256-event ring buffer,
  and a subscription that falls 64 events behind is resynchronised rather than
  buffered. A page in a reload loop cannot make the process grow, and a stalled
  tab cannot make it grow on that tab's behalf.
* **Presence expires.** Entries go after 45 s of silence. It is deliberately not
  a lock: a lock that a heartbeat can hold is a way to lock an inventory by
  closing a laptop lid, and everything that can actually refuse a write — the
  content hash, the tree revision — is a check on the tree rather than on a
  claim.
* **Nothing new becomes a file name.** `?path=` on `/api/tree` goes through the
  same check as `/api/file/<path>`: relative, below the root, no component the
  loader skips, YAML suffix. It is a second door into the same room and it has
  the same lock.
* **Nothing becomes a git *option*.** `?rev=` on `/api/frame` reaches a `git`
  argument list, and `git log --output=<file>` writes a file while
  `--upload-pack=<cmd>` runs a program. A revision that begins with `-` is
  refused by name before any git process starts, on every path that takes one —
  the route, the timeline, `netgraph log` and `netgraph diff --from` alike.

The default port is 8081, one above the `watch` preview's, so a watch run and an
editing session can be open at the same time. `--port 0` lets the operating system
choose one instead. `--open` — on by default — points the default browser at the
page once the server is listening; `--no-open` prints the URL and leaves the
browser alone, which is what you want over SSH.

## Arguments

<!-- generated: arguments web -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[SOURCE]` | no | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options web -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--host` | `ADDRESS` | `127.0.0.1` | Address to bind. The default keeps the interface on this machine; an inventory describes internal topology, so publishing it is an explicit act. |
| `--port` | `INTEGER, 0-65535` | `8081` | Port to bind. 0 lets the operating system choose one. |
| `--open`, `--no-open` | — | `--open` | Open the interface in the default browser once it is listening. |
| `--icons` | `THEME\|DIR` | — | Draw each element as an icon instead of a plain shape. Built in: cisco, none. Chosen here rather than in the browser, because it names a directory on this machine. |
| `--write`, `--read-only` | — | --read-only | Let the browser change the inventory. Only for a SOURCE folder, only on a loopback bind, and never by default: an editor that can write is a decision. |
| `--profile` | `NAME` | — | Apply the [profile.NAME] block of netgraph.toml on top of its [render] table. Explicit flags still win over both. |
| `--show-config` | — | off | Print the settings this invocation resolves to, and where each one came from, then exit without doing any work. |
<!-- /generated -->

## Exit codes

The interface is ended by Ctrl-C, which is how the command is meant to finish.
Text that does not parse is reported in the page, not by the process.

| Code | Meaning |
|---|---|
| 0 | The server ran and was stopped with Ctrl-C. |
| 2 | Usage error, an unusable `netgraph.toml`, or `--write` where it cannot be given. |
| 3 | A `SOURCE` folder could not be read. |
| 6 | The address could not be bound — usually something else on port 8081. |

## See also

* [`netgraph edit`](edit.md) — the same operations from the command line, and the
  layer every write in the browser goes through.
* [`netgraph diff`](diff.md) — the same overlay from the command line, over two
  folders, a git ref or a saved plan.
* [`netgraph watch`](watch.md) — the same live diagram without an editor, for a
  second screen.
* [`docs/editing.md`](../editing.md) — what an operation is, what an inverse
  promises, and how geometry is stored.
* [`docs/rendering.md`](../rendering.md) — the layers and display options the
  header exposes, and the tooltips the info box shares its records with.
* [`docs/inventory-layout.md`](../inventory-layout.md) — why a folder tree means
  namespaces and a stream does not.
* [`docs/validation.md`](../validation.md) — the problems the middle pane lists.
* [`docs/testing.md`](../testing.md#the-browser-layer) — the headless-browser
  suite that drives this page, and how to run it.
