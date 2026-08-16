"""The write path: typed, reversible, comment-preserving edits to an inventory.

Everything netviz does beyond drawing a picture — a visual editor, an undo
stack, a ``plan``/``apply`` pair — needs one thing first: a way to *change* the
files that is as safe and as lossless as the way it reads them. This package is
that way, and it is meant to be the only one.

    from netviz.edit import EditSession, SetField

    session = EditSession(root=Path("inventory"))
    applied = session.apply(SetField(address="sites/hq/core-sw", path="spec.model",
                                     value="C9300"))
    print(session.diff())          # what it would write
    session.commit()               # validated, conflict-checked, atomic per file
    session.apply_all(applied.inverse)   # and undone

Four promises, each implemented in its own module:

**Lossless.** :mod:`netviz.edit.roundtrip` holds a file as a preamble plus one
verbatim text per document, and re-emits only the documents an operation named.
A diff of an edit shows the edit.

**Reversible.** :mod:`netviz.edit.apply` returns the operations that undo each
one, exactly — so an undo stack is a list and undo is applying it backwards.

**Reference-aware.** :mod:`netviz.edit.references` reads the references off the
models, so a rename rewrites every mention of the element across the tree in the
spelling each document used. :mod:`netviz.edit.cascade` says what a *delete*
therefore has to take with it — the links that cannot exist without it, the §21
annotations the loader would refuse without it, the §18 geometry that placed it —
and a delete either takes all of it or refuses and names what is in the way.
:mod:`netviz.edit.rename` is the same three layers for a rename: the geometry
and the annotations move onto the new name rather than being left behind, in the
spelling the document that holds them was already using.

**Safe.** :mod:`netviz.edit.session` hashes every file it reads and refuses to
write over one that moved, and loads and validates the tree as it *would be*
before writing any of it.

``docs/editing.md`` is the prose version of all of this, and
``docs/commands/edit.md`` documents the command that exposes it.
"""

from __future__ import annotations

from netviz.edit.apply import AppliedOperation, apply_operation
from netviz.edit.arrange import (
    ALIGNMENTS,
    ARRANGEMENTS,
    DEFAULT_GRID,
    DISTRIBUTIONS,
    arrange_operations,
    describe_arrangement,
)
from netviz.edit.batch import Batch, BatchResult
from netviz.edit.cascade import CascadePlan, plan_cascade
from netviz.edit.clipboard import (
    CLIPBOARD_FORMAT,
    DEFAULT_SUFFIX,
    UNIQUE_FIELDS,
    CopyPlan,
    Dropped,
    UniqueField,
    clipboard_payload,
    copy_plan,
    dedupe_name,
    paste_plan,
    strip_unique,
    unique_fields_markdown,
)
from netviz.edit.commands import (
    INVENTORY_PLACEHOLDER,
    command_for,
    command_list,
    commands_text,
)
from netviz.edit.containers import (
    MAX_MOVES,
    MovePlan,
    Rehome,
    check_namespace,
    move_plan,
)
from netviz.edit.errors import (
    AddressError,
    CascadeRequired,
    ConflictError,
    EditError,
    OperationError,
    PlacementError,
    Problem,
    RoundTripError,
    ValidationRefused,
)
from netviz.edit.operations import (
    OPERATIONS,
    AddInterface,
    AppendItem,
    Connect,
    CopyElement,
    CreateElement,
    DeleteElement,
    Disconnect,
    MoveElement,
    Operation,
    RemoveFile,
    RemoveInterface,
    RenameElement,
    SetField,
    SetGeometry,
    UnsetField,
    WriteFile,
    operation_from_dict,
    operations_from_json,
    operations_to_json,
)
from netviz.edit.paths import format_field_path, parse_field_path
from netviz.edit.placement import COLLECTION_STEMS, FileFacts, choose_file
from netviz.edit.references import NameIndex, Reference, ReferenceRole, references_of
from netviz.edit.rename import RenamePlan, plan_rename
from netviz.edit.roundtrip import YamlDocument, YamlFile
from netviz.edit.session import EditSession, EditSummary, Mark
from netviz.edit.tree import EditableTree, TreeSnapshot, digest_of

__all__ = [
    "ALIGNMENTS",
    "ARRANGEMENTS",
    "CLIPBOARD_FORMAT",
    "COLLECTION_STEMS",
    "DEFAULT_GRID",
    "DEFAULT_SUFFIX",
    "DISTRIBUTIONS",
    "INVENTORY_PLACEHOLDER",
    "MAX_MOVES",
    "OPERATIONS",
    "UNIQUE_FIELDS",
    "AddInterface",
    "AddressError",
    "AppendItem",
    "AppliedOperation",
    "Batch",
    "BatchResult",
    "CascadePlan",
    "CascadeRequired",
    "ConflictError",
    "Connect",
    "CopyElement",
    "CopyPlan",
    "CreateElement",
    "DeleteElement",
    "Disconnect",
    "Dropped",
    "EditError",
    "EditSession",
    "EditSummary",
    "EditableTree",
    "FileFacts",
    "Mark",
    "MoveElement",
    "MovePlan",
    "NameIndex",
    "Operation",
    "OperationError",
    "PlacementError",
    "Problem",
    "Reference",
    "ReferenceRole",
    "Rehome",
    "RemoveFile",
    "RemoveInterface",
    "RenameElement",
    "RenamePlan",
    "RoundTripError",
    "SetField",
    "SetGeometry",
    "TreeSnapshot",
    "UniqueField",
    "UnsetField",
    "ValidationRefused",
    "WriteFile",
    "YamlDocument",
    "YamlFile",
    "apply_operation",
    "arrange_operations",
    "check_namespace",
    "choose_file",
    "clipboard_payload",
    "command_for",
    "command_list",
    "commands_text",
    "copy_plan",
    "dedupe_name",
    "describe_arrangement",
    "digest_of",
    "format_field_path",
    "move_plan",
    "operation_from_dict",
    "operations_from_json",
    "operations_to_json",
    "parse_field_path",
    "paste_plan",
    "plan_cascade",
    "plan_rename",
    "references_of",
    "strip_unique",
    "unique_fields_markdown",
]
