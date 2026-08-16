"""What a query may ask about, and what type the answer has.

One table per domain, and the table is the specification: the parser checks
names against it, the evaluator coerces values with it, the CLI completes from
it, ``docs/query.md`` is generated against it and the "did you mean" in an
unknown-attribute diagnostic is computed from it. Adding an attribute is adding
a row and writing the function that reads it (:mod:`netviz.query.facts`);
there is nowhere else it has to be registered, and therefore nowhere it can be
half-registered.

Domains
-------

A query is evaluated over *nodes* of the resolved graph — the elements plus the
derived nodes a layer mints. The other three domains are reached through a
scope term, which is existential: ``interface[…]`` is true of an element when at
least one of its interfaces satisfies the inner query.

============  ==============================================================
``element``   a device, adapter, patch panel, PDU, user, group, or a derived
              subnet / tunnel / rack node
``interface`` one port of an element, flattened as
              :class:`~netviz.render.graph.PortView`
``link``      one link *incident to* the element being tested, so ``peer-*``
              names the far end and ``port`` the near one
``netns``     one network namespace the element runs (§23.1)
``zone``      one firewall zone the element declares (§24.5)
============  ==============================================================

Multi-valued attributes
-----------------------

Half of these hold a set rather than a scalar — an element has many addresses,
many VLANs, many interface names. A comparison against a set is **existential**
for the positive operators (``address in 10.0.0.0/8`` is true when *some*
address is) and therefore **universal** for the negated ones (``address !~
10.*`` is true when *no* address matches). That is the only pair of readings
under which ``X`` and ``not X`` partition the inventory, which is a property the
test suite checks rather than a claim this docstring makes.

An attribute with no values at all is false for every operator, positive or
negative — ``has`` is how emptiness is asked about. So an element with no
addresses matches neither ``address in 10.0.0.0/8`` nor ``address !~ 10.*``, and
``not (address in 10.0.0.0/8)`` — which is a *different* query — catches it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

__all__ = [
    "ATTRIBUTES",
    "DOMAINS",
    "Attribute",
    "Domain",
    "ValueType",
    "attribute_names",
    "lookup",
    "suggestions",
]


class Domain(str, Enum):
    """Which family of thing a predicate is about."""

    ELEMENT = "element"
    INTERFACE = "interface"
    LINK = "link"
    NETNS = "netns"
    ZONE = "zone"

    def __str__(self) -> str:
        return self.value


class ValueType(str, Enum):
    """What the values of an attribute are, and therefore how they compare."""

    #: Free text. ``=`` is exact and case-sensitive, ``~`` is a glob, ``=~`` a
    #: regular expression. ``<`` and friends are refused: string ordering is
    #: never the question somebody is asking of a hostname.
    TEXT = "text"
    #: Text with ``/``-separated segments. Everything TEXT allows, plus
    #: ``under``, which is true of the value itself and of anything below it.
    PATH = "path"
    #: An integer. Orders, so ``<``, ``<=``, ``>`` and ``>=`` all apply.
    NUMBER = "number"
    #: A VLAN id: a number bounded to 1-4094, so a typo is caught at parse time.
    VLAN = "vlan"
    #: An IP address or prefix in ``10.0.0.1/24`` form. ``in`` is containment
    #: inside a CIDR, ``=`` is exact, and the globs work on the text.
    ADDRESS = "address"
    #: ``true`` or ``false``. Only ``=`` and ``!=`` apply.
    BOOL = "bool"
    #: Text drawn from a closed vocabulary, so a misspelling can be met with the
    #: list of what was meant instead of with an empty result.
    ENUM = "enum"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Attribute:
    """One thing a query may name, and everything the language needs about it."""

    #: How it is written. Hyphenated, never underscored — the flags are.
    name: str
    type: ValueType
    #: Does it hold a set of values rather than at most one? See the module
    #: docstring: this is what makes a comparison existential.
    multi: bool
    #: One clause, for ``docs/query.md`` and for shell completion.
    summary: str
    #: Other spellings that mean this attribute. A US/UK pair, a plural, or the
    #: word the equivalent CLI flag uses.
    aliases: tuple[str, ...] = ()
    #: Is this a *family* — ``label.role``, ``label.site`` — rather than one
    #: name? A family matches any attribute with ``<name>.`` as its prefix, and
    #: the part after the dot is passed to the reader.
    family: bool = False

    @property
    def orders(self) -> bool:
        """May ``<``, ``<=``, ``>`` and ``>=`` be written against it?"""
        return self.type in (ValueType.NUMBER, ValueType.VLAN)


def _table(*rows: Attribute) -> dict[str, Attribute]:
    """Index a domain's rows by name and by every alias, refusing a clash."""
    indexed: dict[str, Attribute] = {}
    for row in rows:
        for spelling in (row.name, *row.aliases):
            if spelling in indexed:  # pragma: no cover - a coding error, not input
                raise AssertionError(f"{spelling!r} is declared twice")
            indexed[spelling] = row
    return indexed


