"""Folding inventory names into the grammar each artefact demands.

An inventory name is not a hostname. ``metadata.name`` is bounded by §4.1 and
is close to one, but a *namespace* is a directory path — whatever the operating
system let somebody call a folder — and the fully-qualified name a diagram
prints is the two joined by ``/``. None of the five artefacts this package
emits can hold that:

============  ==============================================================
Artefact      Grammar
============  ==============================================================
hosts         A hostname: letters, digits and ``-``, dot-separated labels of
              at most 63 octets (RFC 952, RFC 1123 §2.1).
dns-zone      The same, as the owner name of an A/AAAA/PTR record.
ansible       A group name: ``[A-Za-z0-9_]``, not starting with a digit.
prometheus    Label *values* are arbitrary UTF-8, so nothing is folded; the
              label *names* are constants written out in the emitter.
cable-list    CSV per RFC 4180, or a Markdown table cell.
============  ==============================================================

The rule this module implements, once, is therefore: **fold, do not fail** —
and record every fold. ``sites/Building A/sw 1`` becomes ``sw-1.building-a``
rather than an error, and the manifest says so, because an export that refuses
an inventory the rest of the tool renders happily is an export nobody runs
twice. The one thing that *is* an error is a name of which nothing survives the
fold; that is recorded as a skip, since guessing a replacement would put a
record in a zone file that points at the wrong machine.

Namespace ordering
------------------

A fully-qualified name reads outside in — ``sites/north/access/sw-01`` — and a
domain name reads inside out. So the namespace is reversed, not just
translated: ``sw-01.access.north.sites``. That is what makes the DNS hierarchy
mirror the folder hierarchy, so ``dig axfr`` over ``north.sites.example.com``
returns exactly the site a reader would have expected.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

__all__ = [
    "MAX_DNS_LABEL",
    "MAX_DNS_NAME",
    "ansible_identifier",
    "csv_cell",
    "dns_labels",
    "dns_name_parts",
    "domain_name",
    "is_domain_name",
    "is_host_label",
    "markdown_cell",
    "sanitise_label",
    "transliterate",
]

#: RFC 1035 §2.3.4: a label is at most 63 octets, a name at most 255.
MAX_DNS_LABEL: Final = 63
MAX_DNS_NAME: Final = 255

#: Runs of the replacement character, collapsed so ``a  b`` does not become
#: ``a--b`` and lose the boundary between two labels that were one word apart.
_DASH_RUN: Final = re.compile(r"-{2,}")

#: A legal host label after folding, as a last-line assertion rather than as
#: the thing that does the work.
_LDH_LABEL: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

#: A domain name given on the command line — ``--origin``, ``--soa-mname``.
#: Underscores are allowed because a zone genuinely may be delegated under one
#: (``_tcp.example.com``); a leading digit is allowed because RFC 1123 §2.1
#: permits it. The trailing dot is optional here and normalised on by
#: :func:`domain_name`.
_DOMAIN: Final = re.compile(
    r"^(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*"
    r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.?$"
)

#: An Ansible group or variable name. Ansible resolves inventory groups as
#: Python identifiers in templates, so anything else has to be folded.
_ANSIBLE_LEAD: Final = re.compile(r"^[^A-Za-z_]+")
_ANSIBLE_BAD: Final = re.compile(r"[^A-Za-z0-9_]+")

#: Leading characters every major spreadsheet treats as the start of a formula
#: rather than as text, however the field is quoted. See :func:`csv_cell`.
_FORMULA_LEAD: Final = frozenset("=+@\t\r")
#: A field that merely *starts* with a minus and is a number, which must not be
#: escaped: ``-3`` is a value, ``-2+3`` is a formula.
_NEGATIVE_NUMBER: Final = re.compile(r"^-\d+(?:\.\d+)?$")


def transliterate(text: str) -> str:
    """``text`` reduced to ASCII, keeping as much of it as decomposes.

    ``NFKD`` splits a precomposed character into its base and its combining
    marks, and the ASCII encode then drops the marks: ``münchen`` becomes
    ``munchen`` and ``Ünicorn`` becomes ``Unicorn``. That is worth doing before
    *any* of the folds below, because the alternative — replacing the character
    with a separator — silently eats the first letter of a name and leaves
    ``nicorn``, which reads like a typo rather than like a transliteration.

    A script with no ASCII decomposition (Cyrillic, Han, emoji) leaves nothing
    behind. The callers treat an empty result as "not representable" and record
    it, rather than inventing a replacement.
    """
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def sanitise_label(text: str) -> str:
    """Fold one inventory name segment into a single DNS label.

    Three steps, in this order:

    1. **Transliterate.** ``NFKD`` decomposition followed by an ASCII encode
       turns ``münchen`` into ``munchen`` and ``café`` into ``cafe``, rather
       than throwing away the whole segment because it is not ASCII. A script
       with no ASCII decomposition — Cyrillic, Han, emoji — leaves nothing
       behind, which the caller reports as a skip.
    2. **Fold.** Everything that is not ``[a-z0-9]`` becomes ``-``; runs
       collapse; leading and trailing ``-`` go, because a label may not begin
       or end with a hyphen.
    3. **Truncate** to :data:`MAX_DNS_LABEL` octets, then strip a hyphen the
       cut may have exposed.

    Returns:
        The folded label, or ``""`` when nothing survived.
    """
    folded = "".join(char if char.isalnum() else "-" for char in transliterate(text).lower())
    folded = _DASH_RUN.sub("-", folded).strip("-")
    if len(folded) > MAX_DNS_LABEL:
        folded = folded[:MAX_DNS_LABEL].rstrip("-")
    return folded


def dns_name_parts(fqn: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(own labels, namespace labels)`` of a fully-qualified name.

    ``sites/north/access/sw-01`` becomes ``(("sw-01",), ("access", "north",
    "sites"))``: the element's own name, and its namespace reversed, so the
    domain hierarchy mirrors the folder hierarchy. A segment carrying a dot is
    split on it first — a dot already separates labels in every grammar here —
    and a segment of which nothing survives the fold is dropped rather than
    emitted as an empty label, which would produce a syntactically invalid
    name.

    The two halves are returned separately because the *element's own name* may
    itself be several labels: ``metadata.name`` of ``core.example.com`` is one
    name under the §4.1 grammar and three labels here. A caller publishing a
    short alias has to publish all three — ``core`` alone is a different
    element's name, and handing it out would point ``ping core`` at the wrong
    machine.

    Returns:
        ``((), ())`` when the element's *own* name folds to nothing. A namespace
        that folds away merely shortens the name; an element that does is one
        the caller must skip.
    """
    segments = [segment for segment in fqn.split("/") if segment]
    if not segments:
        return ((), ())
    *namespace, name = segments

    own = tuple(label for label in _split_labels(name) if label)
    if not own:
        return ((), ())
    outer = tuple(
        label for segment in reversed(namespace) for label in _split_labels(segment) if label
    )
    return own, outer


