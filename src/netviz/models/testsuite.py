"""The ``testsuite`` document kind: executable assertions about the network (§20).

An inventory is a source of truth, and a source of truth that nobody checks is a
document that quietly stops being true. Every other kind in this schema says what
the network *is*; a ``kind: testsuite`` document says what somebody is *relying
on* — that the tills reach the payment gateway, that the guest VLAN reaches
nothing else, that no management address is used twice — and ``netviz test``
grades it::

    apiVersion: netviz.dev/v1alpha1
    kind: testsuite
    metadata:
      name: connectivity
    spec:
      assertions:
        - assert: reachable
          name: every desk reaches the gateway
          from: pc-alice
          to: rtr-edge
        - assert: not-reachable
          from: guest-tablet
          to: srv-payroll
        - assert: unique
          select: kind=switch
          field: spec.interfaces[name=Loopback0].ipv4[]

**A test suite is not an element.** Like :mod:`~netviz.models.layout` and
:mod:`~netviz.models.template` it declares no device, terminates no cable, is
never drawn as a node and is never listed by ``netviz list``. It is indexed
apart from the elements, so a suite called ``core`` next to a switch called
``core`` is not a clash — nothing ever resolves one where the other is meant.

Why one flat assertion model
----------------------------

Eleven assertion kinds, each with its own required fields, is the shape of a
discriminated union — and it is deliberately not written as one. A union of
eleven models puts eleven near-identical envelopes in the JSON Schema, eleven
tables in the generated reference and eleven entries in every table that has to
name a model, all to express what one :func:`~pydantic.model_validator` says in
a paragraph. The cost of the flat model is that ``extra="forbid"`` cannot tell
``hops`` on a ``same-vlan`` from a typo — so :meth:`Assertion._check_shape` does,
by name, with a message that says which key belongs to which assertion. The
diagnostic is better than pydantic's union error would have been, which is the
whole argument.

Why the selector is a string
----------------------------

``select: kind=switch, namespace=sites/north`` is the same vocabulary
``netviz render --kind switch --namespace sites/north`` already parses, spelled
as one scalar because a YAML document has nowhere to put a repeated flag. It
parses to the very same :class:`~netviz.render.graph.FilterSpec` the renderer
filters with (:mod:`netviz.testing.selectors`), so an assertion and a diagram
cannot disagree about what ``kind=switch`` selects, and nobody has to learn a
second query language to write a test.
"""

from __future__ import annotations

from enum import Enum
from typing import Final, Literal

from pydantic import Field, model_validator

from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.metadata import Metadata
from netviz.models.scalars import ApiVersion, VlanId

__all__ = [
    "MAX_ASSERTIONS",
    "QUERY_ASSERTIONS",
    "REACHABILITY_ASSERTIONS",
    "SELECTOR_ASSERTIONS",
    "TEST_SUITE_KIND",
    "Assertion",
    "AssertionLayer",
    "AssertionType",
    "TestSuite",
    "TestSuiteSpec",
]

#: ``kind`` of a suite of assertions. Named once so nothing downstream spells it
#: out. Lower case like every other kind in §3: ``patchpanel``, not ``PatchPanel``.
TEST_SUITE_KIND: Final = "testsuite"

#: ``NV-K002`` — ceiling on the assertions one suite may hold. A suite longer
#: than this is several suites that have not been split, and every assertion
#: costs at least one graph search.
MAX_ASSERTIONS: Final = 1024


