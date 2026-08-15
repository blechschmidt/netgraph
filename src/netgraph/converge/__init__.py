"""``netgraph converge`` — turning drift into an ordered, reviewable remediation.

``netgraph drift`` says how the live network differs from the declared
inventory. ``netgraph export config`` says what a device would run if it agreed.
Until this package there was nothing joining them: an operator read a list of
differences and typed the fix, which is the step where the inventory and the
network started disagreeing in the first place.

``netgraph converge plan`` takes the same capture ``drift`` takes and produces,
per device, the ordered set of changes that would close every difference --
each one carrying the drift finding that asked for it, a risk classification, its
prerequisites, the commands that perform it and the commands that undo it::

    report = converge(inventory, ["sw1.lldp.json", "pc.addr.json"])
    print(render_converge(report, "text"))

**netgraph does not apply any of this.** There is no transport here and there is
not meant to be: no SSH client, no credential store, no device session. The
command writes a plan and a set of scripts, and a person or a purpose-built tool
runs them. That keeps the security surface of this whole project at "reads files,
writes files", which is a sentence an auditor can check. The plan type is
designed so a transport *could* consume it later --
:class:`~netgraph.converge.model.ConvergeChange` carries an id, prerequisites and
an inverse precisely so something could apply one change, verify it and roll it
back -- but that transport would be a separate program with a separate threat
model. See ``docs/commands/converge.md``.

The modules
-----------

=================================  ==========================================
:mod:`~netgraph.converge.model`    the plan types, and what a refusal is
:mod:`~netgraph.converge.intent`   the dialect-free vocabulary and the ordering
:mod:`~netgraph.converge.derive`   the join: a drift finding becomes intents
:mod:`~netgraph.converge.risk`     the management path, and what is disruptive
:mod:`~netgraph.converge.commands` netgraph's imperative grammar, and inverses
:mod:`~netgraph.converge.files`    the declarative dialects, via the emitters
:mod:`~netgraph.converge.batch`    maintenance windows, via the impact engine
:mod:`~netgraph.converge.build`    the pipeline that produces a plan
:mod:`~netgraph.converge.report`   text, JSON and markdown
:mod:`~netgraph.converge.script`   the per-device ``.txt`` an operator reads
=================================  ==========================================
"""

from __future__ import annotations

from netgraph.converge.batch import batches_for, blast_radius
from netgraph.converge.build import (
    CONVERGE_DIALECTS,
    ConvergeInputs,
    build_plan,
    converge,
)
from netgraph.converge.commands import describe, render, revert
from netgraph.converge.derive import derive
from netgraph.converge.files import DECLARATIVE, FileChange, file_changes
from netgraph.converge.intent import RANKS, Intent, IntentKind, order_intents
from netgraph.converge.model import (
    Action,
    Batch,
    Command,
    ConvergeChange,
    ConvergeError,
    ConvergePlan,
    DeviceConverge,
    DisruptiveChangeError,
    Provenance,
    Risk,
)
from netgraph.converge.report import (
    REPORT_FORMATS,
    render_converge,
    to_json,
    to_markdown,
    to_text,
)
from netgraph.converge.risk import ManagementPath, classify, management_path
from netgraph.converge.script import script_files, script_for, write_scripts

__all__ = [
    "CONVERGE_DIALECTS",
    "DECLARATIVE",
    "RANKS",
    "REPORT_FORMATS",
    "Action",
    "Batch",
    "Command",
    "ConvergeChange",
    "ConvergeError",
    "ConvergeInputs",
    "ConvergePlan",
    "DeviceConverge",
    "DisruptiveChangeError",
    "FileChange",
    "Intent",
    "IntentKind",
    "ManagementPath",
    "Provenance",
    "Risk",
    "batches_for",
    "blast_radius",
    "build_plan",
    "classify",
    "converge",
    "derive",
    "describe",
    "file_changes",
    "management_path",
    "order_intents",
    "render",
    "render_converge",
    "revert",
    "script_files",
    "script_for",
    "to_json",
    "to_markdown",
    "to_text",
    "write_scripts",
]
