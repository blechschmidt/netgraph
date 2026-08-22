# Copyright (c) netviz contributors
# MIT (see https://github.com/blechschmidt/netviz)
"""A netviz inventory tree, as Ansible's inventory."""

from __future__ import annotations

DOCUMENTATION = """
name: netviz
author: netviz contributors
short_description: Hosts and groups from a netviz network inventory
description:
  - Reads a netviz inventory tree - the YAML that draws the diagram - and turns it into hosts.
  - >-
    Every element with a management address becomes a host, named as
    C(netviz export ansible-inventory) names it, and joins four families of
    group: C(ns_*) for its namespace (nested, so group variables resolve down the
    folder tree), C(kind_*) for its element kind, C(vendor_*) and C(role_*).
  - >-
    Each host starts with the facts a template needs to generate configuration -
    C(netviz_interfaces), C(netviz_addresses), C(netviz_vlans), C(netviz_location)
    and the rest - and with C(netviz_root), which is what lets
    C(lookup('netviz.netviz.query', ...)) work in a template with no arguments.
  - >-
    O(query_vars) and O(query_groups) are queries. A variable declared there is
    answered once per host with that host bound to C($host), C($fqn), C($name),
    C($namespace) and C($kind); a group is answered once, and every host it
    names joins it. They are netviz's own, and sit beside O(compose),
    O(groups) and O(keyed_groups), which are Ansible's and are Jinja over the
    variables each host already has.
extends_documentation_fragment:
  - constructed
options:
  plugin:
    description: The name of this plugin. Required, and must be the fully-qualified one.
    type: str
    required: true
    choices: [netviz.netviz.netviz]
  root:
    description:
      - The inventory tree, or a single YAML file.
      - >-
        Relative paths are relative to this configuration file, not to the
        working directory, because the file and the tree are checked in together
        and the working directory is not. This is why the option is a string
        rather than a C(path): Ansible resolves a C(path) against the working
        directory before a plugin sees it.
    type: str
    default: .
  select:
    description:
      - A netviz selector narrowing which elements become hosts, e.g. V(kind = server).
      - The same language C(netviz render --select) speaks.
    type: str
    required: false
  query_vars:
    description:
      - Host variables that are netviz queries, by variable name.
      - >-
        Answered once per host. One row is the value; anything else is the list
        of rows, so a query that can answer twice always reads as a list.
    type: dict
    required: false
  query_groups:
    description:
      - Groups whose membership is a netviz query, by group name.
      - >-
        Answered once. Every row that names an element - a fully-qualified name,
        or an object with one in it - puts that element's host in the group.
    type: dict
    required: false
  layer:
    description: Which view a O(select) selector is answered against.
    type: str
    default: l2
  require_valid:
    description:
      - Refuse to build an inventory from a tree that does not load or does not validate.
      - >-
        Leave this on. Hosts derived from documents that did not parse are the
        hosts that happen to have loaded, and nothing downstream would say so.
    type: bool
    default: true
  warnings_as_errors:
    description:
      - Treat validation warnings as errors, as C(netviz validate --strict) does.
      - >-
        Not to be confused with O(strict), which comes from the C(constructed)
        fragment and governs O(compose) and O(keyed_groups).
    type: bool
    default: false
notes:
  - Runs on the control node and needs the C(netviz) Python package installed there.
  - >-
    The configuration file's name must end in C(netviz.yml), C(netviz.yaml) or
    C(netviz.json) - so C(inventory/netviz.yml) and C(inventory/prod.netviz.yml)
    are both claimed by this plugin, and an inventory directory holding several
    sources is unambiguous.
seealso:
  - name: netviz export ansible-inventory
    description: The same hosts and groups as a file, for a control node without netviz on it.
    link: https://github.com/blechschmidt/netviz/blob/main/docs/ansible.md
"""

