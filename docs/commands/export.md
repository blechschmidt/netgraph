# `netgraph export`

`netgraph export` turns the inventory into files other tools consume: fourteen
deterministic, text-diffable emitters driven by the same resolved inventory and
the same graph a diagram is drawn from — so the files that draw the picture also
write the hosts file, the zone, the Ansible inventory, the monitoring targets,
the cabling pull-list, the static-route script and the power load schedule.

Eight of the fourteen describe the network. The other six *are* the network: they
generate the configuration a device would actually run — netplan, systemd-networkd,
ifupdown, FRR, WireGuard, and netgraph's own vendor-neutral grammar for everything
those five have nothing to say about. This page is the reference for the command;
[`docs/export.md`](../export.md) is the full treatment of every column, every
option and exactly what each format drops.

---

## Contents

- [Synopsis](#synopsis)
- [The fourteen formats](#the-fourteen-formats)
- [Validation runs first](#validation-runs-first)
- [Where the artefact goes, and the manifest](#where-the-artefact-goes-and-the-manifest)
- [A configuration dialect writes a tree](#a-configuration-dialect-writes-a-tree)
- [Scoping an export: the filters `render` takes](#scoping-an-export-the-filters-render-takes)
- [Options, by the format they belong to](#options-by-the-format-they-belong-to)
- [Exit codes](#exit-codes)

---

## Synopsis

<!-- generated: synopsis export -->
```text
netgraph [GLOBAL OPTIONS] export [OPTIONS] FORMAT
```
<!-- /generated -->

## The fourteen formats

`FORMAT` is the one required argument. Every emitter is lossy in its own way, and
which way is the thing to know before you pick one.

**Eight describe the network:**

| `FORMAT` | Artefact | What it cannot hold |
|---|---|---|
| [`hosts`](../export.md#hosts) | An `/etc/hosts` fragment, one line per address | VLANs, cabling, hardware; loopback and link-local addresses are excluded on purpose |
| [`dns-zone`](../export.md#dns-zone) | RFC 1035 forward zone plus the reverse zones the prefixes imply | Everything but address records; only the qualified name is published |
| [`ansible-inventory`](../export.md#ansible-inventory) | Ansible's JSON inventory, grouped by namespace, kind, vendor and role | The topology — an inventory has no concept of a cable |
| [`prometheus-sd`](../export.md#prometheus-sd) | Prometheus `file_sd` targets with namespace/kind/vendor/site labels | Everything but one address and a few labels |
| [`cable-list`](../export.md#cable-list) | A CSV or Markdown pull-list, one row per physical run | Adapter attachments, tunnels and addressing |
| [`routes`](../export.md#routes) | An iproute2 script, one shell function per device, of the static routes it declares | Everything that is not a static route: BGP and OSPF configuration is vendor syntax and is not invented |
| [`power`](../export.md#power) | A CSV or JSON load schedule, one row per power feed: which outlet, on which strip, on which feed, powering which box in which rack unit | Everything that is not power; a feed has no medium, length or label, and the data run a PoE feed rides on is not described |
| [`drawio`](../export.md#drawio) | An mxGraph diagram draw.io opens, edits and hands back | The model: a name, a kind, a link and a coordinate per cell, but no interfaces, addresses, VLANs or routing |

**Six generate the configuration a device would run.** These write files at the
paths the device keeps them at, and a dialect that cannot express something the
inventory declares writes nothing at all rather than a device that is almost
right —
[Device configuration: the six dialects](../export.md#device-configuration-the-six-dialects):

| `FORMAT` | Artefact | What it refuses |
|---|---|---|
| [`netplan`](../export.md#device-configuration-the-six-dialects) | `etc/netplan/10-netgraph.yaml` for a `computer`, `server` or `router` | The 802.1Q configuration of a bridge port, and a VRF: netplan's `vrfs` section needs a numeric routing table and an inventory states a route distinguisher |
| [`networkd`](../export.md#device-configuration-the-six-dialects) | `etc/systemd/network/*.network` and `*.netdev`, one pair per link the host builds | A VRF, for the same missing table number — `[BridgeVLAN]` covers what netplan cannot |
| [`ifupdown`](../export.md#device-configuration-the-six-dialects) | `etc/network/interfaces`, the Debian original, with routes as `up`/`down` hooks | A bridge port's 802.1Q, a VRF, and an `ap` radio, which is hostapd's job rather than a station's `wpa-ssid` |
| [`frr`](../export.md#device-configuration-the-six-dialects) | `etc/frr/frr.conf`: VRFs, static routes, OSPF areas, BGP neighbours | Nothing — FRR's grammar is a superset of what the schema states about routing. It creates no interfaces, so those come from a host dialect |
| [`wireguard`](../export.md#device-configuration-the-six-dialects) | `etc/wireguard/<interface>.conf`, one file per tunnel, keys written as `REPLACE-ME` | A `cipher` or an `auth` that is not WireGuard's own, which is the part deciding whether the two ends can speak |
| [`interfaces`](../export.md#device-configuration-the-six-dialects) | `interfaces.conf` — netgraph's own vendor-neutral grammar, for **every** device | Nothing, ever. What it cannot do is be applied: no system reads it |

What they have in common — a generated-by header, stable ordering, and no clock
or hostname anywhere in the output, so re-exporting an unchanged inventory
produces a byte-identical file — is
[What every format guarantees](../export.md#what-every-format-guarantees).

<!-- run: -->
```console
$ netgraph -q -i examples/home-lab export hosts
# Generated by 'netgraph export hosts' -- do not edit.
# netgraph 0.1.0. Re-run the command to regenerate from the inventory.
# 7 element(s), 15 address(es).
# Loopback and link-local addresses and unnumbered interfaces are left out;
# the manifest on stderr says which elements produced nothing and why.
# Each element is published under its qualified name and, as an alias,
# its own name: 'sw-01.access.north.sites sw-01'.
#
192.0.2.1        rtr-home.routers rtr-home
192.168.10.1     rtr-home.routers rtr-home
192.168.10.2     sw-home.switches sw-home
192.168.10.3     ap-home.wireless ap-home
192.168.10.10    srv-nas.hosts srv-nas
192.168.10.20    pc-desk.hosts pc-desk
192.168.10.30    adp-usb-eth.hosts adp-usb-eth
192.168.10.40    phone.hosts phone
203.0.113.2      rtr-home.routers rtr-home
2001:db8::1      rtr-home.routers rtr-home
2001:db8:10::1   rtr-home.routers rtr-home
2001:db8:10::10  srv-nas.hosts srv-nas
2001:db8:10::20  pc-desk.hosts pc-desk
2001:db8:10::30  adp-usb-eth.hosts adp-usb-eth
2001:db8:10::40  phone.hosts phone
```

Six elements were considered and five emitted; `-q` is why you cannot see the
sixth being skipped here. How an inventory name becomes a hostname, a DNS label,
an Ansible group or a CSV cell, and what happens when two of them fold together,
is [Names, and how they are folded](../export.md#names-and-how-they-are-folded).

## Validation runs first

Validation runs before the emitter, exactly as it does for a render: an artefact
generated from an inventory with a dangling cable would misrepresent the network,
and a wrong `/etc/hosts` is worse than none. Errors therefore refuse the export.
`--strict` treats warnings as errors too, which is the setting for a pipeline that
publishes what it exports; `--force` proceeds anyway and says on stderr that the
artefact may not match the network.

## Where the artefact goes, and the manifest

The artefact goes to stdout, or to `-o FILE`. **stdout belongs to the artefact**,
so every diagnostic — and the manifest — goes to stderr instead.

A JSON manifest of what was skipped and why is written on every run, clean or
not: a consumer parsing it must not have to tell "nothing was skipped" from "the
tool forgot to say". It goes to stderr, or to `--manifest FILE`. The stderr copy
is commentary and is silenced by `-q`; a named file is written whatever the
verbosity, which is what a quiet pipeline that still wants the record should use.

<!-- norun: uses a shell redirect and a jq pipeline -->
```console
$ netgraph -i examples/home-lab export prometheus-sd -o targets.json 2> manifest.json
$ jq -r '.skipped[] | "\(.subject)\t\(.reason)"' manifest.json
hosts/pc-laptop	not-routable
```

Every `reason` code, and the `rewritten` half of the document that records a name
changed to fit a format's grammar, are in
[The skip manifest](../export.md#the-skip-manifest).

## A configuration dialect writes a tree

The eight description formats produce one artefact, so `-o FILE` is the whole
story. A configuration dialect produces a *tree* — netplan is one file per host,
systemd-networkd a `.netdev` and a `.network` per stacked link, wg-quick one file
per tunnel — and stdout is not a tree. `--out DIR` writes it: one directory per
device, named after the device's fully-qualified name, holding each file at the
path the device keeps it at.

<!-- norun: writes a directory tree into the reader's checkout -->
```console
$ netgraph -q -i examples/overlay export networkd --out build/config
```

Three rules apply to that directory, and they are narrower than the ones
[`netgraph import`](import.md) uses, because regenerating a configuration is the
normal operation rather than an exceptional one. A file netgraph generated is
**overwritten** without a flag, since its banner says so. A file netgraph did not
generate is **refused**, with every clash listed and `--force` named — that is
the `--out /etc` mistake, and the only one worth stopping. And nothing is ever
**deleted**: files left by an earlier, wider run are reported as a count and left
where they are, because deciding what to do about a device that is no longer
configured belongs to an operator.

Point `--out` at a directory outside the inventory: a generated
`10-netgraph.yaml` under the tree netgraph loads is a document netgraph tries to
load on the next run.

Without `--out`, a selection of more than one device is a usage error rather than
a stream nobody can split. One device is allowed, and is the case worth having:

<!-- norun: needs a live host to ssh into -->
```console
$ netgraph -q -i examples/home-lab export netplan --name pc-desk | ssh pc-desk 'cat >/etc/netplan/10-netgraph.yaml'
```

A dialect that cannot express a field a selected device declares refuses the
**whole run** with exit code `4`, naming every field it could not write. Nothing
is written — not for the offending device and not for the ones that were fine —
because a configuration missing one field is a device that is *almost* what the
inventory says, with nothing in the file to say which part is missing. The
distinction between that and an ordinary manifest skip is
[Emitted, skipped, refused](../export.md#emitted-skipped-refused).

## Scoping an export: the filters `render` takes

Every filter [`netgraph render`](render.md) takes, `export` takes, and they mean
the same thing: `--namespace`, `--vlan`, `--kind` and `--name` are repeatable,
and `--neighbors-of NAME` with `--depth N` keeps a neighbourhood. Repeats of one
option widen, different options narrow — so
`export hosts --namespace sites/north --kind server` writes the northern site's
servers and nothing else. The exact semantics, and why a cable is not a `--kind`,
are in [Filters](../rendering.md#filters-drawing-less-of-the-network), with worked
scoping examples in [Scoping an export](../export.md#scoping-an-export).

Reverse zones are regrouped from the prefixes [`netgraph ipam`](ipam.md) sizes
rather than derived a second time, so a zone, a utilisation figure and a layer-3
diagram cannot tell three different stories —
[the same prefixes](../export.md#the-reverse-zones-come-from-the-same-prefixes-ipam-sizes).

## Options, by the format they belong to

Most options apply to exactly **one** format, and typing one at another format is
a usage error rather than a flag that quietly did nothing:

<!-- run: rc=2 -->
```console
$ netgraph -i examples/home-lab export hosts --port 9100
Usage: netgraph export [OPTIONS] FORMAT
Try 'netgraph export --help' for help.

Error: --port applies to 'prometheus-sd', not to 'hosts'
```

**Every format**: `-o/--output`, `--manifest`, the filters above, `--strict` and
`--force`.

**The six configuration dialects**: `--out DIR` writes the tree instead of one
artefact, and `--force` additionally means "overwrite files netgraph did not
generate" there. `--out` is one of the format-specific options above, so giving
it to a description format is a usage error rather than a flag that quietly did
nothing:

<!-- run: rc=2 -->
```console
$ netgraph -i examples/home-lab export hosts --out build/config
Usage: netgraph export [OPTIONS] FORMAT
Try 'netgraph export --help' for help.

Error: --out applies to 'netplan', 'networkd', 'ifupdown', 'frr', 'wireguard' or 'interfaces', not to 'hosts'
```

**`dns-zone`**: `--origin NAME` is *required* — a zone file has no meaning without
the domain its records hang under, so omitting it is a usage error before
anything is loaded. `--ttl` sets the `$TTL` of every zone written (default 3600).
`--soa-mname` and `--soa-rname` default to `ns.<origin>` and
`hostmaster.<origin>`, and `--refresh`, `--retry`, `--expire` and `--minimum` are
the rest of the SOA. `--serial` is fixed rather than derived from the clock, so
re-exporting an unchanged inventory produces an unchanged file — bump it where
you publish. `--ns NAME` is repeatable and defaults to the `--soa-mname`.
`--zones all|forward|reverse` chooses which zones to write: `all` concatenates
them into one document for reading, while a nameserver wants `forward` and
`reverse` in separate files. See
[One document, several zones](../export.md#one-document-several-zones) and
[The serial does not move on its own](../export.md#the-serial-does-not-move-on-its-own).

**`prometheus-sd`**: `--port PORT` is appended to every target, with IPv6
bracketed automatically; without it a target is a bare address. `--label
KEY=VALUE` is repeatable and merged into every target, for the labels the
inventory cannot know — `env=prod`, say.

**`cable-list`**: `--table-format csv|markdown` chooses the layout. The rows and
columns are the same either way; `markdown` is for pasting into a change ticket,
and [The columns](../export.md#the-columns) documents all of them.

**`power`**: `--schedule-format csv|json` chooses the layout. The feed rows are the
same either way — `csv` is the sheet somebody prints and initials, and `json` adds
the per-PDU and per-PSE totals a capacity tool wants and a spreadsheet would
compute for itself. Neither is the lossy one. All twenty-two columns, and why a
dual-corded server is two rows rather than one, are
[`power`](../export.md#power) and
[The columns of the schedule](../export.md#the-columns-of-the-schedule).

**`hosts`** and **`ansible-inventory`** have no options of their own. What
`ansible-inventory` puts in each group, how `ansible_host` is chosen and which
host variables are set are [Groups](../export.md#groups),
[`ansible_host`](../export.md#ansible_host) and
[Host variables](../export.md#host-variables).

## Arguments

<!-- generated: arguments export -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `FORMAT` | yes | 1 | — |
<!-- /generated -->

## Options

<!-- generated: options export -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `-o`, `--output` | `FILE` | — | Write the artefact to this file instead of stdout. |
| `--out` | `DIRECTORY` | — | Write a configuration dialect as a tree: one directory per device, named after its fully-qualified name, holding the files at the paths the device keeps them at. Without it a single device's configuration goes to stdout. |
| `--manifest` | `FILE` | — | Write the JSON record of what was skipped to this file. It goes to stderr when no file is named. |
| `--namespace` | `NS` | — | Keep only elements in this namespace or below it. Repeatable. |
| `--vlan` | `VID` | — | Keep only elements participating in this VLAN. Repeatable. |
| `--kind` | `[switch\|router\|hub\|computer\|server\|adapter\|patchpanel\|pdu\|user\|group]` | — | Keep only elements of this kind. Repeatable. |
| `--name` | `GLOB` | — | Keep only elements whose name matches this glob. Repeatable. |
| `--neighbors-of` | `NAME` | — | Keep only the neighbourhood of this element. |
| `--depth` | `INTEGER, >= 0` | `1` | How many hops --neighbors-of reaches. |
| `--origin` | `NAME` | — | Zone origin, e.g. 'example.com'. Required by dns-zone. |
| `--ttl` | `SECONDS` | `3600` | $TTL of every zone written. |
| `--soa-mname` | `NAME` | ns.<origin> | Primary nameserver for the SOA record. |
| `--soa-rname` | `NAME` | hostmaster.<origin> | Responsible mailbox for the SOA record, in DNS form. |
| `--serial` | `N` | `1` | SOA serial. Fixed rather than derived from the clock, so that re-exporting an unchanged inventory produces an unchanged file; bump it where you publish. |
| `--refresh` | `SECONDS` | `86400` | SOA refresh: how often a secondary re-checks the zone. |
| `--retry` | `SECONDS` | `7200` | SOA retry: how long a secondary waits after a failed refresh. |
| `--expire` | `SECONDS` | `3600000` | SOA expire: when a secondary stops answering with data it could not refresh. |
| `--minimum` | `SECONDS` | `3600` | SOA minimum: the negative-caching TTL of RFC 2308. |
| `--ns` | `NAME` | the --soa-mname | NS record at the zone apex. Repeatable. |
| `--zones` | `[all\|forward\|reverse]` | `all` | Which zones to write. 'all' concatenates them into one document for reading; a nameserver wants 'forward' and 'reverse' in separate files. |
| `--port` | `PORT` | none, a bare address | Port appended to every prometheus-sd target. IPv6 is bracketed automatically. |
| `--label` | `KEY=VALUE` | — | Static label merged into every prometheus-sd target. Repeatable. |
| `--table-format` | `[csv\|markdown]` | `csv` | How cable-list is laid out. The rows and columns are the same either way. |
| `--schedule-format` | `[csv\|json]` | `csv` | How the power load schedule is laid out. json adds the per-PDU and per-PSE totals; the feed rows are the same either way. |
| `--view` | `[physical\|l1\|l2\|l3\|overlay\|routing\|rack\|power\|identity]` | `l1` | Which view the drawio diagram draws. Unlike the other formats this one is a picture, and the arrangement it opens with is the one stored for that view. |
| `--icons` | `THEME\|DIR` | `cisco` | Icon theme inlined into the drawio file as data URIs, so the file needs nothing beside it. Built in: cisco, none. 'none' draws coloured boxes. |
| `--compress`, `--no-compress` | — | `--no-compress` | Write the deflate+base64 encoding draw.io writes by default. Off here: a plain diagram is one that reviews and diffs, and draw.io opens both. |
| `--frames`, `--no-frames` | — | `--frames` | Draw a container frame per namespace, so dragging a site carries its devices. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Proceed even when validation failed. The result may not match the files. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The artefact was written. Elements skipped into the manifest are not a failure. |
| `1` | The inventory was rejected — validation found errors, or `--strict` promoted a warning — and `--force` was not given. Nothing is written. |
| `2` | Usage error — an unknown `FORMAT`, a format-specific option given to another format, or `dns-zone` without `--origin`. |
| `3` | The inventory could not be discovered or read at all, or the `--out` tree could not be written — the directory is a file, a write failed, or it holds files netgraph did not generate and `--force` was not given. |
| `4` | A configuration dialect cannot express something a selected device declares. Every refusal is listed with the field that produced it, and **nothing** was written. |
| `5` | The artefact or the manifest could not be written: `-o`, `--manifest` or stdout is not writable. |
| `130` | Interrupted. |
| `141` | The downstream end of a pipe closed first. |

## See also

* [`docs/export.md`](../export.md) — every column of the pull list, the SOA and
  zone options, the Ansible group and variable scheme, the manifest reason codes,
  and using the emitters as a library.
* [`netgraph render`](render.md) and
  [`docs/rendering.md`](../rendering.md#filters-drawing-less-of-the-network) — the
  filters `export` shares, in full.
* [`netgraph ipam`](ipam.md) and [`docs/ipam.md`](../ipam.md) — the prefixes the
  reverse zones are grouped from.
* [`docs/ci.md`](../ci.md) — regenerating artefacts in a pipeline, and asserting
  they have not drifted.
