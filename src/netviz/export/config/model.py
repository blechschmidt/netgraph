"""What a generated configuration *is*, and how a dialect refuses to write one.

Every other emitter in :mod:`netviz.export` produces one string. A device
configuration does not fit that shape: netplan is one YAML file, systemd-networkd
is a ``.netdev`` and a ``.network`` per stacked interface, and wg-quick is one
file per tunnel. So the config dialects produce a *tree* — :class:`ConfigFile`
inside :class:`DeviceConfig` inside :class:`ConfigSet` — and the single-string
form the registry needs is derived from it (:meth:`ConfigSet.as_stream`) rather
than being the thing the emitters build.

Three rules are enforced by these types rather than by each dialect:

**A path is relative and stays inside the tree.** :class:`ConfigFile` paths are
the real system paths a file belongs at (``etc/netplan/10-netviz.yaml``), so
``--out DIR`` produces something an operator can copy into place. They are
therefore also the one place a name from the inventory could escape the output
directory, and :func:`safe_relative_path` is where that is stopped.

**Nothing is written for a device that was refused.** A dialect that cannot
express a field reports an :class:`Unsupported` rather than emitting a file with
the field silently dropped. The whole run then fails
(:class:`UnsupportedConfigError`), because half a configuration applied to a real
device is worse than none: the operator would have a box that is *almost* what
the inventory says, and no way to tell which half.

**A refusal names the field.** ``spec.interfaces[2].vlan``, not "VLAN
configuration" — the operator has to find it in a file, and a path they can grep
for is the difference between a diagnostic and a complaint.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from netviz.errors import NetvizError

__all__ = [
    "ConfigFile",
    "ConfigSet",
    "DeviceConfig",
    "Unsupported",
    "UnsupportedConfigError",
    "device_directory",
    "safe_relative_path",
]

#: Path segments that would leave the output directory, or name it. Element
#: names cannot hold ``/`` and the §4.1 grammar rejects a name that is only
#: dots, but a generated path is assembled from several sources and the check
#: belongs where the path is built rather than in each dialect's memory.
_UNSAFE_SEGMENTS: Final[frozenset[str]] = frozenset({"", ".", ".."})


def safe_relative_path(path: str) -> str:
    """``path`` as a relative POSIX path, or raise.

    Raises:
        ValueError: The path is absolute, holds a drive letter, or holds a
            segment that would climb out of the directory it is written under.
            Every caller builds its path from element and interface names, so
            this is a programming error rather than a user one — but it is the
            one place a hostile inventory could reach the filesystem, so it is
            checked rather than assumed.
    """
    if "\\" in path:
        # Harmless on POSIX and a separator on Windows, so a segment holding one
        # would mean two different trees depending on where the export ran.
        raise ValueError(f"{path!r} holds a backslash; generated paths are POSIX")
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError(f"{path!r} is not a relative path")
    if not pure.parts:
        raise ValueError("a generated file needs a path")
    if any(segment in _UNSAFE_SEGMENTS for segment in pure.parts):
        raise ValueError(f"{path!r} holds a path segment that would leave the output directory")
    return pure.as_posix()


def device_directory(fqn: str) -> str:
    """The directory one device's files are written under, from its name.

    The fully-qualified name is already a path — ``sites/north/core/rtr-01`` —
    so it is used as one. That keeps the output tree shaped like the inventory
    tree, which is what makes ``diff -r`` between two exports readable, and it
    cannot collide, because a fully-qualified name is unique by construction.
    """
    return safe_relative_path("/".join(segment for segment in fqn.split("/") if segment))


@dataclass(frozen=True, slots=True)
class ConfigFile:
    """One generated file: where it belongs on the device, and what is in it."""

    #: Relative POSIX path, spelled as the *device's* filesystem spells it:
    #: ``etc/netplan/10-netviz.yaml``, ``etc/frr/frr.conf``. Relative rather
    #: than absolute because it is written under an export directory first, and
    #: because an absolute path is not something to hand to ``mkdir -p``.
    path: str
    #: The whole file, newline-terminated.
    content: str

    def __post_init__(self) -> None:
        safe_relative_path(self.path)

    @property
    def name(self) -> str:
        """The last segment, for a diagnostic that has no room for the path."""
        return PurePosixPath(self.path).name


@dataclass(frozen=True, slots=True)
class Unsupported:
    """One field a dialect cannot express, and why.

    Distinct from a :class:`~netviz.export.manifest.Skip`, which records
    something the artefact merely does not carry. This is stronger: the value is
    *within* the dialect's remit, the dialect has no syntax for it, and writing
    the file without it would configure the device to behave differently from
    what the inventory declares. That is the one case where producing nothing is
    the right answer.
    """

    #: Fully-qualified name of the device.
    element: str
    #: Dotted path of the field, as the document spells it:
    #: ``spec.interfaces[2].vlan``.
    field: str
    #: One sentence naming what the dialect would have had to invent, and where
    #: to go instead.
    detail: str

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.element, self.field, self.detail)

    def __str__(self) -> str:
        return f"{self.element}: {self.field} -- {self.detail}"


class UnsupportedConfigError(NetvizError):
    """Raised when a dialect was asked to write a device it cannot describe.

    Carries every refusal of the whole run, not the first one: a configuration
    export is re-run while an inventory is being adapted, and finding out about
    the second device's problem only after fixing the first is the failure mode
    ``netviz import`` already avoids for clashing files.

    Shares its exit status with :class:`~netviz.errors.ValidationError`. From
    the operator's side both mean the same thing — the inventory says something
    netviz will not paper over.
    """

    exit_code = 4

    def __init__(self, dialect: str, refusals: Sequence[Unsupported]) -> None:
        self.dialect = dialect
        self.refusals = tuple(sorted(refusals, key=lambda entry: entry.sort_key))
        super().__init__(self._message())

    def _message(self) -> str:
        elements = sorted({refusal.element for refusal in self.refusals})
        head = (
            f"the {self.dialect!r} dialect cannot express "
            f"{len(self.refusals)} field(s) of {len(elements)} device(s); "
            "nothing was written, because a configuration missing one of these would "
            "put the device out of step with the inventory it was generated from"
        )
        return "\n".join([head, *(f"  {refusal}" for refusal in self.refusals)])


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """Everything one dialect produced for one device."""

    #: Fully-qualified name of the device.
    element: str
    #: Files, in the order the dialect wrote them, which is the order they
    #: should be read in: a ``.netdev`` before the ``.network`` that matches it.
    files: tuple[ConfigFile, ...] = ()

    @property
    def directory(self) -> str:
        """Where this device's files go under ``--out``."""
        return device_directory(self.element)

    def paths(self) -> tuple[str, ...]:
        """Every file's path, prefixed with the device's directory."""
        return tuple(f"{self.directory}/{entry.path}" for entry in self.files)


