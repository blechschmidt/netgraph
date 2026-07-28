"""JSON Schema (2020-12) for netgraph documents, generated from the models.

``netgraph validate`` tells you about a typo *after* you have written the file.
A JSON Schema tells you about it while you are typing: the yaml-language-server
behind VS Code, Neovim and the JetBrains IDEs offers completion, hover text and
inline errors as soon as a document declares which schema it follows. See
``docs/schema.md`` for the editor setup.

Everything mechanical comes straight out of pydantic: the field list, the
types, the enumerations, the bounds, which keys are required, and
``additionalProperties: false`` from ``extra="forbid"``. Two things pydantic
cannot supply, and this module adds:

**Prose.** :mod:`netgraph.models.fielddocs` holds one description per field —
the same table ``docs/schema-reference.md`` is built from — which is hung off
the schema as ``title`` and ``description`` so hover text is useful.

**Input shorthands.** Several models accept more on input than their fields
suggest, because a ``model_validator(mode="before")`` expands a shorthand:
``ipv4: [10.0.0.1/24]``, ``rtr:eth0`` for an endpoint, ``"10,20,100-110"`` for a
VLAN set. Pydantic generates the schema of the *expanded* shape, which would
reject documents the loader accepts, so :data:`_SHORTHANDS` widens those
definitions back to the grammar of §5 of ``docs/schema.md``. Each entry is
checked against the generated schema, so a renamed model fails loudly here
rather than silently emitting a schema that no longer covers anything.

What the schema deliberately does *not* cover is anything that needs more than
one document, or more than one field of one object: that a cable endpoint names
an element that exists, that VLAN ids are unique within a device, that an
interface carrying IPv6 has an MTU of at least 1280. Those stay with
``netgraph validate``; see ``docs/validation-rules.md``.
"""

from __future__ import annotations

import re
from typing import Any, Final

from pydantic import TypeAdapter

from netgraph.models.document import ELEMENT_MODELS, Element, element_model_for
from netgraph.models.element import KINDS, ElementBase
from netgraph.models.fielddocs import (
    DOCUMENTED_MODELS,
    FIELD_DOCS,
    KIND_NOTES,
    NONE,
    check_coverage,
)
from netgraph.models.scalars import (
    API_VERSION,
    BITRATE_PATTERN,
    ELEMENT_REF_PATTERN,
    IFNAME_PATTERN,
    MAC_PATTERNS,
    MAX_VLAN_ID,
    MIN_VLAN_ID,
    VLAN_SET_PATTERN,
)

__all__ = [
    "SCHEMA_DIALECT",
    "SCHEMA_VERSION",
    "UnknownKindError",
    "build_schema",
    "schema_id",
]

#: The JSON Schema dialect the output declares. Pydantic emits 2020-12 keywords
#: (``prefixItems``, ``$defs``, ``const``), so this is a statement of fact.
SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"

#: The schema is versioned with the documents it describes: it says what
#: ``apiVersion: netgraph.dev/v1alpha1`` means and nothing else. A future
#: ``v1beta1`` gets its own ``$id`` next to this one rather than replacing it.
SCHEMA_VERSION: Final = API_VERSION.rpartition("/")[2]

_SCHEMA_BASE_URI: Final = "https://netgraph.dev/schema"


class UnknownKindError(ValueError):
    """Raised when a schema is requested for a ``kind`` that does not exist."""


def schema_id(kind: str | None = None) -> str:
    """The ``$id`` of the schema for ``kind``, or of the all-kinds schema."""
    return f"{_SCHEMA_BASE_URI}/{SCHEMA_VERSION}/{kind or 'element'}.json"


# --------------------------------------------------------------------------- #
# Input grammars pydantic cannot see
# --------------------------------------------------------------------------- #

#: A dotted quad with a prefix length, as written in the ``10.0.0.1/24``
#: shorthand. Deliberately exact about octet and prefix ranges: an editor that
#: flags ``10.0.0.300/24`` as you type is the whole point of this file.
_V4_OCTET: Final = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4_CIDR_PATTERN: Final = rf"^{_V4_OCTET}(?:\.{_V4_OCTET}){{3}}/(?:3[0-2]|[12]?\d)$"
#: IPv6 is checked loosely — a full RFC 4291 regular expression is unreadable
#: and the model parses the address properly anyway. This catches the common
#: mistakes: a missing prefix length, an out-of-range one, stray characters.
_IPV6_CIDR_PATTERN: Final = r"^[0-9A-Fa-f:]+(?:\.\d{1,3}){0,4}/(?:12[0-8]|1[01]\d|[1-9]?\d)$"


