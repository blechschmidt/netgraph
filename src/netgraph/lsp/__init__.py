"""``netgraph lsp`` — a language server for inventory YAML.

The visual editor and the text are supposed to be one source of truth. The
visual half has an editor of its own; this is the other half, and it is
deliberately not a second implementation of anything. Every answer it gives is
produced by the machinery the command line already uses:

============================ =================================================
Diagnostics                  :func:`netgraph.validate.validate`, through
                             :func:`~netgraph.diagnostics.build_report`
Completion                   :func:`netgraph.schema.build_schema`, plus the
                             names in the loaded tree
Hover, definition, references :mod:`netgraph.edit.references` and the loader's
                             provenance
Rename                       :class:`~netgraph.edit.operations.RenameElement`
Formatting                   :func:`netgraph.fmt.format_source`
Code actions                 :mod:`netgraph.fixes`
Watching                     :mod:`netgraph.watch.loop`
============================ =================================================

There is no new dependency. LSP's transport is JSON-RPC in ``Content-Length``
frames, which is :mod:`netgraph.lsp.jsonrpc` and about a hundred lines; a
library would have brought a framework along with it and nothing else.

``docs/lsp.md`` is the setup guide for VS Code and Neovim.
"""

from __future__ import annotations

from netgraph.lsp.jsonrpc import Connection, ProtocolError, ResponseError
from netgraph.lsp.server import SERVER_NAME, LanguageServer, serve
from netgraph.lsp.text import Encoding, Position, Range, TextDocument
from netgraph.lsp.workspace import LONE_FILE_RULES, Analysis, Workspace

__all__ = [
    "LONE_FILE_RULES",
    "SERVER_NAME",
    "Analysis",
    "Connection",
    "Encoding",
    "LanguageServer",
    "Position",
    "ProtocolError",
    "Range",
    "ResponseError",
    "TextDocument",
    "Workspace",
    "serve",
]
