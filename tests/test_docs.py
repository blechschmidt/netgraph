"""The documentation, checked the same way the code is.

Docs rot when nothing verifies them, which is the exact failure mode netgraph
exists to prevent — so these promises are asserted here:

* ``docs/schema-reference.md`` is what ``tools/gen_schema_reference.py`` would
  produce right now. A field added to a model without regenerating the
  reference fails here.
* Every generated region in the documentation — the synopsis, argument and flag
  tables of ``docs/commands/*.md``, the command index, the rule index — is what
  ``tools/gen_docs.py`` would write from the CLI and the rule catalogue today. A
  flag added without regenerating fails here, which is the point: the reference
  is derived from Click's own introspection rather than maintained twice.
* Every command has a page, and every flag of every command appears on it. No
  page names a flag that does not exist.
* ``docs/validation-rules.md`` documents every rule in
  :data:`netgraph.rules.RULES`, with the severity and the aliases the code
  actually uses, and every rule appears in the index in ``docs/validation.md``.
  A new rule that is never written up fails here.
* Every relative link and image in the Markdown of this repository points at a
  file that exists, and at a heading that exists when it carries an anchor.
* Every fenced ``console``/``bash`` example that invokes ``netgraph`` is either
  executed and compared against its transcript, or explicitly excused with a
  reason. See ``tools/check_examples.py``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from netgraph.report import LOAD_RULE
from netgraph.rules import RULES

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
COMMAND_DOCS = DOCS / "commands"
TOOLS = REPO_ROOT / "tools"
GENERATOR = TOOLS / "gen_schema_reference.py"

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


def load_tool(name: str) -> ModuleType:
    """Import a script from ``tools/`` as a module."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The modules define dataclasses, whose field resolution looks the defining
    # module up in sys.modules; registering it first is not optional.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_generator() -> ModuleType:
    """Import ``tools/gen_schema_reference.py`` as a module."""
    return load_tool("gen_schema_reference")


#: The two tools that derive documentation from the code. Imported eagerly
#: because the parametrisation below is built from what they find.
GEN_DOCS = load_tool("gen_docs")
EXAMPLES = load_tool("check_examples")


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


@pytest.mark.parametrize("rule", [*RULES, LOAD_RULE], ids=[rule.id for rule in (*RULES, LOAD_RULE)])
def test_every_rule_title_matches_its_heading(rule: object, validation_rules_doc: str) -> None:
    """``Rule.title`` is what the deep link in every report is built from.

    The SARIF ``helpUri`` and the human-readable part of a GitHub annotation
    title both come from it, so a heading reworded without touching the
    catalogue would ship links that 404 in somebody's code-scanning UI.
    """
    assert isinstance(rule, type(RULES[0]))
    assert rule.title, f"{rule.id} has no title"
    assert rule.anchor in anchors_of(DOCS / "validation-rules.md"), (
        f"{rule.id}: no heading in validation-rules.md answers to '#{rule.anchor}'"
    )
    assert rule.help_uri.endswith(f"#{rule.anchor}")


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