class AssertionType(str, Enum):
    """What an assertion claims. The value is the word written in ``assert``."""

    #: A path exists from ``from`` to ``to``.
    REACHABLE = "reachable"
    #: No path exists from ``from`` to ``to``. The assertion segmentation is for.
    NOT_REACHABLE = "not-reachable"
    #: A path exists and crosses fewer than ``hops`` links.
    PATH_SHORTER_THAN = "path-shorter-than"
    #: Every selected element shares at least one VLAN with every other.
    SAME_VLAN = "same-vlan"
    #: No two selected elements share a VLAN.
    DISTINCT_VLAN = "distinct-vlan"
    #: Every routable address on a selected element lies inside ``prefix``.
    WITHIN_PREFIX = "within-prefix"
    #: Every selected element has an interface whose name matches ``interface``.
    HAS_INTERFACE = "has-interface"
    #: Every selected element declares at least ``ports`` interfaces.
    PORT_COUNT_AT_LEAST = "port-count-at-least"
    #: No two selected elements produce the same value for ``field``.
    UNIQUE = "unique"
    #: How many elements the selector matches, compared against a bound.
    COUNT = "count"
    #: No single element or link, on its own, cuts anything off.
    NO_SINGLE_POINT_OF_FAILURE = "no-single-point-of-failure"
    #: A selector query, graded against how much it matches. The general form:
    #: anything the language can express is an assertion, and the default claim
    #: — "this matches nothing" — is how a network invariant is written, because
    #: an invariant is a search for counterexamples (§20.3).
    QUERY = "query"

    def __str__(self) -> str:
        return self.value


class AssertionLayer(str, Enum):
    """Which view of the network an assertion is made about."""

    #: Whichever layer answers first: switched if there is a switched path,
    #: routed otherwise. The default, and what ``netviz path`` does.
    ANY = "any"
    #: The physical topology, cables and patch segments.
    L1 = "l1"
    #: Broadcast domains.
    L2 = "l2"
    #: Routed adjacency.
    L3 = "l3"
    #: The power plan: which PDU feeds what.
    POWER = "power"

    def __str__(self) -> str:
        return self.value


#: The assertions that trace a route, and therefore need ``from`` and ``to``.
REACHABILITY_ASSERTIONS: Final[frozenset[AssertionType]] = frozenset(
    {
        AssertionType.REACHABLE,
        AssertionType.NOT_REACHABLE,
        AssertionType.PATH_SHORTER_THAN,
    }
)

#: The assertions made about a *set* of elements, and therefore about a selector.
SELECTOR_ASSERTIONS: Final[frozenset[AssertionType]] = frozenset(
    {
        AssertionType.SAME_VLAN,
        AssertionType.DISTINCT_VLAN,
        AssertionType.WITHIN_PREFIX,
        AssertionType.HAS_INTERFACE,
        AssertionType.PORT_COUNT_AT_LEAST,
        AssertionType.UNIQUE,
        AssertionType.COUNT,
    }
)

#: The assertions that take a ``query`` instead of, or as well as, a ``select``.
#: Every selector assertion does — one selector language means an assertion that
#: took the old spelling takes the new one — plus ``query`` itself, whose whole
#: subject it is, and ``no-single-point-of-failure``, which narrows the
#: candidates it reports.
QUERY_ASSERTIONS: Final[frozenset[AssertionType]] = SELECTOR_ASSERTIONS | {
    AssertionType.QUERY,
    AssertionType.NO_SINGLE_POINT_OF_FAILURE,
}

#: Layers a reachability assertion may name. ``l1`` is not one of them: a trace
#: answers "how does A reach B", and a cable is not an answer to that.
_TRACE_LAYERS: Final[frozenset[AssertionLayer]] = frozenset(
    {AssertionLayer.ANY, AssertionLayer.L2, AssertionLayer.L3}
)

#: Layers a single-point-of-failure assertion may name. ``any`` means all four.
_SPOF_LAYERS: Final[frozenset[AssertionLayer]] = frozenset(
    {
        AssertionLayer.ANY,
        AssertionLayer.L1,
        AssertionLayer.L2,
        AssertionLayer.L3,
        AssertionLayer.POWER,
    }
)

