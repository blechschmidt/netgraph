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
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

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
PAGE_QUERY: Final = "view=physical&show_ips=1&show_vlans=1&group_by_namespace=0&strict=0"

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
            if source is None:
                session = EditingSession(root=root, writable=writable)
                if watch:
                    stack.enter_context(TreeWatcher(session, debounce_ms=50))
            server = stack.enter_context(
                WebServer.create(source=source or "", session=session, host="127.0.0.1", port=0)
            )

            console = Console()
            context = stack.enter_context(
                chromium.new_context(viewport={"width": 1400, "height": 900})
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
    box = locator.bounding_box()
    assert box is not None, "the element is not on screen, so it cannot be dragged"
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


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