def test_the_readme_is_short_enough_to_be_read() -> None:
    """A README nobody can find the start of is the problem, not the fix.

    GitHub truncates a long one, and a newcomer gives up before the first useful
    command. The cap is deliberately generous — it is a ceiling, not a target —
    and everything beyond a quickstart and a map belongs in ``docs/``.
    """
    lines = (REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 500, f"README.md is {len(lines)} lines; move a section into docs/"


def test_the_tooltip_example_is_what_netgraph_produces() -> None:
    """The worked example in the rendering guide, rendered rather than typed.

    A sample of output in a document is a promise about the tool, and the only
    kind of promise that survives a refactor is one a test makes.
    """
    from netgraph.loader import load_tree
    from netgraph.render import (
        RenderOptions,
        build_details,
        build_graph,
        detail_text,
        element_ids,
    )

    page = (DOCS / "rendering.md").read_text(encoding="utf-8")
    block = page.partition("<!-- tooltip-example -->")[2]
    documented = block.partition("```\n")[2].partition("```")[0].rstrip("\n")
    assert documented, "the tooltip example is missing from docs/rendering.md"

    graph = build_graph(load_tree(REPO_ROOT / "examples" / "quickstart"))
    ids = element_ids(graph)
    details = build_details(graph, RenderOptions(), ids=ids)
    produced = detail_text(details[ids.nodes["devices/sw-office"]])
    assert produced == documented


# --------------------------------------------------------------------------- #
# The command reference, generated from Click
# --------------------------------------------------------------------------- #

#: Every page that may hold a generated region.
REGION_PAGES = [path for path in GEN_DOCS.pages() if "<!-- generated:" in path.read_text("utf-8")]

#: Every command below ``netgraph``, ``config show`` included.
COMMAND_PATHS = sorted(GEN_DOCS.command_paths())

#: Flag-shaped strings the command pages name on purpose although no command has
#: them. Keeping this explicit is the point: an accidental one fails the test.
FOREIGN_FLAGS = {
    # docs/commands/validate.md says, of a summary-only mode, that there is none.
    "--summary",
}


@pytest.mark.parametrize(
    "path", REGION_PAGES, ids=[str(p.relative_to(REPO_ROOT)) for p in REGION_PAGES]
)
def test_every_generated_region_is_up_to_date(path: Path) -> None:
    """The synopsis, flag and index tables are what the code says today."""
    current = path.read_text(encoding="utf-8")
    assert current == GEN_DOCS.regenerate(current), (
        f"{path.relative_to(REPO_ROOT)} has a stale generated region; "
        "run 'python tools/gen_docs.py'"
    )


def test_the_command_pages_are_the_commands_that_exist() -> None:
    """Every command has a page, and every page belongs to a command."""
    documented = set(GEN_DOCS.PAGE)
    assert documented == set(COMMAND_PATHS), (
        "tools/gen_docs.py:PAGE disagrees with the CLI: "
        f"missing {sorted(set(COMMAND_PATHS) - documented)}, "
        f"stale {sorted(documented - set(COMMAND_PATHS))}"
    )
    for command, page in GEN_DOCS.PAGE.items():
        assert (COMMAND_DOCS / page).is_file(), f"{command} is documented in a missing {page}"


def test_the_command_index_covers_every_leaf_command() -> None:
    """A command absent from the index is a command nobody will find."""
    # ``config`` is a group: its page documents ``config show``, which is what a
    # reader types, so the group itself is deliberately not in the index.
    groups = {
        path
        for path in COMMAND_PATHS
        if any(other.startswith(f"{path} ") for other in COMMAND_PATHS)
    }
    assert set(GEN_DOCS.INDEX_ORDER) == set(COMMAND_PATHS) - groups
    assert set(GEN_DOCS.SUMMARY) == set(GEN_DOCS.INDEX_ORDER)


@pytest.mark.parametrize("command", COMMAND_PATHS)
def test_every_flag_appears_on_its_command_page(command: str) -> None:
    """A flag the CLI accepts and the docs never mention does not exist to a reader."""
    page = COMMAND_DOCS / GEN_DOCS.PAGE[command]
    text = page.read_text(encoding="utf-8")
    assert f"netgraph {command}" in text, f"{page.name} never names 'netgraph {command}'"
    for flag in GEN_DOCS.flags_of(command):
        assert re.search(rf"`{re.escape(flag)}`(?![-\w])", text), (
            f"{page.name} does not document {flag} of 'netgraph {command}'"
        )


@pytest.mark.parametrize(
    "path",
    sorted(COMMAND_DOCS.glob("*.md")),
    ids=lambda p: p.name,
)
def test_no_command_page_names_a_flag_that_does_not_exist(path: Path) -> None:
    """The other direction: a flag removed from the CLI but left in the prose."""
    known = {flag for command in ["", *COMMAND_PATHS] for flag in GEN_DOCS.flags_of(command)}
    known |= FOREIGN_FLAGS
    # ``-h`` comes from click rather than from a decorator, so it is not in
    # ``params`` — but it works, and the pages are allowed to say so.
    known |= {"-h", "--help"}
    for token in re.findall(r"`(--?[A-Za-z0-9][-A-Za-z0-9]*)`", path.read_text(encoding="utf-8")):
        assert token in known, f"{path.name} names {token}, which no command has"


# --------------------------------------------------------------------------- #
# The rule index
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rule", RULES, ids=[rule.id for rule in RULES])
def test_every_rule_id_and_alias_reaches_the_validation_reference(rule: object) -> None:
    """``NG-*`` is the vocabulary of the specification and of ``--disable``.

    A reader who meets ``NG-C002`` in a diagnostic must be able to find it, so
    both the reference and the index in ``docs/validation.md`` have to name it.
    """
    assert isinstance(rule, type(RULES[0]))
    reference = (DOCS / "validation-rules.md").read_text(encoding="utf-8")
    index = (DOCS / "validation.md").read_text(encoding="utf-8")
    assert rule.id in reference and rule.id in index
    for alias in rule.aliases:
        assert alias in reference, f"validation-rules.md never names {alias}"
        assert alias in index, f"the index in validation.md never names {alias}"


# --------------------------------------------------------------------------- #
# The examples
# --------------------------------------------------------------------------- #

_BLOCKS, _UNMARKED = EXAMPLES.all_blocks()


def test_every_example_declares_whether_it_runs() -> None:
    """Neither state is the silent default: checked, or excused with a reason."""
    offenders = [entry.id for entry in _UNMARKED]
    assert not offenders, (
        "these examples invoke netgraph but carry no '<!-- run: … -->' or "
        f"'<!-- norun: … -->' marker: {offenders}"
    )


def test_the_examples_are_mostly_executed() -> None:
    """A suite of excuses would satisfy the letter of the check and nothing else."""
    executed = [block for block in _BLOCKS if block.runnable]
    assert len(executed) >= len(_BLOCKS) // 2, (
        f"only {len(executed)} of {len(_BLOCKS)} examples are executed"
    )


@pytest.mark.parametrize("block", _BLOCKS, ids=[block.id for block in _BLOCKS])
def test_the_documented_example_is_what_netgraph_prints(block: Any) -> None:
    """Run the transcript and diff it, or check the excuse gives a reason.

    A ``run`` block is executed through the installed console script, so a usage
    error it documents carries the program name a reader would actually see.
    """
    problem = EXAMPLES.check(block)
    assert problem is None, f"{block.id}: {problem}"
