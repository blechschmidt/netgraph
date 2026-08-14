# `netgraph log`

`netgraph log` is `git log` for the **network** rather than for the files. It
lists the commits that touched the inventory, newest first, and beside each one a
sentence saying what that commit did to the network:

<!-- norun: the output is this repository's own history, which moves every commit -->
```console
$ netgraph -i net log
a1b2c3d4e  2026-08-14  Ada Byrne   Bring the spine up to four members
           2 devices added, 4 links added
9f8e7d6c5  2026-08-13  Sam Patel   Move the lab out of VLAN 10
           6 addresses moved, 1 link removed
4c3b2a190  2026-08-11  Ada Byrne   Retire rack B
           24 devices removed, 24 links removed
```

Each summary is a real changeset, computed by the same code
[`netgraph plan`](plan.md) and [`netgraph diff`](diff.md) use: the revision on
either side of the commit is read, both are loaded, and the difference between
the two *networks* is counted. A commit that reformatted a file, moved a
document between files or added a comment says `no change to the network`,
because that is what it did.

Every revision is read out of the object database with `git archive`. The
working tree, the index and the checked-out branch are never touched, so this is
safe to run in a dirty tree and safe to run while an editor has the folder open.

## Contents

- [Synopsis](#synopsis)
- [Which commits are listed](#which-commits-are-listed)
- [Ranges](#ranges)
- [What a summary counts](#what-a-summary-counts)
- [When a revision cannot be read](#when-a-revision-cannot-be-read)
- [The bound](#the-bound)
- [The JSON form](#the-json-form)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis log -->
```text
netgraph [GLOBAL OPTIONS] log [OPTIONS]
```
<!-- /generated -->

---

## Which commits are listed

The ones that changed a file **inside the inventory directory**, and no others.
If the inventory is `net/` inside a repository that also holds Terraform and a
README, a commit that touched only those is not history this command has an
opinion about.

The inventory directory is wherever the global `-i/--inventory` points. When it
*is* the repository root, every commit is a candidate.

A merge is listed like any other commit, and its changeset is computed against
its **first** parent — the same side `git log -p` shows, and what "the state this
landed on" means to a reader.

---

## Ranges

`--from` and `--to` are `git log a..b`, spelled out:

| Invocation | What it lists |
|---|---|
| `log` | the newest 20 commits that touched the inventory |
| `log -n 5` | the newest 5 |
| `log --from v1.0` | everything since the tag, excluding the tag's own commit |
| `log --from v1.0 --to v2.0` | the commits between the two tags |
| `log --to HEAD~10` | the newest 20 as of ten commits ago |

`--from` is **exclusive**, because the revision it names is the state the oldest
listed commit is drawn against rather than a commit being reported on.

---

## What a summary counts

Elements, by what sort of thing they are, and what happened to them:

```text
3 devices added, 1 link removed, 2 addresses moved
```

Not actions — [`netgraph plan`](plan.md) already says `+ 3 to add, - 1 to
destroy`, and that is the right summary for a changeset somebody is about to
apply. Reading a history, the question is what the network *gained and lost*, so
the noun comes first and a cable is called a link.

One special case earns its own noun: an update whose every changed field sits
under an interface's addresses is counted as `N addresses moved` rather than as
`N devices changed`. Readdressing is the most common thing that happens to a
network that is otherwise standing still, and "2 devices changed" does not say
it.

`--no-renames` counts a rename as a removal and an addition, exactly as it does
on `plan` and `diff`.

`--no-summary` lists the commits alone. It reads no revision at all, so it is
the fast form: one `git log` and nothing else.

---

## When a revision cannot be read

It is shown, with the reason, and marked:

```text
4eaf3edb2  2026-08-13  Sam Patel   Split the site file
           ! the inventory at 8f1a4ec79 does not load: mapping values are not
             allowed here
```

A commit that broke the tree is precisely the one somebody reading a history is
looking for, so it is never skipped and never summarised as "no change". The
exit status stays 0: the history was listed, and one of the things it says is
that this revision does not load.

Two other edges have their own wording:

- **The inventory folder does not exist at that revision.** A repository that
  grew its `net/` directory at some commit legitimately has nothing before it.
  The commit that *added* the folder therefore reads as everything being added,
  with `(the inventory did not exist before this commit)` after the summary,
  rather than as a failure to read the parent.
- **Nothing has ever touched the inventory.** Said in one line, rather than
  printed as an empty list.

---

## The bound

Summarising a commit means loading two inventories, and drawing one — in
[the editor's timeline](web.md#the-history-timeline) — means two Graphviz runs
as well. So there is a ceiling on how much history one invocation will read:

```toml
# netgraph.toml
[history]
max-revisions = 250
```

100 by default, overridable per invocation with `--max-revisions`. A range wider
than the bound is refused **before anything is read**:

```text
error: v1.0..HEAD holds 312 revisions of the inventory, more than the bound of
100; narrow the range, pass a limit, or raise 'max-revisions' in the [history]
table of netgraph.toml
```

`-n/--limit` is not the same thing. A limit *narrows* the range and is always
honoured; the bound is about refusing a range nobody could look at.

---

## The JSON form

`-F json` (or `--json`) prints one document, with the range it was asked for and
one object per commit:

```json
{
  "root": "/srv/net",
  "range": {"from": null, "to": "HEAD"},
  "maxRevisions": 100,
  "commits": [
    {
      "hash": "a1b2c3d4e5f6...",
      "abbrev": "a1b2c3d4e",
      "parents": ["9f8e7d6c5..."],
      "author": "Ada Byrne",
      "email": "ada@example.com",
      "date": "2026-08-14T09:31:04+01:00",
      "subject": "Bring the spine up to four members",
      "tree": "e592e7c09cea...",
      "summary": "2 devices added, 4 links added",
      "error": null,
      "note": null,
      "changes": {"create": 6, "update": 0, "delete": 0, "rename": 0}
    }
  ]
}
```

`tree` is the hash of the inventory *directory* at that commit. Two commits that
leave the inventory identical share it, which is what the editor keys its frame
cache by — and what a script wanting to skip a revision it has already processed
should key on too.

`error` is `null` or the one-line reason the revision could not be read.
`changes` is absent under `--no-summary`, along with `summary`, because nothing
was read to produce them.

---

## Options

<!-- generated: options log -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--from` | `REV` | — | Oldest revision to list, exclusive — as 'git log a..b' means it. The revision itself is the state the oldest listed commit is drawn against, so it is not listed. |
| `--to` | `REV` | `HEAD` | Newest revision to list, inclusive. |
| `-n`, `--limit` | `INTEGER, >= 1` | `20` | List at most this many commits, newest first. 'netgraph log -n 1' is the last change. |
| `--max-revisions` | `INTEGER, >= 1` | [history] max-revisions in netgraph.toml, or 100 | Refuse a range holding more revisions than this rather than reading them all. A limit narrows the range; this bounds what may be asked for. |
| `--summary`, `--no-summary` | — | `--summary` | Say what each commit did to the network, which means loading the inventory on both sides of it. --no-summary lists the commits alone and reads nothing. |
| `--no-renames` | — | off | Count every rename as a removal and an addition rather than detecting it. |
| `-F`, `--output-format` | `[text\|json]` | `text` | text is for reading; json is for a script. |
| `--json` | — | off | Shorthand for '-F json'. |
<!-- /generated -->

## Exit codes

| Code | When |
|---|---|
| 0 | The history was listed. Including when one of its revisions does not load. |
| 1 | There is no repository, `git` cannot be run, a revision does not resolve, or the range is wider than the bound. |

## See also

- [`netgraph diff`](diff.md) — one pair of revisions, drawn.
- [`netgraph plan`](plan.md) — one pair of revisions, in full.
- [`netgraph web`](web.md#the-history-timeline) — the same list as a scrubber
  under the canvas, with the diagram repainting as you step.
- [`netgraph config`](config.md) — where `[history] max-revisions` is read from.