EXAMPLES = """
# inventory/netviz.yml - the least of it
plugin: netviz.netviz.netviz
root: ../net

# inventory/netviz.yml - the servers of one site, with facts that are answers
plugin: netviz.netviz.netviz
root: ../net
select: kind = server and namespace under 'sites/north'
query_vars:
  netviz_mgmt: >-
    select (device filter .fqn = $fqn).interfaces
    { name, addresses := .addresses.address } filter .name = 'mgmt0'
  netviz_uplink_vlans: select distinct (device filter .fqn = $fqn).interfaces.vlans.id
query_groups:
  needs_addressing: select device.fqn filter not exists .addresses
  wireless: kind = switch and interface[type = wifi]
compose:
  ansible_user: "'netops'"
keyed_groups:
  - key: netviz_location.site
    prefix: site
"""

from ansible.errors import AnsibleParserError  # noqa: E402
from ansible.plugins.inventory import BaseInventoryPlugin, Constructable  # noqa: E402
from ansible_collections.netviz.netviz.plugins.plugin_utils.netviz_api import (  # noqa: E402
    api,
    errors,
    resolve_root,
)

#: What a configuration file for this plugin may be called. An inventory
#: directory holds several sources and every plugin is offered each of them, so
#: this is how one is claimed without claiming somebody else's.
ENDINGS = ("netviz.yml", "netviz.yaml", "netviz.json")


class InventoryModule(BaseInventoryPlugin, Constructable):
    """A netviz tree, as hosts and groups."""

    NAME = "netviz.netviz.netviz"

    def verify_file(self, path):
        """Is this a configuration file for this plugin?"""
        return super().verify_file(path) and path.endswith(ENDINGS)

    def parse(self, inventory, loader, path, cache=True):
        """Read ``path``, build the document, and feed it to Ansible."""
        super().parse(inventory, loader, path, cache)
        self._read_config_data(path)
        netviz = api()
        root = resolve_root(self.get_option("root"), config_path=path)
        if not root:
            raise AnsibleParserError(f"{path} names no netviz inventory: set 'root'")

        try:
            document = netviz.inventory_document(
                root,
                netviz.InventoryOptions(
                    select=self.get_option("select") or None,
                    host_vars=self.get_option("query_vars") or {},
                    groups=self.get_option("query_groups") or {},
                    layer=netviz.layer_named(self.get_option("layer")),
                ),
                strict=bool(self.get_option("warnings_as_errors")),
                force=not self.get_option("require_valid"),
            )
        except errors() as error:
            # The message whole: a query error underlines the character it is
            # about, and a parser error that kept only its first line would hide
            # the half that says where.
            raise AnsibleParserError(str(error)) from error

        self._populate(document, root)

    def _populate(self, document, root):
        """Turn the JSON document into groups, hosts and variables."""
        hostvars = document.get("_meta", {}).get("hostvars", {})
        for name, entry in document.items():
            if name == "_meta":
                continue
            if name != "all":
                self.inventory.add_group(name)
            for child in entry.get("children", ()):
                self.inventory.add_group(child)
                self.inventory.add_child(name, child)
            for host in entry.get("hosts", ()):
                self.inventory.add_host(host, group=name)
            for key, value in entry.get("vars", {}).items():
                self.inventory.set_variable(name, key, value)

        # The tree this was read from, so a lookup in a template needs no
        # arguments. Set here rather than in the document itself: it is a fact
        # about where the inventory came from, not about the network.
        self.inventory.set_variable("all", "netviz_root", root)

        constructed = self.get_option("strict")
        for host, variables in hostvars.items():
            self.inventory.add_host(host)
            for key, value in variables.items():
                self.inventory.set_variable(host, key, value)
            self._set_composite_vars(self.get_option("compose"), variables, host, constructed)
            self._add_host_to_composed_groups(
                self.get_option("groups"), variables, host, constructed
            )
            self._add_host_to_keyed_groups(
                self.get_option("keyed_groups"), variables, host, constructed
            )
