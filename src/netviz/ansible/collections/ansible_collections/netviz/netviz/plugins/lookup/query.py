# Copyright (c) netviz contributors
# MIT (see https://github.com/blechschmidt/netviz)
"""Answer a netviz query from a task, a variable or a template."""

from __future__ import annotations

DOCUMENTATION = """
name: query
author: netviz contributors
short_description: Ask a netviz inventory a question, from a template
description:
  - Runs a query against a netviz inventory tree and returns its rows.
  - >-
    A query beginning with C(select) or C(with) is relational: it walks the
    schema, follows links instead of spelling out joins, and returns whatever
    shape it projects - a value, an object, or a list of objects. Anything else
    is a selector, a predicate over elements, and the rows are then the
    fully-qualified name of every element it picked.
  - >-
    A query may leave a hole for a value, written C($name), and take it from
    O(params). The value is never read as query text, so a device name with a
    quotation mark in it cannot change what the query asks. This is the
    difference between templating a query and concatenating one.
  - >-
    The host being configured is bound for you. C($host) is the name Ansible
    knows it by, C($fqn) its element's fully-qualified name, and C($name),
    C($namespace) and C($kind) the rest of its identity - so the usual template
    passes no arguments at all.
options:
  _terms:
    description: One or more queries. The rows of each are concatenated.
    type: list
    elements: str
    required: true
  root:
    description:
      - The inventory tree, or a single YAML file.
      - >-
        Defaults to the C(netviz_root) variable, which the
        C(netviz.netviz.netviz) inventory plugin sets to the tree it read - so
        a playbook using that inventory needs nothing here.
      - Relative to the working directory. A string rather than a C(path) so
        that C(~) and relative paths are resolved once, here, however the value
        arrived.
    type: str
    required: false
    vars:
      - name: netviz_root
    env:
      - name: NETVIZ_ROOT
    ini:
      - section: netviz
        key: root
  params:
    description:
      - Values for the C($name) holes in the query, by name.
      - Wins over the parameters O(host) binds, so a template can ask about another host.
    type: dict
    required: false
  host:
    description:
      - The host whose identity is bound to C($host), C($fqn), C($name), C($namespace) and C($kind).
      - Defaults to C(inventory_hostname). Set it to V(null) to bind nothing.
    type: str
    required: false
  layer:
    description:
      - Which view a I(selector) query is answered against. A relational query reads the whole
        inventory and ignores this.
    type: str
    default: l2
  require_valid:
    description:
      - Refuse to answer from a tree that does not load or does not validate.
      - >-
        Leave this on. An answer computed from documents that did not parse
        reports what is left rather than what is declared, and a playbook is the
        wrong place to find that out.
    type: bool
    default: true
  warnings_as_errors:
    description: Treat validation warnings as errors, as C(netviz validate --strict) does.
    type: bool
    default: false
notes:
  - Runs on the control node and needs the C(netviz) Python package installed there.
  - >-
    The tree is read once per process and held still for the rest of it, so a
    play that renders forty templates loads one inventory. A file edited
    mid-play is not picked up, which is what keeps two templates in one run from
    disagreeing about the network.
seealso:
  - name: netviz query
    description: The same two languages at the terminal, where a query is developed.
    link: https://github.com/blechschmidt/netviz/blob/main/docs/nql.md
"""

EXAMPLES = """
- name: Every address the inventory gives this host
  ansible.builtin.debug:
    var: lookup('netviz.netviz.query',
                'select (device filter .fqn = $fqn).addresses.address', wantlist=True)

- name: Render a systemd unit per addressed interface
  ansible.builtin.template:
    src: 10-netviz.network.j2
    dest: "/etc/systemd/network/10-{{ item.name }}.network"
  loop: >-
    {{ query('netviz.netviz.query',
             'select (device filter .fqn = $fqn).interfaces
              { name, addresses := .addresses.address } filter exists .addresses') }}
  loop_control:
    label: "{{ item.name }}"

- name: Ask about a host other than this one
  ansible.builtin.set_fact:
    gateway: >-
      {{ query('netviz.netviz.query',
               'select (device filter .name = $who).addresses.address',
               params={'who': 'rtr-edge'}) | first }}

- name: Which elements are servers with no address (a selector)
  ansible.builtin.debug:
    msg: "{{ query('netviz.netviz.query', 'kind = server and not has address') }}"

- name: A structured answer, used as one
  ansible.builtin.copy:
    content: "{{ query('netviz.netviz.query', 'select vlan { id, name }') | to_nice_yaml }}"
    dest: vlans.yml
"""

RETURN = """
_raw:
  description:
    - The rows the query produced, JSON-ready.
    - Objects and lists come back whole, so use C(query()) rather than C(lookup()) for them.
  type: list
  elements: raw
"""

from ansible.errors import AnsibleError  # noqa: E402
from ansible.plugins.lookup import LookupBase  # noqa: E402
from ansible_collections.netviz.netviz.plugins.plugin_utils.netviz_api import (  # noqa: E402
    api,
    errors,
    resolve_root,
    translate,
)


class LookupModule(LookupBase):
    """netviz queries, as a lookup."""

    def run(self, terms, variables=None, **kwargs):
        """Answer every query in ``terms`` and return the rows of all of them."""
        self.set_options(var_options=variables, direct=kwargs)
        netviz = api()
        root = resolve_root(self.get_option("root"), variables=variables)
        if not root:
            raise AnsibleError(
                "no netviz inventory was named. Set the 'root' argument, the "
                "'netviz_root' variable, or the NETVIZ_ROOT environment variable; "
                "the netviz.netviz.netviz inventory plugin sets the variable for you."
            )
        host = self._host(variables)
        rows = []
        for term in terms:
            try:
                rows.extend(
                    netviz.answer(
                        root,
                        str(term),
                        params=self.get_option("params") or None,
                        host=host,
                        strict=bool(self.get_option("warnings_as_errors")),
                        force=not self.get_option("require_valid"),
                        layer=self.get_option("layer"),
                    )
                )
            except errors() as error:
                raise translate(error) from error
        return rows

    def _host(self, variables):
        """The host whose identity the query may name, or ``None``."""
        chosen = self.get_option("host")
        if chosen is not None:
            return str(chosen) or None
        return (variables or {}).get("inventory_hostname")
