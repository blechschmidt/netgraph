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
