"""What a copy *is*: the name, the fields that cannot come along, and the links.

Copy, cut, paste and duplicate are one question asked four ways, and none of the
four is "write the same document twice". A second document carrying the same
``metadata.name`` does not load; a second document carrying the same MAC address
loads and is wrong. So a copy is three decisions, and this module makes all three
in one place so that the browser, the command line and a script agree about them:

**The name.** ``sw1`` becomes ``sw1-copy``, then ``sw1-copy-2``, ``sw1-copy-3``.
The series is per *family* rather than per document: copying ``sw1-copy`` gives
``sw1-copy-2`` and not ``sw1-copy-copy``, because the second is what a tool that
has stopped thinking produces. The suffix is configurable (``--suffix``) for the
inventory whose convention is ``-b`` or ``-standby``.

**The fields that must be unique.** :data:`UNIQUE_FIELDS` is the table, one row
per field, each naming the rule that would fail if the copy kept it. It is
deliberately *not* "everything that looks like an identifier": a copy that lost
its model number, its VLAN database and its description would not be a copy of
anything. What goes is what two elements in one inventory cannot both have.

**The links.** A cable is not a property of a switch, it is an element joining
two of them, so copying a switch cannot copy its cables — there is nothing at the
far end. Copying a *set* is different: if both ends of a cable are in the set the
cable is part of the shape being copied, and the copy is rewired to the copies.
If only one end is, the cable is dropped and said so, because a cable rewired to
one clone and one original is a claim about the network nobody made.

The unit
--------

:class:`~netgraph.edit.operations.CopyElement` is one element. Everything larger
— a multi-selection, a namespace, a payload from another window — is planned
here into a list of those, in an order where each one's references already
resolve: elements first, then the links between them. That keeps the *semantics*
in the applier (:func:`netgraph.edit.apply._copy`) and the *arithmetic* here,
which is the same split :mod:`netgraph.edit.arrange` makes.

Between two windows
-------------------

:func:`clipboard_payload` serialises a selection as JSON — the documents
themselves, their namespaces, and where the view places them. That is what goes
on the system clipboard, so a fragment can be pasted into another inventory, or
into a text editor, where it is a perfectly ordinary multi-document YAML
inventory in JSON clothing. :func:`paste_plan` reads one back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Final

from netgraph.edit.errors import EditError, OperationError
from netgraph.edit.operations import CopyElement, CreateElement, Operation, SetGeometry
from netgraph.edit.references import NameIndex, reference_text, references_of, rewrite_reference
from netgraph.errors import SchemaError
from netgraph.layout.document import inline_entry
from netgraph.layout.geometry import DEFAULT_GRID, Placement
from netgraph.layout.resolve import resolve_key
from netgraph.loader.inventory import Inventory, namespace_of, qualify, short_name
from netgraph.models import API_VERSION, Cable, Element, Tunnel, parse_document
from netgraph.models.scalars import ELEMENT_NAME_PATTERN

__all__ = [
    "CLIPBOARD_FORMAT",
    "DEFAULT_SUFFIX",
    "MAX_COPIES",
    "UNIQUE_FIELDS",
    "CopyPlan",
    "Dropped",
    "UniqueField",
    "clipboard_payload",
    "copy_plan",
    "dedupe_name",
    "paste_plan",
    "strip_unique",
    "unique_fields_markdown",
]

#: What a copied name gets when nobody says otherwise.
DEFAULT_SUFFIX: Final = "copy"

#: The ``format`` a serialised clipboard carries. Version it here rather than
#: sniffing the shape: a payload from a newer netgraph should be refused with a
#: sentence, not silently half-understood.
CLIPBOARD_FORMAT: Final = "netgraph.dev/clipboard/v1"

#: How many elements one copy may produce. A guard rather than a policy — the
#: same one :mod:`netgraph.edit.arrange` sets on an arrangement — because a
#: mis-aimed "copy this site" on a ten-thousand-device tree should come back as
#: a sentence rather than as a minute of writing.
MAX_COPIES: Final = 2000

#: Highest number the name ladder climbs to before giving up.
_MAX_SERIAL: Final = 1000

_NAME_RE: Final = re.compile(ELEMENT_NAME_PATTERN)


# --------------------------------------------------------------------------- #
# The fields a copy cannot keep
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class UniqueField:
    """One field a copy has to drop, and the rule that says so.

    ``path`` is read against the raw document, with ``*`` standing for "every
    entry of this sequence" — so ``spec.interfaces.*.mac`` is every port's MAC
    and not the first one's.
    """

    #: The field path, ``*`` meaning every entry of a sequence.
    path: tuple[str, ...]
    #: The ``NG-*`` rule a copy that kept it would break, or ``""`` when the
    #: reason is physical rather than checkable.
    rule: str
    #: Why it cannot be shared, in one clause.
    why: str

    @property
    def spelling(self) -> str:
        """The path as a reader writes it: ``spec.interfaces[].mac``."""
        return ".".join("[]" if part == "*" else part for part in self.path).replace(".[]", "[]")


#: Every field a copy drops, in document order.
#:
#: The test is not "is this an identifier" but "would two elements in one
#: inventory both having it be a defect netgraph reports". Everything else —
#: vendor, model, MTU, VLAN database, description, labels, style, routes — comes
#: across, because a copy that lost them would not be a copy worth having.
#:
#: ``--keep-unique`` turns the whole table off, for the case where the copy is
#: about to be edited into a different machine anyway and the values are wanted
#: as a starting point.
UNIQUE_FIELDS: Final[tuple[UniqueField, ...]] = (
    UniqueField(
        path=("metadata", "location", "position"),
        rule="",
        why="two things cannot be bolted into the same rack unit",
    ),
    UniqueField(
        path=("spec", "serial"),
        rule="",
        why="a serial number names one physical unit",
    ),
    UniqueField(
        path=("spec", "label"),
        rule="",
        why="the identifier printed on a cable is on exactly one cable",
    ),
    UniqueField(
        path=("spec", "login"),
        rule="NG-S013",
        why="two accounts cannot share a login",
    ),
    UniqueField(path=("spec", "uid"), rule="NG-S013", why="two users cannot share a uid"),
    UniqueField(path=("spec", "gid"), rule="NG-S013", why="two groups cannot share a gid"),
    UniqueField(
        path=("spec", "bridge", "address"),
        rule="",
        why="a bridge address is one bridge component's own MAC address",
    ),
    UniqueField(
        path=("spec", "interfaces", "*", "mac"),
        rule="NG-I008",
        why="a MAC address is unique across the inventory",
    ),
    UniqueField(
        path=("spec", "interfaces", "*", "ipv4", "addresses"),
        rule="NG-A004",
        why="a fixed address is claimed by one interface in its subnet",
    ),
    UniqueField(
        path=("spec", "interfaces", "*", "ipv6", "addresses"),
        rule="NG-A004",
        why="a fixed address is claimed by one interface in its subnet",
    ),
    UniqueField(
        path=("spec", "interfaces", "*", "wireless", "bss", "*", "bssid"),
        rule="NG-W008",
        why="a BSSID is one radio's own MAC address",
    ),
    UniqueField(
        path=("spec", "power", "inputs"),
        rule="NG-E010",
        why="one PDU outlet feeds one power supply",
    ),
    UniqueField(
        path=("spec", "routing", "ospf", "router_id"),
        rule="NG-F012",
        why="two routers cannot share a router id",
    ),
    UniqueField(
        path=("spec", "routing", "bgp", "router_id"),
        rule="NG-F012",
        why="two routers cannot share a router id",
    ),
)


def unique_fields_markdown() -> str:
    """:data:`UNIQUE_FIELDS` as a Markdown table, for ``docs/editing.md``."""
    lines = ["| Field | Rule | Why a copy cannot keep it |", "|---|---|---|"]
    for entry in UNIQUE_FIELDS:
        rule = f"`{entry.rule}`" if entry.rule else "—"
        lines.append(f"| `{entry.spelling}` | {rule} | {entry.why} |")
    return "\n".join(lines) + "\n"


def strip_unique(document: MutableMapping[str, Any]) -> tuple[str, ...]:
    """Remove every :data:`UNIQUE_FIELDS` entry from ``document``, in place.

    Works on a plain mapping and on a ``ruamel`` round-trip tree alike, which is
    what lets one implementation serve both the copy that keeps the original's
    comments and the paste that rebuilds a document from JSON.

    Returns:
        The paths actually removed, in table order, for the report.
    """
    removed: list[str] = []
    for entry in UNIQUE_FIELDS:
        if _remove_at(document, entry.path):
            removed.append(entry.spelling)
    _tidy(document, set(removed))
    return tuple(removed)


def _remove_at(node: Any, path: Sequence[str]) -> bool:
    """Delete everything ``path`` names below ``node``. True if anything went."""
    if not path:  # pragma: no cover - every table entry names a field
        return False
    head, rest = path[0], path[1:]
    if head == "*":
        if not isinstance(node, list):
            return False
        # Every entry, not the first one that matches: ``any`` over a generator
        # would stop at the first port that had a MAC and leave the rest of them.
        found = False
        for item in node:
            found = _remove_at(item, rest) or found
        return found
    if not isinstance(node, MutableMapping) or head not in node:
        return False
    if not rest:
        del node[head]
        return True
    return _remove_at(node[head], rest)


def _tidy(document: MutableMapping[str, Any], removed: set[str]) -> None:
    """Remove what a stripped field leaves behind that means nothing alone.

    Every one of these is a value that was *true because of* the value that has
    just gone, so leaving it is not conservative — it is asserting something the
    copy no longer supports, and the validator says so:

    * ``spec.power.redundant`` claims the device survives losing a feed, which
      is an assertion about feeds that are no longer written (``NG-E002``);
    * ``ipv4.gateway`` is a first hop, and a first hop is resolved by neighbour
      discovery — with no address on the interface there is no on-link prefix
      for it to be in, which is ``E020``;
    * an address family container left holding only ``enabled: true`` says
      exactly what its absence says.

    Guarded on what was actually removed, so a document that arrived with a
    gateway and no address keeps both: that is a defect the *source* has, and a
    copy is not the place to quietly repair it.
    """
    spec = document.get("spec")
    if not isinstance(spec, MutableMapping):
        return
    power = spec.get("power")
    if "spec.power.inputs" in removed and isinstance(power, MutableMapping):
        power.pop("redundant", None)
        if not power:
            spec.pop("power", None)
    interfaces = spec.get("interfaces")
    for interface in interfaces if isinstance(interfaces, list) else ():
        if not isinstance(interface, MutableMapping):  # pragma: no cover - schema says mapping
            continue
        for family in ("ipv4", "ipv6"):
            if f"spec.interfaces[].{family}.addresses" not in removed:
                continue
            block = interface.get(family)
            if not isinstance(block, MutableMapping) or "addresses" in block:
                continue
            block.pop("gateway", None)
            if not set(block) - {"enabled"}:
                interface.pop(family, None)


# --------------------------------------------------------------------------- #
# The name
# --------------------------------------------------------------------------- #


def dedupe_name(base: str, taken: Iterable[str], *, suffix: str = DEFAULT_SUFFIX) -> str:
    """A free name for a copy of something called ``base``.

    ``sw1`` gives ``sw1-copy``; with that taken, ``sw1-copy-2``, then
    ``sw1-copy-3``. A ``base`` that is *itself* a copy re-joins the series it
    came from rather than starting a nested one, so copying ``sw1-copy`` gives
    ``sw1-copy-2``.

    Args:
        base: ``metadata.name`` of the original.
        taken: Names already used in the namespace the copy lands in.
        suffix: What is appended before the counter.

    Raises:
        OperationError: The suffix would not make a legal name (§4.1), or a
            thousand copies of one element already exist.
    """
    if not suffix or not _NAME_RE.match(suffix):
        raise OperationError(
            f"{suffix!r} is not a usable copy suffix; it has to be a name segment "
            f"({ELEMENT_NAME_PATTERN})"
        )
    used = set(taken)
    stem = _family(base, suffix)
    candidate = f"{stem}-{suffix}"
    if candidate not in used and _NAME_RE.match(candidate):
        return candidate
    for serial in range(2, _MAX_SERIAL):
        candidate = f"{stem}-{suffix}-{serial}"
        if candidate not in used and _NAME_RE.match(candidate):
            return candidate
    raise OperationError(  # pragma: no cover - a thousand copies of one element
        f"cannot find a free name for a copy of {base!r}; give one with --name"
    )


def _family(base: str, suffix: str) -> str:
    """``base`` with a trailing ``-suffix`` or ``-suffix-N`` taken back off."""
    stripped = re.sub(rf"-{re.escape(suffix)}(-\d+)?$", "", base)
    return stripped or base


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Dropped:
    """Something the copy deliberately left behind, and why."""

    address: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"address": self.address, "reason": self.reason}

    def __str__(self) -> str:
        return f"{self.address}: {self.reason}"


@dataclass(frozen=True, slots=True)
class CopyPlan:
    """What a copy would do: the operations, the names, and what was dropped."""

    operations: tuple[Operation, ...]
    #: Source fully-qualified name to the copy's, in the order they are written.
    mapping: Mapping[str, str]
    #: Links that could not come along, each with the reason.
    dropped: tuple[Dropped, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.operations)

    @property
    def copies(self) -> tuple[str, ...]:
        """The fully-qualified names the copy creates, in order."""
        return tuple(self.mapping.values())

    def describe(self) -> str:
        """One line naming the whole plan, for an undo stack and a log."""
        count = len(self.mapping)
        if count == 1:
            return f"copy {next(iter(self.mapping))} to {next(iter(self.mapping.values()))}"
        return f"copy {count} elements"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations": [operation.to_dict() for operation in self.operations],
            "mapping": dict(self.mapping),
            "dropped": [entry.to_dict() for entry in self.dropped],
        }


@dataclass(frozen=True, slots=True)
class _Source:
    """One thing being copied: where it is now, and where it is going."""

    fqn: str
    element: Element
    namespace: str
    name: str

    @property
    def target(self) -> str:
        return qualify(self.namespace, self.name)


@dataclass(frozen=True, slots=True)
class _Placed:
    """One node's stored position and the layout document that holds it."""

    layout: str
    key: str
    placement: Placement


