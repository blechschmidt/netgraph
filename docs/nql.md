# The relational query language

The [selector](query.md) answers one question — *which elements?* — and it
answers it everywhere: `--select`, the editor's search box, an `assert: query`.
It is a predicate, so it cannot join, cannot project and cannot return anything
but a list of names.

This is the other half. **NQL** walks the schema, joins by following links, and
returns whatever shape you ask it for: a value, an object, or an array of nested
objects.

<!-- run: cwd=examples/campus -->
```console
$ netviz query "select server { name, addresses := .addresses.address } filter .name = 'srv-north-01'" -F json
{
  "query": "select server { name, addresses := .addresses.address } filter .name = 'srv-north-01'",
  "count": 1,
  "type": "many server",
  "results": [
    {
      "name": "srv-north-01",
      "addresses": [
        "127.0.0.1/8",
        "::1/128",
        "10.1.20.11/24",
        "2001:db8:1:20::11/64"
      ]
    }
  ]
}
```

One command runs both languages. `netviz query` reads the first word: `select`
or `with` means relational, anything else is a selector.

- [Why this shape](#why-this-shape)
- [The four questions](#the-four-questions)
- [Grammar](#grammar)
- [Paths, and the implicit subject](#paths-and-the-implicit-subject)
- [Shapes: how a result gets its structure](#shapes-how-a-result-gets-its-structure)
- [Clauses](#clauses)
- [Operators](#operators)
- [Functions](#functions)
- [Set semantics, and the two things to know](#set-semantics-and-the-two-things-to-know)
- [The type graph](#the-type-graph)
- [Output formats](#output-formats)
- [Errors](#errors)
- [Limits](#limits)
- [Cookbook](#cookbook)

---

## Why this shape

Three languages were candidates. The network model chose between them.

**SQL** projects better than its reputation suggests, but its joins are
explicit. Walking from a server to its addresses means knowing that addresses
hang off interfaces, which hang off devices, and spelling both joins out — and
following the schema is the *whole* of what these questions do. A language whose
common case is its verbose case is the wrong language here. Reachability needs a
recursive CTE, and nested output needs the JSON functions.

**Cypher** inverts that. `MATCH (d:Server)-[:HAS_IF]->(i)` is a pleasure to
write and `-[*1..3]->` makes reachability a character rather than a paragraph.
But a property graph is untyped: relationship names live outside any schema, so
nothing can tell you `HAS_IF` should have been `HAS_INTERFACE` until the query
comes back empty. And it returns *rows* — building a nested object means
`collect()` and map projections, and the result's shape stops being legible from
the query that produced it.

**EdgeQL** is what netviz already needed. It keeps SQL's set semantics and its
clauses, replaces joins with path navigation over a *typed* schema, and makes
nesting syntax: `{ name, interfaces: { mac } }` is the answer's shape written
out. netviz's inventory is already a typed object graph — that is what
[`docs/schema.md`](schema.md) is — so the schema the language checks against is
one the project maintains anyway.

So NQL is EdgeQL's shape over netviz's schema, plus two functions borrowed from
Cypher's traversal (`neighbors`, `reachable`) for the questions that no fixed
number of named steps can answer.

What that buys, concretely:

* **Every name is checked before a file is opened.** `select interface { mak }`
  is a parse error with a suggestion, not an empty result.
* **The result's structure comes from the schema, not from the data.** A link
  the schema calls `many` renders as a JSON array even when it holds one value,
  so a script never has to sniff.
* **A join is a dot.** `.parent`, `.addresses.subnet`, `.interfaces.peer.parent`.

---

## The four questions

Verified against [`examples/campus`](../examples/campus).

### Every network interface with an IP address, and what it is attached to

<!-- norun: the answer is 49 rows; the page shows the first three -->
```console
$ netviz query 'select interface {
      fqn,
      attached_to := .parent.fqn,
      addresses := .addresses.address
  } filter exists .addresses'
FQN                                                     ATTACHED_TO                                ADDRESSES
------------------------------------------------------  -----------------------------------------  -------------------------------------
sites/north/access/sw-north-acc-01:Vlan99               sites/north/access/sw-north-acc-01         10.1.99.11/24
sites/north/access/sw-north-acc-02:Vlan99               sites/north/access/sw-north-acc-02         10.1.99.12/24
sites/north/access/sw-north-acc-03:Vlan99               sites/north/access/sw-north-acc-03         10.1.99.13/24
…
```

`.parent` is the element the port is attached to — a device, an adapter or a
patch panel. It is a link like any other, so it can be filtered
(`filter .parent is server`), narrowed (`.parent[is router].asn`) or projected
whole (`parent: { name, kind, vendor }`).

### Every interface in the broadcast domain of switch Y, and its device

<!-- norun: the answer is the JSON block below, trimmed to three ports -->
```console
$ netviz query "select broadcast_domain {
      name,
      size,
      ports := .interfaces { fqn, device := .parent.name }
  } filter .vlan_id = 20 and .members.name = 'sw-south-dist-01'" -F json
```

```json
{
  "name": "vlan20#2",
  "size": 4,
  "ports": [
    { "fqn": "sites/south/access/sw-south-acc-01:GigabitEthernet1/0/2", "device": "sw-south-acc-01" },
    { "fqn": "sites/south/access/sw-south-acc-01:TenGigabitEthernet1/1/1", "device": "sw-south-acc-01" },
    { "fqn": "sites/south/distribution/sw-south-dist-01:Vlan20", "device": "sw-south-dist-01" }
  ]
}
```

Four elements are in the domain and seven ports join it, because a switch joins
through every port that carries the VLAN; the block above is trimmed to three.

A `broadcast_domain` is derived, not declared: it is one VLAN id *and* one
connected component of the links that carry it, which is why the answer is
`vlan20#2` and not `vlan20`. The same objects that a layer-2 diagram draws;
[§9.3](schema.md) explains the partitioning.

### Which IP addresses are assigned to a server called X

<!-- run: cwd=examples/campus -->
```console
$ netviz query "select (server filter .name = 'srv-north-01').addresses.address"
ADDRESS
--------------------
127.0.0.1/8
::1/128
10.1.20.11/24
2001:db8:1:20::11/64
```

`filter` attaches to the nearest set of *objects* in the path, so writing it
the other way round —

<!-- norun: a usage error; the page shows the diagnostic without click's preamble -->
```console
$ netviz query "select server.addresses.address filter .name = 'srv-north-01'"
query:1:41: 'name' is not a member of address
  select server.addresses.address filter .name = 'srv-north-01'
                                          ^^^^
```

— says so rather than quietly answering a different question. See
[paths](#paths-and-the-implicit-subject).

### Which MAC addresses are assigned to a server

<!-- run: cwd=examples/campus -->
```console
$ netviz query 'select interface { host := .parent.name, port := .name, mac } filter .parent is server and exists .mac'
HOST          PORT  MAC
------------  ----  -----------------
srv-north-01  eth0  3c:d9:2b:01:20:01
srv-south-01  eth0  3c:d9:2b:02:20:01
srv-west-01   eth0  3c:d9:2b:03:20:01
```

---

## Grammar

```text
query        := [ "with" NAME ":=" expr ("," NAME ":=" expr)* ] statement
statement    := expr clause*
expr         := or
or           := and ("or" and)*
and          := unary ("and" unary)*
unary        := ("not" | "exists" | "distinct") unary | comparison
comparison   := additive [ compare_op additive ]
              | additive "is" ["not"] TYPE
compare_op   := "=" | "==" | "!=" | "<" | "<=" | ">" | ">="
              | "in" | "not" "in" | "like" | "ilike" | "~" | "!~" | "=~" | "under"
additive     := multiplicative (("+" | "-" | "++") multiplicative)*
multiplicative := prefix (("*" | "/" | "%") prefix)*
prefix       := "-" prefix | postfix
postfix      := primary ("." NAME | "[" "is" TYPE "]")*
primary      := NUMBER | STRING | "true" | "false" | "none"
              | "(" statement ")"
              | "{" expr ("," expr)* "}"            -- a set
              | "{" NAME ":=" expr ("," …)* "}"     -- a free object
              | "." NAME                            -- from the implicit subject
              | NAME "(" [ statement ("," statement)* ] ")"
              | NAME                                -- a type, or a `with` binding
              | "select" expr [ shape ] clause*
clause       := ("filter" | "where") expr
              | "order" "by" order (("," | "then") order)*
              | "limit" expr
              | "offset" expr
order        := expr ["asc" | "desc"]
shape        := "{" field ("," field)* "}"
field        := NAME                        -- project a property or a link
              | NAME ":" shape clause*      -- project linked objects, shaped
              | NAME ":=" expr [shape] clause*
              | "*"                         -- every property of the type
```

Precedence runs loosest to tightest down the list, so `a or b and c` is
`a or (b and c)` and `.mtu + 8 > 1500` compares the sum.

`select` is optional wherever a whole set is expected — inside parentheses, a
function argument or a `with` binding — so `count(interface filter exists
.addresses)` reads the way it is meant. A whole *query* must still open with
`select` or `with`, because that is how `netviz query` tells the two languages
apart.

**Names are letters, digits and `_`.** `-` is the minus sign, so a value with
punctuation in it is quoted: `filter .name = 'sw-core-01'`. Keywords are
case-insensitive; values are not. `#` starts a comment that runs to the end of
the line.

---

## Paths, and the implicit subject

A path is a chain of dots. Each step is a property (ending in a scalar) or a
link (ending in more objects).

```text
device.interfaces.addresses.subnet.prefix
```

Inside `filter`, `order by` and a shape, a **leading dot** starts from the
object currently being considered:

```text
select device { name, ports := count(.interfaces) } filter .vendor = 'Cisco'
```

When the selected expression is a path that ends in a *scalar*, the clauses
attach to the objects it came from, not to the strings it ends in:

| Written | `.` means | Equivalent to |
|---|---|---|
| `select interface.mac filter .parent is switch` | `interface` | `select (interface filter .parent is switch).mac` |
| `select device.interfaces.name filter .enabled` | `device.interfaces` | `select (device.interfaces filter .enabled).name` |
| `select device.interfaces { name } filter .enabled` | `device.interfaces` | — it is already the objects |

The subject is the **nearest** object set, not the first one. So
`server.addresses.address filter .name = …` scopes to `address`, which has no
`name` — and the parse error says exactly that instead of returning every
server's addresses.

`[is TYPE]` narrows a polymorphic link. `interface.parent` is an `element`,
because a port may belong to a device, an adapter or a patch panel; `.parent[is
router].asn` keeps only the routers and reads a member only routers have.
`x is TYPE` is the *question* rather than the narrowing, and it yields a boolean:

<!-- norun: shows the form, not an answer -->
```console
$ netviz query 'select interface { fqn } filter .parent is adapter'
```

---

## Shapes: how a result gets its structure

A shape says what an object projects to. Four kinds of field:

| Written | Means |
|---|---|
| `name` | the property or link called `name` |
| `interfaces: { name, mac }` | the linked objects, each projected through that shape |
| `ports := count(.interfaces)` | any expression, under a name you choose |
| `*` | every property of the type — useful for exploring, noisy for scripting |

Shapes nest without limit, and a field's **cardinality decides its JSON type**:

<!-- norun: the answer is the JSON block below -->
```console
$ netviz query 'select adapter { name, attached_to: { name, kind }, interfaces: { name } }' -F json
```

```json
{
  "name": "adp-usb-eth",
  "attached_to": { "name": "laptop", "kind": "computer" },
  "interfaces": [
    { "name": "enx001122334455" },
    { "name": "usb0" }
  ]
}
```

`attached_to` is an `optional element`, so it is an object or `null`, and never
a one-element array. `interfaces` is `many interface`, so it is an array — and
it stays an array on an adapter with a single port. That comes from the schema,
which [`--describe`](commands/query.md) prints, not from the data.

(`usb0` is the adapter's `upstream` — the host-facing socket. It is a port a
cable can terminate on, so it is an `interface` like the downstream ones, with
its bus as its `type`.)

A shape is **output only**. `(select device { name }).vendor` is still a set of
devices with a vendor read off it; the shape does not change what the expression
*is*. That is what keeps nested projections easy to reason about.

A **free object** assembles a result that is not any schema type:

<!-- norun: the answer is the JSON block below -->
```console
$ netviz query 'select {
      devices  := count(device),
      subnets  := count(subnet),
      addressed := count(interface filter exists .addresses)
  }' -F json
```

```json
{ "devices": 22, "subnets": 33, "addressed": 49 }
```

---

## Clauses

| Clause | Notes |
|---|---|
| `filter <condition>` | `where` is a synonym. Repeatable; two clauses are an `and`. Must be a condition, not a set of objects — `filter .interfaces` is refused, `filter exists .interfaces` is meant. |
| `order by <key> [asc\|desc] [then <key> …]` | Empty sorts last. Numbers sort as numbers, addresses as addresses, everything else as text. |
| `limit N` / `offset N` | Whole numbers. |

Clauses attach to the nearest thing that can take them, which inside a shape is
the *field*:

```text
select device {
  name,
  interfaces: { name } filter .enabled order by .name limit 4
} filter .kind = 'switch'
```

The inner clauses narrow that device's ports; the outer one narrows the devices.

---

## Operators

| Operator | Meaning |
|---|---|
| `=` `==` | Equality. Objects compare by identity, numbers as numbers, everything else as text. |
| `!=` | Its mirror — see below, it is not its negation on a multi-valued property. |
| `<` `<=` `>` `>=` | Numbers and addresses. Refused on text at parse time. |
| `in` | Membership in a set — **or** containment when the right side is a prefix: `.ip in '10.0.0.0/8'`, `.prefix in '10.0.0.0/8'`. |
| `not in` | Its negation. |
| `~` `like` | Shell glob, case-sensitive. `ilike` folds case. `!~` negates. |
| `=~` | Regular expression, unanchored. Checked at parse time. |
| `under` | Namespace containment: `.namespace under 'sites/north'` keeps `sites/north/access`. |
| `is` / `is not` | Type test. |
| `and` `or` `not` | Conditions. |
| `exists` | Is the set non-empty? Always exactly one boolean. |
| `distinct` | The set with repeats removed, order kept. |
| `+ - * / %` | Arithmetic. |
| `++` | Text concatenation — spelled separately so `+` stays unambiguously arithmetic. |

---

## Functions

`netviz query --describe` prints the current list with signatures.

| Function | What it does |
|---|---|
| `count(any)` | How many values the set holds. |
| `sum(number)` `avg(number)` | Total, mean. |
| `min(scalar)` `max(scalar)` | Extremes, or nothing for an empty set. |
| `any(bool)` `all(bool)` | Existential and universal. `all` is true of an empty set. |
| `len(text)` `lower(text)` `upper(text)` `text(any)` | Text. |
| `contains(a, b)` `starts_with(a, b)` `ends_with(a, b)` `matches(a, glob)` | Text predicates. |
| `lookup(text, key)` | The value of one `key=value` entry — how `.labels` and `.annotations` are read. |
| `neighbors(objects[, hops])` | Every element at most `hops` links away, seeds included. Defaults to one hop. |
| `reachable(objects)` | The whole connected component of every seed. |

`count`, `sum`, `min`, `max`, `avg`, `any` and `all` **aggregate**: they collapse
a whole set to one value, which is how `{ ports := count(.interfaces) }` becomes
a number rather than a list. Everything else applies to the cartesian product of
its arguments.

---

## Set semantics, and the two things to know

Everything is a set. An operator applies to the cartesian product of its
operands, and `filter` keeps an object when its condition yields **at least one**
true. So `filter .addresses.ip = '10.0.0.1'` means "has such an address", which
is what an operator means by it.

**1. `!=` is not the negation of `=` on a multi-valued property.**
`.addresses.ip != '10.0.0.1'` means *has an address that differs* — which is
true even of the host that has `10.0.0.1`, because it also has a loopback. On
`examples/campus` it keeps all 22 devices. `not` is the negation, and it is the
one operator that collapses a set to a single answer:

```text
filter .addresses.ip != '10.0.0.1'          # has some other address  -- 22 of 22
filter not (.addresses.ip = '10.0.0.1')     # has no such address     -- 21 of 22
filter all(.interfaces.enabled)             # every port is up
```

`filter X` and `filter not X` therefore partition the set exactly: nothing is in
both, and nothing is in neither.

**2. A bare type name is a fresh read of the whole inventory, not a reference to
what is being filtered.** Inside a `filter`, `device` means *every* device, so
`filter device.name = 'x'` asks "is there a device called x", which is a
different question from "is this one called x". Use a path from the subject:

```text
select device { name } filter .name = 'srv-north-01'          # this one
select device { name } filter exists (device filter .name = 'x')   # any of them
```

---

## The type graph

`netviz query --describe` lists every type; `--describe TYPE` lists one type's
members with cardinalities and summaries. The shape mirrors
[`docs/schema.md`](schema.md):

| Type | What it is |
|---|---|
| `element` | *(abstract)* anything a document declares. Every other declared kind inherits its name, namespace, labels, location and links. |
| `device` | *(abstract)* the six kinds of §6, with `switch`, `router`, `firewall`, `hub`, `computer` and `server` as concrete subtypes. |
| `adapter`, `patchpanel`, `pdu`, `cable`, `tunnel`, `user`, `group` | The other declared kinds. |
| `interface` | One entry of `spec.interfaces` — plus `.parent`, `.addresses`, `.vlans`, `.cable`, `.peer`, `.veth_peer`, `.netns`, `.zone`, `.broadcast_domains`. |
| `address` | One configured IP address, with `.interface`, `.element` and `.subnet`. |
| `vlan`, `netns`, `zone`, `route` | The sub-objects a device holds. |
| `subnet`, `broadcast_domain`, `link`, `rack` | **Derived.** Nobody declares them; they come from the same functions a diagram draws them with, so a query and a picture cannot disagree. |

Every link exists in both directions: `interface.vlans` and `vlan.interfaces`
are two indices onto one assignment.

Adding a fact to the language is two edits — a row in
`src/netviz/nql/schema.py` and its reader in `src/netviz/nql/world.py` — and a
test fails when only one of them is made.

---

## Output formats

| `-F` | Shape |
|---|---|
| `table` (default) | Flat. A nested list becomes `a, b, c`; a nested object becomes its values joined by a space. Headings come from the shape, so an empty answer still prints them. |
| `json` | The whole structure, under `query`, `count`, `type` and `results`. |
| `yaml` | The same document. |
| `csv` | Flattened like the table, RFC 4180. |

`--count` composes with all four. The command exits 1 when nothing matched, so a
query is usable as a check in a script.

---

## Errors

Every name is resolved while the query is read, so a mistake is a diagnostic with
a caret and — where there is one — a suggestion:

<!-- norun: a usage error; the page shows the diagnostic without click's preamble -->
```console
$ netviz query 'select interface { mak }'
query:1:20: 'mak' is not a member of interface
  select interface { mak }
                     ^^^
  help: did you mean 'mac'?
```

<!-- norun: a usage error; the page shows the diagnostic without click's preamble -->
```console
$ netviz query 'select device filter .name < 3'
query:1:22: str does not order, so < cannot be written on it
  select device filter .name < 3
                       ^^^^^
  help: compare with '=' or '~', or order it with 'order by'
```

None of these read the inventory. A query is checked against the *schema*, which
is why a typo costs a millisecond.

---

## Limits

| Limit | Value | Why |
|---|---|---|
| Query length | 4096 characters | A query is something a person types. |
| Nesting depth | 48 | A hand-written query reaches four or five. |
| Terms | 1024 | Past this it is generated. |
| Regex length | 512 characters | Same. |

There is no user-defined function, no lambda and no recursion: a query is a
bounded walk of the schema, and the only unbounded search in the language is
`reachable`, whose termination is the graph's business. Nothing a query can say
changes anything.

---

## Cookbook

```text
# Ports that are down, and whose they are
select interface { fqn, description } filter not .enabled

# Every trunk carrying the management VLAN
select interface { fqn, vlans := .vlans.id }
filter .vlan_mode = 'trunk' and .vlans.id = 99

# Subnets by how full they are
select subnet { prefix, vrf, used, size, utilisation }
order by .utilisation desc limit 10

# Devices with an interface in more than one VLAN
select device { name, vlans := distinct .interfaces.vlans.id }
filter count(distinct .interfaces.vlans) > 1

# The far end of every fibre run
select cable { name, medium, ends := .ends { fqn, device := .parent.name } }
filter .medium = 'fiber'

# Everything within two hops of the core router
select neighbors(router filter .name = 'rtr-north-core-01', 2) { name, kind }

# Containers: every namespace, its depth, and what is addressed in it
select netns { fqn, depth, addresses := .addresses.address }
order by .fqn

# Address plan as data, for another tool -- run it with -F json
select address { address, vrf, on := .interface.fqn, element := .element.fqn }
filter .is_routable

# Which devices carry a label
select device { name, role := lookup(.labels, 'role') } filter exists lookup(.labels, 'role')

# Ports that are not plugged into anything
select interface { fqn } filter .type = 'ethernet' and not exists .cable
```

---

## See also

- [`docs/query.md`](query.md) — the selector language, and where it is used.
- [`netviz query`](commands/query.md) — the command, its flags and its exit codes.
- [`docs/schema.md`](schema.md) — the specification the type graph mirrors.
- `netviz query --describe` — the same reference, generated, at the terminal.
