"""What every name in the tree points at, and where each one is written.

Hover, go-to-definition, find-references and rename are four questions about one
table: which spans of which files are *names*, and what each of them denotes.
Building that table once and answering all four from it is what keeps them
consistent — a reference the hover resolves and the definition does not would be
a bug nobody could explain.

The table is not guessed from the text. Which fields hold references is decided
by :mod:`netviz.edit.references`, which reads it off the models, and *where*
each one is written comes from the loader's provenance — so a value a device
inherited from a template resolves to the template's file, and jumping to the
definition of an inherited interface lands on the line that declares it rather
than on the device that merely uses it.

It is built lazily and thrown away whole on the next edit. An inventory reloads
in milliseconds; an index kept in step with a folder by hand would not survive
its first surprise.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from netviz.edit.references import (
    NameIndex,
    Reference,
    ReferenceRole,
    references_of,
)
from netviz.loader.inventory import Inventory, SourceLocation
from netviz.lsp.locate import Located, range_at, value_at
from netviz.lsp.text import Encoding, Position, Range
from netviz.models import Element, Pdu

__all__ = ["Anchor", "AnchorKind", "SemanticIndex"]


class AnchorKind(str, Enum):
    """What a span of text names."""

    #: ``metadata.name`` — the element's own declaration.
    ELEMENT_NAME = "element-name"
    #: ``spec.interfaces[].name`` — an interface's own declaration.
    INTERFACE_NAME = "interface-name"
    #: The element half of a reference to another document.
    REFERENCE = "reference"
    #: The interface or outlet half of one.
    REFERENCE_DETAIL = "reference-detail"


@dataclass(frozen=True, slots=True)
class Anchor:
    """One span of one file, and what it names."""

    kind: AnchorKind
    #: POSIX path of the file, relative to the inventory root.
    relative: str
    range: Range
    #: The element the span belongs to: the declaring element, or, for a
    #: reference, the element holding it.
    owner: str
    #: The element the span denotes: itself for a declaration, the resolved
    #: target for a reference. ``None`` when a reference resolves to nothing.
    target: str | None = None
    #: The interface or outlet named, when the span names one.
    detail: str | None = None
    #: The text exactly as it was written.
    written: str = ""
    role: ReferenceRole | None = None

    @property
    def located(self) -> Located:
        return Located(relative=self.relative, range=self.range)

    @property
    def is_reference(self) -> bool:
        return self.kind in {AnchorKind.REFERENCE, AnchorKind.REFERENCE_DETAIL}


class SemanticIndex:
    """Every name in one loaded inventory, keyed by the file it is written in."""

    def __init__(
        self,
        inventory: Inventory,
        *,
        text_of: Callable[[str], str],
        encoding: Encoding = Encoding.UTF16,
    ) -> None:
        self._inventory = inventory
        self._text_of = text_of
        self._encoding = encoding
        self._names = NameIndex(inventory.elements.keys())
        self._anchors: dict[str, list[Anchor]] | None = None
        self._lines: dict[str, list[str]] = {}

    # -- names -----------------------------------------------------------

    @property
    def names(self) -> NameIndex:
        return self._names

    def resolve(self, written: str, namespace: str) -> str | None:
        """The element ``written`` denotes when read from ``namespace``."""
        return self._names.lookup(written, namespace)

    # -- the table -------------------------------------------------------

    def anchors(self) -> Mapping[str, list[Anchor]]:
        """Every anchor, bucketed by the file it is written in."""
        if self._anchors is None:
            table: dict[str, list[Anchor]] = {}
            for anchor in self._build():
                table.setdefault(anchor.relative, []).append(anchor)
            for entries in table.values():
                entries.sort(key=lambda anchor: (anchor.range.start, anchor.range.end))
            self._anchors = table
        return self._anchors

    def anchors_in(self, relative: str) -> Sequence[Anchor]:
        return self.anchors().get(relative, ())

    def anchor_at(self, relative: str, position: Position) -> Anchor | None:
        """The innermost anchor the caret touches, or ``None``.

        The interface half of ``sw-home:eth0`` is nested inside nothing, but the
        two halves abut, so the caret between them belongs to the *device*: that
        is where a reader who has not typed the colon yet is looking.
        """
        found: Anchor | None = None
        for anchor in self.anchors_in(relative):
            if not anchor.range.touches(position):
                continue
            if found is None or _narrower(anchor.range, found.range):
                found = anchor
        return found

    # -- definitions -----------------------------------------------------

    def definition_of(self, fqn: str) -> Located | None:
        """Where ``fqn`` declares its name."""
        source = self._inventory.source_of(fqn)
        if source is None:
            return None
        return self._locate(source, ("metadata", "name"))

    def interface_definition(self, fqn: str, name: str) -> Located | None:
        """Where ``fqn`` declares the interface called ``name``."""
        element = self._inventory.get(fqn)
        source = self._inventory.source_of(fqn)
        if element is None or source is None:
            return None
        for index, interface in enumerate(_interfaces_of(element)):
            if interface == name:
                return self._locate(source, ("spec", "interfaces", index, "name"))
        if isinstance(element, Pdu):
            # Outlets are a numbering (``outlets: 1-24``) rather than a list of
            # declarations, so the whole range is the definition of any of them.
            return self._locate(source, ("spec", "outlets"))
        return None

    def references_to(self, fqn: str) -> list[Anchor]:
        """Every reference anchor in the tree that resolves to ``fqn``."""
        return [
            anchor
            for entries in self.anchors().values()
            for anchor in entries
            if anchor.kind is AnchorKind.REFERENCE and anchor.target == fqn
        ]

    # -- building --------------------------------------------------------

    def _build(self) -> Iterator[Anchor]:
        for fqn, element in self._inventory.elements.items():
            source = self._inventory.source_of(fqn)
            if source is None:  # pragma: no cover - every loaded element has one
                continue
            yield from self._declaration_anchors(fqn, element, source)
            yield from self._reference_anchors(fqn, element, source)

    def _declaration_anchors(
        self, fqn: str, element: Element, source: SourceLocation
    ) -> Iterator[Anchor]:
        located = self._locate(source, ("metadata", "name"))
        if located is not None:
            yield Anchor(
                kind=AnchorKind.ELEMENT_NAME,
                relative=located.relative,
                range=located.range,
                owner=fqn,
                target=fqn,
                written=element.metadata.name,
            )
        for index, name in enumerate(_interfaces_of(element)):
            site = self._locate(source, ("spec", "interfaces", index, "name"))
            if site is None:
                continue
            yield Anchor(
                kind=AnchorKind.INTERFACE_NAME,
                relative=site.relative,
                range=site.range,
                owner=fqn,
                target=fqn,
                detail=name,
                written=name,
            )

    def _reference_anchors(
        self, fqn: str, element: Element, source: SourceLocation
    ) -> Iterator[Anchor]:
        namespace = self._inventory.namespace_for(fqn)
        for reference in references_of(fqn, element):
            target = self._names.lookup(reference.target, namespace)
            yield from self._reference_spans(fqn, reference, target, source)

    def _reference_spans(
        self, fqn: str, reference: Reference, target: str | None, source: SourceLocation
    ) -> Iterator[Anchor]:
        site = source.locate(reference.path)
        if site is None:  # pragma: no cover - a parsed element always has one
            return
        written = value_at(site.document.data, site.path)
        if isinstance(written, Mapping):
            yield from self._mapping_spans(fqn, reference, target, source)
            return
        located = self._located(site.relative, site.mark)
        if located is None:
            return
        text = self._line(located.relative, located.range.start.line)
        element_range, detail_range = _split_reference(
            located.range, text, has_detail=reference.detail is not None
        )
        yield Anchor(
            kind=AnchorKind.REFERENCE,
            relative=located.relative,
            range=element_range,
            owner=fqn,
            target=target,
            detail=reference.detail,
            written=reference.target,
            role=reference.role,
        )
        if detail_range is not None and reference.detail is not None:
            yield Anchor(
                kind=AnchorKind.REFERENCE_DETAIL,
                relative=located.relative,
                range=detail_range,
                owner=fqn,
                target=target,
                detail=reference.detail,
                written=reference.detail,
                role=reference.role,
            )

    def _mapping_spans(
        self, fqn: str, reference: Reference, target: str | None, source: SourceLocation
    ) -> Iterator[Anchor]:
        """The ``{device: …, interface: …}`` spelling, whose halves are two nodes."""
        keys = _MAPPING_KEYS.get(reference.role)
        if keys is None:  # pragma: no cover - only endpoints and inputs have one
            return
        element_key, detail_key = keys
        located = self._locate(source, (*reference.path, element_key))
        if located is not None:
            yield Anchor(
                kind=AnchorKind.REFERENCE,
                relative=located.relative,
                range=located.range,
                owner=fqn,
                target=target,
                detail=reference.detail,
                written=reference.target,
                role=reference.role,
            )
        if reference.detail is None:
            return
        located = self._locate(source, (*reference.path, detail_key))
        if located is not None:
            yield Anchor(
                kind=AnchorKind.REFERENCE_DETAIL,
                relative=located.relative,
                range=located.range,
                owner=fqn,
                target=target,
                detail=reference.detail,
                written=reference.detail,
                role=reference.role,
            )

    # -- text ------------------------------------------------------------

    def _locate(self, source: SourceLocation, path: Sequence[str | int]) -> Located | None:
        site = source.locate(tuple(path))
        return None if site is None else self._located(site.relative, site.mark)

    def _located(self, relative: str, mark: tuple[int, int] | None) -> Located | None:
        if mark is None:
            return None
        line, column = mark
        text = self._text_of(relative)
        if not text:
            return None
        return Located(relative=relative, range=range_at(text, line, column, self._encoding))

    def _line(self, relative: str, number: int) -> str:
        lines = self._lines.get(relative)
        if lines is None:
            lines = self._text_of(relative).split("\n")
            self._lines[relative] = lines
        return lines[number].rstrip("\r") if 0 <= number < len(lines) else ""


#: The two keys the mapping spelling of a reference uses, per role.
_MAPPING_KEYS: Mapping[ReferenceRole, tuple[str, str]] = {
    ReferenceRole.ENDPOINT: ("device", "interface"),
    ReferenceRole.POWER_INPUT: ("pdu", "outlet"),
}


def _narrower(candidate: Range, current: Range) -> bool:
    """Is ``candidate`` the tighter of the two spans?"""
    if candidate.start != current.start:
        return candidate.start > current.start
    return candidate.end < current.end


def _split_reference(span: Range, text: str, *, has_detail: bool) -> tuple[Range, Range | None]:
    """Split ``device:interface`` into its two halves, by the first colon.

    Neither half may contain a colon — an element name and an interface name are
    both spelled out of a grammar that excludes it (``NG-N001``, §6.2) — so the
    first one is the separator and there is nothing to disambiguate.
    """
    if not has_detail:
        return span, None
    start = span.start.character
    end = span.end.character
    body = text[start:end]
    quoted = bool(body) and body[0] in "\"'"
    inner = body[1:-1] if quoted and len(body) >= 2 and body[-1] == body[0] else body
    offset = 1 if quoted else 0
    colon = inner.find(":")
    if colon == -1:
        return span, None
    line = span.start.line
    element = Range(Position(line, start + offset), Position(line, start + offset + colon))
    detail = Range(Position(line, start + offset + colon + 1), Position(line, end - offset))
    return element, detail


def _interfaces_of(element: Element) -> list[str]:
    """The interface names ``element`` declares, in document order."""
    interfaces = getattr(element.spec, "interfaces", None)
    if not isinstance(interfaces, Sequence):
        return []
    return [str(getattr(entry, "name", "")) for entry in interfaces]
