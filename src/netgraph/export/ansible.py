"""Ansible's JSON inventory: the same file that draws the diagram, as hosts.

``ansible-inventory --list`` and every dynamic-inventory script speak one shape:
a ``_meta.hostvars`` table holding the variables of each host, plus one key per
group naming its ``hosts`` and its ``children``. That is what this emits, so the
output drops straight in::

    netgraph export ansible-inventory -o inventory.json
    ansible-inventory -i inventory.json --graph
    ansible-playbook -i inventory.json site.yml

Groups
------

Four axes, each prefixed so two of them can never collide on a name:

``ns_*``
    The namespace, **nested**: an element in ``sites/north/access`` joins
    ``ns_sites_north_access``, which is a child of ``ns_sites_north``, which is
    a child of ``ns_sites``. Ansible resolves group variables down that chain,
    so ``group_vars/ns_sites_north.yml`` applies to a whole site exactly as a
    reader of the folder tree would expect.
``kind_*``
    ``kind_switch``, ``kind_server`` — the element kind of §3.
``vendor_*``
    ``spec.vendor``, when the inventory declares one.
``role_*``
    The ``role`` label, when the inventory carries one. Nothing in the schema
    mandates it; this is the conventional selector label, and an inventory that
    does not use it simply has no ``role_*`` groups.

``ansible_host``
    The management address, chosen by
    :func:`~netgraph.export.context.management_address` — a management
    interface first, then a loopback, then declaration order, IPv4 before IPv6
    at every tier. An element with no routable address is not a host anybody
    can reach, so it is skipped and recorded rather than emitted with a null.

What it drops
-------------

The topology. An Ansible inventory has no concept of a cable, so which port is
plugged into which is not representable and does not appear; ``cable-list`` and
``render -f json`` are where that lives. Per-host variables carry the interface
and VLAN detail a template needs to *generate* config, not the adjacency needed
to reason about the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from netgraph.export.context import (
    ExportContext,
    NameRegistry,
    element_addresses,
    elements_of,
    location_of,
    management_address,
    record_addressless,
)
from netgraph.export.header import GENERATOR
from netgraph.export.manifest import Reason, Recorder
from netgraph.export.names import ansible_identifier
from netgraph.models import Device
from netgraph.render.graph import Layer, Node, PortView

__all__ = ["emit"]

#: The label an inventory conventionally records an element's function under.
#: Not part of the schema — §3.1 leaves labels to the user — so the ``role_*``
#: groups exist exactly when the inventory chose to use it.
ROLE_LABEL = "role"

#: Prefix per group axis, so ``kind_core`` and a namespace called ``core``
#: cannot become the same group.
_NAMESPACE_PREFIX = "ns_"
_KIND_PREFIX = "kind_"
_VENDOR_PREFIX = "vendor_"
_ROLE_PREFIX = "role_"


def emit(context: ExportContext) -> str:
    """Render the Ansible inventory as a JSON document, newline-terminated."""
    recorder = context.recorder
    hostvars: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, list[str]]] = {}
    names = _GroupNames(recorder)
    # The namespace *paths* the selection covers. The hierarchy is built from
    # these rather than from the folded group names: a namespace segment that
    # folds to something containing '_' would otherwise be split back apart at
    # the wrong places (see :func:`_link_namespaces`).
    namespaces: set[str] = set()
    registry = NameRegistry(recorder)

    nodes = elements_of(context.at(Layer.L2))
    recorder.considered = len(nodes)
    for node in nodes:
        address = management_address(node)
        if address is None:
            record_addressless(node, recorder)
            continue
        name = registry.register(node)
        if name is None:
            continue
        recorder.emitted += 1
        namespaces.add(node.namespace)
        hostvars[name.fqdn] = _host_vars(node, address.ip)
        for source, group in _groups_of(node):
            groups.setdefault(group, {"hosts": [], "children": []})["hosts"].append(name.fqdn)
            names.record(source=source, group=group)

    _link_namespaces(groups, namespaces)
    document: dict[str, Any] = {
        "_meta": {"hostvars": {name: hostvars[name] for name in sorted(hostvars)}},
        "all": {
            "children": sorted(_root_groups(groups)),
            # The one place this schema has for provenance: a variable every
            # host inherits and no playbook is obliged to look at.
            "vars": {"netgraph_generated_by": GENERATOR},
        },
    }
    for group in sorted(groups):
        entry: dict[str, Any] = {"hosts": sorted(set(groups[group]["hosts"]))}
        children = sorted(set(groups[group]["children"]))
        if children:
            entry["children"] = children
        document[group] = entry
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


# --------------------------------------------------------------------------- #
# Groups
# --------------------------------------------------------------------------- #


def _groups_of(node: Node) -> Iterator[tuple[str, str]]:
    """Every group an element belongs to directly, as ``(source, group)``.

    The source text is carried alongside so the caller can report the fold: a
    vendor of ``Ubiquiti Networks`` becomes ``vendor_ubiquiti_networks``, and
    an operator grepping the inventory for the string they wrote would not find
    it unless the manifest says so.

    The namespace contributes only its *deepest* group here; the ancestors are
    wired up as ``children`` by :func:`_link_namespaces`, which is what makes
    ``group_vars`` inherit down the tree instead of duplicating each host into
    every ancestor's ``hosts`` list.
    """
    if node.namespace:
        yield node.namespace, _namespace_group(node.namespace)
    yield node.kind, ansible_identifier(node.kind, prefix=_KIND_PREFIX)
    vendor = _vendor_of(node)
    if vendor:
        yield vendor, ansible_identifier(vendor, prefix=_VENDOR_PREFIX)
    role = node.labels.get(ROLE_LABEL)
    if role:
        yield role, ansible_identifier(role, prefix=_ROLE_PREFIX)


@dataclass(slots=True)
class _GroupNames:
    """Which source text each group came from, and what has been reported.

    Two things are reported, both on first sight only — this runs once per
    host, and a fifty-switch site would otherwise report the same rename fifty
    times:

    A **rename**, when the group is spelled differently from the text it came
    from — ``Ubiquiti Networks`` becoming ``vendor_ubiquiti_networks``, which
    an operator grepping for what they wrote would not otherwise find.

    A **collision**, when two different source strings fold to one group:
    ``sites/a b`` and ``sites/a/b`` are different namespaces and the same
    identifier, so the merge has to be said out loud rather than left for
    somebody to discover from a playbook that touched more hosts than they
    meant.
    """

    recorder: Recorder
    #: Group name -> the first source text that produced it.
    origin: dict[str, str] = field(default_factory=dict)
    #: ``(source, group)`` pairs already reported as a merge.
    merged: set[tuple[str, str]] = field(default_factory=set)

    def record(self, *, source: str, group: str) -> None:
        """Note that ``source`` produced ``group``, reporting anything new."""
        if group not in self.origin:
            self.origin[group] = source
            self.recorder.rewrite(group, field="group", original=source, rewritten=group)
            return
        first = self.origin[group]
        if first == source or (source, group) in self.merged:
            return
        self.merged.add((source, group))
        self.recorder.skip(
            source,
            Reason.NAME_COLLISION,
            f"folds to the Ansible group '{group}', which already stands for '{first}'; "
            f"the two are merged, so a playbook targeting it reaches both",
        )


def _namespace_group(namespace: str) -> str:
    """``sites/north/access`` becomes ``ns_sites_north_access``.

    Each path segment is folded on its own and the results are joined with
    ``_``. Folding the joined string instead would be wrong in the other
    direction — a segment called ``rack 1`` and a segment boundary would both
    become ``_``, and nothing downstream could tell them apart.

    ``keep_lead`` is passed because the segments are composed, not used alone:
    without it a segment called ``1-north`` would lose its leading digit and
    become indistinguishable from ``2-north``, silently merging two sites into
    one group. The ``ns_`` prefix supplies the legal opening the identifier
    needs.
    """
    segments = [segment for segment in namespace.split("/") if segment]
    return _NAMESPACE_PREFIX + "_".join(
        ansible_identifier(segment, keep_lead=True) for segment in segments
    )


def _link_namespaces(groups: dict[str, dict[str, list[str]]], namespaces: set[str]) -> None:
    """Make each namespace group a child of its parent namespace group.

    Driven by the namespace *paths*, never by the folded group names: a segment
    that folds to a string containing ``_`` — which anything with a space or a
    comma in it does — cannot be split back into segments afterwards, and doing
    so produced a tower of nonsense ancestors.

    Every ancestor is created even when no element sits directly in it, so a
    tree laid out as ``sites/<site>/<tier>`` still offers ``ns_sites`` to select
    on. An empty intermediate group is legal in Ansible and is exactly what a
    reader of the folder tree expects to find.
    """
    for namespace in namespaces:
        segments = [segment for segment in namespace.split("/") if segment]
        for depth in range(1, len(segments)):
            parent = _namespace_group("/".join(segments[:depth]))
            child = _namespace_group("/".join(segments[: depth + 1]))
            groups.setdefault(parent, {"hosts": [], "children": []})["children"].append(child)


def _root_groups(groups: Mapping[str, Mapping[str, list[str]]]) -> Iterator[str]:
    """The groups that hang directly off ``all``: everything with no parent."""
    nested = {child for entry in groups.values() for child in entry["children"]}
    for group in groups:
        if group not in nested:
            yield group


# --------------------------------------------------------------------------- #
# Host variables
# --------------------------------------------------------------------------- #


def _host_vars(node: Node, management: str) -> dict[str, Any]:
    """Everything a playbook can template a device's configuration from.

    Namespaced under ``netgraph_`` apart from ``ansible_host``: a variable this
    tool invents must not be able to collide with one Ansible or a role already
    defines, and a reader of a playbook should be able to tell at a glance which
    facts came out of the inventory tree.
    """
    site, room, rack, position, height = location_of(node)
    element = node.element
    spec = element.spec if element is not None else None
    variables: dict[str, Any] = {
        "ansible_host": management,
        "netgraph_element": node.fqn,
        "netgraph_name": node.name,
        "netgraph_kind": node.kind,
        "netgraph_namespace": node.namespace,
    }
    if node.description:
        variables["netgraph_description"] = node.description
    for key, value in (
        ("netgraph_vendor", getattr(spec, "vendor", None)),
        ("netgraph_model", getattr(spec, "model", None)),
        ("netgraph_serial", getattr(spec, "serial", None)),
    ):
        if value:
            variables[key] = value
    if node.labels:
        variables["netgraph_labels"] = dict(sorted(node.labels.items()))
    location = {
        key: value
        for key, value in (
            ("site", site),
            ("room", room),
            ("rack", rack),
            ("position", position),
            ("height", height if position is not None else None),
        )
        if value not in ("", None)
    }
    if location:
        variables["netgraph_location"] = location

    variables["netgraph_addresses"] = [address.cidr for address in element_addresses(node)]
    variables["netgraph_interfaces"] = [_interface(port) for port in node.ports]
    variables["netgraph_vlan_ids"] = sorted(node.vlans)
    vlans = _vlan_database(node)
    if vlans:
        variables["netgraph_vlans"] = vlans
    return variables


def _interface(port: PortView) -> dict[str, Any]:
    """One interface, in declaration order and with every configured fact.

    Declaration order, not sorted: ``spec.interfaces`` is written by a person
    and a template that renders it back into a device configuration should
    produce the order they wrote. The *set* of hosts and groups above is sorted;
    the contents of one host's list are its own.
    """
    record: dict[str, Any] = {"name": port.name, "type": port.type, "enabled": port.enabled}
    if port.description:
        record["description"] = port.description
    if port.mac:
        record["mac"] = port.mac
    if port.mtu is not None:
        record["mtu"] = port.mtu
    if port.addresses:
        record["addresses"] = list(port.addresses)
    if port.vlan_mode is not None:
        record["vlan_mode"] = port.vlan_mode
    if port.vlans:
        record["vlans"] = sorted(port.vlans)
    return record


def _vlan_database(node: Node) -> list[dict[str, Any]]:
    """``spec.vlans`` of a device: the names a template needs for a VLAN id.

    Only a device has one — an adapter and a patch panel carry no VLAN database
    — so this is empty rather than absent for everything else, and the caller
    omits the variable entirely.
    """
    element = node.element
    if not isinstance(element, Device):
        return []
    entries: list[dict[str, Any]] = []
    for vlan in sorted(element.spec.vlans, key=lambda entry: entry.id):
        record: dict[str, Any] = {"id": vlan.id}
        if vlan.name:
            record["name"] = vlan.name
        if vlan.description:
            record["description"] = vlan.description
        entries.append(record)
    return entries


def _vendor_of(node: Node) -> str | None:
    element = node.element
    return getattr(element.spec, "vendor", None) if element is not None else None
