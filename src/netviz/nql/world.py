"""The inventory, flattened into the objects and relations a query walks.

:mod:`netviz.nql.schema` says what exists; this says what it *is*. Every type in
the table gets its rows here, every link gets both directions, and after
:func:`build_world` returns, answering a query is dictionary lookups and set
operations — no model introspection, no re-resolution of a reference, no second
opinion about what a broadcast domain contains.

Three decisions shape the module.

**One global id space.** Every object, of every type, has an id of the form
``type|local``. A ``|`` cannot occur in an element name, an interface name or a
prefix, so ids never collide across types and a link can point at anything
without carrying its target's type alongside. It also means the executor
compares objects by string equality and nothing else.

**Both directions of every link.** ``interface.vlans`` and ``vlan.interfaces``
are filled in the same pass, from the same fact. A query asking "which
interfaces are in VLAN 10" and one asking "which VLANs is this interface in"
cannot disagree, because there is one assignment and two indices onto it.

**Derived facts come from the code that already derives them.** Subnets are
:func:`~netviz.subnets.subnets_of`, broadcast domains are
:func:`~netviz.graph.broadcast_domains`, links are the edges of the layer-1
graph a renderer draws. A query, a diagram and a validation finding therefore
say the same thing about the same network, which is the whole reason those
functions live where they do.

The cost is one pass over the inventory and one graph build, paid once per
command; every query afterwards reads dictionaries.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, TypeAlias

import networkx as nx

from netviz.graph import broadcast_domains, to_networkx
from netviz.loader.inventory import Inventory, namespace_of
from netviz.models import (
    GLOBAL_VRF,
    Adapter,
    Device,
    Element,
    Interface,
    PatchPanel,
    Pdu,
    User,
)
from netviz.models.cable import Cable
from netviz.models.netns import netns_depth, netns_path, resolve_netns_tree
from netviz.models.tunnel import Tunnel
from netviz.nql.schema import SCHEMA
from netviz.nql.types import Schema
from netviz.subnets import is_routable_address, subnets_of

__all__ = ["SEPARATOR", "Obj", "Ref", "Value", "World", "build_world"]

#: Separates an object's type from its local identity. Barred from element
#: names (§2 name grammar), interface names and prefixes, so an id is
#: unambiguous and can be split back into its parts.
SEPARATOR: Final = "|"

#: Edge kinds of the layer-1 graph that become ``link`` objects. A subnet
#: membership is not one — nobody plugs anything into a prefix — and neither is
#: a power feed or a protocol adjacency, both of which live at their own layer.
_LINK_KINDS: Final[frozenset[str]] = frozenset({"cable", "attachment", "tunnel"})

#: Node-id prefix the graph gives a tunnel drawn as a node (multipoint).
_TUNNEL_NODE: Final = "tunnel:"


@dataclass(frozen=True, slots=True)
class Ref:
    """A reference to one object. What an object-valued expression yields."""

    id: str

    @property
    def type(self) -> str:
        """The object's type, read back out of its id."""
        return self.id.partition(SEPARATOR)[0]

    @property
    def local(self) -> str:
        """The identity within that type."""
        return self.id.partition(SEPARATOR)[2]

    def __str__(self) -> str:
        return self.local


#: What an expression evaluates to, one element at a time.
Value: TypeAlias = "str | int | float | bool | Ref"


@dataclass(slots=True)
class Obj:
    """One object: its identity, its scalars, and where its links point."""

    id: str
    type: str
    #: What a table prints when the object is not projected through a shape.
    display: str
    props: dict[str, tuple[Value, ...]] = field(default_factory=dict)
    links: dict[str, list[str]] = field(default_factory=dict)

    def add(self, member: str, target: str) -> None:
        """Point ``member`` at one more object, without repeating one."""
        found = self.links.setdefault(member, [])
        if target not in found:
            found.append(target)


def _id(type_name: str, local: str) -> str:
    return f"{type_name}{SEPARATOR}{local}"


