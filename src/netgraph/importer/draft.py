"""The intermediate inventory ``netgraph import`` builds before writing anything.

Every dialect reader produces the same three record types — :class:`DraftDevice`,
:class:`DraftInterface`, :class:`DraftCable` — and appends them to one
:class:`Draft`. Keeping a neutral shape between "what a tool printed" and "what
a YAML document looks like" buys three things:

* **Merging.** ``ip -j link show`` and ``ip -j addr show`` describe the same
  host, and two neighbours describe the same cable from opposite ends. Merging
  is a property of the draft, not of a parser, so every dialect gets it and none
  of them has to know about the others.
* **Honesty.** A field is either observed or absent. Where netgraph had to
  reason about an observation — the medium of a cable, the fact that a parent
  port must trunk the VLAN its sub-interface encapsulates — the reasoning is
  recorded as a *comment* next to the value, not folded silently into it. The
  emitter has nowhere to write a value that no reader put here.
* **Testability.** The readers can be checked against a fixture without a
  filesystem, and the emitter against a draft without a parser.

The draft deliberately holds strings and ints rather than
:mod:`netgraph.models` objects. Its job is to describe a document that a human
is about to edit, including the parts netgraph could not fill in; a model would
insist on being complete and correct before that editing has happened.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from netgraph.errors import clip_text
from netgraph.importer.names import element_name

__all__ = [
    "Draft",
    "DraftCable",
    "DraftDevice",
    "DraftInterface",
    "DraftVlan",
    "Endpoint",
    "comment_text",
]

#: How much of a value taken from a tool's output a comment quotes back. The
#: comment is there to let a reader recognise what was renamed, not to reproduce
#: a 4 KiB chassis description in the middle of a YAML document.
MAX_COMMENT_LENGTH: Final = 100

#: ``(device, interface)`` — one end of a cable. Interfaces are per device, so
#: the pair is the smallest thing that identifies a link end globally.
Endpoint = tuple[str, str]


def comment_text(text: str) -> str:
    """Make ``text`` safe to put after a ``#``.

    A comment carries values that came from a device: a port description, a
    chassis name, a filename. Any of them may hold a newline, which would end
    the comment and turn the rest of the value into YAML, and any of them may be
    arbitrarily long. Both are the emitter's problem to prevent rather than the
    reader's to remember.
    """
    collapsed = " ".join(text.split())
    return clip_text(collapsed, limit=MAX_COMMENT_LENGTH)


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DraftVlan:
    """``interfaces[].vlan`` as observed, or as reasoned about from what was."""

    #: ``access`` or ``trunk``.
    mode: str
    access_vlan: int | None = None
    trunk_vlans: list[int] = field(default_factory=list)
    #: Why this block is here, when it was not read off the device verbatim.
    comment: str | None = None

    def merge(self, other: DraftVlan) -> None:
        """Fold ``other`` in, keeping what is already known."""
        if self.mode == other.mode == "trunk":
            self.trunk_vlans = sorted({*self.trunk_vlans, *other.trunk_vlans})
        if self.access_vlan is None:
            self.access_vlan = other.access_vlan
        if self.comment is None:
            self.comment = other.comment


@dataclass(slots=True)
class DraftInterface:
    """One entry of ``spec.interfaces``, in the order §6.2 writes them."""

    name: str
    #: One of the :class:`~netgraph.models.interface.InterfaceType` values.
    type: str = "ethernet"
    description: str | None = None
    #: Admin state. ``None`` means "not observed"; the schema default is ``true``,
    #: so only an observed *down* interface is written out.
    enabled: bool | None = None
    mac: str | None = None
    mtu: int | None = None
    #: ``a.b.c.d/len`` strings, in the order the source reported them.
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    vlan: DraftVlan | None = None
    parent: str | None = None
    members: list[str] = field(default_factory=list)
    #: Lines emitted above the entry, each already prefixed with its own
    #: ``inferred:`` marker where one applies.
    comments: list[str] = field(default_factory=list)

    def merge(self, other: DraftInterface) -> None:
        """Fold ``other`` into this interface, preferring what is already known.

        Two captures of one host are the usual case — ``ip -j link show`` and
        ``ip -j addr show`` overlap almost entirely — so "first observation
        wins, later ones fill gaps" is both the safe rule and the one that makes
        the argument order of the command line irrelevant.
        """
        # A more specific type always wins over the ``ethernet`` fallback: a
        # neighbour that only named the port cannot know it is a bond.
        if self.type == "ethernet" and other.type != "ethernet":
            self.type = other.type
        for attribute in ("description", "enabled", "mac", "mtu", "parent"):
            if getattr(self, attribute) is None:
                setattr(self, attribute, getattr(other, attribute))
        for attribute in ("ipv4", "ipv6"):
            merged = list(getattr(self, attribute))
            merged.extend(value for value in getattr(other, attribute) if value not in merged)
            setattr(self, attribute, merged)
        self.members = sorted({*self.members, *other.members})
        if self.vlan is None:
            self.vlan = other.vlan
        elif other.vlan is not None:
            self.vlan.merge(other.vlan)
        _extend_unique(self.comments, other.comments)


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DraftDevice:
    """One device document, with its interfaces indexed by name."""

    name: str
    #: ``switch``, ``router``, ``hub``, ``computer`` or ``server``.
    kind: str = "computer"
    #: Note printed beside ``kind:``. Present whenever the kind was reasoned
    #: about rather than read, which for ``computer`` is always: no dialect
    #: netgraph reads reports "this is a workstation".
    kind_comment: str | None = None
    description: str | None = None
    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    location: str | None = None
    interfaces: dict[str, DraftInterface] = field(default_factory=dict)
    #: VLAN ids seen anywhere on this device, written out as ``spec.vlans`` so a
    #: port referencing one does not trip ``W113``.
    vlans: set[int] = field(default_factory=set)
    #: Lines emitted above ``apiVersion``, under the generated header.
    comments: list[str] = field(default_factory=list)
    #: Input names this device was seen in, in the order they were read.
    sources: list[str] = field(default_factory=list)

    def interface(self, name: str) -> DraftInterface:
        """The interface called ``name``, created as a plain ethernet port if new."""
        existing = self.interfaces.get(name)
        if existing is None:
            existing = self.interfaces[name] = DraftInterface(name=name)
        return existing

    def add_interface(self, interface: DraftInterface) -> DraftInterface:
        """Add ``interface``, merging it into one of the same name."""
        existing = self.interfaces.get(interface.name)
        if existing is None:
            self.interfaces[interface.name] = interface
            return interface
        existing.merge(interface)
        return existing

    def note(self, comment: str) -> None:
        _extend_unique(self.comments, [comment])

    def observed_in(self, source: str) -> None:
        _extend_unique(self.sources, [source])

    def refine_kind(self, kind: str, comment: str | None) -> None:
        """Adopt ``kind`` unless something more specific is already known.

        ``computer`` is the fallback every dialect lands on when it has nothing
        to go by, so it never displaces a kind another input observed. Two
        inputs that both observed a kind and disagree keep the first: the second
        is recorded as a comment, and disagreement about what a box *is* is
        exactly the sort of thing a human should settle.
        """
        if kind == self.kind:
            return
        if self.kind == "computer" and self.kind_comment is not None:
            self.kind, self.kind_comment = kind, comment
            return
        if kind != "computer":
            self.note(
                f"another input reported this device as a {kind}; netgraph kept "
                f"{self.kind!r} — check which is right"
            )

    def sorted_interfaces(self) -> list[DraftInterface]:
        """Interfaces in a stable, readable order.

        Physical ports first and stacked interfaces after them, so a bridge
        reads below the ports it is built from, and alphabetically within each
        group so the output does not depend on the order a tool happened to
        print its records in.
        """
        return sorted(
            self.interfaces.values(), key=lambda entry: (_TYPE_ORDER.get(entry.type, 9), entry.name)
        )


#: Interface types in the order they are written out. Members before aggregates,
#: parents before sub-interfaces: the file then reads bottom-up like the stack.
_TYPE_ORDER: Final[dict[str, int]] = {
    "ethernet": 0,
    "wifi": 1,
    "lag": 2,
    "bridge": 3,
    "vlan": 4,
    "tunnel": 5,
    "loopback": 6,
}


# --------------------------------------------------------------------------- #
# Cables
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DraftCable:
    """One ``cable`` document: two endpoints and whatever else was observed."""

    endpoints: tuple[Endpoint, Endpoint]
    #: Assigned by :meth:`Draft.assign_cable_names` once every adjacency has
    #: been collected, because a name has to be unique across the whole run and
    #: no single reader can know that.
    name: str = ""
    #: ``copper`` is what a cable falls back to. It is a *required* field, so
    #: unlike everything else in the draft it cannot simply be left out when
    #: nothing observed it — which is what :attr:`medium_stated` is for.
    medium: str = "copper"
    #: Did an input actually say what the medium is? No capture format reports
    #: it, so this is false for LLDP and for a CSV row that omits the column,
    #: and the emitter writes a comment saying the value was filled in.
    medium_stated: bool = False
    speed: str | None = None
    label: str | None = None
    #: What was observed, one line per capture that saw the adjacency.
    comments: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[Endpoint, Endpoint]:
        """Identity of the adjacency, independent of which end reported it.

        LLDP is symmetric: run on both neighbours it yields ``A:1 -> B:2`` and
        ``B:2 -> A:1``, which are one cable. Sorting the pair is what collapses
        them, and it is also what makes the generated name stable regardless of
        the order the capture files were passed in.
        """
        first, second = sorted(self.endpoints)
        return (first, second)

    def merge(self, other: DraftCable) -> None:
        for attribute in ("speed", "label"):
            if getattr(self, attribute) is None:
                setattr(self, attribute, getattr(other, attribute))
        # A medium an input stated beats the fallback, whichever end of the link
        # happened to state it; two inputs that both state one keep the first.
        if not self.medium_stated and other.medium_stated:
            self.medium, self.medium_stated = other.medium, True
        _extend_unique(self.comments, other.comments)
        _extend_unique(self.sources, other.sources)


# --------------------------------------------------------------------------- #
# The draft
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Draft:
    """Everything one ``netgraph import`` run observed, before it is a tree."""

    devices: dict[str, DraftDevice] = field(default_factory=dict)
    #: Keyed by :attr:`DraftCable.key`, which is what performs the dedup.
    cables: dict[tuple[Endpoint, Endpoint], DraftCable] = field(default_factory=dict)
    #: Lines for the run report: what was skipped, and why.
    notes: list[str] = field(default_factory=list)
    #: Input name to the dialect it was read as, filled in by
    #: :func:`~netgraph.importer.run.build_draft`.
    #:
    #: The importer itself has no use for this — a document is written from what
    #: was observed, not from what did the observing. ``netgraph drift`` does:
    #: whether the *absence* of something in a capture means "the network does
    #: not have it" or "this dialect cannot see it" is a property of the dialect,
    #: and :attr:`DraftDevice.sources` is what ties a device to one.
    dialects: dict[str, str] = field(default_factory=dict)

    def device(self, name: str) -> DraftDevice:
        """The device called ``name``, created as a bare ``computer`` if new."""
        existing = self.devices.get(name)
        if existing is None:
            existing = self.devices[name] = DraftDevice(
                name=name,
                kind_comment=(
                    "inferred: nothing in the captured output states what this device is; "
                    "'computer' is netgraph's neutral default — correct it by hand"
                ),
            )
        return existing

    def add_cable(self, cable: DraftCable) -> DraftCable:
        """Add ``cable``, folding it into the same adjacency seen from the other end."""
        existing = self.cables.get(cable.key)
        if existing is None:
            self.cables[cable.key] = cable
            return cable
        existing.merge(cable)
        return existing

    def note(self, message: str) -> None:
        _extend_unique(self.notes, [message])

    def prune(self) -> None:
        """Drop what cannot become a document, saying so on :attr:`notes`.

        A device with no interfaces is the one shape the schema rejects outright
        (``spec.interfaces`` needs at least one entry), and it is reachable: a
        neighbour whose port id netgraph could not read is named without a single
        port to its name. Writing the document anyway would produce a tree that
        does not load, which is a worse answer than a line in the report.
        """
        for name in [name for name, device in self.devices.items() if not device.interfaces]:
            del self.devices[name]
            self.note(
                f"{name!r} was named by an input but no interface of it was observed, so no "
                "document was written for it"
            )
        for key in [
            key
            for key, cable in self.cables.items()
            if any(device not in self.devices for device, _ in cable.endpoints)
        ]:
            del self.cables[key]
            self.note(
                f"the link {_endpoints_text(key)} was dropped because one of its devices has "
                "no observed interface"
            )

    def assign_cable_names(self) -> None:
        """Give every cable a unique ``metadata.name`` derived from its endpoints.

        The single link between two devices is ``cbl-pc-alice-sw-core-01``, which
        is what somebody reading a diagram legend wants. Where a pair is joined
        by more than one cable that name cannot tell them apart, so *every* cable
        of that pair takes the long form with both ports in it — not just the
        second one, which would make the name of an existing link depend on
        whether a later capture found a sibling.

        Naming happens once, at the end, from the *sorted* endpoint pair, so it
        does not depend on which capture mentioned the link first. Two distinct
        adjacencies can still sanitise onto one identifier (``Gi0/1`` and
        ``Gi0-1`` are different ports), so a numeric suffix is the last resort.
        """
        pairs: dict[tuple[str, str], int] = {}
        for cable in self.cables.values():
            pairs[_device_pair(cable.key)] = pairs.get(_device_pair(cable.key), 0) + 1

        taken: set[str] = set()
        for cable in self.sorted_cables():
            base = (
                _short_cable_name(cable.key)
                if pairs[_device_pair(cable.key)] == 1
                else _long_cable_name(cable.key)
            )
            name, index = base, 1
            while name in taken:
                index += 1
                name = f"{base}-{index}"
            taken.add(name)
            cable.name = name

    def sorted_devices(self) -> list[DraftDevice]:
        return sorted(self.devices.values(), key=lambda device: device.name)

    def sorted_cables(self) -> list[DraftCable]:
        return sorted(self.cables.values(), key=lambda cable: (cable.key, cable.name))


def _endpoints_text(key: tuple[Endpoint, Endpoint]) -> str:
    (device_a, port_a), (device_b, port_b) = key
    return f"{device_a}:{port_a} <-> {device_b}:{port_b}"


def _device_pair(key: tuple[Endpoint, Endpoint]) -> tuple[str, str]:
    return (key[0][0], key[1][0])


def _short_cable_name(key: tuple[Endpoint, Endpoint]) -> str:
    """``cbl-pc-alice-sw-core-01`` — the only link between two devices."""
    device_a, device_b = _device_pair(key)
    return _sanitised(f"cbl-{device_a}-{device_b}")


def _long_cable_name(key: tuple[Endpoint, Endpoint]) -> str:
    """``cbl-sw-core-01-Gi0-1-srv-hyper-eno1`` — one of several parallel links."""
    (device_a, port_a), (device_b, port_b) = key
    return _sanitised(f"cbl-{device_a}-{port_a}-{device_b}-{port_b}")


def _sanitised(candidate: str) -> str:
    # Both halves are already legal element names or interface names, so the
    # only way this fails is a length ceiling that trimmed everything away.
    return element_name(candidate)[0] or "cbl"


def _extend_unique(target: list[str], values: Iterable[str]) -> None:
    """Append the values of ``values`` that ``target`` does not already hold."""
    for value in values:
        if value not in target:
            target.append(value)
