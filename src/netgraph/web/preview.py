"""One ``parse → validate → render`` pass, over a stream or over a loaded tree.

This is the whole of what ``netgraph web`` does per keystroke-burst, and it is
the same pipeline the command line runs, in the same order, with two
differences that follow from being interactive:

* **A rejected inventory is still drawn.** ``netgraph render`` refuses one
  without ``--force``, because a diagram that disagrees with the files
  misinforms whoever it is shown to. Here the diagram *is* the feedback: text
  being edited is wrong most of the time, and blanking the picture on every
  half-typed line would make the tool useless. So every problem is reported —
  prominently, with its line — and whatever resolved is drawn anyway.
* **The picture comes back as a fragment, not a file.**
  :func:`~netgraph.web.svgdoc.prepare` strips everything that could execute or
  navigate, so the diagram can be put straight into the live page.

Two entry points, one body. :func:`render_source` takes the document stream the
scratchpad edits; :func:`render_inventory` takes a tree
:class:`~netgraph.web.session.EditingSession` has already loaded, so an editing
session draws its files without parsing them a second time. Everything after
the load is identical, which is what keeps the two faces of the command showing
the same diagram.

The result carries a :class:`~netgraph.watch.pipeline.Status`, and a status
that is not ``ok`` is the front end's cue to say so, not to hide the diagram.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from netgraph.config import ValidationConfig
from netgraph.diff import Drawing, draw
from netgraph.errors import NetgraphError
from netgraph.layout.geometry import Geometry
from netgraph.loader import Inventory, load_stream
from netgraph.plan import Plan
from netgraph.query import QueryError
from netgraph.query import parse as parse_query
from netgraph.query.apply import narrow as narrow_graph
from netgraph.render import (
    DETAIL_OPTIONS,
    FilterSpec,
    Graph,
    IconTheme,
    Layer,
    RenderOptions,
    build_details,
    build_graph,
)
from netgraph.render.aggregate import (
    AGGREGATE_ID_PREFIX,
    AggregateSpec,
    collapse_namespaces,
    collapse_targets,
)
from netgraph.render.dot import cluster_keys, to_dot, to_image
from netgraph.render.ids import element_ids
from netgraph.render.jsonexport import annotations_payload
from netgraph.render.routes import RouteCache, anchors_of, default_routing, fans_of, route_plan
from netgraph.render.styles import StyleMap
from netgraph.render.theme import Theme
from netgraph.rules import Severity
from netgraph.validate import Finding
from netgraph.validate import validate as run_validation
from netgraph.watch.pipeline import Problem, Status, flatten_problems
from netgraph.web.svgdoc import prepare

__all__ = [
    "MAX_PROBLEMS",
    "MAX_VLAN",
    "Preview",
    "ViewOptions",
    "clip_problems",
    "graph_digest",
    "render_diff",
    "render_inventory",
    "render_source",
]

#: The highest VLAN id ``--vlan`` and the browser may ask for (§9.1).
MAX_VLAN: Final = 4094

#: How many problems one answer carries to the browser.
#:
#: Not a cap on what was *found* — every answer reports the true totals beside
#: the list, and ``netgraph validate`` prints every one — but a cap on what is
#: serialised, sent and turned into DOM rows on each of the four answers an edit
#: produces. On the thousand-device benchmark tree the validator reports 2 101
#: findings, almost all of them one informational rule; that was 538 kB on every
#: answer and 2 101 rows rebuilt in the page each time, for a list nobody can
#: read past the first screen of.
#:
#: The kept ones are the most severe, because :func:`~netgraph.watch.pipeline.
#: flatten_problems` has already sorted them that way: an inventory with three
#: errors and two thousand notes sends the three errors first, which is the
#: order somebody reads them in anyway.
MAX_PROBLEMS: Final = 200

#: How many namespaces one request may ask to have folded. A collapse is a
#: triangle somebody clicked on a container header, and a page that has folded
#: five hundred of them is not a page anybody is reading — so the cap is a
#: bound on a malformed request rather than on a gesture.
MAX_COLLAPSED: Final = 500

#: The deepest namespace level drawn as a container frame. A namespace may nest
#: as far as the folders do; the *editor* draws a frame per level, and past this
#: the frames are thinner than their own captions. Everything deeper is still
#: inside its ancestors' frames and is still dropped into by name — only the
#: box for it is not drawn.
MAX_CONTAINER_DEPTH: Final = 8

#: What the browser is allowed to ask for, mapped to the field it sets. Keeping
#: this closed is what stops a request from reaching a rendering knob the web
#: interface does not offer.
_BOOLEAN_FIELDS: Final[tuple[str, ...]] = (
    "show_ips",
    "show_vlans",
    "group_by_namespace",
    "annotations",
    "strict",
)


class RequestError(NetgraphError):
    """Raised when a rendering request is not one this interface can honour.

    Distinct from a *problem with the inventory*: this is the front end asking
    for something impossible, which is a 400 rather than something to draw.
    """

    exit_code = 2


@dataclass(frozen=True, slots=True)
class ViewOptions:
    """Which graph to build from a stream, and how much of it to draw.

    The fields mirror the options ``netgraph render`` takes, minus the ones
    that only make sense against a folder on disk.
    """

    layer: Layer = Layer.L1
    show_ips: bool = True
    show_vlans: bool = True
    group_by_namespace: bool = False
    #: Draw the notes, areas and legends of §21. A per-view toggle rather than a
    #: setting, because commentary is exactly the layer somebody wants off while
    #: reading the topology and on while explaining it — and because it changes
    #: no fact, turning it off is never a different answer, only a plainer
    #: picture.
    annotations: bool = True
    #: Namespaces drawn as one node instead of as a box full of them, exactly as
    #: ``netgraph render --collapse`` folds them
    #: (:func:`~netgraph.render.aggregate.collapse_namespaces`). The browser's,
    #: not the command line's: collapsing a container is a *view* gesture — a
    #: triangle on its header — and writes nothing, so it belongs beside the
    #: layer and the VLAN filter rather than in a document.
    collapse: tuple[str, ...] = ()
    #: Keep only elements participating in these VLANs; empty keeps everything.
    vlans: frozenset[int] = frozenset()
    #: Keep only these element kinds; empty keeps everything.
    kinds: tuple[str, ...] = ()
    #: A selector query (:mod:`netgraph.query`) the drawing is narrowed to, or
    #: ``None``. The browser's, like ``collapse``: the search box can *filter*
    #: as well as highlight, and filtering is the same narrowing ``render
    #: --select`` does — so it goes through the same
    #: :class:`~netgraph.render.graph.FilterSpec` rather than the browser
    #: hiding shapes it was sent.
    select: str | None = None
    title: str | None = None
    #: Promote surviving warnings to errors, as ``--strict`` does elsewhere.
    strict: bool = False
    #: Icon theme, chosen once on the command line rather than by the browser:
    #: it names a directory, and a request must not be able to.
    icons: IconTheme | None = field(default=None, compare=False)
    #: The stylesheet in force (§22), chosen on the command line for the same
    #: reason as :attr:`icons`: it names a file on this machine.
    theme: Theme | None = field(default=None, compare=False)
    #: Honour the styles the inventory and the theme declare. Unlike the
    #: theme itself this *is* the browser's to set — it names nothing and
    #: changes nothing on disk, and "show me the plain diagram" is exactly
    #: the kind of question somebody asks while reading one.
    styling: bool = True

    @classmethod
    def from_request(
        cls,
        payload: Mapping[str, Any],
        *,
        icons: IconTheme | None = None,
        theme: Theme | None = None,
    ) -> ViewOptions:
        """Build options from a decoded JSON request body.

        Every value is checked here rather than trusted: the body arrives from
        a browser, which may be showing a page this server did not write.

        Raises:
            RequestError: A field is of the wrong type or out of range.
        """
        values: dict[str, Any] = {"icons": icons, "theme": theme}
        for name in _BOOLEAN_FIELDS:
            if name in payload:
                values[name] = _boolean(payload[name], name)
        if "layer" in payload:
            values["layer"] = _layer(payload["layer"])
        if "vlans" in payload:
            values["vlans"] = _vlans(payload["vlans"])
        if "kinds" in payload:
            values["kinds"] = _strings(payload["kinds"], "kinds")
        if payload.get("select"):
            values["select"] = _query(payload["select"])
        if "collapse" in payload:
            values["collapse"] = _namespaces(payload["collapse"])
        if payload.get("title"):
            values["title"] = _text(payload["title"], "title")
        return cls(**values)

    @classmethod
    def from_query(
        cls,
        query: Mapping[str, Sequence[str]],
        *,
        icons: IconTheme | None = None,
        theme: Theme | None = None,
    ) -> ViewOptions:
        """Build options from a parsed query string.

        ``GET /api/graph?view=l2&vlans=10,20`` is the shape a browser can put in
        an address bar and a reader can type by hand, so the editing session
        takes its view that way rather than by posting a body. Every value goes
        through the same checks :meth:`from_request` applies — a query string is
        no more trustworthy for coming from a URL.

        Raises:
            RequestError: A parameter is malformed or out of range.
        """
        payload: dict[str, Any] = {}
        for name, key in (
            ("view", "layer"),
            ("layer", "layer"),
            ("title", "title"),
        ):
            if query.get(name):
                payload[key] = query[name][-1]
        for name in _BOOLEAN_FIELDS:
            # ``show_ips`` on the wire, ``show-ips`` for a person typing it.
            for spelling in (name, name.replace("_", "-")):
                if query.get(spelling):
                    payload[name] = _flag(query[spelling][-1], name)
        if query.get("vlans"):
            payload["vlans"] = _split_ids(query["vlans"][-1])
        if query.get("kinds"):
            payload["kinds"] = [
                item for value in query["kinds"] for item in value.split(",") if item
            ]
        if query.get("collapse"):
            payload["collapse"] = [
                item for value in query["collapse"] for item in value.split(",") if item
            ]
        if query.get("select"):
            payload["select"] = query["select"][-1]
        return cls.from_request(payload, icons=icons, theme=theme)

    @property
    def filter_spec(self) -> FilterSpec:
        return FilterSpec(vlans=self.vlans, kinds=self.kinds, select=self.select)

    @property
    def render_options(self) -> RenderOptions:
        """How to draw the diagram the page embeds.

        ``element_ids`` is always on: the ids are how the page maps the shape
        under the cursor onto its info-box record. Graphviz's own tooltips are
        always off, because that record says strictly more and the browser
        would otherwise pop a second, thinner one over it a moment later
        (:func:`netgraph.web.svgdoc.prepare` strips any that survive).
        """
        return RenderOptions(
            show_ips=self.show_ips,
            show_vlans=self.show_vlans,
            group_by_namespace=self.group_by_namespace,
            annotations=self.annotations,
            title=self.title,
            icons=self.icons,
            theme=self.theme,
            styling=self.styling,
            tooltips=False,
            element_ids=True,
        )


@dataclass(frozen=True, slots=True)
class Preview:
    """The outcome of one pass, ready to be serialised to the browser."""

    status: Status
    #: One-line summary: the size of the graph, or why there is none.
    message: str
    #: The diagram as an embeddable ``<svg>`` fragment, or ``None`` when this
    #: pass produced no picture at all.
    svg: str | None = None
    #: Info-box records keyed by SVG element id; see :mod:`netgraph.render.details`.
    details: Mapping[str, Any] = field(default_factory=dict)
    problems: tuple[Problem, ...] = ()
    nodes: int = 0
    edges: int = 0
    #: Cables the graph builder had to drop, with the reason.
    dangling: tuple[str, ...] = ()
    #: The stored arrangement this pass drew from, in the form
    #: ``netgraph render -f json`` exports it, or ``None`` when the inventory
    #: stores none. A canvas that lets somebody drag a node needs to know where
    #: netgraph thinks that node is, and whether the position is stored or was
    #: invented by the engine; see :mod:`netgraph.layout.geometry`.
    geometry: Mapping[str, Any] | None = None
    #: The notes, areas and legends this pass drew (§21), in the form
    #: ``netgraph render -f json`` publishes them, or ``None`` when the view
    #: declares none or they are turned off. Beside :attr:`geometry` and for the
    #: same reason: a canvas that lets somebody drag a note has to know which
    #: document is behind the box under the pointer, where that box is pinned,
    #: and what it says — none of which can be read off the SVG, because an area
    #: in an arranged drawing is a rectangle in the background with no id at all.
    annotations: Mapping[str, Any] | None = None
    #: One entry per namespace the editor may draw a container frame around, in
    #: namespace order; see :func:`_containers`. Empty for a drawing with no
    #: namespaces, which is the single-folder inventory most people start with.
    #:
    #: Beside :attr:`geometry` for the same reason it is: the boundary of a
    #: namespace box is an editing surface — things are dropped into it, it is
    #: folded and unfolded, and its rectangle is written to a document — and
    #: none of what that needs can be read off the SVG, where a namespace is a
    #: rounded path with a caption and no members.
    containers: Sequence[Mapping[str, Any]] = ()
    #: The resolved appearance of everything this pass drew (§22), keyed by
    #: address: ``{"nodes": {fqn: style}, "edges": {id: style}}``, each style
    #: in the form ``netgraph render -f json`` publishes it, provenance
    #: included. ``None`` when the pass produced no picture.
    #:
    #: The style inspector needs all three of what the value *is*, which rung
    #: it came from, and what it would fall back to — none of which can be
    #: read off the SVG, where every rung has already collapsed into one hex
    #: literal on one attribute.
    styles: Mapping[str, Any] | None = None
    #: Wall-clock duration of the pass, in seconds.
    duration: float = 0.0
    #: What changed between two states, when this pass drew a *diff* — the marks
    #: keyed by node and edge id, exactly as ``netgraph diff -f json`` publishes
    #: them, with the changeset under ``changeset``. ``None`` for the ordinary
    #: single-state rendering, which is every pass but the changes drawer's.
    diff: Mapping[str, Any] | None = None
    #: Fingerprint of the picture this pass would draw; see :func:`graph_digest`.
    #: ``None`` when the pass never got as far as building a graph.
    graph_hash: str | None = None
    #: Set when the caller said it already held :attr:`graph_hash` and this pass
    #: therefore stopped before Graphviz. There is no :attr:`svg` and no
    #: :attr:`details` in that case — the client has them — but the problems and
    #: the counts are this revision's, because those can move while the drawing
    #: does not.
    unchanged: bool = False

    @property
    def error_count(self) -> int:
        return sum(1 for problem in self.problems if problem.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for problem in self.problems if problem.severity is Severity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        """The JSON the page consumes. Keys are only ever added, never renamed."""
        shown, omitted = clip_problems(self.problems)
        return {
            "status": str(self.status),
            "message": self.message,
            "svg": self.svg,
            "details": dict(self.details),
            "problems": [
                {
                    "severity": str(problem.severity),
                    "location": problem.location,
                    "rule": problem.rule,
                    "message": problem.message,
                    **(
                        {"fixes": [{"key": key, "title": title} for key, title in problem.fixes]}
                        if problem.fixes
                        else {}
                    ),
                }
                for problem in shown
            ],
            "problemsOmitted": omitted,
            "dangling": list(self.dangling),
            "geometry": dict(self.geometry) if self.geometry is not None else None,
            "annotations": dict(self.annotations) if self.annotations is not None else None,
            "containers": [dict(entry) for entry in self.containers],
            "styles": dict(self.styles) if self.styles is not None else None,
            "counts": {
                "nodes": self.nodes,
                "edges": self.edges,
                "errors": self.error_count,
                "warnings": self.warning_count,
            },
            "durationMs": round(self.duration * 1000, 1),
            "diff": dict(self.diff) if self.diff is not None else None,
            "graphHash": self.graph_hash,
            "unchanged": self.unchanged,
        }


def clip_problems(
    problems: Sequence[Problem], *, limit: int = MAX_PROBLEMS
) -> tuple[Sequence[Problem], int]:
    """The problems to send, and how many were left out. See :data:`MAX_PROBLEMS`.

    The count is the honest half: an answer that quietly stopped at two hundred
    would read as an inventory with two hundred problems, which is a worse lie
    than a slow page.
    """
    if len(problems) <= limit:
        return problems, 0
    return problems[:limit], len(problems) - limit


def graph_digest(graph: Graph, options: RenderOptions) -> str:
    """A fingerprint of the picture ``graph`` and ``options`` would produce.

    The DOT document, hashed. It is the exact input Graphviz is given, so two
    passes that agree here cannot disagree on the drawing — which is what lets a
    client that already holds the SVG be told "nothing moved" instead of being
    sent a re-render of the same picture.

    Structural rather than incidental: the tree can move for a change that alters
    no drawn layer at all — a description edited, a device added to a namespace
    the current view filters out, a comment reflowed — and on a large inventory
    the Graphviz run that produces the identical SVG is the single most expensive
    thing an edit triggers.

    Computing it costs one DOT serialisation, which :func:`render_inventory` then
    repeats inside :func:`~netgraph.render.dot.to_image` when the picture *has*
    moved. That is a few milliseconds against Graphviz's hundreds, and buying it
    back would mean threading the source through a layer whose job is to lay
    out a graph, not to cache one.
    """
    return hashlib.sha256(to_dot(graph, options, target="svg").encode("utf-8")).hexdigest()


def render_source(source: str, view: ViewOptions | None = None) -> Preview:
    """Parse, validate and draw ``source``.

    Never raises for anything the *text* can be wrong about: a syntax error, a
    dangling cable and a filter that matches nothing all come back as a
    :class:`Preview` whose problems say so. A missing Graphviz — the one
    failure the text cannot cause — comes back as
    :attr:`~netgraph.watch.pipeline.Status.FAILED` with the installation hint
    the renderer produces.

    Args:
        source: The YAML document stream, ``---`` separators included.
        view: Which graph to build and how to draw it.

    Returns:
        The diagram, its info-box records, and every problem found on the way.
    """
    started = time.monotonic()
    return render_inventory(load_stream(source), view, started=started)


def render_inventory(
    inventory: Inventory,
    view: ViewOptions | None = None,
    *,
    settings: ValidationConfig | None = None,
    started: float | None = None,
    known: str | None = None,
    findings: Sequence[Finding] | None = None,
    fixes: Mapping[tuple[str, str], Sequence[tuple[str, str]]] | None = None,
    routes: RouteCache | None = None,
) -> Preview:
    """Validate and draw an inventory somebody else has already loaded.

    The half of :func:`render_source` that does not care where the documents
    came from, so an editing session over a folder
    (:mod:`netgraph.web.session`) draws its tree without re-parsing it and
    without either face of ``netgraph web`` growing its own renderer.

    Args:
        inventory: A loaded tree or stream, errors and all.
        view: Which graph to build and how to draw it.
        settings: Validation settings, normally the tree's own ``netgraph.toml``
            with the page's ``strict`` toggle folded in. Defaults to netgraph's
            built-ins under that toggle, which is what a stream — having no
            folder to look in — has to use.
        started: When the pass began, for the duration this reports. Supplied by
            a caller that did work before calling.
        known: A :func:`graph_digest` the caller already holds the drawing for.
            When this pass would produce the same one, it stops before Graphviz
            and answers with :attr:`Preview.unchanged`.
        findings: What ``validate`` said about this inventory under these
            settings, when the caller has already asked. An editing session has:
            the write path validates the tree to decide whether to write it, and
            the page then asks for the diagram of the tree that was written. Two
            passes of the validator over the same objects answer the same thing,
            and on a thousand-device tree each one is a tenth of a second, so the
            caller that already has the answer hands it over. ``None`` — every
            other caller — validates here.
        fixes: The mechanical repairs on offer, keyed as
            :func:`~netgraph.watch.pipeline.flatten_problems` keys them. Passed
            for the same reason the problems are reported at all: the page draws
            one list, fed from *both* this answer and the file list's, and a
            **Fix** button that appeared or vanished depending on which of the
            two landed second would be a race the user could see.
        routes: A :class:`~netgraph.render.routes.RouteCache` to draw orthogonal
            links through, normally the editing session's. Dragging one device
            changes the obstacle set for every link in the drawing, and
            re-searching all of them because of it is what would put a full
            render inside a drag; the cache re-searches only the links whose
            line the move actually broke. ``None`` — every other caller —
            routes from cold, which is what a command-line render must do to be
            reproducible.

    Returns:
        The diagram, its info-box records, and every problem found on the way.
    """
    options = view or ViewOptions()
    started = time.monotonic() if started is None else started

    if findings is None:
        findings = run_validation(
            inventory, settings if settings is not None else ValidationConfig(strict=options.strict)
        )
    problems = flatten_problems(inventory.errors, findings, fixes=fixes)
    rejected = bool(inventory.errors) or any(finding.severity.is_fatal for finding in findings)

    try:
        whole = narrow_graph(build_graph(inventory, layer=options.layer), options.filter_spec)
        # Folding is applied *after* the filters and before anything is drawn or
        # fingerprinted, so a folded container is a different picture and gets a
        # different hash. ``whole`` is kept: the container payload is built from
        # the unfolded drawing, because a folded namespace still has to report
        # what it stands for and how to unfold it.
        folded = collapse_targets(whole, AggregateSpec(collapse=options.collapse))
        graph = collapse_namespaces(whole, folded) if folded else whole
        digest = graph_digest(graph, options.render_options)
        status = Status.INVALID if rejected else Status.OK
        message = _summary(inventory, graph, rejected=rejected)
        if known is not None and known == digest:
            return Preview(
                status=status,
                message=message,
                problems=problems,
                nodes=len(graph.nodes),
                edges=len(graph.edges),
                dangling=tuple(graph.dangling),
                duration=time.monotonic() - started,
                graph_hash=digest,
                unchanged=True,
            )
        payload = to_image(graph, options.render_options, format="svg")
        svg = prepare(payload)
    except (NetgraphError, OSError) as exc:
        return Preview(
            status=Status.FAILED,
            message=_describe(exc),
            problems=problems,
            duration=time.monotonic() - started,
        )

    return Preview(
        status=status,
        message=message,
        svg=svg,
        details=build_details(graph, DETAIL_OPTIONS),
        problems=problems,
        nodes=len(graph.nodes),
        edges=len(graph.edges),
        dangling=tuple(graph.dangling),
        geometry=_geometry(graph, options.render_options, cache=routes),
        annotations=annotations_payload(graph, options.render_options),
        containers=_containers(whole, graph, options, folded),
        styles=_styles(graph, options.render_options),
        duration=time.monotonic() - started,
        graph_hash=digest,
    )


def _containers(
    graph: Graph, drawn: Graph, options: ViewOptions, collapsed: Sequence[str]
) -> list[dict[str, Any]]:
    """One entry per namespace the editor can draw a frame around (§2).

    The container layer is the editor's, not Graphviz's, and this is why. A
    ``--group-by-namespace`` render boxes the namespaces that hold elements
    *directly* — Graphviz has no reason to draw ``sites`` around three sites
    that each have their own box — but the editing gesture needs every level:
    dragging a switch onto ``sites/south`` is a legal move whether or not
    ``sites/south`` has a device of its own in it. So every level from the root
    down is published, each with the members whose hull the frame follows, and
    the browser draws the ones it can fit.

    ``boxed`` is the difference that matters for writing. A namespace Graphviz
    boxes is one :func:`~netgraph.render.dot.cluster_keys` names, and only such
    a namespace has somewhere for a resize to *go*: its rectangle is stored
    under that key in the layout document's ``groups`` and drawn back from it
    (:func:`netgraph.render.dot._frames`). A level in between is drawn round
    whatever is under it and has no stored box, exactly as an annotation area
    following its members does — so it is a drop target and a collapse target,
    but not a resize target, and the page says so rather than writing a number
    the next render would ignore.

    Computed from ``graph`` — the drawing *before* any collapsing — so that a
    folded container still reports what it stands for and can be unfolded. What
    it is drawn *as* comes from ``drawn``: a cluster id while it is open, and
    the aggregate node's id once it is folded.

    **Empty unless the drawing is grouped by namespace**, and that is the
    contract rather than an optimisation. A container frame promises that
    everything inside the rectangle is in that namespace; an ungrouped layout
    scatters a namespace's members across the page, so a frame round them would
    enclose half the diagram and dropping something into it would be a lie. The
    editor's whole container layer — the frames, the headers, the drag — is
    therefore off exactly when the drawing does not group.
    """
    if graph.is_empty or not options.group_by_namespace:
        return []
    boxed = set(cluster_keys(graph, options.render_options).values())
    folded = set(collapsed)
    identity, aggregates = element_ids(graph), element_ids(drawn)
    members: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        namespace = node.namespace
        if not namespace:
            continue
        for level in _levels(namespace):
            members.setdefault(level, []).append(node.fqn)
    entries: list[dict[str, Any]] = []
    for namespace in sorted(members):
        depth = namespace.count("/") + 1
        if depth > MAX_CONTAINER_DEPTH:
            continue
        inside = members[namespace]
        parent = namespace.rpartition("/")[0]
        is_folded = namespace in folded
        entries.append(
            {
                "namespace": namespace,
                "label": namespace.rpartition("/")[2],
                "parent": parent if parent in members else "",
                "depth": depth,
                "count": len(inside),
                "members": inside,
                "boxed": namespace in boxed,
                "collapsed": is_folded,
                # Which shape on the page *is* this container: the cluster
                # Graphviz drew round it, or — once folded — the single node it
                # became. Either way the browser measures the box off the
                # drawing rather than guessing it.
                "element": (
                    aggregates.node(f"{AGGREGATE_ID_PREFIX}{namespace}")
                    if is_folded
                    else identity.cluster(namespace)
                ),
                # Whether an *ancestor* is folded, in which case this container
                # is not on the page at all and must not be drawn or dropped on.
                "hidden": any(level in folded for level in _levels(parent)),
            }
        )
    return entries


def _levels(namespace: str) -> tuple[str, ...]:
    """``a/b/c`` as ``('a', 'a/b', 'a/b/c')``; the root contributes nothing."""
    parts = [part for part in namespace.split("/") if part]
    return tuple("/".join(parts[: index + 1]) for index in range(len(parts)))


def _styles(graph: Graph, options: RenderOptions) -> Mapping[str, Any]:
    """The resolved appearance of one drawing, keyed by address.

    Resolved a second time rather than read back out of the renderer: the
    resolution is a pure function of the graph and the options, and threading a
    :class:`~netgraph.render.styles.StyleMap` back out of ``to_image`` — which
    goes through Graphviz and returns bytes — would be a channel that exists
    only for this. The cost is one pass over the nodes and links, next to a
    subprocess.
    """
    resolved = StyleMap.build(
        graph, theme=options.theme, icons=options.icons, output="svg", styling=options.styling
    )
    return {
        "nodes": {fqn: style.to_dict() for fqn, style in resolved.nodes.items()},
        "edges": {
            edge.id: resolved.edge(index).to_dict() for index, edge in enumerate(graph.edges)
        },
        "theme": resolved.theme.name if resolved.theme is not None else None,
        "enabled": resolved.enabled,
    }


def render_diff(
    before: Inventory,
    after: Inventory,
    plan: Plan,
    view: ViewOptions | None = None,
    *,
    settings: ValidationConfig | None = None,
    started: float | None = None,
    known: str | None = None,
) -> Preview:
    """Draw ``after`` with what ``plan`` says changed since ``before`` painted on.

    The same pass :func:`render_inventory` makes, over the *union* of the two
    states rather than over one of them: added things green, removed things red
    and dashed but still placed, changed things amber. Nothing here decides what
    changed — :mod:`netgraph.diff` joins the changeset to the two drawings, and
    the same code answers ``netgraph diff`` on the command line.

    The problems reported are ``after``'s. A diff is shown *while editing*, and
    the problems a user needs are the ones in the tree they are editing, not the
    ones in the state they have left behind.

    Args:
        before: The state being compared against — the session's starting tree,
            or git HEAD.
        after: The tree as it is now.
        plan: The changeset between them, from :func:`netgraph.plan.diff`.
        view: Which graph to build and how to draw it.
        settings: Validation settings for ``after``.
        started: When the pass began, for the duration this reports.
        known: A :func:`graph_digest` the caller already holds the overlay for,
            as :func:`render_inventory` takes it. The fingerprint covers the
            overlay as well as the graph, so a diff whose *marks* moved is
            redrawn even when both states are otherwise unchanged.

    Returns:
        The diagram, its info-box records, every problem in ``after``, and the
        marks under :attr:`Preview.diff`.
    """
    options = view or ViewOptions()
    started = time.monotonic() if started is None else started

    findings = run_validation(
        after, settings if settings is not None else ValidationConfig(strict=options.strict)
    )
    problems = flatten_problems(after.errors, findings)
    rejected = bool(after.errors) or any(finding.severity.is_fatal for finding in findings)

    try:
        drawing = draw(
            plan,
            narrow_graph(build_graph(before, layer=options.layer), options.filter_spec),
            narrow_graph(build_graph(after, layer=options.layer), options.filter_spec),
        )
        graph = drawing.graph
        marked = replace(options.render_options, diff=drawing.overlay)
        digest = graph_digest(graph, marked)
        status = Status.INVALID if rejected else Status.OK
        message = _diff_summary(drawing, rejected=rejected)
        if known is not None and known == digest:
            return Preview(
                status=status,
                message=message,
                problems=problems,
                nodes=len(graph.nodes),
                edges=len(graph.edges),
                dangling=tuple(graph.dangling),
                duration=time.monotonic() - started,
                graph_hash=digest,
                unchanged=True,
                diff=drawing.overlay.to_dict() | {"changeset": plan.to_dict()},
            )
        payload = to_image(graph, marked, format="svg")
        svg = prepare(payload)
    except (NetgraphError, OSError) as exc:
        return Preview(
            status=Status.FAILED,
            message=_describe(exc),
            problems=problems,
            duration=time.monotonic() - started,
        )

    return Preview(
        status=status,
        message=message,
        svg=svg,
        details=build_details(graph, DETAIL_OPTIONS),
        problems=problems,
        nodes=len(graph.nodes),
        edges=len(graph.edges),
        dangling=tuple(graph.dangling),
        geometry=_geometry(graph, options.render_options),
        annotations=annotations_payload(graph, marked),
        duration=time.monotonic() - started,
        diff=drawing.overlay.to_dict() | {"changeset": plan.to_dict()},
        graph_hash=digest,
    )


def _diff_summary(drawing: Drawing, *, rejected: bool) -> str:
    """The one-line message over a diff: what moved, not how big the graph is."""
    if drawing.is_empty:
        return "nothing has changed yet; the whole diagram is drawn untouched"
    counted = drawing.overlay.summary()
    return f"{counted} (drawn despite the problems below)" if rejected else counted


def _geometry(
    graph: Graph, options: RenderOptions | None = None, *, cache: RouteCache | None = None
) -> dict[str, Any] | None:
    """The graph's stored arrangement, or ``None`` when it stores none.

    The same coordinate system, units and ``mode`` as
    ``netgraph render -f json`` — points, ``y`` upwards, a position being the
    centre of what it places — because the two describe the same arrangement and
    a client that learned one must not have to learn the other.

    ``links`` is the half the canvas needs and the command line does not: per
    link, what the inventory pins (the bends, the routing style, the label
    position) *and* the line netgraph drew from it. The canvas puts a grab
    handle on each bend and, while one is being dragged, redraws the line
    itself from the same inputs — see ``web/assets/links.js``, which mirrors
    :mod:`netgraph.layout.routing`. ``anchors`` is what it clips against, so
    that the decision "how big is this box" is made once here rather than twice
    in two languages.
    """
    geometry: Geometry = graph.geometry
    if geometry.is_empty:
        return None
    payload: dict[str, Any] = {
        "units": "points",
        "mode": str(geometry.mode(graph.nodes)),
        "routing": str(default_routing(graph, options)),
        "nodes": {
            key: {"x": placement.x, "y": placement.y}
            | (
                {"width": placement.width, "height": placement.height}
                if placement.width is not None and placement.height is not None
                else {}
            )
            for key, placement in sorted(geometry.nodes.items())
        },
    }
    if geometry.groups:
        payload["groups"] = {
            key: {"x": box.x, "y": box.y, "width": box.width, "height": box.height}
            for key, box in sorted(geometry.groups.items())
        }
    links = _links(graph, options, cache=cache)
    if links:
        payload["links"] = links
        payload["anchors"] = {
            fqn: {"x": anchor.x, "y": anchor.y, "width": anchor.width, "height": anchor.height}
            for fqn, anchor in sorted(anchors_of(graph).items())
        }
    return payload


def _links(
    graph: Graph, options: RenderOptions | None, *, cache: RouteCache | None = None
) -> dict[str, Any]:
    """Per link: what is pinned, what was drawn, and which nodes it joins.

    Only for a drawing every node of which is placed. Anywhere else Graphviz is
    routing, there is no line for the canvas to put handles on, and offering one
    would let somebody drag a bend that the next render would throw away.

    ``waypoints`` and ``routed`` are the two halves of where a line goes, and
    the canvas must not confuse them. ``waypoints`` is what the *inventory*
    says: the bends a person dragged, each of which gets a grab handle and any
    of which can be moved or deleted. ``routed`` is what
    :mod:`netgraph.layout.avoid` worked out on top of them so the line misses
    the boxes it is not attached to; it carries no handle, is recomputed on
    every render and is never written to a document unless somebody asks for it
    by name. Publishing it rather than making the page derive it is what keeps
    the JavaScript mirror of :mod:`netgraph.layout.routing` exact: the canvas
    still draws the line from anchors and waypoints, and the waypoints it draws
    from are the ones the renderer used.
    """
    plan = route_plan(graph, options, cache=cache)
    links: dict[str, Any] = {}
    for edge, line, computed in zip(graph.edges, plan.routes, plan.computed, strict=True):
        if line is None:
            continue
        pinned = graph.geometry.link(edge.id)
        links[edge.id] = {
            "endpoints": [edge.source, edge.target],
            "waypoints": [{"x": x, "y": y} for x, y in pinned.waypoints],
            "routed": [{"x": x, "y": y} for x, y in computed],
            "routing": None if pinned.routing is None else str(pinned.routing),
            "drawnAs": str(line.routing),
            "route": [{"x": x, "y": y} for x, y in line.corners],
            "label": (
                None
                if pinned.label is None
                else {
                    "at": pinned.label.at,
                    "offset": {"x": pinned.label.dx, "y": pinned.label.dy},
                }
            ),
            "fan": _fans(graph).get(edge.id, 0.0),
        }
    return links


def _fans(graph: Graph) -> dict[str, float]:
    """The sideways offset of each link, keyed by link id rather than by index."""
    return {graph.edges[index].id: offset for index, offset in fans_of(graph).items()}


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RequestError(f"{field_name!r} must be true or false")
    return value


def _flag(value: str, field_name: str) -> bool:
    """A boolean written the way a query string writes one."""
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise RequestError(f"{field_name!r} must be true or false, not {value!r}")


def _split_ids(value: str) -> list[int]:
    """``10,20`` as VLAN ids, refusing anything that is not one."""
    ids: list[int] = []
    for part in value.replace(" ", ",").split(","):
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise RequestError(f"{part!r} is not a VLAN id between 1 and {MAX_VLAN}") from None
    return ids


def _layer(value: Any) -> Layer:
    try:
        return Layer(value)
    except ValueError:
        expected = ", ".join(layer.value for layer in Layer)
        raise RequestError(f"unknown layer {value!r}; expected one of {expected}") from None


def _vlans(value: Any) -> frozenset[int]:
    if not isinstance(value, list):
        raise RequestError("'vlans' must be a list of VLAN ids")
    ids: set[int] = set()
    for item in value:
        # ``bool`` is an ``int`` in Python and ``true`` is not a VLAN id.
        if not isinstance(item, int) or isinstance(item, bool) or not 1 <= item <= MAX_VLAN:
            raise RequestError(f"{item!r} is not a VLAN id between 1 and {MAX_VLAN}")
        ids.add(item)
    return frozenset(ids)


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RequestError(f"{field_name!r} must be a list of strings")
    return tuple(value)


def _query(value: Any) -> str:
    """``select`` as a query, checked here so a bad one is a 400 and not a 500.

    Parsed and thrown away, exactly as ``--select``'s Click callback does: the
    answer depends on a graph that has not been built yet, and a malformed
    expression should be refused before an inventory is read. The parse error
    carries its caret block, and the browser puts it under the search box.
    """
    text = _text(value, "select")
    try:
        parse_query(text, source="select")
    except QueryError as exc:
        raise RequestError(str(exc)) from exc
    return text


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{field_name!r} must be a string")
    return value


def _namespaces(value: Any) -> tuple[str, ...]:
    """``collapse`` as the namespace prefixes it names.

    Bounded, and bounded deliberately: a collapse list is one triangle per
    container the reader has folded, and a request carrying thousands of them
    is not a person's. Each entry is stripped of its slashes so that
    ``sites/north`` and ``/sites/north/`` are the same container, which is what
    :func:`~netgraph.render.aggregate.collapse_targets` also assumes.
    """
    if isinstance(value, str):
        items = [part for part in value.split(",") if part]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = [item for item in value if item]
    else:
        raise RequestError("'collapse' must be a namespace, or a list of them")
    wanted = tuple(dict.fromkeys(item.strip().strip("/") for item in items))
    kept = tuple(item for item in wanted if item)
    if len(kept) > MAX_COLLAPSED:
        raise RequestError(
            f"'collapse' names {len(kept)} namespaces, which is more than the "
            f"{MAX_COLLAPSED} one drawing can usefully fold"
        )
    return kept


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _summary(inventory: Inventory, graph: Graph, *, rejected: bool) -> str:
    if not inventory.elements:
        return "nothing to draw yet: the stream holds no elements"
    if graph.is_empty:
        return "no element survived the filters"
    counted = f"{_plural(len(graph.nodes), 'node')}, {_plural(len(graph.edges), 'edge')}"
    return f"{counted} (drawn despite the problems below)" if rejected else counted


def _describe(exc: NetgraphError | OSError) -> str:
    """A failure in the words the page should show.

    Only two things reach here: a netgraph error — Graphviz missing, an SVG
    that would not parse — which already reads as a sentence, and a bare
    ``OSError`` from reading an icon file, which does not.
    """
    if isinstance(exc, OSError) and not isinstance(exc, NetgraphError):
        return f"{exc.strerror or exc}: {exc.filename}" if exc.filename else str(exc)
    return str(exc)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
