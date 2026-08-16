"""Turning names a device reported into names ``docs/schema.md`` §4.1 accepts.

Nothing guarantees that a chassis name or a port description is a legal netviz
identifier. LLDP happily reports ``Port 1``, ``sw core (rack 4)`` or a chassis
name with a trailing dot, and a CSV that came out of a spreadsheet can hold
anything at all. Rejecting those inputs would defeat the point of the command;
silently rewriting them would hide the one thing a reader needs to know, which
is that the file no longer says what the device said.

So both functions here return a *pair*: the usable name, and whether it had to
be changed. Every caller that gets ``changed=True`` writes a comment recording
the original next to the value. The rewrite is deterministic, so re-importing
the same capture produces the same tree and a diff shows only what moved.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["MAX_ELEMENT_NAME", "MAX_IFNAME", "SanitisedName", "element_name", "interface_name"]

#: ``metadata.name`` length ceiling (§4.1).
MAX_ELEMENT_NAME: Final = 253
#: ``interfaces[].name`` length ceiling (§4.1).
MAX_IFNAME: Final = 64

#: Characters an element name may hold. ``-``, ``_`` and ``.`` are separators
#: there rather than free characters, which is why the run-collapsing below is
#: not merely cosmetic: ``sw--core`` does not match the grammar, ``sw-core`` does.
_ELEMENT_ILLEGAL: Final = re.compile(r"[^A-Za-z0-9._-]+")
_ELEMENT_SEPARATOR_RUN: Final = re.compile(r"[._-]{2,}")
#: Characters an interface name may hold (§4.1); ``/`` is legal here and not
#: there, because ``Gi0/1`` is a port and ``a/b`` is a namespace.
_INTERFACE_ILLEGAL: Final = re.compile(r"[^A-Za-z0-9._/-]+")

#: Result of a sanitisation: the name to use, and the original when it differs.
SanitisedName = tuple[str | None, str | None]


def element_name(raw: str) -> SanitisedName:
    """``raw`` as a ``metadata.name``, and the original when it had to change.

    Returns ``(None, raw)`` when nothing usable is left — an all-punctuation
    chassis name, or an empty one. Callers report that rather than invent a
    name, because a device netviz cannot name is a device the operator has to
    look at.
    """
    cleaned = _ELEMENT_ILLEGAL.sub("-", raw.strip())
    # Collapse ``a--b`` and ``a-_b`` to a single separator, then drop any
    # separator left at either end: the grammar requires alphanumerics there.
    cleaned = _ELEMENT_SEPARATOR_RUN.sub(lambda match: match.group()[0], cleaned)
    cleaned = cleaned.strip("._-")[:MAX_ELEMENT_NAME].strip("._-")
    if not cleaned:
        return (None, raw)
    return (cleaned, None if cleaned == raw else raw)


def interface_name(raw: str) -> SanitisedName:
    """``raw`` as an ``interfaces[].name``, and the original when it had to change."""
    cleaned = _INTERFACE_ILLEGAL.sub("-", raw.strip())
    cleaned = cleaned.strip("-")[:MAX_IFNAME].strip("-")
    if not cleaned:
        return (None, raw)
    return (cleaned, None if cleaned == raw else raw)
