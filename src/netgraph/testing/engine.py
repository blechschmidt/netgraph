"""Grading the assertions a ``kind: testsuite`` document makes.

One function — :func:`run_tests` — walks every suite in load order, grades every
assertion in the order it is written, and returns a
:class:`~netgraph.testing.model.TestReport`. Nothing here re-reads the inventory
or re-resolves a reference: every claim is checked against the graphs
:func:`~netgraph.render.graph.build_graph` already builds and the routes
:mod:`netgraph.trace` already finds, so a failing test and a rendered diagram
cannot disagree about what is connected to what.

The graphs are built **once per run**, lazily, and shared by every assertion
(:class:`_Context`). A suite of forty assertions over a thousand-device tree
otherwise spends its whole time resolving the same cables forty times.

Why a failed assertion is never an exception
--------------------------------------------

An assertion that names an element the inventory does not hold is a *failing
test*, not a broken command. It fails with the same shape as one that named a
real element and found no path — a message, the elements involved, what the
graph actually contained, and the file and line the assertion is written on —
because that is what makes a red CI run something a person can act on without
opening a terminal. The only thing that stops the run is an inventory that will
not load, and that is the caller's decision, not this module's.
"""

from __future__ import annotations

import fnmatch
import ipaddress
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from netgraph import __version__
from netgraph.impact.engine import anchors_for, single_points
from netgraph.impact.graphs import LAYERS, POWER, LayerView, views
from netgraph.loader.inventory import Inventory, short_name
from netgraph.models import Assertion, AssertionLayer, AssertionType, TestSuite
from netgraph.power import PowerPlan, power_plan
from netgraph.render.graph import FilterSpec, Graph, Layer, Node, build_graph
from netgraph.testing.fields import FieldError, evaluate, render_value
from netgraph.testing.model import (
    FAILED,
    PASSED,
    SKIPPED,
    Location,
    SuiteResult,
    TestReport,
    Verdict,
)
from netgraph.testing.selectors import (
    SelectorError,
    parse_selector,
    query_spec,
    select_nodes,
)
from netgraph.trace import DEFAULT_MAX_HOPS, TracedPath, TraceError, TraceResult, trace

__all__ = ["MAX_PAIRS", "MAX_REPORTED", "run_tests", "suite_location"]

#: Ceiling on the routes one reachability assertion may trace. ``from`` and
#: ``to`` may each be a selector, and the product of two selectors over a large
#: inventory is a search that would never finish. A refusal that says how to
#: narrow the assertion is more use than a hang.
MAX_PAIRS: Final = 256

#: How many offending items a failure lists before it says "and N more". A
#: failure has to be readable in a terminal and in a CI annotation; a hundred
#: lines of detail is a log nobody scrolls.
MAX_REPORTED: Final = 10

#: Characters that make a ``from``/``to`` a selector rather than one endpoint.
#: ``=`` is a filter term; the three globbing characters are a name pattern.
_SELECTOR_MARKS: Final = ("=", "*", "?", "[")


def run_tests(
    inventory: Inventory,
    *,
    names: Sequence[str] = (),
    max_hops: int = DEFAULT_MAX_HOPS,
) -> TestReport:
    """Grade every suite the inventory declares.

    Args:
        inventory: A tree loaded by :func:`~netgraph.loader.load_tree`.
        names: Globs narrowing which suites run, matched against the
            fully-qualified and the short name. Empty runs all of them. A glob
            matching nothing is reported in
            :attr:`~netgraph.testing.model.TestReport.unmatched` rather than
            silently running zero assertions.
        max_hops: Default ceiling on the length of a traced route, overridden
            per assertion by ``max_hops``.

    Returns:
        The report. Ordering is fixed everywhere in it: suites in load order,
        assertions in document order.
    """
    context = _Context(inventory=inventory, max_hops=max_hops)
    selected, unmatched = _select_suites(inventory, names)
    return TestReport(
        root=inventory.root,
        suites=tuple(_grade(context, fqn, suite) for fqn, suite in selected),
        version=__version__,
        unmatched=unmatched,
    )


