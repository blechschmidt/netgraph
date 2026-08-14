# netgraph documentation

Everything netgraph can do, arranged by what you are trying to get done. Start with the
table: find the row that describes your problem and follow the link. If you have never
run netgraph before, [getting-started.md](getting-started.md) is the one to read first.

## If you want to…

| If you want to… | Read |
|---|---|
| install netgraph and draw your first diagram | [getting-started.md](getting-started.md) |
| decide how to lay out your files, namespaces and templates | [inventory-layout.md](inventory-layout.md) |
| know exactly what a field means or what values it accepts | [schema-reference.md](schema-reference.md) |
| know why the schema is shaped the way it is, normatively | [schema.md](schema.md) |
| control what a diagram shows — layers, filters, icons, formats | [rendering.md](rendering.md) |
| arrange the diagram by hand and have it stay arranged | [commands/layout.md](commands/layout.md) |
| understand a finding, or silence one | [validation.md](validation.md) |
| look up one validation rule by its id | [validation-rules.md](validation-rules.md) |
| check subnet utilisation, find free space, hunt overlaps | [ipam.md](ipam.md) |
| find out how two machines reach each other | [paths.md](paths.md) |
| keep the YAML in one canonical form | [format.md](format.md) |
| change the inventory safely, from a script or an editor | [editing.md](editing.md) |
| get completion, inline errors and rename in your editor | [lsp.md](lsp.md) |
| diff two inventory states, review the change, and apply it | [commands/plan.md](commands/plan.md), [commands/apply.md](commands/apply.md) |
| adopt what a live network reports into the declared inventory | [commands/drift.md](commands/drift.md), [commands/plan.md](commands/plan.md) |
| gate a pull request on the inventory validating | [ci.md](ci.md) |
| bootstrap an inventory from a network that already exists | [importing.md](importing.md) |
| stop retyping the same flags | [configuration.md](configuration.md) |
| run it without installing Python or Graphviz | [docker.md](docker.md) |
| turn the inventory into hosts files, DNS zones, Ansible or Prometheus | [export.md](export.md) |
| hand a diagram to somebody who only has draw.io, and take it back | [drawio.md](drawio.md) |
| hand over as-built documentation: a page per site and per device | [commands/report.md](commands/report.md) |
| look up a command's flags | [commands/](commands/README.md) |
| see how an inventory maps onto RFC 8343, RFC 8344 and 802.1Q | [yang-mapping.md](yang-mapping.md) |
| work on netgraph itself | [architecture.md](architecture.md), [../CONTRIBUTING.md](../CONTRIBUTING.md) |
| know whether upgrading will break your inventory | [releasing.md](releasing.md), [../CHANGELOG.md](../CHANGELOG.md) |
| understand how netgraph is tested | [testing.md](testing.md) |

## The pages, by kind

### Guides — read once, front to back

* **[getting-started.md](getting-started.md)** — install netgraph and Graphviz, build a
  three-device inventory by hand, validate it, render it, then interrogate it. Ends with
  the editor setup that gives you completion and inline errors.
* **[inventory-layout.md](inventory-layout.md)** — how files become namespaces, how
  references resolve, which files are read, the element kinds, and how to declare a
  48-port switch without typing 48 interfaces.
* **[importing.md](importing.md)** — bootstrap the first inventory from LLDP, `ip -j addr`
  or the cabling spreadsheet you already keep, then converge on it by hand.
* **[rendering.md](rendering.md)** — the nine layers, the filters, namespace collapsing and
  link bundling, icon themes, labelling, stored arrangements, interactivity, and what each
  output format is good for.
* **[validation.md](validation.md)** — the three passes, severities, `--strict`, the four
  ways to suppress a rule, and how to read a finding.
* **[ci.md](ci.md)** — `netgraph validate` as a gate: the JSON envelope, SARIF and code
  scanning, inline annotations, the GitHub Action, pre-commit, GitLab.
* **[format.md](format.md)** — the canonical form `netgraph fmt` writes, and why each
  decision in it is the way it is.
* **[editing.md](editing.md)** — the write path: what an operation is, what its inverse
  is, how a rename finds every reference, and the two gates between an edit and the
  disk. `netgraph apply` and the web editor are both built on it.
