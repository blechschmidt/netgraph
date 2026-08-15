#!/usr/bin/env python3
"""Build the published site: the documentation, plus every example, playable.

``.github/workflows/pages.yml`` runs this on every push to ``main`` and deploys
what it writes to GitHub Pages. The point of it is the first sentence of the
README's problem statement: until now nothing let a stranger see what netgraph
does without installing Python and Graphviz first.

What it produces
----------------

``index.html``
    The README, with a strip of links into the live examples above the fold.
``docs/…``
    Every Markdown file under ``docs/`` as a page, with the same anchors GitHub
    derives, so a URL somebody has bookmarked or a ``NG-*`` rule's help link
    lands in the same place here as it does on GitHub.
``demo/<example>-diagram.html``
    ``netgraph render -f html`` over each inventory in ``examples/``, several
    layers deep, exactly as a reader would get by running the command — byte for
    byte, with nothing added. The layers, the filters, the tooltips and the
    outline are all live; there is no second front end to keep in step with the
    real one, which is the whole reason this reuses the command instead of
    inventing a viewer.
``demo/<example>.html``
    That file embedded in the site's shell, so a reader who arrives at a diagram
    can get back out of it, and a link to the raw one beside it.
``changelog.html``, ``contributing.html``
    The two root documents worth publishing beside the docs set.

What it guarantees
------------------

**Every example renders, or the build fails.** A demo site that quietly drops
the inventory that stopped rendering is worse than no demo site: it is a
regression nobody sees. :func:`render_examples` raises on the first failure and
the workflow has nothing to deploy.

**Every internal link resolves.** ``tests/test_docs.py`` already proves that for
the Markdown; this rewrites ``.md`` targets to ``.html`` and
:func:`check_links` proves it again for what is written, because the rewriting
is the part that can be wrong.

**It is hermetic.** One third-party dependency (``markdown-it-py``, the
``site`` extra), no CDN, no web font, no analytics, no network access at build
time or at read time. The pages are plain HTML over one stylesheet.

Run it locally with::

    pip install -e '.[site]'
    python tools/build_site.py --output site
    python -m http.server -d site
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DOCS: Final = REPO_ROOT / "docs"
EXAMPLES: Final = REPO_ROOT / "examples"

#: The canonical home of the built site. Written into the README badge and into
#: ``docs/README.md``; ``tests/test_site.py`` checks the three agree.
SITE_URL: Final = "https://blechschmidt.github.io/netgraph/"

#: Root documents that are published beside the docs set, and where each lands.
#: ``README.md`` is the landing page: it is already the shortest correct answer
#: to "what is this", and maintaining a second one would mean maintaining a
#: second one.
ROOT_PAGES: Final[Mapping[str, str]] = {
    "README.md": "index.html",
    "CHANGELOG.md": "changelog.html",
    "CONTRIBUTING.md": "contributing.html",
}

#: Directories copied verbatim: the hero diagrams and the committed report's
#: own SVGs. Relative to the repository root, and landing at the same path.
COPIED: Final[tuple[str, ...]] = (
    "docs/images",
    "docs/example-report/diagrams",
    # An interactive render committed as documentation of the format. It is a
    # page in its own right, so it is published rather than linked at GitHub,
    # which would offer a reader the HTML source of a diagram.
    "docs/home-lab.html",
)


@dataclass(frozen=True)
class Demo:
    """One example inventory, and how it is drawn for the site."""

    #: Directory name under ``examples/``, and the page's own name.
    name: str
    #: What the card says it is.
    summary: str
    #: The layers to draw, in the order the page's switcher offers them. Only
    #: layers this inventory actually has something to say at: a switcher whose
    #: third tab is empty teaches a reader that the third layer is useless.
    layers: tuple[str, ...]


#: Every example, with the layers worth drawing for it. Explicit rather than
#: derived: which layers an inventory has anything to say at is a property of
#: what it was written to demonstrate, and a probe that guessed would put an
#: empty ``overlay`` tab on four of these five.
DEMOS: Final[tuple[Demo, ...]] = (
    Demo(
        name="home-lab",
        summary=(
            "A house: a router, a switch, an access point, three computers, a server "
            "and a USB-to-Ethernet adapter, with VLANs, addresses and one wireless "
            "association. Start here."
        ),
        layers=("l1", "l2", "l3", "identity"),
    ),
    Demo(
        name="quickstart",
        summary="The three devices docs/getting-started.md builds, one file at a time.",
        layers=("l1", "l2", "l3"),
    ),
    Demo(
        name="campus",
        summary=(
            "Several sites, a routed core, BGP and OSPF. The one to open the "
            "namespace collapsing and the routing layer on."
        ),
        layers=("l1", "l2", "l3", "routing"),
    ),
    Demo(
        name="overlay",
        summary="Tunnels, including VXLAN over IPsec — encapsulation drawn as its own layer.",
        layers=("l1", "l3", "overlay"),
    ),
    Demo(
        name="patch-room",
        summary="Patch panels, racks and power feeds: the layers a diagram usually leaves out.",
        layers=("physical", "l1", "rack", "power"),
    ),
)

#: Site navigation. Every page carries it, and it is short on purpose.
NAV: Final[tuple[tuple[str, str], ...]] = (
    ("Home", "index.html"),
    ("Try it", "demo/index.html"),
    ("Documentation", "docs/index.html"),
    ("Commands", "docs/commands/index.html"),
    ("Schema", "docs/schema-reference.html"),
    ("Changelog", "changelog.html"),
)

#: Where the source lives, for the "edit this page" link every page carries.
SOURCE_URL: Final = "https://github.com/blechschmidt/netgraph"

#: Link targets that are already somewhere else, and are left alone. ``#`` is a
#: same-page anchor and ``data:`` is the favicon this shell inlines.
_EXTERNAL: Final = ("http://", "https://", "mailto:", "ftp://", "data:", "//", "#")


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def slug(heading: str) -> str:
    """The anchor GitHub derives from a heading.

    Deliberately the same function as ``tests/test_docs.py``'s, and
    ``tests/test_site.py`` asserts they stay the same: every ``NG-*`` finding's
    help link is ``validation-rules.md#<anchor>``, and a site that derived its
    own anchors would answer those links with the top of the page.
    """
    text = heading.strip().lower()
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def markdown() -> object:
    """The renderer, configured once.

    CommonMark plus the two GitHub extensions the documentation actually uses:
    tables everywhere, and strikethrough in ``docs/follow-ups.md``. Raw HTML is
    left in — ``<sub>`` under the hero diagram, ``<details>`` in several pages —
    because these files are ours and are already checked by the test suite.
    """
    try:
        from markdown_it import MarkdownIt
    except ModuleNotFoundError:  # pragma: no cover - the message is the point
        raise SystemExit(
            "building the site needs markdown-it-py: pip install -e '.[site]'"
        ) from None
    return MarkdownIt("commonmark", {"html": True, "linkify": False}).enable(
        ["table", "strikethrough"]
    )


class Page:
    """One source document and where it lands on the site."""

    def __init__(self, source: Path, destination: str) -> None:
        #: Absolute path of the Markdown file.
        self.source = source
        #: Site-relative path of the HTML file, POSIX, e.g. ``docs/ci.html``.
        self.destination = destination

    @property
    def title(self) -> str:
        """The first ATX heading, or the file name when there is none."""
        for line in self.source.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return self.source.stem

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Page({self.destination})"


def pages() -> list[Page]:
    """Every Markdown file that becomes a page, in a stable order."""
    found = [
        Page(REPO_ROOT / name, destination)
        for name, destination in ROOT_PAGES.items()
        if (REPO_ROOT / name).is_file()
    ]
    for path in sorted(DOCS.rglob("*.md")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        # ``docs/README.md`` is the documentation index, and an index is
        # ``index.html`` wherever it sits — otherwise every link to a directory
        # needs to know the name of the file inside it.
        destination = re.sub(r"README\.md$", "index.md", relative)
        found.append(Page(path, destination[: -len(".md")] + ".html"))
    return found


def link_map(page_list: Sequence[Page]) -> dict[str, str]:
    """Repository path → site path, for every page and every copied asset."""
    mapping = {
        page.source.relative_to(REPO_ROOT).as_posix(): page.destination for page in page_list
    }
    for name in COPIED:
        source = REPO_ROOT / name
        found = [source] if source.is_file() else sorted(source.rglob("*"))
        for path in found:
            if path.is_file():
                relative = path.relative_to(REPO_ROOT).as_posix()
                mapping[relative] = relative
    return mapping


# --------------------------------------------------------------------------- #
# The page shell
# --------------------------------------------------------------------------- #

SHELL: Final = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="data:,">
<link rel="stylesheet" href="{root}style.css">
</head>
<body>
<a class="skip" href="#content">Skip to the content</a>
<header class="site-head">
  <a class="brand" href="{root}index.html">netgraph</a>
  <nav aria-label="Site">{nav}</nav>
  <a class="repo" href="{source}">GitHub</a>
</header>
<main id="content" class="prose {width}">
{body}
</main>
<footer class="site-foot">
  <p><a href="{edit}">Edit this page on GitHub</a> &middot;
     netgraph is MIT-licensed &middot;
     this site is built from {commit} by <code>tools/build_site.py</code></p>
</footer>
</body>
</html>
"""


