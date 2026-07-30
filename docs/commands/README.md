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
| [`netgraph import`](import.md) | Build a first inventory from output captured on live devices. | [import.md](import.md) |
| [`netgraph validate`](validate.md) | Check the inventory; the gate for CI and pre-commit. | [validate.md](validate.md) |
| [`netgraph fmt`](fmt.md) | Rewrite inventory YAML into the canonical form. | [fmt.md](fmt.md) |
| [`netgraph render`](render.md) | Draw the graph as SVG, PNG, PDF, DOT, Mermaid, JSON or HTML. | [render.md](render.md) |
| [`netgraph watch`](watch.md) | Re-render on every save, optionally serving the result. | [watch.md](watch.md) |
| [`netgraph web`](web.md) | Edit the YAML and see the diagram side by side in a browser. | [web.md](web.md) |
| [`netgraph path`](path.md) | Trace how two elements reach each other, hop by hop. | [path.md](path.md) |
| [`netgraph list`](list.md) | Tabulate devices, cables, tunnels, VLANs, BSSs or subnets. | [list.md](list.md) |
| [`netgraph ipam`](ipam.md) | Report utilisation, free space, overlaps and aggregates. | [ipam.md](ipam.md) |
| [`netgraph export`](export.md) | Emit hosts files, DNS zones, Ansible, Prometheus, cable lists. | [export.md](export.md) |
| [`netgraph show`](show.md) | Print one element as it was resolved, expansions included. | [show.md](show.md) |
| [`netgraph rules`](rules.md) | List the validation rules and their ids. | [rules.md](rules.md) |
| [`netgraph schema`](schema.md) | Write the JSON Schema for editor completion. | [schema.md](schema.md) |
| [`netgraph config show`](config.md) | Show the resolved settings and where each value came from. | [config.md](config.md) |
| [`netgraph completion`](completion.md) | Print the shell completion script. | [completion.md](completion.md) |
<!-- /generated -->

## Global options

These come **before** the subcommand, because they say which inventory the subcommand
works on and how loudly it reports.

<!-- generated: options  -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-V`, `--version` | — | off | Show the version and exit. |
| `-i`, `--inventory` | `PATH` | current directory | Root folder of the YAML inventory tree, or a single YAML file. |
| `-q`, `--quiet` | — | off | Only report errors. |
| `-v`, `--verbose` | `INTEGER, >= 0` | `0` | Increase verbosity; repeatable. |
| `--color`, `--no-color` | — | — | Force coloured output on or off. Auto-detected from the terminal by default. |
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
