"""Machine-readable reports for ``netgraph validate``.

``validate`` is the one command that belongs in continuous integration, so it
has to speak more than English. This module turns the two kinds of problem an
inventory can have — a :class:`~netgraph.loader.inventory.LoadError` from the
loader and a :class:`~netgraph.validate.Finding` from the semantic engine — into
one flat :class:`Diagnostic`, and renders a list of those in three shapes:

``json``
    A stable envelope (:func:`as_json`) documented in ``docs/ci.md``: tool
    version, inventory root, counts by severity, and one record per diagnostic.
``sarif``
    SARIF 2.1.0 (:func:`as_sarif`), uploadable to GitHub code scanning. Every
    rule in :data:`netgraph.rules.RULES` is described once in the tool driver,
    so a result carries only its id and the reader still gets the severity, the
    summary and a link to the write-up.
``github``
    GitHub Actions workflow commands (:func:`as_github`), which annotate the
    diff of a pull request without a code-scanning upload.

A fourth shape lives here without being one of ``validate``'s formats: JUnit XML
(:func:`as_junit`), which every CI system in existence renders as a test report.
It is written against :class:`JUnitCase` rather than against
:class:`Diagnostic`, because the thing a reader wants one test case *per* differs
by command — ``netgraph drift`` wants one per element, not one per difference —
and the escaping, the counting and the document skeleton are the parts worth
having in one place. :func:`dump_json` is shared for the same reason.

Two conventions run through all of it.

**Paths.** A diagnostic's :attr:`Diagnostic.file` is relative to the *inventory
root*, which is what the JSON envelope reports and what the human output has
always printed. SARIF and the workflow commands are read by GitHub, which
resolves paths against the *repository* root, so both prefix the file with the
inventory root's own location (:attr:`Report.prefix`). ``netgraph -i
examples/home-lab validate -F github`` therefore annotates
``examples/home-lab/cables/links.yaml``, which is the file that exists in the
checkout.

**Order.** Diagnostics are emitted sorted by file, then line, then rule id
(:attr:`Diagnostic.order`), never in discovery order. Two runs over an unchanged
inventory produce byte-identical output, so a report can be committed, diffed
and reviewed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from netgraph import __version__
from netgraph.errors import format_path
from netgraph.loader.inventory import Inventory, LoadError, namespace_of
from netgraph.rules import RULES, Rule, Severity
from netgraph.validate import Finding

__all__ = [
    "FORMATS",
    "LOAD_RULE",
    "SARIF_SCHEMA_URL",
    "SARIF_VERSION",
    "Diagnostic",
    "JUnitCase",
    "Report",
    "as_github",
    "as_json",
    "as_junit",
    "as_sarif",
    "build_report",
    "dump_json",
    "render_report",
]

#: Every value ``validate --output-format`` accepts, ``text`` first.
FORMATS: Final[tuple[str, ...]] = ("text", "json", "sarif", "github")

#: Version of the JSON envelope. Bumped only for a change that could break a
#: consumer — a new optional key does not count, a renamed one does.
JSON_SCHEMA_VERSION: Final = 1

SARIF_VERSION: Final = "2.1.0"
#: The OASIS-published schema, not the schemastore mirror and not a branch of the
#: spec repository: a ``$schema`` that can change under a reader is not a
#: reference. ``tests/fixtures/sarif-schema-2.1.0.json`` is a byte-for-byte copy
#: of this document, so what the tests check is what the log claims to be.
SARIF_SCHEMA_URL: Final = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
)

#: Where the tool itself is documented, for SARIF's ``informationUri``.
TOOL_URL: Final = "https://github.com/blechschmidt/netgraph"

#: The pseudo-rule every loader and schema problem is filed under.
#:
#: Those problems are not configurable — a document that did not parse is absent
#: from the graph, and no severity setting can make that benign — so they are
#: not in :data:`~netgraph.rules.RULES`. They still need an id, because a SARIF
#: result whose ``ruleId`` resolves to nothing in the driver loses its severity
#: and its help link in code scanning. The ``NG-*`` id the model supplied, when
#: there is one, travels in :attr:`Diagnostic.alias`.
LOAD_RULE: Final = Rule(
    "load",
    Severity.ERROR,
    "A document could not be read, parsed, or matched against the schema of its kind.",
    (),
    title="document rejected by the schema",
    # It has no heading of its own: the whole of pass 2 is its write-up.
    section="pass-2--schema",
)

#: ``Severity`` to the four levels SARIF defines.
_SARIF_LEVELS: Final[dict[Severity, str]] = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "note",
}

#: ``Severity`` to the workflow commands GitHub understands. ``info`` becomes a
#: notice, which annotates the diff without colouring the check run.
_GITHUB_COMMANDS: Final[dict[Severity, str]] = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "notice",
}


# --------------------------------------------------------------------------- #
# One problem, whatever produced it
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One problem, flattened out of a load error or a finding."""

    #: Canonical rule id — ``E002``, or ``load`` for a loader/schema problem.
    rule: str
    #: Severity after configuration, ``netgraph/ignore`` and ``--strict``.
    severity: Severity
    #: One line, naming every element involved.
    message: str
    #: Primary ``NG-*`` identifier from ``docs/schema.md`` §10, when there is one.
    alias: str | None = None
    #: Fully-qualified name of the element the problem is anchored to.
    element: str | None = None
    #: Namespace of :attr:`element` (``""`` at the inventory root).
    namespace: str | None = None
    #: ``kind`` of :attr:`element` — ``switch``, ``cable``, ….
    kind: str | None = None
    #: Source file, POSIX style, relative to the inventory root.
    file: str | None = None
    #: 0-based index of the document within :attr:`file`.
    index: int | None = None
    #: 1-based line of the offending value.
    line: int | None = None
    #: 1-based column of the offending value.
    column: int | None = None
    #: RFC 6901 pointer to the offending field inside the document, e.g.
    #: ``/spec/interfaces/0/mtu``. ``None`` for a whole-document problem.
    pointer: str | None = None

    @property
    def order(self) -> tuple[str, int, str, int, int, str]:
        """Sort key: file, then line, then rule id, then stable tie-breakers."""
        return (
            self.file or "",
            self.line if self.line is not None else -1,
            self.rule,
            self.index if self.index is not None else -1,
            self.column if self.column is not None else -1,
            self.message,
        )

    @property
    def descriptor(self) -> Rule:
        """The catalogue entry this diagnostic is filed under."""
        return _BY_ID.get(self.rule, LOAD_RULE)

    @property
    def title(self) -> str:
        """``E002 interface terminated by more than one cable``, for a heading.

        A loader problem's own id is only ever ``load``, so the ``NG-*`` alias
        leads when the model supplied one — ``NG-D005 document rejected by the
        schema`` says something; ``load document rejected by the schema`` does
        not.
        """
        rule = self.descriptor
        name = self.alias if self.rule == LOAD_RULE.id and self.alias else self.rule
        return f"{name} {rule.title}" if rule.title else name

    def as_record(self) -> dict[str, Any]:
        """This diagnostic as one entry of the ``findings`` array."""
        return {
            "rule": self.rule,
            "alias": self.alias,
            "severity": str(self.severity),
            "message": self.message,
            "element": self.element,
            "namespace": self.namespace,
            "kind": self.kind,
            "file": self.file,
            "document": self.index,
            "line": self.line,
            "column": self.column,
            "pointer": self.pointer,
            "help": self.descriptor.help_uri,
        }

    # -- construction ----------------------------------------------------

    @classmethod
    def from_load_error(cls, error: LoadError) -> Diagnostic:
        """A loader or schema problem.

        The field path is folded into the message the same way the human report
        folds it, because the model's own wording (``Input should be greater
        than or equal to 68``) says nothing about *which* input on its own.
        """
        message = error.message
        if error.field_path:
            message = f"{format_path(error.field_path)}: {message}"
        return cls(
            rule=LOAD_RULE.id,
            severity=Severity.ERROR,
            message=message,
            alias=error.rule,
            file=error.relative,
            index=error.index,
            line=error.line,
            column=error.column,
            pointer=_pointer(error.field_path),
        )

    @classmethod
    def from_finding(cls, finding: Finding, inventory: Inventory) -> Diagnostic:
        """A semantic finding, narrowed to the field that caused it.

        The location comes from :attr:`~netgraph.validate.Finding.site` when the
        loader recorded provenance, so a value inherited from a template points
        at the template's file rather than at the device that inherited it.
        """
        rule = _BY_ID.get(finding.rule)
        element = finding.element
        kind = None
        if element is not None:
            declared = inventory.get(element)
            kind = declared.kind if declared is not None else None

        source = finding.source
        site = finding.site
        # One walk of the node tree, not two: ``Site.line`` and ``Site.column``
        # each resolve the path from scratch.
        mark = site.mark if site is not None else None
        return cls(
            rule=finding.rule,
            severity=finding.severity,
            message=finding.message,
            alias=rule.alias if rule is not None else None,
            element=element,
            namespace=namespace_of(element) if element is not None else None,
            kind=kind,
            file=site.relative if site is not None else finding.file,
            index=site.index if site is not None else (None if source is None else source.index),
            line=mark[0] if mark is not None else (None if source is None else source.line),
            column=mark[1] if mark is not None else None,
            pointer=_pointer(finding.field_path),
        )


