"""Re-homing documents by dropping them into a namespace (§2).

A namespace is a folder and a folder is a namespace — that is the whole of §2,
and it is why the visual editor can draw one as a box and mean it. Dragging a
device into the rack drawn around ``sites/north/racks/r1`` is not a picture of a
move; it *is* ``netviz edit move``, with the document rewritten into that
directory, ``metadata.namespace`` following the folder it now sits in, and every
reference to it re-spelled by :mod:`netviz.edit.references`.

This module is the half of that gesture which decides **what would happen**, so
that the drop can be refused before a byte is written. It answers one question —
"these addresses, dropped on that namespace: which ``move`` operations is that,
and is it legal?" — and it answers it from a loaded
:class:`~netviz.loader.inventory.Inventory` plus the tree's file facts, with no
access to the tree itself. Nothing here writes; the operations it returns go
through :func:`~netviz.edit.apply.apply_operation` like any others, which is
what keeps the browser's drop and ``netviz edit move`` the same code path
rather than two implementations of the same rule.

Three decisions are worth reading before the code.

**A drop is refused before it is applied, not after.** The two refusals that
matter — a name already taken in the target namespace, and two of the dragged
documents that would collide with *each other* once they land — are both
knowable from the inventory. Finding them here means the answer is "that switch
would collide with the one already in this rack" rather than a half-applied
batch rolled back with a validator's complaint. The transactional apply behind
this would roll such a batch back anyway (:class:`~netviz.edit.session.
EditSession`), so this is about the *message*, and the message is the feature.

**A container may be dragged as well as an element.** An address that names a
namespace rather than an element moves that whole subtree and keeps its shape:
``sites/north/racks/r1`` dropped on ``sites/south`` puts every element under it
in ``sites/south/r1/…``, one level deeper than the target and no flatter than it
started. That is what dragging a rack between two sites has to mean; flattening
the subtree into the target would silently merge two racks the moment their
switches shared a name.

**Where each document lands is the placement convention's business, not this
module's.** :func:`~netviz.edit.placement.choose_file` picks the file, so a
switch dropped into a rack gets ``r1/sw-north-acc-01.yaml`` and a cable joins
``r1/cables.yaml`` — the same answer ``netviz edit create`` would give, which
is what stops a dragged tree and a typed one from diverging.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

from netviz.edit.errors import EditError, PlacementError
from netviz.edit.operations import MoveElement, Operation
from netviz.edit.placement import FileFacts, check_file, choose_file
from netviz.loader.inventory import Inventory, namespace_of, qualify
from netviz.models import Element

__all__ = [
    "MAX_MOVES",
    "MovePlan",
    "Rehome",
    "check_namespace",
    "move_plan",
]

#: How many documents one drop may re-home. A gesture, not a migration: a drag
#: that would rewrite a thousand files is a ``netviz edit`` script somebody
#: should be able to read before running, and the same bound
#: :mod:`netviz.edit.clipboard` puts on a paste for the same reason.
MAX_MOVES: Final = 500

#: A file name :func:`check_namespace` puts inside the folder it is checking, so
#: that a namespace is judged by the rules the document landing in it will be
#: judged by rather than by a second, drifting copy of them. Never written.
_PROBE: Final = "element.yaml"


def check_namespace(namespace: str) -> str:
    """``namespace`` as the folder path the loader would read it from.

    The root namespace is ``""`` and is legal: dropping onto empty canvas means
    "take this out of whatever it is in", which is a move to the root.

    Raises:
        PlacementError: The path escapes the inventory or names a directory the
            loader skips, so nothing written there would be part of the tree.
    """
    text = str(namespace).replace("\\", "/").strip().strip("/")
    if not text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise PlacementError(
            f"{namespace!r} must be a folder inside the inventory, relative to its root"
        )
    if any(part.startswith((".", "_")) for part in path.parts):
        raise PlacementError(
            f"{namespace!r} starts a path component with '.' or '_', which the loader skips "
            f"(NV-L002); nothing moved there would be part of the inventory"
        )
    # The same rules as the file that will land in it, so a namespace and the
    # document in it can never disagree about what is legal. Asked of a name the
    # placement convention would actually produce, which is what makes this a
    # check of the *folder* rather than of a spelling invented here.
    check_file(f"{text}/{_PROBE}")
    return path.as_posix()


@dataclass(frozen=True, slots=True)
class Rehome:
    """One document's move: where it is, where it lands, and in which file."""

    #: Fully-qualified name before the move.
    address: str
    #: Fully-qualified name after it. Differs from :attr:`address` in its
    #: namespace and never in its ``metadata.name`` — a move is not a rename.
    target: str
    #: The namespace it lands in; ``""`` for the root.
    namespace: str
    #: The file its document is written to, POSIX, relative to the root.
    file: str
    #: The element kind, for the sentence a refusal or a log line is made of.
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "target": self.target,
            "namespace": self.namespace,
            "file": self.file,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class MovePlan:
    """What one drop would do: the operations, and what it left alone."""

    #: The namespace everything was dropped on.
    namespace: str
    operations: tuple[Operation, ...] = ()
    moves: tuple[Rehome, ...] = ()
    #: Addresses that were already where they were dropped. Not an error — a
    #: drag that ends where it started is how somebody decides not to move
    #: something — and not a write either.
    unchanged: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.operations)

    def describe(self) -> str:
        """One line for the undo stack and the log."""
        where = self.namespace or "the root namespace"
        if not self.moves:
            return f"nothing to move into {where}"
        if len(self.moves) == 1:
            return f"moved {self.moves[0].address} into {where}"
        return f"moved {len(self.moves)} elements into {where}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "operations": [operation.to_dict() for operation in self.operations],
            "moves": [move.to_dict() for move in self.moves],
            "unchanged": list(self.unchanged),
        }