def shell(
    *,
    title: str,
    body: str,
    destination: str,
    description: str,
    edit: str,
    commit: str,
    wide: bool = False,
) -> str:
    """Wrap a rendered fragment in the site's one template.

    ``wide`` drops the measure a page of prose is held to. Used by the demo
    pages and by nothing else: a column comfortable to read is a peephole to
    look at a network diagram through.
    """
    depth = destination.count("/")
    root = "../" * depth
    nav = "".join(f'<a href="{root}{href}">{html.escape(label)}</a>' for label, href in NAV)
    return SHELL.format(
        title=html.escape(title),
        description=html.escape(description),
        root=root,
        nav=nav,
        body=body,
        source=SOURCE_URL,
        edit=edit,
        commit=html.escape(commit),
        width="wide" if wide else "",
    )


STYLE: Final = """\
/* The published site. One stylesheet, no font to fetch, no script.
 *
 * The colours are the editor's own tokens (netgraph/web/assets/app.css), for
 * the same reason and to the same contrast requirement: every one of them
 * clears 4.5:1 against its own background, in both schemes, and nothing is
 * dimmed with opacity. */

:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --raised: #f6f7f9;
  --fg: #1c2024;
  --muted: #5b6167;
  --edge: #cbd1d7;
  --accent: #0b5ed7;
  --focus: #6b21a8;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a;
    --raised: #1d2126;
    --fg: #e6e9ec;
    --muted: #a7aeb6;
    --edge: #39414a;
    --accent: #7fb0ff;
    --focus: #c4a1ff;
  }
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 16px/1.65 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}

a { color: var(--accent); }
a:focus-visible, button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

.skip {
  position: absolute;
  left: -9999px;
}
.skip:focus { left: .5rem; top: .5rem; background: var(--bg); padding: .4rem .6rem; z-index: 5; }

.site-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .3rem 1rem;
  padding: .7rem 1.2rem;
  border-bottom: 1px solid var(--edge);
  background: var(--raised);
  position: sticky;
  top: 0;
  z-index: 4;
}

.brand { font: 700 18px/1 var(--mono); color: var(--fg); text-decoration: none; }
.site-head nav { display: flex; flex-wrap: wrap; gap: .9rem; }
.site-head nav a { text-decoration: none; font-size: 14px; }
.site-head nav a:hover { text-decoration: underline; }
.site-head .repo { margin-left: auto; font-size: 14px; }

.prose { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1.2rem 4rem; }
/* A diagram is not prose and is not read at a 52rem measure. */
.prose.wide { max-width: 82rem; }
.prose h1 { font-size: 2rem; line-height: 1.2; margin: 1.2rem 0 .6rem; }
.prose h2 { font-size: 1.45rem; margin: 2rem 0 .5rem; padding-bottom: .2rem;
            border-bottom: 1px solid var(--edge); }
.prose h3 { font-size: 1.15rem; margin: 1.6rem 0 .4rem; }
.prose h4, .prose h5, .prose h6 { font-size: 1rem; margin: 1.2rem 0 .3rem; }
.prose img, .prose svg { max-width: 100%; height: auto; }
.prose blockquote {
  margin: 1rem 0;
  padding: .1rem 1rem;
  border-left: 4px solid var(--edge);
  color: var(--muted);
}

.prose code { font: 13.5px/1.5 var(--mono); background: var(--raised); padding: .1em .3em;
              border-radius: 3px; }
.prose pre { background: var(--raised); border: 1px solid var(--edge); border-radius: 6px;
             padding: .8rem 1rem; overflow-x: auto; }
.prose pre code { background: none; padding: 0; font-size: 13px; }

.prose table { border-collapse: collapse; width: 100%; margin: 1rem 0; display: block;
               overflow-x: auto; }
.prose th, .prose td { border: 1px solid var(--edge); padding: .35rem .6rem; text-align: left;
                       vertical-align: top; font-size: 14px; }
.prose th { background: var(--raised); }

.site-foot {
  border-top: 1px solid var(--edge);
  padding: 1rem 1.2rem 2rem;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
}

/* ------------------------------------------------------------ the demos */

.demo-strip {
  margin: 1.5rem 0;
  padding: 1rem 1.2rem;
  border: 1px solid var(--accent);
  border-left: 4px solid var(--accent);
  border-radius: 8px;
  background: var(--raised);
}
.demo-strip h2 { margin: 0 0 .3rem; border: 0; font-size: 1.2rem; }
.demo-strip p { margin: 0 0 .6rem; }
.demo-strip ul { display: flex; flex-wrap: wrap; gap: .5rem; margin: 0; padding: 0;
                 list-style: none; }
.demo-strip a {
  display: inline-block;
  padding: .3rem .7rem;
  border: 1px solid var(--edge);
  border-radius: 999px;
  background: var(--bg);
  text-decoration: none;
  font: 13px/1.6 var(--mono);
}

.cards { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr));
         padding: 0; margin: 1.5rem 0; list-style: none; }
.card { border: 1px solid var(--edge); border-radius: 8px; padding: 1rem; background: var(--raised); }
.card h3 { margin: 0 0 .3rem; font: 600 1.05rem/1.3 var(--mono); }
.card p { margin: 0 0 .7rem; font-size: 14px; color: var(--muted); }
.card .layers { font: 12px/1.6 var(--mono); color: var(--muted); }

/* The render, embedded. Tall enough to be a diagram rather than a peephole,
 * and capped against the viewport so the page around it stays reachable. */
.demo-frame {
  width: 100%;
  height: min(75vh, 44rem);
  border: 1px solid var(--edge);
  border-radius: 8px;
  background: var(--bg);
}
.demo-meta { font-size: 14px; color: var(--muted); }

@media (forced-colors: active) {
  .demo-strip, .card { border-color: CanvasText; }
}
"""


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def rewrite_links(body: str, *, source: str, mapping: Mapping[str, str]) -> tuple[str, list[str]]:
    """Point every relative ``href``/``src`` in ``body`` at its published file.

    ``source`` is the repository path of the document the fragment came from, so
    a relative target is resolved the way a reader on GitHub would resolve it,
    and then looked up. What comes back is the rewritten fragment and the list
    of targets nothing was known about — which the caller turns into a failure,
    because a broken link on a page whose whole job is to explain the tool is
    not a cosmetic defect.
    """
    directory = Path(source).parent
    unresolved: list[str] = []

    def resolve(target: str) -> str | None:
        path, _, anchor = target.partition("#")
        if not path:
            return None  # a same-page anchor; nothing to rewrite
        try:
            key = (directory / path).resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            unresolved.append(target)
            return None
        fragment = f"#{anchor}" if anchor else ""
        landing = mapping.get(key)
        if landing is not None:
            return landing + fragment
        # Not a published page, but part of the repository: the docs link into
        # the source, the examples, the fixtures and the workflows on purpose,
        # and the honest destination for those is where they actually are. A
        # site that published `src/` to make its own links work would be
        # publishing a second copy of the code.
        absolute = REPO_ROOT / key
        if absolute.exists():
            kind = "tree" if absolute.is_dir() else "blob"
            return f"{SOURCE_URL}/{kind}/main/{key}{fragment}"
        unresolved.append(target)
        return None

    def replace(match: re.Match[str]) -> str:
        attribute, quote, target = match.group(1), match.group(2), match.group(3)
        if target.startswith(_EXTERNAL):
            return match.group(0)
        landing = resolve(target)
        if landing is None:
            return match.group(0)
        return f"{attribute}={quote}{landing}{quote}"

    return re.sub(r'\b(href|src)=(["\'])([^"\']+)\2', replace, body), unresolved