@dataclass
class _Anchor:
    """Where a pasted fragment lands: an offset, or a point to centre it on."""

    offset: tuple[float, float] = (DEFAULT_GRID, -DEFAULT_GRID)
    at: tuple[float, float] | None = None
    _sources: list[Placement] = field(default_factory=list, repr=False)

    def resolve(self, placements: Sequence[Placement]) -> tuple[float, float]:
        """The translation to apply to every copied node.

        A plain offset when nobody named a point, and otherwise whatever moves
        the centre of the fragment's bounding box onto that point — so pasting
        at the pointer puts the fragment *under the pointer* rather than putting
        its first element there and the rest wherever they happened to be.
        """
        if self.at is None or not placements:
            return self.offset
        xs = [placement.x for placement in placements]
        ys = [placement.y for placement in placements]
        centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        return (self.at[0] - centre[0], self.at[1] - centre[1])


def copy_plan(
    inventory: Inventory,
    addresses: Sequence[str],
    *,
    namespace: str | None = None,
    name: str | None = None,
    suffix: str = DEFAULT_SUFFIX,
    keep_unique: bool = False,
    view: str | None = None,
    offset: tuple[float, float] | None = None,
    at: tuple[float, float] | None = None,
) -> CopyPlan:
    """The operations that copy ``addresses`` within one inventory.

    Args:
        inventory: The loaded tree.
        addresses: Elements, and namespaces — a namespace copies its subtree.
        namespace: Where the copies go. ``None`` keeps each source's own, which
            is what a duplicate means.
        name: ``metadata.name`` of the copy. Only legal for a single element,
            because two elements cannot both be called it.
        suffix: What a derived name gets before its counter.
        keep_unique: Keep the :data:`UNIQUE_FIELDS` values rather than dropping
            them. The result is very likely to be refused by the validation
            gate; it exists for the copy that is about to be edited.
        view: The layer whose geometry the copies are placed in. ``None`` writes
            no geometry at all, which is what a scripted copy wants.
        offset: How far the copies sit from the originals, in points.
        at: A point to centre the copied fragment on instead.

    Returns:
        The plan. Empty operations means there was nothing to copy.

    Raises:
        EditError: Nothing was named, something named does not exist, or the
            selection is larger than :data:`MAX_COPIES`.
        OperationError: ``name`` was given for more than one element, or the
            suffix cannot make a legal name.
    """
    sources = _resolve(inventory, addresses)
    if not sources:
        raise EditError("nothing was named to copy")
    if len(sources) > MAX_COPIES:
        raise EditError(
            f"{len(sources)} elements is more than one copy should make at once "
            f"(the limit is {MAX_COPIES})"
        )
    if name is not None and len(sources) > 1:
        raise OperationError(
            f"--name gives one copy one name, but {len(sources)} elements were named; "
            f"copy them one at a time, or let the names be derived"
        )

    roots = _namespace_roots(inventory, addresses)
    if namespace is not None:
        # Whatever the *selection* has in common is its implicit root, so a set
        # spanning ``cables/`` and ``switches/`` keeps that shape below the
        # target rather than being flattened into one folder. Explicit namespace
        # roots are tried first, since naming one says what the shape is.
        roots = (*roots, _common_namespace([namespace_of(one.fqn) for one in sources]))
    chosen = _choose_names(
        inventory,
        sources,
        namespace=namespace,
        name=name,
        suffix=suffix,
        roots=roots,
    )
    index = NameIndex(inventory.elements)
    mapping = {source.fqn: source.target for source in chosen}
    kept, dropped = _partition_links(chosen, mapping, index=index)

    operations: list[Operation] = []
    for source in kept:
        rewrite = _rewrite_for(source, mapping, index=index)
        operations.append(
            CopyElement(
                address=source.fqn,
                name=source.name,
                namespace=source.namespace,
                suffix=suffix,
                keep_unique=keep_unique,
                rewrite=rewrite,
            )
        )
    written = {source.fqn: source.target for source in kept}
    if view:
        operations.extend(
            _geometry_operations(
                inventory,
                view=view,
                placed=_placements(inventory, view, written),
                mapping=written,
                anchor=_Anchor(offset=offset or (DEFAULT_GRID, -DEFAULT_GRID), at=at),
            )
        )
    return CopyPlan(operations=tuple(operations), mapping=written, dropped=dropped)


