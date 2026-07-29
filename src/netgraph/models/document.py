"""Parsing a single YAML document into the element model of its ``kind``.

:data:`Element` is a discriminated union on ``kind`` (§3), so one
:class:`~pydantic.TypeAdapter` turns any well-formed document into the right
model. :func:`parse_document` wraps that adapter and converts pydantic's
low-level errors into a :class:`~netgraph.errors.SchemaError` carrying the field
path of every offending value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final

from pydantic import Field, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from netgraph.errors import SchemaError, SchemaIssue, echo_value
from netgraph.models.adapter import Adapter
from netgraph.models.cable import Cable
from netgraph.models.device import Computer, Hub, Router, Server, Switch
from netgraph.models.diagnostics import decode_field_error
from netgraph.models.element import DOCUMENT_KINDS, KINDS, ElementBase
from netgraph.models.template import Template
from netgraph.models.tunnel import Tunnel

__all__ = [
    "ELEMENT_MODELS",
    "Element",
    "element_model_for",
    "parse_document",
    "parse_template",
]

#: Every concrete element model, in the order kinds are listed in §3.
ELEMENT_MODELS: Final[tuple[type[ElementBase], ...]] = (
    Switch,
    Router,
    Hub,
    Computer,
    Server,
    Cable,
    Adapter,
    Tunnel,
)

#: A parsed document. Discriminated on ``kind`` (§3).
Element = Annotated[
    Switch | Router | Hub | Computer | Server | Cable | Adapter | Tunnel,
    Field(discriminator="kind"),
]

_ELEMENT_ADAPTER: Final[TypeAdapter[Element]] = TypeAdapter(Element)
_TEMPLATE_ADAPTER: Final[TypeAdapter[Template]] = TypeAdapter(Template)

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
            :class:`~netgraph.errors.SchemaIssue` per problem, each carrying the
            field path of the offending value.
    """
    if not isinstance(document, Mapping):
        raise SchemaError(
            issues=[
                SchemaIssue(
                    message=(
                        "a document must be a mapping with the keys apiVersion, kind, "
                        f"metadata and spec, got {type(document).__name__}"
                    ),
                    rule="NG-D001",
                )
            ],
            source=source,
        )

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
    :mod:`netgraph.models.template`.

    Raises:
        SchemaError: The document is not a mapping, or does not match the
            template envelope.
    """
    if not isinstance(document, Mapping):
        raise SchemaError(
            issues=[
                SchemaIssue(
                    message=(
                        "a document must be a mapping with the keys apiVersion, kind, "
                        f"metadata and spec, got {type(document).__name__}"
                    ),
                    rule="NG-D001",
                )
            ],
            source=source,
        )
    try:
        return _TEMPLATE_ADAPTER.validate_python(dict(document))
    except PydanticValidationError as exc:
        raise SchemaError(issues=_issues_from(exc), source=source) from exc


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
    if loc and loc[0] in KINDS:
        loc = loc[1:]
    return tuple(part if isinstance(part, int) else str(part) for part in loc)


def _strip_prefix(message: str) -> str:
    """Drop the ``Value error, `` pydantic prepends to a validator's message."""
    for prefix in _PYDANTIC_PREFIXES:
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message
