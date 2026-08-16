"""``--from nftables``: an ``/etc/nftables.conf``, or what ``nft list ruleset`` prints.

The mirror of :mod:`netviz.export.config.nftables`, and the one reader here
that imports *nothing* into the draft on purpose.

**A ruleset says what a box refuses, and a draft has nowhere to put that.** A
:class:`~netviz.importer.draft.Draft` is the vocabulary
:mod:`netviz.importer.emit` can write: elements, interfaces, addresses, VLANs,
members, links. It has no field for a security zone and none for a filter rule,
because those are ``spec.zones`` and ``spec.firewall`` (§24) and a draft predates
both. So this reader reads the file, counts what is in it, and says so — the
same thing :mod:`netviz.importer.config.neutral` does for a ``route`` or a
``policy`` stanza, and for the same reason: a note naming what was found is the
difference between "netviz does not import this" and "netviz read your
firewall and quietly dropped it".

**The interfaces in a zone are not imported either**, and that is the tempting
mistake. ``set zone_lan { elements = { "eth0", "eth1" } }`` names two ports of
the device, and adding them to the draft would produce interfaces with no type,
no MTU and no address — which is not an observation of a port, it is the
knowledge that a *name* was mentioned. A drift check comparing that against a
real inventory would report every one of them as a difference in whichever
direction the comparison ran.

So :mod:`netviz.drift.coverage` grants this dialect nothing at all, exactly as
it grants ``frr`` and ``wireguard`` nothing: what it sees is not part of what
drift compares, and an interface in the inventory and not here is not drift.

What it *does* read is the banner — which device the file is for, and which
documents it came from — because that is what makes ``netviz drift`` able to
take a directory of generated files without being told what each one is.

Nothing here raises: an unreadable line is counted and the next one is read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from netviz.errors import count_text
from netviz.importer.config.common import fold_into
from netviz.importer.draft import Draft

__all__ = ["read_nftables"]

#: ``set zone_lan {`` — the declaration of a zone's interface set. The prefix is
#: the emitter's (:func:`netviz.export.config.nftables._set_name`), and a set
#: without it is somebody else's set and none of this reader's business.
_ZONE_SET: Final[re.Pattern[str]] = re.compile(r"^set\s+zone_(\w+)\s*\{")

#: ``chain input {`` — a chain of any kind; the name is what the note reports.
_CHAIN: Final[re.Pattern[str]] = re.compile(r"^chain\s+(\S+)\s*\{")

#: ``type filter hook input priority filter; policy drop;`` — a base chain's
#: declaration, which is the only place its default action is written.
_POLICY: Final[re.Pattern[str]] = re.compile(
    r"^type\s+(\S+)\s+hook\s+(\S+)\s+.*?policy\s+(\w+)\s*;"
)

#: A verdict at the end of a rule. Enough to tell a rule from the brace, the
#: comment and the ``elements =`` line, which is all the count needs.
_VERDICT: Final[re.Pattern[str]] = re.compile(
    r"\b(accept|drop|reject|masquerade|snat|dnat|redirect|log|meta mark set)\b"
)


@dataclass
class _Seen:
    """What the file held, for the one note this reader produces."""

    zones: list[str]
    chains: list[str]
    policies: list[str]
    rules: int = 0

    def summary(self) -> str:
        """``2 zone(s) [lan, wan], 3 chain(s) …`` — everything, in one phrase."""
        parts: list[str] = []
        if self.zones:
            parts.append(f"{count_text(len(self.zones), 'zone')} [{', '.join(self.zones)}]")
        if self.chains:
            parts.append(f"{count_text(len(self.chains), 'chain')} [{', '.join(self.chains)}]")
        if self.policies:
            parts.append("defaults " + ", ".join(self.policies))
        if self.rules:
            parts.append(count_text(self.rules, "rule"))
        return "; ".join(parts)


def read_nftables(text: str, *, source: str, host: str, draft: Draft) -> None:
    """Fold one nftables ruleset into ``draft`` — which is to say, note it.

    Args:
        text: The file, or what ``nft list ruleset`` printed.
        source: Name of the input, for the note and for the coverage record.
        host: Element name of the device the ruleset belongs to.
        draft: Accumulator, mutated in place. Only :meth:`Draft.device` and the
            note below are touched: see the module docstring for why nothing in
            a ruleset becomes a draft field.
    """
    fold_into(draft, host, source)

    seen = _Seen(zones=[], chains=[], policies=[])
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if (zone := _ZONE_SET.match(line)) is not None:
            seen.zones.append(zone.group(1))
            continue
        if (chain := _CHAIN.match(line)) is not None:
            seen.chains.append(chain.group(1))
            continue
        if (policy := _POLICY.match(line)) is not None:
            seen.policies.append(f"{policy.group(2)}={policy.group(3)}")
            continue
        if line.startswith(("table ", "elements ", "type ", "comment ")) or line in ("}", "{"):
            continue
        if _VERDICT.search(line):
            seen.rules += 1

    if summary := seen.summary():
        draft.note(
            f"{source} states {summary}; a draft has no security zone and no filter policy, "
            f"so none of it was imported — 'spec.zones' and 'spec.firewall' (section 24) are "
            f"where they belong, and a drift check cannot compare what this device refuses"
        )
