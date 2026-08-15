"""Every command the web editor can perform, and the keys that reach it.

This table is the *only* place a binding is written down. Three consumers read
it and none of them keeps a second copy:

``/api/bindings``
    The page fetches it at boot. ``keys.js`` matches keystrokes against it,
    lists it in the command palette, and renders the shortcut reference that
    ``?`` opens. A command with no entry here has no key and no palette row.
``tools/gen_docs.py``
    The ``keybindings`` region of ``docs/commands/web.md`` is this table as
    Markdown, so the documented shortcut and the working one cannot drift.
``tests/test_web.py``
    Asserts that every :attr:`Binding.id` is implemented by a handler in
    ``keys.js`` and that every handler is declared here — the one direction the
    two files above cannot check for themselves.

How a chord is spelled
----------------------

Modifiers first, in the order ``Ctrl``, ``Alt``, ``Shift``, then the key, joined
with ``-``::

    Ctrl-K   Ctrl-Shift-Z   Alt-3   ArrowRight   F2   ?   Escape

``Ctrl`` means "the platform's command modifier": it matches ``Meta`` as well, so
a Mac user presses ⌘-K and this table does not have to say so twice. A key that
is itself a ``-`` is spelled ``Minus``, and ``+`` is ``Plus``, so the separator
stays unambiguous. Everything else is exactly what ``KeyboardEvent.key`` reports,
lower-cased for a letter.

Where a binding applies
-----------------------

``global``
    Anywhere on the page, except that a chord without ``Ctrl``/``Alt`` is
    ignored while the caret is in the YAML pane or another text field — ``n``
    creates a device on the canvas and types an ``n`` in the editor.
``canvas``
    Only while the diagram has focus. This is where the single-letter gestures
    live.

What a binding needs
--------------------

``needs`` is the honest precondition, shown greyed in the palette rather than
hidden — "why can I not do this" is a question the interface should answer:

``""``
    Nothing; it always works.
``session``
    ``netgraph web DIR``: there is a tree of files, not a scratchpad.
``write``
    ``--write`` as well: the session may put bytes on disk.
``focus``
    Something in the diagram is focused for the command to act on.

What the pointer offers
-----------------------

:data:`MENUS` is the second half of the same idea: what right-clicking a shape
puts in front of somebody who has never opened the palette. It is a *layout*, not
a second catalogue — every row names a :attr:`Binding.id` declared above, so a
context menu cannot offer a command the keyboard does not have, and a command
cannot be reached from the pointer without appearing in the reference under its
own shortcut. ``menu.js`` draws it; ``tests/test_web.py`` checks that every row
resolves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from netgraph.models import KINDS

__all__ = [
    "BINDINGS",
    "MENUS",
    "MENU_TARGETS",
    "SECTIONS",
    "Binding",
    "Menu",
    "MenuItem",
    "markdown_menus",
    "markdown_table",
    "payload",
]


@dataclass(frozen=True)
class Binding:
    """One command: what it is called, what reaches it, and what it needs."""

    #: Stable identity. ``keys.js`` registers a handler under exactly this name.
    id: str
    #: The imperative the palette and the reference show, e.g. "Create an element".
    title: str
    #: Which block of the reference it belongs to; see :data:`SECTIONS`.
    section: str
    #: Chords that run it, best-known first. Empty for a palette-only command.
    keys: tuple[str, ...]
    #: One line of prose: what it does, and anything surprising about when.
    detail: str
    #: ``global`` or ``canvas``; see the module docstring.
    where: str = "global"
    #: ``""``, ``session``, ``write`` or ``focus``; see the module docstring.
    needs: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "section": self.section,
            "keys": list(self.keys),
            "detail": self.detail,
            "where": self.where,
            "needs": self.needs,
        }


@dataclass(frozen=True)
class MenuItem:
    """One row of a context menu: a command, under the name that context gives it."""

    #: The :attr:`Binding.id` this row runs. Naming one that is not declared is
    #: a failure in ``tests/test_web.py`` rather than a row that does nothing.
    binding: str
    #: What the row reads as. ``""`` takes the binding's own title, which is the
    #: right answer whenever the palette's wording still fits. It often does not:
    #: "Delete the focused element" is how you ask for a command you cannot point
    #: at, and "Delete" is how you ask for one you have just right-clicked.
    label: str = ""
    #: ``""`` for a plain row. ``"kinds"`` for a row that opens a submenu of one
    #: entry per element kind, each running the command with that kind already
    #: chosen — how a new switch is two clicks rather than a form.
    submenu: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"binding": self.binding, "label": self.label, "submenu": self.submenu}


@dataclass(frozen=True)
class Menu:
    """What right-clicking one kind of thing offers, in the order it offers it."""

    #: What was under the pointer; one of :data:`MENU_TARGETS`.
    target: str
    #: The rows, in groups. A group is drawn with a rule above it, so "what this
    #: does to the picture" and "what this does to the files" do not run together.
    groups: tuple[tuple[MenuItem, ...], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "groups": [[item.to_dict() for item in group] for group in self.groups],
        }


#: What a right-click can land on. ``node`` and ``link`` are the two kinds of
#: shape the diagram draws and ``annotation`` is the commentary drawn over them
#: (§21); ``canvas`` is the paper between them all, which is where "make a new
#: one" belongs because it is the one place nothing else is meant.
#: ``selection`` outranks the rest: right-clicking inside a multi-selection is
#: asking about the *set*, and offering "Rename it…" there would be offering to
#: rename whichever of the eleven happened to be under the pointer.
MENU_TARGETS: Final[tuple[str, ...]] = (
    "selection",
    "node",
    "link",
    "annotation",
    "container",
    "canvas",
)


#: The order the reference and the palette group commands in. A reader looking
#: for "how do I delete this" should not have to read the view toggles first.
SECTIONS: Final[tuple[str, ...]] = (
    "Everywhere",
    "Moving around",
    "Editing the inventory",
    "Arranging the diagram",
    "The view",
    "Files and history",
)


BINDINGS: Final[tuple[Binding, ...]] = (
    # -- Everywhere --------------------------------------------------------
    Binding(
        id="palette",
        title="Command palette",
        section="Everywhere",
        keys=("Ctrl-K", "Ctrl-Shift-P"),
        detail=(
            "Every command on this page, searched by name — and every element "
            "address and file path in the inventory, so one field is also 'go to'."
        ),
    ),
    Binding(
        id="help",
        title="Keyboard shortcuts",
        section="Everywhere",
        keys=("?", "F1"),
        detail="This table, rendered from the bindings the page actually registered.",
    ),
    Binding(
        id="menu.open",
        title="Open the context menu",
        section="Everywhere",
        keys=("ContextMenu", "Shift-F10"),
        detail=(
            "What the pointer's right-click offers, on whatever the diagram has "
            "focused — the element, the link, or the canvas itself when nothing is."
        ),
        where="canvas",
    ),
    Binding(
        id="tour",
        title="Take the guided tour",
        section="Everywhere",
        keys=(),
        detail=(
            "Sixty seconds that create a device, cable it up, move its document, show "
            "the YAML that changed and undo the lot — on a throwaway copy of this "
            "inventory, so nothing here is written to your files."
        ),
        needs="session",
    ),
    Binding(
        id="dismiss",
        title="Close what is open",
        section="Everywhere",
        keys=("Escape",),
        detail="The palette, the reference, a prompt, the changes drawer, the inspector — in that order.",
    ),
    Binding(
        id="focus.files",
        title="Focus the inventory list",
        section="Everywhere",
        keys=("Alt-1",),
        detail="The file list. Arrow keys move down it; Enter opens a file.",
        needs="session",
    ),
    Binding(
        id="focus.editor",
        title="Focus the YAML pane",
        section="Everywhere",
        keys=("Alt-2",),
        detail="The text of the open document. Escape leaves it again.",
    ),
    Binding(
        id="focus.canvas",
        title="Focus the diagram",
        section="Everywhere",
        keys=("Alt-3",),
        detail="Puts a focus ring on an element and turns on the gestures below.",
    ),
    Binding(
        id="focus.outline",
        title="Focus the diagram outline",
        section="Everywhere",
        keys=("Alt-4",),
        detail=(
            "The diagram as a list a screen reader can read straight through: "
            "one line per element, with what it is linked to."
        ),
    ),
    Binding(
        id="render",
        title="Render now",
        section="Everywhere",
        keys=("Ctrl-Enter",),
        detail="Draw the diagram again without waiting for the editor to settle.",
    ),
    Binding(
        id="validate",
        title="Validate the inventory",
        section="Everywhere",
        keys=("Ctrl-Shift-Enter",),
        detail="Re-run the checks and move focus to the problems list.",
    ),
    # -- Moving around -----------------------------------------------------
    Binding(
        id="node.move",
        title="Move to the adjacent element",
        section="Moving around",
        keys=("ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown"),
        detail=(
            "Steps to the nearest element in that direction, preferring one this "
            "element is linked to — so a whole path can be walked with one hand."
        ),
        where="canvas",
    ),
    Binding(
        id="node.link",
        title="Cycle this element's links",
        section="Moving around",
        keys=("l", "Shift-L"),
        detail=(
            "Focuses each cable or tunnel that terminates here in turn, so a link "
            "can be inspected or removed without a pointer. Tab is left alone: the "
            "diagram is one stop on the page's tab order, never a trap."
        ),
        where="canvas",
    ),
    Binding(
        id="node.first",
        title="First element",
        section="Moving around",
        keys=("Home",),
        detail="The first element of the outline, which is the diagram in reading order.",
        where="canvas",
    ),
    Binding(
        id="node.last",
        title="Last element",
        section="Moving around",
        keys=("End",),
        detail="The last element of the outline.",
        where="canvas",
    ),
    Binding(
        id="node.inspect",
        title="Open the inspector",
        section="Moving around",
        keys=("Enter",),
        detail=(
            "Everything known about the focused element, and — in a session — the "
            "document that declares it, opened at its line."
        ),
        where="canvas",
        needs="focus",
    ),
    Binding(
        id="node.select",
        title="Pin the inspector",
        section="Moving around",
        keys=("Space",),
        detail="Keeps the inspector up, and tells the other tabs what this one is looking at.",
        where="canvas",
        needs="focus",
    ),
    Binding(
        id="element.goto",
        title="Go to element…",
        section="Moving around",
        keys=("Ctrl-G",),
        detail="The palette, opened over element addresses alone.",
    ),
    Binding(
        id="file.open",
        title="Open file…",
        section="Moving around",
        keys=("Ctrl-O",),
        detail="The palette, opened over the inventory's file paths alone.",
        needs="session",
    ),
    # -- Selecting ---------------------------------------------------------
    #
    # A selection is a set of element addresses, not a shape and not a DOM node,
    # which is what lets it survive a re-render and be posted verbatim as the
    # subject of a batch. ``select.js`` owns it; every command below is the same
    # set said a different way.
    Binding(
        id="select.all",
        title="Select everything in this view",
        section="Moving around",
        keys=("Ctrl-A",),
        detail=(
            "Every element and link the diagram is drawing, including the ones "
            "culled off screen. The canvas only — Ctrl-A in the YAML pane is "
            "still the text."
        ),
        where="canvas",
    ),
    Binding(
        id="select.none",
        title="Clear the selection",
        section="Moving around",
        keys=("Ctrl-Shift-A",),
        detail="Escape does this too, before it closes anything else.",
    ),
    Binding(
        id="select.extend",
        title="Extend the selection",
        section="Moving around",
        keys=("Shift-ArrowRight", "Shift-ArrowLeft", "Shift-ArrowUp", "Shift-ArrowDown"),
        detail=(
            "Steps the way the arrow keys do — preferring an element this one is "
            "linked to — and adds what it lands on, so a trunk and everything "
            "hanging off it can be collected without a pointer."
        ),
        where="canvas",
    ),
    # -- Editing the inventory ---------------------------------------------
    Binding(
        id="element.create",
        title="Create an element…",
        section="Editing the inventory",
        keys=("n",),
        detail="Asks for a kind and a name, and writes the document. 'netgraph edit create'.",
        where="canvas",
        needs="write",
    ),
    Binding(
        id="element.connect",
        title="Connect this element…",
        section="Editing the inventory",
        keys=("c",),
        detail=("Cables the focused element to another, port to port. 'netgraph edit connect'."),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="element.delete",
        title="Delete the selection",
        section="Editing the inventory",
        keys=("Delete", "Backspace"),
        detail=(
            "Removes everything selected, or the focused element when nothing is. "
            "Asks once, listing what goes and the cables that dangle as a result, "
            "and writes it as one change. 'netgraph edit delete' / 'disconnect'."
        ),
        where="canvas",
        needs="write",
    ),
    # -- The clipboard -----------------------------------------------------
    #
    # The four chords everybody already knows, and the reason they are worth
    # spelling out here: on this canvas they do not move *shapes*, they write
    # *documents*. Ctrl-C serialises the selected elements — and the cables
    # between them — onto the system clipboard as JSON, so the fragment can be
    # pasted into another window, another inventory, or a text editor. Ctrl-V
    # writes those documents into this tree with free names. All four are
    # ``canvas`` bindings on purpose: Ctrl-C in the YAML pane is still the text.
    Binding(
        id="clipboard.copy",
        title="Copy the selection",
        section="Editing the inventory",
        keys=("Ctrl-C",),
        detail=(
            "Puts the selected elements on the system clipboard as JSON — the "
            "documents themselves, plus any cable whose two ends are both "
            "selected. Paste it into another netgraph window, or into a text "
            "editor to read it. 'netgraph edit copy'."
        ),
        where="canvas",
        needs="session",
    ),
    Binding(
        id="clipboard.cut",
        title="Cut the selection",
        section="Editing the inventory",
        keys=("Ctrl-X",),
        detail=(
            "Copy, and then delete what was copied — as one change, so one "
            "Ctrl-Z puts the documents back. Asks first, listing what goes."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="clipboard.paste",
        title="Paste",
        section="Editing the inventory",
        keys=("Ctrl-V",),
        detail=(
            "Writes the clipboard fragment into this inventory: new documents, "
            "with free names, the internal cables rewired to the copies, and "
            "positions offset from the originals — or dropped where you last "
            "right-clicked. A fragment from another inventory pastes the same way."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="clipboard.duplicate",
        title="Duplicate the selection",
        section="Editing the inventory",
        keys=("Ctrl-D",),
        detail=(
            "Copy and paste in one keystroke, without touching the system "
            "clipboard: each selected element gets a sibling called 'sw1-copy' "
            "beside it. 'netgraph edit duplicate'."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="element.rename",
        title="Rename the focused element…",
        section="Editing the inventory",
        keys=("F2",),
        detail="Renames it and every reference to it. 'netgraph edit rename'.",
        where="canvas",
        needs="write",
    ),
    Binding(
        id="element.set",
        title="Set a field…",
        section="Editing the inventory",
        keys=("e",),
        detail=(
            "A dotted path and a YAML value, on every selected element at once — or "
            "on the focused one when nothing is selected. 'netgraph edit set'."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="element.unset",
        title="Remove a field…",
        section="Editing the inventory",
        keys=(),
        detail="'netgraph edit unset', across the whole selection as one change.",
        needs="write",
    ),
    Binding(
        id="element.move",
        title="Move to another file…",
        section="Editing the inventory",
        keys=(),
        detail=(
            "Moves the selected documents into a different file, together. 'netgraph edit move'."
        ),
        needs="write",
    ),
    Binding(
        id="container.create",
        title="New namespace…",
        section="Editing the inventory",
        keys=(),
        detail=(
            "Makes a namespace by putting something in it — the selection, moved "
            "there, or a new element created there. A namespace *is* a folder and a "
            "folder netgraph would read is one holding a document, so an empty one "
            "is not a thing the inventory can record."
        ),
        needs="write",
    ),
    Binding(
        id="container.move",
        title="Move into a namespace…",
        section="Editing the inventory",
        keys=(),
        detail=(
            "Re-homes the selection into another namespace: the typed form of "
            "dragging it into that container's box. The documents are rewritten "
            "into the folder and every reference to them is re-spelled. "
            "'netgraph edit move'."
        ),
        needs="write",
    ),
    Binding(
        id="element.disconnect",
        title="Disconnect a cable…",
        section="Editing the inventory",
        keys=(),
        detail="Removes a cable, leaving both devices. 'netgraph edit disconnect'.",
        needs="write",
    ),
    # -- Routing a link ----------------------------------------------------
    #
    # A cable's shape is inventory too (§18), so every one of these ends in a
    # ``kind: layout`` document through the same write path a rename takes.
    # Each is reachable two ways — a gesture on the canvas, and a command from
    # anywhere — because a bend that can only be placed with a mouse is a bend
    # somebody working from the keyboard cannot place at all.
    Binding(
        id="link.bend",
        title="Add a bend to the focused link",
        section="Editing the inventory",
        keys=("b",),
        detail=(
            "Drops a waypoint half way along the link, which the route then passes "
            "through. Double-clicking the line does the same at the point clicked."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="link.straighten",
        title="Straighten the focused link",
        section="Editing the inventory",
        keys=("Shift-B",),
        detail=(
            "Clears every bend, leaving the link to run directly between its two "
            "devices. The routing style and the label position are kept."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="link.route",
        title="Change how the link is routed…",
        section="Editing the inventory",
        keys=("r",),
        detail=(
            "Spline, orthogonal or straight, on this link alone. Clearing it takes the "
            "view's default back. Honoured by 'netgraph render' as well as here."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="link.pin-route",
        title="Pin the route the renderer worked out",
        section="Editing the inventory",
        keys=("Shift-R",),
        detail=(
            "Writes the bends netgraph computed to keep this link clear of the boxes it "
            "passes into the layout document, so they become bends you placed: they stop "
            "being recomputed, they get a grab handle each, and moving a device no longer "
            "moves them. Refuses on a link that needed no detour, since there would be "
            "nothing to pin."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="link.label.reset",
        title="Put the link's label back on the line",
        section="Editing the inventory",
        keys=(),
        detail=(
            "Undoes a nudged label, leaving it half way along the route where the "
            "renderer puts one nobody has moved."
        ),
        needs="write",
    ),
    # -- Annotating the diagram --------------------------------------------
    #
    # A note, an area and a legend are not elements (§21): they declare no
    # network fact, and nothing in the inventory refers to them. So they have
    # their own commands rather than sharing the element ones — Enter inspects
    # an element and F2 renames one, and neither question can be asked of a
    # callout without the answer meaning something else.
    Binding(
        id="annotation.create",
        title="Add a note to the diagram…",
        section="Editing the inventory",
        keys=("Shift-N",),
        detail=(
            "Drops a note where the pointer is — or in the middle of the view when the "
            "keyboard asks — and opens it for typing. Right-clicking an element or a "
            "link anchors the note to it instead, so it follows what it is about. "
            "'netgraph edit create-annotation'."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="annotation.edit",
        title="Edit the note's text…",
        section="Editing the inventory",
        keys=("Shift-E",),
        detail=(
            "A text box over the note itself, in the markdown subset §21 defines. "
            "Ctrl-Enter or clicking away writes 'spec.text'; Escape abandons it and "
            "writes nothing. Double-clicking the note does the same."
        ),
        where="canvas",
        needs="write",
    ),
    Binding(
        id="interface.add",
        title="Add an interface…",
        section="Editing the inventory",
        keys=("i",),
        detail="'netgraph edit add-interface'.",
        where="canvas",
        needs="write",
    ),
    Binding(
        id="interface.remove",
        title="Remove an interface…",
        section="Editing the inventory",
        keys=(),
        detail="'netgraph edit remove-interface'.",
        needs="write",
    ),
    # -- Arranging the diagram ---------------------------------------------
    #
    # Nine commands that mean nothing about one shape. Each one is a *selection*
    # turned into a batch of ``set-geometry`` operations by
    # :mod:`netgraph.edit.arrange` — one per layout document that loses an
    # entry — so a whole alignment is one reviewable diff and one ``Ctrl-Z``.
    #
    # None of them carries a chord. They are pointer-and-palette gestures in
    # every editor that has them, the letters left on the canvas are worth more
    # to the commands somebody uses every minute, and each is two keystrokes
    # away through the palette and one row away from a right-click.
    Binding(
        id="align.left",
        title="Align left",
        section="Arranging the diagram",
        keys=(),
        detail="Every selected element onto the leftmost one's left edge.",
        needs="write",
    ),
    Binding(
        id="align.centre",
        title="Align centres",
        section="Arranging the diagram",
        keys=(),
        detail="Onto the vertical axis half way across the selection.",
        needs="write",
    ),
    Binding(
        id="align.right",
        title="Align right",
        section="Arranging the diagram",
        keys=(),
        detail="Onto the rightmost one's right edge.",
        needs="write",
    ),
    Binding(
        id="align.top",
        title="Align top",
        section="Arranging the diagram",
        keys=(),
        detail="Onto the topmost one's top edge.",
        needs="write",
    ),
    Binding(
        id="align.middle",
        title="Align middles",
        section="Arranging the diagram",
        keys=(),
        detail="Onto the horizontal axis half way down the selection.",
        needs="write",
    ),
    Binding(
        id="align.bottom",
        title="Align bottom",
        section="Arranging the diagram",
        keys=(),
        detail="Onto the bottommost one's bottom edge.",
        needs="write",
    ),
    Binding(
        id="distribute.horizontal",
        title="Distribute horizontally",
        section="Arranging the diagram",
        keys=(),
        detail=(
            "Equal gaps between the boxes, left to right, with the two outermost "
            "left where they are. Needs three."
        ),
        needs="write",
    ),
    Binding(
        id="distribute.vertical",
        title="Distribute vertically",
        section="Arranging the diagram",
        keys=(),
        detail="The same, top to bottom.",
        needs="write",
    ),
    Binding(
        id="geometry.snap",
        title="Snap to the grid",
        section="Arranging the diagram",
        keys=(),
        detail=(
            "Rounds each selected element's position to the pitch this inventory "
            "sets in 'netgraph.toml' ([editor] grid, 20 points by default)."
        ),
        needs="write",
    ),
    # -- The view ----------------------------------------------------------
    Binding(
        id="view.layer",
        title="Switch layer…",
        section="The view",
        keys=(),
        detail="Physical, l1, l2, l3, overlay, routing, rack, power, identity.",
    ),
    Binding(
        id="view.layer.next",
        title="Next layer",
        section="The view",
        keys=("]",),
        detail="The next entry of the layer menu.",
    ),
    Binding(
        id="view.layer.previous",
        title="Previous layer",
        section="The view",
        keys=("[",),
        detail="The previous entry of the layer menu.",
    ),
    Binding(
        id="view.ips",
        title="Toggle IP addresses",
        section="The view",
        keys=("Alt-I",),
        detail="Whether the picture prints addresses. The inspector shows them either way.",
    ),
    Binding(
        id="view.vlans",
        title="Toggle VLANs",
        section="The view",
        keys=("Alt-V",),
        detail="Whether the picture prints VLAN membership.",
    ),
    Binding(
        id="view.group",
        title="Toggle namespace grouping",
        section="The view",
        keys=("Alt-G",),
        detail="Collapse each namespace into one box.",
    ),
    Binding(
        id="container.fold",
        title="Fold or unfold this namespace",
        section="The view",
        keys=("f",),
        detail=(
            "Draws a namespace box as the single node it stands for, or opens it "
            "again — the container the pointer picked, or the one holding the focused "
            "element. The same folding 'netgraph render --collapse' does. A view, not "
            "an edit: nothing is written, and how much of a diagram somebody wants to "
            "look at is not a fact about the network."
        ),
        where="canvas",
    ),
    Binding(
        id="view.annotations",
        title="Toggle annotations",
        section="The view",
        keys=("Alt-N",),
        detail=(
            "Whether the notes, areas and legends of §21 are drawn. They are "
            "commentary, never topology, so hiding them changes nothing the tool "
            "concludes — only how much of the picture is somebody's explanation."
        ),
    ),
    Binding(
        id="view.strict",
        title="Toggle strict",
        section="The view",
        keys=("Alt-S",),
        detail="Report warnings as errors.",
    ),
    Binding(
        id="view.vlanFilter",
        title="Filter by VLAN…",
        section="The view",
        keys=(),
        detail="Keep only elements participating in the VLANs given.",
    ),
    Binding(
        id="view.failure",
        title="Failure mode",
        section="The view",
        keys=("Alt-F",),
        detail=(
            "Click an element and everything it would isolate from the gateways greys out; "
            "the status line names the count. Reads only — nothing is written, and Escape or "
            "the same key puts the diagram back."
        ),
        needs="session",
    ),
    Binding(
        id="view.fit",
        title="Fit the diagram",
        section="The view",
        keys=("0",),
        detail="Undo the panning and zooming.",
    ),
    Binding(
        id="view.zoomIn",
        title="Zoom in",
        section="The view",
        keys=("Plus", "="),
        detail="Around the middle of the canvas, so nothing jumps off screen.",
    ),
    Binding(
        id="view.zoomOut",
        title="Zoom out",
        section="The view",
        keys=("Minus",),
        detail="Around the middle of the canvas.",
    ),
    # -- Files and history -------------------------------------------------
    Binding(
        id="file.save",
        title="Save the open file",
        section="Files and history",
        keys=("Ctrl-S",),
        detail="Writes it back, stating the hash it was opened at.",
        needs="write",
    ),
    Binding(
        id="history.undo",
        title="Undo",
        section="Files and history",
        keys=("Ctrl-Z",),
        detail="The session's stack, not the browser's: it puts files back on disk.",
        needs="write",
    ),
    Binding(
        id="history.redo",
        title="Redo",
        section="Files and history",
        keys=("Ctrl-Shift-Z", "Ctrl-Y"),
        detail="Applies the last undone change again.",
        needs="write",
    ),
    Binding(
        id="changes.toggle",
        title="Changes drawer",
        section="Files and history",
        keys=("Ctrl-B",),
        detail="This session's changes, and the diagram repainted as the diff they add up to.",
        needs="session",
    ),
    Binding(
        id="style.toggle",
        title="Style inspector",
        section="Editing the inventory",
        keys=("Ctrl-Shift-Y",),
        detail=(
            "How the selection is drawn (§22), which layer each value came from, and the "
            "controls to change it. A change is written to spec.style, so the picture and "
            "the YAML stay one thing."
        ),
        needs="session",
    ),
    Binding(
        id="style.inspect",
        title="Restyle the selection",
        section="Editing the inventory",
        keys=(),
        detail="Open the style inspector on what is selected.",
        needs="write",
    ),
    Binding(
        id="changes.copy",
        title="Copy the equivalent commands",
        section="Files and history",
        keys=(),
        detail="The session as a 'netgraph edit' script somebody else can review or run.",
        needs="session",
    ),
    Binding(
        id="timeline.toggle",
        title="History timeline",
        section="Files and history",
        keys=("Ctrl-Shift-H",),
        detail=(
            "A scrubber over the commits that changed this inventory. The diagram becomes the "
            "diff the selected commit carries against its parent, arranged as that revision "
            "arranged it."
        ),
        needs="session",
    ),
    Binding(
        id="timeline.prev",
        title="Older revision",
        section="Files and history",
        keys=("Alt-ArrowLeft",),
        detail="One commit back along the timeline. Stops the playback if it is running.",
        needs="session",
    ),
    Binding(
        id="timeline.next",
        title="Newer revision",
        section="Files and history",
        keys=("Alt-ArrowRight",),
        detail="One commit forward along the timeline.",
        needs="session",
    ),
    Binding(
        id="timeline.play",
        title="Play the history",
        section="Files and history",
        keys=("Alt-P",),
        detail=(
            "Step through the range by itself, a frame at a time, until the newest revision or "
            "until one that will not load."
        ),
        needs="session",
    ),
)


#: What right-clicking offers, per target.
#:
#: A short menu on purpose. The palette already has all fifty commands and is one
#: keystroke away; what the pointer is for is the handful somebody reaches for
#: while looking at a shape, and a menu long enough to need reading is a menu
#: that has stopped being faster than typing.
#:
#: The order is the same argument three times: what this tells you, then what it
#: adds, then what it changes, then what it removes — so the destructive row is
#: never where the reflex click lands.
MENUS: Final[tuple[Menu, ...]] = (
    Menu(
        target="selection",
        groups=(
            (
                MenuItem("align.left", "Align left"),
                MenuItem("align.centre", "Align centres"),
                MenuItem("align.right", "Align right"),
                MenuItem("align.top", "Align top"),
                MenuItem("align.middle", "Align middles"),
                MenuItem("align.bottom", "Align bottom"),
            ),
            (
                MenuItem("distribute.horizontal", "Distribute horizontally"),
                MenuItem("distribute.vertical", "Distribute vertically"),
                MenuItem("geometry.snap", "Snap to the grid"),
            ),
            (
                MenuItem("clipboard.copy", "Copy"),
                MenuItem("clipboard.cut", "Cut"),
                MenuItem("clipboard.duplicate", "Duplicate"),
            ),
            (
                MenuItem("element.set", "Set a field on all of them…"),
                MenuItem("element.unset", "Remove a field from all of them…"),
                MenuItem("element.move", "Move their documents…"),
                MenuItem("container.move", "Move into a namespace…"),
            ),
            (
                MenuItem("select.none", "Clear the selection"),
                MenuItem("element.delete", "Delete all of them"),
            ),
        ),
    ),
    Menu(
        target="node",
        groups=(
            (
                MenuItem("node.inspect", "Inspect it"),
                MenuItem("node.select", "Pin the inspector"),
            ),
            (
                MenuItem("element.connect", "Cable it to…"),
                MenuItem("interface.add", "Add an interface…"),
                # Here rather than only on the canvas because *this* is where a
                # note gets an anchor: one created over an element is about that
                # element and follows it when the diagram is laid out again.
                MenuItem("annotation.create", "Note about it…"),
            ),
            (
                MenuItem("clipboard.copy", "Copy"),
                MenuItem("clipboard.cut", "Cut"),
                MenuItem("clipboard.duplicate", "Duplicate"),
            ),
            (
                MenuItem("element.rename", "Rename it…"),
                MenuItem("style.inspect", "Change how it looks…"),
                MenuItem("element.set", "Set a field…"),
                MenuItem("element.unset", "Remove a field…"),
                MenuItem("element.move", "Move its document…"),
            ),
            (MenuItem("element.delete", "Delete it"),),
        ),
    ),
    Menu(
        target="link",
        groups=(
            (MenuItem("node.inspect", "Inspect it"),),
            (
                MenuItem("link.bend", "Add a bend"),
                MenuItem("link.straighten", "Straighten it"),
                MenuItem("link.route", "Route it…"),
                MenuItem("link.pin-route", "Pin the computed route"),
                MenuItem("link.label.reset", "Put the label back on the line"),
                MenuItem("annotation.create", "Note about it…"),
            ),
            (
                MenuItem("style.inspect", "Change how it looks…"),
                MenuItem("element.set", "Set a field…"),
                MenuItem("element.delete", "Disconnect it"),
            ),
        ),
    ),
    # A note, an area and a legend all answer here. The rows that only make
    # sense for one of the three are drawn greyed with the reason on them —
    # "an area has no text" is worth more than a menu that silently differs
    # depending on which piece of commentary was clicked.
    Menu(
        target="annotation",
        groups=(
            (MenuItem("annotation.edit", "Edit the text…"),),
            (MenuItem("element.delete", "Delete it"),),
        ),
    ),
    # A namespace frame. Its rows are about the *box*: what to put in it, how
    # much of it to show, and how big it is. Deliberately short — everything
    # else a reader wants is a row on the thing inside it.
    Menu(
        target="container",
        groups=(
            (
                MenuItem("container.fold", "Fold or unfold it"),
                MenuItem("element.create", "New in it", submenu="kinds"),
                # A paste is a drop: the copies land in this namespace, the same
                # way dragging something into the box would put it there.
                MenuItem("clipboard.paste", "Paste into it"),
            ),
            (
                MenuItem("container.move", "Move the selection into it…"),
                MenuItem("container.create", "New namespace inside it…"),
            ),
        ),
    ),
    Menu(
        target="canvas",
        groups=(
            (
                MenuItem("element.create", "New", submenu="kinds"),
                MenuItem("container.create", "New namespace…"),
                MenuItem("annotation.create", "New note"),
                # Here and nowhere else, because *this* is where a paste gets
                # its anchor: the fragment lands where the pointer was rather
                # than offset from wherever it was copied.
                MenuItem("clipboard.paste", "Paste here"),
            ),
            (
                MenuItem("view.layer", "Show another layer…"),
                MenuItem("view.fit", "Fit the diagram"),
            ),
            (
                MenuItem("history.undo", "Undo"),
                MenuItem("history.redo", "Redo"),
                MenuItem("changes.toggle", "Show what changed"),
            ),
            (MenuItem("palette", "All commands…"),),
        ),
    ),
)


def payload() -> dict[str, Any]:
    """What ``/api/bindings`` answers: the table, and the order to group it in.

    ``kinds`` rides along because the create gesture has to offer a menu of
    them, and :data:`netgraph.models.KINDS` is where they are declared — a page
    with its own list of element kinds is a page that stops offering the
    thirteenth one the day it is added.
    """
    return {
        "sections": list(SECTIONS),
        "bindings": [binding.to_dict() for binding in BINDINGS],
        "menus": [menu.to_dict() for menu in MENUS],
        "kinds": list(KINDS),
    }


#: How ``needs`` reads in a table of prose rather than in a JSON field.
_NEEDS: Final[dict[str, str]] = {
    "": "—",
    "session": "a folder",
    "write": "`--write`",
    "focus": "a focused element",
}

#: How ``where`` reads.
_WHERE: Final[dict[str, str]] = {"global": "anywhere", "canvas": "the diagram"}


def _cell(text: str) -> str:
    """Collapse a sentence into one Markdown table cell."""
    return re.sub(r"\s+", " ", text).replace("|", "\\|").strip()


def markdown_table() -> str:
    """The whole table as Markdown, one block per section.

    Used by ``tools/gen_docs.py`` for the ``keybindings`` region of
    ``docs/commands/web.md``. Sections are emitted in :data:`SECTIONS` order, and
    a section with nothing in it is skipped rather than left as an empty heading.
    """
    blocks: list[str] = []
    for section in SECTIONS:
        rows = [binding for binding in BINDINGS if binding.section == section]
        if not rows:  # pragma: no cover - every section in SECTIONS has entries
            continue
        lines = [
            f"**{section}**",
            "",
            "| Keys | Command | Where | Needs | What it does |",
            "|---|---|---|---|---|",
        ]
        for binding in rows:
            keys = " / ".join(f"`{key}`" for key in binding.keys) or "*palette only*"
            lines.append(
                f"| {keys} | {_cell(binding.title)} | {_WHERE[binding.where]} "
                f"| {_NEEDS[binding.needs]} | {_cell(binding.detail)} |"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


#: What each target is called in prose, and what right-clicking it means.
_TARGETS: Final[dict[str, str]] = {
    "selection": "a multi-selection",
    "node": "an element",
    "link": "a link",
    "annotation": "a note, an area or a legend",
    "container": "a namespace box",
    "canvas": "the canvas",
}


def markdown_menus() -> str:
    """The context menus as Markdown, one block per target.

    Used by ``tools/gen_docs.py`` for the ``context-menus`` region of
    ``docs/commands/web.md``. Each row shows the label the menu draws and the
    chord that runs the same command, which is the point of the arrangement: the
    menu is how the shortcut is *learnt*, so the page that documents one has to
    document the other beside it.
    """
    blocks: list[str] = []
    for menu in MENUS:
        lines = [
            f"**Right-clicking {_TARGETS[menu.target]}**",
            "",
            "| Offers | Same as | Needs |",
            "|---|---|---|",
        ]
        for group in menu.groups:
            for item in group:
                binding = next(one for one in BINDINGS if one.id == item.binding)
                label = _cell(item.label or binding.title)
                if item.submenu == "kinds":
                    label += " ▸ *(one row per element kind)*"
                chord = f"`{binding.keys[0]}`" if binding.keys else "*palette only*"
                lines.append(
                    f"| {label} | {_cell(binding.title)} — {chord} | {_NEEDS[binding.needs]} |"
                )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
