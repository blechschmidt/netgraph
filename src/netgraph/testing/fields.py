"""Reading a value out of an element by path, for the ``unique`` assertion.

"Every management address is distinct" is a claim about a *field*, and the field
is not always a scalar sitting at the top of the document. It is one address in
one list on one interface out of forty-eight. So ``unique`` takes a path::

    field: spec.interfaces[name=mgmt0].ipv4[]

and this module evaluates it against the element as the loader resolved it —
ranges expanded, template merged, shorthands normalised — so the assertion sees
what the graph sees rather than what the file happens to say.

The grammar
-----------

Dotted keys, each optionally followed by one or more bracket steps:

``[]``
    Flatten: the value is a list, and evaluation continues once per item. Two
    of them in a path (``interfaces[].ipv4[]``) is a nested flatten, which is
    exactly what "every address on every interface" means.
``[key=value]``, ``[key!=value]``
    Filter: the value is a list of mappings, and only those whose ``key``
    matches (or does not match) ``value`` continue. ``value`` is a shell glob,
    so ``[name=Gi0/0/*]`` selects a slot. The negated form is what makes "every
    address except the loopback" writable —
    ``spec.interfaces[type!=loopback].ipv4.addresses[].ip`` — and without it the
    single most useful ``unique`` assertion, that no two hosts share an address,
    would be defeated by the ``127.0.0.1`` every host declares.

A path that runs off the end of the document — a key no element has, an index
into something that is not a list — yields **nothing** rather than raising. That
is the useful behaviour for ``unique``: a device that has no ``mgmt0`` cannot
collide with one that does, and the assertion says how many elements produced a
value so a silent zero cannot be mistaken for a pass.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from netgraph.errors import echo_value

__all__ = ["FieldError", "evaluate", "render_value"]

#: One path component: a key, then any number of ``[]`` or ``[k=v]`` steps.
_STEP: Final = re.compile(r"^(?P<key>[^.\[\]]+)(?P<steps>(?:\[[^\[\]]*\])*)$")
_BRACKET: Final = re.compile(r"\[([^\[\]]*)\]")

#: Ceiling on how many values one expression may yield for one element. A path
#: of three nested flattens over a 4096-port document is a mistake, and the
#: answer to a mistake is a diagnostic rather than an out-of-memory kill.
MAX_VALUES: Final = 10_000


class FieldError(ValueError):
    """A field expression that cannot be read as a path."""


def evaluate(document: Mapping[str, Any], expression: str) -> list[Any]:
    """Every value ``expression`` addresses in ``document``.

    Args:
        document: One element, dumped with ``model_dump(mode="json")`` so the
            values are plain JSON types and two of them compare by value.
        expression: The path, e.g. ``spec.interfaces[name=mgmt0].ipv4[]``.

    Returns:
        The values found, in document order. Empty when the path addresses
        nothing, which is not an error — see the module docstring.

    Raises:
        FieldError: The expression is not a path, or it yields more than
            :data:`MAX_VALUES` values.
    """
    values: list[Any] = [document]
    for component in _components(expression):
        values = list(_step(values, component))
        if len(values) > MAX_VALUES:
            raise FieldError(
                f"field expression {echo_value(expression)} yields more than {MAX_VALUES} "
                f"values for one element; narrow it with a [key=value] filter"
            )
    return values


def _components(expression: str) -> Iterator[tuple[str, tuple[str, ...]]]:
    """``expression`` as ``(key, bracket steps)`` pairs."""
    text = expression.strip()
    if not text:
        raise FieldError("the field expression is empty; write 'spec.interfaces[].mac'")
    for part in text.split("."):
        match = _STEP.match(part)
        if match is None:
            raise FieldError(
                f"{echo_value(part)} is not a path component of {echo_value(text)}; expected "
                f"a key, optionally followed by '[]' or '[key=value]'"
            )
        yield match["key"], tuple(_BRACKET.findall(match["steps"]))


def _step(values: Sequence[Any], component: tuple[str, tuple[str, ...]]) -> Iterator[Any]:
    """Apply one ``key`` plus its bracket steps to every value in ``values``."""
    key, brackets = component
    for value in values:
        if not isinstance(value, Mapping) or key not in value:
            continue
        found: list[Any] = [value[key]]
        for bracket in brackets:
            found = list(_bracket(found, bracket))
        yield from found


def _bracket(values: Sequence[Any], bracket: str) -> Iterator[Any]:
    """Apply one ``[]``, ``[key=value]`` or ``[key!=value]`` step."""
    text = bracket.strip()
    if not text:
        for value in values:
            if isinstance(value, list):
                yield from value
        return
    key, separator, pattern = text.partition("=")
    if not separator:
        raise FieldError(
            f"'[{bracket}]' is not a filter; write '[]' to flatten a list, or "
            f"'[key=value]' / '[key!=value]' to pick from one"
        )
    negated = key.endswith("!")
    key = key.removesuffix("!").strip()
    pattern = pattern.strip()
    if not key:
        raise FieldError(f"'[{bracket}]' has no key; write '[name=eth0]'")
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                continue
            if fnmatch.fnmatchcase(str(item.get(key, "")), pattern) is not negated:
                yield item


def render_value(value: Any) -> str:
    """``value`` as the one string that stands for it in a message and a key.

    A scalar prints as itself, so a duplicate address is reported as
    ``10.0.0.1/24`` rather than as a quoted JSON string. Anything else prints as
    compact JSON with sorted keys, which is what makes two equal mappings equal
    strings whatever order the loader happened to build them in.
    """
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