@dataclass(frozen=True, slots=True)
class ConfigSet:
    """One dialect's answer for a whole selection of devices."""

    dialect: str
    #: By fully-qualified device name, which is the canonical order every other
    #: emitter here sorts by.
    devices: tuple[DeviceConfig, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.devices)

    @property
    def file_count(self) -> int:
        return sum(len(device.files) for device in self.devices)

    def files(self) -> Iterator[tuple[str, str]]:
        """``(path relative to --out, content)`` for every file, in order."""
        for device in self.devices:
            for entry in device.files:
                yield f"{device.directory}/{entry.path}", entry.content

    def as_stream(self, marker: str) -> str:
        """The set as one document, for stdout.

        A single file is written verbatim, which is the case worth optimising
        for: ``netviz export netplan --name pc-desk`` should pipe straight
        into ``netplan apply``. Several files cannot be one file in any dialect
        here, so they are separated by a banner naming each path — the shape
        ``tail`` and ``head`` print for several files, for the same reason: the
        reader has to be able to tell where one ends.

        Args:
            marker: The dialect's comment introducer, so the banner is inert in
                the format it separates.
        """
        parts = [entry for device in self.devices for entry in device.files]
        if len(parts) == 1:
            return parts[0].content
        chunks: list[str] = []
        for path, content in self.files():
            chunks.append(f"{marker} ==> {path} <==\n")
            chunks.append(content if content.endswith("\n") else content + "\n")
        return "".join(chunks)
