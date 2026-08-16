"""The folder watch, so an edit made outside the editor still refreshes.

``netviz watch`` already knows how to watch an inventory: which files count,
which are noise, and how long to wait for a burst of filesystem events to settle
into one save. The server reuses that filter and that debounce rather than
growing a second opinion about what an inventory file is — the two commands are
watching the same folder for the same reason, and a file one of them reacted to
and the other ignored would be a bug nobody could reproduce.

It runs in a thread and posts batches into the server's queue. The server is
single-threaded above that: everything it knows is mutated on one thread, in the
order events arrive, so there is no lock on the inventory and no window in which
a request is answered from a half-updated tree.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from netviz.watch.loop import DEFAULT_DEBOUNCE_MS, InventoryFilter, file_changes

__all__ = ["FolderWatcher"]


class FolderWatcher:
    """Watches one inventory root and calls back with each settled batch."""

    def __init__(
        self,
        root: Path,
        on_change: Callable[[Sequence[str]], None],
        *,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._root = root
        self._on_change = on_change
        self._on_error = on_error
        self._debounce_ms = debounce_ms
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin watching, unless the watcher cannot be built on this platform.

        A missing ``watchfiles`` wheel is not a reason to refuse to be a language
        server: everything else keeps working, the client's own file watching
        still reaches us through ``workspace/didChangeWatchedFiles``, and the
        failure is reported once rather than on every edit.
        """
        if self._thread is not None:  # pragma: no cover - started once
            return
        thread = threading.Thread(target=self._run, name="netviz-lsp-watch", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            changes = file_changes(
                [self._root],
                watch_filter=InventoryFilter(root=self._root),
                debounce_ms=self._debounce_ms,
                stop=self._stop,
            )
            for batch in changes:
                if self._stop.is_set():
                    return
                self._on_change(batch)
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(f"file watching is off: {exc}")
