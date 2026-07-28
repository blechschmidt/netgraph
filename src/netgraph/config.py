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
from netgraph.rules import RULE_IDS, WILDCARD, Severity, resolve_rule_id

if sys.version_info >= (3, 11):  # pragma: no cover - trivial version fork
    import tomllib
else:  # pragma: no cover - trivial version fork
    import tomli as tomllib

__all__ = [
    "CONFIG_FILE_NAME",
    "Config",
    "ValidationConfig",
    "load_config",
    "parse_config",
]

#: Name of the per-inventory configuration file.
CONFIG_FILE_NAME: Final = "netgraph.toml"

#: Keys accepted inside ``[validate]``. Anything else is a typo, not a feature
#: from the future: unknown *tables* are tolerated, unknown *keys* are not.
_VALIDATE_KEYS: Final[frozenset[str]] = frozenset({"strict", "ignore", "severity"})

_EMPTY_SEVERITIES: Final[Mapping[str, Severity]] = MappingProxyType({})


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
class Config:
    """Everything ``netgraph.toml`` configures."""

    #: The file the settings came from; ``None`` when defaults are in use.
    path: Path | None = None
    #: The ``[validate]`` table.
    validation: ValidationConfig = field(default_factory=ValidationConfig)

    @property
    def is_default(self) -> bool:
        """Were the settings synthesised rather than read from a file?"""
        return self.path is None


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

    return Config(
        path=path,
        validation=ValidationConfig(
            ignore=frozenset(_parse_ignore(section.get("ignore", ()), where=where)),
            severity=MappingProxyType(_parse_severity(section.get("severity", {}), where=where)),
            strict=_parse_bool(section.get("strict", False), key="validate.strict", where=where),
        ),
    )


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