def _unanchor(pattern: str) -> str:
    """Strip the ``^``/``$`` anchors so a pattern can be embedded in another."""
    return pattern.removeprefix("^").removesuffix("$")


#: ``device:interface``, where the device half may be fully qualified (§4.2).
#: Composed from the two name grammars so it cannot drift from either.
_IFREF_PATTERN: Final = f"^{_unanchor(ELEMENT_REF_PATTERN)}:{_unanchor(IFNAME_PATTERN)}$"

_MAC_SCHEMA: Final[dict[str, Any]] = {
    "type": "string",
    "title": "MAC address",
    "description": (
        "An EUI-48 address in colon (`00:1e:8c:00:10:01`), dash "
        "(`00-1E-8C-00-10-01`) or Cisco dotted (`001e.8c00.1001`) form, "
        "normalised to lower-case colon form on load. Quote it: an unquoted "
        "MAC can be read as a number by a YAML 1.1 parser."
    ),
    "pattern": "|".join(MAC_PATTERNS),
}

_BITRATE_SCHEMA: Final[dict[str, Any]] = {
    "title": "Bit rate",
    "description": (
        "A link rate, either a whole number of bit/s or `<number><unit>` with "
        "unit `bps`, `kbps`, `Mbps`, `Gbps` or `Tbps` (`1Gbps`). Stored in bit/s."
    ),
    "anyOf": [
        {"type": "integer", "exclusiveMinimum": 0},
        {"type": "string", "pattern": BITRATE_PATTERN},
    ],
}

_VLAN_ID_SCHEMA: Final[dict[str, Any]] = {
    "type": "integer",
    "minimum": MIN_VLAN_ID,
    "maximum": MAX_VLAN_ID,
}

_VLAN_SET_SCHEMA: Final[dict[str, Any]] = {
    "title": "VLAN set",
    "description": (
        "A set of VLAN ids: a single id (`10`), a list (`[10, 20]`), a "
        'comma-separated string with ranges (`"10,20,100-110"`), or the '
        "keywords `all` (1-4094) and `none`. Normalised to the coalesced "
        "string form on load."
    ),
    "anyOf": [
        _VLAN_ID_SCHEMA,
        {"type": "string", "pattern": VLAN_SET_PATTERN},
        {
            "type": "array",
            "items": {"anyOf": [_VLAN_ID_SCHEMA, {"type": "string", "pattern": VLAN_SET_PATTERN}]},
        },
    ],
}


# --------------------------------------------------------------------------- #
# Shorthand widening
# --------------------------------------------------------------------------- #


def _widen_interface_ref(definition: dict[str, Any]) -> dict[str, Any]:
    """``endpoints: [rtr-home:lan0]`` as well as the mapping form (§4.2)."""
    return {
        "title": "Interface reference",
        "description": (
            "A reference to one port, written as `device:interface`. The device "
            "half may be fully qualified (`sites/berlin/rack1/sw1:eth0`) to pick "
            "one of several elements sharing a short name. The mapping form "
            "`{device: ..., interface: ...}` is equivalent."
        ),
        "anyOf": [{"type": "string", "pattern": _IFREF_PATTERN}, _bare(definition)],
    }


def _widen_ipv4_address(definition: dict[str, Any]) -> dict[str, Any]:
    """``10.0.0.1/24``, or the mapping with ``prefix_length`` **or** ``netmask``."""
    mapping = _bare(definition)
    mapping["properties"]["netmask"] = {
        "type": "string",
        "title": "netmask",
        "description": (
            "The dotted-quad spelling of `prefix_length`, normalised away on "
            "load. Mutually exclusive with `prefix_length`; a non-contiguous "
            "mask is rejected (`NG-A003`)."
        ),
        "pattern": rf"^{_V4_OCTET}(?:\.{_V4_OCTET}){{3}}$",
    }
    # RFC 8344 models this as a choice, so exactly one of the two must be given.
    mapping["required"] = ["ip"]
    mapping["oneOf"] = [{"required": ["prefix_length"]}, {"required": ["netmask"]}]
    return {
        "title": "IPv4 address",
        "description": (
            "One IPv4 address, written as `10.0.0.1/24` or as a mapping with "
            "`ip` and either `prefix_length` or `netmask`."
        ),
        "anyOf": [{"type": "string", "pattern": _IPV4_CIDR_PATTERN}, mapping],
    }


