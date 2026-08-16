"""Device configuration, generated from the inventory: the last step of the loop.

Everything else netgraph exports is *about* the network — a hosts file, a zone, a
pull list, a monitoring target. None of them is the network. The configuration a
device actually runs is, and until netgraph could write one, the inventory was a
document beside the truth rather than the source of it: somebody still typed the
addresses into the box, and the typing is where the two started to disagree.

Seven dialects, each a pure function from a :class:`~netgraph.export.config.plan.
DevicePlan` to a set of files:

===============  =============================================================
``netplan``      ``etc/netplan/10-netgraph.yaml`` for a Linux host
``networkd``     ``etc/systemd/network/*.network`` and ``*.netdev``
``ifupdown``     ``etc/network/interfaces``, the Debian original
``frr``          ``etc/frr/frr.conf``: VRFs, static routes, OSPF and BGP
``nftables``     ``etc/nftables.conf``: the zones, the filter policy and the NAT
``wireguard``    ``etc/wireguard/<if>.conf``, one per tunnel, keys left blank
``interfaces``   netgraph's own vendor-neutral rendering, for everything else
===============  =============================================================

Six of the seven describe how a device is *wired*; ``nftables`` describes what it
refuses, which is the one half of a configuration none of the others could write.

Four rules hold across all seven, and they are the difference between a generator
worth trusting and one worth reading over.

**Nothing is invented.** A value that is not in the inventory is not in the
output. Where a dialect *requires* a value netgraph does not hold — a WireGuard
private key, a wifi passphrase — an obvious placeholder is written instead, and
where a dialect requires one that cannot be derived at all — netplan's numeric
VRF table, which is not a route distinguisher — the device is refused.

**A refusal is a refusal.** A dialect that cannot express a field records an
:class:`~netgraph.export.config.model.Unsupported` naming it, and the whole run
fails with every refusal listed. Nothing is written. A configuration missing one
field is a device that is *almost* what the inventory says, with nothing in the
file to say which part is missing — which is worse than no file.

**A skip is not a refusal.** A field outside a dialect's remit — a PoE budget in
netplan, an OSPF area in ifupdown — does not make the generated file wrong, so it
is recorded in the export manifest naming the dialect that *does* cover it, and
the file is written.

**Every file says where it came from.** The banner names the element, its kind
and every inventory document behind it, in a form
:mod:`netgraph.importer` reads back: ``netgraph drift`` takes these same six
dialects as its live input, so generate-then-compare is symmetric.

Adding a dialect
----------------

Write a module exposing ``selects``, ``declines``, ``limits`` and ``files``, add
a :class:`ConfigDialect` entry to :data:`CONFIG_DIALECTS`, and the export
registry, the CLI, ``--help``, completion and the docs test pick it up.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from netgraph.export.config import (
    frr,
    ifupdown,
    netplan,
    networkd,
    neutral,
    nftables,
    wireguard,
)
from netgraph.export.config.header import DIALECT_KEY, ELEMENT_KEY, SOURCE_KEY, parse_banner
from netgraph.export.config.model import (
    ConfigFile,
    ConfigSet,
    DeviceConfig,
    Unsupported,
    UnsupportedConfigError,
    device_directory,
    safe_relative_path,
)
from netgraph.export.config.plan import DevicePlan, device_plans
from netgraph.export.context import ExportContext
from netgraph.export.manifest import Reason, Recorder
from netgraph.render.graph import Layer

__all__ = [
    "CONFIG_DIALECTS",
    "CONFIG_LAYERS",
    "DIALECT_KEY",
    "ELEMENT_KEY",
    "SOURCE_KEY",
    "ConfigDialect",
    "ConfigFile",
    "ConfigSet",
    "DeviceConfig",
    "DevicePlan",
    "Unsupported",
    "UnsupportedConfigError",
    "device_directory",
    "generate",
    "parse_banner",
    "safe_relative_path",
]

#: The graph a configuration export is scoped by. ``l1`` because the selection
#: is a set of *devices* and that is the layer whose nodes are elements; the
#: cabling it also carries is unused, and building a second graph to avoid
#: carrying it would cost more than it saved.
CONFIG_LAYERS: Final[tuple[Layer, ...]] = (Layer.L1,)


@dataclass(frozen=True, slots=True)
class ConfigDialect:
    """One configuration dialect, and everything the registry needs from it."""

    name: str
    #: One clause for ``--help``, lower case and without a trailing stop.
    description: str
    #: What this dialect cannot hold, in one sentence, for ``--help`` and the docs.
    lossy: str
    #: Conventional extension of its *primary* file, for ``-o`` completion. A
    #: dialect writing several files still has one a reader means by "the file".
    suffix: str
    #: The comment introducer of the format, for the banner separating several
    #: files on stdout. ``!`` for FRR, ``#`` for everything else here.
    comment: str
    #: Does this dialect have anything to say about a device of this shape?
    selects: Callable[[DevicePlan], bool]
    #: Why a device it did not select got nothing, for the manifest.
    declines: Callable[[DevicePlan], str]
    #: What it would have had to invent for a device it *did* select. Checked
    #: for every selected device before a single file is written.
    limits: Callable[[DevicePlan], tuple[Unsupported, ...]]
    #: The files, for a device it selected and did not refuse.
    files: Callable[[DevicePlan, Recorder], tuple[ConfigFile, ...]]


#: The registry. Insertion order is the order ``--help`` lists them in: the three
#: Linux host renderers, then the two daemons, then the fallback that covers
#: whatever the five did not.
CONFIG_DIALECTS: Final[Mapping[str, ConfigDialect]] = {
    "netplan": ConfigDialect(
        name="netplan",
        description="netplan YAML for a Linux host: addresses, VLANs, bonds, tunnels, routes",
        lossy=(
            "host interface configuration only: no bridge-port 802.1Q, no routing protocol, "
            "and no key material — netplan's VRF table cannot be derived from a route "
            "distinguisher, so a device declaring one is refused"
        ),
        suffix=".yaml",
        comment="#",
        selects=netplan.selects,
        declines=netplan.declines,
        limits=netplan.limits,
        files=netplan.files,
    ),
    "networkd": ConfigDialect(
        name="networkd",
        description="systemd-networkd .network and .netdev units, one pair per stacked link",
        lossy=(
            "host interface configuration only: a radio's SSIDs belong to wpa_supplicant, a "
            "routing protocol to frr, and a VRF needs a table number the inventory does not "
            "state"
        ),
        suffix=".network",
        comment="#",
        selects=networkd.selects,
        declines=networkd.declines,
        limits=networkd.limits,
        files=networkd.files,
    ),
    "ifupdown": ConfigDialect(
        name="ifupdown",
        description="a Debian /etc/network/interfaces, with routes as up/down commands",
        lossy=(
            "the oldest and narrowest of the three host dialects: no VRF, no bridge-port "
            "802.1Q, no tunnel syntax, and an access point is hostapd's job"
        ),
        suffix=".interfaces",
        comment="#",
        selects=ifupdown.selects,
        declines=ifupdown.declines,
        limits=ifupdown.limits,
        files=ifupdown.files,
    ),
    "frr": ConfigDialect(
        name="frr",
        description="an frr.conf of the VRFs, static routes, OSPF areas and BGP neighbours",
        lossy=(
            "the control plane only: FRR does not create interfaces, so bridges, bonds, VLAN "
            "sub-interfaces and tunnels come from one of the host dialects"
        ),
        suffix=".conf",
        comment="!",
        selects=frr.selects,
        declines=frr.declines,
        limits=frr.limits,
        files=frr.files,
    ),
    "nftables": ConfigDialect(
        name="nftables",
        description="an /etc/nftables.conf of the zones, the filter policy and the NAT",
        lossy=(
            "the firewall only: nothing here creates an interface or an address, and a rule "
            "inverting its whole selector set has no nftables spelling, so a device using "
            "'invert' is refused rather than written matching the opposite"
        ),
        suffix=".conf",
        comment="#",
        selects=nftables.selects,
        declines=nftables.declines,
        limits=nftables.limits,
        files=nftables.files,
    ),
    "wireguard": ConfigDialect(
        name="wireguard",
        description="a wg-quick .conf per WireGuard tunnel, with peers from both endpoints",
        lossy=(
            "one tunnel per file and no key material: private and public keys are written as "
            "placeholders, because an inventory that held them would be a secret in version "
            "control"
        ),
        suffix=".conf",
        comment="#",
        selects=wireguard.selects,
        declines=wireguard.declines,
        limits=wireguard.limits,
        files=wireguard.files,
    ),
    "interfaces": ConfigDialect(
        name="interfaces",
        description="a vendor-neutral rendering of every device, so nothing is left out",
        lossy=(
            "nothing — it is netgraph's own grammar and holds whatever the inventory states; "
            "what it cannot do is be applied, since no system reads it"
        ),
        suffix=".conf",
        comment="#",
        selects=neutral.selects,
        declines=lambda plan: "",  # never reached: this dialect selects every device
        limits=lambda plan: (),
        files=neutral.files,
    ),
}

#: The dialect names, in registry order.
CONFIG_FORMATS: Final[tuple[str, ...]] = tuple(CONFIG_DIALECTS)


def generate(dialect: str, context: ExportContext) -> ConfigSet:
    """Run one dialect over every selected device.

    Two passes, and the order matters. Every selected device is asked for its
    limits first; only if *none* of them refused is a single file rendered. A
    dialect that wrote four devices and then refused the fifth would leave an
    output directory that looks like a complete estate and is not, and the
    operator most likely to be caught by that is the one running it from a
    pipeline.

    Args:
        dialect: A key of :data:`CONFIG_DIALECTS`.
        context: The resolved inventory, the filtered graph and the recorder.

    Raises:
        KeyError: No such dialect. The CLI validates with ``click.Choice``
            first, so reaching this is a programming error.
        UnsupportedConfigError: At least one selected device declares something
            this dialect cannot express. Nothing has been written.
    """
    entry = CONFIG_DIALECTS[dialect]
    recorder = context.recorder
    plans = device_plans(context)
    recorder.considered = len(plans)

    selected: list[DevicePlan] = []
    refusals: list[Unsupported] = []
    for plan in plans:
        if not entry.selects(plan):
            recorder.skip(plan.fqn, Reason.NOT_REPRESENTABLE, entry.declines(plan))
            continue
        selected.append(plan)
        refusals.extend(entry.limits(plan))
    if refusals:
        raise UnsupportedConfigError(dialect, refusals)

    devices: list[DeviceConfig] = []
    for plan in selected:
        files = entry.files(plan, recorder)
        if files:
            devices.append(DeviceConfig(element=plan.fqn, files=tuple(files)))
    return ConfigSet(dialect=dialect, devices=tuple(devices))


def emit(dialect: str) -> Callable[[ExportContext], str]:
    """The single-string emitter the export registry needs for ``dialect``.

    A configuration set is a tree of files and the registry's contract is one
    artefact, so the two are bridged here rather than in each dialect:
    :meth:`~netgraph.export.config.model.ConfigSet.as_stream` writes a single
    file verbatim and separates several with a banner in the dialect's own
    comment syntax. ``--out DIR`` bypasses this and writes the tree.
    """

    def run(context: ExportContext) -> str:
        return generate(dialect, context).as_stream(CONFIG_DIALECTS[dialect].comment)

    return run
