"""JSON graph exporter — the machine-readable face of a rendering.

Where DOT and Mermaid are pictures, this is data: the resolved topology as a
document another tool can consume without reimplementing name resolution, VLAN
derivation or adapter attachment semantics. It is the format to reach for when
diffing two revisions of an inventory, feeding a dashboard, or asserting on a
topology in a test.

Every reference is a fully-qualified name, every collection is ordered
deterministically, and enums appear as their schema spelling — so two runs over
the same inventory produce byte-identical output.

Node types
----------

Every node carries a ``type`` discriminator, ``element`` or ``subnet``. Below
layer 3 only the first occurs; a layer-3 document mixes the two, and a consumer
must be able to tell a derived prefix from a declared device without guessing
from the identifier. A ``subnet`` node adds a ``subnet`` object — the prefix,
the family, the addresses inside it and the elements holding them — and carries
no interfaces; an ``element`` node is exactly what it is at layer 1.
"""

from __future__ import annotations

import json
from typing import Any, Final

from netgraph.models import API_VERSION
from netgraph.render.graph import Edge, EdgeKind, Graph, Node, PortView, Subnet
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
    optional ``description``, ``labels`` and — for a layer-3 prefix node —
    ``subnet``. ``id`` is the fully-qualified name, and is what every edge
    endpoint refers to. An edge is ``{id, kind, endpoints, vlans}`` plus the
    medium, speed, label and length it was declared with; ``endpoints`` is a
    two-element list of ``{node, interface}``, with ``interface`` absent when
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
    payload["vlans"] = sorted(node.vlans)
    payload["interfaces"] = [_port(port, options) for port in node.ports]
    return payload


def _subnet(subnet: Subnet) -> dict[str, Any]:
    """A subnet node's prefix, its family and who is addressed in it.

    Unlike the per-port address list, this is not gated on ``show_ips``: at
    layer 3 the addresses *are* the topology, exactly as VLAN membership is, and
    a consumer filtering by prefix would otherwise have to recompute them.
    """
    return {
        "prefix": subnet.prefix,
        "family": subnet.family,
        "addresses": list(subnet.addresses),
        "elements": list(subnet.elements),
    }


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
    else:
        payload["medium"] = edge.medium
    if edge.speed is not None:
        payload["speed"] = edge.speed
        payload["speedText"] = edge.speed_text
    if edge.label:
        payload["label"] = edge.label
    if edge.length_m is not None:
        payload["lengthM"] = edge.length_m
    payload["vlans"] = sorted(edge.vlans)
    return payload


#: Kept so callers written against the original name keep working; ``to_json``
#: is the canonical spelling, matching ``to_dot`` and ``to_mermaid``.
render_json = to_json