def move_plan(
    inventory: Inventory,
    addresses: Sequence[str],
    *,
    namespace: str,
    files: Mapping[str, FileFacts] | None = None,
) -> MovePlan:
    """The ``move`` operations that dropping ``addresses`` on ``namespace`` means.

    Args:
        inventory: The loaded tree.
        addresses: Elements, and namespaces — a namespace moves its subtree and
            keeps its shape (see the module docstring).
        namespace: Where they land. ``""`` is the root, which is what a drop on
            empty canvas means.
        files: What each file already holds, from
            :meth:`~netviz.edit.tree.EditableTree.facts`, so the placement
            convention can put a dropped cable in the ``cables.yaml`` that is
            already there. Defaults to knowing about no files, which places
            every document by the convention for its kind.

    Returns:
        The plan. Empty of operations — rather than an error — when everything
        dropped was already in the target namespace.

    Raises:
        EditError: Nothing was named, an address names neither an element nor a
            namespace, the target is not a folder the loader would read, the
            drop is larger than :data:`MAX_MOVES`, or the move would collide
            with a name already in the target namespace or with another of the
            dragged documents. Nothing is written either way.
    """
    target = check_namespace(namespace)
    roots = _namespace_roots(inventory, addresses)
    sources = _resolve(inventory, addresses)
    if not sources:
        raise EditError("nothing was named to move")
    if len(sources) > MAX_MOVES:
        raise EditError(
            f"{len(sources)} elements is more than one drop should re-home at once "
            f"(the limit is {MAX_MOVES}); move them with 'netviz edit move'"
        )

    facts = dict(files or {})
    moving = {fqn for fqn, _element in sources}
    landing: dict[str, str] = {}
    moves: list[Rehome] = []
    unchanged: list[str] = []
    for fqn, element in sources:
        wanted = _retarget(namespace_of(fqn), target=target, roots=roots)
        name = element.metadata.name
        new_fqn = qualify(wanted, name)
        if new_fqn == fqn:
            unchanged.append(fqn)
            continue
        _check_collision(new_fqn, fqn, inventory=inventory, moving=moving, landing=landing)
        landing[new_fqn] = fqn
        relative = choose_file(kind=element.kind, namespace=wanted, name=name, files=facts)
        # The file this document is about to occupy is a sibling for the next
        # one placed beside it, so a rack that receives four switches gets four
        # files and a rack that receives four cables gets one ``cables.yaml``.
        held = facts.get(relative) or FileFacts(relative=relative, kinds=(), names=())
        facts[relative] = FileFacts(
            relative=relative,
            kinds=(*held.kinds, element.kind),
            names=(*held.names, name),
        )
        moves.append(
            Rehome(address=fqn, target=new_fqn, namespace=wanted, file=relative, kind=element.kind)
        )

    return MovePlan(
        namespace=target,
        operations=tuple(MoveElement(address=move.address, file=move.file) for move in moves),
        moves=tuple(moves),
        unchanged=tuple(unchanged),
    )