def add_anchors(body: str) -> str:
    """Give every heading the ``id`` GitHub would give it."""

    def replace(match: re.Match[str]) -> str:
        level, attributes, text = match.group(1), match.group(2), match.group(3)
        if "id=" in attributes:
            return match.group(0)
        anchor = slug(re.sub(r"<[^>]+>", "", text))
        return f'<h{level}{attributes} id="{html.escape(anchor)}">{text}</h{level}>'

    return re.sub(r"<h([1-6])([^>]*)>(.*?)</h\1>", replace, body, flags=re.DOTALL)


def demo_strip(root: str) -> str:
    """The block bolted above the README on the landing page."""
    links = "".join(
        f'<li><a href="{root}demo/{demo.name}.html">{html.escape(demo.name)}</a></li>'
        for demo in DEMOS
    )
    return (
        '<section class="demo-strip">'
        "<h2>Try it without installing anything</h2>"
        "<p>Every example inventory below is the real <code>netgraph render -f html</code> "
        "output: switch layers, filter by VLAN, hover a node for its interfaces and "
        "addresses. Nothing is fetched and nothing is installed.</p>"
        f"<ul>{links}</ul>"
        "</section>"
    )


def build_pages(output: Path, *, commit: str) -> list[str]:
    """Write every Markdown page. Returns the site-relative paths written."""
    renderer = markdown()
    page_list = pages()
    mapping = link_map(page_list)
    written: list[str] = []
    problems: list[str] = []
    for page in page_list:
        relative = page.source.relative_to(REPO_ROOT).as_posix()
        text = page.source.read_text(encoding="utf-8")
        body = add_anchors(str(renderer.render(text)))  # type: ignore[attr-defined]
        depth = page.destination.count("/")
        body, unresolved = rewrite_links(body, source=relative, mapping=mapping)
        if depth:
            body = _reroot(body, depth)
        problems.extend(f"{relative}: {target}" for target in unresolved)
        if page.destination == "index.html":
            body = demo_strip("") + body
        target = output / page.destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            shell(
                title="netgraph"
                if page.destination == "index.html"
                else f"{page.title} — netgraph",
                body=body,
                destination=page.destination,
                description=_summary(text),
                edit=f"{SOURCE_URL}/blob/main/{relative}",
                commit=commit,
            ),
            encoding="utf-8",
            newline="\n",
        )
        written.append(page.destination)
    if problems:
        raise SystemExit(
            "the site has links that point at nothing published:\n  " + "\n  ".join(problems)
        )
    return written