def _resolve(inventory: Inventory, addresses: Sequence[str]) -> tuple[_Source, ...]:
    """Every element ``addresses`` names, namespaces expanded to their subtrees.

    Order is the inventory's rather than the caller's, because that is the order
    the documents were loaded in and therefore the order a reviewer reads the
    diff in. Duplicates collapse: selecting a site *and* a switch inside it
    copies the switch once.

    Raises:
        EditError: One of the addresses names neither an element nor a namespace.
    """
    wanted: dict[str, None] = {}
    for address in addresses:
        text = str(address).strip()
        if not text:
            continue
        resolution = inventory.lookup(text)
        if resolution.ambiguous:
            raise EditError(
                f"{text!r} is ambiguous; it could mean {', '.join(resolution.ambiguous)}. "
                f"Write the fully-qualified name."
            )
        if resolution.fqn is not None:
            wanted.setdefault(resolution.fqn, None)
            continue
        members = _members_of(inventory, text)
        if members is None:
            raise EditError(f"there is no element or namespace called {text!r} in this inventory")
        for fqn in members:
            wanted.setdefault(fqn, None)
    return tuple(
        _Source(fqn=fqn, element=inventory.elements[fqn], namespace=namespace_of(fqn), name="")
        for fqn in inventory.elements
        if fqn in wanted
    )