def _check_collision(
    new_fqn: str,
    fqn: str,
    *,
    inventory: Inventory,
    moving: frozenset[str] | set[str],
    landing: Mapping[str, str],
) -> None:
    """Refuse a landing spot that is taken, before anything is written.

    Two ways it can be taken, and they need different sentences: something the
    inventory already holds and is *not* part of this drag, or another document
    in the same drag that got there first. The second is the one a person hits
    by selecting two racks' worth of switches and dropping them in one rack.

    Raises:
        EditError: The name is taken.
    """
    if new_fqn in inventory.elements and new_fqn not in moving:
        where = namespace_of(new_fqn) or "the root namespace"
        raise EditError(
            f"{fqn} cannot move into {where}: it is already home to {new_fqn}, and two "
            f"elements there cannot share a name. Rename one of them first."
        )
    other = landing.get(new_fqn)
    if other is not None:
        where = namespace_of(new_fqn) or "the root namespace"
        raise EditError(
            f"{fqn} and {other} would both become {new_fqn} in {where}; drop one of them "
            f"somewhere else, or rename it first."
        )


def _retarget(current: str, *, target: str, roots: Sequence[str]) -> str:
    """The namespace one document lands in.

    A document dragged on its own lands *in* the target. One dragged as part of
    a namespace lands under the target, at the same depth below it that it sat
    below the namespace's own root — which is what makes a dragged rack arrive
    as a rack rather than as its contents.
    """
    for root in roots:
        if current == root or current.startswith(f"{root}/"):
            rest = current[len(root) :].lstrip("/")
            stem = PurePosixPath(root).name
            parts = [part for part in (target, stem, rest) if part]
            return "/".join(parts)
    return target


def _resolve(inventory: Inventory, addresses: Sequence[str]) -> tuple[tuple[str, Element], ...]:
    """Every element ``addresses`` names, namespaces expanded to their subtrees.

    In the inventory's own order rather than the caller's, so the diff a
    reviewer reads is in the order the documents were loaded. Duplicates
    collapse: dragging a rack *and* a switch inside it moves the switch once.

    Raises:
        EditError: An address is ambiguous, or names neither an element nor a
            namespace.
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
            raise EditError(
                f"there is no element or namespace called {text!r} in this inventory; only a "
                f"document can be moved, and a note, an area or a legend is moved by editing it"
            )
        for fqn in members:
            wanted.setdefault(fqn, None)
    return tuple((fqn, inventory.elements[fqn]) for fqn in inventory.elements if fqn in wanted)


def _members_of(inventory: Inventory, namespace: str) -> tuple[str, ...] | None:
    """Every element below ``namespace``, or ``None`` when there is no such folder.

    "Below", not "in": ``sites/hq`` names a folder even when every element is a
    level further down, and dragging a site has to mean the site.
    """
    text = namespace.strip("/")
    if not text:
        return None
    prefix = f"{text}/"
    found = tuple(fqn for fqn in inventory.elements if fqn.startswith(prefix))
    return found or None


def _namespace_roots(inventory: Inventory, addresses: Sequence[str]) -> tuple[str, ...]:
    """The namespaces among ``addresses``, outermost first.

    Outermost first so that a nested pair — a site and a rack inside it, both
    caught by one rubber band — is matched by the *site*: dragging a site
    carries the racks in it, and the rack keeps its place under the site rather
    than being lifted out and dropped beside it.
    """
    found = [
        address.strip().strip("/")
        for address in addresses
        if address.strip()
        and inventory.lookup(address.strip()).fqn is None
        and _members_of(inventory, address.strip()) is not None
    ]
    return tuple(sorted(dict.fromkeys(found), key=len))
