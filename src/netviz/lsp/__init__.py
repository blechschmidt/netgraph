"""``netviz lsp`` — a language server for inventory YAML.

The visual editor and the text are supposed to be one source of truth. The
visual half has an editor of its own; this is the other half, and it is
deliberately not a second implementation of anything. Every answer it gives is
produced by the machinery the command line already uses:

============================ =================================================
Diagnostics                  :func:`netviz.validate.validate`, through
                             :func:`~netviz.diagnostics.build_report`
Completion                   :func:`netviz.schema.build_schema`, plus the
                             names in the loaded tree
Hover, definition, references :mod:`netviz.edit.references` and the loader's
                             provenance
Rename                       :class:`~netviz.edit.operations.RenameElement`
Formatting                   :func:`netviz.fmt.format_source`
Code actions                 :mod:`netviz.fixes`
Watching                     :mod:`netviz.watch.loop`
============================ =================================================

There is no new dependency. LSP's transport is JSON-RPC in ``Content-Length``
frames, which is :mod:`netviz.lsp.jsonrpc` and about a hundred lines; a
library would have brought a framework along with it and nothing else.

``docs/lsp.md`` is the setup guide for VS Code and Neovim.
"""

from __future__ import annotations

from netviz.lsp.jsonrpc import Connection, ProtocolError, ResponseError
from netviz.lsp.server import SERVER_NAME, LanguageServer, serve
from netviz.lsp.text import Encoding, Position, Range, TextDocument
from netviz.lsp.workspace import LONE_FILE_RULES, Analysis, Workspace

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
