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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from netgraph.models import KINDS

__all__ = [
    "BINDINGS",
    "SECTIONS",
    "Binding",
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


#: The order the reference and the palette group commands in. A reader looking
#: for "how do I delete this" should not have to read the view toggles first.
SECTIONS: Final[tuple[str, ...]] = (
    "Everywhere",
    "Moving around",
    "Editing the inventory",
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
        title="Delete the focused element",
        section="Editing the inventory",
        keys=("Delete", "Backspace"),
        detail=(
            "Removes the element, or the cable when a link is focused. Asks first. "
            "'netgraph edit delete' / 'disconnect'."
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
        detail="A dotted path and a YAML value, on the focused element. 'netgraph edit set'.",
        where="canvas",
        needs="write",
    ),
    Binding(
        id="element.unset",
        title="Remove a field…",
        section="Editing the inventory",
        keys=(),
        detail="'netgraph edit unset'.",
        needs="write",
    ),
    Binding(
        id="element.move",
        title="Move to another file…",
        section="Editing the inventory",
        keys=(),
        detail="Moves the element's document into a different file. 'netgraph edit move'.",
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
