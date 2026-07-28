"""The document envelope shared by every element kind (§3 of ``docs/schema.md``)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from netgraph.models.base import NetgraphModel
from netgraph.models.metadata import Metadata
from netgraph.models.scalars import API_VERSION, ApiVersion

__all__ = ["DEFAULT_API_VERSION", "KINDS", "ElementBase"]

#: Re-exported for callers that build documents programmatically.
DEFAULT_API_VERSION = API_VERSION

#: The seven kinds defined by ``netgraph.dev/v1alpha1`` (§3).
KINDS: tuple[str, ...] = (
    "switch",
    "router",
    "hub",
    "computer",
    "server",
    "cable",
    "adapter",
)


class ElementBase(NetgraphModel):
    """Envelope of a netgraph document.

    Subclasses narrow :attr:`kind` to a literal, which makes the union of all
    element models a discriminated union (see :mod:`netgraph.models.document`).
    """

    api_version: ApiVersion = Field(alias="apiVersion", serialization_alias="apiVersion")
    kind: str
    metadata: Metadata

    #: Element kinds that own interfaces and can therefore be cabled.
    has_interfaces: ClassVar[bool] = True

    @property
    def name(self) -> str:
        """Shortcut for ``metadata.name`` — unique within the element's namespace.

        The loader qualifies it with the directory the document was found in;
        see :func:`netgraph.loader.qualify`.
        """
        return self.metadata.name

    def __str__(self) -> str:
        return f"{self.kind}/{self.metadata.name}"
