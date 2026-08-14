"""``netgraph export`` — the inventory as operational artefacts.

A rendering answers "what does this network look like?". Everything here
answers "what do I *do* with it?", by turning the same resolved inventory and
the same graph a diagram is drawn from into files other tools consume:

=================  =========================================================
Format             What it is
=================  =========================================================
``hosts``          An ``/etc/hosts`` fragment: every element at every address
                   it holds, under its qualified name and its own name.
``dns-zone``       An RFC 1035 forward zone and the reverse zones implied by
                   the prefixes :mod:`netgraph.subnets` derives.
``ansible-inventory``  Ansible's JSON inventory: one host per element,
                   grouped by namespace, kind, vendor and role, with the
                   interface and VLAN detail a playbook templates from.
``prometheus-sd``  A Prometheus ``file_sd`` target list, labelled with
                   namespace, kind, vendor and site.
``cable-list``     The pull list: one row per physical run, both ends
                   located by rack, unit and panel port.
``routes``         An iproute2 script, one function per device, holding the
                   static routes that device declares (§16.2).
``power``          The load schedule: one row per power feed, both ends
                   located, with the per-PDU and per-PSE totals (§17.7).
``drawio``         An mxGraph diagram draw.io opens already arranged, carrying
                   the identity of each element in its cell so that the edited
                   file can be brought back (``docs/drawio.md``).
=================  =========================================================

Four promises hold across all eight, and they are why this is a package rather
than eight ad-hoc printers:

**Deterministic.** Every collection is sorted by an explicit canonical key —
never by dict order, never by the loader's directory traversal. Two runs over an
unchanged tree produce identical bytes, so an artefact is a file worth
committing and a diff in it means the network changed.

**Scoped like a render.** The same :class:`~netgraph.render.graph.FilterSpec`
that narrows a diagram narrows an export, so ``--namespace sites/north --kind
switch`` means the same thing to both.

**Loud about what it drops.** Every format is lossy, in ways specific to it: a
hosts file has nowhere to put a VLAN, a zone file has nowhere to put a cable,
an Ansible inventory has nowhere to put a device with no address, and a routing
script has nowhere to put a protocol. Each emitter
records what it left out and why (:mod:`netgraph.export.manifest`) and the CLI
prints that record as JSON on stderr, leaving stdout for the artefact.

**Correctly escaped.** Each target format has its own grammar for names and its
own quoting — RFC 1035 labels, Ansible identifiers, RFC 4180 fields, Markdown
cells — and folding an inventory name into one of them is done once, in
:mod:`netgraph.export.names`, with every fold recorded.

Adding a format
---------------

Write an emitter module exposing ``emit(ExportContext) -> str``, add one
:class:`Exporter` entry to :data:`EXPORTERS`, and the CLI picks it up: ``-f``
accepts it, ``--help`` describes it, the completion offers it, and the docs test
requires it to be documented. Declare the layers it needs; the CLI builds and
filters exactly those.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from netgraph.export import ansible, cables, dnszone, drawio, hosts, power, prometheus, routes
from netgraph.export.context import ExportContext, ExportOptions
from netgraph.export.manifest import MANIFEST_KIND, Manifest, Reason, Recorder, Skip
from netgraph.export.names import ansible_identifier, domain_name, is_domain_name, sanitise_label
from netgraph.export.prometheus import is_assignable_label, is_label_name
from netgraph.render.graph import Layer

__all__ = [
    "EXPORTERS",
    "FORMATS",
    "MANIFEST_KIND",
    "ExportContext",
    "ExportOptions",
    "ExportResult",
    "Exporter",
    "Manifest",
    "Reason",
    "Recorder",
    "Skip",
    "ansible_identifier",
    "domain_name",
    "export",
    "is_assignable_label",
    "is_domain_name",
    "is_label_name",
    "layers_for",
    "sanitise_label",
    "suffix_for",
]


@dataclass(frozen=True, slots=True)
class Exporter:
    """One emitter, and everything the CLI needs to know about it."""

    name: str
    #: One clause for ``--help``, lower case and without a trailing stop.
    description: str
    #: The graph layers the emitter reads. The CLI builds and filters exactly
    #: these, so an emitter cannot silently depend on an unfiltered view.
    layers: tuple[Layer, ...]
    #: Default file extension, for the docs and for ``-o`` completion.
    suffix: str
    #: What this format cannot hold, in one sentence. Surfaced by ``--help``
    #: and by ``docs/export.md``, because "lossy" is only useful with the *what*.
    lossy: str
    emit: Callable[[ExportContext], str]
    #: For a format whose layers depend on what was asked for. Only ``drawio``
    #: has one: it draws a single view and the reader picks it, so declaring a
    #: fixed layer would either build the wrong graph or build all nine.
    #: :attr:`layers` stays the honest default for ``--help`` and the docs.
    select: Callable[[ExportOptions], tuple[Layer, ...]] | None = None


#: The registry. Insertion order is the order ``--help`` lists them in, chosen
#: to run from the simplest artefact to the most specialised.
EXPORTERS: Final[Mapping[str, Exporter]] = {
    "hosts": Exporter(
        name="hosts",
        description="an /etc/hosts fragment, one line per address",
        layers=(Layer.L1,),
        suffix=".hosts",
        lossy="holds names and addresses only: no VLANs, no cabling, no hardware detail",
        emit=hosts.emit,
    ),
    "dns-zone": Exporter(
        name="dns-zone",
        description="RFC 1035 forward and reverse zone files",
        layers=(Layer.L1,),
        suffix=".zone",
        lossy=(
            "address records only, and one name per element: the short name is not "
            "published, because two namespaces may hold it"
        ),
        emit=dnszone.emit,
    ),
    "ansible-inventory": Exporter(
        name="ansible-inventory",
        description="Ansible's JSON inventory, grouped by namespace, kind, vendor and role",
        layers=(Layer.L2,),
        suffix=".json",
        lossy="carries no topology: which port is cabled to which has no representation",
        emit=ansible.emit,
    ),
    "prometheus-sd": Exporter(
        name="prometheus-sd",
        description="Prometheus file_sd targets, labelled by namespace, kind, vendor and site",
        layers=(Layer.L1,),
        suffix=".json",
        lossy="one address and a handful of labels per element; everything else is dropped",
        emit=prometheus.emit,
    ),
    "cable-list": Exporter(
        name="cable-list",
        description="a CSV or Markdown pull list, one row per physical run",
        layers=(Layer.PHYSICAL, Layer.L1),
        suffix=".csv",
        lossy="physical runs only: adapter attachments, tunnels and addressing are absent",
        emit=cables.emit,
    ),
    "routes": Exporter(
        name="routes",
        description="an iproute2 script of the static routes each device declares",
        layers=(Layer.L1,),
        suffix=".sh",
        lossy=(
            "static routes only: BGP and OSPF configuration is vendor syntax and is not "
            "invented here"
        ),
        emit=routes.emit,
    ),
    "power": Exporter(
        name="power",
        description="a CSV or JSON load schedule, one row per power feed",
        layers=(Layer.POWER,),
        suffix=".csv",
        lossy=(
            "power only: a feed carries no medium, no length and no label, and the data path "
            "the PoE rides on is not described"
        ),
        emit=power.emit,
    ),
    "drawio": Exporter(
        name="drawio",
        description="an mxGraph diagram draw.io opens, edits and hands back",
        layers=(Layer.L1,),
        suffix=".drawio",
        lossy=(
            "a picture and an identity per cell: names, kinds and coordinates, but no "
            "interfaces, addresses, VLANs or routing"
        ),
        emit=drawio.emit,
        select=lambda options: drawio.layers_for_options(options.view),
    ),
}

#: The format names, in registry order, for ``click.Choice`` and completion.
FORMATS: Final[tuple[str, ...]] = tuple(EXPORTERS)


@dataclass(frozen=True, slots=True)
class ExportResult:
    """An artefact and the record of what did not make it into one."""

    export_format: str
    #: The artefact itself, as text. Every format here is text; nothing in this
    #: package emits bytes, which is what makes all five of them diffable.
    payload: str
    manifest: Manifest

    def encode(self) -> bytes:
        """The artefact as UTF-8, which is what a file or stdout wants."""
        return self.payload.encode("utf-8")


def layers_for(export_format: str, options: ExportOptions | None = None) -> tuple[Layer, ...]:
    """The graph layers ``export_format`` needs built.

    Args:
        export_format: The registered name.
        options: What the command line settled. A format that draws a view the
            reader chose — ``drawio`` — needs them to know which layer to
            build; the other seven ignore them, and a caller with nothing to
            say gets each format's declared default.

    Raises:
        KeyError: No such format. The CLI validates with ``click.Choice``
            first, so reaching this is a programming error.
    """
    exporter = EXPORTERS[export_format]
    if exporter.select is not None and options is not None:
        return exporter.select(options)
    return exporter.layers


def suffix_for(export_format: str) -> str:
    """The conventional file extension of ``export_format``."""
    return EXPORTERS[export_format].suffix


def export(
    export_format: str,
    context_factory: Callable[[Recorder], ExportContext],
) -> ExportResult:
    """Run one emitter and seal its manifest.

    The context is built through a factory rather than passed in so that the
    recorder the emitter writes to is the same one this function seals: an
    emitter cannot be handed a recorder whose contents are then thrown away.

    Raises:
        KeyError: No such format.
    """
    exporter = EXPORTERS[export_format]
    recorder = Recorder()
    payload = exporter.emit(context_factory(recorder))
    return ExportResult(
        export_format=export_format,
        payload=payload,
        manifest=recorder.sealed(export_format),
    )
