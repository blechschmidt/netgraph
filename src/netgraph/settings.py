"""Render defaults and named profiles: the ``[render]`` and ``[profile.*]`` tables.

``netgraph render`` takes twenty-odd flags that shape the picture rather than
choose it — which layers, which format, icons, collapsing, bundling, filters,
labels. A team settles on one combination and then retypes it on every
invocation, and a wall poster, a pull-request thumbnail and an addressing view
each want a *different* combination. Both are configuration, so both belong in
the inventory's ``netgraph.toml``::

    [render]                      # defaults for every render of this inventory
    layer = "l2"
    icons = "cisco"
    group-by-namespace = true

    [profile.poster]              # inherits [render], overrides what it names
    layer = ["l1", "l2", "l3"]
    format = "html"
    title = "Campus — every layer"

    [profile.review]
    collapse-depth = 1
    bundle-links = true
    show-ips = false

selected with ``netgraph render --profile poster``.

The naming rule is one line long: **a key is the long flag without its leading
dashes**. ``--collapse-depth`` is ``collapse-depth``, ``--no-show-ips`` is
``show-ips = false``, ``--layer`` repeated is ``layer = [...]``. There is no
second vocabulary to learn and no table mapping one to the other — this module
*is* that table, and :data:`SETTINGS` is what the parser, the resolver, the
provenance report and the documentation test all read.

Precedence, from strongest to weakest:

1. an explicit command-line flag,
2. the selected ``[profile.<name>]`` block,
3. the ``[render]`` table,
4. netgraph's built-in default.

Rung 1 is the subtle one. Click cannot tell "``--depth`` absent" from
"``--depth 1`` given", because both leave ``1`` in ``ctx.params``; asking
:meth:`click.Context.get_parameter_source` can, and that is what
:func:`resolve_settings` takes as its ``given`` argument. A user who passes the
default value explicitly still beats the file, which is the only behaviour that
can be explained in one sentence.

Every value parsed here comes out in the *shape Click would have produced* — a
tuple for a repeatable option, an :class:`~netgraph.render.icons.IconTheme` for
``--icons`` — so the command bodies downstream read ``ctx.params`` and cannot
tell whether a setting arrived from the command line or from a file.
"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, NoReturn

from netgraph.errors import ConfigurationError

__all__ = [
    "PROFILE_TABLE",
    "RENDER_TABLE",
    "SETTINGS",
    "SETTINGS_BY_KEY",
    "SETTINGS_BY_PARAM",
    "Origin",
    "RenderConfig",
    "Resolution",
    "Setting",
    "describe_value",
    "parse_profiles",
    "parse_render",
    "profile_summaries",
    "resolve_settings",
]

#: The table holding the defaults for every render of this inventory.
RENDER_TABLE: Final = "render"

#: The table holding the named profiles, one sub-table per profile.
PROFILE_TABLE: Final = "profile"

_EMPTY: Final[Mapping[str, Any]] = MappingProxyType({})


# --------------------------------------------------------------------------- #
# Where a value came from, for diagnostics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Where:
    """The location a value is being parsed from, for error messages.

    Every diagnostic this module raises names the file and the key, because a
    configuration error the user cannot locate is worse than no configuration.
    """

    #: ``"/path/to/netgraph.toml: "``, or ``""`` when parsing a bare mapping.
    prefix: str = ""
    #: Dotted key, e.g. ``render.icons`` or ``profile.poster.icons``.
    key: str = ""
    #: Directory relative paths in the file resolve against.
    base: Path | None = None

    def at(self, key: str) -> _Where:
        return _Where(
            prefix=self.prefix, key=f"{self.key}.{key}" if self.key else key, base=self.base
        )

    def fail(self, message: str) -> NoReturn:
        raise ConfigurationError(f"{self.prefix}{self.key} {message}")


#: TOML's own names for the types it decodes to, so a diagnostic speaks the
#: language of the file rather than of the parser: ``got array``, not
#: ``got list``.
_TOML_TYPES: Final[Mapping[type, str]] = MappingProxyType(
    {int: "integer", float: "float", str: "string", list: "array", dict: "table"}
)


def _kind(value: Any) -> str:
    """The TOML-ish type name of ``value``, for diagnostics."""
    if isinstance(value, bool):
        # bool is a subclass of int, and "got integer" for ``true`` would be a
        # confusing thing to read.
        return "boolean"
    # A date, a time or an offset date-time falls through to its Python name,
    # which happens to be what TOML calls it too.
    return _TOML_TYPES.get(type(value), type(value).__name__)


# --------------------------------------------------------------------------- #
# Value parsers
# --------------------------------------------------------------------------- #

_Parse = Callable[[Any, _Where], Any]


def _boolean(value: Any, where: _Where) -> bool:
    if not isinstance(value, bool):
        where.fail(f"must be true or false, got {_kind(value)}")
    return value


def _text(value: Any, where: _Where) -> str:
    if not isinstance(value, str):
        where.fail(f"must be a string, got {_kind(value)}")
    return value


def _integer(*, minimum: int, maximum: int | None = None) -> _Parse:
    def parse(value: Any, where: _Where) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            where.fail(f"must be an integer, got {_kind(value)}")
        if value < minimum or (maximum is not None and value > maximum):
            bound = (
                f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
            )
            where.fail(f"must be {bound}, got {value}")
        return value

    return parse


def _sequence(item: _Parse) -> _Parse:
    """A repeatable option: a list, or a bare scalar as a one-element list.

    ``namespace = "sites/hq"`` and ``namespace = ["sites/hq"]`` mean the same
    thing. Insisting on the array for the common single-value case would be
    ceremony, and the flag it mirrors accepts one occurrence too.
    """

    def parse(value: Any, where: _Where) -> tuple[Any, ...]:
        entries = value if isinstance(value, (list, tuple)) else [value]
        return tuple(item(entry, where.at(f"[{index}]")) for index, entry in enumerate(entries))

    return parse


def _choice(choices: Callable[[], Sequence[str]], *, fold_case: bool = False) -> _Parse:
    """One of a closed set, named by the registry that owns it.

    The choices are read through a callable so a format or a layer added
    elsewhere is accepted here without a line changing, and so importing this
    module does not drag in the renderers.
    """

    def parse(value: Any, where: _Where) -> str:
        text = _text(value, where)
        allowed = choices()
        candidate = text.strip().upper() if fold_case else text.strip()
        if candidate not in allowed:
            where.fail(f"must be one of {', '.join(allowed)}, got {text!r}")
        return candidate

    return parse


def _icons(value: Any, where: _Where) -> Any:
    """``icons``: a bundled theme name, ``none``, or a directory of images.

    A relative directory is resolved against the *configuration file*, not the
    working directory: the file lives with the inventory, and a team member who
    runs ``netgraph`` from a parent folder must get the same icons.
    """
    from netgraph.errors import RenderError  # local: keeps this module import-cheap
    from netgraph.render.icons import BUNDLED_THEMES, NO_ICONS, icon_theme

    text = _text(value, where).strip()
    spec = text
    if text and text != NO_ICONS and text not in BUNDLED_THEMES and where.base is not None:
        candidate = Path(text)
        if not candidate.is_absolute():
            spec = str(where.base / candidate)
    try:
        return icon_theme(spec)
    except RenderError as exc:
        where.fail(f"is not usable: {exc}")


def _link_template(value: Any, where: _Where) -> Any:
    from netgraph.errors import RenderError
    from netgraph.render.links import LinkTemplate

    text = _text(value, where)
    try:
        return LinkTemplate.parse(text)
    except RenderError as exc:
        where.fail(f"is not usable: {exc}")


def _formats() -> Sequence[str]:
    from netgraph.render import FORMATS

    return tuple(FORMATS)


def _layer_names() -> Sequence[str]:
    from netgraph.render import Layer

    return tuple(layer.value for layer in Layer)


def _node_kinds() -> Sequence[str]:
    from netgraph.render import NODE_KINDS

    return NODE_KINDS


def _rankdirs() -> Sequence[str]:
    from netgraph.render import RANKDIRS

    return RANKDIRS


def _routing_styles() -> Sequence[str]:
    from netgraph.models.layout import ROUTING_STYLES

    return ROUTING_STYLES


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Setting:
    """One render-shaping option, in the file and on the command line."""

    #: The key in ``[render]``: the long flag without its leading dashes.
    key: str
    #: The Click parameter it feeds. A command that declares no such parameter
    #: simply does not take this setting.
    param: str
    #: Turns a TOML value into the shape Click would have produced.
    parse: _Parse
    #: One line, for ``netgraph config show`` and the documentation test.
    summary: str

    @property
    def flag(self) -> str:
        """The long option this setting mirrors."""
        return f"--{self.key}"


def _setting(key: str, param: str, parse: _Parse, summary: str) -> Setting:
    return Setting(key=key, param=param, parse=parse, summary=summary)


#: Every render-shaping option that can be defaulted from the file, in the order
#: ``netgraph config show`` prints them: what to draw, then what to summarise,
#: then how much detail to carry.
#:
#: Deliberately absent: ``--output``, ``--force`` and the one-shot arguments.
#: Those say *what this run does*, not *what this inventory's diagrams look
#: like*, and a file that could silently redirect output would be a trap.
#: ``--strict`` is absent too — it is validation, and ``[validate] strict``
#: already carries it.
SETTINGS: Final[tuple[Setting, ...]] = (
    _setting("layer", "layers", _sequence(_choice(_layer_names)), "Which views to draw."),
    _setting("format", "output_format", _choice(_formats), "Output format."),
    _setting(
        "namespace",
        "namespaces",
        _sequence(_text),
        "Keep only elements in these namespaces or below them.",
    ),
    _setting(
        "vlan",
        "vlans",
        _sequence(_integer(minimum=1, maximum=4094)),
        "Keep only elements participating in these VLANs.",
    ),
    _setting(
        "kind", "kinds", _sequence(_choice(_node_kinds)), "Keep only elements of these kinds."
    ),
    _setting("name", "names", _sequence(_text), "Keep only elements matching these globs."),
    _setting("neighbors-of", "neighbors_of", _text, "Keep only the neighbourhood of this element."),
    _setting("depth", "depth", _integer(minimum=0), "How many hops neighbors-of reaches."),
    _setting(
        "collapse",
        "collapse",
        _sequence(_text),
        "Replace these namespaces and everything under them with one node each.",
    ),
    _setting(
        "collapse-depth",
        "collapse_depth",
        _integer(minimum=1),
        "Collapse every namespace this many levels deep.",
    ),
    _setting(
        "bundle-links",
        "bundle_links",
        _boolean,
        "Draw parallel links between the same pair as one edge.",
    ),
    _setting("show-ips", "show_ips", _boolean, "Print configured IP addresses on the nodes."),
    _setting(
        "show-vlans", "show_vlans", _boolean, "Annotate nodes and links with VLAN membership."
    ),
    _setting(
        "annotations",
        "annotations",
        _boolean,
        "Draw the notes, areas and legends the inventory declares for the view.",
    ),
    _setting(
        "group-by-namespace",
        "group_by_namespace",
        _boolean,
        "Draw each namespace as a visual group.",
    ),
    _setting("icons", "icons", _icons, "Theme name or directory to draw elements as icons."),
    _setting(
        "tooltips", "tooltips", _boolean, "Carry the full detail of each element as hover text."
    ),
    _setting(
        "link-template",
        "link_template",
        _link_template,
        "URL linking each element back to the YAML that declares it.",
    ),
    _setting(
        "element-ids", "element_ids", _boolean, "Give every node, edge and group a stable id."
    ),
    _setting(
        "max-addresses",
        "max_addresses",
        _integer(minimum=0),
        "Longest address list spelled out under a node before it is abbreviated.",
    ),
    _setting(
        "rankdir",
        "rankdir",
        _choice(_rankdirs, fold_case=True),
        "Layout direction: TB, LR, BT or RL.",
    ),
    _setting(
        "routing",
        "routing",
        _choice(_routing_styles, fold_case=True),
        "How links are drawn: spline, orthogonal or straight.",
    ),
    _setting("title", "title", _text, "Caption for the diagram."),
)

SETTINGS_BY_KEY: Final[Mapping[str, Setting]] = MappingProxyType(
    {setting.key: setting for setting in SETTINGS}
)
SETTINGS_BY_PARAM: Final[Mapping[str, Setting]] = MappingProxyType(
    {setting.param: setting for setting in SETTINGS}
)


# --------------------------------------------------------------------------- #
# The parsed tables
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RenderConfig:
    """The ``[render]`` table, or one ``[profile.<name>]`` block.

    Held as a mapping of Click parameter name to parsed value rather than as
    twenty typed fields. Two reasons: a setting is then declared once — in
    :data:`SETTINGS` — instead of in a dataclass, a parser and a resolver; and
    *absence* is representable without twenty ``| None`` unions that collide
    with the settings whose own value may legitimately be ``None``.
    """

    #: Profile name; ``None`` for the ``[render]`` table itself.
    name: str | None = None
    #: Click parameter name -> parsed value. A key that is absent was not set.
    #:
    #: Spelled as a factory returning the one shared empty mapping rather than as
    #: a plain default: ``dataclasses`` on Python 3.11 refuses any default whose
    #: *class* declares no ``__hash__``, and ``mappingproxy`` only gained one in
    #: 3.12. The factory is the portable spelling and costs one call per instance
    #: that does not pass ``values`` at all.
    values: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)

    def __contains__(self, param: str) -> bool:
        return param in self.values

    def __bool__(self) -> bool:
        return bool(self.values)

    @property
    def keys(self) -> tuple[str, ...]:
        """The TOML keys this block sets, in registry order."""
        return tuple(setting.key for setting in SETTINGS if setting.param in self.values)


#: The empty ``[render]`` table, for a caller that has no configuration at all.
_NO_DEFAULTS: Final[RenderConfig] = RenderConfig()


def parse_render(
    table: Any,
    *,
    prefix: str = "",
    base: Path | None = None,
    key: str = RENDER_TABLE,
    name: str | None = None,
) -> RenderConfig:
    """Parse a ``[render]``-shaped table.

    Args:
        table: The decoded TOML table.
        prefix: ``"<file>: "``, prepended to every diagnostic.
        base: Directory relative paths resolve against.
        key: Dotted name of the table, for diagnostics.
        name: Profile name, or ``None`` for ``[render]`` itself.

    Raises:
        ConfigurationError: The table is not a table, holds an unknown key, or
            holds a value of the wrong type.
    """
    where = _Where(prefix=prefix, key=key, base=base)
    if not isinstance(table, Mapping):
        where.fail(f"must be a table, got {_kind(table)}")

    unknown = [entry for entry in table if entry not in SETTINGS_BY_KEY]
    if unknown:
        _reject_unknown(unknown, prefix=prefix, key=key)

    values: dict[str, Any] = {}
    for setting in SETTINGS:
        if setting.key in table:
            values[setting.param] = setting.parse(table[setting.key], where.at(setting.key))
    return RenderConfig(name=name, values=MappingProxyType(values))


def parse_profiles(
    table: Any, *, prefix: str = "", base: Path | None = None
) -> Mapping[str, RenderConfig]:
    """Parse the ``[profile.<name>]`` blocks.

    Raises:
        ConfigurationError: A block is not a table, is named unusably, or holds
            a key or value :func:`parse_render` rejects.
    """
    where = _Where(prefix=prefix, key=PROFILE_TABLE, base=base)
    if not isinstance(table, Mapping):
        where.fail(f"must be a table of named profiles, got {_kind(table)}")

    profiles: dict[str, RenderConfig] = {}
    for name, block in table.items():
        if not _is_profile_name(name):
            where.fail(
                f"name {name!r} is not usable; a profile name may hold letters, digits, "
                "'-', '_' and '.', and must start with a letter or a digit"
            )
        profiles[name] = parse_render(
            block,
            prefix=prefix,
            base=base,
            key=f"{PROFILE_TABLE}.{name}",
            name=name,
        )
    return MappingProxyType(profiles)


def _is_profile_name(name: str) -> bool:
    return bool(name) and name[0].isalnum() and all(c.isalnum() or c in "-_." for c in name)


def _reject_unknown(unknown: Sequence[str], *, prefix: str, key: str) -> NoReturn:
    """Fail on a key this version does not know, suggesting the likely spelling.

    An unknown key inside a known table is a typo, not a feature from the
    future: silently ignoring ``show_ips = false`` would leave the user staring
    at a diagram that keeps showing addresses with no clue why.
    """
    # snake_case and a leading dash are the two mistakes the naming rule invites
    # — "it is the flag without its dashes" is easy to half-remember — so they
    # get a suggestion instead of the full list of twenty keys.
    suggestion = unknown[0].replace("_", "-").lstrip("-")
    tail = (
        f"; did you mean {suggestion!r}?"
        if suggestion in SETTINGS_BY_KEY and suggestion != unknown[0]
        else f"; expected one of {', '.join(SETTINGS_BY_KEY)}"
    )
    raise ConfigurationError(
        f"{prefix}unknown key(s) in [{key}]: {', '.join(sorted(unknown))}{tail}"
    )


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #


class Origin(str, Enum):
    """Where a resolved value came from. Printed by ``netgraph config show``."""

    FLAG = "flag"
    PROFILE = "profile"
    FILE = "file"
    DEFAULT = "default"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Resolution:
    """One setting, its effective value, and which rung of the ladder set it."""

    setting: Setting
    value: Any
    origin: Origin
    #: The profile that supplied the value, when :attr:`origin` is a profile.
    profile: str | None = None
    #: The file that supplied it, when :attr:`origin` is the file or a profile.
    path: Path | None = None

    @property
    def source(self) -> str:
        """The provenance column: ``flag --icons``, ``profile poster``, …"""
        if self.origin is Origin.FLAG:
            return f"flag {self.setting.flag}"
        if self.origin is Origin.PROFILE:
            return f"profile {self.profile}"
        if self.origin is Origin.FILE:
            return f"file [{RENDER_TABLE}]"
        return "default"

    @property
    def display(self) -> str:
        return describe_value(self.value)


def resolve_settings(
    *,
    params: Mapping[str, Any],
    given: Container[str],
    render: RenderConfig = _NO_DEFAULTS,
    profile: RenderConfig | None = None,
    path: Path | None = None,
) -> tuple[Resolution, ...]:
    """Apply the precedence ladder to one command's parameters.

    Args:
        params: The command's parsed parameters — Click's values, defaults
            included.
        given: Parameter names the user supplied explicitly. Anything in here
            wins outright, *including* a flag given its own default value.
        render: The ``[render]`` table.
        profile: The selected ``[profile.<name>]`` block, if any.
        path: The file both came from, for the provenance report.

    Returns:
        One :class:`Resolution` per setting the command actually declares, in
        :data:`SETTINGS` order. Settings the command does not take are skipped:
        ``web`` draws no filtered graph, so ``[render] vlan`` is not silently
        pretended to apply to it.
    """
    resolutions: list[Resolution] = []
    for setting in SETTINGS:
        if setting.param not in params:
            continue
        if setting.param in given:
            resolutions.append(Resolution(setting, params[setting.param], Origin.FLAG))
        elif profile is not None and setting.param in profile:
            resolutions.append(
                Resolution(
                    setting,
                    profile.values[setting.param],
                    Origin.PROFILE,
                    profile=profile.name,
                    path=path,
                )
            )
        elif setting.param in render:
            resolutions.append(
                Resolution(setting, render.values[setting.param], Origin.FILE, path=path)
            )
        else:
            resolutions.append(Resolution(setting, params[setting.param], Origin.DEFAULT))
    return tuple(resolutions)


def describe_value(value: Any) -> str:
    """A settings value as one readable cell of ``netgraph config show``."""
    if value is None:
        return "(unset)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, frozenset, set)):
        items = sorted(value) if isinstance(value, (frozenset, set)) else list(value)
        return ", ".join(describe_value(item) for item in items) if items else "(none)"
    name = getattr(value, "name", None)  # IconTheme
    if isinstance(name, str):
        return name
    template = getattr(value, "template", None)  # LinkTemplate
    if isinstance(template, str):
        return template
    return str(value)


def profile_summaries(profiles: Mapping[str, RenderConfig]) -> Iterable[tuple[str, str]]:
    """``(name, "sets layer, icons")`` per profile, for shell completion."""
    for name, block in profiles.items():
        keys = block.keys
        yield name, ("sets " + ", ".join(keys) if keys else "inherits [render] unchanged")
