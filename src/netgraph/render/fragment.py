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

Saying each thing once
----------------------

A page holds a drawing per view, so anything a drawing repeats is repeated
again by every view of it. Two payloads dominated the measurement behind entry 8
of ``docs/follow-ups.md``, and the parse tree this module already builds is
where both are cheapest to remove:

* **the font attributes.** Graphviz states ``font-family``, ``font-size`` and
  ``text-anchor`` on every ``<text>`` element it emits — 36 % of the bytes of a
  campus drawing. They are *inherited* properties, so stating the dominant value
  once on the root and dropping it from the elements that agree renders exactly
  the same picture (:func:`_hoist_text_attributes`);
* **the icons.** ``--icons`` embeds each picture as a ``data:`` URI on every
  node that uses it, in every drawing: 313 kB of a campus page for 4 kB of
  distinct artwork. Each becomes a ``<symbol>`` in one shared
  :class:`IconLibrary` and a ``<use>`` at each site.

Both are transformations of the markup and not of the picture. That is asserted
rather than argued — ``tests/test_html.py`` renders the same inventory with and
without them and requires the drawn geometry to be identical.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Final
from xml.etree import ElementTree

from netgraph.errors import RenderError

__all__ = ["SVG_NAMESPACE", "XLINK_NAMESPACE", "IconLibrary", "fragment"]

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

#: A link to another file of the same bundle, which is what the diagrams in a
#: ``netgraph report`` carry: ``../devices/sw-core.html`` and nothing cleverer.
#: Deliberately not "anything without a scheme" — a relative reference is
#: matched positively, character by character, so no spelling of a scheme, an
#: authority (``//host``) or a control character can be mistaken for one. The
#: leading character rules out an absolute path, which would leave the bundle.
_RELATIVE_LINK: Final = re.compile(r"[A-Za-z0-9._~-][A-Za-z0-9._~/-]*(#[A-Za-z0-9._~-]+)?")

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

#: The element Graphviz draws a label with, and the one it draws an
#: ``--icons`` picture with.
_TEXT_TAG: Final = f"{{{SVG_NAMESPACE}}}text"
_IMAGE_TAG: Final = f"{{{SVG_NAMESPACE}}}image"

#: What a shared picture is stored as and referred to by.
_SYMBOL_TAG: Final = f"{{{SVG_NAMESPACE}}}symbol"
_DEFS_TAG: Final = f"{{{SVG_NAMESPACE}}}defs"
_SVG_TAG: Final = f"{{{SVG_NAMESPACE}}}svg"
_USE_TAG: Final = f"{{{SVG_NAMESPACE}}}use"

#: Inherited text properties Graphviz states on every ``<text>`` element rather
#: than once on the document. Hoisting them is only sound when *every* text
#: carries the attribute: one that carries none would otherwise start
#: inheriting a value it never had, so the check is per attribute and per
#: drawing rather than a list of values known to be safe.
_INHERITED_TEXT: Final[tuple[str, ...]] = (
    "font-family",
    "font-size",
    "text-anchor",
    "font-weight",
)

#: Geometry copied from an inline picture onto the ``<use>`` that replaces it.
#: ``preserveAspectRatio`` deliberately stays behind, on the ``<image>`` inside
#: the symbol, because it is a property of the picture rather than of the box
#: it is drawn in — and two nodes drawing the same file the same way is exactly
#: what makes them shareable.
_IMAGE_GEOMETRY: Final[tuple[str, ...]] = ("x", "y", "width", "height")


