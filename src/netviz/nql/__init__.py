"""NQL — a relational query language over the network the inventory describes.

The selector in :mod:`netviz.query` answers one question: *which elements?* It
is a predicate, and everything about it — one pass, no bindings, no projection —
is shaped by having to run on every keystroke in a browser. It cannot say what a
result should *look* like, and it cannot return anything but elements.

This answers the other questions. "Give me every interface that has an address,
with the element it is attached to." "Which addresses does the server called X
have?" "What is in the broadcast domain of switch Y, and whose ports are they?"
Each of those is a *join* followed by a *projection*, and each wants an object or
an array of objects back, not a list of names.

Why this design
---------------

Three languages were the candidates, and the network model decided between them.

**SQL** projects and nests better than its reputation suggests, but its joins
are explicit: to walk from a server to its addresses a reader has to know that
addresses hang off interfaces, which hang off devices, and spell both joins out.
Following the schema is the *whole* of what these questions do, and a language
in which the common case is the verbose one is the wrong language. Transitive
questions ("everything reachable from here") need a recursive CTE, and nested
output needs the JSON functions.

**Cypher** inverts that: ``MATCH (d:Server)-[:HAS_IF]->(i)`` is a pleasure to
write, and variable-length patterns make reachability a character rather than a
paragraph. But its property graph is untyped, so the relationship names live
outside any schema — nothing can tell the reader that ``HAS_IF`` is not
``HAS_INTERFACE`` until the query returns nothing — and it returns *rows*.
Building a nested object means ``collect()`` and map projections, and the shape
of the result stops being legible from the query.

**EdgeQL** is what netviz already needed. It keeps SQL's set semantics and its
clauses, replaces joins with path navigation over a *typed* schema, and makes
nesting a first-class part of the syntax: ``{ name, interfaces: { mac } }`` is
the result's shape written out. netviz's inventory is already a typed object
graph — that is what ``docs/schema.md`` is — so the schema the language is
checked against is one the project maintains anyway.

So NQL is EdgeQL's shape over netviz's schema, plus two functions borrowed from
Cypher's traversal (``neighbors``, ``reachable``) for the questions no fixed
number of named steps can answer.

What it costs
-------------

One table (:mod:`netviz.nql.schema`) that every name is checked against, and one
pass over the inventory (:mod:`netviz.nql.world`) that materialises the objects
and both directions of every link. After that a query is dictionary lookups.
"""

from __future__ import annotations

from netviz.nql.ast import Query
from netviz.nql.binding import MAX_PARAM_ITEMS, Params, bind, declare, read_assignment
from netviz.nql.describe import EXAMPLES, GRAMMAR, describe, explain, overview
from netviz.nql.execute import Column, Result, execute
from netviz.nql.parser import MAX_DEPTH, MAX_NODES, MAX_PATTERN, is_relational, parse
from netviz.nql.schema import SCHEMA
from netviz.nql.types import Cardinality, ObjectType, ScalarKind, Schema, ValueType
from netviz.nql.world import World, build_world
from netviz.query.errors import QueryError

__all__ = [
    "EXAMPLES",
    "GRAMMAR",
    "MAX_DEPTH",
    "MAX_NODES",
    "MAX_PARAM_ITEMS",
    "MAX_PATTERN",
    "SCHEMA",
    "Cardinality",
    "Column",
    "ObjectType",
    "Params",
    "Query",
    "QueryError",
    "Result",
    "ScalarKind",
    "Schema",
    "ValueType",
    "World",
    "answer",
    "bind",
    "build_world",
    "declare",
    "describe",
    "execute",
    "explain",
    "is_relational",
    "overview",
    "parse",
    "read_assignment",
]


def answer(
    text: str, world: World, *, source: str = "query", params: Params | None = None
) -> Result:
    """Parse ``text`` and run it against ``world``.

    Args:
        text: The query as written.
        source: What a diagnostic calls the origin.
        world: What to answer it from.
        params: A value for each ``$name`` the query uses. Typed by what is
            passed, checked while parsing, substituted while executing — so a
            caller building a query from a template never has to quote
            anything.

    Raises:
        QueryError: The query does not parse, names something the schema does
            not have, names a parameter that was not supplied, or cannot be
            evaluated.
    """
    query = parse(text, source=source, schema=world.schema, params=declare(params))
    return execute(query, world, bind(params))
