"""netviz -- declare network elements in YAML and render them as network graphs.

The package is organised in four layers:

* :mod:`netviz.models` -- typed (pydantic) representations of network elements.
* :mod:`netviz.loader` -- discovery and parsing of a YAML inventory folder tree.
* :mod:`netviz.validate` -- semantic checks *across* documents, graded by the
  catalogue in :mod:`netviz.rules` and filtered by :mod:`netviz.config`.
* :mod:`netviz.render` -- turning a loaded inventory into graph output formats.

:mod:`netviz.graph` sits beside the renderers rather than under them: it hands
the same resolved topology to :mod:`networkx`, so connectivity questions are
answered by graph algorithms instead of by each consumer in turn.

The :mod:`netviz.cli` module wires those layers into the ``netviz`` console
script.
"""

from __future__ import annotations

from importlib import metadata

from netviz.errors import (
    ConfigurationError,
    LoaderError,
    NetvizError,
    RenderError,
    SchemaError,
    SchemaIssue,
    ValidationError,
)

try:
    __version__: str = metadata.version("netviz")
except metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0.dev0"

__all__ = [
    "ConfigurationError",
    "LoaderError",
    "NetvizError",
    "RenderError",
    "SchemaError",
    "SchemaIssue",
    "ValidationError",
    "__version__",
]
