"""Numbered positions declared as a count or as spans.

Two kinds have a list of identical, numbered things that nobody wants to write
out: a patch panel's positions (§15.1) and a power distribution unit's outlets
(§17.1). Both accept the same shorthand — an integer meaning "1 to *n*", or
comma-separated spans such as ``1-12,17-24`` — and both have to reject the same
mistakes, so the expansion lives here once rather than twice.

The two callers differ only in the noun, the field name, the ceiling and the
rule the error carries, which is what the keyword arguments are for. Everything
else — the width rule, the ordering, the bound checked *before* the span is
expanded — is shared, and has to be: a range that amplifies is a denial of
service in eight keystrokes, and a second implementation of the guard is a
second chance to get it wrong.
"""

from __future__ import annotations

import re
from typing import Any, Final

from netgraph.errors import echo_value
from netgraph.models.diagnostics import field_error

__all__ = [
    "MAX_POSITION_DIGITS",
    "POSITION_RANGE_PATTERN",
    "expand_positions",
    "normalise_positions",
]

#: One span of a range: ``7`` or ``1-24``, decimal, leading zeros allowed.
_SPAN_RE: Final = re.compile(r"^(\d+)(?:-(\d+))?$")

#: What a normalised range looks like, for the JSON Schema.
POSITION_RANGE_PATTERN: Final = r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$"

#: Digits one span bound may carry. Every ceiling in use is four digits, so
#: anything past this is out of range twice over; the check exists because
#: ``int`` refuses a literal of more than 4300 digits with a ``ValueError``
#: about ``sys.set_int_max_str_digits`` — true, and of no use to anybody
#: reading a patch record.
MAX_POSITION_DIGITS: Final = 10


def expand_positions(
    value: Any,
    *,
    field: str,
    rule: str,
    limit: int,
    noun: str,
    unit: str,
) -> tuple[str, ...]:
    """Expand a count-or-range shorthand into the numbers it names.

    Accepts an integer — ``24`` means 1 to 24 — or a string of comma-separated
    spans: ``1-24``, ``1-12,17-24``, ``7``. The width of a span's *low* bound is
    the width every value it produces is padded to, which is the same rule
    :mod:`netgraph.loader.ranges` applies to interface ranges, so ``01-12``
    yields ``01 … 12`` and ``1-12`` yields ``1 … 12``.

    Args:
        value: What the document wrote.
        field: The key being expanded, for the diagnostic (``ports``).
        rule: The ``NG-*`` id the error carries.
        limit: Most numbers one document may name.
        noun: What owns them, for the diagnostic (``patch panel``).
        unit: What one of them is called (``position``, ``outlet``).

    Returns:
        The numbers as written, in ascending span order.

    Raises:
        pydantic_core.PydanticCustomError: The value is not an integer or a
            string, a span is malformed or inverted, a number repeats, or the
            total exceeds ``limit``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise field_error(
            f"{field!r} must be a count such as 24 or a range such as '1-24', got "
            f"{type(value).__name__}",
            rule=rule,
        )
    if isinstance(value, int):
        if value < 1:
            raise field_error(f"{field!r} must name at least one {unit}, got {value}", rule=rule)
        value = f"1-{value}" if value > 1 else "1"

    numbers: list[str] = []
    seen: set[int] = set()
    for span in value.split(","):
        match = _SPAN_RE.match(span.strip())
        if match is None:
            raise field_error(
                f"{echo_value(span)} is not a{'n' if unit[0] in 'aeiou' else ''} {unit} or a "
                f"{unit} range; write '7', '1-24' or '1-12,17-24'",
                rule=rule,
            )
        low_text, high_text = match.group(1), match.group(2) or match.group(1)
        # Bounded before ``int``; see :data:`MAX_POSITION_DIGITS`.
        if max(len(low_text), len(high_text)) > MAX_POSITION_DIGITS:
            raise field_error(
                f"{unit} range {echo_value(span)} names a number of more than "
                f"{MAX_POSITION_DIGITS} digits; a {noun} has at most {limit} {unit}s",
                rule=rule,
            )
        low, high = int(low_text), int(high_text)
        if low > high:
            raise field_error(
                f"{unit} range {echo_value(span)} is inverted: {low} > {high}", rule=rule
            )
        # Counted before it is expanded, not after. ``1-999999999`` is eight
        # keystrokes and a billion strings, and a check that ran once the span
        # had been built would be reached only by a process that had already
        # been killed for asking. Arithmetic answers the same question for free.
        if len(numbers) + (high - low + 1) > limit:
            raise field_error(
                f"{field!r} names more than {limit} {unit}s; a {noun} that large is a typo, "
                f"and a rack of them is a document each",
                rule=rule,
            )
        width = len(low_text)
        for number in range(low, high + 1):
            if number in seen:
                raise field_error(f"{unit} {number} is declared twice by {field!r}", rule=rule)
            seen.add(number)
            numbers.append(f"{number:0{width}d}")
    return tuple(numbers)


def normalise_positions(value: Any, **kwargs: Any) -> Any:
    """Canonicalise a range to its string form, rejecting what cannot expand.

    A list or a mapping is refused by :func:`expand_positions` rather than left
    to pydantic's ``string_type`` error, so every way of getting the field wrong
    reports the same rule and the same advice. ``24`` and ``1-24`` normalise to
    one value, which is what lets two documents that mean the same panel compare
    equal.
    """
    expand_positions(value, **kwargs)
    return f"1-{value}" if isinstance(value, int) and value > 1 else str(value).replace(" ", "")
