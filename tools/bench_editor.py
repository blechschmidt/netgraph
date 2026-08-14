#!/usr/bin/env python3
"""Open a thousand-device inventory in ``netgraph web`` and time what a person does.

Every other harness in this directory stops at the Python boundary.
``bench_pipeline.py`` times the load and the layout, ``bench_incremental.py``
times the reload after one edit, ``bench_events.py`` times the hand-off from a
write to a subscriber — and all three would report perfectly healthy numbers for
an editor whose tab takes eleven seconds to open and then locks up for two more
every time somebody nudges a node. The browser is where the editor is slow, so
this is the harness that drives one::

    python tools/bench_editor.py                       # generate, open, report
    python tools/bench_editor.py --inventory ./net     # an existing tree
    python tools/bench_editor.py --sites 2 --racks 3   # something smaller
    python tools/bench_editor.py --json out.json       # for a guard to read

It starts the real :class:`~netgraph.web.server.WebServer` over a real
:class:`~netgraph.web.session.EditingSession` — the same objects ``netgraph web
--write`` builds, with the same parse cache — points the Playwright Chromium
from ``tests/test_browser.py`` at it, and measures five things:

* **cold open** — navigation to first paint of the diagram. What somebody
  waits through before the tool exists.
* **re-render after one field** — a ``set`` on one element's description,
  driven from the page, timed to the moment the canvas has been repainted. The
  inner loop of editing.
* **event-stream latency** — a file written *behind* the session's back, the
  way ``$EDITOR`` writes one, timed from the write to the repaint. What a
  second tab, a ``git checkout`` or a colleague costs.
* **memory in the tab** — heap and DOM node count after the first paint, and
  again after cycling layers, which is what fills the client-side view cache.
* **a fifty-node move** — one ``set-geometry`` carrying fifty positions, which
  is what dragging a marquee selection writes.

Each is reported with the server-side stages underneath it, because a number
with no breakdown tells you an interaction is slow and nothing about which of
the six things it does is to blame.

Requires the ``browser`` extra and its Chromium::

    pip install --editable ".[dev,browser]" && playwright install chromium

Without them it says so and exits 0 with no table: a bench nobody can run is
not a failure, and pretending to have measured would be worse.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - convenience for a checkout
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT / "tools") not in sys.path:  # pragma: no cover - importing the sibling harness
    sys.path.insert(0, str(REPO_ROOT / "tools"))

from bench_pipeline import Shape, generate, yaml_files  # noqa: E402

from netgraph.loader.cache import DocumentCache  # noqa: E402
from netgraph.render import DETAIL_OPTIONS, build_details, build_graph, filter_graph  # noqa: E402
from netgraph.render.dot import find_dot, to_image  # noqa: E402
from netgraph.validate import validate  # noqa: E402
from netgraph.web.preview import ViewOptions, graph_digest  # noqa: E402
from netgraph.web.server import WebServer  # noqa: E402
from netgraph.web.session import EditingSession  # noqa: E402

#: Rounds per measurement. The median of five, because the first touch of any of
#: these paths is a cold cache somewhere — the page cache, Graphviz's font list,
#: V8's compilation of forty thousand lines of JavaScript.
SAMPLES: Final = 5

#: How long any single wait for the page is given before the harness gives up.
#: Generous, because the whole point is measuring an interaction that is slow.
TIMEOUT_MS: Final = 120_000

#: The viewport the tab opens at. The same one ``tests/test_browser.py`` uses,
#: so the culling measurements here and the assertions there agree about how
#: much of the diagram is on screen.
VIEWPORT: Final = {"width": 1400, "height": 900}

#: How often a wait re-checks the page, in milliseconds. Explicit rather than
#: Playwright's default of one animation frame: a tab that is not the frontmost
#: one has its frames throttled by Chromium, so an animation-frame poll on a
#: second context can wait for ever for a condition that came true immediately.
POLL_MS: Final = 25

#: How many nodes the marquee-drag measurement moves at once.
SELECTION: Final = 50

#: What the harness needs and how to get it, said once.
INSTALL: Final = 'pip install --editable ".[dev,browser]" && python -m playwright install chromium'

#: Instrumentation, installed before any of the page's own scripts run. Two
#: hooks, both passive: a mutation observer that stamps every repaint of the
#: canvas, and a wrapper around ``fetch`` that stamps every request the page
#: makes and how many bytes came back. Nothing here changes what the page does —
#: a bench that perturbs the thing it measures measures the perturbation.
PROBE: Final = """
(() => {
  const bench = window.__ngbench = {
    navigation: Date.now(),
    paints: [],
    fetches: [],
    pending: 0,
    firstPaint: 0
  };
  const nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const started = Date.now();
    // The event stream is a fetch that never ends, so it is watched but not
    // counted: waiting for it to settle would be waiting forever.
    const streaming = url.indexOf("/api/events") === 0;
    if (!streaming) { bench.pending += 1; }
    const done = (bytes) => {
      bench.fetches.push({ url: url, started: started, ended: Date.now(), bytes: bytes });
      if (!streaming) { bench.pending -= 1; }
    };
    return nativeFetch.apply(this, arguments).then((response) => {
      if (streaming) { done(0); return response; }
      response.clone().arrayBuffer().then(
        (buffer) => done(buffer.byteLength), () => done(0));
      return response;
    }, (error) => { done(0); throw error; });
  };
  // Has the page caught up with a change that landed at `since`? It has when
  // it has asked for the diagram again and nothing it asked for is still in
  // flight. `settled` answers the wall clock that happened at, so the harness
  // measures the page's own clock rather than the round trip to it.
  bench.settled = (since) => {
    if (bench.pending > 0) { return 0; }
    let latest = 0;
    for (const one of bench.fetches) {
      if (one.started >= since && /\\/api\\/(graph|diff)/.test(one.url)) {
        latest = Math.max(latest, one.ended);
      }
    }
    if (!latest) { return 0; }
    for (const at of bench.paints) { if (at >= latest) { return at; } }
    return latest;
  };
  bench.since = (since) => {
    const seen = bench.fetches.filter((one) => one.started >= since);
    return [seen.length, seen.reduce((total, one) => total + one.bytes, 0)];
  };
  const watch = () => {
    const viewport = document.getElementById("viewport");
    if (!viewport) { window.setTimeout(watch, 4); return; }
    const observer = new MutationObserver(() => {
      const at = Date.now();
      bench.paints.push(at);
      if (!bench.firstPaint && viewport.querySelector("svg")) { bench.firstPaint = at; }
    });
    observer.observe(viewport, { childList: true });
  };
  watch();
})();
"""


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


@dataclass
class Measurement:
    """One row of the table: what was timed, how long it took, and what it moved."""

    label: str
    samples: list[float] = field(default_factory=list)
    note: str = ""
    #: Bytes the page pulled over the wire for one round of this interaction.
    bytes: int = 0

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else float("nan")

    @property
    def minimum(self) -> float:
        return min(self.samples) if self.samples else float("nan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "medianMs": round(self.median, 1),
            "minMs": round(self.minimum, 1),
            "bytes": self.bytes,
            "note": self.note,
            "samples": [round(value, 1) for value in self.samples],
        }


class Report:
    """The table, printed in the shape the other benches in this directory print."""

    def __init__(self) -> None:
        self.rows: list[Measurement] = []
        self.facts: dict[str, Any] = {}

    def add(self, row: Measurement) -> Measurement:
        self.rows.append(row)
        return row

    def fact(self, key: str, value: Any) -> None:
        self.facts[key] = value

    def heading(self, title: str) -> None:
        print()
        print(title)
        print("-" * len(title))

    def print(self, row: Measurement) -> None:
        wire = f"{row.bytes / 1_000_000:6.2f} MB" if row.bytes else " " * 9
        print(
            f"{row.label:<34} {row.median:8.1f} ms  (min {row.minimum:7.1f})  "
            f"{wire}  {row.note}".rstrip()
        )

    def to_dict(self) -> dict[str, Any]:
        return {"facts": self.facts, "rows": [row.to_dict() for row in self.rows]}


# --------------------------------------------------------------------------- #
# The tab
# --------------------------------------------------------------------------- #


@dataclass
class Tab:
    """One browser tab pointed at one running server, with the probe installed."""

    page: Any
    server: WebServer
    session: EditingSession
    root: Path

    def close(self) -> None:
        """Shut the tab *and* its context, so the event stream it holds is released."""
        self.page.context.close()

    # -- the probe -------------------------------------------------------

    def paints(self) -> int:
        """How many times the canvas has been repainted since the page opened."""
        count = self.page.evaluate("window.__ngbench.paints.length")
        assert isinstance(count, int)
        return count

    def wait_for_paint(self, after: int, *, timeout_ms: int = TIMEOUT_MS) -> float:
        """Block until the canvas is repainted; answer the wall clock it happened at."""
        self.page.wait_for_function(
            "count => window.__ngbench.paints.length > count",
            arg=after,
            timeout=timeout_ms,
            polling=POLL_MS,
        )
        stamp = self.page.evaluate("window.__ngbench.paints[window.__ngbench.paints.length - 1]")
        return float(stamp)

    def wait_until_settled(self, since: float, *, timeout_ms: int = TIMEOUT_MS) -> float:
        """Block until the page has caught up with a change that landed at ``since``.

        Caught up, not repainted: the common edit does not move the picture at
        all, and a harness that waited for a repaint would wait for ever on
        exactly the case worth measuring.
        """
        self.page.wait_for_function(
            "since => window.__ngbench.settled(since)",
            arg=since,
            timeout=timeout_ms,
            polling=POLL_MS,
        )
        return float(self.page.evaluate("since => window.__ngbench.settled(since)", since))

    def wire_since(self, since: float) -> tuple[int, int]:
        """``(requests, bytes)`` the page pulled after the wall clock ``since``."""
        payload = self.page.evaluate("since => window.__ngbench.since(since)", since)
        return int(payload[0]), int(payload[1])

    # -- driving it ------------------------------------------------------

    def post(self, path: str, body: Any) -> Any:
        """One POST from *the page*, so the page's own handlers see the answer."""
        return self.page.evaluate(
            """async ([path, body]) => {
                 const response = await fetch(path, {
                   method: "POST",
                   headers: { "Content-Type": "application/json" },
                   body: JSON.stringify(body)
                 });
                 const text = await response.text();
                 if (!response.ok) { throw new Error(response.status + " " + text.slice(0, 300)); }
                 return text.length;
               }""",
            [path, body],
        )

    def metrics(self) -> dict[str, float]:
        """Heap, DOM nodes and layout count, straight from the renderer."""
        client = self.page.context.new_cdp_session(self.page)
        try:
            client.send("Performance.enable")
            raw = client.send("Performance.getMetrics")
        finally:
            client.detach()
        return {entry["name"]: float(entry["value"]) for entry in raw["metrics"]}

    def dom_nodes(self) -> int:
        count = self.page.evaluate(
            "document.getElementById('viewport').querySelectorAll('*').length"
        )
        return int(count)


