# Bootstrapping from a live network with `netgraph import`

[`netgraph init`](commands/init.md) is for a network you are about to build.
`netgraph import` is for the one you already have: it turns machine-readable
output collected from real devices into a starting inventory, so the first tree
is a diff away from correct instead of a weekend of transcription. This page is
the task — what to collect, how to name it, what netgraph concludes from it and
what it deliberately leaves for you. The option-by-option reference is
[`netgraph import`](commands/import.md).

**No network access, ever.** netgraph opens no socket, reads no credential and
runs nothing on a device. You run the collection command — the exact lines are
[below](#collecting-the-input) — and hand netgraph what it printed, from a file
or from a pipe. The command works fine on a laptop with no route to the network
it is documenting, and it keeps netgraph out of the business of holding switch
passwords.

---

## Contents

- [Why bootstrap at all](#why-bootstrap-at-all)
- [The workflow](#the-workflow)
- [Collecting the input](#collecting-the-input)
  - [Why CSV and not NetJSON](#why-csv-and-not-netjson)
- [Naming the host a capture came from](#naming-the-host-a-capture-came-from)
- [What each dialect reads](#what-each-dialect-reads)
  - [`lldp`](#lldp)
  - [`iproute`](#iproute)
  - [`csv`](#csv)
- [What it will and will not write](#what-it-will-and-will-not-write)
  - [Observed, inferred, absent](#observed-inferred-absent)
  - [What is left out, and said so](#what-is-left-out-and-said-so)
- [Re-running an import](#re-running-an-import)
- [The findings afterwards are expected](#the-findings-afterwards-are-expected)
- [Limits](#limits)

---

## Why bootstrap at all

Every other netgraph command assumes the YAML tree exists, and typing the first
one out by hand is the largest barrier the project has: a forty-port switch is
forty interfaces nobody wants to transcribe, and the transcription is wrong by
the time it is finished. The devices already know most of it. LLDP knows which
port faces which neighbour, `ip` knows one host's interfaces down to the MTU,
and the cabling is usually in a spreadsheet somewhere. `import` reads those
three things and writes the tree, so the work left is correcting a draft rather
than producing one.

The tree is written in the layout `init` produces — `devices/<name>.yaml`, one
document per device, plus `cables/links.yaml` — with a `yaml-language-server`
modeline on every file, so the result opens in an editor with completion already
working. See [inventory layout](inventory-layout.md) for what the loader makes
of that tree, and [getting started](getting-started.md) for the rest of the
first-inventory path.

---

## The workflow

Collect, import into an empty directory, format, validate, fix the gaps, commit.

**1. Collect.** Run the commands in [the next section](#collecting-the-input) on
each device and keep the output in one directory, one file per host named after
it.

**2. Import into a directory of its own.** `import` refuses to overwrite
anything it did not write, so pointing `-o` at an empty directory keeps the
first attempt cheap to throw away:

<!-- norun: the capture files are the reader's own, and the command writes a tree -->
```console
$ netgraph import -o net --exclude 'veth*' collected/*.json collected/patch-panel.csv
4 notes about what was not imported:
  srv-hyper.addr.json: 'lo' is the kernel loopback; it terminates no cable and holds only host-scope addresses, so it was not imported
  srv-hyper.addr.json: 'wg0' is a wireguard tunnel; netgraph models a tunnel as its own document naming both ends (docs/schema.md §14) and 'ip' shows only this end, so it was not imported
  ...

wrote 9 files to net:
  devices/ap-lobby.yaml
  devices/pc-alice.yaml
  ...
  cables/links.yaml
  schema/netgraph.schema.json

imported 7 devices and 7 cables from 4 inputs

warnings (15):
  ...
I002, W101, W105 are expected of an imported tree: a port whose neighbour was
never captured terminates no cable, and a device only a neighbour named has no
configuration of its own. Capture the missing hosts and re-run, or fill the gaps
in by hand — they are not errors in what was imported.
```

**3. Format it.** What `import` writes is valid but not canonical: it quotes
every string that came off a device and leaves MACs plain, where [the canonical
form](format.md) does the opposite. One `netgraph fmt` settles that, and it
keeps every comment the importer wrote.

<!-- norun: operates on the tree the previous step wrote -->
```bash
netgraph -i net fmt
netgraph -i net validate
```

**4. Fix the gaps.** The report from step 2 already names them. Correct every
`kind:` that came out as the neutral default, fix the fibre runs, extend the
trunk lists, name the VLANs — and capture the hosts the tree only knows by
hearsay, then import them into a fresh directory and diff.

**5. Commit.** The generated YAML is meant to be edited and committed, not
regenerated. From here on the inventory is the source of truth and the captures
are history.

---

## Collecting the input

Run these on each device and keep the output in one directory, one file per host
named after it. Nothing else is needed.

| Dialect | What to run | What it gives |
|---|---|---|
| `lldp` | `lldpctl -f json > "$(hostname -s).lldp.json"`<br>or `lldpcli -f json show neighbors > …` | Both ends of every link with a neighbour: device names and the port pair, which is exactly a `cable`. Also the neighbour's kind, from its advertised system capabilities. |
| `iproute` | `ip -j addr show > "$(hostname -s).addr.json"` | One host in full: interfaces, MAC addresses, MTUs, admin state, IPv4/IPv6 addresses, and — via `linkinfo` — bridges, bonds and VLAN sub-interfaces. `ip -j link show` also works and is a subset; pass both and they merge. |
| `csv` | whatever produces `device,port,device,port` rows | The cabling you already have written down. Optional fifth and sixth columns are `medium` and `label`. A header row is detected and skipped. |

On a Cisco or Juniper box `lldpctl` is not available, but the neighbours are:
`show lldp neighbors detail` and its JSON forms are not read by this command —
turn them into the four-column CSV instead, which is a one-line `awk` and is
what the CSV dialect exists for.

You do not have to say which file is which. `--from` defaults to `auto`, which
sniffs each input on its own, so one run may mix all three dialects — that is
what makes `netgraph import collected/*` work on a directory holding an LLDP
capture, two `ip` captures and a patch list.

### Why CSV and not NetJSON

NetJSON's `NetworkGraph` describes nodes and links, but a link has no notion of
a *port*: its ends are node ids. Importing it would have to drop the interface
pair — the one thing that makes a netgraph `cable` a cable rather than a line on
a picture — or invent interface names to hang the link on, which is precisely
what this command refuses to do. It would also cost several hundred lines of
shape-guessing across the NetworkGraph, NetworkCollection and
DeviceConfiguration variants. Four columns carry exactly what a cable needs, and
where you do have NetJSON, one `jq` produces them:

<!-- norun: a shell pipeline over the reader's own topology file -->
```console
$ jq -r '.links[] | [.source, "?", .target, "?"] | @csv' topology.json > links.csv
```

(then replace the `?`s with the ports, which is the information NetJSON does not
hold.)

---

## Naming the host a capture came from

An `lldpctl` or `ip` capture describes one host and never says which. netgraph
takes the name from the first of these that applies:

1. `NAME=path` on the argument — `netgraph import sw-core=neighbors.json`. The
   leading segment counts as a name only when it is already a legal
   [element name](schema.md#41-name-grammar), so a path that happens to hold an
   `=` is still read as a path;
2. `--host NAME`, which applies to **every** input of that run, so
   `--host pc1 link.json addr.json` means the obvious thing. A `--host` that is
   not a legal element name is refused rather than rewritten;
3. the file name up to its first dot — `sw-core-01.lldp.json` → `sw-core-01`.
   Everything from the first dot is treated as a suffix chain, so the usual
   collection convention needs no flag at all.

A name that came from a file name is recorded as such in the generated document,
because it is the one field the capture did not supply:

```yaml
# the device name came from the file name 'srv-hyper.addr.json'; the capture itself
# does not say which host it was taken on — pass --host to state it
```

A CSV needs none of this: every row names both of its devices. And a capture
that reaches netgraph with no name at all — piped in as `-` without `--host` —
is refused, with the three ways to fix it.

---

## What each dialect reads

Two of the three dialects describe one host from its own point of view; the
third describes links between hosts. Between them they cover the two halves of
an inventory: `devices/` and `cables/`.

### `lldp`

`lldpctl -f json` and `lldpcli -f json show neighbors`. An LLDP neighbour record
is very nearly a `cable` already: it names the local port, the neighbour's system
name and the neighbour's port, which is exactly the pair of `device:interface`
endpoints a cable joins.

* **Both ends at once.** One capture on one host yields the host's ports *and* a
  stub device for every neighbour, including neighbours that will never be
  captured themselves — a printer, an unmanaged switch, an access point.
* **It is self-checking.** Run on both ends of a link, LLDP reports the
  adjacency twice; the two reports merge into one cable whose comment names both
  captures. A link that appears only once is visible as such, because only one
  host's name ends up in that comment.
* **Shape tolerance.** lldpd has two JSON encodings and has changed both across
  releases: `-f json` keys objects by name and inlines scalars, `-f json0` wraps
  everything in single-element lists and every scalar in `{"value": …}`. Both are
  read, and a shape netgraph does not recognise yields "not present", which is
  reported, rather than a traceback.
* **The neighbour's kind** comes from the system capabilities it advertises *and
  has enabled*: `Bridge` becomes a `switch`, `Router` a `router`, `Repeater` a
  `hub`, `Wlan` a `switch`. `Bridge` wins over `Router`, because in this model an
  L3 switch is still a switch — and preferring it keeps the command from
  promoting every box that happens to forward packets into a `router`.
* **A neighbour that advertises no system name** is named after the chassis id
  LLDP reported for it, with a comment saying so. That is an observed, stable
  identifier of that box; a counter would not be.
* **`mgmt-ip` is not imported.** It is an observed address, but LLDP says neither
  which interface holds it nor its prefix length, and a netgraph address needs
  both. It becomes a comment on the device instead of an invented `/24`.

Only ports with a neighbour are visible to LLDP, so every device an LLDP capture
produces carries a comment saying that a port with no neighbour is missing from
it.

### `iproute`

`ip -j link show` and `ip -j addr show`, the only dialect that describes a
device's *configuration* rather than its neighbours. One capture yields interface
names, MAC addresses, MTUs, admin state, addresses and — through `linkinfo` — the
three stacking constructs netgraph models:

| `linkinfo` | netgraph | where the relationship comes from |
|---|---|---|
| `info_kind: bridge` | `type: bridge` | `members`, from the `master` field of every enslaved link |
| `info_kind: bond`, `team` | `type: lag` | the same |
| `info_kind: vlan` | `type: vlan` | `parent` from `link`, the VID from `info_data.id` |

`veth`, `dummy`, `macvlan`, `macvtap` and `ipvlan` links are written as
`type: ethernet` with a comment saying which kernel type they really were: as far
as this model is concerned they carry frames, hold addresses and can be bridged.

`ip -j addr show` is a superset of `ip -j link show`, so passing both files for
one host is not merely allowed but the expected thing to do. They merge, first
observation winning and later ones filling gaps, and the argument order does not
matter. `ip -j` prints a bare JSON array; a capture pasted into a wrapper object
by a collection script — an Ansible `stdout` field, a `{"links": […]}` envelope —
is unwrapped rather than refused.

Admin state is the `UP` flag — the *administrative* state, which is what
`enabled` means — and not `operstate`, which is the carrier and is not modelled.
Only an observed *down* port is written out, since the schema default is
`enabled: true`.

`--exclude PATTERN` drops interfaces whose name matches a glob, repeatably. It
applies to this dialect, where `veth*` and `docker*` are rarely part of a
physical topology.

### `csv`

`device,port,device,port[,medium[,label]]`, one row per cable:

```
# comments and blank lines are ignored
device,port,device,port[,medium[,label]]
sw-core,Gi0/1,pc-alice,eno1
sw-core,Te1/1,sw-edge,Te0/1,fiber,A-014
```

A header row is detected and skipped — both *port* columns have to carry a
recognised label, so a switch whose ports are genuinely called `port1` is not
mistaken for one. `medium` must be `copper`, `fiber` or `wireless`; when the
column is absent the cable reads `copper` and says the value was filled in.
Both devices are created if they are not already in the draft, so a CSV alone
produces a complete, loadable inventory rather than a set of dangling
references.

A cabling list is short and hand-maintained, so a mistake in it stops the run
rather than dropping a link quietly: a row with fewer than four or more than six
fields, an unknown medium, or a name with no usable character in it is refused
with the row number.

<!-- run: cwd=tests/fixtures/import -->
```console
$ netgraph import --dry-run --from csv patch-panel.csv
...
# ===== cables/links.yaml =====
...
# listed in patch-panel.csv, row 4
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: cbl-srv-hyper-eno1-sw-core-01-GigabitEthernet1-0-1
spec:
  endpoints:
    - srv-hyper:eno1
    - sw-core-01:GigabitEthernet1/0/1
  medium: copper
  label: A-014
...
imported 4 devices and 4 cables from 1 input
...
```

(`tests/fixtures/import/` holds the captures netgraph's own tests import, so
every example on this page can be run as it stands.)

---

## What it will and will not write

The generated YAML is meant to be edited and committed, not regenerated, so it
is formatted for a reader: fields in the order of [`docs/schema.md`](schema.md),
a header explaining where the file came from, and a comment beside anything
netgraph *concluded* rather than read. Interfaces are written physical ports
first and stacked interfaces after them, so a bridge reads below the ports it is
built from.

### Observed, inferred, absent

Nothing is invented. A field no capture covers is absent — there is no
placeholder `vendor:`, no example address, no `description: TODO`. Where the
kind of a device cannot be determined the document says `kind: computer` and says
why, rather than promoting a box that happens to forward packets into a `router`:

```yaml
# inferred: nothing in the captured output states what this device is; 'computer'
# is netgraph's neutral default — correct it by hand
kind: computer
```

Four things are concluded rather than observed, and each is commented in place:

* **A cable's `medium`.** The schema requires one and no capture reports it, so
  every cable reads `copper` unless a CSV column said otherwise. Fix the fibre
  runs before trusting an `l1` diagram.
* **A device's kind, from LLDP capabilities.** A neighbour advertising `Bridge`
  becomes a `switch`, `Router` a `router`, `Repeater` a `hub`.
* **A trunk under a VLAN sub-interface.** `eno1.100` can only receive frames if
  `eno1` carries VLAN 100 tagged, so the parent gets
  `vlan: {mode: trunk, trunk_vlans: [100]}`. `ip` never reports a port's VLAN
  set, so the list is a *minimum* — extend it. Without this the tree would fail
  [`E009`](validation-rules.md#e009--sub-interface-vlan-not-carried-by-its-parent)
  on a configuration that is perfectly real.
* **The VLAN database**, from the VLAN ids observed on the ports. Names and
  descriptions are not reported by anything, so add those by hand.

Three smaller conclusions are commented in the same way. A name a device
reported that is not a legal netgraph identifier is rewritten deterministically
and the original recorded beside it — LLDP happily reports `Port 1` or a chassis
name with a trailing dot, and rejecting those inputs would defeat the point of
the command. An address the kernel says came from DHCP or SLAAC is written as
configuration with a comment telling you to confirm that it is fixed. A
sub-interface `ip` reports as something other than 802.1Q is flagged rather than
silently treated as 802.1Q.

Cable names are derived from the endpoints: the single link between two devices
is `cbl-pc-alice-sw-core-01`, and where a pair is joined by more than one cable
*every* cable of that pair takes the long form with both ports in it, so no
existing name changes when a later capture finds a sibling.

### What is left out, and said so

Four things are deliberately left out, each reported on stderr rather than
dropped silently: the kernel loopback, link- and host-scope addresses (`fe80::`,
`127.0.0.1` — facts about a running kernel, not configuration), the MAC a
bridge, bond or VLAN sub-interface borrows from what is underneath it, and
tunnel interfaces, since netgraph models a tunnel as its own document naming
both ends ([`docs/schema.md`](schema.md#14-tunnels) §14) and `ip` shows only one
end.

Writing a derived MAC out would state as configuration something the kernel
chose, and would trip
[`E003`](validation-rules.md#e003--duplicate-mac-address) on the most ordinary
Linux host there is.

Four further things cannot become a document and are reported for the same
reason:

* a bridge or bond whose members were not in the capture — netgraph requires at
  least one member, so the aggregate is left out and the device says so;
* a VLAN interface whose parent link or VLAN id the capture does not report;
* a device an input named but no interface of which was observed, which happens
  when a neighbour's port id could not be read; the schema needs at least one
  interface, so no document is written;
* a cable one of whose devices was dropped for that reason.

A link `ip` reports whose type maps onto no netgraph interface type is named in
the report with the type it actually was, rather than being turned into an
`ethernet` port on a guess.

---

## Re-running an import

An import is re-run repeatedly while the capture set grows, so the command is
built for it. Re-running on the same captures produces the same bytes: names are
sanitised deterministically, cables are named from the sorted endpoint pair, and
devices and interfaces are written in a fixed order, so a diff after a new
capture is about the network rather than about the emitter.

What it will not do is merge into documents you have edited. Without `--force`,
every file already in the output tree is a clash, and all of them are named at
once rather than one per run; with `--force` those files are replaced wholesale,
hand edits included. So once you have started editing, import fresh captures
into an empty directory and merge the diff yourself. `--dry-run` prints the tree
to stdout and writes nothing, which is the cheap way to see what a new capture
set would produce.

`--schema` (the default) points each document at `schema/netgraph.schema.json`
with a modeline, writing the schema when the tree does not already hold one;
`--no-schema` leaves the editor unwired. Two devices whose names differ only in
case get two files, the second suffixed, because they are two elements to
netgraph and one file name on macOS and Windows.

One capture from one host is kilobytes, so an input over 32 MiB is refused: at
that size it is a tarball or a log, not a command's output. A directory named as
an input is refused too, with the glob to use instead.

---

## The findings afterwards are expected

An imported inventory is *partial* by construction: LLDP shows only the ports
that have a neighbour, `ip` shows one host, and a device nobody captured exists
only because a neighbour named it. `import` runs the validator over what it
wrote and names the rules that follow from that as expected rather than wrong:

| Rule | Why an import trips it |
|---|---|
| [`I002`](validation-rules.md#i002--enabled-interface-terminates-no-cable) | A port whose neighbour was never captured terminates no cable. |
| [`W101`](validation-rules.md#w101--interface-neither-routes-nor-switches) | An interface LLDP named has no address, because LLDP does not report one. |
| [`W103`](validation-rules.md#w103--orphan-device) | A device only a neighbour named has no cable of its own yet. |
| [`W105`](validation-rules.md#w105--subnet-with-a-single-member) | One captured host is the only element addressed in its subnet. |
| [`W109`](validation-rules.md#w109--device-that-cannot-be-cabled) | A device stub has no port a cable could land on. |
| [`W113`](validation-rules.md#w113--undeclared-vlan-referenced) | A VLAN observed on a port of another device is not declared there. |
| [`W121`](validation-rules.md#w121--disconnected-topology) | Two capture islands are not yet joined by a captured link. |

They are the gaps to fill, by capturing the missing hosts and re-running, or by
editing. [Validation](validation.md) explains each rule and how to suppress one
you have decided to live with.

Anything reported as an **error** is not expected, and `import` exits 1 when the
tree it wrote does not validate. The files are still written, so you can see
what went wrong — but check them before building on them.

---

## Limits

* **It never talks to a device.** No socket, no credential, no command run
  anywhere. Collection is yours.
* **It reads three dialects and no vendor configuration.** `show running-config`,
  NX-API, Junos XML and NetJSON are not parsed;
  [CSV](#why-csv-and-not-netjson) is the escape hatch, and one `awk` or `jq`
  usually fills it.
* **It writes devices and cables only.** Racks, patch panels, adapters, tunnels
  and templates are not produced, because no capture format reports them. Add
  them by hand afterwards; nothing about a generated tree is special.
* **It cannot know a cable's medium, a VLAN's name or a device's role.** Those
  are the first things to correct.
* **It is a starting point, not a sync.** There is no reconciliation against an
  existing tree — see [re-running an import](#re-running-an-import). What there
  *is*, once the tree exists, is [`netgraph drift`](commands/drift.md): the same
  captures read as a check on the inventory rather than as a replacement for it.

---

## See also

* [`netgraph import`](commands/import.md) — the flags, the exit codes and a
  worked example.
* [`netgraph drift`](commands/drift.md) — the same captures, compared against a
  tree that already exists instead of writing a new one.
* [`netgraph init`](commands/init.md) — the other way to get a first tree, for a
  network that does not exist yet.
* [Inventory layout](inventory-layout.md) — what the loader makes of the tree
  `import` writes.
* [Validation](validation.md) — the findings an imported tree is expected to
  produce, and what to do about them.