def _pointer(field_path: Sequence[str | int]) -> str | None:
    """A field path as an RFC 6901 JSON pointer, or ``None`` for the document.

    ``~`` and ``/`` inside a key are escaped as the RFC requires, so a pointer is
    safe to hand to any JSON-pointer implementation even when a document uses a
    key netgraph itself would reject.
    """
    if not field_path:
        return None
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in field_path]
    return "/" + "/".join(parts)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Report:
    """Every problem found in one inventory, in a deterministic order."""

    #: Absolute path of the inventory root the diagnostics are relative to.
    root: Path
    #: Sorted by :attr:`Diagnostic.order`.
    diagnostics: tuple[Diagnostic, ...] = ()
    #: The inventory root as GitHub should see it: relative to the directory the
    #: command ran in, ``""`` when they are the same directory. See the module
    #: docstring on paths.
    prefix: str = ""
    #: Version of the tool that produced the report.
    version: str = __version__

    @property
    def counts(self) -> dict[Severity, int]:
        """How many diagnostics of each severity, every severity present."""
        counts = dict.fromkeys(Severity, 0)
        for diagnostic in self.diagnostics:
            counts[diagnostic.severity] += 1
        return counts

    @property
    def failed(self) -> bool:
        """Does this report fail the run?"""
        return any(diagnostic.severity.is_fatal for diagnostic in self.diagnostics)

    def path_of(self, diagnostic: Diagnostic) -> str | None:
        """``diagnostic``'s file as a path from the working directory."""
        if diagnostic.file is None:
            return None
        if not self.prefix:
            return diagnostic.file
        return (PurePosixPath(self.prefix) / diagnostic.file).as_posix()


