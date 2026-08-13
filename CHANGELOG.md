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
