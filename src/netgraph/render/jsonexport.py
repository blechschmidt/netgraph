"""JSON graph exporter — the machine-readable face of a rendering.

Where DOT and Mermaid are pictures, this is data: the resolved topology as a
document another tool can consume without reimplementing name resolution, VLAN
derivation or adapter attachment semantics. It is the format to reach for when
diffing two revisions of an inventory, feeding a dashboard, or asserting on a
topology in a test.

Every reference is a fully-qualified name, every collection is ordered
deterministically, and enums appear as their schema spelling — so two runs over
the same inventory produce byte-identical output.

Routing
-------

A ``--layer routing`` document is element nodes only, each with a ``routing``
object naming the AS, the router id, the OSPF area, the VRFs and the static
routes, and each carrying ``cluster`` — the VRF box it is drawn in. Its edges are
``bgp`` and ``ospf``, and both carry an ``adjacency`` object with the AS pair or
the area. Like ``subnet`` and ``tunnel``, none of it is gated on a display flag:
at this layer the control plane *is* the topology.

Node types
----------

Every node carries a ``type`` discriminator: ``element``, ``subnet``, ``tunnel``,
``rack`` or ``aggregate``. Below layer 3 the first dominates; a layer-3 document mixes
element and subnet nodes and an ``overlay`` document mixes element and tunnel
nodes, so a consumer must be able to tell a derived prefix or a tunnel from a
declared device without guessing from the identifier. A ``subnet`` node adds a
``subnet`` object — the prefix, the family, the addresses inside it and the
elements holding them — and a ``tunnel`` node adds a ``tunnel`` object; neither
carries interfaces. An ``element`` node is exactly what it is at layer 1. A
``rack`` node — the whole of a ``--layer rack`` document — adds a ``rack``
object holding the elevation: how tall the cabinet is, and which units each
element occupies.

Patch panels
------------

Below ``--layer physical`` a run through a patch panel is exported as the one
edge it electrically is, with a ``patch`` object naming the cable segments and
the panel positions it crosses (§15.2). A consumer that wants the segments
themselves renders ``--layer physical``, where the panel is a node and each
segment an edge of its own.

Aggregates
----------

``--collapse`` replaces a whole namespace with one node, and ``--bundle-links``
one set of parallel links with one edge. Both are marked, because a consumer
that read either as a single device or a single cable would be misled about the
network rather than merely about the picture:

* an ``aggregate`` node carries an ``aggregate`` object naming the namespace,
  **every element it stands for**, the count per kind, the VLANs and prefixes
  they participate in, and the ids of the links that ran wholly inside it and
  are therefore absent from ``edges``;
* a bundled edge carries a ``bundle`` object with the member count and the full
  record of each member link, exported by the same code that exports an
  unbundled one — so nothing is lost by asking for the summary.
"""

from __future__ import annotations

import json
from typing import Any, Final

from netgraph.models import API_VERSION
from netgraph.render.aggregate import AggregateView, BundleView
from netgraph.render.graph import (
    AdjacencyView,
    Edge,
    EdgeKind,
    Graph,
    Node,
    PatchView,
    PortView,
    RackView,
    RoutingView,
    Subnet,
    TunnelView,
    WirelessView,
)
from netgraph.render.options import RenderOptions

__all__ = ["GRAPH_KIND", "graph_to_dict", "render_json", "to_json"]

#: ``kind`` of the exported document, mirroring the element envelope of §3.
GRAPH_KIND: Final = "NetworkGraph"