# --------------------------------------------------------------------------- #
# The measurements
# --------------------------------------------------------------------------- #


def cold_open(tab_of: Callable[..., Tab], report: Report) -> None:
    """Navigation to first paint, measured on fresh tabs so nothing is warm."""
    row = report.add(Measurement("cold open (first paint)"))
    for _ in range(min(SAMPLES, 3)):  # three: each one is a whole new tab
        tab = tab_of(fresh=True)
        started = time.time() * 1000
        tab.page.goto(tab.server.url, wait_until="commit")
        tab.page.wait_for_function(
            "() => window.__ngbench.firstPaint > 0", timeout=TIMEOUT_MS, polling=POLL_MS
        )
        painted = float(tab.page.evaluate("window.__ngbench.firstPaint"))
        row.samples.append(painted - started)
        if not row.bytes:
            _, row.bytes = tab.wire_since(0)
            report.fact("domNodes", tab.dom_nodes())
            metrics = tab.metrics()
            report.fact("heapBytes", int(metrics.get("JSHeapUsedSize", 0)))
            report.fact("nodesInTab", int(metrics.get("Nodes", 0)))
        tab.close()
    row.note = f"{report.facts.get('domNodes', 0)} SVG elements in the DOM"


def one_field(tab: Tab, address: str, report: Report) -> None:
    """A ``set`` on one description, driven from the page, timed to the settle.

    Twice, because the two halves of an edit cost very different things and
    reporting their average would describe neither: a description does not move
    the drawing, so the server recognises the fingerprint the page sends and
    never runs Graphviz; a rename does, so it runs the lot.
    """
    quiet = report.add(Measurement("edit one field (picture unmoved)"))
    for index in range(SAMPLES):
        started = time.time() * 1000
        # Distinct every round, and distinct from anything a previous run left
        # behind: a ``set`` to the value already there writes no file, publishes
        # no event, and would be timed as an interaction that never happened.
        tab.post(
            "/api/ops",
            {
                "ops": [
                    {
                        "op": "set",
                        "address": address,
                        "path": "metadata.description",
                        "value": f"bench {time.time_ns()} #{index}",
                    }
                ]
            },
        )
        quiet.samples.append(tab.wait_until_settled(started) - started)
        _, quiet.bytes = tab.wire_since(started)
    quiet.note = "POST /api/ops, then tree + graph + changes"

    loud = report.add(Measurement("edit one field (picture moves)"))
    namespace, _, name = address.rpartition("/")
    here, original = address, name
    for index in range(SAMPLES):
        now = f"{name}-b{index}"
        started = time.time() * 1000
        before = tab.paints()
        tab.post("/api/ops", {"ops": [{"op": "rename", "address": here, "new_name": now}]})
        painted = tab.wait_for_paint(before)
        loud.samples.append(painted - started)
        _, loud.bytes = tab.wire_since(started)
        here = f"{namespace}/{now}" if namespace else now
    tab.session.apply([{"op": "rename", "address": here, "new_name": original}])
    loud.note = "a rename: the whole pipeline, Graphviz included"