def _members_of(inventory: Inventory, namespace: str) -> tuple[str, ...] | None:
    """Every element below ``namespace``, or ``None`` if there is no such folder.

    "Below", not "in": ``sites/hq`` names a folder even when every element is a
    level further down in ``sites/hq/access``, and copying a site has to mean
    the site. That is also why this does not consult
    :attr:`~netgraph.loader.Inventory.namespaces`, which lists the namespaces
    elements are *declared* in rather than every folder on the way to them.
    """
    text = namespace.strip("/")
    if not text:
        return None
    prefix = f"{text}/"
    found = tuple(fqn for fqn in inventory.elements if fqn.startswith(prefix))
    return found or None


def _namespace_roots(inventory: Inventory, addresses: Sequence[str]) -> tuple[str, ...]:
    """The namespaces among ``addresses``, longest first.

    A copy of ``sites/hq`` moves ``sites/hq/racks/r1/sw1`` to
    ``<target>/racks/r1/sw1``: the subtree keeps its own shape and only its root
    is renamed. Longest first so a nested pair is matched by the inner one.
    """
    found = [
        address.strip().strip("/")
        for address in addresses
        if address.strip()
        and inventory.lookup(address.strip()).fqn is None
        and _members_of(inventory, address.strip()) is not None
    ]
    return tuple(sorted(dict.fromkeys(found), key=len, reverse=True))


