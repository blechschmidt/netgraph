# `netgraph drift`

`netgraph drift` reads the same live-device output [`netgraph import`](import.md)
does — LLDP neighbour tables, `ip -j` captures, cabling lists — but the other way
round. `import` turns a network into an inventory; `drift` treats the inventory
as an **assertion about the network** and reports where reality disagrees. It is
the command that keeps a committed inventory honest once the first one exists.

No host is contacted and no credential is read: you run the collection command
and hand netgraph what it printed, exactly as for `import`, so the check works
from a laptop with no route to the network it is checking.

---

## Contents

- [Synopsis](#synopsis)
- [What it reports](#what-it-reports)
- [Drift and unobserved](#drift-and-unobserved)
- [What each dialect can see](#what-each-dialect-can-see)
- [Generate, then compare](#generate-then-compare)
- [A worked example](#a-worked-example)
- [Output formats](#output-formats)
- [The JSON envelope](#the-json-envelope)
- [Narrowing the comparison](#narrowing-the-comparison)
- [Arguments](#arguments)
- [Options](#options)
- [Exit codes](#exit-codes)
- [See also](#see-also)

---

## Synopsis

<!-- generated: synopsis drift -->
```text
netgraph [GLOBAL OPTIONS] drift [OPTIONS] [NAME=]INPUT...
```
<!-- /generated -->

The inventory comes from the global `-i/--inventory`; the arguments are the
captures, in the same `[NAME=]INPUT` form `import` takes, with `-` for standard
input.

---

## What it reports

Three questions, per element, marked in the output the way a diff is:

| Mark | Direction | Question it answers |
|---|---|---|
| `+` | `undeclared` | What does the network have that the inventory does not? An interface nobody wrote down, an unexpected address, a VLAN on a trunk, a neighbour LLDP sees that no cable joins. |
| `-` | `missing` | What does the inventory declare that the network does not have? |
| `~` | `disagrees` | What do both have and spell differently — a MAC, an MTU, a speed, a medium, an address? |

What is compared: the device kind where the capture determined one; the
interface set of a host an `iproute` capture covered; per interface `mac`, `mtu`,
`enabled` and `type`, the addresses of each family, bridge and bond membership,
and the VLANs the port was seen carrying; links, matched by their endpoint pair
rather than by name, because a capture never learns what a cable is *called*;
and per cable `medium`, `speed` and `label`.

Four things are deliberately **not** compared, because comparing them would
manufacture differences rather than find them:

* **`description`.** An LLDP chassis description is a vendor's prose
  (`Debian GNU/Linux 12 (bookworm) Linux 6.1.0-18-amd64`); the inventory's is a
  human's note about intent. Every device would drift on the first run.
* **`vendor`, `model`, `serial`, `location`.** No dialect reports any of them as
  a field, and pulling a model out of the middle of an LLDP sentence would be
  guesswork presented as a measurement.
* **A field the inventory leaves unset.** An absent `mtu:` is not a claim that
  the interface has no MTU, so a capture reporting one contradicts nothing.
  Silence cannot drift.
* **`spec.vlans`, the device VLAN database.** Every VLAN a capture sees is seen
  *on a port*, and that is where it is reported; checking the database too would
  list one difference twice under two names.

---

## Drift and unobserved

A capture is always partial. `lldpctl` never reports an address; `ip -j link
show` does not either, though `ip -j addr show` does; no dialect netgraph reads
prints the VLAN set of a trunk. If absence were read as deletion, the first run
against one host's output would announce that the rest of the network had been
unplugged.

So every absence is classified before it is reported:

**Drift** is a real disagreement. The dialect that produced the capture *can*
see this kind of thing, it looked, and what it found is not what the inventory
says.

**Unobserved** is a blind spot. The dialect cannot see this kind of thing at
all, so the inventory's claim is neither confirmed nor denied. It is listed in
its own section, with the reason it could not be checked, and it **never counts
as drift**: it does not appear in the difference tally, it does not affect
`--fail-on`, and in `-F junit` it is a *skipped* test rather than a failing one.

That distinction is the whole reason the command is safe to schedule. A cron job
that captures whatever it can reach will report more unobserved items on a bad
night and no more drift.

---

## What each dialect can see

Coverage is a property of the dialect, not of the run, and a device captured by
two dialects gets the union of both:

| `--from` | Interface set | Links | Addresses | Members |
|---|---|---|---|---|
| `lldp` | no — only the ports with a neighbour | yes | no | no |
| `iproute` | yes | no | yes, from `ip -j addr show` | yes |
| `csv` | no — only the ports the rows name | yes | no | no |
| `netplan` | yes | no | yes | yes |
| `networkd` | yes | no | yes | yes |
| `ifupdown` | yes | no | yes | yes |
| `interfaces` | yes | no | yes | yes |
| `frr` | no | no | no | no |
| `wireguard` | no | no | no | no |

The last six are the configuration dialects
[`netgraph export`](export.md#the-fourteen-formats) writes, read back, and they
split into two groups for one reason: **does this file describe the whole device,
or a part of it?**

`netplan`, `networkd`, `ifupdown` and `interfaces` describe the whole of a host's
networking. An interface absent from a netplan document is an interface that host
does not bring up, so its absence is a difference and is reported as one. That
makes them the strongest inputs in the table — stronger, in one way, than `ip`:
they say what the box was *told* to be, which is the thing an inventory also
says. What none of them reports is a **neighbour**. A configuration says what a
box does with a port, never what is plugged into it, so every declared cable is
unobserved unless an `lldp` or `csv` input covers it too.

`frr` and `wireguard` describe a *part*, and are therefore blind on every axis. An
`frr.conf` configures the interfaces the routing daemon cares about and is silent
about every other link on the box; a wg-quick file is one tunnel. Reporting a
declared interface as missing because it is not in one of those would be
nonsense. Their capability is deliberately spelled out as empty rather than
omitted, because it is a decision and not an oversight — and it is *directional*
rather than useless: an address FRR configures that the inventory does not
declare is still real drift in the `undeclared` direction.

Three consequences worth knowing:

* A declared interface missing from an `iproute` capture **is** drift; missing
  from an LLDP capture it is unobserved, because LLDP lists only the ports that
  saw a neighbour.
* A declared cable no capture shows is drift only when one of its ports was seen
  connected to *something else*. A silent neighbour, or a port left out of a
  cabling list, is unobserved rather than unplugged.
* A declared address is drift only when the same device yielded some address of
  that family. Capture `ip -j link show` alone and every address is unobserved,
  which is the honest answer.

Two refinements go beyond the table. `ip -j addr show` and `ip -j link show` are
the same dialect and only one of them carries addresses, so address coverage
additionally requires that an address really was observed on the device. And a
trunk's VLAN set is always a *lower bound*: netgraph derives the minimum implied
by the sub-interfaces stacked on the port, so a VLAN there and not in the
inventory is drift, while the converse is unobserved.

---

## Generate, then compare

Six of the dialects above are the ones
[`netgraph export`](export.md#a-configuration-dialect-writes-a-tree) writes, which
makes the loop symmetric: what the emitter writes is exactly what this command
reads. Two commands, and no bespoke collection script:

<!-- norun: writes want.yaml into the reader's directory -->
```console
$ netgraph -q -i examples/home-lab export netplan --name pc-desk -o want.yaml
$ netgraph -i examples/home-lab drift --only pc-desk want.yaml
drift of examples/home-lab against 1 input (netplan)

unobserved (3)
  declared, but outside what these dialects see; never counted as drift
  cables/cbl-sw-desk hosts/pc-desk:eno1, switches/sw-home:port2: no input reported the neighbours of either end; 'lldp', 'csv' do, and nothing else netgraph reads does — a configuration says what a box does with a port, never what is plugged into it
  hosts/pc-desk:eno1 enabled: the capture reports no value for these fields, so the declared ones could not be checked
  hosts/pc-desk:lo: a loopback interface is not something iproute lists, so its absence from the capture says nothing

no drift: 1 element compared, 3 unobserved
```

Neither `--from` nor `--host` was given, and neither was needed: a file netgraph
generated carries `netgraph-dialect:` and `netgraph-element:` in its banner, so it
names its own dialect and its own device. A configuration collected off a real
box has neither, and then the sniffer decides from the shape of the file and
`--host` names the device — which is the form the check actually runs in:

<!-- norun: needs the file that is really running on pc-desk -->
```console
$ ssh pc-desk 'cat /etc/netplan/*.yaml' | netgraph -i examples/home-lab drift --host pc-desk -
```

Two things follow from comparing against a *configuration* rather than against a
capture, and both are the point rather than a limitation.

**A configuration is intent, not observation.** It says what the device was asked
to be, which is not always what it is doing: a link may be down, an address may
have failed to apply, a file may not have been reloaded since it was edited.
`ip -j addr show` answers the other question. Both are worth asking, and the
report names the dialects that saw each device so the answer says which question
was asked.

**`frr` and `wireguard` can only add.** They see nothing, per the table above, so
they never report a declared interface, address or member as missing — only
something configured that the inventory does not declare. Use one of them to
catch an address or a neighbour somebody added to a router by hand; do not expect
either to notice that something was removed. For that, pass both files in one run
— `netgraph drift --host rtr-edge frr.conf 10-netgraph.yaml`. Coverage is the
union over every dialect that saw a device, so the netplan file supplies the
interface, address and membership coverage the `frr.conf` cannot.

---

## A worked example

`tests/fixtures/drift/` holds captures taken against the
[home-lab example](../../examples/home-lab). `pc-desk.addr.json` is an
`ip -j addr show` from the desktop with two things wrong in it: the NIC reports a
MAC ending `:ff` where the inventory says `:01`, and there is an `eno1.30`
sub-interface carrying a VLAN 30 nobody declared.

<!-- run: cwd=. rc=1 -->
```console
$ netgraph -i examples/home-lab drift --only pc-desk tests/fixtures/drift/pc-desk.addr.json
drift of examples/home-lab against 1 input (iproute)

hosts/pc-desk (computer)
  ~ eno1.mac                           declared as 3c:97:0e:20:01:01; the capture reports 3c:97:0e:20:01:ff
  + eno1.vlan                          VLAN 30 is carried on this port; the inventory declares no VLAN here
  + eno1.30                            the capture reports this interface as vlan; the inventory does not declare it

unobserved (2)
  declared, but outside what these dialects see; never counted as drift
  cables/cbl-sw-desk hosts/pc-desk:eno1, switches/sw-home:port2: no input reported the neighbours of either end; 'lldp', 'csv' do, and nothing else netgraph reads does — a configuration says what a box does with a port, never what is plugged into it
  hosts/pc-desk:lo: a loopback interface is not something iproute lists, so its absence from the capture says nothing

3 differences across 1 element (+2 undeclared, ~1 disagrees); 2 unobserved
```

The wrong MAC and the extra VLAN are drift. The two unobserved entries are the
command refusing to lie: `iproute` says nothing about who is plugged into whom,
and it does not import loopback interfaces at all, so neither the cable nor `lo`
was checked. Neither is counted in the tally.

`wlp1s0` is in the capture and in the inventory and agrees — the inventory
declares it as `wifi` and `ip` says `link_type: ether`, which is what `ip` says
about every NIC, so netgraph treats the two as consistent rather than as a
difference.

A switch is not a Linux host, so its configuration reaches netgraph as a
neighbour table or a patch list rather than as `ip` output. Run the same command
over `sw-home.lldp.json` and the shape of the answer is the same:

<!-- run: cwd=. rc=1 -->
```console
$ netgraph -i examples/home-lab drift --only 'sw-home' --only 'prn-*' tests/fixtures/drift/sw-home.lldp.json
drift of examples/home-lab against 1 input (lldp)

cbl-prn-hall-sw-home (cable)
  + link                               sw-home.lldp.json shows prn-hall:eth0 connected to switches/sw-home:port6; no cable declares that link

prn-hall
  + device                             sw-home.lldp.json observed a device called 'prn-hall'; the inventory declares no element of that name

switches/sw-home (switch)
  + port6                              the capture reports this interface as ethernet; the inventory does not declare it

unobserved (12)
  declared, but outside what these dialects see; never counted as drift
  cables/cbl-sw-ap switches/sw-home:port5, wireless/ap-home:eth0: switches/sw-home reported no neighbour on this port; a device that does not speak LLDP, or a port left out of a cabling list, is invisible to the capture
...
  switches/sw-home:port1 enabled, mac, mtu: the capture reports no value for these fields, so the declared ones could not be checked
  switches/sw-home:port1.vlan access: no input reported the VLAN configuration of this port; only a VLAN sub-interface makes one visible to 'ip'
...
3 differences across 3 elements (+3 undeclared); 12 unobserved
```

Somebody plugged a printer into port 6 and did not write it down. The ports that
*are* declared and *did* report their expected neighbour — port1 to `rtr-home`,
port2 to `pc-desk` — produce nothing at all, which is what agreement looks like.

To drive a vendor switch's running configuration through this, turn its
interface and cabling tables into the four-column `device,port,device,port` CSV
the [`csv` dialect](import.md#what-it-reads) reads; `tests/fixtures/drift/patch.csv`
is one. A moved patch lead then shows up as a pair — the declared link missing
and the observed one undeclared — with the port that contradicts it named.

---

## Output formats

`-F/--output-format` picks one of three:

| Value | Goes to | For |
|---|---|---|
| `text` | stdout | Reading. Grouped by element, blind spots in their own section. |
| `json` | stdout | A script, a dashboard, a diff of two runs. The one-line summary moves to stderr. |
| `junit` | stdout | A CI test report. One test case per element, so the row list stays put between runs and goes red and green rather than growing and shrinking. |

In `junit`, an element that drifted is a `<failure>` whose body lists every
difference; an element with nothing but blind spots is `<skipped>` with the
reason; an element that was compared and agreed is a passing case. That is what
makes a run of six devices show six rows rather than only the broken ones.

<!-- run: cwd=. rc=1 -->
```console
$ netgraph -i examples/home-lab drift --only pc-desk -F junit tests/fixtures/drift/pc-desk.addr.json
<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="netgraph drift" tests="2" failures="1" errors="0" skipped="1">
  <testsuite name="netgraph drift" tests="2" failures="1" errors="0" skipped="1">
...
    <testcase classname="netgraph.drift.cable" name="cables/cbl-sw-desk">
      <skipped message="no input reported the neighbours of either end; 'lldp', 'csv' do, and nothing else netgraph reads does — a configuration says what a box does with a port, never what is plugged into it"/>
    </testcase>
    <testcase classname="netgraph.drift.computer" name="hosts/pc-desk">
      <failure message="3 differences from the captured network" type="drift">
~ hosts/pc-desk:eno1.mac: declared as 3c:97:0e:20:01:01; the capture reports 3c:97:0e:20:01:ff
+ hosts/pc-desk:eno1.vlan: VLAN 30 is carried on this port; the inventory declares no VLAN here
+ hosts/pc-desk:eno1.30: the capture reports this interface as vlan; the inventory does not declare it
      </failure>
    </testcase>
  </testsuite>
</testsuites>

3 differences between the inventory and the capture, 2 unobserved items
```

The last line is on stderr, so `netgraph … -F junit > drift.xml` writes a file a
JUnit reader accepts while a person watching the run still sees what happened.
`-q` drops that summary; it never drops the document.

---

## The JSON envelope

`schemaVersion` is bumped only for a change a consumer could trip over — a new
optional key does not count, a renamed one does.

| Key | Contents |
|---|---|
| `schemaVersion` | `1`. |
| `tool` | `{"name": "netgraph", "version": …}`. |
| `inventory.root` | Absolute path of the tree the assertion came from. |
| `capture.inputs` | Input names, in command-line order. |
| `capture.dialects` | The dialects they were read as, sorted. |
| `capture.devices` | The device names the capture yielded. |
| `summary` | `undeclared`, `missing`, `disagrees`, `total`, `unobserved`, `compared`, `filtered`. |
| `drifted` | `true` when `total` is non-zero. Blind spots never set it. |
| `compared` | Fully-qualified names of the elements checked on both sides. |
| `drift` | One record per difference: `direction`, `scope`, `element`, `kind`, `path`, `field`, `declared`, `observed`, `message`. |
| `unobserved` | One record per blind spot: `element`, `kind`, `scope`, `path`, `items`, `reason`. |

Both arrays are sorted by element, then by path, then by field, so two runs over
an unchanged inventory and an unchanged capture produce byte-identical output
and a report can be committed and diffed.

---

## Narrowing the comparison

`--only GLOB` and `--exclude GLOB` are shell-style globs matched against both the
fully-qualified and the short name of an element, so `--only 'sw-*'` and
`--only 'sites/north/*'` both work, and both are repeatable. A link is compared
when at least one of its ends is selected and neither is excluded — a cable to a
device that is explicitly out of scope is out of scope too, but a cable to a
device that simply was not asked for still belongs to the device that was.

`--exclude-interface PATTERN` is the counterpart of
[`import --exclude`](import.md#what-it-reads), and should be given the same
patterns the capture was taken with: a declared interface it matches can never be
reported as missing, because the capture was told not to look at it. It also
keeps `veth*` and `docker*` out of the observed side, where they would otherwise
read as undeclared interfaces.

`--from` and `--host` behave exactly as they do for
[`netgraph import`](import.md#naming-the-host-a-capture-came-from): `auto`
sniffs each input on its own, and an `lldp` or `iproute` capture takes its host
from `NAME=PATH`, from `--host`, or from the file name, in that order.

---

## Arguments

<!-- generated: arguments drift -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[NAME=]INPUT...` | no | any number | — |
<!-- /generated -->

---

## Options

<!-- generated: options drift -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--from` | `[auto\|lldp\|iproute\|csv\|netplan\|networkd\|ifupdown\|frr\|nftables\|wireguard\|interfaces]` | `auto` | Input dialect, as for 'netgraph import'. 'auto' sniffs each input on its own: lldp is 'lldpctl -f json', iproute is 'ip -j link show' or 'ip -j addr show', csv is 'device,port,device,port' cabling rows, and netplan, networkd, ifupdown, frr, nftables, wireguard and interfaces are the running configuration in the same dialects 'netgraph export' writes. |
| `--host` | `NAME` | — | Device every input was captured on. An lldp or iproute capture never names its own host. Without this the name comes from the file name, or from a 'NAME=path' argument. |
| `--only` | `GLOB` | — | Compare only elements whose fully-qualified or short name matches this glob. Repeatable. |
| `--exclude` | `GLOB` | — | Leave elements whose name matches this glob out of the comparison. Repeatable. |
| `--exclude-interface` | `PATTERN` | — | Leave out interfaces whose name matches this glob, as 'netgraph import --exclude' does. A declared interface it matches can never be reported as missing. Repeatable. |
| `-F`, `--output-format` | `[text\|json\|junit]` | `text` | text is for reading; json is for a script, junit for a CI test report. |
| `--fail-on` | `[drift\|none]` | `drift` | Exit 1 when the network disagrees with the inventory, or never. An unobserved field is not a disagreement and never fails the run. |
<!-- /generated -->

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The comparison ran. Either nothing drifted, or `--fail-on none` was given. |
| 1 | The network disagrees with the inventory and `--fail-on drift` (the default) was in force — or the inventory itself does not load, in which case nothing was compared. |
| 2 | Usage error, or an unusable `netgraph.toml`. |
| 3 | An input was missing, unreadable, not UTF-8, oversized, or not the dialect it was given as. |

`--fail-on` is what makes the command a gate. The default `drift` exits 1 on any
difference, which is what a CI job or a cron check wants; `none` always exits 0,
which is what a dashboard that reads the JSON wants. Neither is affected by the
unobserved section.

An inventory that does not load is refused before any comparison happens: a
document the loader rejected is absent from the comparison entirely, so every
element in it would be reported as something the network has and the inventory
does not. Fix it with [`netgraph validate`](validate.md) first.

---

## See also

* [netgraph in CI](../ci.md#workflow-a-scheduled-drift-check) — running this on
  a schedule, and what to do with the result.
* [`netgraph import`](import.md) — the same captures, read the other way round.
* [Importing a live network](../importing.md) — what to collect for each dialect,
  and the exact collection command.
* [`netgraph validate`](validate.md) — the inventory checked against itself
  rather than against the network.