def _select_suites(
    inventory: Inventory, names: Sequence[str]
) -> tuple[list[tuple[str, TestSuite]], tuple[str, ...]]:
    """Which suites to run, and which of ``names`` matched nothing."""
    every = list(inventory.test_suites.items())
    if not names:
        return every, ()
    kept: list[tuple[str, TestSuite]] = []
    unmatched: list[str] = []
    for pattern in names:
        matched = [
            (fqn, suite)
            for fqn, suite in every
            if fnmatch.fnmatchcase(fqn, pattern) or fnmatch.fnmatchcase(short_name(fqn), pattern)
        ]
        if not matched:
            unmatched.append(pattern)
        kept.extend(entry for entry in matched if entry not in kept)
    # Load order, not command-line order: a report is read as a list that should
    # stay put between runs.
    return [entry for entry in every if entry in kept], tuple(unmatched)


def suite_location(
    inventory: Inventory, fqn: str, path: Sequence[str | int] = ()
) -> Location | None:
    """Where ``path`` inside the suite called ``fqn`` is written.

    ``path`` is relative to the document, so ``("spec", "assertions", 3)`` is
    the fourth assertion. Returns ``None`` when the suite is not one this
    inventory holds, which only happens to a caller that built the name itself.
    """
    source = inventory.test_suite_sources.get(fqn)
    if source is None:  # pragma: no cover - every graded suite was loaded
        return None
    site = source.locate(path)
    line = source.line if site is None else site.line
    return Location(file=source.relative, line=line)


# --------------------------------------------------------------------------- #
# The shared, lazily-built view of the inventory
# --------------------------------------------------------------------------- #


