"""One selector language, for the CLI, the renderer, the tests and the editor.

Element selection used to be spelled a different way in every place that needed
it: fixed keyword predicates in :func:`~netviz.render.graph.filter_graph`, its
own flags on ``netviz list``, its own matchers in ``netviz test``, and a
substring match in the editor's search box. None of the four could express the
questions operators actually ask, and no two of them could express quite the
same subset of the ones they could.

This package is the single implementation. A **query** is:

* a predicate over the resolved model — ``kind = switch``, ``label.role =
  access``, ``address in 10.20.0.0/16``, ``has vrf``;
* over sub-objects, existentially — ``interface[address in 10.20.0.0/16 and not
  has vrf]``, ``link[peer-kind = router]``;
* plus bounded graph traversal — ``neighbors of X``, ``within 2 hops of X``,
  ``reachable from X``;
* combined with ``and``, ``or``, ``not`` and parentheses.

and it is deliberately nothing else. There is no binding, no arithmetic, no
call and no recursion beyond the finite tree the parser builds, so every query
terminates and none of them can change anything. That is not asceticism: it is
what makes the same expression safe to run on every keystroke in a browser, in a
pre-commit hook, and inside an assertion somebody will not read again for a
year.

Where it is used
----------------

===============================  ==========================================
``netviz query '<expr>'``      print the matches, ``--json``, ``--count``
``--select '<expr>'``            on render, watch, show, list, export, report
``assert: query`` (§20)          an assertion that is one line
the editor's search box          highlight, filter, and select in bulk
===============================  ==========================================

Everywhere but the first, the query is *layered over* the existing filter rather
than replacing it: the flags keep working, they are documented as sugar for the
equivalent query (:mod:`netviz.query.sugar`), and both narrow the same
:class:`~netviz.render.graph.Graph` through the same
:func:`~netviz.render.graph.filter_graph`.

The modules
-----------

===========================  ================================================
:mod:`~netviz.query.lexer`       text to tokens, each carrying its span
:mod:`~netviz.query.ast`         the nine node types
:mod:`~netviz.query.parser`      tokens to a checked tree, with carets
:mod:`~netviz.query.attributes`  the vocabulary, as tables
:mod:`~netviz.query.facts`       reading an attribute off the model
:mod:`~netviz.query.evaluate`    the tree against a graph
:mod:`~netviz.query.sugar`       the old flags, as the query they mean
:mod:`~netviz.query.errors`      the diagnostic, and its caret
===========================  ================================================
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from netviz.query.ast import Expr, Operator, TraversalKind
from netviz.query.attributes import (
    ATTRIBUTES,
    DOMAINS,
    Attribute,
    Domain,
    ValueType,
    attribute_names,
)
from netviz.query.errors import MAX_QUERY_LENGTH, QueryError
from netviz.query.evaluate import QueryResult, Witness, evaluate, matches
from netviz.query.parser import MAX_DEPTH, MAX_HOPS, MAX_TERMS, Query, parse
from netviz.query.sugar import as_query

if TYPE_CHECKING:
    from netviz.render.graph import Graph

__all__ = [
    "ATTRIBUTES",
    "DOMAINS",
    "MAX_DEPTH",
    "MAX_HOPS",
    "MAX_QUERY_LENGTH",
    "MAX_TERMS",
    "Attribute",
    "Domain",
    "Expr",
    "Operator",
    "Query",
    "QueryError",
    "QueryResult",
    "TraversalKind",
    "ValueType",
    "Witness",
    "as_query",
    "attribute_names",
    "evaluate",
    "matches",
    "parse",
    "select",
]


def select(text: str, graph: Graph, *, source: str = "query") -> QueryResult:
    """Parse ``text`` and answer it against ``graph``, in one call.

    The convenience every caller outside this package wants: nothing downstream
    keeps a parsed query around, because the graph it would be evaluated against
    is rebuilt whenever the inventory changes.

    Raises:
        QueryError: The query cannot be parsed. The message underlines the
            offending column.
    """
    return evaluate(parse(text, source=source), graph)
