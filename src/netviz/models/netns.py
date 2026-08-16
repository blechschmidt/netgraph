"""On-device network namespaces and the veth pairs that join them (§23).

A **network namespace** is a second, independent network stack inside one
machine: its own interfaces, its own addresses, its own routing table, its own
neighbour table. Linux calls it a netns; ``ip netns`` creates one; a container
runtime creates one per container and never tells the inventory about it. It is
*state of a box* in exactly the sense :mod:`netviz.models.routing` means it,
so it hangs off ``spec`` alongside the VRFs rather than being an element of its
own::

    spec:
      netns:
        - name: blue
        - name: web
          parent: blue

``parent`` is what makes the model nest. A namespace is created from *inside*
another one, and the one it is created from is the one that can still see it —
that is the whole of the hierarchy, and it is a tree because a namespace has
exactly one creator. A namespace with no ``parent`` sits in the device's initial
namespace, which no document declares: it is the machine itself, it always
exists, and it is named by the empty string (:data:`ROOT_NETNS`) for the same
reason :data:`~netviz.models.device.GLOBAL_VRF` is.

**A namespace is not a VRF and the two are not interchangeable.** A VRF
partitions the *routing table* of one stack; a namespace is a whole second
stack, so it partitions the interface names, the addresses, the sockets and the
routes at once. An interface can be in a namespace *and* in a VRF, and the pair
is what places it: ``netns: blue`` plus ``vrf: red`` is the ``red`` instance of
the ``blue`` stack, which has nothing to do with the ``red`` instance of the
initial one.

**Namespaces are joined by veth pairs, and a veth is an ethernet interface.**
There is no third interface type here, and deliberately: a veth end *is*
``ianaift:ethernetCsmacd`` — it has a MAC, it carries 802.3 frames, it can be a
bridge port, it can be given a VLAN sub-interface — and the only thing that
distinguishes it from a physical port is that the far end is another interface
of the same machine rather than a socket a cable plugs into. So a veth is
modelled as what it is: two ``type: ethernet`` interfaces naming each other with
``peer`` (§6.2.7). The pairing must be symmetric (``NV-N023``), which is what
makes it a *pair* rather than two independent claims, and a cable must not
terminate on one (``NV-N024``) because there is no socket to plug into.

Nothing here is Linux-only in shape. FreeBSD ``vnet`` jails and the ``epair``
that joins them are the same two concepts under different names, and the model
does not spell either vendor's command into the document.
"""

from __future__ import annotations

from typing import Final

from pydantic import model_validator

from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.scalars import ElementName

__all__ = [
    "ROOT_NETNS",
    "NetnsDefinition",
    "netns_depth",
    "netns_path",
    "resolve_netns_tree",
]

#: The namespace an interface that names none is in: the device's initial
#: network namespace. The empty string rather than ``"root"`` or ``None`` for the
#: reasons :data:`~netviz.models.device.GLOBAL_VRF` gives — it is not a name
#: anybody may declare, so no ``spec.netns`` entry can shadow it, and it sorts
#: before every real name, so the initial namespace leads every listing.
ROOT_NETNS: Final = ""

#: How the path of a nested namespace is spelled in a label and a node id:
#: ``blue/web`` is ``web`` inside ``blue``. A ``/`` because that is what a
#: reader already reads as containment, and because ``ElementName`` forbids one,
#: so a path can always be split back into its parts.
NETNS_SEPARATOR: Final = "/"


class NetnsDefinition(NetvizModel):
    """One entry of ``spec.netns`` — a network namespace on the device (§23.1).

    Three fields and no configuration: everything a namespace *contains* is
    declared by the things that name it. Interfaces move into one with
    ``interfaces[].netns``, and that is also what gives the namespace its
    addresses and its routes, because those hang off the interfaces.

    ``NV-N020`` (names are unique per device), ``NV-N021`` (``parent`` resolves
    and does not loop) and ``NV-N022`` (``interfaces[].netns`` resolves) are all
    checked by the device spec, where the whole table is in view.
    """

    name: ElementName
    #: The namespace this one was created inside; unset means the device's
    #: initial namespace. Names another entry of the same ``spec.netns``
    #: (``NV-N021``), which is what lets namespaces nest arbitrarily deep.
    parent: ElementName | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _check_parent(self) -> NetnsDefinition:
        """The one-step case, which is visible without the rest of the table."""
        if self.parent == self.name:
            raise field_error(
                f"namespace {self.name!r} is its own parent",
                rule="NV-N021",
                path=("parent",),
            )
        return self


def resolve_netns_tree(namespaces: list[NetnsDefinition]) -> dict[str, str]:
    """``name -> parent`` for every declared namespace, the root being ``""``.

    The map every consumer of the hierarchy walks. It is deliberately not a tree
    of objects: a namespace is identified by its name within its device, the
    parent link is the only edge, and a flat map is what both the depth
    calculation and the cycle check want.
    """
    return {entry.name: entry.parent or ROOT_NETNS for entry in namespaces}


def netns_path(name: str, parents: dict[str, str]) -> tuple[str, ...]:
    """The chain from the initial namespace down to ``name``, outermost first.

    ``()`` for the initial namespace itself. A cycle — which ``NV-N021`` refuses,
    but which this may still be handed by a caller working on a half-built
    document — terminates the walk rather than spinning, so the result is always
    finite and the rule that reports the cycle is the one that reports it.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current = name
    while current != ROOT_NETNS and current in parents and current not in seen:
        seen.add(current)
        chain.append(current)
        current = parents[current]
    chain.reverse()
    return tuple(chain)


def netns_depth(name: str, parents: dict[str, str]) -> int:
    """How deeply ``name`` is nested; ``0`` for the initial namespace."""
    return len(netns_path(name, parents))