def stream_latency(tab: Tab, target: Path, report: Report) -> None:
    """A write behind the session's back, timed from the write to the settle."""
    row = report.add(Measurement("write → canvas (event stream)"))
    relative = str(target.relative_to(tab.root))
    original = target.read_text(encoding="utf-8")
    try:
        for index in range(SAMPLES):
            started = time.time() * 1000
            target.write_text(f"{original}\n# bench {index}\n", encoding="utf-8")
            tab.session.invalidate([relative], origin="disk")
            row.samples.append(tab.wait_until_settled(started) - started)
    finally:
        target.write_text(original, encoding="utf-8")
        tab.session.invalidate([relative], origin="disk")
    row.note = "TreeWatcher's path: invalidate, publish, refetch"


def move_selection(tab: Tab, addresses: Sequence[str], report: Report) -> None:
    """One ``set-geometry`` carrying fifty positions: a marquee drag, committed."""
    row = report.add(Measurement(f"move a {len(addresses)}-node selection"))
    for index in range(SAMPLES):
        nodes = {
            address: {"position": {"x": 100 + column * 90, "y": 100 + index * 7 + column}}
            for column, address in enumerate(addresses)
        }
        started = time.time() * 1000
        tab.post("/api/ops", {"ops": [{"op": "set-geometry", "view": "l1", "nodes": nodes}]})
        row.samples.append(tab.wait_until_settled(started) - started)
        _, row.bytes = tab.wire_since(started)
    row.note = "one operation, not fifty: see edit/operations.py SetGeometry"


