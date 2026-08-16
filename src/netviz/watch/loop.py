"""The watch loop: what counts as a change, and what to do when one arrives.

The loop is split from its source of changes so that both halves can be tested
without touching a filesystem clock. :func:`run_watch` consumes any iterable of
change batches; :func:`file_changes` is the one that comes from ``watchfiles``.

What is watched is the whole inventory tree, but only a few kinds of file
provoke a re-render (:class:`InventoryFilter`): the YAML documents themselves,
``netviz.toml`` and ``.netvizignore``. Everything else — an editor swap
file, a rendered diagram, a README — is noise that would rebuild the graph for
nothing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Protocol

from netviz.config import CONFIG_FILE_NAME
from netviz.errors import RenderError
from netviz.loader import IGNORE_FILE_NAME, YAML_SUFFIXES
from netviz.watch.pipeline import (
    CycleResult,
    LiveRender,
    RenderRequest,
    Status,
    run_cycle,
    write_atomically,
)

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from watchfiles import Change

__all__ = [
    "DEFAULT_DEBOUNCE_MS",
    "InventoryFilter",
    "StopSignal",
    "file_changes",
    "run_watch",
]

#: Milliseconds of quiet before a burst of changes is treated as one edit, per
#: platform. A single "save" in an editor is several filesystem events
#: (truncate, write, rename, chmod); rendering each one would be both slow and
#: wrong, so the window has to be wider than the span those events arrive over —
#: and that span is a property of the backend ``watchfiles`` uses, not of the
#: editor.
#:
#: ``inotify`` delivers each event as it happens, so on Linux the events of one
#: save land within a millisecond or two of each other and 300ms is already
#: generous. ``FSEvents`` on macOS coalesces events and delivers them in batches
#: on its own schedule, and ``ReadDirectoryChangesW`` on Windows spreads a
#: save-by-rename over a noticeably longer period — an editor writing a
#: temporary file, flushing it, renaming it over the original and updating the
#: timestamps can arrive as two bursts. Keeping 300ms there would re-render
#: twice for one keystroke, which is what the debounce exists to prevent.
#:
#: These are defaults, not limits: ``--debounce`` overrides them, and a slow
#: inventory on a fast disk is a reason to raise the number rather than a bug.
_DEBOUNCE_MS: Final[dict[str, int]] = {"linux": 300, "darwin": 700, "win32": 700}

#: The debounce window for the platform netviz is running on.
DEFAULT_DEBOUNCE_MS: Final = _DEBOUNCE_MS.get(sys.platform, 300)

#: How long the Rust watcher blocks before handing control back so a stop
#: request can be noticed. Only affects shutdown latency, and it is the reason
#: Ctrl-C is answered within a second rather than at the next file change:
#: without it the watcher would sit in a blocking read on every platform.
_RUST_TIMEOUT_MS: Final = 1_000

#: Directory names never worth descending into, on top of the loader's own
#: rule that a component starting with ``.`` or ``_`` is skipped.
_IGNORED_DIRS: Final[tuple[str, ...]] = (
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
)

#: File names outside :data:`YAML_SUFFIXES` that still change the result.
_EXTRA_FILES: Final[frozenset[str]] = frozenset({CONFIG_FILE_NAME, IGNORE_FILE_NAME})


class StopSignal(Protocol):
    """Anything ``watchfiles`` accepts as a stop event, e.g. ``threading.Event``."""

    def is_set(self) -> bool: ...  # pragma: no cover - structural typing only


class InventoryFilter:
    """Decides whether a filesystem event should trigger a re-render.

    Kept deliberately generous in one direction: a path with no suffix is
    accepted because it is probably a directory being created, renamed or
    deleted, and the documents it carries may not produce events of their own.
    A spurious re-render costs a moment; a missed one leaves a stale diagram on
    screen, which is the failure this whole command exists to prevent.
    """

    def __init__(
        self,
        *,
        root: Path,
        ignore: Iterable[Path] = (),
        only: Iterable[Path] = (),
    ) -> None:
        self.root = root
        #: Paths that must never trigger a cycle — above all the render's own
        #: output, which would otherwise make the watcher feed itself forever.
        self.ignore = frozenset(_resolve(path) for path in ignore)
        #: When non-empty, the only documents that count. A single-file
        #: inventory is watched through its directory, because editors replace
        #: a file rather than rewrite it and a watch on the file itself would
        #: not survive the first save; this keeps its neighbours out.
        self.only = frozenset(_resolve(path) for path in only)

    def __call__(self, change: Change | int, path: str) -> bool:
        return self.accepts(Path(path))

    def accepts(self, path: Path) -> bool:
        resolved = _resolve(path)
        if resolved in self.ignore:
            return False
        if self.only and resolved not in self.only and path.name not in _EXTRA_FILES:
            return False

        parts = _relative_parts(path, self.root)
        for component in parts[:-1]:
            if component in _IGNORED_DIRS or _is_hidden_component(component):
                return False

        name = parts[-1] if parts else path.name
        if name in _EXTRA_FILES:
            return True
        if _is_hidden_component(name) or name in _IGNORED_DIRS:
            return False

        suffix = Path(name).suffix
        if not suffix:
            # Very likely a directory event; see the class docstring.
            return True
        return suffix.lower() in YAML_SUFFIXES


def _is_hidden_component(component: str) -> bool:
    """Mirror ``NG-L002``: the loader never descends into ``.x`` or ``_x``."""
    return component.startswith((".", "_"))


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    """The parts of ``path`` below ``root``; the bare name if it is outside."""
    try:
        return PurePosixPath(path.relative_to(root).as_posix()).parts
    except ValueError:
        return (path.name,)


def _resolve(path: Path) -> Path:
    """Absolute path without touching the filesystem (the file may not exist)."""
    return Path(os.path.abspath(path))


def file_changes(
    paths: Sequence[Path],
    *,
    watch_filter: InventoryFilter,
    debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    stop: StopSignal | None = None,
) -> Generator[tuple[str, ...], None, None]:
    """Yield one batch of changed paths per debounced burst of events.

    The generator owns the underlying watcher: closing it (or letting it be
    garbage collected) releases the filesystem handles.

    Raises:
        ImportError: ``watchfiles`` is not installed.
    """
    watch = _watchfiles_watch()
    for batch in watch(
        *paths,
        watch_filter=watch_filter,
        debounce=debounce_ms,
        # ``step`` is the quiet period that ends a burst early. Sampling at a
        # tenth of the debounce window keeps a single save from waiting the
        # full window while still grouping the events it produces.
        step=max(1, debounce_ms // 10),
        stop_event=stop,
        rust_timeout=_RUST_TIMEOUT_MS,
        # Ctrl-C ends the watch; it is the documented way to stop, not a fault.
        raise_interrupt=False,
    ):
        yield tuple(sorted(path for _, path in batch))


def _watchfiles_watch() -> Any:
    """Import ``watchfiles`` lazily, with a message that says what to install.

    The import costs a Rust extension module, which no other command needs; it
    also keeps ``netviz`` usable on a platform without a prebuilt wheel for
    everything except this one feature.
    """
    try:
        from watchfiles import watch
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "the 'watch' command needs the watchfiles package; install it with "
            "'pip install watchfiles' (or reinstall netviz, which depends on it)"
        ) from exc
    return watch


def run_watch(
    request: RenderRequest,
    changes: Iterable[Sequence[str]],
    *,
    output: Path | None = None,
    live: LiveRender | None = None,
    on_result: Callable[[CycleResult], None] | None = None,
    on_change: Callable[[Sequence[str]], None] | None = None,
) -> LiveRender:
    """Render once, then again after every batch in ``changes``.

    The first cycle runs before the stream is touched, so a watch started
    against a broken inventory reports the breakage immediately instead of
    sitting silent until someone edits a file.

    Args:
        request: What to render, held constant for the whole run.
        changes: Batches of changed paths, e.g. from :func:`file_changes`.
        output: File the successful renders are written to, atomically.
        live: State the preview server reads; created if not supplied.
        on_result: Called with every cycle's outcome, for reporting.
        on_change: Called with each batch before the cycle it triggers.

    Returns:
        The live state, so a caller that did not supply one can inspect it.
    """
    state = live if live is not None else LiveRender(request.output_format)

    def cycle() -> None:
        result = _render_and_store(request, output)
        state.publish(result)
        if on_result is not None:
            on_result(result)

    cycle()
    for batch in changes:
        if on_change is not None:
            on_change(batch)
        cycle()
    return state


def _render_and_store(request: RenderRequest, output: Path | None) -> CycleResult:
    """Run one cycle and persist its payload, if there is anywhere to put it.

    A write that fails demotes the cycle to ``failed`` and discards the payload,
    so nothing is published that a reader cannot actually see: the file on disk
    and the diagram the preview serves must not disagree.
    """
    result = run_cycle(request)
    if output is None or result.payload is None:
        return result
    try:
        write_atomically(output, result.payload)
    except RenderError as exc:
        return replace(result, status=Status.FAILED, payload=None, message=str(exc))
    return result
