"""Semantic validation of a loaded inventory (``docs/schema.md`` §10).

The loader guarantees that every document *parses* and matches the schema of
its own ``kind``. This module answers the next question: do the documents agree
with **each other**? Cables must point at interfaces that exist, MAC and IP
addresses must be unique where uniqueness is physically required, and the two
ends of a link must be configured compatibly.

    findings = validate(inventory, config.validation)
    if any(finding.severity.is_fatal for finding in findings):
        ...

Design notes
------------

*Total, never raising.* :func:`validate` reports; it does not raise. A caller
decides what to do with the findings, exactly as with
:class:`~netgraph.loader.inventory.LoadError`. The only exceptions that escape
come from a broken *configuration*, which is a tooling error rather than an
inventory error.

*One finding per problem.* A duplicate address shared by five interfaces is one
finding naming all five, not five findings. Group findings are anchored at the
**first** declaration in load order and name every participant in the message,
which keeps output stable across runs and lets a reader see the whole conflict
at once.

*Suppression is a filter, not a branch.* Rules never inspect the configuration;
the engine skips disabled rules and drops annotated findings after the fact, so
a check cannot accidentally behave differently when a rule is re-graded.

Suppressing a rule
------------------

Per inventory, in ``netgraph.toml`` (see :mod:`netgraph.config`)::

    [validate]
    ignore = ["W103"]

Per element, with an annotation on any element the finding names::

    metadata:
      name: spare-switch
      annotations:
        netgraph/ignore: "W103, E004"     # or "*" for every rule

Because a finding carries every element it involves, annotating either end of a
cable is enough to silence a finding about that cable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypeAlias

from netgraph.config import ValidationConfig
from netgraph.loader.inventory import Inventory, SourceLocation, namespace_of
from netgraph.models import (
    AGGREGATE_TYPES,
    Adapter,
    Cable,
    Computer,
    Device,
    Duplex,
    Element,
    Hub,
    Interface,
    InterfaceRef,
    InterfaceType,
    IPv4Address,
    IPv6Address,
    Medium,
    Server,
    Switch,
    VlanConfig,
    VlanMode,
)
from netgraph.models.scalars import MAX_VLAN_ID, MIN_VLAN_ID, format_bitrate
from netgraph.rules import RULES, WILDCARD, Rule, Severity, resolve_rule_id
from netgraph.subnets import (
    AddressPlacement,
    IPNetwork,
    Subnet,
    is_routable_address,
    subnets_of,
)

__all__ = [
    "IGNORE_ANNOTATIONS",
    "Finding",
    "Severity",
    "errors_only",
    "has_errors",
    "summarise",
    "validate",
]

#: Annotation keys whose value lists the rules to suppress on an element. The
#: ``netgraph.dev/`` spelling matches the label prefix reserved in §3.1; the
#: short one is what people actually type.
IGNORE_ANNOTATIONS: Final[tuple[str, ...]] = ("netgraph/ignore", "netgraph.dev/ignore")

#: Separators accepted inside an ignore annotation: ``"E001, W102 W103"``.
_TOKEN_SEPARATORS: Final = ",;"

#: Longest list of names spelled out in a message before it is abbreviated.
_MAX_LISTED: Final = 8

#: Elements that own interfaces and can therefore terminate a cable (§4.2).
InterfaceOwner: TypeAlias = Device | Adapter

#: The same set as a tuple, for ``isinstance``.
_OWNER_TYPES: Final = (Device, Adapter)

#: Elements a cable may reach that are end systems rather than network gear
#: (``W115``). An adapter is one: it is a port of the host it hangs off.
_HOST_TYPES: Final = (Computer, Server, Adapter)

#: Interface types a cable can terminate on (``NG-C009``), as an enum set.
_CABLEABLE_TYPES: Final[frozenset[InterfaceType]] = frozenset(
    itype for itype in InterfaceType if itype.is_cableable
)

#: Element kinds an adapter must not hang off (``NG-X007``). An adapter is a
#: port of the host it plugs into; network gear takes a cable.
_NOT_A_HOST_TYPES: Final = (Hub, Switch)

#: One port cabled into a hub's collision domain (``NG-H005``): the element that
#: owns it, the port as ``element:interface``, and the prefixes it is in.
_HubPeer: TypeAlias = tuple[str, str, frozenset["IPNetwork"]]


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Finding:
    """One semantic problem, tied to the rule that found it and the file it is in."""

    #: Canonical rule id, e.g. ``E002`` (see :mod:`netgraph.rules`).
    rule: str
    #: Severity *after* configuration overrides and ``--strict`` are applied.
    severity: Severity
    #: One-line, human-readable description naming the elements involved.
    message: str
    #: Where the finding is anchored; ``None`` only for whole-inventory findings.
    source: SourceLocation | None = None
    #: Fully-qualified names of every element involved, anchor first. Any of
    #: them may suppress the finding through its ``netgraph/ignore`` annotation.
    elements: tuple[str, ...] = ()
    #: Field path inside the anchor document, ``()`` for the document as a whole.
    field_path: tuple[str | int, ...] = ()

    @property
    def file(self) -> str | None:
        """The source file, relative to the inventory root."""
        return self.source.relative if self.source is not None else None

    @property
    def location(self) -> str:
        """Provenance in ``sites/hq/sw.yaml#0:17`` notation (``-`` when unknown)."""
        return str(self.source) if self.source is not None else "-"

    @property
    def element(self) -> str | None:
        """The element the finding is anchored to."""
        return self.elements[0] if self.elements else None

    @property
    def sort_key(self) -> tuple[str, int, int, int, str, str]:
        """Order findings by location, then severity, then rule id."""
        source = self.source
        return (
            source.relative if source is not None else "",
            source.index if source is not None else -1,
            source.line if source is not None and source.line is not None else -1,
            self.severity.rank,
            self.rule,
            self.message,
        )

    def __str__(self) -> str:
        return f"{self.location}: {self.severity}: {self.rule}: {self.message}"


def has_errors(findings: Iterable[Finding]) -> bool:
    """Does any finding fail the run?"""
    return any(finding.severity.is_fatal for finding in findings)


def errors_only(findings: Iterable[Finding]) -> list[Finding]:
    """The subset of ``findings`` that fails the run."""
    return [finding for finding in findings if finding.severity.is_fatal]


def summarise(findings: Iterable[Finding]) -> dict[Severity, int]:
    """Count findings per severity, in severity order."""
    counts = dict.fromkeys(Severity, 0)
    for finding in findings:
        counts[finding.severity] += 1
    return counts


@dataclass(frozen=True, slots=True)
class _Draft:
    """A finding before the engine attaches its rule id and severity.

    Checks describe *what* is wrong; the engine decides how loudly to say it.
    """

    message: str
    #: Anchor first; every entry can suppress the finding.
    elements: tuple[str, ...] = ()
    field_path: tuple[str | int, ...] = ()


#: A check reads the prepared context and yields one draft per problem.
Check = Callable[["_Context"], Iterator[_Draft]]


# --------------------------------------------------------------------------- #
# Prepared context
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """One end of a cable, resolved against the inventory."""

    cable_fqn: str
    cable: Cable
    ref: InterfaceRef
    #: Position within ``spec.endpoints`` (0 or 1), for the field path.
    index: int
    #: The element the device part names, when it resolves to an interface owner.
    owner_fqn: str | None = None
    owner: InterfaceOwner | None = None
    #: The interface the reference names; ``None`` for an unknown interface and
    #: for an adapter upstream port, which carries no L2/L3 configuration (§8.1).
    interface: Interface | None = None
    #: The reference names the adapter's upstream port rather than an interface.
    is_upstream: bool = False
    #: Candidates when the device name stayed ambiguous (§2.2).
    ambiguous: tuple[str, ...] = ()
    #: Set when the name resolved to an element that owns no interfaces.
    wrong_kind: str | None = None

    @property
    def resolved(self) -> bool:
        """Does this endpoint name a real port on a real element?"""
        return self.owner_fqn is not None and (self.interface is not None or self.is_upstream)

    @property
    def port(self) -> str:
        """The endpoint as ``element:interface``, fully qualified where known."""
        return f"{self.owner_fqn or self.ref.device}:{self.ref.interface}"

    @property
    def field_path(self) -> tuple[str | int, ...]:
        return ("spec", "endpoints", self.index)


@dataclass(frozen=True, slots=True)
class _Attachment:
    """One adapter's ``upstream.attached_to`` reference, resolved (§8.2).

    Resolved once here because four rules read it — ``E015`` (does it resolve at
    all), ``E013`` and ``W123`` (is the host attachment declared exactly once),
    ``E014`` (do the attachments loop) and ``W124`` (is the target a host) — and
    they must agree with the renderer about what the reference denotes.
    """

    adapter_fqn: str
    adapter: Adapter
    #: The reference as written, e.g. ``laptop`` or ``sites/hq/laptop``.
    ref: str
    #: The element the reference names, when it resolves to exactly one.
    host_fqn: str | None = None
    host: Element | None = None
    #: Candidates when the name stayed ambiguous (§2.2).
    ambiguous: tuple[str, ...] = ()

    @property
    def field_path(self) -> tuple[str | int, ...]:
        return ("spec", "upstream", "attached_to")