def _reroot(body: str, depth: int) -> str:
    """Make site-absolute paths relative to a page ``depth`` directories down.

    :func:`rewrite_links` produces paths from the root of the site because that
    is what the map holds. The site is deployed under a project path on GitHub
    Pages (``/netgraph/``), so a leading slash would leave the domain; every
    link is therefore relative, and this is where the ``../`` comes from.
    """
    prefix = "../" * depth

    def replace(match: re.Match[str]) -> str:
        attribute, quote, target = match.group(1), match.group(2), match.group(3)
        if target.startswith(_EXTERNAL) or target.startswith(("../", "./")):
            return match.group(0)
        return f"{attribute}={quote}{prefix}{target}{quote}"

    return re.sub(r'\b(href|src)=(["\'])([^"\']+)\2', replace, body)


def _summary(text: str) -> str:
    """The first paragraph of prose, for the page's ``<meta name=description>``."""
    for block in text.split("\n\n"):
        line = " ".join(block.split())
        if line and not line.startswith(("#", "|", "```", ">", "<", "-", "*")):
            return line[:200]
    return "netgraph — declare your network in YAML and render it as a network graph."


def render_examples(output: Path, *, commit: str) -> list[str]:
    """Draw every example with ``netgraph render -f html``, and frame each one.

    Two files per example, and the split is the point. ``<name>-diagram.html`` is
    the command's output, byte for byte, with nothing added — that is what makes
    it evidence rather than an illustration, and it is linked on its own so a
    reader can open it, save it and see that it works with the network unplugged.
    ``<name>.html`` embeds that file in the site's shell, so the demo is
    something you can arrive at and leave again instead of a dead end a search
    engine drops people into.

    Raises:
        SystemExit: Any example failed to render. The workflow then has nothing
            to deploy, which is the intended outcome: a demo site missing the
            inventory that stopped working is a regression nobody would see.
    """
    demos = output / "demo"
    demos.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for demo in DEMOS:
        inventory = EXAMPLES / demo.name
        if not inventory.is_dir():
            raise SystemExit(f"examples/{demo.name} is in DEMOS but not in the repository")
        target = demos / f"{demo.name}-diagram.html"
        command = [
            sys.executable,
            "-m",
            "netgraph",
            "--inventory",
            str(inventory),
            "render",
            "--format",
            "html",
            "--title",
            f"{demo.name} — netgraph",
            "--element-ids",
            "--output",
            str(target),
        ]
        for layer in demo.layers:
            command += ["--layer", layer]
        result = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT)
        if result.returncode != 0 or not target.is_file():
            raise SystemExit(
                f"examples/{demo.name} did not render:\n"
                f"  command: {' '.join(command)}\n"
                f"  exit {result.returncode}\n{result.stdout}{result.stderr}"
            )
        _frame(output, demo, commit=commit)
        written.append(f"demo/{demo.name}.html")
        written.append(f"demo/{demo.name}-diagram.html")
    return written


