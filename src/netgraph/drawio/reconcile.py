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
    ATTR_KIND,
    ATTR_LINK,
    ATTR_NAME,
    ATTR_NODE,
    ATTR_PLACED,
    ATTR_ROUTING,
    ATTR_WAYPOINTS,
    ATTR_X,
    ATTR_Y,
    MODEL_VERSION,
    CellRole,
    Placedness,
    Scope,
    content_hash,
    parse_points,
)
from netgraph.drawio.model import Cell, Diagram, absolute_geometry
from netgraph.drawio.notes import Level, Note
from netgraph.edit.operations import (
    Connect,
    DeleteElement,
    Operation,
    RenameElement,
    SetGeometry,
    SetLinkGeometry,
)
from netgraph.layout.document import inline_entry
from netgraph.layout.geometry import COORDINATE_PLACES, round_coordinate
from netgraph.layout.resolve import resolve_key
from netgraph.layout.seed import DEFAULT_LAYOUT_NAME
from netgraph.loader.inventory import Inventory, namespace_of, short_name
from netgraph.models.element import KINDS
from netgraph.models.interface import CABLEABLE_TYPES
from netgraph.plan.document import body_of
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

#: Words in a draw.io shape style or label that name an element kind. Read in
#: this order, so ``switch`` beats ``computer`` in a style that mentions both —
#: the more specific kind wins, which is the way round that produces a note
#: instead of a wrong guess when neither is meant.
_KIND_HINTS: Final[tuple[tuple[str, str], ...]] = (
    ("patchpanel", "patchpanel"),
    ("patch panel", "patchpanel"),
    ("patch_panel", "patchpanel"),
    ("patch-panel", "patchpanel"),
    ("router", "router"),
    ("firewall", "router"),
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
    unmapped: int = 0

    def note(self, level: Level, subject: str, message: str) -> None:
        self.notes.append(Note(level, subject, message))


def _reconcile_authored(state: _State) -> Reconciliation:
    cells = state.diagram.by_id()
    seen_elements: set[str] = set()
    seen_links: set[str] = set()

    for cell in state.diagram.of_role(CellRole.NODE):
        _node_cell(state, cell, cells, seen_elements)
    for cell in state.diagram.of_role(CellRole.LINK):
        _link_cell(state, cell, cells, seen_links)
    for cell in state.diagram.foreign:
        _foreign_cell(state, cell, cells)

    _deletions(state, seen_elements, seen_links)
    geometry = _geometry_operations(state)

    operations = (
        *state.renames,
        *state.deletions,
        *state.connections,
        *state.links,
        *geometry,
    )
    return Reconciliation(
        operations=operations,
        notes=tuple(state.notes),
        moved=len(state.moves),
        renamed=len(state.renames),
        deleted=len(state.deletions),
        connected=len(state.connections),
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
    expected = cell.attribute(ATTR_HASH)
    element = state.inventory.get(address)
    if not expected or element is None:
        return
    actual = content_hash(body_of(element))
    if actual != expected:
        state.note(
            Level.INFO,
            address,
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

    An edge drawn between two known nodes is the one thing a draw.io user can
    add that netgraph can act on with confidence, and it is the interesting
    one: joining two boxes is how anybody says "and these are patched
    together". Everything else — a note, an arrow, a legend, a box somebody
    typed a name into — is reported and left where it is. Inventing a device
    from a rectangle would put hardware in the inventory that nobody owns.
    """
    if not cell.edge:
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

    operations: list[Operation] = []
    for (layout, namespace), entries in sorted(pending.items()):
        merged = dict(_existing_nodes(state.inventory, layout, namespace, view))
        merged.update(entries)
        operations.append(
            SetGeometry(
                view=view,
                nodes=dict(sorted(merged.items())),
                layout=layout,
                namespace=namespace,
                file=state.options.file if (layout, namespace) == default else None,
            )
        )
    return tuple(operations)


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
