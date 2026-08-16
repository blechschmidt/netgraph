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

from netviz.edit.errors import OperationError
from netviz.edit.paths import format_field_path, parse_field_path
from netviz.models.element import ANNOTATION_DOCUMENT_KINDS
from netviz.models.layout import LAYOUT_VIEWS, ROUTING_STYLES

__all__ = [
    "OPERATIONS",
    "AddInterface",
    "AppendItem",
    "Connect",
    "CopyElement",
    "CreateAnnotation",
    "CreateElement",
    "DeleteAnnotation",
    "DeleteElement",
    "Disconnect",
    "MoveElement",
    "Operation",
    "RemoveFile",
    "RemoveInterface",
    "RenameElement",
    "SetAnnotation",
    "SetField",
    "SetGeometry",
    "SetLinkGeometry",
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
    annotations. Where the document lands is :mod:`netviz.edit.placement`'s
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
class CopyElement(Operation):
    """Write a second element built from an existing one (``copy``/``duplicate``).

    One element, deliberately. A multi-selection, a namespace and a pasted
    fragment are all *plans* over this operation
    (:func:`~netviz.edit.clipboard.copy_plan`), because deciding which cables
    come along and what each copy is called needs the whole set in view, and
    because keeping the set out of the operation keeps each copy separately
    describable, separately undoable and separately reviewable.

    Three things make a copy something other than "write the document twice":

    * ``metadata.name`` is deduplicated — ``sw1`` becomes ``sw1-copy``, then
      ``sw1-copy-2`` (:func:`~netviz.edit.clipboard.dedupe_name`), unless
      :attr:`name` says otherwise;
    * the fields two elements in one inventory cannot both have are dropped
      (:data:`~netviz.edit.clipboard.UNIQUE_FIELDS`), unless
      :attr:`keep_unique` is set;
    * the references named in :attr:`rewrite` are pointed at the copies instead
      of the originals, which is what makes a copied cable join the copied
      switches rather than reaching back to the ones it was cloned from.

    Everything else — the comments, the key order, the quoting — comes across
    verbatim, because the source document's round-trip tree is what is copied.

    ``duplicate`` is this operation with no :attr:`namespace` and no
    :attr:`name`: the same semantics under the name a diagram editor gives them,
    which is why there is one applier and not two. :mod:`netviz.edit.commands`
    renders it back as ``netviz edit duplicate``.
    """

    op: ClassVar[str] = "copy"

    #: The element to copy.
    address: str
    #: ``metadata.name`` of the copy; derived from the source's when absent.
    name: str | None = None
    #: Namespace to write it into; the source's own when absent.
    namespace: str | None = None
    #: What a derived name gets before its counter.
    suffix: str = "copy"
    #: Keep the values :data:`~netviz.edit.clipboard.UNIQUE_FIELDS` lists.
    keep_unique: bool = False
    #: Fully-qualified name to fully-qualified name: references this element
    #: makes that should point at a copy instead of at the original.
    rewrite: Mapping[str, str] = field(default_factory=dict)
    #: Relative POSIX path to write into, or ``None`` to let placement decide.
    file: str | None = None

    def __post_init__(self) -> None:
        if not self.address:
            raise OperationError("a copy must name what it copies")
        if not self.suffix:
            raise OperationError("a copy suffix cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "address": self.address}
        for key in ("name", "namespace", "file"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.suffix != "copy":
            payload["suffix"] = self.suffix
        if self.keep_unique:
            payload["keep_unique"] = True
        if self.rewrite:
            payload["rewrite"] = dict(self.rewrite)
        return payload

    def describe(self) -> str:
        where = f" into {self.namespace}" if self.namespace else ""
        called = f" as {self.name}" if self.name else ""
        return f"copy {self.address}{called}{where}"


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
class AppendItem(Operation):
    """Add a value to a sequence, creating the sequence when it is absent.

    The general form of the gap :class:`AddInterface` names: a sequence entry
    cannot be written at a path that does not exist yet, so ``set`` cannot add
    one and replacing the whole list would rewrite the comments beside the
    entries that were already there. This adds one entry and leaves the rest of
    the document alone, which is what repairing a device's VLAN database or a
    group's member list needs.

    :class:`AddInterface` stays as it is: it also has to know what an interface
    *is* — where a new port belongs in the list, and what a duplicate name
    means — and none of that is expressible here.
    """

    op: ClassVar[str] = "append"

    address: str
    path: str
    value: Any = None
    #: Where in the sequence it goes; ``None`` appends.
    index: int | None = None

    def __post_init__(self) -> None:
        parse_field_path(self.path)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op": self.op,
            "address": self.address,
            "path": self.path,
            "value": self.value,
        }
        if self.index is not None:
            payload["index"] = self.index
        return payload

    def describe(self) -> str:
        where = "append to" if self.index is None else f"insert at {self.index} of"
        return f"{where} {self.address} {self.path}: {json.dumps(self.value, default=str)}"


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
    """Remove a cable. The devices it joined are untouched.

    ``cascade`` means what it means on :class:`DeleteElement`, and it is here
    for the same reason: a cable is not only a cable. A note may be anchored to
    it, and a note whose only tie to the canvas is that anchor is a document
    §21 refuses without one — so it goes too, or the disconnect is refused. The
    geometry that routed the cable is dropped either way, because a waypoint
    for a link that is gone is not a dependency, it is litter.
    """

    op: ClassVar[str] = "disconnect"

    address: str
    cascade: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "address": self.address}
        if self.cascade:
            payload["cascade"] = True
        return payload

    def describe(self) -> str:
        return f"disconnect {self.address}" + (
            " and everything that needs it" if self.cascade else ""
        )


# --------------------------------------------------------------------------- #
# Diagram geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SetGeometry(Operation):
    """Write one view's geometry into a ``kind: layout`` document (§18).

    A whole view at a time rather than a coordinate at a time, because that is
    the unit an arrangement is decided in: an auto-layout produces every
    position at once, and a prune removes several. A hundred ``set`` operations
    would also be a hundred trips through the round-trip parser for one user
    gesture.

    Each section is *replaced* when given and *left alone* when ``None``, and
    the replacement is a keyed merge: an entry that survives keeps the comment
    somebody wrote above it, an entry that is gone is removed, a new one is
    appended. All three ``None`` clears the view; a document left holding no
    views at all is removed, and so is its file if it held nothing else.

    The layout document is named by :attr:`layout` and :attr:`namespace` rather
    than by an address, because it is not an element: it has its own name space,
    and one may have to be *created*, which no address can name.
    """

    op: ClassVar[str] = "set-geometry"

    #: The view, as ``docs/schema.md`` §18 lists them: ``l1``, ``l3``, ...
    view: str
    #: Address to node geometry. ``None`` leaves the section as it is.
    nodes: Mapping[str, Any] | None = None
    #: Address to edge geometry.
    edges: Mapping[str, Any] | None = None
    #: Namespace to group geometry.
    groups: Mapping[str, Any] | None = None
    #: The view's default routing style, one of
    #: :data:`~netviz.models.layout.ROUTING_STYLES`. ``None`` leaves whatever
    #: the document says alone; the empty string removes it, so that "go back to
    #: splines" is expressible and is not the same request as "do not touch".
    routing: str | None = None
    #: ``metadata.name`` of the layout document to write into or create.
    layout: str = "layout"
    #: The namespace — the folder — the document lives in.
    namespace: str = ""
    #: Where a document that has to be created should land. ``None`` leaves
    #: that to :mod:`netviz.edit.placement`.
    file: str | None = None

    def __post_init__(self) -> None:
        if self.view not in LAYOUT_VIEWS:
            raise OperationError(
                f"unknown view {self.view!r}; expected one of {', '.join(LAYOUT_VIEWS)}"
            )
        if not self.layout:
            raise OperationError("a layout document must be named")
        if self.routing is not None and self.routing != "" and self.routing not in ROUTING_STYLES:
            raise OperationError(
                f"unknown routing style {self.routing!r}; "
                f"expected one of {', '.join(ROUTING_STYLES)}"
            )

    @property
    def clears(self) -> bool:
        """Does this operation drop the view rather than write it?"""
        return (
            self.nodes is None
            and self.edges is None
            and self.groups is None
            and self.routing is None
        )

    @property
    def address(self) -> str:
        """The layout document's fully-qualified name."""
        return f"{self.namespace}/{self.layout}" if self.namespace else self.layout

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "view": self.view}
        for section in ("nodes", "edges", "groups"):
            value = getattr(self, section)
            if value is not None:
                payload[section] = dict(value)
        if self.routing is not None:
            payload["routing"] = self.routing
        payload["layout"] = self.layout
        if self.namespace:
            payload["namespace"] = self.namespace
        if self.file is not None:
            payload["file"] = self.file
        return payload

    def describe(self) -> str:
        if self.clears:
            return f"clear the {self.view} geometry of {self.address}"
        said = [
            f"{len(value)} {section}"
            for section, value in (
                ("nodes", self.nodes),
                ("edges", self.edges),
                ("groups", self.groups),
            )
            if value is not None
        ]
        if self.routing is not None:
            said.append(f"routing {self.routing or 'by default'}")
        return f"set the {self.view} geometry of {self.address} ({', '.join(said)})"


