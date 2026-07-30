"""Where every page of a bundle lives, and how one page links to another.

A report is a set of files that reference each other, so the file names are part
of its contract: a committed report is diffed, a published one is bookmarked, and
a link that moves because two devices were declared in a different order is a
diff nobody can read. Everything here is therefore a pure function of the
*content* of the inventory:

* a page name is the element's fully-qualified name reduced to a file-safe slug,
  lower-cased so that two names differing only in case cannot become one file on
  a case-insensitive filesystem;
* collisions are resolved by appending ``-2``, ``-3`` in **sorted** order rather
  than in load order, so a file name never depends on directory iteration;
* a cross-reference is stored bundle-relative (``devices/sw-core.md``) and turned
  into a page-relative link (``../devices/sw-core.md``) only when the page that
  holds it is written, which is what lets one collected report be emitted as
  Markdown or as HTML without re-deriving any of it.

The layout is deliberately shallow — three directories, one level each — because
the depth of a link is then a property of the *kind* of page rather than of the
namespace an element happens to sit in, and a diagram embedded in a site page can
link to a device page with one fixed prefix.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final

from netgraph.fsio import safe_file_stem
from netgraph.render.ids import slug as id_slug

__all__ = [
    "DEVICES_DIR",
    "DIAGRAMS_DIR",
    "SITES_DIR",
    "Layout",
    "anchor",
    "page_slug",
]

#: The three directories a bundle holds, beside its index.
DEVICES_DIR: Final = "devices"
SITES_DIR: Final = "sites"
DIAGRAMS_DIR: Final = "diagrams"

#: What the root page is called, per format. ``README.md`` rather than
#: ``index.md`` because a Markdown bundle is meant to be committed, and a forge
#: renders the README of a directory when somebody browses to it. The ``json``
#: document holds every page in one file, so its "paths" are extension-less
#: identifiers a consumer keys on rather than files anybody opens.
_INDEX: Final[Mapping[str, str]] = {"markdown": "README.md", "html": "index.html", "json": "index"}

#: File extension per format.
_SUFFIX: Final[Mapping[str, str]] = {"markdown": ".md", "html": ".html", "json": ""}

#: The drawing prefix for the whole-report scope, which has no site page of its
#: own. Never a site's stem: ``_names`` hands out ``root`` for the root namespace,
#: and the two must not both be ``overview``.
_OVERVIEW_STEM: Final = "overview"

#: Stands in for a name that slugs away to nothing at all — an element whose
#: whole name is punctuation, which the schema permits in a namespace segment.
_FALLBACK_SLUG: Final = "element"


def page_slug(name: str) -> str:
    """``sites/hq/sw-core`` → ``sites-hq-sw-core``, safe as a file stem.

    Built on :func:`netgraph.render.ids.slug`, which is the same reduction the
    SVG element ids use, so a page name and the id of the shape that links to it
    are derived from one definition. Two extra steps happen here and not there:
    the result is lower-cased (a file system may not distinguish two cases, an
    XML id must) and passed through :func:`~netgraph.fsio.safe_file_stem` (an
    element legitimately called ``con`` is a file Windows refuses to create).
    """
    reduced = id_slug(name).strip("-").lower() or _FALLBACK_SLUG
    return safe_file_stem(reduced)


def anchor(*parts: str | int) -> str:
    """A stable in-page anchor from ``parts``, e.g. ``vlan-10``.

    Anchors are emitted as explicit ``<a id="...">`` elements in Markdown as
    well as in HTML: a heading's anchor is whatever the renderer of the day
    derives from its text, and a report's own cross-references cannot be left to
    depend on that.
    """
    return page_slug("-".join(str(part) for part in parts if str(part) != ""))


@dataclass(frozen=True, slots=True)
class Layout:
    """The file name of every page, and the link from any page to any other."""

    #: ``markdown`` or ``html``.
    format: str
    #: Bundle-relative path of the overview page.
    index: str
    #: Fully-qualified element name → bundle-relative path of its page.
    devices: Mapping[str, str] = field(default_factory=dict)
    #: Namespace → bundle-relative path of its page. The root namespace is
    #: spelled ``""`` and gets a page like any other when elements sit in it.
    sites: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, format: str, *, devices: Iterable[str], sites: Iterable[str]) -> Layout:
        """Assign a page to every element and every site.

        Args:
            format: ``markdown`` or ``html``; decides the extension and the name
                of the index.
            devices: Fully-qualified names of the elements that get a page.
            sites: Namespaces that get a page.
        """
        suffix = _SUFFIX[format]
        return cls(
            format=format,
            index=_INDEX[format],
            devices=_names(devices, directory=DEVICES_DIR, suffix=suffix),
            sites=_names(sites, directory=SITES_DIR, suffix=suffix, fallback="root"),
        )

    def device(self, fqn: str) -> str:
        """The page of one element, or ``""`` when it has none."""
        return self.devices.get(fqn, "")

    def site(self, namespace: str) -> str:
        """The page of one site, or ``""`` when it has none."""
        return self.sites.get(namespace, "")

    def diagram(self, scope: str, layer: str) -> str:
        """The file a diagram is written to, for the formats that write one.

        Named after the *page* that holds it rather than after the namespace it
        draws: two namespaces differing only in case are two pages — the
        collision was already resolved when they were assigned — and deriving
        the drawing's name from the namespace again would give both of them one
        file and let the second overwrite the first.
        """
        page = self.sites.get(scope, "")
        stem = PurePosixPath(page).stem if page else _OVERVIEW_STEM
        return f"{DIAGRAMS_DIR}/{stem}-{page_slug(layer)}.svg"

    def link(self, target: str, *, frm: str, fragment: str = "") -> str:
        """``target`` as a link written on the page ``frm``.

        Both arguments are bundle-relative. The result is relative to ``frm``'s
        own directory, so the bundle can be moved, served from a subdirectory or
        opened from a ``file://`` URL without a single link breaking.
        """
        if not target:
            return f"#{fragment}" if fragment else ""
        suffix = f"#{fragment}" if fragment else ""
        if target == frm:
            return suffix or target.rpartition("/")[2]
        return _relative(frm, target) + suffix


def _names(
    subjects: Iterable[str], *, directory: str, suffix: str, fallback: str = ""
) -> dict[str, str]:
    """One file name per subject, collisions resolved in sorted order.

    Sorted rather than as-given: two elements whose names differ only in a
    character the slug folds away have to be told apart by a numeric suffix, and
    *which* of the two gets the plain name must not depend on the order the
    loader walked the directory tree in.
    """
    assigned: dict[str, str] = {}
    taken: set[str] = set()
    for subject in sorted(set(subjects)):
        stem = page_slug(subject) if subject else (fallback or _FALLBACK_SLUG)
        candidate = stem
        counter = 1
        while candidate in taken:
            counter += 1
            candidate = f"{stem}-{counter}"
        taken.add(candidate)
        assigned[subject] = f"{directory}/{candidate}{suffix}"
    return assigned


def _relative(frm: str, target: str) -> str:
    """``target`` as a path relative to the *directory* holding ``frm``."""
    source = PurePosixPath(frm).parent.parts
    destination = PurePosixPath(target).parts
    shared = 0
    for left, right in zip(source, destination[:-1], strict=False):
        if left != right:
            break
        shared += 1
    ascend = [".."] * (len(source) - shared)
    return "/".join([*ascend, *destination[shared:]])
