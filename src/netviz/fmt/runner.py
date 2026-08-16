"""Applying the canonical form to files: what to visit, and what to do there.

The three modes ``netviz fmt`` offers differ only in what they do with the
formatted text — rewrite the file, report that it differs, or print a diff — so
they share one pass and are distinguished by a :class:`Mode`. Each file yields
exactly one :class:`FileResult`, whatever happened to it, which is what lets the
command report "3 reformatted, 1 unchanged, 1 failed" without a second walk.

Which files are visited is not this module's decision. It calls
:func:`~netviz.loader.tree.iter_inventory_files`, the same discovery
``validate`` and ``render`` use, so ``.netvizignore``, the dot- and
underscore-prefix rules and the ``.yaml``/``.yml`` suffixes mean here exactly
what they mean everywhere else. A file the loader would not read is a file
``fmt`` must not rewrite: reformatting something outside the inventory would be
a formatter exceeding its remit, and reformatting a file the inventory ignores
would be worse — it might not be netviz YAML at all.
"""

from __future__ import annotations

import difflib
import enum
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from netviz.fmt.canonical import FormatSyntaxError, format_stream
from netviz.fmt.verify import verify
from netviz.fsio import display_path, write_text_atomically
from netviz.loader.inventory import LoadError
from netviz.loader.tree import iter_inventory_files

__all__ = [
    "STDIN_NAME",
    "FileResult",
    "Mode",
    "Outcome",
    "Summary",
    "diff_text",
    "display_path",
    "format_paths",
    "format_source",
]

#: What a stream read from stdin is called in diffs and diagnostics.
STDIN_NAME = "<stdin>"

#: An absolute path as :func:`display_path` spells one: a leading slash, or a
#: Windows drive letter. Both are paths a diff header must not prefix.
_ABSOLUTE: Final = re.compile(r"^(?:/|[A-Za-z]:/)")


class Mode(enum.Enum):
    """What to do with a file whose canonical form differs from its contents."""

    #: Rewrite it. The default, and the only mode that touches the disk.
    WRITE = "write"
    #: Leave it and report it, so CI can fail on it.
    CHECK = "check"
    #: Print a unified diff of what would change.
    DIFF = "diff"


class Outcome(enum.Enum):
    """How one file came out."""

    #: Already canonical; nothing to do in any mode.
    UNCHANGED = "unchanged"
    #: Differs from its canonical form (and was rewritten, in ``WRITE`` mode).
    CHANGED = "changed"
    #: Could not be formatted. ``FileResult.error`` says why.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FileResult:
    """What happened to one file."""

    #: The file, as the user would name it.
    path: Path
    outcome: Outcome
    #: Why it failed, for :data:`Outcome.FAILED`.
    error: str | None = None
    #: Unified diff of the change, populated only in :data:`Mode.DIFF`.
    diff: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAILED

    @property
    def display(self) -> str:
        """``path`` as the user would type it: relative to where they are."""
        return display_path(self.path)


@dataclass(frozen=True, slots=True)
class Summary:
    """Every file's result, plus the problems discovery itself hit."""

    results: tuple[FileResult, ...]
    #: Unreadable directories, symlink loops, broken ignore files. Reported but
    #: never fatal, exactly as ``validate`` treats them.
    discovery_errors: tuple[LoadError, ...] = ()

    def of(self, outcome: Outcome) -> tuple[FileResult, ...]:
        return tuple(result for result in self.results if result.outcome is outcome)

    @property
    def changed(self) -> tuple[FileResult, ...]:
        return self.of(Outcome.CHANGED)

    @property
    def unchanged(self) -> tuple[FileResult, ...]:
        return self.of(Outcome.UNCHANGED)

    @property
    def failures(self) -> tuple[FileResult, ...]:
        return self.of(Outcome.FAILED)

    def rejects(self, mode: Mode) -> bool:
        """Should the run exit non-zero?

        A failure always rejects. A *change* only rejects in the two modes that
        wrote nothing: ``--check`` and ``--diff`` exist to be a gate, and their
        exit status is the answer. Plain ``netviz fmt`` reformatting a file is
        it succeeding, so it exits 0 — the same way ``gofmt -w`` and
        ``ruff format`` do.
        """
        if self.failures:
            return True
        return bool(self.changed) and mode is not Mode.WRITE


