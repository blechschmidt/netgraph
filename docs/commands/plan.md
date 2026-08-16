# `netviz plan`

`netviz plan` answers "what is the difference between these two networks?" and
answers it in terms of *elements and their fields*, not lines of YAML. Two
inventory states go in — this branch and `main`, this folder and that one, what
is declared and what the network reports — and an ordered changeset comes out:
what is added, what is changed field by field, what is renamed, what is
destroyed, in the order it would have to happen.

The plan is the reviewable half of the write path. [`netviz
apply`](apply.md) executes one against the **files**, through the same typed
operations [`netviz edit`](edit.md) uses, so comments and formatting survive.

`netviz plan` itself writes nothing unless you pass `-out`, and it never talks
to a device.

## Contents

- [Synopsis](#synopsis)
- [Where the two sides come from](#where-the-two-sides-come-from)
- [Addresses](#addresses)
- [Reading a plan](#reading-a-plan)
- [Renames, not delete-plus-create](#renames-not-delete-plus-create)
- [The order the entries are in](#the-order-the-entries-are-in)
- [Planning against the live network](#planning-against-the-live-network)
- [Plan files and the state hash](#plan-files-and-the-state-hash)
- [JSON, and gating CI on an empty plan](#json-and-gating-ci-on-an-empty-plan)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis plan -->
```text
netviz [GLOBAL OPTIONS] plan [OPTIONS] [NAME=]INPUT...
```
<!-- /generated -->

---

## Where the two sides come from

A plan always has a **current** side and a **desired** side. The working tree —
whatever the global `-i/--inventory` points at — is one of them unless you name
something else for both.

| Invocation | Current | Desired |
|---|---|---|
| `plan --from HEAD` | the git ref | the working tree |
| `plan --from origin/main` | the git ref | the working tree |
| `plan --to ../proposed` | the working tree | that folder |
| `plan --from a --to b` | folder `a` | folder `b` |
| `plan --from-live caps/*` | the working tree | the inventory as the capture says it should read |

`--from` and `--to` take either a directory or a git ref: a path that exists as a
directory is a folder, and anything else is handed to `git`. A ref is read with
`git archive` into a temporary directory, so the command cannot disturb the
working tree, an uncommitted change, or the index. A tree that is not in a
repository — or a machine with no `git` — simply cannot use a ref, and says so.

Giving none of the three is an error: there would be nothing to compare against.

---

## Addresses

Every element has a stable **address**: its type, a dot, and its fully-qualified
name.

```text
device.core/sw-1
cable.core/sw1-eth0--rtr-eth1
patchpanel.panels/pp-r1-a
layout.default
```

The type is a *category*, not the document's `kind`. All five device kinds are
`device.`, which is what makes `kind: switch` → `kind: router` an update of one
element rather than the destruction of one and the creation of another. The name
is the one the loader assigns: the folder the document was found in, plus
`metadata.name` ([§2.2](../schema.md#22-namespaces-and-name-resolution)).

`--target` selects a subset of the plan, and matches three spellings so you can
use whichever you have to hand — the whole address, the qualified name, or the
short name. Each is a shell-style glob:

<!-- run: cwd=. -->
```console
$ netviz -i examples/home-lab plan --from-live tests/fixtures/drift/patch.csv --target 'cable.*'
netviz plan: the inventory → the live network (csv)

  ~ cable.cables/cbl-sw-ap  [cable]
      ~ spec.medium: copper -> fiber
  ~ cable.cables/cbl-sw-nas  [cable]
      ~ spec.endpoints: [srv-nas:eth0, sw-home:port3] -> [srv-nas:eth0, sw-home:port6]
      ~ spec.medium: copper -> fiber

Plan: ~ 2 to change.
11 declared items the capture could not vouch for were left alone; -v lists them
```

`--target 'device.core/*'` takes every device in one namespace, `--target
sw-core-01` takes one element by its short name.

---

## Reading a plan

The summary is the terraform one, and the detail under each entry is the fields
that move:

```text
  ~ device.core/sw-core-01  [switch]
      ~ spec.interfaces[name=GigabitEthernet1/0/1].mtu: 1500 -> 9000
      + spec.interfaces[name=GigabitEthernet1/0/9]: {name: Gi1/0/9, type: ethernet}
      - spec.vendor: Cisco

Plan: + 3 to add, ~ 5 to change, - 1 to destroy.
```

| Mark | Action | What it means |
|---|---|---|
| `+` | `create` | The element is in the desired state and in no other. |
| `~` | `update` | The element is in both, and these fields differ. |
| `-` | `delete` | The element is in the current state and in no other. |
| `→` | `rename` | One element, two names. |

Two things about the field paths are worth knowing.

**A list entry is named, not numbered.** `spec.interfaces[name=eth0].mtu` rather
than `spec.interfaces[2].mtu`, because a plan is written now and applied later,
and an interface inserted in between would silently move every index onto the
wrong port. `apply` resolves the name to an index against the document it is
about to write.

**What is compared is meaning, not text.** Both sides are loaded through the
normal pipeline, so templates are merged, interface ranges are expanded,
defaults are filled in and every scalar is in its canonical form before the
comparison. Two trees that spell the same network differently produce an empty
plan — which is what makes `netviz plan --from HEAD` usable on a tree somebody
has just run [`netviz fmt`](fmt.md) over.

Comparing an inventory with itself is the shortest demonstration of both:

<!-- run: cwd=. -->
```console
$ netviz -i examples/home-lab plan --to examples/home-lab
netviz plan: the inventory → examples/home-lab

No changes. The two states describe the same network.
```

---

## Renames, not delete-plus-create

A diff that keys on the name alone reports a rename as a deletion and a
creation. That is not merely verbose, it is wrong about what will happen:
executed, it would drop the device's document — description, comments and all —
and write a fresh one, and every cable that terminated on it would have to go
too.

So `plan` pairs up the elements that appear on only one side by **structural
identity**, strongest evidence first:

| Evidence | What it is |
|---|---|
| `netviz.dev/id` | An annotation you set yourself. Nothing beats being told. |
| serial | `spec.serial`, with the vendor. Vendors do not reissue them. |
| MAC | The set of hardware addresses a device's interfaces declare. |
| ends | The two `device:interface` pairs a cable or tunnel joins; the host an adapter hangs off. |
| label | The sticker on a cable. It survives re-patching, which is the one change that moves a cable's ends without making it a different cable. |
| rack | Site, room, rack and rack unit. Two things cannot be in one slot. |
| ports | The kind, plus the set of interface names. |

Two rules keep this honest. A pairing is only made when the evidence matches
**exactly one** unpaired element on each side — three unnamed patch panels in
one rack cannot be told apart, and guessing which became which would move cables
onto the wrong panel. And the first three kinds of evidence *veto*: two elements
that both state a serial or a MAC and state different ones are two boxes,
however alike their port lists are.

A rename entry carries no fields. When the element also changed, the changeset
holds a separate `update` at the new address, so the two decisions stay
separable and `--target` can take one without the other.

`--no-renames` turns the detection off and reports every rename as a delete and
a create. It is occasionally what you want to *see*, and never what you want to
apply.

---

## The order the entries are in

Every reference in an inventory points outwards from the thing that cannot exist
without it: a cable names two devices, an adapter names its host, a tunnel names
the tunnel it runs over. That gives the order, and `apply` depends on it:

1. **Deletions**, dependents before dependees — the cable is removed before the
   device it terminates on. Backwards, the delete of the device would be refused.
2. **Renames**, so everything after them speaks the new names.
3. **Creations**, dependees before dependents — the device exists before the
   cable that lands on it.
4. **Updates**, which may point a field at something just created.

Within a group the order is the dependency order where there is one and the
address order where there is not, so two runs of the same diff produce the same
plan, byte for byte.

---

## Planning against the live network

`--from-live` reads the same captures [`netviz import`](import.md) and
[`netviz drift`](drift.md) do — `lldpctl -f json`, `ip -j addr show`, a cabling
CSV — and makes the **desired** side "the inventory as it would read if it agreed
with the network". Diffing that against the declaration is the write half of the
drift loop: `drift` tells you the files are wrong, `plan --from-live` tells you
exactly how to make them right, and [`apply`](apply.md) does it.

The design rests on one rule: **the target starts as the source.** It is not the
capture rendered as YAML. A capture sees a fraction of an inventory — `lldp` sees
neighbours and no interfaces, `iproute` sees interfaces and no neighbours,
neither sees a rack position or a description — so every declared document is
carried over untouched and an observation only ever overwrites the field it is an
observation *of*. What follows from it:

- A declared **interface** the capture did not mention is removed only where the
  dialect that saw the device lists every interface it has.
- A declared **cable** the capture did not report is removed only where a port
  contradicts it — where the capture shows one of its ends plugged into
  something else. A port simply not mentioned is no evidence at all.
- A re-patched cable keeps its document. Matched on its label, it becomes an
  update to `spec.endpoints` rather than a destroyed cable and an unrelated new
  one, so its length, its category and its comments survive.
- A **trunk's VLAN set** is merged, never substituted: no dialect netviz reads
  prints a port's whole VLAN list, so an observed VLAN is evidence that one *is*
  carried and never evidence that another is not.
- `kind: computer` from a capture is never adopted. It is the importer's "I
  could not tell", not an observation.

Everything the capture was not entitled to change is counted, and `-v` lists it
one line at a time.

`tests/fixtures/drift/patch.csv` is the home-lab patch list as the label sheet in
the cabinet has it. Two rows agree with the inventory; the third records the NAS
run on `port6` where the inventory still says `port3`, and the fourth calls the
AP uplink fibre where the inventory says copper:

<!-- run: cwd=. -->
```console
$ netviz -i examples/home-lab plan --from-live tests/fixtures/drift/patch.csv
netviz plan: the inventory → the live network (csv)

  ~ device.switches/sw-home  [switch]
      + spec.interfaces[name=port6]: {name: port6, type: ethernet, enabled: true}
  ~ cable.cables/cbl-sw-ap  [cable]
      ~ spec.medium: copper -> fiber
  ~ cable.cables/cbl-sw-nas  [cable]
      ~ spec.endpoints: [srv-nas:eth0, sw-home:port3] -> [srv-nas:eth0, sw-home:port6]
      ~ spec.medium: copper -> fiber

Plan: ~ 3 to change.
11 declared items the capture could not vouch for were left alone; -v lists them
```

The last line is on stderr, where the commentary goes; the plan itself is on
stdout. Three surgical updates, and nothing proposed about the four devices the patch
list says nothing about. `--only`, `--exclude` and `--exclude-interface` narrow
the adoption exactly as they narrow a `drift` comparison.

A capture may contradict itself — an LLDP table and a patch list that disagree
about what is on a port. The plan reports both, and `apply` refuses the result at
the validation gate rather than writing two cables onto one port. That is the
intended behaviour: the fix is to correct the capture, not the inventory.

---

## Plan files and the state hash

`-out FILE` writes the plan as JSON so that `netviz apply FILE` executes
**exactly what was reviewed**, and not a fresh diff that may have moved since.

The file records a hash of the state the plan was made from, and `apply` refuses
to run against a tree that hashes differently:

<!-- norun: writes files, and the digests differ per tree -->
```console
$ netviz plan --from-live caps/*.json -out drift.plan
$ netviz apply drift.plan
error: drift.plan was made against a different state of net; the tree has changed since.
Re-run 'netviz plan' and review the new plan.
  plan expects sha256:50195cf0…
  tree is      sha256:040ea676…
```

The hash is over **meaning, not bytes**: every element, addressed, with its
fields as the loader resolved them, as canonical JSON. Reformatting the tree or
re-wrapping a comment does not invalidate a plan; adding an element, changing a
field or moving a document to another namespace does.

There is no flag to bypass the check. A plan applied to a state it was not made
from is not a description of what will happen, and that is the one thing a plan
has to be.

---

## JSON, and gating CI on an empty plan

`--json` (or `-F json`) puts the whole changeset on stdout and moves the summary
to stderr, so `netviz plan --json > plan.json` writes a file a script can read
while a person watching the run still sees what happened. It is the same
document `-out` writes.

<!-- run: cwd=. -->
```console
$ netviz -q -i examples/home-lab plan --from-live tests/fixtures/drift/patch.csv --target cbl-sw-ap --json
{
  "schemaVersion": 1,
  "tool": {
...
  "summary": {
    "create": 0,
    "update": 1,
    "delete": 0,
    "rename": 0,
    "total": 1
  },
  "empty": false,
  "changes": [
    {
      "action": "update",
      "address": "cable.cables/cbl-sw-ap",
      "kind": "cable",
      "fields": [
        {
          "path": "spec.medium",
          "before": "copper",
          "after": "fiber"
        }
      ],
      "source": "cables/links.yaml#4:73"
    }
  ]
}
```

(`...` elides the `tool`, `source` and `target` blocks, which carry the netviz
version and the two state hashes.)

A field that is absent on one side omits that key entirely, rather than carrying
`null`: a document that says `mtu: null` and one that says nothing about the MTU
are different things, and only the second is restored by removing the key.

By default `plan` exits 0 whether or not there is anything to do, so that
`netviz plan -out p && netviz apply p` works. `--fail-on changes` makes a
non-empty plan exit 1, which is how CI asserts that a branch has been applied:

<!-- run: cwd=. rc=1 -->
```console
$ netviz -q -i examples/home-lab plan --from-live tests/fixtures/drift/patch.csv --fail-on changes --target 'device.*'
netviz plan: the inventory → the live network (csv)

  ~ device.switches/sw-home  [switch]
      + spec.interfaces[name=port6]: {name: port6, type: ethernet, enabled: true}

Plan: ~ 1 to change.
```

which in a workflow is one line:

<!-- norun: a CI fragment, not a transcript -->
```yaml
- run: netviz -i net plan --from-live caps/ --fail-on changes
```

---

## Arguments

<!-- generated: arguments plan -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[NAME=]INPUT...` | no | any number | — |
<!-- /generated -->

Inputs are only read with `--from-live`, and take the same `[NAME=]INPUT` form
`import` and `drift` take, with `-` for standard input.

## Options

<!-- generated: options plan -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--from` | `REF\|DIR` | — | Take the current state from a git ref or another folder instead of from the inventory. A directory that exists is a folder; anything else is a git ref, exported read-only — the working tree is never touched. |
| `--to` | `REF\|DIR` | — | Take the desired state from a git ref or another folder. Defaults to the inventory. |
| `--from-live` | — | off | Take the desired state from a live capture: the inventory as it would read if it agreed with what the network reports. Reads the same inputs 'netviz import' and 'netviz drift' do. |
| `--dialect` | `[auto\|lldp\|iproute\|csv\|netplan\|networkd\|ifupdown\|frr\|nftables\|wireguard\|interfaces]` | `auto` | Input dialect for --from-live, as 'netviz drift --from' takes it. |
| `--host` | `NAME` | — | Device every --from-live input was captured on, when the input does not name it. |
| `--only` | `GLOB` | — | Adopt only elements whose name matches this glob (--from-live only). Repeatable. |
| `--exclude` | `GLOB` | — | Leave elements matching this glob out of the adoption (--from-live only). Repeatable. |
| `--exclude-interface` | `GLOB` | — | Leave interfaces matching this glob out of the adoption. Repeatable. |
| `--target` | `ADDRESS` | — | Keep only changes to elements this glob selects, matched against the address (device.core/sw-1), the qualified name or the short name. Repeatable. |
| `--no-renames` | — | off | Report every rename as a delete and a create rather than detecting it. |
| `-F`, `--output-format` | `[text\|json]` | `text` | text is for reading; json is for CI and for a script. |
| `--json` | — | off | Shorthand for '-F json'. |
| `-out`, `--out` | `FILE` | — | Write the plan to FILE so 'netviz apply FILE' executes exactly what was reviewed. The file records a hash of the current state and apply refuses if the tree has moved on. |
| `--fail-on` | `[never\|changes]` | `never` | Exit 1 when the plan is not empty, so CI can gate on 'nothing to do'. |
<!-- /generated -->

## Exit codes

| Code | When |
|---|---|
| 0 | The plan was computed. Whether or not it is empty, unless `--fail-on changes`. |
| 1 | Either side does not load, a ref or a capture cannot be read, or `--fail-on changes` and the plan is not empty. |
| 2 | Usage: no side to compare against, `--from-live` with `--to`, capture inputs without `--from-live`. |

An inventory that does not load is refused rather than planned against: a
document that was rejected is absent from the inventory, so diffing against it
would read as a deletion.

## See also

- [`netviz apply`](apply.md) — executing a plan against the files.
- [`netviz drift`](drift.md) — the read half of the same loop.
- [`netviz edit`](edit.md) — the operations `apply` is built out of.
- [`netviz validate`](validate.md) — the gate a plan has to pass to be written.
- [`docs/editing.md`](../editing.md) — the write path, in prose.
