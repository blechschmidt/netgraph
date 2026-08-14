"""``wireguard`` — the ``wg-quick`` file each end of a WireGuard tunnel reads.

wg-quick's unit is the *interface*, not the host: ``wg-quick up wg0`` reads
``/etc/wireguard/wg0.conf`` and that file describes one tunnel and every peer on
it. So this dialect is the only one here that writes several files for one
device, one per WireGuard tunnel with an end on it, and the device's other
interfaces are none of its business — ``netgraph export netplan`` or
``netgraph export interfaces`` describes those.

Four decisions are worth stating, because each of them is a place where a
generator could plausibly have written more than the inventory says.

**Keys are placeholders.** ``PrivateKey`` and ``PublicKey`` are the two things
wg-quick cannot do without and the two things an inventory deliberately does not
hold (``docs/schema.md`` §14.2). They are written as ``REPLACE-ME``, which is not
a key of any length: a placeholder that parses is a placeholder that reaches
production. The generated file is therefore not runnable until a human fills them
in, and that is the intended shape — it is a skeleton with the topology already
correct, not a configuration waiting to be applied unread.

**AllowedIPs is the peer itself.** WireGuard's ``AllowedIPs`` is simultaneously
the cryptographic access-control list and a routing statement, and the only thing
the inventory states about it is which addresses the peer holds *inside* the
tunnel. Those become host routes — ``/32`` and ``/128`` — for the same reason
:mod:`netgraph.export.config.netplan` narrows them: widening ``10.9.0.2/24`` to
the whole ``/24`` would route every address of that prefix down the tunnel, which
is a policy nobody wrote down.

**Cryptography is not negotiable.** WireGuard is Noise_IKpsk2 — ChaCha20-Poly1305,
Curve25519, BLAKE2s — with static public keys and no cipher suite to choose. A
tunnel document that names a different ``cipher``, or an ``auth`` other than
``public-key``, therefore describes something wg-quick has no syntax to be told,
and the difference is not cosmetic: it is the part that decides whether the two
ends can speak at all. Both are refusals
(:class:`~netgraph.export.config.model.Unsupported`), named with the tunnel
document's own field so the operator can grep for it.

**Multi-peer is native; nesting is not.** More than two ends needs no apology —
a ``[Peer]`` section per far end is exactly what WireGuard is for, and a hub is
written the same way as a point-to-point tunnel. ``spec.over`` is the opposite
case: wg-quick has no syntax for "bring the underlay up first", and inventing a
``PostUp`` to do it would put a command in a generated file that the inventory
never asked for. The file is still written — it is a correct description of the
WireGuard interface — and the nesting is recorded in the manifest and in the
file's own banner, for the person who has to sequence the two.

There is no ``PersistentKeepalive`` anywhere in the output. A keepalive changes
what the tunnel does on the wire, the inventory states no interval, and 25
seconds is a folk default rather than a fact about this network.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

from netgraph.export.config.header import config_header
from netgraph.export.config.model import ConfigFile, Unsupported
from netgraph.export.config.plan import DevicePlan, TunnelPeer, TunnelPlan, addresses_of
from netgraph.export.manifest import Reason, Recorder
from netgraph.models.tunnel import TunnelAuth, TunnelType

__all__ = ["DIRECTORY", "declines", "files", "limits", "selects"]

#: Where wg-quick looks. The file is named after the interface because that is
#: how wg-quick is invoked -- ``wg-quick up wg0`` is spelled with the interface
#: name and nothing else, so the name in the inventory has to be the name here.
DIRECTORY = "etc/wireguard"

#: Written where wg-quick needs a secret, spelled as
#: :mod:`netgraph.export.config.netplan` spells it so that a reader who has both
#: files in front of them is looking at one placeholder rather than two.
_PLACEHOLDER = "REPLACE-ME"

#: The one suite WireGuard has, in the spellings a document might use for it.
#: Compared case-insensitively; anything else is a refusal, because WireGuard
#: offers no way to select a cipher and a file claiming otherwise would be a lie
#: about what the tunnel does.
_CIPHERS: Final[frozenset[str]] = frozenset({"chacha20-poly1305", "chacha20poly1305"})


def selects(plan: DevicePlan) -> bool:
    """Does this device terminate a WireGuard tunnel?

    Asked of the tunnels rather than of the device kind: a WireGuard peer is as
    likely to be a laptop as a router, and the thing that decides whether there
    is a ``wg0.conf`` to write is whether a tunnel document says so.
    """
    return any(tunnel.type is TunnelType.WIREGUARD for tunnel in plan.tunnels)


def declines(plan: DevicePlan) -> str:
    """Why a device this dialect did not select got no file."""
    return (
        f"{plan.name} terminates no WireGuard tunnel, and a wg-quick file describes one "
        f"tunnel rather than a host; the rest of this device's networking is "
        f"'netgraph export netplan' or 'netgraph export interfaces'"
    )


def limits(plan: DevicePlan) -> tuple[Unsupported, ...]:
    """Everything wg-quick would have had to contradict for ``plan``."""
    return tuple(_limits(plan))


def _limits(plan: DevicePlan) -> Iterator[Unsupported]:
    for tunnel in plan.tunnels:
        if tunnel.type is not TunnelType.WIREGUARD:
            continue
        spec = tunnel.tunnel.spec
        if spec.auth is not None and spec.auth is not TunnelAuth.PUBLIC_KEY:
            yield Unsupported(
                element=plan.fqn,
                field=f"{tunnel.fqn}: {tunnel.field('auth')}",
                detail=(
                    f"{tunnel.name} authenticates with {spec.auth}; WireGuard proves identity "
                    f"with static public keys and has no other method, so wg-quick cannot be "
                    f"told to do this and the generated tunnel would not be the declared one"
                ),
            )
        if spec.cipher is not None and spec.cipher.strip().lower() not in _CIPHERS:
            yield Unsupported(
                element=plan.fqn,
                field=f"{tunnel.fqn}: {tunnel.field('cipher')}",
                detail=(
                    f"{tunnel.name} names the cipher {spec.cipher!r}; WireGuard's cryptography "
                    f"is fixed (Noise_IKpsk2: ChaCha20-Poly1305, Curve25519, BLAKE2s) and there "
                    f"is no field in a wg-quick file to select another"
                ),
            )


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #


def _file_name(plan: DevicePlan, interface: str, recorder: Recorder) -> str:
    """The file's own name, which for wg-quick *is* the interface's name.

    ``wg-quick up wg0`` reads ``/etc/wireguard/wg0.conf`` and takes the interface
    name from the file name — which puts a bound on the name this dialect can
    carry that no other dialect here has. §4.1 allows a ``/`` in an interface
    name (``xe-0/0/0`` is one name, not three) and a path segment does not, so a
    slash is folded to a hyphen and the fold is recorded. Written literally the
    file would land in a subdirectory, wg-quick would never find it, and reading
    it back would invent an interface called ``0``.

    The fold changes what the file *means* — unlike ``networkd``'s, where
    ``[Match] Name=`` keeps the inventory's spelling — so it is a rewrite the
    manifest reports rather than a detail of the layout. A WireGuard interface is
    a netdev the host creates and names, so a name wg-quick cannot carry is a
    name to change in the inventory.
    """
    folded = interface.replace("/", "-")
    recorder.rewrite(
        f"{plan.fqn}:{interface}", field="interface", original=interface, rewritten=folded
    )
    return folded


def files(plan: DevicePlan, recorder: Recorder) -> tuple[ConfigFile, ...]:
    """One wg-quick file per WireGuard tunnel with an end on this device."""
    written: list[ConfigFile] = []
    paths: set[str] = set()
    for tunnel in plan.tunnels:
        if tunnel.type is not TunnelType.WIREGUARD:
            recorder.skip(
                f"{plan.fqn}:{tunnel.interface.name}",
                Reason.NOT_REPRESENTABLE,
                f"{tunnel.name} carries {tunnel.type}, not WireGuard; wg-quick has nothing "
                f"to write for it, and strongSwan, openvpn, pppd or 'ip link' own those",
            )
            continue
        path = f"{DIRECTORY}/{_file_name(plan, tunnel.interface.name, recorder)}.conf"
        if path in paths:
            # An interface may terminate several tunnels (§14.3), and wg-quick
            # names its file after the interface, so the second one has nowhere
            # to go. Reported rather than overwritten.
            recorder.skip(
                f"{plan.fqn}:{tunnel.interface.name}",
                Reason.NAME_COLLISION,
                f"{tunnel.name} is a second WireGuard tunnel on {tunnel.interface.name}; "
                f"wg-quick names its file after the interface, so only the first is written",
            )
            continue
        paths.add(path)
        written.append(ConfigFile(path=path, content=_content(plan, tunnel, recorder)))

    if written:
        recorder.emitted += 1
    return tuple(written)


def _content(plan: DevicePlan, tunnel: TunnelPlan, recorder: Recorder) -> str:
    lines = [
        *config_header("#", "wireguard", plan, notes=_notes(tunnel)),
        f"# {tunnel.fqn}",
        "[Interface]",
        *_interface(plan, tunnel, recorder),
    ]
    for peer in tunnel.peers:
        lines.extend(["", f"# {peer.element}:{peer.interface}", "[Peer]"])
        lines.extend(_peer(plan, tunnel, peer, recorder))
    return "".join(f"{line}\n" for line in lines)


def _notes(tunnel: TunnelPlan) -> tuple[str, ...]:
    notes = [
        f"One tunnel, not one host: 'wg-quick up {tunnel.interface.name}' reads this file.",
        "It is chmod 600 material -- a WireGuard private key belongs in it, and this",
        f"file holds {_PLACEHOLDER} where the keys go, because netgraph stores no key",
        "material (docs/schema.md section 14.2). Fill them in before the tunnel is used.",
    ]
    if tunnel.tunnel.spec.over is not None:
        notes.append("")
        notes.append(
            f"This tunnel runs inside {tunnel.tunnel.spec.over}; wg-quick has no syntax for"
        )
        notes.append("an underlay tunnel, so that one has to be up before this one.")
    return tuple(notes)


def _interface(plan: DevicePlan, tunnel: TunnelPlan, recorder: Recorder) -> Iterator[str]:
    """The ``[Interface]`` body: this end's overlay address, port, MTU and key."""
    spec = tunnel.tunnel.spec
    if spec.over is not None:
        recorder.skip(
            f"{plan.fqn}:{tunnel.interface.name}",
            Reason.NOT_REPRESENTABLE,
            f"{tunnel.name} declares 'spec.over: {spec.over}'; a wg-quick file cannot say "
            f"that it needs another tunnel up first, so the ordering is the operator's",
        )
    addresses = addresses_of(tunnel.interface)
    if addresses:
        yield f"Address = {', '.join(addresses)}"
    else:
        recorder.skip(
            f"{plan.fqn}:{tunnel.interface.name}",
            Reason.NO_ADDRESS,
            f"{tunnel.interface.name} declares no address inside the tunnel, so there is "
            f"nothing to put in 'Address' and wg-quick will bring up an unnumbered link",
        )
    if spec.port is not None:
        yield f"ListenPort = {spec.port}"
    # The interface's own MTU wins over the tunnel document's, because it is the
    # more specific of the two -- the same rule the 'netplan' and 'networkd'
    # dialects apply. Three files configuring one interface must not set three
    # different MTUs: on a WireGuard link a wrong one is a tunnel that comes up
    # and then black-holes every large packet.
    mtu = tunnel.interface.mtu if tunnel.interface.mtu is not None else spec.mtu
    if mtu is not None:
        yield f"MTU = {mtu}"
    yield (
        f"PrivateKey = {_PLACEHOLDER}  # private key of {plan.name}; netgraph holds no "
        f"key material (docs/schema.md section 14.2)"
    )


