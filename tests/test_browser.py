"""``netgraph web``, driven by a real browser.

Everything else in this suite stops at the HTTP boundary: ``tests/test_web.py``
and ``tests/test_web_session.py`` prove the server answers correctly, and prove
nothing at all about the fourteen hundred lines of CSS and JavaScript that turn
those answers into an editor. A regression in ``app.js`` ships silently without
this file.

So this one starts the real server over a real inventory on an ephemeral
loopback port, points a real headless Chromium at it, and asserts what a person
would see:

* the page boots, fetches its state, and draws the tree;
* typing in the text pane re-renders the diagram;
* hovering a node opens the info box, carrying the fields the JSON export does;
* clicking a node reveals the document that declares it, at its line;
* clicking a diagnostic jumps to the file and line it points at;
* saving writes the file and ``Ctrl-Z`` puts it back — on disk *and* on screen;
* a read-only session offers no control that would write, and refuses one that
  is asked for anyway;
* an edit made outside the session reaches the open page, and a save that would
  have clobbered it is refused instead.

Two properties hold for every test here, and they are the cheap half of the
value:

**The console is an assertion.** Every message the page logs is collected, and a
test fails if any of them was an error — an uncaught exception, a 404 for an
asset, a fetch that came back wrong. That single check catches most asset
regressions without anybody having to predict them. A test that *expects* a
refusal says so with :meth:`Console.allow`, naming the status it expects, so the
allowance is visible in the test rather than global.

**A failure leaves evidence.** A screenshot, the page's HTML and the whole
console log are written under :data:`ARTIFACT_DIR` for any test that fails, which
is what the CI job uploads. A browser failure nobody can reproduce is a browser
failure nobody fixes.

Running it
----------

::

    $ pip install --editable ".[dev,browser]"
    $ playwright install chromium
    $ pytest tests/test_browser.py --no-cov

Without Playwright, or without the browser it drives, the whole module skips
with the command to run — never a hard failure for a contributor who has neither.
``NETGRAPH_INSTALL_BROWSER=1`` turns the second command into something the suite
does for itself, which is how the CI job is wired; see ``docs/testing.md``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
import yaml

from netgraph.layout.geometry import Routing
from netgraph.layout.routing import Anchor, route
from netgraph.models import KINDS
from netgraph.render.theme import load_theme
from netgraph.web.server import WebServer
from netgraph.web.session import EditingSession, TreeWatcher

from conftest import failed  # isort: skip -- tests/ is on sys.path, not a package
from platform_marks import requires_dot  # isort: skip

if TYPE_CHECKING:  # pragma: no cover - types only
    from playwright.sync_api import Browser, ConsoleMessage, Locator, Page, Playwright

try:
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import expect, sync_playwright

    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - the module skips itself below
    HAVE_PLAYWRIGHT = False

try:
    from axe_core_python.sync_playwright import Axe

    HAVE_AXE = True
except ImportError:  # pragma: no cover - the accessibility tests skip themselves
    HAVE_AXE = False


REPO_ROOT: Final = Path(__file__).resolve().parent.parent
HOME_LAB: Final = REPO_ROOT / "examples" / "home-lab"

#: How long any single assertion about the page may take to come true. Generous
#: on purpose: a cold Graphviz on a loaded CI runner is slow, and a flaky
#: browser test is worse than a slow one.
TIMEOUT_MS: Final = 20_000

#: How long a *gesture that may do nothing* is given before it is concluded that
#: it did nothing. Short, because that is the expected outcome today; see
#: :data:`DIRECT_MANIPULATION`.
PROBE_S: Final = 4.0

#: Where a failing test leaves its screenshot, page source and console log. The
#: CI job points this at a directory it uploads afterwards.
ARTIFACT_DIR: Final = Path(
    os.environ.get("NETGRAPH_BROWSER_ARTIFACTS") or REPO_ROOT / ".browser-artifacts"
)

#: What to run when Playwright is installed but its browser is not.
INSTALL_COMMAND: Final = f"{Path(sys.executable).name} -m playwright install chromium"

#: Set this to have the suite run :data:`INSTALL_COMMAND` itself rather than
#: skip. Opt-in: downloading a browser is not something a test run should decide
#: to do on somebody's laptop, and it is exactly what the CI job wants.
INSTALL_ENV_VAR: Final = "NETGRAPH_INSTALL_BROWSER"

#: Why the two direct-manipulation tests below skip today. The gestures they
#: perform — drag a node, drag from one node to another — are inert on the
#: current canvas: a drag pans the diagram and writes nothing. Each test does the
#: gesture for real and skips only when the tree did not move, so the day the
#: canvas grows the gesture they start asserting its outcome without being
#: touched.
DIRECT_MANIPULATION: Final = (
    "the canvas does not edit yet: this gesture panned the diagram and changed no file. "
    "The test asserts what the gesture must write once direct manipulation lands"
)

#: A switch the history test adds in one of its commits, so that a frame of the
#: timeline has one green box on it and not merely a faded diagram.
NEW_SWITCH: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-lab
  description: Added in a commit, so a frame of the history has something in it.
spec:
  interfaces:
    - name: eth1
      type: ethernet
"""

#: One placed node, committed by the arrangement test. Enough to make the frame
#: report a stored geometry; the coordinates themselves are checked in
#: ``tests/test_layout.py``.
FIXED_LAYOUT: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: layout
metadata:
  name: layout
spec:
  views:
    physical:
      nodes:
        routers/rtr-home:
          position: {x: 1234, y: 4321}
"""

#: Which axe-core rule sets the page is held to. The standards, and only the
#: standards: axe's ``best-practice`` tag carries opinions ("every region should
#: be a landmark", "id attributes should be unique across the document") that a
#: page embedding a Graphviz drawing will trip over for reasons that have nothing
#: to do with whether it can be used. A gate that shouts about taste is a gate
#: people learn to ignore.
AXE_TAGS: Final = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]

#: A device with a spare port, added to the copied inventory by the keyboard
#: test. ``examples/home-lab`` is fully patched -- every port of ``sw-home``
#: terminates a cable -- so a test that must *connect* something needs somewhere
#: for it to go that does not depend on the example never gaining a device.
SPARE_HOST: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-spare
  description: A host with a free port. Fixture for the keyboard-only test.
spec:
  interfaces:
    - name: eth0
      type: ethernet
"""

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not HAVE_PLAYWRIGHT,
        reason="Playwright is not installed; pip install '.[browser]' to drive a real browser",
    ),
    requires_dot,
]


# --------------------------------------------------------------------------- #
# Fixtures: the browser
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def chromium() -> Iterator[Browser]:
    """One headless Chromium for the whole run.

    Chromium only, deliberately. This layer is here to catch a regression in
    *our* assets, not to survey browser engines: three engines would triple the
    download, the runtime and the number of ways a run can flake, and would tell
    us the same thing about ``app.js`` three times.
    """
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            yield browser
        finally:
            browser.close()


def _launch(playwright: Playwright) -> Browser:
    """Start the browser, installing it first if this run was told it may."""
    try:
        return playwright.chromium.launch()
    except BrowserError as first:
        if not os.environ.get(INSTALL_ENV_VAR):
            pytest.skip(
                f"Playwright has no chromium to drive ({first.message.splitlines()[0]}); "
                f"run '{INSTALL_COMMAND}', or set {INSTALL_ENV_VAR}=1 to have the suite do it"
            )
    completed = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0:  # pragma: no cover - a download that failed
        pytest.skip(f"'{INSTALL_COMMAND}' failed: {completed.stderr.strip()[-500:]}")
    try:
        return playwright.chromium.launch()
    except BrowserError as exc:  # pragma: no cover - installed and still unusable
        pytest.skip(f"chromium was installed but will not start: {exc.message.splitlines()[0]}")


# --------------------------------------------------------------------------- #
# Fixtures: the page, the console and the server behind them
# --------------------------------------------------------------------------- #


@dataclass
class Console:
    """Everything the page said, and which of it counts as a failure.

    An uncaught exception is always a failure. A logged *error* is a failure
    unless the test allowed it by fragment — which a test that drives a refusal
    on purpose does, naming the status code it expects.
    """

    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)

    def message(self, message: ConsoleMessage) -> None:
        self.lines.append(f"[{message.type}] {message.text}")
        if message.type == "error":
            self.errors.append(message.text)

    def crash(self, error: Exception) -> None:
        text = str(error).splitlines()[0] if str(error) else repr(error)
        self.lines.append(f"[pageerror] {text}")
        # An uncaught exception is never allowed away: there is no legitimate
        # reason for this page to throw, and the allowances exist for HTTP
        # refusals the test asked for.
        self.errors.append(f"uncaught: {text}")

    def allow(self, fragment: str) -> None:
        """Expect console errors mentioning ``fragment``; a 403 the test asked for."""
        self.allowed.append(fragment)

    @property
    def unexpected(self) -> list[str]:
        return [
            text
            for text in self.errors
            # An uncaught exception is a failure whatever was allowed: the
            # allowances are for HTTP refusals a test drives on purpose.
            if text.startswith("uncaught: ")
            or not any(fragment in text for fragment in self.allowed)
        ]

    def report(self) -> str:
        return "\n".join(self.lines) or "(the page logged nothing)"


