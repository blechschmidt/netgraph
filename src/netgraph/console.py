"""Terminal output helpers: colour that disappears when nobody can see it.

netgraph is a tool people pipe. ``netgraph list devices | grep sw-`` and
``netgraph render -f json > topology.json`` have to produce clean text, while an
interactive run should be pleasant to read. Both are served here:

* :class:`Console` decides **once** whether the stream is a terminal and styles
  accordingly, so no caller has to remember to check. Colour is additionally
  suppressed when ``NO_COLOR`` is set (https://no-color.org) and forced on by
  ``FORCE_COLOR`` or ``--color``.
* :func:`format_table` aligns columns with spaces only. Alignment is plain text,
  so a piped table stays a table; there are no box-drawing characters to strip.

Diagnostics go to stderr and data goes to stdout, always. A user redirecting
stdout must get the data and nothing else.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import IO, Any, Final, Literal

import click

__all__ = ["Align", "Console", "format_table"]

Align = Literal["left", "right"]

#: Separator between table columns. Two spaces read as a column break without
#: making a wide table wrap on an 80-column terminal.
_COLUMN_GAP: Final = "  "


@dataclass(frozen=True, slots=True)
class Console:
    """Styled output for one stream, with the styling decision made up front."""

    #: Whether ANSI styling may be emitted at all.
    color: bool = False
    #: Suppress informational (non-error, non-data) output.
    quiet: bool = False
    #: Send :meth:`print` to stderr. Used by commands whose stdout carries data
    #: — ``render`` writes a diagram there, so its findings must go elsewhere.
    err: bool = False

    @classmethod
    def create(
        cls,
        *,
        color: bool | None = None,
        quiet: bool = False,
        stream: IO[Any] | None = None,
        err: bool = False,
    ) -> Console:
        """Build a console for ``stream``.

        Args:
            color: Force styling on or off. ``None`` auto-detects: styling is
                used only when the stream is a terminal and ``NO_COLOR`` is unset.
            err: Write data lines to stderr rather than stdout. The stream that
                styling is detected on follows suit unless ``stream`` says
                otherwise.
        """
        target = stream if stream is not None else (sys.stderr if err else sys.stdout)
        return cls(color=cls._detect(color, target), quiet=quiet, err=err)

    def to_stderr(self) -> Console:
        """The same console, writing its data lines to stderr."""
        return Console(color=self.color, quiet=self.quiet, err=True)

    @staticmethod
    def _detect(color: bool | None, stream: IO[Any]) -> bool:
        if color is not None:
            return color
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        try:
            return bool(stream.isatty())
        except (AttributeError, ValueError):  # pragma: no cover - closed stream
            return False

    # -- styling ---------------------------------------------------------

    def style(self, text: str, **attributes: Any) -> str:
        """Apply ``click.style`` attributes, or return ``text`` unchanged.

        Styling is dropped entirely rather than emitted and stripped later, so
        the width of a styled string always equals the width it occupies.
        """
        return click.style(text, **attributes) if self.color else text

    def bold(self, text: str) -> str:
        return self.style(text, bold=True)

    def dim(self, text: str) -> str:
        return self.style(text, dim=True)

    # -- writing ---------------------------------------------------------

    def print(self, message: str = "") -> None:
        """Write one line of *data* to the console's stream."""
        click.echo(message, err=self.err, color=self.color)

    def info(self, message: str = "") -> None:
        """Write one line of *commentary* to stderr, unless quiet."""
        if not self.quiet:
            click.echo(message, err=True, color=self.color)

    def warn(self, message: str) -> None:
        """Write a warning to stderr. Not suppressed by ``--quiet``."""
        click.echo(
            f"{self.style('warning:', fg='yellow', bold=True)} {message}",
            err=True,
            color=self.color,
        )

    def error(self, message: str) -> None:
        """Write an error to stderr. Not suppressed by ``--quiet``."""
        click.echo(
            f"{self.style('error:', fg='red', bold=True)} {message}", err=True, color=self.color
        )

    def table(
        self,
        headers: Sequence[str],
        rows: Iterable[Sequence[str]],
        *,
        aligns: Sequence[Align] | None = None,
        empty: str = "(none)",
    ) -> None:
        """Print an aligned table to stdout, or ``empty`` when there are no rows."""
        materialised = [list(row) for row in rows]
        if not materialised:
            self.print(self.dim(empty))
            return
        for line in format_table(headers, materialised, aligns=aligns, console=self):
            self.print(line)


def format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    aligns: Sequence[Align] | None = None,
    console: Console | None = None,
) -> list[str]:
    """Lay out ``rows`` under ``headers`` as space-aligned plain text.

    Column widths are measured on the *unstyled* text, so a coloured cell does
    not push its column out by the length of its escape sequence. Trailing
    whitespace is stripped from every line: a piped table should not carry
    padding a reader has to clean up.
    """
    columns = len(headers)
    widths = [_width(header) for header in headers]
    for row in rows:
        for index in range(columns):
            cell = row[index] if index < len(row) else ""
            widths[index] = max(widths[index], _width(cell))

    alignment: Sequence[Align] = aligns or ["left"] * columns
    styler = console or Console()

    lines = [
        _join(
            [
                styler.bold(_pad(header, widths[index], alignment[index]))
                for index, header in enumerate(headers)
            ]
        ),
        _join(["-" * width for width in widths]),
    ]
    for row in rows:
        cells = [
            _pad(row[index] if index < len(row) else "", widths[index], alignment[index])
            for index in range(columns)
        ]
        lines.append(_join(cells))
    return lines


def _join(cells: Sequence[str]) -> str:
    return _COLUMN_GAP.join(cells).rstrip()


def _pad(text: str, width: int, align: Align) -> str:
    """Pad to ``width`` measured on the visible text, keeping any styling."""
    padding = " " * max(0, width - _width(text))
    return f"{padding}{text}" if align == "right" else f"{text}{padding}"


def _width(text: str) -> int:
    """The printable width of ``text``, ignoring ANSI escape sequences."""
    return len(click.unstyle(text))