def build_report(
    inventory: Inventory,
    findings: Iterable[Finding],
    *,
    base: Path | None = None,
) -> Report:
    """Collect an inventory's load errors and findings into one sorted report.

    Args:
        inventory: The loaded tree; supplies the root, the load errors and the
            ``kind`` of every element a finding is anchored to.
        findings: What :func:`netgraph.validate.validate` returned.
        base: Directory the emitted paths should be relative to for SARIF and
            the workflow commands. Defaults to the working directory, which in
            CI is the repository root.
    """
    diagnostics = [Diagnostic.from_load_error(error) for error in inventory.errors]
    diagnostics.extend(Diagnostic.from_finding(finding, inventory) for finding in findings)
    diagnostics.sort(key=lambda diagnostic: diagnostic.order)
    root = _root_of(inventory)
    return Report(
        root=root,
        diagnostics=tuple(diagnostics),
        prefix=_prefix(root, base if base is not None else Path.cwd()),
    )


def _root_of(inventory: Inventory) -> Path:
    """The directory every :attr:`Diagnostic.file` is relative to.

    ``netgraph -i switch.yaml`` loads a single file, and the loader records that
    file as the inventory root — but the documents inside it are still reported
    as ``switch.yaml``, relative to the directory holding it. Taking the root
    literally would join the two into ``switch.yaml/switch.yaml``.
    """
    root = inventory.root
    return root.parent if root.is_file() else root