class IconLibrary:
    """The pictures a set of drawings share, each stored once.

    An ``--icons`` theme reaches a rendering as a ``data:`` URI per *node*, and
    a page draws every node once per view, so the same few kilobytes of artwork
    can be spelled out hundreds of times. A library collects them: each distinct
    picture becomes one ``<symbol>``, and every site that drew it becomes a
    ``<use>`` naming that symbol.

    One library is shared across every drawing of a page (see
    :mod:`netgraph.render.html`), which is what makes the icon payload a
    property of the *theme* rather than of the view count. A caller with a
    single drawing can let :func:`fragment` make its own, in which case the
    symbols are written into that drawing.

    The identity of a picture is its data and how it is fitted into its box:
    two nodes share a symbol when both would have drawn the same bytes the same
    way, and not otherwise.
    """

    def __init__(self, prefix: str = "ng-icon") -> None:
        self._prefix = prefix
        self._symbols: dict[tuple[str, str], str] = {}

    def reference(self, href: str, fit: str) -> str:
        """The id of the symbol drawing ``href`` fitted with ``fit``."""
        key = (href, fit)
        name = self._symbols.get(key)
        if name is None:
            name = f"{self._prefix}-{len(self._symbols) + 1}"
            self._symbols[key] = name
        return name

    def defs(self) -> ElementTree.Element | None:
        """The ``<defs>`` holding every symbol, or ``None`` when there are none.

        A symbol carries no viewport of its own: a ``<use>`` referring to one
        supplies the width and height, the ``<image>`` inside fills that box,
        and the fit the original picture asked for decides how. So the pair
        draws exactly what the single ``<image>`` drew, at every site.
        """
        if not self._symbols:
            return None
        defs = ElementTree.Element(_DEFS_TAG)
        for (href, fit), name in self._symbols.items():
            symbol = ElementTree.SubElement(defs, _SYMBOL_TAG, {"id": name})
            attributes = {
                f"{{{XLINK_NAMESPACE}}}href": href,
                "width": "100%",
                "height": "100%",
            }
            if fit:
                attributes["preserveAspectRatio"] = fit
            ElementTree.SubElement(symbol, _IMAGE_TAG, attributes)
        return defs

    def markup(self) -> str:
        """The library as a standalone ``<svg>`` to place once in a page.

        Zero-sized and out of the accessibility tree: nothing inside ``<defs>``
        is drawn, and the element exists only so that a ``<use>`` elsewhere in
        the document has something to name.
        """
        defs = self.defs()
        if defs is None:
            return ""
        root = ElementTree.Element(
            _SVG_TAG,
            {"class": "ng-defs", "width": "0", "height": "0", "aria-hidden": "true"},
        )
        root.append(defs)
        return ElementTree.tostring(root, encoding="unicode")


