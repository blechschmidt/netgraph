"""The document ``metadata`` block (§3.1 of ``docs/schema.md``)."""

from __future__ import annotations

import re
from typing import Final

from pydantic import Field, field_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.scalars import ElementName

__all__ = ["RESERVED_LABEL_PREFIX", "Metadata"]

#: Label prefix reserved for tool-generated labels (§3.1).
RESERVED_LABEL_PREFIX: Final = "netgraph.dev/"

#: Longest annotation value. Annotations carry tool input (suppression lists,
#: rendering hints), so they are roomier than labels but still bounded.
_MAX_ANNOTATION_VALUE = 4096

_LABEL_NAME_RE: Final = re.compile(r"^[a-z0-9](?:[-a-z0-9_.]*[a-z0-9])?$")
_DNS_LABEL_RE: Final = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")

_MAX_LABEL_NAME = 63
_MAX_LABEL_PREFIX = 253
_MAX_LABEL_VALUE = 253


def _check_key(key: str, *, kind: str) -> None:
    """Validate a label or annotation key against ``NG-N003``."""
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
    """Validate a label key against ``NG-N003``.

    Labels are *user* vocabulary and drive ``--select``, so the tool's own
    prefix is off limits. Annotations are the opposite: they exist to carry
    tool input, so :data:`RESERVED_LABEL_PREFIX` is allowed there.
    """
    if key.startswith(RESERVED_LABEL_PREFIX):
        raise ValueError(
            f"label key {echo_value(key)} uses the reserved prefix {RESERVED_LABEL_PREFIX!r}"
        )
    _check_key(key, kind="label")


class Metadata(NetgraphModel):
    """Identity and free-form annotation of an element."""

    #: Unique across the whole inventory (``NG-N002``, checked by the validator).
    name: ElementName
    #: Free text, may be multi-line. Rendered as a node tooltip.
    description: str | None = None
    #: Selector-friendly key/value pairs driving ``--select`` and ``--group-by``.
    labels: dict[str, str] = Field(default_factory=dict)
    #: Non-selectable per-element input to the tooling. ``netgraph/ignore``
    #: suppresses validation rules on this element; see :mod:`netgraph.validate`.
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
