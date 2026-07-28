"""Exception hierarchy shared by every netgraph layer.

Every error that is expected to reach the user is derived from
:class:`NetgraphError`. The CLI catches that base class and turns it into a
short diagnostic plus a non-zero exit status, so lower layers never need to
print or call :func:`sys.exit` themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MAX_ECHOED_VALUE_LENGTH",
    "ConfigurationError",
    "LoaderError",
    "NetgraphError",
    "RenderError",
    "SchemaError",
    "SchemaIssue",
    "ValidationError",
    "clip_text",
    "echo_value",
    "format_path",
]

#: How much of a rejected value a diagnostic quotes back before eliding the
#: rest. Long enough that every well-formed value netgraph accepts — the
#: longest of which is a 253-character name — is recognisable from its prefix.
MAX_ECHOED_VALUE_LENGTH: Final = 120


def clip_text(text: str, *, limit: int = MAX_ECHOED_VALUE_LENGTH) -> str:
    """Bound ``text`` to ``limit`` characters, noting how many were dropped.

    For text that is already prose — the wording of a nested exception, which
    tends to quote the same oversized value a second time. Use
    :func:`echo_value` for a value, which has to be quoted as well as bounded.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} more characters)"


def echo_value(value: object, *, limit: int = MAX_ECHOED_VALUE_LENGTH) -> str:
    """Quote ``value`` for a diagnostic, bounded to a readable prefix.

    A diagnostic names the value it rejected so the reader recognises it, but
    that value comes from a document and is therefore of unbounded size: a
    200 000-character ``mac:`` would otherwise produce a single 200 000-character
    error line. The location prefix already says where to look, so past ``limit``
    characters the value is replaced by a count of what was dropped.

    Values at or under ``limit`` are echoed verbatim, as ``repr`` renders them,
    which is what makes a trailing space or a mixed-script homoglyph visible.
    """
    if not isinstance(value, str):
        # No prefix of the value itself to take, so bound its repr instead;
        # that also covers a pathologically large int, list or mapping.
        return clip_text(repr(value), limit=limit)
    if len(value) <= limit:
        return repr(value)
    return f"{value[:limit]!r}… (+{len(value) - limit} more characters)"


class NetgraphError(Exception):
    """Base class for all errors raised deliberately by netgraph."""

    #: Process exit status the CLI uses when this error escapes to the top level.
    exit_code: int = 1


class ConfigurationError(NetgraphError):
    """Raised when user-supplied options or environment settings are unusable."""

    exit_code = 2


class LoaderError(NetgraphError):
    """Raised when an inventory tree cannot be discovered, read, or parsed."""

    exit_code = 3


@dataclass(frozen=True, slots=True)
class SchemaIssue:
    """A single reason why a document does not match the schema.

    ``path`` is the location of the offending value inside the document, as a
    sequence of mapping keys and list indices, for example
    ``("spec", "interfaces", 0, "mtu")``. An empty path refers to the document
    as a whole.
    """

    path: tuple[str | int, ...] = ()
    message: str = "invalid document"
    #: Validation-rule identifier from ``docs/schema.md`` §10, when one applies.
    rule: str | None = None

    @property
    def location(self) -> str:
        """The path in ``spec.interfaces[0].mtu`` notation (``.`` for the root)."""
        return format_path(self.path)

    def __str__(self) -> str:
        prefix = f"{self.rule}: " if self.rule else ""
        return f"{prefix}{self.location}: {self.message}"


def format_path(path: tuple[str | int, ...]) -> str:
    """Render a field path as ``spec.interfaces[0].mtu``.

    A path component is a mapping key straight out of a document, so it is
    bounded here for the same reason a rejected value is: an unknown key
    200 000 characters long would otherwise arrive as the *location* of its own
    diagnostic, however carefully the message itself was clipped.
    """
    if not path:
        return "."
    rendered = ""
    for part in path:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif rendered:
            rendered += f".{clip_text(part)}"
        else:
            rendered = clip_text(part)
    return rendered


class SchemaError(LoaderError):
    """Raised when a document does not match the schema of its ``kind``.

    The error carries one :class:`SchemaIssue` per problem found, each with the
    field path of the offending value, so callers can report every mistake in a
    document instead of only the first one.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        issues: Sequence[SchemaIssue] = (),
        source: str | None = None,
    ) -> None:
        self.issues: tuple[SchemaIssue, ...] = tuple(issues)
        self.source = source
        super().__init__(message or self._summarise())

    def _summarise(self) -> str:
        if not self.issues:
            return "document does not match the schema"
        where = f"{self.source}: " if self.source else ""
        if len(self.issues) == 1:
            return f"{where}{self.issues[0]}"
        listed = "\n".join(f"  - {issue}" for issue in self.issues)
        return f"{where}{len(self.issues)} schema errors:\n{listed}"

    @property
    def path(self) -> tuple[str | int, ...]:
        """Field path of the first issue (``()`` when there is none)."""
        return self.issues[0].path if self.issues else ()

    @property
    def location(self) -> str:
        """Field path of the first issue in ``spec.interfaces[0].mtu`` notation."""
        return format_path(self.path)


class ValidationError(NetgraphError):
    """Raised when an inventory parses but is not semantically consistent."""

    exit_code = 4


class RenderError(NetgraphError):
    """Raised when a loaded inventory cannot be rendered to the requested format."""

    exit_code = 5
