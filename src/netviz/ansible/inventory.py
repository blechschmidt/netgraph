"""The dynamic inventory a playbook runs against, and the queries hung off it.

The document itself is not new: ``netviz export ansible-inventory`` has
produced it since §46, and this calls that emitter rather than a second one.
Two implementations of "which hosts are there, and in which groups" would drift,
and the day they did, an inventory file checked into a repository and the plugin
that replaces it would disagree about who is a server.

What this adds is the part an exporter cannot have: **variables that are
answers**. A host variable declared as a query is run once per host with that
host bound, so a template can say ``{{ netviz_mgmt }}`` and get the address the
inventory says the machine has, rather than a fact gathered from the machine
itself — which is the wrong direction when the point is to *configure* it.

Five parameters are bound for the host being considered, so a query never has to
interpolate anything:

``$host``
    The name Ansible knows it by, as this document spells it.
``$fqn``
    The element's fully-qualified name, ``sites/north/sw-01``. This is the one
    to match on: it is unique across the tree, and ``.fqn`` is a property of
    every object in the schema.
``$name``, ``$namespace``, ``$kind``
    The short name, the namespace it sits in, and its element kind.

Groups work the same way, in the other direction: a group declared as a query is
run *once*, and every host it names joins it. A relational query answers with
whatever it projects — so it must project something that names elements — and a
selector answers with fully-qualified names already.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from netviz.ansible.session import DEFAULT_LAYER, Answer, Session
from netviz.errors import NetvizError
from netviz.export import export, layers_for
from netviz.export.context import ExportContext, ExportOptions
from netviz.export.manifest import Recorder
from netviz.query import QueryError
from netviz.render.graph import FilterSpec, Layer

__all__ = [
    "FORMAT",
    "HOST_PARAMS",
    "InventoryOptions",
    "build",
    "host_params",
    "hosts_of",
]

#: The exporter this is a view of. Named once, here, so the coupling is a fact
#: about this module rather than a string in three places.
FORMAT = "ansible-inventory"

#: Host variable holding the element's fully-qualified name. The exporter writes
#: it, and it is what makes an Ansible host resolvable back to an element.
ELEMENT_VAR = "netviz_element"

#: The parameters bound while a per-host query runs, and the host variable each
#: is read from. ``$host`` is the key of the hostvars table itself.
HOST_PARAMS: Mapping[str, str] = {
    "fqn": ELEMENT_VAR,
    "name": "netviz_name",
    "kind": "netviz_kind",
    "namespace": "netviz_namespace",
}


@dataclass(frozen=True, slots=True)
class InventoryOptions:
    """What a caller may vary about the document.

    Everything here is optional and the defaults produce exactly what ``netviz
    export ansible-inventory`` produces, which is the property that keeps the
    two honest: the plugin is the exporter plus whatever was asked for.
    """

    #: A selector query narrowing which elements become hosts. The same language
    #: ``--select`` speaks on every other command.
    select: str | None = None
    #: Host variable name -> query, run once per host.
    host_vars: Mapping[str, str] = field(default_factory=dict)
    #: Group name -> query, run once, naming the hosts that join it.
    groups: Mapping[str, str] = field(default_factory=dict)
    #: What the layer-2 graph is built as. Rarely anything else; it is here
    #: because a caller asking about layer 3 should not have to reach past this
    #: type to do it.
    layer: Layer = DEFAULT_LAYER


def build(session: Session, options: InventoryOptions | None = None) -> dict[str, Any]:
    """The inventory document, as ``ansible-inventory --list`` prints it.

    Raises:
        NetvizError: A declared group is one the document already has.
        QueryError: A declared query does not parse, or a group query answers
            with something that does not name a host.
    """
    chosen = options or InventoryOptions()
    document = _base(session, chosen)
    hostvars: dict[str, dict[str, Any]] = document["_meta"]["hostvars"]
    for name, query in chosen.host_vars.items():
        for host, variables in hostvars.items():
            variables[name] = _one_or_all(
                session.ask(query, host_params(host, variables), source=f"vars.{name}")
            )
    for group, query in chosen.groups.items():
        if group in document:
            # ``ns_*``, ``kind_*``, ``vendor_*`` and ``role_*`` are derived, and a
            # query that quietly replaced one of them would leave a group whose
            # name says one thing and whose membership is another.
            raise NetvizError(
                f"the group '{group}' is one this inventory already derives; "
                "name the query's group something else"
            )
        members = _hosts_named(session, query, hostvars, group)
        if not members:
            continue
        document[group] = {"hosts": members}
        children = document["all"].setdefault("children", [])
        if group not in children:
            children.append(group)
    return document


def hosts_of(session: Session, options: InventoryOptions | None = None) -> dict[str, Any]:
    """Every host and its variables, with nothing queried.

    What a lookup needs to turn ``inventory_hostname`` back into an element, and
    memoised on the session because a play asks for it once per template.
    """
    key = ("hosts", options.select if options else None)
    if key not in session.derived:
        cached: dict[str, Any] = _base(session, options or InventoryOptions())["_meta"]["hostvars"]
        session.derived[key] = cached
    found: dict[str, Any] = session.derived[key]
    return found


def host_params(host: str, variables: Mapping[str, Any]) -> dict[str, str]:
    """The parameters a per-host query is run with.

    A variable the exporter did not write is not bound at all, rather than bound
    to an empty string: a query naming ``$kind`` for a host that has no kind
    should say so, and an empty string would quietly match nothing instead.
    """
    bound = {"host": host}
    for param, variable in HOST_PARAMS.items():
        value = variables.get(variable)
        if isinstance(value, str) and value:
            bound[param] = value
    return bound


def _base(session: Session, options: InventoryOptions) -> dict[str, Any]:
    """The exporter's document, parsed back into Python.

    Through :func:`~netviz.export.export` rather than by calling the emitter, so
    the manifest is sealed the way every other export seals it — nothing is
    dropped here that ``netviz export ansible-inventory`` would have reported.
    """
    spec = session.select(options.select) if options.select else FilterSpec()
    graphs = {
        layer: session.narrowed(spec, layer=layer) for layer in layers_for(FORMAT, ExportOptions())
    }
    result = export(
        FORMAT,
        lambda recorder: _context(session, graphs, recorder),
    )
    document: dict[str, Any] = json.loads(result.payload)
    return document


def _context(session: Session, graphs: Mapping[Layer, Any], recorder: Recorder) -> ExportContext:
    """One export context, built the way the CLI builds it."""
    return ExportContext(
        inventory=session.inventory,
        graphs=dict(graphs),
        options=ExportOptions(),
        recorder=recorder,
    )


def _one_or_all(answer: Answer) -> Any:
    """A query's rows as a variable's value.

    One row is that row, and anything else is the list — including none of them,
    which is an empty list rather than ``None``. A template testing ``if
    netviz_mgmt`` reads both the same way, and one testing ``| length`` gets the
    honest count.
    """
    if len(answer.rows) == 1:
        return answer.rows[0]
    return list(answer.rows)


def _hosts_named(
    session: Session, query: str, hostvars: Mapping[str, Mapping[str, Any]], group: str
) -> list[str]:
    """The hosts a group query names, in this document's spelling.

    A query answers about *elements*; a group holds *hosts*. The two are the same
    machines under two names, and this is the join: every row is read as a
    fully-qualified name, or as an object with one in it, and looked up in the
    hostvars table. A row naming an element that is not a host — one with no
    management address, or one the selection dropped — is simply not in the
    group, because a group listing a host the inventory does not define is an
    error every Ansible command reports.

    Raises:
        QueryError: A row is not something a name can be read out of.
    """
    by_element = {
        variables.get(ELEMENT_VAR): host
        for host, variables in hostvars.items()
        if variables.get(ELEMENT_VAR)
    }
    named: list[str] = []
    for row in session.ask(query, source=f"groups.{group}").rows:
        for candidate in _names_in(row, group):
            host = by_element.get(candidate) or (candidate if candidate in hostvars else None)
            if host is not None and host not in named:
                named.append(host)
    return sorted(named)


def _names_in(row: Any, group: str) -> Iterator[str]:
    """Every string in ``row`` that could be an element's name.

    Raises:
        QueryError: ``row`` holds nothing that could be one.
    """
    if isinstance(row, str):
        yield row
        return
    if isinstance(row, Mapping):
        found = [value for value in row.values() if isinstance(value, str)]
        if found:
            yield from found
            return
    raise QueryError(
        f"the query for group '{group}' answered with {type(row).__name__}, and a group "
        "is a list of hosts",
        help="project a name: select device.fqn filter ...",
    )
