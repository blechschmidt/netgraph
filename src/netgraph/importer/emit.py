"""Turning a :class:`~netgraph.importer.draft.Draft` into YAML somebody wants to edit.

The output of an importer is not an artefact; it is the *first draft of an
inventory* that a human is about to correct, extend and commit. That makes the
formatting part of the job rather than a detail of it, and three rules follow:

* **Field order is the order of ``docs/schema.md``.** A reader who has the
  specification open finds the fields where the tables put them, and two
  documents written by this module never differ in layout — so a re-import after
  more captures produces a diff about the network, not about the emitter.
* **Every inference is a comment next to the value it explains.** ``kind:
  computer`` on its own is a claim; ``kind: computer`` under a line saying no
  input stated a role is a question addressed to the reader. Comments are how
  this command distinguishes "observed" from "concluded" without inventing a
  field to say so.
* **Nothing is written that was not derived from an input.** There is no
  placeholder ``vendor:``, no ``description: TODO``, no example address. What
  the capture did not cover is either absent or noted in a comment.

The YAML is generated as text rather than dumped from objects, for the same
reason :mod:`netgraph.scaffold` does: a dumper cannot interleave comments, and
the comments are half the point. :func:`scalar` is the one place quoting is
decided, and ``tests/test_import.py`` round-trips every emitted document through
the real loader to prove that decision right.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Final

from netgraph.errors import clip_text
from netgraph.importer.draft import Draft, DraftCable, DraftDevice, DraftInterface
from netgraph.models import API_VERSION
from netgraph.scaffold import SCHEMA_FILE_NAME

__all__ = ["CABLES_FILE", "DEVICES_DIR", "render_cables", "render_device", "render_draft", "scalar"]

#: Where devices and cables go, matching the tree ``netgraph init`` writes.
DEVICES_DIR: Final = "devices"
CABLES_FILE: Final = "cables/links.yaml"

#: Width comments are wrapped to, before their indent and ``# `` prefix. Chosen
#: so the longest line of a generated document fits the 100-column limit the
#: repository lints Python to, with room for a deeply indented interface.
_COMMENT_WIDTH: Final = 84

#: Backstop on the length of one comment, before wrapping. Long enough that no
#: explanation this module writes is ever cut, short enough that a value that
#: escaped :func:`~netgraph.importer.draft.comment_text` cannot turn a document
#: into a wall of text.
MAX_COMMENT_LINE: Final = 600

#: A string that may be written as a YAML plain scalar. The first character is
#: restricted to alphanumerics, which rules out every YAML indicator in one go;
#: ``:`` is allowed after it because ``00:1e:8c:aa:00:01`` and ``2001:db8::1/64``
#: are far more readable unquoted and neither can start a mapping without the
#: space that this pattern already forbids.
_PLAIN_SAFE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+@:-]*$")

#: Plain scalars YAML 1.1 would resolve to something other than a string.
#: PyYAML implements 1.1, so these have to be quoted to survive a round trip.
_RESERVED_WORDS: Final[frozenset[str]] = frozenset(
    {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "none", "~"}
)
#: The YAML 1.1 sexagesimal integer, e.g. ``12:30:45``. A MAC never matches it
#: (its octets are two hex digits and the tail groups may not exceed 59), but a
#: port name or a label could.
_SEXAGESIMAL: Final = re.compile(r"^[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+$")


# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #


def scalar(value: Any) -> str:
    """``value`` as a YAML scalar, quoted only when it has to be.

    JSON's string form is a subset of YAML's double-quoted form, so
    :func:`json.dumps` is a correct — and escape-complete — fallback for
    everything the plain form cannot carry.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if (
        _PLAIN_SAFE.match(text)
        and text.lower() not in _RESERVED_WORDS
        and not _SEXAGESIMAL.match(text)
        and not _is_number(text)
    ):
        return text
    return json.dumps(text, ensure_ascii=False)


def _is_number(text: str) -> bool:
    """Would YAML read this plain scalar as a number rather than as a string?"""
    try:
        float(text)
    except ValueError:
        return False
    return True


def _flow(values: Sequence[Any]) -> str:
    return "[" + ", ".join(scalar(value) for value in values) + "]"


# --------------------------------------------------------------------------- #
# Line building
# --------------------------------------------------------------------------- #