def _choose_names(
    inventory: Inventory,
    sources: Sequence[_Source],
    *,
    namespace: str | None,
    name: str | None,
    suffix: str,
    roots: Sequence[str],
) -> tuple[_Source, ...]:
    """Where each copy lands and what it is called.

    Three rules, in order:

    * an explicit ``--name`` is used as given, and refused if it is taken;
    * a copy that lands in a *different* namespace keeps its name when that
      name is free there, because "the same switch, in the lab folder" is what
      copying to a folder means;
    * otherwise the name is derived (:func:`dedupe_name`).

    Names allocated earlier in the same plan count as taken, so copying eleven
    switches at once produces eleven different names rather than eleven attempts
    at the first one.
    """
    taken = set(inventory.elements)
    chosen: list[_Source] = []
    for source in sources:
        target_namespace = _target_namespace(source.namespace, namespace=namespace, roots=roots)
        if name is not None:
            if not _NAME_RE.match(name):
                raise OperationError(
                    f"{name!r} is not a legal element name ({ELEMENT_NAME_PATTERN})"
                )
            chosen_name = name
            if qualify(target_namespace, chosen_name) in taken:
                raise EditError(
                    f"{qualify(target_namespace, chosen_name)} already exists; "
                    f"a name is unique within its namespace (NG-N002)"
                )
        else:
            here = short_name(source.fqn)
            siblings = {short_name(fqn) for fqn in taken if namespace_of(fqn) == target_namespace}
            chosen_name = (
                here
                if target_namespace != source.namespace and here not in siblings
                else dedupe_name(here, siblings, suffix=suffix)
            )
        taken.add(qualify(target_namespace, chosen_name))
        chosen.append(
            _Source(
                fqn=source.fqn,
                element=source.element,
                namespace=target_namespace,
                name=chosen_name,
            )
        )
    return tuple(chosen)


def _below(base: str, rest: str) -> str:
    """``rest`` written under ``base``, with either of them possibly empty.

    Not :func:`~netgraph.loader.inventory.qualify`, which joins a namespace to a
    *name* and so never has to think about an empty second half. Here it is a
    namespace under a namespace, and "the root of the fragment" is spelled
    ``""`` — so joining naively produces ``devices/``, which is a namespace
    nothing is in and a bug that only shows up as a duplicate name.
    """
    if not base:
        return rest
    return f"{base}/{rest}" if rest else base


def _target_namespace(current: str, *, namespace: str | None, roots: Sequence[str]) -> str:
    """Where an element whose namespace is ``current`` lands."""
    if namespace is None:
        return current
    for root in roots:
        if not root:
            # The selection spans the whole tree: every namespace is below the
            # implicit root, so every one of them is kept, under the target.
            return _below(namespace, current)
        if current == root:
            return namespace
        if current.startswith(f"{root}/"):
            return _below(namespace, current[len(root) + 1 :])
    return namespace


def _partition_links(
    sources: Sequence[_Source], mapping: Mapping[str, str], *, index: NameIndex
) -> tuple[tuple[_Source, ...], tuple[Dropped, ...]]:
    """Split the selection into what is copied and what is dropped.

    Elements first, then the links whose *both* ends are being copied — which is
    both the order the operations have to be applied in (a cable copy resolves
    its new endpoints against the tree the element copies left) and the honest
    reading of what a fragment is.
    """
    elements = [source for source in sources if not isinstance(source.element, (Cable, Tunnel))]
    kept: list[_Source] = []
    dropped: list[Dropped] = []
    for source in sources:
        if not isinstance(source.element, (Cable, Tunnel)):
            continue
        ends = _endpoints(source, index=index)
        outside = [end for end in ends if end not in mapping]
        if outside:
            dropped.append(
                Dropped(
                    address=source.fqn,
                    reason=(
                        f"only one end is in the selection ({', '.join(sorted(outside))} is not), "
                        f"so a copy would join a clone to the original"
                        if len(outside) < len(ends)
                        else "neither end is in the selection"
                    ),
                )
            )
            continue
        kept.append(source)
    return (*elements, *kept), tuple(dropped)


def _endpoints(source: _Source, *, index: NameIndex) -> tuple[str, ...]:
    """The fully-qualified devices a link terminates on.

    Resolved the loader's way (:class:`~netgraph.edit.references.NameIndex`), so
    a cable that names ``sw1`` from inside ``sites/hq`` is understood to mean
    the same switch the picture draws it against.
    """
    element = source.element
    if not isinstance(element, (Cable, Tunnel)):  # pragma: no cover - guarded by the caller
        return ()
    # ``source.namespace`` is where the copy is *going*; a reference is resolved
    # from where the document *is*, which is the namespace of its own name.
    here = namespace_of(source.fqn)
    return tuple(
        index.lookup(endpoint.device, here) or endpoint.device
        for endpoint in element.spec.endpoints
    )


