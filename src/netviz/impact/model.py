"""What an impact analysis is made of: failures, blast radius, single points.

The shapes here are the contract between the engine
(:mod:`netviz.impact.engine`) and everything that consumes one — the text
report, the JSON document, the editor overlay. They carry *resolved* facts only:
fully-qualified names that exist, counts already taken, orders already fixed.
Nothing here decides anything and nothing here re-reads an inventory.

Every sequence is ordered, and ordered by something other than the order things
happened to be discovered in, because a report that reshuffles itself between
two runs over an unchanged tree cannot be committed, diffed or reviewed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from netviz.errors import NetvizError
from netviz.loader.inventory import short_name
from netviz.validate import Finding

__all__ = [
    "CAUSE_POWER",
    "CAUSE_REQUESTED",
    "STATUS_BROKEN",
    "STATUS_DEGRADED",
    "STATUS_MISSING",
    "STATUS_UNCHANGED",
    "Failure",
    "ImpactError",
    "ImpactReport",
    "LayerResult",
    "PathResult",
    "Split",
    "Spof",
]


class ImpactError(NetvizError):
    """An analysis cannot be attempted: ``--fail`` names nothing, or too much.

    Distinct from *finding a large blast radius*, which is an answer rather than
    a failure and comes back as an :class:`ImpactReport`.
    """

    #: 7 is taken by :class:`~netviz.trace.model.TraceError` and 6 by
    #: :class:`~netviz.httpserve.ServeError`; the CLI turns this one into a
    #: usage error long before it reaches the top level, and the code is here
    #: for a library caller who lets it propagate.
    exit_code = 8

    def __init__(self, message: str, candidates: Sequence[str] = ()) -> None:
        #: Every element the ambiguous or unknown reference could have meant, so
        #: a front end can list them instead of only saying "no".
        self.candidates: tuple[str, ...] = tuple(candidates)
        super().__init__(message)


#: :attr:`Failure.cause` of something the operator asked to fail.
CAUSE_REQUESTED: Final = "requested"
#: :attr:`Failure.cause` of something that went dark because its last feed did.
CAUSE_POWER: Final = "power"


@dataclass(frozen=True, slots=True)
class Failure:
    """One element removed from the inventory for the length of the analysis."""

    #: Fully-qualified name.
    element: str
    #: The element's ``kind`` — ``switch``, ``cable``, ``pdu``, ….
    kind: str
    #: :data:`CAUSE_REQUESTED` or :data:`CAUSE_POWER`.
    cause: str = CAUSE_REQUESTED
    #: What the operator typed, for a requested failure; the source that went
    #: dark, for a power one.
    spec: str = ""

    @property
    def name(self) -> str:
        return short_name(self.element)

    @property
    def is_collateral(self) -> bool:
        return self.cause != CAUSE_REQUESTED

    def describe(self) -> str:
        """``sw-access-01 (switch)``, or ``… (switch, lost pdu-r1-a)``."""
        if self.cause == CAUSE_POWER and self.spec:
            return f"{self.name} ({self.kind}, lost {short_name(self.spec)})"
        return f"{self.name} ({self.kind})"

    def as_record(self) -> dict[str, Any]:
        return {
            "element": self.element,
            "name": self.name,
            "kind": self.kind,
            "cause": self.cause,
            "spec": self.spec or None,
        }


@dataclass(frozen=True, slots=True)
class Split:
    """One namespace the failure broke into pieces."""

    namespace: str
    #: Connected pieces the namespace's elements fell into before and after.
    before: int
    after: int
    #: The pieces, each in graph order, ordered by their first member.
    fragments: tuple[tuple[str, ...], ...] = ()

    @property
    def label(self) -> str:
        """The namespace as a reader sees it; the root has no name of its own."""
        return self.namespace or "(root)"

    def as_record(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "before": self.before,
            "after": self.after,
            "fragments": [list(fragment) for fragment in self.fragments],
        }


@dataclass(frozen=True, slots=True)
class LayerResult:
    """What one failure did to one layer."""

    layer: str
    title: str
    #: The nodes reachability was measured from, in graph order.
    anchors: tuple[str, ...] = ()
    #: Elements reachable from the anchors before and after.
    served_before: int = 0
    served_after: int = 0
    #: Elements that were reachable and are not any more, in graph order. Never
    #: includes a failed element: it is gone, not stranded.
    isolated: tuple[str, ...] = ()
    #: Elements that were already unreachable before anything failed. Reported
    #: once, as context, so a reader does not mistake a pre-existing island for
    #: something this failure caused.
    stranded: tuple[str, ...] = ()
    #: Namespaces this failure partitioned, in namespace order.
    splits: tuple[Split, ...] = ()
    #: What was actually taken out of *this* layer: an element is a node here
    #: and a cable is a link, and a cable is neither at layer 3.
    removed_nodes: tuple[str, ...] = ()
    removed_links: tuple[str, ...] = ()
    #: Set when no anchor is present in this layer at all, in which case
    #: reachability says nothing and only the partitions are reported.
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.layer} ({self.title})"

    @property
    def affected(self) -> bool:
        """Did anything measurable happen here?"""
        return bool(self.isolated or self.splits)

    def as_record(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "title": self.title,
            "anchors": list(self.anchors),
            "served": {"before": self.served_before, "after": self.served_after},
            "isolated": list(self.isolated),
            "stranded": list(self.stranded),
            "partitioned": [split.as_record() for split in self.splits],
            "removed": {"nodes": list(self.removed_nodes), "links": list(self.removed_links)},
            "note": self.note or None,
        }


#: :attr:`PathResult.status` — the route existed and does not any more.
STATUS_BROKEN: Final = "broken"
#: Some routes are gone, at least one remains.
STATUS_DEGRADED: Final = "degraded"
#: As many routes as before.
STATUS_UNCHANGED: Final = "unchanged"
#: There was no route even before the failure, so this says nothing about it.
STATUS_MISSING: Final = "missing"


@dataclass(frozen=True, slots=True)
class PathResult:
    """One route the trace engine checked on both sides of the failure."""

    #: What the operator typed, ``src=dst``.
    spec: str
    source: str
    destination: str
    #: How many distinct routes the trace engine found, before and after.
    before: int = 0
    after: int = 0
    #: The layer the route was found at, before and after. ``None`` for none.
    layer_before: str | None = None
    layer_after: str | None = None
    status: str = STATUS_UNCHANGED
    #: Why the check could not be made, when it could not.
    note: str = ""

    @property
    def broke(self) -> bool:
        return self.status == STATUS_BROKEN

    def as_record(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "source": self.source,
            "destination": self.destination,
            "status": self.status,
            "paths": {"before": self.before, "after": self.after},
            "layer": {"before": self.layer_before, "after": self.layer_after},
            "note": self.note or None,
        }


@dataclass(frozen=True, slots=True)
class Spof:
    """One element whose loss, on its own, cuts endpoints off."""

    #: Which view found it: ``l1``, ``l2``, ``l3`` or ``power``.
    layer: str
    #: ``node`` or ``link`` (:mod:`netviz.connectivity`).
    kind: str
    #: The element's fully-qualified name, or the link's edge id.
    id: str
    #: How many endpoints it isolates. The ranking key.
    isolated: int
    #: What the thing is: an element kind, or the kind of link.
    element_kind: str = ""
    #: The endpoints it isolates, in graph order. Filled only for the entries
    #: the report actually prints — see :func:`netviz.impact.engine.single_points`.
    isolates: tuple[str, ...] = ()
    articulation: bool = False
    bridge: bool = False
    #: Set when the finding is a power dependency rather than a topological cut.
    feeds: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return short_name(self.id.partition("#")[0])

    @property
    def is_power(self) -> bool:
        return self.layer == "power"

    @property
    def reason(self) -> str:
        """Why this is a single point of failure, in three words."""
        if self.is_power:
            return "only feed"
        if self.kind == "link":
            return "bridge" if self.bridge else "sole link"
        return "articulation point" if self.articulation else "sole anchor"

    @property
    def order(self) -> tuple[int, str, str, str]:
        """Worst first; ties broken by layer, kind and identity."""
        return (-self.isolated, self.layer, self.kind, self.id)

    def as_record(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "elementKind": self.element_kind or None,
            "isolated": self.isolated,
            "isolates": list(self.isolates),
            "reason": self.reason,
            "articulation": self.articulation,
            "bridge": self.bridge,
            "feeds": list(self.feeds),
        }


@dataclass(frozen=True, slots=True)
class ImpactReport:
    """Everything one ``netviz impact`` run found."""

    #: Absolute path of the inventory root.
    root: Path
    #: ``fail``, ``spof`` or both, in the order the report prints them.
    modes: tuple[str, ...] = ()
    #: What was removed, requested failures first, then the power cascade.
    failures: tuple[Failure, ...] = ()
    #: Where reachability was measured from, and how they were chosen.
    anchors: tuple[str, ...] = ()
    anchor_source: str = ""
    #: One per requested layer, in :data:`~netviz.impact.graphs.LAYERS` order.
    layers: tuple[LayerResult, ...] = ()
    #: One per ``--path``, in the order they were given.
    paths: tuple[PathResult, ...] = ()
    #: The single points of failure the report prints, worst first.
    spofs: tuple[Spof, ...] = ()
    #: How many were found in total, before the cutoff dropped any.
    spof_total: int = 0
    #: What ``--redundancy`` produced, through the ordinary rule machinery.
    findings: tuple[Finding, ...] = ()
    #: Anything the reader needs that is neither a failure nor a finding.
    notes: tuple[str, ...] = ()

    @property
    def isolated_total(self) -> int:
        """The largest number of endpoints any one layer lost."""
        return max((len(layer.isolated) for layer in self.layers), default=0)

    @property
    def broken_paths(self) -> tuple[PathResult, ...]:
        return tuple(path for path in self.paths if path.broke)

    @property
    def failed_expectations(self) -> tuple[Finding, ...]:
        """The redundancy findings that fail a run."""
        return tuple(finding for finding in self.findings if finding.severity.is_fatal)

    @property
    def impacted(self) -> bool:
        """Did this run find something a CI gate should fail on?

        Enumerating single points of failure is not one of those things. Every
        network of any size has them, and a command that exited non-zero for
        saying so would be a command nobody could put in a pipeline.
        """
        return bool(
            any(layer.affected for layer in self.layers)
            or self.broken_paths
            or self.failed_expectations
        )

    @property
    def truncated(self) -> bool:
        return self.spof_total > len(self.spofs)

    def as_dict(self, *, version: str) -> dict[str, Any]:
        """The JSON document. Keys are only ever added, never renamed."""
        return {
            "schemaVersion": 1,
            "tool": {"name": "netviz", "version": version},
            "inventory": {"root": str(self.root)},
            "modes": list(self.modes),
            "anchors": {"elements": list(self.anchors), "source": self.anchor_source},
            "failed": [failure.as_record() for failure in self.failures],
            "layers": [layer.as_record() for layer in self.layers],
            "paths": [path.as_record() for path in self.paths],
            "spof": {
                "total": self.spof_total,
                "reported": len(self.spofs),
                "truncated": self.truncated,
                "entries": [spof.as_record() for spof in self.spofs],
            },
            "expectations": [_finding_record(finding) for finding in self.findings],
            "summary": {
                "isolated": self.isolated_total,
                "brokenPaths": len(self.broken_paths),
                "violations": len(self.failed_expectations),
                "impacted": self.impacted,
            },
            "notes": list(self.notes),
        }


def _finding_record(finding: Finding) -> Mapping[str, Any]:
    """One redundancy finding, in the shape ``validate --output-format json`` uses.

    Deliberately a subset of :meth:`netviz.diagnostics.Diagnostic.as_record`
    rather than a shape of its own: a consumer that already reads netviz
    findings should not need a second parser for these.
    """
    return {
        "rule": finding.rule,
        "severity": str(finding.severity),
        "message": finding.message,
        "element": finding.element,
        "elements": list(finding.elements),
        "file": finding.file,
    }
