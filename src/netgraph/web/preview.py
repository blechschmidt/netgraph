"""One ``parse → validate → render`` pass over a YAML document stream.

This is the whole of what ``netgraph web`` does per keystroke-burst, and it is
the same pipeline the command line runs, in the same order, with two
differences that follow from being interactive:

* **The input is a stream, not a tree.** :func:`~netgraph.loader.load_stream`
  reads what the browser sent, under the same strict parser and the same schema
  validation as a folder of files. No file is opened and nothing is written.
* **A rejected inventory is still drawn.** ``netgraph render`` refuses one
  without ``--force``, because a diagram that disagrees with the files
  misinforms whoever it is shown to. Here the diagram *is* the feedback: text
  being edited is wrong most of the time, and blanking the picture on every
  half-typed line would make the tool useless. So every problem is reported —
  prominently, with its line — and whatever resolved is drawn anyway.

The result carries a :class:`~netgraph.watch.pipeline.Status`, and a status
that is not ``ok`` is the front end's cue to say so, not to hide the diagram.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from netgraph.config import ValidationConfig
from netgraph.errors import NetgraphError
from netgraph.loader import Inventory, load_stream
from netgraph.render import (
    DETAIL_OPTIONS,
    FilterSpec,
    Graph,
    IconTheme,
    Layer,
    RenderOptions,
    build_details,
    build_graph,
    filter_graph,
)
from netgraph.render.dot import to_image
from netgraph.rules import Severity
from netgraph.validate import validate as run_validation
from netgraph.watch.pipeline import Problem, Status, flatten_problems
from netgraph.web.svgdoc import prepare

__all__ = ["MAX_VLAN", "Preview", "ViewOptions", "render_source"]

#: The highest VLAN id ``--vlan`` and the browser may ask for (§9.1).
MAX_VLAN: Final = 4094

#: What the browser is allowed to ask for, mapped to the field it sets. Keeping
#: this closed is what stops a request from reaching a rendering knob the web
#: interface does not offer.
_BOOLEAN_FIELDS: Final[tuple[str, ...]] = ("show_ips", "show_vlans", "group_by_namespace", "strict")


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
    #: Keep only elements participating in these VLANs; empty keeps everything.
    vlans: frozenset[int] = frozenset()
    #: Keep only these element kinds; empty keeps everything.
    kinds: tuple[str, ...] = ()
    title: str | None = None
    #: Promote surviving warnings to errors, as ``--strict`` does elsewhere.
    strict: bool = False
    #: Icon theme, chosen once on the command line rather than by the browser:
    #: it names a directory, and a request must not be able to.
    icons: IconTheme | None = field(default=None, compare=False)

    @classmethod
    def from_request(
        cls, payload: Mapping[str, Any], *, icons: IconTheme | None = None
    ) -> ViewOptions:
        """Build options from a decoded JSON request body.

        Every value is checked here rather than trusted: the body arrives from
        a browser, which may be showing a page this server did not write.

        Raises:
            RequestError: A field is of the wrong type or out of range.
        """
        values: dict[str, Any] = {"icons": icons}
        for name in _BOOLEAN_FIELDS:
            if name in payload:
                values[name] = _boolean(payload[name], name)
        if "layer" in payload:
            values["layer"] = _layer(payload["layer"])
        if "vlans" in payload:
            values["vlans"] = _vlans(payload["vlans"])
        if "kinds" in payload:
            values["kinds"] = _strings(payload["kinds"], "kinds")
        if payload.get("title"):
            values["title"] = _text(payload["title"], "title")
        return cls(**values)

    @property
    def filter_spec(self) -> FilterSpec:
        return FilterSpec(vlans=self.vlans, kinds=self.kinds)

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
            title=self.title,
            icons=self.icons,
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
    #: Wall-clock duration of the pass, in seconds.
    duration: float = 0.0

    @property
    def error_count(self) -> int:
        return sum(1 for problem in self.problems if problem.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for problem in self.problems if problem.severity is Severity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        """The JSON the page consumes. Keys are only ever added, never renamed."""
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
                }
                for problem in self.problems
            ],
            "dangling": list(self.dangling),
            "counts": {
                "nodes": self.nodes,
                "edges": self.edges,
                "errors": self.error_count,
                "warnings": self.warning_count,
            },
            "durationMs": round(self.duration * 1000, 1),
        }


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
    options = view or ViewOptions()
    started = time.monotonic()

    inventory = load_stream(source)
    findings = run_validation(inventory, ValidationConfig(strict=options.strict))
    problems = flatten_problems(inventory.errors, findings)
    rejected = bool(inventory.errors) or any(finding.severity.is_fatal for finding in findings)

    try:
        graph = filter_graph(build_graph(inventory, layer=options.layer), options.filter_spec)
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
        status=Status.INVALID if rejected else Status.OK,
        message=_summary(inventory, graph, rejected=rejected),
        svg=svg,
        details=build_details(graph, DETAIL_OPTIONS),
        problems=problems,
        nodes=len(graph.nodes),
        edges=len(graph.edges),
        dangling=tuple(graph.dangling),
        duration=time.monotonic() - started,
    )


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RequestError(f"{field_name!r} must be true or false")
    return value


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


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{field_name!r} must be a string")
    return value


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
