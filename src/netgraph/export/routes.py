"""The static routes of every router, as a script the router's host can run.

``spec.routes`` is the one part of an inventory that is *already* a list of
commands: a destination, a next hop, an egress, a table. Everything the other
emitters produce has to be translated into somebody else's model; this one is a
transcription, which is exactly why it is worth having — a routing plan that
lives only in a diagram gets typed into a box by hand, and the typing is where
the diagram and the network start to disagree.

The artefact is **one file holding one function per device**, plus a dispatcher::

    sh routes.sh                  # apply the routes of $(uname -n)
    sh routes.sh rtr-north-core-01   # or of a device named explicitly

Nothing runs until the dispatcher matches, so the file is inert on a machine the
inventory does not describe — it exits non-zero and says so, rather than applying
somebody else's routing table. A device is matched by its own name *and* by its
fully-qualified name, because ``uname -n`` reports the former and a deployment
pipeline usually holds the latter.

The commands are iproute2's, and every one is a ``replace``: an idempotent script
can be re-run after a partial failure, which ``add`` cannot (it fails on a route
that is already there and leaves the rest unapplied). A VRF becomes ``vrf
<name>``, which iproute2 resolves to the instance's table.

What it drops
-------------

Everything that is not a static route. Dynamic routing is a *protocol* — an
inventory can say a router is in AS 65001 and OSPF area 0, but the configuration
that makes it so is vendor syntax this emitter has no business inventing, so
``spec.routing`` is left to a template engine and the manifest says it was left.
A device with no ``spec.routes`` produces no function and is recorded as skipped;
so is any route whose ``dev`` or next hop the validator has already refused,
because a script that cannot be applied is worse than one that is missing a line.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from netgraph.export.context import ExportContext, elements_of
from netgraph.export.header import comment_header
from netgraph.export.manifest import Reason, Recorder
from netgraph.loader.inventory import short_name
from netgraph.models import Device, StaticRoute
from netgraph.render.graph import Layer, Node

__all__ = ["emit"]

#: Everything a POSIX shell function name may not hold. A fully-qualified name
#: is a path, so ``/`` and ``-`` both have to go; the result is prefixed, so a
#: name starting with a digit is legal without being folded further.
_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_]+")

#: Prefix of every generated function. Long and specific on purpose: the script
#: may be sourced into a shell that already has functions of its own.
_FUNCTION_PREFIX: Final = "netgraph_routes_"

#: How a single-quoted shell word carrying a quote is written. POSIX has no
#: escape inside single quotes, so the quote is closed, escaped and reopened.
_QUOTE_ESCAPE: Final = "'\\''"


@dataclass(frozen=True, slots=True)
class _Script:
    """One device's routes, as the function that applies them."""

    #: Fully-qualified name, for the dispatcher and the manifest.
    element: str
    function: str
    #: The command lines, in declaration order, comments included.
    lines: tuple[str, ...]

    @property
    def names(self) -> tuple[str, ...]:
        """The names the dispatcher matches, own name first, without repeats."""
        return tuple(dict.fromkeys((short_name(self.element), self.element)))


def emit(context: ExportContext) -> str:
    """Render the routing script, newline-terminated."""
    recorder = context.recorder
    nodes = elements_of(context.at(Layer.L1))
    recorder.considered = len(nodes)

    scripts: list[_Script] = []
    taken: set[str] = set()
    for node in nodes:
        script = _script_for(node, recorder, taken)
        if script is None:
            continue
        taken.add(script.function)
        scripts.append(script)
        recorder.emitted += 1

    lines = [*_header(context, scripts)]
    for script in scripts:
        lines.extend(["", *_function(script)])
    lines.extend(["", *_dispatcher(scripts)])
    return "".join(f"{line}\n" for line in lines)


def _script_for(node: Node, recorder: Recorder, taken: set[str]) -> _Script | None:
    """The function for one element, or ``None`` when it has no routes to apply."""
    element = node.element
    if not isinstance(element, Device):
        # A patch panel and an adapter have no routing table at all; saying so
        # per panel would fill the manifest with the obvious.
        return None
    routes = element.spec.routes
    if not routes:
        recorder.skip(
            node.fqn,
            Reason.NO_ROUTES,
            f"{node.kind} declares no 'spec.routes'; dynamic routing is not emitted here",
        )
        return None
    if element.spec.routing is not None:
        recorder.skip(
            node.fqn,
            Reason.NOT_REPRESENTABLE,
            "'spec.routing' is a protocol configuration, not a command; only the static "
            "routes of this device are in the script",
        )
    return _Script(
        element=node.fqn,
        function=_function_name(node.fqn, taken),
        lines=tuple(_commands(routes)),
    )