@dataclass(frozen=True)
class SetLinkGeometry(Operation):
    """Write one *link's* geometry: its bends, its routing style, its label.

    One link at a time, which is the opposite unit to :class:`SetGeometry` and
    for the opposite reason. A whole view is what an *automatic layout* decides;
    a route is what a *hand* decides, one cable at a time, and dragging a bend
    must not have to send — and so must not be able to clobber — the coordinates
    of every other thing in the diagram. Two people arranging one diagram is the
    case that makes the difference load-bearing.

    The entry is **replaced**, not merged: what is given is what the link ends
    up saying. So straightening a cable is this operation with no waypoints, and
    a link left with nothing pinned at all has its entry removed, which is what
    keeps a layout document from filling up with keys that say ``{}``.
    """

    op: ClassVar[str] = "set-link-geometry"

    #: The view, as ``docs/schema.md`` §18 lists them.
    view: str
    #: The link's address, spelled as a layout key: a cable's or tunnel's name,
    #: or the synthetic id of a derived edge.
    link: str
    #: The bends, source end first — interior points only, ``[{x, y}, ...]``.
    waypoints: Sequence[Any] = ()
    #: One of :data:`~netviz.models.layout.ROUTING_STYLES`, or ``None`` to
    #: take the view's default.
    routing: str | None = None
    #: ``{"at": 0.5, "offset": {"x": 0, "y": 0}}``, or ``None`` to leave the
    #: label where the renderer puts it.
    label: Mapping[str, Any] | None = None
    #: ``metadata.name`` of the layout document to write into or create.
    layout: str = "layout"
    #: The namespace — the folder — the document lives in.
    namespace: str = ""
    #: Where a document that has to be created should land.
    file: str | None = None

    def __post_init__(self) -> None:
        if self.view not in LAYOUT_VIEWS:
            raise OperationError(
                f"unknown view {self.view!r}; expected one of {', '.join(LAYOUT_VIEWS)}"
            )
        if not self.link:
            raise OperationError("a link geometry must name the link it places")
        if not self.layout:
            raise OperationError("a layout document must be named")
        if self.routing is not None and self.routing not in ROUTING_STYLES:
            raise OperationError(
                f"unknown routing style {self.routing!r}; "
                f"expected one of {', '.join(ROUTING_STYLES)}"
            )

    @property
    def clears(self) -> bool:
        """Does this leave the link with nothing pinned, and so no entry?"""
        return not self.waypoints and self.routing is None and self.label is None

    @property
    def address(self) -> str:
        """The layout document's fully-qualified name."""
        return f"{self.namespace}/{self.layout}" if self.namespace else self.layout

    @property
    def entry(self) -> dict[str, Any]:
        """The document form of what this pins, ready to be merged in."""
        payload: dict[str, Any] = {}
        if self.waypoints:
            payload["waypoints"] = [dict(point) for point in self.waypoints]
        if self.routing is not None:
            payload["routing"] = self.routing
        if self.label is not None:
            payload["label"] = dict(self.label)
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "view": self.view, "link": self.link}
        if self.waypoints:
            payload["waypoints"] = [dict(point) for point in self.waypoints]
        if self.routing is not None:
            payload["routing"] = self.routing
        if self.label is not None:
            payload["label"] = dict(self.label)
        payload["layout"] = self.layout
        if self.namespace:
            payload["namespace"] = self.namespace
        if self.file is not None:
            payload["file"] = self.file
        return payload

    def describe(self) -> str:
        if self.clears:
            return f"straighten {self.link} in the {self.view} view"
        said = []
        if self.waypoints:
            count = len(self.waypoints)
            said.append(f"{count} bend{'' if count == 1 else 's'}")
        if self.routing is not None:
            said.append(self.routing)
        if self.label is not None:
            said.append("label")
        return f"route {self.link} in the {self.view} view ({', '.join(said)})"


