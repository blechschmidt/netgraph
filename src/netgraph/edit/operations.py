"""The closed set of things an edit can be.

Everything that writes an inventory — the CLI, the web editor, and the ``apply``
that is still to come — expresses itself as a list of these and nothing else.
Two properties follow from that, and both are the reason for the closed set:

* **Every change is describable.** An operation is a small immutable record with
  a JSON form, so a change can be sent over a socket, written to a log, replayed,
  or shown to somebody for approval before it touches a file.
* **Every change is undoable.** Applying one returns its inverse — also
  operations — so an undo stack is a list, and undo is applying the list
  backwards.

There are two layers. The **semantic** operations are the vocabulary a person
uses: create this, rename that, connect these two. The **primitive** ones —
:class:`WriteFile` and :class:`RemoveFile` — are file-level and carry text
verbatim. They exist because an inverse has to restore *bytes*: undoing a rename
means putting back the comment style, the quoting and the reference spellings of
every document the rename rewrote, and no semantic operation can promise that.
So a semantic operation is inverted by a semantic one where that is exact
by construction (a create is undone by a delete, a move by a move back) and by
primitives everywhere else. Both kinds go through the same validation gate; a
primitive is a shortcut past the round-trip machinery, not past the checks.

The JSON form is one object per operation, discriminated by ``op``::

    {"op": "set", "address": "switches/sw-home", "path": "spec.model", "value": "C9300"}
    {"op": "connect", "a": "sw-home:eth3", "b": "pc-desk:eno1", "spec": {"medium": "copper"}}

Keys that are absent mean "not given" and take the operation's default; no key
ever means something different when it is present and null.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

from netgraph.edit.errors import OperationError
from netgraph.edit.paths import format_field_path, parse_field_path

__all__ = [
    "OPERATIONS",
    "AddInterface",
    "Connect",
    "CreateElement",
    "DeleteElement",
    "Disconnect",
    "MoveElement",
    "Operation",
    "RemoveFile",
    "RemoveInterface",
    "RenameElement",
    "SetField",
    "UnsetField",
    "WriteFile",
    "operation_from_dict",
    "operations_from_json",
    "operations_to_json",
]


class Operation:
    """Base of every operation: a name, a JSON form and a one-line description."""

    #: The value of ``op`` in the JSON form. Unique across :data:`OPERATIONS`.
    op: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - every subclass overrides
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - every subclass overrides
        raise NotImplementedError

    def __str__(self) -> str:
        return self.describe()


# --------------------------------------------------------------------------- #
# Elements
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CreateElement(Operation):
    """Add a document declaring a new element.

    ``spec`` is the document's ``spec`` mapping, exactly as it would be written
    in YAML; ``metadata`` is everything besides the name — a description, labels,
    annotations. Where the document lands is :mod:`netgraph.edit.placement`'s
    decision unless ``file`` overrides it.
    """

    op: ClassVar[str] = "create"

    kind: str
    name: str
    namespace: str = ""
    spec: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Relative POSIX path to write into, or ``None`` to let placement decide.
    file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op": self.op,
            "kind": self.kind,
            "name": self.name,
            "namespace": self.namespace,
            "spec": dict(self.spec),
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        if self.file is not None:
            payload["file"] = self.file
        return payload

    def describe(self) -> str:
        where = f" in {self.namespace}" if self.namespace else ""
        return f"create {self.kind} {self.name}{where}"


@dataclass(frozen=True)
class DeleteElement(Operation):
    """Remove an element's document, and the file if it was the last one in it.

    Refuses when other elements refer to it, unless ``cascade`` is set — in
    which case the cables and tunnels that terminate on it go too, and the
    optional references to it (an adapter's ``attached_to``, a power input) are
    cleared.
    """

    op: ClassVar[str] = "delete"

    address: str
    cascade: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "address": self.address}
        if self.cascade:
            payload["cascade"] = True
        return payload

    def describe(self) -> str:
        return f"delete {self.address}" + (" and everything that needs it" if self.cascade else "")


@dataclass(frozen=True)
class RenameElement(Operation):
    """Change ``metadata.name``, and every reference to it across the tree.

    The namespace does not change — that is :class:`MoveElement` — so a rename
    is a rename and never a move by accident.
    """

    op: ClassVar[str] = "rename"

    address: str
    new_name: str

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "address": self.address, "new_name": self.new_name}

    def describe(self) -> str:
        return f"rename {self.address} to {self.new_name}"


@dataclass(frozen=True)
class MoveElement(Operation):
    """Move an element's document to another file, verbatim.

    The document's bytes are carried across unchanged, comments included. If the
    new file is in another folder the element's namespace changes with it, and
    the references to it are rewritten the way a rename rewrites them.
    """

    op: ClassVar[str] = "move"

    address: str
    #: The file to move it into, relative to the inventory root.
    file: str
    #: Which document it becomes in that file; ``None`` appends it. Set on the
    #: inverse, so undoing a move puts the document back where it was.
    index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "address": self.address, "file": self.file}
        if self.index is not None:
            payload["index"] = self.index
        return payload

    def describe(self) -> str:
        return f"move {self.address} to {self.file}"


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SetField(Operation):
    """Write a value at a field path, creating the mappings on the way to it."""

    op: ClassVar[str] = "set"

    address: str
    path: str
    value: Any = None

    def __post_init__(self) -> None:
        parse_field_path(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "address": self.address, "path": self.path, "value": self.value}

    def describe(self) -> str:
        return f"set {self.address} {self.path} = {json.dumps(self.value, default=str)}"


@dataclass(frozen=True)
class UnsetField(Operation):
    """Remove the value at a field path."""

    op: ClassVar[str] = "unset"

    address: str
    path: str

    def __post_init__(self) -> None:
        parse_field_path(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "address": self.address, "path": self.path}

    def describe(self) -> str:
        return f"unset {self.address} {self.path}"


@dataclass(frozen=True)
class AddInterface(Operation):
    """Append an interface to ``spec.interfaces``.

    A separate operation from :class:`SetField` because a sequence entry cannot
    be written at a path that does not exist yet, and because adding a port is
    something a diagram does by dragging rather than by naming an index.
    """

    op: ClassVar[str] = "add-interface"

    address: str
    #: The interface mapping: at least ``name``, usually ``type``.
    interface: Mapping[str, Any] = field(default_factory=dict)
    #: Where in the list it goes; ``None`` appends.
    index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op": self.op,
            "address": self.address,
            "interface": dict(self.interface),
        }
        if self.index is not None:
            payload["index"] = self.index
        return payload

    def describe(self) -> str:
        return f"add interface {self.interface.get('name', '?')} to {self.address}"


@dataclass(frozen=True)
class RemoveInterface(Operation):
    """Remove an interface, and — with ``cascade`` — whatever terminated on it."""

    op: ClassVar[str] = "remove-interface"

    address: str
    name: str
    cascade: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "address": self.address, "name": self.name}
        if self.cascade:
            payload["cascade"] = True
        return payload

    def describe(self) -> str:
        return f"remove interface {self.name} from {self.address}"


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Connect(Operation):
    """Create a cable between two interfaces.

    A :class:`CreateElement` with a cable spec would do the same thing; this
    exists because joining two ports is the single most common edit there is,
    because the endpoints decide where the document goes, and because the name
    can be derived from them rather than invented by the user.
    """

    op: ClassVar[str] = "connect"

    #: ``device:interface`` for each end.
    a: str
    b: str
    #: The rest of the cable's ``spec``; ``medium`` defaults to ``copper``.
    spec: Mapping[str, Any] = field(default_factory=dict)
    #: ``metadata.name`` of the cable; derived from the endpoints when absent.
    name: str | None = None
    #: Namespace to declare it in; the nearest common one of the two ends when
    #: absent, which is where a patch record belongs.
    namespace: str | None = None
    file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "a": self.a, "b": self.b}
        if self.spec:
            payload["spec"] = dict(self.spec)
        for key in ("name", "namespace", "file"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload

    def describe(self) -> str:
        return f"connect {self.a} to {self.b}"


@dataclass(frozen=True)
class Disconnect(Operation):
    """Remove a cable. The devices it joined are untouched."""

    op: ClassVar[str] = "disconnect"

    address: str

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "address": self.address}

    def describe(self) -> str:
        return f"disconnect {self.address}"


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WriteFile(Operation):
    """Replace a whole file with the given text.

    The inverse of anything whose semantic inverse would not restore the bytes
    exactly. Not something to reach for by hand: it says nothing about *what*
    changed, so a log of these is a log of diffs rather than of intentions.
    """

    op: ClassVar[str] = "write-file"

    path: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "path": self.path, "text": self.text}

    def describe(self) -> str:
        lines = self.text.count("\n")
        return f"write {self.path} ({lines} line{'' if lines == 1 else 's'})"


@dataclass(frozen=True)
class RemoveFile(Operation):
    """Delete a whole file, and the directories that empty out under it."""

    op: ClassVar[str] = "remove-file"

    path: str

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "path": self.path}

    def describe(self) -> str:
        return f"remove {self.path}"


#: Every operation, keyed by its ``op`` discriminator.
OPERATIONS: Final[dict[str, type[Operation]]] = {
    cls.op: cls
    for cls in (
        CreateElement,
        DeleteElement,
        RenameElement,
        MoveElement,
        SetField,
        UnsetField,
        AddInterface,
        RemoveInterface,
        Connect,
        Disconnect,
        WriteFile,
        RemoveFile,
    )
}

#: Which keys each operation takes, and which of them are required. Kept beside
#: the classes rather than derived from them, because a decoder has to reject an
#: unknown key with a message naming the ones it does know -- and because
#: silently ignoring a misspelled key is how a caller's edit quietly does
#: nothing.
_FIELDS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "create": (("kind", "name"), ("namespace", "spec", "metadata", "file")),
    "delete": (("address",), ("cascade",)),
    "rename": (("address", "new_name"), ()),
    "move": (("address", "file"), ("index",)),
    "set": (("address", "path", "value"), ()),
    "unset": (("address", "path"), ()),
    "add-interface": (("address", "interface"), ("index",)),
    "remove-interface": (("address", "name"), ("cascade",)),
    "connect": (("a", "b"), ("spec", "name", "namespace", "file")),
    "disconnect": (("address",), ()),
    "write-file": (("path", "text"), ()),
    "remove-file": (("path",), ()),
}


def operation_from_dict(payload: Any) -> Operation:
    """Build one operation from its JSON form.

    Raises:
        OperationError: ``payload`` is not an object, names no known ``op``,
            leaves out a required key, or carries one that does not belong.
    """
    if not isinstance(payload, Mapping):
        raise OperationError(f"an operation must be a JSON object, got {type(payload).__name__}")
    name = payload.get("op")
    if not isinstance(name, str):
        raise OperationError(
            "an operation must carry an 'op' naming what it does; "
            f"one of {', '.join(sorted(OPERATIONS))}"
        )
    cls = OPERATIONS.get(name)
    if cls is None:
        raise OperationError(
            f"unknown operation {name!r}; expected one of {', '.join(sorted(OPERATIONS))}"
        )
    required, optional = _FIELDS[name]
    given = {key: value for key, value in payload.items() if key != "op"}
    for key in required:
        if key not in given:
            raise OperationError(f"{name}: missing required key {key!r}")
    for key in given:
        if key not in required and key not in optional:
            allowed = ", ".join(required + optional)
            raise OperationError(f"{name}: unknown key {key!r}; it takes {allowed}")
    try:
        return cls(**given)
    except OperationError:
        raise
    except (TypeError, ValueError) as exc:  # pragma: no cover - guarded above
        raise OperationError(f"{name}: {exc}") from exc


def operations_from_json(text: str) -> tuple[Operation, ...]:
    """Parse a JSON document holding one operation or a list of them.

    Both shapes are accepted because both are natural: a script emitting one
    change should not have to wrap it in a list, and a batch should not have to
    be sent one line at a time.

    Raises:
        OperationError: The text is not JSON, or is not operations.
    """
    stripped = text.strip()
    if not stripped:
        raise OperationError("no operations were given")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise OperationError(f"cannot read the operations as JSON: {exc}") from exc
    if isinstance(payload, Mapping):
        payload = [payload]
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise OperationError(
            f"expected an operation or a list of them, got {type(payload).__name__}"
        )
    return tuple(operation_from_dict(entry) for entry in payload)


def operations_to_json(operations: Sequence[Operation], *, indent: int | None = 2) -> str:
    """Render operations as a JSON list, newline-terminated."""
    return (
        json.dumps([operation.to_dict() for operation in operations], indent=indent, default=str)
        + "\n"
    )


def describe_path(path: Sequence[str | int]) -> str:
    """A parsed field path, back in the notation an operation writes it in."""
    return format_field_path(path)
