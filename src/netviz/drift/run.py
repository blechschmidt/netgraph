"""Turning command-line arguments into a :class:`~netviz.drift.model.DriftReport`.

Four steps, in one function, so the command in :mod:`netviz.cli` stays about
options and exit codes:

1. read the inputs (:func:`netviz.importer.read_inputs`) — files, or ``-`` for
   standard input, exactly as ``netviz import`` reads them;
2. parse them into a draft (:func:`netviz.importer.build_draft`) with the
   importer's own dialect readers;
3. work out what those dialects could see (:func:`netviz.drift.coverage_of`);
4. compare the draft with the declared tree (:func:`netviz.drift.compare`).

Nothing here opens a socket or reads a credential, for the same reason ``import``
does not: the operator runs the collection command and hands netviz what it
printed, so the check works from a laptop with no route to the network it is
checking, and netviz never holds a switch password.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from netviz.drift.compare import EVERYTHING, CompareSpec, compare
from netviz.drift.coverage import coverage_of
from netviz.drift.model import DriftReport
from netviz.importer import build_draft, read_inputs
from netviz.loader.inventory import Inventory

__all__ = ["check_drift"]


def check_drift(
    inventory: Inventory,
    specs: Sequence[str],
    *,
    dialect: str = "auto",
    host: str | None = None,
    spec: CompareSpec = EVERYTHING,
    stdin: Any = None,
) -> DriftReport:
    """Compare ``inventory`` with the live-network output named by ``specs``.

    Args:
        inventory: The declared tree, already loaded.
        specs: Capture files, ``NAME=PATH`` pairs, or ``-`` for standard input.
        dialect: One of :data:`netviz.importer.DIALECTS`.
        host: The device every input without its own ``NAME=`` was captured on.
        spec: Element and interface filters.
        stdin: Stream to read ``-`` from; defaults to :data:`sys.stdin`.

    Raises:
        ImportSourceError: An input is missing, unreadable, or not the dialect
            it was given as.
        CsvError: A cabling row is malformed.
    """
    entries = read_inputs(list(specs), host=host, stdin=stdin)
    draft = build_draft(entries, dialect=dialect, exclude=spec.ignore_interfaces)
    return compare(
        inventory,
        draft,
        coverage=coverage_of(draft),
        spec=spec,
        inputs=tuple(entry.name for entry in entries),
    )
