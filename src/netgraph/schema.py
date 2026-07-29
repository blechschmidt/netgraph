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

import copy
import re
from typing import Any, Final

from pydantic import TypeAdapter

from netgraph.models.device import Switch
from netgraph.models.document import ELEMENT_MODELS, Element, element_model_for
from netgraph.models.element import DOCUMENT_KINDS, TEMPLATE_KIND, ElementBase
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
    MAX_ELEMENT_REF_LENGTH,
    MAX_VLAN_ID,
    MIN_VLAN_ID,
    VLAN_SET_PATTERN,
)
from netgraph.models.template import INHERIT_KEY

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


#: Name of the ``$defs`` entry for a ``kind: template`` document, and for the
#: partial device spec it carries.
_TEMPLATE_DEF: Final = "Template"
_TEMPLATE_SPEC_DEF: Final = "TemplateSpec"


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
            # A ``tunnel`` interface *may* name the underlay port its outer
            # packets leave by (§14.4); every other type may not name a parent
            # at all.
            "else": {
                "if": {"properties": {"type": {"const": "tunnel"}}, "required": ["type"]},
                "then": True,
                "else": {"not": {"required": ["parent"]}},
            },
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
# Loader sugar the models never see
# --------------------------------------------------------------------------- #

#: ``interfaces[].range``: interface-name characters interleaved with at least
#: one ``[low-high]`` span. Built from :data:`IFNAME_PATTERN` so the literal part
#: of a range is exactly what a name may hold.
_IFNAME_CHARS: Final = _unanchor(IFNAME_PATTERN).removesuffix("+")
_SPAN: Final = r"\[\d+-\d+\]"
_RANGE_PATTERN: Final = f"^{_IFNAME_CHARS}*{_SPAN}(?:{_IFNAME_CHARS}*{_SPAN})*{_IFNAME_CHARS}*$"

_RANGE_SCHEMA: Final[dict[str, Any]] = {
    "type": "string",
    "title": "range",
    "description": (
        "Declares many interfaces at once instead of `name`, by bracket "
        "expansion over one or more numeric spans: `GigabitEthernet1/0/[1-48]`, "
        "`ge-[0-1]/0/[0-3]`. Several spans expand as an odometer, rightmost "
        "fastest; the width of the low bound is the zero padding "
        "(`[01-12]` yields `01`…`12`). In `description`, `{}` and `%d` stand "
        "for the last span and `{0}`, `{1}`, … for a span by position. The "
        "loader expands the entry before anything else sees the document, so a "
        "range never reaches validation, the graph or a renderer."
    ),
    "minLength": 1,
    "maxLength": 256,
    "pattern": _RANGE_PATTERN,
}

_INHERIT_SCHEMA: Final[dict[str, Any]] = {
    "type": "string",
    "title": INHERIT_KEY,
    "description": (
        "Names a `kind: template` document whose partial spec is merged "
        "underneath this one. The device's own keys win; `interfaces` merge by "
        "`name`; any other list the device declares replaces the template's "
        "outright. Resolved with the ordinary reference rules, so a template "
        "may live in another namespace (`templates/c9200l-48p`)."
    ),
    "minLength": 1,
    "maxLength": MAX_ELEMENT_REF_LENGTH,
    "pattern": ELEMENT_REF_PATTERN,
}

_TEMPLATE_SPEC_DESCRIPTION: Final = (
    "A partial device spec. Every key of a device `spec` is allowed and none is "
    "required: what the template does not say, the device using it says. "
    "`interfaces` is where the leverage is — one `range` entry is a whole "
    "line card."
)

#: Name of the ``$defs`` entry holding the interface *fields*, with nothing
#: required beyond a name. See :func:`_split_interface`.
_PARTIAL_INTERFACE_DEF: Final = "PartialInterface"

_PARTIAL_INTERFACE_DESCRIPTION: Final = (
    "One interface, with only its identity required. This is the shape an entry "
    "takes inside a `spec` that inherits a template: the device restates the "
    "`name` and the handful of fields it overrides, and the template supplies "
    "the rest. Everywhere else `type` is required too — see `Interface`."
)


