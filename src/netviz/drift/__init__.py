"""``netviz drift``: the inventory as an assertion about the live network.

``netviz import`` turns output collected from real devices into inventory
YAML. This package inverts the arrow. The same captures — ``lldpctl -f json``,
``ip -j addr show``, a cabling CSV — are read by the same dialect parsers, and
what comes out is not a tree to write but a *claim* to check the declared tree
against. Three questions get answered, per element:

* what does the network have that the inventory does not declare — an
  undeclared interface, an unexpected address, a VLAN on a trunk nobody wrote
  down, a neighbour LLDP sees that no cable joins;
* what does the inventory declare that the network does not have;
* what do both have and spell differently — a MAC, an MTU, a speed, an address.

**And a fourth, which is what makes the answer usable.** A capture is always
partial. ``lldpctl`` never reports an address; ``ip -j link show`` never reports
one either, though ``ip -j addr show`` does; no dialect netviz reads prints
the VLAN set of a trunk. Anything the capture is constitutionally unable to see
is reported as *unobserved*, in its own section, and is never counted as drift —
so running the command against one host's ``ip`` output does not announce that
the rest of the network has been unplugged. :mod:`netviz.drift.coverage` is
where that judgement is made and is the module to read first.

The pipeline is :mod:`~netviz.drift.run` → :mod:`~netviz.drift.compare` →
:mod:`~netviz.drift.report`, over the types in :mod:`~netviz.drift.model`.
Only ``run`` touches the filesystem; everything else is a pure function of its
argument, which is what lets the comparison be tested against a hand-built draft
and the renderers against a hand-built report.
"""

from __future__ import annotations

from netviz.drift.compare import CompareSpec, compare
from netviz.drift.coverage import CAPABILITIES, Capability, Coverage, coverage_of
from netviz.drift.model import (
    DIRECTION_SYMBOLS,
    Change,
    Direction,
    DriftReport,
    ElementDrift,
    Unobserved,
)
from netviz.drift.report import FORMATS, as_json, as_junit_report, render_drift, write_text
from netviz.drift.run import check_drift

__all__ = [
    "CAPABILITIES",
    "DIRECTION_SYMBOLS",
    "FORMATS",
    "Capability",
    "Change",
    "CompareSpec",
    "Coverage",
    "Direction",
    "DriftReport",
    "ElementDrift",
    "Unobserved",
    "as_json",
    "as_junit_report",
    "check_drift",
    "compare",
    "coverage_of",
    "render_drift",
    "write_text",
]
