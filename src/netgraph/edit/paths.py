"""Naming a value inside a document, and reading, writing or removing it.

Every field-level operation takes a path — ``spec.model``,
``spec.interfaces[2].mtu``, ``metadata.labels.site`` — and the two things that
matter about the grammar are that it is the one the diagnostics already use
(:func:`netgraph.errors.format_path` prints the same shape) and that it is
unambiguous without quoting: a key is a run of characters that is not ``.`` or
``[``, and an index is a bracketed integer.

Writing is deliberately conservative. A missing mapping on the way to the value
is created, because ``set device spec.location "rack 4"`` on a device with no
``spec.location`` is obviously meant to work. A missing *list element* is not:
``spec.interfaces[7]`` on a device with three interfaces is a mistake, not an
instruction to invent four empty ones, and the operations that do add list
entries (:class:`~netgraph.edit.operations.AddInterface`) say so in their name.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from ruamel.yaml.comments import CommentedMap

from netgraph.edit.errors import OperationError

__all__ = [
    "FieldPath",
    "format_field_path",
    "get_field",
    "parse_field_path",
    "set_field",
    "unset_field",
]

#: A parsed path: mapping keys as strings, sequence positions as integers.
FieldPath = tuple[str | int, ...]

_SEGMENT_RE: Final = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def parse_field_path(text: str) -> FieldPath:
    """Parse ``spec.interfaces[0].mtu`` into ``("spec", "interfaces", 0, "mtu")``.

    Raises:
        OperationError: The path is empty or does not parse.
    """
    if not text or not text.strip():
        raise OperationError("a field path cannot be empty")
    path: list[str | int] = []
    position = 0
    expecting_key = True
    while position < len(text):
        if text[position] == ".":
            if expecting_key:
                raise OperationError(f"{text!r} is not a field path: empty key before '.'")
            position += 1
            expecting_key = True
            continue
        match = _SEGMENT_RE.match(text, position)
        if match is None:
            raise OperationError(f"{text!r} is not a field path: unexpected {text[position]!r}")
        key, index = match.groups()
        if key is not None:
            if not expecting_key:
                raise OperationError(f"{text!r} is not a field path: missing '.' before {key!r}")
            path.append(key)
        else:
            path.append(int(index))
        expecting_key = False
        position = match.end()
    if expecting_key:
        raise OperationError(f"{text!r} is not a field path: it ends with '.'")
    return tuple(path)


def format_field_path(path: Sequence[str | int]) -> str:
    """The inverse of :func:`parse_field_path`, for diagnostics and JSON."""
    text = ""
    for step in path:
        if isinstance(step, int):
            text += f"[{step}]"
        elif text:
            text += f".{step}"
        else:
            text = str(step)
    return text


class _Missing:
    """Sentinel for "there is no value here", distinct from ``None``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


MISSING: Final = _Missing()


def get_field(document: Any, path: Sequence[str | int]) -> Any:
    """The value at ``path``, or :data:`MISSING`.

    ``None`` is a value — a document may say ``description: null`` — so absence
    needs its own answer, which is what makes the inverse of a
    :class:`~netgraph.edit.operations.SetField` decidable.
    """
    node = document
    for step in path:
        if isinstance(step, int):
            if not isinstance(node, list) or not -len(node) <= step < len(node):
                return MISSING
            node = node[step]
        else:
            if not isinstance(node, dict) or step not in node:
                return MISSING
            node = node[step]
    return node


def set_field(document: Any, path: Sequence[str | int], value: Any) -> None:
    """Write ``value`` at ``path``, creating the mappings on the way.

    Raises:
        OperationError: The path runs through something that is not a mapping,
            or through a sequence position that does not exist.
    """
    if not path:
        raise OperationError("a field path cannot be empty")
    node = _descend(document, path[:-1], creating=True)
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(node, list) or not -len(node) <= last < len(node):
            raise OperationError(
                f"{format_field_path(path)}: there is no entry {last} to set; "
                "a sequence entry has to be added before it can be set"
            )
        node[last] = value
        return
    if not isinstance(node, dict):
        raise OperationError(
            f"{format_field_path(path)}: {format_field_path(path[:-1]) or 'the document'} "
            f"is a {_describe(node)}, not a mapping"
        )
    node[last] = value


def unset_field(document: Any, path: Sequence[str | int]) -> Any:
    """Remove the value at ``path`` and return it.

    Raises:
        OperationError: There is nothing at ``path`` to remove.
    """
    if not path:
        raise OperationError("a field path cannot be empty")
    node = _descend(document, path[:-1], creating=False)
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(node, list) or not -len(node) <= last < len(node):
            raise OperationError(f"{format_field_path(path)}: there is no entry to remove")
        return node.pop(last)
    if not isinstance(node, dict) or last not in node:
        raise OperationError(f"{format_field_path(path)}: there is no such field to remove")
    return node.pop(last)


def _descend(document: Any, path: Sequence[str | int], *, creating: bool) -> Any:
    """Walk to the container ``path`` names, optionally creating mappings."""
    node = document
    for position, step in enumerate(path):
        if isinstance(step, int):
            if not isinstance(node, list) or not -len(node) <= step < len(node):
                raise OperationError(
                    f"{format_field_path(path[: position + 1])}: there is no such entry"
                )
            node = node[step]
            continue
        if isinstance(node, dict):
            if step not in node:
                if not creating:
                    raise OperationError(
                        f"{format_field_path(path[: position + 1])}: there is no such field"
                    )
                node[step] = CommentedMap()
            node = node[step]
            continue
        raise OperationError(
            f"{format_field_path(path[:position]) or 'the document'} is a "
            f"{_describe(node)}, not a mapping"
        )
    return node


def _describe(node: Any) -> str:
    """What a value is, in the words a message about a path should use."""
    if isinstance(node, list):
        return "sequence"
    if isinstance(node, dict):  # pragma: no cover - callers check for this first
        return "mapping"
    if node is None:
        return "null"
    return type(node).__name__