def _widen_ipv6_address(definition: dict[str, Any]) -> dict[str, Any]:
    """``2001:db8::1/64``, or the mapping form. IPv6 has no netmask case."""
    return {
        "title": "IPv6 address",
        "description": (
            "One IPv6 address, written as `2001:db8::1/64` or as a mapping with "
            "`ip` and `prefix_length`. Quote it if it would otherwise start a "
            "YAML alias or flow mapping."
        ),
        "anyOf": [{"type": "string", "pattern": _IPV6_CIDR_PATTERN}, _bare(definition)],
    }


def _widen_address_family(family: int) -> Any:
    """``ipv4: [10.0.0.1/24]`` as well as ``ipv4: {addresses: [...]}`` (§6.2.3)."""

    def widen(definition: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": f"IPv{family} configuration",
            "description": (
                f"The RFC 8344 `ip:ipv{family}` container. A bare list of "
                f"addresses is shorthand for `{{addresses: [...]}}`."
            ),
            "anyOf": [
                _bare(definition),
                {"type": "array", "items": {"$ref": f"#/$defs/IPv{family}Address"}},
            ],
        }

    return widen


def _widen_vlan_set(_definition: dict[str, Any]) -> dict[str, Any]:
    """The model stores coalesced ranges; documents never write that form."""
    return dict(_VLAN_SET_SCHEMA)


#: ``$defs`` entry → replacement, for every model that accepts a shorthand.
#: Checked against the generated schema by :func:`_apply`, so renaming a model
#: without updating this table is an error rather than a silent hole.
_SHORTHANDS: Final[dict[str, Any]] = {
    "InterfaceRef": _widen_interface_ref,
    "IPv4Address": _widen_ipv4_address,
    "IPv6Address": _widen_ipv6_address,
    "IPv4Config": _widen_address_family(4),
    "IPv6Config": _widen_address_family(6),
    "VlanSet": _widen_vlan_set,
}

#: ``(definition, property)`` → the schema to use instead of what pydantic
#: inferred. These are scalars behind a ``BeforeValidator``, which pydantic can
#: only describe as "a string" or "an integer".
_SCALAR_PROPERTIES: Final[dict[tuple[str, str], dict[str, Any]]] = {
    ("Interface", "mac"): _MAC_SCHEMA,
    ("BridgeConfig", "address"): _MAC_SCHEMA,
    ("CableSpec", "speed"): _BITRATE_SCHEMA,
    ("UpstreamPort", "speed"): _BITRATE_SCHEMA,
}


# --------------------------------------------------------------------------- #
# Single-object conditionals
# --------------------------------------------------------------------------- #

#: Cross-field rules that hold within one object and need no other document.
#: They are the ones worth expressing in JSON Schema: an editor can flag
#: ``native_vlan`` on an access port the moment it is typed. Everything that
#: needs a second object stays in :mod:`netgraph.validate`.
_CONDITIONALS: Final[dict[str, list[dict[str, Any]]]] = {
    "Interface": [
        {
            "if": {"properties": {"type": {"const": "vlan"}}, "required": ["type"]},
            "then": {"required": ["parent", "vlan"]},
            "else": {"not": {"required": ["parent"]}},
        },
        {
            "if": {
                "properties": {"type": {"enum": ["lag", "bridge"]}},
                "required": ["type"],
            },
            "then": {"required": ["members"]},
            "else": {"not": {"required": ["members"]}},
        },
    ],
    "VlanConfig": [
        {
            "if": {"properties": {"mode": {"const": "access"}}, "required": ["mode"]},
            "then": {
                "allOf": [
                    {"not": {"required": ["trunk_vlans"]}},
                    {"not": {"required": ["native_vlan"]}},
                ]
            },
            "else": {
                "required": ["trunk_vlans"],
                "not": {"required": ["access_vlan"]},
            },
        },
    ],
}


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def build_schema(kind: str | None = None) -> dict[str, Any]:
    """Build the JSON Schema for one ``kind``, or for every kind at once.

    Args:
        kind: An element kind (``switch``, ``cable``, ...). ``None`` produces
            the discriminated union keyed on ``kind``, which covers every
            document in an inventory tree with one schema.

    Returns:
        A JSON-serialisable JSON Schema 2020-12 document.

    Raises:
        UnknownKindError: ``kind`` is not one of :data:`~netgraph.models.KINDS`.
        RuntimeError: The models and this module have drifted apart — a
            definition named in :data:`_SHORTHANDS` or in
            :data:`~netgraph.models.fielddocs.FIELD_DOCS` no longer exists.
    """
    check_coverage()
    root, definitions, strict = _generate(kind)

    _apply(definitions, _SHORTHANDS, strict=strict, what="shorthand")
    _apply_scalars(definitions, strict=strict)
    _apply_conditionals(definitions, strict=strict)
    _document(definitions)
    _require_kind(definitions)

    return _envelope(root, definitions, kind)