class _Document:
    """A YAML document under construction, as indented lines."""

    __slots__ = ("_lines",)

    def __init__(self) -> None:
        self._lines: list[str] = []

    def line(self, text: str, *, indent: int = 0) -> None:
        self._lines.append(f"{' ' * indent}{text}" if text else "")

    def comment(self, text: str, *, indent: int = 0) -> None:
        """One comment, wrapped so a long explanation stays readable.

        Values taken from an input are bounded by :func:`comment_text` where
        they are interpolated, so the ceiling here is only a backstop against a
        pathological one that slipped through — not a limit the explanations
        themselves are expected to respect.
        """
        prefix = f"{' ' * indent}# "
        width = max(_COMMENT_WIDTH - indent, 30)
        body = clip_text(" ".join(text.split()), limit=MAX_COMMENT_LINE)
        for chunk in textwrap.wrap(body, width=width) or [""]:
            self._lines.append(f"{prefix}{chunk}".rstrip())

    def comments(self, texts: Iterable[str], *, indent: int = 0) -> None:
        for text in texts:
            self.comment(text, indent=indent)

    def field(self, key: str, value: Any, *, indent: int = 0) -> None:
        self.line(f"{key}: {scalar(value)}", indent=indent)

    def optional(self, key: str, value: Any, *, indent: int = 0) -> None:
        if value is not None:
            self.field(key, value, indent=indent)

    def text(self) -> str:
        return "\n".join(self._lines).rstrip("\n") + "\n"


def _header(document: _Document, lines: Sequence[str]) -> None:
    for index, paragraph in enumerate(lines):
        if index:
            document.line("#")
        document.comment(paragraph)


def _modeline(*, schema: bool) -> str:
    """The ``yaml-language-server`` reference a generated document carries.

    Every file this module writes sits one directory below the inventory root —
    ``devices/`` or ``cables/`` — so the relative reference is the same for all
    of them and there is nothing to compute per file.
    """
    return f"# yaml-language-server: $schema=../{SCHEMA_FILE_NAME}\n" if schema else ""


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #

_DEVICE_HEADER: Final[tuple[str, ...]] = (
    "Written by 'netgraph import'. This is a first draft to edit and commit, not an "
    "artefact to regenerate.",
    "Every value below was reported by the device. A line marked 'inferred:' is "
    "netgraph's reading of what was reported and is worth checking first; nothing was "
    "invented, so what the capture does not cover is absent or noted here rather than "
    "filled in with a plausible value.",
)


def render_device(device: DraftDevice, *, schema: bool = True) -> str:
    """One device document, in ``docs/schema.md`` field order."""
    out = _Document()
    _header(out, (*_DEVICE_HEADER, f"Source: {', '.join(device.sources) or 'unknown'}."))
    if device.comments:
        out.line("#")
        out.comments(device.comments)

    out.field("apiVersion", API_VERSION)
    if device.kind_comment:
        out.comment(device.kind_comment)
    out.field("kind", device.kind)

    out.line("metadata:")
    out.field("name", device.name, indent=2)
    out.optional("description", device.description, indent=2)

    out.line("spec:")
    for key in ("vendor", "model", "serial", "location"):
        out.optional(key, getattr(device, key), indent=2)

    out.line("interfaces:", indent=2)
    for interface in device.sorted_interfaces():
        _render_interface(out, interface)

    if device.vlans:
        out.comment(
            "the VLAN database, from every VLAN id observed on the ports above; names and "
            "descriptions are not reported by any capture, so add them by hand",
            indent=2,
        )
        out.line("vlans:", indent=2)
        for vid in sorted(device.vlans):
            out.line(f"- id: {vid}", indent=4)

    return _modeline(schema=schema) + out.text()


def _render_interface(out: _Document, interface: DraftInterface) -> None:
    """One ``spec.interfaces[]`` entry, fields in the order of §6.2."""
    out.comments(interface.comments, indent=4)
    out.line(f"- name: {scalar(interface.name)}", indent=4)
    out.field("type", interface.type, indent=6)
    out.optional("description", interface.description, indent=6)
    # ``enabled`` defaults to true, so only an observed *down* port is written.
    if interface.enabled is False:
        out.comment("observed administratively down", indent=6)
        out.field("enabled", False, indent=6)
    out.optional("mac", interface.mac, indent=6)
    out.optional("mtu", interface.mtu, indent=6)
    _render_addresses(out, "ipv4", interface.ipv4)
    _render_addresses(out, "ipv6", interface.ipv6)
    _render_vlan(out, interface)
    out.optional("parent", interface.parent, indent=6)
    if interface.members:
        out.line(f"members: {_flow(interface.members)}", indent=6)


