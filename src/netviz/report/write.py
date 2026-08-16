"""Turning a collected report into files: Markdown, HTML or JSON.

One template environment, three formats, and a single rule that decides what a
link looks like — so a bundle is the same document however it is written.

**Templates, not string building.** Every page goes through a Jinja2 template
under ``netviz/report/templates``, and ``--template DIR`` puts a directory in
front of them: a template found there wins, and everything else falls back to the
bundled one, so overriding the device page does not mean re-implementing the
overview. The templates are deliberately dumb — loops over the model of
:mod:`netviz.report.model` and nothing else — because the moment a template
starts deriving facts, the layout stops being safely editable.

**Determinism.** The output is written with ``\\n`` line endings on every
platform, JSON is dumped in a fixed key order, tables are already sorted by the
collector and nothing here reads a clock, an environment variable or a hostname.
Two runs over the same inventory produce the same bytes, which is what makes a
report committable and diffable (the generated-at stamp is the one variable, and
:mod:`netviz.report.stamp` explains how to pin it).

**Self-contained HTML.** Each page inlines the one style sheet
(``assets/report.css``) through the same
:func:`~netviz.render.html.asset_text` the interactive renderer uses, and
carries the matching :func:`~netviz.render.html.policy` in a ``<meta>``: no
fetch, no script, no font, nothing to serve beside the files.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Final

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, StrictUndefined
from markupsafe import Markup

from netviz.errors import NetvizError
from netviz.render.html import asset_text, policy
from netviz.report.layout import Layout
from netviz.report.model import Cell, Column, Page, Report

__all__ = [
    "JSON_FILE",
    "markdown_align",
    "markdown_cell",
    "markdown_link",
    "markdown_text",
    "render_pages",
    "stylesheet",
]

#: A ``<`` that a Markdown renderer would read as the start of raw HTML: a tag
#: name, a closing tag, a comment or a processing instruction.
_TAG_START: Final = re.compile(r"<(?=[A-Za-z/!?])")

#: The one file a JSON report writes.
JSON_FILE: Final = "report.json"

#: Inlined into every HTML page.
_STYLES: Final[tuple[str, ...]] = ("report.css",)

#: Template per page kind, per format. A page kind with no entry is a programming
#: error rather than a user one, so the lookup is allowed to raise.
_TEMPLATES: Final[Mapping[str, Mapping[str, str]]] = {
    "markdown": {
        "overview": "overview.md.j2",
        "site": "site.md.j2",
        "device": "device.md.j2",
    },
    "html": {
        "overview": "overview.html.j2",
        "site": "site.html.j2",
        "device": "device.html.j2",
    },
}


def stylesheet() -> str:
    """The report style sheet, as text."""
    return asset_text(_STYLES, package="netviz.report")


def render_pages(
    report: Report, *, layout: Layout, templates: Path | None = None
) -> dict[str, bytes]:
    """Every page of ``report``, rendered, keyed by bundle-relative path.

    Args:
        report: The collected document.
        layout: Where the pages go; used to resolve every cross-reference
            relative to the page being written.
        templates: A directory whose templates take precedence over the bundled
            ones. Only consulted for the names in :data:`_TEMPLATES`.

    Raises:
        NetvizError: A template is missing or does not render.
    """
    if report.meta.format == "json":
        return {JSON_FILE: _json_bytes(report)}

    environment = _environment(templates)
    style = stylesheet()
    written: dict[str, bytes] = {}
    for page in report.pages:
        name = _TEMPLATES[report.meta.format][page.kind]
        try:
            template = environment.get_template(name)
        except Exception as exc:  # jinja raises several types for one problem
            raise NetvizError(f"cannot load the report template {name!r}: {exc}") from exc
        try:
            text = template.render(
                page=page,
                report=report,
                meta=report.meta,
                link=_linker(layout, page),
                # The policy is a hash of the exact text the template inlines,
                # so the two must be the same string: a newline between them
                # would produce a page whose own CSP refuses its style sheet.
                style=Markup(style),
                csp=policy(style),
            )
        except Exception as exc:
            raise NetvizError(f"the report template {name!r} failed: {exc}") from exc
        written[page.path] = _encode(text)
    return written


def _linker(layout: Layout, page: Page) -> Callable[..., str]:
    """A ``link(cell)`` callable for the template of ``page``.

    Handed to the template rather than pre-resolved into the model because the
    answer depends on which page is being written, and the model is written once
    for all of them.
    """

    def link(target: Cell | str, fragment: str = "") -> str:
        if isinstance(target, Cell):
            if target.url:
                return target.url
            return layout.link(target.page, frm=page.path, fragment=target.fragment)
        return layout.link(target, frm=page.path, fragment=fragment)

    return link


def _json_bytes(report: Report) -> bytes:
    """The whole report as one JSON document.

    ``sort_keys`` is off on purpose: the key order is the one
    :meth:`~netviz.report.model.Report.to_json` writes, which is the order a
    reader wants rather than the alphabet, and it is just as deterministic.
    """
    text = json.dumps(report.to_json(), indent=2, ensure_ascii=False, sort_keys=False)
    return _encode(f"{text}\n")


def _encode(text: str) -> bytes:
    """UTF-8 with ``\\n`` line endings, whatever platform this runs on."""
    return text.replace("\r\n", "\n").encode("utf-8")


def markdown_text(value: object) -> str:
    """Inventory text, safe to drop into Markdown prose.

    Two things are escaped, and deliberately only two. A ``<`` that could open an
    HTML tag: a report is something people publish, so a device description
    containing ``<script>`` has to land on the page as text — while ``<0.1%`` in a
    utilisation cell is not a tag and is left alone, because a document that
    spelled every comparison operator as an entity would be worse to read as a
    file, which is half of what Markdown is for. And the brackets of a link label,
    for the reason :data:`_TEXT_ESCAPES` gives.

    Everything a template writes *around* these values — the headings, the tables,
    the links — is markup the template owns. Text that reaches this function is
    data, and this is where the two are kept apart.
    """
    text = _TAG_START.sub("&lt;", str(value))
    for character in _TEXT_ESCAPES:
        text = text.replace(character, f"\\{character}")
    return text


def markdown_link(value: Cell, link: Callable[..., str]) -> str:
    """One :class:`~netviz.report.model.Cell` as Markdown, linked if it can be.

    A filter rather than a macro so a template can write a whole row as
    ``row | map("mdcell", link) | join(" | ")``: a Jinja loop inside a table row
    would have to fight the block-trimming rules for every newline, and a
    Markdown table that loses a line break stops being a table.
    """
    text = markdown_cell(value.text)
    url = link(value)
    return f"[{text}]({url})" if url and text else text


def markdown_align(column: Column) -> str:
    """The delimiter cell that gives one column its alignment."""
    return "---:" if column.align == "right" else ":---"


#: Characters that would end the construct inventory text sits in. ``[`` and ``]``
#: delimit the label of a link — and a namespace segment is a *directory name*,
#: never validated against the element-name grammar, so ``a](javascript:…)`` is a
#: name somebody can create. ``|`` ends a table cell. The backslash goes first, or
#: it would escape the escapes.
_TEXT_ESCAPES: Final = ("\\", "[", "]")
_CELL_ESCAPES: Final = (*_TEXT_ESCAPES, "|")


def markdown_cell(value: object) -> str:
    """Inventory text, safe inside a Markdown *table* cell and inside a link.

    A cell additionally cannot hold a line break — GitHub-flavoured tables are one
    row per line — and an unescaped ``|`` would end the cell early and shift every
    column after it.
    """
    text = markdown_text(value).replace("|", "\\|")
    return " ".join(text.split())


@lru_cache(maxsize=4)
def _environment(templates: Path | None) -> Environment:
    """The Jinja2 environment, with ``--template DIR`` in front of the package.

    Autoescaping is on for the HTML templates and off for the Markdown ones,
    where HTML escaping would corrupt the document rather than protect it; the
    Markdown templates pipe every inventory-derived value through
    :func:`markdown_text` or :func:`markdown_cell` instead. The one thing that
    reaches a page as markup either way is an SVG the renderer itself produced,
    and that arrives as :class:`markupsafe.Markup` after
    :mod:`netviz.render.fragment` has sanitised it.
    """
    loaders: list[FileSystemLoader | PackageLoader] = [PackageLoader("netviz.report", "templates")]
    if templates is not None:
        if not templates.is_dir():
            raise NetvizError(f"--template {templates} is not a directory")
        loaders.insert(0, FileSystemLoader(str(templates)))
    environment = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=lambda name: bool(name and name.endswith(".html.j2")),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    environment.filters["md"] = markdown_text
    environment.filters["cell"] = markdown_cell
    environment.filters["mdcell"] = markdown_link
    environment.filters["mdalign"] = markdown_align
    return environment
