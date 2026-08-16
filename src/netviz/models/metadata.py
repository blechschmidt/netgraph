"""The document ``metadata`` block (§3.1 of ``docs/schema.md``)."""

from __future__ import annotations

import re
from typing import Final

from pydantic import Field, field_validator, model_validator

from netviz.errors import echo_value
from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.scalars import ElementName, RackUnit, RackUnits

__all__ = ["RESERVED_LABEL_PREFIX", "Location", "Metadata"]

#: Label prefix reserved for tool-generated labels (§3.1).
RESERVED_LABEL_PREFIX: Final = "netviz.dev/"

#: Longest annotation value. Annotations carry tool input (suppression lists,
#: rendering hints), so they are roomier than labels but still bounded.
_MAX_ANNOTATION_VALUE = 4096

_LABEL_NAME_RE: Final = re.compile(r"^[a-z0-9](?:[-a-z0-9_.]*[a-z0-9])?$")
_DNS_LABEL_RE: Final = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")

_MAX_LABEL_NAME = 63
_MAX_LABEL_PREFIX = 253
_MAX_LABEL_VALUE = 253


def _check_key(key: str, *, kind: str) -> None:
    """Validate a label or annotation key against ``NV-N003``."""
    prefix, separator, name = key.rpartition("/")
    if separator and not prefix:
        raise ValueError(f"{kind} key {echo_value(key)} has an empty prefix")
    if "/" in prefix:
        raise ValueError(f"{kind} key {echo_value(key)} has more than one '/' separator")

    if not name:
        raise ValueError(f"{kind} key {echo_value(key)} has an empty name part")
    if len(name) > _MAX_LABEL_NAME:
        raise ValueError(
            f"{kind} key name part {echo_value(name)} is longer than {_MAX_LABEL_NAME} characters"
        )
    if not _LABEL_NAME_RE.match(name):
        raise ValueError(
            f"{kind} key name part {echo_value(name)} must match {_LABEL_NAME_RE.pattern}"
        )

    if not separator:
        return
    if len(prefix) > _MAX_LABEL_PREFIX:
        raise ValueError(
            f"{kind} key prefix {echo_value(prefix)} is longer than {_MAX_LABEL_PREFIX} characters"
        )
    if not all(_DNS_LABEL_RE.match(part) for part in prefix.split(".")):
        raise ValueError(f"{kind} key prefix {echo_value(prefix)} is not a DNS subdomain")


def _check_label_key(key: str) -> None:
    """Validate a label key against ``NV-N003``.

    Labels are *user* vocabulary and drive ``--select``, so the tool's own
    prefix is off limits. Annotations are the opposite: they exist to carry
    tool input, so :data:`RESERVED_LABEL_PREFIX` is allowed there.
    """
    if key.startswith(RESERVED_LABEL_PREFIX):
        raise ValueError(
            f"label key {echo_value(key)} uses the reserved prefix {RESERVED_LABEL_PREFIX!r}"
        )
    _check_key(key, kind="label")


class Location(NetvizModel):
    """``metadata.location`` — where the hardware physically is (§3.2).

    Free-text ``spec.location`` says it in prose, which is enough for a label
    and useless for anything else: no tool can tell from ``"MDF, third rack,
    near the top"`` whether two things collide. This block is the structured
    form, and it is on ``metadata`` rather than on a ``spec`` because it is true
    of every kind — a patch panel is racked exactly as a server is.

    ``position`` is the *lowest* rack unit the element occupies and ``height``
    how many it takes, so a 2U server at ``position: 10`` fills U10 and U11.
    Units count from 1 at the bottom, which is how a cabinet is labelled.
    """

    site: str | None = None
    room: str | None = None
    #: Rack identifier, unique within its room. Naming one is what puts the
    #: element on an elevation; without it the block is documentation only.
    rack: str | None = None
    #: Lowest rack unit the element occupies, counted from 1 at the bottom.
    position: RackUnit | None = None
    #: How many units it occupies, upwards from :attr:`position`.
    height: RackUnits = 1
    #: How tall the rack is. Declared by any element in it; ``NV-U003`` refuses
    #: two elements that disagree, and the value bounds ``NV-U002``.
    rack_height: RackUnits | None = None

    @model_validator(mode="after")
    def _check_rack(self) -> Location:
        for key in ("position", "rack_height"):
            if getattr(self, key) is not None and self.rack is None:
                raise field_error(
                    f"{key!r} places the element in a rack, so 'rack' must name which one",
                    rule="NV-U004",
                    path=(key,),
                )
        return self

    @property
    def rack_key(self) -> tuple[str, str, str] | None:
        """Identity of the rack, or ``None`` when the element names none.

        A rack name is unique within its room and a room within its site, so
        the three together are what two elements have to share before they can
        collide. An unset site or room is the empty string rather than a
        wildcard: an inventory that names the rack of one element and the site,
        room and rack of another has not said the two are in the same place.
        """
        if self.rack is None:
            return None
        return (self.site or "", self.room or "", self.rack)

    @property
    def rack_label(self) -> str:
        """The rack as a reader would write it, e.g. ``hq / mdf / r1``."""
        key = self.rack_key
        return " / ".join(part for part in key if part) if key is not None else ""

    @property
    def units(self) -> range:
        """The rack units this element occupies; empty when it is unplaced."""
        if self.position is None:
            return range(0)
        return range(self.position, self.position + self.height)

    @property
    def top(self) -> int | None:
        """The highest rack unit the element occupies, or ``None`` if unplaced."""
        return None if self.position is None else self.position + self.height - 1

    @property
    def is_placed(self) -> bool:
        """Does this element occupy a definite span of a definite rack?"""
        return self.rack is not None and self.position is not None


class Metadata(NetvizModel):
    """Identity and free-form annotation of an element."""

    #: Unique across the whole inventory (``NV-N002``, checked by the validator).
    name: ElementName
    #: Free text, may be multi-line. Rendered as a node tooltip.
    description: str | None = None
    #: Where the hardware is: site, room, rack and the units it occupies (§3.2).
    location: Location | None = None
    #: Selector-friendly key/value pairs driving ``--select`` and ``--group-by``.
    labels: dict[str, str] = Field(default_factory=dict)
    #: Non-selectable per-element input to the tooling. ``netviz/ignore``
    #: suppresses validation rules on this element; see :mod:`netviz.validate`.
    annotations: dict[str, str] = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def _check_labels(cls, labels: dict[str, str]) -> dict[str, str]:
        for key, value in labels.items():
            _check_label_key(key)
            if len(value) > _MAX_LABEL_VALUE:
                raise ValueError(
                    f"value of label {echo_value(key)} is longer than {_MAX_LABEL_VALUE} characters"
                )
        return labels

    @field_validator("annotations")
    @classmethod
    def _check_annotations(cls, annotations: dict[str, str]) -> dict[str, str]:
        for key, value in annotations.items():
            _check_key(key, kind="annotation")
            if len(value) > _MAX_ANNOTATION_VALUE:
                raise ValueError(
                    f"value of annotation {echo_value(key)} is longer than "
                    f"{_MAX_ANNOTATION_VALUE} characters"
                )
        return annotations

    def label(self, key: str, default: str | None = None) -> str | None:
        """Return the value of ``key``, or ``default`` when it is not set."""
        return self.labels.get(key, default)

    def annotation(self, key: str, default: str | None = None) -> str | None:
        """Return the value of annotation ``key``, or ``default``."""
        return self.annotations.get(key, default)
