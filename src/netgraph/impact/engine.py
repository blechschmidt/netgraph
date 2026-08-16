"""The analysis itself: what breaks, what is a single point of failure, what was promised.

Three questions, one set of graphs, and one rule that decides the shape of all of
it: an answer must be *exact* or it must not be given. An operator planning a
maintenance window is deciding whether to send somebody to a data centre at
three in the morning, and a blast radius that is approximately right is worse
than none, because it will be believed.

How a failure is simulated
--------------------------

By removing the elements from the inventory and building the graphs again — not
by deleting nodes out of an already-built graph. The difference matters at every
layer above the first. A broadcast domain is derived by walking the links that
carry a VLAN; a prefix is derived from the addresses of the elements in it; a
power feed is derived by walking the run to the far end. Delete a cable from a
finished layer-2 graph and the domains it partitions do not notice. Delete it
from the *inventory* and :func:`~netgraph.graph.broadcast_domains` re-derives two
domains where there was one, which is what actually happened.

It costs a second resolution pass, and that is affordable precisely because a
``--fail`` run does it twice in total rather than once per candidate — see
``docs/commands/impact.md`` for the measured numbers on a thousand-device tree.

How single points of failure are found
--------------------------------------

Not that way. Enumerating them means asking the question once per element, and
re-resolving the inventory a thousand times would take minutes. Instead
:func:`single_points` hands each layer to :func:`netgraph.connectivity.analyse`,
which gets every articulation point, every bridge and every isolation count out
of one depth-first search, in time linear in the size of the graph. The
identities of the isolated endpoints are then materialised only for the entries
the report will actually print, which is what the cutoff is for.

Power is not a layer of the same kind. A device whose only feed is one PDU is cut
off by that PDU whatever its cabling looks like, so
:func:`~netgraph.impact.graphs.unpowered` walks the feed graph separately and the
result is folded in as its own class of single point.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from netgraph.connectivity import LINK, NODE, Cut, Graph, analyse, components, reachable
from netgraph.expectations import Declaration, Expectation, declarations
from netgraph.impact.graphs import LAYERS, POWER, LayerView, feed_sources, unpowered, views
from netgraph.impact.model import (
    CAUSE_POWER,
    STATUS_BROKEN,
    STATUS_DEGRADED,
    STATUS_MISSING,
    STATUS_UNCHANGED,
    Failure,
    ImpactError,
    ImpactReport,
    LayerResult,
    PathResult,
    Split,
    Spof,
)
from netgraph.loader.inventory import Inventory, namespace_of, short_name
from netgraph.models import Adapter, Device, Element, Interface
from netgraph.power import PowerPlan, power_plan
from netgraph.trace import TracedPath, TraceError, TraceResult, trace
from netgraph.validate import Finding, validate

__all__ = [
    "DEFAULT_LIMIT",
    "GATEWAY_ANCHORS",
    "REDUNDANCY_RULES",
    "ROUTER_ANCHORS",
    "check_expectations",
    "gateways",
    "prune",
    "resolve_element",
    "simulate",
    "single_points",
    "split_path_spec",
]

#: How many single points of failure are reported before the list is cut off.
#: Large enough that a small network's answer is complete and small enough that
#: a thousand-device tree does not answer a question with a thousand rows.
DEFAULT_LIMIT: Final = 25

#: :attr:`~netgraph.impact.model.ImpactReport.anchor_source` when the anchors
#: were derived from the ``gateway`` an interface declares.
GATEWAY_ANCHORS: Final = "gateways"
#: … when no gateway was declared anywhere and the routers stood in.
ROUTER_ANCHORS: Final = "routers"
#: … when the operator named them with ``--from``.
GIVEN_ANCHORS: Final = "given"

#: The rules ``--redundancy`` reports, in catalogue order. They are ordinary
#: validation rules — ``netgraph validate`` gates on them too — and this is the
#: subset that is about survivability rather than about the documents.
REDUNDANCY_RULES: Final[tuple[str, ...]] = ("E047", "E048", "W141")

#: What ``--fail device/sw1`` may put in front of the slash. ``device`` and
#: ``link`` are classes rather than kinds: a person writing a maintenance ticket
#: says "the device", not "the router or switch or hub or computer or server".
_KIND_CLASSES: Final[Mapping[str, frozenset[str]]] = {
    "device": frozenset({"switch", "router", "firewall", "hub", "computer", "server"}),
    "link": frozenset({"cable", "tunnel"}),
}

_KINDS: Final[frozenset[str]] = frozenset(
    {
        "switch",
        "router",
        "firewall",
        "hub",
        "computer",
        "server",
        "cable",
        "adapter",
        "tunnel",
        "patchpanel",
        "pdu",
        "user",
        "group",
    }
)


# --------------------------------------------------------------------------- #
# Naming things
# --------------------------------------------------------------------------- #


def resolve_element(inventory: Inventory, spec: str) -> str:
    """Resolve what ``--fail`` was given to one fully-qualified name.

    Four spellings, tried in this order, because each later one could otherwise
    shadow an earlier:

    * a **fully-qualified name** — ``sites/north/switches/sw1`` — which is
      unambiguous by construction;
    * a **kind-qualified name** — ``device/sw1``, ``cable/rack1-a3`` — which is
      how a change ticket names a thing, and which disambiguates a switch and a
      cable that share a short name;
    * a **relative or fully-qualified reference**, resolved the way every other
      reference in the inventory is (:meth:`Inventory.lookup`);
    * a **short name**, when exactly one element in the whole tree carries it.

    Raises:
        ImpactError: nothing carries the name, or more than one thing does.
            :attr:`ImpactError.candidates` then names them.
    """
    text = spec.strip()
    if not text:
        raise ImpactError("an element to fail cannot be empty")
    if text in inventory.elements:
        return text

    prefix, separator, rest = _split_kind(text)
    if separator and rest:
        wanted = _KIND_CLASSES.get(prefix, frozenset({prefix}))
        found = _by_kind(inventory, rest, wanted)
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ImpactError(
                f"{spec!r} is ambiguous: {len(found)} elements match", candidates=found
            )
        raise ImpactError(
            f"{spec!r} names no {prefix} in this inventory",
            candidates=_by_kind(inventory, rest, _KINDS),
        )

    resolution = inventory.lookup(text)
    if resolution.fqn is not None:
        return resolution.fqn
    if resolution.ambiguous:
        raise ImpactError(
            f"{spec!r} is ambiguous: it matches {len(resolution.ambiguous)} elements. "
            f"Qualify it with a namespace or a kind, e.g. 'device/{text}'.",
            candidates=tuple(resolution.ambiguous),
        )
    raise ImpactError(f"no element named {spec!r} is declared in this inventory")


def _split_kind(text: str) -> tuple[str, str, str]:
    """Split a kind-qualified reference; ``("", "", text)`` when it is not one."""
    for separator in ("/", ":"):
        prefix, found, rest = text.partition(separator)
        if found and (prefix in _KIND_CLASSES or prefix in _KINDS):
            return prefix, separator, rest
    return "", "", text


def _by_kind(inventory: Inventory, name: str, kinds: frozenset[str]) -> tuple[str, ...]:
    """Every element of one of ``kinds`` that ``name`` could mean, in load order."""
    return tuple(
        fqn
        for fqn, element in inventory.elements.items()
        if element.kind in kinds and (fqn == name or short_name(fqn) == name)
    )


def split_path_spec(spec: str) -> tuple[str, str]:
    """Split a ``--path src=dst`` argument into its two ends.

    ``=`` rather than a colon or an arrow: a colon already means
    ``element:interface`` in an endpoint, and an arrow has to be quoted in every
    shell there is.

    Raises:
        ImpactError: the argument has no ``=``, or an empty end.
    """
    source, separator, destination = spec.partition("=")
    if not separator or not source.strip() or not destination.strip():
        raise ImpactError(f"{spec!r} is not a path: write it as 'SRC=DST', e.g. 'pc-alice=srv-web'")
    return source.strip(), destination.strip()


# --------------------------------------------------------------------------- #
# Where reachability is measured from
# --------------------------------------------------------------------------- #


def gateways(inventory: Inventory) -> tuple[str, ...]:
    """The elements the inventory treats as its way out, in load order.

    A *designated gateway* is not a kind or a flag; it is an element holding an
    address that some other interface names as its ``gateway``. That is the one
    definition the files already carry, and it is the right one: a router
    nobody points a default route at is not what anybody loses service through.
    """
    wanted: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for element in inventory.elements.values():
        for interface in _interfaces(element):
            for _, address in interface.gateways():
                wanted.add(address)
    if not wanted:
        return ()
    found: list[str] = []
    for fqn, element in inventory.elements.items():
        for interface in _interfaces(element):
            if any(address.ip in wanted for address in interface.addresses()):
                found.append(fqn)
                break
    return tuple(found)


def _interfaces(element: Element) -> Sequence[Interface]:
    """The interfaces of an element that has any; ``()`` for one that cannot."""
    if isinstance(element, (Device, Adapter)):
        return element.interfaces
    return ()


def anchors_for(inventory: Inventory, given: Sequence[str]) -> tuple[tuple[str, ...], str]:
    """Resolve ``--from``, or derive the anchors, and say which happened.

    Returns:
        The anchor fqns in load order, and one of :data:`GIVEN_ANCHORS`,
        :data:`GATEWAY_ANCHORS` or :data:`ROUTER_ANCHORS`. An inventory that
        declares neither a gateway nor a router yields an empty tuple, and the
        caller reports partitions without reachability rather than inventing an
        anchor nobody designated.
    """
    if given:
        return tuple(dict.fromkeys(resolve_element(inventory, spec) for spec in given)), (
            GIVEN_ANCHORS
        )
    derived = gateways(inventory)
    if derived:
        return derived, GATEWAY_ANCHORS
    routers = tuple(fqn for fqn, element in inventory.elements.items() if element.kind == "router")
    return routers, ROUTER_ANCHORS


# --------------------------------------------------------------------------- #
# Removing things
# --------------------------------------------------------------------------- #


def prune(inventory: Inventory, removed: Iterable[str]) -> Inventory:
    """``inventory`` without the named elements, re-indexed.

    A cable whose far end is gone becomes a dangling reference, which
    :func:`~netgraph.render.graph.build_graph` already drops from the drawing —
    so an unplugged switch takes its cables out of the topology without anything
    here having to reason about which ones they were.

    Load errors are deliberately *not* carried over. They describe the tree on
    disk; this inventory does not exist on disk, and a diagnostic pointing at a
    file for an element the analysis removed would be a lie.
    """
    gone = set(removed)
    pruned = Inventory(root=inventory.root)
    for fqn, element in inventory.elements.items():
        if fqn in gone:
            continue
        pruned.add(element, namespace=namespace_of(fqn), source=inventory.sources[fqn])
    for fqn, layout in inventory.layouts.items():
        pruned.add_layout(layout, namespace=namespace_of(fqn), source=inventory.layout_sources[fqn])
    return pruned


def _failures(inventory: Inventory, specs: Sequence[str], plan: PowerPlan) -> tuple[Failure, ...]:
    """Resolve every ``--fail`` and follow the power cascade out of it."""
    requested: list[Failure] = []
    seen: set[str] = set()
    for spec in specs:
        fqn = resolve_element(inventory, spec)
        if fqn in seen:
            continue
        seen.add(fqn)
        element = inventory.elements[fqn]
        requested.append(Failure(element=fqn, kind=element.kind, spec=spec))

    dark = set(unpowered(plan, seen))
    sources = feed_sources(plan)
    gone = seen | dark
    collateral = [
        Failure(
            element=fqn,
            kind=inventory.elements[fqn].kind,
            cause=CAUSE_POWER,
            # The source that actually went dark, which for a device two steps
            # down the chain is the switch above it rather than the PDU: an
            # operator restoring service works up the chain from what they can
            # see, and "lost pdu-a" on an access point they cannot see is a
            # cause they cannot act on.
            spec=next((source for source in sources.get(fqn, ()) if source in gone), ""),
        )
        # Inventory order, not the alphabetical order the cascade walked in: it
        # is the order every other listing in the tool uses.
        for fqn in inventory.elements
        if fqn in dark
    ]
    return (*requested, *collateral)


# --------------------------------------------------------------------------- #
# The simulation
# --------------------------------------------------------------------------- #


def simulate(
    inventory: Inventory,
    *,
    fail: Sequence[str] = (),
    anchors: Sequence[str] = (),
    paths: Sequence[str] = (),
    wanted_layers: Sequence[str] = LAYERS,
    spof: bool = False,
    redundancy: bool = False,
    limit: int = DEFAULT_LIMIT,
    minimum: int = 1,
) -> ImpactReport:
    """Run the analysis the flags asked for.

    Args:
        inventory: A tree loaded by :func:`~netgraph.loader.load_tree`.
        fail: Elements to remove, in the spelling :func:`resolve_element` takes.
        anchors: What ``--from`` named; empty derives them (:func:`anchors_for`).
        paths: ``src=dst`` assertions to re-check with the trace engine.
        wanted_layers: Which of :data:`~netgraph.impact.graphs.LAYERS` to report.
        spof: Also enumerate single points of failure.
        redundancy: Also check the declared expectations.
        limit: How many single points of failure to report; ``0`` for all.
        minimum: Ignore a single point of failure that isolates fewer endpoints
            than this.

    Returns:
        The report. Ordering is fixed everywhere in it.

    Raises:
        ImpactError: something named in ``fail``, ``anchors`` or ``paths`` does
            not resolve.
    """
    plan = power_plan(inventory)
    anchor_set, anchor_source = anchors_for(inventory, anchors)
    failures = _failures(inventory, fail, plan) if fail else ()
    modes = tuple(
        mode
        for mode, wanted in (("fail", bool(fail)), ("spof", spof), ("redundancy", redundancy))
        if wanted
    )

    # ``l1`` is built whether or not it is reported: it is the physical truth a
    # traced route has to be checked against, and building it twice would cost
    # more than carrying it through.
    needed = tuple(dict.fromkeys((*wanted_layers, "l1")))
    built = views(inventory, needed, plan=plan)
    before = tuple(view for view in built if view.layer in set(wanted_layers))
    notes: list[str] = []
    results: tuple[LayerResult, ...] = ()
    path_results: tuple[PathResult, ...] = ()

    if failures:
        gone = {failure.element for failure in failures}
        pruned = prune(inventory, gone)
        rebuilt = views(pruned, needed)
        after = {view.layer: view for view in rebuilt}
        results = tuple(_compare(view, after.get(view.layer), anchor_set, gone) for view in before)
        path_results = tuple(
            _check_paths(
                inventory,
                pruned,
                paths,
                _physical(built),
                _physical(rebuilt),
            )
        )
    elif paths:
        notes.append("--path says nothing without --fail: nothing was removed to compare against")

    spofs: tuple[Spof, ...] = ()
    total = 0
    if spof:
        spofs, total = single_points(
            inventory,
            before,
            anchor_set,
            plan=plan,
            limit=limit,
            minimum=minimum,
        )

    findings = check_expectations(inventory) if redundancy else ()

    if not anchor_set:
        notes.append(
            "no gateway is declared and no router exists, so reachability was not measured; "
            "name an anchor with --from"
        )
    return ImpactReport(
        root=inventory.root,
        modes=modes,
        failures=failures,
        anchors=anchor_set,
        anchor_source=anchor_source,
        layers=results,
        paths=path_results,
        spofs=spofs,
        spof_total=total,
        findings=findings,
        notes=tuple(notes),
    )


def _compare(
    before: LayerView,
    after: LayerView | None,
    anchors: Sequence[str],
    gone: set[str],
) -> LayerResult:
    """What one failure did to one layer, measured on both sides of it."""
    live_anchors = tuple(node for node in anchors if node in before.graph.position)
    surviving = tuple(node for node in live_anchors if node not in gone)

    served_before: frozenset[str] = frozenset()
    served_after: frozenset[str] = frozenset()
    note = ""
    if live_anchors:
        served_before = reachable(before.graph, live_anchors) & before.graph.endpoints
    else:
        note = "no anchor is present in this layer"
    if after is not None and surviving:
        served_after = reachable(after.graph, surviving) & after.graph.endpoints
    elif after is not None and live_anchors:
        note = "every anchor failed, so nothing is reachable from one"

    isolated = before.graph.order((served_before - served_after) - gone)
    stranded = before.graph.order(set(before.graph.endpoints) - served_before - gone)

    return LayerResult(
        layer=before.layer,
        title=before.title,
        anchors=before.graph.order(live_anchors),
        served_before=len(served_before),
        served_after=len(served_after),
        isolated=isolated,
        stranded=stranded,
        splits=_splits(before, after, gone),
        removed_nodes=before.graph.order(node for node in before.graph.nodes if node in gone),
        removed_links=tuple(
            edge.id
            for edge in before.graph.edges
            if after is None or after.graph.edge(edge.id) is None
        ),
        note=note,
    )


def _splits(before: LayerView, after: LayerView | None, gone: set[str]) -> tuple[Split, ...]:
    """Namespaces whose elements fell into more pieces than they were in.

    Measured per namespace rather than over the whole graph because "the
    inventory is now in four pieces" is not actionable and "sites/north is now
    in two" is: a namespace is a site, a rack or a floor, and a site split in
    half is an outage with an address.
    """
    if after is None:
        return ()
    grouped: dict[str, list[str]] = {}
    for node in before.elements():
        if node in gone:
            continue
        grouped.setdefault(namespace_of(node), []).append(node)

    found: list[Split] = []
    for namespace, members in sorted(grouped.items()):
        was = _pieces(before.graph, members)
        now = _pieces(after.graph, members)
        if len(now) > len(was):
            found.append(
                Split(
                    namespace=namespace,
                    before=len(was),
                    after=len(now),
                    fragments=now,
                )
            )
    return tuple(found)


def _pieces(graph: Graph, members: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """Group ``members`` by which of them can still reach each other.

    Reachability runs over the *whole* graph, not over the induced subgraph: two
    switches in one rack that reach each other through the core are one piece,
    and pretending otherwise would report every leaf rack as shattered.
    """
    remaining = [node for node in members if node in graph.position]
    seen: set[str] = set()
    pieces: list[tuple[str, ...]] = []
    for node in remaining:
        if node in seen:
            continue
        component = reachable(graph, (node,))
        piece = tuple(other for other in remaining if other in component)
        seen.update(piece)
        pieces.append(piece)
    return tuple(pieces)


def _check_paths(
    before: Inventory,
    after: Inventory,
    specs: Sequence[str],
    physical_before: LayerView | None,
    physical_after: LayerView | None,
) -> Iterable[PathResult]:
    """Re-run each ``--path`` assertion on both inventories.

    The trace engine is the authority on whether a route exists, so the check is
    made by *asking it twice* rather than by reasoning about the graphs — which
    is what keeps ``netgraph impact`` and ``netgraph path`` from disagreeing
    about the same pair of elements.

    With one correction, and it is not a disagreement. The engine's layer-3
    search calls two elements adjacent when they hold addresses in one prefix,
    which is the right definition for a *route* and not a sufficient one for a
    *frame*: unplug the switch between two hosts in ``10.1.10.0/24`` and they are
    still in that prefix. The check is therefore made twice over — once by the
    trace engine, once against the physical graph — and a route whose every hop
    is not physically realisable is not counted as surviving. Reporting one as
    intact is the single most dangerous thing this command could get wrong.
    """
    for spec in specs:
        source, destination = split_path_spec(spec)
        try:
            was = trace(before, source, destination)
        except TraceError as error:
            yield PathResult(
                spec=spec,
                source=source,
                destination=destination,
                status=STATUS_MISSING,
                note=str(error),
            )
            continue
        now = _trace_or_none(after, source, destination)
        counted_before = _realisable(was, physical_before)
        counted_after = _realisable(now, physical_after)
        note = ""
        if now is None:
            note = "an end of this path is gone, so there is nothing left to reach"
        elif counted_after < len(now.paths):
            dropped = len(now.paths) - counted_after
            note = (
                f"{dropped} route{'' if dropped == 1 else 's'} the trace still finds "
                f"cross{'es' if dropped == 1 else ''} a prefix or a tunnel whose two ends are "
                f"no longer physically connected, so {'it is' if dropped == 1 else 'they are'} "
                f"not counted"
            )
        yield PathResult(
            spec=spec,
            source=source,
            destination=destination,
            before=counted_before,
            after=counted_after,
            layer_before=str(was.layer) if was.layer is not None else None,
            layer_after=str(now.layer) if now is not None and now.layer is not None else None,
            status=_path_status(counted_before, counted_after),
            note=note,
        )


def _physical(built: Sequence[LayerView]) -> LayerView | None:
    return next((view for view in built if view.layer == "l1"), None)


def _realisable(result: TraceResult | None, physical: LayerView | None) -> int:
    """How many of the traced routes could actually carry a frame.

    A cable hop is realisable by construction — the graph it came out of is the
    graph the cable is in. A routed hop and a tunnel hop are not: both are
    adjacencies derived from configuration, and both need the two ends to be able
    to exchange frames at layer 1 before anything can cross them.
    """
    if result is None:
        return 0
    if physical is None:
        return len(result.paths)
    component = _component_index(physical)
    return sum(1 for path in result.paths if _hops_land(path, component))


def _hops_land(path: TracedPath, component: Mapping[str, int]) -> bool:
    for link in path.links:
        if link.kind not in _DERIVED_HOPS:
            continue
        near, far = component.get(link.source), component.get(link.target)
        if near is None or far is None or near != far:
            return False
    return True


#: Hop kinds that stand for a configured adjacency rather than for a wire, and
#: therefore have to be checked against the physical graph.
_DERIVED_HOPS: Final[frozenset[str]] = frozenset({"subnet", "tunnel"})


def _component_index(view: LayerView) -> Mapping[str, int]:
    """``node -> which physical island it is on``, islands numbered in graph order."""
    return {node: index for index, piece in enumerate(components(view.graph)) for node in piece}


def _trace_or_none(inventory: Inventory, source: str, destination: str) -> TraceResult | None:
    """Trace, or ``None`` when an endpoint no longer exists after the failure."""
    try:
        return trace(inventory, source, destination)
    except TraceError:
        return None


def _path_status(before: int, after: int) -> str:
    if not before:
        return STATUS_MISSING
    if after == 0:
        return STATUS_BROKEN
    if after < before:
        return STATUS_DEGRADED
    return STATUS_UNCHANGED


# --------------------------------------------------------------------------- #
# Single points of failure
# --------------------------------------------------------------------------- #


def single_points(
    inventory: Inventory,
    layer_views: Sequence[LayerView],
    anchors: Sequence[str],
    *,
    plan: PowerPlan | None = None,
    limit: int = DEFAULT_LIMIT,
    minimum: int = 1,
) -> tuple[tuple[Spof, ...], int]:
    """Enumerate every single point of failure, ranked by what it isolates.

    Args:
        inventory: The tree, for naming what each candidate is.
        layer_views: The views to sweep, from :func:`~netgraph.impact.graphs.views`.
        anchors: The nodes that must stay reachable.
        plan: A resolved power plan, when the caller has one.
        limit: How many to return; ``0`` for all of them.
        minimum: Drop a candidate isolating fewer endpoints than this. ``1`` —
            the default — drops nothing that costs anybody service.

    Returns:
        The ranked entries and how many there were in total, so a caller can say
        what the cutoff dropped rather than presenting a truncated list as the
        whole answer.
    """
    resolved = plan if plan is not None else power_plan(inventory)
    found: list[Spof] = []
    swept: dict[str, LayerView] = {}
    for view in layer_views:
        if view.layer == POWER:
            continue
        swept[view.layer] = view
        analysis = analyse(view.graph, anchors)
        found.extend(
            _spof(view, cut)
            for cut in analysis.cuts
            if cut.isolated >= minimum and view.is_failable(cut.kind, cut.id)
        )

    found.extend(_power_spofs(inventory, resolved, minimum))
    found.sort(key=lambda entry: entry.order)
    total = len(found)
    reported = found if limit <= 0 else found[:limit]
    return tuple(_materialise(entry, swept, anchors) for entry in reported), total


def _spof(view: LayerView, cut: Cut) -> Spof:
    """One :class:`~netgraph.connectivity.Cut`, told what it is."""
    if cut.kind == NODE:
        return Spof(
            layer=view.layer,
            kind=NODE,
            id=cut.id,
            isolated=cut.isolated,
            element_kind=view.kinds.get(cut.id, ""),
            articulation=cut.articulation,
        )
    return Spof(
        layer=view.layer,
        kind=LINK,
        id=cut.id,
        isolated=cut.isolated,
        element_kind=view.link_kinds.get(cut.id, ""),
        bridge=cut.bridge,
    )


def _materialise(entry: Spof, swept: Mapping[str, LayerView], anchors: Sequence[str]) -> Spof:
    """Fill in *which* endpoints an entry isolates, not just how many.

    Done here rather than in the sweep because it costs a traversal per entry:
    for a report of twenty-five that is nothing, and for the thousand candidates
    a large tree has it would be the whole cost of the command.

    With no anchors designated the counts came from "everything outside the
    largest fragment", which has no single set of survivors to subtract, so the
    names are left off rather than guessed at. The count is still exact.
    """
    view = swept.get(entry.layer)
    if entry.is_power or view is None or not anchors:
        return entry
    graph = view.graph
    served = reachable(graph, anchors) & graph.endpoints
    surviving = tuple(node for node in anchors if node != entry.id)
    if entry.kind == NODE:
        after = reachable(graph, surviving, without_nodes=(entry.id,))
    else:
        after = reachable(graph, surviving, without_edges=(entry.id,))
    lost = (served - after) - {entry.id}
    return Spof(
        layer=entry.layer,
        kind=entry.kind,
        id=entry.id,
        isolated=entry.isolated,
        element_kind=entry.element_kind,
        isolates=graph.order(lost),
        articulation=entry.articulation,
        bridge=entry.bridge,
    )


def _power_spofs(inventory: Inventory, plan: PowerPlan, minimum: int) -> list[Spof]:
    """Sources that are the only way power reaches something.

    A device whose cabling is textbook redundant and whose two power supplies are
    both in the same PDU has a single point of failure, and no amount of graph
    theory over the cables will find it. This is the sweep that does — including
    the cascade, so a PDU that feeds a switch that sources PoE for six access
    points is reported as isolating all seven.
    """
    sources = feed_sources(plan)
    sole: dict[str, list[str]] = {}
    for powered, feeding in sources.items():
        if len(feeding) == 1:
            sole.setdefault(feeding[0], []).append(powered)
    found: list[Spof] = []
    for source in plan.nodes:
        direct = sole.get(source)
        if not direct:
            continue
        dark = unpowered(plan, {source})
        if len(dark) < minimum:
            continue
        element = inventory.elements.get(source)
        found.append(
            Spof(
                layer=POWER,
                kind=NODE,
                id=source,
                isolated=len(dark),
                element_kind=element.kind if element is not None else "",
                isolates=dark,
                feeds=tuple(direct),
            )
        )
    return found


# --------------------------------------------------------------------------- #
# Declared expectations
# --------------------------------------------------------------------------- #


def check_expectations(inventory: Inventory) -> tuple[Finding, ...]:
    """The redundancy findings of ``inventory``, in validator order.

    Delegated to :func:`netgraph.validate.validate` rather than reimplemented,
    exactly the way :mod:`netgraph.ipam` delegates its own subset: a rule that
    two commands can disagree about is a rule nobody can act on, and ``netgraph
    validate`` has to gate on these anyway for CI to be able to.
    """
    wanted = frozenset(REDUNDANCY_RULES)
    return tuple(finding for finding in validate(inventory) if finding.rule in wanted)


def expectations(inventory: Inventory) -> tuple[Declaration, ...]:
    """Every declared expectation, for a report that wants to list them."""
    return declarations(inventory)


def declared_gateway_expectations(inventory: Inventory) -> tuple[str, ...]:
    """The elements that declared a ``gateway`` expectation, in load order."""
    return tuple(
        declaration.element
        for declaration in declarations(inventory)
        if declaration.wants(Expectation.GATEWAY)
    )