def _function_name(fqn: str, taken: set[str]) -> str:
    """A shell function name for ``fqn``, unique within the script.

    Two names that fold to one identifier — ``sites/a/rtr-1`` and
    ``sites-a/rtr_1`` — would otherwise silently share a function, and the second
    device's routes would replace the first's. The suffix is positional and
    therefore stable for a given inventory.
    """
    base = f"{_FUNCTION_PREFIX}{_UNSAFE.sub('_', fqn).strip('_').lower()}"
    if base not in taken:
        return base
    for counter in range(2, len(taken) + 3):  # pragma: no branch - always returns
        candidate = f"{base}_{counter}"
        if candidate not in taken:
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover - the loop always finds one


def _commands(routes: Sequence[StaticRoute]) -> Iterator[str]:
    """One iproute2 command per route, in declaration order.

    ``-4``/``-6`` is stated rather than inferred by iproute2 from the prefix: the
    two families have separate tables, and being explicit is what makes a line of
    the script readable on its own.
    """
    for route in routes:
        family = "-4" if route.prefix.version == 4 else "-6"
        words = ["ip", family, "route", "replace"]
        if route.blackhole:
            # ``blackhole`` is a route *type* and precedes the prefix, unlike
            # every other word here.
            words.append("blackhole")
        words.append(str(route.prefix))
        if route.via is not None:
            words.extend(["via", str(route.via)])
        if route.dev is not None:
            words.extend(["dev", _word(route.dev)])
        if route.vrf is not None:
            words.extend(["vrf", _word(route.vrf)])
        if route.metric is not None:
            words.extend(["metric", str(route.metric)])
        yield " ".join(words)


def _word(text: str) -> str:
    """One shell word, quoted when it is not already inert.

    An interface name is bounded by §4.1's grammar and a VRF name by
    ``ElementName``, so neither can hold a shell metacharacter today. Quoting
    anyway is the difference between a guarantee and an assumption, and it is the
    grammar of the *target* language that has to be satisfied here — the same
    reasoning the DOT and CSV emitters follow.
    """
    if text and all(char.isalnum() or char in "._-/" for char in text):
        return text
    return "'" + text.replace("'", _QUOTE_ESCAPE) + "'"


def _function(script: _Script) -> Iterator[str]:
    """One device's function: a banner, its commands, and a closing brace."""
    yield f"# {script.element}"
    yield f"{script.function}() {{"
    for line in script.lines:
        yield f"    {line}"
    yield "}"


def _dispatcher(scripts: Sequence[_Script]) -> Iterator[str]:
    """The ``case`` that runs exactly one device's function.

    ``$1`` wins over ``uname -n`` so that the script can be applied from a
    pipeline that knows which device it is deploying, and a machine whose host
    name does not match anything in the inventory is a hard error rather than a
    silent no-op: applying nothing looks identical to applying everything.
    """
    yield 'target="${1:-$(uname -n)}"'
    yield 'case "$target" in'
    for script in scripts:
        yield f"    {'|'.join(_pattern(name) for name in script.names)})"
        yield f"        {script.function}"
        yield "        ;;"
    yield "    *)"
    yield "        echo \"netgraph: no static routes declared for '$target'\" >&2"
    yield "        exit 1"
    yield "        ;;"
    yield "esac"


def _pattern(name: str) -> str:
    """One ``case`` pattern, with the glob characters of a name defused.

    ``?``, ``*`` and ``[`` are wildcards to a shell ``case``, and §4.1 allows
    none of them in a name — but the guarantee belongs in the emitter, not in the
    reader's memory, so the pattern is quoted whenever it holds one.
    """
    if any(char in name for char in "?*[]\\'\"$`"):
        return "'" + name.replace("'", _QUOTE_ESCAPE) + "'"
    return name


def _header(context: ExportContext, scripts: Sequence[_Script]) -> Iterator[str]:
    """The provenance banner, then the shell preamble."""
    yield "#!/bin/sh"
    yield from comment_header(
        "#",
        "routes",
        (
            f"{len(scripts)} device(s) with static routes, "
            f"{sum(len(script.lines) for script in scripts)} route(s).",
            "Apply with 'sh <file>' on the device, or 'sh <file> <device name>'.",
            "Only 'spec.routes' is emitted: BGP and OSPF configuration is vendor syntax",
            "and is deliberately not invented here. The manifest on stderr says what",
            "each device contributed and what was left out.",
        ),
    )
    # ``-e`` so a rejected route stops the script rather than leaving a table
    # half applied, and ``-u`` so an unset variable is an error rather than an
    # empty word in an ``ip`` command line.
    yield "set -eu"