def _peer(
    plan: DevicePlan, tunnel: TunnelPlan, peer: TunnelPeer, recorder: Recorder
) -> Iterator[str]:
    """One ``[Peer]`` section: whose key, which addresses, and where to reach it."""
    yield f"PublicKey = {_PLACEHOLDER}  # public key of {peer.name}:{peer.interface}"
    if peer.overlay:
        yield f"AllowedIPs = {', '.join(_host_route(cidr) for cidr in peer.overlay)}"
    else:
        recorder.skip(
            f"{plan.fqn}:{tunnel.interface.name}",
            Reason.NO_ADDRESS,
            f"peer {peer.element}:{peer.interface} declares no address inside the tunnel, "
            f"so there is nothing to put in 'AllowedIPs'",
        )
    if peer.endpoint:
        # ``port`` is materialised from the type on load, so the fallback is
        # unreachable; 0 rather than 51820 keeps a guess out of a generated file
        # if it ever is reached.
        yield f"Endpoint = {_socket(peer.endpoint, tunnel.port or 0)}"
    elif peer.endpoint_note:
        # A roaming peer legitimately has no Endpoint -- WireGuard learns it from
        # the first packet -- so this is a comment rather than a gap.
        yield f"# no Endpoint: {peer.endpoint_note}"


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def _socket(address: str, port: int) -> str:
    """``host:port``, with an IPv6 literal bracketed (RFC 3986 §3.2.2)."""
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{port}"


def _host_route(cidr: str) -> str:
    """``10.9.0.2/24`` → ``10.9.0.2/32``.

    ``AllowedIPs`` is a routing decision as much as an access-control one, and
    the only one the inventory supports is "the peer itself". See the module
    docstring; the reasoning is netplan's ``allowed-ips``, unchanged.
    """
    address, _, _ = cidr.partition("/")
    return f"{address}/{128 if ':' in address else 32}"
