"""What an info box shows: one record per drawn node and edge.

The diagram a browser gets is deliberately thin — a shape, a name, an
interface or two — because a picture that spelled everything out would be
unreadable. Everything that did not fit lives here instead, keyed by the same
identifier the shape carries (:func:`netgraph.render.dot.node_element_id`), so
the page can answer "what is this?" for whatever is under the pointer without
asking the server again.

The records *are* the JSON export
---------------------------------

Every field comes from :func:`~netgraph.render.jsonexport.graph_to_dict`,
unchanged, with two additions: an ``element`` id per record, and a ``links``
cross-reference on each node naming the edges that terminate on it. That is a
deliberate constraint rather than an implementation detail — a hover box that
computed its own view of an inventory would eventually disagree with
``netgraph render -f json``, and the person comparing the two would have no way
to tell which one was lying.

The one place the export is not followed is its display options: the graph is
exported with addresses and VLANs *on*, whatever the diagram is drawn with,
because hiding an address from a label is a decision about legibility and the
info box is where a reader goes when the label was not enough.
"""

from __future__ import annotations

from typing import Any, Final

from netgraph.render import Graph, RenderOptions, graph_to_dict
from netgraph.render.dot import edge_element_id, node_element_id

__all__ = ["DETAIL_OPTIONS", "build_details"]

#: Display options the records are built with: everything shown. See the module
#: docstring for why this ignores what the diagram was drawn with.
DETAIL_OPTIONS: Final = RenderOptions(show_ips=True, show_vlans=True)


def build_details(graph: Graph) -> dict[str, dict[str, Any]]:
    """One record per node and edge of ``graph``, keyed by element id.

    The keys are exactly the ``id`` attributes a rendering made with
    ``element_ids=True`` carries, so a front end looks a record up with the
    ``id`` of the SVG element under the cursor and nothing else.
    """
    document = graph_to_dict(graph, DETAIL_OPTIONS)
    nodes: list[dict[str, Any]] = document["nodes"]
    edges: list[dict[str, Any]] = document["edges"]

    ids = {node["id"]: node_element_id(index) for index, node in enumerate(nodes)}
    details: dict[str, dict[str, Any]] = {}

    links: dict[str, list[dict[str, Any]]] = {node["id"]: [] for node in nodes}
    for index, edge in enumerate(edges):
        element = edge_element_id(index)
        record = dict(edge, element=element, type="edge")
        record["endpoints"] = [
            dict(endpoint, element=ids.get(endpoint["node"])) for endpoint in edge["endpoints"]
        ]
        details[element] = record
        for near, far in ((0, 1), (1, 0)):
            endpoint, other = record["endpoints"][near], record["endpoints"][far]
            # A self-link terminates twice on the same node and is listed twice,
            # once per end: the two ends are different ports and a reader
            # looking at the node wants to see both.
            links.setdefault(endpoint["node"], []).append(
                {
                    "element": element,
                    "kind": edge["kind"],
                    "interface": endpoint.get("interface"),
                    "peer": other["node"],
                    "peerElement": other.get("element"),
                    "peerInterface": other.get("interface"),
                    "medium": edge.get("medium"),
                    "speedText": edge.get("speedText"),
                    "label": edge.get("label"),
                    "vlans": edge.get("vlans", []),
                }
            )

    for index, node in enumerate(nodes):
        element = node_element_id(index)
        details[element] = dict(node, element=element, links=links.get(node["id"], []))
    return details
