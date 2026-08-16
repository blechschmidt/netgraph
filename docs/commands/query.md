# `netgraph query`

`netgraph query` answers the selector language: it prints the elements a query
selects, and nothing else. It is the language's home, but not its only use —
the same expression narrows a render with `--select`, grades a network with
`assert: query`, and drives the editor's search box.

[`docs/query.md`](../query.md) is the grammar reference and the cookbook. This
page is the command.

---

## Synopsis

<!-- generated: synopsis query -->
```text
netgraph [GLOBAL OPTIONS] query [OPTIONS] QUERY
```
<!-- /generated -->

---

## What it is for

Three questions, and they are different:

**"Which elements are these?"** — the default. One fully-qualified name per
line, in load order, so the output pipes into `xargs`, `grep -c` or another
`netgraph` invocation.

<!-- run: cwd=examples/campus -->
```console
$ netgraph query 'kind = router and label.site = north'
sites/north/core/rtr-north-core-01
```

**"How many?"** — `--count` prints the number alone.

<!-- run: cwd=examples/campus -->
```console
$ netgraph query 'kind = switch' --count
10
```

**"Is this true?"** — the exit status. `netgraph query` exits **1** when nothing
matched, so a query is a check:

<!-- run: cwd=examples/campus rc=1 -->
```console
$ netgraph query 'kind = switch and not has address' --count
0
```

An invariant is written as a search for its counterexamples, so *no match* is
the passing case — and a shell that wants it that way inverts the status, or
writes the claim as an [`assert: query`](../testing.md) and lets `netgraph test`
report it.

---

## Printing the sub-objects

A scope asks about interfaces, links, namespaces or zones, and answering with
the elements holding them loses the part of the answer that was asked for.
`--print interfaces` and `--print links` report the sub-objects that satisfied a
scope instead:

<!-- run: cwd=examples/campus -->
```console
$ netgraph query 'interface[type = loopback and has address]' --print interfaces
sites/north/core/rtr-north-core-01:lo0
sites/north/hosts/pc-north-01:lo
sites/north/hosts/srv-north-01:lo
sites/north/hosts/pc-north-02:lo
sites/south/core/rtr-south-core-01:lo0
sites/south/hosts/pc-south-01:lo
sites/south/hosts/srv-south-01:lo
sites/south/hosts/pc-south-02:lo
sites/west/core/rtr-west-core-01:lo0
sites/west/hosts/pc-west-01:lo
sites/west/hosts/srv-west-01:lo
sites/west/hosts/pc-west-02:lo
```

Only the sub-objects matched at **positive** polarity are reported: under a
`not`, a scope's being satisfied is what makes the surrounding term false, and
reporting those interfaces as if they had been selected would be a lie.

---

## `--json`

The whole answer as data, carrying the query that produced it so a saved result
says what it is:

<!-- run: cwd=examples/campus -->
```console
$ netgraph query 'kind = router and label.site = south' --json
{
  "query": "kind = router and label.site = south",
  "count": 1,
  "subject": "elements",
  "matches": [
    {
      "element": "sites/south/core/rtr-south-core-01"
    }
  ]
}
```

With `--print interfaces` each record carries `element` and `interface`; with
`--print links`, `element` and `link`. `--count --json` drops the records and
keeps the number.

---

## The layer

`--layer` picks which view the query is answered against, exactly as it picks
which view `render` draws. It matters:

- at `l3` the graph holds **subnet** nodes, so `kind = subnet` and `prefix in
  10.1.0.0/16` have something to select and a traversal walks through prefixes;
- at `netns` a machine's containers are nodes of their own, so
  `netns[depth > 0]` finds them;
- at `power` a PDU is a node and a feed is a link.

<!-- run: cwd=examples/campus -->
```console
$ netgraph query --layer l3 'kind = subnet and prefix in 10.1.0.0/16' --count
4
```

Given several `--layer` flags the last wins: a query has one answer, and a
command that quietly unioned three views would not be able to say which layer a
match came from.

---

## The filter flags scope the question

`netgraph query` takes the same `--kind`, `--namespace`, `--name`, `--vlan`,
`--neighbors-of` and `--depth` every command that draws the whole inventory
does. Here they narrow the graph the query is *answered against* — "among the
switches, which match this" — rather than being AND-ed into the expression
afterwards. That is what makes `--neighbors-of` useful: it says which part of
the network the question is about.

