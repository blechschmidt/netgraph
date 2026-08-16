"""What the report is *about*: the scopes, the shared derivations and the stamp.

One report covers a selection of an inventory, and inside it a set of sites. This
module works out both, and hands :mod:`netgraph.report.pages` a
:class:`Context` holding every derivation the pages share — so the pages
themselves are about wording and ordering rather than about resolving anything
twice.

Everything is derived from what netgraph already computes:

* the tables are :mod:`netgraph.listing`, the same functions ``netgraph list``
  prints, over a narrowed inventory (:func:`~netgraph.loader.inventory.subset`);
* the address plan is :func:`netgraph.ipam.build_report`;
* the cable schedule is :func:`netgraph.export.cables.schedule`;
* the power schedule is :func:`netgraph.power.power_plan`;
* the diagrams are :func:`~netgraph.render.graph.build_graph` at each layer.

**A site is a namespace.** Which namespace depends on the tree: a report of a
campus laid out as ``sites/<site>/<tier>`` should have a page per *site* and not
one per tier, so the grouping level is counted from the shallowest namespace every
element shares — the same definition ``--collapse-depth`` uses
(:func:`~netgraph.render.aggregate.common_prefix`) — and ``--group-depth``
overrides it. An element too shallow to be under any group is its own group, so
the groups partition the selection instead of covering part of it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from netgraph.diagnostics import Diagnostic
from netgraph.listing import Listing
from netgraph.loader.inventory import Inventory, namespace_of, short_name, subset
from netgraph.models import Adapter, Device, PatchPanel, Pdu
from netgraph.power import PowerPlan, power_plan
from netgraph.render.aggregate import common_prefix
from netgraph.render.graph import Edge, Graph, Layer, Node, build_graph
from netgraph.report.diagrams import Diagrams
from netgraph.report.layout import Layout
from netgraph.report.model import BLANK, Cell, Column, Meta, Table
from netgraph.report.options import Options

__all__ = [
    "DATA_LAYER",
    "FACT_LAYERS",
    "ROOT_SITE",
    "Context",
    "Scope",
    "grouping_level",
    "layers_for",
    "paged_elements",
    "prepare",
    "site_groups",
    "table_from_listing",
]

#: The layer every report holds a data view of, whether or not it draws one: the
#: cabling with the patch panels still in it. Everything a device page says about
#: what is plugged into it comes from here, because this is the one layer that has
#: not spliced a passive cross-connect out of the run (§15.2).
DATA_LAYER: Final = Layer.PHYSICAL

#: How the root namespace is named to a reader: it has no name of its own, and
#: an empty cell reads as a bug.
ROOT_SITE: Final = "(inventory root)"

#: The views a *page* reads facts from, whatever ``--layer`` asks to draw. A
#: device page states where the element is bolted, what is cabled to it, what it
#: routes and what feeds it, and no single layer holds all four: the cabling view
#: has the patch panels, the power view has the PDUs and the control-plane view
#: has the adjacencies. Built for the whole report even when nothing draws them,
#: because ``--layer`` chooses pictures and not facts.
FACT_LAYERS: Final = (DATA_LAYER, Layer.POWER, Layer.OVERLAY, Layer.ROUTING)

#: Element kinds that get a page of their own. A cable and a tunnel do not: they
#: are links, and a link is documented on both of the pages it joins.
_PAGE_KINDS: Final = (Device, Adapter, PatchPanel, Pdu)


@dataclass(frozen=True, slots=True)
class Scope:
    """An inventory narrowed to what one page is about, and the graphs over it."""

    #: The namespace this scope covers; ``""`` for the whole report.
    name: str
    inventory: Inventory
    #: The graphs built for this scope so far, keyed by layer. Mutated by
    #: :meth:`graph`, which is why this is a ``dict``: a page that needs a view
    #: nobody asked to *draw* — the VLAN matrix wants layer 2 even when
    #: ``--layer l3`` was given — should cost one graph, not one per table.
    graphs: dict[Layer, Graph] = field(default_factory=dict)

    def graph(self, layer: Layer) -> Graph:
        """The graph at ``layer``, built on demand and remembered."""
        existing = self.graphs.get(layer)
        if existing is None:
            existing = build_graph(self.inventory, layer=layer)
            self.graphs[layer] = existing
        return existing

    def at(self, layer: Layer) -> Graph | None:
        """The graph at ``layer`` *if it has already been built*, else ``None``.

        Distinct from :meth:`graph` on purpose: :meth:`node` searches several
        layers for one element and must not lay out five graphs to find it.
        """
        return self.graphs.get(layer)

    def nodes(self, layer: Layer) -> Mapping[str, Node]:
        return self.graph(layer).nodes

    def edges(self, layer: Layer) -> tuple[Edge, ...]:
        return self.graph(layer).edges

    def node(self, fqn: str) -> Node | None:
        """The node for ``fqn`` at whichever built layer holds it.

        A patch panel is spliced out of every layer above the cabling and a PDU
        only exists in the power view, so "the node for this element" is a search
        rather than a lookup. The order is fixed so the answer is deterministic,
        and only *built* graphs are consulted — :data:`FACT_LAYERS` is what makes
        sure the ones a device page reads are among them.
        """
        for layer in FACT_LAYERS:
            graph = self.at(layer)
            node = graph.nodes.get(fqn) if graph is not None else None
            if node is not None:
                return node
        return None


def paged_elements(inventory: Inventory) -> tuple[str, ...]:
    """Every element that gets a page of its own, in load order."""
    return tuple(
        fqn for fqn, element in inventory.elements.items() if isinstance(element, _PAGE_KINDS)
    )


def layers_for(inventory: Inventory, requested: Sequence[Layer] = ()) -> tuple[Layer, ...]:
    """Which layers this inventory is worth drawing, or exactly what was asked for.

    ``--layer`` is honoured verbatim when given, including a layer that turns out
    to be empty: a reader who asked for the overlay of a network with no tunnels
    should be told it has none rather than left wondering whether the flag worked.

    Without it, the set is derived from what the inventory *declares* — no patch
    panel, no cabling diagram; no PDU, no power diagram — so a small inventory
    gets a short report and a fully modelled one gets everything it has earned.
    """
    if requested:
        return tuple(dict.fromkeys(requested))

    devices = tuple(inventory.devices.values())
    interfaces = [
        interface for owner in inventory.interface_owners.values() for interface in owner.interfaces
    ]
    chosen: list[Layer] = []
    if inventory.patchpanels:
        chosen.append(Layer.PHYSICAL)
    chosen.append(Layer.L1)
    if any(device.spec.vlans for device in devices) or any(
        interface.vlan is not None for interface in interfaces
    ):
        chosen.append(Layer.L2)
    if any(interface.ipv4 is not None or interface.ipv6 is not None for interface in interfaces):
        chosen.append(Layer.L3)
    if inventory.tunnels:
        chosen.append(Layer.OVERLAY)
    if any(
        device.spec.routing is not None
        or device.spec.vrfs
        or device.spec.routes
        or device.spec.route_tables
        or device.spec.routing_policy
        for device in devices
    ):
        chosen.append(Layer.ROUTING)
    if any(
        element.metadata.location is not None and element.metadata.location.rack
        for element in inventory.elements.values()
    ):
        chosen.append(Layer.RACK)
    if inventory.pdus or any(device.spec.power is not None for device in devices):
        chosen.append(Layer.POWER)
    return tuple(chosen)


def grouping_level(inventory: Inventory, *, depth: int | None = None) -> int:
    """How many namespace components a site page's name has.

    ``depth`` counts levels below the namespace every element shares. ``None``
    picks it: 1 when the tree actually branches below that level — a campus laid
    out as ``sites/<site>/<tier>`` — and 0 when it does not. A home lab whose
    directories are ``routers/``, ``switches/`` and ``hosts/`` is one site, and
    splitting it into four pages would put every cable in it on none of them.
    """
    namespaces = [namespace_of(fqn) for fqn in paged_elements(inventory)]
    if not namespaces:
        return 0
    shared = len(common_prefix(namespaces))
    return shared + (_auto_depth(namespaces, shared=shared) if depth is None else max(depth, 0))


def site_groups(inventory: Inventory, *, depth: int | None = None) -> tuple[str, ...]:
    """The namespaces that get a site page, sorted.

    The groups **partition** the elements: every element that gets a page maps to
    exactly one of these, and an element whose namespace is shallower than the
    grouping level is a group of its own rather than a member of a deeper one.
    """
    level = grouping_level(inventory, depth=depth)
    namespaces = [namespace_of(fqn) for fqn in paged_elements(inventory)]
    return tuple(sorted({_group_of(namespace, level) for namespace in namespaces}))


def _auto_depth(namespaces: Sequence[str], *, shared: int) -> int:
    """1 when there is a level below the site level to summarise, else 0."""
    deepest = max(len(namespace.split("/")) if namespace else 0 for namespace in namespaces)
    return 1 if deepest > shared + 1 else 0


def _group_of(namespace: str, wanted: int) -> str:
    """The site page ``namespace`` belongs to, given the grouping level."""
    parts = namespace.split("/") if namespace else []
    return "/".join(parts[:wanted]) if len(parts) >= wanted else namespace


def _members_of(inventory: Inventory, group: str, *, level: int) -> frozenset[str]:
    """Every element of ``inventory`` that belongs on ``group``'s page.

    Membership is the grouping function and nothing else, so the pages partition
    the inventory: an element documented on two site pages would be counted twice
    in every total the overview prints.

    A cable and a tunnel are offered to *every* group and kept by
    :func:`~netgraph.loader.inventory.subset` only where everything they join
    survives. Their own namespace is the wrong criterion: an inventory that keeps
    its cabling in ``cables/`` — which the schema encourages, since a cable
    belongs to no single device — would otherwise have a cabling record on no page
    at all.
    """
    return frozenset(
        fqn
        for fqn, element in inventory.elements.items()
        if not isinstance(element, _PAGE_KINDS) or _group_of(namespace_of(fqn), level) == group
    )


@dataclass(frozen=True, slots=True)
class Context:
    """Everything the page builders share, resolved exactly once."""

    options: Options
    layout: Layout
    diagrams: Diagrams
    #: The whole selection the report covers.
    report: Scope
    #: The inventory before ``--namespace``/``--kind`` narrowed it, so a scoped
    #: report can still say which links leave the selection.
    full: Inventory
    #: Everything the selection could have been joined to: a scope over the
    #: inventory *before* the filters. Read only by the section that lists the
    #: links leaving a page.
    outside: Scope | None = None
    #: The layers this report draws, in page order. Derived once by
    #: :func:`layers_for` so every page draws the same set.
    layers: tuple[Layer, ...] = ()
    #: Open validation findings, already flattened.
    diagnostics: tuple[Diagnostic, ...] = ()
    power: PowerPlan = field(default_factory=PowerPlan)
    #: Site namespace → its scope, in page order.
    sites: Mapping[str, Scope] = field(default_factory=dict)
    #: Element → the site namespace whose page holds it.
    site_of: Mapping[str, str] = field(default_factory=dict)
    #: Element → the drawings it appears in, as ``(page path, layer)``. Filled in
    #: once the site pages have been built; see :func:`with_appearances`.
    appearances: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)

    @property
    def inventory(self) -> Inventory:
        return self.report.inventory

    def cell_for(self, fqn: str, *, text: str = "") -> Cell:
        """A cell naming an element, linked to its page when it has one."""
        return Cell(text=text or short_name(fqn) or fqn, page=self.layout.device(fqn))

    def site_cell(self, fqn: str) -> Cell:
        """A cell naming the site an element is documented under."""
        group = self.site_of.get(fqn, "")
        return Cell(text=group or ROOT_SITE, page=self.layout.site(group))

    def findings_for(self, elements: Iterable[str]) -> tuple[Diagnostic, ...]:
        """The findings that name one of ``elements``.

        A finding with no element at all — a syntax error in a file — belongs to
        the whole report rather than to a site, and is reported on the overview.
        """
        wanted = frozenset(elements)
        return tuple(
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.element is not None and diagnostic.element in wanted
        )


def prepare(
    inventory: Inventory,
    *,
    options: Options,
    layout: Layout,
    diagrams: Diagrams,
    diagnostics: Iterable[Diagnostic] = (),
    full: Inventory | None = None,
) -> Context:
    """Resolve everything the pages are built from.

    Args:
        inventory: The selection the report covers, already narrowed by the
            command line's filters.
        options: The resolved command line.
        layout: Where each page goes. Must have been built for these elements.
        diagrams: The drawing helper; filled in as pages are built.
        diagnostics: Open validation findings.
        full: The inventory before filtering. Defaults to ``inventory``.
    """
    layers = layers_for(inventory, options.layers)
    report = Scope(name="", inventory=inventory)
    for layer in dict.fromkeys((*FACT_LAYERS, *layers)):
        report.graph(layer)

    level = grouping_level(inventory, depth=options.group_depth)
    sites: dict[str, Scope] = {}
    for group in site_groups(inventory, depth=options.group_depth):
        narrowed = subset(inventory, _members_of(inventory, group, level=level))
        scope = Scope(name=group, inventory=narrowed)
        for layer in layers:
            scope.graph(layer)
        sites[group] = scope
    site_of = {fqn: group for group, scope in sites.items() for fqn in scope.inventory.elements}

    outside = inventory if full is None else full
    return Context(
        options=options,
        layout=layout,
        diagrams=diagrams,
        report=report,
        full=outside,
        # A scope over the *unfiltered* inventory, so a report of one site can
        # still say which links leave it. Its graphs are built on demand: a
        # report of everything never asks.
        outside=Scope(name="", inventory=outside),
        layers=layers,
        diagnostics=tuple(diagnostics),
        power=power_plan(inventory),
        sites=sites,
        site_of=site_of,
    )


def with_appearances(
    context: Context, appearances: Mapping[str, tuple[tuple[str, str], ...]]
) -> Context:
    """``context`` with the diagram cross-references filled in.

    Separate from :func:`prepare` because "which diagrams is this device in" is
    only answerable once the site pages have been drawn — and it is the device
    pages that want the answer.
    """
    return replace(context, appearances=appearances)


def meta(context: Context) -> Meta:
    """The provenance block every page carries."""
    inventory = context.inventory
    counts: dict[str, int] = {}
    for element in inventory.elements.values():
        counts[element.kind] = counts.get(element.kind, 0) + 1
    return Meta(
        title=context.options.title_for(inventory.root),
        inventory=context.options.inventory_name(inventory.root),
        version=context.options.version,
        generated_at=context.options.generated_at,
        revision=context.options.revision,
        revision_state=context.options.revision_state,
        scope=context.options.scope,
        format=context.options.format,
        counts={
            "elements": len(inventory.elements),
            "sites": len(context.sites),
            **dict(sorted(counts.items())),
        },
    )


def table_from_listing(
    context: Context,
    result: Listing,
    *,
    key: str,
    title: str,
    note: str = "",
    empty: str = "Nothing declared.",
    column: int = 0,
    record_key: str = "name",
) -> Table:
    """One :mod:`netgraph.listing` table, with its subject column linked.

    ``column`` and ``record_key`` say which cell names an element and where its
    fully-qualified name sits in the record — which is the only thing this
    package needs to know about a listing, since every other cell is already
    formatted for a reader.
    """
    rows: list[tuple[Cell, ...]] = []
    for cells, record in zip(result.rows, result.records, strict=True):
        subject = record.get(record_key)
        page = context.layout.device(subject) if isinstance(subject, str) else ""
        rows.append(
            tuple(
                Cell(text=text or BLANK, page=page if index == column else "")
                for index, text in enumerate(cells)
            )
        )
    return Table(
        key=key,
        title=title,
        columns=tuple(
            Column(header=header, align=align)
            for header, align in zip(result.headers, result.aligns, strict=True)
        ),
        rows=tuple(rows),
        note=note,
        empty=empty,
    )
