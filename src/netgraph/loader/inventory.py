"""The result of loading an inventory tree.

An :class:`Inventory` indexes every element by its **fully-qualified name**: the
directory the document was found in, plus ``metadata.name``. So
``sites/berlin/rack1/sw1.yaml`` declaring ``name: sw1`` becomes
``sites/berlin/rack1/sw1``.

The directory tree carries no semantics of its own (§2) — it exists to keep
names readable and locally unique. References are therefore written with the
plain name and resolved *outwards*: the referring element's own namespace
first, then each ancestor namespace, then the whole inventory
(:meth:`Inventory.lookup`). Two elements in different namespaces may share a
short name; a reference is only rejected if it stays ambiguous after the
namespace-local and ancestor lookups have failed.

Loading never raises for a bad document: problems are collected as
:class:`LoadError` records, so one broken file cannot hide the rest.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from netgraph.errors import SchemaIssue, format_path
from netgraph.models import Adapter, Cable, Device, Element

__all__ = [
    "Inventory",
    "LoadError",
    "Resolution",
    "SourceLocation",
    "namespace_of",
    "qualify",
    "short_name",
]


def qualify(namespace: str, name: str) -> str:
    """Join a namespace and a short name into a fully-qualified name."""
    return f"{namespace}/{name}" if namespace else name


def namespace_of(fqn: str) -> str:
    """The namespace part of a fully-qualified name (``""`` at the root)."""
    namespace, separator, _ = fqn.rpartition("/")
    return namespace if separator else ""


def short_name(fqn: str) -> str:
    """The ``metadata.name`` part of a fully-qualified name."""
    return fqn.rpartition("/")[2]


def _ancestors(namespace: str) -> Iterator[str]:
    """Yield ``namespace`` and every parent of it, nearest first, root last."""
    current = namespace
    while current:
        yield current
        current = namespace_of(current)
    yield ""


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Where an element was declared (§2.2)."""

    #: Absolute path of the file.
    path: Path
    #: The same file relative to the inventory root, POSIX style.
    relative: str
    #: 0-based document index within the file.
    index: int
    #: 1-based line the document starts on, when the parser could report one.
    line: int | None = None

    def __str__(self) -> str:
        suffix = f":{self.line}" if self.line is not None else ""
        return f"{self.relative}#{self.index}{suffix}"


