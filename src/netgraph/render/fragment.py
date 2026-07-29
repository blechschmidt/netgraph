"""Turning a Graphviz SVG into one that can be embedded in a page.

``netgraph render -f svg`` produces a standalone document: an XML declaration, a
doctype, a fixed ``width``/``height`` in points, a native browser tooltip on
every shape and — with ``--link-template`` — an anchor around it. All of that is
right for a file opened on its own and wrong for a document that already has a
viewport, a detail panel and a page of its own around it, so this module
rewrites it:

* the wrapper (declaration, doctype, comments) is dropped and only the ``<svg>``
  element survives;
* ``width`` and ``height`` go, ``viewBox`` stays, so the diagram scales with
  whatever box CSS gives it;
* anything that could execute is removed — see :func:`fragment`;
* the native tooltips and the links are kept or dropped by the caller, because
  the two embedders disagree: ``netgraph web`` replaces both with an info box
  and must not have a second tooltip popping up over it, while a page with
  ``--link-template`` links in it wants the anchors it asked for;
* every ``id`` can be given a prefix, because a page holding more than one
  drawing of the same inventory — the layers and view variants of
  :mod:`netgraph.render.html` — would otherwise hold each element id several
  times over, and ``getElementById`` answers with whichever came first.

The removal is a real parse rather than a few regular expressions. Graphviz
emits none of the dangerous constructs today and escapes what it does emit, so
this is defence in depth rather than a fix for a known hole: an SVG built from
user-supplied text is spliced into a document, which is exactly the situation
where "the generator is careful" should not be the only thing standing between
the two.
"""

from __future__ import annotations

import re
from typing import Final
from xml.etree import ElementTree

from netgraph.errors import RenderError

__all__ = ["SVG_NAMESPACE", "XLINK_NAMESPACE", "fragment"]

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

#: Attributes removed wherever they appear: the parts of the link machinery
#: that decide *how* a link opens, which is the embedding page's business rather
#: than the diagram's.
_STRIPPED_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        f"{{{XLINK_NAMESPACE}}}show",
        f"{{{XLINK_NAMESPACE}}}actuate",
        "target",
    }
)

#: The attribute form of a native tooltip, kept or dropped with the element
#: form. Graphviz writes the detail here and
#: :func:`netgraph.render.dot._promote_tooltips` moves it into a ``<title>``;
#: this is what an unmatched document would be left holding.
_TITLE_ATTRIBUTE: Final = f"{{{XLINK_NAMESPACE}}}title"

#: The two spellings of a reference.
_REFERENCE_ATTRIBUTES: Final[frozenset[str]] = frozenset({"href", f"{{{XLINK_NAMESPACE}}}href"})

#: How an ``--icons`` theme survives into the rendering
#: (:func:`netgraph.render.dot._embed_icons` inlines each file as a ``data:``
#: URI). A browser runs nothing inside an image, so this reference is always
#: allowed; every other one depends on whether links were asked for.
_INLINE_IMAGE_PREFIX: Final = "data:image/"

#: Schemes a kept link may use. ``--link-template`` builds an ``https`` URL
#: back to a repository; anything that could execute in the page's own origin
#: — ``javascript:``, ``data:text/html`` — is not a link, whatever it is
#: spelled like.
_LINK_SCHEMES: Final[tuple[str, ...]] = ("https://", "http://", "mailto:")

#: The element form of a native tooltip. Graphviz writes one per node and edge.
_TITLE_TAG: Final = f"{{{SVG_NAMESPACE}}}title"

#: The anchor Graphviz wraps a shape in when the element carries a ``URL``.
_ANCHOR_TAG: Final = f"{{{SVG_NAMESPACE}}}a"

#: Sizing attributes on the root: dropped so the page decides how big the
#: diagram is. ``viewBox`` is what keeps the aspect ratio.
_ROOT_SIZE_ATTRIBUTES: Final[tuple[str, ...]] = ("width", "height")

#: A reference to an id inside the same document, as it appears in a paint
#: attribute (``clip-path="url(#clip0)"``). Rewritten with the ids themselves so
#: a prefixed document keeps pointing at its own parts.
_URL_REFERENCE: Final = re.compile(r"url\(#([^)\s]+)\)")

#: Everything a prefix may hold, for the same reason an element id is a slug:
#: it becomes part of an XML ``id``.
_SAFE_PREFIX: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")


