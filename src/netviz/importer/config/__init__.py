"""Reading a device's running configuration back, in the dialects netviz writes.

:mod:`netviz.export.config` turns an inventory into the configuration a device
would run. This closes the loop: the same seven dialects are read *back*, so what
``netviz export`` writes is exactly what ``netviz drift`` compares against,
and "does the network match the inventory?" becomes a question an operator can
answer with two commands and no bespoke collection script::

    netviz export netplan --name pc-desk -o want.yaml
    ssh pc-desk 'cat /etc/netplan/*.yaml' | netviz drift --host pc-desk -

The readers are the mirror of the emitters and are held to the same rule from the
other side: **nothing is invented**. A dialect that does not state something is
silent about it, and silence reaches :mod:`netviz.drift.coverage` as "this
capture cannot see that", never as "the network no longer has it". Which is why
the capability table there is as important as any parser here — a netplan file
lists every address of an interface it configures and *no* neighbour, so an
address missing from it is drift and a cable missing from it is nothing at all.

Two things make reading a generated file exact rather than merely likely.

**The banner names the dialect.** Anything netviz wrote carries
``netviz-dialect:`` in its first lines (:mod:`netviz.export.config.header`),
so ``--from`` and the sniffer are both unnecessary for the round trip.

**The banner names the element.** ``netviz-element:`` carries the
fully-qualified name, so ``--host`` is unnecessary too. A configuration collected
off a real device has neither, and then :func:`sniff` decides from the shape of
the file — which is reliable here because the seven grammars are disjoint in their
first non-comment line.

The one thing a running configuration is *not* is a superset of the generated
one. An operator's ``/etc/network/interfaces`` holds hooks, hand-written
``up`` lines and a distribution's ``lo`` stanza; the readers take what they
understand, leave the rest, and never treat an unrecognised directive as an
error. A drift check that refused to read a real file would be a drift check
nobody ran twice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from netviz.importer.config.common import (
    CONFIG_DIALECT_NAMES,
    banner_dialect,
    banner_element,
    fold_into,
    sniff,
    stanzas,
)
from netviz.importer.config.frr import read_frr
from netviz.importer.config.ifupdown import read_ifupdown
from netviz.importer.config.netplan import read_netplan
from netviz.importer.config.networkd import read_networkd
from netviz.importer.config.neutral import read_interfaces
from netviz.importer.config.nftables import read_nftables
from netviz.importer.config.wireguard import read_wireguard

__all__ = [
    "CONFIG_DIALECT_NAMES",
    "CONFIG_READERS",
    "ConfigReader",
    "banner_dialect",
    "banner_element",
    "fold_into",
    "read_frr",
    "read_ifupdown",
    "read_interfaces",
    "read_netplan",
    "read_networkd",
    "read_nftables",
    "read_wireguard",
    "sniff",
    "stanzas",
]

#: What a reader is: text in, one device folded into the draft. The signature is
#: the one :func:`netviz.importer.lldp.read_lldp` uses, minus the JSON payload,
#: because none of these formats is JSON.
ConfigReader = Callable[..., None]

#: Every configuration dialect ``--from`` accepts, keyed by the name
#: :func:`~netviz.importer.config.common.sniff` returns.
CONFIG_READERS: Final[Mapping[str, ConfigReader]] = {
    "netplan": read_netplan,
    "networkd": read_networkd,
    "ifupdown": read_ifupdown,
    "frr": read_frr,
    "nftables": read_nftables,
    "wireguard": read_wireguard,
    "interfaces": read_interfaces,
}
