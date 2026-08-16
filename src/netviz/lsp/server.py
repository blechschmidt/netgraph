"""The server: one thread, one queue, and a table of methods.

Everything that can change what the server knows — a message from the client, a
batch from the folder watcher — arrives on one queue and is handled on one
thread. That is the whole concurrency design, and it is deliberate: an inventory
is a graph of cross-references, and answering a request from a tree that another
thread is halfway through reloading is the kind of bug that reproduces once a
week and never in a test.

Diagnostics are published when the queue runs dry rather than on every
``didChange``. A burst of keystrokes arrives as a burst of notifications, and
coalescing them costs nothing and saves a full reload per character — without a
timer, so what a test observes is exactly what an editor observes.

Every capability is answered from the same loaded tree the diagnostics came
from, which is the point of the whole exercise: the squiggle, the hover, the
completion list and the rename all agree, because there is one inventory behind
them and it is the one ``netviz validate`` would load.
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from netviz import __version__
from netviz.diagnostics import Diagnostic
from netviz.edit.errors import EditError
from netviz.edit.operations import RenameElement
from netviz.edit.session import EditSession
from netviz.fixes import Fix, apply_fix, fixes_for, offers_for, repair
from netviz.fmt import FormatSyntaxError, format_source
from netviz.loader.inventory import short_name
from netviz.lsp import complete as completion
from netviz.lsp.context import context_at
from netviz.lsp.edits import workspace_edit
from netviz.lsp.hover import hover_markdown, key_markdown
from netviz.lsp.index import Anchor, AnchorKind, SemanticIndex
from netviz.lsp.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    SERVER_NOT_INITIALIZED,
    Connection,
    ProtocolError,
    ResponseError,
)
from netviz.lsp.locate import range_at
from netviz.lsp.publish import (
    by_file,
    diagnostic_key,
    finding_key,
    to_lsp,
    to_lsp_diagnostic,
)
from netviz.lsp.schemaindex import schema_index
from netviz.lsp.text import Encoding, Position, Range, TextDocument, full_range
from netviz.lsp.uri import uri_to_path
from netviz.lsp.watcher import FolderWatcher
from netviz.lsp.workspace import Analysis, Workspace
from netviz.validate import Finding

__all__ = ["LanguageServer", "serve"]

#: What the client sees in ``initialize``.
SERVER_NAME: Final = "netviz"

#: ``TextDocumentSyncKind.Incremental``.
_SYNC_INCREMENTAL: Final = 2

#: ``CodeActionKind``s this server produces.
_QUICK_FIX: Final = "quickfix"
_SOURCE_FIX_ALL: Final = "source.fixAll.netviz"

#: Ceiling on the quick fixes computed eagerly for a client with no
#: ``codeAction/resolve`` support. Each one applies the fix and re-validates, so
#: the cost is real; a client that resolves pays it for the one action it wants.
_MAX_EAGER_ACTIONS: Final = 8

#: What typing one of these asks the client to re-request completions after.
_TRIGGERS: Final = (":", "-", " ", "/")


@dataclass(slots=True)
class _Event:
    """One thing to handle, from the client or from the filesystem."""

    kind: str
    payload: Any = None


@dataclass(eq=False)
class LanguageServer:
    """One client session, from ``initialize`` to ``exit``."""

    connection: Connection
    #: Inventory root from the command line. Overridden by whatever the client
    #: says it opened, because the client is the one that knows.
    root: Path | None = None
    watch: bool = True
    log: Callable[[str], None] | None = None

    workspace: Workspace = field(default_factory=Workspace, init=False)
    _queue: queue.Queue[_Event] = field(default_factory=queue.Queue, init=False, repr=False)
    _initialized: bool = field(default=False, init=False)
    _shutdown: bool = field(default=False, init=False)
    _running: bool = field(default=True, init=False)
    _dirty: bool = field(default=True, init=False)
    _published: set[str] = field(default_factory=set, init=False, repr=False)
    _index: SemanticIndex | None = field(default=None, init=False, repr=False)
    _index_for: Analysis | None = field(default=None, init=False, repr=False)
    _watcher: FolderWatcher | None = field(default=None, init=False, repr=False)
    _document_changes: bool = field(default=True, init=False)
    _resolve_actions: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ #
    # The loop
    # ------------------------------------------------------------------ #

    def serve(self) -> int:
        """Run until the client says ``exit``. Returns the process exit code."""
        reader = threading.Thread(target=self._read, name="netviz-lsp-read", daemon=True)
        reader.start()
        while self._running:
            event = self._queue.get()
            try:
                self._handle(event)
            except ProtocolError as exc:
                self._note(f"protocol error: {exc}")
                return 1
            if self._running and self._dirty and self._queue.empty():
                self._refresh()
        self._stop_watching()
        # ``exit`` before ``shutdown`` is an error, which the specification asks
        # to be reported through the process exit code rather than a message.
        return 0 if self._shutdown else 1

    def _read(self) -> None:
        """Frame the client's stream onto the queue, on its own thread."""
        while True:
            try:
                message = self.connection.read()
            except ProtocolError as exc:
                self._queue.put(_Event("protocol", str(exc)))
                return
            if message is None:
                self._queue.put(_Event("eof"))
                return
            self._queue.put(_Event("message", message))

    def _handle(self, event: _Event) -> None:
        if event.kind == "message":
            self._dispatch(event.payload)
            return
        if event.kind == "changed":
            self._on_files_changed(event.payload)
            return
        if event.kind == "protocol":
            raise ProtocolError(str(event.payload))
        # End of stream without ``exit``: the client is gone, so are we.
        self._running = False

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        identifier = message.get("id")
        if method is None:
            return  # A response to a request we sent; nothing is outstanding.
        handler = _HANDLERS.get(str(method))
        if handler is None:
            if identifier is not None:
                self._error(identifier, METHOD_NOT_FOUND, f"{method} is not implemented by netviz")
            return
        params = message.get("params") or {}
        if not isinstance(params, Mapping):
            if identifier is not None:
                self._error(identifier, INVALID_PARAMS, "params must be an object")
            return
        try:
            result = handler(self, params)
        except ResponseError as exc:
            if identifier is not None:
                self._respond_error(identifier, exc)
            return
        except Exception as exc:
            self._note(f"{method} failed: {exc}\n{traceback.format_exc()}")
            if identifier is not None:
                self._error(identifier, INTERNAL_ERROR, f"{method} failed: {exc}")
            return
        if identifier is not None:
            self._respond(identifier, result)

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #

    def _respond(self, identifier: Any, result: Any) -> None:
        self.connection.write({"jsonrpc": "2.0", "id": identifier, "result": result})

    def _respond_error(self, identifier: Any, error: ResponseError) -> None:
        self.connection.write({"jsonrpc": "2.0", "id": identifier, "error": error.to_dict()})

    def _error(self, identifier: Any, code: int, message: str) -> None:
        self._respond_error(identifier, ResponseError(code, message))

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self.connection.write({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _note(self, message: str) -> None:
        """Say something to the log, both the client's and the operator's."""
        if self.log is not None:
            self.log(message)
        if self._initialized:
            self.notify("window/logMessage", {"type": 3, "message": message})

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def on_initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        capabilities = params.get("capabilities") or {}
        general = capabilities.get("general") or {}
        self.workspace.encoding = Encoding.negotiate(general.get("positionEncodings"))
        workspace_caps = capabilities.get("workspace") or {}
        edit_caps = workspace_caps.get("workspaceEdit") or {}
        self._document_changes = bool(edit_caps.get("documentChanges", False))
        document_caps = capabilities.get("textDocument") or {}
        action_caps = document_caps.get("codeAction") or {}
        self._resolve_actions = "resolveSupport" in action_caps or bool(
            action_caps.get("dataSupport")
        )
        self.workspace.root = self._root_from(params)
        self._initialized = True
        self._dirty = True
        return {
            "capabilities": self._capabilities(),
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }

    def _root_from(self, params: Mapping[str, Any]) -> Path | None:
        """The folder the client opened, or ``None`` for a lone file.

        The command line's ``-i`` is a fallback, not an override: an editor that
        opened a workspace knows better than the shell the server was spawned
        from, and disagreeing with it would put diagnostics on URIs the client
        does not recognise.
        """
        folders = params.get("workspaceFolders")
        if isinstance(folders, Sequence) and folders:
            first = folders[0]
            if isinstance(first, Mapping):
                path = uri_to_path(str(first.get("uri", "")))
                if path is not None and path.is_dir():
                    return path
        for key in ("rootUri", "rootPath"):
            value = params.get(key)
            if not value:
                continue
            path = uri_to_path(str(value)) if key == "rootUri" else Path(str(value))
            if path is not None and path.is_dir():
                return path
        if self.root is not None and self.root.is_dir():
            return self.root
        return None

    def _capabilities(self) -> dict[str, Any]:
        return {
            "positionEncoding": self.workspace.encoding.value,
            "textDocumentSync": {
                "openClose": True,
                "change": _SYNC_INCREMENTAL,
                "save": {"includeText": False},
            },
            "completionProvider": {
                "triggerCharacters": list(_TRIGGERS),
                "resolveProvider": False,
            },
            "hoverProvider": True,
            "definitionProvider": True,
            "referencesProvider": True,
            "documentSymbolProvider": True,
            "documentFormattingProvider": True,
            "renameProvider": {"prepareProvider": True},
            "codeActionProvider": {
                "codeActionKinds": [_QUICK_FIX, _SOURCE_FIX_ALL],
                "resolveProvider": True,
            },
            "workspace": {"workspaceFolders": {"supported": True, "changeNotifications": False}},
        }

    def on_initialized(self, params: Mapping[str, Any]) -> None:
        del params
        if self.watch and self.workspace.root is not None:
            self._start_watching(self.workspace.root)

    def on_shutdown(self, params: Mapping[str, Any]) -> None:
        del params
        self._shutdown = True
        self._stop_watching()
        return None

    def on_exit(self, params: Mapping[str, Any]) -> None:
        del params
        self._running = False

    # ------------------------------------------------------------------ #
    # Document synchronisation
    # ------------------------------------------------------------------ #

    def on_did_open(self, params: Mapping[str, Any]) -> None:
        document = params.get("textDocument") or {}
        uri = str(document.get("uri", ""))
        if not uri:
            return
        self.workspace.open(
            uri,
            str(document.get("text", "")),
            int(document.get("version", 0)),
            str(document.get("languageId", "yaml")),
        )
        self._dirty = True

    def on_did_change(self, params: Mapping[str, Any]) -> None:
        identity = params.get("textDocument") or {}
        uri = str(identity.get("uri", ""))
        current = self.workspace.get(uri)
        if current is None:
            return
        changes = params.get("contentChanges") or []
        if not isinstance(changes, Sequence):
            return
        version = int(identity.get("version", current.version + 1))
        self.workspace.update(current.apply(changes, version, self.workspace.encoding))
        self._dirty = True

    def on_did_save(self, params: Mapping[str, Any]) -> None:
        document = params.get("textDocument") or {}
        text = params.get("text")
        uri = str(document.get("uri", ""))
        current = self.workspace.get(uri)
        if current is not None and isinstance(text, str):
            self.workspace.update(current.replaced(text, current.version))
        self._dirty = True

    def on_did_close(self, params: Mapping[str, Any]) -> None:
        document = params.get("textDocument") or {}
        uri = str(document.get("uri", ""))
        self.workspace.close(uri)
        self._dirty = True

    def on_did_change_watched_files(self, params: Mapping[str, Any]) -> None:
        """The client saw a file change. Whatever it was, the tree is stale."""
        del params
        self.workspace.invalidate()
        self._dirty = True

    def on_did_change_configuration(self, params: Mapping[str, Any]) -> None:
        del params
        self.workspace.reload_config()
        self._dirty = True

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def _on_files_changed(self, batch: Sequence[str]) -> None:
        """A batch from the folder watcher: reload and republish."""
        if any(Path(path).name == "netviz.toml" for path in batch):
            self.workspace.reload_config()
        self.workspace.invalidate()
        self._dirty = True

    def _refresh(self) -> None:
        """Reload the inventory and publish what it says about every file."""
        self._dirty = False
        if not self._initialized:
            return
        try:
            analysis = self.workspace.analysis()
        except Exception as exc:
            self._note(f"the inventory could not be loaded: {exc}")
            return
        grouped = by_file(analysis.report.diagnostics)
        # Three sets, and all three have to be published. The files with
        # problems, obviously. Every open buffer, because an editor only learns
        # a file is *clean* from an empty list — silence reads as "not checked
        # yet". And whatever was published before, so a problem that has been
        # repaired stops being shown in a file nobody has open.
        current = set(grouped) | self._open_files(analysis)
        for relative in sorted(current | self._published):
            self._publish(analysis, relative, grouped.get(relative, []))
        self._published = current

    def _open_files(self, analysis: Analysis) -> set[str]:
        """The open buffers, as paths relative to the analysis' own root."""
        if self.workspace.root is not None:
            return set(self.workspace.buffers())
        return set(analysis.files)

    def _publish(
        self, analysis: Analysis, relative: str, diagnostics: Sequence[Diagnostic]
    ) -> None:
        uri = self.workspace.uri_of(relative, analysis.root)
        text = self.workspace.text_of(relative, analysis.root)
        payload = to_lsp(diagnostics, text, self.workspace.encoding)
        self.notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": payload})

    # ------------------------------------------------------------------ #
    # The analysed tree
    # ------------------------------------------------------------------ #

    def analysis(self) -> Analysis:
        return self.workspace.analysis()

    def index(self) -> SemanticIndex:
        """The name table for the current tree, built on first use per edit."""
        analysis = self.analysis()
        if self._index is None or self._index_for is not analysis:
            self._index = SemanticIndex(
                analysis.inventory,
                text_of=lambda relative: self.workspace.text_of(relative, analysis.root),
                encoding=self.workspace.encoding,
            )
            self._index_for = analysis
        return self._index

    def _document_of(self, params: Mapping[str, Any]) -> tuple[TextDocument, str]:
        """The open buffer a request names, and its path relative to the root.

        Raises:
            ResponseError: The client asked about a document it never opened,
                which is a client bug rather than a user error.
        """
        uri = str((params.get("textDocument") or {}).get("uri", ""))
        document = self.workspace.get(uri)
        if document is None:
            raise ResponseError(INVALID_PARAMS, f"{uri} is not open")
        return document, self._relative_of(uri)

    def _relative_of(self, uri: str) -> str:
        relative = self.workspace.relative_of(uri)
        if relative is not None:
            return relative
        path = uri_to_path(uri)
        return path.name if path is not None else uri

    @staticmethod
    def _position_of(params: Mapping[str, Any]) -> Position:
        return Position.from_dict(params.get("position") or {})

    def _namespace_of(self, relative: str) -> str:
        """The namespace a document written at ``relative`` declares into."""
        parent = str(Path(relative).parent.as_posix())
        return "" if parent in {".", "/"} else parent

    # ------------------------------------------------------------------ #
    # Features
    # ------------------------------------------------------------------ #

    def on_completion(self, params: Mapping[str, Any]) -> dict[str, Any]:
        document, relative = self._document_of(params)
        position = self._position_of(params)
        context = context_at(document, position, self.workspace.encoding)
        items = completion.completions(
            context,
            schema_index(),
            self.analysis().inventory,
            self._namespace_of(relative),
        )
        replace = context.replace.to_dict()
        return {
            "isIncomplete": False,
            "items": [item.to_dict(replace) for item in items],
        }

    def on_hover(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        document, relative = self._document_of(params)
        position = self._position_of(params)
        anchor = self.index().anchor_at(relative, position)
        if anchor is not None:
            text = hover_markdown(anchor, self.analysis().inventory)
            if text:
                return {
                    "contents": {"kind": "markdown", "value": text},
                    "range": anchor.range.to_dict(),
                }
        context = context_at(document, position, self.workspace.encoding)
        text = key_markdown(context, schema_index())
        if not text:
            return None
        return {"contents": {"kind": "markdown", "value": text}}

    def on_definition(self, params: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        _, relative = self._document_of(params)
        anchor = self.index().anchor_at(relative, self._position_of(params))
        if anchor is None or anchor.target is None:
            return None
        index = self.index()
        located = None
        # The interface half of a reference goes to the interface; the element
        # half — and the element's own name — goes to the document.
        if anchor.kind is AnchorKind.REFERENCE_DETAIL and anchor.detail is not None:
            located = index.interface_definition(anchor.target, anchor.detail)
        if located is None:
            located = index.definition_of(anchor.target)
        if located is None:
            return None
        return [self._location(located.relative, located.range)]

    def on_references(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        _, relative = self._document_of(params)
        anchor = self.index().anchor_at(relative, self._position_of(params))
        if anchor is None or anchor.target is None:
            return []
        include_declaration = bool((params.get("context") or {}).get("includeDeclaration", True))
        index = self.index()
        locations = [
            self._location(entry.relative, entry.range)
            for entry in index.references_to(anchor.target)
        ]
        if include_declaration:
            declaration = index.definition_of(anchor.target)
            if declaration is not None:
                locations.insert(0, self._location(declaration.relative, declaration.range))
        return locations

    def on_document_symbol(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        _, relative = self._document_of(params)
        symbols: list[dict[str, Any]] = []
        for anchor in self.index().anchors_in(relative):
            if anchor.kind is not AnchorKind.ELEMENT_NAME:
                continue
            element = self.analysis().inventory.get(anchor.owner)
            symbols.append(
                {
                    "name": short_name(anchor.owner),
                    "detail": element.kind if element is not None else "",
                    "kind": 5,  # SymbolKind.Class — one document, one thing.
                    "range": anchor.range.to_dict(),
                    "selectionRange": anchor.range.to_dict(),
                }
            )
        return symbols

    def _location(self, relative: str, span: Range) -> dict[str, Any]:
        return {
            "uri": self.workspace.uri_of(relative, self.analysis().root),
            "range": span.to_dict(),
        }

    # -- formatting ------------------------------------------------------

    def on_formatting(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        document, relative = self._document_of(params)
        try:
            formatted = format_source(document.text, name=relative)
        except FormatSyntaxError as exc:
            # Formatting a file that does not parse is not a server failure; it
            # is the diagnostic the user is already looking at.
            raise ResponseError(INVALID_REQUEST, f"{relative} cannot be formatted: {exc}") from exc
        if formatted == document.text:
            return []
        return [
            {
                "range": full_range(document.text, self.workspace.encoding).to_dict(),
                "newText": formatted,
            }
        ]

    # -- rename ----------------------------------------------------------

    def on_prepare_rename(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        _, relative = self._document_of(params)
        anchor = self._renameable(relative, self._position_of(params))
        if anchor is None:
            return None
        return {"range": anchor.range.to_dict(), "placeholder": short_name(anchor.target or "")}

    def _renameable(self, relative: str, position: Position) -> Anchor | None:
        """The anchor at ``position``, if renaming it is a thing netviz can do.

        An interface name is deliberately excluded. Renaming one has to rewrite
        the endpoints that land on it as well, and the write path has no
        operation for that yet; offering it and doing half of it would be worse
        than not offering it.
        """
        anchor = self.index().anchor_at(relative, position)
        if anchor is None or anchor.target is None:
            return None
        if anchor.kind in {AnchorKind.ELEMENT_NAME, AnchorKind.REFERENCE}:
            return anchor
        return None

    def on_rename(self, params: Mapping[str, Any]) -> dict[str, Any]:
        _, relative = self._document_of(params)
        new_name = str(params.get("newName", "")).strip()
        anchor = self._renameable(relative, self._position_of(params))
        if anchor is None or anchor.target is None:
            raise ResponseError(INVALID_REQUEST, "only element names can be renamed")
        if not new_name:
            raise ResponseError(INVALID_PARAMS, "the new name is empty")
        if new_name == short_name(anchor.target):
            return {"documentChanges": []} if self._document_changes else {"changes": {}}
        session = self._session()
        try:
            session.apply(RenameElement(address=anchor.target, new_name=new_name))
        except EditError as exc:
            raise ResponseError(INVALID_REQUEST, f"cannot rename: {exc}") from exc
        return self._workspace_edit(session)

    # -- code actions ----------------------------------------------------

    def on_code_action(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        document, relative = self._document_of(params)
        span = Range.from_dict(params.get("range") or {})
        wanted = (params.get("context") or {}).get("only")
        findings = self._findings_in(relative, span)
        actions: list[dict[str, Any]] = []
        if _wants(wanted, _QUICK_FIX):
            actions.extend(self._quick_fixes(document.uri, relative, findings))
        if _wants(wanted, _SOURCE_FIX_ALL) and self._fixable(relative):
            actions.append(
                {
                    "title": "netviz: fix everything that can be fixed",
                    "kind": _SOURCE_FIX_ALL,
                    "data": {"uri": document.uri, "action": "fix-all"},
                }
            )
        if not self._resolve_actions:
            actions = [self._resolved(action) for action in actions]
        return actions

    def on_code_action_resolve(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._resolved(dict(params))

    def _findings_in(self, relative: str, span: Range) -> list[Finding]:
        """The findings of ``relative`` whose range meets ``span``."""
        analysis = self.analysis()
        text = self.workspace.text_of(relative, analysis.root)
        wanted: list[Finding] = []
        for finding in analysis.findings:
            if finding.file != relative:
                continue
            site = finding.site
            mark = site.mark if site is not None else None
            if mark is None:
                continue
            if range_at(text, mark[0], mark[1], self.workspace.encoding).overlaps(span):
                wanted.append(finding)
        return wanted

    def _fixable(self, relative: str) -> bool:
        analysis = self.analysis()
        here = [finding for finding in analysis.findings if finding.file == relative]
        return bool(offers_for(here, analysis.inventory, limit=1))

    def _quick_fixes(
        self, uri: str, relative: str, findings: Sequence[Finding]
    ) -> list[dict[str, Any]]:
        analysis = self.analysis()
        actions: list[dict[str, Any]] = []
        for offer in offers_for(findings, analysis.inventory):
            for fix in offer.fixes:
                actions.append(
                    {
                        "title": f"netviz: {fix.title}",
                        "kind": _QUICK_FIX,
                        "diagnostics": self._diagnostics_for(offer.finding, relative),
                        "data": {
                            "uri": uri,
                            "action": "fix",
                            "finding": finding_key(offer.finding),
                            "fix": fix.key,
                        },
                    }
                )
                if len(actions) >= _MAX_EAGER_ACTIONS:
                    return actions
        return actions

    def _diagnostics_for(self, finding: Finding, relative: str) -> list[dict[str, Any]]:
        """The published diagnostic a fix would clear, so the editor can dim it."""
        analysis = self.analysis()
        text = self.workspace.text_of(relative, analysis.root)
        key = finding_key(finding)
        return [
            to_lsp_diagnostic(entry, text, self.workspace.encoding)
            for entry in analysis.report.diagnostics
            if entry.file == relative and diagnostic_key(entry) == key
        ]

    def _resolved(self, action: dict[str, Any]) -> dict[str, Any]:
        """Attach the workspace edit a code action would apply."""
        if "edit" in action:
            return action
        data = action.get("data") or {}
        if not isinstance(data, Mapping):
            return action
        try:
            edit = self._edit_for(data)
        except (EditError, ResponseError) as exc:
            self._note(f"the quick fix could not be computed: {exc}")
            return action
        if edit is not None:
            action["edit"] = edit
        return action

    def _edit_for(self, data: Mapping[str, Any]) -> dict[str, Any] | None:
        session = self._session()
        if data.get("action") == "fix-all":
            repair(session, settings=self.workspace.validation)
            return self._workspace_edit(session)
        finding = self._finding_by_key(str(data.get("finding", "")))
        if finding is None:
            return None
        fix = self._fix_by_key(finding, str(data.get("fix", "")))
        if fix is None:
            return None
        outcome = apply_fix(session, finding, fix, settings=self.workspace.validation)
        if not outcome.kept:
            self._note(f"{finding.rule}: {fix.title} was not applied — {outcome.reason}")
            return None
        return self._workspace_edit(session)

    def _finding_by_key(self, key: str) -> Finding | None:
        return next(
            (finding for finding in self.analysis().findings if finding_key(finding) == key),
            None,
        )

    def _fix_by_key(self, finding: Finding, key: str) -> Fix | None:
        fixes = fixes_for(finding, self.analysis().inventory)
        if not fixes:
            return None
        return next((fix for fix in fixes if fix.key == key), fixes[0])

    # -- the write path --------------------------------------------------

    def _session(self) -> EditSession:
        """An edit session over the tree, seeded with every unsaved buffer."""
        root = self.workspace.root
        if root is None:
            raise ResponseError(
                INVALID_REQUEST,
                "this needs an open folder: netviz cannot rewrite references "
                "it cannot see (see docs/lsp.md)",
            )
        return EditSession(
            root=root, config=self.workspace.config(), buffers=self.workspace.buffers()
        )

    def _workspace_edit(self, session: EditSession) -> dict[str, Any]:
        analysis = self.analysis()
        return workspace_edit(
            session.changes,
            uri_of=lambda relative: self.workspace.uri_of(relative, analysis.root),
            before_of=lambda relative: (
                session.tree.original_of(relative)
                or self.workspace.text_of(relative, analysis.root)
                or None
            ),
            version_of=self._version_of,
            document_changes=self._document_changes,
            encoding=self.workspace.encoding,
        )

    def _version_of(self, relative: str) -> int | None:
        analysis = self.analysis()
        document = self.workspace.get(self.workspace.uri_of(relative, analysis.root))
        return document.version if document is not None else None

    # ------------------------------------------------------------------ #
    # Watching
    # ------------------------------------------------------------------ #

    def _start_watching(self, root: Path) -> None:
        watcher = FolderWatcher(
            root,
            on_change=lambda batch: self._queue.put(_Event("changed", tuple(batch))),
            on_error=self._note,
        )
        self._watcher = watcher
        watcher.start()

    def _stop_watching(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None


def _wants(only: Any, kind: str) -> bool:
    """Did the client ask for this kind of action, or for anything at all?"""
    if not only or not isinstance(only, Sequence) or isinstance(only, str):
        return True
    return any(kind == str(entry) or kind.startswith(f"{entry}.") for entry in only)


def _guard(
    handler: Callable[[LanguageServer, Mapping[str, Any]], Any],
) -> Callable[[LanguageServer, Mapping[str, Any]], Any]:
    """Refuse everything but ``initialize`` until the client has initialised."""

    def wrapper(server: LanguageServer, params: Mapping[str, Any]) -> Any:
        if not server._initialized:
            raise ResponseError(SERVER_NOT_INITIALIZED, "initialize has not been received")
        return handler(server, params)

    return wrapper


#: Method to handler. Notifications return ``None`` and are never answered.
_HANDLERS: Final[dict[str, Callable[[LanguageServer, Mapping[str, Any]], Any]]] = {
    "initialize": LanguageServer.on_initialize,
    "initialized": LanguageServer.on_initialized,
    "shutdown": LanguageServer.on_shutdown,
    "exit": LanguageServer.on_exit,
    "textDocument/didOpen": _guard(LanguageServer.on_did_open),
    "textDocument/didChange": _guard(LanguageServer.on_did_change),
    "textDocument/didSave": _guard(LanguageServer.on_did_save),
    "textDocument/didClose": _guard(LanguageServer.on_did_close),
    "textDocument/completion": _guard(LanguageServer.on_completion),
    "textDocument/hover": _guard(LanguageServer.on_hover),
    "textDocument/definition": _guard(LanguageServer.on_definition),
    "textDocument/references": _guard(LanguageServer.on_references),
    "textDocument/documentSymbol": _guard(LanguageServer.on_document_symbol),
    "textDocument/formatting": _guard(LanguageServer.on_formatting),
    "textDocument/prepareRename": _guard(LanguageServer.on_prepare_rename),
    "textDocument/rename": _guard(LanguageServer.on_rename),
    "textDocument/codeAction": _guard(LanguageServer.on_code_action),
    "codeAction/resolve": _guard(LanguageServer.on_code_action_resolve),
    "workspace/didChangeWatchedFiles": _guard(LanguageServer.on_did_change_watched_files),
    "workspace/didChangeConfiguration": _guard(LanguageServer.on_did_change_configuration),
}


def serve(
    connection: Connection,
    *,
    root: Path | None = None,
    watch: bool = True,
    log: Callable[[str], None] | None = None,
) -> int:
    """Run one session to completion."""
    return LanguageServer(connection=connection, root=root, watch=watch, log=log).serve()