def _generate(kind: str | None) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Run pydantic, and return ``(root, every named object, strict)``.

    ``strict`` says whether every entry of the override tables must be present:
    it is true only for the all-kinds schema, which by construction reaches
    every model. A single-kind schema legitimately omits most of them.
    """
    if kind is None:
        root = TypeAdapter(Element).json_schema(mode="validation", by_alias=True)
        return root, dict(root.pop("$defs", {})), True

    model = element_model_for(kind)
    if model is None:
        raise UnknownKindError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    root = model.model_json_schema(mode="validation", by_alias=True)
    definitions = dict(root.pop("$defs", {}))
    # The requested model is inlined at the root rather than put in ``$defs``;
    # naming it here lets every pass below treat both shapes identically.
    definitions[model.__name__] = root
    return root, definitions, False


def _apply(
    definitions: dict[str, Any],
    table: dict[str, Any],
    *,
    strict: bool,
    what: str,
) -> None:
    """Replace each named definition with ``table[name](definition)``."""
    missing = sorted(name for name in table if name not in definitions)
    if missing and strict:
        raise RuntimeError(
            f"netgraph.schema: no such definition for {what} override: {', '.join(missing)}"
        )
    for name, widen in table.items():
        if name in definitions:
            definitions[name] = widen(definitions[name])


def _apply_scalars(definitions: dict[str, Any], *, strict: bool) -> None:
    """Point the scalars behind a ``BeforeValidator`` at a real grammar."""
    for (owner, key), replacement in _SCALAR_PROPERTIES.items():
        target = definitions.get(owner, {}).get("properties", {}).get(key)
        if target is None:
            if strict:
                raise RuntimeError(f"netgraph.schema: no property {owner}.{key} to override")
            continue
        _replace_value_schema(target, replacement)


def _apply_conditionals(definitions: dict[str, Any], *, strict: bool) -> None:
    for owner, rules in _CONDITIONALS.items():
        definition = definitions.get(owner)
        if definition is None:
            if strict:
                raise RuntimeError(f"netgraph.schema: no definition {owner} to constrain")
            continue
        definition.setdefault("allOf", []).extend(rules)


def _replace_value_schema(target: dict[str, Any], replacement: dict[str, Any]) -> None:
    """Swap the value schema of a property, keeping ``null`` if it was allowed.

    An optional field arrives as ``{"anyOf": [<value>, {"type": "null"}]}``; a
    required one as the value schema itself. Both must end up carrying
    ``replacement`` without losing the property's default.
    """
    members = target.get("anyOf")
    if isinstance(members, list) and any(member.get("type") == "null" for member in members):
        target["anyOf"] = [dict(replacement), {"type": "null"}]
        return
    keep: dict[str, Any] = {
        key: target[key] for key in ("default", "title", "description") if key in target
    }
    target.clear()
    target.update(replacement)
    target.update(keep)


def _bare(definition: dict[str, Any]) -> dict[str, Any]:
    """A definition without its class-level ``title`` and ``description``.

    The wrapper produced by a shorthand widener carries its own, written for the
    grammar rather than for the Python class.
    """
    return {key: value for key, value in definition.items() if key not in ("title", "description")}


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #

#: ``docs/schema.md`` is written in reStructuredText-flavoured docstrings;
#: hover text is rendered as Markdown, where ``x`` means something else.
_RST_LITERAL_RE: Final = re.compile(r"``([^`]+)``")


def _markdown(text: str) -> str:
    """Turn a docstring into the Markdown an editor tooltip renders."""
    return _RST_LITERAL_RE.sub(r"`\1`", text)


def _document(definitions: dict[str, Any]) -> None:
    """Hang :data:`FIELD_DOCS` off every property of every known definition."""
    for definition in definitions.values():
        if "description" in definition:
            definition["description"] = _markdown(str(definition["description"]))

    for model in (*DOCUMENTED_MODELS, *ELEMENT_MODELS):
        definition = definitions.get(model.__name__)
        if definition is None:
            continue
        properties = definition.get("properties", {})
        owner = "ElementBase" if issubclass(model, ElementBase) else model.__name__
        for name, field in model.model_fields.items():
            target = properties.get(field.alias or name)
            if target is None:
                continue
            target["title"] = field.alias or name
            doc = FIELD_DOCS.get((owner, name))
            if doc is not None:
                target["description"] = _describe(doc.description, doc.yang)

    _document_element_bodies(definitions)


def _describe(description: str, yang: str = NONE) -> str:
    if yang == NONE:
        return description
    return f"{description}\n\nYANG: `{yang}`"


def _document_element_bodies(definitions: dict[str, Any]) -> None:
    """``kind`` and ``spec`` are per-element, so they are described per element."""
    for model in ELEMENT_MODELS:
        definition = definitions.get(model.__name__)
        if definition is None:
            continue
        kind = str(model.model_fields["kind"].default)
        properties = definition.get("properties", {})
        if "kind" in properties:
            properties["kind"]["description"] = f"Selects the shape of `spec`. {KIND_NOTES[kind]}"
        if "spec" in properties:
            properties["spec"]["description"] = f"The body of a `{kind}` document."


def _require_kind(definitions: dict[str, Any]) -> None:
    """Make ``kind`` mandatory so the union discriminates on a present key.

    Pydantic leaves it out of ``required`` because each subclass defaults it to
    its own literal. In a document it is never optional, and without it every
    branch of the ``oneOf`` matches — which reports as "valid against more than
    one schema" instead of "kind is missing".
    """
    for model in ELEMENT_MODELS:
        definition = definitions.get(model.__name__)
        if definition is None:
            continue
        required = definition.setdefault("required", [])
        if "kind" not in required:
            required.insert(min(1, len(required)), "kind")
        definition.get("properties", {}).get("kind", {}).pop("default", None)


def _envelope(
    root: dict[str, Any], definitions: dict[str, Any], kind: str | None
) -> dict[str, Any]:
    """Wrap the generated body in the keywords that make it a schema document."""
    body: dict[str, Any]
    if kind is None:
        body = {
            "title": "netgraph element document",
            "description": _DOCUMENT_DESCRIPTION,
            "type": "object",
            "required": ["apiVersion", "kind", "metadata", "spec"],
            **{key: value for key, value in root.items() if key != "$defs"},
        }
    else:
        # ``root`` is also ``definitions[model]``; emitting it twice would make
        # the document self-referential, so the root copy is the one that stays.
        definitions = {name: value for name, value in definitions.items() if value is not root}
        body = {
            "title": f"netgraph {kind} document",
            "description": f"{_DOCUMENT_DESCRIPTION}\n\n{KIND_NOTES[kind]}",
            **{
                key: value
                for key, value in root.items()
                if key not in ("$defs", "title", "description")
            },
        }

    return {
        "$schema": SCHEMA_DIALECT,
        "$id": schema_id(kind),
        **body,
        "$defs": dict(sorted(definitions.items())),
    }


_DOCUMENT_DESCRIPTION: Final = (
    f"One netgraph element declared in YAML, `apiVersion: {API_VERSION}`. This schema is "
    "versioned with that apiVersion: it describes structure, value grammars and the "
    "cross-field rules of a single object. Rules that span documents — that a cable "
    "endpoint resolves, that names are unique, that an address is inside its subnet — are "
    "checked by `netgraph validate`."
)
