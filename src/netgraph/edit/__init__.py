"""The write path: typed, reversible, comment-preserving edits to an inventory.

Everything netgraph does beyond drawing a picture — a visual editor, an undo
stack, a ``plan``/``apply`` pair — needs one thing first: a way to *change* the
files that is as safe and as lossless as the way it reads them. This package is
that way, and it is meant to be the only one.

    from netgraph.edit import EditSession, SetField

    session = EditSession(root=Path("inventory"))
    applied = session.apply(SetField(address="sites/hq/core-sw", path="spec.model",
                                     value="C9300"))
    print(session.diff())          # what it would write
    session.commit()               # validated, conflict-checked, atomic per file
    session.apply_all(applied.inverse)   # and undone

Four promises, each implemented in its own module:

**Lossless.** :mod:`netgraph.edit.roundtrip` holds a file as a preamble plus one
verbatim text per document, and re-emits only the documents an operation named.
A diff of an edit shows the edit.

**Reversible.** :mod:`netgraph.edit.apply` returns the operations that undo each
one, exactly — so an undo stack is a list and undo is applying it backwards.

**Reference-aware.** :mod:`netgraph.edit.references` reads the references off the
models, so a rename rewrites every mention of the element across the tree in the
spelling each document used, and a delete either takes the cables and tunnels
that terminate on the element with it or refuses and names them.

**Safe.** :mod:`netgraph.edit.session` hashes every file it reads and refuses to
write over one that moved, and loads and validates the tree as it *would be*
before writing any of it.

``docs/editing.md`` is the prose version of all of this, and
``docs/commands/edit.md`` documents the command that exposes it.
"""

from __future__ import annotations

from netgraph.edit.apply import AppliedOperation, apply_operation
from netgraph.edit.errors import (
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
from netgraph.edit.operations import (
    OPERATIONS,
    AddInterface,
    Connect,
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
from netgraph.edit.paths import format_field_path, parse_field_path
from netgraph.edit.placement import COLLECTION_STEMS, FileFacts, choose_file
from netgraph.edit.references import NameIndex, Reference, ReferenceRole, references_of
from netgraph.edit.roundtrip import YamlDocument, YamlFile
from netgraph.edit.session import EditSession, EditSummary
from netgraph.edit.tree import EditableTree, digest_of

__all__ = [
    "COLLECTION_STEMS",
    "OPERATIONS",
    "AddInterface",
    "AddressError",
    "AppliedOperation",
    "CascadeRequired",
    "ConflictError",
    "Connect",
    "CreateElement",
    "DeleteElement",
    "Disconnect",
    "EditError",
    "EditSession",
    "EditSummary",
    "EditableTree",
    "FileFacts",
    "MoveElement",
    "NameIndex",
    "Operation",
    "OperationError",
    "PlacementError",
    "Problem",
    "Reference",
    "ReferenceRole",
    "RemoveFile",
    "RemoveInterface",
    "RenameElement",
    "RoundTripError",
    "SetField",
    "SetGeometry",
    "UnsetField",
    "ValidationRefused",
    "WriteFile",
    "YamlDocument",
    "YamlFile",
    "apply_operation",
    "choose_file",
    "digest_of",
    "format_field_path",
    "operation_from_dict",
    "operations_from_json",
    "operations_to_json",
    "parse_field_path",
    "references_of",
]
