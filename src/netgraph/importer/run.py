"""Reading the inputs, choosing a dialect for each, and writing the tree.

This is the part of ``netgraph import`` that touches the outside world; every
other module in the package is a pure function of its argument. It does four
things and nothing else:

**Collects inputs.** An input is a file, or ``-`` for standard input. Nothing is
fetched, no host is contacted and no credential is read: the operator runs the
collection command themselves and hands netgraph what it printed. That is a
design constraint, not an omission — a tool that logs into switches is a
different tool with a different threat model.

**Decides which device each input describes.** ``ip -j link show`` and
``lldpctl -f json`` both describe one host and neither says which, so the name
comes from ``NAME=PATH``, from ``--host``, or from the file name — in that
order, most explicit first. A name that came from a file name is recorded as
such in the generated document, because it is the one field the capture did not
supply.

**Chooses a dialect.** ``--from`` names one; ``auto`` sniffs, which is reliable
here because the three shapes are disjoint: an LLDP capture is a JSON object
with an ``lldp`` key, an iproute capture is a JSON array of link records, and
anything that is not JSON at all is the CSV. Sniffing is what makes ``netgraph
import collected/*`` work on a directory holding all three.

**Writes, or refuses to.** An existing file is never overwritten without
``--force``, and every clash is reported at once rather than one per run: an
import is re-run repeatedly while the capture set grows, and finding out about
the third collision on the third attempt would be tiresome.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from netgraph.errors import NetgraphError, clip_text, echo_value
from netgraph.importer.csvlinks import read_csv_links
from netgraph.importer.draft import Draft
from netgraph.importer.emit import render_draft
from netgraph.importer.iproute import read_iproute
from netgraph.importer.lldp import read_lldp
from netgraph.importer.names import element_name

__all__ = [
    "DIALECTS",
    "STDIN_TOKEN",
    "ImportInput",
    "ImportSourceError",
    "build_draft",
    "dialect_of",
    "read_inputs",
    "write_files",
]

#: What ``--from`` accepts. ``auto`` is first because it is the default.
DIALECTS: Final[tuple[str, ...]] = ("auto", "lldp", "iproute", "csv")

#: The conventional "read standard input" argument.
STDIN_TOKEN: Final = "-"

#: Name reported for standard input in comments and diagnostics.
STDIN_NAME: Final = "<stdin>"

#: Ceiling on one input. A capture from one host is kilobytes; anything past
#: this is a mistake — a tarball, a log, a ``/dev/zero`` — and reading it into
#: memory to find that out helps nobody.
MAX_INPUT_BYTES: Final = 32 * 1024 * 1024


class ImportSourceError(NetgraphError):
    """Raised when an input cannot be read, or cannot be read as what it claims.

    Shares its exit status with :class:`~netgraph.errors.LoaderError`: from the
    user's side both mean "netgraph could not parse the thing you pointed it at".
    """

    exit_code = 3


@dataclass(slots=True)
class ImportInput:
    """One capture file, its content, and the device it came from."""

    #: How the input is named in comments and diagnostics.
    name: str
    #: The whole file.
    text: str
    #: Element name of the device the capture was taken on, when one is known.
    host: str | None = None
    #: Did :attr:`host` come from the file name rather than from the operator?
    host_from_filename: bool = False
    _payload: Any = field(default=None, repr=False)
    _parsed: bool = field(default=False, repr=False)

    def payload(self) -> Any:
        """The input parsed as JSON, parsed at most once.

        Raises:
            ImportSourceError: The text is not JSON.
        """
        if not self._parsed:
            try:
                self._payload = json.loads(self.text)
            except json.JSONDecodeError as exc:
                raise ImportSourceError(
                    f"{self.name}: not valid JSON: {clip_text(str(exc))}"
                ) from exc
            self._parsed = True
        return self._payload


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def read_inputs(
    specs: Sequence[str], *, host: str | None = None, stdin: Any = None
) -> list[ImportInput]:
    """Read every input named on the command line.

    Args:
        specs: Paths, ``NAME=PATH`` pairs, or ``-`` for standard input.
        host: ``--host``: the device every input without its own ``NAME=`` came
            from. Applies to all of them, which is what makes
            ``--host pc1 link.json addr.json`` mean the obvious thing.
        stdin: Stream to read ``-`` from; defaults to :data:`sys.stdin`.

    Raises:
        ImportSourceError: An input is missing, unreadable, not text, or
            oversized; or ``-`` was given more than once.
    """
    if not specs:
        raise ImportSourceError("no input given; name a capture file, or '-' to read stdin")
    if host is not None and element_name(host)[0] != host:
        raise ImportSourceError(
            f"--host {echo_value(host)} is not a usable element name; it may hold letters, "
            "digits and single '-', '_' or '.' separators (docs/schema.md §4.1)"
        )

    inputs: list[ImportInput] = []
    seen_stdin = False
    for spec in specs:
        tagged, target = _split_spec(spec)
        if target == STDIN_TOKEN:
            if seen_stdin:
                raise ImportSourceError("'-' may be given only once; stdin can be read but once")
            seen_stdin = True
            inputs.append(
                ImportInput(name=STDIN_NAME, text=_read_stdin(stdin), host=tagged or host)
            )
            continue
        inputs.append(_read_file(Path(target), tagged=tagged, host=host))
    return inputs


def _split_spec(spec: str) -> tuple[str | None, str]:
    """``NAME=PATH`` into its halves; a bare path keeps ``None``.

    Only a leading segment that is already a legal element name counts as a tag,
    so a file whose *path* holds an ``=`` is still read as a path.
    """
    name, separator, path = spec.partition("=")
    if separator and element_name(name)[0] == name and path:
        return name, path
    return None, spec


def _read_stdin(stream: Any) -> str:
    source = stream if stream is not None else sys.stdin
    try:
        text = source.read()
    except OSError as exc:  # pragma: no cover - depends on the invoking shell
        raise ImportSourceError(f"cannot read stdin: {exc.strerror or exc}") from exc
    if not isinstance(text, str):  # pragma: no cover - a byte stream on stdin
        text = text.decode("utf-8", errors="replace")
    if not text.strip():
        raise ImportSourceError("stdin was empty; nothing to import")
    return text


def _read_file(path: Path, *, tagged: str | None, host: str | None) -> ImportInput:
    try:
        if path.is_dir():
            raise ImportSourceError(
                f"{path} is a directory; name the capture files inside it "
                f"(for example '{path}/*.json')"
            )
        size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise ImportSourceError(
                f"{path} is {size} bytes, past the {MAX_INPUT_BYTES}-byte ceiling on one "
                "capture; this is a whole-host command output, not an archive"
            )
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ImportSourceError(f"{path}: no such file") from exc
    except UnicodeDecodeError as exc:
        raise ImportSourceError(f"{path}: not UTF-8 text ({exc.reason})") from exc
    except OSError as exc:
        raise ImportSourceError(f"cannot read {path}: {exc.strerror or exc}") from exc

    if not text.strip():
        raise ImportSourceError(f"{path} is empty; nothing to import")

    inferred = tagged is None and host is None
    return ImportInput(
        name=path.name,
        text=text,
        host=tagged or host or _host_from_path(path),
        host_from_filename=inferred,
    )


def _host_from_path(path: Path) -> str | None:
    """``sw-core-01.lldp.json`` → ``sw-core-01``.

    Everything from the first dot is treated as a suffix chain, so the common
    collection convention — one file per host, named after it, with the tool and
    the format appended — needs no flag at all.
    """
    return element_name(path.name.split(".")[0])[0]


# --------------------------------------------------------------------------- #
# Dialects
# --------------------------------------------------------------------------- #


def build_draft(
    inputs: Sequence[ImportInput], *, dialect: str = "auto", exclude: Sequence[str] = ()
) -> Draft:
    """Read every input into one draft inventory.

    Args:
        inputs: From :func:`read_inputs`, in command-line order. Order decides
            only which of two conflicting observations is kept; a complete
            capture set gives the same tree in any order.
        dialect: One of :data:`DIALECTS`. ``auto`` sniffs each input separately,
            so one run may mix all three.
        exclude: ``fnmatch`` patterns applied to interface names of ``iproute``
            captures.

    Raises:
        ImportSourceError: An input cannot be read as the dialect it was given
            as, or as any dialect at all.
    """
    draft = Draft()
    for entry in inputs:
        chosen = dialect_of(entry, dialect)
        draft.dialects[entry.name] = chosen
        _feed(entry, chosen, draft=draft, exclude=exclude)
    draft.prune()
    draft.assign_cable_names()
    return draft


def dialect_of(entry: ImportInput, requested: str = "auto") -> str:
    """The dialect to read ``entry`` as, sniffing it when ``auto`` was asked for.

    Raises:
        ImportSourceError: The input is JSON but no capture netgraph reads.
    """
    if requested != "auto":
        return requested
    if entry.text.lstrip()[:1] not in "[{":
        return "csv"

    payload = entry.payload()
    if isinstance(payload, list):
        return "iproute"
    if isinstance(payload, dict):
        if "lldp" in payload or "interface" in payload:
            return "lldp"
        if any(isinstance(payload.get(key), list) for key in ("addr_info", "links", "interfaces")):
            return "iproute"
    raise ImportSourceError(
        f"{entry.name}: this is JSON, but not an lldpctl capture (an object with an 'lldp' key) "
        "nor an 'ip -j' capture (an array of link records); name the dialect with --from"
    )


def _feed(entry: ImportInput, dialect: str, *, draft: Draft, exclude: Sequence[str]) -> None:
    """Hand one input to the reader for ``dialect``."""
    if dialect == "csv":
        read_csv_links(entry.text, source=entry.name, draft=draft)
        return

    host = _require_host(entry, dialect)
    if dialect == "lldp":
        read_lldp(entry.payload(), source=entry.name, host=host, draft=draft)
    else:
        read_iproute(entry.payload(), source=entry.name, host=host, draft=draft, exclude=exclude)

    if entry.host_from_filename:
        draft.device(host).note(
            f"the device name came from the file name {entry.name!r}; the capture itself does "
            "not say which host it was taken on — pass --host to state it"
        )


def _require_host(entry: ImportInput, dialect: str) -> str:
    if entry.host is not None:
        return entry.host
    tool = "lldpctl" if dialect == "lldp" else "ip"
    raise ImportSourceError(
        f"{entry.name}: a {tool!r} capture describes one host and does not name it; "
        "pass --host NAME, write the input as NAME=PATH, or name the file after the host"
    )


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def build_files(draft: Draft, *, schema: bool = True) -> dict[str, str]:
    """The tree ``draft`` becomes: relative POSIX path to file content."""
    return render_draft(draft, schema=schema)


def write_files(files: dict[str, str], target: Path, *, force: bool = False) -> list[Path]:
    """Write ``files`` under ``target``, refusing to overwrite without ``force``.

    Args:
        files: Relative POSIX path to content, from :func:`build_files`.
        target: Inventory root. Created, with its parents, when absent.
        force: Overwrite files that are already there.

    Returns:
        The files written, in the order of ``files``.

    Raises:
        ImportSourceError: ``target`` is not a directory, a file would be
            clobbered without ``force``, or a write failed.
    """
    if target.exists() and not target.is_dir():
        raise ImportSourceError(f"{target} exists and is not a directory")
    if not force:
        _refuse_clashes(files, target)

    written: list[Path] = []
    for relative, content in files.items():
        path = target.joinpath(*PurePosixPath(relative).parts)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ImportSourceError(f"cannot write {path}: {exc.strerror or exc}") from exc
        written.append(path)
    return written


#: How many clashing paths a refusal names before summarising the rest.
MAX_LISTED_CLASHES: Final = 8


def _refuse_clashes(files: dict[str, str], target: Path) -> None:
    clashes = [
        relative for relative in files if target.joinpath(*PurePosixPath(relative).parts).exists()
    ]
    if not clashes:
        return
    listed = ", ".join(clashes[:MAX_LISTED_CLASHES])
    if len(clashes) > MAX_LISTED_CLASHES:
        listed += f", and {len(clashes) - MAX_LISTED_CLASHES} more"
    raise ImportSourceError(
        f"refusing to overwrite {len(clashes)} existing file(s) in {target}: {listed}; "
        "pass --force to replace them, --dry-run to see what would be written, or "
        "-o to name an empty directory"
    )