@dataclass(frozen=True, slots=True)
class _Context:
    """Everything the checks need, computed once.

    Building this up front keeps each rule a straight loop over prepared data
    instead of a nest of repeated lookups, and guarantees that all rules agree
    on how a reference resolves.
    """

    inventory: Inventory
    #: Devices and adapters in load order (cables own no interfaces).
    owners: Mapping[str, InterfaceOwner]
    endpoints: tuple[_Endpoint, ...]
    #: Every adapter that declares an ``attached_to``, in load order.
    attachments: tuple[_Attachment, ...]
    #: ``(owner fqn, interface name)`` -> the endpoints landing on it, in load order.
    terminations: Mapping[tuple[str, str], tuple[_Endpoint, ...]]
    #: Element fqns reachable through a cable or an adapter attachment.
    connected: frozenset[str]
    #: Per element: interface name -> the LAG that aggregates it (§10.6).
    lag_masters: Mapping[str, Mapping[str, Interface]]
    #: Per element: interface name -> representative of its stacking group.
    stacking_groups: Mapping[str, Mapping[str, str]]
    #: Per element: interface name -> the interface, for stacking lookups.
    by_name: Mapping[str, Mapping[str, Interface]]
    #: Per element: interface name -> every ``lag``/``bridge`` aggregating it,
    #: in declaration order. ``lag_masters`` answers "which configuration
    #: governs this port"; this answers "how many claim it", which is what
    #: ``E008`` is about.
    aggregated_by: Mapping[str, Mapping[str, tuple[str, ...]]]
    #: Per element: the rule ids its annotations suppress.
    suppressions: Mapping[str, frozenset[str]]
    #: Every prefix an address sits in (:mod:`netgraph.subnets`), in prefix
    #: order. This is the same grouping the layer-3 graph draws, so a finding
    #: about a subnet and the diagram of it can never disagree.
    subnets: tuple[Subnet, ...] = ()

    def source_of(self, fqn: str | None) -> SourceLocation | None:
        return self.inventory.source_of(fqn) if fqn is not None else None

    def effective(self, endpoint: _Endpoint) -> Interface | None:
        """The interface whose configuration governs a link end (§10.6).

        A cable that lands on a LAG member is governed by the aggregate: VLAN
        membership and MTU are properties of the bundle, not of one lane.
        """
        interface = endpoint.interface
        if interface is None or endpoint.owner_fqn is None:
            return interface
        masters = self.lag_masters.get(endpoint.owner_fqn, {})
        return masters.get(interface.name, interface)

    def is_suppressed(self, rule_id: str, elements: Sequence[str]) -> bool:
        """Does any element involved carry an annotation silencing ``rule_id``?"""
        for fqn in elements:
            ignored = self.suppressions.get(fqn)
            if ignored and (WILDCARD in ignored or rule_id in ignored):
                return True
        return False


def _build_context(inventory: Inventory) -> _Context:
    owners: dict[str, InterfaceOwner] = {
        fqn: element
        for fqn, element in inventory.elements.items()
        if isinstance(element, _OWNER_TYPES)
    }

    endpoints: list[_Endpoint] = []
    terminations: dict[tuple[str, str], list[_Endpoint]] = {}
    connected: set[str] = set()

    for cable_fqn, cable in inventory.cables.items():
        namespace = namespace_of(cable_fqn)
        for index, ref in enumerate(cable.endpoints):
            endpoint = _resolve_endpoint(inventory, cable_fqn, cable, ref, index, namespace)
            endpoints.append(endpoint)
            owner_fqn = endpoint.owner_fqn
            if owner_fqn is None:
                continue
            # A device that exists but is missing the interface still counts as
            # cabled: reporting it as an orphan on top of E001 would be two
            # findings for one mistake.
            connected.add(owner_fqn)
            if endpoint.resolved:
                terminations.setdefault((owner_fqn, ref.interface), []).append(endpoint)

    attachments: list[_Attachment] = []
    for fqn, adapter in inventory.adapters.items():
        host = adapter.upstream.attached_to
        if host is None:
            continue
        resolution = inventory.lookup(host, namespace=namespace_of(fqn))
        attachments.append(
            _Attachment(
                adapter_fqn=fqn,
                adapter=adapter,
                ref=host,
                host_fqn=resolution.fqn,
                host=resolution.element,
                ambiguous=resolution.ambiguous,
            )
        )
        if resolution.fqn is not None:
            # §8.2: `attached_to` is itself a graph edge, so both ends are joined.
            connected.add(resolution.fqn)
            connected.add(fqn)

    return _Context(
        inventory=inventory,
        owners=owners,
        endpoints=tuple(endpoints),
        attachments=tuple(attachments),
        terminations={key: tuple(value) for key, value in terminations.items()},
        connected=frozenset(connected),
        lag_masters={fqn: _lag_masters(owner) for fqn, owner in owners.items()},
        stacking_groups={fqn: _stacking_groups(owner) for fqn, owner in owners.items()},
        by_name={
            fqn: {interface.name: interface for interface in owner.interfaces}
            for fqn, owner in owners.items()
        },
        aggregated_by={fqn: _aggregated_by(owner) for fqn, owner in owners.items()},
        suppressions=_collect_suppressions(inventory),
        subnets=subnets_of(inventory),
    )


def _resolve_endpoint(
    inventory: Inventory,
    cable_fqn: str,
    cable: Cable,
    ref: InterfaceRef,
    index: int,
    namespace: str,
) -> _Endpoint:
    """Resolve one ``device:interface`` reference (§4.2)."""
    resolution = inventory.lookup(ref.device, namespace=namespace)
    element = resolution.element
    if element is None:
        return _Endpoint(
            cable_fqn=cable_fqn,
            cable=cable,
            ref=ref,
            index=index,
            ambiguous=resolution.ambiguous,
        )
    if not isinstance(element, _OWNER_TYPES):
        return _Endpoint(
            cable_fqn=cable_fqn,
            cable=cable,
            ref=ref,
            index=index,
            wrong_kind=element.kind,
        )

    is_upstream = isinstance(element, Adapter) and ref.interface == element.upstream.name
    return _Endpoint(
        cable_fqn=cable_fqn,
        cable=cable,
        ref=ref,
        index=index,
        owner_fqn=resolution.fqn,
        owner=element,
        interface=None if is_upstream else element.interface(ref.interface),
        is_upstream=is_upstream,
    )


def _lag_masters(owner: InterfaceOwner) -> dict[str, Interface]:
    """Map each LAG member to its aggregate (§10.6)."""
    masters: dict[str, Interface] = {}
    for interface in owner.interfaces:
        if interface.type is InterfaceType.LAG:
            for member in interface.members or ():
                masters.setdefault(member, interface)
    return masters


def _aggregated_by(owner: InterfaceOwner) -> dict[str, tuple[str, ...]]:
    """Map each member to every ``lag``/``bridge`` that lists it (``NG-I005``)."""
    claims: dict[str, list[str]] = {}
    for interface in owner.interfaces:
        if interface.type in AGGREGATE_TYPES:
            for member in interface.members or ():
                claims.setdefault(member, []).append(interface.name)
    return {member: tuple(names) for member, names in claims.items()}


def _stacking_groups(owner: InterfaceOwner) -> dict[str, str]:
    """Map each interface to a representative of its stacking group.

    Interfaces joined by ``parent``/``members`` form one group: a VLAN
    sub-interface, its parent, a LAG and its members all legitimately share a
    hardware address, so a shared MAC within a group is not a duplicate.
    """
    parent: dict[str, str] = {interface.name: interface.name for interface in owner.interfaces}

    def find(name: str) -> str:
        root = name
        while parent[root] != root:
            root = parent[root]
        while parent[name] != root:  # path compression
            parent[name], name = root, parent[name]
        return root

    for interface in owner.interfaces:
        for lower in interface.lower_layer_if:
            if lower in parent:
                left, right = find(interface.name), find(lower)
                if left != right:
                    parent[left] = right

    return {name: find(name) for name in parent}


def _collect_suppressions(inventory: Inventory) -> dict[str, frozenset[str]]:
    """Read the ``netgraph/ignore`` annotation of every element."""
    suppressions: dict[str, frozenset[str]] = {}
    for fqn, element in inventory.elements.items():
        tokens: list[str] = []
        for key in IGNORE_ANNOTATIONS:
            raw = element.metadata.annotations.get(key)
            if raw:
                tokens.extend(_split_tokens(raw))
        if tokens:
            # Unknown ids are kept verbatim and simply match nothing: a typo in
            # a suppression must not hide the finding it was aimed at, and must
            # not abort a run over inventory data either.
            suppressions[fqn] = frozenset(resolve_rule_id(token, strict=False) for token in tokens)
    return suppressions


def _split_tokens(value: str) -> list[str]:
    """Split ``"E001, W102 W103"`` into its rule ids."""
    for separator in _TOKEN_SEPARATORS:
        value = value.replace(separator, " ")
    return value.split()


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def _check_endpoint_references(ctx: _Context) -> Iterator[_Draft]:
    """E001 — a cable endpoint references an unknown device or interface."""
    for endpoint in ctx.endpoints:
        elements = (endpoint.cable_fqn,)
        prefix = f"cable {_q(endpoint.cable_fqn)} endpoint {endpoint.ref}"
        owner, owner_fqn = endpoint.owner, endpoint.owner_fqn

        if endpoint.ambiguous:
            candidates = _join(sorted(endpoint.ambiguous))
            yield _Draft(
                f"{prefix}: {_q(endpoint.ref.device)} is ambiguous here; it matches "
                f"{candidates}. Move the cable next to the element it refers to, or "
                f"rename one of them.",
                elements,
                endpoint.field_path,
            )
        elif endpoint.wrong_kind is not None:
            yield _Draft(
                f"{prefix}: {_q(endpoint.ref.device)} is a {endpoint.wrong_kind}, which owns "
                f"no interfaces",
                elements,
                endpoint.field_path,
            )
        elif owner is None or owner_fqn is None:
            yield _Draft(
                f"{prefix}: no element named {_q(endpoint.ref.device)} is declared in this "
                f"inventory",
                elements,
                endpoint.field_path,
            )
        elif not endpoint.resolved:
            known = _join(sorted(owner.interface_names()))
            yield _Draft(
                f"{prefix}: {_q(owner_fqn)} has no interface {_q(endpoint.ref.interface)}; "
                f"it declares {known}",
                (*elements, owner_fqn),
                endpoint.field_path,
            )


def _check_double_termination(ctx: _Context) -> Iterator[_Draft]:
    """E002 — an interface is terminated by more than one cable."""
    for (owner_fqn, interface_name), endpoints in ctx.terminations.items():
        if len(endpoints) < 2:
            continue
        port = f"{owner_fqn}:{interface_name}"
        cables = list(dict.fromkeys(endpoint.cable_fqn for endpoint in endpoints))
        if len(cables) == 1:
            yield _Draft(
                f"both endpoints of cable {_q(cables[0])} terminate on {_q(port)}; "
                f"a cable joins two distinct interfaces",
                (cables[0], owner_fqn),
                ("spec", "endpoints"),
            )
        else:
            yield _Draft(
                f"interface {_q(port)} is terminated by {len(cables)} cables: {_join(cables)}. "
                f"A physical port takes one cable.",
                (*cables, owner_fqn),
                ("spec", "endpoints"),
            )


