# Ansible

The inventory that draws the diagram is the inventory Ansible runs against, and
the addresses in a generated configuration file are the ones the YAML declares.

Two things are on offer, and they answer different questions.

| Question | What answers it |
|---|---|
| *Who is out there?* | the **inventory plugin** — hosts, groups and per-host facts |
| *What should this host's configuration say?* | the **lookup plugin** — a query, in a template |

The second is the interesting one. Ansible's usual source of a host's addresses
is the host: `ansible_facts` reports what is configured, which is precisely the
wrong direction when the point is to *say* what should be configured. A query
answers from the declaration instead — so the unit file, the diagram, the
validation rules and the review all come from one document.

```jinja
[Match]
Name={{ item.name }}

[Network]
{% for address in item.addresses %}
Address={{ address }}
{% endfor %}
```

---

## Install

The plugins run on the **control node** — the machine typing `ansible-playbook`
— and they need netviz importable there. Nothing is needed on the targets.

<!-- norun: installs netviz and sets a variable in the reader's shell -->
```bash
pip install netviz            # or: uv tool install netviz, pipx install netviz
export ANSIBLE_COLLECTIONS_PATH="$(netviz ansible path)"
```

`netviz ansible path` prints a collections path pointing straight into the
installed package, so nothing is copied and the plugins are always the ones
belonging to the netviz beside them. To put a copy where the control node keeps
its collections instead:

<!-- norun: writes into the reader's collections path -->
```bash
netviz ansible install        # into ~/.ansible/collections
```

Either way:

```bash
ansible-doc -t lookup netviz.netviz.query
```

[`docs/commands/ansible.md`](commands/ansible.md) is the reference for both.

---

## The inventory

One file, and the tree it points at:

```yaml
# inventory/netviz.yml
plugin: netviz.netviz.netviz
root: ../net
```

`root` is relative to **this file**, not to the working directory: the two are
checked in together, and the directory `ansible-playbook` happens to be run from
is not.

<!-- norun: needs a control node, and the collection on its path -->
```console
$ ansible-inventory -i inventory/netviz.yml --graph
@all:
  |--@ns_routers:
  |  |--rtr-home.routers
  |--@kind_switch:
  |  |--ap-home.wireless
  |  |--sw-home.switches
  …
```

