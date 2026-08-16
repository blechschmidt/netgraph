# Editing an inventory

netviz reads a folder of YAML and draws a network. This page is about the
other direction: how it *writes* that folder, and why writing it is a harder
problem than reading it.

Everything netviz is growing towards — a diagram you can drag things around in,
an undo stack, a `plan`/`apply` pair that shows a changeset before it lands —
needs one thing first: a way to change the files that is as safe and as lossless
as the way it reads them. That way is `netviz.edit`, and
[`netviz edit`](commands/edit.md) is its command-line face.

[`netviz apply`](commands/apply.md) is the first of those callers to arrive: it
takes a changeset computed by [`netviz plan`](commands/plan.md) and turns each
entry into the operations described here, which is why a plan applied to a tree
leaves every comment in it alone.

## Contents

- [The problem](#the-problem)
- [The operations](#the-operations)
- [What an operation guarantees](#what-an-operation-guarantees)
- [Inverses](#inverses)
- [References](#references)
- [Placement](#placement)
- [The two gates](#the-two-gates)
- [Batches: many operations, one change](#batches-many-operations-one-change)
- [Copying, cutting and pasting](#copying-cutting-and-pasting)
- [Arranging a selection](#arranging-a-selection)
- [Containers: dragging a document into a namespace](#containers-dragging-a-document-into-a-namespace)
- [Annotating a diagram](#annotating-a-diagram)
- [Using it from Python](#using-it-from-python)
- [Reviewing what you changed](#reviewing-what-you-changed)
- [What it deliberately does not do](#what-it-deliberately-does-not-do)

---

## The problem

A YAML document is not the data it parses to. It also holds the comment above
the VLAN block explaining why that VLAN exists, the blank line separating two
racks, the fact that somebody wrote the MAC address in single quotes and the
prefix without them, and the order the keys happen to be in. None of that
survives a parse-and-dump cycle, and all of it is the reason anybody can read
the file a year later.

So a write path that loads the models and dumps them back is not an editor, it
is a reformatter that happens to change one value. Three things follow, and they
are the whole design:

1. **Reading and writing use different parsers.** Meaning comes from the strict
   loader and the pydantic models, because that is where the schema lives.
   Change is made through `ruamel.yaml`'s round-trip parser, because that is
   where the comments live.
2. **A document nobody named is never re-emitted.** A file is held as a preamble
   plus one verbatim text per document; rendering it concatenates them. Only a
   document an operation actually changed is dumped again.
3. **Everything is expressed as an operation.** Not as "here is the new text of
   this file" — as "rename this element", which is a thing that can be checked,
   logged, sent over a socket, refused, and undone.

## The operations

There are nineteen. Seventeen are **semantic** — the vocabulary a person or a
diagram uses:

| Operation | JSON `op` | What it does |
|---|---|---|
| `CreateElement(kind, name, namespace, spec, metadata, file)` | `create` | Adds a document declaring a new element. |
| `CopyElement(address, name, namespace, suffix, keep_unique, rewrite, file)` | `copy` | Writes a second element built from an existing one; see [copying](#copying-cutting-and-pasting). |
| `DeleteElement(address, cascade)` | `delete` | Removes it, and the file if it was the last document in it; `cascade` takes what [cannot outlive it](#deleting-asks-first). |
| `RenameElement(address, new_name)` | `rename` | Changes `metadata.name`, every reference to it, the geometry that placed it and the annotations about it. |
| `MoveElement(address, file, index)` | `move` | Moves the document, verbatim, possibly to another namespace. |
| `SetField(address, path, value)` | `set` | Writes a value at a field path. |
| `UnsetField(address, path)` | `unset` | Removes it. |
| `AppendItem(address, path, value, index)` | `append` | Adds one entry to a sequence, creating the sequence if it is absent. |
| `AddInterface(address, interface, index)` | `add-interface` | Appends to `spec.interfaces`. |
| `RemoveInterface(address, name, cascade)` | `remove-interface` | Removes a port, and what terminated on it. |
| `Connect(a, b, spec, name, namespace, file)` | `connect` | Creates a cable between two interfaces. |
| `Disconnect(address, cascade)` | `disconnect` | Removes a cable, and the geometry that routed it. |
| `SetGeometry(view, nodes, edges, groups, routing, layout, namespace, file)` | `set-geometry` | Writes one view of a [`kind: layout`](schema.md#18-layout-diagram-geometry) document. |
| `SetLinkGeometry(view, link, waypoints, routing, label, layout, namespace, file)` | `set-link-geometry` | Writes one *link's* geometry into that document: its bends, its routing style and where its label sits. |
| `CreateAnnotation(kind, name, namespace, spec, metadata, file)` | `create-annotation` | Adds a `note`, `area` or `legend` document ([§21](schema.md#21-diagram-annotations-notes-areas-and-legends)). |
| `DeleteAnnotation(kind, name, namespace)` | `delete-annotation` | Removes one, and the file if it was the last document in it. |
| `SetAnnotation(kind, name, namespace, path, value, unset)` | `set-annotation` | Sets — or removes — one of its fields. |

Two are **primitive**, and file-level:

| Operation | JSON `op` | What it does |
|---|---|---|
| `WriteFile(path, text)` | `write-file` | Replaces a whole file with the given text. |
| `RemoveFile(path)` | `remove-file` | Deletes a whole file. |

The primitives exist for one reason, [inverses](#inverses), and are not something
to reach for by hand: a log of them is a log of diffs rather than of intentions.
They go through the same gates as everything else.

`SetGeometry` is the odd one out, and deliberately so. It takes a whole view at
a time rather than a coordinate at a time, because that is the unit an
arrangement is decided in: an auto-layout produces every position at once, and a
hundred `set` operations would be a hundred trips through the round-trip parser
for one user gesture. Each section it names is *replaced* and each section it
leaves out is *left alone*, and the replacement is a keyed merge — an entry that
survives keeps the comment somebody wrote above it. It is the operation
[`netviz layout`](commands/layout.md) writes through, and the one a canvas will
write through when a node is dragged.

`SetLinkGeometry` is the opposite unit for the opposite reason. A whole view is
what an *automatic layout* decides; a route is what a *hand* decides, one cable
at a time, and dragging a bend must not have to send — and so must not be able
to clobber — the coordinates of everything else in the diagram. Two people
arranging one diagram is the case that makes that load-bearing. The entry is
**replaced** rather than merged, so straightening a cable is this operation with
no waypoints, and a link left with nothing pinned at all has its entry removed
rather than left saying `{}`. It is what every link-routing gesture in
[`netviz web`](commands/web.md#the-keyboard) ends in — dropping a bend,
dragging one, setting a routing style, putting a nudged label back on the line —
and [`docs/rendering.md`](rendering.md#links-are-geometry-too) is what the
stored result draws as.

`AppendItem` is the general form of the gap `AddInterface` names: a sequence
entry cannot be written at a path that does not exist yet, so `set` cannot add
one, and replacing the whole list to add an entry would rewrite the comments
beside the entries that were already there. It is what a repair reaches for when
it has to extend a device's VLAN database, and `AddInterface` stays as it is
because a port also has to be *placed*, and a duplicate name has to mean
something.

The three annotation operations are separate from the element ones for a reason
that is about the *name space* rather than about the shape of a document. An
annotation is a sidecar: a note called `core` may sit beside a switch called
`core`, so a create that carried only a name could not say which of the two it
meant — and one that went through the element path would refuse the note because
the switch already has the name. `DeleteAnnotation` has no `cascade` and never
will: nothing in an inventory refers to an annotation, so nothing can be orphaned
by removing one. `SetAnnotation` is the operation a dragged note is made of, and
the one rule worth knowing about it is written up under
[annotating a diagram](#annotating-a-diagram).

The set is deliberately closed. A twentieth kind of change is a twentieth
operation, defined here, and not a caller reaching for the file system.

## What an operation guarantees

**Only the intended hunk changes.** The tests assert this by diffing every file
an operation touched and requiring the diff to be the change and nothing else.
A document nobody edited comes back as the exact bytes it was read as, byte-order
mark and CRLF line endings included. Inside a document that *was* edited,
`ruamel` keeps the comments, blank lines, quoting and key order, and the emitter
is [probed](#a-note-on-indent-probing) against the source so that its indent
style matches the file rather than netviz's preference.

**A canonical file stays canonical.** If a document was already in the form
[`netviz fmt`](commands/fmt.md) writes, the edited version is put back through
the formatter — so a key added to a mapping lands where the schema order puts it
rather than at the end. If the document was *not* canonical, it is left alone:
reformatting it would bury the edit under a diff nobody asked for.

**Nothing is half-applied.** An operation that refuses — a delete that would
dangle a cable, a path that names no field — leaves the tree exactly as it was.
Refusals happen before any file is written, and the in-memory tree is rolled back
to the state the operation found.

### A note on indent probing

`ruamel` has to be told how a file indents its sequences before it can reproduce
them. Rather than guess, `netviz.edit.roundtrip` dumps the *unmodified*
document with each candidate style and keeps the first that reproduces the source
byte for byte. The canonical form and the other common style are both matched. A
document no candidate reproduces is still editable — the edit lands and is
correct — but its diff is wider than one hunk, which is one of the arguments for
running `netviz fmt` over a tree once and then leaving it alone.

## Inverses

Applying an operation returns the operations that undo it. That is what makes an
undo stack a list and undo a loop:

```python
applied = session.apply(RenameElement(address="sw-old", new_name="sw-new"))
session.apply_all(applied.inverse)  # back where we started, byte for byte
```

The inverse is **exact**: after applying it, the tree is the tree you started
with, comment for comment. That is a stronger promise than "equivalent", and it
is the one that matters — an undo that quietly restyled four files would be worse
than no undo.

Exactness is why some inverses are not the operation you would expect. A
semantic inverse is returned only when it cannot disturb a byte the operation did
not write:

| Operation | Inverse |
|---|---|
| `create` | `delete` — the document did not exist, so removing it leaves the file as it was |
| `connect` | `disconnect`, for the same reason |
| `move` within a namespace | `move` back to the index it came from, the document travelling as text — unless the move emptied its source file |
| `set` of a field that was absent | `unset` — the key was not there, and had no comment |
| `add-interface` | `remove-interface` |
| `append` | `unset` of the position it was inserted at — or of the key, when the sequence had to be created |

`move` carries a condition of its own. A move that takes the last document out of
a file deletes the file, so the inverse has to *make* it again — and a file
netviz makes is plain UTF-8 with `\n` line endings that starts at its first
document. A CRLF checkout, a byte-order mark or a licence header above the first
`---` is none of those, so a move that emptied such a file is inverted with the
pre-images instead. (This is not hypothetical: it is what made the property tests
fail on Windows and nowhere else.)

The last two carry a condition too. They edit a document *in place*, so they may
only claim a semantic inverse when re-emitting that document reproduces it exactly —
the [indent probe](#a-note-on-indent-probing) succeeded and no scalar changed
spelling. When it does not hold, applying the operation has already rewritten
lines nobody touched, and the opposite operation would rewrite them again rather
than put them back; only the pre-images can.

Everything else is inverted with `write-file` operations carrying the text each
touched file had before. That is not a shortcut past the typed model, it is the
only honest answer: undoing a rename means restoring the *spelling* of every
reference that was rewritten, and undoing an `unset` means restoring the comment
that sat above the key. No semantic operation carries either, and one that
pretended to would be an undo that sometimes lost a comment — which is exactly
the failure this whole layer exists to prevent.

## References

A rename is not a rename until every reference to the old name names the new one,
and a delete is not safe until the tree has been asked what would be left
dangling. There are six places one document names another, and they are read off
the *models* rather than found by looking for colons in strings:

| Field | What it points at |
|---|---|
| `spec.endpoints[]` | a cable or tunnel end, `device:interface` ([§7](schema.md#7-cables), [§14.3](schema.md#143-semantics)) |
| `spec.over` | the tunnel a tunnel runs inside ([§14](schema.md#14-tunnels)) |
| `spec.upstream.attached_to` | the host an adapter is plugged into ([§8](schema.md#8-adapters)) |
| `spec.power.inputs[]` | the outlet feeding a power supply, `pdu:outlet` ([§17](schema.md#17-power)) |
| `spec.members[]` | a user or a nested group in a group ([§19.2](schema.md#192-group)) |
| `spec.from` | the template a device inherits ([§6.6](schema.md#66-template--reusable-partial-device-specs)) |

### Renaming keeps the spelling its author chose

`sw-home` and `switches/sw-home` may name the same switch, and which one a
document wrote was a choice. netviz keeps it: a short name stays short where a
short name still resolves to the right element, and a qualified name stays
qualified in the same relative-or-absolute shape. Only when the author's form
would now resolve to something else — or to nothing — is it escalated to the
fully-qualified name, which always resolves.

The same machinery runs when a `move` changes an element's namespace, in both
directions: the references *to* the moved element are re-spelled, and the
references the moved document *makes* are re-spelled too, because a plain name
resolves outwards from the folder its document sits in and a document that
changes folders can otherwise silently start naming something else.

### The rename reaches the drawing too

A reference is not the only place a name is written down. Two more are, and both
of them are keys rather than values, which is why they went unnoticed for
longer:

**Geometry (§18).** A layout document places a node under a key that *is* the
element's address — including the derived ids §18 allows, `adp-usb-eth#upstream`
for an adapter's attachment and `tunnel:sites/hq/vx-100` for a tunnel drawn as a
box. The key moves with the name, in every view of every layout document, so an
element arranged on the L1 and the L2 diagram keeps both arrangements. A rename
that left the key behind produced a
[`W138`](validation-rules.md#w138--stale-diagram-geometry) and an arrangement
lost silently: the element was redrawn wherever the engine put it, and `netviz
layout --prune` then dropped the coordinates rather than moving them.

**Annotations (§21).** A note's `spec.anchor` and an area's `spec.members[]`
name elements, and a stale one is
[`W142`](validation-rules.md#w142--annotation-about-something-that-is-gone).
Both are re-spelled by the rule above, so a member list written short stays
short.

An area's `selector` is deliberately left alone. It names a *pattern* rather than
an element, and netviz cannot tell whether the pattern was meant to match the
old name or merely happened to — rewriting one would be guessing.

### Deleting asks first

Deleting a device with cables on it is refused, and the cables are named:

<!-- norun: writes to the working tree -->
```console
$ netviz -i examples/home-lab edit delete sw-home
error: switches/sw-home is referred to by cables/cbl-rtr-sw, cables/cbl-sw-ap, …
```

`--cascade` accepts. It is not a blunt instrument: a reference is either
**structural**, meaning the referring element cannot exist without its target —
a cable end, a tunnel's `over` — or optional. Structural references take the
referring element with them, transitively, so deleting a device deletes its
cables and deleting a tunnel deletes the tunnels stacked on it. Optional ones —
an adapter's `attached_to`, a power input, a group membership — are cleared, and
the referring element survives. Deleting somebody who has left therefore empties
their memberships rather than taking the groups with them, which is the right way
round; marking the account `status: departed` instead keeps the memberships
*visible* until they have been revoked
([`W140`](validation-rules.md#w140--departed-user-still-in-a-group)).

Clearing a reference tidies up what it makes untrue as well as what it leaves
empty: dropping one of two power inputs also drops `redundant: true`, because
that flag claims the device survives losing a feed and one feed does not
([`E042`](validation-rules.md#e042--redundant-power-that-is-not-redundant)) — and
since that is a *load* error, leaving it
behind would take the device out of the inventory and dangle every cable on it.

### The delete reaches the drawing too

An element is drawn as well as declared, and both go:

**Annotations (§21)** follow the same structural-or-optional rule, decided by
§21's own coherence checks rather than by a table. A note anchored to the deleted
element and *placed* somewhere keeps its text and loses its anchor; a note that
is only anchored cannot be drawn at all without it and is a dependent, named in
the refusal and removed by `--cascade`. An area drops the doomed members, and
goes only if that would leave it with no members, no selector and no rectangle.
The rule in one line: **an annotation is removed exactly when clearing its
references would leave a document the loader refuses.**

**Geometry (§18) is never a dependency.** The positions, waypoints and group
boxes that placed what is being deleted are dropped without being asked about,
because coordinates for something that is gone are not a claim about the network
— they are litter, and the diagnostic for them
([`W138`](validation-rules.md#w138--stale-diagram-geometry)) exists precisely
because deletes used to leave them. A `netviz edit delete` of one switch used
to hand back a tree with
a warning per cable it took; it now hands back the tree it found, minus the
switch. A section, a view, a document and a file left empty by the last entry are
each dropped in turn.

This is deliberately *not* `netviz layout --prune`, which drops every key the
current drawing lacks — including the position of a device merely filtered out of
the view. A cascade removes the geometry of what it is itself removing. Derived
keys spelled `subnet:` and `rack:` are left alone for the same reason: those nodes
exist because of what the *surviving* elements say, and whether the last one has
gone is a question only the layout engine can answer.

## Placement

New documents are placed rather than dumped. The rules, which follow
[`docs/inventory-layout.md`](inventory-layout.md):

1. An explicit `file` wins, after checking that it is a YAML file inside the
   inventory that the loader would read, and that its folder is the namespace
   asked for — because a document's folder *is* its namespace, so the two cannot
   disagree.
2. Otherwise, join the file that already holds elements of that kind in that
   namespace — unless that file is named after the single element it holds,
   which is the layout's marker for "this device owns a file and a `git log`".
3. Otherwise, a new file: `cables.yaml` or `tunnels.yaml` for a link, because a
   link is meaningless without the two things it joins and belongs in a patch
   record; `<name>.yaml` for everything else.

Deletion is the mirror: the last document leaving a file takes the file, and the
last file leaving a folder takes the folder — an empty folder is an empty
namespace, which nothing can be put in and every listing has to skip.

## The two gates

**Validation.** Before anything is written the tree is loaded *as it would be*,
through an in-memory overlay, and validated. The comparison is against the same
tree loaded as it is, per rule and by count. An inventory that already has three
`W103` warnings can still be edited; one that would gain a fourth *error* cannot,
without `--force`. Warnings never block a write — an edit that adds one is
usually an edit somebody means to make, and a gate people routinely force past is
worse than no gate. Absolute cleanliness is not the bar, because an inventory
that fails `validate` is exactly when an editor is most needed; not making it
worse is.

**Conflict.** Every file the session reads carries the SHA-256 of the bytes it
was read as, and each is checked again immediately before the write. A file that
moved in between — your editor, a `git checkout`, a second netviz — is a typed
`ConflictError` and the edit is dropped. `--force` does not skip this: it
overrides a judgement about *your* change, not a fact about somebody else's.

Writes themselves go through a temporary file and a rename, so a reader sees the
old file or the new one and never half of either. There is no cross-file
transaction — a plain filesystem does not offer one — so a write that fails
part-way through a multi-file change says which files it had already written.

## Batches: many operations, one change

An operation is atomic: an applier that refuses leaves the tree exactly as it
found it, down to the byte. That is the right grain for one edit and the wrong
grain for a *gesture*. Selecting eleven switches in the editor and pressing
Delete is eleven operations that mean one thing, and if the seventh cannot go —
something still refers to it, a file moved on disk — the honest outcome is that
none of them went. Six deleted devices and an error message is a state nobody
asked for and nobody can undo in one step.

`Batch` is that grain:

```python
from netviz.edit import Batch, DeleteElement, EditSession

session = EditSession(root=Path("inventory"))
batch = Batch(session, label="retire the old access layer")
batch.add(DeleteElement(address=address, cascade=True) for address in doomed)

result = batch.apply()  # every one of them, or none of them
print(result.files)  # what it would write
batch.commit()  # one validation, one hash check, one write
```

Four things it adds, and they are the four an editor needs:

| | |
|---|---|
| **One transaction** | The tree is snapshotted before the first operation and restored if any of them refuses. The refusal names which one it was and its position in the batch. |
| **One entry in the undo stack** | `result.inverse` is the inverse of each operation in reverse order — one `Ctrl-Z` for the whole gesture. |
| **One conflict check, one save** | `commit` validates the tree the batch *would* produce once, hashes every file it touches once, and writes them together. |
| **One label** | `describe()` names the batch after its first operation and how many followed: `delete sites/hq/sw-a (+10 more)`. |

The web editor puts every gesture through this, which is why deleting a
multi-selection asks once, lists what goes *and* the cables that will dangle as
a result, and comes back as a single entry in the changes drawer.

`EditSession.apply_all` is the other thing, and stays what it was: it applies in
order and stops at the first refusal, keeping what the earlier ones did. That is
right for a caller replaying a list it already trusts — an undo stack, a plan —
and wrong for one acting on somebody's selection.

## Copying, cutting and pasting

The most-used gesture in any diagram editor, and the one that is least like a
diagram gesture underneath: a copy of a switch is a *second document*, and a
second document that says the same thing as the first one does not load.

So a copy is three decisions, all of them made in `netviz.edit.clipboard` and
none of them in JavaScript — the browser, `netviz edit copy` and a script all
get the same answer.

### The name

`sw1` becomes `sw1-copy`, then `sw1-copy-2`, `sw1-copy-3`. The series is per
*family*, not per document: copying `sw1-copy` gives `sw1-copy-2`, never
`sw1-copy-copy`. `--suffix` changes the word for an inventory whose convention
is `-b` or `-standby`, and `--name` names one copy outright.

A copy that lands in a **different** namespace keeps its name where that name is
free there, because "the same switch, in the lab folder" is what copying to a
folder means. Only a collision makes it derive one.

### The fields a copy cannot keep

Everything comes across — vendor, model, MTU, the VLAN database, the
description, the labels, the style, the routes, and the comments somebody wrote
beside them. What goes is what two elements in one inventory cannot both have:

<!-- generated: unique-fields -->
| Field | Rule | Why a copy cannot keep it |
|---|---|---|
| `metadata.location.position` | — | two things cannot be bolted into the same rack unit |
| `spec.serial` | — | a serial number names one physical unit |
| `spec.label` | — | the identifier printed on a cable is on exactly one cable |
| `spec.login` | `NG-S013` | two accounts cannot share a login |
| `spec.uid` | `NG-S013` | two users cannot share a uid |
| `spec.gid` | `NG-S013` | two groups cannot share a gid |
| `spec.bridge.address` | — | a bridge address is one bridge component's own MAC address |
| `spec.interfaces[].mac` | `NG-I008` | a MAC address is unique across the inventory |
| `spec.interfaces[].ipv4.addresses` | `NG-A004` | a fixed address is claimed by one interface in its subnet |
| `spec.interfaces[].ipv6.addresses` | `NG-A004` | a fixed address is claimed by one interface in its subnet |
| `spec.interfaces[].wireless.bss[].bssid` | `NG-W008` | a BSSID is one radio's own MAC address |
| `spec.power.inputs` | `NG-E010` | one PDU outlet feeds one power supply |
| `spec.routing.ospf.router_id` | `NG-F012` | two routers cannot share a router id |
| `spec.routing.bgp.router_id` | `NG-F012` | two routers cannot share a router id |
<!-- /generated -->

Stripping a field tidies up after it: `spec.power` loses `redundant` with its
`inputs` — a claim about feeds that are no longer written — and an `ipv4` block
left holding nothing but `enabled: true` goes entirely, since that is what its
absence already says. `--keep-unique` turns the whole table off, for the copy
that is a starting point you are about to edit; the validation gate will
usually refuse the result, and that is the point of it being a flag.

### The links

A cable is not a property of a switch; it is an element joining two of them. So:

* copying a **switch** copies no cables — there would be nothing at the far end;
* copying a **set** clones every cable whose *both* ends are in the set, rewired
  to the clones;
* a cable with only **one** end in the set is dropped, and named. A cable joining
  a clone to an original is a claim about the network nobody made;
* copying a cable **on its own** is refused, because the copy would land a second
  cable on interfaces that already have one (`NG-C001`).

Copying a **namespace** copies its subtree: every element below it lands under
`--to` with the same shape, and the same link rule applies across the whole set.

### Geometry

Given `--view`, the copies are placed in that view's stored geometry — offset
from the originals by the grid pitch, or centred on a point when the editor
passes one, which is what makes `Ctrl-V` after a right-click land under the
pointer. The entries go into the same `kind: layout` document the originals are
placed in, so a site's arrangement stays in the site's own file.

### From the command line

<!-- norun: illustrative one-liners over elements that are not in this repository -->
```bash
netviz edit copy sw1                      # -> sw1-copy, beside it
netviz edit copy sw1 --to sites/lab       # -> sites/lab/sw1
netviz edit copy sw1 --name sw2           # -> sw2
netviz edit copy sites/hq --to sites/dr   # the whole subtree, cables and all
netviz edit duplicate sw1 --view l1       # and place it in the l1 diagram
```

`duplicate` *is* `copy` with no `--to`: one operation, two spellings, because
that is what the keyboard calls it.

### In the editor

`Ctrl-C`, `Ctrl-X`, `Ctrl-V` and `Ctrl-D` over the existing multi-selection,
each one batch and each one `Ctrl-Z`. All four are canvas bindings, so `Ctrl-C`
in the YAML pane is still the text.

`Ctrl-C` puts a **serialised fragment** on the system clipboard — JSON holding
the copied documents, their namespaces and their positions — so a fragment can
be pasted into another editor session, into a different inventory, or into a
text editor where it reads as data:

```json
{
  "format": "netviz.dev/clipboard/v1",
  "root": "devices",
  "documents": [
    {"address": "devices/sw-access", "namespace": "",
     "document": {"apiVersion": "netviz.dev/v1alpha1", "kind": "switch", "…": "…"}}
  ],
  "geometry": {"devices/sw-access": {"position": {"x": 277, "y": 43}}},
  "dropped": []
}
```

The documents in it are dumped from the *models*, so a template a document
inherits ([§6.6](schema.md#66-template--reusable-partial-device-specs)) and an interface range it declares are
already expanded — which is what lets the fragment be pasted into an inventory
that has never heard of that template. Defaults are left out, so the fragment is
short enough to read.

Pasting prefers the system clipboard and falls back to the last fragment this
page copied, so `Ctrl-V` works even where the browser will not hand over
clipboard read permission. A clipboard holding anything else — a URL, a line of
YAML — is left alone rather than treated as an error.

The four routes are `POST /api/copy`, `/api/cut`, `/api/paste` and
`/api/duplicate`. `copy` writes nothing and a read-only session answers it, since
reading a fragment out of one tree to paste into another is a read.

## Arranging a selection

Align, distribute and snap-to-grid are the three gestures a diagram editor has
that mean nothing about a single shape. They live in `netviz.edit.arrange`,
not in the browser, for the same reason every other mutation does: an
arrangement is `kind: layout` documents (§18), and deciding which document holds
which node — then writing back only the entries that moved, keeping the comments
and the key spellings of the ones that did not — is the mutation layer's job.

```python
from netviz.edit import Batch, EditSession, arrange_operations

session = EditSession(root=Path("inventory"))
operations = arrange_operations(
    session.inventory,
    command="align.left",
    view="l1",
    addresses=["core/sw-a", "core/sw-b", "core/sw-c"],
)
Batch(session).apply(operations)
session.commit()
```

| Command | What it does |
|---|---|
| `align.left` / `align.centre` / `align.right` | Settles the `x` axis: onto the leftmost left edge, the selection's own vertical axis, or the rightmost right edge. |
| `align.top` / `align.middle` / `align.bottom` | Settles the `y` axis. `y` grows *upwards* here, so "top" is the largest value. |
| `distribute.horizontal` / `distribute.vertical` | Equal *gaps* between the boxes, the two outermost left where they are. Needs three. |
| `snap` | Rounds each position to the grid pitch. |

The answer is one `SetGeometry` per layout document that loses an entry, each
carrying that document's whole `nodes` section for the view — whole, because
`SetGeometry` replaces a section, and the replacement is itself a keyed merge,
so an entry whose coordinates did not change comes out of it byte-identical. Two
hundred aligned nodes spread across three layout documents is three operations
and three touched files.

A tidying that would move nothing produces no operation at all, so aligning an
already-aligned row is a no-op rather than a second identical step to undo. A
node whose entry stores no `size` is treated as a point, which is the honest
reading — the size is a consequence of the label, and the arrangement did not
decide it.

The grid pitch is the inventory's, in `netviz.toml`:

```toml
[editor]
grid = 20     # points; the default
```

A property of the diagram rather than of the person looking at it, because
snapping writes real coordinates into a real document: two people tidying the
same inventory to two different lattices would spend the afternoon undoing each
other.

## Containers: dragging a document into a namespace

A namespace is a folder and a folder is a namespace
([§2](inventory-layout.md#folders-are-namespaces)). That one fact is what lets
the editor draw a namespace as a box and *mean* it: the rectangle round
`sites/north/racks/r1` is the boundary of a directory, so dragging a switch into
it is not a picture of a move — it is `netviz edit move`, and the file moves.

```
netviz edit move sites/north/access/sw-north-acc-01 sites/north/racks/r1/sw-north-acc-01.yaml
```

is the command line for the same gesture, and the two go through the same
operation. What the editor adds is *which file*: you point at a namespace, and
[placement](#placement) decides whether the document joins a `switches.yaml`
that is already there or gets a file of its own — the same answer
`netviz edit create` gives, so a dragged tree and a typed one do not diverge.

### The gestures

| Gesture | What it writes |
|---|---|
| Drag an element into a container's frame | one `move` per document, as one change |
| Drag a multi-selection into one | the same, in one batch and one `Ctrl-Z` |
| Drag a container into another | every document under it, keeping the subtree's own shape |
| Drop on empty canvas | a `move` into the root namespace |
| Drop where it already was | nothing; the status line says so |
| **New namespace…** in the canvas or container menu | a `create` in the new folder, or the selection moved into it |
| **Paste into it** on a container | the [paste](#copying-cutting-and-pasting), into that namespace |
| Drag a container's corner | `set-geometry`, into that view's `groups` |
| The triangle on a header, or `f` | nothing: folding is a view |

The frames are drawn only when the diagram is **grouped by namespace** — the
*group* box, or `Alt-G`. A container frame promises that everything inside the
rectangle is in that namespace, and an ungrouped layout scatters a namespace's
members across the page, so a frame round them would enclose half the diagram
and dropping into it would be a lie. With grouping off there are no frames, no
drop targets and no gesture, and a drag pans the canvas.

### What a drop is refused for, and when

Before anything is written. The two refusals worth knowing about are both
knowable from the inventory alone, so the answer is the sentence naming both
sides rather than a half-applied batch and a validator's complaint:

- **the name is taken.** `sw-01` dropped into a rack that already holds an
  `sw-01` is refused, naming the one that is already there. Rename one of them
  first — two elements in one namespace cannot share a name.
- **two of the dragged documents would collide with each other.** Select two
  racks' worth of switches, drop them into one rack, and any two that share a
  name are named in the refusal.

A namespace the loader would skip is refused for the same reason: a folder whose
name starts with `.` or `_` is not read
([`NG-L002`](validation-rules.md)), so a document moved there would silently
leave the inventory.

### What travels with the document

Everything the [references](#references) section describes. The document is
moved *verbatim* — every comment, blank line and quoting choice arrives
unchanged — and then two rewrites happen around it:

- every reference **to** it is re-spelled wherever the old spelling stopped
  resolving, keeping the shape each document's author chose;
- every reference **it makes** is re-spelled for its new folder, because a plain
  name resolves outwards from the directory the document sits in and a document
  that changes directory can otherwise silently start naming something else.

Cables, tunnels, adapters, group memberships, PDU outlets, layouts and
annotations are all covered, because they are all read off the models by
`netviz.edit.references` rather than by a list kept here.

### Making a namespace

There is no operation that creates an empty one, and that is not an oversight: a
folder netviz reads is one holding a document, so an empty directory is not
something the inventory can record. **New namespace…** therefore makes the
folder by putting something in it — a new element created there, or the current
selection moved there — and the directory comes into existence with the write.

### The size of a container

A container's rectangle is stored in the `groups` section of a
[`kind: layout`](schema.md#18-layout-diagram-geometry) document, keyed by
namespace, and drawn back from it. Dragging a corner writes it; nothing else
does, which is the rule
[`docs/follow-ups.md` §16](follow-ups.md#16-diagram-geometry-is-a-sidecar-not-a-field-on-each-element)
states for sizes generally — a stored size that nobody asked for goes stale the
moment the thing inside it changes.

Two conditions, and the editor offers no handles unless both hold:

- **the diagram is arranged.** Under an automatic layout Graphviz sizes a
  cluster to fit its members on every run and ignores the stored box, so a
  resize would write a number nothing reads.
- **Graphviz boxes that namespace.** It boxes the ones holding elements
  *directly*; a level in between — `sites`, above three sites that each have
  their own box — is drawn by the editor round whatever is under it and has no
  stored box of its own. It is still a drop target and still folds.

## Annotating a diagram

A note, an area and a legend ([§21](schema.md#21-diagram-annotations-notes-areas-and-legends))
are the one part of an inventory whose whole purpose is to be arranged by hand,
so the editor treats them as directly as it treats a bend. Every gesture below
ends in one of the three annotation operations, through `/api/ops`, into the
document that declares the annotation — never into a browser-side model that
could drift from the file.

| Gesture | What it writes |
|---|---|
| `Shift-N`, or **New note** in the canvas menu | `create-annotation`, at the pointer or in the middle of the view |
| **Note about it…** on an element or a link | the same, with `spec.anchor` instead of a coordinate |
| Double-click a note, or `Shift-E` on a selected one | `set-annotation spec.text`, on `Ctrl-Enter` or on clicking away |
| Drag a note | `set-annotation spec.geometry.x` and `.y` |
| Drag its corner | `.width` and `.height` |
| Drag an area's outline, or one of its corners | `.x`, `.y`, `.width` and `.height`, in one batch |
| `Delete` on a selected one | `delete-annotation` |
| `Alt-N` | nothing: the toggle is about the picture, not the files |

Four things about it are decisions rather than details.

**An unplaced annotation gets its whole `geometry` block in one write.** A note
anchored to a switch pins no point, and `spec.geometry.x` written onto it would
leave a position with no `y` — which §21 refuses under `NG-G005`. So the first
drag of an unplaced note sends `spec.geometry` as a mapping, and every drag after
that sends a field at a time, which is what a reviewer wants to read in the
changes drawer. It is the same rule
[the draw.io round trip](drawio.md) applies to a diagram coming home changed, and
it is why `netviz.edit.apply` lets a *coherence* failure through to the commit
gate while refusing a *value* failure on the spot: one gesture is several writes,
and the document is briefly incoherent and finally correct.

**One gesture is one batch.** A drag posts both coordinates together, so `Ctrl-Z`
puts the whole drag back rather than half of it.

**An area that follows its members refuses to be dragged, and says why.** Its box
is the hull of wherever those devices were drawn, so there is no rectangle to
move: the status line answers "this area is drawn round its members; move them,
or give it a geometry to pin it to the paper". The alternative — quietly
converting it to an explicit rectangle — would change what the area *means*, from
"wherever these two devices are" to "this piece of paper", and a change of
meaning should not be a side effect of a drag. An area that does pin a rectangle
gets corner handles, and carries its extent whenever it is written, because a
zone with a position and no size is drawn round its members again.

**Handles are offered only on an arranged diagram.** Under an automatic layout
Graphviz places a note itself and `spec.geometry` changes nothing, so a drag
would write a number nobody could see the effect of — the same bargain
[link routing](commands/web.md#the-keyboard) makes. Selecting, retyping and
deleting work either way, because none of the three is about coordinates. One
caveat on resizing a note: the SVG renderer sizes a note to its text, so the box
is written for `netviz export drawio` rather than for the picture on screen.

## Using it from Python

```python
from pathlib import Path
from netviz.edit import Connect, EditSession, SetField

session = EditSession(root=Path("inventory"))
session.apply(SetField(address="sites/hq/core-sw", path="spec.model", value="C9300"))
applied = session.apply(Connect(a="core-sw:Gi1/0/3", b="acc-sw:Gi1/0/1"))

print(session.diff())  # what it would write, as a unified diff
print(session.check())  # problems it would introduce, if any
session.commit()  # validated, conflict-checked, written

undo = EditSession(root=Path("inventory"))
undo.apply_all(applied.inverse)
undo.commit()
```

Between operations the session reloads the inventory from its own overlay, so a
batch that creates a device and then sets a field on it works, and every
operation resolves names against the tree the previous one left behind.

## Reviewing what you changed

An afternoon of editing is a changeset, and a changeset is something to read
before it is committed. There are three ways to read the same one, and all three
are built on the parts above rather than on a fourth notion of what changed.

### As a diagram

[`netviz diff`](commands/diff.md) draws two inventory states as one picture:
added elements green, removed ones red and dashed but still in place, changed
ones amber with a badge naming the fields that moved, everything untouched
faded.

<!-- run: cwd=. -->
```console
$ netviz -i tests/fixtures/diff/home-lab-proposed diff --from examples/home-lab -f json -o /dev/null
diff at layer l1: 2 added, 4 changed, 2 removed
```

The comparison is [`netviz plan`](commands/plan.md)'s and the drawing is the
renderer's; nothing between them decides what changed.

### In the editor

[`netviz web DIR --write`](commands/web.md) has a **changes** drawer. It lists
every gesture made in the session — one entry per gesture, not per operation, so
deleting a switch is one line rather than five — and each entry carries:

- the YAML hunk it wrote, as a unified diff you could paste into a patch;
- a click on its label, which reveals the document it changed at its line;
- a **Revert** button.

Opening the drawer also repaints the canvas as a diff against the state the
session started from, or against `git HEAD` when the inventory is in a
repository. So the afternoon can be reviewed as a diagram before it is committed,
and the two halves of the review — the picture and the text — are two views of
one answer rather than two answers.

**A revert is a new change, not a rewind.** It applies the gesture's own inverse
as a fresh edit, which is itself logged and itself undoable. Reverting the third
of ten gestures leaves the other nine alone — and fails, loudly and without
writing, when one of them depended on what the third one did. That is the honest
behaviour: the alternative is a rewind that silently discards work.

### As a script

The drawer's **Copy commands** button hands the session over as a list of
[`netviz edit`](commands/edit.md) invocations, in the order they happened.
Paste it into a pull-request description, a runbook, or a colleague's terminal.

```text
netviz -i net edit set pc-desk spec.model 'OptiPlex 7020'
netviz -i net edit rename ap-home ap-attic
netviz -i net edit delete srv-nas --cascade
```

The rendering is never lossy. An operation a subcommand takes exactly becomes
that subcommand; one it does not — a whole-file write, a stored arrangement, an
interface richer than `--field` can carry — becomes `netviz edit apply -f -`
with the operation's own JSON on standard input. There is deliberately no third
case where the rendering *approximates* the operation: a command list that
quietly drops the length of a cable is worse than one with a JSON blob in it.

## What it deliberately does not do

**It does not repair documents.** Like `netviz fmt`, it changes what you asked
it to change. A document that does not parse cannot be edited through it — the
error says so, and names the file.

**It does not edit templates through the devices that inherit from them.** A
value a `kind: template` contributed is not written in the device's document, and
rewriting it there would silently fork fifty devices from the template that was
supposed to keep them identical. Attempting it is refused with a message naming
the template.

**It does not resolve conflicts.** It detects them and stops. Merging is a job
for the tool that already does it well, and the files are text.

**It does not lay diagrams out.** It *stores* where a node sits and which bends
a cable goes through — `SetGeometry` and `SetLinkGeometry` are first-class
operations and geometry is inventory data like anything else — but deciding
where a node should sit is [`netviz layout`](commands/layout.md)'s job, and
turning a bend into a line is the renderer's.

## See also

- [`netviz edit`](commands/edit.md) — the command reference.
- [`docs/inventory-layout.md`](inventory-layout.md) — the conventions placement follows.
- [`docs/format.md`](format.md) — the canonical form an edited canonical file keeps.
- [`docs/validation.md`](validation.md) — what the write gate runs.
- [`netviz layout`](commands/layout.md) — the first caller to write through it that is
  not a person: it stores a diagram's arrangement as `set-geometry` operations.
- [`netviz plan`](commands/plan.md) and [`netviz apply`](commands/apply.md) — a
  changeset between two inventory states, and its execution through these operations.
- [`netviz diff`](commands/diff.md) — the same changeset, drawn.
- [`netviz web`](commands/web.md) — the editor, and the changes drawer over a session.
- [`docs/architecture.md`](architecture.md) — where the write path sits in the pipeline.
