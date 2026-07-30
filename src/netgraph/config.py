"""Per-inventory configuration read from ``netgraph.toml``.

The file sits at the root of the inventory tree and is entirely optional; an
inventory without one behaves exactly as if it declared the defaults::

    # netgraph.toml
    [validate]
    strict = false
    ignore = ["W103", "NG-C010"]

    [validate.severity]
    E004 = "warning"

``ignore`` silences rules outright, ``severity`` re-grades them, and ``strict``
promotes every surviving warning to an error. Rules may be named by their short
id (``E004``) or by the ``NG-*`` id used in ``docs/schema.md`` §10; the two are
interchangeable (see :mod:`netgraph.rules`).

The other half of the file says how this inventory is *drawn* — a ``[render]``
table of defaults and any number of ``[profile.<name>]`` blocks that inherit
from it. Those live in :mod:`netgraph.settings`, which owns the setting
registry, the precedence ladder and the provenance report; this module only
reads the tables and hands them over.

A third, much smaller table says where this inventory's parse cache lives and
how big it may get (:class:`CacheConfig`). It is the one table that configures a
detail of *how* netgraph runs rather than what it produces, which it earns by
being the one thing a shared inventory may need to say about the machines it is
used on — a CI runner with a read-only home directory, a repository that wants
the cache inside a directory it already archives.

Unknown keys *inside* a known table are rejected rather than ignored: a
misspelt ``ingore = [...]`` that silently did nothing would be worse than a
failed run. Unknown *top-level* tables are left alone, so a configuration file
shared with a later netgraph version does not break this one.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from netgraph.errors import ConfigurationError
from netgraph.loader.cache import DEFAULT_MAX_BYTES
from netgraph.rules import RULE_IDS, WILDCARD, Severity, resolve_rule_id
from netgraph.settings import (
    PROFILE_TABLE,
    RENDER_TABLE,
    RenderConfig,
    parse_profiles,
    parse_render,
)

if sys.version_info >= (3, 11):  # pragma: no cover - trivial version fork
    import tomllib
else:  # pragma: no cover - trivial version fork
    import tomli as tomllib

__all__ = [
    "CACHE_TABLE",
    "CONFIG_FILE_NAME",
    "CacheConfig",
    "Config",
    "ValidationConfig",
    "load_config",
    "parse_cache",
    "parse_config",
]

#: Name of the per-inventory configuration file.
CONFIG_FILE_NAME: Final = "netgraph.toml"

#: Keys accepted inside ``[validate]``. Anything else is a typo, not a feature
#: from the future: unknown *tables* are tolerated, unknown *keys* are not.
_VALIDATE_KEYS: Final[frozenset[str]] = frozenset({"strict", "ignore", "severity"})

#: The table configuring the parse cache.
CACHE_TABLE: Final = "cache"

#: Keys accepted inside ``[cache]``.
_CACHE_KEYS: Final[frozenset[str]] = frozenset({"enabled", "dir", "max-size"})

#: The suffixes ``max-size`` accepts, and what each multiplies by. A cache size
#: written in bytes is unreadable and one written in gigabytes by accident is a
#: disk full, so the unit is required to be spelled unless the value is a plain
#: integer count of bytes.
_SIZE_UNITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
    }
)

_EMPTY_SEVERITIES: Final[Mapping[str, Severity]] = MappingProxyType({})

_EMPTY_PROFILES: Final[Mapping[str, RenderConfig]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    """How the semantic validator should grade and filter its findings."""

    #: Canonical ids of rules that are not reported at all. May hold
    #: :data:`~netgraph.rules.WILDCARD` to disable validation entirely.
    ignore: frozenset[str] = frozenset()
    #: Severity overrides, keyed by canonical rule id.
    severity: Mapping[str, Severity] = _EMPTY_SEVERITIES
    #: Promote every warning that survives :attr:`ignore` to an error.
    strict: bool = False

    def is_disabled(self, rule_id: str) -> bool:
        """Is ``rule_id`` silenced for this inventory?"""
        return WILDCARD in self.ignore or rule_id in self.ignore

    def severity_for(self, rule_id: str, default: Severity) -> Severity:
        """The severity to report ``rule_id`` at.

        The configured override wins over the rule's own default; ``strict``
        then promotes the result when it is a warning.
        """
        severity = self.severity.get(rule_id, default)
        if self.strict and severity is Severity.WARNING:
            return Severity.ERROR
        return severity

    def with_overrides(
        self,
        *,
        strict: bool | None = None,
        ignore: Iterable[str] = (),
    ) -> ValidationConfig:
        """A copy with command-line overrides applied on top of the file.

        Raises:
            ConfigurationError: ``ignore`` names an unknown rule.
        """
        extra = frozenset(_resolve_ids(ignore, where="--disable"))
        return replace(
            self,
            strict=self.strict if strict is None else strict,
            ignore=self.ignore | extra,
        )


@dataclass(frozen=True, slots=True)
class CacheConfig:
    """The ``[cache]`` table: where parsed documents are remembered, and how much.

    Only three things are configurable, because only three are decisions rather
    than implementation: whether the cache is used at all, where it goes, and how
    large it may grow. What is *in* it and how it is keyed is netgraph's business
    — see :mod:`netgraph.loader.cache`.
    """

    #: Use the cache. ``--no-cache`` and :data:`~netgraph.loader.cache.DISABLE_ENV_VAR`
    #: can turn this off; nothing on the command line turns it on, so an
    #: inventory that has opted out stays opted out.
    enabled: bool = True
    #: Base directory, already resolved against the configuration file.
    #: ``None`` leaves the platform's answer alone, and
    #: :data:`~netgraph.loader.cache.CACHE_DIR_ENV_VAR` outranks it either way.
    directory: Path | None = None
    #: Cap on the bytes kept for this inventory before the least recently used
    #: entries are dropped.
    max_bytes: int = DEFAULT_MAX_BYTES

    def with_overrides(self, *, no_cache: bool = False) -> CacheConfig:
        """A copy with the command line applied: ``--no-cache`` and nothing else."""
        return replace(self, enabled=self.enabled and not no_cache)


@dataclass(frozen=True, slots=True)
class Config:
    """Everything ``netgraph.toml`` configures."""

    #: The file the settings came from; ``None`` when defaults are in use.
    path: Path | None = None
    #: The ``[validate]`` table.
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    #: The ``[render]`` table: defaults for every diagram of this inventory.
    render: RenderConfig = field(default_factory=RenderConfig)
    #: The ``[profile.<name>]`` blocks, in the order the file declares them.
    profiles: Mapping[str, RenderConfig] = field(default_factory=lambda: _EMPTY_PROFILES)
    #: The ``[cache]`` table.
    cache: CacheConfig = field(default_factory=CacheConfig)

    @property
    def is_default(self) -> bool:
        """Were the settings synthesised rather than read from a file?"""
        return self.path is None

    @property
    def profile_names(self) -> tuple[str, ...]:
        """The declared profile names, in file order."""
        return tuple(self.profiles)

    def profile(self, name: str | None) -> RenderConfig | None:
        """The named profile block, or ``None`` when none was asked for.

        Raises:
            ConfigurationError: No such profile. The message lists the ones the
                file does declare — a mistyped ``--profile`` that silently
                rendered the defaults would be a diagram the user never asked
                for, and would look exactly like the one they wanted.
        """
        if name is None:
            return None
        block = self.profiles.get(name)
        if block is not None:
            return block
        where = f"{self.path}: " if self.path is not None else ""
        if not self.profiles:
            raise ConfigurationError(
                f"{where}no profile {name!r}: this inventory declares no [{PROFILE_TABLE}.<name>] "
                f"block. Add one to {CONFIG_FILE_NAME} at the inventory root"
            )
        raise ConfigurationError(
            f"{where}no profile {name!r}; this inventory declares {', '.join(self.profile_names)}"
        )


def load_config(source: Path, *, name: str = CONFIG_FILE_NAME) -> Config:
    """Read the configuration for an inventory.

    Args:
        source: The inventory root (the file ``name`` inside it is read), or a
            configuration file to read directly.
        name: File name looked for when ``source`` is a directory.

    Returns:
        The parsed configuration, or the defaults when no file exists. A
        directory without a ``netgraph.toml`` is not an error; an explicitly
        named file that is missing is.

    Raises:
        ConfigurationError: The file cannot be read, is not valid TOML, or
            holds a value this version does not understand.
    """
    path = source / name if source.is_dir() else source
    if not path.exists():
        if source.is_dir():
            return Config()
        raise ConfigurationError(f"configuration file {path} does not exist")

    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except OSError as exc:
        raise ConfigurationError(f"{path}: cannot be read: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"{path}: is not valid TOML: {exc}") from exc

    return parse_config(data, path=path)


def parse_config(data: Mapping[str, Any], *, path: Path | None = None) -> Config:
    """Build a :class:`Config` from an already-decoded TOML mapping.

    Raises:
        ConfigurationError: A value has the wrong type or names an unknown rule.
    """
    where = f"{path}: " if path is not None else ""
    section = data.get("validate", {})
    if not isinstance(section, Mapping):
        raise ConfigurationError(f"{where}'validate' must be a table, got {_kind(section)}")

    unknown = sorted(set(section) - _VALIDATE_KEYS)
    if unknown:
        known = ", ".join(sorted(_VALIDATE_KEYS))
        raise ConfigurationError(
            f"{where}unknown key(s) in [validate]: {', '.join(unknown)}; expected one of {known}"
        )

    base = path.parent if path is not None else None
    return Config(
        path=path,
        validation=ValidationConfig(
            ignore=frozenset(_parse_ignore(section.get("ignore", ()), where=where)),
            severity=MappingProxyType(_parse_severity(section.get("severity", {}), where=where)),
            strict=_parse_bool(section.get("strict", False), key="validate.strict", where=where),
        ),
        render=parse_render(data.get(RENDER_TABLE, {}), prefix=where, base=base),
        profiles=parse_profiles(data.get(PROFILE_TABLE, {}), prefix=where, base=base),
        cache=parse_cache(data.get(CACHE_TABLE, {}), where=where, base=base),
    )


def parse_cache(value: Any, *, where: str = "", base: Path | None = None) -> CacheConfig:
    """Parse the ``[cache]`` table.

    Args:
        value: The decoded table.
        where: ``"<file>: "``, prepended to every diagnostic.
        base: Directory a relative ``dir`` resolves against — the configuration
            file's own, not the working directory, because the file is committed
            and shared and a cache that landed somewhere different depending on
            where you stood would be a support question.

    Raises:
        ConfigurationError: The table holds an unknown key or a bad value.
    """
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{where}'{CACHE_TABLE}' must be a table, got {_kind(value)}")
    unknown = sorted(set(value) - _CACHE_KEYS)
    if unknown:
        known = ", ".join(sorted(_CACHE_KEYS))
        raise ConfigurationError(
            f"{where}unknown key(s) in [{CACHE_TABLE}]: {', '.join(unknown)}; "
            f"expected one of {known}"
        )

    directory: Path | None = None
    if "dir" in value:
        raw = value["dir"]
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigurationError(
                f"{where}{CACHE_TABLE}.dir must be a non-empty path string, got {_kind(raw)}"
            )
        candidate = Path(raw.strip()).expanduser()
        directory = candidate if candidate.is_absolute() or base is None else base / candidate

    return CacheConfig(
        enabled=_parse_bool(value.get("enabled", True), key=f"{CACHE_TABLE}.enabled", where=where),
        directory=directory,
        max_bytes=_parse_size(value.get("max-size", DEFAULT_MAX_BYTES), where=where),
    )


def _parse_size(value: Any, *, where: str) -> int:
    """``max-size``: a byte count, or a size with a unit — ``"256MB"``, ``"1GiB"``."""
    key = f"{CACHE_TABLE}.max-size"
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigurationError(
            f'{where}{key} must be a byte count or a size like "256MB", got {_kind(value)}'
        )
    if isinstance(value, int):
        number, unit = float(value), "b"
    else:
        text = value.strip().lower()
        digits = len(text) - len(text.lstrip("0123456789._"))
        unit = text[digits:].strip() or "b"
        try:
            number = float(text[:digits])
        except ValueError:
            raise ConfigurationError(
                f"{where}{key}: {value!r} is not a size; expected a byte count or a number "
                f"followed by one of {', '.join(sorted(_SIZE_UNITS))}"
            ) from None
    if unit not in _SIZE_UNITS:
        raise ConfigurationError(
            f"{where}{key}: {value!r} names no unit netgraph knows; "
            f"expected one of {', '.join(sorted(_SIZE_UNITS))}"
        )
    size = int(number * _SIZE_UNITS[unit])
    if size < 0:
        raise ConfigurationError(f"{where}{key} cannot be negative, got {value!r}")
    return size


def _parse_ignore(value: Any, *, where: str) -> frozenset[str]:
    """Accept ``ignore = "E001"`` as well as ``ignore = ["E001", "W103"]``."""
    if isinstance(value, str):
        tokens: list[str] = [value]
    elif isinstance(value, (list, tuple)):
        tokens = []
        for index, entry in enumerate(value):
            if not isinstance(entry, str):
                raise ConfigurationError(
                    f"{where}validate.ignore[{index}] must be a rule id string, got {_kind(entry)}"
                )
            tokens.append(entry)
    else:
        raise ConfigurationError(
            f"{where}validate.ignore must be a rule id or a list of rule ids, got {_kind(value)}"
        )
    return frozenset(_resolve_ids(tokens, where=f"{where}validate.ignore"))


def _parse_severity(value: Any, *, where: str) -> dict[str, Severity]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(
            f"{where}[validate.severity] must be a table of rule id = severity, got {_kind(value)}"
        )
    overrides: dict[str, Severity] = {}
    for key, raw in value.items():
        rule_id = _resolve_id(key, where=f"{where}validate.severity")
        if rule_id == WILDCARD:
            raise ConfigurationError(
                f"{where}validate.severity: the wildcard '{WILDCARD}' cannot be re-graded; "
                "name the rules individually"
            )
        if not isinstance(raw, str):
            raise ConfigurationError(
                f"{where}validate.severity.{key} must be one of "
                f"{_severity_choices()}, got {_kind(raw)}"
            )
        try:
            overrides[rule_id] = Severity(raw.strip().lower())
        except ValueError:
            raise ConfigurationError(
                f"{where}validate.severity.{key}: {raw!r} is not a severity; "
                f"expected one of {_severity_choices()}"
            ) from None
    return overrides


def _parse_bool(value: Any, *, key: str, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{where}{key} must be true or false, got {_kind(value)}")
    return value


def _resolve_ids(tokens: Iterable[str], *, where: str) -> list[str]:
    return [_resolve_id(token, where=where) for token in tokens]


def _resolve_id(token: str, *, where: str) -> str:
    """Normalise one rule id, failing loudly on a typo.

    Configuration is tooling, not data: a suppression that names no rule is
    almost always a mistake, and silently keeping the finding would send the
    user hunting for a setting that never applied.
    """
    try:
        return resolve_rule_id(token)
    except KeyError:
        raise ConfigurationError(
            f"{where}: {token!r} is not a known rule id; expected one of "
            f"{', '.join(RULE_IDS)}, an NG-* alias from docs/schema.md §10, or '{WILDCARD}'"
        ) from None


def _severity_choices() -> str:
    return ", ".join(repr(severity.value) for severity in Severity)


def _kind(value: Any) -> str:
    """The TOML-ish type name of ``value``, for diagnostics."""
    return type(value).__name__
