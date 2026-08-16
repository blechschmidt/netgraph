"""Parsing a single YAML document into the element model of its ``kind``.

:data:`Element` is a discriminated union on ``kind`` (§3), so one
:class:`~pydantic.TypeAdapter` turns any well-formed document into the right
model. :func:`parse_document` wraps that adapter and converts pydantic's
low-level errors into a :class:`~netviz.errors.SchemaError` carrying the field
path of every offending value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final

from pydantic import Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from netviz.errors import SchemaError, SchemaIssue, echo_value
from netviz.models.adapter import Adapter
from netviz.models.annotation import Annotation, AnnotationBase, Area, Legend, Note
from netviz.models.cable import Cable
from netviz.models.device import Computer, Firewall, Hub, Router, Server, Switch
from netviz.models.diagnostics import decode_field_error
from netviz.models.element import ANNOTATION_DOCUMENT_KINDS, DOCUMENT_KINDS, KINDS, ElementBase
from netviz.models.identity import Group, User
from netviz.models.layout import Layout
from netviz.models.patchpanel import PatchPanel
from netviz.models.pdu import Pdu
from netviz.models.template import Template
from netviz.models.testsuite import TestSuite
from netviz.models.theme import Theme
from netviz.models.tunnel import Tunnel

__all__ = [
    "ANNOTATION_MODELS",
    "ELEMENT_MODELS",
    "Element",
    "annotation_model_for",
    "element_model_for",
    "parse_annotation",
    "parse_document",
    "parse_layout",
    "parse_template",
    "parse_test_suite",
    "parse_theme",
]

#: Every concrete element model, in the order kinds are listed in §3.
ELEMENT_MODELS: Final[tuple[type[ElementBase], ...]] = (
    Switch,
    Router,
    Firewall,
    Hub,
    Computer,
    Server,
    Cable,
    Adapter,
    Tunnel,
    PatchPanel,
    Pdu,
    User,
    Group,
)

#: A parsed document. Discriminated on ``kind`` (§3).
Element = Annotated[
    Switch
    | Router
    | Firewall
    | Hub
    | Computer
    | Server
    | Cable
    | Adapter
    | Tunnel
    | PatchPanel
    | Pdu
    | User
    | Group,
    Field(discriminator="kind"),
]

#: Every annotation model (§21), in the order the kinds are listed. Not a
#: discriminated union with the elements: an annotation is a sidecar, and mixing
#: it into :data:`Element` would put a note into ``inventory.elements``, where
#: every consumer would then have to remember it is not a device.
ANNOTATION_MODELS: Final[tuple[type[AnnotationBase], ...]] = (Note, Area, Legend)

#: A parsed annotation. Discriminated on ``kind`` like :data:`Element`, and for
#: the same reason: one adapter, and a helpful error when the tag is wrong.
_AnnotationUnion = Annotated[Note | Area | Legend, Field(discriminator="kind")]

_ELEMENT_ADAPTER: Final[TypeAdapter[Element]] = TypeAdapter(Element)
_TEMPLATE_ADAPTER: Final[TypeAdapter[Template]] = TypeAdapter(Template)
_LAYOUT_ADAPTER: Final[TypeAdapter[Layout]] = TypeAdapter(Layout)
_TEST_SUITE_ADAPTER: Final[TypeAdapter[TestSuite]] = TypeAdapter(TestSuite)
_ANNOTATION_ADAPTER: Final[TypeAdapter[Annotation]] = TypeAdapter(_AnnotationUnion)
_THEME_ADAPTER: Final[TypeAdapter[Theme]] = TypeAdapter(Theme)

#: pydantic error types that map onto a schema rule of §10.
_RULE_BY_ERROR_TYPE: Final[dict[str, str]] = {
    "extra_forbidden": "NG-D005",
    "union_tag_invalid": "NG-D003",
    "union_tag_not_found": "NG-D003",
}
_PYDANTIC_PREFIXES: Final[tuple[str, ...]] = (
    "Value error, ",
    "Assertion failed, ",
)


def element_model_for(kind: str) -> type[ElementBase] | None:
    """Return the model class implementing ``kind``, or ``None``."""
    return next(
        (model for model in ELEMENT_MODELS if model.model_fields["kind"].default == kind),
        None,
    )


def annotation_model_for(kind: str) -> type[AnnotationBase] | None:
    """Return the annotation model implementing ``kind``, or ``None``."""
    return next(
        (model for model in ANNOTATION_MODELS if model.model_fields["kind"].default == kind),
        None,
    )


def parse_document(document: Any, *, source: str | None = None) -> Element:
    """Parse one YAML document into its element model.

    Args:
        document: The mapping a YAML document was loaded into.
        source: Optional provenance (``file.yaml#0``) quoted in diagnostics.

    Returns:
        The :data:`Element` model matching the document's ``kind``.

    Raises:
        SchemaError: The document is not a mapping, its ``kind`` is unknown, or
            a value does not match the schema. The error lists one
            :class:`~netviz.errors.SchemaIssue` per problem, each carrying the
            field path of the offending value.
    """
    if not isinstance(document, Mapping):
        raise SchemaError(issues=[_not_a_mapping(document)], source=source)

    _reject_unknown_kind(document, source=source)

    try:
        return _ELEMENT_ADAPTER.validate_python(dict(document))
    except PydanticValidationError as exc:
        raise SchemaError(issues=_issues_from(exc), source=source) from exc


def parse_template(document: Any, *, source: str | None = None) -> Template:
    """Parse one ``kind: template`` document (§6.6).

    Only the envelope and the *shape* of ``spec`` are checked; the partial spec
    itself is validated on each device that merges it, where its fields finally
    have a context that says what they must satisfy. See
    :mod:`netviz.models.template`.

    Raises:
        SchemaError: The document is not a mapping, or does not match the
            template envelope.
    """
    if not isinstance(document, Mapping):
        raise SchemaError(issues=[_not_a_mapping(document)], source=source)
    try:
        return _TEMPLATE_ADAPTER.validate_python(dict(document))
    except PydanticValidationError as exc:
        raise SchemaError(issues=_issues_from(exc), source=source) from exc


def parse_layout(document: Any, *, source: str | None = None) -> Layout:
    """Parse one ``kind: layout`` document (§18).

    Geometry is checked for *shape* only — a view netviz draws, coordinates
    that are finite numbers, a positive size. Whether a key names something the
    inventory still holds is not knowable from one document, so it is left to
    the validator (``NG-Y001``), which can see the whole tree and reports it as
    a warning that ``netviz layout --prune`` clears.

    Raises:
        SchemaError: The document is not a mapping, or does not match the
            layout envelope.
    """
    if not isinstance(document, Mapping):
        raise SchemaError(issues=[_not_a_mapping(document)], source=source)
    try:
        return _LAYOUT_ADAPTER.validate_python(dict(document))
    except PydanticValidationError as exc:
        raise SchemaError(issues=_issues_from(exc), source=source) from exc


def parse_theme(document: Any, *, source: str | None = None) -> Theme:
    """Parse one ``kind: theme`` document (§22.3).

    A theme is not part of an inventory — see :mod:`netviz.models.theme` — so
    this is reached from :mod:`netviz.render.theme` with a file the user named
    rather than from the tree walk. Everything a theme says is checkable from
    the one document: a selector is a set of globs and labels, and a style is a
    closed vocabulary, so there is nothing here for the semantic validator to
    finish later.

    Raises:
        SchemaError: The document is not a mapping, or does not match the theme
            envelope.
    """
    if not isinstance(document, Mapping):
        raise SchemaError(issues=[_not_a_mapping(document)], source=source)
    try:
        return _THEME_ADAPTER.validate_python(dict(document))
    except PydanticValidationError as exc:
        raise SchemaError(issues=_issues_from(exc), source=source) from exc


def parse_test_suite(document: Any, *, source: str | None = None) -> TestSuite:
    """Parse one ``kind: testsuite`` document (§20).

    Only the *shape* of each assertion is checked here — that it names a claim
    netviz can grade, and that every key it carries belongs to that claim
    (``NG-T003``). Whether ``from: pc-alice`` names anything is not knowable from
    one document, and it is not the validator's question either: an assertion
    that names nothing is a **failing test**, reported by ``netviz test``
    against the assertion's own line, rather than a broken inventory.

    Raises:
        SchemaError: The document is not a mapping, or does not match the test
            suite envelope.
    """
    if not isinstance(document, Mapping):
        raise SchemaError(issues=[_not_a_mapping(document)], source=source)
    try:
        return _TEST_SUITE_ADAPTER.validate_python(dict(document))
    except PydanticValidationError as exc:
        raise SchemaError(issues=_issues_from(exc), source=source) from exc


def parse_annotation(document: Any, *, source: str | None = None) -> Annotation:
    """Parse one ``kind: note``, ``kind: area`` or ``kind: legend`` document (§21).

    Only the *shape* is checked — that the text is text, the colour is a colour,
    the view is a view netviz draws, and that the annotation says where it
    goes. Whether an anchor or a member names anything the inventory holds is not
    knowable from one document, and is reported by the validator as ``NG-G001``,
    a **warning**: deleting a switch must not break ``netviz validate``, and an
    annotation about something that is gone simply is not drawn.

    Raises:
        SchemaError: The document is not a mapping, its ``kind`` is not one of
            the three, or a value does not match the schema.
    """
    if not isinstance(document, Mapping):
        raise SchemaError(issues=[_not_a_mapping(document)], source=source)
    kind = document.get("kind")
    if not isinstance(kind, str) or kind not in ANNOTATION_DOCUMENT_KINDS:
        raise SchemaError(
            issues=[
                SchemaIssue(
                    path=("kind",),
                    message=(
                        f"{echo_value(kind)} is not an annotation; expected one of "
                        f"{', '.join(ANNOTATION_DOCUMENT_KINDS)}"
                    ),
                    rule="NG-D003",
                )
            ],
            source=source,
        )
    try:
        return _ANNOTATION_ADAPTER.validate_python(dict(document))
    except PydanticValidationError as exc:
        raise SchemaError(issues=_issues_from(exc), source=source) from exc


def _not_a_mapping(document: Any) -> SchemaIssue:
    """``NG-D001`` — the document is a scalar or a sequence, not an envelope."""
    return SchemaIssue(
        message=(
            "a document must be a mapping with the keys apiVersion, kind, "
            f"metadata and spec, got {type(document).__name__}"
        ),
        rule="NG-D001",
    )


def _reject_unknown_kind(document: Mapping[str, Any], *, source: str | None) -> None:
    """Produce a helpful ``NG-D003`` before pydantic reports a union-tag error."""
    kind = document.get("kind")
    if kind is None:
        raise SchemaError(
            issues=[
                SchemaIssue(
                    path=("kind",),
                    message=f"'kind' is required; expected one of {', '.join(DOCUMENT_KINDS)}",
                    rule="NG-D001",
                )
            ],
            source=source,
        )
    if not isinstance(kind, str) or kind not in KINDS:
        raise SchemaError(
            issues=[
                SchemaIssue(
                    path=("kind",),
                    message=(
                        f"unknown kind {echo_value(kind)}; expected one of "
                        f"{', '.join(DOCUMENT_KINDS)}"
                    ),
                    rule="NG-D003",
                )
            ],
            source=source,
        )


def _issues_from(error: PydanticValidationError) -> list[SchemaIssue]:
    """Turn a pydantic error into one :class:`SchemaIssue` per problem."""
    issues: list[SchemaIssue] = []
    for detail in error.errors(include_url=False):
        path = _clean_path(tuple(detail["loc"]))
        message, rule, relative = decode_field_error(_strip_prefix(str(detail["msg"])))
        if rule is None:
            rule = _rule_for(str(detail["type"]), path)
        if detail["type"] == "extra_forbidden":
            message = f"unknown key {echo_value(path[-1])}" if path else "unknown key"
        issues.append(SchemaIssue(path=path + relative, message=message, rule=rule))
    return issues


def _rule_for(error_type: str, path: tuple[str | int, ...]) -> str | None:
    """Map a pydantic error onto the §10 rule it implements, when there is one."""
    if error_type == "missing" and len(path) == 1:
        return "NG-D001"
    if error_type == "literal_error" and path == ("apiVersion",):
        return "NG-D002"
    if error_type == "string_pattern_mismatch" and path == ("metadata", "name"):
        return "NG-N001"
    return _RULE_BY_ERROR_TYPE.get(error_type)


def _clean_path(loc: tuple[Any, ...]) -> tuple[str | int, ...]:
    """Strip the discriminator tag pydantic prepends inside a tagged union."""
    if loc and loc[0] in (*KINDS, *ANNOTATION_DOCUMENT_KINDS):
        loc = loc[1:]
    return tuple(part if isinstance(part, int) else str(part) for part in loc)


def _strip_prefix(message: str) -> str:
    """Drop the ``Value error, `` pydantic prepends to a validator's message."""
    for prefix in _PYDANTIC_PREFIXES:
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message
