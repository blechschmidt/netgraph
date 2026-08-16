"""``netviz validate``'s findings, in the shape ``textDocument/publishDiagnostics`` takes.

Nothing is re-derived here. The rules, the severities, the messages and the
grading a ``netviz.toml`` asks for all come from
:func:`~netviz.diagnostics.build_report`, which is what the CLI, the SARIF
output and the GitHub annotations are built from too — so what an editor
underlines and what a pull request is failed for are the same sentence about the
same line, every time.

Three things are added on the way out:

* **A range.** The report carries the line and column of the offending value;
  the token there is re-read from the buffer so the squiggle covers the value
  and stops at it (:mod:`netviz.lsp.locate`).
* **The rule as a code, with its documentation.** ``NG-C002`` is the identifier
  the specification uses and the one ``--disable`` takes, so it is what the
  editor shows, and ``codeDescription`` links to the rule's own section of
  ``docs/validation-rules.md``. Nobody should have to search for what a code
  means.
* **Enough data to fix it.** The canonical id and the finding's identity travel
  in ``data``, so ``textDocument/codeAction`` can find the finding again without
  re-running the validator or matching on the message text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from netviz.diagnostics import Diagnostic
from netviz.lsp.locate import range_at
from netviz.lsp.text import Encoding, Range
from netviz.rules import Severity
from netviz.validate import Finding

__all__ = ["DIAGNOSTIC_SOURCE", "finding_key", "to_lsp", "to_lsp_diagnostic"]

#: What the editor shows next to the message, so a user with three linters
#: running knows which one is talking.
DIAGNOSTIC_SOURCE: Final = "netviz"

#: ``DiagnosticSeverity`` (§3.17.5).
_SEVERITY: Final[Mapping[Severity, int]] = {
    Severity.ERROR: 1,
    Severity.WARNING: 2,
    Severity.INFO: 3,
}


def finding_key(finding: Finding) -> str:
    """A stable identity for one finding, carried through a code action.

    The rule, the location and the message together: two findings of one rule on
    one line are still distinguishable, and the key survives a round trip
    through the client as a plain string.
    """
    source = finding.source
    location = "-" if source is None else f"{source.relative}#{source.index}"
    return f"{finding.rule} {location} {finding.message}"


def diagnostic_key(diagnostic: Diagnostic) -> str:
    """The same identity, computed from the reported form."""
    location = "-" if diagnostic.file is None else f"{diagnostic.file}#{diagnostic.index}"
    return f"{diagnostic.rule} {location} {diagnostic.message}"


def to_lsp_diagnostic(
    diagnostic: Diagnostic, text: str, encoding: Encoding = Encoding.UTF16
) -> dict[str, Any]:
    """One diagnostic, ranged against ``text`` — the buffer it is about."""
    line = diagnostic.line if diagnostic.line is not None else 1
    column = diagnostic.column if diagnostic.column is not None else 1
    span: Range = range_at(text, line, column, encoding)
    descriptor = diagnostic.descriptor
    payload: dict[str, Any] = {
        "range": span.to_dict(),
        "severity": _SEVERITY.get(diagnostic.severity, 3),
        "source": DIAGNOSTIC_SOURCE,
        "message": diagnostic.message,
        "code": diagnostic.alias or diagnostic.rule,
        "data": {
            "rule": diagnostic.rule,
            "alias": diagnostic.alias,
            "key": diagnostic_key(diagnostic),
            "pointer": diagnostic.pointer,
            "element": diagnostic.element,
        },
    }
    if descriptor.help_uri:
        payload["codeDescription"] = {"href": descriptor.help_uri}
    return payload


def to_lsp(
    diagnostics: Iterable[Diagnostic],
    text: str,
    encoding: Encoding = Encoding.UTF16,
) -> list[dict[str, Any]]:
    """Every diagnostic of one file, in report order."""
    return [to_lsp_diagnostic(entry, text, encoding) for entry in diagnostics]


def by_file(diagnostics: Sequence[Diagnostic]) -> dict[str, list[Diagnostic]]:
    """The diagnostics grouped by the file they belong to, order preserved.

    A diagnostic with no file — one about the inventory as a whole — belongs to
    no buffer and is dropped: there is nothing for an editor to underline, and
    the CLI reports it in full.
    """
    grouped: dict[str, list[Diagnostic]] = {}
    for diagnostic in diagnostics:
        if diagnostic.file is None:
            continue
        grouped.setdefault(diagnostic.file, []).append(diagnostic)
    return grouped
