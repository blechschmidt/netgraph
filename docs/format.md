# The canonical form, and `netviz fmt`

An inventory is the source of truth for a network, and it lives in review. That
makes the shape of a file everybody's problem: two people writing the same
switch will indent it differently, order its keys differently and quote its MAC
differently, and every one of those differences shows up in a diff that is
supposed to be about the network.

`netviz fmt` removes the question. There is one canonical form, defined below,
and the tool puts a file into it — the way `gofmt` and `ruff format` do for code.

```
netviz fmt [OPTIONS] [PATHS]...
```

Formatting **never changes what a document means.** Every file is read back with
the same strict loader `validate` and `render` use, and compared against what it
said before; a file that does not survive that comparison is left exactly as it
was. See [Safety](#safety).

---

## Contents

- [The canonical form](#the-canonical-form)
  - [Layout](#layout)
  - [Key order](#key-order)
  - [Quoting](#quoting)
  - [Flow and block](#flow-and-block)
  - [Comments and blank lines](#comments-and-blank-lines)
  - [Empty collections](#empty-collections)
- [Modes](#modes)
- [Which files are formatted](#which-files-are-formatted)
- [Safety](#safety)
- [What `fmt` will not do](#what-fmt-will-not-do)
- [Options](#options)
- [Exit codes](#exit-codes)
- [In CI and pre-commit](#in-ci-and-pre-commit)

---

## The canonical form

### Layout

| Rule | Value |
|---|---|
| Indent | two spaces per mapping level |
| Sequence indent | four columns from the parent key; the `-` sits two in |
| Document separator | `---` between documents, never before the first, never `...` at the end |
| Trailing whitespace | none, on any line, including inside comments |
| Trailing newline | exactly one; no blank lines at the end of a file |
| Blank line before `---` | removed |
| Byte-order mark | removed |
| Line endings | `\n`, on every platform |

So a device reads:

```yaml
apiVersion: netviz.dev/v1alpha1
kind: switch
metadata:
  name: sw-office
spec:
  interfaces:
    - name: port1
      type: ethernet
```

The sequence rule is the one worth stating twice, because YAML permits three
spellings of it. `interfaces:` sits at column 2, its dashes at column 4, and the
keys of each entry at column 6.

The last two rows are also stated twice, because they are the two the comparison
is made in **bytes** for. A byte-order mark and a CRLF line ending both decode to
an identical `str`, so a formatter comparing text would declare such a file
unchanged and let it keep them forever. `netviz fmt` compares the encoded bytes
instead, which is why it reports — and rewrites — both.

That has one consequence on Windows worth knowing before it surprises you. Git's
`core.autocrlf` defaults to `true` there, so every YAML file arrives CRLF, and
`netviz fmt` then rewrites it to LF: `--check` fails on a fresh clone, and
`git status` reports every file as modified after a run. Neither is a netviz
bug and neither has a fix in netviz — the fix is to stop Git translating, with a
`.gitattributes` next to the inventory:

```gitattributes
*.yaml text eol=lf
*.yml  text eol=lf
```

This repository ships exactly that file for exactly that reason.

### Key order

Mapping keys are ordered to match **the field order of
[`docs/schema.md`](schema.md)** — not alphabetically. Alphabetical order would
put `annotations` before `name` and `bridge` before `interfaces`, which is a
consistent way of making every document harder to read than the author left it.
Schema order puts identity first and detail after it.

`apiVersion`, `kind`, `metadata`, `spec` come first, in that order. Below them
each mapping follows the order its section of `docs/schema.md` documents:

```yaml
spec:                   # vendor, model, serial, location,
  vendor: Arista        # interfaces, from, bridge, vlans, forwarding
  model: 7050SX3-48YC8
  interfaces:           # name, range, type, description, enabled, mac,
    - name: Vlan10      # mtu, ipv4, ipv6, vlan, parent, members
      type: vlan
      description: Staff gateway
      ipv4:
        addresses: [10.1.10.1/24]
      vlan:
        mode: access
        access_vlan: 10
      parent: br0
  vlans:
    - id: 10
      name: staff
  forwarding:
    ipv4: true
    ipv6: true
```

The order is not written down a second time inside the formatter. It is read off
the pydantic models, whose field declaration order `docs/schema.md` documents and
`tests/test_docs.py` keeps in step — so a field added to the schema is placed
without anyone teaching `fmt` about it.

Two keys are not model fields, because the loader expands and removes them
before the models see a document. They are placed where `docs/schema.md`
documents them: `spec.from` (§6.6) after `interfaces`, and
`spec.interfaces[].range` (§6.5) after `name`.

A key the schema does not know — a typo, or a field from a future version —
keeps its value and is moved to the **end** of its mapping, after the keys that
are ordered. Trying to keep it near the neighbours it was written between is not
something the output can express, and so not something a second run could
reproduce; see [idempotence](#safety).

Free-form mappings are never reordered. `metadata.labels` and
`metadata.annotations` have keys that belong to the user, and YAML gives their
order no meaning, so `fmt` leaves it as written rather than imposing one.

### Quoting

**Quote only what YAML requires, plus what a reader would otherwise misread.**

What YAML *requires* is not a judgement call, and `fmt` does not make it: the
emitter adds quotes whenever a plain scalar would not survive being read back —
a leading `-`, a `: `, a `#`, a leading or trailing space, an empty string.

On top of that, two things are quoted because plain would be *misleading*:

- **Values a stock YAML reader resolves differently than netviz does.**
  netviz is YAML 1.2 about booleans, so `yes`, `no`, `on` and `off` are
  strings here and booleans nearly everywhere else. They are quoted, so that
  both agree.

  ```yaml
  description: 'no'          # a string in netviz; false to YAML 1.1
  ```

- **MAC addresses**, in all three spellings (`aa:bb:cc:dd:ee:ff`,
  `aa-bb-cc-dd-ee-ff`, `aabb.ccdd.eeff`). Some MACs are read as base-60 integers
  by a YAML 1.1 reader and some are not — `10:20:30:40:50:01` is the integer
  8041827001, `b4:96:91:01:10:01` is a string — and nothing but counting tells
  the two apart. Quoting the whole class means nobody has to count.

  ```yaml
  mac: '00:1b:0d:01:a1:01'
  ```

Everything else is written plain, including IP addresses and prefixes
(`10.1.10.1`, `2001:db8::1/64`), speeds (`10Gbps`) and interface names
(`GigabitEthernet1/0/1`). Quotes that a document had and does not need are
removed.

Version numbers need no rule of their own, though it is worth saying why. `1.0`
is a float to every YAML reader including netviz's, so it is never a string to
begin with; `'1.0'` written with quotes keeps them, because dropping them would
turn a string into a float. `1.2.3` is unambiguously a string to everyone. A
shape rule for dotted numerals would be actively wrong — `10.1.10.1` is one
too, and quoting every IP address in an inventory serves nobody.

When quotes are added they are **single** quotes, which carry no escape
sequences: what is between them is what the value is.

### Flow and block

Block style is never turned into flow. A hand-written block list is a deliberate
shape — one interface per stanza, a comment against an entry — and collapsing it
would destroy the grouping this formatter promises to keep.

Flow style is kept where it was used and still fits. `addresses: [10.1.10.1/24]`
stays on one line; so does `labels: {site: office}`. A flow collection becomes a
block when it cannot stay readable as one:

- it would take the line past **100 columns** (the same width `[tool.ruff]`
  gives Python in this repository), or
- it holds a comment, which flow style cannot carry, or
- it holds another collection.

### Comments and blank lines

**Both are preserved.** This is the point at which a YAML formatter is worth
having at all: a comment explaining why an interface is disabled, and the blank
line separating one interface stanza from the next, are content.

```yaml
      ipv4:
        addresses: [10.1.10.51/24]
        # The Vlan10 SVI on sw-north-dist-01. 'gateway' is checked against this
        # interface's own prefixes by NV-A013; 'netviz ipam' reports it.
        gateway: 10.1.10.1
```

Trailing whitespace *inside* a comment is stripped; nothing else about it
changes. Comments are never re-wrapped, re-indented relative to their key, or
moved between keys. That last promise is checked on every file, not merely
intended: the comment lines of the output are counted against the input's, and
a format that dropped one is refused along with everything else in
[Safety](#safety).

Blank lines are kept as grouping, with two normalisations: two or more in a row
become one, and one immediately after a `---` is removed, since the separator
already separates.

#### Comments and key order

The two can conflict, and comments win.

A YAML comment is attached to the line it follows, not the key it describes, so
a comment written *above* a key cannot be carried along when that key moves —
it would stay put and end up describing whatever landed beneath it. Rather than
produce that, **`fmt` does not reorder the keys of a block that contains a
comment on a line of its own.** Everything else still applies to that block:
indent, quoting, styles, whitespace. Only the ordering defers, on the grounds
that a comment inside a block is the author saying something about the shape
they chose.

```yaml
spec:
  # why this device forwards
  forwarding:
    ipv4: true
    ipv6: true
  interfaces:            # 'forwarding' would sort after this, and does not
    - name: e0
      type: ethernet
```

An end-of-line comment is filed against its own key and moves with it, so it
does not freeze anything:

```yaml
spec:
  interfaces:
    - name: e0
      type: ethernet
  forwarding:  # why this device forwards -- reordered, comment and all
    ipv4: true
    ipv6: true
```

### Empty collections

An empty mapping is written `{}` and an empty sequence `[]`, whichever way they
were spelled. Nothing has one spelling too: `key:` and `key: ~` are both
written `key: null`, which is the form a reader cannot mistake for a line
somebody forgot to finish.

An empty *document* in a multi-document file becomes an explicit `null`. The
loader treats the two identically (`NV-L004`), so nothing downstream can tell;
dropping the document instead would renumber every document after it and move
the line every diagnostic points at.

---

## Modes

<!-- norun: the first four lines rewrite or gate the reader's own tree, and the last is a shell pipeline -->
```console
$ netviz fmt                       # rewrite the inventory -i points at
$ netviz fmt inventory devices/    # rewrite these paths
$ netviz fmt --check inventory     # write nothing; exit 1 and list what differs
$ netviz fmt --diff inventory      # write nothing; print a unified diff
$ ... | netviz fmt --stdin         # format a stream onto stdout
```

**In place** is the default, and the only mode that touches the disk. Each file
is written through a temporary file in the same directory and then renamed, so
an interrupted run leaves either the old file or the new one and never half of
either. It exits 0 when it worked, whether or not anything changed — the same
as `gofmt -w`.

**`--check`** is the CI mode. It writes nothing, lists the files that are not
canonical on stdout, one per line, and exits 1 if there are any:

<!-- norun: CI gates the committed examples/ tree on --check, so this failure listing cannot be reproduced from it -->
```console
$ netviz fmt --check examples
examples/campus/sites/north/hosts/hosts.yaml
examples/home-lab/switches/sw-home.yaml
2 file(s) would be reformatted, 36 already formatted
```

The list is stdout and the tally is stderr, so `netviz fmt --check | xargs
$EDITOR` opens the files and nothing else.

**`--diff`** writes nothing and prints a unified diff. Paths below the working
directory get git's `a/`/`b/` prefixes, so the output is a patch:

<!-- norun: a shell pipeline into git apply -->
```console
$ netviz fmt --diff examples | git apply -R    # or just read it
```

It exits 1 when there is a diff, so it is usable as a gate too.

**`--stdin`** (or the path `-`) reads a YAML stream from stdin and writes the
formatted stream to stdout. Nothing else is printed on success. Discovery does
not apply — a stream is not a file and has no ignore rules — and neither does
in-place rewriting, so this is the mode an editor's "format buffer" command
wants:

<!-- norun: redirects both ways, over paths in the reader's directory -->
```console
$ netviz fmt --stdin < devices/sw.yaml > devices/sw.formatted.yaml
```

---

## Which files are formatted

Exactly the files the loader would read, and no others. `fmt` walks a folder
with the same discovery [`validate`](commands/validate.md) and
[`render`](commands/render.md) use, so all of `docs/schema.md` §2.1
applies unchanged:

- only `*.yaml` and `*.yml`, compared case-insensitively (`NV-L001`);
- nothing under a path component starting with `.` or `_` (`NV-L002`);
- nothing a [`.netvizignore`](schema.md) excludes.

That is a deliberate limit rather than an incidental one. A file the inventory
ignores may not be netviz YAML at all, and rewriting it would be the formatter
exceeding its remit. A path named outright on the command line is still subject
to the ignore rules of the tree it sits in.

A path may be a folder to walk or a single YAML file. The same file reached
through two paths is formatted once.

---

## Safety

Two properties are tested over every document under `examples/` and
`tests/fixtures/`, on every run of the suite (`tests/test_fmt.py`).

**Formatting preserves meaning.** Before anything is written, the formatted text
is parsed again — with `netviz.loader.documents`, the strict loader, not the
round-trip parser that produced it — and compared against the original:

- a document that validates is compared as its **model's JSON**, which is what
  lets `fmt` reorder keys and restyle scalars at all;
- a document that does not validate — an invalid fixture, a file mid-edit — is
  compared as its **raw parsed data**, because `fmt` has to work on files
  `validate` rejects and still may not change what they say.

If the two differ, or the output does not parse, **nothing is written** and the
file is reported as failed. That outcome is a bug in netviz rather than in the
file, and the message says so.

**Comments are preserved.** The whole-line comments of the output are counted
against the input's, and a format that lost one is refused. Model comparison is
blind to comments by construction, so without a check of its own nothing would
notice them all disappearing — which is the one failure a round-trip formatter
must not have.

**Formatting is idempotent.** Formatting twice produces the same bytes as
formatting once. This is not a nicety: without it `--check` could fail on a file
that `fmt` had just written, and the two modes would disagree about what
canonical means.

---

## What `fmt` will not do

It canonicalises documents; it does not repair them.

The clearest case is a scalar netviz already misreads. `1:02` is the integer
62 to a YAML 1.1 resolver, and so it is to netviz's — quoting it would make it
the string `1:02`, which is very likely what the author meant and is
categorically not `fmt`'s call to make. The same goes for a MAC that lands on
the base-60 pattern. Those are findings for
[`netviz validate`](validation-rules.md), which can report them without
silently rewriting them.

It also does not:

- add, remove or reorder documents;
- sort sequences, or the free-form mappings in `labels` and `annotations`;
- re-wrap a `|` or `>` block scalar, or turn one into a quoted string;
- reorder the keys of a block you have written a comment inside;
- touch a file it cannot parse — a syntax error is reported, not guessed at.

---

## Options

| Option | Effect |
|---|---|
| `--check` | Write nothing. List the files that are not canonical, and exit 1 if there are any. |
| `--diff` | Write nothing. Print a unified diff of what would change, and exit 1 if there is one. |
| `--stdin` | Format the stream on stdin onto stdout. The path `-` means the same. |

`--check` and `--diff` cannot be combined.

The global `-i`/`--inventory` decides what is formatted when no `PATHS` are
given; `-q`/`--quiet` suppresses the tally without suppressing the file list,
the diff, or any error.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything is canonical, or was made canonical. |
| `1` | `--check` or `--diff` found a file that is not canonical, or some file could not be formatted. |
| `3` | A path does not exist, or a stream on stdin is not well-formed YAML. |

---

## In CI and pre-commit

This repository gates its own `examples/` tree on `--check`; the step is in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) next to `ruff format
--check`, and prints a `--diff` into the log when it fails.

```yaml
      - run: pip install netviz
      - run: netviz fmt --check inventory
```

Two pre-commit hooks are published, and they differ only in whether the files
come back changed:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/blechschmidt/netviz
    rev: v0.0.1
    hooks:
      - id: netviz-fmt          # rewrites in place; git add and commit again
```

```yaml
      - id: netviz-fmt-check    # reports only; nothing is rewritten
```

Both take the staged filenames rather than walking the tree — formatting is
per-file, unlike [validation](ci.md#pre-commit), where a cable is only dangling
when compared against the devices in the *other* files.

`netviz-fmt` rewrites and then relies on pre-commit noticing the modification,
which fails the commit regardless of the exit status. That is the intended
loop: the files come back fixed, and `git add` is the whole remedy.

For an inventory that does not sit at the repository root, restrict `files`
rather than overriding `entry` — unlike `netviz-validate`, this hook is given
the paths to work on:

```yaml
      - id: netviz-fmt
        files: ^inventory/.*\.ya?ml$
```
