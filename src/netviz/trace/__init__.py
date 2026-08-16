"""Reachability tracing: how one element reaches another, and what it crosses.

An inventory already knows enough to answer the question a network engineer
actually asks. This package asks it::

    result = trace(inventory, "pc-north-01", "srv-south-01")
    print(render_trace(result, "text"))

The pipeline is three stages, each independently testable:

1. :mod:`~netviz.trace.endpoints` turns what the user typed — an element, an
   ``element:interface``, or an IP address — into a resolved
   :class:`~netviz.trace.model.Endpoint`;
2. :mod:`~netviz.trace.engine` searches, at layer 2 first and layer 3 after,
   over the graphs :func:`~netviz.render.graph.build_graph` already builds;
3. :mod:`~netviz.trace.report` renders the answer as hop-by-hop text or as a
   JSON document.

Nothing here re-reads the inventory or re-resolves a reference, so a traced path
and a rendered diagram cannot disagree about what is connected to what — which
is what lets :meth:`TracedPath.highlight
<netviz.trace.model.TracedPath.highlight>` hand the route straight to a
renderer.
"""

from __future__ import annotations

from netviz.trace.endpoints import resolve_endpoint
from netviz.trace.engine import trace
from netviz.trace.model import (
    DEFAULT_MAX_HOPS,
    MAX_PATHS,
    Endpoint,
    Frontier,
    Link,
    TracedPath,
    TraceError,
    TraceResult,
    Waypoint,
)
from netviz.trace.report import (
    PATH_KIND,
    REPORT_FORMATS,
    render_trace,
    to_json,
    to_text,
    trace_to_dict,
)

__all__ = [
    "DEFAULT_MAX_HOPS",
    "MAX_PATHS",
    "PATH_KIND",
    "REPORT_FORMATS",
    "Endpoint",
    "Frontier",
    "Link",
    "TraceError",
    "TraceResult",
    "TracedPath",
    "Waypoint",
    "render_trace",
    "resolve_endpoint",
    "to_json",
    "to_text",
    "trace",
    "trace_to_dict",
]
