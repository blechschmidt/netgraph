"""Building a :class:`~netgraph.converge.model.ConvergePlan` from a capture.

Seven steps, in one place, so every consumer -- the CLI, a test, a future
transport -- gets the same plan from the same inputs:

1. read the capture exactly as ``netgraph drift`` does, through the same
   importer, so a dialect drift understands is a dialect converge understands;
2. build the *observed* inventory, the declaration with every observation folded
   in (:func:`netgraph.plan.live.adopt`), which is what the declarative dialects
   diff against;
3. turn every drift finding into intents (:mod:`netgraph.converge.derive`);
4. classify each one against the device's management path
   (:mod:`netgraph.converge.risk`);
5. render commands -- neutral lines, or per-dialect files
   (:mod:`netgraph.converge.commands`, :mod:`netgraph.converge.files`);
6. group into maintenance batches with the impact engine
   (:mod:`netgraph.converge.batch`);
7. refuse the whole plan if it holds a disruptive change and nobody allowed one.

Step 7 is last on purpose. The refusal names *every* disruptive change, so an
operator deciding whether to pass ``--allow-disruptive`` is deciding about the
whole set rather than discovering the second one after re-running -- the same
discipline :func:`netgraph.export.config.generate` applies to a dialect that
cannot express a device. It is also *total*: the exception carries the plan
nowhere, so a refused run writes nothing at all.

Nothing here opens a socket. The capture is a file somebody collected and the
output is a file somebody reads; see ``docs/commands/converge.md`` for why that
boundary is where it is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Final

from netgraph.converge.batch import batches_for
from netgraph.converge.commands import describe, render, revert
from netgraph.converge.derive import derive
from netgraph.converge.files import DECLARATIVE, FileChange, file_changes
from netgraph.converge.intent import Intent, IntentKind, prerequisites_of
from netgraph.converge.model import (
    Action,
    Batch,
    ConvergeChange,
    ConvergePlan,
    DeviceConverge,
    DisruptiveChangeError,
    Provenance,
    Risk,
)
from netgraph.converge.risk import ManagementPath, classify, management_path
from netgraph.drift.compare import EVERYTHING, CompareSpec, compare
from netgraph.drift.coverage import coverage_of
from netgraph.drift.model import DriftReport
from netgraph.export.context import ExportContext, ExportOptions
from netgraph.export.manifest import Recorder
from netgraph.importer import build_draft, read_inputs
from netgraph.importer.draft import Draft
from netgraph.loader.inventory import Inventory
from netgraph.models import Device
from netgraph.plan.live import adopt
from netgraph.render.graph import Graph, Layer, build_graph

__all__ = ["CONVERGE_DIALECTS", "ConvergeInputs", "build_plan", "converge"]

#: Every dialect ``--dialect`` accepts, in the order ``--help`` lists them.
#: ``interfaces`` is first and is the default: it is the only one that covers
#: every device kind an inventory can hold, and a plan is read before it is run.
CONVERGE_DIALECTS: Final[tuple[str, ...]] = ("interfaces", *DECLARATIVE)

#: The layer the plan is scoped by, matching ``netgraph export config``: the
#: selection is a set of devices, and L1 is the layer whose nodes are elements.
_LAYERS: Final[tuple[Layer, ...]] = (Layer.L1,)

#: Rank for the per-dialect file writes. Above every intent rank in
#: :data:`netgraph.converge.intent.RANKS` except ``MANUAL``, because writing the
#: file is the realisation of everything the intents above it described.
_FILE_RANK: Final = 200

#: What each file action is called in a summary.
_FILE_VERBS: Final[dict[Action, str]] = {
    Action.DELETE: "remove",
    Action.CREATE: "install",
    Action.UPDATE: "update",
}


class ConvergeInputs:
    """The capture, read once and kept, so the plan and the report agree.

    :func:`netgraph.drift.check_drift` throws the draft away and returns only the
    report. Converge needs both -- the report to know what disagrees, the draft
    to build the observed inventory -- so the two steps are done here rather
    than by calling ``check_drift`` and re-reading the inputs, which would give
    two chances to disagree about what was captured.
    """

    __slots__ = ("draft", "inventory", "observed", "report", "spec")

    def __init__(
        self,
        inventory: Inventory,
        specs: Sequence[str],
        *,
        dialect: str = "auto",
        host: str | None = None,
        spec: CompareSpec = EVERYTHING,
        stdin: Any = None,
    ) -> None:
        entries = read_inputs(list(specs), host=host, stdin=stdin)
        draft = build_draft(entries, dialect=dialect, exclude=spec.ignore_interfaces)
        coverage = coverage_of(draft)
        self.inventory = inventory
        self.draft: Draft = draft
        self.spec = spec
        self.report: DriftReport = compare(
            inventory,
            draft,
            coverage=coverage,
            spec=spec,
            inputs=tuple(entry.name for entry in entries),
        )
        self.observed: Inventory = adopt(inventory, draft, coverage=coverage, spec=spec).inventory


def converge(
    inventory: Inventory,
    specs: Sequence[str],
    *,
    dialect: str = "interfaces",
    capture_dialect: str = "auto",
    host: str | None = None,
    spec: CompareSpec = EVERYTHING,
    allow_disruptive: bool = False,
    stdin: Any = None,
) -> ConvergePlan:
    """Read ``specs`` and produce the plan that would converge the network.

    Args:
        inventory: The declared tree, already loaded.
        specs: Capture files, ``NAME=PATH`` pairs, or ``-`` for standard input.
        dialect: One of :data:`CONVERGE_DIALECTS`.
        capture_dialect: How to read the capture, as ``drift --from`` takes it.
        host: The device every input without its own ``NAME=`` was captured on.
        spec: Element and interface filters, as drift takes them.
        allow_disruptive: Emit a plan even when it touches a management path.
        stdin: Stream to read ``-`` from.

    Raises:
        DisruptiveChangeError: The plan would cut management reachability and
            ``allow_disruptive`` is false. Nothing has been written.
        UnsupportedConfigError: A declarative dialect cannot express a device.
        ImportSourceError: An input is missing, unreadable, or not its dialect.
    """
    inputs = ConvergeInputs(
        inventory, specs, dialect=capture_dialect, host=host, spec=spec, stdin=stdin
    )
    return build_plan(inputs, dialect=dialect, allow_disruptive=allow_disruptive)


def build_plan(
    inputs: ConvergeInputs,
    *,
    dialect: str = "interfaces",
    allow_disruptive: bool = False,
) -> ConvergePlan:
    """The plan for an already-read capture. See :func:`converge`.

    Raises:
        ValueError: ``dialect`` is not one of :data:`CONVERGE_DIALECTS`. The CLI
            validates with ``click.Choice`` first, so reaching this is a
            programming error.
        DisruptiveChangeError: As :func:`converge`.
    """
    if dialect not in CONVERGE_DIALECTS:
        raise ValueError(f"not a converge dialect: {dialect!r}")

    inventory = inputs.inventory
    paths = _management_paths(inventory, build_graph(inventory, layer=Layer.L1))
    intents = derive(inventory, inputs.draft, inputs.report)
    verdicts = {intent.id: classify(intent, paths.get(intent.element)) for intent in intents}

    by_element: dict[str, list[Intent]] = {}
    for intent in intents:
        by_element.setdefault(intent.element, []).append(intent)

    files: Mapping[str, tuple[FileChange, ...]] = {}
    file_notes: tuple[str, ...] = ()
    if dialect in DECLARATIVE:
        covered = {
            element for element in by_element if isinstance(inventory.elements.get(element), Device)
        }
        files, file_notes = file_changes(
            dialect, _context(inventory), _context(inputs.observed), covered
        )

    devices: list[DeviceConverge] = []
    for element in sorted({*by_element, *files}):
        element_intents = by_element.get(element, [])
        changes = [
            _change(intent, element_intents, verdicts[intent.id], dialect)
            for intent in element_intents
        ]
        changes.extend(
            _file_entries(element, files.get(element, ()), element_intents, verdicts, dialect)
        )
        if not changes:  # pragma: no cover - an element is listed because it has one
            continue
        devices.append(
            DeviceConverge(
                element=element,
                kind=_kind_of(inventory, element, element_intents),
                changes=tuple(sorted(changes, key=lambda change: change.order)),
            )
        )

    plan = _with_batches(
        ConvergePlan(
            root=inventory.root,
            inputs=inputs.report.inputs,
            capture_dialects=inputs.report.dialects,
            dialect=dialect,
            devices=tuple(devices),
            allow_disruptive=allow_disruptive,
            notes=(*file_notes, *_notes(dialect, devices)),
        ),
        inventory,
    )

    if plan.disruptive and not allow_disruptive:
        raise DisruptiveChangeError(plan.disruptive)
    return plan


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


def _context(inventory: Inventory) -> ExportContext:
    """An export context the config emitters can be run over.

    The same one ``netgraph export netplan`` builds, minus the CLI: one L1 graph,
    default options, a throw-away recorder. The recorder's skips are not
    reported here -- a device a dialect declines is a device with no file, which
    the plan already shows by having nothing for it.
    """
    return ExportContext(
        inventory=inventory,
        graphs={layer: build_graph(inventory, layer=layer) for layer in _LAYERS},
        options=ExportOptions(),
        recorder=Recorder(),
    )


def _management_paths(inventory: Inventory, graph: Graph) -> dict[str, ManagementPath]:
    """The management path of every device in the graph."""
    paths: dict[str, ManagementPath] = {}
    for node in graph.element_nodes:
        element = inventory.elements.get(node.fqn)
        if isinstance(element, Device):
            paths[node.fqn] = management_path(node, element)
    return paths


def _change(
    intent: Intent,
    siblings: Sequence[Intent],
    verdict: tuple[Risk, str],
    dialect: str,
) -> ConvergeChange:
    """One intent as a plan entry, rendered for ``dialect``."""
    risk, reason = verdict
    declarative = dialect in DECLARATIVE
    inert = declarative or intent.kind is IntentKind.MANUAL
    note = intent.note
    if declarative and intent.kind is not IntentKind.MANUAL:
        note = (
            f"realised by the {dialect} file(s) below: a declarative dialect has no command "
            "for one field"
        )
    return ConvergeChange(
        id=intent.id,
        element=intent.element,
        action=intent.action,
        object=intent.object,
        interface=intent.interface,
        target=intent.target or intent.interface,
        summary=describe(intent),
        value=intent.value,
        previous=intent.previous,
        rank=intent.rank,
        risk=risk,
        risk_reason=reason,
        provenance=intent.provenance,
        prerequisites=prerequisites_of(intent, siblings),
        commands=() if inert else render(intent),
        rollback=() if inert else revert(intent),
        note=note,
    )


def _file_entries(
    element: str,
    entries: Sequence[FileChange],
    intents: Sequence[Intent],
    verdicts: Mapping[str, tuple[Risk, str]],
    dialect: str,
) -> list[ConvergeChange]:
    """The per-dialect file writes for one device, as plan entries.

    Each file carries the provenance of *every* actionable intent on the device,
    because that is the truth about a declarative dialect: the one file is what
    closes all of them, and attributing it to a single finding would be a
    fiction a reviewer could act on.
    """
    if not entries:
        return []
    provenance = _merged_provenance(intents, element)
    risk, reason = _worst_risk(intents, verdicts)
    changes: list[ConvergeChange] = []
    for index, entry in enumerate(entries):
        action = (
            Action.DELETE if entry.removed else Action.CREATE if entry.created else Action.UPDATE
        )
        changes.append(
            ConvergeChange(
                id=f"{element}#file{entry.path}",
                element=element,
                action=action,
                object="file",
                target=entry.path,
                summary=f"{_FILE_VERBS[action]} {entry.path} and reload {dialect}",
                rank=_FILE_RANK + index,
                risk=risk,
                risk_reason=reason,
                provenance=provenance,
                commands=entry.commands(),
                rollback=entry.rollback(),
            )
        )
    return changes


def _merged_provenance(intents: Sequence[Intent], element: str) -> tuple[Provenance, ...]:
    """Every finding the file closes, deduplicated, in plan order.

    A device can have a file change and no actionable intent: the capture may
    have reported a field the dialect renders and drift does not compare. The
    fallback provenance says exactly that rather than leaving the change
    unattributed, because an unattributed change is one nobody can review.
    """
    seen: list[Provenance] = []
    for intent in intents:
        if intent.kind is IntentKind.MANUAL:
            continue
        for entry in intent.provenance:
            if entry not in seen:
                seen.append(entry)
    if seen:
        return tuple(seen)
    return (
        Provenance(
            element=element,
            message=(
                "the generated configuration for this device differs from what the capture "
                "shows it running, in a field no drift finding named on its own"
            ),
        ),
    )


def _worst_risk(
    intents: Sequence[Intent],
    verdicts: Mapping[str, tuple[Risk, str]],
) -> tuple[Risk, str]:
    """A file that realises a disruptive intent is itself disruptive.

    Writing a netplan that no longer holds the management address and running
    ``netplan apply`` is exactly as final as ``ip addr del``, so the file
    inherits the worst risk of everything it realises rather than being judged
    as "a file write". The first reason in plan order is quoted; the rest are on
    the changes themselves, which the plan prints above the file.
    """
    for intent in intents:
        if intent.kind is IntentKind.MANUAL:
            continue
        risk, reason = verdicts.get(intent.id, (Risk.SAFE, ""))
        if risk is Risk.DISRUPTIVE:
            return (Risk.DISRUPTIVE, f"applying this file {reason}")
    return (Risk.SAFE, "")


def _kind_of(inventory: Inventory, element: str, intents: Sequence[Intent]) -> str | None:
    resolved = inventory.elements.get(element)
    if resolved is not None:
        return resolved.kind
    for intent in intents:
        for entry in intent.provenance:
            if entry.kind:
                return entry.kind
    return None


def _notes(dialect: str, devices: Sequence[DeviceConverge]) -> tuple[str, ...]:
    """Anything the reader has to know that no single change carries."""
    notes: list[str] = []
    if dialect in DECLARATIVE:
        without = sorted(
            device.element
            for device in devices
            if device.actionable and not any(change.object == "file" for change in device.changes)
        )
        if without:
            notes.append(
                f"the {dialect!r} dialect writes nothing for "
                f"{', '.join(without)}: it does not describe a device of that shape. The "
                "changes are listed so the plan is complete; use --dialect interfaces to see "
                "them as commands"
            )
    return tuple(notes)


def _with_batches(plan: ConvergePlan, inventory: Inventory) -> ConvergePlan:
    """Attach maintenance batches, and point each device at the one it is in.

    Only elements the impact engine can place get a batch: a cable in the wrong
    port and a device the inventory does not declare are not things a window is
    scheduled around, and giving them one would make the schedule claim
    something it cannot measure.
    """
    placed = [
        device.element
        for device in plan.devices
        if isinstance(inventory.elements.get(device.element), Device) and device.actionable
    ]
    batches: tuple[Batch, ...] = batches_for(inventory, placed) if placed else ()
    index_of = {element: batch.index for batch in batches for element in batch.elements}
    devices = tuple(replace(device, batch=index_of.get(device.element)) for device in plan.devices)
    return replace(plan, devices=devices, batches=batches)
