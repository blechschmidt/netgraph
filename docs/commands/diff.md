# `netgraph diff`

`netgraph diff` is [`netgraph plan`](plan.md) as a picture. The same two
inventory states go in and the same changeset comes out of the same comparison
code — but instead of printing it, this draws one diagram in which:

| | how it is drawn |
|---|---|
| **added** | green, at full weight |
| **removed** | red and **dashed**, and still in place |
| **changed** | amber, with a badge naming the fields that moved |
| **untouched** | faded |

The point of "still in place" is that a deletion must not reshuffle the diagram.
If removing one server moved the other forty boxes, the change you were trying
to look at would be lost in the churn it caused, so a removed node keeps the
position the [layout document](layout.md) gave it and the picture stays
comparable to the one you had before.

`netgraph diff` writes nothing to the inventory and never talks to a device.

## Contents

- [Synopsis](#synopsis)
- [Where the two sides come from](#where-the-two-sides-come-from)
- [Two revisions](#two-revisions)
- [Reading the diagram](#reading-the-diagram)
- [One layer at a time](#one-layer-at-a-time)
- [Narrowing what is marked](#narrowing-what-is-marked)
- [The JSON form](#the-json-form)
- [Formats](#formats)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis diff -->
```text
netgraph [GLOBAL OPTIONS] diff [OPTIONS]
```
<!-- /generated -->

---

## Where the two sides come from

Exactly as [`netgraph plan`](plan.md#where-the-two-sides-come-from) reads them,
plus one spelling and one source of its own:

| Invocation | Left of the diff | Right of the diff |
|---|---|---|
| `diff --against HEAD` | the git ref | the working tree |
| `diff --from origin/main` | the git ref | the working tree |
| `diff --to ../proposed` | the working tree | that folder |
| `diff --from a --to b` | folder `a` | folder `b` |
| `diff --plan drift.plan` | the working tree | the tree as that plan would leave it |

`--against` and `--from` are the same side; `--against` exists because "diff the
inventory **against** HEAD" is how the question is asked out loud. Giving both is
an error.

`--from` and `--to` take either a directory or a git ref. A path that exists as a
directory is a folder; anything else is handed to `git archive`, which reads it
into a temporary directory, so the command cannot disturb the working tree, an
uncommitted change or the index.

`--plan` takes a file written by `netgraph plan -out`. The plan is executed into
an in-memory edit session that is never committed, so what is drawn on the right
is the same text [`netgraph apply`](apply.md) would write — produced by the same
operations, not by a reconstruction. The state hash is checked exactly as `apply`
checks it: a plan made against a tree that has since moved on is refused.

<!-- run: cwd=. -->
```console
$ netgraph -i tests/fixtures/diff/home-lab-proposed diff --from examples/home-lab -f json -o /dev/null
diff at layer l1: 2 added, 4 changed, 2 removed
```

---

## Two revisions

Neither side has to be the working tree. `--from` and `--to` each take any
revision git resolves — a hash, a tag, a branch, `HEAD~10`, `origin/main@{2
weeks ago}` — so any two points in the inventory's history can be drawn against
each other:

<!-- norun: the revisions are this reader's, not this repository's -->
```console
$ netgraph -i net diff --from v1.0 --to v2.0 -f svg -o release.svg
$ netgraph -i net diff --from HEAD~1 --to HEAD -f svg -o last-change.svg
$ netgraph -i net diff --from 'origin/main' -f html -o review.html
```

Both sides are read out of the **object database** with `git archive`, into a
temporary directory that is removed afterwards. Nothing is checked out, the
index is not touched, and an uncommitted change in the tree is neither used nor
disturbed — so this is safe to run mid-edit, and safe to run while
[`netgraph web`](web.md) has the same folder open.

[`netgraph log`](log.md) is how you find the two revisions, and
[the editor's timeline](web.md#the-history-timeline) is this command as a
scrubber: one frame per commit, each drawn against its parent.

Two failures are told apart, because they mean different things:

<!-- norun: the repository is the reader's -->
```console
$ netgraph -i net diff --from v0.1
error: v0.1 has no 'net' directory in it, so there is no inventory to read at that revision
$ netgraph -i net diff --from v0.0
error: git cannot read 'v0.0'; check that the ref exists and that the inventory directory is present in it
```

The first is a repository that grew its inventory folder later, which is a fact
about the history; the second is a ref nobody has heard of, which is a typo.

---

## Reading the diagram

Colour is never the only carrier. A removed node is dashed as well as red and a
removed link is dashed whatever medium it was declared with, so the diagram
survives a greyscale print and a red-green reader. Every marked node also carries
the sigil `netgraph plan` prints — `+`, `-`, `~` — in a row of its own label.

An amber badge names the fields that moved, spelled the way `netgraph plan`
spells them:

```text
~ spec.interfaces[name=eth0].mtu
```

Three paths are spelled out and the rest counted (`+2 more`); a device whose
whole `spec` was rewritten is one to read in `netgraph plan`, not one to fit on a
node label.

### Renames are one box, not two

A rename detected by the plan is drawn as **one** amber box under the new name,
badged `was <old address>`. Drawing the old name in red beside the new one in
green would say two devices were swapped, which is exactly what did not happen.
`--no-renames` turns the detection off and gets the deletion-and-creation pair
back, which is occasionally what you want to see.

### What decides each mark

Two things, and no third opinion about what changed:

- **Presence** decides *added* and *removed*: drawn on the right and not the
  left, or the other way round. This is the only thing that *can* answer for a
  derived node — nothing declares `subnet:192.168.10.0/24`, so no changeset can
  mention it.
- **The changeset** decides everything finer: that an element was *updated*
  rather than merely still present, which of its fields moved, and that a box is
  the same device under another name.

A consequence worth knowing: a change to a device's `spec.model` marks the
device, not the subnet it sits in. The membership link did not move.

---

## One layer at a time

`--layer` may be given once. A diff is a comparison of one view, and two views
would need two overlays computed against two different pairs of drawings; one
output holding both would have to say which of them each mark belonged to. Draw
each layer as its own diff instead.

The layer matters to what you see. A change to an IP address is invisible at
layer 1 and obvious at layer 3; a new tunnel is invisible below `--layer
overlay`. When a changeset has entries but none of them shows at the layer asked
for, the command says so rather than presenting a wholly faded diagram as the
answer.

---

## Narrowing what is marked

`--target` narrows the **marks**, not the graph. Everything the filters would
have drawn is still drawn — the rest of the network simply comes out untouched,
which is the difference between narrowing a diff and filtering a graph. It takes
the same three spellings [`plan --target`](plan.md#addresses) takes.

The ordinary graph filters — `--namespace`, `--kind`, `--name`,
`--neighbors-of` — still work and still decide what *exists* in the drawing.
Reach for `--target` when you want the context kept and for the filters when you
want the page smaller.

---

## The JSON form

`-f json` emits the union graph with a `diff` object on **every** node and edge —
untouched ones included, so a consumer can tell "this export carries no diff"
from "this element did not change" — plus two documents at the top level:

```json
{
  "nodes": [
    {"id": "hosts/srv-nas", "diff": {"mark": "removed"}},
    {"id": "hosts/pc-desk", "diff": {"mark": "changed", "fields": ["spec.model"]}}
  ],
  "diff": {"nodes": {}, "edges": {}, "counts": {"added": 2, "changed": 4, "removed": 2}},
  "changeset": {"schemaVersion": 1, "summary": {}, "changes": []}
}
```

`changeset` is byte-for-byte what `netgraph plan --json` would have printed. It
travels *with* the graph rather than in a second file, because a consumer of a
diff needs both and two documents that could be paired wrongly is the failure
that avoids.

---

## Formats

Every format [`netgraph render`](render.md) has, except Mermaid: a Mermaid
flowchart can neither colour a node nor hold a changeset beside one, so
`-f mermaid` is refused by name rather than emitting a diagram in which nothing
distinguishes the deleted switch.

<!-- run: cwd=. rc=2 -->
```console
$ netgraph -i tests/fixtures/diff/home-lab-proposed diff --from examples/home-lab -f mermaid
Usage: netgraph diff [OPTIONS]
Try 'netgraph diff --help' for help.

Error: mermaid output has no way to say what changed — it can colour nothing and hold no changeset beside the graph; render the diff as dot, svg, html, png, pdf, json
```

---

## Options

<!-- generated: options diff -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-f`, `--format` | `[dot\|svg\|html\|png\|pdf\|mermaid\|json]` | `dot` | Output format. dot: Graphviz DOT source; svg: SVG image, via Graphviz; html: self-contained interactive page, via Graphviz; png: PNG image, via Graphviz; pdf: PDF document, via Graphviz; mermaid: Mermaid flowchart, for embedding in Markdown; json: node-link JSON, for downstream tooling. |
| `-o`, `--output` | `FILE` | — | Write to this file instead of stdout. |
| `--against` | `REF\|DIR` | — | Draw the inventory against this git ref or folder: '--against HEAD' is 'what have I changed since the last commit'. The same side as --from, spelled the way a diff reads. |
| `--from` | `REF\|DIR` | — | Take the state on the left of the diff from a git ref or another folder. A directory that exists is a folder; anything else is a git ref, exported read-only. |
| `--to` | `REF\|DIR` | — | Take the state on the right of the diff from a git ref or folder. Defaults to the inventory. |
| `--plan` | `FILE` | — | Draw the inventory against the state a saved plan would leave it in, without writing anything. The plan is checked against the tree exactly as 'netgraph apply' checks it. |
| `--target` | `ADDRESS` | — | Mark only changes to elements this glob selects; the rest of the diagram is drawn untouched. Repeatable. |
| `--no-renames` | — | off | Draw every rename as a deletion beside a creation rather than as one moved element. |
| `--namespace` | `NS` | — | Keep only elements in this namespace or below it. Repeatable. |
| `--vlan` | `VID` | — | Keep only elements participating in this VLAN. Repeatable. |
| `--kind` | `[switch\|router\|firewall\|hub\|computer\|server\|adapter\|patchpanel\|pdu\|user\|group]` | — | Keep only elements of this kind. Repeatable. |
| `--name` | `GLOB` | — | Keep only elements whose name matches this glob. Repeatable. |
| `--neighbors-of` | `NAME` | — | Keep only the neighbourhood of this element. |
| `--depth` | `INTEGER, >= 0` | `1` | How many hops --neighbors-of reaches. |
| `--select` | `QUERY` | — | Keep only the elements this query selects, e.g. "kind = switch and not has vrf". The flags above are sugar for the equivalent query and are combined with it; 'netgraph query --explain' prints which. See docs/query.md. |
| `--collapse` | `NS` | — | Replace this namespace and everything under it with one node, labelled with what it holds. Links crossing the boundary attach to it; links inside it are counted rather than drawn. Repeatable. |
| `--collapse-depth` | `N` | — | Collapse every namespace N levels deep, counted from the shallowest one that branches: '--collapse-depth 1' is the site-level overview of a tree laid out as sites/<site>/<tier>. |
| `--bundle-links`, `--no-bundle-links` | — | — | Draw parallel links between the same pair of elements as one edge, with the count in the label. Members of a declared 'lag' interface are bundled either way unless --no-bundle-links is given, since the inventory already says they are one logical link. |
| `--show-ips`, `--no-show-ips` | — | `--show-ips` | Print configured IP addresses on the nodes. |
| `--show-vlans`, `--no-show-vlans` | — | `--show-vlans` | Annotate nodes and links with VLAN membership. |
| `--annotations`, `--no-annotations` | — | `--annotations` | Draw the notes, areas and legends the inventory declares for this view. Turn them off for a diagram that should carry the topology and nothing written about it — a printed page for an audit — and leave them on for one that is being read rather than checked, where the callout is the reason the screenshot is worth attaching to the ticket. |
| `--group-by-namespace` | — | off | Draw each namespace as a visual group. |
| `--icons` | `THEME\|DIR` | — | Draw each element as an icon instead of a plain shape. Built in: cisco, none. A directory of images named after element kinds (router.svg, switch.png, ...) also works. Graphviz formats only. |
| `--theme` | `NAME\|PATH` | — | Apply a stylesheet: selectors by kind, name, namespace, role or label, each mapping onto a style block. Built in: blueprint, mono, none. A path to a 'kind: theme' YAML file also works. An element's own spec.style still wins. |
| `--style`, `--no-style` | — | `--style` | Honour the styles the inventory and the theme declare. --no-style draws the plain diagram from the built-in palette alone, which is the way to read a topology whose stylesheet is in the way. Icons are unaffected: use --icons none. |
| `--tooltips`, `--no-tooltips` | — | `--tooltips` | Carry the full detail of each element — interfaces, addresses, VLANs, cabling — as hover text. Reaches a reader in svg output; png and pdf have nowhere to put it. |
| `--link-template` | `URL` | — | Link each element back to the YAML that declares it, e.g. 'https://git.example.com/net/blob/main/{file}#L{line}'. Placeholders: {file}, {line}, {name}, {namespace}, {kind}. dot and svg only. |
| `--element-ids` | — | off | Give every node, edge and namespace a stable id derived from its name, so the diagram can be deep-linked and styled from outside. dot and svg only. |
| `--max-addresses` | `N` | `4` | Longest address list spelled out under a node before it is abbreviated to 'and N more'. 0 prints the count alone. |
| `--rankdir` | `[tb\|lr\|bt\|rl]` | TB, top to bottom | Layout direction. A wide network reads better left to right; a deep one top to bottom. Honoured by the Graphviz backends and by mermaid. |
| `--routing` | `[spline\|orthogonal\|straight]` | whatever the inventory's layout documents say, else spline | How links are drawn between the bends they are pinned through: 'spline' is the curve Graphviz draws, 'orthogonal' right angles, 'straight' segment to segment. A default: a link that pins a style of its own keeps it. Honoured by the Graphviz backends, the JSON export and the editor. 'netgraph layout --write' records it in the view it arranges, so the choice is the inventory's rather than the command line's from then on. |
| `--avoid`, `--no-avoid` | — | avoid | Route orthogonal links around the boxes they are not attached to instead of straight across them. Only applies to an arranged diagram drawn with '--routing orthogonal': a spline has nothing to route around, and an unarranged one is routed by Graphviz, which already avoids nodes. A bend you placed yourself is never moved — routing fills the segments between them. '--no-avoid' is the local Z-and-L every orthogonal diagram was drawn with before this existed. |
| `--title` | `TEXT` | — | Caption for the diagram. |
| `--layer` | `[physical\|l1\|l2\|l3\|overlay\|routing\|rack\|power\|identity\|netns\|security]` | `l1` | l1 draws the physical topology; l2 annotates it with VLANs; l3 draws IP subnets and the elements addressed in them; overlay draws the tunnels; routing draws the BGP sessions and OSPF adjacencies, clustered by VRF; physical adds the patch panels l1 splices out; rack draws a front elevation per rack; power draws the PDUs and the feeds into everything they power; identity draws the users and groups; netns opens each machine up into the network stacks inside it, joined by their veth pairs; security draws the firewall zones and what the policy lets cross between them. Repeatable for -f html, which draws each layer and puts a switcher over them. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Proceed even when validation failed. The result may not match the files. |
| `--profile` | `NAME` | — | Apply the [profile.NAME] block of netgraph.toml on top of its [render] table. Explicit flags still win over both. |
| `--show-config` | — | off | Print the settings this invocation resolves to, and where each one came from, then exit without doing any work. |
<!-- /generated -->

The filter and display options are `render`'s, and mean the same thing here —
they decide what the drawing *contains*, and the diff then marks what is in it.
`--layer` is the one exception to the shared help above: it may be given once,
for the reason under [One layer at a time](#one-layer-at-a-time).

## Exit codes

| Code | When |
|---|---|
| 0 | The diff was drawn. Whether or not anything changed. |
| 1 | Either side does not load (without `--force`), a ref cannot be read, or `--plan` names something that is not a plan or was made against another state. |
| 2 | Usage: nothing to compare against, `--against` with `--from`, `--plan` with `--from`/`--to`, `--layer` more than once, or a format that cannot draw a diff. |

An inventory that does not load is refused rather than drawn, for the reason
`plan` refuses it: a document that was rejected is absent from the inventory, so
diffing against it would read as a deletion. `--force` draws it anyway and says
so once per side.

## See also

- [`netgraph log`](log.md) — which revisions there are to diff, and what each did.
- [`netgraph plan`](plan.md) — the same changeset, as text.
- [`netgraph apply`](apply.md) — executing one against the files.
- [`netgraph render`](render.md) — the formats, the layers and the filters.
- [`netgraph layout`](layout.md) — the arrangement a removed node keeps.
- [`netgraph web`](web.md) — the same overlay, live, over a session's edits.
- [`docs/editing.md`](../editing.md) — the write path, in prose.
