"""``--from csv``: a patch list of ``device,port,device,port`` rows, as cables.

**Why CSV rather than NetJSON.** The task this dialect exists for is "I already
have my cabling written down somewhere". NetJSON's ``NetworkGraph`` describes
nodes and links, but a link has no notion of a *port*: its ends are node ids.
Importing it would therefore either drop the interface pair — the one thing that
makes a netgraph ``cable`` a cable rather than a line on a picture — or invent
interface names to hang the link on, which is exactly what this command refuses
to do. It would also cost several hundred lines of shape-guessing across the
NetworkGraph, NetworkCollection and DeviceConfiguration variants.

A four-column CSV carries precisely what a cable needs and nothing it does not.
Every patch-panel spreadsheet, cable-label export and hand-kept text file is one
``awk`` away from it, and the reader below is under a hundred lines. Where an
operator does have NetJSON, ``jq`` turns its links into these four columns in a
single command, which is documented in the README — so this dialect subsumes
that one rather than competing with it.

The grammar::

    # comments and blank lines are ignored
    device,port,device,port[,medium[,label]]
    sw-core,Gi0/1,pc-alice,eno1
    sw-core,Te1/1,sw-edge,Te0/1,fiber,A-014

A header row is detected and skipped. ``medium`` defaults to ``copper`` and is
marked as inferred when it was not stated, exactly as for LLDP. Both devices are
created if they are not already in the draft, so a CSV alone produces a complete,
loadable inventory rather than a set of dangling references.
"""

from __future__ import annotations

import csv
import io
from typing import Final

from netgraph.errors import NetgraphError, echo_value
from netgraph.importer.draft import Draft, DraftCable
from netgraph.importer.names import element_name, interface_name

__all__ = ["CSV_HEADER_WORDS", "MEDIA", "read_csv_links"]

#: ``spec.medium`` values the fifth column may hold (``docs/schema.md`` §7).
MEDIA: Final[frozenset[str]] = frozenset({"copper", "fiber", "wireless"})

#: Column labels that mark the first row as a header rather than as a cable.
#: Both *port* columns have to match, and the match is exact once punctuation is
#: stripped — so ``port_a,peer_port`` is a header and a switch whose ports are
#: genuinely called ``port1`` is not mistaken for one.
CSV_HEADER_WORDS: Final[frozenset[str]] = frozenset(
    {
        "port",
        "ports",
        "porta",
        "portb",
        "peerport",
        "localport",
        "remoteport",
        "if",
        "ifname",
        "interface",
        "interfaces",
    }
)

_MIN_COLUMNS: Final = 4
_MAX_COLUMNS: Final = 6


class CsvError(NetgraphError):
    """Raised when a cabling CSV cannot be read as one.

    Unlike a malformed JSON capture — which is a machine's output and either
    parses or does not — a CSV is usually hand-kept, so the diagnostic names the
    row and what was wrong with it.
    """

    exit_code = 3


def read_csv_links(text: str, *, source: str, draft: Draft) -> None:
    """Fold a ``device,port,device,port`` cabling list into ``draft``.

    Args:
        text: The whole file.
        source: Name of the input, for comments and the run report.
        draft: Accumulator, mutated in place.

    Raises:
        CsvError: A row does not hold between four and six fields, names an
            unknown medium, or holds a name with nothing usable in it. A cabling
            list is short and hand-maintained, so a mistake in it is worth
            stopping for rather than dropping a link and carrying on.
    """
    rows = list(csv.reader(io.StringIO(text)))
    seen = 0
    for number, row in enumerate(rows, start=1):
        fields = [field.strip() for field in row]
        if not any(fields) or fields[0].startswith("#"):
            continue
        if seen == 0 and _is_header(fields):
            continue
        seen += 1
        _read_row(fields, number=number, source=source, draft=draft)

    if seen == 0:
        draft.note(f"{source}: no cable rows in the file")


def _is_header(fields: list[str]) -> bool:
    """Is this first row column labels rather than a cable?

    Both port columns are tested, because those are the ones whose labels are
    predictable: a device column may be headed ``device``, ``a``, ``switch``,
    ``from`` or the site's own vocabulary, but the column holding ``Gi0/1`` is
    called some variation of "port" in every spreadsheet there has ever been.
    """
    if len(fields) < _MIN_COLUMNS:
        return False
    return all(_label(fields[index]) in CSV_HEADER_WORDS for index in (1, 3))


def _label(text: str) -> str:
    """``peer_port`` → ``peerport``; a column label with its punctuation gone.

    Digits are *kept*, which is the whole point: ``port1`` is a port on a switch
    and ``port_a`` is a column heading, and stripping digits would collapse the
    two onto each other.
    """
    return "".join(character for character in text.lower() if character.isalnum())


def _read_row(fields: list[str], *, number: int, source: str, draft: Draft) -> None:
    if not _MIN_COLUMNS <= len(fields) <= _MAX_COLUMNS:
        raise CsvError(
            f"{source}, row {number}: expected 4 to {_MAX_COLUMNS} fields "
            f"(device,port,device,port[,medium[,label]]), got {len(fields)}"
        )

    endpoints = tuple(
        _endpoint(fields[index], fields[index + 1], number=number, source=source)
        for index in (0, 2)
    )
    medium = (fields[4].lower() if len(fields) > 4 and fields[4] else "").strip()
    if medium and medium not in MEDIA:
        raise CsvError(
            f"{source}, row {number}: {echo_value(fields[4])} is not a medium; "
            f"expected one of {', '.join(sorted(MEDIA))}"
        )
    label = fields[5] if len(fields) > 5 and fields[5] else None

    for device_name, port in endpoints:
        device = draft.device(device_name)
        device.observed_in(source)
        device.interface(port)

    draft.add_cable(
        DraftCable(
            endpoints=(endpoints[0], endpoints[1]),
            medium=medium or "copper",
            medium_stated=bool(medium),
            label=label,
            comments=[f"listed in {source}, row {number}"],
            sources=[source],
        )
    )


def _endpoint(device: str, port: str, *, number: int, source: str) -> tuple[str, str]:
    """One ``device,port`` pair, with both halves made into legal names."""
    name, _ = element_name(device)
    interface, _ = interface_name(port)
    if name is None or interface is None:
        missing = "device name" if name is None else "port name"
        raise CsvError(
            f"{source}, row {number}: the {missing} {echo_value(device if name is None else port)} "
            "holds no character a netgraph name may use"
        )
    return (name, interface)