def layer_cycle(tab: Tab, report: Report) -> None:
    """Switch layers and come back, which is what fills the client's view cache."""
    row = report.add(Measurement("switch layer and come back"))
    layers = tab.page.eval_on_selector_all(
        "#layer option", "options => options.map(option => option.value)"
    )
    here = tab.page.eval_on_selector("#layer", "select => select.value")
    other = next((value for value in layers if value != here), here)
    for _ in range(min(SAMPLES, 3)):
        started = time.time() * 1000
        for layer in (other, here):
            before = tab.paints()
            tab.page.select_option("#layer", layer)
            tab.wait_for_paint(before)
        row.samples.append(time.time() * 1000 - started)
    metrics = tab.metrics()
    report.fact("heapAfterCycleBytes", int(metrics.get("JSHeapUsedSize", 0)))
    row.note = (
        f"heap {metrics.get('JSHeapUsedSize', 0) / 1_000_000:.0f} MB after, "
        f"{report.facts.get('heapBytes', 0) / 1_000_000:.0f} MB at first paint"
    )


def server_stages(session: EditingSession, report: Report) -> None:
    """What the server spends a repaint on, so the browser rows have a floor."""
    options = ViewOptions()
    inventory = session.inventory()

    def timed(label: str, call: Callable[[], Any], note: str = "") -> Any:
        timings = []
        outcome: Any = None
        for _ in range(3):
            started = time.perf_counter()
            outcome = call()
            timings.append((time.perf_counter() - started) * 1000)
        row = report.add(Measurement(label, timings, note))
        report.print(row)
        return outcome

    report.heading("what one repaint costs the server")
    timed("load_tree (cache warm)", lambda: session.inventory())
    timed("validate", lambda: validate(inventory, session.settings()))
    graph = timed(
        "build_graph + filter",
        lambda: filter_graph(build_graph(inventory, layer=options.layer), options.filter_spec),
    )
    render_options = options.render_options
    timed("graph_digest (to_dot + sha)", lambda: graph_digest(graph, render_options))
    svg = timed("Graphviz layout (to_image)", lambda: to_image(graph, render_options, format="svg"))
    details = timed("build_details", lambda: build_details(graph, DETAIL_OPTIONS))
    report.fact("svgBytes", len(svg))
    report.fact("detailBytes", len(json.dumps(dict(details))))
    print(
        f"{'':<34} {'':>8}      {'':>13}  "
        f"{len(svg) / 1_000_000:.1f} MB of SVG, "
        f"{len(json.dumps(dict(details))) / 1_000_000:.1f} MB of records"
    )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