* **[commands/plan.md](commands/plan.md)** — the diff engine: stable addresses, structural
  rename detection, the order a changeset has to run in, plan files and the state hash,
  and how a capture becomes a proposal. [commands/apply.md](commands/apply.md) executes
  the result against the files.
* **[ipam.md](ipam.md)** — utilisation, free space, the next free block, aggregation and
  conflicts, with the arithmetic spelled out.
* **[paths.md](paths.md)** — how the trace works, what counts as a hop, several paths and
  none, and how to draw the answer.
* **[export.md](export.md)** — the six operational artefacts, what each guarantees, what
  each drops, and how names are folded.
* **[drawio.md](drawio.md)** — the draw.io round trip: what the exported diagram
  carries, what a draw.io user may and may not safely change, and how an edited file
  comes back as a reviewable changeset.
* **[commands/report.md](commands/report.md)** — the as-built document: what each page
  carries, how a namespace becomes a site, why the output is byte-stable, and how to edit
  the layout. [example-report/](example-report/) is one, committed and browsable.
* **[configuration.md](configuration.md)** — `netgraph.toml`: per-inventory render
  defaults, named profiles, precedence, and how to see what resolved.
* **[docker.md](docker.md)** — the image and the compose file: the CLI, the live preview and
  the browser editor in a container, what they mount, what they publish, and who owns the
  files they write.
* **[lsp.md](lsp.md)** — `netgraph lsp` in VS Code, Neovim, Helix and Emacs: what each
  capability is answered by, how it degrades when the editor opens a lone file, and where
  the published JSON Schema stops being enough.

### Reference — look things up

* **[commands/](commands/README.md)** — one page per command, with every flag. The tables
  are generated from the CLI, so they cannot drift.
* **[schema-reference.md](schema-reference.md)** — every field of every kind, with types,
  defaults and its YANG counterpart. Generated from the models.
* **[validation-rules.md](validation-rules.md)** — every rule, with what triggers it, why
  it exists, how to fix it and how to suppress it. This is where a finding's help link
  lands, so its anchors are part of netgraph's interface.
* **[schema.md](schema.md)** — the normative specification. Numbered sections and `NG-*`
  rule ids that code and diagnostics quote; the anchors are stable on purpose.
* **[yang-mapping.md](yang-mapping.md)** — which standard each field comes from, and what
  netgraph deliberately does not model.

### For contributors

* **[architecture.md](architecture.md)** — the pipeline (`load_tree` → `validate` →
  `build_graph` → `filter`/`aggregate` → renderers), which module owns each stage, what
  each may assume, and the invariants not to break. Also: using netgraph as a library.
* **[../CONTRIBUTING.md](../CONTRIBUTING.md)** — dev setup, the gates, and step-by-step
  recipes for adding a validation rule or a renderer.
* **[testing.md](testing.md)** — the property-based and fuzz testing, the Hypothesis
  profiles, and how to reproduce a failure.
* **[follow-ups.md](follow-ups.md)** — the running list of known gaps and deliberate
  deferrals, each with the reasoning that deferred it.
* **[releasing.md](releasing.md)** — what a `0.x` version number promises, which surfaces
  are public API and which are internal, how a breaking change has to be recorded, and the
  mechanics of cutting a release.
* **[../CHANGELOG.md](../CHANGELOG.md)** — what changed in each release, and what you have
  to do about it.

## How the documentation is kept honest

Documentation this size only stays correct if something fails when it stops being
correct. [`tests/test_docs.py`](../tests/test_docs.py) asserts that:

* every relative link and `#anchor` in every Markdown file in the repository resolves;
* every command and every flag the CLI has appears in `commands/`, and no documented flag
  has been removed — the tables are generated from Click by
  [`tools/gen_docs.py`](../tools/gen_docs.py) and compared against what is committed;
* every rule in `netgraph/rules.py` has a section in [validation-rules.md](validation-rules.md)
  with the right severity, aliases and anchor, and appears in the index in
  [validation.md](validation.md);
* every field of every model has an entry in [schema-reference.md](schema-reference.md);
* every fenced `console` example that invokes `netgraph` is either **executed**, and its
  transcript compared byte for byte, or explicitly marked non-executable with a reason —
  see [`tools/check_examples.py`](../tools/check_examples.py).

So an example in these pages is not an illustration of what netgraph used to do. It is
either a test, or it says why it is not.