def to_json(graph: Graph, options: RenderOptions | None = None, *, indent: int = 2) -> str:
    """Render ``graph`` as a JSON document, newline-terminated.

    The document is node-link shaped and versioned by the ``apiVersion`` /
    ``kind`` pair every netgraph document carries, so a consumer can pin what it
    parses::

        {
          "apiVersion": "netgraph.dev/v1alpha1",
          "kind": "NetworkGraph",
          "layer": "l1",
          "title": "…",              # only when a title was given
          "nodes": [ … ],
          "edges": [ … ],
          "dangling": [ … ]          # only under --force
        }

    A node is ``{id, type, name, kind, namespace, vlans, interfaces}`` plus the
    optional ``description``, ``labels`` and — for a layer-3 prefix node or a
    tunnel node — ``subnet`` or ``tunnel``. ``id`` is the fully-qualified name,
    and is what every edge endpoint refers to. An edge is
    ``{id, kind, endpoints, vlans}`` plus the medium, speed, label and length it
    was declared with, or the ``tunnel`` object when it is one; ``endpoints`` is
    a two-element list of ``{node, interface}``, with ``interface`` absent when
    the edge attaches to an element rather than to one of its ports.

    Within one ``apiVersion`` these keys are only added, never renamed or
    removed, and an absent optional key means "not configured" rather than
    "unknown".
    """
    payload = graph_to_dict(graph, options)
    return json.dumps(payload, indent=indent, ensure_ascii=False, sort_keys=False) + "\n"


def graph_to_dict(graph: Graph, options: RenderOptions | None = None) -> dict[str, Any]:
    """The graph as plain JSON-compatible data.

    ``show_ips`` and ``show_vlans`` control the *per-port* detail, exactly as
    they control what a diagram prints. Node and link VLAN membership is always
    exported: it is topology, not decoration, and a consumer filtering by VLAN
    would otherwise have to recompute it.
    """
    opts = options or RenderOptions()
    document: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": GRAPH_KIND,
        "layer": graph.layer.value,
        "nodes": [_node(node, opts) for node in graph.nodes.values()],
        "edges": [_edge(edge) for edge in graph.edges],
    }
    if opts.title:
        document["title"] = opts.title
    if graph.dangling:
        # Only reachable behind ``--force``; a consumer must be able to tell that
        # the export is missing links rather than that the links do not exist.
        document["dangling"] = list(graph.dangling)
    return document


def _node(node: Node, options: RenderOptions) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": node.fqn,
        "type": node.type.value,
        "name": node.name,
        "kind": node.kind,
        "namespace": node.namespace,
    }
    if node.description:
        payload["description"] = node.description
    if node.labels:
        payload["labels"] = dict(sorted(node.labels.items()))
    if node.subnet is not None:
        payload["subnet"] = _subnet(node.subnet)
    if node.tunnel is not None:
        payload["tunnel"] = _tunnel(node.tunnel)
    if node.aggregate is not None:
        payload["aggregate"] = _aggregate(node.aggregate)
    if node.rack is not None:
        payload["rack"] = _rack(node.rack)
    if node.routing is not None:
        payload["routing"] = _routing(node.routing)
    if node.cluster:
        payload["cluster"] = node.cluster
    payload["vlans"] = sorted(node.vlans)
    payload["interfaces"] = [_port(port, options) for port in node.ports]
    return payload


def _aggregate(view: AggregateView) -> dict[str, Any]:
    """A collapsed namespace: what it stands for, in full.

    ``elements`` is the point of the object. A consumer holding one aggregate
    node must be able to get back to the devices behind it — to count them, to
    look them up in ``netgraph render -f json`` without ``--collapse``, or to
    refuse to treat the box as a device. Like ``subnet`` and ``tunnel``, none of
    it is gated on a display flag: which elements a box stands for is topology,
    not decoration.
    """
    return {
        "namespace": view.namespace,
        "elements": list(view.elements),
        "elementCount": view.size,
        "countsByKind": dict(view.by_kind),
        "namespaces": list(view.namespaces),
        "vlans": sorted(view.vlans),
        "subnets": list(view.subnets),
        # The links the summary swallowed. Absent from ``edges`` by
        # construction, so a consumer counting cables needs them named.
        "internalLinks": list(view.internal_links),
    }


def _rack(view: RackView) -> dict[str, Any]:
    """A rack node's elevation: the cabinet, and what is bolted where.

    ``units`` is every unit from the bottom up, occupied or free, because the
    free space is half of what an elevation says; ``slots`` is the same fact
    keyed by element, for a consumer that wants to look one up rather than walk
    the cabinet.
    """
    return {
        "site": view.site,
        "room": view.room,
        "name": view.name,
        "label": view.label,
        "height": view.height,
        "heightInferred": view.inferred_height,
        "usedUnits": view.used_units,
        "slots": [
            {
                "element": slot.element,
                "name": slot.name,
                "kind": slot.kind,
                "position": slot.position,
                "height": slot.height,
            }
            for slot in view.slots
        ],
        "units": [
            {"unit": unit, "element": slot.element if slot is not None else None}
            for unit, slot in reversed(view.elevation())
        ],
    }