def _frame(output: Path, demo: Demo, *, commit: str) -> None:
    """The site page that embeds one render."""
    diagram = f"{demo.name}-diagram.html"
    body = (
        f"<h1>{html.escape(demo.name)}</h1>"
        f"<p>{html.escape(demo.summary)}</p>"
        f'<p class="demo-meta">Drawn at {" · ".join(demo.layers)} from '
        f'<a href="{SOURCE_URL}/tree/main/examples/{demo.name}"><code>'
        f"examples/{html.escape(demo.name)}</code></a> by "
        f"<code>netgraph -i examples/{html.escape(demo.name)} render -f html</code>. "
        f'<a href="{diagram}">Open it on its own</a> — it is one file, and it works '
        "offline.</p>"
        f'<iframe class="demo-frame" src="{diagram}" '
        f'title="{html.escape(demo.name)}, drawn by netgraph" loading="lazy"></iframe>'
        '<p class="demo-meta">Pick a layer from the switcher inside the diagram; hover or '
        "focus a node for its interfaces, addresses, VLANs and cabling; filter by VLAN or "
        'namespace. <a href="index.html">All the examples</a> · '
        '<a href="../docs/getting-started.html">Build one of your own</a>.</p>'
    )
    (output / "demo" / f"{demo.name}.html").write_text(
        shell(
            title=f"{demo.name} — netgraph",
            body=body,
            destination=f"demo/{demo.name}.html",
            description=demo.summary,
            edit=f"{SOURCE_URL}/tree/main/examples/{demo.name}",
            commit=commit,
            wide=True,
        ),
        encoding="utf-8",
        newline="\n",
    )


