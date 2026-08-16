# Command reference

One page per command. Each opens with what the command is for, then a synopsis, then
the prose a table cannot carry — how the interesting flags interact, and at least one
worked example — and closes with the full flag table and the exit codes.

The synopsis, argument and flag tables on these pages are **generated from the CLI
itself** by [`tools/gen_docs.py`](../../tools/gen_docs.py) and checked by
[`tests/test_docs.py`](../../tests/test_docs.py), so a flag that exists is documented and
a flag that is documented exists. The prose around them is written by hand.

<!-- generated: command-index base= -->
| Command | What it does | Reference |
|---|---|---|
| [`netgraph init`](init.md) | Scaffold a new inventory, ready to validate and render. | [init.md](init.md) |
| [`netgraph import captures`](import.md) | Build a first inventory from output captured on live devices. | [import.md](import.md) |
| [`netgraph import drawio`](import.md) | Bring an edited draw.io diagram back as a reviewable changeset. | [import.md](import.md) |
| [`netgraph drift`](drift.md) | Compare a live network against the declared inventory. | [drift.md](drift.md) |
| [`netgraph validate`](validate.md) | Check the inventory; the gate for CI and pre-commit. | [validate.md](validate.md) |
| [`netgraph test`](test.md) | Grade the assertions the inventory declares about itself. | [test.md](test.md) |
| [`netgraph fmt`](fmt.md) | Rewrite inventory YAML into the canonical form. | [fmt.md](fmt.md) |
| [`netgraph edit set`](edit.md) | Set a field on an element, in place, comments and all. | [edit.md](edit.md) |
| [`netgraph edit unset`](edit.md) | Remove a field from an element. | [edit.md](edit.md) |
| [`netgraph edit create`](edit.md) | Declare a new element and place its document. | [edit.md](edit.md) |
| [`netgraph edit copy`](edit.md) | Copy an element or a whole namespace, links and all. | [edit.md](edit.md) |
| [`netgraph edit duplicate`](edit.md) | Copy an element into the namespace it is already in. | [edit.md](edit.md) |
| [`netgraph edit delete`](edit.md) | Remove an element, and what cannot survive it. | [edit.md](edit.md) |
| [`netgraph edit rename`](edit.md) | Rename an element and every reference to it. | [edit.md](edit.md) |
| [`netgraph edit move`](edit.md) | Move an element's document to another file. | [edit.md](edit.md) |
| [`netgraph edit connect`](edit.md) | Cable two interfaces together. | [edit.md](edit.md) |
| [`netgraph edit disconnect`](edit.md) | Remove a cable. | [edit.md](edit.md) |
| [`netgraph edit add-interface`](edit.md) | Add an interface to an element. | [edit.md](edit.md) |
| [`netgraph edit remove-interface`](edit.md) | Remove an interface from an element. | [edit.md](edit.md) |
| [`netgraph edit apply`](edit.md) | Apply operations given as JSON; the programmatic face. | [edit.md](edit.md) |
| [`netgraph plan`](plan.md) | Diff two inventory states into a reviewable changeset. | [plan.md](plan.md) |
| [`netgraph diff`](diff.md) | Draw the difference between two inventory states as one diagram. | [diff.md](diff.md) |
| [`netgraph review`](review.md) | Write a change up as one pull-request review: changeset, diagram, new findings. | [review.md](review.md) |
| [`netgraph apply`](apply.md) | Execute a plan against the inventory files. | [apply.md](apply.md) |
| [`netgraph converge plan`](converge.md) | Turn drift into an ordered, per-device remediation plan. | [converge.md](converge.md) |
| [`netgraph log`](log.md) | List the commits that changed the inventory, and what each one changed. | [log.md](log.md) |
| [`netgraph render`](render.md) | Draw the graph as SVG, PNG, PDF, DOT, Mermaid, JSON or HTML. | [render.md](render.md) |
| [`netgraph layout`](layout.md) | Store the diagram's arrangement, so a hand-placed node stays put. | [layout.md](layout.md) |
| [`netgraph watch`](watch.md) | Re-render on every save, optionally serving the result. | [watch.md](watch.md) |
| [`netgraph web`](web.md) | Edit the YAML and see the diagram side by side in a browser. | [web.md](web.md) |
| [`netgraph lsp`](lsp.md) | Serve completion, diagnostics and rename to an editor over LSP. | [lsp.md](lsp.md) |
| [`netgraph path`](path.md) | Trace how two elements reach each other, hop by hop. | [path.md](path.md) |
| [`netgraph impact`](impact.md) | Simulate a failure: blast radius, single points of failure, promises. | [impact.md](impact.md) |
| [`netgraph list`](list.md) | Tabulate devices, cables, tunnels, VLANs, BSSs or subnets. | [list.md](list.md) |
| [`netgraph query`](query.md) | Select elements with one selector language, and print what matched. | [query.md](query.md) |
| [`netgraph ipam`](ipam.md) | Report utilisation, free space, overlaps and aggregates. | [ipam.md](ipam.md) |
| [`netgraph export`](export.md) | Emit hosts files, DNS zones, Ansible, Prometheus, cable lists. | [export.md](export.md) |
| [`netgraph report`](report.md) | Write the as-built documentation: a page per site and per device. | [report.md](report.md) |
| [`netgraph show`](show.md) | Print one element as it was resolved, expansions included. | [show.md](show.md) |
| [`netgraph rules`](rules.md) | List the validation rules and their ids. | [rules.md](rules.md) |
| [`netgraph schema`](schema.md) | Write the JSON Schema for editor completion. | [schema.md](schema.md) |
| [`netgraph config show`](config.md) | Show the resolved settings and where each value came from. | [config.md](config.md) |
| [`netgraph cache info`](cache.md) | Report where the parse cache is and what is in it. | [cache.md](cache.md) |
| [`netgraph cache clear`](cache.md) | Delete this inventory's cached documents. | [cache.md](cache.md) |
| [`netgraph completion`](completion.md) | Print the shell completion script. | [completion.md](completion.md) |
| [`netgraph version`](version.md) | Report the netgraph, Python and Graphviz versions in use. | [version.md](version.md) |
<!-- /generated -->

