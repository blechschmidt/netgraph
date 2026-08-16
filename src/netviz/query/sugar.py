"""The old filter flags, written as the query they are.

``--kind switch --kind router --namespace sites/north`` was netviz's first
selector and it is not going away: it is shorter than a query for the thing it
does, it completes in a shell, and it is in every runbook anybody has written.
What it *is* is sugar — every combination of those flags denotes a query — and
this module is where that claim is made executable rather than asserted in a
document.

:func:`as_query` renders a :class:`~netviz.render.graph.FilterSpec` as the
query text it means. ``netviz query --explain`` prints it, ``docs/query.md``
generates its sugar table from it, and ``tests/test_query.py`` checks that the
rendering selects the same elements the flags do — over every example inventory
and over generated ones — which is the only form of the claim that can go stale
noisily instead of quietly.

The translation
---------------

============================  ==============================================
``--namespace NS``            ``namespace under NS``
``--vlan V``                  ``vlan = V``
``--kind K``                  ``kind = K``
``--name G``                  ``name ~ G``
``--neighbors-of N --depth D``  ``within D hops of fqn-or-name N``
============================  ==============================================

Repeats within one flag are alternatives and become ``in (…)`` or a
parenthesised ``or``; different flags are combined with ``and``. That is
:class:`~netviz.render.graph.FilterSpec`'s own rule, restated in the language.
"""

from __future__ import annotations

from netviz.query.lexer import PUNCTUATION
from netviz.render.graph import FilterSpec

__all__ = ["as_query", "quote"]

#: Characters that force a value to be quoted. The word breaks of the lexer,
#: plus the quotes themselves — a value holding any of them would otherwise be
#: read as two tokens.
_NEEDS_QUOTING = PUNCTUATION | frozenset("<>=~!\"' \t\r\n")


def quote(value: str) -> str:
    """Render ``value`` as a query token, quoting it only when it has to be.

    Single quotes, because a query is usually typed inside double quotes in a
    shell. The escape is a backslash, matching the lexer.
    """
    if value and not any(char in _NEEDS_QUOTING for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def as_query(spec: FilterSpec) -> str:
    """The query text ``spec`` means. ``*`` for a spec that filters nothing."""
    clauses: list[str] = []
    if spec.kinds:
        clauses.append(_alternatives("kind", spec.kinds))
    if spec.namespaces:
        clauses.append(
            _joined(
                [f"namespace under {quote(namespace)}" for namespace in spec.namespaces],
                "or",
            )
        )
    if spec.names:
        clauses.append(_joined([f"name ~ {quote(name)}" for name in spec.names], "or"))
    if spec.vlans:
        clauses.append(_alternatives("vlan", [str(vlan) for vlan in sorted(spec.vlans)]))
    if spec.neighbors_of is not None:
        centre = quote(spec.neighbors_of)
        # The flag resolves its argument against the fqn *and* the short name,
        # so the query it means has to say both. `fqn = x or name = x` is that,
        # written out, and it is what makes the equivalence testable rather than
        # approximate.
        seed = f"(fqn = {centre} or name = {centre})"
        clauses.append(f"within {spec.depth} hops of {seed}")
    if not clauses:
        return "*"
    return _joined(clauses, "and")


def _alternatives(attribute: str, values: list[str] | tuple[str, ...]) -> str:
    """``kind = switch`` for one value, ``kind in (switch, router)`` for several."""
    listed = [quote(value) for value in values]
    if len(listed) == 1:
        return f"{attribute} = {listed[0]}"
    return f"{attribute} in ({', '.join(listed)})"


def _joined(clauses: list[str], keyword: str) -> str:
    """Join with ``and``/``or``, parenthesising a multi-clause group.

    A one-clause group is left bare, so the common case — one ``--kind`` — reads
    as ``kind = switch`` and not as ``(kind = switch)``.
    """
    if len(clauses) == 1:
        return clauses[0]
    return "(" + f" {keyword} ".join(clauses) + ")"
