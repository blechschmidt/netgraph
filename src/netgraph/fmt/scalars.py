"""Deciding whether a string is written plain or quoted.

``docs/format.md`` states the rule: *quote only what YAML requires, plus what a
reader would otherwise misread*. The first half is not this module's problem —
ruamel's emitter analyses every scalar it writes and adds quotes when plain
would not round-trip syntactically, so asking for plain and getting ``'a: b'``
back is the emitter doing its job. What is decided here is the second half.

Two things make a plain scalar misleading, and one rule bounds what may be done
about them:

**A resolver disagreement.** netgraph's loader is YAML 1.2 about booleans
(``netgraph.loader.documents`` drops the 1.1 rule), so ``yes`` is the string
"yes" here and the boolean ``true`` in almost every other YAML reader. A value
whose plain form means one thing to netgraph and another to PyYAML's stock
resolver is quoted, so that both agree.

**A recognisable shape.** A MAC address is a class of value where *some*
members are misread and others are not: ``10:20:30:40:50:01`` is the base-60
integer 8041827001 to YAML 1.1, while ``b4:96:91:01:10:01`` is a string, and
nothing but counting tells the two apart. So the whole class is quoted, and a
reader never has to do the counting.

A version number is the other half of that story and needs no rule of its own.
``1.0`` is already a float to every reader including netgraph's, so it never
reaches this module as a string; if it was written ``'1.0'`` the resolver rule
below keeps the quotes, because dropping them would turn a string into a float.
``1.2.3`` is unambiguously a string to everyone. There is no third case — and a
shape rule for dotted numerals would be actively wrong, because ``10.1.10.1`` is
one too, and quoting every IP address in an inventory serves nobody.

**A change of meaning, which is the one thing quoting may not do.** Adding
quotes to ``1:02`` — which netgraph's own loader reads as the integer 62 —
would silently turn it into a string. So every rule above is gated on the plain
form already being a string to netgraph: :func:`quote_style` refuses to quote
anything else. ``netgraph fmt`` canonicalises documents; it does not repair
them, and a value the loader misreads is a job for ``netgraph validate``.

The decisions are made on the *tag a resolver assigns*, not by parsing, so they
cost a regex match rather than a document load.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Final

import yaml
from ruamel.yaml.scalarstring import (
    DoubleQuotedScalarString,
    FoldedScalarString,
    LiteralScalarString,
    ScalarString,
    SingleQuotedScalarString,
)

from netgraph.loader.documents import StrictSafeLoader, scan_tokens

__all__ = [
    "QUOTE",
    "canonical_string",
    "is_block_scalar",
    "is_quoted",
    "is_untouchable",
    "looks_like_mac",
    "plain",
    "plain_survives",
    "quote_style",
    "scalar_lines",
]

_STR_TAG: Final = "tag:yaml.org,2002:str"

#: The quoting style ``fmt`` emits when a value needs quotes. Single quotes
#: carry no escape sequences, so what is between them is what the value is.
QUOTE: Final = "'"

#: ``aa:bb:cc:dd:ee:ff`` and ``aa-bb-cc-dd-ee-ff``.
_MAC_COLON: Final = re.compile(
    r"\A[0-9A-Fa-f]{2}(?P<sep>[:-])(?:[0-9A-Fa-f]{2}(?P=sep)){4}[0-9A-Fa-f]{2}\Z"
)
#: The three-group form Cisco writes, ``aabb.ccdd.eeff``.
_MAC_DOTTED: Final = re.compile(r"\A[0-9A-Fa-f]{4}(?:\.[0-9A-Fa-f]{4}){2}\Z")


def looks_like_mac(text: str) -> bool:
    """Is ``text`` shaped like a MAC address?"""
    return bool(_MAC_COLON.match(text) or _MAC_DOTTED.match(text))


#: The bound strict loader, typed loosely: ``documents`` exports it through a
#: structural ``NodeLoader`` protocol that says nothing about resolver tables.
_STRICT_LOADER: Final[Any] = StrictSafeLoader


class _StrictResolver(yaml.resolver.Resolver):
    """PyYAML's resolver carrying netgraph's implicit-tag table.

    Borrowing the table off the bound loader rather than rebuilding it means
    this cannot drift from what the loader actually does — including the YAML
    1.2 boolean rule ``netgraph.loader.documents`` adds after class creation.
    """

    yaml_implicit_resolvers = _STRICT_LOADER.yaml_implicit_resolvers


#: How netgraph reads a plain scalar, and how the rest of the world does.
#: ``yaml.resolver.Resolver`` unmodified *is* ``yaml.SafeLoader``'s table.
_STRICT: Final = _StrictResolver()
_PERMISSIVE: Final = yaml.resolver.Resolver()


@functools.lru_cache(maxsize=4096)
def _tags(text: str) -> tuple[str, str]:
    """The implicit tags netgraph and a stock YAML 1.1 reader give ``text``.

    Cached: an inventory repeats a small vocabulary of scalars — ``ethernet``,
    ``true``, interface names — thousands of times over, and resolving one means
    walking a list of regexes.
    """
    return (_resolve(_STRICT, text), _resolve(_PERMISSIVE, text))


def _resolve(resolver: yaml.resolver.Resolver, text: str) -> str:
    """The implicit tag ``resolver`` gives the plain scalar ``text``."""
    # PyYAML ships no annotation for ``resolve``; the repository ignores the
    # same complaint at every other call into its resolver API.
    resolve: Any = resolver.resolve
    return str(resolve(yaml.ScalarNode, text, (True, False)))


def plain(value: str) -> str:
    """``value`` as a bare :class:`str`, whatever subclass it arrived as.

    ``str(value)`` is *not* this. Every ruamel scalar style is a subclass of
    ``str``, and ``str.__str__`` returns ``self`` — so ``str(quoted)`` hands back
    the same quoted object and the emitter keeps the quotes. Slicing is what
    actually produces a new plain ``str``.
    """
    return value[:] if type(value) is not str else value


def is_block_scalar(value: object) -> bool:
    """Is ``value`` a ``|`` or ``>`` scalar, whose style must be left alone?

    A block scalar is how multi-line prose is written — every ``description``
    longer than a line in ``examples/`` uses one — and its style is part of how
    the text reads. Rewriting it as a quoted one-liner would be a formatter
    making an editorial decision, so ``fmt`` does not.
    """
    return isinstance(value, LiteralScalarString | FoldedScalarString)


def is_untouchable(value: str) -> bool:
    """Is ``value`` a plain scalar netgraph does not read as a string at all?

    ``1:02`` written bare is the integer 62 to netgraph's loader, and ruamel
    hands it over as the string ``"1:02"`` because ruamel is YAML 1.2 and has no
    base-60 rule. Every restyling decision below is therefore off the table for
    it: quoting it would make it the string it looks like, which is very
    probably what the author meant and categorically not a formatter's call.
    The source text is written back byte for byte instead.
    """
    return type(value) is str and _tags(value)[0] != _STR_TAG


def quote_style(value: str) -> str | None:
    """The style ``value`` should be written in: :data:`QUOTE`, or ``None`` for plain.

    ``value`` is a string as ruamel loaded it, so a ruamel
    :class:`~ruamel.yaml.scalarstring.ScalarString` subclass means the source
    had quotes and a bare :class:`str` means it did not. That distinction is
    what makes unquoting safe: a value that arrived quoted is a string for
    certain, whereas a plain one is only a string if netgraph's resolver says so.
    """
    text = plain(value)
    was_quoted = isinstance(value, ScalarString)
    strict_tag, permissive_tag = _tags(text)

    if strict_tag != _STR_TAG:
        # Plain, ``text`` is not a string to netgraph today (``1:02`` is 62).
        # Quoting it would change that, so leave the source alone. Quoted, it
        # is a string and must stay one, so the quotes have to remain.
        return QUOTE if was_quoted else None

    if permissive_tag != _STR_TAG:
        # A YAML 1.1 reader would see a boolean, an integer or a timestamp
        # where netgraph sees a string: ``yes``, ``no``, ``on``, ``off``.
        return QUOTE
    if looks_like_mac(text):
        return QUOTE
    return None


@functools.lru_cache(maxsize=4096)
def plain_survives(text: str, *, in_flow: bool) -> bool:
    """Would ``text`` written unquoted still read back as ``text``?

    Asked of netgraph's own parser rather than answered from the YAML grammar,
    because the two YAML implementations in this process do not agree about it.
    ``::1/128`` unquoted inside a flow sequence is legal YAML 1.2 and ruamel
    emits it happily; PyYAML — which is what ``validate`` and ``render`` read
    with — refuses a plain scalar starting with ``:`` in flow context and fails
    the file. ruamel's emitter cannot be asked to apply PyYAML's rules, so the
    question goes to PyYAML directly.

    ``in_flow`` matters in both directions: flow rejects a leading ``:`` that
    block accepts, and block accepts a comma that flow would read as a
    separator. Probing the wrong one would quote half the descriptions in an
    inventory, or none of the addresses that need it.
    """
    probe = f"k: [{text}]" if in_flow else f"k: {text}"
    expected = {"k": [text]} if in_flow else {"k": text}
    try:
        return bool(yaml.load(probe, Loader=_STRICT_LOADER) == expected)
    except yaml.YAMLError:
        return False


def canonical_string(value: str, *, in_flow: bool = False) -> str:
    """``value`` re-styled per :func:`quote_style`, ready to hand to the emitter.

    "Plain" is returned as a bare :class:`str` rather than as ruamel's
    :class:`~ruamel.yaml.scalarstring.PlainScalarString`, and the difference
    matters. ``PlainScalarString`` is an *instruction*: it makes the emitter
    write the value unquoted whether or not unquoted is legal there, which turns
    ``addresses: ["::1/128"]`` into ``addresses: [::1/128]`` — not YAML. A bare
    string leaves the emitter free to do the analysis it is good at and add the
    quotes the syntax requires. What this module decides is only whether quotes
    are wanted for *meaning*; whether they are needed for *syntax* is not a
    judgement anyone should be making twice.

    See :func:`~netgraph.fmt.canonical.set_scalar` for the assignment side of
    the same point.

    A block scalar keeps its style, and so does any other value spanning more
    than one line: single quotes cannot hold a newline without the emitter
    folding it, and folding rewraps prose.
    """
    if is_block_scalar(value) or "\n" in value or is_untouchable(value):
        return value
    # ``quote_style`` is given ``value``, not ``text``: it reads the source's
    # quoting off the class, which ``plain`` has just stripped.
    style = quote_style(value)
    text = plain(value)
    if style is None and plain_survives(text, in_flow=in_flow):
        return text
    quoted: str = SingleQuotedScalarString(text)
    return quoted


def is_quoted(value: object) -> bool:
    """Was ``value`` given a quoted style? Used to measure a rendered width."""
    return isinstance(value, SingleQuotedScalarString | DoubleQuotedScalarString)


def scalar_lines(text: str) -> frozenset[int]:
    """Line numbers whose *start* lies inside a scalar rather than in the layout.

    Two of the formatter's jobs are line-level — strip trailing whitespace,
    collapse runs of blank lines — and both are wrong when applied to the
    continuation of a multi-line scalar, where a blank line is a ``\n`` in
    somebody's description and a trailing space in a ``|`` block is a character
    they typed. The same question comes up in :mod:`netgraph.fmt.verify`, where
    a ``#`` at the start of such a line is text and not a comment.

    Nothing in the text can answer it, so this asks the scanner, which already
    knows. The end of a scalar is exclusive when it lands in column 0 — exactly
    the case of a block scalar followed by a real comment or the next key —
    so the line the end mark points at is only counted when the scalar reaches
    into it.

    Returns:
        The zero-based line numbers, or an empty set for text the scanner
        refuses. That fallback is the conservative one for every caller: it
        means "assume nothing is protected", which is the behaviour these rules
        had before they knew about scalars at all.
    """
    covered: set[int] = set()
    try:
        # The same loader everything else reads with, so the fast scanner is
        # used where PyYAML has it: this runs three times per formatted file
        # (once in ``_tidy``, once per side in ``verify``) and libyaml scans
        # about seven times quicker. Both scanners produce identical marks --
        # ``tests/test_yaml_loader.py`` is where that is pinned down.
        tokens = list(scan_tokens(text))
    except yaml.YAMLError:
        return frozenset()
    for token in tokens:
        if not isinstance(token, yaml.ScalarToken):
            continue
        start, end = token.start_mark, token.end_mark
        covered.update(range(start.line + 1, end.line))
        if end.line > start.line and end.column > 0:
            covered.add(end.line)
    return frozenset(covered)