class World:
    """Every object of every type, indexed the two ways a query reads them."""

    def __init__(self, objects: Mapping[str, Obj], *, schema: Schema = SCHEMA) -> None:
        self.schema = schema
        self.objects: Mapping[str, Obj] = objects
        concrete: dict[str, list[str]] = {}
        for one in objects.values():
            concrete.setdefault(one.type, []).append(one.id)
        # Every type, abstract ones included, rolled up from its concrete
        # descendants so that ``select device`` reads six lists and
        # ``select element`` reads all thirteen.
        self._by_type: dict[str, tuple[str, ...]] = {
            one.name: tuple(
                member for name in schema.concrete(one.name) for member in concrete.get(name, ())
            )
            for one in schema
        }
        self._adjacency = self._element_adjacency()

    def _element_adjacency(self) -> Mapping[str, tuple[str, ...]]:
        """Element id -> the elements one link away, for the graph functions."""
        found: dict[str, list[str]] = {}
        for one in self.objects.values():
            if one.type != "link":
                continue
            ends = one.links.get("elements", [])
            for near in ends:
                for far in ends:
                    if near != far:
                        found.setdefault(near, []).append(far)
        return {key: tuple(dict.fromkeys(value)) for key, value in found.items()}

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    def all(self, type_name: str) -> tuple[Ref, ...]:
        """Every object of ``type_name``, subtypes included, in inventory order."""
        return tuple(Ref(one) for one in self._by_type.get(self.schema.canonical(type_name), ()))

    def step(self, ref: Ref, member: str, *, is_link: bool) -> tuple[Value, ...]:
        """The values ``ref.member`` yields, in stored order."""
        one = self.objects.get(ref.id)
        if one is None:
            return ()
        if is_link:
            return tuple(Ref(target) for target in one.links.get(member, ()))
        return one.props.get(member, ())

    def type_of(self, ref: Ref) -> str:
        one = self.objects.get(ref.id)
        return one.type if one is not None else ref.type

    def display(self, ref: Ref) -> str:
        one = self.objects.get(ref.id)
        return one.display if one is not None else ref.local

    def neighbors(self, refs: Iterable[Ref], hops: int) -> tuple[Ref, ...]:
        """Every element at most ``hops`` links from one of ``refs``.

        The seeds are included, which is what makes ``neighbors(x, 2)`` the
        two-hop *neighbourhood* rather than the ring at distance two. Ordering
        is by distance and then by inventory order, so a truncated result is the
        nearest part of it.
        """
        seen: dict[str, None] = {}
        frontier = [ref.id for ref in refs if ref.id in self.objects]
        for one in frontier:
            seen.setdefault(one, None)
        for _ in range(max(0, hops)):
            following = [
                far
                for one in frontier
                for far in self._adjacency.get(one, ())
                if far not in seen and seen.setdefault(far, None) is None
            ]
            if not following:
                break
            frontier = following
        return tuple(Ref(one) for one in seen)

    def reachable(self, refs: Iterable[Ref]) -> tuple[Ref, ...]:
        """The whole connected component of every seed, seeds included."""
        return self.neighbors(refs, len(self.objects))

    def __len__(self) -> int:
        return len(self.objects)


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def build_world(inventory: Inventory) -> World:
    """Flatten ``inventory`` into every object a relational query can select."""
    return _Builder(inventory).run()


