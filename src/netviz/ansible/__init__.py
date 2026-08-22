"""netviz as an Ansible control-node library: the inventory, and the answers.

Ansible has two questions for a source of truth, and they are not the same
question. *Who is out there?* is the dynamic inventory: hosts, groups, and the
variables each host starts with. *What should this host's configuration say?* is
a template, and it is asked once per file per host, with the answer pasted into
something a machine will run.

netviz has had an answer to the first since :mod:`netviz.export.ansible` —
``netviz export ansible-inventory`` writes the JSON document
``ansible-inventory -i`` reads. The second is what this package adds, and the
piece that makes it possible is the parameter (:mod:`netviz.nql.binding`): a
template's query has a hole in it where the host goes, and a hole that is a
*token* cannot be closed by a value that happens to contain a quotation mark.

So::

    [Match]
    Name={{ item.name }}

    [Network]
    {% for address in item.addresses %}
    Address={{ address }}
    {% endfor %}

over::

    {{ query('netviz.netviz.query',
             'select (device filter .fqn = $fqn).interfaces { name, addresses := .addresses.address }') }}

is a systemd unit whose addresses came out of the inventory tree, with nothing
interpolated into the query and nothing gathered from the machine.

What is here
------------

:mod:`netviz.ansible.session`
    One tree, loaded once, asked many times. A play's worth of templates is one
    load and then dictionary lookups.
:mod:`netviz.ansible.inventory`
    The dynamic inventory document, which is the exporter's document plus
    variables and groups that are *queries*.
:mod:`netviz.ansible.collection`
    The shipped ``netviz.netviz`` collection — an inventory plugin, a lookup
    plugin and four filters — and where to put it so Ansible finds it.

The plugins themselves are thin: they declare their options in the form Ansible
documents and validates, and then call in here. Everything that decides an
answer is in netviz, is typed, and is tested without Ansible installed; what
needs Ansible to test is the wiring, and that is the part that is twenty lines.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from netviz.ansible.collection import (
    COLLECTION,
    DEFAULT_DESTINATION,
    NAMESPACE,
    collection_path,
    collections_path,
    install,
)
from netviz.ansible.inventory import HOST_PARAMS, InventoryOptions, build, host_params, hosts_of
from netviz.ansible.session import (
    DEFAULT_LAYER,
    Answer,
    InventoryRejected,
    Session,
    forget,
    layer_named,
    open_session,
)
from netviz.render.graph import Layer

__all__ = [
    "COLLECTION",
    "DEFAULT_DESTINATION",
    "DEFAULT_LAYER",
    "HOST_PARAMS",
    "NAMESPACE",
    "Answer",
    "InventoryOptions",
    "InventoryRejected",
    "Session",
    "answer",
    "build",
    "collection_path",
    "collections_path",
    "forget",
    "host_params",
    "hosts_of",
    "install",
    "inventory_document",
    "layer_named",
    "open_session",
]


def answer(
    root: Path | str,
    expression: str,
    *,
    params: Mapping[str, Any] | None = None,
    host: str | None = None,
    strict: bool = False,
    force: bool = False,
    layer: Layer | str | None = None,
) -> list[Any]:
    """Answer one query about the tree at ``root``, as a list of rows.

    Args:
        root: The inventory tree, or a single YAML file.
        expression: A relational query, or a selector.
        params: Values for the ``$name`` holes in it. These win over the ones
            ``host`` binds, so a template can ask about a host other than the
            one it is being rendered for by saying which.
        host: An Ansible host name, as this inventory spells it. Its element's
            identity is bound to :data:`~netviz.ansible.inventory.HOST_PARAMS`
            — ``$host``, ``$fqn``, ``$name``, ``$namespace``, ``$kind`` — so the
            common template needs no arguments at all. A name that is not a host
            here binds nothing, and the query then says which parameter it
            wanted.
        strict: Treat warnings as errors while validating the tree.
        force: Answer even from a tree with errors in it.
        layer: Which graph a *selector* is answered against, as the enum or as
            its name. A relational query reads the whole inventory and ignores
            it.

    Returns:
        The rows, JSON-ready: scalars and objects from a relational query, and
        fully-qualified element names from a selector.

    Raises:
        InventoryRejected: The tree does not load or does not validate.
        QueryError: The query does not parse or names something unknown.
    """
    session = open_session(root, strict=strict, force=force)
    bound = dict(_host_params(session, expression, host))
    bound.update(params or {})
    return list(session.ask(expression, bound or None, layer=layer_named(layer)).rows)


def inventory_document(
    root: Path | str,
    options: InventoryOptions | None = None,
    *,
    strict: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """The whole dynamic inventory for ``root``.

    Raises:
        InventoryRejected: The tree does not load or does not validate.
        QueryError: A declared variable or group query is not answerable.
    """
    return build(open_session(root, strict=strict, force=force), options)


def _host_params(session: Session, expression: str, host: str | None) -> Mapping[str, str]:
    """What ``host`` binds, or nothing.

    The hostvars table costs an export to build, so it is not built for a query
    that has no hole in it — which is most of them, and all of the ones a
    selector can write.
    """
    if host is None or "$" not in expression:
        return {}
    variables = hosts_of(session).get(host)
    return host_params(host, variables) if variables is not None else {}
