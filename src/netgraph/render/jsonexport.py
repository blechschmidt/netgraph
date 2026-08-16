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

Style
-----

Every node and every edge carries a ``style`` object: the *resolved* appearance
(§22), with a ``from`` map saying which rung of the ladder supplied each value —
``element``, ``theme:<name>#<rule>``, ``icons`` or ``default``. It is what lets
a consumer that draws the graph itself, rather than reading a picture netgraph
drew, produce the same colours; and it is what the editor's style inspector
reads to say "this navy is the theme's, not yours" before offering to reset it.

The object is always present, because the ladder always terminates in the
built-in palette. Under ``--no-style`` it is the palette's answer alone, and
every entry in ``from`` says ``default``.

Annotations
-----------

A document whose view declares any (§21) carries a top-level ``annotations``
object with ``notes``, ``areas`` and ``legends``. It is the contract the browser
and every downstream tool read, so it is complete: each entry carries its stable
id, the fully-qualified name of the document behind it, its resolved members or
anchor, its colours and whatever geometry it pins. A note carries its text
*twice* — once as the markdown-subset source and once as the parse
(:func:`~netgraph.render.annotations.parse_markup`) — so that a consumer draws
the same bold word netgraph draws without reimplementing the parser, and a
generated legend carries the swatches it generated rather than the instruction
to generate them, because the entries depend on what this drawing drew.

