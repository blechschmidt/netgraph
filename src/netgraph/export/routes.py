"""The routing of every router, as a script the router's host can run.

``spec.routes`` is the one part of an inventory that is *already* a list of
commands: a destination, a next hop, an egress, a table. Everything the other
emitters produce has to be translated into somebody else's model; this one is a
transcription, which is exactly why it is worth having — a routing plan that
lives only in a diagram gets typed into a box by hand, and the typing is where
the diagram and the network start to disagree.

``spec.routing_policy`` is the same statement one level up (§16.6): the rules
that decide *which table* a packet is routed by. They are emitted here, with
their routes, because the two halves are useless apart — a table nobody selects
holds routes nobody consults, and a rule selecting a table nobody filled routes
nothing. A device declaring either gets a function; one declaring neither gets
none.

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

``ip rule`` has no ``replace``, so a policy rule is written as a ``del`` of
whatever sits at its priority followed by an ``add`` — which is the same
guarantee by other means, since a priority holds one rule (``NG-F020``) and the
``del`` is allowed to fail. A declared table is written as its **number**, with
its name in a trailing comment: a name works only if somebody has put it in
``/etc/iproute2/rt_tables``, and a script that edits that file is a script that
changes a machine outside the routing table it was asked to apply.

What it drops
-------------

Everything that is not a route or a rule. Dynamic routing is a *protocol* — an
inventory can say a router is in AS 65001 and OSPF area 0, but the configuration
that makes it so is vendor syntax this emitter has no business inventing, so
``spec.routing`` is left to a template engine and the manifest says it was left.
A device with neither ``spec.routes`` nor ``spec.routing_policy`` produces no
function and is recorded as skipped; so is any route whose ``dev`` or next hop
the validator has already refused, because a script that cannot be applied is
worse than one that is missing a line.
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
from netgraph.models import Device, DeviceSpec, PolicyAction, PolicyRule
from netgraph.models.routing import AddressFamily
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
    """One device's routing, as the function that applies it."""

    #: Fully-qualified name, for the dispatcher and the manifest.
    element: str
    function: str
    #: The lines of the function body, in the order they run: comments, blank
    #: separators and commands alike, since the body is written verbatim.
    lines: tuple[str, ...]

    @property
    def names(self) -> tuple[str, ...]:
        """The names the dispatcher matches, own name first, without repeats."""
        return tuple(dict.fromkeys((short_name(self.element), self.element)))

    @property
    def commands(self) -> int:
        """How many of the lines actually run something, for the banner's count."""
        return sum(1 for line in self.lines if line and not line.startswith("#"))


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
    """The function for one element, or ``None`` when it has nothing to apply."""
    element = node.element
    if not isinstance(element, Device):
        # A patch panel and an adapter have no routing table at all; saying so
        # per panel would fill the manifest with the obvious.
        return None
    spec = element.spec
    routes, policy = spec.routes, spec.routing_policy
    if not routes and not policy:
        recorder.skip(
            node.fqn,
            Reason.NO_ROUTES,
            f"{node.kind} declares no 'spec.routes' and no 'spec.routing_policy'; dynamic "
            f"routing is not emitted here",
        )
        return None
    if spec.routing is not None:
        recorder.skip(
            node.fqn,
            Reason.NOT_REPRESENTABLE,
            "'spec.routing' is a protocol configuration, not a command; only the static "
            "routes and the policy database of this device are in the script",
        )
    return _Script(
        element=node.fqn,
        function=_function_name(node.fqn, taken),
        lines=tuple(_commands(spec)),
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


def _commands(spec: DeviceSpec) -> Iterator[str]:
    """One device's routing, as commands: the routes, then the policy over them.

    The policy comes second because it is what *reaches* the tables the routes
    were just put in. Applied the other way round there is a window — however
    short — in which a rule diverts traffic to a table that is still empty, and
    the packets crossing it are dropped rather than routed by ``main``.
    """
    yield from _route_commands(spec)
    if spec.routing_policy:
        yield from _rule_commands(spec)


def _route_commands(spec: DeviceSpec) -> Iterator[str]:
    """One iproute2 command per route, in declaration order.

    ``-4``/``-6`` is stated rather than inferred by iproute2 from the prefix: the
    two families have separate tables, and being explicit is what makes a line of
    the script readable on its own.
    """
    for route in spec.routes:
        words = ["ip", _family_flag(route.prefix.version), "route", "replace"]
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
        table, comment = _table_words(spec, route.table)
        words.extend(table)
        if route.metric is not None:
            words.extend(["metric", str(route.metric)])
        yield " ".join(words) + comment


def _rule_commands(spec: DeviceSpec) -> Iterator[str]:
    """The policy database, in the order the device walks it (§16.6).

    Each rule is a ``del`` of the priority followed by an ``add`` of the rule,
    which is how ``ip rule`` is made idempotent: it has no ``replace``, a
    priority holds one rule (``NG-F020``), and a ``del`` that matches nothing is
    the normal case on a first run rather than a failure. ``|| :`` is what says
    so to the ``set -e`` at the top of the file.

    A rule that names no family is written into both databases, because that is
    what it means: the selectors it carries are family-independent, so the
    operator would have typed ``ip rule`` and ``ip -6 rule``.
    """
    yield ""
    yield "# routing policy database (spec.routing_policy)"
    for family in AddressFamily:
        for rule in spec.policy_in(family):
            flag = _family_flag(4 if family is AddressFamily.IPV4 else 6)
            priority = str(rule.priority)
            yield f"ip {flag} rule del priority {priority} 2>/dev/null || :"
            words = ["ip", flag, "rule", "add", "priority", priority]
            if rule.invert:
                words.append("not")
            words.extend(_selector_words(rule))
            target, comment = _target_words(spec, rule)
            words.extend(target)
            yield " ".join(words) + comment


def _selector_words(rule: PolicyRule) -> Iterator[str]:
    """The selectors, in the order ``ip rule`` reads them.

    ``from all`` is stated for a rule with no selector at all. iproute2 defaults
    to it, but a bare ``ip rule add priority 32766 lookup main`` reads like a
    line somebody forgot to finish, and the whole point of the terminator is
    that it is deliberate.
    """
    if not rule.selectors:
        yield from ("from", "all")
        return
    if rule.src is not None:
        yield from ("from", str(rule.src))
    if rule.dst is not None:
        yield from ("to", str(rule.dst))
    if rule.iif is not None:
        yield from ("iif", _word(rule.iif))
    if rule.oif is not None:
        yield from ("oif", _word(rule.oif))
    if rule.fwmark is not None:
        yield from ("fwmark", rule.fwmark)
    if rule.dscp is not None:
        yield from ("dsfield", f"0x{rule.dscp << 2:02x}")


def _target_words(spec: DeviceSpec, rule: PolicyRule) -> tuple[list[str], str]:
    """What the rule does, and the comment naming the table it does it to."""
    if rule.action is PolicyAction.LOOKUP:
        assert rule.table is not None  # NG-F016: a lookup always names one
        table, comment = _table_words(spec, rule.table, keyword="lookup")
        return table, comment
    if rule.action is PolicyAction.GOTO:
        return ["goto", str(rule.goto)], ""
    return [rule.action.value], ""


def _table_words(
    spec: DeviceSpec, name: str | None, *, keyword: str = "table"
) -> tuple[list[str], str]:
    """``['table', '100']`` and ``'  # uplink-b'`` — how a table is named on the wire.

    By number wherever the inventory knows one, because a *name* only resolves if
    somebody has put it in ``/etc/iproute2/rt_tables`` and this script does not
    edit that file. The name goes into a trailing comment so the line still reads
    as the inventory wrote it.

    A VRF is the exception: iproute2 resolves ``vrf blue`` itself, and netgraph
    has no number for it to resolve to (§16.1 records a route distinguisher, not
    a table id), so the name is what goes on the line.
    """
    if name is None:
        return [], ""
    number = spec.table_id(name)
    if number is None:
        return [keyword, _word(name)], ""
    return [keyword, str(number)], f"  # {name}"


def _family_flag(version: int) -> str:
    """``-4`` or ``-6``, from an address family's version number."""
    return "-4" if version == 4 else "-6"


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
        # An empty separator stays empty: an indented blank line is trailing
        # whitespace, which every linter of the generated script would flag.
        yield f"    {line}" if line else ""
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
    yield "        echo \"netgraph: no routing declared for '$target'\" >&2"
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
            f"{len(scripts)} device(s) with routing to apply, "
            f"{sum(script.commands for script in scripts)} command(s).",
            "Apply with 'sh <file>' on the device, or 'sh <file> <device name>'.",
            "Only 'spec.routes' and 'spec.routing_policy' are emitted: BGP and OSPF",
            "configuration is vendor syntax and is deliberately not invented here. The",
            "manifest on stderr says what each device contributed and what was left out.",
            "A routing table is named by number; its name from the inventory is in the",
            "trailing comment, since a name resolves only through /etc/iproute2/rt_tables.",
        ),
    )
    # ``-e`` so a rejected route stops the script rather than leaving a table
    # half applied, and ``-u`` so an unset variable is an error rather than an
    # empty word in an ``ip`` command line.
    yield "set -eu"
