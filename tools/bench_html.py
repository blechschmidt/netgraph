#!/usr/bin/env python3
"""Measure what an interactive HTML page costs, and how that cost grows.

This is the harness behind entry 8 of ``docs/follow-ups.md``. ``-f html`` embeds
one laid-out drawing per *view* -- each ``--layer`` asked for, with and without
the address and VLAN annotations -- so the interesting question is not "how big
is a page" but "how much does the next view add"::

    python tools/bench_html.py                    # the default matrix
    python tools/bench_html.py --icons cisco      # …with an icon theme
    python tools/bench_html.py --breakdown        # where the bytes actually are
    python tools/bench_html.py --browser          # …plus paint and switch timings

Five numbers per page, because they fail in different ways:

``bytes``
    What a mail attachment or a ``git add`` costs.
``gzip``
    What a static host serving ``Content-Encoding: gzip`` costs. An SVG is
    repetitive text, so this is much smaller -- which is exactly why the raw
    figure must be reported next to it rather than instead of it.
``dom``
    Elements the browser has to build. Hidden views are out of the render tree
    but not out of the document, so this is the cost of *holding* a view.
``paint`` / ``switch``
    Time to first paint of the default view, and time to switch to another one.
    Needs a browser; see ``--browser`` below.

``--breakdown`` splits a page into the parts that scale differently: the client
and the style sheet (fixed), the records (grow with the network and, before
entry 8, with the layer count), and the drawings (grow with the views). Within
the drawings it reports the two payloads that were found to repeat -- the icon
``data:`` URIs and the per-``<text>`` font attributes -- so a later pass can see
whether they have come back.

``--browser`` drives Chromium through ``playwright-core``, which is not a
dependency of this project and not needed for the byte columns. Install it
alongside a browser and point the harness at it::

    npm install playwright-core && npx playwright install chromium
    python tools/bench_html.py --browser --node-modules ./node_modules

The generated inventories come from ``tools/bench_pipeline.py``'s generator, so
a size here is comparable with a size there.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - convenience for a checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT / "tools") not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT / "tools"))

from bench_pipeline import Shape, generate  # noqa: E402

from netviz.loader import load_tree  # noqa: E402
from netviz.render import Layer, RenderOptions, build_graph, html_document  # noqa: E402
from netviz.render.dot import DOT_EXECUTABLE  # noqa: E402
from netviz.render.html import DATA_ELEMENT_ID  # noqa: E402
from netviz.render.icons import icon_theme  # noqa: E402

#: The layer stacks measured, shortest first. A page holding one layer is the
#: baseline every marginal figure is taken against.
LAYER_SETS: Final[tuple[tuple[Layer, ...], ...]] = (
    (Layer.L1,),
    (Layer.L1, Layer.L2),
    (Layer.L1, Layer.L2, Layer.L3),
)

#: The inventories measured, smallest first. ``None`` names a committed example;
#: a :class:`Shape` is generated the way ``bench_pipeline`` generates one.
SIZES: Final[tuple[tuple[str, Shape | None], ...]] = (
    ("home-lab", None),
    ("campus", None),
    ("generated/1 site", Shape(sites=1, racks_per_site=2, hosts_per_rack=4)),
    ("generated/3 sites", Shape(sites=3, racks_per_site=3, hosts_per_rack=6)),
)

EXAMPLES: Final = REPO_ROOT / "examples"


# --------------------------------------------------------------------------- #
# Measuring one page
# --------------------------------------------------------------------------- #


class _Elements(HTMLParser):
    """Every start tag in a document, which is every node a browser builds."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.count = 0
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.count += 1

    handle_startendtag = handle_starttag


@dataclass(frozen=True, slots=True)
class Page:
    """One rendered page and what it costs."""

    label: str
    layers: int
    views: int
    drawings: int
    text: str

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def gzipped(self) -> int:
        return len(gzip.compress(self.text.encode("utf-8"), 9, mtime=0))

    @property
    def dom(self) -> int:
        return _Elements(self.text).count


def measure(label: str, root: Path, layers: Sequence[Layer], *, icons: str | None) -> Page:
    """Render ``root`` as a page holding ``layers`` and weigh the result."""
    inventory = load_tree(root)
    options = RenderOptions(icons=icon_theme(icons))
    graphs = [build_graph(inventory, layer=layer) for layer in layers]
    text = html_document(graphs, options)
    data = json.loads(_data_block(text))
    views = sum(len(layer["views"]) for layer in data["layers"])
    drawings = len({view["view"] for layer in data["layers"] for view in layer["views"]})
    return Page(label=label, layers=len(layers), views=views, drawings=drawings, text=text)


