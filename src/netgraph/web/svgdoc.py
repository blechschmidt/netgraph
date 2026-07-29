"""Turning a Graphviz SVG into one that can be embedded in a live page.

``netgraph render -f svg`` produces a standalone document: an XML declaration,
a doctype, a fixed ``width``/``height`` in points, and a native browser tooltip
on every shape. All four are wrong for a document that is about to be spliced
into a page which already has a viewport of its own and an info box of its own,
so this module rewrites them:

* the wrapper (declaration, doctype, comments) is dropped and only the ``<svg>``
  element survives;
* ``width`` and ``height`` go, ``viewBox`` stays, so the diagram scales with
  whatever box CSS gives it;
* the native tooltips go — ``xlink:title`` and the ``<title>`` element — because
  the browser would otherwise pop its own, a second later and in a different
  place, over an info box that says strictly more;
* anything that could execute or navigate is removed — see :func:`prepare`.

The last point is the reason this is a real parse rather than a few regular
expressions. Graphviz emits none of those constructs today, and the tooltips it
does emit are properly escaped, so this is defence in depth rather than a fix
for a known hole: an SVG built from user-supplied text is injected into a live
DOM, which is exactly the situation where "the generator is careful" should not
be the only thing standing between the two.
"""

from __future__ import annotations

from typing import Final
from xml.etree import ElementTree

from netgraph.errors import RenderError

__all__ = ["SVG_NAMESPACE", "XLINK_NAMESPACE", "prepare"]

SVG_NAMESPACE: Final = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE: Final = "http://www.w3.org/1999/xlink"

#: Elements that can run code or pull in a document of their own. Removed with
#: everything below them.
_FORBIDDEN_TAGS: Final[frozenset[str]] = frozenset(
    {
        f"{{{SVG_NAMESPACE}}}{name}"
        for name in ("script", "foreignObject", "iframe", "animate", "set", "handler")
    }
)

#: Attributes removed wherever they appear: the native tooltips, which the info
#: box replaces, and the link machinery, because a diagram must not be able to
#: navigate the page that embeds it.
_STRIPPED_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        f"{{{XLINK_NAMESPACE}}}title",
        f"{{{XLINK_NAMESPACE}}}show",
        f"{{{XLINK_NAMESPACE}}}actuate",
        "target",
    }
)

#: The two spellings of a reference. Kept only when they point at an inline
#: picture: that is how an ``--icons`` theme survives into the rendering
#: (:func:`netgraph.render.dot._embed_icons` inlines each file as a ``data:``
#: URI), and a browser runs nothing inside an image. Every other value —
#: ``http:``, ``javascript:``, a fragment — is dropped.
_REFERENCE_ATTRIBUTES: Final[frozenset[str]] = frozenset({"href", f"{{{XLINK_NAMESPACE}}}href"})
_INLINE_IMAGE_PREFIX: Final = "data:image/"

#: The element form of a native tooltip. Graphviz writes one per node and edge
#: holding the identifier it laid out; the info box says the same thing and
#: more, immediately, so keeping both would mean two tooltips disagreeing about
#: which one the pointer is over.
_TITLE_TAG: Final = f"{{{SVG_NAMESPACE}}}title"

#: Sizing attributes on the root: dropped so the page decides how big the
#: diagram is. ``viewBox`` is what keeps the aspect ratio.
_ROOT_SIZE_ATTRIBUTES: Final[tuple[str, ...]] = ("width", "height")


def prepare(payload: bytes) -> str:
    """Return ``payload`` as an ``<svg>`` fragment safe to embed in a page.

    Args:
        payload: An SVG rendering, as Graphviz produced it.

    Returns:
        The serialised ``<svg>`` element, without the XML declaration.

    Raises:
        RenderError: The payload is not parseable XML, or is not an SVG.
    """
    ElementTree.register_namespace("", SVG_NAMESPACE)
    ElementTree.register_namespace("xlink", XLINK_NAMESPACE)
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise RenderError(f"Graphviz produced SVG that could not be parsed: {exc}") from exc
    if root.tag != f"{{{SVG_NAMESPACE}}}svg":
        raise RenderError(f"expected an SVG document, got {root.tag!r}")

    for attribute in _ROOT_SIZE_ATTRIBUTES:
        root.attrib.pop(attribute, None)
    # Without a viewBox there is nothing to scale against, so the fixed size is
    # the only sizing the document has and taking it away would collapse it.
    if "viewBox" not in root.attrib:
        raise RenderError("Graphviz produced an SVG with no viewBox to scale against")
    root.set("preserveAspectRatio", "xMidYMid meet")

    _sanitise(root)
    _drop_native_tooltips(root)
    return ElementTree.tostring(root, encoding="unicode")


def _sanitise(root: ElementTree.Element) -> None:
    """Remove every element and attribute that could run or navigate."""
    for parent in list(root.iter()):
        for child in [child for child in parent if child.tag in _FORBIDDEN_TAGS]:
            parent.remove(child)
    for element in root.iter():
        for name in [
            name for name, value in element.attrib.items() if _is_unsafe(name, str(value))
        ]:
            del element.attrib[name]


def _drop_native_tooltips(root: ElementTree.Element) -> None:
    """Remove every ``<title>``; see :data:`_TITLE_TAG`."""
    for parent in list(root.iter()):
        for child in [child for child in parent if child.tag == _TITLE_TAG]:
            parent.remove(child)


def _is_unsafe(name: str, value: str) -> bool:
    """Is this attribute one no embedded diagram has any business carrying?

    Event handlers are matched by shape rather than by name: ``onload`` and
    ``onmouseover`` are the ones that matter today, but the list of SVG events
    is long and grows, and an unknown ``on*`` attribute is not something a
    layout engine ever needed to emit.
    """
    if name in _REFERENCE_ATTRIBUTES:
        return not value.startswith(_INLINE_IMAGE_PREFIX)
    return name in _STRIPPED_ATTRIBUTES or name.lower().startswith("on")
