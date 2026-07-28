"""Exclusion of inventory paths through ``.netgraphignore`` files.

The syntax is the subset of ``.gitignore`` that makes sense for an inventory
tree, so users do not have to learn a second pattern language:

* Blank lines and lines whose first character is ``#`` are ignored.
* A leading ``!`` negates the pattern; the *last* matching rule decides.
* A trailing ``/`` restricts the rule to directories.
* A pattern containing a ``/`` anywhere but at the end is anchored to the
  directory holding the ``.netgraphignore``; otherwise it matches a basename at
  any depth.
* ``*`` and ``?`` do not cross ``/``; ``**`` does. Character classes
  (``[0-9]``) and backslash escapes are supported.

As in git, a file below an excluded directory cannot be re-included: the walker
never descends into a pruned directory. A ``.netgraphignore`` in a subdirectory
applies to that subtree, and its rules are evaluated after (and therefore win
over) those of its parents.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

__all__ = [
    "IGNORE_FILE_NAME",
    "IgnoreRule",
    "IgnoreRuleSet",
    "IgnoreStack",
    "compile_rules",
    "parse_ignore_file",
]

#: Name of the per-directory exclusion file.
IGNORE_FILE_NAME: Final = ".netgraphignore"

#: Trailing whitespace is insignificant unless the last space is escaped.
_TRAILING_SPACE_RE: Final = re.compile(r"(?<!\\)\s+$")


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    """One pattern line of a ``.netgraphignore``."""

    #: The pattern as written, quoted in diagnostics.
    pattern: str
    #: Compiled form, matched against a path relative to :attr:`IgnoreRuleSet.base`.
    regex: re.Pattern[str]
    #: ``!pattern`` — a match re-includes the path instead of excluding it.
    negated: bool = False
    #: ``pattern/`` — the rule only applies to directories.
    directory_only: bool = False
    #: 1-based line number in the source file, for diagnostics.
    lineno: int = 0

    def matches(self, relative: str, *, is_dir: bool) -> bool:
        """Does this rule apply to ``relative`` (a path relative to the base)?"""
        if self.directory_only and not is_dir:
            return False
        return self.regex.fullmatch(relative) is not None


@dataclass(frozen=True, slots=True)
class IgnoreRuleSet:
    """The rules of a single ``.netgraphignore`` plus the subtree they govern."""

    #: Directory holding the file, relative to the inventory root (``""`` at the root).
    base: str
    #: Absolute path of the file the rules came from.
    source: Path
    rules: tuple[IgnoreRule, ...]

    def verdict(self, relative: str, *, is_dir: bool) -> bool | None:
        """Return ``True`` (exclude), ``False`` (re-include) or ``None`` (no rule).

        ``relative`` is a POSIX path relative to the *inventory root*; paths
        outside this rule set's subtree never match.
        """
        candidate = self._strip_base(relative)
        if candidate is None:
            return None
        decision: bool | None = None
        for rule in self.rules:
            if rule.matches(candidate, is_dir=is_dir):
                decision = not rule.negated
        return decision

    def _strip_base(self, relative: str) -> str | None:
        """Re-root ``relative`` on :attr:`base`, or ``None`` if it is outside."""
        if not self.base:
            return relative
        prefix = f"{self.base}/"
        if not relative.startswith(prefix):
            return None
        return relative[len(prefix) :]


@dataclass(frozen=True, slots=True)
class IgnoreStack:
    """The rule sets in effect for a directory, ordered root-first.

    Deeper files are evaluated last so that a nested ``.netgraphignore`` can
    override a decision made further up the tree.
    """

    rule_sets: tuple[IgnoreRuleSet, ...] = ()

    def push(self, rule_set: IgnoreRuleSet | None) -> IgnoreStack:
        """Return a stack with ``rule_set`` appended (``self`` when it is ``None``)."""
        if rule_set is None or not rule_set.rules:
            return self
        return IgnoreStack((*self.rule_sets, rule_set))

    def is_ignored(self, relative: str | PurePosixPath, *, is_dir: bool) -> bool:
        """Is ``relative`` (a path relative to the inventory root) excluded?

        Ancestors are consulted first: as in git, a path below an excluded
        directory stays excluded even if a later rule would re-include it.
        """
        path = PurePosixPath(relative)
        for ancestor in reversed(path.parents):
            text = ancestor.as_posix()
            if text != "." and self._decide(text, is_dir=True):
                return True
        return self._decide(path.as_posix(), is_dir=is_dir)

    def _decide(self, relative: str, *, is_dir: bool) -> bool:
        decision: bool | None = None
        for rule_set in self.rule_sets:
            verdict = rule_set.verdict(relative, is_dir=is_dir)
            if verdict is not None:
                decision = verdict
        return decision is True

    def __bool__(self) -> bool:
        return bool(self.rule_sets)


def parse_ignore_file(path: Path, *, base: str) -> IgnoreRuleSet:
    """Read and compile a ``.netgraphignore``.

    Args:
        path: The file to read.
        base: Its directory relative to the inventory root, POSIX style.

    Returns:
        The compiled rule set; unreadable or malformed *lines* never raise, an
        unreadable *file* does.

    Raises:
        OSError: The file cannot be read.
        UnicodeDecodeError: The file is not valid UTF-8.
    """
    text = path.read_text(encoding="utf-8-sig")
    return IgnoreRuleSet(base=base, source=path, rules=tuple(compile_rules(text.splitlines())))


def compile_rules(lines: Iterable[str]) -> list[IgnoreRule]:
    """Compile the pattern lines of a ``.netgraphignore``, skipping comments."""
    rules: list[IgnoreRule] = []
    for lineno, line in enumerate(lines, start=1):
        rule = _compile_rule(line, lineno)
        if rule is not None:
            rules.append(rule)
    return rules


def _compile_rule(line: str, lineno: int) -> IgnoreRule | None:
    """Compile one line, or return ``None`` for a blank line or a comment."""
    original = line
    text = _TRAILING_SPACE_RE.sub("", line)
    if not text or text.startswith("#"):
        return None

    negated = text.startswith("!")
    if negated:
        text = text[1:]
    # ``\#`` and ``\!`` are literal leading characters.
    if text.startswith("\\") and len(text) > 1 and text[1] in "#!":
        text = text[1:]

    directory_only = text.endswith("/")
    if directory_only:
        text = text[:-1]
    if not text:
        return None

    anchored = "/" in text.removeprefix("/")
    if text.startswith("/"):
        anchored = True
        text = text[1:]
    if not text:
        return None

    body = _translate(text)
    prefix = "" if anchored else "(?:.*/)?"
    return IgnoreRule(
        pattern=original.strip(),
        regex=re.compile(f"{prefix}{body}"),
        negated=negated,
        directory_only=directory_only,
        lineno=lineno,
    )


def _translate(pattern: str) -> str:
    """Translate a gitignore-style glob into a regular expression body."""
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("/**", index) and index + 3 == length:
            # ``a/**`` matches everything below ``a``.
            out.append("/.*")
            index += 3
            continue
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue

        char = pattern[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            klass, index = _translate_class(pattern, index)
            out.append(klass)
            continue
        elif char == "\\" and index + 1 < length:
            out.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _translate_class(pattern: str, start: int) -> tuple[str, int]:
    """Translate a ``[...]`` character class, returning it and the next index."""
    index = start + 1
    if index < len(pattern) and pattern[index] in "!^":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    while index < len(pattern) and pattern[index] != "]":
        index += 1
    if index >= len(pattern):
        # Unterminated class: treat the bracket as a literal, as git does.
        return re.escape("["), start + 1
    inner = pattern[start + 1 : index]
    if inner.startswith("!"):
        inner = f"^{inner[1:]}"
    return f"[{inner}]", index + 1