# --------------------------------------------------------------------------- #
# Annotations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CreateAnnotation(Operation):
    """Write a new ``note``, ``area`` or ``legend`` document (§21).

    Not :class:`CreateElement` with a different ``kind``, and the reason is the
    name space rather than the shape of the document. An annotation is a
    sidecar: a note called ``core`` may sit beside a switch called ``core``, so
    a create that only carried a name could not say which of the two it meant,
    and — worse — a create that went through the element path would refuse the
    note because the switch already has the name.
    """

    op: ClassVar[str] = "create-annotation"

    #: One of :data:`~netviz.models.ANNOTATION_DOCUMENT_KINDS`.
    kind: str
    name: str
    namespace: str = ""
    spec: Mapping[str, Any] = field(default_factory=dict)
    #: Everything in ``metadata`` besides the name.
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Relative POSIX path to write into, or ``None`` to let placement decide.
    file: str | None = None

    def __post_init__(self) -> None:
        _check_annotation_kind(self.kind)
        if not self.name:
            raise OperationError("an annotation must be named")

    @property
    def address(self) -> str:
        """The annotation's fully-qualified name."""
        return f"{self.namespace}/{self.name}" if self.namespace else self.name

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
class DeleteAnnotation(Operation):
    """Remove an annotation's document, and its file if it was the last one.

    No ``cascade``, and there never will be one: nothing in an inventory refers
    to an annotation, so nothing can be orphaned by removing it. That asymmetry
    with :class:`DeleteElement` is the point of §21 rather than an omission.
    """

    op: ClassVar[str] = "delete-annotation"

    kind: str
    name: str
    namespace: str = ""

    def __post_init__(self) -> None:
        _check_annotation_kind(self.kind)
        if not self.name:
            raise OperationError("an annotation must be named")

    @property
    def address(self) -> str:
        return f"{self.namespace}/{self.name}" if self.namespace else self.name

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "kind": self.kind, "name": self.name}
        if self.namespace:
            payload["namespace"] = self.namespace
        return payload

    def describe(self) -> str:
        return f"delete {self.kind} {self.address}"