def _prefix(root: Path, base: Path) -> str:
    """``root`` seen from ``base``, POSIX style; ``""`` when they are the same.

    A root that is not under ``base`` — an absolute ``--inventory`` somewhere
    else on the filesystem, or a different drive on Windows — has no meaningful
    repository-relative form, so the paths are left alone rather than decorated
    with a ``../..`` chain no code-scanning upload could resolve.
    """
    try:
        resolved_root = root.resolve()
        resolved_base = base.resolve()
    except OSError:  # pragma: no cover - resolve() only fails on a broken cwd
        return ""
    if resolved_root == resolved_base:
        return ""
    try:
        relative = resolved_root.relative_to(resolved_base)
    except ValueError:
        return ""
    return PurePosixPath(relative).as_posix()


# --------------------------------------------------------------------------- #
# json
# --------------------------------------------------------------------------- #


def as_json(report: Report) -> dict[str, Any]:
    """The documented JSON envelope. See ``docs/ci.md`` for the contract."""
    counts = report.counts
    return {
        "schemaVersion": JSON_SCHEMA_VERSION,
        "tool": {"name": "netgraph", "version": report.version},
        "inventory": {
            "root": str(report.root),
            "prefix": report.prefix,
        },
        "summary": {
            **{str(severity): counts[severity] for severity in Severity},
            "total": len(report.diagnostics),
        },
        "failed": report.failed,
        "findings": [diagnostic.as_record() for diagnostic in report.diagnostics],
    }


# --------------------------------------------------------------------------- #
# sarif
# --------------------------------------------------------------------------- #


def as_sarif(report: Report) -> dict[str, Any]:
    """The report as a SARIF 2.1.0 log with exactly one run.

    Every rule is described once in ``runs[0].tool.driver.rules``, in catalogue
    order, whether or not it fired: a driver that only describes the rules that
    happened to trigger tells a reader nothing about what was checked.
    """
    descriptors = (*RULES, LOAD_RULE)
    index_of = {rule.id: position for position, rule in enumerate(descriptors)}
    return {
        "$schema": SARIF_SCHEMA_URL,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "netgraph",
                        "version": report.version,
                        "semanticVersion": report.version,
                        "informationUri": TOOL_URL,
                        "rules": [_sarif_rule(rule) for rule in descriptors],
                    }
                },
                "invocations": [{"executionSuccessful": not report.failed}],
                "results": [
                    _sarif_result(report, diagnostic, index_of) for diagnostic in report.diagnostics
                ],
            }
        ],
    }


def _sarif_rule(rule: Rule) -> dict[str, Any]:
    """One ``reportingDescriptor``.

    ``shortDescription`` is the heading and ``fullDescription`` the summary, so
    a code-scanning alert reads as a title plus a sentence rather than as the
    same string twice.
    """
    properties: dict[str, Any] = {
        "problem.severity": _SARIF_LEVELS[rule.severity],
        "tags": ["netgraph", str(rule.severity)],
    }
    if rule.aliases:
        # Not ``deprecatedIds``: the ``NG-*`` ids are the *specification's*
        # names for the same rule and are still accepted everywhere, so calling
        # them deprecated would be a lie a reader could act on.
        properties["aliases"] = list(rule.aliases)
    return {
        "id": rule.id,
        "name": _pascal_case(rule.title or rule.id),
        "shortDescription": {"text": rule.title or rule.summary},
        "fullDescription": {"text": rule.summary},
        "defaultConfiguration": {"level": _SARIF_LEVELS[rule.severity]},
        "helpUri": rule.help_uri,
        "properties": properties,
    }


