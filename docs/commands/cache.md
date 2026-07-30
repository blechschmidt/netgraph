# `netgraph cache`

Every netgraph command re-reads the inventory from disk, because the files are the
only state it trusts. Turning those bytes back into validated models is the
expensive half of that, and on a tree where one file changed it is also the
*repeated* half — so a file that has been parsed once is remembered, keyed by its
own contents.

`netgraph cache` is the two things you can do to that cache from outside:
[`info`](#netgraph-cache-info) says where it is and what is in it, and
[`clear`](#netgraph-cache-clear) empties it. Neither is part of a normal
workflow. The cache needs no maintenance: an entry is keyed by the file's bytes
*and* by the code that read them, so it cannot go stale, and
[`--no-cache`](#--no-cache) rules it out of an experiment without deleting
anything.

What is stored, where, and how to switch it off for a whole environment is in
[`docs/configuration.md`](../configuration.md#cache--remembering-parsed-files).

---

## Synopsis

<!-- generated: synopsis cache info -->
```text
netgraph [GLOBAL OPTIONS] cache info [OPTIONS]
```
<!-- /generated -->

<!-- generated: synopsis cache clear -->
```text
netgraph [GLOBAL OPTIONS] cache clear [OPTIONS]
```
<!-- /generated -->

## `netgraph cache info`

Nothing is loaded and nothing is written; this reads the cache directory and
describes it.

<!-- norun: the directory and the identity are per machine and per installation -->
```console
$ netgraph -i examples/campus cache info
cache
SETTING        VALUE
-------------  -------------------------------------------------------
enabled        true
directory      /home/ada/.cache/netgraph/inventories/campus-9f21c0be44a1
location from  ~/.cache
entries        15
size           15.7 kB
stale entries  0 (0 B)
maximum size   67.1 MB

identity (an entry is keyed by this and the file's contents)
INPUT       VALUE
----------  ---------------------
format      1
netgraph    0.1.0
apiVersion  netgraph.dev/v1alpha1
parser      CStrictSafeLoader
pydantic    2.13.4
python      3.12
pyyaml      6.0.3
sources     6773bbaa227ef5a1
```

The two tables answer different questions.

**The first is about this inventory's cache.** `enabled` names the reason when it
is off — `--no-cache`, `NETGRAPH_NO_CACHE`, or `[cache] enabled = false` in
`netgraph.toml`. `location from` names the rung of the ladder that chose the
directory, which is the only thing worth knowing about a path that is not where
you expected: `NETGRAPH_CACHE_DIR`, `netgraph.toml [cache] dir`,
`XDG_CACHE_HOME`, or the platform default. `stale entries` are the ones written
by a netgraph that has since changed — they are never read, and the next sweep
reclaims them first.

**The second is the identity**: everything besides a file's own bytes that
decides what that file means. If the cache keeps missing, one of these lines is
changing between runs, and `sources` — a digest over netgraph's own source files
— is the one that changes when you are editing netgraph itself. That is
deliberate: a cache keyed on the version number alone would serve conclusions
drawn by code you have since rewritten.

An inventory nothing has loaded yet reports zero entries and says so; the next
command that loads it fills the cache.

## `netgraph cache clear`

<!-- norun: the count and the directory are the reader's own -->
```console
$ netgraph -i examples/campus cache clear
cleared this inventory: 15 entries under /home/ada/.cache/netgraph/inventories/campus-9f21c0be44a1
15 entries, 15.7 kB freed
```

Only `*.ngc` files are removed, and then the directories that held them. A cache
directory somebody has pointed at their home folder by mistake therefore loses
its cache and nothing else.

`--all` clears every inventory's cache under the same base directory, which is
what to reach for when reclaiming space rather than investigating one tree:

<!-- norun: the count is the reader's own -->
```console
$ netgraph cache clear --all
cleared every inventory: 214 entries under /home/ada/.cache/netgraph/inventories
214 entries, 3.1 MB freed
```

Clearing is never a fix. If an inventory renders wrongly, the cache is not why:
change the file and the key changes with it. Reach for `--no-cache` first — it
proves the point in one run without throwing anything away.

## `--no-cache`

`--no-cache` is a global option and goes before the subcommand, because it is
about the run rather than about the command:

<!-- run: -->
```console
$ netgraph -i examples/home-lab --no-cache validate
no problems found
```

It parses every file and remembers nothing, which is what to use when comparing a
timing against a cold one, or to rule the cache out of a bug report. To switch the
cache off for a whole environment — a CI job, a container image — set
`NETGRAPH_NO_CACHE=1` instead of adding the flag to every invocation, and see
[Turning it off](../configuration.md#turning-it-off).

## What is not cached

Two shapes stay on the slow path forever, and `netgraph cache info`'s counters
say how many files they cost:

* A file declaring a **`kind: template`** (§6.6), because the template is used by
  documents in other files.
* A device inheriting one with **`spec.from`**, because its element is the merge
  of this file with a template that may live anywhere in the tree.

A cache keyed on one file's bytes cannot notice the other file changing, so it
does not try. Neither is anything cached by `netgraph validate --format json`,
`sarif` or `github`: those keep the per-field provenance that lets a finding be
reported at the line that caused it, and that provenance *is* the YAML node tree
a cache entry does not hold.

## Options

<!-- generated: options cache info -->
*No options of its own; the global options apply.*
<!-- /generated -->

<!-- generated: options cache clear -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--all` | — | off | Clear the cache of every inventory, not just this one. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The cache was described, or cleared. |
| `2` | Usage error — an unknown flag, or an unusable `netgraph.toml`. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

## See also

* [`docs/configuration.md`](../configuration.md#cache--remembering-parsed-files) —
  the `[cache]` table, exactly what is stored, and how to disable it in CI.
* [`netgraph watch`](watch.md) — the loop the cache exists for: only the files
  that changed are parsed again.
* [`docs/follow-ups.md`](../follow-ups.md) — entry 14, the measurement the cache
  was built from and what dominates a re-render now.
