"""Where each field of a rewritten document actually came from.

Two loader stages rewrite a document between parsing and model validation:
interface ranges are expanded (:mod:`netgraph.loader.ranges`) and a device
template is merged underneath a device's own ``spec``
(:mod:`netgraph.loader.templates`). Both move fields around, and the second one
moves them *between files*. A diagnostic that still pointed at
``spec.interfaces[17].mtu`` of the document the user wrote would then name a
line that either says something else or does not exist.

A :class:`Provenance` is the map back. It is deliberately not a per-field table
— a 48-port switch has thousands of leaves — but a small set of **redirects**,
each rewriting one path prefix onto a ``(document, path)`` pair::

    ("spec", "interfaces", 17)  ->  templates/c9200l.yaml#0, ("spec", "interfaces", 2)

Resolution takes the longest redirect that is a prefix of the path being looked
up and appends whatever is left, so one redirect per interface covers every
field inside it, and a redirect on a single key (``mtu`` overridden by the
device, the rest inherited) wins over the one on its entry. A path that matches
no redirect resolves to itself in :attr:`Provenance.base`, which is the document
the element was declared in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from netgraph.loader.documents import RawDocument

__all__ = ["FieldPath", "Provenance", "Site"]

#: A path into a document: mapping keys and sequence indices, outermost first.
FieldPath = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class Site:
    """One resolved location: a document, a path inside it, and its line."""

    document: RawDocument
    path: FieldPath

    @property
    def mark(self) -> tuple[int, int] | None:
        """1-based line and column of the value, or of its closest ancestor."""
        return self.document.mark_for(self.path)

    @property
    def line(self) -> int | None:
        """1-based line of the value, or of the closest ancestor that exists."""
        return self.document.line_for(self.path)

    @property
    def column(self) -> int | None:
        """1-based column of the value, or of the closest ancestor that exists."""
        mark = self.mark
        return None if mark is None else mark[1]

    @property
    def file(self) -> Path:
        return self.document.path

    @property
    def relative(self) -> str:
        return self.document.relative.as_posix()

    @property
    def index(self) -> int:
        return self.document.index

    def __str__(self) -> str:
        line = self.line
        suffix = f":{line}" if line is not None else ""
        return f"{self.relative}#{self.index}{suffix}"


@dataclass(frozen=True, slots=True)
class Provenance:
    """The redirect table of one rewritten document.

    Instances are immutable; :meth:`with_redirects` builds the next stage's
    table from this one, which is what lets a template that itself inherits from
    another template keep pointing every field at the file that wrote it.
    """

    #: The document the element was declared in. Paths with no redirect resolve
    #: here, unchanged.
    base: RawDocument
    #: ``output prefix -> where it came from``. Never contains the empty prefix.
    redirects: Mapping[FieldPath, Site] = field(default_factory=dict)

    def locate(self, path: Sequence[str | int]) -> Site:
        """Where the value at ``path`` in the rewritten document was written.

        The longest redirect that is a prefix of ``path`` wins; the remainder of
        the path is appended to the redirect's target. Paths are at most a
        handful of components long, so this is a handful of dict lookups rather
        than a scan of the table.
        """
        key: FieldPath = tuple(path)
        if self.redirects:
            for cut in range(len(key), 0, -1):
                site = self.redirects.get(key[:cut])
                if site is not None:
                    return Site(site.document, site.path + key[cut:])
        return Site(self.base, key)

    def with_redirects(self, redirects: Mapping[FieldPath, Site]) -> Provenance:
        """A table for the next rewrite of the same document.

        The new redirects *replace* rather than extend the old ones: a stage
        that reorders ``spec.interfaces`` has already resolved every entry's
        origin through this table, so keeping the stale entries would let a
        pre-reorder prefix shadow the post-reorder one.
        """
        return Provenance(base=self.base, redirects=dict(redirects))