Every element with a management address becomes a host, named as
[`netviz export ansible-inventory`](export.md#ansible-inventory) names it —
because it *is* that exporter: the plugin builds the same document and then adds
to it. Two implementations of "which hosts are there" would drift, and the day
they did, a checked-in inventory file and the plugin that replaces it would
disagree about who is a server.

Four families of group, each prefixed so two can never collide:

| Group | From |
|---|---|
| `ns_*` | the namespace, **nested** — `ns_sites_north` is a child of `ns_sites`, so `group_vars/ns_sites.yml` applies to a whole site |
| `kind_*` | the element kind: `kind_switch`, `kind_server` |
| `vendor_*` | `spec.vendor` |
| `role_*` | the `role` label, when the inventory uses one |

And every host starts with the facts a template needs — `netviz_interfaces`,
`netviz_addresses`, `netviz_vlans`, `netviz_location`, `netviz_labels`,
`netviz_element` — plus `netviz_root`, which is the tree it was read from. That
last one is what makes a lookup in a template need no arguments at all.

### Variables and groups that are queries

```yaml
plugin: netviz.netviz.netviz
root: ../net
select: kind = server and namespace under 'sites/north'
query_vars:
  uplink_vlans: select distinct (device filter .fqn = $fqn).interfaces.vlans.id
  mgmt: select (device filter .fqn = $fqn).interfaces { addresses := .addresses.address } filter .name = 'mgmt0'
query_groups:
  unaddressed: select device.fqn filter not exists .addresses
  wireless: kind = switch and interface[type = wifi]
```

`select` narrows which elements become hosts, in the
[selector language](query.md) every other command's `--select` speaks.

`query_vars` is answered **once per host**, with that host bound (see below).
One row is the value; anything else is the list of rows, so a query that can
answer twice always reads as a list and a template never has to sniff.

`query_groups` is answered **once**, and every row that names an element — a
fully-qualified name, or an object with one in it — puts that element's host in
the group. A group that ends up empty is not written, because a group naming no
host is one every Ansible command warns about; a group whose name is one of the
derived ones is refused rather than replaced, because a `kind_router` holding
something other than the routers is a trap rather than a shortcut.

Ansible's own `compose`, `groups` and `keyed_groups` work as they do on any
inventory plugin. They are Jinja over the variables a host already has;
`query_vars` and `query_groups` are queries over the network. Both are useful,
and the names are different so it is always clear which is meant.

---

## Queries in a template

```jinja
{% for address in query('netviz.netviz.query',
                        'select (device filter .fqn = $fqn).addresses.address') %}
Address={{ address }}
{% endfor %}
```

`$fqn` is the fully-qualified name of the element this host came from. It is
bound for you, along with the rest of the host's identity:

| Parameter | Is |
|---|---|
| `$host` | the name Ansible knows it by — `sw-01.sites.north` |
| `$fqn` | the element's fully-qualified name — `sites/north/sw-01` |
| `$name` | the short name — `sw-01` |
| `$namespace` | `sites/north` |
| `$kind` | `switch`, `server`, … |

Use `query()` rather than `lookup()` when the answer is structured: `lookup()`
joins its results into a string, and an array of objects deserves better.

To ask about something other than the current host, pass parameters:

```jinja
{{ query('netviz.netviz.query',
         'select (device filter .name = $who).addresses.address',
         params={'who': 'rtr-edge'}) | netviz.netviz.one }}
```

### Why a parameter, and not a string

Because this is wrong:

```jinja
{# don't #}
{{ query('netviz.netviz.query',
         "select device filter .name = '" ~ inventory_hostname ~ "'") }}
```

It means what it says only while nobody puts an apostrophe in a device name, and
the day somebody does, the query either fails to parse or — worse — parses into
a different question. A `$name` is a *token*: the value never reaches the
parser, so nothing in it can change what is being asked. It is also *typed*, by
the value that was passed, which is why `params={'id': 10}` compares against a
VLAN id and refuses to compare against a name.

The same holes work at the terminal, which is where a template's query is
developed:

<!-- run: cwd=examples/home-lab -->
```console
$ netviz query 'select (device filter .name = $host).addresses.address' --param host=rtr-home
ADDRESS
-----------------
192.0.2.1/32
2001:db8::1/128
203.0.113.2/30
192.168.10.1/24
2001:db8:10::1/64
```

[`docs/nql.md`](nql.md#parameters) is the language; the parameters section is
the whole of this feature.

### Selectors answer too

A query that does not begin with `select` or `with` is a
[selector](query.md), and answers with the fully-qualified name of every element
it picked — which is what `when:` and `loop:` want:

```yaml
- name: Warn about anything unaddressed
  ansible.builtin.debug:
    msg: "{{ item }} has no address"
  loop: "{{ query('netviz.netviz.query', 'kind = server and not has address') }}"
```

---

## The filters

Five, for the last inch between an answer and a file. Anything larger belongs in
`ansible.utils`, which does it properly.

| Filter | `10.20.0.5/24` becomes |
|---|---|
| `netviz.netviz.host` | `10.20.0.5` |
| `netviz.netviz.network` | `10.20.0.0/24` |
| `netviz.netviz.netmask` | `255.255.255.0` |
| `netviz.netviz.prefix_length` | `24` |

And `netviz.netviz.one`, which is the one that matters: exactly one row, or an
error saying how many there were. A template that silently took `| first` of
three answers is a template that will one day configure the wrong address.

---

## A worked example: systemd-networkd

The collection ships the playbook. It renders one `.network` unit per addressed
interface, from the inventory, on the control node:

<!-- norun: runs a playbook against the reader's own inventory -->
```bash
export ANSIBLE_COLLECTIONS_PATH="$(netviz ansible path)"
ansible-playbook netviz.netviz.systemd_network \
    -i inventory/netviz.yml -e netviz_units="$PWD/build/units"
```

```ini
# build/units/rtr-home.routers/10-wan0.network
# Generated by netviz.netviz.systemd_network from routers/rtr-home.
# The inventory is the source of truth: edit the YAML, not this file.

[Match]
Name=wan0

[Network]
Address=203.0.113.2/30

[Link]
MTUBytes=1500
RequiredForOnline=yes
```

Nothing is written to a machine: the play produces files you can read, diff and
then ship with your own `copy` task and a `systemctl restart systemd-networkd`
handler. Deciding when a network interface is reconfigured is not a decision a
generator should be making.

The query it runs is a variable, so a play that wants different interfaces
overrides it rather than forking the playbook:

```yaml
vars:
  netviz_interface_query: >-
    select (device filter .fqn = $fqn).interfaces {
      name, mtu, addresses := .addresses.address, vlans := .vlans.id
    } filter exists .addresses and .type != 'loopback' order by .name
```

[`netviz export`](export.md) is the other way to generate configuration —
`netplan`, `networkd`, `ifupdown`, `frr`, `wireguard`, `interfaces` — and it
needs no Ansible at all. Reach for a template when the file is *yours*: your
comments, your options, your conventions, with the network's facts filled in.
Reach for `export` when netviz already knows the dialect.

---

## Without the collection

Two ways, both of which keep working when a control node cannot have netviz
installed:

<!-- norun: one writes a file, and the other's output is a page long -->
```bash
netviz export ansible-inventory -o inventory.json   # a file, committed or generated in CI
netviz ansible inventory --var mgmt='select …'      # the plugin's document, on stdout
```

The first is what to commit or to generate in a CI step. The second is the
plugin's exact output, and wrapping it in a two-line executable makes it a
dynamic inventory script — see
[`docs/commands/ansible.md`](commands/ansible.md#netviz-ansible-inventory).

Neither gives a template the lookup, so a query in that world becomes a `netviz
query --param … -F json` in a `command` task. That works, and it costs a process
per question; the collection exists so it does not have to.

---

## Notes and limits

* **A play sees one snapshot.** The tree is read once per process and held still
  for the rest of it. That is what makes a play of forty templates one load —
  and it is also what keeps a template rendered at the top of a run and one
  rendered at the bottom from disagreeing about the network.
* **A broken tree is refused.** An answer computed from documents that did not
  parse reports what is left rather than what is declared, and a playbook is the
  wrong place to find that out. `require_valid: false` overrides it;
  `warnings_as_errors: true` tightens it to what `netviz validate --strict`
  grades.
* **An element with no management address is not a host.** It cannot be reached,
  so it is skipped — `netviz export ansible-inventory` records why in its
  manifest.
* **ansible-core 2.15 or newer**, on the control node.

---

## See also

* [`docs/commands/ansible.md`](commands/ansible.md) — the command reference.
* [`docs/nql.md`](nql.md) — the relational query language and its parameters.
* [`docs/query.md`](query.md) — the selector.
* [`docs/export.md`](export.md) — artefacts, including the Ansible inventory as
  a file and six device-configuration dialects.