def _rewrite_for(
    source: _Source, mapping: Mapping[str, str], *, index: NameIndex
) -> dict[str, str]:
    """The subset of ``mapping`` this element actually points at.

    Sending the whole map with every operation would work and would put a
    hundred-entry table in a hundred JSON objects; a copy should carry the
    references it makes and nothing else.
    """
    wanted: dict[str, str] = {}
    here = namespace_of(source.fqn)
    for reference in references_of(source.fqn, source.element):
        target = index.lookup(reference.target, here)
        if target is not None and target in mapping:
            wanted[target] = mapping[target]
    return wanted


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _placements(inventory: Inventory, view: str, wanted: Mapping[str, str]) -> dict[str, _Placed]:
    """Where ``view`` places each of ``wanted``, and in which document.

    First document wins, the same rule
    :func:`~netgraph.layout.resolve.resolve_geometry` applies when it merges the
    arrangement the renderer draws — this must not disagree with the picture
    about which entry the reader is looking at.
    """
    found: dict[str, _Placed] = {}
    for fqn, layout in inventory.layouts.items():
        geometry = layout.view(view)
        if geometry is None:
            continue
        namespace = namespace_of(fqn)
        for key, node in geometry.nodes.items():
            resolved = resolve_key(key, inventory=inventory, namespace=namespace)
            if resolved in wanted:
                found.setdefault(
                    resolved, _Placed(layout=fqn, key=key, placement=Placement.from_model(node))
                )
    return found


def _geometry_operations(
    inventory: Inventory,
    *,
    view: str,
    placed: Mapping[str, _Placed],
    mapping: Mapping[str, str],
    anchor: _Anchor,
    fallback: str = "",
    default_layout: str = "",
) -> tuple[Operation, ...]:
    """One ``set-geometry`` per layout document that gains an entry.

    The copies are written into the *same* document the originals are placed in,
    which is what keeps a site's arrangement in the site's own file. A fragment
    whose originals are placed nowhere as far as *this* tree is concerned — a
    paste from another window — goes into the document ``default_layout`` names,
    and into a new one in ``fallback`` when the tree has no layout at all.

    Every operation carries the document's whole ``nodes`` section for the view,
    because :class:`~netgraph.edit.operations.SetGeometry` replaces a section
    rather than merging into it. The replacement is itself a keyed merge, so the
    entries that were already there keep their comments and their spelling —
    which is also why leaving one out would *delete* it.
    """
    if not placed:
        return ()
    translation = anchor.resolve([entry.placement for entry in placed.values()])
    additions: dict[str, dict[str, Any]] = {}
    for source, entry in placed.items():
        target = mapping.get(source)
        if target is None:  # pragma: no cover - placed is built from the mapping
            continue
        moved = Placement(
            x=entry.placement.x + translation[0],
            y=entry.placement.y + translation[1],
            width=entry.placement.width,
            height=entry.placement.height,
        )
        holder = entry.layout or default_layout
        additions.setdefault(holder, {})[target] = moved.to_model().model_dump(exclude_none=True)

    operations: list[Operation] = []
    for fqn, layout in inventory.layouts.items():
        wanted = additions.pop(fqn, None)
        geometry = layout.view(view)
        if not wanted:
            continue
        namespace = namespace_of(fqn)
        nodes: dict[str, Any] = {}
        if geometry is not None:
            for key, node in geometry.nodes.items():
                nodes[key] = inline_entry(
                    Placement.from_model(node).to_model().model_dump(exclude_none=True)
                )
        for target, entry in wanted.items():
            nodes[_layout_key(target, namespace)] = inline_entry(entry)
        operations.append(
            SetGeometry(view=view, nodes=nodes, layout=short_name(fqn), namespace=namespace)
        )
    # Whatever is left names a document that does not exist yet, which is the
    # paste-into-an-unarranged-tree case: one new layout document holds it.
    for wanted in additions.values():
        operations.append(
            SetGeometry(
                view=view,
                nodes={
                    _layout_key(target, fallback): inline_entry(entry)
                    for target, entry in wanted.items()
                },
                layout="layout",
                namespace=fallback,
            )
        )
    return tuple(operations)


def _default_layout(inventory: Inventory, view: str, namespace: str) -> str:
    """Which layout document a fragment with no local arrangement lands in.

    The one in the namespace it is being pasted into, if there is one, and
    otherwise the first document that already places anything in this view — so
    a paste joins the arrangement the tree already has rather than starting a
    rival one beside it. ``""`` means the tree has no layout document at all.
    """
    placing = [fqn for fqn, layout in inventory.layouts.items() if layout.view(view) is not None]
    for candidates in (placing, list(inventory.layouts)):
        here = [fqn for fqn in candidates if namespace_of(fqn) == namespace]
        if here:
            return here[0]
        if candidates:
            return candidates[0]
    return ""


def _layout_key(fqn: str, namespace: str) -> str:
    """How a layout document in ``namespace`` should spell ``fqn``.

    Relative when it is below the document's own folder, which is how a
    hand-written layout reads, and fully qualified otherwise — never a bare
    short name, which would resolve outwards and could find something else.
    """
    if not namespace:
        return fqn
    prefix = f"{namespace}/"
    return fqn[len(prefix) :] if fqn.startswith(prefix) else fqn