def _data_block(text: str) -> str:
    match = re.search(
        rf'<script id="{DATA_ELEMENT_ID}" type="application/json">(.*?)</script>', text, re.S
    )
    if match is None:  # pragma: no cover - the renderer always writes one
        raise SystemExit("the page carries no record block")
    return match.group(1)


# --------------------------------------------------------------------------- #
# Where the bytes are
# --------------------------------------------------------------------------- #

#: Attributes Graphviz repeats on every ``<text>`` element it emits. They are
#: inheritable, so a drawing that states them once on its root says the same
#: thing; this measures what stating them 300 times over costs.
_INHERITED: Final[tuple[str, ...]] = ("font-family", "font-size", "text-anchor", "font-weight")


def breakdown(page: Page) -> dict[str, int]:
    """``page`` split into the parts that scale differently."""
    text = page.text
    style = sum(len(block) for block in re.findall(r"<style>(.*?)</style>", text, re.S))
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", text, re.S)
    records = len(_data_block(text))
    client = sum(len(block) for block in scripts) - records
    svgs = re.findall(r"(<svg\b.*?</svg>)", text, re.S)
    drawings = sum(len(block) for block in svgs)

    icons = re.findall(r'href="(data:image/[^"]*)"', text)
    inherited = 0
    for name in _INHERITED:
        for value in re.findall(rf'\s{name}="([^"]*)"', "".join(svgs)):
            inherited += len(name) + len(value) + 4
    return {
        "total": len(text),
        "style+client": style + client,
        "records": records,
        "drawings": drawings,
        "…icon data: URIs": sum(len(uri) for uri in icons),
        "…distinct icons": sum(len(uri) for uri in set(icons)),
        "…inherited text attributes": inherited,
        "other": len(text) - style - client - records - drawings,
    }


# --------------------------------------------------------------------------- #
# Timing a browser
# --------------------------------------------------------------------------- #

#: Driven with ``node``. Each page is opened from a ``file://`` URL -- which is
#: how one arrives, as a mail attachment -- and two things are timed:
#:
#: * first paint of the default view: first-contentful-paint where the build
#:   reports one, and ``loadEventEnd`` -- parse, DOM build, first style and
#:   layout -- otherwise. A headless shell with no compositor does not always
#:   emit the paint entry, and a column that is silently empty is worse than
#:   one that says which of the two it holds;
#: * a layer switch, timed from the ``change`` event through a forced layout of
#:   the drawing that just became visible. Reading its geometry back is what
#:   makes the browser do the work inside the sample rather than at the next
#:   vsync, where a frame-quantised number would hide it. Ten switches, median,
#:   after one discarded warm-up.
_DRIVER: Final = """
const { chromium } = require(process.argv[2]);
const executablePath = process.env.NETVIZ_CHROMIUM || undefined;
const files = process.argv.slice(3);
(async () => {
  const browser = await chromium.launch({ executablePath });
  const out = [];
  // Twice over the list, keeping the second pass: the first page a fresh
  // browser opens pays for warming the process up, and that is not what any
  // column here is trying to measure.
  for (const file of [...files, ...files]) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.goto('file://' + file, { waitUntil: 'load' });
    const paint = await page.evaluate(() => {
      const entry = performance.getEntriesByType('paint')
        .find((e) => e.name === 'first-contentful-paint');
      if (entry) { return { ms: entry.startTime, kind: 'fcp' }; }
      const nav = performance.getEntriesByType('navigation')[0];
      return nav ? { ms: nav.loadEventEnd, kind: 'load' } : null;
    });
    const switched = await page.evaluate(() => {
      const select = document.getElementById('ng-layer');
      if (!select) { return null; }
      const shown = () => {
        const pane = document.querySelector('#ng-viewport .view:not([hidden]) svg');
        return pane ? pane.getBoundingClientRect().width : 0;
      };
      const values = Array.from(select.options).map((o) => o.value);
      const samples = [];
      for (let i = 0; i <= 10; i += 1) {
        select.value = values[(i + 1) % values.length];
        const start = performance.now();
        select.dispatchEvent(new Event('change'));
        shown();
        if (i > 0) { samples.push(performance.now() - start); }
      }
      samples.sort((a, b) => a - b);
      return samples[Math.floor(samples.length / 2)];
    });
    out.push({ file, paint, switch: switched });
    await page.close();
  }
  await browser.close();
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exit(1); });
"""


