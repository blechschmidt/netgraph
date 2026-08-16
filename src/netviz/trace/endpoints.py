"""Turning what a user typed into an end of a trace.

``netviz path A B`` accepts three spellings, and which one was meant is
decided by the *shape* of the argument rather than by a flag:

``10.1.10.51``
    An IP address configured somewhere in the inventory. This is the spelling an
    operator reaches for, because an address is what a ticket, a log line or a
    packet capture carries — nobody looks up which switch port ``10.1.10.51``
    is on before asking how it reaches the file server. Pins the element, the
    interface *and* the address.
``sw1:GigabitEthernet1/0/3``
    A specific port. Pins the element and the interface, so the trace must
    leave (or arrive) by that port and not by whichever one happens to be
    shortest. This is how a redundant pair is told apart.
``sw1``
    The element. The trace may use any of its ports.

The three cannot collide. An address is recognised by
:func:`ipaddress.ip_address` accepting it, and neither an element name nor an
interface name can parse as one (§2 name grammar requires a letter first, and
``10.1.10.51`` is not a legal ``metadata.name``). A ``:`` cannot occur in a
fully-qualified name — the namespace separator is ``/`` — nor in an interface
name, so the first colon is unambiguously the separator.

Every failure names what it could not resolve *and what it could have meant*:
an ambiguous short name lists its candidates, an unknown port lists the ports
the element does have. A trace that fails at the argument stage should tell the
reader how to fix the argument, not just that it was wrong.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator

from netviz.errors import echo_value
from netviz.loader.inventory import Inventory
from netviz.models import Adapter, Device, Tunnel
from netviz.subnets import is_routable_address
from netviz.trace.model import Endpoint, TraceError

__all__ = ["resolve_endpoint"]

#: How many candidate names a diagnostic lists before it stops. An address
#: duplicated across a 400-device inventory would otherwise produce an error
#: message longer than the report it replaced.
_MAX_LISTED = 8


def resolve_endpoint(inventory: Inventory, spec: str, *, role: str = "endpoint") -> Endpoint:
    """Resolve one ``SRC``/``DST`` argument.

    Args:
        inventory: The loaded tree.
        spec: The argument as typed — an address, ``element:interface``, or an
            element name.
        role: ``source`` or ``destination``, used only in diagnostics so the
            reader knows which of the two arguments to fix.

    Raises:
        TraceError: Nothing matches, or more than one thing does.
    """
    text = spec.strip()
    if not text:
        raise TraceError(f"the {role} is empty; give an element name, 'element:interface' or an IP")

    address = _as_address(text)
    if address is not None:
        return _by_address(inventory, text, address, role=role)
    if ":" in text:
        return _by_port(inventory, text, role=role)
    return _by_element(inventory, text, role=role)[0]


def _as_address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``text`` as a bare IP address, or ``None`` when it is not one.

    A prefix length is accepted and discarded: an operator pasting
    ``10.1.10.51/24`` out of ``ip addr`` output means the address, and refusing
    it would be pedantry.
    """
    candidate = text.partition("/")[0] if "/" in text else text
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# By address
# --------------------------------------------------------------------------- #


def _by_address(
    inventory: Inventory,
    spec: str,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    role: str,
) -> Endpoint:
    """The one interface configured with ``address``.

    Loopback and link-local addresses are not searched: every host declares
    ``127.0.0.1``, so accepting it would make the argument match the whole
    inventory and mean nothing (:func:`~netviz.subnets.is_routable_address`).
    """
    matches = list(_placements(inventory, address))
    if len(matches) == 1:
        element, interface, configured, kind, netns = matches[0]
        return Endpoint(
            spec=spec,
            element=element,
            kind=kind,
            interface=interface,
            address=configured,
            netns=netns,
        )
    if not matches:
        hint = (
            " Loopback and link-local addresses are scoped to one host or one link and are "
            "deliberately not searched."
            if address.is_loopback or address.is_link_local
            else " Run 'netviz list subnets' to see what the inventory is addressed in."
        )
        raise TraceError(
            f"no interface in this inventory is addressed {echo_value(spec)} "
            f"({role} argument).{hint}"
        )

    ports = [f"{element}:{interface}" for element, interface, _, _, _ in matches]
    raise TraceError(
        f"{echo_value(spec)} is configured on {len(ports)} interfaces ({role} argument), so it "
        f"does not identify one: {_listed(ports)}. Name the port instead, as 'element:interface'. "
        f"A duplicated address is what 'E004' and 'W106' report.",
        candidates=ports,
    )


