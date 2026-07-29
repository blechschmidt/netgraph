"""``netgraph import``: a first inventory, from output a real network already prints.

Nothing else in netgraph helps somebody who *has* a network. Every other command
assumes the YAML tree exists, and typing the first one out by hand is the largest
barrier the project has: a forty-port switch is forty interfaces nobody wants to
transcribe, and the transcription is wrong by the time it is finished.

This package turns machine-readable output an operator has already collected into
a ``devices/`` and ``cables/`` tree with the layout ``netgraph init`` writes.
Three dialects are read, each from a file or from standard input:

``lldp``
    ``lldpctl -f json`` / ``lldpcli -f json show neighbors``. A neighbour record
    names both ends of a link and the port at each end, which is exactly a
    ``cable``; :mod:`~netgraph.importer.lldp`.
``iproute``
    ``ip -j link show`` and ``ip -j addr show`` for one host: interfaces, MACs,
    MTUs, admin state, addresses, and — through ``linkinfo`` — bridges, bonds and
    VLAN sub-interfaces; :mod:`~netgraph.importer.iproute`.
``csv``
    ``device,port,device,port`` cabling rows, for the patch list that already
    exists in a spreadsheet; :mod:`~netgraph.importer.csvlinks` explains why this
    rather than NetJSON.

**No network access, ever.** Nothing here opens a socket, reads a credential or
runs a command on a device. The operator runs the collection command — the README
gives the exact line for each dialect — and hands netgraph what it printed. That
keeps the command usable from a laptop with no route to the network it is
documenting, and keeps netgraph out of the business of holding switch passwords.

**No invented values.** A field no capture covers is absent from the output, and
anything netgraph concluded rather than read carries a comment saying so. Where a
device kind cannot be determined the document says ``kind: computer`` and says
why, rather than promoting the box to a router on a hunch.

The pipeline is four stages, each testable on its own: read inputs
(:mod:`~netgraph.importer.run`) → parse into a neutral draft
(:mod:`~netgraph.importer.draft`) → render as commented YAML
(:mod:`~netgraph.importer.emit`) → write. The command in :mod:`netgraph.cli`
then loads the result and runs the ordinary validator over it, because a
partly-observed network has findings that are expected rather than wrong, and
the operator should see them at once.
"""

from __future__ import annotations

from netgraph.importer.csvlinks import CsvError, read_csv_links
from netgraph.importer.draft import (
    Draft,
    DraftCable,
    DraftDevice,
    DraftInterface,
    DraftVlan,
    Endpoint,
)
from netgraph.importer.emit import (
    CABLES_FILE,
    DEVICES_DIR,
    render_cables,
    render_device,
    render_draft,
)
from netgraph.importer.iproute import read_iproute
from netgraph.importer.lldp import read_lldp
from netgraph.importer.names import element_name, interface_name
from netgraph.importer.run import (
    DIALECTS,
    STDIN_TOKEN,
    ImportInput,
    ImportSourceError,
    build_draft,
    build_files,
    read_inputs,
    write_files,
)

__all__ = [
    "CABLES_FILE",
    "DEVICES_DIR",
    "DIALECTS",
    "STDIN_TOKEN",
    "CsvError",
    "Draft",
    "DraftCable",
    "DraftDevice",
    "DraftInterface",
    "DraftVlan",
    "Endpoint",
    "ImportInput",
    "ImportSourceError",
    "build_draft",
    "build_files",
    "element_name",
    "interface_name",
    "read_csv_links",
    "read_inputs",
    "read_iproute",
    "read_lldp",
    "render_cables",
    "render_device",
    "render_draft",
    "write_files",
]
