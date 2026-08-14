"""Merging every layout document in a tree into one arrangement per view.

An inventory may hold several ``kind: layout`` documents — one per site, one per
diagram, one that a colleague dropped in — and each names things the way its own
namespace names them. This module turns that into the single flat table a
renderer can use, and does the two jobs that need the whole tree to do:

**Resolution.** A key is an address, resolved exactly as a cable endpoint is:
relative to the layout document's own namespace first, then outwards. A key that
resolves to nothing is kept *verbatim*, because the derived nodes a layer
invents — ``subnet:10.0.0.0/24``, ``tunnel:site/wg0``, ``rack:hq/comms/r1`` — are
real node ids that no document declares, and refusing them would make an L3
diagram unarrangeable. Whether a verbatim key means anything is settled against
the *graph*, not here — by ``netgraph layout``, which builds one — or against
the elements, by rule ``W138``, which does not.

**Precedence.** Two documents placing the same node in the same view is a
conflict, and the first in load order wins — the same rule the loader applies to
a duplicate element name, for the same reason: an inventory must render the same
way twice.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

from netgraph.layout.geometry import Box, Geometry, Placement, points_of
from netgraph.loader.inventory import Inventory, namespace_of

__all__ = ["Conflict", "conflicts_in", "resolve_geometry", "resolve_key"]


def resolve_key(key: str, *, inventory: Inventory, namespace: str) -> str:
    """The node id a layout key names.

    An exact fully-qualified name wins outright. Anything carrying a ``:`` is a
    derived id (``subnet:``, ``tunnel:``, ``rack:``, ``aggregate:``) and is
    taken as written, since element names cannot contain one. Everything else
    goes through the loader's own resolution, so ``sw-core`` written inside
    ``sites/hq/`` means what it means everywhere else in that folder.
    """
    if key in inventory.elements:
        return key
    if ":" in key:
        return key
    return inventory.resolve_fqn(key, namespace=namespace) or key


def resolve_geometry(inventory: Inventory, view: str) -> Geometry:
    """Every layout document's geometry for ``view``, merged and resolved.

    Args:
        inventory: The loaded tree.
        view: The layer name, as :class:`~netgraph.render.graph.Layer` spells
            it (``l1``, ``l3``, ``routing``, ...).

    Returns:
        One :class:`~netgraph.layout.geometry.Geometry`. Empty — and cheap —
        when the tree declares no layout at all, which is the normal case and
        the one that must not cost a rendering anything.
    """
    if not inventory.layouts:
        return Geometry(view=view)

    nodes: dict[str, Placement] = {}
    edges: dict[str, tuple[tuple[float, float], ...]] = {}
    groups: dict[str, Box] = {}
    for fqn, layout in inventory.layouts.items():
        geometry = layout.view(view)
        if geometry is None:
            continue
        namespace = namespace_of(fqn)
        for key, placement in geometry.nodes.items():
            nodes.setdefault(
                resolve_key(key, inventory=inventory, namespace=namespace),
                Placement.from_model(placement),
            )
        for key, waypoints in geometry.edges.items():
            edges.setdefault(
                resolve_key(key, inventory=inventory, namespace=namespace),
                points_of(waypoints),
            )
        for key, box in geometry.groups.items():
            # A group key is a namespace, not an element, so it is qualified
            # rather than resolved: a layout in ``sites/`` naming ``hq`` means
            # ``sites/hq``, and nothing has to exist for that to be true.
            groups.setdefault(_qualify_group(key, namespace), Box.from_model(box))
    return Geometry(view=view, nodes=nodes, edges=edges, groups=groups)


def _qualify_group(key: str, namespace: str) -> str:
    """A namespace key read from a document in ``namespace``."""
    if not namespace or key.startswith(f"{namespace}/") or key == namespace:
        return key
    return f"{namespace}/{key}"


class Conflict(tuple[str, str, str, str]):
    """``(view, section, key, layout)`` — geometry a later document could not add.

    A tuple subclass rather than a dataclass because it is only ever formatted
    into a message and compared for equality, and a tuple already does both.
    """

    __slots__ = ()

    @property
    def view(self) -> str:
        return self[0]

    @property
    def section(self) -> str:
        return self[1]

    @property
    def key(self) -> str:
        return self[2]

    @property
    def layout(self) -> str:
        return self[3]


def conflicts_in(inventory: Inventory, views: Iterable[str]) -> tuple[Conflict, ...]:
    """Geometry a second layout document declared for something already placed.

    Reported rather than merged: silently taking one of two answers is how a
    diagram starts disagreeing with itself between machines.
    """
    return tuple(_conflicts(inventory, views))


def _conflicts(inventory: Inventory, views: Iterable[str]) -> Iterator[Conflict]:
    for view in views:
        seen: dict[tuple[str, str], str] = {}
        for fqn, layout in inventory.layouts.items():
            geometry = layout.view(view)
            if geometry is None:
                continue
            namespace = namespace_of(fqn)
            for section, entries in _sections(geometry):
                for key in entries:
                    resolved = (
                        _qualify_group(key, namespace)
                        if section == "groups"
                        else resolve_key(key, inventory=inventory, namespace=namespace)
                    )
                    owner = seen.setdefault((section, resolved), fqn)
                    if owner != fqn:
                        yield Conflict((view, section, resolved, owner))


def _sections(geometry: object) -> Iterator[tuple[str, Mapping[str, object]]]:
    for section in ("nodes", "edges", "groups"):
        yield section, getattr(geometry, section)