#: Everything a query may ask of an element. The order is the order
#: ``docs/query.md`` lists them in, which is grouped rather than alphabetical:
#: identity, then classification, then the network, then the control plane, then
#: the shape of the graph.
_ELEMENT: Final = _table(
    Attribute("name", ValueType.TEXT, False, "The element's short name, without its namespace."),
    Attribute("fqn", ValueType.TEXT, False, "The fully-qualified name.", aliases=("id",)),
    Attribute(
        "namespace",
        ValueType.PATH,
        False,
        "The folder namespace the document lives in.",
        aliases=("ns",),
    ),
    Attribute("kind", ValueType.ENUM, False, "switch, router, cable, user, subnet, …"),
    Attribute(
        "type",
        ValueType.ENUM,
        False,
        "Whether the node is declared (element) or derived (subnet, tunnel, rack, …).",
    ),
    Attribute("description", ValueType.TEXT, False, "metadata.description.", aliases=("desc",)),
    Attribute(
        "label", ValueType.TEXT, False, "metadata.labels.<key>, e.g. label.role.", family=True
    ),
    Attribute("vendor", ValueType.TEXT, False, "spec.vendor."),
    Attribute("model", ValueType.TEXT, False, "spec.model."),
    Attribute("serial", ValueType.TEXT, False, "spec.serial."),
    Attribute("location", ValueType.TEXT, False, "spec.location, as free text."),
    Attribute("vlan", ValueType.VLAN, True, "Every VLAN the element participates in."),
    Attribute(
        "address",
        ValueType.ADDRESS,
        True,
        "Every address configured on any of its interfaces.",
        aliases=("ip",),
    ),
    Attribute(
        "routable-address",
        ValueType.ADDRESS,
        True,
        "The same, with loopback and link-local addresses removed.",
    ),
    Attribute("prefix", ValueType.ADDRESS, True, "The prefix a derived subnet node stands for."),
    Attribute("interface", ValueType.TEXT, True, "The name of each of its interfaces."),
    Attribute("mac", ValueType.TEXT, True, "Each configured MAC address."),
    Attribute("mtu", ValueType.NUMBER, True, "Each configured MTU."),
    Attribute("vrf", ValueType.TEXT, True, "Every VRF it declares or binds an interface to."),
    Attribute("netns", ValueType.TEXT, True, "Every network namespace it runs (§23.1)."),
    Attribute("zone", ValueType.TEXT, True, "Every firewall zone it declares (§24.5)."),
    Attribute("asn", ValueType.NUMBER, False, "The BGP autonomous system number."),
    Attribute("router-id", ValueType.TEXT, False, "The router id, as text."),
    Attribute("area", ValueType.TEXT, False, "The OSPF area it runs."),
    Attribute("degree", ValueType.NUMBER, False, "How many links are incident to it in this view."),
    Attribute("ports", ValueType.NUMBER, False, "How many interfaces it declares."),
    Attribute(
        "file",
        ValueType.PATH,
        False,
        "The inventory-relative path of the document that declares it.",
        aliases=("source",),
    ),
)

