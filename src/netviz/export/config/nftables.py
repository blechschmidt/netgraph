"""``nftables`` — the ruleset a Linux box actually loads, from ``spec.firewall``.

The other five dialects here describe how a device is *wired*: addresses, links,
routes, sessions. This one describes what it *refuses*, which is the half of a
device's configuration nothing else in netviz could write — and the half where
a document beside the truth is most expensive, because a firewall that disagrees
with its diagram fails silently in exactly one direction.

The artefact is one ``etc/nftables.conf``: a single ``table inet netviz``
holding the three base chains, whatever NAT chains the translations need, and a
``set`` per zone naming the interfaces in it.

Six decisions are worth stating.

**One table, and it is ours.** Everything is written into ``table inet
netviz``, preceded by a ``destroy table`` of the same name. nftables tables are
independent — the kernel evaluates every base chain of every table and the
packet needs a verdict from all of them — so replacing one table leaves anything
else on the box exactly as it was, and re-running the file is idempotent without
flushing a ruleset netviz did not write. ``inet`` rather than ``ip`` and
``ip6`` because a rule that names no family is installed in both (§24.2), which
is what ``inet`` means and what two tables would have to duplicate.

**A zone is a set.** ``spec.zones`` becomes ``set zone_lan { type ifname; }``
with the interfaces as elements, and a rule selecting on a zone matches
``iifname @zone_lan``. Not an inline list: the set is what makes the generated
file readable as *the policy the inventory states*, and adding an interface to a
zone changes one line rather than every rule that names it.

**The hook comes from the zones.** ``dst_zone: local`` is the ``input`` chain,
``src_zone: local`` is ``output``, two real zones are ``forward``, and a rule
naming one real zone goes in both chains it could be in (§24.2). So a rule may
be written more than once — it is one statement about the packet, and nftables
has no chain that means "either of these two".

**The default is a policy, and the policy is stated.** Each base chain carries
``policy drop`` or ``policy accept`` from ``spec.firewall.default_*``, plus a
trailing comment naming which key it came from. A base chain with no policy
clause defaults to ``accept``, which is the one thing a generator must never
leave to a default.

**Nothing is invented.** There is no ``ct state established,related accept``
that the inventory did not ask for, no loopback exemption, no rate limit and no
logging beyond an ``action: log``. Every one of those is a rule somebody has to
have written down, and a generated file that quietly opens a hole is worse than
one that quietly closes one: the second is noticed the same afternoon.

**What it will not write, it refuses.** ``invert`` is the one selector nftables
has no single spelling for — ``!=`` negates *one* expression and ``spec.firewall``
inverts the whole set — so a rule using it is an
:class:`~netviz.export.config.model.Unsupported` rather than a rule that
matches the opposite of what the document says.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Final

from netviz.errors import count_text
from netviz.export.config.header import config_header
from netviz.export.config.model import ConfigFile, Unsupported
from netviz.export.config.plan import DevicePlan
from netviz.export.manifest import Reason, Recorder
from netviz.models import (
    LOCAL_ZONE,
    FirewallAction,
    FirewallConfig,
    FirewallHook,
    FirewallRule,
    NatRule,
    NatType,
    Zone,
)

__all__ = [
    "DIRECTORY",
    "MAX_COMMENT",
    "PATH",
    "TABLE",
    "declines",
    "files",
    "limits",
    "selects",
]

#: Where the systemd unit ``nftables.service`` looks, on every distribution that
#: ships one. A single file rather than an ``nftables.d`` fragment: the fragment
#: convention is Debian's alone, and a file at the canonical path is one an
#: operator can diff against what the box is running.
DIRECTORY: Final = "etc"
PATH: Final = f"{DIRECTORY}/nftables.conf"

#: The table everything is written into, and the only thing this file destroys.
TABLE: Final = "netviz"

#: How long a comment nftables stores. ``NFTNL_UDATA_COMMENT`` is 128 bytes and
#: nft refuses a longer one outright, so a description written for a human has to
#: be cut here rather than at the point where the ruleset fails to load.
MAX_COMMENT: Final = 128

#: How a hook's default action is spelled as a chain policy. nftables allows
#: only ``accept`` and ``drop`` there — ``reject`` needs a rule, because it has
#: to build a packet — so a ``reject`` default becomes ``policy drop`` with an
#: explicit trailing ``reject`` rule, which is the same behaviour written out.
_CHAIN_POLICY: Final[dict[FirewallAction, str]] = {
    FirewallAction.ACCEPT: "accept",
    FirewallAction.DROP: "drop",
    FirewallAction.REJECT: "drop",
}

#: nftables' priority for each base chain, as ``nft`` itself names them. The
#: words rather than the numbers: ``filter`` is -1 in ``inet`` and 0 in ``bridge``,
#: and a file that spelled the number would be right for one family only.
_HOOK_PRIORITY: Final[dict[FirewallHook, str]] = {
    FirewallHook.INPUT: "filter",
    FirewallHook.FORWARD: "filter",
    FirewallHook.OUTPUT: "filter",
}

#: What the interface selector of a rule is, per hook: on the way in a packet
#: has an ingress interface and no egress one decided yet, and on the way out
#: the reverse. ``forward`` has both, which is what makes a zone pair expressible
#: there in one rule.
_ZONE_KEYWORD: Final[dict[FirewallHook, tuple[str, str]]] = {
    FirewallHook.INPUT: ("iifname", ""),
    FirewallHook.FORWARD: ("iifname", "oifname"),
    FirewallHook.OUTPUT: ("", "oifname"),
}


def selects(plan: DevicePlan) -> bool:
    """Does this device declare a firewall to write?

    ``spec.firewall``, not ``spec.zones``: a zone table with no policy over it is
    a partition nobody has written rules for yet, and the ruleset it would
    generate is three empty chains and their defaults — which is a real
    configuration and a surprising thing to hand somebody who wrote no rules.
    """
    return plan.device.spec.firewall is not None


def declines(plan: DevicePlan) -> str:
    """Why a device this dialect did not select got no file."""
    zones = plan.device.spec.zones
    written = (
        f" it declares {count_text(len(zones), 'zone')} and no policy over them, and a "
        f"ruleset of nothing but chain defaults is not what that says"
        if zones
        else ""
    )
    return f"{plan.name} declares no 'spec.firewall';{written or ' there is no ruleset to write'}"


def limits(plan: DevicePlan) -> tuple[Unsupported, ...]:
    """Everything nftables would have had to contradict for ``plan``."""
    return tuple(_limits(plan))


def _limits(plan: DevicePlan) -> Iterator[Unsupported]:
    policy = plan.device.spec.firewall
    if policy is None:  # pragma: no cover - ``selects`` decided otherwise
        return
    for index, rule in enumerate(policy.rules):
        if not rule.invert:
            continue
        yield Unsupported(
            element=plan.fqn,
            field=plan.field("firewall", "rules", index, "invert"),
            detail=(
                f"rule {rule.priority} matches everything its selectors do not, and nftables "
                f"negates one expression at a time ('!=') rather than a selector set; the "
                f"rule would be written matching the opposite of what the inventory states"
            ),
        )


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #


def files(plan: DevicePlan, recorder: Recorder) -> tuple[ConfigFile, ...]:
    """The one ruleset file for this device."""
    policy = plan.device.spec.firewall
    assert policy is not None  # ``selects`` said so
    _record_out_of_remit(plan, policy, recorder)
    return (ConfigFile(path=PATH, content=_content(plan, policy)),)


def _record_out_of_remit(plan: DevicePlan, policy: FirewallConfig, recorder: Recorder) -> None:
    """What this dialect is simply not the tool for, named with the tool that is."""
    if plan.device.spec.routing_policy:
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            f"{plan.field('routing_policy')}: the "
            + count_text(len(plan.device.spec.routing_policy), "rule")
            + " of the policy database decide which table a packet is routed by, which is "
            "iproute2's job rather than the firewall's; 'netviz export routes' writes them, "
            "and the marks this file sets are what they match",
        )
    for zone in plan.device.spec.zones:
        if zone.interfaces:
            continue
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            f"zone {zone.name!r} holds no interface, so its set is written empty and every "
            f"rule naming it matches nothing (W150)",
        )
    if policy.default_output.denies and not any(
        rule.in_hook(FirewallHook.OUTPUT) and rule.action.permits for rule in policy.rules
    ):
        # Not a refusal: it is exactly what the document says. But it is the one
        # generated file that can lock an operator out of the box it is applied
        # to, and the manifest is where that gets said before it is applied.
        recorder.skip(
            plan.fqn,
            Reason.NOT_REPRESENTABLE,
            f"{plan.field('firewall', 'default_output')} is "
            f"{policy.default_output.value!r} and no rule permits anything out of this "
            f"machine; the generated ruleset stops the box answering a DNS query or "
            f"reaching a package mirror, which is what the inventory states",
        )


def _content(plan: DevicePlan, policy: FirewallConfig) -> str:
    """The whole file: banner, teardown, then the one table."""
    notes = [
        f"Applies with 'nft -f {PATH}'. Only 'table inet {TABLE}' is replaced;",
        "anything else on the box is left exactly as it was.",
        "",
        "Nothing here is inferred. There is no connection-tracking rule, no loopback",
        "exemption and no rate limit that 'spec.firewall' did not state.",
    ]
    lines = [*config_header("#", "nftables", plan, notes=notes)]
    # ``destroy`` rather than ``delete``: it succeeds on a table that does not
    # exist, which is the first run, and ``delete`` does not.
    lines.append(f"destroy table inet {TABLE}")
    lines.append("")
    lines.append(f"table inet {TABLE} {{")
    lines.extend(_sets(plan.device.spec.zones))
    lines.extend(_filter_chains(plan, policy))
    lines.extend(_nat_chains(plan, policy))
    lines.append("}")
    return "".join(f"{line}\n" for line in lines)


def _sets(zones: Sequence[Zone]) -> Iterator[str]:
    """One ``set`` per declared zone, holding the interfaces in it (§24.1).

    ``type ifname`` rather than a list inlined into every rule, so that the
    generated file states the partition once and a zone gaining an interface is
    one line of diff. An empty zone still gets its set: the rules naming it
    reference it, and a missing set is a parse error rather than a rule that
    matches nothing.
    """
    for zone in zones:
        yield f"    set {_set_name(zone.name)} {{"
        yield "        type ifname"
        if zone.description:
            yield f"        comment {_string(zone.description.strip())}"
        if zone.interfaces:
            yield "        elements = { " + ", ".join(_string(n) for n in zone.interfaces) + " }"
        yield "    }"
        yield ""


def _filter_chains(plan: DevicePlan, policy: FirewallConfig) -> Iterator[str]:
    """The three base chains, each with its stated default and its rules."""
    for hook in FirewallHook:
        default = policy.default_for(hook)
        key = f"default_{hook.value}"
        yield f"    chain {hook.value} {{"
        yield (
            f"        type filter hook {hook.value} priority {_HOOK_PRIORITY[hook]}; "
            f"policy {_CHAIN_POLICY[default]};  # {plan.field('firewall', key)}"
        )
        for rule in policy.rules_in(hook):
            if rule.invert:  # pragma: no cover - ``limits`` refused the device
                continue
            yield f"        {_rule(rule, hook)}"
        if default is FirewallAction.REJECT:
            # A chain policy cannot be ``reject`` -- it has to build a packet --
            # so the policy is ``drop`` and the refusal is the last rule.
            yield f"        reject comment {_string(f'{key}: reject')}"
        yield "    }"
        yield ""


def _nat_chains(plan: DevicePlan, policy: FirewallConfig) -> Iterator[str]:
    """The two NAT chains, written only when a translation needs them (§24.4).

    A base chain in the ``nat`` type is not free: registering one turns
    connection tracking on for every packet the hook sees, so a device that
    translates nothing does not get an empty pair of them.
    """
    source = [rule for rule in policy.nat if rule.type.is_source]
    destination = [rule for rule in policy.nat if not rule.type.is_source]
    # ``dstnat`` and ``srcnat`` are nft's own names for -100 and 100, and they
    # are the priorities the two hooks *must* have: a destination translation
    # runs before routing decides where the packet goes, and a source one after
    # the egress interface is known. Written as the words rather than the
    # numbers, for the reason :data:`_HOOK_PRIORITY` is.
    for hook, priority, entries in (
        ("prerouting", "dstnat", destination),
        ("postrouting", "srcnat", source),
    ):
        if not entries:
            continue
        yield f"    chain {hook} {{"
        yield f"        type nat hook {hook} priority {priority}; policy accept;"
        for entry in entries:
            yield f"        {_nat_rule(entry)}"
        yield "    }"
        yield ""


def _rule(rule: FirewallRule, hook: FirewallHook) -> str:
    """One filter rule, in the order ``nft`` reads the expressions.

    Zones first, because that is what the rule is *about*; then the addresses,
    the protocol and its ports, then connection state; then the verdict. The
    priority and any description go into the trailing ``comment``, which is what
    ``nft list ruleset`` prints back — so the running box can be read against the
    inventory line by line.
    """
    words = [*_zone_words(rule, hook), *_match_words(rule), *_verdict_words(rule)]
    words.append(f"comment {_string(_comment(rule))}")
    return " ".join(words)


def _zone_words(rule: FirewallRule, hook: FirewallHook) -> Iterator[str]:
    """``iifname @zone_lan oifname @zone_wan`` — as much of it as the hook has.

    :data:`LOCAL_ZONE` produces nothing: "the packet terminates here" is what the
    ``input`` chain already means, and an interface selector for it would be a
    second, weaker statement of the same thing.
    """
    ingress, egress = _ZONE_KEYWORD[hook]
    for keyword, zone in ((ingress, rule.src_zone), (egress, rule.dst_zone)):
        if keyword and zone is not None and zone != LOCAL_ZONE:
            yield f"{keyword} @{_set_name(zone)}"


def _match_words(rule: FirewallRule) -> Iterator[str]:
    """The selectors below the zone: addresses, protocol, ports, state."""
    for keyword, prefix in (("saddr", rule.src), ("daddr", rule.dst)):
        if prefix is not None:
            yield f"{'ip' if prefix.version == 4 else 'ip6'} {keyword} {prefix}"
    if rule.protocol is not None:
        # ``meta l4proto`` rather than ``ip protocol``: the table is ``inet``, so
        # a rule naming the family's own keyword would only ever match one of the
        # two -- which is the opposite of what an unstated family means (§24.2).
        yield f"meta l4proto {rule.protocol.value}"
    for keyword, ports in (("sport", rule.src_ports), ("dport", rule.dst_ports)):
        if ports and rule.protocol is not None:
            yield f"{rule.protocol.value} {keyword} {_set_of(ports)}"
    if rule.ct_state:
        yield "ct state " + ",".join(state.value for state in rule.ct_state)


def _verdict_words(rule: FirewallRule) -> Iterator[str]:
    """What the rule does: a verdict, a mark, or a log that carries on."""
    if rule.action is FirewallAction.MARK:
        # ``meta mark set`` and no verdict: the packet carries on to the next
        # rule, which is the whole reason 'mark' is not terminal (§24.2).
        assert rule.mark is not None  # NV-B005: a 'mark' rule always names one
        yield f"meta mark set {_mark_expression(rule.mark)}"
        return
    if rule.action is FirewallAction.LOG:
        yield "log" + (f" prefix {_string(rule.log_prefix)}" if rule.log_prefix else "")
        return
    yield rule.action.value


def _mark_expression(mark: str) -> str:
    """``0x1``, or the read-modify-write a *masked* mark actually is.

    ``meta mark set 0x1`` replaces the whole 32-bit word, which is right for a
    mark written without a mask. A mask says the opposite — *these bits, and
    leave the rest alone* — and nft has no assignment that means it, because
    there is no such operation: the packet's existing mark has to be read,
    cleared under the mask and reassembled. ``meta mark set meta mark and
    0xffffff00 or 0x1`` is that, spelled out, and it is what makes a second
    marking rule able to coexist with the first.
    """
    value, separator, mask = mark.partition("/")
    if not separator:
        return value
    return f"meta mark and {hex(~int(mask, 16) & 0xFFFFFFFF)} or {value}"


def _nat_rule(rule: NatRule) -> str:
    """One translation, in the chain its direction put it in (§24.4)."""
    words: list[str] = []
    keyword = "oifname" if rule.type.is_source else "iifname"
    zone = rule.dst_zone if rule.type.is_source else rule.src_zone
    if zone is not None and zone != LOCAL_ZONE:
        words.append(f"{keyword} @{_set_name(zone)}")
    for name, prefix in (("saddr", rule.src), ("daddr", rule.dst)):
        if prefix is not None:
            words.append(f"{'ip' if prefix.version == 4 else 'ip6'} {name} {prefix}")
    if rule.protocol is not None:
        words.append(f"meta l4proto {rule.protocol.value}")
    if rule.dst_ports and rule.protocol is not None:
        words.append(f"{rule.protocol.value} dport {_set_of(rule.dst_ports)}")
    words.append(_translation(rule))
    words.append(f"comment {_string(rule.name or rule.describe())}")
    return " ".join(words)


def _translation(rule: NatRule) -> str:
    """``snat ip to 203.0.113.5``, ``masquerade``, ``dnat ip to 10.0.0.5:8443``.

    The family is written between the verb and the address, and it has to be:
    ``inet`` holds both families and nft refuses a bare ``dnat to`` there,
    because the address it is given could be either. ``masquerade`` and
    ``redirect`` name no address, so there is nothing to disambiguate and nft
    takes them as they are.
    """
    if rule.type is NatType.MASQUERADE:
        return "masquerade"
    if rule.to_address is None:  # ``redirect``: a port and nothing else
        return f"{rule.type.value} to :{rule.to_port}"
    family = "ip" if rule.to_address.version == 4 else "ip6"
    port = "" if rule.to_port is None else f":{rule.to_port}"
    return f"{rule.type.value} {family} to {rule.to_address}{port}"


def _comment(rule: FirewallRule) -> str:
    """``100 lab VPN egress`` — the priority, then whatever the document said.

    The priority is always there, because it is the rule's identity in the
    inventory (``NV-B008``) and the one thing that lets a line of ``nft list
    ruleset`` be found in a YAML file.
    """
    text = rule.name or rule.description or ""
    return f"{rule.priority} {text}".strip() if text else str(rule.priority)


def _set_name(zone: str) -> str:
    """``zone_lan`` — an nftables identifier for a zone name.

    Prefixed so a zone called ``ct`` or ``ip`` cannot collide with a keyword,
    and folded because §4.1 allows a ``.`` and a ``-`` in an element name where
    an nftables identifier allows neither.
    """
    return "zone_" + "".join(character if character.isalnum() else "_" for character in zone)


def _set_of(values: Sequence[str]) -> str:
    """``{ 22, 443 }`` — an anonymous set, or the bare value when there is one."""
    return values[0] if len(values) == 1 else "{ " + ", ".join(values) + " }"


def _string(text: str) -> str:
    """A double-quoted nftables string: the one form nft's lexer takes.

    nft has **no escape sequence inside a string**. A quote cannot be written at
    all, so one is folded to an apostrophe rather than escaped — an escape would
    produce a file nft refuses to parse, which is a worse answer than a comment
    that reads slightly differently from the description it came from. A
    backslash goes the same way, and a newline becomes a space, since nft has no
    continuation either.

    The result is bounded by :data:`MAX_COMMENT`. Whitespace is otherwise kept as
    written: a ``log_prefix`` conventionally *ends* with a space or a colon —
    ``"dropped: "`` — so trimming it would change what the box prints.
    """
    folded = " ".join(text.splitlines()).replace('"', "'").replace("\\", "/")
    if len(folded) > MAX_COMMENT:
        folded = folded[: MAX_COMMENT - 1].rstrip() + "\u2026"
    return f'"{folded}"'
