# `netgraph ipam`

[`netgraph list subnets`](list.md) says which prefixes exist. `netgraph ipam`
says whether the address plan is *healthy*: how full every prefix is, what is
free inside one, where the next block starts, and what conflicts. This page is
the reference for the command and its modes;
[`docs/ipam.md`](../ipam.md) is the full treatment of the arithmetic, the output
contracts and the worked examples.

---

## Contents

- [Synopsis](#synopsis)
- [The modes, and which flag selects one](#the-modes-and-which-flag-selects-one)
- [Utilisation, the default report](#utilisation-the-default-report)
- [Conflicts are `validate`, filtered](#conflicts-are-validate-filtered)
- [What is left, and where the next block starts](#what-is-left-and-where-the-next-block-starts)
- [Output formats](#output-formats)
- [Exit codes](#exit-codes)

---

## Synopsis

<!-- generated: synopsis ipam -->
```text
netgraph [GLOBAL OPTIONS] ipam [OPTIONS]
```
<!-- /generated -->

## The modes, and which flag selects one

`ipam` prints one of four reports, and the flags are not independent of each
other: `--free` and `--next-free` each *replace* the utilisation table rather than
adding to it.

| Mode | Selected by | Prints |
|---|---|---|
| Utilisation | nothing — the default | One row per prefix, then the conflicts. |
| Conflicts only | `--conflicts` | The conflicts, without the utilisation table. |
| Free space | `--free PREFIX` | The unallocated CIDR blocks inside `PREFIX`. |
| Next block | `--next-free PREFIX` | One prefix, and nothing else. |

The remaining flags modify one of those and are refused when given to another,
because a flag that is quietly dropped is worse than an error — you would
believe you had asked for something you did not get:

* `--size LENGTH` sizes the block `--next-free` looks for. `24` and `/24` both
  work; the default is `/24` for IPv4 and `/64` for IPv6. Given without
  `--next-free` it is a usage error, since nothing else has a size to choose.
* `--aggregate` folds sibling prefixes that between them fill their supernet
  into one row of the utilisation table, and adds a `PARTS` column saying how
  many rows were folded. It is refused with `--free`, `--next-free` and
  `--conflicts`, none of which print a utilisation table for it to fold.
* `--family ipv4|ipv6` restricts the utilisation table to one address family,
  and applies to that table only. `--free` and `--next-free` take their family
  from the prefix you named, and a conflict is a conflict in either family.
* `--free` and `--next-free` together are a usage error: they ask different
  questions.

## Utilisation, the default report

With no options, `ipam` prints how full every prefix is, then the conflicts:

<!-- run: -->
```console
$ netgraph -i examples/campus ipam --family ipv4
VRF   PREFIX           IP  VLANS  HOSTS  USED  FREE    UTIL  DEVICES
----  ---------------  --  -----  -----  ----  ----  ------  -------
-     10.1.0.0/30       4  -          2     2     0  100.0%        2
-     10.1.10.0/24      4  10       254     3   251    1.2%        3
-     10.1.20.0/24      4  20       254     2   252    0.8%        2
...
-     198.51.100.8/30   4  -          2     2     0  100.0%        2
mgmt  10.1.99.0/24      4  99       254     4   250    1.6%        4
mgmt  10.2.99.0/24      4  99       254     3   251    1.2%        3
mgmt  10.3.99.0/24      4  99       254     3   251    1.2%        3

conflicts
no problems found
```

The `VRF` column appears only when something is in one: two routing instances may
hold the same prefix, and without it the two rows would be indistinguishable
(schema §16.1). `-` is the global instance.

`HOSTS` is what the prefix can actually hold, not `2^n`: IPv4 spends two
addresses on the network and the broadcast, except on a `/31` (RFC 3021) and a
`/32`; IPv6 reserves one for the subnet-router anycast address (RFC 4291
§2.6.1). A `/64` prints as `2^64` rather than as twenty digits, and a prefix in
use that rounds to zero prints as `<0.1%`. The per-length rules are tabulated in
[Sizing](../ipam.md#sizing), and what `--aggregate` will and will not fold is in
[Aggregation](../ipam.md#aggregation).

`UTIL` is colourised past the thresholds a capacity plan is usually reviewed
against — yellow past 80 %, red past 95 % — so a screenful of prefixes is
skimmable; `NO_COLOR` or a redirect gives the same numbers without the colour.

## Conflicts are `validate`, filtered

The other half of the report is the conflicts, and they are **not** a second
implementation of anything. `ipam` calls
[`netgraph validate`](../validation.md) and filters to the addressing rules, so a
suppression or a re-grading in [`netgraph.toml`](../configuration.md#validate--how-findings-are-graded)
applies to both commands identically:

| Conflict | Rule |
|---|---|
| Duplicate host address within a prefix | [`E004`](../validation-rules.md#e004--duplicate-ip-address), [`W106`](../validation-rules.md#w106--one-address-claimed-twice-in-a-subnet) — existing rules, called not copied |
| Prefixes that overlap but are not nested | [`W130`](../validation-rules.md#w130--prefix-claimed-by-two-broadcast-domains) |
| A nested prefix whose parent is on another VLAN | [`W131`](../validation-rules.md#w131--nested-prefix-in-a-different-broadcast-domain) |
| An address outside every prefix on its link | [`W132`](../validation-rules.md#w132--address-outside-every-prefix-on-its-link) |
| A `gateway` that is not on-link | [`E020`](../validation-rules.md#e020--first-hop-is-not-on-link) |

Because the severity comes from the same catalogue, `ipam` exits 1 exactly when
one of these is graded an error. `--conflicts` reduces the command to that check,
which is the form to put in CI. Why "overlapping but not nested" is a question
about VLANs rather than about bits is argued in
[docs/ipam.md](../ipam.md#why-overlapping-but-not-nested-is-a-vlan-question).

## What is left, and where the next block starts

Adding a device is two commands — what is left, and where the next block starts:

<!-- run: -->
```console
$ netgraph -i examples/campus ipam --free 10.1.0.0/22
BLOCK          IP  HOSTS
-------------  --  -----
10.1.0.4/30     4      2
10.1.0.8/29     4      6
...
10.1.2.0/23     4    510
free space in 10.1.0.0/22: 8 block(s), 1 allocation(s) already carved out
$ netgraph -i examples/campus ipam --next-free 10.1.0.0/16
10.1.1.0/24
$ netgraph -i examples/campus ipam --next-free 2001:db8:1::/48
2001:db8:1:1::/64
```

The `free space in …` summary is commentary and goes to stderr, so it arrives
before the table on a terminal and after it when the two streams are captured
separately, as above; `-q` drops it and leaves the table alone.

Allocation is counted **per subnet, not per address**: a `/30` with one address
configured in it is carved out of the parent whole, because that is how an
address plan is actually kept.

`--next-free` prints one prefix and nothing else, so it pipes. It walks the free
list rather than enumerating candidates, which is why searching a v6 `/32` for a
free `/64` returns immediately instead of considering 2^32 blocks. When there is
no room it says so and exits 1, naming the `--free` command that shows what is
left:

<!-- run: rc=1 -->
```console
$ netgraph -i examples/campus ipam --next-free 10.1.0.0/30
error: no free /24 inside 10.1.0.0/30; run 'netgraph ipam --free 10.1.0.0/30' to see what is left
```

[Free space](../ipam.md#free-space) and
[Finding the next block](../ipam.md#finding-the-next-block) have the algorithms
and more examples, including `--size` for something other than a `/24`.

## Output formats

`-F table` is for reading. `-F json` carries **both halves** of the report — the
utilisation rows and the conflicts — in one document, which is what a dashboard
or a review script wants. `-F csv` carries one table, because a CSV file has one
header row; in the default mode that is the utilisation table. The spelling
`--format` and `--output-format` are accepted as well as `-F`. Key by key, the
two contracts are in [Output formats](../ipam.md#output-formats).

## Options

<!-- generated: options ipam -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--free` | `PREFIX` | — | List the unallocated CIDR blocks inside PREFIX instead of the utilisation table. |
| `--next-free` | `PREFIX` | — | Print the first free block inside PREFIX, and nothing else. |
| `--size` | `LENGTH` | — | Prefix length --next-free should look for, as '24' or '/24'. [default: /24 for IPv4, /64 for IPv6] |
| `--aggregate` | — | off | Collapse sibling prefixes that fill their supernet into one row. |
| `--conflicts` | — | off | Report only the address-plan conflicts, without the utilisation table. |
| `--family` | `[all\|ipv4\|ipv6]` | `all` | Restrict the utilisation table to one address family. |
| `-F`, `--format`, `--output-format` | `[table\|json\|csv]` | `table` | table is for reading; json and csv are for scripting. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The report was printed and no conflict was graded an error. |
| `1` | A conflict is an error, or `--next-free` found no room. |
| `2` | Usage error — an unparseable prefix, or two flags that ask different questions (`--free` with `--next-free`, `--size` without `--next-free`, `--aggregate` or `--family` with a mode that prints no utilisation table). |
| `3` | The inventory could not be discovered or read at all. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

## See also

* [`docs/ipam.md`](../ipam.md) — the sizing rules per prefix length, how free
  space and aggregation are computed, the JSON and CSV contracts, and why
  "overlapping but not nested" is a VLAN question.
* [`netgraph list`](list.md) — `list subnets` is the same grouping without the
  arithmetic.
* [`docs/validation.md`](../validation.md) and
  [`docs/validation-rules.md`](../validation-rules.md) — the rules the conflicts
  half reports, and how to suppress or re-grade one.
* [`docs/ci.md`](../ci.md#exit-codes) — using `--conflicts` as a build step.
