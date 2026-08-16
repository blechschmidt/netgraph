"""Reading an edited diagram back into typed edits.

A diagram comes home changed, and the changes have to be turned into
:mod:`netgraph.edit` operations without ever *guessing*. Everything here is
built on one rule: **identity comes from the attributes, never from the label
and never from the position**. A cell that still carries ``netgraph:name``
stands for the same element it always did, however far it has been dragged and
whatever it now says on the canvas. That is what makes the four gestures a
draw.io user can perform unambiguous:

=========================  ================================================
On the canvas              In the inventory
=========================  ================================================
A cell moved               A geometry write (§18), and nothing else
A label retyped            ``rename``, with every reference rewritten
A cell deleted             ``delete``, cascading to what cannot survive it
Two cells newly joined     ``connect``, on the first free port at each end
=========================  ================================================

An annotation cell (§21) answers the same four questions and writes them into
its own document rather than into the tree: a dragged note is
``set-annotation spec.geometry.x``, a resized one is ``spec.geometry.width``, a
retyped one is ``spec.text`` — converted back out of the HTML draw.io left
behind, as faithfully as :mod:`netgraph.drawio.markup` can — and one that is
gone is ``delete-annotation``. An area answers with ``spec.geometry`` and
``spec.label``.

**A legend is not reconciled at all, and never will be.** It is *generated*: its
entries come from what the drawing turned out to contain, so a corrected swatch
label has nowhere in the inventory to go and writing one back would be inventing
a hand-written key from a generated one. Its cells carry identity for exactly one
purpose — so that this module can recognise them and do nothing, rather than
reporting three hundred unmapped rectangles or, worse, proposing to delete them.

And one rule about what is *not* done. A cell that is simply missing is only a
deletion when the exported file said it held the whole view
(:attr:`~netgraph.drawio.identity.Scope.COMPLETE`). Export a diagram narrowed by
``--namespace`` and re-import it and nothing is deleted at all: absence proves
nothing about a diagram that was narrowed before it was drawn, and the failure
mode of getting this wrong is deleting an estate.

A file netgraph did not write gets the best-effort treatment: the kind is
inferred from the shape style and the label, and everything that could not be
mapped is reported. Nothing is invented — an unrecognised cell becomes a note,
not a ``computer``.

Nothing here touches the filesystem. The operations are handed to an
:class:`~netgraph.edit.session.EditSession`, and what the session's result
*means* is shown to the user as a :mod:`netgraph.plan` changeset before a single
byte is written.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from netgraph.drawio.build import element_of
from netgraph.drawio.identity import (
    ATTR_HASH,
    ATTR_HEIGHT,
    ATTR_KIND,
    ATTR_LABEL,
    ATTR_LINK,
    ATTR_NAME,
    ATTR_NODE,
    ATTR_PLACED,
    ATTR_ROUTING,
    ATTR_TEXT,
    ATTR_WAYPOINTS,
    ATTR_WIDTH,
    ATTR_X,
    ATTR_Y,
    MODEL_VERSION,
    CellRole,
    Placedness,
    Scope,
    content_hash,
    parse_points,
)
from netgraph.drawio.markup import html_to_markup, markup_html, plain_text
from netgraph.drawio.model import Cell, Diagram, absolute_geometry
from netgraph.drawio.notes import Level, Note
from netgraph.drawio.styles import NOTE_SHAPE
from netgraph.edit.cascade import placed_element, plan_cascade
from netgraph.edit.operations import (
    Connect,
    CreateAnnotation,
    DeleteAnnotation,
    DeleteElement,
    Operation,
    RenameElement,
    SetAnnotation,
    SetGeometry,
    SetLinkGeometry,
)
from netgraph.edit.references import NameIndex
from netgraph.edit.rename import respelled_key
from netgraph.layout.document import inline_entry
from netgraph.layout.geometry import COORDINATE_PLACES, round_coordinate
from netgraph.layout.resolve import resolve_key
from netgraph.layout.seed import DEFAULT_LAYOUT_NAME
from netgraph.loader.inventory import Inventory, namespace_of, short_name
from netgraph.models.annotation import AREA_KIND, NOTE_KIND, Annotation
from netgraph.models.element import KINDS
from netgraph.models.interface import CABLEABLE_TYPES
from netgraph.plan.document import body_of
from netgraph.render.annotations import annotation_views, parse_markup
from netgraph.render.graph import Graph, Node

__all__ = [
    "MOVE_EPSILON",
    "Level",
    "Note",
    "ReconcileOptions",
    "Reconciliation",
    "infer_kind",
    "reconcile",
]

#: How far a cell must move before it counts as moved, in points. Coordinates
#: are stored to :data:`~netgraph.layout.geometry.COORDINATE_PLACES`, so
#: anything at that scale is rounding rather than a decision — and treating it
#: as one would make every round trip through draw.io a diff.
MOVE_EPSILON: Final = 10.0 ** (-COORDINATE_PLACES) / 2

#: The name grammar of §4.1, as far as this module needs it: what a retyped
#: label has to be before it can become a rename.
_ELEMENT_NAME: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|[-_.][A-Za-z0-9])*$")

#: Longest label accepted as a rename. §4.1 bounds a name; a label past this is
#: a caption somebody typed on the canvas, not a new name.
_MAX_NAME = 253

#: Anything that cannot appear in a name, for making one out of a note's text.
_NAME_UNSAFE: Final = re.compile(r"[^a-z0-9]+")

#: How much of a new note's text is kept as its name. Long enough to tell two
#: callouts apart in a file listing, short enough to be a filename.
_NEW_NOTE_NAME_LIMIT: Final = 40

#: Words in a draw.io shape style or label that name an element kind. Read in
#: this order, so ``switch`` beats ``computer`` in a style that mentions both —
#: the more specific kind wins, which is the way round that produces a note
#: instead of a wrong guess when neither is meant.
_KIND_HINTS: Final[tuple[tuple[str, str], ...]] = (
    ("patchpanel", "patchpanel"),
    ("patch panel", "patchpanel"),
    ("patch_panel", "patchpanel"),
    ("patch-panel", "patchpanel"),
    ("firewall", "firewall"),
    ("router", "router"),
    ("switch", "switch"),
    ("hub", "hub"),
    ("server", "server"),
    ("storage", "server"),
    ("pdu", "pdu"),
    ("ups", "pdu"),
    ("laptop", "computer"),
    ("desktop", "computer"),
    ("workstation", "computer"),
    ("pc", "computer"),
    ("computer", "computer"),
    ("adapter", "adapter"),
    ("dongle", "adapter"),
)


@dataclass(frozen=True, slots=True)
class ReconcileOptions:
    """What the caller settles about one import."""

    #: ``metadata.name`` of the layout document new geometry is written into.
    #: Geometry for a key an existing document already holds is written back
    #: into *that* document instead, so two people arranging two sites do not
    #: collide.
    layout: str = DEFAULT_LAYOUT_NAME
    #: The folder that document lives in.
    namespace: str = ""
    #: Where a document that has to be created should land.
    file: str | None = None
    #: Act on cells that vanished. On by default — a deletion is one of the
    #: four gestures the round trip exists to carry — but never applied to a
    #: partial export, whatever this says.
    deletions: bool = True
    #: Act on labels that changed.
    renames: bool = True
    #: Act on cells that moved.
    geometry: bool = True
    #: Act on edges the draw.io user drew.
    connections: bool = True
    #: Act on the annotation cells of §21 at all. Off leaves every note and area
    #: exactly as the inventory has it — and, importantly, still does not report
    #: their cells as unmapped: they are netgraph's own, whether or not this
    #: import is interested in what happened to them. What happens *to* an
    #: annotation is then decided by the same three switches above, because a
    #: dragged note is a move, a retyped one is a label, and a deleted one is a
    #: deletion.
    annotations: bool = True


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Everything one diagram asks for, and everything it could not express."""

    #: The operations, already ordered: renames first (so the geometry that
    #: follows can be keyed by the new address), then deletions, then new
    #: cables, then geometry.
    operations: tuple[Operation, ...] = ()
    notes: tuple[Note, ...] = ()
    #: One count per gesture, for the one-line summary.
    moved: int = 0
    renamed: int = 0
    deleted: int = 0
    connected: int = 0
    #: Operations against a note or an area (§21). Counted apart from the rest
    #: because they change the *picture* and nothing else: a reader deciding
    #: whether an import is worth reviewing carefully wants to know that six of
    #: the seven changes are callouts.
    annotated: int = 0
    #: Cells netgraph could not map onto anything.
    unmapped: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.operations

    @property
    def failed(self) -> bool:
        """Did something stop the import outright?"""
        return any(note.level is Level.ERROR for note in self.notes)

    def summary(self) -> str:
        """``3 moved, 1 renamed, 1 deleted`` — what the diagram asks for."""
        counted = [
            (self.moved, "moved"),
            (self.renamed, "renamed"),
            (self.deleted, "deleted"),
            (self.connected, "newly connected"),
            (self.annotated, "annotation change(s)"),
            (self.unmapped, "unmapped"),
        ]
        said = [f"{count} {label}" for count, label in counted if count]
        return ", ".join(said) if said else "nothing to change"

    def of_level(self, level: Level) -> tuple[Note, ...]:
        return tuple(note for note in self.notes if note.level is level)