def _check_duplicate_mac(ctx: _Context) -> Iterator[_Draft]:
    """E003 — two interfaces in the inventory share a MAC address."""
    groups: dict[str, list[tuple[str, Interface]]] = {}
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            if interface.mac is not None:
                groups.setdefault(interface.mac, []).append((fqn, interface))

    for mac, entries in groups.items():
        # A stacking group (LAG and its members, a VLAN sub-interface and its
        # parent) shares one hardware address by design, so it counts once.
        seen: set[tuple[str, str]] = set()
        distinct: list[tuple[str, Interface]] = []
        for fqn, interface in entries:
            key = (fqn, ctx.stacking_groups[fqn].get(interface.name, interface.name))
            if key in seen:
                continue
            seen.add(key)
            distinct.append((fqn, interface))

        if len(distinct) < 2:
            continue
        ports = [f"{fqn}:{interface.name}" for fqn, interface in distinct]
        yield _Draft(
            f"MAC address {mac} is used by {len(ports)} interfaces: {_join(ports)}",
            tuple(dict.fromkeys(fqn for fqn, _ in distinct)),
            _interface_path(ctx.owners[distinct[0][0]], distinct[0][1], "mac"),
        )


def _check_duplicate_ip(ctx: _Context) -> Iterator[_Draft]:
    """E004 — one IP address is assigned twice inside a subnet and VLAN."""
    groups: dict[tuple[str, str, int | None], list[tuple[str, Interface]]] = {}
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            scope = interface.vlan.pvid if interface.vlan is not None else None
            for address in interface.addresses():
                # A loopback address is scoped to the host that holds it
                # (RFC 1122 §3.2.1.3, RFC 4291 §2.5.3) and never appears on a
                # link, so every machine declaring 127.0.0.1 is correct rather
                # than in conflict.
                if address.ip.is_loopback:
                    continue
                key = (str(address.ip), str(address.network), scope)
                groups.setdefault(key, []).append((fqn, interface))

    for (ip, network, scope), entries in groups.items():
        if len(entries) < 2:
            continue
        ports = [f"{fqn}:{interface.name}" for fqn, interface in entries]
        domain = _describe_scope(scope)
        yield _Draft(
            f"IP address {ip} in {network} is assigned to {len(ports)} interfaces in "
            f"{domain}: {_join(ports)}",
            tuple(dict.fromkeys(fqn for fqn, _ in entries)),
            _interface_path(ctx.owners[entries[0][0]], entries[0][1]),
        )


def _check_vlan_mismatch(ctx: _Context) -> Iterator[_Draft]:
    """E005 — the two ends of a link disagree about VLANs (``NG-C011``).

    Three shapes of disagreement, all of which leave a link that looks perfectly
    cabled while carrying nothing the operator meant it to: two access ports in
    different VLANs, an access port facing a trunk, and two trunks whose VLAN
    sets do not meet. A fourth — two trunks that both name a *native* VLAN and
    name different ones — is the same mistake for untagged frames only.

    A port with no ``vlan`` block at all is silent by design: an untagged host
    facing an access port is the normal pairing (§11.1), and the host correctly
    says nothing about VLANs. Both ends are resolved through the LAG master
    first (§10.6), because membership is a property of the bundle.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        left, right = ctx.effective(first), ctx.effective(second)
        if left is None or right is None:
            continue
        left_vlan, right_vlan = left.vlan, right.vlan
        if left_vlan is None or right_vlan is None:
            continue
        detail = _vlan_disagreement(
            _describe_port(first, left), left_vlan, _describe_port(second, right), right_vlan
        )
        if detail is None:
            continue
        yield _Draft(
            f"cable {_q(cable_fqn)} {detail}",
            _cable_elements(cable_fqn, first, second),
            ("spec", "endpoints"),
        )


def _vlan_disagreement(
    near: str, near_vlan: VlanConfig, far: str, far_vlan: VlanConfig
) -> str | None:
    """Describe how two linked ports disagree about VLANs, or ``None`` if they agree."""
    if near_vlan.mode is VlanMode.ACCESS and far_vlan.mode is VlanMode.ACCESS:
        if near_vlan.pvid == far_vlan.pvid:
            return None
        return (
            f"connects access port {near} in VLAN {near_vlan.pvid} to access port {far} in "
            f"VLAN {far_vlan.pvid}; the two ends would be in different broadcast domains"
        )

    if VlanMode.ACCESS in (near_vlan.mode, far_vlan.mode):
        if far_vlan.mode is VlanMode.ACCESS:  # normalise: the access end first
            near, near_vlan, far, far_vlan = far, far_vlan, near, near_vlan
        carried = _describe_carried(far_vlan)
        if near_vlan.pvid == far_vlan.pvid:
            return (
                f"connects access port {near} in VLAN {near_vlan.pvid} to trunk port {far} "
                f"carrying {carried}; only the trunk's native VLAN {far_vlan.pvid} crosses, and "
                f"every tagged VLAN it carries is dropped by the access port"
            )
        return (
            f"connects access port {near} in VLAN {near_vlan.pvid} to trunk port {far} carrying "
            f"{carried}, untagged in VLAN {far_vlan.pvid}; an access port drops every tagged "
            f"frame, so the two ends share no broadcast domain"
        )

    if near_vlan.vlan_ids().isdisjoint(far_vlan.vlan_ids()):
        return (
            f"connects trunk port {near} carrying {_describe_carried(near_vlan)} to trunk port "
            f"{far} carrying {_describe_carried(far_vlan)}; the two sets are disjoint, so the "
            f"link passes no VLAN at all"
        )
    if (
        near_vlan.native_vlan is not None
        and far_vlan.native_vlan is not None
        and near_vlan.native_vlan != far_vlan.native_vlan
    ):
        return (
            f"connects trunk port {near} with native VLAN {near_vlan.native_vlan} to trunk port "
            f"{far} with native VLAN {far_vlan.native_vlan}; untagged frames would cross from one "
            f"broadcast domain into the other"
        )
    return None


def _check_adapter_capacity(ctx: _Context) -> Iterator[_Draft]:
    """E006 — an adapter declares more downstream interfaces than it has ports."""
    for fqn, adapter in ctx.inventory.adapters.items():
        capacity = adapter.spec.ports
        if capacity is None or len(adapter.interfaces) <= capacity:
            continue
        yield _Draft(
            f"adapter {_q(fqn)} declares {_count(len(adapter.interfaces), 'downstream interface')}"
            f" but has only {_count(capacity, 'port')}",
            (fqn,),
            ("spec", "interfaces"),
        )


def _check_unaddressed_interface(ctx: _Context) -> Iterator[_Draft]:
    """W101 — an interface has no address and is not a switchport."""
    for fqn, owner in ctx.owners.items():
        # §6.5: a hub is a layer-1 repeater; its ports cannot hold an address,
        # so the rule would fire on every one of them.
        if isinstance(owner, Hub):
            continue
        # Anything another interface is stacked on carries the lower layer, not
        # the addresses: LAG members and the parent of a VLAN sub-interface.
        substrate = {name for interface in owner.interfaces for name in interface.lower_layer_if}
        for index, interface in enumerate(owner.interfaces):
            if not interface.enabled or interface.name in substrate:
                continue
            if interface.has_ipv4_addresses or interface.has_ipv6_addresses:
                continue
            if interface.vlan is not None:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} has no IPv4 or IPv6 address and "
                f"no 'vlan' block, so it neither routes nor switches",
                (fqn,),
                ("spec", "interfaces", index),
            )


def _check_mtu_mismatch(ctx: _Context) -> Iterator[_Draft]:
    """W102 — the two ends of a cable disagree about the MTU."""
    for cable_fqn, first, second in _linked_endpoints(ctx):
        left, right = ctx.effective(first), ctx.effective(second)
        if left is None or right is None or left.mtu is None or right.mtu is None:
            continue
        if left.mtu == right.mtu:
            continue
        yield _Draft(
            f"cable {_q(cable_fqn)} joins {_describe_port(first, left)} with MTU {left.mtu} to "
            f"{_describe_port(second, right)} with MTU {right.mtu}; the mismatch causes silent "
            f"path-MTU failures",
            _cable_elements(cable_fqn, first, second),
            ("spec", "endpoints"),
        )


def _check_orphan_device(ctx: _Context) -> Iterator[_Draft]:
    """W103 — a device terminates no cable and hosts no adapter."""
    for fqn in ctx.inventory.devices:
        if fqn in ctx.connected:
            continue
        yield _Draft(
            f"device {_q(fqn)} terminates no cable and hosts no adapter; it is drawn as an "
            f"isolated node",
            (fqn,),
        )


def _check_ip_on_access_port(ctx: _Context) -> Iterator[_Draft]:
    """W104 — an access port of a layer-2-only switch carries an IP address."""
    for fqn, device in ctx.inventory.devices.items():
        if not isinstance(device, Switch) or _is_layer3(device):
            continue
        for index, interface in enumerate(device.interfaces):
            # An SVI is exactly where a management address belongs, and it is
            # modelled as an access-mode block carrying the encapsulation VID.
            if interface.type is InterfaceType.VLAN or interface.vlan is None:
                continue
            if interface.vlan.mode is not VlanMode.ACCESS:
                continue
            if not (interface.has_ipv4_addresses or interface.has_ipv6_addresses):
                continue
            yield _Draft(
                f"access port {_q(f'{fqn}:{interface.name}')} of layer-2-only switch "
                f"{_q(fqn)} carries an IP address; put it on a 'vlan' (SVI) interface "
                f"instead",
                (fqn,),
                ("spec", "interfaces", index),
            )


def _check_lonely_subnet(ctx: _Context) -> Iterator[_Draft]:
    """W105 — a subnet holds exactly one element.

    This is the layer-3 view's own finding: the L1 and L2 pictures cannot show
    it, because a prefix is not a thing either of them draws.
    """
    for subnet in ctx.subnets:
        # A host route holds one address by definition, and the far end of a
        # point-to-point link is routinely outside the inventory — an ISP
        # hand-off is not a device anybody here declares.
        if subnet.is_point_to_point or len(subnet.elements) != 1:
            continue
        first = subnet.members[0]
        ports = _join([member.port for member in subnet.members])
        yield _Draft(
            f"only one element is addressed in subnet {_q(subnet.prefix)}: {ports} "
            f"({_join_plain(subnet.addresses)}); nothing else in the inventory is addressed in "
            f"it, so either the prefix length is wrong or the neighbour is missing",
            (first.element,),
            ("spec", "interfaces", first.index),
        )


def _check_subnet_address_clash(ctx: _Context) -> Iterator[_Draft]:
    """W106 — two elements claim one address in a subnet, across VLAN boundaries.

    ``E004`` is the same clash seen from layer 2, and it deliberately scopes
    itself to one VLAN: re-using a prefix per broadcast domain is a normal
    design. Layer 3 has no VLAN column — a routing table does not — so the same
    address in one prefix is drawn as one subnet with two claimants whatever
    VLANs the two ports sit in, and that is worth saying once. When two of the
    holders *do* share a VLAN, ``E004`` reports it as an error and this rule
    stays quiet rather than doubling the diagnostic.
    """
    for subnet in ctx.subnets:
        for ip, holders in _group_by_ip(subnet).items():
            if len({holder.element for holder in holders}) < 2:
                continue
            if _shares_a_broadcast_domain(holders):
                continue
            first = holders[0]
            domains = _join_plain([_describe_scope(holder.scope) for holder in holders])
            yield _Draft(
                f"address {ip} in subnet {_q(subnet.prefix)} is claimed by "
                f"{_count(len(holders), 'interface')} in different broadcast domains "
                f"({domains}): {_join([holder.port for holder in holders])}. The layer-3 view "
                f"draws one subnet, so not all of them can be reached at that address.",
                tuple(dict.fromkeys(holder.element for holder in holders)),
                ("spec", "interfaces", first.index),
            )


# --------------------------------------------------------------------------- #
# Interfaces (§10.2)
# --------------------------------------------------------------------------- #


def _check_stacking_cycle(ctx: _Context) -> Iterator[_Draft]:
    """E007 — ``parent``/``members`` stacking contains a cycle.

    The schema already rejects the one-step case: an interface cannot be its own
    ``parent`` (``NG-I002``) nor list itself as a member (``NG-I003``). A longer
    loop — ``bond0`` aggregating ``bond1`` aggregating ``bond0`` — passes every
    per-document check and is only visible once the whole element is in view.
    """
    for fqn, owner in ctx.owners.items():
        for cycle in _stacking_cycles(owner):
            ports = [f"{fqn}:{name}" for name in cycle]
            chain = " -> ".join(_q(name) for name in (*cycle, cycle[0]))
            yield _Draft(
                f"interface stacking on {_q(fqn)} is cyclic: {chain}. "
                f"{_count(len(cycle), 'interface')} ({_join(ports)}) would each have to "
                f"sit on top of the next.",
                (fqn,),
                _index_path(owner, cycle[0]),
            )


def _stacking_cycles(owner: InterfaceOwner) -> list[list[str]]:
    """The cycles of the ``if:lower-layer-if`` graph, in load order.

    An iterative depth-first search rather than a recursive one: the recursion
    depth would be the length of the longest stack, which is inventory data.
    """
    lower: dict[str, tuple[str, ...]] = {
        interface.name: interface.lower_layer_if for interface in owner.interfaces
    }

    unvisited, active, done = 0, 1, 2
    state = dict.fromkeys(lower, unvisited)
    cycles: list[list[str]] = []

    for start in lower:
        if state[start] != unvisited:
            continue
        state[start] = active
        path = [start]
        stack = [(start, iter(lower[start]))]
        while stack:
            node, remaining = stack[-1]
            following = next(remaining, None)
            if following is None:
                stack.pop()
                state[node] = done
                path.pop()
            elif following not in state:
                continue  # NG-I002/NG-I003 already rejected the dangling reference
            elif state[following] is active:
                # A back edge closes exactly one cycle, and its own endpoints fix
                # that cycle's first and last interface, so no two back edges can
                # report the same loop. Only the loops reachable as back edges of
                # this spanning tree are found, which is one finding per element
                # rather than an enumeration of every cycle it contains.
                cycles.append(path[path.index(following) :])
            elif state[following] is unvisited:
                state[following] = active
                path.append(following)
                stack.append((following, iter(lower[following])))
    return cycles


def _check_member_is_aggregated(ctx: _Context) -> Iterator[_Draft]:
    """E008 — a ``lag``/``bridge`` member is not free to be aggregated.

    A physical port belongs to exactly one aggregate, and nothing may be stacked
    on it once it does: the aggregate owns the port's frames. Three shapes break
    that, and all three describe hardware that cannot be built.

    The one legitimate nesting is a ``lag`` inside a ``bridge`` — ``br0`` with
    ``members: [bond0, eth2]`` is how every Linux box bridges a bond — so it is
    the single exemption rather than a special case sprinkled through the check.
    """
    for fqn, owner in ctx.owners.items():
        claims = ctx.aggregated_by[fqn]
        by_name = ctx.by_name[fqn]
        parents = _sub_interface_parents(owner)
        for member, aggregates in claims.items():
            interface = by_name.get(member)
            if interface is None:
                continue  # NG-I003 already rejected the dangling member
            port = f"{fqn}:{member}"
            path = _index_path(owner, member)
            if len(aggregates) > 1:
                yield _Draft(
                    f"interface {_q(port)} is a member of {_count(len(aggregates), 'aggregate')} "
                    f"at once: {_join([f'{fqn}:{name}' for name in aggregates])}. A port "
                    f"belongs to one aggregate.",
                    (fqn,),
                    path,
                )
            for aggregate in aggregates:
                if not _may_aggregate(by_name[aggregate], interface):
                    yield _Draft(
                        f"interface {_q(port)} is a {interface.type.value!r} aggregate but is "
                        f"listed as a member of {_q(f'{fqn}:{aggregate}')}, which is a "
                        f"{by_name[aggregate].type.value!r}; an aggregate cannot be enslaved "
                        f"to another one",
                        (fqn,),
                        path,
                    )
            for child in parents.get(member, ()):
                yield _Draft(
                    f"interface {_q(port)} is a member of {_join([f'{fqn}:{a}' for a in aggregates])}"
                    f" and is also the parent of sub-interface {_q(f'{fqn}:{child}')}; traffic "
                    f"for the sub-interface would never reach it",
                    (fqn,),
                    path,
                )


def _may_aggregate(aggregate: Interface, member: Interface) -> bool:
    """May ``member`` legitimately appear in ``aggregate``'s ``members``?

    Anything that is not itself an aggregate may. Of the aggregates, only a
    ``lag`` inside a ``bridge`` is real hardware.
    """
    if member.type not in AGGREGATE_TYPES:
        return True
    return aggregate.type is InterfaceType.BRIDGE and member.type is InterfaceType.LAG


def _sub_interface_parents(owner: InterfaceOwner) -> dict[str, tuple[str, ...]]:
    """Map each interface to the ``type: vlan`` sub-interfaces stacked on it."""
    children: dict[str, list[str]] = {}
    for interface in owner.interfaces:
        if interface.parent is not None:
            children.setdefault(interface.parent, []).append(interface.name)
    return {parent: tuple(names) for parent, names in children.items()}


def _check_addresses_on_member(ctx: _Context) -> Iterator[_Draft]:
    """W107 — a ``lag``/``bridge`` member carries its own addresses.

    The aggregate is the interface the network sees; an address on one lane is
    reachable only while that lane is up, which defeats the point of bonding.
    """
    for fqn, owner in ctx.owners.items():
        by_name = ctx.by_name[fqn]
        for member, aggregates in ctx.aggregated_by[fqn].items():
            interface = by_name.get(member)
            if interface is None:
                continue
            addresses = [str(address) for address in interface.addresses()]
            if not addresses:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{member}')} is a member of "
                f"{_join([f'{fqn}:{name}' for name in aggregates])} but carries addresses of "
                f"its own ({_join_plain(addresses)}); addresses belong on the aggregate",
                (fqn,),
                _index_path(owner, member),
            )


def _check_mac_on_loopback(ctx: _Context) -> Iterator[_Draft]:
    """W108 — a ``loopback`` interface declares a MAC address.

    A software loopback has no medium and therefore no hardware address. One
    written here is nearly always a block copied from a physical port, and it
    will collide with the port it came from under ``E003``.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            if interface.type is not InterfaceType.LOOPBACK or interface.mac is None:
                continue
            yield _Draft(
                f"loopback interface {_q(f'{fqn}:{interface.name}')} declares the MAC address "
                f"{interface.mac}; a software loopback has no hardware address",
                (fqn,),
                _index_path(owner, interface.name, "mac"),
            )