# --------------------------------------------------------------------------- #
# The serialised clipboard
# --------------------------------------------------------------------------- #


def clipboard_payload(
    inventory: Inventory, addresses: Sequence[str], *, view: str | None = None
) -> dict[str, Any]:
    """A selection, serialised for the system clipboard.

    JSON rather than YAML because that is what a browser can put on a clipboard
    and read back without a parser, and because a payload that lands in a text
    editor by accident should look like data rather than like a broken
    inventory. What is *in* it is the documents themselves — so pasting into
    another netgraph window rebuilds exactly these elements, links included.

    Raises:
        EditError: Nothing was named, or something named does not exist.
    """
    sources = _resolve(inventory, addresses)
    if not sources:
        raise EditError("nothing was named to copy")
    mapping = {source.fqn: source.fqn for source in sources}
    kept, dropped = _partition_links(sources, mapping, index=NameIndex(inventory.elements))
    root = _common_namespace([source.namespace for source in kept])
    documents = []
    for source in kept:
        documents.append(
            {
                "address": source.fqn,
                "namespace": _relative_namespace(source.namespace, root),
                "document": _document_of(source),
            }
        )
    payload: dict[str, Any] = {
        "format": CLIPBOARD_FORMAT,
        "root": root,
        "documents": documents,
        "dropped": [entry.to_dict() for entry in dropped],
    }
    if view:
        payload["view"] = view
        payload["geometry"] = {
            source: {
                "position": {"x": entry.placement.x, "y": entry.placement.y},
                **(
                    {"size": {"width": entry.placement.width, "height": entry.placement.height}}
                    if entry.placement.width is not None and entry.placement.height is not None
                    else {}
                ),
            }
            for source, entry in _placements(inventory, view, mapping).items()
        }
    return payload


def _document_of(source: _Source) -> dict[str, Any]:
    """One element as the document it would be written as.

    Round-tripped through the *model* rather than re-read from the file, and
    that choice carries two things worth stating. A comment cannot survive JSON,
    so nothing is lost by not reading the bytes; and a template a document
    inherits from (§6.6) or an interface range it declares is already expanded
    here, which is what lets the fragment be pasted into an inventory that has
    never heard of that template.

    ``exclude_defaults`` keeps it readable. A payload that spelled out every
    default the models apply — ``enabled: true`` on each of forty-eight ports —
    would be a fragment nobody could check by eye, and pasting it would write
    those defaults into the target as though somebody had chosen them.
    """
    element = source.element
    dumped = element.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude_defaults=True
    )
    metadata = dict(dumped.get("metadata") or {})
    metadata["name"] = short_name(source.fqn)
    return {
        "apiVersion": API_VERSION,
        "kind": element.kind,
        "metadata": metadata,
        "spec": dict(dumped.get("spec") or {}),
    }


def _common_namespace(namespaces: Sequence[str]) -> str:
    """The deepest namespace containing all of them."""
    parts = [namespace.split("/") if namespace else [] for namespace in namespaces]
    shared: list[str] = []
    for step in zip(*parts, strict=False):
        if len(set(step)) != 1:
            break
        shared.append(step[0])
    return "/".join(shared)


def _relative_namespace(namespace: str, root: str) -> str:
    """``namespace`` written below ``root``."""
    if not root:
        return namespace
    if namespace == root:
        return ""
    prefix = f"{root}/"
    return namespace[len(prefix) :] if namespace.startswith(prefix) else namespace