def reconcile(
    diagram: Diagram,
    inventory: Inventory,
    graph: Graph,
    options: ReconcileOptions | None = None,
) -> Reconciliation:
    """Everything ``diagram`` asks of ``inventory``, as typed operations.

    Args:
        diagram: The parsed ``.drawio`` model.
        inventory: The tree as it is now — not as it was when the file was
            exported, which is exactly why the content hashes are checked.
        graph: The graph for the diagram's view, built from ``inventory`` and
            **not** filtered: the reconciler decides for itself what a missing
            cell means, and a narrowed graph would make everything missing.
        options: Which gestures to act on, and where geometry is written.

    Returns:
        A :class:`Reconciliation`. Never raises for a diagram it cannot read:
        what it could not map comes back as notes, because the operator is
        going to be shown a changeset either way.
    """
    settings = options or ReconcileOptions()
    state = _State(diagram=diagram, inventory=inventory, graph=graph, options=settings)
    if not diagram.is_netgraph:
        return _foreign(state)
    if diagram.version and diagram.version > MODEL_VERSION:
        return Reconciliation(
            notes=(
                Note(
                    Level.ERROR,
                    "",
                    f"the diagram was written by a newer netgraph (model version "
                    f"{diagram.version}, this one reads {MODEL_VERSION}); upgrade netgraph "
                    "rather than importing it with this one",
                ),
            )
        )
    return _reconcile_authored(state)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _State:
    """One reconciliation in progress."""

    diagram: Diagram
    inventory: Inventory
    graph: Graph
    options: ReconcileOptions
    notes: list[Note] = field(default_factory=list)
    #: Layout key to its new position, in netgraph coordinates.
    moves: dict[str, tuple[float, float]] = field(default_factory=dict)
    renames: list[Operation] = field(default_factory=list)
    deletions: list[Operation] = field(default_factory=list)
    connections: list[Operation] = field(default_factory=list)
    links: list[Operation] = field(default_factory=list)
    #: Everything asked of a note or an area, in the order the cells were read.
    annotations: list[Operation] = field(default_factory=list)
    unmapped: int = 0

    def note(self, level: Level, subject: str, message: str) -> None:
        self.notes.append(Note(level, subject, message))


