"""``/etc/hosts`` fragment: every element, at every address it holds.

The simplest of the five artefacts and the one with the shortest path to being
useful — drop it into ``/etc/hosts.d/`` or append it to ``/etc/hosts`` and the
names in the diagram resolve on the machine you are debugging from.

Format (RFC 952, and the ``hosts(5)`` convention every resolver follows)::

    <address>   <canonical name>   [alias ...]

One line per *address*, not per element: a router with four addresses gets four
lines, which is what makes ``ping rtr-core`` reach it on whichever family is up.
Where two elements share an address — which the validator reports as ``E004``,
but ``--force`` still renders — both names land on the one line rather than on
two conflicting ones, because a resolver reading two lines for one address uses
the first and silently ignores the second.

What it drops
-------------

Loopback and link-local addresses, because ``127.0.0.1 my-server`` is wrong on
every machine that is not that server. Unnumbered interfaces, because there is
nothing to write. VLANs, cabling, hardware — a hosts file is a name-to-address
map and holds none of it. Everything omitted is in the manifest on stderr.
"""

from __future__ import annotations

from collections.abc import Iterator

from netviz.export.context import (
    Address,
    ExportContext,
    NameRegistry,
    element_addresses,
    elements_of,
    record_addressless,
)
from netviz.export.header import comment_header
from netviz.render.graph import Layer

__all__ = ["emit"]

#: Column the names start in, when no address is wider. Two spaces past the
#: widest IPv4 address (``255.255.255.255``) so a v4-only file lines up in the
#: familiar place, and past that the column simply grows.
_MIN_ADDRESS_COLUMN = 17


def emit(context: ExportContext) -> str:
    """Render the hosts fragment, newline-terminated."""
    recorder = context.recorder
    entries: dict[str, tuple[Address, list[str]]] = {}

    # The one format that publishes a short alias beside the qualified name.
    registry = NameRegistry(recorder, aliases=True)
    nodes = elements_of(context.at(Layer.L1))
    recorder.considered = len(nodes)
    for node in nodes:
        addresses = element_addresses(node)
        if not addresses:
            record_addressless(node, recorder)
            continue
        name = registry.register(node)
        if name is None:
            continue
        recorder.emitted += 1
        for address in addresses:
            _, names = entries.setdefault(address.ip, (address, []))
            names.extend(alias for alias in name.aliases if alias not in names)

    ordered = sorted(entries.values(), key=lambda entry: entry[0].packed)
    width = max((len(address.ip) for address, _ in ordered), default=0)
    column = max(width + 2, _MIN_ADDRESS_COLUMN)
    body = [f"{address.ip.ljust(column - 1)} {' '.join(names)}" for address, names in ordered]
    return "".join(f"{line}\n" for line in [*_header(context, len(ordered)), *body])


def _header(context: ExportContext, addresses: int) -> Iterator[str]:
    yield from comment_header(
        "#",
        "hosts",
        (
            f"{context.recorder.emitted} element(s), {addresses} address(es).",
            "Loopback and link-local addresses and unnumbered interfaces are left out;",
            "the manifest on stderr says which elements produced nothing and why.",
            "Each element is published under its qualified name and, as an alias,",
            "its own name: 'sw-01.access.north.sites sw-01'.",
        ),
    )