#: Everything a query may ask of one interface, inside ``interface[…]``.
_INTERFACE: Final = _table(
    Attribute("name", ValueType.TEXT, False, "The interface name, e.g. GigabitEthernet1/0/1."),
    Attribute("type", ValueType.ENUM, False, "ethernet, loopback, vlan, bridge, tunnel, …"),
    Attribute("description", ValueType.TEXT, False, "Its description.", aliases=("desc",)),
    Attribute("enabled", ValueType.BOOL, False, "Whether it is administratively up."),
    Attribute("address", ValueType.ADDRESS, True, "Each address on it.", aliases=("ip",)),
    Attribute(
        "routable-address",
        ValueType.ADDRESS,
        True,
        "The same, with loopback and link-local addresses removed.",
    ),
    Attribute("mac", ValueType.TEXT, False, "Its MAC address."),
    Attribute("mtu", ValueType.NUMBER, False, "Its MTU."),
    Attribute("vlan", ValueType.VLAN, True, "Every VLAN it is a member of."),
    Attribute("vlan-mode", ValueType.ENUM, False, "access or trunk."),
    Attribute("vrf", ValueType.TEXT, False, "The VRF it is bound to."),
    Attribute("netns", ValueType.TEXT, False, "The network namespace it is in (§23.1)."),
    Attribute("peer", ValueType.TEXT, False, "The other end of the veth pair (§23.2)."),
    Attribute(
        "element", ValueType.TEXT, False, "The fully-qualified name of the element it is on."
    ),
)

#: Everything a query may ask of one incident link, inside ``link[…]``.
_LINK: Final = _table(
    Attribute("id", ValueType.TEXT, False, "The link's identifier.", aliases=("name",)),
    Attribute("kind", ValueType.ENUM, False, "cable, tunnel, adapter, subnet, bgp, power, …"),
    Attribute("medium", ValueType.ENUM, False, "copper, fibre, wireless, …"),
    Attribute("speed", ValueType.NUMBER, False, "The declared speed, in Mbit/s."),
    Attribute("length", ValueType.NUMBER, False, "The declared length, in whole metres."),
    Attribute("label", ValueType.TEXT, False, "The label the link is drawn with."),
    Attribute("vlan", ValueType.VLAN, True, "Every VLAN the link carries."),
    Attribute(
        "port", ValueType.TEXT, False, "The interface on *this* element the link attaches to."
    ),
    Attribute("peer", ValueType.TEXT, False, "Fully-qualified name of the element at the far end."),
    Attribute("peer-name", ValueType.TEXT, False, "Its short name."),
    Attribute("peer-kind", ValueType.ENUM, False, "Its kind."),
    Attribute("peer-namespace", ValueType.PATH, False, "Its namespace."),
    Attribute("peer-port", ValueType.TEXT, False, "The interface at the far end."),
)

#: Everything a query may ask of one network namespace, inside ``netns[…]``.
_NETNS: Final = _table(
    Attribute("name", ValueType.TEXT, False, "The namespace name; empty for the initial one."),
    Attribute("parent", ValueType.TEXT, False, "The namespace it was created inside."),
    Attribute("depth", ValueType.NUMBER, False, "How deeply it is nested; 0 is the initial one."),
    Attribute("description", ValueType.TEXT, False, "Its description.", aliases=("desc",)),
    Attribute("interface", ValueType.TEXT, True, "The name of each interface in it."),
    Attribute("address", ValueType.ADDRESS, True, "Each address in it.", aliases=("ip",)),
)

#: Everything a query may ask of one firewall zone, inside ``zone[…]``.
_ZONE: Final = _table(
    Attribute("name", ValueType.TEXT, False, "The zone's name."),
    Attribute("description", ValueType.TEXT, False, "Its description.", aliases=("desc",)),
    Attribute("interface", ValueType.TEXT, True, "The name of each interface in it."),
    Attribute("rules", ValueType.NUMBER, False, "How many filter rules mention it."),
    Attribute("translations", ValueType.NUMBER, False, "How many address translations do."),
    Attribute("declared", ValueType.BOOL, False, "False for the implicit 'local' and 'any' zones."),
)