def _reconcile_authored(state: _State) -> Reconciliation:
    cells = state.diagram.by_id()
    seen_elements: set[str] = set()
    seen_links: set[str] = set()
    seen_annotations: set[tuple[str, str]] = set()

    for cell in state.diagram.of_role(CellRole.NODE):
        _node_cell(state, cell, cells, seen_elements)
    for cell in state.diagram.of_role(CellRole.LINK):
        _link_cell(state, cell, cells, seen_links)
    for cell in state.diagram.of_role(CellRole.NOTE):
        _annotation_cell(state, cell, cells, NOTE_KIND, seen_annotations)
    for cell in state.diagram.of_role(CellRole.AREA):
        _annotation_cell(state, cell, cells, AREA_KIND, seen_annotations)
    # A legend cell and a leader are read and left: see the module docstring on
    # why generated presentation has nothing to say to the inventory.
    for cell in state.diagram.foreign:
        _foreign_cell(state, cell, cells)

    _deletions(state, seen_elements, seen_links)
    _annotation_deletions(state, seen_annotations)
    geometry = _geometry_operations(state)

    operations = (
        *state.renames,
        *state.deletions,
        *state.connections,
        *state.links,
        *geometry,
        *state.annotations,
    )
    return Reconciliation(
        operations=operations,
        notes=tuple(state.notes),
        moved=len(state.moves),
        renamed=len(state.renames),
        deleted=len(state.deletions),
        connected=len(state.connections),
        annotated=len(state.annotations),
        unmapped=state.unmapped,
    )


# --------------------------------------------------------------------------- #
# Vertices
# --------------------------------------------------------------------------- #


def _node_cell(state: _State, cell: Cell, cells: Mapping[str, Cell], seen: set[str]) -> None:
    """One vertex netgraph wrote: a possible move, and a possible rename."""
    key = cell.attribute(ATTR_NODE)
    if not key:
        state.unmapped += 1
        state.note(
            Level.WARNING,
            cell.id,
            "carries a netgraph role but no node key, so netgraph cannot tell what it "
            "stands for; it was left alone",
        )
        return

    address = cell.attribute(ATTR_NAME)
    if address:
        seen.add(address)
        if address not in state.inventory:
            state.note(
                Level.WARNING,
                address,
                "is drawn in the diagram but is not in the inventory any more; the diagram "
                "was exported from an older state of the tree",
            )
            return
        _check_hash(state, cell, address)
        key = _rename(state, cell, address) or key

    _move(state, cell, cells, key)


def _check_hash(state: _State, cell: Cell, address: str) -> None:
    """Say so when the element changed under the diagram, and carry on.

    A warning rather than a refusal: the change is usually somebody else's
    commit landing while the diagram was out for review, and refusing the whole
    import would lose the reviewer's work over an unrelated edit. What it must
    not do is pass unmentioned — the geometry and the rename are still applied,
    and the operator has to know they are being applied to a moved target.
    """
    element = state.inventory.get(address)
    if element is not None:
        _compare_hash(state, cell, address, body_of(element))


def _compare_hash(state: _State, cell: Cell, subject: str, body: Any) -> None:
    """The half of :func:`_check_hash` that does not care what sort of document it is."""
    expected = cell.attribute(ATTR_HASH)
    if not expected:
        return
    if content_hash(body) != expected:
        state.note(
            Level.INFO,
            subject,
            "has changed in the inventory since the diagram was exported; the diagram's "
            "geometry and label are still applied, but re-export before making more edits",
        )


def _rename(state: _State, cell: Cell, address: str) -> str | None:
    """A retyped label, if it is one and if it can legally be a name.

    Returns:
        The element's new address when a rename was issued, so the geometry
        that follows is keyed by the name the element will *have*. ``None``
        when nothing was renamed.
    """
    if not state.options.renames:
        return None
    label = cell.label.strip()
    current = short_name(address)
    if not label or label == current:
        return None
    if not _is_element_name(label):
        state.note(
            Level.WARNING,
            address,
            f"the label was changed to {label!r}, which is not a usable element name "
            "(letters, digits and single '-', '_' or '.' separators — docs/schema.md §4.1); "
            "the element was not renamed",
        )
        return None

    namespace = namespace_of(address)
    proposed = f"{namespace}/{label}" if namespace else label
    if proposed in state.inventory:
        state.note(
            Level.WARNING,
            address,
            f"the label was changed to {label!r}, but {proposed} is already declared; "
            "the element was not renamed",
        )
        return None
    state.renames.append(RenameElement(address=address, new_name=label))
    return proposed


def _is_element_name(label: str) -> bool:
    return len(label) <= _MAX_NAME and bool(_ELEMENT_NAME.match(label))


def _move(state: _State, cell: Cell, cells: Mapping[str, Cell], key: str) -> None:
    """A dragged cell, if it was dragged.

    The comparison is against the position netgraph *stamped into the cell*,
    not against the arrangement in the tree. That is what makes exporting an
    unarranged inventory and importing it straight back a no-op: an invented
    position that comes home unchanged says nothing, and writing it would
    commit an arrangement nobody chose.
    """
    if not state.options.geometry:
        return
    x, y, width, height = absolute_geometry(cell, cells)
    centre = state.diagram.frame.box_to_netgraph(x, y, width, height)
    exported = _exported_position(cell)
    if exported is None:
        state.note(
            Level.WARNING,
            key,
            "netgraph did not stamp a position on this cell, so there is nothing to compare "
            "the current one against; its geometry was left alone",
        )
        return
    if _within(centre, exported):
        return
    if cell.attribute(ATTR_PLACED) == Placedness.AUTO.value:
        state.note(
            Level.INFO,
            key,
            "had no stored position before; moving it in draw.io is what puts one in the inventory",
        )
    state.moves[key] = (round_coordinate(centre[0]), round_coordinate(centre[1]))


