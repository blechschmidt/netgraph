"""What the server knows: the open buffers, the tree they belong to, its findings.

One rule shapes everything here: **the answer the server gives is the answer
``netgraph validate`` would give about the text on screen.** So the inventory is
loaded through an :class:`~netgraph.loader.Overlay` carrying every unsaved
buffer, and it is reloaded — not patched — whenever one of them changes. An
inventory is milliseconds to load and the parse cache makes the files nobody
touched free; a server that tried to keep an incrementally-updated model in step
with a folder that other tools are also writing to would be fast and wrong.

Two modes
---------

**Folder.** The client opened a workspace. There is a root, the whole tree is
loaded, and cross-document rules mean what they say: a cable endpoint resolves
or it does not.

**Lone file.** The client opened one document with no workspace — ``$EDITOR
switch.yaml`` from anywhere. There is no tree to resolve against, so the file is
loaded on its own with :func:`~netgraph.loader.load_stream` and the rules that
can only be judged against a tree are held back (:data:`LONE_FILE_RULES`).
Reporting "cable endpoint references an unknown device" against every endpoint
of a file that is *supposed* to reference other files would make the mode
useless. Everything a single document can be wrong about on its own — its
schema, its syntax, its own interfaces, addresses and VLANs — is still reported.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from netgraph.config import Config, ValidationConfig, load_config
from netgraph.diagnostics import Report, build_report
from netgraph.loader import DocumentCache, Inventory, Overlay, load_stream, load_tree
from netgraph.loader.tree import YAML_SUFFIXES
from netgraph.lsp.text import Encoding, TextDocument
from netgraph.lsp.uri import path_to_uri, relative_to, uri_to_path
from netgraph.validate import Finding, validate

__all__ = ["LONE_FILE_RULES", "Analysis", "Workspace"]

#: Rules a single document cannot be judged by, held back in lone-file mode.
#: Every one of them asks a question about the *rest* of the inventory — does
#: this name resolve, is this element reachable, is anything patched into this —
#: and in a tree of one file the answer is always the alarming one. They are
#: reported in full as soon as a folder is open.
LONE_FILE_RULES: Final = frozenset(
    {
        "E001",  # a cable endpoint references an unknown device
        "E015",  # an adapter's 'attached_to' names no element
        "E016",  # a tunnel endpoint references an unknown element
        "E018",  # a tunnel's 'over' names no tunnel
        "E021",  # a cable terminates on a position the panel does not have
        "E023",  # a patch panel is named where an active element is required
        "E038",  # a power input names an outlet that does not exist
        "E043",  # a group names a member the inventory does not declare
        "W103",  # a device terminates no cable and hosts no adapter
        "W121",  # the topology graph is disconnected
        "W125",  # an overlay terminates where its underlay does not reach
        "W128",  # a 'tunnel' interface is named by no tunnel document
        "W133",  # a cabled patch-panel position is coupled to a dangling one
        "W135",  # a BGP neighbour address resolves to no element
        "W137",  # a device declares a power draw but no power path
        "W138",  # diagram geometry names an element the inventory does not have
        "I002",  # an interface is enabled but terminates no cable
    }
)


@dataclass(frozen=True, slots=True)
class Analysis:
    """One load of the inventory and everything derived from it."""

    #: The root the paths in :attr:`report` are relative to. In lone-file mode
    #: this is the file's own directory, so a URI can still be rebuilt from a
    #: relative path.
    root: Path
    inventory: Inventory
    findings: tuple[Finding, ...]
    report: Report
    #: Files the analysis covers, so diagnostics can be cleared from the ones it
    #: no longer has anything to say about.
    files: frozenset[str] = frozenset()


@dataclass(eq=False)
class Workspace:
    """The documents the client has open and the tree they are part of."""

    #: ``None`` until ``initialize`` says whether a folder was opened.
    root: Path | None = None
    encoding: Encoding = Encoding.UTF16
    cache: DocumentCache | None = None
    _documents: dict[str, TextDocument] = field(default_factory=dict, repr=False)
    _analysis: Analysis | None = field(default=None, init=False, repr=False)
    _config: Config | None = field(default=None, init=False, repr=False)

    # -- open documents --------------------------------------------------

    @property
    def documents(self) -> Mapping[str, TextDocument]:
        return self._documents

    def get(self, uri: str) -> TextDocument | None:
        return self._documents.get(uri)

    def open(self, uri: str, text: str, version: int, language_id: str = "yaml") -> TextDocument:
        document = TextDocument(uri=uri, text=text, version=version, language_id=language_id)
        self._documents[uri] = document
        self.invalidate()
        return document

    def update(self, document: TextDocument) -> None:
        self._documents[document.uri] = document
        self.invalidate()

    def close(self, uri: str) -> None:
        if self._documents.pop(uri, None) is not None:
            self.invalidate()

    def invalidate(self) -> None:
        """Forget the analysis; the next request reloads."""
        self._analysis = None

    # -- paths -----------------------------------------------------------

    @property
    def is_folder(self) -> bool:
        return self.root is not None

    def relative_of(self, uri: str) -> str | None:
        """``uri`` as a path below the root, or ``None`` if it is outside it."""
        path = uri_to_path(uri)
        if path is None or self.root is None:
            return None
        return relative_to(path, self.root)

    def uri_of(self, relative: str, root: Path | None = None) -> str:
        """The URI of a path relative to the inventory root."""
        base = root if root is not None else self.root
        return path_to_uri((base or Path.cwd()) / relative)

    def is_inventory_file(self, uri: str) -> bool:
        """Is ``uri`` a document this server has anything to say about?"""
        path = uri_to_path(uri)
        return path is not None and path.suffix.lower() in YAML_SUFFIXES

    def buffers(self) -> dict[str, str]:
        """Every open document that lives below the root, keyed by relative path."""
        found: dict[str, str] = {}
        for uri, document in self._documents.items():
            relative = self.relative_of(uri)
            if relative is not None:
                found[relative] = document.text
        return found

    def text_of(self, relative: str, root: Path | None = None) -> str:
        """What ``relative`` holds: the open buffer if there is one, else the disk."""
        base = root if root is not None else self.root
        if base is None:
            return ""
        uri = path_to_uri(base / relative)
        document = self._documents.get(uri)
        if document is not None:
            return document.text
        for candidate_uri, candidate in self._documents.items():
            if self.relative_of(candidate_uri) == relative:
                return candidate.text
        try:
            return (base / relative).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return ""

    # -- configuration ---------------------------------------------------

    def config(self) -> Config:
        """``netgraph.toml`` as it stands, loaded once per server run."""
        if self._config is None:
            self._config = load_config(self.root) if self.root is not None else Config()
        return self._config

    def reload_config(self) -> None:
        self._config = None
        self.invalidate()

    @property
    def validation(self) -> ValidationConfig:
        return self.config().validation

    # -- analysis --------------------------------------------------------

    def analysis(self) -> Analysis:
        """The current inventory and its findings, loaded on first use per edit."""
        if self._analysis is None:
            self._analysis = self._load()
        return self._analysis

    def _load(self) -> Analysis:
        if self.root is None:
            return self._load_lone()
        buffers = self.buffers()
        inventory = load_tree(
            self.root,
            keep_provenance=True,
            overlay=Overlay(files=dict(buffers)) if buffers else None,
        )
        findings = tuple(validate(inventory, self.validation))
        report = build_report(inventory, findings, base=self.root)
        return Analysis(
            root=self.root,
            inventory=inventory,
            findings=findings,
            report=report,
            files=frozenset(self._files_of(inventory, buffers)),
        )

    def _load_lone(self) -> Analysis:
        """The single open document, loaded as a stream of its own."""
        uri, document = next(iter(self._documents.items()), ("", None))
        path = uri_to_path(uri) if uri else None
        name = path.name if path is not None else "stream.yaml"
        root = path.parent if path is not None else Path.cwd()
        text = document.text if document is not None else ""
        inventory = load_stream(text, name=name, keep_provenance=True)
        findings = tuple(
            finding
            for finding in validate(inventory, self.validation)
            if finding.rule not in LONE_FILE_RULES
        )
        report = build_report(inventory, findings, base=root)
        return Analysis(
            root=root,
            inventory=inventory,
            findings=findings,
            report=report,
            files=frozenset({name}),
        )

    @staticmethod
    def _files_of(inventory: Inventory, buffers: Mapping[str, str]) -> Iterator[str]:
        """Every file the analysis has an opinion about."""
        yield from buffers
        for source in inventory.sources.values():
            yield source.relative
        for source in inventory.layout_sources.values():
            yield source.relative
        for error in inventory.errors:
            if error.relative is not None:
                yield error.relative
