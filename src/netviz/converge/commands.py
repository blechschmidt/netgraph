"""Turning an intent into lines: netviz's own imperative grammar, and its inverse.

Every intent has to be readable before it is runnable, and the vocabulary here
is the one place that is decided. It is not invented: it is the ``interfaces``
dialect of :mod:`netviz.export.config.neutral` -- the same stanza names
(``vlan-access``, ``ipv4-address``, ``vlan-tagged``, ``member``) -- with a verb
in front. A person who has read one of those files can read one of these
scripts, and a person who reads one of these scripts knows which field of which
document they are looking at.

Four verbs, and their meaning is the same for every noun::

    create interface eno1.30 type vlan parent eno1
    delete interface eno1.30
    set    interface eno1 mtu 1500
    unset  interface eno1 mtu
    add    interface eno1 ipv4-address 192.168.10.20/24
    remove interface eno1 ipv4-address 192.168.10.20/24

``set``/``unset`` are for a field that holds one value; ``add``/``remove`` for
one that holds a set. That distinction is what makes the *inverse* mechanical
rather than a second table: the inverse of ``add`` is ``remove``, and the
inverse of ``set X`` is ``set`` back to what the capture reported or ``unset``
when it reported nothing. ``--rollback`` is generated from the same intents as
the forward plan, so the two cannot disagree about what a change was.

Nothing in this module reads the inventory or the capture. An intent already
carries the declared value and the observed one; if it did not, a rollback could
be built from a state nobody measured, which is the one thing a rollback must
never be.
"""

from __future__ import annotations

from typing import Final

from netviz.converge.intent import Intent, IntentKind, article
from netviz.converge.model import Command

__all__ = ["describe", "render", "revert"]

#: Interface attribute names, as the ``interfaces`` dialect spells them. The
#: left-hand side is what a drift finding calls the field.
_ATTRIBUTES: Final[dict[str, str]] = {
    "mac": "mac",
    "mtu": "mtu",
    "parent": "parent",
    "enabled": "enabled",
    "mode": "vlan-mode",
}


def render(intent: Intent) -> tuple[Command, ...]:
    """The commands that carry ``intent`` out, in order."""
    return tuple(Command(text=line) for line in _forward(intent))


def revert(intent: Intent) -> tuple[Command, ...]:
    """The commands that put the device back the way the capture found it."""
    return tuple(Command(text=line) for line in _inverse(intent))