def format_source(text: str, *, name: str) -> str:
    """The canonical form of ``text``, checked to still mean the same thing.

    This is the whole of the formatter's contract in one function: parse,
    canonicalise, and refuse to return anything whose meaning moved.

    Raises:
        FormatSyntaxError: ``text`` is not well-formed YAML, or the canonical
            form did not survive being read back. The second case is a bug in
            netviz and says so; either way nothing is written.
    """
    formatted = format_stream(text)
    problem = verify(text, formatted, name=name)
    if problem is not None:
        raise FormatSyntaxError(
            f"refusing to format {name}: {problem}. This is a bug in netviz -- "
            f"the file has not been touched; please report it with the file that caused it"
        )
    return formatted


def diff_text(before: str, after: str, *, name: str) -> str:
    """A unified diff of one file's reformatting, or ``""`` if nothing changed.

    A relative name gets git's ``a/``/``b/`` prefixes so ``git apply`` reads the
    result; an absolute one does not, because ``a//home/you/x.yaml`` is neither
    a path ``-p1`` can strip nor something anyone wants to look at. A Windows
    path is absolute in a second way — ``C:/net/x.yaml``, a drive letter rather
    than a leading slash — which :data:`_ABSOLUTE` is here to recognise;
    prefixing that would produce ``a/C:/net/x.yaml``, equally unusable.
    """
    prefixed = not _ABSOLUTE.match(name)
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}" if prefixed else name,
        tofile=f"b/{name}" if prefixed else name,
        n=3,
    )
    text = "".join(lines)
    # A diff whose last hunk has no trailing newline would otherwise run into
    # the next file's header.
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def format_paths(roots: Sequence[Path], *, mode: Mode) -> Summary:
    """Format every inventory file below each of ``roots``.

    A path may be a directory to walk or a single YAML file. The same file
    reached through two roots is visited once, in the order it was first
    discovered, so a diff does not repeat itself.

    Raises:
        LoaderError: One of ``roots`` does not exist, or is neither a directory
            nor a YAML file.
    """
    results: list[FileResult] = []
    problems: list[LoadError] = []
    for path in _discover(roots, problems):
        results.append(_format_file(path, mode=mode))
    return Summary(results=tuple(results), discovery_errors=tuple(problems))


def _discover(roots: Sequence[Path], problems: list[LoadError]) -> Iterator[Path]:
    """Every file to format, deduplicated across ``roots``, in discovery order.

    Raises:
        LoaderError: A root does not exist or is not something to walk.
    """
    seen: set[Path] = set()
    for root in roots:
        for found in iter_inventory_files(root, errors=problems):
            resolved = found.path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield found.path


def _format_file(path: Path, *, mode: Mode) -> FileResult:
    """Read, format and — in :data:`Mode.WRITE` — rewrite one file."""
    try:
        raw = path.read_bytes()
        # ``utf-8-sig`` matches the loader, which tolerates a byte-order mark.
        original = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return FileResult(path=path, outcome=Outcome.FAILED, error=str(exc))

    name = display_path(path)
    try:
        formatted = format_source(original, name=name)
    except FormatSyntaxError as exc:
        return FileResult(path=path, outcome=Outcome.FAILED, error=str(exc))

    # Compared as bytes, not as text. Decoding is lossy in exactly the two ways
    # the canonical form has an opinion about: a leading byte-order mark and
    # CRLF line endings both vanish into an identical ``str``, so a file
    # carrying either would be declared unchanged and keep them forever.
    encoded = formatted.encode("utf-8")
    if encoded == raw:
        return FileResult(path=path, outcome=Outcome.UNCHANGED)
    if mode is Mode.WRITE:
        try:
            _write(path, formatted)
        except OSError as exc:
            return FileResult(path=path, outcome=Outcome.FAILED, error=str(exc))
    diff = diff_text(original, formatted, name=name) if mode is Mode.DIFF else ""
    return FileResult(path=path, outcome=Outcome.CHANGED, diff=diff)


def _write(path: Path, text: str) -> None:
    """Replace ``path``'s contents atomically.

    Writing through a temporary file in the same directory means an interrupted
    run leaves either the old file or the new one, never half of either — which
    matters more than usual when the thing being rewritten is the only copy of a
    network's configuration.

    :mod:`netviz.fsio` owns both halves of the platform behaviour: no newline
    translation, because the canonical form is defined in bytes and is the same
    bytes everywhere (a CRLF-translated write would produce a file this very
    function's ``--check`` mode then reports as unformatted), and a rename that
    tolerates the transient sharing violation Windows raises when something else
    still has the file open.

    No ``mode`` is asked for, so the rewritten file gets whatever the umask
    gives — which is what this function has always done, and what an ordinary
    editor writing the same file would do.

    Raises:
        OSError: The file or its directory cannot be written.
    """
    write_text_atomically(path, text)
