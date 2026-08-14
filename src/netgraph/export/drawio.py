"""``netgraph export drawio`` — the inventory as an mxGraph diagram.

Every other emitter in this package produces something a *machine* consumes.
This one produces something a *person* opens, in a tool they already have, and
— unlike the SVG a render produces — edits and hands back. That difference is
the whole reason it exists: netgraph's argument is that a network diagram and a
network description should be one artefact, and that argument only lands if the
diagram survives a round trip through the editor everybody already uses.

So the file is deliberately more than a picture:

* Every cell carries the identity attributes of
  :mod:`netgraph.drawio.identity`, which is what makes ``netgraph import
  drawio`` able to tell a moved switch from a new one.
* Every position comes from the stored arrangement (§18), so the file opens
  *already arranged* rather than as a heap draw.io lays out afresh.
* Every icon is inlined as a data URI, so the file is one file — it can be
  mailed, and it will draw the same on a machine that has never seen netgraph.

What it does not hold is the model. A ``.drawio`` file records a device's name,
kind and place; not its interfaces, its addresses, its VLANs or its routing.
That is the lossiness this format is honest about, and it is why the import side
reconciles *gestures* — move, rename, delete, connect — rather than replacing
documents wholesale.
"""

from __future__ import annotations

from netgraph.drawio.build import BuildOptions, build_diagram
from netgraph.drawio.identity import CellRole, Scope
from netgraph.drawio.mxfile import write_mxfile
from netgraph.drawio.notes import Note, Notes
from netgraph.export.context import ExportContext
from netgraph.export.manifest import Reason, Recorder
from netgraph.render.graph import Layer

__all__ = ["emit", "layers_for_options"]


def layers_for_options(view: str) -> tuple[Layer, ...]:
    """The one layer a ``drawio`` export builds.

    Unlike the other emitters, this one draws a *view* and the reader chooses
    which: a cabling diagram and a routing diagram are different pictures of
    one inventory, and a stakeholder is usually being asked about one of them.
    """
    return (Layer(view),)


def emit(context: ExportContext) -> str:
    """One view of the inventory as a ``.drawio`` document."""
    options = context.options
    layer = Layer(options.view)
    graph = context.at(layer)
    notes = Notes()
    diagram = build_diagram(
        graph,
        context.inventory,
        BuildOptions(
            view=layer.value,
            icons=options.icons,
            scope=Scope.COMPLETE if options.complete else Scope.PARTIAL,
            groups=options.frames,
        ),
        notes=notes,
    )
    _record(context.recorder, notes.sealed())
    context.recorder.considered += len(graph.nodes) + len(graph.edges)
    context.recorder.emitted += len(diagram.of_role(CellRole.NODE)) + len(
        diagram.of_role(CellRole.LINK)
    )
    return write_mxfile(diagram, compress=options.compress)


def _record(recorder: Recorder, notes: tuple[Note, ...]) -> None:
    """Fold the builder's notes into the ordinary export manifest.

    The translation lives here rather than in :mod:`netgraph.drawio` because
    the dependency has to run this way round: the export package knows about
    the wire format, and the wire format must not know about the export
    package — otherwise importing either one imports both, which is a cycle.
    A note with no reason token is one that only ever gets printed; there are
    none on this path today, and dropping one silently is still wrong, so it
    lands under the most honest reason there is.
    """
    for note in notes:
        recorder.skip(note.subject, Reason(note.reason or Reason.NOT_REPRESENTABLE), note.message)