def _placements(
    inventory: Inventory, address: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> Iterator[tuple[str, str, str, str, str]]:
    """``(element, interface, address, kind, netns)`` per port holding ``address``.

    The namespace comes along because an address identifies a *stack* and not
    only a machine (§23.1): two containers of one host may legitimately hold the
    same address, and a routed trace has to start in the one the argument named.
    """
    for fqn, element in inventory.elements.items():
        if not isinstance(element, (Device, Adapter)):
            continue
        for interface in element.interfaces:
            for configured in interface.addresses():
                if configured.ip == address and is_routable_address(configured):
                    yield (
                        fqn,
                        interface.name,
                        str(configured),
                        element.kind,
                        interface.netns_name,
                    )


# --------------------------------------------------------------------------- #
# By element, with or without a port
# --------------------------------------------------------------------------- #


def _by_port(inventory: Inventory, spec: str, *, role: str) -> Endpoint:
    """``element:interface`` — the element, and the port to enter or leave by."""
    reference, _, interface = spec.partition(":")
    endpoint, element = _by_element(inventory, reference, role=role, spec=spec)

    names = tuple(_interface_names(element))
    if interface not in names:
        raise TraceError(
            f"{echo_value(endpoint.element)} has no interface {echo_value(interface)} "
            f"({role} argument). It has: {_listed(names)}.",
            candidates=names,
        )
    port = element.interface(interface)
    return Endpoint(
        spec=spec,
        element=endpoint.element,
        kind=endpoint.kind,
        interface=interface,
        address=_first_address(element, interface),
        # An interface is in exactly one network stack, so naming it names one
        # (§23.1). An adapter has none — it is a bus, not a machine — and
        # ``netns_name`` answers ``""`` for it, which is the initial namespace
        # and the only one it could be in.
        netns=port.netns_name if port is not None else "",
    )


def _by_element(
    inventory: Inventory, reference: str, *, role: str, spec: str | None = None
) -> tuple[Endpoint, Device | Adapter]:
    """An element name, fully qualified or unambiguously short.

    Returns the endpoint *and* the element behind it: :func:`_by_port` needs the
    interface list, and this is the one place that has already established the
    element owns interfaces at all.
    """
    resolution = inventory.lookup(reference)
    if resolution.ambiguous:
        raise TraceError(
            f"{echo_value(reference)} is ambiguous ({role} argument); it matches "
            f"{_listed(resolution.ambiguous)}. Use the fully-qualified name.",
            candidates=resolution.ambiguous,
        )
    element, fqn = resolution.element, resolution.fqn
    if element is None or fqn is None:
        raise TraceError(
            f"no element named {echo_value(reference)} in this inventory ({role} argument). "
            f"Run 'netviz list devices' to see what is declared."
        )
    if isinstance(element, Tunnel):
        raise TraceError(
            f"{echo_value(fqn)} is a tunnel, which is a link rather than a place traffic can "
            f"start or end ({role} argument). Name one of the elements it terminates on: "
            f"{_listed(str(ref) for ref in element.endpoints)}.",
            candidates=tuple(str(ref) for ref in element.endpoints),
        )
    if not isinstance(element, (Device, Adapter)):
        raise TraceError(
            f"{echo_value(fqn)} is a {element.kind}, which owns no interfaces and cannot be an "
            f"end of a path ({role} argument)."
        )
    return (
        Endpoint(spec=spec if spec is not None else reference, element=fqn, kind=element.kind),
        element,
    )


def _interface_names(element: Device | Adapter) -> Iterator[str]:
    """Every interface name a reference may use, an adapter's upstream included."""
    if isinstance(element, Adapter):
        yield from element.interface_names()
        return
    yield from (interface.name for interface in element.interfaces)


def _first_address(element: Device | Adapter, interface: str) -> str | None:
    """The first routable address on ``interface``, for the report's benefit."""
    port = element.interface(interface)
    if port is None:
        return None
    return next(
        (str(address) for address in port.addresses() if is_routable_address(address)), None
    )


def _listed(names: Iterator[str] | tuple[str, ...] | list[str]) -> str:
    """Names for a diagnostic, bounded so one error stays one screen."""
    ordered = list(names)
    shown = ", ".join(ordered[:_MAX_LISTED])
    remaining = len(ordered) - _MAX_LISTED
    return f"{shown} (+{remaining} more)" if remaining > 0 else shown
