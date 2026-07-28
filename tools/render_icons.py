#!/usr/bin/env python3
"""Rasterise the bundled icon themes: one PNG beside every SVG.

Why both exist
--------------

Graphviz can only read an SVG image when it was built against librsvg, and its
cairo-backed outputs — ``png`` and ``pdf`` — are exactly where many builds were
not. A raster icon is readable by every build, so each theme ships both: the SVG
is the source and is what SVG output uses, the PNG is what everything else uses.
See :mod:`netgraph.render.icons` for the selection rule.

The PNGs are committed rather than generated at install time, because
rasterising needs cairo and netgraph does not otherwise depend on it. Run this
after editing an SVG::

    pip install cairosvg      # not a project dependency; only this tool needs it
    python tools/render_icons.py

``--check`` reports what is stale without writing anything, which is what a
release check wants.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Rendered width in pixels. The icons are drawn 72 points wide and displayed at
#: about 58, so four times over leaves enough resolution for a retina screen and for print.
SCALE: int = 4

ICONSETS = Path(__file__).resolve().parent.parent / "src" / "netgraph" / "render" / "iconsets"


def sources(root: Path) -> list[Path]:
    """Every theme SVG, in a stable order."""
    return sorted(root.glob("*/*.svg"))


def render(source: Path) -> bytes:
    """The PNG for one icon.

    Raises:
        SystemExit: cairosvg is not installed.
    """
    try:
        import cairosvg
    except ImportError:  # pragma: no cover - developer tooling
        raise SystemExit(
            "cairosvg is needed to rasterise the icons and is not installed "
            "(it is deliberately not a netgraph dependency): pip install cairosvg"
        ) from None

    payload = cairosvg.svg2png(url=str(source), scale=SCALE)
    assert isinstance(payload, bytes)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale or missing PNGs instead of writing them.",
    )
    parser.add_argument(
        "--iconsets",
        type=Path,
        default=ICONSETS,
        help="Root holding one directory per theme (default: the bundled one).",
    )
    args = parser.parse_args(argv)

    stale: list[Path] = []
    for source in sources(args.iconsets):
        target = source.with_suffix(".png")
        payload = render(source)
        if target.exists() and target.read_bytes() == payload:
            continue
        stale.append(target)
        if not args.check:
            target.write_bytes(payload)
            print(f"wrote {target.relative_to(args.iconsets)}")

    if args.check and stale:
        names = ", ".join(str(path.relative_to(args.iconsets)) for path in stale)
        print(f"out of date: {names}; run tools/render_icons.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
