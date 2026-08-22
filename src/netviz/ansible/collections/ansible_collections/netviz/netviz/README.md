# `netviz.netviz`

Ansible reads the network inventory that draws the diagram.

This collection ships inside the [netviz](https://github.com/blechschmidt/netviz)
Python package. It is not published separately, and it does not need to be:
`netviz ansible path` prints a collections path pointing straight into the
installed package, so the plugins are always the ones belonging to the netviz
beside them.

<!-- norun: installs netviz and sets a variable in the reader's shell -->
```bash
pip install netviz
export ANSIBLE_COLLECTIONS_PATH="$(netviz ansible path)"
ansible-doc -t lookup netviz.netviz.query
```

`netviz ansible install` copies the collection into `~/.ansible/collections`
instead, for a control node that keeps its own collections path.

## What is in it

| Plugin | Name | What it does |
|---|---|---|
| inventory | `netviz.netviz.netviz` | A netviz tree as hosts and groups, with variables that are queries |
| lookup | `netviz.netviz.query` | Answers a query — in a task, a variable, or a template |
| filter | `netviz.netviz.one` | Exactly one row, or an error saying how many there were |
| filter | `netviz.netviz.host` | `10.20.0.5/24` → `10.20.0.5` |
| filter | `netviz.netviz.network` | `10.20.0.5/24` → `10.20.0.0/24` |
| filter | `netviz.netviz.netmask` | `10.20.0.5/24` → `255.255.255.0` |
| filter | `netviz.netviz.prefix_length` | `10.20.0.5/24` → `24` |

And one playbook, `netviz.netviz.systemd_network`, which renders a
systemd-networkd unit per addressed interface from the inventory.

## The shortest useful thing

`inventory/netviz.yml`:

```yaml
plugin: netviz.netviz.netviz
root: ../net
```

A template:

```jinja
[Network]
{% for address in query('netviz.netviz.query',
                        'select (device filter .fqn = $fqn).addresses.address') %}
Address={{ address }}
{% endfor %}
```

`$fqn` is the element this host came from. It is bound for you, and it is a
*parameter* — the value is never read as query text, so a device name with an
apostrophe in it cannot change what the query asks.

The full guide is [docs/ansible.md](https://github.com/blechschmidt/netviz/blob/main/docs/ansible.md);
the query language is [docs/nql.md](https://github.com/blechschmidt/netviz/blob/main/docs/nql.md).

## Requirements

* ansible-core 2.15 or newer.
* `netviz` importable by the **control node's** Python — these plugins run
  there, not on the target.
