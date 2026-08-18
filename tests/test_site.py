"""The published site, checked the way the documentation it publishes is.

``tools/build_site.py`` writes what GitHub Pages serves. Three things about it
are worth a test rather than a look, because all three fail silently:

* **The anchors.** Every ``NV-*`` finding netviz prints carries a help URL
  ending in ``validation-rules.md#<anchor>``, and those anchors are GitHub's,
  derived from the heading text. A site that derived its own would answer every
  one of those links with the top of a very long page. So the builder's
  :func:`~tools.build_site.slug` and ``tests/test_docs.py``'s are asserted to
  agree on every heading in the repository, not merely to look alike.
* **The links.** The build rewrites ``.md`` targets to ``.html`` and sends the
  ones that point into the source tree at GitHub. That rewriting is the part
  that can be wrong, and a broken link on the page that explains the tool is
  worse than a broken link anywhere else.
* **The examples.** The whole reason the site exists is that a stranger can
  click through a real inventory without installing anything. An example that
  stops rendering has to fail the build, not quietly vanish from the index.

The module skips itself when ``markdown-it-py`` is missing, with the command to
install it — the same arrangement ``tests/test_browser.py`` has for Playwright,
and for the same reason: a contributor typing ``pytest`` did not ask to build a
website.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from platform_marks import requires_dot  # isort: skip -- tests/ is on sys.path

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DOCS: Final = REPO_ROOT / "docs"
EXAMPLES: Final = REPO_ROOT / "examples"

HAVE_MARKDOWN: Final = importlib.util.find_spec("markdown_it") is not None

pytestmark = [
    pytest.mark.skipif(
        not HAVE_MARKDOWN,
        reason="markdown-it-py is not installed; uv sync --extra site to build the site",
    ),
]


def _builder() -> ModuleType:
    """``tools/build_site.py``, imported as a module."""
    name = "build_site"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUILD: Final = _builder() if HAVE_MARKDOWN else None


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The whole site, built once, examples and all.

    Module-scoped because the build shells out to Graphviz five times and every
    test below asks about the same output. A test that needed a *different*
    build would be testing the builder's arguments, which is what
    :func:`test_the_build_refuses_an_example_that_will_not_render` does directly.
    """
    output = tmp_path_factory.mktemp("site") / "site"
    BUILD.build(output)
    yield output


# --------------------------------------------------------------------------- #
# The shape of what is published
# --------------------------------------------------------------------------- #


@requires_dot
def test_every_documentation_page_is_published(site: Path) -> None:
    """A Markdown file under ``docs/`` that is not on the site is not documentation."""
    for source in sorted(DOCS.rglob("*.md")):
        relative = source.relative_to(REPO_ROOT).as_posix()
        expected = re.sub(r"README\.md$", "index.md", relative)[: -len(".md")] + ".html"
        assert (site / expected).is_file(), f"{relative} was not published as {expected}"
    for name, destination in BUILD.ROOT_PAGES.items():
        assert (site / destination).is_file(), f"{name} was not published"


@requires_dot
def test_the_landing_page_offers_the_demos_above_the_readme(site: Path) -> None:
    """The first thing on the site is the thing that needs no install."""
    text = (site / "index.html").read_text(encoding="utf-8")
    strip = text.index("Try it without installing anything")
    heading = text.index('<h1 id="netviz">')
    assert strip < heading, "the demo strip is below the README's own title"
    for demo in BUILD.DEMOS:
        assert f'href="demo/{demo.name}.html"' in text, demo.name