None of it is topology, and none of it appears in ``nodes`` or ``edges``. The
key is absent when the view declares nothing and absent when annotations are
turned off, so a document from an inventory with no annotations in it is
byte-identical to what it always was.
"""

from __future__ import annotations

import json
from typing import Any, Final

from netgraph.layout.geometry import Geometry, Placement
from netgraph.layout.routing import Route
from netgraph.models import API_VERSION
from netgraph.power import Feed, PowerNode
from netgraph.render.aggregate import AggregateView, BundleView
from netgraph.render.annotations import AreaView, LegendView, NoteView, annotation_views
from netgraph.render.diffview import DiffOverlay, Mark
from netgraph.render.graph import (
    AdjacencyView,
    Edge,
    EdgeKind,
    Graph,
    NetnsView,
    Node,
    PatchView,
    PolicyView,
    PortView,
    RackView,
    RoutingView,
    SecurityView,
    Subnet,
    TunnelView,
    WirelessView,
)
from netgraph.render.options import RenderOptions
from netgraph.render.routes import default_routing, route_plan
from netgraph.render.styles import PLAIN, ResolvedStyle, StyleMap

__all__ = ["GRAPH_KIND", "annotations_payload", "graph_to_dict", "render_json", "to_json"]

#: ``kind`` of the exported document, mirroring the element envelope of §3.
GRAPH_KIND: Final = "NetworkGraph"

#: What a helper called without one resolves against: nothing was styled, so
#: every field is absent and ``from`` is empty.
_NO_STYLES: Final = StyleMap()


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
    # The resolved appearance travels *with* the graph: a consumer drawing
    # this document itself — the editor's canvas, a dashboard — must be able
    # to draw what netgraph drew without reimplementing the ladder of §22,
    # and the provenance is what lets the editor's inspector say which rung
    # each value came from.
    styles = StyleMap.build(
        graph, theme=opts.theme, icons=opts.icons, output="svg", styling=opts.styling
    )
    plan = route_plan(graph, opts)
    document: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": GRAPH_KIND,
        "layer": graph.layer.value,
        "nodes": [_node(node, opts, graph.geometry, styles) for node in graph.nodes.values()],
        "edges": [
            _edge(edge, graph.geometry, opts.diff, line, styles.edge(index), computed)
            for index, (edge, line, computed) in enumerate(
                zip(graph.edges, plan.routes, plan.computed, strict=True)
            )
        ],
    }
    if opts.diff is not None:
        # The changeset travels *with* the graph rather than in a second file:
        # a consumer of a diff needs both, and two documents that could be
        # paired wrongly is the failure this avoids.
        document["diff"] = opts.diff.to_dict()
        if opts.diff.changeset is not None:
            document["changeset"] = dict(opts.diff.changeset)
    layout = _layout(graph)
    if layout is not None:
        document["layout"] = layout
    annotations = annotations_payload(graph, opts)
    if annotations is not None:
        document["annotations"] = annotations
    if opts.title:
        document["title"] = opts.title
    if graph.dangling:
        # Only reachable behind ``--force``; a consumer must be able to tell that
        # the export is missing links rather than that the links do not exist.
        document["dangling"] = list(graph.dangling)
    return document


def _layout(graph: Graph) -> dict[str, Any] | None:
    """The stored arrangement, for a client that draws the graph itself.

    ``mode`` is what the SVG renderer decided (§18): ``fixed`` means every
    coordinate in this document is exactly where netgraph would draw it, and a
    client can lay nothing out at all. ``partial`` means some are, and the rest
    are the client's problem — the same problem netgraph hands to Graphviz.
    ``auto``, and the key is left out entirely, so an inventory with no
    arrangement exports byte-identically to what it always did.

    Coordinates are points, ``y`` upwards, a position being the centre of what
    it places — Graphviz's system, which is also the SVG the ``svg`` and ``html``
    renderers emit, so the three agree by construction rather than by promise.
    """
    geometry = graph.geometry
    if geometry.is_empty:
        return None
    payload: dict[str, Any] = {
        "units": "points",
        "mode": str(geometry.mode(graph.nodes)),
        # What a link is drawn as when it does not say for itself. Published
        # beside the coordinates because a client redrawing this graph needs it
        # to reproduce the picture, and it is a property of the arrangement
        # rather than of any one link.
        "routing": str(default_routing(graph)),
    }
    if geometry.groups:
        payload["groups"] = {
            key: {
                "position": {"x": box.x, "y": box.y},
                "size": {"width": box.width, "height": box.height},
            }
            for key, box in sorted(geometry.groups.items())
        }
    return payload


def annotations_payload(
    graph: Graph, options: RenderOptions | None = None
) -> dict[str, Any] | None:
    """The notes, areas and legends of this drawing (§21), or ``None``.

    Resolved by :func:`~netgraph.render.annotations.annotation_views`, the same
    call the picture backends make, so a consumer that draws this document
    itself boxes the same elements and generates the same key. Members and
    anchors are already narrowed to what this graph holds: an exporter that
    published a member the ``nodes`` array does not contain would be handing out
    a dangling reference.
    """
    views = annotation_views(graph, options)
    if not views:
        return None
    payload: dict[str, Any] = {}
    if views.notes:
        payload["notes"] = [_note(note) for note in views.notes]
    if views.areas:
        payload["areas"] = [_area(area) for area in views.areas]
    if views.legends:
        payload["legends"] = [_legend(legend) for legend in views.legends]
    return payload


def _note(view: NoteView) -> dict[str, Any]:
    """One callout: what it says, what it is about, and where it sits."""
    payload: dict[str, Any] = {
        "id": view.id,
        "fqn": view.fqn,
        "text": view.text,
        # The parse, so a client renders the same emphasis without owning a
        # markdown implementation; see the module docstring.
        "blocks": [
            {
                "kind": block.kind,
                "spans": [
                    {"style": span.style, "text": span.text} if span.style else {"text": span.text}
                    for span in block.spans
                ],
            }
            for block in view.lines
        ],
        "color": view.fill,
        "stroke": view.stroke,
        "leader": view.leader,
    }
    if view.anchor:
        payload["anchor"] = view.anchor
    geometry = _annotation_geometry(view.x, view.y, view.width, view.height)
    if geometry:
        payload["layout"] = geometry
    return payload


def _area(view: AreaView) -> dict[str, Any]:
    """One zone: what it encloses, how it is drawn, and any rectangle it pins."""
    payload: dict[str, Any] = {
        "id": view.id,
        "fqn": view.fqn,
        "members": list(view.members),
        "color": view.fill,
        "stroke": view.stroke,
        "border": view.border,
        "padding": view.padding,
    }
    if view.label:
        payload["label"] = view.label
    if view.box is not None:
        payload["layout"] = _annotation_geometry(
            view.box.x, view.box.y, view.box.width, view.box.height
        )
    return payload


def _legend(view: LegendView) -> dict[str, Any]:
    """One key, with ``auto: layers`` already resolved into its swatches."""
    payload: dict[str, Any] = {
        "id": view.id,
        "fqn": view.fqn,
        "corner": view.corner,
        "color": view.fill,
        "stroke": view.stroke,
        "entries": [
            {
                "label": entry.label,
                "color": entry.color,
                "shape": entry.shape,
                **({"description": entry.description} if entry.description else {}),
            }
            for entry in view.entries
        ],
    }
    if view.title:
        payload["title"] = view.title
    return payload


def _annotation_geometry(
    x: float | None, y: float | None, width: float | None, height: float | None
) -> dict[str, Any]:
    """An annotation's pinned position and size, in the §18 coordinate system.

    Shaped like every other ``layout`` object in this document — a ``position``
    and a ``size``, points, ``y`` upwards, the position being the centre — so a
    client reads one geometry, not two.
    """
    payload: dict[str, Any] = {}
    if x is not None and y is not None:
        payload["position"] = {"x": x, "y": y}
    if width is not None and height is not None:
        payload["size"] = {"width": width, "height": height}
    return payload


def _placement(placement: Placement) -> dict[str, Any]:
    payload: dict[str, Any] = {"position": {"x": placement.x, "y": placement.y}}
    if placement.width is not None and placement.height is not None:
        payload["size"] = {"width": placement.width, "height": placement.height}
    return payload


def _node(
    node: Node,
    options: RenderOptions,
    geometry: Geometry,
    styles: StyleMap = _NO_STYLES,
) -> dict[str, Any]:
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
    if node.power is not None:
        payload["power"] = _power(node.power)
    if node.netns is not None:
        payload["netns"] = _netns(node.netns)
        if node.address != node.fqn:
            # The element a click on this node acts on (§23.1). Emitted only
            # where it differs from ``id`` — that is, on a stack node, which is
            # the one node whose identity is not an address ``netgraph edit``
            # accepts. See :attr:`~netgraph.render.graph.Node.address`.
            payload["address"] = node.address
    if node.security is not None:
        payload["zone"] = _zone(node.security)
    if node.cluster:
        payload["cluster"] = node.cluster
    payload["vlans"] = sorted(node.vlans)
    payload["interfaces"] = [_port(port, options) for port in node.ports]
    placement = geometry.nodes.get(node.fqn)
    if placement is not None:
        payload["layout"] = _placement(placement)
    payload["style"] = styles.node(node.fqn).to_dict()
    diff = _diff_mark(options.diff, node.fqn)
    if diff is not None:
        payload["diff"] = diff
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
                # What this unit costs, or how full this strip is (§17.5). Only
                # when the inventory records it: a defaulted zero would be a
                # claim that the box draws nothing.
                **({"power": _power(slot.power)} if slot.power is not None else {}),
            }
            for slot in view.slots
        ],
        "units": [
            {"unit": unit, "element": slot.element if slot is not None else None}
            for unit, slot in reversed(view.elevation())
        ],
    }


def _power(view: PowerNode) -> dict[str, Any]:
    """What one element contributes to the power view (§17.5).

    Emitted structurally rather than as the rendered label, and never defaulted:
    a consumer must be able to tell "no capacity recorded" from "0 W", which is
    the whole difference between a strip nobody measured and one nobody may use.
    ``roles`` is first because it says which of the rest are meaningful — a switch
    is commonly a load and a source at once.
    """
    payload: dict[str, Any] = {"roles": [str(role) for role in view.roles]}
    if view.draw_watts:
        payload["drawWatts"] = view.draw_watts
    if view.maximum_watts and view.maximum_watts != view.draw_watts:
        payload["maximumWatts"] = view.maximum_watts
    if view.is_pdu:
        payload["outlets"] = view.outlets
        payload["usedOutlets"] = view.used_outlets
        payload["loadWatts"] = round(view.load_watts, 3)
        payload["failoverWatts"] = round(view.failover_watts, 3)
        if view.capacity_watts is not None:
            payload["capacityWatts"] = view.capacity_watts
        if view.input_feed:
            payload["inputFeed"] = view.input_feed
    if view.poe_budget_watts is not None:
        payload["poeBudgetWatts"] = view.poe_budget_watts
    if view.poe_allocated_watts:
        payload["poeAllocatedWatts"] = round(view.poe_allocated_watts, 3)
    if view.inputs:
        payload["inputs"] = view.inputs
    if view.redundant:
        payload["redundant"] = True
    if view.powered_by_poe:
        payload["poweredBy"] = "poe"
    utilisation = view.utilisation
    if utilisation is not None:
        payload["utilisation"] = round(utilisation, 6)
    return payload


def _feed(feed: Feed) -> dict[str, Any]:
    """One power feed: the socket, the supply and what it carries (§17.5)."""
    payload: dict[str, Any] = {"feedKind": str(feed.kind), "source": feed.source}
    if feed.outlet:
        payload["outlet"] = feed.outlet
    if feed.port:
        payload["port"] = feed.port
    if feed.peer_port:
        payload["peerPort"] = feed.peer_port
    if feed.psu:
        payload["psu"] = feed.psu
    if feed.through:
        payload["through"] = list(feed.through)
    if feed.reserved_watts:
        payload["reservedWatts"] = round(feed.reserved_watts, 3)
    if feed.element_watts:
        payload["elementWatts"] = feed.element_watts
    if feed.input_feed:
        payload["inputFeed"] = feed.input_feed
    return payload


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


def _netns(view: NetnsView) -> dict[str, Any]:
    """One network namespace of one machine (§23.3).

    ``path`` rather than only ``name`` because nesting is the fact a consumer
    cannot re-derive: ``parent`` gives it one link of the chain and the chain is
    what says how deep the stack is. The initial namespace emits an empty name
    and an empty path, which is exactly what distinguishes it.
    """
    payload: dict[str, Any] = {"name": view.name, "element": view.element}
    if view.path:
        payload["path"] = list(view.path)
    if view.parent:
        payload["parent"] = view.parent
    if view.description:
        payload["description"] = view.description
    return payload


def _routing(view: RoutingView) -> dict[str, Any]:
    """What one element contributes to the control plane (§16.8).

    Only what the inventory states is emitted, so a consumer can tell "runs no
    OSPF" from "runs OSPF in area 0.0.0.0" — which a defaulted area would not
    allow. ``routes`` and ``policy`` are the rendered forms (``0.0.0.0/0 via
    203.0.113.1``, ``100: from 10.20.0.0/16 lookup uplink-b``), the same strings
    the diagram prints; a consumer that wants the fields reads the inventory,
    which is where they are declared. ``policy`` is in priority order, which is
    the order the device walks it.
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
    if view.tables:
        payload["tables"] = [{"name": name, "id": number} for name, number in view.tables]
    if view.routes:
        payload["routes"] = list(view.routes)
    if view.policy:
        payload["policy"] = list(view.policy)
    return payload