@dataclass
class _Context:
    """The graphs every assertion in a run shares, built at most once each."""

    inventory: Inventory
    max_hops: int = DEFAULT_MAX_HOPS
    _graph: Graph | None = field(default=None, repr=False)
    _plan: PowerPlan | None = field(default=None, repr=False)
    _views: dict[str, LayerView] = field(default_factory=dict, repr=False)
    _anchors: tuple[str, ...] | None = field(default=None, repr=False)

    def graph(self) -> Graph:
        """The layer-2 graph, which is the layer-1 node set plus VLAN membership."""
        if self._graph is None:
            self._graph = build_graph(self.inventory, layer=Layer.L2)
        return self._graph

    def plan(self) -> PowerPlan:
        if self._plan is None:
            self._plan = power_plan(self.inventory)
        return self._plan

    def views(self, wanted: Sequence[str]) -> tuple[LayerView, ...]:
        """The named failure-analysis views, building only what is missing."""
        missing = [name for name in wanted if name not in self._views]
        if missing:
            for view in views(self.inventory, missing, plan=self.plan()):
                self._views[view.layer] = view
        return tuple(self._views[name] for name in wanted if name in self._views)

    def anchors(self) -> tuple[str, ...]:
        """What the inventory treats as its way out; see :func:`anchors_for`."""
        if self._anchors is None:
            self._anchors = anchors_for(self.inventory, ())[0]
        return self._anchors

    def node(self, fqn: str) -> Node | None:
        return self.graph().nodes.get(fqn)


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What one evaluator decided, before it is dressed up as a verdict."""

    state: str
    message: str = ""
    detail: tuple[str, ...] = ()
    elements: tuple[str, ...] = ()


def _grade(context: _Context, fqn: str, suite: TestSuite) -> SuiteResult:
    """Grade every assertion of one suite."""
    verdicts: list[Verdict] = []
    for index, assertion in enumerate(suite.assertions):
        outcome = _evaluate(context, assertion)
        verdicts.append(
            Verdict(
                suite=fqn,
                index=index,
                type=str(assertion.type),
                title=assertion.title,
                state=outcome.state,
                message=outcome.message,
                detail=outcome.detail,
                elements=outcome.elements,
                location=suite_location(context.inventory, fqn, ("spec", "assertions", index)),
                description=suite.spec.assertions[index].description or "",
            )
        )
    return SuiteResult(
        name=fqn,
        description=suite.spec.description or "",
        location=suite_location(context.inventory, fqn),
        verdicts=tuple(verdicts),
    )


def _evaluate(context: _Context, assertion: Assertion) -> _Outcome:
    """Grade one assertion, turning every foreseeable problem into a failure."""
    try:
        return _EVALUATORS[assertion.type](context, assertion)
    except (SelectorError, FieldError, TraceError) as exc:
        return _Outcome(FAILED, f"the assertion could not be evaluated: {exc}")


# -- reachability ------------------------------------------------------------


def _pairs(context: _Context, assertion: Assertion) -> list[tuple[str, str]]:
    """Every ``(source, destination)`` the assertion is about.

    ``from`` and ``to`` are usually one endpoint each, in which case this is one
    pair. Either may instead be a selector, and then the claim is made about
    every combination — "every access switch reaches the core" is one line.
    """
    sources = _endpoints(context, assertion.source, role="from")
    destinations = _endpoints(context, assertion.to, role="to")
    if len(sources) * len(destinations) > MAX_PAIRS:
        raise SelectorError(
            f"'{assertion.source}' x '{assertion.to}' is "
            f"{len(sources) * len(destinations)} routes to trace, above the ceiling of "
            f"{MAX_PAIRS}; narrow one side of the assertion"
        )
    return [
        (source, destination)
        for source in sources
        for destination in destinations
        if source != destination
    ]


def _endpoints(context: _Context, spec: str | None, *, role: str) -> list[str]:
    """One endpoint, or every element a selector matches."""
    assert spec is not None  # the model requires both on a reachability assertion
    if not any(mark in spec for mark in _SELECTOR_MARKS):
        return [spec]
    selected = select_nodes(context.graph(), parse_selector(spec))
    if not selected:
        raise SelectorError(f"the {role} selector {spec!r} matches no element")
    return list(selected)


def _reachable(context: _Context, assertion: Assertion) -> _Outcome:
    """``reachable`` — every pair has a route, at the layer asked for."""
    pairs = _pairs(context, assertion)
    if not pairs:
        return _Outcome(FAILED, f"'{assertion.source}' and '{assertion.to}' are the same element")
    broken: list[str] = []
    for source, destination in pairs:
        result = _trace(context, assertion, source, destination)
        if not result.paths:
            broken.append(f"{source} -> {destination}: {_why_not(result)}")
        elif (layer := _wanted_layer(assertion)) is not None and result.layer is not layer:
            found = "no" if result.layer is None else str(result.layer.value)
            broken.append(
                f"{source} -> {destination}: reachable, but at layer {found} rather than "
                f"{layer.value} ({_render_path(result.paths[0])})"
            )
    if not broken:
        return _Outcome(PASSED, elements=_involved(pairs))
    return _Outcome(
        FAILED,
        f"{_plural(len(broken), 'route', len(pairs))} {_verb(len(broken), 'does', 'do')} not exist",
        detail=_capped(broken),
        elements=_involved(pairs),
    )


def _not_reachable(context: _Context, assertion: Assertion) -> _Outcome:
    """``not-reachable`` — no pair has a route the assertion would object to."""
    pairs = _pairs(context, assertion)
    if not pairs:
        return _Outcome(FAILED, f"'{assertion.source}' and '{assertion.to}' are the same element")
    found: list[str] = []
    wanted = _wanted_layer(assertion)
    for source, destination in pairs:
        result = _trace(context, assertion, source, destination)
        if not result.paths:
            continue
        if wanted is not None and result.layer is not wanted:
            continue
        at = "" if result.layer is None else f" at layer {result.layer.value}"
        found.append(f"{source} -> {destination}{at}: {_render_path(result.paths[0])}")
    if not found:
        return _Outcome(PASSED, elements=_involved(pairs))
    return _Outcome(
        FAILED,
        f"{_plural(len(found), 'route', len(pairs))} {_verb(len(found), 'exists', 'exist')} "
        f"and should not",
        detail=_capped(found),
        elements=_involved(pairs),
    )


def _path_shorter_than(context: _Context, assertion: Assertion) -> _Outcome:
    """``path-shorter-than`` — every pair has a route under the hop bound."""
    assert assertion.hops is not None  # required by the model
    pairs = _pairs(context, assertion)
    if not pairs:
        return _Outcome(FAILED, f"'{assertion.source}' and '{assertion.to}' are the same element")
    bad: list[str] = []
    for source, destination in pairs:
        result = _trace(context, assertion, source, destination)
        if not result.paths:
            bad.append(f"{source} -> {destination}: {_why_not(result)}")
            continue
        best = min(result.paths, key=lambda path: len(path.links))
        if len(best.links) >= assertion.hops:
            bad.append(f"{source} -> {destination}: {len(best.links)} hops ({_render_path(best)})")
    if not bad:
        return _Outcome(PASSED, elements=_involved(pairs))
    return _Outcome(
        FAILED,
        f"{_plural(len(bad), 'route', len(pairs))} {_verb(len(bad), 'is', 'are')} not shorter "
        f"than {assertion.hops} hops",
        detail=_capped(bad),
        elements=_involved(pairs),
    )


def _trace(context: _Context, assertion: Assertion, source: str, destination: str) -> TraceResult:
    """One search, with the assertion's own ceilings applied."""
    return trace(
        context.inventory,
        source,
        destination,
        vlan=assertion.vlan,
        max_hops=assertion.max_hops or context.max_hops,
    )