@dataclass(frozen=True)
class SetAnnotation(Operation):
    """Set — or remove — one field of an annotation.

    This is the operation a dragged note is made of: ``spec.geometry.x``, then
    ``spec.geometry.y``. The mappings on the way are created, so the first drag
    of a note that has never been placed writes a whole ``geometry`` block
    rather than refusing; that is the difference between an annotation and an
    element, whose ``spec`` shape is fixed by its kind.

    ``unset`` removes the key rather than writing null, which is
    :class:`UnsetField`'s distinction and is here for the same reason: a note
    with ``color: null`` is not a note with no colour.
    """

    op: ClassVar[str] = "set-annotation"

    kind: str
    name: str
    namespace: str = ""
    #: The field path, in the notation :func:`~netviz.edit.paths.parse_field_path`
    #: reads — ``spec.geometry.x``, ``spec.members[0]``.
    path: str = ""
    value: Any = None
    unset: bool = False

    def __post_init__(self) -> None:
        _check_annotation_kind(self.kind)
        if not self.name:
            raise OperationError("an annotation must be named")
        if not self.path:
            raise OperationError("a set-annotation must name the field it changes")
        parse_field_path(self.path)
        if self.unset and self.value is not None:
            raise OperationError("an unset removes the key; it cannot also carry a value")

    @property
    def address(self) -> str:
        return f"{self.namespace}/{self.name}" if self.namespace else self.name

    @property
    def parsed_path(self) -> tuple[str | int, ...]:
        """The field path, split."""
        return parse_field_path(self.path)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op": self.op,
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
        }
        if self.namespace:
            payload["namespace"] = self.namespace
        if self.unset:
            payload["unset"] = True
        else:
            payload["value"] = self.value
        return payload

    def describe(self) -> str:
        what = f"{self.kind} {self.address}"
        if self.unset:
            return f"unset {self.path} of {what}"
        return f"set {self.path} of {what} to {json.dumps(self.value, default=str)}"


def _check_annotation_kind(kind: str) -> None:
    """Refuse anything that is not one of the three annotation kinds."""
    if kind not in ANNOTATION_DOCUMENT_KINDS:
        raise OperationError(
            f"unknown annotation kind {kind!r}; "
            f"expected one of {', '.join(ANNOTATION_DOCUMENT_KINDS)}"
        )


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
        CopyElement,
        DeleteElement,
        RenameElement,
        MoveElement,
        SetField,
        UnsetField,
        AppendItem,
        AddInterface,
        RemoveInterface,
        Connect,
        Disconnect,
        SetGeometry,
        SetLinkGeometry,
        CreateAnnotation,
        DeleteAnnotation,
        SetAnnotation,
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
    "copy": (
        ("address",),
        ("name", "namespace", "suffix", "keep_unique", "rewrite", "file"),
    ),
    "delete": (("address",), ("cascade",)),
    "rename": (("address", "new_name"), ()),
    "move": (("address", "file"), ("index",)),
    "set": (("address", "path", "value"), ()),
    "unset": (("address", "path"), ()),
    "append": (("address", "path", "value"), ("index",)),
    "add-interface": (("address", "interface"), ("index",)),
    "remove-interface": (("address", "name"), ("cascade",)),
    "connect": (("a", "b"), ("spec", "name", "namespace", "file")),
    "disconnect": (("address",), ("cascade",)),
    "set-geometry": (
        ("view",),
        ("nodes", "edges", "groups", "routing", "layout", "namespace", "file"),
    ),
    "set-link-geometry": (
        ("view", "link"),
        ("waypoints", "routing", "label", "layout", "namespace", "file"),
    ),
    "create-annotation": (("kind", "name"), ("namespace", "spec", "metadata", "file")),
    "delete-annotation": (("kind", "name"), ("namespace",)),
    "set-annotation": (("kind", "name", "path"), ("namespace", "value", "unset")),
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