def _zone(view: SecurityView) -> dict[str, Any]:
    """What one security zone is, for a consumer of the graph (§24.5).

    ``declared`` is emitted rather than left to be inferred from the name: a
    consumer must be able to tell a zone the inventory wrote from ``local`` and
    ``any``, which the view minted, without knowing that those two words are
    reserved.
    """
    payload: dict[str, Any] = {
        "element": view.element,
        "elementKind": view.owner_kind,
        "name": view.name,
        "declared": view.is_declared,
    }
    if view.interfaces:
        payload["interfaces"] = list(view.interfaces)
    if view.description:
        payload["description"] = view.description
    if view.rules:
        payload["rules"] = view.rules
    if view.translations:
        payload["translations"] = view.translations
    return payload


def _policy(view: PolicyView) -> dict[str, Any]:
    """What a device's firewall does between two of its zones (§24.5).

    ``rules`` and ``translations`` are the rendered forms — the same strings the
    diagram prints — in the order the device walks them; a consumer that wants
    the fields reads the inventory, which is where they are declared.
    """
    payload: dict[str, Any] = {
        "from": view.source,
        "to": view.target,
        "verdict": view.verdict,
        "rules": list(view.rules),
    }
    if view.translations:
        payload["translations"] = list(view.translations)
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
    # Emitted at every layer, not only ``netns``: which stack an interface is
    # in is a fact about the interface, and a consumer reading ``10.0.0.1/24``
    # off two ports of one machine has to be able to tell whether they collide.
    if port.netns:
        payload["netns"] = port.netns
    if port.peer is not None:
        payload["peer"] = port.peer
    return payload