def _check_no_cableable_interface(ctx: _Context) -> Iterator[_Draft]:
    """W109 — a device declares no ``ethernet``, ``wifi`` or ``lag`` interface.

    Only those three can terminate a cable (``NG-C009``), so such a device can
    never appear on a link. Adapters are exempt: ``NG-X003`` already restricts
    them to exactly those types at schema time.
    """
    for fqn, device in ctx.inventory.devices.items():
        types = {interface.type for interface in device.interfaces}
        if types & _CABLEABLE_TYPES:
            continue
        declared = _join_plain(sorted({interface.type.value for interface in device.interfaces}))
        yield _Draft(
            f"device {_q(fqn)} declares no ethernet, wifi or lag interface (only {declared}), "
            f"so no cable can terminate on it",
            (fqn,),
            ("spec", "interfaces"),
        )


def _check_multicast_mac(ctx: _Context) -> Iterator[_Draft]:
    """E010 — a MAC address has the multicast bit set.

    Bit 0 of the first octet marks a group address (IEEE 802-2014 §8.2). A group
    address can be a frame's *destination* but never its source, so no interface
    can own one. Graded an error rather than §10.2's warning, on the precedent of
    ``E003``/``E004`` (§10.9): unlike a duplicate address, which VRRP makes
    legitimate, there is no configuration in which this is what was meant — it is
    a mistyped octet.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            if interface.mac is None or not _first_octet(interface.mac) & 0b1:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} declares the MAC address "
                f"{interface.mac}, whose first octet has the multicast bit set; a group "
                f"address is never a valid source address",
                (fqn,),
                _index_path(owner, interface.name, "mac"),
            )


def _check_local_mac(ctx: _Context) -> Iterator[_Draft]:
    """I001 — a MAC address is locally administered.

    Bit 1 of the first octet says the address was assigned by the operator
    rather than taken from the vendor's OUI (IEEE 802-2014 §8.2). That is
    perfectly legal and often deliberate — virtual machines, bonds, anonymised
    documentation — so it is information, not a complaint. It is worth printing
    because an address that no vendor issued cannot be looked up when tracing a
    port, and because a hand-written one is the kind that gets duplicated.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            octet = _first_octet(interface.mac) if interface.mac is not None else 0
            if not octet & 0b10:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} declares the locally administered "
                f"MAC address {interface.mac}; no vendor OUI identifies it",
                (fqn,),
                _index_path(owner, interface.name, "mac"),
            )