@dataclass(frozen=True, slots=True)
class LoadError:
    """A problem found while loading, tied to the file and line that caused it.

    Instances are collected rather than raised so that a single unreadable file
    or malformed document does not hide the rest of the inventory.
    """

    message: str
    #: Absolute path of the offending file, when the problem has one.
    path: Path | None = None
    #: The same file relative to the inventory root, POSIX style.
    relative: str | None = None
    #: 1-based line, when the parser or the node tree could supply one.
    line: int | None = None
    #: 1-based column; only YAML syntax errors carry one.
    column: int | None = None
    #: 0-based document index within the file.
    index: int | None = None
    #: Field path inside the document, ``()`` for a whole-document problem.
    field_path: tuple[str | int, ...] = ()
    #: Rule id from ``docs/schema.md`` §10, when one applies.
    rule: str | None = None

    @property
    def location(self) -> str:
        """Provenance in ``sites/hq/sw.yaml#0:17`` notation (``-`` when unknown)."""
        if self.relative is None:
            return "-" if self.path is None else str(self.path)
        text = self.relative
        if self.index is not None:
            text += f"#{self.index}"
        if self.line is not None:
            text += f":{self.line}"
            if self.column is not None:
                text += f":{self.column}"
        return text

    def __str__(self) -> str:
        parts = [self.location]
        if self.field_path:
            parts.append(format_path(self.field_path))
        prefix = f"{self.rule}: " if self.rule else ""
        return f"{': '.join(parts)}: {prefix}{self.message}"

    @classmethod
    def from_issue(
        cls,
        issue: SchemaIssue,
        *,
        path: Path,
        relative: str | PurePosixPath,
        index: int,
        line: int | None = None,
    ) -> LoadError:
        """Build a record from one :class:`~netgraph.errors.SchemaIssue`."""
        return cls(
            message=issue.message,
            path=path,
            relative=str(relative),
            line=line,
            index=index,
            field_path=issue.path,
            rule=issue.rule,
        )


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving a reference (see :meth:`Inventory.lookup`)."""

    #: Fully-qualified name of the element the reference denotes, if unambiguous.
    fqn: str | None = None
    element: Element | None = None
    #: Every candidate, when the name matched more than one element globally.
    ambiguous: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.element is not None

    def __bool__(self) -> bool:
        return self.found


@dataclass(eq=False)
class Inventory:
    """Every element of a loaded tree, indexed by fully-qualified name.

    The four element maps preserve load order (``NG-L005``), so iterating an
    inventory is deterministic and renderers produce stable output.
    """

    #: Root directory the tree was loaded from.
    root: Path
    #: Every element, keyed by fully-qualified name.
    elements: dict[str, Element] = field(default_factory=dict)
    #: The subset of :attr:`elements` that are devices (the five device kinds).
    devices: dict[str, Device] = field(default_factory=dict)
    cables: dict[str, Cable] = field(default_factory=dict)
    adapters: dict[str, Adapter] = field(default_factory=dict)
    #: Provenance of each element, keyed by fully-qualified name.
    sources: dict[str, SourceLocation] = field(default_factory=dict)
    #: Problems found while loading, in the order they were encountered.
    errors: list[LoadError] = field(default_factory=list)

    #: namespace -> short name -> fully-qualified name.
    _by_namespace: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    #: short name -> every fully-qualified name using it, in load order.
    _by_short_name: dict[str, list[str]] = field(default_factory=dict, repr=False)

    # -- population ------------------------------------------------------

    def add(self, element: Element, *, namespace: str, source: SourceLocation) -> str | None:
        """Index ``element`` under ``namespace``.

        Returns:
            The fully-qualified name, or ``None`` when the name is already taken
            in that namespace. The caller reports the clash (``NG-N002``) and
            the first declaration wins, which keeps loading deterministic.
        """
        fqn = qualify(namespace, element.metadata.name)
        if fqn in self.elements:
            return None

        self.elements[fqn] = element
        if isinstance(element, Cable):
            self.cables[fqn] = element
        elif isinstance(element, Adapter):
            self.adapters[fqn] = element
        elif isinstance(element, Device):
            self.devices[fqn] = element

        self.sources[fqn] = source
        self._by_namespace.setdefault(namespace, {})[element.metadata.name] = fqn
        self._by_short_name.setdefault(element.metadata.name, []).append(fqn)
        return fqn

    def record(self, error: LoadError) -> None:
        """Append a problem to :attr:`errors`."""
        self.errors.append(error)

    # -- lookup ----------------------------------------------------------

    def lookup(self, name: str, *, namespace: str = "") -> Resolution:
        """Resolve a reference written in ``namespace``.

        The order is: the namespace itself, then each ancestor namespace
        (nearest first, the root last), then the inventory as a whole. A name
        containing ``/`` is tried relative to ``namespace`` first and as a
        fully-qualified name second.

        A global match only counts when exactly one element carries the short
        name; otherwise the result reports every candidate in
        :attr:`Resolution.ambiguous` so the caller can name them all.
        """
        if "/" in name:
            for candidate in (qualify(namespace, name), name):
                element = self.elements.get(candidate)
                if element is not None:
                    return Resolution(fqn=candidate, element=element)
            return Resolution()

        for scope in _ancestors(namespace):
            fqn = self._by_namespace.get(scope, {}).get(name)
            if fqn is not None:
                return Resolution(fqn=fqn, element=self.elements[fqn])

        matches = self._by_short_name.get(name, ())
        if len(matches) == 1:
            fqn = matches[0]
            return Resolution(fqn=fqn, element=self.elements[fqn])
        return Resolution(ambiguous=tuple(matches))

    def resolve(self, name: str, *, namespace: str = "") -> Element | None:
        """The element ``name`` denotes in ``namespace``, or ``None``."""
        return self.lookup(name, namespace=namespace).element

    def resolve_fqn(self, name: str, *, namespace: str = "") -> str | None:
        """The fully-qualified name ``name`` denotes in ``namespace``, or ``None``."""
        return self.lookup(name, namespace=namespace).fqn

    def namespace_for(self, fqn: str) -> str:
        """The namespace an element was loaded into."""
        return namespace_of(fqn)

    def source_of(self, fqn: str) -> SourceLocation | None:
        """Where the element called ``fqn`` was declared."""
        return self.sources.get(fqn)

    def names_in(self, namespace: str) -> Mapping[str, str]:
        """Short name to fully-qualified name for one namespace."""
        return dict(self._by_namespace.get(namespace, {}))

    @property
    def namespaces(self) -> tuple[str, ...]:
        """Every namespace that holds at least one element, sorted."""
        return tuple(sorted(self._by_namespace))

    @property
    def interface_owners(self) -> dict[str, Device | Adapter]:
        """Elements that own interfaces and can therefore be cabled (§4.2)."""
        return {**self.devices, **self.adapters}

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    # -- container protocol ----------------------------------------------

    def __iter__(self) -> Iterator[Element]:
        return iter(self.elements.values())

    def __len__(self) -> int:
        return len(self.elements)

    def __contains__(self, fqn: object) -> bool:
        return fqn in self.elements

    def __getitem__(self, fqn: str) -> Element:
        return self.elements[fqn]

    def get(self, fqn: str) -> Element | None:
        """The element with this exact fully-qualified name, or ``None``."""
        return self.elements.get(fqn)

    def __repr__(self) -> str:
        return (
            f"Inventory(root={str(self.root)!r}, elements={len(self.elements)}, "
            f"devices={len(self.devices)}, cables={len(self.cables)}, "
            f"adapters={len(self.adapters)}, errors={len(self.errors)})"
        )