class _Builder:
    """One pass per family of object, in dependency order.

    Later passes fill the back-links of earlier ones, so the order in
    :meth:`run` is the order the passes are written: a namespace exists before
    an interface names it, and a subnet exists before an address is put in one.
    """

    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory
        self.objects: dict[str, Obj] = {}
        #: ``(element fqn, interface name) -> interface object id``.
        self.interfaces: dict[tuple[str, str], str] = {}
        self.vlans: dict[int, str] = {}
        self.netns: dict[tuple[str, str], str] = {}
        self.zones: dict[tuple[str, str], str] = {}
        self.racks: dict[tuple[str, str, str], str] = {}
        self.graph: nx.MultiGraph = nx.MultiGraph()

    def run(self) -> World:
        self.graph = to_networkx(self.inventory)
        self._elements()
        self._racks()
        self._namespaces()
        self._zones()
        self._vlan_table()
        self._interfaces()
        self._routes()
        self._subnets()
        self._links()
        self._domains()
        return World(self.objects)

    # -- helpers ------------------------------------------------------- #

    def _new(self, type_name: str, local: str, display: str, **props: Any) -> Obj:
        one = Obj(id=_id(type_name, local), type=type_name, display=display)
        one.props = _scalars(**props)
        self.objects[one.id] = one
        return one

    def _join(self, left: Obj, left_member: str, right: Obj, right_member: str) -> None:
        """Record one fact in both directions."""
        left.add(left_member, right.id)
        right.add(right_member, left.id)

    def _element(self, fqn: str) -> Obj | None:
        element = self.inventory.elements.get(fqn)
        return self.objects.get(_id(element.kind, fqn)) if element is not None else None

    # -- elements ------------------------------------------------------ #

    def _elements(self) -> None:
        for fqn, element in self.inventory.elements.items():
            source = self.inventory.source_of(fqn)
            location = element.metadata.location
            one = self._new(
                element.kind,
                fqn,
                fqn,
                name=element.metadata.name,
                fqn=fqn,
                namespace=namespace_of(fqn),
                kind=element.kind,
                description=element.metadata.description,
                site=location.site if location else None,
                room=location.room if location else None,
                rack_name=location.rack if location else None,
                position=location.position if location else None,
                height=location.height if location else None,
                labels=_pairs(element.metadata.labels),
                annotations=_pairs(element.metadata.annotations),
                file=source.relative if source else None,
                line=source.line if source else None,
            )
            one.props.update(_scalars(**_spec_of(element)))
        self._references()
        self._identities()

    def _references(self) -> None:
        """The element-to-element references a ``spec`` holds by name.

        Resolved outwards from the referring document's namespace, the way
        every other consumer resolves one (§4.1), so a query and a diagram
        follow the same reference to the same element.
        """
        for fqn, adapter in self.inventory.adapters.items():
            host = adapter.spec.upstream.attached_to
            target = self.inventory.resolve_fqn(host, namespace=namespace_of(fqn)) if host else None
            held = self._element(target) if target else None
            if held is not None:
                self.objects[_id("adapter", fqn)].add("attached_to", held.id)
        for fqn, tunnel in self.inventory.tunnels.items():
            carrier = tunnel.spec.over
            target = (
                self.inventory.resolve_fqn(carrier, namespace=namespace_of(fqn))
                if carrier
                else None
            )
            held = self._element(target) if target else None
            if held is not None and held.type == "tunnel":
                self.objects[_id("tunnel", fqn)].add("over", held.id)

    def _identities(self) -> None:
        """``spec.members`` of every group, resolved outwards from its namespace."""
        for fqn, group in self.inventory.groups.items():
            one = self.objects[_id("group", fqn)]
            for member in group.spec.members:
                target = self.inventory.resolve_fqn(member, namespace=namespace_of(fqn))
                held = self._element(target) if target else None
                if held is None:
                    continue
                one.add("members", held.id)
                # Only a user or a group has a ``groups`` link to fill; §19.2
                # allows nothing else in ``spec.members``, and a document that
                # names something else is a validation finding, not a relation.
                if held.type in ("user", "group"):
                    held.add("groups", one.id)

    # -- racks --------------------------------------------------------- #

    def _racks(self) -> None:
        """One object per cabinet anything claims to be mounted in (§3.2)."""
        used: dict[str, int] = {}
        for fqn, element in self.inventory.elements.items():
            location = element.metadata.location
            if location is None or location.rack is None:
                continue
            key = (location.site or "", location.room or "", location.rack)
            rack = self.objects.get(self.racks.get(key, ""))
            if rack is None:
                local = "/".join(key)
                rack = self._new(
                    "rack", local, local, name=key[2], fqn=local, site=key[0], room=key[1]
                )
                self.racks[key] = rack.id
                used[rack.id] = 0
            if location.rack_height is not None and "height" not in rack.props:
                rack.props["height"] = (int(location.rack_height),)
            used[rack.id] += int(location.height or 0)
            held = self._element(fqn)
            if held is not None:
                self._join(rack, "elements", held, "rack")
        for rack_id, occupied in used.items():
            self.objects[rack_id].props["used"] = (occupied,)

    # -- namespaces and zones ------------------------------------------ #

    def _namespaces(self) -> None:
        for fqn, device in self.inventory.devices.items():
            declarations = device.spec.netns
            if not declarations:
                continue
            parents = resolve_netns_tree(declarations)
            for declaration in declarations:
                local = f"{fqn}:{declaration.name}"
                one = self._new(
                    "netns",
                    local,
                    local,
                    name=declaration.name,
                    fqn=local,
                    path="/".join(netns_path(declaration.name, parents)),
                    depth=netns_depth(declaration.name, parents),
                    description=declaration.description,
                )
                self.netns[(fqn, declaration.name)] = one.id
                self._join(one, "device", self.objects[_id(device.kind, fqn)], "netns")
            for declaration in declarations:
                if not declaration.parent:
                    continue
                child = self.objects[self.netns[(fqn, declaration.name)]]
                parent = self.objects.get(self.netns.get((fqn, declaration.parent), ""))
                if parent is not None:
                    self._join(child, "parent", parent, "children")

    def _zones(self) -> None:
        for fqn, device in self.inventory.devices.items():
            rules = device.spec.firewall.rules if device.spec.firewall else []
            for zone in device.spec.zones:
                local = f"{fqn}:{zone.name}"
                one = self._new(
                    "zone",
                    local,
                    local,
                    name=zone.name,
                    fqn=local,
                    description=zone.description,
                    rules=sum(1 for rule in rules if zone.name in (rule.src_zone, rule.dst_zone)),
                )
                self.zones[(fqn, zone.name)] = one.id
                self._join(one, "device", self.objects[_id(device.kind, fqn)], "zones")

    # -- VLANs --------------------------------------------------------- #

    def _vlan_table(self) -> None:
        """Every VLAN id anything mentions, named from the device databases.

        A VLAN is a number before it is a name: an id configured on a trunk that
        no ``spec.vlans`` entry describes is still a VLAN, and leaving it out
        would make ``select vlan`` disagree with ``netviz list vlans``.
        """
        names: dict[int, tuple[str | None, str | None]] = {}
        found: set[int] = set()
        for device in self.inventory.devices.values():
            for declaration in device.spec.vlans:
                found.add(declaration.id)
                names.setdefault(declaration.id, (declaration.name, declaration.description))
        for element in self.inventory.interface_owners.values():
            for interface in element.interfaces:
                if interface.vlan is not None:
                    found |= interface.vlan.vlan_ids()
        for vid in sorted(found):
            name, description = names.get(vid, (None, None))
            one = self._new(
                "vlan", str(vid), f"vlan{vid}", id=vid, name=name, description=description
            )
            self.vlans[vid] = one.id
            for fqn, device in self.inventory.devices.items():
                if any(declaration.id == vid for declaration in device.spec.vlans):
                    self._join(one, "declared_on", self.objects[_id(device.kind, fqn)], "vlans")

    # -- interfaces and addresses -------------------------------------- #

    def _interfaces(self) -> None:
        for fqn, element in self.inventory.elements.items():
            for interface in _ports_of(element):
                self._interface(fqn, element, interface)
            if isinstance(element, Adapter):
                self._upstream(fqn, element)
        # Resolved once every port of every element exists, because each of
        # these names another port of the same element.
        for fqn, element in self.inventory.elements.items():
            for interface in _ports_of(element):
                self._interface_relations(fqn, interface)

    def _interface(self, fqn: str, element: Element, interface: Interface) -> None:
        owner = self.objects[_id(element.kind, fqn)]
        wireless = interface.wireless
        vlan = interface.vlan
        local = f"{fqn}:{interface.name}"
        one = self._new(
            "interface",
            local,
            local,
            name=interface.name,
            fqn=local,
            type=interface.type,
            description=interface.description,
            enabled=bool(interface.enabled),
            mac=str(interface.mac) if interface.mac else None,
            mtu=interface.mtu,
            vlan_mode=vlan.mode if vlan is not None else None,
            pvid=vlan.pvid if vlan is not None else None,
            vrf=interface.vrf,
            netns_name=interface.netns_name,
            is_veth=bool(interface.is_veth),
            ssid=wireless.ssids if wireless else (),
            band=wireless.band if wireless else None,
            channel=wireless.channel if wireless else None,
            radio_role=wireless.role if wireless else None,
            poe=interface.poe is not None,
        )
        self.interfaces[(fqn, interface.name)] = one.id
        self._join(one, "parent", owner, "interfaces")

        for vid in sorted(vlan.vlan_ids()) if vlan is not None else ():
            held = self.objects[self.vlans[vid]]
            self._join(one, "vlans", held, "interfaces")
            held.add("elements", owner.id)
        namespace = self.objects.get(self.netns.get((fqn, interface.netns_name), ""))
        if namespace is not None:
            self._join(one, "netns", namespace, "interfaces")
        for zone in getattr(getattr(element, "spec", None), "zones", ()):
            if interface.name in zone.interfaces:
                self._join(one, "zone", self.objects[self.zones[(fqn, zone.name)]], "interfaces")
        self._addresses(fqn, owner, one, interface)

    def _upstream(self, fqn: str, adapter: Adapter) -> None:
        """The adapter's host-facing port, which a cable may terminate on (§8.1).

        It is not an entry of ``spec.interfaces`` — it is ``spec.upstream``, a
        USB or Thunderbolt or SFP socket — so it is built here rather than
        forced through the interface model. Its ``type`` is the bus it is on,
        which is the honest answer and the one a query would want.
        """
        upstream = adapter.spec.upstream
        local = f"{fqn}:{upstream.name}"
        one = self._new(
            "interface",
            local,
            local,
            name=upstream.name,
            fqn=local,
            type=upstream.type,
            enabled=True,
            netns_name="",
            is_veth=False,
            poe=False,
        )
        self.interfaces[(fqn, upstream.name)] = one.id
        self._join(one, "parent", self.objects[_id("adapter", fqn)], "interfaces")

    def _addresses(self, fqn: str, owner: Obj, port: Obj, interface: Interface) -> None:
        gateways = {str(address) for _, address in interface.gateways()}
        for index, address in enumerate(interface.addresses()):
            ip = str(address.ip)
            one = self._new(
                "address",
                f"{fqn}:{interface.name}#{index}",
                f"{ip}/{address.prefix_length}",
                address=f"{ip}/{address.prefix_length}",
                ip=ip,
                prefix_length=address.prefix_length,
                network=str(address.network),
                family=address.network.version,
                vrf=interface.vrf or GLOBAL_VRF,
                netns_name=interface.netns_name,
                is_routable=is_routable_address(address),
                is_gateway=ip in gateways,
            )
            self._join(one, "interface", port, "addresses")
            one.add("element", owner.id)
            owner.add("addresses", one.id)
            namespace = self.objects.get(self.netns.get((fqn, interface.netns_name), ""))
            if namespace is not None:
                namespace.add("addresses", one.id)

    def _interface_relations(self, fqn: str, interface: Interface) -> None:
        """The links from a port to other ports of the same element."""
        one = self.objects[self.interfaces[(fqn, interface.name)]]
        for name in interface.members or ():
            member = self.objects.get(self.interfaces.get((fqn, name), ""))
            if member is not None:
                self._join(one, "members", member, "member_of")
        for link, named in (("base", interface.parent), ("veth_peer", interface.peer)):
            held = self.objects.get(self.interfaces.get((fqn, named or ""), ""))
            if held is not None:
                one.add(link, held.id)

    # -- routes -------------------------------------------------------- #

    def _routes(self) -> None:
        for fqn, device in self.inventory.devices.items():
            owner = self.objects[_id(device.kind, fqn)]
            for index, route in enumerate(device.spec.routes):
                network = ipaddress.ip_network(str(route.prefix), strict=False)
                one = self._new(
                    "route",
                    f"{fqn}#{index}",
                    f"{fqn} {route.prefix}",
                    destination=str(route.prefix),
                    via=str(route.via) if route.via is not None else None,
                    interface_name=route.dev,
                    vrf=route.vrf or GLOBAL_VRF,
                    table=route.table,
                    metric=route.metric,
                    family=network.version,
                    blackhole=bool(route.blackhole),
                )
                self._join(one, "device", owner, "routes")
                port = self.objects.get(self.interfaces.get((fqn, route.dev or ""), ""))
                if port is not None:
                    one.add("interface", port.id)

    # -- subnets ------------------------------------------------------- #

    def _subnets(self) -> None:
        for subnet in subnets_of(self.inventory):
            used = len({member.ip for member in subnet.members})
            size = subnet.network.num_addresses
            local = subnet.node_id.partition(":")[2]
            one = self._new(
                "subnet",
                local,
                subnet.label,
                prefix=subnet.prefix,
                fqn=local,
                vrf=subnet.vrf,
                family=subnet.version,
                prefix_length=subnet.network.prefixlen,
                size=size,
                used=used,
                free=max(0, size - used),
                utilisation=round(used / size, 6) if size else 0.0,
                is_point_to_point=subnet.is_point_to_point,
            )
            for member in subnet.members:
                port = self.objects.get(self.interfaces.get((member.element, member.interface), ""))
                if port is None:
                    continue
                self._join(one, "interfaces", port, "subnets")
                owner = self.objects[port.links["parent"][0]]
                self._join(one, "elements", owner, "subnets")
                for address in port.links.get("addresses", ()):
                    if self.objects[address].props.get("address") == (member.address,):
                        self._join(one, "addresses", self.objects[address], "subnet")
                for vid in sorted(member.vlans):
                    self._join(one, "vlans", self.objects[self.vlans[vid]], "subnets")

    # -- links and broadcast domains ----------------------------------- #

    def _links(self) -> None:
        """One object per edge of the layer-1 graph a renderer would draw."""
        for _, _, data in self.graph.edges(data=True):
            if str(data.get("kind")) not in _LINK_KINDS:
                continue
            one = self._new(
                "link",
                str(data["id"]),
                str(data["id"]),
                id=str(data["id"]),
                kind=str(data["kind"]),
                medium=str(data.get("medium") or "") or None,
                speed=data.get("speed"),
                label=data.get("label"),
            )
            for end, port in (
                (str(data.get("source", "")), str(data.get("source_port", ""))),
                (str(data.get("target", "")), str(data.get("target_port", ""))),
            ):
                element = self._element(_element_fqn(end))
                if element is None:
                    continue
                self._join(one, "elements", element, "links")
                interface = self.objects.get(self.interfaces.get((_element_fqn(end), port), ""))
                if interface is not None:
                    self._join(one, "interfaces", interface, "links")
            for vid in sorted(data.get("vlans", ())):
                if vid in self.vlans:
                    self._join(one, "vlans", self.objects[self.vlans[vid]], "links")
            self._link_document(one, data)
            if one.props.get("kind") == ("cable",):
                ports = one.links.get("interfaces", ())
                if len(ports) == 2:
                    self.objects[ports[0]].add("peer", ports[1])
                    self.objects[ports[1]].add("peer", ports[0])
        self._neighbours()

    def _link_document(self, one: Obj, data: Mapping[str, Any]) -> None:
        """Point the link at the cable or tunnel it came from, and it back."""
        edge = data.get("edge")
        document = getattr(edge, "document", None) if edge is not None else None
        if document is None:
            return
        declared = self.objects.get(_id(document.kind, str(data["id"]).partition("#")[0]))
        if declared is None:
            return
        one.add("element", declared.id)
        if declared.type not in ("cable", "tunnel"):
            return
        reverse = "cable" if declared.type == "cable" else "tunnels"
        for port in one.links.get("interfaces", ()):
            self._join(declared, "ends", self.objects[port], reverse)
            declared.add("elements", self.objects[port].links["parent"][0])

    def _neighbours(self) -> None:
        """``element.neighbors``: what a link's other end belongs to.

        Elements only. A broadcast domain also has a ``links`` member — the
        cables inside it — and walking it here would give a domain "neighbours",
        which is a member no type declares and a question nobody asked.
        """
        for one in self.objects.values():
            if not SCHEMA.is_subtype(one.type, "element"):
                continue
            for link in one.links.get("links", ()):
                for far in self.objects[link].links.get("elements", ()):
                    if far != one.id:
                        one.add("neighbors", far)

    def _domains(self) -> None:
        for domain in broadcast_domains(self.graph):
            one = self._new(
                "broadcast_domain",
                domain.id.replace(":", "_", 1),
                domain.name,
                id=domain.id,
                name=domain.name,
                vlan_id=domain.vlan,
                index=domain.index,
                size=len(domain.members),
                is_isolated=domain.is_isolated,
            )
            vlan = self.objects.get(self.vlans.get(domain.vlan, ""))
            if vlan is not None:
                self._join(one, "vlan", vlan, "broadcast_domains")
            for fqn in domain.members:
                element = self._element(_element_fqn(fqn))
                if element is None:
                    continue
                self._join(one, "members", element, "broadcast_domains")
                for port in element.links.get("interfaces", ()):
                    interface = self.objects[port]
                    if vlan is not None and vlan.id in interface.links.get("vlans", ()):
                        self._join(one, "interfaces", interface, "broadcast_domains")
            for link in domain.links:
                held = self.objects.get(_id("link", link))
                if held is not None:
                    self._join(one, "links", held, "broadcast_domains")


