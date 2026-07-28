"""Live re-rendering of an inventory as it is edited.

Three pieces, each usable on its own:

:mod:`~netgraph.watch.pipeline`
    One ``load → validate → render`` cycle, and :class:`LiveRender`, the state
    it publishes to. The cycle never raises for a problem the inventory itself
    can cause, and a failed cycle leaves the last good payload in place.
:mod:`~netgraph.watch.loop`
    :func:`run_watch`, which repeats the cycle for every batch of changes, and
    :func:`file_changes`, the ``watchfiles``-backed source of those batches.
:mod:`~netgraph.watch.server`
    :class:`PreviewServer`, a loopback-bound HTTP preview whose page polls for
    new revisions and swaps the diagram in without a manual reload.

The split is what makes the loop testable: ``run_watch`` takes any iterable of
change batches, so a test drives it with a list instead of a filesystem.
"""

from __future__ import annotations

from netgraph.watch.loop import (
    DEFAULT_DEBOUNCE_MS,
    InventoryFilter,
    file_changes,
    run_watch,
)
from netgraph.watch.pipeline import (
    STAMP_FORMAT,
    CycleResult,
    LiveRender,
    Problem,
    RenderRequest,
    Snapshot,
    Status,
    run_cycle,
    write_atomically,
)
from netgraph.watch.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    STATUS_PATH,
    PreviewServer,
    ServeError,
    describe_exposure,
    is_loopback,
    status_document,
)

__all__ = [
    "DEFAULT_DEBOUNCE_MS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "STAMP_FORMAT",
    "STATUS_PATH",
    "CycleResult",
    "InventoryFilter",
    "LiveRender",
    "PreviewServer",
    "Problem",
    "RenderRequest",
    "ServeError",
    "Snapshot",
    "Status",
    "describe_exposure",
    "file_changes",
    "is_loopback",
    "run_cycle",
    "run_watch",
    "status_document",
    "write_atomically",
]