#: The arrangement a member of a bundle is exported against: none. A bundle is
#: drawn as one edge, so the links folded into it have no geometry of their own.
_NO_GEOMETRY: Final[Geometry] = Geometry()


def _link_layout(
    edge: Edge,
    geometry: Geometry,
    line: Route | None,
    computed: tuple[tuple[float, float], ...] = (),
) -> dict[str, Any]:
    """What this document says about how one link is drawn.

    Three different things, and a consumer wants all three. ``waypoints``,
    ``routing`` and ``label`` are what the *inventory pins* — the decisions
    somebody made, which is what an editor round-trips. ``routed`` is what
    :mod:`netgraph.layout.avoid` added on top of them so the line misses the
    boxes it passes; it is recomputed on every render, is written to no
    document, and is published so that a client can tell a computed bend from a
    chosen one rather than having to guess. ``route`` is the line netgraph
    actually draws, in the same coordinates as the node positions beside it, so
    a client can reproduce the picture without reimplementing any of this. The
    last two are only present for a fully-arranged drawing, because anywhere
    else Graphviz decides the line and this document does not know the answer.
    """
    link = geometry.link(edge.id)
    payload: dict[str, Any] = {}
    if link.waypoints:
        payload["waypoints"] = [{"x": x, "y": y} for x, y in link.waypoints]
    if computed:
        payload["routed"] = [{"x": x, "y": y} for x, y in computed]
    if link.routing is not None:
        payload["routing"] = str(link.routing)
    if link.label is not None:
        payload["label"] = {"at": link.label.at, "offset": {"x": link.label.dx, "y": link.label.dy}}
    if line is not None:
        payload["route"] = [{"x": x, "y": y} for x, y in line.corners]
        payload["controls"] = [{"x": x, "y": y} for x, y in line.controls]
        payload["drawnAs"] = str(line.routing)
    return payload