# --------------------------------------------------------------------------- #
# Reading the models
# --------------------------------------------------------------------------- #


def _spec_of(element: Element) -> dict[str, Any]:
    """The per-kind scalars of one element, each read straight off the model."""
    if isinstance(element, Device):
        routing = element.spec.routing
        bgp = routing.bgp if routing else None
        ospf = routing.ospf if routing else None
        forwarding = element.spec.forwarding
        return {
            "vendor": element.spec.vendor,
            "model": element.spec.model,
            "serial": element.spec.serial,
            "location": element.spec.location,
            "asn": bgp.asn if bgp else None,
            "router_id": (bgp.router_id if bgp else None) or (ospf.router_id if ospf else None),
            "forwards": (
                bool(forwarding.ipv4 or forwarding.ipv6) if forwarding is not None else None
            ),
        }
    if isinstance(element, Adapter):
        return {
            "vendor": element.spec.vendor,
            "model": element.spec.model,
            "serial": element.spec.serial,
            "form_factor": element.spec.form_factor,
            "passthrough": bool(element.spec.passthrough),
            "upstream": element.spec.upstream.name,
            "upstream_type": element.spec.upstream.type,
        }
    if isinstance(element, PatchPanel):
        return {
            "vendor": element.spec.vendor,
            "model": element.spec.model,
            "serial": element.spec.serial,
            "form_factor": element.spec.form_factor,
            "ports": len(element.port_numbers),
        }
    if isinstance(element, Pdu):
        return {
            "vendor": element.spec.vendor,
            "model": element.spec.model,
            "serial": element.spec.serial,
            "form_factor": element.spec.form_factor,
            "outlets": len(element.outlet_numbers),
            "capacity_watts": element.capacity_watts,
            "input_feed": element.input_feed or None,
        }
    if isinstance(element, Cable):
        return {
            "medium": element.spec.medium,
            "speed": element.spec.speed,
            "duplex": element.spec.duplex,
            "length_m": element.spec.length_m,
            "category": element.spec.category,
            "connector": element.spec.connector,
            "label": element.spec.label,
        }
    if isinstance(element, Tunnel):
        return {
            "type": element.spec.type,
            "layer": element.spec.type.layer,
            "encrypted": bool(element.encrypts),
            "vni": element.spec.vni,
            "mtu": element.spec.mtu,
            "port": element.spec.port,
            "mode": element.spec.mode,
            "multipoint": bool(element.is_multipoint),
            "label": element.spec.label,
        }
    if isinstance(element, User):
        return {
            "login": element.login,
            "full_name": element.spec.full_name,
            "email": element.spec.email,
            "uid": element.spec.uid,
            "type": element.spec.type,
            "status": element.spec.status,
            "keys": len(element.spec.ssh_keys),
        }
    # Every element kind of §3 is covered above; what is left is a group.
    return {"gid": element.spec.gid, "email": element.spec.email}