def _patch(view: PatchView) -> dict[str, Any]:
    """The passive cross-connects one spliced edge runs through (§15.2)."""
    return {
        "segments": list(view.segments),
        "panels": [
            {"panel": hop.panel, "ingress": hop.ingress, "egress": hop.egress} for hop in view.hops
        ],
    }


def _subnet(subnet: Subnet) -> dict[str, Any]:
    """A subnet node's prefix, its family and who is addressed in it.

    Unlike the per-port address list, this is not gated on ``show_ips``: at
    layer 3 the addresses *are* the topology, exactly as VLAN membership is, and
    a consumer filtering by prefix would otherwise have to recompute them.
    """
    payload: dict[str, Any] = {
        "prefix": subnet.prefix,
        "family": subnet.family,
        "addresses": list(subnet.addresses),
        "elements": list(subnet.elements),
    }
    if subnet.vrf:
        # Absent for the global instance, so a consumer reading a document from
        # an inventory with no VRF in it sees exactly what it saw before (§16.1).
        payload["vrf"] = subnet.vrf
    return payload


def _routing(view: RoutingView) -> dict[str, Any]:
    """What one element contributes to the control plane (§16.6).

    Only what the inventory states is emitted, so a consumer can tell "runs no
    OSPF" from "runs OSPF in area 0.0.0.0" — which a defaulted area would not
    allow. ``routes`` is the rendered form (``0.0.0.0/0 via 203.0.113.1``), the
    same string the diagram prints; a consumer that wants the fields reads the
    inventory, which is where they are declared.
    """
    payload: dict[str, Any] = {}
    if view.asn is not None:
        payload["asn"] = view.asn
    if view.router_id is not None:
        payload["routerId"] = view.router_id
    if view.area is not None:
        payload["ospfArea"] = view.area
        payload["ospfInterfaces"] = list(view.ospf_interfaces)
    if view.vrfs:
        payload["vrfs"] = [{"name": name, "rd": rd} for name, rd in view.vrfs]
    if view.routes:
        payload["routes"] = list(view.routes)
    return payload


def _adjacency(view: AdjacencyView) -> dict[str, Any]:
    """One protocol adjacency: which protocol, and what it is between.

    ``internal`` is stated rather than left to be derived from the AS pair: iBGP
    versus eBGP is the first question anybody asks of a session, and a consumer
    should not have to know that equal AS numbers mean one.
    """
    payload: dict[str, Any] = {"protocol": view.protocol}
    if view.peer_address:
        payload["peerAddress"] = view.peer_address
    if view.asns:
        payload["asns"] = list(view.asns)
        if len(view.asns) == 2:
            payload["internal"] = view.is_internal
    if view.area is not None:
        payload["area"] = view.area
    if view.description:
        payload["description"] = view.description
    return payload


def _tunnel(view: TunnelView) -> dict[str, Any]:
    """A tunnel's encapsulation, its endpoints and where it sits in the stack.

    Like ``subnet``, this is not gated on any display flag: at the overlay layer
    the encapsulation *is* the topology, and a consumer asking "what carries
    this VXLAN?" would otherwise have to re-resolve ``over`` itself.
    """
    spec = view.tunnel.spec
    payload: dict[str, Any] = {
        "id": view.fqn,
        "type": view.type,
        "layer": view.layer,
        "transport": spec.type.transport.value,
        "endpoints": [{"node": end.element, "interface": end.interface} for end in view.ends],
        "encrypted": view.encrypted,
        "protected": view.protected,
        # The stack, innermost first, is what makes ``vxlan over ipsec`` a fact
        # a consumer can read rather than a phrase it has to parse.
        "stack": list(view.stack),
        "depth": view.depth,
        "overheadBytes": view.overhead_bytes,
    }
    if view.over is not None:
        payload["over"] = view.over
    if view.encrypted_by is not None:
        payload["encryptedBy"] = view.encrypted_by
    if spec.port is not None:
        payload["port"] = spec.port
    if spec.mode is not None:
        payload["mode"] = spec.mode.value
    if view.vni is not None:
        payload["vni"] = view.vni
    if spec.cipher:
        payload["cipher"] = spec.cipher
    if spec.auth is not None:
        payload["auth"] = spec.auth.value
    if view.mtu is not None:
        payload["mtu"] = view.mtu
    return payload


