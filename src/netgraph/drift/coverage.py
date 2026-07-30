"""What a capture *could* have seen, as distinct from what it did see.

This is the module the whole command turns on. Comparing an inventory with a
capture is easy; comparing them without turning every blind spot of the capture
into a deletion is not. ``lldpctl`` never reports an IP address, so an address
in the inventory and not in the capture says nothing at all — and reporting it
as "the network no longer has this address" would make the command useless the
first time somebody ran it against a partial capture, which is every time.

So each dialect declares what it observes, as a :class:`Capability`:

=========  ==========  =====  =========  =======  =========================
``--from`` interfaces  links  addresses  members  what the absence of a thing means
=========  ==========  =====  =========  =======  =========================
lldp       no          yes    no         no       nothing, except on a port where LLDP did see a neighbour
iproute    yes         no     yes        yes      the host does not have it
csv        no          yes    no         no       nothing, except on a port the list does mention
=========  ==========  =====  =========  =======  =========================

A device's coverage is the union over every dialect that observed it
(:meth:`Coverage.of`), because two captures of one host are the ordinary case:
``ip -j addr show`` plus ``lldpctl -f json`` between them see the interfaces,
the addresses *and* the neighbours.

Two refinements keep the table from over-claiming.

**Evidence, not just capability.** ``ip -j link show`` and ``ip -j addr show``
are the same dialect, and only the second carries addresses. A capability alone
would let a ``link show`` capture report every declared address as missing, so
address coverage additionally requires that the device really did yield an
address of that family (:meth:`Coverage.observes_addresses`). The same argument
applies to bridge and bond membership.

**Lower bounds stay lower bounds.** ``ip`` never reports the VLAN set a trunk
carries; :mod:`netgraph.importer.iproute` derives the minimum set implied by the
sub-interfaces stacked on the port and marks it as inference. A VLAN observed
there and not declared is a real difference — the port carries something the
inventory does not admit to — while a VLAN declared and not observed is simply
outside what ``ip`` prints. Coverage is therefore directional for such a field,
and :meth:`Coverage.observes_trunk_vlans` is always false.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from netgraph.importer.draft import Draft, DraftDevice

__all__ = ["CAPABILITIES", "Capability", "Coverage", "coverage_of"]


@dataclass(frozen=True, slots=True)
class Capability:
    """What one dialect can see about a device it observed.

    Every flag answers the same question — *does the absence of something from
    this capture mean the network does not have it?* — for one kind of thing. A
    flag is never about whether a value can be read, only about whether silence
    is informative; a scalar such as a MAC needs no flag at all, because a
    capture that reports one reports it and a capture that does not has said
    nothing either way.
    """

    #: The capture lists every interface the device has.
    interfaces: bool
    #: The capture reports the device's links at all.
    links: bool
    #: The capture lists every address of an interface it reported.
    addresses: bool
    #: The capture lists every member of a bridge or a bond.
    members: bool

    def merge(self, other: Capability) -> Capability:
        """The union: what two captures of one device see between them."""
        return Capability(
            interfaces=self.interfaces or other.interfaces,
            links=self.links or other.links,
            addresses=self.addresses or other.addresses,
            members=self.members or other.members,
        )


#: What each ``--from`` dialect observes. Keyed by the dialect name
#: :func:`netgraph.importer.dialect_of` returns, so ``auto`` never appears.
CAPABILITIES: Final[dict[str, Capability]] = {
    "lldp": Capability(interfaces=False, links=True, addresses=False, members=False),
    "iproute": Capability(interfaces=True, links=False, addresses=True, members=True),
    "csv": Capability(interfaces=False, links=True, addresses=False, members=False),
}

#: A device no capability was found for: it sees nothing, so nothing about it is
#: ever drift. Unreachable through the CLI — every input has a dialect — but it
#: is what keeps a hand-built :class:`Draft` in a test from claiming coverage it
#: never declared.
BLIND: Final = Capability(interfaces=False, links=False, addresses=False, members=False)


@dataclass(frozen=True, slots=True)
class Coverage:
    """Per-device capability, plus the evidence that refines it."""

    #: Observed device name to the union of its dialects' capabilities.
    by_device: dict[str, Capability]
    #: Observed device name to the dialects that saw it, sorted.
    dialects: dict[str, tuple[str, ...]]
    #: The whole run's dialects, sorted.
    used: tuple[str, ...]
    #: The draft the coverage was derived from, for the evidence checks.
    draft: Draft

    def of(self, device: str) -> Capability:
        """What the captures that saw ``device`` can see about it."""
        return self.by_device.get(device, BLIND)

    def dialects_of(self, device: str) -> tuple[str, ...]:
        """Which dialects observed ``device``, sorted."""
        return self.dialects.get(device, ())

    def saw(self, device: str) -> bool:
        """Did any input observe this device at all?"""
        return device in self.by_device

    def observes_interfaces(self, device: str) -> bool:
        """Is the interface list of ``device`` complete in the capture?"""
        return self.of(device).interfaces

    def observes_links(self, device: str) -> bool:
        """Does any capture of ``device`` report its neighbours?"""
        return self.of(device).links

    def observes_addresses(self, device: str, family: str) -> bool:
        """Is the ``family`` address list of ``device`` complete?

        Requires an address of that family to have been observed *somewhere* on
        the device: ``ip -j link show`` is the ``iproute`` dialect too, and it
        carries no addresses at all. Without this an operator who captured only
        the link table would be told every address in their inventory had been
        removed from the network.
        """
        if not self.of(device).addresses:
            return False
        observed = self.draft.devices.get(device)
        if observed is None:  # pragma: no cover - coverage is built from the draft
            return False
        return any(getattr(interface, family) for interface in observed.interfaces.values())

    def observes_members(self, device: str) -> bool:
        """Is bridge and bond membership complete for ``device``?"""
        return self.of(device).members

    def observes_trunk_vlans(self, device: str) -> bool:
        """Always false: no dialect netgraph reads prints a port's VLAN set.

        A method rather than a constant so the asymmetry is stated where the
        other coverage questions are asked, and so a dialect that *does* report
        one has an obvious place to change.
        """
        return False


def coverage_of(draft: Draft) -> Coverage:
    """Derive the coverage of a draft from the dialects that built it.

    :attr:`~netgraph.importer.draft.DraftDevice.sources` names the inputs a
    device was seen in and :attr:`~netgraph.importer.draft.Draft.dialects` maps
    an input to how it was read, so the join is the whole of this function.
    """
    by_device: dict[str, Capability] = {}
    dialects: dict[str, tuple[str, ...]] = {}
    for name, device in draft.devices.items():
        seen = _dialects_of(device, draft)
        dialects[name] = seen
        capability = BLIND
        for dialect in seen:
            capability = capability.merge(CAPABILITIES.get(dialect, BLIND))
        by_device[name] = capability
    return Coverage(
        by_device=by_device,
        dialects=dialects,
        used=tuple(sorted(set(draft.dialects.values()))),
        draft=draft,
    )


def _dialects_of(device: DraftDevice, draft: Draft) -> tuple[str, ...]:
    """The dialects of every input ``device`` was observed in, sorted."""
    return tuple(
        sorted({dialect for source in device.sources if (dialect := draft.dialects.get(source))})
    )
