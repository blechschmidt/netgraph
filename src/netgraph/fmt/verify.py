"""Proving a formatted file still means what it meant.

``netgraph fmt`` rewrites files that are the source of truth for a network, so
"it looked right in the diff" is not a standard it can be held to. Every format
is checked before it is allowed anywhere near the disk, against the *strict*
loader — the same one ``validate`` and ``render`` use, not the round-trip parser
that produced the output. A formatter checked with its own parser proves only
that it is self-consistent.

The check is :func:`meaning`, run before and after and compared:

* Each document is parsed with :mod:`netgraph.loader.documents`, so a syntax
  error the formatter introduced is caught even if nothing else is.
* A document that parses into an element or a template is validated into its
  model and serialised to JSON. Comparing models rather than raw YAML is what
  lets the formatter reorder keys and restyle scalars at all, and comparing
  their JSON makes the comparison exact instead of relying on ``__eq__`` of a
  tree of pydantic objects.
* A document that does *not* validate — an invalid fixture, a partial file
  mid-edit — falls back to comparing the raw parsed data. ``fmt`` has to work on
  a file that ``validate`` rejects, and it still may not change what that file
  says.

Comments are checked too, and separately, because they are not *meaning* — a
model comparison is blind to them by construction, so nothing above would notice
them all disappearing. A round-trip parser is the whole reason this command
exists, and "it kept the comments" is the promise ``docs/format.md`` makes
first; :func:`comments` collects them and :func:`verify` insists the set comes
back intact.

:func:`verify` returns the reason a format was refused, or ``None``. A refusal
is a bug in netgraph rather than in the user's file, so it says so.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from netgraph.errors import SchemaError
from netgraph.fmt.scalars import scalar_lines
from netgraph.loader.documents import YamlSyntaxError, parse_documents
from netgraph.models import parse_document, parse_template
from netgraph.models.element import TEMPLATE_KIND

__all__ = ["comments", "meaning", "verify"]

#: What :func:`meaning` returns for a document nothing can be made of.
_UNPARSED = "raw"


def _stable_json(value: Any) -> str:
    """``value`` as JSON with every mapping's keys sorted.

    Sorting is the whole point: this string exists to be compared against the
    same document formatted, and reordering keys is exactly what the formatter
    just did. Nothing is lost by it. A model's own key order is fixed by its
    field declarations, so sorting cannot hide a change there; the only mappings
    whose order sorting discards are the free-form ones — ``metadata.labels``, a
    template's partial ``spec`` — where YAML gives order no meaning either.
    Sequence order is left alone, because there it *is* meaning.
    """
    return json.dumps(value, sort_keys=True, default=repr)


def meaning(text: str, *, name: str) -> list[str]:
    """One string per document, capturing everything netgraph reads from it.

    Raises:
        YamlSyntaxError: ``text`` is not well-formed YAML.
    """
    path = Path(name)
    relative = PurePosixPath(path.name)
    return [
        _document_meaning(document.data)
        for document in parse_documents(text, path=path, relative=relative)
    ]


def _document_meaning(data: Any) -> str:
    """The canonical description of one parsed document."""
    model = _as_model(data)
    if model is not None:
        return f"model:{model}"
    return f"{_UNPARSED}:{_stable_json(data)}"


def _as_model(data: Any) -> str | None:
    """``data`` as its validated model's JSON, or ``None`` if it does not validate."""
    if not isinstance(data, dict):
        return None
    try:
        if data.get("kind") == TEMPLATE_KIND:
            dumped = parse_template(data).model_dump_json()
        else:
            dumped = parse_document(data).model_dump_json()
    except (SchemaError, PydanticValidationError, ValueError, TypeError):
        return None
    # Round-tripped through ``json`` rather than compared as pydantic emitted
    # it: a template's ``spec`` is a free-form dict, so its keys come out in the
    # order the document happened to use, which the formatter is entitled to
    # change. See :func:`_stable_json`.
    return _stable_json(json.loads(dumped))


def comments(text: str) -> Counter[str]:
    """How many times each whole-line comment appears in ``text``.

    Whole-line only. An end-of-line comment cannot be told from a ``#`` inside a
    quoted scalar without parsing, and quoting is something the formatter is
    entitled to change — so counting those would produce false alarms about the
    one thing this check exists to be trusted on. A comment on its own line is
    the kind that goes missing anyway: reordering moves whole lines around, it
    does not strip text off the end of one.

    A line that *begins* with ``#`` is still not a comment when it is the
    continuation of a multi-line scalar, which is why
    :func:`~netgraph.fmt.scalars.scalar_lines` is consulted rather than the text
    alone. Counting one of those produced a false "the formatter dropped a
    comment" — and so a refusal to format a perfectly legal file — for any
    document with a ``#`` at the start of a continuation line; see
    ``test_a_hash_inside_a_multiline_scalar_is_not_a_comment``.

    Counted rather than collected as a set, so that deleting one of two
    identical comments is still a difference.
    """
    candidates = [
        (number, stripped)
        for number, line in enumerate(text.splitlines())
        if (stripped := line.strip()).startswith("#")
    ]
    if not candidates:
        # Nothing to classify, so nothing to scan for. Worth the branch: this
        # runs twice per formatted file and a scan is not free.
        return Counter()
    inside = scalar_lines(text)
    return Counter(stripped for number, stripped in candidates if number not in inside)


def verify(before: str, after: str, *, name: str) -> str | None:
    """Why ``after`` may not replace ``before``, or ``None`` if it may.

    ``before`` is assumed to parse — the caller has already formatted it, which
    it could not have done otherwise.
    """
    try:
        original = meaning(before, name=name)
    except YamlSyntaxError as exc:  # pragma: no cover - the formatter parsed it first
        return f"the original could not be re-read by the strict loader: {exc}"
    try:
        formatted = meaning(after, name=name)
    except YamlSyntaxError as exc:
        return f"the formatted output is not valid YAML: {exc}"
    if len(original) != len(formatted):
        return f"formatting changed the document count from {len(original)} to {len(formatted)}"
    for index, (was, now) in enumerate(zip(original, formatted, strict=True)):
        if was != now:
            return f"formatting changed what document {index + 1} means"

    lost = comments(before) - comments(after)
    if lost:
        missing = ", ".join(sorted(lost.elements())[:3])
        return f"formatting dropped {sum(lost.values())} comment line(s), starting with: {missing}"
    return None