def _wanted_layer(assertion: Assertion) -> Layer | None:
    """The layer the assertion insists on, or ``None`` for "whichever answers"."""
    if assertion.layer is None or assertion.layer is AssertionLayer.ANY:
        return None
    return Layer.L2 if assertion.layer is AssertionLayer.L2 else Layer.L3


def _why_not(result: TraceResult) -> str:
    """Why no route was found, in the words the trace engine already found."""
    reasons = [note for note in result.notes if note]
    frontier = ", ".join(
        f"{item.layer.value} reached {_plural(item.reached, 'element')}"
        for item in result.frontiers
        if item.reached
    )
    if frontier:
        reasons.append(f"the search got as far as: {frontier}")
    return "no path; " + ("; ".join(reasons) if reasons else "nothing adjacent was found")


def _render_path(path: TracedPath) -> str:
    """A traced route as ``a -> b -> c``, short names only."""
    return " -> ".join(short_name(waypoint.element) for waypoint in path.waypoints)


def _involved(pairs: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """Every endpoint named by the pairs, deduplicated, in first-seen order."""
    seen: dict[str, None] = {}
    for source, destination in pairs:
        seen.setdefault(source, None)
        seen.setdefault(destination, None)
    return tuple(seen)


# -- selector assertions -----------------------------------------------------


def _selected(context: _Context, assertion: Assertion) -> tuple[str, ...]:
    """The elements the assertion is about, from ``select``, ``query`` or both.

    One implementation of *selects*: whichever key was written, the answer comes
    out of the same :func:`~netgraph.testing.selectors.select_nodes` the renderer
    filters with. Written together they are ANDed, which is how "the switches,
    and of those the ones with no uplink" is said without one long query.
    """
    assert assertion.select is not None or assertion.query is not None  # required by the model
    spec = parse_selector(assertion.select) if assertion.select is not None else FilterSpec()
    return select_nodes(context.graph(), query_spec(assertion.query, spec))


def _require_selection(context: _Context, assertion: Assertion) -> tuple[tuple[str, ...], str]:
    """The selection, and the message to fail with when it is empty.

    An empty selection is a failure everywhere except ``count``: an assertion
    graded against nothing is an assertion that reports green having checked
    nothing, and that is the failure mode a test suite exists to prevent.
    """
    selected = _selected(context, assertion)
    if selected:
        return selected, ""
    return (), f"the selector {_subject(assertion)!r} matches no element, so nothing was checked"


def _subject(assertion: Assertion) -> str:
    """How a message names what the assertion was about: both keys, when both."""
    written = [one for one in (assertion.select, assertion.query) if one is not None]
    return " and ".join(written)


def _same_vlan(context: _Context, assertion: Assertion) -> _Outcome:
    """``same-vlan`` — every selected element shares a VLAN with every other."""
    selected, empty = _require_selection(context, assertion)
    if empty:
        return _Outcome(FAILED, empty)
    membership = {fqn: _vlans(context, fqn) for fqn in selected}
    if assertion.vlan is not None:
        missing = [fqn for fqn, vlans in membership.items() if assertion.vlan not in vlans]
        if not missing:
            return _Outcome(PASSED, elements=selected)
        return _Outcome(
            FAILED,
            f"{_plural(len(missing), 'element', len(selected))} "
            f"{_verb(len(missing), 'is', 'are')} not in VLAN {assertion.vlan}",
            detail=_capped([f"{fqn}: {_vlan_list(membership[fqn])}" for fqn in missing]),
            elements=selected,
        )
    common = set.intersection(*(set(vlans) for vlans in membership.values()))
    if common:
        return _Outcome(PASSED, elements=selected)
    return _Outcome(
        FAILED,
        f"the {len(selected)} selected elements share no VLAN",
        detail=_capped([f"{fqn}: {_vlan_list(membership[fqn])}" for fqn in selected]),
        elements=selected,
    )


def _distinct_vlan(context: _Context, assertion: Assertion) -> _Outcome:
    """``distinct-vlan`` — no two selected elements share a VLAN."""
    selected, empty = _require_selection(context, assertion)
    if empty:
        return _Outcome(FAILED, empty)
    membership = {fqn: _vlans(context, fqn) for fqn in selected}
    clashes = [
        f"{left} and {right} share VLAN {_vlan_list(membership[left] & membership[right])}"
        for index, left in enumerate(selected)
        for right in selected[index + 1 :]
        if membership[left] & membership[right]
    ]
    if not clashes:
        return _Outcome(PASSED, elements=selected)
    return _Outcome(
        FAILED,
        f"{_plural(len(clashes), 'pair')} of selected elements "
        f"{_verb(len(clashes), 'shares', 'share')} a VLAN",
        detail=_capped(clashes),
        elements=selected,
    )


def _vlans(context: _Context, fqn: str) -> frozenset[int]:
    node = context.node(fqn)
    return frozenset() if node is None else node.vlans


def _vlan_list(vlans: frozenset[int]) -> str:
    return ",".join(str(vlan) for vlan in sorted(vlans)) if vlans else "no VLAN"


def _within_prefix(context: _Context, assertion: Assertion) -> _Outcome:
    """``within-prefix`` — every routable address of the same family is inside it."""
    selected, empty = _require_selection(context, assertion)
    if empty:
        return _Outcome(FAILED, empty)
    assert assertion.prefix is not None  # required by the model
    try:
        network = ipaddress.ip_network(assertion.prefix, strict=False)
    except ValueError as exc:
        return _Outcome(FAILED, f"{assertion.prefix!r} is not a prefix: {exc}")

    outside: list[str] = []
    considered = 0
    for fqn in selected:
        for port, address in _addresses(context, fqn):
            if address.version != network.version:
                continue
            considered += 1
            if address not in network:
                outside.append(f"{fqn}:{port} has {address}")
    if not considered:
        return _Outcome(
            FAILED,
            f"no selected element carries a routable IPv{network.version} address, so "
            f"nothing was checked against {network}",
            elements=selected,
        )
    if not outside:
        return _Outcome(PASSED, elements=selected)
    return _Outcome(
        FAILED,
        f"{_plural(len(outside), 'address', considered, plural='addresses')} is outside {network}",
        detail=_capped(outside),
        elements=selected,
    )


def _addresses(
    context: _Context, fqn: str
) -> Iterator[tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    """Every routable address the graph resolved on ``fqn``, with its port."""
    node = context.node(fqn)
    if node is None:  # pragma: no cover - every selected fqn came from the graph
        return
    for port in node.ports:
        for text in port.routable_addresses:
            yield port.name, ipaddress.ip_interface(text).ip


def _has_interface(context: _Context, assertion: Assertion) -> _Outcome:
    """``has-interface`` — every selected element declares a matching port."""
    selected, empty = _require_selection(context, assertion)
    if empty:
        return _Outcome(FAILED, empty)
    assert assertion.interface is not None  # required by the model
    pattern = assertion.interface
    missing: list[str] = []
    for fqn in selected:
        node = context.node(fqn)
        ports = () if node is None else tuple(port.name for port in node.ports)
        if not any(fnmatch.fnmatchcase(name, pattern) for name in ports):
            missing.append(f"{fqn} has {_port_list(ports)}")
    if not missing:
        return _Outcome(PASSED, elements=selected)
    return _Outcome(
        FAILED,
        f"{_plural(len(missing), 'element', len(selected))} has no interface matching {pattern!r}",
        detail=_capped(missing),
        elements=selected,
    )


def _port_count_at_least(context: _Context, assertion: Assertion) -> _Outcome:
    """``port-count-at-least`` — every selected element has enough interfaces."""
    selected, empty = _require_selection(context, assertion)
    if empty:
        return _Outcome(FAILED, empty)
    assert assertion.ports is not None  # required by the model
    short: list[str] = []
    for fqn in selected:
        node = context.node(fqn)
        count = 0 if node is None else len(node.ports)
        if count < assertion.ports:
            short.append(f"{fqn} declares {count}")
    if not short:
        return _Outcome(PASSED, elements=selected)
    return _Outcome(
        FAILED,
        f"{_plural(len(short), 'element', len(selected))} "
        f"{_verb(len(short), 'has', 'have')} fewer than {assertion.ports} interfaces",
        detail=_capped(short),
        elements=selected,
    )


def _port_list(ports: Sequence[str]) -> str:
    if not ports:
        return "no interfaces"
    shown = ", ".join(ports[:MAX_REPORTED])
    return shown + (f", and {len(ports) - MAX_REPORTED} more" if len(ports) > MAX_REPORTED else "")


def _unique(context: _Context, assertion: Assertion) -> _Outcome:
    """``unique`` — no two selected elements produce the same value."""
    selected, empty = _require_selection(context, assertion)
    if empty:
        return _Outcome(FAILED, empty)
    assert assertion.field is not None  # required by the model

    owners: dict[str, list[str]] = {}
    producers = 0
    for fqn in selected:
        element = context.inventory.elements.get(fqn)
        if element is None:  # pragma: no cover - every selected fqn is an element
            continue
        # ``exclude_none`` matters: a field nobody set is *absent*, not a value.
        # Without it every device without a MAC would collide with every other
        # on ``null`` and 'unique' would report a duplicate that is not one.
        document: dict[str, Any] = element.model_dump(mode="json", by_alias=True, exclude_none=True)
        values = evaluate(document, assertion.field)
        if values:
            producers += 1
        for value in values:
            owners.setdefault(render_value(value), []).append(fqn)

    if not producers:
        return _Outcome(
            FAILED,
            f"no selected element has a value at {assertion.field!r}, so nothing was checked",
            elements=selected,
        )
    duplicated = {value: holders for value, holders in owners.items() if len(holders) > 1}
    if not duplicated:
        return _Outcome(PASSED, elements=selected)
    return _Outcome(
        FAILED,
        f"{_plural(len(duplicated), 'value')} of {assertion.field!r} "
        f"{_verb(len(duplicated), 'is', 'are')} used more than once",
        detail=_capped(
            [
                f"{value} is used by {', '.join(sorted(set(holders)))}"
                for value, holders in sorted(duplicated.items())
            ]
        ),
        elements=selected,
    )


def _count(context: _Context, assertion: Assertion) -> _Outcome:
    """``count`` — how many elements the selector matches, against its bounds."""
    selected = _selected(context, assertion)
    found = len(selected)
    failures = [
        f"{found} is not {word} {bound}"
        for word, bound in (
            ("exactly", assertion.equals),
            ("at least", assertion.at_least),
            ("at most", assertion.at_most),
        )
        if bound is not None and not _compare(found, word, bound)
    ]
    if not failures:
        return _Outcome(PASSED, elements=selected)
    return _Outcome(
        FAILED,
        f"the selector {_subject(assertion)!r} matches {_plural(found, 'element')}: "
        + "; ".join(failures),
        detail=_capped(list(selected)),
        elements=selected,
    )


def _compare(found: int, word: str, bound: int) -> bool:
    if word == "exactly":
        return found == bound
    return found >= bound if word == "at least" else found <= bound


# -- single points of failure ------------------------------------------------


def _no_single_point_of_failure(context: _Context, assertion: Assertion) -> _Outcome:
    """``no-single-point-of-failure`` — nothing, alone, cuts an endpoint off."""
    wanted = _spof_layers(assertion)
    built = tuple(view for view in context.views(wanted) if view.graph.endpoints)
    if not built:
        return _Outcome(
            SKIPPED,
            f"this inventory has nothing in the {_listed(wanted)} view, so there was nothing "
            f"to look for a single point of failure in",
        )
    anchors = context.anchors()
    if not anchors:
        return _Outcome(
            SKIPPED,
            "no element is designated a gateway and no router is declared, so there is "
            "nothing for a failure to cut anything off from",
        )
    selected = (
        _selected(context, assertion)
        if assertion.select is not None or assertion.query is not None
        else None
    )
    spofs, _ = single_points(
        context.inventory,
        built,
        anchors,
        plan=context.plan(),
        limit=0,
        minimum=assertion.min_isolated or 1,
    )
    if selected is not None:
        chosen = set(selected)
        spofs = tuple(spof for spof in spofs if spof.id.partition("#")[0] in chosen)
    if not spofs:
        return _Outcome(PASSED, elements=tuple(selected or ()))
    return _Outcome(
        FAILED,
        f"{_plural(len(spofs), 'single point of failure', plural='single points of failure')}"
        f" in the {_listed(wanted)} {_verb(len(wanted), 'view', 'views')}",
        detail=_capped(
            [
                f"{spof.layer}: losing {spof.kind} {spof.id} isolates "
                f"{_plural(spof.isolated, 'endpoint')}"
                + (f" ({', '.join(spof.isolates[:5])})" if spof.isolates else "")
                for spof in spofs
            ]
        ),
        elements=tuple(dict.fromkeys(spof.id for spof in spofs)),
    )


def _spof_layers(assertion: Assertion) -> tuple[str, ...]:
    """Which views the assertion covers; ``any`` covers all four."""
    if assertion.layer is None or assertion.layer is AssertionLayer.ANY:
        return (*LAYERS, POWER)
    return (assertion.layer.value,)


def _listed(layers: Sequence[str]) -> str:
    return ", ".join(layers)


# -- shared formatting -------------------------------------------------------


def _plural(count: int, noun: str, total: int | None = None, *, plural: str = "") -> str:
    """``3 routes``, or ``3 routes of 12`` when only some of them are meant.

    The total goes *after* the noun rather than in front of it so that the noun
    agrees with ``count`` and a verb can follow: "1 route of 2 does not exist"
    reads, and "1 of 2 routes does not exist" does not.
    """
    word = noun if count == 1 else (plural or noun + "s")
    of = f" of {total}" if total is not None and total != count else ""
    return f"{count} {word}{of}"


def _verb(count: int, singular: str, plural: str) -> str:
    """The form of a verb agreeing with ``count``."""
    return singular if count == 1 else plural


def _capped(lines: Sequence[str]) -> tuple[str, ...]:
    """``lines``, truncated to :data:`MAX_REPORTED` with the remainder counted.

    Nothing is ever dropped silently: a report that showed ten of forty problems
    and said nothing about the other thirty would read as a list of ten.
    """
    if len(lines) <= MAX_REPORTED:
        return tuple(lines)
    return (*lines[:MAX_REPORTED], f"... and {len(lines) - MAX_REPORTED} more")


# -- the query assertion -----------------------------------------------------


def _query(context: _Context, assertion: Assertion) -> _Outcome:
    """``query`` — the selector language, graded against how much it matches.

    With no bound the claim is that it matches **nothing**, because that is how
    a network invariant is written: "no device is missing a management address"
    is a search for the counterexamples, and the counterexamples are the
    failure report. With a bound it is a ``count`` over an expression the
    ``select:`` vocabulary cannot express, which is the other half of why the
    language exists.
    """
    selected = _selected(context, assertion)
    found = len(selected)
    bounds = tuple(
        (word, bound)
        for word, bound in (
            ("exactly", assertion.equals),
            ("at least", assertion.at_least),
            ("at most", assertion.at_most),
        )
        if bound is not None
    )
    if not bounds:
        if not found:
            return _Outcome(PASSED)
        return _Outcome(
            FAILED,
            f"{_plural(found, 'element')} match {assertion.query!r}, and the assertion is "
            f"that none does",
            detail=_capped(list(selected)),
            elements=selected,
        )
    failures = [
        f"{found} is not {word} {bound}"
        for word, bound in bounds
        if not _compare(found, word, bound)
    ]
    if not failures:
        return _Outcome(PASSED, elements=selected)
    return _Outcome(
        FAILED,
        f"{assertion.query!r} matches {_plural(found, 'element')}: " + "; ".join(failures),
        detail=_capped(list(selected)),
        elements=selected,
    )


#: One evaluator per assertion, so adding a claim is adding a function and a row.
_EVALUATORS: Final[dict[AssertionType, Callable[[_Context, Assertion], _Outcome]]] = {
    AssertionType.REACHABLE: _reachable,
    AssertionType.NOT_REACHABLE: _not_reachable,
    AssertionType.PATH_SHORTER_THAN: _path_shorter_than,
    AssertionType.SAME_VLAN: _same_vlan,
    AssertionType.DISTINCT_VLAN: _distinct_vlan,
    AssertionType.WITHIN_PREFIX: _within_prefix,
    AssertionType.HAS_INTERFACE: _has_interface,
    AssertionType.PORT_COUNT_AT_LEAST: _port_count_at_least,
    AssertionType.UNIQUE: _unique,
    AssertionType.COUNT: _count,
    AssertionType.NO_SINGLE_POINT_OF_FAILURE: _no_single_point_of_failure,
    AssertionType.QUERY: _query,
}

assert set(_EVALUATORS) == set(AssertionType), "every assertion needs an evaluator"