def _exported_position(cell: Cell) -> tuple[float, float] | None:
    x, y = cell.attribute(ATTR_X), cell.attribute(ATTR_Y)
    if not x or not y:
        return None
    try:
        return (float(x), float(y))
    except ValueError:
        return None


def _within(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return abs(first[0] - second[0]) <= MOVE_EPSILON and abs(first[1] - second[1]) <= MOVE_EPSILON


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #


def _link_cell(state: _State, cell: Cell, cells: Mapping[str, Cell], seen: set[str]) -> None:
    """One edge netgraph wrote: a possible reroute, or a link left alone."""
    key = cell.attribute(ATTR_LINK)
    if key:
        seen.add(key)
    address = cell.attribute(ATTR_NAME)
    if address and address in state.inventory:
        _check_hash(state, cell, address)
    if key and state.options.geometry:
        _reroute(state, cell, key)


def _reroute(state: _State, cell: Cell, key: str) -> None:
    """A link whose bends were dragged, if they were."""
    frame = state.diagram.frame
    current = tuple(frame.to_netgraph(x, y) for x, y in cell.points)
    exported = parse_points(cell.attribute(ATTR_WAYPOINTS))
    if len(current) == len(exported) and all(
        _within(one, other) for one, other in zip(current, exported, strict=True)
    ):
        return
    routing = cell.attribute(ATTR_ROUTING) or None
    state.links.append(
        SetLinkGeometry(
            view=state.diagram.view,
            link=key,
            waypoints=[{"x": round_coordinate(x), "y": round_coordinate(y)} for x, y in current],
            routing=routing,
            layout=state.options.layout,
            namespace=state.options.namespace,
            file=state.options.file,
        )
    )


def _foreign_cell(state: _State, cell: Cell, cells: Mapping[str, Cell]) -> None:
    """A cell netgraph did not write, inside a file netgraph did.

    Two things a draw.io user can add are acted on, and both are unambiguous:
    an edge between two known nodes — joining two boxes is how anybody says "and
    these are patched together" — and a sticky note, which becomes a ``kind:
    note`` document (:func:`_new_note`). Everything else — an arrow, a legend
    somebody drew by hand, a box they typed a name into — is reported and left
    where it is. Inventing a device from a rectangle would put hardware in the
    inventory that nobody owns.
    """
    if not cell.edge:
        if _new_note(state, cell, cells):
            return
        state.unmapped += 1
        state.note(
            Level.INFO,
            cell.id,
            f"is a cell netgraph did not write ({_describe(cell)}); it was left in the "
            "diagram and has no place in the inventory",
        )
        return
    if not state.options.connections:
        return
    ends = _endpoints(state, cell, cells)
    if ends is None:
        return
    state.connections.append(Connect(a=ends[0], b=ends[1], spec={"medium": "copper"}))


def _endpoints(state: _State, cell: Cell, cells: Mapping[str, Cell]) -> tuple[str, str] | None:
    """``device:interface`` at each end of a newly drawn edge, or ``None``.

    The port is not something draw.io can express, so it is chosen: the first
    interface at each end that terminates nothing. When there is none the edge
    is reported rather than forced onto an occupied port, because a cable
    landing on a port that already has one is the mistake this whole tool
    exists to catch.
    """
    source = _element_at(state, cell.source, cells)
    target = _element_at(state, cell.target, cells)
    if source is None or target is None:
        state.unmapped += 1
        state.note(
            Level.WARNING,
            cell.id,
            "is an edge drawn between something other than two netgraph elements, so there "
            "is no cable it could become",
        )
        return None
    if source == target:
        state.unmapped += 1
        state.note(
            Level.WARNING, cell.id, "is an edge with both ends on one element; no cable was made"
        )
        return None

    ports = [_free_port(state, end) for end in (source, target)]
    if any(port is None for port in ports):
        state.unmapped += 1
        without = [end for end, port in zip((source, target), ports, strict=True) if port is None]
        state.note(
            Level.WARNING,
            cell.id,
            f"is a new edge, but {' and '.join(without)} "
            f"{'has' if len(without) == 1 else 'have'} no free interface to land it on; add "
            "one and re-import, or connect it with 'netgraph edit connect'",
        )
        return None
    return (f"{source}:{ports[0]}", f"{target}:{ports[1]}")


def _element_at(state: _State, cell_id: str, cells: Mapping[str, Cell]) -> str | None:
    """The element a cell stands for, if it stands for one that exists."""
    cell = cells.get(cell_id)
    if cell is None or cell.role is not CellRole.NODE:
        return None
    address = cell.attribute(ATTR_NAME)
    return address if address and address in state.inventory else None


def _free_port(state: _State, address: str) -> str | None:
    """The first cablable interface of ``address`` that nothing terminates on.

    Cablable is the validator's own definition (``E012``): a loopback or a
    bridge is an interface but not a socket, and landing a cable on one would
    produce a document that fails validation the moment it is written. So a
    device whose only free ports are virtual has *no* free port here, and the
    edge is reported instead of forced.
    """
    node = state.graph.nodes.get(address)
    if node is None:
        return None
    taken = {
        port
        for edge in state.graph.edges
        for element, port in (
            (edge.source, edge.source_port),
            (edge.target, edge.target_port),
        )
        if element == address and port
    }
    return next(
        (
            port.name
            for port in node.ports
            if port.type in CABLEABLE_TYPES and port.enabled and port.name not in taken
        ),
        None,
    )


# --------------------------------------------------------------------------- #
# Annotations (§21)
# --------------------------------------------------------------------------- #


def _annotation_cell(
    state: _State, cell: Cell, cells: Mapping[str, Cell], kind: str, seen: set[tuple[str, str]]
) -> None:
    """One note or area netgraph wrote: what the canvas now says about it.

    Reading the cell is unconditional — a note that is *present* has not been
    deleted, whatever this import is willing to act on — and everything after
    that is gated, so ``--no-geometry`` on a diagram full of callouts still
    reports nothing and changes nothing.
    """
    fqn = cell.attribute(ATTR_NAME)
    if not fqn:
        state.unmapped += 1
        state.note(
            Level.WARNING,
            cell.id,
            f"is a {kind} cell with no name on it, so netgraph cannot tell which document it "
            "stands for; it was left alone",
        )
        return
    seen.add((kind, fqn))
    if not state.options.annotations:
        return

    document = state.inventory.annotations_of(kind).get(fqn)
    if document is None:
        state.note(
            Level.WARNING,
            fqn,
            f"is drawn as a {kind} but is not in the inventory any more; the diagram was "
            "exported from an older state of the tree",
        )
        return
    _compare_hash(state, cell, fqn, body_of(document))
    _annotation_geometry(state, cell, cells, kind, fqn, document)
    if kind == NOTE_KIND:
        _note_text(state, cell, fqn)
    else:
        _area_label(state, cell, fqn)


def _annotation_geometry(
    state: _State,
    cell: Cell,
    cells: Mapping[str, Cell],
    kind: str,
    fqn: str,
    document: Annotation,
) -> None:
    """A dragged or resized annotation, as writes to ``spec.geometry``.

    Compared against the box netgraph stamped into the cell, exactly as a node
    is: an annotation whose box netgraph *computed* — a zone following its
    members, a note beside its anchor — must come home unchanged without pinning
    anything, or every round trip would freeze the layout it was drawn with.
    """
    if not state.options.geometry:
        return
    x, y, width, height = absolute_geometry(cell, cells)
    centre = state.diagram.frame.box_to_netgraph(x, y, width, height)
    exported = _exported_position(cell)
    if exported is None:
        state.note(
            Level.WARNING,
            fqn,
            "netgraph did not stamp a position on this cell, so there is nothing to compare "
            "the current one against; its geometry was left alone",
        )
        return
    size = _exported_size(cell)
    updates: dict[str, float] = {}
    if not _within(centre, exported):
        updates["x"] = round_coordinate(centre[0])
        updates["y"] = round_coordinate(centre[1])
        if cell.attribute(ATTR_PLACED) == Placedness.AUTO.value:
            state.note(
                Level.INFO,
                fqn,
                f"followed the diagram before; moving it in draw.io is what pins the {kind} at "
                "a position of its own",
            )
    if size is not None and not _within((width, height), size):
        updates["width"] = round_coordinate(width)
        updates["height"] = round_coordinate(height)
    if updates:
        _write_geometry(state, kind, fqn, document, updates, extent=(width, height))


def _write_geometry(
    state: _State,
    kind: str,
    fqn: str,
    document: Annotation,
    updates: dict[str, float],
    *,
    extent: tuple[float, float],
) -> None:
    """The changed numbers, as the fewest writes that are each *individually* valid.

    One operation per field is what a reviewer wants to read — ``spec.geometry.x``
    says what happened — but every write is checked against §21 the moment it
    lands, and ``x`` without ``y`` is a position that places nothing. So an
    annotation that has never been placed gets its whole ``geometry`` block in
    one write, which is exactly what
    :class:`~netgraph.edit.operations.SetAnnotation` documents as the first drag
    of an unplaced note; one that is already placed gets a field at a time.

    An **area** is a further case: a zone with a position and no size is ignored
    by the renderer, which draws it round its members again — so dragging one
    would silently do nothing. Its extent therefore goes with it, and a dragged
    zone stays where it was put.
    """
    geometry = getattr(document.spec, "geometry", None)
    if kind == AREA_KIND and (geometry is None or not geometry.sized):
        updates.setdefault("width", round_coordinate(extent[0]))
        updates.setdefault("height", round_coordinate(extent[1]))
    if geometry is not None and geometry.placed:
        for field_name, value in updates.items():
            _set(state, kind, fqn, f"spec.geometry.{field_name}", value)
        return
    merged: dict[str, float] = {} if geometry is None else geometry.model_dump(exclude_none=True)
    merged.update(updates)
    _set(state, kind, fqn, "spec.geometry", merged)


def _exported_size(cell: Cell) -> tuple[float, float] | None:
    width, height = cell.attribute(ATTR_WIDTH), cell.attribute(ATTR_HEIGHT)
    if not width or not height:
        return None
    try:
        return (float(width), float(height))
    except ValueError:
        return None


def _note_text(state: _State, cell: Cell, fqn: str) -> None:
    """A retyped note, converted back out of draw.io's HTML.

    "Was it retyped?" is asked by rendering the *stamped source* again and
    comparing that with the label, rather than by converting the label back and
    comparing texts: the forward direction is exact and the backward one is best
    effort, so asking the question the other way round would make an untouched
    note look edited whenever the conversion lost a nuance.
    """
    if not state.options.renames:
        return
    stamped = cell.attribute(ATTR_TEXT)
    if not stamped:
        state.note(
            Level.WARNING,
            fqn,
            "netgraph did not stamp this note's source on the cell, so an edit to its text "
            "cannot be told from a re-rendering of it; the text was left alone",
        )
        return
    if markup_html(parse_markup(stamped)) == cell.label:
        return
    text = html_to_markup(cell.label)
    if not text.strip():
        state.note(
            Level.WARNING,
            fqn,
            "the note's text was emptied on the canvas, and a note with no text is not a "
            "note; delete the cell to remove it, or type something into it",
        )
        return
    _set(state, NOTE_KIND, fqn, "spec.text", text)


def _area_label(state: _State, cell: Cell, fqn: str) -> None:
    """A retyped zone caption. Plain text, so anything else about it is dropped."""
    if not state.options.renames:
        return
    label = plain_text(cell.label)
    if label == cell.attribute(ATTR_LABEL):
        return
    if label:
        _set(state, AREA_KIND, fqn, "spec.label", label)
        return
    _set(state, AREA_KIND, fqn, "spec.label", unset=True)


def _set(
    state: _State, kind: str, fqn: str, path: str, value: Any = None, *, unset: bool = False
) -> None:
    state.annotations.append(
        SetAnnotation(
            kind=kind,
            name=short_name(fqn),
            namespace=namespace_of(fqn),
            path=path,
            value=value,
            unset=unset,
        )
    )


def _new_note(state: _State, cell: Cell, cells: Mapping[str, Cell]) -> bool:
    """A sticky note somebody added to the diagram, as a new ``kind: note``.

    The one thing a draw.io user can *add* to a vertex that netgraph is willing
    to write into the tree, and it is safe for the reason the same reasoning
    refuses everything else: reaching for the note shape is an unambiguous
    statement of intent, and the worst case is a callout nobody wanted rather
    than hardware nobody owns. Any other new rectangle is still reported and
    left where it is.

    Returns:
        Was the cell claimed? ``False`` leaves it to the ordinary foreign-cell
        path, which reports it.
    """
    if not (state.options.annotations and _is_note_shape(cell.style)):
        return False
    text = html_to_markup(cell.label).strip()
    if not text:
        return False
    x, y, width, height = absolute_geometry(cell, cells)
    centre = state.diagram.frame.box_to_netgraph(x, y, width, height)
    name = _fresh_note_name(state, text)
    state.annotations.append(
        CreateAnnotation(
            kind=NOTE_KIND,
            name=name,
            spec={
                "text": text,
                "geometry": {
                    "x": round_coordinate(centre[0]),
                    "y": round_coordinate(centre[1]),
                    "width": round_coordinate(width),
                    "height": round_coordinate(height),
                },
            },
        )
    )
    state.note(
        Level.INFO,
        cell.id,
        f"is a note somebody added to the diagram; it becomes 'kind: note' {name}. Nothing "
        "else a draw.io user draws becomes an inventory document",
    )
    return True


def _is_note_shape(style: str) -> bool:
    """Is this draw.io's sticky-note shape, under any of the names it has?

    ``shape=note`` is the built-in one; a stencil from a shape library spells
    the same picture ``mxgraph.something.note``. Both mean a person picked a
    sticky note out of a palette.
    """
    shape = _shape_of(style)
    return shape == NOTE_SHAPE or shape.endswith(f".{NOTE_SHAPE}")


def _fresh_note_name(state: _State, text: str) -> str:
    """A name for a note nobody named: its first few words, made into a slug.

    Derived from the text so that the document is findable by what it says —
    ``notes/check-the-uplink.yaml`` — and disambiguated against both the
    inventory and the notes this same import is already creating, because two
    new callouts starting with the same three words is not a rare accident.
    """
    words = _NAME_UNSAFE.sub("-", text.lower()).strip("-")
    candidate = words[:_NEW_NOTE_NAME_LIMIT].strip("-") or "note"
    if not _ELEMENT_NAME.match(candidate):
        candidate = "note"
    taken = set(state.inventory.annotations_of(NOTE_KIND)) | {
        operation.address
        for operation in state.annotations
        if isinstance(operation, CreateAnnotation)
    }
    chosen, counter = candidate, 2
    while chosen in taken:
        chosen = f"{candidate}-{counter}"
        counter += 1
    return chosen


def _annotation_deletions(state: _State, seen: set[tuple[str, str]]) -> None:
    """Every note and area this view draws that the diagram no longer holds.

    Guarded three times over, because the failure mode is deleting somebody's
    documentation: only for a diagram that says it held the whole view, only for
    one written by an exporter that draws annotations at all — a file from
    before §21 holds no note cells because none were ever written — and only for
    the annotations that this view would actually have drawn, since an area
    whose members were all filtered out was never in the file to begin with.
    """
    if not (state.options.deletions and state.options.annotations):
        return
    if state.diagram.scope is not Scope.COMPLETE or not state.diagram.annotated:
        return
    for kind, fqn in _drawn_annotations(state.graph):
        if (kind, fqn) not in seen:
            state.annotations.append(
                DeleteAnnotation(kind=kind, name=short_name(fqn), namespace=namespace_of(fqn))
            )


def _drawn_annotations(graph: Graph) -> tuple[tuple[str, str], ...]:
    """The notes and areas this view draws, as ``(kind, fqn)``, in draw order.

    Resolved through :func:`~netgraph.render.annotations.annotation_views` — the
    same function the exporter draws from — so that "was it in the file?" is
    answered by the code that decided, rather than by a second rule that would
    drift from it. Legends are left out: they are never reconciled.
    """
    views = annotation_views(graph)
    return (
        *((AREA_KIND, area.fqn) for area in views.areas),
        *((NOTE_KIND, note.fqn) for note in views.notes),
    )


# --------------------------------------------------------------------------- #
# Deletions
# --------------------------------------------------------------------------- #


def _deletions(state: _State, elements: set[str], links: set[str]) -> None:
    """Everything the view draws that the diagram no longer holds."""
    if not state.options.deletions:
        return
    if state.diagram.scope is not Scope.COMPLETE:
        missing = [
            fqn for fqn in _drawn_elements(state.graph, state.inventory) if fqn not in elements
        ]
        if missing:
            state.note(
                Level.INFO,
                "",
                f"{len(missing)} element(s) of this view are not in the diagram, but the "
                "diagram was exported from a filtered view, so nothing was deleted; export "
                "without a filter if deletions are meant to come back",
            )
        return

    for fqn in _drawn_elements(state.graph, state.inventory):
        if fqn not in elements:
            state.deletions.append(DeleteElement(address=fqn, cascade=True))
    for edge in state.graph.edges:
        fqn = element_of(edge.id)
        if edge.id in links or fqn not in state.inventory:
            continue
        # A cable whose device went with it is already gone; deleting it again
        # would fail on an address that no longer resolves.
        if any(
            isinstance(op, DeleteElement) and _terminates(state, fqn, op) for op in state.deletions
        ):
            continue
        state.deletions.append(DeleteElement(address=fqn, cascade=True))


def _drawn_elements(graph: Graph, inventory: Inventory) -> tuple[str, ...]:
    """Every declared element this view draws as a node, in canonical order.

    A derived node — a subnet, a rack — is not an element and cannot be
    deleted; deleting the *devices* is what makes a subnet stop being drawn.
    """
    return tuple(sorted(node.fqn for node in graph.nodes.values() if _is_declared(node, inventory)))


def _is_declared(node: Node, inventory: Inventory) -> bool:
    return node.fqn in inventory


def _terminates(state: _State, fqn: str, deletion: Operation) -> bool:
    """Would ``deletion`` take the link ``fqn`` with it?"""
    assert isinstance(deletion, DeleteElement)
    for edge in state.graph.edges:
        if element_of(edge.id) != fqn:
            continue
        if deletion.address in (edge.source, edge.target):
            return True
    return False


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _geometry_operations(state: _State) -> tuple[Operation, ...]:
    """The moves, grouped into one :class:`SetGeometry` per layout document.

    A ``set-geometry`` replaces a whole section, so each document is rewritten
    with the entries it already holds plus the ones that moved. Which document
    a key goes to is decided by which one already holds it — so two people
    arranging two sites, each in their own layout file, do not end up with one
    file holding both and a duplicate-key conflict in the other.

    "The entries it already holds" is read from the tree as it was *before* this
    import, which still places everything the import deletes — so the deleted
    keys are taken out here. Otherwise the geometry write, which runs last,
    would put back the coordinates the delete had just cleaned up, and a diagram
    with one node dragged onto it would come back with a stale ``W138`` per
    node removed.
    """
    if not state.moves:
        return ()
    view = state.diagram.view
    default = (state.options.layout, state.options.namespace)
    pending: dict[tuple[str, str], dict[str, Any]] = {}

    for key, position in sorted(state.moves.items()):
        owner = _owner_of(state.inventory, key, view) or default
        written = _written_key(key, owner[1], state.inventory)
        pending.setdefault(owner, {})[written] = inline_entry(
            {"position": {"x": position[0], "y": position[1]}}
        )

    doomed = _doomed_keys(state)
    renamed, after = _renamed_keys(state)
    operations: list[Operation] = []
    for (layout, namespace), entries in sorted(pending.items()):
        merged = {
            key: entry
            for key, entry in _existing_nodes(state.inventory, layout, namespace, view)
            if placed_element(key, inventory=state.inventory, namespace=namespace) not in doomed
        }
        merged.update(entries)
        placed = _respell(state, merged, namespace, renamed, after)
        operations.append(
            SetGeometry(
                view=view,
                nodes=dict(sorted(placed.items())),
                layout=layout,
                namespace=namespace,
                file=state.options.file if (layout, namespace) == default else None,
            )
        )
    return tuple(operations)


def _doomed_keys(state: _State) -> frozenset[str]:
    """Every element this import removes, cascade included.

    :func:`~netgraph.edit.cascade.plan_cascade` rather than the deletion list,
    because a deleted device takes its cables and they are placed too — and it
    is the same function the delete itself will run, so the geometry that is
    written back and the geometry that is cleaned up cannot disagree.
    """
    addresses = [
        operation.address for operation in state.deletions if isinstance(operation, DeleteElement)
    ]
    if not addresses:
        return frozenset()
    return frozenset(plan_cascade(state.inventory, addresses).elements)


def _renamed_keys(state: _State) -> tuple[Mapping[str, str], NameIndex]:
    """The renames this import makes, and the name table they leave behind.

    The geometry write runs *after* the renames and replaces a whole section,
    so it has to speak the new names: the entries it carries across were read
    from the tree as it was before the import, and writing them back unchanged
    would put the old key straight back into the file the rename had just
    fixed. The name table is returned with it because deciding how to *spell*
    the new key means resolving against the tree as it will be.
    """
    renamed: dict[str, str] = {}
    for operation in state.renames:
        if not isinstance(operation, RenameElement):  # pragma: no cover - only renames are here
            continue
        namespace = namespace_of(operation.address)
        renamed[operation.address] = (
            f"{namespace}/{operation.new_name}" if namespace else operation.new_name
        )
    index = NameIndex(state.inventory.elements if renamed else ())
    for old, new in renamed.items():
        index = index.replaced(old, new)
    return renamed, index


def _respell(
    state: _State,
    entries: Mapping[str, Any],
    namespace: str,
    renamed: Mapping[str, str],
    after: NameIndex,
) -> dict[str, Any]:
    """``entries`` with the keys of renamed elements written the new way.

    The renamed keys are written last, so that a rename onto a spelling the
    document already carries — stale geometry left by an earlier element of that
    name — leaves the live element's coordinates rather than the dead one's.
    That is the same way round :func:`netgraph.edit.apply._rekey` decides it.
    """
    if not renamed:
        return dict(entries)
    placed: dict[str, Any] = {}
    moved: dict[str, Any] = {}
    for key, entry in entries.items():
        new = renamed.get(placed_element(key, inventory=state.inventory, namespace=namespace))
        if new is None:
            placed[key] = entry
        else:
            moved[respelled_key(key, new=new, namespace=namespace, index=after)] = entry
    placed.update(moved)
    return placed


def _owner_of(inventory: Inventory, key: str, view: str) -> tuple[str, str] | None:
    """The layout document already placing ``key`` in ``view``, if there is one."""
    for fqn, layout in inventory.layouts.items():
        geometry = layout.view(view)
        if geometry is None:
            continue
        namespace = namespace_of(fqn)
        for written in geometry.nodes:
            if resolve_key(written, inventory=inventory, namespace=namespace) == key:
                return (short_name(fqn), namespace)
    return None


def _existing_nodes(
    inventory: Inventory, layout: str, namespace: str, view: str
) -> Iterator[tuple[str, Any]]:
    """The node entries a document already holds, as they are written.

    Carried across verbatim rather than re-resolved: a document that writes
    ``sw-core`` relative to its own folder must go on writing ``sw-core``, or
    every re-import would requalify half the file.
    """
    fqn = f"{namespace}/{layout}" if namespace else layout
    document = inventory.layouts.get(fqn)
    if document is None:
        return
    geometry = document.view(view)
    if geometry is None:
        return
    for key, entry in geometry.nodes.items():
        yield (key, inline_entry(entry.model_dump(exclude_none=True)))


def _written_key(key: str, namespace: str, inventory: Inventory) -> str:
    """How a document in ``namespace`` should spell ``key``.

    Relative when the document sits in the element's own folder, which is what
    a hand-written layout does and what keeps the file readable; absolute
    otherwise, because a relative key that resolves somewhere else is a bug
    waiting for the next person to move a directory.
    """
    if not namespace or ":" in key:
        return key
    if namespace_of(key) == namespace:
        candidate = short_name(key)
        if resolve_key(candidate, inventory=inventory, namespace=namespace) == key:
            return candidate
    return key


# --------------------------------------------------------------------------- #
# A diagram netgraph did not write
# --------------------------------------------------------------------------- #


def _foreign(state: _State) -> Reconciliation:
    """Best effort on a diagram somebody drew by hand.

    Nothing is reconciled, because there is nothing to reconcile against: a
    hand-drawn diagram has no identity attributes, so every cell in it is
    either a new element or noise, and netgraph cannot tell which. What it
    *can* do is say what it saw and what it made of each cell, which is the
    difference between "netgraph could not read my diagram" and "netgraph read
    it and here is what it could not place".
    """
    notes: list[Note] = [
        Note(
            Level.WARNING,
            "",
            "this file was not written by 'netgraph export drawio', so it carries no "
            "identity attributes; nothing can be reconciled against the inventory and "
            "nothing will be changed. What netgraph made of each cell follows",
        )
    ]
    unmapped = 0
    for cell in state.diagram.cells:
        if cell.edge:
            notes.append(
                Note(Level.INFO, cell.id, "an edge; it would become a cable between its two ends")
            )
            continue
        kind = infer_kind(cell)
        if kind is None:
            unmapped += 1
            notes.append(
                Note(
                    Level.WARNING,
                    cell.id,
                    f"no element kind could be inferred from its style or its label "
                    f"({_describe(cell)}); netgraph does not guess",
                )
            )
            continue
        notes.append(
            Note(Level.INFO, cell.id, f"looks like a {kind} called {cell.label.strip()!r}")
        )
    notes.append(
        Note(
            Level.INFO,
            "",
            "to make a diagram netgraph can reconcile, start from 'netgraph export drawio' "
            "and edit that; see docs/drawio.md",
        )
    )
    return Reconciliation(notes=tuple(notes), unmapped=unmapped)


def infer_kind(cell: Cell) -> str | None:
    """The element kind a foreign cell looks like, or ``None``.

    Read from the shape style first — a draw.io user who reached for the Cisco
    stencil said what they meant — and from the label second. ``None`` is a
    perfectly good answer and the common one: a rectangle is a rectangle.
    """
    declared = cell.attribute(ATTR_KIND)
    if declared in KINDS:
        return declared
    haystack = f"{cell.style} {cell.label}".lower()
    for hint, kind in _KIND_HINTS:
        if hint in haystack:
            return kind
    return None


def _describe(cell: Cell) -> str:
    """A cell in a few words, for a note about it."""
    label = cell.label.strip()
    shape = _shape_of(cell.style)
    if label and shape:
        return f"a {shape} labelled {label!r}"
    if label:
        return f"labelled {label!r}"
    return f"a {shape}" if shape else "unlabelled"


def _shape_of(style: str) -> str:
    """The ``shape=`` of a style string, or its first bare token."""
    for declaration in style.split(";"):
        key, separator, value = declaration.partition("=")
        if separator and key == "shape":
            return value
    first = style.split(";")[0]
    return first if first and "=" not in first else ""
