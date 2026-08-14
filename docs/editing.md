# Editing an inventory

netgraph reads a folder of YAML and draws a network. This page is about the
other direction: how it *writes* that folder, and why writing it is a harder
problem than reading it.

Everything netgraph is growing towards — a diagram you can drag things around in,
an undo stack, a `plan`/`apply` pair that shows a changeset before it lands —
needs one thing first: a way to change the files that is as safe and as lossless
as the way it reads them. That way is `netgraph.edit`, and
[`netgraph edit`](commands/edit.md) is its command-line face.

[`netgraph apply`](commands/apply.md) is the first of those callers to arrive: it
takes a changeset computed by [`netgraph plan`](commands/plan.md) and turns each
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
- [Using it from Python](#using-it-from-python)
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

There are thirteen. Eleven are **semantic** — the vocabulary a person or a
diagram uses:

| Operation | JSON `op` | What it does |
|---|---|---|
| `CreateElement(kind, name, namespace, spec, metadata, file)` | `create` | Adds a document declaring a new element. |
| `DeleteElement(address, cascade)` | `delete` | Removes it, and the file if it was the last document in it. |
| `RenameElement(address, new_name)` | `rename` | Changes `metadata.name`, and every reference to it. |
| `MoveElement(address, file, index)` | `move` | Moves the document, verbatim, possibly to another namespace. |
| `SetField(address, path, value)` | `set` | Writes a value at a field path. |
| `UnsetField(address, path)` | `unset` | Removes it. |
| `AddInterface(address, interface, index)` | `add-interface` | Appends to `spec.interfaces`. |
| `RemoveInterface(address, name, cascade)` | `remove-interface` | Removes a port, and what terminated on it. |
| `Connect(a, b, spec, name, namespace, file)` | `connect` | Creates a cable between two interfaces. |
| `Disconnect(address)` | `disconnect` | Removes a cable. |
| `SetGeometry(view, nodes, edges, groups, layout, namespace, file)` | `set-geometry` | Writes one view of a [`kind: layout`](schema.md#18-layout-diagram-geometry) document. |

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
[`netgraph layout`](commands/layout.md) writes through, and the one a canvas will
write through when a node is dragged.

The set is deliberately closed. A fourteenth kind of change is a fourteenth
operation, defined here, and not a caller reaching for the file system.

## What an operation guarantees

**Only the intended hunk changes.** The tests assert this by diffing every file
an operation touched and requiring the diff to be the change and nothing else.
A document nobody edited comes back as the exact bytes it was read as, byte-order
mark and CRLF line endings included. Inside a document that *was* edited,
`ruamel` keeps the comments, blank lines, quoting and key order, and the emitter
is [probed](#a-note-on-indent-probing) against the source so that its indent
style matches the file rather than netgraph's preference.

**A canonical file stays canonical.** If a document was already in the form
[`netgraph fmt`](commands/fmt.md) writes, the edited version is put back through
the formatter — so a key added to a mapping lands where the schema order puts it
rather than at the end. If the document was *not* canonical, it is left alone:
reformatting it would bury the edit under a diff nobody asked for.

**Nothing is half-applied.** An operation that refuses — a delete that would
dangle a cable, a path that names no field — leaves the tree exactly as it was.
Refusals happen before any file is written, and the in-memory tree is rolled back
to the state the operation found.

### A note on indent probing

`ruamel` has to be told how a file indents its sequences before it can reproduce
them. Rather than guess, `netgraph.edit.roundtrip` dumps the *unmodified*
document with each candidate style and keeps the first that reproduces the source
byte for byte. The canonical form and the other common style are both matched. A
document no candidate reproduces is still editable — the edit lands and is
correct — but its diff is wider than one hunk, which is one of the arguments for
running `netgraph fmt` over a tree once and then leaving it alone.

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

`move` carries a condition of its own. A move that takes the last document out of
a file deletes the file, so the inverse has to *make* it again — and a file
netgraph makes is plain UTF-8 with `\n` line endings that starts at its first
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
document wrote was a choice. netgraph keeps it: a short name stays short where a
short name still resolves to the right element, and a qualified name stays
qualified in the same relative-or-absolute shape. Only when the author's form
would now resolve to something else — or to nothing — is it escalated to the
fully-qualified name, which always resolves.

The same machinery runs when a `move` changes an element's namespace, in both
directions: the references *to* the moved element are re-spelled, and the
references the moved document *makes* are re-spelled too, because a plain name
resolves outwards from the folder its document sits in and a document that
changes folders can otherwise silently start naming something else.

### Deleting asks first

Deleting a device with cables on it is refused, and the cables are named:

<!-- norun: writes to the working tree -->
```console
$ netgraph -i examples/home-lab edit delete sw-home
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
moved in between — your editor, a `git checkout`, a second netgraph — is a typed
`ConflictError` and the edit is dropped. `--force` does not skip this: it
overrides a judgement about *your* change, not a fact about somebody else's.

Writes themselves go through a temporary file and a rename, so a reader sees the
old file or the new one and never half of either. There is no cross-file
transaction — a plain filesystem does not offer one — so a write that fails
part-way through a multi-file change says which files it had already written.

## Using it from Python

```python
from pathlib import Path
from netgraph.edit import Connect, EditSession, SetField

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

## What it deliberately does not do

**It does not repair documents.** Like `netgraph fmt`, it changes what you asked
it to change. A document that does not parse cannot be edited through it — the
error says so, and names the file.

**It does not edit templates through the devices that inherit from them.** A
value a `kind: template` contributed is not written in the device's document, and
rewriting it there would silently fork fifty devices from the template that was
supposed to keep them identical. Attempting it is refused with a message naming
the template.

**It does not resolve conflicts.** It detects them and stops. Merging is a job
for the tool that already does it well, and the files are text.

**It does not lay diagrams out.** It *stores* where a node sits — `SetGeometry`
is a first-class operation and geometry is inventory data like anything else —
but deciding where a node should sit is [`netgraph layout`](commands/layout.md)'s
job, and drawing it there is the renderer's.

## See also

- [`netgraph edit`](commands/edit.md) — the command reference.
- [`docs/inventory-layout.md`](inventory-layout.md) — the conventions placement follows.
- [`docs/format.md`](format.md) — the canonical form an edited canonical file keeps.
- [`docs/validation.md`](validation.md) — what the write gate runs.
- [`netgraph layout`](commands/layout.md) — the first caller to write through it that is
  not a person: it stores a diagram's arrangement as `set-geometry` operations.
- [`netgraph plan`](commands/plan.md) and [`netgraph apply`](commands/apply.md) — a
  changeset between two inventory states, and its execution through these operations.
- [`docs/architecture.md`](architecture.md) — where the write path sits in the pipeline.
