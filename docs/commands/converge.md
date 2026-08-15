# `netgraph converge`

[`netgraph drift`](drift.md) says how the live network differs from the declared
inventory. [`netgraph export`](../export.md#device-configuration-the-six-dialects)
says what a device would run if it agreed. Nothing joined them: an operator read
a list of differences and typed the fix, and typing the fix is where the network
and the inventory started disagreeing in the first place.

`netgraph converge plan` is the join. It takes the same captures `drift` takes
and produces, per device, the **minimal ordered set of changes** that would move
it from what the capture found to what the inventory declares — each one
carrying the drift finding that asked for it, a risk classification, its
prerequisites, the commands that perform it and the commands that undo it.

It is `plan`/`apply` pointed at devices rather than at files. Where
[`netgraph plan`](plan.md) diffs two inventory *states* and
[`netgraph apply`](apply.md) writes YAML, this diffs the inventory against the
*network* and writes a script somebody runs.

---

## Contents

- [netgraph does not touch your devices](#netgraph-does-not-touch-your-devices)
- [Synopsis](#synopsis)
- [A worked example](#a-worked-example)
- [What a change is](#what-a-change-is)
- [The order](#the-order)
- [Risk, the management path, and `--allow-disruptive`](#risk-the-management-path-and---allow-disruptive)
- [Maintenance batches](#maintenance-batches)
- [The dialects](#the-dialects)
- [What netgraph will not write a command for](#what-netgraph-will-not-write-a-command-for)
- [Per-device scripts, and `--rollback`](#per-device-scripts-and---rollback)
- [Output formats](#output-formats)
- [The JSON document](#the-json-document)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## netgraph does not touch your devices

**There is no transport in this command, and there is no flag that adds one.**

netgraph opens no SSH session, holds no credential, reads no `known_hosts`, and
has no code path that sends a byte to a device. `converge plan` reads capture
files somebody already collected and writes a plan and a set of `.txt` scripts.
A person — or a purpose-built tool with its own threat model — runs them.

That is a deliberate boundary and it is worth stating plainly, because the
alternative is the thing people are right to be afraid of. The security surface
of the whole netgraph project is *reads files, writes files*, and that is a
sentence an auditor can check by grepping for a socket. A tool that can also log
in to four hundred switches has an entirely different one: it needs credentials
somewhere, it needs to decide what a host key means, it needs a story about what
happens when command seventeen of forty fails on device three of two hundred.
None of that belongs in the program that also draws diagrams.

The scripts are `.txt` rather than `.sh` for the same reason. A file with a
shebang and an executable bit invites somebody to run it against an estate
without reading it; a `.txt` invites them to read it. The lines in it are still
the exact lines to run.

**The plan type is designed so a transport could consume it later.** Each change
in the JSON has a stable `id`, a list of `prerequisites` naming the ids that must
land first, a `risk`, and a `rollback` command list. Something applying changes
one at a time, verifying after each and backing out on failure, has everything it
needs without re-deriving anything. It would be a separate program.

---

## Synopsis

<!-- generated: synopsis converge plan -->
```text
netgraph [GLOBAL OPTIONS] converge plan [OPTIONS] [NAME=]INPUT...
```
<!-- /generated -->

The inventory comes from the global `-i/--inventory`; the arguments are the
captures, in the same `[NAME=]INPUT` form [`import`](import.md) and
[`drift`](drift.md) take, with `-` for standard input.

---

## A worked example

`tests/fixtures/drift/` holds captures taken against the
[home-lab example](../../examples/home-lab), the same ones
[`netgraph drift`](drift.md#a-worked-example) is documented with. Collect what you
would for `drift`, then plan:

<!-- run: cwd=. rc=4 -->
```console
$ netgraph -i examples/home-lab converge plan --only pc-desk --only srv-nas tests/fixtures/drift/pc-desk.addr.json tests/fixtures/drift/srv-nas.link.json
error: refusing to emit a plan: 2 change(s) on 1 device(s) would touch the path the device is managed on. Nothing was written. Re-run with --allow-disruptive once you have a way back in -- console, out-of-band, or somebody standing next to the rack
  hosts/pc-desk: set mac on eno1 to 3c:97:0e:20:01:01 -- changes mac on eno1, which bounces the interface; eno1 carries the management address 192.168.10.20/24
  hosts/pc-desk: remove the vlan interface eno1.30 -- removes the interface eno1.30; anything configured on it goes with it
```

That refusal is the command working: `eno1` is how you reach `pc-desk`, and both
of those changes bounce it. With a way back in — a console, or somebody next to
the rack:

<!-- run: cwd=. rc=2 -->
```console
$ netgraph -i examples/home-lab converge plan --allow-disruptive --only pc-desk --only srv-nas tests/fixtures/drift/pc-desk.addr.json tests/fixtures/drift/srv-nas.link.json
converge plan for examples/home-lab from 2 input(s) (iproute), written as interfaces commands

hosts/pc-desk (computer) [batch 0]
  ~ !! set mac on eno1 to 3c:97:0e:20:01:01
        from hosts/pc-desk:eno1.mac: declared as 3c:97:0e:20:01:01; the capture reports 3c:97:0e:20:01:ff
        disruptive: changes mac on eno1, which bounces the interface; eno1 carries the management address 192.168.10.20/24
        $ set interface eno1 mac 3c:97:0e:20:01:01
  -    stop carrying VLAN 30 on eno1
        from hosts/pc-desk:eno1.vlan: VLAN 30 is carried on this port; the inventory declares no VLAN here
        $ remove interface eno1 vlan-tagged 30
  - !! remove the vlan interface eno1.30
        from hosts/pc-desk:eno1.30: the capture reports this interface as vlan; the inventory does not declare it
        disruptive: removes the interface eno1.30; anything configured on it goes with it
        $ delete interface eno1.30

hosts/srv-nas (server) [batch 0]
  ~    set mtu on eth0 to 1500
        from hosts/srv-nas:eth0.mtu: declared as 1500; the capture reports 9000
        $ set interface eth0 mtu 1500

maintenance batches
  batch 0: pc-desk, srv-nas
      nothing loses reachability while this batch is worked

4 change(s) across 2 element(s) (0 create, 2 update, 2 delete), 2 disruptive; 1 maintenance batch(es)
```

Every line of the plan traces back to a line of `netgraph drift`. Nothing else
is ever proposed.

---

## What a change is

One entry of the plan is one thing to do to one device. It carries:

| Field | What it is |
|---|---|
| `id` | Stable within the plan, and between runs over unchanged inputs: `hosts/pc-desk#interface.set/eno1/mac`. |
| `action` | `create`, `update`, `delete`, or `manual` — something no command closes. |
| `object` | `vlan`, `interface`, `address`, `member`, `field`, `file`, `link` or `device`. |
| `interface` | The interface it is on, or absent for a device-wide change and for a file. |
| `target` | Which one, in the device's own words: `eno1`, `mtu`, `20`, `10.0.0.5/24`. |
| `value` / `previous` | What the change sets, and what the capture reported in its place — carried on the change rather than only inside the command text, so a consumer never has to parse a command back apart. `previous` is what the rollback restores, and is absent when the capture reported nothing. |
| `summary` | One imperative sentence. |
| `rank` | Position in the dependency order — see below. |
| `risk` | `safe` or `disruptive`. |
| `provenance` | The drift findings that asked for it: element, path, field, direction, both values and the finding's own sentence, verbatim. Every change has at least one. |
| `prerequisites` | Ids of changes on the same device that must land first. |
| `commands` | What to run, in order. |
| `rollback` | What to run to put the device back the way the capture found it. |

**A change with no provenance would be netgraph's own opinion about a network,
and this command does not have one.** It only ever proposes what closes a
difference somebody can go and read in `netgraph drift` output.

The vocabulary is bounded by what a capture can actually detect, which is what
`drift` compares: the interface set, `mac`/`mtu`/`enabled`/`parent`, the address
list per family, bridge and bond membership, VLAN mode, access VLAN and carried
VLANs. Nothing here proposes a change to something no input could have
contradicted — a plan that "fixed" an OSPF area nobody measured would be
netgraph guessing with a root shell.

One prerequisite is filled in from the *inventory* rather than from the capture:
if a port is being put into VLAN 20 and the declared device has 20 in its VLAN
database, the VLAN is created first. A capture rarely reports a VLAN database —
`ip` has no concept of one and LLDP does not carry it — so drift cannot report
the VLAN as missing, and a plan built from drift alone would put a port into a
VLAN that does not exist. Only VLANs the inventory *declares* are ever created.

---

## The order

Changes are emitted in a dependency order, and the order is a fixed table
because the dependencies between *kinds* of change are fixed by how networks
work rather than by the particular network:

| Rank | Change |
|---|---|
| 10 | create a VLAN |
| 20 | create an interface |
| 30 | set `mtu`, `mac`, `parent` |
| 40 | enslave a member |
| 50–70 | VLAN mode, access VLAN, tag a VLAN |
| 80 | add an address |
| 90 | bring an interface up |
| 100 | shut an interface |
| 110 | remove an address |
| 120 | untag a VLAN |
| 130 | release a member |
| 140 | delete an interface |
| 150 | delete a VLAN |
| 200+ | write the dialect's file and reload |
| 900 | the manual items |

Four rules produced that table:

* **Build before you use.** A VLAN exists before a port is put in it; an
  interface exists before it is enslaved, addressed or brought up.
* **Address before routing.** A next hop that is not on a configured subnet is
  rejected by every stack there is.
* **Undo in reverse.** Removals run after every addition and in the mirror
  order: an address comes off before the interface under it does. Applied to a
  half-finished run, that leaves the device in a state that still forwards.
* **Down last, up early.** Bringing interfaces up sits before every removal and
  shutting them after every addition, so a script that is interrupted has
  brought things up and not yet taken anything down.

Within a rank, stacked interfaces are ordered by depth: `eno1` before `eno1.30`
when both are being made, and `eno1.30` before `eno1` when both are going away.
Depth follows the declared `parent`, not the name — a sub-interface is usually
called after what it sits on, but nothing makes `adm0 parent: wan0` illegal, and
reading the name would put a child first in exactly the inventories that do not
follow the convention.

The `prerequisites` list adds the *specific* edges the table cannot know — this
sub-interface needs that parent, this port needs that VLAN — so a transport
applying changes out of order still has the constraints.

---

## Risk, the management path, and `--allow-disruptive`

The one failure mode that makes an automated remediation worse than no
remediation is the plan that works perfectly and then cannot be checked, because
the last command took away the path the operator was on.

So every change is classified, and **a plan holding a disruptive change is
refused outright** — nothing is printed, nothing is written — unless
`--allow-disruptive` is given.

A change is `disruptive` when it touches the **management path** of its device,
or when it shuts or deletes an interface.

The management path is three things, and the first is not a guess:

1. **The interface netgraph would reach the box on.** That is the same ranking
   that picks `ansible_host` and a Prometheus scrape target in
   [`netgraph export`](export.md): an explicitly named management port
   (`mgmt0`, `idrac`, anything whose name or description says management or
   out-of-band) first, then a loopback with a routable address, then the
   declared interface order, IPv4 before IPv6. One definition, three consumers —
   if netgraph would monitor the device over `mgmt0`, then `mgmt0` is what a
   converge script must not pull out from under itself.
2. **Everything that interface is built on.** Taking a member out of the bond
   that carries the management address, or deleting the parent of the management
   sub-interface, is the same mistake spelled differently.
3. **The VLAN it lives in**, when the management interface is a VLAN
   sub-interface or an access port.

Touching any of those with a change that *takes something away* — shut, delete,
remove an address, release a member, untag a VLAN, change the VLAN mode or the
access VLAN — is disruptive. So is changing `mac` or `parent` on it, because
both bounce the link.

Membership is the one relation that names two interfaces, and both are checked:
`ip link set mgmt0 master br0` takes the management address off `mgmt0` just as
finally as releasing it would, so **enslaving** the management port is disruptive
as well as releasing it. And removing the management *address* is disruptive
wherever the capture found it, even on an interface the declaration does not put
it on.

Deliberately **not** disruptive: setting an MTU, correcting a MAC on an
interface that is not the management one, adding an address, creating a VLAN,
bringing an interface up. Those are the changes a converge run should be able to
make at three in the morning without an argument. `mtu` in particular is left
safe on the management path too: every stack netgraph writes for changes it on a
live interface without taking the link down, and classifying it as disruptive
would make `--allow-disruptive` the flag every run needs, which is the same as
having no flag.

The refusal names **every** disruptive change, not the first, because an
operator deciding whether to pass the flag is deciding about the whole set.

---

## Maintenance batches

A plan that lists forty devices is a plan nobody can schedule. What an operator
needs to know is *which of these can I do at the same time, and what goes dark
while I do*. That is the question [`netgraph impact`](impact.md) already answers
for a hypothetical failure, and a device being reconfigured is a device that may
bounce — so it is the same question and gets the same engine.

Two devices share a batch when **neither is in the other's blast radius and
their blast radii do not overlap**. Taking both out at once is then no worse
than taking either out on its own, which is the only property that makes a
window schedulable. An access switch and the core switch it hangs off fall into
different batches, so walking the batches in order never doubles up an outage.

Each batch reports what loses reachability while it is worked, and which
namespaces it partitions, measured across layers 1, 2 and 3. Power is left out:
a device being reconfigured is not a device losing its feed.

The packing is first-fit over devices in name order. That is not optimal and
does not try to be — an optimal packing would depend on the whole set, so adding
one device could reshuffle every batch, and a schedule that changes shape when
the inventory grows by one is a schedule nobody trusts.

---

## The dialects

`--dialect` chooses what the commands are written in. Every dialect is one
netgraph already generates configuration for, and the plan runs those same
emitters rather than a second rendering that could disagree with them.

| `--dialect` | What a change looks like |
|---|---|
| `interfaces` (default) | netgraph's own imperative grammar, one line per change. |
| `netplan` | the generated `/etc/netplan/10-netgraph.yaml`, then `netplan apply`. |
| `networkd` | the generated `.network`/`.netdev` units, then `networkctl reload`. |
| `ifupdown` | the generated `/etc/network/interfaces`, then `systemctl restart networking`. |
| `frr` | the generated `/etc/frr/frr.conf`, then `vtysh -b`. |
| `wireguard` | the generated `/etc/wireguard/<if>.conf`, then `wg-quick down/up <if>`. |

### `interfaces` — the imperative one

The vocabulary is not invented: it is the
[`interfaces` dialect](../export.md#device-configuration-the-six-dialects) with a
verb in front. Somebody who has read one of those files can read one of these
scripts, and somebody reading a script knows which field of which document they
are looking at.

```text
create vlan 20 name Guest
create interface eno1.30 type vlan parent eno1
set    interface eno1 mtu 1500
unset  interface eno1 mtu
add    interface eno1 ipv4-address 192.168.10.20/24
remove interface eno1 ipv4-address 192.168.10.20/24
```

`set`/`unset` are for a field holding one value; `add`/`remove` for one holding
a set. That distinction is what makes the inverse mechanical rather than a
second table: the inverse of `add` is `remove`, and the inverse of `set X` is
`set` back to what the capture reported, or `unset` when it reported nothing.

It covers every device kind an inventory can hold, which is why it is the
default: a campus estate is mostly boxes nobody generates netplan for.

### The five declarative ones

netplan, systemd-networkd, ifupdown, FRR and wg-quick have no command for "give
this interface an MTU of 1500". Their minimal remediation is genuinely *make the
file say this, then reload*, and a plan that pretended otherwise would be
inventing a command language no box speaks.

So minimality for these is at the file level, and it is measured rather than
assumed. The existing emitters are run **twice** — over the declared inventory,
giving what the device should have, and over the *observed* inventory (the
declaration with every observation folded in, which is what
[`netgraph plan --from-live`](plan.md) already builds), giving what it has now.
A file is in the plan only if the two differ, ignoring the generated banner. A
file the observed side has and the declared side does not is removed.

That symmetry also gives `--rollback` for free: the inverse of "write the
declared file" is "write the observed one", and the observed one was generated
from a measurement rather than reconstructed from a diff.

In a declarative dialect the individual changes are still listed, in order, with
their provenance — you can see exactly what the file is doing — but their
`commands` are empty and the file write below them carries the commands. The
file inherits the worst risk of everything it realises: writing a netplan that
no longer holds the management address and running `netplan apply` is exactly as
final as `ip addr del`.

If the *capture* found a device in a shape the dialect has no syntax for, the
plan says so in a note and falls back to listing every generated file rather
than failing. If the *inventory* declares something the dialect cannot express,
the run fails with exit code 4, exactly as `netgraph export` does — half a
configuration on a real box is worse than none.

---

## What netgraph will not write a command for

Three classes of finding stay in the plan as `manual` changes. They are kept
rather than dropped, because "the plan is empty" and "the plan is empty and
three cables are in the wrong ports" are different states of the world — and
they carry no commands, so nothing can pretend otherwise.

* **Cabling.** A cable in the wrong port is somebody walking to a rack.
  Configuration does not move fibre.
* **A physical port the inventory does not declare.** The capture found it
  because it is physically there; the thing that is wrong is the document.
  netgraph only ever deletes interfaces it could have *created* — an interface
  whose `type` is `vlan`, `bridge`, `lag` or `tunnel` — which keeps the rule
  symmetric: netgraph removes what netgraph makes. An `ethernet`, `wifi` or
  `loopback` interface is never removed or shut.
* **A device the inventory does not declare, or one that is a different kind
  than declared.** There is no declared state to converge on to. Run
  [`netgraph import`](import.md) to adopt it, or take it off the network.

An interface whose *type* disagrees is manual too: changing an interface's type
is a re-creation rather than an edit, and netgraph leaves that to somebody who
can see what else is on it.

---

## Per-device scripts, and `--rollback`

`-o/--out DIR` writes one script per element, under a directory named after the
element's fully-qualified name — the same layout
[`netgraph export config --out`](export.md#a-configuration-dialect-writes-a-tree) uses, so the tree is shaped like the
inventory tree and `diff -r` between two runs is readable:

<!-- norun: writes a tree into the reader's directory -->
```console
$ netgraph -i net converge plan --allow-disruptive --dialect networkd -o scripts/ caps/*
2 script(s) written under scripts/
$ find scripts -type f
scripts/hosts/pc-desk/converge.txt
scripts/hosts/srv-nas/converge.txt
```

Each script leads with a header naming the element, the dialect, the captures it
came from, its batch, and how many of its changes are disruptive. Then one block
per change, introduced by the drift finding that asked for it, so a reviewer
scrolling the middle of a long script can still see *why* a line is there. An
element whose changes are all manual gets no file; its items are listed as
comments at the foot of any script it does have.

`--rollback` writes `rollback.txt` instead of `converge.txt`, from the inverse
commands: the state the capture found, not some previous plan and not a state
nobody measured. Run it twice into the same directory and the two land side by
side, which is the point of the different name rather than a different tree — a
reviewer comparing them does not have to hold two paths in their head.

<!-- norun: writes a tree into the reader's directory -->
```console
$ netgraph -i net converge plan --allow-disruptive -o scripts/ caps/*
2 script(s) written under scripts/
$ netgraph -i net converge plan --allow-disruptive --rollback -o scripts/ caps/*
2 script(s) written under scripts/
$ ls scripts/hosts/pc-desk/
converge.txt  rollback.txt
```

Without `-o` the plan already carries every inverse command — `-F json` shows
them per change under `rollback` — so `--rollback` on its own says so rather
than silently doing nothing.

---

## Output formats

| `-F` | For |
|---|---|
| `text` (default) | Reading before deciding. Grouped by device, ordered, every command shown, disruptive changes flagged in the margin, the batches at the end. |
| `json` | A script, or a transport. The whole plan: provenance, prerequisites, commands, rollback, nothing summarised away. |
| `markdown` | A change ticket or a pull-request comment. Tables where a table reads better than a list. |

All three come from one document, so they cannot disagree, and all three are
byte-identical between two runs over unchanged inputs.

---

## The JSON document

```json
{
  "version": "0.1.0",
  "root": "examples/home-lab",
  "inputs": ["sw-home.lldp.json", "patch.csv"],
  "captureDialects": ["csv", "lldp"],
  "dialect": "interfaces",
  "allowDisruptive": true,
  "converged": false,
  "counts": {"create": 0, "update": 2, "delete": 2, "manual": 6},
  "devices": [
    {
      "element": "hosts/pc-desk",
      "kind": "computer",
      "risk": "disruptive",
      "batch": 0,
      "changes": [
        {
          "id": "hosts/pc-desk#interface.set/eno1/mac",
          "action": "update",
          "object": "field",
          "target": "mac",
          "summary": "set mac on eno1 to 3c:97:0e:20:01:01",
          "rank": 30,
          "risk": "disruptive",
          "risk_reason": "changes mac on eno1, which bounces the interface; ...",
          "prerequisites": [],
          "provenance": [{"element": "hosts/pc-desk", "path": "eno1", "field": "mac",
                          "direction": "disagrees", "declared": "3c:97:0e:20:01:01",
                          "observed": "3c:97:0e:20:01:ff", "message": "..."}],
          "commands": [{"kind": "exec", "text": "set interface eno1 mac 3c:97:0e:20:01:01"}],
          "rollback": [{"kind": "exec", "text": "set interface eno1 mac 3c:97:0e:20:01:ff"}]
        }
      ]
    }
  ],
  "batches": [{"index": 0, "elements": ["hosts/pc-desk"], "isolated": [], "splits": []}],
  "notes": []
}
```

A `write` command carries `path` and `content` instead of being a shell line, so
a consumer does not have to parse a here-document out of a script.

---

## Arguments

<!-- generated: arguments converge plan -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[NAME=]INPUT...` | no | any number | — |
<!-- /generated -->

---

## Options

<!-- generated: options converge plan -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--from` | `[auto\|lldp\|iproute\|csv\|netplan\|networkd\|ifupdown\|frr\|wireguard\|interfaces]` | `auto` | Input dialect, exactly as 'netgraph drift --from' takes it. |
| `--host` | `NAME` | — | Device every input was captured on, when the input does not name it. |
| `--dialect` | `[interfaces\|netplan\|networkd\|ifupdown\|frr\|wireguard]` | `interfaces` | Which configuration dialect the commands are written in. 'interfaces' is netgraph's own imperative grammar and covers every device kind; the other five are declarative, so their remediation is the generated file plus a reload. |
| `--only` | `GLOB` | — | Converge only elements whose name matches this glob. Repeatable. |
| `--exclude` | `GLOB` | — | Leave elements whose name matches this glob out of the plan. Repeatable. |
| `--exclude-interface` | `GLOB` | — | Leave interfaces whose name matches this glob out, as 'drift' takes it. Repeatable. |
| `--allow-disruptive` | — | off | Emit a plan even when a change touches the path a device is managed on. Without this the whole plan is refused and nothing is written. |
| `--rollback` | — | off | Emit the inverse of every change: the commands that put each device back the way the capture found it. Affects the scripts written by --out. |
| `-F`, `--format` | `[text\|json\|markdown]` | `text` | text is for reading, json for a script or a transport, markdown for a ticket. |
| `-o`, `--out` | `DIR` | — | Write one .txt script per device under DIR, laid out like the inventory tree. |
<!-- /generated -->

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Converged: the network already matches the inventory, and there is nothing to do. |
| 1 | The inventory itself does not load, so nothing was compared. |
| 2 | Changes are pending — or a usage error, or an unusable `netgraph.toml`. |
| 3 | An input was missing, unreadable, not UTF-8, oversized, or not the dialect it was given as. |
| 4 | The plan was refused: it holds a disruptive change and `--allow-disruptive` was not given, or a dialect cannot express a declared device. Nothing was written. |

The 0/2 contract mirrors [`netgraph plan`](plan.md), so a pipeline can gate on
"nothing to do" without parsing anything. A `manual` change counts: a plan that
is empty except for three cables in the wrong ports is not a converged network,
and an exit code saying otherwise would be the one thing this command must not
get wrong.

---

## See also

* [`netgraph drift`](drift.md) — the differences this plan closes, and what each
  capture dialect can and cannot see.
* [`netgraph export`](../export.md#device-configuration-the-six-dialects) — the six
  configuration dialects, and what each one is lossy about.
* [`netgraph impact`](impact.md) — the blast-radius engine the batches use.
* [`netgraph plan`](plan.md) and [`netgraph apply`](apply.md) — the same
  discipline pointed at the inventory files rather than at devices.
* [Importing a live network](../importing.md) — what to collect, and the exact
  collection command for each dialect.