@requires_dot
def test_the_site_carries_its_own_stylesheet_and_no_jekyll(site: Path) -> None:
    """Self-contained, and served as written.

    ``.nojekyll`` is not decoration: without it Pages runs Jekyll over the
    upload, which drops every path beginning with an underscore.
    """
    assert (site / "style.css").is_file()
    assert (site / ".nojekyll").exists()
    text = (site / "docs" / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="../style.css">' in text
    assert "http://" not in text.replace("http://www.w3.org", ""), "the site fetches something"


# --------------------------------------------------------------------------- #
# The examples
# --------------------------------------------------------------------------- #


@requires_dot
def test_every_example_inventory_is_published(site: Path) -> None:
    """A new example that nobody added to ``DEMOS`` would never be seen.

    The index is the whole point of the site, so "we forgot to list it" is the
    failure mode worth a test. Both directions: every example is a demo, and
    every demo is an example.
    """
    published = {demo.name for demo in BUILD.DEMOS}
    present = {
        path.name
        for path in sorted(EXAMPLES.iterdir())
        if path.is_dir() and not path.name.startswith((".", "_"))
    }
    assert published == present, (
        "examples/ and tools/build_site.py DEMOS disagree; "
        f"unpublished: {sorted(present - published)}, missing: {sorted(published - present)}"
    )


@requires_dot
def test_every_demo_is_an_interactive_diagram(site: Path) -> None:
    """What is published is ``netviz render -f html``, not a screenshot of it."""
    for demo in BUILD.DEMOS:
        raw = (site / "demo" / f"{demo.name}-diagram.html").read_text(encoding="utf-8")
        assert "<svg" in raw, f"{demo.name} has no diagram"
        # The layer switcher, which is the thing a reader is meant to press.
        for layer in demo.layers:
            assert layer in raw, f"{demo.name} was not drawn at layer {layer}"
        # Self-contained: no stylesheet to fetch, no script to fetch, no font.
        assert '<link rel="stylesheet" href="http' not in raw
        assert 'src="http' not in raw
        assert "Content-Security-Policy" in raw

        # And it is embedded in a page that can be left again, which is what a
        # reader who arrived from a search engine needs.
        framed = (site / "demo" / f"{demo.name}.html").read_text(encoding="utf-8")
        assert f'src="{demo.name}-diagram.html"' in framed
        assert 'href="index.html"' in framed, f"{demo.name} is a dead end"


@requires_dot
def test_the_demo_index_describes_every_one(site: Path) -> None:
    text = (site / "demo" / "index.html").read_text(encoding="utf-8")
    for demo in BUILD.DEMOS:
        assert f'href="{demo.name}.html"' in text
        assert demo.summary.split(".")[0][:40] in text


def test_the_build_refuses_an_example_that_will_not_render(tmp_path: Path) -> None:
    """The gate the workflow relies on, asserted rather than assumed.

    Driven through the same code path the workflow uses, with one entry pointed
    at a directory that is not there — the cheap stand-in for the real failure,
    which is an example that stops loading. Either way the build must raise, so
    that ``.github/workflows/pages.yml`` has nothing to deploy.
    """
    original = BUILD.DEMOS
    BUILD.DEMOS = (*original, BUILD.Demo(name="not-an-example", summary="", layers=("l1",)))
    try:
        with pytest.raises(SystemExit, match="not-an-example"):
            BUILD.build(tmp_path / "site")
    finally:
        BUILD.DEMOS = original


# --------------------------------------------------------------------------- #
# Anchors and links
# --------------------------------------------------------------------------- #


def test_the_anchors_are_the_ones_the_documentation_promises() -> None:
    """The builder's slug and the documentation's are one function, checked.

    Over every heading in the repository rather than over a handful of made-up
    strings: the two implementations only have to disagree about one character
    class for every rule help link to land on the wrong page.
    """
    docs = importlib.import_module("test_docs")
    headings = [
        match.group(2)
        for path in docs.MARKDOWN
        for match in (docs._HEADING_RE.match(line) for line in path.read_text("utf-8").splitlines())
        if match is not None
    ]
    assert len(headings) > 500, "this check is only worth anything over the real headings"
    for heading in headings:
        assert BUILD.slug(heading) == docs.slug(heading), heading


@requires_dot
def test_the_rule_help_anchors_resolve_on_the_site(site: Path) -> None:
    """Where a finding's ``--help-uri`` points, on the published copy.

    ``netviz.rules`` builds those URLs against GitHub, but the same anchors
    have to exist here, because this is where a reader following the docs set
    ends up.
    """
    from netviz.rules import RULES

    page = (site / "docs" / "validation-rules.html").read_text(encoding="utf-8")
    anchors = set(re.findall(r'<h[1-6][^>]*\bid="([^"]+)"', page))
    missing = sorted(rule.anchor for rule in RULES if rule.anchor not in anchors)
    assert not missing, f"the published rule reference has no anchor for: {missing}"


@requires_dot
def test_nothing_published_links_at_nothing(site: Path) -> None:
    """The build already refuses; this says so out loud, and covers the copy step."""
    assert BUILD.check_links(site) == []


@requires_dot
def test_a_link_into_the_source_tree_goes_to_github(site: Path) -> None:
    """The docs link at ``src/``, ``examples/`` and the workflows on purpose.

    Publishing those would mean publishing a second copy of the repository, so
    they are rewritten to point at the repository itself — and that rewriting is
    only correct if it actually happens.
    """
    text = (site / "docs" / "ipam.html").read_text(encoding="utf-8")
    assert f'href="{BUILD.SOURCE_URL}/blob/main/src/netviz/subnets.py"' in text
    # A directory becomes a ``tree`` URL, not a ``blob`` one.
    rendering = (site / "docs" / "rendering.html").read_text(encoding="utf-8")
    assert f'href="{BUILD.SOURCE_URL}/tree/main/examples/overlay"' in rendering
    # And a page that *is* published is still a relative link, not a trip to
    # GitHub: the site has to be navigable as a site.
    assert 'href="../docs/validation-rules.html#' in text, "a published page was sent to GitHub"


# --------------------------------------------------------------------------- #
# The advertised address
# --------------------------------------------------------------------------- #


def test_the_readme_and_the_docs_point_at_the_published_site() -> None:
    """One address, written in three places, checked in one.

    The badge is what a stranger sees first, so it pointing somewhere stale is
    the most expensive kind of stale there is.
    """
    host = BUILD.SITE_URL.rstrip("/")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"{host}/demo/" in readme, "the README does not link the demo site"
    assert readme.index("img.shields.io") < readme.index("Declare your network"), (
        "the badge is not at the top of the README"
    )
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    assert f"{host}/demo/" in index, "docs/README.md has no row for the demo site"
    started = (DOCS / "getting-started.md").read_text(encoding="utf-8")
    assert "## Try it without installing" in started
    assert started.index("## Try it without installing") < started.index("## Installation"), (
        "getting-started.md offers the install before the thing that needs none"
    )