@contextmanager
def inventory_root(args: argparse.Namespace) -> Iterator[Path]:
    if args.inventory is not None:
        yield Path(args.inventory)
        return
    shape = Shape(sites=args.sites, racks_per_site=args.racks, hosts_per_rack=args.hosts)
    target = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="netgraph-editor-"))
    if target.exists() and args.keep:
        shutil.rmtree(target)
    files, documents = generate(target, shape)
    print(f"generated {files} files / {documents} documents / {shape.devices} devices")
    try:
        yield target
    finally:
        if not args.keep:
            shutil.rmtree(target, ignore_errors=True)


def a_device(session: EditingSession) -> str:
    """An address in the middle of the tree, so the edit is not a trivial one."""
    addresses = sorted(
        address
        for address, element in session.inventory().elements.items()
        if element.kind in {"switch", "router", "computer"}
    )
    if not addresses:  # pragma: no cover - a tree with no devices is not benchable
        raise SystemExit("the inventory declares no device to edit")
    return addresses[len(addresses) // 2]


def a_selection(session: EditingSession, count: int) -> list[str]:
    addresses = sorted(
        address
        for address, element in session.inventory().elements.items()
        if element.kind in {"switch", "router", "computer"}
    )
    return addresses[:count]


def run(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"playwright is not installed; {INSTALL}")
        return 0
    if find_dot() is None:
        print("Graphviz 'dot' is not on PATH; the editor cannot draw and this bench cannot run")
        return 0

    report = Report()
    with ExitStack() as stack:
        root = stack.enter_context(inventory_root(args))
        files = yaml_files(root)
        report.fact("root", str(root))
        report.fact("files", len(files))
        report.fact("bytes", sum(path.stat().st_size for path in files))

        cache = DocumentCache(Path(tempfile.mkdtemp(prefix="netgraph-editor-cache-")))
        session = EditingSession(root=root, writable=True, cache=cache)
        stack.callback(session.close)
        server = stack.enter_context(WebServer.create(session=session, host="127.0.0.1", port=0))
        inventory = session.inventory()
        report.fact("elements", len(inventory.elements))
        print(f"inventory: {root}")
        print(f"           {len(files)} files, {len(inventory.elements)} elements")
        print(f"server:    {server.url}")

        playwright = stack.enter_context(sync_playwright())
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - no browser to drive
            print(f"chromium will not start ({exc}); {INSTALL}")
            return 0
        stack.callback(browser.close)

        tabs: list[Tab] = []

        def tab_of(*, fresh: bool = False, open_it: bool = True) -> Tab:
            context = stack.enter_context(browser.new_context(viewport=VIEWPORT))
            page = context.new_page()
            page.set_default_timeout(TIMEOUT_MS)
            page.add_init_script(PROBE)
            tab = Tab(page=page, server=server, session=session, root=root)
            tabs.append(tab)
            if open_it and not fresh:
                page.goto(server.url, wait_until="commit")
                page.wait_for_function(
                    "() => window.__ngbench.firstPaint > 0",
                    timeout=TIMEOUT_MS,
                    polling=POLL_MS,
                )
            return tab

        report.heading("what the browser waits through")

        def measure(call: Callable[[], None]) -> None:
            mark = len(report.rows)
            call()
            for row in report.rows[mark:]:
                report.print(row)

        measure(lambda: cold_open(tab_of, report))
        tab = tab_of()
        measure(lambda: one_field(tab, a_device(session), report))
        measure(lambda: stream_latency(tab, max(files, key=lambda p: p.stat().st_size), report))
        measure(lambda: move_selection(tab, a_selection(session, args.selection), report))
        measure(lambda: layer_cycle(tab, report))

        print()
        print(
            f"in the tab: {report.facts.get('domNodes', 0)} elements under #viewport, "
            f"{report.facts.get('nodesInTab', 0)} DOM nodes, "
            f"heap {report.facts.get('heapBytes', 0) / 1_000_000:.0f} MB "
            f"→ {report.facts.get('heapAfterCycleBytes', 0) / 1_000_000:.0f} MB after a layer cycle"
        )

        server_stages(session, report)

    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        print()
        print(f"wrote {args.json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default = Shape()
    parser.add_argument("--sites", type=int, default=default.sites)
    parser.add_argument("--racks", type=int, default=default.racks_per_site)
    parser.add_argument("--hosts", type=int, default=default.hosts_per_rack)
    parser.add_argument("--inventory", help="time this tree instead of a generated one")
    parser.add_argument("--keep", help="leave the generated tree at this path")
    parser.add_argument("--selection", type=int, default=SELECTION)
    parser.add_argument("--json", help="also write the table here, for a guard to read")
    return run(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover - a script
    raise SystemExit(main())
