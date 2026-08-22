# `netviz ansible`

Point Ansible at the inventory, and let a template ask the network questions.

netviz ships an Ansible collection, `netviz.netviz`, holding an inventory
plugin, a lookup plugin and five filters. This command group is the three things
that surround it: where the collection is, how to copy it, and what the
inventory plugin would produce.

[`docs/ansible.md`](../ansible.md) is the guide — what the plugins do, and how a
template is written against them. This page is the command.

## Synopsis

<!-- generated: synopsis ansible -->
```text
netviz [GLOBAL OPTIONS] ansible [OPTIONS] COMMAND [ARGS]...
```
<!-- /generated -->

---

## `netviz ansible path`

Prints the directory to put on `ANSIBLE_COLLECTIONS_PATH`: the one that *holds*
`ansible_collections/`, which is what that variable means. It points into the
installed package, so nothing is copied and the plugins are by construction the
ones belonging to this netviz.

<!-- norun: the path is inside the installed package and differs per machine -->
```bash
export ANSIBLE_COLLECTIONS_PATH="$(netviz ansible path)"
ansible-doc -t lookup netviz.netviz.query
```

The path is the whole of stdout, so the command substitutes into a shell
assignment; which collection it holds is said on stderr.

<!-- generated: synopsis ansible path -->
```text
netviz [GLOBAL OPTIONS] ansible path [OPTIONS]
```
<!-- /generated -->

---

## `netviz ansible install`

Copies the collection into a collections path — `~/.ansible/collections` by
default — for a control node that keeps its own and would rather not have an
environment variable in every shell.

<!-- norun: writes into the user's collections path -->
```bash
netviz ansible install                 # into ~/.ansible/collections
netviz ansible install ./collections   # or anywhere else
netviz ansible install --force         # replace an installation already there
```

Without `--force` an installation that is already there is **refused** rather
than merged: merging would leave a plugin from an older netviz beside one from
this one, and the older would keep answering.

A copy can go stale against the netviz beside it. The `galaxy.yml` written with
it records which version it came from, and is generated rather than shipped for
exactly that reason — netviz has one version, and a second copy of it in a file
nobody reads is a copy that will be wrong.

<!-- generated: synopsis ansible install -->
```text
netviz [GLOBAL OPTIONS] ansible install [OPTIONS] [PATH]
```
<!-- /generated -->

<!-- generated: arguments ansible install -->
| Argument | Required | Count | Default |
|---|---|---|---|
| `[PATH]` | no | 1 | — |
<!-- /generated -->

<!-- generated: options ansible install -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--force` | — | off | Replace an installation that is already there, rather than refusing. |
<!-- /generated -->

---

## `netviz ansible inventory`

Prints the document the inventory plugin builds: the same one
[`netviz export ansible-inventory`](export.md) writes, plus the variables and
groups that are *queries* — which is exactly what the plugin adds.

<!-- run: cwd=examples/home-lab -->
```console
$ netviz ansible inventory --select 'name = rtr-home' --var mgmt='select (device filter .fqn = $fqn).addresses.address'
{
  "_meta": {
    "hostvars": {
      "rtr-home.routers": {
        "ansible_host": "192.0.2.1",
        "netviz_element": "routers/rtr-home",
        "netviz_name": "rtr-home",
        "netviz_kind": "router",
        "netviz_namespace": "routers",
...
        "mgmt": [
          "192.0.2.1/32",
          "2001:db8::1/128",
          "203.0.113.2/30",
          "192.168.10.1/24",
          "2001:db8:10::1/64"
        ]
      }
    }
  },
...
1 host in 4 groups
```

Two uses. **Reading and diffing** what the plugin will hand Ansible, without a
control node — a query that answers `null` for every host is a query with a typo
in it, and this is where that is seen. And **as a dynamic inventory script**,
for the case where a plugin is one moving part too many:

<!-- norun: writes an executable and runs a playbook against it -->
```bash
cat > inventory/netviz.sh <<'EOF'
#!/bin/sh
exec netviz -i ../net ansible inventory "$@"
EOF
chmod +x inventory/netviz.sh
ansible-playbook -i inventory/netviz.sh site.yml
```

`--list` is accepted and ignored — this command always lists — so that the
wrapper above can pass Ansible's arguments straight through. The document
carries `netviz_root` as a variable of `all`, exactly as the plugin sets it, so
a template reaching for `lookup('netviz.netviz.query', …)` works in that mode
too.

`--var NAME=QUERY` is answered once per host, with that host bound to `$host`,
`$fqn`, `$name`, `$namespace` and `$kind`. `--group NAME=QUERY` is answered
once, and every host the rows name joins the group. Both are repeatable.

<!-- run: cwd=examples/home-lab -->
```console
$ netviz ansible inventory --select 'name = sw-home' --group unaddressed='select device.fqn filter not exists .addresses' -o /dev/null
1 host in 4 groups
```

<!-- generated: synopsis ansible inventory -->
```text
netviz [GLOBAL OPTIONS] ansible inventory [OPTIONS]
```
<!-- /generated -->

<!-- generated: options ansible inventory -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--var` | `NAME=QUERY` | — | A host variable that is a query, answered once per host. Repeatable. |
| `--group` | `NAME=QUERY` | — | A group whose members a query names, answered once. Repeatable. |
| `-o`, `--output` | `FILE` | — | Write to this file instead of stdout. |
| `--list` | — | off | Accepted and ignored: this command always lists. Here so the output can be piped from a two-line script Ansible calls as a dynamic inventory. |
| `--strict` | — | off | Treat warnings as errors. |
| `--force` | — | off | Build an inventory from a tree that has errors in it. |
| `--select` | `QUERY` | — | A selector narrowing which elements become hosts. |
<!-- /generated -->

---

## Exit status

| Status | Means |
|---|---|
| 0 | It worked. |
| 1 | The tree does not load or does not validate, or an installation is already there. |
| 2 | A query does not parse, or an option is wrong. |

---

## See also

* [`docs/ansible.md`](../ansible.md) — the guide: the plugins, and how a
  template is written against them.
* [`docs/nql.md`](../nql.md) — the query language, and its parameters.
* [`netviz export ansible-inventory`](export.md) — the same hosts and groups as
  a file, for a control node without netviz on it.
