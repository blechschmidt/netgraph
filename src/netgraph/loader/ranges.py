"""Bracket expansion of ``interfaces[].range`` (§6.2.5 of ``docs/schema.md``).

A 48-port switch is 48 near-identical ``interfaces`` entries, which is enough
typing that people stop describing their access layer at all. One entry may
therefore declare ``range:`` instead of ``name:``::

    - range: GigabitEthernet1/0/[1-48]
      type: ethernet
      description: Access port {}
      enabled: false
      vlan: {mode: access, access_vlan: 10}

Expansion happens here, in the loader, immediately after the document is parsed
and before it reaches the models. Everything downstream — validation, the graph,
every renderer, ``netgraph show``, an editor driven by the JSON Schema — sees an
ordinary list of interfaces and needs no notion of a range at all.

Three properties are worth stating precisely, because a user has to be able to
predict them:

* **Ordering.** Several spans expand as an odometer, rightmost fastest:
  ``ge-[0-1]/0/[0-3]`` yields ``ge-0/0/0 … ge-0/0/3`` then ``ge-1/0/0 …``. The
  expansion of one entry lands where that entry was, so the surrounding
  interfaces keep their relative order.
* **Zero padding.** The width of the *low* bound is the width of every value it
  produces, so ``[01-12]`` yields ``01 … 12`` and ``[1-12]`` yields ``1 … 12``.
* **Bounds.** A document expands to at most
  :data:`MAX_INTERFACES_PER_DOCUMENT` interfaces. ``[1-99999999]`` is a typo,
  and the answer to a typo is a diagnostic, not an out-of-memory kill.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

from netgraph.errors import SchemaIssue, echo_value
from netgraph.loader.provenance import FieldPath, Provenance, Site

__all__ = [
    "MAX_INTERFACES_PER_DOCUMENT",
    "MAX_SPANS_PER_RANGE",
    "Expansion",
    "RangeError",
    "RangePattern",
    "Span",
    "expand_interfaces",
    "parse_range",
    "substitute",
]

#: ``NG-R003`` — ceiling on the interfaces one document may expand to. A
#: chassis switch tops out around 576 ports; 4096 leaves room for a stack of
#: them and still bounds the work a single malformed span can ask for.
MAX_INTERFACES_PER_DOCUMENT: Final = 4096

#: ``NG-R002`` — how many spans one pattern may carry. Three (``ge-[0-1]/[0-1]/[0-47]``)
#: covers slot/module/port; the limit exists so the product cannot be built out
#: of a hundred tiny spans that individually look harmless.
MAX_SPANS_PER_RANGE: Final = 4

#: One span: ``[`` low ``-`` high ``]``, both decimal, leading zeros allowed.
_SPAN_RE: Final = re.compile(r"\[(\d+)-(\d+)\]")

#: The literal text between spans. Deliberately excludes the brackets, so a
#: stray ``[`` or ``]`` cannot be mistaken for part of a name.
_LITERAL_RE: Final = re.compile(r"[^\[\]]*")


class RangeError(ValueError):
    """A ``range`` that cannot be expanded, carrying the rule it violates."""

    def __init__(self, message: str, *, rule: str) -> None:
        self.rule = rule
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Span:
    """One ``[low-high]`` span of a range pattern, inclusive at both ends."""

    low: int
    high: int
    #: Number of digits every value is padded to; 1 means "no padding".
    width: int = 1

    def __len__(self) -> int:
        return self.high - self.low + 1

    def values(self) -> Iterator[str]:
        """Every value the span takes, in ascending order, already padded."""
        for value in range(self.low, self.high + 1):
            yield f"{value:0{self.width}d}"


@dataclass(frozen=True, slots=True)
class RangePattern:
    """A parsed ``range``: ``len(spans) + 1`` literals interleaved with spans."""

    literals: tuple[str, ...]
    spans: tuple[Span, ...]

    @property
    def count(self) -> int:
        """How many interface names this pattern expands to."""
        total = 1
        for span in self.spans:
            total *= len(span)
        return total

    def expand(self) -> Iterator[tuple[str, tuple[str, ...]]]:
        """Yield ``(name, span values)``, rightmost span varying fastest."""
        for values in _odometer(self.spans):
            name = self.literals[0]
            for literal, value in zip(self.literals[1:], values, strict=True):
                name += value + literal
            yield name, values


def _odometer(spans: Sequence[Span]) -> Iterator[tuple[str, ...]]:
    """The cartesian product of ``spans`` with the last position fastest.

    Written out rather than delegated to :func:`itertools.product` because
    ``product`` materialises every input iterable up front; here the caller has
    already been told the product is small enough to build, but the *inputs*
    have not, and a span of ten million values should not be listed to discover
    that the product is bounded.
    """
    if not spans:
        yield ()
        return
    head, *rest = spans
    for value in head.values():
        for tail in _odometer(rest):
            yield (value, *tail)


def parse_range(pattern: Any) -> RangePattern:
    """Parse a ``range`` value into literals and spans.

    Raises:
        RangeError: ``pattern`` is not a string, carries no span, has an
            unbalanced or malformed bracket, an inverted span, or more spans
            than :data:`MAX_SPANS_PER_RANGE`.
    """
    if not isinstance(pattern, str):
        raise RangeError(
            f"'range' must be a string such as 'GigabitEthernet1/0/[1-48]', "
            f"got {type(pattern).__name__}",
            rule="NG-R002",
        )

    literals: list[str] = []
    spans: list[Span] = []
    position = 0
    while position < len(pattern):
        literal = _LITERAL_RE.match(pattern, position)
        assert literal is not None  # the pattern matches the empty string
        position = literal.end()
        if position >= len(pattern):
            literals.append(literal.group())
            break
        span = _SPAN_RE.match(pattern, position)
        if span is None:
            raise RangeError(
                f"{echo_value(pattern)} is not a valid range: expected a span "
                f"'[low-high]' at character {position + 1}",
                rule="NG-R002",
            )
        literals.append(literal.group())
        spans.append(_span_from(span, pattern))
        position = span.end()
    else:
        literals.append("")

    if not spans:
        raise RangeError(
            f"{echo_value(pattern)} declares no span; a range needs at least one "
            "'[low-high]', and a single interface is written with 'name'",
            rule="NG-R002",
        )
    if len(spans) > MAX_SPANS_PER_RANGE:
        raise RangeError(
            f"{echo_value(pattern)} declares {len(spans)} spans; at most "
            f"{MAX_SPANS_PER_RANGE} are allowed",
            rule="NG-R002",
        )
    return RangePattern(literals=tuple(literals), spans=tuple(spans))


def _span_from(match: re.Match[str], pattern: str) -> Span:
    low_text, high_text = match.group(1), match.group(2)
    low, high = int(low_text), int(high_text)
    if low > high:
        raise RangeError(
            f"span {match.group()!r} of {echo_value(pattern)} is inverted: {low} > {high}",
            rule="NG-R002",
        )
    # The low bound fixes the width: '[01-12]' is a two-digit port number, and
    # deciding the width per value would produce two naming schemes in one range.
    # A high bound that needs more digits than that simply uses more ('[01-100]'
    # ends at '100'); padding is a minimum, never a truncation.
    width = len(low_text) if low_text.startswith("0") else 1
    return Span(low=low, high=high, width=width)


# --------------------------------------------------------------------------- #
# Per-index substitution
# --------------------------------------------------------------------------- #

#: Every token that means something other than itself. Ordered so that the
#: doubled braces are only considered where a placeholder did not match.
_PLACEHOLDER_RE: Final = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|%d|%%")


def substitute(template: str, values: Sequence[str]) -> str:
    """Substitute the current span values into a per-index ``description``.

    ``{}`` and ``%d`` both stand for the *last* span — the one that varies
    fastest, which on ``GigabitEthernet1/0/[1-48]`` is the port number.
    ``{0}``, ``{1}``, … name a span by position, left to right. ``{{``, ``}}``
    and ``%%`` are the literal characters. A ``%`` that does not begin ``%d`` or
    ``%%`` is left alone, because a description is prose and may well say "50%".

    Raises:
        RangeError: A ``{...}`` placeholder is not empty and not the index of a
            span this range declares, or a brace stands on its own.
    """
    out: list[str] = []
    position = 0
    for match in _PLACEHOLDER_RE.finditer(template):
        gap = template[position : match.start()]
        _reject_lone_brace(gap, template)
        out.append(gap)
        out.append(_expand_placeholder(match, values))
        position = match.end()
    tail = template[position:]
    _reject_lone_brace(tail, template)
    return "".join((*out, tail))


def _expand_placeholder(match: re.Match[str], values: Sequence[str]) -> str:
    token = match.group()
    if token == "{{":
        return "{"
    if token == "}}":
        return "}"
    if token == "%%":
        return "%"
    if token == "%d":
        return values[-1]

    selector = match.group(1)
    if selector == "":
        return values[-1]
    if not selector.isascii() or not selector.isdigit():
        raise RangeError(
            f"'{{{selector}}}' in a range description must be empty or a span number; "
            "write '{}' for the last span or '{0}' for the first",
            rule="NG-R005",
        )
    position = int(selector)
    if position >= len(values):
        span_count = len(values)
        available = "span 0" if span_count == 1 else f"spans 0-{span_count - 1}"
        raise RangeError(
            f"'{{{selector}}}' in a range description names span {position}, but the "
            f"range declares {available}",
            rule="NG-R005",
        )
    return values[position]


def _reject_lone_brace(text: str, template: str) -> None:
    """``NG-R005`` — a brace outside a placeholder is a typo, not a literal."""
    if "{" in text or "}" in text:
        raise RangeError(
            f"unmatched brace in the range description {echo_value(template)}; write "
            "'{{' and '}}' for literal braces",
            rule="NG-R005",
        )


# --------------------------------------------------------------------------- #
# Expansion of a whole interface list
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Expansion:
    """The outcome of expanding one ``interfaces`` list."""

    #: The expanded list. Identical to the input when no entry declared a range.
    entries: list[Any]
    #: ``output path -> where it was written``, for every entry that moved.
    redirects: dict[FieldPath, Site]
    #: Problems found. A non-empty list means the document must not be built.
    issues: list[SchemaIssue]
    #: Did any entry declare a range?
    expanded: bool = False


def expand_interfaces(
    entries: Any,
    *,
    prefix: FieldPath,
    provenance: Provenance,
) -> Expansion:
    """Expand every ``range`` entry of one interface list.

    Args:
        entries: The raw value of ``spec.interfaces``. Anything that is not a
            list is returned untouched, so the models report its real type.
        prefix: Path of that value inside the document, e.g.
            ``("spec", "interfaces")``.
        provenance: Where the *input* list's entries were written. Used both to
            build the output redirects and to quote both sides of a collision.

    Returns:
        An :class:`Expansion`. Its ``issues`` are located by path in the
        **input** document, because a document with a bad range is never built.
    """
    if not isinstance(entries, list):
        return Expansion(entries=entries, redirects={}, issues=[], expanded=False)
    if not any(isinstance(entry, dict) and "range" in entry for entry in entries):
        # The overwhelmingly common case, and the one on the hot path of every
        # load: nothing moves, so nothing needs a redirect and no name can
        # collide by expansion. Two explicitly named duplicates are NG-I001.
        return Expansion(entries=entries, redirects={}, issues=[], expanded=False)

    issues: list[SchemaIssue] = []
    output: list[Any] = []
    redirects: dict[FieldPath, Site] = {}
    #: name -> path of the input entry that first claimed it, and whether that
    #: entry was a range. Only a collision involving a range is reported here;
    #: two explicitly named duplicates are ``NG-I001``, reported by the model
    #: with the wording it has always used.
    claimed: dict[str, tuple[FieldPath, bool]] = {}
    expanded = False

    for index, entry in enumerate(entries):
        source: FieldPath = (*prefix, index)
        if not isinstance(entry, dict) or "range" not in entry:
            _claim(
                claimed,
                _name_of(entry),
                source,
                is_range=False,
                issues=issues,
                provenance=provenance,
            )
            redirects[(*prefix, len(output))] = provenance.locate(source)
            output.append(entry)
            continue

        expanded = True
        produced = _expand_entry(
            entry,
            source=source,
            budget=MAX_INTERFACES_PER_DOCUMENT - len(output),
            issues=issues,
        )
        for name, item in produced:
            _claim(claimed, name, source, is_range=True, issues=issues, provenance=provenance)
            redirects[(*prefix, len(output))] = provenance.locate(source)
            output.append(item)

    if issues:
        return Expansion(entries=entries, redirects={}, issues=issues, expanded=expanded)
    if not expanded:
        # Nothing moved, so the input redirects still describe the list exactly.
        return Expansion(entries=entries, redirects={}, issues=[], expanded=False)
    return Expansion(entries=output, redirects=redirects, issues=[], expanded=True)


def _name_of(entry: Any) -> str | None:
    """The declared ``name`` of an entry, when it has a usable one."""
    if isinstance(entry, dict):
        name = entry.get("name")
        if isinstance(name, str):
            return name
    return None


def _claim(
    claimed: dict[str, tuple[FieldPath, bool]],
    name: str | None,
    source: FieldPath,
    *,
    is_range: bool,
    issues: list[SchemaIssue],
    provenance: Provenance,
) -> None:
    """``NG-R004`` — refuse a name a range already produced, or would shadow."""
    if name is None:
        return
    previous = claimed.get(name)
    if previous is None:
        claimed[name] = (source, is_range)
        return
    previous_path, previous_was_range = previous
    if not is_range and not previous_was_range:
        return  # NG-I001's business; the model says it better.
    issues.append(
        SchemaIssue(
            path=source,
            message=(
                f"interface name {name!r} is produced twice: by "
                f"{_describe(previous_path, previous_was_range, provenance)} and by "
                f"{_describe(source, is_range, provenance)}"
            ),
            rule="NG-R004",
        )
    )


def _describe(path: FieldPath, was_range: bool, provenance: Provenance) -> str:
    kind = "the range at" if was_range else "the interface at"
    return f"{kind} {provenance.locate(path)}"


def _expand_entry(
    entry: dict[str, Any],
    *,
    source: FieldPath,
    budget: int,
    issues: list[SchemaIssue],
) -> list[tuple[str, dict[str, Any]]]:
    """Expand one ``range`` entry, or record why it cannot be expanded."""
    if "name" in entry:
        issues.append(
            SchemaIssue(
                path=(*source, "range"),
                message=(
                    "an interface entry declares either 'name' or 'range', not both; "
                    "drop 'name' to expand the range"
                ),
                rule="NG-R001",
            )
        )
        return []

    try:
        pattern = parse_range(entry["range"])
    except RangeError as exc:
        issues.append(SchemaIssue(path=(*source, "range"), message=str(exc), rule=exc.rule))
        return []

    if pattern.count > budget:
        issues.append(
            SchemaIssue(
                path=(*source, "range"),
                message=(
                    f"expanding {echo_value(entry['range'])} would produce "
                    f"{pattern.count} interfaces; a document expands to at most "
                    f"{MAX_INTERFACES_PER_DOCUMENT}"
                ),
                rule="NG-R003",
            )
        )
        return []

    description = entry.get("description")
    produced: list[tuple[str, dict[str, Any]]] = []
    for name, values in pattern.expand():
        item = copy.deepcopy(entry)
        del item["range"]
        item["name"] = name
        if isinstance(description, str):
            try:
                item["description"] = substitute(description, values)
            except RangeError as exc:
                issues.append(
                    SchemaIssue(path=(*source, "description"), message=str(exc), rule=exc.rule)
                )
                return []
        produced.append((name, item))
    return produced
