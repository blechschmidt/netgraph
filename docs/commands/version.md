# `netviz version`

Report the netviz, Python and Graphviz versions in use, plus which YAML parser was
selected and what the runtime dependencies resolved to. `netviz --version` prints the
same text; this command exists for the `--json` form, which is what to paste into a bug
report.

It needs no inventory and reads no configuration file, so it answers from a directory that
holds nothing at all.

## Synopsis

<!-- generated: synopsis version -->
```text
netviz [GLOBAL OPTIONS] version [OPTIONS]
```
<!-- /generated -->

## Why it reports more than a version

A version number on its own answers almost none of the questions a bug report raises,
because nearly every interesting failure in netviz is a failure of something *around* it:

* **Graphviz is absent, or is a version whose `dot` rejects an attribute.** Every `svg`,
  `png`, `pdf` and `html` render goes through the `dot` binary, and "installed but not on
  `PATH`" is the normal state of Graphviz on Windows.
* **PyYAML was built without libyaml,** so the pure-Python parser is in use. It reports
  positions differently and is several times slower; which one is active cannot be worked
  out from a list of installed packages.
* **The `netviz` on `PATH` belongs to an environment nobody meant to be in.** The
  interpreter path is the fastest way to see that.

So the report names all of it. Nothing in it is looked up lazily or guessed: the Graphviz
version comes from actually running `dot -V` on the binary
[`netviz render`](render.md) would use.

## What it prints

<!-- norun: the Graphviz, Python and dependency versions are properties of the reader's machine -->
```console
$ netviz --version
netviz 0.0.1
Python       3.12.3 (CPython) at /usr/local/bin/python3.12
Graphviz     2.43.0 at /usr/bin/dot
Platform     Linux-6.8.0-generic-x86_64-with-glibc2.39
YAML parser  libyaml
Dependencies pydantic 2.13.4, PyYAML 6.0.3, click 8.4.2, networkx 3.6.1, jinja2 3.1.6, watchfiles 1.2.0, ruamel.yaml 0.19.1
```

The first line is `netviz <version>` and nothing else, so `netviz --version | cut -d' ' -f2`
keeps working.

Graphviz has four states, and the line distinguishes them because they need different
answers: a version and a path (fine), `not found` (install it, or set `NETVIZ_DOT` — the
`dot`, `mermaid` and `json` formats still work), or `unknown` with the reason `dot -V` gave,
which is usually a `NETVIZ_DOT` pointing at something that is not Graphviz.

## The JSON form

<!-- norun: same reason as above -- every value is a property of the reader's machine -->
```console
$ netviz version --json
{
  "schemaVersion": 1,
  "netviz": "0.0.2",
  "python": {
    "version": "3.12.3",
    "implementation": "CPython",
    "executable": "/usr/local/bin/python3.12"
  },
  "graphviz": {
    "version": "2.43.0",
    "path": "/usr/bin/dot",
    "error": null
  },
  "platform": {
    "description": "Linux-6.8.0-generic-x86_64-with-glibc2.39",
    "system": "Linux",
    "machine": "x86_64",
    "os": "posix"
  },
  "yamlParser": "libyaml",
  "dependencies": {
    "pydantic": "2.13.4",
    "PyYAML": "6.0.3",
    "click": "8.4.2",
    "networkx": "3.6.1",
    "jinja2": "3.1.6",
    "watchfiles": "1.2.0",
    "ruamel.yaml": "0.19.1"
  }
}
```

`graphviz` is an object rather than a string because "absent" and "present but unaskable"
are different states: `version` is `null` in both, and `path` tells them apart. A `null`
`version` with a non-null `path` means `error` says what went wrong.

`schemaVersion` follows the same rule as the other JSON documents netviz writes — adding a
key does not bump it, changing or removing one does. See
[`docs/releasing.md`](../releasing.md#what-is-public-api).

## Arguments

<!-- generated: arguments version -->
*Takes no positional arguments.*
<!-- /generated -->

## Options

<!-- generated: options version -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--json` | — | off | Emit the report as a JSON object instead of aligned text. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The report was printed. It is `0` even when Graphviz is missing — that is a fact about the installation, not a fault. |
| `2` | Usage error — an unknown option. |
| `141` | The downstream end of a pipe closed first. |

## See also

* [`docs/getting-started.md`](../getting-started.md#on-windows-and-macos) — installing
  Graphviz, and `NETVIZ_DOT` when it does not land on `PATH`.
* [`docs/releasing.md`](../releasing.md) — what a version number promises and which
  surfaces it promises it about.
* [`CHANGELOG.md`](../../CHANGELOG.md) — what changed between two of these numbers.
* [`docs/testing.md`](../testing.md) — `NETVIZ_YAML_LOADER`, the switch behind the
  `YAML parser` line.