def _apply_loader_sugar(definitions: dict[str, Any], *, kind: str | None, strict: bool) -> None:
    """Add the two keys the loader consumes before the models are reached.

    ``interfaces[].range`` and ``spec.from`` are expanded and merged away in
    :mod:`netgraph.loader`, so pydantic has never heard of either and cannot
    describe them. An editor has, though — they are exactly the keys someone is
    typing when they most want completion — so they are grafted on here, with
    the same conditionals the loader enforces: an interface declares one of
    ``name`` or ``range``; a device spec that omits ``interfaces`` must inherit
    them; and an interface entry may be a partial override only inside a spec
    that has something to override.
    """
    if not _split_interface(definitions) and strict:
        raise RuntimeError("netgraph.schema: no definition Interface to add 'range' to")

    device_spec = definitions.get("DeviceSpec")
    if device_spec is None:
        if strict:
            raise RuntimeError(f"netgraph.schema: no definition DeviceSpec to add {INHERIT_KEY!r}")
        return

    device_spec["properties"][INHERIT_KEY] = dict(_INHERIT_SCHEMA)
    interfaces = device_spec["properties"].get("interfaces")
    if interfaces is not None:
        interfaces["items"] = {"$ref": f"#/$defs/{_PARTIAL_INTERFACE_DEF}"}

    if kind is None or kind == TEMPLATE_KIND:
        # Snapshot before the requirements below: a template requires nothing.
        definitions[_TEMPLATE_SPEC_DEF] = _template_spec(device_spec)

    device_spec["required"] = [
        key for key in device_spec.get("required", ()) if key != "interfaces"
    ]
    device_spec.setdefault("allOf", []).extend(
        (
            {"anyOf": [{"required": ["interfaces"]}, {"required": [INHERIT_KEY]}]},
            # Without a template underneath, there is nothing for a partial
            # entry to be partial *of*, so every interface must be complete.
            {
                "if": {"not": {"required": [INHERIT_KEY]}},
                "then": {"properties": {"interfaces": {"items": {"$ref": "#/$defs/Interface"}}}},
            },
        )
    )
    if kind is None or kind == TEMPLATE_KIND:
        definitions[_TEMPLATE_DEF] = _template_document(definitions)


def _split_interface(definitions: dict[str, Any]) -> bool:
    """Split ``Interface`` into a field list and the requirements on top of it.

    A device that inherits a template writes an interface entry naming only what
    it overrides — ``{name: Vlan99, ipv4: [10.1.99.13/24]}`` — which is a legal
    document but not a legal :class:`~netgraph.models.interface.Interface`. The
    generated definition is therefore renamed to ``PartialInterface`` and keeps
    the properties, ``additionalProperties: false`` and the "name or range" rule;
    ``Interface`` becomes a reference to it that adds back the required ``type``
    and the cross-field conditionals that only make sense once ``type`` is
    known. Nothing is duplicated, and neither shape can drift from the other.

    Returns:
        False when this schema has no ``Interface`` to split (a single-kind
        schema for ``cable``, say), which is not an error there.
    """
    generated = definitions.get("Interface")
    if generated is None:
        return False

    # ``_apply_conditionals`` put these here; they all test ``type``.
    conditionals = generated.pop("allOf", [])
    description = generated.get("description", "")

    generated["properties"]["range"] = dict(_RANGE_SCHEMA)
    generated["title"] = _PARTIAL_INTERFACE_DEF
    generated["description"] = _PARTIAL_INTERFACE_DESCRIPTION
    generated.pop("required", None)
    generated["allOf"] = [{"oneOf": [{"required": ["name"]}, {"required": ["range"]}]}]

    definitions[_PARTIAL_INTERFACE_DEF] = generated
    definitions["Interface"] = {
        "$ref": f"#/$defs/{_PARTIAL_INTERFACE_DEF}",
        "title": "Interface",
        "description": description,
        "required": ["type"],
        **({"allOf": conditionals} if conditionals else {}),
    }
    return True


def _template_spec(device_spec: dict[str, Any]) -> dict[str, Any]:
    """``TemplateSpec``: a ``DeviceSpec`` with nothing required."""
    spec = copy.deepcopy(device_spec)
    spec.pop("required", None)
    spec["title"] = _TEMPLATE_SPEC_DEF
    spec["description"] = _TEMPLATE_SPEC_DESCRIPTION
    return spec


