"""Scalar value types from §5 of ``docs/schema.md``.

Every type here is a *normalising* type: the value stored on the model is the
canonical form, so a document and its fully defaulted, normalised form render
identically (§1).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator, Field, StrictBool, model_serializer, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel

__all__ = [
    "API_VERSION",
    "BITRATE_PATTERN",
    "ELEMENT_NAME_PATTERN",
    "ELEMENT_REF_PATTERN",
    "IFNAME_PATTERN",
    "MAC_PATTERNS",
    "MAX_ELEMENT_REF_LENGTH",
    "MAX_RACK_UNITS",
    "MAX_SSID_OCTETS",
    "MAX_VLAN_ID",
    "MIN_VLAN_ID",
    "VLAN_SET_PATTERN",
    "ApiVersion",
    "BitRate",
    "Boolean",
    "ElementName",
    "ElementRef",
    "IPv4Mtu",
    "IPv6Mtu",
    "IfName",
    "InterfaceMtu",
    "LengthMetres",
    "MacAddress",
    "PortCount",
    "PrefixLengthV4",
    "PrefixLengthV6",
    "RackUnit",
    "RackUnits",
    "Ssid",
    "TxPowerDbm",
    "VlanId",
    "VlanSet",
    "Watts",
    "WirelessChannel",
    "check_ssid",
    "format_bitrate",
    "normalise_mac",
    "parse_bitrate",
]

#: The only ``apiVersion`` this revision of netgraph understands (§12).
API_VERSION: Final = "netgraph.dev/v1alpha1"

MAX_VLAN_ID: Final = 4094
MIN_VLAN_ID: Final = 1

#: One segment of a name: the whole of a declared ``metadata.name``, and one
#: ``/``-separated component of a qualified reference. Both grammars are built
#: from it so they cannot drift apart (§4.1).
_NAME_SEGMENT: Final = r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*"
_ELEMENT_NAME_RE: Final = re.compile(rf"^{_NAME_SEGMENT}$")
#: One or more segments joined by ``/``: no leading, trailing or doubled slash.
_ELEMENT_REF_RE: Final = re.compile(rf"^{_NAME_SEGMENT}(?:/{_NAME_SEGMENT})*$")
_IFNAME_RE: Final = re.compile(r"^[A-Za-z0-9._/-]+$")

#: Ceiling on a reference: a namespace is a directory path, so a qualified name
#: is bounded by what a filesystem will hold rather than by the 253 characters
#: of a single declared name.
MAX_ELEMENT_REF_LENGTH: Final = 1024

#: The name grammars as bare regular expressions. They are exported because the
#: JSON Schema of :mod:`netgraph.schema` has to advertise exactly the grammar
#: enforced here; deriving the schema from these constants is what stops the two
#: from drifting apart.
ELEMENT_NAME_PATTERN: Final = _ELEMENT_NAME_RE.pattern
ELEMENT_REF_PATTERN: Final = _ELEMENT_REF_RE.pattern
IFNAME_PATTERN: Final = _IFNAME_RE.pattern


def _case_insensitive(word: str) -> str:
    """``all`` → ``[aA][lL][lL]``.

    JSON Schema patterns are ECMA-262 regular expressions written without flags,
    so a case-insensitive literal has to be spelled out as character classes.
    """
    return "".join(f"[{character.lower()}{character.upper()}]" for character in word)


# §5: booleans are strict so that a quoted ``"true"`` is an error rather than a
# silently accepted truth value. YAML 1.1's ``yes``/``no`` are rejected by the
# loader, which is the only layer that can still see them.
Boolean = StrictBool

#: ``NG-D002``: the loader accepts exactly the versions it knows (§12).
ApiVersion = Literal["netgraph.dev/v1alpha1"]

#: Element *declaration* grammar, §4.1: what ``metadata.name`` may hold. A
#: declared name is always a single segment — the namespace comes from the
#: directory the document was found in, never from the name itself.
ElementName = Annotated[
    str,
    Field(min_length=1, max_length=253, pattern=_ELEMENT_NAME_RE.pattern),
]

#: Element *reference* grammar, §4.1: what a field naming an existing element
#: may hold. A bare segment is resolved outwards from the referring namespace;
#: a ``/``-separated reference such as ``sites/berlin/rack1/sw1`` is tried
#: relative to that namespace first and as an absolute name second
#: (:meth:`netgraph.loader.Inventory.lookup`, §2.2).
ElementRef = Annotated[
    str,
    Field(min_length=1, max_length=MAX_ELEMENT_REF_LENGTH, pattern=_ELEMENT_REF_RE.pattern),
]

#: Interface name grammar, §4.1.
IfName = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=_IFNAME_RE.pattern),
]

#: 802.1Q VLAN identifier: 0 (priority-tagged) and 4095 (reserved) are rejected.
VlanId = Annotated[int, Field(strict=True, ge=MIN_VLAN_ID, le=MAX_VLAN_ID)]

#: A count of physical ports on a piece of hardware. Hardware with no port is
#: not hardware anyone cables, so the lower bound is 1.
PortCount = Annotated[int, Field(strict=True, ge=1)]

#: Tallest rack anyone racks equipment into. A 58U cabinet is the largest
#: standard product; the bound leaves room for an open frame and still refuses
#: a position that is a typo rather than a shelf.
MAX_RACK_UNITS: Final = 100

#: A rack unit, counted from 1 at the *bottom* of the rack (§3.2). Bottom-up is
#: how a rack is labelled and how an elevation is read, so a position means the
#: same thing in the document and on the cabinet.
RackUnit = Annotated[int, Field(strict=True, ge=1, le=MAX_RACK_UNITS)]

#: A height in rack units. One is the smallest thing anyone mounts.
RackUnits = Annotated[int, Field(strict=True, ge=1, le=MAX_RACK_UNITS)]

#: Layer-2 MTU (``NG-I011``). The RFC 8344 IPv4 minimum is the lower bound; the
#: upper bound is the ``uint16`` maximum of ``ip:ipv4/mtu``.
InterfaceMtu = Annotated[int, Field(strict=True, ge=68, le=65535)]
#: ``ip:ipv4/mtu`` (``uint16``, minimum 68).
IPv4Mtu = Annotated[int, Field(strict=True, ge=68, le=65535)]
#: ``ip:ipv6/mtu`` (``uint32``, minimum 1280).
IPv6Mtu = Annotated[int, Field(strict=True, ge=1280, le=4294967295)]

PrefixLengthV4 = Annotated[int, Field(strict=True, ge=0, le=32)]
PrefixLengthV6 = Annotated[int, Field(strict=True, ge=0, le=128)]


# --------------------------------------------------------------------------- #
# Radio values (§6.2.6)
# --------------------------------------------------------------------------- #

#: IEEE 802.11 caps an SSID at 32 *octets*, not 32 characters, so a name in a
#: non-Latin script runs out of room sooner than its length suggests.
MAX_SSID_OCTETS: Final = 32


def check_ssid(value: Any) -> Any:
    """Reject an SSID that no radio could beacon (``NG-W001``).

    Only the octet bound and the empty string are checked here. Every byte
    sequence is a legal SSID as far as 802.11 is concerned — the element is a
    counted octet string, not text — so a name with a space, an emoji or a
    trailing dot is accepted as written and stored unchanged.

    A YAML scalar that is not a string is refused rather than stringified: an
    unquoted ``5`` is a number to the loader, and silently turning it into
    ``"5"`` would hide the missing quotes from a reader comparing the document
    with the access point.
    """
    if isinstance(value, str):
        octets = len(value.encode("utf-8"))
        if not octets:
            raise ValueError("an SSID is at least one octet long")
        if octets > MAX_SSID_OCTETS:
            raise ValueError(
                f"{echo_value(value)} is {octets} octets long; IEEE 802.11 allows "
                f"at most {MAX_SSID_OCTETS}"
            )
    return value


#: ``dot11:ssid`` — an octet string of 1 to 32 bytes (§5). ``max_length`` is the
#: character bound the JSON Schema can express; :func:`check_ssid` enforces the
#: octet bound the standard actually states.
Ssid = Annotated[
    str,
    BeforeValidator(check_ssid),
    Field(min_length=1, max_length=MAX_SSID_OCTETS),
]

#: An 802.11 channel number. The bound spans every band's numbering: 1 to 14 at
#: 2.4 GHz, 32 to 177 at 5 GHz, 1 to 233 at 6 GHz; which numbers are legal in
#: which band is :class:`~netgraph.models.interface.Band`'s business (``NG-W003``).
WirelessChannel = Annotated[int, Field(strict=True, ge=1, le=233)]

#: Radiated power in dBm. The floor admits the -10 dBm a phone-sized radio backs
#: off to; the ceiling is above any regulatory domain's EIRP limit, so a value
#: past it is a unit mix-up (milliwatts written as dBm) rather than a setting.
TxPowerDbm = Annotated[float, Field(ge=-30.0, le=40.0, allow_inf_nan=False)]


# --------------------------------------------------------------------------- #
# MAC addresses
# --------------------------------------------------------------------------- #

_MAC_COLON_RE: Final = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_MAC_DASH_RE: Final = re.compile(r"^[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}$")
_MAC_DOT_RE: Final = re.compile(r"^[0-9A-Fa-f]{4}(?:\.[0-9A-Fa-f]{4}){2}$")

#: The three spellings :func:`normalise_mac` accepts, each anchored. Joined with
#: ``|`` they become the ``pattern`` of the MAC address in the JSON Schema.
MAC_PATTERNS: Final[tuple[str, ...]] = (
    _MAC_COLON_RE.pattern,
    _MAC_DASH_RE.pattern,
    _MAC_DOT_RE.pattern,
)


def normalise_mac(value: Any) -> str:
    """Normalise an EUI-48 address to lower-case, colon-separated form.

    Accepts the three spellings listed in §5 (``xx:xx:xx:xx:xx:xx``,
    ``XX-XX-XX-XX-XX-XX`` and ``xxxx.xxxx.xxxx``) and rejects everything else.
    """
    if isinstance(value, (bool, int)):
        # A YAML 1.1 loader resolves e.g. ``12:34:56:12:34:56`` as a
        # sexagesimal integer. The original digits cannot be recovered.
        raise ValueError(
            "MAC address was parsed as a number; quote it in the YAML document "
            '(for example "12:34:56:12:34:56")'
        )
    if not isinstance(value, str):
        raise ValueError(f"expected a MAC address string, got {type(value).__name__}")

    text = value.strip()
    if _MAC_COLON_RE.match(text):
        digits = text.replace(":", "")
    elif _MAC_DASH_RE.match(text):
        digits = text.replace("-", "")
    elif _MAC_DOT_RE.match(text):
        digits = text.replace(".", "")
    else:
        raise ValueError(
            f"{echo_value(value)} is not a MAC address; expected xx:xx:xx:xx:xx:xx, "
            "XX-XX-XX-XX-XX-XX or xxxx.xxxx.xxxx"
        )

    digits = digits.lower()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


#: ``yang:phys-address`` restricted to EUI-48, normalised (§5).
MacAddress = Annotated[str, BeforeValidator(normalise_mac)]


# --------------------------------------------------------------------------- #
# Bit rates
# --------------------------------------------------------------------------- #

_BITRATE_RE: Final = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]+)$")
_BITRATE_UNITS: Final[dict[str, int]] = {
    "bps": 1,
    "kbps": 1_000,
    "mbps": 1_000_000,
    "gbps": 1_000_000_000,
    "tbps": 1_000_000_000_000,
}
#: The string form :func:`parse_bitrate` accepts, as an ECMA-262 pattern. Built
#: from :data:`_BITRATE_UNITS` so a new unit cannot be added to one without the
#: other. The named groups of :data:`_BITRATE_RE` are Python-only syntax, which
#: is why this is spelled out separately rather than reused.
BITRATE_PATTERN: Final = (
    r"^\d+(?:\.\d+)?\s*(?:" + "|".join(_case_insensitive(unit) for unit in _BITRATE_UNITS) + r")$"
)

#: Largest unit first, for :func:`format_bitrate`.
_BITRATE_SUFFIXES: Final[tuple[tuple[str, int], ...]] = (
    ("Tbps", 1_000_000_000_000),
    ("Gbps", 1_000_000_000),
    ("Mbps", 1_000_000),
    ("kbps", 1_000),
    ("bps", 1),
)


def parse_bitrate(value: Any) -> int:
    """Normalise a §5 ``speed`` value to bit/s.

    Accepts a plain integer (already bit/s) or ``<number><unit>`` with a
    decimal unit, for example ``1Gbps`` → ``1000000000``.
    """
    if isinstance(value, bool):
        raise ValueError("expected a bit rate, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"bit rate {echo_value(value)} is not a whole number of bit/s")
        return int(value)
    if not isinstance(value, str):
        raise ValueError(f"expected a bit rate, got {type(value).__name__}")

    match = _BITRATE_RE.match(value.strip())
    if match is None:
        raise ValueError(
            f"{echo_value(value)} is not a bit rate; expected bit/s or <number><unit> with "
            "unit bps, kbps, Mbps, Gbps or Tbps"
        )
    factor = _BITRATE_UNITS.get(match["unit"].lower())
    if factor is None:
        raise ValueError(
            f"unknown bit-rate unit {echo_value(match['unit'])}; expected bps, kbps, "
            "Mbps, Gbps or Tbps"
        )
    scaled = float(match["number"]) * factor
    if scaled != int(scaled):
        raise ValueError(f"bit rate {echo_value(value)} is not a whole number of bit/s")
    return int(scaled)


def format_bitrate(bits_per_second: int) -> str:
    """Render bit/s back in the largest unit that stays exact (§5)."""
    for suffix, factor in _BITRATE_SUFFIXES:
        if bits_per_second >= factor and bits_per_second % factor == 0:
            return f"{bits_per_second // factor}{suffix}"
    return f"{bits_per_second}bps"


#: ``if:speed`` (``yang:gauge64``), stored in bit/s.
BitRate = Annotated[int, BeforeValidator(parse_bitrate), Field(strict=True, gt=0)]

#: Physical cable length in metres; integers are accepted and widened.
LengthMetres = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]

#: A wattage (§17): a device's draw, a PDU's capacity, a PoE reservation.
#: Strictly positive — ``0 W`` is not a load, it is the absence of one, and
#: recording it as a load would put a device on a schedule that draws nothing.
#: The ceiling is a megawatt: far above any rack and far below a float that
#: would make a utilisation sum meaningless. Integers are accepted and widened,
#: because ``draw_watts: 120`` is how a nameplate is written.
Watts = Annotated[float, Field(gt=0.0, le=1_000_000.0, allow_inf_nan=False)]


# --------------------------------------------------------------------------- #
# VLAN sets
# --------------------------------------------------------------------------- #

_VLAN_RANGE_RE: Final = re.compile(r"^(?P<low>\d{1,4})\s*-\s*(?P<high>\d{1,4})$")

#: One token of the comma-separated string form: an id, an inclusive range, or
#: one of the two keywords. Ids are not range-checked here — 4095 is a *number*
#: that is out of range, which the model reports far better than "no match".
_VLAN_TOKEN_PATTERN: Final = (
    r"(?:"
    + _case_insensitive("all")
    + r"|"
    + _case_insensitive("none")
    + r"|\d{1,4}(?:\s*-\s*\d{1,4})?)"
)
#: The string form of a ``vlan-set``, as an ECMA-262 pattern.
VLAN_SET_PATTERN: Final = rf"^\s*{_VLAN_TOKEN_PATTERN}\s*(?:,\s*{_VLAN_TOKEN_PATTERN}\s*)*$"


def _coalesce(ranges: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Sort and merge overlapping or adjacent ``(low, high)`` pairs."""
    merged: list[tuple[int, int]] = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1] + 1:
            previous_low, previous_high = merged[-1]
            merged[-1] = (previous_low, max(previous_high, high))
        else:
            merged.append((low, high))
    return tuple(merged)


