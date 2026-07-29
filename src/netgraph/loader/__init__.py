"""Discovery and parsing of a YAML inventory folder tree.

:func:`load_tree` walks an inventory directory, parses every YAML document it
finds with a strict safe loader, validates each one against the schema of its
``kind`` and returns an :class:`Inventory` indexed by fully-qualified name --
the document's directory plus ``metadata.name``, so
``sites/berlin/rack1/sw1.yaml`` becomes ``sites/berlin/rack1/sw1``::

    inventory = load_tree(Path("inventory"))
    switch = inventory.resolve("sw1", namespace="sites/berlin/rack1")
    for problem in inventory.errors:
        print(problem)

Two properties matter to every layer above this one:

* **Loading is total.** A malformed file never aborts the walk; problems are
  collected as :class:`LoadError` records carrying the file and, where the
  parser can supply one, the line. Callers decide whether to proceed.
* **Loading is safe.** Only :data:`~netgraph.loader.documents.StrictSafeLoader`
  is used, so a hostile inventory cannot construct arbitrary Python objects.
  That name is bound at import time to the same strictness mixed over either the
  libyaml or the pure-Python parser, whichever this PyYAML build has; see
  :mod:`netgraph.loader.documents`.
"""

from __future__ import annotations

from netgraph.loader.documents import (
    HAVE_LIBYAML,
    LOADER_ENV_VAR,
    NodeLoader,
    PureStrictSafeLoader,
    RawDocument,
    StrictSafeLoader,
    YamlSyntaxError,
    libyaml_loader,
    parse_documents,
    read_documents,
    select_loader,
)
from netgraph.loader.ignore import (
    IGNORE_FILE_NAME,
    IgnoreRule,
    IgnoreRuleSet,
    IgnoreStack,
    parse_ignore_file,
)
from netgraph.loader.inventory import (
    Inventory,
    LoadError,
    Resolution,
    SourceLocation,
    namespace_of,
    qualify,
    short_name,
)
from netgraph.loader.tree import (
    STREAM_NAME,
    YAML_SUFFIXES,
    InventoryFile,
    iter_inventory_files,
    load_stream,
    load_tree,
)

__all__ = [
    "HAVE_LIBYAML",
    "IGNORE_FILE_NAME",
    "LOADER_ENV_VAR",
    "STREAM_NAME",
    "YAML_SUFFIXES",
    "IgnoreRule",
    "IgnoreRuleSet",
    "IgnoreStack",
    "Inventory",
    "InventoryFile",
    "LoadError",
    "NodeLoader",
    "PureStrictSafeLoader",
    "RawDocument",
    "Resolution",
    "SourceLocation",
    "StrictSafeLoader",
    "YamlSyntaxError",
    "iter_inventory_files",
    "libyaml_loader",
    "load_stream",
    "load_tree",
    "namespace_of",
    "parse_documents",
    "parse_ignore_file",
    "qualify",
    "read_documents",
    "select_loader",
    "short_name",
]
