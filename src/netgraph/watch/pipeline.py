"""One load → validate → render cycle, and the state a preview reads from.

The cycle is the unit of work ``netgraph watch`` repeats. It is deliberately
*total*: every problem a user can cause — an unparseable document, a dangling
cable, a missing Graphviz binary — comes back as a :class:`CycleResult` with a
non-``ok`` status rather than an exception, because a watcher that dies on the
first typo is useless. Genuine bugs still propagate; only the failure modes the
inventory itself can produce are caught.

:class:`LiveRender` holds the outcome of the most recent cycle together with the
last payload that actually rendered. Those two are tracked separately on
purpose: when a cycle fails, the status the user sees must change while the
diagram they are looking at must not.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Final

from netgraph.config import load_config
from netgraph.errors import NetgraphError, RenderError, format_path
from netgraph.loader import Inventory, LoadError, load_tree
from netgraph.render import (
    AggregateSpec,
    FilterSpec,
    Layer,
    RenderOptions,
    UnknownElementError,
    aggregate_graph,
    build_graph,
    filter_graph,
    render_layers,
)
from netgraph.rules import Severity
from netgraph.validate import Finding
from netgraph.validate import validate as run_validation

__all__ = [
    "CycleResult",
    "LiveRender",
    "Problem",
    "RenderRequest",
    "Snapshot",
    "Status",
    "flatten_problems",
    "run_cycle",
    "write_atomically",
]


class Status(str, Enum):
    """How a single cycle ended."""

    #: No cycle has completed yet.
    PENDING = "pending"
    #: The inventory loaded, validated and rendered.
    OK = "ok"
    #: The inventory was rejected; the previous render is still the good one.
    INVALID = "invalid"
    #: The inventory was usable but the render itself could not be produced.
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value

    @property
    def is_ok(self) -> bool:
        return self is Status.OK


@dataclass(frozen=True, slots=True)
class Problem:
    """A load error or a finding, flattened for display in any front end."""

    severity: Severity
    location: str
    rule: str
    message: str

    @classmethod
    def from_load_error(cls, error: LoadError) -> Problem:
        message = error.message
        if error.field_path:
            message = f"{format_path(error.field_path)}: {message}"
        return cls(
            severity=Severity.ERROR,
            location=error.location,
            rule=error.rule or "load",
            message=message,
        )

    @classmethod
    def from_finding(cls, finding: Finding) -> Problem:
        return cls(
            severity=finding.severity,
            location=finding.location,
            rule=finding.rule,
            message=finding.message,
        )

    def __str__(self) -> str:
        return f"{self.severity}  {self.location}  {self.rule}  {self.message}"


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """Everything a cycle needs, fixed for the lifetime of a watch run.

    The per-inventory ``netgraph.toml`` is *not* held here: it is re-read on
    every cycle, so editing it takes effect like editing any other file.
    """

    inventory: Path
    output_format: str = "svg"
    #: The layers to draw. More than one only makes sense for a format that
    #: holds several — ``html`` — and the command line refuses it for the rest;
    #: see :func:`netgraph.render.render_layers`.
    layers: tuple[Layer, ...] = (Layer.L1,)
    spec: FilterSpec = field(default_factory=FilterSpec)
    #: What to summarise rather than draw. Unlike ``spec`` this removes nothing;
    #: see :mod:`netgraph.render.aggregate`.
    aggregate: AggregateSpec = field(default_factory=AggregateSpec)
    options: RenderOptions = field(default_factory=RenderOptions)
    #: Promote surviving warnings to errors, as ``--strict`` does elsewhere.
    strict: bool = False
    #: Rule ids silenced from the command line.
    disabled: tuple[str, ...] = ()
    #: Render even when the inventory was rejected.
    force: bool = False

    @property
    def config_root(self) -> Path:
        """Directory ``netgraph.toml`` is looked for in."""
        return self.inventory if self.inventory.is_dir() else self.inventory.parent


@dataclass(frozen=True, slots=True)
class CycleResult:
    """The outcome of one load → validate → render pass."""

    status: Status
    #: Rendered bytes, or ``None`` when this cycle produced no diagram.
    payload: bytes | None = None
    #: One-line summary suitable for a status line.
    message: str = ""
    #: Load errors and findings, most severe group first.
    problems: tuple[Problem, ...] = ()
    #: Load errors, kept separate so the CLI can report them the usual way.
    errors: tuple[LoadError, ...] = ()
    findings: tuple[Finding, ...] = ()
    #: Elements the graph builder had to drop (dangling references).
    dangling: tuple[str, ...] = ()
    nodes: int = 0
    edges: int = 0
    #: Wall-clock duration of the cycle, in seconds.
    duration: float = 0.0

    @property
    def error_count(self) -> int:
        return sum(1 for problem in self.problems if problem.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for problem in self.problems if problem.severity is Severity.WARNING)


def run_cycle(request: RenderRequest) -> CycleResult:
    """Load, validate and render once, reporting failures instead of raising.

    Every error the *inventory* can cause is turned into a result: a missing
    root, a syntax error, a rejected validation, an unavailable Graphviz. A
    :class:`~netgraph.errors.NetgraphError` escaping this function would kill a
    watch loop that exists precisely to survive broken intermediate states.
    """
    started = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - started

    try:
        settings = load_config(request.config_root).validation.with_overrides(
            # ``--strict`` may only turn strictness on; the file decides otherwise.
            strict=True if request.strict else None,
            ignore=request.disabled,
        )
        inventory = load_tree(request.inventory)
        findings = run_validation(inventory, settings)
        problems = flatten_problems(inventory.errors, findings)

        if _is_rejected(inventory, findings) and not request.force:
            return CycleResult(
                status=Status.INVALID,
                message=_count_summary(problems),
                problems=problems,
                errors=tuple(inventory.errors),
                findings=tuple(findings),
                duration=elapsed(),
            )

        graphs = [
            aggregate_graph(
                filter_graph(build_graph(inventory, layer=layer), request.spec), request.aggregate
            )
            for layer in request.layers
        ]
        payload = render_layers(graphs, request.output_format, request.options)
    except (NetgraphError, UnknownElementError, OSError) as exc:
        return CycleResult(
            status=Status.FAILED,
            message=_describe(exc),
            duration=elapsed(),
        )

    # Every count is over every layer drawn. A one-layer run — which is all but
    # ``-f html --layer … --layer …`` — is the same number it always was.
    nodes = sum(len(graph.nodes) for graph in graphs)
    edges = sum(len(graph.edges) for graph in graphs)
    return CycleResult(
        status=Status.OK,
        payload=payload,
        message=f"{_plural(nodes, 'node')}, {_plural(edges, 'edge')}",
        problems=problems,
        errors=tuple(inventory.errors),
        findings=tuple(findings),
        dangling=tuple(text for graph in graphs for text in graph.dangling),
        nodes=nodes,
        edges=edges,
        duration=elapsed(),
    )


def _is_rejected(inventory: Inventory, findings: Iterable[Finding]) -> bool:
    return bool(inventory.errors) or any(finding.severity.is_fatal for finding in findings)


def flatten_problems(
    errors: Sequence[LoadError], findings: Sequence[Finding]
) -> tuple[Problem, ...]:
    """Flatten load errors and findings, most severe first, load order within.

    Shared with :mod:`netgraph.web.preview`: two front ends listing the same
    inventory's problems in two different orders would be a bug in one of them.
    """
    flattened = [Problem.from_load_error(error) for error in errors]
    flattened.extend(Problem.from_finding(finding) for finding in findings)
    return tuple(sorted(flattened, key=lambda problem: problem.severity.rank))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, UnknownElementError):
        return f"no element named {exc.name!r} in this inventory"
    if isinstance(exc, OSError) and not isinstance(exc, NetgraphError):
        return f"{exc.strerror or exc}: {exc.filename}" if exc.filename else str(exc)
    return str(exc)


def _count_summary(problems: Sequence[Problem]) -> str:
    errors = sum(1 for problem in problems if problem.severity is Severity.ERROR)
    warnings = sum(1 for problem in problems if problem.severity is Severity.WARNING)
    parts = [_plural(errors, "error")] if errors else []
    if warnings:
        parts.append(_plural(warnings, "warning"))
    return ", ".join(parts) or "rejected"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# --------------------------------------------------------------------------- #
# Published state
# --------------------------------------------------------------------------- #

#: Format of the clock in status lines and on the preview page. Watchers are
#: read within seconds of the event, so the date would be noise.
STAMP_FORMAT: Final = "%H:%M:%S"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """An immutable view of the live state, safe to read from any thread."""

    #: Bumped by every cycle, successful or not.
    revision: int = 0
    #: Bumped only when :attr:`payload` changed, so a client can avoid refetching.
    payload_revision: int = 0
    payload: bytes | None = None
    output_format: str = "svg"
    status: Status = Status.PENDING
    message: str = "waiting for the first render"
    #: Local time the cycle finished, ``HH:MM:SS``.
    stamp: str = ""
    problems: tuple[str, ...] = ()
    #: Is :attr:`payload` from an older, successful cycle?
    stale: bool = False


class LiveRender:
    """The latest cycle plus the last render that succeeded.

    A failed cycle updates the status but leaves the payload alone, which is
    what keeps the previously good diagram on screen while the user fixes a
    half-typed YAML file.
    """

    def __init__(self, output_format: str = "svg") -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot(output_format=output_format)

    def publish(self, result: CycleResult, *, stamp: str | None = None) -> Snapshot:
        """Record ``result`` and return the snapshot it produced."""
        when = stamp if stamp is not None else time.strftime(STAMP_FORMAT)
        with self._lock:
            previous = self._snapshot
            keeps_payload = result.payload is None
            self._snapshot = Snapshot(
                revision=previous.revision + 1,
                payload_revision=(
                    previous.payload_revision if keeps_payload else previous.payload_revision + 1
                ),
                payload=previous.payload if keeps_payload else result.payload,
                output_format=previous.output_format,
                status=result.status,
                message=result.message,
                stamp=when,
                problems=tuple(str(problem) for problem in result.problems),
                stale=keeps_payload and previous.payload is not None,
            )
            return self._snapshot

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def write_atomically(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` in one step.

    A watch run rewrites its output while something else — a browser, an image
    viewer, another netgraph — may be reading it. Writing to a sibling temporary
    file and renaming means a reader sees either the old diagram or the new one,
    never half of each.

    Raises:
        RenderError: The destination cannot be written.
    """
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly below
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        # ``NamedTemporaryFile`` creates with 0600; the diagram is not a secret
        # and users expect it to be readable like any other rendered artefact.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except OSError as exc:
        if handle is not None:
            Path(handle.name).unlink(missing_ok=True)
        raise RenderError(f"cannot write {path}: {exc.strerror or exc}") from exc