@dataclass
class Editor:
    """One browser tab, the server it is pointed at, and the tree underneath."""

    page: Page
    console: Console
    server: WebServer
    root: Path
    session: EditingSession | None

    # -- the tree, from this side ---------------------------------------

    def read(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8")

    def write(self, relative: str, text: str) -> None:
        """Change a file the way ``$EDITOR`` would: behind the session's back."""
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    # -- the server, from this side --------------------------------------

    def api(self, path: str) -> dict[str, Any]:
        """One GET against the running server, from the test rather than the page."""
        url = f"http://127.0.0.1:{self.server.port}{path}"
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.load(response)
        assert isinstance(payload, dict)
        return payload

    def graph(self) -> dict[str, Any]:
        """The same payload the page fetched, for comparing screen against source."""
        return self.api("/api/graph?" + PAGE_QUERY)

    def element_id(self, address: str) -> str:
        """The SVG id of the shape drawn for ``address``.

        Asked of the server rather than derived, so this file does not grow a
        second copy of :mod:`netgraph.render.ids` that could disagree with it.
        """
        details: Mapping[str, Any] = self.graph()["details"]
        for key, record in details.items():
            if record.get("id") == address:
                return key
        raise AssertionError(f"nothing in the diagram is {address!r}; have {sorted(details)}")

    # -- the page --------------------------------------------------------

    def shape(self, address: str) -> Locator:
        return self.page.locator(f'#viewport [id="{self.element_id(address)}"]')

    def selection(self) -> str:
        """The text the editor pane has selected, which is where it was sent."""
        return str(
            self.page.evaluate(
                "() => { const t = document.getElementById('source');"
                " return t.value.slice(t.selectionStart, t.selectionEnd); }"
            )
        )

    # -- the keyboard ----------------------------------------------------

    def press(self, *chords: str) -> None:
        """Type, and nothing else. No test below may reach for the mouse."""
        for chord in chords:
            self.page.keyboard.press(chord)

    def focus_ring(self) -> str:
        """The SVG id the diagram's focus ring is on, or ``""``.

        Read off ``aria-activedescendant`` rather than off the class, because
        that attribute is what a screen reader follows: asserting on it is
        asserting the thing that matters.
        """
        return str(
            self.page.evaluate(
                "() => document.getElementById('canvas')"
                ".getAttribute('aria-activedescendant') || ''"
            )
        )

    def focus_label(self) -> str:
        """The accessible name of the focused element, as the page states it."""
        return str(
            self.page.evaluate(
                "() => { const id = document.getElementById('canvas')"
                ".getAttribute('aria-activedescendant');"
                " const node = id && document.getElementById(id);"
                " return node ? node.getAttribute('aria-label') || '' : ''; }"
            )
        )

    def announced(self) -> str:
        """What the polite live region last said."""
        return str(self.page.locator("#announcer").inner_text())

    def drag(self, source: Locator, target: Locator, *, modifier: str | None = None) -> None:
        """A real press-move-release, because a synthetic event proves nothing."""
        start, end = _centre(source), _centre(target)
        mouse = self.page.mouse
        if modifier:
            self.page.keyboard.down(modifier)
        mouse.move(*start)
        mouse.down()
        # Two steps: one mousemove is enough for the page but not for a drag
        # threshold, and a gesture the canvas would ignore is not a test.
        mouse.move((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        mouse.move(*end)
        mouse.up()
        if modifier:
            self.page.keyboard.up(modifier)

    def settles(self, condition: Callable[[], bool], *, timeout: float = PROBE_S) -> bool:
        """Poll ``condition`` until it holds, or say that it never did."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return condition()


#: The query the page sends for its first render, and therefore the one to ask
#: for when comparing what is on screen against the server's own answer. The
#: layer is whichever ``<option>`` comes first in ``index.html``.
PAGE_QUERY: Final = (
    "view=physical&show_ips=1&show_vlans=1&annotations=1&group_by_namespace=0&strict=0"
)

#: A cable naming two devices nobody declared: two ``E001`` findings, in a file
#: whose document does not start on line 1, so "jumps to its location" is a claim
#: about a line and not a coincidence.
BROKEN_CABLE: Final = """\
# A cable that names devices nobody declared.
# Fixture for tests/test_browser.py: the diagnostic below points at line 4.

apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: cbl-ghost
spec:
  endpoints:
    - pc-ghost:eth0
    - pc-phantom:eth0
  medium: copper
"""

#: A layout document placing a switch nobody declares: one ``W138``, which is
#: the smallest diagnostic that has exactly one mechanical repair.
STALE_GEOMETRY: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: layout
metadata:
  name: default
spec:
  views:
    l1:
      nodes:
        sw-home: {position: [54, 18]}
        sw-gone: {position: [54, 234]}
"""

#: A trunk whose native VLAN is not in its ``trunk_vlans``: one ``W114``, the
#: smallest diagnostic that has *two* repairs and no way to choose between them.
AMBIGUOUS_TRUNK: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata:
  name: sw-spare
spec:
  interfaces:
    - name: GigabitEthernet0/1
      type: ethernet
      mtu: 1500
      vlan:
        mode: trunk
        trunk_vlans: "10"
        native_vlan: 20
"""

#: The scratchpad's starting stream: two hosts and the cable between them.
TWO_HOSTS: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-a
spec:
  interfaces:
    - name: eth0
      type: ethernet
---
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-b
spec:
  interfaces:
    - name: eth0
      type: ethernet
---
apiVersion: netgraph.dev/v1alpha1
kind: cable
metadata:
  name: cbl-a-b
spec:
  endpoints: [pc-a:eth0, pc-b:eth0]
  medium: copper
"""

#: What a person types into the scratchpad to make the diagram grow a node.
A_THIRD_HOST: Final = """
---
apiVersion: netgraph.dev/v1alpha1
kind: computer
metadata:
  name: pc-typed
spec:
  interfaces:
    - name: eth0
      type: ethernet
"""


OpenEditor = Callable[..., Editor]


@pytest.fixture
def open_editor(
    chromium: Browser, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[OpenEditor]:
    """Start a server, open the page against it, and wait for it to boot.

    Called rather than injected so a test can choose what it is testing: a
    writable session, a read-only one, a watched one, or the document-stream
    scratchpad that has no tree at all.
    """
    editors: list[Editor] = []
    with ExitStack() as stack:

        def start(
            *,
            writable: bool = False,
            watch: bool = False,
            source: str | None = None,
            extra: Mapping[str, str] | None = None,
            beside: Editor | None = None,
            first_run: bool = False,
            theme: str | None = None,
        ) -> Editor:
            root = tmp_path / "inventory"
            if not root.exists():
                shutil.copytree(HOME_LAB, root)
            for relative, text in (extra or {}).items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(text)

            session: EditingSession | None = None
            if beside is not None:
                # A second tab on the *same* session: one server, one tree, two
                # browsers. The whole of what "shared" means here, and the only
                # way to test what one tab does to the other.
                session, server = beside.session, beside.server
            else:
                if source is None:
                    session = EditingSession(root=root, writable=writable)
                    if watch:
                        stack.enter_context(TreeWatcher(session, debounce_ms=50))
                server = stack.enter_context(
                    WebServer.create(
                        source=source or "",
                        session=session,
                        theme=load_theme(theme),
                        host="127.0.0.1",
                        port=0,
                    )
                )

            console = Console()
            context = stack.enter_context(
                chromium.new_context(viewport={"width": 1400, "height": 900})
            )
            if not first_run:
                # Every context is a fresh profile, so every test below would
                # otherwise be a first run and would boot behind the guided
                # tour's invitation. Answered here once, in the same
                # ``localStorage`` key the page writes when a person answers it,
                # so a test opts *in* to the first-run experience rather than
                # every other test having to dismiss it.
                context.add_init_script(
                    "try { window.localStorage.setItem('netgraph.tour.seen', 'yes'); }"
                    " catch (error) { /* no storage, no invitation */ }"
                )
            page = context.new_page()
            page.set_default_timeout(TIMEOUT_MS)
            page.on("console", console.message)
            page.on("pageerror", console.crash)

            editor = Editor(page=page, console=console, server=server, root=root, session=session)
            editors.append(editor)
            page.goto(server.url, wait_until="domcontentloaded")
            # Booted means "the first render came back", whichever face this is:
            # anything asserted before that is a race with the page's own boot.
            expect(page.locator("#viewport svg")).to_be_visible(timeout=TIMEOUT_MS)
            return editor

        yield start

        if failed(request.node):
            for index, editor in enumerate(editors):
                _keep(editor, request.node.nodeid, index)
        for editor in editors:
            assert not editor.console.unexpected, (
                "the page logged an error:\n  "
                + "\n  ".join(editor.console.unexpected)
                + "\n\nthe whole console log:\n"
                + editor.console.report()
            )


def _keep(editor: Editor, nodeid: str, index: int) -> None:
    """Leave a screenshot, the DOM and the console log where CI can upload them."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", nodeid).strip("-")
    directory = ARTIFACT_DIR / (name if index == 0 else f"{name}-{index}")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "console.log").write_text(editor.console.report() + "\n", encoding="utf-8")
    # Best-effort: a page that crashed the tab cannot be screenshotted, and
    # failing to collect evidence must not replace the real failure.
    with suppress(Exception):
        editor.page.screenshot(path=str(directory / "screenshot.png"), full_page=True)
    with suppress(Exception):
        (directory / "page.html").write_text(editor.page.content(), encoding="utf-8")


def _centre(locator: Locator) -> tuple[float, float]:
    """Where to put the mouse to hit ``locator``, once it is actually there.

    ``bounding_box()`` answers ``None`` for an element that is not laid out at
    that instant, and every locator here is positional — ``.ng-handle-bend >>
    nth=0`` is *whichever* handle is first right now. The editor rebuilds the
    whole viewport on each state change, so the element the call resolved a
    moment ago is a different DOM node from the one it resolves next, and a
    single measurement lands in the gap about one run in six. That is what took
    ``browser (chromium)`` red on 2026-08-14, with a message describing the
    instant rather than the problem.

    So the *measurement* is retried, and only the measurement. Waiting for
    ``visible`` first would be the obvious thing and is the wrong thing here:
    Playwright calls an element with an empty bounding box invisible, and a hit
    band is a transparent stroke — a cable drawn straight down has a box no wider
    than its own line, which :func:`press_on` explains at length. Gating on
    visibility turned four of these tests from "measured a stale node" into
    "waited twenty seconds for a band that was never going to satisfy the check".
    """
    deadline = time.monotonic() + TIMEOUT_MS / 1000
    while True:
        box = locator.bounding_box(timeout=TIMEOUT_MS)
        if box is not None:
            return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        if time.monotonic() >= deadline:
            raise AssertionError("the element is not on screen, so it cannot be dragged")
        time.sleep(0.05)


# --------------------------------------------------------------------------- #
# Booting and rendering
# --------------------------------------------------------------------------- #


def test_the_page_boots_and_draws_the_inventory(open_editor: OpenEditor) -> None:
    """Every asset loads, the state is fetched, and the tree is on screen."""
    editor = open_editor()
    page = editor.page

    expect(page.locator("#status")).to_have_text("ok")
    expect(page.locator("#placeholder")).to_be_hidden()

    # As many shapes as the server says there are records for: the diagram is
    # the server's answer, not an approximation of it.
    details = editor.graph()["details"]
    nodes = [key for key, record in details.items() if record["type"] == "element"]
    assert len(nodes) > 1
    expect(page.locator("#viewport g.node")).to_have_count(len(nodes))

    # The file list is the session half of the page, and it lists the tree.
    expect(page.locator("#files")).to_be_visible()
    expect(page.locator("#file-list .file")).to_have_count(len(editor.api("/api/tree")["files"]))
    expect(page.locator("#files-mode")).to_have_text("read-only")
    expect(page.locator("#file-list")).to_contain_text("sw-home.yaml")


def test_typing_in_the_text_pane_re_renders_the_diagram(open_editor: OpenEditor) -> None:
    """The scratchpad's whole contract: text in, diagram out, no save anywhere."""
    editor = open_editor(source=TWO_HOSTS)
    page = editor.page

    expect(page.locator("#files")).to_be_hidden()
    expect(page.locator("#viewport g.node")).to_have_count(2)

    page.locator("#source").click()
    page.keyboard.press("Control+End")
    page.locator("#source").press_sequentially(A_THIRD_HOST)

    expect(page.locator("#viewport g.node")).to_have_count(3)
    expect(page.locator("#viewport")).to_contain_text("pc-typed")
    expect(page.locator("#status")).to_have_text("ok")


# --------------------------------------------------------------------------- #
# The info box
# --------------------------------------------------------------------------- #


def test_hovering_a_node_opens_the_info_box_with_the_exported_fields(
    open_editor: OpenEditor,
) -> None:
    """The box shows the record, and the record is what the JSON export carries.

    Asserted field by field against the server's own answer rather than against
    a copy pasted in here: the point of the box is that the picture and the
    export describe one inventory.
    """
    editor = open_editor()
    page = editor.page
    record = editor.graph()["details"][editor.element_id("switches/sw-home")]

    expect(page.locator("#info")).to_be_hidden()
    editor.shape("switches/sw-home").hover()
    expect(page.locator("#info")).to_be_visible()

    expect(page.locator("#info h2")).to_contain_text(record["name"])
    expect(page.locator("#info h2 .kind")).to_have_text(f"[{record['kind']}]")
    box = page.locator("#info")
    expect(box).to_contain_text(record["id"])
    expect(box).to_contain_text(record["description"])
    for key, value in record["labels"].items():
        expect(box).to_contain_text(f"label {key}")
        expect(box).to_contain_text(value)
    for port in record["interfaces"]:
        expect(box).to_contain_text(port["name"])
    for link in record["links"]:
        expect(box).to_contain_text(link["peer"])

    # Hovering also lifts the element and its neighbours out of the diagram,
    # which is the other half of "what am I looking at".
    expect(editor.shape("switches/sw-home")).to_have_class(re.compile(r"\bhot\b"))

    page.locator("#info").page.mouse.move(5, 5)
    page.locator("#status").hover()
    expect(page.locator("#info")).to_be_hidden()


# --------------------------------------------------------------------------- #
# The mapping between the picture and the text
# --------------------------------------------------------------------------- #


def test_clicking_a_node_reveals_its_declaring_document(open_editor: OpenEditor) -> None:
    """A shape carries an address; an address has a file and a line; go there.

    This is the 1:1 mapping the whole command exists for, asserted end to end
    through the browser for the first time.
    """
    editor = open_editor()
    page = editor.page
    declared = {
        document["address"]: (entry["path"], document["line"])
        for entry in editor.api("/api/tree")["files"]
        for document in entry["documents"]
    }
    path, line = declared["switches/sw-home"]

    editor.shape("switches/sw-home").click()

    expect(page.locator("#editor-title")).to_have_text(path)
    expect(page.locator("#file-list .file.current")).to_contain_text(Path(path).name)
    assert editor.selection() == editor.read(path).splitlines()[line - 1]
    # And the box stays up, pinned, so the reveal did not cost the reader the
    # thing they clicked on.
    expect(page.locator("#info")).to_be_visible()


def test_a_diagnostic_row_jumps_to_its_location(open_editor: OpenEditor) -> None:
    """A finding names a file, a document and a line. Clicking it goes there."""
    editor = open_editor(extra={"cables/broken.yaml": BROKEN_CABLE})
    page = editor.page

    row = page.locator("#problems .problem.locatable", has_text="E001").first
    expect(row).to_be_visible()
    expect(page.locator("#status")).to_have_text("invalid")

    row.click()

    expect(page.locator("#editor-title")).to_have_text("cables/broken.yaml")
    assert editor.selection() == "apiVersion: netgraph.dev/v1alpha1"
    assert BROKEN_CABLE.splitlines()[3] == editor.selection(), "line 4 is the document's first"


def test_a_fixable_diagnostic_grows_a_fix_button_that_repairs_it(
    open_editor: OpenEditor,
) -> None:
    """The whole loop, in the browser: a warning, a button, a written file, an undo."""
    editor = open_editor(writable=True, extra={"layout.yaml": STALE_GEOMETRY})
    page = editor.page

    row = page.locator("#problems .problem-entry", has_text="W138")
    expect(row).to_be_visible()
    button = row.locator("button.fix")
    expect(button).to_have_text("fix")
    expect(button).to_have_attribute("title", re.compile("sw-gone"))

    button.click()

    expect(page.locator("#problems")).not_to_contain_text("W138")
    assert "sw-gone" not in editor.read("layout.yaml")
    assert "sw-home" in editor.read("layout.yaml"), "only the dead entry goes"

    # One gesture, so one Ctrl-Z, and the file comes back exactly.
    page.keyboard.press("Control+Z")
    expect(page.locator("#problems")).to_contain_text("W138")
    assert editor.read("layout.yaml") == STALE_GEOMETRY


def test_a_diagnostic_with_two_repairs_offers_both(open_editor: OpenEditor) -> None:
    """Where the document cannot say which repair is meant, the page does not either."""
    editor = open_editor(writable=True, extra={"switches/trunk.yaml": AMBIGUOUS_TRUNK})
    page = editor.page

    row = page.locator("#problems .problem-entry", has_text="W114")
    expect(row.locator("button.fix")).to_have_count(2)
    expect(row.locator("button.fix").nth(0)).to_have_text("fix: list")
    expect(row.locator("button.fix").nth(1)).to_have_text("fix: drop")

    row.locator("button.fix").nth(1).click()

    expect(page.locator("#problems")).not_to_contain_text("W114")
    assert "native_vlan" not in editor.read("switches/trunk.yaml")


def test_a_read_only_session_offers_no_fix(open_editor: OpenEditor) -> None:
    editor = open_editor(extra={"layout.yaml": STALE_GEOMETRY})
    page = editor.page
    expect(page.locator("#problems .problem", has_text="W138")).to_be_visible()
    expect(page.locator("#problems button.fix")).to_have_count(0)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def test_saving_writes_the_file_and_ctrl_z_puts_it_back(open_editor: OpenEditor) -> None:
    """The write path, the history and the page's picture of both, in one run."""
    editor = open_editor(writable=True)
    page = editor.page
    relative = "switches/sw-home.yaml"
    original = editor.read(relative)

    page.locator(f'#file-list .file[data-path="{relative}"]').click()
    expect(page.locator("#editor-title")).to_have_text(relative)
    expect(page.locator("#save")).to_be_disabled()

    # An interface, because the label Graphviz draws lists them: this edit is
    # visible in the picture, so the diagram re-rendering is assertable and not
    # merely assumed.
    edited = original.replace(
        "  bridge:\n",
        "    - name: port6\n"
        "      type: ethernet\n"
        "      description: Spare port on the front\n"
        "      mtu: 1500\n"
        "      vlan:\n"
        "        mode: access\n"
        "        access_vlan: 10\n"
        "  bridge:\n",
        1,
    )
    assert edited != original
    page.locator("#source").fill(edited)

    # Typing marks the file, and only then may it be saved.
    expect(page.locator("#editor-state")).to_have_text("unsaved changes")
    expect(page.locator("#save")).to_be_enabled()
    assert editor.read(relative) == original, "nothing is written until Save"

    page.keyboard.press("Control+s")

    expect(page.locator("#toast")).to_contain_text(f"saved {relative}")
    expect(page.locator("#editor-state")).to_be_hidden()
    expect(page.locator("#undo")).to_be_enabled()
    assert editor.read(relative) == edited
    expect(page.locator("#viewport")).to_contain_text("port6")

    page.keyboard.press("Control+z")

    expect(page.locator("#toast")).to_contain_text("undone")
    assert editor.read(relative) == original, "undo restores the file byte for byte"
    # And the editor pane shows the file it now is, rather than the text that is
    # no longer anywhere.
    expect(page.locator("#source")).to_have_value(original)
    expect(page.locator("#viewport")).not_to_contain_text("port6")
    expect(page.locator("#redo")).to_be_enabled()


def test_dragging_a_node_writes_geometry_to_disk(open_editor: OpenEditor) -> None:
    """A hand-arranged diagram is inventory, so a drag has to reach a file.

    Skips while the canvas only pans; see :data:`DIRECT_MANIPULATION`.
    """
    editor = open_editor(writable=True)
    assert editor.session is not None
    before = editor.session.revision
    node = editor.shape("switches/sw-home")
    box = node.bounding_box()
    assert box is not None

    mouse = editor.page.mouse
    mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    mouse.down()
    mouse.move(box["x"] + box["width"] / 2 + 40, box["y"] + box["height"] / 2 - 30)
    mouse.move(box["x"] + box["width"] / 2 + 90, box["y"] + box["height"] / 2 - 70)
    mouse.up()

    if not editor.settles(lambda: editor.session is not None and editor.session.revision != before):
        pytest.skip(DIRECT_MANIPULATION)

    layouts = [  # pragma: no cover - runs once the canvas edits
        path
        for path in sorted(editor.root.rglob("*.yaml"))
        if "kind: layout" in path.read_text(encoding="utf-8")
    ]
    assert layouts, "a drag has to land in a kind: layout document"
    text = "\n".join(path.read_text(encoding="utf-8") for path in layouts)
    assert "switches/sw-home" in text
    assert "position" in text
    assert editor.graph()["geometry"] is not None


def test_drawing_a_link_produces_a_cable(open_editor: OpenEditor) -> None:
    """Connecting two nodes on the canvas has to become YAML somebody can read.

    Skips while the canvas only pans; see :data:`DIRECT_MANIPULATION`.
    """
    editor = open_editor(writable=True)
    assert editor.session is not None
    before = editor.session.revision

    editor.drag(
        editor.shape("hosts/srv-nas"),
        editor.shape("hosts/pc-desk"),
        modifier="Shift",
    )

    if not editor.settles(lambda: editor.session is not None and editor.session.revision != before):
        pytest.skip(DIRECT_MANIPULATION)

    text = "\n".join(  # pragma: no cover - runs once the canvas edits
        path.read_text(encoding="utf-8") for path in sorted(editor.root.rglob("*.yaml"))
    )
    assert "kind: cable" in text
    assert "srv-nas" in text and "pc-desk" in text
    endpoints = editor.api("/api/tree")
    assert any(
        document["kind"] == "cable"
        for entry in endpoints["files"]
        for document in entry["documents"]
    )


# --------------------------------------------------------------------------- #
# Routing a link
# --------------------------------------------------------------------------- #
#
# A cable's *shape* is inventory too (§18), and it is the one part of the
# diagram that can only be edited with a pointer: a bend has no name to type.
# So these are the tests that cannot be written anywhere but here, and the
# reason this file starts a browser at all.


#: The arranged home-lab, as a file to drop beside it. A route needs somewhere
#: to be routed *from*, so every test below runs against a fully placed diagram
#: -- which is also the only kind netgraph offers handles on, for the good
#: reason that a bend pinned on a drawing Graphviz is still laying out is a bend
#: the next render throws away.
ARRANGED_LAYOUT: Final = (REPO_ROOT / "tests" / "fixtures" / "arranged" / "layout.yaml").read_text(
    encoding="utf-8"
)

#: The view those coordinates belong to. The page opens on ``physical``, which
#: the fixture does not arrange.
ARRANGED_LAYER: Final = "l1"

#: A cable long enough to grab, in a straight line nothing else crosses.
A_CABLE: Final = "cables/cbl-sw-desk"


def arranged(open_editor: OpenEditor, *, writable: bool = True) -> Editor:
    """A writable session over an arranged diagram, showing the arranged view."""
    editor = open_editor(writable=writable, extra={"layout.yaml": ARRANGED_LAYOUT})
    editor.page.select_option("#layer", ARRANGED_LAYER)
    expect(editor.page.locator(".ng-link-hit").first).to_be_attached(timeout=TIMEOUT_MS)
    return editor


def band(editor: Editor, link: str = A_CABLE) -> Locator:
    """The invisible band along one link, which is what a click on it hits."""
    return editor.page.locator(f'.ng-link-hit[data-link="{link}"]').first


def press_on(editor: Editor, locator: Locator, *, button: str = "left") -> None:
    """Click with the real mouse, at the centre of what is on screen.

    Rather than ``locator.click()``, which first asks whether the element is
    *visible*: a hit band is a transparent stroke with no fill, and a run of
    cable drawn straight down has a bounding box no wider than its own line.
    Playwright is right to be suspicious of that in general and wrong about it
    here, and the gesture being tested is a press at a point anyway.
    """
    editor.page.mouse.click(*_centre(locator), button=button)


def bends(editor: Editor, link: str = A_CABLE) -> list[dict[str, float]]:
    """What the *server* says the link is pinned through, not what is on screen."""
    geometry = editor.api(f"/api/graph?view={ARRANGED_LAYER}")["geometry"] or {}
    return list((geometry.get("links", {}).get(link) or {}).get("waypoints") or [])


def test_selecting_a_link_reveals_its_handles(open_editor: OpenEditor) -> None:
    """Nothing is grabbable until a link is picked, or the canvas is a field of dots."""
    editor = arranged(open_editor)
    assert editor.page.locator(".ng-handle").count() == 0

    press_on(editor, band(editor))
    expect(editor.page.locator(".ng-handle-add")).to_have_count(1)
    # Two bends are stored for this cable, and each gets a handle of its own.
    assert editor.page.locator(".ng-handle-bend").count() == len(bends(editor))


def test_dragging_a_bend_writes_the_new_route_to_disk(open_editor: OpenEditor) -> None:
    """The gesture the whole feature exists for: a cable dragged into place stays there."""
    editor = arranged(open_editor)
    assert editor.session is not None
    before = editor.session.revision
    original = bends(editor)
    assert original, "this cable is routed, so it has bends to drag"

    press_on(editor, band(editor))
    handle = editor.page.locator(".ng-handle-bend").first
    start = _centre(handle)
    mouse = editor.page.mouse
    mouse.move(*start)
    mouse.down()
    mouse.move(start[0] + 60, start[1] + 20)
    mouse.move(start[0] + 110, start[1] + 35)
    mouse.up()

    assert editor.settles(
        lambda: editor.session is not None and editor.session.revision != before,
        timeout=TIMEOUT_MS / 1000,
    ), "dragging a bend has to reach a file"

    # On disk, in the layout document, as a waypoint -- and the *other* bend is
    # untouched, which is what says a drag moved one point rather than rewriting
    # the route.
    text = editor.read("layout.yaml")
    assert "waypoints:" in text
    after = bends(editor)
    assert len(after) == len(original)
    assert after[0] != original[0], "the dragged bend did not move"
    assert after[1:] == original[1:], "dragging one bend moved another"


def test_double_clicking_a_link_drops_a_bend_and_right_clicking_takes_it_away(
    open_editor: OpenEditor,
) -> None:
    """The two gestures every diagram editor has, and neither has a keyboard shape."""
    editor = arranged(open_editor)
    original = len(bends(editor))

    # A quarter of the way along rather than half: the midpoint carries the
    # "add a bend here" handle, and a double-click on *that* is a press on a
    # handle rather than on the line.
    box = band(editor).bounding_box()
    assert box is not None
    editor.page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 4)
    assert editor.settles(lambda: len(bends(editor)) == original + 1, timeout=TIMEOUT_MS / 1000), (
        "double-clicking a link has to drop a bend"
    )

    press_on(editor, editor.page.locator(".ng-handle-bend").first, button="right")
    assert editor.settles(lambda: len(bends(editor)) == original, timeout=TIMEOUT_MS / 1000), (
        "right-clicking a bend has to remove it"
    )


def test_straightening_a_link_clears_every_bend_from_the_keyboard(
    open_editor: OpenEditor,
) -> None:
    """A diagram has to be arrangeable without a mouse, this one included."""
    editor = arranged(open_editor)
    assert bends(editor), "this cable is routed, so there is something to clear"

    press_on(editor, band(editor))
    editor.page.locator("#canvas").focus()
    editor.press("Shift+B")

    assert editor.settles(lambda: bends(editor) == [], timeout=TIMEOUT_MS / 1000), (
        "link.straighten has to clear the bends"
    )
    # And the geometry that was not asked about survives: straightening is not
    # "forget everything about this cable".
    geometry = editor.api(f"/api/graph?view={ARRANGED_LAYER}")["geometry"]
    assert A_CABLE in geometry["links"]


#: A layout that is arranged, orthogonal, and puts a device squarely between the
#: two ends of one cable, so that the route netgraph computes to get past it is
#: something ``link.pin-route`` has to have to pin. Built from the arranged
#: fixture rather than hand-typed so the coordinates cannot drift from it.
def _obstructed_layout() -> str:
    return ARRANGED_LAYOUT.replace("spec:\n", "spec:\n  routing: orthogonal\n", 1)


def test_pinning_the_computed_route_writes_it_as_bends(open_editor: OpenEditor) -> None:
    """A computed route is a suggestion until somebody says it is the answer.

    Routing keeps ``cables/cbl-sw-nas`` clear of the router it would otherwise
    be drawn across, and publishes that as ``routed`` -- recomputed on every
    render, in no document, with no grab handle. ``Shift-R`` turns it into
    waypoints, at which point it is an authored route like any other and the
    handles appear.
    """
    editor = open_editor(writable=True, extra={"layout.yaml": _obstructed_layout()})
    editor.page.select_option("#layer", ARRANGED_LAYER)
    expect(editor.page.locator(".ng-link-hit").first).to_be_attached(timeout=TIMEOUT_MS)

    link = "cables/cbl-sw-nas"
    published = editor.api(f"/api/graph?view={ARRANGED_LAYER}")["geometry"]["links"][link]
    assert published["routed"], "this cable is routed round the router; that is the fixture"
    assert len(published["routed"]) > len(published["waypoints"]), (
        "the computed route has to say more than the document already does, "
        "or pinning it would be a no-op and this test would prove nothing"
    )

    # Selected through the layer's own API rather than by clicking the band: a
    # route that turns twice to get round a router has a bounding box whose
    # centre is nowhere near the line, so a click at it would land on the canvas
    # behind. Which key reaches which command is settled by test_web.py; what
    # this test is here for is what the command does once it is reached.
    editor.page.evaluate("id => window.netgraphLinks.select(id)", link)
    editor.page.locator("#canvas").focus()
    editor.press("Shift+R")

    assert editor.settles(
        lambda: (
            bends(editor, link)
            == [{"x": point["x"], "y": point["y"]} for point in published["routed"]]
        ),
        timeout=TIMEOUT_MS / 1000,
    ), f"link.pin-route has to write the computed route as waypoints: {bends(editor, link)}"

    # And it is an authored route now: nothing left for routing to add on top of
    # it, and a grab handle on every bend.
    after = editor.api(f"/api/graph?view={ARRANGED_LAYER}")["geometry"]["links"][link]
    assert after["routed"] == [], "a pinned route must not be recomputed on top of itself"
    editor.page.evaluate("id => window.netgraphLinks.select(id)", link)
    expect(editor.page.locator(".ng-handle-bend")).to_have_count(len(published["routed"]))


def test_a_read_only_session_shows_a_route_and_offers_no_handle(
    open_editor: OpenEditor,
) -> None:
    """Looking at an arrangement must never be able to become changing one."""
    editor = arranged(open_editor, writable=False)
    press_on(editor, band(editor))
    editor.page.wait_for_timeout(300)
    assert editor.page.locator(".ng-handle").count() == 0


def test_the_canvas_and_the_renderer_route_a_link_identically(
    open_editor: OpenEditor,
) -> None:
    """The one duplicated algorithm in the codebase, checked against itself.

    ``web/assets/links.js`` mirrors :mod:`netgraph.layout.routing` because a
    line that only moved when the server answered would lag the cursor. A mirror
    is a liability exactly as long as nothing compares the two, so this runs a
    table of cases through both and asserts they agree to the last decimal
    place -- every routing style, with bends and without, a fan, and a
    self-link.
    """
    editor = arranged(open_editor, writable=False)

    source = Anchor(x=100.0, y=100.0, width=120.0, height=60.0)
    target = Anchor(x=500.0, y=400.0, width=80.0, height=40.0)
    cases: list[dict[str, Any]] = []
    for style in Routing:
        for waypoints in ((), ((300.0, 120.0),), ((250.0, 380.0), (420.0, 150.0))):
            for fan in (0.0, 14.0, -21.0):
                cases.append({"style": style.value, "waypoints": waypoints, "fan": fan})
    # And a self-link, whose loop is a different code path in both languages.
    self_cases = [
        {"style": style.value, "waypoints": (), "fan": fan}
        for style in Routing
        for fan in (0.0, 28.0)
    ]

    def expected(case: Mapping[str, Any], ends: tuple[Anchor, Anchor]) -> list[list[float]]:
        line = route(
            ends[0],
            ends[1],
            waypoints=case["waypoints"],
            style=Routing(case["style"]),
            fan=case["fan"],
        )
        return [[round(x, 6), round(y, 6)] for x, y in line.corners]

    def drawn(payload: Sequence[Mapping[str, Any]], ends: tuple[Anchor, Anchor]) -> Any:
        return editor.page.evaluate(
            """([cases, source, target]) => cases.map(function (one) {
                 return window.netgraphLinks.routeOf(
                   source, target,
                   one.waypoints.map(function (p) { return { x: p[0], y: p[1] }; }),
                   one.style, one.fan
                 ).map(function (point) {
                   return [Math.round(point[0] * 1e6) / 1e6, Math.round(point[1] * 1e6) / 1e6];
                 });
               })""",
            [
                [
                    dict(one, waypoints=[list(point) for point in one["waypoints"]])
                    for one in payload
                ],
                _anchor(ends[0]),
                _anchor(ends[1]),
            ],
        )

    for group, ends in ((cases, (source, target)), (self_cases, (source, source))):
        got = drawn(group, ends)
        for case, actual in zip(group, got, strict=True):
            assert actual == expected(case, ends), case


def _anchor(anchor: Anchor) -> dict[str, float]:
    return {"x": anchor.x, "y": anchor.y, "width": anchor.width, "height": anchor.height}


# --------------------------------------------------------------------------- #
# Annotating the diagram (§21)
# --------------------------------------------------------------------------- #
#
# A note, an area and a legend are commentary rather than topology, and the
# canvas treats them accordingly: they are dragged, retyped and deleted, and
# none of it can change what the tool concludes. Every gesture below ends in one
# of the three annotation operations through /api/ops, so what is asserted is
# always what reached the file -- never what the browser drew.

#: Something written on the arranged home lab: a note pinned below the diagram,
#: a zone pinned beside it, and a zone that follows two of its devices. The
#: coordinates keep all three clear of the nodes, whose ``y`` runs 0..440.
ANNOTATIONS: Final = """\
apiVersion: netgraph.dev/v1alpha1
kind: note
metadata:
  name: why-here
spec:
  text: |
    The **switch** is in the cupboard.
  geometry: {x: 900, y: -300}
---
apiVersion: netgraph.dev/v1alpha1
kind: area
metadata:
  name: on-the-ups
spec:
  label: On the UPS
  geometry: {x: 300, y: -300, width: 400, height: 200}
---
apiVersion: netgraph.dev/v1alpha1
kind: area
metadata:
  name: the-desk
spec:
  label: The desk
  members: [switches/sw-home, hosts/pc-desk]
"""

#: The SVG id the renderer gives that note; see netgraph.render.annotations.
A_NOTE: Final = "note-why-here"


def annotated(open_editor: OpenEditor, *, writable: bool = True) -> Editor:
    """An arranged diagram with something written on it, drawn at that layer."""
    editor = open_editor(
        writable=writable, extra={"layout.yaml": ARRANGED_LAYOUT, "annotations.yaml": ANNOTATIONS}
    )
    editor.page.select_option("#layer", ARRANGED_LAYER)
    # The band, not the note: a note is drawn at every layer, the unarranged one
    # the page opens on included, so waiting for the note would race the render
    # that puts the *arranged* view -- and with it the overlay -- on screen.
    expect(editor.page.locator(".ng-anno-band").first).to_be_attached(timeout=TIMEOUT_MS)
    return editor


def note_shape(editor: Editor) -> Locator:
    return editor.page.locator(f'#viewport [id="{A_NOTE}"]')


def pinned(editor: Editor, kind: str, fqn: str) -> dict[str, Any]:
    """What the *server* says one annotation's geometry is, not the screen."""
    payload = editor.api(f"/api/graph?view={ARRANGED_LAYER}")["annotations"] or {}
    for entry in payload.get(kind + "s", []):
        if entry["fqn"] == fqn:
            found = entry.get("layout") or {}
            assert isinstance(found, dict)
            return found
    raise AssertionError(f"the drawing holds no {kind} {fqn!r}")


def test_the_graph_answer_says_where_every_annotation_is(open_editor: OpenEditor) -> None:
    """The payload the canvas hit-tests against, before any gesture uses it.

    An area in an arranged drawing is painted into the graph's background with
    no id on it, so this is the *only* way the page can know it is there -- which
    is why it is asserted separately from the gestures that consume it.
    """
    editor = annotated(open_editor)
    payload = editor.api(f"/api/graph?view={ARRANGED_LAYER}")["annotations"]
    assert [note["id"] for note in payload["notes"]] == [A_NOTE]
    boxed = pinned(editor, "area", "on-the-ups")
    assert boxed["size"] == {"width": 400.0, "height": 200.0}
    # The one that follows its members pins nothing, and that absence is what
    # tells the canvas to refuse to drag it.
    assert pinned(editor, "area", "the-desk") == {}


def test_dragging_a_note_writes_its_position(open_editor: OpenEditor) -> None:
    """The gesture the payload exists for: a note moved on screen stays moved."""
    editor = annotated(open_editor)
    assert editor.session is not None
    before = editor.session.revision
    assert pinned(editor, "note", "why-here")["position"] == {"x": 900.0, "y": -300.0}

    start = _centre(note_shape(editor))
    mouse = editor.page.mouse
    mouse.move(*start)
    mouse.down()
    mouse.move(start[0] + 40, start[1] + 20)
    mouse.move(start[0] + 90, start[1] + 40)
    mouse.up()

    assert editor.settles(
        lambda: editor.session is not None and editor.session.revision != before,
        timeout=TIMEOUT_MS / 1000,
    ), "dragging a note has to reach a file"
    moved = pinned(editor, "note", "why-here")["position"]
    assert moved["x"] > 900.0, "the note did not move right"
    assert moved["y"] < -300.0, "the note did not move down"
    # Through the mutation layer, into the document that declares it, and one
    # gesture is one entry in the undo stack.
    assert "kind: note" in editor.read("annotations.yaml")
    editor.page.locator("#undo").click()
    assert editor.settles(
        lambda: pinned(editor, "note", "why-here")["position"] == {"x": 900.0, "y": -300.0},
        timeout=TIMEOUT_MS / 1000,
    ), "one drag has to be one undo"


def test_double_clicking_a_note_retypes_it(open_editor: OpenEditor) -> None:
    """A note is text, so editing it is typing over it — not a form about it."""
    editor = annotated(open_editor)
    box = note_shape(editor).bounding_box()
    assert box is not None
    editor.page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    field = editor.page.locator(".note-edit-text")
    expect(field).to_be_visible()
    assert "cupboard" in field.input_value(), "the box opens on what the note says"
    field.fill("Rewritten **here**.")
    editor.press("Control+Enter")

    assert editor.settles(
        lambda: "Rewritten" in editor.read("annotations.yaml"), timeout=TIMEOUT_MS / 1000
    ), "committing the text box has to reach the file"
    assert "cupboard" not in editor.read("annotations.yaml")
    # Only the text: a retype is not a move.
    assert pinned(editor, "note", "why-here")["position"] == {"x": 900.0, "y": -300.0}


def test_escape_leaves_a_note_exactly_as_it_was(open_editor: OpenEditor) -> None:
    """The half that has to be free: abandoning an edit writes nothing at all."""
    editor = annotated(open_editor)
    assert editor.session is not None
    before = editor.session.revision
    original = editor.read("annotations.yaml")

    box = note_shape(editor).bounding_box()
    assert box is not None
    editor.page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    expect(editor.page.locator(".note-edit-text")).to_be_visible()
    editor.page.locator(".note-edit-text").fill("never mind")
    editor.press("Escape")

    expect(editor.page.locator(".note-edit-text")).to_have_count(0)
    editor.page.wait_for_timeout(300)
    assert editor.read("annotations.yaml") == original
    assert editor.session.revision == before


def test_the_canvas_menu_drops_a_note_and_opens_it_for_typing(
    open_editor: OpenEditor,
) -> None:
    """Two doors, one command: the menu row runs what Shift-N runs."""
    editor = annotated(open_editor)
    open_menu_on_canvas(editor)
    expect(menu_row(editor, "annotation.create")).to_be_visible()
    menu_row(editor, "annotation.create").click()

    assert editor.settles(
        lambda: "name: note-1" in editor.read("annotations.yaml"), timeout=TIMEOUT_MS / 1000
    ), "the menu's New note never reached a file"
    # The name is generated past what is already there, and the box opens on it
    # so that the placeholder is the first thing typed over.
    expect(editor.page.locator(".note-edit-text")).to_be_visible(timeout=TIMEOUT_MS)
    assert editor.page.locator(".note-edit-text").input_value() == "New note"


def test_the_first_note_in_an_inventory_lands_where_the_keyboard_asked(
    open_editor: OpenEditor,
) -> None:
    """The gesture has to work on a tree that has never had a note in it.

    Which is the case it would be easiest to get wrong: with nothing written on
    the diagram there is no annotation payload, and a canvas that only built its
    overlay when there was one would have no coordinate frame to place the first
    note in — and would drop it at the origin.
    """
    editor = arranged(open_editor)
    editor.page.locator("#canvas").focus()
    editor.press("Shift+N")

    assert editor.settles(
        lambda: (editor.root / "annotations.yaml").exists(), timeout=TIMEOUT_MS / 1000
    ), "Shift-N has to write a document"
    written = editor.read("annotations.yaml")
    assert "kind: note" in written and "name: note-1" in written
    placed = editor.api(f"/api/graph?view={ARRANGED_LAYER}")["annotations"]["notes"][0]
    # In the middle of what is on screen, which is the middle of the drawing --
    # emphatically not the origin.
    assert placed["layout"]["position"]["x"] > 100.0


def test_a_note_made_over_an_element_is_anchored_to_it(open_editor: OpenEditor) -> None:
    """Which is the difference between a note about a switch and a note at x: 400."""
    editor = annotated(open_editor)
    open_menu_on(editor, "switches/sw-home")
    menu_row(editor, "annotation.create").click()

    assert editor.settles(
        lambda: "element: switches/sw-home" in editor.read("annotations.yaml"),
        timeout=TIMEOUT_MS / 1000,
    ), "a note dropped on a device has to be anchored to it"
    assert "geometry" not in editor.read("annotations.yaml").split("name: note-1")[-1]


def test_an_area_pinned_to_a_rectangle_is_resized_by_its_corner(
    open_editor: OpenEditor,
) -> None:
    editor = annotated(open_editor)
    band_ = editor.page.locator(".ng-anno-band:not([data-follows])").first
    box = band_.bounding_box()
    assert box is not None
    # On the outline rather than in the middle: the band is a stroke, so that the
    # inside of a zone stays clickable for the devices in it.
    editor.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + 1)

    handle = editor.page.locator(".ng-anno-handle-corner").first
    expect(handle).to_be_attached()
    start = _centre(handle)
    mouse = editor.page.mouse
    mouse.move(*start)
    mouse.down()
    mouse.move(start[0] - 30, start[1] - 20)
    mouse.move(start[0] - 60, start[1] - 40)
    mouse.up()

    assert editor.settles(
        lambda: (
            pinned(editor, "area", "on-the-ups").get("size") != {"width": 400.0, "height": 200.0}
        ),
        timeout=TIMEOUT_MS / 1000,
    ), "dragging a corner has to write the rectangle"
    assert "width:" in editor.read("annotations.yaml")


def test_an_area_drawn_round_its_members_says_why_it_will_not_move(
    open_editor: OpenEditor,
) -> None:
    """The choice made here, stated: refused with a reason rather than converted.

    Turning a zone that means "wherever these two devices are" into one that
    means "this rectangle" is a change of meaning, and a change of meaning must
    not be a side effect of a drag. So the gesture is refused, and the refusal
    names both ways out.
    """
    editor = annotated(open_editor)
    assert editor.session is not None
    before = editor.session.revision
    band_ = editor.page.locator(".ng-anno-band[data-follows]").first
    box = band_.bounding_box()
    assert box is not None

    mouse = editor.page.mouse
    mouse.move(box["x"] + box["width"] / 2, box["y"] + 1)
    mouse.down()
    mouse.move(box["x"] + box["width"] / 2 + 60, box["y"] + 40)
    mouse.up()

    expect(editor.page.locator("#toast")).to_contain_text("drawn round its members")
    editor.page.wait_for_timeout(300)
    assert editor.session.revision == before, "a refused drag must write nothing"


def test_deleting_a_selected_note_removes_its_document(open_editor: OpenEditor) -> None:
    """The ordinary Delete gesture, on something that is not an element."""
    editor = annotated(open_editor)
    note_shape(editor).click()
    editor.page.locator("#canvas").focus()
    editor.press("Delete")

    prompt = editor.page.locator(".prompt")
    expect(prompt).to_be_visible()
    # Named by kind as well as by name, because a note and a switch may share one.
    assert prompt.locator("input").first.input_value() == "note/why-here"
    prompt.locator('button[type="submit"]').click()

    assert editor.settles(
        lambda: "name: why-here" not in editor.read("annotations.yaml"), timeout=TIMEOUT_MS / 1000
    ), "deleting a note has to remove its document"
    # And nothing else: no cable dies with a callout.
    assert "name: the-desk" in editor.read("annotations.yaml")
    assert (editor.root / "switches" / "sw-home.yaml").exists()


def test_the_annotation_toggle_takes_the_commentary_off_the_canvas(
    open_editor: OpenEditor,
) -> None:
    """A per-view switch, reachable from the keyboard like every other one."""
    editor = annotated(open_editor)
    editor.page.locator("#canvas").focus()
    editor.press("Alt+n")

    expect(note_shape(editor)).to_have_count(0)
    assert editor.page.locator(".ng-anno-band").count() == 0
    assert "annotations off" in editor.announced()
    # Nothing was written: hiding commentary is a way of looking at the diagram.
    assert "name: why-here" in editor.read("annotations.yaml")


def test_a_read_only_session_shows_the_commentary_and_offers_no_handle(
    open_editor: OpenEditor,
) -> None:
    editor = annotated(open_editor, writable=False)
    note_shape(editor).click()
    editor.page.wait_for_timeout(300)
    assert editor.page.locator(".ng-anno-handle").count() == 0
    box = note_shape(editor).bounding_box()
    assert box is not None
    editor.page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    assert editor.page.locator(".note-edit-text").count() == 0


# --------------------------------------------------------------------------- #
# The context menu
# --------------------------------------------------------------------------- #
#
# The other door into the commands, and the only one somebody who has never
# pressed Ctrl-K will find. Everything asserted here is about the *wiring*: the
# menu draws itself from netgraph.web.bindings, aims at what was clicked, and
# hands off to the same handlers the keyboard runs. What each of those handlers
# then does is already tested above, once.


def menu(editor: Editor) -> Locator:
    return editor.page.locator(".menu-layer .menu").first


def menu_row(editor: Editor, command: str) -> Locator:
    """One row of the open menu, by the command it runs rather than its wording.

    The label is the table's to change; the id is the contract.
    """
    return editor.page.locator(f'.menu-layer .menu-item[data-command="{command}"]').first


def open_menu_on(editor: Editor, address: str) -> Locator:
    """Right-click a shape and wait for its menu."""
    press_on(editor, editor.shape(address), button="right")
    expect(menu(editor)).to_be_visible()
    return menu(editor)


def open_menu_on_canvas(editor: Editor) -> Locator:
    """Right-click the paper between the shapes — the corner nothing is drawn in."""
    box = editor.page.locator("#canvas").bounding_box()
    assert box is not None
    editor.page.mouse.click(
        box["x"] + box["width"] - 24, box["y"] + box["height"] - 24, button="right"
    )
    expect(menu(editor)).to_be_visible()
    return menu(editor)


def test_right_clicking_an_element_offers_what_acts_on_it(open_editor: OpenEditor) -> None:
    """The menu is drawn from the binding table, and says what it is drawn on.

    The heading is the element's *address* on purpose: two switches called
    something similar are told apart by nothing else, and Delete is on this menu.
    """
    editor = open_editor(writable=True)

    panel = open_menu_on(editor, "switches/sw-home")

    expect(panel.locator(".menu-head")).to_have_text("switches/sw-home")
    for command in ("node.inspect", "element.connect", "element.rename", "element.delete"):
        expect(menu_row(editor, command)).to_be_visible()
    # Every row teaches its own shortcut, the way a palette row does.
    expect(menu_row(editor, "element.delete").locator(".menu-chord")).to_have_text("Delete")
    # And nothing that belongs to another target leaked in.
    expect(menu_row(editor, "link.bend")).to_have_count(0)
    expect(menu_row(editor, "element.create")).to_have_count(0)


def test_the_menu_aims_at_what_was_clicked_not_at_what_was_focused(
    open_editor: OpenEditor,
) -> None:
    """The whole reason the pointer needs its own door.

    Every edit command defaults to "the focused element", which is right for a
    keyboard and useless for a right-click on a third one. So opening the menu
    moves the focus first, and this is the assertion that says so: the form the
    menu opens is filled in with the shape under the cursor.
    """
    editor = open_editor(writable=True)
    editor.shape("switches/sw-home").click()

    open_menu_on(editor, "hosts/pc-desk")
    menu_row(editor, "element.rename").click()

    expect(editor.page.locator(".prompt")).to_be_visible()
    assert editor.page.input_value(".prompt input") == "hosts/pc-desk"


def test_creating_from_the_canvas_menu_writes_the_document(open_editor: OpenEditor) -> None:
    """Resource creation, the way somebody who has never read the docs finds it.

    Right-click the paper, pick a kind, name it — and a file appears, through
    the same ``netgraph edit create`` the palette and ``n`` reach.
    """
    editor = open_editor(writable=True)
    assert editor.session is not None
    before = editor.session.revision

    open_menu_on_canvas(editor)
    expect(menu_row(editor, "element.create")).to_be_visible()
    menu_row(editor, "element.create").click()

    # The submenu is the kinds, from netgraph.models.KINDS by way of the API.
    kinds = editor.page.locator(".menu-sub .menu-item")
    expect(kinds).to_have_count(len(KINDS))
    editor.page.locator('.menu-sub .menu-item[data-kind="router"]').click()

    prompt = editor.page.locator(".prompt")
    expect(prompt).to_be_visible()
    assert editor.page.input_value(".prompt select") == "router", "the kind was already answered"
    prompt.locator("input").first.fill("rtr-from-the-menu")
    prompt.locator('button[type="submit"]').click()

    assert editor.settles(
        lambda: (editor.root / "rtr-from-the-menu.yaml").exists(), timeout=TIMEOUT_MS / 1000
    ), "the create the menu asked for never reached a file"
    assert editor.session is not None and editor.session.revision != before
    created = editor.read("rtr-from-the-menu.yaml")
    assert "kind: router" in created and "name: rtr-from-the-menu" in created


def test_right_clicking_a_link_offers_its_routing(open_editor: OpenEditor) -> None:
    """A cable's menu is about its *shape*, which is the half it alone has."""
    editor = arranged(open_editor)

    press_on(editor, band(editor), button="right")
    expect(menu(editor)).to_be_visible()

    expect(menu(editor).locator(".menu-head")).to_contain_text("cbl-sw-desk")
    for command in ("link.bend", "link.straighten", "link.route"):
        expect(menu_row(editor, command)).to_be_visible()
    # A cable is deleted by disconnecting it, and the row says so.
    expect(menu_row(editor, "element.delete")).to_have_text(re.compile("Disconnect"))


def test_right_clicking_a_bend_removes_it_and_shows_no_menu(open_editor: OpenEditor) -> None:
    """The one gesture the menu must not swallow.

    A handle is a control, and burying "remove this bend" two rows into a menu
    would trade a gesture for a list. Both halves matter: the bend goes, and the
    menu does not come.
    """
    editor = arranged(open_editor)
    original = len(bends(editor))
    box = band(editor).bounding_box()
    assert box is not None
    editor.page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 4)
    assert editor.settles(lambda: len(bends(editor)) == original + 1, timeout=TIMEOUT_MS / 1000), (
        "no bend to right-click"
    )

    press_on(editor, editor.page.locator(".ng-handle-bend").first, button="right")

    assert editor.settles(lambda: len(bends(editor)) == original, timeout=TIMEOUT_MS / 1000), (
        "the bend outlived its right-click"
    )
    expect(menu(editor)).to_have_count(0)


def test_a_read_only_session_greys_the_rows_that_would_write(open_editor: OpenEditor) -> None:
    """Greyed with the reason, never missing.

    "Why is Delete grey" is a question the interface can answer; "where did
    Delete go" is one it cannot. Clicking it anyway says the same thing out loud
    rather than writing.
    """
    editor = open_editor(writable=False)
    before = editor.read("switches/sw-home.yaml")

    open_menu_on(editor, "switches/sw-home")

    delete = menu_row(editor, "element.delete")
    expect(delete).to_have_class(re.compile("unavailable"))
    expect(delete.locator(".menu-why")).to_contain_text("read-only")
    # What does not write is still offered, because it still works.
    expect(menu_row(editor, "node.inspect")).not_to_have_class(re.compile("unavailable"))

    # With the real mouse: the row is `aria-disabled`, which Playwright rightly
    # declines to click and a person can click all day. What it must do then is
    # say why — the same refusal the palette gives — and write nothing.
    press_on(editor, delete)

    expect(editor.page.locator("#toast")).to_contain_text("read-only")
    expect(editor.page.locator(".prompt")).to_have_count(0)
    assert editor.read("switches/sw-home.yaml") == before


def test_the_menu_opens_and_walks_from_the_keyboard(open_editor: OpenEditor) -> None:
    """A menu only a mouse can open is a set of commands somebody does not have.

    Shift-F10 is what a screen reader sends for a context menu, so it opens this
    one on whatever the diagram has focused — and the arrow keys walk it, and
    Escape leaves it with the focus back where it started.
    """
    editor = open_editor(writable=True)
    editor.press("Alt+3")
    editor.press("Home")

    editor.press("Shift+F10")

    panel = menu(editor)
    expect(panel).to_be_visible()
    expect(panel.locator(".menu-list")).to_have_attribute("role", "menu")
    first = panel.locator(".menu-item").first
    expect(first).to_be_focused()
    editor.press("ArrowDown")
    expect(panel.locator(".menu-item").nth(1)).to_be_focused()

    editor.press("Escape")

    expect(menu(editor)).to_have_count(0)
    expect(editor.page.locator("#canvas")).to_be_focused()


def test_clicking_off_the_menu_closes_it(open_editor: OpenEditor) -> None:
    """It is a menu, not a dialog: anywhere else dismisses it and writes nothing."""
    editor = open_editor(writable=True)
    open_menu_on(editor, "switches/sw-home")

    editor.page.mouse.click(40, 40)

    expect(menu(editor)).to_have_count(0)


# --------------------------------------------------------------------------- #
# Read-only
# --------------------------------------------------------------------------- #


def test_a_read_only_session_disables_every_mutating_control(open_editor: OpenEditor) -> None:
    """ "I only wanted to look at it" must not be able to become a write.

    Both halves are asserted: the page offers no control that would write, and
    the routes refuse one that is asked for anyway — because a page is not a
    security boundary and the buttons being gone is not the reason nothing is
    written.
    """
    editor = open_editor(writable=False)
    page = editor.page
    relative = "switches/sw-home.yaml"
    original = editor.read(relative)
    # The refusals this test drives on purpose. Chromium logs every non-2xx
    # response as a console error, and a 403 here is the assertion, not a defect.
    editor.console.allow("403")

    expect(page.locator("#session-actions")).to_be_hidden()
    expect(page.locator("#files-mode")).to_have_text("read-only")

    page.locator(f'#file-list .file[data-path="{relative}"]').click()
    expect(page.locator("#editor-title")).to_have_text(relative)
    expect(page.locator("#source")).to_have_value(original)
    expect(page.locator("#source")).to_have_attribute("readonly", "")
    expect(page.locator("#editor-hint")).to_have_text("read-only session")

    page.keyboard.press("Control+s")
    page.keyboard.press("Control+z")

    # Every route that could write, asked from the page's own origin.
    statuses = page.evaluate(
        """async (path) => {
            const answers = {};
            const put = await fetch('/api/file/' + path, {
              method: 'PUT',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({text: 'clobbered\\n'})
            });
            answers.put = put.status;
            for (const route of ['ops', 'undo', 'redo']) {
              const response = await fetch('/api/' + route, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ops: [{op: 'delete-element', address: 'switches/sw-home'}]})
              });
              answers[route] = response.status;
            }
            return answers;
        }""",
        relative,
    )
    assert statuses == {"put": 403, "ops": 403, "undo": 403, "redo": 403}
    assert editor.read(relative) == original


# --------------------------------------------------------------------------- #
# Two writers, one tree
# --------------------------------------------------------------------------- #


def test_the_poll_notices_a_change_made_outside_the_session(open_editor: OpenEditor) -> None:
    """``$EDITOR`` writes the file; the open page has to hear about it.

    Clean, the text is replaced with what is on disk. Dirty, it is left alone
    and marked — unsaved work is not something to throw away quietly.
    """
    editor = open_editor(writable=True, watch=True)
    page = editor.page
    relative = "hosts/srv-nas.yaml"
    original = editor.read(relative)

    page.locator(f'#file-list .file[data-path="{relative}"]').click()
    expect(page.locator("#source")).to_have_value(original)

    elsewhere = original.replace(
        "description: Backup and media server", "description: Renamed in another editor"
    )
    assert elsewhere != original
    editor.write(relative, elsewhere)

    # No typing here, so the page adopts the file rather than defending anything.
    expect(page.locator("#source")).to_have_value(elsewhere, timeout=TIMEOUT_MS)
    expect(page.locator("#viewport")).to_contain_text("srv-nas")

    # Now with unsaved edits in the pane, the same change is a conflict.
    page.locator("#source").fill(elsewhere + "\n# typed in the browser\n")
    expect(page.locator("#editor-state")).to_have_text("unsaved changes")
    editor.write(relative, original)

    expect(page.locator("#editor-state")).to_have_text("changed on disk since you opened it")
    expect(page.locator("#editor-state")).to_have_class(re.compile(r"\bconflict\b"))
    expect(page.locator("#toast")).to_contain_text("changed on disk and has unsaved edits here")
    assert editor.read(relative) == original, "the page wrote nothing while noticing"


def test_a_stale_save_is_refused_rather_than_clobbering(open_editor: OpenEditor) -> None:
    """A tab left open over lunch must not undo what was done in ``$EDITOR``.

    The session is deliberately unwatched, so the page still believes it holds
    the current file: what refuses the write is the content hash the save quotes,
    which is the precondition that has to work whether or not anything noticed.
    """
    editor = open_editor(writable=True, watch=False)
    page = editor.page
    relative = "hosts/pc-desk.yaml"
    original = editor.read(relative)
    editor.console.allow("409")

    page.locator(f'#file-list .file[data-path="{relative}"]').click()
    expect(page.locator("#source")).to_have_value(original)

    page.locator("#source").fill(original + "\n# typed in the browser\n")
    expect(page.locator("#save")).to_be_enabled()

    elsewhere = original + "\n# written by another editor\n"
    editor.write(relative, elsewhere)

    page.keyboard.press("Control+s")

    expect(page.locator("#toast")).to_contain_text("changed on disk since it was opened")
    expect(page.locator("#toast")).to_contain_text("save again to overwrite it")
    expect(page.locator("#editor-state")).to_have_text("changed on disk since you opened it")
    assert editor.read(relative) == elsewhere, "the other editor's work is still there"


# --------------------------------------------------------------------------- #
# The changes drawer
# --------------------------------------------------------------------------- #


def _edit_a_file(editor: Editor, relative: str = "hosts/pc-desk.yaml") -> str:
    """Make one reviewable change through the page, and return the file path."""
    page = editor.page
    original = editor.read(relative)
    page.locator(f'#file-list .file[data-path="{relative}"]').click()
    expect(page.locator("#source")).to_have_value(original)
    page.locator("#source").fill(original.replace("OptiPlex 7010", "OptiPlex 7020", 1))
    expect(page.locator("#save")).to_be_enabled()
    page.keyboard.press("Control+s")
    expect(page.locator("#toast")).to_contain_text(f"saved {relative}")
    return relative


def test_the_changes_drawer_lists_the_session_and_paints_the_diff(
    open_editor: OpenEditor,
) -> None:
    """The whole feature, end to end: edit, open the drawer, read the change.

    What is asserted is what a reviewer would look at — the gesture named once,
    the YAML hunk it produced, and the diagram repainted as a diff — rather than
    the requests that produced them, which ``tests/test_web_session.py`` covers.
    """
    editor = open_editor(writable=True)
    page = editor.page

    # Nothing has happened yet, so the drawer says so rather than being empty.
    expect(page.locator("#changes")).to_be_hidden()
    page.locator("#changes-toggle").click()
    expect(page.locator("#changes")).to_be_visible()
    expect(page.locator("#changes-list")).to_contain_text("nothing changed yet")
    expect(page.locator("#legend")).to_be_visible()
    expect(page.locator("#summary")).to_contain_text("nothing has changed yet")
    page.locator("#changes-close").click()
    expect(page.locator("#changes")).to_be_hidden()

    relative = _edit_a_file(editor)

    page.locator("#changes-toggle").click()
    entry = page.locator("#changes-list .change").first
    expect(entry).to_contain_text(f"edit {relative}")
    expect(entry).to_contain_text(relative)
    # The hunk is the point of the entry: the text that was actually written.
    expect(entry.locator("pre .del")).to_contain_text("OptiPlex 7010")
    expect(entry.locator("pre .add")).to_contain_text("OptiPlex 7020")
    expect(page.locator("#changes-count")).to_have_text("(1)")

    # And the canvas is now a diff: one amber box, badged with the field.
    expect(page.locator("#summary")).to_contain_text("1 changed")
    expect(page.locator("#viewport")).to_contain_text("spec.model")


def test_a_drawer_entry_reveals_the_document_it_changed(open_editor: OpenEditor) -> None:
    """Click-to-reveal, from the log rather than from the diagram."""
    editor = open_editor(writable=True)
    page = editor.page
    relative = _edit_a_file(editor)

    # Open something else, so that revealing has somewhere to travel from.
    page.locator('#file-list .file[data-path="switches/sw-home.yaml"]').click()
    expect(page.locator("#editor-title")).to_have_text("switches/sw-home.yaml")

    page.locator("#changes-toggle").click()
    page.locator("#changes-list .change .label").first.click()
    expect(page.locator("#editor-title")).to_have_text(relative)


def test_reverting_one_entry_puts_that_change_back(open_editor: OpenEditor) -> None:
    """A revert is a new change, not a rewind: the log keeps both."""
    editor = open_editor(writable=True)
    page = editor.page
    relative = "hosts/pc-desk.yaml"
    original = editor.read(relative)
    _edit_a_file(editor, relative)
    assert editor.read(relative) != original

    page.locator("#changes-toggle").click()
    # Every row now has two controls -- the label reveals, the button reverts --
    # because a row that does something has to be reachable with a keyboard.
    page.locator("#changes-list .change button.revert").first.click()

    expect(page.locator("#toast")).to_contain_text("put change #1 back")
    assert editor.read(relative) == original, "a revert restores the file byte for byte"
    # Two entries now — the change and its reversal — and the first is marked.
    expect(page.locator("#changes-list .change")).to_have_count(2)
    expect(page.locator("#changes-list .change.reverted")).to_have_count(1)
    expect(page.locator("#summary")).to_contain_text("nothing has changed yet")


def test_the_handover_copies_the_equivalent_edit_commands(open_editor: OpenEditor) -> None:
    """A change explored visually has to be able to leave the browser."""
    editor = open_editor(writable=True)
    page = editor.page
    _edit_a_file(editor)

    page.locator("#changes-toggle").click()
    expect(page.locator("#changes-copy")).to_be_enabled()

    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.locator("#changes-copy").click()
    expect(page.locator("#toast")).to_contain_text("copied 1 command")

    copied = str(page.evaluate("() => navigator.clipboard.readText()"))
    # A whole-file save has no subcommand of its own, so it hands over as the
    # exact JSON form. What matters is that it is runnable and complete.
    assert copied.startswith("echo ") or copied.startswith("netgraph ")
    assert "netgraph" in copied and "edit" in copied


def test_a_read_only_session_offers_the_drawer_but_no_revert(
    open_editor: OpenEditor,
) -> None:
    """Reviewing is a read; only putting a change back is a write."""
    editor = open_editor(writable=False)
    page = editor.page
    # The toggle lives in the session actions, which a read-only page hides
    # wholesale — the drawer is reachable from the API, and the API answers.
    expect(page.locator("#session-actions")).to_be_hidden()
    payload = editor.api("/api/changes")
    assert payload["entries"] == []
    assert payload["baselines"] == ["session"]


# --------------------------------------------------------------------------- #
# The history timeline
# --------------------------------------------------------------------------- #


def _a_history(root: Path) -> list[str]:
    """Put the home lab in a repository with four commits over it.

    Built before the session opens, so ``open_editor`` finds the tree already
    there and serves it as it is. Returns the subjects, oldest first.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(HOME_LAB, root, dirs_exist_ok=True)

    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)

    git("init", "-q", ".")
    git("config", "user.email", "scrub@example.invalid")
    git("config", "user.name", "Scrubber")
    git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-qm", "Bring the home lab under description")

    (root / "switches" / "sw-lab.yaml").write_text(NEW_SWITCH, encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "Add a lab switch")

    (root / "broken.yaml").write_text("this: [is not\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "Break the tree on purpose")

    (root / "broken.yaml").unlink()
    git("add", "-A")
    git("commit", "-qm", "Unbreak the tree")
    return [
        "Bring the home lab under description",
        "Add a lab switch",
        "Break the tree on purpose",
        "Unbreak the tree",
    ]


def test_the_timeline_scrubs_the_inventory_across_its_commits(
    open_editor: OpenEditor, tmp_path: Path
) -> None:
    """The feature, end to end: open the scrubber, step back, watch it repaint.

    What is asserted is what a reader would look at — the commit named beside
    the control, the diagram becoming that commit's diff, and a revision that
    will not load saying so instead of going quietly blank.
    """
    subjects = _a_history(tmp_path / "inventory")
    editor = open_editor()
    page = editor.page

    expect(page.locator("#timeline")).to_be_hidden()
    page.locator("#timeline-toggle").click()
    expect(page.locator("#timeline")).to_be_visible()
    expect(page.locator("#legend")).to_be_visible()

    # It opens on the newest commit, named in full: a hash alone places nothing.
    expect(page.locator("#timeline-subject")).to_have_text(subjects[-1])
    expect(page.locator("#timeline-who")).to_contain_text("Scrubber")
    expect(page.locator("#timeline-hash")).not_to_have_text("")
    expect(page.locator("#timeline-range")).to_have_value("3")

    # "Unbreak the tree" is drawn against a revision that does not load, so the
    # frame says which one rather than presenting an empty diagram as an answer.
    expect(page.locator("#timeline")).to_have_class(re.compile(r"\bbroken\b"))
    expect(page.locator("#timeline-summary")).to_contain_text("does not load")
    expect(page.locator("#placeholder")).to_be_visible()

    page.locator("#timeline-prev").click()
    expect(page.locator("#timeline-subject")).to_have_text(subjects[2])
    expect(page.locator("#timeline-summary")).to_contain_text("does not load")

    # Back one more and the history is readable again: the commit that added a
    # switch is one green box on an otherwise untouched diagram.
    page.locator("#timeline-prev").click()
    expect(page.locator("#timeline-subject")).to_have_text(subjects[1])
    expect(page.locator("#timeline-summary")).to_have_text("1 device added")
    expect(page.locator("#timeline")).not_to_have_class(re.compile(r"\bbroken\b"))
    expect(page.locator("#viewport svg")).to_be_visible()
    expect(page.locator("#viewport")).to_contain_text("sw-lab")

    # And the first commit of the repository is the whole network arriving.
    page.locator("#timeline-prev").click()
    expect(page.locator("#timeline-subject")).to_have_text(subjects[0])
    expect(page.locator("#timeline-summary")).to_contain_text("devices added")
    expect(page.locator("#timeline-prev")).to_be_disabled()

    # Leaving the history puts the working tree back, switch and all.
    page.locator("#timeline-now").click()
    expect(page.locator("#timeline")).to_be_hidden()
    expect(page.locator("#legend")).to_be_hidden()
    expect(page.locator("#viewport")).to_contain_text("sw-lab")


def test_the_timeline_plays_through_the_range(open_editor: OpenEditor, tmp_path: Path) -> None:
    """The play control steps by itself, and stops where it cannot go on."""
    _a_history(tmp_path / "inventory")
    editor = open_editor()
    page = editor.page

    page.locator("#timeline-toggle").click()
    expect(page.locator("#timeline-range")).to_have_value("3")
    page.locator("#timeline-range").fill("0")
    page.locator("#timeline-range").dispatch_event("input")
    expect(page.locator("#timeline-range")).to_have_value("0")

    page.locator("#timeline-play").click()

    # It stops of its own accord at the revision that will not load, rather than
    # flicking past the one frame worth stopping on. Where it stopped is what is
    # asserted, not that it was ever seen mid-flight: a three-frame range can be
    # over before a poll catches it, and a test that raced it would be flaky
    # about the one thing that is deterministic.
    expect(page.locator("#timeline-summary")).to_contain_text("does not load", timeout=TIMEOUT_MS)
    expect(page.locator("#timeline-play")).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#timeline-range")).to_have_value("2")


def test_the_timeline_keeps_each_revisions_own_arrangement(
    open_editor: OpenEditor, tmp_path: Path
) -> None:
    """A diagram that was arranged stays arranged as you scrub back to it.

    The layout document is part of the inventory, so it is read out of the same
    revision as everything else. Nothing special happens here — which is the
    claim being checked.
    """
    root = tmp_path / "inventory"
    _a_history(root)
    (root / "layout.yaml").write_text(FIXED_LAYOUT, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "Arrange the diagram"], cwd=root, check=True, capture_output=True
    )
    editor = open_editor()
    page = editor.page

    page.locator("#timeline-toggle").click()
    expect(page.locator("#timeline-subject")).to_have_text("Arrange the diagram")
    arranged = editor.api(f"/api/frame?rev=HEAD&{PAGE_QUERY}")
    assert arranged["geometry"] is not None

    # The revision before it was committed has no arrangement, and says so
    # rather than borrowing this one.
    before = editor.api(f"/api/frame?rev=HEAD~1&{PAGE_QUERY}")
    assert before["geometry"] is None
    assert before["subject"] == "Unbreak the tree"


def test_a_session_with_no_repository_says_so_in_the_timeline(
    open_editor: OpenEditor,
) -> None:
    """No history is a thing to report, not a control that does nothing."""
    editor = open_editor()
    page = editor.page

    page.locator("#timeline-toggle").click()

    expect(page.locator("#timeline")).to_be_visible()
    expect(page.locator("#timeline-subject")).to_contain_text("not inside a git repository")
    expect(page.locator("#timeline-range")).to_be_disabled()
    expect(page.locator("#timeline-play")).to_be_disabled()
    editor.console.allow("400")


def test_the_timeline_has_no_accessibility_violations(
    open_editor: OpenEditor, tmp_path: Path
) -> None:
    _a_history(tmp_path / "inventory")
    editor = open_editor()
    editor.page.locator("#timeline-toggle").click()
    expect(editor.page.locator("#timeline-subject")).not_to_have_text("")

    violations = _violations(editor)
    assert not violations, "the history timeline:\n" + _explain(violations)


# --------------------------------------------------------------------------- #
# Two tabs, one session
# --------------------------------------------------------------------------- #


def test_the_page_says_it_is_on_the_event_stream(open_editor: OpenEditor) -> None:
    """The indicator is the honest answer to "why is this tab behind"."""
    editor = open_editor(writable=True)
    expect(editor.page.locator("#link-state")).to_have_text("live", timeout=TIMEOUT_MS)


def test_a_second_tab_appears_in_the_first(open_editor: OpenEditor) -> None:
    editor = open_editor(writable=True)
    other = open_editor(beside=editor)
    # Each page lists the *other* one, and neither lists itself.
    expect(editor.page.locator("#clients .client")).to_have_count(1, timeout=TIMEOUT_MS)
    expect(other.page.locator("#clients .client")).to_have_count(1, timeout=TIMEOUT_MS)


def test_a_file_another_tab_is_typing_in_is_badged_but_not_locked(
    open_editor: OpenEditor,
) -> None:
    """A soft lock: shown, and then deliberately ignored.

    The badge is a courtesy between two people who can see each other. What
    refuses a write is the content hash, so the second tab saving the file it was
    warned about must simply work.
    """
    editor = open_editor(writable=True)
    other = open_editor(beside=editor)
    relative = "hosts/pc-desk.yaml"

    other.page.locator(f'#file-list .file[data-path="{relative}"]').click()
    other.page.locator("#source").fill(other.read(relative) + "\n# typed elsewhere\n")
    expect(other.page.locator("#editor-state")).to_have_text("unsaved changes")

    row = editor.page.locator(f'#file-list .file[data-path="{relative}"]')
    expect(row.locator(".badge.elsewhere")).to_have_text("in use", timeout=TIMEOUT_MS)

    # And it blocks nothing: this tab opens the same file and saves it.
    row.click()
    editor.page.locator("#source").fill(editor.read(relative) + "\n# and here\n")
    editor.page.locator("#save").click()
    expect(editor.page.locator("#toast")).to_contain_text("saved " + relative)
    assert editor.read(relative).endswith("# and here\n")


def test_what_another_tab_selected_is_drawn_faintly(open_editor: OpenEditor) -> None:
    editor = open_editor(writable=True)
    other = open_editor(beside=editor)
    address = "switches/sw-home"

    other.shape(address).click()
    # The other tab's shape is marked; this tab's own pick is not, so the two
    # cannot be mistaken for one another.
    expect(
        editor.page.locator(f'#viewport g.remote[id="{editor.element_id(address)}"]')
    ).to_be_attached(timeout=TIMEOUT_MS)
    expect(other.page.locator("#viewport g.remote")).to_have_count(0)


def test_a_save_in_one_tab_reaches_the_other_without_a_full_refetch(
    open_editor: OpenEditor,
) -> None:
    """The push channel, end to end, and the incremental path underneath it.

    The second tab has the file open and clean, so it adopts what was written —
    and it does so from a partial fetch of that one row, which is what the
    request log shows.
    """
    editor = open_editor(writable=True)
    other = open_editor(beside=editor)
    relative = "hosts/pc-desk.yaml"

    other.page.locator(f'#file-list .file[data-path="{relative}"]').click()
    expect(other.page.locator("#source")).to_have_value(other.read(relative))

    requested: list[str] = []
    other.page.on("request", lambda request: requested.append(request.url))

    editor.page.locator(f'#file-list .file[data-path="{relative}"]').click()
    editor.page.locator("#source").fill(editor.read(relative) + "\n# pushed across\n")
    editor.page.locator("#save").click()
    expect(editor.page.locator("#toast")).to_contain_text("saved " + relative)

    expect(other.page.locator("#source")).to_have_value(editor.read(relative), timeout=TIMEOUT_MS)
    trees = [url for url in requested if "/api/tree" in url]
    assert trees, "the other tab did not refresh its file list at all"
    assert all("path=" in url for url in trees), (
        f"a single-file save refetched the whole tree: {trees}"
    )


def test_an_undo_in_one_tab_lands_in_the_other(open_editor: OpenEditor) -> None:
    """The history is the server's, so both tabs' buttons move together."""
    editor = open_editor(writable=True)
    other = open_editor(beside=editor)
    relative = "hosts/pc-desk.yaml"
    original = editor.read(relative)

    editor.page.locator(f'#file-list .file[data-path="{relative}"]').click()
    editor.page.locator("#source").fill(original + "\n# undo me\n")
    editor.page.locator("#save").click()
    expect(editor.page.locator("#toast")).to_contain_text("saved " + relative)

    # The other tab's Undo comes alive without it having done anything.
    expect(other.page.locator("#undo")).to_be_enabled(timeout=TIMEOUT_MS)
    other.page.locator("#undo").click()

    expect(other.page.locator("#toast")).to_contain_text("undone")
    assert editor.read(relative) == original, "an undo restores the bytes"
    # And the tab that made the change is told: its Undo is spent, its Redo is not.
    expect(editor.page.locator("#undo")).to_be_disabled(timeout=TIMEOUT_MS)
    expect(editor.page.locator("#redo")).to_be_enabled(timeout=TIMEOUT_MS)


def test_an_edit_that_does_not_move_the_picture_does_not_redraw_it(
    open_editor: OpenEditor,
) -> None:
    """The fingerprint, from the browser's side.

    A description is not on the diagram. The page still asks — the problems and
    the counts can move — but it sends the hash of what it is showing and the
    server answers `unchanged`, so no layout runs and the SVG on screen is
    literally the same node it was.
    """
    editor = open_editor(writable=True)
    page = editor.page
    relative = "hosts/pc-desk.yaml"

    page.locator(f'#file-list .file[data-path="{relative}"]').click()
    before = page.evaluate("() => document.querySelector('#viewport svg').id || 'anonymous'")
    page.evaluate("() => { document.querySelector('#viewport svg').dataset.witness = 'original'; }")

    text = editor.read(relative)
    assert "description:" in text
    page.locator("#source").fill(text.replace("description:", "description: edited —", 1))
    page.locator("#save").click()
    expect(page.locator("#toast")).to_contain_text("saved " + relative)

    # The witness survives, which it could not if the SVG had been replaced.
    expect(page.locator("#viewport svg[data-witness='original']")).to_be_attached(
        timeout=TIMEOUT_MS
    )
    assert (
        page.evaluate("() => document.querySelector('#viewport svg').id || 'anonymous'") == before
    )


# --------------------------------------------------------------------------- #
# A diagram too big to draw all of
# --------------------------------------------------------------------------- #

#: How many devices the culling tests add to the home lab.
#:
#: ``cull.js`` leaves a drawing alone below ``CULL_ABOVE`` *groups*, and a group
#: is a node or a link, so this has to produce more than that between them: 320
#: computers and the 160 cables joining them in pairs is 480, comfortably over
#: the threshold of 400 — and still a layout Graphviz finishes in a fraction of
#: a second, which a browser test can afford.
CROWD: Final = 320


def a_crowd(count: int = CROWD) -> str:
    """A YAML stream of ``count`` computers, wired together in pairs.

    Deliberately dull: one interface each, one cable per pair, no addresses. The
    tests below are about how much of a drawing is *materialised*, and anything
    interesting in the documents would only be interesting in the diagram.
    """
    documents = []
    for index in range(count):
        documents.append(
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: computer\n"
            f"metadata:\n  name: crowd-{index:03d}\n"
            "spec:\n  interfaces:\n    - name: eth0\n      type: ethernet\n"
        )
    for index in range(0, count - 1, 2):
        documents.append(
            "apiVersion: netgraph.dev/v1alpha1\n"
            "kind: cable\n"
            f"metadata:\n  name: cbl-{index:03d}\n"
            f"spec:\n  endpoints: [crowd-{index:03d}:eth0, crowd-{index + 1:03d}:eth0]\n"
            "  medium: copper\n"
        )
    return "---\n".join(documents)


def crowded(open_editor: OpenEditor) -> Editor:
    """A session over an inventory big enough that the canvas culls."""
    editor = open_editor(extra={"crowd/hosts.yaml": a_crowd()})
    editor.page.wait_for_function(
        "() => window.netgraphCull && netgraphCull.stats().total > netgraphCull.CULL_ABOVE",
        timeout=TIMEOUT_MS,
    )
    return editor


def cull_stats(editor: Editor) -> dict[str, Any]:
    stats = editor.page.evaluate("() => netgraphCull.stats()")
    assert isinstance(stats, dict)
    return stats


def viewport_elements(editor: Editor) -> int:
    """How much SVG the tab is actually holding up, under ``#viewport``."""
    count = editor.page.evaluate(
        "() => document.getElementById('viewport').querySelectorAll('*').length"
    )
    assert isinstance(count, int)
    return count


def zoom(editor: Editor, notches: int) -> None:
    """Wheel over the middle of the canvas, and let the cull catch up."""
    box = editor.page.locator("#canvas").bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    for _ in range(abs(notches)):
        editor.page.mouse.move(x, y)
        editor.page.mouse.wheel(0, -200 if notches > 0 else 200)


@requires_dot
def test_a_small_diagram_is_drawn_whole(open_editor: OpenEditor) -> None:
    """Culling is for drawings that need it, and the home lab does not.

    The cost of culling is a pass over the index on every pan. Paying it to hide
    nothing would be a regression for every inventory anybody actually has.
    """
    editor = open_editor()
    stats = cull_stats(editor)
    assert stats["active"] is False, stats
    assert stats["coarse"] is False, stats


@requires_dot
def test_zooming_into_a_crowd_stops_drawing_what_is_off_screen(
    open_editor: OpenEditor,
) -> None:
    editor = crowded(open_editor)
    whole = cull_stats(editor)
    assert whole["active"] is True, whole
    held = viewport_elements(editor)

    zoom(editor, 30)
    editor.page.wait_for_function(
        "total => netgraphCull.stats().drawn < total", arg=whole["total"], timeout=TIMEOUT_MS
    )
    culled = cull_stats(editor)
    assert culled["drawn"] < whole["total"]
    # The groups themselves stay — that is what keeps every id addressable —
    # so the saving is in their contents, and it has to be a real one.
    assert viewport_elements(editor) < held


@requires_dot
def test_an_element_off_screen_is_still_findable_and_selectable(
    open_editor: OpenEditor,
) -> None:
    """The property culling is not allowed to break.

    The command palette, the outline and find-in-diagram all name an element
    that may be anywhere in the drawing. Landing on one has to work whether or
    not it happens to be materialised — so the outline entry for a device is
    clicked while zoomed into a corner it is nowhere near, and the ring has to
    end up on it.
    """
    editor = crowded(open_editor)
    page = editor.page
    zoom(editor, 30)
    page.wait_for_function(
        "() => netgraphCull.stats().drawn < netgraphCull.stats().total", timeout=TIMEOUT_MS
    )
    parked = page.evaluate(
        """() => {
             const groups = document.querySelectorAll('#viewport svg g.node');
             for (const group of groups) {
               if (!group.childElementCount) { return group.id; }
             }
             return null;
           }"""
    )
    assert parked, "nothing was culled, so this test would prove nothing"

    page.evaluate("id => netgraphA11y.focus(id, { quiet: true })", parked)
    # Materialised by being focused, and wearing the ring.
    expect(page.locator(f'#viewport svg g[id="{parked}"].focused')).to_be_attached(
        timeout=TIMEOUT_MS
    )
    assert page.evaluate(
        "id => document.querySelector('#viewport svg [id=\"' + id + '\"]').childElementCount",
        parked,
    )
    assert (
        page.evaluate(
            "() => document.getElementById('canvas').getAttribute('aria-activedescendant')"
        )
        == parked
    )


@requires_dot
def test_the_keyboard_crosses_a_culled_diagram(open_editor: OpenEditor) -> None:
    """Arrow navigation runs off the index, not off the DOM.

    a11y.js reads a candidate's centre from cull.js's box index, so an element
    whose contents are parked is still a place the keyboard can go. If it read
    ``getBBox`` instead, every arrow press would stop at the edge of the screen.
    """
    editor = crowded(open_editor)
    page = editor.page
    zoom(editor, 30)
    page.wait_for_function(
        "() => netgraphCull.stats().drawn < netgraphCull.stats().total", timeout=TIMEOUT_MS
    )
    page.locator("#canvas").focus()
    page.evaluate("() => netgraphA11y.first({ quiet: true })")
    visited = set()
    for _ in range(12):
        page.keyboard.press("ArrowRight")
        here = page.evaluate("() => { const f = netgraphA11y.focused(); return f && f.element; }")
        if here:
            visited.add(here)
    assert len(visited) > 1, "the keyboard did not move"


@requires_dot
def test_zooming_out_drops_the_detail_and_frames_the_namespaces(
    open_editor: OpenEditor,
) -> None:
    """The level-of-detail half: below the threshold, labels off, frames on."""
    editor = crowded(open_editor)
    page = editor.page
    zoom(editor, -40)
    page.wait_for_function("() => netgraphCull.stats().coarse", timeout=TIMEOUT_MS)
    expect(page.locator("#canvas.coarse")).to_be_attached(timeout=TIMEOUT_MS)
    # One frame per namespace with more than one member: the crowd, and the
    # home lab's own folders.
    assert page.locator("#viewport svg .ng-lod-frame").count() > 0
    assert page.locator("#viewport svg .ng-lod-label").count() > 0
    # A node's label is in the DOM and not rendered, which is the point: the
    # text is what a repaint at this scale was spending itself on.
    hidden = page.evaluate(
        """() => {
             const text = document.querySelector('#viewport svg g.node text');
             return text ? window.getComputedStyle(text).display : 'missing';
           }"""
    )
    assert hidden == "none", hidden

    # And zooming in far enough brings it back, because at that scale it says
    # something again. Sixty notches from the floor, not forty: the drawing
    # starts at a thirtieth of life size, so the ceiling is a long way up.
    zoom(editor, 60)
    page.wait_for_function("() => !netgraphCull.stats().coarse", timeout=TIMEOUT_MS)
    expect(page.locator("#viewport svg .ng-lod-frame")).to_have_count(0, timeout=TIMEOUT_MS)


@requires_dot
def test_a_crowd_can_be_zoomed_in_far_enough_to_read(open_editor: OpenEditor) -> None:
    """The zoom ceiling is the drawing's, not a constant.

    The SVG is sized to the canvas, so a big drawing starts tiny; a fixed 12x
    ceiling left a thousand-device diagram permanently illegible. See
    ``READABLE_SCALE`` in app.js.
    """
    editor = crowded(open_editor)
    zoom(editor, -40)
    editor.page.wait_for_function("() => netgraphCull.stats().coarse", timeout=TIMEOUT_MS)
    zoom(editor, 90)
    editor.page.wait_for_function("() => !netgraphCull.stats().coarse", timeout=TIMEOUT_MS)
    assert cull_stats(editor)["coarse"] is False


# --------------------------------------------------------------------------- #
# Accessibility
# --------------------------------------------------------------------------- #

#: Why the axe tests skip when the checker is not installed. The same shape as
#: the Playwright skip above: never a hard failure for somebody who has one half
#: of the browser layer and not the other.
NO_AXE: Final = "axe-core is not installed; pip install '.[browser]' to run the accessibility gate"


def _violations(editor: Editor, *, include: str | None = None) -> list[Mapping[str, Any]]:
    """Run axe-core over the page and return what it objected to.

    ``include`` narrows the audit to one selector, which is how a dialog is
    checked without re-reporting the page behind it.
    """
    axe = Axe()
    results = axe.run(
        editor.page,
        include,
        {"runOnly": {"type": "tag", "values": AXE_TAGS}},
    )
    found = results["violations"]
    assert isinstance(found, list)
    return found


def _left_edge(locator: Locator) -> float:
    box = locator.bounding_box()
    assert box is not None, "the element is not laid out at all"
    return float(box["x"])


def _explain(violations: list[Mapping[str, Any]]) -> str:
    """A failure somebody can act on without opening the browser themselves."""
    lines = []
    for violation in violations:
        lines.append(f"{violation['id']} ({violation['impact']}): {violation['help']}")
        lines.append(f"  {violation['helpUrl']}")
        for node in violation["nodes"][:4]:
            lines.append(f"  at {' '.join(node['target'])}")
            lines.append(f"    {node['failureSummary'].splitlines()[-1].strip()}")
    return "\n".join(lines)


@pytest.mark.skipif(not HAVE_AXE, reason=NO_AXE)
def test_the_editing_session_has_no_accessibility_violations(open_editor: OpenEditor) -> None:
    """The gate. A new WCAG 2.1 AA failure fails CI rather than shipping.

    Run against the session with everything on screen that a session has: the
    file list, the editor, the problems, the diagram and its annotated SVG. A
    page audited empty is a page audited without the half that is hard.
    """
    editor = open_editor(writable=True)
    expect(editor.page.locator("#file-list .file")).not_to_have_count(0)

    violations = _violations(editor)
    assert not violations, "axe-core found accessibility violations:\n" + _explain(violations)


@pytest.mark.skipif(not HAVE_AXE, reason=NO_AXE)
def test_the_dark_scheme_is_audited_too(open_editor: OpenEditor) -> None:
    """Following the system into dark mode is where a palette usually breaks.

    One set of colours cannot clear 4.5:1 against both a white and a near-black
    background, so app.css declares two — and this is the test that says the
    second one is not decorative.
    """
    editor = open_editor(writable=True)
    editor.page.emulate_media(color_scheme="dark")
    expect(editor.page.locator("#file-list .file")).not_to_have_count(0)
    assert (
        editor.page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        == "rgb(15, 18, 22)"
    ), "the dark tokens did not take, so this audit would have proved nothing"

    violations = _violations(editor)
    assert not violations, "the dark scheme:\n" + _explain(violations)


@pytest.mark.skipif(not HAVE_AXE, reason=NO_AXE)
def test_the_overlays_have_no_accessibility_violations(open_editor: OpenEditor) -> None:
    """The palette, the shortcut sheet and a prompt are dialogs, and are audited.

    They are also the three things a keyboard user meets first, so a violation
    here costs more than one anywhere else on the page.
    """
    editor = open_editor(writable=True)
    page = editor.page

    editor.press("Control+k")
    expect(page.locator(".palette")).to_be_visible()
    palette = _violations(editor)
    assert not palette, "the command palette:\n" + _explain(palette)

    editor.press("Escape", "?")
    expect(page.locator(".sheet")).to_be_visible()
    sheet = _violations(editor)
    assert not sheet, "the shortcut sheet:\n" + _explain(sheet)

    editor.press("Escape", "Alt+3", "n")
    expect(page.locator(".prompt")).to_be_visible()
    prompt = _violations(editor)
    assert not prompt, "the create prompt:\n" + _explain(prompt)
    editor.press("Escape")


@pytest.mark.skipif(not HAVE_AXE, reason=NO_AXE)
def test_the_context_menu_has_no_accessibility_violations(open_editor: OpenEditor) -> None:
    """A `role="menu"` is easy to build and easy to build wrongly.

    Both panels are audited, because a submenu is where the roles usually come
    apart: the list owns what its children mean, so the heading is beside it and
    the row that opens the submenu has to say that it does.
    """
    editor = open_editor(writable=True)

    open_menu_on(editor, "switches/sw-home")
    on_element = _violations(editor)
    assert not on_element, "the element's context menu:\n" + _explain(on_element)

    editor.press("Escape")
    open_menu_on_canvas(editor)
    menu_row(editor, "element.create").click()
    expect(editor.page.locator(".menu-sub")).to_be_visible()
    with_submenu = _violations(editor)
    assert not with_submenu, "the canvas menu and its submenu:\n" + _explain(with_submenu)
    editor.press("Escape", "Escape")


@pytest.mark.skipif(not HAVE_AXE, reason=NO_AXE)
def test_annotating_the_diagram_has_no_accessibility_violations(
    open_editor: OpenEditor,
) -> None:
    """The overlay and the text box a note is retyped in.

    The overlay is furniture — bands and handles say nothing a screen reader can
    use, and are marked ``aria-hidden`` so the diagram is not a field of them —
    but the text box is a real control, so it is audited with the label it
    carries and the keyboard it takes.
    """
    editor = annotated(open_editor)
    canvas = _violations(editor)
    assert not canvas, "the annotation overlay:\n" + _explain(canvas)

    box = note_shape(editor).bounding_box()
    assert box is not None
    editor.page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    expect(editor.page.locator(".note-edit-text")).to_be_visible()
    editing = _violations(editor)
    assert not editing, "the note's text box:\n" + _explain(editing)
    editor.press("Escape")


@pytest.mark.skipif(not HAVE_AXE, reason=NO_AXE)
def test_the_changes_drawer_has_no_accessibility_violations(open_editor: OpenEditor) -> None:
    """The diff view, which is where the colour-only encoding used to live.

    Its contrast is checked here rather than argued about: the tokens are
    declared per colour scheme in app.css precisely so this can pass.
    """
    editor = open_editor(writable=True)
    _edit_a_file(editor)
    editor.page.locator("#changes-toggle").click()
    expect(editor.page.locator("#changes-list .change")).not_to_have_count(0)

    violations = _violations(editor)
    assert not violations, "the changes drawer:\n" + _explain(violations)


def test_every_node_and_link_carries_a_role_and_a_label(open_editor: OpenEditor) -> None:
    """A Graphviz drawing is inert; this is what makes it not.

    The label has to come off the same record the info box uses, so the check is
    that it says what the *inventory* says -- the kind, the port count, the
    peers -- rather than merely that some string is present.
    """
    editor = open_editor()
    switch = editor.shape("switches/sw-home")

    expect(switch).to_have_attribute("role", "img")
    label = switch.get_attribute("aria-label") or ""
    assert label.startswith("sw-home, switch"), label
    assert "interfaces" in label, label
    # The peer is named by its address, which is what the rest of the interface
    # calls it: a label that said 'rtr-home' would name something the file list
    # and the palette do not have.
    assert "linked to routers/rtr-home on port1" in label, label

    # And a link says what it joins, in the same one line.
    details: Mapping[str, Any] = editor.graph()["details"]
    cable = next(key for key, record in details.items() if record.get("type") == "edge")
    edge = editor.page.locator(f'#viewport [id="{cable}"]')
    expect(edge).to_have_attribute("role", "img")
    assert " to " in (edge.get_attribute("aria-label") or "")


def test_the_outline_reads_the_view_as_text(open_editor: OpenEditor) -> None:
    """The fallback that always works: no SVG to traverse, no pointer.

    It is also the only part of the diagram a screen reader can read straight
    through, so it has to have an entry per drawn element and no more.
    """
    editor = open_editor()
    page = editor.page

    drawn = len(editor.graph()["details"])
    expect(page.locator("#outline-list li")).to_have_count(drawn)
    expect(page.locator("#outline-summary")).to_contain_text("physical view")
    assert "sw-home, switch" in page.locator("#outline-list").inner_text()

    # Off screen until it is focused, and a real panel once it is.
    assert _left_edge(page.locator("#outline")) < 0
    editor.press("Alt+4")
    assert _left_edge(page.locator("#outline")) >= 0

    # And an entry is a control: activating one moves the diagram's focus.
    page.locator("#outline-list button").first.press("Enter")
    assert editor.focus_ring(), "activating an outline entry focuses that element"


def test_a_gesture_is_announced_in_a_live_region(open_editor: OpenEditor) -> None:
    """Applied, refused, reverted: every one of them is said out loud, once."""
    editor = open_editor(writable=True)
    page = editor.page

    editor.press("Alt+3")
    expect(page.locator("#announcer")).not_to_be_empty()

    # A refusal interrupts, which is what the assertive region is for.
    editor.press("Control+z")
    expect(page.locator("#alert")).to_contain_text("nothing to undo")


# --------------------------------------------------------------------------- #
# The keyboard
# --------------------------------------------------------------------------- #


def test_the_palette_finds_a_command_and_an_element(open_editor: OpenEditor) -> None:
    """Ctrl-K over commands, element addresses and file paths, in one field."""
    editor = open_editor(writable=True)
    page = editor.page

    editor.press("Control+k")
    expect(page.locator(".palette")).to_be_visible()

    # A command, with the key that runs it printed against it -- which is how
    # the palette teaches the bindings rather than replacing them.
    page.keyboard.type("next layer")
    first = page.locator(".palette-item").first
    expect(first).to_contain_text("Next layer")
    expect(first.locator(".palette-chord")).to_have_text("]")

    # And an element of the inventory, in the same field.
    page.keyboard.press("Control+a")
    page.keyboard.type("sw-home")
    expect(page.locator(".palette-item").first).to_contain_text("switches/sw-home")
    editor.press("Enter")

    expect(page.locator(".palette")).to_have_count(0)
    assert "sw-home" in editor.focus_label()


def test_the_palette_says_why_a_command_is_out_of_reach(open_editor: OpenEditor) -> None:
    """A read-only session still lists the write commands, greyed, with a reason.

    A command that vanishes teaches nothing; a command that says "restart it
    with --write" answers the question the user actually has.
    """
    editor = open_editor(writable=False)
    page = editor.page

    editor.press("Control+k")
    page.keyboard.type("Create an element")
    row = page.locator(".palette-item").first
    expect(row).to_have_class(re.compile(r"unavailable"))
    expect(row).to_contain_text("read-only")
    editor.press("Escape")


def test_the_shortcut_sheet_comes_from_the_registered_bindings(open_editor: OpenEditor) -> None:
    """`?` renders the same table the page bound its keys from.

    Compared against ``/api/bindings`` rather than against a list in this file:
    the point of the arrangement is that there is one table, and a test with its
    own copy of it would be a third.
    """
    editor = open_editor()
    page = editor.page

    editor.press("?")
    sheet = page.locator(".sheet")
    expect(sheet).to_be_visible()

    declared = editor.api("/api/bindings")["bindings"]
    expect(page.locator(".sheet dd")).to_have_count(len(declared))
    text = sheet.inner_text()
    for binding in declared:
        assert binding["title"] in text, binding["id"]
    editor.press("Escape")
    expect(sheet).to_have_count(0)


def test_arrow_keys_walk_the_diagram_and_enter_opens_the_inspector(
    open_editor: OpenEditor,
) -> None:
    """Tab in, arrow around, Enter to look: the whole diagram without a pointer."""
    editor = open_editor()
    page = editor.page

    editor.press("Alt+3")
    start = editor.focus_ring()
    assert start, "focusing the canvas puts the ring on an element"

    # Somewhere on this diagram there is a neighbour in one of the four
    # directions; which one is Graphviz's business, not this test's.
    for chord in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"):
        editor.press(chord)
        if editor.focus_ring() != start:
            break
    else:  # pragma: no cover - a one-element diagram would not be a diagram
        pytest.fail("no arrow key moved the focus ring")

    editor.press("Enter")
    expect(page.locator("#info")).to_be_visible()
    assert editor.focus_label().split(",")[0] in page.locator("#info").inner_text()


def test_the_focus_ring_is_not_the_selection_ring(open_editor: OpenEditor) -> None:
    """Two states, two appearances. A tool that draws them alike cannot be driven.

    Asserted on the computed stroke, because "they are different classes" is not
    the claim -- the claim is that they *look* different.
    """
    editor = open_editor()
    page = editor.page

    # Enter selects what is focused, so move on afterwards: the two rings then
    # sit on two elements, which is the state that has to be readable.
    editor.press("Alt+3", "Enter")
    for chord in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"):
        editor.press(chord)
        if page.locator("#viewport g.focused:not(.selected)").count():
            break
    else:  # pragma: no cover - a one-element diagram would not be a diagram
        pytest.fail("no arrow key moved the focus ring off the selection")
    expect(page.locator("#viewport g.selected")).to_have_count(1)

    def stroke(selector: str) -> tuple[str, str]:
        drawn = page.evaluate(
            "(sel) => { const s = getComputedStyle("
            "document.querySelector(sel + ' > *:not(text)'));"
            " return [s.stroke, s.strokeDasharray]; }",
            selector,
        )
        return str(drawn[0]), str(drawn[1])

    focused = stroke("#viewport g.focused")
    selected = stroke("#viewport g.selected")
    assert focused[0] != selected[0], f"focus and selection are the same colour: {focused[0]}"
    assert selected[1] not in ("", "none"), "the selection ring is dashed"
    assert focused[1] in ("", "none"), "the focus ring is solid, so the two differ unlit too"


def test_a_keyboard_only_session_creates_connects_and_undoes(open_editor: OpenEditor) -> None:
    """The whole claim of this task, end to end, with the mouse unplugged.

    Not one mouse event is dispatched: every step below is a keystroke, and the
    outcome is asserted on the *files*, because the point of this editor is that
    the picture and the text are one document. Two gestures, two undos, and the
    tree ends where it started.
    """
    editor = open_editor(writable=True, extra={"hosts/pc-spare.yaml": SPARE_HOST})
    page = editor.page
    assert editor.session is not None
    before = editor.session.revision

    # -- create ---------------------------------------------------------
    editor.press("Alt+3", "n")
    expect(page.locator(".prompt")).to_be_visible()
    editor.press("Tab")  # kind stays 'switch'; on to the name
    page.keyboard.type("sw-kb")
    editor.press("Enter")

    assert editor.settles(
        lambda: (editor.root / "sw-kb.yaml").exists(), timeout=TIMEOUT_MS / 1000
    ), "the create gesture has to reach a file"
    created = editor.read("sw-kb.yaml")
    assert "kind: switch" in created and "name: sw-kb" in created
    assert "name: eth0" in created, "the new device gets the port the prompt offered"

    # -- connect --------------------------------------------------------
    editor.press("c")
    expect(page.locator(".prompt")).to_be_visible()
    for value in ("sw-kb", "eth0", "hosts/pc-spare", "eth0"):
        page.keyboard.press("Control+a")
        page.keyboard.type(value)
        page.keyboard.press("Tab")
    editor.press("Enter")

    # The endpoint, not merely "a cable": this tree already has five of those,
    # and waiting for one to exist would be a condition that was true before the
    # gesture ran.
    assert editor.settles(lambda: "sw-kb:eth0" in _all_yaml(editor), timeout=TIMEOUT_MS / 1000), (
        "the connect gesture has to become a cable somebody can read"
    )
    cabled = _all_yaml(editor)
    assert "kind: cable" in cabled and "pc-spare:eth0" in cabled

    # -- undo, twice ----------------------------------------------------
    expect(page.locator("#undo")).to_be_enabled()
    editor.press("Control+z")
    assert editor.settles(
        lambda: "sw-kb:eth0" not in _all_yaml(editor), timeout=TIMEOUT_MS / 1000
    ), "the first undo puts the cable back"
    editor.press("Control+z")
    assert editor.settles(
        lambda: not (editor.root / "sw-kb.yaml").exists(), timeout=TIMEOUT_MS / 1000
    ), "the second undo puts the device back"

    assert editor.session.revision != before
    assert editor.api("/api/state")["undo"] == 0, "both gestures are off the stack"


def _all_yaml(editor: Editor) -> str:
    """Every document in the tree, concatenated. What was actually written.

    Polled by ``settles`` while the server is writing, so a file named by the
    walk may be gone by the time it is read — an undo that removes the document
    a gesture created does exactly that. A vanished file is not an error here;
    it is the change being waited for.
    """
    return "\n".join(_read_if_there(path) for path in sorted(editor.root.rglob("*.yaml")))


def _read_if_there(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def test_a_letter_gesture_does_not_fire_while_typing_yaml(open_editor: OpenEditor) -> None:
    """`n` creates a device on the canvas and types an `n` in the editor.

    The one rule that makes single-letter gestures safe on a page with a text
    pane in it, and the one that would be discovered the hard way.
    """
    editor = open_editor(writable=True)
    page = editor.page
    page.locator('#file-list .file[data-path="switches/sw-home.yaml"]').click()
    expect(page.locator("#editor-title")).to_have_text("switches/sw-home.yaml")

    page.locator("#source").focus()
    page.keyboard.type("n")

    expect(page.locator(".prompt")).to_have_count(0)
    assert "n" in editor.selection() or page.locator("#editor-state").is_visible()


def test_the_scratchpad_offers_the_same_commands_and_refuses_the_write(
    open_editor: OpenEditor,
) -> None:
    """One command list, two faces. A scratchpad has no tree, not fewer commands."""
    editor = open_editor(source=TWO_HOSTS)
    page = editor.page

    editor.press("Control+k")
    page.keyboard.type("Open file")
    row = page.locator(".palette-item").first
    expect(row).to_contain_text("Open file")
    expect(row).to_contain_text("open a folder")
    editor.press("Escape")

    # And the view commands, which need nothing, still work here.
    editor.press("]")
    expect(page.locator("#layer")).to_have_value("l1")


def test_the_scratchpad_has_the_context_menu_too(open_editor: OpenEditor) -> None:
    """The same bargain as the palette, at the other door.

    A scratchpad has no tree, so `New` cannot write one — and says so, rather
    than the menu being a shorter list here than it is over a folder. Somebody
    who learns this interface on a paste has learnt the same interface.
    """
    editor = open_editor(source=TWO_HOSTS)

    open_menu_on_canvas(editor)

    create = menu_row(editor, "element.create")
    expect(create).to_have_class(re.compile("unavailable"))
    expect(create.locator(".menu-why")).to_contain_text("open a folder")
    expect(menu_row(editor, "view.fit")).not_to_have_class(re.compile("unavailable"))


def test_the_links_of_an_element_are_a_cycle_of_their_own(open_editor: OpenEditor) -> None:
    """A cable is an element too, so there has to be a way to put focus on one.

    The cycle belongs to the *node* it started from, which is the part that is
    easy to get wrong: deriving the anchor from each link in turn hands it to
    whichever end of that link the record happens to list first, and the cycle
    walks off across the diagram instead of round one device.
    """
    editor = open_editor(writable=True)
    page = editor.page

    editor.press("Alt+3")
    anchor = editor.focus_label()
    assert "linked to" in anchor, "this test needs a starting point that has links"

    editor.press("l")
    first_link = editor.focus_label()
    assert first_link != anchor
    editor.press("l")
    second = editor.focus_label()
    editor.press("l")
    assert editor.focus_label() == first_link, (
        f"the cycle left its anchor: {first_link!r} -> {second!r} -> {editor.focus_label()!r}"
    )

    # And a focused link deletes as a disconnect, with the cable named for you.
    editor.press("Delete")
    expect(page.locator(".prompt")).to_be_visible()
    assert page.locator(".prompt input").first.input_value(), "the cable is pre-filled"
    editor.press("Enter")
    expect(page.locator("#toast")).to_contain_text("disconnected")


# --------------------------------------------------------------------------- #
# The guided tour
# --------------------------------------------------------------------------- #


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, by relative path. The evidence for "untouched".

    Two things are deliberately tolerated, and both are the same thing: this is
    read *while the server is writing*, because a test polls it to find out when
    a write has landed. :func:`netgraph.fsio.write_bytes_atomically` writes a
    hidden sibling and renames it, so a walk can catch that sibling in the act —
    see it in the listing, and then find it gone by the time it is read. It is
    not part of the tree either way, so it is skipped by name, and anything else
    that vanishes under the walk is left out rather than raising.
    """
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.name.endswith(".netgraph.tmp"):
            continue
        try:
            if path.is_file():
                found[path.relative_to(root).as_posix()] = path.read_bytes()
        except OSError:
            continue
    return found


def _documents(root: Path) -> dict[str, bytes]:
    """The same, narrowed to what the loader reads — and therefore what is copied.

    ``netgraph.web.tour`` copies the inventory, not the folder: a README beside
    it is not part of the tree and is deliberately left behind.
    """
    return {name: text for name, text in _tree(root).items() if name.endswith((".yaml", ".yml"))}


def _take_the_tour(editor: Editor) -> None:
    """Start the tour from the palette and wait for the page to come back on it.

    Starting it reloads the page — the token goes in ``sessionStorage`` and
    ``tour.js`` points the whole boot at the scratch — so "the tour started" is
    the card being on screen after a navigation, not the command returning.
    """
    editor.press("Control+k")
    editor.page.keyboard.type("guided tour")
    expect(editor.page.locator(".palette-item").first).to_contain_text("guided tour")
    editor.press("Enter")
    expect(editor.page.locator("#tour")).to_be_visible(timeout=TIMEOUT_MS)
    expect(editor.page.locator("#viewport svg")).to_be_visible(timeout=TIMEOUT_MS)


def test_the_first_run_offers_the_tour_and_takes_no_for_an_answer(
    open_editor: OpenEditor,
) -> None:
    """The one thing a first-time visitor is shown without asking for it.

    And it is shown once. "No thanks" has to be remembered across reloads, or
    the page nags — which is the reason most first-run experiences get switched
    off before they have taught anybody anything.
    """
    editor = open_editor(writable=True, first_run=True)
    page = editor.page

    invitation = page.locator("#tour-invite")
    expect(invitation).to_be_visible()
    expect(invitation).to_contain_text("throwaway copy")
    # It is a dialog, it is labelled, and the keyboard is already in it.
    expect(invitation).to_have_attribute("role", "dialog")
    assert page.evaluate("() => document.activeElement.id") == "tour-take"

    editor.press("Escape")
    expect(invitation).to_have_count(0)
    assert page.evaluate("() => localStorage.getItem('netgraph.tour.seen')") == "yes"

    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#viewport svg")).to_be_visible()
    expect(page.locator("#tour-invite")).to_have_count(0)


def test_the_tour_creates_connects_moves_diffs_and_undoes_a_copy(
    open_editor: OpenEditor,
) -> None:
    """The whole sixty seconds, asserted step by step — and the files it wrote.

    This is the test the tour exists for. Each card's claim is checked against
    the thing it claims about: the device appears in the diagram, the cable
    appears as a link, the document moves to a file that did not exist, the
    drawer holds three hunks of YAML, and the undo puts the scratch back byte
    for byte. Throughout, the inventory the session was opened on is compared
    against a snapshot taken before the tour started — because "a real
    inventory is never touched" is the promise that makes the rest of it safe,
    and it is the one promise a screenshot cannot show.
    """
    editor = open_editor(writable=True)
    page = editor.page
    before = _tree(editor.root)

    _take_the_tour(editor)
    scratch = Path(page.locator("#files-root").inner_text())
    assert scratch != editor.root, "the tour is editing the tree it was supposed to copy"
    assert scratch.is_dir()
    assert _documents(scratch) == _documents(editor.root), "the copy is not a copy"
    expect(page.locator("#tour-title")).to_have_text("A sixty-second tour")
    expect(page.locator("#tour-progress")).to_have_text("Step 1 of 7")
    expect(page.locator("#tour-safe")).to_contain_text(str(editor.root))
    # The real session is read-only or not, but either way it is not the one
    # being written: the scratch always is.
    expect(page.locator("#files-mode")).to_have_text("read-write")

    # 2. Create. A shape in the diagram, and a document on disk to declare it.
    editor.press("Enter")
    expect(page.locator("#tour-title")).to_have_text("Create a device")
    editor.press("Enter")
    expect(page.locator("#tour-outcome")).to_contain_text("created sw-tour")
    expect(page.locator("#viewport")).to_contain_text("sw-tour")
    declared = [name for name in _documents(scratch) if name not in before]
    assert len(declared) == 1, f"one new file, not {declared}"
    assert "sw-tour" in (scratch / declared[0]).read_text(encoding="utf-8")

    # 3. Connect. A cable document, and therefore an edge.
    expect(page.locator("#tour-title")).to_contain_text("Cable it to")
    links = page.locator("#viewport g.edge").count()
    editor.press("Enter")
    expect(page.locator("#tour-outcome")).to_contain_text("cabled sw-tour")
    expect(page.locator("#viewport g.edge")).to_have_count(links + 1)

    # 4. Move. The same element, a different file — the point of the whole tour.
    expect(page.locator("#tour-title")).to_have_text("Move its document")
    editor.press("Enter")
    expect(page.locator("#tour-outcome")).to_contain_text("tour/sw-tour.yaml")
    expect(page.locator('#file-list .file[data-path="tour/sw-tour.yaml"]')).to_be_visible()
    assert "sw-tour" in (scratch / "tour" / "sw-tour.yaml").read_text(encoding="utf-8")
    assert not (scratch / declared[0]).exists() or "sw-tour" not in (
        scratch / declared[0]
    ).read_text(encoding="utf-8")
    expect(page.locator("#viewport")).to_contain_text("sw-tour"), "moving a file moved the element"

    # 5. The diff. Three gestures, each with the YAML it wrote.
    expect(page.locator("#tour-title")).to_have_text("The YAML that changed")
    editor.press("Enter")
    expect(page.locator("#changes")).to_be_visible()
    expect(page.locator("#changes-list .change")).to_have_count(3)
    expect(page.locator("#changes-list")).to_contain_text("+  name: sw-tour")

    # 6. Undo. All three, and the copy is what it was.
    expect(page.locator("#tour-title")).to_have_text("Undo the lot")
    editor.press("Enter")
    expect(page.locator("#tour-outcome")).to_contain_text("files are back")
    expect(page.locator("#tour-title")).to_have_text("That is the whole idea")
    expect(page.locator("#viewport")).not_to_contain_text("sw-tour")
    assert editor.settles(lambda: _documents(scratch) == _documents(editor.root)), (
        "three undos did not put the scratch copy back"
    )

    # Finishing deletes the copy and puts the page back on the inventory.
    editor.press("Enter")
    expect(page.locator("#files-root")).to_have_text(str(editor.root), timeout=TIMEOUT_MS)
    expect(page.locator("#tour")).to_have_count(0)
    assert editor.settles(lambda: not scratch.exists()), "the scratch copy was left behind"
    assert len(editor.server.tours or []) == 0

    # And the whole of it: not one byte of the inventory was written.
    assert _tree(editor.root) == before


def test_the_tour_is_skippable_with_one_key(open_editor: OpenEditor) -> None:
    """Escape, at any point, and the copy goes with it."""
    editor = open_editor(writable=True)
    page = editor.page
    before = _tree(editor.root)

    _take_the_tour(editor)
    scratch = Path(page.locator("#files-root").inner_text())
    editor.press("Enter")  # off the welcome card
    editor.press("Enter")  # and through the first step that writes
    expect(page.locator("#tour-outcome")).to_contain_text("created sw-tour")

    editor.press("Escape")

    expect(page.locator("#files-root")).to_have_text(str(editor.root), timeout=TIMEOUT_MS)
    expect(page.locator("#tour")).to_have_count(0)
    expect(page.locator("#viewport")).not_to_contain_text("sw-tour")
    assert editor.settles(lambda: not scratch.exists())
    assert _tree(editor.root) == before


def test_a_tour_whose_copy_has_gone_does_not_resume_on_the_inventory(
    open_editor: OpenEditor,
) -> None:
    """The one way this feature could do the exact harm it exists to prevent.

    A token the server no longer has — it restarted, or the copy expired — is
    answered from the *tree* rather than refused, deliberately, so that a
    reloaded tab gets a working page instead of a dead one. A tour that resumed
    without checking which session answered would then run its create, its
    connect and its move against the inventory. So the page checks, and this is
    the test that it does: the copy is deleted underneath a running tour, and
    the reload has to come back on the inventory with no tour on it.
    """
    editor = open_editor(writable=True)
    page = editor.page
    before = _tree(editor.root)

    _take_the_tour(editor)
    scratch = Path(page.locator("#files-root").inner_text())

    # Exactly what a server restart looks like from the browser's side.
    assert editor.server.tours is not None
    editor.server.tours.close_all()
    assert not scratch.exists()

    page.reload(wait_until="domcontentloaded")

    expect(page.locator("#viewport svg")).to_be_visible(timeout=TIMEOUT_MS)
    expect(page.locator("#files-root")).to_have_text(str(editor.root))
    expect(page.locator("#tour")).to_have_count(0)
    assert _tree(editor.root) == before


def test_a_read_only_session_can_still_take_the_tour(open_editor: OpenEditor) -> None:
    """The session most likely to be somebody's first is the one that cannot write.

    ``netgraph web DIR`` without ``--write`` refuses every mutating route, and
    the tour writes — to files of its own. So it is offered here, it works here,
    and the refusal the palette shows for every *other* edit command is still
    the refusal for those.
    """
    editor = open_editor(writable=False)
    page = editor.page
    before = _tree(editor.root)

    editor.press("Control+k")
    page.keyboard.type("guided tour")
    row = page.locator(".palette-item").first
    expect(row).to_contain_text("guided tour")
    expect(row).not_to_have_class(re.compile(r"unavailable"))
    editor.press("Escape")

    _take_the_tour(editor)
    expect(page.locator("#files-mode")).to_have_text("read-write")
    scratch = Path(page.locator("#files-root").inner_text())
    editor.press("Enter")
    editor.press("Enter")
    expect(page.locator("#tour-outcome")).to_contain_text("created sw-tour")
    assert _documents(scratch) != _documents(editor.root), "the tour wrote nothing at all"

    editor.press("Escape")
    expect(page.locator("#files-mode")).to_have_text("read-only", timeout=TIMEOUT_MS)
    assert _tree(editor.root) == before


@pytest.mark.skipif(not HAVE_AXE, reason=NO_AXE)
def test_the_tour_has_no_accessibility_violations(open_editor: OpenEditor) -> None:
    """The two panels a first-time visitor reads first, held to the same standard.

    They are also the only two things on this page that appear without being
    asked for, so a contrast failure or an unlabelled dialog here is met by
    somebody who has not yet decided whether the tool is worth their afternoon.
    """
    editor = open_editor(writable=True, first_run=True)
    page = editor.page

    expect(page.locator("#tour-invite")).to_be_visible()
    invitation = _violations(editor)
    assert not invitation, "the first-run invitation:\n" + _explain(invitation)

    editor.press("Enter")
    expect(page.locator("#tour")).to_be_visible(timeout=TIMEOUT_MS)
    expect(page.locator("#viewport svg")).to_be_visible(timeout=TIMEOUT_MS)
    card = _violations(editor)
    assert not card, "the guided tour's card:\n" + _explain(card)

    editor.press("Escape")
    expect(page.locator("#tour")).to_have_count(0, timeout=TIMEOUT_MS)


# --------------------------------------------------------------------------- #
# Multi-select, bulk edit and alignment
# --------------------------------------------------------------------------- #
#
# The half of a diagram editor that only exists once more than one thing can be
# picked: a rubber band, a bulk delete that asks once, and the alignment
# commands that mean nothing about a single shape. Every one of them is a
# gesture, so every one of them can only be asserted here.


def selected(editor: Editor) -> list[str]:
    """What the page says is selected, read out of select.js itself."""
    return list(editor.page.evaluate("() => window.netgraphSelect.addresses()"))


def halos(editor: Editor) -> int:
    """How many selection rings are drawn. Not the same as the count above: a
    ring is only drawn for an element that is on screen."""
    return int(editor.page.locator("#viewport svg .ng-halo").count())


def sweep(editor: Editor, *, over: Sequence[str]) -> None:
    """Drag a rubber band across the paper, enclosing these shapes.

    Started from a corner of the canvas that no shape occupies, because a press
    that lands on a shape pans instead — which is the split app.js makes and the
    thing this is testing the other half of.
    """
    boxes = []
    for address in over:
        box = editor.shape(address).bounding_box(timeout=TIMEOUT_MS)
        assert box is not None, f"{address} is not on screen"
        boxes.append(box)
    frame = editor.page.locator("#canvas").bounding_box()
    assert frame is not None
    # Clamped to the canvas: a band that started a few pixels further left would
    # start in the YAML pane, and a press there is a press in a text field.
    left = max(min(box["x"] for box in boxes) - 12, frame["x"] + 2)
    top = max(min(box["y"] for box in boxes) - 12, frame["y"] + 2)
    right = min(max(box["x"] + box["width"] for box in boxes) + 12, frame["x"] + frame["width"] - 2)
    bottom = min(
        max(box["y"] + box["height"] for box in boxes) + 12, frame["y"] + frame["height"] - 2
    )
    mouse = editor.page.mouse
    mouse.move(left, top)
    mouse.down()
    mouse.move((left + right) / 2, (top + bottom) / 2)
    mouse.move(right, bottom)
    mouse.up()


def test_a_rubber_band_selects_what_it_encloses(open_editor: OpenEditor) -> None:
    """Drag on the paper and everything inside is picked, halo and all."""
    editor = arranged(open_editor)
    assert selected(editor) == []

    sweep(editor, over=["hosts/srv-nas", "routers/rtr-home"])

    picked = selected(editor)
    assert "hosts/srv-nas" in picked and "routers/rtr-home" in picked
    assert "hosts/phone" not in picked, "the band caught something it did not enclose"
    assert halos(editor) >= 2, "a selection has to be visible"
    # And it is said out loud, which is the only form of it a screen reader has.
    expect(editor.page.locator("#outline-summary")).to_contain_text(f"{len(picked)} selected")


def test_shift_clicking_adds_and_takes_away(open_editor: OpenEditor) -> None:
    editor = arranged(open_editor)

    press_on(editor, editor.shape("switches/sw-home"))
    assert selected(editor) == ["switches/sw-home"]

    editor.page.keyboard.down("Shift")
    press_on(editor, editor.shape("hosts/pc-desk"))
    editor.page.keyboard.up("Shift")
    assert sorted(selected(editor)) == ["hosts/pc-desk", "switches/sw-home"]

    # The same chord again on the same shape is how one is taken back out.
    editor.page.keyboard.down("Shift")
    press_on(editor, editor.shape("hosts/pc-desk"))
    editor.page.keyboard.up("Shift")
    assert selected(editor) == ["switches/sw-home"]


def test_select_all_and_escape(open_editor: OpenEditor) -> None:
    """Ctrl-A on the canvas takes the view; Escape gives it back."""
    editor = arranged(open_editor)
    editor.page.locator("#canvas").focus()
    editor.press("Control+a")

    picked = selected(editor)
    drawn = editor.api(f"/api/graph?view={ARRANGED_LAYER}")["details"]
    assert len(picked) == len(drawn), "select-all has to take everything drawn"

    editor.press("Escape")
    assert selected(editor) == []
    assert halos(editor) == 0


def test_ctrl_a_in_the_yaml_pane_is_still_the_text(open_editor: OpenEditor) -> None:
    """A canvas binding must not reach into a text field. Ctrl-A least of all."""
    editor = arranged(open_editor)
    editor.page.locator('.doc[data-address="switches/sw-home"]').first.click()
    expect(editor.page.locator("#editor-title")).to_have_text("switches/sw-home.yaml")

    editor.page.locator("#source").focus()
    editor.press("Control+a")

    assert selected(editor) == [], "Ctrl-A in the editor selected the diagram"
    assert editor.selection().startswith("apiVersion:")


def test_shift_arrow_extends_the_selection_along_the_links(open_editor: OpenEditor) -> None:
    """The keyboard's rubber band: the same neighbour search, collecting as it goes."""
    editor = arranged(open_editor)
    editor.page.locator("#canvas").focus()
    editor.press("Home")
    first = editor.focus_ring()
    assert first

    editor.press("Shift+ArrowRight")
    picked = selected(editor)
    assert len(picked) >= 2, "Shift-arrow has to add rather than replace"
    assert editor.focus_ring() != first, "and has to follow where it went"


def test_a_selection_survives_a_re_render(open_editor: OpenEditor) -> None:
    """It is addresses, not DOM nodes: a redraw must not drop it."""
    editor = arranged(open_editor)
    sweep(editor, over=["hosts/srv-nas", "routers/rtr-home"])
    before = sorted(selected(editor))
    assert len(before) >= 2

    editor.page.locator("#render").click()
    expect(editor.page.locator("#viewport svg")).to_be_visible()
    assert editor.settles(lambda: sorted(selected(editor)) == before, timeout=TIMEOUT_MS / 1000), (
        "the selection did not survive the redraw"
    )
    assert halos(editor) >= 2


def test_deleting_a_selection_asks_once_and_undoes_in_one_step(
    open_editor: OpenEditor,
) -> None:
    """The claim the whole batch layer exists for, asserted on the files."""
    editor = arranged(open_editor)
    assert editor.session is not None
    before = _tree(editor.root)
    depth = editor.api("/api/state")["undo"]

    editor.page.keyboard.down("Shift")
    press_on(editor, editor.shape("hosts/pc-desk"))
    press_on(editor, editor.shape("hosts/srv-nas"))
    editor.page.keyboard.up("Shift")
    assert sorted(selected(editor)) == ["hosts/pc-desk", "hosts/srv-nas"]

    asked: list[str] = []
    editor.page.once("dialog", lambda dialog: (asked.append(dialog.message), dialog.accept()))
    editor.page.locator("#canvas").focus()
    editor.press("Delete")

    assert editor.settles(
        lambda: editor.session is not None and editor.session.revision != 1,
        timeout=TIMEOUT_MS / 1000,
    ), "the bulk delete reached no file"
    assert len(asked) == 1, "a bulk delete must ask once, not once per element"
    # It listed what goes -- and the cables that go with them, which is the part
    # a person cannot work out from the picture.
    assert "pc-desk" in asked[0] and "srv-nas" in asked[0]
    assert "cbl-sw-desk" in asked[0], asked[0]

    after = _tree(editor.root)
    assert "hosts/pc-desk.yaml" not in after
    assert "hosts/srv-nas.yaml" not in after
    # One entry in the stack for the whole gesture, and one Ctrl-Z back.
    assert editor.api("/api/state")["undo"] == depth + 1
    editor.page.locator("#undo").click()
    assert editor.settles(lambda: _tree(editor.root) == before, timeout=TIMEOUT_MS / 1000), (
        "one undo has to put the whole batch back"
    )


def _positions(editor: Editor, view: str = ARRANGED_LAYER) -> dict[str, tuple[float, float]]:
    """Where the server says each node is placed, from the render payload."""
    geometry = editor.api(f"/api/graph?view={view}")["geometry"] or {}
    return {key: (entry["x"], entry["y"]) for key, entry in (geometry.get("nodes") or {}).items()}


def test_aligning_a_selection_writes_one_change(open_editor: OpenEditor) -> None:
    """Align from the palette, and the YAML moves in one reviewable step."""
    editor = arranged(open_editor)
    assert editor.session is not None
    before = _positions(editor)
    depth = editor.api("/api/state")["undo"]
    wanted = ["hosts/pc-desk", "hosts/srv-nas", "routers/rtr-home"]
    assert len({before[address][0] for address in wanted}) == 3, "they start out ragged"
    # Centres rather than edges, so the assertion below is one number and not
    # three: these three nodes have three different widths.

    editor.page.keyboard.down("Shift")
    for address in wanted:
        press_on(editor, editor.shape(address))
    editor.page.keyboard.up("Shift")
    assert sorted(selected(editor)) == sorted(wanted)

    editor.press("Control+k")
    editor.page.locator(".palette-input").fill("Align centres")
    editor.press("Enter")

    assert editor.settles(
        lambda: len({_positions(editor)[address][0] for address in wanted}) == 1,
        timeout=TIMEOUT_MS / 1000,
    ), "aligning centres has to give the three of them one x"

    after = _positions(editor)
    assert after["switches/sw-home"] == before["switches/sw-home"], "an unselected node moved"
    assert "layout.yaml" in editor.api("/api/changes")["entries"][-1]["files"]
    assert editor.api("/api/state")["undo"] == depth + 1
    editor.page.locator("#undo").click()
    assert editor.settles(lambda: _positions(editor) == before, timeout=TIMEOUT_MS / 1000), (
        "one undo has to put a whole alignment back"
    )


def test_right_clicking_a_selection_offers_the_commands_that_need_one(
    open_editor: OpenEditor,
) -> None:
    """A set has its own menu: no rename, and every alignment."""
    editor = arranged(open_editor)
    editor.page.keyboard.down("Shift")
    press_on(editor, editor.shape("hosts/pc-desk"))
    press_on(editor, editor.shape("hosts/srv-nas"))
    editor.page.keyboard.up("Shift")

    press_on(editor, editor.shape("hosts/pc-desk"), button="right")
    expect(menu(editor)).to_be_visible()

    expect(menu(editor).locator(".menu-head")).to_have_text("2 selected elements")
    for command in ("align.left", "distribute.vertical", "geometry.snap", "element.delete"):
        expect(menu_row(editor, command)).to_be_visible()
    # Nothing that can only mean one element: renaming two things is not a thing.
    expect(menu_row(editor, "element.rename")).to_have_count(0)


def test_a_read_only_session_selects_but_will_not_arrange(open_editor: OpenEditor) -> None:
    """Looking at a diagram must never become rearranging it."""
    editor = arranged(open_editor, writable=False)
    sweep(editor, over=["hosts/srv-nas", "routers/rtr-home"])
    assert len(selected(editor)) >= 2, "a read-only session can still select"

    editor.press("Control+k")
    editor.page.locator(".palette-input").fill("Align left")
    row = editor.page.locator(".palette-item").first
    expect(row).to_have_class(re.compile(r"unavailable"))
    editor.press("Escape")


@requires_dot
def test_a_selection_of_a_thousand_devices_stays_workable(open_editor: OpenEditor) -> None:
    """Ctrl-A on a culled diagram: every address held, only the visible ones drawn."""
    editor = crowded(open_editor)
    editor.page.locator("#canvas").focus()
    editor.press("Control+a")

    picked = selected(editor)
    stats = cull_stats(editor)
    assert stats["active"], "this diagram is meant to be big enough to cull"
    assert len(picked) == stats["total"], "an off-screen element is still selectable"
    assert 0 < halos(editor) <= len(picked), "only what is on screen needs a ring"
    expect(editor.page.locator("#outline-summary")).to_contain_text(f"{len(picked)} selected")

    editor.press("Escape")
    assert selected(editor) == []


# --------------------------------------------------------------------------- #
# The style inspector (§22)
# --------------------------------------------------------------------------- #


def expect_eventually(predicate: Callable[[], bool], *, what: str = "") -> None:
    """Wait for something that lands on *disk* rather than in the DOM.

    Playwright's ``expect`` retries assertions about the page; a write reaches
    the tree through a request the page made, so the file is a step behind what
    is on screen and there is no locator to wait on. Same budget, same idea.
    """
    deadline = time.monotonic() + TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(what or "the tree never reached the expected state")


def style_panel(editor: Editor) -> Locator:
    return editor.page.locator("#style")


def style_row(editor: Editor, field: str) -> Locator:
    """The row of the inspector for one style field."""
    return editor.page.locator(f".style-row:has(#style-{field})")


def style_from(editor: Editor, field: str) -> Locator:
    """What the inspector says the field's value came from.

    A locator rather than the text, so every assertion about provenance
    retries: the panel is repainted by the render that *follows* a write, so
    reading it the instant a control changes is a race with the round trip.
    """
    return style_row(editor, field).locator(".style-from")


def set_style(editor: Editor, field: str, value: str) -> None:
    """Type a value into one row, exactly as a person does.

    ``fill()`` dispatches ``change`` itself. Dispatching a second one would
    be a second write against the revision the first has already moved, and
    the server is right to answer that with a conflict.
    """
    editor.page.locator("#style-" + field).fill(value)


def open_style(editor: Editor) -> None:
    """Open the inspector the way a person does, and wait for it to draw."""
    editor.page.locator("#style-toggle").click()
    expect(style_panel(editor)).to_be_visible()


def declared_style(editor: Editor, address: str) -> dict[str, Any]:
    """What the *file* says about one element's appearance, read off disk.

    The whole tree is searched rather than ``<address>.yaml`` opened: several
    documents share a file in this inventory (every cable is in
    ``cables/links.yaml``), and the property under test is that the write landed
    in the document, not that it landed in a file named after it.
    """
    name = address.rpartition("/")[2]
    namespace = address.rpartition("/")[0]
    for path in sorted(editor.root.rglob("*.yaml")):
        prefix = path.parent.relative_to(editor.root).as_posix()
        if prefix == ".":
            prefix = ""
        if prefix != namespace:
            continue
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(document, dict):
                continue
            if (document.get("metadata") or {}).get("name") != name:
                continue
            style = (document.get("spec") or {}).get("style")
            assert style is None or isinstance(style, dict)
            return dict(style or {})
    raise AssertionError(f"no document declares {address}")


def expect_style(editor: Editor, address: str, expected: dict[str, Any]) -> None:
    """Wait for the tree to say ``expected`` about ``address``.

    A write reaches disk through a request the page made, so it lands a moment
    after the control that caused it; see :func:`expect_eventually`.
    """
    expect_eventually(
        lambda: declared_style(editor, address) == expected,
        what=(f"{address} settled at {declared_style(editor, address)!r}, not at {expected!r}"),
    )


def test_the_style_inspector_shows_where_each_value_came_from(
    open_editor: OpenEditor,
) -> None:
    """The point of the panel: not the colour, but which layer chose it."""
    editor = arranged(open_editor)
    press_on(editor, editor.shape("switches/sw-home"))
    open_style(editor)

    expect(editor.page.locator("#style-subject")).to_have_text("switches/sw-home")
    # Nothing has been styled and no theme is in force, so every value on screen
    # is the built-in palette's — which is exactly what the row must say.
    expect(style_from(editor, "fill")).to_have_text("the built-in palette")
    expect(style_from(editor, "shape")).to_have_text("the built-in palette")
    # A field nobody has set anywhere reads as unset rather than as inherited
    # from something: there is no rung below the palette that mentions opacity.
    expect(style_from(editor, "opacity")).to_have_text("not set")
    # And with nothing declared there is nothing to reset.
    expect(style_row(editor, "fill").locator(".style-reset")).to_be_disabled()


def test_setting_a_colour_writes_it_to_the_manifest(open_editor: OpenEditor) -> None:
    """The single-source-of-truth rule, at the level of one colour."""
    editor = arranged(open_editor)
    expect_style(editor, "switches/sw-home", {})

    press_on(editor, editor.shape("switches/sw-home"))
    open_style(editor)
    set_style(editor, "fill", "#123456")

    # The YAML is the record, so that is what is asserted — the picture follows
    # from it and not the other way round.
    expect(editor.page.locator("#style-fill")).to_have_value("#123456")
    expect_style(editor, "switches/sw-home", {"fill": "#123456"})
    # ...and the drawing that comes back is drawn with it.
    expect(
        editor.shape("switches/sw-home").locator("polygon, path, ellipse").first
    ).to_have_attribute("fill", "#123456")
    # The provenance moves with the value: it is the element's now, not the
    # palette's, which is what makes the reset button meaningful.
    expect(style_from(editor, "fill")).to_have_text("this element")
    expect(style_row(editor, "fill").locator(".style-reset")).to_be_enabled()


def test_reset_unsets_the_field_rather_than_writing_the_inherited_value(
    open_editor: OpenEditor,
) -> None:
    """A 'reset to theme' that wrote the theme's colour would pin today's theme."""
    editor = arranged(open_editor)
    press_on(editor, editor.shape("switches/sw-home"))
    open_style(editor)
    set_style(editor, "fill", "#123456")
    expect_style(editor, "switches/sw-home", {"fill": "#123456"})

    style_row(editor, "fill").locator(".style-reset").click()

    expect(editor.page.locator("#style-toggle")).to_have_attribute("aria-expanded", "true")
    # The whole block goes with its last field: an empty `style:` mapping is
    # NG-Z002, so the write path cannot leave one behind.
    expect_style(editor, "switches/sw-home", {})
    expect(style_from(editor, "fill")).to_have_text("the built-in palette")


def test_a_multi_selection_is_one_batch_and_one_undo(open_editor: OpenEditor) -> None:
    """Task 96's rule, applied to appearance: several elements, one changeset."""
    editor = arranged(open_editor)
    sweep(editor, over=["hosts/srv-nas", "routers/rtr-home"])
    picked = [address for address in selected(editor) if not address.startswith("cables/")]
    assert len(picked) >= 2
    # The panel acts on the whole selection, so the band's cables are painted
    # too; the assertions below are about the elements, which are the ones
    # with a document named after them.

    open_style(editor)
    expect(editor.page.locator("#style-subject")).to_contain_text("selected")
    set_style(editor, "fill", "#abcdef")

    for address in picked:
        expect_style(editor, address, {"fill": "#abcdef"})

    # One Ctrl-Z, not one per element. That is the property being asserted.
    editor.page.locator("#canvas").focus()
    editor.press("Control+z")
    for address in picked:
        expect_eventually(
            lambda a=address: declared_style(editor, a) == {},
            what=f"{address} still carries a style after one undo",
        )


def test_a_theme_is_shown_as_the_theme_and_can_be_overridden(
    open_editor: OpenEditor, tmp_path: Path
) -> None:
    """The ladder, end to end: a theme paints, and the element wins over it."""
    editor = open_editor(writable=True, theme="blueprint")
    press_on(editor, editor.shape("switches/sw-home"))
    open_style(editor)

    # blueprint's rule 2 is `kind: [switch]`, which is what has to be named.
    expect(style_from(editor, "fill")).to_have_text(re.compile(r"^rule \d+ of theme blueprint$"))

    set_style(editor, "fill", "#00ff00")
    expect(style_from(editor, "fill")).to_have_text("this element")
    # The fields the element did *not* set still come from the theme: a style is
    # merged field by field, not adopted whole.
    expect(style_from(editor, "stroke")).to_contain_text("blueprint")


def test_a_read_only_session_shows_the_style_but_will_not_change_it(
    open_editor: OpenEditor,
) -> None:
    """Reading a diagram must never become repainting it."""
    editor = arranged(open_editor, writable=False)
    press_on(editor, editor.shape("switches/sw-home"))
    open_style(editor)

    expect(style_row(editor, "fill").locator(".style-input")).to_be_disabled()
    expect(style_row(editor, "fill").locator(".style-reset")).to_be_disabled()


def test_a_link_is_offered_no_shape_and_no_icon(open_editor: OpenEditor) -> None:
    """A cable is a line. Offering it a shape would validate and draw nothing."""
    editor = arranged(open_editor)
    editor.page.keyboard.down("Shift")
    press_on(editor, band(editor))
    editor.page.keyboard.up("Shift")
    assert selected(editor) == [A_CABLE]
    open_style(editor)

    expect(style_row(editor, "stroke")).to_have_count(1)
    expect(style_row(editor, "dash")).to_have_count(1)
    expect(style_row(editor, "shape")).to_have_count(0)
    expect(style_row(editor, "icon")).to_have_count(0)

    set_style(editor, "stroke", "#ff0000")
    expect_style(editor, A_CABLE, {"stroke": "#ff0000"})


# --------------------------------------------------------------------------- #
# The clipboard
# --------------------------------------------------------------------------- #
#
# Ctrl-C, Ctrl-X, Ctrl-V, Ctrl-D. On this canvas they do not move shapes around,
# they write documents — so every claim below is asserted on the *files*, and
# the undo that takes them back is asserted on the bytes.
#
# The system clipboard is deliberately not required. Headless Chromium refuses
# `navigator.clipboard` without a permission grant, which is exactly the
# situation clipboard.js's in-page fallback exists for; a test that granted the
# permission would be testing a case some real users never reach.


def fragment(editor: Editor) -> dict[str, Any] | None:
    """What this page is holding on its clipboard, read out of clipboard.js."""
    held = editor.page.evaluate("() => window.netgraphClipboard.held()")
    assert held is None or isinstance(held, dict)
    return held


def copied_names(editor: Editor) -> list[str]:
    """The addresses on the clipboard, in the order the server put them there."""
    held = fragment(editor)
    return [] if held is None else [str(entry["address"]) for entry in held["documents"]]


def pick(editor: Editor, *addresses: str) -> None:
    """Shift-click a set of shapes and leave the canvas focused."""
    editor.page.keyboard.down("Shift")
    for address in addresses:
        press_on(editor, editor.shape(address))
    editor.page.keyboard.up("Shift")
    editor.page.locator("#canvas").focus()
    assert sorted(selected(editor)) == sorted(addresses)


def test_copy_paste_and_undo_writes_documents_and_takes_them_back(
    open_editor: OpenEditor,
) -> None:
    """The whole gesture, on the files: Ctrl-C, Ctrl-V, and one Ctrl-Z back."""
    editor = arranged(open_editor)
    assert editor.session is not None
    before = _tree(editor.root)
    depth = editor.api("/api/state")["undo"]

    pick(editor, "switches/sw-home", "hosts/pc-desk")
    editor.press("Control+c")

    assert editor.settles(lambda: fragment(editor) is not None, timeout=TIMEOUT_MS / 1000), (
        "Ctrl-C put nothing on the clipboard"
    )
    held = fragment(editor)
    assert held is not None
    assert held["format"] == "netgraph.dev/clipboard/v1"
    assert sorted(copied_names(editor)) == ["hosts/pc-desk", "switches/sw-home"]
    assert _tree(editor.root) == before, "a copy must write nothing"

    editor.press("Control+v")
    assert editor.settles(
        lambda: "switches/sw-home-copy.yaml" in _tree(editor.root), timeout=TIMEOUT_MS / 1000
    ), "Ctrl-V wrote no document"
    after = _tree(editor.root)
    assert "hosts/pc-desk-copy.yaml" in after
    # The originals are untouched: a paste adds, it does not rewrite.
    assert after["switches/sw-home.yaml"] == before["switches/sw-home.yaml"]

    # One entry in the stack for the whole paste, and one Ctrl-Z back.
    assert editor.api("/api/state")["undo"] == depth + 1
    editor.page.locator("#undo").click()
    assert editor.settles(lambda: _tree(editor.root) == before, timeout=TIMEOUT_MS / 1000), (
        "one undo has to put the whole paste back"
    )


def test_a_pasted_cable_joins_the_copies(open_editor: OpenEditor) -> None:
    """Both ends selected means the cable comes too — rewired, not duplicated."""
    editor = arranged(open_editor)
    assert editor.session is not None

    editor.page.keyboard.down("Shift")
    press_on(editor, editor.shape("switches/sw-home"))
    press_on(editor, editor.shape("hosts/pc-desk"))
    press_on(editor, band(editor))
    editor.page.keyboard.up("Shift")
    editor.page.locator("#canvas").focus()
    assert A_CABLE in selected(editor)

    editor.press("Control+c")
    assert editor.settles(lambda: len(copied_names(editor)) == 3, timeout=TIMEOUT_MS / 1000)
    editor.press("Control+v")
    assert editor.settles(
        lambda: "cbl-sw-desk-copy" in editor.read("cables/links.yaml"), timeout=TIMEOUT_MS / 1000
    ), "the cable between the two copied devices did not come with them"

    links = editor.read("cables/links.yaml")
    assert "sw-home-copy:port2" in links
    assert "pc-desk-copy:eno1" in links


def test_ctrl_d_duplicates_without_touching_the_clipboard(open_editor: OpenEditor) -> None:
    """Copy and paste in one keystroke: the system clipboard is not involved."""
    editor = arranged(open_editor)
    assert editor.session is not None
    assert fragment(editor) is None

    pick(editor, "switches/sw-home")
    editor.press("Control+d")

    assert editor.settles(
        lambda: "switches/sw-home-copy.yaml" in _tree(editor.root), timeout=TIMEOUT_MS / 1000
    ), "Ctrl-D wrote no document"
    assert fragment(editor) is None, "a duplicate must not overwrite what you copied earlier"


def test_ctrl_x_cuts_and_one_undo_puts_it_back(open_editor: OpenEditor) -> None:
    """A cut is a delete that also fills the clipboard, and it asks first."""
    editor = arranged(open_editor)
    assert editor.session is not None
    before = _tree(editor.root)

    pick(editor, "hosts/srv-nas")
    asked: list[str] = []
    editor.page.once("dialog", lambda dialog: (asked.append(dialog.message), dialog.accept()))
    editor.press("Control+x")

    assert editor.settles(
        lambda: "hosts/srv-nas.yaml" not in _tree(editor.root), timeout=TIMEOUT_MS / 1000
    ), "the cut removed nothing"
    assert len(asked) == 1, "a cut removes documents, so it has to ask"
    # The document goes before the answer comes back, so what it took has to be
    # waited for rather than read the instant the file disappears.
    assert editor.settles(
        lambda: copied_names(editor) == ["hosts/srv-nas"], timeout=TIMEOUT_MS / 1000
    ), "what a cut removed has to be on the clipboard"

    editor.page.locator("#undo").click()
    assert editor.settles(lambda: _tree(editor.root) == before, timeout=TIMEOUT_MS / 1000), (
        "one undo has to put a cut back"
    )


def test_a_fragment_pastes_into_a_second_session(open_editor: OpenEditor) -> None:
    """The between-windows case: JSON out of one page, documents into another.

    A second browser context over a second :class:`EditingSession` — its own
    revision, its own undo stack, no memory of the copy. All that crosses is the
    JSON, handed over through `remember` exactly as the system clipboard would
    hand it over when the browser allows one to be read. Pasting into a
    *different inventory* is the same code path and is asserted in
    ``tests/test_clipboard.py``, where no browser is needed to prove it.
    """
    source = arranged(open_editor)
    pick(source, "switches/sw-home")
    source.press("Control+c")
    assert source.settles(lambda: fragment(source) is not None, timeout=TIMEOUT_MS / 1000)
    payload = fragment(source)
    assert payload is not None

    target = open_editor(writable=True)
    assert target.session is not None
    assert target.session is not source.session, "a second session, not a second tab"
    assert fragment(target) is None, "the second page has copied nothing"
    before = _tree(target.root)
    target.page.evaluate("payload => window.netgraphClipboard.remember(payload)", payload)
    target.page.locator("#canvas").focus()
    target.press("Control+v")

    assert target.settles(lambda: _tree(target.root) != before, timeout=TIMEOUT_MS / 1000), (
        "the fragment from the other window wrote nothing"
    )
    # It landed under the name it was copied as, because this tree has one too.
    assert "switches/sw-home-copy.yaml" in _tree(target.root)


def test_ctrl_c_in_the_yaml_pane_is_still_the_text(open_editor: OpenEditor) -> None:
    """A canvas binding must not reach into a text field — Ctrl-C least of all."""
    editor = arranged(open_editor)
    pick(editor, "switches/sw-home")
    editor.page.locator('.doc[data-address="switches/sw-home"]').first.click()
    expect(editor.page.locator("#editor-title")).to_have_text("switches/sw-home.yaml")

    editor.page.locator("#source").focus()
    editor.press("Control+a", "Control+c")

    assert fragment(editor) is None, "Ctrl-C in the editor copied the diagram"


def test_a_read_only_session_will_not_paste_but_will_copy(open_editor: OpenEditor) -> None:
    """Reading a fragment out of a tree is a read; writing one into it is not."""
    editor = arranged(open_editor, writable=False)
    before = _tree(editor.root)

    pick(editor, "switches/sw-home")
    editor.press("Control+c")
    assert editor.settles(lambda: fragment(editor) is not None, timeout=TIMEOUT_MS / 1000), (
        "a read-only session still has documents to copy"
    )

    editor.press("Control+v")
    assert editor.settles(
        lambda: editor.page.locator(".toast").count() > 0, timeout=TIMEOUT_MS / 1000
    )
    assert _tree(editor.root) == before, "a read-only session wrote a pasted document"
