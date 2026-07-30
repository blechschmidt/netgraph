"""What an export left out, and why.

Every emitter in this package is *lossy* in the same structural way: an
``/etc/hosts`` fragment has nowhere to put a device with no address, a
Prometheus target list has nowhere to put one with no management address, and a
DNS zone has nowhere to put a namespace segment that is not a legal label. The
tempting behaviour — drop it and say nothing — is the one that costs an operator
an afternoon, because the artefact looks complete and is not.

So nothing is dropped silently. Each emitter records what it left out and what
it had to rename, and the CLI prints the collected record as one JSON document
**on stderr**, leaving stdout for the artefact itself::

    netgraph export prometheus-sd -o targets.json 2> manifest.json

Two kinds of record, because they are two different problems:

:class:`Skip`
    An element, interface or link that produced no output at all. The reader
    has to decide whether that is expected (a patch panel has no address by
    construction) or a gap in the inventory (a server nobody addressed).
:class:`Rewrite`
    Something that *did* reach the output, under a different spelling than the
    inventory gives it — a namespace of ``Building A`` becoming the DNS label
    ``building-a``. It is not a loss, but a reader grepping the artefact for the
    inventory's spelling would not find it, so it is reported rather than
    assumed obvious.

Both are sorted canonically before they are rendered, so re-running an export
over an unchanged tree produces a byte-identical manifest — the same promise the
artefacts themselves make.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from netgraph.models import API_VERSION

__all__ = [
    "MANIFEST_KIND",
    "Manifest",
    "Reason",
    "Recorder",
    "Rewrite",
    "Skip",
]

#: ``kind`` of the manifest document, mirroring the element envelope of §3 and
#: the ``NetworkGraph`` document :mod:`netgraph.render.jsonexport` emits.
MANIFEST_KIND: Final = "ExportManifest"


class Reason(str, Enum):
    """Why one thing did not reach the artefact.

    The value is the stable, machine-readable token; the docstring is what a
    human needs in order to know whether to act on it.
    """

    #: The device declares no ``spec.routes``, so there is no command to write
    #: for it. Expected of everything that is not a router, and of a router whose
    #: routing is entirely dynamic.
    NO_ROUTES = "no-routes"
    #: The element configures no address at all, or none on any interface that
    #: survived the filter. Expected of a patch panel and of an unmanaged
    #: switch; a gap in the inventory for anything else.
    NO_ADDRESS = "no-address"
    #: The element configures addresses, but every one of them is loopback or
    #: link-local, so none of them identifies it *on this network*
    #: (:func:`netgraph.subnets.is_routable_address`).
    NOT_ROUTABLE = "not-routable"
    #: The interface carries no address. An unnumbered port is normal on a
    #: switch and is reported per interface rather than per element.
    UNNUMBERED = "unnumbered"
    #: No address could be chosen to reach the element on, so there is nothing
    #: to put in ``ansible_host`` or in a scrape target.
    NO_MANAGEMENT_ADDRESS = "no-management-address"
    #: A cable or tunnel whose endpoint does not resolve, which the graph layer
    #: already dropped (:attr:`netgraph.render.graph.Graph.dangling`). Only
    #: reachable behind ``--force``.
    UNRESOLVED = "unresolved-endpoint"
    #: Nothing survived of the name after it was folded into the target
    #: format's grammar — a namespace written entirely in a script that has no
    #: ASCII fold, for instance.
    NOT_REPRESENTABLE = "not-representable"
    #: Two elements fold to the same name in the target format, and the format
    #: has no way to hold both. The first one wins; this is the other.
    NAME_COLLISION = "name-collision"
    #: The link is between two elements of which at least one was filtered out,
    #: so the artefact would describe a run to a device it never mentions.
    HALF_SELECTED = "half-selected"
    #: Both endpoints survived the filter but the link itself did not — a VLAN
    #: filter drops a cable carrying none of the VLANs asked for even when both
    #: the devices it joins are in them. Distinct from
    #: :attr:`HALF_SELECTED` because the reader would otherwise go looking for
    #: an element that is right there in the artefact.
    NOT_SELECTED = "not-selected"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Skip:
    """One thing that produced no output."""

    #: Fully-qualified name of the element, cable or tunnel — or
    #: ``element:interface`` when the record is about one port.
    subject: str
    reason: Reason
    #: One sentence a reader can act on, without the reason token's brevity.
    detail: str = ""

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.subject, self.reason.value, self.detail)

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"subject": self.subject, "reason": self.reason.value}
        if self.detail:
            record["detail"] = self.detail
        return record


@dataclass(frozen=True, slots=True)
class Rewrite:
    """One name the artefact spells differently from the inventory."""

    subject: str
    #: What was rewritten: ``hostname``, ``group``, ``label``.
    field: str
    original: str
    rewritten: str

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.subject, self.field, self.rewritten)

    def as_record(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "field": self.field,
            "from": self.original,
            "to": self.rewritten,
        }


@dataclass(slots=True)
class Recorder:
    """The mutable half of a manifest, written to while an emitter runs.

    An emitter is otherwise a pure function of its context; this is the one
    thing it accumulates. Kept separate from :class:`Manifest` so the artefact
    and its record are built in one pass and frozen together at the end.
    """

    skips: list[Skip] = field(default_factory=list)
    rewrites: list[Rewrite] = field(default_factory=list)
    #: How many elements, links or prefixes the emitter was offered.
    considered: int = 0
    #: How many of them reached the artefact.
    emitted: int = 0

    def skip(self, subject: str, reason: Reason, detail: str = "") -> None:
        self.skips.append(Skip(subject=subject, reason=reason, detail=detail))

    def rewrite(self, subject: str, *, field: str, original: str, rewritten: str) -> None:
        """Record a rename, unless the two spellings are the same one."""
        if original == rewritten:
            return
        self.rewrites.append(
            Rewrite(subject=subject, field=field, original=original, rewritten=rewritten)
        )

    def sealed(self, export_format: str) -> Manifest:
        """Freeze what was recorded into a :class:`Manifest`."""
        return Manifest(
            export_format=export_format,
            skipped=tuple(sorted(self.skips, key=lambda entry: entry.sort_key)),
            rewritten=tuple(sorted(self.rewrites, key=lambda entry: entry.sort_key)),
            considered=self.considered,
            emitted=self.emitted,
        )


@dataclass(frozen=True, slots=True)
class Manifest:
    """The finished record of one export, ready to be printed or parsed."""

    export_format: str
    skipped: tuple[Skip, ...] = ()
    rewritten: tuple[Rewrite, ...] = ()
    considered: int = 0
    emitted: int = 0

    @property
    def is_clean(self) -> bool:
        """Did everything the export was offered reach the artefact intact?"""
        return not self.skipped and not self.rewritten

    def of_reason(self, reason: Reason) -> tuple[Skip, ...]:
        """Every skip recorded for one reason, for a caller that summarises."""
        return tuple(entry for entry in self.skipped if entry.reason is reason)

    def reasons(self) -> Iterator[tuple[Reason, int]]:
        """``(reason, count)`` for every reason that occurred, in enum order."""
        for reason in Reason:
            count = sum(1 for entry in self.skipped if entry.reason is reason)
            if count:
                yield reason, count

    def summary(self) -> str:
        """``18 emitted, 2 skipped (no-address 2), 1 renamed`` — one line."""
        parts = [f"{self.emitted} of {self.considered} emitted"]
        if self.skipped:
            detail = ", ".join(f"{reason} {count}" for reason, count in self.reasons())
            parts.append(f"{len(self.skipped)} skipped ({detail})")
        if self.rewritten:
            parts.append(f"{len(self.rewritten)} renamed")
        return ", ".join(parts)

    def as_record(self) -> dict[str, Any]:
        """The manifest as plain JSON-compatible data."""
        return {
            "apiVersion": API_VERSION,
            "kind": MANIFEST_KIND,
            "format": self.export_format,
            "counts": {
                "considered": self.considered,
                "emitted": self.emitted,
                "skipped": len(self.skipped),
                "rewritten": len(self.rewritten),
            },
            "skipped": [entry.as_record() for entry in self.skipped],
            "rewritten": [entry.as_record() for entry in self.rewritten],
        }

    def to_json(self, *, indent: int = 2) -> str:
        """The manifest as a JSON document, newline-terminated."""
        return json.dumps(self.as_record(), indent=indent, ensure_ascii=False) + "\n"