def _template_document(definitions: dict[str, Any]) -> dict[str, Any]:
    """The ``kind: template`` envelope, borrowing a device's own ``apiVersion``."""
    donor = definitions.get(Switch.__name__, {}).get("properties", {})
    api_version = copy.deepcopy(donor.get("apiVersion", {"const": API_VERSION, "type": "string"}))
    metadata = copy.deepcopy(donor.get("metadata", {"$ref": "#/$defs/Metadata"}))
    return {
        "additionalProperties": False,
        "description": (
            "A named partial device spec. A template declares no element: it is "
            "never drawn, never listed and never validated on its own. It exists "
            "to be merged into the devices that name it in `spec.from`, and the "
            "only place it surfaces is as the source location of a field it "
            "contributed."
        ),
        "properties": {
            "apiVersion": api_version,
            "kind": {
                "const": TEMPLATE_KIND,
                "title": "kind",
                "type": "string",
                "description": f"Selects the shape of `spec`. {KIND_NOTES[TEMPLATE_KIND]}",
            },
            "metadata": metadata,
            "spec": {
                "$ref": f"#/$defs/{_TEMPLATE_SPEC_DEF}",
                "title": "spec",
                "description": "The body of a `template` document.",
            },
        },
        "required": ["apiVersion", "kind", "metadata", "spec"],
        "title": _TEMPLATE_DEF,
        "type": "object",
    }


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def build_schema(kind: str | None = None) -> dict[str, Any]:
    """Build the JSON Schema for one ``kind``, or for every kind at once.

    Args:
        kind: A document kind (``switch``, ``cable``, ``template``, ...).
            ``None`` produces the discriminated union keyed on ``kind``, which
            covers every document in an inventory tree with one schema.

    Returns:
        A JSON-serialisable JSON Schema 2020-12 document.

    Raises:
        UnknownKindError: ``kind`` is not one of
            :data:`~netgraph.models.DOCUMENT_KINDS`.
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
    _apply_loader_sugar(definitions, kind=kind, strict=strict)

    if kind == TEMPLATE_KIND:
        # The carrier model was only ever there to pull DeviceSpec and its
        # dependencies into ``$defs``; the document being described is the
        # template envelope built above. Dropping it strands DeviceSpec and
        # Interface, which nothing in a template schema refers to.
        definitions.pop(Switch.__name__, None)
        root = definitions.pop(_TEMPLATE_DEF)
        definitions = _reachable(root, definitions)
    elif kind is None:
        _register_template_branch(root)

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

    # A template has no model of its own to generate from: it is a device spec
    # with nothing required. Any device kind pulls in the same ``$defs``, so one
    # stands in as the carrier and is dropped again once they are collected.
    model = Switch if kind == TEMPLATE_KIND else element_model_for(kind)
    if model is None:
        raise UnknownKindError(
            f"unknown kind {kind!r}; expected one of {', '.join(DOCUMENT_KINDS)}"
        )
    root = model.model_json_schema(mode="validation", by_alias=True)
    definitions = dict(root.pop("$defs", {}))
    # The requested model is inlined at the root rather than put in ``$defs``;
    # naming it here lets every pass below treat both shapes identically.
    definitions[model.__name__] = root
    return root, definitions, False


def _reachable(root: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    """The subset of ``definitions`` some ``$ref`` from ``root`` can reach."""
    kept: dict[str, Any] = {}
    queue: list[Any] = [root]
    while queue:
        node = queue.pop()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str):
                name = reference.rpartition("/")[2]
                if name in definitions and name not in kept:
                    kept[name] = definitions[name]
                    queue.append(definitions[name])
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return kept


def _register_template_branch(root: dict[str, Any]) -> None:
    """Add ``template`` to the union the all-kinds schema discriminates on."""
    branches: list[dict[str, Any]] = root.setdefault("oneOf", [])
    branches.append({"$ref": f"#/$defs/{_TEMPLATE_DEF}"})
    discriminator = root.get("discriminator")
    if discriminator is None:  # pragma: no cover - pydantic always emits one
        return
    mapping: dict[str, str] = discriminator["mapping"]
    mapping[TEMPLATE_KIND] = f"#/$defs/{_TEMPLATE_DEF}"
    discriminator["mapping"] = dict(sorted(mapping.items()))


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