def _edge(
    edge: Edge,
    geometry: Geometry = _NO_GEOMETRY,
    overlay: DiffOverlay | None = None,
    route: Route | None = None,
    style: ResolvedStyle | None = None,
    computed: tuple[tuple[float, float], ...] = (),
) -> dict[str, Any]:
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
    elif edge.feed is not None:
        # Nor does a power feed: a cord is not a network medium, and a PoE feed
        # rides on a run the diagram draws somewhere else.
        payload["feed"] = _feed(edge.feed)
    elif edge.policy is not None:
        # Nor does a policy edge: it is a decision, not a path.
        payload["policy"] = _policy(edge.policy)
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
    layout = _link_layout(edge, geometry, route, computed)
    if layout:
        payload["layout"] = layout
    payload["style"] = (style if style is not None else PLAIN).to_dict()
    diff = _diff_mark(overlay, edge.id)
    if diff is not None:
        payload["diff"] = diff
    return payload


def _diff_mark(overlay: DiffOverlay | None, ident: str) -> dict[str, Any] | None:
    """What ``netgraph diff`` says about one node or link, or ``None``.

    Present on *every* node and edge of a diff document, untouched ones
    included: a consumer filtering for what moved must be able to tell "this
    export carries no diff" from "this element did not change", and an absent
    key would say both.
    """
    if overlay is None:
        return None
    mark = overlay.nodes.get(ident) or overlay.edges.get(ident) or Mark.UNCHANGED
    payload: dict[str, Any] = {"mark": mark.value}
    fields = overlay.fields.get(ident, ())
    if fields:
        payload["fields"] = list(fields)
    previous = overlay.renamed_from.get(ident)
    if previous is not None:
        payload["renamedFrom"] = previous
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