def _first_octet(mac: str) -> int:
    """The first octet of a normalised ``aa:bb:cc:dd:ee:ff`` address."""
    return int(mac[:2], 16)


# --------------------------------------------------------------------------- #
# Addresses (§10.3)
# --------------------------------------------------------------------------- #


def _check_reserved_address(ctx: _Context) -> Iterator[_Draft]:
    """W110 — an address is the network or broadcast address of its own prefix.

    Both are reserved: the all-zeros host part identifies the subnet (and is the
    subnet-router anycast address in IPv6, RFC 4291 §2.6.1), the all-ones one is
    the IPv4 directed broadcast. Neither can be assigned to an interface. A
    prefix with no host part to speak of — ``/31`` and ``/32``, ``/127`` and
    ``/128`` — is exempt, because RFC 3021 and RFC 6164 give both addresses of a
    point-to-point link to the two ends.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            for address in interface.addresses():
                reserved = _reserved_role(address)
                if reserved is None:
                    continue
                yield _Draft(
                    f"interface {_q(f'{fqn}:{interface.name}')} is configured with {address}, "
                    f"which is the {reserved} of {address.network}; it cannot be assigned to "
                    f"an interface",
                    (fqn,),
                    _index_path(owner, interface.name),
                )


def _reserved_role(address: IPv4Address | IPv6Address) -> str | None:
    """Name the reserved role ``address`` occupies in its own prefix, if any."""
    network = address.network
    if network.num_addresses <= 2:
        return None
    if address.ip == network.network_address:
        return "subnet-router anycast address" if network.version == 6 else "network address"
    if network.version == 4 and address.ip == network.broadcast_address:
        return "broadcast address"
    return None


def _check_overlapping_prefixes(ctx: _Context) -> Iterator[_Draft]:
    """W111 — two interfaces on one element sit in overlapping prefixes.

    Two ports in prefixes that contain one another leave the host with no single
    answer to "which interface do I send this out of"; two ports in the *same*
    prefix are the common spelling of it, and usually mean a prefix length was
    copied where a different subnet was meant. Addresses that are scoped rather
    than routed — loopback and link-local — are excluded, since ``fe80::/64`` on
    every port is how link-local works rather than a clash.

    Two addresses on **one** interface are exempt by §10.3's own wording: a
    secondary address inside the primary's prefix is an ordinary alias.
    """
    for fqn, owner in ctx.owners.items():
        placements = [
            (interface.name, address.network)
            for interface in owner.interfaces
            for address in interface.addresses()
            if is_routable_address(address)
        ]
        for first, second in _overlapping_pairs(placements):
            (left_port, left_net), (right_port, right_net) = first, second
            yield _Draft(
                f"element {_q(fqn)} has overlapping prefixes on two interfaces: "
                f"{_q(f'{fqn}:{left_port}')} is in {left_net} and "
                f"{_q(f'{fqn}:{right_port}')} is in {right_net}; traffic for the overlap has "
                f"no single egress",
                (fqn,),
                _index_path(owner, left_port),
            )


def _overlapping_pairs(
    placements: Sequence[tuple[str, IPNetwork]],
) -> Iterator[tuple[tuple[str, IPNetwork], tuple[str, IPNetwork]]]:
    """Each pair of placements on distinct interfaces whose prefixes overlap.

    A pair of *prefixes* is reported once per pair of interfaces, however many
    addresses each interface holds in them.
    """
    seen: set[tuple[str, str, str, str]] = set()
    for index, (left_port, left_net) in enumerate(placements):
        for right_port, right_net in placements[index + 1 :]:
            if left_port == right_port or left_net.version != right_net.version:
                continue
            if not left_net.overlaps(right_net):
                continue
            key = (left_port, str(left_net), right_port, str(right_net))
            if key in seen:
                continue
            seen.add(key)
            yield (left_port, left_net), (right_port, right_net)


def _check_loopback_prefix(ctx: _Context) -> Iterator[_Draft]:
    """W112 — a ``loopback`` interface carries a prefix wider than a host route.

    A routed loopback is a single address the IGP advertises as a host route; a
    ``/24`` on one claims a whole subnet that exists nowhere on the wire, and
    every router that believes it black-holes the rest of that subnet. The
    host-scoped loopback prefixes are exempt — ``127.0.0.1/8`` is what every
    operating system actually configures, and RFC 1122 §3.2.1.3 reserves the
    whole of ``127.0.0.0/8`` for it — so the rule only speaks about the routed
    loopbacks it is aimed at.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            if interface.type is not InterfaceType.LOOPBACK:
                continue
            for address in interface.addresses():
                host_length = 32 if address.network.version == 4 else 128
                if address.prefix_length == host_length or address.ip.is_loopback:
                    continue
                yield _Draft(
                    f"loopback interface {_q(f'{fqn}:{interface.name}')} carries {address}; a "
                    f"routed loopback is a host route, so write it as "
                    f"{address.ip}/{host_length}",
                    (fqn,),
                    _index_path(owner, interface.name),
                )


# --------------------------------------------------------------------------- #
# VLANs (§10.4)
# --------------------------------------------------------------------------- #


def _check_undeclared_vlan(ctx: _Context) -> Iterator[_Draft]:
    """W113 — a port references a VLAN the device's ``vlans`` database omits.

    Declaring the database is optional (§6.4), so a device with no ``vlans`` at
    all is saying "not modelled here" rather than "none exist" and is skipped
    entirely. VLAN 1 is skipped too: 802.1Q gives every bridge a Default VLAN
    nobody configures, and the schema itself defaults ``access_vlan`` to it, so
    reporting it would fire on every port that simply left the field out.
    """
    for fqn, device in ctx.inventory.devices.items():
        declared = {vlan.id for vlan in device.spec.vlans}
        if not declared:
            continue
        for interface in device.interfaces:
            vlan = interface.vlan
            if vlan is None or _trunks_every_vlan(vlan):
                continue
            missing = sorted(vlan.vlan_ids() - declared - {MIN_VLAN_ID})
            if not missing:
                continue
            yield _Draft(
                f"port {_q(f'{fqn}:{interface.name}')} is a member of "
                f"{'VLAN' if len(missing) == 1 else 'VLANs'} "
                f"{_join_plain([str(vlan_id) for vlan_id in missing])}, which "
                f"{_q(fqn)} does not declare in 'vlans'",
                (fqn,),
                _index_path(device, interface.name, "vlan"),
            )


def _check_sub_interface_vlan(ctx: _Context) -> Iterator[_Draft]:
    """E009 — a ``vlan`` sub-interface's VID is not carried by its parent.

    A sub-interface receives the frames its parent tags with that VID and
    nothing else. If the parent is not a trunk, or trunks a set the VID is not
    in, the sub-interface is configured for traffic that can never arrive.

    A ``bridge`` parent is resolved through its members, which is where an SVI
    normally hangs: ``Vlan99`` on ``br0`` is carried as long as *some* port of
    the bridge is in VLAN 99 (``docs/schema.md`` §11.1).
    """
    for fqn, owner in ctx.owners.items():
        by_name = ctx.by_name[fqn]
        for interface in owner.interfaces:
            if interface.type is not InterfaceType.VLAN or interface.vlan is None:
                continue
            parent = by_name.get(interface.parent or "")
            if parent is None:  # pragma: no cover - NG-I002 rejects a dangling parent
                continue
            vid = interface.vlan.pvid
            carried = _carried_vlans(parent, by_name)
            if carried is None or vid in carried:
                continue
            yield _Draft(
                f"sub-interface {_q(f'{fqn}:{interface.name}')} encapsulates VLAN {vid}, but its "
                f"parent {_q(f'{fqn}:{parent.name}')} carries {_describe_vlans(carried)}",
                (fqn,),
                _index_path(owner, interface.name, "vlan"),
            )


def _carried_vlans(
    interface: Interface, by_name: Mapping[str, Interface], _seen: frozenset[str] = frozenset()
) -> frozenset[int] | None:
    """Every VLAN whose frames reach ``interface``, or ``None`` when unbounded.

    ``None`` means "every VLAN": a port trunking ``all`` carries whatever is
    asked of it, so no sub-interface VID can be wrong on it.
    """
    vlan = interface.vlan
    if vlan is not None:
        return None if _trunks_every_vlan(vlan) else vlan.vlan_ids()
    if interface.type not in AGGREGATE_TYPES:
        return frozenset()
    # An aggregate with no `vlan` block of its own carries the union of what its
    # members carry; `_seen` keeps a cyclic stacking (E007) from looping here.
    carried: set[int] = set()
    for member in interface.members or ():
        lower = by_name.get(member)
        if lower is None or member in _seen:
            continue
        below = _carried_vlans(lower, by_name, _seen | {interface.name})
        if below is None:
            return None
        carried |= below
    return frozenset(carried)


def _check_native_vlan_membership(ctx: _Context) -> Iterator[_Draft]:
    """W114 — a trunk's ``native_vlan`` is not listed in its ``trunk_vlans``.

    The native VLAN is the one the port sends and receives *untagged*, so it is
    a member of the port's VLAN set whether or not it appears in the list. The
    document then reads as carrying one VLAN fewer than the port does, which is
    exactly the sort of quiet disagreement between file and hardware this tool
    exists to surface. Writing it out changes nothing operationally.
    """
    for fqn, owner in ctx.owners.items():
        for interface in owner.interfaces:
            vlan = interface.vlan
            if vlan is None or vlan.native_vlan is None or vlan.trunk_vlans is None:
                continue
            if vlan.native_vlan in vlan.trunk_vlans:
                continue
            yield _Draft(
                f"trunk {_q(f'{fqn}:{interface.name}')} has native VLAN {vlan.native_vlan}, "
                f"which is not in its trunk_vlans ({vlan.trunk_vlans}); it is carried "
                f"untagged all the same, so list it",
                (fqn,),
                _index_path(owner, interface.name, "vlan", "native_vlan"),
            )


