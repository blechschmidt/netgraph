"""The provenance keys a generated configuration carries, and how to read them.

A leaf module on purpose. :mod:`netgraph.export.config.header` writes these keys
and :mod:`netgraph.importer.config.common` reads them, and those two packages sit
on opposite sides of a dependency that already runs one way: ``netgraph.export``
reaches ``netgraph.plan``, which reaches ``netgraph.drift``, which reaches
``netgraph.importer``. A reader importing the writer's module would close that
into a cycle and ``import netgraph.importer`` would stop working — invisibly from
the CLI, which enters through ``netgraph.export`` and never notices.

So the three key names and the parser live here, where both sides can reach them
and neither reaches the other. Nothing else belongs in this module: it imports
nothing from netgraph at all, and that is the property worth keeping.
"""

from __future__ import annotations

from typing import Final

__all__ = ["DIALECT_KEY", "ELEMENT_KEY", "SOURCE_KEY", "parse_banner"]

#: Which dialect wrote the file. Spelled with a ``netgraph-`` prefix so the key
#: cannot collide with a directive of the format the comment sits in.
DIALECT_KEY: Final = "netgraph-dialect"

#: The fully-qualified element the file was generated from, followed by its kind
#: in parentheses: ``sites/north/core/rtr-01 (router)``.
ELEMENT_KEY: Final = "netgraph-element"

#: An inventory document the file was generated from, relative to the inventory
#: root. Repeated, once per document.
SOURCE_KEY: Final = "netgraph-source"

#: How much of a file is read looking for the banner. Every generated banner is
#: within the first ten lines; a bound keeps a reader pointed at a disk image
#: from scanning it.
MAX_BANNER_BYTES: Final = 4096


def parse_banner(text: str, marker: str = "#") -> dict[str, str]:
    """The ``netgraph-*`` keys of a generated file, if it has any.

    Reads only the leading comment block, and only the three keys it knows: a
    running configuration may legitimately begin with a comment holding anything
    at all, and scanning the whole file for a colon would find one.

    Args:
        text: The file, or its first few kilobytes.
        marker: The comment introducer of the format — ``!`` for FRR, ``#`` for
            everything else here. A caller that does not yet know which dialect
            wrote the file tries both; that is what it is asking.

    Returns:
        The keys found, with every :data:`SOURCE_KEY` value joined by commas
        since a file names one document per source. An empty mapping for a file
        netgraph did not write, which is the common case and not an error — it
        means the reader has to sniff.
    """
    found: dict[str, str] = {}
    sources: list[str] = []
    for line in text[:MAX_BANNER_BYTES].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith(marker):
            break
        key, separator, value = stripped[len(marker) :].strip().partition(":")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if key == SOURCE_KEY:
            sources.append(value)
        elif key in {DIALECT_KEY, ELEMENT_KEY}:
            found[key] = value
    if sources:
        found[SOURCE_KEY] = ",".join(sources)
    return found