def _ports_of(element: Element) -> Sequence[Interface]:
    """Every declared interface of an element; empty for the kinds that own none."""
    if isinstance(element, (Device, Adapter, PatchPanel)):
        return element.interfaces
    return []


def _element_fqn(node: str) -> str:
    """The element a layer-1 node id stands for.

    A multipoint tunnel is drawn as a node rather than an edge, and its id is
    the tunnel's fqn behind a ``tunnel:`` prefix; every other layer-1 node is an
    element and its id is the fqn itself.
    """
    return node[len(_TUNNEL_NODE) :] if node.startswith(_TUNNEL_NODE) else node


def _scalars(**values: Any) -> dict[str, tuple[Value, ...]]:
    """Normalise read model values into the tuples a property holds.

    ``None`` is dropped rather than stored: an absent property is the empty
    tuple, which is what makes ``exists .vendor`` and ``.vendor = 'Cisco'``
    agree about a device that declares none.
    """
    found: dict[str, tuple[Value, ...]] = {}
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, (tuple, list, frozenset, set)):
            found[name] = tuple(_scalar(one) for one in value)
        else:
            found[name] = (_scalar(value),)
    return found


def _pairs(mapping: Mapping[str, str]) -> tuple[str, ...]:
    """A label or annotation map as ``key=value`` entries, in declaration order.

    Flattened rather than modelled as its own object type because a label is not
    a thing in the network — it is a *tag on* one — and the only question anybody
    asks of it is the value of one key, which ``lookup(.labels, 'role')``
    answers. A label key cannot hold an ``=`` (``NV-N003``), so splitting on the
    first one always splits in the right place.
    """
    return tuple(f"{key}={value}" for key, value in mapping.items())


def _scalar(value: Any) -> Value:
    """One model value as the executor compares it."""
    if isinstance(value, Enum):
        return _scalar(value.value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return str(value)
