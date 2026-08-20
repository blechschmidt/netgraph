# The selector language

Every part of netviz that has to answer "which elements?" answers it with the
same expression. One grammar, one vocabulary, one implementation
(`netviz/query/`), used by:

| Where | How |
|---|---|
| [`netviz query`](commands/query.md) | `netviz query 'kind = switch and not has vrf'` |
| `render`, `watch`, `show`, `list`, `export`, `report` | `--select '<query>'` |
| [`kind: testsuite`](schema.md#20-test-suites-executable-assertions) | `assert: query` / `query:` on any selector assertion |
| [the editor](commands/web.md) | the search box, and the command palette |

The language is deliberately small. There is no binding, no arithmetic, no
function call and no recursion beyond the finite tree the parser builds — so
every query terminates, none of them can change anything, and the same
expression is safe to run in a pre-commit hook, in a browser on every keystroke,
and inside an assertion nobody will read again for a year.

**It also cannot join or project.** When the question is "which interfaces, and
what are they attached to, and what addresses do they have" — a join followed by
a structured answer — that is the [relational language](nql.md), which the same
`netviz query` command runs when the query begins with `select` or `with`.

- [Grammar](#grammar)
- [Terms](#terms)
- [Operators](#operators)
- [Attributes](#attributes)
- [Scopes](#scopes)
- [Traversal](#traversal)
- [Negation, and what it partitions](#negation-and-what-it-partitions)
- [The filter flags are sugar](#the-filter-flags-are-sugar)
- [Errors](#errors)
- [Limits](#limits)
- [Cookbook](#cookbook)

---

## Grammar

```text
query      := or
or         := and ( "or" and )*
and        := unary ( "and" unary )*
unary      := "not" unary | primary
primary    := "(" query ")"
            | "neighbors" "of" unary
            | "within" NUMBER "hops" "of" unary
            | "reachable" "from" unary
            | DOMAIN "[" query "]"
            | "has" attribute
            | attribute operator value
            | attribute "in" "(" value ( "," value )* ")"
            | "*"
            | word                          -- sugar for: name ~ *word*

operator   := "=" | "==" | "!=" | "~" | "!~" | "=~"
            | "<" | "<=" | ">" | ">=" | "in" | "under"
DOMAIN     := "interface" | "link" | "netns" | "zone"
```

`and` binds tighter than `or`, `not` tighter than both, and a traversal tighter
still. So

```text
within 2 hops of fw-edge and kind = switch
```

is *(everything within two hops of `fw-edge`) and (the switches)* — not a
neighbourhood of switches. Write the parentheses when it is not what you meant.

Keywords are case-insensitive (`AND` works); values are not (`name = SW-01` does
not match `sw-01`).

`netviz query --explain` prints this grammar and the whole attribute
vocabulary, generated from the tables rather than written out, so it cannot
drift from what the parser accepts.

---

## Terms

**A bare word is a name search.** `sw-core` means `name ~ *sw-core*`, which is
what the editor's search box used to do on its own and what anybody types
first. A word that already has a wildcard in it is left as written: `sw-*`
means `name ~ sw-*`.

**`*` matches everything.** Useful as the identity — `netviz query '*'` lists
the inventory — and as the left-hand side of a difference: `* and not kind =
cable`.

**Quoting.** Anything that is not whitespace, a bracket, a comma or an operator
is part of a bare word, so `sites/north`, `10.20.0.0/16`, `label.role`,
`GigabitEthernet1/0/1` and `sw-north-*` all need no quotes. Use single or double
quotes for a value with a space, a comma or a bracket in it, and to use a
keyword as a value: `name = "and"`.

**`has`** tests that an attribute holds at least one value: `has vrf`, `has
address`, `has label.role`. It is the only way to ask about emptiness, and it is
the counterpart of the rule below.

---

## Operators

| Operator | Means |
|---|---|
| `=`, `==` | Equal, at the attribute's type: a number numerically, an address as an address, everything else as exact case-sensitive text. |
| `!=` | Its negation over the whole value set — true when *no* value equals. |
| `~` | Shell glob ([`fnmatch`](https://docs.python.org/3/library/fnmatch.html)), case-sensitive: `name ~ sw-*-01`. |
| `!~` | Its negation, over the whole value set. |
| `=~` | Regular expression, unanchored: `name =~ ^sw-(north\|south)`. |
| `<`, `<=`, `>`, `>=` | Numeric order. Only for a number or a VLAN id; anything else is a parse error rather than a silently false comparison. |
| `in (a, b, c)` | Any of the alternatives. |
| `in 10.20.0.0/16` | For an address attribute: containment in that CIDR. Mixed families are simply false. |
| `under sites/north` | For a path attribute (`namespace`, `file`): that path or anything below it, segment-wise — `sites/north` does not contain `sites/northolt`. |

**Multi-valued attributes.** An element has many addresses, many VLANs, many
interface names. A comparison against a set is **existential** for the positive
operators and therefore **universal** for the negated ones:

```text
address in 10.0.0.0/8      # some address of it is in that prefix
address !~ 10.*            # no address of it starts with 10.
```

An attribute with **no** values is false for every operator, positive or
negative — `has` is how emptiness is asked about. So a device with no addresses
at all matches neither of the two above, and `not (address in 10.0.0.0/8)`,
which is a different query, catches it.

---

## Attributes

### `element` — where a query starts

| Attribute | Type | What it is |
|---|---|---|
| `name` | text | The short name, without the namespace. |
| `fqn` (`id`) | text | The fully-qualified name. |
| `namespace` (`ns`) | path | The folder namespace the document lives in. |
| `kind` | enum | `switch`, `router`, `computer`, `server`, `hub`, `adapter`, `patchpanel`, `pdu`, `user`, `group`, and the derived `subnet`, `tunnel`, `rack`. |
| `type` | enum | `element` for something declared, `subnet` / `tunnel` / `rack` / `aggregate` for a node a layer derived. |
| `description` (`desc`) | text | `metadata.description`. |
| `label.<key>` | text | `metadata.labels.<key>` — `label.role`, `label.site`. |
| `vendor`, `model`, `serial`, `location` | text | The `spec` fields of the same name. |
| `vlan` | vlan | Every VLAN it participates in, links included. |
| `address` (`ip`) | address | Every address on any of its interfaces. |
| `routable-address` | address | The same, loopback and link-local removed. |
| `prefix` | address | The prefix a derived `subnet` node stands for. |
| `interface` | text | The name of each interface. |
| `mac` | text | Each configured MAC. |
| `mtu` | number | Each configured MTU. |
| `vrf` | text | Every VRF it declares or binds an interface to. |
| `netns` | text | Every network namespace it runs ([§23](schema.md#23-network-namespaces-and-veth-pairs)); the initial one is not named. |
| `zone` | text | Every firewall zone ([§24.5](schema.md#245-what-it-draws)). |
| `asn`, `router-id`, `area` | number / text | What it contributes to the control plane. |
| `degree` | number | How many links are incident to it *in this view*. |
| `ports` | number | How many interfaces it declares. |
| `file` (`source`) | path | The inventory-relative path of the document declaring it. |

### `interface[…]`

`name`, `type`, `description`, `enabled` (bool), `address`, `routable-address`,
`mac`, `mtu`, `vlan`, `vlan-mode`, `vrf`, `netns`, `peer`, `element`.

### `link[…]`

`id` (`name`), `kind`, `medium`, `speed`, `length`, `label`, `vlan`, `port` —
the interface on *this* element — and the far end: `peer`, `peer-name`,
`peer-kind`, `peer-namespace`, `peer-port`.

### `netns[…]`

`name`, `parent`, `depth`, `description`, `interface`, `address`.

### `zone[…]`

`name`, `description`, `interface`, `rules`, `translations`, `declared` (bool).

---

## Scopes

`interface[…]`, `link[…]`, `netns[…]` and `zone[…]` are **existential**: the
element matches when *at least one* sub-object satisfies the inner query.

```text
interface[address in 10.20.0.0/16 and not has vrf]
```

selects an element with **an** interface addressed there and not in a VRF. "No
such interface" is `not interface[…]`, which is the only reading under which the
two are negations of one another.

A scope may not contain another scope, and a traversal may not be written inside
one: the graph is walked between elements, and an interface has no interfaces.

`netviz query --print interfaces` prints the interfaces that satisfied a
scope rather than the elements holding them, so the query above answers the
question it was asked in.

---

## Traversal

| Form | Means |
|---|---|
| `neighbors of X` | Every node adjacent to a match of `X`, **not** including the matches. |
| `within N hops of X` | Every node at most `N` hops from a match, matches included. `N` is 0–64. |
| `reachable from X` | The whole connected component of every match. |

`X` is a full query, so `within 2 hops of (kind = router and label.site =
north)` is a sentence. The body is answered against the **whole** graph, not
against whatever the rest of the query has already narrowed: a switch two hops
away is still two hops away even when the node in between would have been
filtered out. That is the same rule `--neighbors-of` has always followed.

Which graph is walked is which layer is drawn. At layer 3 the traversal runs
over subnet nodes, so `within 1 hops of pc-north-01` reaches the prefixes it is
addressed in and `within 2 hops` the other hosts in them.

---

## Negation, and what it partitions

`not X` is the complement of `X` **within the graph the query is evaluated
over**. There is no third answer: a node that lacks the attribute is simply not
in `X`, and is therefore in `not X`. So for any query `Q` and any graph,

```text
Q   and   not (Q)
```

partition the nodes exactly — no node in both, no node in neither. `tests/`
checks this against generated inventories rather than trusting this paragraph.

Note the difference between `not (address in 10.0.0.0/8)` and `address !~ 10.*`:
the first includes an element with no addresses at all, the second does not. The
operator form asks a question *of the values*; `not` asks it of the element.

---

## The filter flags are sugar

`--kind`, `--namespace`, `--name`, `--vlan`, `--neighbors-of` and `--depth`
still work everywhere they always did, and each denotes a query:

| Flag | Query |
|---|---|
| `--namespace NS` | `namespace under NS` |
| `--vlan V` | `vlan = V` |
| `--kind K` | `kind = K` |
| `--name G` | `name ~ G` |
| `--neighbors-of N --depth D` | `within D hops of (fqn = N or name = N)` |

Repeats within one flag are alternatives; different flags are combined with
`and`. `netviz query --explain` with the flags prints exactly the query they
mean:

<!-- run: cwd=examples/campus -->
```console
$ netviz query --explain --kind switch --namespace sites/north --vlan 99
# the filter flags, as the query they are sugar for
(kind = switch and namespace under sites/north and vlan = 99)
...
```

Given both, the flags and `--select` are AND-ed, exactly as two flags are.

---

## Errors

A parse error names the column and underlines it:

<!-- run: cwd=examples/campus rc=2 -->
```console
$ netviz query 'kind = swtch and vlna = 99'
Usage: netviz query [OPTIONS] QUERY
Try 'netviz query --help' for help.

Error: Invalid value for 'QUERY': query:1:18: 'vlna' is not an attribute of element
  kind = swtch and vlna = 99
                   ^^^^
  help: did you mean 'vlan'?
```

The location line is the loader's own shape (`<source>:<line>:<column>:
<message>`, the same as a `LoadError`), so a reader who has seen one netviz
diagnostic recognises this one. Everything is bounded: an over-long query is
echoed as a window around the span rather than reproduced whole.

`--select` is parsed in a Click callback, so a typo costs a usage error before
the inventory is read rather than an empty diagram after it.

---

## Limits

Chosen so that a generated or fuzzed query is refused rather than survived. None
is reachable by hand.

| Limit | Value |
|---|---|
| Query length | 4096 characters |
| Nesting depth | 32 |
| Leaf terms | 256 |
| `within N hops` | 64 (use `reachable from` beyond that) |
| Regular-expression length | 512 characters |

---

## Cookbook

Ten worked queries against [`examples/campus`](../examples/campus) — three
sites, twenty-two elements, a backbone ring.

### 1. Every access switch

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'kind = switch and label.role = access'
sites/north/access/sw-north-acc-01
sites/north/access/sw-north-acc-02
sites/north/access/sw-north-acc-03
sites/south/access/sw-south-acc-01
sites/south/access/sw-south-acc-02
sites/west/access/sw-west-acc-01
sites/west/access/sw-west-acc-02
```

### 2. Every access switch **in site north** with no uplink to the distribution layer

The question this whole language exists for. It is empty, which is the answer
you want — every access switch has its uplink.

<!-- run: cwd=examples/campus rc=1 -->
```console
$ netviz query 'label.site = north and label.role = access and not neighbors of (label.role = distribution)' --count
0
```

### 3. Every interface addressed in `10.1.0.0/16` that is not in a VRF

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'interface[address in 10.1.0.0/16 and not has vrf]' --print interfaces
sites/north/core/rtr-north-core-01:xe-0/0/0
sites/north/distribution/sw-north-dist-01:Vlan10
sites/north/distribution/sw-north-dist-01:Vlan20
sites/north/distribution/sw-north-dist-01:Ethernet52/1
sites/north/hosts/pc-north-01:eno1
sites/north/hosts/srv-north-01:eth0
sites/north/hosts/pc-north-02:eno1
```

Drop `--print interfaces` for the five *elements* holding them.

### 4. Everything within two hops of the north core router

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'within 2 hops of rtr-north-core-01' --count
9
```

### 5. Every device with no VRF configured anywhere on it

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'kind in (switch, router) and not has vrf'
sites/north/core/rtr-north-core-01
sites/south/core/rtr-south-core-01
sites/west/core/rtr-west-core-01
```

### 6. Everything in one site that carries the management VLAN

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'namespace under sites/north and vlan = 99' --count
4
```

### 7. Everything that speaks BGP

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'has asn' --count
3
```

### 8. Every device with a fibre link to a router

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'link[medium = fiber and peer-kind = router]' --count
6
```

### 9. Every jumbo-frame interface

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'interface[mtu > 1500]' --count
13
```

### 10. The whole answer as data

`--json` carries the query it answered, so a saved result says what produced it.

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'kind = router and label.site = west' --json
{
  "query": "kind = router and label.site = west",
  "count": 1,
  "subject": "elements",
  "matches": [
    {
      "element": "sites/west/core/rtr-west-core-01"
    }
  ]
}
```

### And as an assertion

Any of the above is a test. With no bound, the claim is that the query matches
**nothing** — which is how a network invariant is written, because an invariant
is a search for its counterexamples:

<!-- norun: illustrative; the file it belongs in is examples/campus/tests.yaml -->
```yaml
- assert: query
  name: no switch or router is missing an address
  query: kind in (switch, router) and not has address

- assert: query
  name: the campus has three core routers
  query: kind = router and label.role = core
  equals: 3
```

See [`docs/testing.md`](testing.md) for how a suite is run and reported.

---

## See also

- [`docs/nql.md`](nql.md) — the relational language, for the questions a predicate cannot ask.
- [`netviz query`](commands/query.md) — the command, its flags and its exit codes.
- [`docs/rendering.md`](rendering.md) — `--select` beside the other filters.
- [`docs/testing.md`](testing.md) — `assert: query` among the assertions.
- [`docs/commands/web.md`](commands/web.md) — the editor's search box.
