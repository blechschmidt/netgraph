"""Shared scaffolding for the six configuration readers.

Split from the package's ``__init__`` because that module imports every reader
and the readers import this one: a helper and a registry in one file would be a
cycle. What lives here is what more than one reader needs — the banner, the
sniffer, and the stanza splitter two of the grammars share.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Final

from netgraph.banner import DIALECT_KEY, ELEMENT_KEY, parse_banner
from netgraph.importer.draft import Draft

__all__ = [
    "CONFIG_DIALECT_NAMES",
    "MAX_VLAN_ID",
    "MIN_VLAN_ID",
    "banner_dialect",
    "banner_element",
    "fold_into",
    "read_int",
    "read_vlan_id",
    "sniff",
    "stanzas",
]

#: The 802.1Q VID range, restated here rather than imported so that the readers
#: have no dependency on the model layer for a bound they apply to raw text.
MIN_VLAN_ID: Final = 1
MAX_VLAN_ID: Final = 4094

#: Longest run of digits a reader will convert. Ten covers every number any of
#: these formats holds -- an MTU, a VID, a table id, a port -- and it is the
#: bound that matters rather than the exact figure: ``int()`` refuses a literal
#: of more than 4300 digits by *raising*, with a message about
#: ``sys.set_int_max_str_digits`` that would escape a drift run as a traceback,
#: and a shorter run can still name a range with four billion members.
_MAX_DIGITS: Final = 10


def read_int(value: str, *, low: int | None = None, high: int | None = None) -> int | None:
    """``value`` as a decimal integer inside the given bounds, or ``None``.

    ``None`` for anything that is not one — a word, a negative number, an empty
    string, a run of digits longer than :data:`_MAX_DIGITS`, or a value outside
    ``low``..``high``. A reader never raises on a capture (:mod:`the package
    docstring <netgraph.importer.config>` says why), so every conversion in every
    reader goes through this rather than through a bare ``int()`` guarded by
    ``str.isdigit``, which is what let a mistyped MTU end a whole drift run.

    ``str.isascii`` matters as much as ``str.isdigit``: the latter accepts
    Devanagari and fullwidth digits, which ``int()`` then happily converts into
    a number nothing in the file said.
    """
    text = value.strip()
    if not text.isascii() or not text.isdigit() or len(text) > _MAX_DIGITS:
        return None
    number = int(text)
    if low is not None and number < low:
        return None
    if high is not None and number > high:
        return None
    return number


def read_vlan_id(value: str) -> int | None:
    """``value`` as an 802.1Q VID, or ``None``.

    Bounded at both ends, which is what keeps a mistyped range in a
    ``[BridgeVLAN] VLAN=1-4000000000`` from being expanded into four billion
    integers — a ``MemoryError`` out of a reader that is documented never to
    raise.
    """
    return read_int(value, low=MIN_VLAN_ID, high=MAX_VLAN_ID)


#: Every configuration dialect ``--from`` accepts, in the order
#: :data:`netgraph.export.config.CONFIG_DIALECTS` lists them. Spelled here rather
#: than imported from the registry so that the sniffer has no dependency on the
#: emitters; the two are kept in step by a test.
CONFIG_DIALECT_NAMES: Final[tuple[str, ...]] = (
    "netplan",
    "networkd",
    "ifupdown",
    "frr",
    "wireguard",
    "interfaces",
)

#: Both comment markers a generated banner may use: ``!`` is FRR's and ``#`` is
#: everything else's. A reader looking for the banner does not yet know which
#: dialect wrote the file -- that is what it is asking -- so both are tried.
_MARKERS: Final[tuple[str, ...]] = ("#", "!")

#: How the sniffer recognises each dialect, in the order it tries them. Ordered
#: most specific first: a netplan document and a neutral one both begin with a
#: word at column 0, and only netplan's is ``network:``.
#:
#: Every pattern is anchored to the start of a line, so a directive mentioned
#: inside a comment cannot be mistaken for the file being that dialect.
_SIGNATURES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("netplan", re.compile(r"^network:\s*$", re.MULTILINE)),
    ("wireguard", re.compile(r"^\[Interface\]\s*$", re.MULTILINE)),
    ("networkd", re.compile(r"^\[(?:Match|NetDev)\]\s*$", re.MULTILINE)),
    ("interfaces", re.compile(r"^device\s+\S+\s*$", re.MULTILINE)),
    ("frr", re.compile(r"^(?:frr version|router (?:bgp|ospf)|line vty)\b", re.MULTILINE)),
    ("ifupdown", re.compile(r"^iface\s+\S+\s+inet6?\s+\w+", re.MULTILINE)),
)


def banner_dialect(text: str) -> str | None:
    """The dialect a generated file says it is, or ``None`` for anything else."""
    return _banner(text).get(DIALECT_KEY)


def banner_element(text: str) -> str | None:
    """The fully-qualified element name a generated file says it describes.

    ``sites/north/core/rtr-01`` -- the whole name, namespace included. The caller
    reduces it to the device name the draft is keyed by; keeping the qualified
    form here means the value is the one the inventory uses, which is what a
    diagnostic wants to print.
    """
    element = _banner(text).get(ELEMENT_KEY)
    if element is None:
        return None
    # The banner writes ``<fqn> (<kind>)``; the kind is for a human reading the
    # file and is not part of the name.
    return element.partition(" (")[0].strip() or None


def sniff(text: str) -> str | None:
    """Which configuration dialect ``text`` is, or ``None`` if it is none of them.

    The banner wins when there is one: a file netgraph wrote says what it is, and
    guessing at it instead would be choosing a heuristic over a statement. For
    anything else the six grammars are told apart by a line only one of them can
    have -- ``network:``, ``[Interface]``, ``[Match]``, a ``device`` stanza, an
    FRR keyword, an ``iface`` line.

    ``None`` rather than a guess when nothing matches: the caller falls back to
    the three capture dialects, and a file that is none of the nine is an error
    with a message naming ``--from``.
    """
    stated = banner_dialect(text)
    if stated in CONFIG_DIALECT_NAMES:
        return stated
    for name, pattern in _SIGNATURES:
        if pattern.search(text):
            return name
    return None


def _banner(text: str) -> Mapping[str, str]:
    """The ``netgraph-*`` keys of ``text``, whichever comment marker it uses."""
    for marker in _MARKERS:
        found = parse_banner(text, marker)
        if DIALECT_KEY in found:
            return found
    return {}


def stanzas(text: str) -> Iterator[tuple[str, list[str]]]:
    """``(header, indented lines)`` for a file whose stanzas start at column 0.

    Shared by the ``interfaces`` and ``ifupdown`` readers, which have the same
    shape for different reasons -- netgraph chose it, Debian inherited it -- and
    would otherwise each grow a slightly different idea of what ends a stanza.

    A comment line is dropped wherever it appears, a blank line does *not* end a
    stanza (an operator's ``/etc/network/interfaces`` is full of them), and a
    line indented by anything at all belongs to the stanza above it.
    """
    header = ""
    body: list[str] = []
    for raw in text.splitlines():
        line = "" if raw.lstrip().startswith(("#", "!")) else raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line[:1].isspace():
            if header:
                body.append(line.strip())
            continue
        if header:
            yield header, body
        header, body = line.strip(), []
    if header:
        yield header, body


def fold_into(draft: Draft, host: str, source: str) -> None:
    """Register ``host`` as observed in ``source``, creating it if new.

    One line, but every reader here needs it before it writes anything, and a
    reader that forgot would produce a device with interfaces and no source --
    which :mod:`netgraph.drift.coverage` reads as "no dialect saw this", making
    everything about it unobserved.
    """
    draft.device(host).observed_in(source)