def time_in_browser(paths: Sequence[Path], modules: Path) -> dict[str, dict[str, Any]]:
    """First paint and view-switch times for each page, keyed by path.

    Returns an empty mapping -- rather than failing the run -- when the browser
    is not installed, because every byte column above it is still worth having.
    ``NETVIZ_CHROMIUM`` names a Chromium build to use instead of the one
    ``playwright-core`` would download for itself.
    """
    entry = modules / "playwright-core"
    if not entry.is_dir():
        print(f"({entry} is not installed, so the timing columns are left out)")
        return {}
    with tempfile.TemporaryDirectory() as work:
        driver = Path(work) / "drive.js"
        driver.write_text(_DRIVER, encoding="utf-8")
        try:
            done = subprocess.run(
                ["node", str(driver), str(entry), *(str(path) for path in paths)],
                capture_output=True,
                check=True,
                timeout=900,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            output = getattr(exc, "stderr", b"") or b""
            print(f"(the browser run failed, so the timing columns are left out: {exc})")
            print(output.decode("utf-8", "replace")[:2000])
            return {}
    return {timing["file"]: timing for timing in json.loads(done.stdout)}


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def report(
    *,
    icons: str | None,
    show_breakdown: bool,
    browser: bool,
    modules: Path,
    keep: Path | None,
) -> None:
    pages: list[Page] = []
    for label, shape in SIZES:
        for root in _tree(label, shape):
            for layers in LAYER_SETS:
                pages.append(measure(f"{label} {_stack(layers)}", root, layers, icons=icons))

    written: dict[str, Page] = {}
    with _workspace(keep) as work:
        for index, page in enumerate(pages):
            path = (work / f"page{index:02d}.html").resolve()
            path.write_text(page.text, encoding="utf-8")
            written[str(path)] = page
        timings = time_in_browser([Path(name) for name in written], modules) if browser else {}

        print()
        header = f"{'page':<28} {'views':>5} {'drawn':>5} {'bytes':>10} {'gzip':>9} {'dom':>7}"
        if timings:
            header += f" {'paint':>13} {'switch':>8}"
        print(header)
        print("-" * len(header))
        first: dict[str, Page] = {}
        for name, page in written.items():
            row = (
                f"{page.label:<28} {page.views:5d} {page.drawings:5d} "
                f"{page.size:10,d} {page.gzipped:9,d} {page.dom:7,d}"
            )
            timing = timings.get(name) or {}
            if timings:
                paint = timing.get("paint")
                row += f" {_paint(paint):>13} {_ms(timing.get('switch')):>8}"
            print(row)
            base = page.label.rsplit(" ", 1)[0]
            anchor = first.setdefault(base, page)
            if page is not anchor and page.views > anchor.views:
                marginal = (page.size - anchor.size) / (page.views - anchor.views)
                print(f"{'':<28} {'':>5} {'':>5} {marginal:10,.0f} per extra view")

        if show_breakdown:
            print()
            for page in pages:
                if page.layers != max(len(layers) for layers in LAYER_SETS):
                    continue
                print(page.label)
                for key, value in breakdown(page).items():
                    print(f"    {key:<28} {value:10,d}")


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}ms"


def _paint(value: dict[str, Any] | None) -> str:
    """First paint, saying which of the two clocks it came from."""
    if not value:
        return "-"
    return f"{value['ms']:.1f}ms {value['kind']}"


def _stack(layers: Sequence[Layer]) -> str:
    return "+".join(layer.value for layer in layers)


@contextmanager
def _workspace(keep: Path | None) -> Iterator[Path]:
    """Where the pages are written, so a browser has a ``file://`` to open."""
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)
        yield keep
        print(f"\npages left in {keep}")
        return
    with tempfile.TemporaryDirectory(prefix="netviz-pages-") as work:
        yield Path(work)


def _tree(label: str, shape: Shape | None) -> Iterator[Path]:
    if shape is None:
        yield EXAMPLES / label
        return
    target = Path(tempfile.mkdtemp(prefix="netviz-html-"))
    try:
        generate(target, shape)
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--icons", default=None, help="render with this icon theme or directory")
    parser.add_argument("--breakdown", action="store_true", help="also report where the bytes are")
    parser.add_argument(
        "--browser", action="store_true", help="also time first paint and a view switch"
    )
    parser.add_argument(
        "--node-modules",
        default="node_modules",
        help="where playwright-core is installed [default: ./node_modules]",
    )
    parser.add_argument("--keep", help="write the pages here and leave them behind")
    args = parser.parse_args(argv)

    if shutil.which(DOT_EXECUTABLE) is None:
        print(f"{DOT_EXECUTABLE!r} is not on PATH; an HTML page needs Graphviz to lay one out")
        return 1
    started = time.perf_counter()
    report(
        icons=args.icons,
        show_breakdown=args.breakdown,
        browser=args.browser,
        modules=Path(args.node_modules),
        keep=Path(args.keep) if args.keep else None,
    )
    print(f"\n{statistics.mean([time.perf_counter() - started]):.1f}s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
