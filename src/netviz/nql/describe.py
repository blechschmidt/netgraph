"""What the language can be asked, printed for a reader at the terminal.

``netviz query --describe`` prints the grammar, the types and the functions;
``--describe TYPE`` prints one type's members. Both read the same tables the
parser checks against, so the help can never drift from what is accepted: a
member that is not in :data:`~netviz.nql.schema.SCHEMA` is neither documented
nor parseable, and one that is, is both.
"""

from __future__ import annotations

from typing import Final

from netviz.nql.functions import FUNCTIONS, Shape
from netviz.nql.schema import SCHEMA
from netviz.nql.types import Link, Schema

__all__ = ["EXAMPLES", "GRAMMAR", "describe", "explain", "overview"]

#: The grammar, as ``--explain`` prints it. Kept next to the parser's docstring
#: rather than generated from it, because a grammar a reader can scan is not the
#: same text as the one a maintainer reads.
GRAMMAR: Final[tuple[str, ...]] = (
    "query      := [ with NAME := expr, ... ] expr",
    "expr       := select expr [ shape ] [ clause ... ]  |  <value expression>",
    "clause     := filter expr            -- 'where' is a synonym",
    "            | order by expr [asc|desc] [ then expr ... ]",
    "            | limit expr | offset expr",
    "shape      := { field, ... }",
    "field      := NAME                   -- project a property or a link",
    "            | NAME : shape [clause]  -- project linked objects, shaped",
    "            | NAME := expr [shape]   -- project anything, under a new name",
    "            | *                      -- every property of the type",
    "path       := .NAME ...              -- from the object being considered",
    "            | TYPE.NAME ...          -- from every object of a type",
    "            | expr[is TYPE]          -- narrow a polymorphic link",
    "operators  := or and not  =  != < <= > >  in  like ilike ~ !~ =~ under  is [not]",
    "            + - * / %  ++   exists  distinct",
)

#: Worked answers to the questions the language exists for. Printed by
#: ``--explain`` because a grammar teaches nobody a query language.
EXAMPLES: Final[tuple[tuple[str, str], ...]] = (
    (
        "every interface that has an IP address, and what it is attached to",
        "select interface { name, parent: { fqn, kind }, addresses: { address } }\n"
        "filter exists .addresses",
    ),
    (
        "the interfaces in a switch's broadcast domains, and their devices",
        "select broadcast_domain { name, interfaces: { fqn, parent: { name, kind } } }\n"
        "filter .members.name = 'sw-core-01'",
    ),
    (
        "the addresses of one server -- filter first, then walk",
        "select (server filter .name = 'srv-app-01').addresses.address",
    ),
    (
        "every MAC address a server has, with the port it is on",
        "select interface { mac, port := .name, server := .parent.name }\n"
        "filter .parent is server and exists .mac",
    ),
    (
        "a summary object rather than a list",
        "select { devices := count(device), addressed := count(interface filter "
        "exists .addresses) }",
    ),
    (
        "the ten fullest subnets",
        "select subnet { prefix, used, size, utilisation } order by .utilisation desc limit 10",
    ),
)


def overview(schema: Schema = SCHEMA) -> tuple[str, ...]:
    """One line per type: its name and what it is."""
    width = max(len(one.name) for one in schema)
    return tuple(
        f"  {one.name:<{width}}  {'(abstract) ' if one.abstract else ''}{one.summary}"
        for one in schema
    )


def describe(type_name: str, schema: Schema = SCHEMA) -> tuple[str, ...]:
    """Every member of one type, inherited ones included, with its cardinality."""
    found = schema.resolve(type_name)
    if found is None:
        return ()
    lines = [f"{found.name} -- {found.summary}"]
    if found.bases:
        lines.append(f"  inherits: {', '.join(found.bases)}")
    subtypes = [one for one in schema.concrete(found.name) if one != found.name]
    if subtypes:
        lines.append(f"  subtypes: {', '.join(subtypes)}")
    if found.aliases:
        lines.append(f"  also spelled: {', '.join(found.aliases)}")
    members = list(schema.members(found.name).values())
    unique = list(dict.fromkeys(members))
    width = max((len(one.describe()) for one in unique), default=0)
    properties = [one for one in unique if not isinstance(one, Link)]
    links = [one for one in unique if isinstance(one, Link)]
    if properties:
        lines.append("")
        lines.append("  properties")
        lines.extend(f"    {one.describe():<{width}}  {one.summary}" for one in properties)
    if links:
        lines.append("")
        lines.append("  links")
        lines.extend(f"    {one.describe():<{width}}  {one.summary}" for one in links)
    return tuple(lines)


def explain(schema: Schema = SCHEMA) -> tuple[str, ...]:
    """The grammar, the vocabulary and the worked examples, as one block."""
    lines: list[str] = ["# grammar"]
    lines.extend(f"  {one}" for one in GRAMMAR)
    lines.extend(("", "# types -- 'netviz query --describe TYPE' lists what each holds"))
    lines.extend(overview(schema))
    width = max(len(one.signature()) for one in FUNCTIONS.values())
    for shape, note in (
        (Shape.AGGREGATE, "collapse a whole set to one value"),
        (Shape.ELEMENTWISE, "apply to every combination of their arguments"),
        (Shape.GRAPH, "walk the links, which no fixed number of steps can"),
    ):
        lines.extend(("", f"# {shape} functions -- {note}"))
        lines.extend(
            f"  {one.signature():<{width}}  {one.summary}"
            for one in FUNCTIONS.values()
            if one.shape is shape
        )
    lines.extend(("", "# examples"))
    for question, query in EXAMPLES:
        lines.append(f"  # {question}")
        lines.extend(f"  {one}" for one in query.splitlines())
        lines.append("")
    return tuple(lines[:-1] if lines and not lines[-1] else lines)