def _render_addresses(out: _Document, family: str, addresses: Sequence[str]) -> None:
    if not addresses:
        return
    out.line(f"{family}:", indent=6)
    if len(addresses) == 1:
        out.line(f"addresses: {_flow(addresses)}", indent=8)
        return
    out.line("addresses:", indent=8)
    for address in addresses:
        out.line(f"- {scalar(address)}", indent=10)


def _render_vlan(out: _Document, interface: DraftInterface) -> None:
    vlan = interface.vlan
    if vlan is None:
        return
    if vlan.comment:
        out.comment(vlan.comment, indent=6)
    out.line("vlan:", indent=6)
    out.field("mode", vlan.mode, indent=8)
    if vlan.mode == "access":
        out.optional("access_vlan", vlan.access_vlan, indent=8)
    else:
        out.line(f"trunk_vlans: {_flow(vlan.trunk_vlans)}", indent=8)


# --------------------------------------------------------------------------- #
# Cables
# --------------------------------------------------------------------------- #

_CABLE_HEADER: Final[tuple[str, ...]] = (
    "Written by 'netgraph import'. One document per adjacency, separated by '---'.",
    "A cable is undirected, so an adjacency reported by both of its neighbours is "
    "one document here, not two; the comment on each cable names every capture it "
    "was seen in. A cable seen from one side only is still a cable — it just means "
    "the far end has not been captured yet.",
    "'medium' is required by the schema and no capture reports it, so every cable "
    "below says 'copper' unless an input stated otherwise. Fix the fibre and "
    "wireless runs before trusting an l1 diagram.",
)


def render_cables(cables: Sequence[DraftCable], *, schema: bool = True) -> str:
    """Every cable as one multi-document YAML file."""
    out = _Document()
    _header(out, _CABLE_HEADER)
    for index, cable in enumerate(cables):
        if index:
            out.line("---")
        _render_cable(out, cable)
    return _modeline(schema=schema) + out.text()


def _render_cable(out: _Document, cable: DraftCable) -> None:
    out.comments(cable.comments)
    if not cable.medium_stated:
        out.comment(
            f"inferred: no input states a medium for this link, so it reads {cable.medium!r}"
        )
    out.field("apiVersion", API_VERSION)
    out.field("kind", "cable")
    out.line("metadata:")
    out.field("name", cable.name, indent=2)
    out.line("spec:")
    out.line("endpoints:", indent=2)
    # ``cable.key`` rather than ``cable.endpoints``: the link is undirected and
    # the loader sorts the pair anyway, so writing the sorted order keeps the
    # file identical whichever end reported the adjacency first.
    for device, interface in cable.key:
        out.line(f"- {scalar(f'{device}:{interface}')}", indent=4)
    out.field("medium", cable.medium, indent=2)
    out.optional("speed", cable.speed, indent=2)
    out.optional("label", cable.label, indent=2)


# --------------------------------------------------------------------------- #
# The tree
# --------------------------------------------------------------------------- #


def render_draft(draft: Draft, *, schema: bool = True) -> dict[str, str]:
    """The whole draft as ``relative POSIX path -> file content``.

    Devices come first and in name order, then the cable file, which is the
    order the files are reported in and the order somebody reads them.
    """
    files: dict[str, str] = {}
    for device, path in _device_paths(draft.sorted_devices()):
        files[path] = render_device(device, schema=schema)
    if draft.cables:
        files[CABLES_FILE] = render_cables(draft.sorted_cables(), schema=schema)
    return files


def _device_paths(devices: Sequence[DraftDevice]) -> Iterator[tuple[DraftDevice, str]]:
    """One file per device, with case-insensitive collisions broken apart.

    ``SW1`` and ``sw1`` are two elements to netgraph and one file name on macOS
    and Windows, so the second one gets a suffix rather than silently replacing
    the first. The suffix is on the *file*; both documents still declare the
    name their device actually has.
    """
    taken: set[str] = set()
    for device in devices:
        stem, index = device.name, 1
        while f"{stem.lower()}.yaml" in taken:
            index += 1
            stem = f"{device.name}-{index}"
        taken.add(f"{stem.lower()}.yaml")
        yield device, f"{DEVICES_DIR}/{stem}.yaml"