def describe(intent: Intent) -> str:
    """One imperative sentence naming what the intent does, for a plan's summary."""
    if intent.summary:
        return intent.summary
    interface = intent.interface
    match intent.kind:
        case IntentKind.VLAN_CREATE:
            named = f" ({intent.value})" if intent.value else ""
            return f"create VLAN {intent.target}{named} in the device's VLAN database"
        case IntentKind.VLAN_DELETE:
            return f"remove VLAN {intent.target} from the device's VLAN database"
        case IntentKind.INTERFACE_CREATE:
            parent = f" on {intent.parent}" if intent.parent else ""
            kind = intent.interface_type
            return f"create {interface} as {article(kind)} {kind} interface{parent}"
        case IntentKind.INTERFACE_DELETE:
            return f"remove the {intent.interface_type or 'virtual'} interface {interface}"
        case IntentKind.INTERFACE_SET:
            return f"set {intent.target} on {interface} to {intent.value}"
        case IntentKind.INTERFACE_ENABLE:
            return f"bring {interface} up"
        case IntentKind.INTERFACE_DISABLE:
            return f"shut {interface}"
        case IntentKind.MEMBER_ADD:
            return f"enslave {intent.target} to {interface}"
        case IntentKind.MEMBER_REMOVE:
            return f"release {intent.target} from {interface}"
        case IntentKind.VLAN_MODE:
            return f"make {interface} {article(intent.value or '')} {intent.value} port"
        case IntentKind.VLAN_ACCESS:
            return f"put {interface} in VLAN {intent.target}"
        case IntentKind.VLAN_TAG:
            return f"carry VLAN {intent.target} tagged on {interface}"
        case IntentKind.VLAN_UNTAG:
            return f"stop carrying VLAN {intent.target} on {interface}"
        case IntentKind.ADDRESS_ADD:
            return f"add {intent.target} to {interface}"
        case IntentKind.ADDRESS_REMOVE:
            return f"remove {intent.target} from {interface}"
        case IntentKind.MANUAL:  # pragma: no cover - a manual intent always sets summary
            return intent.note
    raise AssertionError(f"unhandled intent kind: {intent.kind!r}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# The grammar
# --------------------------------------------------------------------------- #


def _forward(intent: Intent) -> tuple[str, ...]:
    match intent.kind:
        case IntentKind.MANUAL:
            return ()
        case IntentKind.VLAN_CREATE:
            named = f" name {intent.value}" if intent.value else ""
            return (f"create vlan {intent.target}{named}",)
        case IntentKind.VLAN_DELETE:
            return (f"delete vlan {intent.target}",)
        case IntentKind.INTERFACE_CREATE:
            return (_create_interface(intent),)
        case IntentKind.INTERFACE_DELETE:
            return (f"delete interface {intent.interface}",)
        case IntentKind.INTERFACE_SET:
            return (_set(intent.interface, intent.target, intent.value),)
        case IntentKind.INTERFACE_ENABLE:
            return (f"set interface {intent.interface} enabled true",)
        case IntentKind.INTERFACE_DISABLE:
            return (f"set interface {intent.interface} enabled false",)
        case IntentKind.MEMBER_ADD:
            return (f"add interface {intent.interface} member {intent.target}",)
        case IntentKind.MEMBER_REMOVE:
            return (f"remove interface {intent.interface} member {intent.target}",)
        case IntentKind.VLAN_MODE:
            return (f"set interface {intent.interface} vlan-mode {intent.value}",)
        case IntentKind.VLAN_ACCESS:
            return (f"set interface {intent.interface} vlan-access {intent.target}",)
        case IntentKind.VLAN_TAG:
            return (f"add interface {intent.interface} vlan-tagged {intent.target}",)
        case IntentKind.VLAN_UNTAG:
            return (f"remove interface {intent.interface} vlan-tagged {intent.target}",)
        case IntentKind.ADDRESS_ADD:
            return (f"add interface {intent.interface} {_family(intent)}-address {intent.target}",)
        case IntentKind.ADDRESS_REMOVE:
            return (
                f"remove interface {intent.interface} {_family(intent)}-address {intent.target}",
            )
    raise AssertionError(f"unhandled intent kind: {intent.kind!r}")  # pragma: no cover


def _inverse(intent: Intent) -> tuple[str, ...]:
    match intent.kind:
        case IntentKind.MANUAL:
            return ()
        case IntentKind.VLAN_CREATE:
            return (f"delete vlan {intent.target}",)
        case IntentKind.VLAN_DELETE:
            named = f" name {intent.value}" if intent.value else ""
            return (f"create vlan {intent.target}{named}",)
        case IntentKind.INTERFACE_CREATE:
            return (f"delete interface {intent.interface}",)
        case IntentKind.INTERFACE_DELETE:
            return (_create_interface(intent),)
        case IntentKind.INTERFACE_SET:
            return (_set(intent.interface, intent.target, intent.previous),)
        case IntentKind.INTERFACE_ENABLE:
            return (f"set interface {intent.interface} enabled false",)
        case IntentKind.INTERFACE_DISABLE:
            return (f"set interface {intent.interface} enabled true",)
        case IntentKind.MEMBER_ADD:
            return (f"remove interface {intent.interface} member {intent.target}",)
        case IntentKind.MEMBER_REMOVE:
            return (f"add interface {intent.interface} member {intent.target}",)
        case IntentKind.VLAN_MODE:
            return (_set(intent.interface, "mode", intent.previous),)
        case IntentKind.VLAN_ACCESS:
            return (_set(intent.interface, "vlan-access", intent.previous, literal=True),)
        case IntentKind.VLAN_TAG:
            return (f"remove interface {intent.interface} vlan-tagged {intent.target}",)
        case IntentKind.VLAN_UNTAG:
            return (f"add interface {intent.interface} vlan-tagged {intent.target}",)
        case IntentKind.ADDRESS_ADD:
            return (
                f"remove interface {intent.interface} {_family(intent)}-address {intent.target}",
            )
        case IntentKind.ADDRESS_REMOVE:
            return (f"add interface {intent.interface} {_family(intent)}-address {intent.target}",)
    raise AssertionError(f"unhandled intent kind: {intent.kind!r}")  # pragma: no cover


def _create_interface(intent: Intent) -> str:
    kind = intent.interface_type or "ethernet"
    parent = f" parent {intent.parent}" if intent.parent else ""
    return f"create interface {intent.interface} type {kind}{parent}"


def _set(interface: str, field: str, value: str | None, *, literal: bool = False) -> str:
    """``set`` when there is a value to set, ``unset`` when there is not.

    A rollback for a field the capture reported nothing for cannot restore a
    value, and writing one anyway would put the device into a state neither side
    ever observed. ``unset`` says exactly that: take the netviz-set value off
    and leave whatever the device does by default.
    """
    attribute = field if literal else _ATTRIBUTES.get(field, field)
    if value is None:
        return f"unset interface {interface} {attribute}"
    return f"set interface {interface} {attribute} {value}"


def _family(intent: Intent) -> str:
    """``ipv4`` or ``ipv6``. Address intents carry the family in ``value``."""
    return intent.value if intent.value in ("ipv4", "ipv6") else "ipv4"
