"""The document envelope shared by every element kind (§3 of ``docs/schema.md``)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from netgraph.models.base import NetgraphModel
from netgraph.models.metadata import Metadata
from netgraph.models.scalars import API_VERSION, ApiVersion

__all__ = [
    "ANNOTATION_DOCUMENT_KINDS",
    "AREA_KIND",
    "DEFAULT_API_VERSION",
    "DOCUMENT_KINDS",
    "KINDS",
    "LAYOUT_KIND",
    "LEGEND_KIND",
    "NOTE_KIND",
    "TEMPLATE_KIND",
    "TEST_SUITE_KIND",
    "THEME_KIND",
    "ElementBase",
]

#: Re-exported for callers that build documents programmatically.
DEFAULT_API_VERSION = API_VERSION

#: The thirteen *element* kinds defined by ``netgraph.dev/v1alpha1`` (§3). Each
#: one becomes a node or an edge of the graph.
KINDS: tuple[str, ...] = (
    "switch",
    "router",
    "firewall",
    "hub",
    "computer",
    "server",
    "cable",
    "adapter",
    "tunnel",
    "patchpanel",
    "pdu",
    "user",
    "group",
)

#: The thirteenth kind (§6.6). A template declares no element: it is a named partial
#: device ``spec`` that the loader merges into the devices naming it in
#: ``spec.from``, and it is gone by the time anything downstream sees the tree.
TEMPLATE_KIND: str = "template"

#: The fourteenth kind (§18). A layout declares no element either: it is diagram
#: geometry for elements declared elsewhere, kept in its own document so that a
#: model file stays free of pixels and an arrangement can be dropped, diffed or
#: versioned on its own. See :mod:`netgraph.models.layout`.
LAYOUT_KIND: str = "layout"

#: The eighteenth kind (§22.3). A theme declares no element and no network fact:
#: it is a stylesheet, naming classes of element and how each is drawn. Unlike
#: every other kind here it is not *used* by being in the tree — a rendering
#: applies the one ``--theme`` names and no other — but it is recognised here so
#: that keeping ``theme.yaml`` beside the manifests it styles, which is the
#: obvious place for it, is not an error. See :mod:`netgraph.models.theme`.
THEME_KIND: str = "theme"

#: The fifteenth kind (§20). A test suite declares no element either: it is a list
#: of named assertions about the network the other documents describe, graded by
#: ``netgraph test``. See :mod:`netgraph.models.testsuite`.
TEST_SUITE_KIND: str = "testsuite"

#: The sixteenth, seventeenth and eighteenth kinds (§21). Diagram annotations: a
#: callout, a zone, a key. Like a layout they declare no element and carry no
#: network fact — they are what the picture says *about* the network, and they
#: are barred from affecting anything the tool concludes. See
#: :mod:`netgraph.models.annotation`.
NOTE_KIND: str = "note"
AREA_KIND: str = "area"
LEGEND_KIND: str = "legend"

#: The three annotation kinds, in the order §21 introduces them.
ANNOTATION_DOCUMENT_KINDS: tuple[str, ...] = (NOTE_KIND, AREA_KIND, LEGEND_KIND)

#: Every ``kind`` a document may declare: elements, templates, layouts, suites
#: and annotations.
DOCUMENT_KINDS: tuple[str, ...] = (
    *KINDS,
    TEMPLATE_KIND,
    LAYOUT_KIND,
    TEST_SUITE_KIND,
    *ANNOTATION_DOCUMENT_KINDS,
    THEME_KIND,
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