#: Every key that is only meaningful for some assertions, and which those are.
#: One table, so the model, the schema and the documentation cannot disagree
#: about which combination is legal.
_KEYS: Final[dict[str, frozenset[AssertionType]]] = {
    "from": REACHABILITY_ASSERTIONS,
    "to": REACHABILITY_ASSERTIONS,
    "max_hops": REACHABILITY_ASSERTIONS,
    "hops": frozenset({AssertionType.PATH_SHORTER_THAN}),
    "vlan": REACHABILITY_ASSERTIONS | {AssertionType.SAME_VLAN},
    "layer": REACHABILITY_ASSERTIONS | {AssertionType.NO_SINGLE_POINT_OF_FAILURE},
    "select": SELECTOR_ASSERTIONS | {AssertionType.NO_SINGLE_POINT_OF_FAILURE},
    "query": QUERY_ASSERTIONS,
    "prefix": frozenset({AssertionType.WITHIN_PREFIX}),
    "interface": frozenset({AssertionType.HAS_INTERFACE}),
    "ports": frozenset({AssertionType.PORT_COUNT_AT_LEAST}),
    "field": frozenset({AssertionType.UNIQUE}),
    "equals": frozenset({AssertionType.COUNT, AssertionType.QUERY}),
    "at_least": frozenset({AssertionType.COUNT, AssertionType.QUERY}),
    "at_most": frozenset({AssertionType.COUNT, AssertionType.QUERY}),
    "min_isolated": frozenset({AssertionType.NO_SINGLE_POINT_OF_FAILURE}),
}

#: The keys each assertion cannot do without, in the order a diagnostic lists them.
_REQUIRED: Final[dict[AssertionType, tuple[str, ...]]] = {
    AssertionType.REACHABLE: ("from", "to"),
    AssertionType.NOT_REACHABLE: ("from", "to"),
    AssertionType.PATH_SHORTER_THAN: ("from", "to", "hops"),
    AssertionType.SAME_VLAN: ("select",),
    AssertionType.DISTINCT_VLAN: ("select",),
    AssertionType.WITHIN_PREFIX: ("select", "prefix"),
    AssertionType.HAS_INTERFACE: ("select", "interface"),
    AssertionType.PORT_COUNT_AT_LEAST: ("select", "ports"),
    AssertionType.UNIQUE: ("select", "field"),
    AssertionType.COUNT: ("select",),
    AssertionType.NO_SINGLE_POINT_OF_FAILURE: (),
    AssertionType.QUERY: ("query",),
}


