# Copyright (c) netviz contributors
# MIT (see https://github.com/blechschmidt/netviz)
"""Five small filters, for the last inch between an answer and a file.

A query answers with what the inventory says: a list of rows, and an address
written the way the schema writes one, ``10.20.0.5/24``. A configuration file
wants one value, and sometimes wants a different part of it — ``Gateway=`` takes
a host address without its prefix, an ``ifupdown`` stanza takes a netmask, and a
route takes the network.

Doing that arithmetic in Jinja means either another collection on the control
node or a chain of ``split('/')`` calls in a template, and neither is a good
answer for something this small. So:

``one``
    Exactly one row, or an error naming how many there were. A template that
    silently took ``| first`` of three answers is a template that will one day
    configure the wrong address.
``host``, ``network``, ``netmask``, ``prefix_length``
    The four ways to read ``10.20.0.5/24``.

Anything larger than this belongs in ``ansible.utils``, which does it properly.
"""

from __future__ import annotations

import ipaddress

from ansible.errors import AnsibleFilterError


def one(rows, what="value"):
    """The single row of ``rows``, or an error saying how many there were."""
    if isinstance(rows, (str, bytes)) or not hasattr(rows, "__len__"):
        return rows
    if len(rows) != 1:
        listed = ", ".join(repr(row) for row in list(rows)[:4])
        raise AnsibleFilterError(
            f"expected exactly one {what} and the query answered with {len(rows)}"
            + (f": {listed}" if listed else "")
        )
    return next(iter(rows))


def host(address):
    """``10.20.0.5/24`` -> ``10.20.0.5``. An address with no prefix is itself."""
    return str(_interface(address).ip)


def network(address):
    """``10.20.0.5/24`` -> ``10.20.0.0/24``."""
    return str(_interface(address).network)


def netmask(address):
    """``10.20.0.5/24`` -> ``255.255.255.0``."""
    return str(_interface(address).netmask)


def prefix_length(address):
    """``10.20.0.5/24`` -> ``24``."""
    return int(_interface(address).network.prefixlen)


def _interface(address):
    """``address`` as an :mod:`ipaddress` interface.

    Raises:
        AnsibleFilterError: It is not an address at all. The message names the
            value, because the usual cause is a query that answered with a row
            rather than with the field of one.
    """
    try:
        return ipaddress.ip_interface(str(address))
    except ValueError as error:
        raise AnsibleFilterError(f"{address!r} is not an IP address: {error}") from error


class FilterModule:
    """The five, as Ansible collects them."""

    def filters(self):
        """Filter name -> the function, as ``netviz.netviz.<name>``."""
        return {
            "one": one,
            "host": host,
            "network": network,
            "netmask": netmask,
            "prefix_length": prefix_length,
        }
