"""Where a new element's document goes, decided the same way every time.

Nothing in the schema says which file a document lives in — the folder decides
the namespace and that is the end of netviz's interest (§2). But an editor
that dropped every new element into a file of its own, or appended everything to
whichever file it happened to open first, would turn a tidy tree into a mess one
click at a time. So placement follows the conventions
`docs/inventory-layout.md <../../docs/inventory-layout.md>`_ describes, in three
steps:

1. **An explicit file wins.** It must be a YAML file inside the inventory whose
   directory *is* the namespace asked for, because the directory is what makes
   the namespace true; a mismatch is refused rather than silently honoured.

2. **Reuse the file that already holds siblings.** If elements of this kind
   already live in this namespace, the new one joins them — unless the file
   holding them is named after the single element it contains, which is the
   layout's own signal for "one document per file, so this switch has a file and
   a ``git log``". Cables and hosts collected into one file attract more of the
   same; ``sw-north-acc-01.yaml`` does not.

3. **Otherwise, name a new file after the convention for the kind.** Links go
   into one collection per namespace — ``cables.yaml``, ``tunnels.yaml`` — the
   way ``examples/home-lab/cables/links.yaml`` collects a patch record.
   Everything else gets a file named after the element, which is what makes a
   diff touching one switch touch one file.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from netviz.edit.errors import PlacementError
from netviz.fsio import safe_file_stem
from netviz.loader.tree import YAML_SUFFIXES

__all__ = [
    "COLLECTION_STEMS",
    "FileFacts",
    "check_file",
    "choose_file",
    "namespace_of_file",
    "normalise_file",
]

#: Kinds whose documents are collected into one file per namespace, and what
#: that file is called. The first two are links: they are meaningless without the
#: two elements they join, which is exactly the case the layout guide says to
#: collect rather than to give a file each. The three annotations (§21) are
#: collected for a different reason with the same conclusion — a callout is a
#: sentence, and a file per sentence is a directory listing nobody can read.
#: They share one file, because somebody reviewing what the diagram *says* wants
#: to read it in one place, and because the three are edited together.
COLLECTION_STEMS: Final[dict[str, str]] = {
    "cable": "cables",
    "tunnel": "tunnels",
    "note": "annotations",
    "area": "annotations",
    "legend": "annotations",
}

#: The suffix a file netviz creates gets. ``.yml`` is read but never written:
#: one spelling per tree keeps ``git`` history and shell globs simple.
SUFFIX: Final = ".yaml"


@dataclass(frozen=True, slots=True)
class FileFacts:
    """What the tree knows about one file, for the purpose of placing into it.

    Attributes:
        relative: The file, POSIX style, relative to the inventory root.
        kinds: One entry per element document in it, in document order.
        names: The ``metadata.name`` of each of those documents.
    """

    relative: str
    kinds: tuple[str, ...]
    names: tuple[str, ...]

    @property
    def namespace(self) -> str:
        parent = PurePosixPath(self.relative).parent.as_posix()
        return "" if parent == "." else parent

    @property
    def stem(self) -> str:
        return PurePosixPath(self.relative).stem

    @property
    def is_element_file(self) -> bool:
        """Is this a file named after the one element it holds?

        That is the layout's marker for a document somebody wants to own a file,
        and the one case where a new sibling should *not* be appended to it.
        """
        return len(self.names) == 1 and self.stem == self.names[0]


def namespace_of_file(relative: str) -> str:
    """The namespace a file's folder names (``""`` at the root)."""
    parent = PurePosixPath(relative.replace("\\", "/")).parent.as_posix()
    return "" if parent == "." else parent


def check_file(requested: str) -> str:
    """``requested`` as a POSIX path the inventory would actually read.

    Raises:
        PlacementError: The path escapes the inventory, is not a YAML file, or
            sits somewhere discovery skips.
    """
    text = requested.replace("\\", "/").strip()
    if not text:
        raise PlacementError("the file to write to cannot be empty")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise PlacementError(
            f"{requested!r} must be a path inside the inventory, relative to its root"
        )
    if not path.name.lower().endswith(YAML_SUFFIXES):
        raise PlacementError(
            f"{requested!r} is not a file the inventory would read; it has to end in "
            f"{' or '.join(YAML_SUFFIXES)} (NV-L001)"
        )
    if any(part.startswith((".", "_")) for part in path.parts):
        raise PlacementError(
            f"{requested!r} sits under a path component starting with '.' or '_', which the "
            f"loader skips (NV-L002), so nothing written there would be part of the inventory"
        )
    return path.as_posix()


def normalise_file(requested: str, *, namespace: str) -> str:
    """:func:`check_file`, plus the rule that a folder *is* a namespace.

    Raises:
        PlacementError: As :func:`check_file`, or the file sits in a directory
            other than ``namespace``.
    """
    checked = check_file(requested)
    parent = namespace_of_file(checked)
    if parent != namespace:
        raise PlacementError(
            f"{requested!r} is in namespace {parent or '(root)'!r}, but the element was asked "
            f"for in {namespace or '(root)'!r}; a document's folder is its namespace, so the "
            f"two cannot disagree"
        )
    return checked


def choose_file(
    *,
    kind: str,
    namespace: str,
    name: str,
    files: Mapping[str, FileFacts],
    requested: str | None = None,
) -> str:
    """The file a new ``kind`` element called ``name`` should be written to.

    Args:
        files: Every file the tree holds, keyed by relative POSIX path.
        requested: An explicit ``--file``, which is checked and returned as is.

    Raises:
        PlacementError: ``requested`` cannot be used (see :func:`normalise_file`).
    """
    if requested is not None:
        return normalise_file(requested, namespace=namespace)

    siblings = [
        facts
        for facts in files.values()
        if facts.namespace == namespace and kind in facts.kinds and not facts.is_element_file
    ]
    if siblings:
        # The file that already holds the most of this kind, and the first such
        # file in path order when several tie -- so the answer does not depend
        # on the order a mapping happened to be built in.
        counts = Counter({facts.relative: facts.kinds.count(kind) for facts in siblings})
        return min(counts, key=lambda relative: (-counts[relative], relative))

    stem = COLLECTION_STEMS.get(kind) or safe_file_stem(name)
    return (
        PurePosixPath(namespace, f"{stem}{SUFFIX}").as_posix() if namespace else f"{stem}{SUFFIX}"
    )