class Assertion(NetvizModel):
    """One claim about the network, and everything needed to grade it (§20.2)."""

    #: What is being claimed. Every other key is read in the light of this one.
    assert_: AssertionType = Field(alias="assert")
    #: How the claim is reported — a sentence a reader who has never seen the
    #: inventory can act on. Defaults to a description built from the fields.
    name: str | None = Field(default=None, max_length=200)
    #: Why the claim is made. Printed under a failure, so it is the right place
    #: for the ticket number or the standard that demands it.
    description: str | None = None

    # -- reachability ----------------------------------------------------
    #: Where the trace starts: an element, ``element:interface``, an IP address,
    #: or a selector matching several of them. Same spellings as ``netviz path``.
    from_: str | None = Field(default=None, alias="from", min_length=1)
    #: Where the trace ends, in the same four spellings.
    to: str | None = Field(default=None, min_length=1)
    #: Fail a route that crosses more links than this before it is found.
    max_hops: int | None = Field(default=None, ge=1, le=64)
    #: ``path-shorter-than``: the exclusive upper bound on the hop count.
    hops: int | None = Field(default=None, ge=1, le=64)
    #: Restrict the trace to one VLAN, or pin which VLAN ``same-vlan`` means.
    vlan: VlanId | None = None
    #: Which view the claim is about. See :class:`AssertionLayer`.
    layer: AssertionLayer | None = None

    # -- selector --------------------------------------------------------
    #: Which elements the claim is about, in ``netviz render``'s filter
    #: vocabulary: ``kind=switch, namespace=sites/north, name=sw-*``.
    select: str | None = Field(default=None, min_length=1)
    #: The same thing said in the selector language (:mod:`netviz.query`),
    #: which can say things the vocabulary above cannot: ``kind in (switch,
    #: router) and not interface[name ~ 'Vlan*' and has address]``. Either key
    #: supplies the elements a selector assertion is graded over; ``assert:
    #: query`` takes this one and nothing else, and grades the match count
    #: against ``equals`` / ``at_least`` / ``at_most`` — defaulting, when none is
    #: given, to the claim that the query matches *nothing*.
    query: str | None = Field(default=None, min_length=1)
    #: ``within-prefix``: the CIDR every selected address must lie inside.
    prefix: str | None = Field(default=None, min_length=1)
    #: ``has-interface``: the interface name, or a glob matching it.
    interface: str | None = Field(default=None, min_length=1)
    #: ``port-count-at-least``: the inclusive lower bound on the port count.
    ports: int | None = Field(default=None, ge=0)
    #: ``unique``: the field expression whose values must all differ, e.g.
    #: ``spec.interfaces[name=mgmt0].ipv4[]``.
    field: str | None = Field(default=None, min_length=1)
    #: ``count``: how many elements the selector must match, exactly.
    equals: int | None = Field(default=None, ge=0)
    #: ``count``: the inclusive lower bound on how many it must match.
    at_least: int | None = Field(default=None, ge=0)
    #: ``count``: the inclusive upper bound on how many it must match.
    at_most: int | None = Field(default=None, ge=0)
    #: ``no-single-point-of-failure``: ignore a candidate that isolates fewer
    #: endpoints than this. 1 reports every one of them.
    min_isolated: int | None = Field(default=None, ge=1)

    @property
    def type(self) -> AssertionType:
        """``assert``, under a name that is not a Python keyword."""
        return self.assert_

    @property
    def source(self) -> str | None:
        """``from``, under a name that is not a Python keyword."""
        return self.from_

    @property
    def title(self) -> str:
        """What a report calls this assertion: ``name``, or a rendering of it."""
        return self.name or self.describe()

    def describe(self) -> str:
        """The assertion as a clause, for a suite that did not name it."""
        if self.assert_ in REACHABILITY_ASSERTIONS:
            bound = f" in under {self.hops} hops" if self.hops is not None else ""
            return f"{self.assert_}: {self.from_} -> {self.to}{bound}"
        if self.assert_ is AssertionType.QUERY:
            return f"query: {self.query}" + (f" ({self._bounds()})" if self._bounds() else "")
        subject = self.select or self.query
        if self.assert_ is AssertionType.NO_SINGLE_POINT_OF_FAILURE:
            return f"{self.assert_}{f' among {subject}' if subject else ''}"
        detail = {
            AssertionType.WITHIN_PREFIX: self.prefix,
            AssertionType.HAS_INTERFACE: self.interface,
            AssertionType.PORT_COUNT_AT_LEAST: str(self.ports),
            AssertionType.UNIQUE: self.field,
            AssertionType.COUNT: self._bounds(),
        }.get(self.assert_)
        return f"{self.assert_} of {subject}" + (f": {detail}" if detail else "")

    def _bounds(self) -> str:
        """The ``count`` comparison as it is written in a message."""
        parts = [
            f"{word} {value}"
            for word, value in (
                ("==", self.equals),
                (">=", self.at_least),
                ("<=", self.at_most),
            )
            if value is not None
        ]
        return ", ".join(parts)

    @model_validator(mode="after")
    def _check_shape(self) -> Assertion:
        """``NV-K003`` — every key present belongs to this assertion, and none is missing."""
        for key, owners in _KEYS.items():
            if getattr(self, _attribute(key)) is not None and self.assert_ not in owners:
                raise field_error(
                    f"{key!r} is not a key of a {self.assert_!s} assertion; it belongs to "
                    f"{_listed(owners)}",
                    rule="NV-K003",
                    path=(key,),
                )
        for key in _REQUIRED[self.assert_]:
            if key == "select" and self.query is not None:
                # 'select' and 'query' are two spellings of the same thing, so
                # either satisfies the requirement. Both is not an error either:
                # they are ANDed, which is how "the switches, and of those the
                # ones with no uplink" is written without a single long query.
                continue
            if getattr(self, _attribute(key)) is None:
                raise field_error(
                    f"a {self.assert_!s} assertion needs {key!r}"
                    + (" or 'query'" if key == "select" else ""),
                    rule="NV-K003",
                    path=(key,),
                )
        self._check_layer()
        self._check_count()
        return self

    def _check_layer(self) -> None:
        """``NV-K003`` — the named view is one this assertion can be made about."""
        if self.layer is None:
            return
        allowed = (
            _SPOF_LAYERS
            if self.assert_ is AssertionType.NO_SINGLE_POINT_OF_FAILURE
            else _TRACE_LAYERS
        )
        if self.layer not in allowed:
            raise field_error(
                f"a {self.assert_!s} assertion cannot be made about layer {self.layer!s}; "
                f"expected one of {', '.join(sorted(str(layer) for layer in allowed))}",
                rule="NV-K003",
                path=("layer",),
            )

    def _check_count(self) -> None:
        """``NV-K003`` — a ``count`` compares against something, and not against nothing.

        A ``query`` assertion is exempt from the first half: with no bound it
        means "this matches nothing", which is a claim and the most useful one.
        The second half — a range no number can be in — is a mistake either way.
        """
        if self.assert_ not in (AssertionType.COUNT, AssertionType.QUERY):
            return
        if (
            self.assert_ is AssertionType.COUNT
            and self.equals is None
            and self.at_least is None
            and self.at_most is None
        ):
            raise field_error(
                "a count assertion needs at least one of 'equals', 'at_least' or 'at_most'; "
                "without one it claims nothing",
                rule="NV-K003",
                path=("equals",),
            )
        if self.at_least is not None and self.at_most is not None and self.at_least > self.at_most:
            raise field_error(
                f"at_least {self.at_least} is above at_most {self.at_most}, so no count "
                f"can satisfy this assertion",
                rule="NV-K003",
                path=("at_least",),
            )


