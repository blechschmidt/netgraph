"""netgraph -- declare network elements in YAML and render them as network graphs.

The package is organised in four layers:

* :mod:`netgraph.models` -- typed (pydantic) representations of network elements.
* :mod:`netgraph.loader` -- discovery and parsing of a YAML inventory folder tree.
* :mod:`netgraph.validate` -- semantic checks *across* documents, graded by the
  catalogue in :mod:`netgraph.rules` and filtered by :mod:`netgraph.config`.
* :mod:`netgraph.render` -- turning a loaded inventory into graph output formats.

:mod:`netgraph.graph` sits beside the renderers rather than under them: it hands
the same resolved topology to :mod:`networkx`, so connectivity questions are
answered by graph algorithms instead of by each consumer in turn.

The :mod:`netgraph.cli` module wires those layers into the ``netgraph`` console
script.
"""

from __future__ import annotations

from importlib import metadata

from netgraph.errors import (
    ConfigurationError,
    LoaderError,
    NetgraphError,
    RenderError,
    SchemaError,
    SchemaIssue,
    ValidationError,
)

try:
    __version__: str = metadata.version("netgraph")
except metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0.dev0"

__all__ = [
    "ConfigurationError",
    "LoaderError",
    "NetgraphError",
    "RenderError",
    "SchemaError",
    "SchemaIssue",
    "ValidationError",
    "__version__",
]