def fragment(
    payload: bytes,
    *,
    tooltips: bool = False,
    links: bool = False,
    prefix: str = "",
) -> str:
    """Return ``payload`` as an ``<svg>`` fragment safe to embed in a page.

    Args:
        payload: An SVG rendering, as Graphviz produced it.
        tooltips: Keep the native ``<title>`` tooltips. Off for an embedder
            that shows the same detail itself and would otherwise show it twice.
        links: Keep the anchors ``--link-template`` produced. Off for an
            embedder that must not let a diagram navigate the page around it.
        prefix: Prepended to every ``id`` in the document, with internal
            references rewritten to match. Empty leaves the ids alone.

    Returns:
        The serialised ``<svg>`` element, without the XML declaration.

    Raises:
        RenderError: The payload is not parseable XML, is not an SVG, has no
            ``viewBox``, or the prefix is not usable in an XML id.
    """
    if prefix and not _SAFE_PREFIX.fullmatch(prefix):
        raise RenderError(
            f"{prefix!r} cannot prefix an XML id: expected a letter, then [A-Za-z0-9_.-]"
        )
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

    _sanitise(root, links=links, tooltips=tooltips)
    if not tooltips:
        _drop(root, _TITLE_TAG)
    if not links:
        _unwrap_anchors(root)
    if prefix:
        _prefix_ids(root, prefix)
    return ElementTree.tostring(root, encoding="unicode")


def _sanitise(root: ElementTree.Element, *, links: bool, tooltips: bool) -> None:
    """Remove every element and attribute that could run or navigate."""
    for parent in list(root.iter()):
        for child in [child for child in parent if child.tag in _FORBIDDEN_TAGS]:
            parent.remove(child)
    for element in root.iter():
        for name in [
            name
            for name, value in element.attrib.items()
            if _is_unsafe(name, str(value), tag=element.tag, links=links, tooltips=tooltips)
        ]:
            del element.attrib[name]


def _drop(root: ElementTree.Element, tag: str) -> None:
    """Remove every ``tag`` element, wherever it appears."""
    for parent in list(root.iter()):
        for child in [child for child in parent if child.tag == tag]:
            parent.remove(child)


def _unwrap_anchors(root: ElementTree.Element) -> None:
    """Replace every ``<a>`` with its children, keeping what it drew.

    An anchor with its ``href`` already stripped is an element that still
    responds to a click by doing nothing, and a keyboard tab stop that leads
    nowhere. What the diagram needs from it is the shape inside.
    """
    for parent in list(root.iter()):
        if not any(child.tag == _ANCHOR_TAG for child in parent):
            continue
        children: list[ElementTree.Element] = []
        for child in parent:
            children.extend(list(child) if child.tag == _ANCHOR_TAG else [child])
        parent[:] = children


def _is_unsafe(name: str, value: str, *, tag: str, links: bool, tooltips: bool) -> bool:
    """Is this attribute one no embedded diagram has any business carrying?

    Event handlers are matched by shape rather than by name: ``onload`` and
    ``onmouseover`` are the ones that matter today, but the list of SVG events
    is long and grows, and an unknown ``on*`` attribute is not something a
    layout engine ever needed to emit.
    """
    if name in _REFERENCE_ATTRIBUTES:
        return not _is_safe_reference(value, links=links, anchor=tag == _ANCHOR_TAG)
    if name == _TITLE_ATTRIBUTE:
        return not tooltips
    return name in _STRIPPED_ATTRIBUTES or name.lower().startswith("on")


def _is_safe_reference(value: str, *, links: bool, anchor: bool) -> bool:
    """May this ``href`` stay?

    An inline picture always: a browser runs nothing inside an image, and this
    is how an ``--icons`` theme survives. A reference to another part of the
    same drawing (``<use href="#shape">``, a gradient, a clip path) always too —
    it fetches nothing and goes nowhere — *unless* it is on an anchor, where
    ``#`` is navigation like any other and would move the embedding page's
    fragment out from under it. Everything else is a link, and links are kept
    only when the caller asked for them.
    """
    if value.startswith(_INLINE_IMAGE_PREFIX):
        return True
    if value.startswith("#"):
        return not anchor
    return links and value.lower().startswith(_LINK_SCHEMES)


def _prefix_ids(root: ElementTree.Element, prefix: str) -> None:
    """Prepend ``prefix`` to every id, and to every reference to one.

    Both directions matter: an id nobody points at is a bookmark, and a
    reference to an id that moved is a hole in the picture.
    """
    marker = f"{prefix}-"
    for element in root.iter():
        identifier = element.get("id")
        if identifier is not None:
            element.set("id", f"{marker}{identifier}")
        for name, value in element.attrib.items():
            text = str(value)
            if "url(#" in text:
                element.set(name, _URL_REFERENCE.sub(rf"url(#{marker}\1)", text))
            elif name in _REFERENCE_ATTRIBUTES and text.startswith("#"):
                element.set(name, f"#{marker}{text[1:]}")