def _check_trunk_all_to_host(ctx: _Context) -> Iterator[_Draft]:
    """W115 — a port trunking every VLAN faces a host rather than a switch.

    ``trunk_vlans: all`` between switches is normal. Pointed at a host it hands
    the whole VLAN estate to a machine that needs one or two of them, which is
    both a broadcast load nobody planned for and the standard prerequisite for
    VLAN hopping.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        for near, far in ((first, second), (second, first)):
            interface = ctx.effective(near)
            if interface is None or interface.vlan is None:
                continue
            if not _trunks_every_vlan(interface.vlan):
                continue
            if not isinstance(far.owner, _HOST_TYPES):
                continue
            yield _Draft(
                f"port {_describe_port(near, interface)} trunks every VLAN and cable "
                f"{_q(cable_fqn)} takes it to {_q(far.port)}, which is a "
                f"{far.owner.kind} rather than a switch; trunk only the VLANs it needs",
                _cable_elements(cable_fqn, near, far),
                ("spec", "endpoints"),
            )


def _check_lag_member_vlan(ctx: _Context) -> Iterator[_Draft]:
    """W116 — a LAG member declares a ``vlan`` block that differs from the master's.

    §10.6 resolves VLAN and MTU checks on a member through its aggregate, so a
    member's own block is never what the link is checked against. When the two
    disagree, whichever one the reader believes is a coin toss — and the one the
    validator believes is the aggregate's.
    """
    for fqn, owner in ctx.owners.items():
        for member, master in ctx.lag_masters[fqn].items():
            interface = ctx.by_name[fqn].get(member)
            if interface is None or interface.vlan is None or interface.vlan == master.vlan:
                continue
            yield _Draft(
                f"LAG member {_q(f'{fqn}:{member}')} declares {_describe_vlan_block(interface)}, "
                f"but its aggregate {_q(f'{fqn}:{master.name}')} declares "
                f"{_describe_vlan_block(master)}; the aggregate's configuration is the one that "
                f"governs the link (§10.6)",
                (fqn,),
                _index_path(owner, member, "vlan"),
            )


def _trunks_every_vlan(vlan: VlanConfig) -> bool:
    """Is this ``trunk_vlans: all``, i.e. the whole 1-4094 range?"""
    trunk_vlans = vlan.trunk_vlans
    return trunk_vlans is not None and trunk_vlans.ranges == ((MIN_VLAN_ID, MAX_VLAN_ID),)


def _describe_vlans(vlans: frozenset[int]) -> str:
    """``VLANs 10, 20`` / ``no VLAN at all``."""
    if not vlans:
        return "no VLAN at all"
    ids = sorted(vlans)
    return f"{_count(len(ids), 'VLAN')} ({_join_plain([str(vlan_id) for vlan_id in ids])})"


def _describe_carried(vlan: VlanConfig) -> str:
    """``VLANs 10,20-30`` — what a trunk carries, as one phrase.

    The canonical ``dot1qtypes:vid-range-type`` string rather than an
    enumeration, so a port trunking everything reads ``VLANs 1-4094`` instead of
    four thousand ids.
    """
    trunk_vlans = vlan.trunk_vlans
    if trunk_vlans is None:  # pragma: no cover - NG-V002 requires it in trunk mode
        return "no VLAN"
    return f"VLANs {trunk_vlans}"


def _describe_vlan_block(interface: Interface) -> str:
    """``access VLAN 10`` / ``trunk 10,20`` / ``no 'vlan' block``."""
    vlan = interface.vlan
    if vlan is None:
        return "no 'vlan' block"
    if vlan.mode is VlanMode.ACCESS:
        return f"access VLAN {vlan.access_vlan}"
    native = f" native {vlan.native_vlan}" if vlan.native_vlan is not None else ""
    return f"trunk {vlan.trunk_vlans}{native}"


def _group_by_ip(subnet: Subnet) -> dict[str, list[AddressPlacement]]:
    """The members of one prefix, grouped by the address they hold."""
    groups: dict[str, list[AddressPlacement]] = {}
    for member in subnet.members:
        groups.setdefault(member.ip, []).append(member)
    return groups


def _shares_a_broadcast_domain(holders: Sequence[AddressPlacement]) -> bool:
    """Do two *different* elements among ``holders`` sit in one VLAN scope?

    That is exactly ``E004``'s key — address, prefix and ``dot1q:pvid`` — so
    when it is true the clash is already reported, as an error.
    """
    seen: dict[int | None, str] = {}
    for holder in holders:
        other = seen.setdefault(holder.scope, holder.element)
        if other != holder.element:
            return True
    return False


def _describe_scope(scope: int | None) -> str:
    return f"VLAN {scope}" if scope is not None else "the untagged domain"


# --------------------------------------------------------------------------- #
# Cables (§10.5)
# --------------------------------------------------------------------------- #


def _check_self_link(ctx: _Context) -> Iterator[_Draft]:
    """W117 — both endpoints of one cable land on the same element (``NG-C004``).

    Legal — a loopback plug and an MLAG peer-link on one logical switch both
    look like this — but far more often it is a copy-pasted cable document whose
    second endpoint was never edited, which quietly leaves the real neighbour
    undrawn. ``E002`` already reports the degenerate case where both ends name
    the *same port*, so this rule stays quiet there rather than doubling it.
    """
    for cable_fqn, first, second in _endpoint_pairs(ctx):
        if first.owner_fqn is None or first.owner_fqn != second.owner_fqn:
            continue
        if first.ref.interface == second.ref.interface:
            continue
        yield _Draft(
            f"both endpoints of cable {_q(cable_fqn)} land on {_q(first.owner_fqn)} "
            f"({_join([first.ref.interface, second.ref.interface])}); the cable joins the element "
            f"to itself and adds no path to the topology",
            _cable_elements(cable_fqn, first, second),
            ("spec", "endpoints"),
        )


def _check_wireless_medium(ctx: _Context) -> Iterator[_Draft]:
    """E011 — the cable's medium disagrees with an endpoint's type (``NG-C006``).

    ``medium: wireless`` models an *association* rather than a wire, so both
    ends must be radios; conversely a wire cannot be plugged into a radio. Each
    half is checked because each describes a different impossible link, and
    because a medium corrected on the cable but not on the port (or the reverse)
    is exactly how one of them arises.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        wireless = first.cable.spec.medium is Medium.WIRELESS
        radios = [endpoint for endpoint in (first, second) if _is_radio(endpoint)]
        if wireless and len(radios) < 2:
            wired = [endpoint for endpoint in (first, second) if not _is_radio(endpoint)]
            yield _Draft(
                f"cable {_q(cable_fqn)} is 'medium: wireless' but "
                f"{_join([endpoint.port for endpoint in wired])} "
                f"{'is' if len(wired) == 1 else 'are'} not 'type: wifi'; a wireless link is an "
                f"association between two radios",
                _cable_elements(cable_fqn, first, second),
                ("spec", "medium"),
            )
        elif not wireless and radios:
            medium = first.cable.spec.medium.value
            yield _Draft(
                f"cable {_q(cable_fqn)} is 'medium: {medium}' but "
                f"{_join([endpoint.port for endpoint in radios])} "
                f"{'is' if len(radios) == 1 else 'are'} 'type: wifi'; a radio terminates a "
                f"wireless association, not a {medium} run",
                _cable_elements(cable_fqn, first, second),
                ("spec", "medium"),
            )


def _is_radio(endpoint: _Endpoint) -> bool:
    """Is this endpoint a wifi port? An adapter's upstream bus port is not."""
    return endpoint.interface is not None and endpoint.interface.type is InterfaceType.WIFI


def _check_speed_mismatch(ctx: _Context) -> Iterator[_Draft]:
    """W118 — a cable's ``speed`` disagrees with an endpoint's own (``NG-C008``).

    §9.4 projects ``cable.speed`` onto ``if:speed`` at both ends, so the two
    cannot both be true. An interface has no ``speed`` of its own in this
    schema — the wire decides it — with one exception: an adapter's upstream
    port carries the *host bus* rate (§8.1), and a 1 Gbps dongle cabled as if it
    were a 10 Gbps link is the mismatch worth catching.
    """
    for endpoint in ctx.endpoints:
        owner = endpoint.owner
        if not endpoint.is_upstream or not isinstance(owner, Adapter):
            continue
        declared, cable_speed = owner.upstream.speed, endpoint.cable.spec.speed
        if declared is None or cable_speed is None or declared == cable_speed:
            continue
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} is {format_bitrate(cable_speed)} but its endpoint "
            f"{_q(endpoint.port)} declares {format_bitrate(declared)}; §9.4 projects the cable's "
            f"speed onto both ends, so the two cannot both hold",
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_uncableable_endpoint(ctx: _Context) -> Iterator[_Draft]:
    """E012 — an endpoint is a loopback, vlan or bridge interface (``NG-C009``).

    Those three are software constructs: a loopback has no medium, and an SVI or
    a bridge sits *above* the ports that do. A cable drawn to one describes a
    plug that has nowhere to go, and the port it was meant for is left looking
    free.
    """
    for endpoint in ctx.endpoints:
        interface = endpoint.interface
        if interface is None or interface.type.is_cableable:
            continue
        physical = _join(
            sorted(
                candidate.name
                for candidate in (endpoint.owner.interfaces if endpoint.owner else ())
                if candidate.type.is_cableable
            )
        )
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} terminates on {_q(endpoint.port)}, which is a "
            f"{interface.type.value!r} interface; only ethernet, wifi and lag interfaces can be "
            f"cabled"
            + (f" — {_q(endpoint.owner_fqn or '')} declares {physical}" if physical else ""),
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_aggregate_endpoint(ctx: _Context) -> Iterator[_Draft]:
    """W119 — an endpoint is a LAG aggregate rather than a member (``NG-C012``).

    A bundle is logical: the wires land on its members. Cabling the aggregate
    draws one link where the inventory means several, so the diagram understates
    both the port count and the redundancy the bundle exists to provide.
    """
    for endpoint in ctx.endpoints:
        interface = endpoint.interface
        if interface is None or interface.type is not InterfaceType.LAG:
            continue
        members = _join(list(interface.members or ()))
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} terminates on the lag aggregate "
            f"{_q(endpoint.port)}; a bundle is logical, so cable its members "
            f"({members}) instead",
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_half_duplex(ctx: _Context) -> Iterator[_Draft]:
    """W120 — ``duplex: half`` on a link that involves no hub (``NG-C013``).

    Half duplex means the two ends share the medium and must arbitrate for it,
    which is what a repeater's collision domain requires. Between two switched
    ports it is either a speed/duplex negotiation that failed — the classic
    cause of a link that passes pings and collapses under load — or a value
    copied from a document that described a hub.
    """
    for cable_fqn, first, second in _linked_endpoints(ctx):
        if first.cable.spec.duplex is not Duplex.HALF:
            continue
        if any(isinstance(endpoint.owner, Hub) for endpoint in (first, second)):
            continue
        yield _Draft(
            f"cable {_q(cable_fqn)} is 'duplex: half' but joins {_q(first.port)} to "
            f"{_q(second.port)}, neither of which is a hub; a shared collision domain needs a "
            f"repeater",
            _cable_elements(cable_fqn, first, second),
            ("spec", "duplex"),
        )


