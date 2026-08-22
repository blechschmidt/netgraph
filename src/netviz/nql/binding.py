"""Values a caller supplies for the ``$name`` holes in a query.

A query written by a person carries its values in it. A query written by a
*template* — a systemd unit that wants this host's address, a playbook that
wants this site's servers — has a hole where the value goes, and there are only
two ways to fill one: paste the value into the text, or name it and hand it over
separately.

Pasting is the way that breaks. ``"select device filter .name = '" + host + "'"``
means what it says only while nobody puts an apostrophe in a device name, and
the day somebody does the query either fails to parse or, worse, parses into a
different question. Quoting fixes that and nothing else: the *type* is still
whatever the concatenation produced, and a number arrives as text.

So a parameter is a token (:attr:`~netviz.nql.lexer.TokenKind.PARAM`), and this
module is the other half — the table it is filled from. Two passes over the same
mapping, deliberately kept apart:

* :func:`declare` reads the *types*, and the parser checks the query against
  them. ``$vlan`` bound to ``10`` is an int wherever it appears, so
  ``.name = $vlan`` is refused before the inventory is read.
* :func:`bind` reads the *values*, and the executor substitutes them.

The types come from the values rather than from a declaration the caller also
has to write, because a caller who has the value already knows the type and
saying it twice is a way to say it differently. A list binds to a set: ``.name
in $names`` is a parameter, not a loop.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from netviz.nql.types import Cardinality, ScalarKind, ValueType
from netviz.query.errors import QueryError

__all__ = ["MAX_PARAM_ITEMS", "Params", "bind", "declare", "read_assignment"]

#: What a caller may pass: one scalar, or a sequence of them.
Params = Mapping[str, Any]

#: How many items one parameter may carry. A parameter is a value somebody
#: supplies, and a set literal is capped by the parser's node budget; this is the
#: same order of magnitude, so that neither route is the cheap one.
MAX_PARAM_ITEMS: Final = 1024

#: Scalar Python types, in the order they are tested. ``bool`` comes before
#: ``int`` because it is one.
_KINDS: Final[tuple[tuple[type, ScalarKind], ...]] = (
    (bool, ScalarKind.BOOL),
    (int, ScalarKind.INT),
    (float, ScalarKind.FLOAT),
    (str, ScalarKind.STR),
)


def declare(params: Params | None) -> dict[str, ValueType]:
    """What each parameter's value is, as the parser needs to see it.

    A scalar is :attr:`~netviz.nql.types.Cardinality.ONE`; a sequence is
    :attr:`~netviz.nql.types.Cardinality.MANY`, of whatever its items share.
    ``None`` and an empty sequence are the empty set — compatible with
    everything, exactly as ``{}`` written in a query is — because a caller with
    nothing to say should get an honest "nothing matched" rather than a type
    error about a value that was never there.

    Raises:
        QueryError: A parameter is named badly, or holds something no scalar
            kind can carry.
    """
    return {name: _type_of(name, value) for name, value in _checked(params).items()}


def bind(params: Params | None) -> dict[str, tuple[Any, ...]]:
    """The same mapping as sets of values, as the executor substitutes them.

    Every value arrives in the form the world stores: an address is text, and so
    is anything else with a spelling of its own. What comes back is a tuple per
    name, so a scalar and a one-item list are the same thing to everything
    downstream — which is what makes ``.name in $names`` work whether the caller
    passed one name or ten.

    Raises:
        QueryError: As :func:`declare`.
    """
    return {name: _values_of(name, value) for name, value in _checked(params).items()}


def read_assignment(text: str) -> tuple[str, Any]:
    """Read one ``--param`` argument into a name and a value.

    Two spellings, and the difference is the type. ``name=VALUE`` is text,
    always, whatever it looks like — a device called ``0755`` is a device called
    ``0755`` and not an octal number. ``name:=JSON`` is a JSON value, which is
    how a number, a boolean or a list is said on a command line that has no
    types of its own.

    Raises:
        QueryError: No ``=``, an empty name, or a ``:=`` whose right side is not
            JSON.
    """
    name, separator, value = text.partition(":=")
    if not separator:
        name, separator, value = text.partition("=")
    if not separator:
        raise QueryError(
            f"{text!r} is not a parameter",
            help="write 'name=value' for text, or 'name:=JSON' for a number, a list or a boolean",
        )
    name = name.strip()
    if separator == ":=":
        try:
            return name, json.loads(value)
        except json.JSONDecodeError as error:
            raise QueryError(
                f"the value of '{name}' is not JSON: {error.msg}",
                help="quote a string: name:='\"srv-01\"'; or write name=srv-01 for text",
            ) from None
    return name, value


def _checked(params: Params | None) -> Mapping[str, Any]:
    """``params`` with every name checked, or an empty mapping.

    Raises:
        QueryError: A name that no ``$`` could ever spell.
    """
    if not params:
        return {}
    for name in params:
        if not name.isidentifier():
            raise QueryError(
                f"{name!r} is not a parameter name",
                help="a name is letters, digits and '_', and does not start with a digit",
            )
    return params


def _type_of(name: str, value: Any) -> ValueType:
    """The type ``value`` binds ``$name`` to."""
    if isinstance(value, (list, tuple)):
        kinds = {_scalar_of(name, one) for one in _items(name, value)}
        if len(kinds) > 1:
            spelled = ", ".join(sorted(str(kind) for kind in kinds))
            raise QueryError(
                f"the value of '{name}' mixes {spelled}",
                help="a parameter is a set of one kind of value",
            )
        return ValueType(scalar=kinds.pop() if kinds else None, card=Cardinality.MANY)
    if value is None:
        return ValueType(card=Cardinality.OPTIONAL)
    return ValueType(scalar=_scalar_of(name, value), card=Cardinality.ONE)


def _values_of(name: str, value: Any) -> tuple[Any, ...]:
    """The set ``value`` binds ``$name`` to."""
    if isinstance(value, (list, tuple)):
        return tuple(_scalar(name, one) for one in _items(name, value))
    if value is None:
        return ()
    return (_scalar(name, value),)


def _items(name: str, value: Sequence[Any]) -> Sequence[Any]:
    """``value``'s items, refusing a sequence too long to be a value.

    Raises:
        QueryError: Over :data:`MAX_PARAM_ITEMS`.
    """
    if len(value) > MAX_PARAM_ITEMS:
        raise QueryError(
            f"the value of '{name}' holds {len(value)} items, over the "
            f"{MAX_PARAM_ITEMS}-item limit",
            help="ask the question with a filter rather than with a list of everything",
        )
    return value


def _scalar(name: str, value: Any) -> Any:
    """``value`` as the world spells it.

    Raises:
        QueryError: Not a scalar.
    """
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(value)
    if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return str(value)
    _scalar_of(name, value)
    return value


def _scalar_of(name: str, value: Any) -> ScalarKind:
    """Which kind of leaf ``value`` is.

    An address arrives as text, which is what the world holds and therefore what
    every comparison against it already expects: ``in`` reads a prefix out of a
    string, and does not need to be told one is coming.

    Raises:
        QueryError: Not a scalar.
    """
    if isinstance(
        value,
        (
            ipaddress.IPv4Address,
            ipaddress.IPv6Address,
            ipaddress.IPv4Network,
            ipaddress.IPv6Network,
        ),
    ):
        return ScalarKind.STR
    for python_type, kind in _KINDS:
        if isinstance(value, python_type):
            return kind
    raise QueryError(
        f"the value of '{name}' is a {type(value).__name__}, which is not a value a query can hold",
        help="a parameter is text, a number, a boolean, or a list of one of those",
    )
