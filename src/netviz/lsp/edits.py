"""Turning what an edit session would write into a ``WorkspaceEdit``.

Every write netviz performs goes through :mod:`netviz.edit`, and it hands
back the *whole* new text of each file it touched — that is what makes a rename
comment-preserving, because the round-trip parser rewrote the one scalar and
left every byte around it alone. An editor wants ranges, so the translation here
is deliberately the blunt one: replace the file, end to end.

That is not laziness. A rename touches one token per reference, and computing
the minimal ranges for them would mean re-deriving what the edit layer already
did, in a second implementation, with a second set of bugs. Replacing the whole
document is exactly equivalent, is what ``textDocument/formatting`` does anyway,
and leaves one place — the edit layer — that decides what a file becomes.

Clients that advertise ``documentChanges`` get versioned
:class:`TextDocumentEdit` s and real create/delete operations, so applying an
edit against a buffer that moved underneath is refused by the editor rather than
silently applied. Clients that do not get the flat ``changes`` map, which cannot
express a deletion — those are reported instead of guessed at.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from netviz.lsp.text import Encoding, full_range

__all__ = ["WorkspaceEditBuilder", "workspace_edit"]


class WorkspaceEditBuilder:
    """Builds one ``WorkspaceEdit`` from a set of whole-file replacements."""

    def __init__(
        self,
        *,
        uri_of: Callable[[str], str],
        before_of: Callable[[str], str | None],
        version_of: Callable[[str], int | None],
        document_changes: bool = True,
        encoding: Encoding = Encoding.UTF16,
    ) -> None:
        self._uri_of = uri_of
        self._before_of = before_of
        self._version_of = version_of
        self._document_changes = document_changes
        self._encoding = encoding

    def build(self, changes: Mapping[str, str | None]) -> dict[str, Any]:
        """The wire form of ``relative -> new text`` (``None`` meaning removed)."""
        if not self._document_changes:
            return {"changes": self._flat(changes)}
        return {"documentChanges": self._operations(changes)}

    # -- the two shapes --------------------------------------------------

    def _flat(self, changes: Mapping[str, str | None]) -> dict[str, list[dict[str, Any]]]:
        """The ``changes`` map: text edits only, keyed by URI."""
        edits: dict[str, list[dict[str, Any]]] = {}
        for relative, after in sorted(changes.items()):
            if after is None:
                # A flat ``changes`` map has no way to say "delete this file".
                # Emptying it would leave a file the loader still walks.
                continue
            edits[self._uri_of(relative)] = [self._replacement(relative, after)]
        return edits

    def _operations(self, changes: Mapping[str, str | None]) -> list[dict[str, Any]]:
        """``documentChanges``: creates, edits and deletes, in that order."""
        operations: list[dict[str, Any]] = []
        for relative, after in sorted(changes.items()):
            uri = self._uri_of(relative)
            before = self._before_of(relative)
            if after is None:
                operations.append({"kind": "delete", "uri": uri, "options": {"recursive": False}})
                continue
            if before is None:
                operations.append({"kind": "create", "uri": uri, "options": {"overwrite": False}})
            operations.append(
                {
                    "textDocument": {"uri": uri, "version": self._version_of(relative)},
                    "edits": [self._replacement(relative, after)],
                }
            )
        return operations

    def _replacement(self, relative: str, after: str) -> dict[str, Any]:
        before = self._before_of(relative) or ""
        return {"range": full_range(before, self._encoding).to_dict(), "newText": after}


def workspace_edit(
    changes: Mapping[str, str | None],
    *,
    uri_of: Callable[[str], str],
    before_of: Callable[[str], str | None],
    version_of: Callable[[str], int | None],
    document_changes: bool = True,
    encoding: Encoding = Encoding.UTF16,
) -> dict[str, Any]:
    """One-shot :class:`WorkspaceEditBuilder`."""
    return WorkspaceEditBuilder(
        uri_of=uri_of,
        before_of=before_of,
        version_of=version_of,
        document_changes=document_changes,
        encoding=encoding,
    ).build(changes)