def _check_disconnected_topology(ctx: _Context) -> Iterator[_Draft]:
    """W121 — the topology falls into separate islands (``NG-C014``).

    Reported once for the whole inventory, naming each island's smallest member
    so a reader can find them on the diagram. Islands of **one** element are
    left to ``W103``, which says the same thing about a lone device in better
    words; this rule is about the case that looks fine locally — two halves of a
    network that are each internally cabled and never meet.
    """
    islands = [
        island
        for island in _components(ctx.owners, _topology_links(ctx))
        if len(island) > 1  # a lone element is W103's finding, not this one
    ]
    if len(islands) < 2:
        return
    representatives = [min(island) for island in islands]
    described = ", ".join(
        f"{_q(representative)} ({_count(len(island), 'element')})"
        for representative, island in zip(representatives, islands, strict=True)
    )
    yield _Draft(
        f"the topology is disconnected: {_count(len(islands), 'island')} with no link between "
        f"them ({described}); either a cable or an 'attached_to' is missing, or these are "
        f"separate networks that belong in separate inventories",
        tuple(representatives),
    )


def _topology_links(ctx: _Context) -> Iterator[tuple[str, str]]:
    """Every edge the graph layer draws: cables (§7.1) and attachments (§8.2).

    A cable whose endpoint names a *missing interface* still joins its two
    elements, exactly as in :attr:`_Context.connected`: ``E001`` reports the bad
    reference, and treating the link as absent would split the topology over a
    typo.
    """
    for _, first, second in _endpoint_pairs(ctx):
        if first.owner_fqn is not None and second.owner_fqn is not None:
            yield first.owner_fqn, second.owner_fqn
    for attachment in ctx.attachments:
        if attachment.host_fqn is not None:
            yield attachment.adapter_fqn, attachment.host_fqn