def fragment(
    payload: bytes,
    *,
    tooltips: bool = False,
    links: bool = False,
    local_links: bool = False,
    prefix: str = "",
    icons: IconLibrary | None = None,
) -> str:
    """Return ``payload`` as an ``<svg>`` fragment safe to embed in a page.

    Args:
        payload: An SVG rendering, as Graphviz produced it.
        tooltips: Keep the native ``<title>`` tooltips. Off for an embedder
            that shows the same detail itself and would otherwise show it twice.
        links: Keep the anchors ``--link-template`` produced — absolute
            ``https``, ``http`` and ``mailto`` URLs. Off for an embedder that
            must not let a diagram navigate the page around it.
        local_links: Also keep anchors pointing at another file of the same
            bundle (:data:`_RELATIVE_LINK`). This is how a device in a
            ``netgraph report`` diagram reaches its own page; it stays off by
            default because a relative link means nothing in a document whose
            location the renderer does not know.
        prefix: Prepended to every ``id`` in the document, with internal
            references rewritten to match. Empty leaves the ids alone.
        icons: Where the inline pictures are hoisted to. Pass one shared
            library when several drawings go into one document, so the artwork
            is stored once for all of them; the default gives this drawing its
            own, written into the drawing.

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
    if root.tag != _SVG_TAG:
        raise RenderError(f"expected an SVG document, got {root.tag!r}")

    for attribute in _ROOT_SIZE_ATTRIBUTES:
        root.attrib.pop(attribute, None)
    # Without a viewBox there is nothing to scale against, so the fixed size is
    # the only sizing the document has and taking it away would collapse it.
    if "viewBox" not in root.attrib:
        raise RenderError("Graphviz produced an SVG with no viewBox to scale against")
    root.set("preserveAspectRatio", "xMidYMid meet")

    _sanitise(root, links=links, local_links=local_links, tooltips=tooltips)
    if not tooltips:
        _drop(root, _TITLE_TAG)
    if not (links or local_links):
        _unwrap_anchors(root)
    if prefix:
        _prefix_ids(root, prefix)
    _hoist_text_attributes(root)
    # After the prefixing, because a symbol id belongs to the document rather
    # than to one drawing of it: a shared library is named by every view.
    local = icons is None
    library = IconLibrary() if icons is None else icons
    _hoist_icons(root, library)
    if local:
        defs = library.defs()
        if defs is not None:
            root.insert(0, defs)
    return ElementTree.tostring(root, encoding="unicode")


def _sanitise(root: ElementTree.Element, *, links: bool, local_links: bool, tooltips: bool) -> None:
    """Remove every element and attribute that could run or navigate."""
    for parent in list(root.iter()):
        for child in [child for child in parent if child.tag in _FORBIDDEN_TAGS]:
            parent.remove(child)
    for element in root.iter():
        for name in [
            name
            for name, value in element.attrib.items()
            if _is_unsafe(
                name,
                str(value),
                tag=element.tag,
                links=links,
                local_links=local_links,
                tooltips=tooltips,
            )
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


def _is_unsafe(
    name: str, value: str, *, tag: str, links: bool, local_links: bool, tooltips: bool
) -> bool:
    """Is this attribute one no embedded diagram has any business carrying?

    Event handlers are matched by shape rather than by name: ``onload`` and
    ``onmouseover`` are the ones that matter today, but the list of SVG events
    is long and grows, and an unknown ``on*`` attribute is not something a
    layout engine ever needed to emit.
    """
    if name in _REFERENCE_ATTRIBUTES:
        return not _is_safe_reference(
            value, links=links, local_links=local_links, anchor=tag == _ANCHOR_TAG
        )
    if name == _TITLE_ATTRIBUTE:
        return not tooltips
    return name in _STRIPPED_ATTRIBUTES or name.lower().startswith("on")


def _is_safe_reference(value: str, *, links: bool, local_links: bool, anchor: bool) -> bool:
    """May this ``href`` stay?

    An inline picture always: a browser runs nothing inside an image, and this
    is how an ``--icons`` theme survives. A reference to another part of the
    same drawing (``<use href="#shape">``, a gradient, a clip path) always too —
    it fetches nothing and goes nowhere — *unless* it is on an anchor, where
    ``#`` is navigation like any other and would move the embedding page's
    fragment out from under it. Everything else is a link, and links are kept
    only when the caller asked for them: ``links`` for an absolute URL and
    ``local_links`` for a relative reference to a sibling file, which are two
    different questions — one leaves the site, the other cannot.
    """
    if value.startswith(_INLINE_IMAGE_PREFIX):
        return True
    if value.startswith("#"):
        return not anchor
    if links and value.lower().startswith(_LINK_SCHEMES):
        return True
    return local_links and _RELATIVE_LINK.fullmatch(value) is not None


def _hoist_text_attributes(root: ElementTree.Element) -> None:
    """State each inherited text property once instead of once per label.

    Only an attribute *every* ``<text>`` carries can move: an element that
    stated none would begin inheriting the hoisted value, which is a change to
    the picture and not to the markup. Among those, the value that appears most
    often goes to the root and is deleted from the elements that named it; the
    minority keep theirs, where it overrides what they now inherit. Graphviz
    emits these on ``<text>`` and nowhere else, so nothing else in the drawing
    can be affected by a value arriving from above.
    """
    texts = [element for element in root.iter() if element.tag == _TEXT_TAG]
    if not texts:
        return
    for name in _INHERITED_TEXT:
        values = [element.get(name) for element in texts]
        if any(value is None for value in values):
            continue
        common, _ = Counter(values).most_common(1)[0]
        root.set(name, str(common))
        for element in texts:
            if element.get(name) == common:
                del element.attrib[name]


def _hoist_icons(root: ElementTree.Element, library: IconLibrary) -> None:
    """Replace every inline picture with a reference to one shared copy.

    The ``<use>`` keeps the box the ``<image>`` was drawn in — the same ``x``,
    ``y``, ``width`` and ``height`` Graphviz computed — and the picture itself
    moves into the library. An ``<image>`` whose reference is not an inline
    picture is left alone: it is not artwork this renderer put there, and
    sharing something it does not recognise would be a guess.
    """
    for parent in list(root.iter()):
        for index, child in enumerate(parent):
            if child.tag != _IMAGE_TAG:
                continue
            href = next(
                (child.get(name) for name in sorted(_REFERENCE_ATTRIBUTES) if child.get(name)),
                None,
            )
            if href is None or not href.startswith(_INLINE_IMAGE_PREFIX):
                continue
            name = library.reference(href, child.get("preserveAspectRatio", ""))
            reference = ElementTree.Element(
                _USE_TAG,
                {
                    f"{{{XLINK_NAMESPACE}}}href": f"#{name}",
                    **{
                        attribute: value
                        for attribute in _IMAGE_GEOMETRY
                        if (value := child.get(attribute)) is not None
                    },
                },
            )
            parent[index] = reference


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