def _parse_vlan_token(token: Any) -> list[tuple[int, int]]:
    """Turn one ``vlan-set`` token into a list of inclusive ranges."""
    if isinstance(token, bool):
        raise ValueError("expected a VLAN id or range, got a boolean")
    if isinstance(token, int):
        return [(token, token)]
    if not isinstance(token, str):
        raise ValueError(f"expected a VLAN id or range, got {type(token).__name__}")

    text = token.strip()
    if text.lower() == "all":
        return [(MIN_VLAN_ID, MAX_VLAN_ID)]
    if text.lower() == "none":
        return []
    if text.isdigit():
        return [(int(text), int(text))]
    match = _VLAN_RANGE_RE.match(text)
    if match is None:
        raise ValueError(
            f"{echo_value(token)} is not a VLAN id, a range such as '100-110', 'all' or 'none'"
        )
    low, high = int(match["low"]), int(match["high"])
    if low > high:
        raise ValueError(f"VLAN range {echo_value(token)} is inverted: {low} > {high}")
    return [(low, high)]


class VlanSet(NetgraphModel):
    """A ``dot1qtypes:vid-range-type`` value: a sorted, coalesced VLAN set.

    Accepts a single id, a list of ids and ``"low-high"`` range strings, a
    comma-separated string, or the literals ``all`` (1-4094) and ``none``.
    Serialises back to the canonical ``"10,20,100-110"`` string form.
    """

    ranges: tuple[tuple[VlanId, VlanId], ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _accept_shorthands(cls, value: Any) -> Any:
        if isinstance(value, VlanSet):
            return value
        if isinstance(value, dict):
            return value

        tokens: list[Any]
        if isinstance(value, str):
            tokens = [part for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple)):
            tokens = list(value)
        else:
            tokens = [value]

        ranges: list[tuple[int, int]] = []
        for token in tokens:
            ranges.extend(_parse_vlan_token(token))
        return {"ranges": _coalesce(ranges)}

    @model_validator(mode="after")
    def _normalise(self) -> VlanSet:
        coalesced = _coalesce(self.ranges)
        if coalesced != self.ranges:
            self.ranges = coalesced
        return self

    @model_serializer
    def _serialise(self) -> str:
        return self.to_string()

    def to_string(self) -> str:
        """The canonical ``dot1qtypes:vid-range-type`` string."""
        if not self.ranges:
            return "none"
        return ",".join(str(low) if low == high else f"{low}-{high}" for low, high in self.ranges)

    def __contains__(self, vlan_id: object) -> bool:
        if not isinstance(vlan_id, int) or isinstance(vlan_id, bool):
            return False
        return any(low <= vlan_id <= high for low, high in self.ranges)

    def __iter__(self) -> Iterator[int]:  # type: ignore[override]
        for low, high in self.ranges:
            yield from range(low, high + 1)

    def __len__(self) -> int:
        return sum(high - low + 1 for low, high in self.ranges)

    def __bool__(self) -> bool:
        return bool(self.ranges)

    def __str__(self) -> str:
        return self.to_string()

    def isdisjoint(self, other: VlanSet) -> bool:
        """True when no VLAN id is a member of both sets."""
        return all(
            other_high < low or other_low > high
            for low, high in self.ranges
            for other_low, other_high in other.ranges
        )
