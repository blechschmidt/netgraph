"""Writing files the same way on every platform netviz runs on.

Four questions have one answer each here, rather than one answer per caller.

**What is a line ending?** ``\\n``, everywhere. Every artefact netviz emits is
defined in bytes — a golden file compared byte for byte, a canonical YAML form
whose whole point is that two people formatting the same document get the same
file, a DOT source a test feeds to ``dot -Tcanon``. Python's text mode would
translate those ``\\n`` into ``\\r\\n`` on Windows, so ``netviz fmt`` would
write a file ``netviz fmt --check`` then reports as unformatted, and a golden
regenerated on Windows would differ from the same golden regenerated on Linux in
every single line. :func:`write_text` and :func:`write_text_atomically` therefore
pass ``newline=""``, which is the "translate nothing" setting.

Reading is the mirror image and needs no helper: the default universal-newline
mode already folds ``\\r\\n`` into ``\\n``, so a CRLF checkout parses. The one
place that deliberately looks at the raw bytes is ``netviz fmt``, which has an
opinion about them (see :mod:`netviz.fmt.runner`); ``.gitattributes`` keeps
this repository's own YAML and goldens at LF so that opinion is not a Windows
CI failure.

**How is a file replaced?** Through a sibling temporary file and
:func:`os.replace`, so a reader sees the old contents or the new ones and never
half of either. That matters twice over: ``netviz fmt`` rewrites the only copy
of a network's configuration, and ``netviz watch`` rewrites a diagram while a
browser is reading it. On Windows :func:`os.replace` fails outright when
*anything* holds the destination open — an antivirus scanner that opened the new
file to inspect it, a viewer that has not closed the old one — so
:func:`replace_atomically` retries briefly rather than turning a transient
sharing violation into a failed render.

**How is a path shown?** Relative to the working directory and with forward
slashes, by :func:`display_path`, wherever netviz prints one for a person to
read — a diff header, a report, a transcript in ``docs/``. The separator on disk
is the platform's; the separator in a document netviz produces is ``/``, so the
same tree produces the same output whoever ran the command.

**What may a generated file be called?** :func:`safe_file_stem` answers for the
one command that derives file names from data it did not write,
``netviz import``. Windows reserves a handful of device names (``NUL``,
``CON``, ``COM1``…) at *every* directory and with *any* extension, so a switch a
device reported as ``con`` would produce a path that cannot be created at all.
"""

from __future__ import annotations

import itertools
import os
import time
from pathlib import Path
from typing import Final

__all__ = [
    "LINE_ENDING",
    "RESERVED_FILE_STEMS",
    "display_path",
    "is_reserved_file_stem",
    "replace_atomically",
    "safe_file_stem",
    "write_bytes_atomically",
    "write_text",
    "write_text_atomically",
]

#: The line ending every netviz artefact uses, on every platform.
LINE_ENDING: Final = "\n"

#: How many times :func:`replace_atomically` retries a rename Windows refused,
#: and how long it waits between attempts. The budget is deliberately small: a
#: sharing violation caused by a scanner clears in milliseconds, and one caused
#: by a viewer holding the file open forever must be reported rather than
#: waited out.
_REPLACE_ATTEMPTS: Final = 5
_REPLACE_BACKOFF_SECONDS: Final = 0.05

#: The MS-DOS device names Windows still reserves, in every directory and with
#: any extension: ``nul.yaml`` is the null device, not a file. Kept lower-cased
#: because the comparison folds case. ``COM0`` and ``LPT0`` are included even
#: though the documented range starts at 1, because Windows resolves them too.
RESERVED_FILE_STEMS: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(10)}
    | {f"lpt{digit}" for digit in range(10)}
)


def display_path(path: Path) -> str:
    """``path`` as a document should quote it: relative to here, forward slashes.

    ``-i`` resolves its argument, so nearly every path netviz holds is
    absolute — which makes for a noisy file list and, worse, for output that
    differs between two machines looking at the same tree. Relative to the
    working directory is what a person typed and what a patch wants; a path
    outside it stays absolute, since a ``../../..`` chain is not an improvement.

    The separator is always ``/``, including on Windows, and that is the reason
    this is one function rather than four spellings of it. These strings end up
    in a unified diff header, in a JUnit ``<property>``, in the first line of a
    report and in a documented transcript — a ``+++ b/examples\\home-lab\\sw.yaml``
    is a patch nothing can apply, and an ``examples\\home-lab`` in the rest is a
    document that would need a second copy of every page quoting it.
    """
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def is_reserved_file_stem(stem: str) -> bool:
    """Would a file called ``stem`` (before its extension) be unnameable?

    True for the MS-DOS device names in :data:`RESERVED_FILE_STEMS`, compared
    without regard to case and to a trailing dot or space, both of which Windows
    strips before resolving the name — so ``NUL``, ``nul`` and ``nul.`` are all
    the null device.

    The answer does not depend on the platform running the check. A tree written
    on Linux is a tree somebody else opens on Windows, and a name that cannot be
    checked out there is a broken inventory wherever it was produced.
    """
    return stem.rstrip(" .").casefold() in RESERVED_FILE_STEMS