def _port(port: PortView, options: RenderOptions) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": port.name, "type": port.type}
    if not port.enabled:
        payload["enabled"] = False
    if port.description:
        payload["description"] = port.description
    if port.mac:
        payload["mac"] = port.mac
    if port.mtu is not None:
        payload["mtu"] = port.mtu
    if options.show_ips and port.addresses:
        payload["addresses"] = list(port.addresses)
    if options.show_vlans and port.vlan_mode is not None:
        payload["vlan"] = {"mode": port.vlan_mode, "vlans": sorted(port.vlans)}
    return payload


def _edge(edge: Edge) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": edge.id,
        "kind": edge.kind.value,
        "endpoints": [
            {"node": node, "interface": port} if port else {"node": node}
            for node, port in edge.endpoints()
        ],
    }
    if edge.kind is EdgeKind.SUBNET:
        # A membership runs over no medium; the addresses are what it *is*.
        payload["addresses"] = list(edge.addresses)
    elif edge.adjacency is not None:
        # Nor does a routing session: it runs over the rest of the diagram.
        payload["adjacency"] = _adjacency(edge.adjacency)
    elif edge.tunnel is not None:
        # Neither does a tunnel: what it runs over is the rest of the diagram.
        payload["tunnel"] = _tunnel(edge.tunnel)
    else:
        payload["medium"] = edge.medium
    if edge.speed is not None:
        payload["speed"] = edge.speed
        payload["speedText"] = edge.speed_text
    if edge.label:
        payload["label"] = edge.label
    if edge.length_m is not None:
        payload["lengthM"] = edge.length_m
    if edge.wireless is not None:
        payload["wireless"] = _wireless(edge.wireless)
    if edge.patch is not None:
        payload["patch"] = _patch(edge.patch)
    if edge.bundle is not None:
        payload["bundle"] = _bundle(edge.bundle)
    payload["vlans"] = sorted(edge.vlans)
    return payload


def _wireless(view: WirelessView) -> dict[str, Any]:
    """The association a radio link is: the network, and where on the air.

    Only what the inventory states is emitted. A consumer can tell "no channel
    recorded" from "channel 1" — which a defaulted zero would not allow — and
    the access point is named so that the direction of the association survives
    an export, the one thing an undirected edge cannot carry by itself.
    """
    payload: dict[str, Any] = {}
    if view.ssids:
        payload["ssids"] = list(view.ssids)
    if view.band is not None:
        payload["band"] = view.band
    if view.channel is not None:
        payload["channel"] = view.channel
    if view.width_mhz is not None:
        payload["widthMhz"] = view.width_mhz
    if view.access_point:
        payload["accessPoint"] = view.access_point
    return payload


def _bundle(view: BundleView) -> dict[str, Any]:
    """The links one drawn edge stands for, each exported in full.

    Recursion is safe and shallow: bundling flattens, so a member is never
    itself a bundle.
    """
    payload: dict[str, Any] = {"size": view.size, "links": [_edge(edge) for edge in view.edges]}
    if view.aggregate is not None:
        source, target = view.aggregate
        # A declared link aggregation, not a set of links that merely run in
        # parallel: the inventory said these are one logical link.
        payload["aggregate"] = [
            {"node": node, "interface": interface}
            for node, interface in ((view.edges[0].source, source), (view.edges[0].target, target))
            if interface
        ]
    return payload


#: Kept so callers written against the original name keep working; ``to_json``
#: is the canonical spelling, matching ``to_dot`` and ``to_mermaid``.
render_json = to_json