#: Every domain's table, keyed by the domain. The single source the parser, the
#: evaluator, the documentation generator and shell completion all read.
ATTRIBUTES: Final[dict[Domain, dict[str, Attribute]]] = {
    Domain.ELEMENT: _ELEMENT,
    Domain.INTERFACE: _INTERFACE,
    Domain.LINK: _LINK,
    Domain.NETNS: _NETNS,
    Domain.ZONE: _ZONE,
}

#: The scope keywords, in the order a diagnostic lists them. Every domain but
#: ``element``, which is where a query starts and so is never written.
DOMAINS: Final[tuple[str, ...]] = tuple(
    domain.value for domain in Domain if domain is not Domain.ELEMENT
)


def lookup(domain: Domain, name: str) -> tuple[Attribute, str] | None:
    """The attribute ``name`` denotes in ``domain``, and its qualifier.

    The qualifier is ``""`` for an ordinary attribute and the part after the dot
    for a family — ``label.role`` resolves to the ``label`` row and ``"role"``.

    Returns ``None`` when the domain has no such attribute, which is what the
    parser turns into a diagnostic with :func:`suggestions` behind it.
    """
    table = ATTRIBUTES[domain]
    found = table.get(name)
    if found is not None and not found.family:
        return found, ""
    head, dot, rest = name.partition(".")
    if dot:
        family = table.get(head)
        if family is not None and family.family and rest:
            return family, rest
    return None


def attribute_names(domain: Domain) -> tuple[str, ...]:
    """Every spelling ``domain`` accepts, in declaration order, aliases included.

    A family is listed with its dot, ``label.``, because that is what has to be
    typed and because a bare ``label`` is not an attribute.
    """
    seen: dict[str, None] = {}
    for spelling, attribute in ATTRIBUTES[domain].items():
        seen[f"{spelling}." if attribute.family else spelling] = None
    return tuple(seen)


def suggestions(domain: Domain, name: str, *, limit: int = 3) -> tuple[str, ...]:
    """The spellings closest to ``name``, for a "did you mean" clause.

    Prefix and substring hits first — a half-typed name is the common mistake —
    then anything within a small edit distance. Deliberately cheap: the tables
    hold a few dozen rows and this runs once, on the way to an error.
    """
    candidates = attribute_names(domain)
    wanted = name.lower().rstrip(".")
    near: list[tuple[int, str]] = []
    for candidate in candidates:
        plain = candidate.rstrip(".")
        if plain == wanted:
            continue
        if plain.startswith(wanted) or wanted.startswith(plain):
            near.append((0, candidate))
        elif wanted in plain or plain in wanted:
            near.append((1, candidate))
        else:
            distance = _distance(wanted, plain)
            if distance <= max(1, min(3, len(wanted) // 3)):
                near.append((2 + distance, candidate))
    near.sort(key=lambda hit: (hit[0], len(hit[1]), hit[1]))
    return tuple(candidate for _, candidate in near[:limit])


def _distance(left: str, right: str, *, ceiling: int = 4) -> int:
    """Damerau-Levenshtein distance, giving up at ``ceiling``.

    Transpositions count as one edit rather than two, which is the whole reason
    this is not plain Levenshtein: ``kidn`` for ``kind`` is the mistake people
    actually make, and under the plain metric it is two edits away from ``kind``
    and two from ``id`` — so the shorter, wronger suggestion wins.

    The give-up keeps this cheap: an attribute name is a couple of dozen
    characters at most, and anything further away than a few edits is not a
    suggestion worth making.
    """
    if abs(len(left) - len(right)) > ceiling:
        return ceiling + 1
    before: list[int] = []
    previous = list(range(len(right) + 1))
    for i, one in enumerate(left, start=1):
        current = [i]
        for j, other in enumerate(right, start=1):
            cost = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (one != other))
            if i > 1 and j > 1 and one == right[j - 2] and left[i - 2] == other:
                cost = min(cost, before[j - 2] + 1)
            current.append(cost)
        if min(current) > ceiling:
            return ceiling + 1
        before, previous = previous, current
    return previous[-1]