## Global options

These come **before** the subcommand, because they say which inventory the subcommand
works on and how loudly it reports.

<!-- generated: options  -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-V`, `--version` | — | off | Show the netgraph, Python and Graphviz versions in use, and exit. 'netgraph version --json' is the same report, machine-readably. |
| `-i`, `--inventory` | `PATH` | current directory | Root folder of the YAML inventory tree, or a single YAML file. |
| `-q`, `--quiet` | — | off | Only report errors. |
| `-v`, `--verbose` | `INTEGER, >= 0` | `0` | Increase verbosity; repeatable. |
| `--color`, `--no-color` | — | — | Force coloured output on or off. Auto-detected from the terminal by default. |
| `--no-cache` | — | off | Parse every file, and remember nothing. The cache is keyed by file contents and is safe to leave on; set NETGRAPH_NO_CACHE=1 to switch it off for a whole environment. See 'netgraph cache info'. |
<!-- /generated -->

So the inventory is named once, for any command:

<!-- run: -->
```console
$ netgraph -i examples/home-lab validate
no problems found
```

`-v` is repeatable and cumulative: one `-v` names the configuration file and the
inventory root as they are found, two adds per-stage timings. `--no-color` is implied
whenever stdout is not a terminal, and `NO_COLOR` in the environment forces it, so a
transcript captured in CI carries no escape sequences.

`--no-cache` parses every file rather than reusing the parse cache, and remembers
nothing for the next run. It is a global option because it is about the run rather than
about one command; the cache is keyed by file contents and is safe to leave on. See
[`netgraph cache`](cache.md) and
[`docs/configuration.md`](../configuration.md#cache--remembering-parsed-files).

`-h/--help` is not in the table because Click supplies it rather than netgraph, but it
works on the group and on every subcommand, and running `netgraph` with no command at all
prints the same help. Every command that *reads* an inventory reads the one `-i` names;
[`init`](init.md) and [`import`](import.md), which *write* one, take their target as an
argument and as `-o` respectively.

## Output discipline

**Data on stdout, commentary on stderr.** `render` writes the diagram to stdout when no
`--output` is given, so its findings and progress notes go to stderr instead. That is what
makes `netgraph render -f json | jq` and `netgraph validate > report.txt` both do what
they look like they do. Colour is used only when the stream is a terminal.

One consequence worth knowing when reading the transcripts in these pages: the checker
that executes them captures stdout and stderr separately and prints stdout first, so a
block that mixes the two shows them grouped rather than interleaved the way a terminal
would.

## Environment variables

| Variable | Effect |
|---|---|
| `NO_COLOR` | Suppress colour, per [no-color.org](https://no-color.org). |
| `NETGRAPH_YAML_LOADER` | Which YAML parser to use: `auto` (default), `python` or `libyaml`. |
| `NETGRAPH_NO_CACHE` | Set to `1` to disable the parse cache everywhere, as `--no-cache` does per run. |
| `NETGRAPH_CACHE_DIR` | Where the parse cache lives, overriding `XDG_CACHE_HOME` and `netgraph.toml`. |
| `NETGRAPH_DOT` | The Graphviz `dot` to use, overriding the one on `PATH`. |

`auto` takes PyYAML's libyaml bindings when the installed wheel carries them — several
times faster on a large inventory — and falls back to the pure-Python parser otherwise.
The two accept exactly the same documents and report the same line and column for a
problem; they differ only in PyYAML's own wording for a syntax error. Set `python` to pin
the slow path, or `libyaml` to refuse to start without the fast one.

`NETGRAPH_HYPOTHESIS_PROFILE` affects the test suite rather than the tool; see
[`docs/testing.md`](../testing.md).

## Exit codes

Every command draws its codes from the same table; each page narrows it to the codes
that command can actually return.

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | The inventory was rejected, or the command's own question got a negative answer — findings from `validate`, no path from `path`, files that would change under `fmt --check`. |
| `2` | Usage error: an unknown flag, a bad value, a missing argument. Click's code. |
| `3` | The inventory could not be loaded or read at all: unparseable YAML, an unreadable file, a path that is neither a directory nor YAML. |
| `5` | Rendering or writing the artefact failed — Graphviz is missing, or the output path cannot be written. |
| `130` | Interrupted (`Ctrl-C`). |
| `141` | stdout closed early, as when piping into `head`. |

## See also

* [`docs/README.md`](../README.md) — the documentation index.
* [`docs/getting-started.md`](../getting-started.md) — the tutorial these commands appear in.
* [`docs/configuration.md`](../configuration.md) — `netgraph.toml`, so most of these flags
  need typing only once.
* [`docs/architecture.md`](../architecture.md) — what happens between the command line and
  the output.
