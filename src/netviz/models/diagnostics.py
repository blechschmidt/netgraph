"""Attaching a rule id and a field path to a validation error.

Pydantic reports an error raised by a model validator at the location of the
*model*, not of the offending value inside it. A cross-field rule such as
``NV-I001`` ("interface names are unique within their device") therefore loses
the index that makes the diagnostic useful.

:func:`field_error` encodes the rule id and the path of the offending value —
relative to the model that raises it — into the exception message;
:func:`decode_field_error` recovers them when
:mod:`netviz.models.document` builds the :class:`~netviz.errors.SchemaIssue`
list. The encoding is internal to these two functions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Final

__all__ = ["decode_field_error", "field_error"]

#: ASCII unit separator: never part of a human-readable message.
_MARKER: Final = "\x1f"
_RULE_RE: Final = re.compile(r"^(?P<rule>NV-[A-Z][0-9]{3}):\s*(?P<message>.*)$", re.DOTALL)


def field_error(
    message: str,
    *,
    rule: str | None = None,
    path: Sequence[str | int] = (),
) -> ValueError:
    """Build a :class:`ValueError` that carries ``rule`` and ``path``.

    ``path`` is relative to the model whose validator raises the error, so a
    validator on ``spec`` uses ``("interfaces", 3, "mtu")``.
    """
    payload = json.dumps({"rule": rule, "path": list(path)}, separators=(",", ":"))
    return ValueError(f"{message}{_MARKER}{payload}")


def decode_field_error(message: str) -> tuple[str, str | None, tuple[str | int, ...]]:
    """Split an encoded message into ``(message, rule, path)``.

    Falls back to a plain ``"NV-I001: ..."`` prefix, and finally to the message
    unchanged, so errors raised by pydantic itself pass through untouched.
    """
    text, separator, payload = message.partition(_MARKER)
    if separator:
        try:
            decoded = json.loads(payload)
            rule = decoded["rule"]
            path = tuple(decoded["path"])
        except (ValueError, KeyError, TypeError):  # pragma: no cover - defensive
            return text, None, ()
        return text, rule, path

    match = _RULE_RE.match(text)
    if match is not None:
        return match["message"], match["rule"], ()
    return text, None, ()
