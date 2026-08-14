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
``netplan``,       The configuration a device would actually run, one directory
``networkd``,      per device: :mod:`netgraph.export.config`. These are the only
``ifupdown``,      formats here whose artefact is a *tree* rather than one
``frr``,           document, and the only ones that can refuse — a dialect that
``wireguard``,     cannot express a declared field writes nothing and says which
``interfaces``     field, rather than emitting a device that is almost right.
=================  =========================================================

Four promises hold across all fourteen, and they are why this is a package
rather than fourteen ad-hoc printers:

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
from functools import partial
from typing import Final

from netgraph.export import ansible, cables, dnszone, drawio, hosts, power, prometheus, routes
from netgraph.export.config import (
    CONFIG_DIALECTS,
    CONFIG_FORMATS,
    CONFIG_LAYERS,
    ConfigSet,
    UnsupportedConfigError,
)
from netgraph.export.config import emit as config_emit
from netgraph.export.config import generate as generate_config
from netgraph.export.context import ExportContext, ExportOptions
from netgraph.export.manifest import MANIFEST_KIND, Manifest, Reason, Recorder, Skip
from netgraph.export.names import ansible_identifier, domain_name, is_domain_name, sanitise_label
from netgraph.export.prometheus import is_assignable_label, is_label_name
from netgraph.render.graph import Layer

__all__ = [
    "CONFIG_DIALECTS",
    "CONFIG_FORMATS",
    "EXPORTERS",
    "FORMATS",
    "MANIFEST_KIND",
    "ConfigSet",
    "ExportContext",
    "ExportOptions",
    "ExportResult",
    "Exporter",
    "Manifest",
    "Reason",
    "Recorder",
    "Skip",
    "UnsupportedConfigError",
    "ansible_identifier",
    "bundle_for",
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
    #: For a format whose artefact is a *tree of files* rather than one
    #: document — the six configuration dialects, which write one directory per
    #: device (:mod:`netgraph.export.config`). ``--out DIR`` writes the tree;
    #: :attr:`emit` remains the single-stream form stdout gets.
    bundle: Callable[[ExportContext], ConfigSet] | None = None


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
    # The configuration dialects, generated from their own registry so that
    # adding one there is the only edit needed: their descriptions, their
    # lossiness and their order all come from :data:`CONFIG_DIALECTS`, and the
    # two registries cannot disagree about what ``FORMAT`` accepts.
    **{
        name: Exporter(
            name=name,
            description=dialect.description,
            layers=CONFIG_LAYERS,
            suffix=dialect.suffix,
            lossy=dialect.lossy,
            emit=config_emit(name),
            bundle=partial(generate_config, name),
        )
        for name, dialect in CONFIG_DIALECTS.items()
    },
}

#: The format names, in registry order, for ``click.Choice`` and completion.
FORMATS: Final[tuple[str, ...]] = tuple(EXPORTERS)


@dataclass(frozen=True, slots=True)
class ExportResult:
    """An artefact and the record of what did not make it into one."""

    export_format: str
    #: The artefact itself, as text. Every format here is text; nothing in this
    #: package emits bytes, which is what makes all of them diffable.
    payload: str
    manifest: Manifest
    #: The same artefact as a tree of files, for the six configuration dialects;
    #: ``None`` for every format whose artefact is one document. When it is set,
    #: :attr:`payload` was derived from it rather than generated separately, so
    #: the two cannot describe different devices.
    bundle: ConfigSet | None = None

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


def bundle_for(export_format: str) -> Callable[[ExportContext], ConfigSet] | None:
    """The tree-of-files builder of ``export_format``, or ``None``.

    ``None`` for the eight formats whose artefact is one document. The six
    configuration dialects return a builder, and it is what ``--out DIR`` writes
    from: a device that produces four files produces four files, rather than one
    concatenation somebody would then have to split.

    Raises:
        KeyError: No such format.
    """
    return EXPORTERS[export_format].bundle


def export(
    export_format: str,
    context_factory: Callable[[Recorder], ExportContext],
) -> ExportResult:
    """Run one emitter and seal its manifest.

    The context is built through a factory rather than passed in so that the
    recorder the emitter writes to is the same one this function seals: an
    emitter cannot be handed a recorder whose contents are then thrown away.

    A format with a :attr:`Exporter.bundle` is run through *that* and its
    single-stream form derived from the result, rather than being run twice —
    once for ``--out`` and once for stdout. Two runs would be two passes over the
    inventory producing two manifests, and the one the caller kept would be the
    one describing the artefact they did not use.

    Raises:
        KeyError: No such format.
        UnsupportedConfigError: A configuration dialect was asked to write a
            device declaring something it cannot express. Nothing was produced.
    """
    exporter = EXPORTERS[export_format]
    recorder = Recorder()
    context = context_factory(recorder)
    bundle = exporter.bundle(context) if exporter.bundle is not None else None
    payload = (
        bundle.as_stream(CONFIG_DIALECTS[export_format].comment)
        if bundle is not None
        else exporter.emit(context)
    )
    return ExportResult(
        export_format=export_format,
        payload=payload,
        manifest=recorder.sealed(export_format),
        bundle=bundle,
    )