def dns_labels(fqn: str) -> tuple[str, ...]:
    """Every DNS label of ``fqn``, innermost first — the two halves joined.

    ``sites/north/access/sw-01`` becomes ``("sw-01", "access", "north",
    "sites")``. See :func:`dns_name_parts` for why the halves are also
    available separately.
    """
    own, outer = dns_name_parts(fqn)
    return own + outer


def _split_labels(segment: str) -> tuple[str, ...]:
    """One path segment as folded DNS labels, splitting on any dot it holds."""
    return tuple(sanitise_label(part) for part in segment.split("."))


def domain_name(text: str) -> str:
    """Normalise a domain name given on the command line to its absolute form.

    Raises:
        ValueError: ``text`` is not a domain name at all. Refusing here is the
            point: an origin of ``example .com`` silently emitted would produce
            a zone file every nameserver rejects, with the error attributed to
            netviz's output rather than to the flag.
    """
    candidate = text.strip()
    if not candidate:
        raise ValueError("a domain name cannot be empty")
    if len(candidate.rstrip(".")) > MAX_DNS_NAME:
        raise ValueError(
            f"domain name is {len(candidate)} characters, longer than the {MAX_DNS_NAME} "
            f"RFC 1035 §2.3.4 allows"
        )
    if not _DOMAIN.match(candidate):
        raise ValueError(
            f"{candidate!r} is not a domain name: labels may hold letters, digits, '-' and "
            f"'_', must not begin or end with '-', and are at most {MAX_DNS_LABEL} characters"
        )
    return candidate if candidate.endswith(".") else f"{candidate}."