def build_demo_index(output: Path, *, commit: str) -> None:
    """The page that offers the examples, with a sentence about each."""
    cards = "".join(
        '<li class="card">'
        f'<h3><a href="{demo.name}.html">{html.escape(demo.name)}</a></h3>'
        f"<p>{html.escape(demo.summary)}</p>"
        f'<p class="layers">layers: {" · ".join(demo.layers)}</p>'
        "</li>"
        for demo in DEMOS
    )
    body = (
        "<h1>Try netgraph in your browser</h1>"
        "<p>Each diagram below is one <code>netgraph render -f html</code> run over the "
        'inventory of the same name in <a href="' + SOURCE_URL + '/tree/main/examples">'
        "<code>examples/</code></a>. They are self-contained files — the same ones the "
        "command writes — so everything works offline: pick a layer from the switcher, "
        "filter by VLAN or namespace, hover or focus a node to read its interfaces, "
        "addresses, VLANs and cabling.</p>"
        "<p>To draw your own the same way: "
        "<code>netgraph -i my-network render -f html -o network.html</code>. "
        '<a href="../docs/getting-started.html">Getting started</a> builds an inventory '
        "from nothing in about ten minutes.</p>"
        f'<ul class="cards">{cards}</ul>'
    )
    (output / "demo").mkdir(parents=True, exist_ok=True)
    (output / "demo" / "index.html").write_text(
        shell(
            title="Try netgraph — netgraph",
            body=body,
            destination="demo/index.html",
            description=(
                "Every netgraph example inventory, rendered as an interactive diagram you "
                "can click through without installing anything."
            ),
            edit=f"{SOURCE_URL}/blob/main/tools/build_site.py",
            commit=commit,
        ),
        encoding="utf-8",
        newline="\n",
    )


