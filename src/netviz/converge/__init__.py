"""``netviz converge`` — turning drift into an ordered, reviewable remediation.

``netviz drift`` says how the live network differs from the declared
inventory. ``netviz export config`` says what a device would run if it agreed.
Until this package there was nothing joining them: an operator read a list of
differences and typed the fix, which is the step where the inventory and the
network started disagreeing in the first place.

``netviz converge plan`` takes the same capture ``drift`` takes and produces,
per device, the ordered set of changes that would close every difference --
each one carrying the drift finding that asked for it, a risk classification, its
prerequisites, the commands that perform it and the commands that undo it::

    report = converge(inventory, ["sw1.lldp.json", "pc.addr.json"])
    print(render_converge(report, "text"))

**netviz does not apply any of this.** There is no transport here and there is
not meant to be: no SSH client, no credential store, no device session. The
command writes a plan and a set of scripts, and a person or a purpose-built tool
runs them. That keeps the security surface of this whole project at "reads files,
writes files", which is a sentence an auditor can check. The plan type is
designed so a transport *could* consume it later --
:class:`~netviz.converge.model.ConvergeChange` carries an id, prerequisites and
an inverse precisely so something could apply one change, verify it and roll it
back -- but that transport would be a separate program with a separate threat
model. See ``docs/commands/converge.md``.

The modules
-----------

=================================  ==========================================
:mod:`~netviz.converge.model`    the plan types, and what a refusal is
:mod:`~netviz.converge.intent`   the dialect-free vocabulary and the ordering
:mod:`~netviz.converge.derive`   the join: a drift finding becomes intents
:mod:`~netviz.converge.risk`     the management path, and what is disruptive
:mod:`~netviz.converge.commands` netviz's imperative grammar, and inverses
:mod:`~netviz.converge.files`    the declarative dialects, via the emitters
:mod:`~netviz.converge.batch`    maintenance windows, via the impact engine
:mod:`~netviz.converge.build`    the pipeline that produces a plan
:mod:`~netviz.converge.report`   text, JSON and markdown
:mod:`~netviz.converge.script`   the per-device ``.txt`` an operator reads
=================================  ==========================================
"""

from __future__ import annotations

from netviz.converge.batch import batches_for, blast_radius
from netviz.converge.build import (
    CONVERGE_DIALECTS,
    ConvergeInputs,
    build_plan,
    converge,
)
from netviz.converge.commands import describe, render, revert
from netviz.converge.derive import derive
from netviz.converge.files import DECLARATIVE, FileChange, file_changes
from netviz.converge.intent import RANKS, Intent, IntentKind, order_intents
from netviz.converge.model import (
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
from netviz.converge.report import (
    REPORT_FORMATS,
    render_converge,
    to_json,
    to_markdown,
    to_text,
)
from netviz.converge.risk import ManagementPath, classify, management_path
from netviz.converge.script import script_files, script_for, write_scripts

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