def is_domain_name(text: str) -> bool:
    """Would :func:`domain_name` accept ``text``?"""
    try:
        domain_name(text)
    except ValueError:
        return False
    return True


def is_host_label(text: str) -> bool:
    """Is ``text`` already a legal, folded host label?

    The post-condition of :func:`sanitise_label`, kept as a predicate so the
    emitters and the tests can assert on it rather than re-deriving the rule.
    """
    return bool(text) and len(text) <= MAX_DNS_LABEL and _LDH_LABEL.match(text) is not None


def ansible_identifier(text: str, *, prefix: str = "", keep_lead: bool = False) -> str:
    """Fold ``text`` into an Ansible group name, optionally under ``prefix``.

    Ansible group names are used as Python identifiers in templates and as keys
    in the inventory JSON, so ``sites/north`` cannot be one. Everything outside
    ``[A-Za-z0-9_]`` becomes ``_``; an empty result becomes ``_``, which is a
    legal group nobody will mistake for a meaningful one.

    ``prefix`` is applied *after* folding and is itself assumed legal, so
    ``ansible_identifier("north", prefix="site_")`` is ``site_north`` and never
    starts with a digit however ``text`` was spelled.

    ``keep_lead`` keeps a leading digit that a bare identifier could not start
    with. It is for a caller that composes the result into a larger name and
    will supply the legal opening itself — folding ``sites/1-north`` segment by
    segment, for instance. Without it the ``1`` would be dropped and
    ``1-north`` and ``2-north`` would fold to the same group, which is a silent
    merge of two sites rather than a naming inconvenience.

    The result is lower-cased. Ansible group names *are* case-sensitive, which
    is exactly why: an estate that writes ``Cisco`` on one device and ``cisco``
    on the next would otherwise get two vendor groups, and a playbook targeting
    either would quietly skip half the fleet.
    """
    folded = _ANSIBLE_BAD.sub("_", transliterate(text).lower())
    if not prefix and not keep_lead:
        folded = _ANSIBLE_LEAD.sub("", folded)
    folded = folded.strip("_") or "_"
    return f"{prefix}{folded}"


def markdown_cell(text: str) -> str:
    """Escape ``text`` for a GitHub-flavoured Markdown table cell.

    A pipe would end the cell and a newline would end the row, so both have to
    go: the pipe is backslash-escaped, and a line break becomes ``<br>`` — the
    only way a Markdown table holds one. Backslashes are doubled first, so an
    escape this function introduces cannot be confused with one the inventory
    contained.
    """
    escaped = text.replace("\\", "\\\\").replace("|", "\\|")
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def csv_cell(value: object) -> str:
    """One CSV field as text: empty for ``None``, and inert in a spreadsheet.

    Two things RFC 4180 does not settle, both of which matter for a pull list:

    **What a missing value looks like.** An empty field, never the string
    ``None`` and never ``-``, so a spreadsheet reading the column as a number
    does not choke on a placeholder.

    **What happens when a cell starts with an operator.** Excel, LibreOffice
    and Google Sheets all evaluate a field beginning with ``=``, ``+``, ``@``,
    a tab or a carriage return as a *formula*, quoting or no quoting — so a
    cable labelled ``=HYPERLINK("http://…")`` becomes a live link in the one
    artefact here that is guaranteed to be opened in a spreadsheet
    (CWE-1236, formula injection). Such a cell is prefixed with an apostrophe,
    which every spreadsheet reads as "this is text" and which a CSV parser
    hands back as a literal character, so the change is visible rather than
    silent. ``-`` is guarded the same way *only* when what follows is not a
    number, so a negative value still reads as one.

    The quoting itself remains :mod:`csv`'s job — it is the one implementation
    of RFC 4180 worth trusting with an embedded quote, comma or newline.
    """
    if value is None:
        return ""
    text = str(value)
    if text[:1] in _FORMULA_LEAD or (text[:1] == "-" and not _NEGATIVE_NUMBER.match(text)):
        return f"'{text}"
    return text
