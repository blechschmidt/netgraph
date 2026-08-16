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
* **Loading is safe.** Only :data:`~netviz.loader.documents.StrictSafeLoader`
  is used, so a hostile inventory cannot construct arbitrary Python objects.
  That name is bound at import time to the same strictness mixed over either the
  libyaml or the pure-Python parser, whichever this PyYAML build has; see
  :mod:`netviz.loader.documents`.
"""

from __future__ import annotations

from netviz.loader.cache import (
    CACHE_DIR_ENV_VAR,
    DEFAULT_MAX_BYTES,
    DISABLE_ENV_VAR,
    CachedFile,
    CachedSlot,
    CacheInfo,
    CacheStats,
    DocumentCache,
    Identity,
    clear_cache,
    disabled_by_environment,
    inspect_cache,
    inventory_cache_dir,
    open_cache,
    resolve_cache_root,
)
from netviz.loader.documents import (
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
from netviz.loader.ignore import (
    IGNORE_FILE_NAME,
    IgnoreRule,
    IgnoreRuleSet,
    IgnoreStack,
    parse_ignore_file,
)
from netviz.loader.inventory import (
    Inventory,
    LoadError,
    Resolution,
    SourceLocation,
    namespace_of,
    qualify,
    short_name,
    subset,
)
from netviz.loader.provenance import FieldPath, Provenance, Site
from netviz.loader.ranges import (
    MAX_INTERFACES_PER_DOCUMENT,
    MAX_SPANS_PER_RANGE,
    Expansion,
    RangeError,
    RangePattern,
    Span,
    expand_interfaces,
    parse_range,
    substitute,
)
from netviz.loader.templates import (
    INHERIT_KEY,
    ResolvedTemplate,
    TemplateRegistry,
    merge_spec,
    resolved_spec,
)
from netviz.loader.tree import (
    STREAM_NAME,
    YAML_SUFFIXES,
    InventoryFile,
    Overlay,
    iter_inventory_files,
    load_stream,
    load_tree,
)

__all__ = [
    "CACHE_DIR_ENV_VAR",
    "DEFAULT_MAX_BYTES",
    "DISABLE_ENV_VAR",
    "HAVE_LIBYAML",
    "IGNORE_FILE_NAME",
    "INHERIT_KEY",
    "LOADER_ENV_VAR",
    "MAX_INTERFACES_PER_DOCUMENT",
    "MAX_SPANS_PER_RANGE",
    "STREAM_NAME",
    "YAML_SUFFIXES",
    "CacheInfo",
    "CacheStats",
    "CachedFile",
    "CachedSlot",
    "DocumentCache",
    "Expansion",
    "FieldPath",
    "Identity",
    "IgnoreRule",
    "IgnoreRuleSet",
    "IgnoreStack",
    "Inventory",
    "InventoryFile",
    "LoadError",
    "NodeLoader",
    "Overlay",
    "Provenance",
    "PureStrictSafeLoader",
    "RangeError",
    "RangePattern",
    "RawDocument",
    "Resolution",
    "ResolvedTemplate",
    "Site",
    "SourceLocation",
    "Span",
    "StrictSafeLoader",
    "TemplateRegistry",
    "YamlSyntaxError",
    "clear_cache",
    "disabled_by_environment",
    "expand_interfaces",
    "inspect_cache",
    "inventory_cache_dir",
    "iter_inventory_files",
    "libyaml_loader",
    "load_stream",
    "load_tree",
    "merge_spec",
    "namespace_of",
    "open_cache",
    "parse_documents",
    "parse_ignore_file",
    "parse_range",
    "qualify",
    "read_documents",
    "resolve_cache_root",
    "resolved_spec",
    "select_loader",
    "short_name",
    "subset",
    "substitute",
]
