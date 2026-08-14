"""Declared survivability expectations: what an element promises to survive.

An inventory records what the network *is*. This module is where it records
what the network is *for*: a device annotated

.. code-block:: yaml

    metadata:
      annotations:
        netgraph/redundancy: "gateway, power"

is asserting two things that no amount of reading the cabling will tell you —
that losing any one element must not cut it off from its default gateway, and
that losing any one power source must not switch it off. Both are design intent.
Both are the kind of intent that quietly stops being true when somebody
re-patches a rack, which is exactly why it belongs in the files next to the
cabling rather than in a runbook.

Why a leaf module
-----------------

Two very different callers need the same vocabulary and must not disagree about
it: :mod:`netgraph.validate`, which grades the expectations as ``E047``, ``E048``
and ``W141`` so ``netgraph validate`` gates a pull request on them, and
:mod:`netgraph.impact`, which reports them alongside the failure simulation that
explains *why* one is not met. Neither may import the other — the validator is a
pure inventory pass and the impact engine builds graphs and calls the validator —
so the vocabulary lives here, importing nothing but the loader and the models.

Why an annotation and not a spec field
--------------------------------------

An expectation is not a property of the hardware. Two identical switches in two
racks may carry different expectations because one of them serves the ward and
the other serves the car park, and a ``spec`` that had to describe the ward would
stop being a description of the switch. ``metadata.annotations`` is where §3.1
puts per-element input to the tooling, and this is that.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

from netgraph.loader.inventory import Inventory, short_name

__all__ = [
    "EXPECTATION_ANNOTATIONS",
    "Declaration",
    "Expectation",
    "declarations",
    "expectation_names",
    "parse_expectations",
]

#: The annotation keys that carry an expectation, in precedence order. Both
#: spellings are accepted for the same reason ``netgraph/ignore`` accepts both:
#: the ``netgraph.dev/`` prefix matches ``apiVersion`` and the bare one is what
#: people type. A document may set either; setting both merges them.
EXPECTATION_ANNOTATIONS: Final[tuple[str, ...]] = ("netgraph/redundancy", "netgraph.dev/redundancy")

#: How the value is split: on commas and on runs of whitespace, so
#: ``"gateway, power"``, ``"gateway power"`` and a YAML block scalar listing one
#: per line all mean the same thing.
_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[,\s]+")


class Expectation(str, Enum):
    """One thing an element declares it must survive any single failure of."""

    #: It must keep a path to its default gateway. What ``E047`` grades.
    GATEWAY = "gateway"
    #: It must keep power. What ``E048`` grades.
    POWER = "power"

    @property
    def rule(self) -> str:
        """The rule id that grades this expectation."""
        return _RULES[self]

    @property
    def summary(self) -> str:
        """One clause, for a message that has to say what was promised."""
        return _SUMMARIES[self]

    def __str__(self) -> str:
        return self.value


_RULES: Final[dict[Expectation, str]] = {
    Expectation.GATEWAY: "E047",
    Expectation.POWER: "E048",
}

_SUMMARIES: Final[dict[Expectation, str]] = {
    Expectation.GATEWAY: "keep a path to its default gateway under any single failure",
    Expectation.POWER: "keep power under any single failure",
}

#: Every accepted spelling, lower-cased, mapped to its expectation. The plurals
#: and the hyphenated forms are here because a typo in an *expectation* is
#: ``W141`` and therefore visible, but a synonym somebody reasonably expected to
#: work should not have to be one.
_BY_NAME: Final[dict[str, Expectation]] = {
    "gateway": Expectation.GATEWAY,
    "gateways": Expectation.GATEWAY,
    "default-gateway": Expectation.GATEWAY,
    "power": Expectation.POWER,
    "feeds": Expectation.POWER,
}


def expectation_names() -> tuple[str, ...]:
    """Every accepted token, sorted. What ``W141`` lists as the alternatives."""
    return tuple(sorted(_BY_NAME))


def parse_expectations(value: str) -> tuple[tuple[Expectation, ...], tuple[str, ...]]:
    """Split an annotation value into the expectations it names and the rest.

    Args:
        value: The raw annotation, e.g. ``"gateway, power"``.

    Returns:
        The recognised expectations in declaration order without repeats, and
        the tokens that named nothing — kept verbatim so ``W141`` can echo the
        typo rather than a normalised form of it.
    """
    recognised: dict[Expectation, None] = {}
    unknown: dict[str, None] = {}
    for token in _SEPARATORS.split(value.strip()):
        if not token:
            continue
        expectation = _BY_NAME.get(token.lower())
        if expectation is None:
            unknown.setdefault(token, None)
        else:
            recognised.setdefault(expectation, None)
    return tuple(recognised), tuple(unknown)


@dataclass(frozen=True, slots=True)
class Declaration:
    """What one element declared, and where it said it."""

    #: Fully-qualified name of the element.
    element: str
    #: The expectations it names, in declaration order without repeats.
    expectations: tuple[Expectation, ...]
    #: Tokens that named no expectation, verbatim. ``W141``'s business.
    unknown: tuple[str, ...] = ()
    #: The annotation key the value came from, for the field path of a finding.
    #: When both spellings are set this is the first of :data:`EXPECTATION_ANNOTATIONS`.
    key: str = EXPECTATION_ANNOTATIONS[0]

    @property
    def name(self) -> str:
        """The element's short name."""
        return short_name(self.element)

    @property
    def field_path(self) -> tuple[str | int, ...]:
        """Where in the document the expectation was written."""
        return ("metadata", "annotations", self.key)

    def wants(self, expectation: Expectation) -> bool:
        return expectation in self.expectations


def declarations(inventory: Inventory) -> tuple[Declaration, ...]:
    """Every expectation declared in ``inventory``, in load order.

    An element that carries the annotation with an empty value, or with nothing
    but unrecognised tokens, is still returned: it declared *something*, and
    dropping it would leave ``W141`` with nothing to report the typo against.
    """
    return tuple(_declarations(inventory))


def _declarations(inventory: Inventory) -> Iterator[Declaration]:
    for fqn, element in inventory.elements.items():
        annotations = element.metadata.annotations
        present = [key for key in EXPECTATION_ANNOTATIONS if key in annotations]
        if not present:
            continue
        expectations: dict[Expectation, None] = {}
        unknown: dict[str, None] = {}
        for key in present:
            found, rest = parse_expectations(annotations[key] or "")
            for expectation in found:
                expectations.setdefault(expectation, None)
            for token in rest:
                unknown.setdefault(token, None)
        yield Declaration(
            element=fqn,
            expectations=tuple(expectations),
            unknown=tuple(unknown),
            key=present[0],
        )


def describe(expectations: Sequence[Expectation]) -> str:
    """``gateway and power`` — the declared set, for one line of a message."""
    names = [str(expectation) for expectation in expectations]
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} and {names[-1]}"
