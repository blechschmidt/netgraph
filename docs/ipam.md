# Address-space health with `netviz ipam`

[`netviz list subnets`](commands/list.md) enumerates the prefixes an
inventory happens to contain. It does not say whether the address plan is
*healthy*. `netviz ipam` answers the three questions that come next:

> **How full is every prefix? What is left inside one? And what is broken?**

```
netviz ipam [OPTIONS]
```

Nothing is probed and no device is contacted. Every number comes from the same
prefix derivation the layer-3 diagram draws and the validator reasons about —
[`netviz.subnets`](../src/netviz/subnets.py) — so a utilisation figure, a
subnet node in a rendering and an addressing finding can never tell three
different stories.

---

## Contents

- [Utilisation](#utilisation)
- [Free space](#free-space)
- [Finding the next block](#finding-the-next-block)
- [Aggregation](#aggregation)
- [Conflicts](#conflicts)
- [Output formats](#output-formats)
- [Options](#options)
- [Exit codes](#exit-codes)

---

## Utilisation

With no options, `netviz ipam` prints one row per derived prefix:

<!-- run: -->
```console
$ netviz -i examples/campus ipam --family ipv4
VRF   PREFIX           IP  VLANS  HOSTS  USED  FREE    UTIL  DEVICES
----  ---------------  --  -----  -----  ----  ----  ------  -------
-     10.1.0.0/30       4  -          2     2     0  100.0%        2
-     10.1.10.0/24      4  10       254     3   251    1.2%        3
-     10.1.20.0/24      4  20       254     2   252    0.8%        2
-     10.2.0.0/30       4  -          2     2     0  100.0%        2
...
-     192.0.2.1/32      4  -          1     1     0  100.0%        1
...
-     198.51.100.8/30   4  -          2     2     0  100.0%        2
mgmt  10.1.99.0/24      4  99       254     4   250    1.6%        4
mgmt  10.2.99.0/24      4  99       254     3   251    1.2%        3
mgmt  10.3.99.0/24      4  99       254     3   251    1.2%        3

conflicts
no problems found
```

| Column | Meaning |
|---|---|
| `VRF` | The routing instance the prefix is in, shown only when something is in one; `-` is the global instance. |
| `PREFIX` | The derived prefix. Grouping is by prefix *and* instance, exactly as `list subnets` groups. |
| `IP` | Address family, `4` or `6`. |
| `VLANS` | Every VLAN an interface addressed in the prefix belongs to, compacted (`10,20,99` / `100-104`). `-` for a routed or untagged prefix. |
| `HOSTS` | Usable host addresses — see [sizing](#sizing) below. |
| `USED` | Distinct addresses the inventory configures inside the prefix. |
| `FREE` | `HOSTS - USED`. |
| `UTIL` | `USED / HOSTS`, yellow past 80 %, red past 95 %. |
| `DEVICES` | Distinct elements holding an address in the prefix. |

Rows are sorted by family, then network address, then prefix length — the same
deterministic order `list subnets` and the layer-3 graph use, so two runs of the
command over an unchanged tree produce byte-identical output.

### Routing instances

A VRF is a routing table of its own, so it is an address space of its own
([`docs/schema.md` §16.1](schema.md#161-vrfs--routing-instances)). Everything on
this page therefore works per instance:

* one row per `(instance, prefix)`, so the same prefix in `blue` and in the global
  table is sized, counted and reported twice — which is what it is;
* `--aggregate` never folds across instances: two halves of a supernet in two
  tables do not fill it, they are two plans that happen to be adjacent on paper;
* the conflict rules partition the same way, so an address is only in conflict
  with an address in its own instance (`E004`, `W106`, `W130`, `W131`).

`--free` and `--next-free` answer for **every** instance at once, which is the
conservative reading: a block that is free in one table and used in another is
not one to hand out without saying which table was meant.

## Sizing

`HOSTS` is not `2^n`. The rules are the ones the protocols actually specify:

| Prefix | Usable | Why |
|---|---|---|
| IPv4 `/1`–`/30` | `2^n - 2` | The all-zeros address is the network, the all-ones the broadcast. |
| IPv4 `/31` | `2` | RFC 3021 gives both addresses to the two routers of a point-to-point link. |
| IPv4 `/32` | `1` | A host route. |
| IPv6 `/1`–`/126` | `2^n - 1` | No broadcast address, but RFC 4291 §2.6.1 reserves the all-zeros interface identifier as the subnet-router anycast address. |
| IPv6 `/127` | `2` | RFC 6164, the IPv6 point-to-point link. |
| IPv6 `/128` | `1` | A host route. |

An IPv6 prefix is too large to print, so anything with 20 or more host bits is
rendered as a power of two — a `/64` shows `2^64`, not twenty digits. A prefix
that is in use but rounds to zero is shown as `<0.1%` rather than `0.0%`,
because "empty" and "two hosts in a `/64`" are different facts and only one of
them means the prefix can be reclaimed.

`USED` counts distinct *addresses*, not placements: an address configured on
two elements is one address two elements are fighting over, and it occupies one
slot either way. The fight itself is reported as a
[conflict](#conflicts).

---

## Free space

`--free PREFIX` subtracts what is allocated from a prefix and prints the holes
as the fewest CIDR blocks that cover them:

<!-- run: -->
```console
$ netviz -i examples/campus ipam --free 10.1.0.0/22
BLOCK          IP  HOSTS
-------------  --  -----
10.1.0.4/30     4      2
10.1.0.8/29     4      6
10.1.0.16/28    4     14
10.1.0.32/27    4     30
10.1.0.64/26    4     62
10.1.0.128/25   4    126
10.1.1.0/24     4    254
10.1.2.0/23     4    510
free space in 10.1.0.0/22: 8 block(s), 1 allocation(s) already carved out
```

Allocation happens **a subnet at a time**: a prefix nested inside `PREFIX`
consumes the whole of itself, because the 250 free addresses in a `/24` holding
four hosts are not space anyone will hand to another department. `10.1.0.0/30`
above is what swallowed the first four addresses.

An address that falls inside `PREFIX` while its *own* prefix does not — a
summary configured as `10.0.0.1/8` inside a `10.0.0.0/16` plan — cannot consume
its prefix without consuming the whole plan, so it consumes a host route
instead.

Adjacent free blocks are fused before they are reported: two free `/25`s appear
as the `/24` they form, because that is the block that can actually be handed
out. `PREFIX` may be written with host bits set — `--free 10.1.0.1/22` means the
`10.1.0.0/22` you were looking at.

---

## Finding the next block

`--next-free` is the operation an engineer actually performs when adding a
device. It prints one prefix and nothing else, so it composes:

<!-- run: -->
```console
$ netviz -i examples/campus ipam --next-free 10.1.0.0/16
10.1.1.0/24
$ netviz -i examples/campus ipam --next-free 10.1.10.0/23 --size /26
10.1.11.0/26
$ netviz -i examples/campus ipam --next-free 2001:db8:1::/48
2001:db8:1:1::/64
```

`--size` accepts `24` or `/24`. Left out, it defaults to a `/24` for IPv4 and a
`/64` for IPv6 — RFC 4291 §2.5.4 makes the `/64` the unit of an IPv6 plan, since
SLAAC does not work in anything longer.

The search walks the free list in address order, so the block it returns is the
lowest one available. It never enumerates candidates, which is what makes
`--next-free 2001:db8::/32 --size 64` — a search across 2^32 possible blocks —
return immediately.

When there is no room, the command says so on stderr and exits 1:

<!-- run: rc=1 -->
```console
$ netviz -i examples/campus ipam --next-free 10.1.10.0/24 --size 8
error: no free /8 inside 10.1.10.0/24; run 'netviz ipam --free 10.1.10.0/24' to see what is left
```

---

## Aggregation

`--aggregate` collapses sibling prefixes that between them fill their supernet,
so a large inventory produces a summary rather than a wall of `/24`s:

<!-- run: -->
```console
$ netviz -i examples/campus ipam --aggregate --family ipv6
PREFIX              IP  VLANS  HOSTS  USED  FREE    UTIL  DEVICES  PARTS
------------------  --  -----  -----  ----  ----  ------  -------  -----
2001:db8::1/128      6  -          1     1     0  100.0%        1      -
2001:db8::2/127      6  -          2     2     0  100.0%        2      2
2001:db8:1::/64      6  -       2^64     2  2^64   <0.1%        2      -
...
2001:db8:ff:2::/63   6  -       2^65     4  2^65   <0.1%        4      2

conflicts
no problems found
```

`PARTS` is how many declared prefixes the row stands for. Two prefixes are
siblings when they are the two halves of one supernet, and a supernet is only
collapsed when **both** halves are declared — at that point it holds no address
the plan has not already accounted for. A supernet with one half declared is
left alone, because collapsing it would let the summary claim space that is in
fact free. The pass repeats to a fixed point, so four adjacent `/26`s become one
`/24` rather than two `/25`s.

The aggregate's `HOSTS` is the **sum of its children's**, not the capacity of
the supernet. Two `/25`s really do lose four addresses to network and broadcast
between them where a `/24` drawn over them loses two; the sum is what the plan
can hold, so it is what is reported.

---

## Conflicts

The second half of the default report is the address-plan conflicts. They are
**not computed here**: `netviz ipam` calls
[`netviz.validate`](../src/netviz/validate.py) and filters the findings to
the rules that are about addressing. There is one implementation of "is this
address plan sound", and `netviz validate` and `netviz ipam` are two views
of it — so a suppression in `netviz.toml` or a `netviz/ignore` annotation
silences a conflict here exactly as it does there, and a rule re-graded per
inventory is reported at the severity the inventory chose.

| Conflict | Rule | Reimplemented? |
|---|---|---|
| A duplicate host address within a prefix, in one broadcast domain | [`E004`](validation-rules.md#e004--duplicate-ip-address) (`NV-A004`) | No — the existing rule is called. |
| The same address claimed twice within a prefix, across broadcast domains | [`W106`](validation-rules.md#w106--one-address-claimed-twice-in-a-subnet) (`NV-A009`) | No — the existing rule is called. |
| Prefixes that overlap but are not nested | [`W130`](validation-rules.md#w130--prefix-claimed-by-two-broadcast-domains) (`NV-A010`) | New rule. |
| A nested prefix whose parent is declared in a different VLAN | [`W131`](validation-rules.md#w131--nested-prefix-in-a-different-broadcast-domain) (`NV-A011`) | New rule. |
| An address configured outside every prefix on its link | [`W132`](validation-rules.md#w132--address-outside-every-prefix-on-its-link) (`NV-A012`) | New rule. |
| A gateway / first hop outside its own subnet | [`E020`](validation-rules.md#e020--first-hop-is-not-on-link) (`NV-A013`) | New rule. |

The first two rows are the "duplicate host addresses within a prefix" check.
The validator already distinguishes a clash inside one broadcast domain — an
error, because nothing about it is deliberate — from the same address claimed
across two, which is a warning because a prefix re-used per VLAN is a real
design. Reproducing that distinction here would have meant a second
implementation that could disagree with the first, so the two existing rules are
called instead.

<!-- run: -->
```console
$ netviz -i tests/fixtures/invalid/w131-nested-prefix-other-domain.yaml ipam
PREFIX       IP  VLANS  HOSTS  USED   FREE   UTIL  DEVICES
-----------  --  -----  -----  ----  -----  -----  -------
10.0.0.0/16   4  10     65534     2  65532  <0.1%        2
10.0.5.0/24   4  20       254     2    252   0.8%        2

conflicts
warnings (1):
  w131-nested-prefix-other-domain.yaml#3:73  W131  subnet '10.0.5.0/24' sits inside '10.0.0.0/16', but the two are used in different broadcast domains: '10.0.5.0/24' in VLAN 20 and '10.0.0.0/16' in VLAN 10. Hosts in '10.0.0.0/16' treat every address of '10.0.5.0/24' as on-link, so they will ARP for it instead of routing to it.

1 warning
```

`--conflicts` prints the list on its own, without the utilisation table.

### Why "overlapping but not nested" is a VLAN question

Two CIDR prefixes are always either disjoint or nested — that is what makes CIDR
a tree. The overlap an operator means by "these subnets overlap" is therefore
not about the bits: it is *one* prefix claimed by two segments that cannot reach
each other. `W130` reports exactly that, and `W131` reports the nested variant
where the parent is on a different VLAN from the child.

Both rules only compare interfaces that declare a `vlan` block. A host on an
access port declares none — its broadcast domain is a property of the switch it
is cabled to, not of its own document — so counting "untagged" as a domain of
its own would fire on the ordinary pairing of a router sub-interface with the
hosts it serves.

### Declaring a gateway

`E020` needs something to check, so the `ipv4`/`ipv6` containers carry an
optional `gateway`:

```yaml
interfaces:
  - name: eth0
    type: ethernet
    mtu: 1500
    ipv4:
      addresses: [10.0.0.10/24]
      gateway: 10.0.0.1          # must be inside 10.0.0.0/24
    ipv6:
      addresses: [2001:db8::10/64]
      gateway: fe80::1           # link-local: on-link by definition, exempt
```

It is written **without** a prefix length, and it is the one field of those
containers RFC 8344 does not define — a default route lives in `ietf-routing`,
not in `ietf-ip`. See [§6.2.3 of the schema](schema.md#623-ipv4--ipv6).

---

## Output formats

`-F` (also spelled `--format` or `--output-format`) takes `table`, `json` or
`csv`.

**JSON** carries both halves of the default report in one document:

<!-- norun: a jq pipeline -->
```console
$ netviz -i examples/campus ipam -F json | jq '.subnets[0], (.conflicts|length)'
{
  "prefix": "10.1.0.0/30",
  "family": "ipv4",
  "vlans": [],
  "capacity": 2,
  "assigned": 2,
  "free": 0,
  "utilisation": 1.0,
  "devices": 2,
  "aggregated": []
}
0
```

`capacity` and `free` are exact integers, not the abbreviated `2^64` the table
prints. `utilisation` is a fraction in `[0, 1]`, or `null` for a prefix with no
capacity. `aggregated` lists the prefixes an `--aggregate` row stands for.
Conflict entries have the same shape as the `findings` array of
[`netviz validate -F json`](ci.md).

**CSV** holds one table, because that is what a CSV *is*. The default emits the
utilisation rows — the half a spreadsheet or an `awk` script wants — and notes
on stderr how many conflicts were left out:

<!-- run: -->
```console
$ netviz -i examples/campus ipam -F csv --family ipv6
prefix,family,vlans,capacity,assigned,free,utilisation,devices
2001:db8::1/128,ipv6,,1,1,0,1.000000,1
...
```

For the conflicts as CSV, ask for them on their own:

<!-- run: -->
```console
$ netviz -i examples/campus ipam --conflicts -F csv
rule,alias,severity,element,file,message
```

`--free` and `--next-free` honour all three formats too. Line endings are `\n`
rather than CSV's nominal `\r\n`, to match every other format this CLI writes.

The global `--color` / `--no-color` and `--quiet` flags apply throughout:
colour is dropped when the stream is not a terminal or `NO_COLOR` is set, and
`--quiet` silences the stderr commentary without ever touching the data on
stdout.

---

## Options

| Option | Default | Effect |
|---|---|---|
| `--free PREFIX` | — | List the unallocated CIDR blocks inside `PREFIX` instead of the utilisation table. |
| `--next-free PREFIX` | — | Print the first free block inside `PREFIX`, and nothing else. |
| `--size LENGTH` | `/24` (v4), `/64` (v6) | Block size `--next-free` looks for. `24` and `/24` both work. |
| `--aggregate` | off | Collapse sibling prefixes that fill their supernet into one row. |
| `--conflicts` | off | Report only the conflicts, without the utilisation table. |
| `--family {all,ipv4,ipv6}` | `all` | Restrict the utilisation table to one address family. |
| `-F`, `--format`, `--output-format` | `table` | `table`, `json` or `csv`. |

`--free` and `--next-free` ask different questions and are rejected together,
as are `--size` without `--next-free` and `--aggregate`, `--conflicts` or
`--family` alongside a free-space query. A flag that was quietly ignored would
be worse than an error: you would believe you had asked for something you did
not get.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The report was produced; nothing is reported as an error. |
| `1` | A conflict is reported at `error` severity, or `--next-free` found no room. |
| `2` | The command line does not make sense (click's own exit code). |

A warning-severity conflict does not fail the run — use
`netviz validate --strict` for a gate that treats every finding as fatal.
