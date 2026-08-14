"""Field paths that survive an insertion: ``spec.interfaces[name=eth0].mtu``.

The edit layer addresses a field by position — ``spec.interfaces[2].mtu`` — which
is the right thing for a command somebody types against a file they are looking
at. It is the wrong thing for a plan: a plan is written now and applied later,
and an interface added to the front of the list in between would silently move
every index in it onto the wrong port.

So a plan path may carry a **selector** step instead of an index. A selector
names a list entry by one of its own fields (``name=eth0``), which is stable
under insertion, reordering and reformatting, and which reads far better in a
diff than a number does. :func:`resolve` turns one back into the index the edit
layer wants, against the document it is about to be written to.

Everything else is the same grammar the edit layer parses, so a path with no
selector in it round-trips through both.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MISSING",
    "PathError",
    "Selector",
    "Step",
    "format_path",
    "get_path",
    "parse_path",
    "resolve",
]


class PathError(ValueError):
    """A field path is malformed, or does not select anything."""


@dataclass(frozen=True, slots=True)
class Selector:
    """One list entry, named by a field of its own rather than by position."""

    key: str
    value: str

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


#: One step of a path: a mapping key, a list index, or a list selector.
Step = str | int | Selector


class _Missing:
    """The value of a field that is not there at all.

    Distinct from ``None``, which is a value a document may legitimately hold:
    ``mtu: null`` and no ``mtu`` key are different things to a diff, and only
    one of them is undone by removing the key.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


#: Singleton sentinel; compare with ``is``.
MISSING: Final = _Missing()

_KEY_RE: Final = re.compile(r"[^.\[\]]+")


def format_path(steps: Sequence[Step]) -> str:
    """Render a path the way a plan prints it and a plan file stores it."""
    out: list[str] = []
    for step in steps:
        if isinstance(step, str):
            out.append(f".{step}" if out else step)
        else:
            out.append(f"[{step}]")
    return "".join(out)


def parse_path(text: str) -> tuple[Step, ...]:
    """Read a path back from its printed form.

    Raises:
        PathError: The text is empty or does not parse.
    """
    steps = tuple(_steps(text))
    if not steps:
        raise PathError("a field path cannot be empty")
    return steps


def _steps(text: str) -> Iterator[Step]:
    position = 0
    length = len(text)
    expect_separator = False
    while position < length:
        char = text[position]
        if char == ".":
            if not expect_separator:
                raise PathError(f"{text!r}: unexpected '.' at position {position}")
            expect_separator = False
            position += 1
            continue
        if char == "[":
            end = text.find("]", position)
            if end < 0:
                raise PathError(f"{text!r}: unclosed '[' at position {position}")
            yield _bracket(text[position + 1 : end], text)
            position = end + 1
            expect_separator = True
            continue
        if expect_separator:
            raise PathError(f"{text!r}: expected '.' at position {position}")
        match = _KEY_RE.match(text, position)
        if match is None:
            raise PathError(f"{text!r}: expected a key at position {position}")
        yield match.group()
        position = match.end()
        expect_separator = True
    if not expect_separator and length:
        raise PathError(f"{text!r}: path ends with a separator")


def _bracket(body: str, text: str) -> Step:
    """``[2]`` is an index; ``[name=eth0]`` is a selector."""
    key, separator, value = body.partition("=")
    if separator:
        if not key or not value:
            raise PathError(f"{text!r}: a selector needs 'key=value', got '[{body}]'")
        return Selector(key=key, value=value)
    try:
        return int(body)
    except ValueError:
        raise PathError(f"{text!r}: '[{body}]' is neither an index nor 'key=value'") from None


def get_path(document: Any, steps: Sequence[Step]) -> Any:
    """The value at ``steps``, or :data:`MISSING` when nothing is there."""
    current: Any = document
    for step in steps:
        if isinstance(step, Selector):
            current = _entry(current, step)
        elif isinstance(step, int):
            if not isinstance(current, Sequence) or isinstance(current, str | bytes):
                return MISSING
            try:
                current = current[step]
            except IndexError:
                return MISSING
        else:
            if not isinstance(current, Mapping) or step not in current:
                return MISSING
            current = current[step]
        if current is MISSING:
            return MISSING
    return current


def _entry(container: Any, selector: Selector) -> Any:
    if not isinstance(container, Sequence) or isinstance(container, str | bytes):
        return MISSING
    for entry in container:
        if isinstance(entry, Mapping) and _text(entry.get(selector.key)) == selector.value:
            return entry
    return MISSING


def resolve(steps: Sequence[Step], document: Any) -> tuple[Step, ...]:
    """Replace every selector in ``steps`` with the index it names in ``document``.

    The result is a path the edit layer's ``parse_field_path`` accepts.

    Raises:
        PathError: A selector names no entry of the list it is applied to. That
            is the plan and the tree disagreeing about what is there, which is
            precisely what the caller must not paper over by guessing.
    """
    out: list[Step] = []
    current: Any = document
    for step in steps:
        if isinstance(step, Selector):
            index = _index_of(current, step)
            if index is None:
                raise PathError(
                    f"no entry with {step.key} = {step.value!r} at "
                    f"{format_path(out) or '(document)'}"
                )
            out.append(index)
            current = current[index]
            continue
        out.append(step)
        current = get_path(current, (step,))
    return tuple(out)


def _index_of(container: Any, selector: Selector) -> int | None:
    if not isinstance(container, Sequence) or isinstance(container, str | bytes):
        return None
    for index, entry in enumerate(container):
        if isinstance(entry, Mapping) and _text(entry.get(selector.key)) == selector.value:
            return index
    return None


def _text(value: Any) -> str:
    """A selector's value is compared as text, so ``1`` matches ``"1"``.

    Documents reach this both from a YAML parser and from a model dump, and the
    two disagree about whether a VLAN id or an outlet number is an ``int`` or a
    ``str``. The selector is an identifier, not a number to do arithmetic on.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)