def paste_plan(
    inventory: Inventory,
    payload: Mapping[str, Any],
    *,
    namespace: str | None = None,
    suffix: str = DEFAULT_SUFFIX,
    keep_unique: bool = False,
    view: str | None = None,
    offset: tuple[float, float] | None = None,
    at: tuple[float, float] | None = None,
) -> CopyPlan:
    """The operations that paste a :func:`clipboard_payload` into ``inventory``.

    The documents come from the payload rather than from the tree, so this is
    the path that works *between* inventories: nothing here reads a source
    element, and a name that clashes with something in the target is
    deduplicated against the target.

    Raises:
        EditError: The payload is not a netgraph clipboard, or holds nothing.
    """
    documents = _payload_documents(payload)
    if not documents:
        raise EditError("the clipboard holds no netgraph documents")
    if len(documents) > MAX_COPIES:
        raise EditError(
            f"{len(documents)} elements is more than one paste should write at once "
            f"(the limit is {MAX_COPIES})"
        )

    root = namespace if namespace is not None else str(payload.get("root") or "")
    parsed: list[tuple[str, str, dict[str, Any], Element]] = []
    for entry in documents:
        document = entry["document"]
        try:
            element = parse_document(document)
        except SchemaError as exc:
            problems = "; ".join(str(issue) for issue in exc.issues)
            raise EditError(
                f"the clipboard holds a document netgraph cannot read: {problems}"
            ) from exc
        target = _below(root, str(entry.get("namespace") or ""))
        parsed.append((str(entry.get("address") or ""), target, dict(document), element))

    taken = set(inventory.elements)
    mapping: dict[str, str] = {}
    landing: list[tuple[str, str, str, dict[str, Any], Element]] = []
    for address, target_namespace, document, element in parsed:
        here = element.metadata.name
        siblings = {short_name(fqn) for fqn in taken if namespace_of(fqn) == target_namespace}
        name = here if here not in siblings else dedupe_name(here, siblings, suffix=suffix)
        fqn = qualify(target_namespace, name)
        taken.add(fqn)
        if address:
            mapping[address] = fqn
        landing.append((address, target_namespace, name, document, element))

    operations: list[Operation] = []
    inside = {address for address, *_ in landing if address}
    dropped: list[Dropped] = []
    for address, target_namespace, name, document, element in landing:
        if isinstance(element, (Cable, Tunnel)):
            outside = [
                end
                for end in (endpoint.device for endpoint in element.spec.endpoints)
                if not _names_one_of(end, inside)
            ]
            if outside:
                dropped.append(
                    Dropped(
                        address=address or name,
                        reason=(
                            f"{', '.join(sorted(outside))} is not in the pasted fragment, "
                            f"so the link has nothing to land on"
                        ),
                    )
                )
                mapping.pop(address, None)
                continue
        spec = dict(document.get("spec") or {})
        metadata = {
            key: value for key, value in (document.get("metadata") or {}).items() if key != "name"
        }
        built: dict[str, Any] = {"metadata": {"name": name, **metadata}, "spec": spec}
        if not keep_unique:
            strip_unique(built)
        _repoint_payload(
            built, address or name, element, mapping=mapping, namespace=target_namespace
        )
        operations.append(
            CreateElement(
                kind=element.kind,
                name=name,
                namespace=target_namespace,
                spec=built["spec"],
                metadata={key: value for key, value in built["metadata"].items() if key != "name"},
            )
        )

    if view:
        geometry = _payload_geometry(payload, mapping)
        if geometry:
            operations.extend(
                _geometry_operations(
                    inventory,
                    view=view,
                    placed=geometry,
                    mapping=mapping,
                    anchor=_Anchor(offset=offset or (DEFAULT_GRID, -DEFAULT_GRID), at=at),
                    fallback=root,
                    default_layout=_default_layout(inventory, view, root),
                )
            )
    return CopyPlan(operations=tuple(operations), mapping=mapping, dropped=tuple(dropped))


def _payload_documents(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The entries of a clipboard payload, checked.

    Raises:
        EditError: The payload is not a netgraph clipboard of a version this
            netgraph reads.
    """
    if not isinstance(payload, Mapping):  # pragma: no cover - typed at the boundary
        raise EditError("the clipboard does not hold a netgraph fragment")
    fmt = payload.get("format")
    if fmt != CLIPBOARD_FORMAT:
        raise EditError(
            f"the clipboard holds {fmt!r}, which is not a netgraph fragment ({CLIPBOARD_FORMAT})"
        )
    entries = payload.get("documents")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise EditError("the clipboard fragment has no 'documents' list")
    found: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("document"), Mapping):
            raise EditError("a clipboard entry is not a document")
        found.append(dict(entry))
    return found


def _names_one_of(written: str, inside: Iterable[str]) -> bool:
    """Does ``written`` name one of the fully-qualified names in ``inside``?"""
    return any(
        written == fqn or written == short_name(fqn) or fqn.endswith(f"/{written}")
        for fqn in inside
    )


def _repoint_payload(
    built: MutableMapping[str, Any],
    address: str,
    element: Element,
    *,
    mapping: Mapping[str, str],
    namespace: str,
) -> None:
    """Point a pasted document's references at the pasted copies.

    The references are read off the *model* — the same table a rename uses — so
    a string is rewritten because the schema says the field holds a reference,
    never because it looks like one.
    """
    if not mapping:
        return
    index = NameIndex(mapping.values())
    for reference in references_of(address, element):
        for old, new in mapping.items():
            if not _names_one_of(reference.target, [old]):
                continue
            replacement = reference_text(
                new, namespace=namespace, written=reference.target, index=index
            )
            # A document rebuilt from the model always writes its references
            # where the model says they are, so the refusal below cannot fire
            # for a payload netgraph wrote. It can for one somebody edited by
            # hand, and the honest answer there is to leave the reference as
            # written and let the validation gate report it, rather than to
            # refuse the whole paste over one string.
            with suppress(EditError):
                rewrite_reference(built, reference, replacement)
            break


def _payload_geometry(payload: Mapping[str, Any], mapping: Mapping[str, str]) -> dict[str, _Placed]:
    """The positions a payload recorded, for the sources that were pasted."""
    stored = payload.get("geometry")
    if not isinstance(stored, Mapping):
        return {}
    found: dict[str, _Placed] = {}
    for address, entry in stored.items():
        if address not in mapping or not isinstance(entry, Mapping):
            continue
        position = entry.get("position")
        if not isinstance(position, Mapping):  # pragma: no cover - written by us
            continue
        stored_size = entry.get("size")
        size: Mapping[str, Any] = stored_size if isinstance(stored_size, Mapping) else {}
        found[str(address)] = _Placed(
            layout="",
            key=str(address),
            placement=Placement(
                x=float(position.get("x", 0.0)),
                y=float(position.get("y", 0.0)),
                width=_number(size.get("width")),
                height=_number(size.get("height")),
            ),
        )
    return found


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):  # pragma: no cover - written by us
        return None