def copy_assets(output: Path) -> None:
    """The hero diagrams and the committed report's own SVGs, verbatim."""
    for name in COPIED:
        source = REPO_ROOT / name
        target = output / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    (output / "style.css").write_text(STYLE, encoding="utf-8", newline="\n")
    # GitHub Pages runs Jekyll over what it is given unless told not to, and
    # Jekyll drops files and directories beginning with an underscore.
    (output / ".nojekyll").write_text("", encoding="utf-8")


def check_links(output: Path) -> list[str]:
    """Every local ``href``/``src`` in the built site points at a file that is there."""
    broken: list[str] = []
    for path in sorted(output.rglob("*.html")):
        # Pages netgraph itself wrote -- the demo renders, and the committed
        # one under docs/ -- are self-contained by construction and are checked
        # by tests/test_render_html.py. Re-reading them here would only assert
        # that a data URI is not a file.
        relative = path.relative_to(output).as_posix()
        if relative.endswith("-diagram.html") or relative in COPIED:
            continue
        text = path.read_text(encoding="utf-8")
        for _, _, target in re.findall(r'\b(href|src)=(["\'])([^"\']+)\2', text):
            if target.startswith(_EXTERNAL):
                continue
            landing = (path.parent / target.partition("#")[0]).resolve()
            if not landing.exists():
                broken.append(f"{path.relative_to(output)} -> {target}")
    return broken


def current_commit() -> str:
    """The short hash the site was built from, or ``an unknown revision``."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git, no hash
        return "an unknown revision"
    return result.stdout.strip() or "an unknown revision"


def build(output: Path, *, examples: bool = True) -> list[str]:
    """Build the whole site into ``output``, which is emptied first."""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    commit = current_commit()
    copy_assets(output)
    written = build_pages(output, commit=commit)
    if examples:
        written += render_examples(output, commit=commit)
        build_demo_index(output, commit=commit)
    broken = check_links(output)
    if broken:
        raise SystemExit("the built site has broken links:\n  " + "\n  ".join(broken))
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "site",
        help="directory to write the site into; emptied first (default: ./site)",
    )
    parser.add_argument(
        "--no-examples",
        action="store_true",
        help="skip the interactive renders, which are the slow half and need Graphviz",
    )
    arguments = parser.parse_args(argv)
    written = build(arguments.output, examples=not arguments.no_examples)
    print(f"wrote {len(written)} page(s) to {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
