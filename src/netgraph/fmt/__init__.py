"""The canonical form of an inventory file, and the machinery to apply it.

``docs/format.md`` defines the form; this package implements it, behind
``netgraph fmt``. The one thing worth knowing from outside is that it does not
share a parser with the rest of netgraph:

* ``validate`` and ``render`` load through :mod:`netgraph.loader.documents`,
  which is strict and fast and discards comments.
* ``fmt`` parses through ``ruamel.yaml``'s round-trip loader, which keeps
  comments, blank lines and quoting style, and is slower for it.

Nothing here is imported on the loading path, so that difference costs the other
commands nothing — and every format is handed back to the strict loader before
it is written (:mod:`netgraph.fmt.verify`), so the two cannot quietly disagree
about what a file says.
"""

from __future__ import annotations

from netgraph.fmt.canonical import (
    INDENT,
    SEQUENCE_INDENT,
    SEQUENCE_OFFSET,
    WIDTH,
    FormatSyntaxError,
    format_stream,
)
from netgraph.fmt.runner import (
    STDIN_NAME,
    FileResult,
    Mode,
    Outcome,
    Summary,
    diff_text,
    display_path,
    format_paths,
    format_source,
)

__all__ = [
    "INDENT",
    "SEQUENCE_INDENT",
    "SEQUENCE_OFFSET",
    "STDIN_NAME",
    "WIDTH",
    "FileResult",
    "FormatSyntaxError",
    "Mode",
    "Outcome",
    "Summary",
    "diff_text",
    "display_path",
    "format_paths",
    "format_source",
    "format_stream",
]