def _sarif_result(
    report: Report, diagnostic: Diagnostic, index_of: dict[str, int]
) -> dict[str, Any]:
    """One ``result``, located when the diagnostic knows a file."""
    result: dict[str, Any] = {"ruleId": diagnostic.rule}
    position = index_of.get(diagnostic.rule)
    if position is not None:
        result["ruleIndex"] = position
    result["level"] = _SARIF_LEVELS[diagnostic.severity]
    result["message"] = {"text": diagnostic.message}
    result["partialFingerprints"] = {"netgraphFinding/v1": _fingerprint(diagnostic)}
    path = report.path_of(diagnostic)
    if path is not None:
        region: dict[str, Any] = {}
        if diagnostic.line is not None:
            region["startLine"] = diagnostic.line
            if diagnostic.column is not None:
                region["startColumn"] = diagnostic.column
        location: dict[str, Any] = {"artifactLocation": {"uri": path}}
        if region:
            location["region"] = region
        result["locations"] = [{"physicalLocation": location}]
    if diagnostic.element is not None:
        result["properties"] = {
            "element": diagnostic.element,
            "kind": diagnostic.kind,
            "namespace": diagnostic.namespace,
        }
    return result


def _fingerprint(diagnostic: Diagnostic) -> str:
    """A stable identity for one problem, for code scanning's alert tracking.

    Deliberately built from the rule, the file, the element and the pointer, and
    **not** from the line: inserting a document above a broken one must not
    close the old alert and open an identical new one.
    """
    material = "\x1f".join(
        (
            diagnostic.rule,
            diagnostic.file or "",
            diagnostic.element or "",
            diagnostic.pointer or "",
            diagnostic.message,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _pascal_case(text: str) -> str:
    """``unknown cable endpoint`` -> ``UnknownCableEndpoint``.

    SARIF wants ``name`` to be "opaque and readable"; code scanning shows it as
    the rule's title in the alert list, where a sentence-cased phrase with
    backtick-free identifiers in it reads badly.
    """
    words = "".join(character if character.isalnum() else " " for character in text).split()
    return "".join(word[:1].upper() + word[1:] for word in words) or "Rule"


# --------------------------------------------------------------------------- #
# github
# --------------------------------------------------------------------------- #


def as_github(report: Report) -> str:
    """The report as GitHub Actions workflow commands, one per line."""
    return "\n".join(_github_lines(report))


def _github_lines(report: Report) -> Iterator[str]:
    for diagnostic in report.diagnostics:
        command = _GITHUB_COMMANDS[diagnostic.severity]
        # Ordered as GitHub documents the command, so a line in a build log
        # reads the same way as the reference does.
        properties: list[tuple[str, str]] = []
        path = report.path_of(diagnostic)
        if path is not None:
            properties.append(("file", path))
            if diagnostic.line is not None:
                properties.append(("line", str(diagnostic.line)))
                if diagnostic.column is not None:
                    properties.append(("col", str(diagnostic.column)))
        properties.append(("title", diagnostic.title))
        rendered = ",".join(f"{key}={_escape_property(value)}" for key, value in properties)
        yield f"::{command} {rendered}::{_escape_data(diagnostic.message)}"


def _escape_data(text: str) -> str:
    """Escape a workflow command's message, per the Actions toolkit."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    """Escape a workflow command's property value.

    ``,`` separates properties and ``:`` ends them, so both have to go on top of
    what :func:`_escape_data` handles — otherwise a message containing a comma
    would silently invent a property.
    """
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


# --------------------------------------------------------------------------- #
# junit
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class JUnitCase:
    """One ``<testcase>``: something that was checked, and how it went.

    A case is in exactly one of three states, checked in this order: skipped
    when :attr:`skipped` is set, failed when :attr:`failure` is, passed
    otherwise. "Skipped" is what a JUnit reader already understands as *not
    run* — which is precisely what an unobservable field is — so nothing has to
    be invented to carry it.
    """

    #: Grouping shown as the suite/package column by most readers.
    classname: str
    #: The thing checked. Unique within a suite, or readers merge the rows.
    name: str
    #: One-line summary of the failure; ``None`` when the case passed.
    failure: str | None = None
    #: Body of the ``<failure>`` element — the detail behind the summary.
    detail: str = ""
    #: Why the case did not run. Takes precedence over :attr:`failure`.
    skipped: str | None = None
    #: What kind of failure this is, written as the ``type`` of ``<failure>``.
    #: Falls back to the whole report's kind when unset.
    type: str | None = None
    #: File the case is about, relative to the inventory root. GitLab links a
    #: failed case to it; every other reader ignores an attribute it does not
    #: know, so carrying it costs nothing.
    file: str | None = None
    #: 1-based line inside :attr:`file`.
    line: int | None = None

    @property
    def state(self) -> str:
        if self.skipped is not None:
            return "skipped"
        return "failed" if self.failure is not None else "passed"


def as_junit(
    suite: str,
    cases: Sequence[JUnitCase],
    *,
    properties: Mapping[str, str] = {},
    failure_type: str = "failure",
) -> str:
    """``cases`` as a JUnit XML document with exactly one suite.

    The dialect is the widely-implemented one — ``<testsuites>`` wrapping one
    ``<testsuite>``, counts as attributes, ``<failure>`` and ``<skipped>``
    children — because there is no normative schema and every CI system reads
    this shape. ``time`` is deliberately absent: netgraph reports whether an
    inventory agrees with a network, and how long that took is not a fact about
    the answer, only about the machine that computed it. A JUnit reader treats a
    missing ``time`` as zero rather than as an error.
    """
    failures = sum(1 for case in cases if case.state == "failed")
    skipped = sum(1 for case in cases if case.state == "skipped")
    attributes = (
        f'name={quoteattr(suite)} tests="{len(cases)}" failures="{failures}" '
        f'errors="0" skipped="{skipped}"'
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<testsuites {attributes}>",
        f"  <testsuite {attributes}>",
    ]
    if properties:
        lines.append("    <properties>")
        lines.extend(
            f"      <property name={quoteattr(key)} value={quoteattr(value)}/>"
            for key, value in properties.items()
        )
        lines.append("    </properties>")
    for case in cases:
        lines.extend(_junit_case(case, failure_type))
    lines.extend(["  </testsuite>", "</testsuites>", ""])
    return "\n".join(lines)


def _junit_case(case: JUnitCase, failure_type: str) -> Iterator[str]:
    head = f"    <testcase classname={quoteattr(case.classname)} name={quoteattr(case.name)}"
    if case.file is not None:
        head += f" file={quoteattr(case.file)}"
    if case.line is not None:
        head += f' line="{case.line}"'
    if case.skipped is not None:
        yield head + ">"
        yield f"      <skipped message={quoteattr(_xml_text(case.skipped))}/>"
        yield "    </testcase>"
        return
    if case.failure is None:
        yield head + "/>"
        return
    kind = case.type or failure_type
    yield head + ">"
    yield (f"      <failure message={quoteattr(_xml_text(case.failure))} type={quoteattr(kind)}>")
    for line in _xml_text(case.detail).splitlines():
        yield xml_escape(line)
    yield "      </failure>"
    yield "    </testcase>"


#: Characters XML 1.0 forbids outright. A device description read off a switch
#: can hold any of them, and a document carrying one is not merely ugly but
#: unparseable, so they are replaced rather than escaped.
_XML_FORBIDDEN: Final = dict.fromkeys([*range(0, 9), 11, 12, *range(14, 32)], "�")


def _xml_text(text: str) -> str:
    """``text`` with the code points no XML document may hold replaced."""
    return text.translate(_XML_FORBIDDEN)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def render_report(report: Report, output_format: str) -> str:
    """Serialise ``report`` in one of :data:`FORMATS` other than ``text``.

    Raises:
        ValueError: ``output_format`` is not a structured format.
    """
    if output_format == "json":
        return dump_json(as_json(report))
    if output_format == "sarif":
        return dump_json(as_sarif(report))
    if output_format == "github":
        return as_github(report)
    raise ValueError(f"not a structured report format: {output_format!r}")


def dump_json(payload: Any) -> str:
    """Pretty-printed JSON with the key order the builders chose.

    Every structured document netgraph writes goes through here, so two of them
    cannot disagree about indentation or about whether a non-ASCII device name
    survives as itself.
    """
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)


#: Catalogue lookup by canonical id, load pseudo-rule included.
_BY_ID: Final[dict[str, Rule]] = {rule.id: rule for rule in (*RULES, LOAD_RULE)}
