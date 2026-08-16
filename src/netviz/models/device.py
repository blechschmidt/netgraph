"""Device elements: ``switch``, ``router``, ``firewall``, ``hub``, ``computer``, ``server``.

The six kinds share one ``spec`` shape (§6.1 of ``docs/schema.md``); they
differ in which fields are permitted (§6.5) and in how the renderer draws them.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from enum import Enum
from typing import ClassVar, Final, Literal

from pydantic import Field, model_validator

from netviz.models.base import NetvizModel
from netviz.models.diagnostics import field_error
from netviz.models.element import ElementBase
from netviz.models.firewall import LOCAL_ZONE, FirewallConfig, FirewallRule, NatRule, Zone
from netviz.models.interface import Interface, InterfaceList, InterfaceType
from netviz.models.netns import ROOT_NETNS, NetnsDefinition, resolve_netns_tree
from netviz.models.power import PowerConfig
from netviz.models.routing import (
    RESERVED_TABLES,
    AddressFamily,
    PolicyRule,
    RouteTable,
    RoutingConfig,
    StaticRoute,
    VrfDefinition,
)
from netviz.models.scalars import Boolean, ElementName, MacAddress, VlanId
from netviz.models.style import Style

__all__ = [
    "DEVICE_KINDS",
    "GLOBAL_VRF",
    "BridgeConfig",
    "BridgeType",
    "Computer",
    "Device",
    "DeviceSpec",
    "Firewall",
    "Forwarding",
    "Hub",
    "Router",
    "Server",
    "Switch",
    "VlanDefinition",
]

#: The name of the instance an interface that binds to no VRF is in. The empty
#: string rather than ``"default"`` or ``None``: it is not a name anybody may
#: declare (``ElementName`` requires at least one character), so it cannot be
#: shadowed by a VRF called ``global``, and it sorts before every real name — so
#: the global instance leads every listing that groups by VRF.
GLOBAL_VRF: Final = ""


class BridgeType(str, Enum):
    """``dot1q:bridge/bridge-type`` identities (§6.3)."""

    CUSTOMER_VLAN = "customer-vlan-bridge"
    PROVIDER = "provider-bridge"
    PROVIDER_EDGE = "provider-edge-bridge"
    TWO_PORT_MAC_RELAY = "two-port-mac-relay-bridge"
    MAC = "mac-bridge"

    @property
    def port_type(self) -> str:
        """``dot1q:port-type`` implied by this bridge type (§9.3)."""
        return "dot1q:d-bridge-port" if self is BridgeType.MAC else "dot1q:c-vlan-bridge-port"


class Forwarding(NetvizModel):
    """``spec.forwarding`` — the device-wide default for ``ip:*/forwarding``."""

    ipv4: Boolean
    ipv6: Boolean


class BridgeConfig(NetvizModel):
    """``spec.bridge`` — the 802.1Q bridge component (§6.3)."""

    #: ``dot1q:bridge/name``; defaults to ``metadata.name``.
    name: ElementName | None = None
    type: BridgeType = BridgeType.CUSTOMER_VLAN
    #: ``dot1q:bridge/address``.
    address: MacAddress | None = None


class VlanDefinition(NetvizModel):
    """One entry of ``spec.vlans`` — the device VLAN database (§6.4)."""

    id: VlanId
    #: ``dot1q:vlan/name`` (``dot1qtypes:name-type``).
    name: str | None = Field(default=None, max_length=32)
    description: str | None = None


class DeviceSpec(NetvizModel):
    """``spec`` of every device kind (§6.1)."""

    vendor: str | None = None
    model: str | None = None
    serial: str | None = None
    location: str | None = None
    interfaces: InterfaceList
    bridge: BridgeConfig | None = None
    vlans: list[VlanDefinition] = Field(default_factory=list)
    #: ``None`` until the per-kind default of §6.1.1 is applied by the element.
    forwarding: Forwarding | None = None
    #: The network namespaces this device runs (§23.1). Each is a whole second
    #: network stack; ``parent`` nests one inside another, arbitrarily deep.
    netns: list[NetnsDefinition] = Field(default_factory=list)
    #: The routing instances this device implements (§16.1).
    vrfs: list[VrfDefinition] = Field(default_factory=list)
    #: The routing tables this device holds beyond the reserved three (§16.3).
    route_tables: list[RouteTable] = Field(default_factory=list)
    #: Configured static routes (§16.5).
    routes: list[StaticRoute] = Field(default_factory=list)
    #: The routing policy database: which table each packet is routed by (§16.4).
    routing_policy: list[PolicyRule] = Field(default_factory=list)
    #: The dynamic routing protocols the device takes part in (§16.7).
    routing: RoutingConfig | None = None
    #: The security zones the device divides its interfaces into (§24.1). A zone
    #: is what firewall policy is written *between*, so that a rule survives an
    #: interface being renamed or moved to a LAG.
    zones: list[Zone] = Field(default_factory=list)
    #: What the device does to the packets it sees: the filter policy and the
    #: address translations (§24.2). Absent means the inventory records nothing
    #: about its filtering, which is not the same as "it filters nothing".
    firewall: FirewallConfig | None = None
    #: What the device draws, which outlets feed it, and how much PoE it hands
    #: out (§17.2). Absent means the inventory records nothing about its power.
    power: PowerConfig | None = None
    #: How this element is drawn (§22): a ``fill``, a ``stroke``, a ``shape``
    #: and six more, each optional and each inheriting from the theme, then the
    #: icon set, then the built-in palette when absent. See
    #: :mod:`netviz.models.style`.
    style: Style | None = None

    @model_validator(mode="after")
    def _check_interfaces(self) -> DeviceSpec:
        check_interface_set(self.interfaces)
        return self

    @model_validator(mode="after")
    def _check_vrf_table(self) -> DeviceSpec:
        """``NG-F001``/``NG-F002``/``NG-F005``: the VRF table and its references.

        A VRF is referred to by name from two places in the same ``spec`` — an
        interface binds to one, a route is placed in one — so the reference is
        resolved here, where both are in view. Everything that reaches *outside*
        the document (an interface a route sends out of, an OSPF interface, a BGP
        peer) is the validator's business instead.
        """
        declared: set[str] = set()
        for index, vrf in enumerate(self.vrfs):
            if vrf.name in declared:
                raise field_error(
                    f"VRF {vrf.name!r} is declared twice",
                    rule="NG-F001",
                    path=("vrfs", index, "name"),
                )
            declared.add(vrf.name)

        for index, interface in enumerate(self.interfaces):
            if interface.vrf is not None and interface.vrf not in declared:
                raise field_error(
                    f"{interface.name!r} binds to VRF {interface.vrf!r}, which "
                    f"{_vrf_table(declared)}",
                    rule="NG-F002",
                    path=("interfaces", index, "vrf"),
                )

        for index, route in enumerate(self.routes):
            if route.vrf is not None and route.vrf not in declared:
                raise field_error(
                    f"route {route.prefix} is placed in VRF {route.vrf!r}, which "
                    f"{_vrf_table(declared)}",
                    rule="NG-F005",
                    path=("routes", index, "vrf"),
                )
        return self

    @model_validator(mode="after")
    def _check_routing_policy(self) -> DeviceSpec:
        """``NG-F015``/``NG-F019``/``NG-F020``/``NG-F021``: tables and the policy over them.

        The same shape as :meth:`_check_vrf_table` and for the same reason: a
        table is named from two places in one ``spec`` — a route is placed in
        one, a policy rule looks one up — and both references, along with the
        interfaces a rule selects on, are resolvable from inside this document.
        What is *not* here is whether the table anybody looks up holds a route:
        that is a judgement about the whole device rather than a broken
        reference, so it is ``W147``'s and ``W148``'s business.
        """
        declared = self._check_route_tables()
        resolvable = declared | set(RESERVED_TABLES) | {vrf.name for vrf in self.vrfs}

        for index, route in enumerate(self.routes):
            if route.table is not None and route.table not in resolvable:
                raise field_error(
                    f"route {route.prefix} is placed in table {route.table!r}, which "
                    f"{_table_list(resolvable)}",
                    rule="NG-F019",
                    path=("routes", index, "table"),
                )

        names = {interface.name for interface in self.interfaces}
        seen: dict[tuple[AddressFamily, int], int] = {}
        for index, rule in enumerate(self.routing_policy):
            if rule.table is not None and rule.table not in resolvable:
                raise field_error(
                    f"policy rule {rule.priority} looks up table {rule.table!r}, which "
                    f"{_table_list(resolvable)}",
                    rule="NG-F019",
                    path=("routing_policy", index, "table"),
                )
            for family in rule.families:
                if (first := seen.get((family, rule.priority))) is not None:
                    raise field_error(
                        f"two {family.value} policy rules share priority {rule.priority} "
                        f"(this one and entry {first}); the database is walked in priority "
                        f"order, so which of them decides is not something the document says",
                        rule="NG-F020",
                        path=("routing_policy", index, "priority"),
                    )
                seen[(family, rule.priority)] = index
            for key in ("iif", "oif"):
                port: str | None = getattr(rule, key)
                if port is not None and port not in names:
                    raise field_error(
                        f"policy rule {rule.priority} selects on {key} {port!r}, which the "
                        f"device does not have; it has {_name_list(names)}",
                        rule="NG-F021",
                        path=("routing_policy", index, key),
                    )
        return self

    def _check_route_tables(self) -> set[str]:
        """``NG-F015``: the declared tables, checked; their names, returned.

        A reserved name or number is refused rather than merged, because the
        three tables of :data:`~netviz.models.routing.RESERVED_TABLES` exist
        whether or not anybody writes them down — so a second declaration of one
        is not additional information, it is a document describing a device that
        cannot exist.
        """
        declared: set[str] = set()
        ids: dict[int, str] = {}
        for index, table in enumerate(self.route_tables):
            if table.name in RESERVED_TABLES:
                raise field_error(
                    f"{table.name!r} is one of the tables every routing stack already has "
                    f"({_reserved_tables()}); it is nameable without being declared",
                    rule="NG-F015",
                    path=("route_tables", index, "name"),
                )
            if table.name in declared:
                raise field_error(
                    f"routing table {table.name!r} is declared twice",
                    rule="NG-F015",
                    path=("route_tables", index, "name"),
                )
            if (reserved := _reserved_by_id(table.id)) is not None:
                raise field_error(
                    f"table {table.name!r} is numbered {table.id}, which is reserved for "
                    f"{reserved!r}; a table declared there is that table under another name",
                    rule="NG-F015",
                    path=("route_tables", index, "id"),
                )
            if (other := ids.get(table.id)) is not None:
                raise field_error(
                    f"tables {other!r} and {table.name!r} are both numbered {table.id}; a "
                    f"table is its number, so these are one table with two names",
                    rule="NG-F015",
                    path=("route_tables", index, "id"),
                )
            declared.add(table.name)
            ids[table.id] = table.name
        return declared

    @model_validator(mode="after")
    def _check_netns_table(self) -> DeviceSpec:
        """``NG-N020``/``NG-N021``/``NG-N022``: the namespace table and its references.

        The same shape as :meth:`_check_vrf_table`, and here for the same
        reason: a namespace is named from two places in one ``spec`` — another
        namespace nests inside it, an interface lives in it — so both references
        are resolved where the whole table is in view. Nothing here reaches
        outside the document, because a namespace cannot: it is a stack inside
        one machine, and the only thing that crosses the boundary is a veth pair,
        whose two ends are also in this document.
        """
        declared: set[str] = set()
        for index, entry in enumerate(self.netns):
            if entry.name in declared:
                raise field_error(
                    f"network namespace {entry.name!r} is declared twice",
                    rule="NG-N020",
                    path=("netns", index, "name"),
                )
            declared.add(entry.name)

        for index, entry in enumerate(self.netns):
            if entry.parent is not None and entry.parent not in declared:
                raise field_error(
                    f"namespace {entry.name!r} is nested inside {entry.parent!r}, which "
                    f"{_netns_table(declared)}",
                    rule="NG-N021",
                    path=("netns", index, "parent"),
                )

        parents = resolve_netns_tree(self.netns)
        for index, entry in enumerate(self.netns):
            if (cycle := _netns_cycle(entry.name, parents)) is not None:
                raise field_error(
                    f"namespace nesting is cyclic: {' -> '.join(repr(name) for name in cycle)}. "
                    f"A namespace is created from inside exactly one other, so the chain has "
                    f"to end at the initial namespace.",
                    rule="NG-N021",
                    path=("netns", index, "parent"),
                )

        for index, interface in enumerate(self.interfaces):
            if interface.netns is not None and interface.netns not in declared:
                raise field_error(
                    f"{interface.name!r} is in network namespace {interface.netns!r}, which "
                    f"{_netns_table(declared)}",
                    rule="NG-N022",
                    path=("interfaces", index, "netns"),
                )
        return self

    @model_validator(mode="after")
    def _check_zones(self) -> DeviceSpec:
        """``NG-B001``/``NG-B002``/``NG-B003``: the zone table and what is in it.

        The same shape as :meth:`_check_vrf_table` and for the same reason: a
        zone is named from two places in one ``spec`` — an interface is put in
        one, a rule is written between two — and every reference is resolvable
        from inside this document, because a zone cannot reach outside one. It is
        a partition of *this machine's* interfaces, and the machine is here.
        """
        names = {interface.name for interface in self.interfaces}
        declared: set[str] = set()
        placed: dict[str, str] = {}
        for index, zone in enumerate(self.zones):
            if zone.name == LOCAL_ZONE:
                raise field_error(
                    f"{LOCAL_ZONE!r} is the machine itself — the traffic that terminates "
                    f"here rather than passing through — so it is nameable in a rule "
                    f"without being declared, and it is not one of the parts the "
                    f"interfaces are divided into",
                    rule="NG-B001",
                    path=("zones", index, "name"),
                )
            if zone.name in declared:
                raise field_error(
                    f"security zone {zone.name!r} is declared twice",
                    rule="NG-B001",
                    path=("zones", index, "name"),
                )
            declared.add(zone.name)
            for position, port in enumerate(zone.interfaces):
                if port not in names:
                    raise field_error(
                        f"zone {zone.name!r} holds interface {port!r}, which the device does "
                        f"not have; it has {_name_list(names)}",
                        rule="NG-B002",
                        path=("zones", index, "interfaces", position),
                    )
                if (owner := placed.get(port)) is not None:
                    written = (
                        "twice in this zone"
                        if owner == zone.name
                        else f"in {owner!r} as well as here"
                    )
                    raise field_error(
                        f"interface {port!r} is {written}; an interface is in at most one "
                        f"zone, which is what makes 'from {owner}' a statement about a "
                        f"packet rather than a question",
                        rule="NG-B003",
                        path=("zones", index, "interfaces", position),
                    )
                placed[port] = zone.name
        return self

    @model_validator(mode="after")
    def _check_firewall(self) -> DeviceSpec:
        """``NG-B004``/``NG-B008``/``NG-B009``: the policy over the zone table.

        Separate from :meth:`_check_zones` because it depends on it: every zone
        a rule names has to be one the device declares, and the set of those is
        what the previous validator establishes. pydantic runs ``mode="after"``
        validators in declaration order, so by the time this one runs the table
        has been checked and its names can be trusted.
        """
        if self.firewall is None:
            return self
        resolvable = {zone.name for zone in self.zones} | {LOCAL_ZONE}
        names = {interface.name for interface in self.interfaces}

        seen: dict[tuple[AddressFamily, int], int] = {}
        for index, rule in enumerate(self.firewall.rules):
            self._check_rule_zones(rule, resolvable, path=("firewall", "rules", index))
            for family in rule.families:
                if (first := seen.get((family, rule.priority))) is not None:
                    raise field_error(
                        f"two {family.value} firewall rules share priority {rule.priority} "
                        f"(this one and entry {first}); the chain is walked in priority "
                        f"order, so which of them decides is not something the document says",
                        rule="NG-B008",
                        path=("firewall", "rules", index, "priority"),
                    )
                seen[(family, rule.priority)] = index
            for key in ("iif", "oif"):
                port: str | None = getattr(rule, key)
                if port is not None and port not in names:
                    raise field_error(
                        f"firewall rule {rule.priority} selects on {key} {port!r}, which the "
                        f"device does not have; it has {_name_list(names)}",
                        rule="NG-B009",
                        path=("firewall", "rules", index, key),
                    )

        for index, entry in enumerate(self.firewall.nat):
            self._check_rule_zones(entry, resolvable, path=("firewall", "nat", index))
        return self

    def _check_rule_zones(
        self,
        rule: FirewallRule | NatRule,
        resolvable: set[str],
        *,
        path: tuple[str | int, ...],
    ) -> None:
        """``NG-B004``: both zone fields of one rule name a zone that exists."""
        for key in ("src_zone", "dst_zone"):
            name: str | None = getattr(rule, key)
            if name is not None and name not in resolvable:
                raise field_error(
                    f"{rule.describe()} names zone {name!r}, which {_zone_list(resolvable)}",
                    rule="NG-B004",
                    path=(*path, key),
                )

    @model_validator(mode="after")
    def _check_vlan_database(self) -> DeviceSpec:
        """``NG-V001``: ``vlans[].id`` is unique within a device."""
        seen: set[int] = set()
        for index, vlan in enumerate(self.vlans):
            if vlan.id in seen:
                raise field_error(
                    f"VLAN {vlan.id} is declared twice",
                    rule="NG-V001",
                    path=("vlans", index, "id"),
                )
            seen.add(vlan.id)
        return self

    def interface(self, name: str) -> Interface | None:
        """Look an interface up by name."""
        return next((itf for itf in self.interfaces if itf.name == name), None)

    def vlan(self, vlan_id: int) -> VlanDefinition | None:
        """Look a VLAN up in the device VLAN database."""
        return next((vlan for vlan in self.vlans if vlan.id == vlan_id), None)

    def vrf(self, name: str) -> VrfDefinition | None:
        """Look a routing instance up by name (§16.1)."""
        return next((vrf for vrf in self.vrfs if vrf.name == name), None)

    def route_table(self, name: str) -> RouteTable | None:
        """Look a declared routing table up by name (§16.3).

        Declared only: ``main`` is a table this device has and is not an entry
        of ``spec.route_tables``, so it answers ``None`` here. Callers that mean
        "is this a table at all" want :meth:`has_table`.
        """
        return next((table for table in self.route_tables if table.name == name), None)

    def table_id(self, name: str) -> int | None:
        """The number ``name`` is known by, when the inventory knows it (§16.3).

        ``None`` for a VRF: a VRF has a table, but which number the
        implementation gave it is not something a route distinguisher says, and
        every emitter that needs a number has to refuse rather than invent one.
        """
        if (reserved := RESERVED_TABLES.get(name)) is not None:
            return reserved
        table = self.route_table(name)
        return None if table is None else table.id

    def has_table(self, name: str) -> bool:
        """Is ``name`` a table this device routes by — declared, reserved or a VRF?"""
        return (
            name in RESERVED_TABLES
            or self.route_table(name) is not None
            or self.vrf(name) is not None
        )

    def routes_in(self, table: str) -> tuple[StaticRoute, ...]:
        """Every static route placed in ``table``, in declaration order (§16.5).

        Through :attr:`~netviz.models.routing.StaticRoute.table_name`, so a
        route that names neither a table nor a VRF is found under ``main`` — the
        table it is actually in, rather than the one it declines to mention.
        """
        return tuple(route for route in self.routes if route.table_name == table)

    def policy_for(self, table: str) -> tuple[PolicyRule, ...]:
        """Every policy rule that looks ``table`` up, in declaration order (§16.4)."""
        return tuple(rule for rule in self.routing_policy if rule.table == table)

    def policy_in(self, family: AddressFamily) -> tuple[PolicyRule, ...]:
        """The policy database of one family, in the order it is walked (§16.4).

        Sorted by priority rather than by declaration, because that *is* the
        database: two rules in the document are a set, and the order they are
        consulted in is the number on them. Ties cannot happen (``NG-F020``), so
        the sort is total and the result is what the device would do.
        """
        return tuple(
            sorted(
                (rule for rule in self.routing_policy if rule.matches_family(family)),
                key=lambda rule: rule.priority,
            )
        )

    def zone(self, name: str) -> Zone | None:
        """Look a declared security zone up by name (§24.1).

        Declared only: :data:`~netviz.models.firewall.LOCAL_ZONE` is a zone
        every rule may name and no document declares, so it answers ``None``
        here. Callers that mean "is this a zone at all" want :meth:`has_zone`.
        """
        return next((zone for zone in self.zones if zone.name == name), None)

    def has_zone(self, name: str) -> bool:
        """Is ``name`` a zone a rule of this device may name — declared or local?"""
        return name == LOCAL_ZONE or self.zone(name) is not None

    def zone_of(self, interface: str) -> Zone | None:
        """The zone ``interface`` is in, or ``None`` if it is in none (§24.1).

        At most one can match: ``NG-B003`` is what makes that true, and it is
        also what makes this answer meaningful — an interface in two zones would
        leave every rule naming either of them ambiguous.
        """
        return next((zone for zone in self.zones if interface in zone.interfaces), None)

    def unzoned_interfaces(self) -> tuple[Interface, ...]:
        """Every interface that could be in a zone and is not, in order (``W151``).

        Not an error: an interface carrying no transit traffic — a console port,
        for instance — belongs in no zone, and a device that declares no zone at
        all has all of them here.

        Three kinds of interface are left out, because for them "in no zone" is
        not an omission but the truth:

        * a **loopback**, which carries no transit traffic by construction.
          Traffic to it terminates on the machine, and the zone for that is
          :data:`~netviz.models.firewall.LOCAL_ZONE`, which nothing is put in;
        * a **member of an aggregate**, which is governed by the LAG or bridge
          above it exactly as §10.6 has it everywhere else. Putting ``bond0`` in
          a zone puts its lanes there; demanding each lane separately would be
          asking for the one statement the aggregate exists to avoid repeating;
        * an interface **in a network namespace** (§23.1). ``spec.zones``
          partitions the stack the policy above it is written for, and that is
          the machine's initial namespace: a container's ``eth0`` is in a second
          stack with a netfilter instance of its own, which nothing written here
          can see and nothing written here can reach. Without this, every
          container on a host that declares zones at all would be reported, and
          the only edit that silenced it would be a lie about which firewall
          filters that interface.
        """
        aggregated = {
            member for interface in self.interfaces for member in (interface.members or ())
        }
        return tuple(
            interface
            for interface in self.interfaces
            if interface.type is not InterfaceType.LOOPBACK
            and not interface.netns_name
            and interface.name not in aggregated
            and self.zone_of(interface.name) is None
        )

    def firewall_marks(self) -> tuple[str, ...]:
        """Every mark the filter policy writes, once each (§24.3, ``W152``)."""
        return () if self.firewall is None else self.firewall.marks()

    def policy_marks(self) -> tuple[str, ...]:
        """Every mark the routing policy database reads, once each (``W153``)."""
        read = [rule.fwmark for rule in self.routing_policy if rule.fwmark is not None]
        return tuple(dict.fromkeys(read))

    def netns_entry(self, name: str) -> NetnsDefinition | None:
        """Look a network namespace up by name (§23.1).

        Not ``namespace``: that word means the folder namespace of §2.2
        everywhere else here, and a device does not own one of those.
        """
        return next((entry for entry in self.netns if entry.name == name), None)

    def interfaces_in_netns(self, netns: str) -> tuple[Interface, ...]:
        """Every interface in ``netns``, in declaration order.

        :data:`~netviz.models.netns.ROOT_NETNS` selects the interfaces that
        name no namespace, which is the device's initial one.
        """
        wanted = netns or None
        return tuple(interface for interface in self.interfaces if interface.netns == wanted)

    def netns_parents(self) -> dict[str, str]:
        """``name -> parent`` over ``spec.netns``, the initial namespace being ``""``."""
        return resolve_netns_tree(self.netns)

    def netns_names(self) -> tuple[str, ...]:
        """Every network stack the device has, the initial namespace first.

        Declaration order after that, which is the order ``spec.netns`` is
        written in, so a diagram and the document agree on which box comes
        first. A declared namespace that holds no interface is still listed —
        it exists, it is just empty, and ``W146`` is what says so.
        """
        names = [ROOT_NETNS, *(entry.name for entry in self.netns)]
        return tuple(dict.fromkeys(names))

    def veth_pairs(self) -> tuple[tuple[Interface, Interface], ...]:
        """Every veth pair of the device, once each, in declaration order (§23.2).

        Once each rather than twice: the pairing is symmetric (``NG-N023``), so
        walking the interfaces would find every pair from both ends. The end
        declared first is the source, which is what makes the result — and the
        edge a renderer draws from it — deterministic.
        """
        by_name = {interface.name: interface for interface in self.interfaces}
        seen: set[str] = set()
        pairs: list[tuple[Interface, Interface]] = []
        for interface in self.interfaces:
            peer = by_name.get(interface.peer or "")
            if peer is None or interface.name in seen or peer.name in seen:
                continue
            seen.update((interface.name, peer.name))
            pairs.append((interface, peer))
        return tuple(pairs)

    def interfaces_in(self, vrf: str) -> tuple[Interface, ...]:
        """Every interface bound to ``vrf``, in declaration order.

        :data:`GLOBAL_VRF` selects the interfaces that bind to no VRF at all,
        which is the instance every address is in until something says otherwise.
        """
        wanted = vrf or None
        return tuple(interface for interface in self.interfaces if interface.vrf == wanted)


def _vrf_table(declared: Iterable[str]) -> str:
    """``is not declared in 'spec.vrfs' (which holds …)`` — the tail of NG-F002."""
    names = sorted(declared)
    if not names:
        return "is not declared: the device declares no 'spec.vrfs' at all"
    return f"is not declared in 'spec.vrfs'; it holds {', '.join(repr(name) for name in names)}"


def _table_list(resolvable: Iterable[str]) -> str:
    """``is no table of this device; it routes by …`` — the tail of ``NG-F019``.

    The reserved three are named alongside the declared ones because they are
    equally valid answers: somebody who wrote ``table: mian`` needs to see that
    ``main`` was available without being declared.
    """
    return "is no table of this device; it routes by " + ", ".join(
        repr(name) for name in sorted(resolvable)
    )


def _zone_list(resolvable: Iterable[str]) -> str:
    """``is no zone of this device; it has …`` — the tail of ``NG-B004``.

    :data:`~netviz.models.firewall.LOCAL_ZONE` is named alongside the declared
    ones because it is an equally valid answer: somebody who wrote
    ``dst_zone: lokal`` needs to see that ``local`` was available undeclared.
    """
    return "is no zone of this device; it has " + ", ".join(
        repr(name) for name in sorted(resolvable)
    )


def _name_list(names: Iterable[str]) -> str:
    """``'eth0', 'eth1'``, or a phrase for a device with no interface at all."""
    listed = sorted(names)
    return ", ".join(repr(name) for name in listed) if listed else "no interfaces at all"


def _reserved_tables() -> str:
    """``'local', 'main', 'default'`` — the reserved three, highest number first."""
    return ", ".join(repr(name) for name in RESERVED_TABLES)


def _reserved_by_id(number: int) -> str | None:
    """The reserved table numbered ``number``, if one is."""
    return next((name for name, value in RESERVED_TABLES.items() if value == number), None)


def _netns_table(declared: Iterable[str]) -> str:
    """The tail of ``NG-N021`` and ``NG-N022``, shaped like :func:`_vrf_table`."""
    names = sorted(declared)
    if not names:
        return "is not declared: the device declares no 'spec.netns' at all"
    return f"is not declared in 'spec.netns'; it holds {', '.join(repr(name) for name in names)}"


def _netns_cycle(start: str, parents: Mapping[str, str]) -> tuple[str, ...] | None:
    """The nesting chain from ``start`` back to itself, or ``None`` if it ends.

    The one-step case (a namespace naming itself) is refused by the entry's own
    validator; this finds the longer ones, which are only visible once the whole
    table is in view. The walk is bounded by the number of declared namespaces,
    so a chain that ends at the initial namespace always returns ``None``.
    """
    chain: list[str] = [start]
    current = parents.get(start, "")
    while current:
        if current == start:
            return (*chain, start)
        if current in chain:  # a loop that ``start`` only leads into
            return None
        chain.append(current)
        current = parents.get(current, "")
    return None


def check_interface_set(interfaces: Iterable[Interface], *, reserved: Iterable[str] = ()) -> None:
    """Check name uniqueness and stacking references within one element.

    ``NG-I001``: interface names are unique within their device. ``NG-I002`` /
    ``NG-I003``: ``parent`` and ``members`` resolve to interfaces on the same
    device. ``reserved`` holds extra names that are taken but are not entries of
    the list itself (the adapter upstream port, ``NG-X004``).
    """
    reserved_names = set(reserved)
    names: set[str] = set()
    entries = list(interfaces)
    for index, interface in enumerate(entries):
        if interface.name in reserved_names:
            raise field_error(
                f"interface name {interface.name!r} collides with the upstream port",
                rule="NG-X004",
                path=("interfaces", index, "name"),
            )
        if interface.name in names:
            raise field_error(
                f"interface name {interface.name!r} is declared twice",
                rule="NG-I001",
                path=("interfaces", index, "name"),
            )
        names.add(interface.name)

    known = names | reserved_names
    for index, interface in enumerate(entries):
        for referenced in interface.lower_layer_if:
            if referenced not in known:
                key = "parent" if interface.parent is not None else "members"
                raise field_error(
                    f"{interface.name!r} references unknown interface {referenced!r}",
                    rule="NG-I002" if key == "parent" else "NG-I003",
                    path=("interfaces", index, key),
                )

    check_veth_pairs(entries)


def check_veth_pairs(interfaces: Iterable[Interface]) -> None:
    """``NG-N023``: every ``peer`` names a free interface that names it back (§23.2).

    A veth pair is created as a pair and destroyed as a pair; there is no
    operation that leaves one end. So a document in which ``veth0`` names
    ``veth1`` and ``veth1`` names nothing does not describe half a pair — it
    describes something the kernel cannot be asked for, and the half that is
    written is as likely to be the wrong half as the right one. Requiring both
    sides also means the pairing can be read off either end, which is what lets
    the graph layer draw it without a second index.
    """
    entries = list(interfaces)
    by_name = {interface.name: interface for interface in entries}
    for index, interface in enumerate(entries):
        peer_name = interface.peer
        if peer_name is None:
            continue
        peer = by_name.get(peer_name)
        if peer is None:
            raise field_error(
                f"{interface.name!r} is one end of a veth pair whose other end "
                f"{peer_name!r} is not an interface of this element; both ends of a veth "
                f"pair are interfaces of the machine that holds it",
                rule="NG-N023",
                path=("interfaces", index, "peer"),
            )
        if peer.peer != interface.name:
            written = "names nothing" if peer.peer is None else f"names {peer.peer!r}"
            raise field_error(
                f"{interface.name!r} names {peer_name!r} as its veth peer, but {peer_name!r} "
                f"{written}; a veth pair is symmetric",
                rule="NG-N023",
                path=("interfaces", index, "peer"),
            )


class Device(ElementBase):
    """Base class of the five device kinds.

    Subclasses only differ in class-level policy: the ``spec.forwarding``
    default (§6.1.1), whether layer-2/layer-3 configuration is permitted at all
    (§6.5) and the glyph the renderer picks by default.
    """

    spec: DeviceSpec

    #: §6.1.1 — routers forward by default, everything else does not.
    forwarding_default: ClassVar[bool] = False
    #: §6.5 — a hub is a layer-1 repeater and rejects VLAN configuration.
    vlan_aware: ClassVar[bool] = True
    #: §6.5 — a hub has no IP stack.
    layer3_aware: ClassVar[bool] = True
    #: §6.5 — the renderer's default node glyph.
    default_glyph: ClassVar[str] = "device"
    #: Interface types this kind accepts; ``None`` means "every type".
    allowed_interface_types: ClassVar[frozenset[InterfaceType] | None] = None

    @model_validator(mode="after")
    def _apply_kind_policy(self) -> Device:
        self._check_kind_constraints()
        self._apply_defaults()
        return self

    def _check_kind_constraints(self) -> None:
        """§6.5 / ``NG-H001`` to ``NG-H004``."""
        if not self.vlan_aware:
            for key in ("bridge", "vlans"):
                if getattr(self.spec, key):
                    raise field_error(
                        f"a {self.kind} is a layer-1 repeater and has no {key!r}",
                        rule="NG-H003",
                        path=("spec", key),
                    )
            for index, interface in enumerate(self.spec.interfaces):
                if interface.vlan is not None:
                    raise field_error(
                        f"a {self.kind} interface must not declare 'vlan'",
                        rule="NG-H001",
                        path=("spec", "interfaces", index, "vlan"),
                    )

        if not self.layer3_aware:
            for key in ("forwarding", "vrfs", "routes", "routing", "zones", "firewall"):
                if getattr(self.spec, key):
                    raise field_error(
                        f"a {self.kind} has no IP stack and must not declare {key!r}",
                        rule="NG-H003",
                        path=("spec", key),
                    )
            for index, interface in enumerate(self.spec.interfaces):
                for family in ("ipv4", "ipv6"):
                    if getattr(interface, family) is not None:
                        raise field_error(
                            f"a {self.kind} interface must not declare {family!r}",
                            rule="NG-H002",
                            path=("spec", "interfaces", index, family),
                        )
                if interface.vrf is not None:
                    raise field_error(
                        f"a {self.kind} interface has no IP stack, so it is in no VRF",
                        rule="NG-H002",
                        path=("spec", "interfaces", index, "vrf"),
                    )

        allowed = self.allowed_interface_types
        if allowed is not None:
            for index, interface in enumerate(self.spec.interfaces):
                if interface.type not in allowed:
                    permitted = ", ".join(sorted(itype.value for itype in allowed))
                    raise field_error(
                        f"{interface.name!r} is of type {interface.type.value!r}; "
                        f"a {self.kind} only supports {permitted}",
                        rule="NG-H004",
                        path=("spec", "interfaces", index, "type"),
                    )

    def _apply_defaults(self) -> None:
        """§1 — the loader materialises defaults so the model is fully resolved."""
        if self.layer3_aware and self.spec.forwarding is None:
            self.spec.forwarding = Forwarding(
                ipv4=self.forwarding_default, ipv6=self.forwarding_default
            )
        if self.spec.bridge is not None and self.spec.bridge.name is None:
            self.spec.bridge.name = self.metadata.name
        resolve_address_family_defaults(self.spec.interfaces, self.spec.forwarding)

    @property
    def interfaces(self) -> list[Interface]:
        """Shortcut for ``spec.interfaces``."""
        return self.spec.interfaces

    def interface(self, name: str) -> Interface | None:
        """Look an interface up by name."""
        return self.spec.interface(name)

    def interface_names(self) -> Iterator[str]:
        """Every name a cable endpoint may refer to on this element."""
        for interface in self.spec.interfaces:
            yield interface.name


def resolve_address_family_defaults(
    interfaces: Iterable[Interface], forwarding: Forwarding | None
) -> None:
    """Fill in the ``ipv4``/``ipv6`` defaults inherited from the element (§6.2.3).

    ``forwarding`` supplies the device-wide default for ``ip:*/forwarding``; the
    interface MTU supplies the default for ``ip:*/mtu``. A layer-2 MTU below the
    IPv6 minimum is not propagated to IPv6 (§9.2).
    """
    for interface in interfaces:
        if interface.ipv4 is not None:
            if interface.ipv4.forwarding is None:
                interface.ipv4.forwarding = forwarding.ipv4 if forwarding else False
            if interface.ipv4.mtu is None and interface.mtu is not None:
                interface.ipv4.mtu = interface.mtu
        if interface.ipv6 is not None:
            if interface.ipv6.forwarding is None:
                interface.ipv6.forwarding = forwarding.ipv6 if forwarding else False
            if interface.ipv6.mtu is None and interface.mtu is not None and interface.mtu >= 1280:
                interface.ipv6.mtu = interface.mtu


class Switch(Device):
    """A VLAN-aware layer-2 bridge."""

    kind: Literal["switch"] = "switch"
    default_glyph: ClassVar[str] = "switch"


class Router(Device):
    """A layer-3 forwarder. Forwards by default (§6.1.1)."""

    kind: Literal["router"] = "router"
    forwarding_default: ClassVar[bool] = True
    default_glyph: ClassVar[str] = "router"


class Firewall(Device):
    """A layer-3 forwarder that filters. Forwards by default (§6.1.1).

    Structurally a router, and deliberately so: the ``spec`` is the same one,
    because the difference between a firewall and a router is a policy either of
    them may carry rather than a shape only one of them has. ``spec.zones`` and
    ``spec.firewall`` are available on every layer-3 kind (§24), so a router with
    three rules on it is describable — which it has to be, since that is what
    most networks actually run.

    What the kind buys is the picture and the vocabulary. An operator who bought
    a box whose whole job is filtering writes ``kind: firewall`` and gets a brick
    wall on the diagram rather than a router's diamond, and every reader of the
    inventory learns at a glance which boxes the policy is expected to be on.
    """

    kind: Literal["firewall"] = "firewall"
    forwarding_default: ClassVar[bool] = True
    default_glyph: ClassVar[str] = "firewall"


class Hub(Device):
    """A layer-1 repeater: no MAC table, no VLANs, no IP stack (§6.5)."""

    kind: Literal["hub"] = "hub"
    vlan_aware: ClassVar[bool] = False
    layer3_aware: ClassVar[bool] = False
    default_glyph: ClassVar[str] = "hub"
    allowed_interface_types: ClassVar[frozenset[InterfaceType] | None] = frozenset(
        {InterfaceType.ETHERNET}
    )


class Computer(Device):
    """An end host, drawn as a workstation."""

    kind: Literal["computer"] = "computer"
    default_glyph: ClassVar[str] = "workstation"


class Server(Device):
    """An end host, drawn as a rack-mount server. Structurally a computer."""

    kind: Literal["server"] = "server"
    default_glyph: ClassVar[str] = "server"


#: The kinds whose ``spec`` is a :class:`DeviceSpec`, and therefore the kinds a
#: ``template`` can be merged into (§6.6). In the order §3 lists them.
DEVICE_KINDS: tuple[str, ...] = ("switch", "router", "firewall", "hub", "computer", "server")
