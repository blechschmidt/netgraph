"""Allow the package to be executed with ``python -m netgraph``."""

from __future__ import annotations

from netgraph.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
