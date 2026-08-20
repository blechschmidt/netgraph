"""Turning a result into the four shapes the command line prints it in.

JSON and YAML are the honest ones: a result may nest arbitrarily, and both carry
that without loss. A table and a CSV are flat by construction, so a nested field
is flattened into one cell — a list becomes ``a, b, c`` and a projected object
becomes its values joined by a space. That is a *display* decision and it is
made here, once, rather than in each renderer, so the table and the CSV agree.

The column headings come from the shape, not from the data: a query that
selected nothing still prints its headings, which is what tells a reader that
the query ran and matched nothing rather than that it returned junk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from netviz.console import Align
from netviz.nql.ast import Call, FreeObject, Root, Select, Step, Var
from netviz.nql.execute import Result

__all__ = ["headings", "label", "payload", "table", "type_name"]

#: What an empty cell prints as, matching every other netviz table.
EMPTY = "-"


def label(result: Result) -> str:
    """What to call the single column of an unshaped result.

    Read off the query rather than off the values: ``select interface.mac``
    prints a ``MAC`` column even when no interface has one.
    """
    node: Any = result.query.body
    while isinstance(node, Select):
        node = node.source
    if isinstance(node, Step):
        return node.member
    if isinstance(node, Root):
        return node.type_name
    if isinstance(node, Call):
        return node.name
    if isinstance(node, Var):
        return node.name
    return "value"


def headings(result: Result) -> tuple[str, ...]:
    """One heading per column, upper-cased the way every netviz table is."""
    if result.columns:
        return tuple(column.name.upper() for column in result.columns)
    return (label(result).upper(),)


def table(result: Result) -> tuple[list[list[str]], tuple[Align, ...]]:
    """The rows as text, and the alignment of each column."""
    if result.columns:
        rows = [
            [
                cell(row.get(column.name) if isinstance(row, Mapping) else row)
                for column in result.columns
            ]
            for row in result.rows
        ]
        aligns: tuple[Align, ...] = tuple(
            "right" if column.numeric else "left" for column in result.columns
        )
        return rows, aligns
    numeric = result.type.scalar is not None and result.type.scalar.is_numeric
    single: Align = "right" if numeric else "left"
    return [[cell(row)] for row in result.rows], (single,)


def cell(value: Any) -> str:
    """One value as one cell of a flat table."""
    if value is None:
        return EMPTY
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Mapping):
        return " ".join(cell(one) for one in value.values()) or EMPTY
    if isinstance(value, Sequence) and not isinstance(value, str):
        return ", ".join(cell(one) for one in value) or EMPTY
    return str(value)


def payload(result: Result, *, count_only: bool = False) -> dict[str, Any]:
    """The JSON and YAML document: the query, how much matched, and what.

    The query text travels with the answer so that a stored result says what
    produced it — the same reason ``netviz query --json`` has always carried it
    for the selector.
    """
    document: dict[str, Any] = {
        "query": result.query.text,
        "count": len(result),
        "type": type_name(result),
    }
    if not count_only:
        document["results"] = list(result.rows)
    return document


def type_name(result: Result) -> str:
    """What the query returns, as a reader would say it.

    A free object has no schema type — it is assembled by the query — so
    ``str(ValueType)`` would call it "empty", which is true of its *type* and
    false of the thing. It is one object, and that is what the document says.
    """
    if isinstance(result.query.body, FreeObject):
        return f"{result.type.card} object"
    return str(result.type)
