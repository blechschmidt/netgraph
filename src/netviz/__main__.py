"""Allow the package to be executed with ``python -m netviz``."""

from __future__ import annotations

from netviz.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