def _components(nodes: Iterable[str], links: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Group ``nodes`` into connected components, each member in load order.

    A union-find rather than a traversal: the edge list is the natural input
    here, and the components come out in the load order of their first member,
    which keeps the finding stable across runs.
    """
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # path compression
            parent[node], node = root, parent[node]
        return root

    for left, right in links:
        if left not in parent or right not in parent:
            continue
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    groups: dict[str, list[str]] = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    return list(groups.values())


def _check_uncabled_interface(ctx: _Context) -> Iterator[_Draft]:
    """I002 — an interface is ``enabled: true`` but terminates no cable (``NG-C015``).

    Information rather than a complaint: a spare port is a normal thing to own,
    and an uplink whose far end is outside the inventory (an ISP hand-off) is
    normal too. It is printed because the inverse reading is just as likely —
    the cable document was never written — and because a port list with the
    unused ports marked is what makes a patching decision possible.

    Only the types a cable *can* terminate on are considered (``NG-C009``), and
    lag aggregates are excluded: ``NG-C012`` says the wires land on the members,
    so an aggregate that terminates no cable is correct by construction. Saying
    ``enabled: false`` silences the finding and documents the port at the same
    time.
    """
    for fqn, owner in ctx.owners.items():
        for index, interface in enumerate(owner.interfaces):
            if not interface.enabled or not interface.type.is_cableable:
                continue
            if interface.type is InterfaceType.LAG:
                continue
            if (fqn, interface.name) in ctx.terminations:
                continue
            yield _Draft(
                f"interface {_q(f'{fqn}:{interface.name}')} is enabled but terminates no cable; "
                f"mark it 'enabled: false' if the port is spare",
                (fqn,),
                ("spec", "interfaces", index),
            )


# --------------------------------------------------------------------------- #
# Hubs (§10.7)
# --------------------------------------------------------------------------- #


def _check_hub_subnets(ctx: _Context) -> Iterator[_Draft]:
    """W122 — elements on one hub are addressed in different subnets (``NG-H005``).

    A hub is a repeater: every port sees every frame, so everything plugged into
    one is in a single broadcast domain and belongs in a single prefix. Ports in
    prefixes that do not meet cannot talk to each other despite being wired
    together — the network looks built and is not.

    Hubs cabled to each other form one collision domain and are examined as a
    unit. The two address families are checked separately, since a v4-only host
    next to a v6-only host is a dual-stack rollout rather than a mistake.
    """
    for hubs, peers in _hub_domains(ctx):
        for version in (4, 6):
            addressed: list[tuple[str, str, frozenset[IPNetwork]]] = []
            for owner_fqn, port, prefixes in peers:
                family = frozenset(net for net in prefixes if net.version == version)
                if family:
                    addressed.append((owner_fqn, port, family))
            if len(addressed) < 2:
                continue
            if frozenset.intersection(*(prefixes for _, _, prefixes in addressed)):
                continue
            described = ", ".join(
                f"{_q(port)} in {_join_plain(sorted(str(net) for net in prefixes))}"
                for _, port, prefixes in addressed
            )
            yield _Draft(
                f"hub {_q(hubs[0])} joins {_count(len(addressed), f'IPv{version} port')} that "
                f"share no prefix: {described}. A hub is one broadcast domain, so its ports "
                f"cannot reach each other from different subnets.",
                (*hubs, *dict.fromkeys(owner_fqn for owner_fqn, _, _ in addressed)),
                ("spec", "interfaces"),
            )


def _hub_domains(ctx: _Context) -> Iterator[tuple[tuple[str, ...], list[_HubPeer]]]:
    """Each collision domain: the hubs forming it, and the ports cabled into it.

    Hubs joined by a cable repeat each other's frames, so they are one domain
    and are grouped before their peers are collected. A port's addresses are
    read through the LAG master (§10.6) and only the routable ones count —
    ``fe80::/64`` on every interface is not a subnet anybody chose.
    """
    hub_fqns = [fqn for fqn, device in ctx.inventory.devices.items() if isinstance(device, Hub)]
    if not hub_fqns:
        return

    hub_set = set(hub_fqns)
    links: list[tuple[str, str]] = [
        (first.owner_fqn, second.owner_fqn)
        for _, first, second in _endpoint_pairs(ctx)
        if first.owner_fqn is not None
        and second.owner_fqn is not None
        and first.owner_fqn in hub_set
        and second.owner_fqn in hub_set
    ]
    for domain in _components(hub_fqns, links):
        members = set(domain)
        peers: list[_HubPeer] = []
        for _, first, second in _linked_endpoints(ctx):
            for near, far in ((first, second), (second, first)):
                if near.owner_fqn not in members or far.owner_fqn in members:
                    continue
                interface = ctx.effective(far)
                if interface is None or far.owner_fqn is None:
                    continue
                prefixes = frozenset(
                    address.network
                    for address in interface.addresses()
                    if is_routable_address(address)
                )
                if prefixes:
                    peers.append((far.owner_fqn, f"{far.owner_fqn}:{interface.name}", prefixes))
        yield tuple(domain), peers


# --------------------------------------------------------------------------- #
# Adapters (§10.8)
# --------------------------------------------------------------------------- #


def _check_attachment_target(ctx: _Context) -> Iterator[_Draft]:
    """E015 — an ``attached_to`` names nothing that could host the adapter (``NG-X001``).

    Pass 2 checks the *grammar* of the reference — a bare element name, never a
    ``device:interface``. Whether it lands on anything is a question about the
    whole inventory and belongs here. The renderer drops the attachment edge
    when it does not, so without this rule a laptop would be drawn floating next
    to its own dongle with only a note on stderr to say why.
    """
    for attachment in ctx.attachments:
        prefix = f"adapter {_q(attachment.adapter_fqn)}: upstream.attached_to"
        elements = (attachment.adapter_fqn,)
        if attachment.ambiguous:
            yield _Draft(
                f"{prefix} {_q(attachment.ref)} is ambiguous here; it matches "
                f"{_join(sorted(attachment.ambiguous))}. Write it fully qualified, or move the "
                f"adapter next to the host it plugs into.",
                elements,
                attachment.field_path,
            )
        elif attachment.host is None:
            yield _Draft(
                f"{prefix} names no element declared in this inventory: {_q(attachment.ref)}. "
                f"The adapter is drawn detached from its host.",
                elements,
                attachment.field_path,
            )
        elif not isinstance(attachment.host, _OWNER_TYPES):
            yield _Draft(
                f"{prefix} names {_q(attachment.ref)}, which is a {attachment.host.kind}; an "
                f"adapter plugs into a device or another adapter",
                (*elements, attachment.host_fqn or attachment.ref),
                attachment.field_path,
            )


def _check_attachment_and_cable(ctx: _Context) -> Iterator[_Draft]:
    """E013 — an adapter's upstream port is both attached and cabled (``NG-X005``).

    §8.2 declares the host attachment exactly once: ``attached_to`` *is* the
    edge, and no cable document is needed or permitted for it. Both spellings at
    once give the adapter two upstream links where the hardware has one plug,
    and leave a reader unable to tell which of the two is current.
    """
    attached = {
        attachment.adapter_fqn: attachment
        for attachment in ctx.attachments
        if attachment.host_fqn is not None
    }
    for endpoint in ctx.endpoints:
        attachment = attached.get(endpoint.owner_fqn or "")
        if not endpoint.is_upstream or attachment is None:
            continue
        yield _Draft(
            f"cable {_q(endpoint.cable_fqn)} lands on the upstream port {_q(endpoint.port)} of "
            f"adapter {_q(attachment.adapter_fqn)}, which is already attached to "
            f"{_q(attachment.host_fqn or attachment.ref)}; the host attachment is declared once, "
            f"either as 'attached_to' or as a cable",
            _cable_elements(endpoint.cable_fqn, endpoint),
            endpoint.field_path,
        )


def _check_attachment_cycle(ctx: _Context) -> Iterator[_Draft]:
    """E014 — ``attached_to`` attachments form a cycle (``NG-X006``).

    A dock plugged into a dongle plugged back into the dock is not hardware
    anybody can build, and every consumer that walks the chain to find the host
    — the renderer's adapter collapsing, the VLAN propagation of §8.2 — would
    have to defend itself against it.
    """
    upstream = {
        attachment.adapter_fqn: attachment.host_fqn
        for attachment in ctx.attachments
        if attachment.host_fqn is not None
    }
    for cycle in _attachment_cycles(upstream):
        chain = " -> ".join(_q(fqn) for fqn in (*cycle, cycle[0]))
        yield _Draft(
            f"adapter attachment is cyclic: {chain}. "
            f"{_count(len(cycle), 'adapter')} would each have to be plugged into the next.",
            tuple(cycle),
            ("spec", "upstream", "attached_to"),
        )


def _attachment_cycles(upstream: Mapping[str, str]) -> list[list[str]]:
    """The cycles of the ``adapter -> host`` graph, in load order.

    Every adapter has at most one host, so the graph is functional: following it
    from any node either runs out or closes exactly one loop. That makes a plain
    walk with a three-colour marking enough, and reports each cycle once.
    """
    unvisited, active, done = 0, 1, 2
    state: dict[str, int] = {}
    cycles: list[list[str]] = []

    for start in upstream:
        if state.get(start, unvisited) != unvisited:
            continue
        path: list[str] = []
        node: str | None = start
        while node is not None and state.get(node, unvisited) == unvisited:
            state[node] = active
            path.append(node)
            node = upstream.get(node)
        if node is not None and state.get(node) == active:
            cycles.append(path[path.index(node) :])
        for visited in path:
            state[visited] = done
    return cycles


def _check_unattached_adapter(ctx: _Context) -> Iterator[_Draft]:
    """W123 — an adapter is cabled downstream but has no host (``NG-X002``).

    §8.2 calls a free-standing adapter a spare in a drawer or a media converter
    in a run. Once something is patched into its downstream ports it is neither:
    the dongle is in use, and the host it is plugged into was left out. An
    adapter whose *upstream* port terminates a cable is exempt — that cable is
    the attachment, spelled the other legal way (see ``E013``).
    """
    attached = {attachment.adapter_fqn for attachment in ctx.attachments}
    for fqn, adapter in ctx.inventory.adapters.items():
        if fqn in attached or (fqn, adapter.upstream.name) in ctx.terminations:
            continue
        cabled = [
            interface.name
            for interface in adapter.interfaces
            if (fqn, interface.name) in ctx.terminations
        ]
        if not cabled:
            continue
        yield _Draft(
            f"adapter {_q(fqn)} has {_count(len(cabled), 'cabled downstream port')} "
            f"({_join(cabled)}) but no 'upstream.attached_to'; nothing says which machine it is "
            f"plugged into",
            (fqn,),
            ("spec", "upstream"),
        )


def _check_attachment_is_a_host(ctx: _Context) -> Iterator[_Draft]:
    """W124 — ``attached_to`` points at a hub or a switch (``NG-X007``).

    An adapter is a port of the machine it plugs into, so its host is a computer,
    a server, a router — something with a bus. Network gear takes a cable. A
    media converter sitting between two switches is the case that tempts this
    spelling, and §8.2 gives it a better one: ``passthrough: false`` with a cable
    on each side, which draws the converter as the distinct node it is.
    """
    for attachment in ctx.attachments:
        host = attachment.host
        if not isinstance(host, _NOT_A_HOST_TYPES):
            continue
        yield _Draft(
            f"adapter {_q(attachment.adapter_fqn)} is attached to {_q(attachment.host_fqn or '')},"
            f" which is a {host.kind}; adapters plug into hosts. Model a converter between two "
            f"switches with 'passthrough: false' and a cable on each side.",
            (attachment.adapter_fqn, attachment.host_fqn or attachment.ref),
            attachment.field_path,
        )


#: Every check, paired with the rule it reports, in report order.
_CHECKS: Final[tuple[tuple[str, Check], ...]] = (
    ("E001", _check_endpoint_references),
    ("E002", _check_double_termination),
    ("E003", _check_duplicate_mac),
    ("E004", _check_duplicate_ip),
    ("E005", _check_vlan_mismatch),
    ("E006", _check_adapter_capacity),
    ("E007", _check_stacking_cycle),
    ("E008", _check_member_is_aggregated),
    ("E009", _check_sub_interface_vlan),
    ("E010", _check_multicast_mac),
    ("E011", _check_wireless_medium),
    ("E012", _check_uncableable_endpoint),
    ("E013", _check_attachment_and_cable),
    ("E014", _check_attachment_cycle),
    ("E015", _check_attachment_target),
    ("W101", _check_unaddressed_interface),
    ("W102", _check_mtu_mismatch),
    ("W103", _check_orphan_device),
    ("W104", _check_ip_on_access_port),
    ("W105", _check_lonely_subnet),
    ("W106", _check_subnet_address_clash),
    ("W107", _check_addresses_on_member),
    ("W108", _check_mac_on_loopback),
    ("W109", _check_no_cableable_interface),
    ("W110", _check_reserved_address),
    ("W111", _check_overlapping_prefixes),
    ("W112", _check_loopback_prefix),
    ("W113", _check_undeclared_vlan),
    ("W114", _check_native_vlan_membership),
    ("W115", _check_trunk_all_to_host),
    ("W116", _check_lag_member_vlan),
    ("W117", _check_self_link),
    ("W118", _check_speed_mismatch),
    ("W119", _check_aggregate_endpoint),
    ("W120", _check_half_duplex),
    ("W121", _check_disconnected_topology),
    ("W122", _check_hub_subnets),
    ("W123", _check_unattached_adapter),
    ("W124", _check_attachment_is_a_host),
    ("I001", _check_local_mac),
    ("I002", _check_uncabled_interface),
)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def validate(inventory: Inventory, config: ValidationConfig | None = None) -> list[Finding]:
    """Check an inventory for semantic problems.

    Args:
        inventory: A tree loaded by :func:`~netgraph.loader.load_tree`.
        config: Suppressions and severity overrides, normally
            ``load_config(root).validation``. Defaults apply when omitted.

    Returns:
        Every finding that survives suppression, ordered by source file, then
        by position in the file, then by severity and rule id. The order is
        stable across runs, which keeps golden-file tests meaningful.
    """
    settings = config if config is not None else ValidationConfig()
    context = _build_context(inventory)

    findings: list[Finding] = []
    for rule_id, check in _CHECKS:
        if settings.is_disabled(rule_id):
            continue
        rule = _RULES_BY_ID[rule_id]
        severity = settings.severity_for(rule_id, rule.severity)
        for draft in check(context):
            if context.is_suppressed(rule_id, draft.elements):
                continue
            findings.append(
                Finding(
                    rule=rule_id,
                    severity=severity,
                    message=draft.message,
                    source=context.source_of(draft.elements[0] if draft.elements else None),
                    elements=draft.elements,
                    field_path=draft.field_path,
                )
            )

    findings.sort(key=lambda finding: finding.sort_key)
    return findings


_RULES_BY_ID: Final[Mapping[str, Rule]] = {rule.id: rule for rule in RULES}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _endpoint_pairs(ctx: _Context) -> Iterator[tuple[str, _Endpoint, _Endpoint]]:
    """Yield each cable with its two endpoints, resolved or not.

    ``NG-C001`` guarantees the pair at schema time; the guard is here so a
    document that somehow escaped it cannot make a rule raise.
    """
    by_cable: dict[str, list[_Endpoint]] = {}
    for endpoint in ctx.endpoints:
        by_cable.setdefault(endpoint.cable_fqn, []).append(endpoint)
    for cable_fqn, endpoints in by_cable.items():
        if len(endpoints) != 2:  # pragma: no cover - NG-C001 guarantees the pair
            continue
        yield cable_fqn, endpoints[0], endpoints[1]


def _linked_endpoints(ctx: _Context) -> Iterator[tuple[str, _Endpoint, _Endpoint]]:
    """Yield each cable whose two endpoints both resolve, with those endpoints."""
    for cable_fqn, first, second in _endpoint_pairs(ctx):
        if first.resolved and second.resolved:
            yield cable_fqn, first, second


def _cable_elements(cable_fqn: str, *endpoints: _Endpoint) -> tuple[str, ...]:
    """The cable plus the elements it joins, without repeats."""
    owners = (endpoint.owner_fqn for endpoint in endpoints)
    return tuple(dict.fromkeys([cable_fqn, *(fqn for fqn in owners if fqn is not None)]))


def _describe_port(endpoint: _Endpoint, effective: Interface) -> str:
    """Quoted ``element:interface``, naming the LAG the check resolved through."""
    text = _q(endpoint.port)
    if endpoint.interface is not None and effective.name != endpoint.interface.name:
        return f"{text} (aggregated by {_q(effective.name)})"
    return text


def _interface_path(
    owner: InterfaceOwner, interface: Interface, *suffix: str
) -> tuple[str | int, ...]:
    """Field path of an interface inside its element document."""
    return _index_path(owner, interface.name, *suffix)


def _index_path(owner: InterfaceOwner, name: str, *suffix: str) -> tuple[str | int, ...]:
    """Field path of the interface called ``name`` inside its element document."""
    for index, candidate in enumerate(owner.interfaces):
        if candidate.name == name:
            return ("spec", "interfaces", index, *suffix)
    return ("spec", "interfaces")  # pragma: no cover - the interface always belongs


def _is_layer3(device: Device) -> bool:
    """Does the device forward IP, i.e. is it more than a layer-2 bridge?"""
    forwarding = device.spec.forwarding
    return bool(forwarding and (forwarding.ipv4 or forwarding.ipv6))


def _q(value: str) -> str:
    return f"'{value}'"


def _count(number: int, noun: str) -> str:
    """``1 port`` / ``4 ports``."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _join(names: Sequence[str], limit: int = _MAX_LISTED) -> str:
    """Render a list of names, abbreviating anything unreasonably long."""
    if len(names) <= limit:
        return ", ".join(_q(name) for name in names)
    shown = ", ".join(_q(name) for name in names[:limit])
    return f"{shown} and {len(names) - limit} more"


def _join_plain(values: Sequence[str], limit: int = _MAX_LISTED) -> str:
    """Render a list of values that are not names — addresses, VLAN descriptions.

    Repeats are collapsed: naming the same broadcast domain twice reads as a
    second one.
    """
    unique = list(dict.fromkeys(values))
    if len(unique) <= limit:
        return ", ".join(unique)
    return f"{', '.join(unique[:limit])} and {len(unique) - limit} more"