def _attribute(key: str) -> str:
    """The model attribute holding the value written under YAML key ``key``."""
    return "from_" if key == "from" else key


def _listed(kinds: frozenset[AssertionType]) -> str:
    """The assertion kinds owning a key, in the order §20.2 lists them."""
    ordered = [str(kind) for kind in AssertionType if kind in kinds]
    return ", ".join(ordered)


class TestSuiteSpec(NetvizModel):
    """``spec`` of a ``testsuite`` document (§20.1)."""

    #: What the suite is for, in one line. Printed as the suite's progress line.
    description: str | None = None
    #: The claims, graded in the order they are written. A suite that asserts
    #: nothing is refused (``NV-K002``): it would report a green run having
    #: checked nothing, which is worse than no suite at all.
    assertions: list[Assertion] = Field(min_length=1, max_length=MAX_ASSERTIONS)


class TestSuite(NetvizModel):
    """A ``kind: testsuite`` document: named assertions about the network (§20)."""

    api_version: ApiVersion = Field(alias="apiVersion", serialization_alias="apiVersion")
    kind: Literal["testsuite"] = "testsuite"
    metadata: Metadata
    spec: TestSuiteSpec

    @property
    def name(self) -> str:
        """Shortcut for ``metadata.name``."""
        return self.metadata.name

    @property
    def assertions(self) -> list[Assertion]:
        """The claims this suite makes, in document order."""
        return self.spec.assertions

    def __str__(self) -> str:
        return f"{TEST_SUITE_KIND}/{self.metadata.name}"