<!-- run: cwd=examples/campus -->
```console
$ netgraph query --namespace sites/west 'has address' --count
7
```

---

## `--explain`

Prints the grammar and the whole attribute vocabulary, generated from the same
tables the parser checks against, so `--explain` cannot drift from what is
accepted. With the filter flags it also prints the query those flags are sugar
for:

<!-- run: cwd=examples/campus -->
```console
$ netgraph query --explain --neighbors-of sw-north-acc-01 --depth 3
# the filter flags, as the query they are sugar for
within 3 hops of (fqn = sw-north-acc-01 or name = sw-north-acc-01)
...
```

---

## Errors

A query that does not parse is a **usage** error — exit status 2 — reported
before the inventory is read, with the offending column underlined:

<!-- run: cwd=examples/campus rc=2 -->
```console
$ netgraph query 'kind = switch and interface[interface[x]]'
Usage: netgraph query [OPTIONS] QUERY
Try 'netgraph query --help' for help.

Error: Invalid value for 'QUERY': query:1:29: a scope cannot be written inside another scope
  kind = switch and interface[interface[x]]
                              ^^^^^^^^^
  help: an interface has no interfaces; write the terms side by side
```

An inventory with errors in it refuses the query the way every other reading
command does, unless `--force` is given: an answer computed from a broken
inventory is an answer about a network that is not the one described.

---

## Exit status

| Status | Means |
|---|---|
| 0 | The query matched at least one element. |
| 1 | It matched nothing, or the inventory was rejected. |
| 2 | The query is not a query, or an option is wrong. |

---

## Arguments

<!-- generated: arguments query -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `QUERY` | no | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options query -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--layer` | `[physical\|l1\|l2\|l3\|overlay\|routing\|rack\|power\|identity\|netns\|security]` | `l1` | l1 draws the physical topology; l2 annotates it with VLANs; l3 draws IP subnets and the elements addressed in them; overlay draws the tunnels; routing draws the BGP sessions and OSPF adjacencies, clustered by VRF; physical adds the patch panels l1 splices out; rack draws a front elevation per rack; power draws the PDUs and the feeds into everything they power; identity draws the users and groups; netns opens each machine up into the network stacks inside it, joined by their veth pairs; security draws the firewall zones and what the policy lets cross between them. Repeatable for -f html, which draws each layer and puts a switcher over them. |
| `--print` | `[elements\|interfaces\|links]` | `elements` | elements prints what the query selected; interfaces and links print the sub-objects an interface[...] or link[...] scope matched inside them. |
| `--json` | — | off | Report as JSON. |
| `--count` | — | off | Print how many matched, and nothing else. |
| `--explain` | — | off | Print the grammar and the attribute vocabulary instead of running a query, and — with the filter flags — the query they are sugar for. |
| `--namespace` | `NS` | — | Keep only elements in this namespace or below it. Repeatable. |
| `--vlan` | `VID` | — | Keep only elements participating in this VLAN. Repeatable. |
| `--kind` | `[switch\|router\|firewall\|hub\|computer\|server\|adapter\|patchpanel\|pdu\|user\|group]` | — | Keep only elements of this kind. Repeatable. |
| `--name` | `GLOB` | — | Keep only elements whose name matches this glob. Repeatable. |
| `--neighbors-of` | `NAME` | — | Keep only the neighbourhood of this element. |
| `--depth` | `INTEGER, >= 0` | `1` | How many hops --neighbors-of reaches. |
| `--select` | `QUERY` | — | Keep only the elements this query selects, e.g. "kind = switch and not has vrf". The flags above are sugar for the equivalent query and are combined with it; 'netgraph query --explain' prints which. See docs/query.md. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Proceed even when validation failed. The result may not match the files. |
<!-- /generated -->

---

## See also

- [`docs/query.md`](../query.md) — the grammar, the attributes and a cookbook.
- [`netgraph list`](list.md) — the same `--select`, over the tabular subjects.
- [`netgraph render`](render.md) — `--select` beside the other view filters.
- [`netgraph test`](test.md) — the same query as an executable assertion.
