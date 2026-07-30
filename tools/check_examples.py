#!/usr/bin/env python3
"""Run the ``netgraph`` examples in the documentation and diff what they print.

A transcript in a document is a promise about the tool, and the only kind of
promise that survives a refactor is one a test makes. So every fenced
``console`` or ``bash`` block that invokes ``netgraph`` carries a marker on the
line above it saying what should happen to it:

``<!-- run: cwd=examples/quickstart -->``
    Executable. Each ``$ netgraph …`` line is run in ``cwd`` (relative to the
    repository root, defaulting to the root itself) and the lines beneath it
    must be exactly what it prints. ``rc=1`` may be added to assert the exit
    code of the last command. A line consisting of ``...`` matches any run of
    output lines, for transcripts too long to be worth pasting whole.

``<!-- norun: <reason> -->``
    Not executable, and the reason says why — it needs a live device, it starts
    a server, it writes into a directory the reader is expected to have, or the
    paths in it are illustrative.

The point of requiring one of the two on *every* block is that neither state is
the silent default: an example is either checked or it is explicitly excused.

Usage::

    python tools/check_examples.py                # check every marked block
    python tools/check_examples.py --list         # what is checked and what is excused
    python tools/check_examples.py --update       # rewrite the output of failing run blocks
    python tools/check_examples.py docs/ipam.md   # narrow to some files

``tests/test_docs.py`` runs the check, one test per block.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

#: Directories that hold no documentation.
SKIP_PARTS: Final = {".venv", ".cloop", ".git", ".hypothesis", "node_modules", "site-packages"}

_FENCE_OPEN: Final = re.compile(r"^```(console|bash|sh|shell)\s*$")
_FENCE_CLOSE: Final = re.compile(r"^```\s*$")
_RUN_MARKER: Final = re.compile(r"^<!--\s*run:(?P<args>[^>]*?)-->\s*$")
_NORUN_MARKER: Final = re.compile(r"^<!--\s*norun:\s*(?P<reason>[^>]*?)-->\s*$")
#: ``netgraph`` at the start of a command, prompt or not.
_INVOKES: Final = re.compile(r"(?:^|[|(]\s*|&&\s*|;\s*)(?:\$\s+)?netgraph\b")

#: Environment that makes output reproducible: no colour, a fixed width, and no
#: inherited configuration.
#:
#: The environment is *built* rather than inherited, which is the point -- a
#: reader's ``netgraph.toml`` or ``NO_COLOR`` must not change what the docs are
#: checked against. That makes the platform-specific entries below load-bearing
#: rather than defensive: on Windows a process started without ``SYSTEMROOT``
#: cannot initialise, and one started without ``PYTHONUTF8`` writes its output in
#: the ANSI code page, so every em dash and box-drawing character in a documented
#: transcript would come back mangled.
ENV: Final = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "80",
    "LINES": "40",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "PYTHONHASHSEED": "0",
    # POSIX honours the first, Windows the second and third. All three say the
    # same thing: this program's input and output are UTF-8.
    "LC_ALL": "C.UTF-8",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    **{
        # Passed through when the platform has them, absent when it does not, so
        # neither branch carries an empty value that means something else.
        # ``HOME``/``USERPROFILE`` keep a command that resolves ``~`` from writing
        # into the real one; the rest are what CPython and the Windows loader need
        # to start at all.
        name: value
        for name in ("HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "PATHEXT")
        if (value := os.environ.get(name))
    },
}

ELLIPSIS: Final = "..."


@dataclass
class Block:
    """One fenced example, with the marker that decides its fate."""

    path: Path
    #: 1-based line number of the opening fence.
    line: int
    language: str
    lines: list[str]
    runnable: bool
    #: For ``run`` blocks: the working directory, relative to the repo root.
    cwd: str = "."
    #: For ``run`` blocks: the exit code the last command must return, if given.
    rc: int | None = None
    #: For ``norun`` blocks: why not.
    reason: str = ""

    @property
    def id(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}"

    def __str__(self) -> str:
        return self.id


@dataclass
class Unmarked:
    """A block that invokes netgraph and says nothing about whether it runs."""

    path: Path
    line: int
    lines: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}"


# --------------------------------------------------------------------------- #
# Finding the blocks
# --------------------------------------------------------------------------- #


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.md")
        if not SKIP_PARTS & set(path.parts) and "fixtures" not in path.parts
    )


def invokes_netgraph(lines: Sequence[str]) -> bool:
    return any(_INVOKES.search(line) for line in lines)


def scan(path: Path) -> tuple[list[Block], list[Unmarked]]:
    """Every netgraph-invoking example in one file."""
    text = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    unmarked: list[Unmarked] = []
    index = 0
    while index < len(text):
        opening = _FENCE_OPEN.match(text[index])
        if opening is None:
            index += 1
            continue
        start = index
        index += 1
        body: list[str] = []
        while index < len(text) and not _FENCE_CLOSE.match(text[index]):
            body.append(text[index])
            index += 1
        index += 1  # step over the closing fence
        if not invokes_netgraph(body):
            continue

        marker = _preceding_marker(text, start)
        if marker is None:
            unmarked.append(Unmarked(path, start + 1, body))
            continue
        run = _RUN_MARKER.match(marker)
        if run is not None:
            options = dict(part.split("=", 1) for part in run["args"].split() if "=" in part)
            blocks.append(
                Block(
                    path=path,
                    line=start + 1,
                    language=opening.group(1),
                    lines=body,
                    runnable=True,
                    cwd=options.get("cwd", "."),
                    rc=int(options["rc"]) if "rc" in options else None,
                )
            )
            continue
        norun = _NORUN_MARKER.match(marker)
        assert norun is not None
        blocks.append(
            Block(
                path=path,
                line=start + 1,
                language=opening.group(1),
                lines=body,
                runnable=False,
                reason=norun["reason"].strip(),
            )
        )
    return blocks, unmarked


def _preceding_marker(text: Sequence[str], fence: int) -> str | None:
    """The nearest marker above a fence, skipping blank lines."""
    cursor = fence - 1
    while cursor >= 0 and not text[cursor].strip():
        cursor -= 1
    if cursor < 0:
        return None
    candidate = text[cursor].strip()
    if _RUN_MARKER.match(candidate) or _NORUN_MARKER.match(candidate):
        return candidate
    return None


def all_blocks(paths: Sequence[Path] | None = None) -> tuple[list[Block], list[Unmarked]]:
    blocks: list[Block] = []
    unmarked: list[Unmarked] = []
    for path in paths or markdown_files():
        found, missing = scan(path)
        blocks.extend(found)
        unmarked.extend(missing)
    return blocks, unmarked


# --------------------------------------------------------------------------- #
# Running one block
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    """A command in a transcript and the output written beneath it."""

    command: str
    expected: list[str]


def steps_of(block: Block) -> list[Step]:
    """Split a transcript into commands and their output.

    Prompts are ``$ ``; a command may be continued with a trailing backslash.
    """
    steps: list[Step] = []
    lines = list(block.lines)
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("$ "):
            if steps:
                steps[-1].expected.append(line)
                index += 1
                continue
            raise ValueError(f"{block.id}: output before the first '$ ' prompt: {line!r}")
        command = line[2:]
        while command.rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            command = command.rstrip().removesuffix("\\") + " " + lines[index].strip()
        steps.append(Step(command=command.strip(), expected=[]))
        index += 1
    return steps


def entry_point() -> list[str]:
    """How to start netgraph so that it calls itself what the docs call it.

    Click takes the program name from ``sys.argv[0]``, so a usage error raised
    under ``python -m netgraph`` would document a usage line no reader will ever
    see. The installed console script is therefore preferred, and the module form
    is the fallback for a checkout that has not been installed.

    Where to look for it is ``sysconfig``'s answer and not "next to the
    interpreter", because those are the same directory only on POSIX: Windows
    installs console scripts into ``Scripts\\`` beside ``python.exe``, so the
    naive lookup found nothing there, fell through to the module form, and made
    every documented usage line fail the comparison on that platform alone --
    which reads like a bug in netgraph rather than in this lookup. ``.exe`` is
    tried as well as the bare name for the same reason.
    """
    directories = [Path(sysconfig.get_path("scripts")), Path(sys.executable).parent]
    for directory in directories:
        for suffix in ("", ".exe"):
            script = directory / f"netgraph{suffix}"
            if script.is_file():
                return [str(script)]
    return [sys.executable, "-m", "netgraph"]


def argv_for(command: str) -> list[str]:
    """The argument vector for a documented ``netgraph …`` command line."""
    parts = shlex.split(command)
    if not parts or parts[0] != "netgraph":
        raise ValueError(
            f"only a bare 'netgraph …' invocation can be run; got {command!r}. "
            "Use a '<!-- norun: … -->' marker for anything needing a shell."
        )
    return [*entry_point(), *parts[1:]]


def run(block: Block) -> tuple[list[str], int]:
    """Execute a block, returning its combined output and last exit code."""
    cwd = (REPO_ROOT / block.cwd).resolve()
    produced: list[str] = []
    code = 0
    for step in steps_of(block):
        completed = subprocess.run(
            argv_for(step.command),
            capture_output=True,
            text=True,
            # Named rather than left to the locale: ``text=True`` decodes with
            # the ANSI code page on Windows, where an em dash in a documented
            # transcript comes back as two mojibake characters -- or raises.
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=ENV,
            timeout=120,
        )
        code = completed.returncode
        produced.append(f"$ {step.command}")
        produced.extend((completed.stdout + completed.stderr).splitlines())
    return produced, code


def matches(expected: Sequence[str], produced: Sequence[str]) -> bool:
    """Compare a transcript, treating a lone ``...`` as "any lines here"."""
    want = [line.rstrip() for line in _trim(expected)]
    got = [line.rstrip() for line in _trim(produced)]
    chunks: list[list[str]] = [[]]
    for line in want:
        if line.strip() == ELLIPSIS:
            chunks.append([])
        else:
            chunks[-1].append(line)
    if len(chunks) == 1:
        return got == chunks[0]

    # Anchor the first chunk at the start and the last at the end; the rest may
    # appear anywhere in order.
    head, *middle = chunks
    tail = middle.pop() if middle else []
    if got[: len(head)] != head:
        return False
    cursor = len(head)
    if tail:
        if got[len(got) - len(tail) :] != tail:
            return False
        end = len(got) - len(tail)
    else:
        end = len(got)
    for chunk in middle:
        if not chunk:
            continue
        found = _find(got, chunk, cursor, end)
        if found is None:
            return False
        cursor = found + len(chunk)
    return True


def _find(haystack: Sequence[str], needle: Sequence[str], start: int, end: int) -> int | None:
    for position in range(start, end - len(needle) + 1):
        if list(haystack[position : position + len(needle)]) == list(needle):
            return position
    return None


def _trim(lines: Sequence[str]) -> list[str]:
    result = list(lines)
    while result and not result[-1].strip():
        result.pop()
    return result


def check(block: Block) -> str | None:
    """``None`` when the block is fine, otherwise the complaint."""
    if not block.runnable:
        if not block.reason:
            return "the 'norun' marker gives no reason"
        return None
    produced, code = run(block)
    if block.rc is not None and code != block.rc:
        return f"exit code {code}, documented as {block.rc}"
    if not matches(block.lines, produced):
        return "output differs from the transcript:\n" + _diff(block.lines, produced)
    return None


def _diff(expected: Sequence[str], produced: Sequence[str]) -> str:
    import difflib

    return "\n".join(
        difflib.unified_diff(
            [line.rstrip() for line in _trim(expected)],
            [line.rstrip() for line in _trim(produced)],
            fromfile="documented",
            tofile="produced",
            lineterm="",
        )
    )


# --------------------------------------------------------------------------- #
# Updating
# --------------------------------------------------------------------------- #


def update(block: Block) -> bool:
    """Replace a run block's transcript with what it actually prints."""
    produced, _ = run(block)
    if matches(block.lines, produced):
        return False
    text = block.path.read_text(encoding="utf-8").splitlines()
    end = block.line  # the line after the opening fence, 0-based
    while end < len(text) and not _FENCE_CLOSE.match(text[end]):
        end += 1
    text[block.line : end] = produced
    block.path.write_text("\n".join(text) + "\n", encoding="utf-8")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the documented netgraph examples.")
    parser.add_argument("paths", nargs="*", type=Path, help="Markdown files; default all of them.")
    parser.add_argument("--list", action="store_true", help="List the blocks and their verdicts.")
    parser.add_argument(
        "--update", action="store_true", help="Rewrite the transcript of failing run blocks."
    )
    args = parser.parse_args(argv)

    paths = [path.resolve() for path in args.paths] or None
    blocks, unmarked = all_blocks(paths)

    if args.list:
        for block in blocks:
            verdict = f"run cwd={block.cwd}" if block.runnable else f"norun ({block.reason})"
            print(f"{block.id}: {verdict}")

    failures = 0
    for entry in unmarked:
        print(
            f"{entry.id}: example invokes netgraph but carries no "
            "'<!-- run: … -->' or '<!-- norun: … -->' marker",
            file=sys.stderr,
        )
        failures += 1

    for block in blocks:
        if args.update and block.runnable:
            if update(block):
                print(f"updated {block.id}")
            continue
        problem = check(block)
        if problem is not None:
            print(f"{block.id}: {problem}", file=sys.stderr)
            failures += 1

    runnable = sum(1 for block in blocks if block.runnable)
    print(f"{len(blocks)} example(s): {runnable} executed, {len(blocks) - runnable} excused")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - exercised through tests/test_docs.py
    raise SystemExit(main())
