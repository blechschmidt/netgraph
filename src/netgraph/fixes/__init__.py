"""One-click repairs: turning a diagnostic into the edit that resolves it.

``netgraph validate`` says what is wrong. For a good part of the catalogue it
also knows what would put it right — a stale layout key names an element that is
gone, a trunk's native VLAN is missing from a list it is a member of anyway — and
:mod:`netgraph.edit` can already express every one of those changes. This package
is the join: a table of rule to *fix producer*, a producer being a pure function
from a :class:`~netgraph.validate.Finding` and the
:class:`~netgraph.loader.Inventory` it was found in to the operations that repair
it.

    from netgraph.fixes import fixes_for, repair

    for fix in fixes_for(finding, inventory):
        print(fix.key, fix.title, fix.operations)

Three front ends use it, and all three go through the same two modules:

* ``netgraph validate --fix`` applies every unambiguous repair and prints what
  it did; with ``--dry-run`` it prints the diff and writes nothing.
* The web editor puts a **Fix** button on each fixable diagnostic, and applies
  the operations as one logged, revertible gesture.
* ``docs/validation-rules.md`` lists which rules are fixable, generated from
  :data:`FIXES` so the page cannot drift from the code.

:mod:`netgraph.fixes.producers` decides *what* a repair is, and never picks
between two of them: a rule that admits more than one plausible reading offers
all of them and lets a person choose. :mod:`netgraph.fixes.run` decides whether a
repair may stand — it applies one, re-validates, and rolls it back if a single
new finding appeared, which is the promise that ``--fix`` cannot make an
inventory worse.
"""

from __future__ import annotations

from netgraph.fixes.model import Choice, Fix, FixProducer, FixSpec
from netgraph.fixes.producers import (
    FIXES,
    fixable_rules,
    fixes_for,
    producer_for,
    spec_for,
)
from netgraph.fixes.run import (
    MAX_FIXES,
    AppliedFix,
    FixOutcome,
    FixReport,
    Offer,
    SkippedFix,
    apply_fix,
    offers_for,
    repair,
)

__all__ = [
    "FIXES",
    "MAX_FIXES",
    "AppliedFix",
    "Choice",
    "Fix",
    "FixOutcome",
    "FixProducer",
    "FixReport",
    "FixSpec",
    "Offer",
    "SkippedFix",
    "apply_fix",
    "fixable_rules",
    "fixes_for",
    "offers_for",
    "producer_for",
    "repair",
    "spec_for",
]