def safe_file_stem(stem: str, *, suffix: str = "-file") -> str:
    """``stem``, made nameable on every platform, by appending ``suffix`` if need be.

    Only the reserved device names are changed, and only by extension rather
    than by substitution: a device the network calls ``con`` lands in ``con-file.yaml``,
    which still reads as the name it came from. Everything else is returned
    unchanged, so this is safe to apply to every stem rather than to the ones a
    caller suspects.

    Trailing dots and spaces are also removed, since Windows drops them silently
    and two names differing only in one would resolve to the same file.
    """
    trimmed = stem.rstrip(" .") or stem
    return f"{trimmed}{suffix}" if is_reserved_file_stem(trimmed) else trimmed


def write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` verbatim, translating no line endings.

    The non-atomic counterpart of :func:`write_text_atomically`, for the
    commands that create files rather than replace them — ``netviz init``,
    ``netviz import``, ``netviz schema -o`` — where there is no reader to
    protect from a half-written file.

    Raises:
        OSError: The file cannot be written.
    """
    with path.open("w", encoding=encoding, newline="") as handle:
        handle.write(text)


def write_text_atomically(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text`` in one step, translating no line endings.

    Raises:
        OSError: The file cannot be written, or the replacement failed.
    """
    write_bytes_atomically(path, text.encode(encoding))


#: Serial number of the next temporary file this process creates. See
#: :func:`_temporary_for`.
_temporary_serial = itertools.count()


def _temporary_for(path: Path) -> Path:
    """The scratch file ``path`` is written through before being renamed onto.

    Three things are true of the name at once, and each is load-bearing:

    * It **starts with a dot**, which is the loader's own rule for "do not read
      this" (``NV-L002``), so a leftover from a killed process is inert rather
      than a phantom element with a syntax error.
    * It **ends in .tmp**, for a human looking at the directory.
    * It is **unique to this writer**. Two processes filling one parse cache aim
      at the same destination routinely — the key is the content, so they write
      identical bytes and neither cares who wins. Sharing a scratch file is what
      they cannot survive: on Windows, one opening it while the other holds it
      open is a sharing violation, and one renaming it away mid-write leaves the
      other renaming a file that is no longer there. Either way the entry is
      simply never written, which is how a four-process race over forty files
      came back with thirty-nine of them cached. POSIX tolerates the sharing;
      the fix is not POSIX-specific because the bug was never really about
      Windows, only visible there.
    """
    return path.with_name(f".{path.name}.{os.getpid()}-{next(_temporary_serial)}.netviz.tmp")


def write_bytes_atomically(
    path: Path, payload: bytes, *, mode: int | None = None, sync: bool = True
) -> None:
    """Replace ``path`` with ``payload`` in one step.

    The temporary file is a sibling, so the replacement stays within one
    filesystem and :func:`os.replace` is therefore a rename rather than a copy.
    It is hidden (a leading dot) and suffixed ``.tmp`` so that a run interrupted
    between the write and the rename leaves something the loader skips rather
    than a stray document.

    Its name also carries the writer's process id and a counter, so that two
    writers aiming at one destination never share it (:func:`_temporary_for`).

    Args:
        mode: Permission bits to set before the replacement, or ``None`` to leave
            the umask's answer alone. Passing a mode is for an artefact whose
            permissions should not depend on the shell that launched netviz: a
            rendered diagram under a ``0o077`` umask would come out unreadable to
            everyone but its owner, which is not what somebody who asked for a
            picture meant. A rewritten inventory is the opposite case and should
            inherit. Ignored on Windows, which has no such bits.
        sync: Force the bytes to the platter before the rename. On by default,
            because for everything a *user* asked netviz to write — a rewritten
            inventory, a rendered diagram — "the command returned, and the file
            is empty" after a power cut is not an acceptable outcome.

            :mod:`netviz.loader.cache` passes ``False``: an ``fsync`` per file
            costs more than parsing the file it is caching (measured: 138 entries
            take 405 ms with, 55 ms without), and a cache entry lost or truncated
            by a crash is *already* a case that has to be handled, because it is
            indistinguishable from one written by a killed process.

    Raises:
        OSError: The file cannot be written, or the replacement failed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_for(path)
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            if sync:
                handle.flush()
                os.fsync(handle.fileno())
        if mode is not None and os.name != "nt":
            os.chmod(temporary, mode)
        replace_atomically(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def replace_atomically(source: Path, destination: Path) -> None:
    """``os.replace``, retrying the sharing violation only Windows raises.

    On POSIX a rename over an open file always succeeds; the reader keeps the
    inode it opened. Windows refuses it with ``PermissionError`` whenever
    another handle is open on either path, which for a file just written happens
    routinely and briefly — an indexer or an antivirus scanner opened it to look
    at what changed. Retrying for a fraction of a second turns that from a
    failed ``netviz fmt`` into a rename that takes 50ms.

    A handle that is *not* transient — an editor holding the destination open —
    still ends as :class:`PermissionError`, which is the honest outcome: the
    write did not happen, and the caller reports it.

    Raises:
        OSError: The replacement failed. On Windows, only after
            :data:`_REPLACE_ATTEMPTS` tries.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            # Nothing on POSIX makes this transient, so retrying there would
            # only delay an error that is already final.
            if os.name != "nt" or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))
