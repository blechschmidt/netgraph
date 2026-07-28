"""The documentation, checked the same way the code is.

Docs rot when nothing verifies them, which is the exact failure mode netgraph
exists to prevent — so three promises are asserted here:

* ``docs/schema-reference.md`` is what ``tools/gen_schema_reference.py`` would
  produce right now. A field added to a model without regenerating the
  reference fails here.
* ``docs/validation-rules.md`` documents every rule in
  :data:`netgraph.rules.RULES`, with the severity and the aliases the code
  actually uses. A new rule that is never written up fails here.
* Every relative link and image in the Markdown of this repository points at a
  file that exists, and at a heading that exists when it carries an anchor.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from netgraph.rules import RULES

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
GENERATOR = REPO_ROOT / "tools" / "gen_schema_reference.py"

#: Every Markdown file that is part of the documentation, not of a fixture.
MARKDOWN = sorted(
    path
    for path in REPO_ROOT.rglob("*.md")
    if not any(part in {".venv", ".cloop", "node_modules", ".git"} for part in path.parts)
)

#: ``[text](target)`` and ``<img src="target">``.
_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)")
_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=\"([^\"]+)\"")
#: ATX headings outside fenced code blocks are filtered separately.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

_EXTERNAL = ("http://", "https://", "mailto:", "ftp://")


def load_generator() -> ModuleType:
    """Import ``tools/gen_schema_reference.py`` as a module."""
    name = "gen_schema_reference"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The module defines dataclasses, whose field resolution looks the defining
    # module up in sys.modules; registering it first is not optional.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# The generated reference
# --------------------------------------------------------------------------- #


def test_the_schema_reference_is_up_to_date() -> None:
    """``docs/schema-reference.md`` matches what the models say today."""
    generator = load_generator()
    expected = generator.build()
    actual = generator.OUTPUT.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/schema-reference.md is stale; run 'python tools/gen_schema_reference.py'"
    )


def test_the_generator_covers_every_field_of_every_documented_model() -> None:
    """The coverage check is the generator's own guard; make sure it runs."""
    generator = load_generator()
    documented = set(generator.FIELD_DOCS)
    for section in generator.SECTIONS:
        for name in section.model.model_fields:
            assert (section.model.__name__, name) in documented, (
                f"{section.model.__name__}.{name} has no FIELD_DOCS entry"
            )


# --------------------------------------------------------------------------- #
# The rule catalogue
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def validation_rules_doc() -> str:
    return (DOCS / "validation-rules.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("rule", RULES, ids=[rule.id for rule in RULES])
def test_every_rule_is_documented(rule: object, validation_rules_doc: str) -> None:
    """Id, severity and every alias appear in the write-up of the rule."""
    assert isinstance(rule, type(RULES[0]))
    heading = f"#### `{rule.id}` — "
    assert heading in validation_rules_doc, f"{rule.id} has no section in validation-rules.md"

    section = validation_rules_doc.split(heading, 1)[1].split("\n#### ", 1)[0]
    assert f"Severity: {rule.severity}." in section, (
        f"{rule.id} is documented with a severity other than {rule.severity}"
    )
    for alias in rule.aliases:
        assert alias in section, f"{rule.id} does not mention its alias {alias}"


def test_the_rule_document_explains_how_to_suppress_each_rule(validation_rules_doc: str) -> None:
    for rule in RULES:
        section = validation_rules_doc.split(f"#### `{rule.id}` — ", 1)[1].split("\n#### ", 1)[0]
        assert "**Suppress with**" in section, f"{rule.id} does not say how to suppress it"


def test_the_rule_document_names_no_rule_that_does_not_exist(validation_rules_doc: str) -> None:
    """Short ids are the validator's vocabulary; a stray one would be a lie."""
    known = {rule.id for rule in RULES}
    for mentioned in set(re.findall(r"\b([EW]\d{3})\b", validation_rules_doc)):
        assert mentioned in known, f"validation-rules.md documents unknown rule {mentioned}"


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


def slug(heading: str) -> str:
    """The anchor GitHub derives from a heading."""
    text = heading.strip().lower()
    text = re.sub(r"`([^`]*)`", r"\1", text)  # inline code keeps its content
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links keep their text
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    """Every anchor a Markdown file offers, headings and explicit ids alike."""
    found: set[str] = set()
    fence: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        opening = _FENCE_RE.match(line)
        if opening is not None:
            marker = opening.group(1)
            fence = None if fence == marker else (fence or marker)
            continue
        if fence is not None:
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            found.add(slug(heading.group(2)))
    found.update(re.findall(r'<a\s+(?:id|name)="([^"]+)"', path.read_text(encoding="utf-8")))
    return found


def prose_of(path: Path) -> str:
    """The file with fenced blocks and inline code removed.

    A regex such as ``[-a-z0-9_.]*[a-z0-9]`` inside backticks is not a link, and
    neither is anything inside a ```` ``` ```` block.
    """
    lines: list[str] = []
    fence: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        opening = _FENCE_RE.match(line)
        if opening is not None:
            marker = opening.group(1)
            fence = None if fence == marker else (fence or marker)
            continue
        if fence is None:
            lines.append(re.sub(r"`[^`]*`", "``", line))
    return "\n".join(lines)


def targets_of(path: Path) -> list[str]:
    text = prose_of(path)
    return [*_LINK_RE.findall(text), *_IMG_RE.findall(text)]


@pytest.mark.parametrize("path", MARKDOWN, ids=[str(p.relative_to(REPO_ROOT)) for p in MARKDOWN])
def test_every_relative_link_resolves(path: Path) -> None:
    for target in targets_of(path):
        if target.startswith(_EXTERNAL):
            continue
        file_part, _, anchor = target.partition("#")
        if not file_part:
            # A same-file anchor.
            assert anchor in anchors_of(path), f"{path.name}: no heading for '#{anchor}'"
            continue

        destination = (path.parent / file_part).resolve()
        assert destination.exists(), f"{path.name}: link target {target!r} does not exist"
        if anchor and destination.suffix == ".md":
            assert anchor in anchors_of(destination), (
                f"{path.name}: {destination.name} has no heading for '#{anchor}'"
            )


def test_the_readme_shows_a_committed_diagram() -> None:
    """The picture at the top of the README is a file, not a broken image."""
    for name in ("home-lab.svg", "quickstart.svg"):
        image = DOCS / "images" / name
        assert image.is_file(), f"docs/images/{name} is missing"
        content = image.read_text(encoding="utf-8")
        assert "<svg" in content and content.rstrip().endswith("</svg>")
    assert "docs/images/home-lab.svg" in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
