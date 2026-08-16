"""What-if failure simulation: blast radius, single points of failure, promises.

An inventory that knows every cable, tunnel and routed adjacency can answer the
question operators actually care about — *what breaks if this dies* — and this
package asks it::

    report = simulate(inventory, fail=["device/sw1"], spof=True)
    print(render_impact(report, "text"))

Four things, one set of graphs:

1. :mod:`~netviz.impact.graphs` turns an inventory into the searchable views
   of layers 1, 2, 3 and power that everything else here consumes;
2. :mod:`netviz.connectivity` answers the graph questions — what a failure
   disconnects, which single failures disconnect anything at all — in time
   linear in the size of the graph;
3. :mod:`~netviz.impact.engine` decides what to remove, re-derives the layers
   without it, and re-runs the :mod:`trace engine <netviz.trace>` over the
   named path assertions;
4. :mod:`~netviz.impact.report` renders the result as text or as JSON.

Nothing here re-reads the inventory or re-resolves a reference: every view is
built from :func:`~netviz.render.graph.build_graph`, the pass the renderers
and the trace engine already consume, so a simulated failure and a drawn diagram
cannot disagree about what is connected to what.

Declared expectations (``--redundancy``) are graded by ordinary validation rules
— ``E047``, ``E048`` and ``W141`` — rather than by anything in this package, so
``netviz validate`` gates a pull request on them and this command explains
them. See :mod:`netviz.expectations` for the vocabulary.
"""

from __future__ import annotations

from netviz.impact.engine import (
    DEFAULT_LIMIT,
    REDUNDANCY_RULES,
    check_expectations,
    gateways,
    prune,
    resolve_element,
    simulate,
    single_points,
    split_path_spec,
)
from netviz.impact.graphs import LAYERS, POWER, LayerView, views
from netviz.impact.model import (
    Failure,
    ImpactError,
    ImpactReport,
    LayerResult,
    PathResult,
    Split,
    Spof,
)
from netviz.impact.report import REPORT_FORMATS, render_impact, to_json, to_text

__all__ = [
    "DEFAULT_LIMIT",
    "LAYERS",
    "POWER",
    "REDUNDANCY_RULES",
    "REPORT_FORMATS",
    "Failure",
    "ImpactError",
    "ImpactReport",
    "LayerResult",
    "LayerView",
    "PathResult",
    "Split",
    "Spof",
    "check_expectations",
    "gateways",
    "prune",
    "render_impact",
    "resolve_element",
    "simulate",
    "single_points",
    "split_path_spec",
    "to_json",
    "to_text",
    "views",
]
